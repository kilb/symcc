#!/usr/bin/env python3
"""Generate a sealed two-level 2D-affine F418 LLVM fixture."""

from __future__ import annotations

import argparse
import math


def generate(
    object_bytes: int = 24,
    outer_step: int = 1,
    inner_step: int = 2,
    outer_bound_bits: int = 2,
    inner_bound_bits: int = 3,
    affine_constant: int = 0,
    affine_outer_scale: int = 8,
    affine_inner_scale: int = 1,
    writer_widths: tuple[int, ...] = (2, 1),
    writer_values: tuple[int, ...] = (4660, -86),
    load_bytes: int = 2,
) -> str:
    if not 2 <= object_bytes <= 64:
        raise ValueError("object_bytes must be in [2, 64]")
    if not 1 <= outer_step <= 64 or not 1 <= inner_step <= 64:
        raise ValueError("loop steps must be in [1, 64]")
    if not 1 <= outer_bound_bits < 64 or not 1 <= inner_bound_bits < 64:
        raise ValueError("bound widths must be in [1, 63]")
    if (
        not 0 <= affine_constant <= (1 << 63) - 1
        or not 1 <= affine_outer_scale <= (1 << 63) - 1
        or not 1 <= affine_inner_scale <= (1 << 63) - 1
    ):
        raise ValueError("affine coefficients are outside the sealed domain")
    if not 1 <= len(writer_widths) <= 4 or len(writer_values) != len(writer_widths):
        raise ValueError("one to four writer widths and values are required")
    inner_stride = affine_inner_scale * inner_step
    if any(not 1 <= width <= min(8, object_bytes, inner_stride) for width in writer_widths):
        raise ValueError("writer widths must fit the inner address stride")
    if any(
        type(value) is not int
        or not -(1 << (width * 8 - 1)) <= value < (1 << (width * 8 - 1))
        for width, value in zip(writer_widths, writer_values)
    ):
        raise ValueError("writer values must fit their signed widths")
    if not 1 <= load_bytes <= min(8, object_bytes):
        raise ValueError("load_bytes must fit the object")
    outer_maximum = (1 << outer_bound_bits) - 1
    inner_maximum = (1 << inner_bound_bits) - 1
    instances = [
        affine_constant + affine_outer_scale * outer + affine_inner_scale * inner
        for outer in range(0, outer_maximum, outer_step)
        for inner in range(0, inner_maximum, inner_step)
    ]
    if (
        not instances
        or len(instances) > 256
        or any(index < 0 or index + max(writer_widths) > object_bytes for index in instances)
    ):
        raise ValueError("the finite affine instance domain does not fit the object")
    index_bits = max(1, math.ceil(math.log2(object_bytes - load_bytes + 2)))
    stores = "\n".join(
        f"  store i{width * 8} {value}, ptr %write_address, align 1"
        for width, value in zip(writer_widths, writer_values)
    )
    constant_add = (
        f"  %flat = add nuw i64 %affine_sum, {affine_constant}\n"
        if affine_constant else ""
    )
    final_index = "%flat" if affine_constant else "%affine_sum"
    return f"""\
define i{load_bytes * 8} @generated_nested_loop_memoryphi_two_dimensional_affine(
    i{index_bits} %index, i{outer_bound_bits} %outer_count,
    i{inner_bound_bits} %inner_count) {{
entry:
  %object = alloca [{object_bytes} x i8], align 8
  %outer_count64 = zext i{outer_bound_bits} %outer_count to i64
  %inner_count64 = zext i{inner_bound_bits} %inner_count to i64
  br label %outer_header

outer_header:
  %outer_iv = phi i64 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i64 %outer_iv, %outer_count64
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i64 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i64 %inner_iv, %inner_count64
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %outer_term = mul nuw i64 %outer_iv, {affine_outer_scale}
  %inner_term = mul nuw i64 %inner_iv, {affine_inner_scale}
  %affine_sum = add nuw i64 %outer_term, %inner_term
{constant_add}  %write_address = getelementptr inbounds [{object_bytes} x i8], ptr %object,
      i64 0, i64 {final_index}
{stores}
  %inner_next = add nuw i64 %inner_iv, {inner_step}
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, {outer_step}
  br label %outer_header

exit:
  %index64 = zext i{index_bits} %index to i64
  %read_address = getelementptr inbounds [{object_bytes} x i8], ptr %object,
      i64 0, i64 %index64
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
    parser.add_argument("--affine-constant", type=int, default=0)
    parser.add_argument("--affine-outer-scale", type=int, default=8)
    parser.add_argument("--affine-inner-scale", type=int, default=1)
    parser.add_argument("--writer-widths", default="2,1")
    parser.add_argument("--writer-values", default="4660,-86")
    parser.add_argument("--load-bytes", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.output, "w", encoding="utf-8") as stream:
        stream.write(generate(
            args.object_bytes, args.outer_step, args.inner_step,
            args.outer_bound_bits, args.inner_bound_bits,
            args.affine_constant, args.affine_outer_scale,
            args.affine_inner_scale,
            tuple(int(value) for value in args.writer_widths.split(",")),
            tuple(int(value) for value in args.writer_values.split(",")),
            args.load_bytes,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
