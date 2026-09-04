# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_nested_loop_memoryphi_affine_symbolic_value import (  # noqa: E402
    benchmark,
)
from check_nested_loop_memoryphi_affine_symbolic_value_oracles import (  # noqa: E402
    AffineSymbolicValueCase,
    make_certificate,
    mathematically_supported,
    mutation_oracle,
    run_oracle,
    runtime_loads,
    summary_loads,
    validate_certificate,
)
from generate_nested_loop_memoryphi_affine_symbolic_value_fixture import (  # noqa: E402
    generate,
)


def _case(endianness: str = "little") -> AffineSymbolicValueCase:
    return AffineSymbolicValueCase(
        12, 1, 2, 2, 2, 4, 1, 16, 17, 5, 7, 3,
        2, tuple(range(11)), -86, endianness,
    )


def test_v9_binds_input_abi_and_affine_bitvector_formula() -> None:
    certificate = make_certificate(_case())
    assert validate_certificate(certificate, _case())
    assert certificate["schema"].endswith("v9")
    stored = certificate["writers"][0]["stored_value"]
    assert stored == {
        "kind": "affine-bitvector",
        "bits": 16,
        "constant": 17,
        "outer_scale": 5,
        "inner_scale": 7,
        "input_scale": 3,
        "input": {
            "variable": {"var": "arg3"},
            "offset": 3,
            "bytes": 2,
        },
        "semantics": "modulo-2^bits",
    }


def test_modular_first_match_equals_concrete_nested_runtime() -> None:
    for endianness in ("little", "big"):
        case = _case(endianness)
        certificate = make_certificate(case)
        for outer_count in range(4):
            for inner_count in range(4):
                for input_value in (0, 1, 0x7FFF, 0xFFFF):
                    assert runtime_loads(
                        case,
                        outer_count=outer_count,
                        inner_count=inner_count,
                        input_value=input_value,
                    ) == summary_loads(
                        certificate,
                        outer_count=outer_count,
                        inner_count=inner_count,
                        input_value=input_value,
                    )


def test_endianness_changes_byte_extraction_not_value_formula() -> None:
    little = make_certificate(_case("little"))
    big = make_certificate(_case("big"))
    little_expression = little["witnesses"][1]["last_write_cases"][0][
        "value_byte_expression"
    ]
    big_expression = big["witnesses"][1]["last_write_cases"][0][
        "value_byte_expression"
    ]
    assert little_expression["low_bit"] == 8
    assert big_expression["low_bit"] == 0
    assert {
        key: value for key, value in little_expression.items()
        if key != "low_bit"
    } == {
        key: value for key, value in big_expression.items()
        if key != "low_bit"
    }


def test_symbolic_metadata_mutations_and_rejections_fail_closed() -> None:
    assert mutation_oracle() == 18
    unsupported = AffineSymbolicValueCase(
        12, 1, 2, 2, 2, 4, 1, 16, 0, 0, 0, 0,
        2, tuple(range(11)), -86, "little",
    )
    assert not mathematically_supported(unsupported)


def test_finite_affine_symbolic_value_oracle_closes() -> None:
    assert run_oracle() == {
        "schema": "symcc-nested-loop-memoryphi-affine-symbolic-value-oracle-v1",
        "all_passed": True,
        "cases": 1152,
        "accepted_affine_symbolic_summaries": 6,
        "rejected_unsupported_or_partial": 1146,
        "runtime_load_vector_equivalences": 384,
        "runtime_value_byte_equivalences": 1392,
        "runtime_complete_loads": 696,
        "runtime_uninitialized_or_partial_loads": 2504,
        "mutations_rejected": 18,
        "claim_boundary": (
            "Finite two-level i16 affine-bitvector reference equivalence only; "
            "not general recurrence solving, LLVM construction latency, "
            "coverage, solver speed, defect yield, or end-to-end speedup"
        ),
    }


def test_generator_and_reference_benchmark_cardinality() -> None:
    fixture = generate()
    assert "%value_term_0 = mul i16 %payload, 3" in fixture
    assert "%stored = add i16 %value_sum_2, 17" in fixture
    assert "store i8 -86" in fixture
    result = benchmark(repeats=3, iterations=1)
    assert result["all_certificates_valid"] is True
    assert result["analytic_certificate_cardinality"] == {
        "writer_value_records": 2,
        "writer_instance_records": 24,
        "byte_lane_witnesses": 46,
        "last_write_cases": 69,
        "symbolic_byte_expressions": 46,
        "maximum_cases_per_lane": 2,
    }
    assert result["median_validation_ns_per_certificate"] > 0
    assert result["median_selection_ns_per_query"] > 0
