#!/usr/bin/env python3
"""Generate a sealed two-level affine-symbolic-value F419 LLVM fixture."""

from __future__ import annotations

import argparse
import math


def generate(
    object_bytes: int = 24,
    outer_step: int = 1,
    inner_step: int = 2,
    outer_bound_bits: int = 2,
    inner_bound_bits: int = 3,
    address_outer_scale: int = 8,
    address_inner_scale: int = 1,
    value_bits: int = 16,
    value_constant: int = 17,
    value_outer_scale: int = 5,
    value_inner_scale: int = 7,
    value_input_scale: int = 3,
    overlay_byte: int | None = -86,
    load_bytes: int = 2,
) -> str:
    if not 2 <= object_bytes <= 64:
        raise ValueError("object_bytes must be in [2, 64]")
    if not 1 <= outer_step <= 64 or not 1 <= inner_step <= 64:
        raise ValueError("loop steps must be in [1, 64]")
    if not 1 <= outer_bound_bits < 64 or not 1 <= inner_bound_bits < 64:
        raise ValueError("bound widths must be in [1, 63]")
    if value_bits not in {8, 16, 24, 32, 40, 48, 56, 64}:
        raise ValueError("value_bits must be a byte-complete width up to 64")
    value_bytes = value_bits // 8
    coefficients = (
        value_constant, value_outer_scale,
        value_inner_scale, value_input_scale,
    )
    if any(
        type(value) is not int or not 0 <= value <= (1 << 63) - 1
        for value in coefficients
    ) or not any(coefficients[1:]):
        raise ValueError("value coefficients are outside the sealed domain")
    if (
        not 1 <= address_outer_scale <= (1 << 63) - 1
        or not 1 <= address_inner_scale <= (1 << 63) - 1
        or value_bytes > address_inner_scale * inner_step
    ):
        raise ValueError("address coefficients cannot separate writer lanes")
    if overlay_byte is not None and not -128 <= overlay_byte <= 127:
        raise ValueError("overlay_byte must fit i8")
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
        or any(index < 0 or index + value_bytes > object_bytes for index in instances)
    ):
        raise ValueError("the finite affine instance domain does not fit")
    index_bits = max(1, math.ceil(math.log2(object_bytes - load_bytes + 2)))

    value_terms: list[tuple[str, int]] = []
    if value_input_scale:
        value_terms.append(("%payload", value_input_scale))
    if value_outer_scale:
        value_terms.append(("%outer_iv", value_outer_scale))
    if value_inner_scale:
        value_terms.append(("%inner_iv", value_inner_scale))
    operations: list[str] = []
    values: list[str] = []
    for term_index, (source, coefficient) in enumerate(value_terms):
        if coefficient == 1:
            values.append(source)
        else:
            name = f"%value_term_{term_index}"
            operations.append(
                f"  {name} = mul i{value_bits} {source}, {coefficient}"
            )
            values.append(name)
    current = values[0]
    for term_index, value in enumerate(values[1:], start=1):
        name = f"%value_sum_{term_index}"
        operations.append(f"  {name} = add i{value_bits} {current}, {value}")
        current = name
    if value_constant:
        operations.append(
            f"  %stored = add i{value_bits} {current}, {value_constant}"
        )
        current = "%stored"
    value_operations = "\n".join(operations)
    overlay = (
        f"\n  store i8 {overlay_byte}, ptr %write_address, align 1"
        if overlay_byte is not None else ""
    )
    return f"""\
define i{load_bytes * 8} @generated_nested_loop_memoryphi_affine_symbolic_value(
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
{value_operations}
  store i{value_bits} {current}, ptr %write_address, align 1{overlay}
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
    parser.add_argument("--value-constant", type=int, default=17)
    parser.add_argument("--value-outer-scale", type=int, default=5)
    parser.add_argument("--value-inner-scale", type=int, default=7)
    parser.add_argument("--value-input-scale", type=int, default=3)
    parser.add_argument("--overlay-byte", default="-86")
    parser.add_argument("--load-bytes", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    overlay = None if args.overlay_byte == "none" else int(args.overlay_byte)
    with open(args.output, "w", encoding="utf-8") as stream:
        stream.write(generate(
            args.object_bytes, args.outer_step, args.inner_step,
            args.outer_bound_bits, args.inner_bound_bits,
            args.address_outer_scale, args.address_inner_scale,
            args.value_bits, args.value_constant, args.value_outer_scale,
            args.value_inner_scale, args.value_input_scale, overlay,
            args.load_bytes,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
