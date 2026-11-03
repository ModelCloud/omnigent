"""Owner/private-session BGR ledger APIs, not an execution-mode launcher."""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from omnigent.bgr import (
    Ack,
    Attempt,
    Checkpoint,
    ConfigUpdate,
    Envelope,
    Fence,
    Identifier,
    LeaseRequest,
    MailboxDiscard,
    Receipt,
    Recovery,
    Send,
    Task,
    TaskClose,
    TaskReserve,
    Team,
    TeamCreate,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.session_orchestration import (
    require_orchestration_source,
    require_owned_session,
)
from omnigent.stores.bgr_store import BgrStore
from omnigent.stores.conversation_store import CREATED_BY_SESSION_LABEL_KEY, ConversationStore
from omnigent.stores.permission_store import PermissionStore


def create_bgr_router(
    store: BgrStore,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    auth_provider: AuthProvider | None,
) -> APIRouter:
    router = APIRouter(prefix="/bgr")

    def source(request: Request) -> tuple[str, str]:
        user = require_user(request, auth_provider)
        session = require_orchestration_source(request, user, conversations, permissions)
        assert user is not None
        return user, session.id

    @router.get("/capabilities")
    def capabilities(request: Request) -> dict[str, object]:
        source(request)
        return {
            "protocol_version": 1,
            "ledger": True,
            "durable_mailbox": True,
            "runtime_launch": False,
            "runtime_fencing": False,
            "safe_boundary_injection": False,
            "reviewer_isolation": False,
            "profile_catalog_validation": False,
        }

    @router.post("/teams", status_code=201)
    def create(request: Request, body: TeamCreate) -> Team:
        user, session = source(request)
        return store.create(user, session, body)

    @router.get("/teams/{team_id}")
    def recover(request: Request, team_id: Identifier) -> Recovery:
        user, session = source(request)
        return store.recover(user, session, team_id)

    @router.post("/teams/{team_id}/lease")
    def acquire(request: Request, team_id: Identifier, body: LeaseRequest) -> Team:
        user, session = source(request)
        return store.acquire(user, session, team_id, body)

    @router.put("/teams/{team_id}/configuration")
    def configure(request: Request, team_id: Identifier, body: ConfigUpdate) -> Team:
        user, session = source(request)
        return store.configure(user, session, team_id, body)

    @router.post("/teams/{team_id}/tasks", status_code=201)
    def reserve(request: Request, team_id: Identifier, body: TaskReserve) -> Task:
        user, session = source(request)
        # This registers an existing child, never creates or starts a process.
        store.recover(user, session, team_id)
        assert permissions is not None
        child = require_owned_session(
            body.session_id, user, conversations, permissions, private=True
        )
        if (
            child.parent_conversation_id != session
            and child.labels.get(CREATED_BY_SESSION_LABEL_KEY) != session
        ):
            raise OmnigentError(
                "BGR reservations require a worker created by the brain", code=ErrorCode.FORBIDDEN
            )
        if child.host_id != body.workspace.host_id:
            raise OmnigentError(
                "Workspace host does not match the worker session", code=ErrorCode.INVALID_INPUT
            )
        if child.workspace != body.workspace.worktree:
            raise OmnigentError(
                "Workspace path does not match the worker session", code=ErrorCode.INVALID_INPUT
            )
        return store.reserve(user, session, team_id, body)

    @router.post("/teams/{team_id}/checkpoints")
    def checkpoint(request: Request, team_id: Identifier, body: Checkpoint) -> Attempt:
        user, session = source(request)
        return store.checkpoint(user, session, team_id, body)

    @router.post("/teams/{team_id}/attempts/{attempt_id}/reattach")
    def reattach(
        request: Request, team_id: Identifier, attempt_id: Identifier, body: Fence
    ) -> Attempt:
        user, session = source(request)
        return store.reattach(user, session, team_id, attempt_id, body)

    @router.post("/teams/{team_id}/attempts/close")
    def close(request: Request, team_id: Identifier, body: TaskClose) -> Attempt:
        user, session = source(request)
        return store.close(user, session, team_id, body)

    @router.post("/teams/{team_id}/envelopes", status_code=202)
    def send(request: Request, team_id: Identifier, body: Send) -> Receipt:
        user, session = source(request)
        return store.send(user, session, team_id, body)

    @router.get("/teams/{team_id}/mailbox")
    def drain(
        request: Request,
        team_id: Identifier,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[Envelope]:
        user, session = source(request)
        return store.drain(user, session, team_id, limit)

    @router.post("/teams/{team_id}/mailbox/ack")
    def ack(request: Request, team_id: Identifier, body: Ack) -> Envelope:
        user, session = source(request)
        return store.ack(user, session, team_id, body)

    @router.post("/teams/{team_id}/mailbox/discard")
    def discard(request: Request, team_id: Identifier, body: MailboxDiscard) -> list[Envelope]:
        user, session = source(request)
        return store.discard(user, session, team_id, body)

    @router.post("/teams/{team_id}/launch")
    def launch(request: Request, team_id: Identifier) -> None:
        user, session = source(request)
        store.recover(user, session, team_id)
        raise OmnigentError(
            "BGR execution is unavailable: role enforcement, safe-boundary delivery "
            "and reviewer isolation are not implemented",
            code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
        )

    return router
