"""Coordinator fault tests, not real systemd, root isolation or native acceptance."""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import struct
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from omnigent.bgr_supervision import SupervisorStopRequest, supervision_digest
from omnigent.host import bgr_broker, bgr_gate
from omnigent.host.bgr_broker import StubSupervisorBroker
from omnigent.host.bgr_cgroup import CgroupEmptyProof
from omnigent.host.bgr_journal import SupervisorJournal
from omnigent.host.bgr_launch import AdminStubProfile, StubStartRequest, UnitLaunch
from omnigent.host.bgr_supervisor import SupervisorUnavailable
from tests.host.test_bgr_journal import journal_env as journal_env
from tests.host.test_bgr_supervisor import request_stop as request_stop


class _Manager:
    def __init__(self, domain, read):
        self.domain = domain
        self.read = read
        self.events = []
        self.after_start = None
        self.fail_start = False
        self.fail_quiesce = False
        self.sent = False
        self.sender, self.receiver = socket.socketpair(type=socket.SOCK_SEQPACKET)

    def retain_slice(self, plan):
        document = self.read()
        assert document["claims"][0]["state"] == "STARTING"
        assert document["start_plan"]["barrier_id"] == plan.barrier_id
        self.events.append("slice")
        return self.domain

    def start_gate(self, plan, profile):
        document = self.read()
        assert document["domain"] == self.domain.model_dump()
        assert document["claims"][0]["state"] == "SPAWNED_UNBOUND"
        self.events.append("start")
        if self.after_start is not None:
            self.after_start()
        if self.fail_start:
            raise SupervisorUnavailable("Queued job timed out")
        return UnitLaunch(
            generation_id=plan.request.generation.generation_id,
            launch_id=plan.request.generation.launch_id,
            supervisor_incarnation=plan.supervisor_incarnation,
            invocation_id="e" * 32,
            barrier_id=plan.barrier_id,
            profile_sha256=supervision_digest(profile),
            domain_sha256=supervision_digest(self.domain),
        )

    def release_channel(self, launch):
        document = self.read()
        assert document["release_committed"]
        assert document["unit_launch"] == launch.model_dump()
        self.events.append("release")
        self.sent = True
        return self.sender

    def quiesce(self, plan, launch):
        assert self.read()["state"] == "STOPPING"
        self.events.append("quiesce")
        if self.fail_quiesce:
            raise SupervisorUnavailable("Queued job cancellation is unconfirmed")
        self.sender.close()

    def close(self):
        self.sender.close()
        self.receiver.close()


@pytest.fixture
def broker_env(journal_env, request_stop, monkeypatch, tmp_path):
    root, operations = journal_env
    generation = request_stop.admission.generation
    request = StubStartRequest(
        generation=generation,
        profile_id="1" * 32,
        manifest_id=request_stop.admission.manifest_id,
        domain_id=request_stop.admission.domain.domain_id,
        root_claim_id=request_stop.admission.root_claim_id,
    )
    domain = request_stop.admission.domain.model_copy(
        update={"path": f"/bgr{generation.generation_id}.slice"}
    )
    image = tmp_path / "stub-image"
    binaries = image / "usr/libexec"
    binaries.mkdir(parents=True)
    for name in ("omnigent-bgr-gate", "omnigent-bgr-stub"):
        target = binaries / name
        target.write_text("harmless test image")
        target.chmod(0o555)
    profile = AdminStubProfile(
        profile_id=request.profile_id,
        root_directory=str(image),
        worker_uid=60001,
        worker_gid=60001,
        host_uid=1000,
    )
    profile_path = tmp_path / "admin-profile.json"
    profile_path.write_text(profile.model_dump_json())
    monkeypatch.setattr(bgr_broker, "os", operations)
    monkeypatch.setattr(bgr_broker, "_open_directory", lambda path: os.open(path, os.O_RDONLY))
    monkeypatch.setattr(bgr_broker, "_root_protected", lambda metadata: None)
    monkeypatch.setattr(
        bgr_broker,
        "pwd",
        SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_gid=uid, pw_name="stub")),
    )
    monkeypatch.setattr(
        bgr_broker, "grp", SimpleNamespace(getgrgid=lambda gid: SimpleNamespace(gr_name="stub"))
    )
    monkeypatch.setattr(operations, "getgrouplist", lambda name, gid: [gid])
    journal = SupervisorJournal(root)

    def read():
        return json.loads((root / f"{generation.generation_id}.json").read_text())

    manager = _Manager(domain, read)

    class _Pinned:
        invalid = False
        closed = False

        def __init__(self, mount, supplied, *, boot_id):
            if self.invalid or supplied != domain:
                raise SupervisorUnavailable("Pinned domain is missing or replaced")
            self.boot_id = boot_id

        def kill_and_wait(self):
            if self.invalid or self.closed:
                raise SupervisorUnavailable("Pinned domain is missing or replaced")
            assert read()["launchers_quiesced"]
            assert manager.events[-1] == "quiesce"
            manager.events.append("kill")
            return CgroupEmptyProof(
                boot_id=self.boot_id, domain=domain, observed_monotonic=monotonic()
            )

        def close(self):
            self.closed = True

    monkeypatch.setattr(bgr_broker, "PinnedCgroup", _Pinned)
    broker = StubSupervisorBroker(journal, profile_path, manager=manager)
    yield SimpleNamespace(
        root=root,
        broker=broker,
        request=request,
        manager=manager,
        journal=journal,
        operations=operations,
        read=read,
        profile=profile,
        profile_path=profile_path,
        pinned=_Pinned,
    )
    manager.close()
    journal.close()


def _stop_request(env, admission):
    return SupervisorStopRequest(request_id="8" * 32, recovery_id="9" * 32, admission=admission)


def test_release_follows_durable_mapping_and_spawned_claim(broker_env):
    env = broker_env
    admission = env.broker.start(env.request)
    wire = env.manager.receiver.recv(4096)
    launch = UnitLaunch.model_validate_json(wire)
    assert env.manager.events == ["slice", "start", "release"]
    assert launch.slice_unit == Path(admission.domain.path).name
    assert env.read()["unit_launch"] == launch.model_dump()
    assert env.read()["release_committed"]
    assert env.operations.syncs[-2:] == [False, True]
    with pytest.raises(SupervisorUnavailable, match="already attempted"):
        env.broker.start(env.request)
    assert env.manager.events == ["slice", "start", "release"]


@pytest.mark.parametrize("write_stage", ["mapping", "release"])
@pytest.mark.parametrize("after_rename", [False, True])
def test_failed_durable_write_cannot_release_gate(
    broker_env, monkeypatch, write_stage, after_rename
):
    env = broker_env
    original = env.journal._write

    def fail_write(document):
        failed = (
            document.unit_launch is not None
            if write_stage == "mapping"
            else document.release_committed
        )
        if failed:
            if after_rename:
                env.operations.fail_next_directory_sync = True
                original(document)
            raise OSError("Injected fsync failure")
        original(document)

    monkeypatch.setattr(env.journal, "_write", fail_write)
    with pytest.raises(OSError):
        env.broker.start(env.request)
    assert not env.manager.sent
    assert env.read()["claims"][0]["state"] == "SPAWNED_UNBOUND"
    with pytest.raises(SupervisorUnavailable, match="already attempted"):
        env.broker.start(env.request)


def test_release_transport_failure_is_not_cancelled_or_retryable(broker_env):
    env = broker_env
    env.manager.sender.close()
    with pytest.raises(OSError):
        env.broker.start(env.request)
    assert env.read()["release_committed"]
    with pytest.raises(SupervisorUnavailable, match="already attempted"):
        env.broker.start(env.request)


@pytest.mark.parametrize(
    "field",
    [
        "generation_id",
        "launch_id",
        "supervisor_incarnation",
        "barrier_id",
        "profile_sha256",
        "domain_sha256",
        "invocation_id",
    ],
)
def test_unit_mapping_rejects_cross_generation_or_malformed_observations(broker_env, field):
    env = broker_env
    original = env.manager.start_gate

    def stale(plan, profile):
        launch = original(plan, profile)
        return launch.model_copy(
            update={
                field: "invalid" if field == "invocation_id" else "0" * len(getattr(launch, field))
            }
        )

    env.manager.start_gate = stale
    with pytest.raises(ValidationError):
        env.broker.start(env.request)
    assert not env.manager.sent
    assert not env.read()["release_committed"]


def test_stop_before_release_closes_gate_and_rejects_late_release(broker_env, monkeypatch):
    from omnigent.host.bgr_journal import JournalTransaction

    env = broker_env

    def prevent_release(transaction, launch):
        raise SupervisorUnavailable("Injected stop before release")

    with monkeypatch.context() as patch:
        patch.setattr(JournalTransaction, "commit_release", prevent_release)
        with pytest.raises(SupervisorUnavailable, match="before release"):
            env.broker.start(env.request)
    assert not env.manager.sent
    launch = UnitLaunch.model_validate(env.read()["unit_launch"])
    with env.journal.transaction(env.request.generation.generation_id) as transaction:
        admission = transaction.document.admission()
    assert env.broker.stop(_stop_request(env, admission)).manifest.state == "CLOSED"
    assert env.manager.receiver.recv(1) == b""
    with env.journal.transaction(env.request.generation.generation_id) as transaction:
        with pytest.raises(SupervisorUnavailable, match="closed"):
            transaction.commit_release(launch)


def test_empty_slice_alone_cannot_bypass_manager_job_reconciliation(broker_env):
    env = broker_env
    admission = env.broker.start(env.request)
    request = _stop_request(env, admission)
    with env.journal.transaction(env.request.generation.generation_id) as transaction:
        transaction.close_admissions(request)
        proof = CgroupEmptyProof(
            boot_id=request.admission.generation.boot_id,
            domain=request.admission.domain,
            observed_monotonic=monotonic(),
        )
        with pytest.raises(SupervisorUnavailable, match="not been quiesced"):
            transaction.seal(request, proof, cancelled_before_exec=frozenset())


@pytest.mark.parametrize("during_write", [False, True])
def test_empty_proof_before_durable_launcher_quiescence_is_stale(broker_env, during_write):
    env = broker_env
    admission = env.broker.start(env.request)
    request = _stop_request(env, admission)
    with env.journal.transaction(env.request.generation.generation_id) as transaction:
        transaction.close_admissions(request)
        proofs = []

        def sample_proof():
            assert not env.read()["launchers_quiesced"]
            proofs.append(
                CgroupEmptyProof(
                    boot_id=request.admission.generation.boot_id,
                    domain=request.admission.domain,
                    observed_monotonic=monotonic(),
                )
            )

        if during_write:
            env.operations.on_next_file_sync = sample_proof
        else:
            sample_proof()
        env.manager.quiesce(transaction.document.start_plan, transaction.document.unit_launch)
        transaction.quiesce_launchers(transaction.document.start_plan)
        assert len(proofs) == 1
        assert env.read()["launchers_quiesced"]
        with pytest.raises(SupervisorUnavailable, match="incarnation"):
            transaction.seal(request, proofs[0], cancelled_before_exec=frozenset())


def test_broker_loss_cannot_reopen_a_path_to_replace_original_handle(broker_env):
    env = broker_env
    admission = env.broker.start(env.request)
    restarted = StubSupervisorBroker(env.journal, env.profile_path, manager=env.manager)
    with pytest.raises(SupervisorUnavailable, match="Original retained"):
        restarted.stop(_stop_request(env, admission))
    assert env.read()["state"] == "STOPPING"


@pytest.mark.parametrize("quiesce_fails", [False, True])
def test_queued_start_timeout_requires_actual_manager_quiescence(broker_env, quiesce_fails):
    env = broker_env
    env.manager.fail_start = True
    with pytest.raises(SupervisorUnavailable, match="timed out"):
        env.broker.start(env.request)
    assert not env.manager.sent
    with env.journal.transaction(env.request.generation.generation_id) as transaction:
        admission = transaction.document.admission()
    request = _stop_request(env, admission)
    env.manager.fail_quiesce = quiesce_fails
    if quiesce_fails:
        with pytest.raises(SupervisorUnavailable, match="unconfirmed"):
            env.broker.stop(request)
        assert env.read()["state"] == "STOPPING"
        assert "kill" not in env.manager.events
    else:
        receipt = env.broker.stop(request)
        assert receipt.manifest.claims[0].disposition == "domain_stopped"
        assert env.manager.events[-2:] == ["quiesce", "kill"]


def test_fast_service_exit_retains_slice_and_closed_receipt_retries_without_manager(broker_env):
    env = broker_env
    admission = env.broker.start(env.request)
    original_handle = env.broker._domains[env.request.generation.generation_id]
    assert not original_handle.closed
    env.manager.receiver.close()
    request = _stop_request(env, admission)
    receipt = env.broker.stop(request)
    assert original_handle.closed
    env.broker.manager = None
    assert env.broker.stop(request) == receipt


def test_missing_or_replaced_slice_never_seals(broker_env):
    env = broker_env
    admission = env.broker.start(env.request)
    env.pinned.invalid = True
    with pytest.raises(SupervisorUnavailable, match="missing or replaced"):
        env.broker.stop(_stop_request(env, admission))
    assert env.read()["state"] == "STOPPING"
    assert env.read()["receipt"] is None


def test_restart_cannot_release_or_seal_old_invocation(broker_env):
    env = broker_env
    admission = env.broker.start(env.request)
    restarted = SupervisorJournal(env.root)
    env.broker.journal = restarted
    try:
        with pytest.raises(SupervisorUnavailable, match="reconciliation"):
            env.broker.stop(_stop_request(env, admission))
        with restarted.transaction(env.request.generation.generation_id) as transaction:
            with pytest.raises(SupervisorUnavailable, match="reconciliation"):
                transaction.commit_release(transaction.document.unit_launch)
    finally:
        restarted.close()


def test_start_stop_are_serialized_through_manager_job(broker_env):
    env = broker_env
    entered, resume, stopping = Event(), Event(), Event()

    def block_start():
        entered.set()
        assert resume.wait(2)

    env.manager.after_start = block_start
    with ThreadPoolExecutor(max_workers=2) as pool:
        starting = pool.submit(env.broker.start, env.request)
        assert entered.wait(2)

        def stop_when_unlocked():
            stopping.set()
            with env.journal.transaction(env.request.generation.generation_id) as transaction:
                admission = transaction.document.admission()
            return env.broker.stop(_stop_request(env, admission))

        stop = pool.submit(stop_when_unlocked)
        assert stopping.wait(2)
        with pytest.raises(TimeoutError):
            stop.result(timeout=0.01)
        assert env.manager.events == ["slice", "start"]
        resume.set()
        starting.result(timeout=2)
        assert stop.result(timeout=2).manifest.state == "CLOSED"


@pytest.mark.parametrize(
    "bad", [{"argv": ["sh"]}, {"env": {"TOKEN": "forbidden"}}, {"properties": {}}]
)
def test_start_request_has_no_caller_execution_bag(broker_env, bad):
    with pytest.raises(ValidationError):
        StubStartRequest.model_validate({**broker_env.request.model_dump(), **bad})


def test_backend_absence_and_unsafe_image_are_unlaunchable(broker_env):
    env = broker_env
    env.broker.manager = None
    with pytest.raises(SupervisorUnavailable, match="unavailable"):
        env.broker.start(env.request)
    env.broker.manager = env.manager
    target = Path(env.profile.root_directory) / "escape-link"
    target.symlink_to("/")
    with pytest.raises(SupervisorUnavailable, match="unsafe files"):
        env.broker.start(env.request)
    assert env.manager.events == []


@pytest.mark.parametrize("unsafe", ["group", "extra_groups", "capability", "missing_gate"])
def test_profile_credentials_and_image_checks_fail_before_manager(broker_env, monkeypatch, unsafe):
    env = broker_env
    if unsafe == "group":
        monkeypatch.setattr(
            bgr_broker.grp, "getgrgid", lambda gid: SimpleNamespace(gr_name="docker")
        )
    elif unsafe == "extra_groups":
        monkeypatch.setattr(env.operations, "getgrouplist", lambda name, gid: [gid, 27])
    elif unsafe == "capability":
        monkeypatch.setattr(
            env.operations, "listxattr", lambda *args, **kwargs: ["security.capability"]
        )
    else:
        (Path(env.profile.root_directory) / env.profile.gate.lstrip("/")).unlink()
    with pytest.raises((SupervisorUnavailable, OSError)):
        env.broker.start(env.request)
    assert env.manager.events == []


@pytest.mark.parametrize("identity", [{"worker_uid": 0}, {"worker_uid": 1000}, {"worker_gid": 0}])
def test_profile_rejects_root_and_host_identity(broker_env, identity):
    with pytest.raises(ValidationError):
        AdminStubProfile.model_validate({**broker_env.profile.model_dump(), **identity})


class _PeerSocket:
    family = socket.AF_UNIX
    type = socket.SOCK_SEQPACKET

    def __init__(self, channel, uid=0):
        self.channel, self.uid = channel, uid

    def getsockopt(self, *args):
        return struct.pack("3i", 123, self.uid, 0)

    def settimeout(self, value):
        self.channel.settimeout(value)

    def recv(self, count):
        return self.channel.recv(count)

    def close(self):
        self.channel.close()


@pytest.mark.parametrize(
    "failure",
    [None, "eof", "timeout", "stale", "peer", "oversize", "duplicate", "truncated", "replay"],
)
def test_gate_socket_release_is_exact_bounded_and_one_shot(broker_env, failure):
    env = broker_env
    env.broker.start(env.request)
    launch = UnitLaunch.model_validate(env.read()["unit_launch"])
    sender, receiver = socket.socketpair(type=socket.SOCK_SEQPACKET)
    try:
        wire = launch.model_dump_json().encode() + b"\n"
        if failure == "stale":
            wire = (
                launch.model_copy(update={"supervisor_incarnation": "f" * 32})
                .model_dump_json()
                .encode()
                + b"\n"
            )
        elif failure == "oversize":
            wire = b"x" * 4097
        elif failure == "duplicate":
            wire += wire
        elif failure == "truncated":
            wire = wire[:-1]
        if failure == "eof":
            sender.close()
        elif failure != "timeout":
            sender.sendall(wire)
        peer = _PeerSocket(receiver, uid=1000 if failure == "peer" else 0)
        if failure in (None, "replay"):
            bgr_gate.await_release(peer, launch, timeout_s=0.02)
            if failure == "replay":
                with pytest.raises(SupervisorUnavailable):
                    bgr_gate.await_release(peer, launch, timeout_s=0.02)
        else:
            with pytest.raises(SupervisorUnavailable):
                bgr_gate.await_release(peer, launch, timeout_s=0.02)
        assert receiver.fileno() == -1
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("timeout_s", [0, -1, 31])
def test_invalid_gate_deadline_still_closes_channel(broker_env, timeout_s):
    env = broker_env
    env.broker.start(env.request)
    launch = UnitLaunch.model_validate(env.read()["unit_launch"])
    sender, receiver = socket.socketpair(type=socket.SOCK_SEQPACKET)
    try:
        with pytest.raises(SupervisorUnavailable, match="deadline"):
            bgr_gate.await_release(receiver, launch, timeout_s=timeout_s)
        assert receiver.fileno() == -1
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("unsafe", [None, "uid", "gid", "groups", "profile"])
def test_stub_gate_executes_only_fixed_workload_with_empty_environment(
    broker_env, monkeypatch, unsafe
):
    env = broker_env
    env.broker.start(env.request)
    launch = UnitLaunch.model_validate(env.read()["unit_launch"])
    sender, receiver = socket.socketpair(type=socket.SOCK_SEQPACKET)
    calls = []

    def release(channel, expected):
        calls.append("released")
        channel.close()

    def execute(path, argv, environment):
        calls.append((path, argv, environment))

    monkeypatch.setattr(bgr_gate, "await_release", release)
    monkeypatch.setattr(bgr_gate, "_verify_kernel_constraints", lambda channel: None)
    monkeypatch.setattr(
        bgr_gate,
        "os",
        SimpleNamespace(
            getresuid=lambda: (0 if unsafe == "uid" else env.profile.worker_uid,) * 3,
            getresgid=lambda: (0 if unsafe == "gid" else env.profile.worker_gid,) * 3,
            getgroups=lambda: [27] if unsafe == "groups" else [env.profile.worker_gid],
            execve=execute,
        ),
    )
    if unsafe == "profile":
        launch = launch.model_copy(update={"profile_sha256": "0" * 64})
    try:
        if unsafe is None:
            bgr_gate.run_stub_gate(receiver, launch, env.profile)
            assert calls == ["released", (env.profile.workload, [env.profile.workload], {})]
        else:
            with pytest.raises(SupervisorUnavailable):
                bgr_gate.run_stub_gate(receiver, launch, env.profile)
            assert calls == []
        assert receiver.fileno() == -1
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize(
    "unsafe", [None, "privs", "caps", "stdio", "descriptor", "missing_status"]
)
def test_gate_independently_checks_kernel_privileges_and_inherited_descriptors(
    monkeypatch, unsafe
):
    rows = "NoNewPrivs: 1\n" + "\n".join(
        f"{field}: 00000000" for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    )
    if unsafe == "privs":
        rows = rows.replace("NoNewPrivs: 1", "NoNewPrivs: 0")
    elif unsafe == "caps":
        rows = rows.replace("CapBnd: 00000000", "CapBnd: 00000001")
    elif unsafe == "missing_status":
        rows = ""
    monkeypatch.setattr(bgr_gate, "Path", lambda path: SimpleNamespace(read_text=lambda: rows))

    def metadata(descriptor):
        if descriptor > 2 and unsafe != "descriptor":
            raise OSError(errno.EBADF, "Descriptor was the transient procfs directory handle")
        return SimpleNamespace(
            st_mode=stat.S_IFREG if unsafe == "stdio" else stat.S_IFCHR,
            st_rdev=os.makedev(1, 3),
        )

    monkeypatch.setattr(
        bgr_gate,
        "os",
        SimpleNamespace(
            fstat=metadata, makedev=os.makedev, listdir=lambda path: ["0", "1", "2", "4", "5"]
        ),
    )
    channel = SimpleNamespace(fileno=lambda: 4)
    if unsafe is None:
        bgr_gate._verify_kernel_constraints(channel)
    else:
        with pytest.raises(SupervisorUnavailable):
            bgr_gate._verify_kernel_constraints(channel)
