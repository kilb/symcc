#!/usr/bin/env python3
# RUN: %python %s --help >/dev/null
"""Independent randomized oracle for F432 bit-blasting and LRUP lifting."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import tempfile
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qf_bv_backend import (  # noqa: E402
    lower_qfbv_query,
    normalize_qfbv_capabilities,
    parse_qfbv_response,
)
from qf_bv_conformance import (  # noqa: E402
    build_operator_matrix_envelope,
    build_unsat_probe_envelope,
)
from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    lift_ascii_lrat_proof,
)
from qfbv_incremental_sat import (  # noqa: E402
    bitblast_qfbv_query,
    parse_dimacs_model,
)
from query_store import QueryStore  # noqa: E402


SCHEMA = "symcc-f432-bitblast-oracle-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _signed(value: int, width: int = 8) -> int:
    return value - (1 << width) if value & (1 << (width - 1)) else value


def _expected(op: str, left: int, right: int, width: int = 8) -> int | bool:
    mask = (1 << width) - 1
    left &= mask
    right &= mask
    signed_left, signed_right = _signed(left, width), _signed(right, width)
    if op == "add":
        return (left + right) & mask
    if op == "sub":
        return (left - right) & mask
    if op == "mul":
        return (left * right) & mask
    if op == "udiv":
        return mask if right == 0 else left // right
    if op == "urem":
        return left if right == 0 else left % right
    if op == "sdiv":
        if right == 0:
            return 1 if signed_left < 0 else mask
        quotient = abs(signed_left) // abs(signed_right)
        if (signed_left < 0) != (signed_right < 0):
            quotient = -quotient
        return quotient & mask
    if op == "srem":
        if right == 0:
            return left
        quotient = abs(signed_left) // abs(signed_right)
        if (signed_left < 0) != (signed_right < 0):
            quotient = -quotient
        return (signed_left - quotient * signed_right) & mask
    if op == "and":
        return left & right
    if op == "or":
        return left | right
    if op == "xor":
        return left ^ right
    if op == "shl":
        return 0 if right >= width else (left << right) & mask
    if op == "lshr":
        return 0 if right >= width else left >> right
    if op == "ashr":
        return (mask if signed_left < 0 else 0) if right >= width else (
            signed_left >> right
        ) & mask
    if op == "rol":
        shift = right % width
        return ((left << shift) | (left >> ((width - shift) % width))) & mask
    if op == "ror":
        shift = right % width
        return ((left >> shift) | (left << ((width - shift) % width))) & mask
    if op == "ult":
        return left < right
    if op == "ule":
        return left <= right
    if op == "ugt":
        return left > right
    if op == "uge":
        return left >= right
    if op == "slt":
        return signed_left < signed_right
    if op == "sle":
        return signed_left <= signed_right
    if op == "sgt":
        return signed_left > signed_right
    if op == "sge":
        return signed_left >= signed_right
    raise AssertionError(op)


def _case_plan(op: str, left: int, right: int):
    expected = _expected(op, left, right)
    expressions = {
        "left": {"op": "read", "bits": 8, "children": [], "attrs": {"index": 0}},
        "right": {"op": "read", "bits": 8, "children": [], "attrs": {"index": 1}},
        "left-value": {
            "op": "constant", "bits": 8, "children": [],
            "attrs": {"value_hex": f"{left:02x}"},
        },
        "right-value": {
            "op": "constant", "bits": 8, "children": [],
            "attrs": {"value_hex": f"{right:02x}"},
        },
        "fix-left": {
            "op": "equal", "bits": 1,
            "children": ["left", "left-value"], "attrs": {},
        },
        "fix-right": {
            "op": "equal", "bits": 1,
            "children": ["right", "right-value"], "attrs": {},
        },
        "operation": {
            "op": op,
            "bits": 1 if isinstance(expected, bool) else 8,
            "children": ["left", "right"],
            "attrs": {},
        },
    }
    if isinstance(expected, bool):
        if expected:
            root = "operation"
        else:
            expressions["root"] = {
                "op": "lnot", "bits": 1, "children": ["operation"], "attrs": {}
            }
            root = "root"
    else:
        expressions["expected"] = {
            "op": "constant", "bits": 8, "children": [],
            "attrs": {"value_hex": f"{expected:02x}"},
        }
        expressions["root"] = {
            "op": "equal", "bits": 1,
            "children": ["operation", "expected"], "attrs": {},
        }
        root = "root"
    return bitblast_qfbv_query(
        f"oracle-{op}-{left}-{right}",
        ["fix-left", "fix-right", root],
        expressions,
    )


def _run_cadical(binary: Path, dimacs: str, proof: Path | None = None):
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", encoding="ascii") as cnf:
        cnf.write(dimacs)
        cnf.flush()
        command = [str(binary)]
        if proof is not None:
            command += ["--plain", "--lrat", "--no-binary"]
        command.append(cnf.name)
        if proof is not None:
            command.append(str(proof))
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30.0,
            check=False,
        )
    status, model = parse_dimacs_model(completed.stdout)
    if completed.returncode not in {10, 20}:
        raise RuntimeError(
            f"CaDiCaL exited {completed.returncode}: {completed.stderr[-512:]}"
        )
    return status, model


def run(cadical: Path, cvc5: Path, *, cases: int, seed: int) -> dict:
    started = time.monotonic_ns()
    version = subprocess.run(
        [str(cadical), "--version"], text=True, capture_output=True,
        timeout=10.0, check=True,
    ).stdout.strip()
    rng = random.Random(seed)
    operators = (
        "add", "sub", "mul", "udiv", "urem", "sdiv", "srem",
        "and", "or", "xor", "shl", "lshr", "ashr", "rol", "ror",
        "ult", "ule", "ugt", "uge", "slt", "sle", "sgt", "sge",
    )
    mismatches = []
    counts = {operator: 0 for operator in operators}
    for index in range(cases):
        operator = operators[index % len(operators)]
        left = rng.randrange(256)
        right = rng.randrange(256)
        if index < 16:
            left, right = (
                (0x80, 0), (0x80, 0xff), (0xff, 0), (0, 0),
                (0x7f, 0x80), (1, 8), (0x81, 255), (0xff, 1),
            )[index % 8]
        plan = _case_plan(operator, left, right)
        status, model = _run_cadical(
            cadical, plan.dimacs(assumptions_as_units=True)
        )
        assignments = plan.input_bytes_from_model(model) if status == "sat" else {}
        if status != "sat" or assignments != {0: left, 1: right}:
            mismatches.append({
                "case": index,
                "operator": operator,
                "left": left,
                "right": right,
                "status": status,
                "assignments": assignments,
            })
        counts[operator] += 1

    with tempfile.TemporaryDirectory(prefix="symcc-f432-oracle-") as directory:
        store = QueryStore(Path(directory) / "queries")
        query_id, _ = store.ingest(build_operator_matrix_envelope())
        roots, expressions = store.load_query_ir(query_id)
        matrix = bitblast_qfbv_query(query_id, roots, expressions)
        matrix_status, matrix_model = _run_cadical(
            cadical, matrix.dimacs(assumptions_as_units=True)
        )
        matrix_assignments = matrix.input_bytes_from_model(matrix_model)
        smt2, _certificate, offsets = lower_qfbv_query(
            query_id, roots, expressions, normalize_qfbv_capabilities(None)
        )
        smt_path = Path(directory) / "matrix.smt2"
        smt_path.write_text(smt2, encoding="ascii")
        cvc5_result = subprocess.run(
            [str(cvc5), "--lang", "smt2", "--produce-models", str(smt_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30.0,
            check=False,
        )
        cvc5_status, cvc5_assignments = parse_qfbv_response(
            cvc5_result.stdout, offsets
        )

        proof_store = QueryStore(Path(directory) / "unsat-query")
        unsat_id, _ = proof_store.ingest(build_unsat_probe_envelope())
        unsat_roots, unsat_expressions = proof_store.load_query_ir(unsat_id)
        unsat_plan = bitblast_qfbv_query(
            unsat_id, unsat_roots, unsat_expressions
        )
        proof_path = Path(directory) / "proof.lrat"
        proof_status, _ = _run_cadical(
            cadical,
            unsat_plan.dimacs(assumptions_as_units=True),
            proof_path,
        )
        record = lift_ascii_lrat_proof(
            unsat_plan,
            proof_path.read_text(encoding="ascii"),
            source_worker="oracle",
            worker_epoch=0,
            sequence=0,
        )
        proof_authorization = IncrementalProofChecker().verify_clause_record(
            unsat_plan, record
        )

    matrix_ok = (
        matrix_status == "sat"
        and matrix_assignments == {0: 0x42, 1: 0x03}
        and cvc5_status == "sat"
        and cvc5_assignments == {"0": 0x42, "1": 0x03}
    )
    proof_ok = (
        proof_status == "unsat"
        and proof_authorization.clause
        == tuple(-literal for literal in unsat_plan.assumptions)
    )
    result = {
        "schema": SCHEMA,
        "seed": seed,
        "cases": cases,
        "operator_counts": counts,
        "mismatches": mismatches,
        "operator_matrix": {
            "cadical_status": matrix_status,
            "cadical_assignments": matrix_assignments,
            "cvc5_status": cvc5_status,
            "cvc5_assignments": cvc5_assignments,
            "all_38_operators": len(matrix.certificate["operator_counts"]) == 38,
        },
        "proof": {
            "status": proof_status,
            "steps": proof_authorization.proof_steps,
            "propagations": proof_authorization.propagation_count,
            "failed_assumptions": list(unsat_plan.assumptions),
        },
        "cadical": {
            "version": version,
            "sha256": _sha256(cadical),
        },
        "cvc5": {
            "version_output": subprocess.run(
                [str(cvc5), "--version"], text=True, capture_output=True,
                timeout=10.0, check=True,
            ).stdout.strip()[:512],
            "sha256": _sha256(cvc5),
        },
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
        "passed": not mismatches and matrix_ok and proof_ok,
    }
    encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    result["artifact_sha256"] = hashlib.sha256(encoded.encode("ascii")).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cadical", required=True, type=Path)
    parser.add_argument("--cvc5", default="/usr/bin/cvc5", type=Path)
    parser.add_argument("--cases", type=int, default=256)
    parser.add_argument("--seed", type=int, default=0xF432)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    artifact = run(
        args.cadical.resolve(strict=True),
        args.cvc5.resolve(strict=True),
        cases=max(23, min(args.cases, 4096)),
        seed=args.seed,
    )
    encoded = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="ascii")
    print(encoded, end="")
    return 0 if artifact["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
