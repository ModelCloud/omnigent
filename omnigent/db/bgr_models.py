"""BGR ledger tables, on the application database rather than conversation storage."""

from sqlalchemy import BigInteger, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import OmnigentBase, Uuid16


class _ScopedRow:
    workspace_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)


class SqlBgrTeam(_ScopedRow, OmnigentBase):
    __tablename__ = "bgr_teams"

    owner_id: Mapped[str] = mapped_column(String(320))
    brain_session_id: Mapped[str] = mapped_column(Uuid16())
    document: Mapped[str] = mapped_column(CompressedText(max_stored_bytes=16_383))
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    controller_id: Mapped[str | None] = mapped_column(Uuid16())
    lease_expires_at_us: Mapped[int] = mapped_column(BigInteger, default=0)
    next_sequence: Mapped[int] = mapped_column(BigInteger, default=1)
    __table_args__ = (
        UniqueConstraint("workspace_id", "brain_session_id", name="uq_bgr_brain_session"),
    )


class SqlBgrAgent(_ScopedRow, OmnigentBase):
    __tablename__ = "bgr_agents"

    team_id: Mapped[str] = mapped_column(Uuid16())
    session_id: Mapped[str] = mapped_column(Uuid16())
    document: Mapped[str] = mapped_column(CompressedText(max_stored_bytes=16_383))
    __table_args__ = (
        UniqueConstraint("workspace_id", "session_id", name="uq_bgr_agent_session"),
        Index("ix_bgr_agents_team", "workspace_id", "team_id", "id"),
    )


class SqlBgrTask(_ScopedRow, OmnigentBase):
    __tablename__ = "bgr_tasks"

    team_id: Mapped[str] = mapped_column(Uuid16())
    request_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[str] = mapped_column(CompressedText(max_stored_bytes=16_383))
    __table_args__ = (Index("ix_bgr_tasks_team", "workspace_id", "team_id", "id"),)


class SqlBgrAttempt(_ScopedRow, OmnigentBase):
    __tablename__ = "bgr_attempts"

    team_id: Mapped[str] = mapped_column(Uuid16())
    task_id: Mapped[str] = mapped_column(Uuid16())
    active_writer_key: Mapped[str | None] = mapped_column(String(64))
    document: Mapped[str] = mapped_column(CompressedText(max_stored_bytes=16_383))
    __table_args__ = (
        UniqueConstraint("workspace_id", "active_writer_key", name="uq_bgr_workspace_writer"),
        UniqueConstraint("workspace_id", "task_id", name="uq_bgr_task_attempt"),
        Index("ix_bgr_attempts_team", "workspace_id", "team_id", "id"),
        Index("ix_bgr_attempts_active", "workspace_id", "team_id", "active_writer_key", "id"),
    )


class SqlBgrEnvelope(_ScopedRow, OmnigentBase):
    __tablename__ = "bgr_envelopes"

    team_id: Mapped[str] = mapped_column(Uuid16())
    sender_agent_id: Mapped[str] = mapped_column(Uuid16())
    recipient_agent_id: Mapped[str] = mapped_column(Uuid16())
    message_id: Mapped[str] = mapped_column(Uuid16())
    request_hash: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(BigInteger)
    consumed_at_us: Mapped[int | None] = mapped_column(BigInteger)
    replied_at_us: Mapped[int | None] = mapped_column(BigInteger)
    discarded_at_us: Mapped[int | None] = mapped_column(BigInteger)
    document: Mapped[str] = mapped_column(CompressedText(max_stored_bytes=16_383))
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "team_id", "sender_agent_id", "message_id", name="uq_bgr_message"
        ),
        Index(
            "ix_bgr_mailbox",
            "workspace_id",
            "team_id",
            "recipient_agent_id",
            "consumed_at_us",
            "discarded_at_us",
            "sequence",
            "id",
        ),
        Index(
            "ix_bgr_pending_team",
            "workspace_id",
            "team_id",
            "consumed_at_us",
            "discarded_at_us",
            "sequence",
            "id",
        ),
    )


class SqlBgrNativeBinding(_ScopedRow, OmnigentBase):
    __tablename__ = "bgr_native_bindings"

    team_id: Mapped[str] = mapped_column(Uuid16())
    thread_id: Mapped[str] = mapped_column(Uuid16())
    document: Mapped[str] = mapped_column(CompressedText(max_stored_bytes=16_383))
    __table_args__ = (UniqueConstraint("workspace_id", "thread_id", name="uq_bgr_native_thread"),)
