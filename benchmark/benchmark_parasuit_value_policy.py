#!/usr/bin/env python3
"""Mechanism benchmark for F397 ParaSuit-style value-space adaptation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from parasuit_value_policy import (  # noqa: E402
    AdaptiveSelfConfiguringPolicy,
    ValueObservation,
    analyze_value_space,
)
from self_config import ParameterSpec, SelfConfiguringPolicy  # noqa: E402


PROFILES = [
    {"SYMCC_EXECUTOR_CLASS": "exact"},
    {"SYMCC_EXECUTOR_CLASS": "sampling", "SYMCC_POLY_CACHE": "1"},
]
SCHEMA = json.dumps(
    {
        "parameters": {
            "SYMCC_BENCH_NUMERIC": {
                "values": [1, 2, 9, 10],
                "numeric": True,
                "min": 1,
                "max": 20,
            }
        }
    }
)


def observations() -> list[ValueObservation]:
    samples = [
        ("1", 0.05),
        ("1", 0.10),
        ("2", 0.10),
        ("2", 0.15),
        ("9", 0.90),
        ("9", 0.95),
        ("10", 0.90),
        ("10", 1.00),
    ]
    return [
        ValueObservation(value, reward, 1.0, False, "ctx", sequence)
        for sequence, (value, reward) in enumerate(samples, 1)
    ]


def distribution(samples: list[int]) -> dict[str, float]:
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {
        "median_us": round(statistics.median(ordered) / 1000.0, 3),
        "p95_us": round(p95 / 1000.0, 3),
        "max_us": round(max(ordered) / 1000.0, 3),
    }


def time_calls(call: Callable[[], Any], iterations: int) -> list[int]:
    for _ in range(20):
        call()
    samples: list[int] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        call()
        samples.append(time.perf_counter_ns() - started)
    return samples


def run(iterations: int) -> dict[str, Any]:
    spec = ParameterSpec(
        "SYMCC_BENCH_NUMERIC",
        ["1", "2", "9", "10"],
        numeric=True,
        minimum=1,
        maximum=20,
    )
    history = observations()
    decisions = [
        analyze_value_space(spec, history, threshold=0.7, min_samples=4)
        for _ in range(iterations)
    ]
    decision_payloads = [decision.to_mapping() for decision in decisions]
    decision_digests = {
        hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for payload in decision_payloads
    }
    score = decisions[0].silhouette
    threshold_grid = [index / 100.0 for index in range(-100, 101)]
    admitted = sum(
        analyze_value_space(spec, history, threshold=threshold, min_samples=4).mode
        == "exploit"
        for threshold in threshold_grid
    )

    analyze_times = time_calls(
        lambda: analyze_value_space(spec, history, threshold=0.7, min_samples=4),
        iterations,
    )

    baseline = SelfConfiguringPolicy(
        None,
        PROFILES,
        schema_space=SCHEMA,
        provider_commands=[],
        max_parameters=1,
        seed=17,
    )
    baseline_spec = baseline.parameters["SYMCC_BENCH_NUMERIC"]
    for value, posterior in baseline_spec.posteriors.items():
        posterior.update(
            0.5 if value == "__unset__" else float(value) / 10.0, 1.0, False
        )
    baseline_times = time_calls(
        lambda: baseline._select_value(baseline_spec, "ctx", {}), iterations
    )

    adaptive = AdaptiveSelfConfiguringPolicy(
        None,
        PROFILES,
        schema_space=SCHEMA,
        provider_commands=[],
        value_policy="hybrid",
        silhouette_threshold=0.7,
        min_cluster_samples=4,
        exploration_reserve=0,
        max_parameters=1,
        seed=17,
    )
    adaptive_spec = adaptive.parameters["SYMCC_BENCH_NUMERIC"]
    adaptive.value_history[adaptive_spec.name] = history
    adaptive_times = time_calls(
        lambda: adaptive._select_value(adaptive_spec, "ctx", {}), iterations
    )

    reload_times: list[int] = []
    with tempfile.TemporaryDirectory() as temporary:
        state = str(Path(temporary) / "self-config.json")
        persistent = AdaptiveSelfConfiguringPolicy(
            state,
            PROFILES,
            schema_space=SCHEMA,
            provider_commands=[],
            value_policy="hybrid",
            program_key="f397-mechanism-target",
            seed=17,
        )
        persistent.value_history["SYMCC_BENCH_NUMERIC"] = history
        persistent.save()
        base_bytes = Path(state).stat().st_size
        sidecar_bytes = Path(f"{state}.value-space.json").stat().st_size

        def reload() -> AdaptiveSelfConfiguringPolicy:
            return AdaptiveSelfConfiguringPolicy(
                state,
                PROFILES,
                schema_space=SCHEMA,
                provider_commands=[],
                value_policy="hybrid",
                program_key="f397-mechanism-target",
                seed=17,
            )

        reload_times = time_calls(reload, max(20, min(iterations, 200)))
        restored = reload()

    result = {
        "schema": "symcc-f397-parasuit-value-policy-benchmark-v1",
        "iterations": iterations,
        "upstream_revision": "e991924b827db80b117e1f5389fe07820bad13f0",
        "dataset": {
            "observations": len(history),
            "expected_clusters": 2,
            "threshold": 0.7,
        },
        "determinism": {
            "runs": len(decisions),
            "decision_digests": len(decision_digests),
            "silhouette": round(score, 9),
            "labels": list(decisions[0].labels),
            "admitted_thresholds": admitted,
            "threshold_grid": len(threshold_grid),
        },
        "latency": {
            "analysis": distribution(analyze_times),
            "thompson_value_selection": distribution(baseline_times),
            "adaptive_value_selection": distribution(adaptive_times),
            "bound_state_reload": distribution(reload_times),
        },
        "state": {
            "base_bytes": base_bytes,
            "sidecar_bytes": sidecar_bytes,
            "restored_samples": len(restored.value_history["SYMCC_BENCH_NUMERIC"]),
            "state_rejections": restored.value_policy_counts["state_rejections"],
        },
        "claim_boundary": (
            "Mechanism-only synthetic clustering and persistence cost; no target "
            "coverage, solver-throughput, or bug-yield claim."
        ),
    }
    if (
        result["determinism"]["decision_digests"] != 1
        or result["determinism"]["labels"] != [0, 0, 0, 0, 1, 1, 1, 1]
        or result["determinism"]["silhouette"] <= 0.9
        or result["state"]["restored_samples"] != len(history)
        or result["state"]["state_rejections"] != 0
    ):
        raise RuntimeError("F397 mechanism invariant failed")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output")
    arguments = parser.parse_args()
    iterations = max(20, min(10000, arguments.iterations))
    payload = run(iterations)
    rendered = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if arguments.output:
        Path(arguments.output).write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
