import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, fireEvent, userEvent, waitFor, within } from "storybook/test";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import type { NativeModelOption } from "@/lib/types";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { CreateScheduledTaskDialog } from "./CreateScheduledTaskDialog";

const agent = (
  overrides: Partial<AvailableAgent> &
    Pick<AvailableAgent, "id" | "name" | "display_name" | "harness">,
): AvailableAgent => ({
  description: `${overrides.display_name} integration`,
  skills: [],
  builtin: true,
  ...overrides,
});

const AGENTS: AvailableAgent[] = [
  agent({
    id: "agent-claude",
    name: "claude-native-ui",
    display_name: "Claude Code",
    harness: "claude-native",
  }),
  agent({
    id: "agent-multidex",
    name: "codex-native-ui",
    display_name: "MultiDex",
    harness: "codex-native",
  }),
  agent({
    id: "agent-pi",
    name: "pi-native-ui",
    display_name: "Pi",
    harness: "pi-native",
  }),
  agent({
    id: "agent-polly",
    name: "polly",
    display_name: "Polly",
    harness: "claude-sdk",
  }),
];

const HOSTS: Host[] = [
  {
    host_id: "host-laptop",
    name: "dev-laptop",
    owner: "me",
    status: "online",
    sandbox_provider: null,
    configured_harnesses: { "codex-native": true },
  } as Host,
  {
    host_id: "host-server",
    name: "build-server",
    owner: "me",
    status: "online",
    sandbox_provider: null,
    configured_harnesses: { "claude-native": true, "pi-native": true },
  } as Host,
];

const MULTIDEX_MODEL_OPTIONS: NativeModelOption[] = [
  {
    id: "local-1017",
    modelFamily: "ds",
    modelLabel: "DeepSeek v4.1 Flash",
    providerId: "multidex",
    providerHealth: "online",
    speed: 1.0,
    isDefault: true,
    supportedReasoningEfforts: [{ reasoningEffort: "none" }, { reasoningEffort: "max" }],
    defaultReasoningEffort: "max",
  },
  {
    id: "openrouter-1017",
    modelFamily: "ds",
    modelLabel: "DeepSeek v4.1 Flash",
    providerId: "openrouter",
    providerHealth: "online",
    speed: 1.25,
    supportedReasoningEfforts: [{ reasoningEffort: "none" }, { reasoningEffort: "max" }],
    defaultReasoningEffort: "max",
  },
  ...(["regular", "fast", "ultra"] as const).map((variant, i): NativeModelOption => ({
    id: `lithos-${variant}`,
    modelFamily: "ds",
    modelLabel: "DeepSeek v4.1 Flash",
    providerId: "lithos",
    variant,
    providerHealth: "online",
    speed: [1.25, 1.5, 2.0][i],
    supportedReasoningEfforts: [{ reasoningEffort: "none" }, { reasoningEffort: "max" }],
    defaultReasoningEffort: "max",
  })),
];

function DialogStory() {
  return (
    <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
      <StoryQueryRouter
        seed={(queryClient) => {
          queryClient.setQueryData(["available-agents-catalog"], {
            agents: AGENTS,
            suppressedNames: new Set<string>(),
          });
          queryClient.setQueryData(["hosts", { includeSandbox: false }], HOSTS);
          queryClient.setQueryData(
            ["host-model-options", "host-laptop", "codex-native"],
            MULTIDEX_MODEL_OPTIONS,
          );
        }}
      >
        {/* The picker discovers agents from owned sessions too — the provider's
            `mine` query just errors with no server, which the hook degrades to
            an empty discovery (catalog rows still render via placeholder). */}
        <SidebarDataProvider>
          <CreateScheduledTaskDialog open onOpenChange={() => undefined} />
        </SidebarDataProvider>
      </StoryQueryRouter>
    </CapabilitiesProvider>
  );
}

const meta = {
  title: "Components/Scheduled/CreateScheduledTaskDialog",
  component: CreateScheduledTaskDialog,
  tags: ["visual-snapshot"],
  args: { open: true, onOpenChange: () => undefined },
} satisfies Meta<typeof CreateScheduledTaskDialog>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Default open: the agent picker + model/effort surface for the first agent. */
export const Create: Story = {
  render: () => <DialogStory />,
};

/** MultiDex picked: model + effort show without the Claude-only permission row. */
export const CodexHarness: Story = {
  render: () => <DialogStory />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement.ownerDocument.body);
    const trigger = canvas.getByTestId("task-agent-picker").querySelector("button");
    await userEvent.click(trigger!);
    const multidex = await canvas.findByText("MultiDex");
    await userEvent.click(multidex);
    await expect(canvas.getByTestId("task-model-effort-field")).toBeInTheDocument();
    await expect(canvas.queryByTestId("task-permission-control")).not.toBeInTheDocument();
  },
};

/** MultiDex + pinned host: Model → Provider → Speed → Reasoning with bolts. */
export const CodexHarnessGrouped: Story = {
  render: () => <DialogStory />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement.ownerDocument.body);
    // Pin the host serving the MultiDex catalog, then pick MultiDex.
    await userEvent.click(canvas.getByTestId("task-host-trigger"));
    await userEvent.click(await canvas.findByRole("option", { name: /dev-laptop/ }));
    const trigger = canvas
      .getByTestId("task-agent-picker")
      .querySelector<HTMLButtonElement>("button")!;
    // The trigger is disabled until the (seeded) agent catalog resolves, and
    // carries pointer-events:none while it is — open the dropdown with a raw
    // pointerdown once it reports enabled instead of userEvent's hit-test.
    await waitFor(() => expect(trigger).not.toBeDisabled());
    fireEvent.pointerDown(trigger);
    fireEvent.pointerUp(trigger);
    fireEvent.click(trigger);
    await userEvent.click(await canvas.findByText("MultiDex"));

    const grouped = await canvas.findByTestId("native-model-selectors");
    const comboboxes = within(grouped)
      .getAllByRole("combobox")
      .map((node) => node.getAttribute("aria-label"));
    await expect(comboboxes).toEqual(["Model", "Provider", "Reasoning"]);
    // Provider options expose the speed bolts (no numeric values).
    await userEvent.click(within(grouped).getByRole("combobox", { name: "Provider" }));
    await expect(
      canvas.getAllByRole("option").some((option) => option.textContent?.includes("⚡")),
    ).toBe(true);
  },
};
