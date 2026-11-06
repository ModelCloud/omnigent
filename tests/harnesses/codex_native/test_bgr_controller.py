"""Server authority and durable actions over the existing native mailbox boundary."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnigent.bgr import Attempt, LaunchIntent, TaskClose, Team
from omnigent.bgr_control import BgrControl
from omnigent.bgr_review import ReviewEvaluation, ReviewRound
from omnigent.db import current_workspace_id
from omnigent.db.bgr_models import (
    SqlBgrAttempt,
    SqlBgrLaunch,
    SqlBgrNativeBinding,
    SqlBgrReviewRound,
    SqlBgrTeam,
)
from omnigent.errors import OmnigentError
from omnigent.harnesses.codex_native.bgr import NativeCapabilities
from omnigent.harnesses.codex_native.bgr_messages import (
    NativeBgrConnection,
    NativeEnvelope,
    NativeIdentity,
)
from omnigent.server.bgr_controller import BgrControllers, BgrHostRuntime
from tests.harnesses.codex_native.test_bgr_lifecycle import CAPTURED
from tests.harnesses.codex_native.test_bgr_messages import NativeProcess
from tests.harnesses.codex_native.test_bgr_messages import route as route
from tests.stores.test_bgr_recovery import recovery_store as recovery_store
from tests.stores.test_bgr_recovery import replacement_recovery as replacement_recovery
from tests.stores.test_bgr_recovery import supervised_admission as supervised_admission
from tests.stores.test_bgr_review_store import message as review_message
from tests.stores.test_bgr_store import BRAIN, CONTRACT, HOST, OWNER, SHA, TEAM, reservation


@pytest.fixture
async def controller(route):
    mailbox, task, brain, worker = route
    processes = {BRAIN: brain, task.attempt.session_id: worker}
    calls = []
    role_sessions = AsyncMock()
    runtime_id = mailbox.store.native_brain_binding(OWNER, BRAIN, TEAM, mailbox.fence).runtime_id

    async def host_call(operation, parameters):
        calls.append((operation, parameters))
        if operation == "brain":
            return {
                "thread_id": brain.connection.thread_id,
                "runtime_id": runtime_id,
                "host_id": HOST,
            }
        if operation == "workspace":
            return {
                "workspace": {
                    "host_id": HOST,
                    "repository": "repo",
                    "worktree": "/trusted/" + parameters["task_id"],
                    "base_sha": SHA,
                }
            }
        if operation == "stop":
            return {"stop_confirmed": True}
        session = parameters["session_id"]
        if operation == "spawn":
            processes[session] = NativeProcess()
            return {
                "thread_id": str(uuid4()),
                "repository_path": "/trusted/repo",
                "host_id": HOST,
                "options": {
                    "model": "model",
                    "model_provider": "provider",
                    "reasoning_effort": "high",
                },
            }
        assert operation == "rpc"
        method, params = parameters["method"], parameters["params"]
        process = processes[session]
        if method == "bgr/capabilities/read":
            return {"result": CAPTURED}
        if method == "bgr/thread/bind":
            assert params.keys() == {"threadId", "identity"}
        if method in {"bgr/thread/bind", "bgr/reviewer/start"}:
            thread_id = params.get("threadId") or str(uuid4())
            process.connection = NativeBgrConnection(
                thread_id,
                NativeIdentity.model_validate(params["identity"]),
                process.request,
                NativeCapabilities.model_validate(CAPTURED),
            )
            result = (await process.request("bgr/thread/inspect", {"threadId": thread_id}))[
                "result"
            ]
            if method == "bgr/reviewer/start":
                result.update(
                    threadId=thread_id,
                    review={
                        "baseSha": params["review"]["baseSha"],
                        "headSha": params["review"]["headSha"],
                        "changedFiles": ["review.rs"],
                        "coverageLimits": [],
                    },
                )
            return {"result": result}
        if method == "thread/read":
            assert params["includeTurns"] is False
            return {
                "result": {
                    "thread": {"id": process.connection.thread_id, "cwd": "/snapshot/" + session}
                }
            }
        return await process.request(method, params)

    registry = BgrControllers(mailbox.store, role_sessions)
    current = await registry.attach(
        user_id=OWNER,
        brain_session_id=BRAIN,
        team_id=TEAM,
        fence=mailbox.fence,
        runtime=BgrHostRuntime(BRAIN, host_call),
    )
    try:
        yield current, processes, calls, role_sessions
    finally:
        await registry.close()


async def test_same_live_attach_inspects_owner_and_rejects_changed_generation(controller):
    current, processes, _calls, role_sessions = controller
    registry = BgrControllers(current.store, role_sessions)
    registry._controllers[BRAIN] = current
    before = len(processes[BRAIN].calls)
    assert (
        await registry.attach(
            user_id=OWNER,
            brain_session_id=BRAIN,
            team_id=TEAM,
            fence=current.fence,
            runtime=current.runtime,
        )
        is current
    )
    assert len(processes[BRAIN].calls) > before
    await current.renew_lease()

    async def changed(operation, parameters):
        metadata = await current.runtime.call(operation, parameters)
        return {**metadata, "runtime_id": uuid4().hex}

    with pytest.raises(OmnigentError, match="generation changed"):
        await registry.attach(
            user_id=OWNER,
            brain_session_id=BRAIN,
            team_id=TEAM,
            fence=current.fence,
            runtime=BgrHostRuntime(BRAIN, changed),
        )


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("expired", [False, True])
async def test_same_live_restore_preserves_grunt_mail_peers_and_adjudication(
    controller, monkeypatch, cached, expired
):
    current, processes, calls, role_sessions = controller
    worker = next(
        process for process in processes.values() if process.connection.identity.role == "grunt"
    )
    identity = worker.connection.identity
    anchor = current.store.drain(OWNER, identity.session_id, TEAM)[0]
    await current.mailbox.forward(
        current.brain, current.connections[identity.session_id], anchor.message_id
    )
    previous_lease = current.store.get_team(OWNER, BRAIN, TEAM).lease_expires_at
    if expired:
        monkeypatch.setattr(current.store, "_clock", lambda: previous_lease + 1)
    registry = BgrControllers(current.store, role_sessions)
    if cached:
        registry._controllers[BRAIN] = current
    calls.clear()
    try:
        restored = await registry.attach(
            user_id=OWNER,
            brain_session_id=BRAIN,
            team_id=TEAM,
            fence=current.fence,
            runtime=current.runtime,
            existing_only=True,
        )
        assert set(restored.connections) == set(processes)
        assert restored.fence == current.fence
        assert all(operation in {"brain", "rpc"} for operation, _ in calls)
        assert {params["method"] for operation, params in calls if operation == "rpc"} <= {
            "bgr/capabilities/read",
            "bgr/thread/inspect",
        }
        role_sessions.assert_not_awaited()
        assert list(worker.pending) == [anchor.message_id]
        callback = worker.emit(restored.brain, anchor, "surviving-question", "question")
        await restored.native_callback(
            session_id=identity.session_id,
            thread_id=worker.connection.thread_id,
            method="bgr/message/emitted",
            params=callback,
        )
        question = restored.store.drain(OWNER, BRAIN, TEAM)[0]
        assert question.consumed_at is None
        reply = processes[BRAIN].emit(
            restored.connections[identity.session_id],
            NativeEnvelope.model_validate(callback["envelope"]),
            "surviving-answer",
            "answer",
        )
        await restored.native_callback(
            session_id=BRAIN,
            thread_id=restored.brain.thread_id,
            method="bgr/message/emitted",
            params=reply,
        )
        assert len(worker.pending) == 2
        assert all(
            item.consumed_at is None
            for item in restored.store.drain(OWNER, identity.session_id, TEAM)
        )
        await restored.execute(
            OWNER,
            BRAIN,
            BgrControl(
                action="create_grunt", action_id=uuid4().hex, contract=CONTRACT, base_sha=SHA
            ),
        )
        peers = [
            parameters
            for method, parameters in processes[BRAIN].calls
            if method == "bgr/thread/peers"
        ][-1]["peers"]
        assert identity.wire() in peers and len(peers) == 2
        stopped = await restored.execute(
            OWNER,
            BRAIN,
            BgrControl(
                action="adjudicate",
                action_id=uuid4().hex,
                attempt_id=identity.attempt_id,
                outcome="completed",
            ),
        )
        assert stopped["attempt"]["state"] == "completed"
        assert identity.session_id not in restored.connections
        after = restored.store.get_team(OWNER, BRAIN, TEAM)
        assert after.epoch == current.fence.epoch and after.lease_expires_at > previous_lease
        with pytest.raises(OmnigentError):
            await restored.native_callback(
                session_id=identity.session_id,
                thread_id=worker.connection.thread_id,
                method="bgr/message/emitted",
                params=callback,
            )
    finally:
        await registry.close()


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize(
    "loss", ["runtime", "brain_identity", "worker_identity", "brain_cancelled", "worker_cancelled"]
)
async def test_same_live_restore_rejects_lost_native_state_without_lease_or_rebind(
    controller, monkeypatch, cached, expired, loss
):
    current, _processes, calls, role_sessions = controller
    previous = current.store.get_team(OWNER, BRAIN, TEAM)
    if expired:
        monkeypatch.setattr(current.store, "_clock", lambda: previous.lease_expires_at + 1)
    original_call = current.runtime.call

    async def damaged(operation, parameters):
        result = await original_call(operation, parameters)
        if operation == "brain" and loss == "runtime":
            result["runtime_id"] = uuid4().hex
        if operation == "rpc" and parameters["method"] == "bgr/thread/inspect":
            if loss.startswith("brain_") == (parameters["session_id"] == BRAIN):
                snapshot = result["result"]["snapshot"]
                if loss.endswith("cancelled"):
                    snapshot["cancelled"] = True
                elif loss.endswith("identity"):
                    snapshot["identity"]["agentId"] = uuid4().hex
        return result

    monkeypatch.setattr(current.runtime, "call", damaged)
    registry = BgrControllers(current.store, role_sessions)
    if cached:
        registry._controllers[BRAIN] = current
    calls.clear()
    try:
        with pytest.raises(OmnigentError):
            await registry.attach(
                user_id=OWNER,
                brain_session_id=BRAIN,
                team_id=TEAM,
                fence=current.fence,
                runtime=current.runtime,
                existing_only=True,
            )
        with pytest.raises(OmnigentError):
            registry.get(BRAIN)
        after = current.store.get_team(OWNER, BRAIN, TEAM)
        assert (after.epoch, after.controller_id, after.lease_expires_at) == (
            previous.epoch,
            previous.controller_id,
            previous.lease_expires_at,
        )
        assert all(operation in {"brain", "rpc"} for operation, _ in calls)
        assert {params["method"] for operation, params in calls if operation == "rpc"} <= {
            "bgr/capabilities/read",
            "bgr/thread/inspect",
        }
    finally:
        await registry.close()


@pytest.mark.parametrize("mutation", ["attempt", "controller", "recovery"])
async def test_same_live_renewal_rechecks_inventory_and_fence_after_native_awaits(
    controller, monkeypatch, mutation
):
    current, processes, _, role_sessions = controller
    previous = current.store.get_team(OWNER, BRAIN, TEAM)
    monkeypatch.setattr(current.store, "_clock", lambda: previous.lease_expires_at + 1)
    original_call = current.runtime.call
    changed = False

    async def concurrent_change(operation, parameters):
        nonlocal changed
        result = await original_call(operation, parameters)
        if (
            not changed
            and operation == "rpc"
            and parameters["session_id"] != BRAIN
            and parameters["method"] == "bgr/thread/inspect"
        ):
            changed = True
            if mutation == "recovery":
                _, binding = current.store.live_controller_target(OWNER, BRAIN)
                current.store.begin_runtime_recovery(
                    OWNER,
                    BRAIN,
                    TEAM,
                    current.fence,
                    recovery_id=uuid4().hex,
                    runtime_id=binding.runtime_id,
                    replacement_controller_id=uuid4().hex,
                )
            else:
                with current.store._sessions("change_authority_during_native_inspection") as db:
                    if mutation == "controller":
                        db.get(
                            SqlBgrTeam, (current_workspace_id(), TEAM)
                        ).controller_id = uuid4().hex
                    else:
                        identity = processes[parameters["session_id"]].connection.identity
                        row = db.get(SqlBgrAttempt, (current_workspace_id(), identity.attempt_id))
                        row.document = (
                            Attempt.model_validate_json(row.document)
                            .model_copy(update={"state": "completed"})
                            .model_dump_json()
                        )
                        row.active_writer_key = None
        return result

    monkeypatch.setattr(current.runtime, "call", concurrent_change)
    registry = BgrControllers(current.store, role_sessions)
    try:
        with pytest.raises(OmnigentError):
            await registry.attach(
                user_id=OWNER,
                brain_session_id=BRAIN,
                team_id=TEAM,
                fence=current.fence,
                runtime=current.runtime,
                existing_only=True,
            )
        assert changed
        with pytest.raises(OmnigentError):
            registry.get(BRAIN)
        after = current.store.get_team(OWNER, BRAIN, TEAM)
        assert (
            after.epoch == previous.epoch and after.lease_expires_at <= previous.lease_expires_at
        )
    finally:
        await registry.close()


@pytest.mark.parametrize("callback_before_restart", [True, False])
async def test_cold_restore_keeps_sealed_reviewer_for_sibling_barrier(
    controller, callback_before_restart
):
    current, processes, calls, role_sessions = controller
    created = await current.execute(
        OWNER,
        BRAIN,
        BgrControl(
            action="start_review",
            action_id=uuid4().hex,
            repository="ModelCloud/omnigent",
            pr_number=1,
            base_sha=SHA,
            head_sha="e" * 40,
        ),
    )
    round = current.store.get_review_round(OWNER, BRAIN, TEAM, created["round_id"])
    first, second = round.jobs
    source = processes[first.session_id]
    first_anchor = NativeEnvelope.model_validate(next(iter(source.pending.values())))
    first_callback = source.emit(
        current.brain,
        first_anchor,
        "sealed-before-disconnect",
        "result",
        payload={"outcome": "no_findings", "findings": [], "coverageLimits": []},
    )
    if callback_before_restart:
        await current.native_callback(
            session_id=first.session_id,
            thread_id=source.connection.thread_id,
            method="bgr/message/emitted",
            params=first_callback,
        )
    else:
        source = processes[second.session_id]
        source.emit(
            current.brain,
            NativeEnvelope.model_validate(next(iter(source.pending.values()))),
            "second-result-without-callback",
            "result",
            payload={"outcome": "no_findings", "findings": [], "coverageLimits": []},
        )
    assert not processes[BRAIN].pending
    registry = BgrControllers(current.store, role_sessions)
    calls.clear()
    try:
        restored = await registry.attach(
            user_id=OWNER,
            brain_session_id=BRAIN,
            team_id=TEAM,
            fence=current.fence,
            runtime=current.runtime,
            existing_only=True,
        )
        assert {first.session_id, second.session_id} <= restored.connections.keys()
        assert all(operation in {"brain", "rpc"} for operation, _ in calls)
        assert {params["method"] for operation, params in calls if operation == "rpc"} <= {
            "bgr/capabilities/read",
            "bgr/thread/inspect",
            "thread/read",
        }
        status = await restored.execute(
            OWNER,
            BRAIN,
            BgrControl(action="status", action_id=uuid4().hex, round_id=round.round_id),
        )
        if not callback_before_restart:
            assert status["results_ready"] is True
            assert len(processes[BRAIN].pending) == 2
            assert len(restored.store.drain(OWNER, BRAIN, TEAM)) == 2
            assert all(
                item.consumed_at is None for item in restored.store.drain(OWNER, BRAIN, TEAM)
            )
            return
        assert status["results_ready"] is False and all(
            job["result"] is None for job in status["jobs"]
        )
        assert not processes[BRAIN].pending
        source = processes[second.session_id]
        anchor = NativeEnvelope.model_validate(next(iter(source.pending.values())))
        callback = source.emit(
            restored.brain,
            anchor,
            "sealed-after-reconnect",
            "result",
            payload={"outcome": "incomplete", "findings": [], "coverageLimits": ["transport gap"]},
        )
        await restored.native_callback(
            session_id=second.session_id,
            thread_id=source.connection.thread_id,
            method="bgr/message/emitted",
            params=callback,
        )
        status = await restored.execute(
            OWNER,
            BRAIN,
            BgrControl(action="status", action_id=uuid4().hex, round_id=round.round_id),
        )
        assert status["results_ready"] is True
        assert len(processes[BRAIN].pending) == 2
        assert all(item.consumed_at is None for item in restored.store.drain(OWNER, BRAIN, TEAM))
    finally:
        await registry.close()


async def test_cold_restore_does_not_revive_terminal_or_launch_planned_roles(controller):
    current, processes, calls, role_sessions = controller
    worker = next(
        process for process in processes.values() if process.connection.identity.role == "grunt"
    )
    identity = worker.connection.identity
    anchor = current.store.drain(OWNER, identity.session_id, TEAM)[0]
    await current.mailbox.forward(
        current.brain, current.connections[identity.session_id], anchor.message_id
    )
    current.store.close(
        OWNER,
        BRAIN,
        TEAM,
        TaskClose(
            **current.fence.model_dump(),
            attempt_id=identity.attempt_id,
            outcome="completed",
            stop_confirmed=True,
        ),
    )
    planned = current.store.reserve(OWNER, BRAIN, TEAM, reservation())
    registry = BgrControllers(current.store, role_sessions)
    calls.clear()
    try:
        restored = await registry.attach(
            user_id=OWNER,
            brain_session_id=BRAIN,
            team_id=TEAM,
            fence=current.fence,
            runtime=current.runtime,
            existing_only=True,
        )
        assert set(restored.connections) == {BRAIN}
        await restored.execute(OWNER, BRAIN, BgrControl(action="status", action_id=uuid4().hex))
        assert planned.attempt.session_id not in processes
        assert all(operation in {"brain", "rpc"} for operation, _ in calls)
        assert all(parameters.get("session_id", BRAIN) == BRAIN for _, parameters in calls)
        assert not any(
            parameters.get("method") in {"bgr/thread/bind", "bgr/message/deliver"}
            for _, parameters in calls
        )
        role_sessions.assert_not_awaited()
    finally:
        await registry.close()


@pytest.mark.parametrize("loss", ["snapshot", "profile", "head"])
async def test_cold_reviewer_restore_rejects_changed_immutable_metadata(
    controller, monkeypatch, loss
):
    current, _processes, calls, role_sessions = controller
    created = await current.execute(
        OWNER,
        BRAIN,
        BgrControl(
            action="start_review",
            action_id=uuid4().hex,
            repository="ModelCloud/omnigent",
            pr_number=1,
            base_sha=SHA,
            head_sha="e" * 40,
        ),
    )
    round = current.store.get_review_round(OWNER, BRAIN, TEAM, created["round_id"])
    job = round.jobs[0]
    previous = current.store.get_team(OWNER, BRAIN, TEAM)
    monkeypatch.setattr(current.store, "_clock", lambda: previous.lease_expires_at + 1)
    original_call = current.runtime.call

    async def changed(operation, parameters):
        result = await original_call(operation, parameters)
        if (
            operation == "rpc"
            and parameters["session_id"] == job.session_id
            and parameters["method"] == "thread/read"
        ):
            if loss == "snapshot":
                result["result"]["thread"]["cwd"] = "/foreign/snapshot"
            else:
                with current.store._sessions("change_reviewer_metadata_during_inspection") as db:
                    if loss == "profile":
                        row = db.get(SqlBgrAttempt, (current_workspace_id(), job.attempt_id))
                        attempt = Attempt.model_validate_json(row.document)
                        row.document = attempt.model_copy(
                            update={
                                "profile": attempt.profile.model_copy(
                                    update={"model": "foreign-model"}
                                )
                            }
                        ).model_dump_json()
                    else:
                        row = db.get(SqlBgrReviewRound, (current_workspace_id(), round.round_id))
                        row.document = (
                            ReviewRound.model_validate_json(row.document)
                            .model_copy(update={"head_sha": "f" * 40})
                            .model_dump_json()
                        )
        return result

    monkeypatch.setattr(current.runtime, "call", changed)
    registry = BgrControllers(current.store, role_sessions)
    calls.clear()
    try:
        with pytest.raises(OmnigentError):
            await registry.attach(
                user_id=OWNER,
                brain_session_id=BRAIN,
                team_id=TEAM,
                fence=current.fence,
                runtime=current.runtime,
                existing_only=True,
            )
        assert (
            current.store.get_team(OWNER, BRAIN, TEAM).lease_expires_at
            == previous.lease_expires_at
        )
        with pytest.raises(OmnigentError):
            registry.get(BRAIN)
        assert not any(operation != "brain" and operation != "rpc" for operation, _ in calls)
        assert not any(
            parameters.get("method") in {"bgr/thread/bind", "bgr/message/deliver"}
            for _, parameters in calls
        )
    finally:
        await registry.close()


async def test_first_brain_reservation_binds_once_but_existing_only_attach_never_does(controller):
    current, processes, calls, role_sessions = controller
    team = current.store.get_team(OWNER, BRAIN, TEAM)
    worker = next(
        process for process in processes.values() if process.connection.identity.role == "grunt"
    )
    with current.store._sessions("prepare_initial_unbound_native_team") as db:
        for binding_id in (team.brain_agent_id, worker.connection.identity.attempt_id):
            db.delete(db.get(SqlBgrNativeBinding, (current_workspace_id(), binding_id)))
    registry = BgrControllers(current.store, role_sessions)
    calls.clear()
    try:
        await registry.attach(
            user_id=OWNER,
            brain_session_id=BRAIN,
            team_id=TEAM,
            fence=current.fence,
            runtime=current.runtime,
        )
        assert sum(parameters.get("method") == "bgr/thread/bind" for _, parameters in calls) == 1
        registry.remove(BRAIN, current.fence)
        calls.clear()
        restored = await registry.attach(
            user_id=OWNER,
            brain_session_id=BRAIN,
            team_id=TEAM,
            fence=current.fence,
            runtime=current.runtime,
            existing_only=True,
        )
        assert set(restored.connections) == {BRAIN}
        assert not any(parameters.get("method") == "bgr/thread/bind" for _, parameters in calls)
    finally:
        await registry.close()


@pytest.mark.parametrize("denial", ["blocked", "failed", "supervised_history", "retained_marker"])
def test_same_live_target_denies_launch_markers_even_without_supervision_history(
    recovery_store, supervised_admission, denial
):
    store, intent, binding, fence = recovery_store
    if denial == "supervised_history":
        store.reserve_supervised_generation(OWNER, supervised_admission.generation)
    elif denial == "retained_marker":
        store.begin_runtime_recovery(
            OWNER,
            intent.brain_session_id,
            TEAM,
            fence,
            recovery_id=uuid4().hex,
            runtime_id=binding.runtime_id,
            replacement_controller_id=uuid4().hex,
        )
        with store._sessions("retain_supervised_denial_without_launch_history") as db:
            row = db.get(SqlBgrTeam, (current_workspace_id(), TEAM))
            team = Team.model_validate_json(row.document)
            row.document = team.model_copy(
                update={
                    "runtime_recoveries": [
                        team.runtime_recoveries[-1].model_copy(
                            update={"supervised": True, "state": "attached"}
                        )
                    ]
                }
            ).model_dump_json()
            db.delete(db.get(SqlBgrLaunch, (current_workspace_id(), TEAM)))
    else:
        with store._sessions("block_existing_launch_without_recovery_transition") as db:
            row = db.get(SqlBgrLaunch, (current_workspace_id(), TEAM))
            row.document = (
                LaunchIntent.model_validate_json(row.document)
                .model_copy(update={"state": denial})
                .model_dump_json()
            )
    before = store.get_team(OWNER, intent.brain_session_id, TEAM)
    with pytest.raises(OmnigentError):
        store.live_controller_target(OWNER, intent.brain_session_id)
    with pytest.raises(OmnigentError):
        store.resume_verified_live_controller(
            OWNER, intent.brain_session_id, TEAM, fence, binding=binding, assignments=[]
        )
    assert (
        store.get_team(OWNER, intent.brain_session_id, TEAM).lease_expires_at
        == before.lease_expires_at
    )


async def test_retire_revoked_controller_is_exact_fenced_and_idempotent(controller):
    current, _processes, calls, role_sessions = controller
    registry = BgrControllers(current.store, role_sessions)
    registry._controllers[BRAIN] = current
    heartbeat = current._heartbeat
    assert heartbeat is not None
    before = list(calls)
    for fence in (
        current.fence.model_copy(update={"epoch": current.fence.epoch + 1}),
        current.fence.model_copy(update={"controller_id": uuid4().hex}),
    ):
        assert not await registry.retire(BRAIN, fence)
        assert registry.get(BRAIN) is current and not heartbeat.cancelling()
    assert not await registry.retire(uuid4().hex, current.fence)
    binding = current.store.native_brain_binding(OWNER, BRAIN, TEAM, current.fence)
    current.store.begin_runtime_recovery(
        OWNER,
        BRAIN,
        TEAM,
        current.fence,
        recovery_id=uuid4().hex,
        runtime_id=binding.runtime_id,
        replacement_controller_id=uuid4().hex,
    )
    assert await registry.retire(BRAIN, current.fence)
    assert heartbeat.cancelling()
    with pytest.raises(OmnigentError, match="not attached"):
        registry.get(BRAIN)
    assert not await registry.retire(BRAIN, current.fence)
    assert calls == before
    assert not registry._attach_lock.locked() and not current._callbacks_lock.locked()


@pytest.mark.parametrize("cached", [True, False])
@pytest.mark.parametrize("expired", [True, False])
@pytest.mark.parametrize(
    "loss", [None, "foreign_generation", "foreign_host", "foreign_thread", "lost_identity"]
)
async def test_replacement_attach_retries_only_exact_live_native_binding(
    replacement_recovery, monkeypatch, cached, expired, loss
):
    store, intent, fence, binding = replacement_recovery
    session = intent.brain_session_id
    native = NativeProcess()
    foreign = False

    async def host_call(operation, parameters):
        if operation == "brain":
            return {
                "thread_id": str(uuid4())
                if foreign and loss == "foreign_thread"
                else binding.thread_id,
                "runtime_id": uuid4().hex
                if foreign and loss == "foreign_generation"
                else binding.runtime_id,
                "host_id": uuid4().hex if foreign and loss == "foreign_host" else HOST,
            }
        assert operation == "rpc" and parameters["session_id"] == session
        method, params = parameters["method"], parameters["params"]
        if method == "bgr/capabilities/read":
            return {"result": CAPTURED}
        if method == "bgr/thread/bind":
            native.connection = NativeBgrConnection(
                binding.thread_id,
                NativeIdentity.model_validate(params["identity"]),
                native.request,
                NativeCapabilities.model_validate(CAPTURED),
            )
            native.calls.append((method, params))
            return await native.request("bgr/thread/inspect", {"threadId": binding.thread_id})
        return await native.request(method, params)

    runtime = BgrHostRuntime(session, host_call)
    registry = BgrControllers(store, AsyncMock())
    try:
        first = await registry.attach(
            user_id=OWNER, brain_session_id=session, team_id=TEAM, fence=fence, runtime=runtime
        )
        recovery = store.get_team(OWNER, session, TEAM).runtime_recoveries[-1]
        assert not await registry.retire(
            session,
            fence.model_copy(
                update={
                    "epoch": recovery.previous_epoch,
                    "controller_id": recovery.previous_controller_id,
                }
            ),
        )
        assert registry.get(session) is first and not first._heartbeat.cancelling()
        if not cached:
            await registry.close()
            registry = BgrControllers(store, AsyncMock())
        deadline = store.get_team(OWNER, session, TEAM).lease_expires_at
        if expired:
            clock = store._now_us() + 301_000_000
            monkeypatch.setattr(store, "_now_us", lambda: clock)
        if loss is not None and loss.startswith("foreign_"):
            foreign = True
        elif loss == "lost_identity":
            connection = native.connection
            native.connection = NativeBgrConnection(
                binding.thread_id,
                connection.identity.model_copy(update={"epoch": 1}),
                native.request,
                connection.capabilities,
            )

        async def retry():
            return await registry.attach(
                user_id=OWNER, brain_session_id=session, team_id=TEAM, fence=fence, runtime=runtime
            )

        if loss is not None:
            with pytest.raises(OmnigentError):
                await retry()
            with pytest.raises(OmnigentError):
                await first.renew_lease()
            assert store.get_team(OWNER, session, TEAM).lease_expires_at == deadline
        else:
            resumed = await retry()
            assert (resumed is first) == cached
            before_renewal = store.get_team(OWNER, session, TEAM).lease_expires_at
            clock = store._now_us() + 301_000_000
            monkeypatch.setattr(store, "_now_us", lambda: clock)
            await resumed.renew_lease()
            assert store.get_team(OWNER, session, TEAM).lease_expires_at > before_renewal
            assert store.get_team(OWNER, session, TEAM).runtime_recoveries[-1].state == "binding"
            with pytest.raises(OmnigentError):
                store.check_session_runtime_input(session)
            store.mark_launch_running(OWNER, session, TEAM, fence)
            store.check_session_runtime_input(session)
        assert sum(method == "bgr/thread/bind" for method, _ in native.calls) == 1
        assert (
            store.get_launch(OWNER, TEAM).request.initial_prompt == intent.request.initial_prompt
        )
    finally:
        await registry.close()


async def test_brain_create_is_durable_retry_stable_and_capacity_bounded(controller):
    current, processes, calls, role_sessions = controller
    command = BgrControl(
        action="create_grunt", action_id=uuid4().hex, contract=CONTRACT, base_sha=SHA
    )
    for user, session in (("outsider", BRAIN), (OWNER, uuid4().hex)):
        with pytest.raises(OmnigentError):
            await current.execute(user, session, command)
    assert not any(operation == "workspace" for operation, _ in calls)
    result = await current.execute(OWNER, BRAIN, command)
    assert result["native_acceptance_is_consumption"] is False
    assert await current.execute(OWNER, BRAIN, command) == result
    assert sum(operation == "spawn" for operation, _ in calls) == 1
    role_sessions.assert_awaited_once()
    worker = processes[result["task"]["attempt"]["session_id"]]
    assert next(params for method, params in worker.calls if method == "bgr/thread/peers") == {
        "threadId": worker.connection.thread_id,
        "identity": worker.connection.identity.wire(),
        "peers": [current.brain.identity.wire()],
    }
    methods = [method for method, _ in worker.calls]
    assert methods.index("bgr/thread/peers") < methods.index("bgr/message/deliver")
    assert len(worker.pending) == 1
    # The original fixture already occupies one of the two slots.
    with pytest.raises(OmnigentError, match="cap"):
        await current.execute(OWNER, BRAIN, command.model_copy(update={"action_id": uuid4().hex}))
    assert sum(operation == "spawn" for operation, _ in calls) == 1
    changed = command.model_copy(update={"base_sha": "f" * 40})
    with pytest.raises(OmnigentError, match="conflict"):
        await current.execute(OWNER, BRAIN, changed)


async def test_review_factory_starts_two_independent_roles_and_projects_barrier(
    controller, monkeypatch
):
    current, processes, calls, role_sessions = controller
    command = BgrControl(
        action="start_review",
        action_id=uuid4().hex,
        repository="ModelCloud/omnigent",
        pr_number=1,
        base_sha=SHA,
        head_sha="e" * 40,
    )
    result = await current.execute(OWNER, BRAIN, command)
    assert result["results_ready"] is False
    assert len(result["jobs"]) == 2
    assert all(job["state"] == "running" and job["result"] is None for job in result["jobs"])
    reviewers = [
        process for process in processes.values() if process.connection.identity.role == "reviewer"
    ]
    assert len(reviewers) == 2
    assert len({process.connection.identity.agent_id for process in reviewers}) == 2
    assert all(len(process.pending) == 1 for process in reviewers)
    assert role_sessions.await_count == 2
    with pytest.raises(OmnigentError, match="withheld"):
        await current.execute(
            OWNER,
            BRAIN,
            BgrControl(
                action="publication_preview",
                action_id=uuid4().hex,
                round_id=result["round_id"],
                review_id=result["jobs"][0]["review_id"],
            ),
        )
    for round_index in range(2):
        if round_index:
            result = await current.execute(
                OWNER, BRAIN, command.model_copy(update={"action_id": uuid4().hex})
            )
        jobs = [
            job.model_dump()
            for job in current.store.get_review_round(OWNER, BRAIN, TEAM, result["round_id"]).jobs
        ]
        callbacks = []
        for job in jobs:
            process = processes[job["session_id"]]
            anchor = NativeEnvelope.model_validate(next(iter(process.pending.values())))
            callback = process.emit(
                current.brain,
                anchor,
                "review-result-" + job["review_id"],
                "result",
                payload={"outcome": "no_findings", "findings": [], "coverageLimits": []},
            )
            callbacks.append(callback)
            await current.native_callback(
                session_id=job["session_id"],
                thread_id=process.connection.thread_id,
                method="bgr/message/emitted",
                params=callback,
            )
        sealed = current.store.review_status(OWNER, BRAIN, TEAM, result["round_id"])
        assert sealed["results_ready"] is True
        assert all(job["state"] == "sealed" for job in sealed["jobs"])
        for index, job in enumerate(jobs):
            assert (
                current.store.native_assignment(
                    OWNER, BRAIN, TEAM, job["attempt_id"], current.fence
                ).attempt.state
                == "reserved"
            )
            close = BgrControl(
                action="adjudicate",
                action_id=uuid4().hex,
                attempt_id=job["attempt_id"],
                outcome="completed",
            )
            connection = current.connections[job["session_id"]]
            with monkeypatch.context() as patch:
                patch.setattr(current.runtime, "stop", AsyncMock(return_value=False))
                with pytest.raises(OmnigentError, match="stop is unconfirmed"):
                    await current.execute(OWNER, BRAIN, close)
            assert current.connections[job["session_id"]] is connection
            assert (
                current.store.native_assignment(
                    OWNER, BRAIN, TEAM, job["attempt_id"], current.fence
                ).attempt.state
                == "reserved"
            )
            close = close.model_copy(update={"action_id": uuid4().hex})
            stopped = await current.execute(OWNER, BRAIN, close)
            assert stopped["attempt"]["state"] == "completed"
            assert await current.execute(OWNER, BRAIN, close) == stopped
            assert (
                sum(
                    operation == "stop" and parameters["session_id"] == job["session_id"]
                    for operation, parameters in calls
                )
                == 1
            )
            assert current.store.review_status(OWNER, BRAIN, TEAM, result["round_id"]) == sealed
            if index == 0:
                sibling = jobs[1]
                await current.native_callback(
                    session_id=sibling["session_id"],
                    thread_id=processes[sibling["session_id"]].connection.thread_id,
                    method="bgr/message/emitted",
                    params=callbacks[1],
                )
            assert job["session_id"] not in current.connections
        assert set(current.connections) == {
            session
            for session, process in processes.items()
            if process.connection.identity.role != "reviewer"
        }


async def test_publish_uncertain_retries_are_read_only_reconciliation_with_same_action(controller):
    current, _, _, _ = controller
    result = await current.execute(
        OWNER,
        BRAIN,
        BgrControl(
            action="start_review",
            action_id=uuid4().hex,
            repository="ModelCloud/omnigent",
            pr_number=1,
            base_sha=SHA,
            head_sha="e" * 40,
        ),
    )
    round = current.store.get_review_round(OWNER, BRAIN, TEAM, result["round_id"])
    for job in round.jobs:
        current.store.send(
            OWNER,
            job.session_id,
            TEAM,
            review_message(round, job, current.brain.identity.agent_id),
        )
    command = BgrControl(
        action="publish",
        action_id=uuid4().hex,
        round_id=round.round_id,
        review_id=round.jobs[0].review_id,
        approved_body_sha256="d" * 64,
    )
    evaluation = BgrControl(
        action="evaluate_reviews",
        action_id=uuid4().hex,
        round_id=round.round_id,
        evaluation=ReviewEvaluation(
            results_sha256=current.store.review_status(OWNER, BRAIN, TEAM, round.round_id)[
                "results_sha256"
            ],
            next_action="accept",
            rationale="No independent findings in either sealed review.",
        ),
    )
    decision = await current.execute(OWNER, BRAIN, evaluation)
    assert await current.execute(OWNER, BRAIN, evaluation) == decision
    current.runtime.call = AsyncMock(
        side_effect=[
            {"publication": {"state": "uncertain"}},
            {"publication": {"state": "published", "comment_id": 21}},
        ]
    )
    assert (await current.execute(OWNER, BRAIN, command))["publication"]["state"] == "uncertain"
    published = await current.execute(OWNER, BRAIN, command)
    assert published["publication"]["state"] == "published"
    assert await current.execute(OWNER, BRAIN, command) == published
    assert [call.args[0] for call in current.runtime.call.await_args_list] == [
        "publish",
        "publication_reconcile",
    ]
    with pytest.raises(OmnigentError, match="conflict"):
        await current.execute(
            OWNER,
            BRAIN,
            command.model_copy(
                update={
                    "approved_body_sha256": "f" * 64,
                }
            ),
        )
