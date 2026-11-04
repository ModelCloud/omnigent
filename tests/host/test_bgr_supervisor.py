"""Containment records and channel checks, not real cgroup shutdown acceptance."""

from __future__ import annotations

import asyncio
import copy
import json
import socket
import stat
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from omnigent.bgr_supervision import (
    ClosedGenerationManifest,
    GenerationAdmission,
    GenerationStopReceipt,
    SupervisedGeneration,
    SupervisorStopped,
    SupervisorStopRequest,
    supervision_digest,
)
from omnigent.host import bgr_supervisor
from omnigent.host.bgr_supervisor import BgrSupervisorClient, SupervisorUnavailable


@pytest.fixture
def request_stop():
    return SupervisorStopRequest(
        request_id="1" * 32,
        recovery_id="2" * 32,
        admission=GenerationAdmission(
            generation=SupervisedGeneration(
                supervisor_id="3" * 32,
                host_id="host-trusted",
                boot_id="12345678-1234-1234-1234-123456789abc",
                team_id="4" * 32,
                brain_session_id="5" * 32,
                controller_id="6" * 32,
                epoch=1,
                runtime_id="7" * 32,
                generation_id="8" * 32,
                launch_id="9" * 32,
                runner_id="runner-dedicated",
                credential_id="a" * 32,
            ),
            domain={
                "domain_id": "b" * 32,
                "mount_id": 31,
                "device": 29,
                "inode": 42,
                "path": "/system.slice/omnigent-bgr-unique.service",
            },
            manifest_id="c" * 32,
            root_claim_id="d" * 32,
            admission_sequence=1,
            launch_sha256="e" * 64,
        ),
    )


@pytest.fixture
def stopped(request_stop):
    manifest = ClosedGenerationManifest(
        admission=request_stop.admission,
        closed_admission_sequence=3,
        journal_sequence=4,
        claims=[
            {
                "claim_id": request_stop.admission.root_claim_id,
                "admission_sequence": 1,
                "domain_id": request_stop.admission.domain.domain_id,
                "role": "runner",
                "session_id": request_stop.admission.generation.brain_session_id,
                "disposition": "domain_stopped",
            },
            {
                "claim_id": "f" * 32,
                "admission_sequence": 2,
                "domain_id": request_stop.admission.domain.domain_id,
                "role": "helper",
                "session_id": None,
                "disposition": "cancelled_before_exec",
            },
        ],
    )
    return SupervisorStopped(
        request_id=request_stop.request_id,
        request_sha256=supervision_digest(request_stop),
        receipt=GenerationStopReceipt(
            recovery_id=request_stop.recovery_id,
            receipt_id="0" * 32,
            manifest=manifest,
            manifest_sha256=supervision_digest(manifest),
            proof="cgroup.kill+recursive-populated-zero",
        ),
    )


def test_closed_manifest_includes_unbound_admission_and_stable_retry(request_stop, stopped):
    wire = stopped.model_dump(mode="json")
    assert SupervisorStopped.model_validate(wire).verify(request_stop) == stopped.receipt
    assert (
        SupervisorStopped.model_validate_json(json.dumps(wire, indent=2)).verify(request_stop)
        == stopped.receipt
    )
    assert stopped.receipt.manifest.claims[1].session_id is None


@pytest.mark.parametrize(
    "path,value",
    [
        (("request_id",), "f" * 32),
        (("recovery_id",), "f" * 32),
        (("admission", "manifest_id"), "f" * 32),
        (("admission", "root_claim_id"), "f" * 32),
        (("admission", "launch_sha256"), "f" * 64),
        (("admission", "generation", "host_id"), "other-host"),
        (("admission", "generation", "supervisor_id"), "f" * 32),
        (("admission", "generation", "boot_id"), "ffffffff-ffff-ffff-ffff-ffffffffffff"),
        (("admission", "generation", "team_id"), "f" * 32),
        (("admission", "generation", "brain_session_id"), "f" * 32),
        (("admission", "generation", "controller_id"), "f" * 32),
        (("admission", "generation", "epoch"), 2),
        (("admission", "generation", "runtime_id"), "f" * 32),
        (("admission", "generation", "generation_id"), "f" * 32),
        (("admission", "generation", "launch_id"), "f" * 32),
        (("admission", "generation", "runner_id"), "other-runner"),
        (("admission", "generation", "credential_id"), "f" * 32),
        (("admission", "domain", "mount_id"), 32),
        (("admission", "domain", "device"), 30),
        (("admission", "domain", "inode"), 43),
        (("admission", "domain", "path"), "/replacement"),
    ],
)
def test_stop_is_exactly_correlated(request_stop, stopped, path, value):
    wire = request_stop.model_dump(mode="json")
    target = wire
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match="different request/generation"):
        stopped.verify(SupervisorStopRequest.model_validate(wire))


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_claim",
        "duplicate_sequence",
        "missing_root",
        "different_domain",
        "late_claim",
        "unreconciled_claim",
        "wrong_root_session",
        "wrong_root_sequence",
        "open_admissions",
        "missing_terminal_journal",
        "unknown_field",
        "boolean_version",
        "wrong_digest",
        "pid_proof",
    ],
)
def test_partial_or_conflicting_stop_proof_is_refused(stopped, change):
    wire = stopped.receipt.model_dump(mode="json")
    manifest = wire["manifest"]
    root, unbound = manifest["claims"]
    if change == "duplicate_claim":
        manifest["claims"].append(copy.deepcopy(root))
    elif change == "duplicate_sequence":
        unbound["admission_sequence"] = root["admission_sequence"]
    elif change == "missing_root":
        manifest["claims"] = [unbound]
    elif change == "different_domain":
        unbound["domain_id"] = "0" * 32
    elif change == "late_claim":
        unbound["admission_sequence"] = 10
    elif change == "unreconciled_claim":
        unbound["disposition"] = "STARTING"
    elif change == "wrong_root_session":
        root["session_id"] = "0" * 32
    elif change == "wrong_root_sequence":
        root["admission_sequence"] = 3
    elif change == "open_admissions":
        manifest["state"] = "OPEN"
    elif change == "missing_terminal_journal":
        manifest["journal_sequence"] = manifest["closed_admission_sequence"]
    elif change == "unknown_field":
        manifest["inventory_complete"] = True
    elif change == "boolean_version":
        wire["protocol_version"] = True
    elif change == "wrong_digest":
        wire["manifest_sha256"] = "0" * 64
    else:
        wire["proof"] = "main-pid-exited"
    with pytest.raises(ValidationError):
        GenerationStopReceipt.model_validate(wire)


@pytest.mark.parametrize("path", ["/", "//", "//root", "/a/../b", "/a/", "relative", "/a\x00b"])
def test_root_and_mutable_domain_paths_are_refused(request_stop, path):
    wire = request_stop.admission.model_dump(mode="json")
    wire["domain"]["path"] = path
    with pytest.raises(ValidationError):
        GenerationAdmission.model_validate(wire)


class _Peer:
    def __init__(self, uid):
        self.uid = uid

    def getsockopt(self, level, option, size):
        assert (level, option, size) == (
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
        return struct.pack("3i", 1234, self.uid, 0)


class _Writer:
    def __init__(self, uid):
        self.peer = _Peer(uid)
        self.sent = b""
        self.closed = False

    def get_extra_info(self, name):
        assert name == "socket"
        return self.peer

    def write(self, payload):
        self.sent += payload

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.mark.parametrize(
    "fault",
    [None, "nonroot_peer", "replaced_socket", "unknown", "mismatched", "truncated", "oversized"],
)
async def test_supervisor_transport_refuses_untrusted_or_partial_responses(
    monkeypatch, request_stop, stopped, fault
):
    writer = _Writer(1000 if fault == "nonroot_peer" else 0)
    wire = stopped.model_dump(mode="json")
    if fault == "unknown":
        wire = {
            "status": "unknown",
            "request_id": request_stop.request_id,
            "reason": "unreconciled",
        }
    elif fault == "mismatched":
        wire["request_id"] = "f" * 32
    response = json.dumps(wire).encode() + b"\n"
    if fault == "truncated":
        response = response[:-1]
    elif fault == "oversized":
        response = b"x" * (1024 * 1024 + 1) + b"\n"

    async def connect(path, *, limit):
        assert path == "/run/omnigent/supervisor.sock"
        reader = asyncio.StreamReader(limit=limit)
        reader.feed_data(response)
        reader.feed_eof()
        return reader, writer

    checks = 0

    def protected(path):
        nonlocal checks
        checks += 1
        return SimpleNamespace(st_dev=1, st_ino=checks if fault == "replaced_socket" else 1)

    monkeypatch.setattr(bgr_supervisor, "open_unix_connection", connect)
    monkeypatch.setattr(bgr_supervisor, "_protected_socket", protected)
    client = BgrSupervisorClient(Path("/run/omnigent/supervisor.sock"))
    if fault is None:
        assert await client.stop(request_stop) == stopped.receipt
        assert SupervisorStopRequest.model_validate_json(writer.sent) == request_stop
    else:
        with pytest.raises(SupervisorUnavailable):
            await client.stop(request_stop)
    assert writer.closed
    if fault in {"nonroot_peer", "replaced_socket"}:
        assert not writer.sent


@pytest.mark.parametrize(
    "entry,uid,mode",
    [
        ("/run/omnigent", 1000, stat.S_IFDIR | 0o755),
        ("/run/omnigent", 0, stat.S_IFDIR | 0o775),
        ("/run/omnigent", 0, stat.S_IFLNK | 0o777),
        ("/run/omnigent/supervisor.sock", 1000, stat.S_IFSOCK | 0o660),
        ("/run/omnigent/supervisor.sock", 0, stat.S_IFSOCK | 0o666),
        ("/run/omnigent/supervisor.sock", 0, stat.S_IFREG | 0o600),
    ],
)
def test_administrator_channel_protection(monkeypatch, entry, uid, mode):
    def metadata(path):
        is_socket = str(path).endswith(".sock")
        return SimpleNamespace(
            st_uid=uid if str(path) == entry else 0,
            st_mode=mode
            if str(path) == entry
            else (stat.S_IFSOCK | 0o660 if is_socket else stat.S_IFDIR | 0o755),
        )

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(SupervisorUnavailable):
        bgr_supervisor._protected_socket(Path("/run/omnigent/supervisor.sock"))


async def test_unconfigured_host_cannot_produce_stop_proof(tmp_path, request_stop):
    with pytest.raises(SupervisorUnavailable):
        await BgrSupervisorClient(tmp_path / "absent.sock").stop(request_stop)
