import { describe, expect, it } from "vitest";
import { mergeNativeModelOptions } from "./mergeNativeModelOptions";

describe("mergeNativeModelOptions", () => {
  it("fills OpenAI choices absent from an older session catalog while preserving session rows", () => {
    const local = { id: "DS v4.1 Flash 1017", displayName: "DeepSeek v4.1 Flash" };
    const hostLocal = { ...local, displayName: "Changed host label" };
    const openai = { id: "gpt-6-astra", displayName: "GPT-6-Astra" };
    expect(mergeNativeModelOptions([local], [hostLocal, openai])).toEqual([local, openai]);
  });

  it("keeps the same array when the host has no additional choices", () => {
    const options = [{ id: "gpt-6-astra", displayName: "GPT-6-Astra" }];
    expect(mergeNativeModelOptions(options, options)).toBe(options);
  });
});
