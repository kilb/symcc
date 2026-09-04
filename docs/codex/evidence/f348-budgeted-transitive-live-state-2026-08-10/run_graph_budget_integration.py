#!/usr/bin/env python3
"""Exercise budgeted, memoized, transitive live-state restoration."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from unittest import mock


EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[3]
UTIL = REPO / "util"
if str(UTIL) not in sys.path:
    sys.path.insert(0, str(UTIL))

import distributed_state as state  # noqa: E402


def _rejection(operation) -> str:
    try:
        operation()
    except ValueError as error:
        return str(error)
    return ""


def _descriptor(
    *,
    solver: str = "",
    symbolic_store: str = "",
    memory: str = "",
    program: str = "",
    parent: str = "",
) -> state.LiveContinuationDescriptor:
    descriptor = state.LiveContinuationDescriptor.from_mapping({
        "schema": "symcc-live-continuation-v1",
        "engine": "symcc-continuation-ir",
        "path_condition_root": solver,
        "symbolic_store_root": symbolic_store,
        "symbolic_memory_root": memory,
        "program_root": program,
        "parent": parent,
    })
    assert descriptor is not None
    return descriptor


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="symcc-f348-integration-") as tmp:
        root = Path(tmp)
        graph_root = root / "graph"
        store = state.LiveStateStore(
            str(graph_root),
            page_size=64,
            max_graph_objects=32,
            max_graph_bytes=1024 * 1024,
        )
        expression = store.put_expression({
            "op": "equal",
            "children": ["input[0]", 65],
        })
        solver = store.put_solver_frame("", [expression, expression])
        symbolic_store = store.put_symbolic_store({
            "left": expression,
            "right": expression,
        })
        memory = store.create_memory(
            b"A" * 64,
            {0: expression, 1: expression},
        )
        page = dict(store.memory_pages(memory))[0]
        program = store.put_program({
            "entry": "main",
            "functions": {},
        })
        parent = store.put_continuation(_descriptor(
            solver=solver,
            symbolic_store=symbolic_store,
            memory=memory,
            program=program,
        ))
        checkpoint = store.put_continuation(_descriptor(
            solver=solver,
            symbolic_store=symbolic_store,
            memory=memory,
            program=program,
            parent=parent,
        ))
        object_ids = {
            expression,
            solver,
            symbolic_store,
            memory,
            page,
            program,
            parent,
            checkpoint,
        }
        canonical_bytes = sum(
            Path(store.object_path(object_id)).stat().st_size
            for object_id in object_ids
        )
        with mock.patch.object(
                state, "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot) as snapshots:
            restored = store.restore_continuation(checkpoint)

        exact = state.LiveStateStore(
            str(graph_root),
            page_size=64,
            max_graph_objects=len(object_ids),
            max_graph_bytes=canonical_bytes,
        ).restore_continuation(checkpoint)
        object_rejection = _rejection(lambda: state.LiveStateStore(
            str(graph_root),
            page_size=64,
            max_graph_objects=len(object_ids) - 1,
            max_graph_bytes=canonical_bytes,
        ).restore_continuation(checkpoint))
        byte_rejection = _rejection(lambda: state.LiveStateStore(
            str(graph_root),
            page_size=64,
            max_graph_objects=len(object_ids),
            max_graph_bytes=canonical_bytes - 1,
        ).restore_continuation(checkpoint))

        malformed_root = store._put_mapping({
            "schema": "symcc-live-memory-root-v1",
            "page_size": 64,
            "size": 64,
            "pages": [[0, expression]],
        })
        malformed_parent = store.put_continuation(_descriptor(
            memory=malformed_root,
        ))
        malformed_child = store.put_continuation(_descriptor(
            solver=solver,
            parent=malformed_parent,
        ))
        deep_rejection = _rejection(
            lambda: store.restore_continuation(malformed_child))

        duplicate_store = store._put_mapping({
            "schema": "symcc-live-symbolic-store-v1",
            "entries": [["x", expression], ["x", expression]],
        })
        duplicate_checkpoint = store.put_continuation(_descriptor(
            symbolic_store=duplicate_store,
        ))
        duplicate_rejection = _rejection(
            lambda: store.restore_continuation(duplicate_checkpoint))

        invalid_content = b"not-json"
        invalid_id = store._digest(invalid_content)
        invalid_path = Path(store.object_path(invalid_id))
        invalid_path.parent.mkdir(parents=True, exist_ok=True)
        invalid_path.write_bytes(invalid_content)
        semantic_rejection = _rejection(
            lambda: store._get_mapping(invalid_id))

        residue = sorted(
            path.name for path in root.rglob("*.tmp")
        )
        checks = {
            "transitive_parent_graph_round_trip_is_exact": (
                restored.checkpoint_id == checkpoint
                and restored.solver_frames == ((expression, expression),)
                and dict(restored.symbolic_store) == {
                    "left": expression,
                    "right": expression,
                }
                and restored.memory_size == 64
            ),
            "shared_merkle_nodes_are_read_and_charged_once": (
                restored.graph_object_count == len(object_ids)
                and restored.graph_canonical_bytes == canonical_bytes
                and snapshots.call_count == len(object_ids)
            ),
            "exact_object_and_byte_budgets_are_accepted": (
                exact.graph_object_count == len(object_ids)
                and exact.graph_canonical_bytes == canonical_bytes
            ),
            "one_less_unique_object_is_rejected": (
                object_rejection == "live-state graph exceeds object budget"
            ),
            "one_less_canonical_byte_is_rejected": (
                byte_rejection
                == "live-state graph exceeds canonical-byte budget"
            ),
            "invalid_page_in_parent_graph_is_rejected": (
                "symcc-live-memory-page-v1" in deep_rejection
            ),
            "duplicate_symbolic_names_are_rejected": (
                duplicate_rejection == "invalid symbolic store reference"
            ),
            "semantic_failure_revokes_positive_identity": (
                semantic_rejection == "invalid live-state JSON object"
                and invalid_id
                not in store._object_store._verified_identities
            ),
            "temporary_publication_names_are_cleaned": not residue,
        }
        observations = {
            "canonical_graph_bytes": canonical_bytes,
            "checkpoint_id": checkpoint,
            "deep_rejection": deep_rejection,
            "duplicate_rejection": duplicate_rejection,
            "exact_budget": {
                "canonical_bytes": exact.graph_canonical_bytes,
                "unique_objects": exact.graph_object_count,
            },
            "object_rejection": object_rejection,
            "byte_rejection": byte_rejection,
            "parent_chain_descriptors": 2,
            "stable_snapshot_reads": snapshots.call_count,
            "temporary_residue": residue,
            "unique_graph_objects": len(object_ids),
        }

    result = {
        "schema": "symcc-f348-budgeted-transitive-live-state-v1",
        "configuration": {
            "page_size": 64,
            "max_graph_objects": 32,
            "max_graph_bytes": 1024 * 1024,
            "source": (
                "real content-addressed files with deterministic malformed "
                "graph construction and exact boundary budgets"
            ),
        },
        "observations": observations,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "proof_boundary": (
            "This local mechanism integration exercises production graph "
            "restore, memoization, validation, and budget code. It does not "
            "execute MPI, a target, afl-showmap, a symbolic solver, or a "
            "fuzzing campaign, and makes no throughput, coverage, "
            "bug-discovery, or LAVA-M uplift claim."
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
