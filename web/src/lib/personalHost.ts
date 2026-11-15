/** Recognizes the default host-name suffix or the remembered connection id. */

import type { Host } from "@/hooks/useHosts";

export const PERSONAL_HOST_ID_STORAGE_KEY = "omnigent:arca-host-id";

export function isPersonalHost(
  host: Pick<Host, "host_id" | "name">,
  storedPersonalHostId: string | null,
): boolean {
  return (
    host.name.endsWith("'s arca") ||
    (storedPersonalHostId !== null && host.host_id === storedPersonalHostId)
  );
}

/** Prefer the remembered personal remote host, then one with the default name and a live connection. */
export function findConnectedPersonalHost(
  hosts: readonly Host[],
  storedPersonalHostId: string | null,
): Host | undefined {
  const connectedHosts = hosts.filter((host) => host.status === "online" && !host.sandbox_provider);
  return (
    connectedHosts.find((host) => host.host_id === storedPersonalHostId) ??
    connectedHosts.find((host) => isPersonalHost(host, storedPersonalHostId))
  );
}

/** The host id last connected via the personal-host action, or null. */
export function readPersonalHostId(): string | null {
  try {
    return localStorage.getItem(PERSONAL_HOST_ID_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Remember (or with null, forget) the personal host id. */
export function writePersonalHostId(hostId: string | null): void {
  try {
    if (hostId === null) localStorage.removeItem(PERSONAL_HOST_ID_STORAGE_KEY);
    else localStorage.setItem(PERSONAL_HOST_ID_STORAGE_KEY, hostId);
  } catch {
    // Storage unavailable (private mode) — the personal host option stays
    // offered; reconnecting an already-connected host is harmless.
  }
}
