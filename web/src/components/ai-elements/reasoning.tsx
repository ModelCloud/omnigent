"use client";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { ChevronRightIcon, SparklesIcon } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";
import { createContext, memo, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { Streamdown } from "streamdown";
import { MERMAID_STREAMDOWN_PROPS } from "@/components/ai-elements/MermaidBlock";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";

import { normalizeExplicitMathDelimiters } from "./mathMarkdown";
import { formatTps } from "@/store/turnTps";
import { Shimmer } from "./shimmer";
import { MarkdownErrorBoundary } from "./MarkdownErrorBoundary";
import { mermaidOptionsForTheme } from "./MermaidError";
import {
  CHAT_LINK_SAFETY,
  SECURE_STREAMDOWN_REHYPE_PLUGINS,
  STREAMDOWN_PLUGINS,
} from "./streamdown-security";

interface ReasoningContextValue {
  isStreaming: boolean;
  isOpen: boolean;
  /**
   * When set, clicking the trigger inspects the section in the
   * right-side thinking pane instead of expanding it inline. Inline
   * auto-open while streaming is unaffected.
   */
  onInspect?: () => void;
  duration: number | undefined;
  /**
   * Whether this reasoning section can be expanded to reveal content.
   * `false` when there is nothing to show (a settled section with empty
   * text) — the trigger then renders as a flat, non-interactive header
   * with no chevron, and the content is not rendered at all.
   */
  expandable: boolean;
  /** Measured reasoning generation rate (tokens/sec) for this turn. */
  tps: number | undefined;
}

const ReasoningContext = createContext<ReasoningContextValue | null>(null);

export const useReasoning = () => {
  const context = useContext(ReasoningContext);
  if (!context) {
    throw new Error("Reasoning components must be used within Reasoning");
  }
  return context;
};

export type ReasoningProps = ComponentProps<typeof Collapsible> & {
  isStreaming?: boolean;
  duration?: number;
  /**
   * Route trigger clicks to the thinking pane rather than toggling the
   * inline collapsible. Unset callers keep the inline expand/collapse.
   */
  onInspect?: () => void;
  /**
   * Whether the section has content worth expanding. Defaults to `true`
   * so existing callers keep the collapsible behavior. When `false`, the
   * collapsible is locked closed and the trigger renders as a flat,
   * non-interactive header (see `ReasoningTrigger` / `ReasoningContent`).
   */
  expandable?: boolean;
  /**
   * Measured reasoning generation rate (tokens/sec) for this section's
   * turn, appended to the settled trigger label (``Thought for Ns · NN t/s``).
   */
  tps?: number;
};

export const Reasoning = memo(
  ({
    className,
    isStreaming = false,
    duration,
    expandable = true,
    tps,
    onInspect,
    children,
    ...props
  }: ReasoningProps) => {
    // Open state follows isStreaming by default, but a manual user
    // toggle takes precedence until the next time streaming starts.
    // Resetting on the next stream means a new reasoning section
    // doesn't inherit the user's choice from the previous one.
    // When `onInspect` is set the section never expands inline — clicks
    // open the thinking pane — so streaming alone drives the open state.
    const [userOverride, setUserOverride] = useState<boolean | null>(null);
    // A non-expandable section is always closed — there's nothing inside.
    const isOpen = expandable ? (onInspect ? isStreaming : (userOverride ?? isStreaming)) : false;

    useEffect(() => {
      if (isStreaming) setUserOverride(null);
    }, [isStreaming]);

    const handleOpenChange = useCallback(
      (newOpen: boolean) => {
        if (onInspect) onInspect();
        else setUserOverride(newOpen);
      },
      [onInspect],
    );

    const contextValue = useMemo(
      () => ({ duration, isOpen, isStreaming, onInspect, expandable, tps }),
      [duration, isOpen, isStreaming, onInspect, expandable, tps],
    );

    return (
      <ReasoningContext.Provider value={contextValue}>
        <Collapsible
          className={cn("not-prose", className)}
          // Radix locks interaction when disabled — keeps the header from
          // being clickable / focusable when there's nothing to reveal.
          disabled={!expandable}
          onOpenChange={handleOpenChange}
          open={isOpen}
          {...props}
        >
          {children}
        </Collapsible>
      </ReasoningContext.Provider>
    );
  },
);

export type ReasoningTriggerProps = ComponentProps<typeof CollapsibleTrigger> & {
  getThinkingMessage?: (isStreaming: boolean, duration?: number, tps?: number) => ReactNode;
};

const defaultGetThinkingMessage = (isStreaming: boolean, duration?: number, tps?: number) => {
  if (isStreaming) {
    // Glimmer the label while the thought is in flight, matching the
    // "Working…" status pill — the shimmer settles into static text once
    // the section stops streaming.
    return (
      <Shimmer as="span" duration={1.5}>
        Thinking...
      </Shimmer>
    );
  }
  // The measured rate arrives on the turn's usage frame — append it when
  // the forwarder observed the reasoning delta stream.
  const tpsSuffix = tps !== undefined ? ` · ${formatTps(tps)} t/s` : "";
  if (duration === undefined) {
    return <span>Thought for a few seconds{tpsSuffix}</span>;
  }
  return (
    <span>
      Thought for {duration.toFixed(2)}s{tpsSuffix}
    </span>
  );
};

export const ReasoningTrigger = memo(
  ({
    className,
    children,
    getThinkingMessage = defaultGetThinkingMessage,
    ...props
  }: ReasoningTriggerProps) => {
    const { isStreaming, isOpen, duration, expandable, tps, onInspect } = useReasoning();

    const label = (
      <>
        <SparklesIcon className="size-3.5 shrink-0" />
        <span className="min-w-0 flex-1 truncate">
          {getThinkingMessage(isStreaming, duration, tps)}
        </span>
        {expandable && (
          <ChevronRightIcon
            className={cn(
              "size-3.5 shrink-0 transition-transform",
              // With onInspect the chevron stays pointing right — it
              // previews that the section opens in the side pane, and
              // inline expansion never happens.
              !onInspect && isOpen ? "rotate-90" : "rotate-0",
            )}
          />
        )}
      </>
    );

    // Nothing to reveal: render a flat, non-interactive header (no
    // chevron, no button semantics, no hover/pointer affordance) rather
    // than a collapsible trigger that expands into emptiness.
    if (!expandable) {
      return (
        <div
          className={cn(
            "flex w-full items-center gap-1.5 py-0.5 text-left text-muted-foreground text-sm",
            className,
          )}
        >
          {children ?? label}
        </div>
      );
    }

    return (
      <CollapsibleTrigger
        className={cn(
          "flex w-full cursor-pointer items-center gap-1.5 py-0.5 text-left text-muted-foreground text-sm transition-colors hover:text-foreground",
          className,
        )}
        {...props}
      >
        {children ?? label}
      </CollapsibleTrigger>
    );
  },
);

export type ReasoningContentProps = ComponentProps<typeof CollapsibleContent> & {
  children: string;
};

/**
 * The reasoning body as rendered markdown — shared by the inline
 * collapsible content and the thinking pane so both show identical
 * output. Rendered at normal body size in full contrast: reasoning is
 * real content the user reads, not secondary chrome.
 */
export const ReasoningMarkdown = memo(({ children }: { children: string }) => {
  const themeMode = useResolvedThemeMode();
  const normalizedChildren = useMemo(() => normalizeExplicitMathDelimiters(children), [children]);
  const mermaidOptions = useMemo(() => mermaidOptionsForTheme(themeMode), [themeMode]);

  return (
    <MarkdownErrorBoundary source={normalizedChildren}>
      {/* Streamdown is memoized and its comparator ignores the mermaid prop,
          so remount on theme change to recolor already-rendered diagrams. */}
      <Streamdown
        {...MERMAID_STREAMDOWN_PROPS}
        key={themeMode}
        plugins={STREAMDOWN_PLUGINS}
        mermaid={mermaidOptions}
        // Let links open on a plain click (and cmd/ctrl-click in a new tab)
        // instead of Streamdown's default "Open external link?" modal.
        linkSafety={CHAT_LINK_SAFETY}
        // Block remote image fetches that can exfiltrate data through URLs.
        rehypePlugins={SECURE_STREAMDOWN_REHYPE_PLUGINS}
      >
        {normalizedChildren}
      </Streamdown>
    </MarkdownErrorBoundary>
  );
});

export const ReasoningContent = memo(({ className, children, ...props }: ReasoningContentProps) => {
  const { expandable } = useReasoning();

  // Non-expandable section has no content to reveal — render nothing so
  // there's no empty collapsible region under the flat header.
  if (!expandable) return null;

  return (
    <CollapsibleContent
      className={cn(
        "mt-1 ml-2 border-l pl-3 py-1 text-ui",
        // Measured-height open/close like the worked fold: an instant
        // appear/disappear plus translate makes each streamed reasoning
        // section pop the transcript's height mid-turn.
        "turn-fold-content text-foreground outline-none",
        className,
      )}
      {...props}
    >
      <ReasoningMarkdown>{children}</ReasoningMarkdown>
    </CollapsibleContent>
  );
});

Reasoning.displayName = "Reasoning";
ReasoningTrigger.displayName = "ReasoningTrigger";
ReasoningContent.displayName = "ReasoningContent";
ReasoningMarkdown.displayName = "ReasoningMarkdown";
