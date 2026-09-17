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
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import tomllib

LOCALDEX_CONFIG_ROOT = Path.home() / ".local" / "share" / "localdex"
LOCALDEX_CONFIG_PATH = LOCALDEX_CONFIG_ROOT / "config.toml"
LOCALDEX_BINARY = Path.home() / ".local" / "bin" / "localdex"
LOCALDEX_MODEL = "QB/DSV4.1-Flash"
_METADATA_TIMEOUT_SECONDS = 3.0
_DEFAULT_CAPABILITIES: dict[str, object] = {
    "context_window": 524_288,
    "max_prompt_tokens": 524_286,
    "reasoning": {"enabled": True, "summary": True},
    "tools": {"function": True, "custom": True, "namespace": False},
}


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


def localdex_model_catalog(config: LocalDexConfig) -> dict[str, object]:
    """Build Codex model metadata from the local server's advertised contract.

    ``/v1/models`` is an OpenAI-compatible discovery endpoint with an
    Inference-Ultra ``capabilities`` extension.  Older deployments return the
    standard minimal model object, so retain a conservative known-good
    descriptor while they roll forward.
    """
    capabilities = _localdex_capabilities(config)
    context_window = _positive_int(capabilities.get("context_window"), 524_288)
    max_prompt_tokens = _positive_int(
        capabilities.get("max_prompt_tokens"), context_window - 1
    )
    context_window = max(context_window, max_prompt_tokens + 1)
    reasoning = capabilities.get("reasoning")
    reasoning_summary = isinstance(reasoning, dict) and reasoning.get("summary") is True
    tools = capabilities.get("tools")
    custom_tools = isinstance(tools, dict) and tools.get("custom") is True
    return {
        "models": [
            {
                "slug": config.local_model,
                "display_name": "DeepSeek V4.1 Flash",
                "description": "Local DeepSeek V4.1 Flash via Inference-Ultra.",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [
                    {"effort": "low", "description": "Fast, lighter reasoning"},
                    {"effort": "medium", "description": "Balanced reasoning"},
                    {"effort": "high", "description": "Deeper reasoning"},
                ],
                "shell_type": "unified_exec",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 1,
                "availability_nux": None,
                "upgrade": None,
                "model_messages": None,
                "include_skills_usage_instructions": True,
                "include_plugin_usage_instructions": True,
                "include_apps_usage_instructions": True,
                "supports_reasoning_summary_parameter": reasoning_summary,
                "default_reasoning_summary": "auto" if reasoning_summary else "none",
                "support_verbosity": False,
                "default_verbosity": None,
                "apply_patch_tool_type": "freeform" if custom_tools else None,
                "web_search_tool_type": "text",
                "truncation_policy": {"mode": "tokens", "limit": 10_000},
                "supports_image_detail_original": False,
                "context_window": context_window,
                "max_context_window": context_window,
                "auto_compact_token_limit": context_window * 9 // 10,
                "comp_hash": None,
                "effective_context_window_percent": 95,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
                "supports_search_tool": False,
                "supports_experimental_context": False,
                "use_responses_lite": False,
                "node_repl_auto_review_required": False,
                "node_repl_disabled": False,
                "auto_review_model_override": None,
                "model_specialty": None,
                "tool_mode": "direct",
                "multi_agent_version": None,
                "multi_agent_reasoning_effort": None,
            }
        ]
    }


def _localdex_capabilities(config: LocalDexConfig) -> dict[str, object]:
    """Fetch the optional Inference-Ultra model-capability extension."""
    fallback = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in _DEFAULT_CAPABILITIES.items()
    }
    token = os.environ.get(config.env_key)
    if token is None:
        return fallback
    endpoint = f"{config.base_url}/models/{quote(config.local_model, safe='')}"
    request = Request(endpoint, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=_METADATA_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (OSError, ValueError, json.JSONDecodeError):
        return fallback
    capabilities = payload.get("capabilities") if isinstance(payload, dict) else None
    if not isinstance(capabilities, dict):
        return fallback
    for key, value in capabilities.items():
        if isinstance(value, dict) and isinstance(fallback.get(key), dict):
            fallback[key].update(value)  # type: ignore[union-attr]
        else:
            fallback[key] = value
    return fallback


def _positive_int(value: object, default: int) -> int:
    """Return a positive integer capability, otherwise a safe default."""
    return value if isinstance(value, int) and value > 0 else default
