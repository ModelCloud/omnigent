"""Tests for the local mirror of Codex's provider-auth readiness logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.onboarding.codex_auth_readiness import (
    codex_config_effective_auth,
    effective_codex_model_provider,
    load_codex_config,
    provider_table_auth_token,
    provider_table_has_self_contained_auth,
)


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


_ENV_KEY_CONFIG = """\
model_provider = "gateway"

[model_providers.gateway]
env_key = "GATEWAY_TOKEN"
"""


def test_active_profile_provider_wins(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        """\
profile = "work"
model_provider = "top"

[profiles.work]
model_provider = "profile-provider"
""",
    )
    parsed = load_codex_config(path)
    assert parsed is not None
    assert effective_codex_model_provider(parsed) == "profile-provider"


@pytest.mark.parametrize("provider", ["openrouter", "lithos"])
@pytest.mark.parametrize("with_table", [False, True])
@pytest.mark.parametrize("value", [None, "test-key"])
def test_builtin_gateway_needs_a_config_toml_credential(
    tmp_path: Path, provider: str, with_table: bool, value: str | None
) -> None:
    """Env vars can never authenticate the gateway built-ins.

    Neither an exported variable nor an ``env_key`` naming one counts —
    the launch environment forwards no provider credentials.
    """
    body = f'model_provider = "{provider}"\n'
    if with_table:
        body += f'[model_providers.{provider}]\nenv_key = "PROVIDER_TOKEN"\n'
    path = _config(tmp_path, body)
    env = {} if value is None else {"PROVIDER_TOKEN": value}
    assert codex_config_effective_auth(path, env=env) == "provider-auth-missing"


@pytest.mark.parametrize("provider", ["openrouter", "lithos"])
def test_builtin_gateway_bearer_token_is_ready(tmp_path: Path, provider: str) -> None:
    path = _config(
        tmp_path,
        f'model_provider = "{provider}"\n[model_providers.{provider}]\n'
        'experimental_bearer_token = "token"\n',
    )
    assert codex_config_effective_auth(path, env={}) == "provider-ready"


def test_openrouter_builtin_auth_command_does_not_require_env_key(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        'model_provider = "openrouter"\n[model_providers.openrouter.auth]\n'
        'command = "print-token"\n',
    )
    assert codex_config_effective_auth(path, env={}) == "provider-ready"


@pytest.mark.parametrize("value", ["token", " token "])
def test_env_key_present_is_provider_auth_missing(tmp_path: Path, value: str) -> None:
    path = _config(tmp_path, _ENV_KEY_CONFIG)
    assert (
        codex_config_effective_auth(path, env={"GATEWAY_TOKEN": value}) == "provider-auth-missing"
    )


@pytest.mark.parametrize("env", [{}, {"GATEWAY_TOKEN": ""}, {"GATEWAY_TOKEN": "   "}])
def test_env_key_missing_is_provider_auth_missing(tmp_path: Path, env: dict[str, str]) -> None:
    path = _config(tmp_path, _ENV_KEY_CONFIG)
    assert codex_config_effective_auth(path, env=env) == "provider-auth-missing"


def test_profile_selected_env_key_is_checked(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        """\
profile = "work"

[profiles.work]
model_provider = "gateway"

[model_providers.gateway]
env_key = "GATEWAY_TOKEN"
""",
    )
    assert codex_config_effective_auth(path, env={}) == "provider-auth-missing"


@pytest.mark.parametrize(
    "provider_body",
    [
        "[model_providers.gateway]\n",
        '[model_providers.gateway.auth]\ncommand = "print-token"\n',
        '[model_providers.gateway]\nexperimental_bearer_token = "token"\n',
        '[model_providers.gateway.http_headers]\napi-key = "token"\n',
    ],
)
def test_non_openai_provider_without_env_key_is_ready(tmp_path: Path, provider_body: str) -> None:
    path = _config(tmp_path, f'model_provider = "gateway"\n{provider_body}')
    # Mirrors Codex: absent env_key means provider-specific auth is sufficient
    # or unnecessary. Credential validity remains a runtime concern.
    assert codex_config_effective_auth(path, env={}) == "provider-ready"


def test_requires_openai_auth_uses_codex_login(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        """\
model_provider = "gateway"

[model_providers.gateway]
requires_openai_auth = true
""",
    )
    assert codex_config_effective_auth(path, env={}) == "codex-login"


@pytest.mark.parametrize(
    "body",
    [
        "",
        'model_provider = "openai"\n',
        "model_provider = [broken\n",
    ],
)
def test_configs_that_cannot_supply_provider_auth_use_codex_login(
    tmp_path: Path, body: str
) -> None:
    assert codex_config_effective_auth(_config(tmp_path, body), env={}) == "codex-login"


@pytest.mark.parametrize(
    "provider", ["ollama", "lmstudio", "amazon-bedrock", "amazon-bedrock-runtime"]
)
def test_non_openai_builtin_provider_is_ready(tmp_path: Path, provider: str) -> None:
    path = _config(tmp_path, f'model_provider = "{provider}"\n')
    assert codex_config_effective_auth(path, env={}) == "provider-ready"


def test_missing_custom_provider_table_is_not_masked_by_login(tmp_path: Path) -> None:
    path = _config(tmp_path, 'model_provider = "missing-table"\n')
    assert codex_config_effective_auth(path, env={}) == "provider-auth-missing"


def test_redefined_ollama_table_is_ignored(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        'model_provider = "ollama"\n[model_providers.ollama]\nenv_key = "OLLAMA_TOKEN"\n',
    )
    assert codex_config_effective_auth(path, env={}) == "provider-ready"


def test_redefined_openai_table_is_ignored(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        'model_provider = "openai"\n[model_providers.openai]\nrequires_openai_auth = false\n',
    )
    assert codex_config_effective_auth(path, env={}) == "codex-login"


def test_missing_config_uses_codex_login(tmp_path: Path) -> None:
    assert codex_config_effective_auth(tmp_path / "missing.toml", env={}) == "codex-login"


@pytest.mark.parametrize("header", ["Authorization", "api-key", "x-api-key"])
def test_auth_headers_are_self_contained_for_adoption(header: str) -> None:
    assert provider_table_has_self_contained_auth({"http_headers": {header: "credential"}})


def test_env_key_is_not_self_contained_for_adoption() -> None:
    assert not provider_table_has_self_contained_auth({"env_key": "GATEWAY_TOKEN"})


def test_auth_token_prefers_literal_bearer_token() -> None:
    assert provider_table_auth_token({"experimental_bearer_token": " tok-literal "}) == (
        "tok-literal"
    )


def test_auth_token_runs_command_backed_auth() -> None:
    """A built-in gateway provider's credential comes from its ``auth`` command.

    Codex resolves the token by running the command; no provider credential is
    ever read from the process environment, so the mirror must do the same.
    """
    assert (
        provider_table_auth_token(
            {"auth": {"command": "sh", "args": ["-c", "printf %s tok-cmd"], "timeout_ms": 5000}}
        )
        == "tok-cmd"
    )


@pytest.mark.parametrize(
    "table",
    [
        pytest.param({"env_key": "GATEWAY_TOKEN"}, id="env-key-only"),
        pytest.param({"auth": {"command": "   "}}, id="blank-command"),
        pytest.param({"auth": {"command": "/nonexistent/credential-binary"}}, id="missing-binary"),
        pytest.param({"auth": {"command": "sh", "args": ["-c", "exit 3"]}}, id="nonzero-exit"),
        pytest.param({"auth": {"command": "sh", "args": ["-c", "printf '   '"]}}, id="blank-out"),
    ],
)
def test_auth_token_resolves_to_none_on_unusable_credential(table: dict[str, object]) -> None:
    assert provider_table_auth_token(table) is None


def test_auth_token_honours_the_table_timeout() -> None:
    """A hung credential command fails closed instead of stalling the probe."""
    assert (
        provider_table_auth_token(
            {"auth": {"command": "sh", "args": ["-c", "sleep 5"], "timeout_ms": 100}}
        )
        is None
    )
