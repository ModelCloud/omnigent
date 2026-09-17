"""Validated, local-only configuration for the LocalDex native harness."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import tomllib

LOCALDEX_CONFIG_ROOT = Path.home() / ".local" / "share" / "localdex"
LOCALDEX_CONFIG_PATH = LOCALDEX_CONFIG_ROOT / "config.toml"
LOCALDEX_BINARY = Path.home() / ".local" / "bin" / "localdex"


@dataclass(frozen=True)
class LocalDexConfig:
    """The complete provider contract allowed for LocalDex."""

    model: str
    provider: str
    base_url: str
    env_key: str


def load_localdex_config(path: Path = LOCALDEX_CONFIG_PATH) -> LocalDexConfig:
    """Read and strictly validate the dedicated LocalDex provider config."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"LocalDex config is missing: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"LocalDex config is invalid TOML: {exc}") from exc

    model = document.get("model")
    provider_name = document.get("model_provider")
    providers = document.get("model_providers")
    provider = providers.get(provider_name) if isinstance(providers, dict) else None
    if not isinstance(model, str) or not model.strip():
        raise ValueError("LocalDex config requires a non-empty model")
    if not isinstance(provider_name, str) or not isinstance(provider, dict):
        raise ValueError("LocalDex config requires exactly one selected model provider")
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
    if os.environ.get(env_key) is None:
        raise ValueError(f"LocalDex bearer environment variable {env_key!r} is not set")
    return LocalDexConfig(
        model=model.strip(), provider=provider_name, base_url=base_url.rstrip("/"), env_key=env_key
    )
