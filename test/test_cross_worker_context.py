#!/usr/bin/env python3
# RUN: python3 %s

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

import cross_worker_context as context_module  # noqa: E402
from cross_worker_context import CrossWorkerContextStore  # noqa: E402


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class CrossWorkerContextStoreTest(unittest.TestCase):
    def test_chain_is_content_addressed_and_exact_republish_is_a_hit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CrossWorkerContextStore(directory)
            roots = (_digest("root-a"), _digest("root-b"))
            terms = (
                "(bvuge symcc_input_0 (_ bv64 8))",
                "(bvule symcc_input_1 (_ bv80 8))",
            )
            capability = _digest("capability")
            first = store.publish_chain(
                roots, terms, capability_sha256=capability
            )
            self.assertIsNotNone(first)
            assert first is not None
            self.assertFalse(first.exact_hit)
            self.assertEqual(first.created_count, 2)
            self.assertEqual(first.existing_count, 0)
            plan = store.resolve(
                first.context_sha256,
                expected_capability_sha256=capability,
            )
            self.assertEqual(plan.root_hashes, roots)
            self.assertEqual(plan.terms, terms)
            self.assertEqual(plan.offsets, (0, 1))
            self.assertEqual(plan.depth, 2)

            second = store.publish_chain(
                roots, terms, capability_sha256=capability
            )
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(second.context_sha256, first.context_sha256)
            self.assertTrue(second.exact_hit)
            self.assertEqual(second.created_count, 0)
            self.assertEqual(second.existing_count, 2)
            self.assertEqual(store.stats()["contexts"], 2)

    def test_capability_and_parent_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CrossWorkerContextStore(directory)
            publication = store.publish_chain(
                (_digest("root"),),
                ("(= symcc_input_0 (_ bv1 8))",),
                capability_sha256=_digest("capability"),
            )
            assert publication is not None
            with self.assertRaisesRegex(ValueError, "capability identity"):
                store.resolve(
                    publication.context_sha256,
                    expected_capability_sha256=_digest("other-capability"),
                )

            path = store._object_path(publication.context_sha256)
            raw = json.loads(path.read_text(encoding="ascii"))
            raw["term"] = "false"
            path.write_text(json.dumps(raw), encoding="ascii")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                store.resolve(publication.context_sha256)

    def test_symbolic_link_context_object_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CrossWorkerContextStore(directory)
            publication = store.publish_chain(
                (_digest("root"),),
                ("true",),
                capability_sha256=_digest("capability"),
            )
            assert publication is not None
            path = store._object_path(publication.context_sha256)
            outside = Path(directory) / "outside.json"
            outside.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(outside)
            with self.assertRaises(OSError):
                store.resolve(publication.context_sha256)

    def test_concurrent_publishers_converge_on_one_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = tuple(_digest(f"root-{index}") for index in range(3))
            terms = tuple(
                f"(= symcc_input_{index} (_ bv{index} 8))"
                for index in range(3)
            )
            capability = _digest("capability")
            barrier = threading.Barrier(4)

            def publish(_index: int):
                try:
                    local = CrossWorkerContextStore(directory)
                    barrier.wait(timeout=5.0)
                    return local.publish_chain(
                        roots, terms, capability_sha256=capability
                    )
                except BaseException:
                    barrier.abort()
                    raise

            with ThreadPoolExecutor(max_workers=4) as executor:
                publications = list(executor.map(publish, range(4)))
            self.assertTrue(all(item is not None for item in publications))
            digests = {item.context_sha256 for item in publications if item}
            self.assertEqual(len(digests), 1)
            plan = CrossWorkerContextStore(directory).resolve(digests.pop())
            self.assertEqual(plan.terms, terms)
            self.assertEqual(
                CrossWorkerContextStore(directory).stats()["contexts"], 3
            )

    def test_materialization_lease_is_fenced_cancelled_and_reclaimable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CrossWorkerContextStore(directory)
            publication = store.publish_chain(
                (_digest("root"),),
                ("true",),
                capability_sha256=_digest("capability"),
            )
            assert publication is not None
            with mock.patch.object(context_module.time, "time", return_value=100.0):
                first = store.claim_materialization(
                    publication.context_sha256,
                    "worker-a",
                    lease_seconds=10.0,
                )
                self.assertIsNotNone(first)
                self.assertIsNone(
                    store.claim_materialization(
                        publication.context_sha256,
                        "worker-b",
                        lease_seconds=10.0,
                    )
                )
            assert first is not None
            self.assertTrue(store.cancel_materialization(first))
            with mock.patch.object(context_module.time, "time", return_value=101.0):
                second = store.claim_materialization(
                    publication.context_sha256,
                    "worker-b",
                    lease_seconds=10.0,
                )
            self.assertIsNotNone(second)
            assert second is not None
            self.assertGreater(second.token, first.token)
            self.assertFalse(store.release_materialization(first))
            with mock.patch.object(context_module.time, "time", return_value=101.5):
                self.assertTrue(store.release_materialization(second))
            with mock.patch.object(context_module.time, "time", return_value=102.0):
                third = store.claim_materialization(
                    publication.context_sha256,
                    "worker-b",
                    lease_seconds=10.0,
                )
            self.assertIsNotNone(third)
            assert third is not None
            self.assertGreater(third.token, second.token)
            self.assertFalse(store.cancel_materialization(second))
            self.assertTrue(store.release_materialization(third))

    def test_expired_lease_and_global_quota_recover_after_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CrossWorkerContextStore(
                directory, max_active_materializations=1
            )
            first_context = store.publish_chain(
                (_digest("root-a"),),
                ("true",),
                capability_sha256=_digest("capability"),
            )
            second_context = store.publish_chain(
                (_digest("root-b"),),
                ("false",),
                capability_sha256=_digest("capability"),
            )
            assert first_context is not None and second_context is not None
            with mock.patch.object(context_module.time, "time", return_value=10.0):
                abandoned = store.claim_materialization(
                    first_context.context_sha256,
                    "crashed-worker",
                    lease_seconds=1.0,
                    max_active=1,
                )
                self.assertIsNotNone(abandoned)
                self.assertIsNone(
                    store.claim_materialization(
                        second_context.context_sha256,
                        "other-worker",
                        max_active=1,
                    )
                )
            with mock.patch.object(context_module.time, "time", return_value=12.0):
                recovered = store.claim_materialization(
                    second_context.context_sha256,
                    "other-worker",
                    max_active=1,
                )
            self.assertIsNotNone(recovered)

    def test_bounds_and_unknown_context_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CrossWorkerContextStore(directory, max_contexts=1)
            with self.assertRaisesRegex(ValueError, "different lengths"):
                store.publish_chain(
                    (_digest("root"),),
                    (),
                    capability_sha256=_digest("capability"),
                )
            with self.assertRaisesRegex(ValueError, "unknown context"):
                store.claim_materialization(
                    _digest("unknown"), "worker"
                )
            with self.assertRaisesRegex(ValueError, "input offset"):
                store.publish_chain(
                    (_digest("root"),),
                    ("(= symcc_input_4294967296 (_ bv0 8))",),
                    capability_sha256=_digest("capability"),
                )

    def test_store_configuration_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            CrossWorkerContextStore(
                directory,
                max_contexts=8,
                max_active_materializations=2,
            )
            with self.assertRaisesRegex(ValueError, "max_contexts"):
                CrossWorkerContextStore(
                    directory,
                    max_contexts=9,
                    max_active_materializations=2,
                )
            with self.assertRaisesRegex(
                ValueError, "max_active_materializations"
            ):
                CrossWorkerContextStore(
                    directory,
                    max_contexts=8,
                    max_active_materializations=3,
                )


if __name__ == "__main__":
    unittest.main()
