import type { NativeModelOption } from "@/lib/types";

/** Add host-advertised models missing from a session's older native catalog. */
export function mergeNativeModelOptions(
  sessionOptions: readonly NativeModelOption[],
  hostOptions: readonly NativeModelOption[] | undefined,
): NativeModelOption[] {
  if (!hostOptions?.length) return sessionOptions as NativeModelOption[];
  const seen = new Set(sessionOptions.map((option) => option.id));
  const missing = hostOptions.filter((option) => !seen.has(option.id));
  return missing.length ? [...sessionOptions, ...missing] : (sessionOptions as NativeModelOption[]);
}
