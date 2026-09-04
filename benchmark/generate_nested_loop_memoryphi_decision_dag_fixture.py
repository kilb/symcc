#!/usr/bin/env python3
"""Generate a bounded nested-loop affine Decision DAG fixture for F421."""

from __future__ import annotations

import argparse
import math


PREDICATES = {"eq", "ne", "ugt", "uge", "ult", "ule"}


def _arm(
    prefix: str,
    bits: int,
    coefficients: tuple[int, int, int, int],
) -> tuple[list[str], str]:
    constant, outer_scale, inner_scale, input_scale = coefficients
    terms = [
        ("%payload", input_scale),
        ("%outer_iv", outer_scale),
        ("%inner_iv", inner_scale),
    ]
    operations: list[str] = []
    values: list[str] = []
    for index, (source, scale) in enumerate(terms):
        if not scale:
            continue
        if scale == 1:
            values.append(source)
            continue
        name = f"%{prefix}_term_{index}"
        operations.append(f"  {name} = mul i{bits} {source}, {scale}")
        values.append(name)
    if not values:
        return operations, str(constant)
    current = values[0]
    for index, value in enumerate(values[1:], start=1):
        name = f"%{prefix}_sum_{index}"
        operations.append(f"  {name} = add i{bits} {current}, {value}")
        current = name
    if constant:
        name = f"%value_{prefix}"
        operations.append(f"  {name} = add i{bits} {current}, {constant}")
        current = name
    return operations, current


def _guard_operands(
    induction: str,
    constant: int,
    constant_on_left: bool,
) -> str:
    variable = "%outer_iv" if induction == "outer" else "%inner_iv"
    return (
        f"{constant}, {variable}"
        if constant_on_left
        else f"{variable}, {constant}"
    )


def generate(
    *,
    object_bytes: int = 12,
    outer_bound_bits: int = 2,
    inner_bound_bits: int = 2,
    value_bits: int = 16,
    outer_step: int = 1,
    inner_step: int = 2,
    address_outer_scale: int = 4,
    address_inner_scale: int = 1,
    inner_predicate: str = "ult",
    inner_induction: str = "inner",
    inner_constant: int = 2,
    inner_constant_on_left: bool = False,
    root_predicate: str = "ule",
    root_induction: str = "outer",
    root_constant: int = 1,
    root_constant_on_left: bool = False,
    a_coefficients: tuple[int, int, int, int] = (17, 5, 7, 3),
    b_coefficients: tuple[int, int, int, int] = (513, 11, 13, 2),
    c_coefficients: tuple[int, int, int, int] = (2049, 17, 19, 5),
    shared_leaf: bool = True,
    shared_subtree: bool = False,
    mixed_piecewise_writer: bool = False,
    overlay_byte: int | None = -86,
    load_bytes: int = 2,
    endianness: str = "little",
) -> str:
    if not 2 <= object_bytes <= 64:
        raise ValueError("object_bytes must be in [2, 64]")
    if value_bits not in {8, 16, 24, 32, 40, 48, 56, 64}:
        raise ValueError("value_bits must be byte-complete and at most 64")
    if not 1 <= outer_bound_bits < 64 or not 1 <= inner_bound_bits < 64:
        raise ValueError("bound widths must be in [1, 63]")
    if not 1 <= outer_step <= 64 or not 1 <= inner_step <= 64:
        raise ValueError("loop steps must be in [1, 64]")
    if inner_predicate not in PREDICATES or root_predicate not in PREDICATES:
        raise ValueError("unsupported Decision DAG predicate")
    if inner_induction not in {"outer", "inner"} or root_induction not in {
        "outer", "inner",
    }:
        raise ValueError("guard induction must be outer or inner")
    if endianness not in {"little", "big"}:
        raise ValueError("endianness must be little or big")
    integers = (
        inner_constant, root_constant,
        *a_coefficients, *b_coefficients, *c_coefficients,
    )
    if any(type(value) is not int or not 0 <= value <= (1 << 63) - 1
           for value in integers):
        raise ValueError("Decision DAG constants are outside the sealed domain")
    if len({a_coefficients, b_coefficients, c_coefficients}) < (
        2 if shared_leaf else 3
    ):
        raise ValueError("Decision DAG needs distinct affine leaves")
    value_bytes = value_bits // 8
    if (
        address_outer_scale <= 0
        or address_inner_scale <= 0
        or value_bytes > address_inner_scale * inner_step
    ):
        raise ValueError("address coefficients cannot separate writer lanes")
    outer_maximum = (1 << outer_bound_bits) - 1
    inner_maximum = (1 << inner_bound_bits) - 1
    addresses = [
        address_outer_scale * outer + address_inner_scale * inner
        for outer in range(0, outer_maximum, outer_step)
        for inner in range(0, inner_maximum, inner_step)
    ]
    if (
        not addresses
        or len(addresses) > 256
        or any(address + value_bytes > object_bytes for address in addresses)
    ):
        raise ValueError("finite writer domain does not fit the object")
    if overlay_byte is not None and not -128 <= overlay_byte <= 127:
        raise ValueError("overlay byte must fit i8")
    if not 1 <= load_bytes <= min(8, object_bytes):
        raise ValueError("load width must fit the object")

    a_ops, a_value = _arm("a", value_bits, a_coefficients)
    b_ops, b_value = _arm("b", value_bits, b_coefficients)
    c_ops: list[str] = []
    c_value = b_value
    if not shared_leaf or shared_subtree:
        c_ops, c_value = _arm("c", value_bits, c_coefficients)
    operations = "\n".join(a_ops + b_ops + c_ops)
    inner_operands = _guard_operands(
        inner_induction, inner_constant, inner_constant_on_left
    )
    root_operands = _guard_operands(
        root_induction, root_constant, root_constant_on_left
    )
    subtree = (
        f"\n  %subtree_guard = icmp uge i{value_bits} %inner_iv, 1"
        f"\n  %shared_subtree = select i1 %subtree_guard, i{value_bits} "
        f"%inner_selected, i{value_bits} {c_value}"
        if shared_subtree else ""
    )
    root_false = "%shared_subtree" if shared_subtree else c_value
    overlay = (
        f"\n  store i8 {overlay_byte}, ptr %write_address, align 1"
        if overlay_byte is not None else ""
    )
    mixed_writer = (
        f"\n  %direct_guard = icmp uge i{value_bits} %inner_iv, 1"
        f"\n  %direct_value = select i1 %direct_guard, i{value_bits} "
        f"{a_value}, i{value_bits} {b_value}"
        f"\n  store i{value_bits} %direct_value, ptr %write_address, align 1"
        if mixed_piecewise_writer else ""
    )
    index_bits = max(1, math.ceil(math.log2(object_bytes - load_bytes + 2)))
    layout = 'target datalayout = "E-p:64:64"\n\n' if endianness == "big" else ""
    return f"""\
{layout}define i{load_bytes * 8} @generated_nested_loop_memoryphi_decision_dag(
    i{index_bits} %index, i{outer_bound_bits} %outer_count,
    i{inner_bound_bits} %inner_count, i{value_bits} %payload) {{
entry:
  %object = alloca [{object_bytes} x i8], align 8
  %outer_bound = zext i{outer_bound_bits} %outer_count to i{value_bits}
  %inner_bound = zext i{inner_bound_bits} %inner_count to i{value_bits}
  br label %outer_header

outer_header:
  %outer_iv = phi i{value_bits} [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i{value_bits} %outer_iv, %outer_bound
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i{value_bits} [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i{value_bits} %inner_iv, %inner_bound
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %outer_term = mul nuw i{value_bits} %outer_iv, {address_outer_scale}
  %inner_term = mul nuw i{value_bits} %inner_iv, {address_inner_scale}
  %flat = add nuw i{value_bits} %outer_term, %inner_term
  %write_address = getelementptr inbounds [{object_bytes} x i8], ptr %object,
      i{value_bits} 0, i{value_bits} %flat
{operations}
  %inner_guard = icmp {inner_predicate} i{value_bits} {inner_operands}
  %inner_selected = select i1 %inner_guard, i{value_bits} {a_value}, i{value_bits} {b_value}
{subtree}
  %root_guard = icmp {root_predicate} i{value_bits} {root_operands}
  %stored = select i1 %root_guard, i{value_bits} %inner_selected, i{value_bits} {root_false}
  store i{value_bits} %stored, ptr %write_address, align 1{mixed_writer}{overlay}
  %inner_next = add nuw i{value_bits} %inner_iv, {inner_step}
  br label %inner_header

outer_latch:
  %outer_next = add nuw i{value_bits} %outer_iv, {outer_step}
  br label %outer_header

exit:
  %index_value = zext i{index_bits} %index to i{value_bits}
  %read_address = getelementptr inbounds [{object_bytes} x i8], ptr %object,
      i{value_bits} 0, i{value_bits} %index_value
  %value = load i{load_bytes * 8}, ptr %read_address, align 1
  ret i{load_bytes * 8} %value
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--endianness", choices=("little", "big"), default="little")
    parser.add_argument("--shared-leaf", action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument("--mixed-piecewise-writer", action="store_true")
    parser.add_argument("--shared-subtree", action="store_true")
    parser.add_argument("--inner-predicate", choices=sorted(PREDICATES), default="ult")
    parser.add_argument("--root-predicate", choices=sorted(PREDICATES), default="ule")
    args = parser.parse_args()
    with open(args.output, "w", encoding="utf-8") as stream:
        stream.write(generate(
            endianness=args.endianness,
            shared_leaf=args.shared_leaf,
            mixed_piecewise_writer=args.mixed_piecewise_writer,
            shared_subtree=args.shared_subtree,
            inner_predicate=args.inner_predicate,
            root_predicate=args.root_predicate,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
