# BGR controller foundation (protocol version 1)

This is a durable control-plane foundation, **not a working BGR execution mode**.
The [full design](BRAIN_EXECUTOR_REVIEWER_DESIGN.md) remains the target. No Polly
behavior, seeded agent, picker, or runtime prompt changes are included.

The normal server constructs `BgrStore` from its application database URI and
injects it into `create_app`. Embedded servers opt in with the `bgr_store`
argument. Six additive Alembic tables store teams, agent identities, contracts,
attempts, mailbox envelopes and immutable native-thread mappings. No Redis, service, credentials, or separate file
ledger is required. Normal database migration on startup installs the schema.
The tables use application-database workspace scoping, including when transcript
storage is configured separately.

## Authentication and existing session integration

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
| `GET /teams/{team_id}` | Brain-only recovery snapshot: team/lease, agents, tasks/attempts, pending envelope count |
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

### Capabilities, mapping and startup obligations

`await CodexNativeBgrAdapter.negotiate(request, store, user_id=...,
brain_session_id=..., team_id=..., fence=Fence(...))` reads
`bgr/capabilities/read {}`. It requires strict version 1, brain/grunt roles,
`statusScopes:["now"]`, `messageKinds:["status_query","status_reply"]`,
`hostDelivery:true`, `liveReattach:true`, a positive `maxQueriesPerAttempt <= 128`,
and a nondeferred `bgr_report_status` function specification. Durable native
mailbox, restart recovery, reviewer sandbox, cross-host delivery and task dispatch
must all be false for this contract. Missing/old/unsupported capabilities fail
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
Replies commit individually, not as an atomic snapshot batch: a later invalid
reply may fail after earlier valid replies persisted. There is no polling,
stored native cursor, automatic subscription, process restart recovery or
resumption. Native `sequence` is only a process-local watermark, never proof of
host consumption. This helper exposes no checkpoint/cancel operation; in
particular a native cancel ack could not justify host `stop_confirmed`/close.

## Verification and remaining stages

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

Before making BGR selectable or registering a working launcher, implement and
verify host capability negotiation, applied profile/catalog validation,
runtime-enforced role/tool/git restrictions, safe-boundary delivery with actual
callbacks, stop confirmation from a trusted host, independent fresh reviewer
contexts with sealed findings, and publication scheduling. This stage can
reattach surviving reservations but cannot replace a lost brain or allocate a
fresh attempt under an existing task; automatic recovery and attempt history
require a subsequent schema/API extension. Archiving/compacting bounded retention
and actual host-path canonicalization also remain explicit follow-ups.
