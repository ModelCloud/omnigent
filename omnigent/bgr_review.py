"""Immutable review assignments, sealed results and trusted publication records."""

from __future__ import annotations

import hashlib
import html
import re
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, JsonValue, field_validator, model_validator

from omnigent.bgr import Commit, Contract, Fence, Identifier, Label, Profile, Record
from omnigent.process_logging import redact_log_text


def _repository_slug(value: str) -> str:
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("repository requires an owner/name slug without traversal components")
    return value


Repository = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=256),
    AfterValidator(_repository_slug),
]


class ReviewFinding(Record):
    severity: Literal["critical", "high", "medium", "low"]
    description: Annotated[str, Field(min_length=1, max_length=1500)]
    path: Annotated[str, Field(min_length=1, max_length=512)]
    line: Annotated[int, Field(ge=1)] | None = None

    @field_validator("path")
    @classmethod
    def relative_source_path(cls, value: str) -> str:
        parts = value.split("/")
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in parts)
            or any(ord(char) < 32 for char in value)
            or any(part.startswith((".env", ".aws", ".ssh")) for part in parts)
        ):
            raise ValueError("findings require a relative, non-sensitive source path")
        return value


class ReviewResult(Record):
    status: Literal["complete", "incomplete"]
    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    findings: Annotated[list[ReviewFinding], Field(max_length=16)] = Field(default_factory=list)


class _NativeReviewResult(Record):
    outcome: Literal["no_findings", "incomplete"]
    findings: Annotated[list[ReviewFinding], Field(max_length=16)]
    coverageLimits: Annotated[list[Label], Field(max_length=16)]


def normalize_native_review_result(payload: dict[str, JsonValue]) -> ReviewResult:
    """Translate only documented native outcomes; never infer findings or completed coverage."""
    if "outcome" not in payload:
        return ReviewResult.model_validate(payload)
    result = _NativeReviewResult.model_validate(payload)
    if result.outcome == "no_findings" and result.findings:
        raise ValueError("A no-findings result cannot contain findings")
    summary = (
        "Reviewer reported no findings."
        if result.outcome == "no_findings"
        else "Reviewer reported incomplete coverage."
    )
    if result.coverageLimits:
        summary += " Coverage limits: " + "; ".join(result.coverageLimits)
    return ReviewResult(
        status="complete" if result.outcome == "no_findings" else "incomplete",
        summary=summary,
        findings=result.findings,
    )


class ReviewRoundCreate(Fence):
    round_id: Identifier
    repository: Repository
    pr_number: Annotated[int, Field(ge=1, le=2**63 - 1)]
    base_sha: Commit
    head_sha: Commit

    @field_validator("repository")
    @classmethod
    def no_relative_repository(cls, value: str) -> str:
        if any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("repository requires an owner and repository name")
        return value


class Publication(Record):
    state: Literal["pending", "sending", "uncertain", "published", "blocked"] = "pending"
    claim_id: Identifier | None = None
    author_login: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    attempted_at_us: int | None = None
    comment_id: Annotated[int, Field(ge=1, le=2**63 - 1)] | None = None
    comment_url: Annotated[str, Field(max_length=1024)] | None = None
    reason: Annotated[str, Field(max_length=512)] | None = None
    reconcile_page: Annotated[int, Field(ge=1)] = 1
    approved_body: Annotated[str, Field(max_length=16_000)] | None = None
    approved_body_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_validator(mode="after")
    def approved_body_identity(self) -> Self:
        if (self.approved_body is None) != (self.approved_body_sha256 is None):
            raise ValueError("publication body and approval hash must be recorded together")
        if self.approved_body is not None and (
            len(self.approved_body.encode()) > 16_000
            or hashlib.sha256(self.approved_body.encode()).hexdigest() != self.approved_body_sha256
        ):
            raise ValueError("publication body does not match the bounded approved identity")
        return self


class ReviewJob(Record):
    review_id: Identifier
    task_id: Identifier
    attempt_id: Identifier
    agent_id: Identifier
    session_id: Identifier
    profile: Profile
    state: Literal["reserved", "starting", "running", "sealed", "failed", "cancelled"] = "reserved"
    native_thread_id: Annotated[str, Field(max_length=36)] | None = None
    snapshot_root: Annotated[str, Field(max_length=1024)] | None = None
    result: ReviewResult | None = None
    publication: Publication = Publication()

    @model_validator(mode="after")
    def sealed_result(self) -> Self:
        if (self.state == "sealed") != (self.result is not None):
            raise ValueError("only sealed reviews carry results")
        return self


class ReviewRound(Record):
    round_id: Identifier
    team_id: Identifier
    epoch: Annotated[int, Field(ge=1)]
    repository: Repository
    pr_number: Annotated[int, Field(ge=1, le=2**63 - 1)]
    base_sha: Commit
    head_sha: Commit
    publication_version: Literal[1] = 1
    jobs: Annotated[list[ReviewJob], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def independent_identities(self) -> Self:
        for field in ("review_id", "task_id", "attempt_id", "agent_id", "session_id"):
            if len({getattr(job, field) for job in self.jobs}) != len(self.jobs):
                raise ValueError("reviewers require distinct identities and fresh contexts")
        return self


def independent_review_contract(round: ReviewRound, job: ReviewJob) -> Contract:
    if job not in round.jobs:
        raise ValueError("Reviewer does not belong to this immutable review round")
    return Contract(
        objective=f"Independently review {round.repository} PR {round.pr_number}",
        instructions=(
            "Review only the assigned immutable diff using bgr_read_review_snapshot. "
            "Ask brain for clarification through bgr_send_message/bgr_reply_message. "
            "Do not request shell, MCP, delegation, credentials or forge writes. "
            "Send one final result with status ('complete' or 'incomplete'), summary, "
            "and findings (severity: critical/high/medium/low, description, relative path, "
            "optional positive line). Report coverage limits in summary; never claim "
            "unperformed checks. No findings still requires a result. Never include "
            "keys, tokens or other private information. Every independent review and "
            "re-review, including incomplete/no-findings, must receive a fresh PR discussion "
            "comment through the trusted Brain-authorized publisher, never reviewer writes."
        ),
        acceptance=[
            "Return complete or explicitly incomplete independent findings",
            f"Frozen base {round.base_sha} and head {round.head_sha}",
        ],
    )


def sensitive_review_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in {"key", "auth", "pem", "headers", "httpheaders", "queryparams"} or any(
        word in normalized
        for word in (
            "token",
            "password",
            "passwd",
            "passphrase",
            "secret",
            "cookie",
            "credential",
            "apikey",
            "accesskey",
            "privatekey",
            "signingkey",
            "encryptionkey",
            "sessionkey",
            "authorization",
            "authentication",
            "authheader",
            "basicauth",
            "oauth",
            "sshkey",
            "keypair",
            "clientkey",
            "authkey",
            "masterkey",
            "accountkey",
        )
    )


def sanitize_review_text(value: str, *, private_values: tuple[str, ...] = ()) -> str:
    """Remove known secrets, contact data and untrusted URLs, without echoing the input."""
    for private in sorted(set(private_values), key=len, reverse=True):
        if private:
            value = value.replace(private, "[REDACTED]")
    value = redact_log_text(value, include_whitespace_credentials=True)
    value = re.sub(r"(?:https?|ssh)://[^\s<>]+", "[link withheld]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email withheld]", value)
    return "".join(char for char in value if char in "\n\t" or ord(char) >= 32)


def sanitize_review_payload(
    value: JsonValue, *, private_values: tuple[str, ...] = ()
) -> JsonValue:
    if isinstance(value, str):
        return sanitize_review_text(value, private_values=private_values)
    if isinstance(value, list):
        return [sanitize_review_payload(item, private_values=private_values) for item in value]
    if isinstance(value, dict):
        return {
            sanitize_review_text(key, private_values=private_values): (
                "[REDACTED]"
                if sensitive_review_key(key)
                else sanitize_review_payload(item, private_values=private_values)
            )
            for key, item in value.items()
        }
    return value


def sanitize_review(result: ReviewResult, *, private_values: tuple[str, ...] = ()) -> ReviewResult:
    """Remove known secrets and contact/URL data before durable sealing or publication."""

    def clean(value: str) -> str:
        return sanitize_review_text(value, private_values=private_values)

    return ReviewResult(
        status=result.status,
        summary=clean(result.summary),
        findings=[
            ReviewFinding(
                severity=finding.severity,
                description=clean(finding.description),
                path=clean(finding.path),
                line=finding.line,
            )
            for finding in result.findings
        ],
    )


def publication_body(
    round: ReviewRound, review_id: str, *, private_values: tuple[str, ...] = ()
) -> str:
    """Render one independent result only after all sibling outputs are sealed."""
    if any(job.state != "sealed" for job in round.jobs):
        raise ValueError("all independent reviews must be sealed before publication")
    job = next((job for job in round.jobs if job.review_id == review_id), None)
    if job is None or job.result is None:
        raise ValueError("review is not part of the sealed round")

    def quote(value: str) -> str:
        return "\n".join(
            "> " + re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", html.escape(line))
            for line in value.replace("@", "[at]").splitlines()
        )

    result = sanitize_review(job.result, private_values=private_values)
    lines = [
        f"<!-- omnigent-bgr-review:{job.review_id} -->",
        f"## Independent review: reviewer-{job.agent_id}",
        f"Round: `{round.round_id}`. Status: **{result.status}**.",
        f"Frozen snapshot: `{round.base_sha}` → `{round.head_sha}`.",
        "",
        quote(result.summary),
    ]
    for finding in result.findings:
        location = finding.path + (f":{finding.line}" if finding.line else "")
        lines.extend(
            ["", f"### {finding.severity.upper()}", quote(location), quote(finding.description)]
        )
    if not result.findings:
        lines.extend(
            ["", "No findings reported." if result.status == "complete" else "Review incomplete."]
        )
    body = "\n".join(lines)
    if any(private and private in body for private in private_values):
        raise ValueError(
            "Publication still matches a known private value; operator review required"
        )
    if len(body.encode("utf-8")) > 16_000:
        raise ValueError("publication exceeds the bounded review body; submit a smaller result")
    return body
