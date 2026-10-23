import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CommandGoalDialog } from "./CommandGoalDialog";

afterEach(cleanup);

describe("CommandGoalDialog", () => {
  it("submits a trimmed completion condition", () => {
    const onStartGoal = vi.fn();
    const onOpenChange = vi.fn();
    render(
      <CommandGoalDialog
        open
        onOpenChange={onOpenChange}
        readOnly={false}
        onStartGoal={onStartGoal}
      />,
    );

    fireEvent.change(screen.getByTestId("goal-condition"), {
      target: { value: "  All tests pass  " },
    });
    fireEvent.click(screen.getByTestId("goal-start"));

    expect(onStartGoal).toHaveBeenCalledWith("All tests pass");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("describes the selected goal backend", () => {
    render(
      <CommandGoalDialog
        open
        onOpenChange={vi.fn()}
        readOnly={false}
        onStartGoal={vi.fn()}
        backendLabel="Codex"
      />,
    );

    expect(screen.getByText(/Codex keeps working until this condition is met/)).toBeInTheDocument();
  });

  it("rejects an empty condition", () => {
    const onStartGoal = vi.fn();
    render(
      <CommandGoalDialog open onOpenChange={vi.fn()} readOnly={false} onStartGoal={onStartGoal} />,
    );

    fireEvent.click(screen.getByTestId("goal-start"));

    expect(screen.getByText("Goal condition cannot be empty.")).toBeInTheDocument();
    expect(onStartGoal).not.toHaveBeenCalled();
  });

  it("keeps the Start button reachable when the condition runs long", () => {
    render(
      <CommandGoalDialog open onOpenChange={vi.fn()} readOnly={false} onStartGoal={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("goal-condition"), {
      target: { value: "All tests pass. ".repeat(400) },
    });

    // Same cap contract as GoalDialog: 85dvh max, fixed footer, editor scrolls.
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveClass("max-h-[85dvh]", "grid-rows-[auto_1fr_auto]");
    const scrollBody = dialog.querySelector(".overflow-y-auto");
    expect(scrollBody).not.toBeNull();
    const start = screen.getByTestId("goal-start");
    expect(start).toBeEnabled();
    expect(scrollBody).not.toContainElement(start);
  });

  it("disables editing in read-only sessions", () => {
    render(<CommandGoalDialog open onOpenChange={vi.fn()} readOnly onStartGoal={vi.fn()} />);

    expect(screen.getByTestId("goal-condition")).toBeDisabled();
    expect(screen.getByTestId("goal-start")).toBeDisabled();
  });
});
