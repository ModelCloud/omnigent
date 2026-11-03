import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { BgrConfigurationDialog } from "./BgrConfigurationDialog";
import { useHostModelOptions, type Host } from "@/hooks/useHosts";
import { testAgent } from "@/test/agentFixtures";
import { bgrDraftStorageKey, emptyBgrDraft, readBgrDraft, writeBgrDraft } from "@/lib/bgrDraft";

vi.mock("@/hooks/useHosts", () => ({ useHostModelOptions: vi.fn() }));
const host = { host_id: "host-a", name: "Test host", status: "online" } as Host;
const agents = [
  testAgent("codex", "MultiDex", { harness: "codex-native" }),
  testAgent("claude", "Claude", { harness: "claude-native" }),
];
const row = {
  id: "wire-fast",
  modelFamily: "test-family",
  displayName: "Test Model",
  providerId: "actual-provider",
  variant: "fast",
  speed: 2.5,
  supportedReasoningEfforts: [{ reasoningEffort: "high" }, { reasoningEffort: "low" }],
};
const key = bgrDraftStorageKey("server/user/project", "host-a")!;
const refetch = vi.fn();

beforeEach(() => {
  localStorage.clear();
  vi.mocked(useHostModelOptions).mockReturnValue({
    data: [row],
    isLoading: false,
    isError: false,
    refetch,
  } as unknown as ReturnType<typeof useHostModelOptions>);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderDialog(overrides: Partial<Parameters<typeof BgrConfigurationDialog>[0]> = {}) {
  return render(
    <BgrConfigurationDialog
      scope="server/user/project"
      host={host}
      agents={agents}
      onClose={vi.fn()}
      {...overrides}
    />,
  );
}

it("saves and reloads all six axes for one brain, dynamic grunts and two reviewers without a launch", () => {
  const { unmount } = renderDialog();
  expect(screen.getByRole("button", { name: "Launch BGR (unavailable)" })).toBeDisabled();
  expect(screen.getByText(/role enforcement and reviewer isolation/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Reviewers per publication"), { target: { value: "2" } });
  fireEvent.change(screen.getByLabelText("Maximum active grunts"), { target: { value: "64" } });
  for (const [index, name] of ["Brain", "Grunt default", "Reviewer 1", "Reviewer 2"].entries()) {
    const fields = within(screen.getByRole("group", { name }));
    fireEvent.change(fields.getByLabelText("Harness"), { target: { value: "codex-native" } });
    fireEvent.change(fields.getByLabelText("Catalog profile"), { target: { value: row.id } });
    expect(fields.getByLabelText("Provider (required for a team)")).toHaveValue("actual-provider");
    expect(fields.getByLabelText("Speed (optional)")).toHaveValue("2.5");
    fireEvent.change(fields.getByLabelText("Reasoning (optional)"), {
      target: { value: `custom-${index}` },
    });
  }
  expect(screen.getAllByTestId("native-model-selectors")[0]).toHaveClass("min-w-0");
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  expect(
    screen.getByText("Draft saved in this browser. No session was launched."),
  ).toBeInTheDocument();
  const { draft } = readBgrDraft(key);
  expect(draft.max_active_grunts).toBe(64);
  expect(draft.reviewers).toHaveLength(2);
  expect(draft.brain).toEqual({
    harness: "codex-native",
    model: row.id,
    provider: row.providerId,
    variant: row.variant,
    speed: "2.5",
    reasoning: "custom-0",
  });
  expect(draft.reviewers[1].reasoning).toBe("custom-3");
  unmount();
  renderDialog();
  expect(screen.getByLabelText("Maximum active grunts")).toHaveValue(64);
  expect(
    within(screen.getByRole("group", { name: "Reviewer 2" })).getByLabelText(
      "Reasoning (optional)",
    ),
  ).toHaveValue("custom-3");
  fireEvent.change(screen.getByLabelText("Reviewers per publication"), { target: { value: "1" } });
  expect(screen.queryByRole("group", { name: "Reviewer 2" })).not.toBeInTheDocument();
  expect(screen.getByText(/Unsaved changes/)).toBeInTheDocument();
});

it("preserves manual profile values across harness/catalog changes and reports missing catalog values", () => {
  const draft = emptyBgrDraft();
  draft.brain = {
    harness: "retired-harness",
    provider: "custom-provider",
    model: "missing-model",
    variant: "custom-variant",
    speed: "custom-speed",
    reasoning: "custom-reasoning",
  };
  writeBgrDraft(key, draft);
  renderDialog();
  const brain = within(screen.getByRole("group", { name: "Brain" }));
  expect(brain.getByLabelText("Harness")).toHaveValue("retired-harness");
  fireEvent.change(brain.getByLabelText("Harness"), { target: { value: "claude-native" } });
  expect(brain.getByLabelText("Provider (required for a team)")).toHaveValue("custom-provider");
  expect(brain.getByLabelText("Speed (optional)")).toHaveValue("custom-speed");
  expect(brain.getByText(/Saved model is not in this catalog/)).toBeInTheDocument();
  fireEvent.change(brain.getByLabelText("Catalog profile"), { target: { value: row.id } });
  expect(brain.getByLabelText("Reasoning (optional)")).toHaveValue("custom-reasoning");
  expect(brain.getByText(/reasoning value is not advertised/)).toBeInTheDocument();
});

it("shows invalid-cap, damaged draft and storage-write errors without claiming saved", () => {
  localStorage.setItem(key, '{"version":99}');
  renderDialog();
  expect(screen.getByRole("alert")).toHaveTextContent(/unsupported version/);
  fireEvent.change(screen.getByLabelText("Maximum active grunts"), { target: { value: "65" } });
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  expect(screen.getByRole("alert")).toHaveTextContent(/1–64/);
  fireEvent.change(screen.getByLabelText("Maximum active grunts"), { target: { value: "1" } });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("full");
  });
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  expect(screen.getByRole("alert")).toHaveTextContent(/Browser storage is unavailable or full/);
  expect(
    screen.queryByText("Draft saved in this browser. No session was launched."),
  ).not.toBeInTheDocument();
  expect(localStorage.getItem(key)).toBe('{"version":99}');
});

it.each([{ scope: null }, { host: undefined }])(
  "disables saving when scope or host is missing (%s)",
  (overrides) => {
    renderDialog(overrides);
    expect(screen.getByRole("button", { name: "Save draft" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Launch BGR (unavailable)" })).toBeDisabled();
  },
);

it("shows catalog loading/error/empty states with retry and preserves offline editing", () => {
  const draft = emptyBgrDraft();
  draft.brain.harness = "codex-native";
  writeBgrDraft(key, draft);
  vi.mocked(useHostModelOptions).mockReturnValue({ data: undefined, isLoading: true } as ReturnType<
    typeof useHostModelOptions
  >);
  const result = renderDialog();
  expect(screen.getByText("Loading model catalog…")).toBeInTheDocument();
  vi.mocked(useHostModelOptions).mockReturnValue({
    data: undefined,
    isError: true,
    refetch,
  } as unknown as ReturnType<typeof useHostModelOptions>);
  result.rerender(
    <BgrConfigurationDialog
      scope="server/user/project"
      host={host}
      agents={agents}
      onClose={vi.fn()}
    />,
  );
  expect(screen.getByRole("alert")).toHaveTextContent(/catalog unavailable/);
  fireEvent.click(screen.getByRole("button", { name: "Retry catalog" }));
  expect(refetch).toHaveBeenCalled();
  vi.mocked(useHostModelOptions).mockReturnValue({
    data: [],
    isLoading: false,
    isError: false,
  } as unknown as ReturnType<typeof useHostModelOptions>);
  result.rerender(
    <BgrConfigurationDialog
      scope="server/user/project"
      host={host}
      agents={agents}
      onClose={vi.fn()}
    />,
  );
  expect(screen.getByText(/No models advertised/)).toBeInTheDocument();
  result.rerender(
    <BgrConfigurationDialog
      scope="server/user/project"
      host={{ ...host, status: "offline" }}
      agents={agents}
      onClose={vi.fn()}
    />,
  );
  expect(screen.getAllByText(/Connect this host/)).toHaveLength(3);
  expect(screen.getByRole("button", { name: "Save draft" })).toBeEnabled();
});
