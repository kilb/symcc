#!/usr/bin/env python3
# RUN: %python %s --help >/dev/null
"""Run repeated real-CaDiCaL MPI oracles for the F434 protocol."""

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

from qfbv_multirank_evaluation import (  # noqa: E402
    RESULT_SCHEMA,
    MultirankEvaluationError,
    content_digest,
)


SCHEMA = "symcc-f434-multirank-oracle-suite-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_trial(raw: Mapping[str, Any], *, mode: str) -> dict[str, Any]:
    if raw.get("schema") != RESULT_SCHEMA or raw.get("status") != "pass":
        raise MultirankEvaluationError("F434 trial did not pass")
    body = dict(raw)
    artifact = str(body.pop("artifact_sha256", ""))
    if len(artifact) != 64 or content_digest(body) != artifact:
        raise MultirankEvaluationError("F434 trial artifact identity changed")
    config = raw.get("config")
    if not isinstance(config, Mapping) or config.get("mode") != mode:
        raise MultirankEvaluationError("F434 trial mode changed")
    expected = int(raw.get("expected_imports", -1))
    delivered = int(raw.get("delivered_imports", -2))
    replayed = int(raw.get("acks_independently_replayed", -3))
    pairing = bool(config.get("utility_pairing", False))
    if (
        expected <= 0
        or replayed != delivered
        or (not pairing and delivered != expected)
        or (pairing and not 0 <= delivered <= expected)
    ):
        raise MultirankEvaluationError(
            "F434 trial did not preserve exact delivery and replay counts"
        )
    active_rate = float(raw.get("active_delivery_rate", -1.0))
    if mode == "active" and not pairing and active_rate != 1.0:
        raise MultirankEvaluationError(
            "F434 active trial did not deliver during every solve"
        )
    config = raw["config"]
    if bool(config.get("track_clause_activity", False)):
        activities = int(raw.get("clause_activity_receipts", -1))
        unactivated = int(raw.get("clause_activity_unactivated", -1))
        replayed_activity = int(
            raw.get("activity_receipts_independently_replayed", -1)
        )
        if (
            activities < 0
            or unactivated < 0
            or activities + unactivated != delivered
            or replayed_activity != activities
        ):
            raise MultirankEvaluationError(
                "F436 activity trial did not preserve exact replay counts"
            )
    opportunities = int(raw.get("utility_pairing_opportunities", 0))
    admitted = int(raw.get("utility_pairing_admitted", 0))
    suppressed = int(raw.get("utility_pairing_suppressed", 0))
    if pairing:
        outcomes = raw.get("utility_pairing_outcomes")
        if (
            not bool(raw.get("utility_pairing_enabled"))
            or not isinstance(outcomes, Mapping)
            or opportunities < expected
            or admitted != expected
            or admitted + suppressed != opportunities
            or sum(int(value) for value in outcomes.values()) != admitted
            or sum(
                int(outcomes.get(name, -1))
                for name in ("unit", "conflict", "unactivated")
            ) != delivered
        ):
            raise MultirankEvaluationError(
                "F437 pairing trial did not preserve exact conservation"
            )
    if raw.get("scope") not in {
        "local-host-mpi-mechanism",
        "qualified-multi-host-mpi-mechanism",
    }:
        raise MultirankEvaluationError("F434 trial scope is invalid")
    return {
        "mode": mode,
        "artifact_sha256": artifact,
        "scope": raw["scope"],
        "world_size": int(config["world_size"]),
        "publishers": int(config["publishers"]),
        "consumers": len(config["consumer_ranks"]),
        "rounds": int(config["rounds"]),
        "expected_imports": expected,
        "delivered_imports": delivered,
        "active_delivery_rate": active_rate,
        "clause_activity_receipts": int(
            raw.get("clause_activity_receipts", 0)
        ),
        "utility_pairing_enabled": pairing,
        "utility_pairing_opportunities": opportunities,
        "utility_pairing_admitted": admitted,
        "utility_pairing_suppressed": suppressed,
        "publish_median_us": int(raw["publish"]["median_us"]),
        "solve_median_us": int(raw["solve"]["median_us"]),
        "epoch_median_us": int(raw["epoch_makespan"]["median_us"]),
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="ascii") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.utility_pairing and (
        not args.track_clause_activity or args.rounds < 3
    ):
        raise ValueError(
            "utility pairing requires activity tracking and at least three rounds"
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    runner = ROOT / "benchmark" / "run_qfbv_realtime_multirank.py"
    modes = ["preloaded"] + ["active"] * args.active_repetitions
    trials = []
    started = time.monotonic_ns()
    for index, mode in enumerate(modes):
        output = args.output_dir / f"trial-{index + 1:02d}-{mode}.json"
        stdout = args.output_dir / f"trial-{index + 1:02d}-{mode}.stdout.txt"
        stderr = args.output_dir / f"trial-{index + 1:02d}-{mode}.stderr.txt"
        command = [
            args.mpiexec,
            "-n", str(args.processes),
            sys.executable, str(runner),
            "--library", str(args.library.resolve(strict=True)),
            "--proof-root", str((args.output_dir / "proof-runs").resolve()),
            "--output", str(output.resolve()),
            "--publishers", str(args.publishers),
            "--rounds", str(args.rounds),
            "--seed", str(args.seed),
            "--variables", str(args.variables),
            "--clauses", str(args.clauses),
            "--mode", mode,
            "--solve-timeout-ms", str(args.solve_timeout_ms),
            "--qualification-timeout", str(args.qualification_timeout),
        ]
        if args.track_clause_activity:
            command.append("--track-clause-activity")
        if args.utility_pairing:
            command.append("--utility-pairing")
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(
                120,
                int(args.solve_timeout_ms * args.rounds / 1000) + 120,
            ),
        )
        stdout.write_text(completed.stdout, encoding="utf-8")
        stderr.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0 or not output.is_file():
            raise RuntimeError(
                f"F434 {mode} MPI trial failed with {completed.returncode}: "
                f"{completed.stderr[-1000:]}"
            )
        raw = json.loads(output.read_text(encoding="ascii"))
        row = verify_trial(raw, mode=mode)
        row.update({
            "result_file": output.name,
            "result_file_sha256": _sha256(output),
            "stdout_file": stdout.name,
            "stdout_file_sha256": _sha256(stdout),
            "stderr_file": stderr.name,
            "stderr_file_sha256": _sha256(stderr),
        })
        trials.append(row)

    active = [row for row in trials if row["mode"] == "active"]
    if len({row["artifact_sha256"] for row in active}) != len(active):
        raise MultirankEvaluationError(
            "independent F434 active trials reused an artifact identity"
        )
    if args.utility_pairing and not any(
        row["utility_pairing_suppressed"] > 0 for row in active
    ):
        raise MultirankEvaluationError(
            "F437 active trials did not exercise utility suppression"
        )
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "pass",
        "library": str(args.library.resolve()),
        "library_sha256": _sha256(args.library.resolve(strict=True)),
        "processes": args.processes,
        "publishers": args.publishers,
        "rounds": args.rounds,
        "seed": args.seed,
        "variables": args.variables,
        "clauses": args.clauses,
        "active_repetitions": args.active_repetitions,
        "track_clause_activity": args.track_clause_activity,
        "utility_pairing": args.utility_pairing,
        "trials": trials,
        "total_expected_imports": sum(row["expected_imports"] for row in trials),
        "total_delivered_imports": sum(row["delivered_imports"] for row in trials),
        "total_utility_pairing_opportunities": sum(
            row["utility_pairing_opportunities"] for row in trials
        ),
        "total_utility_pairing_suppressed": sum(
            row["utility_pairing_suppressed"] for row in trials
        ),
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
        "claim_boundary": (
            "repeated real-CaDiCaL MPI mechanism oracle; no fuzzing coverage, "
            "defect-yield, multi-node speedup, or public-benchmark claim"
        ),
    }
    body["artifact_sha256"] = content_digest(body)
    _atomic_json(args.output_dir / "oracle-summary.json", body)
    return body


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mpiexec", default="mpiexec")
    parser.add_argument("--processes", type=int, default=5)
    parser.add_argument("--publishers", type=int, default=2)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0xF434)
    parser.add_argument("--variables", type=int, default=200)
    parser.add_argument("--clauses", type=int, default=860)
    parser.add_argument("--active-repetitions", type=int, default=2)
    parser.add_argument("--solve-timeout-ms", type=int, default=30_000)
    parser.add_argument("--qualification-timeout", type=float, default=30.0)
    parser.add_argument("--track-clause-activity", action="store_true")
    parser.add_argument("--utility-pairing", action="store_true")
    return parser.parse_args()


def main() -> int:
    result = run(parse_args())
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
