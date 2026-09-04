#!/usr/bin/env python3
"""Execute the portable String/BV backend conformance matrix."""

from __future__ import annotations

import argparse
import json
import shlex
from collections.abc import Mapping
from typing import Any

from string_constraints import (
    STRING_OPERATION_SCHEMA,
    STRING_QUERY_SCHEMA,
    SymccJsonStringBackend,
    candidate_satisfies_string_query,
    normalize_string_operation,
    normalize_string_query,
    string_query_from_constraint,
    string_solver_backend_from_configuration,
)


def _operation_query(
    op: str,
    content: bytes,
    observed_value: int,
    integer_bits: int,
) -> dict[str, Any]:
    operation = normalize_string_operation({
        "schema": STRING_OPERATION_SCHEMA,
        "op": op,
        "site": 1,
        "symbolic_role": "value",
        "constant_hex": "",
        "observed_length": len(content) - 1,
        "observed_index": len(content) - 1,
        "observed_value": observed_value,
        "integer_bits": integer_bits,
        "complete": True,
        "input_bytes": [
            {"offset": offset, "value": value}
            for offset, value in enumerate(content)
        ],
    })
    query = string_query_from_constraint(operation, len(content))
    assert query is not None
    return query


def conformance_cases() -> list[tuple[str, dict[str, Any], bytes]]:
    binary = normalize_string_query({
        "schema": STRING_QUERY_SCHEMA,
        "input_size": 3,
        "variables": [{"name": "fixed", "offset": 0, "capacity": 3}],
        "constraints": [{
            "op": "equal",
            "left": {"kind": "var", "name": "fixed"},
            "right": {"kind": "literal", "value_hex": "410142"},
        }],
    })
    contains = normalize_string_query({
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
            "needle": {"kind": "literal", "value_hex": "4243"},
            "haystack": {"kind": "var", "name": "input"},
        }],
        "byte_constraints": [{
            "offset": 0, "relation": "eq", "value": ord("A"),
        }],
    })
    indexof = normalize_string_query({
        "schema": STRING_QUERY_SCHEMA,
        "input_size": 4,
        "variables": [{
            "name": "input",
            "offset": 0,
            "capacity": 4,
            "min_length": 0,
            "max_length": 3,
            "nul_terminated": True,
        }],
        "constraints": [{
            "op": "indexof",
            "haystack": {"kind": "var", "name": "input"},
            "needle": {"kind": "literal", "value_hex": "5a"},
            "start": 0,
            "relation": "ne",
            "index": -1,
        }],
    })
    return [
        ("binary", binary, b"xxx"),
        ("contains_bv", contains, b"xxxxx\0"),
        ("indexof_negative", indexof, b"ABC\0"),
        ("atoi", _operation_query("atoi", b"123\0", 123, 32), b"123\0"),
        (
            "strtol10",
            _operation_query("strtol10", b"-123\0", -123, 64),
            b"-123\0",
        ),
        (
            "strtoul10",
            _operation_query("strtoul10", b"456\0", 456, 64),
            b"456\0",
        ),
    ]


def _materialize(
    witness: bytes,
    assignments: Any,
) -> bytes | None:
    if not isinstance(assignments, Mapping):
        return None
    candidate = bytearray(witness)
    try:
        for raw_offset, raw_value in assignments.items():
            offset = int(raw_offset)
            value = int(raw_value)
            if not 0 <= offset < len(candidate) or not 0 <= value <= 255:
                return None
            candidate[offset] = value
    except (TypeError, ValueError):
        return None
    return bytes(candidate)


def run_conformance(
    backend: Any,
    timeout_ms: int,
) -> tuple[dict[str, Any], bool]:
    evidence: dict[str, Any] = {
        "schema": "symcc-string-backend-conformance-v1",
        "backend": str(getattr(backend, "name", "unknown")),
        "cases": [],
    }
    passed = True
    for name, query, witness in conformance_cases():
        solve_all = getattr(backend, "solve_all_conformance", None)
        if not callable(solve_all):
            solve_all = getattr(backend, "solve_all", None)
        results = (
            solve_all(query, timeout_ms)
            if callable(solve_all)
            else [backend.solve(query, timeout_ms)]
        )
        case_results = []
        for result in results:
            status = str(result.get("status", "error"))
            candidate = _materialize(witness, result.get("assignments", {}))
            verified = (
                status == "sat" and candidate is not None and
                candidate_satisfies_string_query(query, candidate)
            )
            case_results.append({
                "solver": str(result.get("solver", ""))[:64],
                "status": status,
                "verified": verified,
                "assignments": len(result.get("assignments", {}))
                if isinstance(result.get("assignments", {}), Mapping) else 0,
                "elapsed_us": max(0, int(result.get("elapsed_us", 0))),
                "reason": str(result.get("reason", ""))[:256],
            })
            passed = passed and verified
        if not case_results:
            passed = False
        evidence["cases"].append({
            "name": name,
            "results": case_results,
        })
    evidence["passed"] = passed
    return evidence, passed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        help="JSON/path backend or portfolio configuration",
    )
    parser.add_argument(
        "--solver",
        help="single symcc-query-solver command when --backend is omitted",
    )
    parser.add_argument("--timeout-ms", type=int, default=5000)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.backend:
        backend = string_solver_backend_from_configuration(args.backend)
    else:
        command = shlex.split(args.solver) if args.solver else None
        backend = SymccJsonStringBackend(command)
    evidence, passed = run_conformance(
        backend, max(1, min(args.timeout_ms, 60000)))
    print(json.dumps(evidence, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
