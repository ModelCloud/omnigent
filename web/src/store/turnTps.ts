import { create } from "zustand";

/** Measured generation rates for one turn, in tokens/sec. */
export interface TurnTps {
  reasoning?: number;
  output?: number;
}

interface TurnTpsState {
  /** `${conversationId}:${responseId}` -> measured rates for that turn. */
  byKey: Record<string, TurnTps>;
  /**
   * Merge a fresh measurement for one response. Rate fields absent on the
   * event keep the previously recorded value — a mid-turn frame may carry
   * only one channel.
   */
  record: (conversationId: string, responseId: string, rates: TurnTps) => void;
}

const key = (conversationId: string, responseId: string) => `${conversationId}${responseId}`;

/**
 * Per-response tokens/sec measured by the codex-native forwarder (delta-stream
 * span ÷ token count — queue, prompt processing and network transfer are
 * excluded). Arrives on ``session.usage``; read by the reasoning trigger and
 * the worked fold to render "… · NN t/s" on the turn it belongs to.
 */
export const useTurnTpsStore = create<TurnTpsState>((set) => ({
  byKey: {},
  record: (conversationId, responseId, rates) =>
    set((s) => {
      const k = key(conversationId, responseId);
      const prior = s.byKey[k];
      return {
        byKey: {
          ...s.byKey,
          [k]: {
            reasoning: rates.reasoning ?? prior?.reasoning,
            output: rates.output ?? prior?.output,
          },
        },
      };
    }),
}));

export const turnTpsKey = key;

/** Format a tokens/sec rate for inline labels: `137`, `42.7`, `0.8`. */
export function formatTps(tps: number): string {
  if (tps >= 100) return String(Math.round(tps));
  return tps.toFixed(1);
}
