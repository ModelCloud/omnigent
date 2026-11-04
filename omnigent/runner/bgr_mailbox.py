"""Durable native role routing on caller-owned connections, without scheduling turns."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from uuid import NAMESPACE_URL, uuid5

from pydantic import JsonValue, ValidationError

from omnigent.bgr import Envelope, Fence, NativeMessageOrigin, Receipt, Send
from omnigent.bgr_review import (
    ReviewResult,
    ReviewRound,
    normalize_native_review_result,
    sanitize_review_payload,
)
from omnigent.errors import OmnigentError
from omnigent.harnesses.codex_native.bgr_messages import (
    NativeBgrConnection,
    NativeEnvelope,
    NativeIdentity,
    NativeMessageAcceptance,
    NativeNotification,
    native_contract_error,
)
from omnigent.runner import bgr_publisher
from omnigent.stores.bgr_review_store import BgrReviewStore
from omnigent.stores.bgr_store import BgrStore


def _host_message_id(team_id: str, agent_id: str, epoch: int, native_id: str) -> str:
    key = json.dumps(["omnigent/bgr/native/message/v1", team_id, agent_id, epoch, native_id])
    return uuid5(NAMESPACE_URL, key).hex


def _host_correlation(team_id: str, attempt_id: str, epoch: int, native_id: str) -> str:
    if re.fullmatch(r"[0-9a-f]{32}", native_id):
        return native_id
    key = json.dumps(["omnigent/bgr/native/correlation/v1", team_id, attempt_id, epoch, native_id])
    return uuid5(NAMESPACE_URL, key).hex


class BgrNativeMailbox:
    """Validate callbacks and retry delivery; the existing runner owns turns and wakeups."""

    def __init__(
        self,
        store: BgrStore,
        user_id: str,
        brain_session_id: str,
        team_id: str,
        fence: Fence,
        *,
        root: str,
        private_values: Callable[[], tuple[str, ...]] | None = None,
    ):
        self.store = store
        self.user_id = user_id
        self.brain_session_id = brain_session_id
        self.team_id = team_id
        self.fence = fence
        self._private_values = private_values or (
            lambda: bgr_publisher._private_values(root, require_forge=False)
        )

    def identity(self, attempt_id: str | None) -> tuple[NativeIdentity, str]:
        if attempt_id is None:
            binding = self.store.native_brain_binding(
                self.user_id, self.brain_session_id, self.team_id, self.fence
            )
            team = self.store.get_team(self.user_id, self.brain_session_id, self.team_id)
            return NativeIdentity.model_validate(
                {
                    "teamId": self.team_id,
                    "agentId": team.brain_agent_id,
                    "sessionId": self.brain_session_id,
                    "epoch": self.fence.epoch,
                    "role": "brain",
                }
            ), binding.thread_id
        assignment = self.store.native_assignment(
            self.user_id, self.brain_session_id, self.team_id, attempt_id, self.fence
        )
        attempt = assignment.attempt
        return NativeIdentity.model_validate(
            {
                "teamId": self.team_id,
                "agentId": attempt.agent_id,
                "sessionId": attempt.session_id,
                "taskId": attempt.task_id,
                "attemptId": attempt.attempt_id,
                "epoch": attempt.epoch,
                "role": "reviewer" if attempt.review_round_id is not None else "grunt",
            }
        ), assignment.binding.thread_id

    def _connection(self, connection: NativeBgrConnection) -> None:
        expected, thread_id = self.identity(connection.identity.attempt_id)
        if (
            connection.identity != expected
            or connection.thread_id != thread_id
            or not connection.capabilities.general_messaging
            or not connection.capabilities.host_record
            or (expected.role == "reviewer" and not connection.capabilities.reviewer_start)
        ):
            raise native_contract_error()

    def _message(self, sender_agent_id: str, message_id: str) -> Envelope:
        return self.store.native_message(
            self.user_id,
            self.brain_session_id,
            self.team_id,
            self.fence,
            sender_agent_id=sender_agent_id,
            message_id=message_id,
        )

    def _review_round(self, source: NativeBgrConnection) -> ReviewRound:
        attempt_id = source.identity.attempt_id
        if not isinstance(self.store, BgrReviewStore) or attempt_id is None:
            raise native_contract_error()
        assignment = self.store.native_assignment(
            self.user_id, self.brain_session_id, self.team_id, attempt_id, self.fence
        )
        round_id = assignment.attempt.review_round_id
        if round_id is None:
            raise native_contract_error()
        return self.store.get_review_round(
            self.user_id, self.brain_session_id, self.team_id, round_id
        )

    def _payload(
        self,
        source: NativeBgrConnection,
        kind: str,
        payload: dict[str, JsonValue],
        private: tuple[str, ...],
    ) -> dict[str, JsonValue]:
        safe = sanitize_review_payload(payload, private_values=private)
        if not isinstance(safe, dict):
            raise native_contract_error()
        if source.identity.role != "reviewer" or kind != "result":
            return safe
        round = self._review_round(source)
        job = next(
            (job for job in round.jobs if job.attempt_id == source.identity.attempt_id), None
        )
        if job is None:
            raise native_contract_error()
        try:
            if set(safe) == {"round_id", "review_id", "result"}:
                if safe["round_id"] != round.round_id or safe["review_id"] != job.review_id:
                    raise native_contract_error()
                result = ReviewResult.model_validate(safe["result"])
            else:
                result = normalize_native_review_result(safe)
        except ValueError:
            raise native_contract_error() from None
        return {
            "round_id": round.round_id,
            "review_id": job.review_id,
            "result": result.model_dump(mode="json"),
        }

    def _anchor(self, sender_agent_id: str, native_id: str) -> Envelope:
        if re.fullmatch(r"[0-9a-f]{32}", native_id):
            try:
                anchor = self._message(sender_agent_id, native_id)
            except OmnigentError:
                pass
            else:
                if anchor.native_origin is None:
                    return anchor
        anchor = self._message(
            sender_agent_id,
            _host_message_id(self.team_id, sender_agent_id, self.fence.epoch, native_id),
        )
        if anchor.native_origin is None or anchor.native_origin.message_id != native_id:
            raise native_contract_error()
        return anchor

    def receive(self, source: NativeBgrConnection, params: object) -> Receipt:
        """Called only from the authenticated source's existing native notification stream."""
        self._connection(source)
        try:
            notification = NativeNotification.model_validate(params)
        except ValidationError:
            raise native_contract_error() from None
        wire = notification.envelope
        target, _ = self.identity(wire.recipient_identity.attempt_id)
        if (
            notification.thread_id != source.thread_id
            or wire.identity != source.identity
            or wire.recipient_identity != target
        ):
            raise native_contract_error()
        worker = source.identity if source.identity.role != "brain" else target
        if worker.attempt_id is None or worker.task_id is None:
            raise native_contract_error()
        private = self._private_values()
        metadata = wire.model_dump(mode="json", by_alias=True, exclude={"payload"})
        if any(value and value in json.dumps(metadata) for value in private):
            raise native_contract_error()
        payload = self._payload(source, wire.kind, wire.payload, private)
        anchor = self._anchor(target.agent_id, wire.in_reply_to) if wire.in_reply_to else None
        if anchor is not None and wire.correlation_id != (
            anchor.native_origin.correlation_id if anchor.native_origin else anchor.correlation_id
        ):
            raise native_contract_error()
        try:
            origin = NativeMessageOrigin(
                thread_id=source.thread_id,
                message_id=wire.message_id,
                correlation_id=wire.correlation_id,
                in_reply_to=wire.in_reply_to,
            )
            body = Send(
                message_id=_host_message_id(
                    self.team_id, source.identity.agent_id, self.fence.epoch, wire.message_id
                ),
                recipient_agent_id=target.agent_id,
                task_id=worker.task_id,
                attempt_id=worker.attempt_id,
                epoch=self.fence.epoch,
                controller_id=self.fence.controller_id
                if source.identity.role == "brain"
                else None,
                kind=wire.kind,
                correlation_id=_host_correlation(
                    self.team_id, worker.attempt_id, self.fence.epoch, wire.correlation_id
                ),
                in_reply_to=anchor.message_id if anchor else None,
                head_sha=wire.head_sha,
                payload=payload,
            )
        except ValidationError:
            raise native_contract_error() from None
        return self.store.send(
            self.user_id, source.identity.session_id, self.team_id, body, native_origin=origin
        )

    def wire(
        self, envelope: Envelope, source: NativeBgrConnection, target: NativeBgrConnection
    ) -> NativeEnvelope:
        self._connection(source)
        self._connection(target)
        worker = source.identity if source.identity.role != "brain" else target.identity
        if (
            envelope.sender_agent_id != source.identity.agent_id
            or envelope.sender_session_id != source.identity.session_id
            or envelope.sender_role != source.identity.role
            or envelope.recipient_agent_id != target.identity.agent_id
            or envelope.team_id != self.team_id
            or envelope.epoch != self.fence.epoch
            or envelope.attempt_id != worker.attempt_id
            or envelope.task_id != worker.task_id
            or envelope.correlation_id is None
            or envelope.discard is not None
        ):
            raise native_contract_error()
        origin = envelope.native_origin
        if origin is not None and origin.thread_id != source.thread_id:
            raise native_contract_error()
        reply_to = envelope.in_reply_to
        if reply_to is not None:
            anchor = self._message(target.identity.agent_id, reply_to)
            if anchor.native_origin is not None:
                reply_to = anchor.native_origin.message_id
        return NativeEnvelope.model_validate(
            {
                "messageId": origin.message_id if origin else envelope.message_id,
                "senderAgentId": envelope.sender_agent_id,
                "recipientAgentId": envelope.recipient_agent_id,
                "identity": source.identity,
                "recipientIdentity": target.identity,
                "kind": envelope.kind,
                "correlationId": origin.correlation_id if origin else envelope.correlation_id,
                "inReplyTo": reply_to,
                "payload": envelope.payload,
                "headSha": envelope.head_sha,
            }
        )

    async def forward(
        self, source: NativeBgrConnection, target: NativeBgrConnection, message_id: str
    ) -> NativeMessageAcceptance | None:
        """Persist first, record host-created source anchors, then deliver idempotently."""
        envelope = self._message(source.identity.agent_id, message_id)
        wire = self.wire(envelope, source, target)
        if source.identity.role == "reviewer" and envelope.kind == "result":
            if any(job.state != "sealed" for job in self._review_round(source).jobs):
                return None
        if envelope.native_origin is None:
            await source.record(wire)
        else:
            snapshot = await source.inspect()
            private = self._private_values()
            originals = [
                reply for reply in snapshot.replies if reply.message_id == wire.message_id
            ]
            if (
                len(originals) != 1
                or originals[0].model_copy(
                    update={
                        "payload": self._payload(
                            source, originals[0].kind, originals[0].payload, private
                        )
                    }
                )
                != wire
            ):
                raise native_contract_error()
        self._connection(source)
        self._connection(target)
        return await target.deliver(wire)

    async def reconcile(self, source: NativeBgrConnection) -> list[Receipt]:
        """Re-ingest live native outbox evidence after connection loss, not process loss."""
        self._connection(source)
        snapshot = await source.inspect()
        return [
            self.receive(source, {"threadId": source.thread_id, "envelope": reply.wire()})
            for reply in snapshot.replies
        ]
