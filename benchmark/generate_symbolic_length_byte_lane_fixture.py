#!/usr/bin/env python3
"""Generate the bounded F410 symbolic-length byte-lane fixture."""

from __future__ import annotations

import argparse


def generate(object_bytes: int = 64, load_bytes: int = 1) -> str:
    if not 2 <= object_bytes <= 64:
        raise ValueError("object_bytes must be in [2, 64]")
    if not 1 <= load_bytes <= min(8, object_bytes):
        raise ValueError("load_bytes must fit the object and scalar limit")
    load_bits = load_bytes * 8
    return f"""\
declare ptr @memset(ptr, i32, i64)

define i{load_bits} @generated_symbolic_length_byte_lane(
    i8 %index, i8 %count) {{
entry:
  %object = alloca [{object_bytes} x i8], align 8
  %length = zext i8 %count to i64
  call ptr @memset(ptr %object, i32 90, i64 %length)
  %wide_index = zext i8 %index to i64
  %address = getelementptr inbounds [{object_bytes} x i8], ptr %object,
      i64 0, i64 %wide_index
  %value = load i{load_bits}, ptr %address, align 1
  ret i{load_bits} %value
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object-bytes", type=int, default=64)
    parser.add_argument("--load-bytes", type=int, default=1)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.output, "w", encoding="utf-8") as stream:
        stream.write(generate(args.object_bytes, args.load_bytes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
