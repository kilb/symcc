#!/usr/bin/env python3
"""Independent finite guard/bitmap oracle for F413 conditional loops."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import json
from typing import Any, Mapping

from check_strided_loop_memoryphi_byte_lane_oracles import (
    StridedInductionCase,
    reachable_writers,
    writer_aliases,
    writer_lane_map,
)


SCHEMA = "symcc-loop-memoryphi-byte-lane-induction-v3"
PREDICATES = ("eq", "ne", "ult", "ule")


@dataclass(frozen=True)
class ConditionalInductionCase:
    object_bytes: int
    seed: int
    step: int
    scale: int
    writer_bytes: int
    bound_bits: int
    induction_bits: int
    load_bytes: int
    load_offsets: tuple[int, ...]
    predicate: str
    guard_limit: int
    writer_when: bool

    def affine(self) -> StridedInductionCase:
        return StridedInductionCase(
            self.object_bytes,
            self.seed,
            self.step,
            self.scale,
            self.writer_bytes,
            self.bound_bits,
            self.induction_bits,
            self.load_bytes,
            self.load_offsets,
        )


def compare(predicate: str, left: int, right: int) -> bool:
    if predicate == "eq":
        return left == right
    if predicate == "ne":
        return left != right
    if predicate == "ult":
        return left < right
    if predicate == "ule":
        return left <= right
    raise ValueError("unsupported reference predicate")


def mathematically_supported(case: ConditionalInductionCase) -> bool:
    affine = case.affine()
    reachable = reachable_writers(affine)
    lane_map = writer_lane_map(affine)
    source_maximum = (1 << case.bound_bits) - 1
    induction_limit = (1 << case.induction_bits) - case.step
    return (
        case.seed == 0
        and 1 <= case.step <= 64
        and case.scale > 0
        and 1 <= case.writer_bytes <= min(
            8, case.step * case.scale
        )
        and bool(reachable)
        and lane_map is not None
        and source_maximum >= reachable[-1][1] + 1
        and source_maximum <= induction_limit
        and 1 <= case.load_bytes <= min(8, case.object_bytes)
        and bool(case.load_offsets)
        and len(set(case.load_offsets)) == len(case.load_offsets)
        and all(
            0 <= offset <= case.object_bytes - case.load_bytes
            for offset in case.load_offsets
        )
        and all(
            offset + lane in lane_map
            for offset in case.load_offsets
            for lane in range(case.load_bytes)
        )
        and case.predicate in PREDICATES
        and isinstance(case.writer_when, bool)
        and 0 <= case.guard_limit < (1 << case.induction_bits)
    )


def make_certificate(case: ConditionalInductionCase) -> dict[str, Any]:
    affine = case.affine()
    aliases = writer_aliases(affine)
    reachable = reachable_writers(affine)
    lane_map = writer_lane_map(affine) or {}
    guard = {
        "predicate": case.predicate,
        "right": case.guard_limit,
        "writer_when": case.writer_when,
        "memory_phi": {
            "writer": "writer-memory-def",
            "skip": "header-memory-phi",
        },
    }
    witnesses = []
    for address in case.load_offsets:
        for lane in range(case.load_bytes):
            owner = lane_map.get(address + lane)
            witnesses.append({
                "load_address": address,
                "lane": lane,
                "writer_address": None if owner is None else owner[0],
                "writer_lane": None if owner is None else owner[1],
                "induction_value": None if owner is None else owner[2],
                "writer_guard": {
                    "predicate": case.predicate,
                    "right": case.guard_limit,
                    "equals": case.writer_when,
                },
            })
    return {
        "schema": SCHEMA,
        "base": 0,
        "load_bytes": case.load_bytes,
        "induction": {
            "bits": case.induction_bits,
            "seed": case.seed,
            "step": case.step,
            "bound_bits": case.bound_bits,
        },
        "writer": {
            "bytes": case.writer_bytes,
            "scale": case.scale,
            "address_stride": case.step * case.scale,
            "addresses": [address for address, _ in aliases],
            "index_values": [index for _, index in aliases],
            "reachable_addresses": [address for address, _ in reachable],
            "reachable_index_values": [index for _, index in reachable],
        },
        "writer_guard": guard,
        "load_addresses": list(case.load_offsets),
        "witnesses": witnesses,
    }


def validate_certificate(
    raw: Mapping[str, Any], case: ConditionalInductionCase
) -> bool:
    return mathematically_supported(case) and raw == make_certificate(case)


def runtime_bitmap_accepts(
    case: ConditionalInductionCase, *, count: int, load_offset: int
) -> bool:
    initialized: set[int] = set()
    for address, induction in reachable_writers(case.affine()):
        if induction >= count:
            continue
        if compare(case.predicate, induction, case.guard_limit) != (
            case.writer_when
        ):
            continue
        initialized.update(range(address, address + case.writer_bytes))
    return all(
        load_offset + lane in initialized
        for lane in range(case.load_bytes)
    )


def guard_carry_accepts(
    case: ConditionalInductionCase, *, count: int, load_offset: int
) -> bool:
    lane_map = writer_lane_map(case.affine())
    if lane_map is None:
        return False
    for lane in range(case.load_bytes):
        owner = lane_map.get(load_offset + lane)
        if owner is None or owner[2] >= count:
            return False
        if compare(case.predicate, owner[2], case.guard_limit) != (
            case.writer_when
        ):
            return False
    return True


def mutation_oracle() -> int:
    case = ConditionalInductionCase(
        8, 0, 2, 1, 2, 8, 64, 2, tuple(range(7)),
        "ult", 6, True,
    )
    baseline = make_certificate(case)
    mutations: list[dict[str, Any]] = []
    for mutate in (
        lambda item: item.__setitem__("schema", "bad"),
        lambda item: item.__setitem__("base", 1),
        lambda item: item["induction"].__setitem__("seed", 1),
        lambda item: item["induction"].__setitem__("step", 1),
        lambda item: item["writer"].__setitem__("bytes", 1),
        lambda item: item["writer"].__setitem__("scale", 2),
        lambda item: item["writer"].__setitem__("address_stride", 3),
        lambda item: item["writer"]["reachable_addresses"].pop(),
        lambda item: item["writer"]["reachable_index_values"].pop(),
        lambda item: item["writer_guard"].__setitem__("predicate", "eq"),
        lambda item: item["writer_guard"].__setitem__("right", 5),
        lambda item: item["writer_guard"].__setitem__("writer_when", False),
        lambda item: item["writer_guard"]["memory_phi"].__setitem__(
            "skip", "writer-memory-def"
        ),
        lambda item: item["load_addresses"].__setitem__(0, 1),
        lambda item: item["witnesses"][0].__setitem__("writer_lane", 1),
        lambda item: item["witnesses"][0].__setitem__("induction_value", 2),
        lambda item: item["witnesses"][0]["writer_guard"].__setitem__(
            "equals", False
        ),
        lambda item: item["witnesses"].pop(),
    ):
        candidate = copy.deepcopy(baseline)
        mutate(candidate)
        mutations.append(candidate)
    if any(validate_certificate(candidate, case) for candidate in mutations):
        raise AssertionError("a mutated conditional certificate was accepted")
    return len(mutations)


def run_oracle() -> dict[str, Any]:
    cases = accepted = rejected = lane_checks = bitmap_checks = 0
    guard_true_checks = guard_false_checks = 0
    for object_bytes in range(2, 11):
        for step in range(1, 5):
            for scale in range(1, 3):
                address_stride = step * scale
                for writer_bytes in range(
                    1, min(3, address_stride, object_bytes) + 1
                ):
                    for load_bytes in range(1, min(3, object_bytes) + 1):
                        last_load = object_bytes - load_bytes
                        affine_probe = StridedInductionCase(
                            object_bytes, 0, step, scale, writer_bytes,
                            8, 16, load_bytes, (0,),
                        )
                        lane_map = writer_lane_map(affine_probe) or {}
                        covered = tuple(
                            offset
                            for offset in range(last_load + 1)
                            if all(
                                offset + lane in lane_map
                                for lane in range(load_bytes)
                            )
                        )
                        domains = {
                            tuple(range(last_load + 1)),
                            covered or (0,),
                            tuple(dict.fromkeys((0, last_load))),
                        }
                        guard_limits = tuple(dict.fromkeys(
                            (0, object_bytes // 2, object_bytes)
                        ))
                        for seed in (0, 1):
                            for bound_bits in (4, 8):
                                for offsets in domains:
                                    for predicate in PREDICATES:
                                        for writer_when in (False, True):
                                            for guard_limit in guard_limits:
                                                case = ConditionalInductionCase(
                                                    object_bytes, seed, step,
                                                    scale, writer_bytes,
                                                    bound_bits, 16, load_bytes,
                                                    offsets, predicate,
                                                    guard_limit, writer_when,
                                                )
                                                observed = validate_certificate(
                                                    make_certificate(case), case
                                                )
                                                expected = (
                                                    mathematically_supported(case)
                                                )
                                                if observed != expected:
                                                    raise AssertionError(
                                                        "conditional cover disagrees with math"
                                                    )
                                                cases += 1
                                                accepted += int(observed)
                                                rejected += int(not observed)
                                                lane_checks += (
                                                    len(offsets) * load_bytes
                                                )
                                                if not observed:
                                                    continue
                                                source_maximum = (
                                                    1 << bound_bits
                                                ) - 1
                                                for count in range(
                                                    min(
                                                        source_maximum,
                                                        object_bytes,
                                                    ) + 2
                                                ):
                                                    for offset in offsets:
                                                        bitmap = (
                                                            runtime_bitmap_accepts(
                                                                case,
                                                                count=count,
                                                                load_offset=offset,
                                                            )
                                                        )
                                                        carried = (
                                                            guard_carry_accepts(
                                                                case,
                                                                count=count,
                                                                load_offset=offset,
                                                            )
                                                        )
                                                        if bitmap != carried:
                                                            raise AssertionError(
                                                                "bitmap disagrees with guard carry"
                                                            )
                                                        bitmap_checks += 1
                                                        guard_true_checks += int(
                                                            carried
                                                        )
                                                        guard_false_checks += int(
                                                            not carried
                                                        )
    return {
        "schema": "symcc-conditional-loop-memoryphi-oracle-results-v1",
        "all_passed": True,
        "cases": cases,
        "accepted_conditional_covers": accepted,
        "rejected_unsupported_or_partial": rejected,
        "lane_checks": lane_checks,
        "runtime_bitmap_equivalences": bitmap_checks,
        "runtime_guard_true_accepts": guard_true_checks,
        "runtime_guard_or_prefix_rejects": guard_false_checks,
        "rejected_certificate_mutations": mutation_oracle(),
        "object_bytes": {"minimum": 2, "maximum": 10},
        "predicates": list(PREDICATES),
        "claim_boundary": (
            "finite single-latch conditional-writer guard carry and runtime "
            "bitmap semantics only; not LLVM latency, coverage, solver "
            "throughput, bug yield, or end-to-end speedup"
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
