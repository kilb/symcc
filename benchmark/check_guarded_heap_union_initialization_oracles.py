#!/usr/bin/env python3
"""Independent finite-domain oracles for guarded heap-union initialization."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import copy
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


PathKey = tuple[bool, ...]
StoreWitness = tuple[int, int, int]


def complete_paths(depth: int) -> tuple[PathKey, ...]:
    """Enumerate a full binary guard tree in producer DFS order."""
    paths: list[PathKey] = [()]
    for _level in range(depth):
        paths = [(*path, decision) for path in paths for decision in (True, False)]
    return tuple(paths)


def guarded_cover(
    paths: Sequence[PathKey],
    selected_bases: Mapping[PathKey, int],
    stores: Mapping[PathKey, StoreWitness],
    load_offsets: Mapping[int, int],
    load_bytes: int,
) -> tuple[bool, tuple[int, ...]]:
    """Model exact-path object selection and same-path interval coverage."""
    if (
        len(paths) < 2
        or len(paths) > 64
        or not paths
        or not 1 <= len(paths[0]) <= 8
        or len(set(paths)) != len(paths)
        or set(selected_bases) != set(paths)
        or set(stores) != set(paths)
        or any(len(path) != len(paths[0]) for path in paths)
    ):
        return False, ()
    certified: set[int] = set()
    witnesses: set[StoreWitness] = set()
    for path in paths:
        base = selected_bases[path]
        load_offset = load_offsets.get(base)
        store_base, store_offset, store_bytes = stores[path]
        if (
            load_offset is None
            or store_base != base
            or store_bytes < load_bytes
            or store_offset > load_offset
            or load_offset + load_bytes > store_offset + store_bytes
        ):
            return False, ()
        certified.add(base)
        witnesses.add(stores[path])
    accepted = len(certified) >= 2 and len(witnesses) >= 2
    return accepted, tuple(sorted(certified)) if accepted else ()


def validator_program(depth: int = 2) -> dict:
    """Build a self-contained guard-tree artifact without invoking the producer."""
    paths = complete_paths(depth)
    bases = [64 + 16 * index for index in range(len(paths))]
    objects = [
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
    ]
    blocks: dict[str, list[dict]] = {"root": []}
    for level in range(depth):
        blocks["root"].extend([
            {"op": "input", "dst": f"raw_{level}", "offset": level},
            {
                "op": "unary",
                "operator": "trunc",
                "dst": f"guard_{level}",
                "value": {"var": f"raw_{level}"},
                "bits": 1,
            },
        ])
    blocks["root"].extend(
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
    )

    def block_name(prefix: PathKey) -> str:
        if not prefix:
            return "root"
        bits = "".join("t" if decision else "f" for decision in prefix)
        return f"node_{bits}"

    for level in range(depth):
        for prefix in complete_paths(level) if level else ((),):
            name = block_name(prefix)
            blocks.setdefault(name, [])
            blocks[name].append({
                "op": "branch",
                "condition": {"var": f"guard_{level}"},
                "true": block_name((*prefix, True)),
                "false": block_name((*prefix, False)),
                "site": f"branch:{level}:{name}",
            })

    transcript_paths: list[dict] = []
    alias_cases: list[dict] = []
    for index, (path, base) in enumerate(zip(paths, bases, strict=True)):
        leaf = block_name(path)
        blocks[leaf] = [
            {
                "op": "store",
                "address": {"var": f"pointer_{index}"},
                "alias_cases": [{
                    "addresses": [base],
                    "guards": [{
                        "value": {"var": f"pointer_{index}"},
                        "equals": base,
                        "bits": 64,
                    }],
                }],
                "value": {"const": index + 1, "bits": 8},
                "bits": 8,
                "bytes": 1,
            },
            {"op": "jump", "target": "merge"},
        ]
        path_blocks = [block_name(path[:level]) for level in range(depth + 1)]
        decisions = [
            {"block": block_name(path[:level]), "equals": path[level]}
            for level in range(depth)
        ]
        transcript_paths.append({
            "blocks": path_blocks,
            "decisions": decisions,
            "predecessor": leaf,
            "base": base,
            "load_address": base,
            "store": {
                "block": leaf,
                "ordinal": 0,
                "address": base,
                "bytes": 1,
            },
        })
        alias_cases.append({
            "addresses": [base],
            "guards": [
                {
                    "value": {"var": f"pointer_{index}"},
                    "equals": base,
                    "bits": 64,
                },
                *[
                    {
                        "value": {"var": f"guard_{level}"},
                        "equals": int(path[level]),
                        "bits": 1,
                    }
                    for level in range(depth)
                ],
            ],
        })
    blocks["merge"] = [
        {
            "op": "load",
            "dst": "value",
            "address": {"const": bases[0], "bits": 64},
            "alias_cases": alias_cases,
            "initialization_bases": bases,
            "initialization_guard_tree": {
                "schema": "symcc-guarded-heap-union-initialization-v1",
                "root": "root",
                "merge": "merge",
                "depth": depth,
                "paths": transcript_paths,
            },
            "bits": 8,
            "bytes": 1,
        },
        {"op": "return", "value": {"var": "value"}},
    ]
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": depth,
        "memory_size": bases[-1] + 1,
        "memory_objects": objects,
        "functions": {
            "main": {"entry": "root", "params": [], "blocks": blocks},
        },
        "lowering": {
            "capabilities": [
                "bounded-heap-lifetime",
                "bounded-pointer-union",
                "bounded-collective-heap-union-initialization",
                "bounded-guard-correlated-heap-union-initialization",
            ],
        },
    }


def rejected_mutations() -> int:
    def guarded_load(program: dict) -> dict:
        return program["functions"]["main"]["blocks"]["merge"][0]

    mutations: list[tuple[str, Callable[[dict], object]]] = [
        (
            "missing-capability",
            lambda program: program["lowering"]["capabilities"].pop(),
        ),
        (
            "flipped-decision",
            lambda program: guarded_load(program)["initialization_guard_tree"]
            ["paths"][0]["decisions"][0].update(equals=False),
        ),
        (
            "duplicate-path",
            lambda program: guarded_load(program)["initialization_guard_tree"]
            ["paths"].__setitem__(
                -1,
                copy.deepcopy(
                    guarded_load(program)["initialization_guard_tree"]["paths"][0]
                ),
            ),
        ),
        (
            "foreign-base",
            lambda program: guarded_load(program)["initialization_guard_tree"]
            ["paths"][0].update(base=4096),
        ),
        (
            "invalid-store-ordinal",
            lambda program: guarded_load(program)["initialization_guard_tree"]
            ["paths"][0]["store"].update(ordinal=1),
        ),
        (
            "undersized-store",
            lambda program: guarded_load(program)["initialization_guard_tree"]
            ["paths"][0]["store"].update(bytes=0),
        ),
        (
            "alias-guard-mismatch",
            lambda program: guarded_load(program)["alias_cases"][0]["guards"][1]
            .update(equals=0),
        ),
        (
            "missing-load-allocation-identity",
            lambda program: guarded_load(program)["alias_cases"][0]["guards"].pop(
                0
            ),
        ),
        (
            "missing-store-allocation-identity",
            lambda program: program["functions"]["main"]["blocks"]["node_tt"]
            [0]["alias_cases"][0].update(guards=[{
                "value": {"var": "guard_0"},
                "equals": 1,
                "bits": 1,
            }]),
        ),
        (
            "missing-transcript",
            lambda program: guarded_load(program).pop("initialization_guard_tree"),
        ),
    ]
    rejected = 0
    with tempfile.TemporaryDirectory() as root:
        store = LiveStateStore(root, page_size=64)
        LiveContinuationExecutor(store).create(validator_program())
        for name, mutate in mutations:
            program = validator_program()
            mutate(program)
            try:
                LiveContinuationExecutor(store).create(program)
            except ValueError:
                rejected += 1
                continue
            raise AssertionError(f"validator admitted mutation {name}")
    return rejected


def main() -> int:
    tree_count = 0
    path_assignments = 0
    interval_checks = 0
    missing_store_rejections = 0
    guard_mismatch_rejections = 0
    partial_width_rejections = 0
    for depth in range(1, 7):
        paths = complete_paths(depth)
        bases = [64 + 16 * index for index in range(len(paths))]
        selected = dict(zip(paths, bases, strict=True))
        load_offsets = {base: 2 for base in bases}
        for load_bytes in range(1, 9):
            stores = {
                path: (base, 0, 16)
                for path, base in zip(paths, bases, strict=True)
            }
            accepted, certificate = guarded_cover(
                paths, selected, stores, load_offsets, load_bytes
            )
            if not accepted or certificate != tuple(bases):
                raise AssertionError("complete guarded cover was rejected")
            tree_count += 1
            path_assignments += len(paths)
            interval_checks += len(paths)

            for victim in paths:
                missing = dict(stores)
                missing.pop(victim)
                if guarded_cover(
                    paths, selected, missing, load_offsets, load_bytes
                )[0]:
                    raise AssertionError("missing path store was accepted")
                missing_store_rejections += 1

                mismatched = dict(selected)
                mismatched[victim] = selected[paths[(paths.index(victim) + 1) % len(paths)]]
                if guarded_cover(
                    paths, mismatched, stores, load_offsets, load_bytes
                )[0]:
                    raise AssertionError("guard/store mismatch was accepted")
                guard_mismatch_rejections += 1

                partial = dict(stores)
                base, offset, _width = partial[victim]
                partial[victim] = (base, offset, max(0, load_bytes - 1))
                if guarded_cover(
                    paths, selected, partial, load_offsets, load_bytes
                )[0]:
                    raise AssertionError("partial-width store was accepted")
                partial_width_rejections += 1

    print(json.dumps(
        {
            "schema": "symcc-guarded-heap-union-init-oracle-v1",
            "depths": [1, 2, 3, 4, 5, 6],
            "maximum_paths": 64,
            "load_widths": [1, 2, 3, 4, 5, 6, 7, 8],
            "accepted_trees": tree_count,
            "path_assignments": path_assignments,
            "interval_checks": interval_checks,
            "missing_store_rejections": missing_store_rejections,
            "guard_mismatch_rejections": guard_mismatch_rejections,
            "partial_width_rejections": partial_width_rejections,
            "rejected_certificate_mutations": rejected_mutations(),
            "all_passed": True,
            "claim_boundary": (
                "complete acyclic binary guard trees, finite ordinary heap bases, "
                "and static scalar intervals only; not arbitrary MemorySSA, loops, "
                "dynamic indices, or interprocedural clobber reasoning"
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
