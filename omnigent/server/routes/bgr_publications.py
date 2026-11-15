"""Brain-host-only durable publication claims; no forge credentials cross this route."""

import asyncio
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from pydantic import Field, JsonValue, model_validator

from omnigent.bgr import Fence, Identifier, Record
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.bgr_controller import BgrControllers


class PublicationLedgerCall(Record):
    operation: Literal["read", "claim", "reconcile"]
    round_id: Identifier
    review_id: Identifier | None = None
    fence: Fence | None = None
    claim_id: Identifier | None = None
    author_login: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    approved_body: Annotated[str, Field(min_length=1, max_length=16000)] | None = None
    approved_body_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    comment_id: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)] | None = None
    next_page: Annotated[int, Field(strict=True, ge=1)] | None = None

    @model_validator(mode="after")
    def fields_for_operation(self):
        allowed = {
            "read": set(),
            "claim": {
                "review_id",
                "fence",
                "claim_id",
                "author_login",
                "approved_body",
                "approved_body_sha256",
            },
            "reconcile": {"review_id", "fence", "claim_id", "comment_id", "next_page"},
        }[self.operation]
        supplied = self.model_dump(exclude_none=True).keys() - {"operation", "round_id"}
        required = allowed - (
            {"comment_id", "next_page"} if self.operation == "reconcile" else set()
        )
        if not required <= supplied <= allowed:
            raise ValueError("Publication ledger has missing or unrelated fields")
        return self


def create_bgr_publication_router(
    controllers: BgrControllers, source: Callable[[Request], tuple[str, str]]
) -> APIRouter:
    router = APIRouter()

    @router.post("/teams/{team_id}/publication-ledger")
    async def ledger(
        request: Request, team_id: Identifier, body: PublicationLedgerCall
    ) -> dict[str, JsonValue]:
        user, session = source(request)
        controller = controllers.get(session)
        controller.authorize(user, session)
        if controller.team_id != team_id or (
            body.operation != "read" and body.fence != controller.fence
        ):
            raise OmnigentError("Foreign publication ledger", code=ErrorCode.FORBIDDEN)
        if body.operation == "read":
            round = await asyncio.to_thread(
                controllers.store.get_review_round, user, session, team_id, body.round_id
            )
            return round.model_dump(mode="json")
        assert body.review_id is not None and body.claim_id is not None
        if body.operation == "claim":
            assert body.approved_body is not None and body.approved_body_sha256 is not None
            assert body.author_login is not None
            job = await asyncio.to_thread(
                controllers.store.claim_publication,
                user,
                session,
                team_id,
                controller.fence,
                round_id=body.round_id,
                review_id=body.review_id,
                claim_id=body.claim_id,
                approved_body=body.approved_body,
                approved_body_sha256=body.approved_body_sha256,
                author_login=body.author_login,
            )
        else:
            job = await asyncio.to_thread(
                controllers.store.reconcile_publication,
                user,
                session,
                team_id,
                controller.fence,
                round_id=body.round_id,
                review_id=body.review_id,
                claim_id=body.claim_id,
                comment_id=body.comment_id,
                next_page=body.next_page or 1,
            )
        return job.model_dump(mode="json")

    return router
