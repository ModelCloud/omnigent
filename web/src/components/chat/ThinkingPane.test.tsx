// DOM smoke for the thinking pane: it renders only for a selection in
// its own conversation, shows the selected section's published content,
// and closes via the X button and Escape.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ThinkingPane } from "./ThinkingPane";
import { useThinkingPaneStore } from "@/store/thinkingPane";

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
});
