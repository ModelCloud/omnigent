"""Regression: a resume must launch on the provider its thread was saved under.

Codex's ``ConfigManager::load_for_resume`` refuses a thread whose saved
provider differs from the configured one:

    -32600 {'type': 'modelProviderMismatch',
            'modelProvider': 'omnigent_provider',
            'currentModelProvider': 'openrouter'}

The check runs against the *saved* route even when the resume request names
another provider, so a resume override cannot recover it -- the launch has to
agree. The launch provider is re-resolved from ambient host state on every
restart while the thread keeps its original provider, so a host that gains or
loses a credential such as ``OPENROUTER_API_KEY`` starts failing every turn.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.harnesses.codex_native.app_server import NativeCodexLaunch
from omnigent.runner.native.orchestration import _align_launch_provider_with_thread

_THREAD_ID = "01a194b1-ed2b-7dd3-89eb-9ad43d279835"

_SAME_ROUTE_TABLES = """
[model_providers.omnigent_provider]
name = "Omnigent Provider"
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"

[model_providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"
"""


def _codex_home(tmp_path: Path, tables: str, provider: str) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        f'model = "gpt-6-astra"\nmodel_provider = "{provider}"\n{tables}',
        encoding="utf-8",
    )
    return home


def _write_rollout(home: Path, provider: str | None) -> None:
    sessions = home / "sessions" / "2026" / "11" / "01"
    sessions.mkdir(parents=True)
    payload = {"id": _THREAD_ID, "timestamp": "2026-11-01T01:20:51Z", "cwd": "/monster/home"}
    if provider is not None:
        payload["model_provider"] = provider
    (sessions / f"rollout-2026-11-01T01-20-51-{_THREAD_ID}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": payload}) + "\n", encoding="utf-8"
    )


def _launch(provider: str) -> NativeCodexLaunch:
    return NativeCodexLaunch(
        config_overrides=[
            f"model_provider={json.dumps(provider)}",
            'model="gpt-6-astra"',
        ],
        model="gpt-6-astra",
        profile=None,
        summary="test",
    )


def test_resume_retargets_launch_onto_the_saved_provider(tmp_path: Path) -> None:
    # The thread was created under omnigent_provider; this restart re-resolved
    # the ambient "openrouter" key detector and would launch with that name.
    home = _codex_home(tmp_path, _SAME_ROUTE_TABLES, provider="openrouter")
    _write_rollout(home, "omnigent_provider")

    launch, saved = _align_launch_provider_with_thread(
        _launch("openrouter"), initial_thread_id=_THREAD_ID, codex_home=home
    )

    assert saved == "omnigent_provider"
    assert 'model_provider="omnigent_provider"' in launch.config_overrides
    assert 'model_provider="openrouter"' not in launch.config_overrides
    # Only the selection key moves; the rest of the launch is untouched.
    assert 'model="gpt-6-astra"' in launch.config_overrides


def test_fresh_launch_is_never_retargeted(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, _SAME_ROUTE_TABLES, provider="openrouter")
    _write_rollout(home, "omnigent_provider")

    launch, saved = _align_launch_provider_with_thread(
        _launch("openrouter"), initial_thread_id=None, codex_home=home
    )

    assert saved is None
    assert 'model_provider="openrouter"' in launch.config_overrides


def test_matching_providers_are_left_alone(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, _SAME_ROUTE_TABLES, provider="omnigent_provider")
    _write_rollout(home, "omnigent_provider")

    launch, saved = _align_launch_provider_with_thread(
        _launch("omnigent_provider"), initial_thread_id=_THREAD_ID, codex_home=home
    )

    assert saved == "omnigent_provider"
    assert launch.config_overrides[0] == 'model_provider="omnigent_provider"'


def test_different_endpoints_are_never_merged(tmp_path: Path) -> None:
    """A rename must not redirect a thread to another upstream."""
    tables = """
[model_providers.omnigent_provider]
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"

[model_providers.openrouter]
base_url = "https://eu.openrouter.ai/api/v1"
wire_api = "responses"
"""
    home = _codex_home(tmp_path, tables, provider="openrouter")
    _write_rollout(home, "omnigent_provider")

    launch, saved = _align_launch_provider_with_thread(
        _launch("openrouter"), initial_thread_id=_THREAD_ID, codex_home=home
    )

    assert saved == "omnigent_provider"
    assert 'model_provider="openrouter"' in launch.config_overrides


def test_identical_credentials_are_reconciled(tmp_path: Path) -> None:
    """Both generated tables carry the same key; the rename must still apply.

    The real host is exactly this shape: ``omnigent_provider`` and
    ``openrouter`` each hold their own ``auth`` block, both printing the same
    ``OPENROUTER_API_KEY`` value. The ids differ, the route and secret do not.
    """
    tables = """
[model_providers.omnigent_provider]
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"
experimental_bearer_token = "sk-same"

[model_providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"
experimental_bearer_token = "sk-same"
"""
    home = _codex_home(tmp_path, tables, provider="openrouter")
    _write_rollout(home, "omnigent_provider")

    launch, saved = _align_launch_provider_with_thread(
        _launch("openrouter"), initial_thread_id=_THREAD_ID, codex_home=home
    )

    assert saved == "omnigent_provider"
    assert 'model_provider="omnigent_provider"' in launch.config_overrides


def test_different_credentials_are_never_swapped(tmp_path: Path) -> None:
    """A rename must not substitute one secret for another."""
    tables = """
[model_providers.omnigent_provider]
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"
experimental_bearer_token = "sk-saved"

[model_providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"
experimental_bearer_token = "sk-launch"
"""
    home = _codex_home(tmp_path, tables, provider="openrouter")
    _write_rollout(home, "omnigent_provider")

    launch, _ = _align_launch_provider_with_thread(
        _launch("openrouter"), initial_thread_id=_THREAD_ID, codex_home=home
    )

    assert 'model_provider="openrouter"' in launch.config_overrides


def test_unknown_saved_provider_leaves_launch_untouched(tmp_path: Path) -> None:
    tables = """
[model_providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"
"""
    home = _codex_home(tmp_path, tables, provider="openrouter")
    _write_rollout(home, "omnigent_provider")

    launch, saved = _align_launch_provider_with_thread(
        _launch("openrouter"), initial_thread_id=_THREAD_ID, codex_home=home
    )

    assert saved == "omnigent_provider"
    assert 'model_provider="openrouter"' in launch.config_overrides


def test_thread_store_row_is_the_fallback_when_no_rollout_exists(tmp_path: Path) -> None:
    import sqlite3

    home = _codex_home(tmp_path, _SAME_ROUTE_TABLES, provider="openrouter")
    conn = sqlite3.connect(home / "state_5.sqlite")
    conn.execute("create table threads (id text primary key, model_provider text)")
    conn.execute("insert into threads values (?, ?)", (_THREAD_ID, "omnigent_provider"))
    conn.commit()
    conn.close()

    launch, saved = _align_launch_provider_with_thread(
        _launch("openrouter"), initial_thread_id=_THREAD_ID, codex_home=home
    )

    assert saved == "omnigent_provider"
    assert 'model_provider="omnigent_provider"' in launch.config_overrides
