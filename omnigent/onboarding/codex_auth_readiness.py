"""Read Codex's effective provider auth without spawning the Codex CLI.

The readiness verdict mirrors Codex's ``auth.credentials`` check: a provider
that requires OpenAI auth uses ``auth.json``; a provider with ``env_key`` is
never ready because provider credentials come only from config.toml (the
launch environment forwards none of them); and any other non-OpenAI provider
handles auth itself. This deliberately remains a local, structural check. It
may be wrong about token validity, but it is cheap enough for the host's
periodic readiness refresh and never probes the network.

This module also owns the shared ``config.toml`` parsing primitives used by
ambient provider adoption. Adoption and readiness ask different questions:
adoption requires self-contained auth, while readiness asks how Codex itself
would authenticate the selected provider.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

CODEX_BUILTIN_PROVIDERS = frozenset(
    {
        "openai",
        "amazon-bedrock",
        "amazon-bedrock-runtime",
        "ollama",
        "lmstudio",
        "openrouter",
        "lithos",
    }
)
_CODEX_AUTH_HEADER_NAMES = frozenset({"authorization", "api-key", "x-api-key"})

CodexConfigAuth = Literal["provider-ready", "provider-auth-missing", "codex-login"]


def load_codex_config(config_path: Path) -> dict[str, object] | None:
    """Parse a Codex ``config.toml``, returning ``None`` when unusable."""
    try:
        raw = config_path.read_bytes()
    except OSError:
        return None
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None


def effective_codex_model_provider(config: dict[str, object]) -> str | None:
    """Resolve Codex's effective provider (active profile before top-level)."""
    provider_id: object = config.get("model_provider")
    active_profile = config.get("profile")
    if isinstance(active_profile, str) and active_profile.strip():
        profiles = config.get("profiles")
        if isinstance(profiles, dict):
            profile_table = profiles.get(active_profile)
            if isinstance(profile_table, dict) and isinstance(
                profile_table.get("model_provider"), str
            ):
                provider_id = profile_table["model_provider"]
    if isinstance(provider_id, str) and provider_id.strip():
        return provider_id
    return None


def effective_custom_provider_table(
    config: dict[str, object],
) -> tuple[str, dict[str, object]] | None:
    """Return the effective custom provider id and table, when both exist."""
    provider_id = effective_codex_model_provider(config)
    if provider_id is None or provider_id in CODEX_BUILTIN_PROVIDERS:
        return None
    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        return None
    table = providers.get(provider_id)
    if not isinstance(table, dict):
        return None
    return provider_id, table


def effective_self_sufficient_builtin_provider(config: dict[str, object]) -> str | None:
    """Return the selected built-in provider id when Codex needs no login for it.

    Every built-in provider except ``openai`` (Bedrock, Ollama, LM Studio,
    OpenRouter, Lithos) authenticates itself, mirroring
    :func:`codex_config_effective_auth`. The gateway providers need a
    configured credential: with no ``experimental_bearer_token`` (or other
    self-contained auth) in their table they serve no authenticated requests.
    """
    provider_id = effective_codex_model_provider(config)
    if provider_id not in CODEX_BUILTIN_PROVIDERS or provider_id == "openai":
        return None
    if provider_id in {"openrouter", "lithos"}:
        providers = config.get("model_providers")
        table = providers.get(provider_id) if isinstance(providers, dict) else None
        if not isinstance(table, dict) or not provider_table_has_self_contained_auth(table):
            return None
    return provider_id


def provider_table_has_self_contained_auth(table: dict[str, object]) -> bool:
    """Return whether a custom provider table carries adoption-safe auth.

    Env-based auth is intentionally excluded: adopting such a provider would
    require forwarding an arbitrary variable through the scrubbed Codex launch
    environment. Readiness handles ``env_key`` separately because Codex keeps
    its own provider config on that path.
    """
    auth = table.get("auth")
    if isinstance(auth, dict):
        command = auth.get("command")
        if isinstance(command, str) and command.strip():
            return True
    bearer = table.get("experimental_bearer_token")
    if isinstance(bearer, str) and bearer.strip():
        return True
    if isinstance(table.get("aws"), dict):
        return True
    headers = table.get("http_headers")
    if isinstance(headers, dict):
        return any(
            isinstance(key, str)
            and key.lower() in _CODEX_AUTH_HEADER_NAMES
            and isinstance(value, str)
            and value.strip()
            for key, value in headers.items()
        )
    return False


def codex_config_effective_auth(
    config_path: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> CodexConfigAuth:
    """Return how Codex authenticates its effective provider.

    Mirrors Codex's provider-specific auth check:

    * unset / built-in ``openai`` / ``requires_openai_auth = true`` →
      ``"codex-login"`` (inspect ``auth.json``);
    * non-OpenAI provider declaring ``env_key`` → ``"provider-auth-missing"``:
      credential env vars are never forwarded to the Codex launch
      environment, so the variable cannot be populated there;
    * any other non-OpenAI provider → ``"provider-ready"`` because Codex
      considers provider-specific auth sufficient or unnecessary.

    Built-in provider definitions in ``[model_providers]`` are ignored here,
    matching Codex's catalog merge, except OpenRouter/Lithos credential
    overrides and Bedrock's endpoint/auth overrides. For example, Ollama needs no
    credential; Bedrock may still fail later if its AWS credential chain is
    unavailable, which is intentionally outside this local status check.

    ``requires_openai_auth`` intentionally wins over ``env_key`` when both are
    set, matching Codex's readiness diagnostic for that contradictory config.

    Host-wide readiness has no agent spec, so it cannot include a particular
    spec's ``sandbox.env_passthrough`` additions; such a custom session may work
    while the host-level picker conservatively reports ``needs-auth``.
    """
    config = load_codex_config(config_path)
    if config is None:
        return "codex-login"

    provider_id = effective_codex_model_provider(config)
    if provider_id is None:
        return "codex-login"

    # Configured entries cannot replace the built-in OpenAI, Ollama, or LM
    # Studio providers. Bedrock accepts only limited endpoint/auth overrides;
    # none changes whether it requires Codex login.
    if provider_id in CODEX_BUILTIN_PROVIDERS and provider_id not in {"openrouter", "lithos"}:
        return "codex-login" if provider_id == "openai" else "provider-ready"

    providers = config.get("model_providers")
    configured_table = providers.get(provider_id) if isinstance(providers, dict) else None
    table = configured_table if isinstance(configured_table, dict) else None
    if provider_id in {"openrouter", "lithos"}:
        table = dict(table or {})
        if provider_table_has_self_contained_auth(table):
            return "provider-ready"
        # The gateway built-ins always need a credential, and only config.toml
        # auth counts: nothing forwards env_key values into the launch env.
        return "provider-auth-missing"
    if table is None:
        # A selected custom provider without a table is invalid. Do not let an
        # unrelated auth.json credential mask the broken selection.
        return "provider-auth-missing"

    if table.get("requires_openai_auth") is True:
        return "codex-login"

    if provider_table_has_self_contained_auth(table):
        return "provider-ready"
    env_key = table.get("env_key")
    if isinstance(env_key, str) and env_key.strip():
        # Credential env vars are never forwarded to the Codex launch
        # environment; only config.toml credentials (bearer token, auth
        # command, auth headers) authenticate providers.
        return "provider-auth-missing"
    return "provider-ready"
