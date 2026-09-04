#!/usr/bin/env python3
"""Benchmark executable self-configuration provider discovery."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from self_config import discover_parameter_registry  # noqa: E402


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _registry_digest(registry: Any) -> str:
    return str(registry.provenance()["providers"][-1]["digest"])


def _scope_counts(registry: Any) -> dict[str, int]:
    return {
        scope: sum(spec.scope == scope for spec in registry.specs.values())
        for scope in ("task", "query-service", "coordinator-campaign")
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--output")
    args = parser.parse_args()
    iterations = max(1, min(1000, args.iterations))
    warmup = max(0, min(100, args.warmup))
    provider = str(Path(args.provider).resolve())

    baseline_started = time.perf_counter_ns()
    baseline = discover_parameter_registry((), provider_commands=[])
    baseline_us = (time.perf_counter_ns() - baseline_started) / 1000.0
    if baseline.errors or baseline.conflicts:
        raise RuntimeError("the builtin registry is not internally consistent")

    for _ in range(warmup):
        current = discover_parameter_registry((), provider_commands=[[provider]])
        if current.errors or current.conflicts:
            raise RuntimeError(current.provenance())

    latencies_us: list[float] = []
    digests: set[str] = set()
    schema_hashes: set[str] = set()
    final = None
    for _ in range(iterations):
        started = time.perf_counter_ns()
        current = discover_parameter_registry((), provider_commands=[[provider]])
        latencies_us.append((time.perf_counter_ns() - started) / 1000.0)
        if current.errors or current.conflicts:
            raise RuntimeError(current.provenance())
        digests.add(_registry_digest(current))
        canonical = json.dumps(
            {
                name: {
                    "values": spec.values,
                    "scope": spec.scope,
                    "numeric": spec.numeric,
                    "minimum": spec.minimum,
                    "maximum": spec.maximum,
                    "active_when": spec.active_when,
                }
                for name, spec in sorted(current.specs.items())
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        schema_hashes.add(hashlib.sha256(canonical.encode()).hexdigest())
        final = current

    assert final is not None
    if len(final.providers) != 2 or len(final.specs) <= len(baseline.specs):
        raise RuntimeError("native provider did not extend the builtin registry")
    if len(digests) != 1 or len(schema_hashes) != 1:
        raise RuntimeError("native provider discovery is not deterministic")
    result = {
        "schema": "symcc-f396-native-parameter-provider-benchmark-v1",
        "iterations": iterations,
        "warmup": warmup,
        "provider": Path(provider).name,
        "baseline": {
            "parameters": len(baseline.specs),
            "providers": len(baseline.providers),
            "scope_counts": _scope_counts(baseline),
            "discovery_us": round(baseline_us, 3),
        },
        "native": {
            "parameters": len(final.specs),
            "providers": len(final.providers),
            "scope_counts": _scope_counts(final),
            "added_parameters": len(final.specs) - len(baseline.specs),
            "median_us": round(statistics.median(latencies_us), 3),
            "p95_us": round(_percentile(latencies_us, 0.95), 3),
            "maximum_us": round(max(latencies_us), 3),
        },
        "verification": {
            "successful_iterations": iterations,
            "provider_digests": sorted(digests),
            "registry_schema_hashes": sorted(schema_hashes),
            "errors": final.errors,
            "conflicts": final.conflicts,
        },
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(encoded, encoding="ascii")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
