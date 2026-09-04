#!/usr/bin/env python3
"""Versioned string-constraint artifacts and candidate materialization."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

import fcntl


STRING_CONSTRAINT_SCHEMA = "symcc-string-constraint-v1"
STRING_OPERATION_SCHEMA = "symcc-string-operation-v1"
STRING_QUERY_SCHEMA = "symcc-string-query-v2"
_LEGACY_STRING_QUERY_SCHEMAS = {"symcc-string-query-v1"}
_MAX_OFFSET = (1 << 32) - 1
_MAX_STRING_BYTES = 4096
_RELATIONS = {"eq", "ne", "lt", "le", "gt", "ge"}
_BYTE_RELATIONS = {"eq", "ne", "ult", "ule", "ugt", "uge"}


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise ValueError(f"{name} must be in [{lower}, {upper}]")
    return value


def _bounded_hex(value: Any, name: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or len(value) > maximum_bytes * 2:
        raise ValueError(f"{name} must be bounded hex")
    if len(value) % 2:
        raise ValueError(f"{name} must have even length")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be valid hex") from exc
    return value.lower()


def _query_name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError(f"{name} must be a bounded string")
    if not value[0].isalpha() or not all(
            character.isalnum() or character == "_" for character in value):
        raise ValueError(f"{name} must be an ASCII identifier")
    if not value.isascii():
        raise ValueError(f"{name} must be an ASCII identifier")
    return value


def _normalize_term(raw: Any, variables: set[str], depth: int = 0) -> dict[str, Any]:
    if depth > 8 or not isinstance(raw, Mapping):
        raise ValueError("string term must be a bounded object")
    kind = str(raw.get("kind", ""))
    if kind == "var":
        name = _query_name(raw.get("name"), "term variable")
        if name not in variables:
            raise ValueError("term references an unknown variable")
        return {"kind": kind, "name": name}
    if kind == "literal":
        return {
            "kind": kind,
            "value_hex": _bounded_hex(
                raw.get("value_hex", ""), "literal value", _MAX_STRING_BYTES),
        }
    if kind == "concat":
        items_raw = raw.get("items")
        if not isinstance(items_raw, Sequence) or isinstance(
                items_raw, (str, bytes)) or not 1 <= len(items_raw) <= 16:
            raise ValueError("concat items must be a bounded non-empty list")
        return {
            "kind": kind,
            "items": [
                _normalize_term(item, variables, depth + 1)
                for item in items_raw
            ],
        }
    if kind == "substr":
        return {
            "kind": kind,
            "value": _normalize_term(raw.get("value"), variables, depth + 1),
            "offset": _integer(
                raw.get("offset"), "substring offset", 0, _MAX_STRING_BYTES),
            "length": _integer(
                raw.get("length"), "substring length", 0, _MAX_STRING_BYTES),
        }
    raise ValueError("unsupported string term kind")


def _normalize_predicate(
    raw: Any,
    variables: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("string predicate must be an object")
    op = str(raw.get("op", ""))
    if op in {"equal", "distinct"}:
        return {
            "op": op,
            "left": _normalize_term(raw.get("left"), variables),
            "right": _normalize_term(raw.get("right"), variables),
        }
    if op in {"prefixof", "suffixof", "contains"}:
        return {
            "op": op,
            "needle": _normalize_term(raw.get("needle"), variables),
            "haystack": _normalize_term(raw.get("haystack"), variables),
        }
    if op == "length":
        relation = str(raw.get("relation", ""))
        if relation not in _RELATIONS:
            raise ValueError("unsupported length relation")
        return {
            "op": op,
            "value": _normalize_term(raw.get("value"), variables),
            "relation": relation,
            "length": _integer(
                raw.get("length"), "string length", 0, _MAX_STRING_BYTES),
        }
    if op == "indexof":
        relation = str(raw.get("relation", ""))
        if relation not in _RELATIONS:
            raise ValueError("unsupported indexof relation")
        return {
            "op": op,
            "haystack": _normalize_term(raw.get("haystack"), variables),
            "needle": _normalize_term(raw.get("needle"), variables),
            "start": _integer(
                raw.get("start", 0), "indexof start", 0, _MAX_STRING_BYTES),
            "relation": relation,
            "index": _integer(
                raw.get("index"), "indexof result", -1, _MAX_STRING_BYTES),
        }
    if op == "char_at":
        relation = str(raw.get("relation", ""))
        if relation not in {"equal", "distinct"}:
            raise ValueError("unsupported char_at relation")
        return {
            "op": op,
            "value": _normalize_term(raw.get("value"), variables),
            "index": _integer(
                raw.get("index"), "char_at index", 0, _MAX_STRING_BYTES),
            "relation": relation,
            "char": _integer(raw.get("char"), "char_at value", 0, 255),
        }
    if op == "to_int":
        relation = str(raw.get("relation", ""))
        if relation not in _RELATIONS:
            raise ValueError("unsupported string-to-int relation")
        return {
            "op": op,
            "value": _normalize_term(raw.get("value"), variables),
            "relation": relation,
            "integer": _integer(
                raw.get("integer"), "string-to-int value",
                -(1 << 64), (1 << 64) - 1),
            "signed": bool(raw.get("signed", False)),
        }
    if op == "decimal":
        return {
            "op": op,
            "value": _normalize_term(raw.get("value"), variables),
            "signed": bool(raw.get("signed", False)),
        }
    raise ValueError("unsupported string predicate")


def normalize_string_query(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("string query must be an object")
    if raw.get("schema") not in {
            STRING_QUERY_SCHEMA, *_LEGACY_STRING_QUERY_SCHEMAS}:
        raise ValueError(f"schema must be {STRING_QUERY_SCHEMA}")
    input_size = _integer(
        raw.get("input_size"), "input_size", 1, 128 * 1024 * 1024)
    variables_raw = raw.get("variables")
    if not isinstance(variables_raw, Sequence) or isinstance(
            variables_raw, (str, bytes)) or not 1 <= len(variables_raw) <= 64:
        raise ValueError("variables must be a bounded non-empty list")
    variables: list[dict[str, Any]] = []
    names: set[str] = set()
    occupied: set[int] = set()
    for raw_variable in variables_raw:
        if not isinstance(raw_variable, Mapping):
            raise ValueError("string variables must be objects")
        name = _query_name(raw_variable.get("name"), "variable name")
        if name in names:
            raise ValueError("string variable names must be unique")
        names.add(name)
        offset = _integer(
            raw_variable.get("offset"), "variable offset", 0, input_size - 1)
        capacity = _integer(
            raw_variable.get("capacity"), "variable capacity", 1,
            min(_MAX_STRING_BYTES, input_size - offset))
        nul_terminated = bool(raw_variable.get("nul_terminated", False))
        default_minimum = 0 if nul_terminated else capacity
        default_maximum = capacity - 1 if nul_terminated else capacity
        minimum = _integer(
            raw_variable.get("min_length", default_minimum),
            "minimum string length",
            0, capacity)
        maximum = _integer(
            raw_variable.get("max_length", default_maximum),
            "maximum string length",
            minimum, capacity)
        if nul_terminated and maximum >= capacity:
            raise ValueError(
                "NUL-terminated string length must leave terminator capacity")
        if not nul_terminated and (
                minimum != capacity or maximum != capacity):
            raise ValueError(
                "fixed byte strings must have capacity-sized length")
        span = set(range(offset, offset + capacity))
        if span & occupied:
            raise ValueError("string variable input spans must not overlap")
        occupied.update(span)
        variables.append({
            "name": name,
            "offset": offset,
            "capacity": capacity,
            "min_length": minimum,
            "max_length": maximum,
            "nul_terminated": nul_terminated,
        })
    constraints_raw = raw.get("constraints")
    if not isinstance(constraints_raw, Sequence) or isinstance(
            constraints_raw, (str, bytes)) or not 1 <= len(
                constraints_raw) <= 256:
        raise ValueError("constraints must be a bounded non-empty list")
    constraints = [
        _normalize_predicate(predicate, names)
        for predicate in constraints_raw
    ]
    byte_constraints_raw = raw.get("byte_constraints", ())
    if (
        not isinstance(byte_constraints_raw, Sequence) or
        isinstance(byte_constraints_raw, (str, bytes)) or
        len(byte_constraints_raw) > 4096
    ):
        raise ValueError("byte_constraints must be a bounded list")
    byte_constraints: list[dict[str, int | str]] = []
    for constraint in byte_constraints_raw:
        if not isinstance(constraint, Mapping):
            raise ValueError("byte constraints must be objects")
        offset = _integer(
            constraint.get("offset"), "byte constraint offset",
            0, input_size - 1)
        relation = str(constraint.get("relation", ""))
        if relation not in _BYTE_RELATIONS:
            raise ValueError("unsupported byte constraint relation")
        if offset not in occupied:
            raise ValueError(
                "byte constraint must reference a string variable span")
        byte_constraints.append({
            "offset": offset,
            "relation": relation,
            "value": _integer(
                constraint.get("value"), "byte constraint value", 0, 255),
        })
    return {
        "schema": STRING_QUERY_SCHEMA,
        "input_size": input_size,
        "variables": variables,
        "constraints": constraints,
        "byte_constraints": byte_constraints,
        "views": {
            "string": True,
            "byte_vector": True,
            "link": "exact-nul-v1",
        },
        "metadata": (
            dict(raw.get("metadata", {}))
            if isinstance(raw.get("metadata", {}), Mapping)
            else {}
        ),
    }


def normalize_string_constraint(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("string constraint must be an object")
    if raw.get("schema") != STRING_CONSTRAINT_SCHEMA:
        raise ValueError(f"schema must be {STRING_CONSTRAINT_SCHEMA}")
    op = str(raw.get("op", ""))
    if op not in {"memcmp", "strcmp", "strncmp"}:
        raise ValueError("unsupported string constraint op")
    token_hex = str(raw.get("token_hex", ""))
    if len(token_hex) > 8192 or len(token_hex) % 2:
        raise ValueError("token_hex must be bounded even-length hex")
    try:
        bytes.fromhex(token_hex)
    except ValueError as exc:
        raise ValueError("token_hex must be valid hex") from exc
    patches_raw = raw.get("patches", ())
    if not isinstance(patches_raw, Sequence) or isinstance(
            patches_raw, (str, bytes)) or len(patches_raw) > 4096:
        raise ValueError("patches must be a bounded list")
    patches: list[dict[str, int]] = []
    seen_offsets: set[int] = set()
    for patch in patches_raw:
        if not isinstance(patch, Mapping):
            raise ValueError("patch entries must be objects")
        offset = _integer(patch.get("offset"), "patch offset", 0, _MAX_OFFSET)
        value = _integer(patch.get("value"), "patch value", 0, 255)
        if offset in seen_offsets:
            raise ValueError("patch offsets must be unique")
        seen_offsets.add(offset)
        patches.append({"offset": offset, "value": value})
    return {
        "schema": STRING_CONSTRAINT_SCHEMA,
        "op": op,
        "site": _integer(raw.get("site", 0), "site", 0, (1 << 64) - 1),
        "result": _integer(raw.get("result", 0), "result", -(1 << 31),
                           (1 << 31) - 1),
        "taken_equal": bool(raw.get("taken_equal", False)),
        "symbolic_side": str(raw.get("symbolic_side", ""))[:16],
        "token_hex": token_hex.lower(),
        "nul_terminated": bool(raw.get("nul_terminated", False)),
        "complete": bool(raw.get("complete", False)),
        "patches": patches,
    }


def normalize_string_operation(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("string operation must be an object")
    if raw.get("schema") != STRING_OPERATION_SCHEMA:
        raise ValueError(f"schema must be {STRING_OPERATION_SCHEMA}")
    op = str(raw.get("op", ""))
    if op not in {
            "strlen", "strchr", "strstr", "atoi", "strtol10", "strtoul10"}:
        raise ValueError("unsupported string operation")
    role = str(raw.get("symbolic_role", ""))
    allowed_roles = {
        "strlen": {"value"},
        "strchr": {"haystack"},
        "strstr": {"haystack", "needle"},
        "atoi": {"value"},
        "strtol10": {"value"},
        "strtoul10": {"value"},
    }
    if role not in allowed_roles[op]:
        raise ValueError("invalid symbolic role for string operation")
    input_bytes_raw = raw.get("input_bytes")
    if (
        not isinstance(input_bytes_raw, Sequence) or
        isinstance(input_bytes_raw, (str, bytes)) or
        not 1 <= len(input_bytes_raw) <= _MAX_STRING_BYTES
    ):
        raise ValueError("input_bytes must be a bounded non-empty list")
    input_bytes: list[dict[str, int]] = []
    for item in input_bytes_raw:
        if not isinstance(item, Mapping):
            raise ValueError("input byte entries must be objects")
        input_bytes.append({
            "offset": _integer(
                item.get("offset"), "input byte offset", 0, _MAX_OFFSET),
            "value": _integer(
                item.get("value"), "input byte value", 0, 255),
        })
    offsets = [item["offset"] for item in input_bytes]
    if offsets != list(range(offsets[0], offsets[0] + len(offsets))):
        raise ValueError("operation input offsets must be contiguous")
    values = bytes(item["value"] for item in input_bytes)
    if values[-1] != 0 or b"\0" in values[:-1]:
        raise ValueError("operation input span must end at its first NUL")
    observed_length = _integer(
        raw.get("observed_length"), "observed string length",
        0, _MAX_STRING_BYTES - 1)
    if observed_length != len(input_bytes) - 1:
        raise ValueError("observed length must match the NUL-terminated span")
    observed_index = _integer(
        raw.get("observed_index", observed_length),
        "observed operation index", -1, _MAX_STRING_BYTES - 1)
    constant_hex = _bounded_hex(
        raw.get("constant_hex", ""), "operation constant",
        _MAX_STRING_BYTES)
    constant = bytes.fromhex(constant_hex)
    if op == "strchr" and (len(constant) != 1 or constant == b"\0"):
        raise ValueError("strchr constant must be one non-NUL byte")
    if op in {"strlen", "atoi", "strtol10", "strtoul10"} and constant:
        raise ValueError(f"{op} must not carry a constant operand")
    observed_minimum = 0 if op == "strtoul10" else -(1 << 63)
    observed_maximum = (
        (1 << 64) - 1 if op == "strtoul10" else (1 << 63) - 1)
    observed_value = _integer(
        raw.get("observed_value", 0), "observed conversion value",
        observed_minimum, observed_maximum)
    default_bits = (
        32 if op == "atoi" else
        64 if op in {"strtol10", "strtoul10"} else 0
    )
    integer_bits = _integer(
        raw.get("integer_bits", default_bits),
        "conversion integer width", 0, 64)
    if (
        (op == "atoi" and integer_bits != 32) or
        (op == "strtol10" and integer_bits not in {32, 64}) or
        (op == "strtoul10" and integer_bits not in {32, 64}) or
        (op not in {"atoi", "strtol10", "strtoul10"} and integer_bits != 0)
    ):
        raise ValueError("invalid conversion integer width")
    if op == "atoi":
        digits = values[:-1]
        if (
            not 1 <= len(digits) <= 10 or
            any(value < ord("0") or value > ord("9") for value in digits) or
            int(digits.decode("ascii")) != observed_value
        ):
            raise ValueError("atoi artifact must describe exact decimal digits")
    if op == "strtol10":
        text = values[:-1]
        negative = text.startswith(b"-")
        digits = text[1:] if negative else text
        max_digits = 19 if integer_bits == 64 else 10
        if (
            not 1 <= len(digits) <= max_digits or
            any(value < ord("0") or value > ord("9") for value in digits)
        ):
            raise ValueError("strtol10 artifact must describe signed decimal")
        converted = int(text.decode("ascii"))
        if (
            converted != observed_value or
            not -(1 << 63) <= converted <= (1 << 63) - 1
        ):
            raise ValueError("strtol10 artifact value is inconsistent")
    if op == "strtoul10":
        digits = values[:-1]
        max_digits = 20 if integer_bits == 64 else 10
        if (
            not 1 <= len(digits) <= max_digits or
            any(value < ord("0") or value > ord("9") for value in digits)
        ):
            raise ValueError(
                "strtoul10 artifact must describe unsigned decimal")
        converted = int(digits.decode("ascii"))
        if (
            converted != observed_value or
            converted > (1 << integer_bits) - 1
        ):
            raise ValueError("strtoul10 artifact value is inconsistent")
    return {
        "schema": STRING_OPERATION_SCHEMA,
        "op": op,
        "site": _integer(raw.get("site", 0), "site", 0, (1 << 64) - 1),
        "symbolic_role": role,
        "constant_hex": constant_hex,
        "observed_length": observed_length,
        "observed_index": observed_index,
        "observed_value": observed_value,
        "integer_bits": integer_bits,
        "complete": bool(raw.get("complete", False)),
        "input_bytes": input_bytes,
        "patches": [],
    }


def normalize_string_record(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping) and raw.get("schema") == STRING_OPERATION_SCHEMA:
        return normalize_string_operation(raw)
    return normalize_string_constraint(raw)


def load_string_constraints(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open(encoding="ascii", errors="ignore") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            records.append(normalize_string_record(json.loads(line)))
    return records


def string_query_from_constraint(
    raw: Mapping[str, Any],
    input_size: int,
) -> dict[str, Any] | None:
    record = normalize_string_record(raw)
    if record["schema"] == STRING_OPERATION_SCHEMA:
        if not record["complete"]:
            return None
        input_bytes = record["input_bytes"]
        base = int(input_bytes[0]["offset"])
        capacity = len(input_bytes)
        if base + capacity > input_size:
            return None
        variable = {"kind": "var", "name": "input"}
        literal = {
            "kind": "literal",
            "value_hex": str(record["constant_hex"]),
        }
        if record["op"] == "strlen":
            constraint = {
                "op": "length",
                "value": variable,
                "relation": "ne",
                "length": int(record["observed_length"]),
            }
        elif record["op"] == "atoi":
            maximum = (1 << (int(record["integer_bits"]) - 1)) - 1
            constraint = [
                {"op": "decimal", "value": variable, "signed": False},
                {
                    "op": "to_int", "value": variable, "relation": "ge",
                    "integer": 0, "signed": False,
                },
                {
                    "op": "to_int", "value": variable, "relation": "le",
                    "integer": maximum, "signed": False,
                },
                {
                    "op": "to_int",
                    "value": variable,
                    "relation": "ne",
                    "integer": int(record["observed_value"]),
                    "signed": False,
                },
            ]
        elif record["op"] == "strtol10":
            bits = int(record["integer_bits"])
            constraint = [
                {"op": "decimal", "value": variable, "signed": True},
                {
                    "op": "to_int", "value": variable, "relation": "ge",
                    "integer": -(1 << (bits - 1)), "signed": True,
                },
                {
                    "op": "to_int", "value": variable, "relation": "le",
                    "integer": (1 << (bits - 1)) - 1, "signed": True,
                },
                {
                    "op": "to_int",
                    "value": variable,
                    "relation": "ne",
                    "integer": int(record["observed_value"]),
                    "signed": True,
                },
            ]
        elif record["op"] == "strtoul10":
            bits = int(record["integer_bits"])
            constraint = [
                {"op": "decimal", "value": variable, "signed": False},
                {
                    "op": "to_int", "value": variable, "relation": "ge",
                    "integer": 0, "signed": False,
                },
                {
                    "op": "to_int", "value": variable, "relation": "le",
                    "integer": (1 << bits) - 1, "signed": False,
                },
                {
                    "op": "to_int",
                    "value": variable,
                    "relation": "ne",
                    "integer": int(record["observed_value"]),
                    "signed": False,
                },
            ]
        else:
            constraint = {
                "op": "indexof",
                "haystack": (
                    variable
                    if record["symbolic_role"] == "haystack"
                    else literal
                ),
                "needle": (
                    literal
                    if record["symbolic_role"] == "haystack"
                    else variable
                ),
                "start": 0,
                "relation": "ne",
                "index": int(record["observed_index"]),
            }
        return normalize_string_query({
            "schema": STRING_QUERY_SCHEMA,
            "input_size": input_size,
            "variables": [{
                "name": "input",
                "offset": base,
                "capacity": capacity,
                "min_length": 0,
                "max_length": capacity - 1,
                "nul_terminated": True,
            }],
            "constraints": (
                constraint if isinstance(constraint, list) else [constraint]),
            "metadata": {
                "source_schema": STRING_OPERATION_SCHEMA,
                "source_op": record["op"],
                "symbolic_role": record["symbolic_role"],
                "site": record["site"],
                "observed_index": record["observed_index"],
                "observed_value": record["observed_value"],
                "integer_bits": record["integer_bits"],
            },
        })
    patches = record["patches"]
    token = bytes.fromhex(record["token_hex"])
    total = len(token) + (1 if record["nul_terminated"] else 0)
    if not record["complete"] or total == 0 or len(patches) != total:
        return None
    offsets = sorted(int(patch["offset"]) for patch in patches)
    base = offsets[0]
    if offsets != list(range(base, base + total)):
        return None
    if base + total > input_size:
        return None
    patch_values = {
        int(patch["offset"]): int(patch["value"])
        for patch in patches
    }
    expected = token + (b"\0" if record["nul_terminated"] else b"")
    if bytes(patch_values[base + index] for index in range(total)) != expected:
        return None

    c_string = bool(record["nul_terminated"])
    maximum = len(token) if c_string else total
    query = {
        "schema": STRING_QUERY_SCHEMA,
        "input_size": input_size,
        "variables": [{
            "name": "input",
            "offset": base,
            "capacity": total,
            "min_length": 0 if c_string else total,
            "max_length": maximum,
            "nul_terminated": c_string,
        }],
        "constraints": [{
            "op": "distinct" if record["taken_equal"] else "equal",
            "left": {"kind": "var", "name": "input"},
            "right": {"kind": "literal", "value_hex": token.hex()},
        }],
        "metadata": {
            "source_schema": STRING_CONSTRAINT_SCHEMA,
            "source_op": record["op"],
            "site": record["site"],
        },
    }
    return normalize_string_query(query)


def _smt_symbol(variable: Mapping[str, Any]) -> str:
    return (
        f"|symcc!str!{int(variable['offset'])}!"
        f"{int(variable['capacity'])}!"
        f"{1 if variable['nul_terminated'] else 0}|"
    )


def _smt_byte_symbol(offset: int) -> str:
    return f"|{offset}|"


def _smt_string_literal(value: bytes) -> str:
    if not value:
        return '""'
    items = [f"(str.from_code {byte})" for byte in value]
    return items[0] if len(items) == 1 else f"(str.++ {' '.join(items)})"


def _lower_term(
    term: Mapping[str, Any],
    variables: Mapping[str, Mapping[str, Any]],
) -> str:
    kind = str(term["kind"])
    if kind == "var":
        return _smt_symbol(variables[str(term["name"])])
    if kind == "literal":
        return _smt_string_literal(bytes.fromhex(str(term["value_hex"])))
    if kind == "concat":
        items = [
            _lower_term(item, variables)
            for item in term["items"]
        ]
        return items[0] if len(items) == 1 else f"(str.++ {' '.join(items)})"
    if kind == "substr":
        value = _lower_term(term["value"], variables)
        return f"(str.substr {value} {int(term['offset'])} {int(term['length'])})"
    raise ValueError("unsupported normalized string term")


def _lower_relation(left: str, relation: str, right: int) -> str:
    right_term = str(right) if right >= 0 else f"(- {-right})"
    if relation == "eq":
        return f"(= {left} {right_term})"
    if relation == "ne":
        return f"(not (= {left} {right_term}))"
    operator = {"lt": "<", "le": "<=", "gt": ">", "ge": ">="}[relation]
    return f"({operator} {left} {right_term})"


def lower_string_query(raw: Mapping[str, Any]) -> str:
    query = normalize_string_query(raw)
    variables = {
        str(variable["name"]): variable
        for variable in query["variables"]
    }
    lines = [
        "(set-logic ALL)",
        "(set-option :produce-models true)",
    ]
    for variable in query["variables"]:
        symbol = _smt_symbol(variable)
        lines.append(f"(declare-fun {symbol} () String)")
        lines.append(
            f"(assert (>= (str.len {symbol}) "
            f"{int(variable['min_length'])}))")
        lines.append(
            f"(assert (<= (str.len {symbol}) "
            f"{int(variable['max_length'])}))")
        offset = int(variable["offset"])
        capacity = int(variable["capacity"])
        for index in range(capacity):
            byte_symbol = _smt_byte_symbol(offset + index)
            lines.append(f"(declare-fun {byte_symbol} () (_ BitVec 8))")
            char = f"(str.at {symbol} {index})"
            byte_char = f"(str.from_code (bv2nat {byte_symbol}))"
            if variable["nul_terminated"]:
                lines.append(
                    f"(assert (=> (< {index} (str.len {symbol})) "
                    f"(and (= {char} {byte_char}) "
                    f"(not (= {byte_symbol} #x00)))))")
                lines.append(
                    f"(assert (=> (= {index} (str.len {symbol})) "
                    f"(= {byte_symbol} #x00)))")
            else:
                lines.append(f"(assert (= {char} {byte_char}))")

    for predicate in query["constraints"]:
        op = str(predicate["op"])
        if op in {"equal", "distinct"}:
            left = _lower_term(predicate["left"], variables)
            right = _lower_term(predicate["right"], variables)
            body = f"(= {left} {right})"
            if op == "distinct":
                body = f"(not {body})"
        elif op in {"prefixof", "suffixof", "contains"}:
            needle = _lower_term(predicate["needle"], variables)
            haystack = _lower_term(predicate["haystack"], variables)
            if op == "contains":
                body = f"(str.contains {haystack} {needle})"
            else:
                smt_op = {
                    "prefixof": "str.prefixof",
                    "suffixof": "str.suffixof",
                }[op]
                body = f"({smt_op} {needle} {haystack})"
        elif op == "length":
            value = _lower_term(predicate["value"], variables)
            body = _lower_relation(
                f"(str.len {value})",
                str(predicate["relation"]),
                int(predicate["length"]),
            )
        elif op == "indexof":
            haystack = _lower_term(predicate["haystack"], variables)
            needle = _lower_term(predicate["needle"], variables)
            index = (
                f"(str.indexof {haystack} {needle} "
                f"{int(predicate['start'])})"
            )
            body = _lower_relation(
                index, str(predicate["relation"]), int(predicate["index"]))
        elif op == "char_at":
            value = _lower_term(predicate["value"], variables)
            char = _smt_string_literal(bytes([int(predicate["char"])]))
            body = f"(= (str.at {value} {int(predicate['index'])}) {char})"
            if predicate["relation"] == "distinct":
                body = f"(not {body})"
        elif op == "to_int":
            value = _lower_term(predicate["value"], variables)
            converted = f"(str.to_int {value})"
            if predicate["signed"]:
                converted = (
                    f"(ite (str.prefixof (str.from_code 45) {value}) "
                    f"(- (str.to_int (str.substr {value} 1 "
                    f"{_MAX_STRING_BYTES}))) {converted})"
                )
            body = _lower_relation(
                converted,
                str(predicate["relation"]),
                int(predicate["integer"]),
            )
        elif op == "decimal":
            value = _lower_term(predicate["value"], variables)
            digits = (
                "(re.+ (re.range (str.from_code 48) "
                "(str.from_code 57)))"
            )
            expression = digits
            if predicate["signed"]:
                expression = (
                    f"(re.union {digits} "
                    f"(re.++ (str.to_re (str.from_code 45)) {digits}))"
                )
            body = f"(str.in_re {value} {expression})"
        else:
            raise ValueError("unsupported normalized string predicate")
        lines.append(f"(assert {body})")
    for constraint in query["byte_constraints"]:
        symbol = _smt_byte_symbol(int(constraint["offset"]))
        value = f"#x{int(constraint['value']):02x}"
        relation = str(constraint["relation"])
        if relation == "eq":
            body = f"(= {symbol} {value})"
        elif relation == "ne":
            body = f"(not (= {symbol} {value}))"
        else:
            operator = {
                "ult": "bvult",
                "ule": "bvule",
                "ugt": "bvugt",
                "uge": "bvuge",
            }[relation]
            body = f"({operator} {symbol} {value})"
        lines.append(f"(assert {body})")
    return "\n".join(lines) + "\n"


def _extract_query_strings(
    query: Mapping[str, Any],
    candidate: bytes,
) -> dict[str, bytes] | None:
    values: dict[str, bytes] = {}
    for variable in query["variables"]:
        offset = int(variable["offset"])
        capacity = int(variable["capacity"])
        raw = candidate[offset:offset + capacity]
        if len(raw) != capacity:
            return None
        if variable["nul_terminated"]:
            terminator = raw.find(b"\0")
            if terminator < 0:
                return None
            value = raw[:terminator]
        else:
            value = raw
        if not (
            int(variable["min_length"])
            <= len(value)
            <= int(variable["max_length"])
        ):
            return None
        values[str(variable["name"])] = value
    return values


def _evaluate_term(
    term: Mapping[str, Any],
    variables: Mapping[str, bytes],
) -> bytes:
    kind = str(term["kind"])
    if kind == "var":
        return variables[str(term["name"])]
    if kind == "literal":
        return bytes.fromhex(str(term["value_hex"]))
    if kind == "concat":
        return b"".join(
            _evaluate_term(item, variables) for item in term["items"])
    if kind == "substr":
        value = _evaluate_term(term["value"], variables)
        offset = int(term["offset"])
        return value[offset:offset + int(term["length"])]
    raise ValueError("unsupported normalized string term")


def _compare_integer(left: int, relation: str, right: int) -> bool:
    return {
        "eq": left == right,
        "ne": left != right,
        "lt": left < right,
        "le": left <= right,
        "gt": left > right,
        "ge": left >= right,
    }[relation]


def candidate_satisfies_string_query(
    raw: Mapping[str, Any],
    candidate: bytes,
) -> bool:
    query = normalize_string_query(raw)
    if len(candidate) != int(query["input_size"]):
        return False
    variables = _extract_query_strings(query, candidate)
    if variables is None:
        return False
    for predicate in query["constraints"]:
        op = str(predicate["op"])
        if op in {"equal", "distinct"}:
            result = (
                _evaluate_term(predicate["left"], variables)
                == _evaluate_term(predicate["right"], variables)
            )
            if result != (op == "equal"):
                return False
        elif op in {"prefixof", "suffixof", "contains"}:
            needle = _evaluate_term(predicate["needle"], variables)
            haystack = _evaluate_term(predicate["haystack"], variables)
            result = {
                "prefixof": haystack.startswith(needle),
                "suffixof": haystack.endswith(needle),
                "contains": needle in haystack,
            }[op]
            if not result:
                return False
        elif op == "length":
            value = _evaluate_term(predicate["value"], variables)
            if not _compare_integer(
                    len(value), str(predicate["relation"]),
                    int(predicate["length"])):
                return False
        elif op == "indexof":
            haystack = _evaluate_term(predicate["haystack"], variables)
            needle = _evaluate_term(predicate["needle"], variables)
            found = haystack.find(needle, int(predicate["start"]))
            if not _compare_integer(
                    found, str(predicate["relation"]),
                    int(predicate["index"])):
                return False
        elif op == "char_at":
            value = _evaluate_term(predicate["value"], variables)
            index = int(predicate["index"])
            found = value[index] if index < len(value) else -1
            equal = found == int(predicate["char"])
            if equal != (predicate["relation"] == "equal"):
                return False
        elif op == "to_int":
            value = _evaluate_term(predicate["value"], variables)
            negative = bool(predicate["signed"] and value.startswith(b"-"))
            digits = value[1:] if negative else value
            converted = (
                int(value.decode("ascii"))
                if digits and all(
                    ord("0") <= byte <= ord("9") for byte in digits)
                else -1
            )
            if not _compare_integer(
                    converted, str(predicate["relation"]),
                    int(predicate["integer"])):
                return False
        elif op == "decimal":
            value = _evaluate_term(predicate["value"], variables)
            if predicate["signed"] and value.startswith(b"-"):
                value = value[1:]
            if not value or any(
                    byte < ord("0") or byte > ord("9") for byte in value):
                return False
        else:
            return False
    for constraint in query["byte_constraints"]:
        left = candidate[int(constraint["offset"])]
        right = int(constraint["value"])
        relation = str(constraint["relation"])
        if not {
            "eq": left == right,
            "ne": left != right,
            "ult": left < right,
            "ule": left <= right,
            "ugt": left > right,
            "uge": left >= right,
        }[relation]:
            return False
    return True


class StringSolverBackend(Protocol):
    name: str

    def solve(
        self,
        query: Mapping[str, Any],
        timeout_ms: int,
    ) -> Mapping[str, Any]:
        ...


def _default_string_solver() -> list[str]:
    configured = os.environ.get("SYMCC_STRING_SOLVER", "").strip()
    if configured:
        return shlex.split(configured)
    discovered = shutil.which("symcc-query-solver")
    if discovered:
        return [discovered]
    root = Path(__file__).resolve().parents[1]
    candidates = [
        root / "build" / "SymCCRuntime-prefix" / "src"
        / "SymCCRuntime-build" / "src" / "backends" / "qsym"
        / "symcc-query-solver",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]
    raise FileNotFoundError(
        "symcc-query-solver was not found; set SYMCC_STRING_SOLVER")


class SymccJsonStringBackend:
    """Backend-neutral string-query API using SymCC's JSON solver protocol."""

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        name: str = "z3-string",
    ):
        self.name = str(name)[:64] or "z3-string"
        self.command = tuple(command or _default_string_solver())
        if not self.command:
            raise ValueError("string solver command must not be empty")

    def solve(
        self,
        query: Mapping[str, Any],
        timeout_ms: int,
    ) -> Mapping[str, Any]:
        normalized = normalize_string_query(query)
        smt2 = lower_string_query(normalized)
        timeout_ms = max(1, min(int(timeout_ms), 3600000))
        path = ""
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="ascii", suffix=".smt2",
                    delete=False) as output:
                output.write(smt2)
                path = output.name
            completed = subprocess.run(
                [*self.command, "--generic", path, str(timeout_ms)],
                check=False,
                capture_output=True,
                text=True,
                timeout=max(1.0, timeout_ms / 1000.0 + 1.0),
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "unknown",
                "assignments": {},
                "solver": self.name,
                "reason": "string solver process timeout",
            }
        finally:
            if path:
                Path(path).unlink(missing_ok=True)
        if completed.returncode not in {0, 2}:
            return {
                "status": "error",
                "assignments": {},
                "solver": self.name,
                "reason": completed.stderr[-512:],
            }
        try:
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return {
                "status": "error",
                "assignments": {},
                "solver": self.name,
                "reason": "invalid JSON string solver response",
            }
        assignments_raw = result.get("assignments", {})
        assignments: dict[str, int] = {}
        if isinstance(assignments_raw, Mapping):
            for raw_offset, raw_value in assignments_raw.items():
                try:
                    offset = int(raw_offset)
                    value = int(raw_value)
                except (TypeError, ValueError):
                    continue
                if 0 <= offset < int(normalized["input_size"]) and 0 <= value <= 255:
                    assignments[str(offset)] = value
        return {
            "status": str(result.get("status", "error")),
            "assignments": assignments,
            "solver": str(result.get("solver", self.name)),
            "elapsed_us": int(result.get("elapsed_us", 0)),
            "reason": str(result.get("reason", ""))[:512],
        }


class SmtLibCliStringBackend:
    """SMT-LIB CLI adapter using linked byte values as the model protocol."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        name: str = "smtlib-string",
    ):
        self.name = str(name)[:64] or "smtlib-string"
        self.command = tuple(str(item) for item in command)
        if not self.command or any(not item for item in self.command):
            raise ValueError("SMT-LIB backend command must not be empty")

    def solve(
        self,
        query: Mapping[str, Any],
        timeout_ms: int,
    ) -> Mapping[str, Any]:
        normalized = normalize_string_query(query)
        offsets = sorted({
            int(variable["offset"]) + index
            for variable in normalized["variables"]
            for index in range(int(variable["capacity"]))
        })
        values = " ".join(_smt_byte_symbol(offset) for offset in offsets)
        smt2 = (
            lower_string_query(normalized) +
            "(check-sat)\n" +
            f"(get-value ({values}))\n"
        )
        timeout_ms = max(1, min(int(timeout_ms), 3600000))
        path = ""
        started = time.monotonic_ns()
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="ascii", suffix=".smt2",
                    delete=False) as output:
                output.write(smt2)
                path = output.name
            command = [
                path if item == "{query}" else
                str(timeout_ms) if item == "{timeout_ms}" else item
                for item in self.command
            ]
            if "{query}" not in self.command:
                command.append(path)
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=max(1.0, timeout_ms / 1000.0 + 1.0),
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "unknown",
                "assignments": {},
                "solver": self.name,
                "reason": "SMT-LIB solver process timeout",
            }
        except OSError as error:
            return {
                "status": "error",
                "assignments": {},
                "solver": self.name,
                "reason": str(error)[:512],
            }
        finally:
            if path:
                Path(path).unlink(missing_ok=True)
        elapsed_us = (time.monotonic_ns() - started) // 1000
        if completed.returncode not in {0, 10, 20}:
            diagnostic = (completed.stderr + "\n" + completed.stdout).strip()
            return {
                "status": "error",
                "assignments": {},
                "solver": self.name,
                "elapsed_us": elapsed_us,
                "reason": diagnostic[-512:],
            }
        status_match = re.search(
            r"(?m)^(sat|unsat|unknown)\s*$", completed.stdout)
        if status_match is None:
            return {
                "status": "error",
                "assignments": {},
                "solver": self.name,
                "elapsed_us": elapsed_us,
                "reason": "missing SMT-LIB status",
            }
        status = status_match.group(1)
        assignments: dict[str, int] = {}
        if status == "sat":
            pattern = re.compile(
                r"\(\s*\|?(\d+)\|?\s+"
                r"(#x[0-9a-fA-F]{2}|#b[01]{8}|"
                r"\(_\s+bv(\d+)\s+8\))\s*\)")
            for match in pattern.finditer(completed.stdout):
                token = match.group(2)
                if token.startswith("#x"):
                    value = int(token[2:], 16)
                elif token.startswith("#b"):
                    value = int(token[2:], 2)
                else:
                    value = int(match.group(3))
                offset = int(match.group(1))
                if offset in offsets and 0 <= value <= 255:
                    assignments[str(offset)] = value
        return {
            "status": status,
            "assignments": assignments,
            "solver": self.name,
            "elapsed_us": elapsed_us,
            "reason": "",
        }


_STRING_BACKEND_POLICY_SCHEMA = "symcc-string-backend-policy-v1"


def _string_query_context(query: Mapping[str, Any]) -> str:
    """Build a stable, deliberately coarse solver-selection context."""
    normalized = normalize_string_query(query)
    operations = sorted({
        str(constraint.get("op", "unknown"))
        for constraint in normalized["constraints"]
    })
    capacity = sum(
        int(variable["capacity"]) for variable in normalized["variables"])
    capacity_bucket = (
        "tiny" if capacity <= 8 else
        "small" if capacity <= 32 else
        "medium" if capacity <= 128 else
        "large"
    )
    variable_bucket = (
        "one" if len(normalized["variables"]) == 1 else "multiple")
    return (
        f"ops={','.join(operations) or 'none'}|capacity={capacity_bucket}|"
        f"variables={variable_bucket}|"
        f"bv={int(bool(normalized.get('byte_constraints')))}"
    )


class _ContextualStringBackendPolicy:
    """Cost-aware contextual backend selection with bounded persistence."""

    def __init__(
        self,
        backend_names: Sequence[str],
        configuration: Mapping[str, Any],
    ):
        self.backend_names = tuple(str(name) for name in backend_names)
        if len(set(self.backend_names)) != len(self.backend_names):
            raise ValueError("string backend names must be unique")
        try:
            maximum = int(configuration.get(
                "max_backends", len(self.backend_names)))
            warmup = int(configuration.get("warmup", 1))
            explore_every = int(configuration.get("explore_every", 16))
            context_limit = int(configuration.get("context_limit", 128))
            exploration = float(configuration.get("exploration", 0.35))
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("invalid string selection configuration") from error
        if not 1 <= maximum <= len(self.backend_names):
            raise ValueError("selection max_backends must fit the portfolio")
        if not 0 <= warmup <= 100:
            raise ValueError("selection warmup must be in [0, 100]")
        if not 1 <= explore_every <= 1000000:
            raise ValueError(
                "selection explore_every must be in [1, 1000000]")
        if not 1 <= context_limit <= 1024:
            raise ValueError("selection context_limit must be in [1, 1024]")
        if not math.isfinite(exploration) or not 0.0 <= exploration <= 10.0:
            raise ValueError("selection exploration must be in [0, 10]")
        raw_path = configuration.get("state_path")
        if raw_path is not None and (
                not isinstance(raw_path, str) or not raw_path.strip()
                or len(raw_path) > 4096):
            raise ValueError("selection state_path must be a bounded path")
        self.max_backends = maximum
        self.warmup = warmup
        self.explore_every = explore_every
        self.context_limit = context_limit
        self.exploration = exploration
        self.state_path = (
            os.path.abspath(os.path.expanduser(raw_path))
            if isinstance(raw_path, str) else None
        )
        self._mutex = threading.Lock()
        self._metrics = {
            "solver_backends_selected": 0,
            "solver_backends_skipped": 0,
            "solver_policy_updates": 0,
            "solver_policy_explorations": 0,
        }
        self._state = self._empty_state()
        self._load()

    @staticmethod
    def _empty_arm() -> dict[str, int]:
        return {"pulls": 0, "verified": 0, "elapsed_us": 0}

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema": _STRING_BACKEND_POLICY_SCHEMA,
            "backend_names": list(self.backend_names),
            "round": 0,
            "updates": 0,
            "global": {
                name: self._empty_arm() for name in self.backend_names
            },
            "contexts": {},
        }

    @staticmethod
    def _normalize_arm(raw: Any) -> dict[str, int]:
        if not isinstance(raw, Mapping):
            return _ContextualStringBackendPolicy._empty_arm()
        try:
            pulls = max(0, int(raw.get("pulls", 0)))
            verified = max(0, min(pulls, int(raw.get("verified", 0))))
            elapsed_us = max(0, int(raw.get("elapsed_us", 0)))
        except (TypeError, ValueError, OverflowError):
            return _ContextualStringBackendPolicy._empty_arm()
        return {
            "pulls": pulls,
            "verified": verified,
            "elapsed_us": elapsed_us,
        }

    def _normalized_state(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, Mapping) or raw.get(
                "schema") != _STRING_BACKEND_POLICY_SCHEMA:
            return None
        state = self._empty_state()
        try:
            state["round"] = max(0, int(raw.get("round", 0)))
            state["updates"] = max(0, int(raw.get("updates", 0)))
        except (TypeError, ValueError, OverflowError):
            return None
        global_raw = raw.get("global", {})
        if isinstance(global_raw, Mapping):
            for name in self.backend_names:
                state["global"][name] = self._normalize_arm(
                    global_raw.get(name))
        contexts_raw = raw.get("contexts", {})
        if isinstance(contexts_raw, Mapping):
            for context, context_raw in list(
                    contexts_raw.items())[-self.context_limit:]:
                if not isinstance(context, str) or len(context) > 512:
                    continue
                if not isinstance(context_raw, Mapping):
                    continue
                arms_raw = context_raw.get("arms", {})
                if not isinstance(arms_raw, Mapping):
                    continue
                try:
                    last_round = max(
                        0, int(context_raw.get("last_round", 0)))
                except (TypeError, ValueError, OverflowError):
                    last_round = 0
                state["contexts"][context] = {
                    "last_round": last_round,
                    "arms": {
                        name: self._normalize_arm(arms_raw.get(name))
                        for name in self.backend_names
                    },
                }
        return state

    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as stream:
                loaded = self._normalized_state(json.load(stream))
        except (OSError, ValueError, TypeError):
            return
        if loaded is not None:
            self._state = loaded

    def _save(self) -> None:
        if not self.state_path:
            return
        directory = os.path.dirname(self.state_path)
        try:
            os.makedirs(directory, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".string-policy-", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(
                        descriptor, "w", encoding="ascii") as stream:
                    json.dump(
                        self._state, stream, sort_keys=True,
                        separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.state_path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError:
            return

    @contextmanager
    def _locked_state(self) -> Iterable[None]:
        if not self.state_path:
            yield
            return
        lock_path = self.state_path + ".lock"
        try:
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            lock = open(lock_path, "a", encoding="ascii")
        except OSError:
            yield
            return
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except OSError:
            lock.close()
            yield
            return
        try:
            self._load()
            yield
            self._save()
        finally:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()

    def _context_state(self, context: str) -> dict[str, Any]:
        contexts = self._state["contexts"]
        current = contexts.get(context)
        if current is None:
            if len(contexts) >= self.context_limit:
                oldest = min(
                    contexts,
                    key=lambda key: (
                        int(contexts[key].get("last_round", 0)), key),
                )
                del contexts[oldest]
            current = {
                "last_round": int(self._state["round"]),
                "arms": {
                    name: self._empty_arm() for name in self.backend_names
                },
            }
            contexts[context] = current
        return current

    def _score(
        self,
        name: str,
        context_arm: Mapping[str, int],
        timeout_ms: int,
    ) -> float:
        global_arm = self._state["global"][name]
        context_pulls = int(context_arm["pulls"])
        global_pulls = int(global_arm["pulls"])
        effective_pulls = context_pulls + 0.25 * global_pulls
        effective_verified = (
            int(context_arm["verified"]) +
            0.25 * int(global_arm["verified"])
        )
        quality = (effective_verified + 1.0) / (effective_pulls + 2.0)
        elapsed_us = (
            int(context_arm["elapsed_us"]) +
            0.25 * int(global_arm["elapsed_us"])
        )
        mean_cost = elapsed_us / max(1.0, effective_pulls)
        cost_factor = 1.0 + mean_cost / max(1000.0, timeout_ms * 1000.0)
        exploration = math.sqrt(
            math.log1p(max(1, int(self._state["round"]))) /
            (1.0 + effective_pulls)
        )
        return quality / cost_factor + self.exploration * exploration

    def select(self, query: Mapping[str, Any], timeout_ms: int) -> tuple[str, ...]:
        context = _string_query_context(query)
        with self._mutex:
            with self._locked_state():
                self._state["round"] = int(self._state["round"]) + 1
                context_state = self._context_state(context)
                context_state["last_round"] = int(self._state["round"])
                chosen: list[str] = []
                cold = sorted(
                    (
                        int(self._state["global"][name]["pulls"]),
                        index,
                        name,
                    )
                        for index, name in enumerate(self.backend_names)
                        if int(self._state["global"][name]["pulls"]) < self.warmup
                )
                for _, _, name in cold:
                    if len(chosen) >= self.max_backends:
                        break
                    chosen.append(name)
                explore = (
                    int(self._state["round"]) % self.explore_every == 0)
                if explore and len(chosen) < self.max_backends:
                    least_used = min(
                        self.backend_names,
                        key=lambda name: (
                            int(context_state["arms"][name]["pulls"]),
                            int(self._state["global"][name]["pulls"]),
                            self.backend_names.index(name),
                        ),
                    )
                    if least_used not in chosen:
                        chosen.append(least_used)
                        self._metrics["solver_policy_explorations"] += 1
                ranked = sorted(
                    self.backend_names,
                    key=lambda name: (
                        -self._score(
                            name, context_state["arms"][name], timeout_ms),
                        self.backend_names.index(name),
                    ),
                )
                for name in ranked:
                    if len(chosen) >= self.max_backends:
                        break
                    if name not in chosen:
                        chosen.append(name)
                self._metrics["solver_backends_selected"] += len(chosen)
                self._metrics["solver_backends_skipped"] += (
                    len(self.backend_names) - len(chosen))
                return tuple(chosen)

    def observe(
        self,
        context: str,
        feedback: Sequence[tuple[str, bool, int]],
    ) -> None:
        if not feedback:
            return
        with self._mutex:
            with self._locked_state():
                context_state = self._context_state(context)
                context_state["last_round"] = int(self._state["round"])
                for name, verified, elapsed_us in feedback:
                    if name not in self._state["global"]:
                        continue
                    for arm in (
                            self._state["global"][name],
                            context_state["arms"][name]):
                        arm["pulls"] = int(arm["pulls"]) + 1
                        arm["verified"] = (
                            int(arm["verified"]) + int(bool(verified)))
                        arm["elapsed_us"] = (
                            int(arm["elapsed_us"]) +
                            max(0, int(elapsed_us)))
                    self._state["updates"] = (
                        int(self._state["updates"]) + 1)
                    self._metrics["solver_policy_updates"] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._mutex:
            return json.loads(json.dumps(self._state))

    def drain_metrics(self) -> dict[str, int]:
        with self._mutex:
            metrics = dict(self._metrics)
            metrics["solver_policy_contexts"] = len(
                self._state["contexts"])
            for key in self._metrics:
                self._metrics[key] = 0
            return metrics


class StringSolverPortfolio:
    """Run or adaptively select bounded backends without trusting UNSAT."""

    def __init__(
        self,
        backends: Sequence[StringSolverBackend],
        *,
        parallelism: int | None = None,
        selection: Mapping[str, Any] | None = None,
    ):
        self.backends = tuple(backends[:8])
        if not self.backends:
            raise ValueError("string solver portfolio must not be empty")
        names = [backend.name for backend in self.backends]
        if len(set(names)) != len(names):
            raise ValueError("string backend names must be unique")
        self.parallelism = max(
            1, min(len(self.backends), int(parallelism or len(self.backends))))
        self.name = "portfolio:" + ",".join(
            backend.name for backend in self.backends)
        self.selection_policy = (
            _ContextualStringBackendPolicy(names, selection)
            if isinstance(selection, Mapping) else None
        )

    def solve_all(
        self,
        query: Mapping[str, Any],
        timeout_ms: int,
        *,
        all_backends: bool = False,
    ) -> list[Mapping[str, Any]]:
        selected_names = (
            tuple(backend.name for backend in self.backends)
            if all_backends or self.selection_policy is None else
            self.selection_policy.select(query, timeout_ms)
        )
        selected = [
            (index, backend) for index, backend in enumerate(self.backends)
            if backend.name in selected_names
        ]
        results: list[Mapping[str, Any] | None] = [None] * len(selected)
        context = _string_query_context(query)
        with ThreadPoolExecutor(
                max_workers=min(self.parallelism, len(selected))) as executor:
            futures = {
                executor.submit(backend.solve, query, timeout_ms): result_index
                for result_index, (_, backend) in enumerate(selected)
            }
            for future in as_completed(futures):
                result_index = futures[future]
                _, backend = selected[result_index]
                try:
                    result = dict(future.result())
                except Exception as error:
                    result = {
                        "status": "error",
                        "assignments": {},
                        "solver": backend.name,
                        "reason": str(error)[:512],
                    }
                result["_portfolio_backend"] = backend.name
                result["_portfolio_context"] = context
                results[result_index] = result
        return [
            result for result in results
            if isinstance(result, Mapping)
        ]

    def solve_all_conformance(
        self,
        query: Mapping[str, Any],
        timeout_ms: int,
    ) -> list[Mapping[str, Any]]:
        """Bypass learned selection so a conformance gate tests every backend."""
        return self.solve_all(query, timeout_ms, all_backends=True)

    def observe_results(
        self,
        results: Sequence[tuple[Mapping[str, Any], bool]],
    ) -> None:
        if self.selection_policy is None:
            return
        grouped: dict[str, list[tuple[str, bool, int]]] = {}
        for result, verified in results:
            context = str(result.get("_portfolio_context", ""))
            backend = str(result.get("_portfolio_backend", ""))
            if not context or not backend:
                continue
            try:
                elapsed_us = max(0, int(result.get("elapsed_us", 0)))
            except (TypeError, ValueError, OverflowError):
                elapsed_us = 0
            grouped.setdefault(context, []).append(
                (backend, bool(verified), elapsed_us))
        for context, feedback in grouped.items():
            self.selection_policy.observe(context, feedback)

    def drain_metrics(self) -> dict[str, int]:
        if self.selection_policy is None:
            return {
                "solver_backends_selected": 0,
                "solver_backends_skipped": 0,
                "solver_policy_updates": 0,
                "solver_policy_explorations": 0,
                "solver_policy_contexts": 0,
            }
        return self.selection_policy.drain_metrics()

    def solve(
        self,
        query: Mapping[str, Any],
        timeout_ms: int,
    ) -> Mapping[str, Any]:
        results = self.solve_all(query, timeout_ms)
        return next(
            (result for result in results if result.get("status") == "sat"),
            results[0] if results else {
                "status": "error",
                "assignments": {},
                "solver": self.name,
                "reason": "empty portfolio result",
            },
        )


def string_solver_backend_from_configuration(
    raw: Any,
    *,
    fallback_command: Sequence[str] | None = None,
) -> StringSolverBackend:
    """Construct a bounded backend/portfolio from a JSON-compatible spec."""
    if raw is None or raw == "":
        return SymccJsonStringBackend(fallback_command)
    parsed = raw
    if isinstance(raw, str):
        candidate = raw.strip()
        if os.path.isfile(candidate):
            with open(candidate, encoding="utf-8") as stream:
                parsed = json.load(stream)
        else:
            parsed = json.loads(candidate)
    if isinstance(parsed, Mapping):
        specifications = parsed.get("backends", [parsed])
        parallelism = parsed.get("parallelism")
        selection = parsed.get("selection")
    else:
        specifications = parsed
        parallelism = None
        selection = None
    if not isinstance(specifications, Sequence) or isinstance(
            specifications, (str, bytes)) or not 1 <= len(specifications) <= 8:
        raise ValueError("string backend configuration must contain 1..8 entries")
    backends: list[StringSolverBackend] = []
    for index, specification in enumerate(specifications):
        if isinstance(specification, str):
            kind = "symcc-json"
            name = f"json-{index}"
            command = shlex.split(specification)
        elif isinstance(specification, Mapping):
            kind = str(specification.get("kind", "symcc-json"))
            name = str(specification.get("name", f"string-{index}"))[:64]
            command_raw = specification.get("command")
            command = (
                shlex.split(command_raw)
                if isinstance(command_raw, str)
                else list(command_raw)
                if isinstance(command_raw, Sequence) and not isinstance(
                    command_raw, (str, bytes))
                else []
            )
        else:
            raise ValueError("invalid string backend entry")
        if not command or any(not isinstance(item, str) or not item
                              for item in command):
            raise ValueError("string backend command must be a string list")
        if kind == "symcc-json":
            backends.append(SymccJsonStringBackend(command, name=name))
        elif kind == "smtlib":
            backends.append(SmtLibCliStringBackend(command, name=name))
        else:
            raise ValueError("unsupported string backend kind")
    if len(backends) == 1:
        return backends[0]
    if selection is not None and not isinstance(selection, Mapping):
        raise ValueError("string backend selection must be an object")
    return StringSolverPortfolio(
        backends, parallelism=parallelism, selection=selection)


def _apply_assignments(
    witness: bytes,
    assignments: Mapping[str, Any],
) -> bytes | None:
    candidate = bytearray(witness)
    for raw_offset, raw_value in assignments.items():
        try:
            offset = int(raw_offset)
            value = int(raw_value)
        except (TypeError, ValueError):
            return None
        if not 0 <= offset < len(candidate) or not 0 <= value <= 255:
            return None
        candidate[offset] = value
    return bytes(candidate)


def materialize_string_candidates(
    records: Iterable[Mapping[str, Any]],
    witness: bytes,
    budget: int,
    verifier: Callable[[bytes], bool] | None = None,
    *,
    solver_backend: StringSolverBackend | None = None,
    solver_timeout_ms: int = 1000,
) -> tuple[list[bytes], dict[str, int]]:
    budget = max(0, min(int(budget), 256))
    verifier = verifier or (lambda _candidate: True)
    accepted: list[bytes] = []
    seen: set[bytes] = {witness}
    considered = 0
    verified = 0
    solver_queries = 0
    solver_sat = 0
    solver_verified = 0
    solver_rejected = 0
    solver_errors = 0
    solver_unsat = 0
    solver_unknown = 0
    solver_backend_runs = 0
    solver_disagreements = 0
    solver_duplicate_models = 0
    solver_backends_selected = 0
    solver_backends_skipped = 0
    solver_policy_updates = 0
    solver_policy_explorations = 0
    solver_policy_contexts = 0
    dual_view_queries = 0
    dual_view_verified = 0
    for raw in records:
        if len(accepted) >= budget:
            break
        record = normalize_string_record(raw)
        if solver_backend is not None:
            query = string_query_from_constraint(record, len(witness))
            if query is not None:
                solver_queries += 1
                dual_view_queries += 1
                try:
                    solve_all = getattr(solver_backend, "solve_all", None)
                    results = (
                        solve_all(query, solver_timeout_ms)
                        if callable(solve_all)
                        else [solver_backend.solve(query, solver_timeout_ms)]
                    )
                except Exception:
                    results = [{"status": "error", "assignments": {}}]
                normalized_results = [
                    result for result in results
                    if isinstance(result, Mapping)
                ][:8]
                statuses = {
                    str(result.get("status", "error"))
                    for result in normalized_results
                }
                if "sat" in statuses and "unsat" in statuses:
                    solver_disagreements += 1
                policy_feedback: list[tuple[Mapping[str, Any], bool]] = []
                for result in normalized_results:
                    solver_backend_runs += 1
                    status = str(result.get("status", "error"))
                    semantically_verified = False
                    if status == "sat":
                        solver_sat += 1
                        assignments = result.get("assignments", {})
                        content = (
                            _apply_assignments(witness, assignments)
                            if isinstance(assignments, Mapping) else None
                        )
                        if (
                            content is not None
                            and candidate_satisfies_string_query(query, content)
                        ):
                            semantically_verified = True
                            if content in seen:
                                solver_duplicate_models += 1
                            elif len(accepted) < budget:
                                solver_verified += 1
                                dual_view_verified += 1
                                seen.add(content)
                                considered += 1
                                verified += 1
                                if verifier(content):
                                    accepted.append(content)
                        else:
                            solver_rejected += 1
                    elif status == "unsat":
                        solver_unsat += 1
                    elif status == "unknown":
                        solver_unknown += 1
                        solver_errors += 1
                    else:
                        solver_errors += 1
                    policy_feedback.append((result, semantically_verified))
                observe_results = getattr(
                    solver_backend, "observe_results", None)
                if callable(observe_results):
                    observe_results(policy_feedback)
                if len(accepted) >= budget:
                    break
        candidate = bytearray(witness)
        valid = True
        for patch in record["patches"]:
            offset = int(patch["offset"])
            if offset >= len(candidate):
                valid = False
                break
            candidate[offset] = int(patch["value"])
        if not valid:
            continue
        content = bytes(candidate)
        if content in seen:
            continue
        seen.add(content)
        considered += 1
        verified += 1
        if verifier(content):
            accepted.append(content)
    drain_metrics = getattr(solver_backend, "drain_metrics", None)
    if callable(drain_metrics):
        policy_metrics = drain_metrics()
        if isinstance(policy_metrics, Mapping):
            solver_backends_selected = max(
                0, int(policy_metrics.get("solver_backends_selected", 0)))
            solver_backends_skipped = max(
                0, int(policy_metrics.get("solver_backends_skipped", 0)))
            solver_policy_updates = max(
                0, int(policy_metrics.get("solver_policy_updates", 0)))
            solver_policy_explorations = max(
                0, int(policy_metrics.get("solver_policy_explorations", 0)))
            solver_policy_contexts = max(
                0, int(policy_metrics.get("solver_policy_contexts", 0)))
    return accepted, {
        "records": considered,
        "verified": verified,
        "accepted": len(accepted),
        "solver_queries": solver_queries,
        "solver_sat": solver_sat,
        "solver_verified": solver_verified,
        "solver_rejected": solver_rejected,
        "solver_errors": solver_errors,
        "solver_unsat": solver_unsat,
        "solver_unknown": solver_unknown,
        "solver_backend_runs": solver_backend_runs,
        "solver_disagreements": solver_disagreements,
        "solver_duplicate_models": solver_duplicate_models,
        "solver_backends_selected": solver_backends_selected,
        "solver_backends_skipped": solver_backends_skipped,
        "solver_policy_updates": solver_policy_updates,
        "solver_policy_explorations": solver_policy_explorations,
        "solver_policy_contexts": solver_policy_contexts,
        "dual_view_queries": dual_view_queries,
        "dual_view_verified": dual_view_verified,
    }
