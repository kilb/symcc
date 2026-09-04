#!/usr/bin/env python3
"""Independent ordered-writer transfer oracle for F415."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import json
from typing import Any, Mapping


SCHEMA = "symcc-loop-memoryphi-byte-lane-induction-v5"


@dataclass(frozen=True)
class OrderedWriterCase:
    object_bytes: int
    seed: int
    step: int
    bound_bits: int
    induction_bits: int
    load_bytes: int
    load_offsets: tuple[int, ...]
    transfers: tuple[tuple[int, ...], ...]


def _reachable(case: OrderedWriterCase, width: int) -> tuple[int, ...]:
    return tuple(range(case.seed, case.object_bytes - width + 1, case.step))


def _covered_lanes(case: OrderedWriterCase) -> set[int]:
    lanes: set[int] = set()
    for writers in case.transfers:
        for width in writers:
            for address in _reachable(case, width):
                lanes.update(range(address, address + width))
    return lanes


def mathematically_supported(case: OrderedWriterCase) -> bool:
    writer_count = sum(len(writers) for writers in case.transfers)
    source_maximum = (1 << case.bound_bits) - 1
    maximum_induction = max(
        (
            induction
            for writers in case.transfers
            for width in writers
            for induction in _reachable(case, width)
        ),
        default=-1,
    )
    covered = _covered_lanes(case)
    return (
        type(case.object_bytes) is int
        and 2 <= case.object_bytes <= 64
        and case.seed == 0
        and type(case.step) is int
        and 1 <= case.step <= 64
        and type(case.bound_bits) is int
        and 1 <= case.bound_bits <= 64
        and type(case.induction_bits) is int
        and 1 <= case.induction_bits <= 64
        and source_maximum >= maximum_induction + 1
        and source_maximum <= (1 << case.induction_bits) - case.step
        and 2 <= len(case.transfers) <= 4
        and all(isinstance(writers, tuple) for writers in case.transfers)
        and all(len(writers) <= 4 for writers in case.transfers)
        and any(len(writers) > 1 for writers in case.transfers)
        and 1 <= writer_count <= 16
        and all(
            type(width) is int and 1 <= width <= min(8, case.step, case.object_bytes)
            for writers in case.transfers
            for width in writers
        )
        and maximum_induction >= 0
        and type(case.load_bytes) is int
        and 1 <= case.load_bytes <= min(8, case.object_bytes)
        and bool(case.load_offsets)
        and len(set(case.load_offsets)) == len(case.load_offsets)
        and all(
            type(offset) is int and 0 <= offset <= case.object_bytes - case.load_bytes
            for offset in case.load_offsets
        )
        and all(
            offset + lane in covered
            for offset in case.load_offsets
            for lane in range(case.load_bytes)
        )
    )


def make_certificate(case: OrderedWriterCase) -> dict[str, Any]:
    domain = sorted(
        {
            induction
            for writers in case.transfers
            for width in writers
            for induction in _reachable(case, width)
        }
    )
    alternatives: dict[int, list[dict[str, int]]] = {}
    transfers = []
    for transfer_ordinal, widths in enumerate(case.transfers):
        transfer: dict[str, Any] = {
            "ordinal": transfer_ordinal,
            "kind": "writer" if widths else "carry",
        }
        if widths:
            transfer["writers"] = []
            for writer_ordinal, width in enumerate(widths):
                reachable = _reachable(case, width)
                transfer["writers"].append(
                    {
                        "ordinal": writer_ordinal,
                        "bytes": width,
                        "address_stride": case.step,
                        "reachable_addresses": list(reachable),
                        "reachable_index_values": list(reachable),
                    }
                )
                for induction in reachable:
                    for writer_lane in range(width):
                        alternatives.setdefault(induction + writer_lane, []).append(
                            {
                                "transfer": transfer_ordinal,
                                "writer": writer_ordinal,
                                "writer_address": induction,
                                "writer_lane": writer_lane,
                                "induction_value": induction,
                            }
                        )
        transfers.append(transfer)

    closed: set[int] = set()
    rounds = []
    for induction in domain:
        before = len(closed)
        for writers in case.transfers:
            for width in writers:
                if induction in _reachable(case, width):
                    closed.update(range(induction, induction + width))
        rounds.append(
            {
                "induction_value": induction,
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
    witnesses = [
        {
            "load_address": load_address,
            "lane": lane,
            "alternatives": alternatives.get(load_address + lane, []),
        }
        for load_address in case.load_offsets
        for lane in range(case.load_bytes)
    ]
    return {
        "schema": SCHEMA,
        "induction": {
            "bits": case.induction_bits,
            "seed": case.seed,
            "step": case.step,
            "bound_bits": case.bound_bits,
        },
        "transfers": transfers,
        "memory_phi": {
            "incoming": [
                {
                    "transfer": ordinal,
                    "kind": (
                        "ordered-writer-memory-def-chain"
                        if writers
                        else "header-memory-phi-carry"
                    ),
                }
                for ordinal, writers in enumerate(case.transfers)
            ],
        },
        "fixed_point": {
            "algorithm": "finite-monotone-byte-lane-union",
            "semantics": "mutually-exclusive-ordered-writer-transfer",
            "domain": domain,
            "rounds": rounds,
            "final_lanes": sorted(closed),
            "stable": True,
        },
        "load_addresses": list(case.load_offsets),
        "load_bytes": case.load_bytes,
        "witnesses": witnesses,
    }


def validate_certificate(raw: Mapping[str, Any], case: OrderedWriterCase) -> bool:
    return mathematically_supported(case) and raw == make_certificate(case)


def runtime_state(
    case: OrderedWriterCase,
    *,
    count: int,
    choices: tuple[int, ...],
) -> tuple[set[int], dict[int, tuple[int, int, int]]]:
    initialized: set[int] = set()
    provenance: dict[int, tuple[int, int, int]] = {}
    for round_ordinal, induction in enumerate(range(case.seed, count, case.step)):
        transfer = choices[round_ordinal % len(choices)]
        for writer_ordinal, width in enumerate(case.transfers[transfer]):
            if induction + width > case.object_bytes:
                continue
            for lane in range(width):
                address = induction + lane
                initialized.add(address)
                provenance[address] = (
                    transfer,
                    writer_ordinal,
                    round_ordinal,
                )
    return initialized, provenance


def transfer_replay_state(
    case: OrderedWriterCase,
    *,
    count: int,
    choices: tuple[int, ...],
) -> tuple[set[int], dict[int, tuple[int, int, int]]]:
    operations: list[tuple[int, int, int, range]] = []
    for round_ordinal, induction in enumerate(range(case.seed, count, case.step)):
        transfer = choices[round_ordinal % len(choices)]
        operations.extend(
            (
                transfer,
                writer_ordinal,
                round_ordinal,
                range(induction, induction + width),
            )
            for writer_ordinal, width in enumerate(case.transfers[transfer])
            if induction + width <= case.object_bytes
        )
    provenance: dict[int, tuple[int, int, int]] = {}
    for transfer, writer, round_ordinal, addresses in operations:
        for address in addresses:
            provenance[address] = (transfer, writer, round_ordinal)
    return set(provenance), provenance


def mutation_oracle() -> int:
    case = OrderedWriterCase(
        8,
        0,
        2,
        8,
        64,
        2,
        tuple(range(7)),
        ((1, 2), (), (2, 1)),
    )
    baseline = make_certificate(case)
    mutations: list[dict[str, Any]] = []
    for mutate in (
        lambda item: item.__setitem__("schema", "bad"),
        lambda item: item["induction"].__setitem__("seed", 1),
        lambda item: item["induction"].__setitem__("step", 1),
        lambda item: item["transfers"].reverse(),
        lambda item: item["transfers"][0]["writers"].reverse(),
        lambda item: item["transfers"][0]["writers"].pop(),
        lambda item: item["transfers"][0]["writers"].append(
            copy.deepcopy(item["transfers"][0]["writers"][-1])
        ),
        lambda item: item["transfers"][0]["writers"][0].__setitem__("ordinal", 1),
        lambda item: item["transfers"][0]["writers"][0].__setitem__("bytes", 2),
        lambda item: item["transfers"][0]["writers"][0].__setitem__(
            "address_stride", 1
        ),
        lambda item: item["transfers"][0]["writers"][0]["reachable_addresses"].pop(),
        lambda item: item["memory_phi"]["incoming"][0].__setitem__(
            "kind", "writer-memory-def"
        ),
        lambda item: item["memory_phi"]["incoming"].reverse(),
        lambda item: item["fixed_point"].__setitem__(
            "semantics", "mutually-exclusive-backedge-transfer"
        ),
        lambda item: item["fixed_point"]["domain"].pop(),
        lambda item: item["fixed_point"]["rounds"][0].__setitem__("new_lanes", 0),
        lambda item: item["fixed_point"]["rounds"].pop(),
        lambda item: item["fixed_point"]["final_lanes"].pop(),
        lambda item: item["fixed_point"].__setitem__("stable", False),
        lambda item: item["witnesses"][0]["alternatives"][0].__setitem__("writer", 3),
        lambda item: item["witnesses"][0]["alternatives"].pop(),
        lambda item: item["witnesses"].pop(),
    ):
        candidate = copy.deepcopy(baseline)
        mutate(candidate)
        mutations.append(candidate)
    if any(validate_certificate(candidate, case) for candidate in mutations):
        raise AssertionError("a mutated ordered-writer certificate was accepted")
    return len(mutations)


def _shapes(transfer_count: int, wide: int) -> tuple[tuple[tuple[int, ...], ...], ...]:
    carries = ((),) * (transfer_count - 1)
    return (
        ((1, wide),) + carries,
        ((wide, 1), (wide,)) + ((),) * (transfer_count - 2),
        ((wide,) * 4,) * transfer_count,
        ((1,) * 5,) + carries,
    )


def run_oracle() -> dict[str, Any]:
    cases = accepted = rejected = lane_checks = 0
    bitmap_checks = provenance_checks = runtime_accepts = runtime_rejects = 0
    for object_bytes in range(2, 11):
        for step in range(1, 5):
            wide = min(step, 2)
            for load_bytes in range(1, min(3, object_bytes) + 1):
                last_load = object_bytes - load_bytes
                offset_domains = {
                    tuple(range(last_load + 1)),
                    tuple(dict.fromkeys((0, last_load))),
                    (0,),
                }
                for transfer_count in (2, 3, 4):
                    for transfers in _shapes(transfer_count, wide):
                        for seed in (0, 1):
                            for bound_bits in (4, 8):
                                for offsets in offset_domains:
                                    case = OrderedWriterCase(
                                        object_bytes,
                                        seed,
                                        step,
                                        bound_bits,
                                        16,
                                        load_bytes,
                                        offsets,
                                        transfers,
                                    )
                                    observed = validate_certificate(
                                        make_certificate(case), case
                                    )
                                    expected = mathematically_supported(case)
                                    if observed != expected:
                                        raise AssertionError(
                                            "ordered-writer oracle disagrees with math"
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
                                    for count in range(0, object_bytes + step + 1):
                                        for load_offset in offsets:
                                            for choices in schedules:
                                                runtime = runtime_state(
                                                    case,
                                                    count=count,
                                                    choices=choices,
                                                )
                                                replay = transfer_replay_state(
                                                    case,
                                                    count=count,
                                                    choices=choices,
                                                )
                                                if runtime != replay:
                                                    raise AssertionError(
                                                        "ordered transfer replay disagrees with runtime"
                                                    )
                                                bitmap_checks += 1
                                                provenance_checks += len(runtime[1])
                                                accepted_load = all(
                                                    load_offset + lane in runtime[0]
                                                    for lane in range(load_bytes)
                                                )
                                                runtime_accepts += int(accepted_load)
                                                runtime_rejects += int(
                                                    not accepted_load
                                                )
    return {
        "schema": "symcc-ordered-multilatch-memoryphi-oracle-v1",
        "all_passed": True,
        "cases": cases,
        "accepted_ordered_fixed_points": accepted,
        "rejected_unsupported_or_partial": rejected,
        "byte_lane_checks": lane_checks,
        "runtime_bitmap_equivalences": bitmap_checks,
        "last_writer_provenance_equivalences": provenance_checks,
        "runtime_complete_load_accepts": runtime_accepts,
        "runtime_prefix_or_carry_rejects": runtime_rejects,
        "mutations_rejected": mutation_oracle(),
        "claim_boundary": (
            "Finite reference-model bitmap and last-writer equivalence only; "
            "not LLVM build cost, coverage, solver speed, or defect yield"
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
