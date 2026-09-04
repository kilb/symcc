#!/usr/bin/env python3
"""Mechanism oracle for checked proof-prefix QF_BV partitioning."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_artifact_lifecycle import ArtifactLifecycleRegistry  # noqa: E402
from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
)
from qfbv_incremental_sat import bitblast_qfbv_query  # noqa: E402
from qfbv_malleable_workers import (  # noqa: E402
    MalleableJobSignal,
    MalleableWorkerPolicy,
    recommend_job_slots,
)
from qfbv_proof_prefix_partition import (  # noqa: E402
    ProofPrefixPartitionPolicy,
    ProofPrefixPartitionStore,
    build_proof_prefix_partition,
    partition_job_catalog,
    verify_proof_prefix_partition,
)
from qfbv_realtime_stream import (  # noqa: E402
    make_checked_import_ack,
    make_clause_activity_receipt,
)


SCHEMA = "symcc-qfbv-proof-prefix-partition-oracle-v1"
MAX_BYTES = 1 << 30


def _plan():
    expressions = {}
    roots = []
    for offset in range(2):
        read = f"read-{offset}"
        zero = f"zero-{offset}"
        equal = f"equal-{offset}"
        expressions[read] = {
            "op": "read",
            "bits": 8,
            "children": [],
            "attrs": {"index": offset},
        }
        expressions[zero] = {
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": "00"},
        }
        expressions[equal] = {
            "op": "equal",
            "bits": 1,
            "children": [read, zero],
            "attrs": {},
        }
        roots.append(equal)
    return bitblast_qfbv_query("f448-oracle", roots, expressions)


def _evidence(plan, proof_store):
    checker = IncrementalProofChecker(proof_store)
    literals = [
        literal
        for _offset, lane in plan.input_literals
        for literal in lane
    ]
    selected = list(reversed(literals))[:12]
    evidence = []
    for sequence, literal in enumerate(selected, 1):
        offset = next(
            offset
            for offset, lane in plan.input_literals
            if literal in lane
        )
        activation = plan.assumptions[offset]
        record = make_rup_clause_record(
            plan,
            (-activation, -literal),
            dependency_assumptions=[activation],
            source_worker=f"oracle-{sequence}",
            worker_epoch=1,
            sequence=sequence,
        )
        digest, _created = proof_store.publish(record)
        authorization = checker.verify_clause_record(
            plan, proof_store.load(digest)
        )
        ack = make_checked_import_ack(
            plan,
            authorization,
            stream_id=hashlib.sha256(b"f448-oracle-stream").hexdigest(),
            token=sequence,
            event_sequence=sequence,
            solve_generation=1,
            delivery_ordinal=sequence,
            authorized_monotonic_ns=sequence,
            native_signature="symcc-qfbv-realtime-v1|cadical-3.0.1-oracle",
            checker_policy_sha256=checker.policy_sha256,
        )
        receipt = make_clause_activity_receipt(
            plan,
            authorization,
            ack,
            token=sequence,
            solve_generation=1,
            activity_ordinal=sequence,
            decision_level=sequence - 1,
            kind="unit",
            unit_literal=-literal,
            falsifying_assignments=[activation],
            native_signature="symcc-qfbv-realtime-v1|cadical-3.0.1-oracle",
        )
        evidence.append((receipt, ack))
    return checker, tuple(evidence), tuple(selected)


def _matches(cube, assignment):
    return all(
        assignment[abs(literal)] == (literal > 0)
        for literal in cube["literals"]
    )


def _percentile(values, percentile):
    ordered = sorted(values)
    index = math.ceil(percentile * len(ordered)) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def _timing(values):
    return {
        "samples": len(values),
        "median_us": round(statistics.median(values) / 1000.0, 3),
        "p95_us": round(_percentile(values, 0.95) / 1000.0, 3),
        "min_us": round(min(values) / 1000.0, 3),
        "max_us": round(max(values) / 1000.0, 3),
    }


def run(rounds, cube_counts):
    plan = _plan()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        lifecycle = ArtifactLifecycleRegistry(root / "lifecycle")
        lease = lifecycle.start_job(
            "f448-oracle", "oracle", lease_seconds=60.0
        )
        proofs = IncrementalProofStore(
            root / "proofs",
            lifecycle=lifecycle,
            lifecycle_lease=lease,
        )
        checker, evidence, expected_order = _evidence(plan, proofs)
        partitions = ProofPrefixPartitionStore(
            root / "partitions",
            lifecycle=lifecycle,
            lifecycle_lease=lease,
        )
        cases = []
        certificates = {}
        timing_counts = {count for count in cube_counts if count >= 5}
        build_times = {count: [] for count in timing_counts}
        replay_times = {count: [] for count in timing_counts}
        concurrency_count = max(cube_counts)
        for count in cube_counts:
            depth = 0 if count == 1 else math.ceil(math.log2(count))
            policy = ProofPrefixPartitionPolicy(
                cube_count=count,
                max_depth=depth,
            )
            static = build_proof_prefix_partition(plan, policy)
            guided = build_proof_prefix_partition(
                plan,
                policy,
                activity_evidence=evidence,
                checker=checker,
            )
            verified = verify_proof_prefix_partition(
                plan, guided, checker=checker
            )
            assert verified == guided
            if depth:
                assert static["split_variables"][0] != expected_order[0]
                assert guided["split_variables"] == list(expected_order[:depth])
            variables = guided["split_variables"]
            assignment_count = 0
            for values in itertools.product((False, True), repeat=len(variables)):
                assignment = dict(zip(variables, values))
                assert sum(
                    _matches(cube, assignment) for cube in guided["cubes"]
                ) == 1
                assignment_count += 1
            if count == concurrency_count:
                digest = guided["partition_sha256"]
            else:
                digest, _created = partitions.publish(
                    plan, guided, checker=checker
                )
            certificates[count] = guided
            cases.append(
                {
                    "cube_count": count,
                    "depth": depth,
                    "assignments_enumerated": assignment_count,
                    "partition_sha256": digest,
                    "static_first_variable": (
                        static["split_variables"][0] if depth else 0
                    ),
                    "guided_first_variable": (
                        guided["split_variables"][0] if depth else 0
                    ),
                    "exhaustive": True,
                    "pairwise_disjoint": True,
                }
            )
        for _round in range(rounds):
            for count in sorted(timing_counts):
                depth = math.ceil(math.log2(count))
                policy = ProofPrefixPartitionPolicy(
                    cube_count=count,
                    max_depth=depth,
                )
                started = time.perf_counter_ns()
                candidate = build_proof_prefix_partition(
                    plan,
                    policy,
                    activity_evidence=evidence,
                    checker=checker,
                )
                build_times[count].append(time.perf_counter_ns() - started)
                assert candidate == certificates[count]
                started = time.perf_counter_ns()
                verify_proof_prefix_partition(
                    plan, candidate, checker=checker
                )
                replay_times[count].append(time.perf_counter_ns() - started)
        concurrency_certificate = certificates[concurrency_count]
        with ThreadPoolExecutor(max_workers=8) as executor:
            convergence = list(
                executor.map(
                    lambda _index: partitions.publish(
                        plan, concurrency_certificate, checker=checker
                    ),
                    range(32),
                )
            )
        assert len({digest for digest, _created in convergence}) == 1
        assert sum(created for _digest, created in convergence) == 1
        catalog_count = min((count for count in cube_counts if count >= 2), default=1)
        catalog = partition_job_catalog(
            plan,
            certificates[catalog_count],
            checker=checker,
            backlog_per_cube=3,
        )
        signals = [
            MalleableJobSignal(
                job_id=job_id,
                formula_family_sha256=scope[0],
                backlog=scope[1],
            )
            for job_id, scope in catalog.items()
        ]
        slots = max(1, 2 * len(signals))
        allocation = recommend_job_slots(
            MalleableWorkerPolicy(total_slots=slots), signals
        )
        assert sum(allocation.values()) == slots
        before_gc = lifecycle.stats()
        assert lifecycle.release_job(lease, now=time.time() + 0.01)
        deletion_order = []

        def remove(kind, digest, size):
            deletion_order.append(kind)
            if kind == "partition":
                return partitions.delete_lifecycle_artifact(kind, digest, size)
            return proofs.delete_lifecycle_artifact(kind, digest, size)

        collected = lifecycle.collect(
            remove,
            grace_seconds=0,
            max_objects=4096,
            max_bytes=MAX_BYTES,
            time_budget_ms=30_000,
            now=time.time() + 1.0,
        )
        partition_objects = len(cube_counts)
        assert len(collected.deleted) == partition_objects + len(evidence)
        first_proof = deletion_order.index("sat-proof")
        assert all(
            kind == "partition" for kind in deletion_order[:first_proof]
        )
        assert lifecycle.stats()["artifacts"] == 0
        return {
            "schema": SCHEMA,
            "status": "pass",
            "rounds": rounds,
            "query_id": plan.query_id,
            "formula_sha256": plan.formula_sha256,
            "activity_receipts": len(evidence),
            "expected_guided_prefix": list(expected_order),
            "cases": cases,
            "timing": {
                str(count): {
                    "build_and_internal_replay": _timing(build_times[count]),
                    "independent_replay": _timing(replay_times[count]),
                }
                for count in sorted(timing_counts)
            },
            "concurrent_publications": {
                "attempts": len(convergence),
                "distinct_identities": len(
                    {digest for digest, _created in convergence}
                ),
                "new_objects": sum(created for _digest, created in convergence),
            },
            "malleable_allocation": {
                "jobs": len(signals),
                "slots": slots,
                "allocated_slots": sum(allocation.values()),
            },
            "lifecycle": {
                "artifacts_before_gc": before_gc["artifacts"],
                "edges_before_gc": before_gc["edges"],
                "deleted": len(collected.deleted),
                "dependent_first": True,
            },
        }


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument(
        "--cube-counts", default="1,3,5,8,32,128,512"
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if not 1 <= args.rounds <= 1000:
        raise ValueError("--rounds must be in [1, 1000]")
    try:
        cube_counts = tuple(
            sorted({int(item) for item in args.cube_counts.split(",")})
        )
    except ValueError as error:
        raise ValueError("--cube-counts must be comma-separated integers") from error
    if not cube_counts or any(not 1 <= count <= 4096 for count in cube_counts):
        raise ValueError("--cube-counts must be in [1, 4096]")
    payload = run(args.rounds, cube_counts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
