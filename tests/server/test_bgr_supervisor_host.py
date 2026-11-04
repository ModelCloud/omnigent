"""Exact supervisor receipts over the host channel, independent of runner tunnels."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from omnigent.host.connect import HostProcess
from omnigent.host.frames import (
    CAP_BGR_SUPERVISOR_STOP,
    HostHelloFrame,
    HostStopBgrGenerationFrame,
    HostStopBgrGenerationResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from tests.host.test_bgr_supervisor import request_stop as request_stop
from tests.host.test_bgr_supervisor import stopped as stopped
from tests.server.test_host_registry import FakeWebSocket


def _connection(registry, request, *, owner="owner", capable=True):
    return registry.register(
        request.admission.generation.host_id,
        FakeWebSocket(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="trusted-host",
            capabilities=[CAP_BGR_SUPERVISOR_STOP] if capable else [],
        ),
        owner=owner,
    )


def test_supervisor_frames_round_trip(request_stop, stopped):
    for frame in (
        HostStopBgrGenerationFrame(request_stop),
        HostStopBgrGenerationResultFrame(stopped),
    ):
        assert decode_host_frame(encode_host_frame(frame)) == frame


@pytest.mark.parametrize("fault", [None, "replaced", "mismatch", "unknown"])
async def test_host_stop_requires_current_connection_and_exact_receipt(
    request_stop, stopped, fault
):
    registry = HostRegistry()
    conn = _connection(registry, request_stop)
    pending = asyncio.create_task(
        registry.request_bgr_generation_stop(conn, request_stop, owner_id="owner", timeout_s=1)
    )
    queued = await asyncio.wait_for(conn.outbound_queue.get(), timeout=1)
    assert decode_host_frame(queued.data) == HostStopBgrGenerationFrame(request_stop)
    future = conn.pending_bgr_stops[request_stop.request_id]
    wire = stopped.model_dump(mode="json")
    if fault == "mismatch":
        wire["receipt"]["recovery_id"] = "f" * 32
    elif fault == "unknown":
        wire = {
            "status": "unknown",
            "request_id": request_stop.request_id,
            "reason": "unsupported",
        }
    future.set_result(wire)
    if fault == "replaced":
        _connection(registry, request_stop)
    if fault is None:
        assert await pending == stopped.receipt
    else:
        with pytest.raises((ConnectionError, ValueError)):
            await pending
    assert not conn.pending_bgr_stops


@pytest.mark.parametrize("capable,owner", [(False, "owner"), (True, "different-owner")])
async def test_old_or_foreign_host_never_gets_a_stop_request(request_stop, capable, owner):
    registry = HostRegistry()
    conn = _connection(registry, request_stop, owner=owner, capable=capable)
    with pytest.raises(ConnectionError):
        await registry.request_bgr_generation_stop(conn, request_stop, owner_id="owner")
    assert conn.outbound_queue.empty()
    assert not conn.pending_bgr_stops


async def test_host_loss_and_timeout_leave_stop_unknown(request_stop):
    registry = HostRegistry()
    conn = _connection(registry, request_stop)
    pending = asyncio.create_task(
        registry.request_bgr_generation_stop(conn, request_stop, owner_id="owner", timeout_s=1)
    )
    await asyncio.wait_for(conn.outbound_queue.get(), timeout=1)
    registry.deregister(conn.host_id)
    with pytest.raises(ConnectionError):
        await pending
    assert not conn.pending_bgr_stops

    conn = _connection(registry, request_stop)
    with pytest.raises(TimeoutError):
        await registry.request_bgr_generation_stop(
            conn, request_stop, owner_id="owner", timeout_s=0.01
        )
    assert not conn.pending_bgr_stops


async def test_unconfigured_host_never_uses_runner_or_native_stop(request_stop):
    host = object.__new__(HostProcess)
    host._identity = SimpleNamespace(host_id=request_stop.admission.generation.host_id)
    host._bgr_supervisor_socket = None
    # No _runners or runner client exists: the supervisor handler must not use one.
    result = await host._handle_bgr_generation_stop(HostStopBgrGenerationFrame(request_stop))
    assert result.result.status == "unknown"
