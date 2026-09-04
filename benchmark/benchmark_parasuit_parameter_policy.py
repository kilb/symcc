#!/usr/bin/env python3
"""Mechanism benchmark for F398 ParaSuit branch-rarity parameter selection."""

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

from parasuit_parameter_policy import (  # noqa: E402
    CoverageObservation,
    ParaSuitSelfConfiguringPolicy,
    analyze_parameter_space,
    branch_outcome_features,
)


PROFILES = [
    {"SYMCC_EXECUTOR_CLASS": "exact"},
    {"SYMCC_EXECUTOR_CLASS": "sampling", "SYMCC_POLY_CACHE": "1"},
]
A = "SYMCC_BACKSOLVER"
B = "SYMCC_PREFIX_CONTEXT_CACHE"


def observations() -> list[CoverageObservation]:
    return [
        CoverageObservation((A,), ("11:0", "12:1"), True, "ctx", 1),
        CoverageObservation((B,), ("13:0",), True, "ctx", 2),
        CoverageObservation((A, B), ("11:0",), False, "ctx", 3),
        CoverageObservation((B,), ("13:0", "14:1"), False, "ctx", 4),
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
    history = observations()
    analyses = [analyze_parameter_space((A, B), history) for _ in range(iterations)]
    analysis_digests = {
        hashlib.sha256(
            json.dumps(
                {
                    "baseline": item.baseline,
                    "combined": item.combined,
                    "normalized": item.normalized,
                    "frequency": item.feature_frequency,
                    "penalized": item.penalized_samples,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        for item in analyses
    }
    analyze_times = time_calls(
        lambda: analyze_parameter_space((A, B), history), iterations
    )

    trace = tuple(
        (index, index + 1, index + 2, 1000 + index, index % 2, index % 3 == 0)
        for index in range(128)
    )
    normalize_times = time_calls(
        lambda: branch_outcome_features(trace, maximum=128), iterations
    )

    hierarchical = ParaSuitSelfConfiguringPolicy(
        None,
        PROFILES,
        provider_commands=[],
        parameter_policy="hierarchical",
        max_parameters=1,
        seed=17,
    )
    hierarchical_times = time_calls(
        lambda: hierarchical._select_parameters({}, "ctx"), iterations
    )

    parasuit = ParaSuitSelfConfiguringPolicy(
        None,
        PROFILES,
        provider_commands=[],
        parameter_policy="parasuit",
        max_parameters=1,
        seed=17,
    )
    parasuit.coverage_history = history
    choices = [
        tuple(spec.name for spec in parasuit._select_parameters({}, "ctx"))
        for _ in range(iterations)
    ]
    selection_digests = {
        hashlib.sha256(json.dumps(choice).encode()).hexdigest() for choice in choices
    }
    parasuit_times = time_calls(
        lambda: parasuit._select_parameters({}, "ctx"), iterations
    )

    reload_times: list[int]
    with tempfile.TemporaryDirectory() as temporary:
        state = str(Path(temporary) / "self-config.json")
        persistent = ParaSuitSelfConfiguringPolicy(
            state,
            PROFILES,
            provider_commands=[],
            parameter_policy="parasuit",
            program_key="f398-mechanism-target",
            seed=17,
        )
        assignment = persistent.select()
        persistent.observe(
            assignment.token,
            reward=0.8,
            elapsed=0.1,
            coverage_features=("101:0", "102:1"),
        )
        persistent.save()
        base_bytes = Path(state).stat().st_size
        value_bytes = Path(f"{state}.value-space.json").stat().st_size
        selection_bytes = Path(f"{state}.parameter-selection.json").stat().st_size

        def reload() -> ParaSuitSelfConfiguringPolicy:
            return ParaSuitSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                parameter_policy="parasuit",
                program_key="f398-mechanism-target",
                seed=17,
            )

        reload_times = time_calls(reload, max(20, min(iterations, 200)))
        restored = reload()

    result = {
        "schema": "symcc-f398-parasuit-parameter-policy-benchmark-v1",
        "iterations": iterations,
        "upstream_revision": "e991924b827db80b117e1f5389fe07820bad13f0",
        "dataset": {
            "observations": len(history),
            "features": len(analyses[0].feature_frequency),
            "parameters": [A, B],
        },
        "oracle": {
            "analysis_digests": len(analysis_digests),
            "selection_digests": len(selection_digests),
            "baseline": analyses[0].baseline,
            "combined": analyses[0].combined,
            "normalized": analyses[0].normalized,
            "penalized": analyses[0].penalized_samples,
            "selected": list(choices[0]),
        },
        "latency": {
            "rarity_analysis": distribution(analyze_times),
            "branch_trace_normalization_128": distribution(normalize_times),
            "hierarchical_parameter_selection": distribution(hierarchical_times),
            "parasuit_parameter_selection": distribution(parasuit_times),
            "bound_three_file_state_reload": distribution(reload_times),
        },
        "state": {
            "base_bytes": base_bytes,
            "value_sidecar_bytes": value_bytes,
            "selection_sidecar_bytes": selection_bytes,
            "restored_coverage_observations": len(restored.coverage_history),
            "state_rejections": restored.parameter_policy_counts["state_rejections"],
        },
        "claim_boundary": (
            "Mechanism-only synthetic rarity, selection, normalization, and "
            "persistence cost; no target coverage, solver-throughput, or bug-yield "
            "claim."
        ),
    }
    if (
        result["oracle"]["analysis_digests"] != 1
        or result["oracle"]["selection_digests"] != 1
        or result["oracle"]["baseline"] != {A: 1.5, B: 0.5}
        or result["oracle"]["combined"] != {A: 0.0, B: 1.0}
        or result["oracle"]["selected"] != [B]
        or result["state"]["restored_coverage_observations"] != 1
        or result["state"]["state_rejections"] != 0
    ):
        raise RuntimeError("F398 mechanism invariant failed")
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
        Path(arguments.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
