#!/usr/bin/env python3
"""Executable mechanism oracle for proof-aware certified cube execution."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qf_bv_backend import normalize_qfbv_capabilities  # noqa: E402
from qfbv_incremental_proof import (  # noqa: E402
    CLAUSE_PROTOCOL,
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
    make_unsat_result_receipt,
)
from qfbv_incremental_sat import (  # noqa: E402
    bitblast_qfbv_query,
    extend_bitblast_assumptions,
)
from qfbv_partition_execution import (  # noqa: E402
    PartitionExecutionPolicy,
    PartitionExecutionStore,
    ProofAwarePartitionExecutor,
)
from qfbv_proof_prefix_partition import (  # noqa: E402
    ProofPrefixPartitionPolicy,
    build_proof_prefix_partition,
)


def _plan(query_id: str, *, contradiction: bool):
    expressions = {
        "input": {
            "op": "read",
            "bits": 8,
            "children": [],
            "attrs": {"index": 0},
        },
        "zero": {
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": "00"},
        },
        "equal": {
            "op": "equal",
            "bits": 1,
            "children": ["input", "zero"],
            "attrs": {},
        },
    }
    roots = ["equal"]
    if contradiction:
        expressions["false"] = {
            "op": "bool",
            "bits": 1,
            "children": [],
            "attrs": {"value": False},
        }
        roots.append("false")
    return bitblast_qfbv_query(query_id, roots, expressions)


def _base_result(plan, checker, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "assignments": {},
        "solver": "f449-oracle",
        "elapsed_us": 1,
        "backend_kind": "bitblast-cadical-qfbv",
        "backend_capabilities": normalize_qfbv_capabilities(
            {"incremental": True}
        ),
        "backend_model_verified": False,
        "backend_unsat_authorized": False,
        "capability_status": "supported",
        "bitblast_certificate": dict(plan.certificate),
        "backend_incremental_proof_protocol": CLAUSE_PROTOCOL,
        "backend_incremental_proof_policy_sha256": checker.policy_sha256,
        "backend_incremental_import_candidates": 0,
        "backend_incremental_imported_clauses": 0,
        "backend_incremental_import_checker_elapsed_us": 0,
        "backend_incremental_import_record_sha256": [],
    }


class _OracleBackend:
    def __init__(
        self,
        plan,
        proof_store,
        checker,
        sequence: "_Sequence",
        *,
        allow_sat: bool,
    ) -> None:
        self.plan = plan
        self.proof_store = proof_store
        self.checker = checker
        self.sequence = sequence
        self.allow_sat = allow_sat

    def solve_with_assumptions(self, _lease, literals):
        derived = extend_bitblast_assumptions(self.plan, literals)
        zero_values = {
            abs(literal): literal < 0
            for _offset, bits in self.plan.input_literals
            for literal in bits
        }
        if self.allow_sat and all(
            zero_values[abs(literal)] == (literal > 0) for literal in literals
        ):
            result = _base_result(derived, self.checker, "sat")
            result.update(
                {
                    "assignments": {
                        offset: 0 for offset, _bits in self.plan.input_literals
                    },
                    "backend_model_verified": True,
                }
            )
            return result
        sequence = self.sequence.next()
        record = make_rup_clause_record(
            derived,
            tuple(-literal for literal in derived.assumptions),
            dependency_assumptions=derived.assumptions,
            source_worker="f449-oracle-leaf",
            worker_epoch=0,
            sequence=sequence,
        )
        authorization = self.checker.verify_clause_record(derived, record)
        digest, created = self.proof_store.publish(record)
        receipt = make_unsat_result_receipt(
            derived, digest, derived.assumptions
        )
        self.checker.verify_result_receipt(derived, receipt)
        result = _base_result(derived, self.checker, "unsat")
        result.update(
            {
                "backend_unsat_authorized": True,
                "backend_incremental_proof_verified": True,
                "backend_incremental_proof_created": created,
                "backend_incremental_proof_record_sha256": digest,
                "backend_incremental_proof_steps": authorization.proof_steps,
                "backend_incremental_proof_propagations": (
                    authorization.propagation_count
                ),
                "backend_incremental_proof_checker_elapsed_us": max(
                    1, authorization.checker_elapsed_us
                ),
                "backend_incremental_result_receipt": receipt,
            }
        )
        return result


class _Sequence:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def next(self) -> int:
        with self._lock:
            result = self._value
            self._value += 1
            return result


class _Parent:
    input_hex = "00"


def _run_unsat(cubes: int, round_index: int) -> dict[str, Any]:
    plan = _plan(f"f449-unsat-{cubes}-{round_index}", contradiction=True)
    certificate = build_proof_prefix_partition(
        plan,
        ProofPrefixPartitionPolicy(
            cube_count=cubes,
            max_depth=max(1, (cubes - 1).bit_length()),
        ),
    )
    policy = PartitionExecutionPolicy(
        parallelism=min(8, cubes),
        max_attempts=2,
        cube_timeout_ms=1000,
        task_lease_ms=3000,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        execution_store = PartitionExecutionStore(root / "execution")
        sequence = _Sequence()
        executor = ProofAwarePartitionExecutor(
            execution_store,
            proof_store,
            checker,
            lambda _index: _OracleBackend(
                plan,
                proof_store,
                checker,
                sequence,
                allow_sat=False,
            ),
        )
        started = time.monotonic_ns()
        result = executor.execute(plan, certificate, _Parent(), policy)
        wall_us = (time.monotonic_ns() - started) // 1000
        if result["status"] != "unsat":
            raise RuntimeError("UNSAT partition oracle did not terminate")
        receipt = checker.verify_result_receipt(
            plan, result["backend_incremental_result_receipt"]
        )
        aggregate = checker.verify_clause_record(
            plan, proof_store.load(receipt.clause_receipt_sha256)
        )
        replay_started = time.monotonic_ns()
        replay = execution_store.result(
            plan,
            result["backend_partition_execution_sha256"],
            checker=checker,
        )
        replay_us = (time.monotonic_ns() - replay_started) // 1000
        if replay["backend_incremental_proof_record_sha256"] != (
            result["backend_incremental_proof_record_sha256"]
        ):
            raise RuntimeError("aggregate identity changed during replay")
        return {
            "wall_us": wall_us,
            "replay_us": replay_us,
            "leaf_proofs": aggregate.import_count,
            "resolution_steps": aggregate.proof_steps,
            "attempts": execution_store.stats()["attempts"],
            "proof_records": proof_store.stats()["records"],
        }


def _run_sat(cubes: int) -> dict[str, Any]:
    plan = _plan("f449-sat-oracle", contradiction=False)
    certificate = build_proof_prefix_partition(
        plan,
        ProofPrefixPartitionPolicy(
            cube_count=cubes,
            max_depth=max(1, (cubes - 1).bit_length()),
        ),
    )
    policy = PartitionExecutionPolicy(
        parallelism=min(8, cubes),
        max_attempts=2,
        cube_timeout_ms=1000,
        task_lease_ms=3000,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        execution_store = PartitionExecutionStore(root / "execution")
        sequence = _Sequence()
        executor = ProofAwarePartitionExecutor(
            execution_store,
            proof_store,
            checker,
            lambda _index: _OracleBackend(
                plan,
                proof_store,
                checker,
                sequence,
                allow_sat=True,
            ),
        )
        result = executor.execute(
            plan,
            certificate,
            _Parent(),
            policy,
            candidate_validator=lambda candidate: candidate == b"\x00",
        )
        if result["status"] != "sat":
            raise RuntimeError("SAT partition oracle did not find its witness")
        snapshot = execution_store.snapshot(
            result["backend_partition_execution_sha256"]
        )
        return {
            "winner": result["backend_partition_winner_cube_sha256"],
            "completed_cubes": result["backend_partition_completed_cubes"],
            "cancelled_cubes": snapshot["task_counts"].get("cancelled", 0),
            "assignments": result["assignments"],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--cube-counts", default="2,4,8,16,32,64")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.rounds <= 20:
        raise ValueError("--rounds must be in [1, 20]")
    cube_counts = [int(value) for value in args.cube_counts.split(",")]
    if (
        not cube_counts
        or len(set(cube_counts)) != len(cube_counts)
        or any(not 2 <= value <= 128 for value in cube_counts)
    ):
        raise ValueError("cube counts must be unique values in [2, 128]")
    cases = []
    for cubes in cube_counts:
        rounds = [_run_unsat(cubes, index) for index in range(args.rounds)]
        if any(
            row["leaf_proofs"] != cubes
            or row["resolution_steps"] != cubes - 1
            or row["attempts"] != cubes
            or row["proof_records"] != cubes + 1
            for row in rounds
        ):
            raise RuntimeError("partition proof cardinality invariant failed")
        cases.append(
            {
                "cube_count": cubes,
                "rounds": args.rounds,
                "leaf_proofs": cubes,
                "resolution_steps": cubes - 1,
                "median_wall_us": statistics.median(
                    row["wall_us"] for row in rounds
                ),
                "median_replay_us": statistics.median(
                    row["replay_us"] for row in rounds
                ),
                "samples": rounds,
            }
        )
    payload = {
        "schema": "symcc-f449-partition-execution-oracle-v1",
        "status": "pass",
        "rounds": args.rounds,
        "cases": cases,
        "sat_first_winner": _run_sat(max(cube_counts)),
        "claim": (
            "mechanism evidence only; wall times are not application speedup or "
            "coverage measurements"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="ascii",
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
