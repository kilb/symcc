#!/usr/bin/env python3
"""Independent finite runtime-vs-Decision-DAG last-write oracle for F421."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import json


PREDICATES = {"eq", "ne", "ugt", "uge", "ult", "ule"}


@dataclass(frozen=True)
class AffineArm:
    constant: int
    outer_scale: int
    inner_scale: int
    input_scale: int

    def evaluate(self, outer: int, inner: int, payload: int, bits: int) -> int:
        return (
            self.constant
            + self.outer_scale * outer
            + self.inner_scale * inner
            + self.input_scale * payload
        ) & ((1 << bits) - 1)


@dataclass(frozen=True)
class Guard:
    predicate: str
    induction: str
    constant: int
    constant_on_left: bool = False

    def evaluate(self, outer: int, inner: int) -> bool:
        induction = outer if self.induction == "outer" else inner
        left, right = (
            (self.constant, induction)
            if self.constant_on_left
            else (induction, self.constant)
        )
        return {
            "eq": left == right,
            "ne": left != right,
            "ugt": left > right,
            "uge": left >= right,
            "ult": left < right,
            "ule": left <= right,
        }[self.predicate]


@dataclass(frozen=True)
class DecisionDagCase:
    inner_guard: Guard
    root_guard: Guard
    arm_a: AffineArm = AffineArm(17, 5, 7, 3)
    arm_b: AffineArm = AffineArm(513, 11, 13, 2)
    arm_c: AffineArm = AffineArm(2049, 17, 19, 5)
    shared_leaf: bool = True
    endianness: str = "little"
    object_bytes: int = 12
    value_bits: int = 16
    outer_step: int = 1
    inner_step: int = 2
    address_outer_scale: int = 4
    address_inner_scale: int = 1
    overlay_byte: int | None = 0xAA
    load_bytes: int = 2


def supported(case: DecisionDagCase) -> bool:
    arms = {case.arm_a, case.arm_b, case.arm_c}
    return (
        case.inner_guard.predicate in PREDICATES
        and case.root_guard.predicate in PREDICATES
        and case.inner_guard.induction in {"outer", "inner"}
        and case.root_guard.induction in {"outer", "inner"}
        and 2 <= case.object_bytes <= 64
        and case.value_bits in {8, 16, 24, 32, 40, 48, 56, 64}
        and case.endianness in {"little", "big"}
        and len(arms) >= (2 if case.shared_leaf else 3)
        and case.value_bits // 8 <= case.address_inner_scale * case.inner_step
    )


def selected_arm(case: DecisionDagCase, outer: int, inner: int) -> AffineArm:
    if not case.root_guard.evaluate(outer, inner):
        return case.arm_b if case.shared_leaf else case.arm_c
    return case.arm_a if case.inner_guard.evaluate(outer, inner) else case.arm_b


def _value_bytes(value: int, byte_count: int, endianness: str) -> list[int]:
    offsets = range(byte_count) if endianness == "little" else reversed(range(byte_count))
    return [(value >> (8 * offset)) & 0xFF for offset in offsets]


def runtime_memory(
    case: DecisionDagCase,
    *,
    outer_count: int,
    inner_count: int,
    payload: int,
) -> tuple[list[int], list[bool]]:
    memory = [0] * case.object_bytes
    initialized = [False] * case.object_bytes
    value_bytes = case.value_bits // 8
    for outer in range(0, outer_count, case.outer_step):
        for inner in range(0, inner_count, case.inner_step):
            address = (
                case.address_outer_scale * outer
                + case.address_inner_scale * inner
            )
            if address < 0 or address + value_bytes > case.object_bytes:
                raise ValueError("runtime writer is outside the sealed object")
            value = selected_arm(case, outer, inner).evaluate(
                outer, inner, payload, case.value_bits
            )
            for lane, byte in enumerate(
                _value_bytes(value, value_bytes, case.endianness)
            ):
                memory[address + lane] = byte
                initialized[address + lane] = True
            if case.overlay_byte is not None:
                memory[address] = case.overlay_byte
                initialized[address] = True
    return memory, initialized


def summary_memory(
    case: DecisionDagCase,
    *,
    outer_count: int,
    inner_count: int,
    payload: int,
) -> tuple[list[int], list[bool]]:
    """Evaluate descending last-write cases, independently of runtime order."""
    value_bytes = case.value_bits // 8
    maximum_outer = 3
    maximum_inner = 3
    instances = [
        (outer, inner)
        for outer in range(0, maximum_outer, case.outer_step)
        for inner in range(0, maximum_inner, case.inner_step)
    ]
    memory = [0] * case.object_bytes
    initialized = [False] * case.object_bytes
    for target in range(case.object_bytes):
        candidates: list[tuple[int, int, int, int]] = []
        for outer, inner in instances:
            address = (
                case.address_outer_scale * outer
                + case.address_inner_scale * inner
            )
            for lane in range(value_bytes):
                if address + lane == target:
                    candidates.append((outer, inner, 0, lane))
            if case.overlay_byte is not None and address == target:
                candidates.append((outer, inner, 1, 0))
        candidates.sort(reverse=True)
        for outer, inner, ordinal, lane in candidates:
            if outer >= outer_count or inner >= inner_count:
                continue
            if ordinal == 1:
                memory[target] = int(case.overlay_byte)
            else:
                value = selected_arm(case, outer, inner).evaluate(
                    outer, inner, payload, case.value_bits
                )
                memory[target] = _value_bytes(
                    value, value_bytes, case.endianness
                )[lane]
            initialized[target] = True
            break
    return memory, initialized


def loads(
    case: DecisionDagCase,
    memory: list[int],
    initialized: list[bool],
) -> tuple[int | None, ...]:
    values: list[int | None] = []
    for address in range(case.object_bytes - case.load_bytes + 1):
        lanes = memory[address:address + case.load_bytes]
        defined = initialized[address:address + case.load_bytes]
        if not all(defined):
            values.append(None)
            continue
        ordered = lanes if case.endianness == "little" else list(reversed(lanes))
        values.append(sum(byte << (8 * lane) for lane, byte in enumerate(ordered)))
    return tuple(values)


def run_oracle() -> dict[str, object]:
    configurations = 0
    vector_equivalences = 0
    scalar_equivalences = 0
    defined_byte_equivalences = 0
    complete_loads = 0
    partial_loads = 0
    path_lengths = {1: 0, 2: 0}
    for inner_predicate in sorted(PREDICATES):
        for root_predicate in sorted(PREDICATES):
            for inner_left in (False, True):
                for root_left in (False, True):
                    for inner_induction, root_induction in (
                        ("inner", "outer"), ("outer", "inner")
                    ):
                        for endianness in ("little", "big"):
                            for shared_leaf in (False, True):
                                case = DecisionDagCase(
                                    Guard(
                                        inner_predicate, inner_induction, 2,
                                        inner_left,
                                    ),
                                    Guard(
                                        root_predicate, root_induction, 1,
                                        root_left,
                                    ),
                                    shared_leaf=shared_leaf,
                                    endianness=endianness,
                                )
                                if not supported(case):
                                    raise AssertionError("oracle case was rejected")
                                configurations += 1
                                for outer_count in range(4):
                                    for inner_count in range(4):
                                        for payload in (0, 0x1234, 0xFFFF):
                                            runtime = runtime_memory(
                                                case,
                                                outer_count=outer_count,
                                                inner_count=inner_count,
                                                payload=payload,
                                            )
                                            summary = summary_memory(
                                                case,
                                                outer_count=outer_count,
                                                inner_count=inner_count,
                                                payload=payload,
                                            )
                                            if runtime != summary:
                                                raise AssertionError(
                                                    "Decision DAG summary memory "
                                                    "diverged from concrete runtime"
                                                )
                                            runtime_loads = loads(case, *runtime)
                                            summary_loads = loads(case, *summary)
                                            if runtime_loads != summary_loads:
                                                raise AssertionError(
                                                    "Decision DAG load vector diverged"
                                                )
                                            vector_equivalences += 1
                                            scalar_equivalences += len(runtime_loads)
                                            complete = sum(
                                                value is not None
                                                for value in runtime_loads
                                            )
                                            complete_loads += complete
                                            partial_loads += len(runtime_loads) - complete
                                            defined_byte_equivalences += sum(runtime[1])
                                for outer in range(3):
                                    for inner in range(0, 3, 2):
                                        path_lengths[
                                            2 if case.root_guard.evaluate(
                                                outer, inner
                                            ) else 1
                                        ] += 1
    unsupported = [
        DecisionDagCase(Guard(predicate, "inner", 2), Guard("ule", "outer", 1))
        for predicate in ("slt", "sle", "sgt", "sge")
    ] + [
        DecisionDagCase(Guard("ult", "input", 2), Guard("ule", "outer", 1)),
        DecisionDagCase(Guard("ult", "inner", 2), Guard("ule", "input", 1)),
    ]
    if any(supported(case) for case in unsupported):
        raise AssertionError("unsupported Decision DAG case was accepted")
    return {
        "schema": "symcc-nested-loop-memoryphi-decision-dag-oracle-v1",
        "all_passed": True,
        "configurations": configurations,
        "runtime_load_vector_equivalences": vector_equivalences,
        "runtime_scalar_load_equivalences": scalar_equivalences,
        "runtime_defined_byte_equivalences": defined_byte_equivalences,
        "runtime_complete_loads": complete_loads,
        "runtime_uninitialized_or_partial_loads": partial_loads,
        "shared_and_unshared_dags": True,
        "specialized_path_lengths": {
            "one_guard": path_lengths[1],
            "two_guards": path_lengths[2],
        },
        "unsupported_cases_rejected": len(unsupported),
        "claim_boundary": (
            "Finite two-level i16 affine Decision DAG and descending last-write "
            "equivalence only; not executable loop replacement, general LoopSCC, "
            "coverage, solver time, defect yield, or end-to-end speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = run_oracle()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
