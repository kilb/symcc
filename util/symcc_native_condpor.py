#!/usr/bin/env python3
"""Run or verify a bounded native ConDPOR campaign."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from native_condpor_campaign import (
    run_native_condpor_campaign,
    verify_native_condpor_campaign_certificate,
)


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
        directory = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
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


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("campaign certificate must be a JSON object")
    return value


def _environment(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, content = value.partition("=")
        if not separator or not name or "\x00" in name or "\x00" in content:
            raise ValueError("--env requires a non-empty NAME=VALUE")
        if name in result:
            raise ValueError(f"duplicate --env variable: {name}")
        result[name] = content
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded model-checked ConDPOR over native re-executions."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    explore = subparsers.add_parser("explore")
    explore.add_argument("--runtime", type=Path, required=True)
    explore.add_argument("--output", type=Path, required=True)
    explore.add_argument("--cwd", type=Path)
    explore.add_argument("--memory-model", choices=("SC", "TSO", "RA"), default="SC")
    explore.add_argument("--env", action="append", default=[])
    explore.add_argument("--max-runs", type=int, default=32)
    explore.add_argument("--max-prefixes", type=int, default=256)
    explore.add_argument("--max-successors-per-run", type=int, default=64)
    explore.add_argument("--max-depth", type=int, default=64)
    explore.add_argument("--max-window", type=int, default=32)
    explore.add_argument("--max-events", type=int, default=64)
    explore.add_argument("--max-memory-events", type=int, default=32)
    explore.add_argument("--max-graph-candidates", type=int, default=4096)
    explore.add_argument("--max-trace-bytes", type=int, default=16 * 1024 * 1024)
    explore.add_argument("--timeout-seconds", type=float, default=10.0)
    explore.add_argument("--replay-wait-ms", type=int, default=1000)
    explore.add_argument("command", nargs=argparse.REMAINDER)
    verify = subparsers.add_parser("verify")
    verify.add_argument("certificate", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.operation == "verify":
            certificate = _read_object(args.certificate)
            if not verify_native_condpor_campaign_certificate(certificate):
                raise ValueError("native ConDPOR certificate verification failed")
            print(certificate["certificate_sha256"])
            return 0
        command = list(args.command)
        if command and command[0] == "--":
            command.pop(0)
        if not command:
            raise ValueError("explore requires a command after --")
        certificate = run_native_condpor_campaign(
            command,
            schedule_runtime=args.runtime,
            cwd=args.cwd,
            memory_model=args.memory_model,
            environment=_environment(args.env),
            max_runs=args.max_runs,
            max_prefixes=args.max_prefixes,
            max_successors_per_run=args.max_successors_per_run,
            max_depth=args.max_depth,
            max_window=args.max_window,
            max_events=args.max_events,
            max_memory_events=args.max_memory_events,
            max_graph_candidates=args.max_graph_candidates,
            max_trace_bytes=args.max_trace_bytes,
            timeout_seconds=args.timeout_seconds,
            replay_wait_ms=args.replay_wait_ms,
        )
        _write_atomic(args.output, certificate)
        print(certificate["certificate_sha256"])
        return 0 if certificate["status"] == "complete" else 3
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"symcc_native_condpor: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
