#!/usr/bin/env python3
"""Aggregate hybrid MPI master/worker profiles across repeated campaigns."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import re
import statistics
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from util.parallel_scale_model import ThroughputObservation, fit_usl  # noqa: E402


WORKER_PHASES = (
    "wait",
    "bmsync",
    "import",
    "exec",
    "showmap_dedup",
    "send",
)
MASTER_PROFILE_RE = re.compile(
    r"\[PROF\] scan=(?P<scan>[0-9.]+)s/(?P<scan_calls>\d+)x "
    r"dispatch=(?P<dispatch>[0-9.]+)s/(?P<dispatch_calls>\d+)x "
    r"recv=(?P<recv>[0-9.]+)s/(?P<recv_calls>\d+)x\([^)]*\) "
    r"triage=(?P<triage>[0-9.]+)s/(?P<triage_calls>\d+)x "
    r"idle=(?P<idle>[0-9.]+)s"
)
MASTER_TRIAGE_RE = re.compile(r"\[PROF-TRIAGE\] (?P<body>.*)")
PROFILE_DETAIL_RE = re.compile(r"(?P<key>[A-Za-z0-9_.]+)=(?P<value>[0-9.]+)s")


def _float(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, "0") or 0)
    except ValueError:
        return 0.0


def _hybrid_result(run_dir: Path) -> dict[str, str]:
    with (run_dir / "benchmark_data.csv").open(
            newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row.get("mode") == "hybrid" and row.get("status") == "success":
                return row
    raise ValueError(f"no successful hybrid row in {run_dir}")


def _worker_profile(profile_dir: Path) -> tuple[int, int, dict[str, float]]:
    totals = {phase: 0.0 for phase in WORKER_PHASES}
    workers = 0
    items = 0
    for path in sorted(profile_dir.glob("phase_timing_rank*.csv")):
        fields = path.read_text(encoding="utf-8").strip().split(",")
        if len(fields) < 2 + len(WORKER_PHASES):
            continue
        workers += 1
        items += int(fields[1])
        for index, phase in enumerate(WORKER_PHASES):
            totals[phase] += float(fields[2 + index])
    if workers == 0:
        raise ValueError(f"no worker phase profiles in {profile_dir}")
    return workers, items, totals


def _redundancy(profile_dir: Path) -> dict[str, int]:
    result = {
        "generated": 0,
        "reported": 0,
        "infeasible": 0,
        "worker_fresh": 0,
        "showmap_none": 0,
        "byte_dup": 0,
        "accepted": 0,
    }
    for path in profile_dir.glob("redun_rank*.csv"):
        fields = path.read_text(encoding="utf-8").strip().split(",")
        if len(fields) < 6:
            continue
        result["generated"] += int(fields[1])
        result["reported"] += int(fields[2])
        result["infeasible"] += int(fields[3])
        result["worker_fresh"] += int(fields[4])
        result["showmap_none"] += int(fields[5])
        if len(fields) >= 9:
            result["byte_dup"] += int(fields[8])
    master_path = profile_dir / "redun_master.csv"
    if master_path.is_file():
        with master_path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        if rows:
            result["accepted"] = int(rows[-1].get("accepted", 0))
    return result


def _master_profile(profile_dir: Path) -> dict[str, float]:
    path = profile_dir / "mpi_master.log"
    text = path.read_text(encoding="utf-8", errors="replace")
    matches = MASTER_PROFILE_RE.findall(text)
    if not matches:
        profile = {
            "scan": 0.0,
            "scan_calls": 0.0,
            "dispatch": 0.0,
            "dispatch_calls": 0.0,
            "recv": 0.0,
            "recv_calls": 0.0,
            "triage": 0.0,
            "triage_calls": 0.0,
            "idle": 0.0,
        }
    else:
        values = matches[-1]
        keys = (
            "scan",
            "scan_calls",
            "dispatch",
            "dispatch_calls",
            "recv",
            "recv_calls",
            "triage",
            "triage_calls",
            "idle",
        )
        profile = {
            key: float(value) for key, value in zip(keys, values, strict=True)
        }
    triage_matches = MASTER_TRIAGE_RE.findall(text)
    if triage_matches:
        for match in PROFILE_DETAIL_RE.finditer(triage_matches[-1]):
            key = match.group("key").replace(".", "_")
            profile[f"triage_detail_{key}"] = float(match.group("value"))
    return profile


def load_run(run_dir: Path) -> dict[str, float | str]:
    result = _hybrid_result(run_dir)
    profile_dir = run_dir / "profiles"
    workers, items, phases = _worker_profile(profile_dir)
    redundancy = _redundancy(profile_dir)
    master = _master_profile(profile_dir)
    wall = _float(result, "wall_time_sec")
    worker_total = sum(phases.values())
    worker_capacity = workers * wall
    generated = redundancy["generated"]
    reported = redundancy["reported"]
    accepted = redundancy["accepted"]
    row: dict[str, float | str] = {
        "run_dir": str(run_dir),
        "np": int(_float(result, "np")),
        "afl_instances": int(_float(result, "afl_instances")),
        "symcc_workers": workers,
        "wall_s": wall,
        "worker_items": items,
        "worker_accounted_s": worker_total,
        "worker_accounted_pct": (
            100.0 * worker_total / worker_capacity if worker_capacity else 0.0),
        "worker_busy_pct": (
            100.0 * (worker_total - phases["wait"]) / worker_capacity
            if worker_capacity else 0.0),
        "generated": generated,
        "reported": reported,
        "accepted": accepted,
        "worker_internal_redundancy_pct": (
            100.0 * (generated - reported) / generated if generated else 0.0),
        "cross_worker_redundancy_pct": (
            100.0 * max(0, reported - accepted) / generated
            if generated else 0.0),
        "accepted_pct": 100.0 * accepted / generated if generated else 0.0,
        "edges_found": _float(result, "edges_found"),
        "coverage_auc": _float(result, "coverage_auc"),
        "afl_execs_per_sec": _float(result, "afl_execs_per_sec"),
        "symcc_peer_sync_complete": _float(result, "symcc_peer_sync_complete"),
    }
    for phase, seconds in phases.items():
        row[f"worker_{phase}_s"] = seconds
        row[f"worker_{phase}_pct"] = (
            100.0 * seconds / worker_total if worker_total else 0.0)
    for key, value in master.items():
        row[f"master_{key}_s" if key not in {
            "scan_calls", "dispatch_calls", "recv_calls", "triage_calls"
        } else f"master_{key}"] = value
    master_accounted = sum(master[key] for key in (
        "scan", "dispatch", "recv", "triage", "idle"))
    row["master_accounted_s"] = master_accounted
    row["master_accounted_pct"] = (
        100.0 * master_accounted / wall if wall else 0.0)
    row["master_scan_ms_per_call"] = (
        1000.0 * master["scan"] / master["scan_calls"]
        if master["scan_calls"] else 0.0)
    return row


def _mean_ci(values: list[float]) -> tuple[float, float]:
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, 0.0
    return mean, 1.96 * statistics.stdev(values) / math.sqrt(len(values))


def summarize(runs: list[dict[str, float | str]]) -> list[dict[str, float]]:
    groups: dict[int, list[dict[str, float | str]]] = {}
    for run in runs:
        groups.setdefault(int(run["np"]), []).append(run)
    base_fields = (
        "symcc_workers",
        "edges_found",
        "coverage_auc",
        "afl_execs_per_sec",
        "worker_busy_pct",
        "worker_exec_pct",
        "worker_showmap_dedup_pct",
        "worker_wait_pct",
        "master_scan_s",
        "master_scan_calls",
        "master_scan_ms_per_call",
        "master_dispatch_s",
        "master_recv_s",
        "master_triage_s",
        "master_idle_s",
        "master_accounted_pct",
        "worker_internal_redundancy_pct",
        "cross_worker_redundancy_pct",
        "accepted_pct",
        "symcc_peer_sync_complete",
    )
    detail_fields = tuple(sorted({
        key
        for row in runs
        for key in row
        if isinstance(key, str) and key.startswith("master_triage_detail_")
    }))
    fields = base_fields + detail_fields
    summaries = []
    for np_value, rows in sorted(groups.items()):
        summary: dict[str, float] = {
            "np": float(np_value),
            "rounds": float(len(rows)),
        }
        for field in fields:
            mean, ci = _mean_ci([float(row.get(field, 0.0)) for row in rows])
            summary[field] = mean
            summary[f"{field}_ci95"] = ci
        summaries.append(summary)
    return summaries


def _write_svg(path: Path, summaries: list[dict[str, float]]) -> None:
    width, height = 1320, 720
    left, top, bottom = 94, 94, 112
    chart_width, chart_height = 1130, height - top - bottom
    bar_width = 72
    groups = len(summaries)
    group_gap = chart_width / max(1, groups)
    colors = {
        "worker_exec_pct": "#006d77",
        "worker_showmap_dedup_pct": "#e76f51",
        "worker_wait_pct": "#e9c46a",
        "worker_other": "#94a3b8",
        "master_scan": "#1d4ed8",
        "master_triage": "#7c3aed",
        "master_other": "#64748b",
    }
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<style>text{font-family:Inter,Arial,sans-serif;fill:#172033}'
        '.title{font-size:26px;font-weight:700}.sub{font-size:14px;fill:#526078}'
        '.axis{stroke:#526078;stroke-width:1.5}.grid{stroke:#dbe4ee;stroke-width:1}'
        '.label{font-size:13px}.legend{font-size:12px}</style>',
        '<text x="66" y="40" class="title">Hybrid parallel profile: '
        'worker cost and master serialization</text>',
        '<text x="66" y="65" class="sub">Means of repeated 90 s runs; '
        'worker bars use accounted worker time, master bars use campaign wall time</text>',
    ]
    for tick in range(0, 101, 20):
        y = top + chart_height * (1.0 - tick / 100.0)
        svg.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + chart_width}" '
            f'y2="{y:.1f}" class="grid"/>')
        svg.append(
            f'<text x="{left - 12}" y="{y + 4:.1f}" text-anchor="end" '
            f'class="label">{tick}%</text>')
    svg += [
        f'<line x1="{left}" y1="{top + chart_height}" '
        f'x2="{left + chart_width}" y2="{top + chart_height}" class="axis"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" '
        f'y2="{top + chart_height}" class="axis"/>',
    ]
    for index, row in enumerate(summaries):
        center = left + group_gap * (index + 0.5)
        worker_x = center - bar_width - 8
        master_x = center + 8
        worker_parts = [
            ("worker_exec_pct", row["worker_exec_pct"]),
            ("worker_showmap_dedup_pct", row["worker_showmap_dedup_pct"]),
            ("worker_wait_pct", row["worker_wait_pct"]),
        ]
        worker_other = max(0.0, 100.0 - sum(value for _, value in worker_parts))
        worker_parts.append(("worker_other", worker_other))
        cumulative = 0.0
        for key, value in worker_parts:
            y = top + chart_height * (1.0 - (cumulative + value) / 100.0)
            h = chart_height * value / 100.0
            svg.append(
                f'<rect x="{worker_x:.1f}" y="{y:.1f}" width="{bar_width}" '
                f'height="{h:.1f}" fill="{colors[key]}"/>')
            cumulative += value
        wall = 90.0
        master_parts = [
            ("master_scan", 100.0 * row["master_scan_s"] / wall),
            ("master_triage", 100.0 * row["master_triage_s"] / wall),
        ]
        master_other = 100.0 * (
            row["master_dispatch_s"] + row["master_recv_s"]
            + row["master_idle_s"]
        ) / wall
        master_parts.append(("master_other", master_other))
        cumulative = 0.0
        for key, value in master_parts:
            y = top + chart_height * (1.0 - (cumulative + value) / 100.0)
            h = chart_height * value / 100.0
            svg.append(
                f'<rect x="{master_x:.1f}" y="{y:.1f}" width="{bar_width}" '
                f'height="{h:.1f}" fill="{colors[key]}"/>')
            cumulative += value
        svg += [
            f'<text x="{worker_x + bar_width / 2:.1f}" '
            f'y="{top + chart_height + 22}" text-anchor="middle" '
            'class="label">worker</text>',
            f'<text x="{master_x + bar_width / 2:.1f}" '
            f'y="{top + chart_height + 22}" text-anchor="middle" '
            'class="label">master</text>',
            f'<text x="{center:.1f}" y="{top + chart_height + 48}" '
            f'text-anchor="middle" class="label">np={int(row["np"])}; '
            f'W={row["symcc_workers"]:.0f}</text>',
        ]
    legend = [
        ("worker_exec_pct", "worker target+SymCC execution"),
        ("worker_showmap_dedup_pct", "worker coverage/dedup"),
        ("worker_wait_pct", "worker wait"),
        ("master_scan", "master queue scan"),
        ("master_triage", "master result triage"),
        ("master_other", "master dispatch/recv/idle"),
    ]
    x = 82.0
    for key, label in legend:
        svg.append(
            f'<rect x="{x:.1f}" y="{height - 38}" width="14" height="14" '
            f'fill="{colors[key]}"/>')
        svg.append(
            f'<text x="{x + 20:.1f}" y="{height - 26}" '
            f'class="legend">{label}</text>')
        x += 194.0
    svg.append('</svg>')
    path.write_text("\n".join(svg) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    runs = [load_run(path) for path in args.run_dirs]
    summaries = summarize(runs)
    accepted_usl = None
    accepted_usl_error = ""
    if len({int(run["symcc_workers"]) for run in runs}) >= 3:
        try:
            accepted_usl = fit_usl([
                ThroughputObservation(
                    int(run["symcc_workers"]),
                    float(run["accepted"]) / float(run["wall_s"]),
                )
                for run in runs
                if float(run["wall_s"]) > 0.0
            ], maximum_parallelism=4096, minimum_doubling_gain=0.10)
        except ValueError as error:
            accepted_usl_error = str(error)
    args.output.mkdir(parents=True, exist_ok=True)

    with (args.output / "profile_runs.csv").open(
            "w", newline="", encoding="utf-8") as stream:
        fieldnames = list(runs[0])
        for row in runs[1:]:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(runs)
    with (args.output / "profile_summary.csv").open(
            "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    (args.output / "profile_summary.json").write_text(
        json.dumps({
            "runs": runs,
            "summaries": summaries,
            "accepted_throughput_usl": (
                accepted_usl.to_dict() if accepted_usl else None),
            "accepted_throughput_fit_error": accepted_usl_error,
        }, indent=2) + "\n",
        encoding="utf-8")
    _write_svg(args.output / "profile_bottlenecks.svg", summaries)

    lines = [
        "# Hybrid parallel profile summary",
        "",
        "| total np | SymCC workers | edges | AUC | worker busy | "
        "worker exec | worker coverage/dedup | master scan | accepted |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            f"| {row['np']:.0f} | {row['symcc_workers']:.0f} | "
            f"{row['edges_found']:.1f} | {row['coverage_auc']:.1f} | "
            f"{row['worker_busy_pct']:.1f}% | {row['worker_exec_pct']:.1f}% | "
            f"{row['worker_showmap_dedup_pct']:.1f}% | "
            f"{row['master_scan_s']:.1f}s | {row['accepted_pct']:.1f}% |"
        )
    detail_fields = [
        field
        for field in summaries[0]
        if field.startswith("master_triage_detail_")
        and not field.endswith("_ci95")
    ]
    if detail_fields:
        lines += [
            "",
            "## Master triage detail",
            "",
            "| total np | dominant sub-phases |",
            "|---:|---|",
        ]
        for row in summaries:
            ranked = sorted(
                (
                    (field, float(row.get(field, 0.0)))
                    for field in detail_fields
                ),
                key=lambda item: item[1],
                reverse=True,
            )[:8]
            detail = ", ".join(
                f"{field.removeprefix('master_triage_detail_').removesuffix('_s')}="
                f"{value:.2f}s"
                for field, value in ranked
                if value > 0.0
            )
            lines.append(f"| {row['np']:.0f} | {detail or 'N/A'} |")
    lines += [
        "",
        "Worker phase percentages use accounted worker time as denominator. "
        "Master phase seconds are the last cumulative profile sample before "
        "campaign termination; consult `master_accounted_pct` before treating "
        "them as a complete wall-clock decomposition.",
    ]
    if accepted_usl is not None:
        lines += [
            "",
            "## Accepted SymCC throughput model",
            "",
            "The useful concolic rate is master-accepted SymCC inputs per "
            "second, excluding AFL executions and worker-rejected candidates.",
            "",
            f"- USL sigma={accepted_usl.contention:.4g}, "
            f"kappa={accepted_usl.coherency:.4g}, "
            f"R2={accepted_usl.r_squared:.4f}",
            f"- 10% doubling-gain ceiling: "
            f"{accepted_usl.doubling_ceiling} SymCC workers",
        ]
    (args.output / "profile_summary.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
