#!/usr/bin/env python3
"""Run a command and require a specific process exit status."""

from __future__ import annotations

import os
import subprocess
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 4 or "--" not in argv:
        print(
            "usage: expect_exit.py EXPECTED [NAME=VALUE ...] -- COMMAND [ARG ...]",
            file=sys.stderr,
        )
        return 2

    separator = argv.index("--")
    try:
        expected = int(argv[1], 0)
    except ValueError:
        print(f"invalid expected exit status: {argv[1]}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    for assignment in argv[2:separator]:
        if "=" not in assignment:
            print(f"invalid environment assignment: {assignment}", file=sys.stderr)
            return 2
        name, value = assignment.split("=", 1)
        env[name] = value

    command = argv[separator + 1 :]
    if not command:
        print("missing command", file=sys.stderr)
        return 2

    completed = subprocess.run(command, env=env)
    if completed.returncode != expected:
        print(
            f"expected exit status {expected}, got {completed.returncode}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
