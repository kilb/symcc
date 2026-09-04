# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from check_nested_loop_memoryphi_two_dimensional_affine_oracles import (  # noqa: E402
    TwoDimensionalCase,
    make_certificate,
    mutation_oracle,
    run_oracle,
    runtime_loads,
    summary_loads,
    validate_certificate,
)
from benchmark_nested_loop_memoryphi_two_dimensional_affine import (  # noqa: E402
    benchmark,
)
from generate_nested_loop_memoryphi_two_dimensional_affine_fixture import (  # noqa: E402
    generate,
)


def _case(endianness: str = "little") -> TwoDimensionalCase:
    return TwoDimensionalCase(
        12, 1, 2, 2, 2, 0, 4, 1, 1, 2, tuple(range(11)),
        (2, 1), (4660, -86), endianness,
    )


def test_v8_binds_two_dimensional_instances_and_endian_value_bytes() -> None:
    little = make_certificate(_case("little"))
    big = make_certificate(_case("big"))
    assert validate_certificate(little, _case("little"))
    assert validate_certificate(big, _case("big"))
    assert little["schema"].endswith("v8")
    assert little["writers"][0]["stored_value"]["bytes"] == [0x34, 0x12]
    assert big["writers"][0]["stored_value"]["bytes"] == [0x12, 0x34]
    instances = little["writers"][0]["instances"]
    assert [
        (
            item["outer_induction_value"],
            item["inner_induction_value"],
            item["index_value"],
        )
        for item in instances
    ] == [
        (0, 0, 0), (0, 2, 2), (1, 0, 4),
        (1, 2, 6), (2, 0, 8), (2, 2, 10),
    ]


def test_lexicographic_first_match_equals_concrete_nested_runtime() -> None:
    for endianness in ("little", "big"):
        case = _case(endianness)
        certificate = make_certificate(case)
        for outer_count in range(4):
            for inner_count in range(4):
                assert runtime_loads(
                    case, outer_count=outer_count, inner_count=inner_count,
                ) == summary_loads(
                    certificate,
                    outer_count=outer_count,
                    inner_count=inner_count,
                )


def test_two_dimensional_metadata_mutations_fail_closed() -> None:
    assert mutation_oracle() == 18


def test_finite_two_dimensional_oracle_closes() -> None:
    assert run_oracle() == {
        "schema": "symcc-nested-loop-memoryphi-two-dimensional-affine-oracle-v1",
        "all_passed": True,
        "cases": 4608,
        "accepted_two_dimensional_summaries": 28,
        "rejected_unsupported_or_partial": 4580,
        "runtime_load_vector_equivalences": 608,
        "runtime_value_byte_equivalences": 3384,
        "runtime_complete_loads": 1692,
        "runtime_uninitialized_or_partial_loads": 5252,
        "sealed_last_write_cases": 1020,
        "mutations_rejected": 18,
        "claim_boundary": (
            "Finite two-level affine constant-store reference equivalence only; "
            "not general polyhedral analysis, LLVM construction latency, "
            "coverage, solver speed, defect yield, or end-to-end speedup"
        ),
    }


def test_reference_benchmark_cardinality_boundary() -> None:
    fixture = generate()
    assert "%outer_term = mul nuw i64 %outer_iv, 8" in fixture
    assert "%affine_sum = add nuw i64 %outer_term, %inner_term" in fixture
    assert "store i16 4660" in fixture
    result = benchmark(repeats=3, iterations=1)
    assert result["all_certificates_valid"] is True
    assert result["analytic_certificate_cardinality"] == {
        "writer_value_records": 2,
        "writer_instance_records": 24,
        "fixed_point_pairs": 12,
        "byte_lane_witnesses": 46,
        "last_write_cases": 69,
        "maximum_cases_per_lane": 2,
    }
    assert result["median_validation_ns_per_certificate"] > 0
    assert result["median_selection_ns_per_query"] > 0
