"""Trusted host publication; credentials never enter a reviewer context."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from yaml import YAMLError

from omnigent.bgr import Fence
from omnigent.bgr_review import ReviewJob, ReviewRound, publication_body, sensitive_review_key
from omnigent.config import load_global_config, load_local_config
from omnigent.harnesses.multidex_native.config import load_multidex_config, multidex_models
from omnigent.inner.codex_executor import _codex_home_config_source_from_env
from omnigent.runner import github_resource
from omnigent.runner.session_prs import PullRequestRef


class PublicationLedger(Protocol):
    def get_review_round(
        self, user_id: str, session_id: str, team_id: str, round_id: str
    ) -> ReviewRound: ...

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
    ) -> ReviewJob: ...

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
        comment_id: int | None,
        next_page: int = 1,
    ) -> ReviewJob: ...


def _config_private_values(value: object, *, sensitive: bool = False) -> Iterator[str]:
    if isinstance(value, str) and sensitive:
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            private = sensitive or sensitive_review_key(str(key))
            yield from _config_private_values(item, sensitive=private)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _config_private_values(item, sensitive=sensitive)


def _codex_private_values() -> Iterator[str]:
    home = _codex_home_config_source_from_env()
    for name in ("config.toml", "auth.json"):
        try:
            content = (home / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        document = tomllib.loads(content) if name.endswith(".toml") else json.loads(content)
        if not isinstance(document, dict):
            raise ValueError("Unknown credential configuration shape")
        yield from _config_private_values(document, sensitive=name == "auth.json")


def _private_values(
    root: str, reference: PullRequestRef | None = None, *, require_forge: bool = True
) -> tuple[str, ...]:
    """Resolve only in the trusted host, never in reviewer requests or persisted metadata."""
    values = [value for key, value in os.environ.items() if sensitive_review_key(key)]
    try:
        values.extend(_codex_private_values())
        for document in (
            load_global_config(),
            load_local_config(Path(root) / ".omnigent/config.yaml"),
        ):
            if not isinstance(document, dict):
                raise ValueError("Unknown configuration shape")
            values.extend(_config_private_values(document))
        try:
            config = load_multidex_config(require_token=False)
        except FileNotFoundError:
            config = None
        if config is not None:
            for model in multidex_models(config):
                values.extend(_config_private_values(model.provider_config))
        token = github_resource._pr_token(root, reference) if reference is not None else None
        if not token:
            code, token, _ = github_resource._gh(
                ["auth", "token", "--hostname", reference.host if reference else "github.com"],
                cwd=root,
            )
            if require_forge and (code != 0 or not token.strip()):
                raise ValueError("Publisher credential inventory is unavailable")
            if code != 0:
                token = ""
        values.append(token.strip())
    except (ValueError, OSError, YAMLError):
        raise ValueError("Could not establish trusted publication privacy context") from None
    return tuple(sorted({value for value in values if value}, key=len, reverse=True))


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

    def __init__(self, store: PublicationLedger, root: str):
        self.store = store
        self.root = root

    def preview(
        self, user_id: str, session_id: str, team_id: str, *, round_id: str, review_id: str
    ) -> str:
        round = self.store.get_review_round(user_id, session_id, team_id, round_id)
        return publication_body(
            round, review_id, private_values=_private_values(self.root, _reference(round))
        )

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
        reconcile_only: bool = False,
    ) -> ReviewJob:
        round = self.store.get_review_round(user_id, session_id, team_id, round_id)
        private_values = _private_values(self.root, _reference(round))
        body = publication_body(round, review_id, private_values=private_values)
        if hashlib.sha256(body.encode()).hexdigest() != approved_body_sha256:
            raise ValueError("Brain must approve the sanitized sealed publication body")
        old = next((job for job in round.jobs if job.review_id == review_id), None)
        if old is None:
            raise ValueError("Unknown sealed review")
        reference = _reference(round)
        if old.publication.state != "pending":
            if old.publication.approved_body != body or (
                old.publication.approved_body_sha256 != approved_body_sha256
            ):
                raise ValueError(
                    "Publication privacy context changed; operator reconciliation required"
                )
            if old.publication.state == "published":
                return old
            return self._reconcile(user_id, session_id, team_id, fence, round, old, body)
        if reconcile_only:
            return old
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
            private_values=private_values,
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
            author = result.get("user")
            if (
                type(candidate) is int
                and 1 <= candidate <= 2**63 - 1
                and result.get("body") == body
                and isinstance(author, dict)
                and author.get("login") == login
            ):
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
