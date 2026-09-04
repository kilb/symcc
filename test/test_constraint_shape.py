#!/usr/bin/env python3
# RUN: python3 %s

from __future__ import annotations

import copy
import os
from pathlib import Path
import random
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from constraint_shape import alpha_normalized_constraint_shape  # noqa: E402
from query_store import QueryStore  # noqa: E402
import symcc_query_service  # noqa: E402


def envelope(
    *,
    index: int,
    value: int = 0x42,
    site: int = 123,
    source: str = "seed",
) -> dict:
    input_bytes = bytes(range(16))
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "constraint-shape-test",
        "nodes": [
            {
                "id": 0,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": index},
            },
            {
                "id": 1,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": f"{value:02x}"},
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
        "input_hex": input_bytes.hex(),
        "timeout_ms": 1000,
        "metadata": {
            "source": source,
            "output_dir": "",
            "site": site,
            "branch": 999,
            "desired": True,
        },
        "smt2": (
            f"(declare-fun |{index}| () (_ BitVec 8))\n"
            "(assert true)\n"
            f"(assert (= |{index}| #x{value:02x}))\n"
        ),
        "prefix_smt2": "(assert true)\n",
        "target_smt2": (
            f"(declare-fun |{index}| () (_ BitVec 8))\n"
            f"(assert (= |{index}| #x{value:02x}))\n"
        ),
    }


class ConstraintShapeTests(unittest.TestCase):
    def test_absolute_read_offsets_are_alpha_normalized(self):
        first = envelope(index=0)
        shifted = envelope(index=11)
        first_shape = alpha_normalized_constraint_shape(
            first["nodes"], [first["target_root"]]
        )
        shifted_shape = alpha_normalized_constraint_shape(
            shifted["nodes"], [shifted["target_root"]]
        )
        self.assertEqual(first_shape.shape_hash, shifted_shape.shape_hash)
        self.assertEqual(first_shape.read_count, 1)
        self.assertEqual(first_shape.node_count, 3)

    def test_constant_operator_and_aliasing_remain_semantic(self):
        base = envelope(index=0)
        different_constant = envelope(index=7, value=0x43)
        different_operator = copy.deepcopy(base)
        different_operator["nodes"][2]["op"] = "distinct"
        hashes = {
            alpha_normalized_constraint_shape(item["nodes"], [2]).shape_hash
            for item in (base, different_constant, different_operator)
        }
        self.assertEqual(len(hashes), 3)

        same_read = copy.deepcopy(base["nodes"])
        same_read.append(
            {
                "id": 4,
                "op": "equal",
                "bits": 1,
                "children": [0, 0],
                "attrs": {},
            }
        )
        different_reads = copy.deepcopy(same_read)
        different_reads.insert(
            1,
            {
                "id": 1,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": 9},
            },
        )
        for node_id, node in enumerate(different_reads):
            node["id"] = node_id
        different_reads[-1]["children"] = [0, 1]
        self.assertNotEqual(
            alpha_normalized_constraint_shape(same_read, [4]).shape_hash,
            alpha_normalized_constraint_shape(different_reads, [5]).shape_hash,
        )

    def test_invalid_dag_fails_closed(self):
        malformed = envelope(index=0)["nodes"]
        malformed[2]["children"] = [2, 1]
        with self.assertRaisesRegex(ValueError, "DAG order"):
            alpha_normalized_constraint_shape(malformed, [2])

    def test_randomized_alpha_renaming_and_semantic_separation(self):
        generator = random.Random(399)
        for width in range(1, 65):
            base_nodes, renamed_nodes, roots = [], [], []
            offsets = generator.sample(range(0, 1_000_000), width)
            renamed_offsets = generator.sample(range(2_000_000, 3_000_000), width)
            for position in range(width):
                for nodes, offset in (
                    (base_nodes, offsets[position]),
                    (renamed_nodes, renamed_offsets[position]),
                ):
                    read_id = len(nodes)
                    nodes.append(
                        {
                            "id": read_id,
                            "op": "read",
                            "bits": 8,
                            "children": [],
                            "attrs": {"index": offset},
                        }
                    )
                    constant_id = len(nodes)
                    nodes.append(
                        {
                            "id": constant_id,
                            "op": "constant",
                            "bits": 8,
                            "children": [],
                            "attrs": {"value_hex": f"{position & 0xFF:02x}"},
                        }
                    )
                    nodes.append(
                        {
                            "id": len(nodes),
                            "op": "equal",
                            "bits": 1,
                            "children": [read_id, constant_id],
                            "attrs": {},
                        }
                    )
                roots.append(len(base_nodes) - 1)

            baseline = alpha_normalized_constraint_shape(base_nodes, roots)
            renamed = alpha_normalized_constraint_shape(renamed_nodes, roots)
            self.assertEqual(baseline.shape_hash, renamed.shape_hash)

            changed = copy.deepcopy(renamed_nodes)
            changed[-2]["attrs"]["value_hex"] = "ff"
            self.assertNotEqual(
                baseline.shape_hash,
                alpha_normalized_constraint_shape(changed, roots).shape_hash,
            )

    def test_store_records_contextual_duplicate_classes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            representative, _ = store.ingest(envelope(index=0, source="first"))
            same_query, created = store.ingest(
                envelope(index=0, site=999, source="another-witness")
            )
            duplicate, _ = store.ingest(envelope(index=11, source="second"))
            other_site, _ = store.ingest(envelope(index=10, site=124))
            other_value, _ = store.ingest(envelope(index=9, value=0x43))
            changed_prefix_envelope = envelope(index=8)
            changed_prefix_envelope["nodes"][3]["attrs"]["value"] = False
            changed_prefix_envelope["prefix_smt2"] = "(assert false)\n"
            changed_prefix_envelope["smt2"] = (
                "(declare-fun |8| () (_ BitVec 8))\n"
                "(assert false)\n(assert (= |8| #x42))\n"
            )
            changed_prefix, _ = store.ingest(changed_prefix_envelope)

            representative_shape = store.query_constraint_shape(representative)
            duplicate_shape = store.query_constraint_shape(duplicate)
            self.assertIsNotNone(representative_shape)
            self.assertIsNotNone(duplicate_shape)
            assert representative_shape is not None and duplicate_shape is not None
            self.assertEqual(same_query, representative)
            self.assertFalse(created)
            self.assertEqual(
                representative_shape["shape_hash"], duplicate_shape["shape_hash"]
            )
            self.assertEqual(representative_shape["duplicate_rank"], 1)
            self.assertEqual(duplicate_shape["duplicate_rank"], 2)
            self.assertEqual(duplicate_shape["representative_query_id"], representative)
            self.assertEqual(
                store.query_constraint_shape(other_site)["duplicate_rank"], 1
            )
            self.assertEqual(
                store.query_constraint_shape(other_value)["duplicate_rank"], 1
            )
            self.assertEqual(
                store.query_constraint_shape(changed_prefix)["duplicate_rank"], 1
            )
            stats = store.stats()
            self.assertEqual(stats["query_shape_classes"], 4)
            self.assertEqual(stats["query_shape_duplicates"], 1)
            self.assertEqual(stats["query_shape_max_class"], 2)

    def test_joint_context_preserves_prefix_target_read_aliasing(self):
        shared = envelope(index=0)
        shared["prefix_roots"] = [2]
        shared["target_root"] = 3
        shared["nodes"][3] = {
            "id": 3,
            "op": "equal",
            "bits": 1,
            "children": [0, 1],
            "attrs": {},
        }

        distinct = copy.deepcopy(shared)
        distinct["nodes"].insert(
            1,
            {
                "id": 1,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": 1},
            },
        )
        for node_id, node in enumerate(distinct["nodes"]):
            node["id"] = node_id
        distinct["nodes"][3]["children"] = [0, 2]
        distinct["nodes"][4]["children"] = [1, 2]
        distinct["prefix_roots"] = [3]
        distinct["target_root"] = 4
        distinct["smt2"] = "(assert true)\n(assert true)\n"
        distinct["prefix_smt2"] = "(assert true)\n"
        distinct["target_smt2"] = "(assert true)\n"

        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            shared_id, _ = store.ingest(shared)
            distinct_id, _ = store.ingest(distinct)
            shared_shape = store.query_constraint_shape(shared_id)
            distinct_shape = store.query_constraint_shape(distinct_id)
            assert shared_shape is not None and distinct_shape is not None
            self.assertNotEqual(
                shared_shape["context_hash"], distinct_shape["context_hash"]
            )
            self.assertEqual(shared_shape["duplicate_rank"], 1)
            self.assertEqual(distinct_shape["duplicate_rank"], 1)

    @staticmethod
    def _finish_unsat(store: QueryStore, owner: str, query_id: str) -> None:
        lease = store.claim(owner, traversal="structural")
        assert lease is not None
        if lease.query_id != query_id:
            lease.close_artifacts()
            raise AssertionError((lease.query_id, query_id))
        if not store.complete(
            lease,
            owner,
            {
                "status": "unsat",
                "assignments": {},
                "solver": "constraint-shape-test",
                "elapsed_us": 1,
            },
        ):
            raise AssertionError("failed to complete query lease")

    def test_scheduler_prefers_novel_shape_then_ages_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            representative, _ = store.ingest(envelope(index=0))
            duplicate, _ = store.ingest(envelope(index=11))
            novel, _ = store.ingest(envelope(index=10, value=0x43))
            self._finish_unsat(store, "representative", representative)

            lease = store.claim("novel", traversal="structural")
            self.assertIsNotNone(lease)
            assert lease is not None
            self.assertEqual(lease.query_id, novel)
            self.assertTrue(
                store.complete(
                    lease,
                    "novel",
                    {
                        "status": "unsat",
                        "assignments": {},
                        "solver": "constraint-shape-test",
                        "elapsed_us": 1,
                    },
                )
            )

            aged_novel, _ = store.ingest(envelope(index=8, value=0x44))
            with store._connect() as db:
                db.execute(
                    "UPDATE queries SET created = ? WHERE query_id = ?",
                    (time.time() - 1000.0, duplicate),
                )
            aged = store.claim("aged", traversal="structural")
            self.assertIsNotNone(aged)
            assert aged is not None
            self.assertEqual(aged.query_id, duplicate)
            aged.close_artifacts()
            self.assertNotEqual(aged.query_id, aged_novel)

    def test_shape_selection_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = QueryStore(temporary)
            representative, _ = store.ingest(envelope(index=0))
            duplicate, _ = store.ingest(envelope(index=11))
            store.ingest(envelope(index=10, value=0x43))
            with mock.patch.dict(os.environ, {"SYMCC_QUERY_SHAPE_SELECTION": "0"}):
                self._finish_unsat(store, "representative", representative)
                lease = store.claim("fifo", traversal="priority")
            self.assertIsNotNone(lease)
            assert lease is not None
            self.assertEqual(lease.query_id, duplicate)
            lease.close_artifacts()

    def test_query_service_defaults_to_structural_traversal(self):
        with mock.patch.object(
            sys,
            "argv",
            ["symcc_query_service.py", "--store", "/tmp/store", "--once"],
        ):
            args = symcc_query_service._parser().parse_args()
        self.assertEqual(args.traversal, "structural")


if __name__ == "__main__":
    unittest.main()
