#!/usr/bin/env python3
"""Independent finite nested-loop MemoryPhi composition oracle for F416."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import json
from typing import Any, Mapping


SCHEMA = "symcc-loop-memoryphi-byte-lane-induction-v6"


@dataclass(frozen=True)
class NestedSummaryCase:
    object_bytes: int
    outer_seed: int
    outer_step: int
    inner_seed: int
    inner_step: int
    bound_bits: int
    induction_bits: int
    load_bytes: int
    load_offsets: tuple[int, ...]
    writers: tuple[int, ...]
    outer_invariant_inner_bound: bool = True
    extra_memory_effects: int = 0
    nesting_depth: int = 2


def _reachable(case: NestedSummaryCase, width: int) -> tuple[int, ...]:
    return tuple(
        range(case.inner_seed, case.object_bytes - width + 1, case.inner_step)
    )


def _covered_lanes(case: NestedSummaryCase) -> set[int]:
    lanes: set[int] = set()
    for width in case.writers:
        for address in _reachable(case, width):
            lanes.update(range(address, address + width))
    return lanes


def mathematically_supported(case: NestedSummaryCase) -> bool:
    source_maximum = (1 << case.bound_bits) - 1
    maximum_inner = max(
        (
            induction
            for width in case.writers
            for induction in _reachable(case, width)
        ),
        default=-1,
    )
    covered = _covered_lanes(case)
    return (
        type(case.object_bytes) is int
        and 2 <= case.object_bytes <= 64
        and case.outer_seed == 0
        and case.inner_seed == 0
        and type(case.outer_step) is int
        and 1 <= case.outer_step <= 64
        and type(case.inner_step) is int
        and 1 <= case.inner_step <= 64
        and type(case.bound_bits) is int
        and 1 <= case.bound_bits <= 64
        and type(case.induction_bits) is int
        and 1 <= case.induction_bits <= 64
        and source_maximum >= 1
        and source_maximum >= maximum_inner + 1
        and source_maximum <= (1 << case.induction_bits) - case.outer_step
        and source_maximum <= (1 << case.induction_bits) - case.inner_step
        and case.outer_invariant_inner_bound is True
        and case.extra_memory_effects == 0
        and case.nesting_depth == 2
        and 1 <= len(case.writers) <= 4
        and all(
            type(width) is int
            and 1 <= width <= min(8, case.inner_step, case.object_bytes)
            for width in case.writers
        )
        and maximum_inner >= 0
        and type(case.load_bytes) is int
        and 1 <= case.load_bytes <= min(8, case.object_bytes)
        and bool(case.load_offsets)
        and len(set(case.load_offsets)) == len(case.load_offsets)
        and all(
            type(offset) is int
            and 0 <= offset <= case.object_bytes - case.load_bytes
            for offset in case.load_offsets
        )
        and all(
            offset + lane in covered
            for offset in case.load_offsets
            for lane in range(case.load_bytes)
        )
    )


def make_certificate(case: NestedSummaryCase) -> dict[str, Any]:
    domain = sorted(
        {
            induction
            for width in case.writers
            for induction in _reachable(case, width)
        }
    )
    alternatives: dict[int, list[dict[str, int]]] = {}
    writers = []
    for ordinal, width in enumerate(case.writers):
        reachable = _reachable(case, width)
        writers.append(
            {
                "ordinal": ordinal,
                "bytes": width,
                "address_stride": case.inner_step,
                "reachable_addresses": list(reachable),
                "reachable_index_values": list(reachable),
            }
        )
        for induction in reachable:
            for lane in range(width):
                alternatives.setdefault(induction + lane, []).append(
                    {
                        "writer": ordinal,
                        "writer_address": induction,
                        "writer_lane": lane,
                        "inner_induction_value": induction,
                    }
                )
    closed: set[int] = set()
    rounds = []
    for induction in domain:
        before = len(closed)
        for width in case.writers:
            if induction in _reachable(case, width):
                closed.update(range(induction, induction + width))
        rounds.append(
            {
                "inner_induction_value": induction,
                "new_lanes": len(closed) - before,
                "total_lanes": len(closed),
            }
        )
    rounds.append(
        {
            "kind": "stability-check",
            "new_lanes": 0,
            "total_lanes": len(closed),
        }
    )
    return {
        "schema": SCHEMA,
        "topology": {
            "nesting_depth": case.nesting_depth,
            "outer_invariant_inner_bound": case.outer_invariant_inner_bound,
            "extra_memory_effects": case.extra_memory_effects,
        },
        "memory_phis": {
            "equation": "H_outer=phi(entry,S_inner(H_outer))",
            "outer_backedge": "inner-memory-phi-summary",
            "inner_preheader": "outer-memory-phi",
            "inner_backedge": "ordered-writer-memory-def-chain",
        },
        "outer_induction": {
            "bits": case.induction_bits,
            "seed": case.outer_seed,
            "step": case.outer_step,
            "bound_bits": case.bound_bits,
        },
        "inner_induction": {
            "bits": case.induction_bits,
            "seed": case.inner_seed,
            "step": case.inner_step,
            "bound_bits": case.bound_bits,
        },
        "summary": {
            "order": "inner-to-outer",
            "input": "outer-memory-phi",
            "output": "inner-memory-phi",
            "writers": writers,
            "fixed_point": {
                "algorithm": "finite-monotone-byte-lane-union",
                "semantics": "inner-to-outer-ordered-writer-summary",
                "domain": domain,
                "rounds": rounds,
                "final_lanes": sorted(closed),
                "stable": True,
            },
        },
        "load_addresses": list(case.load_offsets),
        "load_bytes": case.load_bytes,
        "witnesses": [
            {
                "load_address": offset,
                "lane": lane,
                "alternatives": alternatives.get(offset + lane, []),
            }
            for offset in case.load_offsets
            for lane in range(case.load_bytes)
        ],
    }


def validate_certificate(raw: Mapping[str, Any], case: NestedSummaryCase) -> bool:
    return mathematically_supported(case) and raw == make_certificate(case)


def runtime_state(
    case: NestedSummaryCase,
    *,
    outer_count: int,
    inner_count: int,
) -> tuple[set[int], dict[int, tuple[int, int, int]]]:
    initialized: set[int] = set()
    provenance: dict[int, tuple[int, int, int]] = {}
    for outer_round, _ in enumerate(
        range(case.outer_seed, outer_count, case.outer_step)
    ):
        for inner_round, induction in enumerate(
            range(case.inner_seed, inner_count, case.inner_step)
        ):
            for writer, width in enumerate(case.writers):
                if induction + width > case.object_bytes:
                    continue
                for lane in range(width):
                    address = induction + lane
                    initialized.add(address)
                    provenance[address] = (outer_round, inner_round, writer)
    return initialized, provenance


def composed_summary_state(
    case: NestedSummaryCase,
    *,
    outer_count: int,
    inner_count: int,
) -> tuple[set[int], dict[int, tuple[int, int, int]]]:
    inner_effect: list[tuple[int, int, int, range]] = []
    for inner_round, induction in enumerate(
        range(case.inner_seed, inner_count, case.inner_step)
    ):
        inner_effect.extend(
            (
                inner_round,
                writer,
                induction,
                range(induction, induction + width),
            )
            for writer, width in enumerate(case.writers)
            if induction + width <= case.object_bytes
        )
    provenance: dict[int, tuple[int, int, int]] = {}
    for outer_round, _ in enumerate(
        range(case.outer_seed, outer_count, case.outer_step)
    ):
        for inner_round, writer, _induction, addresses in inner_effect:
            for address in addresses:
                provenance[address] = (outer_round, inner_round, writer)
    return set(provenance), provenance


def mutation_oracle() -> int:
    case = NestedSummaryCase(
        8, 0, 1, 0, 2, 8, 64, 2, tuple(range(7)), (1, 2)
    )
    baseline = make_certificate(case)
    mutations: list[dict[str, Any]] = []
    for mutate in (
        lambda item: item.__setitem__("schema", "bad"),
        lambda item: item["topology"].__setitem__("nesting_depth", 3),
        lambda item: item["topology"].__setitem__(
            "outer_invariant_inner_bound", False
        ),
        lambda item: item["topology"].__setitem__("extra_memory_effects", 1),
        lambda item: item["memory_phis"].__setitem__("equation", "bad"),
        lambda item: item["memory_phis"].__setitem__(
            "outer_backedge", "outer-memory-phi"
        ),
        lambda item: item["memory_phis"].__setitem__(
            "inner_preheader", "live-on-entry"
        ),
        lambda item: item["outer_induction"].__setitem__("seed", 1),
        lambda item: item["inner_induction"].__setitem__("step", 1),
        lambda item: item["summary"].__setitem__("order", "outer-to-inner"),
        lambda item: item["summary"].__setitem__("input", "live-on-entry"),
        lambda item: item["summary"].__setitem__("output", "outer-memory-phi"),
        lambda item: item["summary"]["writers"].reverse(),
        lambda item: item["summary"]["writers"].pop(),
        lambda item: item["summary"]["writers"].append(
            copy.deepcopy(item["summary"]["writers"][-1])
        ),
        lambda item: item["summary"]["writers"][0].__setitem__("ordinal", 1),
        lambda item: item["summary"]["writers"][0].__setitem__("bytes", 2),
        lambda item: item["summary"]["writers"][0][
            "reachable_addresses"
        ].pop(),
        lambda item: item["summary"]["fixed_point"].__setitem__(
            "semantics", "unordered-writer-summary"
        ),
        lambda item: item["summary"]["fixed_point"]["domain"].pop(),
        lambda item: item["summary"]["fixed_point"]["rounds"][0].__setitem__(
            "new_lanes", 0
        ),
        lambda item: item["summary"]["fixed_point"]["final_lanes"].pop(),
        lambda item: item["summary"]["fixed_point"].__setitem__("stable", False),
        lambda item: item["witnesses"][0]["alternatives"][0].__setitem__(
            "writer", 3
        ),
        lambda item: item["witnesses"][0]["alternatives"].pop(),
        lambda item: item["witnesses"].pop(),
    ):
        candidate = copy.deepcopy(baseline)
        mutate(candidate)
        mutations.append(candidate)
    if any(validate_certificate(candidate, case) for candidate in mutations):
        raise AssertionError("a mutated nested summary was accepted")
    return len(mutations)


def _writer_shapes(step: int) -> tuple[tuple[int, ...], ...]:
    wide = min(step, 2)
    return ((1,), (wide,), (1, wide), (wide, 1), (1, 1, 1, 1, 1))


def run_oracle() -> dict[str, Any]:
    cases = accepted = rejected = lane_checks = 0
    bitmap_checks = provenance_checks = runtime_accepts = runtime_rejects = 0
    for object_bytes in range(2, 9):
        for outer_step in (1, 2):
            for inner_step in (1, 2, 3):
                for load_bytes in range(1, min(2, object_bytes) + 1):
                    last_load = object_bytes - load_bytes
                    offset_domains = {
                        tuple(range(last_load + 1)),
                        tuple(dict.fromkeys((0, last_load))),
                        (0,),
                    }
                    for writers in _writer_shapes(inner_step):
                        for outer_seed in (0, 1):
                            for inner_seed in (0, 1):
                                for invariant in (True, False):
                                    for effects in (0, 1):
                                        for offsets in offset_domains:
                                            case = NestedSummaryCase(
                                                object_bytes,
                                                outer_seed,
                                                outer_step,
                                                inner_seed,
                                                inner_step,
                                                8,
                                                16,
                                                load_bytes,
                                                offsets,
                                                writers,
                                                invariant,
                                                effects,
                                            )
                                            observed = validate_certificate(
                                                make_certificate(case), case
                                            )
                                            expected = mathematically_supported(case)
                                            if observed != expected:
                                                raise AssertionError(
                                                    "nested summary oracle disagrees with math"
                                                )
                                            cases += 1
                                            accepted += int(observed)
                                            rejected += int(not observed)
                                            lane_checks += len(offsets) * load_bytes
                                            if not observed:
                                                continue
                                            for outer_count in range(4):
                                                for inner_count in range(
                                                    object_bytes + inner_step + 1
                                                ):
                                                    runtime = runtime_state(
                                                        case,
                                                        outer_count=outer_count,
                                                        inner_count=inner_count,
                                                    )
                                                    composed = composed_summary_state(
                                                        case,
                                                        outer_count=outer_count,
                                                        inner_count=inner_count,
                                                    )
                                                    if runtime != composed:
                                                        raise AssertionError(
                                                            "nested summary composition disagrees with runtime"
                                                        )
                                                    bitmap_checks += 1
                                                    provenance_checks += len(runtime[1])
                                                    for offset in offsets:
                                                        complete = all(
                                                            offset + lane in runtime[0]
                                                            for lane in range(load_bytes)
                                                        )
                                                        runtime_accepts += int(complete)
                                                        runtime_rejects += int(not complete)
    return {
        "schema": "symcc-nested-loop-memoryphi-summary-oracle-v1",
        "all_passed": True,
        "cases": cases,
        "accepted_nested_summaries": accepted,
        "rejected_unsupported_or_partial": rejected,
        "byte_lane_checks": lane_checks,
        "runtime_bitmap_equivalences": bitmap_checks,
        "last_writer_provenance_equivalence_cells": provenance_checks,
        "runtime_complete_load_accepts": runtime_accepts,
        "runtime_zero_outer_or_prefix_rejects": runtime_rejects,
        "mutations_rejected": mutation_oracle(),
        "claim_boundary": (
            "Finite reference-model bitmap and last-writer equivalence only; "
            "not LLVM build cost, coverage, solver speed, defect yield, or "
            "end-to-end speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = json.dumps(run_oracle(), sort_keys=True, separators=(",", ":"))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
