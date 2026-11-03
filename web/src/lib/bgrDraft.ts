import { z } from "zod";
import type { NativeModelOption } from "./types";

const label = z.string().refine((value) => [...value].length <= 256, "Use at most 256 characters");
const profileSchema = z.strictObject({
  harness: label,
  provider: label,
  model: label,
  variant: label,
  speed: label,
  reasoning: label,
});
const draftSchema = z.strictObject({
  version: z.literal(1),
  brain: profileSchema,
  grunt: profileSchema,
  reviewers: z.array(profileSchema).min(1).max(2),
  max_active_grunts: z.number().int().min(1).max(64),
});

export type BgrProfileDraft = z.infer<typeof profileSchema>;
export type BgrDraft = z.infer<typeof draftSchema>;

export function emptyBgrProfile(): BgrProfileDraft {
  return { harness: "", provider: "", model: "", variant: "", speed: "", reasoning: "" };
}

export function emptyBgrDraft(): BgrDraft {
  return {
    version: 1,
    brain: emptyBgrProfile(),
    grunt: emptyBgrProfile(),
    reviewers: [emptyBgrProfile()],
    max_active_grunts: 4,
  };
}

/** Scope already includes the server, authenticated user and project. */
export function bgrDraftStorageKey(scope: string | null, hostId: string | null): string | null {
  return scope && hostId ? `omnigent:bgr-draft:v1:${JSON.stringify([scope, hostId])}` : null;
}

export function readBgrDraft(key: string | null): { draft: BgrDraft; error: string | null } {
  if (key === null) return { draft: emptyBgrDraft(), error: null };
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return { draft: emptyBgrDraft(), error: null };
    const parsed = draftSchema.safeParse(JSON.parse(raw));
    if (parsed.success) return { draft: parsed.data, error: null };
    return {
      draft: emptyBgrDraft(),
      error:
        "Saved BGR draft is invalid or from an unsupported version. It has not been overwritten.",
    };
  } catch {
    return { draft: emptyBgrDraft(), error: "Could not read the BGR draft from browser storage." };
  }
}

export function writeBgrDraft(key: string | null, draft: BgrDraft): string | null {
  if (key === null) return "Select a host and wait for your server identity before saving a draft.";
  const parsed = draftSchema.safeParse(draft);
  if (!parsed.success) {
    return "Draft not saved. Use 1–64 active grunts, 1–2 reviewers and at most 256 characters per profile field.";
  }
  try {
    window.localStorage.setItem(key, JSON.stringify(parsed.data));
    return null;
  } catch {
    return "Draft not saved. Browser storage is unavailable or full; your edits are still here.";
  }
}

export function bgrIncompleteProfiles(draft: BgrDraft): string[] {
  const profiles: readonly (readonly [string, BgrProfileDraft])[] = [
    ["Brain", draft.brain],
    ["Grunt default", draft.grunt],
    ...draft.reviewers.map((profile, index) => [`Reviewer ${index + 1}`, profile] as const),
  ];
  return profiles.flatMap(([name, profile]) =>
    !profile.provider.trim() || !profile.model.trim() ? [name] : [],
  );
}

/** Only advertised metadata is copied; catalog fallbacks are not provider authority. */
export function bgrProfileFromCatalog(
  profile: BgrProfileDraft,
  row: NativeModelOption,
): BgrProfileDraft {
  return {
    ...profile,
    model: row.id,
    provider: row.providerId ?? "",
    variant: row.variant ?? "",
    speed: row.speed === undefined ? "" : String(row.speed),
  };
}
