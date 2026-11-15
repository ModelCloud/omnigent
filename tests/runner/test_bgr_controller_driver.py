"""Driver exercises real tool contracts, not native/provider execution evidence."""

import hashlib
import json
from uuid import uuid4

import pytest

from dev import bgr_controller_driver as driver
from omnigent.bgr_control import BgrControl

CONTROL = "sys_bgr_control"
NAMESPACE = "mcp__omnigent"
TOOLS = [
    {
        "type": "namespace",
        "name": NAMESPACE,
        "tools": [{"type": "function", "name": CONTROL}],
    },
    *[
        {"type": "function", "name": name}
        for name in ("bgr_reply_message", "bgr_send_message", "bgr_report_status")
    ],
]


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

    def run(*inputs, thread_id=thread, tools=TOOLS):
        return driver.respond(
            {
                "prompt_cache_key": thread_id,
                "tools": tools,
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
        assert call["namespace"] == NAMESPACE
        assert isinstance(call["call_id"], str) and call["call_id"]
        assert call["type"] == "function_call"
        BgrControl.model_validate(arguments)
    else:
        assert "namespace" not in call
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
@pytest.mark.parametrize("outcome", ["accept", "revise", "rereview"])
def test_driver_waits_for_siblings_and_approves_each_exact_preview(drive, publish, outcome):
    create = drive()
    next(iter(driver._STATES.values()))["plan"]["publish"] = publish
    attempt = {"agent_id": uuid4().hex, "attempt_id": uuid4().hex}
    adjudicate = drive(
        output(create, {"task": {"attempt": attempt}}),
        envelope("result", attempt["agent_id"], "done"),
    )
    start = drive(output(adjudicate, {"attempt": {**attempt, "state": "completed"}}))
    assert command(start)[1]["action"] == "start_review"
    jobs = [
        {"agent_id": uuid4().hex, "review_id": uuid4().hex, "attempt_id": uuid4().hex}
        for _ in range(2)
    ]
    round = {"round_id": uuid4().hex, "results_ready": False, "jobs": jobs}
    assert (
        "Waiting"
        in drive(output(start, round), envelope("result", jobs[0]["agent_id"], "review-a"))["text"]
    )
    status = drive(envelope("result", jobs[1]["agent_id"], "review-b"))
    assert command(status)[1]["action"] == "status"
    results = {
        **round,
        "results_ready": True,
        "results_sha256": "a" * 64,
        "jobs": [
            {
                **job,
                "result": {
                    "status": "incomplete" if outcome == "rereview" else "complete",
                    "summary": "Inspected the frozen snapshot"
                    if outcome != "rereview"
                    else "Could not inspect the parser",
                    "findings": [
                        {
                            "severity": "high",
                            "path": "parser.py",
                            "description": "Parser drops malformed input",
                        }
                    ]
                    if outcome == "revise"
                    else [],
                },
            }
            for job in jobs
        ],
    }
    evaluation = drive(output(status, results))
    _, decision = command(evaluation)
    assert decision["action"] == "evaluate_reviews"
    assert decision["evaluation"]["next_action"] == outcome
    assert decision["evaluation"]["results_sha256"] == results["results_sha256"]
    if outcome == "revise":
        assert "Parser drops malformed input" in decision["evaluation"]["rationale"]
    if outcome == "rereview":
        assert "Could not inspect the parser" in decision["evaluation"]["rationale"]
    preview = drive(
        output(
            evaluation,
            {"decision": {**decision["evaluation"], "action_id": decision["action_id"]}},
        )
    )
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
            if "items" in preview and job != jobs[-1]:
                assert command(preview)[1]["action"] == "publication_preview"
    assert command(preview)[1]["action"] == "status"
    assert "round_id" not in command(preview)[1]
    preview = drive(
        output(
            preview,
            {
                "tasks": [
                    {"attempt": {**job, "review_round_id": round["round_id"]}} for job in jobs
                ]
                + [{"attempt": attempt}],
            },
        )
    )
    for job in jobs:
        assert command(preview)[1]["action"] == "adjudicate"
        assert command(preview)[1]["attempt_id"] == job["attempt_id"]
        preview = drive(output(preview, {"attempt": {**job, "state": "completed"}}))
    if outcome == "accept":
        assert "finished" in preview["text"]
        assert "Reviewers stopped" in preview["text"]
    elif outcome == "revise":
        assert command(preview)[1]["action"] == "create_grunt"
        assert "Parser drops malformed input" in command(preview)[1]["contract"]["instructions"]
        state = next(iter(driver._STATES.values()))
        assert state["revision_started"] is True
        # An exhausted bounded fixture must not silently accept unresolved findings.
        state["pending"] = None
        assert "new immutable head" in drive()["text"]
    else:
        assert command(preview)[1]["action"] == "start_review"
        state = next(iter(driver._STATES.values()))
        state["pending"] = None
        state["review_rounds"] = 2
        assert "bounded re-review exhausted" in drive()["text"]


def test_driver_refuses_uncertain_control_without_new_action(drive):
    create = drive()
    with pytest.raises(ValueError, match="missing or uncertain"):
        drive()
    assert next(iter(driver._STATES.values()))["pending"]["arguments"] == command(create)[1]


@pytest.mark.parametrize(
    "tools",
    [
        [],
        TOOLS[:1],
        TOOLS[1:],
        [{"type": "function", "name": CONTROL}, *TOOLS[1:]],
        [{"type": "function", "name": f"{NAMESPACE}__{CONTROL}"}, *TOOLS[1:]],
        [{**TOOLS[0], "name": "mcp__other"}, *TOOLS[1:]],
        [TOOLS[0], *TOOLS],
        [
            {**TOOLS[0], "tools": [{"type": "function", "name": f"{NAMESPACE}__{CONTROL}"}]},
            *TOOLS[1:],
        ],
    ],
    ids=[
        "none",
        "no-reply",
        "no-control",
        "unscoped",
        "flattened",
        "foreign",
        "duplicate",
        "flattened-in-namespace",
    ],
)
def test_driver_refuses_missing_advertised_tools(drive, tools):
    with pytest.raises(ValueError, match="actual control"):
        drive(tools=tools)
