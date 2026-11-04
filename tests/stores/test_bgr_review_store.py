"""Trusted-adapter boundary and publisher tests, not model/runtime isolation evidence."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnigent.bgr import ConfigUpdate, Fence, NativeBinding, Send, Workspace
from omnigent.bgr_review import (
    ReviewFinding,
    ReviewResult,
    ReviewRound,
    ReviewRoundCreate,
    publication_body,
    sanitize_review,
)
from omnigent.errors import OmnigentError
from omnigent.runner import bgr_publisher
from omnigent.runner.bgr_publisher import BgrGitHubPublisher
from omnigent.stores.bgr_review_store import BgrReviewStore
from omnigent.stores.bgr_store import BgrStore
from tests.stores.test_bgr_store import BRAIN, CONFIG, CONTROLLER, HOST, OWNER, PROFILE, SHA, TEAM
from tests.stores.test_bgr_store import store as store

FENCE = Fence(controller_id=CONTROLLER, epoch=1)


@pytest.fixture
def reviews(db_uri, store):
    config = CONFIG.model_copy(
        update={"reviewers": [PROFILE, PROFILE.model_copy(update={"model": "independent-model"})]}
    )
    store.configure(OWNER, BRAIN, TEAM, ConfigUpdate(**FENCE.model_dump(), config=config))
    return BgrReviewStore(db_uri)


def request(**overrides):
    return ReviewRoundCreate.model_validate(
        {
            **FENCE.model_dump(),
            "round_id": uuid4().hex,
            "repository": "ModelCloud/omnigent",
            "pr_number": 98,
            "base_sha": SHA,
            "head_sha": "e" * 40,
            **overrides,
        }
    )


def activate(reviews, round, job, **overrides):
    return reviews.activate_reviewer(
        OWNER,
        BRAIN,
        TEAM,
        FENCE,
        round_id=round.round_id,
        review_id=job.review_id,
        **{
            "binding": NativeBinding(
                team_id=TEAM,
                attempt_id=job.attempt_id,
                epoch=1,
                thread_id=str(uuid4()),
            ),
            "workspace": Workspace(
                host_id=HOST,
                repository=round.repository,
                worktree=f"/snapshots/{job.review_id}",
                base_sha=SHA,
            ),
            **overrides,
        },
    )


def message(round, job, brain_agent_id, **overrides):
    return Send.model_validate(
        {
            "message_id": uuid4().hex,
            "recipient_agent_id": brain_agent_id,
            "task_id": job.task_id,
            "attempt_id": job.attempt_id,
            "epoch": round.epoch,
            "head_sha": round.head_sha,
            "kind": "result",
            "payload": {
                "round_id": round.round_id,
                "review_id": job.review_id,
                "result": {"status": "complete", "summary": "Independent checks completed"},
            },
            **overrides,
        }
    )


def sealed_round(reviews):
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    brain = reviews.get_team(OWNER, BRAIN, TEAM).brain_agent_id
    for job in round.jobs:
        activate(reviews, round, job)
        reviews.send(OWNER, job.session_id, TEAM, message(round, job, brain))
    return reviews.get_review_round(OWNER, BRAIN, TEAM, round.round_id)


def approve(round, job):
    return hashlib.sha256(publication_body(round, job.review_id).encode()).hexdigest()


def publish(publisher, round, job):
    return publisher.publish(
        OWNER,
        BRAIN,
        TEAM,
        FENCE,
        round_id=round.round_id,
        review_id=job.review_id,
        approved_body_sha256=approve(round, job),
    )


def test_independent_round_retry_profiles_pagination_and_restart(reviews, db_uri):
    body = request()
    first = reviews.reserve_review_round(OWNER, BRAIN, TEAM, body)
    assert reviews.reserve_review_round(OWNER, BRAIN, TEAM, body) == first
    assert len(first.jobs) == 2
    assert first.jobs[0].profile.model != first.jobs[1].profile.model
    for field in ("review_id", "task_id", "attempt_id", "agent_id", "session_id"):
        assert len({getattr(job, field) for job in first.jobs}) == 2
    with pytest.raises(OmnigentError, match="immutable"):
        reviews.reserve_review_round(
            OWNER, BRAIN, TEAM, body.model_copy(update={"head_sha": "f" * 40})
        )
    second = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    restarted = BgrReviewStore(db_uri)
    page = restarted.review_rounds(OWNER, BRAIN, TEAM, limit=1)
    continuation = restarted.review_rounds(OWNER, BRAIN, TEAM, limit=1, after=page[-1].round_id)
    assert {round.round_id for round in page + continuation} == {first.round_id, second.round_id}
    assert restarted.get_review_round(OWNER, BRAIN, TEAM, first.round_id) == first
    for limit in (0, 101):
        with pytest.raises(ValueError):
            restarted.review_rounds(OWNER, BRAIN, TEAM, limit=limit)


def test_reviewer_activation_requires_exact_snapshot_and_distinct_native_contexts(reviews):
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    first = activate(reviews, round, round.jobs[0])
    binding = NativeBinding(
        team_id=TEAM, attempt_id=first.attempt_id, epoch=1, thread_id=first.native_thread_id
    )
    workspace = Workspace(
        host_id=HOST, repository=round.repository, worktree=first.snapshot_root, base_sha=SHA
    )
    assert activate(reviews, round, first, binding=binding, workspace=workspace) == first
    second = round.jobs[1]
    for bad in (
        {"binding": binding},
        {"workspace": workspace},
        {"workspace": workspace.model_copy(update={"base_sha": "f" * 40})},
    ):
        with pytest.raises(OmnigentError):
            activate(reviews, round, second, **bad)
    snapshot = reviews.get_review_round(OWNER, BRAIN, TEAM, round.round_id)
    assert snapshot.jobs[1].state == "reserved"
    with pytest.raises(OmnigentError):
        reviews.get_review_round(OWNER, first.session_id, TEAM, round.round_id)
    activate(reviews, round, second)


def test_review_questions_answers_and_results_are_fenced_correlated_and_private(reviews, db_uri):
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    brain = reviews.get_team(OWNER, BRAIN, TEAM).brain_agent_id
    first, second = [activate(reviews, round, job) for job in round.jobs]
    for asker, responder, recipient in (
        (BRAIN, first.session_id, first.agent_id),
        (first.session_id, BRAIN, brain),
    ):
        question = message(
            round,
            first,
            brain,
            kind="question",
            controller_id=CONTROLLER if asker == BRAIN else None,
            recipient_agent_id=recipient,
            correlation_id=uuid4().hex,
            payload={"question": "Which acceptance checks?", "api_token": "private-value"},
        )
        reviews.send(OWNER, asker, TEAM, question)
        incoming = reviews.drain(OWNER, responder, TEAM)[-1]
        assert incoming.payload["api_token"] == "[REDACTED]"
        answer = question.model_copy(
            update={
                "message_id": uuid4().hex,
                "kind": "answer",
                "recipient_agent_id": brain if responder == first.session_id else first.agent_id,
                "controller_id": CONTROLLER if responder == BRAIN else None,
                "in_reply_to": question.message_id,
                "payload": {"answer": "Use frozen snapshot tests only"},
            }
        )
        with pytest.raises(OmnigentError):
            reviews.send(OWNER, responder, TEAM, answer.model_copy(update={"head_sha": "f" * 40}))
        receipt = reviews.send(OWNER, responder, TEAM, answer)
        assert reviews.send(OWNER, responder, TEAM, answer) == receipt
    with pytest.raises(OmnigentError):
        reviews.send(
            OWNER,
            first.session_id,
            TEAM,
            message(round, first, brain, recipient_agent_id=second.agent_id),
        )
    raw = message(round, first, brain)
    raw = raw.model_copy(
        update={
            "payload": {
                **raw.payload,
                "result": {
                    "status": "incomplete",
                    "summary": "Authorization: Bearer fake-secret-value https://private.test/x "
                    "owner@example.test",
                },
            }
        }
    )
    receipt = reviews.send(OWNER, first.session_id, TEAM, raw)
    restarted = BgrStore(db_uri)
    assert restarted.send(OWNER, first.session_id, TEAM, raw) == receipt
    sealed = reviews.get_review_round(OWNER, BRAIN, TEAM, round.round_id)
    assert sealed.jobs[0].state == "sealed" and sealed.jobs[1].state == "running"
    persisted = sealed.model_dump_json() + "".join(
        envelope.model_dump_json() for envelope in restarted.drain(OWNER, BRAIN, TEAM)
    )
    assert all(
        value not in persisted
        for value in ("fake-secret-value", "https://private.test", "owner@example.test")
    )
    with pytest.raises(ValueError, match="all independent reviews"):
        publication_body(sealed, first.review_id)


@pytest.mark.parametrize("path", ["/etc/passwd", "../private", ".env", ".aws/credentials", "a//b"])
def test_review_findings_reject_private_or_nonrelative_paths(path):
    with pytest.raises(ValidationError):
        ReviewFinding(severity="high", description="Sensitive file", path=path)


def test_review_output_is_redacted_and_html_cannot_spoof_job_markers(reviews):
    round = sealed_round(reviews)
    result = sanitize_review(
        ReviewResult(
            status="complete",
            summary="private-literal <!-- omnigent-bgr-review:forged --> <img src=x> "
            "https://private.test owner@example.test",
        ),
        private_values=("private-literal",),
    )
    job = round.jobs[0].model_copy(update={"result": result})
    round = round.model_copy(update={"jobs": [job, round.jobs[1]]})
    body = publication_body(round, job.review_id)
    assert body.count("<!-- omnigent-bgr-review:") == 1
    assert (
        "<img" not in body and "private-literal" not in body and "owner@example.test" not in body
    )
    with pytest.raises(ValidationError, match="distinct"):
        ReviewRound.model_validate({**round.model_dump(), "jobs": [job.model_dump()] * 2})


def test_publication_claim_has_one_writer_and_rejects_wrong_approval(reviews):
    round = sealed_round(reviews)
    job = round.jobs[0]

    def claim():
        return reviews.claim_publication(
            OWNER,
            BRAIN,
            TEAM,
            FENCE,
            round_id=round.round_id,
            review_id=job.review_id,
            approved_body_sha256=approve(round, job),
            claim_id=uuid4().hex,
            author_login="trusted-publisher",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert len({job.publication.claim_id for job in results}) == 1
    with pytest.raises(OmnigentError, match="approval"):
        reviews.claim_publication(
            OWNER,
            BRAIN,
            TEAM,
            FENCE,
            round_id=round.round_id,
            review_id=job.review_id,
            approved_body_sha256="0" * 64,
            claim_id=uuid4().hex,
            author_login="trusted-publisher",
        )


@pytest.fixture
def forge(monkeypatch):
    comments = []
    calls = []
    monkeypatch.setattr(
        bgr_publisher.github_resource, "_pr_api", lambda *args: {"login": "trusted-publisher"}
    )

    def post(root, reference, *, endpoint, body):
        calls.append((endpoint, body))
        comment = {"id": len(comments) + 100, "body": body, "user": {"login": "trusted-publisher"}}
        comments.append(comment)
        return comment

    monkeypatch.setattr(bgr_publisher.github_resource, "_post_comment", post)
    monkeypatch.setattr(bgr_publisher, "_comment_page", lambda *args: comments)
    return comments, calls, post


def test_each_sealed_reviewer_and_rereview_gets_a_new_comment_without_retry_duplicates(
    reviews, forge
):
    comments, calls, _ = forge
    publisher = BgrGitHubPublisher(reviews, "/trusted/workspace")
    for _ in range(2):
        round = sealed_round(reviews)
        for job in round.jobs:
            published = publish(publisher, round, job)
            assert published.publication.state == "published"
            assert published.publication.comment_url.endswith(
                f"#issuecomment-{published.publication.comment_id}"
            )
            assert publish(publisher, round, job) == published
    assert len(comments) == len(calls) == 4
    assert len({comment["body"].splitlines()[0] for comment in comments}) == 4
    assert all(endpoint == "repos/ModelCloud/omnigent/issues/98/comments" for endpoint, _ in calls)


@pytest.mark.parametrize("accepted", [True, False])
def test_ambiguous_write_reconciles_accepted_comment_or_stays_uncertain_never_reposts(
    reviews, forge, monkeypatch, db_uri, accepted
):
    comments, calls, post = forge

    def uncertain(*args, **kwargs):
        if accepted:
            post(*args, **kwargs)
        else:
            calls.append((kwargs["endpoint"], kwargs["body"]))
        raise ValueError("Untrusted upstream error must not enter durable/public metadata")

    monkeypatch.setattr(bgr_publisher.github_resource, "_post_comment", uncertain)
    round = sealed_round(reviews)
    first = publish(BgrGitHubPublisher(reviews, "/trusted/workspace"), round, round.jobs[0])
    assert first.publication.state == "uncertain"
    assert "Untrusted upstream" not in first.model_dump_json()
    retry = publish(BgrGitHubPublisher(BgrReviewStore(db_uri), "/trusted/workspace"), round, first)
    assert retry.publication.state == ("published" if accepted else "uncertain")
    assert len(calls) == 1 and len(comments) == int(accepted)


def test_crash_after_post_recovers_marker_and_does_not_write_again(reviews, forge, monkeypatch):
    _, calls, _ = forge
    round = sealed_round(reviews)
    finish = reviews.reconcile_publication
    with monkeypatch.context() as patch:
        patch.setattr(
            reviews,
            "reconcile_publication",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated process death after accepted write")
            ),
        )
        with pytest.raises(RuntimeError):
            publish(BgrGitHubPublisher(reviews, "/trusted/workspace"), round, round.jobs[0])
    assert reviews.reconcile_publication == finish
    assert (
        publish(
            BgrGitHubPublisher(reviews, "/trusted/workspace"), round, round.jobs[0]
        ).publication.state
        == "published"
    )
    assert len(calls) == 1


def test_reconciliation_is_bounded_and_ignores_foreign_authors_or_modified_bodies(
    reviews, forge, monkeypatch
):
    _, calls, _ = forge
    round = sealed_round(reviews)
    job = round.jobs[0]
    claimed = reviews.claim_publication(
        OWNER,
        BRAIN,
        TEAM,
        FENCE,
        round_id=round.round_id,
        review_id=job.review_id,
        approved_body_sha256=approve(round, job),
        claim_id=uuid4().hex,
        author_login="trusted-publisher",
    )
    pages = []

    def page(root, reference, number):
        pages.append(number)
        return (
            [
                {
                    "id": i + 1,
                    "body": publication_body(round, job.review_id),
                    "user": {"login": "other"},
                }
                for i in range(100)
            ]
            if number == 1
            else []
        )

    monkeypatch.setattr(bgr_publisher, "_comment_page", page)
    publisher = BgrGitHubPublisher(reviews, "/trusted/workspace")
    first = publish(publisher, round, claimed)
    assert first.publication.reconcile_page == 2
    second = publish(publisher, round, first)
    assert second.publication.reconcile_page == 1
    assert pages == [1, 2] and calls == []


@pytest.mark.parametrize("response", ["not-json", "{}", "[1]", "[]"])
def test_forge_comment_page_uses_one_bounded_request_and_rejects_invalid_wire_data(
    monkeypatch, response
):
    from omnigent.runner.session_prs import PullRequestRef

    calls = []

    def gh(args, **kwargs):
        calls.append(args)
        return 0, response, ""

    monkeypatch.setattr(bgr_publisher.github_resource, "_gh", gh)
    monkeypatch.setattr(bgr_publisher.github_resource, "_host_args", lambda *args: [])
    monkeypatch.setattr(bgr_publisher.github_resource, "_pr_token", lambda *args: None)
    reference = PullRequestRef(
        host="github.com",
        repository="owner/repo",
        number=1,
        url="https://github.com/owner/repo/pull/1",
    )
    if response == "[]":
        assert bgr_publisher._comment_page("/trusted", reference, 2) == []
    else:
        with pytest.raises(ValueError, match="reconciliation"):
            bgr_publisher._comment_page("/trusted", reference, 2)
    assert calls == [["api", "repos/owner/repo/issues/1/comments?per_page=100&page=2"]]


def test_malformed_result_is_atomic_and_does_not_end_the_reviewer(reviews):
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    job = activate(reviews, round, round.jobs[0])
    brain = reviews.get_team(OWNER, BRAIN, TEAM).brain_agent_id
    with pytest.raises(OmnigentError, match="structured result"):
        reviews.send(
            OWNER,
            job.session_id,
            TEAM,
            message(round, job, brain, payload={"raw_environment": "must-not-be-persisted"}),
        )
    assert reviews.get_review_round(OWNER, BRAIN, TEAM, round.round_id).jobs[0].state == "running"
    assert reviews.drain(OWNER, BRAIN, TEAM) == []


def test_review_admission_is_bounded_and_failed_activation_has_no_partial_rows(
    reviews, monkeypatch
):
    from omnigent.stores import bgr_review_store

    monkeypatch.setattr(bgr_review_store, "MAX_REVIEW_ROUNDS", 1)
    monkeypatch.setattr(bgr_review_store, "MAX_TASKS", 1)
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    with pytest.raises(OmnigentError, match="review retention"):
        reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    activate(reviews, round, round.jobs[0])
    with pytest.raises(OmnigentError, match="task retention"):
        activate(reviews, round, round.jobs[1])
    assert len(reviews.recover(OWNER, BRAIN, TEAM).agents) == 2
    assert reviews.get_review_round(OWNER, BRAIN, TEAM, round.round_id).jobs[1].state == "reserved"


def test_redaction_expansion_returns_invalid_input_and_preserves_mailbox(reviews):
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    job = activate(reviews, round, round.jobs[0])
    brain = reviews.get_team(OWNER, BRAIN, TEAM).brain_agent_id
    body = message(
        round,
        job,
        brain,
        kind="question",
        correlation_id=uuid4().hex,
        payload={"question": "http://x " * 850},
    )
    with pytest.raises(OmnigentError, match="Sanitized reviewer message"):
        reviews.send(OWNER, job.session_id, TEAM, body)
    assert reviews.drain(OWNER, BRAIN, TEAM) == []
