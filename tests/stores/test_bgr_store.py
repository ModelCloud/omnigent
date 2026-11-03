"""BGR transactional ledger on the existing migrated database fixture."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnigent.bgr import (
    Ack,
    Checkpoint,
    ConfigUpdate,
    Contract,
    Fence,
    LeaseRequest,
    MailboxDiscard,
    Profile,
    Send,
    StatusScope,
    TaskClose,
    TaskReserve,
    TeamConfig,
    TeamCreate,
    Workspace,
)
from omnigent.db import workspace_scope
from omnigent.errors import OmnigentError
from omnigent.stores import bgr_store
from omnigent.stores.bgr_store import BgrStore

OWNER, BRAIN, TEAM, CONTROLLER = "alice", "a" * 32, "b" * 32, "c" * 32
HOST, SHA = "host-1", "d" * 40
PROFILE = Profile(provider="provider", model="model", reasoning="high", harness="native")
CONFIG = TeamConfig(brain=PROFILE, grunt=PROFILE, reviewers=[PROFILE], max_active_grunts=2)
CONTRACT = Contract(
    objective="Implement change", instructions="Use isolated worktree", acceptance=["Tests pass"]
)


@pytest.fixture
def store(db_uri):
    store = BgrStore(db_uri)
    store.create(OWNER, BRAIN, TeamCreate(team_id=TEAM, config=CONFIG))
    store.acquire(OWNER, BRAIN, TEAM, LeaseRequest(controller_id=CONTROLLER))
    return store


def reservation(**overrides):
    return TaskReserve.model_validate(
        {
            "controller_id": CONTROLLER,
            "epoch": 1,
            "task_id": uuid4().hex,
            "session_id": uuid4().hex,
            "contract": CONTRACT,
            "workspace": Workspace(
                host_id=HOST, repository="repo", worktree=f"/work/{uuid4().hex}", base_sha=SHA
            ),
            **overrides,
        }
    )


def question(task, **overrides):
    return Send.model_validate(
        {
            "message_id": uuid4().hex,
            "recipient_agent_id": task.attempt.agent_id,
            "task_id": task.task_id,
            "attempt_id": task.attempt.attempt_id,
            "epoch": task.attempt.epoch,
            "controller_id": CONTROLLER,
            "kind": "status_query",
            "correlation_id": uuid4().hex,
            "head_sha": SHA,
            "payload": {"scope": "since_commit", "since_commit": SHA},
            **overrides,
        }
    )


def test_atomic_reservation_and_restart_receipt(store, db_uri):
    body = reservation()
    task = store.reserve(OWNER, BRAIN, TEAM, body)
    restarted = BgrStore(db_uri)
    assert restarted.reserve(OWNER, BRAIN, TEAM, body) == task
    snapshot = restarted.recover(OWNER, BRAIN, TEAM)
    assert snapshot.tasks == [task]
    assert [agent.name for agent in snapshot.agents if agent.role == "brain"] == ["brain"]
    assert snapshot.pending_envelopes == 1
    delivery = restarted.drain(OWNER, body.session_id, TEAM)
    assert len(delivery) == 1 and delivery[0].kind == "task"
    assert delivery[0].payload["contract"] == CONTRACT.model_dump()
    ack = Ack(envelope_id=delivery[0].envelope_id, epoch=1)
    consumed = restarted.ack(OWNER, body.session_id, TEAM, ack)
    assert consumed.consumed_at is not None
    assert restarted.ack(OWNER, body.session_id, TEAM, ack) == consumed
    assert restarted.drain(OWNER, body.session_id, TEAM) == []
    with pytest.raises(OmnigentError):
        store.reserve(
            OWNER,
            BRAIN,
            TEAM,
            body.model_copy(
                update={"contract": CONTRACT.model_copy(update={"objective": "Changed"})}
            ),
        )


def test_status_reply_is_durable_correlated_and_not_an_assignment(store, db_uri):
    task = store.reserve(OWNER, BRAIN, TEAM, reservation())
    snapshot = store.recover(OWNER, BRAIN, TEAM)
    query = question(task)
    receipt = store.send(OWNER, BRAIN, TEAM, query)
    assert receipt.state == "accepted"
    restarted = BgrStore(db_uri)
    assert restarted.send(OWNER, BRAIN, TEAM, query) == receipt
    assert restarted.recover(OWNER, BRAIN, TEAM).tasks == snapshot.tasks
    envelopes = restarted.drain(OWNER, task.attempt.session_id, TEAM)
    assert [e.kind for e in envelopes] == ["task", "status_query"]
    assert envelopes[1].sender_agent_id == snapshot.team.brain_agent_id
    assert envelopes[1].sender_session_id == BRAIN
    assert envelopes[1].consumed_at is None and envelopes[1].replied_at is None
    reply = query.model_copy(
        update={
            "message_id": uuid4().hex,
            "recipient_agent_id": snapshot.team.brain_agent_id,
            "controller_id": None,
            "kind": "status_reply",
            "in_reply_to": query.message_id,
            "payload": {"summary": "Still working", "head_sha": SHA},
        }
    )
    with pytest.raises(OmnigentError):
        restarted.send(
            OWNER, task.attempt.session_id, TEAM, reply.model_copy(update={"head_sha": "e" * 40})
        )
    response = restarted.send(OWNER, task.attempt.session_id, TEAM, reply)
    assert restarted.send(OWNER, task.attempt.session_id, TEAM, reply) == response
    delivered_reply = restarted.drain(OWNER, BRAIN, TEAM)[0]
    assert delivered_reply.in_reply_to == query.message_id
    assert delivered_reply.sender_role == "grunt"
    assert delivered_reply.sender_name.startswith("grunt-")
    query_after = restarted.drain(OWNER, task.attempt.session_id, TEAM)[1]
    assert query_after.replied_at is not None and query_after.consumed_at is None
    with pytest.raises(OmnigentError):
        restarted.send(
            OWNER,
            task.attempt.session_id,
            TEAM,
            reply.model_copy(update={"message_id": uuid4().hex}),
        )
    with pytest.raises(OmnigentError):
        restarted.send(OWNER, BRAIN, TEAM, query.model_copy(update={"payload": {"scope": "now"}}))


def test_concurrent_cap_and_idempotent_reservations(store, db_uri):
    body = reservation()

    def reserve(_):
        return BgrStore(db_uri).reserve(OWNER, BRAIN, TEAM, body)

    with ThreadPoolExecutor(max_workers=2) as workers:
        tasks = list(workers.map(reserve, range(2)))
    assert tasks[0] == tasks[1]

    def compete(_):
        try:
            return BgrStore(db_uri).reserve(OWNER, BRAIN, TEAM, reservation())
        except OmnigentError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(compete, range(2)))
    assert sum(result is not None for result in results) == 1
    snapshot = store.recover(OWNER, BRAIN, TEAM)
    assert len(snapshot.tasks) == 2 and snapshot.pending_envelopes == 2


@pytest.mark.parametrize("collision", ["workspace", "session"])
def test_cross_team_uniqueness_and_rollback(store, collision):
    first = reservation()
    store.reserve(OWNER, BRAIN, TEAM, first)
    other_brain, other_team = "e" * 32, "f" * 32
    store.create(OWNER, other_brain, TeamCreate(team_id=other_team, config=CONFIG))
    store.acquire(OWNER, other_brain, other_team, LeaseRequest(controller_id=CONTROLLER))
    body = reservation(
        **{
            collision if collision == "workspace" else "session_id": getattr(
                first, collision if collision == "workspace" else "session_id"
            )
        }
    )
    with pytest.raises(OmnigentError):
        store.reserve(OWNER, other_brain, other_team, body)
    snapshot = store.recover(OWNER, other_brain, other_team)
    assert not snapshot.tasks and len(snapshot.agents) == 1 and snapshot.pending_envelopes == 0
    closed = store.close(
        OWNER,
        BRAIN,
        TEAM,
        TaskClose(
            controller_id=CONTROLLER,
            epoch=1,
            attempt_id=store.recover(OWNER, BRAIN, TEAM).tasks[0].attempt.attempt_id,
            outcome="completed",
            stop_confirmed=True,
        ),
    )
    assert closed.state == "completed"
    if collision == "workspace":
        assert store.reserve(OWNER, other_brain, other_team, body).task_id == body.task_id


def test_expired_lease_requires_epoch_reconciliation_and_stop_confirmation(db_uri):
    now = [1000.0]
    store = BgrStore(db_uri, clock=lambda: now[0])
    store.create(OWNER, BRAIN, TeamCreate(team_id=TEAM, config=CONFIG))
    lease = LeaseRequest(controller_id=CONTROLLER, ttl_seconds=5)
    assert store.acquire(OWNER, BRAIN, TEAM, lease).epoch == 1
    assert store.acquire(OWNER, BRAIN, TEAM, lease).epoch == 1
    body = reservation()
    task = store.reserve(OWNER, BRAIN, TEAM, body)
    checkpoint = Checkpoint(
        attempt_id=task.attempt.attempt_id,
        epoch=1,
        phase="testing",
        summary="Evidence saved",
        head_sha=SHA,
        artifact_refs=["artifact:tests"],
    )
    assert store.checkpoint(OWNER, body.session_id, TEAM, checkpoint).checkpoint == checkpoint
    with pytest.raises(OmnigentError):
        store.checkpoint(OWNER, BRAIN, TEAM, checkpoint)
    replacement = LeaseRequest(controller_id="e" * 32, ttl_seconds=5)
    with pytest.raises(OmnigentError):
        store.acquire(OWNER, BRAIN, TEAM, replacement)
    now[0] += 6
    with pytest.raises(OmnigentError):
        store.checkpoint(OWNER, body.session_id, TEAM, checkpoint)
    current = store.acquire(OWNER, BRAIN, TEAM, replacement)
    assert current.epoch == 2
    with pytest.raises(OmnigentError):
        store.reserve(OWNER, BRAIN, TEAM, body)
    with pytest.raises(OmnigentError):
        store.ack(
            OWNER,
            body.session_id,
            TEAM,
            Ack(envelope_id=store.drain(OWNER, body.session_id, TEAM)[0].envelope_id, epoch=1),
        )
    fence = Fence(controller_id=replacement.controller_id, epoch=2)
    reattached = store.reattach(OWNER, BRAIN, TEAM, task.attempt.attempt_id, fence)
    assert reattached.epoch == 2 and reattached.checkpoint == checkpoint
    store.checkpoint(OWNER, body.session_id, TEAM, checkpoint.model_copy(update={"epoch": 2}))
    closed = store.close(
        OWNER,
        BRAIN,
        TEAM,
        TaskClose(
            **fence.model_dump(),
            attempt_id=reattached.attempt_id,
            outcome="failed",
            stop_confirmed=True,
        ),
    )
    assert closed.state == "failed"
    with pytest.raises(OmnigentError):
        store.reattach(OWNER, BRAIN, TEAM, closed.attempt_id, fence)


def test_configuration_only_affects_future_assignments(store):
    first = store.reserve(OWNER, BRAIN, TEAM, reservation())
    changed = PROFILE.model_copy(update={"model": "new-model", "speed": "fast"})
    config = CONFIG.model_copy(update={"grunt": changed, "max_active_grunts": 1})
    body = ConfigUpdate(controller_id=CONTROLLER, epoch=1, config=config)
    assert store.configure(OWNER, BRAIN, TEAM, body).config_revision == 2
    assert store.configure(OWNER, BRAIN, TEAM, body).config_revision == 2
    with pytest.raises(OmnigentError):
        store.reserve(OWNER, BRAIN, TEAM, reservation())
    config = config.model_copy(update={"max_active_grunts": 2})
    store.configure(OWNER, BRAIN, TEAM, body.model_copy(update={"config": config}))
    second = store.reserve(OWNER, BRAIN, TEAM, reservation())
    assert second.attempt.profile == changed and second.attempt.config_revision == 3
    recovered = {task.task_id: task for task in store.recover(OWNER, BRAIN, TEAM).tasks}
    assert recovered[first.task_id].attempt == first.attempt
    with pytest.raises(OmnigentError):
        store.configure(
            OWNER,
            BRAIN,
            TEAM,
            body.model_copy(update={"config": config.model_copy(update={"brain": changed})}),
        )


@pytest.mark.parametrize("limit", ["MAX_PENDING", "MAX_ENVELOPES", "MAX_TASKS"])
def test_backpressure_is_transactional(store, monkeypatch, limit):
    task = store.reserve(OWNER, BRAIN, TEAM, reservation())
    monkeypatch.setattr(bgr_store, limit, 1)
    before = store.recover(OWNER, BRAIN, TEAM)
    with pytest.raises(OmnigentError):
        if limit == "MAX_TASKS":
            store.reserve(OWNER, BRAIN, TEAM, reservation())
        else:
            store.send(OWNER, BRAIN, TEAM, question(task))
    assert store.recover(OWNER, BRAIN, TEAM) == before


def test_workspace_and_owner_isolation(store, db_uri):
    body = reservation()
    store.reserve(OWNER, BRAIN, TEAM, body)
    for user, brain in (("bob", BRAIN), (OWNER, "f" * 32)):
        with pytest.raises(OmnigentError):
            store.recover(user, brain, TEAM)
    with workspace_scope(9):
        other = BgrStore(db_uri)
        with pytest.raises(OmnigentError):
            other.recover(OWNER, BRAIN, TEAM)
        other.create(OWNER, BRAIN, TeamCreate(team_id=TEAM, config=CONFIG))
        other.acquire(OWNER, BRAIN, TEAM, LeaseRequest(controller_id=CONTROLLER))
        assert other.reserve(OWNER, BRAIN, TEAM, body).task_id == body.task_id
        assert len(other.recover(OWNER, BRAIN, TEAM).tasks) == 1
    assert len(store.recover(OWNER, BRAIN, TEAM).tasks) == 1


@pytest.mark.parametrize(
    "values",
    [
        {"scope": "now", "since_commit": SHA},
        {"scope": "since_commit", "since_commit": "deadbeef"},
        {
            "scope": "since_time",
            "since_time": datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
        },
        {
            "scope": "since_time",
            "since_time": datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        },
        {"scope": "since_time"},
    ],
)
def test_status_scope_fails_closed(values):
    with pytest.raises(ValidationError):
        StatusScope.model_validate(values)


def test_strict_schema_and_valid_status_anchors():
    assert StatusScope(scope="since_time", since_time=datetime(2026, 1, 1, tzinfo=UTC))
    assert StatusScope(scope="since_commit", since_commit=SHA)
    assert StatusScope(scope="now")
    for invalid in (
        {**CONFIG.model_dump(), "max_active_grunts": "4"},
        {**CONFIG.model_dump(), "reviewers": []},
        {**CONFIG.model_dump(), "reviewers": [PROFILE.model_dump()] * 3},
        {**CONFIG.model_dump(), "unknown": True},
    ):
        with pytest.raises(ValidationError):
            TeamConfig.model_validate(invalid)
    with pytest.raises(ValidationError):
        Workspace(host_id=HOST, repository="repo", worktree="/work/../work", base_sha=SHA)
    with pytest.raises(ValidationError):
        TaskClose(
            controller_id=CONTROLLER,
            epoch=1,
            attempt_id="f" * 32,
            outcome="failed",
            stop_confirmed=False,
        )


def test_separate_databases_do_not_share_teams(store, tmp_path):
    separate = BgrStore(f"sqlite:///{tmp_path / 'separate.db'}")
    with pytest.raises(OmnigentError):
        separate.recover(OWNER, BRAIN, TEAM)
    created = separate.create(OWNER, BRAIN, TeamCreate(team_id=TEAM, config=CONFIG))
    assert created.epoch == 0
    assert store.recover(OWNER, BRAIN, TEAM).team.epoch == 1


def test_reviewer_channels_and_foreign_acknowledgements_fail_closed(store):
    from sqlalchemy import update

    from omnigent.bgr import Agent
    from omnigent.db.bgr_models import SqlBgrAgent

    task = store.reserve(OWNER, BRAIN, TEAM, reservation())
    snapshot = store.recover(OWNER, BRAIN, TEAM)
    worker = next(agent for agent in snapshot.agents if agent.role == "grunt")
    reviewer = Agent(**{**worker.model_dump(), "role": "reviewer"})
    # Even imported future reviewer records cannot open an unsafe channel.
    with store._sessions("fixture_future_reviewer") as database:
        database.execute(
            update(SqlBgrAgent)
            .where(SqlBgrAgent.id == worker.agent_id)
            .values(document=reviewer.model_dump_json())
        )
    with pytest.raises(OmnigentError, match="independent-review"):
        store.send(OWNER, BRAIN, TEAM, question(task))
    envelope = store.drain(OWNER, worker.session_id, TEAM)[0]
    with pytest.raises(OmnigentError):
        store.ack(OWNER, BRAIN, TEAM, Ack(envelope_id=envelope.envelope_id, epoch=1))


def test_team_create_idempotency_and_single_brain(store):
    original = store.recover(OWNER, BRAIN, TEAM).team
    assert store.create(OWNER, BRAIN, TeamCreate(team_id=TEAM, config=CONFIG)) == original
    with pytest.raises(OmnigentError):
        store.create(OWNER, BRAIN, TeamCreate(team_id="e" * 32, config=CONFIG))
    with pytest.raises(OmnigentError):
        store.create("bob", BRAIN, TeamCreate(team_id=TEAM, config=CONFIG))
    with pytest.raises(OmnigentError):
        store.create(
            OWNER,
            BRAIN,
            TeamCreate(team_id=TEAM, config=CONFIG.model_copy(update={"max_active_grunts": 1})),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"protocol_version": True},
        {"protocol_version": "1"},
        {"payload": {"value": float("nan")}},
        {"payload": {"value": float("inf")}},
        {"payload": {"value": "x" * 8001}},
        {"correlation_id": None},
        {"kind": "answer", "in_reply_to": None},
        {"sender_agent_id": BRAIN},
    ],
)
def test_invalid_envelopes_rejected_before_acceptance(overrides):
    with pytest.raises(ValidationError):
        Send.model_validate(
            {
                "message_id": "1" * 32,
                "recipient_agent_id": "2" * 32,
                "task_id": "3" * 32,
                "attempt_id": "4" * 32,
                "epoch": 1,
                "kind": "question",
                "correlation_id": "5" * 32,
                "payload": {"question": "Ready?"},
                **overrides,
            }
        )


def test_reservation_delivery_bound_and_literal_stop_confirmation():
    contract = CONTRACT.model_copy(
        update={
            "instructions": "x" * 4000,
            "objective": "x" * 2000,
            "acceptance": ["x" * 256] * 16,
        }
    )
    with pytest.raises(ValidationError, match="delivery exceeds"):
        reservation(contract=contract)
    with pytest.raises(ValidationError):
        TaskClose(
            controller_id=CONTROLLER,
            epoch=1,
            attempt_id="1" * 32,
            outcome="failed",
            stop_confirmed=1,
        )
    with pytest.raises(ValidationError):
        TeamCreate(team_id=TEAM, config=CONFIG, protocol_version=True)


def test_stale_mailbox_discard_recovers_full_pages_and_capacity(db_uri, monkeypatch):
    now = [1000.0]
    store = BgrStore(db_uri, clock=lambda: now[0])
    store.create(OWNER, BRAIN, TeamCreate(team_id=TEAM, config=CONFIG))
    store.acquire(OWNER, BRAIN, TEAM, LeaseRequest(controller_id=CONTROLLER, ttl_seconds=5))
    task = store.reserve(OWNER, BRAIN, TEAM, reservation())
    worker = task.attempt.session_id
    monkeypatch.setattr(bgr_store, "MAX_PENDING", 101)
    queries = [question(task) for _ in range(100)]
    for query in queries:
        store.send(OWNER, BRAIN, TEAM, query)
    original_page = store.drain(OWNER, worker, TEAM, limit=100)
    assert len(original_page) == 100
    now[0] += 6
    controller = "e" * 32
    lease = store.acquire(OWNER, BRAIN, TEAM, LeaseRequest(controller_id=controller))
    assert lease.epoch == 2
    store.reattach(
        OWNER, BRAIN, TEAM, task.attempt.attempt_id, Fence(controller_id=controller, epoch=2)
    )
    action = MailboxDiscard(
        epoch=2,
        envelope_ids=[envelope.envelope_id for envelope in original_page],
        reason="Reconciled survivor; superseded deliveries are not executable",
    )
    for user, session in (("bob", worker), (OWNER, BRAIN), (OWNER, "f" * 32)):
        with pytest.raises(OmnigentError):
            store.discard(user, session, TEAM, action)
    with workspace_scope(9), pytest.raises(OmnigentError):
        store.discard(OWNER, worker, TEAM, action)
    with pytest.raises(OmnigentError):
        store.discard(OWNER, worker, TEAM, action.model_copy(update={"epoch": 1}))
    for epoch in (1, 2):
        with pytest.raises(OmnigentError):
            store.ack(
                OWNER, worker, TEAM, Ack(epoch=epoch, envelope_id=original_page[0].envelope_id)
            )
    fresh = question(task, epoch=2, controller_id=controller)
    with pytest.raises(OmnigentError, match="backpressure"):
        store.send(OWNER, BRAIN, TEAM, fresh)
    discarded = store.discard(OWNER, worker, TEAM, action)
    for before, after in zip(original_page, discarded, strict=True):
        assert after.model_dump(exclude={"discard"}) == before.model_dump(exclude={"discard"})
        assert after.consumed_at is None and after.replied_at is None
        assert after.discard.session_id == worker and after.discard.epoch == 2
        assert after.discard.reason == action.reason and after.discard.discarded_at == now[0]
    restarted = BgrStore(db_uri, clock=lambda: now[0])
    assert restarted.discard(OWNER, worker, TEAM, action) == discarded
    with pytest.raises(OmnigentError):
        restarted.discard(
            OWNER, worker, TEAM, action.model_copy(update={"reason": "Different decision"})
        )
    assert restarted.recover(OWNER, BRAIN, TEAM).pending_envelopes == 1
    remaining = restarted.drain(OWNER, worker, TEAM)[0]
    receipt = restarted.send(OWNER, BRAIN, TEAM, fresh)
    mixed = MailboxDiscard(
        epoch=2, envelope_ids=[remaining.envelope_id, receipt.envelope_id], reason=action.reason
    )
    with pytest.raises(OmnigentError):
        restarted.discard(OWNER, worker, TEAM, mixed)
    assert restarted.drain(OWNER, worker, TEAM)[0].discard is None
    assert restarted.recover(OWNER, BRAIN, TEAM).pending_envelopes == 2
    restarted.discard(
        OWNER, worker, TEAM, mixed.model_copy(update={"envelope_ids": [remaining.envelope_id]})
    )
    delivered = restarted.drain(OWNER, worker, TEAM)
    assert len(delivered) == 1 and delivered[0].envelope_id == receipt.envelope_id
    assert delivered[0].discard is None
    consumed = restarted.ack(OWNER, worker, TEAM, Ack(epoch=2, envelope_id=receipt.envelope_id))
    assert consumed.consumed_at is not None and consumed.discard is None
    assert restarted.drain(OWNER, worker, TEAM) == []
    assert restarted.recover(OWNER, BRAIN, TEAM).pending_envelopes == 0
    reply = queries[0].model_copy(
        update={
            "message_id": uuid4().hex,
            "epoch": 2,
            "controller_id": None,
            "recipient_agent_id": lease.brain_agent_id,
            "kind": "status_reply",
            "in_reply_to": queries[0].message_id,
            "payload": {"summary": "Stale reply"},
        }
    )
    with pytest.raises(OmnigentError):
        restarted.send(OWNER, worker, TEAM, reply)
    assert restarted.discard(OWNER, worker, TEAM, action) == discarded


@pytest.mark.parametrize(
    "overrides",
    [
        {"envelope_ids": []},
        {"envelope_ids": ["1" * 32] * 101},
        {"envelope_ids": ["1" * 32, "1" * 32]},
        {"reason": " "},
        {"reason": "x" * 513},
        {"epoch": 0},
        {"epoch": "2"},
    ],
)
def test_discard_requires_bounded_explicit_action(overrides):
    with pytest.raises(ValidationError):
        MailboxDiscard.model_validate(
            {"epoch": 2, "envelope_ids": ["1" * 32], "reason": "Reconciled", **overrides}
        )


def test_terminal_membership_never_gains_another_attempt_writer(store):
    first_body = reservation()
    first = store.reserve(OWNER, BRAIN, TEAM, first_body)
    close = TaskClose(
        controller_id=CONTROLLER,
        epoch=1,
        attempt_id=first.attempt.attempt_id,
        outcome="failed",
        stop_confirmed=True,
    )
    result = store.close(OWNER, BRAIN, TEAM, close)
    assert store.close(OWNER, BRAIN, TEAM, close) == result
    with pytest.raises(OmnigentError):
        store.close(OWNER, BRAIN, TEAM, close.model_copy(update={"outcome": "completed"}))
    replacement = store.reserve(OWNER, BRAIN, TEAM, reservation(workspace=first_body.workspace))
    assert replacement.attempt.workspace == first.attempt.workspace
    with pytest.raises(OmnigentError):
        store.reserve(OWNER, BRAIN, TEAM, reservation(session_id=first.attempt.session_id))
    brain_agent = store.recover(OWNER, BRAIN, TEAM).team.brain_agent_id
    for task in (first, replacement):
        for recipient in (task.attempt.agent_id, brain_agent):
            message = question(
                task,
                kind="progress",
                controller_id=None,
                recipient_agent_id=recipient,
                payload={"summary": "Old worker"},
            )
            with pytest.raises(OmnigentError):
                store.send(OWNER, first.attempt.session_id, TEAM, message)
    with pytest.raises(OmnigentError):
        store.checkpoint(
            OWNER,
            first.attempt.session_id,
            TEAM,
            Checkpoint(
                attempt_id=first.attempt.attempt_id,
                epoch=1,
                phase="editing",
                summary="Stale writer",
            ),
        )
    with pytest.raises(OmnigentError):
        store.reattach(
            OWNER, BRAIN, TEAM, first.attempt.attempt_id, Fence(controller_id=CONTROLLER, epoch=1)
        )
    # Retrying the old close must not release the replacement's reservation.
    assert store.close(OWNER, BRAIN, TEAM, close) == result
    with pytest.raises(OmnigentError):
        store.reserve(OWNER, BRAIN, TEAM, reservation(workspace=first_body.workspace))


def test_discard_preserves_reply_evidence_and_never_relabels_consumption(db_uri):
    now = [1000.0]
    store = BgrStore(db_uri, clock=lambda: now[0])
    store.create(OWNER, BRAIN, TeamCreate(team_id=TEAM, config=CONFIG))
    lease = store.acquire(
        OWNER, BRAIN, TEAM, LeaseRequest(controller_id=CONTROLLER, ttl_seconds=5)
    )
    task = store.reserve(OWNER, BRAIN, TEAM, reservation())
    worker = task.attempt.session_id
    query = question(task)
    receipt = store.send(OWNER, BRAIN, TEAM, query)
    delivery = store.drain(OWNER, worker, TEAM)
    consumed = store.ack(OWNER, worker, TEAM, Ack(epoch=1, envelope_id=delivery[0].envelope_id))
    response = store.send(
        OWNER,
        worker,
        TEAM,
        query.model_copy(
            update={
                "message_id": uuid4().hex,
                "controller_id": None,
                "recipient_agent_id": lease.brain_agent_id,
                "kind": "status_reply",
                "in_reply_to": query.message_id,
                "payload": {"summary": "Evidence saved"},
            }
        ),
    )
    replied = store.drain(OWNER, worker, TEAM)[0]
    assert replied.replied_at is not None and replied.consumed_at is None
    now[0] += 6
    store.acquire(OWNER, BRAIN, TEAM, LeaseRequest(controller_id=CONTROLLER))
    action = MailboxDiscard(
        epoch=2,
        envelope_ids=[receipt.envelope_id, consumed.envelope_id],
        reason="Reconcile stale replies, not successful consumption",
    )
    with pytest.raises(OmnigentError):
        store.discard(OWNER, worker, TEAM, action)
    assert store.drain(OWNER, worker, TEAM)[0] == replied
    result = store.discard(
        OWNER, worker, TEAM, action.model_copy(update={"envelope_ids": [receipt.envelope_id]})
    )[0]
    assert result.replied_at == replied.replied_at
    assert result.consumed_at is None and result.discard is not None
    brain_result = store.discard(
        OWNER, BRAIN, TEAM, action.model_copy(update={"envelope_ids": [response.envelope_id]})
    )[0]
    assert brain_result.discard.session_id == BRAIN
    assert store.recover(OWNER, BRAIN, TEAM).pending_envelopes == 0
