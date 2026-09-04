# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_loop_memoryphi_byte_lane import benchmark  # noqa: E402
from check_loop_memoryphi_byte_lane_oracles import (  # noqa: E402
    InductionCase,
    make_certificate,
    mutation_oracle,
    run_oracle,
    runtime_bitmap_accepts,
    validate_certificate,
)
from generate_loop_memoryphi_byte_lane_fixture import generate  # noqa: E402


def test_unit_step_induction_and_runtime_bitmap() -> None:
    case = InductionCase(8, 0, 1, 8, 2, tuple(range(7)))
    assert validate_certificate(make_certificate(case), case)
    assert runtime_bitmap_accepts(case, count=4, load_offset=2)
    assert not runtime_bitmap_accepts(case, count=3, load_offset=2)


def test_noncanonical_inductions_fail_closed() -> None:
    for seed, step in ((1, 1), (0, 2)):
        case = InductionCase(8, seed, step, 8, 1, tuple(range(8)))
        assert not validate_certificate(make_certificate(case), case)
    assert mutation_oracle() == 12


def test_finite_domain_oracle_closes() -> None:
    result = run_oracle()
    assert result["all_passed"] is True
    assert result["cases"] == (
        result["accepted_canonical_covers"]
        + result["rejected_noncanonical_or_partial"]
    )
    assert result["runtime_bitmap_equivalences"] > 10_000


def test_generator_reaches_wide_64_byte_boundary() -> None:
    fixture = generate(64, 8)
    assert "alloca [64 x i8]" in fixture
    assert "%iv = phi i64" in fixture
    assert "load i64" in fixture


def test_reference_benchmark_preserves_claim_boundary() -> None:
    result = benchmark(
        object_bytes=8, load_bytes=2, repeats=3, iterations=1
    )
    assert result["all_certificates_valid"] is True
    assert result["analytic_certificate_cardinality"] == {
        "writer_aliases": 8,
        "load_aliases": 7,
        "byte_lane_witnesses": 14,
        "memory_phi_incoming_edges": 2,
    }
