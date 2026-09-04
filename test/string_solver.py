# REQUIRES: qsym
# RUN: python3 %s %querysolver

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from string_constraints import (  # noqa: E402
    STRING_CONSTRAINT_SCHEMA,
    STRING_QUERY_SCHEMA,
    SymccJsonStringBackend,
    candidate_satisfies_string_query,
    materialize_string_candidates,
    normalize_string_constraint,
    normalize_string_query,
)


def equality_record(taken_equal: bool) -> dict:
    return normalize_string_constraint({
        "schema": STRING_CONSTRAINT_SCHEMA,
        "op": "strcmp",
        "site": 17,
        "result": 0 if taken_equal else -1,
        "taken_equal": taken_equal,
        "symbolic_side": "left",
        "token_hex": b"MAGIC".hex(),
        "nul_terminated": True,
        "complete": True,
        "patches": [
            {"offset": index, "value": value}
            for index, value in enumerate(b"MAGIC\0")
        ],
    })


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("expected query solver path")
    backend = SymccJsonStringBackend([sys.argv[1]])

    candidates, metrics = materialize_string_candidates(
        [equality_record(False)],
        b"xxxxx\0zz",
        4,
        solver_backend=backend,
        solver_timeout_ms=2000,
    )
    assert b"MAGIC\0zz" in candidates, candidates
    assert metrics["solver_verified"] == 1, metrics

    alternatives, metrics = materialize_string_candidates(
        [equality_record(True)],
        b"MAGIC\0zz",
        4,
        solver_backend=backend,
        solver_timeout_ms=2000,
    )
    assert alternatives, metrics
    assert alternatives[0].split(b"\0", 1)[0] != b"MAGIC", alternatives

    contains = normalize_string_query({
        "schema": STRING_QUERY_SCHEMA,
        "input_size": 8,
        "variables": [{
            "name": "input",
            "offset": 0,
            "capacity": 8,
            "min_length": 5,
            "max_length": 7,
            "nul_terminated": True,
        }],
        "constraints": [{
            "op": "contains",
            "needle": {"kind": "literal", "value_hex": b"MAGIC".hex()},
            "haystack": {"kind": "var", "name": "input"},
        }],
    })
    result = backend.solve(contains, 2000)
    assert result["status"] == "sat", result
    candidate = bytearray(b"xxxxxxxx")
    for offset, value in result["assignments"].items():
        candidate[int(offset)] = int(value)
    assert candidate_satisfies_string_query(contains, bytes(candidate)), (
        result, candidate)

    dual = normalize_string_query({
        "schema": STRING_QUERY_SCHEMA,
        "input_size": 6,
        "variables": [{
            "name": "input",
            "offset": 0,
            "capacity": 6,
            "min_length": 3,
            "max_length": 5,
            "nul_terminated": True,
        }],
        "constraints": [{
            "op": "contains",
            "needle": {"kind": "literal", "value_hex": b"BC".hex()},
            "haystack": {"kind": "var", "name": "input"},
        }],
        "byte_constraints": [{
            "offset": 0,
            "relation": "eq",
            "value": ord("A"),
        }],
    })
    result = backend.solve(dual, 2000)
    assert result["status"] == "sat", result
    candidate = bytearray(b"xxxxxx")
    for offset, value in result["assignments"].items():
        candidate[int(offset)] = int(value)
    assert candidate[0] == ord("A"), (result, candidate)
    assert candidate_satisfies_string_query(dual, bytes(candidate)), (
        result, candidate)

    binary = normalize_string_query({
        "schema": STRING_QUERY_SCHEMA,
        "input_size": 3,
        "variables": [{
            "name": "fixed",
            "offset": 0,
            "capacity": 3,
        }],
        "constraints": [{
            "op": "equal",
            "left": {"kind": "var", "name": "fixed"},
            "right": {"kind": "literal", "value_hex": "410142"},
        }],
    })
    result = backend.solve(binary, 2000)
    assert result["status"] == "sat", result
    candidate = bytearray(3)
    for offset, value in result["assignments"].items():
        candidate[int(offset)] = int(value)
    assert bytes(candidate) == b"A\x01B", (result, candidate)
    assert candidate_satisfies_string_query(binary, bytes(candidate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
