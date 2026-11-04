"""Durable loss transitions with explicit supervisor evidence, not native stop ACKs."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnigent.bgr import (
    Checkpoint,
    Fence,
    LaunchRequest,
    LeaseRequest,
    NativeBinding,
    NativeBrainBinding,
    RuntimeStop,
    RuntimeStopEvidence,
    TaskReserve,
    TeamCreate,
    Workspace,
)
from omnigent.bgr_control import BgrControl
from omnigent.bgr_review import ReviewRoundCreate
from omnigent.errors import OmnigentError
from omnigent.stores.bgr_control_store import BgrControlActions
from omnigent.stores.bgr_review_store import BgrReviewStore
from tests.stores.test_bgr_store import CONFIG, CONTRACT, HOST, OWNER, SHA, TEAM, question


@pytest.fixture
def recovery_store(db_uri):
    store = BgrReviewStore(db_uri)
    profile = CONFIG.brain.model_copy(update={"harness": "codex-native"})
    config = CONFIG.model_copy(update={"brain": profile, "grunt": profile, "reviewers": [profile]})
    intent = store.reserve_launch(
        OWNER,
        HOST,
        LaunchRequest(
            team_id=TEAM,
            agent_id="test-agent",
            workspace="/work",
            config=config,
            initial_prompt="The one durable initial task",
        ),
    )
    store.create(OWNER, intent.brain_session_id, TeamCreate(team_id=TEAM, config=config))
    store.acquire(
        OWNER, intent.brain_session_id, TEAM, LeaseRequest(controller_id=intent.controller_id)
    )
    binding = NativeBrainBinding(
        team_id=TEAM, epoch=1, thread_id=str(uuid4()), runtime_id=uuid4().hex, host_id=HOST
    )
    fence = Fence(controller_id=intent.controller_id, epoch=1)
    store.native_brain_binding(OWNER, intent.brain_session_id, TEAM, fence, binding=binding)
    store.mark_launch_running(OWNER, intent.brain_session_id, TEAM, fence)
    return store, intent, binding, fence


def stopped(session_id, thread_id, role="brain", **changes):
    return RuntimeStop(
        session_id=session_id,
        thread_id=thread_id,
        role=role,
        process_tag=uuid4().hex,
        process_domain_id="trusted-supervisor-domain",
        domain_stop_confirmed=True,
        stop_confirmed=True,
        **changes,
    )


def test_loss_recovery_revokes_before_stop_preserves_task_and_fences_old_callbacks(
    recovery_store, monkeypatch
):
    store, intent, binding, fence = recovery_store
    brain = intent.brain_session_id
    task = store.reserve(
        OWNER,
        brain,
        TEAM,
        TaskReserve(
            **fence.model_dump(),
            task_id=uuid4().hex,
            session_id=uuid4().hex,
            contract=CONTRACT,
            workspace=Workspace(
                host_id=HOST, repository="repo", worktree="/work/grunt", base_sha=SHA
            ),
        ),
    )
    worker_thread = str(uuid4())
    store.bind_native(
        OWNER,
        brain,
        TEAM,
        fence,
        NativeBinding(
            team_id=TEAM, attempt_id=task.attempt.attempt_id, epoch=1, thread_id=worker_thread
        ),
    )
    checkpoint = Checkpoint(
        attempt_id=task.attempt.attempt_id,
        epoch=1,
        phase="working",
        summary="Partially implemented, consumption uncertain",
        head_sha=SHA,
    )
    store.checkpoint(OWNER, task.attempt.session_id, TEAM, checkpoint)
    sent = store.send(OWNER, brain, TEAM, question(task, controller_id=fence.controller_id))
    command = BgrControl(action="status", action_id=uuid4().hex)
    actions = BgrControlActions(store)
    actions.claim(OWNER, brain, TEAM, fence, command)
    recovery_id, replacement = uuid4().hex, uuid4().hex
    args = {
        "recovery_id": recovery_id,
        "runtime_id": binding.runtime_id,
        "replacement_controller_id": replacement,
    }
    # Lease expiry does not confer native stop authority or prevent explicit recovery.
    monkeypatch.setattr(store, "_now_us", lambda: 10**18)
    recovery = store.begin_runtime_recovery(OWNER, brain, TEAM, fence, **args)
    assert recovery.state == "stopping"
    assert store.begin_runtime_recovery(OWNER, brain, TEAM, fence, **args) == recovery
    assert store.get_launch(OWNER, TEAM).state == "blocked"
    with pytest.raises(OmnigentError, match="launch retry"):
        store.claim_launch(OWNER, TEAM, uuid4().hex)
    with pytest.raises(OmnigentError):
        store.check_runtime_input(OWNER, brain, TEAM)
    with pytest.raises(OmnigentError):
        store.native_brain_binding(OWNER, brain, TEAM, fence)
    with pytest.raises(OmnigentError):
        store.acquire(OWNER, brain, TEAM, LeaseRequest(controller_id=fence.controller_id))
    with pytest.raises(OmnigentError):
        actions.claim(OWNER, brain, TEAM, fence, command)
    evidence = RuntimeStopEvidence(
        runtime_id=binding.runtime_id,
        host_id=HOST,
        inventory_complete=True,
        processes=[stopped(brain, binding.thread_id)],
    )
    with pytest.raises(OmnigentError, match="unconfirmed"):
        store.complete_runtime_recovery(
            OWNER, brain, TEAM, recovery_id=recovery_id, evidence=evidence
        )
    evidence = evidence.model_copy(
        update={
            "processes": [
                *evidence.processes,
                stopped(task.attempt.session_id, worker_thread, "grunt"),
            ]
        }
    )
    team = store.complete_runtime_recovery(
        OWNER, brain, TEAM, recovery_id=recovery_id, evidence=evidence
    )
    assert team.epoch == 2 and team.controller_id == replacement
    assert team.brain_session_id == brain
    assert (
        store.complete_runtime_recovery(
            OWNER, brain, TEAM, recovery_id=recovery_id, evidence=evidence
        )
        == team
    )
    assert store.get_launch(OWNER, TEAM).request.initial_prompt == intent.request.initial_prompt
    recovered = store.recover(OWNER, brain, TEAM)
    assert recovered.tasks[0].attempt.state == "failed"
    assert recovered.tasks[0].attempt.checkpoint == checkpoint
    with store._sessions("verify_loss_evidence_is_not_consumption") as db:
        from omnigent.bgr import Envelope
        from omnigent.db import current_workspace_id
        from omnigent.db.bgr_models import SqlBgrEnvelope

        row = db.get(SqlBgrEnvelope, (current_workspace_id(), sent.envelope_id))
        envelope = Envelope.model_validate_json(row.document)
        assert envelope.consumed_at is None and envelope.discard is not None
    replacement_fence = Fence(controller_id=replacement, epoch=2)
    with pytest.raises(OmnigentError):
        actions.claim(
            OWNER,
            brain,
            TEAM,
            replacement_fence,
            BgrControl(action="status", action_id=uuid4().hex),
        )
    new_binding = binding.model_copy(
        update={"epoch": 2, "runtime_id": uuid4().hex, "thread_id": str(uuid4())}
    )
    store.native_brain_binding(OWNER, brain, TEAM, replacement_fence, binding=new_binding)
    # The exact pre-input bind retry is stable; a second new thread is not.
    assert (
        store.native_brain_binding(OWNER, brain, TEAM, replacement_fence, binding=new_binding)
        == new_binding
    )
    with pytest.raises(OmnigentError, match="immutable"):
        store.native_brain_binding(
            OWNER,
            brain,
            TEAM,
            replacement_fence,
            binding=new_binding.model_copy(update={"thread_id": str(uuid4())}),
        )
    store.mark_launch_running(OWNER, brain, TEAM, replacement_fence)
    assert store.check_runtime_input(OWNER, brain, TEAM).epoch == 2
    store.check_session_runtime_input(brain)
    with pytest.raises(OmnigentError, match="inactive attempt"):
        store.check_session_runtime_input(task.attempt.session_id)
    assert store.get_team(OWNER, brain, TEAM).runtime_recoveries[-1].state == "attached"
    with pytest.raises(OmnigentError):
        actions.claim(OWNER, brain, TEAM, fence, command)
    with pytest.raises(OmnigentError, match="ID conflict"):
        actions.claim(OWNER, brain, TEAM, replacement_fence, command)


@pytest.mark.parametrize("field", ["stop_confirmed", "domain_stop_confirmed"])
@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_process_ack_and_unknown_domain_are_never_stop_evidence(field, value):
    evidence = stopped(uuid4().hex, str(uuid4())).model_dump()
    evidence[field] = value
    with pytest.raises(ValidationError):
        RuntimeStop.model_validate(evidence)


def test_loss_seals_unfinished_reviews_without_rewriting_independent_findings(recovery_store):
    store, intent, binding, fence = recovery_store
    brain = intent.brain_session_id
    round = store.reserve_review_round(
        OWNER,
        brain,
        TEAM,
        ReviewRoundCreate(
            **fence.model_dump(),
            round_id=uuid4().hex,
            repository="ModelCloud/omnigent",
            pr_number=98,
            base_sha=SHA,
            head_sha="e" * 40,
        ),
    )
    recovery_id = uuid4().hex
    store.begin_runtime_recovery(
        OWNER,
        brain,
        TEAM,
        fence,
        recovery_id=recovery_id,
        runtime_id=binding.runtime_id,
        replacement_controller_id=uuid4().hex,
    )
    store.complete_runtime_recovery(
        OWNER,
        brain,
        TEAM,
        recovery_id=recovery_id,
        evidence=RuntimeStopEvidence(
            runtime_id=binding.runtime_id,
            host_id=HOST,
            inventory_complete=True,
            processes=[stopped(brain, binding.thread_id)],
        ),
    )
    result = store.get_review_round(OWNER, brain, TEAM, round.round_id)
    assert all(job.state == "sealed" and job.result.status == "incomplete" for job in result.jobs)
    assert all(job.publication.state == "pending" for job in result.jobs)


@pytest.mark.parametrize("role", ["brain", "grunt", "reviewer"])
def test_session_input_uses_durable_membership_and_denies_all_roles_during_loss(
    recovery_store, role
):
    store, intent, binding, fence = recovery_store
    brain = intent.brain_session_id
    session_id = brain
    if role == "grunt":
        task = store.reserve(
            OWNER,
            brain,
            TEAM,
            TaskReserve(
                **fence.model_dump(),
                task_id=uuid4().hex,
                session_id=uuid4().hex,
                contract=CONTRACT,
                workspace=Workspace(
                    host_id=HOST, repository="repo", worktree="/work/guard", base_sha=SHA
                ),
            ),
        )
        session_id, attempt_id = task.attempt.session_id, task.attempt.attempt_id
    elif role == "reviewer":
        round = store.reserve_review_round(
            OWNER,
            brain,
            TEAM,
            ReviewRoundCreate(
                **fence.model_dump(),
                round_id=uuid4().hex,
                repository="owner/repo",
                pr_number=1,
                base_sha=SHA,
                head_sha="e" * 40,
            ),
        )
        job = round.jobs[0]
        session_id, attempt_id = job.session_id, job.attempt_id
    if role != "brain":
        native = NativeBinding(
            team_id=TEAM, attempt_id=attempt_id, epoch=1, thread_id=str(uuid4())
        )
        if role == "reviewer":
            store.activate_reviewer(
                OWNER,
                brain,
                TEAM,
                fence,
                round_id=round.round_id,
                review_id=job.review_id,
                binding=native,
                workspace=Workspace(
                    host_id=HOST,
                    repository="owner/repo",
                    worktree="/snapshots/guard",
                    base_sha=SHA,
                ),
            )
        else:
            store.bind_native(OWNER, brain, TEAM, fence, native)
    store.check_session_runtime_input(session_id)
    with pytest.raises(OmnigentError, match="explicit verified runtime recovery"):
        store.check_session_runtime_input(session_id, restart=True)
    store.begin_runtime_recovery(
        OWNER,
        brain,
        TEAM,
        fence,
        recovery_id=uuid4().hex,
        runtime_id=binding.runtime_id,
        replacement_controller_id=uuid4().hex,
    )
    with pytest.raises(OmnigentError):
        store.check_session_runtime_input(session_id)
    store.check_session_runtime_input(uuid4().hex)
