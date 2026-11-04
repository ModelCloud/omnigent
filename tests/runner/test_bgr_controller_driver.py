"""Driver exercises real tool contracts, not native/provider execution evidence."""

import hashlib
import json
from uuid import uuid4

import pytest

from dev import bgr_controller_driver as driver
from omnigent.bgr_control import BgrControl

CONTROL = "mcp__omnigent__sys_bgr_control"


@pytest.fixture
def drive(tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "repository": "ModelCloud/omnigent",
                "pr_number": 98,
                "base_sha": "d" * 40,
                "head_sha": "e" * 40,
                "grunts": 1,
                "publish": True,
            }
        )
    )
    monkeypatch.setenv("BGR_FIXTURE_PLAN", str(plan))
    driver._STATES.clear()
    thread = str(uuid4())

    def run(*inputs, thread_id=thread):
        return driver.respond(
            {
                "prompt_cache_key": thread_id,
                "tools": [{"name": CONTROL}, {"name": "bgr_reply_message"}],
                "input": list(inputs),
            },
            {},
        )

    yield run
    driver._STATES.clear()


def command(response):
    call = response["items"][0]
    arguments = json.loads(call["arguments"])
    if call["name"] == CONTROL:
        BgrControl.model_validate(arguments)
    return call, arguments


def output(response, value):
    return {
        "type": "function_call_output",
        "call_id": response["items"][0]["call_id"],
        "output": json.dumps({"content": [{"type": "text", "text": json.dumps(value)}]}),
    }


def envelope(kind, actor, message_id):
    return {
        "identity": {"role": "grunt", "agentId": actor},
        "recipientIdentity": {"role": "brain", "agentId": uuid4().hex},
        "kind": kind,
        "messageId": message_id,
    }


def test_driver_uses_thread_identity_and_actual_question_anchor(drive):
    create = drive()
    _, arguments = command(create)
    assert arguments["action"] == "create_grunt"
    attempt = {"agent_id": uuid4().hex, "attempt_id": uuid4().hex}
    question_id = "native-question-" + uuid4().hex
    reply = drive(
        output(create, {"task": {"attempt": attempt}}),
        json.dumps(envelope("question", attempt["agent_id"], question_id)),
    )
    call, arguments = command(reply)
    assert call["name"] == "bgr_reply_message"
    assert arguments["inReplyTo"] == question_id
    assert "Waiting" in drive()["text"]
    adjudicate = drive(envelope("result", attempt["agent_id"], "done"))
    _, arguments = command(adjudicate)
    assert arguments["action"] == "adjudicate"
    assert arguments["attempt_id"] == attempt["attempt_id"]
    assert command(drive(thread_id=str(uuid4())))[1]["action"] == "create_grunt"


@pytest.mark.parametrize("publish", [False, True])
def test_driver_waits_for_siblings_and_approves_each_exact_preview(drive, publish):
    create = drive()
    next(iter(driver._STATES.values()))["plan"]["publish"] = publish
    attempt = {"agent_id": uuid4().hex, "attempt_id": uuid4().hex}
    adjudicate = drive(
        output(create, {"task": {"attempt": attempt}}),
        envelope("result", attempt["agent_id"], "done"),
    )
    start = drive(output(adjudicate, {"attempt": {**attempt, "state": "completed"}}))
    assert command(start)[1]["action"] == "start_review"
    jobs = [{"agent_id": uuid4().hex, "review_id": uuid4().hex} for _ in range(2)]
    round = {"round_id": uuid4().hex, "results_ready": False, "jobs": jobs}
    assert (
        "Waiting"
        in drive(output(start, round), envelope("result", jobs[0]["agent_id"], "review-a"))["text"]
    )
    status = drive(envelope("result", jobs[1]["agent_id"], "review-b"))
    assert command(status)[1]["action"] == "status"
    preview = drive(output(status, {**round, "results_ready": True}))
    for job in jobs:
        assert command(preview)[1]["review_id"] == job["review_id"]
        body = "sanitized review " + job["review_id"]
        digest = hashlib.sha256(body.encode()).hexdigest()
        result = drive(output(preview, {"body": body, "approved_body_sha256": digest}))
        if publish:
            assert command(result)[1]["action"] == "publish"
            assert command(result)[1]["approved_body_sha256"] == digest
            preview = drive(output(result, {"publication": {"state": "published"}}))
        else:
            preview = result
            if "items" in preview:
                assert command(preview)[1]["action"] == "publication_preview"
    assert "finished" in preview["text"]


def test_driver_refuses_uncertain_control_without_new_action(drive):
    create = drive()
    with pytest.raises(ValueError, match="missing or uncertain"):
        drive()
    assert next(iter(driver._STATES.values()))["pending"]["arguments"] == command(create)[1]


def test_driver_refuses_missing_advertised_tools(drive):
    drive()
    with pytest.raises(ValueError, match="actual control"):
        driver.respond({"prompt_cache_key": next(iter(driver._STATES)), "tools": []}, {})
