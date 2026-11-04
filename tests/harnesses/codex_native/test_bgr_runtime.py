"""Host ownership and readiness gates; no SQL or fake provider-execution claims."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnigent.errors import OmnigentError
from omnigent.harnesses.codex_native.app_server import CodexAppServerClient
from omnigent.runner import bgr_runtime as runtime
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
