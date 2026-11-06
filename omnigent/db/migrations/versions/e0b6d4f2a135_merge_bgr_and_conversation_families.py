"""Merge BGR and conversation-family migration histories.

Revision ID: e0b6d4f2a135
Revises: d9e4c3b2a167, f1c7a9e2b406

Both parent revisions remain unchanged. Only Alembic version markers change.
"""

from __future__ import annotations

revision: str = "e0b6d4f2a135"
down_revision: tuple[str, ...] = ("d9e4c3b2a167", "f1c7a9e2b406")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent version markers without undoing either schema."""
