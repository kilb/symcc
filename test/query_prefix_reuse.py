# REQUIRES: qsym
# RUN: %python %s %querysolver

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from query_store import (  # noqa: E402
    PersistentSubprocessSolver,
    QueryStore,
    solve_one,
)


def envelope(target: int) -> dict:
    return {
        "schema": "symcc-query-ir-v1",
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
                "attrs": {"value_hex": f"{target:02x}"},
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
        "metadata": {},
        "timeout_ms": 1000,
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x41))\n"
            f"(assert (= |0| #x{target:02x}))\n"
        ),
        "prefix_smt2": ("(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x41))\n"),
        "target_smt2": (
            f"(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x{target:02x}))\n"
        ),
    }


with tempfile.TemporaryDirectory() as temporary:
    store = QueryStore(temporary)
    store.ingest(envelope(0x42))
    store.ingest(envelope(0x43))
    with PersistentSubprocessSolver([sys.argv[1]]) as solver:

        def solve_after_path_replacement(lease):
            lease.smt2_path.write_bytes(b"(assert false)\n")
            lease.prefix_smt2_path.write_bytes(b"(assert false)\n")
            lease.target_smt2_path.write_bytes(b"(assert false)\n")
            return solver(lease)

        assert solve_one(store, "worker", solve_after_path_replacement) == "unsat"
        # Repair the shared prefix pathname before the next claim. The first
        # lease's sealed descriptors, rather than this repair, supplied the
        # already cached prefix to the helper.
        store.ingest(envelope(0x42))
        assert solve_one(store, "worker", solve_after_path_replacement) == "unsat"

    database = sqlite3.connect(store.db_path)
    results = [
        json.loads(row[0])
        for row in database.execute(
            "SELECT result_json FROM results ORDER BY completed"
        )
    ]
    assert len(results) == 2
    assert results[0]["prefix_cache_hit"] is False
    assert results[1]["prefix_cache_hit"] is True
    assert results[1]["prefix_cache_entries"] == 1
