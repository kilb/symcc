#!/usr/bin/env python3
# RUN: %python %s --help >/dev/null
"""Mechanism benchmark for F432 native context and checked clause reuse."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
import tempfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from cadical_qfbv_backend import (  # noqa: E402
    CadicalQfbvSolver,
    PersistentCadicalQfbvSolver,
)
from qf_bv_conformance import build_operator_matrix_envelope  # noqa: E402
from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    IncrementalProofStore,
)
from query_store import QueryStore  # noqa: E402


SCHEMA = "symcc-f432-incremental-sat-benchmark-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary(samples: list[int]) -> dict[str, int]:
    ordered = sorted(samples)
    return {
        "rounds": len(samples),
        "median_us": int(statistics.median(ordered)),
        "p95_us": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)],
        "total_us": sum(ordered),
        "minimum_us": ordered[0],
        "maximum_us": ordered[-1],
    }


def _timed(backend, lease):
    started = time.monotonic_ns()
    result = dict(backend(lease))
    elapsed = (time.monotonic_ns() - started) // 1000
    if (
        result.get("status") != "sat"
        or result.get("assignments") != {0: 0x42, 1: 0x03}
        or result.get("backend_model_verified") is not True
    ):
        raise RuntimeError(f"benchmark solve failed: {result}")
    return elapsed, result


def run(
    cadical: Path,
    library: Path,
    *,
    rounds: int,
) -> dict:
    command = [
        str(cadical), "--plain", "--lrat", "--no-binary",
        "{cnf}", "{proof}",
    ]
    with tempfile.TemporaryDirectory(prefix="symcc-f432-benchmark-") as directory:
        root = Path(directory)
        store = QueryStore(root / "queries")
        store.ingest(build_operator_matrix_envelope(timeout_ms=30_000))
        lease = store.claim("benchmark")
        if lease is None:
            raise RuntimeError("benchmark query is not claimable")
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        cold_backend = CadicalQfbvSolver(
            store,
            command,
            name="cadical-cold",
            proof_store=proof_store,
            proof_checker=checker,
            capabilities={"incremental": True},
        )
        cold_samples = [_timed(cold_backend, lease)[0] for _ in range(rounds)]
        with PersistentCadicalQfbvSolver(
            store,
            library,
            command,
            name="cadical-native",
            proof_store=proof_store,
            proof_checker=checker,
            capabilities={"incremental": True},
            context_cache_entries=4,
        ) as native_backend:
            native_cold_us, first = _timed(native_backend, lease)
            warm_samples = []
            last = first
            for _ in range(rounds):
                elapsed, last = _timed(native_backend, lease)
                warm_samples.append(elapsed)
        if (
            first.get("backend_native_context_cache_hit") is not False
            or last.get("backend_native_context_cache_hit") is not True
            or int(last.get("backend_native_context_solve_count", 0))
            != rounds + 1
        ):
            raise RuntimeError("native context telemetry is inconsistent")
    cold = _summary(cold_samples)
    warm = _summary(warm_samples)
    result = {
        "schema": SCHEMA,
        "rounds": rounds,
        "formula": "qfbv-38-operator-matrix-sat",
        "cold_subprocess": cold,
        "native_first_us": native_cold_us,
        "native_warm": warm,
        "total_speedup": cold["total_us"] / max(1, warm["total_us"]),
        "median_speedup": cold["median_us"] / max(1, warm["median_us"]),
        "cadical": {
            "binary": str(cadical),
            "binary_sha256": _sha256(cadical),
            "library": str(library),
            "library_sha256": _sha256(library),
            "version": last["backend_native_signature"],
        },
        "claim_boundary": (
            "same-process mechanism benchmark; no coverage, defect, or "
            "distributed scaling claim"
        ),
    }
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    result["artifact_sha256"] = hashlib.sha256(encoded.encode("ascii")).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cadical", required=True, type=Path)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--rounds", type=int, default=64)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(
        args.cadical.resolve(strict=True),
        args.library.resolve(strict=True),
        rounds=max(4, min(args.rounds, 4096)),
    )
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="ascii")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
