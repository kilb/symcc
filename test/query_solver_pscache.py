# REQUIRES: qsym
# RUN: python3 %s %querysolver

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from query_store import (  # noqa: E402
    PersistentSubprocessSolver,
    QueryStore,
    solve_one,
)


def equality_envelope() -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "solver-pscache-test",
        "nodes": [
            {"id": 0, "op": "read", "bits": 8, "children": [],
             "attrs": {"index": 0}},
            {"id": 1, "op": "constant", "bits": 8, "children": [],
             "attrs": {"value_hex": "42"}},
            {"id": 2, "op": "equal", "bits": 1, "children": [0, 1],
             "attrs": {}},
        ],
        "prefix_roots": [],
        "target_root": 2,
        "input_hex": "00",
        "timeout_ms": 1000,
        "metadata": {"source": "eq", "output_dir": ""},
        "prefix_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert true)\n"
        ),
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x42))\n"
        ),
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x42))\n"
        ),
    }


def range_envelope() -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "solver-pscache-test",
        "nodes": [
            {"id": 0, "op": "read", "bits": 8, "children": [],
             "attrs": {"index": 0}},
            {"id": 1, "op": "constant", "bits": 8, "children": [],
             "attrs": {"value_hex": "40"}},
            {"id": 2, "op": "uge", "bits": 1, "children": [0, 1],
             "attrs": {}},
            {"id": 3, "op": "constant", "bits": 8, "children": [],
             "attrs": {"value_hex": "50"}},
            {"id": 4, "op": "ule", "bits": 1, "children": [0, 3],
             "attrs": {}},
            {"id": 5, "op": "land", "bits": 1, "children": [2, 4],
             "attrs": {}},
        ],
        "prefix_roots": [],
        "target_root": 5,
        "input_hex": "00",
        "timeout_ms": 1000,
        "metadata": {"source": "range", "output_dir": ""},
        "prefix_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert true)\n"
        ),
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (bvuge |0| #x40))\n"
            "(assert (bvule |0| #x50))\n"
        ),
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (bvuge |0| #x40))\n"
            "(assert (bvule |0| #x50))\n"
        ),
    }


def inconsistent_envelope() -> dict:
    envelope = equality_envelope()
    envelope["producer"] = "solver-pscache-inconsistent-test"
    envelope["nodes"][1]["attrs"]["value_hex"] = "41"
    envelope["prefix_roots"] = [2]
    envelope["nodes"].extend([
        {"id": 3, "op": "constant", "bits": 8, "children": [],
         "attrs": {"value_hex": "42"}},
        {"id": 4, "op": "equal", "bits": 1, "children": [0, 3],
         "attrs": {}},
    ])
    envelope["target_root"] = 4
    envelope["input_hex"] = "41"
    envelope["prefix_smt2"] = (
        "(declare-fun |0| () (_ BitVec 8))\n"
        "(assert (= |0| #x41))\n"
    )
    envelope["target_smt2"] = (
        "(declare-fun |0| () (_ BitVec 8))\n"
        "(assert (= |0| #x42))\n"
    )
    envelope["smt2"] = (
        "(declare-fun |0| () (_ BitVec 8))\n"
        "(assert (= |0| #x41))\n"
        "(assert (= |0| #x42))\n"
    )
    return envelope


def main() -> int:
    os.environ["SYMCC_SOLVER_PSCACHE"] = "1"
    os.environ["SYMCC_SOLVER_PSCACHE_PROBES"] = "4"
    os.environ["SYMCC_SOLVER_PSCACHE_TIMEOUT"] = "100"
    os.environ["SYMCC_SOLVER_PSCACHE_CONFLICTS"] = "1"
    os.environ["SYMCC_SOLVER_PSCACHE_CONFLICT_CORE_CHECKS"] = "8"
    os.environ["SYMCC_SOLVER_PSCACHE_CONFLICT_TIMEOUT"] = "100"
    os.environ["SYMCC_GENERATOR_SAMPLES"] = "0"
    with tempfile.TemporaryDirectory() as temporary:
        store = QueryStore(temporary)
        first_id, _ = store.ingest(equality_envelope())
        second_id, _ = store.ingest(range_envelope())
        with PersistentSubprocessSolver([sys.argv[1]]) as backend:
            assert solve_one(store, "worker", backend) == "sat"
            assert solve_one(store, "worker", backend) == "sat"
        first = json.loads(
            (store.result_dir / first_id[:2] / f"{first_id}.json")
            .read_text(encoding="ascii"))
        second = json.loads(
            (store.result_dir / second_id[:2] / f"{second_id}.json")
            .read_text(encoding="ascii"))
        assert first["solver_pscache_hit"] is False, first
        assert first["solver_pscache_conflict_checks"] == 3, first
        assert len(first["solver_pscache_conflict_solutions"]) == 1, first
        first_conflict = first["solver_pscache_conflict_solutions"][0]
        assert first_conflict["assignments"] == {"0": 0}, first_conflict
        assert first_conflict["core_assignments"] == {"0": 0}, first_conflict
        assert first_conflict["proof_verified"] is True, first_conflict
        assert first_conflict["core_minimal"] is True, first_conflict
        assert second["solver_pscache_hit"] is True, second
        assert second["solver"] == "z3-pscache", second
        assert second["assignments"]["0"] == 66, second
        stats = store.stats()
        assert stats["solver_pscache_hits"] == 1
        assert stats["solver_pscache_conflict_solutions"] >= 2
        assert stats["conflict_partial_solutions"] >= 2

    # An assignment-independent contradiction must not be mislabeled as an
    # assumption conflict: core minimization reaches the empty core and drops it.
    with tempfile.TemporaryDirectory() as temporary:
        store = QueryStore(temporary)
        query_id, _ = store.ingest(inconsistent_envelope())
        with PersistentSubprocessSolver([sys.argv[1]]) as backend:
            assert solve_one(store, "worker", backend) == "unsat"
        result = json.loads(
            (store.result_dir / query_id[:2] / f"{query_id}.json")
            .read_text(encoding="ascii"))
        assert result["solver_pscache_conflict_solutions"] == [], result
        assert store.stats()["conflict_partial_solutions"] == 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
