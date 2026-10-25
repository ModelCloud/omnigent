import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import { familyId, providerId, resolveModelSelection, variantId } from "@/lib/modelSelection";
import type { NativeModelOption } from "@/lib/types";

/** Four dependent controls; every selection maps to one runner-advertised row. */
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
    localdex: "Local",
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
  function field(
    label: string,
    value: string | undefined,
    entries: readonly (readonly [string, string])[],
    change: (v: string) => void,
  ) {
    return (
      <div className="grid grid-cols-[5rem_1fr] items-center gap-2">
        <span className="text-xs text-muted-foreground">{label}</span>
        <Select
          value={value ?? ""}
          onValueChange={change}
          disabled={disabled || entries.length === 0}
        >
          <SelectTrigger aria-label={label} className="w-full">
            <SelectValue placeholder="—" />
          </SelectTrigger>
          <SelectContent>
            {entries.map(([id, text]) => (
              <SelectItem key={id} value={id}>
                {text}
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
      )}
      {field(
        "Speed",
        variant,
        variants.map((v) => [v, labels[v] ?? v]),
        (v) => family && choose(family, provider, v),
      )}
      {field(
        "Reasoning",
        effort ?? "__default__",
        efforts.length > 0
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
