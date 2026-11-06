"""Pinned cgroup-v2 stop mechanics for the trusted external supervisor only."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from time import monotonic, sleep
from typing import Annotated, Literal

from pydantic import Field

from omnigent.bgr_records import Record
from omnigent.bgr_supervision import BootId, CgroupDomain
from omnigent.host.bgr_supervisor import SupervisorUnavailable

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC
_CONTROL_LIMIT = 4096


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _mount_id(fd: int) -> int:
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return int(line.split()[1])
    raise SupervisorUnavailable("Pinned cgroup mount identity is unavailable")


def _is_cgroup2_mount(mount_id: int) -> bool:
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if fields and fields[0] == str(mount_id):
            separator = fields.index("-")
            return fields[3] == "/" and fields[separator + 1] == "cgroup2"
    return False


def _root_protected(metadata: os.stat_result) -> None:
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise SupervisorUnavailable("Cgroup control is not administrator-protected")


def _check_migration_controls(fd: int) -> None:
    for name in ("cgroup.procs", "cgroup.threads", "cgroup.subtree_control"):
        control_fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=fd)
        try:
            _root_protected(os.fstat(control_fd))
        finally:
            os.close(control_fd)


def _open_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise SupervisorUnavailable("Cgroup mount must be a canonical absolute path")
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        _root_protected(os.fstat(fd))
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            _root_protected(os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


class CgroupEmptyProof(Record):
    """Local kernel evidence, usable only after the journal closes every admission."""

    boot_id: BootId
    domain: CgroupDomain
    method: Literal["cgroup.kill+recursive-populated-zero"] = (
        "cgroup.kill+recursive-populated-zero"
    )
    observed_monotonic: Annotated[float, Field(ge=0)]


class PinnedCgroup:
    """Never migrate PIDs, create a domain, reopen admissions, or remove a tombstone."""

    def __init__(self, mount_path: Path, domain: CgroupDomain, *, boot_id: str) -> None:
        self.domain = domain
        self.boot_id = boot_id
        self._mount_fd = -1
        self._domain_fd = -1
        try:
            if (
                sys.platform != "linux"
                or not _NOFOLLOW
                or os.geteuid() != 0
                or _boot_id() != boot_id
            ):
                raise SupervisorUnavailable(
                    "Cgroup stop requires the original root supervisor boot"
                )
            self._mount_fd = _open_directory(mount_path)
            self._domain_fd = self._open_domain()
            self._validate()
        except (OSError, ValueError, SupervisorUnavailable) as exc:
            self.close()
            raise SupervisorUnavailable("Original cgroup domain cannot be pinned") from exc

    def _open_domain(self) -> int:
        fd = os.dup(self._mount_fd)
        try:
            _check_migration_controls(fd)
            for component in self.domain.path.lstrip("/").split("/"):
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
                _root_protected(os.fstat(fd))
                _check_migration_controls(fd)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _validate(self) -> None:
        if self._mount_fd < 0 or self._domain_fd < 0 or _boot_id() != self.boot_id:
            raise SupervisorUnavailable("Original cgroup handle or boot is unavailable")
        mount_id = _mount_id(self._domain_fd)
        pinned = os.fstat(self._domain_fd)
        if (
            mount_id != self.domain.mount_id
            or not _is_cgroup2_mount(mount_id)
            or (pinned.st_dev, pinned.st_ino) != (self.domain.device, self.domain.inode)
            or not stat.S_ISDIR(pinned.st_mode)
            or pinned.st_nlink == 0
        ):
            raise SupervisorUnavailable("Cgroup mount or inode identity changed")
        current_fd = self._open_domain()
        try:
            current = os.fstat(current_fd)
            if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise SupervisorUnavailable("Cgroup path was renamed or replaced")
        finally:
            os.close(current_fd)
        if self._read_control("cgroup.type").strip() != "domain":
            raise SupervisorUnavailable("Cgroup topology is unsupported")

    def _read_control(self, name: str) -> str:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=self._domain_fd)
        try:
            _root_protected(os.fstat(fd))
            contents = os.read(fd, _CONTROL_LIMIT + 1)
            if len(contents) > _CONTROL_LIMIT:
                raise SupervisorUnavailable("Cgroup control response is oversized")
            return contents.decode("ascii")
        finally:
            os.close(fd)

    def _kill(self) -> None:
        fd = os.open("cgroup.kill", os.O_WRONLY | _NOFOLLOW | _CLOEXEC, dir_fd=self._domain_fd)
        try:
            _root_protected(os.fstat(fd))
            if os.write(fd, b"1\n") != 2:
                raise SupervisorUnavailable("Recursive cgroup kill was incomplete")
        finally:
            os.close(fd)

    def kill_and_wait(self, *, timeout_s: float = 30) -> CgroupEmptyProof:
        """Caller must already close admissions and quiesce out-of-domain launchers."""
        if not 0 < timeout_s <= 120:
            raise ValueError("Cgroup stop timeout must be between zero and 120 seconds")
        try:
            self._validate()
            self._kill()
            deadline = monotonic() + timeout_s
            while True:
                self._validate()
                rows = [row.split() for row in self._read_control("cgroup.events").splitlines()]
                populated = [row for row in rows if row and row[0] == "populated"]
                if populated == [["populated", "0"]]:
                    self._validate()
                    return CgroupEmptyProof(
                        boot_id=self.boot_id,
                        domain=self.domain,
                        observed_monotonic=monotonic(),
                    )
                if populated != [["populated", "1"]]:
                    raise SupervisorUnavailable("Recursive cgroup population is unknown")
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise SupervisorUnavailable("Recursive cgroup shutdown timed out")
                sleep(min(remaining, 0.01))
        except (OSError, ValueError, SupervisorUnavailable) as exc:
            raise SupervisorUnavailable("Whole-domain shutdown is unconfirmed") from exc

    def close(self) -> None:
        for attribute in ("_domain_fd", "_mount_fd"):
            fd = getattr(self, attribute, -1)
            if fd >= 0:
                setattr(self, attribute, -1)
                os.close(fd)
