#!/usr/bin/env python3
"""Equal-budget mechanism oracle for F453 online smart cubing."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))
sys.path.insert(0, str(ROOT / "benchmark"))

from check_qfbv_partition_execution_oracles import (  # noqa: E402
    _OracleBackend,
    _Parent,
    _Sequence,
)
from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
)
from qfbv_incremental_sat import bitblast_qfbv_query  # noqa: E402
from qfbv_online_cubing import (  # noqa: E402
    ONLINE_CUBING_ARMS,
    OnlineCubingPolicy,
    OnlineCubingPolicyStore,
    online_cubing_budget,
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
from qfbv_realtime_stream import (  # noqa: E402
    make_checked_import_ack,
    make_clause_activity_receipt,
)
from qfbv_utility_pairing import formula_family_sha256  # noqa: E402


SCHEMA = "symcc-f453-online-cubing-oracle-v1"


def _plan(query_id: str):
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
    expressions["false"] = {
        "op": "bool",
        "bits": 1,
        "children": [],
        "attrs": {"value": False},
    }
    roots.append("false")
    return bitblast_qfbv_query(query_id, roots, expressions)


def _checked_activity(plan, proof_store, checker, trial: int):
    literals = [
        literal
        for _offset, lane in plan.input_literals
        for literal in lane
    ]
    selected = list(reversed(literals))[:12]
    stream_id = hashlib.sha256(f"f453-stream-{trial}".encode("ascii")).hexdigest()
    evidence = []
    for ordinal, literal in enumerate(selected, 1):
        offset = next(
            index
            for index, lane in plan.input_literals
            if literal in lane
        )
        activation = plan.assumptions[offset]
        record = make_rup_clause_record(
            plan,
            (-activation, -literal),
            dependency_assumptions=[activation],
            source_worker=f"f453-prerun-{trial}",
            worker_epoch=1,
            sequence=ordinal,
        )
        digest, _created = proof_store.publish(record)
        authorization = checker.verify_clause_record(
            plan, proof_store.load(digest)
        )
        ack = make_checked_import_ack(
            plan,
            authorization,
            stream_id=stream_id,
            token=ordinal,
            event_sequence=ordinal,
            solve_generation=1,
            delivery_ordinal=ordinal,
            authorized_monotonic_ns=ordinal,
            native_signature="symcc-qfbv-realtime-v1|cadical-3.0.1-f453",
            checker_policy_sha256=checker.policy_sha256,
        )
        receipt = make_clause_activity_receipt(
            plan,
            authorization,
            ack,
            token=ordinal,
            solve_generation=1,
            activity_ordinal=ordinal,
            decision_level=ordinal - 1,
            kind="unit",
            unit_literal=-literal,
            falsifying_assignments=[activation],
            native_signature="symcc-qfbv-realtime-v1|cadical-3.0.1-f453",
        )
        evidence.append((receipt, ack))
    return tuple(evidence)


def _median(rows, field: str) -> float:
    return round(statistics.median(int(row[field]) for row in rows), 3)


def run(rounds: int, cube_candidates: tuple[int, ...]) -> dict:
    base_cube_timeout_ms = 1000
    with tempfile.TemporaryDirectory(prefix="symcc-f453-oracle-") as directory:
        root = Path(directory)
        policy = OnlineCubingPolicy(
            base_cube_count=4 if 4 in cube_candidates else cube_candidates[0],
            cube_candidates=cube_candidates,
            min_exploration_samples=rounds,
            cost_min_exploration_samples=1,
            exploration_permille=0,
            uncertainty_scale=0,
            prerun_budget_ms=250,
            base_cube_timeout_ms=base_cube_timeout_ms,
            max_cube_attempts=2,
            cost_reference_us=base_cube_timeout_ms * 1000,
        )
        online_path = root / "online.sqlite3"
        online = OnlineCubingPolicyStore(online_path, policy)
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        execution_store = PartitionExecutionStore(root / "executions")
        samples = []
        total_trials = rounds * len(ONLINE_CUBING_ARMS)
        for trial in range(total_trials):
            query_id = f"f453-equal-budget-{trial}"
            plan = _plan(query_id)
            family = formula_family_sha256(plan.certificate)
            decision = online.decide(
                query_id, family, activity_available=True
            )
            arm = str(decision["arm"])
            prerun_started = time.monotonic_ns()
            evidence = (
                _checked_activity(plan, proof_store, checker, trial)
                if arm in {"activity", "cost"}
                else ()
            )
            prerun_us = (time.monotonic_ns() - prerun_started) // 1000
            if arm == "static":
                prerun_us = 0
            build_started = time.monotonic_ns()
            certificate = build_proof_prefix_partition(
                plan,
                ProofPrefixPartitionPolicy(
                    cube_count=int(decision["cube_count"]),
                    max_depth=max(
                        1, (int(decision["cube_count"]) - 1).bit_length()
                    ),
                ),
                activity_evidence=evidence,
                checker=checker,
            )
            build_us = (time.monotonic_ns() - build_started) // 1000
            budget = online_cubing_budget(
                policy,
                cube_count=int(decision["cube_count"]),
                prerun_elapsed_us=prerun_us,
            )
            effective_timeout_ms = budget["effective_cube_timeout_ms"]
            execution_policy = PartitionExecutionPolicy(
                parallelism=min(4, int(decision["cube_count"])),
                max_attempts=2,
                cube_timeout_ms=effective_timeout_ms,
                task_lease_ms=3000,
            )
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
            execute_started = time.monotonic_ns()
            result = executor.execute(
                plan, certificate, _Parent(), execution_policy
            )
            execute_us = (time.monotonic_ns() - execute_started) // 1000
            if (
                result.get("status") != "unsat"
                or result.get("backend_partition_completed_cubes")
                != int(decision["cube_count"])
            ):
                raise RuntimeError("F453 equal-budget partition did not converge")
            total_us = prerun_us + build_us + execute_us
            outcome = online.observe(
                decision,
                status="unsat",
                partition_executed=True,
                elapsed_us=total_us,
                prerun_elapsed_us=prerun_us,
                completed_cubes=int(decision["cube_count"]),
                activity_receipts=len(evidence),
            )
            source = str(certificate["selection_source"])
            if arm == "static" and source != "static-input-fallback":
                raise RuntimeError("static arm consumed activity evidence")
            if arm != "static" and not source.startswith("checked-proof-activity"):
                raise RuntimeError("guided arm did not consume checked activity")
            samples.append(
                {
                    "trial": trial,
                    "arm": arm,
                    "cube_count": int(decision["cube_count"]),
                    "selection_reason": decision["selection_reason"],
                    "cube_selection_reason": decision["cube_selection_reason"],
                    "propensity_numerator": decision["propensity_numerator"],
                    "propensity_denominator": decision["propensity_denominator"],
                    "selection_source": source,
                    "activity_receipts": len(evidence),
                    "prerun_us": prerun_us,
                    "partition_build_us": build_us,
                    "partition_execute_us": execute_us,
                    "total_us": total_us,
                    "base_cube_timeout_ms": base_cube_timeout_ms,
                    "configured_cpu_budget_ms": budget[
                        "configured_cpu_budget_ms"
                    ],
                    "effective_cpu_budget_ms": budget[
                        "effective_cpu_budget_ms"
                    ],
                    "prerun_budget_charged_ms": budget[
                        "prerun_budget_charged_ms"
                    ],
                    "effective_cube_timeout_ms": effective_timeout_ms,
                    "decision_sha256": decision["decision_sha256"],
                    "outcome_sha256": outcome["outcome_sha256"],
                }
            )
        groups = {
            arm: [row for row in samples if row["arm"] == arm]
            for arm in ONLINE_CUBING_ARMS
        }
        if any(len(rows) != rounds for rows in groups.values()):
            raise RuntimeError("F453 arm budgets are not balanced")
        snapshot = OnlineCubingPolicyStore(online_path, policy).snapshot()
        if snapshot["decisions"] != total_trials or snapshot["outcomes"] != total_trials:
            raise RuntimeError("F453 persistent policy ledger is incomplete")
        summaries = {
            arm: {
                "samples": len(rows),
                "cube_counts": [int(row["cube_count"]) for row in rows],
                "checked_activity_receipts": sum(
                    int(row["activity_receipts"]) for row in rows
                ),
                "median_prerun_us": _median(rows, "prerun_us"),
                "median_partition_build_us": _median(
                    rows, "partition_build_us"
                ),
                "median_partition_execute_us": _median(
                    rows, "partition_execute_us"
                ),
                "median_total_us": _median(rows, "total_us"),
            }
            for arm, rows in groups.items()
        }
        payload = {
            "schema": SCHEMA,
            "status": "pass",
            "rounds_per_arm": rounds,
            "total_trials": total_trials,
            "policy": policy.as_dict(),
            "policy_sha256": policy.sha256,
            "formula_family_sha256": formula_family_sha256(
                _plan("f453-family-witness").certificate
            ),
            "budget_contract": {
                "base_cube_timeout_ms": base_cube_timeout_ms,
                "configured_cpu_budget_ms": (
                    policy.base_cube_count
                    * policy.base_cube_timeout_ms
                    * policy.max_cube_attempts
                ),
                "guided_prerun_is_deducted_from_total_cpu_budget": True,
                "cube_count_is_normalized_within_total_cpu_budget": True,
                "same_rounds_per_arm": True,
                "same_formula_family": True,
            },
            "arms": summaries,
            "snapshot": snapshot,
            "samples": samples,
            "claim": (
                "I/T/E-mechanism evidence for persistent online policy, checked "
                "activity consumption, cost feedback and equal configured budgets; "
                "synthetic contradictory QF_BV queries are not solver speedup or "
                "public-target coverage evidence"
            ),
        }
        return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--cube-candidates", default="2,4,8")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 3 <= args.rounds <= 20:
        raise ValueError("--rounds must be in [3, 20]")
    try:
        candidates = tuple(int(item) for item in args.cube_candidates.split(","))
    except ValueError as error:
        raise ValueError("--cube-candidates must contain integers") from error
    if (
        not candidates
        or tuple(sorted(set(candidates))) != candidates
        or any(not 2 <= value <= 64 for value in candidates)
    ):
        raise ValueError(
            "--cube-candidates must be canonical unique values in [2, 64]"
        )
    payload = run(args.rounds, candidates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="ascii",
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
