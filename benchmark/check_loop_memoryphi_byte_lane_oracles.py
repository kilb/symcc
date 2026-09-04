#!/usr/bin/env python3
"""Independent finite-domain oracle for F411 loop MemoryPhi certificates."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import json
from typing import Any, Mapping


SCHEMA = "symcc-loop-memoryphi-byte-lane-induction-v1"


@dataclass(frozen=True)
class InductionCase:
    object_bytes: int
    seed: int
    step: int
    bound_bits: int
    load_bytes: int
    load_offsets: tuple[int, ...]


def writer_domain(case: InductionCase) -> tuple[tuple[int, int], ...]:
    if case.step <= 0:
        return ()
    return tuple(
        (index, index)
        for index in range(case.seed, case.object_bytes, case.step)
    )


def make_certificate(case: InductionCase) -> dict[str, Any]:
    writers = writer_domain(case)
    writer_by_address = {address: index for address, index in writers}
    return {
        "schema": SCHEMA,
        "base": 0,
        "load_bytes": case.load_bytes,
        "induction": {
            "bits": 64,
            "seed": case.seed,
            "step": case.step,
            "bound_bits": case.bound_bits,
        },
        "writer": {
            "addresses": [address for address, _ in writers],
            "index_values": [index for _, index in writers],
        },
        "load_addresses": list(case.load_offsets),
        "witnesses": [
            {
                "load_address": address,
                "lane": lane,
                "writer_address": address + lane,
                "induction_value": writer_by_address.get(address + lane),
            }
            for address in case.load_offsets
            for lane in range(case.load_bytes)
        ],
    }


def validate_certificate(
    raw: Mapping[str, Any], case: InductionCase
) -> bool:
    if set(raw) != {
        "schema", "base", "load_bytes", "induction", "writer",
        "load_addresses", "witnesses",
    } or raw.get("schema") != SCHEMA:
        return False
    induction = raw.get("induction")
    writer = raw.get("writer")
    if (
        raw.get("base") != 0
        or raw.get("load_bytes") != case.load_bytes
        or induction != {
            "bits": 64,
            "seed": case.seed,
            "step": case.step,
            "bound_bits": case.bound_bits,
        }
        or not isinstance(writer, dict)
        or set(writer) != {"addresses", "index_values"}
        or raw.get("load_addresses") != list(case.load_offsets)
    ):
        return False
    writers = writer_domain(case)
    addresses = [address for address, _ in writers]
    values = [index for _, index in writers]
    if (
        writer.get("addresses") != addresses
        or writer.get("index_values") != values
        or len(set(addresses)) != len(addresses)
        or len(set(case.load_offsets)) != len(case.load_offsets)
    ):
        return False
    writer_by_address = dict(zip(addresses, values))
    expected = []
    for address in case.load_offsets:
        for lane in range(case.load_bytes):
            writer_address = address + lane
            if writer_address not in writer_by_address:
                return False
            expected.append({
                "load_address": address,
                "lane": lane,
                "writer_address": writer_address,
                "induction_value": writer_by_address[writer_address],
            })
    return (
        raw.get("witnesses") == expected
        and case.seed == 0
        and case.step == 1
        and (1 << case.bound_bits) - 1 >= case.object_bytes
        and all(
            0 <= offset <= case.object_bytes - case.load_bytes
            for offset in case.load_offsets
        )
    )


def runtime_bitmap_accepts(
    case: InductionCase, *, count: int, load_offset: int
) -> bool:
    initialized = {
        index
        for index in range(case.seed, min(count, case.object_bytes), case.step)
    }
    return all(
        load_offset + lane in initialized
        for lane in range(case.load_bytes)
    )


def mutation_oracle() -> int:
    case = InductionCase(8, 0, 1, 8, 2, tuple(range(7)))
    baseline = make_certificate(case)
    mutations: list[dict[str, Any]] = []
    for mutate in (
        lambda item: item.__setitem__("schema", "bad"),
        lambda item: item.__setitem__("base", 1),
        lambda item: item.__setitem__("load_bytes", 1),
        lambda item: item["induction"].__setitem__("seed", 1),
        lambda item: item["induction"].__setitem__("step", 2),
        lambda item: item["induction"].__setitem__("bound_bits", 2),
        lambda item: item["writer"]["addresses"].pop(),
        lambda item: item["writer"]["index_values"].__setitem__(0, 1),
        lambda item: item["load_addresses"].__setitem__(0, 1),
        lambda item: item["witnesses"][0].__setitem__("lane", 1),
        lambda item: item["witnesses"][0].__setitem__(
            "induction_value", 1
        ),
        lambda item: item["witnesses"].pop(),
    ):
        candidate = copy.deepcopy(baseline)
        mutate(candidate)
        mutations.append(candidate)
    if any(validate_certificate(candidate, case) for candidate in mutations):
        raise AssertionError("a mutated loop induction certificate was accepted")
    return len(mutations)


def run_oracle() -> dict[str, Any]:
    cases = accepted = rejected = lane_checks = bitmap_checks = 0
    for object_bytes in range(2, 17):
        for load_bytes in range(1, min(8, object_bytes) + 1):
            last_load = object_bytes - load_bytes
            domains = (
                tuple(range(last_load + 1)),
                (0,),
                (last_load,),
            )
            for seed in range(3):
                for step in range(1, 4):
                    for bound_bits in (2, 4, 8):
                        for offsets in domains:
                            case = InductionCase(
                                object_bytes, seed, step, bound_bits,
                                load_bytes, offsets,
                            )
                            observed = validate_certificate(
                                make_certificate(case), case
                            )
                            expected = (
                                seed == 0
                                and step == 1
                                and (1 << bound_bits) - 1 >= object_bytes
                            )
                            if observed != expected:
                                raise AssertionError(
                                    "induction cover disagrees with finite domain"
                                )
                            cases += 1
                            accepted += int(observed)
                            rejected += int(not observed)
                            lane_checks += len(offsets) * load_bytes
                            if observed:
                                for count in range(object_bytes + 2):
                                    for offset in offsets:
                                        expected_runtime = (
                                            offset + load_bytes
                                            <= min(count, object_bytes)
                                        )
                                        if runtime_bitmap_accepts(
                                            case,
                                            count=count,
                                            load_offset=offset,
                                        ) != expected_runtime:
                                            raise AssertionError(
                                                "runtime byte bitmap disagrees with induction"
                                            )
                                        bitmap_checks += 1
    return {
        "schema": "symcc-loop-memoryphi-byte-lane-oracle-results-v1",
        "all_passed": True,
        "cases": cases,
        "accepted_canonical_covers": accepted,
        "rejected_noncanonical_or_partial": rejected,
        "lane_checks": lane_checks,
        "runtime_bitmap_equivalences": bitmap_checks,
        "rejected_certificate_mutations": mutation_oracle(),
        "object_bytes": {"minimum": 2, "maximum": 16},
        "load_bytes": {"minimum": 1, "maximum": 8},
        "claim_boundary": (
            "finite canonical single-latch unit-step loops and byte-init "
            "bitmap semantics only; not LLVM analysis latency, solver "
            "throughput, coverage, bug yield, or end-to-end speedup"
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
