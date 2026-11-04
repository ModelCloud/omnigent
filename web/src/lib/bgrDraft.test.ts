import { afterEach, expect, it, vi } from "vitest";
import {
  bgrDraftStorageKey,
  bgrIncompleteProfiles,
  bgrProfileFromCatalog,
  emptyBgrDraft,
  readBgrDraft,
  validateBgrLaunchDraft,
  writeBgrDraft,
} from "./bgrDraft";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

it("round-trips all profile axes, incomplete drafts and independent reviewer profiles", () => {
  const draft = emptyBgrDraft();
  expect(bgrIncompleteProfiles(draft)).toEqual(["Brain", "Grunt default", "Reviewer 1"]);
  draft.brain = {
    harness: "codex-native",
    provider: "provider-a",
    model: "wire-model",
    variant: "fast",
    speed: "2.5",
    reasoning: "high",
  };
  draft.reviewers.push({ ...draft.brain, provider: "provider-b" });
  const key = bgrDraftStorageKey("server/user/project", "host-a")!;
  expect(writeBgrDraft(key, draft)).toBeNull();
  expect(readBgrDraft(key)).toEqual({ draft, error: null });
  expect(bgrIncompleteProfiles(draft)).toEqual(["Grunt default", "Reviewer 1"]);
  for (const [scope, host] of [
    ["other-server/user/project", "host-a"],
    ["server/other-user/project", "host-a"],
    ["server/user/other-project", "host-a"],
    ["server/user/project", "host-b"],
  ]) {
    expect(readBgrDraft(bgrDraftStorageKey(scope, host)).draft).toEqual(emptyBgrDraft());
  }
  expect(bgrDraftStorageKey(null, "host-a")).toBeNull();
  expect(bgrDraftStorageKey("scope", null)).toBeNull();
  expect(writeBgrDraft(null, draft)).toMatch(/identity/);
});

it.each([1, 64])("accepts cap boundary %s and multibyte profile field boundaries", (cap) => {
  const draft = emptyBgrDraft();
  draft.max_active_grunts = cap;
  draft.brain.model = "𐀀".repeat(256);
  expect(writeBgrDraft("draft", draft)).toBeNull();
  expect(readBgrDraft("draft").draft).toEqual(draft);
  draft.brain.model += "𐀀";
  expect(writeBgrDraft("draft", draft)).toMatch(/256/);
  expect(readBgrDraft("draft").draft.brain.model).toHaveLength(512);
});

it.each([0, 65, 1.5, NaN])("rejects invalid cap %s without overwriting a saved draft", (cap) => {
  const draft = emptyBgrDraft();
  expect(writeBgrDraft("draft", draft)).toBeNull();
  expect(writeBgrDraft("draft", { ...draft, max_active_grunts: cap })).toMatch(/not saved/);
  expect(readBgrDraft("draft").draft).toEqual(draft);
});

it.each([
  "{",
  '{"version":99}',
  JSON.stringify({ ...emptyBgrDraft(), reviewers: [] }),
  JSON.stringify({ ...emptyBgrDraft(), reviewers: Array(3).fill(emptyBgrDraft().brain) }),
])("reports invalid saved data without deleting it (%s)", (raw) => {
  localStorage.setItem("draft", raw);
  expect(readBgrDraft("draft").error).not.toBeNull();
  expect(localStorage.getItem("draft")).toBe(raw);
});

it("reports storage failures and never claims a save succeeded", () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota");
  });
  expect(writeBgrDraft("draft", emptyBgrDraft())).toMatch(/not saved/);
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("denied");
  });
  expect(readBgrDraft("draft").error).toMatch(/Could not read/);
});

it("copies advertised routing metadata, not labels/fallbacks, while retaining reasoning", () => {
  const profile = { ...emptyBgrDraft().brain, reasoning: "custom", harness: "codex-native" };
  expect(
    bgrProfileFromCatalog(profile, {
      id: "routed-id",
      displayName: "Pretty name",
      model: "upstream-model",
      providerId: "actual-provider",
      variant: "ultra",
      speed: 2.5,
    }),
  ).toEqual({
    ...profile,
    model: "routed-id",
    provider: "actual-provider",
    variant: "ultra",
    speed: "2.5",
  });
  expect(
    bgrProfileFromCatalog(profile, {
      id: "only-id",
      source: { kind: "display-only", label: "Not provider identity" },
    }),
  ).toEqual({ ...profile, model: "only-id" });
});

it("requires complete role profiles and valid limits for launch, not merely saveable drafts", () => {
  const draft = emptyBgrDraft();
  expect(validateBgrLaunchDraft(draft)).toMatch(/model and provider/);
  for (const profile of [draft.brain, draft.grunt, ...draft.reviewers]) {
    profile.model = "model";
    profile.provider = "provider";
  }
  expect(validateBgrLaunchDraft(draft)).toMatch(/harness/);
  for (const profile of [draft.brain, draft.grunt, ...draft.reviewers]) {
    profile.harness = "codex-native";
  }
  expect(validateBgrLaunchDraft(draft)).toBeNull();
  draft.max_active_grunts = 1.5;
  expect(validateBgrLaunchDraft(draft)).toMatch(/1–64/);
  draft.max_active_grunts = 4;
  draft.reviewers[0].model = " ";
  expect(validateBgrLaunchDraft(draft)).toMatch(/model and provider/);
});
