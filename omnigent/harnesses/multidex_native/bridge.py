"""MultiDex bridge paths and environment names, isolated from codex-native."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from omnigent.native import native_bridge_common

MULTIDEX_NATIVE_BRIDGE_ID_LABEL_KEY = "omnigent.multidex_native.bridge_id"
MULTIDEX_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_MULTIDEX_NATIVE_BRIDGE_DIR"
MULTIDEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR = "HARNESS_MULTIDEX_NATIVE_REQUEST_SESSION_ID"
_BRIDGE_ROOT = Path.home() / ".omnigent" / "multidex-native"


def bridge_root() -> Path:
    """Return MultiDex's private bridge root."""
    return _BRIDGE_ROOT


def bridge_dir_for_bridge_id(bridge_id: str) -> Path:
    """Resolve a MultiDex bridge id without entering codex-native state."""
    digest = hashlib.sha256(bridge_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def prepare_bridge_dir(bridge_id: str) -> Path:
    """Create a private MultiDex bridge directory."""
    bridge_dir = bridge_dir_for_bridge_id(bridge_id)
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(bridge_dir, 0o700)
    native_bridge_common.write_owner_pid_marker(bridge_dir)
    return bridge_dir


def build_multidex_native_spawn_env(
    conversation_id: str, *, bridge_id: str | None = None
) -> dict[str, str]:
    """Build the unique MultiDex bridge environment."""
    resolved_bridge_id = bridge_id or conversation_id
    return {
        MULTIDEX_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_bridge_id(resolved_bridge_id)),
        MULTIDEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR: conversation_id,
    }
