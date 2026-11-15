"""Root-admin stub coordinator. No production systemd adapter or host launch is enabled."""

from __future__ import annotations

import grp
import os
import pwd
import socket
import stat
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from omnigent.bgr_supervision import (
    CgroupDomain,
    GenerationAdmission,
    GenerationStopReceipt,
    SupervisorStopRequest,
    supervision_digest,
)
from omnigent.host.bgr_cgroup import PinnedCgroup, _open_directory, _root_protected
from omnigent.host.bgr_journal import SupervisorJournal
from omnigent.host.bgr_launch import AdminStubProfile, StubLaunchPlan, StubStartRequest, UnitLaunch
from omnigent.host.bgr_supervisor import SupervisorUnavailable

_SPAWN_GROUPS = {"root", "sudo", "wheel", "docker", "lxd", "incus", "libvirt", "disk"}


def load_admin_profile(path: Path) -> AdminStubProfile:
    directory = _open_directory(path.parent)
    try:
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=directory,
        )
        try:
            metadata = os.fstat(fd)
            _root_protected(metadata)
            if not stat.S_ISREG(metadata.st_mode):
                raise SupervisorUnavailable("Administrator profile is not a regular file")
            payload = os.read(fd, 16385)
            if len(payload) > 16384:
                raise SupervisorUnavailable("Administrator profile is oversized")
            return AdminStubProfile.model_validate_json(payload)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def validate_stub_image(profile: AdminStubProfile) -> None:
    """Deliberately supports only a tiny immutable stub image, not arbitrary root trees."""
    root = Path(profile.root_directory)
    fd = _open_directory(root)
    os.close(fd)
    entries = 0

    def failed_walk(error: OSError) -> None:
        raise error

    for directory, directories, files in os.walk(root, followlinks=False, onerror=failed_walk):
        for name in [*directories, *files]:
            entries += 1
            metadata = (Path(directory) / name).lstat()
            _root_protected(metadata)
            if (
                entries > 4096
                or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
                or metadata.st_mode & 0o6000
            ):
                raise SupervisorUnavailable("Stub image contains unsafe files or links")
    for executable in (profile.gate, profile.workload):
        target = root / executable.lstrip("/")
        metadata = target.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & 0o111:
            raise SupervisorUnavailable("Stub executable is unavailable")
        if "security.capability" in os.listxattr(target, follow_symlinks=False):
            raise SupervisorUnavailable("Stub executable has file capabilities")
    worker = pwd.getpwuid(profile.worker_uid)
    group = grp.getgrgid(profile.worker_gid)
    if (
        worker.pw_gid != profile.worker_gid
        or group.gr_name in _SPAWN_GROUPS
        or set(os.getgrouplist(worker.pw_name, worker.pw_gid)) != {profile.worker_gid}
    ):
        raise SupervisorUnavailable("Worker account has unsafe group memberships")


class AdminManager(Protocol):
    """Trusted local adapter, never supplied by a request or a workload.

    No implementation is shipped: systemd job reconciliation and effective namespace/
    filesystem/credential verification are prerequisites, not inferred from properties.
    """

    def retain_slice(self, plan: StubLaunchPlan) -> CgroupDomain:
        """Create/retain a new non-delegated slice, refusing existing generation names."""
        ...

    def start_gate(self, plan: StubLaunchPlan, profile: AdminStubProfile) -> UnitLaunch:
        """Start only the fixed gate; verify actual UID/groups, image and isolation.

        Require an exclusive worker UID, private namespaces, no external launch IPC,
        no credentials, null stdio and only the private release descriptor inherited.
        Configure the parent slice before exec; never replace a unit or attach a PID.
        Any timeout retains responsibility for the possibly queued job. The returned
        mapping must use an independently observed InvocationID, never a worker claim.
        """
        ...

    def release_channel(self, launch: UnitLaunch) -> socket.socket:
        """Return the original private gate channel, not a path supplied by a worker."""
        ...

    def quiesce(self, plan: StubLaunchPlan, launch: UnitLaunch | None) -> None:
        """Invalidate release channels and cancel/drain/reconcile ALL possible starts.

        Return only when no queued or external request can subsequently create a unit
        in the retained slice. Failure or lost manager continuity must raise UNKNOWN.
        """
        ...


class StubSupervisorBroker:
    """In-process root-admin API; no socket daemon, native runtime, or auth mint."""

    def __init__(
        self,
        journal: SupervisorJournal,
        profile_path: Path,
        *,
        manager: AdminManager | None = None,
        cgroup_mount: Path = Path("/sys/fs/cgroup"),
    ) -> None:
        self.journal = journal
        self.profile_path = profile_path
        self.manager = manager
        self.cgroup_mount = cgroup_mount
        self._domains: dict[str, PinnedCgroup] = {}

    def _manager(self) -> AdminManager:
        if os.geteuid() != 0 or self.manager is None:
            raise SupervisorUnavailable("Root administrator manager backend is unavailable")
        return self.manager

    def start(self, request: StubStartRequest) -> GenerationAdmission:
        manager = self._manager()
        request = StubStartRequest.model_validate_json(request.model_dump_json())
        profile = load_admin_profile(self.profile_path)
        if profile.profile_id != request.profile_id:
            raise SupervisorUnavailable("Administrator profile is not selected")
        validate_stub_image(profile)
        self.journal.create(
            request.generation,
            domain_id=request.domain_id,
            manifest_id=request.manifest_id,
            root_claim_id=request.root_claim_id,
            launch_sha256=supervision_digest(request),
        )
        with self.journal.transaction(request.generation.generation_id) as transaction:
            if transaction.document.claims[0].state != "RESERVED":
                raise SupervisorUnavailable("Generation start was already attempted")
            transaction.transition(request.root_claim_id, "STARTING")
            plan = StubLaunchPlan(
                request=request,
                profile_sha256=supervision_digest(profile),
                supervisor_incarnation=self.journal.incarnation,
                barrier_id=uuid4().hex,
            )
            transaction.plan_start(plan)
            domain = manager.retain_slice(plan)
            if domain.path != f"/{plan.slice_unit}":
                raise SupervisorUnavailable("Manager returned a different retained slice")
            pinned = PinnedCgroup(self.cgroup_mount, domain, boot_id=request.generation.boot_id)
            self._domains[request.generation.generation_id] = pinned
            admission = transaction.pin(domain)
            transaction.transition(request.root_claim_id, "SPAWNED_UNBOUND")
            launch = manager.start_gate(plan, profile)
            transaction.map_unit(launch)
            transaction.commit_release(launch)
            with manager.release_channel(launch) as channel:
                channel.settimeout(5)
                channel.sendall(launch.model_dump_json().encode() + b"\n")
            return admission

    def stop(self, request: SupervisorStopRequest) -> GenerationStopReceipt:
        if os.geteuid() != 0:
            raise SupervisorUnavailable("Root administrator backend is unavailable")
        with self.journal.transaction(request.admission.generation.generation_id) as transaction:
            transaction.close_admissions(request)
            if transaction.document.state == "CLOSED":
                assert transaction.document.receipt is not None
                self._forget_domain(request.admission.generation.generation_id)
                return transaction.document.receipt
            manager = self._manager()
            if transaction.document.supervisor_incarnation != self.journal.incarnation:
                raise SupervisorUnavailable("Historical invocation requires reconciliation")
            plan = transaction.document.start_plan
            if plan is None:
                raise SupervisorUnavailable("Original manager start inventory is unavailable")
            manager.quiesce(plan, transaction.document.unit_launch)
            transaction.quiesce_launchers(plan)
            pinned = self._domains.get(request.admission.generation.generation_id)
            if pinned is None:
                raise SupervisorUnavailable("Original retained generation handle is unavailable")
            proof = pinned.kill_and_wait()
            receipt = transaction.seal(request, proof, cancelled_before_exec=frozenset())
            self._forget_domain(request.admission.generation.generation_id)
            return receipt

    def _forget_domain(self, generation_id: str) -> None:
        pinned = self._domains.pop(generation_id, None)
        if pinned is not None:
            pinned.close()
