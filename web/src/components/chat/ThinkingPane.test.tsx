// DOM smoke for the thinking pane: it renders only for a selection in
// its own conversation, shows the selected section's published content,
// and closes via the X button and Escape.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThinkingPane } from "./ThinkingPane";
import { useThinkingPaneStore } from "@/store/thinkingPane";
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
  useThinkingPaneStore.getState().close();
});

function select(
  conversationId: string,
  sectionKey = "reasoning:0",
  section = {
    text: "Considered the options.",
    isStreaming: false,
    duration: 2.5,
  },
) {
  useThinkingPaneStore.getState().open(conversationId, sectionKey);
  useThinkingPaneStore.getState().publish(conversationId, sectionKey, section);
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
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    expect(screen.getByText("Thought for 2.50s")).toBeTruthy();
    expect(screen.getByText("Considered the options.")).toBeTruthy();
  });

  it("does not present a completion rate as a selected block's rate", () => {
    select("conv-1", "reasoning:0", {
      text: "body",
      isStreaming: false,
      duration: 2.5,
    });
    render(<ThinkingPane conversationId="conv-1" />);
    expect(screen.getByText("Thought for 2.50s")).toBeTruthy();
    expect(screen.queryByText(/t\/s/)).toBeNull();
  });

  it("closes via the close button", () => {
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Close thinking pane" }));
    expect(screen.queryByTestId("thinking-pane")).toBeNull();
    expect(useThinkingPaneStore.getState().sectionKey).toBeNull();
  });

  it("closes on Escape", () => {
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(useThinkingPaneStore.getState().sectionKey).toBeNull();
  });

  describe("resizing", () => {
    function renderResizablePane() {
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
      act(() => select("conv-1"));
      fireEvent.pointerMove(screen.getByRole("separator", { name: "Resize thinking pane" }), {
        pointerId: 1,
        clientX: 680,
      });
      expect(
        screen.getByTestId("thinking-pane").style.getPropertyValue("--thinking-pane-width"),
      ).toBe("60%");
    });
  });

  it("copies the entire raw trace, preserving Markdown and whitespace", async () => {
    const text = "  **Plan**\n\nFirst option.\n\n```python\nx = 1\n```\n\nFinal option.\n";
    select("conv-1", "reasoning:0", {
      text,
      isStreaming: false,
      duration: 2.5,
    });
    render(<ThinkingPane conversationId="conv-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy reasoning trace" }));
    expect(copyText).toHaveBeenCalledExactlyOnceWith(text);
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith("Reasoning trace copied", { duration: 1500 }),
    );
    expect(screen.getByTestId("thinking-pane")).toBeInTheDocument();
  });

  it("copies the latest streaming text and then the newly selected section", () => {
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    act(() =>
      select("conv-1", "reasoning:0", {
        text: "First thought.\nMore streamed text.",
        isStreaming: true,
        duration: 2.5,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy reasoning trace" }));
    act(() =>
      select("conv-1", "reasoning:1", {
        text: "Another section.",
        isStreaming: false,
        duration: 1,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy reasoning trace" }));
    expect(vi.mocked(copyText).mock.calls).toEqual([
      ["First thought.\nMore streamed text."],
      ["Another section."],
    ]);
  });

  it.each([null, "", " \n "])("disables copy for unavailable content: %j", (text) => {
    useThinkingPaneStore.getState().open("conv-1", "reasoning:0");
    if (text !== null) {
      select("conv-1", "reasoning:0", {
        text,
        isStreaming: true,
        duration: 0,
      });
    }
    render(<ThinkingPane conversationId="conv-1" />);
    const button = screen.getByRole("button", { name: "Copy reasoning trace" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(copyText).not.toHaveBeenCalled();
  });

  it("reports clipboard failure without claiming success", async () => {
    vi.mocked(copyText).mockRejectedValueOnce(new Error("Permission denied"));
    select("conv-1");
    render(<ThinkingPane conversationId="conv-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy reasoning trace" }));
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledExactlyOnceWith("Could not copy reasoning trace"),
    );
  });
});
