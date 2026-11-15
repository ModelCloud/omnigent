import { z } from "zod";
import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";
import { validateBgrLaunchDraft, type BgrDraft, type BgrProfileDraft } from "./bgrDraft";

const capabilitiesSchema = z.object({
  launch_supported: z.boolean(),
  supported_harnesses: z.array(z.string()),
  reviewer_isolation: z.boolean(),
  reason: z.string().nullish(),
});

const launchSchema = z.object({
  brain_session_id: z.string().min(1).nullish(),
  state: z.enum(["starting", "running", "blocked"]),
  reason: z.string().nullish(),
});

export type BgrLaunchCapabilities = z.infer<typeof capabilitiesSchema>;
export interface BgrLaunchResult {
  brainSessionId: string;
  state: "starting" | "running";
}

export interface BgrLaunchRequest {
  teamId: string;
  agentId: string;
  workspace: string;
  config: BgrDraft;
  initialPrompt: string;
  projectId?: string | null;
}

export async function getBgrLaunchCapabilities(
  hostId: string,
  signal?: AbortSignal,
): Promise<BgrLaunchCapabilities> {
  const response = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/bgr/capabilities`,
    { signal },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  const parsed = capabilitiesSchema.safeParse(await response.json());
  if (!parsed.success) throw new Error("The host returned invalid BGR capabilities.");
  return parsed.data;
}

function profileForWire(profile: BgrProfileDraft) {
  const optional = (value: string) => value.trim() || null;
  return {
    harness: profile.harness.trim(),
    provider: profile.provider.trim(),
    model: profile.model.trim(),
    variant: optional(profile.variant),
    speed: optional(profile.speed),
    reasoning: optional(profile.reasoning),
  };
}

export async function launchBgr(
  hostId: string,
  request: BgrLaunchRequest,
): Promise<BgrLaunchResult> {
  const invalid = validateBgrLaunchDraft(request.config);
  if (invalid) throw new Error(invalid);
  if (!hostId || !request.agentId || !request.workspace.trim() || !request.initialPrompt.trim()) {
    throw new Error("Choose a connected host, registered agent, workspace and initial task.");
  }
  if (!/^[0-9a-f]{32}$/.test(request.teamId)) {
    throw new Error("BGR launch requires a valid team retry key.");
  }
  const response = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/bgr/launch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      team_id: request.teamId,
      agent_id: request.agentId,
      workspace: request.workspace.trim(),
      config: {
        brain: profileForWire(request.config.brain),
        grunt: profileForWire(request.config.grunt),
        reviewers: request.config.reviewers.map(profileForWire),
        max_active_grunts: request.config.max_active_grunts,
      },
      initial_prompt: request.initialPrompt,
      ...(request.projectId ? { project_id: request.projectId } : {}),
    }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  const parsed = launchSchema.safeParse(await response.json());
  if (!parsed.success) throw new Error("The server did not confirm a BGR launch.");
  if (parsed.data.state === "blocked") {
    throw new Error(parsed.data.reason || "The server blocked BGR launch.");
  }
  if (!parsed.data.brain_session_id) {
    throw new Error("The server did not confirm a Brain session.");
  }
  return { brainSessionId: parsed.data.brain_session_id, state: parsed.data.state };
}
