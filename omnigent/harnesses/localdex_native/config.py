"""Configuration for LocalDex's local provider inside a full Codex runtime.

LocalDex is not an authless replacement for Codex.  Its one installed binary
keeps the normal OpenAI/ChatGPT provider available and adds one explicitly
configured OpenAI-compatible provider.  The local provider is selected only
for its registered model; every other model continues through Codex's built-in
``openai`` provider and its normal ``auth.json`` login lifecycle.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import tomllib

LOCALDEX_CONFIG_ROOT = Path.home() / ".local" / "share" / "localdex"
LOCALDEX_CONFIG_PATH = LOCALDEX_CONFIG_ROOT / "config.toml"
LOCALDEX_BINARY = Path.home() / ".local" / "bin" / "localdex"
LOCALDEX_MODEL = "QB/DSV4.1-Flash"


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
    parsed = urlparse(base_url) if isinstance(base_url, str) else None
    if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
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
