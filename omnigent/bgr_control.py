"""Framework-owned Brain control schema; callers never choose their source identity."""

import hashlib
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from omnigent.bgr import Commit, Contract, Identifier, Record
from omnigent.bgr_review import Repository, ReviewEvaluation

BGR_CONTROL_TOOL = "sys_bgr_control"


class BgrControl(Record):
    action: Literal[
        "create_grunt",
        "start_review",
        "status",
        "adjudicate",
        "evaluate_reviews",
        "publication_preview",
        "publish",
    ]
    action_id: Identifier
    contract: Contract | None = None
    attempt_id: Identifier | None = None
    round_id: Identifier | None = None
    review_id: Identifier | None = None
    repository: Repository | None = None
    pr_number: int | None = None
    base_sha: Commit | None = None
    head_sha: Commit | None = None
    outcome: Literal["completed", "failed", "cancelled"] | None = None
    evaluation: ReviewEvaluation | None = None
    approved_body_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_validator(mode="after")
    def action_fields(self) -> Self:
        fields = {
            "create_grunt": {"contract", "base_sha"},
            "start_review": {"repository", "pr_number", "base_sha", "head_sha"},
            "status": set(),
            "adjudicate": {"attempt_id", "outcome"},
            "evaluate_reviews": {"round_id", "evaluation"},
            "publication_preview": {"round_id", "review_id"},
            "publish": {"round_id", "review_id", "approved_body_sha256"},
        }[self.action]
        supplied = self.model_dump(exclude_none=True).keys() - {"action", "action_id"}
        if self.action == "status":
            if supplied - {"round_id"}:
                raise ValueError("status accepts only an optional round_id")
        elif supplied != fields:
            raise ValueError("Control action has missing or unrelated fields")
        if self.pr_number is not None and not 1 <= self.pr_number <= 2**63 - 1:
            raise ValueError("Invalid pull request number")
        return self


def control_request_sha256(command: BgrControl) -> str:
    # Optional review metadata must not change legacy action identities.
    return hashlib.sha256(
        command.model_dump_json(
            exclude={"evaluation"} if command.evaluation is None else set()
        ).encode()
    ).hexdigest()


def brain_control_instructions() -> str:
    return (
        "You are the persistent BGR Brain named brain. Use sys_bgr_control for dynamic grunt "
        "creation, immutable independent review rounds, adjudication and trusted publication. "
        "Choose a fresh 32-hex action_id for each action; retries must preserve the ID and exact "
        "arguments. Native bgr_send_message/bgr_reply_message address existing peers only. "
        "Only you plan and evaluate. Keep clarification questions live, but do not request or "
        "relay sealed reviewer findings until all sibling reviews are terminal. Use status "
        "rather than raw controller reads. Review results are sealed from independent native "
        "callbacks; you must not fabricate or rewrite them. Each independent review and "
        "re-review, including no-findings and incomplete results, needs a new PR discussion "
        "comment. Once all siblings are sealed, evaluate their actual findings and coverage. "
        "Use evaluate_reviews with the status results_sha256 and an evidence-based rationale, "
        "choosing accept, revise or rereview. Never equate incomplete with no findings. "
        "A decision is immutable for that epoch; revisions require a new review round. "
        "Follow through with create_grunt for revisions or start_review for another independent "
        "round; do not silently accept unreviewed work. Publish each review regardless of "
        "decision outcome. Request publication_preview, inspect its sanitized body, then publish "
        "using "
        "the exact approved_body_sha256 returned. Never include keys/tokens/private values. "
        "A native acceptance receipt is NOT model consumption. An uncertain operation must "
        "be reconciled, never repeated under a fresh ID to hide uncertainty."
    )


BRAIN_CONTROL_INSTRUCTIONS = brain_control_instructions()
