import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
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
    id: "agent-localdex",
    name: "codex-native-ui",
    display_name: "LocalDex",
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

function DialogStory() {
  return (
    <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
      <StoryQueryRouter
        seed={(queryClient) => {
          queryClient.setQueryData(["available-agents-catalog"], AGENTS);
          queryClient.setQueryData(["hosts", { includeSandbox: false }], HOSTS);
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

/** LocalDex picked: model + effort show without the Claude-only permission row. */
export const CodexHarness: Story = {
  render: () => <DialogStory />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement.ownerDocument.body);
    const trigger = canvas.getByTestId("task-agent-picker").querySelector("button");
    await userEvent.click(trigger!);
    const localdex = await canvas.findByText("LocalDex");
    await userEvent.click(localdex);
    await expect(canvas.getByTestId("task-model-effort-field")).toBeInTheDocument();
    await expect(canvas.queryByTestId("task-permission-control")).not.toBeInTheDocument();
  },
};
