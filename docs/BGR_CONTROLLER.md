# BGR controller foundation (protocol version 1)

This is a durable control-plane foundation, **not a working BGR execution mode**.
The [full design](BRAIN_EXECUTOR_REVIEWER_DESIGN.md) remains the target. No Polly
behavior, seeded agent, picker, or runtime prompt changes are included.

The normal server constructs `BgrStore` from its application database URI and
injects it into `create_app`. Embedded servers opt in with the `bgr_store`
argument. Five additive Alembic tables store teams, agent identities, contracts,
attempts and mailbox envelopes. No Redis, service, credentials, or separate file
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
| `GET /teams/{team_id}/mailbox?limit=50` | Recipient-only ordered unconsumed envelopes; limit 1–100, no destructive dequeue |
| `POST /teams/{team_id}/mailbox/ack` | Recipient explicitly records consumption of an envelope in its live epoch |
| `POST /teams/{team_id}/launch` | Always refuses (503 `runner_capability_mismatch`); execution/isolation are not implemented |

Brain mutations carry `controller_id` and `epoch`; worker messages/checkpoints
and acknowledgements carry `epoch`. Worker sends omit `controller_id`. A live
lease rejects a competing controller. Expiration increments the epoch on the
next acquisition, even for the same controller. Old-epoch mutations fail closed.
Read-only mailbox retrieval remains available for reconciliation after expiry.
Reattachment changes the attempt's epoch, not its task, session, profile,
workspace, checkpoint or historical envelopes. Old envelopes remain visible as
stale evidence and cannot be acknowledged or replied to under a new epoch;
the controller must reconcile delivery deliberately, not blindly redeliver it.

Profiles preserve provider, model, optional variant/speed/reasoning and optional
harness, without guessing defaults or model-family substitutions. One or two
reviewer profiles are required, but no reviewer session/channel can be used yet.
Profile values are ledger snapshots, **not validated backend capabilities**.
The active brain profile is pinned; configuration updates apply to future grunt
assignments only. Lowering the cap never kills or discards existing reservations.

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
256 tasks, 4096 envelopes, and 256 unconsumed envelopes per recipient. Backpressure
fails the **entire** transaction: no orphan reservations or partial reply markers.
Retention currently stops acceptance rather than silently deleting evidence.

This is durable pull transport, not a runner callback dispatcher. No polling or
waiting loops are introduced. Runtime event subscriptions, safe-yield injection,
push callbacks and outbox execution remain follow-up work.

## Verification and remaining stages

```bash
.venv/bin/python -m pytest -q tests/stores/test_bgr_store.py \
  tests/server/integration/test_bgr.py tests/db/test_migration_bgr.py
```

These use existing migrated DB/runtime fixtures and real `create_app` HTTP routes,
reconstruct apps/stores for restart, and test permissions, correlation, cap races,
rollback, database/workspace isolation and upgrade/downgrade. No model, GPU,
credential, browser or external service is needed. They do not prove process
crash recovery or non-SQLite database behavior.

Before making BGR selectable or registering a working launcher, implement and
verify host capability negotiation, applied profile/catalog validation,
runtime-enforced role/tool/git restrictions, safe-boundary delivery with actual
callbacks, stop confirmation from a trusted host, independent fresh reviewer
contexts with sealed findings, and publication scheduling. This stage can
reattach surviving reservations but cannot replace a lost brain or allocate a
fresh attempt under an existing task; automatic recovery and attempt history
require a subsequent schema/API extension. Archiving/compacting bounded retention
and actual host-path canonicalization also remain explicit follow-ups.
