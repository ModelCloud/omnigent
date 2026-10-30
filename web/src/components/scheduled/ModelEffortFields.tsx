// Model + reasoning-effort + permission-mode sub-form for the scheduled-task
// dialog.
//
// A deliberately lightweight, scheduled-dialog-local picker rather than the
// interactive NewChatDialog's 26-prop HarnessConfigModal (which is bound to
// smart-routing / cost-control / per-turn dynamic model loading — all
// disproportionate for a saved scheduled task). It reuses the SHARED source of
// truth for the option lists: the per-harness effort ladders and the
// MODEL_SELECT_DEFAULT / EFFORT_SELECT_NONE sentinels from
// HarnessConfigControls, so the choices never drift from the interactive
// dialog.
//
// The parent gates rendering on the selected agent's model surface (the
// modelPicker / devinMode capabilities, or a codex-native harness — mirroring
// the interactive dialog's `agentHasModelSettings`), and passes the harness so
// the controls match the agent: model options come from the pinned host's live
// catalog for that harness, Claude keeps its static alias fallback, and every
// other harness falls back to a free-text override when no host catalog is
// available. Permission mode renders only for permissionMode-capable agents
// (Claude Code) — the scheduled-task wire field is Claude-only. Both controls
// default to "unselected" ("" = agent default), which the parent omits from the
// create/update body so the fire path uses the agent's configured model +
// effort.

import { Label } from "@/components/scheduled/Label";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  CLAUDE_NATIVE_EFFORTS,
  EFFORT_SELECT_NONE,
  MODEL_SELECT_DEFAULT,
  nativeEffortOptions,
} from "@/components/HarnessConfigControls";
import { NativeModelSelectors } from "@/components/NativeModelSelectors";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import { CLAUDE_NATIVE_PERMISSION_MODES } from "@/lib/claudePermissionMode";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import { useHostModelOptions } from "@/hooks/useHosts";

/** Sentinel Select value for "no permission override" (use the agent default).
 * Distinct from Claude's real `default` (Manual) mode so leaving the control
 * alone keeps the agent's configured mode rather than forcing Manual. */
const PERMISSION_SELECT_DEFAULT = "__agent_default__";

export function ModelEffortFields({
  model,
  effort,
  permissionMode,
  harness,
  supportsPermissionMode,
  hostId,
  onModelChange,
  onEffortChange,
  onPermissionModeChange,
  onSelectOpenChange,
}: {
  /** Selected model id/alias, or "" = agent default (nothing overridden). */
  model: string;
  /** Selected reasoning effort, or "" = agent default. */
  effort: string;
  /** Selected permission mode, or "" = agent default (nothing overridden). */
  permissionMode: string;
  /** Harness of the selected agent ("claude-native", "codex-native", …). */
  harness: string;
  /** Whether the agent carries the permission-mode surface (Claude only). */
  supportsPermissionMode: boolean;
  /** Pinned host id, or "" when unset (task resolves a host at fire time). */
  hostId: string;
  onModelChange: (model: string) => void;
  onEffortChange: (effort: string) => void;
  onPermissionModeChange: (mode: string) => void;
  /** Forwarded to each Select's onOpenChange so the parent Dialog can keep an
   * open dropdown from dismissing the whole modal. */
  onSelectOpenChange?: (open: boolean) => void;
}) {
  // When a host is pinned, use its live-resolved model options for the
  // selected harness (mirrors the interactive dialog on a connected host).
  // With no host pinned — the common case, since scheduled tasks resolve a
  // host at fire time — only Claude has a static alias list; catalog-driven
  // harnesses (Codex, Devin, Pi) fall back to a free-text override below.
  const { data: hostModelOptions } = useHostModelOptions(
    hostId === "" ? null : hostId,
    harness,
    hostId !== "",
  );
  const staticFallback = harness === "claude-native" ? CLAUDE_NATIVE_MODELS : [];
  const modelOptions =
    hostModelOptions && hostModelOptions.length > 0
      ? hostModelOptions.map((o) => ({ id: o.id, label: o.displayName ?? o.id }))
      : staticFallback.map((m) => ({ id: m.id, label: m.label }));
  // Codex resolves a per-model ladder from the catalog; an empty result (no
  // catalog, or the row names no efforts) falls back to the generic ladder.
  const catalogEfforts = nativeEffortOptions(harness, hostModelOptions ?? [], model || null);
  const effortOptions = catalogEfforts?.length ? catalogEfforts : CLAUDE_NATIVE_EFFORTS;

  // Codex/MultiDex catalogs carry provider + serving-variant axes the flat
  // picker can't express: dependent Model → Provider → Speed → Reasoning
  // selectors (the same component the interactive picker uses) drive one
  // model-override id, with liveness dots and speed bolts on the options.
  const codexGrouped =
    harness === "codex-native" && (hostModelOptions?.some((row) => row.modelFamily) ?? false);
  const handleCodexModelChange = (id: string) => {
    onModelChange(id);
    // An effort that the newly resolved row doesn't advertise can't be sent
    // with it — drop it back to the agent default rather than save a pair the
    // harness would reject.
    const row =
      findNativeModelOption(hostModelOptions ?? [], id) ??
      // "" picks "Default": the launch resolves to the catalog's own default.
      (id === "" ? (hostModelOptions ?? []).find((option) => option.isDefault) : null);
    if (
      effort !== "" &&
      !(row?.supportedReasoningEfforts ?? []).some((e) => e.reasoningEffort === effort)
    ) {
      onEffortChange("");
    }
  };
  if (codexGrouped) {
    return (
      <div className="flex flex-col gap-3 sm:gap-4">
        <NativeModelSelectors
          options={hostModelOptions ?? []}
          selectedModel={model === "" ? null : model}
          effort={effort === "" ? null : effort}
          modelDefaultLabel="Default"
          onModelChange={handleCodexModelChange}
          onEffortChange={(e) => onEffortChange(e ?? "")}
          onSelectOpenChange={onSelectOpenChange}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 sm:gap-4">
      <div className="grid gap-3 sm:grid-cols-2 sm:gap-6" data-testid="task-model-effort-row">
        <div className="flex w-full min-w-0 flex-col gap-1.5" data-testid="task-model-control">
          <Label htmlFor="task-model">Model</Label>
          {modelOptions.length > 0 ? (
            <Select
              value={model === "" ? MODEL_SELECT_DEFAULT : model}
              componentId="tasks.scheduled.model"
              valueHasNoPii
              onValueChange={(v) => onModelChange(v === MODEL_SELECT_DEFAULT ? "" : v)}
              onOpenChange={onSelectOpenChange}
            >
              <SelectTrigger id="task-model" data-testid="task-model-trigger" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent
                position="popper"
                align="start"
                className="w-(--radix-select-trigger-width)"
              >
                <SelectItem value={MODEL_SELECT_DEFAULT}>Default</SelectItem>
                {modelOptions.map((m) => (
                  <SelectItem key={m.id} value={m.id}>
                    {m.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            // No catalog to pick from (no pinned host, or the host has no
            // model list for this harness) — the override is still a plain
            // model id the launch hands to the CLI, so accept it free-form.
            <Input
              id="task-model"
              data-testid="task-model-trigger"
              value={model}
              placeholder="Default"
              componentId="tasks.scheduled.model"
              onChange={(e) => onModelChange(e.target.value)}
            />
          )}
        </div>

        <div className="flex w-full min-w-0 flex-col gap-1.5" data-testid="task-effort-control">
          <Label htmlFor="task-effort">Effort</Label>
          <Select
            value={effort === "" ? EFFORT_SELECT_NONE : effort}
            componentId="tasks.scheduled.effort"
            valueHasNoPii
            onValueChange={(v) => onEffortChange(v === EFFORT_SELECT_NONE ? "" : v)}
            onOpenChange={onSelectOpenChange}
          >
            <SelectTrigger id="task-effort" data-testid="task-effort-trigger" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent
              position="popper"
              align="start"
              className="w-(--radix-select-trigger-width)"
            >
              <SelectItem value={EFFORT_SELECT_NONE}>Default</SelectItem>
              {effortOptions.map((e) => (
                <SelectItem key={e.value} value={e.value}>
                  {e.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {supportsPermissionMode && (
        <div className="flex w-full min-w-0 flex-col gap-1.5" data-testid="task-permission-control">
          <Label htmlFor="task-permission">Permission mode</Label>
          <Select
            value={permissionMode === "" ? PERMISSION_SELECT_DEFAULT : permissionMode}
            componentId="tasks.scheduled.permission_mode"
            valueHasNoPii
            onValueChange={(v) => onPermissionModeChange(v === PERMISSION_SELECT_DEFAULT ? "" : v)}
            onOpenChange={onSelectOpenChange}
          >
            <SelectTrigger
              id="task-permission"
              data-testid="task-permission-trigger"
              className="w-full"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent
              position="popper"
              align="start"
              className="w-(--radix-select-trigger-width)"
            >
              <SelectItem value={PERMISSION_SELECT_DEFAULT}>Default</SelectItem>
              {CLAUDE_NATIVE_PERMISSION_MODES.map((m) => (
                <SelectItem key={m.value} value={m.value}>
                  {m.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
    </div>
  );
}
