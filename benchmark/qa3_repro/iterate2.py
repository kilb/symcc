#!/usr/bin/env python3
"""Compare FIFO and AFL-edge-guided frontiers under the same solve budget."""

from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from qa3_common import (
    MeasurementError,
    StreamingCoverageOracle,
    coverage_status_is_eligible,
    iter_output_files,
    load_solver_counts,
    prefix_depth,
    sha256_file,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _depth(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 191:
        raise argparse.ArgumentTypeError(
            "depth must not exceed the A..0xff byte sequence (191)"
        )
    return parsed


def _execute(
    binary: Path,
    input_path: Path,
    output: Path,
    telemetry: Path,
    timeout_seconds: int,
) -> int:
    environment = os.environ.copy()
    environment.update({
        "SYMCC_OUTPUT_DIR": str(output),
        "SYMCC_INPUT_FILE": str(input_path),
        "SYMCC_TELEMETRY_OUT": str(telemetry),
    })
    try:
        completed = subprocess.run(
            [str(binary), str(input_path)],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MeasurementError(
            f"symbolic execution failed for {input_path}: {error}"
        ) from error
    return completed.returncode


def run(args: argparse.Namespace) -> int:
    symbolic_binary = args.symbolic_binary.resolve()
    afl_binary = args.afl_binary.resolve()
    if not symbolic_binary.is_file():
        raise MeasurementError(
            f"symbolic binary does not exist: {symbolic_binary}"
        )
    if args.policy == "cov" and not afl_binary.is_file():
        raise MeasurementError(f"AFL binary does not exist: {afl_binary}")

    workspace = Path(tempfile.mkdtemp(prefix="qa3-frontier-"))
    oracle: StreamingCoverageOracle | None = None
    try:
        seed = workspace / "seed"
        seed.write_bytes(b"\x00" * args.depth)
        covered: set[int] = set()
        showmap_statuses: Counter[str] = Counter()
        unstable_edges = 0
        if args.policy == "cov":
            oracle = StreamingCoverageOracle(
                afl_binary,
                repeats=args.showmap_repeats,
                stability_policy=args.stability_policy,
                timeout_ms=args.showmap_timeout_ms,
                input_mode=args.input_mode,
            )
            seed_measurement = oracle.measure(seed)
            coverage_status_is_eligible(
                seed_measurement.status,
                policy="normal-only",
                label="generated baseline",
            )
            covered.update(seed_measurement.edges)
            showmap_statuses[
                seed_measurement.status
            ] += seed_measurement.replicas
            unstable_edges += seed_measurement.unstable_edges

        seen = {sha256_file(seed)}
        frontier = [seed]
        best = 0
        executions = 0
        total_outputs = 0
        queries = 0
        solve_time_us = 0
        nonzero_executions = 0
        started = time.monotonic()

        for generation in range(1, args.generations + 1):
            next_frontier: list[Path] = []
            for input_path in frontier:
                output = Path(tempfile.mkdtemp(prefix="run-", dir=workspace))
                telemetry = Path(f"{output}.json")
                returncode = _execute(
                    symbolic_binary,
                    input_path,
                    output,
                    telemetry,
                    args.execution_timeout,
                )
                executions += 1
                if returncode != 0:
                    nonzero_executions += 1
                counts = load_solver_counts(telemetry)
                queries += counts.queries
                solve_time_us += counts.time_us
                for candidate in iter_output_files(output):
                    total_outputs += 1
                    digest = sha256_file(candidate)
                    if digest in seen:
                        continue
                    seen.add(digest)
                    retained = workspace / digest
                    shutil.copy2(candidate, retained)
                    if args.policy == "cov":
                        assert oracle is not None
                        measurement = oracle.measure(retained)
                        showmap_statuses[
                            measurement.status
                        ] += measurement.replicas
                        unstable_edges += measurement.unstable_edges
                        if not coverage_status_is_eligible(
                            measurement.status,
                            policy=args.terminal_status_policy,
                            label=str(retained),
                        ):
                            continue
                        novel = set(measurement.edges) - covered
                        if not novel:
                            continue
                        covered.update(measurement.edges)
                    next_frontier.append(retained)
                    best = max(best, prefix_depth(retained, args.depth))

            elapsed = time.monotonic() - started
            if best >= args.depth:
                print(
                    f"  [{args.policy:4}] 深度{args.depth} 第{generation}代解出 | "
                    f"执行={executions} 产出={total_outputs} "
                    f"保留={len(next_frontier)} Z3查询={queries} "
                    f"求解={solve_time_us / 1000:.0f}ms "
                    f"非零退出={nonzero_executions} "
                    f"showmap={dict(showmap_statuses)} "
                    f"不稳定边={unstable_edges} 墙钟={elapsed:.1f}s"
                )
                return 0
            frontier = next_frontier[:args.frontier_cap]
            if not frontier:
                print(
                    f"  [{args.policy:4}] 前沿枯竭 "
                    f"止步深度{best}/{args.depth} 第{generation}代 | "
                    f"执行={executions} 产出={total_outputs} Z3查询={queries} "
                    f"求解={solve_time_us / 1000:.0f}ms "
                    f"非零退出={nonzero_executions} "
                    f"showmap={dict(showmap_statuses)} "
                    f"不稳定边={unstable_edges} 墙钟={elapsed:.1f}s"
                )
                return 0

        elapsed = time.monotonic() - started
        print(
            f"  [{args.policy:4}] {args.generations}代未解出 "
            f"止步深度{best}/{args.depth} | 执行={executions} "
            f"产出={total_outputs} Z3查询={queries} "
            f"求解={solve_time_us / 1000:.0f}ms "
            f"非零退出={nonzero_executions} "
            f"showmap={dict(showmap_statuses)} "
            f"不稳定边={unstable_edges} 墙钟={elapsed:.1f}s"
        )
        return 0
    finally:
        if oracle is not None:
            oracle.close()
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbolic_binary", type=Path)
    parser.add_argument("afl_binary", type=Path)
    parser.add_argument("depth", type=_depth)
    parser.add_argument("generations", type=_positive_int)
    parser.add_argument("policy", choices=("fifo", "cov"))
    parser.add_argument("frontier_cap", type=_positive_int)
    parser.add_argument("--execution-timeout", type=_positive_int, default=300)
    parser.add_argument(
        "--showmap-timeout-ms", type=_positive_int, default=3_000
    )
    parser.add_argument("--showmap-repeats", type=_positive_int, default=3)
    parser.add_argument(
        "--stability-policy",
        choices=("strict", "intersection", "union"),
        default="strict",
    )
    parser.add_argument(
        "--input-mode", choices=("auto", "file", "stdin"), default="auto"
    )
    parser.add_argument(
        "--terminal-status-policy",
        choices=("normal-only", "stratified"),
        default="normal-only",
    )
    args = parser.parse_args()
    try:
        return run(args)
    except MeasurementError as error:
        print(f"qa3 iterate2: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
