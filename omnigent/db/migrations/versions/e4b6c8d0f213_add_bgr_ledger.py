"""Add the compact BGR controller ledger and durable mailbox.

Revision ID: e4b6c8d0f213
Revises: d0a1f879a01d

Additive application-database tables. Downgrade destroys BGR recovery state;
stop controllers and reconcile outstanding work before downgrading.
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "e4b6c8d0f213"
down_revision = "d0a1f879a01d"
branch_labels = None
depends_on = None


def _identity() -> list[sa.Column]:
    return [
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("id", Uuid16(), primary_key=True),
    ]


def upgrade() -> None:
    op.create_table(
        "bgr_teams",
        *_identity(),
        sa.Column("owner_id", sa.String(320), nullable=False),
        sa.Column("brain_session_id", Uuid16(), nullable=False),
        sa.Column("document", sa.LargeBinary(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("controller_id", Uuid16(), nullable=True),
        sa.Column("lease_expires_at", sa.Float(), nullable=False),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("workspace_id", "brain_session_id", name="uq_bgr_brain_session"),
    )
    op.create_table(
        "bgr_agents",
        *_identity(),
        sa.Column("team_id", Uuid16(), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("document", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("workspace_id", "session_id", name="uq_bgr_agent_session"),
    )
    op.create_index("ix_bgr_agents_team", "bgr_agents", ["workspace_id", "team_id", "id"])
    op.create_table(
        "bgr_tasks",
        *_identity(),
        sa.Column("team_id", Uuid16(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("document", sa.LargeBinary(), nullable=False),
    )
    op.create_index("ix_bgr_tasks_team", "bgr_tasks", ["workspace_id", "team_id", "id"])
    op.create_table(
        "bgr_attempts",
        *_identity(),
        sa.Column("team_id", Uuid16(), nullable=False),
        sa.Column("task_id", Uuid16(), nullable=False),
        sa.Column("active_writer_key", sa.String(64), nullable=True),
        sa.Column("document", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("workspace_id", "active_writer_key", name="uq_bgr_workspace_writer"),
        sa.UniqueConstraint("workspace_id", "task_id", name="uq_bgr_task_attempt"),
    )
    op.create_index("ix_bgr_attempts_team", "bgr_attempts", ["workspace_id", "team_id", "id"])
    op.create_table(
        "bgr_envelopes",
        *_identity(),
        sa.Column("team_id", Uuid16(), nullable=False),
        sa.Column("sender_agent_id", Uuid16(), nullable=False),
        sa.Column("recipient_agent_id", Uuid16(), nullable=False),
        sa.Column("message_id", Uuid16(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("consumed_at", sa.Float(), nullable=True),
        sa.Column("replied_at", sa.Float(), nullable=True),
        sa.Column("document", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "team_id", "sender_agent_id", "message_id", name="uq_bgr_message"
        ),
    )
    op.create_index(
        "ix_bgr_mailbox",
        "bgr_envelopes",
        ["workspace_id", "team_id", "recipient_agent_id", "consumed_at", "sequence"],
    )


def downgrade() -> None:
    for name in ("bgr_envelopes", "bgr_attempts", "bgr_tasks", "bgr_agents", "bgr_teams"):
        op.drop_table(name)
