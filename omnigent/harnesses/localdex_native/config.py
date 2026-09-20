"""Configuration for LocalDex's local provider inside a full Codex runtime.

LocalDex is not an authless replacement for Codex.  Its one installed binary
keeps the normal OpenAI/ChatGPT provider available and adds one explicitly
configured OpenAI-compatible provider.  The local provider is selected only
for its registered model; every other model continues through Codex's built-in
``openai`` provider and its normal ``auth.json`` login lifecycle.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
import tomllib

LOCALDEX_CONFIG_ROOT = Path.home() / ".local" / "share" / "localdex"
LOCALDEX_CONFIG_PATH = LOCALDEX_CONFIG_ROOT / "config.toml"
LOCALDEX_BINARY = Path.home() / ".local" / "bin" / "localdex"
LOCALDEX_MODEL = "QB/DSV4.1-Flash"
LOCALDEX_RUNTIME_CAPABILITIES_FILE = "localdex-runtime-capabilities.json"


def localdex_model_picker_row(config: LocalDexConfig) -> dict[str, object]:
    """Return the capability-complete picker row for LocalDex's local model.

    The upstream Codex account catalog does not include a bearer-authenticated
    custom provider. Omnigent therefore adds this row beside the account rows.
    Keep the LocalDex model's supported effort ladder here rather than
    fabricating an id-only row: the web client intentionally hides its effort
    control when a model has no ``supportedReasoningEfforts`` metadata.

    This mirrors LocalDex's bundled ``ModelInfo`` for DSV4.1 Flash. The model
    accepts ``off``, ``low``, ``high``, and ``max``; in particular, do not
    offer Codex's generic ``medium`` value because the endpoint rejects it.
    ``off`` is intentionally explicit and unambiguous; the inference endpoint
    also accepts Responses' legacy ``none`` alias for compatibility.
    """
    return {
        "id": config.local_model,
        "model": config.local_model,
        "displayName": "DeepSeek V4.1 Flash",
        "defaultReasoningEffort": "high",
        "supportedReasoningEfforts": [
            {
                "reasoningEffort": "off",
                "description": "Disable thinking for the fastest responses",
            },
            {
                "reasoningEffort": "low",
                "description": "Fast responses with lighter reasoning",
            },
            {
                "reasoningEffort": "high",
                "description": "Greater reasoning depth for complex work",
            },
            {
                "reasoningEffort": "max",
                "description": "Maximum reasoning depth for difficult work",
            },
        ],
    }


def with_localdex_model_picker_row(
    rows: Sequence[Mapping[str, object]], config: LocalDexConfig
) -> list[dict[str, object]]:
    """Insert the canonical LocalDex row once, preserving a selected default.

    A live app-server row or a previously written host catalog can already
    contain the local model. Replace it with the capability-complete row so
    stale id-only rows cannot suppress the effort UI, and remove duplicates
    before returning the shared picker catalog.
    """
    local_row = localdex_model_picker_row(config)
    matching_rows = [
        row
        for row in rows
        if row.get("id") == config.local_model or row.get("model") == config.local_model
    ]
    if any(row.get("isDefault") is True for row in matching_rows):
        local_row["isDefault"] = True
    return [
        local_row,
        *[
            dict(row)
            for row in rows
            if row.get("id") != config.local_model and row.get("model") != config.local_model
        ],
    ]


@dataclass(frozen=True)
class LocalDexConfig:
    """The local-provider contract registered beside the built-in provider."""

    local_model: str
    provider: str
    base_url: str
    env_key: str


@dataclass(frozen=True)
class LocalDexRuntimeCapabilities:
    """Live context limits advertised by the selected LocalDex model."""

    context_window: int
    max_prompt_tokens: int | None


def load_localdex_config(
    path: Path = LOCALDEX_CONFIG_PATH, *, require_token: bool = True
) -> LocalDexConfig:
    """Read and validate LocalDex's additive local-provider registration.

    ``config.toml`` may select either ``openai`` or the local provider as its
    default.  Both provider definitions remain in the same file so a resumed
    conversation can switch models without losing the LocalDex login state.
    """
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"LocalDex config is missing: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"LocalDex config is invalid TOML: {exc}") from exc

    providers = document.get("model_providers")
    provider_name = "localdex"
    provider = providers.get(provider_name) if isinstance(providers, dict) else None
    # The current registration is intentionally one model.  Keeping it as a
    # harness-owned constant avoids adding LocalDex-only keys to Codex's strict
    # config schema.  The value remains part of the public model picker.
    local_model = LOCALDEX_MODEL
    if not isinstance(provider, dict):
        raise ValueError("LocalDex config requires [model_providers.localdex]")
    base_url, env_key = provider.get("base_url"), provider.get("env_key")
    if not isinstance(base_url, str):
        raise ValueError("LocalDex provider base_url must be an absolute HTTP(S) URL")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("LocalDex provider base_url must be an absolute HTTP(S) URL")
    if not isinstance(env_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key):
        raise ValueError("LocalDex provider env_key is invalid")
    if provider.get("wire_api") != "responses":
        raise ValueError('LocalDex provider must use wire_api = "responses"')
    if provider.get("requires_openai_auth") is not False:
        raise ValueError("LocalDex provider must set requires_openai_auth = false")
    if require_token and os.environ.get(env_key) is None:
        raise ValueError(f"LocalDex bearer environment variable {env_key!r} is not set")
    return LocalDexConfig(
        local_model=local_model,
        provider=provider_name,
        base_url=base_url.rstrip("/"),
        env_key=env_key,
    )


def localdex_model_selected(config: LocalDexConfig, model: str | None) -> bool:
    """Whether ``model`` must route through the configured local provider."""
    return model is not None and model.strip() == config.local_model


async def fetch_localdex_runtime_capabilities(
    config: LocalDexConfig,
) -> LocalDexRuntimeCapabilities:
    """Read the local model's live context budget from its provider.

    The endpoint is authoritative: a deployment can reduce its capacity while
    a Codex thread is still alive.  Do not reuse picker or bundled metadata
    for turn admission.  The process is already credential-isolated by the
    LocalDex launcher; this request deliberately sends only its configured
    bearer token and ignores ambient proxy/credential environment settings.
    """
    token = os.environ.get(config.env_key)
    if not token:
        raise ValueError(f"LocalDex bearer environment variable {config.env_key!r} is not set")
    model_url = f"{config.base_url}/models/{quote(config.local_model, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(
                model_url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            response.raise_for_status()
            document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("LocalDex model capability discovery failed") from exc

    capabilities = document.get("capabilities") if isinstance(document, dict) else None
    if not isinstance(capabilities, dict):
        raise RuntimeError("LocalDex model capability response has no capabilities object")
    context_window = capabilities.get("context_window")
    max_prompt_tokens = capabilities.get("max_prompt_tokens")
    if not isinstance(context_window, int) or context_window <= 0:
        raise RuntimeError("LocalDex model capability response has an invalid context_window")
    if not isinstance(max_prompt_tokens, int) or max_prompt_tokens <= 0:
        max_prompt_tokens = None
    return LocalDexRuntimeCapabilities(
        context_window=context_window,
        max_prompt_tokens=max_prompt_tokens,
    )


def write_localdex_runtime_capabilities(
    codex_home: Path,
    config: LocalDexConfig,
    capabilities: LocalDexRuntimeCapabilities,
) -> None:
    """Atomically publish one freshly-read local context budget to LocalDex.

    The file lives in the session-private ``CODEX_HOME`` shared by Omnigent's
    executor and app-server. LocalDex reads it for each new turn, rather than
    caching a deployment's capacity in the binary or global configuration.
    """
    codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = codex_home / LOCALDEX_RUNTIME_CAPABILITIES_FILE
    payload = {
        "model": config.local_model,
        "context_window": capabilities.context_window,
    }
    if capabilities.max_prompt_tokens is not None:
        payload["max_prompt_tokens"] = capabilities.max_prompt_tokens
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=codex_home,
        prefix=f".{LOCALDEX_RUNTIME_CAPABILITIES_FILE}.",
        delete=False,
    ) as temporary:
        os.chmod(temporary.name, 0o600)
        json.dump(payload, temporary, separators=(",", ":"))
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, destination)


def clear_localdex_runtime_capabilities(codex_home: Path) -> None:
    """Discard a stale discovery snapshot after an endpoint lookup failure."""
    with suppress(FileNotFoundError):
        (codex_home / LOCALDEX_RUNTIME_CAPABILITIES_FILE).unlink()
