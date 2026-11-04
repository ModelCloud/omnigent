"""The additive BGR migration preserves existing sessions and matches ORM metadata."""

import runpy

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from omnigent.db import OmnigentBase
from omnigent.db.utils import _build_alembic_config
from tests.db.test_migration_conversation_metadata_pull_requests import _migrate
from tests.db.test_migration_conversation_metadata_pull_requests import database as database


def test_bgr_upgrade_downgrade_and_model_parity(database):
    uri, engine = database
    script = ScriptDirectory.from_config(_build_alembic_config(uri))
    assert len(script.get_heads()) == 1
    _migrate(uri, engine, "upgrade", "d0a1f879a01d")
    session = bytes.fromhex("0123456789abcdef0123456789abcdef")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO omnigent_conversation_metadata (workspace_id, id, kind) "
                "VALUES (0, :id, 1)"
            ),
            {"id": session},
        )
    _migrate(uri, engine, "upgrade", "head")
    inspector = sa.inspect(engine)
    models = [
        table for name, table in OmnigentBase.metadata.tables.items() if name.startswith("bgr_")
    ]
    assert len(models) >= 8
    for table in models:
        assert set(table.c.keys()) == {
            column["name"] for column in inspector.get_columns(table.name)
        }
        assert set(inspector.get_pk_constraint(table.name)["constrained_columns"]) == {
            "workspace_id",
            "id",
        }
        actual_unique = {
            constraint["name"] for constraint in inspector.get_unique_constraints(table.name)
        }
        expected_unique = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        assert actual_unique == expected_unique
        assert {index["name"] for index in inspector.get_indexes(table.name)} == {
            index.name for index in table.indexes
        }
        columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        payload_name = "response" if table.name == "bgr_control_actions" else "document"
        assert isinstance(columns[payload_name]["type"], sa.LargeBinary)
        assert table.c[payload_name].type.max_stored_bytes == (
            65_535 if table.name == "bgr_launches" else 16_383
        )
        for column in columns.values():
            assert column["default"] is None
            assert not isinstance(column["type"], sa.Float)
            if column["name"].endswith("_at_us"):
                assert isinstance(column["type"], sa.BigInteger)
        for index in inspector.get_indexes(table.name):
            expected = next(i for i in table.indexes if i.name == index["name"])
            assert index["column_names"] == [column.name for column in expected.columns]
    native = {column["name"]: column for column in inspector.get_columns("bgr_native_bindings")}
    assert isinstance(native["thread_id"]["type"], sa.LargeBinary)
    _migrate(uri, engine, "downgrade", "d0a1f879a01d")
    assert not any(name.startswith("bgr_") for name in sa.inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT kind FROM omnigent_conversation_metadata WHERE id = :id"),
                {"id": session},
            ).scalar_one()
            == 1
        )
    _migrate(uri, engine, "upgrade", "head")
    assert {table.name for table in models} <= set(sa.inspect(engine).get_table_names())


def test_bgr_ddl_without_application_metadata(database):
    uri, engine = database
    scripts = ScriptDirectory.from_config(_build_alembic_config(uri))
    revisions = [
        revision.revision
        for revision in reversed(list(scripts.iterate_revisions("heads", "d0a1f879a01d")))
    ]
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        for revision in revisions:
            namespace = runpy.run_path(scripts.get_revision(revision).path)
            namespace["upgrade"]()
        inspector = sa.inspect(connection)
        assert (
            len([table for table in inspector.get_table_names() if table.startswith("bgr_")]) >= 8
        )
        for table in inspector.get_table_names():
            assert all(column["default"] is None for column in inspector.get_columns(table))
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "INSERT INTO bgr_native_bindings (id, team_id, thread_id, document) "
                    "VALUES (:id, :id, :id, :document)"
                ),
                {"id": bytes.fromhex("1" * 32), "document": b"{}"},
            )
        for revision in reversed(revisions):
            runpy.run_path(scripts.get_revision(revision).path)["downgrade"]()
        assert not sa.inspect(connection).get_table_names()


def test_discard_migration_preserves_old_envelopes(database):
    uri, engine = database
    _migrate(uri, engine, "upgrade", "e4b6c8d0f213")
    envelope_id = bytes.fromhex("1" * 32)
    document = b'{"legacy_envelope":"preserved"}'
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO bgr_envelopes (workspace_id, id, team_id, sender_agent_id, "
                "recipient_agent_id, message_id, request_hash, sequence, document) "
                "VALUES (0, :id, :id, :id, :id, :id, :hash, 1, :document)"
            ),
            {"id": envelope_id, "hash": "0" * 64, "document": document},
        )
    _migrate(uri, engine, "upgrade", "f5c7d9e1a324")
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT document, consumed_at_us, replied_at_us, discarded_at_us "
                "FROM bgr_envelopes"
            )
        ).one()
        assert tuple(row) == (document, None, None, None)
    _migrate(uri, engine, "downgrade", "e4b6c8d0f213")
    assert "discarded_at_us" not in {
        column["name"] for column in sa.inspect(engine).get_columns("bgr_envelopes")
    }
    with engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT document FROM bgr_envelopes")).scalar_one()
            == document
        )
    _migrate(uri, engine, "upgrade", "head")
