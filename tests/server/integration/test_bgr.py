"""BGR HTTP contract through create_app, bound runners and real SQL stores."""

import json
from uuid import uuid4

import httpx
import pytest

from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
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
