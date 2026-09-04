#!/usr/bin/env python3
"""Independent correctness and mechanism-cost oracle for native Z3 snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qf_bv_backend import (  # noqa: E402
    parse_native_state_fork_metadata,
    parse_qfbv_response,
)


def _bounded_integer(value: str, lower: int, upper: int, name: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from error
    if not lower <= parsed <= upper:
        raise argparse.ArgumentTypeError(
            f"{name} must be in [{lower}, {upper}]"
        )
    return parsed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * percentile))
    return ordered[index]


class InteractiveHelper:
    def __init__(self, command: list[str]):
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.sequence = 0

    def request(self, script: str) -> tuple[str, int]:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("native helper pipes are unavailable")
        self.sequence += 1
        marker = f"ORACLE_{self.sequence}"
        started = time.monotonic_ns()
        self.process.stdin.write(script)
        if script and not script.endswith("\n"):
            self.process.stdin.write("\n")
        self.process.stdin.write(f'(echo "{marker}")\n')
        self.process.stdin.flush()
        rows: list[str] = []
        response_bytes = 0
        while True:
            row = self.process.stdout.readline()
            if not row:
                raise RuntimeError(
                    f"native helper exited early with {self.process.poll()}"
                )
            if row.strip() == f'"{marker}"':
                return "".join(rows), (time.monotonic_ns() - started) // 1000
            rows.append(row)
            response_bytes += len(row.encode("utf-8", errors="replace"))
            if response_bytes > 8 * 1024 * 1024:
                raise RuntimeError("native helper response exceeds 8 MiB")

    def close(self) -> None:
        if self.process.stdin is not None and self.process.poll() is None:
            self.process.stdin.write("(exit)\n")
            self.process.stdin.flush()
        self.process.wait(timeout=2)
        stderr = (
            self.process.stderr.read() if self.process.stderr is not None else ""
        )
        for stream in (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        ):
            if stream is not None and not stream.closed:
                stream.close()
        if self.process.returncode != 0 or stderr:
            raise RuntimeError(
                f"native helper failed ({self.process.returncode}): {stderr[-512:]}"
            )


def _initialization(timeout_ms: int) -> str:
    return (
        "(set-logic QF_BV)\n"
        "(set-option :produce-models true)\n"
        f"(set-option :timeout {timeout_ms})\n"
        "(declare-fun symcc_input_0 () (_ BitVec 8))\n"
        "(assert (bvuge symcc_input_0 #x20))\n"
        "(assert (bvule symcc_input_0 #xe0))\n"
    )


def _target(value: int, timeout_ms: int) -> str:
    return (
        f"(set-option :timeout {timeout_ms})\n"
        "(push 1)\n"
        f"(assert (= symcc_input_0 #x{value:02x}))\n"
        "(check-sat)\n"
        "(get-value (symcc_input_0))\n"
        "(pop 1)\n"
    )


def _cold_query(z3_command: list[str], value: int, timeout_ms: int) -> tuple[str, int]:
    model_command = (
        "(get-value (symcc_input_0))\n" if 0x20 <= value <= 0xE0 else ""
    )
    script = (
        _initialization(timeout_ms)
        + f"(assert (= symcc_input_0 #x{value:02x}))\n"
        + "(check-sat)\n"
        + model_command
        + "(exit)\n"
    )
    started = time.monotonic_ns()
    completed = subprocess.run(
        z3_command,
        input=script,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=max(1.0, timeout_ms / 1000.0 + 1.0),
        check=False,
    )
    elapsed_us = (time.monotonic_ns() - started) // 1000
    if completed.returncode != 0 or completed.stderr:
        raise RuntimeError(
            f"cold Z3 failed ({completed.returncode}): {completed.stderr[-512:]}"
        )
    offsets = (0,) if model_command else ()
    status, assignments = parse_qfbv_response(completed.stdout, offsets)
    if status == "sat" and assignments != {"0": value}:
        raise RuntimeError("cold Z3 returned an invalid model")
    return status, elapsed_us


def _timeout_isolation(helper_path: Path) -> dict[str, Any]:
    helper = InteractiveHelper(
        [str(helper_path), "--child-start-delay-ms", "100"]
    )
    try:
        helper.request(_initialization(10))
        timed_out_output, _ = helper.request(_target(0x42, 10))
        timed_out = parse_native_state_fork_metadata(timed_out_output)
        recovered_output, _ = helper.request(_target(0x43, 1000))
        recovered = parse_native_state_fork_metadata(recovered_output)
        recovered_status, recovered_model = parse_qfbv_response(
            recovered_output, (0,)
        )
    finally:
        helper.close()
    passed = (
        timed_out["backend_native_child_timed_out"]
        and timed_out["backend_native_child_status"] == "unknown"
        and recovered_status == "sat"
        and recovered_model == {"0": 0x43}
        and recovered["backend_native_snapshot_generation"]
        == timed_out["backend_native_snapshot_generation"]
        and recovered["backend_native_snapshot_queries"] == 2
    )
    return {
        "passed": passed,
        "timed_out_generation": timed_out[
            "backend_native_snapshot_generation"
        ],
        "recovered_generation": recovered[
            "backend_native_snapshot_generation"
        ],
        "recovered_query_count": recovered[
            "backend_native_snapshot_queries"
        ],
        "recovered_model": recovered_model,
    }


def run_oracle(
    helper_path: Path,
    z3_command: list[str],
    *,
    cases: int,
    seed: int,
    timeout_ms: int,
) -> dict[str, Any]:
    generator = random.Random(seed)
    values = [generator.randrange(256) for _ in range(cases)]
    native_wall_us: list[int] = []
    native_solve_us: list[int] = []
    fork_roundtrip_us: list[int] = []
    minor_faults: list[int] = []
    major_faults: list[int] = []
    max_rss_kib: list[int] = []
    cold_wall_us: list[int] = []
    child_pids: set[int] = set()
    mismatches = 0
    invalid_models = 0
    helper = InteractiveHelper([str(helper_path)])
    try:
        initialization_output, initialization_us = helper.request(
            _initialization(timeout_ms)
        )
        if initialization_output:
            raise RuntimeError("native helper emitted initialization noise")
        for index, value in enumerate(values, start=1):
            expected = "sat" if 0x20 <= value <= 0xE0 else "unsat"
            native_output, wall_us = helper.request(_target(value, timeout_ms))
            native_status, native_model = parse_qfbv_response(
                native_output, (0,)
            )
            metadata = parse_native_state_fork_metadata(native_output)
            cold_status, cold_us = _cold_query(z3_command, value, timeout_ms)
            mismatches += int(
                native_status != expected or cold_status != expected
            )
            invalid_models += int(
                native_status == "sat" and native_model != {"0": value}
            )
            if metadata["backend_native_snapshot_generation"] != 1:
                mismatches += 1
            if metadata["backend_native_snapshot_queries"] != index:
                mismatches += 1
            if metadata["backend_native_warm_checks"] != 1:
                mismatches += 1
            native_wall_us.append(wall_us)
            native_solve_us.append(metadata["backend_native_child_solve_us"])
            fork_roundtrip_us.append(
                metadata["backend_native_fork_roundtrip_us"]
            )
            minor_faults.append(metadata["backend_native_child_minor_faults"])
            major_faults.append(metadata["backend_native_child_major_faults"])
            max_rss_kib.append(metadata["backend_native_child_max_rss_kib"])
            cold_wall_us.append(cold_us)
            child_pids.add(metadata["backend_native_child_pid"])
    finally:
        helper.close()
    native_total_us = initialization_us + sum(native_wall_us)
    cold_total_us = sum(cold_wall_us)
    timeout_isolation = _timeout_isolation(helper_path)
    passed = (
        mismatches == 0
        and invalid_models == 0
        and len(child_pids) == cases
        and timeout_isolation["passed"]
    )
    z3_version = subprocess.run(
        [z3_command[0], "-version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    ).stdout.strip()[:256]
    return {
        "schema": "symcc-qfbv-native-state-fork-oracle-v1",
        "passed": passed,
        "claim_boundary": (
            "Same-host solver mechanism cost only; this is not an end-to-end "
            "fuzzing coverage or vulnerability-discovery claim."
        ),
        "configuration": {
            "cases": cases,
            "seed": seed,
            "timeout_ms": timeout_ms,
            "prefix": "0x20 <= symcc_input_0 <= 0xe0",
            "helper": str(helper_path.resolve()),
            "helper_sha256": _sha256(helper_path),
            "z3_command": z3_command,
            "z3_version": z3_version,
        },
        "correctness": {
            "status_mismatches": mismatches,
            "invalid_models": invalid_models,
            "unique_child_pids": len(child_pids),
            "timeout_isolation": timeout_isolation,
        },
        "mechanism_cost_us": {
            "native_initialization": initialization_us,
            "native_total_including_initialization": native_total_us,
            "cold_total": cold_total_us,
            "cold_over_native_ratio": (
                cold_total_us / native_total_us if native_total_us else 0.0
            ),
            "native_wall_median": int(statistics.median(native_wall_us)),
            "native_wall_p95": _percentile(native_wall_us, 0.95),
            "native_child_solve_median": int(
                statistics.median(native_solve_us)
            ),
            "fork_roundtrip_median": int(
                statistics.median(fork_roundtrip_us)
            ),
            "cold_wall_median": int(statistics.median(cold_wall_us)),
            "cold_wall_p95": _percentile(cold_wall_us, 0.95),
        },
        "copy_on_write_observations": {
            "child_minor_faults_median": int(statistics.median(minor_faults)),
            "child_major_faults_total": sum(major_faults),
            "child_max_rss_kib_median": int(statistics.median(max_rss_kib)),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_helper = ROOT / "build" / "symcc-qfbv-z3-forkserver"
    parser.add_argument("--helper", type=Path, default=default_helper)
    parser.add_argument("--z3", default=shutil.which("z3") or "z3")
    parser.add_argument(
        "--cases",
        type=lambda value: _bounded_integer(value, 1, 4096, "cases"),
        default=128,
    )
    parser.add_argument(
        "--seed",
        type=lambda value: _bounded_integer(value, 0, (1 << 63) - 1, "seed"),
        default=430,
    )
    parser.add_argument(
        "--timeout-ms",
        type=lambda value: _bounded_integer(
            value, 1, 3_600_000, "timeout-ms"
        ),
        default=2000,
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    if not arguments.helper.is_file() or not os.access(arguments.helper, os.X_OK):
        parser.error("the native helper must be an executable file")
    result = run_oracle(
        arguments.helper,
        [arguments.z3, "-in", "-smt2"],
        cases=arguments.cases,
        seed=arguments.seed,
        timeout_ms=arguments.timeout_ms,
    )
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        sys.stdout.write(payload)
    else:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(payload, encoding="ascii")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
