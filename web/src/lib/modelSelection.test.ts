import { describe, expect, it } from "vitest";
import { resolveModelSelection } from "./modelSelection";
import type { NativeModelOption } from "./types";

const rows: NativeModelOption[] = [
  { id: "DS v4.1 Flash 1017", modelFamily: "ds", providerId: "localdex" },
  { id: "deepseek/deepseek-v4.1-flash-1017", modelFamily: "ds", providerId: "openrouter" },
  {
    id: "deepseek-ai/DeepSeek-V4.1-Flash-fast",
    modelFamily: "ds",
    providerId: "lithos",
    variant: "fast",
  },
  {
    id: "deepseek-ai/DeepSeek-V4.1-Flash",
    modelFamily: "ds",
    providerId: "lithos",
    variant: "regular",
  },
  {
    id: "deepseek-ai/DeepSeek-V4.1-Flash-ultra",
    modelFamily: "ds",
    providerId: "lithos",
    variant: "ultra",
  },
  { id: "gpt-6-sol", providerId: "openai" },
];

describe("model/provider/variant selection", () => {
  it("uses regular on a provider switch regardless of catalog order", () => {
    expect(resolveModelSelection(rows, "ds", "lithos")?.id).toBe("deepseek-ai/DeepSeek-V4.1-Flash");
  });
  it("routes an explicit speed through its provider's wire model id", () => {
    expect(resolveModelSelection(rows, "ds", "lithos", "ultra")?.id).toBe(
      "deepseek-ai/DeepSeek-V4.1-Flash-ultra",
    );
  });
  it("does not fabricate a provider or a speed unsupported by that provider", () => {
    expect(resolveModelSelection(rows, "gpt-6-sol", "lithos")).toBeUndefined();
    expect(resolveModelSelection(rows, "ds", "localdex", "ultra")?.id).toBe("DS v4.1 Flash 1017");
  });
  it("switches back to an official model without a local endpoint id", () => {
    expect(resolveModelSelection(rows, "gpt-6-sol", "openai")?.id).toBe("gpt-6-sol");
  });
  it("requires an explicit speed when only a paid faster variant is advertised", () => {
    const fastOnly = rows.filter((row) => row.variant === "fast");
    expect(resolveModelSelection(fastOnly, "ds", "lithos")).toBeUndefined();
    expect(resolveModelSelection(fastOnly, "ds", "lithos", "fast")?.variant).toBe("fast");
  });
});
