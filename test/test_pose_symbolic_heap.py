#!/usr/bin/env python3
# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s
"""Tests for the F456 path-optimal initial symbolic heap domain."""

from __future__ import annotations

import copy
import itertools
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import (  # noqa: E402
    ContinuationFrame,
    LiveContinuationDescriptor,
    LiveStateStore,
)
from pose_symbolic_heap import (  # noqa: E402
    NULL_REFERENCE,
    POSE_CHECKPOINT_MARKER,
    PoseHeapError,
    PoseHeapState,
    PoseTypeLayout,
    persist_pose_heap,
    restore_pose_heap,
)


def state_with_roots(*names: str, size: int = 4) -> PoseHeapState:
    return PoseHeapState.create(
        [PoseTypeLayout("Node", size, 1)],
        [(name, "Node") for name in names],
    )


def variable_names(state: PoseHeapState) -> list[str]:
    return sorted(
        str(node["name"])
        for _digest, node in state.to_mapping()["terms"]
        if node["op"] == "var"
    )


class PoseSymbolicHeapTests(unittest.TestCase):
    def test_identity_is_stable_and_root_order_is_canonical(self) -> None:
        left = PoseHeapState.create(
            [PoseTypeLayout("Pair", 8, 8), PoseTypeLayout("Node", 4)],
            [("right", "Node"), ("left", "Pair")],
        )
        right = PoseHeapState.create(
            [PoseTypeLayout("Node", 4), PoseTypeLayout("Pair", 8, 8)],
            [("left", "Pair"), ("right", "Node")],
        )
        self.assertEqual(left.digest, right.digest)
        self.assertEqual(left.roots, right.roots)

    def test_lazy_loads_refine_heap_without_forking(self) -> None:
        state = state_with_roots("a", "b", "c")
        for root in ("a", "b", "c"):
            state, result = state.load(root, 0)
            self.assertEqual(len(result.values), 1)
            self.assertEqual(state.metrics["cfg_forks"], 0)
        self.assertEqual(state.metrics["heap_refinements"], 3)
        self.assertEqual(state.metrics["loads"], 3)

    def test_alias_ite_preserves_same_field_value(self) -> None:
        state = state_with_roots("a", "b")
        state, first = state.load("a", 0)
        state, second = state.load("b", 0)
        names = variable_names(state)
        self.assertEqual(len(names), 2)
        values = {names[0]: 17, names[1]: 93}
        alias_model = {
            "references": {"a": "object-1", "b": "object-1"},
            "values": values,
        }
        distinct_model = {
            "references": {"a": "object-1", "b": "object-2"},
            "values": values,
        }
        self.assertEqual(
            state.evaluate(first.values[0], alias_model),
            state.evaluate(second.values[0], alias_model),
        )
        self.assertNotEqual(
            state.evaluate(first.values[0], distinct_model),
            state.evaluate(second.values[0], distinct_model),
        )

    def test_conditional_store_updates_every_alias_proxy(self) -> None:
        state = state_with_roots("a", "b")
        state, _ = state.load("a", 0)
        state, _ = state.load("b", 0)
        state = state.store("b", 0, [0x5A])
        state, first = state.load("a", 0)
        state, second = state.load("b", 0)
        aliases = {"references": {"a": "same", "b": "same"}, "values": {}}
        distinct = {"references": {"a": "left", "b": "right"}, "values": {}}
        self.assertEqual(state.evaluate(first.values[0], aliases), 0x5A)
        self.assertEqual(state.evaluate(second.values[0], aliases), 0x5A)
        self.assertEqual(state.evaluate(second.values[0], distinct), 0x5A)

    def test_free_updates_liveness_for_aliases_without_heap_fork(self) -> None:
        state = state_with_roots("a", "b")
        state, _ = state.load("a", 0)
        state, _ = state.load("b", 0)
        state = state.free("b")
        state, first = state.load("a", 0)
        aliases = {"references": {"a": "same", "b": "same"}, "values": {}}
        distinct = {"references": {"a": "left", "b": "right"}, "values": {}}
        self.assertEqual(state.evaluate(first.live, aliases), 0)
        self.assertEqual(state.evaluate(first.live, distinct), 1)
        self.assertEqual(state.metrics["cfg_forks"], 0)
        self.assertEqual(state.metrics["frees"], 1)

    def test_only_cfg_alias_branch_creates_child_states(self) -> None:
        state = state_with_roots("a", "b")
        state, _ = state.load("a", 0)
        children = state.branch_alias("a", "b")
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child.metrics["cfg_forks"] == 1 for child in children))
        for child in children:
            grandchildren = child.branch_alias("a", "b")
            self.assertEqual(len(grandchildren), 1)

    def test_dereference_makes_null_cfg_arm_infeasible(self) -> None:
        state = state_with_roots("root")
        state, _ = state.load("root", 0)
        children = state.branch_alias("root", NULL_REFERENCE)
        self.assertEqual(len(children), 1)
        self.assertIsNone(children[0].assume_alias("root", NULL_REFERENCE, True))

    def test_type_incompatible_alias_is_rejected(self) -> None:
        state = PoseHeapState.create(
            [PoseTypeLayout("A", 1), PoseTypeLayout("B", 1)],
            [("a", "A"), ("b", "B")],
        )
        self.assertIsNone(state.assume_alias("a", "b", True))
        self.assertIsNotNone(state.assume_alias("a", "b", False))

    def test_bounds_and_width_fail_closed(self) -> None:
        state = state_with_roots("root", size=2)
        with self.assertRaisesRegex(PoseHeapError, "outside object bounds"):
            state.load("root", 1, 2)
        with self.assertRaisesRegex(PoseHeapError, "width is zero"):
            state.load("root", 0, 0)
        with self.assertRaisesRegex(PoseHeapError, "outside object bounds"):
            state.store("root", 2, [1])

    def test_operations_are_functionally_immutable(self) -> None:
        initial = state_with_roots("a", "b")
        original_digest = initial.digest
        loaded, _ = initial.load("a", 0)
        stored = loaded.store("a", 0, [1])
        self.assertEqual(initial.digest, original_digest)
        self.assertNotEqual(loaded.digest, initial.digest)
        self.assertNotEqual(stored.digest, loaded.digest)

    def test_derived_reference_has_stable_identity(self) -> None:
        state = state_with_roots("root", size=8)
        state, word, _access = state.load_word("root", 0, 8)
        left_state, left = state.derive_reference(
            word, "Node", label="next"
        )
        right_state, right = state.derive_reference(
            word, "Node", label="next"
        )
        self.assertEqual(left, right)
        self.assertEqual(left_state.digest, right_state.digest)
        loaded, _ = left_state.load(left, 1)
        self.assertEqual(loaded.metrics["heap_refinements"], 2)

    def test_little_endian_word_and_reference_field(self) -> None:
        state = state_with_roots("root", size=8)
        state = state.store("root", 0, range(1, 9))
        state, word, access = state.load_word("root", 0, 8)
        self.assertEqual(
            state.evaluate(word, {"references": {"root": "node"}, "values": {}}),
            0x0807060504030201,
        )
        state, reference, second_access = state.load_reference(
            "root", 0, "Node", label="next"
        )
        self.assertEqual(access.values, second_access.values)
        self.assertTrue(any(
            item["id"] == reference
            for item in state.to_mapping()["references"]
        ))

    def test_smt2_formula_is_deterministic_and_z3_accepted(self) -> None:
        state = state_with_roots("a", "b")
        state, _ = state.load("a", 0)
        state, _ = state.load("b", 0)
        child = state.assume_alias("a", "b", False)
        assert child is not None
        smt = child.to_smt2()
        self.assertEqual(smt, child.to_smt2())
        self.assertIn("(set-logic QF_BV)", smt)
        completed = subprocess.run(
            ["z3", "-in"],
            input=smt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "sat")

    def test_snapshot_round_trip_is_identity_exact(self) -> None:
        state = state_with_roots("a", "b")
        state, _ = state.load("a", 0, 2)
        state = state.store("a", 1, [0x7F])
        state, _ = state.load("b", 0, 2)
        restored = PoseHeapState.from_mapping(state.to_mapping())
        self.assertEqual(restored.digest, state.digest)
        self.assertEqual(restored.to_mapping(), state.to_mapping())

    def test_snapshot_rejects_unknown_fields_and_term_tampering(self) -> None:
        state = state_with_roots("root")
        state, _ = state.load("root", 0)
        unknown = copy.deepcopy(state.to_mapping())
        unknown["extra"] = True
        with self.assertRaisesRegex(PoseHeapError, "snapshot schema"):
            PoseHeapState.from_mapping(unknown)
        tampered = copy.deepcopy(state.to_mapping())
        tampered["terms"][0][1]["bits"] = 7
        with self.assertRaisesRegex(PoseHeapError, "term identity"):
            PoseHeapState.from_mapping(tampered)

    def test_snapshot_rejects_contradictory_alias_quotient(self) -> None:
        state = state_with_roots("a", "b")
        equal = state.assume_alias("a", "b", True)
        assert equal is not None
        snapshot = equal.to_mapping()
        pair = sorted((state.roots["a"], state.roots["b"]))
        snapshot["disequalities"] = [pair]
        with self.assertRaisesRegex(PoseHeapError, "both equal and unequal"):
            PoseHeapState.from_mapping(snapshot)

    def test_snapshot_rejects_metric_and_condition_abuse(self) -> None:
        state = state_with_roots("root")
        state, _ = state.load("root", 0)
        bad_metric = copy.deepcopy(state.to_mapping())
        bad_metric["metrics"]["loads"] = -1
        with self.assertRaisesRegex(PoseHeapError, "metric loads"):
            PoseHeapState.from_mapping(bad_metric)
        bad_conditions = copy.deepcopy(state.to_mapping())
        bad_conditions["conditions"].append(bad_conditions["conditions"][0])
        with self.assertRaisesRegex(PoseHeapError, "condition snapshot"):
            PoseHeapState.from_mapping(bad_conditions)

    def test_four_reference_alias_partitions_match_oldest_field(self) -> None:
        roots = ("a", "b", "c", "d")
        state = state_with_roots(*roots)
        accesses = []
        for root in roots:
            state, access = state.load(root, 0)
            accesses.append(access.values[0])
        names = variable_names(state)
        values = {name: 11 + 17 * index for index, name in enumerate(names)}
        distinct = {
            "references": dict(zip(roots, roots, strict=True)),
            "values": values,
        }
        fresh = [state.evaluate(term, distinct) for term in accesses]
        for handles in itertools.product(range(4), repeat=4):
            with self.subTest(handles=handles):
                model = {
                    "references": dict(zip(roots, map(str, handles), strict=True)),
                    "values": values,
                }
                expected: dict[int, int] = {}
                for index, handle in enumerate(handles):
                    expected.setdefault(handle, fresh[index])
                self.assertEqual(
                    [state.evaluate(term, model) for term in accesses],
                    [expected[handle] for handle in handles],
                )

    def test_materializes_alias_quotient_as_one_concrete_object(self) -> None:
        state = state_with_roots("a", "b")
        state, _ = state.load("a", 0)
        state = state.store("a", 0, [41])
        state, _ = state.load("b", 0)
        concrete = state.materialize_model({
            "references": {"a": "node", "b": "node"},
            "values": {},
        })
        self.assertEqual(concrete.roots, {"a": "node", "b": "node"})
        self.assertEqual(set(concrete.objects), {"node"})
        self.assertEqual(concrete.objects["node"]["bytes"]["0"], 41)

    def test_model_gate_rejects_null_and_quotient_conflicts(self) -> None:
        state = state_with_roots("a", "b")
        state, _ = state.load("a", 0)
        self.assertFalse(state.validate_model({
            "references": {"a": None, "b": "node"}, "values": {},
        }))
        equal = state.assume_alias("a", "b", True)
        assert equal is not None
        conflicting = {
            "references": {"a": "left", "b": "right"}, "values": {},
        }
        self.assertFalse(equal.validate_model(conflicting))
        with self.assertRaisesRegex(PoseHeapError, "violates"):
            equal.materialize_model(conflicting)

    def test_model_gate_rejects_unknown_reference_and_bad_handle(self) -> None:
        state = state_with_roots("root")
        with self.assertRaisesRegex(PoseHeapError, "unknown reference"):
            state.validate_model({
                "references": {"missing": "object"}, "values": {},
            })
        with self.assertRaisesRegex(PoseHeapError, "object handle"):
            state.validate_model({
                "references": {"root": ""}, "values": {},
            })
        state, _ = state.load("root", 0)
        scalar = variable_names(state)[0]
        with self.assertRaisesRegex(PoseHeapError, "unknown scalar"):
            state.validate_model({
                "references": {"root": "object"},
                "values": {"not-a-term": 1},
            })
        with self.assertRaisesRegex(PoseHeapError, "not an integer"):
            state.validate_model({
                "references": {"root": "object"},
                "values": {scalar: 1.0},
            })
        with self.assertRaisesRegex(PoseHeapError, "model scalar"):
            state.validate_model({
                "references": {"root": "object"},
                "values": {scalar: 256},
            })

    def test_checkpoint_round_trip_preserves_pose_root(self) -> None:
        state = state_with_roots("a", "b")
        state, _ = state.load("a", 0)
        state, _ = state.load("b", 0)
        with tempfile.TemporaryDirectory() as directory:
            store = LiveStateStore(directory)
            checkpoint = store.put_continuation(LiveContinuationDescriptor(
                engine="symcc-continuation-ir",
                frames=(ContinuationFrame("main", "entry"),),
                symbolic_memory_root=store.create_memory(b""),
            ))
            attached = persist_pose_heap(store, checkpoint, state)
            self.assertNotEqual(attached, checkpoint)
            self.assertIsNone(restore_pose_heap(store, checkpoint))
            restored = restore_pose_heap(store, attached)
            assert restored is not None
            self.assertEqual(restored.digest, state.digest)
            bundle = store.restore_continuation(attached)
            self.assertIn(POSE_CHECKPOINT_MARKER, dict(bundle.symbolic_store))
            self.assertEqual(bundle.descriptor.parent, checkpoint)

    def test_checkpoint_replacement_is_content_addressed(self) -> None:
        state = state_with_roots("root")
        with tempfile.TemporaryDirectory() as directory:
            store = LiveStateStore(directory)
            checkpoint = store.put_continuation(LiveContinuationDescriptor(
                engine="symcc-continuation-ir",
                frames=(ContinuationFrame("main", "entry"),),
            ))
            first = persist_pose_heap(store, checkpoint, state)
            second = persist_pose_heap(store, checkpoint, state)
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
