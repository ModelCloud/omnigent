"""Opt-in host/native BGR translation, not a launcher or event dispatcher."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator
from pydantic.alias_generators import to_camel

from omnigent.bgr import (
    Commit,
    Envelope,
    Fence,
    Identifier,
    NativeAssignment,
    NativeBinding,
    Receipt,
    Send,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerResponseError,
    CodexMessage,
    CodexRequestFn,
)
from omnigent.stores.bgr_store import BgrStore


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="forbid", strict=True, frozen=True)


def _require_tool(tool: dict[str, JsonValue] | None, name: str, required: set[str]) -> None:
    schema = tool.get("inputSchema") if tool else None
    fields = schema.get("required") if isinstance(schema, dict) else None
    if (
        not tool
        or tool.get("type") != "function"
        or tool.get("name") != name
        or not isinstance(tool.get("description"), str)
        or tool.get("deferLoading", False) is not False
        or not isinstance(schema, dict)
        or schema.get("type") != "object"
        or not isinstance(fields, list)
        or not all(isinstance(field, str) for field in fields)
        or not required <= set(fields)
    ):
        raise ValueError(f"requires a nondeferred {name} dynamic tool specification")


class NativeCapabilities(_Wire):
    version: Annotated[int, Field(ge=1, le=1)]
    roles: list[str]
    status_scopes: list[str]
    message_kinds: list[str]
    report_tool: dict[str, JsonValue]
    host_delivery: bool
    live_reattach: bool
    durable_mailbox: bool
    restart_recovery: bool
    reviewer_sandbox: bool
    cross_host_delivery: bool
    task_dispatch: bool
    max_queries_per_attempt: Annotated[int, Field(ge=1, le=128)]
    send_tool: dict[str, JsonValue] | None = None
    reply_tool: dict[str, JsonValue] | None = None
    general_messaging: bool = False
    message_callbacks: bool = False
    consumption_receipts: bool = False
    status_evidence: Annotated[str, Field(min_length=1, max_length=512)] | None = None

    @model_validator(mode="after")
    def supported_contract(self) -> Self:
        if (
            set(self.roles) != {"brain", "grunt"}
            or not self.host_delivery
            or not self.live_reattach
            or any(
                (
                    self.durable_mailbox,
                    self.restart_recovery,
                    self.reviewer_sandbox,
                    self.cross_host_delivery,
                )
            )
        ):
            raise ValueError("requires the v1 host-delivery brain/grunt capability contract")
        _require_tool(self.report_tool, "bgr_report_status", {"messageId", "inReplyTo", "payload"})
        if self.general_messaging:
            if (
                set(self.status_scopes) != {"now", "since_time", "since_commit"}
                or set(self.message_kinds)
                != {
                    "task",
                    "question",
                    "answer",
                    "progress",
                    "result",
                    "cancel",
                    "status_query",
                    "status_reply",
                }
                or not self.task_dispatch
                or not self.message_callbacks
                or self.status_evidence is None
            ):
                raise ValueError("incomplete native general-message capability contract")
            _require_tool(
                self.send_tool,
                "bgr_send_message",
                {"messageId", "recipientAgentId", "kind", "correlationId", "payload"},
            )
            _require_tool(
                self.reply_tool, "bgr_reply_message", {"messageId", "inReplyTo", "kind", "payload"}
            )
        elif (
            self.status_scopes != ["now"]
            or set(self.message_kinds) != {"status_query", "status_reply"}
            or self.task_dispatch
            or self.message_callbacks
            or self.consumption_receipts
            or self.send_tool is not None
            or self.reply_tool is not None
            or self.status_evidence is not None
        ):
            raise ValueError("requires the v1 host-delivery status-only capability contract")
        return self


class _Identity(_Wire):
    team_id: Identifier
    agent_id: Identifier
    session_id: Identifier
    task_id: Identifier
    attempt_id: Identifier
    epoch: Annotated[int, Field(ge=1, le=2**64 - 1)]
    role: Literal["grunt"]


class _Envelope(_Wire):
    version: Annotated[int, Field(ge=1, le=1)]
    message_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
    sender_agent_id: Identifier
    recipient_agent_id: Identifier
    identity: _Identity
    kind: Literal["status_query", "status_reply"]
    correlation_id: Identifier
    in_reply_to: Identifier | None = None
    payload: dict[str, JsonValue]
    head_sha: Commit | None = None


class _StatusReply(_Wire):
    current_action: str
    completed: list[str]
    pending: list[str]
    blockers: list[str]
    head_sha: Commit | None = None
    dirty: bool | None = None
    coverage_limits: list[str]


class _Snapshot(_Wire):
    identity: _Identity
    cancelled: bool
    sequence: Annotated[int, Field(ge=0, le=2**32 - 1)]
    pending: Annotated[list[_Envelope], Field(max_length=128)]
    replies: Annotated[list[_Envelope], Field(max_length=128)]


class _State(_Wire):
    snapshot: _Snapshot


class _Accepted(_Wire):
    message_id: Identifier
    accepted: bool
    duplicate: bool


class _Notification(_Wire):
    thread_id: str
    envelope: _Envelope


def _conflict(detail: str) -> OmnigentError:
    return OmnigentError(f"Native BGR: {detail}", code=ErrorCode.CONFLICT)


def _result(response: CodexMessage) -> object:
    if response.get("error") or not isinstance(response.get("result"), dict):
        raise _conflict("RPC returned no successful result object")
    return response["result"]


def _identity(assignment: NativeAssignment) -> _Identity:
    attempt = assignment.attempt
    return _Identity.model_validate(
        {
            "teamId": attempt.team_id,
            "agentId": attempt.agent_id,
            "sessionId": attempt.session_id,
            "taskId": attempt.task_id,
            "attemptId": attempt.attempt_id,
            "epoch": attempt.epoch,
            "role": "grunt",
        }
    )


def _query_wire(assignment: NativeAssignment, query: Envelope) -> _Envelope:
    if query.payload != {"scope": "now"}:
        raise _conflict("only scope=now status queries can be translated")
    return _Envelope.model_validate(
        {
            "version": 1,
            "messageId": query.message_id,
            "senderAgentId": query.sender_agent_id,
            "recipientAgentId": query.recipient_agent_id,
            "identity": _identity(assignment),
            "kind": "status_query",
            "correlationId": query.correlation_id,
            "inReplyTo": None,
            "payload": query.payload,
            "headSha": query.head_sha,
        }
    )


@dataclass(frozen=True)
class CodexNativeBgrAdapter:
    """Trusted controller helper using an already initialized worker RPC connection.

    Callers own authenticated runner routing and notification subscription. No
    request here starts/resumes/interrupts a turn or acknowledges host consumption.
    """

    request: CodexRequestFn
    store: BgrStore
    user_id: str
    brain_session_id: str
    team_id: str
    fence: Fence
    capabilities: NativeCapabilities

    @classmethod
    async def negotiate(
        cls,
        request: CodexRequestFn,
        store: BgrStore,
        *,
        user_id: str,
        brain_session_id: str,
        team_id: str,
        fence: Fence,
    ) -> Self:
        try:
            capabilities = NativeCapabilities.model_validate(
                _result(await request("bgr/capabilities/read", {}))
            )
        except (ValueError, OmnigentError, CodexAppServerResponseError):
            raise OmnigentError(
                "Native BGR requires compatible experimental v1 hostDelivery capabilities",
                code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
            ) from None
        return cls(request, store, user_id, brain_session_id, team_id, fence, capabilities)

    def startup_options(self) -> CodexMessage:
        """Required thread/start options for the caller; never start a thread here."""
        return {
            "config": {
                "features.multi_agent": False,
                "features.multi_agent_v2": False,
                "features.defer_mailbox_preemption": True,
            },
            "dynamicTools": [
                self.capabilities.model_dump(mode="json", by_alias=True)["reportTool"]
            ],
        }

    def _assignment(self, attempt_id: str) -> NativeAssignment:
        return self.store.native_assignment(
            self.user_id,
            self.brain_session_id,
            self.team_id,
            attempt_id,
            self.fence,
        )

    def _query(self, attempt_id: str, message_id: str) -> Envelope:
        return self.store.native_query(
            self.user_id,
            self.brain_session_id,
            self.team_id,
            attempt_id,
            self.fence,
            message_id,
        )

    @staticmethod
    def _state(response: CodexMessage, assignment: NativeAssignment) -> _Snapshot:
        try:
            snapshot = _State.model_validate(_result(response)).snapshot
        except ValidationError as exc:
            raise _conflict(f"malformed thread snapshot: {exc}") from exc
        if snapshot.identity != _identity(assignment) or snapshot.cancelled:
            raise _conflict("thread snapshot is foreign, stale or cancelled")
        return snapshot

    async def bind(self, attempt_id: str, thread_id: str) -> NativeBinding:
        assignment = self.store.bind_native(
            self.user_id,
            self.brain_session_id,
            self.team_id,
            self.fence,
            NativeBinding(
                team_id=self.team_id,
                attempt_id=attempt_id,
                epoch=self.fence.epoch,
                thread_id=thread_id,
            ),
        )
        response = await self.request(
            "bgr/thread/bind",
            {
                "threadId": thread_id,
                "identity": _identity(assignment).model_dump(mode="json", by_alias=True),
            },
        )
        self._state(response, self._assignment(attempt_id))
        return assignment.binding

    async def deliver(self, attempt_id: str, message_id: str) -> bool:
        """Return native duplicate flag, not a durable delivery/consumption receipt."""
        assignment = self._assignment(attempt_id)
        query = self._query(attempt_id, message_id)
        if query.consumed_at is not None:
            raise _conflict("only unconsumed status queries can be delivered")
        wire = _query_wire(assignment, query)
        response = await self.request(
            "bgr/message/deliver",
            {
                "threadId": assignment.binding.thread_id,
                "sender": {
                    "teamId": query.team_id,
                    "agentId": query.sender_agent_id,
                    "sessionId": query.sender_session_id,
                    "epoch": query.epoch,
                    "role": query.sender_role,
                },
                "envelope": wire.model_dump(mode="json", by_alias=True),
            },
        )
        try:
            receipt = _Accepted.model_validate(_result(response))
        except ValidationError as exc:
            raise _conflict(f"malformed native acceptance: {exc}") from exc
        if receipt.message_id != query.message_id or not receipt.accepted:
            raise _conflict("native receipt did not accept the original message ID")
        self._query(attempt_id, message_id)
        return receipt.duplicate

    def _receive(self, assignment: NativeAssignment, reply: _Envelope) -> Receipt:
        if (
            reply.kind != "status_reply"
            or reply.identity != _identity(assignment)
            or reply.sender_agent_id != assignment.attempt.agent_id
            or reply.recipient_agent_id != assignment.team.brain_agent_id
            or reply.in_reply_to is None
        ):
            raise _conflict("reply sender, recipient or assignment mismatch")
        query = self._query(assignment.attempt.attempt_id, reply.in_reply_to)
        if (
            query.payload != {"scope": "now"}
            or reply.correlation_id != query.correlation_id
            or reply.head_sha != query.head_sha
        ):
            raise _conflict("reply correlation, query scope or head anchor mismatch")
        try:
            payload = _StatusReply.model_validate(reply.payload).model_dump(
                mode="json", by_alias=True
            )
            payload["native_provenance"] = {
                "thread_id": assignment.binding.thread_id,
                "message_id": reply.message_id,
            }
            scoped_id = json.dumps(
                [
                    "omnigent/codex-native/bgr/reply/v1",
                    reply.identity.model_dump(by_alias=True),
                    assignment.binding.thread_id,
                    reply.message_id,
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
            body = Send(
                message_id=uuid5(NAMESPACE_URL, scoped_id).hex,
                recipient_agent_id=reply.recipient_agent_id,
                task_id=reply.identity.task_id,
                attempt_id=reply.identity.attempt_id,
                epoch=reply.identity.epoch,
                kind="status_reply",
                correlation_id=reply.correlation_id,
                in_reply_to=reply.in_reply_to,
                head_sha=reply.head_sha,
                payload=payload,
            )
        except ValidationError as exc:
            raise _conflict(
                f"reply must match status schema and host 8000-byte payload bound: {exc}"
            ) from exc
        return self.store.send(
            self.user_id,
            assignment.attempt.session_id,
            self.team_id,
            body,
        )

    def receive_notification(self, attempt_id: str, params: object) -> Receipt:
        """Handle bgr/message params from the caller's authenticated worker connection."""
        assignment = self._assignment(attempt_id)
        try:
            notification = _Notification.model_validate(params)
        except ValidationError as exc:
            raise _conflict(f"malformed notification: {exc}") from exc
        if notification.thread_id != assignment.binding.thread_id:
            raise _conflict("notification thread does not match the reserved worker")
        return self._receive(assignment, notification.envelope)

    async def inspect(self, attempt_id: str) -> list[Receipt]:
        """One explicit live reconciliation, not polling or process restart recovery."""
        assignment = self._assignment(attempt_id)
        response = await self.request(
            "bgr/thread/inspect", {"threadId": assignment.binding.thread_id}
        )
        snapshot = self._state(response, self._assignment(attempt_id))
        for pending in snapshot.pending:
            query = self._query(attempt_id, pending.message_id)
            if pending != _query_wire(assignment, query):
                raise _conflict("inspect returned a changed or foreign pending query")
        return [self._receive(assignment, reply) for reply in snapshot.replies]
