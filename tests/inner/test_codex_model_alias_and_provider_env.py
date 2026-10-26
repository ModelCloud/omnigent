"""Regression tests for the two agent-gpu-0 failures.

1. A ``config.toml`` carrying the retired ``QB/DSV4.1-Flash`` id made every
   new session and resume fail, because the file is copied verbatim into each
   private ``CODEX_HOME``.
2. A provider ``env_key`` never reached the app-server, so a MultiDex/OpenRouter
   turn died with ``Missing environment variable: OPENROUTER_API_KEY`` even
   though the host service had the key set.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.harnesses.codex_native.app_server import (
    codex_config_env_key_names,
    codex_terminal_env,
)
from omnigent.inner.codex_executor import _normalize_copied_codex_model
from omnigent.models.legacy_model_aliases import canonical_model_id, is_legacy_model_id


class _FakeAppServer:
    """Minimal stand-in for the attributes ``codex_terminal_env`` reads."""

    def __init__(self, *, env: dict[str, str], codex_home: Path, provider_env_keys=(), **kw):
        self.env = env
        self.codex_home = codex_home
        self.provider_env_keys = provider_env_keys
        self.isolated_env_keys = kw.get("isolated_env_keys", ())
        self.config_profile = kw.get("config_profile")


# --------------------------------------------------------------------------- #
# Retired model id -> canonical id
# --------------------------------------------------------------------------- #
def test_legacy_aliases_map_to_canonical_and_pass_others_through() -> None:
    assert canonical_model_id("QB/DSV4.1-Flash") == "DS v4.1 Flash 1017"
    assert canonical_model_id(" qb/dsv4.1-flash ") == "DS v4.1 Flash 1017"
    # Canonical and unrelated ids are untouched.
    assert canonical_model_id("DS v4.1 Flash 1017") == "DS v4.1 Flash 1017"
    assert canonical_model_id("gpt-6-sol") == "gpt-6-sol"
    assert canonical_model_id(None) is None
    assert is_legacy_model_id("QB/DSV4.1-Flash") is True
    assert is_legacy_model_id("gpt-6-sol") is False


def test_normalize_rewrites_only_top_level_model(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "QB/DSV4.1-Flash"\n'
        'model_reasoning_effort = "high"\n'
        "[model_providers.localdex]\n"
        'base_url = "http://10.0.13.33:2120/v1"\n'
        'env_key = "LOCALDEX_API_KEY"\n'
        # A provider-level key named model must not be rewritten.
        '[profiles.other]\n'
        'model = "QB/DSV4.1-Flash"\n',
        encoding="utf-8",
    )
    _normalize_copied_codex_model(config)
    text = config.read_text(encoding="utf-8")
    assert text.startswith('model = "DS v4.1 Flash 1017"')
    # Below the first table header the stale id is preserved verbatim.
    assert text.count('"QB/DSV4.1-Flash"') == 1
    assert 'model_reasoning_effort = "high"' in text


def test_normalize_is_noop_for_canonical_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = 'model = "DS v4.1 Flash 1017"\nmodel_provider = "localdex"\n'
    config.write_text(original, encoding="utf-8")
    _normalize_copied_codex_model(config)
    assert config.read_text(encoding="utf-8") == original


def test_normalize_survives_missing_file(tmp_path: Path) -> None:
    # Best effort: a missing private copy must not raise during session boot.
    _normalize_copied_codex_model(tmp_path / "nope.toml")


# --------------------------------------------------------------------------- #
# Provider env_key passthrough
# --------------------------------------------------------------------------- #
def test_collects_env_keys_from_every_provider(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[model_providers.localdex]\n"
        'base_url = "http://10.0.13.33:2120/v1"\n'
        'env_key = "LOCALDEX_API_KEY"\n'
        "[model_providers.openrouter]\n"
        'base_url = "https://openrouter.ai/api/v1"\n'
        'env_key = "OPENROUTER_API_KEY"\n',
        encoding="utf-8",
    )
    assert codex_config_env_key_names(config) == ("LOCALDEX_API_KEY", "OPENROUTER_API_KEY")


def test_ignores_invalid_env_key_and_missing_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[model_providers.bad]\nenv_key = "not a var!"\n', encoding="utf-8"
    )
    assert codex_config_env_key_names(config) == ()
    assert codex_config_env_key_names(tmp_path / "absent.toml") == ()

    broken = tmp_path / "broken.toml"
    broken.write_text("this is not toml [[[", encoding="utf-8")
    assert codex_config_env_key_names(broken) == ()


def test_terminal_env_forwards_provider_key_for_multidex(tmp_path: Path) -> None:
    """The reported bug: the key exists on the host but is filtered out."""
    app = _FakeAppServer(
        env={
            "OPENROUTER_API_KEY": "sk-or-secret",
            "LOCALDEX_API_KEY": "babayaga",
            "UNRELATED_SECRET": "nope",
            "CODEX_HOME": "/tmp/ignored",
        },
        codex_home=tmp_path,
        provider_env_keys=("OPENROUTER_API_KEY", "LOCALDEX_API_KEY"),
    )
    env = codex_terminal_env(app)
    assert env["OPENROUTER_API_KEY"] == "sk-or-secret"
    assert env["LOCALDEX_API_KEY"] == "babayaga"
    # Non-declared variables still stay out of the child.
    assert "UNRELATED_SECRET" not in env
    assert env["CODEX_HOME"] == str(tmp_path)


def test_terminal_env_keeps_key_out_when_not_declared(tmp_path: Path) -> None:
    """A provider the config does not declare stays filtered.

    Only names read from the user's own config are forwarded; an ambient
    credential with no matching provider declaration must not leak.
    """
    app = _FakeAppServer(
        env={"OPENROUTER_API_KEY": "sk-or-secret", "CODEX_HOME": "/tmp/x"},
        codex_home=tmp_path,
        provider_env_keys=(),
    )
    env = codex_terminal_env(app)
    assert "OPENROUTER_API_KEY" not in env


def test_provider_env_keys_apply_to_plain_codex_native(tmp_path: Path, monkeypatch) -> None:
    """The reported host ran plain codex-native, not MultiDex.

    agent-gpu-0's live process tag was ``codex-native`` while its config.toml
    declared an OpenRouter provider, so gating forwarding on MultiDex would
    have left the original bug in place. Both sources are collected for any
    native launch.
    """
    from omnigent.runner.native import orchestration

    (tmp_path / "config.toml").write_text(
        '[model_providers.openrouter]\n'
        'base_url = "https://openrouter.ai/api/v1"\n'
        'env_key = "OPENROUTER_API_KEY"\n',
        encoding="utf-8",
    )
    (tmp_path / "local-openai.config.toml").write_text(
        '[model_providers.local-openai]\n'
        'env_key = "CODEX_LOCAL_OPENAI_API_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "omnigent.inner.codex_executor._codex_home_config_source_from_env",
        lambda: tmp_path,
    )
    # Both the shared config and the selected profile contribute.
    assert orchestration._codex_provider_env_keys("local-openai") == (
        "CODEX_LOCAL_OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    )
    # No profile: only the shared config's provider applies.
    assert orchestration._codex_provider_env_keys(None) == ("OPENROUTER_API_KEY",)
