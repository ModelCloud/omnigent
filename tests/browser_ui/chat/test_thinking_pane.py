"""Real-browser coverage for the thinking pane's follow and browse modes.

The right-hand pane has an "auto" mode that always shows the newest
completed reasoning trace — a trace that folds open lands in the pane
without a click — and a "manual" mode entered by any user pick (a
transcript header, prev/next, or closing the pane), which stays pinned
until the header's Auto control resumes the follow.
"""

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item


def _reasoning_item(item_id: str, text: str, *, response_id: str) -> dict[str, Any]:
    """Build one persisted reasoning transcript item."""
    return {
        "id": item_id,
        "response_id": response_id,
        "type": "reasoning",
        "status": "completed",
        "model": "mock-model",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": text}],
    }


def _seed_turn_with_thought(index: int, thought: str) -> tuple[list[dict[str, Any]], str]:
    """Build a completed turn (user + reasoning + assistant) as wire items."""
    response_id = f"pane-response-{index}"
    return (
        [
            message_item(
                f"pane-user-{index}",
                "user",
                f"Pane request {index}",
                response_id=response_id,
            ),
            _reasoning_item(f"pane-thought-{index}", thought, response_id=response_id),
            message_item(
                f"pane-assistant-{index}",
                "assistant",
                f"Pane answer {index}.",
                response_id=response_id,
            ),
        ],
        response_id,
    )


def _emit_live_thought(chat: ChatSessionContract, response_id: str, text: str) -> None:
    """Stream one reasoning turn to completion over the mocked SSE feed."""
    chat.emit_busy(response_id)
    chat.emit(
        {
            "event": "response.in_progress",
            "data": {"response": {"id": response_id, "status": "in_progress"}},
        }
    )
    chat.emit({"event": "response.reasoning.started", "data": {"response_id": response_id}})
    chat.emit(
        {
            "event": "response.reasoning_text.delta",
            "data": {"response_id": response_id, "delta": text},
        }
    )
    chat.emit(
        {
            "event": "response.output_item.done",
            "data": {
                "item": {
                    "id": f"{response_id}-reasoning",
                    "response_id": response_id,
                    "type": "reasoning",
                    "status": "completed",
                    "model": "mock-model",
                    "summary": [],
                    "content": [{"type": "reasoning_text", "text": text}],
                }
            },
        }
    )
    # A trailing message makes the completed turn fold, the way a real
    # turn does — a reasoning-only bubble has nothing to fold behind.
    answer = f"Answer for {response_id}."
    chat.emit(
        {
            "event": "response.output_text.delta",
            "data": {
                "response_id": response_id,
                "item_id": f"{response_id}-msg",
                "delta": answer,
            },
        }
    )
    chat.emit(
        {
            "event": "response.output_item.done",
            "data": {
                "item": {
                    "id": f"{response_id}-msg",
                    "response_id": response_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": answer}],
                }
            },
        }
    )
    chat.emit(
        {
            "event": "response.completed",
            "data": {"response": {"id": response_id, "status": "completed"}},
        }
    )
    chat.emit_idle(response_id)


def _open_thinking_pane(page: Page) -> None:
    expect(page.get_by_test_id("thinking-pane")).to_be_visible(timeout=20_000)


def test_pane_auto_follows_newest_trace_and_manual_pick_pauses(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Auto mode opens the latest folded trace; a manual pick pauses it."""
    chat = chat_session_contract
    first_turn, _ = _seed_turn_with_thought(1, "First settled trace.")
    latest_turn, _ = _seed_turn_with_thought(2, "Latest settled trace.")
    chat.set_items([*reversed(latest_turn), *reversed(first_turn)])
    page.goto(chat.url)
    chat.wait_for_stream()

    # Auto mode: the newest completed trace opens on load, no click needed.
    _open_thinking_pane(page)
    pane = page.get_by_test_id("thinking-pane")
    expect(pane.get_by_text("Latest settled trace.")).to_be_visible(timeout=10_000)

    # A live trace streams — auto keeps showing the last settled one —
    # then folds, and the pane follows to it.
    _emit_live_thought(chat, "pane-live-3", "Freshly folded live trace.")
    expect(pane.get_by_text("Freshly folded live trace.")).to_be_visible(timeout=10_000)

    # Expanding the oldest turn's fold and clicking its trace header is a
    # manual pick: the pane pins to it even as newer traces land.
    page.get_by_role("button", name=re.compile("Worked")).first.click()
    page.get_by_role("button", name=re.compile("Thought")).first.click()
    expect(pane.get_by_text("First settled trace.")).to_be_visible(timeout=10_000)
    expect(pane.get_by_test_id("thinking-pane-mode")).to_have_text("Manual")

    _emit_live_thought(chat, "pane-live-4", "Newest trace while paused.")
    # Wait until the new turn's answer landed so the paused assertion
    # can't pass before the events arrive.
    expect(page.get_by_text("Answer for pane-live-4.")).to_be_visible(timeout=10_000)
    expect(pane.get_by_text("First settled trace.")).to_be_visible()
    expect(pane.get_by_text("Newest trace while paused.")).not_to_be_visible()

    # The mode tag resumes the follow: the pane jumps to the newest trace.
    pane.get_by_test_id("thinking-pane-mode").click()
    expect(pane.get_by_test_id("thinking-pane-mode")).to_have_text("Auto")
    expect(pane.get_by_text("Newest trace while paused.")).to_be_visible(timeout=10_000)


def test_pane_prev_next_walks_traces_in_transcript_order(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Prev/next browse pinned traces oldest-to-newest as manual mode."""
    chat = chat_session_contract
    items: list[dict[str, Any]] = []
    for index, thought in enumerate(["Trace one.", "Trace two.", "Trace three."], start=1):
        turn, _ = _seed_turn_with_thought(index, thought)
        items = [*reversed(turn), *items]
    chat.set_items(items)
    page.goto(chat.url)
    chat.wait_for_stream()

    _open_thinking_pane(page)
    pane = page.get_by_test_id("thinking-pane")
    prev_button = pane.get_by_role("button", name="Previous reasoning trace")
    next_button = pane.get_by_role("button", name="Next reasoning trace")

    expect(pane.get_by_text("Trace three.")).to_be_visible(timeout=10_000)
    expect(next_button).to_be_disabled()

    prev_button.click()
    expect(pane.get_by_text("Trace two.")).to_be_visible(timeout=10_000)
    prev_button.click()
    expect(pane.get_by_text("Trace one.")).to_be_visible(timeout=10_000)
    expect(prev_button).to_be_disabled()

    next_button.click()
    expect(pane.get_by_text("Trace two.")).to_be_visible(timeout=10_000)
