"""Telemetry opt-outs are forced into every session child environment."""

from __future__ import annotations

import pytest

from omnigent.privacy_env import PRIVACY_ENV, apply_privacy_env, install_privacy_env, privacy_env

REQUIRED_OPT_OUTS = (
    ("DO_NOT_TRACK", "1"),
    ("DISABLE_TELEMETRY", "1"),
    ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),
    ("CLAUDE_CODE_DISABLE_ERROR_REPORTING", "1"),
)


def test_required_opt_outs_are_present() -> None:
    """The cross-vendor and Claude Code opt-outs stay in the canonical list."""
    for name, value in REQUIRED_OPT_OUTS:
        assert (name, value) in PRIVACY_ENV


def test_privacy_env_returns_a_fresh_copy() -> None:
    """Mutating a caller's dict must not change the canonical list."""
    env = privacy_env()
    env["DO_NOT_TRACK"] = "0"
    assert dict(PRIVACY_ENV)["DO_NOT_TRACK"] == "1"


def test_apply_privacy_env_overrides_inherited_values() -> None:
    """An inherited ``DO_NOT_TRACK=0`` cannot re-enable telemetry."""
    env = {"DO_NOT_TRACK": "0", "DISABLE_TELEMETRY": "false", "KEEP_ME": "value"}
    apply_privacy_env(env)
    assert env["DO_NOT_TRACK"] == "1"
    assert env["DISABLE_TELEMETRY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["KEEP_ME"] == "value"


def test_install_privacy_env_targets_the_supplied_environment() -> None:
    """Entry points install the opt-outs into the environment they pass."""
    env: dict[str, str] = {}
    install_privacy_env(env)
    assert env["DO_NOT_TRACK"] == "1"


def test_clean_agent_env_forces_opt_outs_despite_source_and_allowlist() -> None:
    """The harness filter keeps the opt-outs even when the source disables them."""
    from omnigent.inner.agent_env import clean_agent_env

    env = clean_agent_env(source={"DO_NOT_TRACK": "0", "PATH": "/usr/bin"})
    assert env["DO_NOT_TRACK"] == "1"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


def test_terminal_pane_environment_forces_opt_outs() -> None:
    """Terminal panes run agent CLIs, so they carry the opt-outs too."""
    from omnigent.inner.terminal import terminal_pane_environment

    env = terminal_pane_environment(
        inherit_env=True,
        env={"DO_NOT_TRACK": "0"},
        env_unset=(),
    )
    assert env["DO_NOT_TRACK"] == "1"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


def test_claude_terminal_env_forces_opt_outs() -> None:
    """Claude Code's own terminal environment carries the opt-outs."""
    claude_native_main = pytest.importorskip("omnigent.harnesses.claude_native.main")

    env = claude_native_main.build_native_claude_terminal_env(None)
    assert env["DO_NOT_TRACK"] == "1"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
