"""General routing against independent RPC connections; these are not model-execution tests."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnigent.bgr import (
    Ack,
    Fence,
    LeaseRequest,
    NativeBinding,
    NativeBrainBinding,
    TaskClose,
    TeamCreate,
)
from omnigent.bgr_review import normalize_native_review_result
from omnigent.errors import OmnigentError
from omnigent.harnesses.codex_native.bgr import NativeCapabilities
from omnigent.harnesses.codex_native.bgr_messages import NativeBgrConnection, NativeEnvelope
from omnigent.runner import bgr_publisher
from omnigent.runner.bgr_mailbox import BgrNativeMailbox
from omnigent.stores.bgr_review_store import BgrReviewStore
from tests.stores.test_bgr_review_store import activate
from tests.stores.test_bgr_review_store import request as review_request
from tests.stores.test_bgr_store import BRAIN, CONFIG, CONTROLLER, HOST, OWNER, TEAM, reservation

CAPTURED = json.loads(Path(__file__).with_name("bgr-general-capabilities.json").read_text())
FENCE = Fence(controller_id=CONTROLLER, epoch=1)
PRIVATE = "opaque-host-private"


@dataclass
class NativeProcess:
    connection: NativeBgrConnection | None = None
    calls: list = field(default_factory=list)
    outgoing: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    fail_after_accept: bool = False
    receipt_override: dict | None = None

    async def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        assert params["threadId"] == self.connection.thread_id
        if method in {"bgr/thread/inspect", "bgr/thread/peers"}:
            return {
                "result": {
                    "snapshot": {
                        "identity": self.connection.identity.wire(),
                        "cancelled": False,
                        "sequence": len(self.outgoing) + len(self.pending),
                        "pending": list(self.pending.values()),
                        "replies": list(self.outgoing.values()),
                    }
                }
            }
        envelope = NativeEnvelope.model_validate(params["envelope"])
        if method == "bgr/message/record":
            assert params["identity"] == self.connection.identity.wire()
            assert envelope.identity == self.connection.identity
            bucket = self.outgoing
        else:
            assert method == "bgr/message/deliver"
            assert params["sender"] == envelope.identity.sender()
            assert envelope.recipient_identity == self.connection.identity
            bucket = self.pending
        duplicate = envelope.message_id in bucket
        if duplicate:
            assert bucket[envelope.message_id] == envelope.wire()
        bucket[envelope.message_id] = envelope.wire()
        if self.fail_after_accept:
            self.fail_after_accept = False
            raise TimeoutError("Transport disconnected after accepting")
        return {
            "result": self.receipt_override
            or {
                "messageId": envelope.message_id,
                "accepted": True,
                "duplicate": duplicate,
            }
        }

    def emit(self, peer, anchor, message_id, kind, *, payload=None, reply_to=None):
        envelope = {
            "version": 1,
            "messageId": message_id,
            "kind": kind,
            "identity": self.connection.identity.wire(),
            "recipientIdentity": peer.identity.wire(),
            "senderAgentId": self.connection.identity.agent_id,
            "recipientAgentId": peer.identity.agent_id,
            "correlationId": anchor.correlation_id,
            "inReplyTo": reply_to or anchor.message_id,
            "headSha": anchor.head_sha,
            "payload": payload or {"summary": kind},
        }
        self.outgoing[message_id] = envelope
        return {"threadId": self.connection.thread_id, "envelope": copy.deepcopy(envelope)}


@pytest.fixture
def route(db_uri):
    store = BgrReviewStore(db_uri)
    config = CONFIG.model_copy(
        update={
            "brain": CONFIG.brain.model_copy(update={"harness": "codex-native"}),
            "grunt": CONFIG.grunt.model_copy(update={"harness": "codex-native"}),
            "reviewers": [CONFIG.reviewers[0].model_copy(update={"harness": "codex-native"})] * 2,
        }
    )
    store.create(OWNER, BRAIN, TeamCreate(team_id=TEAM, config=config))
    store.acquire(OWNER, BRAIN, TEAM, LeaseRequest(controller_id=CONTROLLER, ttl_seconds=300))
    task = store.reserve(OWNER, BRAIN, TEAM, reservation())
    store.native_brain_binding(
        OWNER,
        BRAIN,
        TEAM,
        FENCE,
        binding=NativeBrainBinding(
            team_id=TEAM, epoch=1, thread_id=str(uuid4()), runtime_id=uuid4().hex, host_id=HOST
        ),
    )
    store.bind_native(
        OWNER,
        BRAIN,
        TEAM,
        FENCE,
        NativeBinding(
            team_id=TEAM,
            attempt_id=task.attempt.attempt_id,
            epoch=1,
            thread_id=str(uuid4()),
        ),
    )
    mailbox = BgrNativeMailbox(
        store, OWNER, BRAIN, TEAM, FENCE, root="/trusted", private_values=lambda: (PRIVATE,)
    )
    processes = []
    for attempt_id in (None, task.attempt.attempt_id):
        identity, thread_id = mailbox.identity(attempt_id)
        process = NativeProcess()
        process.connection = NativeBgrConnection(
            thread_id, identity, process.request, NativeCapabilities.model_validate(CAPTURED)
        )
        processes.append(process)
    return mailbox, task, *processes


def test_captured_reviewer_record_capability_is_accepted_without_claiming_durability():
    capability = NativeCapabilities.model_validate(CAPTURED)
    assert capability.host_record and capability.reviewer_start and capability.reviewer_sandbox
    assert capability.roles == ["brain", "grunt", "reviewer"]
    assert not capability.consumption_receipts
    assert not capability.durable_mailbox and not capability.restart_recovery
    assert capability.review_snapshot_tool["name"] == "bgr_read_review_snapshot"


async def test_durable_record_then_delivery_retry_and_arbitrary_native_question_answer(
    route, db_uri
):
    mailbox, task, brain, worker = route
    source, target = brain.connection, worker.connection
    anchor = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[0]
    assert (
        anchor.correlation_id == task.task_id
        and anchor.head_sha == task.attempt.workspace.base_sha
    )
    worker.fail_after_accept = True
    with pytest.raises(TimeoutError):
        await mailbox.forward(source, target, anchor.message_id)
    assert brain.outgoing[anchor.message_id] == worker.pending[anchor.message_id]
    assert (await mailbox.forward(source, target, anchor.message_id)).duplicate
    assert [method for method, _ in brain.calls] == ["bgr/message/record"] * 2
    assert mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[0].consumed_at is None
    params = worker.emit(
        source,
        anchor,
        "clarify-contract-1",
        "question",
        payload={
            "summary": f"Clarify {PRIVATE}",
            "nested": [{"summary": PRIVATE}],
        },
    )
    receipt = mailbox.receive(target, params)
    assert mailbox.receive(target, params) == receipt
    question = mailbox.store.drain(OWNER, BRAIN, TEAM)[0]
    assert question.native_origin.message_id == "clarify-contract-1"
    assert PRIVATE not in question.model_dump_json()
    assert question.in_reply_to == anchor.message_id
    restarted = BgrNativeMailbox(
        BgrReviewStore(db_uri),
        OWNER,
        BRAIN,
        TEAM,
        FENCE,
        root="/trusted",
        private_values=lambda: (PRIVATE,),
    )
    await restarted.forward(target, source, question.message_id)
    assert brain.pending["clarify-contract-1"]["payload"] == question.payload
    answer_params = brain.emit(
        target, anchor, "answer-contract-1", "answer", reply_to="clarify-contract-1"
    )
    answer_receipt = restarted.receive(source, answer_params)
    await restarted.forward(source, target, answer_receipt.message_id)
    assert worker.pending["answer-contract-1"]["inReplyTo"] == "clarify-contract-1"
    assert worker.pending["answer-contract-1"]["correlationId"] == anchor.correlation_id
    for index, kind in enumerate(("progress", "progress", "result")):
        callback = worker.emit(source, anchor, f"worker-{kind}-{index}", kind)
        result = restarted.receive(target, callback)
        await restarted.forward(target, source, result.message_id)
    assert [item.kind for item in restarted.store.drain(OWNER, BRAIN, TEAM)] == [
        "question",
        "progress",
        "progress",
        "result",
    ]
    assert all(item.consumed_at is None for item in restarted.store.drain(OWNER, BRAIN, TEAM))
    assert PRIVATE not in json.dumps(brain.calls)
    assert all(method != "bgr/message/record" for method, _ in worker.calls)
    assert len(await restarted.reconcile(target)) == 4


@pytest.mark.parametrize("terminal", [False, True])
async def test_reconcile_skips_recorded_host_tasks_even_for_retired_recipients(route, terminal):
    mailbox, task, brain, worker = route
    anchor = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[0]
    await mailbox.forward(brain.connection, worker.connection, anchor.message_id)
    if terminal:
        mailbox.store.close(
            OWNER,
            BRAIN,
            TEAM,
            TaskClose(
                **FENCE.model_dump(),
                attempt_id=task.attempt.attempt_id,
                outcome="completed",
                stop_confirmed=True,
            ),
        )
    before = copy.deepcopy(worker.pending)
    calls = len(brain.calls)
    assert await mailbox.reconcile(brain.connection) == []
    assert await mailbox.reconcile(brain.connection) == []
    assert [method for method, _ in brain.calls[calls:]] == ["bgr/thread/inspect"] * 2
    assert worker.pending == before
    envelopes = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)
    assert [envelope.message_id for envelope in envelopes] == [anchor.message_id]
    assert envelopes[0].consumed_at is None
    assert mailbox.store.drain(OWNER, BRAIN, TEAM) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload", {"summary": "Changed host task"}),
        ("kind", "progress"),
        ("correlationId", "changed-anchor"),
        ("headSha", "f" * 40),
        ("inReplyTo", "invented-parent"),
        ("recipientIdentity.sessionId", "f" * 32),
        ("recipientIdentity.agentId", "f" * 32),
        ("recipientIdentity.taskId", "f" * 32),
        ("recipientIdentity.attemptId", "f" * 32),
        ("recipientIdentity.role", "reviewer"),
    ],
)
async def test_reconcile_rejects_changed_recorded_host_anchor_without_import(route, field, value):
    mailbox, task, brain, worker = route
    anchor = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[0]
    await mailbox.forward(brain.connection, worker.connection, anchor.message_id)
    wire = brain.outgoing[anchor.message_id]
    if field.startswith("recipientIdentity."):
        wire["recipientIdentity"][field.split(".")[1]] = value
        if field.endswith(".agentId"):
            wire["recipientAgentId"] = value
    else:
        wire[field] = value
    with pytest.raises((OmnigentError, ValueError)):
        await mailbox.reconcile(brain.connection)
    assert mailbox.store.drain(OWNER, BRAIN, TEAM) == []
    envelopes = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)
    assert [envelope.message_id for envelope in envelopes] == [anchor.message_id]
    assert len(worker.pending) == 1


@pytest.mark.parametrize("message_id", ["missed-native-result", "f" * 32])
async def test_reconcile_imports_retained_native_reply_exactly_once(route, db_uri, message_id):
    mailbox, task, brain, worker = route
    anchor = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[0]
    await mailbox.forward(brain.connection, worker.connection, anchor.message_id)
    worker.emit(
        brain.connection,
        anchor,
        message_id,
        "result",
        payload={"summary": f"Completed work; redact {PRIVATE}"},
    )
    restored = BgrNativeMailbox(
        BgrReviewStore(db_uri),
        OWNER,
        BRAIN,
        TEAM,
        FENCE,
        root="/trusted",
        private_values=lambda: (PRIVATE,),
    )
    assert restored.store.drain(OWNER, BRAIN, TEAM) == []
    calls = len(worker.calls)
    receipts = await restored.reconcile(worker.connection)
    assert len(receipts) == 1
    assert await restored.reconcile(worker.connection) == receipts
    assert [method for method, _ in worker.calls[calls:]] == ["bgr/thread/inspect"] * 2
    envelopes = restored.store.drain(OWNER, BRAIN, TEAM)
    assert len(envelopes) == 1 and envelopes[0].native_origin.message_id == message_id
    assert envelopes[0].consumed_at is None
    assert PRIVATE not in envelopes[0].model_dump_json()
    assert brain.pending == {}
    worker.outgoing[message_id]["payload"] = {"summary": "Changed retained result"}
    with pytest.raises(OmnigentError):
        await restored.reconcile(worker.connection)
    assert restored.store.drain(OWNER, BRAIN, TEAM) == envelopes
    restored.store.ack(
        OWNER, BRAIN, TEAM, Ack(epoch=FENCE.epoch, envelope_id=receipts[0].envelope_id)
    )
    worker.outgoing[message_id]["payload"] = {"summary": f"Completed work; redact {PRIVATE}"}
    assert await restored.reconcile(worker.connection) == []


async def test_reconcile_preserves_native_receipt_for_a_retired_recipient(route):
    mailbox, task, brain, worker = route
    anchor = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[0]
    await mailbox.forward(brain.connection, worker.connection, anchor.message_id)
    question = worker.emit(brain.connection, anchor, "clarify-before-close", "question")
    question_receipt = mailbox.receive(worker.connection, question)
    await mailbox.forward(worker.connection, brain.connection, question_receipt.message_id)
    answer = brain.emit(
        worker.connection, anchor, "answer-before-close", "answer", reply_to="clarify-before-close"
    )
    answer_receipt = mailbox.receive(brain.connection, answer)
    mailbox.store.close(
        OWNER,
        BRAIN,
        TEAM,
        TaskClose(
            **FENCE.model_dump(),
            attempt_id=task.attempt.attempt_id,
            outcome="completed",
            stop_confirmed=True,
        ),
    )
    assert await mailbox.reconcile(brain.connection) == [answer_receipt]
    assert await mailbox.reconcile(brain.connection) == [answer_receipt]
    assert worker.pending.keys() == {anchor.message_id}
    assert mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[-1].consumed_at is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("correlation_id", "f" * 32),
        ("controller_id", "f" * 32),
        ("native_origin.in_reply_to", "different-native-parent"),
    ],
)
async def test_reconcile_validates_canonical_fields_of_recorded_native_evidence(
    route, monkeypatch, field, value
):
    mailbox, task, brain, worker = route
    anchor = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[0]
    emitted = worker.emit(brain.connection, anchor, "persisted-native-result", "result")
    receipt = mailbox.receive(worker.connection, emitted)
    original = mailbox._message

    def damaged(sender_agent_id, message_id):
        envelope = original(sender_agent_id, message_id)
        if message_id == receipt.message_id:
            if field.startswith("native_origin."):
                return envelope.model_copy(
                    update={
                        "native_origin": envelope.native_origin.model_copy(
                            update={field.split(".")[1]: value}
                        )
                    }
                )
            return envelope.model_copy(update={field: value})
        return envelope

    monkeypatch.setattr(mailbox, "_message", damaged)
    with pytest.raises(OmnigentError):
        await mailbox.reconcile(worker.connection)
    envelopes = mailbox.store.drain(OWNER, BRAIN, TEAM)
    assert len(envelopes) == 1 and envelopes[0].message_id == receipt.message_id
    assert envelopes[0].consumed_at is None
    assert brain.pending == {}


@pytest.mark.parametrize(
    "mutation",
    [
        {"correlationId": "other"},
        {"headSha": "e" * 40},
        {"inReplyTo": "missing-task"},
        {"recipientAgentId": "f" * 32},
    ],
)
async def test_changed_callback_is_rejected_before_any_durable_write(route, mutation):
    mailbox, task, brain, worker = route
    anchor = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[0]
    callback = worker.emit(brain.connection, anchor, "clarify-contract-1", "question")
    callback["envelope"].update(mutation)
    with pytest.raises(OmnigentError):
        mailbox.receive(worker.connection, callback)
    assert mailbox.store.drain(OWNER, BRAIN, TEAM) == []


@pytest.mark.parametrize(
    "receipt",
    [
        {"messageId": "wrong", "accepted": True, "duplicate": False},
        {"messageId": "wrong", "accepted": False, "duplicate": False},
        {"messageId": "wrong", "accepted": True},
    ],
)
async def test_wrong_source_receipt_prevents_destination_delivery(route, receipt):
    mailbox, task, brain, worker = route
    anchor = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[0]
    brain.receipt_override = receipt
    with pytest.raises(OmnigentError):
        await mailbox.forward(brain.connection, worker.connection, anchor.message_id)
    assert worker.calls == []


async def test_source_binding_fence_and_outbox_payload_change_stop_forwarding(route):
    mailbox, task, brain, worker = route
    anchor = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[0]
    with pytest.raises(OmnigentError):
        await mailbox.forward(
            replace(brain.connection, thread_id=str(uuid4())), worker.connection, anchor.message_id
        )
    assert worker.calls == brain.calls == []
    params = worker.emit(brain.connection, anchor, "clarify-contract-1", "question")
    receipt = mailbox.receive(worker.connection, params)
    worker.outgoing["clarify-contract-1"]["payload"] = {"summary": "changed"}
    with pytest.raises(OmnigentError):
        await mailbox.forward(worker.connection, brain.connection, receipt.message_id)
    assert brain.calls == []
    with pytest.raises(ValidationError):
        NativeEnvelope.model_validate({**params["envelope"], "unexpected": PRIVATE})


@pytest.mark.parametrize("outcome", ["no_findings", "incomplete"])
def test_native_review_outcome_normalization_is_factual_and_bounded(outcome):
    result = normalize_native_review_result(
        {
            "outcome": outcome,
            "findings": [],
            "coverageLimits": ["Only changed sources read"],
        }
    )
    assert result.status == ("complete" if outcome == "no_findings" else "incomplete")
    assert result.findings == [] and "Only changed sources read" in result.summary
    assert normalize_native_review_result(result.model_dump(mode="json")) == result


async def test_native_acceptance_leaves_durable_task_replayable_after_native_metadata_loss(route):
    mailbox, task, brain, worker = route
    accepted = await mailbox.forward(brain.connection, worker.connection, task.task_id)
    assert accepted and accepted.accepted
    worker.pending.clear()
    pending = mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)
    assert len(pending) == 1 and pending[0].consumed_at is None
    replayed = await mailbox.forward(brain.connection, worker.connection, task.task_id)
    assert replayed and not replayed.duplicate
    assert task.task_id in worker.pending
    assert mailbox.store.drain(OWNER, task.attempt.session_id, TEAM)[0].consumed_at is None


@pytest.mark.parametrize(
    "payload",
    [
        {"outcome": "complete", "findings": [], "coverageLimits": []},
        {"outcome": "no_findings", "findings": [], "coverageLimits": [], "unknown": PRIVATE},
        {"outcome": "incomplete", "findings": [], "coverageLimits": ["a" * 500] * 16},
    ],
)
def test_incompatible_or_oversized_native_review_result_is_not_silently_dropped(payload):
    with pytest.raises(ValueError):
        normalize_native_review_result(payload)


async def test_reviewer_callbacks_sanitized_before_sealing_and_sibling_barrier(route, monkeypatch):
    mailbox, _, brain, _ = route
    privacy_calls = []

    def inventory(*args, **kwargs):
        privacy_calls.append((args, kwargs))
        return (PRIVATE,)

    monkeypatch.setattr(bgr_publisher, "_private_values", inventory)
    mailbox = BgrNativeMailbox(mailbox.store, OWNER, BRAIN, TEAM, FENCE, root="/trusted")
    round = mailbox.store.reserve_review_round(OWNER, BRAIN, TEAM, review_request())
    reviewers = []
    anchors = []
    for job in round.jobs:
        activate(mailbox.store, round, job)
        identity, thread_id = mailbox.identity(job.attempt_id)
        process = NativeProcess()
        process.connection = NativeBgrConnection(
            thread_id, identity, process.request, brain.connection.capabilities
        )
        reviewers.append(process)
        anchors.append(
            mailbox.store.native_message(
                OWNER,
                BRAIN,
                TEAM,
                FENCE,
                sender_agent_id=brain.connection.identity.agent_id,
                message_id=job.task_id,
            )
        )
    first, second = reviewers
    question = first.emit(
        brain.connection,
        anchors[0],
        "reviewer-clarify-1",
        "question",
        payload={"summary": f"Question containing {PRIVATE}"},
    )
    receipt = mailbox.receive(first.connection, question)
    await mailbox.forward(first.connection, brain.connection, receipt.message_id)
    assert PRIVATE not in json.dumps(brain.pending)
    early = first.emit(
        brain.connection,
        anchors[0],
        "reviewer-A-result",
        "result",
        payload={"outcome": "no_findings", "findings": [], "coverageLimits": [PRIVATE]},
    )
    early_receipt = mailbox.receive(first.connection, early)
    assert (
        PRIVATE
        not in mailbox.store.get_review_round(OWNER, BRAIN, TEAM, round.round_id).model_dump_json()
    )
    assert early_receipt in await mailbox.reconcile(first.connection)
    calls = len(brain.calls)
    assert (
        await mailbox.forward(first.connection, brain.connection, early_receipt.message_id) is None
    )
    assert len(brain.calls) == calls and second.calls == []
    late = second.emit(
        brain.connection,
        anchors[1],
        "reviewer-B-result",
        "result",
        payload={
            "outcome": "incomplete",
            "findings": [],
            "coverageLimits": ["Could not verify runtime"],
        },
    )
    late_receipts = await mailbox.reconcile(second.connection)
    assert len(late_receipts) == 1
    late_receipt = late_receipts[0]
    assert mailbox.receive(second.connection, late) == late_receipt
    assert await mailbox.forward(first.connection, brain.connection, early_receipt.message_id)
    assert await mailbox.forward(second.connection, brain.connection, late_receipt.message_id)
    assert brain.pending["reviewer-A-result"]["payload"]["result"]["status"] == "complete"
    assert brain.pending["reviewer-B-result"]["payload"]["result"]["status"] == "incomplete"
    assert PRIVATE not in json.dumps(brain.calls)
    assert all(
        args == ("/trusted",) and kwargs == {"require_forge": False}
        for args, kwargs in privacy_calls
    )
