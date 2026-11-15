"""Durable claims around non-idempotent Brain control effects."""

from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import OmnigentBase, Uuid16


class SqlBgrControlAction(OmnigentBase):
    __tablename__ = "bgr_control_actions"

    workspace_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    team_id: Mapped[str] = mapped_column(Uuid16())
    epoch: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    response: Mapped[str | None] = mapped_column(CompressedText(max_stored_bytes=16_383))
