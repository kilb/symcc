# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_nested_loop_memoryphi_value_summary import benchmark  # noqa: E402
from check_nested_loop_memoryphi_summary_oracles import (  # noqa: E402
    NestedSummaryCase,
)
from check_nested_loop_memoryphi_value_summary_oracles import (  # noqa: E402
    ValueSummaryCase,
    make_certificate,
    mutation_oracle,
    run_oracle,
    runtime_loads,
    summary_loads,
    validate_certificate,
)
from generate_nested_loop_memoryphi_summary_fixture import generate  # noqa: E402


def _case(endianness: str = "little") -> ValueSummaryCase:
    initialization = NestedSummaryCase(
        8, 0, 1, 0, 2, 8, 64, 2, tuple(range(7)), (1, 2)
    )
    return ValueSummaryCase(initialization, (-86, 4660), endianness)


def test_v7_binds_constant_operands_endian_bytes_and_selection_order() -> None:
    little = make_certificate(_case("little"))
    big = make_certificate(_case("big"))
    assert validate_certificate(little, _case("little"))
    assert validate_certificate(big, _case("big"))
    assert little["schema"].endswith("v7")
    assert little["summary"]["writers"][1]["stored_value"]["bytes"] == [
        0x34,
        0x12,
    ]
    assert big["summary"]["writers"][1]["stored_value"]["bytes"] == [
        0x12,
        0x34,
    ]
    cases = little["witnesses"][0]["last_write_cases"]
    assert [(item["inner_induction_value"], item["writer"]) for item in cases] == [
        (0, 1),
        (0, 0),
    ]


def test_first_match_value_summary_matches_nested_runtime() -> None:
    for endianness in ("little", "big"):
        case = _case(endianness)
        certificate = make_certificate(case)
        for outer_count in range(4):
            for inner_count in range(11):
                assert runtime_loads(
                    case,
                    outer_count=outer_count,
                    inner_count=inner_count,
                ) == summary_loads(
                    certificate,
                    outer_count=outer_count,
                    inner_count=inner_count,
                )


def test_value_metadata_and_last_write_mutations_fail_closed() -> None:
    assert mutation_oracle() == 18


def test_finite_value_summary_oracle_closes() -> None:
    assert run_oracle() == {
        "schema": "symcc-nested-loop-memoryphi-value-summary-oracle-v1",
        "all_passed": True,
        "cases": 3456,
        "accepted_value_summaries": 496,
        "rejected_unsupported_or_partial": 2960,
        "runtime_load_vector_equivalences": 16416,
        "runtime_value_byte_equivalences": 62112,
        "runtime_complete_loads": 43212,
        "runtime_uninitialized_or_partial_loads": 57572,
        "sealed_last_write_cases": 5520,
        "mutations_rejected": 18,
        "claim_boundary": (
            "Finite constant-store reference-model value and last-write "
            "equivalence only; not symbolic-value summaries, LLVM build cost, "
            "coverage, solver speed, defect yield, or end-to-end speedup"
        ),
    }


def test_generator_and_reference_benchmark_boundary() -> None:
    fixture = generate(
        64,
        1,
        8,
        (1, 2, 4, 8),
        8,
        (17, 4660, 305419896, 72623859790382856),
    )
    assert "store i8 17" in fixture
    assert "store i64 72623859790382856" in fixture
    result = benchmark(repeats=3, iterations=1)
    assert result["all_certificates_valid"] is True
    assert result["analytic_certificate_cardinality"] == {
        "writer_value_records": 4,
        "stored_value_bytes": 15,
        "byte_lane_witnesses": 456,
        "last_write_cases": 855,
        "maximum_cases_per_lane": 4,
    }
    assert result["median_validation_ns_per_certificate"] > 0
    assert result["median_selection_ns_per_query"] > 0
