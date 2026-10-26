import { create } from "zustand";

/** Latest content of the reasoning section shown in the thinking pane. */
export interface ThinkingPaneSection {
  text: string;
  isStreaming: boolean;
  duration: number | undefined;
  /** Forwarder-measured reasoning generation rate (tokens/sec). */
  tps: number | undefined;
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
   * Content the selected section's ReasoningView most recently
   * published. Kept in the store (rather than rendered by the view
   * itself) because reasoning text can still stream after the pane
   * opens — the view re-publishes on each delta so the pane stays live.
   */
  section: ThinkingPaneSection | null;
  open: (conversationId: string, sectionKey: string) => void;
  close: () => void;
  /**
   * Refresh the pane's content — called by the selected ReasoningView
   * whenever its props change. No-op unless the publishing section is
   * the selected one, so non-selected sections never overwrite the pane.
   */
  publish: (conversationId: string, sectionKey: string, section: ThinkingPaneSection) => void;
}

/**
 * Selection state for the right-side thinking pane. Clicking a settled
 * reasoning header opens the pane with that section's content instead of
 * expanding the block inline — inline auto-open stays reserved for the
 * actively streaming edge.
 */
export const useThinkingPaneStore = create<ThinkingPaneState>((set) => ({
  conversationId: null,
  sectionKey: null,
  section: null,
  open: (conversationId, sectionKey) => set({ conversationId, sectionKey, section: null }),
  close: () => set({ conversationId: null, sectionKey: null, section: null }),
  publish: (conversationId, sectionKey, section) =>
    set((s) =>
      s.conversationId === conversationId && s.sectionKey === sectionKey ? { section } : s,
    ),
}));
