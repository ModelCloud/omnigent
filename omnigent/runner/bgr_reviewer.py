"""Serialized durable reviewer launch on runner-owned native processes and callback streams."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from omnigent.bgr import NativeBinding, Workspace
from omnigent.harnesses.codex_native.app_server import CodexRequestFn
from omnigent.harnesses.codex_native.bgr_lifecycle import (
    NativeIsolatedReviewer,
    NativeReviewOptions,
    NativeReviewStart,
)
from omnigent.harnesses.codex_native.bgr_messages import NativeBgrConnection, native_contract_error
from omnigent.runner.bgr_mailbox import BgrNativeMailbox
from omnigent.stores.bgr_review_store import BgrReviewStore


class BgrReviewerRunner:
    """The host supplies one process per reviewer; task delivery owns native turn dispatch."""

    def __init__(
        self,
        mailbox: BgrNativeMailbox,
        brain: NativeBgrConnection,
        register: Callable[[NativeBgrConnection], None],
    ):
        if not isinstance(mailbox.store, BgrReviewStore) or brain.identity.role != "brain":
            raise native_contract_error()
        self.mailbox = mailbox
        self.store = mailbox.store
        self.brain = brain
        self.register = register
        self._launch_lock = asyncio.Lock()

    async def start(
        self,
        *,
        round_id: str,
        review_id: str,
        repository_path: str,
        host_id: str,
        options: NativeReviewOptions,
        request: CodexRequestFn,
        stop: Callable[[], Awaitable[bool]],
    ) -> NativeIsolatedReviewer | None:
        async with self._launch_lock:
            return await self._start(
                round_id, review_id, repository_path, host_id, options, request, stop
            )

    async def _start(
        self,
        round_id: str,
        review_id: str,
        repository_path: str,
        host_id: str,
        options: NativeReviewOptions,
        request: CodexRequestFn,
        stop: Callable[[], Awaitable[bool]],
    ) -> NativeIsolatedReviewer | None:
        mailbox = self.mailbox
        mailbox._connection(self.brain)
        round = self.store.get_review_round(
            mailbox.user_id, mailbox.brain_session_id, mailbox.team_id, round_id
        )
        job = next((job for job in round.jobs if job.review_id == review_id), None)
        if job is None or job.profile.harness != "codex-native":
            raise native_contract_error()
        params = NativeReviewStart.model_validate(
            {
                **options.model_dump(mode="json", by_alias=True, exclude_none=True),
                "identity": {
                    "teamId": mailbox.team_id,
                    "agentId": job.agent_id,
                    "sessionId": job.session_id,
                    "taskId": job.task_id,
                    "attemptId": job.attempt_id,
                    "epoch": mailbox.fence.epoch,
                    "role": "reviewer",
                },
                "peers": [self.brain.identity.wire()],
                "review": {
                    "repository": repository_path,
                    "baseSha": round.base_sha,
                    "headSha": round.head_sha,
                },
            }
        )
        if not self.store.claim_reviewer_start(
            mailbox.user_id,
            mailbox.brain_session_id,
            mailbox.team_id,
            mailbox.fence,
            round_id=round_id,
            review_id=review_id,
        ):
            return None
        try:
            started = await NativeIsolatedReviewer.start(request, params)
            self.store.activate_reviewer(
                mailbox.user_id,
                mailbox.brain_session_id,
                mailbox.team_id,
                mailbox.fence,
                round_id=round_id,
                review_id=review_id,
                binding=NativeBinding(
                    team_id=mailbox.team_id,
                    attempt_id=job.attempt_id,
                    epoch=mailbox.fence.epoch,
                    thread_id=started.connection.thread_id,
                ),
                workspace=Workspace(
                    host_id=host_id,
                    repository=round.repository,
                    worktree=started.snapshot_root,
                    base_sha=round.base_sha,
                ),
            )
            self.register(started.connection)
            assignments = self.store.native_assignments(
                mailbox.user_id, mailbox.brain_session_id, mailbox.team_id, mailbox.fence
            )
            await self.brain.authorize_peers(
                [mailbox.identity(assignment.attempt.attempt_id)[0] for assignment in assignments]
            )
            await mailbox.forward(self.brain, started.connection, job.task_id)
            return started
        except (Exception, asyncio.CancelledError):
            if await stop():
                self.store.settle_incomplete_review(
                    mailbox.user_id,
                    mailbox.brain_session_id,
                    mailbox.team_id,
                    mailbox.fence,
                    round_id=round_id,
                    review_id=review_id,
                    reason="startup_failed",
                    stop_confirmed=True,
                )
            raise
