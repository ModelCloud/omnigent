"""Host ownership and readiness gates; no SQL or fake provider-execution claims."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from omnigent.bgr import Profile, Team, Workspace
from omnigent.errors import OmnigentError
from omnigent.harnesses.codex_native.app_server import CodexAppServerClient
from omnigent.harnesses.multidex_native.config import MultiDexConfig, MultiDexModelRegistration
from omnigent.runner import bgr_runtime as runtime
from omnigent.runner.native import orchestration
from omnigent.spec.types import AgentSpec
from tests.harnesses.codex_native.test_bgr_messages import CAPTURED
from tests.stores.test_bgr_store import BRAIN, CONFIG, CONTROLLER, HOST, OWNER, TEAM


@pytest.mark.parametrize("foreign", ["team_id", "brain_session_id", None])
async def test_team_config_fetch_uses_authenticated_durable_team_and_checks_identity(foreign):
    from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER

    team = Team(
        team_id=TEAM,
        owner_id=OWNER,
        brain_agent_id=uuid4().hex,
        brain_session_id=BRAIN,
        config=CONFIG,
        epoch=1,
        controller_id=CONTROLLER,
        lease_expires_at=0,
    )
    if foreign is not None:
        team = team.model_copy(update={foreign: uuid4().hex})

    def handler(request):
        assert request.url.path == f"/v1/bgr/teams/{TEAM}"
        assert request.url.params["limit"] == "1"
        assert request.headers[ORCHESTRATION_SOURCE_HEADER] == BRAIN
        return httpx.Response(200, json={"team": team.model_dump(mode="json")})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        if foreign:
            with pytest.raises(ValueError, match="another team or Brain"):
                await runtime.load_bgr_team_config(
                    server_client=client, session_id=BRAIN, team_id=TEAM
                )
        else:
            assert (
                await runtime.load_bgr_team_config(
                    server_client=client, session_id=BRAIN, team_id=TEAM
                )
                == CONFIG
            )


@pytest.mark.parametrize("hardened", [False, True])
@pytest.mark.parametrize("connect_failed", [False, True])
async def test_probe_uses_explicit_loopback_transport_and_closes_resources(
    monkeypatch, hardened, connect_failed
):
    server = SimpleNamespace(listen_url=None, close=AsyncMock())
    client = SimpleNamespace(
        connect=AsyncMock(side_effect=RuntimeError("offline") if connect_failed else None),
        close=AsyncMock(),
        request=AsyncMock(
            return_value={
                "result": {**CAPTURED, "reviewerSnapshotNoLazyFetch": hardened},
            }
        ),
    )

    async def start():
        assert server.listen_url == "ws://127.0.0.1:18084"

    server.start = start
    factory = Mock(return_value=client)
    monkeypatch.setattr(runtime, "build_codex_native_server", Mock(return_value=server))
    monkeypatch.setattr(runtime, "_allocate_loopback_port", lambda: 18084)
    monkeypatch.setattr(runtime, "client_for_transport", factory)
    result = await runtime.probe_bgr_runtime()
    assert result["launch_supported"] is (hardened and not connect_failed)
    factory.assert_called_once_with(server.listen_url, client_name="omnigent-bgr-probe")
    if connect_failed:
        client.request.assert_not_awaited()
    else:
        client.request.assert_awaited_once_with("bgr/capabilities/read", {})
    client.close.assert_awaited_once()
    server.close.assert_awaited_once()


@pytest.mark.parametrize("role_name", ["grunt", "reviewer"])
async def test_role_spawn_uses_explicit_loopback_transport(monkeypatch, tmp_path, role_name):
    profile = CONFIG.grunt if role_name == "grunt" else CONFIG.reviewers[0]
    thread_id = str(uuid4())
    server = SimpleNamespace(listen_url=None, close=AsyncMock(), config_overrides=[])
    client = SimpleNamespace(
        connect=AsyncMock(),
        close=AsyncMock(),
        add_notification_observer=Mock(return_value=Mock()),
        request=AsyncMock(
            side_effect=[{"result": CAPTURED}, {"result": {"thread": {"id": thread_id}}}]
        ),
    )

    async def start():
        assert server.listen_url == "ws://127.0.0.1:18084"

    server.start = start
    monkeypatch.setattr(runtime, "build_codex_native_server", Mock(return_value=server))
    monkeypatch.setattr(runtime, "_allocate_loopback_port", lambda: 18084)
    factory = Mock(return_value=client)
    monkeypatch.setattr(runtime, "client_for_transport", factory)
    monkeypatch.setattr(
        runtime,
        "resolve_bgr_profile",
        lambda *_: runtime.NativeCodexLaunch(
            model=profile.model, profile=None, summary="fixture", config_overrides=[]
        ),
    )
    brain = runtime._RoleProcess(
        BRAIN,
        "brain",
        1,
        SimpleNamespace(),
        SimpleNamespace(codex_home=tmp_path / "home", codex_path="fixture"),
    )
    owner = runtime.BgrRuntime(
        brain, workspace=tmp_path, profiles=CONFIG, callback=AsyncMock(), host_id=HOST, spec=None
    )
    assigned = Workspace(
        host_id=HOST, repository=str(tmp_path), worktree=str(tmp_path), base_sha="a" * 40
    )
    owner._workspaces[uuid4().hex] = assigned
    session_id = uuid4().hex
    result = await owner._spawn(
        {
            "session_id": session_id,
            "role": role_name,
            "profile": profile.model_dump(mode="json"),
            "epoch": 1,
            "workspace": assigned.model_dump(mode="json"),
        }
    )
    factory.assert_called_once_with(server.listen_url, client_name="omnigent-bgr-worker")
    assert result["thread_id"] == (thread_id if role_name == "grunt" else None)
    assert owner.roles[session_id].server is server
    assert all(call.args[0] != "turn/start" for call in client.request.await_args_list)


async def test_native_wire_callback_is_observed_and_relayed_after_redaction(tmp_path, monkeypatch):
    observer = Mock(return_value=Mock())
    client = SimpleNamespace(add_notification_observer=observer)
    callback = AsyncMock()
    role = runtime._RoleProcess(BRAIN, "brain", 1, client, SimpleNamespace(), str(uuid4()))
    owner = runtime.BgrRuntime(
        role, workspace=tmp_path, profiles=CONFIG, callback=callback, host_id=HOST, spec=None
    )
    monkeypatch.setattr(owner, "_private_values", lambda: ("fixture-private-value",))
    owner.observe(role)
    method, handler = observer.call_args.args
    assert method == "bgr/message"
    await handler(
        {
            "method": method,
            "params": {
                "threadId": role.thread_id,
                "envelope": {"payload": {"text": "fixture-private-value"}},
            },
        }
    )
    relayed = callback.call_args.args[0]
    assert relayed["method"] == "bgr/message/emitted"
    assert "fixture-private-value" not in str(relayed)
    with pytest.raises(OmnigentError, match="Foreign"):
        await handler({"params": {"threadId": str(uuid4())}})


@pytest.mark.parametrize("operation", ["publication_preview", "publish", "publication_reconcile"])
async def test_publication_uses_brain_workspace_and_authenticated_server_only(
    tmp_path, monkeypatch, operation
):
    from omnigent.runner import bgr_publications

    publication = AsyncMock(return_value={"publication": {"state": "published"}})
    monkeypatch.setattr(bgr_publications, "run_publication", publication)
    server_client = SimpleNamespace()
    team_id = uuid4().hex
    role = runtime._RoleProcess(BRAIN, "brain", 1, SimpleNamespace(), SimpleNamespace())
    owner = runtime.BgrRuntime(
        role,
        workspace=tmp_path,
        profiles=CONFIG,
        callback=AsyncMock(),
        host_id=HOST,
        spec=None,
        server_client=server_client,
        team_id=team_id,
    )
    parameters = {"team_id": team_id}
    await owner.operation(operation, parameters)
    publication.assert_awaited_once_with(
        operation,
        parameters,
        client=server_client,
        session_id=BRAIN,
        team_id=team_id,
        root=str(tmp_path),
    )
    owner.server_client = None
    with pytest.raises(OmnigentError, match="unavailable"):
        await owner.operation(operation, parameters)


async def test_brain_ready_waits_for_server_bind_and_non_bgr_is_noop(tmp_path):
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:9")
    native = SimpleNamespace(proc=None, close=AsyncMock())
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def callback(payload):
        calls.append(payload)
        assert not runtime._RUNTIMES[BRAIN].ready.is_set()
        started.set()
        await release.wait()

    try:
        result = await runtime.register_bgr_brain(
            BRAIN, client, native, tmp_path, CONFIG, callback, host_id=HOST
        )
        assert result == {"thread_id": None, "boot_params": {}}
        await runtime.wait_bgr_brain_ready(uuid4().hex, str(uuid4()))
        thread_id = str(uuid4())
        task = asyncio.create_task(runtime.wait_bgr_brain_ready(BRAIN, thread_id))
        await started.wait()
        assert not task.done()
        assert runtime.bgr_brain_registered(BRAIN)
        release.set()
        await task
        assert runtime._RUNTIMES[BRAIN].ready.is_set()
        assert calls[0]["method"] == "runtime/thread-ready"
        with pytest.raises(OmnigentError, match="identity changed"):
            await runtime.wait_bgr_brain_ready(BRAIN, str(uuid4()))
        with pytest.raises(OmnigentError, match="registration conflict"):
            await runtime.register_bgr_brain(
                BRAIN,
                CodexAppServerClient(ws_url="ws://127.0.0.1:9"),
                native,
                tmp_path,
                CONFIG,
                callback,
                host_id=HOST,
            )
        for method in ("turn/start", "command/exec"):
            with pytest.raises(OmnigentError, match="not allowed"):
                await runtime._RUNTIMES[BRAIN].operation(
                    "rpc",
                    {
                        "session_id": BRAIN,
                        "method": method,
                        "params": {},
                    },
                )
        with pytest.raises(OmnigentError, match="foreign thread"):
            await runtime._RUNTIMES[BRAIN].operation(
                "rpc",
                {
                    "session_id": BRAIN,
                    "method": "thread/read",
                    "params": {"threadId": str(uuid4()), "includeTurns": False},
                },
            )
        with pytest.raises(OmnigentError, match="Unknown native"):
            await runtime._RUNTIMES[BRAIN].operation("stop", {"session_id": uuid4().hex})
    finally:
        runtime._RUNTIMES.pop(BRAIN, None)
        await client.close()


@pytest.mark.parametrize(
    "provider,speed,runtime_provider",
    [
        ("lithos", "1.25", "lithos"),
        ("fixture", "1.0", "omnigent-provider-fixture-f16d05ec6b"),
        ("multidex", "1.0", "omnigent-provider-multidex-5ed753f826"),
    ],
)
@pytest.mark.parametrize("variant", ["regular", None])
def test_registered_profile_preserves_provider_endpoint(
    monkeypatch, variant, provider, speed, runtime_provider
):
    registration = MultiDexModelRegistration(
        model="registered",
        provider=provider,
        display_name="Registered",
        base_url="http://127.0.0.1:18083/v1",
        provider_config={"experimental_bearer_token": "fixture-not-a-secret"},
    )
    config = MultiDexConfig(
        local_model=registration.model,
        provider=registration.provider,
        base_url=registration.base_url,
        models=(registration,),
    )
    monkeypatch.setattr(runtime, "load_multidex_config", lambda **kwargs: config)
    profile = Profile(
        harness="codex-native",
        model="registered",
        provider=provider,
        variant=variant,
        speed=speed,
    )
    launch = runtime.resolve_bgr_profile(profile, None)
    assert launch.model == "registered"
    assert f'model_provider="{runtime_provider}"' in launch.config_overrides
    assert any(registration.base_url in item for item in launch.config_overrides)
    for change in ({"variant": "ultra"}, {"speed": "100"}, {"provider": "openrouter"}):
        with pytest.raises(ValueError, match="does not match"):
            runtime.resolve_bgr_profile(profile.model_copy(update=change), None)


async def test_startup_options_advertise_exact_native_tools_and_disable_scheduler():
    client = SimpleNamespace(request=AsyncMock(return_value={"result": CAPTURED}))
    options = await runtime.bgr_thread_start_options(client)
    assert options["dynamicTools"] == [
        CAPTURED[key] for key in ("reportTool", "sendTool", "replyTool")
    ]
    assert options["config"] == {
        "features.multi_agent": False,
        "features.multi_agent_v2": False,
        "features.defer_mailbox_preemption": True,
        "features.mcp_tool_list_changed": True,
    }
    assert runtime.bgr_native_config_overrides() == (
        "features.multi_agent=false",
        "features.multi_agent_v2=false",
        "features.defer_mailbox_preemption=true",
        "features.mcp_tool_list_changed=true",
    )


async def test_unregister_requires_stopped_exact_owner_and_joins_children(tmp_path):
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:9")
    native = SimpleNamespace(proc=SimpleNamespace(returncode=None))
    worker_client = CodexAppServerClient(ws_url="ws://127.0.0.1:9")
    worker = SimpleNamespace(proc=object(), close=AsyncMock())
    try:
        await runtime.register_bgr_brain(
            BRAIN, client, native, tmp_path, CONFIG, AsyncMock(), host_id=HOST
        )
        assert not await runtime.unregister_bgr_brain(BRAIN, app_server=native)
        assert not await runtime.unregister_bgr_brain(BRAIN, app_server=SimpleNamespace(proc=None))
        role = runtime._RoleProcess(uuid4().hex, "grunt", 1, worker_client, worker)
        runtime._RUNTIMES[BRAIN].roles[role.session_id] = role
        native.proc = None
        assert not await runtime.unregister_bgr_brain(BRAIN, app_server=native)
        assert runtime.bgr_brain_registered(BRAIN)
        worker.proc = None
        assert await runtime.unregister_bgr_brain(BRAIN, app_server=native)
        assert role.stopped
        assert not runtime.bgr_brain_registered(BRAIN)
        assert await runtime.unregister_bgr_brain(BRAIN, app_server=native)
    finally:
        runtime._RUNTIMES.pop(BRAIN, None)
        await client.close()
        await worker_client.close()


async def test_stop_generation_serializes_spawn_and_never_claims_domain_quiescence(tmp_path):
    stopped, release = asyncio.Event(), asyncio.Event()
    native = SimpleNamespace(proc=SimpleNamespace(returncode=None))

    async def close():
        stopped.set()
        await release.wait()
        native.proc = None

    native.close = close
    role = runtime._RoleProcess(BRAIN, "brain", 1, SimpleNamespace(), native, str(uuid4()))
    owner = runtime.BgrRuntime(
        role, workspace=tmp_path, profiles=CONFIG, callback=AsyncMock(), host_id=HOST, spec=None
    )
    with pytest.raises(OmnigentError, match="generation changed"):
        await owner.operation("stop_generation", {"runtime_id": uuid4().hex})
    stopping = asyncio.create_task(
        owner.operation("stop_generation", {"runtime_id": owner.runtime_id})
    )
    await stopped.wait()
    spawning = asyncio.create_task(owner.operation("spawn", {}))
    assert not spawning.done()
    release.set()
    result = await stopping
    assert native.proc is None and result["domain_stop_confirmed"] is False
    with pytest.raises(OmnigentError, match="fenced"):
        await spawning
    for operation in ("brain", "publication_preview", "publish", "publication_reconcile", "rpc"):
        with pytest.raises(OmnigentError, match="fenced"):
            await owner.operation(operation, {})
    with pytest.raises(OmnigentError, match="fenced"):
        await owner.thread_ready(role.thread_id)


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("failure", [None, "relay", "catalog", "bind", "materialize"])
async def test_brain_preparation_retains_tool_enabled_thread_without_a_turn(
    monkeypatch, tmp_path, fresh, failure
):
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:9")
    native = SimpleNamespace(proc=SimpleNamespace(returncode=None))
    thread_id = str(uuid4())
    calls = []

    async def ensure_relay(session_id, *, explicit_bridge_dir, await_notify):
        calls.append("relay")
        assert session_id == BRAIN
        assert explicit_bridge_dir == tmp_path
        assert await_notify is False
        assert runtime.bgr_brain_registered(BRAIN)
        assert not runtime._RUNTIMES[BRAIN].ready.is_set()
        assert runtime._RUNTIMES[BRAIN].brain.thread_id is None
        if failure != "relay":
            (tmp_path / "tool_relay.json").write_text(
                json.dumps({"tools": [{"name": "sys_bgr_control"}]})
            )

    async def request(method, params):
        calls.append(method)
        if method == "bgr/capabilities/read":
            return {"result": CAPTURED}
        if method == "thread/start":
            assert params["cwd"] == str(tmp_path)
            assert params["dynamicTools"] == [
                CAPTURED[key] for key in ("reportTool", "sendTool", "replyTool")
            ]
            assert params["config"]["features.multi_agent"] is False
            return {"result": {"thread": {"id": thread_id}}}
        if method == "mcpServerStatus/list":
            assert params == {
                "threadId": thread_id,
                "serverName": "omnigent",
                "detail": "toolsAndAuthOnly",
            }
            assert not runtime._RUNTIMES[BRAIN].ready.is_set()
            return {
                "result": {
                    "data": [
                        {
                            "name": "omnigent",
                            "runtimeStatus": "connected",
                            "tools": {"sys_bgr_control": {}},
                            "toolsError": "catalog" if failure == "catalog" else None,
                        }
                    ]
                }
            }
        assert method == "thread/name/set"
        assert params == {"threadId": thread_id, "name": "Brain"}
        assert runtime._RUNTIMES[BRAIN].ready.is_set()
        if failure == "materialize":
            raise RuntimeError("materialize")
        return {"result": {}}

    async def callback(payload):
        calls.append("bind")
        assert payload["thread_id"] == thread_id
        assert runtime._RUNTIMES[BRAIN].brain.client is client
        if failure == "bind":
            raise RuntimeError("bind")

    async def close():
        calls.append("close")
        native.proc = None

    native.close = close
    monkeypatch.setattr(client, "request", request)
    try:
        pending = orchestration._prepare_bgr_brain_thread(
            session_id=BRAIN,
            client=client,
            app_server=native,
            workspace=tmp_path,
            profiles=CONFIG,
            callback=callback,
            host_id=HOST,
            team_id=uuid4().hex,
            server_client=SimpleNamespace(),
            spec=AgentSpec(spec_version=1),
            thread_id=None if fresh else thread_id,
            model="registered",
            bridge_dir=tmp_path,
            ensure_comment_relay=ensure_relay,
        )
        if failure in {"relay", "catalog", "bind"} or (fresh and failure == "materialize"):
            with pytest.raises(RuntimeError, match=failure):
                await pending
        else:
            assert await pending == thread_id
        expected = ["relay"]
        if failure != "relay":
            expected += ["bgr/capabilities/read", "thread/start"] if fresh else []
            expected += ["mcpServerStatus/list"]
            if failure != "catalog":
                expected += ["bind"]
                if fresh and failure != "bind":
                    expected += ["thread/name/set"]
        assert calls == expected
        assert runtime.bgr_brain_registered(BRAIN)
        await orchestration._close_codex_native_app_server(BRAIN, native)
        assert calls[-1] == "close"
        assert not runtime.bgr_brain_registered(BRAIN)
    finally:
        runtime._RUNTIMES.pop(BRAIN, None)
        await client.close()


@pytest.mark.parametrize("state", ["ready", "late", "missing", "starting", "error", "foreign"])
async def test_control_catalog_barrier_requires_actual_ready_tool(state):
    thread_id = str(uuid4())
    calls = 0

    async def request(method, params):
        nonlocal calls
        assert method == "mcpServerStatus/list"
        assert params["threadId"] == thread_id
        calls += 1
        return {
            "result": {
                "data": [
                    {
                        "name": "other" if state == "foreign" else "omnigent",
                        "runtimeStatus": "starting" if state == "starting" else "connected",
                        "tools": {}
                        if state == "missing" or (state == "late" and calls == 1)
                        else {"sys_bgr_control": {}},
                        "toolsError": "not ready" if state == "error" else None,
                    }
                ]
            }
        }

    client = SimpleNamespace(request=request)
    if state in {"ready", "late"}:
        await runtime.wait_bgr_control_ready(client, thread_id, timeout=1)
        assert calls == (2 if state == "late" else 1)
    else:
        with pytest.raises(RuntimeError, match="control"):
            await runtime.wait_bgr_control_ready(client, thread_id, timeout=0.01)
