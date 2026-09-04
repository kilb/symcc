# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_ordered_multilatch_memoryphi import benchmark  # noqa: E402
from check_ordered_multilatch_memoryphi_oracles import (  # noqa: E402
    OrderedWriterCase,
    make_certificate,
    mathematically_supported,
    mutation_oracle,
    run_oracle,
    runtime_state,
    transfer_replay_state,
    validate_certificate,
)
from generate_ordered_multilatch_memoryphi_fixture import (  # noqa: E402
    generate,
)


def test_writer_sequence_preserves_last_writer_provenance() -> None:
    forward = OrderedWriterCase(4, 0, 2, 8, 64, 1, tuple(range(4)), ((2, 1), ()))
    reverse = OrderedWriterCase(4, 0, 2, 8, 64, 1, tuple(range(4)), ((1, 2), ()))
    forward_state = runtime_state(forward, count=2, choices=(0,))
    reverse_state = runtime_state(reverse, count=2, choices=(0,))
    assert forward_state[0] == reverse_state[0] == {0, 1}
    assert forward_state[1][1] == (0, 0, 0)
    assert reverse_state[1][1] == (0, 1, 0)
    assert forward_state == transfer_replay_state(forward, count=2, choices=(0,))
    assert reverse_state == transfer_replay_state(reverse, count=2, choices=(0,))


def test_v5_certificate_binds_ordered_chain_and_witness_ordinal() -> None:
    case = OrderedWriterCase(8, 0, 2, 8, 64, 2, tuple(range(7)), ((1, 2), (), (2, 1)))
    certificate = make_certificate(case)
    assert validate_certificate(certificate, case)
    assert certificate["schema"].endswith("v5")
    assert certificate["fixed_point"]["semantics"] == (
        "mutually-exclusive-ordered-writer-transfer"
    )
    assert certificate["memory_phi"]["incoming"][0]["kind"] == (
        "ordered-writer-memory-def-chain"
    )
    assert [writer["ordinal"] for writer in certificate["transfers"][0]["writers"]] == [
        0,
        1,
    ]
    assert {item["writer"] for item in certificate["witnesses"][0]["alternatives"]} == {
        0,
        1,
    }


def test_unsupported_writer_counts_partial_cover_and_mutations_fail_closed() -> None:
    five_in_transfer = OrderedWriterCase(
        8, 0, 2, 8, 64, 1, tuple(range(8)), ((1,) * 5, ())
    )
    too_many_total = OrderedWriterCase(
        8, 0, 2, 8, 64, 1, tuple(range(8)), ((1,) * 4,) * 4 + ((1,),)
    )
    singleton_only = OrderedWriterCase(8, 0, 1, 8, 64, 1, tuple(range(8)), ((1,), ()))
    partial = OrderedWriterCase(8, 0, 2, 8, 64, 1, tuple(range(8)), ((1, 1), ()))
    assert not mathematically_supported(five_in_transfer)
    assert not mathematically_supported(too_many_total)
    assert not mathematically_supported(singleton_only)
    assert not mathematically_supported(partial)
    assert mutation_oracle() == 22


def test_finite_ordered_writer_oracle_closes() -> None:
    assert run_oracle() == {
        "schema": "symcc-ordered-multilatch-memoryphi-oracle-v1",
        "all_passed": True,
        "cases": 13632,
        "accepted_ordered_fixed_points": 3312,
        "rejected_unsupported_or_partial": 10320,
        "byte_lane_checks": 72576,
        "runtime_bitmap_equivalences": 231282,
        "last_writer_provenance_equivalences": 718968,
        "runtime_complete_load_accepts": 102108,
        "runtime_prefix_or_carry_rejects": 129174,
        "mutations_rejected": 22,
        "claim_boundary": (
            "Finite reference-model bitmap and last-writer equivalence only; "
            "not LLVM build cost, coverage, solver speed, or defect yield"
        ),
    }


def test_generator_and_reference_benchmark_boundary() -> None:
    fixture = generate(64, 8, 8, 8, 4)
    assert "alloca [64 x i8]" in fixture
    assert fixture.count("store i64") == 16
    assert fixture.count("br label %header") == 5
    assert "%next_d = add nuw i64 %iv, 8" in fixture
    result = benchmark(
        object_bytes=64,
        stride=8,
        writer_bytes=8,
        load_bytes=8,
        writers_per_transfer=4,
        repeats=3,
        iterations=1,
    )
    assert result["all_certificates_valid"] is True
    assert result["analytic_certificate_cardinality"] == {
        "writer_metadata_records": 8,
        "reachable_writer_instances": 64,
        "potential_writer_bytes": 416,
        "load_aliases": 57,
        "byte_lane_witnesses": 456,
        "witness_alternatives": 2964,
        "backedge_transfers": 4,
        "memory_phi_incoming_edges": 5,
        "fixed_point_rounds_including_stability": 9,
    }
