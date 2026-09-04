# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_nested_loop_memoryphi_summary import benchmark  # noqa: E402
from check_nested_loop_memoryphi_summary_oracles import (  # noqa: E402
    NestedSummaryCase,
    composed_summary_state,
    make_certificate,
    mathematically_supported,
    mutation_oracle,
    run_oracle,
    runtime_state,
    validate_certificate,
)
from generate_nested_loop_memoryphi_summary_fixture import generate  # noqa: E402


def test_inner_effect_composes_across_outer_iterations() -> None:
    case = NestedSummaryCase(
        8, 0, 1, 0, 2, 8, 64, 2, tuple(range(7)), (1, 2)
    )
    for outer_count in range(4):
        for inner_count in range(10):
            assert runtime_state(
                case, outer_count=outer_count, inner_count=inner_count
            ) == composed_summary_state(
                case, outer_count=outer_count, inner_count=inner_count
            )


def test_v6_certificate_binds_two_memoryphis_and_ordered_writers() -> None:
    case = NestedSummaryCase(
        8, 0, 1, 0, 2, 8, 64, 2, tuple(range(7)), (1, 2)
    )
    certificate = make_certificate(case)
    assert validate_certificate(certificate, case)
    assert certificate["schema"].endswith("v6")
    assert certificate["memory_phis"] == {
        "equation": "H_outer=phi(entry,S_inner(H_outer))",
        "outer_backedge": "inner-memory-phi-summary",
        "inner_preheader": "outer-memory-phi",
        "inner_backedge": "ordered-writer-memory-def-chain",
    }
    assert certificate["summary"]["fixed_point"]["semantics"] == (
        "inner-to-outer-ordered-writer-summary"
    )
    assert [writer["ordinal"] for writer in certificate["summary"]["writers"]] == [
        0,
        1,
    ]


def test_unsupported_topology_effects_and_mutations_fail_closed() -> None:
    base = dict(
        object_bytes=8,
        outer_seed=0,
        outer_step=1,
        inner_seed=0,
        inner_step=2,
        bound_bits=8,
        induction_bits=64,
        load_bytes=2,
        load_offsets=tuple(range(7)),
        writers=(1, 2),
    )
    assert not mathematically_supported(
        NestedSummaryCase(**base, outer_invariant_inner_bound=False)
    )
    assert not mathematically_supported(
        NestedSummaryCase(**base, extra_memory_effects=1)
    )
    assert not mathematically_supported(
        NestedSummaryCase(**base, nesting_depth=3)
    )
    assert not mathematically_supported(
        NestedSummaryCase(**{**base, "writers": (1, 1, 1, 1, 1)})
    )
    assert mutation_oracle() == 26


def test_finite_nested_summary_oracle_closes() -> None:
    assert run_oracle() == {
        "schema": "symcc-nested-loop-memoryphi-summary-oracle-v1",
        "all_passed": True,
        "cases": 18240,
        "accepted_nested_summaries": 670,
        "rejected_unsupported_or_partial": 17570,
        "byte_lane_checks": 68160,
        "runtime_bitmap_equivalences": 21480,
        "last_writer_provenance_equivalence_cells": 52662,
        "runtime_complete_load_accepts": 24084,
        "runtime_zero_outer_or_prefix_rejects": 25932,
        "mutations_rejected": 26,
        "claim_boundary": (
            "Finite reference-model bitmap and last-writer equivalence only; "
            "not LLVM build cost, coverage, solver speed, defect yield, or "
            "end-to-end speedup"
        ),
    }


def test_generator_and_reference_benchmark_boundary() -> None:
    fixture = generate(64, 1, 8, (1, 2, 4, 8), 8)
    assert "alloca [64 x i8]" in fixture
    assert fixture.count("store i") == 4
    assert "%inner_next = add nuw i64 %inner_iv, 8" in fixture
    assert "%outer_next = add nuw i64 %outer_iv, 1" in fixture
    result = benchmark(repeats=3, iterations=1)
    assert result["all_certificates_valid"] is True
    assert result["analytic_certificate_cardinality"] == {
        "loop_levels": 2,
        "memory_phi_nodes": 2,
        "writer_metadata_records": 4,
        "reachable_writer_instances": 32,
        "potential_writer_bytes": 120,
        "load_aliases": 57,
        "byte_lane_witnesses": 456,
        "witness_alternatives": 855,
        "fixed_point_rounds_including_stability": 9,
    }
    assert result["median_ns_per_certificate"] > 0
