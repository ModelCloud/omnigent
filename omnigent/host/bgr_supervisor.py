"""Opt-in local transport to an administrator-owned BGR supervisor."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import struct
import sys
from asyncio import open_unix_connection
from contextlib import suppress
from pathlib import Path

from pydantic import TypeAdapter

from omnigent.bgr_supervision import (
    GenerationStopReceipt,
    SupervisorStopRequest,
    SupervisorStopResult,
    SupervisorUnknown,
)

_MAX_RESPONSE_BYTES = 1024 * 1024
_STOP_RESPONSE = TypeAdapter(SupervisorStopResult)


class SupervisorUnavailable(RuntimeError):
    """No trusted complete stop proof is available; replacement must stay disabled."""


def _protected_socket(path: Path) -> os.stat_result:
    """Refuse symlinked or non-administrator-controlled channel paths."""
    if not path.is_absolute() or Path(os.path.normpath(path)) != path:
        raise SupervisorUnavailable("Supervisor socket path must be canonical and absolute")
    for parent in reversed(path.parents):
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise SupervisorUnavailable("Supervisor channel directory is not administrator-owned")
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o007:
        raise SupervisorUnavailable("Supervisor socket is not administrator-protected")
    return metadata


class BgrSupervisorClient:
    """No runner RPC, owner attestation, ordinary launch or PID fallback is permitted."""

    def __init__(self, socket_path: Path, *, timeout_s: float = 30) -> None:
        if not 0 < timeout_s <= 120:
            raise ValueError("Supervisor timeout must be between zero and 120 seconds")
        self.socket_path = socket_path
        self.timeout_s = timeout_s

    async def stop(self, request: SupervisorStopRequest) -> GenerationStopReceipt:
        """Request closure by immutable admission, even when every runner is gone."""
        if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
            raise SupervisorUnavailable(
                "Trusted supervisor transport requires Linux peer credentials"
            )
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(self.timeout_s):
                original = _protected_socket(self.socket_path)
                reader, writer = await open_unix_connection(
                    str(self.socket_path), limit=_MAX_RESPONSE_BYTES
                )
                peer = writer.get_extra_info("socket")
                if peer is None:
                    raise SupervisorUnavailable("Supervisor peer credentials are unavailable")
                _pid, uid, _gid = struct.unpack(
                    "3i",
                    peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
                )
                current = _protected_socket(self.socket_path)
                if uid != 0 or (current.st_dev, current.st_ino) != (
                    original.st_dev,
                    original.st_ino,
                ):
                    raise SupervisorUnavailable("Supervisor channel identity changed")
                payload = request.model_dump_json().encode() + b"\n"
                writer.write(payload)
                await writer.drain()
                response = await reader.readline()
                if not response.endswith(b"\n") or len(response) > _MAX_RESPONSE_BYTES:
                    raise SupervisorUnavailable("Supervisor response is incomplete or oversized")
                result = _STOP_RESPONSE.validate_json(response)
                if isinstance(result, SupervisorUnknown):
                    raise SupervisorUnavailable(
                        "Supervisor cannot prove whole-generation shutdown"
                    )
                return result.verify(request)
        except (OSError, TimeoutError, ValueError) as exc:
            raise SupervisorUnavailable("Trusted supervisor stop proof is unavailable") from exc
        finally:
            if writer is not None:
                writer.close()
                with suppress(OSError, TimeoutError):
                    async with asyncio.timeout(1):
                        await writer.wait_closed()
