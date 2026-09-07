#!/usr/bin/env python3
# RUN: python3 %s

import errno
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

import query_store as query_store_module  # noqa: E402
import symcc_query_service as query_service_module  # noqa: E402
from query_store import (  # noqa: E402
    JOINT_SCHEDULE_QUERY_SCHEMA,
    PersistentSubprocessSolver,
    PortfolioSolver,
    QueryAdmissionError,
    QueryStore,
    SubprocessSolver,
    WorkLease,
    solve_one,
)
from schedule_exploration import (  # noqa: E402
    parse_schedule_trace,
    schedule_smt_artifact,
)
from symcc_query_service import ingest_spool  # noqa: E402


def _envelope(
    *,
    target_value: int = 66,
    source: str = "seed",
    output_dir: str = "",
) -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "test",
        "nodes": [
            {
                "id": 0,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": 0},
            },
            {
                "id": 1,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "41"},
            },
            {
                "id": 2,
                "op": "equal",
                "bits": 1,
                "children": [0, 1],
                "attrs": {},
            },
            {
                "id": 3,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": f"{target_value:02x}"},
            },
            {
                "id": 4,
                "op": "equal",
                "bits": 1,
                "children": [0, 3],
                "attrs": {},
            },
        ],
        "prefix_roots": [2],
        "target_root": 4,
        "input_hex": "41",
        "timeout_ms": 1000,
        "metadata": {
            "source": source,
            "output_dir": output_dir,
            "site": 123,
        },
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x41))\n"
            f"(assert (= |0| #x{target_value:02x}))\n"
        ),
        "prefix_smt2": ("(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x41))\n"),
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            f"(assert (= |0| #x{target_value:02x}))\n"
        ),
    }


def _partial_source_envelope(
    *,
    target_value: int = 66,
    output_dir: str = "",
) -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "test",
        "nodes": [
            {
                "id": 0,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": 0},
            },
            {
                "id": 1,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": f"{target_value:02x}"},
            },
            {
                "id": 2,
                "op": "equal",
                "bits": 1,
                "children": [0, 1],
                "attrs": {},
            },
            {
                "id": 3,
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": True},
            },
        ],
        "prefix_roots": [3],
        "target_root": 2,
        "input_hex": "00",
        "timeout_ms": 1000,
        "metadata": {
            "source": "partial-source",
            "output_dir": output_dir,
            "site": 321,
        },
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            f"(assert (= |0| #x{target_value:02x}))\n"
        ),
        "prefix_smt2": ("(declare-fun |0| () (_ BitVec 8))\n(assert true)\n"),
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            f"(assert (= |0| #x{target_value:02x}))\n"
        ),
    }


def _partial_range_envelope(
    *,
    lower: int = 64,
    upper: int = 67,
    output_dir: str = "",
) -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "test",
        "nodes": [
            {
                "id": 0,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": 0},
            },
            {
                "id": 1,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": f"{lower:02x}"},
            },
            {
                "id": 2,
                "op": "uge",
                "bits": 1,
                "children": [0, 1],
                "attrs": {},
            },
            {
                "id": 3,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": f"{upper:02x}"},
            },
            {
                "id": 4,
                "op": "ule",
                "bits": 1,
                "children": [0, 3],
                "attrs": {},
            },
            {
                "id": 5,
                "op": "land",
                "bits": 1,
                "children": [2, 4],
                "attrs": {},
            },
            {
                "id": 6,
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": True},
            },
        ],
        "prefix_roots": [6],
        "target_root": 5,
        "input_hex": "00",
        "timeout_ms": 1000,
        "metadata": {
            "source": "partial-target",
            "output_dir": output_dir,
            "site": 322,
        },
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            f"(assert (bvuge |0| #x{lower:02x}))\n"
            f"(assert (bvule |0| #x{upper:02x}))\n"
        ),
        "prefix_smt2": ("(declare-fun |0| () (_ BitVec 8))\n(assert true)\n"),
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            f"(assert (bvuge |0| #x{lower:02x}))\n"
            f"(assert (bvule |0| #x{upper:02x}))\n"
        ),
    }


def _schedule_artifact(
    *,
    target_branch: int = 123,
    replay_prefixes: list[list[int]] | None = None,
    query_id: str = "",
) -> dict:
    artifact = {
        "schema": "symcc-schedule-constraint-v1",
        "input_id": "seed-id",
        "target_branch": target_branch,
        "trace_digest": "0" * 64,
        "current_prefix": [],
        "replay_prefixes": [[2]] if replay_prefixes is None else replay_prefixes,
        "conflict_count": 1,
        "sync_conflict_count": 0,
        "memory_conflict_count": 1,
        "event_count": 2,
        "schedulable_count": 2,
        "provenance_counts": {"module": 2},
    }
    if query_id:
        artifact["query_id"] = query_id
    return artifact


class QueryStoreTest(unittest.TestCase):
    def test_terminal_failure_commits_result_and_outbox_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(
                Path(temporary) / "store",
                startup_reconcile_limit=0,
            )
            query_id, _created = store.ingest(_envelope())
            lease = store.claim("worker", lease_seconds=10)
            self.assertIsNotNone(lease)
            assert lease is not None

            with mock.patch.object(
                store,
                "complete",
                side_effect=AssertionError("terminal fail must not reopen completion"),
            ), mock.patch.object(
                store,
                "_publish_result_artifacts",
                side_effect=OSError("publication unavailable"),
            ):
                self.assertTrue(
                    store.fail(
                        lease,
                        "worker",
                        "attempt limit reached",
                        max_attempts=1,
                    )
                )

            with store._connect() as db:
                state = db.execute(
                    "SELECT status, lease_owner, lease_until FROM queries "
                    "WHERE query_id = ?",
                    (query_id,),
                ).fetchone()
                result = db.execute(
                    "SELECT result_json FROM results WHERE query_id = ?",
                    (query_id,),
                ).fetchone()
                publication = db.execute(
                    "SELECT published, attempts, dead_letter "
                    "FROM result_publications WHERE query_id = ?",
                    (query_id,),
                ).fetchone()
            self.assertEqual(
                (state["status"], state["lease_owner"], state["lease_until"]),
                ("done", None, None),
            )
            self.assertEqual(json.loads(result["result_json"])["status"], "error")
            self.assertEqual(
                tuple(publication),
                (0, 1, 0),
            )
            self.assertIsNone(store.claim("must-not-reclaim"))

            for invalid in (True, 1.5, 0):
                with self.subTest(max_attempts=invalid):
                    with self.assertRaisesRegex(ValueError, "maximum query attempts"):
                        store.fail(
                            lease,
                            "worker",
                            "invalid",
                            max_attempts=invalid,
                        )

    def test_iterative_query_ir_evaluator_bounds_deep_and_cyclic_dags(self):
        expressions = {
            "node-0": {
                "op": "constant",
                "bits": 1,
                "children": [],
                "attrs": {"value_hex": "0"},
            },
        }
        for index in range(1, 6000):
            expressions[f"node-{index}"] = {
                "op": "lnot",
                "bits": 1,
                "children": [f"node-{index - 1}"],
                "attrs": {},
            }

        memo = {}
        budget = [6000]
        self.assertEqual(
            QueryStore._evaluate_expression(
                "node-5999", expressions, b"", memo, budget=budget
            ),
            1,
        )
        self.assertEqual((len(memo), budget[0]), (6000, 0))
        self.assertIsNone(
            QueryStore._evaluate_expression(
                "node-5999", expressions, b"", {}, budget=[10]
            )
        )

        cyclic = {
            "a": {"op": "lnot", "bits": 1, "children": ["b"], "attrs": {}},
            "b": {"op": "lnot", "bits": 1, "children": ["a"], "attrs": {}},
        }
        self.assertIsNone(
            QueryStore._evaluate_expression("a", cyclic, b"", {})
        )

    def test_query_ir_evaluator_implements_bitvector_division_by_zero(self):
        def evaluate(operation: str, lhs: int) -> int | None:
            expressions = {
                "lhs": {
                    "op": "constant",
                    "bits": 8,
                    "children": [],
                    "attrs": {"value_hex": f"{lhs:02x}"},
                },
                "zero": {
                    "op": "constant",
                    "bits": 8,
                    "children": [],
                    "attrs": {"value_hex": "00"},
                },
                "root": {
                    "op": operation,
                    "bits": 8,
                    "children": ["lhs", "zero"],
                    "attrs": {},
                },
            }
            return QueryStore._evaluate_expression(
                "root", expressions, b"", {}
            )

        for lhs in (0x00, 0x01, 0x7F, 0x80, 0xFF):
            with self.subTest(operation="udiv", lhs=lhs):
                self.assertEqual(evaluate("udiv", lhs), 0xFF)
            with self.subTest(operation="urem", lhs=lhs):
                self.assertEqual(evaluate("urem", lhs), lhs)
            with self.subTest(operation="sdiv", lhs=lhs):
                self.assertEqual(
                    evaluate("sdiv", lhs),
                    0x01 if lhs & 0x80 else 0xFF,
                )
            with self.subTest(operation="srem", lhs=lhs):
                self.assertEqual(evaluate("srem", lhs), lhs)

    def test_query_claim_steals_from_a_hot_prefix_after_local_miss(self):
        for shape_selection in ("0", "1"):
            with (
                self.subTest(shape_selection=shape_selection),
                mock.patch.dict(
                    os.environ,
                    {"SYMCC_QUERY_SHAPE_SELECTION": shape_selection},
                ),
                tempfile.TemporaryDirectory() as temporary,
            ):
                store = QueryStore(temporary)
                for target_value in range(0x42, 0x46):
                    _query_id, created = store.ingest(
                        _envelope(target_value=target_value)
                    )
                    self.assertTrue(created)

                with store._connect() as db:
                    prefix_ids = {
                        int(row[0])
                        for row in db.execute(
                            "SELECT DISTINCT prefix_id FROM queries"
                        ).fetchall()
                    }
                self.assertEqual(len(prefix_ids), 1)

                leases = []
                try:
                    for shard_index in range(4):
                        lease = store.claim(
                            f"worker-{shard_index}",
                            shard_index=shard_index,
                            shard_count=4,
                        )
                        self.assertIsNotNone(lease)
                        assert lease is not None
                        leases.append(lease)
                    self.assertEqual(len({lease.query_id for lease in leases}), 4)
                finally:
                    for lease in leases:
                        lease.close_artifacts()

    def test_content_addressing_and_prefix_trie(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            first, created = store.ingest(_envelope(target_value=66))
            self.assertTrue(created)
            duplicate, created = store.ingest(
                _envelope(target_value=66, source="other")
            )
            self.assertEqual(first, duplicate)
            self.assertFalse(created)
            second, created = store.ingest(_envelope(target_value=67))
            self.assertNotEqual(first, second)
            self.assertTrue(created)
            stats = store.stats()
            self.assertEqual(stats["queries"], 2)
            self.assertEqual(stats["witnesses"], 3)
            self.assertEqual(stats["prefix_nodes"], 2)
            self.assertEqual(stats["expressions"], 7)
            query_body = json.loads(
                (store.query_dir / first[:2] / f"{first}.json").read_text(
                    encoding="ascii"
                )
            )
            self.assertEqual(query_body["converter_chain"][0]["assignments"], {"0": 66})

            artifact = store.artifact_path(query_body["smt2_hash"])
            expected_smt2 = _envelope(target_value=66)["smt2"].encode("utf-8")
            self.assertEqual(artifact.read_bytes(), expected_smt2)

            artifact.write_bytes(b"(assert false)\n")
            with self.assertRaisesRegex(ValueError, "integrity verification"):
                store.artifact_path(query_body["smt2_hash"])
            with self.assertRaisesRegex(ValueError, "integrity verification"):
                store.claim("must-not-lease-corrupt-artifact")
            with store._connect() as db:
                lease_state = db.execute(
                    "SELECT status, attempts FROM queries WHERE query_id = ?",
                    (first,),
                ).fetchone()
            self.assertEqual(
                (lease_state["status"], lease_state["attempts"]),
                ("pending", 0),
            )

            outside = Path(temporary) / "outside-smt2"
            outside.write_bytes(b"outside-must-not-change")
            artifact.unlink()
            artifact.symlink_to(outside)
            repaired, created = store.ingest(_envelope(target_value=66))
            self.assertEqual(repaired, first)
            self.assertFalse(created)
            self.assertFalse(artifact.is_symlink())
            self.assertEqual(artifact.read_bytes(), expected_smt2)
            self.assertEqual(outside.read_bytes(), b"outside-must-not-change")

            artifact.unlink()
            os.mkfifo(artifact)
            repaired, created = store.ingest(_envelope(target_value=66))
            self.assertEqual(repaired, first)
            self.assertFalse(created)
            self.assertTrue(stat.S_ISREG(artifact.stat().st_mode))
            self.assertEqual(artifact.read_bytes(), expected_smt2)

            reopened = QueryStore(temporary)
            self.assertEqual(
                reopened.artifact_path(query_body["smt2_hash"]).read_bytes(),
                expected_smt2,
            )

            with store._connect() as db:
                db.execute(
                    "UPDATE artifacts SET relative_path = ? WHERE hash = ?",
                    ("../../outside-smt2", query_body["smt2_hash"]),
                )
            with self.assertRaisesRegex(ValueError, "non-canonical path"):
                store.artifact_path(query_body["smt2_hash"])
            store.ingest(_envelope(target_value=66))
            self.assertEqual(store.artifact_path(query_body["smt2_hash"]), artifact)

            create_memfd = query_store_module._sealed_memfd
            created_snapshots = 0
            created_descriptors: list[int] = []

            def fail_second_snapshot(content, *, role, digest):
                nonlocal created_snapshots
                created_snapshots += 1
                if created_snapshots == 2:
                    raise OSError(errno.ENOMEM, "injected memfd allocation failure")
                descriptor = create_memfd(content, role=role, digest=digest)
                created_descriptors.append(descriptor)
                return descriptor

            with mock.patch.object(
                query_store_module,
                "_sealed_memfd",
                side_effect=fail_second_snapshot,
            ):
                with self.assertRaisesRegex(OSError, "injected memfd"):
                    store.claim("snapshot-allocation-failure")
            self.assertEqual(len(created_descriptors), 1)
            with self.assertRaises(OSError) as closed_descriptor:
                os.fstat(created_descriptors[0])
            self.assertEqual(closed_descriptor.exception.errno, errno.EBADF)
            with store._connect() as db:
                states = db.execute(
                    "SELECT status, attempts FROM queries ORDER BY query_id"
                ).fetchall()
            self.assertEqual(
                [(row["status"], row["attempts"]) for row in states],
                [("pending", 0), ("pending", 0)],
            )

            lease = store.claim("sealed-snapshot")
            self.assertIsNotNone(lease)
            assert lease is not None
            descriptors = lease.duplicate_artifacts("full", "prefix", "target")
            try:
                required_seals = query_store_module._SEALED_ARTIFACT_REQUIRED_SEALS
                self.assertTrue(
                    all(
                        query_store_module.fcntl.fcntl(
                            descriptor,
                            query_store_module.fcntl.F_GET_SEALS,
                        )
                        & required_seals
                        == required_seals
                        for descriptor in descriptors
                    )
                )
                with self.assertRaises(OSError):
                    os.write(descriptors[0], b"x")
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)
                lease.close_artifacts()

        with self.subTest("report-only artifact reachability audit"):
            with tempfile.TemporaryDirectory() as temporary:
                audit_store = QueryStore(temporary)
                audit_store.ingest(_envelope())
                baseline = audit_store.audit_artifacts(max_entries=100)
                self.assertTrue(baseline["scan"]["complete"])
                self.assertEqual(baseline["database"]["query_reference_count"], 3)
                self.assertEqual(
                    baseline["database"]["unreferenced_artifact_rows"], 0
                )
                self.assertEqual(baseline["physical"]["missing_primary_count"], 0)
                self.assertFalse(baseline["safe_to_sweep"])

                indexed_digest = audit_store._store_artifact(
                    "smt2",
                    b"(assert indexed-orphan)\n",
                    ".smt2",
                )
                unindexed_content = b"(assert unindexed-orphan)\n"
                unindexed_store = audit_store._artifact_stores["smt2-target"]
                unindexed_digest = unindexed_store.digest(unindexed_content)
                _, unindexed_path = unindexed_store.put(
                    unindexed_content,
                    unindexed_digest,
                )
                malformed = audit_store.object_dir / "smt2" / "not-a-shard"
                malformed.write_bytes(b"must remain untouched")

                report = audit_store.audit_artifacts(max_entries=100)
                self.assertEqual(report["mode"], "report-only")
                self.assertTrue(report["scan"]["complete"])
                self.assertEqual(report["scan"]["noncanonical_entries"], 1)
                self.assertEqual(
                    report["database"]["unreferenced_artifact_rows"], 1
                )
                self.assertEqual(report["physical"]["orphan_objects_observed"], 2)
                self.assertEqual(report["physical"]["unindexed_objects_observed"], 1)
                self.assertEqual(
                    report["candidates"]["unreferenced_artifact_rows"][0][
                        "object_id"
                    ],
                    indexed_digest,
                )
                self.assertEqual(
                    report["candidates"]["unindexed_physical"][0]["object_id"],
                    unindexed_digest,
                )
                self.assertTrue(Path(unindexed_path).is_file())
                self.assertTrue(malformed.is_file())

                partial = audit_store.audit_artifacts(max_entries=1)
                self.assertFalse(partial["scan"]["complete"])
                self.assertIsNone(partial["physical"]["missing_primary_count"])
                self.assertIsNone(partial["candidates"]["missing_primary"])
                self.assertFalse(partial["safe_to_sweep"])

        with self.subTest("artifact audit detects primary size drift"):
            with tempfile.TemporaryDirectory() as temporary:
                audit_store = QueryStore(temporary)
                audit_store.ingest(_envelope())
                with audit_store._connect() as db:
                    row = db.execute(
                        "SELECT hash, relative_path, size FROM artifacts "
                        "WHERE kind = 'smt2'"
                    ).fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                artifact = audit_store.object_dir / row["relative_path"]
                artifact.write_bytes(artifact.read_bytes() + b"; size drift\n")

                drifted = audit_store.audit_artifacts(max_entries=100)
                self.assertTrue(drifted["scan"]["complete"])
                self.assertEqual(drifted["physical"]["missing_primary_count"], 0)
                self.assertEqual(
                    drifted["physical"]["primary_size_mismatch_count"], 1
                )
                self.assertFalse(drifted["verdict"]["namespace_reference_closure"])
                mismatch = drifted["candidates"]["primary_size_mismatches"][0]
                self.assertEqual(mismatch["object_id"], row["hash"])
                self.assertEqual(mismatch["expected_size"], row["size"])
                self.assertEqual(mismatch["size"], artifact.stat().st_size)
                self.assertFalse(drifted["content_digests_verified"])

                audit_store.ingest(_envelope())
                repaired = audit_store.audit_artifacts(max_entries=100)
                self.assertEqual(
                    repaired["physical"]["primary_size_mismatch_count"], 0
                )
                self.assertTrue(repaired["verdict"]["namespace_reference_closure"])

        with self.subTest("query identity rejects conflicting artifact binding"):
            with tempfile.TemporaryDirectory() as temporary:
                binding_store = QueryStore(temporary)
                original = _envelope()
                query_id, created = binding_store.ingest(original)
                self.assertTrue(created)
                query_path = (
                    binding_store.query_dir
                    / query_id[:2]
                    / f"{query_id}.json"
                )
                query_path.unlink()
                with binding_store._connect() as db:
                    before = db.execute(
                        "SELECT smt2_hash, prefix_smt2_hash, target_smt2_hash "
                        "FROM queries WHERE query_id = ?",
                        (query_id,),
                    ).fetchone()
                    artifact_rows_before = int(
                        db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
                    )
                self.assertIsNotNone(before)
                assert before is not None

                conflicting = _envelope()
                conflicting["smt2"] += "; conflicting full\n"
                conflicting["prefix_smt2"] += "; conflicting prefix\n"
                conflicting["target_smt2"] += "; conflicting target\n"
                with self.assertRaisesRegex(
                    QueryAdmissionError,
                    "already bound to different SMT2 artifacts",
                ):
                    binding_store.ingest(conflicting)

                self.assertFalse(query_path.exists())
                with binding_store._connect() as db:
                    after = db.execute(
                        "SELECT smt2_hash, prefix_smt2_hash, target_smt2_hash "
                        "FROM queries WHERE query_id = ?",
                        (query_id,),
                    ).fetchone()
                    artifact_rows_after = int(
                        db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
                    )
                self.assertIsNotNone(after)
                assert after is not None
                columns = (
                    "smt2_hash",
                    "prefix_smt2_hash",
                    "target_smt2_hash",
                )
                self.assertEqual(
                    tuple(after[column] for column in columns),
                    tuple(before[column] for column in columns),
                )
                self.assertEqual(artifact_rows_after, artifact_rows_before)

                repeated, created = binding_store.ingest(original)
                self.assertEqual(repeated, query_id)
                self.assertFalse(created)
                body = json.loads(query_path.read_text(encoding="ascii"))
                self.assertEqual(
                    tuple(body[column] for column in columns),
                    tuple(before[column] for column in columns),
                )

                body["smt2_hash"] = "f" * 64
                query_path.write_text(
                    json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                with self.assertRaisesRegex(ValueError, "disagrees with SQLite"):
                    binding_store.claim("must-not-lease-conflicting-query-body")
                with binding_store._connect() as db:
                    lease_state = db.execute(
                        "SELECT status, attempts FROM queries WHERE query_id = ?",
                        (query_id,),
                    ).fetchone()
                self.assertEqual(
                    (lease_state["status"], lease_state["attempts"]),
                    ("pending", 0),
                )

                binding_store.ingest(original)
                lease = binding_store.claim("repaired-query-body")
                self.assertIsNotNone(lease)
                assert lease is not None
                lease.close_artifacts()

        with self.subTest("artifact publishers share the audit fence"):
            with tempfile.TemporaryDirectory() as temporary:
                barrier = threading.Barrier(2, timeout=2)

                class ConcurrentPublisherStore(QueryStore):
                    def _ingest_validated_locked(self, validated):
                        barrier.wait()
                        return super()._ingest_validated_locked(validated)

                concurrent_store = ConcurrentPublisherStore(temporary)
                results = []
                errors = []

                def publish(target_value):
                    try:
                        results.append(
                            concurrent_store.ingest(
                                _envelope(target_value=target_value)
                            )
                        )
                    except BaseException as error:
                        errors.append(error)

                publishers = [
                    threading.Thread(target=publish, args=(target_value,))
                    for target_value in (66, 67)
                ]
                for publisher in publishers:
                    publisher.start()
                for publisher in publishers:
                    publisher.join(3)
                self.assertTrue(all(not publisher.is_alive() for publisher in publishers))
                self.assertEqual(errors, [])
                self.assertEqual(len(results), 2)
                self.assertEqual(concurrent_store.stats()["queries"], 2)

        with self.subTest("lease identity and deadline preflight"):
            with tempfile.TemporaryDirectory() as temporary:
                lease_store = QueryStore(temporary)
                lease_store.ingest(_envelope())
                for invalid_duration in (float("nan"), float("inf"), 1e308, True):
                    with self.assertRaisesRegex(ValueError, "lease_seconds"):
                        lease_store.claim(
                            "invalid-deadline",
                            lease_seconds=invalid_duration,
                        )
                for invalid_owner in (b"bytes", "x" * 257, "nul\0owner", "\ud800"):
                    with self.assertRaises(ValueError):
                        lease_store.claim(invalid_owner)
                with lease_store._connect() as db:
                    state = db.execute(
                        "SELECT status, attempts FROM queries"
                    ).fetchone()
                self.assertEqual((state["status"], state["attempts"]), ("pending", 0))
                valid = lease_store.claim("valid-worker", lease_seconds=1.0)
                self.assertIsNotNone(valid)
                assert valid is not None
                valid.close_artifacts()

        with self.subTest("artifact audit serializes publication"):
            with tempfile.TemporaryDirectory() as temporary:
                audit_store = QueryStore(temporary)
                audit_store.ingest(_envelope())
                entered = threading.Event()
                release = threading.Event()
                audit_results = []
                ingest_results = []
                errors = []
                physical_store = audit_store._artifact_stores["smt2"]
                scan_objects = physical_store.scan_objects

                def blocking_scan(*, max_entries):
                    entered.set()
                    if not release.wait(2):
                        raise TimeoutError("artifact audit test was not released")
                    return scan_objects(max_entries=max_entries)

                def run_audit():
                    try:
                        audit_results.append(
                            audit_store.audit_artifacts(max_entries=100)
                        )
                    except BaseException as error:
                        errors.append(error)

                def run_ingest():
                    try:
                        ingest_results.append(
                            audit_store.ingest(_envelope(target_value=68))
                        )
                    except BaseException as error:
                        errors.append(error)

                with mock.patch.object(
                    physical_store,
                    "scan_objects",
                    side_effect=blocking_scan,
                ):
                    auditor = threading.Thread(target=run_audit)
                    publisher = threading.Thread(target=run_ingest)
                    auditor.start()
                    self.assertTrue(entered.wait(2))
                    publisher.start()
                    time.sleep(0.05)
                    self.assertTrue(publisher.is_alive())
                    self.assertEqual(audit_store.stats()["queries"], 1)
                    release.set()
                    auditor.join(2)
                    publisher.join(2)
                self.assertFalse(auditor.is_alive())
                self.assertFalse(publisher.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(audit_results), 1)
                self.assertEqual(len(ingest_results), 1)
                self.assertEqual(
                    audit_results[0]["database"]["query_reference_count"], 3
                )
                self.assertEqual(audit_store.stats()["queries"], 2)

        for lock_kind in ("symlink", "fifo"):
            with self.subTest("artifact audit lock fails closed", kind=lock_kind):
                with tempfile.TemporaryDirectory() as temporary:
                    audit_store = QueryStore(temporary)
                    lock_path = audit_store._artifact_audit_lock_path
                    if lock_kind == "symlink":
                        external = Path(temporary) / "external-lock-target"
                        external.write_bytes(b"unchanged")
                        lock_path.symlink_to(external)
                    else:
                        os.mkfifo(lock_path)
                    with self.assertRaises(OSError):
                        audit_store.ingest(_envelope())
                    self.assertEqual(audit_store.stats()["queries"], 0)
                    if lock_kind == "symlink":
                        self.assertEqual(external.read_bytes(), b"unchanged")

    def test_fenced_lease_rejects_stale_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            query_id, _ = store.ingest(_partial_source_envelope())
            stale = store.claim("old", lease_seconds=0.001)
            self.assertIsNotNone(stale)
            time.sleep(0.01)
            result = {
                "status": "sat",
                "assignments": {"0": 66},
                "solver": "test",
                "elapsed_us": 1,
            }
            self.assertFalse(store.complete(stale, "old", result))
            current = store.claim("new", lease_seconds=10)
            self.assertIsNotNone(current)
            assert stale is not None and current is not None
            self.assertEqual(stale.query_id, query_id)
            self.assertGreater(current.token, stale.token)
            self.assertFalse(store.complete(stale, "old", result))
            self.assertTrue(store.complete(current, "new", result))

            store.ingest(_partial_source_envelope(target_value=67))
            expiring = store.claim("commit-race", lease_seconds=10)
            self.assertIsNotNone(expiring)
            assert expiring is not None
            with store._connect() as db:
                lease_until = float(db.execute(
                    "SELECT lease_until FROM queries WHERE query_id = ?",
                    (expiring.query_id,),
                ).fetchone()[0])
            with mock.patch.object(
                query_store_module.time,
                "time",
                side_effect=(lease_until - 1.0, lease_until + 1.0),
            ):
                self.assertFalse(store.complete(
                    expiring,
                    "commit-race",
                    {
                        "status": "error",
                        "assignments": {},
                        "solver": "test",
                        "elapsed_us": 1,
                        "reason": "synthetic commit race",
                    },
                ))
            self.assertEqual(store.stats()["done"], 1)

    def test_claim_deadline_is_measured_after_write_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            store.ingest(_envelope())
            writer = store._connect()
            writer.execute("BEGIN IMMEDIATE")
            started = threading.Event()
            leases: list[WorkLease | None] = []
            errors: list[BaseException] = []

            def blocked_claim() -> None:
                started.set()
                try:
                    leases.append(store.claim("blocked-worker", lease_seconds=0.25))
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=blocked_claim)
            worker.start()
            try:
                self.assertTrue(started.wait(timeout=1.0))
                time.sleep(0.35)
                self.assertTrue(worker.is_alive())
                writer.commit()
            finally:
                writer.close()
            worker.join(timeout=2.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(leases), 1)
            lease = leases[0]
            self.assertIsNotNone(lease)
            assert lease is not None
            try:
                self.assertTrue(
                    store.query_lease_is_active(f"{lease.query_id}:{lease.token}")
                )
                self.assertIsNone(store.claim("second-worker", lease_seconds=0.25))
            finally:
                lease.close_artifacts()

    def test_claim_materializes_artifacts_outside_write_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            store.ingest(_envelope(target_value=66, source="first"))
            store.ingest(_envelope(target_value=67, source="second"))
            physical_store = store._artifact_stores["smt2"]
            original_snapshot = physical_store.snapshot
            first_snapshot_barrier = threading.Barrier(2, timeout=2.0)
            seen_threads: set[int] = set()
            seen_lock = threading.Lock()
            leases: list[WorkLease | None] = []
            errors: list[BaseException] = []

            def synchronized_snapshot(object_id: str, *, retain_content: bool = False):
                thread_id = threading.get_ident()
                with seen_lock:
                    first_for_thread = thread_id not in seen_threads
                    if first_for_thread:
                        seen_threads.add(thread_id)
                if first_for_thread:
                    first_snapshot_barrier.wait()
                return original_snapshot(object_id, retain_content=retain_content)

            def claim(owner: str) -> None:
                try:
                    leases.append(store.claim(owner, lease_seconds=5.0))
                except BaseException as error:
                    errors.append(error)

            with mock.patch.object(
                physical_store,
                "snapshot",
                side_effect=synchronized_snapshot,
            ):
                workers = [
                    threading.Thread(target=claim, args=(f"worker-{index}",))
                    for index in range(2)
                ]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=3.0)

            try:
                self.assertTrue(all(not worker.is_alive() for worker in workers))
                self.assertEqual(errors, [])
                self.assertEqual(len(leases), 2)
                self.assertTrue(all(lease is not None for lease in leases))
            finally:
                for lease in leases:
                    if lease is not None:
                        lease.close_artifacts()

    def test_result_publication_outbox_recovers_post_commit_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = QueryStore(root)
            query_id, _ = store.ingest(_partial_source_envelope())
            lease = store.claim("publisher", lease_seconds=10)
            self.assertIsNotNone(lease)
            assert lease is not None
            result_path = store.result_dir / query_id[:2] / f"{query_id}.json"
            original_write = query_store_module._atomic_write

            def interrupt_result(path, data):
                if Path(path) == result_path:
                    raise OSError("simulated post-commit result publication failure")
                return original_write(path, data)

            with mock.patch.object(
                query_store_module,
                "_atomic_write",
                side_effect=interrupt_result,
            ):
                self.assertTrue(store.complete(
                    lease,
                    "publisher",
                    {
                        "status": "error",
                        "assignments": {},
                        "solver": "test",
                        "elapsed_us": 1,
                        "reason": "synthetic",
                    },
                ))

            failed = store.stats()
            self.assertEqual(failed["done"], 1)
            self.assertEqual(failed["result_publications_pending"], 1)
            self.assertEqual(failed["result_publication_retries"], 1)
            self.assertFalse(result_path.exists())
            self.assertIsNone(store.claim("replacement", lease_seconds=10))

            deferred = QueryStore(root)
            self.assertEqual(
                deferred.publication_reconciliation_snapshot()["attempted"], 0
            )
            self.assertEqual(deferred.stats()["result_publications_pending"], 1)
            retry_time = time.time() + 10.0
            with mock.patch.object(
                query_store_module.time, "time", return_value=retry_time
            ):
                reopened = QueryStore(root)
            recovered = reopened.stats()
            self.assertEqual(recovered["result_publications_pending"], 0)
            self.assertEqual(recovered["result_publications_completed"], 1)
            self.assertTrue(result_path.is_file())

    def test_result_publication_outbox_recovers_candidate_materialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "runtime-output"
            output.mkdir()
            store = QueryStore(root / "store")
            query_id, _ = store.ingest(
                _envelope(target_value=65, output_dir=str(output))
            )
            lease = store.claim("publisher", lease_seconds=10)
            self.assertIsNotNone(lease)
            assert lease is not None
            original_write = query_store_module._atomic_write

            def interrupt_candidate(path, data):
                candidate = Path(path)
                if candidate.suffix == ".bin" and store.candidate_dir in candidate.parents:
                    raise OSError("simulated candidate materialization failure")
                return original_write(path, data)

            with mock.patch.object(
                query_store_module,
                "_atomic_write",
                side_effect=interrupt_candidate,
            ):
                self.assertTrue(store.complete(
                    lease,
                    "publisher",
                    {
                        "status": "sat",
                        "assignments": {"0": 65},
                        "solver": "test",
                        "elapsed_us": 1,
                    },
                ))

            self.assertEqual(store.stats()["result_publications_pending"], 1)
            self.assertEqual(list(store.candidate_dir.rglob("*.bin")), [])

            retry_time = time.time() + 10.0
            with mock.patch.object(
                query_store_module.time, "time", return_value=retry_time
            ):
                reopened = QueryStore(root / "store")
            candidates = list(reopened.candidate_dir.rglob("*.bin"))
            self.assertEqual([path.read_bytes() for path in candidates], [b"A"])
            self.assertEqual([path.read_bytes() for path in output.glob("async-*")], [b"A"])
            self.assertEqual(reopened.stats()["result_publications_pending"], 0)

    def test_startup_publication_recovery_is_bounded_and_skips_poisoned_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "store"
            store = QueryStore(root, startup_reconcile_limit=0)
            query_ids = []
            for index in range(5):
                query_id, _created = store.ingest(
                    _envelope(target_value=65 + index)
                )
                query_ids.append(query_id)
                lease = store.claim("publisher", lease_seconds=10)
                self.assertIsNotNone(lease)
                assert lease is not None
                with mock.patch.object(
                    store,
                    "_publish_result_artifacts",
                    side_effect=OSError(f"initial failure {index}"),
                ):
                    self.assertTrue(store.complete(
                        lease,
                        "publisher",
                        {
                            "status": "error",
                            "assignments": {},
                            "solver": "test",
                            "elapsed_us": 1,
                            "reason": "synthetic",
                        },
                    ))
            self.assertEqual(store.stats()["result_publications_pending"], 5)

            original_publish = QueryStore._publish_result_artifacts

            def publish_except_poison(instance, query_id, result_json):
                if query_id == query_ids[0]:
                    raise OSError("persistent poisoned publication")
                return original_publish(instance, query_id, result_json)

            retry_time = time.time() + 10.0
            with mock.patch.object(
                QueryStore,
                "_publish_result_artifacts",
                autospec=True,
                side_effect=publish_except_poison,
            ), mock.patch.object(
                query_store_module.time, "time", return_value=retry_time
            ):
                reopened = QueryStore(
                    root,
                    startup_reconcile_limit=3,
                    startup_reconcile_seconds=10.0,
                )

            reconciliation = reopened.publication_reconciliation_snapshot()
            self.assertEqual(reconciliation["attempted"], 3)
            self.assertEqual(reconciliation["published"], 2)
            self.assertEqual(reconciliation["failed"], 1)
            self.assertEqual(reopened.stats()["result_publications_pending"], 3)

    def test_publication_janitor_backoff_dead_letter_and_operator_requeue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "store"
            store = QueryStore(root, startup_reconcile_limit=0)
            query_id, _created = store.ingest(_partial_source_envelope())
            lease = store.claim("publisher", lease_seconds=10)
            self.assertIsNotNone(lease)
            assert lease is not None
            with mock.patch.object(
                store,
                "_publish_result_artifacts",
                side_effect=OSError("persistent publication failure"),
            ):
                self.assertTrue(store.complete(
                    lease,
                    "publisher",
                    {
                        "status": "error",
                        "assignments": {},
                        "solver": "test",
                        "elapsed_us": 1,
                        "reason": "synthetic",
                    },
                ))

            self.assertEqual(
                store.reconcile_result_publications(
                    limit=1,
                    respect_retry_after=True,
                    continue_on_error=True,
                ),
                0,
            )
            self.assertEqual(
                store.publication_reconciliation_snapshot()["attempted"], 0
            )
            self.assertEqual(
                store.reconcile_result_publications(
                    [query_id],
                    respect_retry_after=True,
                    continue_on_error=True,
                ),
                0,
            )
            self.assertEqual(
                store.publication_reconciliation_snapshot()["attempted"], 0
            )

            with mock.patch.object(
                store,
                "_publish_result_artifacts",
                side_effect=OSError("still poisoned"),
            ):
                store.reconcile_result_publications(
                    [query_id],
                    continue_on_error=True,
                    max_attempts=2,
                )
            failed = store.stats()
            self.assertEqual(failed["result_publications_pending"], 1)
            self.assertEqual(failed["result_publications_active"], 0)
            self.assertEqual(failed["result_publications_dead_lettered"], 1)
            self.assertGreaterEqual(failed["result_publication_retries"], 2)

            self.assertEqual(store.requeue_result_publications([query_id]), 1)
            self.assertEqual(store.stats()["result_publications_active"], 1)
            self.assertEqual(
                store.reconcile_result_publications([query_id]),
                1,
            )
            recovered = store.stats()
            self.assertEqual(recovered["result_publications_pending"], 0)
            self.assertEqual(recovered["result_publications_completed"], 1)

    def test_concurrent_publication_failures_increment_attempts_linearly(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(
                Path(temporary) / "store",
                startup_reconcile_limit=0,
            )
            query_id, _created = store.ingest(_envelope())
            lease = store.claim("publisher", lease_seconds=10)
            self.assertIsNotNone(lease)
            assert lease is not None
            with mock.patch.object(
                store,
                "_publish_result_artifacts",
                side_effect=OSError("initial outage"),
            ):
                self.assertTrue(store.complete(
                    lease,
                    "publisher",
                    {
                        "status": "error",
                        "assignments": {},
                        "solver": "test",
                        "elapsed_us": 1,
                        "reason": "synthetic",
                    },
                ))

            barrier = threading.Barrier(2, timeout=5.0)

            def fail_together(_query_id, _result_json):
                barrier.wait()
                raise OSError("concurrent outage")

            errors = []

            def reconcile():
                try:
                    store.reconcile_result_publications(
                        [query_id],
                        continue_on_error=True,
                        max_attempts=3,
                        retry_base_seconds=0.0,
                        retry_max_seconds=0.0,
                    )
                except BaseException as error:
                    errors.append(error)

            with mock.patch.object(
                store,
                "_publish_result_artifacts",
                side_effect=fail_together,
            ):
                threads = [threading.Thread(target=reconcile) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10.0)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            stats = store.stats()
            self.assertEqual(stats["result_publication_retries"], 3)
            self.assertEqual(stats["result_publications_active"], 0)
            self.assertEqual(stats["result_publications_dead_lettered"], 1)

    def test_repeated_bounded_publication_passes_drain_more_than_one_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(
                Path(temporary) / "store",
                startup_reconcile_limit=0,
            )
            with mock.patch.object(
                query_store_module.time,
                "time",
                return_value=100.0,
            ), mock.patch.object(
                store,
                "_publish_result_artifacts",
                side_effect=OSError("initial publication outage"),
            ):
                for index in range(65):
                    store.ingest(_envelope(target_value=65 + index))
                    lease = store.claim("publisher", lease_seconds=10.0)
                    self.assertIsNotNone(lease)
                    assert lease is not None
                    self.assertTrue(store.complete(
                            lease,
                            "publisher",
                            {
                                "status": "error",
                                "assignments": {},
                                "solver": "test",
                                "elapsed_us": 1,
                                "reason": "synthetic",
                            },
                        ))

            self.assertEqual(store.stats()["result_publications_active"], 65)
            self.assertEqual(
                store.drain_result_publications(
                    limit=64,
                    time_budget_seconds=10.0,
                ),
                65,
            )
            self.assertEqual(store.stats()["result_publications_pending"], 0)

    def test_publication_drain_continues_after_a_time_bounded_partial_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(
                Path(temporary) / "store",
                startup_reconcile_limit=0,
            )
            passes = [
                {
                    "attempted": 1,
                    "time_budget_exhausted": True,
                },
                {
                    "attempted": 1,
                    "time_budget_exhausted": False,
                },
            ]
            with mock.patch.object(
                store,
                "reconcile_result_publications",
                side_effect=[1, 1],
            ) as reconcile, mock.patch.object(
                store,
                "publication_reconciliation_snapshot",
                side_effect=passes,
            ):
                self.assertEqual(
                    store.drain_result_publications(limit=64),
                    2,
                )
            self.assertEqual(reconcile.call_count, 2)

    def test_positive_publication_budget_admits_one_atomic_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(
                Path(temporary) / "store",
                startup_reconcile_limit=0,
            )
            query_id, _created = store.ingest(_partial_source_envelope())
            lease = store.claim("publisher", lease_seconds=10)
            self.assertIsNotNone(lease)
            assert lease is not None
            with mock.patch.object(
                store,
                "_publish_result_artifacts",
                side_effect=OSError("initial publication outage"),
            ):
                self.assertTrue(store.complete(
                    lease,
                    "publisher",
                    {
                        "status": "error",
                        "assignments": {},
                        "solver": "test",
                        "elapsed_us": 1,
                        "reason": "synthetic",
                    },
                ))

            with mock.patch.object(
                query_store_module.time,
                "monotonic",
                side_effect=[0.0, 2.0, 2.0, 2.0],
            ):
                self.assertEqual(
                    store.reconcile_result_publications(
                        [query_id],
                        time_budget_seconds=0.001,
                    ),
                    1,
                )

    def test_result_publication_replaces_corrupt_content_addressed_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = QueryStore(root)
            query_id, _ = store.ingest(_envelope(target_value=65))
            lease = store.claim("publisher", lease_seconds=10)
            self.assertIsNotNone(lease)
            assert lease is not None

            candidate_hash = hashlib.sha256(b"A").hexdigest()
            candidate_path = (
                store.candidate_dir
                / candidate_hash[:2]
                / f"{candidate_hash}.bin"
            )
            candidate_path.parent.mkdir(parents=True)
            candidate_path.write_bytes(b"corrupt")

            self.assertTrue(store.complete(
                lease,
                "publisher",
                {
                    "status": "sat",
                    "assignments": {"0": 65},
                    "solver": "test",
                    "elapsed_us": 1,
                },
            ))
            self.assertEqual(candidate_path.read_bytes(), b"A")

    def test_solve_one_renews_lease_during_solver_and_commit_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            store.ingest(_partial_source_envelope())
            solve_started = threading.Event()
            release_solver = threading.Event()
            validation_started = threading.Event()
            release_validation = threading.Event()
            statuses: list[str | None] = []

            def backend(_lease: WorkLease) -> dict:
                solve_started.set()
                self.assertTrue(release_solver.wait(timeout=2.0))
                return {
                    "status": "sat",
                    "assignments": {"0": 66},
                    "solver": "test",
                    "elapsed_us": 1,
                }

            original_validate = store._validate_result

            def delayed_validate(result):
                validation_started.set()
                self.assertTrue(release_validation.wait(timeout=2.0))
                return original_validate(result)

            with mock.patch.object(store, "_validate_result", delayed_validate):
                worker = threading.Thread(
                    target=lambda: statuses.append(
                        solve_one(
                            store,
                            "worker-a",
                            backend,
                            lease_seconds=0.12,
                        )
                    )
                )
                worker.start()
                self.assertTrue(solve_started.wait(timeout=1.0))
                time.sleep(0.16)
                self.assertIsNone(store.claim("worker-b", lease_seconds=0.12))
                release_solver.set()
                self.assertTrue(validation_started.wait(timeout=1.0))
                time.sleep(0.16)
                self.assertIsNone(store.claim("worker-b", lease_seconds=0.12))
                release_validation.set()
                worker.join(timeout=2.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(statuses, ["sat"])
            self.assertEqual(store.stats()["done"], 1)

    def test_solver_result_materializes_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "runtime-output"
            output.mkdir()
            store = QueryStore(root / "store")
            query_id, _ = store.ingest(
                _envelope(target_value=65, output_dir=str(output))
            )
            expected_smt2 = _envelope(target_value=65)["smt2"].encode("utf-8")
            helper = SubprocessSolver(
                (
                    sys.executable,
                    "-c",
                    (
                        "import hashlib,json,pathlib,sys;"
                        "content=pathlib.Path(sys.argv[1]).read_bytes();"
                        "print(json.dumps({'status':'sat','assignments':{'0':65},"
                        "'solver':'fd-helper','elapsed_us':10,"
                        "'artifact_sha256':hashlib.sha256(content).hexdigest()}))"
                    ),
                )
            )
            observed_leases: list[WorkLease] = []

            def backend(lease: WorkLease) -> dict:
                self.assertEqual(lease.query_id, query_id)
                self.assertTrue(lease.smt2_path.is_file())
                self.assertTrue(lease.has_sealed_artifacts)
                observed_leases.append(lease)
                lease.smt2_path.write_bytes(b"(assert false)\n")
                result = dict(helper(lease))
                self.assertEqual(
                    result["artifact_sha256"],
                    hashlib.sha256(expected_smt2).hexdigest(),
                )
                return result

            self.assertEqual(solve_one(store, "worker", backend), "sat")
            with self.assertRaisesRegex(ValueError, "lease is closed"):
                observed_leases[0].duplicate_artifacts("full")
            candidates = list((root / "store" / "candidates").rglob("*.bin"))
            self.assertEqual([path.read_bytes() for path in candidates], [b"A"])
            manifest = json.loads(
                candidates[0].with_suffix(".json").read_text(encoding="ascii")
            )
            self.assertEqual(manifest["query_input_offsets"], [0])
            self.assertEqual(manifest["model_assignment_offsets"], [0])
            self.assertTrue(manifest["query_ir_verified"])
            external = list(output.glob("async-*"))
            self.assertEqual([path.read_bytes() for path in external], [b"A"])
            external_manifest = output / f".{external[0].name}.query.json"
            self.assertTrue(external_manifest.is_file())
            self.assertEqual(
                json.loads(external_manifest.read_text(encoding="ascii"))["query_id"],
                query_id,
            )
            self.assertEqual(store.stats()["done"], 1)

    def test_selective_partition_telemetry_is_bounded_and_typed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            store.ingest(_partial_source_envelope())
            lease = store.claim("worker", lease_seconds=10)
            self.assertIsNotNone(lease)
            assert lease is not None
            result = {
                "status": "sat",
                "assignments": {"0": 66},
                "solver": "z3-selective-graph",
                "elapsed_us": 10,
                "selective_query_attempted": True,
                "selective_query_hit": True,
                "selective_query_partition_mode": "relation-graph-v1",
                "selective_query_relation_edges": 4,
                "selective_query_cut_weight": 1,
                "selective_query_smt_assertions": 3,
                "selective_query_random_assertions": 1,
                "selective_query_shared_variables": 1,
                "selective_query_partial_elapsed_us": 5,
                "selective_query_partial_status": "sat",
            }
            self.assertTrue(store.complete(lease, "worker", result))
            self.assertEqual(store.stats()["selective_query_graph_attempts"], 1)
            self.assertEqual(store.stats()["selective_query_graph_hits"], 1)

        for field, value, expected in (
            ("selective_query_partition_mode", "invented", "partition mode"),
            ("selective_query_partial_status", "maybe", "partial status"),
            ("selective_query_relation_edges", -1, "relation_edges"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                store = QueryStore(temporary)
                store.ingest(_partial_source_envelope())
                lease = store.claim("worker", lease_seconds=10)
                self.assertIsNotNone(lease)
                assert lease is not None
                invalid = {
                    "status": "sat",
                    "assignments": {"0": 66},
                    "solver": "test",
                    "elapsed_us": 1,
                    field: value,
                }
                with self.assertRaisesRegex(ValueError, expected):
                    store.complete(lease, "worker", invalid)

        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            store.ingest(_partial_source_envelope())
            lease = store.claim("worker", lease_seconds=10)
            self.assertIsNotNone(lease)
            assert lease is not None
            with self.assertRaisesRegex(ValueError, "not attributed"):
                store.complete(
                    lease,
                    "worker",
                    {
                        **result,
                        "solver": "z3",
                        "selective_query_hit": True,
                    },
                )

    def test_fake_sat_model_fails_query_ir_materialization_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(Path(temporary) / "store")
            query_id, _ = store.ingest(_envelope())
            paths = store.materialize_candidates(
                query_id,
                {
                    "status": "sat",
                    "assignments": {"0": 66},
                    "solver": "fake-contradictory",
                    "elapsed_us": 1,
                },
            )
            self.assertEqual(paths, [])
            self.assertEqual(list(store.candidate_dir.rglob("*.bin")), [])
            self.assertFalse(store.validate_candidate(query_id, b"B"))

    def test_public_query_ir_loader_requires_stable_bound_query_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = QueryStore(root / "store")
            query_id, _ = store.ingest(_partial_source_envelope())
            query_path = store.query_dir / query_id[:2] / f"{query_id}.json"
            original = query_path.read_bytes()
            outside = root / "outside-query.json"
            outside.write_bytes(original)
            query_path.unlink()
            query_path.symlink_to(outside)

            self.assertIsNone(store.load_query_ir(query_id))
            self.assertFalse(store.validate_candidate(query_id, b"B"))
            store.ingest(_partial_source_envelope())
            self.assertFalse(query_path.is_symlink())
            self.assertIsNotNone(store.load_query_ir(query_id))
            self.assertTrue(store.validate_candidate(query_id, b"B"))

    def test_one_shot_solver_bounds_streams_and_parses_strict_json(self):
        lease = WorkLease(
            "protocol-query",
            1,
            Path("query.smt2"),
            "prefix",
            Path("prefix.smt2"),
            Path("target.smt2"),
            1000,
        )
        invalid_responses = (
            (
                '{"status":"sat","status":"unsat","assignments":{}}',
                "duplicate JSON object member 'status'",
            ),
            (
                '{"status":"sat","assignments":{},"elapsed_us":NaN}',
                "non-finite JSON number 'NaN'",
            ),
        )
        for response, error in invalid_responses:
            with self.subTest(error=error):
                solver = SubprocessSolver(
                    (
                        sys.executable,
                        "-c",
                        f"import sys;sys.stdout.write({response!r})",
                    )
                )
                with self.assertRaisesRegex(RuntimeError, error):
                    solver(lease)

        oversized_stdout = SubprocessSolver(
            (sys.executable, "-c", "import sys;sys.stdout.write('x'*64)")
        )
        with mock.patch.object(
            query_store_module,
            "_MAX_SOLVER_RESPONSE_BYTES",
            32,
        ):
            with self.assertRaisesRegex(RuntimeError, "stdout exceeds 32 bytes"):
                oversized_stdout(lease)

        oversized_stderr = SubprocessSolver(
            (
                sys.executable,
                "-c",
                (
                    "import json,sys;sys.stderr.write('x'*64);"
                    "print(json.dumps({'status':'sat','assignments':{}}))"
                ),
            )
        )
        with mock.patch.object(
            query_store_module,
            "_MAX_SOLVER_DIAGNOSTIC_BYTES",
            32,
        ):
            with self.assertRaisesRegex(RuntimeError, "stderr exceeds 32 bytes"):
                oversized_stderr(lease)

    def test_helper_interrupt_reaps_descendants_after_leader_exit(self):
        script = (
            "import subprocess,sys;"
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import time;time.sleep(30)']);"
            "print(child.pid,flush=True)"
        )
        process = subprocess.Popen(
            (sys.executable, "-c", script),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        assert process.stdout is not None
        child_pid = int(process.stdout.readline())
        process.wait(timeout=1.0)
        self.assertTrue(Path(f"/proc/{child_pid}").exists())
        self.assertTrue(
            query_store_module._interrupt_process(process, grace_seconds=0.02)
        )
        deadline = time.monotonic() + 1.0
        while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(Path(f"/proc/{child_pid}").exists())
        process.stdout.close()

    def test_sat_completion_requires_store_verified_query_ir_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(Path(temporary) / "store")
            query_id, _ = store.ingest(_partial_source_envelope())
            lease = store.claim("verification-worker")
            self.assertIsNotNone(lease)
            assert lease is not None
            query_path = store.query_dir / query_id[:2] / f"{query_id}.json"
            query_path.unlink()
            sat_result = {
                "status": "sat",
                "assignments": {"0": 66},
                "solver": "generic-json-helper",
                "elapsed_us": 10,
            }

            with self.assertRaisesRegex(ValueError, "body failed stable validation"):
                store.complete(
                    lease,
                    "verification-worker",
                    sat_result,
                )
            self.assertEqual(store.stats()["leased"], 1)
            self.assertEqual(store.stats()["results"], 0)
            self.assertEqual(store.materialize_candidates(query_id, sat_result), [])
            self.assertEqual(list(store.candidate_dir.rglob("*.bin")), [])

            repaired_id, created = store.ingest(_partial_source_envelope())
            self.assertEqual(repaired_id, query_id)
            self.assertFalse(created)
            with self.assertRaisesRegex(
                ValueError,
                "SAT result failed independent store validation",
            ):
                store.complete(
                    lease,
                    "verification-worker",
                    {**sat_result, "assignments": {"0": 65}},
                )
            self.assertEqual(store.stats()["leased"], 1)
            self.assertTrue(
                store.complete(lease, "verification-worker", sat_result)
            )
            result = json.loads(
                (store.result_dir / query_id[:2] / f"{query_id}.json").read_text(
                    encoding="ascii"
                )
            )
            self.assertTrue(result["store_model_verified"])
            self.assertEqual(
                [path.read_bytes() for path in store.candidate_dir.rglob("*.bin")],
                [b"B"],
            )

    def test_persists_generator_and_materializes_fixed_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = QueryStore(root / "store")
            query_id, _ = store.ingest(_partial_source_envelope())

            def backend(_lease: WorkLease) -> dict:
                return {
                    "status": "sat",
                    "assignments": {"0": 65},
                    "solver": "fake-generator",
                    "elapsed_us": 10,
                    "generator": {
                        "schema": "symcc-solution-generator-v1",
                        "query_id": query_id,
                        "input_size": 0,
                        "seed": 7,
                        "fixed": {"0": 66},
                        "ranges": [],
                        "fields": [],
                        "converter_chain": [],
                        "verified_models": [],
                        "metrics": {
                            "range_checks": 0,
                            "sample_attempts": 0,
                            "valid_models": 0,
                        },
                    },
                }

            self.assertEqual(solve_one(store, "worker", backend), "sat")
            result = json.loads(
                (store.result_dir / query_id[:2] / f"{query_id}.json").read_text(
                    encoding="ascii"
                )
            )
            self.assertEqual(
                result["generator"]["converter_chain"][0]["assignments"],
                {"0": 66},
            )
            self.assertEqual(len(result["generator_hash"]), 64)
            generator_path = (
                store.generator_dir
                / result["generator_hash"][:2]
                / f"{result['generator_hash']}.json"
            )
            self.assertTrue(generator_path.is_file())
            candidates = sorted(
                path.read_bytes() for path in store.candidate_dir.rglob("*.bin")
            )
            self.assertEqual(candidates, [b"B"])
            self.assertEqual(store.stats()["generators"], 1)
            self.assertEqual(store.stats()["generator_models"], 0)

    def test_replays_generator_with_query_ir_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = QueryStore(root / "store")
            query_id, _ = store.ingest(_partial_range_envelope())

            def backend(_lease: WorkLease) -> dict:
                return {
                    "status": "sat",
                    "assignments": {"0": 65},
                    "solver": "fake-generator-replay",
                    "elapsed_us": 10,
                    "generator": {
                        "schema": "symcc-solution-generator-v1",
                        "query_id": query_id,
                        "input_size": 0,
                        "seed": 7,
                        "fixed": {},
                        "ranges": [[0, 64, 66]],
                        "fields": [[0]],
                        "converter_chain": [],
                        "verified_models": [],
                        "metrics": {},
                    },
                }

            self.assertEqual(solve_one(store, "worker", backend), "sat")
            candidates = sorted(
                path.read_bytes() for path in store.candidate_dir.rglob("*.bin")
            )
            self.assertEqual(candidates, [b"@", b"A", b"B"])
            replay_manifest = json.loads(
                next(
                    path
                    for path in store.candidate_dir.rglob("*.json")
                    if json.loads(path.read_text(encoding="ascii")).get(
                        "candidate_source"
                    )
                    == "generator-query-ir-replay"
                ).read_text(encoding="ascii")
            )
            self.assertTrue(replay_manifest["generator_replay_verified"])
            self.assertTrue(replay_manifest["query_ir_verified"])
            self.assertFalse(replay_manifest["solver_verified"])

    def test_partial_solution_cache_materializes_verified_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "runtime-output"
            output.mkdir()
            store = QueryStore(root / "store")
            source_id, _ = store.ingest(_partial_source_envelope())

            def backend(_lease: WorkLease) -> dict:
                return {
                    "status": "sat",
                    "assignments": {"0": 66},
                    "solver": "fake",
                    "elapsed_us": 10,
                }

            self.assertEqual(solve_one(store, "worker", backend), "sat")
            target_id, created = store.ingest(
                _partial_range_envelope(output_dir=str(output))
            )
            self.assertTrue(created)
            self.assertNotEqual(source_id, target_id)

            stats = store.stats()
            self.assertEqual(stats["partial_solutions"], 1)
            self.assertEqual(stats["partial_solution_candidates"], 1)
            self.assertGreaterEqual(stats["partial_solution_clause_links"], 1)
            external = list(output.glob("async-partial-*"))
            self.assertEqual([path.read_bytes() for path in external], [b"B"])
            partial_manifests = list(store.candidate_dir.rglob("*.partial-*.json"))
            self.assertEqual(len(partial_manifests), 1)
            manifest = json.loads(partial_manifests[0].read_text(encoding="ascii"))
            self.assertEqual(manifest["query_id"], target_id)
            self.assertEqual(manifest["partial_solution_source_query_id"], source_id)
            self.assertFalse(manifest["solver_verified"])
            self.assertTrue(manifest["partial_solution_verified"])
            self.assertTrue(manifest["query_ir_verified"])
            lease = store.claim("still-needs-exact-solver")
            self.assertIsNotNone(lease)
            assert lease is not None
            self.assertEqual(lease.query_id, target_id)

    def test_partial_solution_cache_rejects_unverified_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            store.ingest(_partial_source_envelope())

            def backend(_lease: WorkLease) -> dict:
                return {
                    "status": "sat",
                    "assignments": {"0": 66},
                    "solver": "fake",
                    "elapsed_us": 10,
                }

            self.assertEqual(solve_one(store, "worker", backend), "sat")
            store.ingest(_partial_range_envelope(lower=67, upper=67))
            stats = store.stats()
            self.assertEqual(stats["partial_solutions"], 1)
            self.assertEqual(stats["partial_solution_candidates"], 0)
            self.assertGreaterEqual(stats["partial_solution_clause_links"], 1)
            self.assertEqual(list(store.candidate_dir.rglob("*.partial-*.json")), [])

    def test_conflict_solution_is_verified_indexed_and_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "runtime-output"
            output.mkdir()
            store = QueryStore(root / "store")
            source_id, _ = store.ingest(_partial_source_envelope())

            def backend(_lease: WorkLease) -> dict:
                return {
                    "status": "sat",
                    "assignments": {"0": 66},
                    "solver": "fake-conflict-core",
                    "elapsed_us": 10,
                    "solver_pscache_conflict_checks": 3,
                    "solver_pscache_conflict_solutions": [
                        {
                            "schema": "symcc-solver-conflict-solution-v1",
                            "assignments": {"0": 0},
                            "core_assignments": {"0": 0},
                            "source": "concrete-witness",
                            "proof": "z3-assumption-unsat-core-v1",
                            "proof_verified": True,
                            "core_minimal": True,
                            "proof_checks": 2,
                        }
                    ],
                }

            self.assertEqual(solve_one(store, "worker", backend), "sat")
            target_id, _ = store.ingest(
                _partial_range_envelope(lower=0, upper=0, output_dir=str(output))
            )
            self.assertNotEqual(source_id, target_id)
            stats = store.stats()
            self.assertEqual(stats["conflict_partial_solutions"], 1)
            self.assertEqual(stats["solver_pscache_conflict_solutions"], 1)
            self.assertEqual(stats["solver_pscache_conflict_checks"], 3)
            self.assertGreaterEqual(stats["partial_solution_literal_links"], 2)
            conflict_manifests = [
                json.loads(path.read_text(encoding="ascii"))
                for path in store.candidate_dir.rglob("*.partial-*.json")
                if json.loads(path.read_text(encoding="ascii")).get(
                    "partial_solution_provenance"
                )
                == "z3-assumption-conflict"
            ]
            self.assertEqual(len(conflict_manifests), 1)
            manifest = conflict_manifests[0]
            self.assertEqual(manifest["query_id"], target_id)
            self.assertTrue(manifest["partial_solution_source_verified"])
            self.assertTrue(manifest["query_ir_verified"])
            self.assertEqual(
                [path.read_bytes() for path in output.glob("async-partial-*")],
                [b"\x00"],
            )

    def test_conflict_solution_protocol_rejects_invalid_core(self):
        base = {
            "status": "sat",
            "assignments": {"0": 66},
            "solver": "fake-conflict-core",
            "elapsed_us": 10,
            "solver_pscache_conflict_checks": 1,
            "solver_pscache_conflict_solutions": [
                {
                    "schema": "symcc-solver-conflict-solution-v1",
                    "assignments": {"0": 0},
                    "core_assignments": {"0": 1},
                    "source": "concrete-witness",
                    "proof": "z3-assumption-unsat-core-v1",
                    "proof_verified": True,
                    "core_minimal": False,
                    "proof_checks": 1,
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "core must be a subset"):
            QueryStore._validate_result(base)
        base["solver_pscache_conflict_solutions"][0]["core_assignments"] = {}
        with self.assertRaisesRegex(ValueError, "require assignments and a core"):
            QueryStore._validate_result(base)
        base["solver_pscache_conflict_solutions"][0]["core_assignments"] = {"0": 0}
        base["solver_pscache_conflict_solutions"][0]["proof_verified"] = False
        with self.assertRaisesRegex(ValueError, "requires a verified Z3 core"):
            QueryStore._validate_result(base)
        base["solver_pscache_conflict_solutions"][0]["proof_verified"] = True
        base["solver_pscache_conflict_solutions"][0]["assignments"] = {"0": 0, "00": 0}
        with self.assertRaisesRegex(ValueError, "duplicate offsets"):
            QueryStore._validate_result(base)

    def test_schedule_artifact_joint_query_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "joint.jsonl"
            store = QueryStore(root / "store")
            query_id, _ = store.ingest(_partial_source_envelope(target_value=66))

            pending = store.validate_schedule_artifact(
                _schedule_artifact(target_branch=321),
                output_path=output,
            )
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["schema"], JOINT_SCHEDULE_QUERY_SCHEMA)
            self.assertEqual(pending[0]["joint_status"], "path_target_unsolved")
            self.assertEqual(pending[0]["path_validation"]["status"], "unknown")
            self.assertTrue(pending[0]["path_validation"]["prefix_observed"])
            self.assertFalse(pending[0]["path_validation"]["target_satisfied"])
            self.assertEqual(pending[0]["query"]["matched_by"], "target_branch")
            self.assertEqual(pending[0]["schedule"]["provenance_counts"], {"module": 2})

            lease = store.claim("solver")
            self.assertIsNotNone(lease)
            assert lease is not None
            self.assertEqual(lease.query_id, query_id)
            self.assertTrue(
                store.complete(
                    lease,
                    "solver",
                    {
                        "status": "sat",
                        "assignments": {"0": 66},
                        "solver": "fake",
                        "elapsed_us": 10,
                    },
                )
            )
            ready = store.validate_schedule_artifact(
                _schedule_artifact(query_id=query_id, target_branch=999),
                output_path=output,
            )
            self.assertEqual(len(ready), 1)
            self.assertEqual(ready[0]["joint_status"], "ready")
            self.assertEqual(ready[0]["path_validation"]["status"], "sat")
            self.assertEqual(ready[0]["query"]["matched_by"], "query_id")
            self.assertEqual(ready[0]["path_validation"]["models_checked"], 1)

            no_replay = store.validate_schedule_artifact(
                _schedule_artifact(
                    replay_prefixes=[],
                    query_id=query_id,
                    target_branch=999,
                ),
                output_path=output,
            )
            self.assertEqual(no_replay[0]["joint_status"], "schedule_no_replay")
            rows = [
                json.loads(line)
                for line in output.read_text(encoding="ascii").splitlines()
            ]
            self.assertEqual(len(rows), 3)
            self.assertTrue(
                all(row["schema"] == JOINT_SCHEDULE_QUERY_SCHEMA for row in rows)
            )
            stats = store.stats()
            self.assertEqual(stats["joint_schedule_validations"], 3)
            self.assertEqual(stats["joint_schedule_ready"], 1)

    def test_query_store_solves_path_schedule_and_read_from_jointly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "joint-smt.jsonl"
            store = QueryStore(root / "store")
            query_id, _ = store.ingest(_partial_source_envelope(target_value=66))
            schedule = schedule_smt_artifact(
                parse_schedule_trace(
                    "0 1 write 0x100 value=0x99\n1 2 read 0x100 sym-byte=0 init=0x42\n"
                )
            )
            with mock.patch.object(
                store,
                "artifact_path",
                side_effect=AssertionError("mutable artifact path used"),
            ):
                result = store.solve_joint_schedule_smt(
                    schedule,
                    query_id,
                    output_path=output,
                )
            self.assertEqual(result["status"], "sat")
            self.assertTrue(result["query_ir_model_verified"])
            self.assertEqual(result["model"]["input_bytes"], {"0": 66})
            self.assertEqual(result["model"]["read_from"], {"rf_1": -1})
            self.assertEqual(store.stats()["joint_smt_solves"], 1)
            self.assertEqual(store.stats()["joint_smt_verified"], 1)
            persisted = json.loads(output.read_text(encoding="ascii"))
            self.assertEqual(
                persisted["result_sha256"],
                result["result_sha256"],
            )

            contradictory = schedule_smt_artifact(
                parse_schedule_trace(
                    "0 1 write 0x100 value=0x42\n1 2 read 0x100 sym-byte=0 init=0x00\n"
                )
            )
            unsat = store.solve_joint_schedule_smt(
                contradictory,
                query_id,
            )
            self.assertEqual(unsat["status"], "unsat")
            self.assertFalse(unsat["query_ir_model_verified"])
            self.assertEqual(store.stats()["joint_smt_solves"], 2)

    def test_portfolio_solver_persists_attempt_telemetry(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            query_id, _ = store.ingest(_partial_source_envelope())
            solver = PortfolioSolver(
                (
                    (
                        "timeout-profile",
                        lambda _lease: {
                            "status": "unknown",
                            "assignments": {},
                            "solver": "timeout-profile",
                            "elapsed_us": 100,
                        },
                    ),
                    (
                        "z3",
                        lambda _lease: {
                            "status": "sat",
                            "assignments": {"0": 66},
                            "solver": "z3",
                            "elapsed_us": 10,
                        },
                    ),
                )
            )

            self.assertEqual(solve_one(store, "worker", solver), "sat")
            result = json.loads(
                (store.result_dir / query_id[:2] / f"{query_id}.json").read_text(
                    encoding="ascii"
                )
            )
            self.assertEqual(result["portfolio"]["winner"], "z3")
            self.assertFalse(result["portfolio"]["disagreement"])
            self.assertEqual(
                [row["status"] for row in result["portfolio"]["attempts"]],
                ["unknown", "sat"],
            )
            self.assertEqual(store.stats()["portfolio_results"], 1)

    def test_portfolio_solver_rejects_sat_unsat_disagreement(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            query_id, _ = store.ingest(_envelope())
            solver = PortfolioSolver(
                (
                    (
                        "z3-a",
                        lambda _lease: {
                            "status": "sat",
                            "assignments": {"0": 66},
                            "solver": "z3-a",
                            "elapsed_us": 10,
                        },
                    ),
                    (
                        "z3-b",
                        lambda _lease: {
                            "status": "unsat",
                            "assignments": {},
                            "solver": "z3-b",
                            "elapsed_us": 11,
                        },
                    ),
                )
            )

            self.assertEqual(solve_one(store, "worker", solver), "unknown")
            result = json.loads(
                (store.result_dir / query_id[:2] / f"{query_id}.json").read_text(
                    encoding="ascii"
                )
            )
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(result["portfolio"]["disagreement"])
            self.assertEqual(store.stats()["portfolio_disagreements"], 1)

    def test_portfolio_solver_runs_backends_in_parallel(self):
        barrier = threading.Barrier(2)
        lease = WorkLease(
            "query",
            1,
            Path("query.smt2"),
            "prefix",
            Path("prefix.smt2"),
            Path("target.smt2"),
            1000,
        )

        def backend(status: str, solver_name: str) -> dict:
            barrier.wait(timeout=1.0)
            return {
                "status": status,
                "assignments": {"0": 66} if status == "sat" else {},
                "solver": solver_name,
                "elapsed_us": 10,
            }

        solver = PortfolioSolver(
            (
                ("slow-profile", lambda item: backend("unknown", "slow-profile")),
                ("z3", lambda item: backend("sat", "z3")),
            ),
            parallelism=2,
        )
        result = solver(lease)
        self.assertEqual(result["status"], "sat")
        self.assertEqual(result["portfolio"]["mode"], "parallel")
        self.assertEqual(result["portfolio"]["parallelism"], 2)
        self.assertFalse(result["portfolio"]["cancellation_enabled"])
        self.assertTrue(result["portfolio"]["consensus_complete"])
        self.assertEqual(
            [row["status"] for row in result["portfolio"]["attempts"]],
            ["unknown", "sat"],
        )

    def test_portfolio_interrupts_running_one_shot_helper_after_sat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "started"
            script = (
                "import json,pathlib,time;"
                f"pathlib.Path({str(marker)!r}).write_text('1');"
                "time.sleep(5);"
                "print(json.dumps({'status':'unknown','assignments':{}}))"
            )
            slow = SubprocessSolver((sys.executable, "-c", script))
            store = QueryStore(root / "store")
            store.ingest(_partial_source_envelope())
            lease = store.claim("cancel-worker")
            self.assertIsNotNone(lease)
            assert lease is not None

            def fast(_lease: WorkLease) -> dict:
                deadline = time.monotonic() + 1.0
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                return {
                    "status": "sat",
                    "assignments": {"0": 66},
                    "solver": "fast",
                }

            solver = PortfolioSolver(
                (
                    ("slow-process", slow),
                    ("fast", fast),
                ),
                parallelism=2,
                cancel_grace_ms=0,
            )
            started = time.monotonic()
            result = solver(lease)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(result["status"], "sat")
            self.assertTrue(result["portfolio"]["cancel_requested"])
            self.assertEqual(result["portfolio"]["cancelled_attempts"], 1)
            self.assertFalse(result["portfolio"]["consensus_complete"])
            self.assertTrue(result["portfolio"]["attempts"][0]["cancelled"])
            self.assertTrue(store.complete(lease, "cancel-worker", result))
            stats = store.stats()
            self.assertEqual(stats["portfolio_cancel_requests"], 1)
            self.assertEqual(stats["portfolio_cancelled_attempts"], 1)
            self.assertEqual(stats["portfolio_incomplete_consensus"], 1)

    def test_portfolio_grace_observes_fast_sat_unsat_disagreement(self):
        lease = WorkLease(
            "disagreement-query",
            1,
            Path("query.smt2"),
            "prefix",
            Path("prefix.smt2"),
            Path("target.smt2"),
            1000,
        )

        def delayed(status: str, delay: float) -> dict:
            time.sleep(delay)
            return {
                "status": status,
                "assignments": {"0": 66} if status == "sat" else {},
                "solver": status,
            }

        solver = PortfolioSolver(
            (
                ("sat", lambda _: delayed("sat", 0.005)),
                ("unsat", lambda _: delayed("unsat", 0.02)),
            ),
            parallelism=2,
            cancel_grace_ms=100,
        )
        result = solver(lease)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["portfolio"]["disagreement"])
        self.assertEqual(result["portfolio"]["cancelled_attempts"], 0)
        self.assertTrue(result["portfolio"]["consensus_complete"])

    def test_portfolio_cancel_grace_does_not_wait_for_uncancellable_tail(self):
        lease = WorkLease(
            "bounded-cancel-query",
            1,
            Path("query.smt2"),
            "prefix",
            Path("prefix.smt2"),
            Path("target.smt2"),
            1000,
        )
        slow_started = threading.Event()

        def slow(_lease: WorkLease) -> dict:
            slow_started.set()
            time.sleep(0.4)
            return {
                "status": "unknown",
                "assignments": {},
                "solver": "slow",
            }

        def fast(_lease: WorkLease) -> dict:
            self.assertTrue(slow_started.wait(timeout=1.0))
            return {
                "status": "sat",
                "assignments": {"0": 66},
                "solver": "fast",
            }

        started = time.monotonic()
        result = PortfolioSolver(
            (
                ("slow", slow),
                ("fast", fast),
            ),
            parallelism=2,
            cancel_grace_ms=5,
        )(lease)
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertEqual(result["status"], "sat")
        self.assertTrue(result["portfolio"]["cancel_requested"])
        self.assertEqual(result["portfolio"]["cancelled_attempts"], 1)
        self.assertTrue(result["portfolio"]["attempts"][0]["cancelled"])

    def test_portfolio_cooperative_cancel_cleanup_replaces_placeholder(self):
        lease = WorkLease(
            "cooperative-cancel-query",
            1,
            Path("query.smt2"),
            "prefix",
            Path("prefix.smt2"),
            Path("target.smt2"),
            1000,
        )
        slow_started = threading.Event()
        release_slow = threading.Event()

        class CooperativeSolver:
            def __call__(self, _lease: WorkLease) -> dict:
                slow_started.set()
                self.assert_released()
                return {
                    "status": "sat",
                    "assignments": {"0": 67},
                    "solver": "cooperative",
                }

            def assert_released(self) -> None:
                if not release_slow.wait(timeout=1.0):
                    raise RuntimeError("cooperative cancel was not delivered")

            def cancel(self, _lease: WorkLease) -> bool:
                release_slow.set()
                return True

        cooperative = CooperativeSolver()

        def fast(_lease: WorkLease) -> dict:
            self.assertTrue(slow_started.wait(timeout=1.0))
            return {
                "status": "sat",
                "assignments": {"0": 66},
                "solver": "fast",
            }

        result = PortfolioSolver(
            (
                ("cooperative", cooperative),
                ("fast", fast),
            ),
            parallelism=2,
            cancel_grace_ms=0,
        )(lease)
        self.assertEqual(result["status"], "sat")
        self.assertTrue(result["portfolio"]["cancel_requested"])
        self.assertEqual(result["portfolio"]["cancelled_attempts"], 0)
        self.assertFalse(result["portfolio"]["attempts"][0]["cancelled"])
        self.assertFalse(result["portfolio"]["attempts"][1]["cancelled"])

    def test_cancelled_persistent_helper_restarts_cold(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "persistent-started"
            duplicate_ready = Path(temporary) / "duplicate-response-ready"
            script = (
                "import array,fcntl,hashlib,json,os,pathlib,socket,sys,time;"
                "\nchannel=socket.socket(fileno=int(os.environ['SYMCC_QUERY_FD_CHANNEL']))"
                "\nfor line in sys.stdin:"
                "\n fields=line.rstrip('\\n').split('\\t'); q=fields[0]"
                "\n if q == 'duplicate-id':"
                "\n  print(json.dumps({'request_id':q,'status':'sat',"
                "'assignments':{'0':66},'solver':'persistent',"
                "'helper_pid':os.getpid()}),flush=True)"
                "\n  time.sleep(0.05)"
                "\n  print(json.dumps({'request_id':q,'status':'sat',"
                "'assignments':{'0':77},'solver':'stale-duplicate',"
                "'helper_pid':os.getpid()}),flush=True)"
                f"\n  pathlib.Path({str(duplicate_ready)!r}).write_text(str(os.getpid()))"
                "\n  continue"
                "\n if q == 'wrong-id': print(json.dumps({'request_id':'other',"
                "'status':'sat','assignments':{}}),flush=True); continue"
                "\n if q == 'oversized': print('x'*64,flush=True); continue"
                f"\n if q == 'slow': pathlib.Path({str(marker)!r}).write_text('1');"
                "\n if q == 'slow': time.sleep(5)"
                "\n elif fields[3] == '@symcc-fd:prefix':"
                "\n  msg,anc,flags,_=channel.recvmsg(256,socket.CMSG_SPACE(8))"
                "\n  rights=array.array('i')"
                "\n  for level,kind,data in anc:"
                "\n   if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:"
                "\n    rights.frombytes(data[:len(data)-(len(data)%rights.itemsize)])"
                "\n  contents=[os.pread(fd,os.fstat(fd).st_size,0) for fd in rights]"
                "\n  seals=[fcntl.fcntl(fd,fcntl.F_GET_SEALS) for fd in rights]"
                "\n  [os.close(fd) for fd in rights]"
                "\n  print(json.dumps({'request_id':q,'status':'sat',"
                "'assignments':{'0':66},'solver':'persistent-fd',"
                "'fd_request_id':msg.decode('ascii'),"
                "'helper_pid':os.getpid(),"
                "'artifact_sha256':[hashlib.sha256(v).hexdigest() for v in contents],"
                "'artifact_seals':seals}),flush=True)"
                "\n else: print(json.dumps({'request_id':q,'status':'sat',"
                "'assignments':{'0':66},'solver':'persistent',"
                "'helper_pid':os.getpid()}),flush=True)"
            )
            persistent = PersistentSubprocessSolver((sys.executable, "-c", script))
            slow_lease = WorkLease(
                "slow",
                1,
                Path("query.smt2"),
                "prefix",
                Path("prefix.smt2"),
                Path("target.smt2"),
                10000,
            )

            def fast(_lease: WorkLease) -> dict:
                deadline = time.monotonic() + 1.0
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                return {
                    "status": "sat",
                    "assignments": {"0": 66},
                    "solver": "fast",
                }

            try:
                result = PortfolioSolver(
                    (
                        ("persistent", persistent),
                        ("fast", fast),
                    ),
                    parallelism=2,
                    cancel_grace_ms=0,
                )(slow_lease)
                self.assertEqual(result["portfolio"]["cancelled_attempts"], 1)
                restarted = persistent(
                    WorkLease(
                        "next",
                        1,
                        Path("query.smt2"),
                        "prefix",
                        Path("prefix.smt2"),
                        Path("target.smt2"),
                        1000,
                    )
                )
                self.assertEqual(restarted["status"], "sat")
                self.assertEqual(restarted["assignments"], {"0": 66})

                prevalidation_generation = persistent.process.pid
                bad_artifact_paths = (
                    (
                        Path("prefix\nvictim\t1\tinjected\tprefix.smt2"),
                        Path("target.smt2"),
                    ),
                    (Path("prefix.smt2"), Path("target\tinjected.smt2")),
                    (Path("prefix\rinjected.smt2"), Path("target.smt2")),
                    (Path("prefix\x00truncated.smt2"), Path("target.smt2")),
                )
                for prefix_path, target_path in bad_artifact_paths:
                    with self.subTest(
                        prefix_path=prefix_path,
                        target_path=target_path,
                    ):
                        with self.assertRaisesRegex(ValueError, "protocol fields"):
                            persistent(
                                WorkLease(
                                    "invalid-artifact-path",
                                    1,
                                    Path("query.smt2"),
                                    "prefix",
                                    prefix_path,
                                    target_path,
                                    1000,
                                )
                            )
                        self.assertEqual(
                            persistent.process.pid,
                            prevalidation_generation,
                        )
                        self.assertIsNone(persistent.process.poll())
                invalid_timeouts = (0, 3_600_001, "1000", True)
                for invalid_timeout in invalid_timeouts:
                    with self.subTest(invalid_timeout=invalid_timeout):
                        with self.assertRaisesRegex(ValueError, "timeout"):
                            persistent(
                                WorkLease(
                                    "invalid-timeout",
                                    1,
                                    Path("query.smt2"),
                                    "prefix",
                                    Path("prefix.smt2"),
                                    Path("target.smt2"),
                                    invalid_timeout,  # type: ignore[arg-type]
                                )
                            )
                        self.assertEqual(
                            persistent.process.pid,
                            prevalidation_generation,
                        )
                        self.assertIsNone(persistent.process.poll())
                with self.assertRaisesRegex(ValueError, "valid UTF-8"):
                    persistent(
                        WorkLease(
                            "invalid-unicode",
                            1,
                            Path("query.smt2"),
                            "prefix",
                            Path("prefix.smt2"),
                            Path("target.smt2"),
                            1000,
                            "\ud800",
                        )
                    )
                with mock.patch.object(
                    query_store_module,
                    "_MAX_SOLVER_REQUEST_BYTES",
                    32,
                ):
                    with self.assertRaisesRegex(ValueError, "request exceeds"):
                        persistent(
                            WorkLease(
                                "oversized-request",
                                1,
                                Path("query.smt2"),
                                "prefix",
                                Path("prefix.smt2"),
                                Path("target.smt2"),
                                1000,
                            )
                        )
                self.assertEqual(
                    persistent.process.pid,
                    prevalidation_generation,
                )
                self.assertIsNone(persistent.process.poll())
                with (
                    mock.patch.object(
                        query_store_module.time,
                        "monotonic",
                        side_effect=(1.0, 1.6),
                    ),
                    mock.patch.object(query_store_module.os, "set_blocking"),
                    mock.patch.object(
                        query_store_module.os,
                        "write",
                        return_value=1,
                    ) as progressing_write,
                ):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        persistent._write_request(
                            persistent.process,
                            b"abc",
                            deadline=1.5,
                            timeout_seconds=0.5,
                        )
                progressing_write.assert_called_once()
                after_path_prevalidation = persistent(
                    WorkLease(
                        "after-path-prevalidation",
                        1,
                        Path("query.smt2"),
                        "prefix",
                        Path("prefix.smt2"),
                        Path("target.smt2"),
                        1000,
                    )
                )
                self.assertEqual(
                    after_path_prevalidation["helper_pid"],
                    prevalidation_generation,
                )

                duplicate_generation = persistent.process.pid
                first_duplicate_id = persistent(
                    WorkLease(
                        "duplicate-id",
                        1,
                        Path("query.smt2"),
                        "prefix",
                        Path("prefix.smt2"),
                        Path("target.smt2"),
                        1000,
                    )
                )
                wait_deadline = time.monotonic() + 1.0
                while not duplicate_ready.exists() and time.monotonic() < wait_deadline:
                    time.sleep(0.005)
                self.assertTrue(duplicate_ready.exists())
                repeated_duplicate_id = persistent(
                    WorkLease(
                        "duplicate-id",
                        2,
                        Path("query.smt2"),
                        "prefix",
                        Path("prefix.smt2"),
                        Path("target.smt2"),
                        1000,
                    )
                )
                self.assertEqual(first_duplicate_id["assignments"], {"0": 66})
                self.assertEqual(
                    repeated_duplicate_id["assignments"],
                    {"0": 66},
                )
                self.assertEqual(
                    first_duplicate_id["helper_pid"],
                    duplicate_generation,
                )
                self.assertNotEqual(
                    repeated_duplicate_id["helper_pid"],
                    duplicate_generation,
                )
                bounded_generation = persistent.process.pid
                with mock.patch.object(
                    query_store_module,
                    "_MAX_PERSISTENT_GENERATION_REQUESTS",
                    1,
                ):
                    bounded_result = persistent(
                        WorkLease(
                            "bounded-history",
                            1,
                            Path("query.smt2"),
                            "prefix",
                            Path("prefix.smt2"),
                            Path("target.smt2"),
                            1000,
                        )
                    )
                self.assertNotEqual(
                    bounded_result["helper_pid"],
                    bounded_generation,
                )

                mismatched_generation = persistent.process.pid
                with self.assertRaisesRegex(RuntimeError, "response id mismatch"):
                    persistent(
                        WorkLease(
                            "wrong-id",
                            1,
                            Path("query.smt2"),
                            "prefix",
                            Path("prefix.smt2"),
                            Path("target.smt2"),
                            1000,
                        )
                    )
                self.assertIsNotNone(persistent.process.poll())
                after_mismatch = persistent(
                    WorkLease(
                        "after-wrong-id",
                        1,
                        Path("query.smt2"),
                        "prefix",
                        Path("prefix.smt2"),
                        Path("target.smt2"),
                        1000,
                    )
                )
                self.assertNotEqual(
                    after_mismatch["helper_pid"],
                    mismatched_generation,
                )

                oversized_generation = persistent.process.pid
                with mock.patch.object(
                    query_store_module,
                    "_MAX_SOLVER_RESPONSE_BYTES",
                    16,
                ):
                    with self.assertRaisesRegex(RuntimeError, "response"):
                        persistent(
                            WorkLease(
                                "oversized",
                                1,
                                Path("query.smt2"),
                                "prefix",
                                Path("prefix.smt2"),
                                Path("target.smt2"),
                                1000,
                            )
                        )
                self.assertIsNotNone(persistent.process.poll())
                after_oversized = persistent(
                    WorkLease(
                        "after-oversized",
                        1,
                        Path("query.smt2"),
                        "prefix",
                        Path("prefix.smt2"),
                        Path("target.smt2"),
                        1000,
                    )
                )
                self.assertNotEqual(
                    after_oversized["helper_pid"],
                    oversized_generation,
                )

                store = QueryStore(Path(temporary) / "fd-store")
                store.ingest(_partial_source_envelope())
                fd_lease = store.claim("persistent-fd")
                self.assertIsNotNone(fd_lease)
                assert fd_lease is not None
                descriptor_generation = persistent.process.pid
                for invalid_request_id in ("not-ascii-\u00e9", "q" * 257):
                    with self.subTest(invalid_request_id=invalid_request_id[:16]):
                        with self.assertRaisesRegex(ValueError, "request id"):
                            persistent(
                                WorkLease(
                                    invalid_request_id,
                                    fd_lease.token,
                                    fd_lease.smt2_path,
                                    fd_lease.prefix_key,
                                    fd_lease.prefix_smt2_path,
                                    fd_lease.target_smt2_path,
                                    fd_lease.timeout_ms,
                                    fd_lease.input_hex,
                                    fd_lease._sealed_artifacts,
                                )
                            )
                        self.assertEqual(
                            persistent.process.pid,
                            descriptor_generation,
                        )
                        self.assertIsNone(persistent.process.poll())
                expected_hashes = [
                    hashlib.sha256(
                        _partial_source_envelope()[name].encode("utf-8")
                    ).hexdigest()
                    for name in ("prefix_smt2", "target_smt2")
                ]
                fd_lease.prefix_smt2_path.write_bytes(b"(assert false)\n")
                fd_lease.target_smt2_path.write_bytes(b"(assert false)\n")
                fd_result = dict(persistent(fd_lease))
                self.assertEqual(fd_result["fd_request_id"], fd_lease.query_id)
                self.assertEqual(fd_result["artifact_sha256"], expected_hashes)
                self.assertEqual(len(fd_result["artifact_seals"]), 2)
                self.assertTrue(
                    all(seals & 15 == 15 for seals in fd_result["artifact_seals"])
                )
                self.assertTrue(store.complete(fd_lease, "persistent-fd", fd_result))

                store.ingest(_partial_source_envelope(target_value=65))
                failed_lease = store.claim("transport-failure")
                self.assertIsNotNone(failed_lease)
                assert failed_lease is not None
                store.ingest(_partial_source_envelope(target_value=67))
                recovery_lease = store.claim("transport-recovery")
                self.assertIsNotNone(recovery_lease)
                assert recovery_lease is not None

                failed_generation = persistent.process.pid
                artifact_fields = persistent._artifact_fields

                def send_then_fail(lease: WorkLease) -> tuple[str, str]:
                    artifact_fields(lease)
                    raise OSError("injected failure after descriptor transfer")

                with mock.patch.object(
                    persistent,
                    "_artifact_fields",
                    side_effect=send_then_fail,
                ):
                    with self.assertRaisesRegex(OSError, "after descriptor transfer"):
                        persistent(failed_lease)
                self.assertIsNotNone(persistent.process.poll())

                recovered = dict(persistent(recovery_lease))
                self.assertNotEqual(recovered["helper_pid"], failed_generation)
                self.assertEqual(
                    recovered["fd_request_id"],
                    recovery_lease.query_id,
                )
                self.assertEqual(
                    recovered["artifact_sha256"],
                    [
                        hashlib.sha256(
                            _partial_source_envelope(target_value=67)[name].encode(
                                "utf-8"
                            )
                        ).hexdigest()
                        for name in ("prefix_smt2", "target_smt2")
                    ],
                )
                self.assertTrue(
                    store.fail(
                        failed_lease,
                        "transport-failure",
                        "injected transport failure",
                    )
                )
                self.assertTrue(
                    store.complete(
                        recovery_lease,
                        "transport-recovery",
                        {**recovered, "assignments": {"0": 67}},
                    )
                )
            finally:
                persistent.close()

            stall_marker = Path(temporary) / "stall-generation"
            stall_script = (
                "import json,os,pathlib,sys,time;"
                f"\np=pathlib.Path({str(stall_marker)!r})"
                "\ntry: n=int(p.read_text())"
                "\nexcept (FileNotFoundError,ValueError): n=0"
                "\np.write_text(str(n+1))"
                "\nif n == 0: time.sleep(5)"
                "\nfor line in sys.stdin:"
                "\n q=line.rstrip('\\n').split('\\t')[0]"
                "\n print(json.dumps({'request_id':q,'status':'sat',"
                "'assignments':{},'solver':'recovered',"
                "'helper_pid':os.getpid()}),flush=True)"
            )

            def wait_for_marker(path: Path) -> None:
                deadline = time.monotonic() + 1.0
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertTrue(path.exists())

            stalled = PersistentSubprocessSolver((sys.executable, "-c", stall_script))
            try:
                wait_for_marker(stall_marker)
                blocked_generation = stalled.process.pid
                started = time.monotonic()
                with mock.patch.object(
                    query_store_module,
                    "_PERSISTENT_SOLVER_GRACE_SECONDS",
                    0.05,
                ):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        stalled(
                            WorkLease(
                                "blocked-pipe",
                                1,
                                Path("query.smt2"),
                                "prefix",
                                Path("prefix.smt2"),
                                Path("target.smt2"),
                                1,
                                "aa" * (512 * 1024),
                            )
                        )
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertIsNotNone(stalled.process.poll())
                recovered = stalled(
                    WorkLease(
                        "after-blocked-pipe",
                        1,
                        Path("query.smt2"),
                        "prefix",
                        Path("prefix.smt2"),
                        Path("target.smt2"),
                        1000,
                    )
                )
                self.assertNotEqual(recovered["helper_pid"], blocked_generation)
            finally:
                stalled.close()

            descriptor_marker = Path(temporary) / "descriptor-stall-generation"
            descriptor_script = stall_script.replace(
                str(stall_marker),
                str(descriptor_marker),
            )
            descriptor_stalled = PersistentSubprocessSolver(
                (sys.executable, "-c", descriptor_script)
            )
            descriptor_store = QueryStore(Path(temporary) / "descriptor-stall-store")
            descriptor_store.ingest(_envelope(source="descriptor-stall"))
            descriptor_lease = descriptor_store.claim("descriptor-stall")
            self.assertIsNotNone(descriptor_lease)
            assert descriptor_lease is not None
            try:
                wait_for_marker(descriptor_marker)
                blocked_generation = descriptor_stalled.process.pid
                channel = descriptor_stalled._fd_channel
                self.assertIsNotNone(channel)
                assert channel is not None
                channel.setblocking(False)
                while True:
                    try:
                        channel.send(b"x" * 256)
                    except BlockingIOError:
                        break
                with mock.patch.object(
                    query_store_module,
                    "_PERSISTENT_SOLVER_GRACE_SECONDS",
                    0.05,
                ):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        descriptor_stalled(descriptor_lease)
                self.assertIsNotNone(descriptor_stalled.process.poll())
                recovered = descriptor_stalled(
                    WorkLease(
                        "after-blocked-descriptor",
                        1,
                        Path("query.smt2"),
                        "prefix",
                        Path("prefix.smt2"),
                        Path("target.smt2"),
                        1000,
                    )
                )
                self.assertNotEqual(recovered["helper_pid"], blocked_generation)
                self.assertTrue(
                    descriptor_store.fail(
                        descriptor_lease,
                        "descriptor-stall",
                        "injected descriptor transport stall",
                    )
                )
            finally:
                descriptor_stalled.close()

    def test_portfolio_cancels_persistent_helper_before_io_admission(self):
        script = (
            "import json,sys;"
            "\nfor line in sys.stdin:"
            "\n q=line.rstrip('\\n').split('\\t')[0]"
            "\n print(json.dumps({'request_id':q,'status':'sat',"
            "'assignments':{'0':66},'solver':'persistent'}),flush=True)"
        )

        class GatedPersistentSolver(PersistentSubprocessSolver):
            def __init__(self):
                super().__init__((sys.executable, "-c", script))
                self._io_lock.acquire()
                self._gate_held = True

            def cancel(self, lease: WorkLease) -> bool:
                result = super().cancel(lease)
                if self._gate_held:
                    self._gate_held = False
                    self._io_lock.release()
                return result

            def release_gate(self) -> None:
                if self._gate_held:
                    self._gate_held = False
                    self._io_lock.release()

        lease = WorkLease(
            "queued-persistent",
            1,
            Path("query.smt2"),
            "prefix",
            Path("prefix.smt2"),
            Path("target.smt2"),
            1000,
        )
        persistent = GatedPersistentSolver()
        registered = threading.Event()

        def fast(_lease: WorkLease) -> dict:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with persistent._state_lock:
                    if persistent._running.get(str(lease.query_id)):
                        registered.set()
                        break
                time.sleep(0.005)
            return {
                "status": "sat",
                "assignments": {"0": 66},
                "solver": "fast",
            }

        try:
            result = PortfolioSolver(
                (("persistent", persistent), ("fast", fast)),
                parallelism=2,
                cancel_grace_ms=0,
            )(lease)
            self.assertTrue(registered.is_set())
            self.assertTrue(result["portfolio"]["cancel_requested"])
            self.assertEqual(result["portfolio"]["cancelled_attempts"], 1)
            self.assertTrue(result["portfolio"]["attempts"][0]["cancelled"])
            persistent.release_gate()
            recovered = persistent(
                WorkLease(
                    "after-queued-cancel",
                    1,
                    Path("query.smt2"),
                    "prefix",
                    Path("prefix.smt2"),
                    Path("target.smt2"),
                    1000,
                )
            )
            self.assertEqual(recovered["status"], "sat")
            self.assertEqual(recovered["assignments"], {"0": 66})
        finally:
            persistent.release_gate()
            persistent.close()

    def test_persistent_solver_strict_json_and_diagnostic_backpressure(self):
        script = (
            "import json,os,sys;"
            "\nfor line in sys.stdin:"
            "\n q=line.rstrip('\\n').split('\\t')[0]"
            "\n if q == 'duplicate-json':"
            "\n  print('{\"request_id\":\"duplicate-json\",'"
            "'\"request_id\":\"duplicate-json\",'"
            "'\"status\":\"sat\",\"assignments\":{}}',flush=True)"
            "\n elif q == 'nonfinite-json':"
            "\n  print('{\"request_id\":\"nonfinite-json\",'"
            "'\"status\":\"sat\",\"assignments\":{},'"
            "'\"elapsed_us\":NaN}',flush=True)"
            "\n else:"
            "\n  if q == 'diagnostic-flood':"
            "\n   sys.stderr.write('x'*(2*1024*1024));sys.stderr.flush()"
            "\n  print(json.dumps({'request_id':q,'status':'sat',"
            "'assignments':{},'solver':'strict-helper',"
            "'helper_pid':os.getpid()}),flush=True)"
        )
        persistent = PersistentSubprocessSolver((sys.executable, "-c", script))

        def lease(query_id: str) -> WorkLease:
            return WorkLease(
                query_id,
                1,
                Path("query.smt2"),
                "prefix",
                Path("prefix.smt2"),
                Path("target.smt2"),
                1000,
            )

        try:
            duplicate_generation = persistent.process.pid
            with self.assertRaisesRegex(
                RuntimeError,
                "duplicate JSON object member 'request_id'",
            ):
                persistent(lease("duplicate-json"))
            self.assertIsNotNone(persistent.process.poll())
            recovered = persistent(lease("after-duplicate"))
            self.assertNotEqual(recovered["helper_pid"], duplicate_generation)

            nonfinite_generation = persistent.process.pid
            with self.assertRaisesRegex(
                RuntimeError,
                "non-finite JSON number 'NaN'",
            ):
                persistent(lease("nonfinite-json"))
            self.assertIsNotNone(persistent.process.poll())
            recovered = persistent(lease("after-nonfinite"))
            self.assertNotEqual(recovered["helper_pid"], nonfinite_generation)

            started = time.monotonic()
            noisy = persistent(lease("diagnostic-flood"))
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(noisy["status"], "sat")
        finally:
            persistent.close()

    def test_unsat_clause_set_prunes_later_superset(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            first_id, _ = store.ingest(_envelope(target_value=66))

            def unsat_backend(_lease: WorkLease) -> dict:
                return {
                    "status": "unsat",
                    "assignments": {},
                    "solver": "fake",
                    "elapsed_us": 10,
                }

            self.assertEqual(solve_one(store, "worker", unsat_backend), "unsat")

            superset = _envelope(target_value=66)
            superset["nodes"].extend(
                [
                    {
                        "id": 5,
                        "op": "constant",
                        "bits": 8,
                        "children": [],
                        "attrs": {"value_hex": "43"},
                    },
                    {
                        "id": 6,
                        "op": "equal",
                        "bits": 1,
                        "children": [0, 5],
                        "attrs": {},
                    },
                ]
            )
            superset["prefix_roots"] = [2, 4]
            superset["target_root"] = 6
            superset["prefix_smt2"] += "(assert (= |0| #x42))\n"
            superset["target_smt2"] = (
                "(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x43))\n"
            )
            superset["smt2"] += "(assert (= |0| #x43))\n"
            second_id, created = store.ingest(superset)
            self.assertTrue(created)
            self.assertNotEqual(first_id, second_id)
            self.assertIsNone(store.claim("should-not-run"))
            result = json.loads(
                (store.result_dir / second_id[:2] / f"{second_id}.json").read_text(
                    encoding="ascii"
                )
            )
            self.assertEqual(result["status"], "unsat")
            self.assertEqual(result["reused_from"], first_id)
            self.assertEqual(store.stats()["unsat_subsumed"], 1)

    def test_spool_ingestion_is_atomic_and_quarantines_bad_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool = root / "spool"
            incoming = spool / "incoming"
            incoming.mkdir(parents=True)
            serialized = json.dumps(_envelope())
            (incoming / "good.json").write_text(serialized, encoding="utf-8")
            (incoming / "bad.json").write_text("{", encoding="utf-8")
            (incoming / "duplicate.json").write_text(
                serialized.replace(
                    '"schema": "symcc-query-ir-v1"',
                    '"schema": "symcc-query-ir-v1", "schema": "symcc-query-ir-v1"',
                    1,
                ),
                encoding="utf-8",
            )
            (incoming / "nonfinite.json").write_text(
                serialized.replace(
                    '"producer": "test"',
                    '"producer": NaN',
                    1,
                ),
                encoding="utf-8",
            )
            symlink_target = root / "symlink-target.json"
            symlink_target.write_text(serialized, encoding="utf-8")
            (incoming / "symlink.json").symlink_to(symlink_target)
            os.mkfifo(incoming / "fifo.json")
            store = QueryStore(root / "store")
            imported, failed = ingest_spool(store, spool)
            self.assertEqual((imported, failed), (1, 5))
            self.assertTrue((spool / "accepted" / "good.json").is_file())
            self.assertTrue((spool / "rejected" / "bad.json").is_file())
            self.assertTrue((spool / "rejected" / "bad.json.error").is_file())
            self.assertTrue((spool / "rejected" / "duplicate.json").is_file())
            duplicate_error = (spool / "rejected" / "duplicate.json.error").read_text(
                encoding="utf-8"
            )
            self.assertIn("duplicate JSON object member 'schema'", duplicate_error)
            nonfinite_error = (spool / "rejected" / "nonfinite.json.error").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "non-finite JSON number 'NaN' is not supported",
                nonfinite_error,
            )
            symlink_error = (spool / "rejected" / "symlink.json.error").read_text(
                encoding="utf-8"
            )
            self.assertIn("cannot read query envelope", symlink_error)
            self.assertTrue((spool / "rejected" / "symlink.json").is_symlink())
            fifo_error = (spool / "rejected" / "fifo.json.error").read_text(
                encoding="utf-8"
            )
            self.assertIn("query envelope must be a regular file", fifo_error)
            self.assertTrue(
                stat.S_ISFIFO(os.lstat(spool / "rejected" / "fifo.json").st_mode)
            )

            bounded = root / "bounded.json"
            bounded.write_text(serialized, encoding="utf-8")
            understated = mock.Mock(st_size=1)
            with (
                mock.patch.object(
                    query_store_module,
                    "_MAX_ENVELOPE_BYTES",
                    32,
                ),
                mock.patch.object(Path, "stat", return_value=understated) as probe,
                self.assertRaisesRegex(
                    ValueError,
                    "query envelope exceeds 32 bytes",
                ),
            ):
                store.ingest_file(bounded)
            self.assertEqual(probe.call_count, 0)

            metadata = os.stat(bounded, follow_symlinks=False)
            replaced = mock.Mock(
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino + 1,
                st_mode=metadata.st_mode,
                st_size=metadata.st_size,
                st_mtime_ns=metadata.st_mtime_ns,
                st_ctime_ns=metadata.st_ctime_ns,
            )
            with (
                mock.patch.object(
                    query_store_module.os,
                    "stat",
                    return_value=replaced,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "query envelope identity changed while reading",
                ),
            ):
                store.ingest_file(bounded)

            race_spool = root / "race-spool"
            race_incoming = race_spool / "incoming"
            race_incoming.mkdir(parents=True)
            (race_incoming / "query.json").write_text("{}", encoding="utf-8")
            entered = threading.Event()
            release = threading.Event()

            class SlowStore:
                calls = 0

                def ingest_file(self, _path):
                    self.calls += 1
                    entered.set()
                    release.wait(5)
                    return "query", True

            slow_store = SlowStore()
            race_results = []
            race_errors = []

            def consume_spool():
                try:
                    race_results.append(ingest_spool(slow_store, race_spool))
                except BaseException as error:
                    race_errors.append(error)

            first = threading.Thread(target=consume_spool)
            first.start()
            self.assertTrue(entered.wait(2))
            race_results.append(ingest_spool(slow_store, race_spool))
            release.set()
            first.join(2)
            self.assertFalse(first.is_alive())
            self.assertEqual(slow_store.calls, 1)
            self.assertEqual(sorted(race_results), [(0, 0), (1, 0)])
            self.assertEqual(race_errors, [])
            self.assertTrue((race_spool / "accepted" / "query.json").is_file())

            recovery_spool = root / "recovery-spool"
            recovery_incoming = recovery_spool / "incoming"
            recovery_incoming.mkdir(parents=True)
            recovery_path = recovery_incoming / "recovery.json"
            recovery_path.write_text(serialized, encoding="utf-8")
            original_move = query_service_module._move

            def interrupt_move(source, destination):
                if destination.name == "accepted":
                    raise KeyboardInterrupt("simulated consumer crash")
                original_move(source, destination)

            with (
                mock.patch.object(
                    query_service_module,
                    "_move",
                    side_effect=interrupt_move,
                ),
                self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "simulated consumer crash",
                ),
            ):
                ingest_spool(store, recovery_spool)
            self.assertTrue(recovery_path.is_file())
            self.assertEqual(
                ingest_spool(store, recovery_spool),
                (1, 0),
            )
            self.assertTrue((recovery_spool / "accepted" / "recovery.json").is_file())

            publication_spool = root / "publication-spool"
            publication_incoming = publication_spool / "incoming"
            publication_incoming.mkdir(parents=True)
            publication_path = publication_incoming / "publication.json"
            publication_path.write_text(serialized, encoding="utf-8")
            publication_store = QueryStore(root / "publication-store")

            def fail_accepted_move(source, destination):
                if destination.name == "accepted":
                    raise OSError("simulated accepted publication failure")
                original_move(source, destination)

            with (
                mock.patch.object(
                    query_service_module,
                    "_move",
                    side_effect=fail_accepted_move,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "simulated accepted publication failure",
                ),
            ):
                ingest_spool(publication_store, publication_spool)
            self.assertTrue(publication_path.is_file())
            self.assertFalse((publication_spool / "accepted").exists())
            self.assertFalse((publication_spool / "rejected").exists())
            self.assertEqual(publication_store.stats()["queries"], 1)
            self.assertEqual(publication_store.stats()["witnesses"], 1)
            self.assertEqual(
                ingest_spool(publication_store, publication_spool),
                (1, 0),
            )
            self.assertTrue(
                (publication_spool / "accepted" / "publication.json").is_file()
            )
            self.assertEqual(publication_store.stats()["queries"], 1)
            self.assertEqual(publication_store.stats()["witnesses"], 1)

            persistence_spool = root / "persistence-spool"
            persistence_incoming = persistence_spool / "incoming"
            persistence_incoming.mkdir(parents=True)
            persistence_path = persistence_incoming / "persistence.json"
            persistence_path.write_text(serialized, encoding="utf-8")
            persistence_store = QueryStore(root / "persistence-store")
            with (
                mock.patch.object(
                    persistence_store,
                    "_connect",
                    side_effect=sqlite3.OperationalError(
                        "simulated QueryStore persistence failure"
                    ),
                ),
                self.assertRaisesRegex(
                    sqlite3.OperationalError,
                    "simulated QueryStore persistence failure",
                ),
            ):
                ingest_spool(persistence_store, persistence_spool)
            self.assertTrue(persistence_path.is_file())
            self.assertFalse((persistence_spool / "accepted").exists())
            self.assertFalse((persistence_spool / "rejected").exists())
            self.assertEqual(persistence_store.stats()["queries"], 0)
            self.assertEqual(persistence_store.stats()["witnesses"], 0)
            self.assertEqual(
                ingest_spool(persistence_store, persistence_spool),
                (1, 0),
            )
            self.assertTrue(
                (persistence_spool / "accepted" / "persistence.json").is_file()
            )
            self.assertEqual(persistence_store.stats()["queries"], 1)
            self.assertEqual(persistence_store.stats()["witnesses"], 1)

            read_fault_spool = root / "read-fault-spool"
            read_fault_incoming = read_fault_spool / "incoming"
            read_fault_incoming.mkdir(parents=True)
            read_fault_path = read_fault_incoming / "read-fault.json"
            read_fault_path.write_text(serialized, encoding="utf-8")
            read_fault_store = QueryStore(root / "read-fault-store")
            with (
                mock.patch.object(
                    query_store_module,
                    "_load_query_envelope",
                    side_effect=OSError("simulated envelope read failure"),
                ),
                self.assertRaisesRegex(
                    OSError,
                    "simulated envelope read failure",
                ),
            ):
                ingest_spool(read_fault_store, read_fault_spool)
            self.assertTrue(read_fault_path.is_file())
            self.assertFalse((read_fault_spool / "accepted").exists())
            self.assertFalse((read_fault_spool / "rejected").exists())
            self.assertEqual(read_fault_store.stats()["queries"], 0)
            self.assertEqual(
                ingest_spool(read_fault_store, read_fault_spool),
                (1, 0),
            )
            self.assertTrue(
                (read_fault_spool / "accepted" / "read-fault.json").is_file()
            )
            self.assertEqual(read_fault_store.stats()["queries"], 1)
            self.assertEqual(read_fault_store.stats()["witnesses"], 1)

            for lock_kind in ("symlink", "fifo"):
                with self.subTest(lock_kind=lock_kind):
                    poisoned_spool = root / f"{lock_kind}-lock-spool"
                    poisoned_incoming = poisoned_spool / "incoming"
                    poisoned_incoming.mkdir(parents=True)
                    poisoned_query = poisoned_incoming / "query.json"
                    poisoned_query.write_text(serialized, encoding="utf-8")
                    lock_path = poisoned_spool / ".ingest.lock"
                    if lock_kind == "symlink":
                        external_lock = root / "external-lock"
                        external_lock.write_text("unchanged", encoding="ascii")
                        lock_path.symlink_to(external_lock)
                    else:
                        os.mkfifo(lock_path)
                    with self.assertRaises(OSError):
                        ingest_spool(store, poisoned_spool)
                    self.assertTrue(poisoned_query.is_file())
                    if lock_kind == "symlink":
                        self.assertEqual(
                            external_lock.read_text(encoding="ascii"),
                            "unchanged",
                        )


if __name__ == "__main__":
    unittest.main()
