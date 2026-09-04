#!/usr/bin/env python3
"""Independent finite-domain oracle for F412 strided MemoryPhi covers."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import json
from typing import Any, Mapping


SCHEMA = "symcc-loop-memoryphi-byte-lane-induction-v2"


@dataclass(frozen=True)
class StridedInductionCase:
    object_bytes: int
    seed: int
    step: int
    scale: int
    writer_bytes: int
    bound_bits: int
    induction_bits: int
    load_bytes: int
    load_offsets: tuple[int, ...]


def writer_aliases(
    case: StridedInductionCase,
) -> tuple[tuple[int, int], ...]:
    if case.scale <= 0 or not 1 <= case.writer_bytes <= case.object_bytes:
        return ()
    maximum_index = (case.object_bytes - case.writer_bytes) // case.scale
    return tuple(
        (case.scale * index, index)
        for index in range(maximum_index + 1)
    )


def reachable_writers(
    case: StridedInductionCase,
) -> tuple[tuple[int, int], ...]:
    if case.step <= 0:
        return ()
    return tuple(
        (address, index)
        for address, index in writer_aliases(case)
        if index >= case.seed and (index - case.seed) % case.step == 0
    )


def writer_lane_map(
    case: StridedInductionCase,
) -> dict[int, tuple[int, int, int]] | None:
    result: dict[int, tuple[int, int, int]] = {}
    for address, index in reachable_writers(case):
        for lane in range(case.writer_bytes):
            byte_address = address + lane
            if byte_address in result:
                return None
            result[byte_address] = (address, lane, index)
    return result


def mathematically_supported(case: StridedInductionCase) -> bool:
    reachable = reachable_writers(case)
    lane_map = writer_lane_map(case)
    source_maximum = (1 << case.bound_bits) - 1
    induction_limit = (1 << case.induction_bits) - case.step
    generalized = (
        case.step != 1 or case.scale != 1 or case.writer_bytes != 1
    )
    return (
        generalized
        and case.seed == 0
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
    )


def make_certificate(case: StridedInductionCase) -> dict[str, Any]:
    aliases = writer_aliases(case)
    reachable = reachable_writers(case)
    lane_map = writer_lane_map(case) or {}
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
            "residue_origin": None if not reachable else reachable[0][0],
            "covered_residues": list(range(case.writer_bytes)),
        },
        "load_addresses": list(case.load_offsets),
        "witnesses": witnesses,
    }


def validate_certificate(
    raw: Mapping[str, Any], case: StridedInductionCase
) -> bool:
    if not mathematically_supported(case):
        return False
    expected = make_certificate(case)
    return raw == expected


def runtime_bitmap_accepts(
    case: StridedInductionCase, *, count: int, load_offset: int
) -> bool:
    initialized: set[int] = set()
    for address, index in reachable_writers(case):
        if index >= count:
            continue
        initialized.update(range(address, address + case.writer_bytes))
    return all(
        load_offset + lane in initialized
        for lane in range(case.load_bytes)
    )


def residue_formula_accepts(
    case: StridedInductionCase, *, count: int, load_offset: int
) -> bool:
    lane_map = writer_lane_map(case)
    if lane_map is None:
        return False
    for lane in range(case.load_bytes):
        owner = lane_map.get(load_offset + lane)
        if owner is None or owner[2] >= count:
            return False
    return True


def mutation_oracle() -> int:
    case = StridedInductionCase(8, 0, 2, 1, 2, 8, 64, 2, tuple(range(7)))
    baseline = make_certificate(case)
    mutations: list[dict[str, Any]] = []
    for mutate in (
        lambda item: item.__setitem__("schema", "bad"),
        lambda item: item.__setitem__("base", 1),
        lambda item: item["induction"].__setitem__("seed", 1),
        lambda item: item["induction"].__setitem__("step", 3),
        lambda item: item["induction"].__setitem__("bound_bits", 2),
        lambda item: item["writer"].__setitem__("bytes", 1),
        lambda item: item["writer"].__setitem__("scale", 2),
        lambda item: item["writer"].__setitem__("address_stride", 3),
        lambda item: item["writer"]["reachable_addresses"].pop(),
        lambda item: item["writer"]["reachable_index_values"].__setitem__(0, 1),
        lambda item: item["writer"]["covered_residues"].pop(),
        lambda item: item["load_addresses"].__setitem__(0, 1),
        lambda item: item["witnesses"][0].__setitem__("writer_lane", 1),
        lambda item: item["witnesses"][0].__setitem__("induction_value", 2),
        lambda item: item["witnesses"].pop(),
    ):
        candidate = copy.deepcopy(baseline)
        mutate(candidate)
        mutations.append(candidate)
    if any(validate_certificate(candidate, case) for candidate in mutations):
        raise AssertionError("a mutated strided certificate was accepted")
    return len(mutations)


def run_oracle() -> dict[str, Any]:
    cases = accepted = rejected = lane_checks = bitmap_checks = 0
    for object_bytes in range(2, 13):
        for step in range(1, 5):
            for scale in range(1, 4):
                address_stride = step * scale
                for writer_bytes in range(
                    1, min(4, address_stride, object_bytes) + 1
                ):
                    for load_bytes in range(1, min(4, object_bytes) + 1):
                        last_load = object_bytes - load_bytes
                        probe = StridedInductionCase(
                            object_bytes, 0, step, scale, writer_bytes,
                            8, 16, load_bytes, (0,),
                        )
                        lane_map = writer_lane_map(probe) or {}
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
                        for seed in (0, 1):
                            for bound_bits in (2, 4, 8):
                                for offsets in domains:
                                    case = StridedInductionCase(
                                        object_bytes, seed, step, scale,
                                        writer_bytes, bound_bits, 16,
                                        load_bytes, offsets,
                                    )
                                    observed = validate_certificate(
                                        make_certificate(case), case
                                    )
                                    expected = mathematically_supported(case)
                                    if observed != expected:
                                        raise AssertionError(
                                            "strided cover disagrees with math"
                                        )
                                    cases += 1
                                    accepted += int(observed)
                                    rejected += int(not observed)
                                    lane_checks += len(offsets) * load_bytes
                                    if observed:
                                        source_maximum = (
                                            1 << bound_bits
                                        ) - 1
                                        for count in range(
                                            min(source_maximum, object_bytes)
                                            + 2
                                        ):
                                            for offset in offsets:
                                                if runtime_bitmap_accepts(
                                                    case,
                                                    count=count,
                                                    load_offset=offset,
                                                ) != residue_formula_accepts(
                                                    case,
                                                    count=count,
                                                    load_offset=offset,
                                                ):
                                                    raise AssertionError(
                                                        "bitmap disagrees with residue formula"
                                                    )
                                                bitmap_checks += 1
    return {
        "schema": "symcc-strided-loop-memoryphi-oracle-results-v1",
        "all_passed": True,
        "cases": cases,
        "accepted_strided_covers": accepted,
        "rejected_unsupported_or_partial": rejected,
        "lane_checks": lane_checks,
        "runtime_bitmap_equivalences": bitmap_checks,
        "rejected_certificate_mutations": mutation_oracle(),
        "object_bytes": {"minimum": 2, "maximum": 12},
        "claim_boundary": (
            "finite positive constant strides, non-overlapping scalar "
            "writers, residue-class lane cover, and bitmap semantics only; "
            "not LLVM latency, coverage, solver throughput, bug yield, or "
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
