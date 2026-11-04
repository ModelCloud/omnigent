import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { getBgrLaunchCapabilities, launchBgr, type BgrLaunchResult } from "@/lib/bgrApi";
import { bgrDraftStorageKey, type BgrDraft } from "@/lib/bgrDraft";
import {
  confirmBgrLaunchAttempt,
  reserveBgrLaunchAttempt,
  type BgrLaunchAttempt,
} from "@/lib/bgrLaunchAttempt";
import { BgrConfigurationDialog } from "./BgrConfigurationDialog";

export function BgrLaunchDialog({
  scope,
  host,
  agents,
  harnessLabels,
  workspace,
  initialPrompt,
  projectId,
  onClose,
  onLaunched,
}: {
  scope: string | null;
  host: Host | undefined;
  agents: readonly AvailableAgent[];
  harnessLabels: Readonly<Record<string, string>>;
  workspace: string;
  initialPrompt: string;
  projectId: string | null;
  onClose: () => void;
  onLaunched: (result: BgrLaunchResult) => void;
}) {
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  const capabilities = useQuery({
    queryKey: ["bgr-capabilities", scope, host?.host_id],
    queryFn: ({ signal }) => getBgrLaunchCapabilities(host!.host_id, signal),
    enabled: scope !== null && host?.status === "online",
    retry: false,
    staleTime: 0,
  });
  const attempt = useRef<BgrLaunchAttempt | null>(null);
  const launchUnavailableReason =
    host?.status !== "online"
      ? "Connect the selected host before launching."
      : !scope
        ? "Wait for your server identity before launching."
        : !workspace.trim()
          ? "Choose a working directory in New Chat before launching."
          : capabilities.isError
            ? "Could not verify this host's BGR capabilities. Reopen BGR to retry."
            : !capabilities.data || capabilities.isFetching
              ? "Checking this host's BGR runtime capabilities…"
              : !capabilities.data.launch_supported || !capabilities.data.reviewer_isolation
                ? (capabilities.data.reason ??
                  "This host does not support BGR launch with isolated reviewers.")
                : null;

  const launch = async (draft: BgrDraft, task: string) => {
    if (!host || launchUnavailableReason) {
      throw new Error(launchUnavailableReason || "Select a connected host.");
    }
    const agent = agents.find((candidate) => candidate.harness === draft.brain.harness);
    if (!agent) throw new Error("No registered agent matches the brain's selected harness.");
    const request = {
      agentId: agent.id,
      workspace,
      config: draft,
      initialPrompt: task,
      projectId,
    };
    const fingerprint = JSON.stringify([scope, host.host_id, request]);
    const attemptKey = `${bgrDraftStorageKey(scope, host.host_id)}:launch-attempt`;
    if (attempt.current?.fingerprint !== fingerprint) {
      attempt.current = reserveBgrLaunchAttempt(attemptKey, fingerprint);
    }
    const pending = attempt.current;
    const result = await launchBgr(host.host_id, {
      ...request,
      teamId: pending.teamId,
    });
    confirmBgrLaunchAttempt(attemptKey, pending);
    if (active.current) onLaunched(result);
  };

  return (
    <BgrConfigurationDialog
      scope={scope}
      host={host}
      agents={agents}
      harnessLabels={harnessLabels}
      initialPrompt={initialPrompt}
      supportedHarnesses={capabilities.data?.supported_harnesses}
      onLaunch={launch}
      launchUnavailableReason={launchUnavailableReason}
      onClose={onClose}
    />
  );
}
