#!/usr/bin/env python3
"""Generate a maximum-fan-in MemorySSA heap-initialization fixture."""

from __future__ import annotations

import argparse
from pathlib import Path


def fixture(paths: int) -> str:
    if not 2 <= paths <= 64:
        raise ValueError("paths must be in [2, 64]")
    lines = [
        "declare ptr @malloc(i64)",
        "",
        "define i8 @generated_memoryssa_aa_heap_initialization() {",
        "entry:",
    ]
    for index in range(paths):
        lines.append(f"  %object{index} = call ptr @malloc(i64 1)")
    lines.append("  %scratch = call ptr @malloc(i64 1)")
    lines.extend([
        "  switch i8 0, label %initialize0 [",
        *[
            f"    i8 {index}, label %initialize{index}"
            for index in range(1, paths)
        ],
        "  ]",
        "",
    ])
    for index in range(paths):
        lines.extend([
            f"initialize{index}:",
            f"  store i8 1, ptr %object{index}, align 1",
            f"  store i8 {index + 1}, ptr %scratch, align 1",
            "  br label %merge",
            "",
        ])
    incoming = ",\n".join(
        f"    [ %object{index}, %initialize{index} ]"
        for index in range(paths)
    )
    lines.extend([
        "merge:",
        "  %selected = phi ptr",
        incoming,
        "  %value = load i8, ptr %selected, align 1",
        "  ret i8 %value",
        "}",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paths", type=int, default=64)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(fixture(args.paths), encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
