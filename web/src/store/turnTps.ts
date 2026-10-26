import { create } from "zustand";

/** Latest completion's estimated rates, scoped to its containing turn. */
export interface TurnTps {
  reasoning?: number;
  output?: number;
}

interface TurnTpsState {
  /** Encoded [conversationId, responseId] -> latest completion estimate. */
  byKey: Record<string, TurnTps>;
  /**
   * Replace the whole sample so unavailable channels never retain old rates.
   */
  record: (conversationId: string, responseId: string, rates: TurnTps) => void;
}

const key = (conversationId: string, responseId: string) =>
  JSON.stringify([conversationId, responseId]);

/**
 * Completion-level token counts divided by observed delta spans. Never attach
 * these estimates to individual reasoning blocks: their token counts are unknown.
 */
export const useTurnTpsStore = create<TurnTpsState>((set) => ({
  byKey: {},
  record: (conversationId, responseId, rates) =>
    set((s) => {
      const k = key(conversationId, responseId);
      return {
        byKey: {
          ...s.byKey,
          [k]: rates,
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
