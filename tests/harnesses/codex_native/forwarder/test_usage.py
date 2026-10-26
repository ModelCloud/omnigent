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


# ── latest-completion estimates (tokens/sec) ───────────────────────────


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


def test_completion_token_rate_fields_computes_estimates() -> None:
    """Rates divide one completion's token counts by its delta spans.

    Reasoning tokens are a subset of ``outputTokens``, so the visible-output
    rate subtracts them; spans come from the first→last delta bounds.
    """
    state = fwd._CodexForwarderState()
    state.turn_reasoning_first_at = 10.0
    state.turn_reasoning_last_at = 16.0  # 30 reasoning tok / 6s = 5.0 t/s
    state.turn_output_first_at = 20.0
    state.turn_output_last_at = 29.0  # (120 - 30) tok / 9s = 10.0 t/s

    fields = fwd._completion_token_rate_fields(_usage_frame(), state)

    assert fields == {"completion_tps": {"reasoning": 5.0, "output": 10.0}}
    assert state.turn_reasoning_first_at is None
    assert state.turn_output_last_at is None


@pytest.mark.parametrize("span", [None, 0.0])
def test_completion_token_rate_fields_clears_unmeasurable_channels(span: float | None) -> None:
    """No rate without BOTH tokens and a positive measured span."""
    state = fwd._CodexForwarderState()
    if span is not None:
        state.turn_output_first_at = 20.0
        state.turn_output_last_at = 20.0 + span
    assert fwd._completion_token_rate_fields(_usage_frame(), state) == {"completion_tps": {}}


def test_completion_token_rate_fields_subtracts_probed_rtt() -> None:
    """The probed round-trip is removed from the measured delta span."""
    state = fwd._CodexForwarderState()
    state.turn_output_first_at = 20.0
    state.turn_output_last_at = 29.0
    state.provider_rtt_seconds = 3.0  # 90 tok / (9s - 3s) = 15.0 t/s

    fields = fwd._completion_token_rate_fields(_usage_frame(), state)

    assert fields == {"completion_tps": {"output": 15.0}}


def test_completion_token_rate_fields_drops_span_fully_consumed_by_rtt() -> None:
    """An RTT at least as wide as the span leaves nothing measurable."""
    state = fwd._CodexForwarderState()
    state.turn_output_first_at = 20.0
    state.turn_output_last_at = 21.0
    state.provider_rtt_seconds = 5.0

    assert fwd._completion_token_rate_fields(_usage_frame(), state) == {"completion_tps": {}}


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
    assert data["completion_tps"] == {"output": 20.0}


@pytest.mark.asyncio
async def test_successive_completions_have_independent_rates_and_keep_response_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_1")
    state = fwd._CodexForwarderState()
    times = iter([10.0, 16.0, 100.0, 103.0])
    params = {"turnId": "turn_abc", "delta": "trace"}

    for index in range(2):
        with monkeypatch.context() as clock:
            clock.setattr(fwd.time, "monotonic", lambda: next(times))
            for _ in range(2):
                fwd._note_completion_rate_delta(params, state, reasoning=True)
        usage = _usage_frame()
        if index:
            usage["tokenUsage"]["total"]["outputTokens"] = 320
        fwd._handle_usage_update(coalescer, usage, state)
        await coalescer.flush()

    assert [body["data"]["completion_tps"] for _, body in client.posts] == [
        {"reasoning": 5.0},
        {"reasoning": 10.0},
    ]
    assert all(body["data"]["response_id"] == "codex_turn_abc" for _, body in client.posts)


def test_duplicate_usage_does_not_consume_the_next_completion_window() -> None:
    state = fwd._CodexForwarderState()
    fwd._completion_token_rate_fields(_usage_frame(), state)
    state.turn_reasoning_first_at = 100.0
    state.turn_reasoning_last_at = 103.0
    assert fwd._completion_token_rate_fields(_usage_frame(), state) == {}
    assert state.turn_reasoning_first_at == 100.0


def test_new_turn_discards_previous_timing_and_rejects_old_usage() -> None:
    state = fwd._CodexForwarderState()
    state.turn_reasoning_first_at = 10.0
    state.turn_output_last_at = 50.0
    fwd._reset_completion_rate_window(state, "turn_new")
    assert state.turn_reasoning_first_at is None
    assert state.turn_output_last_at is None
    assert fwd._completion_token_rate_fields(_usage_frame(), state) == {}


@pytest.mark.asyncio
async def test_equal_estimates_on_different_turns_keep_both_identities() -> None:
    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_1")
    state = fwd._CodexForwarderState()
    for turn_id in ("turn_a", "turn_b"):
        fwd._reset_completion_rate_window(state, turn_id)
        state.turn_reasoning_first_at = 10.0
        state.turn_reasoning_last_at = 16.0
        usage = _usage_frame()
        usage["turnId"] = turn_id
        fwd._handle_usage_update(coalescer, usage, state)
        await coalescer.flush()
    assert [body["data"]["response_id"] for _, body in client.posts] == [
        "codex_turn_a",
        "codex_turn_b",
    ]
    assert all(body["data"]["completion_tps"] == {"reasoning": 5.0} for _, body in client.posts)


def test_empty_or_unscoped_deltas_do_not_create_timing() -> None:
    state = fwd._CodexForwarderState()
    for params in ({"delta": "unscoped"}, {"turnId": "turn_abc", "delta": ""}):
        fwd._note_completion_rate_delta(params, state, reasoning=True)
    assert state.turn_reasoning_first_at is None


def test_missing_completion_tokens_clears_rates_and_timing() -> None:
    state = fwd._CodexForwarderState()
    state.turn_reasoning_first_at = 10.0
    state.turn_reasoning_last_at = 16.0
    assert fwd._completion_token_rate_fields(
        {"turnId": "turn_abc", "tokenUsage": {"total": {"outputTokens": 100}}},
        state,
    ) == {"completion_tps": {}}
    assert state.turn_reasoning_first_at is None
