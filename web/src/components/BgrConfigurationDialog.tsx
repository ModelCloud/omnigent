import { useId, useState } from "react";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHostModelOptions, type Host } from "@/hooks/useHosts";
import {
  bgrDraftStorageKey,
  bgrIncompleteProfiles,
  bgrProfileFromCatalog,
  emptyBgrProfile,
  readBgrDraft,
  writeBgrDraft,
  type BgrProfileDraft,
} from "@/lib/bgrDraft";
import { NativeModelSelectors } from "@/components/NativeModelSelectors";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  onChange,
}: {
  title: string;
  description: string;
  profile: BgrProfileDraft;
  host: Host | undefined;
  harnesses: readonly string[];
  onChange: (profile: BgrProfileDraft) => void;
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
  const grouped = options.some((row) => row.modelFamily);
  const reasoningOptions = selected?.supportedReasoningEfforts ?? [];
  const fields = ["provider", "model", "variant", "speed", "reasoning"] as const;
  const knownHarnesses = [
    ...new Set([...harnesses, ...(profile.harness ? [profile.harness] : [])]),
  ];
  const changeModel = (model: string) => {
    const row = options.find((option) => option.id === model);
    if (row) onChange(bgrProfileFromCatalog(profile, row));
    else onChange({ ...profile, model });
  };
  return (
    <fieldset className="min-w-0 space-y-3 rounded-xl border p-4">
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
              {harness}
            </option>
          ))}
        </select>
      </div>
      {host?.status !== "online" ? (
        <p className="text-xs text-muted-foreground">
          Connect this host to load its model catalog. Manual draft values remain editable.
        </p>
      ) : !profile.harness ? (
        <p className="text-xs text-muted-foreground">
          Choose a harness to load its model catalog, or enter a profile manually.
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
        <div className="space-y-2">
          <label htmlFor={`${id}-catalog`} className="text-sm font-medium">
            Catalog profile
          </label>
          <select
            id={`${id}-catalog`}
            className={selectClass}
            value={selected?.id ?? ""}
            onChange={(event) => changeModel(event.target.value)}
          >
            <option value="" disabled>
              Choose a catalog row
            </option>
            {options.map((row) => (
              <option key={row.id} value={row.id}>
                {[
                  row.displayName ?? row.id,
                  row.providerId,
                  row.variant,
                  row.speed === undefined ? null : `${row.speed}×`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </option>
            ))}
          </select>
          {grouped && (
            <NativeModelSelectors
              className="min-w-0 p-0"
              options={options}
              selectedModel={profile.model || null}
              effort={profile.reasoning || null}
              onModelChange={changeModel}
              onEffortChange={(reasoning) => onChange({ ...profile, reasoning: reasoning ?? "" })}
            />
          )}
          <p className="text-xs text-muted-foreground">
            Choosing a catalog row replaces model, provider, variant and speed with its advertised
            values. Reasoning is retained until you change it.
          </p>
          <p className="text-xs text-muted-foreground">
            Catalog speed is relative endpoint metadata, not an enforced execution mode.
          </p>
        </div>
      )}
      <div className="grid min-w-0 gap-3 sm:grid-cols-2">
        {fields.map((field) => (
          <div key={field} className="min-w-0 space-y-1.5">
            <label htmlFor={`${id}-${field}`} className="text-sm font-medium">
              {field.charAt(0).toUpperCase() + field.slice(1)}
              {field === "provider" || field === "model" ? " (required for a team)" : " (optional)"}
            </label>
            <Input
              id={`${id}-${field}`}
              maxLength={256}
              value={profile[field]}
              list={field === "reasoning" ? `${id}-reasoning-options` : undefined}
              onChange={(event) => onChange({ ...profile, [field]: event.target.value })}
              autoComplete="off"
            />
          </div>
        ))}
      </div>
      <datalist id={`${id}-reasoning-options`}>
        {reasoningOptions.map((option) => (
          <option key={option.reasoningEffort} value={option.reasoningEffort} />
        ))}
      </datalist>
      {profile.model && !selected && options.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Saved model is not in this catalog. It is preserved as an unverified manual value.
        </p>
      )}
      {profile.reasoning &&
        selected &&
        !reasoningOptions.some((option) => option.reasoningEffort === profile.reasoning) && (
          <p className="text-xs text-muted-foreground">
            This reasoning value is not advertised for the selected model. It is preserved, not
            silently removed.
          </p>
        )}
    </fieldset>
  );
}

/** Remount per server/user/project/host scope so drafts never cross targets. */
export function BgrConfigurationDialog({
  scope,
  host,
  agents,
  onClose,
}: {
  scope: string | null;
  host: Host | undefined;
  agents: readonly AvailableAgent[];
  onClose: () => void;
}) {
  const key = bgrDraftStorageKey(scope, host?.host_id ?? null);
  const [restored] = useState(() => readBgrDraft(key));
  const [draft, setDraft] = useState(restored.draft);
  const [error, setError] = useState(restored.error);
  const [saved, setSaved] = useState(false);
  const [savedDraft, setSavedDraft] = useState(JSON.stringify(restored.draft));
  const dirty = savedDraft !== JSON.stringify(draft);
  const incomplete = bgrIncompleteProfiles(draft);
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
        if (!open) onClose();
      }}
    >
      <DialogContent
        className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl"
        onInteractOutside={(event) => {
          if (dirty) event.preventDefault();
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
            Configuration-only preview. Drafts are stored in this browser for the current server,
            signed-in user, project and host. They are not server teams and do not sync to other
            browsers. Do not enter credentials.
          </p>
          <p id="bgr-launch-unavailable" className="rounded-lg border p-3">
            Launch unavailable: worker lifecycle, role enforcement and reviewer isolation are not
            implemented. Saving never starts a session or grants runtime authority.
          </p>
        </div>
        <form
          noValidate
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            event.stopPropagation();
            const failure = writeBgrDraft(key, draft);
            setError(failure);
            setSaved(failure === null);
            if (failure === null) setSavedDraft(JSON.stringify(draft));
          }}
        >
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
          />
          <RoleProfile
            title="Grunt default"
            description="Workers are allocated dynamically as grunt-<unique>, up to the active cap. Each new attempt snapshots this default profile."
            profile={draft.grunt}
            onChange={(grunt) => update({ ...draft, grunt })}
            host={host}
            harnesses={harnesses}
          />
          {reviewerSlots.map(({ profile, title }, index) => (
            <RoleProfile
              key={title}
              title={title}
              description="Fresh reviewer-<unique> session per publication. Runtime-enforced independent contexts are not available yet."
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
                : "Browser-local draft. Launch remains unavailable."}
          </p>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              Back to New Chat
            </Button>
            <Button type="submit" disabled={key === null}>
              Save draft
            </Button>
            <Button type="button" disabled aria-describedby="bgr-launch-unavailable">
              Launch BGR (unavailable)
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
