#!/usr/bin/env python3
"""Independent finite oracle for bounded MemorySSA heap initialization."""

from __future__ import annotations

import copy
import json
from typing import Any


def covers(store: tuple[int, int, int], load: tuple[int, int, int]) -> bool:
    store_base, store_offset, store_width = store
    load_base, load_offset, load_width = load
    return (
        store_base == load_base
        and store_offset <= load_offset
        and load_offset + load_width <= store_offset + store_width
    )


def prove(
    paths: list[dict[str, object]],
    expected_bases: set[int],
) -> bool:
    witnessed: set[int] = set()
    for path in paths:
        base = int(path["base"])
        store = tuple(path["store"])
        load = tuple(path["load"])
        skipped = list(path["skipped"])
        if base != load[0] or not covers(store, load):
            return False
        if any(int(skipped_base) == base for skipped_base in skipped):
            return False
        witnessed.add(base)
    return witnessed == expected_bases


def validate_artifact(artifact: dict[str, Any]) -> bool:
    """Independently replay the bounded certificate's structural contract."""
    try:
        if "bounded-memoryssa-aa-heap-initialization" not in artifact[
            "capabilities"
        ]:
            return False
        transcript = artifact["transcript"]
        nodes = transcript["nodes"]
        if (
            transcript.get("schema")
            != "symcc-memoryssa-aa-heap-initialization-v1"
            or transcript.get("root_node") != 0
            or not 3 <= len(nodes) <= 128
        ):
            return False
        predecessors = artifact["predecessors"]
        store_catalog = artifact["store_catalog"]
        noalias_store_catalog = artifact["noalias_store_catalog"]
        no_modref_calls = artifact["no_modref_calls"]
        parsed: list[dict[str, Any]] = []
        terminal_bases: dict[int, int] = {}
        for index, node in enumerate(nodes):
            if node.get("id") != index:
                return False
            if node.get("kind") == "memory-phi":
                incoming = node.get("incoming")
                if not isinstance(incoming, list) or not 2 <= len(incoming) <= 64:
                    return False
                blocks = {edge.get("block") for edge in incoming}
                if blocks != set(predecessors.get(node.get("block"), ())):
                    return False
                if any(
                    not isinstance(edge.get("node"), int)
                    or not index < edge["node"] < len(nodes)
                    for edge in incoming
                ):
                    return False
                parsed.append(node)
                continue
            if node.get("kind") != "store":
                return False
            base = node.get("base")
            load = (base, node.get("load_offset"), transcript.get("load_bytes"))
            raw_store = node.get("store")
            key = (raw_store.get("block"), raw_store.get("ordinal"))
            store = store_catalog.get(key)
            if not isinstance(base, int) or store is None or not covers(store, load):
                return False
            for skipped in node.get("skipped_defs", ()):
                skipped_key = (skipped.get("block"), skipped.get("ordinal"))
                if skipped.get("kind") == "store":
                    skipped_base = noalias_store_catalog.get(skipped_key)
                    if (
                        skipped.get("proof")
                        not in {"allocation-noalias", "aa-noalias"}
                        or skipped_base is None
                        or skipped_base == base
                    ):
                        return False
                elif skipped.get("kind") == "call":
                    if (
                        skipped.get("proof") != "aa-no-modref"
                        or skipped.get("opcode") != "call"
                        or skipped_key not in no_modref_calls
                    ):
                        return False
                else:
                    return False
            terminal_bases[index] = base
            parsed.append(node)

        visited: set[int] = set()
        active: set[int] = set()

        def descendants(index: int) -> set[int] | None:
            if index in active:
                return None
            active.add(index)
            visited.add(index)
            node = parsed[index]
            if node["kind"] == "store":
                result: set[int] | None = {terminal_bases[index]}
            else:
                result = set()
                for edge in node["incoming"]:
                    child = descendants(edge["node"])
                    if child is None:
                        return None
                    result.update(child)
            active.remove(index)
            return result

        bases = descendants(0)
        return (
            bases == set(artifact["expected_bases"])
            and len(visited) == len(nodes)
            and nodes[0].get("kind") == "memory-phi"
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def mutation_oracle() -> dict[str, bool]:
    artifact: dict[str, Any] = {
        "capabilities": ["bounded-memoryssa-aa-heap-initialization"],
        "predecessors": {"merge": ["left", "right"]},
        "expected_bases": [64, 80],
        "store_catalog": {
            ("left", 0): (64, 0, 1),
            ("right", 0): (80, 0, 1),
            ("orphan", 0): (96, 0, 1),
        },
        "noalias_store_catalog": {
            ("left", 1): 112,
            ("right", 1): 112,
        },
        "no_modref_calls": {("left", 2), ("right", 2)},
        "transcript": {
            "schema": "symcc-memoryssa-aa-heap-initialization-v1",
            "load_bytes": 1,
            "root_node": 0,
            "nodes": [
                {
                    "id": 0,
                    "kind": "memory-phi",
                    "block": "merge",
                    "incoming": [
                        {"block": "left", "node": 1},
                        {"block": "right", "node": 2},
                    ],
                },
                {
                    "id": 1,
                    "kind": "store",
                    "base": 64,
                    "load_offset": 0,
                    "store": {"block": "left", "ordinal": 0},
                    "skipped_defs": [
                        {
                            "block": "left", "ordinal": 1,
                            "kind": "store", "opcode": "store",
                            "proof": "allocation-noalias",
                        },
                        {
                            "block": "left", "ordinal": 2,
                            "kind": "call", "opcode": "call",
                            "proof": "aa-no-modref",
                        },
                    ],
                },
                {
                    "id": 2,
                    "kind": "store",
                    "base": 80,
                    "load_offset": 0,
                    "store": {"block": "right", "ordinal": 0},
                    "skipped_defs": [
                        {
                            "block": "right", "ordinal": 1,
                            "kind": "store", "opcode": "store",
                            "proof": "aa-noalias",
                        },
                        {
                            "block": "right", "ordinal": 2,
                            "kind": "call", "opcode": "call",
                            "proof": "aa-no-modref",
                        },
                    ],
                },
            ],
        },
    }
    assert validate_artifact(artifact)
    mutations: dict[str, dict[str, Any]] = {}
    mutations["missing-capability"] = copy.deepcopy(artifact)
    mutations["missing-capability"]["capabilities"] = []
    mutations["bad-phi-edge"] = copy.deepcopy(artifact)
    mutations["bad-phi-edge"]["transcript"]["nodes"][0]["incoming"][0][
        "block"
    ] = "foreign"
    mutations["back-edge"] = copy.deepcopy(artifact)
    mutations["back-edge"]["transcript"]["nodes"][0]["incoming"][0][
        "node"
    ] = 0
    mutations["orphan-node"] = copy.deepcopy(artifact)
    mutations["orphan-node"]["transcript"]["nodes"].append({
        "id": 3,
        "kind": "store",
        "base": 96,
        "load_offset": 0,
        "store": {"block": "orphan", "ordinal": 0},
        "skipped_defs": [],
    })
    mutations["bad-store-ordinal"] = copy.deepcopy(artifact)
    mutations["bad-store-ordinal"]["transcript"]["nodes"][1]["store"][
        "ordinal"
    ] = 99
    mutations["bad-base"] = copy.deepcopy(artifact)
    mutations["bad-base"]["transcript"]["nodes"][1]["base"] = 80
    mutations["bad-skip-proof"] = copy.deepcopy(artifact)
    mutations["bad-skip-proof"]["transcript"]["nodes"][1][
        "skipped_defs"
    ][1]["proof"] = "may-modref"
    mutations["missing-transcript"] = copy.deepcopy(artifact)
    del mutations["missing-transcript"]["transcript"]
    return {name: validate_artifact(value) for name, value in mutations.items()}


def main() -> int:
    accepted_graphs = 0
    edge_assignments = 0
    interval_checks = 0
    noalias_skips = 0
    missing_store_rejections = 0
    partial_width_rejections = 0
    alias_mismatch_rejections = 0
    for fan_in in range(2, 65):
        bases = {64 + index * 16 for index in range(fan_in)}
        for width in range(1, 9):
            paths = [
                {
                    "base": base,
                    "load": (base, 0, width),
                    "store": (base, 0, width),
                    "skipped": [64 + fan_in * 16],
                }
                for base in sorted(bases)
            ]
            assert prove(paths, bases)
            accepted_graphs += 1
            edge_assignments += fan_in
            interval_checks += fan_in
            noalias_skips += fan_in
            for index in range(fan_in):
                missing = [dict(path) for path in paths]
                missing[index] = {
                    **missing[index],
                    "store": (missing[index]["base"] + 8, 0, width),
                }
                assert not prove(missing, bases)
                missing_store_rejections += 1

                partial = [dict(path) for path in paths]
                partial[index] = {
                    **partial[index],
                    "store": (partial[index]["base"], 0, width - 1),
                }
                assert not prove(partial, bases)
                partial_width_rejections += 1

                mismatch = [dict(path) for path in paths]
                mismatch[index] = {
                    **mismatch[index],
                    "base": 64 + ((index + 1) % fan_in) * 16,
                }
                assert not prove(mismatch, bases)
                alias_mismatch_rejections += 1

    mutations = mutation_oracle()
    assert not any(mutations.values())
    result = {
        "schema": "symcc-memoryssa-aa-heap-init-oracle-v1",
        "all_passed": True,
        "fan_in": {"minimum": 2, "maximum": 64},
        "load_widths": list(range(1, 9)),
        "accepted_graphs": accepted_graphs,
        "edge_assignments": edge_assignments,
        "interval_checks": interval_checks,
        "noalias_skips": noalias_skips,
        "missing_store_rejections": missing_store_rejections,
        "partial_width_rejections": partial_width_rejections,
        "alias_mismatch_rejections": alias_mismatch_rejections,
        "rejected_certificate_mutations": len(mutations),
        "maximum_nodes": 128,
        "maximum_phi_depth": 8,
        "claim_boundary": (
            "bounded acyclic MemorySSA phi/def graphs, ordinary heap bases, "
            "static scalar intervals, and explicit NoAlias/NoModRef skips "
            "only; not unbounded loops, arbitrary symbolic offsets, or "
            "interprocedural memory summaries"
        ),
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
