#!/usr/bin/env python3
"""Independent finite-domain oracle for F409 heap effect summaries."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Interval:
    offset: int
    width: int

    @property
    def end(self) -> int:
        return self.offset + self.width


def covers(store: Interval, load: Interval) -> bool:
    return store.offset <= load.offset and load.end <= store.end


def validate_artifact(artifact: dict[str, Any]) -> bool:
    try:
        if artifact["schema"] != "symcc-interprocedural-effect-oracle-v1":
            return False
        if artifact["capability"] is not True:
            return False
        callsite = artifact["callsite"]
        if not isinstance(callsite, int) or isinstance(callsite, bool):
            return False
        if callsite < 0 or callsite >= artifact["callsite_count"]:
            return False
        if artifact["callee"] != "initialize":
            return False
        if artifact["parameter"] != 0:
            return False
        if artifact["straight_line"] is not True:
            return False
        if artifact["returns"] != 1 or artifact["writers"] != 1:
            return False
        size = artifact["object_size"]
        load = Interval(*artifact["load"])
        store = Interval(*artifact["store"])
        if (
            not 1 <= size <= 8
            or load.offset < 0
            or store.offset < 0
            or load.width < 1
            or store.width < 1
            or load.end > size
            or store.end > size
            or not covers(store, load)
        ):
            return False
        identity = artifact["identity"]
        return (
            identity == {
                "base": artifact["base"],
                "actual": artifact["base"],
                "store_address": artifact["base"] + store.offset,
                "load_address": artifact["base"] + load.offset,
            }
            and artifact["memory_chain"] == ["terminal-call"]
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def valid_artifact(
    *, callsites: int, callsite: int, size: int, load: Interval,
    store: Interval,
) -> dict[str, Any]:
    base = 0x1000 + callsite * 0x20
    return {
        "schema": "symcc-interprocedural-effect-oracle-v1",
        "capability": True,
        "callsite_count": callsites,
        "callsite": callsite,
        "callee": "initialize",
        "parameter": 0,
        "straight_line": True,
        "returns": 1,
        "writers": 1,
        "object_size": size,
        "base": base,
        "load": [load.offset, load.width],
        "store": [store.offset, store.width],
        "identity": {
            "base": base,
            "actual": base,
            "store_address": base + store.offset,
            "load_address": base + load.offset,
        },
        "memory_chain": ["terminal-call"],
    }


def mutation_oracle() -> int:
    original = valid_artifact(
        callsites=4,
        callsite=2,
        size=4,
        load=Interval(2, 2),
        store=Interval(1, 3),
    )
    mutations: list[dict[str, Any]] = []
    for mutate in (
        lambda item: item.update(capability=False),
        lambda item: item.update(callsite=4),
        lambda item: item.update(callee="other"),
        lambda item: item.update(parameter=1),
        lambda item: item.update(straight_line=False),
        lambda item: item.update(writers=2),
        lambda item: item.update(store=[0, 1]),
        lambda item: item.update(memory_chain=["live-on-entry"]),
    ):
        candidate = copy.deepcopy(original)
        mutate(candidate)
        mutations.append(candidate)
    return sum(not validate_artifact(candidate) for candidate in mutations)


def run_oracle() -> dict[str, Any]:
    accepted = 0
    noncover_rejections = 0
    wrong_callsite_rejections = 0
    instantiations = 0
    for callsites in range(2, 65):
        for size in range(1, 9):
            for load_offset in range(size):
                for load_width in range(1, size - load_offset + 1):
                    load = Interval(load_offset, load_width)
                    for store_offset in range(size):
                        for store_width in range(1, size - store_offset + 1):
                            store = Interval(store_offset, store_width)
                            instantiations += 1
                            artifact = valid_artifact(
                                callsites=callsites,
                                callsite=callsites - 1,
                                size=size,
                                load=load,
                                store=store,
                            )
                            if covers(store, load):
                                if not validate_artifact(artifact):
                                    raise AssertionError(
                                        "valid effect summary was rejected"
                                    )
                                accepted += 1
                                wrong = copy.deepcopy(artifact)
                                wrong["callsite"] = callsites
                                if validate_artifact(wrong):
                                    raise AssertionError(
                                        "wrong callsite was accepted"
                                    )
                                wrong_callsite_rejections += 1
                            else:
                                if validate_artifact(artifact):
                                    raise AssertionError(
                                        "non-covering effect was accepted"
                                    )
                                noncover_rejections += 1
    rejected_mutations = mutation_oracle()
    result = {
        "schema": "symcc-interprocedural-effect-oracle-results-v1",
        "callsites": {"minimum": 2, "maximum": 64},
        "object_sizes": {"minimum": 1, "maximum": 8},
        "interval_instantiations": instantiations,
        "accepted_covers": accepted,
        "noncover_rejections": noncover_rejections,
        "wrong_callsite_rejections": wrong_callsite_rejections,
        "rejected_certificate_mutations": rejected_mutations,
        "all_passed": rejected_mutations == 8,
        "claim_boundary": (
            "finite direct callsites, one straight-line callee store, "
            "constant parameter-relative offsets, and exact artifact identity; "
            "not recursion, indirect calls, conditional writers, dynamic "
            "intervals, exceptions, or public-target performance"
        ),
    }
    if not result["all_passed"]:
        raise AssertionError("certificate mutation oracle failed")
    return result


def main() -> int:
    print(json.dumps(run_oracle(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
