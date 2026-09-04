#!/usr/bin/env python3
"""Generate the sealed two-level, four-writer F416 LLVM fixture."""

from __future__ import annotations

import argparse


def generate(
    object_bytes: int = 64,
    outer_step: int = 1,
    inner_step: int = 8,
    writer_widths: tuple[int, ...] = (1, 2, 4, 8),
    load_bytes: int = 8,
    writer_values: tuple[int, ...] | None = None,
) -> str:
    if not 2 <= object_bytes <= 64:
        raise ValueError("object_bytes must be in [2, 64]")
    if not 1 <= outer_step <= 64 or not 1 <= inner_step <= 64:
        raise ValueError("loop steps must be in [1, 64]")
    if not 1 <= len(writer_widths) <= 4:
        raise ValueError("writer_widths must contain 1 to 4 widths")
    if any(
        not 1 <= width <= min(8, inner_step, object_bytes)
        for width in writer_widths
    ):
        raise ValueError("writer widths must fit the stride and scalar limits")
    if not 1 <= load_bytes <= min(8, object_bytes):
        raise ValueError("load_bytes must fit object and scalar limits")
    if writer_values is None:
        writer_values = tuple(range(1, len(writer_widths) + 1))
    if len(writer_values) != len(writer_widths) or any(
        type(value) is not int
        or not -(1 << (width * 8 - 1))
        <= value
        < (1 << (width * 8 - 1))
        for width, value in zip(writer_widths, writer_values)
    ):
        raise ValueError("writer values must fit their signed integer widths")
    stores = "\n".join(
        f"  store i{width * 8} {value}, ptr %write_address, align 1"
        for width, value in zip(writer_widths, writer_values)
    )
    return f"""\
define i{load_bytes * 8} @generated_nested_loop_memoryphi_summary(
    i8 %index, i8 %outer_count, i8 %inner_count) {{
entry:
  %object = alloca [{object_bytes} x i8], align 8
  %outer_count64 = zext i8 %outer_count to i64
  %inner_count64 = zext i8 %inner_count to i64
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
  %write_address = getelementptr inbounds [{object_bytes} x i8], ptr %object,
      i64 0, i64 %inner_iv
{stores}
  %inner_next = add nuw i64 %inner_iv, {inner_step}
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, {outer_step}
  br label %outer_header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [{object_bytes} x i8], ptr %object,
      i64 0, i64 %index64
  %value = load i{load_bytes * 8}, ptr %read_address, align 1
  ret i{load_bytes * 8} %value
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object-bytes", type=int, default=64)
    parser.add_argument("--outer-step", type=int, default=1)
    parser.add_argument("--inner-step", type=int, default=8)
    parser.add_argument("--writer-widths", default="1,2,4,8")
    parser.add_argument("--load-bytes", type=int, default=8)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    widths = tuple(int(value) for value in args.writer_widths.split(","))
    with open(args.output, "w", encoding="utf-8") as stream:
        stream.write(
            generate(
                args.object_bytes,
                args.outer_step,
                args.inner_step,
                widths,
                args.load_bytes,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
