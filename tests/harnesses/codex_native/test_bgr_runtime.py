"""Host ownership and readiness gates; no SQL or fake provider-execution claims."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnigent.bgr import Profile
from omnigent.errors import OmnigentError
from omnigent.harnesses.codex_native.app_server import CodexAppServerClient
from omnigent.harnesses.multidex_native.config import MultiDexConfig, MultiDexModelRegistration
from omnigent.runner import bgr_runtime as runtime
from tests.harnesses.codex_native.test_bgr_messages import CAPTURED
from tests.stores.test_bgr_store import BRAIN, CONFIG, HOST


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


@pytest.mark.parametrize("variant,speed", [("regular", "1.25"), (None, None)])
def test_registered_profile_preserves_provider_endpoint(monkeypatch, variant, speed):
    registration = MultiDexModelRegistration(
        model="registered",
        provider="lithos",
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
        provider="lithos",
        variant=variant,
        speed=speed,
    )
    launch = runtime.resolve_bgr_profile(profile, None)
    assert launch.model == "registered"
    assert 'model_provider="lithos"' in launch.config_overrides
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
    }
    assert runtime.bgr_native_config_overrides() == (
        "features.multi_agent=false",
        "features.multi_agent_v2=false",
        "features.defer_mailbox_preemption=true",
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
