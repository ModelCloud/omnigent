"""Trusted-adapter boundary and publisher tests, not model/runtime isolation evidence."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnigent.bgr import Ack, ConfigUpdate, Fence, NativeBinding, Send, Workspace
from omnigent.bgr_control import BgrControl, control_request_sha256
from omnigent.bgr_review import (
    Publication,
    ReviewEvaluation,
    ReviewFinding,
    ReviewResult,
    ReviewRound,
    ReviewRoundCreate,
    publication_body,
    review_results_sha256,
    sanitize_review,
)
from omnigent.errors import OmnigentError
from omnigent.harnesses.multidex_native.config import MultiDexConfig, MultiDexModelRegistration
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


def test_reviewer_start_claim_is_durable_once_and_activation_records_source_task(reviews, db_uri):
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    job = round.jobs[0]
    arguments = {"round_id": round.round_id, "review_id": job.review_id}
    assert reviews.claim_reviewer_start(OWNER, BRAIN, TEAM, FENCE, **arguments)
    restarted = BgrReviewStore(db_uri)
    assert not restarted.claim_reviewer_start(OWNER, BRAIN, TEAM, FENCE, **arguments)
    pending = restarted.get_review_round(OWNER, BRAIN, TEAM, round.round_id).jobs[0]
    assert pending.state == "starting"
    with pytest.raises(OmnigentError):
        restarted.claim_reviewer_start(
            OWNER, BRAIN, TEAM, FENCE.model_copy(update={"epoch": 2}), **arguments
        )
    running = activate(restarted, round, job)
    assert running.state == "running"
    assert not restarted.claim_reviewer_start(OWNER, BRAIN, TEAM, FENCE, **arguments)
    anchor = restarted.drain(OWNER, job.session_id, TEAM)[0]
    assert anchor.message_id == job.task_id
    assert anchor.kind == "task" and anchor.correlation_id == job.task_id
    assert anchor.head_sha == round.head_sha
    assert anchor.attempt_id == job.attempt_id and anchor.task_id == job.task_id
    assert anchor.payload["round_id"] == round.round_id
    assert anchor.payload["review_id"] == job.review_id
    contract = anchor.payload["contract"]
    assert isinstance(contract, dict)
    assert "trusted Brain-authorized publisher" in contract["instructions"]
    assert "fresh PR discussion" in contract["instructions"]
    assert restarted.drain(OWNER, job.session_id, TEAM) == [anchor]


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


def evaluate(reviews, round, *, next_action="accept"):
    status = reviews.review_status(OWNER, BRAIN, TEAM, round.round_id)
    command = BgrControl(
        action="evaluate_reviews",
        action_id=uuid4().hex,
        round_id=round.round_id,
        evaluation=ReviewEvaluation(
            results_sha256=status["results_sha256"],
            next_action=next_action,
            rationale="Brain evaluated all sealed fixture evidence.",
        ),
    )
    reviews.evaluate_reviews(OWNER, BRAIN, TEAM, FENCE, command)
    return reviews.get_review_round(OWNER, BRAIN, TEAM, round.round_id)


def sealed_round(reviews, *, evaluated=True):
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    brain = reviews.get_team(OWNER, BRAIN, TEAM).brain_agent_id
    for job in round.jobs:
        activate(reviews, round, job)
        reviews.send(OWNER, job.session_id, TEAM, message(round, job, brain))
    round = reviews.get_review_round(OWNER, BRAIN, TEAM, round.round_id)
    return evaluate(reviews, round) if evaluated else round


def approve(round, job):
    return hashlib.sha256(publication_body(round, job.review_id).encode()).hexdigest()


def test_optional_evaluation_preserves_existing_control_claim_identity():
    command = BgrControl(
        action="adjudicate", action_id=uuid4().hex, attempt_id=uuid4().hex, outcome="completed"
    )
    assert (
        control_request_sha256(command)
        == hashlib.sha256(command.model_dump_json(exclude={"evaluation"}).encode()).hexdigest()
    )


@pytest.mark.parametrize("next_action", ["accept", "revise", "rereview"])
def test_brain_evaluation_is_exact_immutable_durable_and_required_to_publish(
    reviews, db_uri, next_action
):
    round = sealed_round(reviews, evaluated=False)
    digest = review_results_sha256(round)
    assert digest != review_results_sha256(round.model_copy(update={"head_sha": "f" * 40}))
    job = round.jobs[0]
    arguments = {
        "round_id": round.round_id,
        "review_id": job.review_id,
        "approved_body_sha256": approve(round, job),
        "claim_id": uuid4().hex,
        "author_login": "trusted-publisher",
    }
    with pytest.raises(OmnigentError, match="evaluate"):
        reviews.claim_publication(OWNER, BRAIN, TEAM, FENCE, **arguments)
    command = BgrControl(
        action="evaluate_reviews",
        action_id=uuid4().hex,
        round_id=round.round_id,
        evaluation=ReviewEvaluation(
            results_sha256=digest,
            next_action=next_action,
            rationale="Explicit Brain evaluation of each sealed review.",
        ),
    )
    for owner, source, fence, body in (
        ("outsider", BRAIN, FENCE, command),
        (OWNER, job.session_id, FENCE, command),
        (OWNER, BRAIN, FENCE.model_copy(update={"epoch": 2}), command),
        (
            OWNER,
            BRAIN,
            FENCE,
            command.model_copy(
                update={
                    "evaluation": command.evaluation.model_copy(
                        update={"results_sha256": "0" * 64}
                    )
                }
            ),
        ),
    ):
        with pytest.raises(OmnigentError):
            reviews.evaluate_reviews(owner, source, TEAM, fence, body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: reviews.evaluate_reviews(OWNER, BRAIN, TEAM, FENCE, command), range(2)
            )
        )
    restarted = BgrReviewStore(db_uri)
    assert (
        results[0] == results[1] == restarted.evaluate_reviews(OWNER, BRAIN, TEAM, FENCE, command)
    )
    assert (
        restarted.review_status(OWNER, BRAIN, TEAM, round.round_id)["decision"]
        == results[0]["decision"]
    )
    for changed in (
        command.model_copy(update={"action_id": uuid4().hex}),
        command.model_copy(
            update={
                "evaluation": command.evaluation.model_copy(
                    update={"rationale": "Changed evaluation"}
                )
            }
        ),
    ):
        with pytest.raises(OmnigentError):
            reviews.evaluate_reviews(OWNER, BRAIN, TEAM, FENCE, changed)
    reviews.claim_publication(OWNER, BRAIN, TEAM, FENCE, **arguments)
    updated = restarted.get_review_round(OWNER, BRAIN, TEAM, round.round_id)
    assert len(updated.decisions) == 1
    assert (
        updated.decisions[0].brain_agent_id == reviews.get_team(OWNER, BRAIN, TEAM).brain_agent_id
    )
    assert review_results_sha256(updated) == digest


def test_evaluation_cannot_cross_sealed_sibling_barrier(reviews):
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    command = BgrControl(
        action="evaluate_reviews",
        action_id=uuid4().hex,
        round_id=round.round_id,
        evaluation=ReviewEvaluation(
            results_sha256="0" * 64,
            next_action="accept",
            rationale="Cannot infer missing sibling results.",
        ),
    )
    with pytest.raises(OmnigentError, match="withheld"):
        reviews.evaluate_reviews(OWNER, BRAIN, TEAM, FENCE, command)
    assert reviews.review_status(OWNER, BRAIN, TEAM, round.round_id)["decision"] is None
    with pytest.raises(ValueError, match="sealed"):
        review_results_sha256(round)


@pytest.mark.parametrize(
    ("body", "digest"),
    [
        ("body", None),
        (None, "0" * 64),
        ("body", "0" * 64),
        ("é" * 9000, hashlib.sha256(("é" * 9000).encode()).hexdigest()),
    ],
)
def test_publication_approval_pairs_match_identity_and_stored_byte_limit(body, digest):
    with pytest.raises(ValidationError, match="publication body"):
        Publication(approved_body=body, approved_body_sha256=digest)


@pytest.mark.parametrize("from_brain", [True, False])
def test_nested_credential_keys_are_redacted_before_durable_mailbox(reviews, from_brain):
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    job = round.jobs[0]
    activate(reviews, round, job)
    brain = reviews.get_team(OWNER, BRAIN, TEAM).brain_agent_id
    keys = [
        "api_key",
        "API-KEY",
        "apiKey",
        "credential",
        "credentials",
        "authorization",
        "auth",
        "AUTH_HEADERS",
        "private_key",
        "accessKeyId",
        "client_secret",
        "passwd",
        "passphrase",
        "cookie",
        "session_key",
        "signingKey",
        "clientKey",
        "http_headers",
    ]
    payload = {
        "question": "Clarify the assignment",
        "nested": [{key: {"nested": ["opaque-private-value", 123]} for key in keys}],
    }
    receipt = reviews.send(
        OWNER,
        BRAIN if from_brain else job.session_id,
        TEAM,
        message(
            round,
            job,
            job.agent_id if from_brain else brain,
            kind="question",
            controller_id=CONTROLLER if from_brain else None,
            correlation_id=uuid4().hex,
            payload=payload,
        ),
    )
    envelope = next(
        item
        for item in reviews.drain(OWNER, job.session_id if from_brain else BRAIN, TEAM)
        if item.message_id == receipt.message_id
    )
    assert envelope.message_id == receipt.message_id
    assert envelope.payload == {
        "question": "Clarify the assignment",
        "nested": [dict.fromkeys(keys, "[REDACTED]")],
    }
    assert "opaque-private-value" not in envelope.model_dump_json()


def test_trusted_private_inventory_reads_credentials_and_config_not_reviewers(monkeypatch):
    monkeypatch.setattr(bgr_publisher, "_codex_private_values", lambda: ("codex-private",))
    monkeypatch.setattr(
        bgr_publisher.os,
        "environ",
        {
            "PRIVATE_API_KEY": "environment-private",
            "PUBLIC_SETTING": "not-sensitive",
        },
    )
    monkeypatch.setattr(
        bgr_publisher,
        "load_global_config",
        lambda: {
            "providers": {"provider": {"credential": "config-private"}},
        },
    )
    monkeypatch.setattr(
        bgr_publisher,
        "load_local_config",
        lambda path: {
            "nested": [{"http_headers": {"X-Custom": "header-private"}}],
        },
    )
    model = MultiDexModelRegistration(
        model="model",
        provider="provider",
        display_name="Model",
        base_url="https://example.test",
        provider_config={"experimental_bearer_token": "model-private"},
    )
    monkeypatch.setattr(
        bgr_publisher,
        "load_multidex_config",
        lambda **kwargs: MultiDexConfig(
            local_model="model",
            provider="provider",
            base_url=model.base_url,
            models=(model,),
        ),
    )
    monkeypatch.setattr(bgr_publisher.github_resource, "_pr_token", lambda *args: None)
    commands = []

    def gh(args, **kwargs):
        commands.append(args)
        return 0, "forge-private\n", ""

    monkeypatch.setattr(bgr_publisher.github_resource, "_gh", gh)
    reference = bgr_publisher.PullRequestRef(
        host="github.com",
        repository="ModelCloud/omnigent",
        number=98,
        url="https://github.com/ModelCloud/omnigent/pull/98",
    )
    assert set(bgr_publisher._private_values("/trusted", reference)) == {
        "codex-private",
        "environment-private",
        "config-private",
        "header-private",
        "model-private",
        "forge-private",
    }
    assert commands == [["auth", "token", "--hostname", "github.com"]]
    monkeypatch.setattr(
        bgr_publisher.github_resource, "_pr_token", lambda *args: "selected-private"
    )
    assert "selected-private" in bgr_publisher._private_values("/trusted", reference)
    assert len(commands) == 1


def test_codex_private_inventory_uses_existing_home_resolver_without_executing_auth(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(bgr_publisher, "_codex_home_config_source_from_env", lambda: tmp_path)
    (tmp_path / "config.toml").write_text(
        '[model_providers.local]\nexperimental_bearer_token="codex-provider-private"\n'
        '[model_providers.local.http_headers]\nX-Custom="header-private"\n',
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text(
        '{"tokens":{"access_token":"oauth-token-private","account_id":"account-private"}}',
        encoding="utf-8",
    )
    assert set(bgr_publisher._codex_private_values()) == {
        "codex-provider-private",
        "header-private",
        "oauth-token-private",
        "account-private",
    }
    (tmp_path / "auth.json").unlink()
    assert set(bgr_publisher._codex_private_values()) == {
        "codex-provider-private",
        "header-private",
    }


@pytest.mark.parametrize("fault", ["config", "forge"])
def test_unavailable_private_inventory_fails_closed_without_secret_exception(monkeypatch, fault):
    monkeypatch.setattr(bgr_publisher, "_codex_private_values", tuple)
    monkeypatch.setattr(bgr_publisher, "load_global_config", dict)
    monkeypatch.setattr(bgr_publisher, "load_local_config", lambda path: {})
    monkeypatch.setattr(bgr_publisher.github_resource, "_pr_token", lambda *args: None)
    monkeypatch.setattr(bgr_publisher.github_resource, "_gh", lambda *args, **kwargs: (1, "", ""))

    def load(**kwargs):
        if fault == "config":
            raise ValueError("private parser input must not escape")
        raise FileNotFoundError

    monkeypatch.setattr(bgr_publisher, "load_multidex_config", load)
    reference = bgr_publisher.PullRequestRef(
        host="github.com",
        repository="ModelCloud/omnigent",
        number=98,
        url="https://github.com/ModelCloud/omnigent/pull/98",
    )
    with pytest.raises(
        ValueError, match="Could not establish trusted publication privacy context"
    ) as error:
        bgr_publisher._private_values("/trusted", reference)
    assert "private parser input" not in str(error.value)


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
    for envelope in reviews.drain(OWNER, BRAIN, TEAM):
        reviews.ack(OWNER, BRAIN, TEAM, Ack(envelope_id=envelope.envelope_id, epoch=round.epoch))
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

    assert restarted.drain(OWNER, BRAIN, TEAM, limit=1) == []
    status = reviews.review_status(OWNER, BRAIN, TEAM, round.round_id)
    assert status["results_ready"] is False
    assert all(job["result"] is None for job in status["jobs"])
    with pytest.raises(OmnigentError, match="withheld"):
        restarted.ack(OWNER, BRAIN, TEAM, Ack(envelope_id=receipt.envelope_id, epoch=round.epoch))

    clarification = message(
        round,
        second,
        brain,
        kind="question",
        correlation_id=uuid4().hex,
        payload={"question": "May I report no findings?"},
    )
    reviews.send(OWNER, second.session_id, TEAM, clarification)
    assert restarted.drain(OWNER, BRAIN, TEAM, limit=1)[0].message_id == clarification.message_id
    reviews.send(OWNER, second.session_id, TEAM, message(round, second, brain))
    ready = reviews.review_status(OWNER, BRAIN, TEAM, round.round_id)
    assert ready["results_ready"] is True
    assert all(job["result"] is not None for job in ready["jobs"])
    results = [
        envelope for envelope in restarted.drain(OWNER, BRAIN, TEAM) if envelope.kind == "result"
    ]
    assert {envelope.sender_agent_id for envelope in results} == {first.agent_id, second.agent_id}
    assert (
        restarted.ack(
            OWNER, BRAIN, TEAM, Ack(envelope_id=receipt.envelope_id, epoch=round.epoch)
        ).consumed_at
        is not None
    )


@pytest.mark.parametrize("path", ["/etc/passwd", "../private", ".env", ".aws/credentials", "a//b"])
def test_review_findings_reject_private_or_nonrelative_paths(path):
    with pytest.raises(ValidationError):
        ReviewFinding(severity="high", description="Sensitive file", path=path)


@pytest.mark.parametrize("repository", ["../repo", "owner/..", "./repo", "owner/."])
def test_review_repository_rejects_forge_path_traversal(repository):
    with pytest.raises(ValidationError, match="traversal components"):
        ReviewRoundCreate.model_validate({**request().model_dump(), "repository": repository})


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

    with pytest.raises(ValueError, match="known private value"):
        publication_body(round, job.review_id, private_values=(round.head_sha,))


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
    monkeypatch.setattr(bgr_publisher, "_private_values", lambda *args: ())
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


def test_publisher_redacts_host_only_values_before_approval_claim_and_post(
    reviews, forge, monkeypatch, db_uri
):
    _, calls, _ = forge
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    brain = reviews.get_team(OWNER, BRAIN, TEAM).brain_agent_id
    private = ("opaque-host-value", "opaque-model-value", "opaque-forge-value")
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
                    "result": {"status": "complete", "summary": " ".join(private)},
                },
            ),
        )
    round = reviews.get_review_round(OWNER, BRAIN, TEAM, round.round_id)
    job = round.jobs[0]
    publisher = BgrGitHubPublisher(reviews, "/trusted/workspace")
    evaluate(reviews, round)
    monkeypatch.setattr(bgr_publisher, "_private_values", lambda *args: private)
    body = publisher.preview(OWNER, BRAIN, TEAM, round_id=round.round_id, review_id=job.review_id)
    assert all(value not in body for value in private)
    assert "REDACTED" in body
    with pytest.raises(ValueError, match="approve"):
        publish(publisher, round, job)
    assert not calls
    approval = hashlib.sha256(body.encode()).hexdigest()
    result = publisher.publish(
        OWNER,
        BRAIN,
        TEAM,
        FENCE,
        round_id=round.round_id,
        review_id=job.review_id,
        approved_body_sha256=approval,
    )
    assert result.publication.approved_body == body
    assert result.publication.approved_body_sha256 == approval
    assert result.publication.state == "published"
    assert calls == [("repos/ModelCloud/omnigent/issues/98/comments", body)]
    assert all(value not in result.publication.model_dump_json() for value in private)
    restarted = BgrGitHubPublisher(BgrReviewStore(db_uri), "/trusted/workspace")
    assert (
        restarted.publish(
            OWNER,
            BRAIN,
            TEAM,
            FENCE,
            round_id=round.round_id,
            review_id=job.review_id,
            approved_body_sha256=approval,
        )
        == result
    )
    assert len(calls) == 1


@pytest.mark.parametrize("uncertain_write", [True, False])
def test_changed_privacy_context_after_post_requires_operator_never_reposts(
    reviews, forge, monkeypatch, uncertain_write
):
    _, calls, _ = forge
    round = sealed_round(reviews)
    publisher = BgrGitHubPublisher(reviews, "/trusted/workspace")

    def uncertain(*args, **kwargs):
        calls.append(kwargs["body"])
        raise OSError("No response")

    if uncertain_write:
        monkeypatch.setattr(bgr_publisher.github_resource, "_post_comment", uncertain)
    assert publish(publisher, round, round.jobs[0]).publication.state == (
        "uncertain" if uncertain_write else "published"
    )
    monkeypatch.setattr(bgr_publisher, "_private_values", lambda *args: ("checks",))
    body = publisher.preview(
        OWNER, BRAIN, TEAM, round_id=round.round_id, review_id=round.jobs[0].review_id
    )
    with pytest.raises(ValueError, match="operator reconciliation"):
        publisher.publish(
            OWNER,
            BRAIN,
            TEAM,
            FENCE,
            round_id=round.round_id,
            review_id=round.jobs[0].review_id,
            approved_body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        )
    assert len(calls) == 1


@pytest.mark.parametrize("invalid", ["body", "author", "missing_body", "missing_author"])
def test_successful_post_with_unverified_identity_remains_uncertain_without_repost(
    reviews, forge, monkeypatch, invalid
):
    _, calls, post = forge

    def response(*args, **kwargs):
        result = post(*args, **kwargs)
        if invalid == "body":
            result = {**result, "body": "Different body"}
        elif invalid == "author":
            result = {**result, "user": {"login": "wrong-author"}}
        else:
            result = dict(result)
            result.pop("body" if invalid == "missing_body" else "user")
        return result

    monkeypatch.setattr(bgr_publisher.github_resource, "_post_comment", response)
    round = sealed_round(reviews)
    publisher = BgrGitHubPublisher(reviews, "/trusted/workspace")
    first = publish(publisher, round, round.jobs[0])
    assert first.publication.state == "uncertain" and first.publication.comment_id is None
    reconciled = publish(publisher, round, first)
    assert reconciled.publication.state == "published"
    assert len(calls) == 1


@pytest.mark.parametrize("reason", ["startup_failed", "model_failed", "cancelled", "lost"])
@pytest.mark.parametrize("started", [False, True])
def test_failed_reviewer_settles_incomplete_without_starving_sibling_publication(
    reviews, forge, db_uri, reason, started
):
    _, calls, _ = forge
    round = reviews.reserve_review_round(OWNER, BRAIN, TEAM, request())
    completed, failed = round.jobs
    brain = reviews.get_team(OWNER, BRAIN, TEAM).brain_agent_id
    activate(reviews, round, completed)
    reviews.send(OWNER, completed.session_id, TEAM, message(round, completed, brain))
    assert reviews.drain(OWNER, BRAIN, TEAM) == []
    assert reviews.review_status(OWNER, BRAIN, TEAM, round.round_id)["results_ready"] is False
    if started:
        activate(reviews, round, failed)
    arguments = {
        "round_id": round.round_id,
        "review_id": failed.review_id,
        "reason": reason,
        "stop_confirmed": True,
    }
    with pytest.raises(OmnigentError):
        reviews.settle_incomplete_review(OWNER, completed.session_id, TEAM, FENCE, **arguments)
    with pytest.raises(ValueError, match="confirmed stop"):
        reviews.settle_incomplete_review(
            OWNER, BRAIN, TEAM, FENCE, **{**arguments, "stop_confirmed": False}
        )
    settled = reviews.settle_incomplete_review(OWNER, BRAIN, TEAM, FENCE, **arguments)
    assert settled.result.status == "incomplete" and not settled.result.findings
    assert "Host lifecycle evidence, not reviewer findings" in settled.result.summary
    if not started:
        assert "Unable to execute; no isolated reviewer was started" in settled.result.summary
    restarted = BgrReviewStore(db_uri)
    assert restarted.settle_incomplete_review(OWNER, BRAIN, TEAM, FENCE, **arguments) == settled
    assert restarted.review_status(OWNER, BRAIN, TEAM, round.round_id)["results_ready"] is True
    assert restarted.drain(OWNER, BRAIN, TEAM)[0].sender_agent_id == completed.agent_id
    round = restarted.get_review_round(OWNER, BRAIN, TEAM, round.round_id)
    publisher = BgrGitHubPublisher(restarted, "/trusted/workspace")
    evaluate(restarted, round, next_action="rereview")
    for job in round.jobs:
        assert publish(publisher, round, job).publication.state == "published"
    assert len(calls) == 2
    assert any("incomplete" in body for _, body in calls)
    with pytest.raises(OmnigentError, match="cannot be overwritten"):
        restarted.settle_incomplete_review(
            OWNER,
            BRAIN,
            TEAM,
            FENCE,
            **{**arguments, "review_id": completed.review_id},
        )
    if started:
        with pytest.raises(OmnigentError, match=r"stale, terminal|live sandbox"):
            restarted.send(OWNER, failed.session_id, TEAM, message(round, failed, brain))


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
