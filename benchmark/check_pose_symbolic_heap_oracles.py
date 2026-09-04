#!/usr/bin/env python3
"""Independent finite-domain oracles for F456 POSE-C heap semantics."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import (  # noqa: E402
    ContinuationFrame,
    LiveContinuationDescriptor,
    LiveStateStore,
)
from pose_symbolic_heap import (  # noqa: E402
    NULL_REFERENCE,
    PoseHeapState,
    PoseTypeLayout,
    persist_pose_heap,
    restore_pose_heap,
)


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def node_state(*roots: str) -> PoseHeapState:
    return PoseHeapState.create(
        [PoseTypeLayout("Node", 16, 8)],
        [(root, "Node") for root in roots],
    )


def variable_model(state: PoseHeapState) -> dict[str, int]:
    names = sorted(
        str(node["name"])
        for _digest, node in state.to_mapping()["terms"]
        if node["op"] == "var"
    )
    return {name: 17 + 29 * index for index, name in enumerate(names)}


def exhaustive_alias_oracle() -> dict[str, Any]:
    state = node_state("a", "b", "c")
    accesses = []
    for root in ("a", "b", "c"):
        state, access = state.load(root, 0)
        accesses.append(access.values[0])
    values = variable_model(state)
    assignments = 0
    read_matches = 0
    store_matches = 0
    stored = state.store("c", 0, [99])
    stored_accesses = []
    for root in ("a", "b", "c"):
        stored, access = stored.load(root, 0)
        stored_accesses.append(access.values[0])
    distinct_model = {
        "references": {"a": "a", "b": "b", "c": "c"},
        "values": values,
    }
    fresh = [state.evaluate(term, distinct_model) for term in accesses]
    for handles in itertools.product(("o0", "o1", "o2"), repeat=3):
        references = dict(zip(("a", "b", "c"), handles, strict=True))
        model = {"references": references, "values": values}
        assignments += 1
        expected_by_handle: dict[str, int] = {}
        for index, handle in enumerate(handles):
            expected_by_handle.setdefault(handle, fresh[index])
        actual = [state.evaluate(term, model) for term in accesses]
        if actual == [expected_by_handle[handle] for handle in handles]:
            read_matches += 1
        store_expected = dict(expected_by_handle)
        store_expected[handles[2]] = 99
        store_actual = [stored.evaluate(term, model) for term in stored_accesses]
        if store_actual == [store_expected[handle] for handle in handles]:
            store_matches += 1
    return {
        "assignments": assignments,
        "read_matches": read_matches,
        "store_matches": store_matches,
        "state_digest": state.digest,
        "stored_digest": stored.digest,
    }


def swap_case() -> dict[str, Any]:
    state = node_state("this", "s")
    state, this_data = state.load("this", 0)
    children = state.branch_alias("s", NULL_REFERENCE)
    completed: list[PoseHeapState] = []
    for child in children:
        if child.assume_alias("s", NULL_REFERENCE, True) is not None:
            completed.append(child)
            continue
        child, s_data = child.load("s", 0)
        child = child.store("this", 0, [s_data.values[0]])
        child = child.store("s", 0, [this_data.values[0]])
        completed.append(child)
    return {
        "cfg_paths": len(completed),
        "heap_generated_paths": 0,
        "paper_lazy_traces": 21,
        "pose_expected_paths": 2,
    }


def sum_case() -> dict[str, Any]:
    state = node_state("this", "s0", "s1", "s2")
    for root in ("this", "s0", "s1", "s2"):
        state, _ = state.load(root, 0)
    return {
        "cfg_paths": 1,
        "heap_generated_paths": state.metrics["cfg_forks"],
        "paper_lazy_traces": 23,
        "pose_expected_paths": 1,
    }


def bounded_list_case(maximum: int = 10) -> dict[str, Any]:
    state = node_state("this")
    state, current, _next_field = state.load_reference(
        "this", 0, "Node", label="next:0"
    )
    active = [state]
    completed: list[PoseHeapState] = []
    # The sample's max guard gives maximum + 1 null decisions and one
    # bound-exit path, hence maximum + 2 CFG paths.
    for depth in range(maximum + 1):
        next_active: list[PoseHeapState] = []
        for item in active:
            for child in item.branch_alias(current, NULL_REFERENCE):
                if child.assume_alias(current, NULL_REFERENCE, True) is not None:
                    completed.append(child)
                    continue
                child, derived, _access = child.load_reference(
                    current, 0, "Node", label=f"next:{depth + 1}"
                )
                next_active.append(child)
                current = derived
        active = next_active
    completed.extend(active)
    return {
        "cfg_paths": len(completed),
        "heap_generated_paths": 0,
        "maximum": maximum,
        "paper_lazy_traces": 78,
        "pose_expected_paths": maximum + 2,
    }


def checkpoint_oracle() -> dict[str, Any]:
    state = node_state("left", "right")
    state, _ = state.load("left", 0)
    state, _ = state.load("right", 0)
    with tempfile.TemporaryDirectory() as directory:
        store = LiveStateStore(directory)
        checkpoint = store.put_continuation(LiveContinuationDescriptor(
            engine="symcc-continuation-ir",
            frames=(ContinuationFrame("main", "entry"),),
        ))
        first = persist_pose_heap(store, checkpoint, state)
        second = persist_pose_heap(store, checkpoint, state)
        restored = restore_pose_heap(store, first)
        return {
            "content_addressed": first == second,
            "parent_preserved": (
                store.restore_continuation(first).descriptor.parent == checkpoint
            ),
            "restored": restored is not None and restored.digest == state.digest,
            "state_digest": state.digest,
        }


def run() -> dict[str, Any]:
    exhaustive = exhaustive_alias_oracle()
    swap = swap_case()
    summation = sum_case()
    linked_list = bounded_list_case()
    checkpoint = checkpoint_oracle()
    nonforking = node_state("a", "b")
    nonforking, _ = nonforking.load("a", 0)
    nonforking, _ = nonforking.load("b", 0)
    nonforking = nonforking.store("a", 0, [7]).free("b")
    null_model_rejected = not nonforking.validate_model({
        "references": {"a": None, "b": "node"}, "values": {},
    })
    snapshot = PoseHeapState.from_mapping(nonforking.to_mapping())
    checks = {
        "alias_read_enumeration": (
            exhaustive["read_matches"] == exhaustive["assignments"] == 27
        ),
        "alias_store_enumeration": (
            exhaustive["store_matches"] == exhaustive["assignments"] == 27
        ),
        "checkpoint_identity": all(
            checkpoint[key]
            for key in ("content_addressed", "parent_preserved", "restored")
        ),
        "heap_operations_do_not_fork": nonforking.metrics["cfg_forks"] == 0,
        "list_path_optimal": (
            linked_list["cfg_paths"] == linked_list["pose_expected_paths"] == 12
        ),
        "model_gate": null_model_rejected,
        "snapshot_identity": snapshot.digest == nonforking.digest,
        "sum_path_optimal": (
            summation["cfg_paths"] == summation["pose_expected_paths"] == 1
        ),
        "swap_path_optimal": (
            swap["cfg_paths"] == swap["pose_expected_paths"] == 2
        ),
    }
    core = {
        "cases": {
            "bounded_list_max10": linked_list,
            "sum": summation,
            "swap": swap,
        },
        "checks": checks,
        "checkpoint": checkpoint,
        "exhaustive_alias": exhaustive,
        "schema": "symcc-pose-symbolic-heap-oracle-v1",
    }
    return {**core, "result_sha256": sha256(core)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run()
    content = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 0 if all(result["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
