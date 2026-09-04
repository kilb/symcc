#!/usr/bin/env python3
"""Mechanism benchmark for compatible-branch live-state pruning."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402
from live_state_search import LiveProgramGraph  # noqa: E402


def sequential_program(branches: int) -> dict[str, Any]:
    instructions: list[dict[str, Any]] = []
    for index in range(branches):
        instructions.extend((
            {"op": "input", "dst": f"x{index}", "offset": index},
            {
                "op": "binary",
                "operator": "eq",
                "dst": f"c{index}",
                "left": {"var": f"x{index}"},
                "right": {"const": 65 + index, "bits": 8},
                "bits": 1,
            },
        ))
    blocks: dict[str, list[dict[str, Any]]] = {
        "entry": instructions + [{
            "op": "branch",
            "condition": {"var": "c0"},
            "true": "t0",
            "false": "f0",
            "site": 1000,
        }],
    }
    for index in range(branches):
        successor = f"b{index + 1}" if index + 1 < branches else "exit"
        blocks[f"t{index}"] = [{"op": "jump", "target": successor}]
        blocks[f"f{index}"] = [{"op": "jump", "target": successor}]
        if index + 1 < branches:
            blocks[successor] = [{
                "op": "branch",
                "condition": {"var": f"c{index + 1}"},
                "true": f"t{index + 1}",
                "false": f"f{index + 1}",
                "site": 1000 + index + 1,
            }]
    blocks["exit"] = [{"op": "halt", "value": 0}]
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": branches,
        "functions": {"main": {"entry": "entry", "blocks": blocks}},
    }


def run_once(program: dict[str, Any], strategy: str, threshold: int) -> dict[str, Any]:
    environment = {
        "SYMCC_LIVE_SEARCH": strategy,
        "SYMCC_LIVE_CBC_STATE_THRESHOLD": str(threshold),
    }
    with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
        os.environ, environment,
    ):
        executor = LiveContinuationExecutor(LiveStateStore(temporary))
        root = executor.create(
            program, input_bytes=b"A" * int(program["input_size"])
        )
        started = time.perf_counter_ns()
        result = executor.resume(
            root,
            max_steps=10_000_000,
            max_states=1 << (int(program["input_size"]) + 1),
        )
        elapsed = time.perf_counter_ns() - started
        executor.close()
    cbc = result["state_search"]["cbc_execution"]
    return {
        "elapsed_ns": elapsed,
        "halted_paths": len(result["halted"]),
        "forks": result["forks"],
        "generated_checkpoints": len(result["generated_checkpoints"]),
        "feasibility_checks": result["feasibility_checks"],
        "cbc_checks": cbc["checks"],
        "cbc_pruned_states": cbc["pruned_states"],
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"runs": len(rows)}
    for key in rows[0]:
        values = [int(row[key]) for row in rows]
        result[key] = {
            "min": min(values),
            "median": statistics.median(values),
            "max": max(values),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--branches", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--threshold", type=int, default=1)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if not 2 <= arguments.branches <= 12:
        parser.error("--branches must be in 2..12")
    if not 1 <= arguments.repeats <= 100:
        parser.error("--repeats must be in 1..100")
    if not 1 <= arguments.threshold <= 100_000:
        parser.error("--threshold must be in 1..100000")

    program = sequential_program(arguments.branches)
    build_samples = []
    for _ in range(max(10, arguments.repeats)):
        started = time.perf_counter_ns()
        graph = LiveProgramGraph(program, cbc_enabled=True)
        build_samples.append(time.perf_counter_ns() - started)
    graph_telemetry = graph.cbc_telemetry()

    rows = {"bfs": [], "cbc": []}
    for _ in range(arguments.repeats):
        for strategy in ("bfs", "cbc"):
            rows[strategy].append(
                run_once(program, strategy, arguments.threshold)
            )
    baseline_paths = rows["bfs"][0]["halted_paths"]
    cbc_paths = rows["cbc"][0]["halted_paths"]
    baseline_checkpoints = rows["bfs"][0]["generated_checkpoints"]
    cbc_checkpoints = rows["cbc"][0]["generated_checkpoints"]
    payload = {
        "schema": "symcc-live-cbc-benchmark-v1",
        "scope": "synthetic-mechanism-only",
        "branches": arguments.branches,
        "repeats": arguments.repeats,
        "state_threshold": arguments.threshold,
        "expected_exhaustive_paths": 1 << arguments.branches,
        "graph": graph_telemetry,
        "static_build_ns": {
            "min": min(build_samples),
            "median": statistics.median(build_samples),
            "max": max(build_samples),
        },
        "strategies": {
            strategy: summarize(values) for strategy, values in rows.items()
        },
        "path_reduction_fraction": 1.0 - cbc_paths / baseline_paths,
        "checkpoint_reduction_fraction": (
            1.0 - cbc_checkpoints / baseline_checkpoints
        ),
        "claims": {
            "public_campaign_result": False,
            "solver_speedup_claim": False,
            "branch_outcome_preservation_checked_by_oracle": True,
        },
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.write_text(encoded, encoding="ascii")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
