"""Transactional BGR reservations and mailbox; no process launch or model execution.

The team row serializes mutations across replicas. Unique database constraints
also protect session and workspace reservations across different teams.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import TypeVar
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.bgr import (
    Ack,
    Agent,
    Attempt,
    Checkpoint,
    ConfigUpdate,
    Contract,
    DiscardEvidence,
    Envelope,
    Fence,
    LeaseRequest,
    MailboxDiscard,
    Receipt,
    Recovery,
    Send,
    Task,
    TaskClose,
    TaskReserve,
    Team,
    TeamCreate,
)
from omnigent.db.bgr_models import (
    SqlBgrAgent,
    SqlBgrAttempt,
    SqlBgrEnvelope,
    SqlBgrTask,
    SqlBgrTeam,
)
from omnigent.db.db_models import current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.errors import ErrorCode, OmnigentError

_T = TypeVar("_T")
MAX_TASKS = 256
MAX_ENVELOPES = 4096
MAX_PENDING = 256


def _conflict(detail: str) -> OmnigentError:
    return OmnigentError(detail, code=ErrorCode.CONFLICT)


def _forbidden() -> OmnigentError:
    return OmnigentError("BGR team or agent is not owned or available", code=ErrorCode.FORBIDDEN)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _team(row: SqlBgrTeam) -> Team:
    return Team.model_validate_json(row.document).model_copy(
        update={
            "epoch": row.epoch,
            "controller_id": row.controller_id,
            "lease_expires_at": row.lease_expires_at,
        }
    )


def _envelope(row: SqlBgrEnvelope) -> Envelope:
    return Envelope.model_validate_json(row.document).model_copy(
        update={"consumed_at": row.consumed_at, "replied_at": row.replied_at}
    )


class BgrStore:
    def __init__(self, storage_location: str, *, clock: Callable[[], float] = time.time):
        self._sessions = make_named_managed_session_maker(
            get_or_create_engine(storage_location),
            query_name_prefix="omnigent.bgr_store",
            immediate=True,
        )
        self._clock = clock

    def create(self, user_id: str, session_id: str, body: TeamCreate) -> Team:
        def apply(db: Session) -> Team:
            row = db.get(SqlBgrTeam, (current_workspace_id(), body.team_id))
            if row is not None:
                existing = _team(row)
                if existing.owner_id != user_id or existing.brain_session_id != session_id:
                    raise _forbidden()
                if existing.config != body.config:
                    raise _conflict("team_id already has a different configuration")
                return existing
            agent = Agent(
                team_id=body.team_id,
                agent_id=uuid4().hex,
                session_id=session_id,
                role="brain",
                name="brain",
                profile=body.config.brain,
            )
            team = Team(
                team_id=body.team_id,
                owner_id=user_id,
                brain_agent_id=agent.agent_id,
                brain_session_id=session_id,
                config=body.config,
                epoch=0,
                controller_id=None,
                lease_expires_at=0,
            )
            db.add(
                SqlBgrTeam(
                    id=team.team_id,
                    owner_id=user_id,
                    brain_session_id=session_id,
                    document=team.model_dump_json(),
                    epoch=0,
                    lease_expires_at=0,
                    next_sequence=1,
                )
            )
            db.add(self._agent_row(agent))
            return team

        return self._write("create_team", apply)

    @staticmethod
    def _agent_row(agent: Agent) -> SqlBgrAgent:
        return SqlBgrAgent(
            id=agent.agent_id,
            team_id=agent.team_id,
            session_id=agent.session_id,
            document=agent.model_dump_json(),
        )

    def _write(self, name: str, callback: Callable[[Session], _T]) -> _T:
        try:
            return run_write_transaction(self._sessions, name, callback)
        except IntegrityError:
            try:
                return run_write_transaction(self._sessions, name, callback)
            except IntegrityError as exc:
                raise _conflict(
                    "BGR identity, session, or workspace is already reserved; reconcile first"
                ) from exc

    @staticmethod
    def _owned(db: Session, team_id: str, user_id: str, *, lock: bool = True) -> SqlBgrTeam:
        if lock:
            db.execute(
                update(SqlBgrTeam)
                .where(
                    SqlBgrTeam.workspace_id == current_workspace_id(),
                    SqlBgrTeam.id == team_id,
                    SqlBgrTeam.owner_id == user_id,
                )
                .values(epoch=SqlBgrTeam.epoch)
            )
        row = db.get(SqlBgrTeam, (current_workspace_id(), team_id))
        if row is None or row.owner_id != user_id:
            raise _forbidden()
        return row

    @staticmethod
    def _brain(row: SqlBgrTeam, session_id: str) -> None:
        if row.brain_session_id != session_id:
            raise _forbidden()

    def _epoch(self, row: SqlBgrTeam, epoch: int) -> None:
        if row.epoch != epoch or row.lease_expires_at <= self._clock():
            raise _conflict("BGR coordinator epoch is stale or its lease expired; reconcile first")

    def _fence(self, row: SqlBgrTeam, session_id: str, body: Fence) -> None:
        self._brain(row, session_id)
        self._epoch(row, body.epoch)
        if row.controller_id != body.controller_id:
            raise _conflict("Another BGR controller owns this epoch")

    @staticmethod
    def _agent(db: Session, team_id: str, session_id: str) -> Agent:
        row = db.scalar(
            select(SqlBgrAgent).where(
                SqlBgrAgent.workspace_id == current_workspace_id(),
                SqlBgrAgent.team_id == team_id,
                SqlBgrAgent.session_id == session_id,
            )
        )
        if row is None:
            raise _forbidden()
        return Agent.model_validate_json(row.document)

    @staticmethod
    def _attempt(db: Session, team_id: str, attempt_id: str) -> SqlBgrAttempt:
        row = db.get(SqlBgrAttempt, (current_workspace_id(), attempt_id))
        if row is None or row.team_id != team_id:
            raise _forbidden()
        return row

    def acquire(self, user_id: str, session_id: str, team_id: str, body: LeaseRequest) -> Team:
        def apply(db: Session) -> Team:
            row = self._owned(db, team_id, user_id)
            self._brain(row, session_id)
            now = self._clock()
            if row.lease_expires_at > now:
                if row.controller_id != body.controller_id:
                    raise _conflict("Another BGR controller has a live lease")
            else:
                row.epoch += 1
            row.controller_id = body.controller_id
            row.lease_expires_at = now + body.ttl_seconds
            return _team(row)

        return self._write("acquire_controller", apply)

    def reserve(self, user_id: str, session_id: str, team_id: str, body: TaskReserve) -> Task:
        def apply(db: Session) -> Task:
            team = self._owned(db, team_id, user_id)
            self._fence(team, session_id, body)
            request_hash = _digest(body.model_dump(exclude={"epoch", "controller_id"}))
            existing = db.get(SqlBgrTask, (current_workspace_id(), body.task_id))
            if existing is not None:
                if existing.team_id != team_id or existing.request_hash != request_hash:
                    raise _conflict("task_id already has a different contract or reservation")
                attempt = db.scalar(
                    select(SqlBgrAttempt).where(
                        SqlBgrAttempt.workspace_id == current_workspace_id(),
                        SqlBgrAttempt.task_id == body.task_id,
                    )
                )
                assert attempt is not None
                return self._task(existing, attempt)
            attempts = list(
                db.scalars(
                    select(SqlBgrAttempt).where(
                        SqlBgrAttempt.workspace_id == current_workspace_id(),
                        SqlBgrAttempt.team_id == team_id,
                    )
                )
            )
            config = _team(team).config
            if sum(a.active_writer_key is not None for a in attempts) >= config.max_active_grunts:
                raise _conflict(
                    "BGR active grunt cap reached; existing reservations remain active"
                )
            if len(attempts) >= MAX_TASKS:
                raise _conflict("BGR team task retention limit reached")
            agent_id, attempt_id = uuid4().hex, uuid4().hex
            profile = body.profile or config.grunt
            agent = Agent(
                team_id=team_id,
                agent_id=agent_id,
                session_id=body.session_id,
                role="grunt",
                name=f"grunt-{agent_id}",
                profile=profile,
            )
            attempt = Attempt(
                team_id=team_id,
                task_id=body.task_id,
                attempt_id=attempt_id,
                agent_id=agent_id,
                session_id=body.session_id,
                epoch=team.epoch,
                state="reserved",
                workspace=body.workspace,
                profile=profile,
                config_revision=_team(team).config_revision,
            )
            db.add(self._agent_row(agent))
            db.add(
                SqlBgrTask(
                    id=body.task_id,
                    team_id=team_id,
                    request_hash=request_hash,
                    document=body.contract.model_dump_json(),
                )
            )
            db.add(
                SqlBgrAttempt(
                    id=attempt_id,
                    team_id=team_id,
                    task_id=body.task_id,
                    active_writer_key=_digest([body.workspace.host_id, body.workspace.worktree]),
                    document=attempt.model_dump_json(),
                )
            )
            self._insert_envelope(
                db,
                team,
                self._agent(db, team_id, session_id),
                Send(
                    message_id=body.task_id,
                    recipient_agent_id=agent_id,
                    task_id=body.task_id,
                    attempt_id=attempt_id,
                    epoch=team.epoch,
                    controller_id=body.controller_id,
                    kind="task",
                    payload={"contract": json.loads(body.contract.model_dump_json())},
                ),
            )
            return Task(
                team_id=team_id, task_id=body.task_id, contract=body.contract, attempt=attempt
            )

        return self._write("reserve_task_and_delivery", apply)

    def configure(self, user_id: str, session_id: str, team_id: str, body: ConfigUpdate) -> Team:
        def apply(db: Session) -> Team:
            row = self._owned(db, team_id, user_id)
            self._fence(row, session_id, body)
            team = _team(row)
            if team.config.brain != body.config.brain:
                raise _conflict(
                    "The active brain profile is pinned; create a new team to change it"
                )
            if team.config != body.config:
                team = team.model_copy(
                    update={"config": body.config, "config_revision": team.config_revision + 1}
                )
                row.document = team.model_dump_json()
            return team

        return self._write("configure_future_assignments", apply)

    @staticmethod
    def _task(task: SqlBgrTask, attempt: SqlBgrAttempt) -> Task:
        return Task(
            team_id=task.team_id,
            task_id=task.id,
            contract=Contract.model_validate_json(task.document),
            attempt=Attempt.model_validate_json(attempt.document),
        )

    def recover(self, user_id: str, session_id: str, team_id: str) -> Recovery:
        def read(db: Session) -> Recovery:
            row = self._owned(db, team_id, user_id)
            self._brain(row, session_id)
            tasks = list(
                db.scalars(
                    select(SqlBgrTask).where(
                        SqlBgrTask.workspace_id == current_workspace_id(),
                        SqlBgrTask.team_id == team_id,
                    )
                )
            )
            attempts = {
                a.task_id: a
                for a in db.scalars(
                    select(SqlBgrAttempt).where(
                        SqlBgrAttempt.workspace_id == current_workspace_id(),
                        SqlBgrAttempt.team_id == team_id,
                    )
                )
            }
            agents = list(
                db.scalars(
                    select(SqlBgrAgent).where(
                        SqlBgrAgent.workspace_id == current_workspace_id(),
                        SqlBgrAgent.team_id == team_id,
                    )
                )
            )
            pending = (
                db.scalar(
                    select(func.count())
                    .select_from(SqlBgrEnvelope)
                    .where(
                        SqlBgrEnvelope.workspace_id == current_workspace_id(),
                        SqlBgrEnvelope.team_id == team_id,
                        SqlBgrEnvelope.consumed_at.is_(None),
                        SqlBgrEnvelope.discarded_at.is_(None),
                    )
                )
                or 0
            )
            return Recovery(
                team=_team(row),
                agents=[Agent.model_validate_json(a.document) for a in agents],
                tasks=[self._task(t, attempts[t.id]) for t in tasks],
                pending_envelopes=pending,
            )

        return self._write("recover_team", read)

    def checkpoint(self, user_id: str, session_id: str, team_id: str, body: Checkpoint) -> Attempt:
        def apply(db: Session) -> Attempt:
            team = self._owned(db, team_id, user_id)
            self._epoch(team, body.epoch)
            row = self._attempt(db, team_id, body.attempt_id)
            attempt = Attempt.model_validate_json(row.document)
            if attempt.session_id != session_id:
                raise _forbidden()
            if attempt.state != "reserved" or attempt.epoch != body.epoch:
                raise _conflict("Attempt is terminal or has not been reconciled in this epoch")
            attempt = attempt.model_copy(update={"checkpoint": body})
            row.document = attempt.model_dump_json()
            return attempt

        return self._write("checkpoint_attempt", apply)

    def reattach(
        self, user_id: str, session_id: str, team_id: str, attempt_id: str, body: Fence
    ) -> Attempt:
        def apply(db: Session) -> Attempt:
            team = self._owned(db, team_id, user_id)
            self._fence(team, session_id, body)
            row = self._attempt(db, team_id, attempt_id)
            attempt = Attempt.model_validate_json(row.document)
            if attempt.state != "reserved":
                raise _conflict("Terminal attempts cannot be reattached")
            attempt = attempt.model_copy(update={"epoch": team.epoch})
            row.document = attempt.model_dump_json()
            return attempt

        return self._write("reattach_surviving_attempt", apply)

    def close(self, user_id: str, session_id: str, team_id: str, body: TaskClose) -> Attempt:
        def apply(db: Session) -> Attempt:
            team = self._owned(db, team_id, user_id)
            self._fence(team, session_id, body)
            row = self._attempt(db, team_id, body.attempt_id)
            attempt = Attempt.model_validate_json(row.document)
            if attempt.state != "reserved" and attempt.state != body.outcome:
                raise _conflict("Attempt already has another terminal outcome")
            attempt = attempt.model_copy(update={"state": body.outcome})
            row.document = attempt.model_dump_json()
            row.active_writer_key = None
            return attempt

        return self._write("close_stopped_attempt", apply)

    def _insert_envelope(
        self, db: Session, team: SqlBgrTeam, sender: Agent, body: Send
    ) -> Receipt:
        count = (
            db.scalar(
                select(func.count())
                .select_from(SqlBgrEnvelope)
                .where(
                    SqlBgrEnvelope.workspace_id == current_workspace_id(),
                    SqlBgrEnvelope.team_id == team.id,
                )
            )
            or 0
        )
        pending = (
            db.scalar(
                select(func.count())
                .select_from(SqlBgrEnvelope)
                .where(
                    SqlBgrEnvelope.workspace_id == current_workspace_id(),
                    SqlBgrEnvelope.team_id == team.id,
                    SqlBgrEnvelope.recipient_agent_id == body.recipient_agent_id,
                    SqlBgrEnvelope.consumed_at.is_(None),
                    SqlBgrEnvelope.discarded_at.is_(None),
                )
            )
            or 0
        )
        if count >= MAX_ENVELOPES or pending >= MAX_PENDING:
            raise _conflict("BGR mailbox backpressure or retention limit reached")
        envelope = Envelope(
            **body.model_dump(),
            envelope_id=uuid4().hex,
            team_id=team.id,
            sender_agent_id=sender.agent_id,
            sender_session_id=sender.session_id,
            sender_role=sender.role,
            sender_name=sender.name,
            sequence=team.next_sequence,
            created_at=self._clock(),
        )
        team.next_sequence += 1
        db.add(
            SqlBgrEnvelope(
                id=envelope.envelope_id,
                team_id=team.id,
                sender_agent_id=sender.agent_id,
                recipient_agent_id=body.recipient_agent_id,
                message_id=body.message_id,
                request_hash=_digest(body.model_dump()),
                sequence=envelope.sequence,
                document=envelope.model_dump_json(),
            )
        )
        return Receipt(
            envelope_id=envelope.envelope_id,
            message_id=body.message_id,
            sequence=envelope.sequence,
        )

    def send(self, user_id: str, session_id: str, team_id: str, body: Send) -> Receipt:
        def apply(db: Session) -> Receipt:
            team = self._owned(db, team_id, user_id)
            self._epoch(team, body.epoch)
            sender = self._agent(db, team_id, session_id)
            if sender.role == "brain":
                if body.controller_id != team.controller_id:
                    raise _conflict("Another BGR controller owns this epoch")
            elif body.controller_id is not None:
                raise _forbidden()
            existing = db.scalar(
                select(SqlBgrEnvelope).where(
                    SqlBgrEnvelope.workspace_id == current_workspace_id(),
                    SqlBgrEnvelope.team_id == team_id,
                    SqlBgrEnvelope.sender_agent_id == sender.agent_id,
                    SqlBgrEnvelope.message_id == body.message_id,
                )
            )
            if existing is not None:
                if existing.request_hash != _digest(body.model_dump()):
                    raise _conflict("message_id already has different content")
                return Receipt(
                    envelope_id=existing.id, message_id=body.message_id, sequence=existing.sequence
                )
            recipient = db.get(SqlBgrAgent, (current_workspace_id(), body.recipient_agent_id))
            if (
                recipient is None
                or recipient.team_id != team_id
                or recipient.id == sender.agent_id
            ):
                raise _forbidden()
            target = Agent.model_validate_json(recipient.document)
            if "reviewer" in (sender.role, target.role):
                raise _conflict("Reviewer channels require enforced independent-review lifecycle")
            row = self._attempt(db, team_id, body.attempt_id)
            attempt = Attempt.model_validate_json(row.document)
            if (
                attempt.task_id != body.task_id
                or attempt.epoch != body.epoch
                or attempt.state != "reserved"
            ):
                raise _conflict("Task/attempt is stale, terminal, or not reconciled")
            if {sender.agent_id, target.agent_id} != {
                _team(team).brain_agent_id,
                attempt.agent_id,
            }:
                raise _forbidden()
            if body.kind in ("task", "cancel", "status_query") and sender.role != "brain":
                raise _forbidden()
            if body.in_reply_to is not None:
                original = db.scalar(
                    select(SqlBgrEnvelope).where(
                        SqlBgrEnvelope.workspace_id == current_workspace_id(),
                        SqlBgrEnvelope.team_id == team_id,
                        SqlBgrEnvelope.sender_agent_id == target.agent_id,
                        SqlBgrEnvelope.message_id == body.in_reply_to,
                    )
                )
                if original is None:
                    raise _conflict("Reply does not identify a question from the recipient")
                question = _envelope(original)
                if question.discard is not None:
                    raise _conflict("Discarded questions cannot be replied to")
                expected = "status_query" if body.kind == "status_reply" else "question"
                if (
                    question.recipient_agent_id != sender.agent_id
                    or question.kind != expected
                    or question.correlation_id != body.correlation_id
                    or question.attempt_id != body.attempt_id
                    or question.task_id != body.task_id
                    or question.epoch != body.epoch
                    or question.head_sha != body.head_sha
                ):
                    raise _conflict(
                        "Reply question, sender, correlation, attempt or revision mismatch"
                    )
                if original.replied_at is not None:
                    raise _conflict(
                        "Question already has a reply; retry the original reply message_id"
                    )
                original.replied_at = self._clock()
            return self._insert_envelope(db, team, sender, body)

        return self._write("accept_envelope", apply)

    def drain(
        self, user_id: str, session_id: str, team_id: str, limit: int = 50
    ) -> list[Envelope]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1–100")
        with self._sessions("drain_mailbox") as db:
            self._owned(db, team_id, user_id, lock=False)
            agent = self._agent(db, team_id, session_id)
            rows = db.scalars(
                select(SqlBgrEnvelope)
                .where(
                    SqlBgrEnvelope.workspace_id == current_workspace_id(),
                    SqlBgrEnvelope.team_id == team_id,
                    SqlBgrEnvelope.recipient_agent_id == agent.agent_id,
                    SqlBgrEnvelope.consumed_at.is_(None),
                    SqlBgrEnvelope.discarded_at.is_(None),
                )
                .order_by(SqlBgrEnvelope.sequence)
                .limit(limit)
            )
            return [_envelope(row) for row in rows]

    def ack(self, user_id: str, session_id: str, team_id: str, body: Ack) -> Envelope:
        def apply(db: Session) -> Envelope:
            team = self._owned(db, team_id, user_id)
            self._epoch(team, body.epoch)
            agent = self._agent(db, team_id, session_id)
            row = db.get(SqlBgrEnvelope, (current_workspace_id(), body.envelope_id))
            if row is None or row.team_id != team_id or row.recipient_agent_id != agent.agent_id:
                raise _forbidden()
            envelope = _envelope(row)
            if envelope.discard is not None:
                raise _conflict("Discarded envelopes cannot be acknowledged as consumed")
            if envelope.epoch != body.epoch:
                raise _conflict("Acknowledgement epoch does not match the envelope")
            if row.consumed_at is None:
                row.consumed_at = self._clock()
            return _envelope(row)

        return self._write("acknowledge_consumption", apply)

    def discard(
        self, user_id: str, session_id: str, team_id: str, body: MailboxDiscard
    ) -> list[Envelope]:
        def apply(db: Session) -> list[Envelope]:
            team = self._owned(db, team_id, user_id)
            self._epoch(team, body.epoch)
            agent = self._agent(db, team_id, session_id)
            result = []
            for envelope_id in body.envelope_ids:
                row = db.get(SqlBgrEnvelope, (current_workspace_id(), envelope_id))
                if (
                    row is None
                    or row.team_id != team_id
                    or row.recipient_agent_id != agent.agent_id
                ):
                    raise _forbidden()
                envelope = _envelope(row)
                if envelope.epoch >= body.epoch or envelope.consumed_at is not None:
                    raise _conflict("Only unconsumed envelopes from older epochs can be discarded")
                if envelope.discard is not None:
                    evidence = envelope.discard
                    if (
                        evidence.epoch != body.epoch
                        or evidence.reason != body.reason
                        or evidence.session_id != session_id
                    ):
                        raise _conflict("Envelope already has a different discard action")
                else:
                    evidence = DiscardEvidence(
                        epoch=body.epoch,
                        session_id=session_id,
                        reason=body.reason,
                        discarded_at=self._clock(),
                    )
                    envelope = envelope.model_copy(update={"discard": evidence})
                    row.document = envelope.model_dump_json()
                    row.discarded_at = evidence.discarded_at
                result.append(envelope)
            return result

        return self._write("discard_stale_mailbox", apply)
