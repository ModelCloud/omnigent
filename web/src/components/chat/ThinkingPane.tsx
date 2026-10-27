// Right-side pane that displays a reasoning section selected from the
// transcript. Clicking a "Thought for Ns"/"Thinking..." header selects
// the section here instead of expanding it inline, so older thinking
// stays readable while a live thought streams at the bottom edge. The
// selected section's ReasoningView keeps republishing its content, so
// the pane stays live while the section streams.

import { useEffect, useRef, useState, type CSSProperties } from "react";
import { CopyIcon, SparklesIcon, XIcon } from "lucide-react";
import { ReasoningMarkdown } from "@/components/ai-elements/reasoning";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import { copyText } from "@/lib/clipboard";
import { useThinkingPaneStore } from "@/store/thinkingPane";

const DEFAULT_WIDTH_PERCENT = 50;
const MIN_WIDTH_PERCENT = 25;
const MAX_WIDTH_PERCENT = 75;

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
  const paneRef = useRef<HTMLElement>(null);
  const [widthPercent, setWidthPercent] = useState(DEFAULT_WIDTH_PERCENT);
  const drag = useRef<{ pointerId: number; x: number; widthPercent: number } | null>(null);

  const resize = (width: number) => {
    setWidthPercent(Math.max(MIN_WIDTH_PERCENT, Math.min(MAX_WIDTH_PERCENT, width)));
  };
  const endDrag = () => {
    drag.current = null;
  };

  useEffect(() => {
    drag.current = null;
    if (!isSelected) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isSelected, close]);

  if (!isSelected) return null;

  const duration = section?.duration;
  const label = section?.isStreaming ? (
    <Shimmer as="span" duration={1.5}>
      Thinking...
    </Shimmer>
  ) : duration !== undefined ? (
    `Thought for ${duration.toFixed(2)}s`
  ) : (
    "Thought for a few seconds"
  );

  return (
    <aside
      ref={paneRef}
      data-testid="thinking-pane"
      aria-label="Thinking pane"
      style={{ "--thinking-pane-width": `${widthPercent}%` } as CSSProperties}
      className="relative flex w-[var(--thinking-pane-width)] shrink-0 flex-col border-l border-border bg-card/40 pt-14 md:pt-12 max-sm:absolute max-sm:inset-y-0 max-sm:right-0 max-sm:z-40 max-sm:w-[85vw] max-sm:bg-background max-sm:shadow-xl"
    >
      <div
        role="separator"
        tabIndex={0}
        aria-label="Resize thinking pane"
        aria-orientation="vertical"
        aria-valuemin={MIN_WIDTH_PERCENT}
        aria-valuemax={MAX_WIDTH_PERCENT}
        aria-valuenow={Math.round(widthPercent)}
        aria-valuetext={`${Math.round(widthPercent)}% of session width`}
        title="Drag to resize; double-click to reset"
        className="group absolute inset-y-0 -left-1 z-10 flex w-2 touch-none cursor-col-resize items-center justify-center outline-none max-sm:hidden"
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          event.preventDefault();
          event.currentTarget.focus();
          event.currentTarget.setPointerCapture(event.pointerId);
          drag.current = { pointerId: event.pointerId, x: event.clientX, widthPercent };
        }}
        onPointerMove={(event) => {
          const start = drag.current;
          const containerWidth = paneRef.current?.parentElement?.getBoundingClientRect().width;
          if (!start || start.pointerId !== event.pointerId || !containerWidth) return;
          resize(start.widthPercent + ((start.x - event.clientX) / containerWidth) * 100);
        }}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onLostPointerCapture={endDrag}
        onDoubleClick={() => resize(DEFAULT_WIDTH_PERCENT)}
        onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          if (event.key === "Home") resize(MIN_WIDTH_PERCENT);
          else if (event.key === "End") resize(MAX_WIDTH_PERCENT);
          else resize(widthPercent + (event.key === "ArrowLeft" ? 5 : -5));
        }}
      >
        <span className="h-full w-px bg-border group-hover:bg-ring group-focus-visible:bg-ring group-active:bg-ring" />
      </div>
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
