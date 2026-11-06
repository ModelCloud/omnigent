"""Synchronous publisher ledger backed by the authenticated server, never runner SQL."""

import asyncio
import hashlib

import httpx
from pydantic import JsonValue

from omnigent.bgr import Fence
from omnigent.bgr_review import ReviewJob, ReviewRound, publication_body
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.bgr_publisher import BgrGitHubPublisher
from omnigent.server.session_orchestration import ORCHESTRATION_SOURCE_HEADER


class RemotePublicationLedger:
    def __init__(self, client: httpx.AsyncClient, session_id: str, team_id: str):
        self.client = client
        self.session_id = session_id
        self.team_id = team_id
        self.loop = asyncio.get_running_loop()

    def _request(self, operation: str, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        async def request() -> dict[str, JsonValue]:
            try:
                response = await self.client.post(
                    f"/v1/bgr/teams/{self.team_id}/publication-ledger",
                    headers={ORCHESTRATION_SOURCE_HEADER: self.session_id},
                    json={"operation": operation, **parameters},
                    timeout=30,
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("Invalid publication ledger response")
                return result
            except (httpx.HTTPError, ValueError):
                raise ValueError(
                    "Publication ledger unavailable; reconcile before retry"
                ) from None

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is self.loop:
            raise RuntimeError("Publication ledger must run off the event loop")
        future = asyncio.run_coroutine_threadsafe(request(), self.loop)
        try:
            return future.result(timeout=45)
        except TimeoutError:
            future.cancel()
            raise ValueError("Publication ledger unavailable; reconcile before retry") from None

    def _scope(self, session_id: str, team_id: str) -> None:
        if session_id != self.session_id or team_id != self.team_id:
            raise ValueError("Foreign publication ledger")

    def get_review_round(
        self, user_id: str, session_id: str, team_id: str, round_id: str
    ) -> ReviewRound:
        del user_id
        self._scope(session_id, team_id)
        return ReviewRound.model_validate(self._request("read", {"round_id": round_id}))

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
        round = self.get_review_round(user_id, session_id, team_id, round_id)
        body = publication_body(round, review_id, private_values=private_values)
        return ReviewJob.model_validate(
            self._request(
                "claim",
                {
                    "round_id": round_id,
                    "review_id": review_id,
                    "claim_id": claim_id,
                    "fence": fence.model_dump(mode="json"),
                    "author_login": author_login,
                    "approved_body_sha256": approved_body_sha256,
                    "approved_body": body,
                },
            )
        )

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
    ) -> ReviewJob:
        self._scope(session_id, team_id)
        del user_id
        return ReviewJob.model_validate(
            self._request(
                "reconcile",
                {
                    "round_id": round_id,
                    "review_id": review_id,
                    "claim_id": claim_id,
                    "fence": fence.model_dump(mode="json"),
                    "comment_id": comment_id,
                    "next_page": next_page,
                },
            )
        )


async def run_publication(
    operation: str,
    parameters: dict[str, JsonValue],
    *,
    client: httpx.AsyncClient,
    session_id: str,
    team_id: str,
    root: str,
) -> dict[str, JsonValue]:
    if operation not in {"publication_preview", "publish", "publication_reconcile"}:
        raise ValueError("Unknown trusted publication operation")
    if parameters.get("team_id") != team_id:
        raise OmnigentError("Foreign publication team", code=ErrorCode.FORBIDDEN)
    ledger = RemotePublicationLedger(client, session_id, team_id)
    publisher = BgrGitHubPublisher(ledger, root)
    round_id = str(parameters["round_id"])
    review_id = str(parameters["review_id"])
    if operation == "publication_preview":
        body = await asyncio.to_thread(
            publisher.preview, "", session_id, team_id, round_id=round_id, review_id=review_id
        )
        return {"body": body, "approved_body_sha256": hashlib.sha256(body.encode()).hexdigest()}
    job = await asyncio.to_thread(
        publisher.publish,
        "",
        session_id,
        team_id,
        Fence.model_validate(parameters["fence"]),
        round_id=round_id,
        review_id=review_id,
        approved_body_sha256=str(parameters["approved_body_sha256"]),
        reconcile_only=operation == "publication_reconcile",
    )
    return job.model_dump(mode="json")
