#!/usr/bin/env python3
"""Independent finite-transfer/fixed-point oracle for F414."""

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


SCHEMA = "symcc-loop-memoryphi-byte-lane-induction-v4"


@dataclass(frozen=True)
class MultiLatchCase:
    object_bytes: int
    seed: int
    step: int
    scale: int
    writer_bytes: int
    bound_bits: int
    induction_bits: int
    load_bytes: int
    load_offsets: tuple[int, ...]
    writer_transfers: tuple[bool, ...]

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


def mathematically_supported(case: MultiLatchCase) -> bool:
    reachable = reachable_writers(case.affine())
    lane_map = writer_lane_map(case.affine())
    source_maximum = (1 << case.bound_bits) - 1
    induction_limit = (1 << case.induction_bits) - case.step
    return (
        case.seed == 0
        and 1 <= case.step <= 64
        and case.scale > 0
        and 1 <= case.writer_bytes <= min(
            8, case.step * case.scale, case.object_bytes
        )
        and 2 <= len(case.writer_transfers) <= 4
        and all(isinstance(writer, bool) for writer in case.writer_transfers)
        and any(case.writer_transfers)
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


def make_certificate(case: MultiLatchCase) -> dict[str, Any]:
    aliases = writer_aliases(case.affine())
    reachable = reachable_writers(case.affine())
    domain = [index for _, index in reachable]
    transfers = []
    lane_alternatives: dict[int, list[dict[str, int]]] = {}
    for transfer, is_writer in enumerate(case.writer_transfers):
        item: dict[str, Any] = {
            "ordinal": transfer,
            "kind": "writer" if is_writer else "carry",
        }
        if is_writer:
            item["writer"] = {
                "bytes": case.writer_bytes,
                "scale": case.scale,
                "address_stride": case.step * case.scale,
                "addresses": [address for address, _ in aliases],
                "index_values": [index for _, index in aliases],
                "reachable_addresses": [address for address, _ in reachable],
                "reachable_index_values": domain,
            }
            for writer_address, induction_value in reachable:
                for writer_lane in range(case.writer_bytes):
                    lane_alternatives.setdefault(
                        writer_address + writer_lane, []
                    ).append({
                        "transfer": transfer,
                        "writer_address": writer_address,
                        "writer_lane": writer_lane,
                        "induction_value": induction_value,
                    })
        transfers.append(item)
    closed: set[int] = set()
    rounds = []
    for writer_address, induction_value in reachable:
        before = len(closed)
        closed.update(range(writer_address, writer_address + case.writer_bytes))
        rounds.append({
            "induction_value": induction_value,
            "new_lanes": len(closed) - before,
            "total_lanes": len(closed),
        })
    rounds.append({
        "kind": "stability-check",
        "new_lanes": 0,
        "total_lanes": len(closed),
    })
    witnesses = []
    for load_address in case.load_offsets:
        for lane in range(case.load_bytes):
            witnesses.append({
                "load_address": load_address,
                "lane": lane,
                "alternatives": lane_alternatives.get(
                    load_address + lane, []
                ),
            })
    return {
        "schema": SCHEMA,
        "induction": {
            "bits": case.induction_bits,
            "seed": case.seed,
            "step": case.step,
            "bound_bits": case.bound_bits,
        },
        "transfers": transfers,
        "fixed_point": {
            "algorithm": "finite-monotone-byte-lane-union",
            "semantics": "mutually-exclusive-backedge-transfer",
            "domain": domain,
            "rounds": rounds,
            "final_lanes": sorted(closed),
            "stable": True,
        },
        "load_addresses": list(case.load_offsets),
        "load_bytes": case.load_bytes,
        "witnesses": witnesses,
    }


def validate_certificate(raw: Mapping[str, Any], case: MultiLatchCase) -> bool:
    return mathematically_supported(case) and raw == make_certificate(case)


def runtime_bitmap_accepts(
    case: MultiLatchCase,
    *,
    count: int,
    load_offset: int,
    choices: tuple[int, ...],
) -> bool:
    initialized: set[int] = set()
    for round_index, (address, induction) in enumerate(
        reachable_writers(case.affine())
    ):
        if induction >= count:
            continue
        transfer = choices[round_index % len(choices)]
        if case.writer_transfers[transfer]:
            initialized.update(range(address, address + case.writer_bytes))
    return all(
        load_offset + lane in initialized
        for lane in range(case.load_bytes)
    )


def transfer_equation_accepts(
    case: MultiLatchCase,
    *,
    count: int,
    load_offset: int,
    choices: tuple[int, ...],
) -> bool:
    effects: list[set[int]] = []
    for round_index, (address, induction) in enumerate(
        reachable_writers(case.affine())
    ):
        transfer = choices[round_index % len(choices)]
        effect = set()
        if induction < count and case.writer_transfers[transfer]:
            effect.update(range(address, address + case.writer_bytes))
        effects.append(effect)
    fixed_point = set().union(*effects) if effects else set()
    return set(range(load_offset, load_offset + case.load_bytes)) <= fixed_point


def mutation_oracle() -> int:
    case = MultiLatchCase(
        8, 0, 2, 1, 2, 8, 64, 2, tuple(range(7)),
        (True, False, True, False),
    )
    baseline = make_certificate(case)
    mutations: list[dict[str, Any]] = []
    for mutate in (
        lambda item: item.__setitem__("schema", "bad"),
        lambda item: item["induction"].__setitem__("seed", 1),
        lambda item: item["induction"].__setitem__("step", 1),
        lambda item: item["transfers"][0].__setitem__("kind", "carry"),
        lambda item: item["transfers"][1].__setitem__("kind", "writer"),
        lambda item: item["transfers"].reverse(),
        lambda item: item["transfers"][0]["writer"].__setitem__("bytes", 1),
        lambda item: item["transfers"][0]["writer"].__setitem__("scale", 2),
        lambda item: item["transfers"][0]["writer"].__setitem__(
            "address_stride", 3
        ),
        lambda item: item["transfers"][0]["writer"][
            "reachable_addresses"
        ].pop(),
        lambda item: item["fixed_point"]["domain"].pop(),
        lambda item: item["fixed_point"]["rounds"][0].__setitem__(
            "new_lanes", 0
        ),
        lambda item: item["fixed_point"]["rounds"].pop(),
        lambda item: item["fixed_point"]["final_lanes"].pop(),
        lambda item: item["fixed_point"].__setitem__("stable", False),
        lambda item: item["witnesses"][0]["alternatives"][0].__setitem__(
            "transfer", 1
        ),
        lambda item: item["witnesses"][0]["alternatives"].pop(),
        lambda item: item["witnesses"].pop(),
    ):
        candidate = copy.deepcopy(baseline)
        mutate(candidate)
        mutations.append(candidate)
    if any(validate_certificate(candidate, case) for candidate in mutations):
        raise AssertionError("a mutated multi-latch certificate was accepted")
    return len(mutations)


def run_oracle() -> dict[str, Any]:
    cases = accepted = rejected = lane_checks = bitmap_checks = 0
    writer_path_accepts = carry_or_prefix_rejects = 0
    for object_bytes in range(2, 11):
        for step in range(1, 5):
            for scale in range(1, 3):
                address_stride = step * scale
                for writer_bytes in range(
                    1, min(3, address_stride, object_bytes) + 1
                ):
                    for load_bytes in range(1, min(3, object_bytes) + 1):
                        last_load = object_bytes - load_bytes
                        probe = StridedInductionCase(
                            object_bytes, 0, step, scale, writer_bytes,
                            8, 16, load_bytes, (0,),
                        )
                        lane_map = writer_lane_map(probe) or {}
                        covered = tuple(
                            offset for offset in range(last_load + 1)
                            if all(offset + lane in lane_map
                                   for lane in range(load_bytes))
                        )
                        offset_domains = {
                            tuple(range(last_load + 1)),
                            covered or (0,),
                            tuple(dict.fromkeys((0, last_load))),
                        }
                        for transfer_count in (2, 3, 4):
                            masks = {
                                (True,) + (False,) * (transfer_count - 1),
                                (True,) * transfer_count,
                                tuple(index % 2 == 0
                                      for index in range(transfer_count)),
                            }
                            for seed in (0, 1):
                                for bound_bits in (4, 8):
                                    for offsets in offset_domains:
                                        for mask in masks:
                                            case = MultiLatchCase(
                                                object_bytes, seed, step, scale,
                                                writer_bytes, bound_bits, 16,
                                                load_bytes, offsets, mask,
                                            )
                                            observed = validate_certificate(
                                                make_certificate(case), case
                                            )
                                            expected = mathematically_supported(case)
                                            if observed != expected:
                                                raise AssertionError(
                                                    "fixed-point oracle disagrees with math"
                                                )
                                            cases += 1
                                            accepted += int(observed)
                                            rejected += int(not observed)
                                            lane_checks += len(offsets) * load_bytes
                                            if not observed:
                                                continue
                                            schedules = (
                                                tuple(range(transfer_count)),
                                                (0,),
                                                (transfer_count - 1,),
                                            )
                                            for count in range(
                                                0, object_bytes + step + 1
                                            ):
                                                for load_offset in offsets:
                                                    for choices in schedules:
                                                        runtime = runtime_bitmap_accepts(
                                                            case,
                                                            count=count,
                                                            load_offset=load_offset,
                                                            choices=choices,
                                                        )
                                                        replay = transfer_equation_accepts(
                                                            case,
                                                            count=count,
                                                            load_offset=load_offset,
                                                            choices=choices,
                                                        )
                                                        if runtime != replay:
                                                            raise AssertionError(
                                                                "transfer replay disagrees with bitmap"
                                                            )
                                                        bitmap_checks += 1
                                                        writer_path_accepts += int(runtime)
                                                        carry_or_prefix_rejects += int(
                                                            not runtime
                                                        )
    return {
        "schema": "symcc-multilatch-loop-memoryphi-oracle-v1",
        "all_passed": True,
        "cases": cases,
        "accepted_fixed_points": accepted,
        "rejected_unsupported_or_partial": rejected,
        "byte_lane_checks": lane_checks,
        "runtime_bitmap_equivalences": bitmap_checks,
        "runtime_writer_path_accepts": writer_path_accepts,
        "runtime_carry_or_prefix_rejects": carry_or_prefix_rejects,
        "mutations_rejected": mutation_oracle(),
        "claim_boundary": (
            "Finite reference-model equivalence only; not LLVM build cost, "
            "end-to-end coverage, solver speed, or defect yield"
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
