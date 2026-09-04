#!/usr/bin/env python3
"""Emit a minimal F337 descriptor-relative bounded deletion syscall trace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

from distributed_state import durable_rmtree_step  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="symcc-f337-syscall-") as tmp:
        tree = Path(tmp) / "f337-retired-root"
        tree.mkdir()
        for index in range(4):
            (tree / f"f337-entry-{index}").write_bytes(b"retired")
        result = durable_rmtree_step(
            str(tree), entry_limit=2, time_limit=10.0)
        remaining = sorted(path.name for path in tree.iterdir())
        for index, path in enumerate(tree.iterdir()):
            path.rename(tree / f"fixture-cleanup-{index}")
        tree.rename(Path(tmp) / "fixture-cleanup-root")

    observation = {
        "removed_entries": result.removed_entries,
        "complete": result.complete,
        "stop_reason": result.stop_reason,
        "remaining_entries": remaining,
    }
    if (
        observation["removed_entries"] != 2
        or observation["complete"] is not False
        or observation["stop_reason"] != "entry-limit"
        or len(observation["remaining_entries"]) != 2
    ):
        raise SystemExit(f"unexpected bounded deletion: {observation}")
    args.output.write_text(
        json.dumps(observation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(observation, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
