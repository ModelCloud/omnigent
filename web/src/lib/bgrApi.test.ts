import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getBgrLaunchCapabilities, launchBgr, type BgrLaunchRequest } from "./bgrApi";
import { emptyBgrDraft } from "./bgrDraft";
import { authenticatedFetch } from "./identity";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

function request(): BgrLaunchRequest {
  const config = emptyBgrDraft();
  const profile = {
    harness: "codex-native",
    model: "review-model",
    provider: "simulated",
    variant: "",
    speed: "2.5",
    reasoning: "",
  };
  config.brain = { ...profile };
  config.grunt = { ...profile };
  config.reviewers = [{ ...profile }, { ...profile, provider: "second-provider" }];
  return {
    teamId: "a".repeat(32),
    agentId: "registered-multidex",
    workspace: " /repo ",
    config,
    initialPrompt: "Review this PR.",
    projectId: "project-a",
  };
}

beforeEach(() => vi.mocked(authenticatedFetch).mockReset());
afterEach(() => vi.restoreAllMocks());

it("negotiates on the explicit host route and forwards cancellation", async () => {
  const capabilities = {
    launch_supported: true,
    supported_harnesses: ["codex-native"],
    reviewer_isolation: true,
  };
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(JSON.stringify(capabilities)));
  const signal = new AbortController().signal;
  expect(await getBgrLaunchCapabilities("host/one", signal)).toEqual(capabilities);
  expect(authenticatedFetch).toHaveBeenCalledExactlyOnceWith(
    "/v1/hosts/host%2Fone/bgr/capabilities",
    { signal },
  );
});

it("does not treat incomplete capability advertisement as launch support", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(
    new Response(JSON.stringify({ launch_supported: true })),
  );
  await expect(getBgrLaunchCapabilities("host-a")).rejects.toThrow("invalid BGR capabilities");
});

it("normalizes profiles without a draft version and preserves the retry key across retries", async () => {
  vi.mocked(authenticatedFetch).mockImplementation(
    async () =>
      new Response(JSON.stringify({ state: "starting", brain_session_id: "brain-session" })),
  );
  const input = request();
  expect(await launchBgr("host-a", input)).toEqual({
    state: "starting",
    brainSessionId: "brain-session",
  });
  await launchBgr("host-a", input);
  const [url, options] = vi.mocked(authenticatedFetch).mock.calls[0];
  expect(url).toBe("/v1/hosts/host-a/bgr/launch");
  expect(options?.method).toBe("POST");
  const body = JSON.parse(options?.body as string);
  expect(body).toEqual({
    team_id: "a".repeat(32),
    agent_id: input.agentId,
    workspace: "/repo",
    config: {
      brain: { ...input.config.brain, variant: null, reasoning: null },
      grunt: { ...input.config.grunt, variant: null, reasoning: null },
      reviewers: input.config.reviewers.map((profile) => ({
        ...profile,
        variant: null,
        reasoning: null,
      })),
      max_active_grunts: 4,
    },
    initial_prompt: input.initialPrompt,
    project_id: input.projectId,
  });
  expect(vi.mocked(authenticatedFetch).mock.calls[1]).toEqual([url, options]);
  expect(input.config.brain.variant).toBe("");
});

it.each([
  { state: "blocked", reason: "Isolation unavailable" },
  { state: "running" },
  { brain_session_id: "brain-session" },
])("rejects an unconfirmed or blocked launch (%s)", async (response) => {
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(JSON.stringify(response)));
  await expect(launchBgr("host-a", request())).rejects.toThrow(
    "reason" in response ? "Isolation unavailable" : /did not confirm/,
  );
});

it("preserves server errors rather than manufacturing a successful session", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(
    new Response(JSON.stringify({ error: { code: "unavailable", message: "Reconnect host." } }), {
      status: 503,
    }),
  );
  await expect(launchBgr("host-a", request())).rejects.toThrow("Reconnect host.");
});

it.each([
  { initialPrompt: " " },
  { workspace: " " },
  { teamId: "not-a-team" },
  { config: emptyBgrDraft() },
])(
  "blocks invalid drafts, retry keys and empty task/workspace before any request (%s)",
  async (invalid) => {
    await expect(launchBgr("host-a", { ...request(), ...invalid })).rejects.toThrow();
    expect(authenticatedFetch).not.toHaveBeenCalled();
  },
);
