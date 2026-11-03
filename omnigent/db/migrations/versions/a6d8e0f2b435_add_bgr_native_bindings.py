"""Reserve native worker threads separately from logical session identities.

Revision ID: a6d8e0f2b435
Revises: f5c7d9e1a324
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import Uuid16

revision = "a6d8e0f2b435"
down_revision = "f5c7d9e1a324"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bgr_native_bindings",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("team_id", Uuid16(), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("document", CompressedText(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", "thread_id", name="uq_bgr_native_thread"),
    )


def downgrade() -> None:
    op.drop_table("bgr_native_bindings")
