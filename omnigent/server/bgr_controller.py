"""Authoritative BGR scheduler. Native processes and private values remain on their host."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import NAMESPACE_URL, uuid5

from pydantic import JsonValue, TypeAdapter

from omnigent.bgr import (
    Fence,
    LeaseRequest,
    NativeBinding,
    NativeBrainBinding,
    TaskClose,
    TaskReserve,
    Workspace,
)
from omnigent.bgr_control import BgrControl
from omnigent.bgr_review import ReviewRoundCreate
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.codex_native.app_server import CodexMessage, CodexParams, CodexRequestFn
from omnigent.harnesses.codex_native.bgr import NativeCapabilities
from omnigent.harnesses.codex_native.bgr_lifecycle import NativeReviewOptions
from omnigent.harnesses.codex_native.bgr_messages import NativeBgrConnection, NativeIdentity
from omnigent.runner.bgr_mailbox import BgrNativeMailbox
from omnigent.runner.bgr_reviewer import BgrReviewerRunner
from omnigent.stores.bgr_control_store import BgrControlActions
from omnigent.stores.bgr_review_store import BgrReviewStore
from omnigent.stores.bgr_store import BgrStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore

HostCall = Callable[[str, dict[str, JsonValue]], Awaitable[dict[str, JsonValue]]]
CreateRoleSession = Callable[[str, str, str, str, Workspace], Awaitable[None]]
_logger = logging.getLogger(__name__)


class BgrHostRuntime:
    """An owner-checked runner tunnel, not a local native client or SQL store."""

    def __init__(self, brain_session_id: str, call: HostCall):
        self.brain_session_id = brain_session_id
        self.call = call

    def request(self, session_id: str) -> CodexRequestFn:
        async def request(method: str, params: CodexParams) -> CodexMessage:
            response = await self.call(
                "rpc",
                {
                    "session_id": session_id,
                    "method": method,
                    "params": TypeAdapter(dict[str, JsonValue]).validate_python(params),
                },
            )
            return dict(response)

        return request

    async def stop(self, session_id: str) -> bool:
        result = await self.call("stop", {"session_id": session_id})
        return result.get("stop_confirmed") is True


class BgrController:
    def __init__(
        self,
        *,
        store: BgrReviewStore,
        user_id: str,
        brain_session_id: str,
        team_id: str,
        fence: Fence,
        brain: NativeBgrConnection,
        runtime: BgrHostRuntime,
        create_role_session: CreateRoleSession,
    ):
        self.store = store
        self.user_id = user_id
        self.brain_session_id = brain_session_id
        self.team_id = team_id
        self.fence = fence
        self.brain = brain
        self.runtime = runtime
        self.create_role_session = create_role_session
        self.actions = BgrControlActions(store)
        self.mailbox = BgrNativeMailbox(
            store,
            user_id=user_id,
            brain_session_id=brain_session_id,
            team_id=team_id,
            fence=fence,
            root="/",
            private_values=lambda: (),
        )
        self.connections = {brain_session_id: brain}
        self.reviewer = BgrReviewerRunner(self.mailbox, brain, self._register)
        self._actions_lock = asyncio.Lock()
        self._callbacks_lock = asyncio.Lock()
        self._heartbeat: asyncio.Task[None] | None = None

    async def maintain_lease(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                await self.brain.inspect()
                self.authorize(self.user_id, self.brain_session_id)
                team = self.store.acquire(
                    self.user_id,
                    self.brain_session_id,
                    self.team_id,
                    LeaseRequest(controller_id=self.fence.controller_id, ttl_seconds=300),
                )
                if team.epoch != self.fence.epoch:
                    raise OmnigentError("Brain lease epoch changed", code=ErrorCode.CONFLICT)
        except (OmnigentError, OSError, ValueError, RuntimeError, TimeoutError):
            _logger.warning("BGR lease renewal stopped; native roles require reconciliation")
            await asyncio.gather(
                *(self.runtime.stop(session) for session in self.connections),
                return_exceptions=True,
            )

    def _register(self, connection: NativeBgrConnection) -> None:
        previous = self.connections.get(connection.identity.session_id)
        if previous is not None and previous != connection:
            raise OmnigentError("Native role already registered", code=ErrorCode.CONFLICT)
        self.connections[connection.identity.session_id] = connection

    def authorize(self, user_id: str, source_session_id: str) -> None:
        if source_session_id != self.brain_session_id or user_id != self.user_id:
            raise OmnigentError(
                "Only the durable Brain controls this team", code=ErrorCode.FORBIDDEN
            )
        self.store.native_brain_binding(user_id, source_session_id, self.team_id, self.fence)

    async def execute(
        self, user_id: str, source_session_id: str, command: BgrControl
    ) -> dict[str, JsonValue]:
        self.authorize(user_id, source_session_id)
        async with self._actions_lock:
            if command.action in {"status", "publication_preview"}:
                return await self._execute(command)
            previous = self.actions.claim(
                user_id, source_session_id, self.team_id, self.fence, command
            )
            if previous is not None:
                return previous
            try:
                result = await self._execute(command)
            except BaseException:
                self.actions.finish(
                    user_id, source_session_id, self.team_id, self.fence, command.action_id, None
                )
                raise
            self.actions.finish(
                user_id, source_session_id, self.team_id, self.fence, command.action_id, result
            )
            return result

    async def _execute(self, command: BgrControl) -> dict[str, JsonValue]:
        if command.action == "create_grunt":
            return await self._create_grunt(command)
        if command.action == "start_review":
            return await self._start_review(command)
        if command.action == "status":
            if command.round_id is not None:
                return self.store.review_status(
                    self.user_id, self.brain_session_id, self.team_id, command.round_id
                )
            recovery = self.store.recover(self.user_id, self.brain_session_id, self.team_id)
            return {
                "team": recovery.team.model_dump(mode="json"),
                "tasks": [task.model_dump(mode="json") for task in recovery.tasks],
            }
        if command.action == "adjudicate":
            assert command.attempt_id is not None and command.outcome is not None
            assignment = self.store.native_assignment(
                self.user_id, self.brain_session_id, self.team_id, command.attempt_id, self.fence
            )
            if not await self.runtime.stop(assignment.attempt.session_id):
                raise OmnigentError("Worker stop is unconfirmed", code=ErrorCode.CONFLICT)
            attempt = self.store.close(
                self.user_id,
                self.brain_session_id,
                self.team_id,
                TaskClose(
                    **self.fence.model_dump(),
                    attempt_id=command.attempt_id,
                    outcome=command.outcome,
                    stop_confirmed=True,
                ),
            )
            return {"attempt": attempt.model_dump(mode="json")}
        assert command.round_id is not None and command.review_id is not None
        status = self.store.review_status(
            self.user_id, self.brain_session_id, self.team_id, command.round_id
        )
        if status.get("results_ready") is not True:
            raise OmnigentError(
                "Independent sibling results are withheld", code=ErrorCode.CONFLICT
            )
        return await self.runtime.call(
            command.action,
            {
                "team_id": self.team_id,
                "round_id": command.round_id,
                "review_id": command.review_id,
                "epoch": self.fence.epoch,
                "controller_id": self.fence.controller_id,
                "user_id": self.user_id,
                "approved_body_sha256": command.approved_body_sha256,
            },
        )

    async def _create_grunt(self, command: BgrControl) -> dict[str, JsonValue]:
        assert command.contract is not None and command.base_sha is not None
        recovery = self.store.recover(self.user_id, self.brain_session_id, self.team_id)
        if (
            sum(
                task.attempt.state == "reserved" and task.attempt.review_round_id is None
                for task in recovery.tasks
            )
            >= recovery.team.config.max_active_grunts
        ):
            raise OmnigentError("BGR active grunt cap reached", code=ErrorCode.CONFLICT)
        session_id = uuid5(NAMESPACE_URL, f"bgr:{self.team_id}:{command.action_id}").hex
        prepared = await self.runtime.call(
            "workspace", {"task_id": command.action_id, "base_sha": command.base_sha}
        )
        workspace = Workspace.model_validate(prepared.get("workspace"))
        await self.create_role_session(
            self.user_id, self.brain_session_id, session_id, "grunt", workspace
        )
        task = self.store.reserve(
            self.user_id,
            self.brain_session_id,
            self.team_id,
            TaskReserve(
                **self.fence.model_dump(),
                task_id=command.action_id,
                session_id=session_id,
                contract=command.contract,
                workspace=workspace,
            ),
        )
        started = await self.runtime.call(
            "spawn",
            {
                "session_id": session_id,
                "role": "grunt",
                "epoch": self.fence.epoch,
                "profile": task.attempt.profile.model_dump(mode="json"),
                "workspace": workspace.model_dump(mode="json"),
            },
        )
        binding = NativeBinding(
            team_id=self.team_id,
            attempt_id=task.attempt.attempt_id,
            epoch=self.fence.epoch,
            thread_id=str(started["thread_id"]),
        )
        self.store.bind_native(
            self.user_id, self.brain_session_id, self.team_id, self.fence, binding
        )
        identity, thread_id = self.mailbox.identity(task.attempt.attempt_id)
        request = self.runtime.request(session_id)
        await request(
            "bgr/thread/bind",
            {
                "threadId": thread_id,
                "identity": identity.wire(),
                "peers": [self.brain.identity.wire()],
            },
        )
        connection = NativeBgrConnection(thread_id, identity, request, self.brain.capabilities)
        await connection.inspect()
        self._register(connection)
        await self.brain.authorize_peers(
            [
                connection.identity
                for connection in self.connections.values()
                if connection.identity.role != "brain"
            ]
        )
        await self.mailbox.forward(self.brain, connection, task.task_id)
        return {"task": task.model_dump(mode="json"), "native_acceptance_is_consumption": False}

    async def _start_review(self, command: BgrControl) -> dict[str, JsonValue]:
        assert command.repository is not None and command.pr_number is not None
        assert command.base_sha is not None and command.head_sha is not None
        round = self.store.reserve_review_round(
            self.user_id,
            self.brain_session_id,
            self.team_id,
            ReviewRoundCreate(
                **self.fence.model_dump(),
                round_id=command.action_id,
                repository=command.repository,
                pr_number=command.pr_number,
                base_sha=command.base_sha,
                head_sha=command.head_sha,
            ),
        )
        for job in round.jobs:
            try:
                prepared = await self.runtime.call(
                    "spawn",
                    {
                        "session_id": job.session_id,
                        "role": "reviewer",
                        "epoch": self.fence.epoch,
                        "profile": job.profile.model_dump(mode="json"),
                    },
                )
                started = await self.reviewer.start(
                    round_id=round.round_id,
                    review_id=job.review_id,
                    repository_path=str(prepared["repository_path"]),
                    host_id=str(prepared["host_id"]),
                    options=NativeReviewOptions.model_validate(prepared["options"]),
                    request=self.runtime.request(job.session_id),
                    stop=lambda session_id=job.session_id: self.runtime.stop(session_id),
                )
                if started is not None:
                    await self.create_role_session(
                        self.user_id,
                        self.brain_session_id,
                        job.session_id,
                        "reviewer",
                        Workspace(
                            host_id=str(prepared["host_id"]),
                            repository=round.repository,
                            worktree=started.snapshot_root,
                            base_sha=round.base_sha,
                        ),
                    )
            except (OmnigentError, OSError, ValueError, RuntimeError, TimeoutError):
                if not await self.runtime.stop(job.session_id):
                    raise OmnigentError(
                        "Reviewer stop is unconfirmed", code=ErrorCode.CONFLICT
                    ) from None
                self.store.settle_incomplete_review(
                    self.user_id,
                    self.brain_session_id,
                    self.team_id,
                    self.fence,
                    round_id=round.round_id,
                    review_id=job.review_id,
                    reason="startup_failed",
                    stop_confirmed=True,
                )
        return self.store.review_status(
            self.user_id, self.brain_session_id, self.team_id, round.round_id
        )

    async def native_callback(
        self, *, session_id: str, thread_id: str, method: str, params: object
    ) -> None:
        async with self._callbacks_lock:
            connection = self.connections.get(session_id)
            if connection is None or connection.thread_id != thread_id:
                raise OmnigentError("Unknown native callback source", code=ErrorCode.FORBIDDEN)
            self.authorize(self.user_id, self.brain_session_id)
            if method != "bgr/message/emitted":
                return
            receipt = self.mailbox.receive(connection, params)
            envelope = self.store.native_message(
                self.user_id,
                self.brain_session_id,
                self.team_id,
                self.fence,
                sender_agent_id=connection.identity.agent_id,
                message_id=receipt.message_id,
            )
            target = next(
                (
                    peer
                    for peer in self.connections.values()
                    if peer.identity.agent_id == envelope.recipient_agent_id
                ),
                None,
            )
            if target is not None:
                await self.mailbox.forward(connection, target, envelope.message_id)
            if connection.identity.role == "reviewer" and envelope.kind == "result":
                for peer in self.connections.values():
                    if peer.identity.role == "reviewer":
                        for pending in await self.mailbox.reconcile(peer):
                            await self.mailbox.forward(peer, self.brain, pending.message_id)


class BgrControllers:
    def __init__(self, store: BgrReviewStore, create_role_session: CreateRoleSession):
        self.store = store
        self.create_role_session = create_role_session
        self._controllers: dict[str, BgrController] = {}
        self._attach_lock = asyncio.Lock()

    def get(self, brain_session_id: str) -> BgrController:
        controller = self._controllers.get(brain_session_id)
        if controller is None:
            raise OmnigentError("BGR runtime is not attached", code=ErrorCode.CONFLICT)
        return controller

    async def attach(
        self,
        *,
        user_id: str,
        brain_session_id: str,
        team_id: str,
        fence: Fence,
        runtime: BgrHostRuntime,
    ) -> BgrController:
        async with self._attach_lock:
            old = self._controllers.get(brain_session_id)
            if old is not None:
                old.authorize(user_id, brain_session_id)
                if old.team_id != team_id or old.fence != fence:
                    raise OmnigentError("Controller fence conflict", code=ErrorCode.CONFLICT)
                return old
            team = self.store.get_team(user_id, brain_session_id, team_id)
            metadata = await runtime.call("brain", {})
            thread_id = str(metadata["thread_id"])
            request = runtime.request(brain_session_id)
            capabilities = NativeCapabilities.model_validate(
                (await request("bgr/capabilities/read", {})).get("result")
            )
            if not (capabilities.general_messaging and capabilities.host_record):
                raise OmnigentError("Native BGR capabilities unavailable", code=ErrorCode.CONFLICT)
            self.store.native_brain_binding(
                user_id,
                brain_session_id,
                team_id,
                fence,
                binding=NativeBrainBinding(
                    team_id=team_id, epoch=fence.epoch, thread_id=thread_id
                ),
            )
            identity = NativeIdentity.model_validate(
                {
                    "teamId": team_id,
                    "agentId": team.brain_agent_id,
                    "sessionId": brain_session_id,
                    "epoch": fence.epoch,
                    "role": "brain",
                }
            )
            await request("bgr/thread/bind", {"threadId": thread_id, "identity": identity.wire()})
            brain = NativeBgrConnection(thread_id, identity, request, capabilities)
            await brain.inspect()
            controller = BgrController(
                store=self.store,
                user_id=user_id,
                brain_session_id=brain_session_id,
                team_id=team_id,
                fence=fence,
                brain=brain,
                runtime=runtime,
                create_role_session=self.create_role_session,
            )
            self._controllers[brain_session_id] = controller
            controller._heartbeat = asyncio.create_task(
                controller.maintain_lease(), name=f"bgr-controller-lease-{team_id}"
            )
            return controller

    def remove(self, brain_session_id: str, fence: Fence) -> None:
        controller = self.get(brain_session_id)
        if controller.fence != fence:
            raise OmnigentError("Controller fence conflict", code=ErrorCode.CONFLICT)
        del self._controllers[brain_session_id]
        if controller._heartbeat is not None:
            controller._heartbeat.cancel()

    async def close(self) -> None:
        tasks = []
        for controller in self._controllers.values():
            if controller._heartbeat is not None:
                controller._heartbeat.cancel()
                tasks.append(controller._heartbeat)
        await asyncio.gather(*tasks, return_exceptions=True)


def create_bgr_controllers(
    ledger: BgrStore,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
) -> BgrControllers:
    async def create_role_session(
        user: str,
        brain_session_id: str,
        session_id: str,
        role: str,
        workspace: Workspace,
    ) -> None:
        brain = conversations.get_conversation(brain_session_id)
        if brain is None:
            raise OmnigentError("Brain session disappeared", code=ErrorCode.CONFLICT)
        conversations.create_conversation(
            conversation_id=session_id,
            kind="sub_agent",
            title=f"{role}-{session_id}",
            parent_conversation_id=brain_session_id,
            agent_id=brain.agent_id,
            runner_id=brain.runner_id,
            sub_agent_name=f"{role}-{session_id}",
            host_id=workspace.host_id,
            workspace=workspace.worktree,
            harness_override="codex-native",
            labels={"omnigent.bgr.role": role},
        )
        if permissions is not None:
            permissions.grant(user, session_id, 3)

    return BgrControllers(BgrReviewStore.for_ledger(ledger), create_role_session)
