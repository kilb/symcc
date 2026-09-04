#!/usr/bin/env python3
"""Measure how often each solver strategy produces inputs with new edges."""

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
        print(f"种子边数: {len(base_edges)}")

        # Each value is [outputs, outputs with seed-novel edges, edge union].
        stats: dict[str, list] = defaultdict(lambda: [0, 0, set()])
        for candidate in candidates:
            tag = strategy_tag(candidate.name, "nominal(strict)")
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
            new_vs_seed = set(measurement.edges) - base_edges
            row = stats[tag]
            row[0] += 1
            if new_vs_seed:
                row[1] += 1
            row[2].update(new_vs_seed)
        restarts = oracle.restart_count
        oracle_mode = oracle.mode
        fallbacks = oracle.fallbacks

    print(
        f"{'策略':<18} {'产出':>5} {'相对种子有新边':>14} "
        f"{'落地率':>8} {'贡献新边(并集)':>14}"
    )
    for tag, row in sorted(stats.items()):
        outputs = int(row[0])
        hits = int(row[1])
        novel_edges = row[2]
        print(
            f"{tag:<18} {outputs:>5} {hits:>14} "
            f"{hits / outputs * 100:>7.1f}% {len(novel_edges):>14}"
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
        print(f"qa3 landing: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
