"""Durable loss transitions with explicit supervisor evidence, not native stop ACKs."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

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
    Team,
    TeamCreate,
    Workspace,
)
from omnigent.bgr_control import BgrControl
from omnigent.bgr_review import ReviewRoundCreate
from omnigent.bgr_supervision import (
    ClosedGenerationManifest,
    GenerationAdmission,
    GenerationStopReceipt,
    SupervisedGeneration,
    SupervisorStopped,
    SupervisorStopRequest,
    TerminalClaim,
    supervision_digest,
)
from omnigent.db import current_workspace_id
from omnigent.db.bgr_models import (
    SqlBgrAgent,
    SqlBgrAttempt,
    SqlBgrEnvelope,
    SqlBgrLaunch,
    SqlBgrNativeBinding,
    SqlBgrTeam,
)
from omnigent.db.db_models import SqlUser
from omnigent.errors import OmnigentError
from omnigent.runner.identity import token_bound_runner_id
from omnigent.stores.bgr_control_store import BgrControlActions
from omnigent.stores.bgr_review_store import BgrReviewStore
from omnigent.stores.bgr_store import BgrStore
from tests.stores.test_bgr_store import (
    CONFIG,
    CONTRACT,
    HOST,
    OWNER,
    SHA,
    TEAM,
    question,
    reservation,
)


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


@pytest.fixture
def supervised_admission(recovery_store):
    _, intent, binding, fence = recovery_store
    return GenerationAdmission(
        generation=SupervisedGeneration(
            supervisor_id=uuid4().hex,
            host_id=HOST,
            boot_id=str(uuid4()),
            team_id=TEAM,
            brain_session_id=intent.brain_session_id,
            controller_id=fence.controller_id,
            epoch=fence.epoch,
            runtime_id=binding.runtime_id,
            generation_id=uuid4().hex,
            launch_id=uuid4().hex,
            runner_id=token_bound_runner_id("supervised-binding"),
            credential_id=uuid4().hex,
        ),
        domain={
            "domain_id": uuid4().hex,
            "mount_id": 1,
            "device": 0,
            "inode": 1,
            "path": "/trusted/bgr",
        },
        manifest_id=uuid4().hex,
        root_claim_id=uuid4().hex,
        admission_sequence=1,
        launch_sha256="e" * 64,
    )


def test_supervised_admission_requires_reservation_and_preserves_revoked_classification(
    recovery_store, supervised_admission, db_uri
):
    store, intent, binding, fence = recovery_store
    generation = supervised_admission.generation
    with pytest.raises(OmnigentError):
        store.activate_supervised_generation(OWNER, supervised_admission)
    reserved = store.reserve_supervised_generation(OWNER, generation)
    assert reserved.state == "reserved"
    assert store.reserve_supervised_generation(OWNER, generation) == reserved
    assert store.generation_for_runner(generation.runner_id) == generation
    with pytest.raises(OmnigentError):
        store.validate_supervised_generation(generation)
    active = store.activate_supervised_generation(OWNER, supervised_admission)
    assert store.activate_supervised_generation(OWNER, supervised_admission) == active
    assert store.validate_supervised_generation(generation) == OWNER
    with pytest.raises(OmnigentError):
        store.activate_supervised_generation(
            OWNER, supervised_admission.model_copy(update={"manifest_id": uuid4().hex})
        )
    store.begin_runtime_recovery(
        OWNER,
        intent.brain_session_id,
        TEAM,
        fence,
        recovery_id=uuid4().hex,
        runtime_id=binding.runtime_id,
        replacement_controller_id=uuid4().hex,
    )
    cold = BgrReviewStore(db_uri)
    assert cold.generation_for_runner(generation.runner_id) == generation
    with pytest.raises(OmnigentError):
        cold.validate_supervised_generation(generation)
    with pytest.raises(OmnigentError):
        cold.activate_supervised_generation(OWNER, supervised_admission)
    assert cold.get_launch(OWNER, TEAM).supervised_generations[0].state == "revoked"


@pytest.mark.parametrize(
    "field,value",
    [
        ("team_id", "e" * 32),
        ("brain_session_id", "e" * 32),
        ("controller_id", "e" * 32),
        ("epoch", 2),
        ("runtime_id", "e" * 32),
        ("host_id", "foreign-host"),
        ("boot_id", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        ("supervisor_id", "e" * 32),
        ("runner_id", "another-runner"),
        ("generation_id", "e" * 32),
        ("launch_id", "e" * 32),
        ("credential_id", "e" * 32),
    ],
)
def test_supervised_auth_rejects_every_foreign_identity(
    recovery_store, supervised_admission, field, value
):
    store = recovery_store[0]
    generation = supervised_admission.generation
    store.reserve_supervised_generation(OWNER, generation)
    store.activate_supervised_generation(OWNER, supervised_admission)
    with pytest.raises(OmnigentError):
        store.validate_supervised_generation(generation.model_copy(update={field: value}))


@pytest.mark.parametrize("fault", ["corrupt", "duplicate", "overflow", "expired", "epoch"])
def test_supervised_classification_and_validation_fail_closed(
    recovery_store, supervised_admission, fault
):
    store = recovery_store[0]
    generation = supervised_admission.generation
    store.reserve_supervised_generation(OWNER, generation)
    store.activate_supervised_generation(OWNER, supervised_admission)
    intent = store.get_launch(OWNER, TEAM)
    with store._sessions("inject_supervised_ledger_fault") as db:
        row = db.get(SqlBgrLaunch, (current_workspace_id(), TEAM))
        if fault == "corrupt":
            row.document = '{"broken":true}'
        elif fault in ("duplicate", "overflow"):
            records = intent.supervised_generations * (2 if fault == "duplicate" else 17)
            row.document = intent.model_copy(
                update={"supervised_generations": records}
            ).model_dump_json()
        else:
            team = db.get(SqlBgrTeam, (current_workspace_id(), TEAM))
            if fault == "expired":
                team.lease_expires_at_us = 0
            else:
                team.epoch += 1
    if fault in ("corrupt", "duplicate", "overflow"):
        with pytest.raises((OmnigentError, ValidationError)):
            store.generation_for_runner(generation.runner_id)
    else:
        assert store.generation_for_runner(generation.runner_id) == generation
        with pytest.raises(OmnigentError):
            store.validate_supervised_generation(generation)


def test_supervised_history_overflow_never_evicts_tombstones(recovery_store, supervised_admission):
    store = recovery_store[0]
    first = supervised_admission.generation
    store.reserve_supervised_generation(OWNER, first)
    intent = store.get_launch(OWNER, TEAM)
    historical = []
    for _ in range(16):
        generation = first.model_copy(
            update={
                "runner_id": token_bound_runner_id(uuid4().hex),
                "generation_id": uuid4().hex,
                "credential_id": uuid4().hex,
                "launch_id": uuid4().hex,
            }
        )
        historical.append(
            intent.supervised_generations[0].model_copy(
                update={"generation": generation, "state": "revoked"}
            )
        )
    with store._sessions("fill_supervised_history_without_eviction") as db:
        row = db.get(SqlBgrLaunch, (current_workspace_id(), TEAM))
        row.document = intent.model_copy(
            update={"supervised_generations": historical}
        ).model_dump_json()
    before = store.get_launch(OWNER, TEAM)
    with pytest.raises(OmnigentError):
        store.reserve_supervised_generation(OWNER, first)
    assert store.get_launch(OWNER, TEAM) == before
    assert (
        store.generation_for_runner(historical[0].generation.runner_id) == historical[0].generation
    )


@pytest.mark.parametrize("fault", ["deleted", "recreated", "native-runtime"])
def test_supervised_authority_pins_account_and_native_runtime(
    recovery_store, supervised_admission, fault
):
    store = recovery_store[0]
    generation = supervised_admission.generation
    with store._sessions("create_supervised_owner_registration") as db:
        db.add(SqlUser(workspace_id=current_workspace_id(), id=OWNER, account_generation="a" * 32))
    store.reserve_supervised_generation(OWNER, generation)
    store.activate_supervised_generation(OWNER, supervised_admission)
    assert store.validate_supervised_generation(generation) == OWNER
    team = store.get_team(OWNER, generation.brain_session_id, TEAM)
    with store._sessions("invalidate_supervised_owner_or_runtime") as db:
        if fault == "native-runtime":
            mapping = db.get(SqlBgrNativeBinding, (current_workspace_id(), team.brain_agent_id))
            binding = NativeBrainBinding.model_validate_json(mapping.document)
            mapping.document = binding.model_copy(
                update={"runtime_id": uuid4().hex}
            ).model_dump_json()
        else:
            user = db.get(SqlUser, (current_workspace_id(), OWNER))
            if fault == "deleted":
                user.deleted_at = 1
            else:
                user.account_generation = "b" * 32
    assert store.generation_for_runner(generation.runner_id) == generation
    with pytest.raises(OmnigentError):
        store.validate_supervised_generation(generation)


def test_supervised_document_byte_limit_is_fail_closed(recovery_store, supervised_admission):
    store = recovery_store[0]
    store.reserve_supervised_generation(OWNER, supervised_admission.generation)
    intent = store.get_launch(OWNER, TEAM)
    history = []
    for _ in range(16):
        generation = supervised_admission.generation.model_copy(
            update={
                "runner_id": uuid4().hex,
                "generation_id": uuid4().hex,
                "launch_id": uuid4().hex,
                "credential_id": uuid4().hex,
            }
        )
        admission = supervised_admission.model_copy(
            update={
                "generation": generation,
                "domain": supervised_admission.domain.model_copy(
                    update={"path": "/" + "x" * 4000}
                ),
            }
        )
        history.append(
            intent.supervised_generations[0].model_copy(
                update={"generation": generation, "admission": admission, "state": "revoked"}
            )
        )
    oversized = intent.model_copy(update={"supervised_generations": history})
    assert len(oversized.model_dump_json().encode()) > 60_000
    with store._sessions("inject_oversized_supervised_history") as db:
        row = db.get(SqlBgrLaunch, (current_workspace_id(), TEAM))
        row.document = oversized.model_dump_json()
    with pytest.raises(OmnigentError):
        store.generation_for_runner(history[0].generation.runner_id)
    assert store.get_launch(OWNER, TEAM) == oversized


@pytest.mark.parametrize("ready", [False, True])
def test_recovery_admission_selects_original_revoked_identity_after_restart(
    recovery_store, supervised_admission, db_uri, ready
):
    store, intent, binding, fence = recovery_store
    recovery_id, replacement_controller_id = uuid4().hex, uuid4().hex
    with pytest.raises(OmnigentError):
        store.recovery_admission(OWNER, TEAM, recovery_id)
    store.reserve_supervised_generation(OWNER, supervised_admission.generation)
    store.activate_supervised_generation(OWNER, supervised_admission)
    store.begin_runtime_recovery(
        OWNER,
        intent.brain_session_id,
        TEAM,
        fence,
        recovery_id=recovery_id,
        runtime_id=binding.runtime_id,
        replacement_controller_id=replacement_controller_id,
    )
    if ready:
        with store._sessions("restore_ready_recovery_with_original_revoked_admission") as db:
            row = db.get(SqlBgrTeam, (current_workspace_id(), TEAM))
            document = Team.model_validate_json(row.document)
            recovery = document.runtime_recoveries[-1].model_copy(
                update={"state": "ready", "stop_manifest_sha256": "f" * 64}
            )
            row.document = document.model_copy(
                update={"runtime_recoveries": [recovery]}
            ).model_dump_json()
            row.epoch = fence.epoch + 1
            row.controller_id = replacement_controller_id
    cold = BgrStore(db_uri)
    before = cold.get_launch(OWNER, TEAM)
    assert cold.recovery_admission(OWNER, TEAM, recovery_id) == supervised_admission
    assert cold.get_launch(OWNER, TEAM) == before
    assert before.state == "blocked"
    assert before.supervised_generations[0].state == "revoked"
    for owner, team_id, selected_id in (
        ("foreign", TEAM, recovery_id),
        (OWNER, uuid4().hex, recovery_id),
        (OWNER, TEAM, uuid4().hex),
    ):
        with pytest.raises(OmnigentError):
            cold.recovery_admission(owner, team_id, selected_id)
    with pytest.raises(OmnigentError):
        cold.claim_launch(OWNER, TEAM, uuid4().hex)


@pytest.mark.parametrize(
    "fault",
    [
        "corrupt",
        "duplicate",
        "oversized",
        "padding",
        "missing",
        "unadmitted",
        "active",
        "admission-mismatch",
        "team_id",
        "brain_session_id",
        "controller_id",
        "epoch",
        "runtime_id",
        "host_id",
        "fence",
        "team-document",
        "duplicate-recovery",
        "retired-recovery",
    ],
)
def test_recovery_admission_rejects_inexact_or_unbounded_history(
    recovery_store, supervised_admission, fault
):
    store, intent, binding, fence = recovery_store
    recovery_id = uuid4().hex
    store.reserve_supervised_generation(OWNER, supervised_admission.generation)
    store.activate_supervised_generation(OWNER, supervised_admission)
    store.begin_runtime_recovery(
        OWNER,
        intent.brain_session_id,
        TEAM,
        fence,
        recovery_id=recovery_id,
        runtime_id=binding.runtime_id,
        replacement_controller_id=uuid4().hex,
    )
    launch = store.get_launch(OWNER, TEAM)
    item = launch.supervised_generations[0]
    if fault in {"team_id", "brain_session_id", "controller_id", "epoch", "runtime_id", "host_id"}:
        generation = item.generation.model_copy(
            update={fault: 2 if fault == "epoch" else uuid4().hex}
        )
        item = item.model_copy(
            update={
                "generation": generation,
                "admission": supervised_admission.model_copy(update={"generation": generation}),
            }
        )
    elif fault == "unadmitted":
        item = item.model_copy(update={"admission": None})
    elif fault == "active":
        item = item.model_copy(update={"state": "active"})
    elif fault == "admission-mismatch":
        item = item.model_copy(
            update={
                "admission": supervised_admission.model_copy(
                    update={
                        "generation": item.generation.model_copy(
                            update={"runtime_id": uuid4().hex}
                        )
                    }
                )
            }
        )
    history = [] if fault == "missing" else [item]
    if fault in {"duplicate", "oversized"}:
        for _ in range(15 if fault == "oversized" else 1):
            generation = item.generation.model_copy(
                update={
                    "runner_id": uuid4().hex,
                    "generation_id": uuid4().hex,
                    "credential_id": uuid4().hex,
                    "launch_id": uuid4().hex,
                }
            )
            admission = supervised_admission.model_copy(
                update={
                    "generation": generation,
                    "domain": supervised_admission.domain.model_copy(
                        update={"path": "/" + "x" * 4000}
                    ),
                }
            )
            history.append(
                item.model_copy(update={"generation": generation, "admission": admission})
            )
    document = launch.model_copy(update={"supervised_generations": history}).model_dump_json()
    if fault == "padding":
        document += " " * 60_000
    if fault == "oversized":
        assert len(document.encode()) > 60_000
    with store._sessions("inject_inexact_recovery_admission_history") as db:
        row = db.get(SqlBgrLaunch, (current_workspace_id(), TEAM))
        row.document = "{" if fault == "corrupt" else document
        if fault == "fence":
            db.get(SqlBgrTeam, (current_workspace_id(), TEAM)).controller_id = uuid4().hex
        elif fault in {"team-document", "duplicate-recovery", "retired-recovery"}:
            team_row = db.get(SqlBgrTeam, (current_workspace_id(), TEAM))
            team = Team.model_validate_json(team_row.document)
            if fault == "team-document":
                team = team.model_copy(update={"team_id": uuid4().hex})
            else:
                extra = team.runtime_recoveries[-1]
                if fault == "retired-recovery":
                    extra = extra.model_copy(update={"recovery_id": uuid4().hex})
                team = team.model_copy(
                    update={"runtime_recoveries": [*team.runtime_recoveries, extra]}
                )
            team_row.document = team.model_dump_json()
    with pytest.raises((OmnigentError, ValueError)):
        store.recovery_admission(OWNER, TEAM, recovery_id)
    request = SupervisorStopRequest(
        request_id=uuid4().hex, recovery_id=recovery_id, admission=supervised_admission
    )
    result = supervisor_stopped(request, ())
    with pytest.raises((OmnigentError, ValueError)):
        store.complete_supervised_runtime_recovery(
            OWNER, intent.brain_session_id, TEAM, request=request, result=result
        )
    if fault in {"oversized", "padding"}:
        with pytest.raises(OmnigentError):
            store.generation_for_runner(supervised_admission.generation.runner_id)


def supervisor_stopped(request, worker_claims):
    admission = request.admission
    claims = (
        TerminalClaim(
            claim_id=admission.root_claim_id,
            admission_sequence=admission.admission_sequence,
            domain_id=admission.domain.domain_id,
            role="runner",
            session_id=admission.generation.brain_session_id,
            disposition="domain_stopped",
        ),
        *worker_claims,
    )
    last_sequence = max(claim.admission_sequence for claim in claims)
    manifest = ClosedGenerationManifest(
        admission=admission,
        closed_admission_sequence=last_sequence,
        journal_sequence=last_sequence + 1,
        claims=claims,
    )
    return SupervisorStopped(
        request_id=request.request_id,
        request_sha256=supervision_digest(request),
        receipt=GenerationStopReceipt(
            recovery_id=request.recovery_id,
            receipt_id=uuid4().hex,
            manifest=manifest,
            manifest_sha256=supervision_digest(manifest),
            proof="cgroup.kill+recursive-populated-zero",
        ),
    )


def with_stop_claims(result, claims):
    # Bypass construction to prove the store revalidates a tampered nested record.
    barrier = max(
        result.receipt.manifest.closed_admission_sequence,
        *(claim.admission_sequence for claim in claims),
    )
    manifest = result.receipt.manifest.model_copy(
        update={
            "claims": tuple(claims),
            "closed_admission_sequence": barrier,
            "journal_sequence": barrier + 1,
        }
    )
    receipt = result.receipt.model_copy(
        update={
            "manifest": manifest,
            "manifest_sha256": supervision_digest(manifest),
        }
    )
    return result.model_copy(update={"receipt": receipt})


@pytest.fixture
def supervised_stop(recovery_store, supervised_admission):
    store, intent, binding, fence = recovery_store
    brain = intent.brain_session_id
    with store._sessions("register_owner_before_supervised_admission") as db:
        db.add(SqlUser(workspace_id=current_workspace_id(), id=OWNER, account_generation="a" * 32))
    tasks = [
        store.reserve(OWNER, brain, TEAM, reservation(controller_id=fence.controller_id))
        for _ in range(2)
    ]
    store.bind_native(
        OWNER,
        brain,
        TEAM,
        fence,
        NativeBinding(
            team_id=TEAM,
            attempt_id=tasks[0].attempt.attempt_id,
            epoch=1,
            thread_id=str(uuid4()),
        ),
    )
    store.checkpoint(
        OWNER,
        tasks[0].attempt.session_id,
        TEAM,
        Checkpoint(
            attempt_id=tasks[0].attempt.attempt_id,
            epoch=1,
            phase="working",
            summary="Consumption unknown",
            head_sha=SHA,
        ),
    )
    review = store.reserve_review_round(
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
    job = review.jobs[0]
    store.activate_reviewer(
        OWNER,
        brain,
        TEAM,
        fence,
        round_id=review.round_id,
        review_id=job.review_id,
        binding=NativeBinding(
            team_id=TEAM, attempt_id=job.attempt_id, epoch=1, thread_id=str(uuid4())
        ),
        workspace=Workspace(
            host_id=HOST, repository="owner/repo", worktree="/snapshot/review", base_sha=SHA
        ),
    )
    store.reserve_supervised_generation(OWNER, supervised_admission.generation)
    store.activate_supervised_generation(OWNER, supervised_admission)
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
    request = SupervisorStopRequest(
        request_id=uuid4().hex, recovery_id=recovery_id, admission=supervised_admission
    )
    claims = [
        TerminalClaim(
            claim_id=uuid4().hex,
            admission_sequence=index + 2,
            domain_id=supervised_admission.domain.domain_id,
            role="reviewer" if index == 2 else "grunt",
            attempt_id=attempt_id,
            session_id=session_id,
            disposition="cancelled_before_exec" if session_id is None else "domain_stopped",
        )
        for index, (attempt_id, session_id) in enumerate(
            [
                (tasks[0].attempt.attempt_id, tasks[0].attempt.session_id),
                (tasks[1].attempt.attempt_id, None),
                (job.attempt_id, job.session_id),
            ]
        )
    ]
    return store, request, supervisor_stopped(request, claims), review, tasks


def test_supervised_completion_is_durable_exact_and_never_relaunches(supervised_stop, db_uri):
    store, request, result, review, tasks = supervised_stop
    brain = request.admission.generation.brain_session_id
    launch = store.get_launch(OWNER, TEAM)
    store = BgrReviewStore(db_uri)
    ready = store.complete_supervised_runtime_recovery(
        OWNER, brain, TEAM, request=request, result=result
    )
    recovery = ready.runtime_recoveries[-1]
    assert ready.epoch == 1 and ready.controller_id == recovery.previous_controller_id
    assert ready.lease_expires_at == 0
    assert recovery.supervised
    assert recovery.state == "stopped" and recovery.stop_generation == request.admission.generation
    assert recovery.stop_receipt_id == result.receipt.receipt_id
    assert recovery.stop_manifest_sha256 == result.receipt.manifest_sha256
    assert recovery.stop_receipt_sha256 == supervision_digest(result.receipt)
    assert recovery.stop_request_sha256 == supervision_digest(request)
    cold = BgrReviewStore(db_uri)
    assert (
        cold.complete_supervised_runtime_recovery(
            OWNER, brain, TEAM, request=request, result=result
        )
        == ready
    )
    assert cold.recovery_admission(OWNER, TEAM, request.recovery_id) == request.admission
    session, target_team, target = cold.runtime_recovery_target(OWNER, TEAM)
    assert session == brain and target_team == ready and target.runtime_id == recovery.runtime_id
    after = cold.get_launch(OWNER, TEAM)
    assert after.state == "blocked" and after.request == launch.request
    assert after.supervised_generations == launch.supervised_generations
    assert (
        cold.generation_for_runner(request.admission.generation.runner_id)
        == request.admission.generation
    )
    with pytest.raises(OmnigentError):
        cold.validate_supervised_generation(request.admission.generation)
    with pytest.raises(OmnigentError, match="dedicated"):
        cold.claim_launch(OWNER, TEAM, uuid4().hex)
    with pytest.raises(OmnigentError):
        cold.check_runtime_input(OWNER, brain, TEAM)
    with pytest.raises(OmnigentError):
        cold.acquire(
            OWNER, brain, TEAM, LeaseRequest(controller_id=recovery.previous_controller_id)
        )
    fence = Fence(controller_id=recovery.previous_controller_id, epoch=recovery.previous_epoch)
    with pytest.raises(OmnigentError):
        cold.native_brain_binding(OWNER, brain, TEAM, fence)
    with pytest.raises(OmnigentError):
        cold.native_brain_binding(
            OWNER,
            brain,
            TEAM,
            fence,
            binding=NativeBrainBinding(
                team_id=TEAM,
                epoch=recovery.previous_epoch,
                runtime_id=uuid4().hex,
                host_id=HOST,
                thread_id=str(uuid4()),
            ),
        )
    with cold._sessions("inspect_atomic_supervised_cleanup") as db:
        attempts = list(db.scalars(select(SqlBgrAttempt).where(SqlBgrAttempt.team_id == TEAM)))
        assert len(attempts) == 3
        assert all(
            '"state":"failed"' in row.document and row.active_writer_key is None
            for row in attempts
        )
        assert tasks[0].attempt.attempt_id in next(
            row.document for row in attempts if '"checkpoint":{' in row.document
        )
        envelopes = list(db.scalars(select(SqlBgrEnvelope).where(SqlBgrEnvelope.team_id == TEAM)))
        assert envelopes and all(
            row.discarded_at_us is not None and row.consumed_at_us is None for row in envelopes
        )
        assert all("unknown, not replayed" in row.document for row in envelopes)
        row = db.get(SqlBgrLaunch, (current_workspace_id(), TEAM))
        assert row.claim_id is None and row.claim_expires_at_us == 0
    completed = cold.get_review_round(OWNER, brain, TEAM, review.round_id)
    assert all(
        job.state == "sealed" and job.result.status == "incomplete" for job in completed.jobs
    )
    assert all(job.publication.state == "pending" for job in completed.jobs)


@pytest.mark.parametrize(
    "fault",
    [
        "root-missing",
        "root-cancelled",
        "root-attempt",
        "root-session",
        "extra-runner",
        "extra-brain",
        "bound-missing",
        "reserved-missing",
        "reviewer-missing",
        "wrong-role",
        "wrong-attempt",
        "foreign-attempt",
        "wrong-session",
        "null-bound-session",
        "cancelled-bound",
        "wrong-domain",
        "duplicate-attempt",
        "duplicate-claim",
        "duplicate-sequence",
        "foreign-helper",
        "helper-hides-worker",
    ],
)
def test_supervised_claim_coverage_fails_closed(supervised_stop, fault):
    store, request, result, _review, _tasks = supervised_stop
    brain = request.admission.generation.brain_session_id
    claims = list(result.receipt.manifest.claims)
    if fault.endswith("-missing"):
        claims.pop(
            {"root-missing": 0, "bound-missing": 1, "reserved-missing": 2, "reviewer-missing": 3}[
                fault
            ]
        )
    elif fault in {"root-cancelled", "root-attempt", "root-session"}:
        field, value = {
            "root-cancelled": ("disposition", "cancelled_before_exec"),
            "root-attempt": ("attempt_id", uuid4().hex),
            "root-session": ("session_id", uuid4().hex),
        }[fault]
        claims[0] = claims[0].model_copy(update={field: value})
    elif fault in {
        "duplicate-claim",
        "duplicate-sequence",
        "duplicate-attempt",
        "extra-runner",
        "extra-brain",
        "foreign-helper",
    }:
        extra = claims[1].model_copy(update={"claim_id": uuid4().hex, "admission_sequence": 5})
        changes = {
            "duplicate-claim": {"claim_id": claims[1].claim_id},
            "duplicate-sequence": {"admission_sequence": claims[1].admission_sequence},
            "extra-runner": {"role": "runner", "attempt_id": None, "session_id": brain},
            "extra-brain": {"role": "brain", "attempt_id": None, "session_id": brain},
            "foreign-helper": {"role": "helper", "attempt_id": uuid4().hex},
            "duplicate-attempt": {},
        }[fault]
        claims.append(extra.model_copy(update=changes))
    else:
        changes = {
            "wrong-role": {"role": "reviewer"},
            "wrong-attempt": {"attempt_id": claims[2].attempt_id},
            "foreign-attempt": {"attempt_id": uuid4().hex},
            "wrong-session": {"session_id": uuid4().hex},
            "null-bound-session": {"session_id": None},
            "cancelled-bound": {"disposition": "cancelled_before_exec"},
            "wrong-domain": {"domain_id": uuid4().hex},
            "helper-hides-worker": {"role": "helper"},
        }[fault]
        claims[1] = claims[1].model_copy(update=changes)
    result = with_stop_claims(result, claims)
    before = store.get_team(OWNER, brain, TEAM)
    with pytest.raises(OmnigentError):
        store.complete_supervised_runtime_recovery(
            OWNER, brain, TEAM, request=request, result=result
        )
    assert store.get_team(OWNER, brain, TEAM) == before
    assert store.get_launch(OWNER, TEAM).state == "blocked"
    with store._sessions("inspect_rejected_claim_rollback") as db:
        assert all(
            row.active_writer_key is not None
            for row in db.scalars(select(SqlBgrAttempt).where(SqlBgrAttempt.team_id == TEAM))
            if '"review_round_id":null' in row.document
        )
        assert all(
            row.discarded_at_us is None
            for row in db.scalars(select(SqlBgrEnvelope).where(SqlBgrEnvelope.team_id == TEAM))
        )


@pytest.mark.parametrize(
    "fault",
    [
        "receipt-id",
        "request-id",
        "manifest",
        "generation",
        "request-hash",
        "recovery-id",
        "manifest-hash",
    ],
)
@pytest.mark.parametrize("retry", [False, True])
def test_supervised_receipt_and_request_are_exact(supervised_stop, fault, retry):
    store, request, result, _review, _tasks = supervised_stop
    brain = request.admission.generation.brain_session_id
    if retry:
        store.complete_supervised_runtime_recovery(
            OWNER, brain, TEAM, request=request, result=result
        )
    if fault == "receipt-id":
        result = result.model_copy(
            update={"receipt": result.receipt.model_copy(update={"receipt_id": uuid4().hex})}
        )
    elif fault == "request-id":
        request = request.model_copy(update={"request_id": uuid4().hex})
        result = result.model_copy(
            update={
                "request_id": request.request_id,
                "request_sha256": supervision_digest(request),
            }
        )
    elif fault == "manifest":
        claims = list(result.receipt.manifest.claims)
        claims[2] = claims[2].model_copy(update={"disposition": "domain_stopped"})
        result = with_stop_claims(result, claims)
    elif fault == "generation":
        generation = request.admission.generation.model_copy(update={"generation_id": uuid4().hex})
        request = request.model_copy(
            update={"admission": request.admission.model_copy(update={"generation": generation})}
        )
        result = result.model_copy(update={"request_sha256": supervision_digest(request)})
        manifest = result.receipt.manifest.model_copy(update={"admission": request.admission})
        result = result.model_copy(
            update={
                "receipt": result.receipt.model_copy(
                    update={"manifest": manifest, "manifest_sha256": supervision_digest(manifest)}
                )
            }
        )
    elif fault == "request-hash":
        result = result.model_copy(update={"request_sha256": "f" * 64})
    else:
        changes = {
            "recovery-id": {"recovery_id": uuid4().hex},
            "manifest-hash": {"manifest_sha256": "f" * 64},
        }[fault]
        result = result.model_copy(update={"receipt": result.receipt.model_copy(update=changes)})
    if not retry and fault in {"receipt-id", "request-id", "manifest"}:
        # The first authenticated result establishes these previously unassigned identities.
        store.complete_supervised_runtime_recovery(
            OWNER, brain, TEAM, request=request, result=result
        )
    else:
        before = store.get_team(OWNER, brain, TEAM)
        with pytest.raises(OmnigentError):
            store.complete_supervised_runtime_recovery(
                OWNER, brain, TEAM, request=request, result=result
            )
        assert store.get_team(OWNER, brain, TEAM) == before


@pytest.mark.parametrize("different", [False, True])
def test_supervised_completion_serializes_competing_receipts(supervised_stop, db_uri, different):
    store, request, result, _review, _tasks = supervised_stop
    brain = request.admission.generation.brain_session_id
    other = (
        result
        if not different
        else result.model_copy(
            update={"receipt": result.receipt.model_copy(update={"receipt_id": uuid4().hex})}
        )
    )
    barrier = Barrier(2)

    def complete(value):
        cold = BgrReviewStore(db_uri)
        barrier.wait(timeout=10)
        try:
            return cold.complete_supervised_runtime_recovery(
                OWNER, brain, TEAM, request=request, result=value
            )
        except OmnigentError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(complete, [result, other]))
    assert sum(value is not None for value in outcomes) == (1 if different else 2)
    ready = store.get_team(OWNER, brain, TEAM)
    assert ready.epoch == 1 and ready.lease_expires_at == 0 and len(ready.runtime_recoveries) == 1
    assert ready.controller_id == request.admission.generation.controller_id
    assert ready.runtime_recoveries[-1].state == "stopped"
    assert ready.runtime_recoveries[-1].stop_receipt_id in {
        result.receipt.receipt_id,
        other.receipt.receipt_id,
    }
    assert store.get_launch(OWNER, TEAM).state == "blocked"


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("fault", ["deleted", "recreated", "controller", "epoch"])
def test_supervised_completion_rechecks_account_and_current_fence(supervised_stop, retry, fault):
    store, request, result, _review, _tasks = supervised_stop
    brain = request.admission.generation.brain_session_id
    if retry:
        store.complete_supervised_runtime_recovery(
            OWNER, brain, TEAM, request=request, result=result
        )
    with store._sessions("invalidate_authority_before_trusted_completion") as db:
        if fault in {"deleted", "recreated"}:
            row = db.get(SqlUser, (current_workspace_id(), OWNER))
            if fault == "deleted":
                row.deleted_at = 1
            else:
                row.account_generation = uuid4().hex
        else:
            row = db.get(SqlBgrTeam, (current_workspace_id(), TEAM))
            if fault == "controller":
                row.controller_id = uuid4().hex
            else:
                row.epoch += 1
    with pytest.raises(OmnigentError):
        store.complete_supervised_runtime_recovery(
            OWNER, brain, TEAM, request=request, result=result
        )


@pytest.mark.parametrize(
    "history", ["retained", "cleared", "deleted", "corrupt", "admission-missing"]
)
def test_legacy_stop_evidence_cannot_bypass_supervised_receipts(
    supervised_stop, recovery_store, history
):
    store, request, _result, _review, _tasks = supervised_stop
    _, intent, binding, _fence = recovery_store
    launch = store.get_launch(OWNER, TEAM)
    with store._sessions("remove_or_corrupt_supervised_launch_metadata") as db:
        row = db.get(SqlBgrLaunch, (current_workspace_id(), TEAM))
        if history == "deleted":
            db.delete(row)
        elif history == "corrupt":
            row.document = "{"
        elif history in {"cleared", "admission-missing"}:
            generations = (
                []
                if history == "cleared"
                else [launch.supervised_generations[0].model_copy(update={"admission": None})]
            )
            row.document = launch.model_copy(
                update={"supervised_generations": generations}
            ).model_dump_json()
    with pytest.raises(OmnigentError, match="trusted supervisor"):
        store.complete_runtime_recovery(
            OWNER,
            intent.brain_session_id,
            TEAM,
            recovery_id=request.recovery_id,
            evidence=RuntimeStopEvidence(
                runtime_id=binding.runtime_id,
                host_id=HOST,
                inventory_complete=True,
                processes=[stopped(intent.brain_session_id, binding.thread_id)],
            ),
        )


@pytest.mark.parametrize("session", [False, True])
@pytest.mark.parametrize("disposition", ["cancelled_before_exec", "domain_stopped"])
def test_truly_unbound_attempts_and_helpers_have_explicit_terminal_claims(
    supervised_stop, session, disposition
):
    store, request, result, _review, tasks = supervised_stop
    brain = request.admission.generation.brain_session_id
    claims = list(result.receipt.manifest.claims)
    claims[2] = claims[2].model_copy(
        update={
            "session_id": tasks[1].attempt.session_id if session else None,
            "disposition": disposition,
        }
    )
    claims.append(
        TerminalClaim(
            claim_id=uuid4().hex,
            admission_sequence=5,
            domain_id=request.admission.domain.domain_id,
            role="helper",
            disposition=disposition,
        )
    )
    result = with_stop_claims(result, claims)
    completed = store.complete_supervised_runtime_recovery(
        OWNER, brain, TEAM, request=request, result=result
    )
    assert completed.runtime_recoveries[-1].state == "stopped" and completed.epoch == 1


@pytest.mark.parametrize("claim_pending", [False, True])
def test_never_started_review_allocations_are_not_foreign_claims(
    recovery_store, supervised_admission, claim_pending
):
    store, intent, binding, fence = recovery_store
    brain = intent.brain_session_id
    review = store.reserve_review_round(
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
    store.reserve_supervised_generation(OWNER, supervised_admission.generation)
    store.activate_supervised_generation(OWNER, supervised_admission)
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
    request = SupervisorStopRequest(
        request_id=recovery_id, recovery_id=recovery_id, admission=supervised_admission
    )
    claims = (
        ()
        if not claim_pending
        else (
            TerminalClaim(
                claim_id=uuid4().hex,
                admission_sequence=2,
                domain_id=supervised_admission.domain.domain_id,
                role="reviewer",
                attempt_id=review.jobs[0].attempt_id,
                disposition="cancelled_before_exec",
            ),
        )
    )
    result = supervisor_stopped(request, claims)
    completed = store.complete_supervised_runtime_recovery(
        OWNER, brain, TEAM, request=request, result=result
    )
    assert completed.runtime_recoveries[-1].state == "stopped"
    assert (
        store.get_review_round(OWNER, brain, TEAM, review.round_id).jobs[0].result.status
        == "incomplete"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "native-attempt",
        "native-team",
        "native-epoch",
        "native-thread",
        "native-sql-thread",
        "brain-missing",
        "brain-runtime",
        "brain-thread",
        "attempt-id",
        "attempt-team",
        "attempt-task",
        "attempt-session",
        "attempt-epoch",
        "attempt-host",
        "agent-role",
        "agent-session",
        "agent-missing",
    ],
)
def test_supervised_completion_requires_exact_durable_worker_identity(supervised_stop, fault):
    store, request, result, _review, tasks = supervised_stop
    brain = request.admission.generation.brain_session_id
    attempt = tasks[0].attempt
    before = store.get_team(OWNER, brain, TEAM)
    with store._sessions("inject_inexact_old_generation_worker_identity") as db:
        if fault.startswith("brain-"):
            row = db.get(SqlBgrNativeBinding, (current_workspace_id(), before.brain_agent_id))
        elif fault.startswith("native-"):
            row = db.get(SqlBgrNativeBinding, (current_workspace_id(), attempt.attempt_id))
        elif fault.startswith("agent-"):
            row = db.get(SqlBgrAgent, (current_workspace_id(), attempt.agent_id))
        else:
            row = db.get(SqlBgrAttempt, (current_workspace_id(), attempt.attempt_id))
        if fault.endswith("-missing"):
            db.delete(row)
        elif fault == "native-sql-thread":
            row.thread_id = uuid4().hex
        else:
            document = json.loads(row.document)
            field, value = {
                "native-attempt": ("attempt_id", uuid4().hex),
                "native-team": ("team_id", uuid4().hex),
                "native-epoch": ("epoch", 2),
                "native-thread": ("thread_id", str(uuid4())),
                "brain-runtime": ("runtime_id", uuid4().hex),
                "brain-thread": ("thread_id", str(uuid4())),
                "attempt-id": ("attempt_id", uuid4().hex),
                "attempt-team": ("team_id", uuid4().hex),
                "attempt-task": ("task_id", uuid4().hex),
                "attempt-session": ("session_id", uuid4().hex),
                "attempt-epoch": ("epoch", 2),
                "attempt-host": (
                    "workspace",
                    {**document.get("workspace", {}), "host_id": "other-host"},
                ),
                "agent-role": ("role", "reviewer"),
                "agent-session": ("session_id", uuid4().hex),
            }[fault]
            document[field] = value
            row.document = json.dumps(document)
    with pytest.raises(OmnigentError):
        store.complete_supervised_runtime_recovery(
            OWNER, brain, TEAM, request=request, result=result
        )
    assert store.get_team(OWNER, brain, TEAM) == before


def test_supervised_cleanup_rolls_back_all_reconciliation_on_failure(supervised_stop, monkeypatch):
    store, request, result, review, _tasks = supervised_stop
    brain = request.admission.generation.brain_session_id
    before = store.get_team(OWNER, brain, TEAM)

    def fail_after_attempts_and_reviews():
        raise OmnigentError("Injected reconciliation failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_now_us", fail_after_attempts_and_reviews)
        with pytest.raises(OmnigentError, match="Injected"):
            store.complete_supervised_runtime_recovery(
                OWNER, brain, TEAM, request=request, result=result
            )
    assert store.get_team(OWNER, brain, TEAM) == before
    assert store.get_review_round(OWNER, brain, TEAM, review.round_id).jobs[0].state == "running"
    with store._sessions("verify_transaction_rollback_after_partial_reconciliation") as db:
        assert all(
            '"state":"reserved"' in row.document
            for row in db.scalars(select(SqlBgrAttempt).where(SqlBgrAttempt.team_id == TEAM))
        )
        assert all(
            row.discarded_at_us is None
            for row in db.scalars(select(SqlBgrEnvelope).where(SqlBgrEnvelope.team_id == TEAM))
        )
    assert (
        store.complete_supervised_runtime_recovery(
            OWNER, brain, TEAM, request=request, result=result
        )
        .runtime_recoveries[-1]
        .state
        == "stopped"
    )


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


@pytest.fixture
def replacement_recovery(recovery_store):
    store, intent, binding, fence = recovery_store
    pending = store.begin_runtime_recovery(
        OWNER,
        intent.brain_session_id,
        TEAM,
        fence,
        recovery_id=uuid4().hex,
        runtime_id=binding.runtime_id,
        replacement_controller_id=uuid4().hex,
    )
    team = store.complete_runtime_recovery(
        OWNER,
        intent.brain_session_id,
        TEAM,
        recovery_id=pending.recovery_id,
        evidence=RuntimeStopEvidence(
            runtime_id=binding.runtime_id,
            host_id=HOST,
            inventory_complete=True,
            processes=[stopped(intent.brain_session_id, binding.thread_id)],
        ),
    )
    replacement = binding.model_copy(
        update={
            "epoch": team.epoch,
            "runtime_id": uuid4().hex,
            "thread_id": str(uuid4()),
        }
    )
    return store, intent, Fence(controller_id=team.controller_id, epoch=team.epoch), replacement


def test_recovery_resume_requires_exact_durable_binding_not_ready_or_new_generation(
    replacement_recovery, monkeypatch
):
    store, intent, fence, binding = replacement_recovery
    brain = intent.brain_session_id
    with pytest.raises(OmnigentError, match="exact live"):
        store.resume_live_runtime_recovery(OWNER, brain, TEAM, fence, binding=binding)
    store.native_brain_binding(OWNER, brain, TEAM, fence, binding=binding)
    expired = store._now_us() + 301_000_000
    monkeypatch.setattr(store, "_now_us", lambda: expired)
    for owner, session, current_fence, target in (
        ("outsider", brain, fence, binding),
        (OWNER, uuid4().hex, fence, binding),
        (OWNER, brain, fence.model_copy(update={"controller_id": uuid4().hex}), binding),
        (OWNER, brain, fence.model_copy(update={"epoch": 1}), binding),
        (OWNER, brain, fence, binding.model_copy(update={"runtime_id": uuid4().hex})),
        (OWNER, brain, fence, binding.model_copy(update={"thread_id": str(uuid4())})),
    ):
        with pytest.raises(OmnigentError):
            store.resume_live_runtime_recovery(owner, session, TEAM, current_fence, binding=target)
    with pytest.raises(OmnigentError):
        store.acquire(OWNER, brain, TEAM, LeaseRequest(controller_id=fence.controller_id))
    resumed = store.resume_live_runtime_recovery(OWNER, brain, TEAM, fence, binding=binding)
    assert resumed.epoch == fence.epoch and resumed.lease_expires_at == expired / 1_000_000 + 300
    assert resumed.runtime_recoveries[-1].state == "binding"
    with pytest.raises(OmnigentError, match="not attached"):
        store.check_runtime_input(OWNER, brain, TEAM)
    store.mark_launch_running(OWNER, brain, TEAM, fence)
    with pytest.raises(OmnigentError, match="exact live"):
        store.resume_live_runtime_recovery(OWNER, brain, TEAM, fence, binding=binding)


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


def test_owner_recovery_target_uses_durable_binding_after_lease_expiry(
    recovery_store, monkeypatch
):
    store, intent, binding, fence = recovery_store
    monkeypatch.setattr(store, "_now_us", lambda: 10**18)
    brain, team, target = store.runtime_recovery_target(OWNER, TEAM)
    assert brain == intent.brain_session_id and target == binding
    assert team.epoch == fence.epoch and team.controller_id == fence.controller_id
    with pytest.raises(OmnigentError):
        store.runtime_recovery_target("other-owner", TEAM)


@pytest.mark.parametrize("state", ["ready", "binding", "attached"])
@pytest.mark.parametrize("cold", [False, True])
def test_recovery_target_resolves_exact_generation_across_replacement(
    replacement_recovery, db_uri, monkeypatch, state, cold
):
    store, intent, fence, replacement = replacement_recovery
    brain = intent.brain_session_id
    if state != "ready":
        store.native_brain_binding(OWNER, brain, TEAM, fence, binding=replacement)
    if state == "attached":
        store.mark_launch_running(OWNER, brain, TEAM, fence)
    if cold:
        store = BgrReviewStore(db_uri)
    monkeypatch.setattr(store, "_now_us", lambda: 10**18)
    session, team, target = store.runtime_recovery_target(OWNER, TEAM)
    recovery = team.runtime_recoveries[-1]
    assert session == brain and recovery.state == state
    assert team.epoch == fence.epoch and team.controller_id == fence.controller_id
    if state == "ready":
        assert target == NativeBrainBinding(
            team_id=TEAM,
            epoch=recovery.previous_epoch,
            thread_id=recovery.previous_thread_id,
            runtime_id=recovery.runtime_id,
            host_id=recovery.host_id,
        )
    else:
        assert target == replacement
    with pytest.raises(OmnigentError):
        store.runtime_recovery_target("other-owner", TEAM)
    if state != "attached":
        with pytest.raises(OmnigentError, match="launch retry"):
            store.claim_launch(OWNER, TEAM, uuid4().hex)
    with pytest.raises(OmnigentError):
        store.check_session_runtime_input(brain)


@pytest.mark.parametrize(
    "field,value",
    [
        ("team_id", "e" * 32),
        ("epoch", 2),
        ("thread_id", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        ("runtime_id", "e" * 32),
        ("host_id", "other-host"),
        ("team_epoch", 3),
        ("team_controller", "e" * 32),
        ("missing_history", None),
        ("missing_digest", None),
    ],
)
def test_ready_recovery_target_rejects_inconsistent_durable_identity(
    replacement_recovery, db_uri, field, value
):
    store, intent, _fence, _replacement = replacement_recovery
    _brain, team, binding = store.runtime_recovery_target(OWNER, TEAM)
    with store._sessions("fixture_inconsistent_recovery_identity") as db:
        if field in {"team_epoch", "team_controller", "missing_history", "missing_digest"}:
            row = db.get(SqlBgrTeam, (current_workspace_id(), TEAM))
            if field == "team_epoch":
                row.epoch = value
            elif field == "team_controller":
                row.controller_id = value
            else:
                history = (
                    []
                    if field == "missing_history"
                    else [
                        team.runtime_recoveries[-1].model_copy(
                            update={"stop_manifest_sha256": None}
                        )
                    ]
                )
                row.document = team.model_copy(
                    update={"runtime_recoveries": history}
                ).model_dump_json()
        else:
            row = db.get(SqlBgrNativeBinding, (current_workspace_id(), team.brain_agent_id))
            row.document = binding.model_copy(update={field: value}).model_dump_json()
    cold = BgrReviewStore(db_uri)
    with pytest.raises(OmnigentError):
        cold.runtime_recovery_target(OWNER, TEAM)
    assert cold.get_launch(OWNER, TEAM).request.initial_prompt == intent.request.initial_prompt
