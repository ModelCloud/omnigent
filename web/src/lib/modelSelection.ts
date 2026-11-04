import type { NativeModelOption } from "./types";
import { findNativeModelOption } from "./codexNativeModels";

export function hasGroupedModelOptions(options: readonly NativeModelOption[]): boolean {
  return options.some((row) => Boolean(row.modelFamily));
}

/** Keep an explicit effort only when the selected row (or default) advertises it. */
export function compatibleModelEffort(
  options: readonly NativeModelOption[],
  model: string | null,
  effort: string | null,
): string | null {
  const row = model
    ? findNativeModelOption(options, model)
    : options.find((option) => option.isDefault);
  return effort && row?.supportedReasoningEfforts?.some((e) => e.reasoningEffort === effort)
    ? effort
    : null;
}

export function familyId(row: NativeModelOption): string {
  return row.modelFamily ?? row.id;
}

export function providerId(row: NativeModelOption): string {
  return row.providerId ?? row.source?.name ?? row.source?.kind ?? "openai";
}

export function variantId(row: NativeModelOption): string {
  return row.variant ?? "regular";
}

/** Select only combinations advertised by the runner. Regular never implies Fast. */
export function resolveModelSelection(
  options: readonly NativeModelOption[],
  family: string,
  provider?: string,
  variant?: string,
): NativeModelOption | undefined {
  const models = options.filter((row) => familyId(row) === family);
  const providers = provider ? models.filter((row) => providerId(row) === provider) : models;
  if (providers.length === 0) return undefined;
  return (
    providers.find((row) => variantId(row) === (variant ?? "regular")) ??
    providers.find((row) => variantId(row) === "regular")
  );
}
