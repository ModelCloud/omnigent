"""Owner/private Brain controls and trusted host callbacks, separate from launch routing."""

import asyncio
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request
from pydantic import JsonValue, TypeAdapter

from omnigent.bgr import Fence, Identifier, Record
from omnigent.bgr_control import BgrControl
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.bgr_controller import BgrControllers, BgrHostRuntime
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes.bgr_publications import create_bgr_publication_router
from omnigent.server.session_orchestration import require_orchestration_source
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore


class NativeCallback(Record):
    session_id: Identifier
    thread_id: str
    method: str
    params: dict[str, JsonValue]


class OwnerRecoveryRequest(Record):
    """Recovery authority is derived from durable bindings, never request attestations."""


def _host_runtime(client: httpx.AsyncClient, brain_session_id: str) -> BgrHostRuntime:
    async def host_call(operation: str, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        response = await client.post(
            f"/v1/sessions/{brain_session_id}/bgr/runtime",
            json={"operation": operation, "parameters": parameters},
            timeout=120,
        )
        if response.is_error:
            raise OmnigentError("Native runtime refused operation", code=ErrorCode.CONFLICT)
        return TypeAdapter(dict[str, JsonValue]).validate_python(response.json())

    return BgrHostRuntime(brain_session_id, host_call)


def create_bgr_control_router(
    controllers: BgrControllers,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    auth_provider: AuthProvider | None,
) -> APIRouter:
    router = APIRouter(prefix="/bgr")

    def source(request: Request) -> tuple[str, str]:
        user = require_user(request, auth_provider)
        session = require_orchestration_source(
            request, user, conversations, permissions, allow_read_only_sharing=False
        )
        assert user is not None
        return user, session.id

    @router.post("/control")
    async def control(request: Request, body: BgrControl) -> dict[str, JsonValue]:
        user, session = source(request)
        return await controllers.get(session).execute(user, session, body)

    @router.post("/teams/{team_id}/recover", status_code=202)
    async def recover(
        request: Request, team_id: Identifier, _body: OwnerRecoveryRequest
    ) -> dict[str, JsonValue]:
        user = require_user(request, auth_provider)
        if user is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        session, team, binding = await asyncio.to_thread(
            controllers.store.runtime_recovery_target, user, team_id
        )
        assert binding.runtime_id is not None and binding.host_id is not None
        await require_access(user, session, LEVEL_OWNER, permissions, conversations)
        if team.controller_id is None:
            raise OmnigentError(
                "Native controller identity is unavailable", code=ErrorCode.CONFLICT
            )
        previous = team.runtime_recoveries[-1] if team.runtime_recoveries else None
        if previous is not None and previous.state != "attached":
            if previous.state != "stopping":
                raise OmnigentError(
                    "Replacement attach requires reconciliation", code=ErrorCode.CONFLICT
                )
            recovery = previous
        else:
            recovery = await asyncio.to_thread(
                controllers.store.begin_runtime_recovery,
                user,
                session,
                team_id,
                Fence(controller_id=team.controller_id, epoch=team.epoch),
                recovery_id=uuid4().hex,
                runtime_id=binding.runtime_id,
                replacement_controller_id=uuid4().hex,
            )
        try:
            from omnigent.server.routes.sessions import _get_runner_client

            conversation = await asyncio.to_thread(conversations.get_conversation, session)
            client = await _get_runner_client(session, request.app.state.runner_router)
            if (
                client is not None
                and conversation is not None
                and conversation.host_id == binding.host_id
            ):
                runtime = _host_runtime(client, session)
                metadata = await runtime.call("brain", {})
                if all(
                    metadata.get(key) == getattr(binding, key)
                    for key in ("runtime_id", "host_id", "thread_id")
                ):
                    await runtime.call("stop_generation", {"runtime_id": recovery.runtime_id})
        except (httpx.HTTPError, OmnigentError, OSError, ValueError, RuntimeError, TimeoutError):
            pass
        return {
            "recovery": recovery.model_dump(mode="json"),
            "stop_status": "unknown",
            "replacement_enabled": False,
        }

    @router.post("/teams/{team_id}/native-callback")
    async def callback(
        request: Request, team_id: Identifier, body: NativeCallback
    ) -> dict[str, JsonValue]:
        user, session = source(request)
        if body.method == "runtime/thread-ready":
            team = controllers.store.get_team(user, session, team_id)
            if body.session_id != session or team.controller_id is None:
                raise OmnigentError("Foreign runtime attach source", code=ErrorCode.FORBIDDEN)
            from omnigent.server.routes.sessions import _get_runner_client

            client = await _get_runner_client(session, request.app.state.runner_router)
            if client is None:
                raise OmnigentError("Brain runner is unavailable", code=ErrorCode.CONFLICT)

            controller = await controllers.attach(
                user_id=user,
                brain_session_id=session,
                team_id=team_id,
                fence=Fence(controller_id=team.controller_id, epoch=team.epoch),
                runtime=_host_runtime(client, session),
            )
            if controller.brain.thread_id != body.thread_id:
                raise OmnigentError("Native Brain thread changed", code=ErrorCode.FORBIDDEN)
            await asyncio.to_thread(
                controllers.store.mark_launch_running, user, session, team_id, controller.fence
            )
            startup = getattr(request.app.state, "startup_inputs", None)
            if startup is not None:
                await startup.session_ready(request.app, session)
            return {"accepted": True, "consumption_receipt": False}
        controller = controllers.get(session)
        controller.authorize(user, session)
        if controller.team_id != team_id:
            raise OmnigentError("Foreign controller callback", code=ErrorCode.FORBIDDEN)
        await controller.native_callback(**body.model_dump())
        return {"accepted": True, "consumption_receipt": False}

    router.include_router(create_bgr_publication_router(controllers, source))
    return router
