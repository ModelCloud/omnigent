"""Owner/private Brain controls and trusted host callbacks, separate from launch routing."""

import asyncio

from fastapi import APIRouter, Request
from pydantic import JsonValue

from omnigent.bgr import Fence, Identifier, Record
from omnigent.bgr_control import BgrControl
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.bgr_controller import BgrControllers, BgrHostRuntime
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.session_orchestration import require_orchestration_source
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore


class NativeCallback(Record):
    session_id: Identifier
    thread_id: str
    method: str
    params: dict[str, JsonValue]


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

            async def host_call(
                operation: str, parameters: dict[str, JsonValue]
            ) -> dict[str, JsonValue]:
                response = await client.post(
                    f"/v1/sessions/{session}/bgr/runtime",
                    json={"operation": operation, "parameters": parameters},
                    timeout=120,
                )
                if response.is_error:
                    raise OmnigentError(
                        "Native runtime refused operation", code=ErrorCode.CONFLICT
                    )
                from pydantic import TypeAdapter

                return TypeAdapter(dict[str, JsonValue]).validate_python(response.json())

            controller = await controllers.attach(
                user_id=user,
                brain_session_id=session,
                team_id=team_id,
                fence=Fence(controller_id=team.controller_id, epoch=team.epoch),
                runtime=BgrHostRuntime(session, host_call),
            )
            if controller.brain.thread_id != body.thread_id:
                raise OmnigentError("Native Brain thread changed", code=ErrorCode.FORBIDDEN)
            await asyncio.to_thread(
                controllers.store.mark_launch_running, user, session, team_id, controller.fence
            )
            return {"accepted": True, "consumption_receipt": False}
        controller = controllers.get(session)
        controller.authorize(user, session)
        if controller.team_id != team_id:
            raise OmnigentError("Foreign controller callback", code=ErrorCode.FORBIDDEN)
        await controller.native_callback(**body.model_dump())
        return {"accepted": True, "consumption_receipt": False}

    return router
