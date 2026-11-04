"""Real HTTP/SQL ledger boundary with a fake forge; no external PR writes."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from omnigent.bgr_review import publication_body, validate_publication_redactions
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner import bgr_publisher
from omnigent.runner.bgr_publications import RemotePublicationLedger, run_publication
from omnigent.server.routes.bgr_publications import create_bgr_publication_router
from omnigent.server.session_orchestration import ORCHESTRATION_SOURCE_HEADER
from tests.stores.test_bgr_review_store import FENCE, activate, evaluate, message, request
from tests.stores.test_bgr_review_store import forge as forge
from tests.stores.test_bgr_review_store import reviews as reviews
from tests.stores.test_bgr_store import BRAIN, OWNER, TEAM
from tests.stores.test_bgr_store import store as store


@pytest.mark.parametrize(
    "bad",
    [
        "Injected",
        "Header\n\nchanged",
        r"Header\n\n\[REDACTED\]Injected",
        "Header\n\nsecret altered evidence",
        "Header\n\n",
        "Header\n\nfoo secret",
    ],
)
def test_redaction_cannot_rewrite_or_truncate_review(bad):
    with pytest.raises(ValueError):
        validate_publication_redactions("Header\n\nsecret evidence", bad)


def test_redaction_only_preserves_identity_and_order():
    original = "Header\n\nsecret evidence secret trailing"
    redacted = original.replace("secret", r"\[REDACTED\]")
    assert validate_publication_redactions(original, redacted) == redacted
    assert validate_publication_redactions(original, original) == original


@pytest.mark.parametrize("status", ["complete", "incomplete"])
async def test_trusted_host_publication_crosses_real_http_claim_and_reconciles(
    reviews, forge, monkeypatch, status
):
    comments, posts, post = forge
    private = "opaque-host-private"
    monkeypatch.setattr(bgr_publisher, "_private_values", lambda *args: (private,))
    controller = SimpleNamespace(team_id=TEAM, fence=FENCE, authorize=Mock())
    controllers = SimpleNamespace(store=reviews, get=lambda session: controller)

    def source(request):
        if request.headers.get(ORCHESTRATION_SOURCE_HEADER) != BRAIN:
            raise OmnigentError("Foreign source", code=ErrorCode.FORBIDDEN)
        return OWNER, BRAIN

    app = FastAPI()
    app.include_router(create_bgr_publication_router(controllers, source), prefix="/v1/bgr")
    captured = []

    async def capture(request):
        captured.append(request.content.decode())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        event_hooks={"request": [capture]},
    ) as client:
        # A re-review reserves a different immutable job and must create a new comment too.
        for _ in range(2):
            round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
            brain = reviews.get_team(OWNER, BRAIN, TEAM).brain_agent_id
            for job in round.jobs:
                activate(reviews, round, job)
                reviews.send(
                    OWNER,
                    job.session_id,
                    TEAM,
                    message(
                        round,
                        job,
                        brain,
                        payload={
                            "round_id": round.round_id,
                            "review_id": job.review_id,
                            "result": {
                                "status": status,
                                "summary": f"Inspected {private}",
                                "findings": [],
                            },
                        },
                    ),
                )
            round = reviews.get_review_round(OWNER, BRAIN, TEAM, round.round_id)
            evaluate(
                reviews, round, next_action="rereview" if status == "incomplete" else "accept"
            )
            for job in round.jobs:
                parameters = {
                    "team_id": TEAM,
                    "round_id": round.round_id,
                    "review_id": job.review_id,
                    "fence": FENCE.model_dump(mode="json"),
                }

                async def run(operation, parameters=parameters, **extra):
                    return await run_publication(
                        operation,
                        {**parameters, **extra},
                        client=client,
                        session_id=BRAIN,
                        team_id=TEAM,
                        root="/trusted/workspace",
                    )

                preview = await run("publication_preview")
                assert private not in preview["body"]
                approval = preview["approved_body_sha256"]
                with pytest.raises(ValueError, match="approve"):
                    await run("publish", approved_body_sha256="0" * 64)
                before = len(posts)

                def ambiguous(*args, **kwargs):
                    post(*args, **kwargs)
                    raise OSError("response lost")

                monkeypatch.setattr(bgr_publisher.github_resource, "_post_comment", ambiguous)
                first = await run("publish", approved_body_sha256=approval)
                assert first["publication"]["state"] == "uncertain"
                assert len(posts) == before + 1
                second = await run("publication_reconcile", approved_body_sha256=approval)
                assert second["publication"]["state"] == "published"
                assert await run("publish", approved_body_sha256=approval) == second
                assert len(posts) == before + 1
                assert comments[-1]["body"] == preview["body"]
            # A matching hash cannot authorize forged review content.
            body = (
                publication_body(round, round.jobs[0].review_id, private_values=(private,))
                + " forged text"
            )
            with pytest.raises(ValueError, match="cannot change"):
                await client.post(
                    f"/v1/bgr/teams/{TEAM}/publication-ledger",
                    headers={ORCHESTRATION_SOURCE_HEADER: BRAIN},
                    json={
                        "operation": "claim",
                        "round_id": round.round_id,
                        "review_id": round.jobs[0].review_id,
                        "fence": FENCE.model_dump(mode="json"),
                        "claim_id": uuid4().hex,
                        "author_login": "trusted-publisher",
                        "approved_body": body,
                        "approved_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    },
                )
        assert len(posts) == 4
        assert all(private not in data for data in captured if '"operation":"read"' not in data)
        with pytest.raises(OmnigentError, match="Foreign"):
            await client.post(
                f"/v1/bgr/teams/{TEAM}/publication-ledger",
                json={"operation": "read", "round_id": round.round_id},
            )
        ledger = RemotePublicationLedger(client, BRAIN, TEAM)
        with pytest.raises(RuntimeError, match="off the event loop"):
            ledger.get_review_round(OWNER, BRAIN, TEAM, round.round_id)
        with pytest.raises(ValueError, match="Foreign"):
            await asyncio.to_thread(
                ledger.get_review_round, OWNER, uuid4().hex, TEAM, round.round_id
            )


async def test_publication_transport_errors_do_not_expose_response_or_retry_write():
    private = "opaque-response-private"
    calls = []

    async def failing(request):
        calls.append(request)
        return httpx.Response(500, text=private)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(failing), base_url="http://test"
    ) as client:
        ledger = RemotePublicationLedger(client, BRAIN, TEAM)
        with pytest.raises(ValueError, match="reconcile") as raised:
            await asyncio.to_thread(ledger.get_review_round, OWNER, BRAIN, TEAM, uuid4().hex)
        assert private not in str(raised.value)
        assert len(calls) == 1
