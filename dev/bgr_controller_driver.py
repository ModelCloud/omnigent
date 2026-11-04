"""Local provider fixture driver; BGR_FIXTURE_PLAN names a JSON plan, never credentials.

Run the native package's provider_fixture.py --serve --port 18083 --brain-driver
dev/bgr_controller_driver.py with a plan containing repository, pr_number, base_sha,
head_sha, grunts (default 2), and publish (default false). Publication is opt-in.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

_STATES: dict[str, dict] = {}
_LOCK = threading.RLock()


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)
    elif isinstance(value, str):
        decoder = json.JSONDecoder()
        for index, character in enumerate(value):
            if character == "{":
                try:
                    child, _ = decoder.raw_decode(value[index:])
                except ValueError:
                    continue
                yield from _objects(child)


def _call(name, arguments, call_id, *, namespace=None):
    item = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }
    if namespace is not None:
        item["namespace"] = namespace
    return {
        "items": [item],
    }


def respond(request: dict, _state: dict) -> dict:
    with _LOCK:
        return _respond(request)


def _respond(request: dict) -> dict:
    thread = request["prompt_cache_key"]
    if thread not in _STATES:
        plan = json.loads(Path(os.environ["BGR_FIXTURE_PLAN"]).read_text())
        if type(plan.get("grunts", 2)) is not int or not 1 <= plan.get("grunts", 2) <= 4:
            raise ValueError("Fixture requires one to four Grunts")
        _STATES[thread] = {
            "plan": plan,
            "grunts": [],
            "closed": set(),
            "answered": set(),
            "results": set(),
            "previewed": set(),
            "published": set(),
            "pending": None,
            "round": None,
            "review_attempts": None,
            "decision": None,
            "review_rounds": 0,
            "revision_started": False,
        }
    state = _STATES[thread]
    advertised = request.get("tools", [])
    tools = {tool.get("name") for tool in advertised if tool.get("type") == "function"}
    controls = [
        (namespace["name"], tool["name"])
        for namespace in advertised
        if namespace.get("type") == "namespace" and namespace.get("name") == "mcp__omnigent"
        for tool in namespace.get("tools", [])
        if tool.get("type") == "function" and tool.get("name") == "sys_bgr_control"
    ]
    if len(controls) != 1 or "bgr_reply_message" not in tools:
        raise ValueError("Brain must advertise the actual control and native reply tools")
    objects = list(_objects(request.get("input", [])))
    pending = state["pending"]
    if pending:
        output = next(
            (
                item
                for item in reversed(objects)
                if item.get("type") == "function_call_output"
                and item.get("call_id") == pending["call_id"]
            ),
            None,
        )
        if output:
            responses = list(_objects(output["output"]))
            action = pending["arguments"]["action"]
            if action == "create_grunt":
                response = next(item for item in responses if "task" in item)
                state["grunts"].append(response["task"]["attempt"])
            elif action == "adjudicate":
                response = next(item for item in responses if "attempt" in item)
                if response["attempt"]["state"] != "completed":
                    raise ValueError("Controller did not confirm completed adjudication")
                state["closed"].add(response["attempt"]["attempt_id"])
            elif action in {"start_review", "status"}:
                if action == "status" and "round_id" not in pending["arguments"]:
                    response = next(item for item in responses if "tasks" in item)
                    state["review_attempts"] = [
                        task["attempt"]
                        for task in response["tasks"]
                        if task["attempt"].get("review_round_id") == state["round"]["round_id"]
                    ]
                    if len(state["review_attempts"]) != len(state["round"]["jobs"]):
                        raise ValueError("Reviewer shutdown requires every durable assignment")
                else:
                    response = next(item for item in responses if "round_id" in item)
                    state["round"] = response
                    if action == "start_review":
                        state["review_rounds"] += 1
                        state["decision"] = None
                        state["review_attempts"] = None
            elif action == "evaluate_reviews":
                decision = next(item["decision"] for item in responses if "decision" in item)
                evaluation = pending["arguments"]["evaluation"]
                if (
                    any(
                        decision.get(key) != evaluation[key]
                        for key in ("results_sha256", "next_action")
                    )
                    or decision.get("action_id") != pending["arguments"]["action_id"]
                ):
                    raise ValueError("Controller did not record this exact review evaluation")
                state["decision"] = decision
            elif action == "publication_preview":
                response = next(item for item in responses if "approved_body_sha256" in item)
                if (
                    hashlib.sha256(response["body"].encode()).hexdigest()
                    != response["approved_body_sha256"]
                ):
                    raise ValueError("Sanitized publication preview does not match its hash")
                state["preview"] = (pending["arguments"]["review_id"], response)
                state["previewed"].add(pending["arguments"]["review_id"])
            elif action == "publish":
                response = next(item for item in responses if "publication" in item)
                if response["publication"]["state"] != "published":
                    raise ValueError("Publication is uncertain; do not issue a fresh write")
                state["published"].add(pending["arguments"]["review_id"])
                state.pop("preview", None)
            state["pending"] = None
    for envelope in objects:
        if not {"identity", "recipientIdentity", "messageId", "kind"} <= envelope.keys():
            continue
        if envelope["recipientIdentity"].get("role") != "brain":
            continue
        if envelope["kind"] == "result":
            state["results"].add(envelope["identity"]["agentId"])
        if envelope["kind"] == "question" and envelope["messageId"] not in state["answered"]:
            state["answered"].add(envelope["messageId"])
            return _call(
                "bgr_reply_message",
                {
                    "messageId": "answer-" + uuid4().hex,
                    "inReplyTo": envelope["messageId"],
                    "kind": "answer",
                    "payload": {
                        "text": "Yes, inspect the immutable supplied head and report evidence."
                    },
                },
                "answer-call-" + uuid4().hex,
            )
    if state["pending"]:
        raise ValueError("Controller output is missing or uncertain; reconcile the same action")

    def control(action, **arguments):
        command = {"action": action, "action_id": uuid4().hex, **arguments}
        pending = {"arguments": command, "call_id": "control-" + command["action_id"]}
        state["pending"] = pending
        namespace, name = controls[0]
        return _call(name, command, pending["call_id"], namespace=namespace)

    plan = state["plan"]
    if len(state["grunts"]) < plan.get("grunts", 2):
        return control(
            "create_grunt",
            base_sha=plan["base_sha"],
            contract={
                "objective": "Inspect the assigned fixture worktree",
                "instructions": "Ask the Brain for clarification, then return a bounded result.",
                "acceptance": ["Question answered and result returned"],
            },
        )
    for grunt in state["grunts"]:
        if grunt["agent_id"] not in state["results"]:
            return {"text": "Waiting for the Grunt's asynchronous evidence."}
        if grunt["attempt_id"] not in state["closed"]:
            return control("adjudicate", attempt_id=grunt["attempt_id"], outcome="completed")
    if state["round"] is None:
        return control(
            "start_review",
            **{
                key: plan[key]
                for key in (
                    "repository",
                    "pr_number",
                    "base_sha",
                    "head_sha",
                )
            },
        )
    round = state["round"]
    if not round["results_ready"]:
        if all(job["agent_id"] in state["results"] for job in round["jobs"]):
            return control("status", round_id=round["round_id"])
        return {"text": "Waiting for all independent reviewers; sealed results remain withheld."}
    if state["decision"] is None:
        if not all(
            isinstance(job.get("result"), dict)
            and job["result"].get("status") in {"complete", "incomplete"}
            and isinstance(job["result"].get("findings"), list)
            for job in round["jobs"]
        ):
            raise ValueError("Evaluation requires actual sealed reviewer results")
        incomplete = [job for job in round["jobs"] if job["result"]["status"] == "incomplete"]
        findings = [finding for job in round["jobs"] for finding in job["result"]["findings"]]
        if incomplete:
            next_action = "rereview"
            rationale = "Incomplete coverage: " + "; ".join(
                f"{job['review_id']}: {job['result']['summary']}" for job in incomplete
            )
        elif findings:
            next_action = "revise"
            rationale = "Reported findings: " + "; ".join(
                f"{finding['path']}: {finding['description']}" for finding in findings
            )
        else:
            next_action = "accept"
            rationale = "All independent reviews are complete and report no findings."
        return control(
            "evaluate_reviews",
            round_id=round["round_id"],
            evaluation={
                "results_sha256": round["results_sha256"],
                "next_action": next_action,
                "rationale": rationale[:2000],
            },
        )
    for job in round["jobs"]:
        if plan.get("publish", False):
            if job["review_id"] in state["published"]:
                continue
            if "preview" not in state:
                return control(
                    "publication_preview", round_id=round["round_id"], review_id=job["review_id"]
                )
            review_id, preview = state["preview"]
            return control(
                "publish",
                round_id=round["round_id"],
                review_id=review_id,
                approved_body_sha256=preview["approved_body_sha256"],
            )
        if job["review_id"] not in state["previewed"]:
            return control(
                "publication_preview", round_id=round["round_id"], review_id=job["review_id"]
            )
    if state["review_attempts"] is None:
        return control("status")
    for attempt in state["review_attempts"]:
        if attempt["attempt_id"] not in state["closed"]:
            return control("adjudicate", attempt_id=attempt["attempt_id"], outcome="completed")
    decision = state["decision"]
    if decision["next_action"] == "rereview":
        if state["review_rounds"] < 2:
            return control(
                "start_review",
                **{key: plan[key] for key in ("repository", "pr_number", "base_sha", "head_sha")},
            )
        return {
            "text": "BGR fixture blocked: bounded re-review exhausted; "
            "coverage remains incomplete."
        }
    if decision["next_action"] == "revise":
        if not state["revision_started"]:
            state["revision_started"] = True
            return control(
                "create_grunt",
                base_sha=plan["head_sha"],
                contract={
                    "objective": "Address the sealed independent review findings",
                    "instructions": decision["rationale"],
                    "acceptance": [
                        "Return revision evidence and a new immutable head "
                        "for independent re-review"
                    ],
                },
            )
        return {
            "text": "BGR fixture blocked: revision evidence requires a new immutable head "
            "and independent review; original findings remain unaccepted."
        }
    return {
        "text": "BGR fixture finished: Grunts and Reviewers stopped, independent reviews sealed, "
        "and sanitized publication previews checked."
    }
