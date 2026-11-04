"""Fault-injected kernel mechanics; real escape/UID/network acceptance is separate."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.bgr_supervision import CgroupDomain
from omnigent.host import bgr_cgroup
from omnigent.host.bgr_cgroup import PinnedCgroup
from omnigent.host.bgr_supervisor import SupervisorUnavailable

BOOT = "12345678-1234-1234-1234-123456789abc"
DOMAIN = CgroupDomain(
    domain_id="1" * 32,
    mount_id=31,
    device=29,
    inode=42,
    path="/system.slice/omnigent-bgr-unique.service",
)


class _KernelDomain(PinnedCgroup):
    def __init__(self, events, *, fault=None):
        self.domain = DOMAIN
        self.boot_id = BOOT
        self.events = iter(events)
        self.fault = fault
        self.operations = []
        self.elapsed = 0.0

    def _validate(self):
        self.operations.append("validate")
        if self.fault == "identity":
            raise SupervisorUnavailable("Replaced domain")

    def _kill(self):
        self.operations.append("cgroup.kill")
        if self.fault == "kill":
            raise PermissionError("cgroup.kill denied")

    def _read_control(self, name):
        assert name == "cgroup.events"
        self.operations.append(name)
        value = next(self.events)
        if isinstance(value, Exception):
            raise value
        return value

    def tick(self):
        self.elapsed += 1
        return self.elapsed

    def close(self):
        pass


def test_recursive_empty_even_if_frozen(monkeypatch):
    domain = _KernelDomain(["populated 1\nfrozen 1\n", "populated 0\nfrozen 1\n"])
    sleeps = []
    monkeypatch.setattr(bgr_cgroup, "sleep", sleeps.append)
    proof = domain.kill_and_wait()
    assert proof.domain == DOMAIN
    assert proof.boot_id == BOOT
    assert proof.method == "cgroup.kill+recursive-populated-zero"
    assert sleeps == [0.01]
    assert domain.operations == [
        "validate",
        "cgroup.kill",
        "validate",
        "cgroup.events",
        "validate",
        "cgroup.events",
        "validate",
    ]


@pytest.mark.parametrize(
    "events,fault",
    [
        ([], "identity"),
        ([], "kill"),
        (["frozen 0\n"], None),
        (["populated 0\npopulated 1\n"], None),
        (["populated unknown\n"], None),
        ([FileNotFoundError("unlinked cgroup")], None),
        ([PermissionError("events denied")], None),
        ([UnicodeDecodeError("ascii", b"\xff", 0, 1, "unknown")], None),
    ],
)
def test_unknown_or_partial_kernel_evidence_is_never_a_proof(events, fault):
    domain = _KernelDomain(events, fault=fault)
    with pytest.raises(SupervisorUnavailable, match="unconfirmed"):
        domain.kill_and_wait()
    if fault == "identity":
        assert "cgroup.kill" not in domain.operations


def test_populated_descendant_times_out_even_when_main_process_has_exited(monkeypatch):
    domain = _KernelDomain(["populated 1\n"])
    monkeypatch.setattr(bgr_cgroup, "monotonic", domain.tick)
    with pytest.raises(SupervisorUnavailable, match="unconfirmed"):
        domain.kill_and_wait(timeout_s=0.1)
    assert domain.operations.count("cgroup.events") == 1


@pytest.mark.parametrize(
    "boot,mount,inode,nlink",
    [("other", 31, 42, 2), (BOOT, 32, 42, 2), (BOOT, 31, 43, 2), (BOOT, 31, 42, 0)],
)
def test_pinned_identity_changes_are_refused_before_kill(monkeypatch, boot, mount, inode, nlink):
    pinned = object.__new__(PinnedCgroup)
    pinned.domain = DOMAIN
    pinned.boot_id = BOOT
    pinned._mount_fd = 101
    pinned._domain_fd = 102
    monkeypatch.setattr(bgr_cgroup, "_boot_id", lambda: boot)
    monkeypatch.setattr(bgr_cgroup, "_mount_id", lambda fd: mount)
    monkeypatch.setattr(bgr_cgroup, "_is_cgroup2_mount", lambda mount_id: True)
    # Patch only the module seam, not the global os module used by pytest.
    monkeypatch.setattr(
        bgr_cgroup,
        "os",
        SimpleNamespace(
            fstat=lambda fd: SimpleNamespace(
                st_dev=29, st_ino=inode, st_mode=0o40755, st_nlink=nlink
            )
        ),
    )
    with pytest.raises(SupervisorUnavailable):
        pinned._validate()


def test_unsupported_host_cannot_pin_an_ordinary_filesystem(tmp_path):
    with pytest.raises(SupervisorUnavailable):
        PinnedCgroup(tmp_path, DOMAIN, boot_id=BOOT)


@pytest.mark.parametrize("mode,uid", [(0o100664, 0), (0o100644, 1000), (0o100666, 0)])
def test_migration_controls_must_not_be_worker_writable(mode, uid):
    with pytest.raises(SupervisorUnavailable):
        bgr_cgroup._root_protected(SimpleNamespace(st_uid=uid, st_mode=mode))


def test_constructor_requires_trusted_root_before_any_filesystem_access(monkeypatch, tmp_path):
    monkeypatch.setattr(bgr_cgroup, "os", SimpleNamespace(geteuid=lambda: 1000))
    with pytest.raises(SupervisorUnavailable):
        PinnedCgroup(tmp_path, DOMAIN, boot_id=BOOT)
