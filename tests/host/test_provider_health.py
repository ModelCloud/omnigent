"""Unit tests for the JIT provider liveness probe behind the model picker."""

from __future__ import annotations

import pytest

from omnigent.host.provider_health import (
    PROVIDER_HEALTH_TTL_S,
    annotate_provider_health,
    probe_provider_health,
)


@pytest.mark.asyncio
async def test_probe_queries_models_route_and_classifies_status(monkeypatch):
    seen: list[str] = []

    class _Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url):
            seen.append(url)
            if "dead" in url:
                raise TimeoutError("unreachable")
            if "broken" in url:
                return _Response(503)
            return _Response(401)  # auth-gated still proves the gateway is up

    monkeypatch.setattr("omnigent.host.provider_health.httpx.AsyncClient", lambda **kw: _Client())
    health = await probe_provider_health(
        {
            "up": "https://openrouter.ai/api/v1",
            "broken": "https://broken.invalid/v1/",
            "dead": "http://dead.invalid/v1",
        }
    )
    assert health == {"up": "online", "broken": "offline", "dead": "offline"}
    assert seen == [
        "https://openrouter.ai/api/v1/models",
        "https://broken.invalid/v1/models",
        "http://dead.invalid/v1/models",
    ]


@pytest.mark.asyncio
async def test_probe_serves_fresh_results_from_ttl_cache(monkeypatch):
    calls = []

    class _Response:
        status_code = 200

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url):
            calls.append(url)
            return _Response()

    monkeypatch.setattr("omnigent.host.provider_health.httpx.AsyncClient", lambda **kw: _Client())
    cache: dict[str, tuple[float, bool]] = {}
    providers = {"p": "https://api.openai.com/v1"}
    assert (await probe_provider_health(providers, cache=cache)) == {"p": "online"}
    # Second call inside the TTL must not re-ping the endpoint.
    assert (await probe_provider_health(providers, cache=cache)) == {"p": "online"}
    assert calls == ["https://api.openai.com/v1/models"]


@pytest.mark.asyncio
async def test_probe_rechecks_after_ttl_expiry(monkeypatch):
    calls = []
    clock = {"now": 1000.0}

    class _Response:
        status_code = 200

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url):
            calls.append(url)
            return _Response()

    monkeypatch.setattr("omnigent.host.provider_health.httpx.AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr("omnigent.host.provider_health.time.monotonic", lambda: clock["now"])
    cache: dict[str, tuple[float, bool]] = {}
    providers = {"p": "https://api.openai.com/v1"}
    await probe_provider_health(providers, cache=cache)
    clock["now"] += PROVIDER_HEALTH_TTL_S + 0.1
    await probe_provider_health(providers, cache=cache)
    assert calls == ["https://api.openai.com/v1/models", "https://api.openai.com/v1/models"]


@pytest.mark.asyncio
async def test_annotate_sets_provider_health_per_row(monkeypatch):
    async def fake_probe(providers, *, cache=None):
        assert providers == {
            "lithos": "https://api.lithosai.cloud/v1",
            "openai": "https://api.openai.com/v1",
        }
        return {"lithos": "online", "openai": "offline"}

    monkeypatch.setattr("omnigent.host.provider_health.probe_provider_health", fake_probe)
    monkeypatch.setattr(
        "omnigent.host.provider_health._provider_base_urls",
        lambda referenced: {
            "lithos": "https://api.lithosai.cloud/v1",
            "openai": "https://api.openai.com/v1",
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
    monkeypatch.setattr("omnigent.host.provider_health._provider_base_urls", lambda referenced: {})
    rows: list[dict[str, object]] = [{"id": "custom", "providerId": "custom"}]
    await annotate_provider_health(rows)
    assert "providerHealth" not in rows[0]
