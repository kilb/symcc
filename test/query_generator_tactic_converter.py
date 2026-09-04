# REQUIRES: qsym
# RUN: python3 %s %querysolver

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from query_store import PersistentSubprocessSolver, QueryStore, solve_one  # noqa: E402


def envelope() -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "tactic-model-converter-integration-test",
        "nodes": [
            {
                "id": 0, "op": "read", "bits": 8, "children": [],
                "attrs": {"index": 0},
            },
            {
                "id": 1, "op": "read", "bits": 8, "children": [],
                "attrs": {"index": 1},
            },
            {
                "id": 2, "op": "constant", "bits": 8, "children": [],
                "attrs": {"value_hex": "01"},
            },
            {
                "id": 3, "op": "add", "bits": 8, "children": [1, 2],
                "attrs": {},
            },
            {
                "id": 4, "op": "equal", "bits": 1, "children": [0, 3],
                "attrs": {},
            },
            {
                "id": 5, "op": "constant", "bits": 8, "children": [],
                "attrs": {"value_hex": "08"},
            },
            {
                "id": 6, "op": "uge", "bits": 1, "children": [1, 5],
                "attrs": {},
            },
            {
                "id": 7, "op": "constant", "bits": 8, "children": [],
                "attrs": {"value_hex": "0a"},
            },
            {
                "id": 8, "op": "ule", "bits": 1, "children": [1, 7],
                "attrs": {},
            },
            {
                "id": 9, "op": "land", "bits": 1, "children": [4, 6],
                "attrs": {},
            },
            {
                "id": 10, "op": "land", "bits": 1, "children": [9, 8],
                "attrs": {},
            },
            {
                "id": 11, "op": "bool", "bits": 1, "children": [],
                "attrs": {"value": True},
            },
        ],
        "prefix_roots": [11],
        "target_root": 10,
        "input_hex": "0908",
        "timeout_ms": 2000,
        "metadata": {"source": "tactic-converter", "output_dir": ""},
        "prefix_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(declare-fun |1| () (_ BitVec 8))\n"
            "(assert true)\n"
        ),
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(declare-fun |1| () (_ BitVec 8))\n"
            "(assert (= |0| (bvadd |1| #x01)))\n"
            "(assert (bvuge |1| #x08))\n"
            "(assert (bvule |1| #x0a))\n"
        ),
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(declare-fun |1| () (_ BitVec 8))\n"
            "(assert (= |0| (bvadd |1| #x01)))\n"
            "(assert (bvuge |1| #x08))\n"
            "(assert (bvule |1| #x0a))\n"
        ),
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("expected query solver path")
    os.environ["SYMCC_GENERATOR_OPTIMISTIC"] = "1"
    os.environ["SYMCC_GENERATOR_TACTIC_CONVERTER"] = "1"
    os.environ["SYMCC_GENERATOR_CONVERTER_SAMPLES"] = "2"
    os.environ["SYMCC_GENERATOR_SAMPLES"] = "4"
    with tempfile.TemporaryDirectory() as temporary:
        store = QueryStore(temporary)
        query_id, created = store.ingest(envelope())
        assert created
        with PersistentSubprocessSolver([sys.argv[1]]) as backend:
            assert solve_one(store, "tactic-converter", backend) == "sat"
        result = json.loads(
            (store.result_dir / query_id[:2] / f"{query_id}.json")
            .read_text(encoding="ascii"))

    generator = result["generator"]
    converter = generator["tactic_model_converter"]
    assert converter["enabled"] and converter["applied"], converter
    assert converter["pipeline"] == ["simplify", "solve-eqs"], converter
    assert converter["subgoals"] == 1, converter
    assert converter["converted_models"] >= 2, converter
    assert converter["full_validation_accepted"] >= 1, converter
    assert converter["full_validation_failures"] == 0, converter
    assert converter["original_query_hash"] == query_id, converter
    models = generator["verified_models"]
    assert all(model["0"] == (model["1"] + 1) & 0xFF for model in models)
    assert all(8 <= model["1"] <= 10 for model in models)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
