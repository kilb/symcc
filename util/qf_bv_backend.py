#!/usr/bin/env python3
"""Verified Query-IR lowering for heterogeneous QF_BV solver backends."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Mapping, Sequence

from cross_worker_context import (
    CONTEXT_PROTOCOL as CROSS_WORKER_CONTEXT_PROTOCOL,
    ContextPlan,
    CrossWorkerContextStore,
    MaterializationLease,
    context_chain_identity,
)
from qfbv_proof_receipt import (
    PROOF_PROTOCOL as QFBV_PROOF_PROTOCOL,
    ProofAuthorization,
    ProofVerificationError,
    QfbvProofVerifier,
)
from qfbv_lemma_exchange import (
    LEMMA_PROTOCOL as QFBV_LEMMA_PROTOCOL,
    LemmaExchangeError,
    QfbvLemmaExchange,
    parse_learned_literal_response,
)
from qfbv_substitution_core import (
    CORE_PROTOCOL as QFBV_SUBSTITUTION_CORE_PROTOCOL,
    QfbvSubstitutionCoreExchange,
    SubstitutionCoreError,
)


CAPABILITY_SCHEMA = "symcc-qfbv-capability-v1"
LOWERING_SCHEMA = "symcc-qfbv-lowering-v1"
NATIVE_STATE_FORK_PROTOCOL = "symcc-native-state-fork-v1"

QF_BV_OPERATORS = frozenset({
    "bool",
    "constant",
    "read",
    "concat",
    "extract",
    "zext",
    "sext",
    "add",
    "sub",
    "mul",
    "udiv",
    "sdiv",
    "urem",
    "srem",
    "neg",
    "not",
    "and",
    "or",
    "xor",
    "shl",
    "lshr",
    "ashr",
    "equal",
    "distinct",
    "ult",
    "ule",
    "ugt",
    "uge",
    "slt",
    "sle",
    "sgt",
    "sge",
    "land",
    "lor",
    "lnot",
    "ite",
    "rol",
    "ror",
})


class _BackendCancelled(RuntimeError):
    pass


def _interrupt_process(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = 0.2,
) -> bool:
    if process.poll() is not None:
        return False
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            return False
    try:
        process.wait(timeout=max(0.01, grace_seconds))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            return False
    return True


class QfBvLoweringError(ValueError):
    """A fail-closed Query IR capability or well-formedness rejection."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_int(
    value: Any,
    name: str,
    lower: int,
    upper: int,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < lower or parsed > upper:
        raise ValueError(f"{name} must be in [{lower}, {upper}]")
    return parsed


def normalize_qfbv_capabilities(
    raw: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize the exact capability contract advertised by one backend."""
    raw = raw or {}
    if not isinstance(raw, Mapping):
        raise ValueError("QF_BV capabilities must be an object")
    logic = str(raw.get("logic", "QF_BV"))
    if logic != "QF_BV":
        raise ValueError("QF_BV backend logic must be QF_BV")
    operators_raw = raw.get("operators", sorted(QF_BV_OPERATORS))
    if (not isinstance(operators_raw, Sequence)
            or isinstance(operators_raw, (str, bytes))):
        raise ValueError("QF_BV capability operators must be a list")
    operators = sorted({str(operator) for operator in operators_raw})
    if not operators or any(
            operator not in QF_BV_OPERATORS for operator in operators):
        raise ValueError("QF_BV capabilities contain an unsupported operator")
    model_values = raw.get("model_values", True)
    accept_unsat = raw.get("accept_unsat", False)
    incremental = raw.get("incremental", False)
    if not isinstance(model_values, bool) or not model_values:
        raise ValueError("QF_BV backends must support get-value models")
    if not isinstance(accept_unsat, bool):
        raise ValueError("accept_unsat must be Boolean")
    if not isinstance(incremental, bool):
        raise ValueError("incremental must be Boolean")
    normalized = {
        "schema": CAPABILITY_SCHEMA,
        "logic": "QF_BV",
        "operators": operators,
        "max_bits": _bounded_int(
            raw.get("max_bits", 4096), "max_bits", 1, 1 << 20),
        "max_nodes": _bounded_int(
            raw.get("max_nodes", 250000),
            "max_nodes", 1, 250000),
        "max_input_bytes": _bounded_int(
            raw.get("max_input_bytes", 4096),
            "max_input_bytes", 0, 65536),
        "model_values": True,
        "accept_unsat": accept_unsat,
        "incremental": incremental,
    }
    normalized["capability_sha256"] = _digest(_canonical_json(normalized))
    return normalized


def _bv_constant(value: int, bits: int, *, binary: bool = False) -> str:
    normalized = value & ((1 << bits) - 1)
    if binary:
        return f"#b{normalized:0{bits}b}"
    return f"(_ bv{normalized} {bits})"


def _lower_qfbv_query_plan(
    query_id: str,
    roots: Sequence[str],
    expressions: Mapping[str, Mapping[str, Any]],
    capabilities: Mapping[str, Any],
    *,
    binary_literals: bool = False,
) -> tuple[str, dict[str, Any], tuple[int, ...], tuple[str, ...]]:
    """Lower content-addressed Query IR into independently checked SMT-LIB."""
    normalized_capabilities = normalize_qfbv_capabilities(capabilities)
    allowed = set(normalized_capabilities["operators"])
    max_bits = int(normalized_capabilities["max_bits"])
    max_nodes = int(normalized_capabilities["max_nodes"])
    max_inputs = int(normalized_capabilities["max_input_bytes"])
    if not roots:
        raise QfBvLoweringError("query has no roots")
    if len(expressions) > max_nodes:
        raise QfBvLoweringError("query exceeds backend node capability")

    memo: dict[str, tuple[str, int, str]] = {}
    visiting: set[str] = set()
    read_widths: dict[int, int] = {}
    operator_counts: Counter[str] = Counter()
    maximum_width = 1

    def lower(node_hash: str) -> tuple[str, int, str]:
        nonlocal maximum_width
        if node_hash in memo:
            return memo[node_hash]
        if node_hash in visiting:
            raise QfBvLoweringError("Query IR contains an expression cycle")
        node = expressions.get(node_hash)
        if not isinstance(node, Mapping):
            raise QfBvLoweringError("Query IR references a missing node")
        op = str(node.get("op", ""))
        if op not in allowed:
            raise QfBvLoweringError(
                f"operator {op or '<empty>'} is outside backend capability")
        bits = _bounded_int(node.get("bits"), "node bits", 1, max_bits)
        children_raw = node.get("children", ())
        attrs = node.get("attrs", {})
        if (not isinstance(children_raw, list)
                or not isinstance(attrs, Mapping)):
            raise QfBvLoweringError("malformed Query IR node")
        children = [str(child) for child in children_raw]
        visiting.add(node_hash)
        lowered_children = [lower(child) for child in children]
        visiting.remove(node_hash)
        operator_counts[op] += 1
        maximum_width = max(maximum_width, bits)

        def require_arity(*allowed_arities: int) -> None:
            if len(children) not in allowed_arities:
                expected = "/".join(str(value) for value in allowed_arities)
                raise QfBvLoweringError(
                    f"{op} expects arity {expected}, got {len(children)}")

        def require_bv(index: int, width: int | None = None) -> int:
            sort, child_bits, _term = lowered_children[index]
            if sort != "BV" or (width is not None and child_bits != width):
                raise QfBvLoweringError(f"{op} has incompatible BV operands")
            return child_bits

        def require_bool(index: int) -> None:
            sort, child_bits, _term = lowered_children[index]
            if sort != "Bool" or child_bits != 1:
                raise QfBvLoweringError(f"{op} requires Boolean operands")

        terms = [child[2] for child in lowered_children]
        sort = "BV"
        if op == "bool":
            require_arity(0)
            if bits != 1 or not isinstance(attrs.get("value"), bool):
                raise QfBvLoweringError("bool node has an invalid value")
            sort = "Bool"
            term = "true" if attrs["value"] else "false"
        elif op == "constant":
            require_arity(0)
            try:
                value = int(str(attrs.get("value_hex", "")), 16)
            except ValueError as error:
                raise QfBvLoweringError(
                    "constant has invalid hexadecimal value") from error
            if value < 0 or value.bit_length() > bits:
                raise QfBvLoweringError("constant does not fit its bit width")
            term = _bv_constant(value, bits, binary=binary_literals)
        elif op == "read":
            require_arity(0)
            if bits != 8:
                raise QfBvLoweringError(
                    "byte-assignment protocol requires 8-bit reads")
            index = _bounded_int(
                attrs.get("index"), "read index", 0, (1 << 32) - 1)
            previous = read_widths.setdefault(index, bits)
            if previous != bits:
                raise QfBvLoweringError(
                    "one input offset has inconsistent widths")
            if len(read_widths) > max_inputs:
                raise QfBvLoweringError(
                    "query exceeds backend input-byte capability")
            term = f"symcc_input_{index}"
        elif op == "concat":
            require_arity(2)
            left_bits = require_bv(0)
            right_bits = require_bv(1)
            if bits != left_bits + right_bits:
                raise QfBvLoweringError("concat result width is inconsistent")
            term = f"(concat {terms[0]} {terms[1]})"
        elif op == "extract":
            require_arity(1)
            source_bits = require_bv(0)
            index = _bounded_int(
                attrs.get("index"), "extract index", 0, max_bits)
            if index + bits > source_bits:
                raise QfBvLoweringError("extract range exceeds its operand")
            term = f"((_ extract {index + bits - 1} {index}) {terms[0]})"
        elif op in {"zext", "sext"}:
            require_arity(1)
            source_bits = require_bv(0)
            if bits < source_bits:
                raise QfBvLoweringError("extension narrows its operand")
            extension = bits - source_bits
            smt_op = "zero_extend" if op == "zext" else "sign_extend"
            term = f"((_ {smt_op} {extension}) {terms[0]})"
        elif op in {
            "add", "sub", "mul", "udiv", "sdiv", "urem", "srem",
            "shl", "lshr", "ashr",
        }:
            require_arity(2)
            require_bv(0, bits)
            require_bv(1, bits)
            smt_op = {
                "add": "bvadd",
                "sub": "bvsub",
                "mul": "bvmul",
                "udiv": "bvudiv",
                "sdiv": "bvsdiv",
                "urem": "bvurem",
                "srem": "bvsrem",
                "shl": "bvshl",
                "lshr": "bvlshr",
                "ashr": "bvashr",
            }[op]
            term = f"({smt_op} {terms[0]} {terms[1]})"
        elif op in {"neg", "not"}:
            require_arity(1)
            require_bv(0, bits)
            smt_op = "bvneg" if op == "neg" else "bvnot"
            term = f"({smt_op} {terms[0]})"
        elif op in {"and", "or", "xor"}:
            require_arity(1, 2, 3)
            for index in range(len(terms)):
                require_bv(index, bits)
            smt_op = {"and": "bvand", "or": "bvor", "xor": "bvxor"}[op]
            term = terms[0]
            for operand in terms[1:]:
                term = f"({smt_op} {term} {operand})"
        elif op in {
            "ult", "ule", "ugt", "uge", "slt", "sle", "sgt", "sge",
        }:
            require_arity(2)
            left_bits = require_bv(0)
            require_bv(1, left_bits)
            if bits != 1:
                raise QfBvLoweringError("comparison result must be Boolean")
            sort = "Bool"
            term = f"(bv{op} {terms[0]} {terms[1]})"
        elif op in {"equal", "distinct"}:
            require_arity(2)
            if lowered_children[0][:2] != lowered_children[1][:2] or bits != 1:
                raise QfBvLoweringError("equality operand sorts do not match")
            sort = "Bool"
            equality = f"(= {terms[0]} {terms[1]})"
            term = equality if op == "equal" else f"(not {equality})"
        elif op in {"land", "lor"}:
            require_arity(1, 2, 3)
            if bits != 1:
                raise QfBvLoweringError("logical result must be Boolean")
            for index in range(len(terms)):
                require_bool(index)
            sort = "Bool"
            smt_op = "and" if op == "land" else "or"
            term = terms[0] if len(terms) == 1 else (
                f"({smt_op} {' '.join(terms)})")
        elif op == "lnot":
            require_arity(1)
            require_bool(0)
            if bits != 1:
                raise QfBvLoweringError("logical not result must be Boolean")
            sort = "Bool"
            term = f"(not {terms[0]})"
        elif op == "ite":
            require_arity(3)
            require_bool(0)
            if lowered_children[1][:2] != lowered_children[2][:2]:
                raise QfBvLoweringError("ite branch sorts do not match")
            sort, branch_bits = lowered_children[1][:2]
            if bits != branch_bits:
                raise QfBvLoweringError("ite result width is inconsistent")
            term = f"(ite {terms[0]} {terms[1]} {terms[2]})"
        elif op in {"rol", "ror"}:
            require_arity(2)
            require_bv(0, bits)
            require_bv(1, bits)
            width = _bv_constant(bits, bits, binary=binary_literals)
            shift = f"(bvurem {terms[1]} {width})"
            remaining = f"(bvsub {width} {shift})"
            if op == "rol":
                first = f"(bvshl {terms[0]} {shift})"
                second = f"(bvlshr {terms[0]} {remaining})"
            else:
                first = f"(bvlshr {terms[0]} {shift})"
                second = f"(bvshl {terms[0]} {remaining})"
            term = f"(bvor {first} {second})"
        else:
            raise QfBvLoweringError(f"operator {op} is not lowerable")

        result = (sort, bits, term)
        memo[node_hash] = result
        return result

    root_terms: list[str] = []
    for root in roots:
        sort, bits, term = lower(str(root))
        if sort != "Bool" or bits != 1:
            raise QfBvLoweringError("query roots must be Boolean")
        root_terms.append(term)
    if len(memo) > max_nodes:
        raise QfBvLoweringError("reachable query exceeds backend node capability")

    offsets = tuple(sorted(read_widths))
    lines = [
        "(set-logic QF_BV)",
        "(set-option :produce-models true)",
    ]
    lines.extend(
        f"(declare-fun symcc_input_{offset} () (_ BitVec 8))"
        for offset in offsets
    )
    lines.extend(f"(assert {term})" for term in root_terms)
    lines.append("(check-sat)")
    if offsets:
        symbols = " ".join(
            f"symcc_input_{offset}" for offset in offsets)
        lines.append(f"(get-value ({symbols}))")
    lines.append("(exit)")
    smt2 = "\n".join(lines) + "\n"
    offset_digest = _digest(_canonical_json(offsets))
    certificate = {
        "schema": LOWERING_SCHEMA,
        "query_id": str(query_id)[:128],
        "logic": "QF_BV",
        "node_count": len(memo),
        "root_count": len(root_terms),
        "input_offset_count": len(offsets),
        "input_offsets_sha256": offset_digest,
        "maximum_width": maximum_width,
        "operator_counts": dict(sorted(operator_counts.items())),
        "capability_sha256": normalized_capabilities["capability_sha256"],
        "smt2_sha256": _digest(smt2.encode("ascii")),
        "sort_verified": True,
        "model_protocol": "smtlib-get-value-input-bytes-v1",
    }
    certificate["certificate_sha256"] = _digest(
        _canonical_json(certificate))
    return smt2, certificate, offsets, tuple(root_terms)


def lower_qfbv_query(
    query_id: str,
    roots: Sequence[str],
    expressions: Mapping[str, Mapping[str, Any]],
    capabilities: Mapping[str, Any],
) -> tuple[str, dict[str, Any], tuple[int, ...]]:
    """Lower a complete Query IR formula and omit internal term fragments."""
    smt2, certificate, offsets, _root_terms = _lower_qfbv_query_plan(
        query_id, roots, expressions, capabilities)
    return smt2, certificate, offsets


def lower_qfbv_proof_problem(
    query_id: str,
    roots: Sequence[str],
    expressions: Mapping[str, Mapping[str, Any]],
    capabilities: Mapping[str, Any],
) -> tuple[
    str,
    str,
    str,
    dict[str, Any],
    tuple[int, ...],
    tuple[str, ...],
    dict[str, Any] | None,
]:
    """Lower the solver query and an Ethos-referenceable proof problem."""
    smt2, certificate, offsets, root_terms = _lower_qfbv_query_plan(
        query_id,
        roots,
        expressions,
        capabilities,
    )
    (
        _binary_smt2,
        binary_certificate,
        binary_offsets,
        binary_root_terms,
    ) = _lower_qfbv_query_plan(
        query_id,
        roots,
        expressions,
        capabilities,
        binary_literals=True,
    )
    comparable = {
        key: value
        for key, value in certificate.items()
        if key not in {"smt2_sha256", "certificate_sha256"}
    }
    binary_comparable = {
        key: value
        for key, value in binary_certificate.items()
        if key not in {"smt2_sha256", "certificate_sha256"}
    }
    if offsets != binary_offsets or comparable != binary_comparable:
        raise QfBvLoweringError("proof lowering diverged from solver lowering")
    for normal_term, binary_term in zip(root_terms, binary_root_terms, strict=True):
        normal_forms = _parse_s_expressions(normal_term)
        binary_forms = _parse_s_expressions(binary_term)
        if (
            len(normal_forms) != 1
            or len(binary_forms) != 1
            or _canonical_qfbv_term(normal_forms[0])
            != _canonical_qfbv_term(binary_forms[0])
        ):
            raise QfBvLoweringError(
                "proof literal lowering is not structurally equivalent"
            )
    reference_lines = ["(set-logic QF_BV)"]
    reference_lines.extend(
        f"(declare-const symcc_input_{offset} (_ BitVec 8))"
        for offset in offsets
    )
    reference_lines.extend(
        f"(assert {term})" for term in binary_root_terms
    )
    reference_smt2 = "\n".join(reference_lines) + "\n"
    proof_query_smt2 = reference_smt2 + "(check-sat)\n(exit)\n"
    context = context_chain_identity(
        tuple(str(root) for root in roots[:-1]),
        root_terms[:-1],
        capability_sha256=str(certificate["capability_sha256"]),
    )
    return (
        smt2,
        proof_query_smt2,
        reference_smt2,
        certificate,
        offsets,
        root_terms,
        context,
    )


def qfbv_prefix_context_identity(
    query_id: str,
    roots: Sequence[str],
    expressions: Mapping[str, Mapping[str, Any]],
    capabilities: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Recompute lowering and the canonical shared-prefix identity."""
    _smt2, certificate, _offsets, root_terms = _lower_qfbv_query_plan(
        query_id,
        roots,
        expressions,
        capabilities,
    )
    identity = context_chain_identity(
        tuple(str(root) for root in roots[:-1]),
        root_terms[:-1],
        capability_sha256=str(certificate["capability_sha256"]),
    )
    return certificate, identity


def qfbv_full_context_identity(
    query_id: str,
    roots: Sequence[str],
    expressions: Mapping[str, Mapping[str, Any]],
    capabilities: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Recompute lowering and the canonical full-formula context identity."""
    _smt2, certificate, _offsets, root_terms = _lower_qfbv_query_plan(
        query_id,
        roots,
        expressions,
        capabilities,
    )
    identity = context_chain_identity(
        tuple(str(root) for root in roots),
        root_terms,
        capability_sha256=str(certificate["capability_sha256"]),
    )
    return certificate, identity


def _parse_s_expressions(text: str) -> list[Any]:
    if len(text.encode("utf-8", errors="replace")) > 8 * 1024 * 1024:
        raise ValueError("SMT-LIB response exceeds 8 MiB")
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == ";":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
            continue
        if char in "()":
            tokens.append(char)
            index += 1
            continue
        if char == '"':
            start = index
            index += 1
            while index < len(text):
                if text[index] == '"':
                    if index + 1 < len(text) and text[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise ValueError("unterminated SMT-LIB string")
            tokens.append(text[start:index])
            continue
        if char == "|":
            start = index
            index += 1
            while index < len(text) and text[index] != "|":
                if text[index] == "\\":
                    index += 1
                index += 1
            if index >= len(text):
                raise ValueError("unterminated quoted SMT-LIB symbol")
            index += 1
            tokens.append(text[start:index])
            continue
        start = index
        while (index < len(text) and not text[index].isspace()
               and text[index] not in "();"):
            index += 1
        tokens.append(text[start:index])
        if len(tokens) > 1000000:
            raise ValueError("SMT-LIB response has too many tokens")

    forms: list[Any] = []
    stack: list[list[Any]] = []
    for token in tokens:
        if token == "(":
            stack.append([])
        elif token == ")":
            if not stack:
                raise ValueError("unbalanced SMT-LIB response")
            completed = stack.pop()
            if stack:
                stack[-1].append(completed)
            else:
                forms.append(completed)
        elif stack:
            stack[-1].append(token)
        else:
            forms.append(token)
    if stack:
        raise ValueError("unbalanced SMT-LIB response")
    return forms


def parse_native_state_fork_metadata(output: str) -> dict[str, Any]:
    """Validate one native fork response without trusting helper telemetry."""
    forms = _parse_s_expressions(output)
    metadata_forms = [
        form
        for form in forms
        if (
            isinstance(form, list)
            and form
            and form[0] == NATIVE_STATE_FORK_PROTOCOL
        )
    ]
    if len(metadata_forms) != 1:
        raise ValueError(
            "native state response must contain one protocol metadata form"
        )
    if any(
        isinstance(form, list) and form and form[0] == "error"
        for form in forms
    ):
        raise ValueError("native state helper returned an SMT-LIB error")
    rows: dict[str, str] = {}
    for row in metadata_forms[0][1:]:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or not all(isinstance(item, str) for item in row)
        ):
            raise ValueError("native state metadata row is malformed")
        key, value = row
        if key in rows:
            raise ValueError(f"duplicate native state metadata key {key!r}")
        rows[key] = value
    numeric_keys = {
        "snapshot-generation",
        "snapshot-queries",
        "warm-checks",
        "forked",
        "child-pid",
        "child-timed-out",
        "child-solve-us",
        "fork-roundtrip-us",
        "child-minor-faults",
        "child-major-faults",
        "child-max-rss-kib",
    }
    expected = numeric_keys | {"child-status", "warm-status"}
    if set(rows) != expected:
        raise ValueError("native state metadata keys do not match protocol")
    values: dict[str, int] = {}
    for key in numeric_keys:
        raw = rows[key]
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 20:
            raise ValueError(f"native state metadata {key!r} is not decimal")
        value = int(raw, 10)
        if value > (1 << 63) - 1:
            raise ValueError(f"native state metadata {key!r} is out of range")
        values[key] = value
    statuses = [
        form
        for form in forms
        if isinstance(form, str) and form in {"sat", "unsat", "unknown"}
    ]
    if len(statuses) != 1:
        raise ValueError("native state response must contain one solver status")
    if rows["child-status"] not in {"sat", "unsat", "unknown"}:
        raise ValueError("native child status is invalid")
    if rows["warm-status"] not in {"sat", "unsat", "unknown"}:
        raise ValueError("native warm status is invalid")
    if rows["child-status"] != statuses[0]:
        raise ValueError("native child status disagrees with solver response")
    if values["snapshot-generation"] < 1:
        raise ValueError("native snapshot generation must be positive")
    if values["snapshot-queries"] < 1:
        raise ValueError("native snapshot query count must be positive")
    if values["warm-checks"] != values["snapshot-generation"]:
        raise ValueError("native warm-check count disagrees with generation")
    if values["forked"] != 1 or values["child-pid"] < 1:
        raise ValueError("native response was not produced by one child")
    if values["child-timed-out"] not in {0, 1}:
        raise ValueError("native child timeout flag is invalid")
    if values["child-timed-out"] and rows["child-status"] != "unknown":
        raise ValueError("timed-out native child must return unknown")
    if values["fork-roundtrip-us"] < values["child-solve-us"]:
        raise ValueError("native fork timing is internally inconsistent")
    return {
        "backend_native_state_protocol": NATIVE_STATE_FORK_PROTOCOL,
        "backend_native_snapshot_generation": values[
            "snapshot-generation"
        ],
        "backend_native_snapshot_queries": values["snapshot-queries"],
        "backend_native_warm_checks": values["warm-checks"],
        "backend_native_forked": True,
        "backend_native_child_pid": values["child-pid"],
        "backend_native_child_status": rows["child-status"],
        "backend_native_child_timed_out": bool(values["child-timed-out"]),
        "backend_native_child_solve_us": values["child-solve-us"],
        "backend_native_fork_roundtrip_us": values["fork-roundtrip-us"],
        "backend_native_child_minor_faults": values[
            "child-minor-faults"
        ],
        "backend_native_child_major_faults": values[
            "child-major-faults"
        ],
        "backend_native_child_max_rss_kib": values[
            "child-max-rss-kib"
        ],
        "backend_native_warm_status": rows["warm-status"],
    }


def _canonical_qfbv_term(value: Any) -> Any:
    """Canonicalize the two standard ground bit-vector literal spellings."""
    if isinstance(value, str) and value.startswith("#b"):
        digits = value[2:]
        if not digits or any(bit not in "01" for bit in digits):
            raise QfBvLoweringError("invalid binary proof literal")
        return ("bitvector-literal", len(digits), int(digits, 2))
    if (
        isinstance(value, list)
        and len(value) == 3
        and value[0] == "_"
        and isinstance(value[1], str)
        and value[1].startswith("bv")
    ):
        try:
            width = int(value[2])
            number = int(value[1][2:])
        except (TypeError, ValueError) as error:
            raise QfBvLoweringError("invalid indexed proof literal") from error
        if width <= 0 or number < 0 or number >= (1 << width):
            raise QfBvLoweringError("out-of-range indexed proof literal")
        return ("bitvector-literal", width, number)
    if isinstance(value, list):
        return tuple(_canonical_qfbv_term(item) for item in value)
    return value


def _bitvector_value(value: Any, width: int) -> int | None:
    if isinstance(value, str):
        if value.startswith("#x"):
            digits = value[2:]
            if len(digits) * 4 != width:
                return None
            try:
                return int(digits, 16)
            except ValueError:
                return None
        if value.startswith("#b"):
            digits = value[2:]
            if len(digits) != width or any(bit not in "01" for bit in digits):
                return None
            return int(digits, 2)
        return None
    if (isinstance(value, list) and len(value) == 3
            and value[0] == "_" and isinstance(value[1], str)
            and value[1].startswith("bv")):
        try:
            parsed_width = int(value[2])
            parsed_value = int(value[1][2:])
        except (TypeError, ValueError):
            return None
        if parsed_width != width or not 0 <= parsed_value < (1 << width):
            return None
        return parsed_value
    return None


def parse_qfbv_response(
    output: str,
    offsets: Sequence[int],
) -> tuple[str, dict[str, int]]:
    forms = _parse_s_expressions(output)
    statuses = [
        form for form in forms
        if isinstance(form, str) and form in {"sat", "unsat", "unknown"}
    ]
    if len(statuses) != 1:
        raise ValueError("SMT-LIB response must contain one solver status")
    status = statuses[0]
    if status != "sat":
        return status, {}
    expected = {f"symcc_input_{int(offset)}": int(offset)
                for offset in offsets}
    assignments: dict[str, int] = {}

    def visit(form: Any) -> None:
        if not isinstance(form, list):
            return
        for entry in form:
            if (isinstance(entry, list) and len(entry) == 2
                    and isinstance(entry[0], str)):
                symbol = entry[0]
                if symbol.startswith("|") and symbol.endswith("|"):
                    symbol = symbol[1:-1]
                offset = expected.get(symbol)
                if offset is not None:
                    parsed = _bitvector_value(entry[1], 8)
                    if parsed is None:
                        raise ValueError(
                            f"invalid model value for {symbol}")
                    assignments[str(offset)] = parsed
            visit(entry)

    for form in forms:
        visit(form)
    if set(assignments) != {str(offset) for offset in offsets}:
        raise ValueError("SMT-LIB model is missing input-byte values")
    return status, assignments


class SmtLibQfbvSolver:
    """One-shot exact QF_BV backend with independent Query IR validation."""

    def __init__(
        self,
        store: Any,
        command: Sequence[str],
        *,
        name: str,
        capabilities: Mapping[str, Any] | None = None,
        proof_verifier: QfbvProofVerifier | None = None,
        substitution_core_exchange: QfbvSubstitutionCoreExchange | None = None,
    ):
        if not command or any(not str(item) for item in command):
            raise ValueError("SMT-LIB QF_BV command must not be empty")
        self.store = store
        self.command = tuple(str(item) for item in command)
        self.name = str(name)[:128] or "smtlib-qfbv"
        self.capabilities = normalize_qfbv_capabilities(capabilities)
        self.proof_verifier = proof_verifier
        self.substitution_core_exchange = substitution_core_exchange
        if self.substitution_core_exchange is not None and (
            self.proof_verifier is None
            or self.substitution_core_exchange.proof_verifier.policy_sha256
            != self.proof_verifier.policy_sha256
        ):
            raise ValueError(
                "substitution-core exchange requires the active proof verifier"
            )
        self._active_lock = threading.Lock()
        self._active: dict[
            int, tuple[str, subprocess.Popen[Any], threading.Event]
        ] = {}
        self._running_queries: set[str] = set()
        self._pending_cancellations: set[str] = set()

    def _begin_query(self, query_id: str) -> None:
        with self._active_lock:
            self._running_queries.add(query_id)

    def _end_query(self, query_id: str) -> None:
        with self._active_lock:
            self._running_queries.discard(query_id)
            self._pending_cancellations.discard(query_id)

    def _register_process(
        self,
        query_id: str,
        process: subprocess.Popen[Any],
    ) -> tuple[int, threading.Event]:
        token = id(process)
        cancelled = threading.Event()
        with self._active_lock:
            self._active[token] = (query_id, process, cancelled)
            cancel_immediately = query_id in self._pending_cancellations
            if cancel_immediately:
                cancelled.set()
        if cancel_immediately:
            _interrupt_process(process)
        return token, cancelled

    def _unregister_process(self, token: int) -> None:
        with self._active_lock:
            self._active.pop(token, None)

    def cancel(self, lease: Any) -> bool:
        query_id = str(lease.query_id)
        with self._active_lock:
            running = query_id in self._running_queries
            # A portfolio future can be running before the backend reaches
            # _begin_query().  Preserve cancellation across that hand-off so
            # the first registered process is interrupted immediately.
            self._pending_cancellations.add(query_id)
            active = [
                (process, cancelled)
                for active_query, process, cancelled in self._active.values()
                if active_query == query_id and process.poll() is None
            ]
            for _, cancelled in active:
                cancelled.set()
        for process, _ in active:
            _interrupt_process(process)
        return running or bool(active)

    def _base_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "assignments": {},
            "solver": self.name,
            "backend_kind": "smtlib-qfbv",
            "backend_capabilities": self.capabilities,
            "backend_model_verified": False,
            "backend_unsat_authorized": False,
            "capability_status": "supported",
        }
        if self.substitution_core_exchange is not None:
            result.update(
                {
                    "backend_substitution_core_protocol": (
                        QFBV_SUBSTITUTION_CORE_PROTOCOL
                    ),
                    "backend_substitution_core_policy_sha256": (
                        self.substitution_core_exchange.policy_sha256
                    ),
                    "backend_substitution_core_attempted": False,
                    "backend_substitution_core_hit": False,
                    "backend_substitution_core_candidates": 0,
                    "backend_substitution_core_candidate_scan": 0,
                    "backend_substitution_core_checker_elapsed_us": 0,
                    "backend_substitution_core_proof_reused": False,
                    "backend_substitution_core_match_elapsed_us": 0,
                    "backend_substitution_core_clause_pairs": 0,
                    "backend_substitution_core_candidate_rows": 0,
                    "backend_substitution_core_join_states": 0,
                    "backend_substitution_core_publish_attempted": False,
                    "backend_substitution_core_published": False,
                    "backend_substitution_core_publish_created": False,
                }
            )
        return result

    def _try_substitution_core_reuse(
        self,
        lease: Any,
        base: dict[str, Any],
        roots: Sequence[str],
        expressions: Mapping[str, Mapping[str, Any]],
        *,
        timeout_ms: int,
        started_ns: int,
    ) -> Mapping[str, Any] | None:
        exchange = self.substitution_core_exchange
        if exchange is None:
            return None
        base["backend_substitution_core_attempted"] = True
        try:
            authorization = exchange.lookup(
                roots,
                expressions,
                self.capabilities,
                timeout_ms=max(1, min(timeout_ms, exchange.lookup_timeout_ms)),
            )
        except (
            FileNotFoundError,
            OSError,
            sqlite3.Error,
            TimeoutError,
            SubstitutionCoreError,
        ) as error:
            base["backend_substitution_core_reason"] = str(error)[:512]
            return None
        if authorization is None:
            return None
        match = authorization.match
        record = authorization.record
        return {
            **base,
            "status": "unsat",
            "backend_status": "unsat",
            "backend_unsat_authorized": True,
            "backend_substitution_core_hit": True,
            "backend_substitution_core_candidates": authorization.candidates,
            "backend_substitution_core_candidate_scan": (
                authorization.candidate_scan
            ),
            "backend_substitution_core_checker_elapsed_us": (
                authorization.checker_elapsed_us
            ),
            "backend_substitution_core_proof_reused": (
                authorization.proof_reused
            ),
            "backend_substitution_core_match_elapsed_us": match.elapsed_us,
            "backend_substitution_core_clause_pairs": (
                match.clause_pairs_considered
            ),
            "backend_substitution_core_candidate_rows": match.candidate_rows,
            "backend_substitution_core_join_states": match.join_states,
            "backend_substitution_core_record_sha256": record["record_sha256"],
            "backend_substitution_core_source_query_id": (
                record["source_query_id"]
            ),
            "backend_substitution_core_mapping": [
                [source, target] for source, target in sorted(match.mapping.items())
            ],
            "elapsed_us": (time.monotonic_ns() - started_ns) // 1000,
        }

    def _publish_substitution_core(
        self,
        result: Mapping[str, Any],
        lease: Any,
        roots: Sequence[str],
        expressions: Mapping[str, Mapping[str, Any]],
        root_terms: Sequence[str],
        offsets: Sequence[int],
        *,
        started_ns: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        finalized = dict(result)
        exchange = self.substitution_core_exchange
        if (
            exchange is None
            or finalized.get("status") != "unsat"
            or finalized.get("backend_substitution_core_hit") is True
        ):
            return finalized
        finalized["backend_substitution_core_publish_attempted"] = True
        remaining_ms = timeout_ms - int(
            (time.monotonic_ns() - started_ns) // 1_000_000
        )
        if remaining_ms <= 0:
            finalized["backend_substitution_core_publish_reason"] = (
                "core publication deadline expired"
            )
            return finalized
        try:
            record, created, extractor_us, proof_us = exchange.publish_from_unsat(
                source_query_id=str(lease.query_id),
                roots=roots,
                expressions=expressions,
                root_terms=root_terms,
                offsets=offsets,
                capabilities=self.capabilities,
                timeout_ms=min(remaining_ms, exchange.publish_timeout_ms),
                register_process=lambda process: self._register_process(
                    str(lease.query_id), process
                ),
                unregister_process=self._unregister_process,
            )
        except (
            FileNotFoundError,
            OSError,
            sqlite3.Error,
            TimeoutError,
            SubstitutionCoreError,
        ) as error:
            finalized["backend_substitution_core_publish_reason"] = str(error)[:512]
            return finalized
        finalized.update(
            {
                "backend_substitution_core_published": True,
                "backend_substitution_core_publish_created": created,
                "backend_substitution_core_published_record_sha256": (
                    record["record_sha256"]
                ),
                "backend_substitution_core_published_clause_count": (
                    record["source_clause_count"]
                ),
                "backend_substitution_core_extractor_elapsed_us": extractor_us,
                "backend_substitution_core_publish_proof_elapsed_us": proof_us,
            }
        )
        finalized["elapsed_us"] = (
            time.monotonic_ns() - started_ns
        ) // 1000
        return finalized

    @staticmethod
    def _proof_result_fields(
        authorization: ProofAuthorization,
    ) -> dict[str, Any]:
        return {
            "backend_unsat_authorized": True,
            "backend_unsat_proof_verified": True,
            "backend_unsat_proof_protocol": QFBV_PROOF_PROTOCOL,
            "backend_unsat_proof_receipt": authorization.receipt,
            "backend_unsat_proof_reused": authorization.reused,
            "backend_unsat_proof_generator_elapsed_us": (
                authorization.generator_elapsed_us
            ),
            "backend_unsat_proof_checker_elapsed_us": (
                authorization.checker_elapsed_us
            ),
        }

    def _proof_authorization(
        self,
        lease: Any,
        proof_inputs: Mapping[str, Any],
        *,
        timeout_ms: int,
        reuse_only: bool,
    ) -> ProofAuthorization | None:
        if self.proof_verifier is None:
            return None
        arguments = {
            "query_id": str(lease.query_id),
            "smt2": str(proof_inputs["smt2"]).encode("ascii"),
            "proof_query_smt2": str(
                proof_inputs["proof_query_smt2"]
            ).encode("ascii"),
            "reference_smt2": str(
                proof_inputs["reference_smt2"]
            ).encode("ascii"),
            "offsets": tuple(int(value) for value in proof_inputs["offsets"]),
            "lowering_certificate_sha256": str(
                proof_inputs["lowering_certificate_sha256"]
            ),
            "capability_sha256": str(
                self.capabilities["capability_sha256"]
            ),
            "context": proof_inputs.get("context"),
            "timeout_ms": max(1, timeout_ms),
            "register_process": lambda process: self._register_process(
                str(lease.query_id), process
            ),
            "unregister_process": self._unregister_process,
        }
        if reuse_only:
            return self.proof_verifier.try_reuse(**arguments)
        return self.proof_verifier.authorize(**arguments)

    def _try_proof_reuse(
        self,
        lease: Any,
        base: Mapping[str, Any],
        proof_inputs: Mapping[str, Any],
        *,
        timeout_ms: int,
        started_ns: int,
    ) -> Mapping[str, Any] | None:
        if self.proof_verifier is None:
            return None
        try:
            authorization = self._proof_authorization(
                lease,
                proof_inputs,
                timeout_ms=timeout_ms,
                reuse_only=True,
            )
        except (OSError, sqlite3.Error, ProofVerificationError) as error:
            cancelled = "cancelled" in str(error).lower()
            return {
                **base,
                "status": "unknown",
                "backend_status": "unknown",
                "elapsed_us": (time.monotonic_ns() - started_ns) // 1000,
                "cancelled": cancelled,
                "cancel_reason": (
                    "portfolio-sat-winner" if cancelled else ""
                ),
                "reason": f"UNSAT proof receipt reuse failed: {error}"[:512],
            }
        if authorization is None:
            return None
        return {
            **base,
            "status": "unsat",
            "backend_status": "unsat",
            **self._proof_result_fields(authorization),
            "elapsed_us": (time.monotonic_ns() - started_ns) // 1000,
        }

    def _finalize_response(
        self,
        lease: Any,
        base: Mapping[str, Any],
        output: str,
        offsets: Sequence[int],
        elapsed_us: int,
        *,
        proof_inputs: Mapping[str, Any] | None = None,
        proof_timeout_ms: int = 0,
    ) -> Mapping[str, Any]:
        try:
            status, assignments = parse_qfbv_response(output, offsets)
        except ValueError as error:
            return {
                **base,
                "status": "error",
                "elapsed_us": elapsed_us,
                "reason": str(error)[:512],
            }
        if status == "unknown":
            return {
                **base,
                "status": "unknown",
                "elapsed_us": elapsed_us,
                "reason": "SMT-LIB QF_BV backend returned unknown",
            }
        if status == "unsat":
            if self.proof_verifier is not None:
                if proof_inputs is None or proof_timeout_ms <= 0:
                    return {
                        **base,
                        "status": "unknown",
                        "backend_status": "unsat",
                        "elapsed_us": elapsed_us,
                        "reason": "UNSAT proof budget was exhausted",
                    }
                try:
                    authorization = self._proof_authorization(
                        lease,
                        proof_inputs,
                        timeout_ms=proof_timeout_ms,
                        reuse_only=False,
                    )
                except (OSError, sqlite3.Error, ProofVerificationError) as error:
                    cancelled = "cancelled" in str(error).lower()
                    return {
                        **base,
                        "status": "unknown",
                        "backend_status": "unsat",
                        "elapsed_us": elapsed_us,
                        "cancelled": cancelled,
                        "cancel_reason": (
                            "portfolio-sat-winner" if cancelled else ""
                        ),
                        "reason": f"UNSAT proof verification failed: {error}"[:512],
                    }
                assert authorization is not None
                return {
                    **base,
                    "status": "unsat",
                    "backend_status": "unsat",
                    **self._proof_result_fields(authorization),
                    "elapsed_us": (
                        elapsed_us
                        + authorization.generator_elapsed_us
                        + authorization.checker_elapsed_us
                    ),
                }
            if not self.capabilities["accept_unsat"]:
                return {
                    **base,
                    "status": "unknown",
                    "backend_status": "unsat",
                    "elapsed_us": elapsed_us,
                    "reason": (
                        "backend UNSAT is not authorized by its capability "
                        "contract"
                    ),
                }
            return {
                **base,
                "status": "unsat",
                "backend_unsat_authorized": True,
                "elapsed_us": elapsed_us,
            }

        try:
            candidate = bytearray.fromhex(str(lease.input_hex))
        except ValueError:
            candidate = bytearray()
        for raw_offset, value in assignments.items():
            offset = int(raw_offset)
            if offset < 0 or offset >= len(candidate):
                return {
                    **base,
                    "status": "unknown",
                    "elapsed_us": elapsed_us,
                    "reason": "SMT-LIB model exceeds the concrete witness",
                }
            candidate[offset] = value
        if not self.store.validate_candidate(
                str(lease.query_id), bytes(candidate)):
            return {
                **base,
                "status": "unknown",
                "elapsed_us": elapsed_us,
                "reason": "SMT-LIB model failed Query IR validation",
            }
        return {
            **base,
            "status": "sat",
            "assignments": assignments,
            "backend_model_verified": True,
            "elapsed_us": elapsed_us,
        }

    def _confirm_nonzero_unsat(
        self,
        query_id: str,
        smt2: str,
        timeout_ms: int,
    ) -> str | None:
        """Confirm UNSAT without requesting a model after a CLI error exit."""
        status_only = "\n".join(
            line
            for line in smt2.splitlines()
            if not line.startswith("(get-value (")
        ) + "\n"
        query_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="ascii", suffix=".smt2",
                    delete=False) as output:
                output.write(status_only)
                query_path = output.name
            has_query_placeholder = any(
                "{query}" in item for item in self.command)
            command = [
                item.replace("{query}", query_path).replace(
                    "{timeout_ms}", str(timeout_ms))
                for item in self.command
            ]
            if not has_query_placeholder:
                command.append(query_path)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            token, cancelled = self._register_process(query_id, process)
            try:
                stdout, _stderr = process.communicate(
                    timeout=max(1.0, timeout_ms / 1000.0 + 1.0))
            except subprocess.TimeoutExpired:
                _interrupt_process(process)
                process.communicate()
                return None
            finally:
                self._unregister_process(token)
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            if cancelled.is_set() or process.returncode not in {0, 10, 20}:
                return None
            try:
                status, _ = parse_qfbv_response(stdout, ())
            except ValueError:
                return None
            return stdout if status == "unsat" else None
        except OSError:
            return None
        finally:
            if query_path:
                try:
                    os.unlink(query_path)
                except FileNotFoundError:
                    pass

    def __call__(self, lease: Any) -> Mapping[str, Any]:
        query_id = str(lease.query_id)
        self._begin_query(query_id)
        try:
            return self._solve(lease)
        finally:
            self._end_query(query_id)

    def _solve(self, lease: Any) -> Mapping[str, Any]:
        started = time.monotonic_ns()
        base = self._base_result()
        loaded = self.store.load_query_ir(str(lease.query_id))
        if loaded is None:
            return {
                **base,
                "status": "error",
                "elapsed_us": 0,
                "reason": "Query IR is unavailable",
            }
        roots, expressions = loaded
        try:
            proof_inputs: dict[str, Any] | None = None
            root_terms: tuple[str, ...] = ()
            if self.proof_verifier is None:
                smt2, certificate, offsets = lower_qfbv_query(
                    str(lease.query_id), roots, expressions, self.capabilities
                )
            else:
                (
                    smt2,
                    proof_query_smt2,
                    reference_smt2,
                    certificate,
                    offsets,
                    root_terms,
                    proof_context,
                ) = lower_qfbv_proof_problem(
                    str(lease.query_id), roots, expressions, self.capabilities
                )
                proof_inputs = {
                    "smt2": smt2,
                    "proof_query_smt2": proof_query_smt2,
                    "reference_smt2": reference_smt2,
                    "offsets": offsets,
                    "lowering_certificate_sha256": certificate[
                        "certificate_sha256"
                    ],
                    "context": proof_context,
                }
        except (QfBvLoweringError, ValueError) as error:
            return {
                **base,
                "status": "unknown",
                "elapsed_us": (
                    time.monotonic_ns() - started) // 1000,
                "capability_status": "unsupported",
                "reason": str(error)[:512],
            }
        base["lowering_certificate"] = certificate
        query_path = ""
        timeout_ms = max(1, min(int(lease.timeout_ms), 3600000))
        if proof_inputs is not None:
            reused = self._try_proof_reuse(
                lease,
                base,
                proof_inputs,
                timeout_ms=timeout_ms,
                started_ns=started,
            )
            if reused is not None:
                return reused
            reused_core = self._try_substitution_core_reuse(
                lease,
                base,
                roots,
                expressions,
                timeout_ms=timeout_ms,
                started_ns=started,
            )
            if reused_core is not None:
                return reused_core
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="ascii", suffix=".smt2",
                    delete=False) as output:
                output.write(smt2)
                query_path = output.name
            has_query_placeholder = any(
                "{query}" in item for item in self.command)
            command = [
                item.replace("{query}", query_path).replace(
                    "{timeout_ms}", str(timeout_ms))
                for item in self.command
            ]
            if not has_query_placeholder:
                command.append(query_path)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            token, cancelled = self._register_process(
                str(lease.query_id), process)
            try:
                stdout, stderr = process.communicate(
                    timeout=max(1.0, timeout_ms / 1000.0 + 1.0))
            except subprocess.TimeoutExpired:
                _interrupt_process(process)
                process.communicate()
                return {
                    **base,
                    "status": "unknown",
                    "elapsed_us": (
                        time.monotonic_ns() - started) // 1000,
                    "reason": "SMT-LIB QF_BV process timeout",
                }
            finally:
                self._unregister_process(token)
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            if cancelled.is_set():
                return {
                    **base,
                    "status": "unknown",
                    "elapsed_us": (
                        time.monotonic_ns() - started) // 1000,
                    "cancelled": True,
                    "cancel_reason": "portfolio-sat-winner",
                    "reason": (
                        "SMT-LIB QF_BV solver interrupted after portfolio SAT"),
                }
        except OSError as error:
            return {
                **base,
                "status": "error",
                "elapsed_us": (
                    time.monotonic_ns() - started) // 1000,
                "reason": str(error)[:512],
            }
        finally:
            if query_path:
                try:
                    os.unlink(query_path)
                except FileNotFoundError:
                    pass
        elapsed_us = (time.monotonic_ns() - started) // 1000
        if process.returncode not in {0, 10, 20}:
            try:
                raw_status, _ = parse_qfbv_response(stdout, offsets)
            except ValueError:
                raw_status = ""
            remaining_ms = max(
                0,
                timeout_ms - int(elapsed_us // 1000),
            )
            if raw_status == "unsat" and remaining_ms > 0:
                confirmed = self._confirm_nonzero_unsat(
                    str(lease.query_id),
                    smt2,
                    remaining_ms,
                )
                if confirmed is not None:
                    base["backend_unsat_confirmation"] = (
                        "status-only-rerun-v1"
                    )
                    elapsed_us = (
                        time.monotonic_ns() - started) // 1000
                    return self._publish_substitution_core(
                        self._finalize_response(
                            lease,
                            base,
                            confirmed,
                            offsets,
                            elapsed_us,
                            proof_inputs=proof_inputs,
                            proof_timeout_ms=remaining_ms,
                        ),
                        lease,
                        roots,
                        expressions,
                        root_terms,
                        offsets,
                        started_ns=started,
                        timeout_ms=timeout_ms,
                    )
            diagnostic = (stderr + "\n" + stdout).strip()
            return {
                **base,
                "status": "error",
                "elapsed_us": elapsed_us,
                "reason": (
                    f"SMT-LIB QF_BV solver exited {process.returncode}: "
                    f"{diagnostic[-512:]}"
                ),
            }
        remaining_ms = max(
            0,
            timeout_ms - int(elapsed_us // 1000),
        )
        return self._publish_substitution_core(
            self._finalize_response(
                lease,
                base,
                stdout,
                offsets,
                elapsed_us,
                proof_inputs=proof_inputs,
                proof_timeout_ms=remaining_ms,
            ),
            lease,
            roots,
            expressions,
            root_terms,
            offsets,
            started_ns=started,
            timeout_ms=timeout_ms,
        )


@dataclass
class _IncrementalQfBvContext:
    process: subprocess.Popen[str]
    prefix_digest: str
    declared_offsets: set[int] = field(default_factory=set)
    queries: int = 0
    context_sha256: str = ""
    depth: int = 0
    injected_lemma_sha256: set[str] = field(default_factory=set)
    injected_lemma_records: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )
    native_generation: int = 0
    native_queries: int = 0
    native_warm_checks: int = 0


class PersistentSmtLibQfbvSolver(SmtLibQfbvSolver):
    """LRU pool of interactive SMT-LIB processes keyed by exact prefix."""

    def __init__(
        self,
        store: Any,
        command: Sequence[str],
        *,
        name: str,
        capabilities: Mapping[str, Any],
        prefix_cache_entries: int = 4,
        shared_context_store: CrossWorkerContextStore | None = None,
        context_owner: str = "",
        materialization_lease_seconds: float = 30.0,
        materialization_max_active: int = 64,
        proof_verifier: QfbvProofVerifier | None = None,
        substitution_core_exchange: QfbvSubstitutionCoreExchange | None = None,
        lemma_exchange: QfbvLemmaExchange | None = None,
        learned_literal_type: str = "preprocess",
        max_learned_lemmas: int = 8,
        lemma_timeout_ms: int = 30_000,
        native_state_fork: bool = False,
    ):
        super().__init__(
            store,
            command,
            name=name,
            capabilities=capabilities,
            proof_verifier=proof_verifier,
            substitution_core_exchange=substitution_core_exchange,
        )
        if not self.capabilities["incremental"]:
            raise ValueError(
                "persistent SMT-LIB backend requires incremental capability")
        if any(
                "{query}" in item or "{timeout_ms}" in item
                for item in self.command):
            raise ValueError(
                "persistent SMT-LIB command cannot contain query placeholders")
        if not isinstance(native_state_fork, bool):
            raise ValueError("native_state_fork must be Boolean")
        if native_state_fork and not sys.platform.startswith("linux"):
            raise ValueError("native state fork requires Linux")
        self.native_state_fork = native_state_fork
        self.prefix_cache_entries = _bounded_int(
            prefix_cache_entries, "prefix_cache_entries", 1, 64)
        self._contexts: OrderedDict[str, _IncrementalQfBvContext] = (
            OrderedDict())
        self._sequence = 0
        self._lock = threading.Lock()
        self.shared_context_store = shared_context_store
        self.lemma_exchange = lemma_exchange
        self.learned_literal_type = str(learned_literal_type)
        if self.learned_literal_type not in {
            "preprocess",
            "input",
            "solvable",
            "internal",
        }:
            raise ValueError("unsupported cvc5 learned literal type")
        self.max_learned_lemmas = _bounded_int(
            max_learned_lemmas,
            "max_learned_lemmas",
            1,
            64,
        )
        self.lemma_timeout_ms = _bounded_int(
            lemma_timeout_ms,
            "lemma_timeout_ms",
            1,
            3_600_000,
        )
        if self.lemma_exchange is not None and (
            self.shared_context_store is None or self.proof_verifier is None
        ):
            raise ValueError(
                "lemma exchange requires shared context and proof verifier"
            )
        if self.native_state_fork and self.lemma_exchange is not None:
            raise ValueError(
                "native state fork cannot publish cvc5 learned literals"
            )
        self.context_owner = (
            str(context_owner)[:256]
            or f"pid-{os.getpid()}-backend-{id(self):x}"
        )
        if "\x00" in self.context_owner:
            raise ValueError("context_owner contains NUL")
        self.materialization_lease_seconds = float(
            materialization_lease_seconds
        )
        if not 0.1 <= self.materialization_lease_seconds <= 3600.0:
            raise ValueError(
                "materialization_lease_seconds must be in [0.1, 3600]"
            )
        self.materialization_max_active = _bounded_int(
            materialization_max_active,
            "materialization_max_active",
            1,
            4096,
        )
        if (
            self.shared_context_store is not None
            and self.shared_context_store.max_active_materializations
            != self.materialization_max_active
        ):
            raise ValueError(
                "backend materialization quota disagrees with shared store"
            )
        self._materialization_lock = threading.Lock()
        self._active_materializations: dict[str, MaterializationLease] = {}

    @staticmethod
    def _response_error(output: str) -> str:
        try:
            forms = _parse_s_expressions(output)
        except ValueError as error:
            return str(error)
        for form in forms:
            if (isinstance(form, list) and form
                    and form[0] == "error"):
                return " ".join(str(item) for item in form[1:])[:512]
            if isinstance(form, str) and form not in {"success"}:
                return f"unexpected initialization response {form!r}"
        return ""

    def _validate_native_response(
        self,
        context: _IncrementalQfBvContext,
        output: str,
    ) -> dict[str, Any]:
        metadata = parse_native_state_fork_metadata(output)
        generation = int(metadata["backend_native_snapshot_generation"])
        queries = int(metadata["backend_native_snapshot_queries"])
        warm_checks = int(metadata["backend_native_warm_checks"])
        expected_queries = context.native_queries + 1
        if queries != expected_queries:
            raise ValueError(
                "native snapshot query sequence is not monotonic"
            )
        if generation < context.native_generation:
            raise ValueError("native snapshot generation regressed")
        if warm_checks < context.native_warm_checks:
            raise ValueError("native warm-check sequence regressed")
        context.native_generation = generation
        context.native_queries = queries
        context.native_warm_checks = warm_checks
        return metadata

    @staticmethod
    def _close_context(context: _IncrementalQfBvContext) -> None:
        process = context.process
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write("(exit)\n")
                    process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def _drop_context(self, cache_key: str) -> None:
        context = self._contexts.pop(cache_key, None)
        if context is not None and (
            context.process.poll() is None
            or any(
                stream is not None and not stream.closed
                for stream in (
                    context.process.stdin,
                    context.process.stdout,
                    context.process.stderr,
                )
            )
        ):
            self._close_context(context)

    def cancel(self, lease: Any) -> bool:
        interrupted = super().cancel(lease)
        query_id = str(lease.query_id)
        with self._materialization_lock:
            materialization = self._active_materializations.get(query_id)
        if self.shared_context_store is not None and materialization is not None:
            interrupted = (
                self.shared_context_store.cancel_materialization(materialization)
                or interrupted
            )
        return interrupted

    def _claim_materialization(
        self,
        query_id: str,
        context_sha256: str,
        *,
        timeout_ms: int,
    ) -> MaterializationLease | None:
        if self.shared_context_store is None:
            return None
        deadline = time.monotonic() + max(0.001, timeout_ms / 1000.0)
        while True:
            lease = self.shared_context_store.claim_materialization(
                context_sha256,
                f"{self.context_owner}:{query_id[:64]}",
                lease_seconds=self.materialization_lease_seconds,
                max_active=self.materialization_max_active,
            )
            if lease is not None:
                with self._materialization_lock:
                    self._active_materializations[query_id] = lease
                return lease
            with self._active_lock:
                cancelled = query_id in self._pending_cancellations
            if cancelled:
                raise _BackendCancelled(
                    "shared context materialization cancelled"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.005, remaining))

    def _release_materialization(
        self,
        query_id: str,
        lease: MaterializationLease | None,
    ) -> None:
        if lease is None or self.shared_context_store is None:
            return
        with self._materialization_lock:
            self._active_materializations.pop(query_id, None)
        self.shared_context_store.release_materialization(lease)

    def _marker(self, query_id: str) -> str:
        self._sequence += 1
        return (
            f"SYMCC_QFBV_{self._sequence}_"
            f"{str(query_id)[:16].upper()}"
        )

    def _request(
        self,
        context: _IncrementalQfBvContext,
        script: str,
        *,
        query_id: str,
        timeout_ms: int,
    ) -> str:
        process = context.process
        if process.poll() is not None:
            raise RuntimeError(
                f"incremental solver exited {process.returncode}")
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("incremental solver pipes are unavailable")
        marker = self._marker(query_id)
        process.stdin.write(script)
        if script and not script.endswith("\n"):
            process.stdin.write("\n")
        process.stdin.write(f'(echo "{marker}")\n')
        process.stdin.flush()
        token, cancelled = self._register_process(query_id, process)
        response: Queue[tuple[bool, str]] = Queue(maxsize=1)

        def read_response() -> None:
            lines: list[str] = []
            response_size = 0
            try:
                while True:
                    line = process.stdout.readline()
                    if not line:
                        response.put((
                            False,
                            f"incremental solver closed output "
                            f"(returncode={process.poll()})",
                        ))
                        return
                    if line.strip().strip('"') == marker:
                        response.put((True, "".join(lines)))
                        return
                    lines.append(line)
                    response_size += len(line)
                    if response_size > 8 * 1024 * 1024:
                        response.put((
                            False,
                            "incremental solver response exceeds 8 MiB",
                        ))
                        return
            except (OSError, ValueError) as error:
                response.put((False, str(error)[:512]))

        reader = threading.Thread(target=read_response, daemon=True)
        reader.start()
        try:
            succeeded, output = response.get(
                timeout=max(1.0, timeout_ms / 1000.0 + 1.0))
        except Empty as error:
            self._close_context(context)
            reader.join(timeout=1.0)
            raise TimeoutError(
                "incremental SMT-LIB request timeout") from error
        finally:
            self._unregister_process(token)
        reader.join(timeout=1.0)
        if cancelled.is_set():
            raise _BackendCancelled(
                "incremental SMT-LIB request cancelled after portfolio SAT")
        if not succeeded:
            raise RuntimeError(output)
        return output

    def _create_context(
        self,
        cache_key: str,
        prefix_terms: Sequence[str],
        offsets: Sequence[int],
        *,
        query_id: str,
        timeout_ms: int,
        context_sha256: str = "",
    ) -> _IncrementalQfBvContext:
        try:
            process = subprocess.Popen(
                list(self.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as error:
            raise RuntimeError(str(error)[:512]) from error
        prefix_digest = _digest(_canonical_json(tuple(prefix_terms)))
        context = _IncrementalQfBvContext(
            process=process,
            prefix_digest=prefix_digest,
            declared_offsets=set(int(offset) for offset in offsets),
            context_sha256=context_sha256,
            depth=len(prefix_terms),
        )
        declarations = "\n".join(
            f"(declare-fun symcc_input_{int(offset)} () (_ BitVec 8))"
            for offset in offsets
        )
        assertions = "\n".join(
            f"(assert {term})" for term in prefix_terms)
        initialization = "".join(
            (
                "(set-logic QF_BV)\n",
                "(set-option :produce-models true)\n",
                f"(set-option :timeout {timeout_ms})\n"
                if self.native_state_fork
                else "",
                "(set-option :produce-learned-literals true)\n"
                if self.lemma_exchange is not None
                else "",
                f"{declarations}\n{assertions}\n",
            )
        )
        try:
            output = self._request(
                context,
                initialization,
                query_id=query_id,
                timeout_ms=timeout_ms,
            )
            error = self._response_error(output)
            if error:
                raise RuntimeError(error)
        except Exception:
            self._close_context(context)
            raise
        self._contexts[cache_key] = context
        self._contexts.move_to_end(cache_key)
        while len(self._contexts) > self.prefix_cache_entries:
            _old_key, old_context = self._contexts.popitem(last=False)
            self._close_context(old_context)
        return context

    def _extend_parent_context(
        self,
        parent_key: str,
        cache_key: str,
        plan: ContextPlan,
        *,
        query_id: str,
        timeout_ms: int,
    ) -> _IncrementalQfBvContext | None:
        context = self._contexts.get(parent_key)
        if context is None or plan.depth < 1:
            return None
        parent_terms = plan.terms[:-1]
        parent_digest = _digest(_canonical_json(tuple(parent_terms)))
        if (
            context.prefix_digest != parent_digest
            or context.context_sha256 != plan.parent_context_sha256
            or context.depth != plan.depth - 1
        ):
            self._drop_context(parent_key)
            return None
        new_offsets = sorted(set(plan.offsets) - context.declared_offsets)
        script_rows = [
            f"(declare-fun symcc_input_{offset} () (_ BitVec 8))"
            for offset in new_offsets
        ]
        script_rows.append(f"(assert {plan.terms[-1]})")
        try:
            output = self._request(
                context,
                "\n".join(script_rows),
                query_id=query_id,
                timeout_ms=timeout_ms,
            )
            error = self._response_error(output)
            if error:
                raise RuntimeError(error)
        except Exception:
            self._drop_context(parent_key)
            raise
        self._contexts.pop(parent_key, None)
        context.prefix_digest = _digest(_canonical_json(tuple(plan.terms)))
        context.declared_offsets.update(new_offsets)
        context.context_sha256 = plan.context_sha256
        context.depth = plan.depth
        self._contexts[cache_key] = context
        self._contexts.move_to_end(cache_key)
        return context

    @staticmethod
    def _remaining_query_ms(
        started_ns: int,
        timeout_ms: int,
    ) -> int:
        elapsed_ms = (time.monotonic_ns() - started_ns) // 1_000_000
        return max(0, int(timeout_ms) - int(elapsed_ms))

    def _inject_verified_lemmas(
        self,
        context: _IncrementalQfBvContext,
        plan: ContextPlan,
        base: dict[str, Any],
        *,
        query_id: str,
        started_ns: int,
        timeout_ms: int,
    ) -> None:
        if self.lemma_exchange is None:
            return
        base.update(
            {
                "backend_lemma_protocol": QFBV_LEMMA_PROTOCOL,
                "backend_lemma_exchange_policy_sha256": (
                    self.lemma_exchange.policy_sha256
                ),
                "backend_lemma_candidates": 0,
                "backend_lemma_injected": 0,
                "backend_lemma_active": len(
                    context.injected_lemma_sha256
                ),
                "backend_lemma_rejected": 0,
                "backend_lemma_checker_elapsed_us": 0,
                "backend_verified_lemma_records": list(
                    context.injected_lemma_records.values()
                ),
            }
        )
        remaining = self._remaining_query_ms(started_ns, timeout_ms)
        if remaining <= 0:
            base["backend_lemma_rejected"] = 1
            base["backend_lemma_reason"] = "query deadline expired before lemma lookup"
            return
        try:
            authorizations, rejected = self.lemma_exchange.applicable(
                plan.context_sha256,
                limit=self.max_learned_lemmas,
                timeout_ms=min(remaining, self.lemma_timeout_ms),
                register_process=lambda process: self._register_process(
                    query_id, process
                ),
                unregister_process=self._unregister_process,
            )
        except (OSError, sqlite3.Error, LemmaExchangeError) as error:
            base["backend_lemma_rejected"] = 1
            base["backend_lemma_reason"] = str(error)[:512]
            return
        base["backend_lemma_candidates"] = len(authorizations) + rejected
        base["backend_lemma_rejected"] = rejected
        base["backend_lemma_checker_elapsed_us"] = sum(
            authorization.checker_elapsed_us
            for authorization in authorizations
        )
        pending = [
            authorization.record
            for authorization in authorizations
            if authorization.record["lemma_sha256"]
            not in context.injected_lemma_sha256
        ]
        capacity = max(0, 64 - len(context.injected_lemma_sha256))
        if len(pending) > capacity:
            base["backend_lemma_rejected"] += len(pending) - capacity
            pending = pending[:capacity]
        if pending:
            remaining = self._remaining_query_ms(started_ns, timeout_ms)
            if remaining <= 0:
                base["backend_lemma_rejected"] += len(pending)
                base["backend_lemma_reason"] = (
                    "query deadline expired before lemma injection"
                )
                return
            script = "\n".join(
                f"(assert {record['lemma']})" for record in pending
            )
            output = self._request(
                context,
                script,
                query_id=query_id,
                timeout_ms=remaining,
            )
            error = self._response_error(output)
            if error:
                raise RuntimeError(
                    f"verified lemma injection failed: {error}"
                )
            for record in pending:
                digest = str(record["lemma_sha256"])
                context.injected_lemma_sha256.add(digest)
                context.injected_lemma_records[digest] = dict(record)
        base["backend_lemma_injected"] = len(pending)
        base["backend_lemma_active"] = len(context.injected_lemma_sha256)
        base["backend_verified_lemma_records"] = [
            context.injected_lemma_records[digest]
            for digest in sorted(context.injected_lemma_records)
        ]

    def _publish_learned_lemmas(
        self,
        result: dict[str, Any],
        roots: Sequence[str],
        root_terms: Sequence[str],
        offsets: Sequence[int],
        *,
        query_id: str,
        started_ns: int,
        timeout_ms: int,
    ) -> None:
        if (
            self.lemma_exchange is None
            or self.shared_context_store is None
            or result.get("status") != "sat"
        ):
            return
        result.update(
            {
                "backend_lemma_publish_candidates": 0,
                "backend_lemma_published": 0,
                "backend_lemma_publish_rejected": 0,
                "backend_lemma_proof_reuses": 0,
                "backend_published_lemma_record_sha256": [],
            }
        )
        try:
            remaining = self._remaining_query_ms(started_ns, timeout_ms)
            if remaining <= 0:
                raise LemmaExchangeError(
                    "query deadline expired before learned lemma extraction"
                )
            lemmas, extractor_elapsed_us = self._extract_learned_literals(
                root_terms,
                offsets,
                query_id=query_id,
                timeout_ms=min(remaining, self.lemma_timeout_ms),
            )
            result["backend_lemma_extractor_elapsed_us"] = (
                extractor_elapsed_us
            )
        except (OSError, LemmaExchangeError) as error:
            result["backend_lemma_publish_reason"] = str(error)[:512]
            return
        result["backend_lemma_publish_candidates"] = len(lemmas)
        if not lemmas:
            return
        try:
            publication = self.shared_context_store.publish_chain(
                tuple(str(root) for root in roots),
                tuple(str(term) for term in root_terms),
                capability_sha256=str(
                    self.capabilities["capability_sha256"]
                ),
            )
            if publication is None:
                raise LemmaExchangeError("learned lemma source context is empty")
            source = self.shared_context_store.resolve(
                publication.context_sha256,
                expected_capability_sha256=str(
                    self.capabilities["capability_sha256"]
                ),
            )
            if (
                source.root_hashes != tuple(str(root) for root in roots)
                or source.terms != tuple(str(term) for term in root_terms)
            ):
                raise LemmaExchangeError(
                    "learned lemma source context disagrees with Query IR"
                )
        except (OSError, sqlite3.Error, ValueError) as error:
            result["backend_lemma_publish_rejected"] = len(lemmas)
            result["backend_lemma_publish_reason"] = str(error)[:512]
            return
        result["backend_lemma_source_context_sha256"] = (
            source.context_sha256
        )
        published: list[str] = []
        rejected = 0
        proof_reuses = 0
        for lemma in lemmas:
            remaining = self._remaining_query_ms(started_ns, timeout_ms)
            if remaining <= 0:
                rejected += 1
                continue
            try:
                authorization = self.lemma_exchange.certify_and_publish(
                    source.context_sha256,
                    lemma,
                    category=self.learned_literal_type,
                    timeout_ms=min(remaining, self.lemma_timeout_ms),
                    register_process=lambda process: self._register_process(
                        query_id, process
                    ),
                    unregister_process=self._unregister_process,
                )
            except (
                FileNotFoundError,
                OSError,
                sqlite3.Error,
                LemmaExchangeError,
            ):
                rejected += 1
                continue
            published.append(str(authorization.record["record_sha256"]))
            proof_reuses += int(authorization.proof_reused)
        result["backend_lemma_published"] = len(published)
        result["backend_lemma_publish_rejected"] = rejected
        result["backend_lemma_proof_reuses"] = proof_reuses
        result["backend_published_lemma_record_sha256"] = published

    def _extract_learned_literals(
        self,
        root_terms: Sequence[str],
        offsets: Sequence[int],
        *,
        query_id: str,
        timeout_ms: int,
    ) -> tuple[tuple[str, ...], int]:
        declarations = "\n".join(
            f"(declare-fun symcc_input_{int(offset)} () (_ BitVec 8))"
            for offset in offsets
        )
        assertions = "\n".join(
            f"(assert {term})" for term in root_terms
        )
        script = (
            "(set-logic QF_BV)\n"
            "(set-option :produce-learned-literals true)\n"
            f"{declarations}\n{assertions}\n"
            "(check-sat)\n"
            f"(get-learned-literals :{self.learned_literal_type})\n"
            "(exit)\n"
        )
        started = time.monotonic_ns()
        with tempfile.TemporaryDirectory(
            prefix="symcc-qfbv-lemma-extract-"
        ) as directory:
            query_path = os.path.join(directory, "query.smt2")
            with open(query_path, "w", encoding="ascii") as output:
                output.write(script)
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                try:
                    process = subprocess.Popen(
                        [*self.command, query_path],
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        start_new_session=True,
                    )
                except OSError as error:
                    raise LemmaExchangeError(
                        f"learned lemma extractor failed to start: {error}"
                    ) from error
                token, cancelled = self._register_process(query_id, process)
                try:
                    try:
                        process.wait(timeout=max(0.001, timeout_ms / 1000.0))
                    except subprocess.TimeoutExpired as error:
                        _interrupt_process(process)
                        process.wait()
                        raise LemmaExchangeError(
                            "learned lemma extractor timeout"
                        ) from error
                finally:
                    self._unregister_process(token)
                if cancelled.is_set():
                    raise LemmaExchangeError(
                        "learned lemma extractor was cancelled"
                    )
                if (
                    stdout_file.tell() > 8 * 1024 * 1024
                    or stderr_file.tell() > 64 * 1024
                ):
                    raise LemmaExchangeError(
                        "learned lemma extractor output exceeds its bound"
                    )
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read(8 * 1024 * 1024 + 1)
                stderr = stderr_file.read(64 * 1024 + 1)
                if process.returncode not in {0, 10} or stderr.strip():
                    diagnostic = (stderr + b"\n" + stdout)[-512:]
                    raise LemmaExchangeError(
                        "learned lemma extractor failed: "
                        + diagnostic.decode("utf-8", errors="replace")
                    )
        try:
            text = stdout.decode("ascii")
        except UnicodeDecodeError as error:
            raise LemmaExchangeError(
                "learned lemma extractor output is not ASCII"
            ) from error
        lemmas = parse_learned_literal_response(
            text,
            allowed_offsets=offsets,
            max_lemmas=self.max_learned_lemmas,
        )
        return lemmas, (time.monotonic_ns() - started) // 1000

    def __call__(self, lease: Any) -> Mapping[str, Any]:
        query_id = str(lease.query_id)
        self._begin_query(query_id)
        try:
            return self._solve_persistent(lease)
        finally:
            self._end_query(query_id)

    def _solve_persistent(self, lease: Any) -> Mapping[str, Any]:
        started = time.monotonic_ns()
        base = self._base_result()
        loaded = self.store.load_query_ir(str(lease.query_id))
        if loaded is None:
            return {
                **base,
                "status": "error",
                "elapsed_us": 0,
                "reason": "Query IR is unavailable",
            }
        roots, expressions = loaded
        try:
            proof_inputs: dict[str, Any] | None = None
            if self.proof_verifier is None:
                smt2, certificate, offsets, root_terms = (
                    _lower_qfbv_query_plan(
                        str(lease.query_id),
                        roots,
                        expressions,
                        self.capabilities,
                    )
                )
            else:
                (
                    smt2,
                    proof_query_smt2,
                    reference_smt2,
                    certificate,
                    offsets,
                    root_terms,
                    proof_context,
                ) = lower_qfbv_proof_problem(
                    str(lease.query_id),
                    roots,
                    expressions,
                    self.capabilities,
                )
                proof_inputs = {
                    "smt2": smt2,
                    "proof_query_smt2": proof_query_smt2,
                    "reference_smt2": reference_smt2,
                    "offsets": offsets,
                    "lowering_certificate_sha256": certificate[
                        "certificate_sha256"
                    ],
                    "context": proof_context,
                }
        except (QfBvLoweringError, ValueError) as error:
            return {
                **base,
                "status": "unknown",
                "elapsed_us": (
                    time.monotonic_ns() - started) // 1000,
                "capability_status": "unsupported",
                "reason": str(error)[:512],
            }
        base["lowering_certificate"] = certificate
        base["backend_context_protocol"] = (
            "smtlib-prefix-process-push-pop-v1")
        if self.native_state_fork:
            base["backend_native_state_protocol"] = (
                NATIVE_STATE_FORK_PROTOCOL
            )
        timeout_ms = max(1, min(int(lease.timeout_ms), 3600000))
        if proof_inputs is not None:
            reused = self._try_proof_reuse(
                lease,
                base,
                proof_inputs,
                timeout_ms=timeout_ms,
                started_ns=started,
            )
            if reused is not None:
                return reused
            reused_core = self._try_substitution_core_reuse(
                lease,
                base,
                roots,
                expressions,
                timeout_ms=timeout_ms,
                started_ns=started,
            )
            if reused_core is not None:
                return reused_core
        prefix_roots = tuple(str(root) for root in roots[:-1])
        prefix_terms = tuple(root_terms[:-1])
        target_term = root_terms[-1]
        shared_plan: ContextPlan | None = None
        publication = None
        if self.shared_context_store is not None and prefix_terms:
            try:
                publication = self.shared_context_store.publish_chain(
                    prefix_roots,
                    prefix_terms,
                    capability_sha256=str(
                        self.capabilities["capability_sha256"]
                    ),
                )
                if publication is None:
                    raise ValueError("shared context publication is empty")
                shared_plan = self.shared_context_store.resolve(
                    publication.context_sha256,
                    expected_capability_sha256=str(
                        self.capabilities["capability_sha256"]
                    ),
                )
                if (
                    shared_plan.root_hashes != prefix_roots
                    or shared_plan.terms != prefix_terms
                ):
                    raise ValueError(
                        "shared context does not match current Query IR lowering"
                    )
                base.update({
                    "backend_context_protocol": CROSS_WORKER_CONTEXT_PROTOCOL,
                    "backend_shared_context_sha256": (
                        shared_plan.context_sha256
                    ),
                    "backend_shared_parent_context_sha256": (
                        shared_plan.parent_context_sha256
                    ),
                    "backend_shared_context_exact_hit": publication.exact_hit,
                    "backend_shared_context_created": publication.created_count,
                    "backend_shared_context_existing": publication.existing_count,
                    "backend_shared_context_depth": shared_plan.depth,
                    "backend_shared_context_materialization": "unmaterialized",
                    "backend_parent_context_reused": False,
                })
                prefix_terms = shared_plan.terms
            except (OSError, sqlite3.Error, ValueError) as error:
                return {
                    **base,
                    "status": "error",
                    "elapsed_us": (
                        time.monotonic_ns() - started
                    ) // 1000,
                    "reason": f"shared context verification failed: {error}"[:512],
                }
        prefix_digest = _digest(_canonical_json(tuple(prefix_terms)))
        cache_key = (
            shared_plan.context_sha256
            if shared_plan is not None
            else str(lease.prefix_key)
        )
        with self._lock:
            context = self._contexts.get(cache_key)
            cache_hit = context is not None
            if context is not None and context.prefix_digest != prefix_digest:
                self._drop_context(cache_key)
                return {
                    **base,
                    "status": "error",
                    "elapsed_us": (
                        time.monotonic_ns() - started) // 1000,
                    "reason": "prefix key resolved to different Query IR",
                }
            try:
                if context is None:
                    materialization = (
                        self._claim_materialization(
                            str(lease.query_id),
                            shared_plan.context_sha256,
                            timeout_ms=timeout_ms,
                        )
                        if shared_plan is not None
                        else None
                    )
                    base["backend_shared_context_materialization"] = (
                        "leased"
                        if materialization is not None
                        else (
                            "quota-timeout"
                            if shared_plan is not None
                            else "disabled"
                        )
                    )
                    if shared_plan is not None and materialization is None:
                        return {
                            **base,
                            "status": "unknown",
                            "prefix_cache_hit": False,
                            "prefix_cache_entries": len(self._contexts),
                            "elapsed_us": (
                                time.monotonic_ns() - started
                            ) // 1000,
                            "reason": (
                                "shared context materialization quota timeout"
                            ),
                        }
                    try:
                        context = None
                        if (
                            materialization is not None
                            and shared_plan is not None
                            and shared_plan.parent_context_sha256
                        ):
                            context = self._extend_parent_context(
                                shared_plan.parent_context_sha256,
                                cache_key,
                                shared_plan,
                                query_id=str(lease.query_id),
                                timeout_ms=timeout_ms,
                            )
                        base["backend_parent_context_reused"] = context is not None
                        if context is None:
                            context = self._create_context(
                                cache_key,
                                prefix_terms,
                                (
                                    shared_plan.offsets
                                    if shared_plan is not None
                                    else offsets
                                ),
                                query_id=str(lease.query_id),
                                timeout_ms=timeout_ms,
                                context_sha256=(
                                    shared_plan.context_sha256
                                    if shared_plan is not None
                                    else ""
                                ),
                            )
                    finally:
                        self._release_materialization(
                            str(lease.query_id), materialization
                        )
                else:
                    self._contexts.move_to_end(cache_key)
                    base["backend_shared_context_materialization"] = (
                        "local-hit" if shared_plan is not None else "disabled"
                    )
                    base["backend_parent_context_reused"] = False
                new_offsets = sorted(
                    set(int(offset) for offset in offsets)
                    - context.declared_offsets
                )
                if new_offsets:
                    declaration_script = "\n".join(
                        f"(declare-fun symcc_input_{offset} "
                        "() (_ BitVec 8))"
                        for offset in new_offsets
                    )
                    declaration_output = self._request(
                        context,
                        declaration_script,
                        query_id=str(lease.query_id),
                        timeout_ms=timeout_ms,
                    )
                    declaration_error = self._response_error(
                        declaration_output)
                    if declaration_error:
                        raise RuntimeError(declaration_error)
                    context.declared_offsets.update(new_offsets)
                if shared_plan is not None:
                    self._inject_verified_lemmas(
                        context,
                        shared_plan,
                        base,
                        query_id=str(lease.query_id),
                        started_ns=started,
                        timeout_ms=timeout_ms,
                    )
                elif self.lemma_exchange is not None:
                    base.update(
                        {
                            "backend_lemma_protocol": QFBV_LEMMA_PROTOCOL,
                            "backend_lemma_exchange_policy_sha256": (
                                self.lemma_exchange.policy_sha256
                            ),
                            "backend_lemma_candidates": 0,
                            "backend_lemma_injected": 0,
                            "backend_lemma_active": 0,
                            "backend_lemma_rejected": 0,
                            "backend_lemma_checker_elapsed_us": 0,
                            "backend_verified_lemma_records": [],
                        }
                    )
                symbols = " ".join(
                    f"symcc_input_{int(offset)}" for offset in offsets)
                model_command = (
                    f"(get-value ({symbols}))\n" if offsets else "")
                request_timeout_ms = timeout_ms
                if self.lemma_exchange is not None:
                    request_timeout_ms = self._remaining_query_ms(
                        started,
                        timeout_ms,
                    )
                    if request_timeout_ms <= 0:
                        raise TimeoutError(
                            "incremental SMT-LIB query timeout: deadline expired"
                        )
                timeout_command = (
                    f"(set-option :timeout {request_timeout_ms})\n"
                    if self.native_state_fork
                    else ""
                )
                query_script = (
                    f"{timeout_command}"
                    "(push 1)\n"
                    f"(assert {target_term})\n"
                    "(check-sat)\n"
                    f"{model_command}"
                    "(pop 1)\n"
                )
                output = self._request(
                    context,
                    query_script,
                    query_id=str(lease.query_id),
                    timeout_ms=request_timeout_ms,
                )
                if self.native_state_fork:
                    base.update(
                        self._validate_native_response(context, output)
                    )
                    # Validate the paired status/model before retaining the
                    # native snapshot; finalization independently repeats it.
                    parse_qfbv_response(output, offsets)
                context.queries += 1
            except TimeoutError as error:
                # A timed-out interactive process may still own three pipe
                # descriptors and a live child. Eviction must terminate and
                # close it before the cold retry creates another context.
                self._drop_context(cache_key)
                return {
                    **base,
                    "status": "unknown",
                    "prefix_cache_hit": cache_hit,
                    "prefix_cache_entries": len(self._contexts),
                    "elapsed_us": (
                        time.monotonic_ns() - started) // 1000,
                    "reason": str(error)[:512],
                }
            except _BackendCancelled as error:
                self._drop_context(cache_key)
                return {
                    **base,
                    "status": "unknown",
                    "prefix_cache_hit": cache_hit,
                    "prefix_cache_entries": len(self._contexts),
                    "elapsed_us": (
                        time.monotonic_ns() - started) // 1000,
                    "cancelled": True,
                    "cancel_reason": "portfolio-sat-winner",
                    "reason": str(error)[:512],
                }
            except (BrokenPipeError, OSError, RuntimeError, ValueError) as error:
                self._drop_context(cache_key)
                return {
                    **base,
                    "status": "error",
                    "prefix_cache_hit": cache_hit,
                    "prefix_cache_entries": len(self._contexts),
                    "elapsed_us": (
                        time.monotonic_ns() - started) // 1000,
                    "reason": str(error)[:512],
                }
            base["prefix_cache_hit"] = cache_hit
            base["prefix_cache_entries"] = len(self._contexts)
            elapsed_us = (time.monotonic_ns() - started) // 1000
            remaining_ms = max(
                0,
                timeout_ms - int(elapsed_us // 1000),
            )
            finalized = dict(
                self._finalize_response(
                lease,
                base,
                output,
                offsets,
                elapsed_us,
                proof_inputs=proof_inputs,
                proof_timeout_ms=remaining_ms,
                )
            )
            finalized = self._publish_substitution_core(
                finalized,
                lease,
                roots,
                expressions,
                root_terms,
                offsets,
                started_ns=started,
                timeout_ms=timeout_ms,
            )
            self._publish_learned_lemmas(
                finalized,
                roots,
                root_terms,
                offsets,
                query_id=str(lease.query_id),
                started_ns=started,
                timeout_ms=timeout_ms,
            )
            finalized["elapsed_us"] = (
                time.monotonic_ns() - started
            ) // 1000
            return finalized

    def close(self) -> None:
        with self._lock:
            contexts = list(self._contexts.values())
            self._contexts.clear()
        for context in contexts:
            self._close_context(context)

    def __enter__(self) -> "PersistentSmtLibQfbvSolver":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
