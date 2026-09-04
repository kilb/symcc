#!/usr/bin/env python3
"""Generate a sealed guard-specialized piecewise-affine F420 fixture."""

from __future__ import annotations

import argparse
import math


PREDICATES = {"eq", "ne", "ugt", "uge", "ult", "ule"}


def _arm(
    prefix: str,
    bits: int,
    constant: int,
    outer_scale: int,
    inner_scale: int,
    input_scale: int,
) -> tuple[list[str], str]:
    terms: list[tuple[str, int]] = []
    if input_scale:
        terms.append(("%payload", input_scale))
    if outer_scale:
        terms.append(("%outer_iv", outer_scale))
    if inner_scale:
        terms.append(("%inner_iv", inner_scale))
    operations: list[str] = []
    values: list[str] = []
    for index, (source, coefficient) in enumerate(terms):
        if coefficient == 1:
            values.append(source)
        else:
            name = f"%{prefix}_term_{index}"
            operations.append(f"  {name} = mul i{bits} {source}, {coefficient}")
            values.append(name)
    if not values:
        return operations, str(constant)
    current = values[0]
    for index, value in enumerate(values[1:], start=1):
        name = f"%{prefix}_sum_{index}"
        operations.append(f"  {name} = add i{bits} {current}, {value}")
        current = name
    if constant:
        name = f"%{prefix}_value"
        operations.append(f"  {name} = add i{bits} {current}, {constant}")
        current = name
    return operations, current


def generate(
    object_bytes: int = 24,
    outer_step: int = 1,
    inner_step: int = 2,
    outer_bound_bits: int = 2,
    inner_bound_bits: int = 3,
    address_outer_scale: int = 8,
    address_inner_scale: int = 1,
    value_bits: int = 16,
    guard_predicate: str = "ult",
    guard_induction: str = "inner",
    guard_constant: int = 2,
    constant_on_left: bool = False,
    true_coefficients: tuple[int, int, int, int] = (17, 5, 7, 3),
    false_coefficients: tuple[int, int, int, int] = (1025, 2, 11, 3),
    overlay_byte: int | None = -86,
    load_bytes: int = 2,
    endianness: str = "little",
) -> str:
    if not 2 <= object_bytes <= 64:
        raise ValueError("object_bytes must be in [2, 64]")
    if not 1 <= outer_step <= 64 or not 1 <= inner_step <= 64:
        raise ValueError("loop steps must be in [1, 64]")
    if not 1 <= outer_bound_bits < 64 or not 1 <= inner_bound_bits < 64:
        raise ValueError("bound widths must be in [1, 63]")
    if value_bits not in {8, 16, 24, 32, 40, 48, 56, 64}:
        raise ValueError("value_bits must be byte-complete and at most 64")
    if guard_predicate not in PREDICATES:
        raise ValueError("unsupported guard predicate")
    if guard_induction not in {"outer", "inner"}:
        raise ValueError("guard_induction must be outer or inner")
    if type(guard_constant) is not int or not 0 <= guard_constant <= (1 << 63) - 1:
        raise ValueError("guard_constant is outside the sealed domain")
    coefficients = true_coefficients + false_coefficients
    if any(type(value) is not int or not 0 <= value <= (1 << 63) - 1
           for value in coefficients):
        raise ValueError("arm coefficients are outside the sealed domain")
    if true_coefficients == false_coefficients:
        raise ValueError("piecewise arms must differ")
    value_bytes = value_bits // 8
    if (
        not 1 <= address_outer_scale <= (1 << 63) - 1
        or not 1 <= address_inner_scale <= (1 << 63) - 1
        or value_bytes > address_inner_scale * inner_step
    ):
        raise ValueError("address coefficients cannot separate writer lanes")
    if overlay_byte is not None and not -128 <= overlay_byte <= 127:
        raise ValueError("overlay_byte must fit i8")
    if endianness not in {"little", "big"}:
        raise ValueError("endianness must be little or big")
    if not 1 <= load_bytes <= min(8, object_bytes):
        raise ValueError("load_bytes must fit the object")
    outer_maximum = (1 << outer_bound_bits) - 1
    inner_maximum = (1 << inner_bound_bits) - 1
    instances = [
        address_outer_scale * outer + address_inner_scale * inner
        for outer in range(0, outer_maximum, outer_step)
        for inner in range(0, inner_maximum, inner_step)
    ]
    if (
        not instances
        or len(instances) > 256
        or any(index + value_bytes > object_bytes for index in instances)
    ):
        raise ValueError("finite affine instance domain does not fit")
    index_bits = max(1, math.ceil(math.log2(object_bytes - load_bytes + 2)))
    true_ops, true_value = _arm(
        "true", value_bits, *true_coefficients
    )
    false_ops, false_value = _arm(
        "false", value_bits, *false_coefficients
    )
    guard_variable = "%outer_iv" if guard_induction == "outer" else "%inner_iv"
    guard_operands = (
        f"{guard_constant}, {guard_variable}"
        if constant_on_left else f"{guard_variable}, {guard_constant}"
    )
    operations = "\n".join(true_ops + false_ops)
    overlay = (
        f"\n  store i8 {overlay_byte}, ptr %write_address, align 1"
        if overlay_byte is not None else ""
    )
    layout = 'target datalayout = "E-p:64:64"\n\n' if endianness == "big" else ""
    return f"""\
{layout}define i{load_bytes * 8} @generated_nested_loop_memoryphi_piecewise_affine_value(
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
  %piecewise_guard = icmp {guard_predicate} i{value_bits} {guard_operands}
  %stored = select i1 %piecewise_guard, i{value_bits} {true_value}, i{value_bits} {false_value}
  store i{value_bits} %stored, ptr %write_address, align 1{overlay}
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
    parser.add_argument("--object-bytes", type=int, default=24)
    parser.add_argument("--outer-step", type=int, default=1)
    parser.add_argument("--inner-step", type=int, default=2)
    parser.add_argument("--outer-bound-bits", type=int, default=2)
    parser.add_argument("--inner-bound-bits", type=int, default=3)
    parser.add_argument("--address-outer-scale", type=int, default=8)
    parser.add_argument("--address-inner-scale", type=int, default=1)
    parser.add_argument("--value-bits", type=int, default=16)
    parser.add_argument("--guard-predicate", choices=sorted(PREDICATES), default="ult")
    parser.add_argument("--guard-induction", choices=("outer", "inner"), default="inner")
    parser.add_argument("--guard-constant", type=int, default=2)
    parser.add_argument("--constant-on-left", action="store_true")
    parser.add_argument("--true-coefficients", default="17,5,7,3")
    parser.add_argument("--false-coefficients", default="1025,2,11,3")
    parser.add_argument("--overlay-byte", default="-86")
    parser.add_argument("--load-bytes", type=int, default=2)
    parser.add_argument("--endianness", choices=("little", "big"), default="little")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    def coefficients(raw: str) -> tuple[int, int, int, int]:
        values = tuple(int(item) for item in raw.split(","))
        if len(values) != 4:
            raise ValueError("arm coefficients require constant,outer,inner,input")
        return values  # type: ignore[return-value]

    overlay = None if args.overlay_byte == "none" else int(args.overlay_byte)
    payload = generate(
        args.object_bytes, args.outer_step, args.inner_step,
        args.outer_bound_bits, args.inner_bound_bits,
        args.address_outer_scale, args.address_inner_scale, args.value_bits,
        args.guard_predicate, args.guard_induction, args.guard_constant,
        args.constant_on_left, coefficients(args.true_coefficients),
        coefficients(args.false_coefficients), overlay, args.load_bytes,
        args.endianness,
    )
    with open(args.output, "w", encoding="utf-8") as stream:
        stream.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
