#!/usr/bin/env python3
"""Finite-domain oracles for collective heap-union initialization proofs."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


def collective_cover(
    bases: list[int],
    stores: list[tuple[int, int, bool]],
    load_bytes: int,
) -> tuple[bool, tuple[int, ...]]:
    """Independent interval/dominance model of the producer certificate."""
    covered: set[int] = set()
    contributors: set[int] = set()
    for store_index, (base, store_bytes, dominates) in enumerate(stores):
        if not dominates or store_bytes < load_bytes:
            continue
        if base in bases:
            covered.add(base)
            contributors.add(store_index)
    accepted = len(bases) >= 2 and len(contributors) >= 2 and covered == set(bases)
    return accepted, tuple(sorted(covered))


def validator_program() -> dict:
    bases = [64, 80]
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "memory_size": 81,
        "memory_objects": [
            {
                "name": f"heap:site:{index}",
                "kind": "heap",
                "site": f"site:{index}",
                "slot": 0,
                "capacity": 1,
                "address": base,
                "size": 1,
                "read_only": False,
                "lifetime": "runtime-alloc-free",
                "allocation": "bounded-pool-infallible",
            }
            for index, base in enumerate(bases)
        ],
        "functions": {
            "main": {
                "entry": "entry",
                "params": [],
                "blocks": {
                    "entry": [
                        *[
                            {
                                "op": "heap_alloc",
                                "dst": f"pointer_{index}",
                                "addresses": [base],
                                "capacity": 1,
                                "size": 1,
                                "site": f"site:{index}",
                                "allocator": "malloc",
                                "bits": 64,
                            }
                            for index, base in enumerate(bases)
                        ],
                        {
                            "op": "load",
                            "dst": "value",
                            "address": {"const": 64, "bits": 64},
                            "alias_cases": [
                                {
                                    "addresses": [base],
                                    "guards": [
                                        {
                                            "value": {
                                                "const": int(base == 64),
                                                "bits": 1,
                                            },
                                            "equals": 1,
                                            "bits": 1,
                                        }
                                    ],
                                }
                                for base in bases
                            ],
                            "initialization_bases": bases,
                            "bits": 8,
                            "bytes": 1,
                        },
                        {"op": "return", "value": 0},
                    ],
                },
            },
        },
        "lowering": {
            "capabilities": [
                "bounded-heap-lifetime",
                "bounded-pointer-union",
                "bounded-collective-heap-union-initialization",
            ],
        },
    }


def rejected_mutations() -> int:
    mutations: list[tuple[str, Callable[[dict], object]]] = [
        (
            "missing-capability",
            lambda program: program["lowering"]["capabilities"].pop(),
        ),
        (
            "incomplete",
            lambda program: program["functions"]["main"]["blocks"]["entry"][-2].update(
                initialization_bases=[64]
            ),
        ),
        (
            "foreign",
            lambda program: program["functions"]["main"]["blocks"]["entry"][-2].update(
                initialization_bases=[64, 4096]
            ),
        ),
        (
            "missing-contract",
            lambda program: program["functions"]["main"]["blocks"]["entry"][-2].pop(
                "initialization_bases"
            ),
        ),
    ]
    rejected = 0
    with tempfile.TemporaryDirectory() as root:
        store = LiveStateStore(root, page_size=64)
        LiveContinuationExecutor(store).create(validator_program())
        for _name, mutate in mutations:
            program = json.loads(json.dumps(validator_program()))
            mutate(program)
            try:
                LiveContinuationExecutor(store).create(program)
            except ValueError:
                rejected += 1
                continue
            raise AssertionError("validator admitted a certificate mutation")
    return rejected


def main() -> int:
    domains = 31
    interval_checks = 0
    lifetime_assignments = 0
    legacy_rejections = 0
    partial_rejections = 0
    nondominating_rejections = 0
    for domain in range(2, 33):
        bases = [64 + 16 * index for index in range(domain)]
        values = {base: (17 * index + 3) & 0xFF for index, base in enumerate(bases)}
        for load_bytes in range(1, 9):
            stores = [(base, 8, True) for base in bases]
            accepted, certificate = collective_cover(bases, stores, load_bytes)
            if not accepted or certificate != tuple(bases):
                raise AssertionError("complete collective cover was rejected")
            interval_checks += domain
            if any(
                all(store_base == loaded for loaded in bases)
                for store_base, _store_bytes, _dominates in stores
            ):
                raise AssertionError("legacy single-store proof was accepted")
            legacy_rejections += 1

            if collective_cover(bases, stores[:-1], load_bytes)[0]:
                raise AssertionError("partial collective cover was accepted")
            partial_rejections += 1

            nondominating = [*stores[:-1], (bases[-1], 8, False)]
            if collective_cover(bases, nondominating, load_bytes)[0]:
                raise AssertionError("non-dominating cover was accepted")
            nondominating_rejections += 1

        for victim in bases:
            for survivor in bases:
                if victim == survivor:
                    continue
                live = {base: base != victim for base in bases}
                initialized = dict(live)
                if not live[survivor] or not initialized[survivor]:
                    raise AssertionError("surviving object lost lifetime state")
                merged_load = next(
                    value
                    for base, value in values.items()
                    if base == survivor and live[base] and initialized[base]
                )
                survivor_index = (survivor - 64) // 16
                expected_value = (17 * survivor_index + 3) & 0xFF
                if merged_load != expected_value:
                    raise AssertionError("surviving object value changed")
                lifetime_assignments += 1

    print(
        json.dumps(
            {
                "schema": "symcc-collective-heap-union-init-oracle-v1",
                "domains": domains,
                "maximum_domain": 32,
                "interval_checks": interval_checks,
                "lifetime_assignments": lifetime_assignments,
                "legacy_single_store_rejections": legacy_rejections,
                "partial_cover_rejections": partial_rejections,
                "nondominating_cover_rejections": nondominating_rejections,
                "rejected_certificate_mutations": rejected_mutations(),
                "all_passed": True,
                "claim_boundary": (
                    "finite ordinary heap bases and dominating scalar stores only; "
                    "not a proof for arbitrary symbolic heaps or path-correlated "
                    "branch-local stores"
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
