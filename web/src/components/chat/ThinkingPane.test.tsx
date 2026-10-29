// DOM smoke for the thinking pane: it renders only for a selection in
// its own conversation, shows the selected section's content from the
// published `sections` list, follows the latest settled trace in auto
// mode, navigates with prev/next, and closes via the X button and
// Escape.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThinkingPane } from "./ThinkingPane";
import { useThinkingPaneStore, type ThinkingPaneSection } from "@/store/thinkingPane";
import { copyText } from "@/lib/clipboard";
import { showToast } from "@/components/ui/toast";

vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(copyText).mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  resetPane();
});

function resetPane() {
  useThinkingPaneStore.setState({
    conversationId: null,
    sectionKey: null,
    mode: "auto",
    sections: [],
    sectionsConversationId: null,
  });
}

function section(
  key: string,
  text = "Considered the options.",
  rest: Partial<ThinkingPaneSection> = {},
): ThinkingPaneSection {
  return { key, text, isStreaming: false, duration: 2.5, ...rest };
}

function publish(conversationId: string, sections: ThinkingPaneSection[]) {
  useThinkingPaneStore.getState().setSections(conversationId, sections);
}

function select(conversationId: string, sectionKey = "reasoning:0") {
  useThinkingPaneStore.getState().open(conversationId, sectionKey);
}

describe("ThinkingPane", () => {
  it("renders nothing when nothing is selected", () => {
    render(<ThinkingPane conversationId="conv-1" />);
    expect(screen.queryByTestId("thinking-pane")).toBeNull();
  });

  it("renders nothing for a selection made in another conversation", () => {
    // Selection is conversation-scoped — navigating conversations must
    // never show the previous conversation's reasoning.
    select("conv-2");
    render(<ThinkingPane conversationId="conv-1" />);
    expect(screen.queryByTestId("thinking-pane")).toBeNull();
  });

  it("shows the selected section's label and content", () => {
    publish("conv-1", [section("reasoning:0")]);
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    expect(screen.getByText("Thought for 2.50s")).toBeTruthy();
    expect(screen.getByText("Considered the options.")).toBeTruthy();
  });

  it("does not present a completion rate as a selected block's rate", () => {
    publish("conv-1", [section("reasoning:0", "body")]);
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    expect(screen.getByText("Thought for 2.50s")).toBeTruthy();
    expect(screen.queryByText(/t\/s/)).toBeNull();
  });

  it("closes via the close button", () => {
    publish("conv-1", [section("reasoning:0")]);
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Close thinking pane" }));
    expect(screen.queryByTestId("thinking-pane")).toBeNull();
    expect(useThinkingPaneStore.getState().sectionKey).toBeNull();
  });

  it("closes on Escape", () => {
    publish("conv-1", [section("reasoning:0")]);
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(useThinkingPaneStore.getState().sectionKey).toBeNull();
  });

  describe("resizing", () => {
    function renderResizablePane() {
      publish("conv-1", [section("reasoning:0"), section("reasoning:1", "other")]);
      select("conv-1");
      const view = render(<ThinkingPane conversationId="conv-1" />);
      const pane = screen.getByTestId("thinking-pane");
      const handle = screen.getByRole("separator", { name: "Resize thinking pane" });
      handle.setPointerCapture = vi.fn();
      const bounds = vi
        .spyOn(pane.parentElement!, "getBoundingClientRect")
        .mockReturnValue({ left: 200, right: 1000, width: 800 } as DOMRect);
      return { ...view, pane, handle, bounds };
    }

    it("defaults to half width and drags relative to the session, not the viewport", () => {
      const { pane, handle } = renderResizablePane();
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("50%");
      fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 600 });
      expect(handle.setPointerCapture).toHaveBeenCalledWith(1);
      expect(handle).toHaveFocus();
      fireEvent.pointerMove(handle, { pointerId: 1, clientX: 520 });
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("60%");
      fireEvent.pointerMove(handle, { pointerId: 1, clientX: 680 });
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("40%");
    });

    it("keeps both columns usable at the drag limits", () => {
      const { pane, handle } = renderResizablePane();
      fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 600 });
      fireEvent.pointerMove(handle, { pointerId: 1, clientX: -1000 });
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("75%");
      expect(handle).toHaveAttribute("aria-valuenow", "75");
      fireEvent.pointerMove(handle, { pointerId: 1, clientX: 2000 });
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("25%");
      expect(handle).toHaveAttribute("aria-valuenow", "25");
    });

    it("ignores secondary clicks, unrelated pointers, and unmeasurable containers", () => {
      const { pane, handle, bounds } = renderResizablePane();
      fireEvent.pointerDown(handle, { button: 2, pointerId: 1, clientX: 600 });
      fireEvent.pointerMove(handle, { pointerId: 1, clientX: 520 });
      expect(handle.setPointerCapture).not.toHaveBeenCalled();
      fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 600 });
      fireEvent.pointerMove(handle, { pointerId: 2, clientX: 520 });
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("50%");
      bounds.mockReturnValue({ width: 0 } as DOMRect);
      fireEvent.pointerMove(handle, { pointerId: 1, clientX: 520 });
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("50%");
    });

    it.each(["pointerUp", "pointerCancel", "lostPointerCapture"] as const)(
      "stops resizing after %s",
      (event) => {
        const { pane, handle } = renderResizablePane();
        fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 600 });
        fireEvent.pointerMove(handle, { pointerId: 1, clientX: 520 });
        fireEvent[event](handle, { pointerId: 1 });
        fireEvent.pointerMove(handle, { pointerId: 1, clientX: 680 });
        expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("60%");
      },
    );

    it("supports keyboard resizing, bounds, and double-click reset", () => {
      const { pane, handle } = renderResizablePane();
      fireEvent.keyDown(handle, { key: "ArrowLeft" });
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("55%");
      fireEvent.keyDown(handle, { key: "ArrowRight" });
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("50%");
      fireEvent.keyDown(handle, { key: "End" });
      fireEvent.keyDown(handle, { key: "ArrowLeft" });
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("75%");
      fireEvent.keyDown(handle, { key: "Home" });
      fireEvent.keyDown(handle, { key: "ArrowRight" });
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("25%");
      fireEvent.doubleClick(handle);
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("50%");
      fireEvent.keyDown(handle, { key: "ArrowDown" });
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("50%");
    });

    it("retains the chosen width across content updates and close/reopen, but not the drag", () => {
      const { pane, handle } = renderResizablePane();
      fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 600 });
      fireEvent.pointerMove(handle, { pointerId: 1, clientX: 520 });
      act(() => select("conv-1", "reasoning:1"));
      expect(pane.style.getPropertyValue("--thinking-pane-width")).toBe("60%");
      fireEvent.keyDown(handle, { key: "Escape" });
      act(() => select("conv-1", "reasoning:0"));
      fireEvent.pointerMove(screen.getByRole("separator", { name: "Resize thinking pane" }), {
        pointerId: 1,
        clientX: 680,
      });
      expect(
        screen.getByTestId("thinking-pane").style.getPropertyValue("--thinking-pane-width"),
      ).toBe("60%");
    });
  });

  describe("auto mode", () => {
    it("opens the latest completed trace without any click", () => {
      publish("conv-1", [section("reasoning:0", "first"), section("reasoning:1", "latest")]);
      render(<ThinkingPane conversationId="conv-1" />);
      expect(screen.getByText("latest")).toBeTruthy();
      expect(useThinkingPaneStore.getState().sectionKey).toBe("reasoning:1");
    });

    it("moves to each newly completed trace, skipping the one still streaming", () => {
      publish("conv-1", [
        section("reasoning:0", "settled"),
        section("reasoning:1", "streaming", { isStreaming: true, duration: undefined }),
      ]);
      render(<ThinkingPane conversationId="conv-1" />);
      // The streaming trace is not "complete" yet — auto shows the previous one.
      expect(screen.getByText("settled")).toBeTruthy();
      // Once it folds, auto follows to it.
      act(() =>
        publish("conv-1", [section("reasoning:0", "settled"), section("reasoning:1", "done")]),
      );
      expect(screen.getByText("done")).toBeTruthy();
    });

    it("does not open while only a streaming trace exists", () => {
      publish("conv-1", [
        section("reasoning:0", "streaming", { isStreaming: true, duration: undefined }),
      ]);
      render(<ThinkingPane conversationId="conv-1" />);
      expect(screen.queryByTestId("thinking-pane")).toBeNull();
    });

    it("pauses after a manual pick and resumes on the Auto control", () => {
      publish("conv-1", [section("reasoning:0", "first"), section("reasoning:1", "latest")]);
      render(<ThinkingPane conversationId="conv-1" />);
      // User picks an older trace — manual mode: auto-follow pauses.
      act(() => select("conv-1", "reasoning:0"));
      expect(useThinkingPaneStore.getState().mode).toBe("manual");
      act(() =>
        publish("conv-1", [
          section("reasoning:0", "first"),
          section("reasoning:1", "latest"),
          section("reasoning:2", "newest"),
        ]),
      );
      expect(screen.getByText("first")).toBeTruthy();
      // The mode tag resumes the follow.
      fireEvent.click(screen.getByRole("button", { name: /manual mode/i }));
      expect(useThinkingPaneStore.getState().mode).toBe("auto");
      expect(screen.getByText("newest")).toBeTruthy();
    });

    it("stays paused after close until the Auto control is used", () => {
      publish("conv-1", [section("reasoning:0", "first")]);
      render(<ThinkingPane conversationId="conv-1" />);
      fireEvent.click(screen.getByRole("button", { name: "Close thinking pane" }));
      expect(useThinkingPaneStore.getState().mode).toBe("manual");
      act(() =>
        publish("conv-1", [section("reasoning:0", "first"), section("reasoning:1", "newest")]),
      );
      expect(screen.queryByTestId("thinking-pane")).toBeNull();
    });
  });

  describe("prev/next navigation", () => {
    function renderNavPane() {
      publish("conv-1", [
        section("reasoning:0", "first"),
        section("reasoning:1", "middle"),
        section("reasoning:2", "last"),
      ]);
      select("conv-1", "reasoning:1");
      render(<ThinkingPane conversationId="conv-1" />);
    }

    it("walks the trace list in transcript order", () => {
      renderNavPane();
      fireEvent.click(screen.getByRole("button", { name: "Previous reasoning trace" }));
      expect(screen.getByText("first")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Next reasoning trace" }));
      fireEvent.click(screen.getByRole("button", { name: "Next reasoning trace" }));
      expect(screen.getByText("last")).toBeTruthy();
      expect(screen.getByRole("button", { name: "Next reasoning trace" })).toBeDisabled();
    });

    it("disables prev at the oldest trace", () => {
      renderNavPane();
      fireEvent.click(screen.getByRole("button", { name: "Previous reasoning trace" }));
      expect(screen.getByRole("button", { name: "Previous reasoning trace" })).toBeDisabled();
    });

    it("switches to manual mode", () => {
      publish("conv-1", [section("reasoning:0", "first"), section("reasoning:1", "last")]);
      render(<ThinkingPane conversationId="conv-1" />);
      expect(useThinkingPaneStore.getState().mode).toBe("auto");
      fireEvent.click(screen.getByRole("button", { name: "Previous reasoning trace" }));
      expect(useThinkingPaneStore.getState().mode).toBe("manual");
      expect(screen.getByText("first")).toBeTruthy();
    });
  });

  it("copies the entire raw trace, preserving Markdown and whitespace", async () => {
    const text = "  **Plan**\n\nFirst option.\n\n```python\nx = 1\n```\n\nFinal option.\n";
    publish("conv-1", [section("reasoning:0", text)]);
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy reasoning trace" }));
    expect(copyText).toHaveBeenCalledExactlyOnceWith(text);
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith("Reasoning trace copied", { duration: 1500 }),
    );
    expect(screen.getByTestId("thinking-pane")).toBeInTheDocument();
  });

  it("copies the latest streaming text and then the newly selected section", () => {
    publish("conv-1", [section("reasoning:0", "First thought.")]);
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    act(() =>
      publish("conv-1", [
        section("reasoning:0", "First thought.\nMore streamed text.", { isStreaming: true }),
      ]),
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy reasoning trace" }));
    act(() =>
      publish("conv-1", [
        section("reasoning:0", "First thought.\nMore streamed text."),
        section("reasoning:1", "Another section.", { duration: 1 }),
      ]),
    );
    act(() => select("conv-1", "reasoning:1"));
    fireEvent.click(screen.getByRole("button", { name: "Copy reasoning trace" }));
    expect(vi.mocked(copyText).mock.calls).toEqual([
      ["First thought.\nMore streamed text."],
      ["Another section."],
    ]);
  });

  it.each(["", " \n "])("disables copy for unavailable content: %j", (text) => {
    publish("conv-1", [section("reasoning:0", text, { isStreaming: true, duration: 0 })]);
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    const button = screen.getByRole("button", { name: "Copy reasoning trace" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(copyText).not.toHaveBeenCalled();
  });

  it("shows a fallback when the selected section left the transcript", () => {
    publish("conv-1", [section("reasoning:0")]);
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    act(() => publish("conv-1", [section("reasoning:9", "other")]));
    expect(screen.getByText("This reasoning section is no longer available.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy reasoning trace" })).toBeDisabled();
  });

  it("reports clipboard failure without claiming success", async () => {
    vi.mocked(copyText).mockRejectedValueOnce(new Error("Permission denied"));
    publish("conv-1", [section("reasoning:0")]);
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy reasoning trace" }));
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledExactlyOnceWith("Could not copy reasoning trace"),
    );
  });
});
