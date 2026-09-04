#!/usr/bin/env python3
"""Validate runtime string-operation artifacts through the dual-view solver."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from string_constraints import (  # noqa: E402
    STRING_OPERATION_SCHEMA,
    SymccJsonStringBackend,
    candidate_satisfies_string_query,
    load_string_constraints,
    string_query_from_constraint,
)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        raise SystemExit(
            "usage: check_string_operations.py ARTIFACT WITNESS_HEX SOLVER")
    records = [
        record for record in load_string_constraints(argv[1])
        if record["schema"] == STRING_OPERATION_SCHEMA
    ]
    witness = bytes.fromhex(argv[2])
    by_op = {record["op"]: record for record in records}
    assert set(by_op) == {
        "strlen", "strchr", "strstr", "atoi", "strtol10", "strtoul10"}, records
    assert by_op["strlen"]["observed_length"] == 3
    assert by_op["strchr"]["observed_index"] == -1
    assert by_op["strstr"]["observed_index"] == 1
    assert by_op["atoi"]["observed_value"] == 123
    assert by_op["strtol10"]["observed_value"] == -123
    assert by_op["strtol10"]["integer_bits"] in {32, 64}
    assert by_op["strtoul10"]["observed_value"] == 456
    assert by_op["strtoul10"]["integer_bits"] in {32, 64}
    assert sum(record["op"] == "strtol10" for record in records) == 1
    assert sum(record["op"] == "strtoul10" for record in records) == 1

    backend = SymccJsonStringBackend([argv[3]])
    for operation in (
            "strlen", "strchr", "strstr", "atoi", "strtol10", "strtoul10"):
        query = string_query_from_constraint(by_op[operation], len(witness))
        assert query is not None, by_op[operation]
        assert not candidate_satisfies_string_query(query, witness)
        result = backend.solve(query, 2000)
        assert result["status"] == "sat", (operation, result)
        candidate = bytearray(witness)
        for offset, value in result["assignments"].items():
            candidate[int(offset)] = int(value)
        assert candidate_satisfies_string_query(
            query, bytes(candidate)), (operation, result, candidate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
