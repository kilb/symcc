#!/usr/bin/env python3
"""Check SymCC string-constraint artifacts in lit tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from string_constraints import (
    load_string_constraints,
    materialize_string_candidates,
)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        raise SystemExit(
            "usage: check_string_constraints.py <jsonl> <witness-hex> <target>")
    records = load_string_constraints(argv[1])
    witness = bytes.fromhex(argv[2])
    target = argv[3].encode()
    candidates, metrics = materialize_string_candidates(
        records, witness, 16, lambda value: value.startswith(target + b"\0"))
    assert records, "no string constraints recorded"
    assert any(record["op"] == "strcmp" for record in records), records
    assert any(record["complete"] for record in records), records
    assert candidates, (records, metrics)
    assert candidates[0].startswith(target + b"\0"), candidates
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
