# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_nested_loop_memoryphi_piecewise_affine_value import (  # noqa: E402
    benchmark,
)
from check_nested_loop_memoryphi_piecewise_affine_value_oracles import (  # noqa: E402
    AffineArm,
    PiecewiseAffineValueCase,
    make_certificate,
    mathematically_supported,
    mutation_oracle,
    run_oracle,
    runtime_loads,
    summary_loads,
    validate_certificate,
)
from generate_nested_loop_memoryphi_piecewise_affine_value_fixture import (  # noqa: E402
    PREDICATES,
    generate,
)


def _case(
    *,
    predicate: str = "ult",
    induction: str = "inner",
    constant_on_left: bool = False,
    endianness: str = "little",
) -> PiecewiseAffineValueCase:
    return PiecewiseAffineValueCase(
        24, 1, 2, 2, 3, 8, 1, 16,
        predicate, induction, 2, constant_on_left,
        AffineArm(17, 5, 7, 3), AffineArm(1025, 2, 11, 3),
        2, tuple(range(23)), -86, endianness,
    )


def test_v10_binds_guard_and_both_affine_arms() -> None:
    case = _case()
    certificate = make_certificate(case)
    assert validate_certificate(certificate, case)
    assert certificate["schema"].endswith("v10")
    stored = certificate["writers"][0]["stored_value"]
    assert stored["kind"] == "piecewise-affine-bitvector"
    assert stored["guard"] == {
        "predicate": "ult",
        "induction": "inner",
        "constant_on_left": False,
        "constant": 2,
        "bits": 16,
    }
    assert stored["when_true"]["constant"] == 17
    assert stored["when_false"]["constant"] == 1025
    assert stored["when_true"]["input"] == stored["when_false"]["input"]


def test_all_guards_orders_and_endianness_match_concrete_runtime() -> None:
    for predicate in sorted(PREDICATES):
        for induction in ("outer", "inner"):
            for constant_on_left in (False, True):
                for endianness in ("little", "big"):
                    case = _case(
                        predicate=predicate,
                        induction=induction,
                        constant_on_left=constant_on_left,
                        endianness=endianness,
                    )
                    certificate = make_certificate(case)
                    for outer_count in range(4):
                        for inner_count in range(8):
                            for input_value in (0, 0x7FFF, 0xFFFF):
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


def test_witness_specializes_guard_before_affine_byte_extraction() -> None:
    certificate = make_certificate(_case())
    expressions = [
        item["value_byte_expression"]
        for witness in certificate["witnesses"]
        for item in witness["last_write_cases"]
        if item["value_byte_expression"]["kind"] == "guard-specialized-byte"
    ]
    assert {item["selected_arm"] for item in expressions} == {"true", "false"}
    assert all(
        item["guard_result"] == (item["selected_arm"] == "true")
        and item["value"]["kind"] == "extract-affine-bitvector-byte"
        for item in expressions
    )


def test_endianness_only_reverses_byte_extraction_lane() -> None:
    little = make_certificate(_case(endianness="little"))
    big = make_certificate(_case(endianness="big"))
    little_expression = next(
        item["value_byte_expression"]
        for item in little["witnesses"][1]["last_write_cases"]
        if item["value_byte_expression"]["kind"] == "guard-specialized-byte"
    )
    big_expression = next(
        item["value_byte_expression"]
        for item in big["witnesses"][1]["last_write_cases"]
        if item["value_byte_expression"]["kind"] == "guard-specialized-byte"
    )
    assert little_expression["value"]["low_bit"] == 8
    assert big_expression["value"]["low_bit"] == 0


def test_mutations_and_unsupported_boundaries_fail_closed() -> None:
    assert mutation_oracle() == 22
    signed_guard = _case(predicate="slt")
    identical = PiecewiseAffineValueCase(
        24, 1, 2, 2, 3, 8, 1, 16, "ult", "inner", 2, False,
        AffineArm(17, 5, 7, 3), AffineArm(17, 5, 7, 3),
        2, tuple(range(23)), -86, "little",
    )
    modulo_identical = PiecewiseAffineValueCase(
        1, 1, 1, 1, 1, 1, 1, 8, "ult", "inner", 1, False,
        AffineArm(0, 0, 0, 256), AffineArm(0, 0, 0, 0),
        1, (0,), None, "little",
    )
    assert not mathematically_supported(signed_guard)
    assert not mathematically_supported(identical)
    assert not mathematically_supported(modulo_identical)


def test_oracle_generator_and_reference_benchmark_close() -> None:
    assert run_oracle() == {
        "schema": "symcc-nested-loop-memoryphi-piecewise-affine-value-oracle-v1",
        "all_passed": True,
        "cases": 96,
        "accepted_piecewise_affine_summaries": 48,
        "rejected_unsupported": 48,
        "predicate_order_endian_configurations": 48,
        "runtime_load_vector_equivalences": 6144,
        "runtime_value_byte_equivalences": 58752,
        "runtime_complete_loads": 29376,
        "runtime_uninitialized_or_partial_loads": 111936,
        "mutations_rejected": 22,
        "claim_boundary": (
            "Finite two-level i16 guard-specialized piecewise-affine reference "
            "equivalence only; not general path merging, LLVM construction "
            "latency, coverage, solver time, defect yield, or speedup"
        ),
    }
    for predicate in sorted(PREDICATES):
        fixture = generate(guard_predicate=predicate)
        assert f"icmp {predicate} i16 %inner_iv, 2" in fixture
        assert "%stored = select i1 %piecewise_guard" in fixture
    reversed_fixture = generate(
        guard_predicate="uge", guard_induction="outer",
        constant_on_left=True, endianness="big",
    )
    assert "target datalayout = \"E-p:64:64\"" in reversed_fixture
    assert "icmp uge i16 2, %outer_iv" in reversed_fixture
    result = benchmark(repeats=3, iterations=1)
    assert result["all_certificates_valid"] is True
    assert result["analytic_certificate_cardinality"] == {
        "writer_value_records": 2,
        "writer_instance_records": 24,
        "byte_lane_witnesses": 46,
        "last_write_cases": 69,
        "guard_specialized_byte_expressions": 46,
        "true_arm_expressions": 11,
        "false_arm_expressions": 35,
        "maximum_cases_per_lane": 2,
    }
    assert result["median_validation_ns_per_certificate"] > 0
    assert result["median_selection_ns_per_query"] > 0
