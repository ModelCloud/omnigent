// Adapter from a `RenderItem.reasoning` to the vendored `<Reasoning>`
// chain. The vendored component handles auto-open/auto-close timing and
// the "Thought for N seconds" timer via wall clock; we just thread
// through `isStreaming` and the accumulated text.
//
// Clicks never expand the section inline: they open the right-side
// thinking pane (`useThinkingPaneStore`) showing this section, so older
// thoughts stay inspectable while a new one streams at the live edge.
// The pane reads section content from the store's `sections` list
// (published by the Transcript from the rendered bubbles), so it stays
// live while this section streams and while it is folded/unmounted.

import { Reasoning, ReasoningContent, ReasoningTrigger } from "@/components/ai-elements/reasoning";
import { useThinkingPaneStore } from "@/store/thinkingPane";

interface ReasoningViewProps {
  text: string;
  isStreaming: boolean;
  duration: number | undefined;
  /** Conversation owning this section — scopes the pane selection. */
  conversationId?: string;
  /**
   * Positional render key identifying this section (`keyFor` output —
   * reasoning items carry no itemId). Needed with `conversationId` to
   * open the thinking pane.
   */
  sectionKey?: string;
}

export function ReasoningView({
  text,
  isStreaming,
  duration,
  conversationId,
  sectionKey,
}: ReasoningViewProps) {
  // A reasoning section is only worth expanding when it has something to
  // show. While streaming we keep it expandable even before the first
  // chunk lands (the "Thinking..." shimmer is live feedback, and content
  // is on its way). Once settled, an empty section (e.g. a `reasoning_start`
  // with no chunks) renders as a flat header with no dead expand affordance.
  const expandable = isStreaming || text.trim().length > 0;

  const openPane = useThinkingPaneStore((s) => s.open);
  const onInspect =
    conversationId !== undefined && sectionKey !== undefined
      ? () => openPane(conversationId, sectionKey)
      : undefined;

  return (
    <Reasoning
      isStreaming={isStreaming}
      duration={duration}
      expandable={expandable}
      onInspect={onInspect}
    >
      <ReasoningTrigger />
      <ReasoningContent>{text}</ReasoningContent>
    </Reasoning>
  );
}
