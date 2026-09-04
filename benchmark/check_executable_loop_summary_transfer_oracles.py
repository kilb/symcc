#!/usr/bin/env python3
"""Differential concrete oracle for the executable nested-loop summary.

The reference below executes the LLVM fixture's source-level loop directly.
It deliberately does not consume the producer transcript or normalized write
list, so a shared lowering/consumer bug cannot make both sides agree.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


def concrete_loop_state(
    index: int,
    outer_count: int,
    inner_count: int,
    payload: int,
) -> tuple[bytes, tuple[bool, ...]]:
    """Execute the source-level loop using LLVM i16 modulo semantics."""
    if not 0 <= index <= 10:
        raise ValueError("oracle load index must stay inside the 12-byte object")
    if not 0 <= outer_count <= 3 or not 0 <= inner_count <= 3:
        raise ValueError("oracle bounds must fit their source i2 values")
    memory = bytearray(12)
    initialized = [False] * 12
    mask = (1 << 16) - 1
    for outer in range(outer_count):
        for inner in range(0, inner_count, 2):
            address = outer * 4 + inner
            value_a = (
                payload * 3 + outer * 5 + inner * 7 + 17
            ) & mask
            value_b = (
                payload * 2 + outer * 11 + inner * 13 + 513
            ) & mask
            selected = (
                value_a if inner < 2 and outer <= 1 else value_b
            )
            memory[address] = selected & 0xFF
            memory[address + 1] = (selected >> 8) & 0xFF
            initialized[address] = True
            initialized[address + 1] = True
            memory[address] = 0xAA
            initialized[address] = True
    return bytes(memory), tuple(initialized)


def concrete_loop(
    index: int,
    outer_count: int,
    inner_count: int,
    payload: int,
) -> tuple[int, bool]:
    memory, initialized = concrete_loop_state(
        index, outer_count, inner_count, payload
    )
    value = memory[index] | (memory[index + 1] << 8)
    return value, initialized[index] and initialized[index + 1]


def verify_domain(artifact: dict, *, payload: int) -> dict:
    configurations = 0
    initialized_loads = 0
    partial_or_uninitialized_loads = 0
    maximum_steps = 0
    memory_byte_equivalences = 0
    initializedness_equivalences = 0
    defined_load_value_equivalences = 0
    stack_objects = [
        item for item in artifact.get("memory_objects", ())
        if item.get("kind") == "stack"
    ]
    if (
        len(stack_objects) != 1
        or int(stack_objects[0].get("address", -1)) != 64
        or int(stack_objects[0].get("size", -1)) != 12
    ):
        raise AssertionError("differential fixture stack object changed")
    with tempfile.TemporaryDirectory() as tmp:
        executor = LiveContinuationExecutor(
            LiveStateStore(tmp, page_size=64),
            enable_loop_summary_transfer=True,
        )
        for index in range(11):
            for outer_count in range(4):
                for inner_count in range(4):
                    expected_memory, expected_initialized = concrete_loop_state(
                        index, outer_count, inner_count, payload
                    )
                    expected = (
                        expected_memory[index]
                        | (expected_memory[index + 1] << 8)
                    )
                    fully_initialized = (
                        expected_initialized[index]
                        and expected_initialized[index + 1]
                    )
                    input_bytes = bytes((
                        index,
                        outer_count,
                        inner_count,
                        payload & 0xFF,
                        (payload >> 8) & 0xFF,
                    ))
                    checkpoint = executor.create(
                        artifact, input_bytes=input_bytes
                    )
                    transfer_result = executor.resume(
                        checkpoint, max_steps=23, max_states=4
                    )
                    if (
                        len(transfer_result["frontier"]) != 1
                        or transfer_result["forks"] != 0
                        or transfer_result["loop_summary_transfers_applied"] != 1
                        or transfer_result["loop_summary_transfer_fallbacks"] != 0
                        or not transfer_result["loop_summary_transfer_enabled"]
                    ):
                        raise AssertionError(
                            "loop-summary transfer contract failed for "
                            f"index={index}, outer={outer_count}, "
                            f"inner={inner_count}: {transfer_result}"
                        )
                    summary_checkpoint = transfer_result["frontier"][0]
                    summary_state = executor._load_state(summary_checkpoint)
                    concrete_bytes, symbolic_bytes = executor.store.read_memory(
                        summary_state.memory_root, 64, 12
                    )
                    actual_memory = bytearray(concrete_bytes)
                    for offset, digest in symbolic_bytes.items():
                        actual_memory[offset] = executor._concrete(digest)
                    if bytes(actual_memory) != expected_memory:
                        raise AssertionError(
                            "loop-summary memory mismatch for "
                            f"index={index}, outer={outer_count}, "
                            f"inner={inner_count}: expected "
                            f"{expected_memory.hex()}, got {bytes(actual_memory).hex()}"
                        )
                    actual_initialized = tuple(
                        bool(executor._concrete(summary_state.values[marker]))
                        if (marker := f"@stack:init:0:{64 + offset}")
                        in summary_state.values
                        else False
                        for offset in range(12)
                    )
                    if actual_initialized != expected_initialized:
                        raise AssertionError(
                            "loop-summary initializedness mismatch for "
                            f"index={index}, outer={outer_count}, "
                            f"inner={inner_count}: expected "
                            f"{expected_initialized}, got {actual_initialized}"
                        )
                    configurations += 1
                    configuration_steps = int(transfer_result["steps"])
                    memory_byte_equivalences += 12
                    initializedness_equivalences += 12
                    if fully_initialized:
                        result = executor.resume(
                            summary_checkpoint, max_steps=8, max_states=4
                        )
                        returned = [
                            int(row["value"])
                            for row in result["halted"]
                            if row.get("status") == "returned"
                        ]
                        if returned != [expected]:
                            raise AssertionError(
                                "loop-summary differential mismatch for "
                                f"index={index}, outer={outer_count}, "
                                f"inner={inner_count}, payload={payload}: "
                                f"expected {expected}, got {returned}"
                            )
                        if result["forks"] != 0 or result["bounded"]:
                            raise AssertionError(
                                "loop-summary execution contract failed for "
                                f"index={index}, outer={outer_count}, "
                                f"inner={inner_count}: {result}"
                            )
                        configuration_steps += int(result["steps"])
                        initialized_loads += 1
                        defined_load_value_equivalences += 1
                    else:
                        partial_or_uninitialized_loads += 1
                    maximum_steps = max(maximum_steps, configuration_steps)
    return {
        "schema": "symcc-executable-loop-summary-differential-v1",
        "all_passed": True,
        "configurations": configurations,
        "payload": payload,
        "initialized_loads": initialized_loads,
        "partial_or_uninitialized_loads": partial_or_uninitialized_loads,
        "maximum_steps": maximum_steps,
        "forks": 0,
        "summary_transfers": configurations,
        "memory_byte_equivalences": memory_byte_equivalences,
        "initializedness_equivalences": initializedness_equivalences,
        "defined_load_value_equivalences": defined_load_value_equivalences,
        "claim_boundary": (
            "Concrete memory and initializedness equivalence for the bounded "
            "i2/i2, i16 Decision-DAG fixture, with value equality asserted "
            "only for fully initialized in-object i16 loads; not general "
            "LoopSCC, heap, call-effect, solver-time, or campaign-speedup proof"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact")
    parser.add_argument("--payload", type=lambda raw: int(raw, 0), default=0x1234)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if not 0 <= args.payload <= 0xFFFF:
        raise ValueError("payload is outside the i16 domain")
    with open(args.artifact, encoding="utf-8") as stream:
        artifact = json.load(stream)
    result = verify_domain(artifact, payload=args.payload)
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="ascii")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
