// Right-side pane that displays a reasoning section selected from the
// transcript. Clicking a "Thought for Ns"/"Thinking..." header selects
// the section here instead of expanding it inline, so older thinking
// stays readable while a live thought streams at the bottom edge. The
// selected section's ReasoningView keeps republishing its content, so
// the pane stays live while the section streams.

import { useEffect } from "react";
import { CopyIcon, SparklesIcon, XIcon } from "lucide-react";
import { ReasoningMarkdown } from "@/components/ai-elements/reasoning";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import { copyText } from "@/lib/clipboard";
import { formatTps } from "@/store/turnTps";
import { useThinkingPaneStore } from "@/store/thinkingPane";

interface ThinkingPaneProps {
  /** Conversation this pane belongs to; selections from other conversations never render. */
  conversationId: string;
}

export function ThinkingPane({ conversationId }: ThinkingPaneProps) {
  const isSelected = useThinkingPaneStore(
    (s) => s.conversationId === conversationId && s.sectionKey !== null,
  );
  const section = useThinkingPaneStore((s) =>
    s.conversationId === conversationId ? s.section : null,
  );
  const close = useThinkingPaneStore((s) => s.close);

  useEffect(() => {
    if (!isSelected) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isSelected, close]);

  if (!isSelected) return null;

  const duration = section?.duration;
  const tps = section?.tps;
  const tpsSuffix = tps !== undefined ? ` · ${formatTps(tps)} t/s` : "";
  const label = section?.isStreaming ? (
    <Shimmer as="span" duration={1.5}>
      Thinking...
    </Shimmer>
  ) : duration !== undefined ? (
    `Thought for ${duration.toFixed(2)}s${tpsSuffix}`
  ) : (
    `Thought for a few seconds${tpsSuffix}`
  );

  return (
    <aside
      data-testid="thinking-pane"
      aria-label="Thinking pane"
      className="flex w-1/2 shrink-0 flex-col border-l border-border bg-card/40 pt-14 md:pt-12 max-sm:absolute max-sm:inset-y-0 max-sm:right-0 max-sm:z-40 max-sm:w-[85vw] max-sm:bg-background max-sm:shadow-xl"
    >
      <div className="flex items-center gap-1.5 border-b border-border px-3 py-2">
        <SparklesIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-ui text-foreground">{label}</span>
        <Button
          type="button"
          size="icon-sm"
          variant="ghost"
          className="shrink-0"
          aria-label="Copy reasoning trace"
          title="Copy entire reasoning trace"
          disabled={!section?.text.trim()}
          onClick={() => {
            if (!section?.text.trim()) return;
            void copyText(section.text).then(
              () => showToast("Reasoning trace copied", { duration: 1500 }),
              () => showToast("Could not copy reasoning trace"),
            );
          }}
        >
          <CopyIcon className="size-4" />
        </Button>
        <button
          type="button"
          aria-label="Close thinking pane"
          onClick={close}
          className="shrink-0 rounded-full p-0.5 text-muted-foreground transition-colors hover:text-foreground"
        >
          <XIcon className="size-4" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2 text-ui text-foreground chat-markdown">
        {section !== null && section.text.trim().length > 0 ? (
          <ReasoningMarkdown>{section.text}</ReasoningMarkdown>
        ) : (
          <p className="text-muted-foreground">
            {section?.isStreaming
              ? "Reasoning is streaming — deltas appear here as they arrive."
              : "No reasoning content was recorded for this section."}
          </p>
        )}
      </div>
    </aside>
  );
}
