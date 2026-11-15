import { afterEach, expect, it, vi } from "vitest";
import { confirmBgrLaunchAttempt, reserveBgrLaunchAttempt } from "./bgrLaunchAttempt";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

it("reuses a pending team identity across fresh calls until its launch is confirmed", () => {
  const first = reserveBgrLaunchAttempt("attempt", "request-a");
  expect(first.teamId).toMatch(/^[0-9a-f]{32}$/);
  expect(reserveBgrLaunchAttempt("attempt", "request-a")).toEqual(first);
  confirmBgrLaunchAttempt("attempt", first);
  expect(localStorage.getItem("attempt")).toBeNull();
  expect(reserveBgrLaunchAttempt("attempt", "request-a").teamId).not.toBe(first.teamId);
});

it("does not reuse another request or scope, or clear a newer request's identity", () => {
  const first = reserveBgrLaunchAttempt("attempt-a", "request-a");
  const newer = reserveBgrLaunchAttempt("attempt-a", "request-b");
  expect(newer.teamId).not.toBe(first.teamId);
  expect(reserveBgrLaunchAttempt("attempt-b", "request-b").teamId).not.toBe(newer.teamId);
  confirmBgrLaunchAttempt("attempt-a", first);
  expect(reserveBgrLaunchAttempt("attempt-a", "request-b")).toEqual(newer);
});

it.each(["{", "{}", '{"fingerprint":"request-a","teamId":"invalid"}'])(
  "preserves invalid retry state rather than silently requesting another team (%s)",
  (raw) => {
    localStorage.setItem("attempt", raw);
    expect(() => reserveBgrLaunchAttempt("attempt", "request-a")).toThrow(/no new team/);
    expect(localStorage.getItem("attempt")).toBe(raw);
  },
);

it.each(["getItem", "setItem"] as const)(
  "does not reserve a team when browser storage %s fails",
  (operation) => {
    vi.spyOn(Storage.prototype, operation).mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    expect(() => reserveBgrLaunchAttempt("attempt", "request-a")).toThrow(/Check browser storage/);
  },
);

it("never reports a confirmed launch as failed when its retry key cannot be removed", () => {
  const first = reserveBgrLaunchAttempt("attempt", "request-a");
  vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
    throw new Error("storage unavailable");
  });
  expect(() => confirmBgrLaunchAttempt("attempt", first)).not.toThrow();
  expect(reserveBgrLaunchAttempt("attempt", "request-a")).toEqual(first);
});
