"""Bounded independent review lifecycle and ambiguity-safe publication outbox."""

from __future__ import annotations

import hashlib
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.bgr import Agent, Attempt, Fence, NativeBinding, Record, Send, Workspace
from omnigent.bgr_review import (
    Publication,
    ReviewJob,
    ReviewResult,
    ReviewRound,
    ReviewRoundCreate,
    independent_review_contract,
    publication_body,
    sanitize_review,
    sanitize_review_payload,
)
from omnigent.db import current_workspace_id
from omnigent.db.bgr_models import (
    SqlBgrAttempt,
    SqlBgrNativeBinding,
    SqlBgrReviewRound,
    SqlBgrTask,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.bgr_store import MAX_TASKS, BgrStore, _conflict, _digest, _forbidden, _team

MAX_REVIEW_ROUNDS = 128


def sealed_review_agents(db: Session, team_id: str) -> set[str]:
    ready = set()
    rows = db.scalars(
        select(SqlBgrReviewRound)
        .where(
            SqlBgrReviewRound.workspace_id == current_workspace_id(),
            SqlBgrReviewRound.team_id == team_id,
        )
        .limit(MAX_REVIEW_ROUNDS)
    )
    for row in rows:
        round = ReviewRound.model_validate_json(row.document)
        if all(job.state == "sealed" for job in round.jobs):
            ready.update(job.agent_id for job in round.jobs)
    return ready


class _ResultPayload(Record):
    round_id: str
    review_id: str
    result: ReviewResult


def validate_review_message(
    db: Session,
    team_id: str,
    sender: Agent,
    target: Agent,
    attempt: Attempt,
    body: Send,
) -> Send:
    """Validate the live sandbox assignment and atomically seal sanitized results."""
    row = db.get(SqlBgrReviewRound, (current_workspace_id(), attempt.review_round_id))
    if row is None or row.team_id != team_id:
        raise _conflict("Reviewer channels require enforced independent-review lifecycle")
    round = ReviewRound.model_validate_json(row.document)
    worker = sender if sender.role == "reviewer" else target
    job = next((job for job in round.jobs if job.agent_id == worker.agent_id), None)
    if (
        job is None
        or job.session_id != worker.session_id
        or job.attempt_id != attempt.attempt_id
        or job.task_id != attempt.task_id
        or job.state != "running"
        or job.native_thread_id is None
        or job.snapshot_root != attempt.workspace.worktree
        or round.epoch != body.epoch
        or round.head_sha != body.head_sha
    ):
        raise _conflict("Review identity, live sandbox, epoch or immutable head mismatch")
    if body.kind == "result":
        if sender.role != "reviewer":
            raise _forbidden()
        try:
            payload = _ResultPayload.model_validate(body.payload)
            if payload.round_id != round.round_id or payload.review_id != job.review_id:
                raise _forbidden()
            result = sanitize_review(payload.result)
            sealed = job.model_copy(update={"state": "sealed", "result": result})
            BgrReviewStore._replace(row, round, sealed)
            safe = _ResultPayload(
                round_id=round.round_id, review_id=job.review_id, result=result
            ).model_dump(mode="json")
        except ValidationError:
            raise OmnigentError(
                "Reviewer result must use the bounded structured result contract",
                code=ErrorCode.INVALID_INPUT,
            ) from None
    else:
        safe = sanitize_review_payload(body.payload)
    try:
        return Send.model_validate({**body.model_dump(), "payload": safe})
    except ValidationError:
        raise OmnigentError(
            "Sanitized reviewer message exceeds the bounded payload contract",
            code=ErrorCode.INVALID_INPUT,
        ) from None


class BgrReviewStore(BgrStore):
    @classmethod
    def for_ledger(cls, store: BgrStore) -> BgrReviewStore:
        """Share the server ledger's scoped transaction factory and controller clock."""
        reviews = cls.__new__(cls)
        reviews._sessions = store._sessions
        reviews._clock = store._clock
        return reviews

    @staticmethod
    def _round(db: Session, team_id: str, round_id: str) -> tuple[SqlBgrReviewRound, ReviewRound]:
        row = db.get(SqlBgrReviewRound, (current_workspace_id(), round_id))
        if row is None or row.team_id != team_id:
            raise _forbidden()
        return row, ReviewRound.model_validate_json(row.document)

    @staticmethod
    def _job(round: ReviewRound, review_id: str) -> ReviewJob:
        job = next((job for job in round.jobs if job.review_id == review_id), None)
        if job is None:
            raise _forbidden()
        return job

    @staticmethod
    def _replace(row: SqlBgrReviewRound, round: ReviewRound, job: ReviewJob) -> ReviewRound:
        result = round.model_copy(
            update={"jobs": [job if old.review_id == job.review_id else old for old in round.jobs]}
        )
        row.document = result.model_dump_json()
        return result

    def reserve_review_round(
        self, user_id: str, session_id: str, team_id: str, body: ReviewRoundCreate
    ) -> ReviewRound:
        def apply(db: Session) -> ReviewRound:
            team = self._owned(db, team_id, user_id)
            self._fence(team, session_id, body)
            existing = db.get(SqlBgrReviewRound, (current_workspace_id(), body.round_id))
            if existing is not None:
                if existing.team_id != team_id:
                    raise _forbidden()
                round = ReviewRound.model_validate_json(existing.document)
                if any(
                    getattr(round, field) != getattr(body, field)
                    for field in ("repository", "pr_number", "base_sha", "head_sha", "epoch")
                ):
                    raise _conflict("round_id already identifies another immutable review")
                return round
            retained = list(
                db.scalars(
                    select(SqlBgrReviewRound.id)
                    .where(
                        SqlBgrReviewRound.workspace_id == current_workspace_id(),
                        SqlBgrReviewRound.team_id == team_id,
                    )
                    .limit(MAX_REVIEW_ROUNDS)
                )
            )
            if len(retained) >= MAX_REVIEW_ROUNDS:
                raise _conflict("BGR team review retention limit reached")
            round = ReviewRound(
                round_id=body.round_id,
                team_id=team_id,
                epoch=body.epoch,
                repository=body.repository,
                pr_number=body.pr_number,
                base_sha=body.base_sha,
                head_sha=body.head_sha,
                jobs=[
                    ReviewJob(
                        review_id=uuid4().hex,
                        task_id=uuid4().hex,
                        attempt_id=uuid4().hex,
                        agent_id=uuid4().hex,
                        session_id=uuid4().hex,
                        profile=profile,
                    )
                    for profile in _team(team).config.reviewers
                ],
            )
            db.add(
                SqlBgrReviewRound(
                    workspace_id=current_workspace_id(),
                    id=round.round_id,
                    team_id=team_id,
                    document=round.model_dump_json(),
                )
            )
            return round

        return self._write("reserve_independent_review_round", apply)

    def claim_reviewer_start(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        round_id: str,
        review_id: str,
    ) -> bool:
        """Claim once before the non-idempotent native factory; uncertainty needs recovery."""

        def apply(db: Session) -> bool:
            team = self._owned(db, team_id, user_id)
            self._fence(team, session_id, fence)
            row, round = self._round(db, team_id, round_id)
            if round.epoch != fence.epoch:
                raise _conflict("Review round belongs to another controller epoch")
            job = self._job(round, review_id)
            if job.state != "reserved":
                return False
            self._replace(row, round, job.model_copy(update={"state": "starting"}))
            return True

        return self._write("claim_atomic_independent_reviewer_start", apply)

    def activate_reviewer(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        round_id: str,
        review_id: str,
        binding: NativeBinding,
        workspace: Workspace,
    ) -> ReviewJob:
        """Record a reviewer only after the trusted adapter verifies atomic sandbox startup."""

        def apply(db: Session) -> ReviewJob:
            team = self._owned(db, team_id, user_id)
            self._fence(team, session_id, fence)
            row, round = self._round(db, team_id, round_id)
            job = self._job(round, review_id)
            if (
                round.epoch != fence.epoch
                or binding.team_id != team_id
                or binding.attempt_id != job.attempt_id
                or binding.epoch != fence.epoch
                or workspace.repository != round.repository
                or workspace.base_sha != round.base_sha
            ):
                raise _conflict("Reviewer binding does not match its frozen assignment")
            if job.state not in {"reserved", "starting"}:
                if (
                    job.state == "running"
                    and job.native_thread_id == binding.thread_id
                    and job.snapshot_root == workspace.worktree
                ):
                    return job
                raise _conflict("Reviewer already started or is terminal")
            if any(
                other.review_id != job.review_id
                and (
                    other.native_thread_id == binding.thread_id
                    or other.snapshot_root == workspace.worktree
                )
                for other in round.jobs
            ):
                raise _conflict("Independent reviewers cannot share a thread or snapshot root")
            retained = list(
                db.scalars(
                    select(SqlBgrAttempt.id)
                    .where(
                        SqlBgrAttempt.workspace_id == current_workspace_id(),
                        SqlBgrAttempt.team_id == team_id,
                    )
                    .limit(MAX_TASKS)
                )
            )
            if len(retained) >= MAX_TASKS:
                raise _conflict("BGR team task retention limit reached")
            agent = Agent(
                team_id=team_id,
                agent_id=job.agent_id,
                session_id=job.session_id,
                role="reviewer",
                name=f"reviewer-{job.agent_id}",
                profile=job.profile,
            )
            attempt = Attempt(
                team_id=team_id,
                task_id=job.task_id,
                attempt_id=job.attempt_id,
                agent_id=job.agent_id,
                session_id=job.session_id,
                epoch=fence.epoch,
                state="reserved",
                workspace=workspace,
                profile=job.profile,
                config_revision=_team(team).config_revision,
                review_round_id=round_id,
            )
            contract = independent_review_contract(round, job)
            db.add(self._agent_row(agent))
            db.add(
                SqlBgrTask(
                    workspace_id=current_workspace_id(),
                    id=job.task_id,
                    team_id=team_id,
                    request_hash=_digest([round_id, review_id]),
                    document=contract.model_dump_json(),
                )
            )
            db.add(
                SqlBgrAttempt(
                    workspace_id=current_workspace_id(),
                    id=job.attempt_id,
                    team_id=team_id,
                    task_id=job.task_id,
                    active_writer_key=None,
                    document=attempt.model_dump_json(),
                )
            )
            db.add(
                SqlBgrNativeBinding(
                    workspace_id=current_workspace_id(),
                    id=job.attempt_id,
                    team_id=team_id,
                    thread_id=binding.thread_id,
                    document=binding.model_dump_json(),
                )
            )
            self._insert_envelope(
                db,
                team,
                self._agent(db, team_id, session_id),
                Send(
                    **fence.model_dump(),
                    message_id=job.task_id,
                    recipient_agent_id=job.agent_id,
                    task_id=job.task_id,
                    attempt_id=job.attempt_id,
                    kind="task",
                    correlation_id=job.task_id,
                    head_sha=round.head_sha,
                    payload={
                        "contract": contract.model_dump(mode="json"),
                        "round_id": round_id,
                        "review_id": review_id,
                    },
                ),
            )
            job = job.model_copy(
                update={
                    "state": "running",
                    "native_thread_id": binding.thread_id,
                    "snapshot_root": workspace.worktree,
                }
            )
            self._replace(row, round, job)
            return job

        return self._write("activate_verified_independent_reviewer", apply)

    def get_review_round(
        self, user_id: str, session_id: str, team_id: str, round_id: str
    ) -> ReviewRound:
        with self._sessions("read_brain_review_round") as db:
            team = self._owned(db, team_id, user_id, lock=False)
            self._brain(team, session_id)
            return self._round(db, team_id, round_id)[1]

    def review_status(
        self, user_id: str, session_id: str, team_id: str, round_id: str
    ) -> dict[str, JsonValue]:
        """Model-visible projection; trusted lifecycle reads retain sealed evidence."""
        round = self.get_review_round(user_id, session_id, team_id, round_id)
        ready = all(job.state == "sealed" for job in round.jobs)
        return {
            "round_id": round.round_id,
            "base_sha": round.base_sha,
            "head_sha": round.head_sha,
            "results_ready": ready,
            "jobs": [
                {
                    "review_id": job.review_id,
                    "agent_id": job.agent_id,
                    "state": job.state,
                    "result": job.result.model_dump(mode="json") if ready and job.result else None,
                }
                for job in round.jobs
            ],
        }

    def settle_incomplete_review(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        round_id: str,
        review_id: str,
        reason: Literal["startup_failed", "model_failed", "cancelled", "lost"],
        stop_confirmed: Literal[True],
    ) -> ReviewJob:
        """Seal trusted lifecycle evidence only after confirming no reviewer can continue."""
        reasons = {
            "startup_failed": "Isolated reviewer startup failed",
            "model_failed": "Reviewer execution failed",
            "cancelled": "Review was cancelled",
            "lost": "Reviewer runtime could not be recovered",
        }
        if stop_confirmed is not True or reason not in reasons:
            raise ValueError("Incomplete settlement requires a valid reason and confirmed stop")

        def apply(db: Session) -> ReviewJob:
            team = self._owned(db, team_id, user_id)
            self._fence(team, session_id, fence)
            row, round = self._round(db, team_id, round_id)
            job = self._job(round, review_id)
            if job.state == "starting":
                phase = "Unable to execute; isolated reviewer startup was not confirmed"
            elif job.native_thread_id is None:
                phase = "Unable to execute; no isolated reviewer was started"
            else:
                phase = "Execution did not yield a completed independent review"
            result = ReviewResult(
                status="incomplete",
                summary=(
                    f"{phase}. {reasons[reason]}. Host lifecycle evidence, not reviewer findings."
                ),
            )
            if job.state == "sealed":
                if job.result == result:
                    return job
                raise _conflict("A sealed independent review cannot be overwritten")
            attempt_row = db.get(SqlBgrAttempt, (current_workspace_id(), job.attempt_id))
            if attempt_row is not None:
                attempt = Attempt.model_validate_json(attempt_row.document)
                outcome = "cancelled" if reason == "cancelled" else "failed"
                attempt_row.document = attempt.model_copy(
                    update={"state": outcome}
                ).model_dump_json()
                attempt_row.active_writer_key = None
            job = job.model_copy(update={"state": "sealed", "result": result})
            self._replace(row, round, job)
            return job

        return self._write("settle_incomplete_independent_review", apply)

    def review_rounds(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        *,
        after: str | None = None,
        limit: int = 50,
    ) -> list[ReviewRound]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1–100")
        with self._sessions("page_brain_review_rounds") as db:
            team = self._owned(db, team_id, user_id, lock=False)
            self._brain(team, session_id)
            query = select(SqlBgrReviewRound).where(
                SqlBgrReviewRound.workspace_id == current_workspace_id(),
                SqlBgrReviewRound.team_id == team_id,
            )
            if after is not None:
                query = query.where(SqlBgrReviewRound.id > after)
            return [
                ReviewRound.model_validate_json(row.document)
                for row in db.scalars(query.order_by(SqlBgrReviewRound.id).limit(limit))
            ]

    def claim_publication(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        round_id: str,
        review_id: str,
        approved_body_sha256: str,
        claim_id: str,
        author_login: str,
        private_values: tuple[str, ...] = (),
    ) -> ReviewJob:
        def apply(db: Session) -> ReviewJob:
            team = self._owned(db, team_id, user_id)
            self._fence(team, session_id, fence)
            row, round = self._round(db, team_id, round_id)
            job = self._job(round, review_id)
            body = publication_body(round, review_id, private_values=private_values)
            if hashlib.sha256(body.encode()).hexdigest() != approved_body_sha256:
                raise _conflict("Brain approval does not match the sanitized sealed publication")
            if job.publication.state != "pending":
                return job
            job = job.model_copy(
                update={
                    "publication": Publication(
                        state="sending",
                        claim_id=claim_id,
                        author_login=author_login,
                        attempted_at_us=self._now_us(),
                        approved_body=body,
                        approved_body_sha256=approved_body_sha256,
                    )
                }
            )
            self._replace(row, round, job)
            return job

        return self._write("claim_trusted_review_publication", apply)

    def reconcile_publication(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        round_id: str,
        review_id: str,
        claim_id: str,
        comment_id: int | None = None,
        next_page: int = 1,
    ) -> ReviewJob:
        def apply(db: Session) -> ReviewJob:
            team = self._owned(db, team_id, user_id)
            self._fence(team, session_id, fence)
            row, round = self._round(db, team_id, round_id)
            job = self._job(round, review_id)
            old = job.publication
            if old.claim_id != claim_id or old.state not in {"sending", "uncertain", "published"}:
                raise _conflict("Publication callback has a stale or foreign claim")
            if old.state == "published":
                if comment_id != old.comment_id:
                    raise _conflict("Published review already identifies another comment")
                return job
            publication = old.model_copy(
                update={
                    "state": "published" if comment_id is not None else "uncertain",
                    "comment_id": comment_id,
                    "comment_url": (
                        f"https://github.com/{round.repository}/pull/{round.pr_number}"
                        f"#issuecomment-{comment_id}"
                        if comment_id is not None
                        else None
                    ),
                    "reason": None
                    if comment_id is not None
                    else (
                        "Posting outcome is unconfirmed; reconcile the original marker. "
                        "Automatic reposting is forbidden."
                    ),
                    "reconcile_page": next_page,
                }
            )
            publication = Publication.model_validate(publication.model_dump())
            job = job.model_copy(update={"publication": publication})
            self._replace(row, round, job)
            return job

        return self._write("reconcile_trusted_review_publication", apply)
