import { z } from "zod";
import { randomUUID } from "./randomUUID";

const attemptSchema = z.strictObject({
  fingerprint: z.string(),
  teamId: z.string().regex(/^[0-9a-f]{32}$/),
});

export type BgrLaunchAttempt = z.infer<typeof attemptSchema>;

export function reserveBgrLaunchAttempt(key: string, fingerprint: string): BgrLaunchAttempt {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw !== null) {
      const saved = attemptSchema.parse(JSON.parse(raw));
      if (saved.fingerprint === fingerprint) return saved;
    }
    const attempt = { fingerprint, teamId: randomUUID().replaceAll("-", "") };
    window.localStorage.setItem(key, JSON.stringify(attempt));
    return attempt;
  } catch {
    throw new Error(
      "Could not save the BGR launch retry key. Check browser storage before launching; no new team was requested.",
    );
  }
}

export function confirmBgrLaunchAttempt(key: string, attempt: BgrLaunchAttempt): void {
  try {
    if (window.localStorage.getItem(key) === JSON.stringify(attempt)) {
      window.localStorage.removeItem(key);
    }
  } catch {
    // Keeping a confirmed retry key is safer than requesting another team.
  }
}
