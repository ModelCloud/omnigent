"""Add independent review rounds and their publication outbox.

Revision ID: b7e9f1a3c546
Revises: a6d8e0f2b435
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import Uuid16

revision = "b7e9f1a3c546"
down_revision = "a6d8e0f2b435"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bgr_review_rounds",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("team_id", Uuid16(), nullable=False),
        sa.Column("document", CompressedText(max_stored_bytes=16_383), nullable=False),
    )
    op.create_index("ix_bgr_reviews_team", "bgr_review_rounds", ["workspace_id", "team_id", "id"])


def downgrade() -> None:
    op.drop_table("bgr_review_rounds")
