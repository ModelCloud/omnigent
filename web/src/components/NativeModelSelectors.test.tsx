import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { NativeModelSelectors } from "./NativeModelSelectors";
import type { NativeModelOption } from "@/lib/types";

afterEach(cleanup);
const options: NativeModelOption[] = [
  {
    id: "openrouter-1017",
    modelFamily: "ds",
    modelLabel: "DeepSeek v4.1 Flash",
    providerId: "openrouter",
    supportedReasoningEfforts: [{ reasoningEffort: "none" }, { reasoningEffort: "max" }],
    defaultReasoningEffort: "max",
  },
  {
    id: "local-1017",
    modelFamily: "ds",
    modelLabel: "DeepSeek v4.1 Flash",
    providerId: "multidex",
    supportedReasoningEfforts: [{ reasoningEffort: "none" }, { reasoningEffort: "max" }],
    defaultReasoningEffort: "max",
  },
  {
    id: "lithos-regular",
    modelFamily: "ds",
    modelLabel: "DeepSeek v4.1 Flash",
    providerId: "lithos",
    variant: "regular",
    supportedReasoningEfforts: [
      { reasoningEffort: "none" },
      { reasoningEffort: "low" },
      { reasoningEffort: "max" },
    ],
    defaultReasoningEffort: "max",
  },
  {
    id: "lithos-ultra",
    modelFamily: "ds",
    modelLabel: "DeepSeek v4.1 Flash",
    providerId: "lithos",
    variant: "ultra",
    supportedReasoningEfforts: [{ reasoningEffort: "none" }, { reasoningEffort: "max" }],
    defaultReasoningEffort: "max",
  },
  {
    id: "lithos-fast",
    modelFamily: "ds",
    modelLabel: "DeepSeek v4.1 Flash",
    providerId: "lithos",
    variant: "fast",
    supportedReasoningEfforts: [{ reasoningEffort: "none" }, { reasoningEffort: "max" }],
    defaultReasoningEffort: "max",
  },
  ...["Astra", "Sol"].map((name) => ({
    id: `openai-${name}`,
    modelLabel: name,
    providerId: "openai",
    supportedReasoningEfforts: [{ reasoningEffort: "none" }, { reasoningEffort: "high" }],
    defaultReasoningEffort: "high",
  })),
];

it("renders Model, Provider, Speed, Reasoning in order without wire ids in the labels", () => {
  render(
    <NativeModelSelectors
      options={options}
      selectedModel="lithos-regular"
      effort={null}
      onModelChange={vi.fn()}
      onEffortChange={vi.fn()}
    />,
  );
  expect(screen.getAllByRole("combobox").map((node) => node.getAttribute("aria-label"))).toEqual([
    "Model",
    "Provider",
    "Speed",
    "Reasoning",
  ]);
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("DeepSeek v4.1 Flash");
  expect(screen.getByRole("combobox", { name: "Reasoning" })).toHaveTextContent("Max");
});

it("selects a provider's regular model atomically and only shows its available speeds", async () => {
  const change = vi.fn();
  const props = { options, effort: null, onModelChange: change, onEffortChange: vi.fn() };
  const { rerender } = render(<NativeModelSelectors {...props} selectedModel="local-1017" />);
  expect(screen.queryByRole("combobox", { name: "Speed" })).not.toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Provider" }), { key: "ArrowDown" });
  expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
    "OpenRouter",
    "Local",
    "Lithos",
  ]);
  fireEvent.click(await screen.findByRole("option", { name: "Lithos" }));
  expect(change).toHaveBeenCalledWith("lithos-regular");
  rerender(<NativeModelSelectors {...props} selectedModel="lithos-regular" />);
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Speed" }), { key: "ArrowDown" });
  expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
    "Regular",
    "Ultra",
    "Fast",
  ]);
  fireEvent.click(await screen.findByRole("option", { name: "Ultra" }));
  expect(change).toHaveBeenLastCalledWith("lithos-ultra");
  rerender(<NativeModelSelectors {...props} selectedModel="lithos-ultra" />);
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Provider" }), { key: "ArrowDown" });
  fireEvent.click(await screen.findByRole("option", { name: "OpenRouter" }));
  expect(change).toHaveBeenLastCalledWith("openrouter-1017");
  rerender(<NativeModelSelectors {...props} selectedModel="openrouter-1017" />);
  expect(screen.queryByRole("combobox", { name: "Speed" })).not.toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Model" }), { key: "ArrowDown" });
  fireEvent.click(await screen.findByRole("option", { name: "Astra" }));
  expect(change).toHaveBeenLastCalledWith("openai-Astra");
  rerender(<NativeModelSelectors {...props} selectedModel="openai-Astra" />);
  expect(screen.queryByRole("combobox", { name: "Provider" })).not.toBeInTheDocument();
});

it("prefixes provider options with an online/offline status dot", async () => {
  const healthy = options.map((row) =>
    row.providerId === "lithos"
      ? { ...row, providerHealth: "online" as const }
      : row.providerId === "multidex"
        ? { ...row, providerHealth: "offline" as const }
        : row,
  );
  render(
    <NativeModelSelectors
      options={healthy}
      selectedModel="local-1017"
      effort={null}
      onModelChange={vi.fn()}
      onEffortChange={vi.fn()}
    />,
  );
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Provider" }), { key: "ArrowDown" });
  const dots = Object.fromEntries(
    screen
      .getAllByRole("option")
      .map((option) => [
        option.textContent?.replace(/, (online|offline)$/, ""),
        option.querySelector(".rounded-full")?.className ?? "",
      ]),
  );
  expect(dots["Lithos"]).toContain("bg-success");
  expect(dots["Local"]).toContain("bg-muted-foreground/50");
  expect(dots["OpenRouter"]).not.toContain("bg-success");
  expect(dots["OpenRouter"]).not.toContain("bg-muted-foreground");
});

it.each(options.filter((row) => row.modelFamily === "ds").map((row) => row.id))(
  "keeps explicit Off distinct from Default for %s",
  async (id) => {
    const onEffortChange = vi.fn();
    const props = {
      options,
      selectedModel: id,
      onModelChange: vi.fn(),
      onEffortChange,
    };
    const { rerender } = render(<NativeModelSelectors {...props} effort={null} />);
    fireEvent.keyDown(screen.getByRole("combobox", { name: "Reasoning" }), { key: "ArrowDown" });
    fireEvent.click(await screen.findByRole("option", { name: "Off" }));
    expect(onEffortChange).toHaveBeenLastCalledWith("none");

    rerender(<NativeModelSelectors {...props} effort="none" />);
    expect(screen.getByRole("combobox", { name: "Reasoning" })).toHaveTextContent("Off");
    fireEvent.keyDown(screen.getByRole("combobox", { name: "Reasoning" }), { key: "ArrowDown" });
    fireEvent.click(await screen.findByRole("option", { name: "Default (Max)" }));
    expect(onEffortChange).toHaveBeenLastCalledWith(null);
  },
);

it.each(["openai-Astra", "openai-Sol"])("hides singleton provider and speed for %s", (id) => {
  render(
    <NativeModelSelectors
      options={options}
      selectedModel={id}
      effort={null}
      onModelChange={vi.fn()}
      onEffortChange={vi.fn()}
    />,
  );
  expect(screen.getAllByRole("combobox").map((node) => node.getAttribute("aria-label"))).toEqual([
    "Model",
    "Reasoning",
  ]);
});

it.each([
  { kind: "empty", supportedReasoningEfforts: [] },
  { kind: "singleton", supportedReasoningEfforts: [{ reasoningEffort: "max" }] },
])("hides $kind choices without changing the selected model", ({ supportedReasoningEfforts }) => {
  const onModelChange = vi.fn();
  const onEffortChange = vi.fn();
  const row = { ...options[0], supportedReasoningEfforts };
  render(
    <NativeModelSelectors
      options={[row]}
      selectedModel={row.id}
      effort={null}
      onModelChange={onModelChange}
      onEffortChange={onEffortChange}
    />,
  );
  expect(screen.queryAllByRole("combobox")).toHaveLength(0);
  expect(onModelChange).not.toHaveBeenCalled();
  expect(onEffortChange).not.toHaveBeenCalled();
});
