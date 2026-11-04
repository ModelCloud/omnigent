"""Add retry-safe BGR Brain launch intents.

Revision ID: c8f0a2b4d657
Revises: b7e9f1a3c546
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import Uuid16

revision = "c8f0a2b4d657"
down_revision = "b7e9f1a3c546"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bgr_launches",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("owner_id", sa.String(320), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("document", CompressedText(max_stored_bytes=65_535), nullable=False),
        sa.Column("claim_id", Uuid16(), nullable=True),
        sa.Column("claim_expires_at_us", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("bgr_launches")
