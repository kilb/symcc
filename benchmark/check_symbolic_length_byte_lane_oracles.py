#!/usr/bin/env python3
"""Independent finite-domain oracle for F410 byte-lane cover certificates."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import json
from typing import Any, Mapping


SCHEMA = "symcc-symbolic-length-byte-lane-cover-v1"


@dataclass(frozen=True)
class CoverCase:
    object_bytes: int
    writer_offset: int
    maximum_bytes: int
    load_bytes: int
    load_offsets: tuple[int, ...]


def make_certificate(case: CoverCase) -> dict[str, Any]:
    addresses = list(case.load_offsets)
    return {
        "schema": SCHEMA,
        "base": 0,
        "load_bytes": case.load_bytes,
        "writer": {
            "address": case.writer_offset,
            "maximum_bytes": case.maximum_bytes,
        },
        "index": {
            "bits": 8,
            "minimum": min(case.load_offsets),
            "maximum": max(case.load_offsets),
            "values": list(case.load_offsets),
        },
        "load_addresses": addresses,
        "lanes": [
            {
                "address": address,
                "lane": lane,
                "region_offset": address + lane - case.writer_offset,
            }
            for address in addresses
            for lane in range(case.load_bytes)
        ],
    }


def validate_certificate(
    raw: Mapping[str, Any], case: CoverCase
) -> bool:
    if set(raw) != {
        "schema", "base", "load_bytes", "writer", "index",
        "load_addresses", "lanes",
    } or raw.get("schema") != SCHEMA:
        return False
    if raw.get("base") != 0 or raw.get("load_bytes") != case.load_bytes:
        return False
    writer = raw.get("writer")
    index = raw.get("index")
    addresses = raw.get("load_addresses")
    lanes = raw.get("lanes")
    if (
        not isinstance(writer, dict)
        or set(writer) != {"address", "maximum_bytes"}
        or writer.get("address") != case.writer_offset
        or writer.get("maximum_bytes") != case.maximum_bytes
        or not isinstance(index, dict)
        or set(index) != {"bits", "minimum", "maximum", "values"}
        or index.get("bits") != 8
        or index.get("minimum") != min(case.load_offsets)
        or index.get("maximum") != max(case.load_offsets)
        or index.get("values") != list(case.load_offsets)
        or addresses != list(case.load_offsets)
        or len(set(addresses)) != len(addresses)
        or not isinstance(lanes, list)
    ):
        return False
    expected_lanes = [
        {
            "address": address,
            "lane": lane,
            "region_offset": address + lane - case.writer_offset,
        }
        for address in case.load_offsets
        for lane in range(case.load_bytes)
    ]
    if lanes != expected_lanes:
        return False
    writer_end = case.writer_offset + case.maximum_bytes
    return (
        0 <= case.writer_offset < case.object_bytes
        and 1 <= case.maximum_bytes <= 64
        and writer_end <= case.object_bytes
        and all(
            case.writer_offset <= address
            and address + case.load_bytes <= writer_end
            for address in case.load_offsets
        )
    )


def runtime_lane_guard(
    case: CoverCase, *, load_offset: int, symbolic_length: int
) -> bool:
    return all(
        (load_offset + lane - case.writer_offset) < symbolic_length
        for lane in range(case.load_bytes)
    )


def mutation_oracle() -> int:
    case = CoverCase(8, 0, 8, 2, (0, 1, 2, 3, 4, 5, 6))
    baseline = make_certificate(case)
    mutations: list[dict[str, Any]] = []
    for mutate in (
        lambda item: item.__setitem__("schema", "bad"),
        lambda item: item.__setitem__("base", 1),
        lambda item: item.__setitem__("load_bytes", 1),
        lambda item: item["writer"].__setitem__("address", 1),
        lambda item: item["writer"].__setitem__("maximum_bytes", 7),
        lambda item: item["index"]["values"].__setitem__(0, 1),
        lambda item: item["load_addresses"].__setitem__(0, 1),
        lambda item: item["lanes"][0].__setitem__("region_offset", 1),
        lambda item: item["lanes"].pop(),
        lambda item: item["load_addresses"].append(6),
    ):
        candidate = copy.deepcopy(baseline)
        mutate(candidate)
        mutations.append(candidate)
    if any(validate_certificate(candidate, case) for candidate in mutations):
        raise AssertionError("a mutated byte-lane certificate was accepted")
    return len(mutations)


def run_oracle() -> dict[str, Any]:
    cases = 0
    accepted = 0
    rejected = 0
    lane_checks = 0
    guard_equivalences = 0
    for object_bytes in range(2, 13):
        for load_bytes in range(1, min(8, object_bytes) + 1):
            last_load = object_bytes - load_bytes
            for writer_offset in range(object_bytes):
                for maximum_bytes in range(
                    1, min(8, object_bytes - writer_offset) + 1
                ):
                    for domain_start in range(last_load + 1):
                        for domain_size in range(
                            1, min(4, last_load - domain_start + 1) + 1
                        ):
                            offsets = tuple(range(
                                domain_start,
                                domain_start + domain_size,
                            ))
                            case = CoverCase(
                                object_bytes,
                                writer_offset,
                                maximum_bytes,
                                load_bytes,
                                offsets,
                            )
                            certificate = make_certificate(case)
                            observed = validate_certificate(certificate, case)
                            expected = all(
                                writer_offset <= offset
                                and offset + load_bytes
                                <= writer_offset + maximum_bytes
                                for offset in offsets
                            )
                            if observed != expected:
                                raise AssertionError(
                                    "finite-domain cover disagrees with interval semantics"
                                )
                            cases += 1
                            lane_checks += len(offsets) * load_bytes
                            accepted += int(observed)
                            rejected += int(not observed)
                            if observed:
                                for offset in offsets:
                                    for length in range(maximum_bytes + 1):
                                        expected_guard = (
                                            offset + load_bytes
                                            <= writer_offset + length
                                        )
                                        if runtime_lane_guard(
                                            case,
                                            load_offset=offset,
                                            symbolic_length=length,
                                        ) != expected_guard:
                                            raise AssertionError(
                                                "lane guards disagree with half-open interval semantics"
                                            )
                                        guard_equivalences += 1
    return {
        "schema": "symcc-symbolic-length-byte-lane-oracle-results-v1",
        "all_passed": True,
        "object_bytes": {"minimum": 2, "maximum": 12},
        "load_bytes": {"minimum": 1, "maximum": 8},
        "cases": cases,
        "accepted_covers": accepted,
        "rejected_non_covers": rejected,
        "lane_checks": lane_checks,
        "runtime_guard_equivalences": guard_equivalences,
        "rejected_certificate_mutations": mutation_oracle(),
        "claim_boundary": (
            "finite contiguous byte domains, capacity at most 64, and "
            "half-open conditional lane guards; not LLVM analysis latency, "
            "solver throughput, coverage, bug yield, or end-to-end speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = json.dumps(
        run_oracle(), sort_keys=True, separators=(",", ":")
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
