"""Cold resume reads a complete compaction window instead of the whole archive."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import main as codex_native


async def _fetch_history(
    history: list[dict[str, Any]], *, page_size: int = 1000
) -> tuple[list[dict[str, Any]], int]:
    newest_first = list(reversed(history))
    positions = {item["id"]: index for index, item in enumerate(newest_first)}
    requests = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.path == "/v1/sessions/conv_codex/items"
        assert request.url.params["order"] == "desc"
        assert request.url.params["limit"] == "1000"
        after = request.url.params.get("after")
        start = positions[after] + 1 if after else 0
        page = newest_first[start : start + page_size]
        return httpx.Response(
            200,
            json={
                "data": page,
                "last_id": page[-1]["id"] if page else None,
                "has_more": start + len(page) < len(history),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        result = await codex_native._fetch_session_items_for_codex_resume(client, "conv_codex")
    return result, requests


def _records(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return codex_native._codex_rollout_records_from_session_items(
        items,
        session_id="conv_codex",
        external_session_id="019e96aa-0be2-7343-8d3b-6f914d60936b",
        cwd=Path("/workspace"),
        model_provider="test_provider",
        cli_version="0.0.0",
    )


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(codex_native, "_codex_rollout_timestamp", lambda: "2026-10-20T00:00:00Z")
    return [
        {
            "id": f"old_{index}",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "old turn"}],
        }
        for index in range(40)
    ]


@pytest.fixture
def compaction() -> dict[str, Any]:
    return {
        "id": "checkpoint",
        "type": "compaction",
        "response_id": "compact_snapshot",
        "summary": "earlier work",
        "window_id": "window_1",
        "compacted_messages": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "summary"}],
            }
        ],
    }


async def test_large_archive_stops_after_one_page_with_identical_rollout(
    history: list[dict[str, Any]], compaction: dict[str, Any]
) -> None:
    history = [{**history[0], "id": f"old_{index}"} for index in range(129_000)]
    history.extend(
        [
            compaction,
            {
                "id": "latest_user",
                "response_id": "codex_latest",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "continue"}],
            },
        ]
    )
    fetched, requests = await _fetch_history(history)
    assert requests == 1  # The ascending implementation needs 130 pages.
    assert len(fetched) == 1000
    assert _records(fetched) == _records(history)


@pytest.mark.parametrize("snapshot_has_calls", [False, True])
@pytest.mark.parametrize("interruption", [None, "before", "after"])
async def test_fetches_cross_compaction_calls_and_interruption_boundaries(
    history: list[dict[str, Any]],
    compaction: dict[str, Any],
    snapshot_has_calls: bool,
    interruption: str | None,
) -> None:
    user = {
        "id": "user_slow",
        "response_id": "codex_slow",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "run both"}],
    }
    calls = [
        {
            "id": f"fc_{name}",
            "response_id": "codex_slow",
            "type": "function_call",
            "name": "exec_command",
            "arguments": "{}",
            "call_id": name,
        }
        for name in ["first", "second"]
    ]
    marker = {
        "id": "cancelled",
        "response_id": "codex_slow",
        "type": "message",
        "role": "assistant",
        "interrupted": True,
        "content": [{"type": "output_text", "text": "cancelled"}],
    }
    history.extend([user, *calls])
    # Force dependency recovery across pages and across two compactions.
    history.extend([{**compaction, "id": "earlier_checkpoint"}, *history[:4]])
    # Page cursors must remain unique even for repeated message contents.
    for index, item in enumerate(history):
        history[index] = {**item, "id": f"item_{index}"}
    if interruption == "before":
        history.append(marker)
    if snapshot_has_calls:
        compaction["compacted_messages"].extend(
            {key: value for key, value in call.items() if key != "response_id"} for call in calls
        )
    history.append(compaction)
    history.extend(
        {
            "id": f"output_{name}",
            "type": "function_call_output",
            "call_id": name,
            "response_id": "codex_slow",
            "output": "finished",
        }
        for name in ["second", "first"]
    )
    if interruption == "after":
        history.append(marker)

    fetched, requests = await _fetch_history(history, page_size=4)
    assert requests < (len(history) + 3) // 4
    assert _records(fetched) == _records(history)
    if interruption is None:
        outputs = [
            record["payload"]["call_id"]
            for record in _records(fetched)
            if record["type"] == "response_item"
            and record["payload"]["type"] == "function_call_output"
        ]
        assert outputs == ["first", "second"]


@pytest.mark.parametrize("snapshot", [None, [], "not a snapshot"])
async def test_unusable_compaction_fetches_complete_history(
    history: list[dict[str, Any]], compaction: dict[str, Any], snapshot: Any
) -> None:
    compaction["compacted_messages"] = snapshot
    history.append(compaction)
    fetched, requests = await _fetch_history(history, page_size=4)
    assert fetched == history
    assert requests == (len(history) + 3) // 4


async def test_no_compaction_fetches_complete_history(history: list[dict[str, Any]]) -> None:
    fetched, requests = await _fetch_history(history, page_size=4)
    assert fetched == history
    assert requests == 10


@pytest.mark.parametrize("boundary", ["interrupted", "imported", "missing_user"])
async def test_uncertain_turn_boundary_fetches_complete_history(
    history: list[dict[str, Any]], compaction: dict[str, Any], boundary: str
) -> None:
    if boundary == "imported":
        compaction["response_id"] = "codex_imported"
    history.append(compaction)
    history.append(
        {
            "id": "assistant_tail",
            "type": "message",
            "role": "assistant",
            "response_id": "compact_snapshot" if boundary == "interrupted" else "codex_tail",
            "content": [{"type": "output_text", "text": "last message"}],
            "interrupted": boundary == "interrupted",
        }
    )
    fetched, requests = await _fetch_history(history, page_size=4)
    assert requests == (len(history) + 3) // 4
    assert fetched == history
    assert _records(fetched) == _records(history)
