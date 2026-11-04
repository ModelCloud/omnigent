# BGR controller foundation (protocol version 1)

This is a durable control-plane foundation, **not a working BGR execution mode**.
The [full design](BRAIN_EXECUTOR_REVIEWER_DESIGN.md) remains the target. No Polly
behavior, seeded agent, or runtime prompt changes are included. New Chat has a
configuration-only BGR entry; it is not a runnable catalog agent.

The lease heartbeat uses the same exact live host/native identity checks and
binding-only renewal while final attachment is pending; it does not treat a
healthy persisted replacement as normal attached input authority. A changed
host/runtime/thread or lost native identity still requires reconciliation.

## Brain evaluation of independent reviews

The sealed-sibling barrier is not a Brain decision. Round status exposes an exact
`results_sha256` only when every independent reviewer is sealed. The digest binds
the round, repository/PR, frozen base/head, role identities and actual results;
publication metadata and Brain decisions cannot change it.

After reading all findings and coverage limits, the current Brain calls the
existing `sys_bgr_control` tool:

```json
{
  "action": "evaluate_reviews",
  "action_id": "<fresh 32-hex identifier>",
  "round_id": "<round identifier>",
  "evaluation": {
    "results_sha256": "<exact status digest>",
    "next_action": "accept",
    "rationale": "Evidence-based evaluation of every independent result"
  }
}
```

`next_action` is `accept`, `revise` or `rereview`; incomplete coverage must not be
silently treated as no findings. The server checks owner/Brain/fence, the sibling
barrier and exact evidence, then atomically records the decision and idempotent
control response in existing SQL records. Exact retries return that response;
changed arguments/IDs cannot replace the decision for that epoch. History is
bounded, and a new runtime epoch must evaluate the evidence again. Existing
round documents default to no decision; no schema migration is needed.

Previews remain readable after the sealed barrier. Both controller publication
and direct trusted-ledger claims require a current evidence-bound decision, but
**any decision outcome permits publication**: findings, no-findings and incomplete
reviews all still require their own new PR discussion comments. This does not
relax the exact sanitized-body approval, credential isolation or uncertain-write
reconciliation rules.

The fixture driver consumes actual sealed results before choosing a decision:
complete/no-findings accepts; findings create one revision Grunt, then block for
a new immutable head; incomplete coverage starts one fresh independent round,
then explicitly blocks if still incomplete. It never manufactures revision or
coverage success. `publish:false` previews every result without any forge write.

To verify without publishing, run the controller-driver, review-store and remote
publication tests. In an authorized no-publish runtime, inspect tool history for
`status` with real results → `evaluate_reviews` → each `publication_preview`.
Findings/incomplete must choose the corresponding follow-up action, not a blind
success message. A stale hash or decision-free publish must fail before a POST.

## New Chat configuration preview

On an ordinary host, open New Chat's harness/agent picker and choose **BGR →
Configure draft** beside Polly. The responsive dialog has exactly one **Brain**
(`brain`), a **Grunt default** for dynamic `grunt-<unique>` workers with an active
cap of 1–64 (default 4), and one or two **Reviewer** profiles for fresh
`reviewer-<unique>` sessions per publication. Each profile preserves harness,
provider, model, variant, speed and reasoning independently. No worker/session
names or identities are fabricated by the form.

Each role follows **Harness → Model → Provider → Variant → Reasoning**.
Harness choices come from the existing agent catalog with shared friendly labels
(for example, `codex-native` displays as **MultiDex**, not as a new wire ID).
The host model catalog supplies one ordered selection flow, including ungrouped
catalogs; there is no extra catalog-profile picker. **Provider** means “Service
serving this model.” Single choices display as static values after selection,
but even a sole model can be explicitly selected from an empty draft. Fast-only
or otherwise non-regular models require an explicit variant choice; merely
choosing their family never commits an implicit fast variant.

Applying an advertised model/variant replaces its four routing/metadata values;
absent provider metadata displays as “Not advertised” and stays blank rather
than being inferred from a display label or subscription source. Numeric catalog
speed is saved as a string to match `Profile.speed`; it describes relative endpoint performance, **not an
enforced execution mode**. Manual values, a retired harness/model and reasoning
not advertised by a replacement catalog are retained with visible unverified-value
notices. **Advanced manual overrides**, collapsed by default for every role,
exposes all six raw fields without duplicating the primary pickers. A manual
model/provider/variant/speed combination that differs from an advertised row
does not claim that row as the active catalog selection. Changing harness never
silently clears profile fields. Profiles are drafts, not applied or
runtime-validated configuration.

**Save draft** writes a versioned browser-local record, including incomplete
profiles, under `omnigent:bgr-draft:v1:` scoped through the existing New Chat
server/user/project identity plus the selected host ID. It does not create SQL
teams or call runner-authenticated `/v1/bgr` APIs: those require an existing
private source session and trusted runner authentication. No authentication is
weakened. Provider/model must be filled before a draft could describe a complete
team; the field lengths, reviewer count and grunt cap are checked on save.
Browser reload/reopening restores saved values; unsaved edits are labelled and
are discarded on explicit close. Storage read/write errors are visible and a
failed write never claims success or overwrites the previous record. Do not put
credentials in drafts; they do not sync between browsers or configure a host.

**Launch BGR (unavailable)** stays disabled even for complete drafts, with missing
worker lifecycle, role enforcement and reviewer isolation explained. While the
dialog is open, the ordinary New Chat submit button, keyboard and form-submit
paths cannot create a generic session. Closing returns to the previous normal
agent and composer draft. No host/identity means no saved draft; offline hosts
allow manual draft editing without catalog probes. Managed sandboxes and
deployment-curated agent menus do not expose the BGR preview. This does not add
BGR to fork, schedule or project-default catalog pickers.

The normal server constructs `BgrStore` from its application database URI and
injects it into `create_app`. Embedded servers opt in with the `bgr_store`
argument. Six additive Alembic tables store teams, agent identities, contracts,
attempts, mailbox envelopes and immutable native-thread mappings. No Redis, service, credentials, or separate file
ledger is required. Normal database migration on startup installs the schema.
The tables use application-database workspace scoping, including when transcript
storage is configured separately.

### Storage limits and schema review

All six BGR document columns use the existing
`CompressedText(max_stored_bytes=16_383)`. The limit applies to actual stored
bytes, including UTF-8, compression and framing, on every insert and update.
It covers embedded profiles, configuration and checkpoints as well as envelopes.
Character limits alone do not guarantee acceptance: high-entropy multibyte text
can exceed the stored-byte cap even with otherwise valid fields. A rejected
mutation returns HTTP 400 `invalid_input`, with no partial reservation, revision
or checkpoint update. Save large evidence in artifacts and keep small references
in the ledger. Compressible documents larger than 16,383 uncompressed bytes
remain valid if their stored frame fits. The separate 8000-byte envelope payload
and contract-delivery limits remain unchanged.

The separately stored owner ID is limited to 320 Unicode characters before any
team creation writes (at most 1280 UTF-8 bytes). Oversized identities return
`invalid_input` without partial team/brain rows. This BGR-local limit does not
change authentication or user IDs elsewhere.

The indexed timestamp columns are `BIGINT` Unix epoch microseconds:
`lease_expires_at_us`, `consumed_at_us`, `replied_at_us`, `discarded_at_us`.
Lease comparisons and timestamp writes use integers; JSON/API timestamps retain
Unix seconds for compatibility. The normal clock uses `now_epoch_us`; injected
second-based test clocks are converted once at the store boundary. Workspace IDs
are supplied explicitly by the application, never a database default. Native
thread UUIDs use the existing binary `Uuid16` SQL type, while native-facing
bindings and RPCs retain canonical lowercase hyphenated UUID strings.

**Pre-merge compatibility:** the new BGR create-table migrations were corrected
in place during review. They import only existing shared SQL helpers and run
without BGR application records/stores. A database already created from earlier
versions of these same unmerged revisions is **not** converted by `upgrade head`.
Do not run this code against such a schema. Preserve its data and arrange an
explicit conversion if needed; do not silently reset it or use downgrade as a
data-preserving repair. Fresh databases and databases at the pre-BGR base are
covered by upgrade/downgrade tests. The required standalone-schema PR extraction
and database-owner approval remain pending; this combined PR is not ready to
merge or deploy. No separate schema PR has been opened.

## Authentication and existing session integration

### Additive native general-message adapter

The opt-in `BgrNativeMailbox` accepts already-bound private native connections;
it does not create sessions, install callbacks, start turns or schedule workers.
Host-created messages must exist in the SQL ledger before native source recording
and destination delivery. Both operations are idempotent. Native `hostRecord`
is required; native acceptance is never recorded as durable consumption. Pending
host messages survive loss of native process-local mailbox metadata.

Native arbitrary message IDs (including `clarify-contract-1`) retain their source
identity and exact reply anchors through durable origin metadata. Task-linked
questions, progress and results may share one task anchor; only answers and status
replies settle a one-shot reply slot. Source thread, role, assignment, epoch,
correlation and immutable head are checked before persistence and routing.

Callback payloads are recursively sanitized with trusted-host private values
before SQL persistence. Native `no_findings` and `incomplete` results are translated
factually into validated sealed results without inventing coverage. Native result
forwarding waits until all sibling jobs seal; questions remain live. The captured
capability fixture keeps native durability, restart recovery and consumption
receipts false. These adapter checks do not demonstrate isolated process startup,
provider execution, a complete Brain-facing store projection barrier, controller
lease recovery or automatic publication draining. Launch remains unavailable.

Every endpoint uses the existing session-orchestration checks:

- An authenticated owner, not an editor, reader, or administrator bypass.
- `X-Omnigent-Orchestration-Source`: the actual source session ID.
- `X-Omnigent-Runner-Tunnel-Token`: that session's bound runner token.
- A live private source session, including inherited sharing/archival checks.
- The existing `session_orchestration` feature must be enabled.

The team's source becomes its one logical `brain`. Worker reservations attach an
**existing** private, same-owner session created by that brain, either through
the parent relationship or the existing trusted creation-attribution label.
Existing child-session creation tooling can establish this relationship; the BGR
API itself never launches a session. The reservation's host and worktree must
match the worker's persisted session metadata. Senders are derived from the
authenticated session and cannot be supplied in an envelope request.

Reservations protect **ledger writers only**: they are not OS locks, host-side
symlink resolution, or execution fences. Existing session execution, actual
process stop, runner enforcement and safe-boundary transport are not controlled
by these APIs yet. Do not use them as evidence that an arbitrary worker is safe
to execute a BGR role.

## HTTP surface

All paths below are relative to `/v1/bgr`; OpenAPI supplies complete schemas.
IDs are lowercase 32-character hex UUIDs. Commit anchors are full lowercase
40-character SHAs. Unknown fields, type coercions and oversized payloads fail
validation. Resource conflicts return 409; ownership failures return 403.

| Method/path | Effect |
| --- | --- |
| `GET /capabilities` | Reports protocol 1, ledger/mailbox support, and explicit missing runtime capabilities |
| `POST /teams` | Creates one brain and a profile/configuration snapshot; exact replay is idempotent (201) |
| `GET /teams/{team_id}?limit=50&cursor=...` | Brain-only bounded recovery page: team/lease, agents, tasks/attempts, pending envelope lower bound and continuation cursor |
| `POST /teams/{team_id}/lease` | Acquires/renews a coordinator lease (`controller_id`, optional `ttl_seconds`, 5–300, default 60) |
| `PUT /teams/{team_id}/configuration` | Fenced configuration update for future assignments; increments `config_revision` only on change |
| `POST /teams/{team_id}/tasks` | Fenced, atomic grunt/task/attempt/workspace reservation **and task envelope** (201) |
| `POST /teams/{team_id}/checkpoints` | The assigned worker saves a bounded coarse checkpoint in its current epoch |
| `POST /teams/{team_id}/attempts/{attempt_id}/reattach` | Brain explicitly reconciles a surviving reserved attempt into the current epoch |
| `POST /teams/{team_id}/attempts/close` | Brain records a terminal outcome; requires literal `stop_confirmed: true` before releasing the writer reservation |
| `POST /teams/{team_id}/envelopes` | Durable acceptance receipt (202), not execution or completion |
| `GET /teams/{team_id}/mailbox?limit=50` | Recipient-only ordered pending envelopes (neither consumed nor explicitly discarded); limit 1–100, no destructive dequeue |
| `POST /teams/{team_id}/mailbox/ack` | Recipient explicitly records consumption of an envelope in its live epoch |
| `POST /teams/{team_id}/mailbox/discard` | Recipient atomically discards 1–100 explicitly identified stale envelopes, with durable reconciliation evidence; never successful consumption |
| `POST /teams/{team_id}/launch` | Always refuses (503 `runner_capability_mismatch`); execution/isolation are not implemented |

Brain mutations carry `controller_id` and `epoch`; worker messages/checkpoints
and acknowledgements carry `epoch`. Worker sends omit `controller_id`. A live
lease rejects a competing controller. Expiration increments the epoch on the
next acquisition, even for the same controller. Old-epoch mutations fail closed.
Read-only mailbox retrieval remains available for reconciliation after expiry.
Reattachment changes the attempt's epoch, not its task, session, profile,
workspace, checkpoint or historical envelopes. Old envelopes remain visible as
stale evidence and cannot be acknowledged or replied to under a new epoch.
The recipient must explicitly reconcile/discard superseded deliveries before
proceeding, not blindly execute or redeliver them. Nothing clears them implicitly.

Profiles preserve provider, model, optional variant/speed/reasoning and optional
harness, without guessing defaults or model-family substitutions. One or two
reviewer profiles are required, but no reviewer session/channel can be used yet.
The [Brain-to-Reviewer instruction contract](BRAIN_EXECUTOR_REVIEWER_DESIGN.md#required-brain-to-reviewer-instructions)
requires a new, sanitized PR discussion comment for each review, through a
trusted publisher without giving reviewers publication credentials. That prompt
dispatch and publishing lifecycle are not implemented by this ledger.
Profile values are ledger snapshots, **not validated backend capabilities**.
The active brain profile is pinned; configuration updates apply to future grunt
assignments only. Lowering the cap never kills or discards existing reservations.
Terminal close preserves historical agent/session membership, rather than
reassigning that identity to a new writer. New envelopes must connect the team's
brain and the attempt's assigned worker: neither an active sibling nor a terminal
worker can send on someone else's attempt. Terminal attempts reject checkpoints,
reattachment and new sends. Historical mailbox operations and exact receipt/close
replays grant no execution authority. Closing an old attempt again cannot release
a replacement attempt's writer reservation.

### Recovery pagination

`GET /teams/{team_id}` accepts `limit` (1–100, default 50) and an optional opaque
`cursor`. Each response contains at most `limit` agents and `limit` tasks, each
ordered by ascending ID; every returned task includes its own attempt. Reads
fetch only one lookahead per collection. Attempt lookups use at most `limit`
unique workspace/task keys, not the team's complete attempt history. Team/ID
and pending-mailbox indexes support the continuation and filtered reads.

Pass `next_cursor` unchanged to continue, including after reconstructing the
server/store. It tracks the two collections independently, so completing one
does not restart it while the other continues. `next_cursor: null` means both
collections are complete; an empty team returns just its brain and no task page.
Malformed, oversized or foreign workspace/team cursors return `invalid_input`.
Cursors grant no authority: each page rechecks ownership and the brain session.
Recovery stays readable after lease expiry for explicit reconciliation.

`pending_envelopes` is now **bounded**, not always an exact count: it is
`min(current_pending, limit)`. `pending_envelopes_has_more: true` means additional
unconsumed, undiscarded envelopes exist beyond that lower bound. This summary
is recomputed on each request and is independent of the task/agent cursor;
mailbox retrieval remains the recipient-only API for actual envelope delivery.
No unbounded `COUNT(*)` is used. Reservation and send capacity checks also use
indexed SQL limits instead of loading all historical attempts/envelopes. These
bounds apply even if retained data exceeds a product quota.

Continuation is deterministic for an unchanged ledger, **not a frozen multi-page
snapshot**. Concurrent membership/assignment changes can introduce IDs behind a
cursor or change returned attempt state. Fence competing controllers and restart
from the first page when a complete current traversal is needed. No cursor is a
lease, acknowledgement, scheduling instruction or runtime-resumption guarantee.

### Reservation example

After creating a team (`team_id`, `config`) and acquiring its lease, submit:

```json
{
  "controller_id": "11111111111111111111111111111111",
  "epoch": 1,
  "task_id": "22222222222222222222222222222222",
  "session_id": "33333333333333333333333333333333",
  "contract": {
    "objective": "Fix the parser bug",
    "instructions": "Use the reserved worktree; return test evidence",
    "acceptance": ["Parser regression test passes"],
    "checks": ["pytest tests/test_parser.py"],
    "artifact_refs": []
  },
  "workspace": {
    "host_id": "44444444444444444444444444444444",
    "repository": "owner/repo",
    "worktree": "/work/parser-fix",
    "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
```

The response includes the generated `grunt-<uuid>` agent, its unique attempt,
session/workspace/profile/config revision and stable task identity. An optional
explicit `profile` overrides the configured default for that reservation.
Reusing the same task ID and assignment returns the original task without a
second grunt or task envelope; changed content conflicts. Global workspace and
session uniqueness are enforced within the application's workspace, across teams.
The same host/worktree cannot be reserved again until explicit terminal close.

## Mailbox semantics

`Send` includes `message_id` (sender-scoped idempotency key), recipient agent,
task, attempt, epoch, kind, payload, optional head SHA and correlation fields.
The host stamps envelope ID, team, sender agent/session/role/name, sequence and
acceptance time. The receipt contains `envelope_id`, `message_id`, `sequence`,
and `state: accepted`; it says nothing about execution. Exact replay during the
live epoch returns the original durable receipt, including after acknowledgement.
Reusing a message ID with changed content conflicts.

`status_query` needs `correlation_id` and a payload of exactly one of:

```json
{"scope": "now"}
{"scope": "since_time", "since_time": "2026-11-03T00:00:00Z"}
{"scope": "since_commit", "since_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
```

`since_time` must be UTC. A query only adds an envelope: it never creates a task,
attempt, worker, turn or replacement job. `question` also requires correlation.
`status_reply`/`answer` carry the original correlation and `in_reply_to` equal to
the original **message ID**, not its envelope ID. The store verifies reversed
sender/recipient, matching task/attempt/epoch/head SHA and question/reply kind.
One durable reply seals that question; a changed second reply conflicts.
Reply acceptance stamps `replied_at` on the question, independently of explicit
consumption acknowledgement (`consumed_at`).

The payload is JSON-only, at most 8000 UTF-8 bytes, with no NaN/infinities. The
initial contract delivery must fit the same bound. Each team retains at most
256 tasks, 4096 retained envelopes, and 256 pending envelopes per recipient. Backpressure
fails the **entire** transaction: no orphan reservations or partial reply markers.
Retention currently stops acceptance rather than silently deleting evidence.

### Explicit stale-envelope reconciliation

After acquiring the new lease/epoch and reconciling surviving attempts, the
authenticated **recipient session** sends:

```http
POST /v1/bgr/teams/{team_id}/mailbox/discard
```

```json
{
  "epoch": 2,
  "envelope_ids": ["55555555555555555555555555555555"],
  "reason": "Survivor reattached; superseded task delivery reconciled from checkpoint"
}
```

The body requires the current live lease's integer epoch, 1–100 distinct envelope
IDs, and a nonblank reason of at most 512 characters. There is no caller-supplied
recipient or actor identity. The usual owner/private-source/bound-runner checks
apply. Only that source's own, unconsumed envelopes from **older** epochs qualify;
even the brain cannot discard a worker's mailbox on its behalf. Wrong owners,
unknown identities or another recipient's envelopes fail 403. Expired/stale
epochs, current/future envelopes, consumed envelopes or changed discard decisions
fail 409. Invalid/duplicate/oversized ID lists and blank reasons fail 422.

Success (200) returns the original envelopes in request order, each with a new
`discard` object containing `epoch` (the reconciled epoch), `session_id` (the
authenticated recipient), `reason`, and `discarded_at` (server timestamp).
Original IDs, sender/correlation fields, sequence, payload, original epoch,
`consumed_at` and `replied_at` are preserved. An undiscarded envelope has
`discard: null`. Discard is **not** successful consumption or a synthetic reply.
Discarded questions cannot accept replies, and discarded deliveries cannot later
be acknowledged as consumed.

The batch is atomic: any invalid or unauthorized entry rolls back every change.
An exact retry in that live epoch, including after app/store reconstruction,
returns the same evidence/timestamps; changed epoch/reason conflicts. Discarded
rows leave pending delivery, pending counts and recipient backpressure, so old
pages no longer starve fresh delivery. They remain in SQL and count toward the
4096-envelope retention ceiling. Nothing is deleted, relaunched, automatically
redelivered or silently called consumed. Iterate explicitly over remaining stale
pages if there are more than 100 entries. The additive mailbox-discard migration
indexes the separate nullable `discarded_at` column; audit metadata stays in the
compressed envelope record. Database downgrade is not an evidence-preserving
application rollback: stop controllers and back up/reconcile state first.

This is durable pull transport, not a runner callback dispatcher. No polling or
waiting loops are introduced. Runtime event subscriptions, safe-yield injection,
push callbacks and outbox execution remain follow-up work.

## Opt-in codex-native protocol adapter (not runtime integration)

`omnigent.harnesses.codex_native.bgr.CodexNativeBgrAdapter` is a trusted Python
helper over the existing `CodexRequestFn` (`CodexAppServerClient.request`), not
a public launch API, scheduler, new event loop, or automatic callback registration.
It translates already persisted host `status_query` envelopes and imports native
`status_reply` events through real `BgrStore.send`. The source of truth remains
Omnigent SQL. Tests use an external Claude-profile brain with **only a worker**
on the native connection; no native brain, proxy model, fictional brain task or
brain attempt is allocated. This is not proof of cross-provider execution.

The baseline [MultiDex native types](https://github.com/ModelCloud/MultiDex/blob/design/brain-executor-reviewer-contract/codex-rs/protocol/src/bgr.rs),
[app-server types](https://github.com/ModelCloud/MultiDex/blob/design/brain-executor-reviewer-contract/codex-rs/app-server-protocol/src/protocol/v2/bgr.rs)
and [startup contract](https://github.com/ModelCloud/MultiDex/blob/design/brain-executor-reviewer-contract/docs/brain-executor-reviewer-design.md#implemented-backend-contract-version-1)
define the wire format. The adapter additionally **requires** the host-delivery
extension: `hostDelivery:true` and `bgr/message/deliver`. Earlier backends exposing
only same-app-server `bgr/message/send` are refused, not given a fake
`sourceThreadId`. All native BGR RPCs are experimental. Initialize the existing
client with `experimentalApi:true` before negotiating (the existing client
already does this).

Compatibility is pinned by the captured mock-provider native exchange from
[MultiDex 5e1d8040](https://github.com/ModelCloud/MultiDex/commit/5e1d8040f7ea52f3f1e0bd9ad4ef24040a3ad418),
retained in `tests/harnesses/codex_native/bgr-native-conformance.json`. Adapter
tests negotiate its unmodified capability document, including the report tool's
draft-07 schema, `definitions` and `$ref`, and preserve it in startup options.
The captured delivery, receipt and notification also round-trip through SQL,
rebinding only routing identities to the test reservation. This fixture is not
a live cross-provider execution test and adds no runtime feature exposure.

### Capabilities, mapping and startup obligations

`await CodexNativeBgrAdapter.negotiate(request, store, user_id=...,
brain_session_id=..., team_id=..., fence=Fence(...))` reads
`bgr/capabilities/read {}`. It requires strict version 1, brain/grunt roles,
`statusScopes:["now"]`, `messageKinds:["status_query","status_reply"]`,
`hostDelivery:true`, `liveReattach:true`, a positive `maxQueriesPerAttempt <= 128`,
and a nondeferred `bgr_report_status` function specification. Durable native
mailbox, restart recovery, reviewer sandbox, cross-host delivery and task dispatch
must all be false for the legacy contract. The additive general-message contract
is accepted explicitly too: all three status scopes, all eight message kinds,
`generalMessaging`, `messageCallbacks`, `taskDispatch`, evidence limits and
nondeferred `sendTool`/`replyTool` specifications must agree. Partial upgrades fail;
`consumptionReceipts:false` remains false rather than becoming an inferred receipt.
The legacy translator still starts only its status tool and does not enable
general transport or reviewers merely because negotiation succeeded. Its modern
schema regression is source-derived from
[MultiDex 99ac0e1f5](https://github.com/ModelCloud/MultiDex/commit/99ac0e1f50e7a93f585ee7b86941375dc5b6c278),
not a captured modern binary exchange. Missing/old/unsupported capabilities fail
with `runner_capability_mismatch`. There is no model-family/profile fallback.

`startup_options()` returns the negotiated `reportTool` unchanged in
`dynamicTools` plus `config` overrides `features.multi_agent:false`,
`features.multi_agent_v2:false`, `features.defer_mailbox_preemption:true`.
The caller must apply these to a **fresh worker** `thread/start`, resolve the
worker's explicit profile/catalog settings, bind before its first turn/history,
and enforce host permissions/worktree/tool ceilings. This helper starts no
thread or turn. `bind` requires the reserved worker profile's explicit
`harness:"codex-native"`; another/missing harness is not silently rerouted.

`await adapter.bind(attempt_id, native_thread_uuid)` reserves an immutable
workspace-scoped SQL mapping before `bgr/thread/bind {threadId,identity}`.
`NativeBinding` fields are `team_id`, `attempt_id`, `epoch`, `thread_id`; the
mapping table is `bgr_native_bindings`, keyed by workspace/attempt with a unique
workspace/native-thread constraint. The remaining identity comes from the live
attempt. Native thread IDs must be canonical lowercase hyphenated UUIDs; logical
host IDs remain lowercase 32-hex. Native `identity.sessionId` is always the
logical worker session, **never** the native UUID. A bind result must return the
exact worker identity and an uncancelled snapshot. A failed/ambiguous RPC leaves
the reservation intact for an explicit exact retry; it is not proof of successful
native binding. No unbind/reassignment or native process-restart recovery exists.

`BgrStore.bind_native`, `native_assignment`, and `native_query` require the owned
brain session, live lease/current controller, current reserved attempt and exact
binding. Thread/attempt remapping is forbidden, including after terminal close.
Native mappings are deliberately pinned to their original epoch. Advancing a
ledger attempt via `reattach` does **not** reauthorize an old native binding:
this backend cannot rebind that thread to a new identity/epoch. Fence externally
and use a separately reserved clean assignment; automatic fresh attempts under
the same logical task are still unsupported. Migration `a6d8e0f2b435` adds only
the mapping table; downgrading drops mappings and is not a safe live rollback.

The caller must authenticate the owning private sessions and route to their
trusted runner connection before using this helper. It is not a new authority
boundary or a substitute for the HTTP session/runner-token checks above.

### Delivery and receipt example

`await adapter.deliver(attempt_id, original_query_message_id)` loads the actual
persisted envelope using its original **message ID**, not server `envelope_id`.
Only unconsumed `status_query` with exact `{"scope":"now"}` is supported.
The target identity comes from the mapped attempt; the host sender attestation
comes exclusively from the authenticated persisted brain envelope. Example
`bgr/message/deliver` parameters:

```json
{
  "threadId": "12345678-1234-1234-1234-123456789abc",
  "sender": {
    "teamId": "cccccccccccccccccccccccccccccccc",
    "agentId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "sessionId": "11111111111111111111111111111111",
    "epoch": 1,
    "role": "brain"
  },
  "envelope": {
    "version": 1,
    "messageId": "33333333333333333333333333333333",
    "senderAgentId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "recipientAgentId": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "identity": {
      "teamId": "cccccccccccccccccccccccccccccccc",
      "agentId": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "sessionId": "22222222222222222222222222222222",
      "taskId": "dddddddddddddddddddddddddddddddd",
      "attemptId": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      "epoch": 1,
      "role": "grunt"
    },
    "kind": "status_query",
    "correlationId": "ffffffffffffffffffffffffffffffff",
    "inReplyTo": null,
    "payload": {"scope": "now"},
    "headSha": "4444444444444444444444444444444444444444"
  }
}
```

The native JSON-RPC response has
`result:{messageId:"33333333333333333333333333333333",accepted:true,duplicate:false}`.
The adapter checks exact message ID and strict boolean acceptance/duplicate,
rechecks the host fence, and returns the native `duplicate` flag. **Neither
acceptance nor a successful RPC acknowledges host consumption.** The original
mailbox envelope is untouched; a separate explicit recipient ack still requires
actual consumption evidence. Socket loss is ambiguous and is not automatically
retried or translated into success. Host delivery is not native cross-host
routing (`crossHostDelivery` remains false).

### Native replies and explicit inspect reconciliation

The caller's existing worker notification dispatcher can pass `bgr/message`
notification **params** to
`adapter.receive_notification(attempt_id, params)`. Example params:

```json
{
  "threadId": "12345678-1234-1234-1234-123456789abc",
  "envelope": {
    "version": 1,
    "messageId": "reply-one",
    "senderAgentId": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "recipientAgentId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "identity": {
      "teamId": "cccccccccccccccccccccccccccccccc",
      "agentId": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "sessionId": "22222222222222222222222222222222",
      "taskId": "dddddddddddddddddddddddddddddddd",
      "attemptId": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      "epoch": 1,
      "role": "grunt"
    },
    "kind": "status_reply",
    "correlationId": "ffffffffffffffffffffffffffffffff",
    "inReplyTo": "33333333333333333333333333333333",
    "payload": {
      "currentAction": "Implementing",
      "completed": [],
      "pending": ["Tests"],
      "blockers": [],
      "headSha": "5555555555555555555555555555555555555555",
      "dirty": true,
      "coverageLimits": ["Worker self-report; not a repository attestation"]
    },
    "headSha": "4444444444444444444444444444444444444444"
  }
}
```

The adapter validates thread, team, worker agent, logical session, task, attempt,
epoch, role, sender, brain recipient, original correlation, `inReplyTo`, query
scope and query head anchor. Top-level `headSha` must preserve the original
query anchor; observed worker head belongs **only in payload**. Status payloads
have strict native camelCase fields as above; nullable head SHA/dirty may be
omitted. Non-null head SHAs must meet the host's full lowercase 40-hex bound.
Unknown fields/types fail explicitly rather than being truncated or coerced.

Native reply IDs accept the backend's 1–128 ASCII alphanumeric/`-_.:` format,
including `reply-one`. **All** native reply IDs (even host-shaped ones) map to a
lowercase 32-hex host message ID using UUIDv5 in the URL namespace over compact,
key-sorted JSON of `["omnigent/codex-native/bgr/reply/v1", nativeIdentity,
nativeThreadId, originalNativeMessageId]`. This stable, domain-separated,
assignment/thread-scoped mapping survives adapter/store reconstruction. Query
message IDs, `correlationId` and `inReplyTo` are never remapped. The imported
payload preserves the native status fields and adds
`native_provenance:{thread_id:nativeThreadId,message_id:originalNativeMessageId}`.
The **entire** imported payload, including provenance and nullable fields, must
fit the host's serialized UTF-8 8000-byte limit, even though native permits
32 KiB. Oversize/malformed replies fail with explicit errors and no host write.

Import is a real worker-session `Send`, so durable store idempotency returns the
same receipt after restart, while reusing a native ID with changed status or for
another query reaches the store's content-conflict check. The original query
gets `replied_at`, not `consumed_at`. Out-of-order replies retain their own
query/correlation; stale/terminal/cancelled identities are not promoted.

`await adapter.inspect(attempt_id)` issues one explicit `bgr/thread/inspect`,
validates identity/cancellation and each pending query against the SQL ledger,
then imports retained replies through the same path. Retry is idempotent.
Native `pending` means unanswered, not unconsumed: a host-acknowledged query can
remain there until its reply arrives. Inspect compares its persisted content
without redelivering it or changing consumption evidence; `deliver` still refuses
consumed queries. Consumed-but-unanswered queries do not block importing other
valid replies. Discard, assignment and epoch fencing still apply.
Replies commit individually, not as an atomic snapshot batch: a later invalid
reply may fail after earlier valid replies persisted. There is no polling,
stored native cursor, automatic subscription, process restart recovery or
resumption. Native `sequence` is only a process-local watermark, never proof of
host consumption. This helper exposes no checkpoint/cancel operation; in
particular a native cancel ack could not justify host `stop_confirmed`/close.

## Verification and remaining stages

### Independent-review and publication core

`BgrReviewStore` reserves immutable repository/PR/base/head rounds with one or
two distinct reviewer identities. A trusted native adapter must activate each
reviewer only after atomic isolated snapshot startup; there is no browser or
model-facing activation API. Imported reviewer identities remain blocked. Once
activated, reviewer messages require the exact live assignment and head, and
can only address Brain. Structured results are sanitized and sealed atomically
with durable mailbox acceptance. Reviewers cannot read a round or sibling
results. These ledger checks do not implement the native sandbox themselves.
After a trusted host confirms a failed/cancelled reviewer has stopped (or never
started), `settle_incomplete_review` seals fixed incomplete lifecycle evidence.
It never manufactures findings or claims a model ran. Pre-start failures say
"Unable to execute; no isolated reviewer was started". Successful sibling results
can then publish; late callbacks cannot overwrite either sealed result.

The trusted host `BgrGitHubPublisher` reuses existing GitHub credential and
comment helpers outside reviewers. It requires every independent result to be
sealed and Brain approval of the exact sanitized body hash. Each job and
re-review gets a new comment with its own stable marker. A durable unique claim
permits one POST attempt. A crash, timeout or ambiguous response stays uncertain:
retry reads at most one 100-comment page, matches the original publisher account
and exact body, and never automatically reposts. This avoids duplicate writes
but may require operator reconciliation when the write never reached GitHub.
Pattern redaction cannot establish that arbitrary prose contains no private
information; approval of the sanitized body remains mandatory.

Credential-key context (including nested API keys, credentials, authorization,
private/access keys and headers) is removed before reviewer messages enter the
mailbox. Publication independently resolves known private values in the trusted
host: credential-named environment entries, user/project config, MultiDex provider
registration, resolved Codex auth/provider config and the selected forge credential.
None of that inventory is given
to reviewers or stored in the outbox. `preview` renders the body for Brain approval;
`publish` re-resolves the privacy context and refuses an approval for a different
body. Only the sanitized body/hash are frozen durably with the write claim. A
changed privacy context after an ambiguous write needs operator reconciliation,
never another POST. A missing credential inventory also blocks publication.

Publication remains **pending authorization** until the trusted controller stages
approval. This core does not yet automatically stage or drain jobs on completion
or restart; it must not be described as automatic per-review posting.

The additional review-round migration remains independently executable and
caps actual stored document bytes at 16,383. These core components are not yet
wired to launch, native reviewer startup or publication scheduling. Existing
capability responses therefore remain disabled. Unit tests use trusted-adapter
fixtures and a fake forge, not model-generated reviews or external comments:

```bash
.venv/bin/python -m pytest -q tests/stores/test_bgr_review_store.py \
  tests/db/test_migration_bgr.py --reruns 0
```

```bash
.venv/bin/python -m pytest -q tests/stores/test_bgr_store.py \
  tests/server/integration/test_bgr.py tests/db/test_migration_bgr.py \
  tests/harnesses/codex_native/test_bgr.py \
  tests/harnesses/codex_native/test_codex_native_app_server_rpc.py
```

These use existing migrated DB/runtime fixtures and real `create_app` HTTP routes,
reconstruct apps/stores for restart, and test permissions, correlation, cap races,
rollback, database/workspace isolation and upgrade/downgrade. No model, GPU,
credential, browser or external service is needed. They do not prove process
crash recovery or non-SQLite database behavior.
Adapter contract tests use real SQL acceptance/reply/restart and a fake sole
worker request boundary; a local fake WebSocket server additionally exercises
the existing JSON-RPC client's response multiplexing out of order. They prove
translation and fencing, not actual MultiDex execution or cross-provider turns.

Before enabling BGR execution or registering a working launcher, implement and
verify host capability negotiation, applied profile/catalog validation,
runtime-enforced role/tool/git restrictions, safe-boundary delivery with actual
callbacks, stop confirmation from a trusted host, independent fresh reviewer
contexts with sealed findings, and publication scheduling. This stage can
reattach surviving reservations but cannot replace a lost brain or allocate a
fresh attempt under an existing task; automatic recovery and attempt history
require a subsequent schema/API extension. Archiving/compacting bounded retention
and actual host-path canonicalization also remain explicit follow-ups.

## Process-loss fencing primitives (replacement disabled)

Public session events and MCP execution now resolve BGR membership from the durable
agent/team records, never session labels. Brain, Grunt and reviewer writes require
a live team lease and current native binding; old worker epochs and inactive attempts
are rejected. Generic stop/retry/relaunch operations cannot restart BGR-managed roles.
Interactive terminal attaches recheck authority on every browser write, including an
already-open WebSocket; read-only viewing remains available. These server-side checks
do **not** fence direct native TUI/app-server turns or establish process-domain quiescence.

The current host launcher **cannot prove complete descendant quiescence**.
`server.proc is None`, process-group termination, native cancellation, archive,
checkpoint and an empty mailbox are not whole-domain stop evidence. Escaped
descendants are not covered. Consequently this implementation never advertises
working process-loss replacement and never automatically restarts the initial task.
A trusted containment supervisor is required before enabling replacement.

`BgrRuntime` allocates a fresh 32-hex `runtime_id` per registration. Same-live
registration preserves that generation; unregister/re-registration does not.
`runtime/thread-ready.params` carries `thread_id`, `runtime_id`, `host_id`.
The immutable native Brain binding persists the same generation and host alongside
its epoch and thread UUID. Reattach requires all three to match and a successful
native identity inspection; native lease expiry alone cannot acquire a new epoch.

Private host RPC is `POST /v1/sessions/{brain_session_id}/bgr/runtime`:

```json
{"operation":"brain","parameters":{}}
```

It returns `thread_id`, `runtime_id`, `host_id`, `process_loss_recovery:false`.
After durable revocation, an authenticated owner recovery flow may request:

```json
{"operation":"stop_generation","parameters":{"runtime_id":"<exact old generation>"}}
```

This serializes with spawn, ordinary RPC, publication, stop and unregister. It
fences subsequent callbacks/operations and closes the owned app-server groups.
Its response is **not** stop evidence:

```json
{
  "runtime_id":"<exact old generation>",
  "host_id":"<registered host>",
  "domain_stop_confirmed":false,
  "reason":"Process-group shutdown cannot prove escaped descendant quiescence"
}
```

An owner can initiate revocation even without a live controller via
`POST /v1/bgr/teams/{team_id}/recover` with JSON `{}` and ordinary authenticated
owner credentials. The team, Brain, host and generation come from durable records;
caller-supplied fences or stop attestations are rejected. Revocation commits before
any runner RPC. The server checks the bound host/generation before requesting
`stop_generation`. Retries reuse the pending recovery. An unreachable runner,
generation mismatch or the current unverified process-group stop returns HTTP 202
with `stop_status:"unknown"`, `replacement_enabled:false` and recovery `stopping`.
Even a positive bare host boolean is not accepted as complete stop evidence. No epoch
advance, replacement launch or initial-input replay occurs through this endpoint.

The durable store APIs are:

- `runtime_recovery_target(user, team)`: owner-only durable Brain/native identity;
  intentionally does not require a live lease or live controller object.
- `resume_live_runtime_recovery(user, brain, team, fence, binding=...)`: trusted
  attach-only renewal of an already persisted replacement `binding`, after exact
  authenticated host metadata and native identity inspection. It does not bind a
  new generation, advance the epoch or unblock public input. Cached and cold
  controllers can retry that same live attach without rebinding native identity.
  `ready` without a durable replacement binding, a lost native identity, a changed
  host/runtime/thread, or missing stop proof remain blocked. Expired `ready`
  recovery still requires trusted process-domain reconciliation; this API cannot
  safely infer that an unrecorded replacement never started or has stopped.
- `begin_runtime_recovery(user, brain_session, team, fence, *, recovery_id,
  runtime_id, replacement_controller_id)`: valid even after old lease expiry;
  atomically revokes the old lease, blocks launch and records the previous
  controller, epoch, runtime and thread. Exact argument retries are stable.
- `check_runtime_input(user, brain_session, team)`: shared public-input guard;
  requires owner/Brain identity, a live lease and no unfinished recovery.
- `complete_runtime_recovery(user, brain_session, team, *, recovery_id, evidence)`:
  trusted-supervisor-only transition. Evidence must contain matching `runtime_id`,
  `host_id`, explicit `inventory_complete:true` (including starting/unbound process
  claims), and a `processes` entry for the exact Brain and every previous-epoch
  native binding. Entries contain `session_id`, `thread_id`, `role`, `process_tag`,
  `process_domain_id`, literal `stop_confirmed:true` and
  `domain_stop_confirmed:true`. Native ACKs, unknown domains and truthy substitutes
  do not qualify. The present host has no producer of such evidence.

Only verified completion advances the epoch and makes the next controller ready.
Old reserved attempts become failed with checkpoints preserved, unfinished reviews
become explicitly incomplete, and unconsumed old envelopes get unknown/not-replayed
discard evidence, never fabricated consumption. Publication claims remain intact
for reconciliation. The logical Brain/team and original launch request are unchanged.
The replacement must use a fresh native home/thread/runtime and a **separate recovery
context**, never the already-delivered initial task or temporary reviewer roots.

Recovery progresses `stopping → ready → binding → attached`. Normal input,
`sys_bgr_control`, callbacks and publication must remain blocked until `attached`.
Only trusted startup can install the one fresh native Brain binding in `ready`;
`mark_launch_running` finishes its attachment. A second different binding is denied.
The supervisor-free host remains in `stopping`; no launcher retry is authorized by
an UNKNOWN response. These are guarded ledger primitives, not an operator UI or a
successful integrated process-loss recovery demonstration.

```bash
.venv/bin/python -m pytest -q tests/stores/test_bgr_recovery.py \
  tests/harnesses/codex_native/test_bgr_runtime.py \
  tests/harnesses/codex_native/test_bgr_controller.py --reruns 0
```

Recovery tests inject explicit supervisor fixtures to exercise atomic state/fence
transitions. Host tests separately prove the real process-group adapter returns
UNKNOWN and blocks further operations; they do not claim escaped tools were reaped.
