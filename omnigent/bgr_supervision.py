"""Supervisor wire records. Validation never substitutes for an authenticated channel."""

from __future__ import annotations

import hashlib
import json
import posixpath
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from omnigent.bgr_records import Identifier, Label, Record, Role

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Sequence = Annotated[int, Field(ge=1)]
BootId = Annotated[
    str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]


def supervision_digest(record: Record) -> str:
    """Hash a canonical wire record, independent of JSON formatting."""
    wire = json.dumps(
        record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(wire.encode()).hexdigest()


class SupervisedGeneration(Record):
    protocol_version: Literal[1] = 1
    supervisor_id: Identifier
    host_id: Label
    boot_id: BootId
    team_id: Identifier
    brain_session_id: Identifier
    controller_id: Identifier
    epoch: Sequence
    runtime_id: Identifier
    generation_id: Identifier
    launch_id: Identifier
    runner_id: Label
    credential_id: Identifier


class CgroupDomain(Record):
    kind: Literal["cgroup_v2"] = "cgroup_v2"
    domain_id: Identifier
    mount_id: Sequence
    device: Annotated[int, Field(ge=0)]
    inode: Sequence
    path: Annotated[str, Field(min_length=2, max_length=4096)]

    @field_validator("path")
    @classmethod
    def nonroot_canonical_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or value.startswith("//")
            or "\x00" in value
            or posixpath.normpath(value) != value
        ):
            raise ValueError("Containment requires a canonical non-root cgroup path")
        return value


class GenerationAdmission(Record):
    """The server retains this receipt before releasing a dedicated runner."""

    protocol_version: Literal[1] = 1
    generation: SupervisedGeneration
    domain: CgroupDomain
    manifest_id: Identifier
    root_claim_id: Identifier
    admission_sequence: Sequence
    launch_sha256: Sha256


class TerminalClaim(Record):
    claim_id: Identifier
    admission_sequence: Sequence
    domain_id: Identifier
    role: Role | Literal["runner", "helper"]
    attempt_id: Identifier | None = None
    session_id: Identifier | None = None
    disposition: Literal["cancelled_before_exec", "domain_stopped"]


class ClosedGenerationManifest(Record):
    """All admissions, including starting and unbound claims, remain in the journal."""

    protocol_version: Literal[1] = 1
    admission: GenerationAdmission
    state: Literal["CLOSED"] = "CLOSED"
    closed_admission_sequence: Sequence
    journal_sequence: Sequence
    claims: Annotated[list[TerminalClaim], Field(min_length=1, max_length=1024)]

    @model_validator(mode="after")
    def closed_inventory(self) -> Self:
        if self.closed_admission_sequence < self.admission.admission_sequence:
            raise ValueError("Admission barrier precedes the original admission")
        if self.journal_sequence <= self.closed_admission_sequence:
            raise ValueError("Terminal journal must follow admission closure")
        claim_ids: set[str] = set()
        sequences: set[int] = set()
        root: TerminalClaim | None = None
        for claim in self.claims:
            if claim.claim_id in claim_ids or claim.admission_sequence in sequences:
                raise ValueError("Duplicate admission claim or sequence")
            if (
                claim.domain_id != self.admission.domain.domain_id
                or claim.admission_sequence < self.admission.admission_sequence
                or claim.admission_sequence > self.closed_admission_sequence
            ):
                raise ValueError("Claim is outside the closed generation")
            claim_ids.add(claim.claim_id)
            sequences.add(claim.admission_sequence)
            if claim.claim_id == self.admission.root_claim_id:
                root = claim
        if (
            root is None
            or root.role != "runner"
            or root.admission_sequence != self.admission.admission_sequence
            or root.session_id != self.admission.generation.brain_session_id
        ):
            raise ValueError("Original whole-runner admission is missing or changed")
        return self


class GenerationStopReceipt(Record):
    protocol_version: Literal[1] = 1
    recovery_id: Identifier
    receipt_id: Identifier
    manifest: ClosedGenerationManifest
    manifest_sha256: Sha256
    proof: Literal["cgroup.kill+recursive-populated-zero"]

    @model_validator(mode="after")
    def exact_manifest_digest(self) -> Self:
        if self.manifest_sha256 != supervision_digest(self.manifest):
            raise ValueError("Terminal manifest digest changed")
        return self


class SupervisorStopRequest(Record):
    protocol_version: Literal[1] = 1
    operation: Literal["stop_generation"] = "stop_generation"
    request_id: Identifier
    recovery_id: Identifier
    admission: GenerationAdmission


class SupervisorStopped(Record):
    protocol_version: Literal[1] = 1
    status: Literal["stopped"] = "stopped"
    request_id: Identifier
    request_sha256: Sha256
    receipt: GenerationStopReceipt

    def verify(self, request: SupervisorStopRequest) -> GenerationStopReceipt:
        """Correlate trusted transport output against the durable original admission."""
        if (
            self.request_id != request.request_id
            or self.request_sha256 != supervision_digest(request)
            or self.receipt.recovery_id != request.recovery_id
            or self.receipt.manifest.admission != request.admission
        ):
            raise ValueError("Supervisor stop receipt belongs to a different request/generation")
        return self.receipt


class SupervisorUnknown(Record):
    protocol_version: Literal[1] = 1
    status: Literal["unknown"] = "unknown"
    request_id: Identifier
    reason: Label
