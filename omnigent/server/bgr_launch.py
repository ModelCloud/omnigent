"""Owner-authorized preflight for native BGR team launch."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from omnigent.bgr import TeamConfig
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_BGR_PROBE
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._host_model_options import request_host_model_options
from omnigent.stores.host_store import HostStore


def _speed_matches(advertised: object, selected: str | None) -> bool:
    if advertised is None or selected is None:
        return advertised is None and selected is None
    if isinstance(advertised, bool) or not isinstance(advertised, (int, float, str)):
        return False
    try:
        advertised_speed, selected_speed = float(advertised), float(selected)
    except (ValueError, OverflowError):
        return False
    return (
        math.isfinite(advertised_speed)
        and math.isfinite(selected_speed)
        and advertised_speed > 0
        and advertised_speed == selected_speed
    )


async def probe_host(
    *, user_id: str, host_id: str, hosts: HostStore, registry: HostRegistry
) -> dict[str, Any]:
    """Authorize before sending any host RPC; old hosts remain explicitly blocked."""
    host = await asyncio.to_thread(
        resolve_host_owner, user_id=user_id, host_id=host_id, host_store=hosts
    )
    conn = registry.get(host.host_id)
    reason = None
    if conn is None:
        reason = "Host is offline; reconnect it before launching BGR"
    elif CAP_BGR_PROBE not in conn.hello.capabilities:
        reason = "Update the host to a version with the BGR runtime"
    else:
        try:
            return await registry.request_bgr_probe(conn)
        except (ConnectionError, TimeoutError):
            reason = "BGR runtime probe did not complete; reconnect or update the host"
    return {
        "launch_supported": False,
        "supported_harnesses": [],
        "reviewer_isolation": False,
        "reason": reason,
    }


async def validate_profiles(*, host_id: str, config: TeamConfig, registry: HostRegistry) -> None:
    """Require every role's model/provider/variant to match advertised host metadata."""
    profiles = [config.brain, config.grunt, *config.reviewers]
    if any(profile.harness != "codex-native" for profile in profiles):
        raise OmnigentError(
            "This BGR runtime requires MultiDex for every role",
            code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
        )
    conn = registry.get(host_id)
    if conn is None:
        raise OmnigentError("BGR host disconnected", code=ErrorCode.RUNNER_CAPABILITY_MISMATCH)
    listing = await request_host_model_options(
        host_registry=registry, host_conn=conn, harness="codex-native", timeout_s=30
    )
    rows = listing.get("models")
    if listing.get("status") != "ok" or not isinstance(rows, list):
        raise OmnigentError(
            "Cannot validate BGR profiles against the host catalog",
            code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
        )
    for profile in profiles:
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("id") == profile.model
            and row.get("providerId") == profile.provider
            and (row.get("variant") or None) == profile.variant
            and _speed_matches(row.get("speed"), profile.speed)
        ]
        if not matches:
            raise OmnigentError(
                "BGR role profile is not advertised by the selected host",
                code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
            )
