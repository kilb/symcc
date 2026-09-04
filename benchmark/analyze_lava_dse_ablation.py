#!/usr/bin/env python3
"""Summarize retained LAVA-M DSE ablation case directories."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Iterable


TELEMETRY_KEYS = (
    "solver_queries",
    "solver_sat",
    "solver_unsat",
    "solver_unknown",
    "solver_time_us",
    "fast_solves",
    "z3_solves",
    "backsolver_targets",
    "backsolver_attempts",
    "backsolver_sat",
    "backsolver_direct_sat",
    "backsolver_z3_fallbacks",
    "poly_cache_hits",
    "poly_cache_entries",
    "poly_samples",
    "poly_template_constraints",
    "poly_john_steps",
    "poly_cross_prefix_probes",
    "poly_cross_prefix_hits",
    "prefix_context_hits",
    "prefix_context_entries",
    "unsat_core_hits",
    "unsat_core_entries",
    "unsat_core_clauses",
    "linear_subsumption_prunes",
)


def load_cases(root: Path) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for path in sorted(root.rglob("lava_case_result.json")):
        data = json.loads(path.read_text())
        data["_path"] = str(path)
        cases.append(data)
    return cases


def numeric(values: Iterable[object]) -> list[float]:
    out: list[float] = []
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            out.append(float(value))
    return out


def summarize(cases: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for case in cases:
        grouped[(str(case["program"]), str(case["profile"]))].append(case)

    rows: list[dict[str, object]] = []
    for (program, profile), group in sorted(grouped.items()):
        listed_union: set[int] = set()
        extra_union: set[int] = set()
        strategy_counts: Counter[str] = Counter()
        telemetry_sums: Counter[str] = Counter()
        with_telemetry = 0
        for case in group:
            replay = case.get("lava_bug_replay", {})
            listed_union.update(int(x) for x in replay.get("listed_hits", []))
            extra_union.update(int(x) for x in replay.get("extra_hits", []))
            strategy_counts.update(case.get("strategy_outputs", {}))
            telemetry = case.get("telemetry") or {}
            if telemetry:
                with_telemetry += 1
            for key in TELEMETRY_KEYS:
                value = telemetry.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    telemetry_sums[key] += value

        generated = numeric(case.get("generated") for case in group)
        unique_generated = numeric(case.get("unique_generated") for case in group)
        elapsed = numeric(case.get("elapsed_seconds") for case in group)
        listed_hits = numeric(
            case.get("lava_bug_replay", {}).get("listed_hit_count")
            for case in group
        )
        listed_total = max(
            int(case.get("lava_bug_replay", {}).get("listed_total", 0))
            for case in group
        )
        rows.append({
            "program": program,
            "profile": profile,
            "cases": len(group),
            "telemetry_cases": with_telemetry,
            "timeouts": sum(bool(case.get("timed_out")) for case in group),
            "generated_mean": mean(generated) if generated else 0.0,
            "generated_median": median(generated) if generated else 0.0,
            "unique_generated_mean": mean(unique_generated) if unique_generated else 0.0,
            "elapsed_seconds_mean": mean(elapsed) if elapsed else 0.0,
            "listed_hits_mean": mean(listed_hits) if listed_hits else 0.0,
            "listed_hits_median": median(listed_hits) if listed_hits else 0.0,
            "listed_union_count": len(listed_union),
            "listed_total": listed_total,
            "extra_union": sorted(extra_union),
            "strategies": dict(sorted(strategy_counts.items())),
            "telemetry_totals": dict(sorted(telemetry_sums.items())),
        })
    return rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [
        "program", "profile", "cases", "telemetry_cases", "timeouts",
        "generated_mean", "generated_median", "unique_generated_mean",
        "elapsed_seconds_mean", "listed_hits_mean", "listed_hits_median",
        "listed_union_count", "listed_total", "extra_union",
        "strategies", "telemetry_totals",
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize LAVA-M DSE ablation case outputs.")
    parser.add_argument("root", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--csv-out", type=Path)
    args = parser.parse_args()

    cases = load_cases(args.root)
    rows = summarize(cases)
    payload = {
        "schema": "symcc-lava-dse-ablation-summary-v1",
        "root": str(args.root.resolve()),
        "case_count": len(cases),
        "profiles": rows,
    }
    if args.json_out:
        args.json_out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if args.csv_out:
        write_csv(args.csv_out, rows)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
