# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_multilatch_loop_memoryphi import benchmark  # noqa: E402
from check_multilatch_loop_memoryphi_oracles import (  # noqa: E402
    MultiLatchCase,
    make_certificate,
    mathematically_supported,
    mutation_oracle,
    run_oracle,
    runtime_bitmap_accepts,
    transfer_equation_accepts,
    validate_certificate,
)
from generate_multilatch_loop_memoryphi_fixture import generate  # noqa: E402


def test_writer_and_carry_transfers_form_alternatives_not_composition() -> None:
    case = MultiLatchCase(
        8, 0, 2, 1, 2, 8, 64, 2, tuple(range(7)),
        (True, False, True, False),
    )
    certificate = make_certificate(case)
    assert validate_certificate(certificate, case)
    assert [item["kind"] for item in certificate["transfers"]] == [
        "writer", "carry", "writer", "carry",
    ]
    assert len(certificate["witnesses"][0]["alternatives"]) == 2
    assert certificate["fixed_point"]["rounds"][-1] == {
        "kind": "stability-check", "new_lanes": 0, "total_lanes": 8,
    }


def test_transfer_equation_matches_runtime_byte_init_bitmap() -> None:
    case = MultiLatchCase(
        8, 0, 2, 1, 2, 8, 64, 2, tuple(range(7)),
        (True, False, True),
    )
    for count in range(11):
        for offset in range(7):
            for choices in ((0,), (1,), (2,), (0, 1, 2)):
                assert runtime_bitmap_accepts(
                    case, count=count, load_offset=offset, choices=choices
                ) == transfer_equation_accepts(
                    case, count=count, load_offset=offset, choices=choices
                )


def test_partial_cover_bad_transfer_domain_and_mutations_fail_closed() -> None:
    partial = MultiLatchCase(
        8, 0, 2, 1, 1, 8, 64, 1, tuple(range(8)),
        (True, False),
    )
    no_writer = MultiLatchCase(
        8, 0, 2, 1, 2, 8, 64, 2, tuple(range(7)),
        (False, False),
    )
    too_many = MultiLatchCase(
        8, 0, 2, 1, 2, 8, 64, 2, tuple(range(7)),
        (True, False, True, False, True),
    )
    assert not mathematically_supported(partial)
    assert not validate_certificate(make_certificate(partial), partial)
    assert not mathematically_supported(no_writer)
    assert not mathematically_supported(too_many)
    assert mutation_oracle() == 18


def test_finite_multilatch_oracle_closes() -> None:
    result = run_oracle()
    assert result == {
        "schema": "symcc-multilatch-loop-memoryphi-oracle-v1",
        "all_passed": True,
        "cases": 42208,
        "accepted_fixed_points": 6880,
        "rejected_unsupported_or_partial": 35328,
        "byte_lane_checks": 256224,
        "runtime_bitmap_equivalences": 633552,
        "runtime_writer_path_accepts": 345284,
        "runtime_carry_or_prefix_rejects": 288268,
        "mutations_rejected": 18,
        "claim_boundary": (
            "Finite reference-model equivalence only; not LLVM build cost, "
            "end-to-end coverage, solver speed, or defect yield"
        ),
    }


def test_generator_and_reference_benchmark_boundary() -> None:
    fixture = generate(64, 8, 8, 8)
    assert "alloca [64 x i8]" in fixture
    assert fixture.count("br label %header") == 5
    assert "%next_d = add nuw i64 %iv, 8" in fixture
    result = benchmark(
        object_bytes=64,
        stride=8,
        writer_bytes=8,
        load_bytes=8,
        repeats=3,
        iterations=1,
    )
    assert result["all_certificates_valid"] is True
    assert result["analytic_certificate_cardinality"] == {
        "writer_aliases_per_transfer": 57,
        "reachable_writers_per_transfer": 8,
        "potential_writer_bytes": 128,
        "load_aliases": 57,
        "byte_lane_witnesses": 456,
        "witness_alternatives": 912,
        "decision_blocks": 3,
        "backedge_transfers": 4,
        "memory_phi_nodes": 1,
        "memory_phi_incoming_edges": 5,
        "fixed_point_rounds_including_stability": 9,
    }
