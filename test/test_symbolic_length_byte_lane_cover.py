# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_symbolic_length_byte_lane_cover import benchmark  # noqa: E402
from check_symbolic_length_byte_lane_oracles import (  # noqa: E402
    CoverCase,
    make_certificate,
    mutation_oracle,
    run_oracle,
    runtime_lane_guard,
    validate_certificate,
)
from generate_symbolic_length_byte_lane_fixture import generate  # noqa: E402


def test_half_open_lane_cover_and_runtime_guard() -> None:
    case = CoverCase(8, 1, 6, 2, (1, 2, 3, 4, 5))
    assert validate_certificate(make_certificate(case), case)
    assert runtime_lane_guard(case, load_offset=4, symbolic_length=5)
    assert not runtime_lane_guard(case, load_offset=5, symbolic_length=5)


def test_certificate_mutations_fail_closed() -> None:
    assert mutation_oracle() == 10


def test_finite_domain_oracle_closes() -> None:
    result = run_oracle()
    assert result["all_passed"] is True
    assert result["cases"] == (
        result["accepted_covers"] + result["rejected_non_covers"]
    )
    assert result["runtime_guard_equivalences"] > 100_000


def test_generator_reaches_the_64_byte_boundary() -> None:
    fixture = generate(64, 1)
    assert "alloca [64 x i8]" in fixture
    assert "call ptr @memset" in fixture
    assert "load i8" in fixture


def test_reference_benchmark_preserves_claim_boundary() -> None:
    result = benchmark(aliases=8, repeats=3, iterations=1)
    assert result["all_certificates_valid"] is True
    assert result["analytic_bounded_effect_cardinality"] == {
        "bounded_length_values": 65,
        "conditional_byte_writes": 64,
        "continuation_state_forks": 0,
    }
