"""Server authority and durable actions over the existing native mailbox boundary."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnigent.bgr_control import BgrControl
from omnigent.errors import OmnigentError
from omnigent.harnesses.codex_native.bgr import NativeCapabilities
from omnigent.harnesses.codex_native.bgr_messages import NativeBgrConnection, NativeIdentity
from omnigent.server.bgr_controller import BgrControllers, BgrHostRuntime
from tests.harnesses.codex_native.test_bgr_lifecycle import CAPTURED
from tests.harnesses.codex_native.test_bgr_messages import NativeProcess
from tests.harnesses.codex_native.test_bgr_messages import route as route
from tests.stores.test_bgr_recovery import recovery_store as recovery_store
from tests.stores.test_bgr_recovery import replacement_recovery as replacement_recovery
from tests.stores.test_bgr_review_store import message as review_message
from tests.stores.test_bgr_store import BRAIN, CONTRACT, HOST, OWNER, SHA, TEAM


@pytest.fixture
async def controller(route):
    mailbox, _, brain, _ = route
    processes = {BRAIN: brain}
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


@pytest.mark.parametrize("cached", [True, False])
@pytest.mark.parametrize("expired", [True, False])
@pytest.mark.parametrize("loss", [None, "foreign_generation", "lost_identity"])
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
                "thread_id": binding.thread_id,
                "runtime_id": uuid4().hex if foreign else binding.runtime_id,
                "host_id": HOST,
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
        if not cached:
            await registry.close()
            registry = BgrControllers(store, AsyncMock())
        deadline = store.get_team(OWNER, session, TEAM).lease_expires_at
        if expired:
            clock = store._now_us() + 301_000_000
            monkeypatch.setattr(store, "_now_us", lambda: clock)
        if loss == "foreign_generation":
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
            assert store.get_team(OWNER, session, TEAM).lease_expires_at == deadline
        else:
            resumed = await retry()
            assert (resumed is first) == cached
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


async def test_review_factory_starts_two_independent_roles_and_projects_barrier(controller):
    current, processes, _, role_sessions = controller
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
