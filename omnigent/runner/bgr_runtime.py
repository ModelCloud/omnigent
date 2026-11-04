"""Trusted host process ownership and RPC transport; SQL and scheduling live on the server."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter
from pydantic import JsonValue, TypeAdapter

from omnigent.bgr import Identifier, Profile, Record, TeamConfig, Workspace
from omnigent.bgr_review import sanitize_review_payload
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexMessage,
    CodexNativeAppServer,
    NativeCodexLaunch,
    _model_provider_override,
    build_codex_native_server,
    client_for_transport,
    resolve_native_codex_launch,
)
from omnigent.harnesses.codex_native.bgr import NativeCapabilities, bgr_thread_config
from omnigent.harnesses.multidex_native.config import (
    load_multidex_config,
    multidex_model_for_selection,
    multidex_provider_config_overrides,
    multidex_registration_bearer_token,
    multidex_runtime_provider_id,
)
from omnigent.host.git_worktree import create_worktree
from omnigent.host.provider_health import annotate_provider_speed
from omnigent.runner import bgr_publisher
from omnigent.spec.types import AgentSpec, ExecutorSpec, ProviderAuth

_JSON = TypeAdapter(dict[str, JsonValue])
Callback = Callable[[dict[str, JsonValue]], Awaitable[None]]
_RUNTIMES: dict[str, BgrRuntime] = {}
_RPC_METHODS = frozenset(
    {
        "bgr/capabilities/read",
        "bgr/thread/bind",
        "bgr/thread/peers",
        "bgr/thread/inspect",
        "bgr/message/record",
        "bgr/message/deliver",
        "bgr/reviewer/start",
        "thread/read",
    }
)


def bgr_brain_registered(session_id: str) -> bool:
    return session_id in _RUNTIMES


def bgr_native_config_overrides() -> tuple[str, ...]:
    return tuple(f"{key}={json.dumps(value)}" for key, value in bgr_thread_config().items())


def resolve_bgr_profile(profile: Profile, spec: AgentSpec | None) -> NativeCodexLaunch:
    if profile.harness != "codex-native":
        raise ValueError("BGR requires the native harness")
    try:
        config = load_multidex_config(require_token=False)
    except FileNotFoundError:
        config = None
    registration = multidex_model_for_selection(config, profile.model) if config else None
    variant = registration.variant if registration else "regular"
    if profile.variant not in {None, variant}:
        raise ValueError("BGR variant does not match the registered endpoint")
    row: dict[str, object] = {"providerId": profile.provider, "variant": variant}
    annotate_provider_speed([row])
    if profile.speed is not None and float(profile.speed) != row["speed"]:
        raise ValueError("BGR speed does not match the registered endpoint")
    if registration is not None:
        if registration.provider != profile.provider:
            raise ValueError("BGR provider does not match the registered endpoint")
        if not multidex_registration_bearer_token(registration):
            raise ValueError("BGR provider needs a configured credential")
        assert config is not None
        provider = multidex_runtime_provider_id(registration.provider)
        return NativeCodexLaunch(
            model=registration.model,
            profile=None,
            summary="BGR registered MultiDex endpoint",
            config_overrides=[
                *multidex_provider_config_overrides(config),
                f"model_provider={json.dumps(provider)}",
                f"model={json.dumps(registration.model)}",
            ],
        )
    role_spec = dataclasses.replace(
        spec or AgentSpec(spec_version=1),
        executor=ExecutorSpec(auth=ProviderAuth(name=profile.provider)),
    )
    return resolve_native_codex_launch(model=profile.model, spec=role_spec)


async def bgr_thread_start_options(client: CodexAppServerClient) -> CodexMessage:
    response = await client.request("bgr/capabilities/read", {})
    capabilities = NativeCapabilities.model_validate(_JSON.validate_python(response["result"]))
    tools = [capabilities.report_tool, capabilities.send_tool, capabilities.reply_tool]
    if any(tool is None for tool in tools):
        raise OmnigentError("Native message tools are unavailable", code=ErrorCode.CONFLICT)
    return {"config": bgr_thread_config(), "dynamicTools": tools}


async def unregister_bgr_brain(
    session_id: str, *, app_server: CodexNativeAppServer | None = None
) -> bool:
    """Only a verified stopped Brain owner may release its registry and child processes."""
    runtime = _RUNTIMES.get(session_id)
    if runtime is None:
        return True
    if runtime.brain.server is not app_server:
        return False
    process = runtime.brain.server.proc
    if process is not None and process.returncode is None:
        return False
    async with runtime._lock:
        for role in tuple(runtime.roles.values()):
            if role is not runtime.brain and not role.stopped:
                result = await runtime.operation("stop", {"session_id": role.session_id})
                if result.get("stop_confirmed") is not True:
                    return False
        for role in runtime.roles.values():
            for remove in role.remove_observers:
                remove()
            await role.client.close()
        runtime.roles.clear()
        if _RUNTIMES.get(session_id) is runtime:
            del _RUNTIMES[session_id]
    return True


@dataclass
class _RoleProcess:
    session_id: str
    role: str
    epoch: int
    client: CodexAppServerClient
    server: CodexNativeAppServer
    thread_id: str | None = None
    stopped: bool = False
    remove_observers: list[Callable[[], None]] = field(default_factory=list)


class BgrRuntime:
    def __init__(
        self,
        brain: _RoleProcess,
        *,
        workspace: Path,
        profiles: TeamConfig,
        callback: Callback,
        host_id: str,
        spec: AgentSpec | None,
        server_client: httpx.AsyncClient | None = None,
        team_id: str | None = None,
    ):
        self.brain = brain
        self.workspace = workspace.resolve(strict=True)
        self.profiles = profiles
        self.callback = callback
        self.host_id = host_id
        self.spec = spec
        self.server_client = server_client
        self.team_id = team_id
        self.roles = {brain.session_id: brain}
        self.ready = asyncio.Event()
        self._lock = asyncio.Lock()
        self._workspaces: dict[str, Workspace] = {}

    def _private_values(self) -> tuple[str, ...]:
        return bgr_publisher._private_values(str(self.workspace), require_forge=False)

    def observe(self, role: _RoleProcess) -> None:
        async def emitted(message: CodexMessage) -> None:
            params = _JSON.validate_python(message.get("params"))
            thread_id = params.get("threadId")
            if role.thread_id is None or thread_id != role.thread_id:
                raise OmnigentError("Foreign native callback thread", code=ErrorCode.FORBIDDEN)
            cleaned = sanitize_review_payload(params, private_values=self._private_values())
            assert isinstance(cleaned, dict)
            await self.callback(
                {
                    "session_id": role.session_id,
                    "thread_id": role.thread_id,
                    "method": "bgr/message/emitted",
                    "params": cleaned,
                }
            )

        role.remove_observers.append(role.client.add_notification_observer("bgr/message", emitted))

    async def thread_ready(self, thread_id: str) -> None:
        if self.brain.thread_id not in {None, thread_id}:
            raise OmnigentError("Brain thread changed", code=ErrorCode.CONFLICT)
        self.brain.thread_id = thread_id
        await self.callback(
            {
                "session_id": self.brain.session_id,
                "thread_id": thread_id,
                "method": "runtime/thread-ready",
                "params": {"thread_id": thread_id},
            }
        )
        self.ready.set()

    async def operation(
        self, operation: str, parameters: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        if operation == "brain":
            if self.brain.thread_id is None:
                raise OmnigentError("Brain thread not discovered", code=ErrorCode.CONFLICT)
            return {"thread_id": self.brain.thread_id}
        if operation == "workspace":
            return await self._workspace(parameters)
        if operation == "spawn":
            async with self._lock:
                return await self._spawn(parameters)
        session_id = parameters.get("session_id")
        role = self.roles.get(session_id) if isinstance(session_id, str) else None
        if role is None:
            raise OmnigentError("Unknown native role process", code=ErrorCode.FORBIDDEN)
        if operation == "stop":
            for remove in role.remove_observers:
                remove()
            if role is not self.brain:
                await role.client.close()
            await role.server.close()
            role.stopped = role.server.proc is None
            return {"stop_confirmed": role.stopped}
        if operation != "rpc" or role.stopped:
            raise OmnigentError(
                "Unsupported native runtime operation", code=ErrorCode.INVALID_INPUT
            )
        method = parameters.get("method")
        params = _JSON.validate_python(parameters.get("params"))
        if method not in _RPC_METHODS:
            raise OmnigentError("Native RPC method is not allowed", code=ErrorCode.FORBIDDEN)
        if method in {
            "thread/read",
            "bgr/thread/inspect",
            "bgr/message/record",
            "bgr/message/deliver",
            "bgr/thread/peers",
            "bgr/thread/bind",
        }:
            if role.thread_id is None or params.get("threadId") != role.thread_id:
                raise OmnigentError(
                    "Native RPC targets a foreign thread", code=ErrorCode.FORBIDDEN
                )
        if method == "bgr/reviewer/start" and (
            role.role != "reviewer" or role.thread_id is not None
        ):
            raise OmnigentError("Reviewer start already claimed", code=ErrorCode.CONFLICT)
        if method == "thread/read" and params.get("includeTurns") is not False:
            raise OmnigentError(
                "Only live snapshot metadata may be inspected", code=ErrorCode.FORBIDDEN
            )
        response = await role.client.request(str(method), dict(params))
        if method == "bgr/reviewer/start":
            result = _JSON.validate_python(response.get("result"))
            role.thread_id = str(result["threadId"])
        cleaned = sanitize_review_payload(
            _JSON.validate_python(response), private_values=self._private_values()
        )
        assert isinstance(cleaned, dict)
        return cleaned

    async def _workspace(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        task_id = TypeAdapter(Identifier).validate_python(parameters.get("task_id"))
        base_sha = parameters.get("base_sha")
        if not isinstance(base_sha, str) or len(base_sha) != 40:
            raise OmnigentError("Invalid workspace base", code=ErrorCode.INVALID_INPUT)
        previous = self._workspaces.get(task_id)
        if previous is not None:
            if previous.base_sha != base_sha:
                raise OmnigentError("Workspace task anchor changed", code=ErrorCode.CONFLICT)
            return {"workspace": previous.model_dump(mode="json")}
        result = await asyncio.to_thread(
            create_worktree,
            repo_path=str(self.workspace),
            branch_name=f"bgr-grunt-{task_id}",
            base_branch=base_sha,
        )
        workspace = Workspace(
            host_id=self.host_id,
            repository=str(self.workspace),
            worktree=result.worktree_path,
            base_sha=base_sha,
        )
        self._workspaces[task_id] = workspace
        return {"workspace": workspace.model_dump(mode="json")}

    async def _spawn(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        session_id = TypeAdapter(Identifier).validate_python(parameters.get("session_id"))
        role = parameters.get("role")
        profile = Profile.model_validate(parameters.get("profile"))
        epoch = parameters.get("epoch")
        allowed = [self.profiles.grunt] if role == "grunt" else self.profiles.reviewers
        if role not in {"grunt", "reviewer"} or profile not in allowed or type(epoch) is not int:
            raise OmnigentError(
                "Role/profile does not match trusted team", code=ErrorCode.FORBIDDEN
            )
        if session_id in self.roles:
            raise OmnigentError("Native role process already exists", code=ErrorCode.CONFLICT)
        workspace = self.workspace
        if role == "grunt":
            assigned = Workspace.model_validate(parameters.get("workspace"))
            if assigned not in self._workspaces.values():
                raise OmnigentError("Worker workspace is not assigned", code=ErrorCode.FORBIDDEN)
            workspace = Path(assigned.worktree)
        launch = await asyncio.to_thread(resolve_bgr_profile, profile, self.spec)
        if launch.login_required:
            raise OmnigentError("Role provider requires login", code=ErrorCode.FORBIDDEN)
        directory = self.brain.server.codex_home.parent / f"bgr-{session_id}"
        directory.mkdir(parents=True, exist_ok=True)
        server = build_codex_native_server(
            socket_path=directory / "app-server.sock",
            codex_home=directory / "home",
            cwd=workspace,
            model=launch.model,
            profile=launch.profile,
            config_profile=launch.config_profile,
            bridge_dir=directory / "bridge",
            session_id=session_id,
            codex_path=self.brain.server.codex_path,
            extra_config_overrides=[*launch.config_overrides, *bgr_native_config_overrides()],
            reasoning_effort=profile.reasoning,
            reconcile_process_registry=False,
        )
        client: CodexAppServerClient | None = None
        try:
            await server.start()
            assert server.listen_url is not None
            client = client_for_transport(server.listen_url, client_name="omnigent-bgr-worker")
            await client.connect()
            owned = _RoleProcess(session_id, str(role), epoch, client, server)
            self.roles[session_id] = owned
            self.observe(owned)
            options: dict[str, JsonValue] = {
                "model": profile.model,
                "modelProvider": _model_provider_override(server.config_overrides) or "openai",
            }
            if profile.reasoning is not None:
                options["reasoningEffort"] = profile.reasoning
            if role == "grunt":
                startup_options = await bgr_thread_start_options(client)
                started = await client.request(
                    "thread/start",
                    {
                        "cwd": str(workspace),
                        "model": profile.model,
                        "approvalPolicy": "never",
                        "sandbox": "workspace-write",
                        **startup_options,
                    },
                )
                thread = _JSON.validate_python(
                    _JSON.validate_python(started.get("result"))["thread"]
                )
                owned.thread_id = str(thread["id"])
            return {
                "thread_id": owned.thread_id,
                "options": options,
                "repository_path": str(self.workspace),
                "host_id": self.host_id,
            }
        except BaseException:
            if client is not None:
                await client.close()
            await server.close()
            raise


async def register_bgr_brain(
    session_id: str,
    client: CodexAppServerClient,
    app_server: CodexNativeAppServer,
    workspace: Path,
    profiles: TeamConfig,
    callback: Callback,
    *,
    host_id: str,
    thread_id: str | None = None,
    spec: AgentSpec | None = None,
    server_client: httpx.AsyncClient | None = None,
    team_id: str | None = None,
) -> dict[str, JsonValue]:
    existing = _RUNTIMES.get(session_id)
    if existing is not None:
        if existing.brain.client is not client or existing.profiles != profiles:
            raise OmnigentError("Brain runtime registration conflict", code=ErrorCode.CONFLICT)
        return {"thread_id": existing.brain.thread_id, "boot_params": {}}
    brain = _RoleProcess(session_id, "brain", 0, client, app_server, thread_id)
    runtime = BgrRuntime(
        brain,
        workspace=workspace,
        profiles=profiles,
        callback=callback,
        host_id=host_id,
        spec=spec,
        server_client=server_client,
        team_id=team_id,
    )
    runtime.observe(brain)
    _RUNTIMES[session_id] = runtime
    if thread_id is not None:
        await runtime.thread_ready(thread_id)
    return {"thread_id": thread_id, "boot_params": {}}


async def wait_bgr_brain_ready(session_id: str, thread_id: str) -> None:
    runtime = _RUNTIMES.get(session_id)
    if runtime is None:
        return
    if not runtime.ready.is_set():
        await runtime.thread_ready(thread_id)
    if runtime.brain.thread_id != thread_id:
        raise OmnigentError("Brain thread identity changed", code=ErrorCode.CONFLICT)
    await runtime.ready.wait()


async def probe_bgr_runtime() -> dict[str, JsonValue]:
    """Probe only metadata in an empty temporary home; never start a model turn."""
    server: CodexNativeAppServer | None = None
    client: CodexAppServerClient | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="omnigent-bgr-probe-") as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            server = build_codex_native_server(
                socket_path=root / "app-server.sock",
                codex_home=root / "home",
                cwd=root,
                model=None,
                profile=None,
                bridge_dir=root / "bridge",
                config_source=source,
                reconcile_process_registry=False,
            )
            await server.start()
            assert server.listen_url is not None
            client = client_for_transport(server.listen_url, client_name="omnigent-bgr-probe")
            await client.connect()
            native = _JSON.validate_python(
                (await client.request("bgr/capabilities/read", {}))["result"]
            )
            capabilities = NativeCapabilities.model_validate(native)
            supported = (
                capabilities.general_messaging
                and capabilities.host_record
                and capabilities.reviewer_start
                and capabilities.reviewer_sandbox
                and capabilities.reviewer_snapshot_no_lazy_fetch
            )
            return {
                "launch_supported": supported,
                "supported_harnesses": ["codex-native"] if supported else [],
                "reviewer_isolation": supported,
                "native_capabilities": native,
                "reason": None if supported else "Native hardened reviewer factory unavailable",
            }
    except (ImportError, OSError, RuntimeError, ValueError, TimeoutError):
        return {
            "launch_supported": False,
            "supported_harnesses": [],
            "reviewer_isolation": False,
            "reason": "Native BGR metadata probe failed; install a hardened supported package",
        }
    finally:
        if client is not None:
            await client.close()
        if server is not None:
            await server.close()


class RuntimeCall(Record):
    operation: Literal[
        "brain", "workspace", "spawn", "rpc", "stop", "publication_preview", "publish"
    ]
    parameters: dict[str, JsonValue]


def create_bgr_runtime_router() -> APIRouter:
    router = APIRouter()

    @router.post("/v1/sessions/{brain_session_id}/bgr/runtime")
    async def runtime_call(
        brain_session_id: Identifier, body: RuntimeCall
    ) -> dict[str, JsonValue]:
        runtime = _RUNTIMES.get(brain_session_id)
        if runtime is None:
            raise OmnigentError("BGR Brain is not registered", code=ErrorCode.FORBIDDEN)
        return await runtime.operation(body.operation, body.parameters)

    return router
