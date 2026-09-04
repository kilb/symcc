#!/usr/bin/env python3
"""Materialize SymCC string-constraint artifacts into candidate inputs."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from string_constraints import (  # noqa: E402
    SymccJsonStringBackend,
    load_string_constraints,
    materialize_string_candidates,
    string_solver_backend_from_configuration,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("constraints", type=Path, help="string JSONL artifact")
    parser.add_argument("witness", type=Path, help="base input file")
    parser.add_argument("output_dir", type=Path, help="candidate output dir")
    parser.add_argument(
        "--solver",
        help="symcc-query-solver command; defaults to SYMCC_STRING_SOLVER/PATH",
    )
    parser.add_argument(
        "--no-solver",
        action="store_true",
        help="only apply exact offset patches, skipping SMT string solving",
    )
    parser.add_argument(
        "--portfolio",
        help=(
            "JSON or file describing 1..8 symcc-json/smtlib backends; "
            "overrides --solver"
        ),
    )
    parser.add_argument("--budget", type=int, default=16)
    parser.add_argument("--query-limit", type=int, default=16)
    parser.add_argument("--timeout-ms", type=int, default=1000)
    parser.add_argument("--prefix", default="string-solver")
    return parser


def _write_candidate(output_dir: Path, prefix: str, index: int, content: bytes) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{prefix}-{index:06d}"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as stream:
        stream.write(content)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    records = load_string_constraints(args.constraints)[:max(0, args.query_limit)]
    with args.witness.open("rb") as stream:
        witness = stream.read()

    backend = None
    backend_error = ""
    if not args.no_solver:
        try:
            if args.portfolio:
                backend = string_solver_backend_from_configuration(
                    args.portfolio)
            else:
                command = shlex.split(args.solver) if args.solver else None
                backend = SymccJsonStringBackend(command)
        except (OSError, ValueError, TypeError, FileNotFoundError,
                json.JSONDecodeError) as error:
            backend_error = str(error)

    candidates, metrics = materialize_string_candidates(
        records,
        witness,
        max(0, args.budget),
        solver_backend=backend,
        solver_timeout_ms=max(1, args.timeout_ms),
    )
    written = 0
    for index, content in enumerate(candidates):
        _write_candidate(args.output_dir, args.prefix, index, content)
        written += 1
    metrics.update({
        "records_loaded": len(records),
        "written": written,
        "solver_backend": backend.name if backend is not None else "none",
    })
    if backend_error:
        metrics["solver_backend_error"] = backend_error[:512]
    print(json.dumps(metrics, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
