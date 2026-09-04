#!/usr/bin/env python3
"""Generate a bounded constant-stride loop MemoryPhi fixture."""

from __future__ import annotations

import argparse


def generate(
    object_bytes: int = 64,
    stride: int = 8,
    writer_bytes: int = 8,
    load_bytes: int = 8,
) -> str:
    if not 2 <= object_bytes <= 64:
        raise ValueError("object_bytes must be in [2, 64]")
    if not 2 <= stride <= 64:
        raise ValueError("stride must be in [2, 64]")
    if not 1 <= writer_bytes <= min(8, stride, object_bytes):
        raise ValueError("writer_bytes must fit the stride and scalar limit")
    if not 1 <= load_bytes <= min(8, object_bytes):
        raise ValueError("load_bytes must fit the object and scalar limit")
    writer_bits = writer_bytes * 8
    load_bits = load_bytes * 8
    return f"""\
define i{load_bits} @generated_strided_loop_memoryphi_byte_lane(
    i8 %index, i8 %count) {{
entry:
  %object = alloca [{object_bytes} x i8], align 8
  %count64 = zext i8 %count to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %body, label %exit

body:
  %write_address = getelementptr inbounds [{object_bytes} x i8], ptr %object,
      i64 0, i64 %iv
  store i{writer_bits} 0, ptr %write_address, align 1
  br label %latch

latch:
  %next = add nuw i64 %iv, {stride}
  br label %header

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
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.output, "w", encoding="utf-8") as stream:
        stream.write(generate(
            args.object_bytes,
            args.stride,
            args.writer_bytes,
            args.load_bytes,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
