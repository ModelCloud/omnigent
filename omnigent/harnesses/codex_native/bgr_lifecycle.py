"""Atomic isolated reviewer startup; ordinary runner machinery owns provider turns."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from omnigent.bgr import Commit, Label
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerResponseError,
    CodexRequestFn,
)
from omnigent.harnesses.codex_native.bgr import NativeCapabilities, _result, _Wire
from omnigent.harnesses.codex_native.bgr_messages import (
    NativeBgrConnection,
    NativeIdentity,
    NativeMailboxSnapshot,
    NativeThreadId,
    native_contract_error,
)


class NativeReviewInput(_Wire):
    repository: Annotated[str, Field(min_length=1, max_length=1024)]
    base_sha: Commit
    head_sha: Commit

    @field_validator("repository")
    @classmethod
    def local_repository(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value:
            raise ValueError("Review requires a trusted absolute host repository path")
        return value


class NativeReviewOptions(_Wire):
    """Provider-facing values resolved from the selected catalog row by trusted host code."""

    model_config = ConfigDict(populate_by_name=True)

    model: Label
    model_provider: Label
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None


class NativeReviewStart(_Wire):
    identity: NativeIdentity
    peers: Annotated[list[NativeIdentity], Field(min_length=1, max_length=1)]
    review: NativeReviewInput
    model: Label
    model_provider: Label
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None

    @model_validator(mode="after")
    def independent_peer(self) -> Self:
        peer = self.peers[0]
        if (
            self.identity.role != "reviewer"
            or peer.role != "brain"
            or self.identity.team_id != peer.team_id
            or self.identity.epoch != peer.epoch
            or self.identity.session_id == peer.session_id
            or self.identity.agent_id == peer.agent_id
        ):
            raise ValueError("Isolated reviewer requires exactly its independent Brain peer")
        return self

    def wire(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class NativeReviewManifest(_Wire):
    base_sha: Commit
    head_sha: Commit
    changed_files: Annotated[list[Annotated[str, Field(max_length=1024)]], Field(max_length=4096)]
    coverage_limits: Annotated[list[Annotated[str, Field(max_length=2000)]], Field(max_length=128)]

    @field_validator("changed_files")
    @classmethod
    def relative_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            if (
                path.startswith("/")
                or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or any(ord(char) < 32 for char in path)
            ):
                raise ValueError("Review manifest requires bounded relative source paths")
        return value


class NativeReviewStarted(_Wire):
    thread_id: NativeThreadId
    snapshot: NativeMailboxSnapshot
    review: NativeReviewManifest


@dataclass(frozen=True)
class NativeIsolatedReviewer:
    connection: NativeBgrConnection
    manifest: NativeReviewManifest
    snapshot_root: str

    @classmethod
    async def start(cls, request: CodexRequestFn, params: NativeReviewStart) -> Self:
        """Call once after durable start claim; an ambiguous response must not be retried."""
        try:
            capabilities = NativeCapabilities.model_validate(
                _result(await request("bgr/capabilities/read", {}))
            )
            if (
                not capabilities.reviewer_start
                or not capabilities.host_record
                or not capabilities.reviewer_snapshot_no_lazy_fetch
            ):
                raise native_contract_error()
            started = NativeReviewStarted.model_validate(
                _result(await request("bgr/reviewer/start", params.wire()))
            )
        except (ValidationError, CodexAppServerResponseError):
            raise native_contract_error() from None
        if (
            started.snapshot.identity != params.identity
            or started.snapshot.cancelled
            or started.snapshot.sequence != 0
            or started.snapshot.pending
            or started.snapshot.replies
            or started.review.base_sha != params.review.base_sha
            or started.review.head_sha != params.review.head_sha
        ):
            raise native_contract_error()
        connection = NativeBgrConnection(started.thread_id, params.identity, request, capabilities)
        await connection.inspect()
        document = _result(
            await request("thread/read", {"threadId": started.thread_id, "includeTurns": False})
        )
        thread = document.get("thread") if isinstance(document, dict) else None
        root = thread.get("cwd") if isinstance(thread, dict) else None
        if (
            not isinstance(thread, dict)
            or thread.get("id") != started.thread_id
            or not isinstance(root, str)
            or not root.startswith("/")
            or posixpath.normpath(root) != root
            or "\x00" in root
            or root == params.review.repository
        ):
            raise native_contract_error()
        return cls(connection, started.review, root)
