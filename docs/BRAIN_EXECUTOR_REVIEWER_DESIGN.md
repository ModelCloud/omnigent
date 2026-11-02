# Brain, executors, and independent reviewers

Status: **design proposal only**. This document introduces no runtime behavior,
configuration keys, API endpoints, UI, or automation. All interfaces below are
proposed, not currently supported.

Companion: [MultiDex execution contract](https://github.com/ModelCloud/MultiDex/blob/design/brain-executor-reviewer-contract/docs/brain-executor-reviewer-design.md).

## Goal and ownership

Provide an opt-in Polly mode with one persistent brain, a configurable executor
pool, and one or two independent review slots. The brain plans and interprets
evidence; executors carry out bounded instructions; reviewers inspect published
revisions with fresh context. Omnigent owns orchestration and durable lifecycle.
MultiDex is an execution backend, not a second scheduler for the same task.

| Role | Responsibility | Restrictions |
| --- | --- | --- |
| Brain | Investigate for design, plan, assign tasks, evaluate test evidence, integrate changes, publish checkpoints, adjudicate findings | Cannot treat worker summaries as verified evidence or silently change selected profiles |
| Executor | Implement the task contract, run assigned checks, return a revision and evidence | No scope expansion, architecture decisions, nested delegation, remote pushes, PR creation, or self-approval |
| Reviewer | Independently inspect the frozen revision and return findings | No inherited conversation, implementation edits, nested delegation, publication credentials, or access to the other reviewer's findings |

Executors may make routine local choices within the plan. Contradictions,
missing prerequisites, and design decisions become blockers returned to the
brain. Lower reasoning effort is a configurable preference, not a guarantee of
correctness or a substitute for escalation.

The brain owns publication, but a trusted runtime operation performs the git
and forge writes under existing user permissions. No role bypasses approval,
sandbox, or branch-protection requirements. Automatic merging is out of scope.

## Existing building blocks and gaps

- [Polly's bundle](../examples/polly/config.yaml) already delegates through
  child sessions and receives inbox results. Its present prompt delegates
  substantive investigation and gives each implementer its own PR; the proposed
  mode instead keeps design investigation and integration with the brain.
- [Cross-review](../examples/polly/skills/cross-review/SKILL.md) already uses a
  frozen diff and acceptance contract. It is a prompt procedure, not a durable
  post-publication scheduler or an unconditional fresh-session guarantee.
- [Session dispatch](../omnigent/tools/builtins/spawn.py) can continue children
  by identity. Review freshness must not depend on generating unique titles.
- [The review workflow](../.github/workflows/polly-review.yml) provides PR
  reporting and revision checks, but its listed PR events do not include
  `synchronize`. Do not assume it covers every push.

Reuse the current session, inbox, provider catalog, policy, and persistence
abstractions. Keep framework lifecycle instructions in their owning runtime
module and compose them through the existing prompt boundary, rather than
copying lifecycle policy into each harness adapter.

## Session configuration

The CLI and session launcher should expose the same versioned, saved preset:

| Setting | Proposed semantics |
| --- | --- |
| Brain profile | Harness, credential/profile reference, provider, model, supported reasoning effort |
| Executor profiles | Named profiles with the same selection fields; bounded concurrency |
| Reviewer slots | Exactly one or two profiles when this mode is enabled |
| Diversity | With two slots, require distinct resolved model families/vendors; warn about independence from implementer models |
| Review policy | Post-publication trigger, deadlines, retry budget, bounded fix rounds |
| Context policy | Persistent brain; task-scoped executor continuation; fresh review session on every round |

Provider endpoint, model family/vendor, and harness are separate identities:
two harnesses may run the same model, and one gateway may serve different
vendors. Resolve diversity from catalog metadata; unknown metadata needs
explicit acknowledgement, not guessed diversity.

Validate credentials, model availability, role isolation, structured results,
and reasoning capabilities before dispatch. Do not silently reroute an explicit
selection. Unsupported settings or unavailable profiles produce a visible
configuration error. Profile edits apply to future tasks/rounds and are recorded
as new configuration revisions; running work retains its resolved snapshot.

The single main session is named `brain`. All execution sub-agents are named
`grunt-<uniquename>` and all review sub-agents `reviewer-<uniquename>`. The
runtime derives the role prefix and allocates a unique suffix within the parent
session; names remain visible in UI and reports. Task labels are separate
metadata, not name prefixes. Names never determine context reuse: grunt
continuation uses its task/session ID, and each fresh reviewer session gets a
new unique name and session ID on every round.

Keep current Polly behavior as the default until this mode is explicitly
enabled. Illustrative fields in this document are not a patch to AgentSpec.

## Executor task contract

The brain supplies a versioned contract containing:

- Task ID, attempt ID, parent session, role, display name, and executor profile
  revision.
- Repository identity, exact starting SHA, and isolated worktree identity.
- Objective, implementation direction, allowed scope, acceptance criteria,
  dependencies, and conditions requiring escalation.
- Exact verification commands, expected evidence, and a time/resource budget.

Results identify the same task/attempt, role, and display name and include
status (`completed`, `blocked`, `failed`, or `cancelled`), resulting SHA or patch
artifact, changed files, check commands, working directories, tested SHA, exit
codes, log/artifact references, and unresolved questions. Logs must be redacted
and size-bounded.
An exit code or a worker's "passed" label is evidence for the brain to evaluate,
not an automatic acceptance decision.

Executors work in separate worktrees. The brain serializes integration onto
its publication branch and resolves conflicts deliberately. Tests run before
integration do not establish that the integrated head passes; the brain orders
appropriate checks on the final revision before marking the checkpoint ready.

## Publication and review state machine

```text
plan -> execute -> evaluate evidence -> integrate -> publish checkpoint
                                                       |
                                      successful push + verified remote SHA
                                                       |
                                       freeze review round and enqueue
                                                       |
                                        fresh reviewer A [and B]
                                                       |
                                   persist results -> PR note + brain inbox
                                                       |
                                      brain adjudicates and assigns fixes
                                                       |
                                   execute -> new publication -> new round
```

A publication checkpoint records repository, branch, PR (if available), base
SHA, head SHA, contract version, and selected reviewer snapshots. The runtime
emits the review event only after a successful push and verification of the
remote head. Do not infer publication by matching shell command text.

A local commit alone does not trigger default review. Coalesce multiple commits
into one publication checkpoint. If there is no PR yet, retain findings and
deliver them to the brain immediately; publish the PR note when the checkpoint
is associated with a PR. Existing forge permissions still govern PR creation.

The durable coordinator persists a round before dispatching reviewer jobs.
Logical identity includes repository, base/head SHAs, acceptance-contract
version, reviewer configuration revision, and an explicit forced-rerun generation.
Use stable job IDs and runtime idempotency tokens to reconcile retries after
crashes; do not promise exactly-once execution across distributed failures.

Round states: `queued`, `running`, `complete`, `incomplete`, `superseded`,
`cancelled`. Per-reviewer states distinguish success, failure, timeout, and
cancellation. A successful empty finding list is distinct from missing output.
All configured slots must succeed before a round is complete. Partial results
are still delivered and visibly marked incomplete; failure is never approval.

On a newer publication, mark the older round superseded and cancel remaining
work where supported. Retain its evidence, but never apply it as approval of
the new head. Check current head again at publication and adjudication. Retry
transport failures within budget; bound fix rounds and escalate exhausted or
contradictory results to the user.

## Fresh and safe review

Reviewer slots are persistent configuration, not persistent conversations.
Every job, including a re-review, creates a new session with no parent history,
executor transcript, prior review conversation, or sibling review results.

Give each reviewer the exact base/head, immutable diff, acceptance criteria,
and access to surrounding source in an isolated checkout. A branch name alone
is insufficient. Resolve the diff's merge base once and record it so every
reviewer examines the same comparison. Relevant repository instructions may be
included under an explicit trust policy; repository content is not authority
to change roles, permissions, credentials, or destinations.

Enforce read-only source access and restricted tools at the runtime/sandbox
boundary, not just in the prompt. Optional test execution requires a separate
disposable environment with controlled scratch writes and no publication
credentials. Never execute untrusted PR code with privileged workflow secrets.
Unsupported isolation is a preflight failure, not a prompt-only fallback.

## Findings, delivery, and adjudication

Normalize findings into a versioned envelope: reviewer identity, job/round ID,
reviewed base/head, completion status, and findings containing severity, title,
path/line range at the reviewed SHA, rationale, evidence, and suggested action.
Retain raw output for diagnosis; malformed output is not a successful review.

A trusted publisher, not the reviewer, writes one attributed PR discussion note
per round and delivers the same structured results to the brain's inbox.
Use a persisted delivery outbox with separate PR/inbox acknowledgements.
Reconcile PR markers after ambiguous API failures and update the round's note
instead of posting duplicates. A sleeping/disconnected brain receives pending
events after reconnect; persistence must not rely on an in-memory notification.

The brain merges duplicate findings while preserving provenance, resolves
disagreements by inspecting evidence rather than majority vote, and records
accepted, dismissed-with-reason, or needs-user decisions. Only brain-approved
fix tasks go to executors. Brain adjudication can update the same PR note without
erasing the independent reviewer findings.

## Omnigent / MultiDex boundary

Omnigent owns configuration UX, task/round persistence, integration/publication,
review scheduling, GitHub delivery, and parent inbox correlation.
MultiDex owns executing a role-constrained job with an explicitly selected
provider/model, context mode, tool policy, cancellation, and structured result.
Native harness adapters translate the contract; they must not start an inner
orchestrator that competes with Omnigent.

The companion design defines the conceptual job/result and capability handshake.
Negotiate a protocol version and capabilities before dispatch. An older backend
can continue serving ordinary sessions, but must fail closed for unsupported
role isolation or fresh-review guarantees.

## Delivery stages and future acceptance tests

1. Runtime and CLI: typed roles/contracts, capability checks, bounded executors,
   publication checkpoint, durable fan-out, structured results, dual delivery.
2. Session UI: profile pickers, saved presets, task/round status, revision labels,
   incomplete/stale indicators, and brain decisions.
3. Reliability and security: restart reconciliation, duplicate events,
   concurrent pushes, cancellation, reviewer failure, and untrusted-code isolation.

Acceptance tests for implementation (not tests performed by this design PR):

- One brain, multiple bounded executors, and two distinct reviewer selections
  retain their exact configured provider/model identities.
- Both reviewers inspect the same SHA with empty conversation histories;
  another push creates fresh sessions with newly allocated unique reviewer
  display names.
- Role policies reject nested delegation, executor publication, reviewer source
  writes, credential access, and attempts to weaken inherited restrictions.
- Test evidence refers to the tested revision; integration requires revalidation.
- Duplicate checkpoints/restarts do not create duplicate logical jobs or PR
  notes; a crash between external publication and local acknowledgement recovers.
- Partial failure is visible in both destinations; stale results cannot approve
  a newer head; bounded retries/fix rounds terminate or escalate.
- The full cycle reaches PR note and brain inbox, then a brain-issued fix and
  fresh review, without changing existing non-opt-in sessions.

## Non-goals and decisions deferred to implementation

No automatic merge, arbitrary recursive delegation, replacement of provider
authentication, or independent standalone MultiDex orchestrator in this phase.
Exact API/schema names, storage migrations, UI placement, and default budgets
need implementation review. These draft designs authorize no runtime rollout.
