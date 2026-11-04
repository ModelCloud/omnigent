"""Factory response validation only; positive provider execution is a separate boundary."""

import copy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnigent.errors import OmnigentError
from omnigent.harnesses.codex_native.bgr import NativeCapabilities
from omnigent.harnesses.codex_native.bgr_lifecycle import (
    NativeIsolatedReviewer,
    NativeReviewOptions,
    NativeReviewStart,
)
from omnigent.harnesses.codex_native.bgr_messages import NativeBgrConnection, NativeIdentity
from omnigent.runner.bgr_reviewer import BgrReviewerRunner
from tests.harnesses.codex_native.test_bgr_messages import CAPTURED, NativeProcess
from tests.harnesses.codex_native.test_bgr_messages import route as route
from tests.stores.test_bgr_review_store import request as review_request
from tests.stores.test_bgr_store import BRAIN, HOST, OWNER, PROFILE, SHA, TEAM


@pytest.fixture
def start_request():
    return NativeReviewStart.model_validate(
        {
            "identity": {
                "teamId": TEAM,
                "agentId": uuid4().hex,
                "sessionId": uuid4().hex,
                "taskId": uuid4().hex,
                "attemptId": uuid4().hex,
                "epoch": 1,
                "role": "reviewer",
            },
            "peers": [
                {
                    "teamId": TEAM,
                    "agentId": uuid4().hex,
                    "sessionId": BRAIN,
                    "epoch": 1,
                    "role": "brain",
                }
            ],
            "review": {"repository": "/trusted/repository", "baseSha": SHA, "headSha": "e" * 40},
            "model": PROFILE.model,
            "modelProvider": PROFILE.provider,
        }
    )


@pytest.fixture
def start_response(start_request):
    return {
        "threadId": str(uuid4()),
        "snapshot": {
            "identity": start_request.identity.wire(),
            "cancelled": False,
            "sequence": 0,
            "pending": [],
            "replies": [],
        },
        "review": {
            "baseSha": SHA,
            "headSha": "e" * 40,
            "changedFiles": ["source.py"],
            "coverageLimits": [],
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [None, "identity", "cancelled", "sequence", "base", "head", "path", "extra", "root"],
)
async def test_atomic_factory_validates_fresh_identity_and_frozen_snapshot(
    start_request, start_response, fault
):
    response = copy.deepcopy(start_response)
    if fault == "identity":
        response["snapshot"]["identity"]["epoch"] += 1
    elif fault == "cancelled":
        response["snapshot"]["cancelled"] = True
    elif fault == "sequence":
        response["snapshot"]["sequence"] = 1
    elif fault in {"base", "head"}:
        response["review"][f"{fault}Sha"] = "f" * 40
    elif fault == "path":
        response["review"]["changedFiles"] = ["../runtime/config.toml"]
    elif fault == "extra":
        response["tools"] = ["shell"]
    calls = []

    async def request(method, params):
        calls.append((method, copy.deepcopy(params)))
        if method == "bgr/capabilities/read":
            return {"result": copy.deepcopy(CAPTURED)}
        if method == "bgr/reviewer/start":
            assert params == start_request.wire()
            return {"result": response}
        assert params["threadId"] == start_response["threadId"]
        if method == "bgr/thread/inspect":
            return {"result": {"snapshot": copy.deepcopy(start_response["snapshot"])}}
        assert method == "thread/read" and params["includeTurns"] is False
        return {
            "result": {
                "thread": {
                    "id": start_response["threadId"],
                    "cwd": "/trusted/repository" if fault == "root" else "/isolated/snapshot",
                }
            }
        }

    if fault is not None:
        with pytest.raises(OmnigentError):
            await NativeIsolatedReviewer.start(request, start_request)
    else:
        started = await NativeIsolatedReviewer.start(request, start_request)
        assert started.connection.identity == start_request.identity
        assert started.manifest.head_sha == start_request.review.head_sha
        assert started.snapshot_root == "/isolated/snapshot"
    assert sum(method == "bgr/reviewer/start" for method, _ in calls) == 1
    assert not any(
        method in {"thread/start", "turn/start", "bgr/thread/bind"} for method, _ in calls
    )


def test_atomic_reviewer_rejects_unsafe_launch_fields_and_extra_peers(start_request):
    for field in ("tools", "env", "cwd", "config", "instructions"):
        with pytest.raises(ValidationError):
            NativeReviewStart.model_validate({**start_request.wire(), field: {}})
    with pytest.raises(ValidationError):
        NativeReviewStart.model_validate(
            {**start_request.wire(), "peers": start_request.wire()["peers"] * 2}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["reviewerStart", "reviewerSandbox", "hostRecord"])
async def test_factory_requires_negotiated_isolation_and_source_record_before_start(
    start_request, flag
):
    calls = []

    async def request(method, params):
        calls.append(method)
        assert method == "bgr/capabilities/read"
        return {"result": {**CAPTURED, flag: False}}

    with pytest.raises(OmnigentError):
        await NativeIsolatedReviewer.start(request, start_request)
    assert calls == ["bgr/capabilities/read"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["epoch", "team", "agent", "session", "role", "duplicate"])
async def test_brain_rejects_peer_fence_mismatch_before_native_rpc(route, fault):
    _, _, brain, worker = route
    peer = worker.connection.identity
    changes = {
        "epoch": {"epoch": 2},
        "team": {"team_id": uuid4().hex},
        "agent": {"agent_id": brain.connection.identity.agent_id},
        "session": {"session_id": BRAIN},
        "role": {"role": "brain"},
        "duplicate": {},
    }
    invalid = peer.model_copy(update=changes[fault])
    with pytest.raises(OmnigentError):
        await brain.connection.authorize_peers([invalid] * (2 if fault == "duplicate" else 1))
    assert not brain.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "start_timeout", "delivery_timeout", "unconfirmed_stop"])
async def test_runner_claims_before_start_and_registers_before_source_record_and_delivery(
    route, fault
):
    mailbox, _, brain, _ = route
    round = mailbox.store.reserve_review_round(OWNER, BRAIN, TEAM, review_request())
    job = round.jobs[0]
    process = NativeProcess(fail_after_accept=fault == "delivery_timeout")
    thread_id = str(uuid4())
    calls = []
    registered = []
    stops = []

    async def request(method, params):
        calls.append((method, copy.deepcopy(params)))
        if method == "bgr/capabilities/read":
            return {"result": copy.deepcopy(CAPTURED)}
        if method == "bgr/reviewer/start":
            claimed = mailbox.store.get_review_round(OWNER, BRAIN, TEAM, round.round_id).jobs[0]
            assert claimed.state == "starting"
            assert params["peers"] == [brain.connection.identity.wire()]
            process.connection = NativeBgrConnection(
                thread_id,
                NativeIdentity.model_validate(params["identity"]),
                request,
                NativeCapabilities.model_validate(CAPTURED),
            )
            if fault in {"start_timeout", "unconfirmed_stop"}:
                raise TimeoutError("Unconfirmed start response")
            return {
                "result": {
                    "threadId": thread_id,
                    "snapshot": (
                        await process.request("bgr/thread/inspect", {"threadId": thread_id})
                    )["result"]["snapshot"],
                    "review": {
                        "baseSha": params["review"]["baseSha"],
                        "headSha": params["review"]["headSha"],
                        "changedFiles": ["source.py"],
                        "coverageLimits": [],
                    },
                }
            }
        if method == "thread/read":
            return {"result": {"thread": {"id": thread_id, "cwd": f"/snapshots/{thread_id}"}}}
        return await process.request(method, params)

    def register(connection):
        identity, bound = mailbox.identity(connection.identity.attempt_id)
        assert identity == connection.identity and bound == thread_id
        anchor = mailbox.store.drain(OWNER, job.session_id, TEAM)[0]
        assert anchor.message_id == job.task_id and anchor.head_sha == round.head_sha
        assert not process.pending and not brain.outgoing
        registered.append(connection)

    async def stop():
        stops.append(True)
        return fault != "unconfirmed_stop"

    runner = BgrReviewerRunner(mailbox, brain.connection, register)
    arguments = {
        "round_id": round.round_id,
        "review_id": job.review_id,
        "repository_path": "/trusted/repository",
        "host_id": HOST,
        "options": NativeReviewOptions(model=PROFILE.model, model_provider=PROFILE.provider),
        "request": request,
        "stop": stop,
    }
    if fault is not None:
        with pytest.raises(TimeoutError):
            await runner.start(**arguments)
    else:
        started = await runner.start(**arguments)
        assert registered == [started.connection]
        anchor = mailbox.store.drain(OWNER, job.session_id, TEAM)[0]
        assert anchor.consumed_at is None
        assert brain.outgoing[job.task_id] == process.pending[job.task_id]
        peer_call = next(params for method, params in brain.calls if method == "bgr/thread/peers")
        assert len(peer_call["peers"]) == 2
        assert started.connection.identity.wire() in peer_call["peers"]
    assert await runner.start(**arguments) is None
    assert sum(method == "bgr/reviewer/start" for method, _ in calls) == 1
    current = mailbox.store.get_review_round(OWNER, BRAIN, TEAM, round.round_id).jobs[0]
    assert current.state == (
        "starting" if fault == "unconfirmed_stop" else "sealed" if fault else "running"
    )
    assert stops == ([True] if fault else [])
    if fault in {"start_timeout", "unconfirmed_stop"}:
        assert not registered and not process.pending and not brain.outgoing
    if fault == "start_timeout":
        assert current.result.status == "incomplete"
        assert "startup was not confirmed" in current.result.summary
