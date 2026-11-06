import { useId, useState } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { MODEL_SELECT_DEFAULT } from "@/components/HarnessConfigControls";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import { familyId, providerId, resolveModelSelection, variantId } from "@/lib/modelSelection";
import { cn } from "@/lib/utils";
import type { NativeModelOption } from "@/lib/types";

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
  modelDefaultLabel,
  onSelectOpenChange,
  className,
  configuration = false,
}: {
  options: readonly NativeModelOption[];
  selectedModel: string | null;
  effort: string | null;
  disabled?: boolean;
  onModelChange: (id: string) => void;
  onEffortChange: (effort: string | null) => void;
  /**
   * When set, the Model field gains a "no override" entry ahead of the
   * families: picking it calls `onModelChange("")` and shows while
   * `selectedModel` is empty. Dependent fields then resolve against the
   * catalog's `isDefault` row so Provider/Speed/Reasoning still pick from the
   * model the default launches as.
   */
  modelDefaultLabel?: string;
  /** Forwarded to each Select's onOpenChange (nested-dropdown dismiss guards). */
  onSelectOpenChange?: (open: boolean) => void;
  className?: string;
  /** Full draft form: explicit first pick, singleton values and no inferred provider. */
  configuration?: boolean;
}) {
  if (configuration) {
    return (
      <ConfigurationModelSelectors
        options={options}
        selectedModel={selectedModel}
        effort={effort}
        disabled={disabled}
        onModelChange={onModelChange}
        onEffortChange={onEffortChange}
        className={className}
      />
    );
  }
  const selected = findNativeModelOption(options, selectedModel);
  // No explicit pick under a "Default" entry still needs dependent rows: they
  // read off the catalog's own default, which is what launching with no
  // override resolves to.
  const effectiveSelected =
    selected ??
    (modelDefaultLabel !== undefined && !selectedModel
      ? (options.find((row) => row.isDefault) ?? null)
      : null);
  const families = [
    ...new Map(
      options.map((row) => [familyId(row), row.modelLabel ?? row.displayName ?? row.id]),
    ).entries(),
  ];
  const family = effectiveSelected ? familyId(effectiveSelected) : undefined;
  const familyRows = options.filter((row) => familyId(row) === family);
  const providers = [...new Set(familyRows.map(providerId))];
  const provider = effectiveSelected ? providerId(effectiveSelected) : undefined;
  const variants = [
    ...new Set(familyRows.filter((row) => providerId(row) === provider).map(variantId)),
  ];
  const variant = effectiveSelected ? variantId(effectiveSelected) : undefined;
  const efforts = effectiveSelected?.supportedReasoningEfforts ?? [];
  const choose = (nextFamily: string, nextProvider?: string, nextVariant?: string) => {
    const row = resolveModelSelection(options, nextFamily, nextProvider, nextVariant);
    if (row) onModelChange(row.id);
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
        <Select
          value={value ?? ""}
          onValueChange={change}
          disabled={disabled}
          onOpenChange={onSelectOpenChange}
        >
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
    <div className={cn("min-w-72 space-y-2 p-2", className)} data-testid="native-model-selectors">
      {field(
        "Model",
        modelDefaultLabel !== undefined && !selectedModel ? MODEL_SELECT_DEFAULT : family,
        [
          ...(modelDefaultLabel !== undefined
            ? [[MODEL_SELECT_DEFAULT, modelDefaultLabel] as const]
            : []),
          ...families,
        ],
        (v) => {
          if (v === MODEL_SELECT_DEFAULT && modelDefaultLabel !== undefined) {
            onModelChange("");
            return;
          }
          const sameProvider = options.some(
            (row) => familyId(row) === v && providerId(row) === provider,
          );
          choose(v, sameProvider ? provider : undefined);
        },
      )}
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
                effectiveSelected?.defaultReasoningEffort
                  ? `Default (${labels[effectiveSelected.defaultReasoningEffort] ?? effectiveSelected.defaultReasoningEffort})`
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

function ConfigurationModelSelectors({
  options,
  selectedModel,
  effort,
  disabled,
  onModelChange,
  onEffortChange,
  className,
}: Pick<
  Parameters<typeof NativeModelSelectors>[0],
  | "options"
  | "selectedModel"
  | "effort"
  | "disabled"
  | "onModelChange"
  | "onEffortChange"
  | "className"
>) {
  const id = useId();
  const [pending, setPending] = useState<{ family: string; provider?: string } | null>(null);
  const selected = options.find((row) => row.id === selectedModel);
  const family = pending?.family ?? (selected ? familyId(selected) : undefined);
  const familyRows = options.filter((row) => familyId(row) === family);
  // UI-only keys keep missing metadata distinct without inventing a wire provider.
  const providerKey = (row: NativeModelOption) =>
    row.providerId ? `known:${row.providerId}` : "unadvertised";
  const providers = [
    ...new Map(
      familyRows.map((row) => [
        providerKey(row),
        row.providerId ? (labels[row.providerId] ?? row.providerId) : "Not advertised",
      ]),
    ).entries(),
  ];
  const provider = pending
    ? (pending.provider ?? (providers.length === 1 ? providers[0][0] : undefined))
    : selected
      ? providerKey(selected)
      : undefined;
  const providerRows = familyRows.filter((row) => providerKey(row) === provider);
  const families = [
    ...new Map(
      options.map((row) => [familyId(row), row.modelLabel ?? row.displayName ?? row.id]),
    ).entries(),
  ];
  const apply = (row: NativeModelOption) => {
    setPending(null);
    onModelChange(row.id);
  };
  const choose = (nextFamily: string, nextProvider?: string) => {
    const candidates = options.filter(
      (row) =>
        familyId(row) === nextFamily &&
        (nextProvider === undefined || providerKey(row) === nextProvider),
    );
    const row = resolveModelSelection(candidates, nextFamily);
    if (row) apply(row);
    else setPending({ family: nextFamily, provider: nextProvider });
  };
  function field(
    label: string,
    value: string | undefined,
    entries: readonly (readonly [string, string])[],
    change: (value: string) => void,
    placeholder: string,
    help?: string,
  ) {
    const controlId = `${id}-${label.replaceAll(" ", "-")}`;
    const single = entries.length === 1 && entries[0][0] === value;
    return (
      <div className="min-w-0 space-y-1.5" role="group" aria-label={label}>
        {single || entries.length === 0 ? (
          <span className="text-sm font-medium">{label}</span>
        ) : (
          <label htmlFor={controlId} className="text-sm font-medium">
            {label}
          </label>
        )}
        {single || entries.length === 0 ? (
          <p id={controlId} className="break-words text-sm">
            {single ? entries[0][1] : placeholder}
          </p>
        ) : (
          <select
            id={controlId}
            className="h-9 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            value={value ?? ""}
            disabled={disabled}
            aria-describedby={help ? `${controlId}-help` : undefined}
            onChange={(event) => change(event.target.value)}
          >
            {!entries.some(([key]) => key === "") && (
              <option value="" disabled>
                {placeholder}
              </option>
            )}
            {entries.map(([key, text]) => (
              <option key={key} value={key}>
                {text}
              </option>
            ))}
          </select>
        )}
        {help && (
          <p id={`${controlId}-help`} className="text-xs text-muted-foreground">
            {help}
          </p>
        )}
      </div>
    );
  }
  const efforts = selected?.supportedReasoningEfforts ?? [];
  const unverifiedEffort = effort && !efforts.some((row) => row.reasoningEffort === effort);
  return (
    <div className={cn("min-w-0 space-y-3", className)} data-testid="native-model-selectors">
      {field("Model", family, families, (next) => choose(next), "Choose a model")}
      {field(
        "Provider",
        provider,
        providers,
        (next) => family !== undefined && choose(family, next),
        providers.length ? "Choose a provider" : "Choose a model first",
        "Service serving this model",
      )}
      {field(
        "Variant",
        pending ? undefined : selected?.id,
        providerRows.map((row) => [
          row.id,
          [
            labels[variantId(row)] ?? variantId(row),
            row.speed === undefined ? null : `${row.speed}×`,
          ]
            .filter(Boolean)
            .join(" · "),
        ]),
        (next) => {
          const row = providerRows.find((candidate) => candidate.id === next);
          if (row) apply(row);
        },
        providerRows.length ? "Choose a variant" : "Choose a model and provider first",
        "Relative endpoint speed, not an enforced execution mode.",
      )}
      {pending && (
        <p role="status" className="text-xs text-muted-foreground">
          Choose a variant to apply this model. Current profile values have not changed.
        </p>
      )}
      {field(
        "Reasoning",
        effort ?? "",
        efforts.length
          ? [
              [
                "",
                selected?.defaultReasoningEffort
                  ? `Default (${labels[selected.defaultReasoningEffort] ?? selected.defaultReasoningEffort})`
                  : "Default",
              ],
              ...efforts.map(
                (row) =>
                  [
                    row.reasoningEffort,
                    labels[row.reasoningEffort] ?? row.reasoningEffort,
                  ] as const,
              ),
              ...(unverifiedEffort ? [[effort, `Unverified: ${effort}`] as const] : []),
            ]
          : [],
        (next) => onEffortChange(next || null),
        effort ? `Unverified: ${effort}` : "Not advertised",
      )}
    </div>
  );
}
