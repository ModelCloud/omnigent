"""Fault-injected kernel mechanics; real escape/UID/network acceptance is separate."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnigent.bgr_supervision import CgroupDomain
from omnigent.host import bgr_cgroup
from omnigent.host.bgr_cgroup import PinnedCgroup
from omnigent.host.bgr_supervisor import SupervisorUnavailable
from tests._helpers.polling import poll_until

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
        (["populated\npopulated 0\n"], None),
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


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_TEST_BGR_CGROUP") != "1",
    reason="Opt-in root/systemd kernel test; not production containment acceptance",
)
@pytest.mark.parametrize("frozen", [False, True])
def test_real_systemd_domain_kills_double_fork_setsid_and_concurrent_forks(frozen):
    assert sys.platform == "linux" and os.geteuid() == 0
    unit = f"omnigent-bgr-test-{uuid4().hex}.service"
    slice_name = f"bgrtest{uuid4().hex}.slice"
    script = """
import os, time
if os.fork() == 0:
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    while True:
        child = os.fork()
        if child == 0:
            time.sleep(0.1)
            os._exit(0)
        time.sleep(0.02)
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
while True:
    time.sleep(1)
"""
    pinned = None
    try:
        subprocess.run(
            [
                "systemd-run",
                "--quiet",
                f"--unit={unit}",
                f"--slice={slice_name}",
                "--property=Type=exec",
                "--property=DynamicUser=yes",
                "--property=RemainAfterExit=yes",
                "--property=Restart=no",
                "--property=TasksMax=32",
                "--property=RuntimeMaxSec=30",
                "--property=NoNewPrivileges=yes",
                "--property=CapabilityBoundingSet=",
                "--property=ProtectControlGroups=yes",
                "--property=PrivateTmp=yes",
                "--property=PrivateNetwork=yes",
                "/usr/bin/python3",
                "-I",
                "-c",
                script,
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        relative = subprocess.run(
            ["systemctl", "show", unit, "--property=ControlGroup", "--value"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        assert relative == f"/{slice_name}/{unit}"
        # Retain the generation parent independently of the runner service's lifetime.
        subprocess.run(
            ["systemctl", "start", slice_name], check=True, capture_output=True, timeout=5
        )
        relative = f"/{slice_name}"
        mount = Path("/sys/fs/cgroup")
        directory = mount / relative.lstrip("/")
        main_process = int(
            subprocess.run(
                ["systemctl", "show", unit, "--property=MainPID", "--value"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        )
        main_session = os.getsid(main_process)

        def escaped_session_is_running():
            for value in (directory / unit / "cgroup.procs").read_text().split():
                process_id = int(value)
                try:
                    if os.getsid(process_id) == main_session:
                        continue
                    status = Path(f"/proc/{process_id}/status").read_text()
                    assert int(status.split("Uid:", 1)[1].split()[0]) != 0
                    assert int(status.split("PPid:", 1)[1].split()[0]) == 1
                    return True
                except (ProcessLookupError, FileNotFoundError):
                    continue
            return False

        poll_until(escaped_session_is_running, timeout_s=5, interval_s=0.01)
        metadata = directory.stat()
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            domain = CgroupDomain(
                domain_id=uuid4().hex,
                mount_id=bgr_cgroup._mount_id(descriptor),
                device=metadata.st_dev,
                inode=metadata.st_ino,
                path=relative,
            )
        finally:
            os.close(descriptor)
        pinned = PinnedCgroup(mount, domain, boot_id=bgr_cgroup._boot_id())
        if frozen:
            (directory / "cgroup.freeze").write_text("1\n")
            poll_until(
                lambda: "frozen 1" in (directory / "cgroup.events").read_text().splitlines(),
                timeout_s=5,
                interval_s=0.01,
            )
        proof = pinned.kill_and_wait(timeout_s=5)
        assert proof.domain == domain
        assert "populated 0" in (directory / "cgroup.events").read_text().splitlines()
    finally:
        if pinned is not None:
            pinned.close()
        subprocess.run(["systemctl", "stop", unit], capture_output=True, timeout=10)
        subprocess.run(["systemctl", "reset-failed", unit], capture_output=True, timeout=5)
        subprocess.run(["systemctl", "stop", slice_name], capture_output=True, timeout=5)
