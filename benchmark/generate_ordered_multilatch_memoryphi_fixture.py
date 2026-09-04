#!/usr/bin/env python3
"""Generate the sealed maximum-chain F415 MemoryPhi fixture."""

from __future__ import annotations

import argparse


def generate(
    object_bytes: int = 64,
    stride: int = 8,
    writer_bytes: int = 8,
    load_bytes: int = 8,
    writers_per_transfer: int = 4,
) -> str:
    if not 2 <= object_bytes <= 64:
        raise ValueError("object_bytes must be in [2, 64]")
    if not 1 <= stride <= 64:
        raise ValueError("stride must be in [1, 64]")
    if not 1 <= writer_bytes <= min(8, stride, object_bytes):
        raise ValueError("writer_bytes must fit stride and scalar limits")
    if not 1 <= load_bytes <= min(8, object_bytes):
        raise ValueError("load_bytes must fit object and scalar limits")
    if not 2 <= writers_per_transfer <= 4:
        raise ValueError("writers_per_transfer must be in [2, 4]")
    writer_bits = writer_bytes * 8
    load_bits = load_bytes * 8

    def latch(name: str) -> str:
        stores = "\n".join(
            f"  store i{writer_bits} {ordinal + 1}, ptr %address_{name}, align 1"
            for ordinal in range(writers_per_transfer)
        )
        return f"""\
latch_{name}:
  %address_{name} = getelementptr inbounds [{object_bytes} x i8], ptr %object,
      i64 0, i64 %iv
{stores}
  %next_{name} = add nuw i64 %iv, {stride}
  br label %header
"""

    return f"""\
define i{load_bits} @generated_ordered_multilatch_memoryphi_transfer(
    i8 %index, i8 %count, i8 %root_limit, i8 %leaf_limit) {{
entry:
  %object = alloca [{object_bytes} x i8], align 8
  %count64 = zext i8 %count to i64
  %root64 = zext i8 %root_limit to i64
  %leaf64 = zext i8 %leaf_limit to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next_a, %latch_a ], [ %next_b, %latch_b ], [ %next_c, %latch_c ], [ %next_d, %latch_d ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %root, label %exit

root:
  %go_left = icmp ult i64 %iv, %root64
  br i1 %go_left, label %left, label %right

left:
  %choose_a = icmp ult i64 %iv, %leaf64
  br i1 %choose_a, label %latch_a, label %latch_b

right:
  %choose_c = icmp uge i64 %iv, %leaf64
  br i1 %choose_c, label %latch_c, label %latch_d

{latch("a")}
{latch("b")}
{latch("c")}
{latch("d")}
exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [{object_bytes} x i8], ptr %object,
      i64 0, i64 %index64
  %value = load i{load_bits}, ptr %read_address, align 1
  ret i{load_bits} %value
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object-bytes", type=int, default=64)
    parser.add_argument("--stride", type=int, default=8)
    parser.add_argument("--writer-bytes", type=int, default=8)
    parser.add_argument("--load-bytes", type=int, default=8)
    parser.add_argument("--writers-per-transfer", type=int, default=4)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.output, "w", encoding="utf-8") as stream:
        stream.write(
            generate(
                args.object_bytes,
                args.stride,
                args.writer_bytes,
                args.load_bytes,
                args.writers_per_transfer,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
