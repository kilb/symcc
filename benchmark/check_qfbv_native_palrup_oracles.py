#!/usr/bin/env python3
"""Run solver-native PalRUP production through the official SAT 2026 checker."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_palrup_native_producer import (  # noqa: E402
    NATIVE_PRODUCER_COMMIT,
    NativePalrupProducer,
)
from qfbv_palrup_pipeline import PalrupGlobalChecker  # noqa: E402
from qfbv_proof_wire import PALRUP_CHECKER_COMMIT, ProofWireError  # noqa: E402


SCHEMA = "symcc-f454-native-palrup-official-oracle-v1"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_digest(value: object) -> str:
    return _digest(
        json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
    )


def _pinned(path: Path, commit: str, label: str) -> tuple[Path, str]:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise RuntimeError(f"{label} is not a regular file")
    commit_file = resolved.parent.parent / "share" / "source-commit"
    if commit_file.read_text(encoding="ascii").strip() != commit:
        raise RuntimeError(f"{label} source commit differs from policy")
    return resolved, _digest(resolved.read_bytes())


def _checker(arguments: argparse.Namespace) -> tuple[PalrupGlobalChecker, dict[str, str]]:
    local, local_hash = _pinned(
        arguments.local_check, PALRUP_CHECKER_COMMIT, "PalRUP local checker"
    )
    redistribute, redistribute_hash = _pinned(
        arguments.redistribute, PALRUP_CHECKER_COMMIT, "PalRUP redistribute"
    )
    confirm, confirm_hash = _pinned(
        arguments.confirm, PALRUP_CHECKER_COMMIT, "PalRUP confirm"
    )
    checker = PalrupGlobalChecker(
        local,
        redistribute,
        confirm,
        local_checker_sha256=local_hash,
        redistribute_sha256=redistribute_hash,
        confirm_sha256=confirm_hash,
        timeout_ms=arguments.checker_timeout_ms,
        max_parallel=arguments.max_parallel,
        read_buffer_kib=1024,
        write_buffer_kib=1024,
        merge_buffer_kib=1024,
        queue_kib=16 * 1024,
    )
    return checker, {
        "palrup_local_check": local_hash,
        "palrup_redistribute": redistribute_hash,
        "palrup_confirm": confirm_hash,
    }


def _pigeonhole_formula(
    root: Path, *, pigeons: int = 9, holes: int = 8
) -> Path:
    def variable(pigeon: int, hole: int) -> int:
        return pigeon * holes + hole + 1

    clauses: list[tuple[int, ...]] = []
    for pigeon in range(pigeons):
        clauses.append(tuple(variable(pigeon, hole) for hole in range(holes)))
        for first in range(holes):
            for second in range(first + 1, holes):
                clauses.append(
                    (-variable(pigeon, first), -variable(pigeon, second))
                )
    for hole in range(holes):
        for first in range(pigeons):
            for second in range(first + 1, pigeons):
                clauses.append(
                    (-variable(first, hole), -variable(second, hole))
                )
    path = root / "pigeonhole-9-8.cnf"
    lines = [f"p cnf {pigeons * holes} {len(clauses)}"]
    lines.extend(" ".join(map(str, clause)) + " 0" for clause in clauses)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def _run(arguments: argparse.Namespace, artifact_root: Path) -> dict[str, object]:
    started = time.monotonic_ns()
    library, library_hash = _pinned(
        arguments.library, NATIVE_PRODUCER_COMMIT, "native PalRUP producer"
    )
    helper = arguments.helper.expanduser().resolve(strict=True)
    helper_hash = _digest(helper.read_bytes())
    checker, checker_hashes = _checker(arguments)
    producer = NativePalrupProducer(
        library,
        helper,
        library_sha256=library_hash,
        helper_sha256=helper_hash,
        timeout_ms=arguments.producer_timeout_ms,
        max_parallel=arguments.max_parallel,
        maximum_shared_clause_length=arguments.maximum_shared_clause_length,
        queue_capacity_clauses=arguments.queue_capacity_clauses,
    )
    samples: list[dict[str, object]] = []
    formula = _pigeonhole_formula(artifact_root)
    for solvers in arguments.solver_counts:
        for trial in range(arguments.rounds):
            target = artifact_root / f"proof-n{solvers}-r{trial}"
            run_started = time.monotonic_ns()
            receipt = producer.produce(formula, target, solvers, checker=checker)
            producer.validate_receipt(receipt, target, checker=checker, recheck=True)
            phases = receipt["official_checker"]["phases"]
            worker_statistics = [
                item["statistics"] for item in receipt["workers"]
            ]
            samples.append(
                {
                    "solver_count": solvers,
                    "trial": trial,
                    "receipt_sha256": receipt["receipt_sha256"],
                    "official_receipt_sha256": receipt["official_checker"]["receipt_sha256"],
                    "fragment_bytes": receipt["total_fragment_bytes"],
                    "fragment_directives": sum(
                        item["directive_count"] for item in receipt["fragments"]
                    ),
                    "empty_clauses": sum(
                        item["empty_clauses"] for item in receipt["fragments"]
                    ),
                    "conflicts": sum(
                        item["conflicts"] for item in worker_statistics
                    ),
                    "native_worker_elapsed_us": [
                        item["elapsed_us"] for item in worker_statistics
                    ],
                    "pool_elapsed_us": receipt["pool_elapsed_us"],
                    "clause_sharing_active": receipt["clause_sharing_active"],
                    "imported": sum(item["imported"] for item in worker_statistics),
                    "exported": sum(item["exported"] for item in worker_statistics),
                    "delivered": sum(item["delivered"] for item in worker_statistics),
                    "dropped": sum(item["dropped"] for item in worker_statistics),
                    "pending": sum(item["pending"] for item in worker_statistics),
                    "official_phase_elapsed_us": {
                        name: sum(item["elapsed_us"] for item in entries)
                        for name, entries in phases.items()
                        if isinstance(entries, list)
                    },
                    "unsat_witness_ranks": receipt["official_checker"]["unsat_witness_ranks"],
                    "wall_elapsed_us": (time.monotonic_ns() - run_started) // 1000,
                }
            )

    sat_target = artifact_root / "sat-must-not-publish"
    try:
        producer.produce(
            ROOT / "test" / "fixtures" / "palrup_native_sat.cnf",
            sat_target,
            max(arguments.solver_counts),
            checker=checker,
        )
    except ProofWireError as error:
        sat_negative = {
            "rejected": True,
            "diagnostic": str(error)[:256],
            "proof_root_absent": not sat_target.exists(),
        }
    else:
        raise RuntimeError("SAT formula unexpectedly published a PalRUP UNSAT root")
    if not sat_negative["proof_root_absent"]:
        raise RuntimeError("failed native PalRUP run exposed a proof root")

    summaries: dict[str, object] = {}
    for solvers in arguments.solver_counts:
        rows = [row for row in samples if row["solver_count"] == solvers]
        sharing_expected = solvers > 1
        all_sharing_observed = all(
            bool(row["clause_sharing_active"]) for row in rows
        )
        if sharing_expected and not all_sharing_observed:
            raise RuntimeError(
                f"native PalRUP sharing was inactive at {solvers} solvers"
            )
        summaries[str(solvers)] = {
            "samples": len(rows),
            "median_pool_elapsed_us": statistics.median(
                int(row["pool_elapsed_us"]) for row in rows
            ),
            "median_wall_elapsed_us": statistics.median(
                int(row["wall_elapsed_us"]) for row in rows
            ),
            "median_fragment_bytes": statistics.median(
                int(row["fragment_bytes"]) for row in rows
            ),
            "median_imported": statistics.median(
                int(row["imported"]) for row in rows
            ),
            "median_exported": statistics.median(
                int(row["exported"]) for row in rows
            ),
            "median_pending": statistics.median(
                int(row["pending"]) for row in rows
            ),
            "maximum_pending": max(int(row["pending"]) for row in rows),
            "all_fanout_conserved": all(
                int(row["delivered"]) + int(row["dropped"])
                == int(row["exported"]) * (solvers - 1)
                for row in rows
            ),
            "all_dropped_zero": all(int(row["dropped"]) == 0 for row in rows),
            "all_clause_sharing_active": all_sharing_observed,
            "all_officially_rechecked": True,
        }
    result: dict[str, object] = {
        "schema": SCHEMA,
        "status": "passed",
        "producer_source_commit": NATIVE_PRODUCER_COMMIT,
        "checker_source_commit": PALRUP_CHECKER_COMMIT,
        "producer_library_sha256": library_hash,
        "helper_sha256": helper_hash,
        "checker_tool_sha256": checker_hashes,
        "rounds": arguments.rounds,
        "solver_counts": list(arguments.solver_counts),
        "formula": {
            "name": "pigeonhole-9-8",
            "sha256": _digest(formula.read_bytes()),
            "variables": 72,
            "clauses": 549,
        },
        "maximum_shared_clause_length": arguments.maximum_shared_clause_length,
        "queue_capacity_clauses": arguments.queue_capacity_clauses,
        "samples": samples,
        "summaries": summaries,
        "sat_negative": sat_negative,
        "claim_boundary": (
            "native clause-sharing production and official global-confirmation "
            "mechanism oracle; "
            "not a SAT speedup, fuzzing coverage, or multi-node scaling claim"
        ),
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
    }
    result["result_sha256"] = _canonical_digest(result)
    return result


def main() -> int:
    producer_prefix = Path.home() / ".local" / "opt" / "cadical-palrup-sat2026"
    checker_prefix = Path.home() / ".local" / "opt" / "palrup-check-sat2026"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--library",
        type=Path,
        default=producer_prefix / "lib" / "libsymcc_qfbv_palrup_pool.so",
    )
    parser.add_argument(
        "--helper",
        type=Path,
        default=ROOT / "util" / "qfbv_palrup_native_pool_worker.py",
    )
    parser.add_argument(
        "--local-check",
        type=Path,
        default=checker_prefix / "bin" / "palrup_local_check",
    )
    parser.add_argument(
        "--redistribute",
        type=Path,
        default=checker_prefix / "bin" / "palrup_redistribute",
    )
    parser.add_argument(
        "--confirm",
        type=Path,
        default=checker_prefix / "bin" / "palrup_confirm",
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--solver-counts", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--producer-timeout-ms", type=int, default=30_000)
    parser.add_argument("--checker-timeout-ms", type=int, default=30_000)
    parser.add_argument("--max-parallel", type=int, default=4)
    parser.add_argument("--maximum-shared-clause-length", type=int, default=32)
    parser.add_argument("--queue-capacity-clauses", type=int, default=65_536)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if not 1 <= arguments.rounds <= 100:
        raise ValueError("--rounds must be in [1, 100]")
    if (
        not arguments.solver_counts
        or len(set(arguments.solver_counts)) != len(arguments.solver_counts)
        or any(not 1 <= value <= 64 for value in arguments.solver_counts)
    ):
        raise ValueError("--solver-counts must be unique values in [1, 64]")
    if arguments.max_parallel < max(arguments.solver_counts):
        raise ValueError("--max-parallel must cover every solver count")
    if not 1 <= arguments.maximum_shared_clause_length <= 1024:
        raise ValueError("--maximum-shared-clause-length must be in [1, 1024]")
    if not 1 <= arguments.queue_capacity_clauses <= 1_000_000:
        raise ValueError("--queue-capacity-clauses must be in [1, 1000000]")
    if arguments.artifact_dir is None:
        with tempfile.TemporaryDirectory(prefix="symcc-f454-oracle-") as directory:
            result = _run(arguments, Path(directory))
    else:
        arguments.artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        result = _run(arguments, arguments.artifact_dir)
    encoded = json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="ascii")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
