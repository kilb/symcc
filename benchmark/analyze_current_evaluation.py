#!/usr/bin/env python3
"""Analyze repeated current-vs-legacy SymCC benchmark campaigns.

The generic ablation analyzer groups by the benchmark's internal ``np`` field.
That is useful for ordinary scaling experiments, but it cannot directly compare
the hybrid ``np=8`` rows with the single-instance AFL sanity baseline.  This
script keeps the campaign label as an explicit dimension.  The engineering
campaign did not randomize or block treatment runs, so comparisons treat runs
as independent samples instead of manufacturing pairs from repeat indices.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from pathlib import Path
from statistics import mean, median, stdev
from typing import Iterable, Mapping

from analyze_ablation import (
    bootstrap_ci,
    holm_adjust,
    vargha_delaney,
)


DEFAULT_METRICS = (
    "edge_cov_pct",
    "afl_bitmap_cvg",
    "unique",
    "afl_execs_done",
)


def parse_number(value: object) -> float | None:
    text = str(value or "").strip().removesuffix("%")
    if not text:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def load_groups(specifications: Iterable[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for specification in specifications:
        if "=" not in specification:
            raise ValueError(f"group must be NAME=CSV, got {specification!r}")
        group, filename = specification.split("=", 1)
        group = group.strip()
        if not group:
            raise ValueError("group name cannot be empty")
        with Path(filename).open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                copied = dict(row)
                copied["campaign"] = group
                copied["configuration_key"] = (
                    f"{group}/{copied.get('mode', '')}"
                )
                rows.append(copied)
    return rows


def successful(row: Mapping[str, str]) -> bool:
    return str(row.get("status", "")).strip().lower() in {
        "", "ok", "success", "timeout",
    }


def select_values(
    rows: Iterable[Mapping[str, str]],
    configuration: str,
    target: str,
    metric: str,
) -> dict[int, float]:
    selected: dict[int, float] = {}
    for row in rows:
        if (
            row.get("configuration_key") != configuration
            or row.get("target") != target
            or not successful(row)
        ):
            continue
        value = parse_number(row.get(metric))
        if value is None:
            continue
        selected[int(row.get("round", "0") or 0)] = value
    return selected


def summarize(
    rows: list[dict[str, str]], metrics: Iterable[str]
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    keys = sorted({
        (row["configuration_key"], row.get("target", ""))
        for row in rows
        if row.get("mode") in {"hybrid", "afl-only"}
    })
    for metric in metrics:
        for configuration, target in keys:
            values_by_round = select_values(
                rows, configuration, target, metric
            )
            values = list(values_by_round.values())
            if not values:
                continue
            ci_low, ci_high = bootstrap_ci(values, samples=10000)
            matching_rows = [
                row for row in rows
                if row["configuration_key"] == configuration
                and row.get("target") == target
                and row.get("mode") in {"hybrid", "afl-only"}
            ]
            output.append({
                "configuration": configuration,
                "target": target,
                "metric": metric,
                "n_total": len(matching_rows),
                "n": len(values),
                "failures": sum(not successful(row) for row in matching_rows),
                "mean": mean(values),
                "standard_deviation": stdev(values) if len(values) > 1 else 0.0,
                "median": median(values),
                "median_ci95_low": ci_low,
                "median_ci95_high": ci_high,
                "min": min(values),
                "q1": percentile(values, 0.25),
                "q3": percentile(values, 0.75),
                "max": max(values),
            })
    return output


def independent_bootstrap_ci(
    treatment: list[float],
    baseline: list[float],
    *,
    samples: int = 10000,
    confidence: float = 0.95,
    seed: int = 7331,
) -> tuple[float, float]:
    """Bootstrap an independent-sample mean difference."""
    if not treatment or not baseline:
        return (0.0, 0.0)
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(max(100, int(samples))):
        treatment_draw = [
            treatment[rng.randrange(len(treatment))] for _ in treatment
        ]
        baseline_draw = [
            baseline[rng.randrange(len(baseline))] for _ in baseline
        ]
        estimates.append(mean(treatment_draw) - mean(baseline_draw))
    estimates.sort()
    alpha = (1.0 - confidence) / 2.0
    low = estimates[int(alpha * (len(estimates) - 1))]
    high = estimates[int((1.0 - alpha) * (len(estimates) - 1))]
    return (low, high)


def independent_randomization_p_value(
    treatment: list[float],
    baseline: list[float],
    *,
    samples: int = 100000,
    seed: int = 20260730,
) -> float:
    """Two-sided label-permutation test for an independent mean difference."""
    if not treatment or not baseline:
        return 1.0
    observed = abs(mean(treatment) - mean(baseline))
    combined = list(treatment) + list(baseline)
    treatment_size = len(treatment)
    rng = random.Random(seed)
    count = 0
    total = max(1000, int(samples))
    for _ in range(total):
        rng.shuffle(combined)
        estimate = abs(
            mean(combined[:treatment_size])
            - mean(combined[treatment_size:])
        )
        count += estimate >= observed - 1e-15
    return (count + 1) / (total + 1)


def compare(
    rows: list[dict[str, str]],
    metrics: Iterable[str],
    comparisons: Iterable[str],
) -> list[dict[str, object]]:
    targets = sorted({
        row.get("target", "") for row in rows
        if row.get("mode") in {"hybrid", "afl-only"}
    })
    output: list[dict[str, object]] = []
    raw_p_values: list[float] = []
    for comparison in comparisons:
        if "," not in comparison:
            raise ValueError(
                "comparison must be TREATMENT,BASELINE configuration keys"
            )
        treatment, baseline = (
            item.strip() for item in comparison.split(",", 1)
        )
        for metric in metrics:
            for target in targets:
                treatment_values = select_values(
                    rows, treatment, target, metric
                )
                baseline_values = select_values(
                    rows, baseline, target, metric
                )
                treatment_sample = list(treatment_values.values())
                baseline_sample = list(baseline_values.values())
                if not treatment_sample or not baseline_sample:
                    continue
                delta_low, delta_high = independent_bootstrap_ci(
                    treatment_sample, baseline_sample, samples=10000
                )
                p_value = independent_randomization_p_value(
                    treatment_sample, baseline_sample, samples=100000
                )
                a12 = vargha_delaney(
                    treatment_sample, baseline_sample
                )
                output.append({
                    "treatment": treatment,
                    "baseline": baseline,
                    "target": target,
                    "metric": metric,
                    "treatment_n": len(treatment_sample),
                    "baseline_n": len(baseline_sample),
                    "treatment_mean": mean(treatment_sample),
                    "baseline_mean": mean(baseline_sample),
                    "mean_delta": (
                        mean(treatment_sample) - mean(baseline_sample)
                    ),
                    "delta_ci95_low": delta_low,
                    "delta_ci95_high": delta_high,
                    "relative_mean_change_pct": (
                        100.0
                        * (mean(treatment_sample) - mean(baseline_sample))
                        / abs(mean(baseline_sample))
                        if mean(baseline_sample) else 0.0
                    ),
                    "a12": a12,
                    "cliffs_delta": 2.0 * a12 - 1.0,
                    "randomization_p": p_value,
                    "holm_p": 1.0,
                })
                raw_p_values.append(p_value)
    for result, adjusted in zip(output, holm_adjust(raw_p_values)):
        result["holm_p"] = adjusted
    return output


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def xml_escape(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_svg(rows: list[dict[str, str]], output: Path) -> None:
    """Render compact box/point plots without a plotting dependency."""
    targets = sorted({
        row.get("target", "") for row in rows
        if row.get("mode") in {"hybrid", "afl-only"}
    })
    configurations = [
        item for item in (
            "legacy/afl-only",
            "legacy/hybrid",
            "current/afl-only",
            "current/hybrid",
        )
        if any(row["configuration_key"] == item for row in rows)
    ]
    colors = {
        "legacy/afl-only": "#6b7280",
        "legacy/hybrid": "#c2410c",
        "current/afl-only": "#15803d",
        "current/hybrid": "#0369a1",
    }
    width = 1120
    panel_height = 360
    height = 95 + panel_height * len(targets)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<style>text{font-family:Arial,sans-serif;fill:#17202a;'
        'letter-spacing:0}.title{font-size:24px;font-weight:700}'
        '.label{font-size:15px}.small{font-size:13px;fill:#4b5563}'
        '.grid{stroke:#d1d5db;stroke-width:1}</style>',
        '<text x="72" y="42" class="title">'
        'Repeated 15 s edge coverage distributions (n=20)</text>',
    ]
    legend_x = 72
    for configuration in configurations:
        parts.append(
            f'<rect x="{legend_x}" y="57" width="14" height="14" '
            f'fill="{colors[configuration]}"/>'
            f'<text x="{legend_x + 20}" y="69" class="small">'
            f'{xml_escape(configuration)}</text>'
        )
        legend_x += 210

    for panel, target in enumerate(targets):
        top = 95 + panel * panel_height
        left = 78
        right = width - 38
        plot_top = top + 48
        plot_bottom = top + panel_height - 62
        samples = {
            configuration: list(select_values(
                rows, configuration, target, "edge_cov_pct"
            ).values())
            for configuration in configurations
        }
        all_values = [
            value for values in samples.values() for value in values
        ]
        low = math.floor((min(all_values) - 0.5) * 2.0) / 2.0
        high = math.ceil((max(all_values) + 0.5) * 2.0) / 2.0
        if high <= low:
            high = low + 1.0

        def y(value: float) -> float:
            return plot_bottom - (
                (value - low) / (high - low)
            ) * (plot_bottom - plot_top)

        parts.append(
            f'<text x="{left}" y="{top + 25}" class="label" '
            f'font-weight="700">{xml_escape(target)}</text>'
        )
        tick = math.ceil(low * 2.0) / 2.0
        while tick <= high + 1e-9:
            yy = y(tick)
            parts.append(
                f'<line x1="{left}" y1="{yy:.1f}" x2="{right}" '
                f'y2="{yy:.1f}" class="grid"/>'
                f'<text x="{left - 10}" y="{yy + 5:.1f}" '
                f'text-anchor="end" class="small">{tick:.1f}%</text>'
            )
            tick += 0.5
        column_width = (right - left) / max(1, len(configurations))
        for index, configuration in enumerate(configurations):
            values = samples[configuration]
            if not values:
                continue
            center = left + column_width * (index + 0.5)
            q1 = percentile(values, 0.25)
            q3 = percentile(values, 0.75)
            med = median(values)
            color = colors[configuration]
            parts.extend([
                f'<line x1="{center:.1f}" y1="{y(min(values)):.1f}" '
                f'x2="{center:.1f}" y2="{y(max(values)):.1f}" '
                f'stroke="{color}" stroke-width="2"/>',
                f'<rect x="{center - 36:.1f}" y="{y(q3):.1f}" '
                f'width="72" height="{max(2.0, y(q1) - y(q3)):.1f}" '
                f'fill="{color}" fill-opacity="0.18" stroke="{color}" '
                f'stroke-width="2"/>',
                f'<line x1="{center - 36:.1f}" y1="{y(med):.1f}" '
                f'x2="{center + 36:.1f}" y2="{y(med):.1f}" '
                f'stroke="{color}" stroke-width="3"/>',
            ])
            rng = random.Random(f"{target}:{configuration}")
            for value in values:
                jitter = rng.uniform(-26.0, 26.0)
                parts.append(
                    f'<circle cx="{center + jitter:.1f}" '
                    f'cy="{y(value):.1f}" r="3.2" fill="{color}" '
                    f'fill-opacity="0.68"/>'
                )
            parts.append(
                f'<text x="{center:.1f}" y="{plot_bottom + 27}" '
                f'text-anchor="middle" class="small">'
                f'{xml_escape(configuration)}</text>'
            )
        parts.append(
            f'<text x="{left}" y="{plot_bottom + 52}" class="small">'
            'Points are individual runs; boxes show IQR and median.</text>'
        )
    parts.append("</svg>\n")
    output.write_text("".join(parts), encoding="utf-8")


def write_markdown(
    path: Path,
    summaries: list[dict[str, object]],
    comparisons: list[dict[str, object]],
) -> None:
    with path.open("w", encoding="utf-8") as stream:
        stream.write("# Current Evaluation Statistical Summary\n\n")
        stream.write(
            "All rows retain the original campaign result. Location is the "
            "median; intervals are percentile bootstrap intervals. "
            "Because the campaign did not use randomized blocks, comparisons "
            "treat runs as independent samples and use a two-sided label-"
            "permutation test with Holm correction.\n\n"
        )
        stream.write(
            "| configuration | target | metric | n | mean | median "
            "| median 95% CI | sd | range |\n"
        )
        stream.write(
            "|---|---|---|---:|---:|---:|---:|---:|---:|\n"
        )
        for row in summaries:
            stream.write(
                f"| {row['configuration']} | {row['target']} | "
                f"{row['metric']} | {row['n']} | "
                f"{float(row['mean']):.3f} | "
                f"{float(row['median']):.3f} | "
                f"[{float(row['median_ci95_low']):.3f}, "
                f"{float(row['median_ci95_high']):.3f}] | "
                f"{float(row['standard_deviation']):.3f} | "
                f"[{float(row['min']):.3f}, {float(row['max']):.3f}] |\n"
            )
        stream.write("\n## Independent-sample comparisons\n\n")
        stream.write(
            "| treatment vs baseline | target | metric | n / n | "
            "mean delta | delta 95% CI | mean change | A12 | "
            "Cliff delta | Holm p |\n"
        )
        stream.write(
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|\n"
        )
        for row in comparisons:
            stream.write(
                f"| {row['treatment']} vs {row['baseline']} | "
                f"{row['target']} | {row['metric']} | "
                f"{row['treatment_n']} / {row['baseline_n']} | "
                f"{float(row['mean_delta']):+.3f} | "
                f"[{float(row['delta_ci95_low']):+.3f}, "
                f"{float(row['delta_ci95_high']):+.3f}] | "
                f"{float(row['relative_mean_change_pct']):+.1f}% | "
                f"{float(row['a12']):.3f} | "
                f"{float(row['cliffs_delta']):+.3f} | "
                f"{float(row['holm_p']):.4g} |\n"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--group", action="append", required=True, metavar="NAME=CSV"
    )
    parser.add_argument(
        "--metric", action="append", default=None
    )
    parser.add_argument(
        "--comparison", action="append", default=[]
    )
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    rows = load_groups(args.group)
    metrics = tuple(args.metric or DEFAULT_METRICS)
    summaries = summarize(rows, metrics)
    comparisons = compare(rows, metrics, args.comparison)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "descriptive_statistics.csv", summaries)
    write_csv(output / "independent_comparisons.csv", comparisons)
    write_markdown(
        output / "statistical_summary.md", summaries, comparisons
    )
    render_svg(rows, output / "edge_coverage_distributions.svg")
    with (output / "statistical_results.json").open(
        "w", encoding="utf-8"
    ) as stream:
        json.dump({
            "schema": "symcc-current-evaluation-statistics-v2",
            "methods": {
                "pairing": "none; engineering runs are independent samples",
                "location": "mean difference",
                "interval": "percentile bootstrap, 10000 samples",
                "test": "two-sided independent label permutation",
                "multiplicity": "Holm correction over emitted comparisons",
                "effect_sizes": ["Vargha-Delaney A12", "Cliff's delta"],
            },
            "summaries": summaries,
            "comparisons": comparisons,
        }, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(f"Wrote {len(summaries)} summaries and "
          f"{len(comparisons)} comparisons to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
