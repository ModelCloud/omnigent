"""Host ledger/native worker contract seam, without a scheduler or model execution."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest
from websockets.asyncio.server import serve

from omnigent.bgr import Ack, Fence, LeaseRequest, NativeBinding, TaskClose, TeamCreate
from omnigent.db import workspace_scope
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexAppServerResponseError,
)
from omnigent.harnesses.codex_native.bgr import CodexNativeBgrAdapter
from omnigent.stores.bgr_store import BgrStore
from tests.stores.test_bgr_store import (
    BRAIN,
    CONFIG,
    CONTROLLER,
    OWNER,
    SHA,
    TEAM,
    question,
    reservation,
)

FENCE = Fence(controller_id=CONTROLLER, epoch=1)
THREAD = "12345678-1234-1234-1234-123456789abc"
NATIVE_CONFORMANCE = json.loads(
    Path(__file__).with_name("bgr-native-conformance.json").read_text(encoding="utf-8")
)
CAPABILITIES = NATIVE_CONFORMANCE["capabilitiesRead"]["result"]


@pytest.fixture
def ledger(db_uri):
    store = BgrStore(db_uri)
    config = CONFIG.model_copy(
        update={
            "brain": CONFIG.brain.model_copy(update={"harness": "claude-code"}),
            "grunt": CONFIG.grunt.model_copy(update={"harness": "codex-native"}),
        }
    )
    store.create(OWNER, BRAIN, TeamCreate(team_id=TEAM, config=config))
    store.acquire(OWNER, BRAIN, TEAM, LeaseRequest(controller_id=CONTROLLER, ttl_seconds=300))
    return store


@pytest.fixture
def worker(ledger):
    return ledger.reserve(OWNER, BRAIN, TEAM, reservation())


@dataclass
class WorkerRpc:
    """Only the sole target worker is native; the external brain never has a thread."""

    capabilities: dict = field(default_factory=lambda: copy.deepcopy(CAPABILITIES))
    identity: dict | None = None
    pending: dict = field(default_factory=dict)
    replies: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    receipt_override: dict | None = None
    cancelled: bool = False

    def snapshot(self):
        return {
            "snapshot": {
                "identity": self.identity,
                "cancelled": self.cancelled,
                "sequence": len(self.pending) + len(self.replies),
                "pending": list(self.pending.values()),
                "replies": self.replies,
            }
        }

    async def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "bgr/capabilities/read":
            assert params == {}
            return {"result": self.capabilities}
        assert params["threadId"] == THREAD
        if method == "bgr/thread/bind":
            self.identity = params["identity"]
            return {"result": self.snapshot()}
        if method == "bgr/thread/inspect":
            return {"result": self.snapshot()}
        assert method == "bgr/message/deliver"
        assert "sourceThreadId" not in params
        envelope = params["envelope"]
        assert params["sender"] == {
            "teamId": TEAM,
            "agentId": envelope["senderAgentId"],
            "sessionId": BRAIN,
            "epoch": 1,
            "role": "brain",
        }
        duplicate = envelope["messageId"] in self.pending
        self.pending[envelope["messageId"]] = envelope
        return {
            "result": self.receipt_override
            or {
                "messageId": envelope["messageId"],
                "accepted": True,
                "duplicate": duplicate,
            }
        }

    def reply(self, message_id, native_id="reply-one", **payload):
        query = self.pending[message_id]
        return {
            "threadId": THREAD,
            "envelope": {
                **query,
                "messageId": native_id,
                "senderAgentId": query["recipientAgentId"],
                "recipientAgentId": query["senderAgentId"],
                "kind": "status_reply",
                "inReplyTo": query["messageId"],
                "payload": {
                    "currentAction": "Implementing",
                    "completed": [],
                    "pending": ["Tests"],
                    "blockers": [],
                    "headSha": "e" * 40,
                    "dirty": True,
                    "coverageLimits": ["Self report, not repository attestation"],
                    **payload,
                },
            },
        }


async def adapter(ledger, rpc, **kwargs):
    return await CodexNativeBgrAdapter.negotiate(
        rpc.request,
        ledger,
        user_id=OWNER,
        brain_session_id=BRAIN,
        team_id=TEAM,
        fence=FENCE,
        **kwargs,
    )


async def bound(ledger, worker, rpc):
    bridge = await adapter(ledger, rpc)
    await bridge.bind(worker.attempt.attempt_id, THREAD)
    return bridge


def persist_query(ledger, worker, **overrides):
    query = question(worker, payload={"scope": "now"}, **overrides)
    ledger.send(OWNER, BRAIN, TEAM, query)
    return query


async def test_external_brain_query_roundtrip_replay_and_explicit_consumption(
    ledger, worker, db_uri
):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    options = bridge.startup_options()
    assert options == {
        "config": {
            "features.multi_agent": False,
            "features.multi_agent_v2": False,
            "features.defer_mailbox_preemption": True,
        },
        "dynamicTools": [CAPABILITIES["reportTool"]],
    }
    options["dynamicTools"][0]["name"] = "mutated"
    assert bridge.startup_options()["dynamicTools"][0]["name"] == "bgr_report_status"
    assert rpc.identity["sessionId"] == worker.attempt.session_id != THREAD
    assert rpc.identity["agentId"] == worker.attempt.agent_id
    assert rpc.identity["role"] == "grunt"
    queries = [persist_query(ledger, worker) for _ in range(2)]
    for query in queries:
        assert await bridge.deliver(worker.attempt.attempt_id, query.message_id) is False
        wire = rpc.pending[query.message_id]
        assert wire["messageId"] == query.message_id
        assert wire["correlationId"] == query.correlation_id
        assert wire["headSha"] == SHA
    assert await bridge.deliver(worker.attempt.attempt_id, queries[0].message_id) is True
    notifications = [rpc.reply(query.message_id, f"reply-{i}") for i, query in enumerate(queries)]
    rpc.replies = [notification["envelope"] for notification in reversed(notifications)]
    rpc.pending.clear()
    second = bridge.receive_notification(worker.attempt.attempt_id, notifications[1])
    restarted = await adapter(BgrStore(db_uri), rpc)
    receipts = await restarted.inspect(worker.attempt.attempt_id)
    assert receipts[0] == second
    assert (
        restarted.receive_notification(worker.attempt.attempt_id, notifications[0]) == receipts[1]
    )
    inbox = ledger.drain(OWNER, BRAIN, TEAM)
    assert [message.in_reply_to for message in inbox] == [
        query.message_id for query in reversed(queries)
    ]
    assert len({message.message_id for message in inbox}) == 2
    for message, notification in zip(inbox, reversed(notifications), strict=True):
        assert (
            len(message.message_id) == 32
            and message.message_id != notification["envelope"]["messageId"]
        )
        assert message.sender_session_id == worker.attempt.session_id
        assert message.head_sha == SHA and message.payload["headSha"] == "e" * 40
        assert message.payload["native_provenance"] == {
            "thread_id": THREAD,
            "message_id": notification["envelope"]["messageId"],
        }
        ledger.ack(OWNER, BRAIN, TEAM, Ack(envelope_id=message.envelope_id, epoch=1))
    worker_mailbox = ledger.drain(OWNER, worker.attempt.session_id, TEAM)
    assert [message.kind for message in worker_mailbox] == ["task", "status_query", "status_query"]
    assert all(message.consumed_at is None for message in worker_mailbox)
    assert all(message.replied_at is not None for message in worker_mailbox[1:])
    assert ledger.recover(OWNER, BRAIN, TEAM).tasks == [worker]
    assert {method for method, _ in rpc.calls} <= {
        "bgr/capabilities/read",
        "bgr/thread/bind",
        "bgr/thread/inspect",
        "bgr/message/deliver",
    }


async def test_captured_native_host_delivery_contract(ledger, worker, db_uri):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    expected = copy.deepcopy(NATIVE_CONFORMANCE["messageDeliver"]["params"])
    wire = expected["envelope"]
    query = persist_query(
        ledger,
        worker,
        message_id=wire["messageId"],
        correlation_id=wire["correlationId"],
        head_sha=wire["headSha"],
    )
    assignment = ledger.native_assignment(OWNER, BRAIN, TEAM, worker.attempt.attempt_id, FENCE)
    # Rebind only captured routing identities to this test's real reservation.
    expected["threadId"] = THREAD
    expected["sender"].update(teamId=TEAM, agentId=assignment.team.brain_agent_id, sessionId=BRAIN)
    wire.update(
        identity=rpc.identity,
        senderAgentId=assignment.team.brain_agent_id,
        recipientAgentId=worker.attempt.agent_id,
    )
    rpc.receipt_override = NATIVE_CONFORMANCE["messageDeliver"]["result"]
    assert await bridge.deliver(worker.attempt.attempt_id, query.message_id) is False
    assert rpc.calls[-1] == ("bgr/message/deliver", expected)

    captured = NATIVE_CONFORMANCE["messageNotification"]
    assert captured["method"] == "bgr/message"
    notification = copy.deepcopy(captured["params"])
    notification["threadId"] = THREAD
    notification["envelope"].update(
        identity=rpc.identity,
        senderAgentId=worker.attempt.agent_id,
        recipientAgentId=assignment.team.brain_agent_id,
    )
    receipt = bridge.receive_notification(worker.attempt.attempt_id, notification)
    restarted = await adapter(BgrStore(db_uri), rpc)
    assert restarted.receive_notification(worker.attempt.attempt_id, notification) == receipt
    reply = ledger.drain(OWNER, BRAIN, TEAM)[0]
    assert reply.head_sha == query.head_sha
    payload = reply.payload.copy()
    assert payload.pop("native_provenance") == {
        "thread_id": THREAD,
        "message_id": notification["envelope"]["messageId"],
    }
    assert payload == captured["params"]["envelope"]["payload"]
    assert payload["headSha"] != query.head_sha
    pending = ledger.drain(OWNER, worker.attempt.session_id, TEAM)[1]
    assert pending.consumed_at is None and pending.replied_at is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("hostDelivery", False),
        ("hostDelivery", None),
        ("version", 2),
        ("version", True),
        ("statusScopes", ["since_time"]),
        ("roles", ["grunt"]),
        ("messageKinds", ["status_query"]),
        ("reportTool", {}),
        ("liveReattach", False),
        ("maxQueriesPerAttempt", 0),
        ("maxQueriesPerAttempt", 129),
        ("reviewerSandbox", True),
    ],
)
async def test_unsupported_capabilities_never_bind_or_deliver(ledger, field, value):
    rpc = WorkerRpc()
    rpc.capabilities[field] = value
    if value is None:
        del rpc.capabilities[field]
    with pytest.raises(OmnigentError) as error:
        await adapter(ledger, rpc)
    assert error.value.code == ErrorCode.RUNNER_CAPABILITY_MISMATCH
    assert [method for method, _ in rpc.calls] == ["bgr/capabilities/read"]


@pytest.mark.parametrize("failure", ["unknown_rpc", "malformed", "bad_schema"])
async def test_unavailable_or_malformed_capabilities_fail_explicitly(ledger, failure):
    async def request(method, params):
        assert (method, params) == ("bgr/capabilities/read", {})
        if failure == "unknown_rpc":
            raise CodexAppServerResponseError({"code": -32601, "message": "Method not found"})
        if failure == "malformed":
            return {"result": []}
        capabilities = copy.deepcopy(CAPABILITIES)
        capabilities["reportTool"]["inputSchema"]["required"] = [{}]
        return {"result": capabilities}

    with pytest.raises(OmnigentError) as error:
        await CodexNativeBgrAdapter.negotiate(
            request,
            ledger,
            user_id=OWNER,
            brain_session_id=BRAIN,
            team_id=TEAM,
            fence=FENCE,
        )
    assert error.value.code == ErrorCode.RUNNER_CAPABILITY_MISMATCH


@pytest.mark.parametrize(
    "path,value",
    [
        (("threadId",), str(uuid4())),
        (("envelope", "version"), True),
        (("envelope", "identity", "teamId"), "f" * 32),
        (("envelope", "identity", "agentId"), "f" * 32),
        (("envelope", "identity", "sessionId"), "f" * 32),
        (("envelope", "identity", "taskId"), "f" * 32),
        (("envelope", "identity", "attemptId"), "f" * 32),
        (("envelope", "identity", "epoch"), 2),
        (("envelope", "identity", "role"), "reviewer"),
        (("envelope", "senderAgentId"), "f" * 32),
        (("envelope", "recipientAgentId"), "f" * 32),
        (("envelope", "correlationId"), "f" * 32),
        (("envelope", "inReplyTo"), "f" * 32),
        (("envelope", "kind"), "result"),
        (("envelope", "headSha"), "e" * 40),
        (("envelope", "payload", "currentAction"), 42),
        (("envelope", "payload", "coverageLimits"), "not a list"),
        (("envelope", "payload", "extra"), "untrusted extension"),
        (("envelope", "messageId"), "not\nan\nid"),
    ],
)
async def test_foreign_or_malformed_native_reply_is_not_imported(ledger, worker, path, value):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    query = persist_query(ledger, worker)
    await bridge.deliver(worker.attempt.attempt_id, query.message_id)
    notification = rpc.reply(query.message_id)
    target = notification
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(OmnigentError):
        bridge.receive_notification(worker.attempt.attempt_id, notification)
    assert ledger.drain(OWNER, BRAIN, TEAM) == []
    assert ledger.drain(OWNER, worker.attempt.session_id, TEAM)[1].replied_at is None


async def test_changed_duplicate_reaches_durable_store_conflict_and_host_size_is_enforced(
    ledger, worker
):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    query = persist_query(ledger, worker)
    await bridge.deliver(worker.attempt.attempt_id, query.message_id)
    oversized = rpc.reply(query.message_id, currentAction="x" * 8000)
    assert len(json.dumps(oversized).encode()) < 32 * 1024
    with pytest.raises(OmnigentError, match="8000-byte"):
        bridge.receive_notification(worker.attempt.attempt_id, oversized)
    notification = rpc.reply(query.message_id)
    original = bridge.receive_notification(worker.attempt.attempt_id, notification)
    assert bridge.receive_notification(worker.attempt.attempt_id, notification) == original
    notification["envelope"]["payload"]["currentAction"] = "Changed"
    with pytest.raises(OmnigentError, match="message_id already has different content"):
        bridge.receive_notification(worker.attempt.attempt_id, notification)
    assert len(ledger.drain(OWNER, BRAIN, TEAM)) == 1


@pytest.mark.parametrize(
    "receipt",
    [
        {"messageId": "f" * 32, "accepted": True, "duplicate": False},
        {"messageId": None, "accepted": True, "duplicate": False},
        {"accepted": False, "duplicate": False},
        {"accepted": 1, "duplicate": False},
        {"accepted": True, "duplicate": "false"},
    ],
)
async def test_bad_native_acceptance_never_acknowledges_host(ledger, worker, receipt):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    query = persist_query(ledger, worker)
    rpc.receipt_override = {"messageId": query.message_id, **receipt}
    with pytest.raises(OmnigentError):
        await bridge.deliver(worker.attempt.attempt_id, query.message_id)
    assert ledger.drain(OWNER, worker.attempt.session_id, TEAM)[1].consumed_at is None


async def test_mapping_reservation_is_immutable_authorized_and_restart_safe(
    ledger, worker, db_uri
):
    from sqlalchemy import select, text

    from omnigent.db.bgr_models import SqlBgrNativeBinding
    from omnigent.db.utils import get_or_create_engine

    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    existing = ledger.native_assignment(OWNER, BRAIN, TEAM, worker.attempt.attempt_id, FENCE)
    assert (
        BgrStore(db_uri).native_assignment(OWNER, BRAIN, TEAM, worker.attempt.attempt_id, FENCE)
        == existing
    )
    assert await bridge.bind(worker.attempt.attempt_id, THREAD) == existing.binding
    with get_or_create_engine(db_uri).connect() as connection:
        assert connection.scalar(select(SqlBgrNativeBinding.thread_id)) == THREAD.replace("-", "")
        assert connection.scalar(text("SELECT length(thread_id) FROM bgr_native_bindings")) == 16
    assert existing.binding.thread_id == THREAD
    calls = len(rpc.calls)
    with pytest.raises(OmnigentError, match="immutable"):
        await bridge.bind(worker.attempt.attempt_id, str(uuid4()))
    another = ledger.reserve(OWNER, BRAIN, TEAM, reservation())
    with pytest.raises(OmnigentError, match="already reserved"):
        await bridge.bind(another.attempt.attempt_id, THREAD)
    assert len(rpc.calls) == calls
    for user, session, team, fence in [
        ("mallory", BRAIN, TEAM, FENCE),
        (OWNER, worker.attempt.session_id, TEAM, FENCE),
        (OWNER, BRAIN, "f" * 32, FENCE),
        (OWNER, BRAIN, TEAM, Fence(controller_id="f" * 32, epoch=1)),
    ]:
        with pytest.raises(OmnigentError):
            ledger.native_assignment(user, session, team, worker.attempt.attempt_id, fence)
    with workspace_scope(99), pytest.raises(OmnigentError):
        ledger.native_assignment(OWNER, BRAIN, TEAM, worker.attempt.attempt_id, FENCE)
    with pytest.raises(OmnigentError):
        await bridge.deliver(another.attempt.attempt_id, "f" * 32)


async def test_mapping_never_auto_routes_other_profiles_or_cross_attempt_queries(
    ledger, worker, tmp_path
):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    incompatible = ledger.reserve(
        OWNER,
        BRAIN,
        TEAM,
        reservation(
            profile=CONFIG.grunt.model_copy(update={"harness": "claude-code"}),
        ),
    )
    with pytest.raises(OmnigentError, match="explicit codex-native"):
        await bridge.bind(incompatible.attempt.attempt_id, str(uuid4()))
    ledger.close(
        OWNER,
        BRAIN,
        TEAM,
        TaskClose(
            **FENCE.model_dump(),
            attempt_id=incompatible.attempt.attempt_id,
            outcome="cancelled",
            stop_confirmed=True,
        ),
    )
    another = ledger.reserve(OWNER, BRAIN, TEAM, reservation())
    ledger.bind_native(
        OWNER,
        BRAIN,
        TEAM,
        FENCE,
        NativeBinding(
            team_id=TEAM,
            attempt_id=another.attempt.attempt_id,
            epoch=1,
            thread_id=str(uuid4()),
        ),
    )
    query = persist_query(ledger, worker)
    with pytest.raises(OmnigentError, match="live authenticated brain"):
        ledger.native_query(
            OWNER, BRAIN, TEAM, another.attempt.attempt_id, FENCE, query.message_id
        )
    separate = BgrStore(f"sqlite:///{tmp_path / 'separate.db'}")
    with pytest.raises(OmnigentError):
        separate.native_assignment(OWNER, BRAIN, TEAM, worker.attempt.attempt_id, FENCE)


async def test_bind_failure_keeps_exact_mapping_for_explicit_retry(ledger, worker):
    rpc = WorkerRpc()
    fail_bind = True

    async def request(method, params):
        nonlocal fail_bind
        if method == "bgr/thread/bind" and fail_bind:
            raise ConnectionError("ambiguous socket loss")
        return await rpc.request(method, params)

    bridge = await CodexNativeBgrAdapter.negotiate(
        request,
        ledger,
        user_id=OWNER,
        brain_session_id=BRAIN,
        team_id=TEAM,
        fence=FENCE,
    )
    with pytest.raises(ConnectionError, match="socket loss"):
        await bridge.bind(worker.attempt.attempt_id, THREAD)
    binding = ledger.native_assignment(
        OWNER, BRAIN, TEAM, worker.attempt.attempt_id, FENCE
    ).binding
    assert binding.thread_id == THREAD
    fail_bind = False
    assert await bridge.bind(worker.attempt.attempt_id, THREAD) == binding


async def test_native_reply_id_reuse_for_a_different_query_hits_store_conflict(ledger, worker):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    queries = [persist_query(ledger, worker) for _ in range(2)]
    for query in queries:
        await bridge.deliver(worker.attempt.attempt_id, query.message_id)
    first = rpc.reply(queries[0].message_id, "a" * 32)
    second = rpc.reply(queries[1].message_id, "a" * 32)
    bridge.receive_notification(worker.attempt.attempt_id, first)
    with pytest.raises(OmnigentError, match="message_id already has different content"):
        bridge.receive_notification(worker.attempt.attempt_id, second)
    assert len(ledger.drain(OWNER, BRAIN, TEAM)) == 1


async def test_terminal_and_epoch_fences_block_late_delivery_reply_and_rebinding(ledger, worker):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    query = persist_query(ledger, worker)
    await bridge.deliver(worker.attempt.attempt_id, query.message_id)
    reply = rpc.reply(query.message_id)
    ledger.close(
        OWNER,
        BRAIN,
        TEAM,
        TaskClose(
            **FENCE.model_dump(),
            attempt_id=worker.attempt.attempt_id,
            outcome="cancelled",
            stop_confirmed=True,
        ),
    )
    calls = len(rpc.calls)
    with pytest.raises(OmnigentError, match="terminal"):
        bridge.receive_notification(worker.attempt.attempt_id, reply)
    with pytest.raises(OmnigentError):
        await bridge.deliver(worker.attempt.attempt_id, query.message_id)
    with pytest.raises(OmnigentError, match="terminal"):
        await bridge.inspect(worker.attempt.attempt_id)
    with pytest.raises(OmnigentError):
        await bridge.bind(worker.attempt.attempt_id, THREAD)
    assert len(rpc.calls) == calls


async def test_epoch_rollover_does_not_promote_native_binding_or_old_replies(ledger, worker):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    query = persist_query(ledger, worker)
    await bridge.deliver(worker.attempt.attempt_id, query.message_id)
    reply = rpc.reply(query.message_id)
    ledger._clock = lambda: 10**12
    ledger.acquire(OWNER, BRAIN, TEAM, LeaseRequest(controller_id=CONTROLLER))
    with pytest.raises(OmnigentError, match="epoch is stale"):
        bridge.receive_notification(worker.attempt.attempt_id, reply)
    with pytest.raises(OmnigentError, match="epoch is stale"):
        await bridge.inspect(worker.attempt.attempt_id)
    fence = Fence(controller_id=CONTROLLER, epoch=2)
    ledger.reattach(OWNER, BRAIN, TEAM, worker.attempt.attempt_id, fence)
    with pytest.raises(OmnigentError, match="older epoch"):
        ledger.native_assignment(OWNER, BRAIN, TEAM, worker.attempt.attempt_id, fence)
    with pytest.raises(OmnigentError, match="immutable"):
        ledger.bind_native(
            OWNER,
            BRAIN,
            TEAM,
            fence,
            NativeBinding(
                team_id=TEAM,
                attempt_id=worker.attempt.attempt_id,
                epoch=2,
                thread_id=THREAD,
            ),
        )


async def test_scoped_queries_and_unpersisted_messages_are_never_forwarded(ledger, worker):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    scoped = question(worker)
    ledger.send(OWNER, BRAIN, TEAM, scoped)
    calls = len(rpc.calls)
    with pytest.raises(OmnigentError, match="scope=now"):
        await bridge.deliver(worker.attempt.attempt_id, scoped.message_id)
    with pytest.raises(OmnigentError):
        await bridge.deliver(worker.attempt.attempt_id, uuid4().hex)
    task_envelope = ledger.drain(OWNER, worker.attempt.session_id, TEAM)[0]
    with pytest.raises(OmnigentError, match="status query"):
        await bridge.deliver(worker.attempt.attempt_id, task_envelope.message_id)
    assert len(rpc.calls) == calls


async def test_inspect_recovers_other_replies_while_consumed_query_is_unanswered(
    ledger, worker, db_uri
):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    queries = [persist_query(ledger, worker) for _ in range(2)]
    for query in queries:
        await bridge.deliver(worker.attempt.attempt_id, query.message_id)
    first = ledger.native_query(
        OWNER, BRAIN, TEAM, worker.attempt.attempt_id, FENCE, queries[0].message_id
    )
    consumed = ledger.ack(
        OWNER, worker.attempt.session_id, TEAM, Ack(envelope_id=first.envelope_id, epoch=1)
    )
    assert consumed.consumed_at is not None and consumed.replied_at is None
    rpc.replies = [rpc.reply(queries[1].message_id)["envelope"]]
    del rpc.pending[queries[1].message_id]

    restarted = await adapter(BgrStore(db_uri), rpc)
    calls = len(rpc.calls)
    with pytest.raises(OmnigentError, match="unconsumed"):
        await restarted.deliver(worker.attempt.attempt_id, first.message_id)
    assert len(rpc.calls) == calls
    receipts = await restarted.inspect(worker.attempt.attempt_id)
    assert len(receipts) == 1
    reply = ledger.drain(OWNER, BRAIN, TEAM)[0]
    assert reply.envelope_id == receipts[0].envelope_id
    assert reply.in_reply_to == queries[1].message_id
    assert reply.correlation_id == queries[1].correlation_id
    assert (
        ledger.native_query(OWNER, BRAIN, TEAM, worker.attempt.attempt_id, FENCE, first.message_id)
        == consumed
    )
    second = ledger.native_query(
        OWNER, BRAIN, TEAM, worker.attempt.attempt_id, FENCE, queries[1].message_id
    )
    assert second.consumed_at is None and second.replied_at is not None

    replayed = await adapter(BgrStore(db_uri), rpc)
    assert await replayed.inspect(worker.attempt.attempt_id) == receipts
    assert ledger.drain(OWNER, BRAIN, TEAM) == [reply]
    assert ledger.recover(OWNER, BRAIN, TEAM).tasks == [worker]


@pytest.mark.parametrize("consumed", [False, True])
@pytest.mark.parametrize(
    "path,value",
    [
        (("payload",), {"scope": "since_time"}),
        (("identity", "attemptId"), "f" * 32),
        (("identity", "epoch"), 2),
        (("recipientAgentId",), "f" * 32),
    ],
)
async def test_inspect_validates_pending_identity_and_cancelled_snapshot(
    ledger, worker, consumed, path, value
):
    rpc = WorkerRpc()
    bridge = await bound(ledger, worker, rpc)
    query = persist_query(ledger, worker)
    await bridge.deliver(worker.attempt.attempt_id, query.message_id)
    if consumed:
        original = ledger.native_query(
            OWNER, BRAIN, TEAM, worker.attempt.attempt_id, FENCE, query.message_id
        )
        ledger.ack(
            OWNER,
            worker.attempt.session_id,
            TEAM,
            Ack(envelope_id=original.envelope_id, epoch=1),
        )
    assert await bridge.inspect(worker.attempt.attempt_id) == []
    target = rpc.pending[query.message_id]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(OmnigentError, match="changed or foreign"):
        await bridge.inspect(worker.attempt.attempt_id)
    rpc.cancelled = True
    with pytest.raises(OmnigentError, match="cancelled"):
        await bridge.inspect(worker.attempt.attempt_id)
    assert ledger.recover(OWNER, BRAIN, TEAM).tasks == [worker]


async def test_existing_rpc_client_routes_out_of_order_host_delivery_responses(ledger, worker):
    """Use the existing client's reader/IDs, not a parallel BGR protocol loop."""
    rpc = WorkerRpc()
    queued = []

    async def handle(websocket):
        async for raw in websocket:
            message = json.loads(raw)
            if message["method"] == "initialized":
                continue
            if message["method"] == "initialize":
                await websocket.send(json.dumps({"id": message["id"], "result": {}}))
                continue
            response = await rpc.request(message["method"], message["params"])
            response["id"] = message["id"]
            if message["method"] == "bgr/message/deliver":
                queued.append(response)
                if len(queued) < 2:
                    continue
                for pending in reversed(queued):
                    await websocket.send(json.dumps(pending))
            else:
                await websocket.send(json.dumps(response))

    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        await client.connect()
        try:
            bridge = await CodexNativeBgrAdapter.negotiate(
                client.request,
                ledger,
                user_id=OWNER,
                brain_session_id=BRAIN,
                team_id=TEAM,
                fence=FENCE,
            )
            await bridge.bind(worker.attempt.attempt_id, THREAD)
            queries = [persist_query(ledger, worker) for _ in range(2)]
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        bridge.deliver(worker.attempt.attempt_id, query.message_id)
                        for query in queries
                    )
                ),
                timeout=3,
            )
            assert results == [False, False]
            assert [response["result"]["messageId"] for response in queued] == [
                q.message_id for q in queries
            ]
            assert client._pending_requests == {}
        finally:
            await client.close()
