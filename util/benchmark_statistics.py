"""Reproducible paired statistics for symbolic-execution benchmarks."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import math
import random
from typing import Sequence


@dataclass(frozen=True)
class MetricSummary:
    n: int
    mean: float
    median: float
    min: float
    max: float
    bootstrap_low: float
    bootstrap_high: float


def _values(values: Sequence[float]) -> list[float]:
    if not values:
        raise ValueError("at least one observation is required")
    result = [float(value) for value in values]
    if any(not math.isfinite(value) for value in result):
        raise ValueError("observations must be finite")
    return result


def summarize(values: Sequence[float], *, seed: int = 0,
              samples: int = 2000) -> MetricSummary:
    data = _values(values)
    if not 100 <= int(samples) <= 100_000:
        raise ValueError("samples must be in 100..100000")
    ordered = sorted(data)
    rng = random.Random(seed)
    means = [sum(rng.choice(data) for _ in data) / len(data)
             for _ in range(int(samples))]
    means.sort()
    return MetricSummary(
        len(data), sum(data) / len(data),
        ordered[(len(ordered) - 1) // 2], min(data), max(data),
        means[int(.025 * len(means))], means[int(.975 * len(means)) - 1])


def cliffs_delta(baseline: Sequence[float], treatment: Sequence[float]) -> float:
    left, right = _values(baseline), _values(treatment)
    wins = sum(1 for x in left for y in right if y > x)
    losses = sum(1 for x in left for y in right if y < x)
    return (wins - losses) / (len(left) * len(right))


def paired_report(baseline: Sequence[float], treatment: Sequence[float], *,
                  seed: int = 0, samples: int = 2000) -> dict[str, object]:
    base, treat = _values(baseline), _values(treatment)
    if len(base) != len(treat):
        raise ValueError("paired benchmark runs must have equal length")
    deltas = [right - left for left, right in zip(base, treat)]
    report = asdict(summarize(deltas, seed=seed, samples=samples))
    report.update({
        "schema": "symcc-benchmark-paired-report-v1",
        "baseline": asdict(summarize(base, seed=seed, samples=samples)),
        "treatment": asdict(summarize(treat, seed=seed, samples=samples)),
        "cliffs_delta": cliffs_delta(base, treat),
        "improved_runs": sum(delta > 0 for delta in deltas),
        "unchanged_runs": sum(delta == 0 for delta in deltas),
        "regressed_runs": sum(delta < 0 for delta in deltas),
    })
    return report
