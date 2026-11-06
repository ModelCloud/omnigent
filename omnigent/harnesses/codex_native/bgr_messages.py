"""Negotiated general-message RPCs; receipts attest acceptance, never consumption."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from omnigent.bgr import Commit, Identifier, Kind
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerResponseError,
    CodexRequestFn,
)
from omnigent.harnesses.codex_native.bgr import NativeCapabilities, _Wire

NativeMessageId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
NativeThreadId = Annotated[
    str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]


def native_contract_error() -> OmnigentError:
    return OmnigentError(
        "Native BGR message does not match its authenticated durable assignment",
        code=ErrorCode.CONFLICT,
    )


class NativeIdentity(_Wire):
    team_id: Identifier
    agent_id: Identifier
    session_id: Identifier
    epoch: Annotated[int, Field(ge=1, le=2**64 - 1)]
    role: Literal["brain", "grunt", "reviewer"]
    task_id: Identifier | None = None
    attempt_id: Identifier | None = None

    @model_validator(mode="after")
    def assignment(self) -> Self:
        if self.role == "brain":
            if self.task_id is not None or self.attempt_id is not None:
                raise ValueError("Brain has no worker assignment")
        elif self.task_id is None or self.attempt_id is None:
            raise ValueError("Workers require an exact task and attempt")
        return self

    def wire(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def sender(self) -> dict[str, JsonValue]:
        return {
            key: value for key, value in self.wire().items() if key not in {"taskId", "attemptId"}
        }


class NativeEnvelope(_Wire):
    version: Literal[1] = 1
    message_id: NativeMessageId
    sender_agent_id: Identifier
    recipient_agent_id: Identifier
    identity: NativeIdentity
    recipient_identity: NativeIdentity
    kind: Kind
    correlation_id: NativeMessageId
    in_reply_to: NativeMessageId | None = None
    payload: dict[str, JsonValue]
    head_sha: Commit | None = None

    @model_validator(mode="after")
    def peers(self) -> Self:
        source, target = self.identity, self.recipient_identity
        if (
            self.sender_agent_id != source.agent_id
            or self.recipient_agent_id != target.agent_id
            or source.team_id != target.team_id
            or source.epoch != target.epoch
            or (source.role == "brain") == (target.role == "brain")
        ):
            raise ValueError("general envelopes require exact source and destination identities")
        return self

    def wire(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class NativeNotification(_Wire):
    thread_id: NativeThreadId
    envelope: NativeEnvelope


class NativeMessageAcceptance(_Wire):
    message_id: NativeMessageId
    accepted: Literal[True]
    duplicate: bool


class NativeMailboxSnapshot(_Wire):
    identity: NativeIdentity
    cancelled: bool
    sequence: Annotated[int, Field(ge=0)]
    pending: Annotated[list[NativeEnvelope], Field(max_length=128)]
    replies: Annotated[list[NativeEnvelope], Field(max_length=128)]


class NativeMailboxState(_Wire):
    snapshot: NativeMailboxSnapshot


@dataclass(frozen=True)
class NativeBgrConnection:
    """An already-owned native session connection; no additional scheduler or turn."""

    thread_id: str
    identity: NativeIdentity
    request: CodexRequestFn
    capabilities: NativeCapabilities

    async def record(self, envelope: NativeEnvelope) -> NativeMessageAcceptance:
        if not self.capabilities.host_record or envelope.identity != self.identity:
            raise native_contract_error()
        return await self._accept("bgr/message/record", envelope)

    async def authorize_peers(self, peers: list[NativeIdentity]) -> None:
        if self.identity.role != "brain" or len({peer.agent_id for peer in peers}) != len(peers):
            raise native_contract_error()
        if any(
            peer.role == "brain"
            or peer.team_id != self.identity.team_id
            or peer.epoch != self.identity.epoch
            or peer.session_id == self.identity.session_id
            or peer.agent_id == self.identity.agent_id
            for peer in peers
        ):
            raise native_contract_error()
        try:
            response = await self.request(
                "bgr/thread/peers",
                {
                    "threadId": self.thread_id,
                    "identity": self.identity.wire(),
                    "peers": [peer.wire() for peer in peers],
                },
            )
            snapshot = NativeMailboxState.model_validate(response.get("result")).snapshot
        except (ValidationError, CodexAppServerResponseError):
            raise native_contract_error() from None
        if snapshot.identity != self.identity or snapshot.cancelled:
            raise native_contract_error()

    async def deliver(self, envelope: NativeEnvelope) -> NativeMessageAcceptance:
        if envelope.recipient_identity != self.identity:
            raise native_contract_error()
        return await self._accept("bgr/message/deliver", envelope)

    async def _accept(self, method: str, envelope: NativeEnvelope) -> NativeMessageAcceptance:
        params: dict[str, object] = {"threadId": self.thread_id, "envelope": envelope.wire()}
        if method == "bgr/message/record":
            params["identity"] = self.identity.wire()
        else:
            params["sender"] = envelope.identity.sender()
        try:
            response = await self.request(method, params)
            receipt = NativeMessageAcceptance.model_validate(response.get("result"))
        except (ValidationError, CodexAppServerResponseError):
            raise native_contract_error() from None
        if receipt.message_id != envelope.message_id:
            raise native_contract_error()
        return receipt

    async def inspect(self) -> NativeMailboxSnapshot:
        try:
            response = await self.request("bgr/thread/inspect", {"threadId": self.thread_id})
            snapshot = NativeMailboxState.model_validate(response.get("result")).snapshot
        except (ValidationError, CodexAppServerResponseError):
            raise native_contract_error() from None
        if snapshot.identity != self.identity or snapshot.cancelled:
            raise native_contract_error()
        return snapshot
