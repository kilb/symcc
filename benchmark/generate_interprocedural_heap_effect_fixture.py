#!/usr/bin/env python3
"""Generate a bounded many-callsite heap initializer continuation fixture."""

from __future__ import annotations

import argparse
from pathlib import Path


def generate(calls: int) -> str:
    if not 2 <= calls <= 64:
        raise ValueError("calls must be in [2, 64]")
    lines = [
        "declare ptr @malloc(i64)",
        "",
        "define internal void @generated_initialize(ptr %object) {",
        "entry:",
        "  store i8 1, ptr %object, align 1",
        "  ret void",
        "}",
        "",
        "define i8 @generated_interprocedural_heap_effect() {",
        "entry:",
    ]
    accumulator = "0"
    for index in range(calls):
        lines.extend([
            f"  %object{index} = call ptr @malloc(i64 1)",
            f"  call void @generated_initialize(ptr %object{index})",
            f"  %value{index} = load i8, ptr %object{index}, align 1",
            f"  %sum{index} = add i8 {accumulator}, %value{index}",
        ])
        accumulator = f"%sum{index}"
    lines.extend([
        f"  ret i8 {accumulator}",
        "}",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=64)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(generate(args.calls), encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
