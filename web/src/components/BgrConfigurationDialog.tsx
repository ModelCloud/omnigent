import { useId, useRef, useState } from "react";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHostModelOptions, type Host } from "@/hooks/useHosts";
import {
  bgrDraftStorageKey,
  bgrIncompleteProfiles,
  bgrProfileFromCatalog,
  emptyBgrProfile,
  readBgrDraft,
  validateBgrLaunchDraft,
  writeBgrDraft,
  type BgrDraft,
  type BgrProfileDraft,
} from "@/lib/bgrDraft";
import { NativeModelSelectors } from "@/components/NativeModelSelectors";
import { BRAIN_HARNESS_LABELS } from "@/lib/agentLabels";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const selectClass =
  "h-9 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function RoleProfile({
  title,
  description,
  profile,
  host,
  harnesses,
  harnessLabels,
  onChange,
  disabled = false,
}: {
  title: string;
  description: string;
  profile: BgrProfileDraft;
  host: Host | undefined;
  harnesses: readonly string[];
  harnessLabels: Readonly<Record<string, string>>;
  onChange: (profile: BgrProfileDraft) => void;
  disabled?: boolean;
}) {
  const id = useId();
  const catalog = useHostModelOptions(
    host?.host_id ?? null,
    profile.harness,
    host?.status === "online" && profile.harness !== "",
    { poll: false },
  );
  const options = catalog.data ?? [];
  const selected = options.find((row) => row.id === profile.model);
  const verified =
    selected &&
    profile.provider === (selected.providerId ?? "") &&
    profile.variant === (selected.variant ?? "") &&
    profile.speed === (selected.speed === undefined ? "" : String(selected.speed));
  const reasoningOptions = selected?.supportedReasoningEfforts ?? [];
  const fields = [
    ["harness", "Harness ID"],
    ["model", "Model ID"],
    ["provider", "Provider ID"],
    ["variant", "Variant value"],
    ["speed", "Speed value"],
    ["reasoning", "Reasoning value"],
  ] as const;
  const [advanced, setAdvanced] = useState(false);
  const knownHarnesses = [
    ...new Set([...harnesses, ...(profile.harness ? [profile.harness] : [])]),
  ];
  const changeModel = (model: string) => {
    const row = options.find((option) => option.id === model);
    if (row) onChange(bgrProfileFromCatalog(profile, row));
    else onChange({ ...profile, model });
  };
  return (
    <fieldset disabled={disabled} className="min-w-0 space-y-3 rounded-xl border p-4">
      <legend className="px-1 font-medium">{title}</legend>
      <p className="text-xs text-muted-foreground">{description}</p>
      <div className="space-y-1.5">
        <label htmlFor={`${id}-harness`} className="text-sm font-medium">
          Harness
        </label>
        <select
          id={`${id}-harness`}
          className={selectClass}
          value={profile.harness}
          onChange={(event) => onChange({ ...profile, harness: event.target.value })}
        >
          <option value="">Not specified</option>
          {knownHarnesses.map((harness) => (
            <option key={harness} value={harness}>
              {nativeCodingAgentForHarness(harness)?.displayName ??
                harnessLabels[harness] ??
                harness}
              {!harnesses.includes(harness) ? " (unverified)" : ""}
            </option>
          ))}
        </select>
      </div>
      {host?.status !== "online" ? (
        <p className="text-xs text-muted-foreground">
          Connect this host to load its model catalog. Manual draft values remain editable in
          Advanced.
        </p>
      ) : !profile.harness ? (
        <p className="text-xs text-muted-foreground">
          Choose a harness to load its model catalog, or enter manual values in Advanced.
        </p>
      ) : catalog.isLoading ? (
        <p role="status" className="text-xs text-muted-foreground">
          Loading model catalog…
        </p>
      ) : catalog.isError ? (
        <div className="text-xs text-muted-foreground">
          <p role="alert">Model catalog unavailable. Existing values have not changed.</p>
          <Button type="button" variant="ghost" size="sm" onClick={() => void catalog.refetch()}>
            Retry catalog
          </Button>
        </div>
      ) : options.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No models advertised for this harness. Manual values are unverified.
        </p>
      ) : (
        <NativeModelSelectors
          key={JSON.stringify([
            profile.harness,
            profile.model,
            profile.provider,
            profile.variant,
            profile.speed,
          ])}
          configuration
          className="min-w-0 p-0"
          options={options}
          selectedModel={verified ? profile.model : null}
          effort={profile.reasoning || null}
          onModelChange={changeModel}
          onEffortChange={(reasoning) => onChange({ ...profile, reasoning: reasoning ?? "" })}
        />
      )}
      {!verified &&
        [profile.model, profile.provider, profile.variant, profile.speed].some(Boolean) && (
          <div className="space-y-1 break-words rounded-md border p-2 text-xs text-muted-foreground">
            <p>
              Manual values (unverified). They remain unchanged until you select an advertised
              model/variant or edit Advanced.
            </p>
            <p>
              Model: {profile.model || "Not set"} · Provider: {profile.provider || "Not set"} ·
              Variant: {profile.variant || "Not set"} · Speed: {profile.speed || "Not set"}
            </p>
          </div>
        )}
      {profile.reasoning &&
        (!verified ||
          !reasoningOptions.some((option) => option.reasoningEffort === profile.reasoning)) && (
          <p className="text-xs text-muted-foreground">
            Reasoning: {profile.reasoning} (unverified). This reasoning value is not advertised for
            the selected model. It is preserved, not silently removed.
          </p>
        )}
      <div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-expanded={advanced}
          aria-controls={`${id}-advanced`}
          onClick={() => setAdvanced(!advanced)}
        >
          Advanced manual overrides
        </Button>
        {advanced && (
          <div id={`${id}-advanced`} className="space-y-3 rounded-md border p-3">
            <p className="text-xs text-muted-foreground">
              Raw saved values, not runtime-validated. Catalog selection replaces model, provider,
              variant and speed with advertised values; reasoning is retained until changed.
            </p>
            <div className="grid min-w-0 gap-3 sm:grid-cols-2">
              {fields.map(([field, label]) => (
                <div key={field} className="min-w-0 space-y-1.5">
                  <label htmlFor={`${id}-${field}-manual`} className="text-sm font-medium">
                    {label}
                  </label>
                  <Input
                    id={`${id}-${field}-manual`}
                    maxLength={256}
                    value={profile[field]}
                    onChange={(event) => onChange({ ...profile, [field]: event.target.value })}
                    autoComplete="off"
                  />
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </fieldset>
  );
}

/** Remount per server/user/project/host scope so drafts never cross targets. */
export function BgrConfigurationDialog({
  scope,
  host,
  agents,
  harnessLabels = BRAIN_HARNESS_LABELS,
  onClose,
  onLaunch,
  launchUnavailableReason,
  initialPrompt = "",
  supportedHarnesses,
}: {
  scope: string | null;
  host: Host | undefined;
  agents: readonly AvailableAgent[];
  harnessLabels?: Readonly<Record<string, string>>;
  onClose: () => void;
  onLaunch?: (draft: BgrDraft, initialPrompt: string) => Promise<void>;
  launchUnavailableReason?: string | null;
  initialPrompt?: string;
  supportedHarnesses?: readonly string[];
}) {
  const key = bgrDraftStorageKey(scope, host?.host_id ?? null);
  const [restored] = useState(() => readBgrDraft(key));
  const [draft, setDraft] = useState(restored.draft);
  const [error, setError] = useState(restored.error);
  const [saved, setSaved] = useState(false);
  const [savedDraft, setSavedDraft] = useState(JSON.stringify(restored.draft));
  const [launching, setLaunching] = useState(false);
  const [task, setTask] = useState(initialPrompt);
  const launchPending = useRef(false);
  const dirty = savedDraft !== JSON.stringify(draft);
  const incomplete = bgrIncompleteProfiles(draft);
  const launchBlocked =
    launchUnavailableReason ??
    (!onLaunch
      ? "Launch unavailable: worker lifecycle, role enforcement and reviewer isolation are not implemented. Saving never starts a session or grants runtime authority."
      : host?.status !== "online"
        ? "Connect the selected host before launching."
        : key === null
          ? "Select a host and wait for your server identity before launching."
          : (validateBgrLaunchDraft(draft) ??
            (supportedHarnesses &&
            [draft.brain, draft.grunt, ...draft.reviewers].some(
              (profile) => !supportedHarnesses.includes(profile.harness.trim()),
            )
              ? "This host cannot run one or more selected harnesses. Choose supported harnesses for every role."
              : !task.trim()
                ? "Describe the initial task before launching."
                : null)));
  const launch = async () => {
    if (!onLaunch || launchBlocked || launchPending.current) return;
    launchPending.current = true;
    setLaunching(true);
    setError(null);
    try {
      await onLaunch(draft, task);
      onClose();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "BGR launch failed. Try again.");
    } finally {
      launchPending.current = false;
      setLaunching(false);
    }
  };
  const harnesses = [
    ...new Set(agents.flatMap((agent) => (agent.harness ? [agent.harness] : []))),
  ].sort();
  const update = (next: typeof draft) => {
    setDraft(next);
    setSaved(false);
  };
  const reviewerSlots = draft.reviewers.map((profile, index) => ({
    profile,
    title: `Reviewer ${index + 1}`,
  }));
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !launchPending.current) onClose();
      }}
    >
      <DialogContent
        className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl"
        onInteractOutside={(event) => {
          if (dirty || launchPending.current) event.preventDefault();
        }}
        onEscapeKeyDown={(event) => {
          if (launchPending.current) event.preventDefault();
        }}
      >
        <DialogHeader>
          <DialogTitle>BGR configuration</DialogTitle>
          <DialogDescription>
            One brain plans; dynamic grunts execute; fresh independent reviewers evaluate each
            publication.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2 text-sm">
          <p className="break-words">
            Host: <strong>{host?.name ?? "No host selected"}</strong>
          </p>
          <p className="text-muted-foreground">
            {!onLaunch && "Configuration-only preview. "}
            Drafts are stored in this browser for the current server, signed-in user, project and
            host. Saving a draft does not create a server team. Do not enter credentials.
          </p>
          <p id="bgr-launch-state" className="rounded-lg border p-3">
            {launching
              ? "Launching BGR… Wait for confirmation before leaving this dialog."
              : (launchBlocked ??
                "Launch starts one brain with the selected role profiles. The server validates runtime capabilities before starting the team.")}
          </p>
        </div>
        <form
          noValidate
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            event.stopPropagation();
            if (launchPending.current) return;
            const failure = writeBgrDraft(key, draft);
            setError(failure);
            setSaved(failure === null);
            if (failure === null) setSavedDraft(JSON.stringify(draft));
          }}
        >
          {onLaunch && (
            <div className="space-y-1.5">
              <label htmlFor="bgr-initial-task" className="text-sm font-medium">
                Initial task
              </label>
              <Textarea
                id="bgr-initial-task"
                value={task}
                disabled={launching}
                onChange={(event) => setTask(event.target.value)}
                placeholder="Describe what the brain should plan and assign."
              />
              <p className="text-xs text-muted-foreground">
                Sent to the brain only when you launch. Saving role profiles does not save this
                task.
              </p>
            </div>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label htmlFor="bgr-grunt-cap" className="text-sm font-medium">
                Maximum active grunts
              </label>
              <Input
                id="bgr-grunt-cap"
                type="number"
                min={1}
                max={64}
                step={1}
                disabled={launching}
                value={draft.max_active_grunts}
                onChange={(event) =>
                  update({ ...draft, max_active_grunts: Number(event.target.value) })
                }
              />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="bgr-reviewer-count" className="text-sm font-medium">
                Reviewers per publication
              </label>
              <select
                id="bgr-reviewer-count"
                className={selectClass}
                value={draft.reviewers.length}
                disabled={launching}
                onChange={(event) =>
                  update({
                    ...draft,
                    reviewers:
                      event.target.value === "1"
                        ? draft.reviewers.slice(0, 1)
                        : [...draft.reviewers, emptyBgrProfile()],
                  })
                }
              >
                <option value="1">1 reviewer</option>
                <option value="2">2 independent reviewers</option>
              </select>
            </div>
          </div>
          <RoleProfile
            title="Brain"
            description="Exactly one agent named brain, persistent for the team."
            profile={draft.brain}
            onChange={(brain) => update({ ...draft, brain })}
            host={host}
            harnesses={harnesses}
            harnessLabels={harnessLabels}
            disabled={launching}
          />
          <RoleProfile
            title="Grunt default"
            description="Workers are allocated dynamically as grunt-<unique>, up to the active cap. Each new attempt snapshots this default profile."
            profile={draft.grunt}
            onChange={(grunt) => update({ ...draft, grunt })}
            host={host}
            harnesses={harnesses}
            harnessLabels={harnessLabels}
            disabled={launching}
          />
          {reviewerSlots.map(({ profile, title }, index) => (
            <RoleProfile
              key={title}
              title={title}
              description={
                onLaunch
                  ? "Fresh reviewer-<unique> session per publication, independently inspecting the same frozen source revision."
                  : "Fresh reviewer-<unique> session per publication. Runtime-enforced independent contexts are not available yet."
              }
              profile={profile}
              onChange={(next) =>
                update({
                  ...draft,
                  reviewers: draft.reviewers.map((current, currentIndex) =>
                    currentIndex === index ? next : current,
                  ),
                })
              }
              host={host}
              harnesses={harnesses}
              harnessLabels={harnessLabels}
              disabled={launching}
            />
          ))}
          {incomplete.length > 0 && (
            <p className="text-sm text-muted-foreground">
              Incomplete profiles: {incomplete.join(", ")}. You can save an incomplete draft; a team
              will require a model and provider for each role. Manual values and harness changes are
              not runtime-validated.
            </p>
          )}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          {key === null && (
            <p role="status" className="text-sm text-muted-foreground">
              Select a host and wait for your server identity to save. Close this form to change the
              target.
            </p>
          )}
          <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
            {saved
              ? "Draft saved in this browser. No session was launched."
              : dirty
                ? "Unsaved changes — save before closing."
                : onLaunch
                  ? "Browser-local draft. Launch requires server confirmation."
                  : "Browser-local draft. Launch remains unavailable."}
          </p>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose} disabled={launching}>
              Back to New Chat
            </Button>
            <Button type="submit" disabled={key === null || launching}>
              Save draft
            </Button>
            <Button
              type="button"
              disabled={!onLaunch || !!launchBlocked || launching}
              aria-describedby="bgr-launch-state"
              onClick={() => void launch()}
            >
              {launching ? "Launching BGR…" : onLaunch ? "Launch BGR" : "Launch BGR (unavailable)"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
