"""Configuration for LocalDex's local provider inside a full Codex runtime.

LocalDex is not an authless replacement for Codex. Its one installed binary
keeps the normal OpenAI/ChatGPT provider available and adds explicitly
configured OpenAI-compatible providers. Each custom model is routed only to
its declared provider; every other model keeps its normal Codex provider and
authentication lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
import tomllib

LOCALDEX_CONFIG_ROOT = Path.home() / ".local" / "share" / "localdex"
LOCALDEX_CONFIG_PATH = LOCALDEX_CONFIG_ROOT / "config.toml"
LOCALDEX_MODELS_PATH = LOCALDEX_CONFIG_ROOT / "models.toml"
LOCALDEX_BINARY = Path.home() / ".local" / "bin" / "codex"
LOCALDEX_MODEL = "QB/DSV4.1-Flash"
LOCALDEX_RUNTIME_CAPABILITIES_FILE = "localdex-runtime-capabilities.json"


def localdex_model_picker_row(
    config: LocalDexConfig, model_registration: LocalDexModelRegistration | None = None
) -> dict[str, object]:
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
    model = model_registration or next(
        (item for item in localdex_models(config) if item.model == config.local_model), None
    )
    if model is None:
        return {
            "id": config.local_model,
            "model": config.local_model,
            "displayName": config.local_model,
        }
    row: dict[str, object] = {
        "id": model.model,
        "model": model.model,
        "displayName": model.display_name,
    }
    if model.model != LOCALDEX_MODEL:
        return row
    row.update(
        {
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
    )
    return row


def with_localdex_model_picker_row(
    rows: Sequence[Mapping[str, object]], config: LocalDexConfig
) -> list[dict[str, object]]:
    """Insert the canonical LocalDex row once, preserving a selected default.

    A live app-server row or a previously written host catalog can already
    contain the local model. Replace it with the capability-complete row so
    stale id-only rows cannot suppress the effort UI, and remove duplicates
    before returning the shared picker catalog.
    """
    registered_rows: list[dict[str, object]] = []
    models = localdex_models(config)
    registered_ids = {item.model for item in models}
    for item in models:
        row: dict[str, object] = {
            "id": item.model,
            "model": item.model,
            "displayName": item.display_name,
        }
        if item.model == LOCALDEX_MODEL:
            row = localdex_model_picker_row(config, item)
        if any(
            source.get("isDefault") is True
            and (source.get("id") == item.model or source.get("model") == item.model)
            for source in rows
        ):
            row["isDefault"] = True
        registered_rows.append(row)
    return [
        *registered_rows,
        *[
            dict(row)
            for row in rows
            if row.get("id") not in registered_ids and row.get("model") not in registered_ids
        ],
    ]


@dataclass(frozen=True)
class LocalDexModelRegistration:
    """One explicitly mapped custom model and its provider credential."""

    model: str
    provider: str
    display_name: str
    base_url: str
    env_key: str
    discover_capabilities: bool = False
    provider_config: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LocalDexConfig:
    """Named OpenAI-compatible providers and models added to Codex."""

    local_model: str
    provider: str
    base_url: str
    env_key: str
    models: tuple[LocalDexModelRegistration, ...] = ()


def localdex_models(config: LocalDexConfig) -> tuple[LocalDexModelRegistration, ...]:
    """Return configured models, adapting old one-model registrations."""
    configured = getattr(config, "models", ())
    if configured:
        return configured
    return (
        LocalDexModelRegistration(
            model=config.local_model,
            provider=getattr(config, "provider", "localdex"),
            display_name=(
                "DeepSeek V4.1 Flash"
                if config.local_model == LOCALDEX_MODEL
                else config.local_model
            ),
            base_url=getattr(config, "base_url", ""),
            env_key=config.env_key,
            discover_capabilities=config.local_model == LOCALDEX_MODEL,
        ),
    )


def localdex_runtime_provider_id(provider: str) -> str:
    """Map a LocalDex config name into a private Codex provider namespace."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", provider).strip("-_")[:24] or "provider"
    digest = hashlib.sha256(provider.encode("utf-8")).hexdigest()[:10]
    return f"omnigent-localdex-{slug}-{digest}"


def localdex_provider_config_overrides(config: LocalDexConfig) -> tuple[str, ...]:
    """Serialize declared providers for the session-private Codex config.

    The provider tables are merged into the user's normal Codex config so a
    running thread can switch in either direction. Runtime IDs are namespaced
    to prevent a LocalDex provider named ``openai`` or ``gateway`` from
    replacing the user's official or gateway provider.
    """
    import tomlkit

    providers: dict[str, dict[str, object]] = {}
    for model in localdex_models(config):
        runtime_id = localdex_runtime_provider_id(model.provider)
        provider_config = dict(model.provider_config)
        provider_config.setdefault("name", model.provider)
        provider_config.update(
            {
                "base_url": model.base_url,
                "env_key": model.env_key,
                "wire_api": "responses",
                "requires_openai_auth": False,
            }
        )
        if model.provider == "localdex":
            provider_config.setdefault("supports_responses_continuation", True)
        existing = providers.get(runtime_id)
        if existing is not None and existing != provider_config:
            raise ValueError(f"LocalDex provider {model.provider!r} has inconsistent settings")
        providers[runtime_id] = provider_config

    document = tomlkit.document()
    document["model_providers"] = providers
    return (tomlkit.dumps(document),)


@dataclass(frozen=True)
class LocalDexRuntimeCapabilities:
    """Live context limits advertised by the selected LocalDex model."""

    context_window: int
    max_prompt_tokens: int | None


def load_localdex_config(
    path: Path = LOCALDEX_CONFIG_PATH,
    *,
    models_path: Path = LOCALDEX_MODELS_PATH,
    require_token: bool = True,
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
    if not isinstance(providers, dict):
        raise ValueError("LocalDex config requires [model_providers.<provider>]")
    try:
        model_document = tomllib.loads(models_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Read pre-migration installations without changing their user data.
        model_document = {"models": {LOCALDEX_MODEL: {"provider": "localdex"}}}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"LocalDex model registry is invalid TOML: {exc}") from exc
    declared_models = model_document.get("models")
    if not isinstance(declared_models, dict) or not declared_models:
        raise ValueError("LocalDex models.toml requires [models.<model-id>] entries")

    registered: list[LocalDexModelRegistration] = []
    for model_id, model in declared_models.items():
        if not isinstance(model_id, str) or not model_id.strip() or not isinstance(model, dict):
            raise ValueError("LocalDex model entries must be named tables")
        provider_name = model.get("provider")
        provider = providers.get(provider_name) if isinstance(provider_name, str) else None
        if not isinstance(provider, dict):
            raise ValueError(f"LocalDex model {model_id!r} references an unknown provider")
        forbidden_provider_fields = {
            "api_key",
            "api_key_command",
            "auth",
            "auth_command",
            "authorization",
            "bearer_token",
            "headers",
            "http_headers",
            "password",
            "secret",
            "token",
        }
        if forbidden_provider_fields.intersection(key.lower() for key in provider):
            raise ValueError(
                f"LocalDex provider {provider_name!r} must use env_key, not inline credentials"
            )
        base_url, env_key = provider.get("base_url"), provider.get("env_key")
        if not isinstance(base_url, str):
            raise ValueError(f"LocalDex provider {provider_name!r} base_url must be HTTP(S)")
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"LocalDex provider {provider_name!r} base_url must be HTTP(S)")
        if not isinstance(provider_name, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", provider_name
        ):
            raise ValueError(f"LocalDex model {model_id!r} has an invalid provider name")
        if not isinstance(env_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key):
            raise ValueError(f"LocalDex provider {provider_name!r} env_key is invalid")
        if env_key.startswith(("OPENAI_", "DATABRICKS_")):
            raise ValueError(
                f"LocalDex provider {provider_name!r} must use a provider-specific env_key"
            )
        if provider.get("wire_api") != "responses":
            raise ValueError(
                f'LocalDex provider {provider_name!r} must use wire_api = "responses"'
            )
        if provider.get("requires_openai_auth") is not False:
            raise ValueError(
                f"LocalDex provider {provider_name!r} must set requires_openai_auth = false"
            )
        if require_token and os.environ.get(env_key) is None:
            raise ValueError(f"LocalDex bearer environment variable {env_key!r} is not set")
        registered.append(
            LocalDexModelRegistration(
                model=model_id,
                provider=provider_name,
                display_name=str(model.get("display_name") or model_id),
                base_url=base_url.rstrip("/"),
                env_key=env_key,
                discover_capabilities=model.get("discover_capabilities") is True,
                provider_config=dict(provider),
            )
        )
    local = next((item for item in registered if item.model == LOCALDEX_MODEL), registered[0])
    return LocalDexConfig(
        local_model=local.model,
        provider=local.provider,
        base_url=local.base_url,
        env_key=local.env_key,
        models=tuple(registered),
    )


def localdex_model_selected(config: LocalDexConfig, model: str | None) -> bool:
    """Whether ``model`` must route through the configured local provider."""
    return localdex_model_for_selection(config, model) is not None


def localdex_model_for_selection(
    config: LocalDexConfig, model: str | None
) -> LocalDexModelRegistration | None:
    """Return the explicit provider mapping for one selected model."""
    if model is None:
        return None
    selected = model.strip()
    registered = next((entry for entry in config.models if entry.model == selected), None)
    if registered is not None:
        return registered
    if not config.models and selected == config.local_model:
        return LocalDexModelRegistration(
            model=config.local_model,
            provider=config.provider,
            display_name="DeepSeek V4.1 Flash"
            if config.local_model == LOCALDEX_MODEL
            else config.local_model,
            base_url=config.base_url,
            env_key=config.env_key,
            discover_capabilities=config.local_model == LOCALDEX_MODEL,
        )
    return None


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
