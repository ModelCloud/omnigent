"""BGR launch routes with real stores and explicit native-host frame responses."""

from uuid import uuid4

import pytest

from omnigent.host.frames import (
    CAP_BGR_PROBE,
    HostLaunchRunnerFrame,
    HostModelOptionsFrame,
    HostProbeBgrFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.server.auth import LEVEL_READ
from omnigent.server.sharing_settings import DefaultPublicSessions
from omnigent.stores.bgr_store import BgrStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent
from tests.server.integration.test_managed_startup_inputs import auth_app as auth_app
from tests.server.integration.test_managed_startup_inputs import (
    startup_permissions as startup_permissions,
)
from tests.server.integration.test_sessions_permissions import _register_online_host

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def bgr_launch_env(auth_app, auth_client, db_uri, monkeypatch):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    agent = await create_test_agent(
        auth_client,
        name="bgr-native-brain",
        user="alice",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
        include_llm=False,
    )
    host_id = uuid4().hex
    _register_online_host(auth_app, host_id, "alice")
    auth_app.state.host_store.upsert_on_connect(host_id, host_id, "alice")
    conn = auth_app.state.host_registry.get(host_id)
    conn.hello.capabilities.append(CAP_BGR_PROBE)
    auth_app.state.bgr_store = BgrStore(db_uri)
    auth_app.state.bgr_controllers = object()
    auth_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    frames = []
    row = {"id": "test-model", "providerId": "simulated"}

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        frames.append(frame)
        if isinstance(frame, HostProbeBgrFrame):
            connection.pending_bgr_probes[frame.request_id].set_result(
                {
                    "launch_supported": True,
                    "supported_harnesses": ["codex-native"],
                    "reviewer_isolation": True,
                    "reason": None,
                }
            )
        elif isinstance(frame, HostModelOptionsFrame):
            connection.pending_model_options[frame.request_id].set_result(
                {"status": "ok", "models": [row], "routable_models": ["test-model"]}
            )
        elif isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(auth_app.state.host_registry, "send_text", answer_host)
    profile = {"harness": "codex-native", "provider": "simulated", "model": "test-model"}
    body = {
        "team_id": uuid4().hex,
        "agent_id": agent["id"],
        "workspace": "/work",
        "config": {"brain": profile, "grunt": profile, "reviewers": [profile, profile]},
        "initial_prompt": "Implement the task, then request two independent reviews.",
    }
    return host_id, body, frames, row


async def test_launch_retries_reuse_one_private_brain_and_one_initial_task(
    auth_client, auth_app, db_uri, startup_permissions, bgr_launch_env
):
    host_id, body, frames, _ = bgr_launch_env
    url = f"/v1/hosts/{host_id}/bgr/launch"
    response = await auth_client.post(url, json=body)
    assert response.status_code == 200, response.text
    brain_id = response.json()["brain_session_id"]
    assert brain_id and response.json()["state"] == "starting"
    retry = await auth_client.post(url, json=body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["brain_session_id"] == brain_id
    assert sum(isinstance(frame, HostLaunchRunnerFrame) for frame in frames) == 1
    store = SqlAlchemyConversationStore(db_uri)
    brain = store.get_conversation(brain_id)
    assert brain.host_id == host_id
    assert brain.labels["omnigent.bgr.team_id"] == body["team_id"]
    assert brain.labels["omnigent.startup"] == "pending"
    assert len(store.list_pending_inputs(brain_id)) == 1
    intent = auth_app.state.bgr_store.get_launch("alice", body["team_id"])
    assert intent.brain_session_id == brain_id
    assert (
        auth_app.state.bgr_store.get_team("alice", brain_id, body["team_id"]).brain_session_id
        == brain_id
    )
    assert startup_permissions.get("__public__", brain_id) is None
    startup_permissions.ensure_user("bob")
    startup_permissions.grant("bob", brain_id, LEVEL_READ)
    denied = await auth_client.post(url, json=body)
    assert denied.status_code == 403, denied.text
    assert len(store.list_pending_inputs(brain_id)) == 1
    assert sum(isinstance(frame, HostLaunchRunnerFrame) for frame in frames) == 1


@pytest.mark.parametrize("role", ["brain", "grunt", "reviewer-a", "reviewer-b"])
async def test_launch_rejects_unadvertised_profiles_before_creating_brain(
    auth_client, auth_app, bgr_launch_env, role
):
    host_id, body, frames, _ = bgr_launch_env
    config = body["config"]
    if role.startswith("reviewer-"):
        index = 0 if role == "reviewer-a" else 1
        config["reviewers"][index] = {**config["reviewers"][index], "provider": "not-configured"}
    else:
        config[role] = {**config[role], "provider": "not-configured"}
    response = await auth_client.post(f"/v1/hosts/{host_id}/bgr/launch", json=body)
    assert response.status_code == 503, response.text
    assert not any(isinstance(frame, HostLaunchRunnerFrame) for frame in frames)
    from omnigent.errors import OmnigentError

    with pytest.raises(OmnigentError):
        auth_app.state.bgr_store.get_launch("alice", body["team_id"])


async def test_capability_and_launch_check_host_ownership_before_native_probe(
    auth_client, bgr_launch_env
):
    host_id, body, frames, _ = bgr_launch_env
    auth_client.headers["X-Forwarded-Email"] = "bob"
    for method, suffix in [("GET", "capabilities"), ("POST", "launch")]:
        response = await auth_client.request(
            method,
            f"/v1/hosts/{host_id}/bgr/{suffix}",
            **({"json": body} if method == "POST" else {}),
        )
        assert response.status_code == 403, response.text
    assert not frames


async def test_capabilities_refuse_older_host_without_sending_unknown_frames(
    auth_client, auth_app, bgr_launch_env
):
    host_id, body, frames, _ = bgr_launch_env
    auth_app.state.host_registry.get(host_id).hello.capabilities.remove(CAP_BGR_PROBE)
    response = await auth_client.get(f"/v1/hosts/{host_id}/bgr/capabilities")
    assert response.status_code == 200, response.text
    assert response.json()["launch_supported"] is False
    launch = await auth_client.post(f"/v1/hosts/{host_id}/bgr/launch", json=body)
    assert launch.status_code == 200, launch.text
    assert launch.json()["state"] == "blocked"
    assert launch.json()["brain_session_id"] is None
    assert not frames
