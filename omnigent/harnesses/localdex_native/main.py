"""CLI-facing LocalDex native wrapper."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from omnigent.harnesses.codex_native.main import _run_with_remote_server
from omnigent.native.native_coding_agents import native_shell_terminal_spec

from .config import LOCALDEX_BINARY, load_localdex_config

# Server-side built-in-agent seeding must be credential-free. The host validates
# and materializes the dedicated LocalDex config immediately before launch.
_DEFAULT_LOCALDEX_MODEL = "QB/DSV4.1-Flash"


def _materialize_localdex_agent_spec(tmpdir: Path, *, model: str | None) -> Path:
    """Write a LocalDex-owned terminal-first agent spec."""
    path = tmpdir / "localdex-native-ui.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "localdex-native-ui",
                "prompt": (
                    "LocalDex is running in the session terminal. Web UI messages are "
                    "forwarded into the same isolated LocalDex app-server thread."
                ),
                "executor": {
                    "harness": "localdex-native",
                    "model": model or _DEFAULT_LOCALDEX_MODEL,
                },
                "spawn": True,
                "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
                "terminals": native_shell_terminal_spec(),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def run_localdex_native(
    *,
    server: str | None,
    session_id: str | None,
    extra_args: tuple[str, ...] | None = None,
    codex_args: tuple[str, ...] | None = None,
    resume_picker: bool = False,
    command: str = str(LOCALDEX_BINARY),
    model: str | None = None,
    prompt: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """Launch the pinned LocalDex wrapper against an Omnigent server."""
    if server is None:
        raise ValueError("LocalDex requires a resolved Omnigent server URL")
    # Validate the dedicated host-only provider contract before a session is
    # created. Server seeding deliberately does not call this helper.
    load_localdex_config()
    if Path(command) != LOCALDEX_BINARY:
        raise ValueError("LocalDex must use its pinned localdex executable")
    if not LOCALDEX_BINARY.is_file():
        raise FileNotFoundError(f"LocalDex binary is missing: {LOCALDEX_BINARY}")
    args = extra_args if extra_args is not None else (codex_args or ())
    with TemporaryDirectory(prefix="omnigent-localdex-native-") as temp_dir:
        spec_path = _materialize_localdex_agent_spec(Path(temp_dir), model=model)
        _run_with_remote_server(
            server.rstrip("/"),
            spec_path,
            session_id=session_id,
            resume_picker=resume_picker,
            codex_args=args,
            model=model,
            prompt=prompt,
            auto_open_conversation=auto_open_conversation,
        )
