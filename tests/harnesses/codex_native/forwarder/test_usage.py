"""Usage tests for Codex forwarder."""

from __future__ import annotations

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)

# ── sub-agent usage pricing: seed the child coalescer's model ──────────


@pytest.mark.asyncio
async def test_usage_coalescer_seeded_model_rides_along_so_child_usage_prices() -> None:
    """A coalescer seeded with a model attaches it to its token post.

    Codex sub-agent (child-thread) usage is recorded on a coalescer created on
    the child-event path, where ``forwarder_state`` (the usual model source) is
    intentionally ``None`` — so ``record()`` receives no model. Without the
    constructor seed the token post carries no ``model``, the server leaves the
    child's ``total_cost_usd`` unpriced (``None``), and the sub-agent's spend
    drops out of the parent's subtree cost — letting it run past the budget.
    The seeded model must ride along on every token post so the server can
    price the cumulative tokens.
    """
    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_child", model="gpt-5.5")
    # Mirror the child path exactly: a usage frame with NO model in record().
    coalescer.record({"tokenUsage": {"total": {"inputTokens": 46003, "outputTokens": 4141}}})
    await coalescer.flush()

    assert len(client.posts) == 1  # one external_session_usage post
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_child/events"
    assert body["type"] == "external_session_usage"
    data = body["data"]
    # The seeded model rides along — this is what lets the server price the
    # tokens into the child's total_cost_usd (the whole point of the fix).
    assert data["model"] == "gpt-5.5"
    # The cumulative token counts the server prices from are present.
    assert data["cumulative_input_tokens"] == 46003
    assert data["cumulative_output_tokens"] == 4141


@pytest.mark.asyncio
async def test_usage_coalescer_unseeded_omits_model() -> None:
    """Without a seed (and no model via record), the post carries no model.

    This is the pre-fix behavior that left a sub-agent's cost unpriced; the
    test pins the contrast so a regression dropping the seed is caught (the
    post would silently go back to model-less and the budget gap would return).
    """
    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_child")  # no model seed
    coalescer.record({"tokenUsage": {"total": {"inputTokens": 100, "outputTokens": 5}}})
    await coalescer.flush()

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert "model" not in body["data"]


# ── per-turn generation rates (tokens/sec) ──────────────────────────────


def _usage_frame() -> dict[str, object]:
    return {
        "turnId": "turn_abc",
        "tokenUsage": {
            "total": {"inputTokens": 1000, "outputTokens": 200},
            "last": {
                "inputTokens": 900,
                "outputTokens": 120,
                "reasoningOutputTokens": 30,
            },
        },
    }


def test_turn_token_rate_fields_computes_generation_rates() -> None:
    """Rates divide the turn's token counts by the measured delta spans.

    Reasoning tokens are a subset of ``outputTokens``, so the visible-output
    rate subtracts them; spans come from the first→last delta bounds.
    """
    state = fwd._CodexForwarderState()
    state.turn_reasoning_first_at = 10.0
    state.turn_reasoning_last_at = 16.0  # 30 reasoning tok / 6s = 5.0 t/s
    state.turn_output_first_at = 20.0
    state.turn_output_last_at = 29.0  # (120 - 30) tok / 9s = 10.0 t/s

    fields = fwd._turn_token_rate_fields(_usage_frame(), state)

    assert fields == {"turn_reasoning_tps": 5.0, "turn_output_tps": 10.0}


def test_turn_token_rate_fields_omits_unmeasurable_channels() -> None:
    """No rate without BOTH tokens and a positive measured span."""
    state = fwd._CodexForwarderState()
    # No deltas observed at all.
    assert fwd._turn_token_rate_fields(_usage_frame(), state) == {}

    state.turn_output_first_at = 20.0
    state.turn_output_last_at = 20.0  # zero-width span (single instant delta)
    assert fwd._turn_token_rate_fields(_usage_frame(), state) == {}


def test_turn_token_rate_fields_subtracts_probed_rtt() -> None:
    """The probed round-trip is removed from the measured delta span."""
    state = fwd._CodexForwarderState()
    state.turn_output_first_at = 20.0
    state.turn_output_last_at = 29.0
    state.provider_rtt_seconds = 3.0  # 90 tok / (9s - 3s) = 15.0 t/s

    fields = fwd._turn_token_rate_fields(_usage_frame(), state)

    assert fields["turn_output_tps"] == 15.0


def test_turn_token_rate_fields_drops_span_fully_consumed_by_rtt() -> None:
    """An RTT at least as wide as the span leaves nothing measurable."""
    state = fwd._CodexForwarderState()
    state.turn_output_first_at = 20.0
    state.turn_output_last_at = 21.0
    state.provider_rtt_seconds = 5.0

    assert fwd._turn_token_rate_fields(_usage_frame(), state) == {}


@pytest.mark.asyncio
async def test_usage_post_carries_response_id_and_rates() -> None:
    """The usage post pins rates to the turn's response id."""
    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_1")
    state = fwd._CodexForwarderState()
    state.turn_output_first_at = 100.0
    state.turn_output_last_at = 104.5  # 90 visible tok / 4.5s = 20 t/s

    fwd._handle_usage_update(coalescer, _usage_frame(), state)
    await coalescer.flush()

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    data = body["data"]
    assert data["response_id"] == "codex_turn_abc"
    assert data["turn_output_tps"] == 20.0
    assert "turn_reasoning_tps" not in data  # no reasoning deltas observed
