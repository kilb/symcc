#!/usr/bin/env python3
"""Analyze multi-round scaling results and estimate a useful worker ceiling."""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from util.parallel_scale_model import (  # noqa: E402
    CoverageObservation,
    ThroughputObservation,
    combine_ceilings,
    fit_coverage_saturation,
    fit_usl,
)


_MAX_EXACT_COUNT = (1 << 64) - 1


def _number(
    row: dict[str, str],
    *names: str,
    default: float | None = 0.0,
) -> float | None:
    for name in names:
        value = row.get(name, "")
        if value not in (None, ""):
            try:
                result = float(value)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError(f"{name} is not numeric") from error
            if not math.isfinite(result):
                raise ValueError(f"{name} is not finite")
            return result
    return default


def _integer(
    row: dict[str, str],
    name: str,
    *,
    default: int | None = None,
    minimum: int = 0,
) -> int | None:
    raw = row.get(name, "")
    if raw in (None, ""):
        return default
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} must be an integer >= {minimum}") from error
    if (
        not value.is_finite()
        or value != value.to_integral_value()
        or value < minimum
        or value > _MAX_EXACT_COUNT
    ):
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return int(value)


def _required_number(row: dict[str, str], *names: str) -> float:
    value = _number(row, *names, default=None)
    if value is None:
        raise ValueError(f"successful run is missing {' or '.join(names)}")
    return value


def _required_count(row: dict[str, str], *names: str) -> int:
    for name in names:
        raw = row.get(name, "")
        if raw in (None, ""):
            continue
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError) as error:
            raise ValueError(
                f"{' or '.join(names)} must be a non-negative integer count"
            ) from error
        if (
            not value.is_finite()
            or value != value.to_integral_value()
            or value < 0
            or value > _MAX_EXACT_COUNT
        ):
            raise ValueError(
                f"{' or '.join(names)} must be a non-negative integer count"
            )
        return int(value)
    raise ValueError(f"successful run is missing {' or '.join(names)}")


def _optional_boolean(row: dict[str, str], name: str) -> bool | None:
    value = (row.get(name, "") or "").strip().lower()
    if not value:
        return None
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _worker_count(np_value: int, mode: str, workers_per_master: int) -> int:
    if mode != "mpi":
        return np_value
    available = np_value - 1
    if available <= workers_per_master:
        masters = 1
    else:
        masters = max(1, min(
            math.ceil(available / workers_per_master), available // 3))
    return max(1, np_value - masters)


def _allocation(
    raw: dict[str, str],
    np_value: int,
    mode: str,
    workers_per_master: int,
) -> tuple[int, int, int, int, bool]:
    reported_workers = _integer(raw, "num_workers", default=None, minimum=0)
    reported_masters = _integer(raw, "num_masters", default=None, minimum=0)
    reported_afl = _integer(raw, "afl_instances", default=None, minimum=0)
    inferred = False

    if mode == "mpi":
        if np_value < 2:
            raise ValueError("MPI allocation requires at least two ranks")
        if reported_workers in (None, 0) and reported_masters in (None, 0):
            workers = _worker_count(np_value, mode, workers_per_master)
            masters = np_value - workers
            inferred = True
        elif reported_workers in (None, 0):
            masters = int(reported_masters)
            workers = np_value - masters
            inferred = True
        elif reported_masters in (None, 0):
            workers = int(reported_workers)
            masters = np_value - workers
            inferred = True
        else:
            workers = int(reported_workers)
            masters = int(reported_masters)
        afl_instances = 0
        if workers < 1 or masters < 1 or workers + masters != np_value:
            raise ValueError(
                "MPI role ledger must satisfy workers + masters == np"
            )
    elif mode == "hybrid":
        if np_value < 3:
            raise ValueError("hybrid allocation requires at least three roles")
        masters = 1 if reported_masters in (None, 0) else int(reported_masters)
        if reported_masters in (None, 0):
            inferred = True
        if reported_workers not in (None, 0) and reported_afl not in (None, 0):
            workers = int(reported_workers)
            afl_instances = int(reported_afl)
        elif reported_workers not in (None, 0):
            workers = int(reported_workers)
            afl_instances = np_value - masters - workers
            inferred = True
        elif reported_afl not in (None, 0):
            afl_instances = int(reported_afl)
            workers = np_value - masters - afl_instances
            inferred = True
        else:
            raise ValueError(
                "hybrid allocation needs num_workers or afl_instances"
            )
        if (
            workers < 1
            or masters < 1
            or afl_instances < 1
            or workers + masters + afl_instances != np_value
        ):
            raise ValueError(
                "hybrid role ledger must satisfy workers + masters + "
                "afl_instances == np"
            )
    else:
        workers = np_value
        masters = 0
        afl_instances = 0
    model_parallelism = (
        workers + afl_instances
        if mode == "hybrid"
        else workers if mode == "mpi" else np_value
    )
    return workers, masters, afl_instances, model_parallelism, inferred


def _allocation_key(row: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """Return the complete resource allocation represented by a run."""
    return (
        int(row["np"]),
        int(row["workers"]),
        int(row["masters"]),
        int(row["afl_instances"]),
        int(row["parallelism"]),
    )


def _mean_ci(values: list[float]) -> tuple[float, float, float]:
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, mean, mean
    rng = random.Random(20260827)
    estimates = sorted(
        statistics.fmean(values[rng.randrange(len(values))] for _ in values)
        for _ in range(5000)
    )
    return (
        mean,
        estimates[int(0.025 * (len(estimates) - 1))],
        estimates[int(0.975 * (len(estimates) - 1))],
    )


def _load_rows(
    paths: list[Path], target: str, mode: str, workers_per_master: int,
) -> tuple[
    list[dict[str, Any]], dict[str, Any], float | None, float | None,
]:
    runs: list[dict[str, Any]] = []
    attempts: dict[tuple[int, int, int, int, int], dict[str, Any]] = {}
    seed_edge_values: set[float] = set()
    total_edge_values: set[float] = set()
    attempted_run_ids: set[str] = set()
    inferred_run_ids = 0
    coverage_provenance_missing = 0
    coverage_provenance_invalid = 0
    coverage_sampling_provenance_missing = 0
    coverage_sampling_provenance_invalid = 0
    coverage_sampled_rows = 0
    auxiliary_slots_missing = 0
    auxiliary_slot_values: set[int] = set()

    def observe_coverage_sampling(raw: dict[str, str]) -> None:
        nonlocal coverage_sampling_provenance_missing
        nonlocal coverage_sampling_provenance_invalid
        nonlocal coverage_sampled_rows
        sampled = _optional_boolean(raw, "coverage_sampled")
        sampled_cases = _integer(
            raw, "coverage_sampled_cases", default=None, minimum=0
        )
        total_cases = _integer(
            raw, "coverage_total_cases", default=None, minimum=0
        )
        if sampled is None or sampled_cases is None or total_cases is None:
            coverage_sampling_provenance_missing += 1
            return
        if (
            sampled_cases > total_cases
            or (sampled and sampled_cases >= total_cases)
            or (not sampled and sampled_cases != total_cases)
        ):
            coverage_sampling_provenance_invalid += 1
            return
        coverage_sampled_rows += int(sampled)
    for path in paths:
        with path.open(newline="", encoding="utf-8") as stream:
            for line_number, raw in enumerate(csv.DictReader(stream), 2):
                if raw.get("target") != target:
                    continue
                row_mode = raw.get("mode", "")
                if row_mode == "seed":
                    seed_value = _integer(
                        raw, "edges_found", default=None, minimum=0
                    )
                    total_value = _integer(
                        raw, "edges_total", default=None, minimum=1
                    )
                    if seed_value is not None:
                        seed_edge_values.add(seed_value)
                    if total_value is not None:
                        total_edge_values.add(total_value)
                    if seed_value is not None or total_value is not None:
                        observe_coverage_sampling(raw)
                        measurement_ok = _optional_boolean(
                            raw, "coverage_measure_ok"
                        )
                        denominator_kind = (
                            raw.get("coverage_denominator_kind", "") or ""
                        ).strip()
                        if measurement_ok is None or not denominator_kind:
                            coverage_provenance_missing += 1
                        elif not measurement_ok or denominator_kind != "existing_edges":
                            coverage_provenance_invalid += 1
                    continue
                if row_mode != mode:
                    continue
                status = (raw.get("status", "success") or "success").strip().lower()
                if status not in {"success", "failed", "timeout"}:
                    raise ValueError(f"unsupported run status {status!r}")
                np_value = _integer(raw, "np", minimum=1)
                assert np_value is not None
                workers, masters, afl_instances, model_parallelism, inferred = (
                    _allocation(raw, np_value, mode, workers_per_master)
                )
                allocation_key = (
                    np_value, workers, masters, afl_instances, model_parallelism,
                )
                allocation = attempts.setdefault(allocation_key, {
                    "np": np_value,
                    "parallelism": model_parallelism,
                    "workers": workers,
                    "masters": masters,
                    "afl_instances": afl_instances,
                    "attempted": 0,
                    "successful": 0,
                    "failed": 0,
                    "timed_out": 0,
                    "roles_inferred": 0,
                })
                run_id = str(raw.get("run_id", "") or "").strip()
                if run_id:
                    if run_id in attempted_run_ids:
                        raise ValueError(f"duplicate run_id {run_id!r}")
                else:
                    run_id = f"inferred:{path.resolve()}:{line_number}"
                    inferred_run_ids += 1
                attempted_run_ids.add(run_id)
                allocation["attempted"] += 1
                allocation["roles_inferred"] += int(inferred)
                reported_auxiliary = _integer(
                    raw,
                    "auxiliary_compute_slots",
                    default=None,
                    minimum=0,
                )
                if reported_auxiliary is None:
                    # The standalone MPI driver has no out-of-rank compute
                    # pools. Hybrid attempts must carry the Master's ledger,
                    # including failures and timeouts, or their resource
                    # allocation is unknown.
                    auxiliary_slots = 0
                    if mode == "hybrid":
                        auxiliary_slots_missing += 1
                    else:
                        auxiliary_slot_values.add(0)
                else:
                    auxiliary_slots = reported_auxiliary
                    auxiliary_slot_values.add(auxiliary_slots)
                if status == "success":
                    allocation["successful"] += 1
                elif status == "timeout":
                    allocation["timed_out"] += 1
                else:
                    allocation["failed"] += 1
                if status != "success":
                    continue

                wall = _required_number(raw, "wall_time_sec", "wall_time")
                if wall <= 0.0:
                    raise ValueError("successful run requires positive wall time")
                wall_budget = _number(
                    raw, "wall_budget_seconds", default=None,
                )
                if wall_budget is not None and wall_budget <= 0.0:
                    raise ValueError(
                        "wall_budget_seconds must be positive when reported"
                    )
                # Keep numerator and denominator in the same measurement
                # universe.  Pure MPI reports generated concolic candidates;
                # hybrid reports AFL executions plus SymCC candidates.  Using
                # only symcc_generated for hybrid and dividing the combined
                # retained corpus by it can produce an impossible >100%
                # acceptance ratio.
                if mode == "mpi":
                    generated = _required_count(
                        raw, "symcc_generated", "generated"
                    )
                    afl_generated = 0
                    symcc_generated = generated
                else:
                    generated = _required_count(raw, "generated")
                    afl_generated = _required_count(
                        raw, "afl_executions", "afl_generated"
                    )
                    symcc_generated = _required_count(raw, "symcc_generated")
                    if generated != afl_generated + symcc_generated:
                        raise ValueError(
                            "hybrid generated count must equal AFL executions "
                            "plus SymCC candidates"
                        )
                unique = _required_count(raw, "unique")
                if unique > generated:
                    raise ValueError(
                        "unique corpus count exceeds generated work in the "
                        "same measurement universe"
                    )
                edges = _required_count(raw, "edges_found")
                observed_total = _required_count(raw, "edges_total")
                if observed_total <= 0:
                    raise ValueError("coverage universe must be positive")
                if edges > observed_total:
                    raise ValueError("edge count exceeds coverage universe")
                total_edge_values.add(observed_total)
                observe_coverage_sampling(raw)
                measurement_ok = _optional_boolean(raw, "coverage_measure_ok")
                denominator_kind = (
                    raw.get("coverage_denominator_kind", "") or ""
                ).strip()
                if measurement_ok is None or not denominator_kind:
                    coverage_provenance_missing += 1
                elif not measurement_ok or denominator_kind != "existing_edges":
                    coverage_provenance_invalid += 1
                runs.append({
                    "np": float(np_value),
                    "workers": float(workers),
                    "afl_instances": float(afl_instances),
                    "parallelism": float(model_parallelism),
                    "masters": float(masters),
                    "auxiliary_compute_slots": float(auxiliary_slots),
                    "wall": wall,
                    "generated": generated,
                    "unique": unique,
                    "generated_rate": generated / wall,
                    "unique_rate": unique / wall,
                    "afl_rate": afl_generated / wall,
                    "symcc_rate": symcc_generated / wall,
                    "edges": edges,
                    "round": _integer(raw, "round", default=0, minimum=0),
                    "random_seed": _integer(
                        raw, "random_seed", default=None, minimum=0,
                    ),
                    "wall_budget": wall_budget,
                    "run_id": run_id,
                })
    if not runs:
        count = sum(row["attempted"] for row in attempts.values())
        raise ValueError(
            f"no successful {mode} rows for target {target!r} "
            f"among {count} attempted runs"
        )
    if len(seed_edge_values) > 1:
        raise ValueError("seed edge baselines disagree across input files")
    if len(total_edge_values) > 1:
        raise ValueError("coverage universes disagree across input files")
    seed_edges = next(iter(seed_edge_values), None)
    total_edges = next(iter(total_edge_values), None)
    if (
        seed_edges is not None
        and total_edges is not None
        and seed_edges > total_edges
    ):
        raise ValueError("seed edge baseline exceeds coverage universe")
    allocation_rows = []
    successful_by_allocation: dict[
        tuple[int, int, int, int, int], list[dict[str, Any]]
    ] = {}
    for run in runs:
        successful_by_allocation.setdefault(_allocation_key(run), []).append(run)
    for allocation in sorted(
        attempts.values(), key=lambda row: (row["parallelism"], row["np"]),
    ):
        allocation = dict(allocation)
        successful_runs = successful_by_allocation.get((
            allocation["np"],
            allocation["workers"],
            allocation["masters"],
            allocation["afl_instances"],
            allocation["parallelism"],
        ), [])
        seeds = [
            row["random_seed"] for row in successful_runs
            if row["random_seed"] is not None
        ]
        allocation["success_rate"] = (
            allocation["successful"] / allocation["attempted"]
        )
        allocation["independent_seeds"] = len(set(seeds))
        allocation["missing_random_seeds"] = sum(
            row["random_seed"] is None for row in successful_runs
        )
        allocation["duplicate_seed_runs"] = len(seeds) - len(set(seeds))
        allocation_rows.append(allocation)
    attempted = sum(row["attempted"] for row in allocation_rows)
    successful = sum(row["successful"] for row in allocation_rows)
    reliability = {
        "attempted": attempted,
        "successful": successful,
        "failed": sum(row["failed"] for row in allocation_rows),
        "timed_out": sum(row["timed_out"] for row in allocation_rows),
        "success_rate": successful / attempted,
        "roles_inferred": sum(row["roles_inferred"] for row in allocation_rows),
        "run_ids_inferred": inferred_run_ids,
        "missing_random_seeds": sum(
            row["random_seed"] is None for row in runs
        ),
        "coverage_provenance_missing": coverage_provenance_missing,
        "coverage_provenance_invalid": coverage_provenance_invalid,
        "coverage_sampling_provenance_missing": (
            coverage_sampling_provenance_missing
        ),
        "coverage_sampling_provenance_invalid": (
            coverage_sampling_provenance_invalid
        ),
        "coverage_sampled_rows": coverage_sampled_rows,
        "auxiliary_slots_missing": auxiliary_slots_missing,
        "observed_auxiliary_compute_slots": sorted(auxiliary_slot_values),
        "allocations": allocation_rows,
    }
    return runs, reliability, seed_edges, total_edges


def _summaries(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int, int, int, int], list[dict[str, Any]]] = {}
    for row in runs:
        key = _allocation_key(row)
        grouped.setdefault(key, []).append(row)
    result = []
    for (
        np_value, workers, masters, afl_instances, parallelism,
    ), rows in sorted(grouped.items(), key=lambda item: item[0][-1]):
        generated, generated_low, generated_high = _mean_ci([
            row["generated_rate"] for row in rows])
        unique, unique_low, unique_high = _mean_ci([
            row["unique_rate"] for row in rows])
        edges, edges_low, edges_high = _mean_ci([
            row["edges"] for row in rows])
        afl_rate, _, _ = _mean_ci([row["afl_rate"] for row in rows])
        symcc_rate, _, _ = _mean_ci([row["symcc_rate"] for row in rows])
        result.append({
            "parallelism": parallelism,
            "workers": workers,
            "concolic_workers": workers,
            "masters": masters,
            "afl_instances": afl_instances,
            "auxiliary_compute_slots": int(round(statistics.median(
                row.get("auxiliary_compute_slots", 0.0) for row in rows
            ))),
            "np": np_value,
            "rounds": len(rows),
            "independent_seeds": len({
                row["random_seed"] for row in rows
                if row["random_seed"] is not None
            }),
            "wall_budget_seconds": (
                statistics.fmean(row["wall_budget"] for row in rows)
                if all(row["wall_budget"] is not None for row in rows)
                else None
            ),
            "generated_rate": generated,
            "generated_rate_ci_low": generated_low,
            "generated_rate_ci_high": generated_high,
            "unique_rate": unique,
            "unique_rate_ci_low": unique_low,
            "unique_rate_ci_high": unique_high,
            "afl_rate": afl_rate,
            "symcc_rate": symcc_rate,
            "edges": edges,
            "edges_ci_low": edges_low,
            "edges_ci_high": edges_high,
            "acceptance_ratio": (
                sum(row["unique"] for row in rows)
                / sum(row["generated"] for row in rows)
                if sum(row["generated"] for row in rows) > 0
                else 0.0
            ),
        })
    return result


def _percentile_interval(values: list[float]) -> list[float] | None:
    if not values:
        return None
    ordered = sorted(values)
    last = len(ordered) - 1
    return [ordered[int(0.025 * last)], ordered[int(0.975 * last)]]


def _model_uncertainty(
    runs: list[dict[str, Any]],
    *,
    seed_edges: float | None,
    total_edges: float | None,
    resource_ceiling: int,
    fit_coverage: bool,
    samples: int,
) -> dict[str, Any]:
    grouped: dict[
        tuple[int, int, int, int, int], dict[int, dict[str, Any]]
    ] = {}
    for row in runs:
        seed = row["random_seed"]
        assert seed is not None
        grouped.setdefault(_allocation_key(row), {})[int(seed)] = row
    seed_sets = [set(rows) for rows in grouped.values()]
    common_seeds = sorted(set.intersection(*seed_sets))
    rng = random.Random(20260828)
    scale_values: list[float] = []
    contention_values: list[float] = []
    coherency_values: list[float] = []
    throughput_ceilings: list[float] = []
    coverage_limits: list[float] = []
    coverage_rates: list[float] = []
    coverage_ceilings: list[float] = []
    recommended: list[float] = []
    # Sampling a small set of paired seed blocks with replacement produces
    # many repeated multisets.  Model fitting is deterministic, so cache one
    # fit per count vector while still retaining every bootstrap replicate in
    # the percentile distribution.
    resample_cache: dict[
        tuple[int, ...],
        tuple[object, object | None, object] | None,
    ] = {}
    for _ in range(samples):
        sampled_indexes = [
            rng.randrange(len(common_seeds)) for _item in common_seeds
        ]
        resample_key = tuple(
            sampled_indexes.count(index) for index in range(len(common_seeds))
        )
        cached = resample_cache.get(resample_key, False)
        if cached is False:
            sampled_seeds = [common_seeds[index] for index in sampled_indexes]
            # Each sampled seed contributes one complete cross-allocation
            # block.  This preserves campaign-wide noise shared by all scale
            # levels.
            resampled = [
                rows[seed]
                for seed in sampled_seeds
                for rows in grouped.values()
            ]
            try:
                throughput = fit_usl([
                    ThroughputObservation(
                        int(row["parallelism"]), row["unique_rate"])
                    for row in resampled
                ])
            except ValueError:
                resample_cache[resample_key] = None
                continue
            coverage = None
            if fit_coverage:
                assert seed_edges is not None and total_edges is not None
                try:
                    coverage = fit_coverage_saturation([
                        CoverageObservation(
                            int(row["parallelism"]), row["edges"])
                        for row in resampled
                    ], seed_edges=seed_edges, total_edges=total_edges)
                except ValueError:
                    resample_cache[resample_key] = None
                    continue
            combined = combine_ceilings(
                throughput, coverage, resource_ceiling=resource_ceiling,
            )
            resample_cache[resample_key] = (throughput, coverage, combined)
        elif cached is None:
            continue
        else:
            throughput, coverage, combined = cached
        scale_values.append(throughput.scale)
        contention_values.append(throughput.contention)
        coherency_values.append(throughput.coherency)
        if throughput.doubling_ceiling is not None:
            throughput_ceilings.append(float(throughput.doubling_ceiling))
        if coverage is not None:
            coverage_limits.append(coverage.asymptotic_edges)
            coverage_rates.append(coverage.rate)
            if coverage.edge_gain_ceiling is not None:
                coverage_ceilings.append(float(coverage.edge_gain_ceiling))
        recommended.append(float(combined.recommended_parallelism))
    return {
        "method": "paired cluster bootstrap over complete random-seed blocks",
        "independent_seed_blocks": len(common_seeds),
        "confidence": 0.95,
        "requested_replicates": samples,
        "successful_replicates": len(recommended),
        "unique_resamples_evaluated": len(resample_cache),
        "minimum_successful_replicates": math.ceil(samples * 0.8),
        "unique_usl": {
            "scale_ci95": _percentile_interval(scale_values),
            "contention_ci95": _percentile_interval(contention_values),
            "coherency_ci95": _percentile_interval(coherency_values),
            "doubling_ceiling_ci95": _percentile_interval(
                throughput_ceilings),
        },
        "coverage_saturation": (
            {
                "asymptotic_edges_ci95": _percentile_interval(coverage_limits),
                "rate_ci95": _percentile_interval(coverage_rates),
                "edge_gain_ceiling_ci95": _percentile_interval(
                    coverage_ceilings),
            }
            if fit_coverage else None
        ),
        "recommended_parallelism_ci95": _percentile_interval(recommended),
    }


def _svg(
    path: Path,
    summaries: list[dict[str, float]],
    unique_fit,
    coverage_fit,
) -> None:
    width, height = 1280, 700
    left, right, top, bottom = 86, 54, 72, 90
    gap = 90
    panel_width = (width - left - right - gap) / 2
    panel_height = height - top - bottom
    max_workers = max(row["parallelism"] for row in summaries)
    x_limit = max(2, int(max_workers * 1.15))

    def panel_x(panel: int, n: float) -> float:
        origin = left + panel * (panel_width + gap)
        return origin + panel_width * n / x_limit

    unique_max = max(row["unique_rate_ci_high"] for row in summaries)
    if unique_fit is not None:
        unique_max = max(
            unique_max,
            max(unique_fit.predict(n) for n in range(1, x_limit + 1)),
        )
    unique_max *= 1.12
    edge_min = min(row["edges_ci_low"] for row in summaries)
    edge_max = max(
        max(row["edges_ci_high"] for row in summaries),
        coverage_fit.predict(x_limit) if coverage_fit else edge_min + 1.0,
    )
    edge_pad = max(1.0, (edge_max - edge_min) * 0.12)
    edge_min -= edge_pad
    edge_max += edge_pad

    def y_unique(value: float) -> float:
        return top + panel_height * (1.0 - value / unique_max)

    def y_edges(value: float) -> float:
        return top + panel_height * (1.0 - (value - edge_min) / (edge_max - edge_min))

    chart_title = (
        "Parallel symbolic execution: useful throughput and coverage saturation"
        if coverage_fit
        else "Parallel symbolic execution: useful throughput and endpoint coverage"
    )
    content = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<style>text{font-family:Inter,Arial,sans-serif;fill:#172033}.title{font-size:25px;font-weight:700}.label{font-size:14px}.small{font-size:12px;fill:#526078}.grid{stroke:#d8e0ea;stroke-width:1}.axis{stroke:#536176;stroke-width:1.4}.fit{fill:none;stroke:#006d77;stroke-width:4}.edgefit{fill:none;stroke:#c2410c;stroke-width:4}.point{fill:#0f766e;stroke:white;stroke-width:2}.edgepoint{fill:#ea580c;stroke:white;stroke-width:2}</style>',
        f'<text x="64" y="38" class="title">{chart_title}</text>',
    ]
    for panel in range(2):
        x0 = left + panel * (panel_width + gap)
        content.append(f'<line x1="{x0}" y1="{top + panel_height}" x2="{x0 + panel_width}" y2="{top + panel_height}" class="axis"/>')
        content.append(f'<line x1="{x0}" y1="{top}" x2="{x0}" y2="{top + panel_height}" class="axis"/>')
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = top + panel_height * (1.0 - fraction)
            content.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x0 + panel_width}" y2="{y:.1f}" class="grid"/>')
            label = unique_max * fraction if panel == 0 else edge_min + (edge_max - edge_min) * fraction
            content.append(f'<text x="{x0 - 10}" y="{y + 4:.1f}" text-anchor="end" class="small">{label:.1f}</text>')
        for n in sorted({
            1, *[int(row["parallelism"]) for row in summaries], x_limit,
        }):
            x = panel_x(panel, n)
            content.append(f'<line x1="{x:.1f}" y1="{top + panel_height}" x2="{x:.1f}" y2="{top + panel_height + 6}" class="axis"/>')
            content.append(f'<text x="{x:.1f}" y="{top + panel_height + 24}" text-anchor="middle" class="small">{n}</text>')
        title = "Unique corpus throughput (inputs/s)" if panel == 0 else "Endpoint edge coverage (edges)"
        content.append(f'<text x="{x0 + panel_width / 2:.1f}" y="{top - 18}" text-anchor="middle" class="label">{title}</text>')

    if unique_fit is not None:
        unique_line = " ".join(
            f'{panel_x(0, n):.1f},{y_unique(unique_fit.predict(n)):.1f}'
            for n in range(1, x_limit + 1))
        content.append(f'<polyline points="{unique_line}" class="fit"/>')
    if coverage_fit:
        edge_line = " ".join(
            f'{panel_x(1, n):.1f},{y_edges(coverage_fit.predict(n)):.1f}'
            for n in range(1, x_limit + 1))
        content.append(f'<polyline points="{edge_line}" class="edgefit"/>')
    else:
        content.append(
            f'<text x="{left + panel_width + gap + panel_width / 2:.1f}" '
            f'y="{height - 50}" text-anchor="middle" class="small">'
            "Nonmonotone observations; saturation fit withheld</text>"
        )
    for row in summaries:
        x0 = panel_x(0, row["parallelism"])
        content.append(f'<line x1="{x0:.1f}" y1="{y_unique(row["unique_rate_ci_low"]):.1f}" x2="{x0:.1f}" y2="{y_unique(row["unique_rate_ci_high"]):.1f}" stroke="#0f766e" stroke-width="2"/>')
        content.append(f'<circle cx="{x0:.1f}" cy="{y_unique(row["unique_rate"]):.1f}" r="6" class="point"/>')
        x1 = panel_x(1, row["parallelism"])
        content.append(f'<line x1="{x1:.1f}" y1="{y_edges(row["edges_ci_low"]):.1f}" x2="{x1:.1f}" y2="{y_edges(row["edges_ci_high"]):.1f}" stroke="#ea580c" stroke-width="2"/>')
        content.append(f'<circle cx="{x1:.1f}" cy="{y_edges(row["edges"]):.1f}" r="6" class="edgepoint"/>')
    content.append(f'<text x="{width / 2:.1f}" y="{height - 24}" text-anchor="middle" class="label">Parallelism level; points are round means with bootstrap 95% confidence intervals</text>')
    content.append('</svg>')
    path.write_text("\n".join(content) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", nargs="+", type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--mode", choices=("mpi", "hybrid"), default="mpi")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers-per-master", type=int, default=90)
    parser.add_argument("--physical-cores", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--seed-edges", type=float)
    parser.add_argument("--total-edges", type=float)
    parser.add_argument("--minimum-success-rate", type=float, default=1.0)
    parser.add_argument("--minimum-successful-rounds", type=int, default=3)
    parser.add_argument("--minimum-parallelism-levels", type=int, default=5)
    parser.add_argument("--bootstrap-samples", type=int, default=200)
    parser.add_argument(
        "--maximum-wall-budget-relative-span", type=float, default=0.01,
        help=(
            "maximum (max-min)/median planned wall-budget spread allowed for "
            "a decision-grade endpoint-coverage comparison"
        ),
    )
    args = parser.parse_args()
    if not 0.0 < args.minimum_success_rate <= 1.0:
        parser.error("--minimum-success-rate must be in (0, 1]")
    if args.minimum_successful_rounds < 1:
        parser.error("--minimum-successful-rounds must be positive")
    if args.minimum_parallelism_levels < 4:
        parser.error("--minimum-parallelism-levels must be at least four")
    if not 20 <= args.bootstrap_samples <= 5000:
        parser.error("--bootstrap-samples must be in [20, 5000]")
    if not 0.0 <= args.maximum_wall_budget_relative_span <= 0.10:
        parser.error("--maximum-wall-budget-relative-span must be in [0, 0.10]")
    if args.workers_per_master < 1:
        parser.error("--workers-per-master must be positive")
    if args.physical_cores < 1:
        parser.error("--physical-cores must be positive")
    for name, value in (
        ("--seed-edges", args.seed_edges),
        ("--total-edges", args.total_edges),
    ):
        if value is not None and (
            not math.isfinite(value)
            or not value.is_integer()
            or value < 0.0
            or (name == "--total-edges" and value == 0.0)
        ):
            parser.error(
                f"{name} must be a finite "
                f"{'positive' if name == '--total-edges' else 'non-negative'} "
                "integer"
            )
    if (
        args.seed_edges is not None
        and args.total_edges is not None
        and args.seed_edges > args.total_edges
    ):
        parser.error("--seed-edges cannot exceed --total-edges")

    runs, reliability, discovered_seed, discovered_total = _load_rows(
        args.csv, args.target, args.mode, args.workers_per_master)
    if (
        args.seed_edges is not None
        and discovered_seed is not None
        and args.seed_edges != discovered_seed
    ):
        parser.error("--seed-edges conflicts with the CSV seed baseline")
    if (
        args.total_edges is not None
        and discovered_total is not None
        and args.total_edges != discovered_total
    ):
        parser.error("--total-edges conflicts with the CSV coverage universe")
    summaries = _summaries(runs)
    decision_reasons = []
    if reliability["success_rate"] < args.minimum_success_rate:
        decision_reasons.append(
            f"success rate {reliability['success_rate']:.3f} is below "
            f"{args.minimum_success_rate:.3f}"
        )
    under_repeated = [
        row for row in reliability["allocations"]
        if row["independent_seeds"] < args.minimum_successful_rounds
    ]
    if under_repeated:
        decision_reasons.append(
            f"{len(under_repeated)} allocation(s) have fewer than "
            f"{args.minimum_successful_rounds} independent random seeds"
        )
    duplicate_seed_allocations = [
        row for row in reliability["allocations"]
        if row["duplicate_seed_runs"]
    ]
    if duplicate_seed_allocations:
        decision_reasons.append(
            f"{len(duplicate_seed_allocations)} allocation(s) reuse a random "
            "seed; repeated executions are technical replicates, not "
            "independent evidence"
        )
    if reliability["missing_random_seeds"]:
        decision_reasons.append(
            f"random_seed is missing for "
            f"{reliability['missing_random_seeds']} successful run(s)"
        )
    allocation_seed_sets: dict[
        tuple[int, int, int, int, int], set[int]
    ] = {}
    for row in runs:
        seeds = allocation_seed_sets.setdefault(_allocation_key(row), set())
        if row["random_seed"] is not None:
            seeds.add(int(row["random_seed"]))
    seed_sets = list(allocation_seed_sets.values())
    common_seeds = set.intersection(*seed_sets) if seed_sets else set()
    if seed_sets and any(seeds != common_seeds for seeds in seed_sets):
        decision_reasons.append(
            "random-seed blocks are incomplete across resource allocations"
        )
    reliability["complete_random_seed_blocks"] = len(common_seeds)
    reliability["paired_seed_block_complete"] = bool(
        seed_sets and all(seeds == common_seeds for seeds in seed_sets)
    )
    levels = len({int(row["parallelism"]) for row in runs})
    if levels < args.minimum_parallelism_levels:
        decision_reasons.append(
            f"only {levels} parallelism levels; "
            f"{args.minimum_parallelism_levels} required"
        )
    if reliability["roles_inferred"]:
        decision_reasons.append(
            f"role assignments were inferred for "
            f"{reliability['roles_inferred']} run(s)"
        )
    if reliability["run_ids_inferred"]:
        decision_reasons.append(
            f"stable run_id is missing for "
            f"{reliability['run_ids_inferred']} attempted run(s)"
        )
    if reliability["coverage_provenance_missing"]:
        decision_reasons.append(
            "coverage measurement provenance is missing for "
            f"{reliability['coverage_provenance_missing']} row(s)"
        )
    if reliability["coverage_provenance_invalid"]:
        decision_reasons.append(
            "existing-edge coverage measurement is invalid for "
            f"{reliability['coverage_provenance_invalid']} row(s)"
        )
    if reliability["coverage_sampling_provenance_missing"]:
        decision_reasons.append(
            "coverage sampling provenance is missing for "
            f"{reliability['coverage_sampling_provenance_missing']} row(s)"
        )
    if reliability["coverage_sampling_provenance_invalid"]:
        decision_reasons.append(
            "coverage sampling provenance is invalid for "
            f"{reliability['coverage_sampling_provenance_invalid']} row(s)"
        )
    if reliability["coverage_sampled_rows"]:
        decision_reasons.append(
            "endpoint coverage was corpus-sampled for "
            f"{reliability['coverage_sampled_rows']} row(s)"
        )
    auxiliary_slot_values = set(
        reliability["observed_auxiliary_compute_slots"]
    )
    if reliability["auxiliary_slots_missing"]:
        decision_reasons.append(
            "auxiliary_compute_slots is missing for "
            f"{reliability['auxiliary_slots_missing']} hybrid attempt(s)"
        )
    if len(auxiliary_slot_values) != 1:
        decision_reasons.append(
            "auxiliary compute-slot allocation changes across scale levels"
        )
    reserved_auxiliary_slots = max(auxiliary_slot_values, default=0)
    wall_budgets = [
        row["wall_budget"] for row in runs if row["wall_budget"] is not None
    ]
    missing_wall_budgets = len(runs) - len(wall_budgets)
    wall_budget_relative_span = None
    if missing_wall_budgets:
        decision_reasons.append(
            f"wall_budget_seconds is missing for {missing_wall_budgets} "
            "successful run(s)"
        )
    elif wall_budgets:
        median_wall_budget = statistics.median(wall_budgets)
        wall_budget_relative_span = (
            max(wall_budgets) - min(wall_budgets)
        ) / median_wall_budget
        if wall_budget_relative_span > args.maximum_wall_budget_relative_span:
            decision_reasons.append(
                "planned wall budgets differ by "
                f"{100 * wall_budget_relative_span:.2f}% across runs; "
                "endpoint coverage requires equal exposure"
            )
    reliability["wall_budget_relative_span"] = wall_budget_relative_span
    reliability["maximum_wall_budget_relative_span"] = (
        args.maximum_wall_budget_relative_span
    )
    reliability["wall_budget_complete"] = not missing_wall_budgets
    model_axis_counts: dict[int, set[tuple[int, int, int, int, int]]] = {}
    for row in runs:
        model_axis_counts.setdefault(
            int(row["parallelism"]), set(),
        ).add(_allocation_key(row))
    if any(len(allocations) != 1 for allocations in model_axis_counts.values()):
        decision_reasons.append(
            "multiple resource allocations share one modeled parallelism level"
        )
    if args.mode == "hybrid":
        hybrid_ratios = set()
        for row in runs:
            workers = int(row["workers"])
            afl_instances = int(row["afl_instances"])
            divisor = math.gcd(workers, afl_instances)
            hybrid_ratios.add((workers // divisor, afl_instances // divisor))
        if len(hybrid_ratios) != 1:
            decision_reasons.append(
                "hybrid AFL/SymCC allocation ratio changes across scale levels"
            )
        if len({int(row["masters"]) for row in runs}) != 1:
            decision_reasons.append(
                "hybrid coordinator count changes across scale levels"
            )
        reliability["hybrid_allocation_ratios"] = [
            list(ratio) for ratio in sorted(hybrid_ratios)
        ]
    reliable_for_fitting = (
        reliability["success_rate"] >= args.minimum_success_rate
    )
    reliability_fit_error = (
        "withheld: " + decision_reasons[0]
        if not reliable_for_fitting else ""
    )
    unique_fit = None
    unique_fit_error = ""
    if reliable_for_fitting:
        try:
            unique_fit = fit_usl([
                ThroughputObservation(
                    int(row["parallelism"]), row["unique_rate"])
                for row in runs
            ])
        except ValueError as error:
            unique_fit_error = str(error)
    else:
        unique_fit_error = reliability_fit_error
    generated_fit = None
    generated_fit_error = ""
    if args.mode == "hybrid":
        generated_fit_error = (
            "withheld: hybrid total work mixes AFL executions and concolic "
            "candidates; use the component fits"
        )
    elif reliable_for_fitting:
        try:
            generated_fit = fit_usl([
                ThroughputObservation(
                    int(row["parallelism"]), row["generated_rate"])
                for row in runs
            ])
        except ValueError as error:
            generated_fit_error = str(error)
    else:
        generated_fit_error = reliability_fit_error

    def component_fit(
        parallelism_key: str, rate_key: str,
    ) -> tuple[object | None, str]:
        observations = [
            ThroughputObservation(
                int(row[parallelism_key]), row[rate_key])
            for row in runs
            if row[parallelism_key] >= 1.0 and row[rate_key] > 0.0
        ]
        if not reliable_for_fitting:
            return None, reliability_fit_error
        try:
            return fit_usl(observations), ""
        except ValueError as error:
            return None, str(error)

    symcc_fit, symcc_fit_error = component_fit("workers", "symcc_rate")
    afl_fit, afl_fit_error = component_fit("afl_instances", "afl_rate")
    seed_edges = args.seed_edges if args.seed_edges is not None else discovered_seed
    total_edges = args.total_edges if args.total_edges is not None else discovered_total
    if seed_edges is not None and any(row["edges"] < seed_edges for row in runs):
        parser.error("a successful run reports fewer edges than the seed baseline")
    coverage_fit = None
    coverage_fit_error = ""
    if (
        reliable_for_fitting
        and seed_edges is not None
        and total_edges is not None
        and seed_edges < total_edges
    ):
        try:
            coverage_fit = fit_coverage_saturation([
                CoverageObservation(int(row["parallelism"]), row["edges"])
                for row in runs
            ], seed_edges=seed_edges, total_edges=total_edges)
        except ValueError as error:
            coverage_fit_error = str(error)
    elif not reliable_for_fitting:
        coverage_fit_error = reliability_fit_error
    elif seed_edges is None or total_edges is None:
        coverage_fit_error = "seed baseline and coverage universe are required"
    elif seed_edges >= total_edges:
        coverage_fit_error = (
            "seed corpus already covers the declared coverage universe"
        )
    if coverage_fit is None:
        decision_reasons.append(
            "coverage-saturation model is unavailable: "
            + (coverage_fit_error or "insufficient coverage evidence")
        )
    available_role_cores = args.physical_cores - reserved_auxiliary_slots
    if args.mode == "mpi":
        if available_role_cores < 2:
            decision_reasons.append(
                "physical-core budget cannot host one MPI coordinator and "
                "one compute worker after auxiliary reservations"
            )
        resource_ceiling = _worker_count(
            max(1, available_role_cores),
            args.mode,
            args.workers_per_master,
        )
        resource_basis = (
            "compute_roles(physical_cores - auxiliary_compute_slots, "
            "workers_per_master)"
        )
    else:
        # Hybrid model parallelism already includes both AFL and concolic
        # compute roles. Coordinators and their declared background compute
        # pools are outside that axis.
        observed_masters = {int(row["masters"]) for row in runs}
        reserved = max(0, int(round(statistics.median(observed_masters))))
        if available_role_cores <= reserved:
            decision_reasons.append(
                "physical-core budget cannot host the hybrid coordinators "
                "and one compute worker after auxiliary reservations"
            )
        resource_ceiling = max(
            1, args.physical_cores - reserved - reserved_auxiliary_slots
        )
        resource_basis = (
            "physical_cores minus observed coordinator and auxiliary slots"
            if len(observed_masters) == 1
            else "physical_cores minus exploratory coordinator and auxiliary slots"
        )
    if resource_ceiling < max(1, max(
        int(row["parallelism"]) for row in runs
    )):
        decision_reasons.append(
            "declared physical-core budget cannot host every observed compute "
            "allocation after coordinator and auxiliary reservations"
        )
    exploratory_ceiling = combine_ceilings(
        unique_fit, coverage_fit, resource_ceiling=resource_ceiling)
    if unique_fit is None:
        decision_reasons.append(
            "useful-throughput model is unavailable"
        )
    model_uncertainty = None
    if not decision_reasons:
        model_uncertainty = _model_uncertainty(
            runs,
            seed_edges=seed_edges,
            total_edges=total_edges,
            resource_ceiling=resource_ceiling,
            fit_coverage=coverage_fit is not None,
            samples=args.bootstrap_samples,
        )
        if (
            model_uncertainty["successful_replicates"]
            < model_uncertainty["minimum_successful_replicates"]
        ):
            decision_reasons.append(
                "fewer than 80% of bootstrap replicates produced valid models"
            )
    decision_reasons = list(dict.fromkeys(decision_reasons))
    decision_eligible = not decision_reasons
    ceiling = exploratory_ceiling.to_dict()
    ceiling["decision_eligible"] = decision_eligible
    ceiling["decision_reasons"] = decision_reasons
    ceiling["exploratory_recommended_parallelism"] = (
        exploratory_ceiling.recommended_parallelism
    )
    if not decision_eligible:
        ceiling["recommended_parallelism"] = None
        ceiling["limiting_factors"] = ["evidence-quality"]

    reliability.update({
        "minimum_success_rate": args.minimum_success_rate,
        "minimum_successful_rounds": args.minimum_successful_rounds,
        "minimum_parallelism_levels": args.minimum_parallelism_levels,
        "decision_eligible": decision_eligible,
        "decision_reasons": decision_reasons,
    })

    args.output.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "symcc-parallel-scale-analysis-v6",
        "target": args.target,
        "mode": args.mode,
        "input_csv": [str(path.resolve()) for path in args.csv],
        "parallel_unit": (
            "concolic-workers"
            if args.mode == "mpi"
            else "compute-workers(concolic+afl)"
            if args.mode == "hybrid"
            else "total-np"
        ),
        "allocation_dimensions": {
            "concolic": "num_workers",
            "afl": "afl_instances",
            "coordinator": "num_masters",
            "auxiliary": "auxiliary_compute_slots",
        },
        "hybrid_activity_rate_note": (
            "generated_rate is retained for backward compatibility but mixes "
            "AFL executions with concolic candidates and is not fitted"
            if args.mode == "hybrid" else ""
        ),
        "reliability": reliability,
        "summaries": summaries,
        "unique_throughput_usl": unique_fit.to_dict() if unique_fit else None,
        "unique_throughput_fit_error": unique_fit_error,
        "generated_throughput_usl": (
            generated_fit.to_dict() if generated_fit else None
        ),
        "generated_throughput_fit_error": generated_fit_error,
        "symcc_component_usl": symcc_fit.to_dict() if symcc_fit else None,
        "symcc_component_fit_error": symcc_fit_error,
        "afl_component_usl": afl_fit.to_dict() if afl_fit else None,
        "afl_component_fit_error": afl_fit_error,
        "coverage_saturation": coverage_fit.to_dict() if coverage_fit else None,
        "coverage_fit_error": coverage_fit_error,
        "model_uncertainty": model_uncertainty,
        "resource_ceiling_basis": resource_basis,
        "reserved_auxiliary_compute_slots": reserved_auxiliary_slots,
        "ceiling": ceiling,
    }
    (args.output / "parallel_scale_model.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.output / "parallel_scale_summary.csv").open(
            "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    _svg(args.output / "parallel_scale_model.svg", summaries, unique_fit, coverage_fit)

    lines = [
        f"# Parallel scale analysis: {args.target}",
        "",
        "| compute workers | concolic | AFL | auxiliary | rounds | total work/s | AFL exec/s | SymCC cand/s | "
        "unique/s | retention | edges |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            f"| {row['parallelism']} | {row['concolic_workers']} | "
            f"{row['afl_instances']} | {row['auxiliary_compute_slots']} | "
            f"{row['rounds']} | "
            f"{row['generated_rate']:.2f} | {row['afl_rate']:.2f} | "
            f"{row['symcc_rate']:.2f} | {row['unique_rate']:.2f} | "
            f"{100 * row['acceptance_ratio']:.3f}% | {row['edges']:.1f} |")
    lines += [
        "",
        "## Evidence quality",
        "",
        f"- attempted={reliability['attempted']}, "
        f"successful={reliability['successful']}, "
        f"failed={reliability['failed']}, "
        f"timed-out={reliability['timed_out']}, "
        f"success-rate={reliability['success_rate']:.3f}",
        f"- decision eligible: **{str(decision_eligible).lower()}**",
    ]
    if decision_reasons:
        lines.append("- withholding reasons: " + "; ".join(decision_reasons))
    lines += [
        "",
        "## Model",
        "",
        "`C(N) = gamma*N / (1 + sigma*(N-1) + kappa*N*(N-1))`",
        "",
    ]
    if unique_fit:
        lines.append(
            f"- useful-throughput USL: gamma={unique_fit.scale:.4g}, "
            f"sigma={unique_fit.contention:.4g}, "
            f"kappa={unique_fit.coherency:.4g}, "
            f"R2={unique_fit.r_squared:.4f}"
        )
    else:
        lines.append(
            f"- useful-throughput USL withheld: {unique_fit_error}"
        )
    if generated_fit:
        lines.append(
            f"- raw-throughput USL: gamma={generated_fit.scale:.4g}, "
            f"sigma={generated_fit.contention:.4g}, "
            f"kappa={generated_fit.coherency:.4g}, "
            f"R2={generated_fit.r_squared:.4f}"
        )
    else:
        lines.append(f"- raw-throughput USL withheld: {generated_fit_error}")
    if symcc_fit:
        lines.append(
            f"- SymCC component USL (concolic-worker axis): "
            f"R2={symcc_fit.r_squared:.4f}"
        )
    elif symcc_fit_error:
        lines.append(
            f"- SymCC component USL withheld: {symcc_fit_error}")
    if afl_fit:
        lines.append(
            f"- AFL component USL (AFL-instance axis): "
            f"R2={afl_fit.r_squared:.4f}"
        )
    elif afl_fit_error and args.mode == "hybrid":
        lines.append(f"- AFL component USL withheld: {afl_fit_error}")
    if coverage_fit:
        lines.append(
            f"- coverage saturation: asymptote={coverage_fit.asymptotic_edges:.1f} "
            f"edges, rho={coverage_fit.rate:.4g}, R2={coverage_fit.r_squared:.4f}")
    elif coverage_fit_error:
        lines.append(
            "- coverage saturation: not fitted because the observations "
            f"violate the monotone model ({coverage_fit_error})")
    if decision_eligible:
        lines.append(
            f"- recommended ceiling: **{ceiling['recommended_parallelism']}** "
            f"({', '.join(ceiling['limiting_factors'])})"
        )
        if model_uncertainty is not None:
            interval = model_uncertainty["recommended_parallelism_ci95"]
            lines.append(
                f"- bootstrap 95% ceiling interval: "
                f"**{interval[0]:.0f}..{interval[1]:.0f}** "
                f"({model_uncertainty['successful_replicates']}/"
                f"{model_uncertainty['requested_replicates']} valid resamples)"
            )
    else:
        lines.append(
            "- recommended ceiling: **withheld**; exploratory point estimate="
            f"{ceiling['exploratory_recommended_parallelism']}"
        )
    lines += [
        "",
        "The estimate is workload- and budget-specific. Refit it after changing "
        "the target, seed corpus, campaign duration, scheduler, or machine.",
    ]
    (args.output / "parallel_scale_report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(ceiling, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
