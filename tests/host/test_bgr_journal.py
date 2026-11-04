"""Real file/lock tests with root ownership injected only in test setup."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest

from omnigent.host import bgr_journal
from omnigent.host.bgr_cgroup import CgroupEmptyProof
from omnigent.host.bgr_journal import AdmissionClaim, SupervisorJournal
from omnigent.host.bgr_supervisor import SupervisorUnavailable
from tests.host.test_bgr_supervisor import request_stop as request_stop


class _TestOs:
    def __init__(self):
        self.syncs = []
        self.fail_sync = False

    def __getattr__(self, name):
        return getattr(os, name)

    def geteuid(self):
        return 0

    def fsync(self, fd):
        if self.fail_sync:
            raise OSError("Injected fsync failure")
        self.syncs.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        os.fsync(fd)


@pytest.fixture
def journal_env(monkeypatch, tmp_path):
    root = tmp_path / "protected-supervisor-journal"
    root.mkdir(mode=0o700)
    operations = _TestOs()
    monkeypatch.setattr(bgr_journal, "os", operations)
    monkeypatch.setattr(bgr_journal, "_open_directory", lambda path: os.open(path, os.O_RDONLY))
    monkeypatch.setattr(bgr_journal, "_root_protected", lambda metadata: None)
    return root, operations


def _reserve(journal, request):
    admission = request.admission
    return journal.create(
        admission.generation,
        domain_id=admission.domain.domain_id,
        manifest_id=admission.manifest_id,
        root_claim_id=admission.root_claim_id,
        launch_sha256=admission.launch_sha256,
    )


def _admit(journal, request):
    _reserve(journal, request)
    with journal.transaction(request.admission.generation.generation_id) as transaction:
        transaction.transition(request.admission.root_claim_id, "STARTING")
        assert transaction.pin(request.admission.domain) == request.admission
        transaction.transition(request.admission.root_claim_id, "SPAWNED_UNBOUND")
        transaction.reserve(
            AdmissionClaim(
                claim_id="f" * 32,
                admission_sequence=transaction.document.sequence + 1,
                role="helper",
            )
        )


def _proof(request):
    return CgroupEmptyProof(
        boot_id=request.admission.generation.boot_id,
        domain=request.admission.domain,
        observed_monotonic=monotonic(),
    )


def test_journal_fsyncs_before_admission_and_seals_all_claims(journal_env, request_stop):
    root, operations = journal_env
    journal = SupervisorJournal(root)
    try:
        _admit(journal, request_stop)
        assert operations.syncs == [False, True] * 6
        generation_id = request_stop.admission.generation.generation_id
        with journal.transaction(generation_id) as transaction:
            transaction.close_admissions(request_stop)
            with pytest.raises(SupervisorUnavailable, match="closed"):
                transaction.transition(request_stop.admission.root_claim_id, "BOUND")
            with pytest.raises(SupervisorUnavailable, match="explicitly cancelled"):
                transaction.seal(
                    request_stop, _proof(request_stop), cancelled_before_exec=frozenset()
                )
            receipt = transaction.seal(
                request_stop, _proof(request_stop), cancelled_before_exec=frozenset({"f" * 32})
            )
            assert len(receipt.manifest.claims) == 2
            assert receipt.manifest.claims[1].session_id is None
            assert receipt.manifest.claims[1].disposition == "cancelled_before_exec"
        assert operations.syncs == [False, True] * 9
        with pytest.raises(SupervisorUnavailable, match="ended"):
            transaction.close_admissions(request_stop)
        with journal.transaction(generation_id) as retried:
            assert (
                retried.seal(request_stop, _proof(request_stop), cancelled_before_exec=frozenset())
                == receipt
            )
    finally:
        journal.close()


def test_supervisor_restart_is_stop_only_and_preserves_receipt(journal_env, request_stop):
    root, _ = journal_env
    first = SupervisorJournal(root)
    _admit(first, request_stop)
    first.close()
    restarted = SupervisorJournal(root)
    generation_id = request_stop.admission.generation.generation_id
    try:
        _reserve(restarted, request_stop)
        with restarted.transaction(generation_id) as transaction:
            with pytest.raises(SupervisorUnavailable, match="reconciliation"):
                transaction.transition(request_stop.admission.root_claim_id, "BOUND")
            transaction.close_admissions(request_stop)
            receipt = transaction.seal(
                request_stop, _proof(request_stop), cancelled_before_exec=frozenset({"f" * 32})
            )
    finally:
        restarted.close()
    cold = SupervisorJournal(root)
    try:
        assert _reserve(cold, request_stop).receipt == receipt
        with cold.transaction(generation_id) as transaction:
            assert transaction.close_admissions(request_stop).receipt == receipt
            with pytest.raises(SupervisorUnavailable):
                transaction.close_admissions(
                    request_stop.model_copy(update={"recovery_id": "0" * 32})
                )
    finally:
        cold.close()


def test_missing_or_corrupt_journal_is_unknown_not_empty(journal_env, request_stop):
    root, _ = journal_env
    journal = SupervisorJournal(root)
    generation_id = request_stop.admission.generation.generation_id
    try:
        with pytest.raises(FileNotFoundError):
            with journal.transaction(generation_id):
                pytest.fail("Missing journal must not authorize a stop")
        _reserve(journal, request_stop)
        record = root / f"{generation_id}.json"
        record.write_bytes(b'{"incomplete":')
        with pytest.raises(ValueError):
            with journal.transaction(generation_id):
                pytest.fail("Corruption must not authorize a stop")
    finally:
        journal.close()


def test_durable_barrier_precedes_kernel_proof(journal_env, request_stop):
    root, _ = journal_env
    journal = SupervisorJournal(root)
    try:
        _admit(journal, request_stop)
        with journal.transaction(request_stop.admission.generation.generation_id) as transaction:
            stale_proof = _proof(request_stop)
            with pytest.raises(SupervisorUnavailable, match="already be closed"):
                transaction.seal(
                    request_stop, stale_proof, cancelled_before_exec=frozenset({"f" * 32})
                )
            transaction.close_admissions(request_stop)
            with pytest.raises(SupervisorUnavailable, match="incarnation"):
                transaction.seal(
                    request_stop, stale_proof, cancelled_before_exec=frozenset({"f" * 32})
                )
    finally:
        journal.close()


def test_failed_fsync_cannot_release_a_launch(journal_env, request_stop):
    root, operations = journal_env
    journal = SupervisorJournal(root)
    try:
        operations.fail_sync = True
        with pytest.raises(OSError, match="fsync"):
            _reserve(journal, request_stop)
        assert not list(root.glob("*.json"))
        assert not list(root.glob("*.tmp"))
    finally:
        journal.close()


def test_no_generation_identity_or_claim_reuse(journal_env, request_stop):
    root, _ = journal_env
    journal = SupervisorJournal(root)
    try:
        _admit(journal, request_stop)
        changed = request_stop.model_copy(
            update={
                "admission": request_stop.admission.model_copy(update={"launch_sha256": "0" * 64})
            }
        )
        with pytest.raises(SupervisorUnavailable, match="already used"):
            _reserve(journal, changed)
        with journal.transaction(request_stop.admission.generation.generation_id) as transaction:
            with pytest.raises(SupervisorUnavailable, match="different arguments"):
                transaction.reserve(
                    AdmissionClaim(claim_id="f" * 32, admission_sequence=99, role="grunt")
                )
            with pytest.raises(SupervisorUnavailable, match="domain"):
                transaction.pin(request_stop.admission.domain.model_copy(update={"inode": 99}))
    finally:
        journal.close()


def test_closed_receipt_cannot_omit_admissions(journal_env, request_stop):
    root, _ = journal_env
    journal = SupervisorJournal(root)
    try:
        _admit(journal, request_stop)
        with journal.transaction(request_stop.admission.generation.generation_id) as transaction:
            transaction.close_admissions(request_stop)
            transaction.seal(
                request_stop, _proof(request_stop), cancelled_before_exec=frozenset({"f" * 32})
            )
        path = root / f"{request_stop.admission.generation.generation_id}.json"
        document = json.loads(path.read_text())
        document["claims"] = document["claims"][:1]
        path.write_text(json.dumps(document))
        with pytest.raises(ValueError):
            with journal.transaction(request_stop.admission.generation.generation_id):
                pytest.fail("Conflicting receipt must not be accepted")
    finally:
        journal.close()


def test_stop_barrier_serializes_against_concurrent_admission(journal_env, request_stop):
    root, _ = journal_env
    journal = SupervisorJournal(root)
    attempted = Event()
    generation_id = request_stop.admission.generation.generation_id

    def reserve_concurrently():
        attempted.set()
        with journal.transaction(generation_id) as transaction:
            transaction.reserve(
                AdmissionClaim(
                    claim_id="0" * 32,
                    admission_sequence=transaction.document.sequence + 1,
                    role="helper",
                )
            )

    try:
        _admit(journal, request_stop)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with journal.transaction(generation_id) as transaction:
                pending = executor.submit(reserve_concurrently)
                assert attempted.wait(timeout=2)
                transaction.close_admissions(request_stop)
            with pytest.raises(SupervisorUnavailable, match="closed"):
                pending.result(timeout=2)
        with journal.transaction(generation_id) as transaction:
            assert len(transaction.document.claims) == 2
    finally:
        journal.close()


def test_admission_overflow_never_evicts_an_unresolved_claim(journal_env, request_stop):
    root, _ = journal_env
    journal = SupervisorJournal(root)
    generation_id = request_stop.admission.generation.generation_id
    try:
        _admit(journal, request_stop)
        with journal.transaction(generation_id) as transaction:
            claims = [
                transaction.document.claims[0],
                *(
                    AdmissionClaim(
                        claim_id=f"{index:032x}",
                        admission_sequence=index + 2,
                        role="helper",
                    )
                    for index in range(1023)
                ),
            ]
            transaction._save(claims=claims, sequence=1024)
            with pytest.raises(SupervisorUnavailable, match="capacity"):
                transaction.reserve(
                    AdmissionClaim(claim_id="e" * 32, admission_sequence=1025, role="helper")
                )
        with journal.transaction(generation_id) as transaction:
            assert transaction.document.claims == tuple(claims)
    finally:
        journal.close()


def test_retry_requires_durable_storage_even_after_lost_ack(journal_env, request_stop):
    root, operations = journal_env
    journal = SupervisorJournal(root)
    try:
        _admit(journal, request_stop)
        operations.fail_sync = True
        with pytest.raises(OSError, match="fsync"):
            _reserve(journal, request_stop)
        operations.fail_sync = False
        assert _reserve(journal, request_stop).generation == request_stop.admission.generation
    finally:
        journal.close()
