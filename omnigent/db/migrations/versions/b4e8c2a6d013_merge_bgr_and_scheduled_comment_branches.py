"""Merge the BGR and scheduled-task/comment schema branches.

Revision ID: b4e8c2a6d013
Revises: e0b6d4f2a135, a7d4c2e9f610

Both parent revisions remain unchanged. Upgrade and downgrade only reconcile
Alembic's version rows; the merge itself changes no application schema or data.
"""

from __future__ import annotations

revision: str = "b4e8c2a6d013"
down_revision: tuple[str, ...] = ("e0b6d4f2a135", "a7d4c2e9f610")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent version markers without undoing either schema."""
