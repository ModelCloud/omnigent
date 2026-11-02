# BGR: brain, grunts, and reviewers

Status: **design proposal only**. This document introduces no runtime behavior,
configuration keys, API endpoints, UI, or automation. All interfaces below are
proposed, not currently supported.

Companion: [MultiDex execution contract](https://github.com/ModelCloud/MultiDex/blob/design/brain-executor-reviewer-contract/docs/brain-executor-reviewer-design.md).

## Goal and ownership

Provide BGR (brain-grunt-reviewers) as a separate opt-in mode, selectable
alongside Polly in the Omnigent server/host UI. Selecting BGR does not silently
change or replace existing Polly behavior. BGR has one persistent brain,
dynamically spawned bounded grunts, and one or two independent review slots.
The brain plans and interprets evidence; grunts carry out bounded instructions;
reviewers inspect published revisions with fresh context. Omnigent owns BGR
configuration, orchestration, and durable lifecycle. MultiDex is an execution
backend, not a second scheduler for the same task.

| Role | Responsibility | Restrictions |
| --- | --- | --- |
| Brain | Investigate for design, plan, assign tasks, evaluate test evidence, integrate changes, publish checkpoints, adjudicate findings | Cannot treat worker summaries as verified evidence or silently change selected profiles |
| Grunt | Implement the task contract, run assigned checks, return a revision and evidence | No scope expansion, architecture decisions, nested delegation, remote pushes, PR creation, or self-approval |
| Reviewer | Independently inspect the frozen revision and return findings | No inherited conversation, implementation edits, nested delegation, publication credentials, or access to another reviewer's findings before independent outputs are sealed |

Grunts may make routine local choices within the plan. Contradictions,
missing prerequisites, and design decisions become blockers returned to the
brain. Lower reasoning effort is a configurable preference, not a guarantee of
correctness or a substitute for escalation.

The brain owns publication, but a trusted runtime operation performs the git
and forge writes under existing user permissions. No role bypasses approval,
sandbox, or branch-protection requirements. Automatic merging is out of scope.

## Existing building blocks and gaps

- [Polly's bundle](../examples/polly/config.yaml) already delegates through
  child sessions and receives inbox results. Its present prompt delegates
  substantive investigation and gives each implementer its own PR; BGR instead
  keeps design investigation and integration with the brain.
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

## BGR selection and session configuration

The Omnigent server/host UI presents BGR as a distinct, opt-in selection
alongside Polly. Its versioned, saved configuration exposes:

| Setting | Proposed semantics |
| --- | --- |
| Active mode | BGR or Polly; selecting BGR is explicit and never changes existing Polly sessions |
| Brain default profile | Provider, model, variant, speed, reasoning, and execution-compatible harness |
| Grunt default profile | Provider, model, variant, speed, reasoning, and execution-compatible harness |
| Per-grunt profiles | Optional profile defaults; no configured static worker pool |
| Max concurrent grunt workers | Dynamic upper bound on in-flight grunts; the brain starts only workers needed for tasks, up to this cap |
| Active reviewers | Exactly one or two reviewers |
| Reviewer profiles | Each active reviewer independently selects provider, model, variant, speed, reasoning, and execution-compatible harness |
| Diversity | With two reviewers, require distinct resolved model families/vendors; warn about independence from implementer models |
| Review policy | Post-publication trigger, deadlines, retry budget, bounded fix rounds |
| Context policy | Persistent brain; task-scoped grunt continuation; fresh reviewer session on every round |

Provider endpoint, model family/vendor, and harness are separate identities:
two harnesses may run the same model, and one gateway may serve different
vendors. Harness identifies execution compatibility under a profile; it does
not substitute for provider or model settings. Resolve each profile's supported
variant, speed, and reasoning choices from the capability catalog, show only
supported values, and validate combinations against that catalog. Resolve
reviewer diversity from catalog metadata; unknown model-family/vendor metadata
requires explicit acknowledgement, not guessed diversity. Validate credentials,
model availability, role isolation, structured results, and reasoning
capabilities before dispatch. An explicit invalid choice or unsupported
combination is a visible error, never a silent remap or reroute.
Resolve and pin the brain default when a BGR session is created, recording its
profile revision with the session. Changes to that default apply to future
sessions, not by silently mutating an active brain. Grunt and reviewer profile
edits apply to future jobs/rounds and carry recorded configuration revisions;
in-flight work retains its resolved snapshot.

The single main session is named `brain`. All execution sub-agents are named
`grunt-<uniquename>` and all review sub-agents `reviewer-<uniquename>`. The
runtime derives the role prefix and allocates a unique suffix within the parent
session; names remain visible in UI and reports. Task labels are separate
metadata, not name prefixes. Names never determine context reuse: grunt
continuation uses its task/session ID, and each fresh reviewer session gets a
new unique name and session ID on every round.

The brain spawns grunts only as tasks require them, up to the configured dynamic
concurrency cap; the cap is not a static pool size or pre-spawn count. The cap
applies only to in-flight grunts, never reviewers. Omnigent persists and
enforces it across brain turns and restarts. When the cap is occupied, excess
work is queued or rejected. Lowering the cap does not terminate active grunts;
new grunts remain blocked until the in-flight count drops below the new cap.
The brain may explicitly assign a permitted per-grunt profile within user
policy; assignments never trigger implicit task-based auto-routing.

Illustrative fields in this document are not a patch to AgentSpec.

## Grunt task contract

The brain supplies a versioned contract containing:

- Task ID, attempt ID, parent session, role, display name, and grunt profile
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

Grunts work in separate worktrees. The brain serializes integration onto
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
grunt transcript, prior review conversation, or sibling review results.

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
fix tasks go to grunts. Brain adjudication can update the same PR note without
erasing the independent reviewer findings.

## Omnigent / MultiDex boundary

Omnigent owns BGR selection and configuration UX, profile revisions, dynamic
grunt-cap enforcement, task/round persistence, integration/publication, review
scheduling, GitHub delivery, and parent inbox correlation.
MultiDex owns executing a role-constrained job with an explicitly selected
provider/model, resolved variant/speed/reasoning, compatible harness, context
mode, tool policy, cancellation, and structured result.
Native harness adapters translate the contract; they must not start an inner
orchestrator that competes with Omnigent.

The companion design defines the conceptual job/result and capability handshake.
Negotiate a protocol version and capabilities before dispatch. An older backend
can continue serving ordinary sessions, but must fail closed for unsupported
role isolation or fresh-review guarantees.

## Cross-provider team messaging

BGR must support communication among OpenAI-backed, MultiDex/Codex-native, and
Claude-backed brain, grunt, and reviewer sessions. MultiDex is a harness and
execution backend, not an LLM vendor. Model APIs do not communicate directly:
harness adapters expose host messaging tools, and Omnigent routes authenticated
envelopes. Agents receive no messaging credentials and make no TCP, shared
filesystem, or same-host assumptions.

Reuse the existing child-dispatch, inbox, session-event, and host-routing
primitives, but do not treat peer messaging as a correlated request/reply
mailbox. `sys_session_send` dispatches only to direct children; same-session
nudge/continuation and completion use the inbox path, including
`sys_read_inbox`. `runner/subagent_work.py` supplies result receipts, recovery,
and parent-wake retries. `sys_session_message` uses an authenticated HTTP POST
to `/v1/sessions/{target}/events` with type `session_message` for queue/steer
delivery and host/replica rerouting. Queueing is not execution; replies are not
automatically forwarded, and uncertain deliveries are not automatically resent.
These are source observations, not live validation. Preserve the existing
[messaging tool and approval boundary](session-messaging-tool-boundary.md).

Add BGR team send, receive, and acknowledgement semantics at runtime; exact
tool and API names remain deferred. Do not broaden `sys_session_send` to peers
or silently apply its child-tool approval exemption to peer sends. Preserve
native approvals and the existing server gates: `SESSION_ORCHESTRATION`, same
ownership, a private source after accounting for inherited sharing, and the
bound runner. Peer sends from shared sessions are unsupported until a
separately reviewed authorization change; fail visibly during preflight rather
than bypassing the gate.

A team roster binds immutable team and session IDs to each role and visible
`brain`, `grunt-*`, or `reviewer-*` name. A scoped team ACL controls permitted
communication edges. Brain-to-grunt, brain-to-reviewer, grunt-to-grunt, and
reviewer/grunt or reviewer/reviewer communication are supported only where
role and phase policy allow. Communication grants no planning, spawning,
editing, or publication authority. The brain observes task communications and
remains the sole assigner and adjudicator.

Preserve independent review. A reviewer starts with only the frozen code and
contract. Route factual clarification requests through the brain; responses
contain minimal source-grounded facts, never implementation transcripts or
another reviewer's findings. During independent review, block or defer lateral
reviewer messages that expose findings or deliberation. Enable findings
exchange only after all configured reviewers' independent outputs are sealed;
later discussion cannot rewrite those results. A new revision always gets fresh
review context.

Each conceptual envelope carries `protocol_version`, `message_id` and
`idempotency_key`, `team_id`, `source_session_id`/role/display name,
`destination_session_id`, `task_id` or `round_id`, optional reviewed head SHA,
kind (`task`, `question`, `answer`, `progress`, `result`, or `cancel`),
`correlation_id`/`in_reply_to`, payload or artifact references, sequence, and
creation/expiry timestamps. The runtime stamps the authenticated source; text
labels are not authorization. Validate same-team membership, source
permissions, and payload bounds; strip secrets and scope artifact access to the
team.

Persist a message before accepting it. Use a durable per-recipient mailbox and
delivery outbox with consume/ack cursors, at-least-once transport, deduplication,
and ordering per recipient (no global ordering promise). Distinguish accepted,
delivered, consumed, and replied states. Combine push wake notifications with
pull/drain catch-up after reconnect. Retry known IDs only through the new
idempotent broker contract, never by blindly resending existing
`sys_session_message` calls. Make timeouts, backpressure, expiry, dead-letter
state, cancellation, and stale-SHA rejection visible to the brain. An arbitrary
incoming message must not resurrect a completed job; require explicit task
continuation.

The MVP uses existing Omnigent persistence and event transport behind a mailbox
interface; Redis is not mandatory. If measured load or multi-host operation
demonstrates a need, an optional Redis Streams/consumer-groups adapter may
provide pending/ack/reclaim, retention, and backpressure under the same durable
envelope, ACL, and recovery contract. Plain Redis Pub/Sub is insufficient for
offline/restart durability. Agents never connect to Redis directly. These
design proposals implement no messaging service.

Future acceptance tests cover cross-provider brain-to-grunt-to-brain round
trips, grunt peer question/reply, reviewer factual clarification, and
post-seal reviewer exchange; offline/restart replay, duplicate IDs, unavailable
hosts, denied native approval, wrong-team or spoofed senders, shared-source
preflight, stale rounds, and review isolation. These tests have not been run.

## Delivery stages and future acceptance tests

1. Runtime and CLI: typed roles/contracts, capability checks, dynamic grunt
   concurrency enforcement, publication checkpoint, durable fan-out, structured
   results, dual delivery.
2. Server/host UI: separate Polly and BGR selection; active reviewer count;
   independent reviewer profiles; brain/grunt defaults; dynamic grunt cap;
   task/round status, revision labels, incomplete/stale indicators, and brain
   decisions.
3. Reliability and security: restart reconciliation, duplicate events,
   concurrent pushes, cancellation, reviewer failure, and untrusted-code isolation.

Acceptance tests for implementation (not tests performed by this design PR):

- Polly remains available alongside opt-in BGR; existing Polly sessions and
  defaults do not change. BGR exposes all required reviewer, profile, and
  dynamic-cap controls in the server/host UI.
- BGR exposes one or two active reviewers, each with independent
  provider/model/variant/speed/reasoning settings.
- The brain default resolves at session creation; config revisions apply to
  future grunt jobs/review rounds without silently mutating active sessions.
- Capability-aware profiles expose supported variants, speeds, and reasoning;
  invalid explicit combinations fail rather than remapping. Harness selection
  does not replace provider/model settings.
- The brain starts only needed grunts up to a dynamic cap enforced across
  turns/restarts; reviewers do not consume it. Excess work queues or is rejected,
  and a lowered cap never terminates in-flight grunts.
- Grunt default selection is used unless the brain explicitly assigns a
  permitted profile within user policy; no implicit auto-routing or required
  static worker pool.
- Grunts and two distinct reviewers retain their exact resolved
  provider/model/variant/speed/reasoning selections and configuration revisions.
- When two reviewers are active, they inspect the same SHA with empty
  conversation histories; another push creates fresh sessions with newly
  allocated unique reviewer display names.
- Role policies reject nested delegation, grunt publication, reviewer source
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
