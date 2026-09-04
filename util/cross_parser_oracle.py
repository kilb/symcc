#!/usr/bin/env python3
"""Run two parser oracles on the same candidate and bind both traces.

The primary trace remains authoritative for proposal admission.  The secondary
trace is embedded only as independently revalidated calibration evidence, so
the proposal manager can construct a candidate-paired acceptance confusion
matrix without comparing unrelated fuzzing campaigns.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


CROSS_SCHEMA = "symcc-cross-parser-telemetry-v1"
CROSS_PROOF = "paired-independent-trace-v1"
MAX_TRACE_BYTES = 1024 * 1024
MAX_COMMAND_BYTES = 4096
MAX_ARGUMENTS = 256


class ParserFailure(RuntimeError):
    """Fail-closed command, parser, trace, or encoding failure."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _content_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _command_digest(arguments: Sequence[str]) -> str:
    return hashlib.sha256(json.dumps(
        list(arguments),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")).hexdigest()


def _parse_command(command: str) -> tuple[str, ...]:
    if not isinstance(command, str) or not 1 <= len(command) <= MAX_COMMAND_BYTES:
        raise ParserFailure("parser command exceeds the size bound")
    try:
        arguments = tuple(shlex.split(command))
    except ValueError as error:
        raise ParserFailure("parser command quoting is invalid") from error
    if (
        not 1 <= len(arguments) <= MAX_ARGUMENTS
        or "{input}" not in arguments
        or "{trace}" not in arguments
        or "{cache}" in arguments
        or any(
            not argument or len(argument) > MAX_COMMAND_BYTES
            for argument in arguments
        )
    ):
        raise ParserFailure(
            "parser command needs exact {input}/{trace} arguments")
    return arguments


def _materialize_command(
    arguments: Sequence[str],
    *,
    input_path: str,
    trace_path: str,
) -> list[str]:
    return [
        input_path if argument == "{input}" else
        trace_path if argument == "{trace}" else
        argument
        for argument in arguments
    ]


def _run_parser(
    arguments: Sequence[str],
    *,
    input_path: str,
    trace_path: str,
    timeout: float,
) -> tuple[int, int]:
    started = time.monotonic_ns()
    try:
        completed = subprocess.run(
            _materialize_command(
                arguments,
                input_path=input_path,
                trace_path=trace_path,
            ),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ParserFailure("child parser failed or timed out") from error
    elapsed_us = (time.monotonic_ns() - started) // 1000
    if completed.returncode not in {0, 1}:
        raise ParserFailure("child parser returned an internal failure")
    return int(completed.returncode), max(0, elapsed_us)


def _load_trace(path: str, returncode: int) -> dict[str, Any]:
    try:
        encoded = Path(path).read_bytes()
        if not 1 <= len(encoded) <= MAX_TRACE_BYTES:
            raise ParserFailure("child trace exceeds the size bound")
        raw = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise ParserFailure("child trace is unreadable") from error
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("accepted"), bool)
        or bool(raw["accepted"]) != (returncode == 0)
        or "cross_parser_telemetry" in raw
        or "cross_parser_trace" in raw
    ):
        raise ParserFailure("child trace acceptance is inconsistent")
    return raw


def _atomic_write(path: str, value: Mapping[str, Any]) -> None:
    encoded = _canonical_bytes(value)
    if len(encoded) > MAX_TRACE_BYTES:
        raise ParserFailure("paired trace exceeds the size bound")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def compare(
    *,
    input_path: str,
    trace_path: str,
    primary_command: str,
    secondary_command: str,
    timeout: float,
) -> bool:
    """Run both parser commands concurrently and emit the primary envelope."""
    primary_arguments = _parse_command(primary_command)
    secondary_arguments = _parse_command(secondary_command)
    if primary_arguments == secondary_arguments:
        raise ParserFailure("paired parser commands must be independent")
    timeout = max(0.01, min(float(timeout), 60.0))
    try:
        candidate = Path(input_path).read_bytes()
    except OSError as error:
        raise ParserFailure("candidate input is unreadable") from error
    with tempfile.TemporaryDirectory(prefix="symcc-cross-parser-") as tmp:
        primary_path = os.path.join(tmp, "primary.json")
        secondary_path = os.path.join(tmp, "secondary.json")
        with ThreadPoolExecutor(max_workers=2) as executor:
            primary_future = executor.submit(
                _run_parser,
                primary_arguments,
                input_path=input_path,
                trace_path=primary_path,
                timeout=timeout,
            )
            secondary_future = executor.submit(
                _run_parser,
                secondary_arguments,
                input_path=input_path,
                trace_path=secondary_path,
                timeout=timeout,
            )
            primary_returncode, primary_elapsed_us = primary_future.result()
            secondary_returncode, secondary_elapsed_us = (
                secondary_future.result())
        primary = _load_trace(primary_path, primary_returncode)
        secondary = _load_trace(secondary_path, secondary_returncode)

    primary_core_sha256 = _content_digest(primary)
    secondary_sha256 = _content_digest(secondary)
    primary["cross_parser_trace"] = secondary
    primary["cross_parser_telemetry"] = {
        "schema": CROSS_SCHEMA,
        "proof": CROSS_PROOF,
        "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
        "primary_command_sha256": _command_digest(primary_arguments),
        "secondary_command_sha256": _command_digest(secondary_arguments),
        "primary_trace_sha256": primary_core_sha256,
        "secondary_trace_sha256": secondary_sha256,
        "primary_parser": str(primary.get("parser", "")),
        "secondary_parser": str(secondary.get("parser", "")),
        "primary_returncode": primary_returncode,
        "secondary_returncode": secondary_returncode,
        "primary_accepted": bool(primary["accepted"]),
        "secondary_accepted": bool(secondary["accepted"]),
        "primary_elapsed_us": primary_elapsed_us,
        "secondary_elapsed_us": secondary_elapsed_us,
    }
    _atomic_write(trace_path, primary)
    return bool(primary["accepted"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--primary-command", required=True)
    parser.add_argument("--secondary-command", required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        accepted = compare(
            input_path=args.input,
            trace_path=args.trace,
            primary_command=args.primary_command,
            secondary_command=args.secondary_command,
            timeout=args.timeout,
        )
        return 0 if accepted else 1
    except (ParserFailure, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
