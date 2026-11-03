"""The additive BGR migration preserves existing sessions and matches ORM metadata."""

import sqlalchemy as sa
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
    _migrate(uri, engine, "upgrade", "e4b6c8d0f213")
    inspector = sa.inspect(engine)
    models = [
        table for name, table in OmnigentBase.metadata.tables.items() if name.startswith("bgr_")
    ]
    assert len(models) == 5
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
        assert isinstance(columns["document"]["type"], sa.LargeBinary)
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
