#!/usr/bin/env python3
# RUN: %python %s --help >/dev/null
"""Run exact-formula baseline/pairing MPI mechanism ablations for F437."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))
sys.path.insert(0, str(ROOT / "benchmark"))

from check_qfbv_realtime_multirank_oracles import verify_trial  # noqa: E402
from qfbv_multirank_evaluation import (  # noqa: E402
    MultirankEvaluationError,
    content_digest,
)


SCHEMA = "symcc-f437-utility-pairing-ablation-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="ascii") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _formula_identities(raw: Mapping[str, Any]) -> tuple[str, ...]:
    reports = raw.get("rank_reports")
    if not isinstance(reports, list) or not reports:
        raise MultirankEvaluationError("F437 rank reports are missing")
    coordinator = reports[0]
    rounds = coordinator.get("rounds")
    if not isinstance(rounds, list):
        raise MultirankEvaluationError("F437 coordinator rounds are missing")
    return tuple(str(row.get("formula_sha256", "")) for row in rounds)


def verify_pair(
    baseline: Mapping[str, Any],
    pairing: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify one exact-input mechanism pair without claiming solve speedup."""
    baseline_row = verify_trial(baseline, mode="active")
    pairing_row = verify_trial(pairing, mode="active")
    if baseline_row["utility_pairing_enabled"]:
        raise MultirankEvaluationError("F437 baseline unexpectedly enabled pairing")
    if not pairing_row["utility_pairing_enabled"]:
        raise MultirankEvaluationError("F437 treatment did not enable pairing")
    if baseline.get("library_sha256") != pairing.get("library_sha256"):
        raise MultirankEvaluationError("F437 native library differs across pair")
    baseline_config = baseline.get("config")
    pairing_config = pairing.get("config")
    if not isinstance(baseline_config, Mapping) or not isinstance(
        pairing_config, Mapping
    ):
        raise MultirankEvaluationError("F437 paired configuration is missing")
    ignored = {"utility_pairing"}
    if {
        key: value for key, value in baseline_config.items() if key not in ignored
    } != {
        key: value for key, value in pairing_config.items() if key not in ignored
    }:
        raise MultirankEvaluationError("F437 paired configuration changed")
    formulas = _formula_identities(baseline)
    if not formulas or formulas != _formula_identities(pairing):
        raise MultirankEvaluationError("F437 paired formulas changed")

    opportunities = int(pairing.get("utility_pairing_opportunities", -1))
    admitted = int(pairing.get("utility_pairing_admitted", -1))
    suppressed = int(pairing.get("utility_pairing_suppressed", -1))
    baseline_delivered = int(baseline.get("delivered_imports", -1))
    pairing_delivered = int(pairing.get("delivered_imports", -1))
    outcomes = pairing.get("utility_pairing_outcomes")
    if (
        not isinstance(outcomes, Mapping)
        or opportunities != baseline_delivered
        or admitted + suppressed != opportunities
        or suppressed <= 0
        or pairing_delivered != (
            int(outcomes.get("unit", -1))
            + int(outcomes.get("conflict", -1))
            + int(outcomes.get("unactivated", -1))
        )
    ):
        raise MultirankEvaluationError(
            "F437 paired opportunity/outcome conservation changed"
        )
    baseline_activated = int(baseline.get("clause_activity_unit", -1)) + int(
        baseline.get("clause_activity_conflict", -1)
    )
    pairing_activated = int(pairing.get("clause_activity_unit", -1)) + int(
        pairing.get("clause_activity_conflict", -1)
    )
    if pairing_activated != baseline_activated:
        raise MultirankEvaluationError(
            "F437 treatment did not retain the baseline activation opportunities"
        )
    return {
        "baseline_artifact_sha256": baseline["artifact_sha256"],
        "pairing_artifact_sha256": pairing["artifact_sha256"],
        "formula_sha256": list(formulas),
        "opportunities": opportunities,
        "admitted": admitted,
        "suppressed": suppressed,
        "baseline_delivered": baseline_delivered,
        "pairing_delivered": pairing_delivered,
        "baseline_activated": baseline_activated,
        "pairing_activated": pairing_activated,
        "baseline_unactivated": int(
            baseline.get("clause_activity_unactivated", -1)
        ),
        "pairing_unactivated": int(
            pairing.get("clause_activity_unactivated", -1)
        ),
        "baseline_solve_total_us": int(baseline["solve"]["total_us"]),
        "pairing_solve_total_us": int(pairing["solve"]["total_us"]),
        "baseline_checker_total_us": int(
            baseline["checker_cpu"]["total_us"]
        ),
        "pairing_checker_total_us": int(
            pairing["checker_cpu"]["total_us"]
        ),
    }


def _run_trial(
    args: argparse.Namespace,
    *,
    seed: int,
    pairing: bool,
    output_dir: Path,
) -> dict[str, Any]:
    label = "pairing" if pairing else "baseline"
    result_path = output_dir / f"seed-{seed}-{label}.json"
    stdout_path = output_dir / f"seed-{seed}-{label}.stdout.txt"
    stderr_path = output_dir / f"seed-{seed}-{label}.stderr.txt"
    command = [
        args.mpiexec,
        "-n", str(args.processes),
        sys.executable, str(ROOT / "benchmark" / "run_qfbv_realtime_multirank.py"),
        "--library", str(args.library.resolve(strict=True)),
        "--proof-root", str((output_dir / "proof-runs").resolve()),
        "--output", str(result_path.resolve()),
        "--publishers", str(args.publishers),
        "--rounds", str(args.rounds),
        "--seed", str(seed),
        "--variables", str(args.variables),
        "--clauses", str(args.clauses),
        "--mode", "active",
        "--solve-timeout-ms", str(args.solve_timeout_ms),
        "--qualification-timeout", str(args.qualification_timeout),
        "--track-clause-activity",
    ]
    if pairing:
        command.append("--utility-pairing")
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=max(120, int(args.solve_timeout_ms * args.rounds / 1000) + 120),
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not result_path.is_file():
        raise RuntimeError(
            f"F437 {label} trial failed with {completed.returncode}: "
            f"{completed.stderr[-1000:]}"
        )
    raw = json.loads(result_path.read_text(encoding="ascii"))
    raw["_files"] = {
        "result": result_path.name,
        "result_sha256": _sha256(result_path),
        "stdout": stdout_path.name,
        "stdout_sha256": _sha256(stdout_path),
        "stderr": stderr_path.name,
        "stderr_sha256": _sha256(stderr_path),
    }
    return raw


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.rounds < 3 or args.publishers < 2 or args.repetitions < 1:
        raise ValueError(
            "F437 ablation requires at least two publishers, three rounds, "
            "and one repetition"
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic_ns()
    pairs = []
    for repetition in range(args.repetitions):
        seed = args.seed + repetition * 0x10001
        baseline = _run_trial(
            args, seed=seed, pairing=False, output_dir=args.output_dir
        )
        treatment = _run_trial(
            args, seed=seed, pairing=True, output_dir=args.output_dir
        )
        baseline_files = baseline.pop("_files")
        pairing_files = treatment.pop("_files")
        row = verify_pair(baseline, treatment)
        row.update({
            "seed": seed,
            "baseline_files": baseline_files,
            "pairing_files": pairing_files,
        })
        pairs.append(row)
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "pass",
        "library": str(args.library.resolve()),
        "library_sha256": _sha256(args.library.resolve(strict=True)),
        "processes": args.processes,
        "publishers": args.publishers,
        "consumers": args.processes - args.publishers - 1,
        "rounds": args.rounds,
        "variables": args.variables,
        "clauses": args.clauses,
        "repetitions": args.repetitions,
        "pairs": pairs,
        "total_opportunities": sum(row["opportunities"] for row in pairs),
        "total_suppressed": sum(row["suppressed"] for row in pairs),
        "total_baseline_activated": sum(
            row["baseline_activated"] for row in pairs
        ),
        "total_pairing_activated": sum(
            row["pairing_activated"] for row in pairs
        ),
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
        "claim_boundary": (
            "paired local MPI mechanism ablation; suppression and activation "
            "retention only, with no solve-time, fuzzing-coverage, defect-yield, "
            "or multi-node scaling claim"
        ),
    }
    body["artifact_sha256"] = content_digest(body)
    _atomic_json(args.output_dir / "ablation-summary.json", body)
    return body


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mpiexec", default="mpiexec")
    parser.add_argument("--processes", type=int, default=5)
    parser.add_argument("--publishers", type=int, default=2)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0xF437)
    parser.add_argument("--variables", type=int, default=200)
    parser.add_argument("--clauses", type=int, default=860)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--solve-timeout-ms", type=int, default=30_000)
    parser.add_argument("--qualification-timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    result = run(parse_args())
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
