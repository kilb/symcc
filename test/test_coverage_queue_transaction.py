# RUN: python3 -m pytest -q %s

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

import coverage_queue_transaction as transaction_module  # noqa: E402
from coverage_queue_transaction import CoverageQueueTransactionStore  # noqa: E402


def test_prepared_transaction_is_invisible_and_conservatively_recovered():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        queue = root / "queue"
        store = CoverageQueueTransactionStore(str(root), str(queue))

        store.prepare([(b"first", "000001"), (b"second", "000002")])

        assert list(queue.iterdir()) == []
        recovered = store.recover(0)
        assert recovered.conservatively_recovered == 2
        assert recovered.next_queue_id == 2
        assert [path.name for path in sorted(queue.iterdir())] == [
            "id:000000,src:000001",
            "id:000001,src:000002",
        ]
        assert [path.read_bytes() for path in sorted(queue.iterdir())] == [
            b"first",
            b"second",
        ]


def test_decided_transaction_promotes_only_global_winners():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        queue = root / "queue"
        store = CoverageQueueTransactionStore(str(root), str(queue))
        transaction_id = store.prepare([
            (b"lost", "000001"),
            (b"winner", "000002"),
        ])
        store.decide(transaction_id, [0, 3], first_queue_id=4)

        committed = store.commit(transaction_id)

        assert committed.destinations[0] is None
        assert committed.destinations[1] == str(queue / "id:000004,src:000002")
        assert committed.next_queue_id == 5
        assert committed.redundant == 1
        assert [path.read_bytes() for path in queue.iterdir()] == [b"winner"]


def test_variable_width_afl_ids_round_trip_without_truncation():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        queue = root / "queue"
        store = CoverageQueueTransactionStore(str(root), str(queue))
        transaction_id = store.prepare([(b"winner", "1000000")])
        store.decide(transaction_id, [1], first_queue_id=1_000_000)

        committed = store.commit(transaction_id)

        assert committed.next_queue_id == 1_000_001
        assert committed.destinations == (
            str(queue / "id:1000000,src:1000000"),
        )


def test_recovery_finishes_partially_promoted_decision_idempotently():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        queue = root / "queue"
        store = CoverageQueueTransactionStore(str(root), str(queue))
        transaction_id = store.prepare([
            (b"first", "000001"),
            (b"second", "000002"),
        ])
        store.decide(transaction_id, [1, 1], first_queue_id=0)
        manifest = store._read_manifest(transaction_id)
        first_stage = store._stage_path(manifest["records"][0]["stage"])
        os.replace(first_stage, queue / "id:000000,src:000001")

        recovered = store.recover(0)

        assert recovered.next_queue_id == 2
        assert [path.read_bytes() for path in sorted(queue.iterdir())] == [
            b"first",
            b"second",
        ]


def test_recovery_never_overwrites_an_existing_queue_entry():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        queue = root / "queue"
        store = CoverageQueueTransactionStore(str(root), str(queue))
        transaction_id = store.prepare([(b"candidate", "000001")])
        store.decide(transaction_id, [1], first_queue_id=0)
        destination = queue / "id:000000,src:000001"
        destination.write_bytes(b"unrelated")

        with pytest.raises(FileExistsError, match="overwrite another testcase"):
            store.recover(0)

        assert destination.read_bytes() == b"unrelated"
        assert Path(store._manifest_path(transaction_id)).is_file()


def test_recovery_finishes_decisions_before_allocating_prepared_ids():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        queue = root / "queue"
        store = CoverageQueueTransactionStore(str(root), str(queue))
        prepared = store.prepare([(b"ambiguous", "000003")])
        decided = store.prepare([(b"decided", "000004")])
        store.decide(decided, [1], first_queue_id=0)

        recovered = store.recover(0)

        assert recovered.next_queue_id == 2
        assert (queue / "id:000000,src:000004").read_bytes() == b"decided"
        assert (queue / "id:000001,src:000003").read_bytes() == b"ambiguous"
        assert not Path(store._manifest_path(prepared)).exists()


def test_manifest_rejects_duplicate_keys_even_when_digest_still_matches():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CoverageQueueTransactionStore(str(root), str(root / "queue"))
        transaction_id = store.prepare([(b"candidate", "000001")])
        manifest_path = Path(store._manifest_path(transaction_id))
        original = manifest_path.read_bytes()
        decoded = json.loads(original)
        assert decoded["schema"] == 1
        # The repeated key has the same value, so ordinary json.loads yields
        # exactly the digest-authorized object and would otherwise accept it.
        manifest_path.write_bytes(b'{"schema":1,' + original[1:])

        with pytest.raises(ValueError, match="transaction manifest"):
            store.recover(0)


def test_recovery_removes_unreferenced_stage_and_atomic_temporary_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CoverageQueueTransactionStore(str(root), str(root / "queue"))
        transaction_id = store.prepare([(b"orphan", "000001")])
        manifest = store._read_manifest(transaction_id)
        stage = Path(store._stage_path(manifest["records"][0]["stage"]))
        Path(store._manifest_path(transaction_id)).unlink()
        temporary = Path(store.root) / ".coverage-queue-crashed.tmp"
        temporary.write_bytes(b"partial")

        recovered = store.recover(0)

        assert recovered.destinations == ()
        assert not stage.exists()
        assert not temporary.exists()


def test_prepare_fsync_failure_removes_visible_manifest_and_stages(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CoverageQueueTransactionStore(str(root), str(root / "queue"))
        original_sync = transaction_module._sync_directory
        calls = 0

        def fail_manifest_sync(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("manifest directory fsync failed")
            return original_sync(path)

        monkeypatch.setattr(transaction_module, "_sync_directory", fail_manifest_sync)
        with pytest.raises(OSError, match="manifest directory fsync failed"):
            store.prepare([(b"candidate", "000001")])

        assert list(Path(store.root).iterdir()) == []
        recovered = store.recover(0)
        assert recovered.destinations == ()
        assert recovered.next_queue_id == 0
