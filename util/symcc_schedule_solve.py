#!/usr/bin/env python3
"""Solve a schedule-SMT artifact through the system libz3 C API."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from schedule_exploration import (
    solve_schedule_smt_query,
    write_schedule_linear_extension_prefix,
)


def _load_json_row(path: Path, line: int) -> dict[str, Any]:
    rows = [
        json.loads(raw)
        for raw in path.read_text(encoding="utf-8").splitlines()
        if raw.strip()
    ]
    if not rows:
        raise ValueError("artifact file is empty")
    try:
        row = rows[line]
    except IndexError as exc:
        raise ValueError("artifact line is out of range") from exc
    if not isinstance(row, dict):
        raise ValueError("artifact row must be a JSON object")
    return row


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Solve a bounded schedule-SMT query, refine violated critical "
            "section exclusions, and emit a checked replay certificate."
        )
    )
    parser.add_argument("artifact", type=Path)
    parser.add_argument(
        "--line",
        type=int,
        default=-1,
        help="zero-based JSONL row; negative values count from the end",
    )
    query_group = parser.add_mutually_exclusive_group()
    query_group.add_argument("--query-index", type=int, default=0)
    query_group.add_argument(
        "--base-only",
        action="store_true",
        help="solve the shared base without a replay/conflict delta",
    )
    parser.add_argument(
        "--eager",
        action="store_true",
        help="use the authoritative exact base without lazy refinement",
    )
    parser.add_argument(
        "--max-refinement-rounds",
        type=int,
        default=64,
    )
    parser.add_argument(
        "--extra-smt2",
        type=Path,
        help="append research assertions before solving",
    )
    parser.add_argument("--result-out", type=Path)
    parser.add_argument("--certificate-out", type=Path)
    parser.add_argument("--prefix-out", type=Path)
    args = parser.parse_args(argv)

    try:
        artifact = _load_json_row(args.artifact, args.line)
        extra_smt2 = (
            args.extra_smt2.read_text(encoding="utf-8")
            if args.extra_smt2 is not None else ""
        )
        result = solve_schedule_smt_query(
            artifact,
            None if args.base_only else args.query_index,
            lazy_refinement=not args.eager,
            max_refinement_rounds=args.max_refinement_rounds,
            extra_smt2=extra_smt2,
        )
        certificate = result.get("certificate")
        if args.certificate_out is not None:
            if not isinstance(certificate, dict):
                raise ValueError(
                    "certificate output requires a satisfiable result"
                )
            _write_json_atomic(args.certificate_out, certificate)
        if args.prefix_out is not None:
            if not isinstance(certificate, dict):
                raise ValueError(
                    "prefix output requires a satisfiable result"
                )
            if certificate.get("runtime_replayable") is not True:
                raise ValueError(
                    "the solved query contains memory events that the "
                    "pthread replay runtime cannot control"
                )
            if not write_schedule_linear_extension_prefix(
                str(args.prefix_out), artifact, certificate
            ):
                raise OSError("failed to write replay prefix")
        if args.result_out is not None:
            _write_json_atomic(args.result_out, result)
        print(json.dumps(
            result, sort_keys=True, separators=(",", ":")
        ))
        return 3 if result["status"] == "refinement_limit" else 0
    except (OSError, RuntimeError, TypeError, ValueError,
            json.JSONDecodeError) as exc:
        print(f"symcc_schedule_solve: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
