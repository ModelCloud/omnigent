"""Preserve explicit stale-mailbox reconciliation separately from consumption.

Revision ID: f5c7d9e1a324
Revises: e4b6c8d0f213
"""

import sqlalchemy as sa
from alembic import op

revision = "f5c7d9e1a324"
down_revision = "e4b6c8d0f213"
branch_labels = None
depends_on = None


def _mailbox_index(*, discarded: bool) -> None:
    columns = ["workspace_id", "team_id", "recipient_agent_id", "consumed_at"]
    if discarded:
        columns.append("discarded_at")
    op.create_index("ix_bgr_mailbox", "bgr_envelopes", [*columns, "sequence"])


def upgrade() -> None:
    op.add_column("bgr_envelopes", sa.Column("discarded_at", sa.Float(), nullable=True))
    op.drop_index("ix_bgr_mailbox", table_name="bgr_envelopes")
    _mailbox_index(discarded=True)


def downgrade() -> None:
    op.drop_index("ix_bgr_mailbox", table_name="bgr_envelopes")
    with op.batch_alter_table("bgr_envelopes") as batch:
        batch.drop_column("discarded_at")
    _mailbox_index(discarded=False)
