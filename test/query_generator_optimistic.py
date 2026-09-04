# REQUIRES: qsym
# RUN: python3 %s %querysolver

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from query_store import PersistentSubprocessSolver, QueryStore, solve_one  # noqa: E402


def envelope(value: int) -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "optimistic-generator-integration-test",
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
                "id": 2, "op": "ule", "bits": 1, "children": [0, 1],
                "attrs": {},
            },
            {
                "id": 3, "op": "constant", "bits": 8, "children": [],
                "attrs": {"value_hex": "08"},
            },
            {
                "id": 4, "op": "uge", "bits": 1, "children": [0, 3],
                "attrs": {},
            },
        ],
        "prefix_roots": [2],
        "target_root": 4,
        "input_hex": f"{value:02x}",
        "timeout_ms": 2000,
        "metadata": {"source": "optimistic-generator", "output_dir": ""},
        "prefix_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (bvule |0| #x0a))\n"
        ),
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (bvuge |0| #x08))\n"
        ),
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (bvule |0| #x0a))\n"
            "(assert (bvuge |0| #x08))\n"
        ),
    }


def solve(root: Path, solver_path: str, optimistic: bool) -> dict:
    os.environ["SYMCC_GENERATOR_OPTIMISTIC"] = "1" if optimistic else "0"
    os.environ["SYMCC_GENERATOR_SAMPLES"] = "8"
    os.environ["SYMCC_GENERATOR_MAX_VARS"] = "4"
    store = QueryStore(root)
    query_id, created = store.ingest(envelope(9))
    assert created
    with PersistentSubprocessSolver([solver_path]) as backend:
        assert solve_one(store, "optimistic-integration", backend) == "sat"
    return json.loads(
        (store.result_dir / query_id[:2] / f"{query_id}.json")
        .read_text(encoding="ascii"))


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("expected query solver path")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        optimistic = solve(root / "optimistic", sys.argv[1], True)
        exact = solve(root / "exact", sys.argv[1], False)

    generated = optimistic["generator"]
    certificate = generated["optimistic_simplification"]
    assert certificate["enabled"]
    assert certificate["strategy"] == "target-assertion-slice"
    assert certificate["kept_assertions"] == [1], certificate
    assert certificate["dropped_assertions"] == [0], certificate
    assert certificate["original_query_hash"] == optimistic["generator"]["query_id"]
    assert generated["ranges"] == [[0, 8, 255]], generated
    metrics = generated["metrics"]
    assert metrics["full_validation_checks"] >= 3, metrics
    assert metrics["full_validation_failures"] >= 2, metrics
    assert metrics["full_validation_accepted"] == len(
        generated["verified_models"])
    assert all(8 <= model["0"] <= 10 for model in generated["verified_models"])

    exact_generator = exact["generator"]
    exact_certificate = exact_generator["optimistic_simplification"]
    assert not exact_certificate["enabled"]
    assert exact_certificate["dropped_assertions"] == []
    assert exact_generator["ranges"] == [[0, 8, 10]], exact_generator
    assert exact_generator["metrics"]["full_validation_failures"] == 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
