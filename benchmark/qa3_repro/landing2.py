#!/usr/bin/env python3
"""Measure edge complementarity and strategy-exclusive edge contributions."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path
import sys

from qa3_common import (
    MeasurementError,
    StreamingCoverageOracle,
    coverage_status_is_eligible,
    iter_output_files,
    measure_corpus_interleaved,
    strategy_tag,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return parsed


def run(args: argparse.Namespace) -> int:
    afl_binary = args.afl_binary.resolve()
    output_directory = args.output_directory.resolve()
    seed = args.seed.resolve()
    if not afl_binary.is_file():
        raise MeasurementError(f"AFL binary does not exist: {afl_binary}")
    if not output_directory.is_dir():
        raise MeasurementError(
            f"output directory does not exist: {output_directory}"
        )
    if not seed.is_file():
        raise MeasurementError(f"seed does not exist: {seed}")

    statuses: Counter[str] = Counter()
    unstable_edges = 0
    terminal_outputs: Counter[str] = Counter()
    candidates = list(iter_output_files(output_directory))
    with StreamingCoverageOracle(
        afl_binary,
        repeats=1,
        stability_policy="strict",
        timeout_ms=args.showmap_timeout_ms,
        input_mode=args.input_mode,
    ) as oracle:
        campaign = measure_corpus_interleaved(
            oracle,
            [seed, *candidates],
            rounds=args.showmap_repeats,
            stability_policy=args.stability_policy,
            round_delay_seconds=args.replica_delay_ms / 1000.0,
        )
        baseline = campaign.measurements[seed]
        coverage_status_is_eligible(
            baseline.status,
            policy="normal-only",
            label=f"baseline {seed}",
        )
        statuses[baseline.status] += baseline.replicas
        unstable_edges += baseline.unstable_edges
        base_edges = set(baseline.edges)
        edge_unions: dict[str, set[int]] = defaultdict(set)
        output_counts: Counter[str] = Counter()

        for candidate in candidates:
            tag = strategy_tag(candidate.name, "nominal")
            measurement = campaign.measurements[candidate.resolve()]
            statuses[measurement.status] += measurement.replicas
            unstable_edges += measurement.unstable_edges
            if not coverage_status_is_eligible(
                measurement.status,
                policy=args.terminal_status_policy,
                label=str(candidate),
            ):
                terminal_outputs[f"{tag}:{measurement.status}"] += 1
                continue
            edge_unions[tag].update(set(measurement.edges) - base_edges)
            output_counts[tag] += 1
        restarts = oracle.restart_count
        oracle_mode = oracle.mode
        fallbacks = oracle.fallbacks

    tags = sorted(edge_unions)
    all_novel_edges = (
        set().union(*(edge_unions[tag] for tag in tags)) if tags else set()
    )
    print(
        f"种子边={len(base_edges)}  "
        f"全部产出合计新边={len(all_novel_edges)}"
    )
    for tag in tags:
        other_edges = (
            set().union(*(
                edge_unions[other] for other in tags if other != tag
            ))
            if len(tags) > 1
            else set()
        )
        exclusive_edges = edge_unions[tag] - other_edges
        outputs = output_counts[tag]
        print(
            f"  {tag:<16} 产出={outputs:>4}  "
            f"新边={len(edge_unions[tag]):>4}  "
            f"独有新边={len(exclusive_edges):>4}  "
            f"边/产出={len(edge_unions[tag]) / outputs:>5.2f}"
        )
    print(
        f"showmap状态: {dict(statuses)}; "
        f"重复={args.showmap_repeats}; 策略={args.stability_policy}; "
        f"轮间隔={args.replica_delay_ms}ms; "
        f"不稳定边事件={unstable_edges}; "
        f"oracle={oracle_mode}; fallback={fallbacks}; 重启={restarts}"
    )
    if terminal_outputs:
        print(f"异常终态分层(未计入coverage): {dict(terminal_outputs)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("afl_binary", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("seed", type=Path)
    parser.add_argument(
        "--showmap-timeout-ms", type=_positive_int, default=5_000
    )
    parser.add_argument("--showmap-repeats", type=_positive_int, default=3)
    parser.add_argument(
        "--replica-delay-ms", type=_nonnegative_int, default=1_000
    )
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
        print(f"qa3 landing2: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
