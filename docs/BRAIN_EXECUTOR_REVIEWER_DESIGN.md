# BGR: brain, grunts, and reviewers

Status: **design with a staged ledger implementation**. The full execution mode
below remains proposed. The implemented, owner-authorized SQL ledger and mailbox
APIs are documented in [BGR controller foundation](BGR_CONTROLLER.md). They do not
launch workers, enforce runtime roles, deliver safe-boundary callbacks, or provide
independent review. BGR is not selectable in the UI yet.

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
kind (`task`, `question`, `answer`, `progress`, `status_query`, `status_reply`,
`result`, or `cancel`),
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

### Two-way progress ping/pong without replacing the task

The brain must be able to ask any assigned grunt or reviewer:

- "What are you doing now?"
- "Summarize your work since timestamp T."
- "Summarize your work since commit C."

This is a correlated control exchange over the team mailbox, not a new task,
task completion, cancellation, or generic steering instruction. The worker
answers and continues its existing assignment with the same session, attempt,
workspace, and task contract. A status request never starts a replacement
agent, consumes another grunt slot, or resets the worker's context.

**All brain/sub-agent communication is asynchronous by default.** Sending
returns after durable acceptance with a message/query ID and delivery state,
not after the recipient answers. The brain continues planning, assigning other
eligible work, or handling other results; it does not synchronously wait or
poll for the pong. Multiple questions may be outstanding and replies may
arrive out of order. Only an actual task dependency can prevent dependent work
from proceeding; the communication tool does not block the entire brain.

Treat each reply like an email envelope: authenticated sender session/agent ID,
role and display name; destination brain/team; message ID; `in_reply_to` and
correlation ID identifying the original question; task/attempt/revision; typed
payload; observation/creation timestamps; and status. Persist the original
question or its durable reference so a recovered brain can understand a reply
without remembering the question in model context.

The channel delivers replies as asynchronous inbox events/callbacks, nudging an
idle brain or making them available at its next safe boundary while busy.
This is runtime event delivery, not an arbitrary executable callback or a
second concurrent brain turn. The brain consumes and acknowledges each
envelope, then decides what to do next. A resumed brain drains unacknowledged
events; duplicate callbacks do not repeat decisions or task assignments.
Delivery order is not question order, so match by IDs, never "the last ask."
Timeout, refusal, cancellation, and unavailable-worker outcomes also arrive
as correlated status events; silence must not be treated as success. Transport
acceptance is distinct from an eventual agent reply.

| Message | Required content |
| --- | --- |
| `status_query` | Query/correlation ID, team/task/attempt and destination IDs, requested scope (`now`, `since_time`, or `since_commit`), optional UTC timestamp or full commit SHA, deadline |
| `status_reply` | Matching query/attempt IDs, observation time and evidence watermark, current phase/action, completed work within scope, pending work/blockers, current head and dirty-work status, check progress/results, artifact references, coverage/freshness limits |

The mailbox acknowledges acceptance promptly. At the next safe execution
boundary the worker reads the query, produces a bounded factual summary,
sends its reply to the brain's mailbox, and continues from its saved execution
position. The summary is an agent response, not just a transport heartbeat.
Deliver it mid-task: do not wait for normal task completion or route it through
the terminal-result handler. Keep it distinct from the worker's final result.

Do not run a concurrent model turn against the same conversation or edit
workspace while the worker is active. Harness adapters must support a safe
control-message boundary or report that capability as unavailable. Existing
queue/steer tools are building blocks, not proof that non-destructive status
queries already work on every harness.

If a long tool call is still executing, the runtime can immediately return
separately labelled observed status (for example, command running, last
checkpoint, or last heartbeat), with the agent-authored reply marked pending.
Wait for a safe yield; do not kill a test/build just to answer. A wedged,
disconnected, or unavailable agent yields a timeout/unavailable status with
the last observed time, never invented current activity. Cancellation or a
change of task remains an explicit separate brain action.

For time-scoped queries, summarize recorded work events after the requested
UTC timestamp up to the reply's watermark, using stable event sequence IDs
to handle clock skew. For commit-scoped queries, resolve and verify the exact
commit in the assigned repository and summarize relevant changes from that
anchor to observed HEAD; report uncommitted work separately. Include the
observed head SHA, comparison relationship, and limitations. A missing,
ambiguous, non-ancestor, or out-of-retention anchor returns an explicit
unsupported/partial answer; never silently substitute another anchor or infer
when work happened from commit author timestamps.

Use existing task events, bounded checkpoint summaries, code diffs, and test
artifacts to answer. Continuous minute-by-minute transcripts or hidden reasoning
capture are not required. A reply states what is observed, what the worker
reports, and what cannot be reconstructed; "exactly now" means an observation
at a disclosed timestamp, not a guarantee that activity has not since changed.

Persist query/reply IDs and delivery acknowledgements so retries deduplicate
without re-running work. Bound query frequency and response size; coalesce
redundant pending queries while acknowledging every correlation ID. Late
replies retain their original attempt/revision and cannot resurrect cancelled
work or satisfy a query addressed to a replacement attempt. Brain reconnection
can drain pending replies without repeating the assignment.

Reviewers may report their current inspection phase, files/checks covered,
remaining scope, and blockers. Status traffic does not carry sibling findings,
implementation transcripts, or instructions to change the independent verdict.
Findings stay in the sealed review-result path; requesting status must not
invalidate clean-context review.

Future acceptance tests should prove the brain makes useful progress after
sending, receives multiple out-of-order replies matched to their original
questions, and recovers callback envelopes after disconnect without polling or
duplicate assignments. They should also send a status ping during implementation and
during review, receive a correlated worker-authored pong before completion,
and verify the original task continues without a new session or lost work.
Also cover timestamp/commit scopes, dirty changes, invalid anchors, long-running
tools, duplicate queries, timeout, disconnect/reconnect, stale-attempt replies,
and reviewer isolation across the supported provider/harness combinations.
These are proposed tests, not executed behavior.

## Resumption after individual or collective failure

Recovery is a BGR requirement, not a promise to checkpoint every agent's
conversation or internal reasoning. Persist a compact **team recovery ledger**
in Omnigent's durable database, with content-addressed artifacts in durable
storage. Model context receives a bounded projection of that ledger; it is not
the source of truth. Runner memory, local worktrees, and optional Redis delivery
queues must not be the only copies. Reuse existing stores and session recovery
primitives rather than introducing a separate orchestration database.

### Minimum durable state

| Record | Recovery data |
| --- | --- |
| Team | Team ID, user/ownership policy, configuration revision, grunt cap, lifecycle state, active brain session ID, coordinator epoch |
| Plan | Goal, current plan/contract revision, acceptance criteria, dependencies, brain decisions and explicit unresolved questions |
| Assignment | Task ID, role, display name, logical agent ID, backend session/thread ID, host ID, attempt ID, assignment contract and profile snapshot |
| Progress | Coarse phase, last acknowledged checkpoint, blocker, lease/heartbeat time, intended next action, retry budget |
| Workspace | Repository and base/result SHAs, branch/worktree identity, durable patch/checkpoint reference and checksum, dirty/lost/available status |
| Evidence | Exact tested revision, check commands and exit codes, bounded log/artifact references, completed findings and adjudication decisions |
| Delivery/publication | Message IDs and acknowledgement cursors, pending outbox entries, publication intent and verified remote SHA/PR, review round/slot states |

Keep the immutable assignment contract or a durable reference to it, not merely
"task assigned to agent X." A fresh worker needs the instructions to reconstruct
the work. Backend resume handles are optional and access-controlled; secrets
never enter the ledger or prompt projection. Preserve logical task identity
across attempts, but allocate a new attempt/session identity when replacing a
worker. Names remain presentation metadata with the specified role prefixes.

Checkpoint at meaningful boundaries: plan/assignment accepted, launch
acknowledged, implementation artifact available, checks completed, result
accepted, publication intended/confirmed, review sealed, and delivery
acknowledged. Heartbeats record liveness only, not continuous context snapshots.
A grunt may publish a bounded work-in-progress patch/checkpoint at a safe
milestone; this is a private recovery artifact, not permission to push a remote
branch or open a PR. Do not copy every tool output or token into recovery state.

### Recovery protocol

1. Load the ledger and acquire a coordinator lease with an incrementing fencing
   epoch. Only one active `brain` and coordinator may issue accepted assignments
   or publication operations for a team. Persist assignment intent and its
   outbox entry transactionally before dispatch.
2. Reconcile recorded attempts with reachable backends, artifact checksums, and
   actual repository/remote state. A missing heartbeat means **unknown or
   disconnected**, not definitely dead. Launch acknowledgement loss must not
   cause an unconditional second launch.
3. Reattach to an existing live session when its identity, profile, role policy,
   workspace, and attempt match. If its durable completion exists, consume that
   result instead of rerunning the task. Resume the provider's own session only
   when available and compatible; BGR does not require copying its transcript.
4. For confirmed loss, start a replacement attempt from the latest verified
   durable code artifact and task contract. The brain decides whether to salvage
   partial work, rerun a safe check, or revise the plan. Missing uncheckpointed
   work is explicitly reported as lost, never represented as recovered.
5. Restore mailbox cursors, reconcile pending external side effects, recompute
   in-flight grunt reservations, and then dispatch only eligible pending work.
   Unresolved attempts retain reservations until fenced or confirmed stopped.
   Old attempt/epoch results are retained for diagnosis but cannot advance the
   current task or publish a revision.

Fencing must apply at runner command and publication boundaries, not just to
database writes. During a partition a worker may finish an already-running
local command, but cannot gain new work after lease expiry. Before replacement
writes begin, confirm termination/isolation of the old attempt or allocate a
separate worktree and revoke its authority. Never let two attempts concurrently
write one workspace. If the backend cannot establish safe isolation, pause and
escalate instead of guessing that the old worker died.

### Failure cases

| Failure | Recovery behavior |
| --- | --- |
| Grunt disconnects but process survives | Reconcile and reattach by attempt/session ID; do not duplicate its work |
| Grunt/session/host is lost | Fresh attempt from contract plus last durable patch/commit; retain completed evidence only for matching code and environment |
| One reviewer is lost | Preserve other sealed outputs; retry only that slot with fresh context at the same frozen revision/configuration, never seed it with partial or sibling findings |
| Brain fails while workers live | Allow already authorized bounded work to checkpoint/finish while leases permit; persist results; no new assignments or publication until one recovered brain owns the epoch |
| All agents or the server restart | Recover team/plan/assignments from the ledger, restore available artifacts, reconcile backends and outboxes, then rebuild the brain's bounded recovery context |
| Push or PR-note acknowledgement is lost | Query remote SHA or stable note marker and reconcile intent; do not blindly repeat externally visible actions |
| Storage/artifact is unavailable or corrupt | Pause affected work; expose missing checkpoint and available restart point rather than fabricate a successful resume |

An interrupted reviewer may reconnect to the same still-live attempt, but a
replacement reviewer is always a new session/name. A changed head or review
configuration requires a new round, not reuse of a sealed result. Successful
completion of one slot is not approval while another is missing.

Do not automatically replay arbitrary tool commands: tests may themselves
have side effects. Unknown side-effect outcomes enter a reconciliation state
and require evidence or a brain/user decision. A cancellation remains durable;
restarting the team must not resurrect cancelled work.

### Brain recovery context and user controls

Resume the brain's existing provider session when valid. Otherwise create a
replacement physical session for the same logical `brain`, with a compact
recovery packet: goal and plan revision, decisions, task-to-agent/attempt map,
completed outputs, current revisions, pending reviews/messages, blockers,
budgets, and next eligible steps. Attach durable references for details.
This recovers orchestration intent, not the prior model's hidden reasoning.
Existing roster/child links must be reconciled to the new active brain binding;
preserve ownership/approval checks rather than spoofing old session parentage.

Expose team Resume/Pause/Cancel, per-agent reconnect/retry status, and the last
durable checkpoint in the BGR UI. Resume reconciles before dispatch; Retry
creates a new attempt only after the old one is safely fenced. Show whether
work was reattached, reconstructed, restarted, or lost. Configuration changes
and manual recovery decisions become new ledger revisions.

Durable storage still has finite retention and backup guarantees. If both the
ledger and backups are lost, reliable reconstruction is not promised. Artifact
retention must cover active/recoverable tasks; cleanup must not discard the only
checkpoint while an attempt or review can still resume.

Future crash-injection tests should cover loss before/after dispatch
acknowledgement, one grunt, one reviewer, brain-only and whole-team restarts,
partitioned workers, duplicate recovery coordinators, missing worktrees,
corrupt artifacts, and lost push/comment acknowledgements. Assert one active
brain, no duplicate workspace writers/publications, preserved task ownership
and concurrency limits, fresh replacement reviewers, and explicit accounting
for lost progress. These tests are proposed, not executed by this design PR.

## Delivery stages and future acceptance tests

1. Runtime and CLI: typed roles/contracts, capability checks, dynamic grunt
   concurrency enforcement, publication checkpoint, durable fan-out, structured
   results, dual delivery, and the durable recovery ledger with safe resumption.
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
