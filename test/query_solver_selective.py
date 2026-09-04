# REQUIRES: qsym
# RUN: python3 %s %querysolver

import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from query_store import (  # noqa: E402
    PersistentSubprocessSolver,
    QueryStore,
    solve_one,
)


def envelope(
    witness: bytes,
    *,
    prefix_values: tuple[int, int] = (5, 7),
    target_value: int = 0x42,
) -> dict:
    prefix_left, prefix_right = prefix_values
    nodes = [
        {"id": 0, "op": "read", "bits": 8, "children": [],
         "attrs": {"index": 0}},
        {"id": 1, "op": "read", "bits": 8, "children": [],
         "attrs": {"index": 1}},
        {"id": 2, "op": "read", "bits": 8, "children": [],
         "attrs": {"index": 2}},
        {"id": 3, "op": "constant", "bits": 8, "children": [],
         "attrs": {"value_hex": f"{prefix_left:02x}"}},
        {"id": 4, "op": "equal", "bits": 1, "children": [1, 3],
         "attrs": {}},
        {"id": 5, "op": "constant", "bits": 8, "children": [],
         "attrs": {"value_hex": f"{prefix_right:02x}"}},
        {"id": 6, "op": "equal", "bits": 1, "children": [2, 5],
         "attrs": {}},
        {"id": 7, "op": "constant", "bits": 8, "children": [],
         "attrs": {"value_hex": f"{target_value:02x}"}},
        {"id": 8, "op": "equal", "bits": 1, "children": [0, 7],
         "attrs": {}},
    ]
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "selective-query-test",
        "nodes": nodes,
        "prefix_roots": [4, 6],
        "target_root": 8,
        "input_hex": witness.hex(),
        "timeout_ms": 1000,
        "metadata": {"source": "selective", "output_dir": ""},
        "prefix_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(declare-fun |1| () (_ BitVec 8))\n"
            "(declare-fun |2| () (_ BitVec 8))\n"
            f"(assert (= |1| #x{prefix_left:02x}))\n"
            f"(assert (= |2| #x{prefix_right:02x}))\n"
        ),
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            f"(assert (= |0| #x{target_value:02x}))\n"
        ),
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(declare-fun |1| () (_ BitVec 8))\n"
            "(declare-fun |2| () (_ BitVec 8))\n"
            f"(assert (= |1| #x{prefix_left:02x}))\n"
            f"(assert (= |2| #x{prefix_right:02x}))\n"
            f"(assert (= |0| #x{target_value:02x}))\n"
        ),
    }


def graph_partition_envelope(witness: bytes) -> dict:
    nodes = [
        {"id": 0, "op": "read", "bits": 8, "children": [],
         "attrs": {"index": 0}},
        {"id": 1, "op": "read", "bits": 8, "children": [],
         "attrs": {"index": 1}},
        {"id": 2, "op": "read", "bits": 8, "children": [],
         "attrs": {"index": 2}},
        {"id": 3, "op": "read", "bits": 8, "children": [],
         "attrs": {"index": 3}},
        {"id": 4, "op": "constant", "bits": 8, "children": [],
         "attrs": {"value_hex": "02"}},
        {"id": 5, "op": "add", "bits": 8, "children": [3, 4],
         "attrs": {}},
        {"id": 6, "op": "equal", "bits": 1, "children": [1, 5],
         "attrs": {}},
        {"id": 7, "op": "equal", "bits": 1, "children": [1, 2],
         "attrs": {}},
        {"id": 8, "op": "add", "bits": 8, "children": [3, 3],
         "attrs": {}},
        {"id": 9, "op": "equal", "bits": 1, "children": [2, 8],
         "attrs": {}},
        {"id": 10, "op": "mul", "bits": 8, "children": [0, 0],
         "attrs": {}},
        {"id": 11, "op": "mul", "bits": 8, "children": [10, 0],
         "attrs": {}},
        {"id": 12, "op": "ugt", "bits": 1, "children": [11, 1],
         "attrs": {}},
    ]
    declarations = (
        "(declare-fun |0| () (_ BitVec 8))\n"
        "(declare-fun |1| () (_ BitVec 8))\n"
        "(declare-fun |2| () (_ BitVec 8))\n"
        "(declare-fun |3| () (_ BitVec 8))\n"
    )
    prefix = (
        "(assert (= |1| (bvadd |3| #x02)))\n"
        "(assert (= |1| |2|))\n"
        "(assert (= |2| (bvadd |3| |3|)))\n"
    )
    target = "(assert (bvugt (bvmul (bvmul |0| |0|) |0|) |1|))\n"
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "selective-graph-test",
        "nodes": nodes,
        "prefix_roots": [6, 7, 9],
        "target_root": 12,
        "input_hex": witness.hex(),
        "timeout_ms": 1000,
        "metadata": {"source": "selective-graph", "output_dir": ""},
        "prefix_smt2": declarations + prefix,
        "target_smt2": declarations + target,
        "smt2": declarations + prefix + target,
    }


def solve_with_backend(
    store: QueryStore,
    backend: PersistentSubprocessSolver,
    body: dict,
) -> dict:
    query_id, created = store.ingest(body)
    assert created
    assert solve_one(store, "selective-test", backend) == "sat"
    result = json.loads(
        (store.result_dir / query_id[:2] / f"{query_id}.json")
        .read_text(encoding="ascii")
    )
    result["_stats"] = store.stats()
    return result


def solve_case(root: str, solver_path: str, body: dict) -> dict:
    store = QueryStore(root)
    with PersistentSubprocessSolver([solver_path]) as backend:
        return solve_with_backend(store, backend, body)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("expected query solver path")
    os.environ["SYMCC_SELECTIVE_QUERY"] = "1"
    os.environ["SYMCC_SELECTIVE_QUERY_MIN_FIXED"] = "2"
    os.environ["SYMCC_SELECTIVE_QUERY_MAX_FIXED"] = "8"
    os.environ["SYMCC_SELECTIVE_QUERY_MAX_SYMBOLIC"] = "4"
    os.environ["SYMCC_SELECTIVE_QUERY_TIMEOUT"] = "100"
    os.environ["SYMCC_SELECTIVE_QUERY_LEARNING"] = "0"
    os.environ["SYMCC_SELECTIVE_QUERY_COMPLETIONS"] = "1"
    os.environ["SYMCC_SELECTIVE_QUERY_GRAPH_PARTITION"] = "1"
    os.environ["SYMCC_SELECTIVE_QUERY_GRAPH_MIN_COSTLY"] = "1"
    os.environ["SYMCC_SOLVER_PSCACHE"] = "0"
    os.environ["SYMCC_GENERATOR_SAMPLES"] = "0"
    with tempfile.TemporaryDirectory() as temporary:
        hit = solve_case(
            str(Path(temporary) / "hit"),
            sys.argv[1],
            envelope(b"\x00\x05\x07"),
        )
        assert hit["solver"] == "z3-selective", hit
        assert hit["selective_query_attempted"] is True, hit
        assert hit["selective_query_hit"] is True, hit
        assert hit["selective_query_eligible"] is True, hit
        assert hit["selective_query_symbolic"] == 1, hit
        assert hit["selective_query_fixed"] == 2, hit
        assert hit["selective_query_policy_decision"] == "structural", hit
        assert hit["assignments"] == {"0": 66, "1": 5, "2": 7}, hit
        assert hit["_stats"]["selective_query_hits"] == 1, hit

        fallback = solve_case(
            str(Path(temporary) / "fallback"),
            sys.argv[1],
            envelope(b"\x00\x00\x00"),
        )
        assert fallback["solver"] == "z3", fallback
        assert fallback["selective_query_attempted"] is True, fallback
        assert fallback["selective_query_hit"] is False, fallback
        assert fallback["status"] == "sat", fallback
        assert fallback["assignments"] == {"0": 66, "1": 5, "2": 7}, fallback
        assert fallback["_stats"]["selective_query_attempts"] == 1, fallback
        assert fallback["_stats"]["selective_query_hits"] == 0, fallback

        # A bounded completion portfolio can solve a disconnected component
        # without weakening the full query. Completion 3 is the all-0xff
        # boundary assignment; completion 2 is deduplicated against the seed.
        os.environ["SYMCC_SELECTIVE_QUERY_COMPLETIONS"] = "3"
        mixed = solve_case(
            str(Path(temporary) / "mixed"),
            sys.argv[1],
            envelope(
                b"\x00\x00\x00",
                prefix_values=(0xff, 0xff),
                target_value=0x41,
            ),
        )
        assert mixed["solver"] == "z3-selective", mixed
        assert mixed["assignments"] == {"0": 65, "1": 255, "2": 255}, mixed
        assert mixed["selective_query_completions"] == 2, mixed
        assert mixed["selective_query_hit_completion"] == 3, mixed
        assert mixed["_stats"]["selective_query_mixed_hits"] == 1, mixed

        # The nonlinear target atom and three linear prefix atoms form a
        # weighted relation graph.  The partial solver fixes the shared y
        # boundary, the random side completes x, and the full formula remains
        # the final SAT authority.
        os.environ["SYMCC_SELECTIVE_QUERY_MIN_FIXED"] = "1"
        os.environ["SYMCC_SELECTIVE_QUERY_COMPLETIONS"] = "1"
        graph_hit = solve_case(
            str(Path(temporary) / "graph-hit"),
            sys.argv[1],
            graph_partition_envelope(b"\x02\x04\x04\x02"),
        )
        assert graph_hit["solver"] == "z3-selective-graph", graph_hit
        assert graph_hit["selective_query_hit"] is True, graph_hit
        assert graph_hit["selective_query_partition_mode"] == (
            "relation-graph-v1"), graph_hit
        assert graph_hit["selective_query_smt_assertions"] == 3, graph_hit
        assert graph_hit["selective_query_random_assertions"] == 1, graph_hit
        assert graph_hit["selective_query_shared_variables"] == 1, graph_hit
        assert graph_hit["selective_query_relation_edges"] == 4, graph_hit
        assert graph_hit["selective_query_cut_weight"] == 1, graph_hit
        assert graph_hit["selective_query_partial_status"] == "sat", graph_hit
        assert graph_hit["assignments"] == {
            "0": 2, "1": 4, "2": 4, "3": 2,
        }, graph_hit
        assert graph_hit["_stats"]["selective_query_graph_attempts"] == 1
        assert graph_hit["_stats"]["selective_query_graph_hits"] == 1

        # A bad random-side completion is only a selective miss.  It must not
        # authorize UNSAT or prune the full query.
        graph_miss = solve_case(
            str(Path(temporary) / "graph-miss"),
            sys.argv[1],
            graph_partition_envelope(b"\x00\x04\x04\x02"),
        )
        assert graph_miss["solver"] == "z3", graph_miss
        assert graph_miss["status"] == "sat", graph_miss
        assert graph_miss["selective_query_attempted"] is True, graph_miss
        assert graph_miss["selective_query_hit"] is False, graph_miss
        assert graph_miss["selective_query_partial_status"] == "sat", graph_miss
        assert graph_miss["selective_query_partition_mode"] == (
            "relation-graph-v1"), graph_miss
        assert graph_miss["_stats"]["selective_query_graph_attempts"] == 1
        assert graph_miss["_stats"]["selective_query_graph_hits"] == 0

        # Two known misses train a context to skip an expensive low-yield
        # probe. The state is loaded by a fresh helper process.
        policy_state = Path(temporary) / "selective-policy.state"
        os.environ["SYMCC_SELECTIVE_QUERY_LEARNING"] = "1"
        os.environ["SYMCC_SELECTIVE_QUERY_COMPLETIONS"] = "1"
        os.environ["SYMCC_SELECTIVE_QUERY_POLICY_EXPLORE"] = "2"
        os.environ[
            "SYMCC_SELECTIVE_QUERY_POLICY_MIN_SAVINGS_US"
        ] = "1000000000"
        os.environ["SYMCC_SELECTIVE_QUERY_POLICY_SAVE_INTERVAL"] = "1"
        os.environ["SYMCC_SELECTIVE_QUERY_POLICY_STATE"] = str(policy_state)
        policy_store = QueryStore(str(Path(temporary) / "policy"))
        with PersistentSubprocessSolver([sys.argv[1]]) as backend:
            first = solve_with_backend(
                policy_store, backend,
                envelope(b"\x00\x00\x00", target_value=0x43))
            second = solve_with_backend(
                policy_store, backend,
                envelope(b"\x00\x00\x00", target_value=0x44))
            skipped = solve_with_backend(
                policy_store, backend,
                envelope(b"\x00\x00\x00", target_value=0x45))
        assert first["selective_query_policy_decision"] == "explore", first
        assert second["selective_query_policy_decision"] == "explore", second
        assert skipped["selective_query_eligible"] is True, skipped
        assert skipped["selective_query_attempted"] is False, skipped
        assert skipped["selective_query_policy_decision"] == "learned-skip", skipped
        assert skipped["selective_query_policy_observations"] == 3, skipped
        assert policy_state.read_text(encoding="ascii").startswith(
            "symcc-selective-query-policy-v1\n")

        with PersistentSubprocessSolver([sys.argv[1]]) as backend:
            restored = solve_with_backend(
                policy_store, backend,
                envelope(b"\x00\x00\x00", target_value=0x46))
        assert restored["selective_query_attempted"] is False, restored
        assert restored["selective_query_policy_decision"] == "learned-skip", restored
        assert restored["selective_query_policy_observations"] == 4, restored
        assert restored["_stats"]["selective_query_policy_skips"] == 2, restored
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
