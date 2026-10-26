"""Unit tests for the JIT provider liveness probe behind the model picker."""

from __future__ import annotations

import pytest

from omnigent.host.provider_health import (
    PROVIDER_HEALTH_TTL_S,
    annotate_provider_health,
    probe_provider_health,
)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _client(get):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url, headers=None):
            return await get(url, headers)

    return _Client


@pytest.mark.asyncio
async def test_probe_queries_models_route_with_auth_and_classifies_status(monkeypatch):
    seen: list[tuple[str, dict | None]] = []

    async def get(url, headers):
        seen.append((url, headers))
        if "dead" in url:
            raise TimeoutError("unreachable")
        if "broken" in url:
            return _Response(503)
        if "badkey" in url:
            return _Response(401)
        if "gated" in url:
            return _Response(401)  # auth-gated gateway; no token to send
        return _Response(200)

    monkeypatch.setattr(
        "omnigent.host.provider_health.httpx.AsyncClient", lambda **kw: _client(get)()
    )
    health = await probe_provider_health(
        {
            "up": ("https://openrouter.ai/api/v1", "or-tok"),
            "badkey": ("https://badkey.invalid/v1/", "wrong"),
            "gated": ("https://gated.invalid/v1", None),
            "broken": ("https://broken.invalid/v1", None),
            "dead": ("http://dead.invalid/v1", None),
        }
    )
    assert health == {
        "up": "online",
        "badkey": "offline",  # authenticated 401 = credential problem
        "gated": "online",  # no credential to send; 401 still proves reachability
        "broken": "offline",
        "dead": "offline",
    }
    assert seen == [
        ("https://openrouter.ai/api/v1/models", {"Authorization": "Bearer or-tok"}),
        ("https://badkey.invalid/v1/models", {"Authorization": "Bearer wrong"}),
        ("https://gated.invalid/v1/models", None),
        ("https://broken.invalid/v1/models", None),
        ("http://dead.invalid/v1/models", None),
    ]


@pytest.mark.asyncio
async def test_probe_serves_fresh_results_from_ttl_cache(monkeypatch):
    calls = []

    async def get(url, headers):
        calls.append(url)
        return _Response(200)

    monkeypatch.setattr(
        "omnigent.host.provider_health.httpx.AsyncClient", lambda **kw: _client(get)()
    )
    cache: dict[str, tuple[float, bool]] = {}
    providers = {"p": ("https://api.openai.com/v1", None)}
    assert (await probe_provider_health(providers, cache=cache)) == {"p": "online"}
    # Second call inside the TTL must not re-ping the endpoint.
    assert (await probe_provider_health(providers, cache=cache)) == {"p": "online"}
    assert calls == ["https://api.openai.com/v1/models"]


@pytest.mark.asyncio
async def test_probe_rechecks_after_ttl_expiry(monkeypatch):
    calls = []
    clock = {"now": 1000.0}

    async def get(url, headers):
        calls.append(url)
        return _Response(200)

    monkeypatch.setattr(
        "omnigent.host.provider_health.httpx.AsyncClient", lambda **kw: _client(get)()
    )
    monkeypatch.setattr("omnigent.host.provider_health.time.monotonic", lambda: clock["now"])
    cache: dict[str, tuple[float, bool]] = {}
    providers = {"p": ("https://api.openai.com/v1", None)}
    await probe_provider_health(providers, cache=cache)
    clock["now"] += PROVIDER_HEALTH_TTL_S + 0.1
    await probe_provider_health(providers, cache=cache)
    assert calls == ["https://api.openai.com/v1/models", "https://api.openai.com/v1/models"]


def test_annotate_provider_speed_stamps_relative_speed_per_combo():
    from omnigent.host.provider_health import annotate_provider_speed

    rows: list[dict[str, object]] = [
        {"id": "local", "providerId": "multidex", "variant": "regular"},
        {"id": "or", "providerId": "openrouter", "variant": "regular"},
        {"id": "l-reg", "providerId": "lithos", "variant": "regular"},
        {"id": "l-fast", "providerId": "lithos", "variant": "fast"},
        {"id": "l-ultra", "providerId": "lithos", "variant": "ultra"},
        {"id": "gpt"},  # implicit openai
    ]
    annotate_provider_speed(rows)
    assert [r["speed"] for r in rows] == [1.0, 1.25, 1.25, 1.5, 2.0, 1.0]


@pytest.mark.asyncio
async def test_annotate_sets_provider_health_per_row(monkeypatch):
    async def fake_probe(providers, *, cache=None):
        assert providers == {
            "lithos": ("https://api.lithosai.cloud/v1", "lith-tok"),
            "openai": ("https://api.openai.com/v1", None),
        }
        return {"lithos": "online", "openai": "offline"}

    monkeypatch.setattr("omnigent.host.provider_health.probe_provider_health", fake_probe)
    monkeypatch.setattr(
        "omnigent.host.provider_health._provider_endpoints",
        lambda referenced: {
            "lithos": ("https://api.lithosai.cloud/v1", "lith-tok"),
            "openai": ("https://api.openai.com/v1", None),
        },
    )
    rows: list[dict[str, object]] = [
        {"id": "ds-flash-ultra", "providerId": "lithos"},
        {"id": "gpt-5"},  # no providerId -> openai
    ]
    await annotate_provider_health(rows)
    assert rows[0]["providerHealth"] == "online"
    assert rows[1]["providerHealth"] == "offline"


@pytest.mark.asyncio
async def test_annotate_leaves_rows_untouched_when_nothing_resolvable(monkeypatch):
    monkeypatch.setattr("omnigent.host.provider_health._provider_endpoints", lambda referenced: {})
    rows: list[dict[str, object]] = [{"id": "custom", "providerId": "custom"}]
    await annotate_provider_health(rows)
    assert "providerHealth" not in rows[0]
