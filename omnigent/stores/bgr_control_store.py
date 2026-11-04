"""Server-side action claims; runner processes never open this SQL store."""

import hashlib
import json

from pydantic import JsonValue, TypeAdapter
from sqlalchemy.orm import Session

from omnigent.bgr import Fence
from omnigent.bgr_control import BgrControl
from omnigent.db.bgr_control_models import SqlBgrControlAction
from omnigent.db.db_models import current_workspace_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.bgr_store import BgrStore

_OBJECT = TypeAdapter(dict[str, JsonValue])


class BgrControlActions:
    def __init__(self, store: BgrStore):
        self.store = store

    def claim(
        self, user: str, brain: str, team: str, fence: Fence, command: BgrControl
    ) -> dict[str, JsonValue] | None:
        digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()

        def apply(db: Session) -> dict[str, JsonValue] | None:
            owner = self.store._owned(db, team, user)
            self.store._fence(owner, brain, fence)
            key = (current_workspace_id(), command.action_id)
            existing = db.get(SqlBgrControlAction, key, with_for_update=True)
            if existing is not None:
                if (
                    existing.team_id != team
                    or existing.epoch != fence.epoch
                    or existing.request_hash != digest
                ):
                    raise OmnigentError("Control action ID conflict", code=ErrorCode.CONFLICT)
                if existing.state == "completed" and existing.response is not None:
                    return _OBJECT.validate_json(existing.response)
                if command.action == "publish":
                    return {"_publication_reconcile": True}
                raise OmnigentError(
                    "Control action is pending or uncertain; reconcile before retry",
                    code=ErrorCode.CONFLICT,
                )
            db.add(
                SqlBgrControlAction(
                    workspace_id=current_workspace_id(),
                    id=command.action_id,
                    team_id=team,
                    epoch=fence.epoch,
                    request_hash=digest,
                    state="running",
                )
            )
            return None

        return self.store._write("claim_brain_control_action", apply)

    def finish(
        self,
        user: str,
        brain: str,
        team: str,
        fence: Fence,
        action_id: str,
        response: dict[str, JsonValue] | None,
    ) -> None:
        def apply(db: Session) -> None:
            owner = self.store._owned(db, team, user)
            self.store._fence(owner, brain, fence)
            row = db.get(
                SqlBgrControlAction, (current_workspace_id(), action_id), with_for_update=True
            )
            if row is None or row.team_id != team or row.epoch != fence.epoch:
                raise OmnigentError("Unknown control action claim", code=ErrorCode.CONFLICT)
            row.state = "uncertain" if response is None else "completed"
            row.response = (
                None if response is None else json.dumps(response, separators=(",", ":"))
            )

        self.store._write("settle_brain_control_action", apply)
