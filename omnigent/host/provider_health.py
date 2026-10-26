"""JIT provider endpoint liveness for the model picker.

The pre-launch catalog answers which providers a model family routes to; a
reachability ping tells the picker which of those endpoints are actually up
right now so a gray dot flags a dead gateway before a user selects it.
"""

from __future__ import annotations

import asyncio
import time

import httpx

# Pickers refetch the catalog on a poll loop; pings are bounded per provider so
# repeated catalog answers never turn into repeated requests to a dead
# endpoint. Results younger than this serve straight from cache.
PROVIDER_HEALTH_TTL_S = 5.0

_PROBE_TIMEOUT_S = 2.0


async def _probe_one(base_url: str, token: str | None) -> bool:
    """Return True when ``GET {base_url}/models`` succeeds.

    The probe always authenticates with the provider's configured bearer
    token when one exists, and then requires a 2xx — a 401/403 flags a wrong
    or expired credential and counts offline. Providers with no configured
    credential (e.g. ``openai``, authenticated via Codex login rather than a
    bearer) fall back to a reachability check: any HTTP response below 5xx
    means the gateway answers; 5xx and transport failures (DNS, refused,
    TLS, timeout) count offline either way.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S, follow_redirects=True) as client:
            response = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
    except httpx.HTTPError:
        return False
    except Exception:  # noqa: BLE001 - a probe must never break the catalog answer
        return False
    if token:
        return 200 <= response.status_code < 300
    return response.status_code < 500


async def probe_provider_health(
    providers: dict[str, tuple[str, str | None]],
    *,
    cache: dict[str, tuple[float, bool]] | None = None,
    now: float | None = None,
) -> dict[str, str]:
    """Map ``provider_id -> "online" | "offline"`` for each endpoint.

    :param providers: Provider id to ``(base_url, bearer_token_or_None)``.
    :param cache: Per-process TTL cache; misses and entries older than
        :data:`PROVIDER_HEALTH_TTL_S` are probed concurrently. Passing
        ``None`` probes every entry.
    :param now: Clock override for tests.
    """
    now = time.monotonic() if now is None else now
    pending: dict[str, tuple[str, str | None]] = {}
    health: dict[str, str] = {}
    for provider_id, endpoint in providers.items():
        base_url = endpoint[0]
        if not base_url:
            continue
        if cache is not None:
            entry = cache.get(provider_id)
            if entry is not None and now - entry[0] < PROVIDER_HEALTH_TTL_S:
                health[provider_id] = "online" if entry[1] else "offline"
                continue
        pending[provider_id] = endpoint
    if pending:
        results = await asyncio.gather(
            *(_probe_one(url, token) for url, token in pending.values())
        )
        for provider_id, up in zip(pending, results, strict=True):
            if cache is not None:
                cache[provider_id] = (now, up)
            health[provider_id] = "online" if up else "offline"
    return health


# Gateway defaults when a provider table has no explicit base_url — the same
# endpoints MultiDex's built-ins compile in.
BUILTIN_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "lithos": "https://api.lithosai.cloud/v1",
}

# Host and runner are separate processes; one TTL cache per process still
# bounds pings since pickers poll within one daemon lifetime.
_SHARED_CACHE: dict[str, tuple[float, bool]] = {}


# Relative serving speed per provider/variant combo; local routing is the 1.0
# baseline. Lithos exposes three native variants whose pricing maps onto
# speed; providers without variants carry the flat baseline. Stamp it on the
# row so the picker can flag fast lanes at both the provider and variant step.
PROVIDER_SPEEDS = {"openrouter": 1.25}
LITHOS_VARIANT_SPEEDS = {"regular": 1.25, "fast": 1.5, "ultra": 2.0}


def annotate_provider_speed(rows: list[dict[str, object]]) -> None:
    """Set ``speed`` (relative endpoint speed, 1.0 = local baseline) on each row."""
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = str(row.get("providerId") or "openai")
        variant = row.get("variant")
        if provider == "lithos":
            row["speed"] = LITHOS_VARIANT_SPEEDS.get(
                str(variant), PROVIDER_SPEEDS.get(provider, 1.0)
            )
        else:
            row["speed"] = PROVIDER_SPEEDS.get(provider, 1.0)


def _provider_endpoints(referenced: set[str]) -> dict[str, tuple[str, str | None]]:
    """Resolve each referenced provider id to ``(base_url, bearer_token)``.

    MultiDex registrations carry both on the model entry; the remaining ids
    fall back to the shared ``config.toml`` provider table, then to the
    compiled-in gateway defaults (no token).
    """
    from omnigent.harnesses.multidex_native.config import (
        load_multidex_config,
        multidex_models,
        multidex_registration_bearer_token,
    )

    endpoints: dict[str, tuple[str, str | None]] = {}
    try:
        multidex = load_multidex_config(require_token=False)
    except (FileNotFoundError, ValueError):
        multidex = None
    if multidex is not None:
        for item in multidex_models(multidex):
            if item.provider in referenced and item.base_url:
                endpoints[item.provider] = (
                    item.base_url,
                    multidex_registration_bearer_token(item),
                )
    for provider_id in referenced - endpoints.keys():
        url, token = _config_toml_provider_endpoint(provider_id)
        if url is None:
            url = BUILTIN_PROVIDER_BASE_URLS.get(provider_id)
        if url:
            endpoints[provider_id] = (url, token)
    return endpoints


def _config_toml_provider_endpoint(
    provider_name: str,
) -> tuple[str | None, str | None]:
    """Read ``base_url`` + ``experimental_bearer_token`` for a config.toml provider."""
    import tomllib

    from omnigent.inner.codex_executor import _codex_home_config_source_from_env

    config_path = _codex_home_config_source_from_env() / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        table = data["model_providers"][provider_name]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return None, None
    if not isinstance(table, dict):
        return None, None
    base_url = table.get("base_url")
    token = table.get("experimental_bearer_token")
    return (
        base_url if isinstance(base_url, str) else None,
        token if isinstance(token, str) else None,
    )


async def annotate_provider_health(
    rows: list[dict[str, object]],
    *,
    cache: dict[str, tuple[float, bool]] | None = None,
) -> None:
    """Set ``providerHealth`` on each picker row from a cached endpoint ping.

    ``providerId`` names the gateway a row routes through (``openai`` when
    absent — the codex catalog's implicit provider). The JIT probe gives the
    picker a liveness prefix without slowing the catalog answer: pings are
    concurrent, capped at ~2s each, and TTL-cached so repeated catalog
    refetches never re-ping a recently probed endpoint.
    """
    referenced = {str(row.get("providerId") or "openai") for row in rows if isinstance(row, dict)}
    if not referenced:
        return
    endpoints = await asyncio.to_thread(_provider_endpoints, referenced)
    if not endpoints:
        return
    health = await probe_provider_health(
        endpoints, cache=_SHARED_CACHE if cache is None else cache
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = health.get(str(row.get("providerId") or "openai"))
        if status:
            row["providerHealth"] = status
