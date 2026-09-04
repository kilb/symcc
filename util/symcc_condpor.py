#!/usr/bin/env python3
"""Explore or verify a bounded interpreter-level ConDPOR artifact."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from condpor_interpreter import (
    explore_condpor_program,
    verify_condpor_interpreter_certificate,
)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded SC ConDPOR exploration for the closed SymCC QF_BV IR."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    explore = subparsers.add_parser("explore", help="explore a program JSON file")
    explore.add_argument("program", type=Path)
    explore.add_argument("--output", type=Path)
    explore.add_argument("--max-graphs", type=int, default=10_000)
    explore.add_argument("--max-events", type=int, default=64)
    explore.add_argument("--max-revisits", type=int, default=10_000)
    explore.add_argument("--max-solver-checks", type=int, default=100_000)
    explore.add_argument("--max-internal-steps", type=int, default=10_000)
    verify = subparsers.add_parser("verify", help="recompute and verify a certificate")
    verify.add_argument("certificate", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "explore":
            certificate = explore_condpor_program(
                _read_object(args.program),
                max_graphs=args.max_graphs,
                max_events=args.max_events,
                max_revisits=args.max_revisits,
                max_solver_checks=args.max_solver_checks,
                max_internal_steps=args.max_internal_steps,
            )
            if args.output is not None:
                _write_atomic(args.output, certificate)
            print(json.dumps(certificate, sort_keys=True, separators=(",", ":")))
            return 0 if certificate["status"] == "complete" else 3
        certificate = _read_object(args.certificate)
        valid = verify_condpor_interpreter_certificate(certificate)
        print(json.dumps({"valid": valid}, sort_keys=True, separators=(",", ":")))
        return 0 if valid else 1
    except (json.JSONDecodeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"symcc_condpor: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
