"""BGR HTTP contract through create_app, bound runners and real SQL stores."""

import json
from random import Random
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request

from omnigent.bgr import Fence, NativeBrainBinding
from omnigent.errors import OmnigentError
from omnigent.runner.bgr_runtime import load_bgr_team_config
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.bgr_input import check_bgr_session_input
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.mcp_policy_adapter import McpPolicyAdapter
from omnigent.server.routes._sessions.orchestration import _handle_mcp_tools_call
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.bgr_store import BgrStore
from omnigent.stores.conversation_store import CREATED_BY_SESSION_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio
PREFIX = "/v1/bgr"
HOST, SHA = "1" * 32, "d" * 40


@pytest.fixture
def bgr_environment(runtime_init, db_uri, tmp_path):
    now = [1000.0]
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("alice")
    permissions.ensure_user("bob")
    brain = conversations.create_conversation(host_id=HOST, workspace="/work/brain")
    conversations.set_runner_id(brain.id, token_bound_runner_id(brain.id))
    permissions.grant("alice", brain.id, LEVEL_OWNER)

    def worker(*, parent=True, owner="alice", host=HOST):
        session_id = uuid4().hex
        child = conversations.create_conversation(
            conversation_id=session_id,
            parent_conversation_id=brain.id if parent else None,
            labels={} if parent else {CREATED_BY_SESSION_LABEL_KEY: brain.id},
            host_id=host,
            workspace=f"/work/{session_id}",
        )
        conversations.set_runner_id(child.id, token_bound_runner_id(child.id))
        if not parent:
            permissions.grant(owner, child.id, LEVEL_OWNER)
        return child.id

    def application(features=None):
        artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
        return create_app(
            agent_store=SqlAlchemyAgentStore(db_uri),
            file_store=SqlAlchemyFileStore(db_uri),
            conversation_store=SqlAlchemyConversationStore(db_uri),
            artifact_store=artifacts,
            agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
            permission_store=SqlAlchemyPermissionStore(db_uri),
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            bgr_store=BgrStore(db_uri, clock=lambda: now[0]),
            feature_flags=resolve_feature_flags({"OMNIGENT_FEATURES": features or ""}),
        )

    return application, brain.id, worker, permissions, now


def headers(session_id, user="alice"):
    return {
        "X-Forwarded-Email": user,
        ORCHESTRATION_SOURCE_HEADER: session_id,
        RUNNER_TUNNEL_TOKEN_HEADER: session_id,
    }


async def create_team(client, brain):
    profile = {
        "provider": "example",
        "model": "model",
        "variant": "variant",
        "speed": "fast",
        "reasoning": "high",
        "harness": "codex-native",
    }
    team_id = uuid4().hex
    response = await client.post(
        f"{PREFIX}/teams",
        headers=headers(brain),
        json={
            "team_id": team_id,
            "config": {
                "brain": profile,
                "grunt": profile,
                "reviewers": [profile],
                "max_active_grunts": 2,
            },
        },
    )
    assert response.status_code == 201, response.text
    team = response.json()
    controller_id = uuid4().hex
    response = await client.post(
        f"{PREFIX}/teams/{team_id}/lease",
        headers=headers(brain),
        json={"controller_id": controller_id},
    )
    assert response.status_code == 200, response.text
    return team, {"controller_id": controller_id, "epoch": response.json()["epoch"]}


async def test_http_control_and_callback_fail_closed_before_brain_attach(bgr_environment):
    application, brain, worker, _, _ = bgr_environment
    app = application()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        team, _ = await create_team(client, brain)
        client.headers.update(headers(brain))
        config = await load_bgr_team_config(
            server_client=client, session_id=brain, team_id=team["team_id"]
        )
        assert config.model_dump(mode="json") == team["config"]
        assert len(config.model_dump_json()) > 256
        for session in (brain, worker()):
            response = await client.post(
                f"{PREFIX}/control",
                headers=headers(session),
                json={"action": "status", "action_id": uuid4().hex},
            )
            assert response.status_code >= 400
        response = await client.post(
            f"{PREFIX}/teams/{team['team_id']}/native-callback",
            headers=headers(brain),
            json={
                "session_id": worker(),
                "thread_id": str(uuid4()),
                "method": "runtime/thread-ready",
                "params": {},
            },
        )
        assert response.status_code == 403
        response = await client.post(
            f"{PREFIX}/control",
            headers=headers(brain, user="bob"),
            json={"action": "status", "action_id": uuid4().hex},
        )
        assert response.status_code == 403
    await app.state.bgr_controllers.close()


@pytest.mark.parametrize("rejected_at", [None, "thread", "launch"])
async def test_native_ready_hands_off_startup_only_after_verified_attachment(
    bgr_environment, monkeypatch, rejected_at
):
    application, brain, _, _, _ = bgr_environment
    app = application()
    order = []
    thread_id = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        team, fence = await create_team(client, brain)

        async def attach(**kwargs):
            order.append("attach")
            return SimpleNamespace(
                brain=SimpleNamespace(
                    thread_id=str(uuid4()) if rejected_at == "thread" else thread_id
                ),
                fence=Fence.model_validate(fence),
            )

        def mark_running(*args):
            order.append("running")
            if rejected_at == "launch":
                raise OmnigentError("Stale launch fence")

        async def ready(*args):
            order.append("startup")

        monkeypatch.setattr(
            "omnigent.server.routes.sessions._get_runner_client", AsyncMock(return_value=client)
        )
        monkeypatch.setattr(app.state.bgr_controllers, "attach", attach)
        monkeypatch.setattr(app.state.bgr_controllers.store, "mark_launch_running", mark_running)
        monkeypatch.setattr(app.state.startup_inputs, "session_ready", ready)
        response = await client.post(
            f"{PREFIX}/teams/{team['team_id']}/native-callback",
            headers=headers(brain),
            json={
                "session_id": brain,
                "thread_id": thread_id,
                "method": "runtime/thread-ready",
                "params": {},
            },
        )
        if rejected_at:
            assert response.status_code >= 400
            assert "startup" not in order
        else:
            assert response.status_code == 200, response.text
            assert order == ["attach", "running", "startup"]
    await app.state.bgr_controllers.close()


def task_request(worker, fence):
    return {
        **fence,
        "task_id": uuid4().hex,
        "session_id": worker,
        "contract": {
            "objective": "Fix bug",
            "instructions": "Use own worktree",
            "acceptance": ["Tests pass"],
        },
        "workspace": {
            "host_id": HOST,
            "repository": "repo",
            "worktree": f"/work/{worker}",
            "base_sha": SHA,
        },
    }


async def test_http_recovery_cursor_pages_survive_app_restart(bgr_environment):
    application, brain, worker, _, _ = bgr_environment
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()),
        base_url="http://test",
    ) as client:
        team, fence = await create_team(client, brain)
        team_id = team["team_id"]
        children = [worker(), worker()]
        tasks = []
        for child in children:
            response = await client.post(
                f"{PREFIX}/teams/{team_id}/tasks",
                headers=headers(brain),
                json=task_request(child, fence),
            )
            assert response.status_code == 201, response.text
            tasks.append(response.json())
        response = await client.get(
            f"{PREFIX}/teams/{team_id}",
            headers=headers(brain),
            params={"limit": 1},
        )
        assert response.status_code == 200, response.text
        first = response.json()
        assert len(first["tasks"]) == len(first["agents"]) == 1
        assert first["pending_envelopes"] == 1 and first["pending_envelopes_has_more"]
        assert first["next_cursor"] is not None
        for params in ({"limit": 0}, {"limit": 101}, {"cursor": "!"}, {"cursor": "x" * 1025}):
            response = await client.get(
                f"{PREFIX}/teams/{team_id}", headers=headers(brain), params=params
            )
            assert response.status_code in (400, 422), response.text
        response = await client.get(
            f"{PREFIX}/teams/{team_id}",
            headers=headers(children[0]),
            params={"cursor": first["next_cursor"]},
        )
        assert response.status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()),
        base_url="http://test",
    ) as client:
        second = (
            await client.get(
                f"{PREFIX}/teams/{team_id}",
                headers=headers(brain),
                params={"limit": 1, "cursor": first["next_cursor"]},
            )
        ).json()
        third = (
            await client.get(
                f"{PREFIX}/teams/{team_id}",
                headers=headers(brain),
                params={"limit": 1, "cursor": second["next_cursor"]},
            )
        ).json()
        assert third["tasks"] == [] and third["next_cursor"] is None
        assert first["tasks"] + second["tasks"] == sorted(tasks, key=lambda task: task["task_id"])
        agents = first["agents"] + second["agents"] + third["agents"]
        assert len({agent["agent_id"] for agent in agents}) == 3


async def test_http_oversized_checkpoint_returns_invalid_input_and_rolls_back(bgr_environment):
    application, brain, worker, _, _ = bgr_environment
    random = Random(0)

    def text(length):
        return "".join(chr(random.randrange(0x10000, 0x10FFFF)) for _ in range(length))

    child = worker()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()),
        base_url="http://test",
    ) as client:
        team, fence = await create_team(client, brain)
        team_id = team["team_id"]
        task = (
            await client.post(
                f"{PREFIX}/teams/{team_id}/tasks",
                headers=headers(brain),
                json=task_request(child, fence),
            )
        ).json()
        checkpoint = {
            "attempt_id": task["attempt"]["attempt_id"],
            "epoch": 1,
            "phase": text(256),
            "summary": text(2000),
            "artifact_refs": [text(256) for _ in range(16)],
        }
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/checkpoints",
            headers=headers(child),
            json=checkpoint,
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_input" and "16383" in response.text
        snapshot = (await client.get(f"{PREFIX}/teams/{team_id}", headers=headers(brain))).json()
        assert snapshot["tasks"] == [task] and snapshot["pending_envelopes"] == 1
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/checkpoints",
            headers=headers(child),
            json={
                **checkpoint,
                "phase": "saved",
                "summary": "artifact refs only",
                "artifact_refs": [],
            },
        )
        assert response.status_code == 200, response.text


async def test_http_restart_acceptance_correlation_checkpoint_and_ack(bgr_environment):
    application, brain, worker, _, _ = bgr_environment
    child = worker()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()), base_url="http://test"
    ) as client:
        team, fence = await create_team(client, brain)
        team_id = team["team_id"]
        body = task_request(child, fence)
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/tasks", headers=headers(brain), json=body
        )
        assert response.status_code == 201, response.text
        task = response.json()
        attempt = task["attempt"]
        query = {
            **fence,
            "message_id": uuid4().hex,
            "recipient_agent_id": attempt["agent_id"],
            "task_id": task["task_id"],
            "attempt_id": attempt["attempt_id"],
            "kind": "status_query",
            "correlation_id": uuid4().hex,
            "head_sha": SHA,
            "payload": {"scope": "since_time", "since_time": "2026-11-03T00:00:00Z"},
        }
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/envelopes", headers=headers(brain), json=query
        )
        assert response.status_code == 202, response.text
        receipt = response.json()

    # Reconstruct the production app and all stores, using only the persisted DB.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()), base_url="http://test"
    ) as client:
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/envelopes", headers=headers(brain), json=query
        )
        assert response.json() == receipt
        snapshot = (await client.get(f"{PREFIX}/teams/{team_id}", headers=headers(brain))).json()
        assert snapshot["tasks"] == [task] and snapshot["pending_envelopes"] == 2
        response = await client.get(f"{PREFIX}/teams/{team_id}/mailbox", headers=headers(child))
        assert response.status_code == 200, response.text
        delivery = response.json()
        assert [entry["kind"] for entry in delivery] == ["task", "status_query"]
        assert delivery[1]["sender_session_id"] == brain
        assert delivery[1]["sender_name"] == "brain"
        assert delivery[1]["consumed_at"] is None
        checkpoint = {
            "attempt_id": attempt["attempt_id"],
            "epoch": 1,
            "phase": "testing",
            "summary": "Evidence saved",
            "head_sha": SHA,
            "artifact_refs": ["artifact:results"],
        }
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/checkpoints", headers=headers(child), json=checkpoint
        )
        assert response.status_code == 200 and response.json()["checkpoint"] == checkpoint
        reply = {
            **query,
            "controller_id": None,
            "message_id": uuid4().hex,
            "recipient_agent_id": team["brain_agent_id"],
            "kind": "status_reply",
            "in_reply_to": query["message_id"],
            "payload": {"summary": "Working; tests running"},
        }
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/envelopes", headers=headers(child), json=reply
        )
        assert response.status_code == 202, response.text
        callback = (
            await client.get(f"{PREFIX}/teams/{team_id}/mailbox", headers=headers(brain))
        ).json()[0]
        assert callback["sender_agent_id"] == attempt["agent_id"]
        assert callback["in_reply_to"] == query["message_id"]
        assert callback["correlation_id"] == query["correlation_id"]
        assert callback["payload"] == reply["payload"]
        for envelope in delivery:
            response = await client.post(
                f"{PREFIX}/teams/{team_id}/mailbox/ack",
                headers=headers(child),
                json={"envelope_id": envelope["envelope_id"], "epoch": 1},
            )
            assert response.status_code == 200, response.text
        assert (
            await client.get(f"{PREFIX}/teams/{team_id}/mailbox", headers=headers(child))
        ).json() == []
        snapshot = (await client.get(f"{PREFIX}/teams/{team_id}", headers=headers(brain))).json()
        assert len(snapshot["tasks"]) == 1 and len(snapshot["agents"]) == 2


async def test_http_bidirectional_questions_and_worker_result(bgr_environment):
    application, brain, worker, _, _ = bgr_environment
    child = worker()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()), base_url="http://test"
    ) as client:
        team, fence = await create_team(client, brain)
        endpoint = f"{PREFIX}/teams/{team['team_id']}"
        response = await client.post(
            f"{endpoint}/tasks", headers=headers(brain), json=task_request(child, fence)
        )
        assert response.status_code == 201, response.text
        task = response.json()
        attempt = task["attempt"]
        assignment = {
            "epoch": fence["epoch"],
            "task_id": task["task_id"],
            "attempt_id": attempt["attempt_id"],
            "head_sha": SHA,
        }
        for asker, responder, recipient in (
            (brain, child, attempt["agent_id"]),
            (child, brain, team["brain_agent_id"]),
        ):
            question = {
                **assignment,
                "controller_id": fence["controller_id"] if asker == brain else None,
                "message_id": uuid4().hex,
                "recipient_agent_id": recipient,
                "kind": "question",
                "correlation_id": uuid4().hex,
                "payload": {"question": "Which checks does this task require?"},
            }
            response = await client.post(
                f"{endpoint}/envelopes", headers=headers(asker), json=question
            )
            assert response.status_code == 202, response.text
            delivery = (await client.get(f"{endpoint}/mailbox", headers=headers(responder))).json()
            received = next(
                item for item in delivery if item["message_id"] == question["message_id"]
            )
            assert received["sender_session_id"] == asker
            assert received["correlation_id"] == question["correlation_id"]
            response = await client.post(
                f"{endpoint}/mailbox/ack",
                headers=headers(responder),
                json={"epoch": fence["epoch"], "envelope_id": received["envelope_id"]},
            )
            assert response.status_code == 200, response.text
            answer = {
                **question,
                "controller_id": fence["controller_id"] if responder == brain else None,
                "message_id": uuid4().hex,
                "recipient_agent_id": received["sender_agent_id"],
                "kind": "answer",
                "in_reply_to": question["message_id"],
                "payload": {"answer": "Run the task's specified focused checks."},
            }
            for mismatch in ({"correlation_id": uuid4().hex}, {"head_sha": "e" * 40}):
                response = await client.post(
                    f"{endpoint}/envelopes",
                    headers=headers(responder),
                    json={**answer, **mismatch},
                )
                assert response.status_code == 409, response.text
            response = await client.post(
                f"{endpoint}/envelopes", headers=headers(responder), json=answer
            )
            assert response.status_code == 202, response.text
            receipt = response.json()
            assert (
                await client.post(f"{endpoint}/envelopes", headers=headers(responder), json=answer)
            ).json() == receipt
            inbox = (await client.get(f"{endpoint}/mailbox", headers=headers(asker))).json()
            callback = next(item for item in inbox if item["message_id"] == answer["message_id"])
            assert callback["sender_session_id"] == responder
            assert callback["in_reply_to"] == question["message_id"]
            assert callback["correlation_id"] == question["correlation_id"]
            assert callback["payload"] == answer["payload"]
        result = {
            **assignment,
            "message_id": uuid4().hex,
            "recipient_agent_id": team["brain_agent_id"],
            "kind": "result",
            "payload": {"summary": "Focused checks passed", "head_sha": SHA},
        }
        response = await client.post(f"{endpoint}/envelopes", headers=headers(child), json=result)
        assert response.status_code == 202, response.text
        inbox = (await client.get(f"{endpoint}/mailbox", headers=headers(brain))).json()
        received = next(item for item in inbox if item["message_id"] == result["message_id"])
        assert received["sender_agent_id"] == attempt["agent_id"]
        assert received["payload"] == result["payload"]
        snapshot = (await client.get(endpoint, headers=headers(brain))).json()
        assert snapshot["tasks"][0]["attempt"]["state"] == "reserved"


@pytest.mark.parametrize(
    "case,expected",
    [
        ("anonymous", 401),
        ("other_owner", 403),
        ("wrong_runner", 403),
        ("missing_source", 403),
        ("shared", 403),
        ("admin", 403),
        ("worker", 403),
    ],
)
async def test_http_ownership_private_session_and_runner_binding(bgr_environment, case, expected):
    application, brain, worker, permissions, _ = bgr_environment
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()), base_url="http://test"
    ) as client:
        team, _ = await create_team(client, brain)
        request_headers = headers(brain)
        if case == "anonymous":
            del request_headers["X-Forwarded-Email"]
        elif case == "other_owner":
            request_headers = headers(brain, "bob")
        elif case == "wrong_runner":
            request_headers[RUNNER_TUNNEL_TOKEN_HEADER] = "unbound"
        elif case == "missing_source":
            del request_headers[ORCHESTRATION_SOURCE_HEADER]
        elif case == "shared":
            permissions.grant("bob", brain, LEVEL_READ)
        elif case == "admin":
            permissions.ensure_user("bob", is_admin=True)
            request_headers = headers(brain, "bob")
        elif case == "worker":
            request_headers = headers(worker())
        response = await client.get(f"{PREFIX}/teams/{team['team_id']}", headers=request_headers)
        assert response.status_code == expected, response.text


@pytest.mark.parametrize(
    "case", ["unowned", "host_mismatch", "path_mismatch", "unrelated", "shared_worker"]
)
async def test_http_worker_reservation_requires_owned_private_child(bgr_environment, db_uri, case):
    application, brain, worker, permissions, _ = bgr_environment
    child = worker(
        parent=False,
        owner="bob" if case == "unowned" else "alice",
        host="2" * 32 if case == "host_mismatch" else HOST,
    )
    if case == "unrelated":
        SqlAlchemyConversationStore(db_uri).delete_label(child, CREATED_BY_SESSION_LABEL_KEY)
    if case == "shared_worker":
        permissions.grant("bob", child, LEVEL_READ)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()), base_url="http://test"
    ) as client:
        team, fence = await create_team(client, brain)
        body = task_request(child, fence)
        if case == "path_mismatch":
            body["workspace"]["worktree"] = "/other-worktree"
        response = await client.post(
            f"{PREFIX}/teams/{team['team_id']}/tasks",
            headers=headers(brain),
            json=body,
        )
        assert response.status_code == (
            400 if case in ("host_mismatch", "path_mismatch") else 403
        ), response.text
        snapshot = (
            await client.get(f"{PREFIX}/teams/{team['team_id']}", headers=headers(brain))
        ).json()
        assert snapshot["tasks"] == [] and snapshot["pending_envelopes"] == 0


async def test_http_configuration_fences_and_launch_capabilities(bgr_environment):
    application, brain, worker, _, now = bgr_environment
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()), base_url="http://test"
    ) as client:
        team, fence = await create_team(client, brain)
        team_id = team["team_id"]
        capabilities = (await client.get(f"{PREFIX}/capabilities", headers=headers(brain))).json()
        assert capabilities["ledger"] is True
        assert (
            capabilities["runtime_launch"] is False and capabilities["reviewer_isolation"] is False
        )
        response = await client.post(f"{PREFIX}/teams/{team_id}/launch", headers=headers(brain))
        assert response.status_code == 503 and "reviewer isolation" in response.text
        child = worker(parent=False)
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/tasks",
            headers=headers(brain),
            json=task_request(child, fence),
        )
        assert response.status_code == 201, response.text
        attempt = response.json()["attempt"]
        config = {**team["config"], "max_active_grunts": 1}
        response = await client.put(
            f"{PREFIX}/teams/{team_id}/configuration",
            headers=headers(brain),
            json={**fence, "config": config},
        )
        assert response.status_code == 200 and response.json()["config_revision"] == 2
        now[0] += 61
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/lease",
            headers=headers(brain),
            json={"controller_id": uuid4().hex},
        )
        replacement = {"controller_id": response.json()["controller_id"], "epoch": 2}
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/attempts/{attempt['attempt_id']}/reattach",
            headers=headers(brain),
            json=fence,
        )
        assert response.status_code == 409
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/attempts/{attempt['attempt_id']}/reattach",
            headers=headers(brain),
            json=replacement,
        )
        assert response.status_code == 200 and response.json()["epoch"] == 2
        close = {
            **replacement,
            "attempt_id": attempt["attempt_id"],
            "outcome": "cancelled",
            "stop_confirmed": True,
        }
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/attempts/close",
            headers=headers(brain),
            json={**close, "stop_confirmed": False},
        )
        assert response.status_code == 422
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/attempts/close", headers=headers(brain), json=close
        )
        assert response.status_code == 200 and response.json()["state"] == "cancelled"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application("-session_orchestration")),
        base_url="http://test",
    ) as client:
        response = await client.get(f"{PREFIX}/capabilities", headers=headers(brain))
        assert response.status_code == 403


async def test_http_strict_payloads_cannot_forge_sender(bgr_environment):
    application, brain, worker, _, _ = bgr_environment
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()), base_url="http://test"
    ) as client:
        team, fence = await create_team(client, brain)
        body = task_request(worker(), fence)
        response = await client.post(
            f"{PREFIX}/teams/{team['team_id']}/tasks", headers=headers(brain), json=body
        )
        task = response.json()
        query = {
            **fence,
            "message_id": uuid4().hex,
            "recipient_agent_id": task["attempt"]["agent_id"],
            "task_id": task["task_id"],
            "attempt_id": task["attempt"]["attempt_id"],
            "kind": "question",
            "correlation_id": uuid4().hex,
            "payload": {"question": "Ready?"},
        }
        for extra in (
            {"sender_agent_id": team["brain_agent_id"]},
            {"payload": {"data": "x" * 8001}},
            {"correlation_id": None},
        ):
            response = await client.post(
                f"{PREFIX}/teams/{team['team_id']}/envelopes",
                headers=headers(brain),
                json={**query, **extra},
            )
            assert response.status_code == 422, response.text
        response = await client.post(
            f"{PREFIX}/teams/{team['team_id']}/envelopes",
            headers=headers(brain),
            json={**query, "controller_id": uuid4().hex},
        )
        assert response.status_code == 409
        response = await client.get(
            f"{PREFIX}/teams/{team['team_id']}/mailbox?limit=101", headers=headers(brain)
        )
        assert response.status_code == 422
        response = await client.get(f"{PREFIX}/teams/bad-id", headers=headers(brain))
        assert response.status_code == 422
        assert "BGR" not in json.dumps(
            (await client.get("/v1/agents", headers=headers(brain))).json()
        )


async def test_http_stale_mailbox_reconciliation_survives_restart(bgr_environment):
    application, brain, worker, _, now = bgr_environment
    child = worker()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()), base_url="http://test"
    ) as client:
        team, fence = await create_team(client, brain)
        team_id = team["team_id"]
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/tasks",
            headers=headers(brain),
            json=task_request(child, fence),
        )
        assert response.status_code == 201, response.text
        task = response.json()
        query = {
            **fence,
            "message_id": uuid4().hex,
            "recipient_agent_id": task["attempt"]["agent_id"],
            "task_id": task["task_id"],
            "attempt_id": task["attempt"]["attempt_id"],
            "kind": "status_query",
            "correlation_id": uuid4().hex,
            "payload": {"scope": "now"},
        }
        for _ in range(49):
            query = {**query, "message_id": uuid4().hex}
            response = await client.post(
                f"{PREFIX}/teams/{team_id}/envelopes", headers=headers(brain), json=query
            )
            assert response.status_code == 202, response.text
        original = (
            await client.get(f"{PREFIX}/teams/{team_id}/mailbox", headers=headers(child))
        ).json()
        assert len(original) == 50
        now[0] += 61
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/lease",
            headers=headers(brain),
            json={"controller_id": uuid4().hex},
        )
        assert response.status_code == 200, response.text
        fence = {
            "epoch": response.json()["epoch"],
            "controller_id": response.json()["controller_id"],
        }
        assert fence["epoch"] == 2
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/attempts/{task['attempt']['attempt_id']}/reattach",
            headers=headers(brain),
            json=fence,
        )
        assert response.status_code == 200, response.text
        action = {
            "epoch": 2,
            "envelope_ids": [entry["envelope_id"] for entry in original],
            "reason": "Recovery confirmed survivor; discard superseded deliveries",
        }
        path = f"{PREFIX}/teams/{team_id}/mailbox/discard"
        for source, user, changes, expected in (
            (brain, "alice", {}, 403),
            (child, "bob", {}, 403),
            (child, "alice", {"epoch": 1}, 409),
            (child, "alice", {"envelope_ids": []}, 422),
            (child, "alice", {"envelope_ids": [uuid4().hex for _ in range(101)]}, 422),
            (child, "alice", {"envelope_ids": [original[0]["envelope_id"]] * 2}, 422),
        ):
            response = await client.post(
                path, headers=headers(source, user), json={**action, **changes}
            )
            assert response.status_code == expected, response.text
        for epoch in (1, 2):
            response = await client.post(
                f"{PREFIX}/teams/{team_id}/mailbox/ack",
                headers=headers(child),
                json={"epoch": epoch, "envelope_id": original[0]["envelope_id"]},
            )
            assert response.status_code == 409, response.text
        response = await client.post(path, headers=headers(child), json=action)
        assert response.status_code == 200, response.text
        discarded = response.json()
        assert len(discarded) == 50
        for before, after in zip(original, discarded, strict=True):
            assert {key: value for key, value in after.items() if key != "discard"} == {
                key: value for key, value in before.items() if key != "discard"
            }
            assert after["consumed_at"] is None and after["replied_at"] is None
            assert after["discard"] == {
                "epoch": 2,
                "session_id": child,
                "reason": action["reason"],
                "discarded_at": now[0],
            }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application()), base_url="http://test"
    ) as client:
        response = await client.post(path, headers=headers(child), json=action)
        assert response.status_code == 200 and response.json() == discarded
        assert (
            await client.get(f"{PREFIX}/teams/{team_id}/mailbox", headers=headers(child))
        ).json() == []
        reply = {
            **query,
            "epoch": 2,
            "controller_id": None,
            "message_id": uuid4().hex,
            "recipient_agent_id": team["brain_agent_id"],
            "kind": "status_reply",
            "in_reply_to": query["message_id"],
            "payload": {"summary": "Stale callback"},
        }
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/envelopes", headers=headers(child), json=reply
        )
        assert response.status_code == 409, response.text
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/envelopes",
            headers=headers(brain),
            json={**query, **fence, "message_id": uuid4().hex},
        )
        assert response.status_code == 202, response.text
        receipt = response.json()
        delivery = (
            await client.get(f"{PREFIX}/teams/{team_id}/mailbox", headers=headers(child))
        ).json()
        assert len(delivery) == 1 and delivery[0]["envelope_id"] == receipt["envelope_id"]
        response = await client.post(
            f"{PREFIX}/teams/{team_id}/mailbox/ack",
            headers=headers(child),
            json={"epoch": 2, "envelope_id": receipt["envelope_id"]},
        )
        assert response.status_code == 200 and response.json()["consumed_at"] is not None
        assert response.json()["discard"] is None
        snapshot = (await client.get(f"{PREFIX}/teams/{team_id}", headers=headers(brain))).json()
        assert snapshot["pending_envelopes"] == 0


@pytest.mark.parametrize("role", ["brain", "grunt"])
@pytest.mark.parametrize("lost", ["expired", "recovery"])
async def test_public_input_and_mcp_deny_durable_roles_after_authority_loss(
    bgr_environment, db_uri, role, lost
):
    application, brain, worker, permissions, now = bgr_environment
    app = application()
    conversations = SqlAlchemyConversationStore(db_uri)
    agents = SqlAlchemyAgentStore(db_uri)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        team, fence_data = await create_team(client, brain)
        team_id = team["team_id"]
        store = app.state.bgr_store
        fence = Fence.model_validate(fence_data)
        binding = NativeBrainBinding(
            team_id=team_id, epoch=1, thread_id=str(uuid4()), runtime_id=uuid4().hex, host_id=HOST
        )
        store.native_brain_binding("alice", brain, team_id, fence, binding=binding)
        session_id = brain
        if role == "grunt":
            session_id = worker()
            response = await client.post(
                f"{PREFIX}/teams/{team_id}/tasks",
                headers=headers(brain),
                json={
                    **fence_data,
                    "task_id": uuid4().hex,
                    "session_id": session_id,
                    "contract": {
                        "objective": "Test input fencing",
                        "instructions": "Do not execute after authority loss",
                        "acceptance": ["No stale execution"],
                    },
                    "workspace": {
                        "host_id": HOST,
                        "repository": "repo",
                        "worktree": f"/work/{session_id}",
                        "base_sha": SHA,
                    },
                },
            )
            assert response.status_code == 201, response.text
        if lost == "expired":
            now[0] += 301
        else:
            store.begin_runtime_recovery(
                "alice",
                brain,
                team_id,
                fence,
                recovery_id=uuid4().hex,
                runtime_id=binding.runtime_id,
                replacement_controller_id=uuid4().hex,
            )
        for event in ("message", "stop_session", "retry_session"):
            response = await client.post(
                f"/v1/sessions/{session_id}/events",
                headers={"X-Forwarded-Email": "alice"},
                json={"type": event, "data": {"role": "user", "content": "Do not execute"}},
            )
            assert response.status_code == 409, response.text
        request = Request(
            {"type": "http", "app": app, "headers": [(b"x-forwarded-email", b"alice")]}
        )
        with pytest.raises(OmnigentError):
            await _handle_mcp_tools_call(
                1,
                session_id,
                {"name": "sys_os_write", "arguments": {}},
                conversations,
                agents,
                None,
                request=request,
            )
        policy = McpPolicyAdapter(
            conversations, agents, UnifiedAuthProvider(source="header"), permissions
        )
        with pytest.raises(OmnigentError):
            await policy.authorize(request, session_id, "unavailable-service")
        ordinary = worker()
        conversations.set_labels(ordinary, {"omnigent.bgr.team_id": team_id})
        await check_bgr_session_input(app.state, ordinary, restart=True)


@pytest.mark.parametrize("host_result", ["unknown", "unavailable", "error", "foreign", "positive"])
@pytest.mark.parametrize("cached", ["absent", "old", "newer"])
async def test_owner_recovery_retires_only_old_controller_before_host_rpc(
    bgr_environment, monkeypatch, host_result, cached
):
    application, brain, _, permissions, now = bgr_environment
    app = application()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        team, fence_data = await create_team(client, brain)
        team_id = team["team_id"]
        store = app.state.bgr_store
        binding = NativeBrainBinding(
            team_id=team_id, epoch=1, thread_id=str(uuid4()), runtime_id=uuid4().hex, host_id=HOST
        )
        store.native_brain_binding(
            "alice", brain, team_id, Fence.model_validate(fence_data), binding=binding
        )
        with pytest.raises(OmnigentError, match="not attached"):
            app.state.bgr_controllers.get(brain)
        registry = app.state.bgr_controllers
        heartbeat = Mock()
        controller = SimpleNamespace(
            fence=Fence.model_validate(fence_data)
            if cached == "old"
            else Fence(controller_id=uuid4().hex, epoch=2),
            _heartbeat=heartbeat,
        )
        if cached != "absent":
            registry._controllers[brain] = controller
        now[0] += 301
        operations = []

        async def handle(request):
            assert request.url.path == f"/v1/sessions/{brain}/bgr/runtime"
            current = store.get_team("alice", brain, team_id)
            assert current.runtime_recoveries[-1].state == "stopping"
            assert current.epoch == 1 and current.lease_expires_at == 0
            assert not registry._attach_lock.locked()
            if cached == "newer":
                assert registry.get(brain) is controller
            else:
                with pytest.raises(OmnigentError, match="not attached"):
                    registry.get(brain)
            with pytest.raises(OmnigentError):
                store.check_session_runtime_input(brain)
            payload = json.loads(request.content)
            operations.append(payload["operation"])
            if host_result == "error":
                raise httpx.ConnectError("private host detail", request=request)
            if payload["operation"] == "brain":
                return httpx.Response(
                    200,
                    json={
                        "thread_id": binding.thread_id,
                        "host_id": binding.host_id,
                        "runtime_id": uuid4().hex
                        if host_result == "foreign"
                        else binding.runtime_id,
                    },
                )
            assert payload == {
                "operation": "stop_generation",
                "parameters": {"runtime_id": binding.runtime_id},
            }
            return httpx.Response(200, json={"domain_stop_confirmed": host_result == "positive"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://bound-runner"
        ) as host:

            async def get_runner(session_id, router):
                assert session_id == brain
                return None if host_result == "unavailable" else host

            monkeypatch.setattr("omnigent.server.routes.sessions._get_runner_client", get_runner)
            url = f"{PREFIX}/teams/{team_id}/recover"
            owner = {"X-Forwarded-Email": "alice"}
            for forbidden in (
                {"evidence": {"domain_stop_confirmed": True}},
                {"runtime_id": uuid4().hex},
            ):
                response = await client.post(url, headers=owner, json=forbidden)
                assert response.status_code == 422
                assert store.get_team("alice", brain, team_id).runtime_recoveries == []
            response = await client.post(url, headers={"X-Forwarded-Email": "bob"}, json={})
            assert response.status_code == 403
            assert store.get_team("alice", brain, team_id).runtime_recoveries == []
            heartbeat.cancel.assert_not_called()
            response = await client.post(url, headers=owner, json={})
            assert response.status_code == 202, response.text
            result = response.json()
            assert result["stop_status"] == "unknown" and not result["replacement_enabled"]
            assert result["recovery"]["state"] == "stopping"
            assert "private host detail" not in response.text
            again = await client.post(url, headers=owner, json={})
            assert again.status_code == 202 and again.json() == result
            assert heartbeat.cancel.call_count == (1 if cached == "old" else 0)
            if cached == "newer":
                assert registry.get(brain) is controller
            else:
                with pytest.raises(OmnigentError, match="not attached"):
                    registry.get(brain)
            permissions.revoke("alice", brain)
            denied = await client.post(url, headers=owner, json={})
            assert denied.status_code == 404
        expected = [] if host_result == "unavailable" else ["brain"]
        if host_result in {"unknown", "positive"}:
            expected.append("stop_generation")
        assert operations == expected * 2
        after = store.get_team("alice", brain, team_id)
        assert after.epoch == 1 and after.lease_expires_at == 0
        assert len(after.runtime_recoveries) == 1
