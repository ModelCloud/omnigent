"""Transactional BGR reservations and mailbox; no process launch or model execution.

The team row serializes mutations across replicas. Unique database constraints
also protect session and workspace reservations across different teams.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import TypeVar
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, StatementError
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
    Identifier,
    LaunchIntent,
    LaunchRequest,
    LeaseRequest,
    MailboxDiscard,
    NativeAssignment,
    NativeBinding,
    NativeBrainBinding,
    NativeMessageOrigin,
    Receipt,
    Record,
    Recovery,
    RuntimeRecovery,
    RuntimeStopEvidence,
    Send,
    SupervisedLaunch,
    Task,
    TaskClose,
    TaskReserve,
    Team,
    TeamCreate,
)
from omnigent.bgr_supervision import (
    GenerationAdmission,
    GenerationStopReceipt,
    SupervisedGeneration,
    SupervisorStopped,
    SupervisorStopRequest,
    supervision_digest,
)
from omnigent.db.account_authority import require_active_account
from omnigent.db.bgr_models import (
    SqlBgrAgent,
    SqlBgrAttempt,
    SqlBgrEnvelope,
    SqlBgrLaunch,
    SqlBgrNativeBinding,
    SqlBgrTask,
    SqlBgrTeam,
)
from omnigent.db.db_models import current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)
from omnigent.errors import ErrorCode, OmnigentError

_T = TypeVar("_T")
MAX_TASKS = 256
MAX_ENVELOPES = 4096
MAX_PENDING = 256
_MICROSECONDS = 1_000_000


class _RecoveryCursor(Record):
    workspace_id: int
    team_id: Identifier
    tasks_after: Identifier | None = None
    agents_after: Identifier | None = None
    tasks_done: bool = False
    agents_done: bool = False


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
            "lease_expires_at": row.lease_expires_at_us / _MICROSECONDS,
        }
    )


def _envelope(row: SqlBgrEnvelope) -> Envelope:
    return Envelope.model_validate_json(row.document).model_copy(
        update={
            "consumed_at": None
            if row.consumed_at_us is None
            else row.consumed_at_us / _MICROSECONDS,
            "replied_at": None if row.replied_at_us is None else row.replied_at_us / _MICROSECONDS,
        }
    )


class BgrStore:
    def __init__(self, storage_location: str, *, clock: Callable[[], float] = time.time):
        self._sessions = make_named_managed_session_maker(
            get_or_create_engine(storage_location),
            query_name_prefix="omnigent.bgr_store",
            immediate=True,
        )
        self._clock = clock

    def _now_us(self) -> int:
        if self._clock is time.time:
            return now_epoch_us()
        return int(Decimal(str(self._clock())) * _MICROSECONDS)

    def reserve_launch(self, user_id: str, host_id: str, body: LaunchRequest) -> LaunchIntent:
        """Reserve one Brain identity before any session or process side effects."""
        if not user_id or len(user_id) > 320:
            raise OmnigentError("Invalid BGR owner", code=ErrorCode.INVALID_INPUT)
        request_hash = _digest({"host_id": host_id, "request": body.model_dump(mode="json")})

        def apply(db: Session) -> LaunchIntent:
            row = db.get(SqlBgrLaunch, (current_workspace_id(), body.team_id))
            if row is not None:
                if row.owner_id != user_id:
                    raise _forbidden()
                if row.request_hash != request_hash:
                    raise _conflict("BGR launch retry must keep the same configuration and task")
                return LaunchIntent.model_validate_json(row.document)
            intent = LaunchIntent(
                team_id=body.team_id,
                brain_session_id=uuid4().hex,
                controller_id=uuid4().hex,
                host_id=host_id,
                request=body,
            )
            db.add(
                SqlBgrLaunch(
                    workspace_id=current_workspace_id(),
                    id=body.team_id,
                    owner_id=user_id,
                    request_hash=request_hash,
                    document=intent.model_dump_json(),
                )
            )
            return intent

        return self._write("reserve_launch_intent", apply)

    def get_launch(self, user_id: str, team_id: str) -> LaunchIntent:
        def read(db: Session) -> LaunchIntent:
            row = db.get(SqlBgrLaunch, (current_workspace_id(), team_id))
            if row is None or row.owner_id != user_id:
                raise _forbidden()
            return LaunchIntent.model_validate_json(row.document)

        return self._write("read_launch_intent", read)

    def claim_launch(
        self, user_id: str, team_id: str, claim_id: str, *, ttl_seconds: int = 120
    ) -> LaunchIntent | None:
        """Fence concurrent/restarted launch handlers without changing Brain identity."""
        if not 5 <= ttl_seconds <= 300:
            raise OmnigentError("Invalid launch lease duration", code=ErrorCode.INVALID_INPUT)
        claim_id = TypeAdapter(Identifier).validate_python(claim_id, strict=True)

        def apply(db: Session) -> LaunchIntent | None:
            db.execute(
                update(SqlBgrLaunch)
                .where(
                    SqlBgrLaunch.workspace_id == current_workspace_id(),
                    SqlBgrLaunch.id == team_id,
                    SqlBgrLaunch.owner_id == user_id,
                )
                .values(claim_expires_at_us=SqlBgrLaunch.claim_expires_at_us)
            )
            row = db.get(SqlBgrLaunch, (current_workspace_id(), team_id))
            if row is None or row.owner_id != user_id:
                raise _forbidden()
            if LaunchIntent.model_validate_json(row.document).supervised_generations:
                raise _conflict("Supervised generations require a dedicated recovery launch path")
            team = db.get(SqlBgrTeam, (current_workspace_id(), team_id))
            history = _team(team).runtime_recoveries if team is not None else []
            if any(item.supervised for item in history):
                raise _conflict("Supervised generations require a dedicated recovery launch path")
            if history and history[-1].state != "attached":
                raise _conflict("BGR runtime recovery must attach before any launch retry")
            now = self._now_us()
            if row.claim_id != claim_id and row.claim_expires_at_us > now:
                return None
            row.claim_id = claim_id
            row.claim_expires_at_us = now + ttl_seconds * _MICROSECONDS
            return LaunchIntent.model_validate_json(row.document)

        return self._write("claim_launch_intent", apply)

    @staticmethod
    def _supervised_document(intent: LaunchIntent) -> str:
        document = intent.model_dump_json()
        if len(intent.supervised_generations) > 16 or len(document.encode()) > 60_000:
            raise _conflict("Supervised generation history is full; tombstones cannot be evicted")
        return document

    @classmethod
    def _supervised_intent(cls, document: str) -> LaunchIntent:
        if len(document) > 60_000 or len(document.encode()) > 60_000:
            raise _conflict("Supervised generation document exceeds its byte limit")
        intent = LaunchIntent.model_validate_json(document)
        cls._supervised_document(intent)
        return intent

    def generation_for_runner(self, runner_id: str) -> SupervisedGeneration | None:
        """Retain classification after revocation, rebinding, and cold restarts."""
        with self._sessions("classify_supervised_runner") as db:
            return self._generation_for_runner(db, runner_id)

    def recovery_admission(
        self, user_id: str, team_id: str, recovery_id: str
    ) -> GenerationAdmission:
        """Resolve original revoked admission for trusted stop, never activate a generation."""
        with self._sessions("resolve_original_supervised_recovery_admission") as db:
            return self._recovery_admission(db, user_id, team_id, recovery_id)

    def _recovery_admission(
        self, db: Session, user_id: str, team_id: str, recovery_id: str
    ) -> GenerationAdmission:
        row = self._owned(db, team_id, user_id, lock=False)
        team = _team(row)
        if (
            team.team_id != team_id
            or team.owner_id != user_id
            or team.brain_session_id != row.brain_session_id
        ):
            raise _conflict("Recovery team identity changed")
        matches = [item for item in team.runtime_recoveries if item.recovery_id == recovery_id]
        if len(matches) != 1 or matches[0] != team.runtime_recoveries[-1]:
            raise _conflict("Exact current runtime recovery is required")
        recovery = matches[0]
        previous_fence = recovery.state in ("stopping", "stopped")
        expected_epoch = recovery.previous_epoch if previous_fence else recovery.previous_epoch + 1
        expected_controller = (
            recovery.previous_controller_id
            if previous_fence
            else recovery.replacement_controller_id
        )
        if (
            row.epoch != expected_epoch
            or row.controller_id != expected_controller
            or (recovery.state != "stopping" and recovery.stop_manifest_sha256 is None)
        ):
            raise _conflict("Runtime recovery controller fence changed")
        launch = db.get(SqlBgrLaunch, (current_workspace_id(), team_id))
        if launch is None or launch.owner_id != user_id:
            raise _forbidden()
        intent = self._supervised_intent(launch.document)
        if (
            intent.team_id != team_id
            or intent.brain_session_id != team.brain_session_id
            or intent.host_id != recovery.host_id
        ):
            raise _conflict("Recovery launch identity changed")
        candidates = [
            item
            for item in intent.supervised_generations
            if item.generation.team_id == team_id
            and item.generation.brain_session_id == team.brain_session_id
            and item.generation.controller_id == recovery.previous_controller_id
            and item.generation.epoch == recovery.previous_epoch
            and item.generation.runtime_id == recovery.runtime_id
            and item.generation.host_id == recovery.host_id
        ]
        if len(candidates) != 1:
            raise _conflict("Recovery requires one exact original supervised admission")
        item = candidates[0]
        if (
            item.state != "revoked"
            or item.admission is None
            or item.admission.generation != item.generation
        ):
            raise _conflict("Original supervised admission must be retained and revoked")
        require_active_account(db, user_id, generation=item.account_generation)
        return item.admission

    @staticmethod
    def _generation_for_runner(db: Session, runner_id: str) -> SupervisedGeneration | None:
        rows = db.scalars(
            select(SqlBgrLaunch)
            .where(SqlBgrLaunch.workspace_id == current_workspace_id())
            .execution_options(yield_per=32)
        )
        found = None
        for row in rows:
            intent = BgrStore._supervised_intent(row.document)
            for item in intent.supervised_generations:
                if item.generation.runner_id == runner_id:
                    if found is not None:
                        raise _conflict("Supervised runner identity is ambiguous")
                    found = item.generation
        return found

    def reserve_supervised_generation(
        self, user_id: str, generation: SupervisedGeneration
    ) -> SupervisedLaunch:
        """Persist an immutable classification before any supervised launch side effects."""

        def apply(db: Session) -> SupervisedLaunch:
            account_generation = require_active_account(db, user_id)
            team = self._owned(db, generation.team_id, user_id)
            self._fence(
                team,
                generation.brain_session_id,
                Fence(controller_id=generation.controller_id, epoch=generation.epoch),
            )
            row = db.get(SqlBgrLaunch, (current_workspace_id(), generation.team_id))
            if row is None or row.owner_id != user_id:
                raise _forbidden()
            intent = self._supervised_intent(row.document)
            if (
                intent.host_id != generation.host_id
                or intent.brain_session_id != generation.brain_session_id
            ):
                raise _conflict("Supervised launch target does not match its intent")
            self._supervised_document(intent)
            for item in intent.supervised_generations:
                if item.generation == generation:
                    return item
                if (
                    any(
                        getattr(item.generation, field) == getattr(generation, field)
                        for field in ("runner_id", "generation_id", "credential_id", "launch_id")
                    )
                    or item.state != "revoked"
                ):
                    raise _conflict("Supervised identities cannot be reused or overlap")
            if self._generation_for_runner(db, generation.runner_id) is not None:
                raise _conflict("Supervised runner is already classified")
            item = SupervisedLaunch(generation=generation, account_generation=account_generation)
            row.document = self._supervised_document(
                intent.model_copy(
                    update={"supervised_generations": [*intent.supervised_generations, item]}
                )
            )
            return item

        return self._write("reserve_supervised_generation", apply)

    def activate_supervised_generation(
        self, user_id: str, admission: GenerationAdmission
    ) -> SupervisedLaunch:
        """Internal only: the trusted supervisor transport must authenticate admission first."""

        def apply(db: Session) -> SupervisedLaunch:
            generation = admission.generation
            require_active_account(db, user_id)
            team = self._owned(db, generation.team_id, user_id)
            self._fence(
                team,
                generation.brain_session_id,
                Fence(controller_id=generation.controller_id, epoch=generation.epoch),
            )
            row = db.get(SqlBgrLaunch, (current_workspace_id(), generation.team_id))
            if row is None or row.owner_id != user_id:
                raise _forbidden()
            intent = self._supervised_intent(row.document)
            items = list(intent.supervised_generations)
            for index, item in enumerate(items):
                if item.generation != generation:
                    continue
                require_active_account(db, user_id, generation=item.account_generation)
                if item.state == "revoked" or (
                    item.admission is not None and item.admission != admission
                ):
                    raise _conflict("Supervised admission is revoked or changed")
                active = item.model_copy(update={"state": "active", "admission": admission})
                items[index] = active
                row.document = self._supervised_document(
                    intent.model_copy(update={"supervised_generations": items})
                )
                return active
            raise _conflict("Supervised admission has no durable reservation")

        return self._write("activate_supervised_generation", apply)

    def validate_supervised_generation(self, generation: SupervisedGeneration) -> str:
        """Resolve internal ownership only for the exact live, admitted generation."""
        with self._sessions("validate_supervised_generation_authority") as db:
            row = db.get(SqlBgrLaunch, (current_workspace_id(), generation.team_id))
            if row is None:
                raise _forbidden()
            intent = self._supervised_intent(row.document)
            if not any(
                item.generation == generation
                and item.state == "active"
                and item.admission is not None
                for item in intent.supervised_generations
            ):
                raise _forbidden()
            team = self._owned(db, generation.team_id, row.owner_id, lock=False)
            item = next(
                item for item in intent.supervised_generations if item.generation == generation
            )
            require_active_account(db, row.owner_id, generation=item.account_generation)
            self._fence(
                team,
                generation.brain_session_id,
                Fence(controller_id=generation.controller_id, epoch=generation.epoch),
            )
            mapping = db.get(
                SqlBgrNativeBinding, (current_workspace_id(), _team(team).brain_agent_id)
            )
            if mapping is not None:
                binding = NativeBrainBinding.model_validate_json(mapping.document)
                if (
                    binding.team_id != generation.team_id
                    or binding.epoch != generation.epoch
                    or binding.runtime_id != generation.runtime_id
                    or binding.host_id != generation.host_id
                ):
                    raise _conflict(
                        "Supervised generation does not match its native Brain mapping"
                    )
            return row.owner_id

    def finish_launch(
        self,
        user_id: str,
        team_id: str,
        claim_id: str,
        *,
        state: str,
        reason: str | None = None,
    ) -> LaunchIntent:
        def apply(db: Session) -> LaunchIntent:
            db.execute(
                update(SqlBgrLaunch)
                .where(
                    SqlBgrLaunch.workspace_id == current_workspace_id(),
                    SqlBgrLaunch.id == team_id,
                    SqlBgrLaunch.owner_id == user_id,
                )
                .values(claim_expires_at_us=SqlBgrLaunch.claim_expires_at_us)
            )
            row = db.get(SqlBgrLaunch, (current_workspace_id(), team_id))
            if row is None or row.owner_id != user_id:
                raise _forbidden()
            if row.claim_id != claim_id or row.claim_expires_at_us <= self._now_us():
                raise _conflict("BGR launch lease expired; reconcile the existing Brain")
            payload = json.loads(row.document)
            if payload["state"] == "running":
                state_value, reason_value = "running", None
            else:
                state_value, reason_value = state, reason
            payload.update(state=state_value, reason=reason_value)
            intent = LaunchIntent.model_validate(payload)
            row.document = intent.model_dump_json()
            row.claim_id = None
            row.claim_expires_at_us = 0
            return intent

        return self._write("finish_launch_intent", apply)

    def mark_launch_running(
        self, user_id: str, session_id: str, team_id: str, fence: Fence
    ) -> LaunchIntent:
        """Record attachment under the same live controller fence as the Brain binding."""

        def apply(db: Session) -> LaunchIntent:
            team = self._owned(db, team_id, user_id)
            self._fence(team, session_id, fence, allow_recovery=True)
            binding_row = db.get(
                SqlBgrNativeBinding, (current_workspace_id(), _team(team).brain_agent_id)
            )
            if binding_row is None:
                raise _conflict("Brain has no native binding")
            binding = NativeBrainBinding.model_validate_json(binding_row.document)
            if binding.epoch != fence.epoch or binding.team_id != team_id:
                raise _conflict("Brain native binding is stale")
            row = db.get(SqlBgrLaunch, (current_workspace_id(), team_id))
            if row is None or row.owner_id != user_id:
                raise _forbidden()
            intent = LaunchIntent.model_validate_json(row.document)
            if intent.brain_session_id != session_id:
                raise _forbidden()
            intent = intent.model_copy(update={"state": "running", "reason": None})
            row.document = intent.model_dump_json()
            history = _team(team).runtime_recoveries
            if history:
                if history[-1].state not in {"binding", "attached"}:
                    raise _conflict("Replacement Brain has not been bound")
                team.document = (
                    _team(team)
                    .model_copy(
                        update={
                            "runtime_recoveries": [
                                *history[:-1],
                                history[-1].model_copy(update={"state": "attached"}),
                            ]
                        }
                    )
                    .model_dump_json()
                )
            return intent

        return self._write("record_verified_brain_attachment", apply)

    def create(self, user_id: str, session_id: str, body: TeamCreate) -> Team:
        if len(user_id) > 320:
            raise OmnigentError(
                "BGR owner ID must not exceed 320 characters", code=ErrorCode.INVALID_INPUT
            )

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
                    workspace_id=current_workspace_id(),
                    id=team.team_id,
                    owner_id=user_id,
                    brain_session_id=session_id,
                    document=team.model_dump_json(),
                    epoch=0,
                    lease_expires_at_us=0,
                    next_sequence=1,
                )
            )
            db.add(self._agent_row(agent))
            return team

        return self._write("create_team", apply)

    @staticmethod
    def _agent_row(agent: Agent) -> SqlBgrAgent:
        return SqlBgrAgent(
            workspace_id=current_workspace_id(),
            id=agent.agent_id,
            team_id=agent.team_id,
            session_id=agent.session_id,
            document=agent.model_dump_json(),
        )

    def _write(self, name: str, callback: Callable[[Session], _T]) -> _T:
        try:
            try:
                return run_write_transaction(self._sessions, name, callback)
            except IntegrityError:
                try:
                    return run_write_transaction(self._sessions, name, callback)
                except IntegrityError as exc:
                    raise _conflict(
                        "BGR identity, session, or workspace is already reserved; reconcile first"
                    ) from exc
        except StatementError as exc:
            if (
                isinstance(exc.orig, ValueError)
                and str(exc.orig) == "Compressed text exceeds size limit"
            ):
                raise OmnigentError(
                    "BGR document exceeds 16383 stored bytes including compression framing; "
                    "use smaller metadata or artifact references",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
            raise

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

    def _epoch(self, row: SqlBgrTeam, epoch: int, *, allow_recovery: bool = False) -> None:
        if row.epoch != epoch or row.lease_expires_at_us <= self._now_us():
            raise _conflict("BGR coordinator epoch is stale or its lease expired; reconcile first")
        history = _team(row).runtime_recoveries
        if history and history[-1].state != "attached" and not allow_recovery:
            raise _conflict("BGR runtime replacement is not attached")

    def _fence(
        self, row: SqlBgrTeam, session_id: str, body: Fence, *, allow_recovery: bool = False
    ) -> None:
        self._brain(row, session_id)
        self._epoch(row, body.epoch, allow_recovery=allow_recovery)
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
        agent = Agent.model_validate_json(row.document)
        if (
            agent.team_id != team_id
            or agent.agent_id != row.id
            or agent.session_id != row.session_id
        ):
            raise _conflict("BGR agent identity changed")
        return agent

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
            history = _team(row).runtime_recoveries
            if history and history[-1].state != "attached":
                raise _conflict(
                    "Previous native process stops or replacement attach are unconfirmed"
                )
            now = self._now_us()
            if row.lease_expires_at_us > now:
                if row.controller_id != body.controller_id:
                    raise _conflict("Another BGR controller has a live lease")
            else:
                if db.get(
                    SqlBgrNativeBinding, (current_workspace_id(), _team(row).brain_agent_id)
                ):
                    raise _conflict("Native lease expiry requires verified process-loss recovery")
                row.epoch += 1
            row.controller_id = body.controller_id
            row.lease_expires_at_us = now + body.ttl_seconds * _MICROSECONDS
            return _team(row)

        return self._write("acquire_controller", apply)

    def runtime_recovery_target(
        self, user_id: str, team_id: str
    ) -> tuple[str, Team, NativeBrainBinding]:
        """Resolve the durable generation, including the stopped mapping before replacement."""
        with self._sessions("resolve_runtime_recovery_owner") as db:
            row = self._owned(db, team_id, user_id, lock=False)
            team = _team(row)
            agent = db.get(SqlBgrAgent, (current_workspace_id(), team.brain_agent_id))
            mapping = db.get(SqlBgrNativeBinding, (current_workspace_id(), team.brain_agent_id))
            if agent is None or mapping is None:
                raise _conflict("Recovery requires an exact native Brain owner")
            binding = NativeBrainBinding.model_validate_json(mapping.document)
            if binding.runtime_id is None or binding.host_id is None:
                raise _conflict("Recovery requires the recorded native runtime generation")
            recovery = team.runtime_recoveries[-1] if team.runtime_recoveries else None
            if recovery is not None and recovery.state in ("ready", "stopped"):
                stopped = recovery.state == "stopped"
                if (
                    row.epoch != recovery.previous_epoch + (0 if stopped else 1)
                    or row.controller_id
                    != (
                        recovery.previous_controller_id
                        if stopped
                        else recovery.replacement_controller_id
                    )
                    or recovery.stop_manifest_sha256 is None
                    or binding
                    != NativeBrainBinding(
                        team_id=team_id,
                        epoch=recovery.previous_epoch,
                        thread_id=recovery.previous_thread_id,
                        runtime_id=recovery.runtime_id,
                        host_id=recovery.host_id,
                    )
                ):
                    raise _conflict("Recovery requires the exact stopped Brain mapping")
            elif binding.epoch != row.epoch:
                raise _conflict("Recovery requires the recorded native runtime generation")
            return agent.session_id, team, binding

    def live_controller_target(
        self, user_id: str, session_id: str
    ) -> tuple[Team, NativeBrainBinding]:
        """Read an existing owned generation, including after its lease expires."""
        with self._sessions("resolve_same_live_controller") as db:
            row = db.scalar(
                select(SqlBgrTeam).where(
                    SqlBgrTeam.workspace_id == current_workspace_id(),
                    SqlBgrTeam.owner_id == user_id,
                    SqlBgrTeam.brain_session_id == session_id,
                )
            )
            if row is None or row.controller_id is None:
                raise _conflict("No durable native controller is available")
            fence = Fence(controller_id=row.controller_id, epoch=row.epoch)
            binding = self._live_controller_binding(db, row, session_id, fence)
            return _team(row), binding

    def _live_controller_binding(
        self, db: Session, row: SqlBgrTeam, session_id: str, fence: Fence
    ) -> NativeBrainBinding:
        self._brain(row, session_id)
        team = _team(row)
        if (
            row.epoch != fence.epoch
            or row.controller_id != fence.controller_id
            or team.team_id != row.id
            or team.owner_id != row.owner_id
            or team.brain_session_id != session_id
            or (team.runtime_recoveries and team.runtime_recoveries[-1].state != "attached")
            or any(recovery.supervised for recovery in team.runtime_recoveries)
        ):
            raise _conflict("Same-live controller authority changed")
        agent = self._agent(db, row.id, session_id)
        mapping = db.get(SqlBgrNativeBinding, (current_workspace_id(), team.brain_agent_id))
        if (
            agent.agent_id != team.brain_agent_id
            or agent.team_id != row.id
            or agent.session_id != session_id
            or agent.role != "brain"
            or mapping is None
        ):
            raise _conflict("Same-live recovery requires an existing native Brain binding")
        binding = NativeBrainBinding.model_validate_json(mapping.document)
        if (
            binding.team_id != row.id
            or binding.epoch != fence.epoch
            or binding.runtime_id is None
            or binding.host_id is None
            or mapping.team_id != row.id
            or UUID(mapping.thread_id) != UUID(binding.thread_id)
        ):
            raise _conflict("Same-live native Brain identity changed")
        launch = db.get(SqlBgrLaunch, (current_workspace_id(), row.id))
        if launch is not None:
            try:
                intent = self._supervised_intent(launch.document)
            except ValueError:
                raise _conflict("Same-live launch metadata is invalid") from None
            if (
                launch.owner_id != row.owner_id
                or intent.team_id != row.id
                or intent.brain_session_id != session_id
                or intent.host_id != binding.host_id
                or intent.controller_id != fence.controller_id
                or intent.state == "blocked"
                or intent.supervised_generations
            ):
                raise _conflict("Same-live launch authority is unavailable")
        return binding

    def resume_verified_live_controller(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        binding: NativeBrainBinding,
        assignments: list[NativeAssignment],
    ) -> Team:
        """Trusted native inspection may renew only the unchanged controller and inventory."""

        def apply(db: Session) -> Team:
            require_active_account(db, user_id)
            row = self._owned(db, team_id, user_id)
            if self._live_controller_binding(db, row, session_id, fence) != binding:
                raise _conflict("Same-live native generation changed")
            current = self._native_assignments(
                db, row, user_id, session_id, team_id, fence, binding
            )
            if [(item.attempt, item.binding) for item in current] != [
                (item.attempt, item.binding) for item in assignments
            ]:
                raise _conflict("Same-live native inventory changed during inspection")
            row.lease_expires_at_us = max(
                row.lease_expires_at_us, self._now_us() + 300 * _MICROSECONDS
            )
            return _team(row)

        return self._write("renew_verified_same_live_controller", apply)

    def begin_runtime_recovery(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        recovery_id: str,
        runtime_id: str,
        replacement_controller_id: str,
    ) -> RuntimeRecovery:
        """Fence old authority before asking its authenticated host to stop processes."""

        def apply(db: Session) -> RuntimeRecovery:
            row = self._owned(db, team_id, user_id)
            self._brain(row, session_id)
            team = _team(row)
            if team.runtime_recoveries:
                previous = team.runtime_recoveries[-1]
                if previous.recovery_id == recovery_id:
                    if (
                        previous.runtime_id != runtime_id
                        or previous.previous_epoch != fence.epoch
                        or previous.previous_controller_id != fence.controller_id
                        or previous.replacement_controller_id != replacement_controller_id
                    ):
                        raise _conflict("Recovery ID has different arguments")
                    return previous
                if previous.state != "attached":
                    raise _conflict("Existing runtime recovery must be reconciled first")
            if row.epoch != fence.epoch or row.controller_id != fence.controller_id:
                raise _conflict("Recovery source fence changed")
            mapping = db.get(SqlBgrNativeBinding, (current_workspace_id(), team.brain_agent_id))
            if mapping is None:
                raise _conflict("Recovery requires an exact native Brain owner")
            binding = NativeBrainBinding.model_validate_json(mapping.document)
            if (
                binding.runtime_id != runtime_id
                or binding.epoch != fence.epoch
                or binding.host_id is None
            ):
                raise _conflict("Recovery requires the recorded native runtime generation")
            launch = db.get(SqlBgrLaunch, (current_workspace_id(), team_id))
            intent = (
                LaunchIntent.model_validate_json(launch.document) if launch is not None else None
            )
            recovery = RuntimeRecovery(
                recovery_id=recovery_id,
                runtime_id=runtime_id,
                host_id=binding.host_id,
                previous_thread_id=binding.thread_id,
                previous_epoch=fence.epoch,
                previous_controller_id=fence.controller_id,
                replacement_controller_id=replacement_controller_id,
                supervised=intent is not None and bool(intent.supervised_generations),
            )
            row.document = team.model_copy(
                update={"runtime_recoveries": [*team.runtime_recoveries[-31:], recovery]}
            ).model_dump_json()
            row.lease_expires_at_us = 0
            if launch is not None and intent is not None:
                launch.document = intent.model_copy(
                    update={
                        "state": "blocked",
                        "reason": "Native runtime stop is not yet confirmed",
                        "supervised_generations": [
                            item.model_copy(update={"state": "revoked"})
                            for item in intent.supervised_generations
                        ],
                    }
                ).model_dump_json()
            return recovery

        return self._write("fence_lost_runtime_before_host_stop", apply)

    def complete_runtime_recovery(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        *,
        recovery_id: str,
        evidence: RuntimeStopEvidence,
    ) -> Team:
        """Accept only complete exact-generation host stop evidence, never native ACKs."""

        def apply(db: Session) -> Team:
            row = self._owned(db, team_id, user_id)
            self._brain(row, session_id)
            team = _team(row)
            if any(item.supervised for item in team.runtime_recoveries):
                raise _conflict("Supervised generations require trusted supervisor stop receipts")
            launch = db.get(SqlBgrLaunch, (current_workspace_id(), team_id))
            if (
                launch is not None
                and LaunchIntent.model_validate_json(launch.document).supervised_generations
            ):
                raise _conflict("Supervised generations require trusted supervisor stop receipts")
            if not team.runtime_recoveries:
                raise _conflict("No runtime recovery is pending")
            recovery = team.runtime_recoveries[-1]
            digest = _digest(evidence.model_dump(mode="json"))
            if (
                recovery.recovery_id != recovery_id
                or recovery.runtime_id != evidence.runtime_id
                or recovery.host_id != evidence.host_id
            ):
                raise _conflict("Stop evidence belongs to another native runtime")
            if recovery.state != "stopping":
                if recovery.stop_manifest_sha256 != digest:
                    raise _conflict("Recovery already has different stop evidence")
                return team
            stopped = {process.session_id: process for process in evidence.processes}
            if len(stopped) != len(evidence.processes):
                raise _conflict("Stop inventory contains duplicate sessions")
            brain = stopped.get(session_id)
            if (
                brain is None
                or brain.role != "brain"
                or brain.thread_id != recovery.previous_thread_id
            ):
                raise _conflict("Exact previous Brain process stop is unconfirmed")
            agents = {
                agent.id: Agent.model_validate_json(agent.document)
                for agent in db.scalars(
                    select(SqlBgrAgent).where(
                        SqlBgrAgent.workspace_id == current_workspace_id(),
                        SqlBgrAgent.team_id == team_id,
                    )
                )
            }
            for mapping in db.scalars(
                select(SqlBgrNativeBinding).where(
                    SqlBgrNativeBinding.workspace_id == current_workspace_id(),
                    SqlBgrNativeBinding.team_id == team_id,
                )
            ):
                if mapping.id == team.brain_agent_id:
                    continue
                binding = NativeBinding.model_validate_json(mapping.document)
                if binding.epoch != recovery.previous_epoch:
                    continue
                attempt_row = self._attempt(db, team_id, binding.attempt_id)
                attempt = Attempt.model_validate_json(attempt_row.document)
                process = stopped.get(attempt.session_id)
                if (
                    process is None
                    or process.thread_id != binding.thread_id
                    or process.role != agents[attempt.agent_id].role
                ):
                    raise _conflict("An owned native process stop is unconfirmed")
            return self._finish_runtime_recovery(
                db, row, team, recovery, digest=digest, supervised=False
            )

        return self._write("replace_fenced_runtime_after_all_processes_stopped", apply)

    def _finish_runtime_recovery(
        self,
        db: Session,
        row: SqlBgrTeam,
        team: Team,
        recovery: RuntimeRecovery,
        *,
        digest: str,
        supervised: bool,
    ) -> Team:
        from omnigent.bgr_review import ReviewResult, ReviewRound
        from omnigent.db.bgr_models import SqlBgrReviewRound

        team_id, session_id = team.team_id, team.brain_session_id
        for attempt_row in db.scalars(
            select(SqlBgrAttempt).where(
                SqlBgrAttempt.workspace_id == current_workspace_id(),
                SqlBgrAttempt.team_id == team_id,
            )
        ):
            attempt = Attempt.model_validate_json(attempt_row.document)
            if attempt.epoch == recovery.previous_epoch and attempt.state == "reserved":
                attempt_row.document = attempt.model_copy(
                    update={"state": "failed"}
                ).model_dump_json()
                attempt_row.active_writer_key = None
        for review_row in db.scalars(
            select(SqlBgrReviewRound).where(
                SqlBgrReviewRound.workspace_id == current_workspace_id(),
                SqlBgrReviewRound.team_id == team_id,
            )
        ):
            round = ReviewRound.model_validate_json(review_row.document)
            if round.epoch == recovery.previous_epoch:
                jobs = [
                    job
                    if job.state == "sealed"
                    else job.model_copy(
                        update={
                            "state": "sealed",
                            "result": ReviewResult(
                                status="incomplete",
                                summary=(
                                    "Native reviewer process was lost and its host "
                                    "confirmed shutdown. No completed review is available."
                                ),
                            ),
                        }
                    )
                    for job in round.jobs
                ]
                review_row.document = round.model_copy(update={"jobs": jobs}).model_dump_json()
        now = self._now_us()
        for envelope_row in db.scalars(
            select(SqlBgrEnvelope).where(
                SqlBgrEnvelope.workspace_id == current_workspace_id(),
                SqlBgrEnvelope.team_id == team_id,
                SqlBgrEnvelope.consumed_at_us.is_(None),
                SqlBgrEnvelope.discarded_at_us.is_(None),
            )
        ):
            envelope = _envelope(envelope_row)
            if envelope.epoch <= recovery.previous_epoch:
                envelope_row.document = envelope.model_copy(
                    update={
                        "discard": DiscardEvidence(
                            epoch=recovery.previous_epoch + (0 if supervised else 1),
                            session_id=session_id,
                            reason=(
                                "Previous native generation stopped; "
                                "delivery/consumption is unknown, not replayed"
                            ),
                            discarded_at=now / _MICROSECONDS,
                        )
                    }
                ).model_dump_json()
                envelope_row.discarded_at_us = now
        recovery = recovery.model_copy(
            update={
                "state": "stopped" if supervised else "ready",
                "stop_manifest_sha256": digest,
            }
        )
        row.document = team.model_copy(
            update={"runtime_recoveries": [*team.runtime_recoveries[:-1], recovery]}
        ).model_dump_json()
        if supervised:
            row.lease_expires_at_us = 0
        else:
            row.epoch = recovery.previous_epoch + 1
            row.controller_id = recovery.replacement_controller_id
            row.lease_expires_at_us = now + 300 * _MICROSECONDS
        launch = db.get(SqlBgrLaunch, (current_workspace_id(), team_id))
        if launch is not None:
            launch.document = (
                LaunchIntent.model_validate_json(launch.document)
                .model_copy(
                    update={
                        "controller_id": row.controller_id,
                        "state": "blocked" if supervised else "pending",
                        "reason": (
                            "Stopped supervised generation; replacement launch is disabled"
                            if supervised
                            else None
                        ),
                    }
                )
                .model_dump_json()
            )
            launch.claim_id = None
            launch.claim_expires_at_us = 0
        return _team(row)

    def complete_supervised_runtime_recovery(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        *,
        request: SupervisorStopRequest,
        result: SupervisorStopped,
    ) -> Team:
        """Complete trusted host-registry output; never accept a public caller's receipt."""

        def apply(db: Session) -> Team:
            row = self._owned(db, team_id, user_id)
            self._brain(row, session_id)
            try:
                original = SupervisorStopRequest.model_validate_json(request.model_dump_json())
                stopped = SupervisorStopped.model_validate_json(result.model_dump_json())
                receipt = stopped.verify(original)
            except ValueError as exc:
                raise _conflict("Invalid supervised stop result") from exc
            admission = self._recovery_admission(db, user_id, team_id, original.recovery_id)
            if original.admission != admission:
                raise _conflict("Stop result differs from the retained original admission")
            team = _team(row)
            recovery = team.runtime_recoveries[-1]
            receipt_digest = supervision_digest(receipt)
            request_digest = supervision_digest(original)
            if recovery.state != "stopping":
                if (
                    recovery.state != "stopped"
                    or recovery.stop_receipt_id != receipt.receipt_id
                    or recovery.stop_manifest_sha256 != receipt.manifest_sha256
                    or recovery.stop_receipt_sha256 != receipt_digest
                    or recovery.stop_request_sha256 != request_digest
                    or recovery.stop_generation != admission.generation
                ):
                    raise _conflict("Recovery already has different supervised stop evidence")
                self._verify_supervised_claims(db, team, recovery, receipt)
                return team
            self._verify_supervised_claims(db, team, recovery, receipt)
            recovery = recovery.model_copy(
                update={
                    "state": "stopped",
                    "supervised": True,
                    "stop_receipt_id": receipt.receipt_id,
                    "stop_manifest_sha256": receipt.manifest_sha256,
                    "stop_receipt_sha256": receipt_digest,
                    "stop_request_sha256": request_digest,
                    "stop_generation": admission.generation,
                }
            )
            return self._finish_runtime_recovery(
                db, row, team, recovery, digest=receipt.manifest_sha256, supervised=True
            )

        return self._write("complete_trusted_supervised_generation_stop", apply)

    def _verify_supervised_claims(
        self, db: Session, team: Team, recovery: RuntimeRecovery, receipt: GenerationStopReceipt
    ) -> None:
        from omnigent.bgr_review import ReviewRound
        from omnigent.db.bgr_models import SqlBgrReviewRound

        admission = receipt.manifest.admission
        root = next(
            claim for claim in receipt.manifest.claims if claim.claim_id == admission.root_claim_id
        )
        if (
            root.role != "runner"
            or root.attempt_id is not None
            or root.session_id != team.brain_session_id
            or root.disposition != "domain_stopped"
        ):
            raise _conflict("Exact root runner domain stop is unconfirmed")
        brain_row = db.get(SqlBgrNativeBinding, (current_workspace_id(), team.brain_agent_id))
        if brain_row is None or brain_row.team_id != team.team_id:
            raise _conflict("Original Brain binding is missing")
        brain = NativeBrainBinding.model_validate_json(brain_row.document)
        if (
            brain.team_id != team.team_id
            or brain.epoch != recovery.previous_epoch
            or brain.runtime_id != recovery.runtime_id
            or brain.host_id != recovery.host_id
            or brain.thread_id != recovery.previous_thread_id
            or brain_row.thread_id != UUID(brain.thread_id).hex
        ):
            raise _conflict("Original Brain binding changed")
        agents = {
            agent.id: Agent.model_validate_json(agent.document)
            for agent in db.scalars(
                select(SqlBgrAgent).where(
                    SqlBgrAgent.workspace_id == current_workspace_id(),
                    SqlBgrAgent.team_id == team.team_id,
                )
            )
        }
        identities: dict[str, tuple[str, str]] = {}
        required: set[str] = set()
        for attempt_row in db.scalars(
            select(SqlBgrAttempt).where(
                SqlBgrAttempt.workspace_id == current_workspace_id(),
                SqlBgrAttempt.team_id == team.team_id,
            )
        ):
            attempt = Attempt.model_validate_json(attempt_row.document)
            if attempt.epoch != recovery.previous_epoch:
                continue
            agent = agents.get(attempt.agent_id)
            if (
                attempt.attempt_id != attempt_row.id
                or attempt.team_id != team.team_id
                or attempt.task_id != attempt_row.task_id
                or agent is None
                or agent.team_id != team.team_id
                or agent.agent_id != attempt.agent_id
                or agent.session_id != attempt.session_id
                or agent.role not in ("grunt", "reviewer")
                or attempt.workspace.host_id != recovery.host_id
            ):
                raise _conflict("Old attempt identity is inconsistent")
            identities[attempt.attempt_id] = (agent.role, attempt.session_id)
            if attempt.state == "reserved":
                required.add(attempt.attempt_id)
        bound: set[str] = set()
        for mapping in db.scalars(
            select(SqlBgrNativeBinding).where(
                SqlBgrNativeBinding.workspace_id == current_workspace_id(),
                SqlBgrNativeBinding.team_id == team.team_id,
            )
        ):
            if mapping.id == team.brain_agent_id:
                continue
            binding = NativeBinding.model_validate_json(mapping.document)
            if binding.epoch != recovery.previous_epoch:
                if mapping.id in identities or binding.epoch > recovery.previous_epoch:
                    raise _conflict("Native binding epoch differs from its old attempt")
                continue
            if (
                binding.team_id != team.team_id
                or binding.attempt_id != mapping.id
                or binding.attempt_id not in identities
                or UUID(binding.thread_id).hex != mapping.thread_id
            ):
                raise _conflict("Old native binding identity is inconsistent")
            bound.add(binding.attempt_id)
        required.update(bound)
        for review_row in db.scalars(
            select(SqlBgrReviewRound).where(
                SqlBgrReviewRound.workspace_id == current_workspace_id(),
                SqlBgrReviewRound.team_id == team.team_id,
            )
        ):
            review = ReviewRound.model_validate_json(review_row.document)
            if review.epoch != recovery.previous_epoch:
                continue
            if review.team_id != team.team_id or review.round_id != review_row.id:
                raise _conflict("Old review allocation identity is inconsistent")
            for job in review.jobs:
                identity = ("reviewer", job.session_id)
                if job.attempt_id in identities and identities[job.attempt_id] != identity:
                    raise _conflict("Reviewer allocation has conflicting attempt identity")
                identities[job.attempt_id] = identity
        seen: set[str] = set()
        for claim in receipt.manifest.claims:
            if claim == root:
                continue
            identity = identities.get(claim.attempt_id) if claim.attempt_id is not None else None
            if claim.role == "helper":
                if (claim.attempt_id is not None and identity is None) or claim.session_id not in (
                    None,
                    identity[1] if identity else team.brain_session_id,
                ):
                    raise _conflict("Helper claim has foreign worker identity")
                continue
            if (
                identity is None
                or claim.role != identity[0]
                or claim.attempt_id in seen
                or claim.session_id not in (None, identity[1])
            ):
                raise _conflict("Worker claim is foreign, conflicting or ambiguous")
            if claim.attempt_id in bound and (
                claim.session_id != identity[1] or claim.disposition != "domain_stopped"
            ):
                raise _conflict("Bound worker domain stop is unconfirmed")
            assert claim.attempt_id is not None
            seen.add(claim.attempt_id)
        if required - seen:
            raise _conflict(
                "An old native binding or reserved attempt lacks an explicit stop claim"
            )

    def resume_live_runtime_recovery(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        binding: NativeBrainBinding,
    ) -> Team:
        """Trusted attach only, after authenticating and inspecting the exact live binding."""

        def apply(db: Session) -> Team:
            row = self._owned(db, team_id, user_id)
            self._brain(row, session_id)
            team = _team(row)
            history = team.runtime_recoveries
            mapping = db.get(SqlBgrNativeBinding, (current_workspace_id(), team.brain_agent_id))
            if (
                row.epoch != fence.epoch
                or row.controller_id != fence.controller_id
                or not history
                or history[-1].state != "binding"
                or history[-1].replacement_controller_id != fence.controller_id
                or history[-1].previous_epoch + 1 != fence.epoch
                or history[-1].stop_manifest_sha256 is None
                or mapping is None
                or NativeBrainBinding.model_validate_json(mapping.document) != binding
                or binding.team_id != team_id
                or binding.epoch != fence.epoch
                or binding.runtime_id is None
                or binding.runtime_id == history[-1].runtime_id
                or binding.host_id != history[-1].host_id
            ):
                raise _conflict("Only the exact live replacement binding may resume")
            row.lease_expires_at_us = max(
                row.lease_expires_at_us, self._now_us() + 300 * _MICROSECONDS
            )
            return _team(row)

        return self._write("resume_verified_live_replacement_binding", apply)

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
            config = _team(team).config
            active = list(
                db.scalars(
                    select(SqlBgrAttempt.id)
                    .where(
                        SqlBgrAttempt.workspace_id == current_workspace_id(),
                        SqlBgrAttempt.team_id == team_id,
                        SqlBgrAttempt.active_writer_key.is_not(None),
                    )
                    .order_by(SqlBgrAttempt.active_writer_key, SqlBgrAttempt.id)
                    .limit(config.max_active_grunts)
                )
            )
            if len(active) >= config.max_active_grunts:
                raise _conflict(
                    "BGR active grunt cap reached; existing reservations remain active"
                )
            retained = list(
                db.scalars(
                    select(SqlBgrAttempt.id)
                    .where(
                        SqlBgrAttempt.workspace_id == current_workspace_id(),
                        SqlBgrAttempt.team_id == team_id,
                    )
                    .limit(MAX_TASKS)
                )
            )
            if len(retained) >= MAX_TASKS:
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
                    workspace_id=current_workspace_id(),
                    id=body.task_id,
                    team_id=team_id,
                    request_hash=request_hash,
                    document=body.contract.model_dump_json(),
                )
            )
            db.add(
                SqlBgrAttempt(
                    workspace_id=current_workspace_id(),
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
                    correlation_id=body.task_id,
                    head_sha=body.workspace.base_sha,
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

    def get_team(self, user_id: str, session_id: str, team_id: str) -> Team:
        with self._sessions("read_team") as db:
            row = self._owned(db, team_id, user_id, lock=False)
            self._brain(row, session_id)
            return _team(row)

    def check_runtime_input(self, user_id: str, session_id: str, team_id: str) -> Team:
        """Authorize public input only while the Brain's current runtime has a live lease."""
        with self._sessions("authorize_runtime_input") as db:
            row = self._owned(db, team_id, user_id, lock=False)
            self._brain(row, session_id)
            self._epoch(row, row.epoch)
            return _team(row)

    def check_session_runtime_input(self, session_id: str, *, restart: bool = False) -> None:
        """Fence any durable role; callers must authorize session access separately."""
        with self._sessions("authorize_bgr_session_input") as db:
            agent = db.scalar(
                select(SqlBgrAgent).where(
                    SqlBgrAgent.workspace_id == current_workspace_id(),
                    SqlBgrAgent.session_id == session_id,
                )
            )
            if agent is None:
                return
            row = db.get(SqlBgrTeam, (current_workspace_id(), agent.team_id))
            if row is None:
                raise _conflict("BGR membership has no durable team")
            if restart:
                raise _conflict("BGR lifecycle requires explicit verified runtime recovery")
            self._epoch(row, row.epoch)
            role = Agent.model_validate_json(agent.document).role
            if role == "brain":
                mapping = db.get(SqlBgrNativeBinding, (current_workspace_id(), agent.id))
                if mapping is None:
                    raise _conflict("BGR native runtime is not attached")
                binding = NativeBrainBinding.model_validate_json(mapping.document)
                if binding.epoch != row.epoch:
                    raise _conflict("BGR Brain belongs to an old runtime epoch")
            else:
                attempt = next(
                    (
                        parsed
                        for candidate in db.scalars(
                            select(SqlBgrAttempt)
                            .where(
                                SqlBgrAttempt.workspace_id == current_workspace_id(),
                                SqlBgrAttempt.team_id == agent.team_id,
                            )
                            .limit(MAX_TASKS)
                        )
                        if (parsed := Attempt.model_validate_json(candidate.document)).agent_id
                        == agent.id
                    ),
                    None,
                )
                if attempt is None or attempt.epoch != row.epoch or attempt.state != "reserved":
                    raise _conflict("BGR worker belongs to an inactive attempt or runtime epoch")
                mapping = db.get(SqlBgrNativeBinding, (current_workspace_id(), attempt.attempt_id))
                if mapping is None:
                    raise _conflict("BGR native runtime is not attached")
                if NativeBinding.model_validate_json(mapping.document).epoch != row.epoch:
                    raise _conflict("BGR worker belongs to an old native runtime epoch")

    def recover(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Recovery:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1–100")

        def read(db: Session) -> Recovery:
            row = self._owned(db, team_id, user_id, lock=False)
            self._brain(row, session_id)
            position = _RecoveryCursor(workspace_id=current_workspace_id(), team_id=team_id)
            if cursor is not None:
                try:
                    if len(cursor) > 1024:
                        raise ValueError("oversized cursor")
                    position = _RecoveryCursor.model_validate_json(
                        base64.b64decode(cursor, altchars=b"-_", validate=True)
                    )
                    if (
                        position.workspace_id != current_workspace_id()
                        or position.team_id != team_id
                    ):
                        raise ValueError("foreign cursor")
                except (ValueError, ValidationError) as exc:
                    raise OmnigentError(
                        "Invalid BGR recovery cursor", code=ErrorCode.INVALID_INPUT
                    ) from exc
            task_query = select(SqlBgrTask).where(
                SqlBgrTask.workspace_id == current_workspace_id(),
                SqlBgrTask.team_id == team_id,
            )
            if position.tasks_after is not None:
                task_query = task_query.where(SqlBgrTask.id > position.tasks_after)
            task_rows = (
                []
                if position.tasks_done
                else list(db.scalars(task_query.order_by(SqlBgrTask.id).limit(limit + 1)))
            )
            tasks = task_rows[:limit]
            attempts = (
                {
                    a.task_id: a
                    for a in db.scalars(
                        select(SqlBgrAttempt).where(
                            SqlBgrAttempt.workspace_id == current_workspace_id(),
                            SqlBgrAttempt.task_id.in_([t.id for t in tasks]),
                        )
                    )
                }
                if tasks
                else {}
            )
            agent_query = select(SqlBgrAgent).where(
                SqlBgrAgent.workspace_id == current_workspace_id(),
                SqlBgrAgent.team_id == team_id,
            )
            if position.agents_after is not None:
                agent_query = agent_query.where(SqlBgrAgent.id > position.agents_after)
            agent_rows = (
                []
                if position.agents_done
                else list(db.scalars(agent_query.order_by(SqlBgrAgent.id).limit(limit + 1)))
            )
            agents = agent_rows[:limit]
            position = position.model_copy(
                update={
                    "tasks_after": tasks[-1].id if tasks else position.tasks_after,
                    "agents_after": agents[-1].id if agents else position.agents_after,
                    "tasks_done": position.tasks_done or len(task_rows) <= limit,
                    "agents_done": position.agents_done or len(agent_rows) <= limit,
                }
            )
            pending = list(
                db.scalars(
                    select(SqlBgrEnvelope.id)
                    .where(
                        SqlBgrEnvelope.workspace_id == current_workspace_id(),
                        SqlBgrEnvelope.team_id == team_id,
                        SqlBgrEnvelope.consumed_at_us.is_(None),
                        SqlBgrEnvelope.discarded_at_us.is_(None),
                    )
                    .order_by(SqlBgrEnvelope.sequence, SqlBgrEnvelope.id)
                    .limit(limit + 1)
                )
            )
            return Recovery(
                team=_team(row),
                agents=[Agent.model_validate_json(a.document) for a in agents],
                tasks=[self._task(t, attempts[t.id]) for t in tasks],
                pending_envelopes=min(len(pending), limit),
                pending_envelopes_has_more=len(pending) > limit,
                next_cursor=None
                if position.tasks_done and position.agents_done
                else base64.urlsafe_b64encode(position.model_dump_json().encode()).decode(),
            )

        with self._sessions("recover_team") as db:
            return read(db)

    def native_assignments(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        live_binding: NativeBrainBinding | None = None,
    ) -> list[NativeAssignment]:
        with self._sessions("read_current_native_role_bindings") as db:
            team = self._owned(db, team_id, user_id, lock=live_binding is None)
            return self._native_assignments(
                db, team, user_id, session_id, team_id, fence, live_binding
            )

    def _native_assignments(
        self,
        db: Session,
        team: SqlBgrTeam,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        live_binding: NativeBrainBinding | None,
    ) -> list[NativeAssignment]:
        if live_binding is None:
            self._fence(team, session_id, fence)
        elif self._live_controller_binding(db, team, session_id, fence) != live_binding:
            raise _conflict("Same-live native generation changed")
        bindings = list(
            db.execute(
                select(SqlBgrNativeBinding, SqlBgrAttempt)
                .outerjoin(
                    SqlBgrAttempt,
                    (SqlBgrAttempt.workspace_id == SqlBgrNativeBinding.workspace_id)
                    & (SqlBgrAttempt.id == SqlBgrNativeBinding.id),
                )
                .where(
                    SqlBgrNativeBinding.workspace_id == current_workspace_id(),
                    (SqlBgrNativeBinding.team_id == team_id) | (SqlBgrAttempt.team_id == team_id),
                    SqlBgrNativeBinding.id != _team(team).brain_agent_id,
                )
                .order_by(SqlBgrNativeBinding.id)
                .limit(MAX_TASKS + 1)
            )
        )
        if len(bindings) > MAX_TASKS:
            raise _conflict("Native assignment retention limit exceeded")
        current = []
        for binding_row, row in bindings:
            binding = NativeBinding.model_validate_json(binding_row.document)
            if (
                row is None
                or binding_row.team_id != team_id
                or binding.team_id != team_id
                or binding.attempt_id != binding_row.id
                or UUID(binding.thread_id) != UUID(binding_row.thread_id)
            ):
                raise _conflict("Native binding has no exact durable assignment")
            attempt = Attempt.model_validate_json(row.document)
            if (
                attempt.attempt_id != row.id
                or attempt.team_id != team_id
                or row.team_id != team_id
                or attempt.task_id != row.task_id
            ):
                raise _conflict("Native assignment identity changed")
            if attempt.state == "reserved" and attempt.epoch == fence.epoch:
                current.append(
                    self._native_assignment(
                        db,
                        user_id,
                        session_id,
                        team_id,
                        attempt.attempt_id,
                        fence,
                        live_binding=live_binding,
                    )
                )
        return current

    def _native_assignment(
        self,
        db: Session,
        user_id: str,
        session_id: str,
        team_id: str,
        attempt_id: str,
        fence: Fence,
        binding: NativeBinding | None = None,
        *,
        live_binding: NativeBrainBinding | None = None,
    ) -> NativeAssignment:
        team = self._owned(db, team_id, user_id, lock=live_binding is None)
        if live_binding is None:
            self._fence(team, session_id, fence)
        elif self._live_controller_binding(db, team, session_id, fence) != live_binding:
            raise _conflict("Same-live native generation changed")
        row = self._attempt(db, team_id, attempt_id)
        attempt = Attempt.model_validate_json(row.document)
        if attempt.state != "reserved" or attempt.epoch != fence.epoch:
            raise _conflict("Native attempt is terminal or stale")
        if attempt.profile.harness != "codex-native":
            raise _conflict("Native BGR binding requires an explicit codex-native profile")
        existing = db.get(SqlBgrNativeBinding, (current_workspace_id(), attempt_id))
        if binding is not None:
            if (
                binding.team_id != team_id
                or binding.attempt_id != attempt_id
                or binding.epoch != fence.epoch
            ):
                raise _conflict("Native binding does not match the assignment")
            if existing is None:
                db.add(
                    SqlBgrNativeBinding(
                        workspace_id=current_workspace_id(),
                        id=attempt_id,
                        team_id=team_id,
                        thread_id=binding.thread_id,
                        document=binding.model_dump_json(),
                    )
                )
            elif NativeBinding.model_validate_json(existing.document) != binding:
                raise _conflict("Attempt already has another immutable native binding")
        elif existing is None:
            raise _conflict("Attempt has no reserved native binding")
        else:
            binding = NativeBinding.model_validate_json(existing.document)
        if binding.epoch != fence.epoch:
            raise _conflict("Native binding belongs to an older epoch; replacement is unsupported")
        if existing is not None and (
            existing.team_id != team_id or UUID(existing.thread_id) != UUID(binding.thread_id)
        ):
            raise _conflict("Native binding columns do not match its immutable identity")
        agent = self._agent(db, team_id, attempt.session_id)
        if (
            attempt.team_id != team_id
            or attempt.attempt_id != attempt_id
            or binding.team_id != team_id
            or binding.attempt_id != attempt_id
            or agent.team_id != team_id
            or agent.agent_id != attempt.agent_id
            or agent.session_id != attempt.session_id
            or agent.profile != attempt.profile
            or agent.role != ("reviewer" if attempt.review_round_id is not None else "grunt")
        ):
            raise _conflict("Native role does not match its durable assignment")
        return NativeAssignment(team=_team(team), attempt=attempt, binding=binding)

    def bind_native(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        binding: NativeBinding,
    ) -> NativeAssignment:
        """Reserve an immutable mapping before RPC; this is not proof of native binding."""
        return self._write(
            "reserve_native_worker_mapping",
            lambda db: self._native_assignment(
                db,
                user_id,
                session_id,
                team_id,
                binding.attempt_id,
                fence,
                binding,
            ),
        )

    def native_assignment(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        attempt_id: str,
        fence: Fence,
    ) -> NativeAssignment:
        return self._write(
            "read_live_native_assignment",
            lambda db: self._native_assignment(
                db,
                user_id,
                session_id,
                team_id,
                attempt_id,
                fence,
            ),
        )

    def native_query(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        attempt_id: str,
        fence: Fence,
        message_id: str,
    ) -> Envelope:
        def read(db: Session) -> Envelope:
            assignment = self._native_assignment(
                db,
                user_id,
                session_id,
                team_id,
                attempt_id,
                fence,
            )
            row = db.scalar(
                select(SqlBgrEnvelope).where(
                    SqlBgrEnvelope.workspace_id == current_workspace_id(),
                    SqlBgrEnvelope.team_id == team_id,
                    SqlBgrEnvelope.sender_agent_id == assignment.team.brain_agent_id,
                    SqlBgrEnvelope.message_id == message_id,
                )
            )
            if row is None:
                raise _forbidden()
            query = _envelope(row)
            if (
                query.kind != "status_query"
                or query.epoch != fence.epoch
                or query.attempt_id != attempt_id
                or query.task_id != assignment.attempt.task_id
                or query.recipient_agent_id != assignment.attempt.agent_id
                or query.sender_session_id != session_id
                or query.sender_role != "brain"
                or query.discard is not None
            ):
                raise _conflict("Native query is not a live authenticated brain status query")
            return query

        return self._write("read_persisted_native_query", read)

    def native_brain_binding(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        binding: NativeBrainBinding | None = None,
    ) -> NativeBrainBinding:
        """Reserve or read the Brain mapping without inventing a worker assignment."""
        return self._native_brain_binding(user_id, session_id, team_id, fence, binding=binding)[0]

    def reserve_native_brain_binding(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        binding: NativeBrainBinding,
        existing_only: bool = False,
    ) -> bool:
        """Return whether this transaction reserved a new native identity to bind."""
        return self._native_brain_binding(
            user_id,
            session_id,
            team_id,
            fence,
            binding=binding,
            inspect_existing=True,
            existing_only=existing_only,
        )[1]

    def _native_brain_binding(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        binding: NativeBrainBinding | None,
        inspect_existing: bool = False,
        existing_only: bool = False,
    ) -> tuple[NativeBrainBinding, bool]:
        def apply(db: Session) -> tuple[NativeBrainBinding, bool]:
            team = self._owned(db, team_id, user_id)
            state = _team(team)
            if inspect_existing and (
                not state.runtime_recoveries or state.runtime_recoveries[-1].state == "attached"
            ):
                existing = db.get(
                    SqlBgrNativeBinding, (current_workspace_id(), state.brain_agent_id)
                )
                if existing is not None:
                    if self._live_controller_binding(db, team, session_id, fence) != binding:
                        raise _conflict("Native Brain already has another immutable binding")
                    assert binding is not None
                    return binding, False
            if existing_only:
                raise _conflict("Same-live attach requires an existing native Brain binding")
            self._fence(team, session_id, fence, allow_recovery=binding is not None)
            if _team(team).config.brain.harness != "codex-native":
                raise _conflict("Native Brain requires an explicit codex-native profile")
            brain_agent_id = _team(team).brain_agent_id
            key = (current_workspace_id(), brain_agent_id)
            existing = db.get(SqlBgrNativeBinding, key)
            reserved = False
            if binding is not None:
                if binding.team_id != team_id or binding.epoch != fence.epoch:
                    raise _conflict("Native Brain binding does not match the team epoch")
                if existing is None:
                    reserved = True
                    db.add(
                        SqlBgrNativeBinding(
                            workspace_id=current_workspace_id(),
                            id=brain_agent_id,
                            team_id=team_id,
                            thread_id=binding.thread_id,
                            document=binding.model_dump_json(),
                        )
                    )
                elif NativeBrainBinding.model_validate_json(existing.document) != binding:
                    history = _team(team).runtime_recoveries
                    previous = NativeBrainBinding.model_validate_json(existing.document)
                    if (
                        not history
                        or history[-1].state != "ready"
                        or history[-1].previous_epoch != previous.epoch
                        or history[-1].previous_thread_id != previous.thread_id
                        or history[-1].runtime_id != previous.runtime_id
                        or binding.epoch != previous.epoch + 1
                        or binding.runtime_id is None
                        or binding.runtime_id == previous.runtime_id
                        or binding.thread_id == previous.thread_id
                        or binding.host_id != previous.host_id
                    ):
                        raise _conflict("Brain already has another immutable native binding")
                    existing.thread_id = binding.thread_id
                    existing.document = binding.model_dump_json()
                    reserved = True
                    team.document = (
                        _team(team)
                        .model_copy(
                            update={
                                "runtime_recoveries": [
                                    *history[:-1],
                                    history[-1].model_copy(update={"state": "binding"}),
                                ]
                            }
                        )
                        .model_dump_json()
                    )
            elif existing is None:
                raise _conflict("Brain has no native binding")
            else:
                binding_value = NativeBrainBinding.model_validate_json(existing.document)
                if binding_value.epoch != fence.epoch:
                    raise _conflict("Brain native binding requires a proven epoch transition")
                return binding_value, False
            return binding, reserved

        return self._write("reserve_or_read_native_brain_mapping", apply)

    def native_message(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        sender_agent_id: str,
        message_id: str,
        live_binding: NativeBrainBinding | None = None,
    ) -> Envelope:
        """Read an exact durable source anchor under the live Brain controller fence."""

        def read(db: Session) -> Envelope:
            return self._native_message(
                db, user_id, session_id, team_id, fence, sender_agent_id, message_id, live_binding
            )

        return self._write("read_exact_native_source_anchor", read)

    def _native_message(
        self,
        db: Session,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        sender_agent_id: str,
        message_id: str,
        live_binding: NativeBrainBinding | None = None,
    ) -> Envelope:
        team = self._owned(db, team_id, user_id, lock=live_binding is None)
        if live_binding is None:
            self._fence(team, session_id, fence)
        elif self._live_controller_binding(db, team, session_id, fence) != live_binding:
            raise _conflict("Same-live native generation changed")
        row = db.scalar(
            select(SqlBgrEnvelope).where(
                SqlBgrEnvelope.workspace_id == current_workspace_id(),
                SqlBgrEnvelope.team_id == team_id,
                SqlBgrEnvelope.sender_agent_id == sender_agent_id,
                SqlBgrEnvelope.message_id == message_id,
            )
        )
        if row is None:
            raise _conflict("Native message has no durable source anchor")
        envelope = _envelope(row)
        if (
            envelope.team_id != team_id
            or envelope.sender_agent_id != sender_agent_id
            or envelope.message_id != message_id
            or envelope.recipient_agent_id != row.recipient_agent_id
            or envelope.epoch != fence.epoch
            or envelope.discard is not None
        ):
            raise _conflict("Native source anchor is stale or discarded")
        return envelope

    def native_message_recipient(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        fence: Fence,
        *,
        sender_agent_id: str,
        message_id: str,
    ) -> Agent:
        """Read historical recipient identity, never authority to revive its attempt."""

        def read(db: Session) -> Agent:
            envelope = self._native_message(
                db, user_id, session_id, team_id, fence, sender_agent_id, message_id
            )
            row = db.get(SqlBgrAgent, (current_workspace_id(), envelope.recipient_agent_id))
            if row is None:
                raise _conflict("Native anchor recipient is missing")
            agent = Agent.model_validate_json(row.document)
            if (
                row.team_id != team_id
                or agent.team_id != team_id
                or agent.agent_id != row.id
                or agent.session_id != row.session_id
            ):
                raise _conflict("Native anchor recipient identity changed")
            return agent

        return self._write("read_historical_native_anchor_recipient", read)

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
        self,
        db: Session,
        team: SqlBgrTeam,
        sender: Agent,
        body: Send,
        *,
        request_hash: str | None = None,
        native_origin: NativeMessageOrigin | None = None,
    ) -> Receipt:
        retained = list(
            db.scalars(
                select(SqlBgrEnvelope.id)
                .where(
                    SqlBgrEnvelope.workspace_id == current_workspace_id(),
                    SqlBgrEnvelope.team_id == team.id,
                )
                .limit(MAX_ENVELOPES)
            )
        )
        pending = list(
            db.scalars(
                select(SqlBgrEnvelope.id)
                .where(
                    SqlBgrEnvelope.workspace_id == current_workspace_id(),
                    SqlBgrEnvelope.team_id == team.id,
                    SqlBgrEnvelope.recipient_agent_id == body.recipient_agent_id,
                    SqlBgrEnvelope.consumed_at_us.is_(None),
                    SqlBgrEnvelope.discarded_at_us.is_(None),
                )
                .limit(MAX_PENDING)
            )
        )
        if len(retained) >= MAX_ENVELOPES or len(pending) >= MAX_PENDING:
            raise _conflict("BGR mailbox backpressure or retention limit reached")
        envelope = Envelope(
            **body.model_dump(),
            envelope_id=uuid4().hex,
            team_id=team.id,
            sender_agent_id=sender.agent_id,
            sender_session_id=sender.session_id,
            sender_role=sender.role,
            sender_name=sender.name,
            native_origin=native_origin,
            sequence=team.next_sequence,
            created_at=self._now_us() / _MICROSECONDS,
        )
        team.next_sequence += 1
        db.add(
            SqlBgrEnvelope(
                workspace_id=current_workspace_id(),
                id=envelope.envelope_id,
                team_id=team.id,
                sender_agent_id=sender.agent_id,
                recipient_agent_id=body.recipient_agent_id,
                message_id=body.message_id,
                request_hash=request_hash or _digest(body.model_dump()),
                sequence=envelope.sequence,
                document=envelope.model_dump_json(),
            )
        )
        return Receipt(
            envelope_id=envelope.envelope_id,
            message_id=body.message_id,
            sequence=envelope.sequence,
        )

    def send(
        self,
        user_id: str,
        session_id: str,
        team_id: str,
        body: Send,
        *,
        native_origin: NativeMessageOrigin | None = None,
    ) -> Receipt:
        def apply(db: Session) -> Receipt:
            delivered = body
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
                identity = body.model_dump()
                if native_origin is not None:
                    identity["native_origin"] = native_origin.model_dump()
                if existing.request_hash != _digest(identity):
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
            reviewing = "reviewer" in (sender.role, target.role)
            if reviewing:
                # Legacy/imported identities have no verified isolation lifecycle.
                candidate = self._attempt(db, team_id, body.attempt_id)
                if Attempt.model_validate_json(candidate.document).review_round_id is None:
                    raise _conflict(
                        "Reviewer channels require enforced independent-review lifecycle"
                    )
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
            if reviewing:
                from omnigent.stores.bgr_review_store import validate_review_message

                delivered = validate_review_message(db, team_id, sender, target, attempt, body)
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
                expected = {
                    "status_reply": "status_query",
                    "answer": "question",
                }.get(body.kind, "task")
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
                if body.kind in ("answer", "status_reply") and original.replied_at_us is not None:
                    raise _conflict(
                        "Question already has a reply; retry the original reply message_id"
                    )
                if body.kind in ("answer", "status_reply"):
                    original.replied_at_us = self._now_us()
            identity = body.model_dump()
            if native_origin is not None:
                identity["native_origin"] = native_origin.model_dump()
            return self._insert_envelope(
                db,
                team,
                sender,
                delivered,
                request_hash=_digest(identity),
                native_origin=native_origin,
            )

        return self._write("accept_envelope", apply)

    def drain(
        self, user_id: str, session_id: str, team_id: str, limit: int = 50
    ) -> list[Envelope]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1–100")
        with self._sessions("drain_mailbox") as db:
            self._owned(db, team_id, user_id, lock=False)
            agent = self._agent(db, team_id, session_id)
            from omnigent.stores.bgr_review_store import sealed_review_agents

            ready = sealed_review_agents(db, team_id) if agent.role == "brain" else set()
            rows = db.scalars(
                select(SqlBgrEnvelope)
                .where(
                    SqlBgrEnvelope.workspace_id == current_workspace_id(),
                    SqlBgrEnvelope.team_id == team_id,
                    SqlBgrEnvelope.recipient_agent_id == agent.agent_id,
                    SqlBgrEnvelope.consumed_at_us.is_(None),
                    SqlBgrEnvelope.discarded_at_us.is_(None),
                )
                .order_by(SqlBgrEnvelope.sequence, SqlBgrEnvelope.id)
                .limit(MAX_PENDING)
            )
            result = []
            for row in rows:
                envelope = _envelope(row)
                if (
                    agent.role == "brain"
                    and envelope.sender_role == "reviewer"
                    and envelope.kind == "result"
                    and envelope.sender_agent_id not in ready
                ):
                    continue
                result.append(envelope)
                if len(result) == limit:
                    break
            return result

    def ack(self, user_id: str, session_id: str, team_id: str, body: Ack) -> Envelope:
        def apply(db: Session) -> Envelope:
            team = self._owned(db, team_id, user_id)
            self._epoch(team, body.epoch)
            agent = self._agent(db, team_id, session_id)
            row = db.get(SqlBgrEnvelope, (current_workspace_id(), body.envelope_id))
            if row is None or row.team_id != team_id or row.recipient_agent_id != agent.agent_id:
                raise _forbidden()
            envelope = _envelope(row)
            if (
                agent.role == "brain"
                and envelope.sender_role == "reviewer"
                and envelope.kind == "result"
            ):
                from omnigent.stores.bgr_review_store import sealed_review_agents

                if envelope.sender_agent_id not in sealed_review_agents(db, team_id):
                    raise _conflict(
                        "Independent review results are withheld until all siblings seal"
                    )
            if envelope.discard is not None:
                raise _conflict("Discarded envelopes cannot be acknowledged as consumed")
            if envelope.epoch != body.epoch:
                raise _conflict("Acknowledgement epoch does not match the envelope")
            if row.consumed_at_us is None:
                row.consumed_at_us = self._now_us()
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
                    discarded_at_us = self._now_us()
                    evidence = DiscardEvidence(
                        epoch=body.epoch,
                        session_id=session_id,
                        reason=body.reason,
                        discarded_at=discarded_at_us / _MICROSECONDS,
                    )
                    envelope = envelope.model_copy(update={"discard": evidence})
                    row.document = envelope.model_dump_json()
                    row.discarded_at_us = discarded_at_us
                result.append(envelope)
            return result

        return self._write("discard_stale_mailbox", apply)
