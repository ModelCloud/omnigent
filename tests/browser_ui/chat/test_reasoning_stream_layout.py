"""Mobile viewport: a live reasoning section must not remount or jump-layout
when its `output_item.done` lands mid-turn.

Live reasoning chunks carry no itemId, so the merged RenderItem gains the real
id only when the done event arrives. If the React key is derived from the id,
the section unmounts/remounts at that instant — the transcript under the
reader loses its scroll anchor and the collapsible replays its open
animation, which on a narrow mobile viewport reads as the pane contents
jumping up and down.
"""

import json
import time
from typing import Any

import pytest
from playwright.sync_api import Page


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 390, "height": 844},
        "has_touch": True,
        "is_mobile": True,
        "device_scale_factor": 2.625,
    }


def _emit_delta(chat, response_id: str, text: str) -> None:
    chat.emit(
        {
            "event": "response.reasoning_text.delta",
            "data": {"response_id": response_id, "delta": text},
        }
    )


def test_reasoning_done_does_not_remount_or_jump(
    page: Page, chat_session_contract: Any
):
    chat = chat_session_contract
    chat.seed_transcript(turns=8)
    page.goto(chat.url)
    chat.wait_for_stream()

    scroller = page.locator(".transcript-hide-native-scrollbar")
    scroller.wait_for()

    # Per-frame sampler on the real scroller: scrollTop, scrollHeight and the
    # streaming row's geometry. Catches oscillation the eye sees on mobile.
    page.evaluate(
        """() => {
          const el = document.querySelector('.transcript-hide-native-scrollbar');
          window.__samples = [];
          window.__anims = [];
          el.addEventListener('animationstart', e => window.__anims.push([performance.now(), (e.target.className||'').toString().slice(0,120), e.animationName]), true);
          let raf = 0;
          const tick = () => {
            const rows = el.querySelectorAll('[data-index]');
            const last = rows[rows.length - 1];
            const r = last ? last.getBoundingClientRect() : {height: -1, top: -1};
            window.__samples.push([
              performance.now(), el.scrollTop, el.scrollHeight, r.height, r.top,
            ]);
            raf = requestAnimationFrame(tick);
          };
          raf = requestAnimationFrame(tick);
          window.__stop = () => cancelAnimationFrame(raf);
        }"""
    )

    response_id = "stream-reasoning"
    chat.emit_busy(response_id)
    chat.emit(
        {
            "event": "response.in_progress",
            "data": {"response": {"id": response_id, "status": "in_progress"}},
        }
    )
    chat.emit({"event": "response.reasoning.started", "data": {"response_id": response_id}})
    for i in range(12):
        _emit_delta(
            chat,
            response_id,
            f"Reasoning chunk {i} with enough prose to wrap across "
            "several lines on a narrow mobile viewport. ",
        )
        time.sleep(0.03)

    # Tag the live section's DOM node; a mid-turn remount replaces it.
    page.evaluate(
        """() => {
          const el = document.querySelector('.transcript-hide-native-scrollbar');
          const rows = el.querySelectorAll('[data-index]');
          const node = rows[rows.length - 1].querySelector('[data-slot="collapsible-content"]')
                    ?? rows[rows.length - 1];
          node.__probe = "kept";
        }"""
    )

    chat.emit(
        {
            "event": "response.output_item.done",
            "data": {
                "item": {
                    "id": f"{response_id}-reasoning-0",
                    "response_id": response_id,
                    "type": "reasoning",
                    "status": "completed",
                    "model": "mock-model",
                    "summary": [],
                    "content": [
                        {"type": "reasoning_text", "text": "done reasoning " * 40}
                    ],
                }
            },
        }
    )
    page.wait_for_timeout(500)
    page.evaluate("() => window.__stop()")

    # 1) The DOM node must be the same element — a React key swap remounts it.
    kept = page.evaluate(
        """() => {
          const el = document.querySelector('.transcript-hide-native-scrollbar');
          const rows = el.querySelectorAll('[data-index]');
          for (const row of rows) {
            const node = row.querySelector('[data-slot="collapsible-content"]') ?? row;
            if (node.__probe === "kept") return true;
          }
          return false;
        }"""
    )
    assert kept, (
        "reasoning section DOM node was replaced when output_item.done landed "
        "— its React key changed mid-turn, remounting the collapsible"
    )

    # 2) scrollTop may drop once (the collapse itself) but must not oscillate.
    samples = page.evaluate("() => window.__samples")
    down_scrolls = [
        cur[1] - prev[1]
        for prev, cur in zip(samples, samples[1:])
        if cur[1] - prev[1] < -4
    ]
    anims = page.evaluate("() => window.__anims")
    print(json.dumps({"n": len(samples), "down_scrolls": down_scrolls}))
    print("ANIMS", json.dumps([a[1:] for a in anims]))
    assert len(down_scrolls) <= 1, (
        f"scrollTop moved backwards {len(down_scrolls)} times during the stream: "
        f"{down_scrolls}"
    )


def test_thinking_pane_left_strip_swallows_taps_and_closes(
    page: Page, chat_session_contract: Any
) -> None:
    """On mobile the pane overlays ~85vw; the uncovered strip must eat taps
    (never reaching the chat UI) and close the pane."""
    from playwright.sync_api import expect

    from tests.browser_ui.chat.test_thinking_pane import _seed_turn_with_thought

    chat = chat_session_contract
    turn, _ = _seed_turn_with_thought(1, "Mobile scrim trace.")
    chat.set_items(list(reversed(turn)))
    page.goto(chat.url)
    chat.wait_for_stream()

    pane = page.get_by_test_id("thinking-pane")
    expect(pane).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("thinking-pane-scrim")).to_be_attached()

    # Any click reaching the chat surface under the overlay is a miss.
    page.evaluate(
        """() => {
          window.__stray = 0;
          document.querySelector('[data-chat-surface]')
            .addEventListener('click', () => window.__stray += 1, true);
        }"""
    )
    page.touchscreen.tap(10, 400)
    expect(pane).to_be_hidden(timeout=5_000)
    assert page.evaluate("() => window.__stray") == 0, (
        "tap on the uncovered strip reached the chat UI under the thinking pane"
    )
