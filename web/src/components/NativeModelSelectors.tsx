import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import { familyId, providerId, resolveModelSelection, variantId } from "@/lib/modelSelection";
import { cn } from "@/lib/utils";
import type { NativeModelOption } from "@/lib/types";

/** One bolt per whole multiplier; a fractional remainder adds a bolt faded
 * vertically so e.g. 1.5 renders ⚡⚡ with the second bolt half-transparent. */
function SpeedBolts({ speed }: { speed: string }) {
  const value = Number(speed.split("–").pop());
  if (!Number.isFinite(value) || value <= 0) return null;
  const whole = Math.floor(value);
  const fraction = value - whole;
  const faded =
    fraction > 0
      ? `linear-gradient(to top, black ${fraction * 100}%, rgba(0,0,0,0.2) ${fraction * 100}%)`
      : undefined;
  return (
    <span aria-hidden className="text-amber-600 dark:text-amber-500">
      {"⚡".repeat(whole)}
      {faded && <span style={{ maskImage: faded, WebkitMaskImage: faded }}>⚡</span>}
    </span>
  );
}

/** Dependent choices; every selection maps to one runner-advertised row. */
export function NativeModelSelectors({
  options,
  selectedModel,
  effort,
  disabled,
  onModelChange,
  onEffortChange,
}: {
  options: readonly NativeModelOption[];
  selectedModel: string | null;
  effort: string | null;
  disabled?: boolean;
  onModelChange: (id: string) => void;
  onEffortChange: (effort: string | null) => void;
}) {
  const selected = findNativeModelOption(options, selectedModel);
  const families = [
    ...new Map(
      options.map((row) => [familyId(row), row.modelLabel ?? row.displayName ?? row.id]),
    ).entries(),
  ];
  const family = selected ? familyId(selected) : undefined;
  const familyRows = options.filter((row) => familyId(row) === family);
  const providers = [...new Set(familyRows.map(providerId))];
  const provider = selected ? providerId(selected) : undefined;
  const variants = [
    ...new Set(familyRows.filter((row) => providerId(row) === provider).map(variantId)),
  ];
  const variant = selected ? variantId(selected) : undefined;
  const efforts = selected?.supportedReasoningEfforts ?? [];
  const choose = (nextFamily: string, nextProvider?: string, nextVariant?: string) => {
    const row = resolveModelSelection(options, nextFamily, nextProvider, nextVariant);
    if (row) onModelChange(row.id);
  };
  const labels: Record<string, string> = {
    multidex: "Local",
    openrouter: "OpenRouter",
    lithos: "Lithos",
    openai: "OpenAI",
    regular: "Regular",
    fast: "Fast",
    ultra: "Ultra",
    off: "Off",
    none: "Off",
    low: "Low",
    medium: "Medium",
    high: "High",
    xhigh: "Extra high",
    max: "Max",
  };
  const healthByProvider: Record<string, "online" | "offline"> = Object.fromEntries(
    familyRows.flatMap((r) => (r.providerHealth ? [[providerId(r), r.providerHealth]] : [])),
  );
  const fmtSpeed = (n: number) => (Number.isInteger(n) ? n.toFixed(1) : `${n}`);
  const speedByProvider: Record<string, string> = Object.fromEntries(
    providers.map((p) => {
      const speeds = [
        ...new Set(
          familyRows
            .filter((r) => providerId(r) === p && typeof r.speed === "number")
            .map((r) => r.speed as number),
        ),
      ].sort((a, b) => a - b);
      if (!speeds.length) return [p, ""];
      return [
        p,
        speeds.length > 1
          ? `${fmtSpeed(speeds[0])}–${fmtSpeed(speeds[speeds.length - 1])}`
          : fmtSpeed(speeds[0]),
      ];
    }),
  );
  const speedByVariant: Record<string, string> = Object.fromEntries(
    familyRows
      .filter((r) => providerId(r) === provider && typeof r.speed === "number")
      .map((r) => [variantId(r), fmtSpeed(r.speed as number)]),
  );
  function field(
    label: string,
    value: string | undefined,
    entries: readonly (readonly [string, string])[],
    change: (v: string) => void,
    health?: Record<string, "online" | "offline">,
    speed?: Record<string, string>,
  ) {
    if (entries.length <= 1) return null;
    return (
      <div className="grid grid-cols-[5rem_minmax(0,1fr)] items-center gap-2">
        <span className="text-xs text-muted-foreground">{label}</span>
        <Select value={value ?? ""} onValueChange={change} disabled={disabled}>
          <SelectTrigger aria-label={label} className="w-full">
            <SelectValue placeholder="—" />
          </SelectTrigger>
          <SelectContent>
            {entries.map(([id, text]) => (
              <SelectItem key={id} value={id}>
                <span className="flex min-w-0 items-center gap-1.5">
                  {health?.[id] && (
                    <span
                      aria-hidden
                      className={cn(
                        "size-2 shrink-0 rounded-full",
                        health[id] === "online" ? "bg-success" : "bg-muted-foreground/50",
                      )}
                    />
                  )}
                  <span className="truncate">{text}</span>
                  {speed?.[id] && (
                    <span className="shrink-0">
                      <SpeedBolts speed={speed[id]} />
                      <span className="sr-only">, speed {speed[id]}</span>
                    </span>
                  )}
                  {health?.[id] && <span className="sr-only">, {health[id]}</span>}
                </span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    );
  }
  return (
    <div className="min-w-72 space-y-2 p-2" data-testid="native-model-selectors">
      {field("Model", family, families, (v) => {
        const sameProvider = options.some(
          (row) => familyId(row) === v && providerId(row) === provider,
        );
        choose(v, sameProvider ? provider : undefined);
      })}
      {field(
        "Provider",
        provider,
        providers.map((v) => [v, labels[v] ?? v]),
        (v) => family && choose(family, v),
        healthByProvider,
        speedByProvider,
      )}
      {field(
        "Speed",
        variant,
        variants.map((v) => [v, labels[v] ?? v]),
        (v) => family && choose(family, provider, v),
        undefined,
        speedByVariant,
      )}
      {field(
        "Reasoning",
        effort ?? "__default__",
        efforts.length > 1
          ? [
              [
                "__default__",
                selected?.defaultReasoningEffort
                  ? `Default (${labels[selected.defaultReasoningEffort] ?? selected.defaultReasoningEffort})`
                  : "Default",
              ],
              ...efforts.map(
                (v) => [v.reasoningEffort, labels[v.reasoningEffort] ?? v.reasoningEffort] as const,
              ),
            ]
          : [],
        (value) => onEffortChange(value === "__default__" ? null : value),
      )}
    </div>
  );
}
