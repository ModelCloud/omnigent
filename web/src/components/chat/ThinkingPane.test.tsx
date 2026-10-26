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
    tps: undefined as number | undefined,
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

  it("appends the measured reasoning rate when present", () => {
    select("conv-1", "reasoning:0", {
      text: "body",
      isStreaming: false,
      duration: 2.5,
      tps: 48.04,
    });
    render(<ThinkingPane conversationId="conv-1" />);
    expect(screen.getByText("Thought for 2.50s · 48.0 t/s")).toBeTruthy();
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

  it("copies the entire raw trace, preserving Markdown and whitespace", async () => {
    const text = "  **Plan**\n\nFirst option.\n\n```python\nx = 1\n```\n\nFinal option.\n";
    select("conv-1", "reasoning:0", {
      text,
      isStreaming: false,
      duration: 2.5,
      tps: undefined,
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
        tps: undefined,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy reasoning trace" }));
    act(() =>
      select("conv-1", "reasoning:1", {
        text: "Another section.",
        isStreaming: false,
        duration: 1,
        tps: undefined,
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
        tps: undefined,
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
