#!/usr/bin/env python3
# RUN: python3 %s

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qfbv_artifact_lifecycle import (  # noqa: E402
    ArtifactJobLease,
    ArtifactLeaseHeartbeat,
    ArtifactLifecycleError,
    ArtifactLifecycleRegistry,
    ArtifactRef,
)
from cross_worker_context import CrossWorkerContextStore  # noqa: E402
from qfbv_lemma_exchange import (  # noqa: E402
    LemmaExchangeError,
    QfbvLemmaStore,
)
from qfbv_proof_receipt import (  # noqa: E402
    ProofVerificationError,
    QfbvProofStore,
)
from symcc_query_service import main as query_service_main  # noqa: E402


def _ref(kind: str, name: str) -> ArtifactRef:
    return ArtifactRef(kind, hashlib.sha256(name.encode("ascii")).hexdigest())


class ArtifactLifecycleRegistryTest(unittest.TestCase):
    def test_artifact_kind_schema_migrates_only_the_exact_legacy_set(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            with registry.maintenance():
                with registry._connect() as database:
                    database.execute(
                        "UPDATE store_metadata SET value = ? "
                        "WHERE key = 'artifact_kinds'",
                        ("context,lemma,proof,receipt",),
                    )
            migrated = ArtifactLifecycleRegistry(directory)
            with migrated._connect() as database:
                value = database.execute(
                    "SELECT value FROM store_metadata "
                    "WHERE key = 'artifact_kinds'"
                ).fetchone()[0]
            self.assertEqual(
                value,
                "context,core,lemma,partition,partition-execution,proof,receipt,"
                "sat-proof",
            )
            with migrated.maintenance():
                with migrated._connect() as database:
                    database.execute(
                        "UPDATE store_metadata SET value = ? "
                        "WHERE key = 'artifact_kinds'",
                        ("context,core,lemma,proof,receipt,sat-proof",),
                    )
            upgraded = ArtifactLifecycleRegistry(directory)
            with upgraded._connect() as database:
                value = database.execute(
                    "SELECT value FROM store_metadata "
                    "WHERE key = 'artifact_kinds'"
                ).fetchone()[0]
            self.assertEqual(
                value,
                "context,core,lemma,partition,partition-execution,proof,receipt,"
                "sat-proof",
            )
            with upgraded.maintenance():
                with upgraded._connect() as database:
                    database.execute(
                        "UPDATE store_metadata SET value = ? "
                        "WHERE key = 'artifact_kinds'",
                        ("context,proof",),
                    )
            with self.assertRaisesRegex(
                ArtifactLifecycleError, "metadata mismatch"
            ):
                ArtifactLifecycleRegistry(directory)

    def test_active_job_marks_transitive_graph_then_release_collects_dependents_first(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            parent = _ref("context", "parent")
            child = _ref("context", "child")
            proof = _ref("proof", "proof")
            receipt = _ref("receipt", "receipt")
            core = _ref("core", "core")
            lemma = _ref("lemma", "lemma")
            registry.record_artifact(parent, encoded_bytes=10, now=1.0)
            registry.record_artifact(
                child, encoded_bytes=11, edges=(parent,), now=1.0
            )
            registry.record_artifact(proof, encoded_bytes=12, now=1.0)
            registry.record_artifact(
                receipt, encoded_bytes=13, edges=(proof,), now=1.0
            )
            registry.record_artifact(
                core, encoded_bytes=14, edges=(receipt,), now=1.0
            )
            registry.record_artifact(
                lemma,
                encoded_bytes=15,
                edges=(child, core),
                now=1.0,
            )
            lease = registry.start_job(
                "campaign-a", "worker-0", lease_seconds=100.0, now=100.0
            )
            registry.touch((lemma,), lease=lease, now=100.0)

            deleted_calls: list[ArtifactRef] = []

            def delete(kind: str, digest: str, size: int) -> int:
                deleted_calls.append(ArtifactRef(kind, digest))
                return size

            live = registry.collect(
                delete,
                grace_seconds=0.0,
                max_objects=10,
                max_bytes=1_000,
                time_budget_ms=1_000,
                now=101.0,
            )
            self.assertEqual(live.deleted, ())
            self.assertEqual(live.protected, 6)

            self.assertTrue(registry.release_job(lease, now=102.0))
            dead = registry.collect(
                delete,
                grace_seconds=0.0,
                max_objects=10,
                max_bytes=1_000,
                time_budget_ms=1_000,
                now=103.0,
            )
            self.assertEqual(
                set(dead.deleted), {parent, child, proof, receipt, core, lemma}
            )
            positions = {value: index for index, value in enumerate(dead.deleted)}
            self.assertLess(positions[lemma], positions[child])
            self.assertLess(positions[lemma], positions[core])
            self.assertLess(positions[core], positions[receipt])
            self.assertLess(positions[child], positions[parent])
            self.assertLess(positions[receipt], positions[proof])
            self.assertEqual(dead.deleted_bytes, 75)
            self.assertEqual(registry.stats(now=103.0)["artifacts"], 0)

    def test_grace_period_is_a_root_and_expires_at_the_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            artifact = _ref("proof", "recent")
            registry.record_artifact(artifact, encoded_bytes=9, now=95.0)
            calls: list[ArtifactRef] = []

            def delete(kind: str, digest: str, size: int) -> int:
                calls.append(ArtifactRef(kind, digest))
                return size

            recent = registry.collect(
                delete,
                grace_seconds=10.0,
                max_objects=1,
                max_bytes=10,
                time_budget_ms=1_000,
                now=100.0,
            )
            self.assertEqual(recent.deleted, ())
            expired = registry.collect(
                delete,
                grace_seconds=10.0,
                max_objects=1,
                max_bytes=10,
                time_budget_ms=1_000,
                now=106.0,
            )
            self.assertEqual(expired.deleted, (artifact,))
            self.assertEqual(calls, [artifact])

    def test_fencing_rejects_prior_job_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            first = registry.start_job(
                "same-job", "worker-a", lease_seconds=60.0, now=10.0
            )
            second = registry.start_job(
                "same-job", "worker-b", lease_seconds=60.0, now=11.0
            )
            artifact = _ref("context", "fenced")
            registry.record_artifact(artifact, encoded_bytes=1, now=1.0)
            with self.assertRaisesRegex(ArtifactLifecycleError, "stale or expired"):
                registry.touch((artifact,), lease=first, now=12.0)
            registry.touch((artifact,), lease=second, now=12.0)
            self.assertEqual(registry.stats(now=12.0)["active_references"], 1)

    def test_failed_release_cannot_remove_active_job_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            artifact = _ref("context", "release-owner")
            lease = registry.start_job(
                "release-job", "owner", lease_seconds=100.0, now=10.0
            )
            registry.record_artifact(
                artifact, encoded_bytes=1, lease=lease, now=10.0
            )
            forged = ArtifactJobLease(
                lease.job_id,
                "other-owner",
                lease.generation,
                lease.lease_until,
            )
            self.assertFalse(registry.release_job(forged, now=11.0))
            self.assertEqual(registry.stats(now=11.0)["active_references"], 1)
            result = registry.collect(
                lambda _kind, _digest, size: size,
                grace_seconds=0.0,
                max_objects=1,
                max_bytes=1,
                time_budget_ms=1_000,
                now=12.0,
            )
            self.assertEqual(result.deleted, ())
            self.assertEqual(result.protected, 1)

    def test_nonfinite_lifecycle_times_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            with self.assertRaisesRegex(
                ArtifactLifecycleError, "wall time must be"
            ):
                registry.start_job(
                    "nonfinite", "owner", lease_seconds=1.0, now=float("nan")
                )
            with self.assertRaisesRegex(
                ArtifactLifecycleError, "wall time must be"
            ):
                registry.stats(now=float("inf"))

    def test_expired_job_reference_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            artifact = _ref("lemma", "expired")
            lease = registry.start_job(
                "short-job", "worker", lease_seconds=1.0, now=10.0
            )
            registry.record_artifact(
                artifact,
                encoded_bytes=7,
                lease=lease,
                now=10.0,
            )
            result = registry.collect(
                lambda _kind, _digest, size: size,
                grace_seconds=0.0,
                max_objects=1,
                max_bytes=7,
                time_budget_ms=1_000,
                now=12.0,
            )
            self.assertEqual(result.deleted, (artifact,))
            self.assertEqual(result.expired_jobs, 1)

    def test_object_and_byte_budgets_are_hard_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            first = _ref("proof", "first")
            second = _ref("proof", "second")
            registry.record_artifact(first, encoded_bytes=5, now=1.0)
            registry.record_artifact(second, encoded_bytes=6, now=2.0)
            one = registry.collect(
                lambda _kind, _digest, size: size,
                grace_seconds=0.0,
                max_objects=1,
                max_bytes=100,
                time_budget_ms=1_000,
                now=10.0,
            )
            self.assertEqual(len(one.deleted), 1)
            self.assertEqual(one.stop_reason, "object_budget")
            blocked = registry.collect(
                lambda _kind, _digest, size: size,
                grace_seconds=0.0,
                max_objects=1,
                max_bytes=1,
                time_budget_ms=1_000,
                now=10.0,
            )
            self.assertEqual(blocked.deleted, ())
            self.assertEqual(blocked.stop_reason, "byte_budget")

    def test_unreachable_dependency_cycle_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            left = _ref("context", "left")
            right = _ref("context", "right")
            registry.record_artifact(left, encoded_bytes=1, edges=(right,), now=1.0)
            registry.record_artifact(right, encoded_bytes=1, edges=(left,), now=1.0)
            result = registry.collect(
                lambda _kind, _digest, size: size,
                grace_seconds=0.0,
                max_objects=2,
                max_bytes=2,
                time_budget_ms=1_000,
                now=10.0,
            )
            self.assertEqual(result.deleted, ())
            self.assertEqual(result.stop_reason, "dependency_cycle")

    def test_incomplete_dependency_graph_fails_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            source = _ref("lemma", "incomplete-source")
            missing = _ref("receipt", "missing-target")
            registry.record_artifact(
                source, encoded_bytes=7, edges=(missing,), now=1.0
            )
            calls = 0

            def delete(_kind: str, _digest: str, size: int) -> int:
                nonlocal calls
                calls += 1
                return size

            with self.assertRaisesRegex(
                ArtifactLifecycleError, "dependency graph is incomplete"
            ):
                registry.collect(
                    delete,
                    grace_seconds=0.0,
                    max_objects=1,
                    max_bytes=100,
                    time_budget_ms=1_000,
                    now=10.0,
                )
            self.assertEqual(calls, 0)
            self.assertEqual(registry.stats(now=10.0)["artifacts"], 1)

    def test_job_identity_capacity_is_bounded_without_reusing_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory, max_jobs=2)
            first = registry.start_job(
                "job-a", "owner", lease_seconds=1.0, now=1.0
            )
            self.assertTrue(registry.release_job(first, now=1.5))
            second = registry.start_job(
                "job-b", "owner", lease_seconds=1.0, now=2.0
            )
            self.assertTrue(registry.release_job(second, now=2.5))
            with self.assertRaisesRegex(
                ArtifactLifecycleError, "job ID capacity is exhausted"
            ):
                registry.start_job(
                    "job-c", "owner", lease_seconds=1.0, now=3.0
                )
            renewed = registry.start_job(
                "job-a", "owner", lease_seconds=1.0, now=3.0
            )
            self.assertEqual(renewed.generation, 2)
            self.assertEqual(registry.stats(now=3.0)["jobs"], 2)
            with self.assertRaisesRegex(
                ArtifactLifecycleError, "metadata mismatch for max_jobs"
            ):
                ArtifactLifecycleRegistry(directory, max_jobs=3)

    def test_proof_inventory_requires_bidirectional_result_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ArtifactLifecycleRegistry(root / "lifecycle")
            proofs = QfbvProofStore(root / "proofs", lifecycle=registry)
            proof_digest = hashlib.sha256(b"proof-row").hexdigest()
            receipt_digest = hashlib.sha256(b"receipt-row").hexdigest()
            result_key = hashlib.sha256(b"result-row").hexdigest()
            with proofs._connect() as database:
                database.execute(
                    "INSERT INTO proofs VALUES(?, ?, ?, ?, ?)",
                    (proof_digest, 1, "proofs/missing", 1.0, 1.0),
                )
                database.execute(
                    "INSERT INTO receipts VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt_digest,
                        result_key,
                        proof_digest,
                        1,
                        "receipts/missing",
                        1.0,
                        1.0,
                    ),
                )
            with self.assertRaisesRegex(
                ProofVerificationError, "inventory graph is incomplete"
            ):
                proofs.synchronize_lifecycle(max_entries=2)

    def test_collector_waits_for_inflight_shared_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            artifact = _ref("receipt", "reader")
            registry.record_artifact(artifact, encoded_bytes=1, now=1.0)
            entered = threading.Event()
            release = threading.Event()

            def reader() -> None:
                with registry.operation():
                    entered.set()
                    self.assertTrue(release.wait(timeout=2.0))

            thread = threading.Thread(target=reader)
            thread.start()
            self.assertTrue(entered.wait(timeout=1.0))
            timer = threading.Timer(0.1, release.set)
            timer.start()
            started = time.monotonic()
            result = registry.collect(
                lambda _kind, _digest, size: size,
                grace_seconds=0.0,
                max_objects=1,
                max_bytes=1,
                time_budget_ms=1_000,
                now=10.0,
            )
            elapsed = time.monotonic() - started
            thread.join(timeout=1.0)
            timer.cancel()
            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(elapsed, 0.08)
            self.assertEqual(result.deleted, (artifact,))

    def test_collection_lock_deadline_fails_without_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            artifact = _ref("proof", "lock-timeout")
            registry.record_artifact(artifact, encoded_bytes=1, now=1.0)
            entered = threading.Event()
            release = threading.Event()

            def reader() -> None:
                with registry.operation():
                    entered.set()
                    release.wait(timeout=2.0)

            thread = threading.Thread(target=reader)
            thread.start()
            self.assertTrue(entered.wait(timeout=1.0))
            try:
                with self.assertRaisesRegex(
                    ArtifactLifecycleError, "lock timeout"
                ):
                    registry.collect(
                        lambda _kind, _digest, size: size,
                        grace_seconds=0.0,
                        max_objects=1,
                        max_bytes=1,
                        time_budget_ms=20,
                        now=10.0,
                    )
                self.assertEqual(registry.stats(now=10.0)["artifacts"], 1)
            finally:
                release.set()
                thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())

    def test_artifact_content_identity_includes_size_and_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            source = _ref("lemma", "stable")
            target = _ref("receipt", "dependency")
            registry.record_artifact(
                source, encoded_bytes=5, edges=(target,), now=1.0
            )
            with self.assertRaisesRegex(ArtifactLifecycleError, "size identity"):
                registry.record_artifact(
                    source, encoded_bytes=6, edges=(target,), now=2.0
                )
            with self.assertRaisesRegex(ArtifactLifecycleError, "dependency identity"):
                registry.record_artifact(source, encoded_bytes=5, now=2.0)

    def test_background_heartbeat_keeps_job_root_live_and_releases_it(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ArtifactLifecycleRegistry(directory)
            artifact = _ref("context", "heartbeat")
            lease = registry.start_job(
                "heartbeat-job", "worker", lease_seconds=0.2
            )
            registry.record_artifact(
                artifact, encoded_bytes=1, lease=lease
            )
            heartbeat = ArtifactLeaseHeartbeat(
                registry,
                lease,
                lease_seconds=0.2,
                interval_seconds=0.05,
            )
            time.sleep(0.3)
            heartbeat.check()
            registry.touch((artifact,), lease=lease)
            self.assertEqual(registry.stats()["active_jobs"], 1)
            heartbeat.close()
            self.assertEqual(registry.stats()["active_jobs"], 0)

    def test_query_service_gc_only_collects_real_context_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle_root = root / "lifecycle"
            context_root = root / "contexts"
            registry = ArtifactLifecycleRegistry(lifecycle_root)
            lease = registry.start_job(
                "service-fixture", "producer", lease_seconds=60.0
            )
            contexts = CrossWorkerContextStore(
                context_root,
                lifecycle=registry,
                lifecycle_lease=lease,
            )
            terms = (
                "(= symcc_input_0 (_ bv1 8))",
                "(= symcc_input_1 (_ bv2 8))",
            )
            publication = contexts.publish_chain(
                tuple(hashlib.sha256(term.encode("ascii")).hexdigest() for term in terms),
                terms,
                capability_sha256=hashlib.sha256(b"capability").hexdigest(),
            )
            self.assertIsNotNone(publication)
            self.assertTrue(registry.release_job(lease))
            output = StringIO()
            with redirect_stdout(output):
                status = query_service_main(
                    [
                        "--store",
                        str(root / "queries"),
                        "--qfbv-context-store",
                        str(context_root),
                        "--qfbv-artifact-lifecycle-store",
                        str(lifecycle_root),
                        "--qfbv-artifact-gc-only",
                        "--qfbv-artifact-gc-grace-seconds",
                        "0",
                        "--qfbv-artifact-gc-max-objects",
                        "10",
                        "--qfbv-artifact-gc-max-bytes",
                        "1048576",
                        "--qfbv-artifact-gc-time-ms",
                        "5000",
                    ]
                )
            self.assertEqual(status, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(
                payload["qfbv_artifact_gc"]["deleted_objects"], 2
            )
            self.assertEqual(
                payload["qfbv_artifact_gc"]["stop_reason"], "complete"
            )
            self.assertEqual(
                payload["qfbv_artifact_gc_inventory"]["context"],
                {"complete": True, "scanned": 2, "total": 2},
            )
            self.assertEqual(
                payload["qfbv_artifact_lifecycle"]["artifacts"], 0
            )
            self.assertEqual(
                CrossWorkerContextStore(
                    context_root,
                    lifecycle=ArtifactLifecycleRegistry(lifecycle_root),
                ).stats()["contexts"],
                0,
            )

    def test_gc_recovers_object_published_before_index_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ArtifactLifecycleRegistry(root / "lifecycle")
            contexts = CrossWorkerContextStore(
                root / "contexts", lifecycle=registry
            )
            term = "(= symcc_input_0 (_ bv1 8))"
            original = contexts._publish_object

            def publish_then_crash(digest: str, encoded: bytes) -> bool:
                original(digest, encoded)
                raise RuntimeError("injected crash before context index commit")

            with mock.patch.object(
                contexts, "_publish_object", side_effect=publish_then_crash
            ):
                with self.assertRaisesRegex(RuntimeError, "injected crash"):
                    contexts.publish_chain(
                        (hashlib.sha256(term.encode("ascii")).hexdigest(),),
                        (term,),
                        capability_sha256=hashlib.sha256(b"capability").hexdigest(),
                    )
            self.assertEqual(contexts.stats()["contexts"], 0)
            self.assertEqual(registry.stats()["artifacts"], 1)
            result = registry.collect(
                contexts.delete_lifecycle_artifact,
                grace_seconds=0.0,
                max_objects=1,
                max_bytes=1_000_000,
                time_budget_ms=1_000,
                now=time.time() + 1.0,
            )
            self.assertEqual(len(result.deleted), 1)
            self.assertEqual(registry.stats()["artifacts"], 0)
            self.assertEqual(
                list((root / "contexts/objects").rglob("*.json")), []
            )

    def test_legacy_index_requires_complete_bounded_synchronization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context_root = root / "contexts"
            legacy = CrossWorkerContextStore(context_root)
            terms = (
                "(= symcc_input_0 (_ bv1 8))",
                "(= symcc_input_1 (_ bv2 8))",
            )
            legacy.publish_chain(
                tuple(hashlib.sha256(term.encode("ascii")).hexdigest() for term in terms),
                terms,
                capability_sha256=hashlib.sha256(b"capability").hexdigest(),
            )
            registry = ArtifactLifecycleRegistry(root / "lifecycle")
            managed = CrossWorkerContextStore(
                context_root, lifecycle=registry
            )
            with self.assertRaisesRegex(
                RuntimeError, "synchronization budget is insufficient"
            ):
                query_service_main(
                    [
                        "--store",
                        str(root / "queries"),
                        "--qfbv-context-store",
                        str(context_root),
                        "--qfbv-artifact-lifecycle-store",
                        str(root / "lifecycle"),
                        "--qfbv-artifact-gc-only",
                        "--qfbv-artifact-gc-scan-max-objects",
                        "1",
                    ]
                )
            self.assertEqual(managed.stats()["contexts"], 2)
            bounded = managed.synchronize_lifecycle(max_entries=1)
            self.assertEqual(
                bounded, {"complete": False, "scanned": 0, "total": 2}
            )
            self.assertEqual(registry.stats()["artifacts"], 0)
            complete = managed.synchronize_lifecycle(max_entries=2)
            self.assertEqual(
                complete, {"complete": True, "scanned": 2, "total": 2}
            )
            self.assertEqual(registry.stats()["artifacts"], 2)

    def test_gc_retries_file_after_index_first_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ArtifactLifecycleRegistry(root / "lifecycle")
            contexts = CrossWorkerContextStore(
                root / "contexts", lifecycle=registry
            )
            term = "(= symcc_input_0 (_ bv1 8))"
            publication = contexts.publish_chain(
                (hashlib.sha256(term.encode("ascii")).hexdigest(),),
                (term,),
                capability_sha256=hashlib.sha256(b"capability").hexdigest(),
            )
            assert publication is not None
            path = contexts._object_path(publication.context_sha256)
            original_unlink = Path.unlink
            injected = False

            def crash_once(candidate: Path, *args: object, **kwargs: object) -> None:
                nonlocal injected
                if candidate == path and not injected:
                    injected = True
                    raise OSError("injected crash before unlink")
                original_unlink(candidate, *args, **kwargs)

            with mock.patch.object(Path, "unlink", autospec=True, side_effect=crash_once):
                with self.assertRaisesRegex(OSError, "injected crash"):
                    registry.collect(
                        contexts.delete_lifecycle_artifact,
                        grace_seconds=0.0,
                        max_objects=1,
                        max_bytes=1_000_000,
                        time_budget_ms=1_000,
                        now=time.time() + 1.0,
                    )
            self.assertEqual(contexts.stats()["contexts"], 0)
            self.assertTrue(path.is_file())
            self.assertEqual(registry.stats()["artifacts"], 1)
            recovered = registry.collect(
                contexts.delete_lifecycle_artifact,
                grace_seconds=0.0,
                max_objects=1,
                max_bytes=1_000_000,
                time_budget_ms=1_000,
                now=time.time() + 1.0,
            )
            self.assertEqual(len(recovered.deleted), 1)
            self.assertFalse(path.exists())
            self.assertEqual(registry.stats()["artifacts"], 0)

    def test_managed_stores_reject_uncoordinated_or_different_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = ArtifactLifecycleRegistry(root / "lifecycle-a")
            second = ArtifactLifecycleRegistry(root / "lifecycle-b")
            legacy_contexts = CrossWorkerContextStore(root / "contexts")
            legacy_proofs = QfbvProofStore(root / "proofs")
            legacy_lemmas = QfbvLemmaStore(root / "lemmas")
            CrossWorkerContextStore(root / "contexts", lifecycle=first)
            QfbvProofStore(root / "proofs", lifecycle=first)
            QfbvLemmaStore(root / "lemmas", lifecycle=first)
            digest = hashlib.sha256(b"missing").hexdigest()
            with self.assertRaisesRegex(ValueError, "requires its artifact lifecycle"):
                legacy_contexts.publish_chain(
                    (), (), capability_sha256=hashlib.sha256(b"cap").hexdigest()
                )
            with self.assertRaisesRegex(
                ProofVerificationError, "requires its artifact lifecycle"
            ):
                legacy_proofs.lookup(digest)
            with self.assertRaisesRegex(
                LemmaExchangeError, "requires its artifact lifecycle"
            ):
                legacy_lemmas.records_for_contexts((digest,), limit=1)
            with self.assertRaisesRegex(ValueError, "requires its artifact lifecycle"):
                CrossWorkerContextStore(root / "contexts")
            with self.assertRaisesRegex(ValueError, "lifecycle_root_sha256"):
                CrossWorkerContextStore(root / "contexts", lifecycle=second)
            with self.assertRaisesRegex(
                ProofVerificationError, "requires its artifact lifecycle"
            ):
                QfbvProofStore(root / "proofs")
            with self.assertRaisesRegex(
                ProofVerificationError, "lifecycle_root_sha256"
            ):
                QfbvProofStore(root / "proofs", lifecycle=second)
            with self.assertRaisesRegex(
                LemmaExchangeError, "requires its artifact lifecycle"
            ):
                QfbvLemmaStore(root / "lemmas")
            with self.assertRaisesRegex(
                LemmaExchangeError, "lifecycle_root_sha256"
            ):
                QfbvLemmaStore(root / "lemmas", lifecycle=second)

    def test_managed_store_initialization_is_serialized_across_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            barrier = threading.Barrier(8)

            def initialize(_index: int) -> tuple[int, int, int]:
                try:
                    registry = ArtifactLifecycleRegistry(root / "lifecycle")
                    barrier.wait(timeout=5.0)
                    contexts = CrossWorkerContextStore(
                        root / "contexts", lifecycle=registry
                    )
                    proofs = QfbvProofStore(
                        root / "proofs", lifecycle=registry
                    )
                    lemmas = QfbvLemmaStore(
                        root / "lemmas", lifecycle=registry
                    )
                    return (
                        contexts.stats()["contexts"],
                        proofs.stats()["proofs"],
                        lemmas.stats()["records"],
                    )
                except BaseException:
                    barrier.abort()
                    raise

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(initialize, range(8)))
            self.assertEqual(results, [(0, 0, 0)] * 8)

    def test_executable_oracle_reproduces_graph_and_live_store_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "oracle.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(
                        Path(__file__).resolve().parents[1]
                        / "benchmark/check_qfbv_artifact_lifecycle_oracles.py"
                    ),
                    "--graphs",
                    "4",
                    "--output",
                    str(output),
                ],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(output.read_text(encoding="ascii"))
            self.assertEqual(
                payload["finite_graphs"],
                {
                    "dependency_order_violations": 0,
                    "false_deletions": 0,
                    "graphs": 4,
                    "missed_deletions": 0,
                    "nodes": 64,
                },
            )
            self.assertEqual(payload["live"]["active_protected"], 5)
            self.assertEqual(payload["live"]["released_deleted"], 5)
            self.assertEqual(payload["live"]["orphan_deleted"], 1)


if __name__ == "__main__":
    unittest.main()
