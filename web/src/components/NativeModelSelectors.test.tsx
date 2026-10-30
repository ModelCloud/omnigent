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

it("shows the relative speed on provider and variant options", async () => {
  const timed = options.map((row) =>
    row.providerId === "lithos"
      ? { ...row, speed: row.variant === "ultra" ? 2.0 : row.variant === "fast" ? 1.5 : 1.25 }
      : { ...row, speed: row.providerId === "openrouter" ? 1.25 : 1.0 },
  );
  render(
    <NativeModelSelectors
      options={timed}
      selectedModel="lithos-fast"
      effort={null}
      onModelChange={vi.fn()}
      onEffortChange={vi.fn()}
    />,
  );
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Provider" }), { key: "ArrowDown" });
  const providerTexts = screen.getAllByRole("option").map((option) => option.textContent);
  expect(providerTexts).toEqual(
    expect.arrayContaining([
      expect.stringContaining("Lithos⚡⚡, speed 1.25–2.0"), // bolts from max variant
      expect.stringContaining("OpenRouter⚡⚡, speed 1.25"),
      expect.stringContaining("Local⚡, speed 1.0"), // flat baseline
    ]),
  );
  fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Speed" }), { key: "ArrowDown" });
  const variantTexts = screen.getAllByRole("option").map((option) => option.textContent);
  expect(variantTexts).toEqual(
    expect.arrayContaining([
      expect.stringContaining("Regular⚡⚡, speed 1.25"),
      expect.stringContaining("Fast⚡⚡, speed 1.5"), // second bolt is masked half-faded
      expect.stringContaining("Ultra⚡⚡, speed 2.0"),
    ]),
  );
});

it("fades the fractional bolt in proportion to the speed remainder", async () => {
  // The mask gradient must show `fraction` of the bolt — an inverted stop
  // (1 - fraction) made smaller remainders look stronger, so Regular (1.25)
  // rendered a nearly-full second bolt while Fast (1.5) looked weaker.
  const timed = options.map((row) =>
    row.providerId === "lithos"
      ? { ...row, speed: row.variant === "ultra" ? 2.0 : row.variant === "fast" ? 1.5 : 1.25 }
      : { ...row, speed: row.providerId === "openrouter" ? 1.25 : 1.0 },
  );
  render(
    <NativeModelSelectors
      options={timed}
      selectedModel="lithos-fast"
      effort={null}
      onModelChange={vi.fn()}
      onEffortChange={vi.fn()}
    />,
  );
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Speed" }), { key: "ArrowDown" });
  const fadedBolt = (label: string) =>
    screen
      .getAllByRole("option")
      .find((option) => option.textContent?.startsWith(label))
      ?.querySelector<HTMLElement>("span[style*='mask-image']") ?? null;
  expect(fadedBolt("Regular")?.style.maskImage).toContain("black 25%");
  expect(fadedBolt("Fast")?.style.maskImage).toContain("black 50%");
  expect(fadedBolt("Ultra")).toBeNull();
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

it("shows the Default entry and resolves dependents off the catalog default", async () => {
  const catalog = options.map((row) =>
    row.id === "local-1017" ? { ...row, isDefault: true } : row,
  );
  const onModelChange = vi.fn();
  const props = {
    options: catalog,
    effort: null,
    modelDefaultLabel: "Default",
    onModelChange,
    onEffortChange: vi.fn(),
  };
  const { rerender } = render(<NativeModelSelectors {...props} selectedModel={null} />);
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Default");
  // Provider/Speed/Reasoning read off the catalog's isDefault row (local-1017),
  // which is what launching with no override resolves to.
  expect(screen.getByRole("combobox", { name: "Provider" })).toHaveTextContent("Local");
  expect(screen.getByRole("combobox", { name: "Reasoning" })).toHaveTextContent("Max");
  // Pick a concrete family (keeps the effective provider, so it resolves the
  // local row), then switch back: "Default" emits "" (no override).
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Model" }), { key: "ArrowDown" });
  fireEvent.click(await screen.findByRole("option", { name: "DeepSeek v4.1 Flash" }));
  expect(onModelChange).toHaveBeenLastCalledWith("local-1017");
  rerender(<NativeModelSelectors {...props} selectedModel="local-1017" />);
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Model" }), { key: "ArrowDown" });
  fireEvent.click(await screen.findByRole("option", { name: "Default" }));
  expect(onModelChange).toHaveBeenLastCalledWith("");
});

it("omits the Default entry when modelDefaultLabel is unset", () => {
  render(
    <NativeModelSelectors
      options={options}
      selectedModel={null}
      effort={null}
      onModelChange={vi.fn()}
      onEffortChange={vi.fn()}
    />,
  );
  // With no default affordance an empty selection has no dependents either.
  expect(screen.queryAllByRole("combobox")).toHaveLength(1);
});

it("forwards select open/close to onSelectOpenChange (dialog dismiss guard)", async () => {
  const onOpenChange = vi.fn();
  render(
    <NativeModelSelectors
      options={options}
      selectedModel="local-1017"
      effort={null}
      onModelChange={vi.fn()}
      onEffortChange={vi.fn()}
      onSelectOpenChange={onOpenChange}
    />,
  );
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Provider" }), { key: "ArrowDown" });
  expect(onOpenChange).toHaveBeenLastCalledWith(true);
});
