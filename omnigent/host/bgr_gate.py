"""One-shot root-peer gate. No timeout, EOF or failed validation executes a workload."""

from __future__ import annotations

import errno
import os
import socket
import stat
import struct
from pathlib import Path

from omnigent.bgr_supervision import supervision_digest
from omnigent.host.bgr_launch import AdminStubProfile, UnitLaunch
from omnigent.host.bgr_supervisor import SupervisorUnavailable

_MAX_RELEASE_BYTES = 4096


def _verify_kernel_constraints(channel: socket.socket) -> None:
    try:
        rows = dict(
            line.split(":", 1)
            for line in Path("/proc/self/status").read_text().splitlines()
            if ":" in line
        )
        if rows.get("NoNewPrivs", "").strip() != "1" or any(
            int(rows.get(field, "-1").strip(), 16) != 0
            for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        ):
            raise SupervisorUnavailable("Gate privileges are not confined")
        for descriptor in (0, 1, 2):
            metadata = os.fstat(descriptor)
            if not stat.S_ISCHR(metadata.st_mode) or metadata.st_rdev != os.makedev(1, 3):
                raise SupervisorUnavailable("Gate standard streams are not private null devices")
        for name in os.listdir("/proc/self/fd"):
            descriptor = int(name)
            if descriptor > 2 and descriptor != channel.fileno():
                try:
                    os.fstat(descriptor)
                except OSError as exc:
                    if exc.errno == errno.EBADF:
                        continue
                    raise
                raise SupervisorUnavailable("Gate inherited an unexpected descriptor")
    except (OSError, ValueError) as exc:
        raise SupervisorUnavailable("Gate kernel constraints are unavailable") from exc


def await_release(channel: socket.socket, expected: UnitLaunch, *, timeout_s: float = 30) -> None:
    """The admin adapter supplies this private connected socket and expected mapping."""
    try:
        if not 0 < timeout_s <= 30 or not hasattr(socket, "SO_PEERCRED"):
            raise SupervisorUnavailable("Gate transport or deadline is unsupported")
        if channel.family != socket.AF_UNIX or channel.type != socket.SOCK_SEQPACKET:
            raise SupervisorUnavailable("Gate requires a private Unix packet channel")
        credentials = channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _, uid, _ = struct.unpack("3i", credentials)
        if uid != 0:
            raise SupervisorUnavailable("Gate peer is not the root supervisor")
        channel.settimeout(timeout_s)
        payload = channel.recv(_MAX_RELEASE_BYTES + 1)
        if not payload.endswith(b"\n") or len(payload) > _MAX_RELEASE_BYTES:
            raise SupervisorUnavailable("Gate release is missing or oversized")
        if UnitLaunch.model_validate_json(payload) != expected:
            raise SupervisorUnavailable("Gate release mapping is stale")
    except (OSError, ValueError) as exc:
        raise SupervisorUnavailable("Gate release is unverified") from exc
    finally:
        channel.close()


def run_stub_gate(channel: socket.socket, expected: UnitLaunch, profile: AdminStubProfile) -> None:
    """Called only inside the administrator-confined stub unit, never as root."""
    try:
        if (
            expected.profile_sha256 != supervision_digest(profile)
            or os.getresuid() != (profile.worker_uid,) * 3
            or os.getresgid() != (profile.worker_gid,) * 3
            or set(os.getgroups()) - {profile.worker_gid}
        ):
            raise SupervisorUnavailable("Gate worker credentials are not confined")
        _verify_kernel_constraints(channel)
    except BaseException:
        channel.close()
        raise
    await_release(channel, expected)
    os.execve(profile.workload, [profile.workload], {})
