#!/usr/bin/env python3
"""Mechanism benchmark for concrete-constraint-guided live scheduling."""

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


def constant(value: int, bits: int) -> dict[str, int]:
    return {"const": value, "bits": bits}


def comparison_program(
    operator: str,
    bits: int,
    constant_value: int,
) -> dict[str, Any]:
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "memory_size": 8,
        "functions": {"main": {
            "entry": "entry",
            "blocks": {
                "entry": [
                    {
                        "op": "store", "address": constant(0, 64),
                        "value": constant(1, bits), "bits": bits,
                    },
                    {
                        "op": "load", "dst": "loaded",
                        "address": constant(0, 64), "bits": bits,
                    },
                    {
                        "op": "binary", "operator": operator,
                        "dst": "condition", "left": {"var": "loaded"},
                        "right": constant(constant_value, bits), "bits": 1,
                    },
                    {
                        "op": "branch", "condition": {"var": "condition"},
                        "true": "yes", "false": "no", "site": 300,
                    },
                ],
                "yes": [{"op": "halt", "value": 1}],
                "no": [{"op": "halt", "value": 0}],
            },
        }},
    }


def scheduling_program(distractor_instructions: int = 500) -> dict[str, Any]:
    distractor = [
        {
            "op": "const", "dst": f"padding{index}",
            "value": index, "bits": 32,
        }
        for index in range(distractor_instructions)
    ]
    distractor.append({"op": "halt", "value": 20})
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 2,
        "memory_size": 1,
        "functions": {"main": {
            "entry": "entry",
            "blocks": {
                "entry": [
                    {"op": "input", "dst": "x", "offset": 0},
                    {
                        "op": "binary", "operator": "eq", "dst": "root",
                        "left": {"var": "x"}, "right": constant(0, 8),
                        "bits": 1,
                    },
                    {
                        "op": "branch", "condition": {"var": "root"},
                        "true": "seed_store", "false": "split", "site": 100,
                    },
                ],
                "seed_store": [
                    {
                        "op": "store", "address": constant(0, 64),
                        "value": constant(1, 8), "bits": 8,
                    },
                    {"op": "jump", "target": "check"},
                ],
                "split": [
                    {"op": "input", "dst": "y", "offset": 1},
                    {
                        "op": "binary", "operator": "eq", "dst": "fork",
                        "left": {"var": "y"}, "right": constant(0, 8),
                        "bits": 1,
                    },
                    {
                        "op": "branch", "condition": {"var": "fork"},
                        "true": "distractor", "false": "target_store",
                        "site": 200,
                    },
                ],
                "distractor": distractor,
                "target_store": [
                    {
                        "op": "store", "address": constant(0, 64),
                        "value": constant(4, 8), "bits": 8,
                    },
                    {"op": "jump", "target": "check"},
                ],
                "check": [
                    {
                        "op": "load", "dst": "loaded",
                        "address": constant(0, 64), "bits": 8,
                    },
                    {
                        "op": "binary", "operator": "ugt", "dst": "target",
                        "left": {"var": "loaded"},
                        "right": constant(2, 8), "bits": 1,
                    },
                    {
                        "op": "branch", "condition": {"var": "target"},
                        "true": "target_hit", "false": "seed_done",
                        "site": 300,
                    },
                ],
                "target_hit": [{"op": "halt", "value": 30}],
                "seed_done": [{"op": "halt", "value": 10}],
            },
        }},
    }


def run_once(
    program: dict[str, Any], strategy: str, max_steps: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
        os.environ, {"SYMCC_LIVE_SEARCH": strategy},
    ):
        executor = LiveContinuationExecutor(LiveStateStore(temporary))
        root = executor.create(program, input_bytes=b"\x00\x00")
        started = time.perf_counter_ns()
        result = executor.resume(root, max_steps=max_steps, max_states=16)
        elapsed = time.perf_counter_ns() - started
        executor.close()
    return {
        "elapsed_ns": elapsed,
        "steps": result["steps"],
        "forks": result["forks"],
        "bounded": result["bounded"],
        "halted_values": [row["value"] for row in result["halted"]],
        "target_hit": any(row["value"] == 30 for row in result["halted"]),
        "cgs_execution": result["state_search"]["cgs_execution"],
    }


def minimum_target_budget(
    program: dict[str, Any], strategy: str, upper: int,
) -> int:
    low, high = 1, upper
    if not run_once(program, strategy, high)["target_hit"]:
        raise RuntimeError("target was not reached within the benchmark bound")
    while low < high:
        middle = (low + high) // 2
        if run_once(program, strategy, middle)["target_hit"]:
            high = middle
        else:
            low = middle + 1
    return low


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = [int(row["elapsed_ns"]) for row in rows]
    return {
        "runs": len(rows),
        "elapsed_ns": {
            "min": min(elapsed),
            "median": statistics.median(elapsed),
            "max": max(elapsed),
        },
        "steps": rows[0]["steps"],
        "forks": rows[0]["forks"],
        "halted_values": rows[0]["halted_values"],
        "cgs_execution": rows[0]["cgs_execution"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distractor-instructions", type=int, default=500)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if not 1 <= arguments.distractor_instructions <= 100_000:
        parser.error("--distractor-instructions must be in 1..100000")
    if not 1 <= arguments.repeats <= 100:
        parser.error("--repeats must be in 1..100")

    program = scheduling_program(arguments.distractor_instructions)
    full_bound = arguments.distractor_instructions + 100
    graph_samples = []
    for _ in range(max(10, arguments.repeats)):
        started = time.perf_counter_ns()
        graph = LiveProgramGraph(program, cgs_enabled=True)
        graph_samples.append(time.perf_counter_ns() - started)

    budgets = {
        strategy: minimum_target_budget(program, strategy, full_bound)
        for strategy in ("bfs", "cgs")
    }
    rows = {"bfs": [], "cgs": []}
    for _ in range(arguments.repeats):
        for strategy in rows:
            rows[strategy].append(run_once(program, strategy, full_bound))
    if sorted(rows["bfs"][0]["halted_values"]) != [10, 20, 30]:
        raise AssertionError("BFS terminal set is incomplete")
    if sorted(rows["cgs"][0]["halted_values"]) != [10, 20, 30]:
        raise AssertionError("CGS terminal set is incomplete")

    payload = {
        "schema": "symcc-live-cgs-benchmark-v1",
        "scope": "synthetic-mechanism-only",
        "distractor_instructions": arguments.distractor_instructions,
        "repeats": arguments.repeats,
        "graph": graph.cgs_telemetry(),
        "static_build_ns": {
            "min": min(graph_samples),
            "median": statistics.median(graph_samples),
            "max": max(graph_samples),
        },
        "minimum_target_instruction_budget": budgets,
        "target_budget_reduction_fraction": 1.0 - (
            budgets["cgs"] / budgets["bfs"]
        ),
        "strategies": {
            strategy: summarize(values) for strategy, values in rows.items()
        },
        "terminal_set_equal": True,
        "claims": {
            "public_campaign_result": False,
            "solver_speedup_claim": False,
            "mechanism_target_latency_result": True,
            "completeness_proof": False,
        },
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.write_text(encoded, encoding="ascii")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
