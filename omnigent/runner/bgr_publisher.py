"""Trusted host publication; credentials never enter a reviewer context."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from omnigent.bgr import Fence
from omnigent.bgr_review import ReviewJob, ReviewRound, publication_body
from omnigent.runner import github_resource
from omnigent.runner.session_prs import PullRequestRef
from omnigent.stores.bgr_review_store import BgrReviewStore


def _reference(round: ReviewRound) -> PullRequestRef:
    return PullRequestRef(
        host="github.com",
        repository=round.repository,
        number=round.pr_number,
        url=f"https://github.com/{round.repository}/pull/{round.pr_number}",
    )


def _comment_page(root: str, reference: PullRequestRef, page: int) -> list[dict[str, object]]:
    endpoint = (
        f"repos/{reference.repository}/issues/{reference.number}/comments?per_page=100&page={page}"
    )
    code, output, _ = github_resource._gh(
        ["api", *github_resource._host_args(root, reference), endpoint],
        cwd=root,
        token=github_resource._pr_token(root, reference),
    )
    if code != 0:
        raise ValueError("Could not reconcile the original review comment")
    try:
        result = json.loads(output)
    except ValueError:
        raise ValueError("Invalid review-comment reconciliation response") from None
    if (
        not isinstance(result, list)
        or len(result) > 100
        or any(not isinstance(item, dict) for item in result)
    ):
        raise ValueError("Invalid review-comment reconciliation response")
    return result


class BgrGitHubPublisher:
    """One write attempt per sealed job; uncertain attempts reconcile without reposting."""

    def __init__(self, store: BgrReviewStore, root: str):
        self.store = store
        self.root = root

    def publish(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        round_id: str,
        review_id: str,
        approved_body_sha256: str,
    ) -> ReviewJob:
        round = self.store.get_review_round(user_id, session_id, team_id, round_id)
        body = publication_body(round, review_id)
        if hashlib.sha256(body.encode()).hexdigest() != approved_body_sha256:
            raise ValueError("Brain must approve the sanitized sealed publication body")
        old = self.store._job(round, review_id)
        if old.publication.state == "published":
            return old
        reference = _reference(round)
        if old.publication.state != "pending":
            return self._reconcile(user_id, session_id, team_id, fence, round, old, body)
        account = github_resource._pr_api(self.root, reference, "user")
        login = account.get("login")
        if not isinstance(login, str) or not 1 <= len(login) <= 100:
            raise ValueError("Trusted publisher account is unavailable; no comment was sent")
        claim_id = uuid4().hex
        job = self.store.claim_publication(
            user_id,
            session_id,
            team_id,
            fence,
            round_id=round_id,
            review_id=review_id,
            approved_body_sha256=approved_body_sha256,
            claim_id=claim_id,
            author_login=login,
        )
        if job.publication.claim_id != claim_id:
            if job.publication.state == "published":
                return job
            return self._reconcile(user_id, session_id, team_id, fence, round, job, body)
        comment_id = None
        try:
            result = github_resource._post_comment(
                self.root,
                reference,
                endpoint=f"repos/{round.repository}/issues/{round.pr_number}/comments",
                body=body,
            )
            candidate = result.get("id")
            if type(candidate) is int and 1 <= candidate <= 2**63 - 1:
                comment_id = candidate
        except (ValueError, OSError):
            # A failed response does not prove the external write failed.
            pass
        return self.store.reconcile_publication(
            user_id,
            session_id,
            team_id,
            fence,
            round_id=round_id,
            review_id=review_id,
            claim_id=claim_id,
            comment_id=comment_id,
        )

    def _reconcile(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        round: ReviewRound,
        job: ReviewJob,
        body: str,
    ) -> ReviewJob:
        claim = job.publication
        if claim.claim_id is None or claim.author_login is None:
            raise ValueError("Publication requires operator reconciliation; reposting is blocked")
        comments = _comment_page(self.root, _reference(round), claim.reconcile_page)
        matches: list[int] = []
        for comment in comments:
            author = comment.get("user")
            identifier = comment.get("id")
            if (
                comment.get("body") == body
                and isinstance(author, dict)
                and author.get("login") == claim.author_login
                and type(identifier) is int
                and 1 <= identifier <= 2**63 - 1
            ):
                matches.append(identifier)
        if len(matches) > 1:
            raise ValueError("Multiple matching comments need operator reconciliation")
        return self.store.reconcile_publication(
            user_id,
            session_id,
            team_id,
            fence,
            round_id=round.round_id,
            review_id=job.review_id,
            claim_id=claim.claim_id,
            comment_id=matches[0] if matches else None,
            next_page=claim.reconcile_page + 1 if len(comments) == 100 else 1,
        )
