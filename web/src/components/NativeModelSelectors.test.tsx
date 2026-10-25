import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { NativeModelSelectors } from "./NativeModelSelectors";
import type { NativeModelOption } from "@/lib/types";

afterEach(cleanup);
const options: NativeModelOption[] = [
  {
    id: "local-1017",
    modelFamily: "ds",
    modelLabel: "DeepSeek v4.1 Flash",
    providerId: "localdex",
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
];

it("renders Model, Provider, Speed, Reasoning in order without wire ids in the labels", () => {
  render(
    <NativeModelSelectors
      options={options}
      selectedModel="local-1017"
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
  render(
    <NativeModelSelectors
      options={options}
      selectedModel="local-1017"
      effort={null}
      onModelChange={change}
      onEffortChange={vi.fn()}
    />,
  );
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Provider" }), { key: "ArrowDown" });
  fireEvent.click(await screen.findByRole("option", { name: "Lithos" }));
  expect(change).toHaveBeenCalledWith("lithos-regular");
});

it.each(options.map((row) => row.id))(
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
