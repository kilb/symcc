#!/usr/bin/env python3
"""Independent finite-domain oracle for certified heap lifetime unions."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


def build_program(domain: int) -> dict:
    if not 1 <= domain <= 32:
        raise ValueError("domain must be in 1..32")
    addresses = [64 + 4 * index for index in range(domain)]
    instructions: list[dict] = []
    for index, address in enumerate(addresses):
        instructions.append({
            "op": "heap_alloc",
            "dst": f"pointer_{index}",
            "addresses": [address],
            "capacity": 1,
            "size": 4,
            "site": f"site:{index}",
            "allocator": "malloc",
            "bits": 64,
        })
    instructions.append({"op": "input", "dst": "raw", "offset": 0})
    selected = f"pointer_{domain - 1}"
    for index in range(domain - 1):
        condition = f"choose_{index}"
        destination = f"selected_{index}"
        instructions.extend((
            {
                "op": "binary",
                "operator": "eq",
                "dst": condition,
                "left": {"var": "raw"},
                "right": {"const": index, "bits": 8},
                "bits": 1,
            },
            {
                "op": "select",
                "dst": destination,
                "condition": {"var": condition},
                "true": {"var": f"pointer_{index}"},
                "false": {"var": selected},
                "bits": 64,
            },
        ))
        selected = destination
    instructions.extend((
        {
            "op": "heap_free",
            "address": {"var": selected},
            "addresses": addresses,
            "bits": 64,
        },
        {"op": "return", "value": 0},
    ))
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 1,
        "memory_size": 64 + 4 * domain,
        "memory_objects": [
            {
                "name": f"heap:site:{index}",
                "kind": "heap",
                "site": f"site:{index}",
                "slot": 0,
                "capacity": 1,
                "address": address,
                "size": 4,
                "read_only": False,
                "lifetime": "runtime-alloc-free",
                "allocation": "bounded-pool-infallible",
            }
            for index, address in enumerate(addresses)
        ],
        "functions": {
            "main": {
                "entry": "entry",
                "params": [],
                "blocks": {"entry": instructions},
            },
        },
        "lowering": {
            "capabilities": [
                "bounded-heap-lifetime",
                "bounded-heap-lifetime-pointer-union",
            ],
        },
    }


def evaluate(
    store: LiveStateStore,
    digest: str,
    raw: int,
    memo: dict[str, int],
) -> int:
    if digest in memo:
        return memo[digest]
    expression = store.get_expression(digest)
    op = str(expression["op"])
    if op == "const":
        result = int(expression["value"])
    elif op == "input":
        result = raw & 0xFF
    else:
        children = [
            evaluate(store, str(child), raw, memo)
            for child in expression.get("children", ())
        ]
        operations = {
            "not": lambda: int(not children[0]),
            "and": lambda: children[0] & children[1],
            "eq": lambda: int(children[0] == children[1]),
            "ite": lambda: children[1] if children[0] else children[2],
        }
        if op not in operations:
            raise AssertionError(f"oracle cannot evaluate {op}")
        result = int(operations[op]())
    memo[digest] = result
    return result


def execute(domain: int) -> tuple[LiveStateStore, dict, tempfile.TemporaryDirectory]:
    temporary = tempfile.TemporaryDirectory()
    store = LiveStateStore(temporary.name, page_size=64)
    executor = LiveContinuationExecutor(store)
    result = executor.resume(
        executor.create(build_program(domain), input_bytes=b"\x00"),
        max_steps=4096,
    )
    if len(result["halted"]) != 1 or result["forks"] != 0:
        temporary.cleanup()
        raise AssertionError("conditional heap release changed control paths")
    bundle = store.restore_continuation(result["halted"][0]["checkpoint"])
    return store, dict(bundle.symbolic_store), temporary


def main() -> int:
    marker_evaluations = 0
    for domain in range(1, 17):
        store, values, temporary = execute(domain)
        try:
            addresses = [64 + 4 * index for index in range(domain)]
            for raw in range(256):
                if domain == 1:
                    if "@heap:live:64" in values:
                        raise AssertionError(
                            "singleton concrete release retained live marker"
                        )
                    marker_evaluations += 1
                    continue
                memo: dict[str, int] = {}
                live = [
                    evaluate(
                        store,
                        values[f"@heap:live:{address}"],
                        raw,
                        memo,
                    )
                    for address in addresses
                ]
                expected = raw if raw < domain - 1 else domain - 1
                if live.count(0) != 1 or live[expected] != 0:
                    raise AssertionError(
                        f"domain={domain} raw={raw} live={live}"
                    )
                marker_evaluations += domain
        finally:
            temporary.cleanup()

    program = build_program(8)
    with tempfile.TemporaryDirectory() as root:
        store = LiveStateStore(root, page_size=64)
        direct_executor = LiveContinuationExecutor(store)
        initial = direct_executor.create(program, input_bytes=b"\x00")
        direct = direct_executor.resume(initial, max_steps=4096)
        direct_values = dict(store.restore_continuation(
            direct["halted"][0]["checkpoint"]
        ).symbolic_store)

        paused_executor = LiveContinuationExecutor(store)
        paused = paused_executor.resume(initial, max_steps=23)
        if len(paused["frontier"]) != 1:
            raise AssertionError("restart oracle did not pause before free")
        resumed = LiveContinuationExecutor(store).resume(
            paused["frontier"][0], max_steps=4096
        )
        resumed_values = dict(store.restore_continuation(
            resumed["halted"][0]["checkpoint"]
        ).symbolic_store)
        if direct_values != resumed_values:
            raise AssertionError("restart changed conditional heap release")

        for mutation in (
            "foreign",
            "unsorted",
            "missing-capability",
            "width-mismatch",
        ):
            tampered = json.loads(json.dumps(program))
            release = tampered["functions"]["main"]["blocks"]["entry"][-2]
            if mutation == "foreign":
                release["addresses"][-1] = 4096
            elif mutation == "unsorted":
                release["addresses"] = list(reversed(release["addresses"]))
            elif mutation == "missing-capability":
                tampered["lowering"]["capabilities"].pop()
            else:
                release["bits"] = 32
            try:
                executor = LiveContinuationExecutor(store)
                checkpoint = executor.create(tampered)
                executor.resume(checkpoint, max_steps=4096)
            except ValueError:
                continue
            raise AssertionError(f"validator admitted {mutation} certificate")

    print(json.dumps({
        "schema": "symcc-heap-lifetime-union-oracle-v1",
        "domains": 16,
        "input_assignments": 16 * 256,
        "marker_evaluations": marker_evaluations,
        "restart_equivalence": True,
        "rejected_mutations": 4,
        "all_passed": True,
        "claim_boundary": (
            "finite allocated C heap objects only; not POSE initial symbolic "
            "heap materialization or an end-to-end performance result"
        ),
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
