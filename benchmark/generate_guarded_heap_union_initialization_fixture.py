#!/usr/bin/env python3
"""Generate a complete guard-tree LLVM fixture for producer boundary tests."""

from __future__ import annotations

import argparse
from pathlib import Path


def paths_at_depth(depth: int) -> list[tuple[bool, ...]]:
    paths: list[tuple[bool, ...]] = [()]
    for _level in range(depth):
        paths = [(*path, decision) for path in paths for decision in (True, False)]
    return paths


def block_name(path: tuple[bool, ...]) -> str:
    if not path:
        return "entry"
    return "node_" + "".join("t" if decision else "f" for decision in path)


def generate(depth: int) -> str:
    leaves = paths_at_depth(depth)
    lines = [
        "; Generated complete guard-tree producer fixture.",
        "declare ptr @malloc(i64)",
        "",
        "define i8 @generated_guarded_heap_union_initialization() {",
        "entry:",
    ]
    for index in range(len(leaves)):
        lines.append(f"  %pointer_{index} = call ptr @malloc(i64 1)")
    for level in range(depth):
        lines.append(f"  %guard_{level} = icmp eq i8 {level}, {level}")
    lines.append(
        "  br i1 %guard_0, label %node_t, label %node_f"
    )

    for level in range(1, depth):
        for prefix in paths_at_depth(level):
            name = block_name(prefix)
            lines.extend([
                "",
                f"{name}:",
                f"  br i1 %guard_{level}, "
                f"label %{block_name((*prefix, True))}, "
                f"label %{block_name((*prefix, False))}",
            ])

    for index, path in enumerate(leaves):
        name = block_name(path)
        lines.extend([
            "",
            f"{name}:",
            f"  store i8 {index + 1}, ptr %pointer_{index}, align 1",
            "  br label %merge",
        ])

    incoming = ", ".join(
        f"[ %pointer_{index}, %{block_name(path)} ]"
        for index, path in enumerate(leaves)
    )
    lines.extend([
        "",
        "merge:",
        f"  %selected = phi ptr {incoming}",
        "  %value = load i8, ptr %selected, align 1",
        "  ret i8 %value",
        "}",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--depth", type=int, default=6)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.depth <= 6:
        raise SystemExit("depth must be in 1..6")
    args.output.write_text(generate(args.depth), encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
