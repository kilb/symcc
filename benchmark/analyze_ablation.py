#!/usr/bin/env python3
"""Paired statistical analysis for reproducible SymCC ablations."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Iterable, Mapping

from research_protocol import (
    coverage_auc,
    time_to_target,
    verify_pcfg_research_artifact,
    verify_parser_research_artifact,
)


SUCCESS_STATUSES = frozenset({"", "ok", "success", "completed"})
LOWER_IS_BETTER_HINTS = (
    "time", "latency", "delay", "cpu", "rss", "cost", "redundancy",
    "nll", "memory", "bytes", "overhead",
)


def _float(row: Mapping[str, str], key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _successful(row: Mapping[str, str]) -> bool:
    return str(row.get("status", "")).strip().lower() in SUCCESS_STATUSES


def _censored(row: Mapping[str, str]) -> bool:
    return (
        str(row.get("status", "")).strip().lower() == "timeout"
        or str(row.get("time_to_target_censored", "0")).strip().lower()
        in {"1", "true", "yes"}
    )


def _is_time_to_target_metric(metric: str) -> bool:
    normalized = metric.strip().lower().replace("-", "_")
    return normalized.startswith("time_to_target")


def completion_adjusted_a12(
    treatment: list[Mapping[str, str]],
    baseline: list[Mapping[str, str]],
    metric: str,
    direction: str,
) -> float:
    """Return a favorable A12 without treating censoring as an observation.

    ``time_to_target`` is a right-censored endpoint.  A completed event can only
    beat a censored event when it happened no later than the censoring time; an
    event after that time is indeterminate.  For other metrics, the
    ``time_to_target_censored`` column is unrelated and must not affect ranking.
    """
    def observation(row: Mapping[str, str]) -> tuple[int, float | None]:
        status = str(row.get("status", "")).strip().lower()
        value = _float(row, metric)
        if _successful(row):
            rank = 2
        elif status == "timeout":
            rank = 1
        else:
            rank = 0
        return rank, value

    left = [observation(row) for row in treatment]
    right = [observation(row) for row in baseline]
    if not left or not right:
        return 0.5
    time_to_target = _is_time_to_target_metric(metric)
    wins = 0.0
    for left_row, (left_rank, left_value) in zip(treatment, left):
        for right_row, (right_rank, right_value) in zip(baseline, right):
            if time_to_target:
                left_failed = left_rank == 0
                right_failed = right_rank == 0
                if left_failed != right_failed:
                    wins += 0.0 if left_failed else 1.0
                    continue
                if left_failed and right_failed:
                    wins += 0.5
                    continue
                if left_value is None or right_value is None:
                    wins += 0.5
                    continue
                left_censored = _censored(left_row)
                right_censored = _censored(right_row)
                if left_censored and right_censored:
                    wins += 0.5
                elif left_censored:
                    # The baseline event is known to be faster only when it was
                    # observed before the treatment censoring boundary.
                    wins += 0.0 if right_value <= left_value else 0.5
                elif right_censored:
                    wins += 1.0 if left_value <= right_value else 0.5
                elif left_value == right_value:
                    wins += 0.5
                elif direction == "higher":
                    wins += 1.0 if left_value > right_value else 0.0
                else:
                    wins += 1.0 if left_value < right_value else 0.0
                continue
            if left_rank != right_rank:
                wins += 1.0 if left_rank > right_rank else 0.0
            elif left_value is None or right_value is None:
                wins += 0.5
            elif left_value == right_value:
                wins += 0.5
            elif direction == "higher":
                wins += 1.0 if left_value > right_value else 0.0
            else:
                wins += 1.0 if left_value < right_value else 0.0
    return wins / (len(left) * len(right))


def load_rows(paths: Iterable[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in paths:
        with open(path, newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                row["_source"] = path
                rows.append(row)
    return rows


def load_timeseries(paths: Iterable[str]) -> list[dict[str, str]]:
    return load_rows(paths)


def load_pcfg_artifacts(paths: Iterable[str]) -> list[dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    seen: dict[str, str] = {}
    for raw_path in paths:
        source = Path(raw_path)
        candidates = (
            sorted(source.rglob("pcfg_research_artifact.json"))
            if source.is_dir() else [source]
        )
        for candidate in candidates:
            with candidate.open(encoding="utf-8") as stream:
                artifact = json.load(stream)
            if not isinstance(artifact, dict):
                raise ValueError(
                    f"PCFG artifact {candidate} is not an object")
            verify_pcfg_research_artifact(artifact)
            metadata = artifact["metadata"]
            run_id = str(metadata.get("run_id", ""))
            digest = str(artifact["artifact_sha256"])
            if not run_id:
                raise ValueError(
                    f"PCFG artifact {candidate} has no run_id")
            previous = seen.setdefault(run_id, digest)
            if previous != digest:
                raise ValueError(
                    f"conflicting PCFG artifacts for run {run_id}")
            if previous == digest and any(
                str(item["metadata"].get("run_id", "")) == run_id
                for item in artifacts
            ):
                continue
            artifact["_source"] = str(candidate)
            artifacts.append(artifact)
    return artifacts


def attach_pcfg_artifact_metrics(
    rows: list[dict[str, str]],
    artifacts: Iterable[Mapping[str, object]],
) -> None:
    """Join verified scalar PCFG telemetry to benchmark rows by run id."""
    by_run: dict[str, Mapping[str, object]] = {}
    for artifact in artifacts:
        metadata = artifact.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        run_id = str(metadata.get("run_id", ""))
        if run_id:
            by_run[run_id] = artifact
    for row in rows:
        run_id = str(
            row.get("protocol_run_id", "") or row.get("run_id", ""))
        artifact = by_run.get(run_id)
        if artifact is None:
            continue
        metrics = artifact.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        row["pcfg_artifact_verified"] = "1"
        row["pcfg_artifact_sha256"] = str(
            artifact.get("artifact_sha256", ""))
        for key, value in metrics.items():
            if (
                str(key).startswith("pcfg_") and
                isinstance(value, (str, int, float)) and
                not isinstance(value, bool)
            ):
                row[str(key)] = str(value)


def load_parser_artifacts(paths: Iterable[str]) -> list[dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    seen: dict[str, str] = {}
    for raw_path in paths:
        source = Path(raw_path)
        candidates = (
            sorted(source.rglob("parser_research_artifact.json"))
            if source.is_dir() else [source]
        )
        for candidate in candidates:
            with candidate.open(encoding="utf-8") as stream:
                artifact = json.load(stream)
            if not isinstance(artifact, dict):
                raise ValueError(
                    f"parser artifact {candidate} is not an object")
            verify_parser_research_artifact(artifact)
            metadata = artifact["metadata"]
            run_id = str(metadata.get("run_id", ""))
            digest = str(artifact["artifact_sha256"])
            if not run_id:
                raise ValueError(
                    f"parser artifact {candidate} has no run_id")
            previous = seen.setdefault(run_id, digest)
            if previous != digest:
                raise ValueError(
                    f"conflicting parser artifacts for run {run_id}")
            if previous == digest and any(
                str(item["metadata"].get("run_id", "")) == run_id
                for item in artifacts
            ):
                continue
            artifact["_source"] = str(candidate)
            artifacts.append(artifact)
    return artifacts


def attach_parser_artifact_metrics(
    rows: list[dict[str, str]],
    artifacts: Iterable[Mapping[str, object]],
) -> None:
    """Join verified scalar parser telemetry to benchmark rows by run id."""
    by_run: dict[str, Mapping[str, object]] = {}
    for artifact in artifacts:
        metadata = artifact.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        run_id = str(metadata.get("run_id", ""))
        if run_id:
            by_run[run_id] = artifact
    for row in rows:
        run_id = str(
            row.get("protocol_run_id", "") or row.get("run_id", ""))
        artifact = by_run.get(run_id)
        if artifact is None:
            continue
        metrics = artifact.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        row["parser_artifact_verified"] = "1"
        row["parser_artifact_sha256"] = str(
            artifact.get("artifact_sha256", ""))
        for key, value in metrics.items():
            if (
                str(key).startswith("proposal_parser_") and
                isinstance(value, (str, int, float)) and
                not isinstance(value, bool)
            ):
                row[str(key)] = str(value)


def bootstrap_ci(
    values: list[float],
    samples: int = 5000,
    confidence: float = 0.95,
    seed: int = 1337,
) -> tuple[float, float]:
    """Percentile bootstrap interval for a sample median."""
    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        return (values[0], values[0])
    rng = random.Random(seed)
    estimates = []
    for _ in range(max(100, int(samples))):
        draw = [values[rng.randrange(len(values))] for _ in values]
        estimates.append(median(draw))
    estimates.sort()
    alpha = (1.0 - confidence) / 2.0
    lo = estimates[int(alpha * (len(estimates) - 1))]
    hi = estimates[int((1.0 - alpha) * (len(estimates) - 1))]
    return (lo, hi)


def paired_bootstrap_ci(
    differences: list[float],
    samples: int = 5000,
    confidence: float = 0.95,
    seed: int = 7331,
) -> tuple[float, float]:
    """Bootstrap a paired median difference without breaking blocks."""
    return bootstrap_ci(differences, samples, confidence, seed)


def vargha_delaney(a: list[float], b: list[float]) -> float:
    """Return A12: probability a random a exceeds b, ties count half."""
    if not a or not b:
        return 0.5
    wins = 0.0
    for left in a:
        for right in b:
            wins += 1.0 if left > right else 0.5 if left == right else 0.0
    return wins / (len(a) * len(b))


def randomization_p_value(
    differences: list[float],
    *,
    samples: int = 20000,
    seed: int = 20260727,
) -> float:
    """Two-sided paired sign-flip randomization test."""
    if not differences:
        return 1.0
    observed = abs(mean(differences))
    count = 0
    if len(differences) <= 16:
        total = 1 << len(differences)
        for mask in range(total):
            estimate = mean([
                value if mask & (1 << index) else -value
                for index, value in enumerate(differences)
            ])
            count += abs(estimate) >= observed - 1e-15
        return count / total
    rng = random.Random(seed)
    total = max(1000, int(samples))
    for _ in range(total):
        estimate = mean([
            value if rng.getrandbits(1) else -value
            for value in differences
        ])
        count += abs(estimate) >= observed - 1e-15
    return (count + 1) / (total + 1)


def holm_adjust(p_values: list[float]) -> list[float]:
    """Holm step-down family-wise error correction."""
    if not p_values:
        return []
    order = sorted(range(len(p_values)), key=lambda index: p_values[index])
    adjusted = [1.0] * len(p_values)
    running = 0.0
    total = len(p_values)
    for rank, index in enumerate(order):
        candidate = min(1.0, (total - rank) * p_values[index])
        running = max(running, candidate)
        adjusted[index] = running
    return adjusted


def _row_key(row: Mapping[str, str]) -> tuple[str, str, str]:
    return (
        str(row.get("target", "")),
        str(row.get("np", "")),
        str(row.get("configuration", "") or row.get("mode", "")),
    )


def _pair_key(row: Mapping[str, str]) -> str:
    explicit = str(row.get("pair_id", "")).strip()
    if explicit:
        return explicit
    repeat = str(row.get("round", row.get("repeat", ""))).strip()
    experiment = str(row.get("experiment_id", "")).strip()
    return f"{experiment}:{repeat}" if repeat else ""


def _metric_direction(metric: str, explicit: str = "auto") -> str:
    if explicit in {"higher", "lower"}:
        return explicit
    lowered = metric.lower()
    return (
        "lower" if any(hint in lowered for hint in LOWER_IS_BETTER_HINTS)
        else "higher"
    )


def attach_timeseries_metrics(
    rows: list[dict[str, str]],
    timeseries: list[dict[str, str]],
    *,
    budget_seconds: float | None = None,
    target_threshold: float | None = None,
    value_key: str = "edges_found",
) -> None:
    """Attach coverage AUC and censored target time to benchmark rows."""
    grouped: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for point in timeseries:
        run_id = str(point.get("run_id", "")).strip()
        if run_id:
            key = ("run", run_id)
        else:
            key = (
                "legacy",
                str(point.get("target", "")),
                str(point.get("configuration", "") or point.get("mode", "")),
                str(point.get("np", "")),
                str(point.get("round", point.get("repeat", ""))),
            )
        grouped[key].append(point)

    for row in rows:
        run_id = str(row.get("run_id", "")).strip()
        if run_id:
            key = ("run", run_id)
        else:
            key = (
                "legacy",
                str(row.get("target", "")),
                str(row.get("configuration", "") or row.get("mode", "")),
                str(row.get("np", "")),
                str(row.get("round", row.get("repeat", ""))),
            )
        points = grouped.get(key, [])
        if not points:
            continue
        budget = budget_seconds
        if budget is None:
            budget = (
                _float(row, "wall_budget_seconds")
                or _float(row, "wall_time_sec")
                or max(
                    _float(point, "timestamp_sec") or 0.0
                    for point in points
                )
            )
        if budget <= 0:
            continue
        row["coverage_auc"] = str(
            coverage_auc(points, budget, value_key=value_key))
        if target_threshold is not None:
            target_time, censored = time_to_target(
                points, target_threshold, budget, value_key=value_key)
            row["time_to_target_sec"] = str(target_time)
            row["time_to_target_censored"] = "1" if censored else "0"


def summarize(
    rows: list[dict[str, str]],
    metric: str,
    baseline_mode: str | None = None,
    *,
    direction: str = "auto",
    bootstrap_samples: int = 5000,
    permutation_samples: int = 20000,
) -> list[dict[str, object]]:
    """Summarize a metric while retaining failed and censored runs."""
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        target, np_value, mode = _row_key(row)
        if target and mode:
            grouped[(target, np_value, mode)].append(row)

    baselines: dict[tuple[str, str], str] = {}
    for target, np_value, mode in sorted(grouped):
        key = (target, np_value)
        if baseline_mode and mode == baseline_mode:
            baselines[key] = mode
        elif key not in baselines:
            baselines[key] = mode

    metric_direction = _metric_direction(metric, direction)
    if _is_time_to_target_metric(metric) and metric_direction != "lower":
        raise ValueError("time-to-target metrics must use lower-is-better direction")
    sign = 1.0 if metric_direction == "higher" else -1.0
    out: list[dict[str, object]] = []
    comparison_indices: list[int] = []
    comparison_p_values: list[float] = []
    for (target, np_value, mode), group_rows in sorted(grouped.items()):
        values = [
            value for row in group_rows
            if (value := _float(row, metric)) is not None
        ]
        values.sort()
        baseline = baselines[(target, np_value)]
        baseline_rows = grouped.get((target, np_value, baseline), [])
        baseline_values = [
            value for row in baseline_rows
            if (value := _float(row, metric)) is not None
        ]
        baseline_values.sort()
        ci_lo, ci_hi = bootstrap_ci(
            values, samples=bootstrap_samples)
        med = median(values) if values else 0.0
        base_med = median(baseline_values) if baseline_values else 0.0
        delta = med - base_med
        pct = (delta / abs(base_med) * 100.0) if base_med else 0.0

        mode_pairs = {
            _pair_key(row): value
            for row in group_rows
            if _pair_key(row)
            and (value := _float(row, metric)) is not None
        }
        baseline_pairs = {
            _pair_key(row): value
            for row in baseline_rows
            if _pair_key(row)
            and (value := _float(row, metric)) is not None
        }
        common_pairs = sorted(set(mode_pairs) & set(baseline_pairs))
        paired_differences = [
            mode_pairs[pair] - baseline_pairs[pair] for pair in common_pairs
        ]
        if mode == baseline:
            paired_differences = [0.0 for _ in baseline_values]
            common_pairs = sorted(baseline_pairs)
        if paired_differences:
            delta_ci_lo, delta_ci_hi = paired_bootstrap_ci(
                paired_differences, samples=bootstrap_samples)
            p_value = (
                1.0 if mode == baseline
                else randomization_p_value(
                    [sign * value for value in paired_differences],
                    samples=permutation_samples,
                )
            )
        else:
            delta_ci_lo, delta_ci_hi = (0.0, 0.0)
            p_value = 1.0

        favorable_a12 = completion_adjusted_a12(
            group_rows,
            baseline_rows,
            metric,
            metric_direction,
        )
        statuses = [
            str(row.get("status", "")).strip().lower() for row in group_rows
        ]
        censored = sum(
            str(row.get("time_to_target_censored", "0")).lower()
            in {"1", "true", "yes"}
            for row in group_rows
        )
        phase_values = {
            str(row.get("phase", row.get("research_phase", ""))).lower()
            for row in group_rows
            if row.get("phase", row.get("research_phase", ""))
        }
        confirmatory = (
            phase_values == {"confirmatory"}
            and len(group_rows) >= 20
            and (mode == baseline or len(common_pairs) >= 20)
            and not censored
            and all(status in SUCCESS_STATUSES for status in statuses)
            and len(values) == len(group_rows)
        )
        result: dict[str, object] = {
            "target": target,
            "np": np_value,
            "mode": mode,
            "metric": metric,
            "direction": metric_direction,
            "n_total": len(group_rows),
            "n": len(values),
            "n_success": sum(status in SUCCESS_STATUSES for status in statuses),
            "n_failed": sum(
                status not in SUCCESS_STATUSES and status != "timeout"
                for status in statuses
            ),
            "n_timeout": sum(status == "timeout" for status in statuses),
            "n_censored": censored,
            "paired_n": len(common_pairs),
            "mean": mean(values) if values else 0.0,
            "median": med,
            "ci95_low": ci_lo,
            "ci95_high": ci_hi,
            "baseline_mode": baseline,
            "delta_vs_baseline": delta,
            "favorable_delta": sign * delta,
            "delta_ci95_low": delta_ci_lo,
            "delta_ci95_high": delta_ci_hi,
            "pct_vs_baseline": pct,
            "a12_vs_baseline": vargha_delaney(values, baseline_values),
            "a12_favorable": favorable_a12,
            "cliffs_delta_favorable": 2.0 * favorable_a12 - 1.0,
            "censoring_method": (
                "right-censored-pairwise-concordance"
                if _is_time_to_target_metric(metric)
                else "completion-rank;time-to-target-censoring-not-applicable"
            ),
            "randomization_p": p_value,
            "holm_p": 1.0,
            "evidence_grade": "confirmatory" if confirmatory else "exploratory",
        }
        out.append(result)
        if mode != baseline:
            comparison_indices.append(len(out) - 1)
            comparison_p_values.append(p_value)

    for index, adjusted in zip(
        comparison_indices, holm_adjust(comparison_p_values)
    ):
        out[index]["holm_p"] = adjusted
    return out


SUMMARY_FIELDS = [
    "target", "np", "mode", "metric", "direction", "n_total", "n",
    "n_success", "n_failed", "n_timeout", "n_censored", "paired_n",
    "mean", "median", "ci95_low", "ci95_high", "baseline_mode",
    "delta_vs_baseline", "favorable_delta", "delta_ci95_low",
    "delta_ci95_high", "pct_vs_baseline", "a12_vs_baseline",
    "a12_favorable", "cliffs_delta_favorable", "randomization_p",
    "holm_p", "censoring_method", "evidence_grade",
]


def write_csv(rows: list[dict[str, object]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(rows: list[dict[str, object]], path: str) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("# Ablation Summary\n\n")
        stream.write(
            "| target | np | mode | success/total | paired | median "
            "| 95% CI | favorable delta | A12 | Cliff delta | Holm p | grade |\n"
        )
        stream.write(
            "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|\n"
        )
        for row in rows:
            stream.write(
                f"| {row['target']} | {row['np']} | {row['mode']} | "
                f"{row['n_success']}/{row['n_total']} | {row['paired_n']} | "
                f"{float(row['median']):.3f} | "
                f"[{float(row['ci95_low']):.3f}, "
                f"{float(row['ci95_high']):.3f}] | "
                f"{float(row['favorable_delta']):+.3f} | "
                f"{float(row['a12_favorable']):.3f} | "
                f"{float(row['cliffs_delta_favorable']):+.3f} | "
                f"{float(row['holm_p']):.4g} | "
                f"{row['evidence_grade']} |\n"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", nargs="+", help="benchmark_data.csv files")
    parser.add_argument(
        "--metric", action="append", default=None,
        help="metric column; repeat for multiple outcomes",
    )
    parser.add_argument("--baseline-mode", default=None)
    parser.add_argument(
        "--direction", choices=["auto", "higher", "lower"], default="auto")
    parser.add_argument("--timeseries", nargs="*", default=[])
    parser.add_argument("--budget-seconds", type=float, default=None)
    parser.add_argument("--time-to-target", type=float, default=None)
    parser.add_argument("--timeseries-value", default="edges_found")
    parser.add_argument(
        "--pcfg-artifact", nargs="*", default=[],
        help=(
            "PCFG research artifact files or result directories; "
            "verified scalar metrics are joined by run id"
        ),
    )
    parser.add_argument(
        "--parser-artifact", nargs="*", default=[],
        help=(
            "parser research artifact files or result directories; "
            "verified scalar metrics are joined by run id"
        ),
    )
    parser.add_argument("--bootstrap-samples", type=int, default=5000)
    parser.add_argument("--permutation-samples", type=int, default=20000)
    parser.add_argument("--strict-confirmatory", action="store_true")
    parser.add_argument("--output-dir", default="benchmark/ablation_results")
    args = parser.parse_args()

    source_rows = load_rows(args.csv)
    if args.pcfg_artifact:
        attach_pcfg_artifact_metrics(
            source_rows,
            load_pcfg_artifacts(args.pcfg_artifact),
        )
    if args.parser_artifact:
        attach_parser_artifact_metrics(
            source_rows,
            load_parser_artifacts(args.parser_artifact),
        )
    if args.timeseries:
        attach_timeseries_metrics(
            source_rows,
            load_timeseries(args.timeseries),
            budget_seconds=args.budget_seconds,
            target_threshold=args.time_to_target,
            value_key=args.timeseries_value,
        )
    metrics = args.metric or ["edge_cov_pct"]
    rows: list[dict[str, object]] = []
    for metric in metrics:
        rows.extend(summarize(
            source_rows,
            metric,
            args.baseline_mode,
            direction=args.direction,
            bootstrap_samples=args.bootstrap_samples,
            permutation_samples=args.permutation_samples,
        ))
    if args.strict_confirmatory:
        weak = [
            row for row in rows
            if row["mode"] != row["baseline_mode"]
            and row["evidence_grade"] != "confirmatory"
        ]
        if weak:
            raise SystemExit(
                f"confirmatory gate rejected {len(weak)} underpowered comparisons"
            )

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    write_csv(rows, str(output / "ablation_summary.csv"))
    write_markdown(rows, str(output / "ablation_summary.md"))
    with (output / "ablation_summary.json").open(
            "w", encoding="utf-8") as stream:
        json.dump({
            "schema": "symcc-paired-ablation-summary-v1",
            "metrics": metrics,
            "methods": {
                "location": "median",
                "confidence_interval": "paired percentile bootstrap where possible",
                "effect_sizes": ["Vargha-Delaney A12", "Cliff's delta"],
                "hypothesis_test": "paired sign-flip randomization",
                "multiplicity": "Holm family-wise correction",
                "failures_retained": True,
                "minimum_confirmatory_repeats": 20,
            },
            "rows": rows,
        }, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(f"Wrote {len(rows)} rows to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
