#!/usr/bin/env python3
"""Generate the bounded constant-value nested-loop F417 LLVM fixture."""

from __future__ import annotations

import argparse

from generate_nested_loop_memoryphi_summary_fixture import generate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object-bytes", type=int, default=64)
    parser.add_argument("--outer-step", type=int, default=1)
    parser.add_argument("--inner-step", type=int, default=8)
    parser.add_argument("--writer-widths", default="1,2,4,8")
    parser.add_argument(
        "--writer-values", default="17,4660,305419896,72623859790382856"
    )
    parser.add_argument("--load-bytes", type=int, default=8)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    widths = tuple(int(value) for value in args.writer_widths.split(","))
    values = tuple(int(value) for value in args.writer_values.split(","))
    with open(args.output, "w", encoding="utf-8") as stream:
        stream.write(
            generate(
                args.object_bytes,
                args.outer_step,
                args.inner_step,
                widths,
                args.load_bytes,
                values,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
