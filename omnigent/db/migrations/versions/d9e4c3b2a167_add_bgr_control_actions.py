"""Add durable Brain action claims.

Revision ID: d9e4c3b2a167
Revises: c8f0a2b4d657
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import Uuid16

revision = "d9e4c3b2a167"
down_revision = "c8f0a2b4d657"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bgr_control_actions",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("team_id", Uuid16(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("response", CompressedText(max_stored_bytes=16_383), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("bgr_control_actions")
