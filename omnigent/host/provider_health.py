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


async def _probe_one(base_url: str) -> bool:
    """Return True when the endpoint answers an HTTP request at all.

    Any status code counts as online — a 401/404 still proves the service is
    reachable; only transport failures (DNS, refused, TLS, timeout) are
    offline.
    """
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S, follow_redirects=True) as client:
            await client.get(base_url)
    except httpx.HTTPError:
        return False
    except Exception:  # noqa: BLE001 - a probe must never break the catalog answer
        return False
    return True


async def probe_provider_health(
    providers: dict[str, str],
    *,
    cache: dict[str, tuple[float, bool]] | None = None,
    now: float | None = None,
) -> dict[str, str]:
    """Map ``provider_id -> "online" | "offline"`` for each ``base_url``.

    :param providers: Provider id to endpoint base_url.
    :param cache: Per-process TTL cache; misses and entries older than
        :data:`PROVIDER_HEALTH_TTL_S` are probed concurrently. Passing
        ``None`` probes every entry.
    :param now: Clock override for tests.
    """
    now = time.monotonic() if now is None else now
    pending: dict[str, str] = {}
    health: dict[str, str] = {}
    for provider_id, base_url in providers.items():
        if not base_url:
            continue
        if cache is not None:
            entry = cache.get(provider_id)
            if entry is not None and now - entry[0] < PROVIDER_HEALTH_TTL_S:
                health[provider_id] = "online" if entry[1] else "offline"
                continue
        pending[provider_id] = base_url
    if pending:
        results = await asyncio.gather(*(_probe_one(url) for url in pending.values()))
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


def _provider_base_urls(referenced: set[str]) -> dict[str, str]:
    """Resolve each referenced provider id to its configured endpoint.

    MultiDex registrations carry their own ``base_url``; the remaining ids
    fall back to the shared ``config.toml`` provider table, then to the
    compiled-in gateway defaults.
    """
    from omnigent.harnesses.codex_native.app_server import _config_toml_provider_base_url
    from omnigent.harnesses.multidex_native.config import (
        load_multidex_config,
        multidex_models,
    )

    base_urls: dict[str, str] = {}
    try:
        multidex = load_multidex_config(require_token=False)
    except (FileNotFoundError, ValueError):
        multidex = None
    if multidex is not None:
        for item in multidex_models(multidex):
            if item.provider in referenced and item.base_url:
                base_urls[item.provider] = item.base_url
    for provider_id in referenced - base_urls.keys():
        url = _config_toml_provider_base_url(provider_id)
        if url is None:
            url = BUILTIN_PROVIDER_BASE_URLS.get(provider_id)
        if url:
            base_urls[provider_id] = url
    return base_urls


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
    base_urls = await asyncio.to_thread(_provider_base_urls, referenced)
    if not base_urls:
        return
    health = await probe_provider_health(
        base_urls, cache=_SHARED_CACHE if cache is None else cache
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = health.get(str(row.get("providerId") or "openai"))
        if status:
            row["providerHealth"] = status
