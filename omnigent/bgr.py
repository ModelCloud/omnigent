"""Version-one BGR controller records. These records do not grant runtime authority."""

from __future__ import annotations

import json
import posixpath
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from omnigent.bgr_records import Identifier as Identifier
from omnigent.bgr_records import Label as Label
from omnigent.bgr_records import Record as Record
from omnigent.bgr_records import Role as Role
from omnigent.bgr_supervision import GenerationAdmission, SupervisedGeneration

Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Kind = Literal[
    "task", "question", "answer", "progress", "status_query", "status_reply", "result", "cancel"
]


class Profile(Record):
    provider: Label
    model: Label
    variant: Label | None = None
    speed: Label | None = None
    reasoning: Label | None = None
    harness: Label | None = None


class TeamConfig(Record):
    brain: Profile
    grunt: Profile
    reviewers: Annotated[list[Profile], Field(min_length=1, max_length=2)]
    max_active_grunts: Annotated[int, Field(ge=1, le=64)] = 4


class TeamCreate(Record):
    protocol_version: Literal[1] = 1
    team_id: Identifier
    config: TeamConfig


class LaunchRequest(Record):
    team_id: Identifier
    agent_id: Label
    workspace: Annotated[str, Field(min_length=1, max_length=1024)]
    config: TeamConfig
    initial_prompt: Annotated[str, Field(min_length=1, max_length=8000)]
    project_id: Identifier | None = None

    @field_validator("workspace")
    @classmethod
    def canonical_workspace(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value or posixpath.normpath(value) != value:
            raise ValueError("workspace must be a normalized absolute host path")
        return value

    @field_validator("initial_prompt")
    @classmethod
    def nonblank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("initial_prompt must not be blank")
        return value


class SupervisedLaunch(Record):
    generation: SupervisedGeneration
    account_generation: Identifier | None = None
    state: Literal["reserved", "active", "revoked"] = "reserved"
    admission: GenerationAdmission | None = None

    @model_validator(mode="after")
    def exact_admission(self) -> Self:
        if self.admission is not None and self.admission.generation != self.generation:
            raise ValueError("Admission must match the reserved generation")
        if self.state == "active" and self.admission is None:
            raise ValueError("Active generations require trusted admission")
        return self


class LaunchIntent(Record):
    team_id: Identifier
    brain_session_id: Identifier
    controller_id: Identifier
    host_id: Annotated[str, Field(min_length=1, max_length=128)]
    request: LaunchRequest
    state: Literal["pending", "starting", "running", "blocked"] = "pending"
    reason: Annotated[str, Field(max_length=2000)] | None = None
    supervised_generations: Annotated[list[SupervisedLaunch], Field(max_length=16)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def unique_generation_history(self) -> Self:
        for field in ("runner_id", "generation_id", "credential_id", "launch_id"):
            values = [getattr(item.generation, field) for item in self.supervised_generations]
            if len(values) != len(set(values)):
                raise ValueError("Supervised identities cannot be reused")
        if any(
            item.generation.team_id != self.team_id
            or item.generation.brain_session_id != self.brain_session_id
            for item in self.supervised_generations
        ):
            raise ValueError("Supervised history belongs to another team or Brain")
        return self


class LeaseRequest(Record):
    controller_id: Identifier
    ttl_seconds: Annotated[int, Field(ge=5, le=300)] = 60


class Fence(Record):
    controller_id: Identifier
    epoch: Annotated[int, Field(ge=1)]


class ConfigUpdate(Fence):
    config: TeamConfig


class Workspace(Record):
    host_id: Annotated[str, Field(min_length=1, max_length=128)]
    repository: Label
    worktree: Annotated[str, Field(min_length=1, max_length=1024)]
    base_sha: Commit

    @field_validator("worktree")
    @classmethod
    def canonical_worktree(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value or posixpath.normpath(value) != value:
            raise ValueError("worktree must be a normalized absolute host path")
        return value


class Contract(Record):
    objective: Annotated[str, Field(min_length=1, max_length=2000)]
    instructions: Annotated[str, Field(min_length=1, max_length=4000)]
    acceptance: Annotated[list[Label], Field(min_length=1, max_length=16)]
    checks: Annotated[list[Label], Field(max_length=16)] = Field(default_factory=list)
    artifact_refs: Annotated[list[Label], Field(max_length=16)] = Field(default_factory=list)


class TaskReserve(Fence):
    task_id: Identifier
    session_id: Identifier
    contract: Contract
    workspace: Workspace
    profile: Profile | None = None

    @model_validator(mode="after")
    def validate_delivery_size(self) -> Self:
        payload = {"contract": self.contract.model_dump(mode="json")}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 8000:
            raise ValueError("contract delivery exceeds 8000 bytes")
        return self


class Checkpoint(Record):
    attempt_id: Identifier
    epoch: Annotated[int, Field(ge=1)]
    phase: Label
    summary: Annotated[str, Field(max_length=2000)]
    head_sha: Commit | None = None
    artifact_refs: Annotated[list[Label], Field(max_length=16)] = Field(default_factory=list)


class StatusScope(Record):
    scope: Literal["now", "since_time", "since_commit"]
    since_time: datetime | None = None
    since_commit: Commit | None = None

    @model_validator(mode="after")
    def validate_anchor(self) -> Self:
        if self.scope == "since_time":
            if self.since_time is None or self.since_time.utcoffset() != UTC.utcoffset(None):
                raise ValueError("since_time requires a UTC timestamp")
        elif self.since_time is not None:
            raise ValueError("since_time is only valid for since_time scope")
        if (self.scope == "since_commit") != (self.since_commit is not None):
            raise ValueError(
                "since_commit scope requires a full SHA, and only that scope accepts it"
            )
        return self


class Send(Record):
    protocol_version: Literal[1] = 1
    message_id: Identifier
    recipient_agent_id: Identifier
    task_id: Identifier
    attempt_id: Identifier
    epoch: Annotated[int, Field(ge=1)]
    controller_id: Identifier | None = None
    kind: Kind
    correlation_id: Identifier | None = None
    in_reply_to: Identifier | None = None
    head_sha: Commit | None = None
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_message(self) -> Self:
        if len(json.dumps(self.payload, ensure_ascii=False, allow_nan=False).encode()) > 8000:
            raise ValueError("payload exceeds 8000 bytes")
        if self.kind in ("question", "status_query") and self.correlation_id is None:
            raise ValueError("questions require correlation_id")
        if self.kind in ("answer", "status_reply"):
            if self.correlation_id is None or self.in_reply_to is None:
                raise ValueError("replies require correlation_id and in_reply_to")
        elif self.in_reply_to is not None and self.kind not in ("question", "progress", "result"):
            raise ValueError("in_reply_to is only valid for a reply")
        if self.in_reply_to is not None and self.correlation_id is None:
            raise ValueError("anchored messages require correlation_id")
        if self.kind == "status_query":
            StatusScope.model_validate_json(json.dumps(self.payload))
        return self


class Ack(Record):
    envelope_id: Identifier
    epoch: Annotated[int, Field(ge=1)]


class MailboxDiscard(Record):
    epoch: Annotated[int, Field(ge=1)]
    envelope_ids: Annotated[list[Identifier], Field(min_length=1, max_length=100)]
    reason: Annotated[str, Field(min_length=1, max_length=512)]

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if len(set(self.envelope_ids)) != len(self.envelope_ids):
            raise ValueError("envelope_ids must be unique")
        if not self.reason.strip():
            raise ValueError("discard requires a nonblank reconciliation reason")
        return self


class DiscardEvidence(Record):
    epoch: Annotated[int, Field(ge=1)]
    session_id: Identifier
    reason: Annotated[str, Field(min_length=1, max_length=512)]
    discarded_at: float


class TaskClose(Fence):
    attempt_id: Identifier
    outcome: Literal["completed", "failed", "cancelled"]
    stop_confirmed: Literal[True]

    @field_validator("stop_confirmed", mode="before")
    @classmethod
    def explicit_stop_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("stop_confirmed requires explicit true")
        return value


class RuntimeStop(Record):
    session_id: Identifier
    thread_id: str | None
    role: Literal["brain", "grunt", "reviewer"]
    process_tag: Label
    process_domain_id: Label
    domain_stop_confirmed: Literal[True]
    stop_confirmed: Literal[True]

    @field_validator("stop_confirmed", "domain_stop_confirmed", mode="before")
    @classmethod
    def explicit_stop(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Native process stop requires explicit true")
        return value


class RuntimeStopEvidence(Record):
    runtime_id: Identifier
    host_id: Label
    inventory_complete: Literal[True]
    processes: Annotated[list[RuntimeStop], Field(min_length=1, max_length=1024)]

    @field_validator("inventory_complete", mode="before")
    @classmethod
    def complete_inventory(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Native stop inventory must include starting and unbound processes")
        return value


class RuntimeRecovery(Record):
    recovery_id: Identifier
    runtime_id: Identifier
    host_id: Label
    previous_thread_id: str
    previous_epoch: Annotated[int, Field(ge=1)]
    previous_controller_id: Identifier
    replacement_controller_id: Identifier
    supervised: bool = False
    state: Literal["stopping", "stopped", "ready", "binding", "attached"] = "stopping"
    stop_manifest_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    stop_receipt_id: Identifier | None = None
    stop_receipt_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    stop_request_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    stop_generation: SupervisedGeneration | None = None

    @model_validator(mode="after")
    def supervised_stop_identity(self) -> Self:
        fields = (
            self.stop_receipt_id,
            self.stop_receipt_sha256,
            self.stop_request_sha256,
            self.stop_generation,
        )
        if self.state == "stopped" and all(value is None for value in fields):
            raise ValueError("Stopped recovery requires a supervised receipt")
        if any(value is not None for value in fields):
            if (
                not self.supervised
                or any(value is None for value in fields)
                or self.stop_manifest_sha256 is None
                or self.state == "stopping"
            ):
                raise ValueError(
                    "Supervised completion requires complete terminal receipt identity"
                )
            generation = self.stop_generation
            assert generation is not None
            if (
                generation.runtime_id != self.runtime_id
                or generation.host_id != self.host_id
                or generation.controller_id != self.previous_controller_id
                or generation.epoch != self.previous_epoch
            ):
                raise ValueError("Receipt generation differs from the original recovery")
        return self


class Team(Record):
    protocol_version: Literal[1] = 1
    team_id: Identifier
    owner_id: str
    brain_agent_id: Identifier
    brain_session_id: Identifier
    config: TeamConfig
    config_revision: int = 1
    epoch: int
    controller_id: Identifier | None
    lease_expires_at: float
    runtime_recoveries: Annotated[list[RuntimeRecovery], Field(max_length=32)] = Field(
        default_factory=list
    )


class Agent(Record):
    team_id: Identifier
    agent_id: Identifier
    session_id: Identifier
    role: Role
    name: str
    profile: Profile


class Attempt(Record):
    team_id: Identifier
    task_id: Identifier
    attempt_id: Identifier
    agent_id: Identifier
    session_id: Identifier
    epoch: int
    state: Literal["reserved", "completed", "failed", "cancelled"]
    workspace: Workspace
    profile: Profile
    config_revision: int = 1
    checkpoint: Checkpoint | None = None
    review_round_id: Identifier | None = None


class Task(Record):
    team_id: Identifier
    task_id: Identifier
    contract: Contract
    attempt: Attempt


class NativeBinding(Record):
    team_id: Identifier
    attempt_id: Identifier
    epoch: Annotated[int, Field(ge=1)]
    thread_id: Annotated[
        str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    ]


class NativeAssignment(Record):
    team: Team
    attempt: Attempt
    binding: NativeBinding


class NativeBrainBinding(Record):
    team_id: Identifier
    epoch: Annotated[int, Field(ge=1)]
    runtime_id: Identifier | None = None
    host_id: Label | None = None
    thread_id: Annotated[
        str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    ]


class NativeMessageOrigin(Record):
    thread_id: Annotated[
        str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    ]
    message_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
    correlation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
    in_reply_to: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")] | None = None


class Envelope(Send):
    envelope_id: Identifier
    team_id: Identifier
    sender_agent_id: Identifier
    sender_session_id: Identifier
    sender_role: Role
    sender_name: str
    native_origin: NativeMessageOrigin | None = None
    sequence: int
    created_at: float
    consumed_at: float | None = None
    replied_at: float | None = None
    discard: DiscardEvidence | None = None


class Receipt(Record):
    envelope_id: Identifier
    message_id: Identifier
    sequence: int
    state: Literal["accepted"] = "accepted"


class Recovery(Record):
    team: Team
    agents: Annotated[list[Agent], Field(max_length=100)]
    tasks: Annotated[list[Task], Field(max_length=100)]
    pending_envelopes: Annotated[int, Field(ge=0, le=100)]
    pending_envelopes_has_more: bool
    next_cursor: Annotated[str, Field(max_length=1024)] | None
