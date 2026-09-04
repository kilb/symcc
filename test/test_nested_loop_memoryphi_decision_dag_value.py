# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_nested_loop_memoryphi_decision_dag import benchmark  # noqa: E402
from check_nested_loop_memoryphi_decision_dag_oracles import (  # noqa: E402
    AffineArm,
    DecisionDagCase,
    Guard,
    loads,
    run_oracle,
    runtime_memory,
    selected_arm,
    summary_memory,
    supported,
)
from generate_nested_loop_memoryphi_decision_dag_fixture import (  # noqa: E402
    PREDICATES,
    generate,
)


def test_shared_dag_selects_one_or_two_guards() -> None:
    case = DecisionDagCase(
        Guard("ult", "inner", 2), Guard("ule", "outer", 1),
        shared_leaf=True,
    )
    assert selected_arm(case, 0, 0) == case.arm_a
    assert selected_arm(case, 0, 2) == case.arm_b
    assert selected_arm(case, 2, 0) == case.arm_b


def test_descending_last_write_matches_runtime_for_sampled_states() -> None:
    for shared_leaf in (False, True):
        for endianness in ("little", "big"):
            case = DecisionDagCase(
                Guard("ne", "inner", 2, True),
                Guard("ugt", "outer", 1),
                shared_leaf=shared_leaf,
                endianness=endianness,
            )
            for outer_count in range(4):
                for inner_count in range(4):
                    runtime = runtime_memory(
                        case,
                        outer_count=outer_count,
                        inner_count=inner_count,
                        payload=0xFFFF,
                    )
                    summary = summary_memory(
                        case,
                        outer_count=outer_count,
                        inner_count=inner_count,
                        payload=0xFFFF,
                    )
                    assert runtime == summary
                    assert loads(case, *runtime) == loads(case, *summary)


def test_generator_covers_predicates_sharing_and_endianness() -> None:
    for predicate in sorted(PREDICATES):
        fixture = generate(inner_predicate=predicate, root_predicate=predicate)
        assert f"%inner_guard = icmp {predicate} i16" in fixture
        assert f"%root_guard = icmp {predicate} i16" in fixture
        assert "%stored = select i1 %root_guard" in fixture
        assert "%inner_selected = select i1 %inner_guard" in fixture
        assert "%stored = select i1 %root_guard, i16 %inner_selected, i16 %value_b" in fixture
    unshared_big = generate(shared_leaf=False, endianness="big")
    assert 'target datalayout = "E-p:64:64"' in unshared_big
    assert "%value_c" in unshared_big
    mixed = generate(mixed_piecewise_writer=True)
    assert "%direct_value = select i1 %direct_guard" in mixed
    assert mixed.count("store i16") == 2
    subtree = generate(shared_subtree=True)
    assert subtree.count("%inner_selected") >= 3
    assert "%shared_subtree = select i1 %subtree_guard" in subtree


def test_unsupported_guards_and_degenerate_dags_fail_closed() -> None:
    assert not supported(
        DecisionDagCase(Guard("slt", "inner", 2), Guard("ule", "outer", 1))
    )
    assert not supported(
        DecisionDagCase(Guard("ult", "input", 2), Guard("ule", "outer", 1))
    )
    degenerate = DecisionDagCase(
        Guard("ult", "inner", 2),
        Guard("ule", "outer", 1),
        arm_b=AffineArm(17, 5, 7, 3),
        arm_c=AffineArm(17, 5, 7, 3),
        shared_leaf=False,
    )
    assert not supported(degenerate)


def test_independent_oracle_and_reference_benchmark_close() -> None:
    assert run_oracle() == {
        "schema": "symcc-nested-loop-memoryphi-decision-dag-oracle-v1",
        "all_passed": True,
        "configurations": 1152,
        "runtime_load_vector_equivalences": 55296,
        "runtime_scalar_load_equivalences": 608256,
        "runtime_defined_byte_equivalences": 165888,
        "runtime_complete_loads": 114048,
        "runtime_uninitialized_or_partial_loads": 494208,
        "shared_and_unshared_dags": True,
        "specialized_path_lengths": {
            "one_guard": 3456,
            "two_guards": 3456,
        },
        "unsupported_cases_rejected": 6,
        "claim_boundary": (
            "Finite two-level i16 affine Decision DAG and descending last-write "
            "equivalence only; not executable loop replacement, general LoopSCC, "
            "coverage, solver time, defect yield, or end-to-end speedup"
        ),
    }
    result = benchmark(repeats=3, iterations=10)
    assert result["all_passed"] is True
    assert result["parameters"] == {
        "repeats": 3,
        "iterations_per_repeat": 10,
        "dag_nodes": 4,
        "guard_nodes": 2,
        "affine_leaves": 2,
        "shared_child_edges": 2,
        "maximum_depth": 2,
    }
    assert result["median_validation_ns_per_dag"] > 0
    assert result["median_guard_selection_ns_per_instance"] > 0
    assert result["median_last_write_summary_ns_per_query"] > 0
