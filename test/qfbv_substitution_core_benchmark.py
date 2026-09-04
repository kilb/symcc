#!/usr/bin/env python3
# RUN: python3 %s --rounds 16
"""Mechanism microbenchmark for F431 substitution-core proof reuse."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qf_bv_backend import (  # noqa: E402
    lower_qfbv_proof_problem,
    normalize_qfbv_capabilities,
)
from qfbv_proof_receipt import QfbvProofStore, QfbvProofVerifier  # noqa: E402
from qfbv_substitution_core import (  # noqa: E402
    QfbvSubstitutionCoreExchange,
    QfbvSubstitutionCoreStore,
)


TOOL_ROOT = Path(
    os.environ.get(
        "SYMCC_TEST_CPC_TOOL_ROOT",
        str(Path.home() / ".local" / "share" / "symcc-cpc-1.3.4"),
    )
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def formula(
    offset: int,
    padding: int,
) -> tuple[tuple[str, ...], dict[str, dict]]:
    expressions: dict[str, dict] = {}

    def node(op: str, bits: int, children: tuple[str, ...], attrs: dict) -> str:
        body = {
            "schema": "symcc-expr-node-v1",
            "op": op,
            "bits": bits,
            "children": list(children),
            "attrs": attrs,
        }
        digest = _digest(body)
        expressions[digest] = body
        return digest

    read = node("read", 8, (), {"index": offset})
    first = node("constant", 8, (), {"value_hex": "41"})
    second = node("constant", 8, (), {"value_hex": "42"})
    roots = [
        node("equal", 1, (read, first), {}),
        node("equal", 1, (read, second), {}),
    ]
    for index in range(padding):
        padding_read = node(
            "read", 8, (), {"index": offset + index + 1}
        )
        constant = node(
            "constant", 8, (), {"value_hex": f"{index % 256:02x}"}
        )
        roots.append(node("equal", 1, (padding_read, constant), {}))
    return tuple(roots), expressions


def verifier(root: Path) -> QfbvProofVerifier:
    return QfbvProofVerifier(
        QfbvProofStore(root / "proofs"),
        generator_command=[
            str(TOOL_ROOT / "bin" / "cvc5"),
            "--lang=smt2",
            "--safe-mode=safe",
            "--proof-granularity=dsl-rewrite",
            "--dump-proofs",
            "{query}",
        ],
        checker_command=[str(TOOL_ROOT / "bin" / "ethos"), "{proof}"],
        signature_root=TOOL_ROOT / "share" / "cpc",
    )


def proof_inputs(
    query_id: str,
    roots: tuple[str, ...],
    expressions: dict[str, dict],
    capabilities: dict,
) -> dict:
    (
        smt2,
        proof_query,
        reference,
        certificate,
        offsets,
        root_terms,
        context,
    ) = lower_qfbv_proof_problem(
        query_id, roots, expressions, capabilities
    )
    return {
        "query_id": query_id,
        "smt2": smt2.encode("ascii"),
        "proof_query_smt2": proof_query.encode("ascii"),
        "reference_smt2": reference.encode("ascii"),
        "offsets": offsets,
        "lowering_certificate_sha256": certificate["certificate_sha256"],
        "capability_sha256": capabilities["capability_sha256"],
        "context": context,
        "root_terms": root_terms,
    }


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def run(rounds: int, padding: int) -> dict[str, object]:
    if not (TOOL_ROOT / "bin" / "cvc5").is_file() or not (
        TOOL_ROOT / "bin" / "ethos"
    ).is_file():
        return {
            "schema": "symcc-qfbv-substitution-core-benchmark-v1",
            "status": "unsupported",
            "reason": "pinned cvc5/Ethos tools are unavailable",
        }
    capabilities = normalize_qfbv_capabilities(None)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        proof_verifier = verifier(root)
        exchange = QfbvSubstitutionCoreExchange(
            QfbvSubstitutionCoreStore(root / "cores"),
            proof_verifier,
            [
                str(TOOL_ROOT / "bin" / "cvc5"),
                "--lang=smt2",
                "--safe-mode=safe",
                "{query}",
            ],
        )
        source_roots, source_expressions = formula(0, padding)
        source_id = _digest(
            {"schema": "f431-benchmark-source-v1", "roots": source_roots}
        )
        source_inputs = proof_inputs(
            source_id, source_roots, source_expressions, capabilities
        )
        record, _created, _extractor_us, _proof_us = exchange.publish_from_unsat(
            source_query_id=source_id,
            roots=source_roots,
            expressions=source_expressions,
            root_terms=source_inputs["root_terms"],
            offsets=source_inputs["offsets"],
            capabilities=capabilities,
        )
        cold_verifier = verifier(root)
        cold_exchange = QfbvSubstitutionCoreExchange(
            QfbvSubstitutionCoreStore(root / "cores"),
            cold_verifier,
            [
                str(TOOL_ROOT / "bin" / "cvc5"),
                "--lang=smt2",
                "--safe-mode=safe",
                "{query}",
            ],
        )
        cold_offset = 500
        cold_roots, cold_expressions = formula(cold_offset, padding)
        started = time.monotonic_ns()
        cold_authorization = cold_exchange.lookup(
            cold_roots,
            cold_expressions,
            capabilities,
            timeout_ms=30_000,
        )
        cold_reuse_us = (time.monotonic_ns() - started) // 1000
        if (
            cold_authorization is None
            or cold_authorization.proof_reused
            or cold_authorization.match.mapping != {0: cold_offset}
        ):
            raise RuntimeError("fresh-consumer core verification failed")
        baseline_us: list[int] = []
        reuse_us: list[int] = []
        for index in range(rounds):
            target_offset = 1000 + index * (padding + 1)
            roots, expressions = formula(target_offset, padding)
            query_id = _digest(
                {
                    "schema": "f431-benchmark-target-v1",
                    "roots": roots,
                    "round": index,
                }
            )
            inputs = proof_inputs(query_id, roots, expressions, capabilities)
            started = time.monotonic_ns()
            authorization = proof_verifier.authorize(
                **{
                    key: value
                    for key, value in inputs.items()
                    if key != "root_terms"
                },
                timeout_ms=30_000,
            )
            baseline_us.append((time.monotonic_ns() - started) // 1000)
            if authorization is None:
                raise RuntimeError("baseline proof was not authorized")
            started = time.monotonic_ns()
            reused = exchange.lookup(
                roots,
                expressions,
                capabilities,
                timeout_ms=30_000,
            )
            reuse_us.append((time.monotonic_ns() - started) // 1000)
            if (
                reused is None
                or reused.record["record_sha256"] != record["record_sha256"]
                or reused.match.mapping != {0: target_offset}
            ):
                raise RuntimeError("substitution-core reuse failed")
        baseline_total = sum(baseline_us)
        reuse_total = sum(reuse_us)
        return {
            "schema": "symcc-qfbv-substitution-core-benchmark-v1",
            "status": "complete",
            "rounds": rounds,
            "padding_clauses": padding,
            "formula_clause_count": padding + 2,
            "baseline": "fresh-target-cvc5-cpc-generation-plus-ethos",
            "cold_reuse": "fresh-consumer-ethos-replay-plus-exact-substitution",
            "cold_reuse_us": cold_reuse_us,
            "reuse": "verified-source-core-cache-plus-exact-substitution",
            "baseline_total_us": baseline_total,
            "reuse_total_us": reuse_total,
            "baseline_median_us": int(statistics.median(baseline_us)),
            "reuse_median_us": int(statistics.median(reuse_us)),
            "baseline_p95_us": percentile(baseline_us, 0.95),
            "reuse_p95_us": percentile(reuse_us, 0.95),
            "total_speedup": baseline_total / max(1, reuse_total),
            "record_clause_count": record["source_clause_count"],
            "record_node_count": record["source_node_count"],
            "claim_scope": "mechanism-only; not a fuzzing coverage claim",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=32)
    parser.add_argument("--padding", type=int, default=128)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 10_000:
        parser.error("--rounds must be in [1, 10000]")
    if not 0 <= args.padding <= 4094:
        parser.error("--padding must be in [0, 4094]")
    result = run(args.rounds, args.padding)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
