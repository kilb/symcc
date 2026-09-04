# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_strided_loop_memoryphi_byte_lane import benchmark  # noqa: E402
from check_strided_loop_memoryphi_byte_lane_oracles import (  # noqa: E402
    StridedInductionCase,
    make_certificate,
    mutation_oracle,
    residue_formula_accepts,
    run_oracle,
    runtime_bitmap_accepts,
    validate_certificate,
)
from generate_strided_loop_memoryphi_byte_lane_fixture import (  # noqa: E402
    generate,
)


def test_full_residue_cover_and_runtime_bitmap() -> None:
    case = StridedInductionCase(
        8, 0, 2, 1, 2, 8, 64, 2, tuple(range(7))
    )
    assert validate_certificate(make_certificate(case), case)
    assert runtime_bitmap_accepts(case, count=4, load_offset=2)
    assert residue_formula_accepts(case, count=4, load_offset=2)
    assert not runtime_bitmap_accepts(case, count=2, load_offset=2)


def test_partial_residue_cover_requires_matching_load_domain() -> None:
    covered = StridedInductionCase(
        8, 0, 2, 1, 1, 8, 64, 1, (0, 2, 4, 6)
    )
    gap = StridedInductionCase(
        8, 0, 2, 1, 1, 8, 64, 1, tuple(range(8))
    )
    assert validate_certificate(make_certificate(covered), covered)
    assert not validate_certificate(make_certificate(gap), gap)


def test_overlap_wrap_and_mutations_fail_closed() -> None:
    overlap = StridedInductionCase(
        8, 0, 1, 1, 2, 8, 64, 2, tuple(range(7))
    )
    wrap = StridedInductionCase(
        8, 0, 2, 1, 2, 8, 8, 2, tuple(range(7))
    )
    assert not validate_certificate(make_certificate(overlap), overlap)
    assert not validate_certificate(make_certificate(wrap), wrap)
    assert mutation_oracle() == 15


def test_finite_domain_oracle_closes() -> None:
    result = run_oracle()
    assert result["all_passed"] is True
    assert result["cases"] == (
        result["accepted_strided_covers"]
        + result["rejected_unsupported_or_partial"]
    )
    assert result["accepted_strided_covers"] > 1_000
    assert result["runtime_bitmap_equivalences"] > 10_000


def test_generator_and_reference_benchmark_boundary() -> None:
    fixture = generate(64, 8, 8, 8)
    assert "alloca [64 x i8]" in fixture
    assert "add nuw i64 %iv, 8" in fixture
    assert "store i64" in fixture
    result = benchmark(
        object_bytes=64, stride=8, writer_bytes=8,
        load_bytes=8, repeats=3, iterations=1,
    )
    assert result["all_certificates_valid"] is True
    assert result["analytic_certificate_cardinality"] == {
        "writer_aliases": 57,
        "reachable_writers": 8,
        "covered_writer_bytes": 64,
        "load_aliases": 57,
        "byte_lane_witnesses": 456,
        "memory_phi_incoming_edges": 2,
    }
