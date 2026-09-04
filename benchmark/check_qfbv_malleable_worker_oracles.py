#!/usr/bin/env python3
# RUN: %python %s --help >/dev/null
"""Generate and independently replay an F438 logical multi-rank trial."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_malleable_evaluation import (  # noqa: E402
    MalleableEvaluationConfig,
    build_malleable_trial,
    verify_malleable_trial,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="ascii") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world-size", type=int, default=5)
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0xF438)
    parser.add_argument("--backlog-per-slot", type=int, default=2)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.verify is not None:
        raw = json.loads(args.verify.read_text(encoding="ascii"))
        result = verify_malleable_trial(raw)
    else:
        result = build_malleable_trial(
            MalleableEvaluationConfig(
                world_size=args.world_size,
                epochs=args.epochs,
                seed=args.seed,
                jobs=args.jobs,
                backlog_per_slot=args.backlog_per_slot,
            )
        )
    if args.output is not None:
        _write_json(args.output, result)
    print(
        json.dumps(
            {
                "status": "pass",
                "artifact_sha256": result["artifact_sha256"],
                "operations": len(result["operations"]),
                "attached_leases": result["attached_leases"],
                "retired_leases": result["retired_leases"],
                "durable_proofs": result["durable_proofs"],
                "stale_fences_rejected": result["stale_fences_rejected"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
