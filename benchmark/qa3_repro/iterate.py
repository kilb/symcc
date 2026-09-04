#!/usr/bin/env python3
"""Iteratively feed concolic outputs back until the nested target is solved."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from qa3_common import (
    MeasurementError,
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
    binary = args.binary.resolve()
    if not binary.is_file():
        raise MeasurementError(f"symbolic binary does not exist: {binary}")

    workspace = Path(tempfile.mkdtemp(prefix="qa3-nested-"))
    try:
        seed = workspace / "seed"
        seed.write_bytes(b"\x00" * args.depth)
        seed_digest = sha256_file(seed)
        corpus = {seed_digest: seed}
        frontier = [seed]
        best = prefix_depth(seed, args.depth)
        total_queries = 0
        total_time_us = 0
        total_executions = 0
        total_outputs = 0
        nonzero_executions = 0
        started = time.monotonic()

        print(
            f"  {'代':>3} {'执行':>5} {'产出':>6} {'Z3查询':>7} "
            f"{'求解ms':>8} {'最深层':>6} {'累计秒':>7}"
        )
        for generation in range(1, args.generations + 1):
            next_frontier: list[Path] = []
            generation_queries = 0
            generation_time_us = 0
            generation_outputs = 0
            for input_path in frontier:
                output = Path(tempfile.mkdtemp(prefix="run-", dir=workspace))
                telemetry = Path(f"{output}.json")
                returncode = _execute(
                    binary,
                    input_path,
                    output,
                    telemetry,
                    args.execution_timeout,
                )
                total_executions += 1
                if returncode != 0:
                    nonzero_executions += 1
                counts = load_solver_counts(telemetry)
                generation_queries += counts.queries
                generation_time_us += counts.time_us
                for candidate in iter_output_files(output):
                    generation_outputs += 1
                    digest = sha256_file(candidate)
                    if digest in corpus:
                        continue
                    retained = workspace / digest
                    shutil.copy2(candidate, retained)
                    corpus[digest] = retained
                    next_frontier.append(retained)
                    best = max(best, prefix_depth(retained, args.depth))

            total_queries += generation_queries
            total_time_us += generation_time_us
            total_outputs += generation_outputs
            elapsed = time.monotonic() - started
            print(
                f"  {generation:>3} {len(frontier):>5} "
                f"{generation_outputs:>6} {generation_queries:>7} "
                f"{generation_time_us / 1000:>8.1f} {best:>6} "
                f"{elapsed:>7.1f}"
            )
            if best >= args.depth:
                print(
                    f"  ==> 深度 {args.depth} 在第 {generation} 代解出;"
                    f"累计 执行={total_executions} 产出={total_outputs} "
                    f"Z3查询={total_queries} "
                    f"求解={total_time_us / 1000:.0f}ms "
                    f"非零退出={nonzero_executions} 墙钟={elapsed:.1f}s"
                )
                return 0
            frontier = next_frontier[:args.frontier_cap]
            if not frontier:
                print(
                    f"  ==> 前沿枯竭,止步于深度 {best}/{args.depth};"
                    f"非零退出={nonzero_executions}"
                )
                return 0

        elapsed = time.monotonic() - started
        print(
            f"  ==> {args.generations} 代未解出,止步于深度 "
            f"{best}/{args.depth};累计 执行={total_executions} "
            f"产出={total_outputs} Z3查询={total_queries} "
            f"求解={total_time_us / 1000:.0f}ms "
            f"非零退出={nonzero_executions} 墙钟={elapsed:.1f}s"
        )
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("depth", type=_depth)
    parser.add_argument("generations", type=_positive_int)
    parser.add_argument("--frontier-cap", type=_positive_int, default=64)
    parser.add_argument("--execution-timeout", type=_positive_int, default=600)
    args = parser.parse_args()
    try:
        return run(args)
    except MeasurementError as error:
        print(f"qa3 iterate: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
