"""Fsynced admission inventory outside runner memory, workspaces and native homes."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from time import monotonic
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import Field, TypeAdapter, field_validator, model_validator

from omnigent.bgr_records import Identifier, Record, Role
from omnigent.bgr_supervision import (
    CgroupDomain,
    ClosedGenerationManifest,
    GenerationAdmission,
    GenerationStopReceipt,
    Sequence,
    Sha256,
    SupervisedGeneration,
    SupervisorStopRequest,
    TerminalClaim,
    supervision_digest,
)
from omnigent.host.bgr_cgroup import CgroupEmptyProof, _open_directory, _root_protected
from omnigent.host.bgr_launch import StubLaunchPlan, UnitLaunch
from omnigent.host.bgr_supervisor import SupervisorUnavailable

_MAX_JOURNAL_BYTES = 1024 * 1024
_IDENTIFIER = TypeAdapter(Identifier)


class AdmissionClaim(Record):
    claim_id: Identifier
    admission_sequence: Sequence
    role: Role | Literal["runner", "helper"]
    session_id: Identifier | None = None
    attempt_id: Identifier | None = None
    state: Literal["RESERVED", "STARTING", "SPAWNED_UNBOUND", "BOUND"] = "RESERVED"


class GenerationJournal(Record):
    protocol_version: Literal[1] = 1
    generation: SupervisedGeneration
    supervisor_incarnation: Identifier
    manifest_id: Identifier
    domain_id: Identifier
    launch_sha256: Sha256
    sequence: Sequence = 1
    state: Literal["OPEN", "STOPPING", "CLOSED"] = "OPEN"
    domain: CgroupDomain | None = None
    start_plan: StubLaunchPlan | None = None
    unit_launch: UnitLaunch | None = None
    release_committed: bool = False
    launchers_quiesced: bool = False
    closed_admission_sequence: Sequence | None = None
    recovery_id: Identifier | None = None
    claims: Annotated[tuple[AdmissionClaim, ...], Field(min_length=1, max_length=1024)]
    receipt: GenerationStopReceipt | None = None

    @field_validator("claims", mode="before")
    @classmethod
    def immutable_claims(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def consistent_inventory(self) -> Self:
        root = self.claims[0]
        plan = self.start_plan
        launch = self.unit_launch
        if plan is not None and (
            plan.request.generation != self.generation
            or plan.request.manifest_id != self.manifest_id
            or plan.request.domain_id != self.domain_id
            or plan.request.root_claim_id != root.claim_id
            or plan.supervisor_incarnation != self.supervisor_incarnation
            or supervision_digest(plan.request) != self.launch_sha256
        ):
            raise ValueError("Start plan changed the original admission")
        if launch is not None and (
            plan is None
            or launch.profile_sha256 != plan.profile_sha256
            or launch.barrier_id != plan.barrier_id
            or launch.generation_id != self.generation.generation_id
            or launch.launch_id != self.generation.launch_id
            or launch.supervisor_incarnation != self.supervisor_incarnation
            or self.domain is None
            or launch.domain_sha256 != supervision_digest(self.domain)
            or self.domain.path != f"/{launch.slice_unit}"
        ):
            raise ValueError("Unit launch does not match the retained generation slice")
        if self.release_committed and (
            launch is None or root.state not in ("SPAWNED_UNBOUND", "BOUND")
        ):
            raise ValueError("Release requires an immutable spawned unit mapping")
        if self.launchers_quiesced and (plan is None or self.state == "OPEN"):
            raise ValueError("Launcher quiescence requires closed start admissions")
        if (
            root.role != "runner"
            or root.admission_sequence != 1
            or root.session_id != self.generation.brain_session_id
            or (self.domain is not None and self.domain.domain_id != self.domain_id)
        ):
            raise ValueError("Original runner admission changed")
        if len({claim.claim_id for claim in self.claims}) != len(self.claims):
            raise ValueError("Duplicate admission identity")
        sequences = [claim.admission_sequence for claim in self.claims]
        if sequences != sorted(set(sequences)) or sequences[-1] > self.sequence:
            raise ValueError("Admission sequence is not monotonic")
        if self.state != "OPEN":
            if (
                self.recovery_id is None
                or self.closed_admission_sequence is None
                or self.closed_admission_sequence < sequences[-1]
                or self.closed_admission_sequence >= self.sequence
            ):
                raise ValueError("Closed admission barrier is missing")
        elif self.recovery_id is not None or self.closed_admission_sequence is not None:
            raise ValueError("Open generation cannot contain a stop barrier")
        if self.state == "CLOSED":
            if (
                self.receipt is None
                or self.receipt.recovery_id != self.recovery_id
                or self.receipt.manifest.admission != self.admission()
                or self.receipt.manifest.journal_sequence != self.sequence
                or self.receipt.manifest.closed_admission_sequence
                != self.closed_admission_sequence
                or len(self.receipt.manifest.claims) != len(self.claims)
            ):
                raise ValueError("Closed generation has no exact durable receipt")
            for claim, terminal in zip(self.claims, self.receipt.manifest.claims, strict=True):
                if any(
                    getattr(claim, key) != getattr(terminal, key)
                    for key in (
                        "claim_id",
                        "admission_sequence",
                        "role",
                        "attempt_id",
                        "session_id",
                    )
                ):
                    raise ValueError("Terminal receipt changed the admission inventory")
                if (
                    claim.state == "RESERVED" and terminal.disposition != "cancelled_before_exec"
                ) or (
                    claim.state in ("SPAWNED_UNBOUND", "BOUND")
                    and terminal.disposition != "domain_stopped"
                ):
                    raise ValueError("Terminal receipt changed a launch outcome")
        elif self.receipt is not None:
            raise ValueError("Unclosed generation cannot issue a terminal receipt")
        return self

    def admission(self) -> GenerationAdmission:
        if self.domain is None:
            raise SupervisorUnavailable("Generation domain was never pinned")
        return GenerationAdmission(
            generation=self.generation,
            domain=self.domain,
            manifest_id=self.manifest_id,
            root_claim_id=self.claims[0].claim_id,
            admission_sequence=1,
            launch_sha256=self.launch_sha256,
        )


class JournalTransaction:
    """Hold the per-generation lock across launcher operations as well as journal writes."""

    def __init__(self, journal: SupervisorJournal, document: GenerationJournal) -> None:
        self.journal = journal
        self.document = document
        self._active = True
        self._barrier_observed_monotonic = monotonic() if document.state != "OPEN" else None

    def _require_active(self) -> None:
        if not self._active:
            raise SupervisorUnavailable("Supervisor journal transaction has ended")

    def _save(self, **updates: object) -> GenerationJournal:
        self._require_active()
        values = {**self.document.model_dump(), **updates}
        document = GenerationJournal.model_validate(values)
        try:
            self.journal._write(document)
        except BaseException:
            self._active = False
            raise
        self.document = document
        return document

    def _require_open(self) -> None:
        self._require_active()
        if (
            self.document.state != "OPEN"
            or self.document.supervisor_incarnation != self.journal.incarnation
        ):
            raise SupervisorUnavailable(
                "Admissions are closed or require supervisor reconciliation"
            )

    def reserve(self, claim: AdmissionClaim) -> AdmissionClaim:
        self._require_open()
        existing = next(
            (item for item in self.document.claims if item.claim_id == claim.claim_id), None
        )
        if existing is not None:
            if existing != claim:
                raise SupervisorUnavailable("Admission identity has different arguments")
            return existing
        if (
            len(self.document.claims) >= 1024
            or claim.role == "runner"
            or claim.state != "RESERVED"
            or claim.admission_sequence != self.document.sequence + 1
        ):
            raise SupervisorUnavailable("Admission capacity or sequence is invalid")
        self._save(
            claims=[*self.document.claims, claim],
            sequence=claim.admission_sequence,
        )
        return claim

    def transition(
        self, claim_id: str, state: Literal["STARTING", "SPAWNED_UNBOUND", "BOUND"]
    ) -> None:
        self._require_open()
        allowed = {
            "RESERVED": "STARTING",
            "STARTING": "SPAWNED_UNBOUND",
            "SPAWNED_UNBOUND": "BOUND",
        }
        claims = list(self.document.claims)
        for index, claim in enumerate(claims):
            if claim.claim_id == claim_id:
                if claim.state == state:
                    return
                if allowed.get(claim.state) != state:
                    raise SupervisorUnavailable("Admission transition is not monotonic")
                claims[index] = claim.model_copy(update={"state": state})
                self._save(claims=claims, sequence=self.document.sequence + 1)
                return
        raise SupervisorUnavailable("Admission claim is unknown")

    def pin(self, domain: CgroupDomain) -> GenerationAdmission:
        self._require_open()
        if (
            domain.domain_id != self.document.domain_id
            or self.document.claims[0].state != "STARTING"
            or (self.document.domain is not None and self.document.domain != domain)
        ):
            raise SupervisorUnavailable("Generation domain changed or was not started")
        if self.document.domain is None:
            self._save(domain=domain, sequence=self.document.sequence + 1)
        return self.document.admission()

    def close_admissions(self, request: SupervisorStopRequest) -> GenerationJournal:
        self._require_active()
        if self.document.admission() != request.admission:
            raise SupervisorUnavailable("Stop does not match the original durable admission")
        if self.document.state != "OPEN":
            if self.document.recovery_id != request.recovery_id:
                raise SupervisorUnavailable("Generation already has a different stop request")
            return self.document
        document = self._save(
            state="STOPPING",
            recovery_id=request.recovery_id,
            closed_admission_sequence=self.document.sequence,
            sequence=self.document.sequence + 1,
        )
        self._barrier_observed_monotonic = monotonic()
        return document

    def map_unit(self, launch: UnitLaunch) -> None:
        self._require_open()
        launch = UnitLaunch.model_validate_json(launch.model_dump_json())
        if self.document.claims[0].state != "SPAWNED_UNBOUND":
            raise SupervisorUnavailable("Unit mapping requires a spawned root admission")
        if self.document.unit_launch is not None:
            raise SupervisorUnavailable("Unit invocation is already mapped")
        self._save(unit_launch=launch, sequence=self.document.sequence + 1)

    def plan_start(self, plan: StubLaunchPlan) -> None:
        self._require_open()
        if self.document.start_plan is not None or self.document.claims[0].state != "STARTING":
            raise SupervisorUnavailable("Start is already planned or not starting")
        self._save(start_plan=plan, sequence=self.document.sequence + 1)

    def quiesce_launchers(self, plan: StubLaunchPlan) -> None:
        self._require_active()
        if (
            self.document.state != "STOPPING"
            or self.document.start_plan != plan
            or plan.supervisor_incarnation != self.journal.incarnation
        ):
            raise SupervisorUnavailable("Launcher reconciliation is unavailable")
        self._save(launchers_quiesced=True, sequence=self.document.sequence + 1)
        self._barrier_observed_monotonic = monotonic()

    def commit_release(self, launch: UnitLaunch) -> None:
        """Persist intent before sending; delivery failure is execution-unknown."""
        self._require_open()
        if (
            self.document.unit_launch != launch
            or self.document.release_committed
            or self.document.claims[0].state != "SPAWNED_UNBOUND"
        ):
            raise SupervisorUnavailable("Release mapping is stale or already committed")
        self._save(release_committed=True, sequence=self.document.sequence + 1)

    def seal(
        self,
        request: SupervisorStopRequest,
        proof: CgroupEmptyProof,
        *,
        cancelled_before_exec: frozenset[str],
    ) -> GenerationStopReceipt:
        """External launchers must be quiesced under this lock before kernel stop proof."""
        self._require_active()
        document = self.document
        if (
            document.state == "OPEN"
            or document.recovery_id != request.recovery_id
            or document.admission() != request.admission
        ):
            raise SupervisorUnavailable("Original admissions must already be closed")
        if document.state == "CLOSED":
            assert document.receipt is not None
            return document.receipt
        if document.supervisor_incarnation != self.journal.incarnation:
            raise SupervisorUnavailable("Historical domain requires supervisor reconciliation")
        if document.start_plan is not None and not document.launchers_quiesced:
            raise SupervisorUnavailable("External launchers have not been quiesced")
        if (
            proof.boot_id != document.generation.boot_id
            or proof.domain != document.domain
            or self._barrier_observed_monotonic is None
            or proof.observed_monotonic < self._barrier_observed_monotonic
        ):
            raise SupervisorUnavailable("Kernel proof belongs to another domain incarnation")
        claims = {claim.claim_id: claim for claim in document.claims}
        if not cancelled_before_exec <= claims.keys() or any(
            claims[claim_id].state not in ("RESERVED", "STARTING")
            for claim_id in cancelled_before_exec
        ):
            raise SupervisorUnavailable("Cancelled launcher inventory is inconsistent")
        if any(
            claim.state in ("RESERVED", "STARTING") and claim.claim_id not in cancelled_before_exec
            for claim in document.claims
        ):
            raise SupervisorUnavailable("Pending admission was not explicitly cancelled")
        assert document.closed_admission_sequence is not None
        manifest = ClosedGenerationManifest(
            admission=document.admission(),
            closed_admission_sequence=document.closed_admission_sequence,
            journal_sequence=document.sequence + 1,
            claims=tuple(
                TerminalClaim(
                    claim_id=claim.claim_id,
                    admission_sequence=claim.admission_sequence,
                    domain_id=document.domain_id,
                    role=claim.role,
                    attempt_id=claim.attempt_id,
                    session_id=claim.session_id,
                    disposition=(
                        "cancelled_before_exec"
                        if claim.claim_id in cancelled_before_exec
                        else "domain_stopped"
                    ),
                )
                for claim in document.claims
            ),
        )
        receipt = GenerationStopReceipt(
            recovery_id=request.recovery_id,
            receipt_id=uuid4().hex,
            manifest=manifest,
            manifest_sha256=supervision_digest(manifest),
            proof=proof.method,
        )
        self._save(state="CLOSED", sequence=manifest.journal_sequence, receipt=receipt)
        return receipt


class SupervisorJournal:
    """Never delete unresolved records or reuse a generation filename."""

    def __init__(self, directory: Path) -> None:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise SupervisorUnavailable(
                "Supervisor journal requires an administrator-owned Linux service"
            )
        self.incarnation = uuid4().hex
        self._directory_fd = _open_directory(directory)
        metadata = os.fstat(self._directory_fd)
        if metadata.st_mode & 0o077:
            self.close()
            raise SupervisorUnavailable("Supervisor journal directory must be private")

    @contextmanager
    def _lock(self, generation_id: str, *, create: bool = False) -> Iterator[bool]:
        import fcntl

        generation_id = _IDENTIFIER.validate_python(generation_id)
        fresh = False
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        name = f"{generation_id}.lock"
        if create:
            try:
                fd = os.open(
                    name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._directory_fd
                )
                fresh = True
            except FileExistsError:
                fd = os.open(name, flags, dir_fd=self._directory_fd)
        else:
            fd = os.open(name, flags, dir_fd=self._directory_fd)
        try:
            metadata = os.fstat(fd)
            _root_protected(metadata)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_nlink != 1
            ):
                raise SupervisorUnavailable("Supervisor journal lock is not private")
            fcntl.flock(fd, fcntl.LOCK_EX)
            marker = f"omnigent-bgr-generation-v1:{generation_id}\n".encode()
            if fresh:
                try:
                    os.stat(
                        f"{generation_id}.json",
                        dir_fd=self._directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    contents = marker
                else:
                    contents = b"UNKNOWN: original lock identity is missing\n"
                if os.write(fd, contents) != len(contents):
                    raise SupervisorUnavailable("Generation marker write was incomplete")
                os.fsync(fd)
                os.fsync(self._directory_fd)
                if contents != marker:
                    raise SupervisorUnavailable("Original generation lock identity is missing")
            elif os.pread(fd, len(marker) + 1, 0) != marker:
                raise SupervisorUnavailable("Generation marker is missing or unreconciled")
            yield fresh
        finally:
            os.close(fd)

    def _read(self, generation_id: str) -> GenerationJournal:
        fd = os.open(
            f"{generation_id}.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self._directory_fd,
        )
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            _root_protected(metadata)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise SupervisorUnavailable("Supervisor journal is not private")
            payload = stream.read(_MAX_JOURNAL_BYTES + 1)
            os.fsync(stream.fileno())
            os.fsync(self._directory_fd)
        if len(payload) > _MAX_JOURNAL_BYTES:
            raise SupervisorUnavailable("Supervisor journal is oversized")
        document = GenerationJournal.model_validate_json(payload)
        if document.generation.generation_id != generation_id:
            raise SupervisorUnavailable("Supervisor generation journal identity changed")
        return document

    def _write(self, document: GenerationJournal) -> None:
        payload = document.model_dump_json().encode()
        if len(payload) > _MAX_JOURNAL_BYTES:
            raise SupervisorUnavailable("Supervisor journal capacity exceeded")
        temporary = f".{uuid4().hex}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self._directory_fd,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                f"{document.generation.generation_id}.json",
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
            os.fsync(self._directory_fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self._directory_fd)

    def create(
        self,
        generation: SupervisedGeneration,
        *,
        domain_id: str,
        manifest_id: str,
        root_claim_id: str,
        launch_sha256: str,
    ) -> GenerationJournal:
        document = GenerationJournal(
            generation=generation,
            supervisor_incarnation=self.incarnation,
            manifest_id=manifest_id,
            domain_id=domain_id,
            launch_sha256=launch_sha256,
            claims=(
                AdmissionClaim(
                    claim_id=root_claim_id,
                    admission_sequence=1,
                    role="runner",
                    session_id=generation.brain_session_id,
                ),
            ),
        )
        with self._lock(generation.generation_id, create=True) as fresh:
            try:
                existing = self._read(generation.generation_id)
            except FileNotFoundError:
                if not fresh:
                    raise SupervisorUnavailable(
                        "Previously seen generation inventory is missing"
                    ) from None
                self._write(document)
                return document
            if fresh:
                raise SupervisorUnavailable("Original generation lock identity is missing")
            if (
                existing.generation != generation
                or existing.domain_id != domain_id
                or existing.manifest_id != manifest_id
                or existing.launch_sha256 != launch_sha256
                or existing.claims[0].claim_id != root_claim_id
            ):
                raise SupervisorUnavailable(
                    "Generation identity was already used for another launch"
                )
            return existing

    @contextmanager
    def transaction(self, generation_id: str) -> Iterator[JournalTransaction]:
        with self._lock(generation_id):
            transaction = JournalTransaction(self, self._read(generation_id))
            try:
                yield transaction
            finally:
                transaction._active = False

    def close(self) -> None:
        if self._directory_fd >= 0:
            fd, self._directory_fd = self._directory_fd, -1
            os.close(fd)
