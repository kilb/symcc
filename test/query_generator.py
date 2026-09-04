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


def envelope() -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "generator-integration-test",
        "nodes": [
            {
                "id": 0, "op": "read", "bits": 8, "children": [],
                "attrs": {"index": 0},
            },
            {
                "id": 1, "op": "constant", "bits": 8, "children": [],
                "attrs": {"value_hex": "0a"},
            },
            {
                "id": 2, "op": "uge", "bits": 1, "children": [0, 1],
                "attrs": {},
            },
            {
                "id": 3, "op": "constant", "bits": 8, "children": [],
                "attrs": {"value_hex": "14"},
            },
            {
                "id": 4, "op": "ule", "bits": 1, "children": [0, 3],
                "attrs": {},
            },
            {
                "id": 5, "op": "land", "bits": 1, "children": [2, 4],
                "attrs": {},
            },
            {
                "id": 6, "op": "bool", "bits": 1, "children": [],
                "attrs": {"value": True},
            },
        ],
        "prefix_roots": [6],
        "target_root": 5,
        "input_hex": "00",
        "timeout_ms": 2000,
        "metadata": {"source": "range", "output_dir": ""},
        "prefix_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert true)\n"
        ),
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (bvuge |0| #x0a))\n"
            "(assert (bvule |0| #x14))\n"
        ),
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (bvuge |0| #x0a))\n"
            "(assert (bvule |0| #x14))\n"
        ),
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("expected query solver path")
    os.environ["SYMCC_GENERATOR_SAMPLES"] = "4"
    os.environ["SYMCC_GENERATOR_MAX_VARS"] = "4"
    with tempfile.TemporaryDirectory() as temporary:
        store = QueryStore(temporary)
        query_id, created = store.ingest(envelope())
        assert created
        with PersistentSubprocessSolver([sys.argv[1]]) as backend:
            assert solve_one(store, "integration", backend) == "sat"

        result_path = (
            store.result_dir / query_id[:2] / f"{query_id}.json")
        result = json.loads(result_path.read_text(encoding="ascii"))
        generator = result["generator"]
        assert generator["ranges"] == [[0, 10, 20]], generator
        assert generator["fields"] == [[0]], generator
        assert generator["metrics"]["range_checks"] >= 8, generator
        assert generator["metrics"]["valid_models"] >= 3, generator
        assert len(result["generator_hash"]) == 64
        generator_path = (
            store.generator_dir / result["generator_hash"][:2] /
            f"{result['generator_hash']}.json")
        assert generator_path.is_file()

        candidates = {
            path.read_bytes()
            for path in store.candidate_dir.rglob("*.bin")
        }
        assert len(candidates) >= 4, candidates
        assert all(len(value) == 1 and 10 <= value[0] <= 20
                   for value in candidates), candidates
        stats = store.stats()
        assert stats["generators"] == 1
        assert stats["generator_models"] >= 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
