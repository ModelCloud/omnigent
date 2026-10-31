import { create } from "zustand";

/**
 * One reasoning section of a transcript, in transcript order. Derived
 * from the same bubble list the transcript renders (`streamBubbles`),
 * so `key` is identical to the `sectionKey` a mounted ReasoningView
 * opens on click (`responseId:keyFor(item, index)`).
 */
export interface ThinkingPaneSection {
  key: string;
  text: string;
  isStreaming: boolean;
  duration: number | undefined;
}

interface ThinkingPaneState {
  /**
   * Conversation the selected section belongs to. A pane selection is
   * conversation-scoped: switching conversations never shows the other
   * conversation's reasoning.
   */
  conversationId: string | null;
  /**
   * Render key of the selected reasoning section within its bubble
   * (`keyFor` positional key — reasoning items carry no itemId).
   */
  sectionKey: string | null;
  /**
   * `"auto"`: the pane follows the transcript — whenever a reasoning
   * section settles it becomes the selection, so the pane always shows
   * the latest completed trace and refreshes as traces land.
   *
   * `"manual"`: auto-follow is paused. Any user pick — a transcript
   * click, the pane's prev/next controls, or closing the pane —
   * switches here until the pane's Auto control switches back.
   */
  mode: "auto" | "manual";
  /**
   * Reasoning sections of `sectionsConversationId`, ordered as in the
   * transcript. The Transcript publishes these from the same bubble
   * list it renders, so folded and off-screen sections are present.
   */
  sections: ThinkingPaneSection[];
  sectionsConversationId: string | null;
  /**
   * User-driven selection: picks this section and switches to manual
   * mode (pauses auto-follow until the pane's Auto control).
   */
  open: (conversationId: string, sectionKey: string) => void;
  /**
   * Selection change without a mode change — the auto-follow effect
   * moves the selection to the newest completed section.
   */
  follow: (conversationId: string, sectionKey: string | null) => void;
  /** Flip between auto-follow and manual browsing. */
  toggleAuto: () => void;
  /**
   * Menu-driven open: clears any manual selection and switches to auto
   * mode — the pane's follow effect lands on the newest settled trace,
   * so a session with no completed reasoning simply stays closed.
   */
  openAuto: (conversationId: string) => void;
  /**
   * Closing is a user pick too: it pauses auto-follow (manual mode),
   * otherwise the pane would reopen on the next settled trace.
   */
  close: () => void;
  setSections: (conversationId: string | null, sections: ThinkingPaneSection[]) => void;
}

function sectionsEqual(a: ThinkingPaneSection[], b: ThinkingPaneSection[]): boolean {
  return (
    a.length === b.length &&
    a.every(
      (section, i) =>
        section.key === b[i]!.key &&
        section.text === b[i]!.text &&
        section.isStreaming === b[i]!.isStreaming &&
        section.duration === b[i]!.duration,
    )
  );
}

/**
 * Selection state for the right-side thinking pane. Content comes from
 * `sections` (published by the Transcript), never from the views, so
 * the pane navigates and auto-follows sections whose headers are
 * folded or virtualized out of the DOM.
 */
export const useThinkingPaneStore = create<ThinkingPaneState>((set) => ({
  conversationId: null,
  sectionKey: null,
  mode: "auto",
  sections: [],
  sectionsConversationId: null,
  open: (conversationId, sectionKey) => set({ conversationId, sectionKey, mode: "manual" }),
  follow: (conversationId, sectionKey) =>
    set((s) =>
      s.conversationId === conversationId && s.sectionKey === sectionKey
        ? s
        : { conversationId, sectionKey },
    ),
  toggleAuto: () => set((s) => ({ mode: s.mode === "auto" ? "manual" : "auto" })),
  openAuto: (conversationId) => set({ conversationId, sectionKey: null, mode: "auto" }),
  close: () => set({ conversationId: null, sectionKey: null, mode: "manual" }),
  setSections: (conversationId, sections) =>
    set((s) =>
      s.sectionsConversationId === conversationId && sectionsEqual(s.sections, sections)
        ? s
        : { sections, sectionsConversationId: conversationId },
    ),
}));
