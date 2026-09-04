# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_conditional_loop_memoryphi_byte_lane import (  # noqa: E402
    benchmark,
)
from check_conditional_loop_memoryphi_byte_lane_oracles import (  # noqa: E402
    ConditionalInductionCase,
    guard_carry_accepts,
    make_certificate,
    mutation_oracle,
    run_oracle,
    runtime_bitmap_accepts,
    validate_certificate,
)
from generate_conditional_loop_memoryphi_byte_lane_fixture import (  # noqa: E402
    generate,
)


def test_guard_carry_matches_runtime_bitmap() -> None:
    case = ConditionalInductionCase(
        8, 0, 2, 1, 2, 8, 64, 2, tuple(range(7)),
        "ult", 4, True,
    )
    assert validate_certificate(make_certificate(case), case)
    assert runtime_bitmap_accepts(case, count=4, load_offset=2)
    assert guard_carry_accepts(case, count=4, load_offset=2)
    assert not runtime_bitmap_accepts(case, count=2, load_offset=2)
    assert not runtime_bitmap_accepts(case, count=8, load_offset=4)


def test_false_writer_edge_and_unit_stride_are_supported() -> None:
    case = ConditionalInductionCase(
        4, 0, 1, 1, 1, 8, 64, 1, tuple(range(4)),
        "ult", 2, False,
    )
    assert validate_certificate(make_certificate(case), case)
    assert not guard_carry_accepts(case, count=4, load_offset=0)
    assert guard_carry_accepts(case, count=4, load_offset=3)


def test_partial_cover_invalid_guard_and_mutations_fail_closed() -> None:
    gap = ConditionalInductionCase(
        8, 0, 2, 1, 1, 8, 64, 1, tuple(range(8)),
        "ult", 8, True,
    )
    bad_predicate = ConditionalInductionCase(
        8, 0, 2, 1, 2, 8, 64, 2, tuple(range(7)),
        "unsupported", 8, True,
    )
    assert not validate_certificate(make_certificate(gap), gap)
    assert not validate_certificate(
        make_certificate(bad_predicate), bad_predicate
    )
    assert mutation_oracle() == 18


def test_finite_domain_oracle_closes() -> None:
    result = run_oracle()
    assert result["all_passed"] is True
    assert result["cases"] == (
        result["accepted_conditional_covers"]
        + result["rejected_unsupported_or_partial"]
    )
    assert result["accepted_conditional_covers"] > 10_000
    assert result["runtime_bitmap_equivalences"] > 100_000
    assert result["runtime_guard_true_accepts"] > 10_000
    assert result["runtime_guard_or_prefix_rejects"] > 10_000


def test_generator_and_reference_benchmark_boundary() -> None:
    fixture = generate(64, 8, 8, 8)
    assert "alloca [64 x i8]" in fixture
    assert "%should_write = icmp ult" in fixture
    assert "label %writer, label %skip" in fixture
    result = benchmark(
        object_bytes=64, stride=8, writer_bytes=8,
        load_bytes=8, repeats=3, iterations=1,
    )
    assert result["all_certificates_valid"] is True
    assert result["analytic_certificate_cardinality"] == {
        "writer_aliases": 57,
        "reachable_writers": 8,
        "potential_writer_bytes": 64,
        "load_aliases": 57,
        "byte_lane_witnesses": 456,
        "guarded_byte_lane_witnesses": 456,
        "memory_phi_nodes": 2,
        "memory_phi_incoming_edges": 4,
    }
