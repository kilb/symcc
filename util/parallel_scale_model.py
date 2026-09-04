#!/usr/bin/env python3
"""Dependency-free scalability models for parallel symbolic execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable


MIN_DECISION_R_SQUARED = 0.50


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _finite_product(*values: int | float) -> bool:
    product = 1.0
    try:
        for value in values:
            product *= float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(product)


@dataclass(frozen=True)
class ThroughputObservation:
    parallelism: int
    throughput: float
    weight: float = 1.0


@dataclass(frozen=True)
class CoverageObservation:
    parallelism: int
    edges: float
    weight: float = 1.0


@dataclass(frozen=True)
class USLFit:
    scale: float
    contention: float
    coherency: float
    r_squared: float
    rmse: float
    analytic_peak: float | None
    doubling_ceiling: int | None

    def predict(self, parallelism: float) -> float:
        return universal_capacity(
            parallelism,
            self.scale,
            self.contention,
            self.coherency,
        )

    def to_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


@dataclass(frozen=True)
class CoverageSaturationFit:
    seed_edges: float
    baseline_parallelism: int
    baseline_edges: float
    asymptotic_gain: float
    rate: float
    r_squared: float
    rmse: float
    edge_gain_ceiling: int | None

    @property
    def asymptotic_edges(self) -> float:
        return self.baseline_edges + self.asymptotic_gain

    def predict(self, parallelism: float) -> float:
        delta = max(0.0, float(parallelism) - self.baseline_parallelism)
        return self.baseline_edges + self.asymptotic_gain * (
            1.0 - math.exp(-self.rate * delta)
        )

    def to_dict(self) -> dict[str, float | int | None]:
        result = asdict(self)
        result["asymptotic_edges"] = self.asymptotic_edges
        return result


@dataclass(frozen=True)
class ParallelCeiling:
    recommended_parallelism: int
    usl_ceiling: int | None
    coverage_ceiling: int | None
    resource_ceiling: int | None
    limiting_factors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["limiting_factors"] = list(self.limiting_factors)
        return result


def universal_capacity(
    parallelism: float,
    scale: float,
    contention: float,
    coherency: float,
) -> float:
    """Gunther USL capacity with an absolute single-worker scale."""
    n = float(parallelism)
    if n <= 0.0:
        return 0.0
    denominator = 1.0 + contention * (n - 1.0) + coherency * n * (n - 1.0)
    if denominator <= 0.0:
        return 0.0
    return scale * n / denominator


def _validated_throughput(
    observations: Iterable[ThroughputObservation],
) -> tuple[ThroughputObservation, ...]:
    rows = tuple(observations)
    for row in rows:
        if (
            type(row.parallelism) is not int
            or row.parallelism < 1
            or not _finite_number(row.throughput)
            or row.throughput <= 0.0
            or not _finite_number(row.weight)
            or row.weight <= 0.0
            or not _finite_product(
                row.weight, row.throughput, row.throughput
            )
            or not _finite_product(
                row.weight, row.parallelism, row.throughput
            )
        ):
            raise ValueError("invalid throughput observation")
    if len({row.parallelism for row in rows}) < 4:
        raise ValueError("USL fitting requires at least four parallelism levels")
    return rows


def _aggregate_throughput(
    rows: tuple[ThroughputObservation, ...],
) -> tuple[tuple[ThroughputObservation, ...], float]:
    """Return per-level sufficient statistics and exact within-level SSE."""
    grouped: dict[int, list[ThroughputObservation]] = {}
    for row in rows:
        grouped.setdefault(row.parallelism, []).append(row)
    aggregated = []
    within_error = 0.0
    for parallelism, group in sorted(grouped.items()):
        weight = sum(row.weight for row in group)
        mean = sum(row.weight * row.throughput for row in group) / weight
        within_error += sum(
            row.weight * (row.throughput - mean) ** 2 for row in group
        )
        aggregated.append(ThroughputObservation(parallelism, mean, weight))
    if not math.isfinite(within_error):
        raise ValueError("USL fit arithmetic exceeded finite range")
    return tuple(aggregated), within_error


def _fit_scale(
    rows: tuple[ThroughputObservation, ...],
    contention: float,
    coherency: float,
) -> tuple[float, float]:
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        basis = universal_capacity(
            row.parallelism, 1.0, contention, coherency)
        numerator += row.weight * basis * row.throughput
        denominator += row.weight * basis * basis
    scale = numerator / denominator if denominator > 0.0 else 0.0
    squared_error = sum(
        row.weight
        * (universal_capacity(
            row.parallelism, scale, contention, coherency,
        ) - row.throughput) ** 2
        for row in rows
    )
    return scale, squared_error


def _doubling_ceiling(
    fit: USLFit,
    maximum: int,
    minimum_gain: float,
) -> int | None:
    # Only evaluate a true doubling.  Clamping 2*n to the model horizon makes
    # every otherwise-linear curve appear to saturate near the horizon.
    for n in range(1, maximum // 2 + 1):
        current = fit.predict(n)
        doubled = fit.predict(2 * n)
        if current <= 0.0:
            continue
        if doubled <= current or (doubled - current) / current < minimum_gain:
            return n
    return None


def fit_usl(
    observations: Iterable[ThroughputObservation],
    *,
    maximum_parallelism: int = 4096,
    minimum_doubling_gain: float = 0.10,
    minimum_r_squared: float = MIN_DECISION_R_SQUARED,
) -> USLFit:
    """Fit non-negative USL parameters using a deterministic grid search."""
    raw_rows = _validated_throughput(observations)
    rows, within_error = _aggregate_throughput(raw_rows)
    if type(maximum_parallelism) is not int or maximum_parallelism < 2:
        raise ValueError("maximum_parallelism must be at least two")
    if maximum_parallelism < max(row.parallelism for row in rows):
        raise ValueError("maximum_parallelism is below an observed level")
    if (
        not _finite_number(minimum_doubling_gain)
        or not 0.0 <= minimum_doubling_gain < 1.0
    ):
        raise ValueError("minimum_doubling_gain must be in [0, 1)")
    if (
        not _finite_number(minimum_r_squared)
        or not 0.0 <= minimum_r_squared <= 1.0
    ):
        raise ValueError("minimum_r_squared must be in [0, 1]")

    # Coherency is commonly several orders of magnitude below contention.
    # A logarithmic axis preserves the important near-zero part without scipy.
    kappas = [0.0] + [10.0 ** (-8.0 + index * 7.0 / 140.0)
                      for index in range(141)]
    best: tuple[float, float, float, float] | None = None
    for sigma_index in range(201):
        sigma = sigma_index / 200.0
        for kappa in kappas:
            scale, error = _fit_scale(rows, sigma, kappa)
            candidate = (error, sigma, kappa, scale)
            if best is None or candidate < best:
                best = candidate
    assert best is not None

    grouped_error, sigma, kappa, scale = best
    if not all(math.isfinite(value) for value in best):
        raise ValueError("USL fit arithmetic exceeded finite range")
    # Local pattern search improves the coarse grid while retaining bounds.
    sigma_step = 0.01
    kappa_step = max(1e-8, kappa * 0.35, 1e-5)
    for _ in range(28):
        improved = False
        for candidate_sigma in {
            max(0.0, sigma - sigma_step), sigma,
            min(1.0, sigma + sigma_step),
        }:
            for candidate_kappa in {
                max(0.0, kappa - kappa_step), kappa, kappa + kappa_step,
            }:
                candidate_scale, candidate_error = _fit_scale(
                    rows, candidate_sigma, candidate_kappa)
                candidate = (
                    candidate_error,
                    candidate_sigma,
                    candidate_kappa,
                    candidate_scale,
                )
                if candidate < best:
                    best = candidate
                    grouped_error, sigma, kappa, scale = candidate
                    improved = True
        if not improved:
            sigma_step *= 0.5
            kappa_step *= 0.5

    error = grouped_error + within_error
    total_weight = sum(row.weight for row in raw_rows)
    weighted_total = sum(row.weight * row.throughput for row in raw_rows)
    if not math.isfinite(total_weight) or not math.isfinite(weighted_total):
        raise ValueError("USL fit arithmetic exceeded finite range")
    mean = weighted_total / total_weight
    total_variance = sum(
        row.weight * (row.throughput - mean) ** 2 for row in raw_rows)
    variance_scale = sum(
        row.weight * row.throughput * row.throughput for row in raw_rows
    )
    if total_variance <= math.ulp(1.0) * max(total_weight, variance_scale):
        raise ValueError("USL fit quality is undefined for zero weighted variance")
    r_squared = 1.0 - error / total_variance
    rmse = math.sqrt(error / total_weight)
    if r_squared < minimum_r_squared:
        raise ValueError(
            f"USL fit quality R2={r_squared:.4f} is below the decision "
            f"threshold {minimum_r_squared:.4f}"
        )
    analytic_peak = None
    if kappa > 0.0 and sigma < 1.0:
        analytic_peak = math.sqrt((1.0 - sigma) / kappa)

    provisional = USLFit(
        scale=scale,
        contention=sigma,
        coherency=kappa,
        r_squared=r_squared,
        rmse=rmse,
        analytic_peak=analytic_peak,
        doubling_ceiling=None,
    )
    return USLFit(
        scale=scale,
        contention=sigma,
        coherency=kappa,
        r_squared=r_squared,
        rmse=rmse,
        analytic_peak=analytic_peak,
        doubling_ceiling=_doubling_ceiling(
            provisional, maximum_parallelism, minimum_doubling_gain),
    )


def fit_coverage_saturation(
    observations: Iterable[CoverageObservation],
    *,
    seed_edges: float,
    total_edges: float,
    maximum_parallelism: int = 4096,
    minimum_edge_gain: float = 1.0,
    minimum_r_squared: float = MIN_DECISION_R_SQUARED,
) -> CoverageSaturationFit:
    """Fit E(n)=E0+L(1-exp(-rho*n)) to finite coverage endpoints."""
    rows = tuple(observations)
    if (
        not _finite_number(seed_edges)
        or not _finite_number(total_edges)
        or not 0.0 <= seed_edges < total_edges
    ):
        raise ValueError("invalid coverage bounds")
    if type(maximum_parallelism) is not int or maximum_parallelism < 2:
        raise ValueError("maximum_parallelism must be at least two")
    if (
        not _finite_number(minimum_edge_gain)
        or minimum_edge_gain <= 0.0
    ):
        raise ValueError("minimum_edge_gain must be finite and positive")
    if (
        not _finite_number(minimum_r_squared)
        or not 0.0 <= minimum_r_squared <= 1.0
    ):
        raise ValueError("minimum_r_squared must be in [0, 1]")
    grouped: dict[int, list[CoverageObservation]] = {}
    for row in rows:
        if (
            type(row.parallelism) is not int
            or row.parallelism < 1
            or not _finite_number(row.edges)
            or not seed_edges <= row.edges <= total_edges
            or not _finite_number(row.weight)
            or row.weight <= 0.0
            or not _finite_product(row.weight, row.edges, row.edges)
        ):
            raise ValueError("invalid coverage observation")
        grouped.setdefault(row.parallelism, []).append(row)
    if len(grouped) < 4:
        raise ValueError("coverage fitting requires at least four levels")
    if maximum_parallelism < max(grouped):
        raise ValueError("maximum_parallelism is below an observed level")
    aggregated = tuple(
        CoverageObservation(
            parallelism,
            sum(item.edges * item.weight for item in group)
            / sum(item.weight for item in group),
            sum(item.weight for item in group),
        )
        for parallelism, group in sorted(grouped.items())
    )
    baseline_parallelism = min(grouped)
    baseline_edges = next(
        row.edges for row in aggregated
        if row.parallelism == baseline_parallelism)
    previous_edges = None
    for row in aggregated:
        if previous_edges is not None and row.edges + 1e-9 < previous_edges:
            raise ValueError("invalid coverage observation")
        previous_edges = row.edges
    gains = []
    modeled_rows = []
    within_error = sum(
        item.weight * (item.edges - row.edges) ** 2
        for row in aggregated
        for item in grouped[row.parallelism]
    )
    for row in aggregated:
        gain = row.edges - baseline_edges
        if (
            not math.isfinite(gain)
            or gain < -1e-9
        ):
            raise ValueError("invalid coverage observation")
        if row.parallelism > baseline_parallelism:
            gains.append(max(0.0, gain))
            modeled_rows.append(row)
    maximum_gain = max(gains)
    capacity = total_edges - baseline_edges
    if maximum_gain <= 0.0:
        raise ValueError("coverage observations contain no gain over the seed")

    lower = min(capacity, maximum_gain + max(1e-6, maximum_gain * 1e-6))
    best: tuple[float, float, float] | None = None
    for index in range(401):
        limit = lower + (capacity - lower) * index / 400.0
        if limit <= maximum_gain:
            continue
        observed_rates = [
            -math.log(max(1e-15, 1.0 - gain / limit))
            / (row.parallelism - baseline_parallelism)
            for row, gain in zip(modeled_rows, gains)
        ]
        upper_rate = max(1e-9, max(observed_rates) * 4.0)

        def error_at(candidate_rate: float) -> float:
            return sum(
                row.weight * (
                    limit
                    * (1.0 - math.exp(
                        -candidate_rate
                        * (row.parallelism - baseline_parallelism)))
                    - gain
                ) ** 2
                for row, gain in zip(modeled_rows, gains)
            )

        low_rate = 0.0
        high_rate = upper_rate
        for _ in range(56):
            left = (2.0 * low_rate + high_rate) / 3.0
            right = (low_rate + 2.0 * high_rate) / 3.0
            if error_at(left) <= error_at(right):
                high_rate = right
            else:
                low_rate = left
        rate = (low_rate + high_rate) / 2.0
        error = error_at(rate)
        candidate = (error, limit, rate)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        raise ValueError("coverage saturation fit is underdetermined")
    grouped_error, limit, rate = best
    if not all(math.isfinite(value) for value in best):
        raise ValueError("coverage fit arithmetic exceeded finite range")
    error = grouped_error + within_error
    raw_modeled = [
        item
        for parallelism, group in grouped.items()
        if parallelism > baseline_parallelism
        for item in group
    ]
    total_weight = sum(row.weight for row in raw_modeled)
    weighted_total = sum(
        row.weight * (row.edges - baseline_edges) for row in raw_modeled
    )
    if not math.isfinite(total_weight) or not math.isfinite(weighted_total):
        raise ValueError("coverage fit arithmetic exceeded finite range")
    mean = weighted_total / total_weight
    variance = sum(
        row.weight * ((row.edges - baseline_edges) - mean) ** 2
        for row in raw_modeled)
    variance_scale = sum(
        row.weight * (row.edges - baseline_edges) ** 2
        for row in raw_modeled
    )
    if variance <= math.ulp(1.0) * max(total_weight, variance_scale):
        raise ValueError(
            "coverage fit quality is undefined for zero weighted variance"
        )
    r_squared = 1.0 - error / variance
    rmse_weight = sum(row.weight for row in rows)
    rmse = math.sqrt(error / rmse_weight)
    if r_squared < minimum_r_squared:
        raise ValueError(
            f"coverage fit quality R2={r_squared:.4f} is below the decision "
            f"threshold {minimum_r_squared:.4f}"
        )

    ceiling = None
    for n in range(
        baseline_parallelism,
        maximum_parallelism // 2 + 1,
    ):
        current = limit * (
            1.0 - math.exp(-rate * (n - baseline_parallelism)))
        doubled = limit * (
            1.0 - math.exp(
                -rate * (2 * n - baseline_parallelism)))
        if doubled - current < minimum_edge_gain:
            ceiling = n
            break
    return CoverageSaturationFit(
        seed_edges=seed_edges,
        baseline_parallelism=baseline_parallelism,
        baseline_edges=baseline_edges,
        asymptotic_gain=limit,
        rate=rate,
        r_squared=r_squared,
        rmse=rmse,
        edge_gain_ceiling=ceiling,
    )


def combine_ceilings(
    usl: USLFit | None,
    coverage: CoverageSaturationFit | None,
    *,
    resource_ceiling: int | None = None,
) -> ParallelCeiling:
    candidates: list[tuple[str, int]] = []
    usl_ceiling = usl.doubling_ceiling if usl is not None else None
    if usl is not None and usl.analytic_peak is not None:
        peak = max(1, int(math.floor(usl.analytic_peak)))
        usl_ceiling = peak if usl_ceiling is None else min(usl_ceiling, peak)
    if usl_ceiling is not None:
        candidates.append(("throughput", usl_ceiling))
    coverage_ceiling = coverage.edge_gain_ceiling if coverage else None
    if coverage_ceiling is not None:
        candidates.append(("coverage-novelty", coverage_ceiling))
    if resource_ceiling is not None:
        if type(resource_ceiling) is not int or resource_ceiling < 1:
            raise ValueError("resource_ceiling must be positive")
        candidates.append(("resource", resource_ceiling))
    if not candidates:
        raise ValueError("at least one finite ceiling is required")
    recommended = min(value for _name, value in candidates)
    limiting = tuple(name for name, value in candidates if value == recommended)
    return ParallelCeiling(
        recommended_parallelism=recommended,
        usl_ceiling=usl_ceiling,
        coverage_ceiling=coverage_ceiling,
        resource_ceiling=resource_ceiling,
        limiting_factors=limiting,
    )
