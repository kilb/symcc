#!/usr/bin/env python3
"""Run independent finite and live F428 verified-lemma oracles."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from cross_worker_context import CrossWorkerContextStore  # noqa: E402
from qf_bv_backend import (  # noqa: E402
    PersistentSmtLibQfbvSolver,
    normalize_qfbv_capabilities,
)
from qfbv_lemma_exchange import (  # noqa: E402
    LemmaExchangeError,
    QfbvLemmaExchange,
    QfbvLemmaStore,
    normalize_lemma_record,
)
from qfbv_proof_receipt import (  # noqa: E402
    ProofVerificationError,
    QfbvProofStore,
    QfbvProofVerifier,
)
from query_store import QueryStore  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _term_digest(term: str) -> str:
    return hashlib.sha256(term.encode("ascii")).hexdigest()


def _capabilities() -> dict[str, Any]:
    return normalize_qfbv_capabilities(
        {"incremental": True, "accept_unsat": False}
    )


def _verifier(tool_root: Path, proof_root: Path) -> QfbvProofVerifier:
    return QfbvProofVerifier(
        QfbvProofStore(proof_root),
        generator_command=[
            str(tool_root / "bin" / "cvc5"),
            "--lang=smt2",
            "--safe-mode=safe",
            "--proof-granularity=dsl-rewrite",
            "--dump-proofs",
            "{query}",
        ],
        checker_command=[str(tool_root / "bin" / "ethos"), "{proof}"],
        signature_root=tool_root / "share" / "cpc",
        timeout_ms=30_000,
    )


def _envelope(*, descendant: bool, target_value: int = 7) -> dict[str, Any]:
    nodes = [
        {"id": 0, "op": "read", "bits": 8, "children": [], "attrs": {"index": 0}},
        {"id": 1, "op": "read", "bits": 8, "children": [], "attrs": {"index": 1}},
        {"id": 2, "op": "read", "bits": 8, "children": [], "attrs": {"index": 2}},
        {"id": 3, "op": "constant", "bits": 8, "children": [], "attrs": {"value_hex": "41"}},
        {"id": 4, "op": "equal", "bits": 1, "children": [0, 3], "attrs": {}},
        {"id": 5, "op": "constant", "bits": 8, "children": [], "attrs": {"value_hex": "42"}},
        {"id": 6, "op": "equal", "bits": 1, "children": [1, 5], "attrs": {}},
        {"id": 7, "op": "constant", "bits": 8, "children": [], "attrs": {"value_hex": "40"}},
        {"id": 8, "op": "equal", "bits": 1, "children": [0, 7], "attrs": {}},
        {"id": 9, "op": "lor", "bits": 1, "children": [6, 8], "attrs": {}},
        {
            "id": 10,
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": f"{target_value:02x}"},
        },
        {"id": 11, "op": "equal", "bits": 1, "children": [2, 10], "attrs": {}},
    ]
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f428-independent-oracle",
        "nodes": nodes,
        "prefix_roots": [4, 9] if descendant else [4],
        "target_root": 11 if descendant else 9,
        "input_hex": "000000",
        "timeout_ms": 30_000,
        "metadata": {
            "source": "f428-independent-oracle",
            "target_value": target_value,
        },
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


def _finite_entailment_oracle() -> dict[str, int]:
    valuations = 0
    source_models = 0
    false_entailments = 0
    invalid_counterexamples = 0
    for first in range(256):
        for second in range(256):
            valuations += 1
            source = first == 65 and (second == 66 or first == 64)
            if source:
                source_models += 1
                false_entailments += int(second != 66)
                invalid_counterexamples += int(second != 67)
    if source_models != 1 or false_entailments != 0 or invalid_counterexamples != 1:
        raise RuntimeError("independent finite entailment oracle disagrees")
    return {
        "valuations": valuations,
        "source_models": source_models,
        "valid_lemma_counterexamples": false_entailments,
        "invalid_lemma_counterexamples": invalid_counterexamples,
    }


def _backend(
    query_store: QueryStore,
    context_store: CrossWorkerContextStore,
    exchange: QfbvLemmaExchange,
    verifier: QfbvProofVerifier,
    cvc5: Path,
    name: str,
) -> PersistentSmtLibQfbvSolver:
    return PersistentSmtLibQfbvSolver(
        query_store,
        [str(cvc5), "--lang=smt2", "--incremental", "--produce-models"],
        name=name,
        capabilities=_capabilities(),
        shared_context_store=context_store,
        context_owner=name,
        proof_verifier=verifier,
        lemma_exchange=exchange,
        max_learned_lemmas=8,
        lemma_timeout_ms=30_000,
    )


def _live_oracle(tool_root: Path, repetitions: int) -> dict[str, Any]:
    cvc5 = tool_root / "bin" / "cvc5"
    with tempfile.TemporaryDirectory(prefix="symcc-f428-oracle-") as directory:
        root = Path(directory)
        query_store = QueryStore(root / "queries")
        context_root = root / "contexts"
        proof_root = root / "proofs"
        lemma_root = root / "lemmas"
        contexts = CrossWorkerContextStore(context_root)
        verifier = _verifier(tool_root, proof_root)
        exchange = QfbvLemmaExchange(
            QfbvLemmaStore(lemma_root), contexts, verifier
        )
        query_store.register_qfbv_proof_verifier(verifier)
        query_store.register_qfbv_lemma_exchange(exchange)

        query_store.ingest(_envelope(descendant=False))
        first_lease = query_store.claim("producer")
        if first_lease is None:
            raise RuntimeError("producer query was not claimable")
        with _backend(
            query_store, contexts, exchange, verifier, cvc5, "producer"
        ) as producer:
            first = dict(producer(first_lease))
        if first.get("status") != "sat" or not query_store.complete(
            first_lease, "producer", first
        ):
            raise RuntimeError(f"producer result failed: {first!r}")
        if first.get("backend_lemma_published") != 1:
            raise RuntimeError(f"producer did not publish one lemma: {first!r}")
        record_sha256 = str(first["backend_published_lemma_record_sha256"][0])
        record = exchange.store.load(record_sha256)
        if record["lemma"] != "(= symcc_input_1 #b01000010)":
            raise RuntimeError(f"unexpected learned lemma: {record['lemma']!r}")

        injected: list[int] = []
        backend_checker_us: list[int] = []
        store_checker_us: list[int] = []
        total_us: list[int] = []
        for index in range(repetitions):
            query_store.ingest(
                _envelope(descendant=True, target_value=7 + index)
            )
            owner = f"consumer-{index}"
            lease = query_store.claim(owner)
            if lease is None:
                raise RuntimeError("consumer query was not claimable")
            local_contexts = CrossWorkerContextStore(context_root)
            local_verifier = _verifier(tool_root, proof_root)
            local_exchange = QfbvLemmaExchange(
                QfbvLemmaStore(lemma_root), local_contexts, local_verifier
            )
            query_store.register_qfbv_proof_verifier(local_verifier)
            query_store.register_qfbv_lemma_exchange(local_exchange)
            started = time.monotonic_ns()
            with _backend(
                query_store,
                local_contexts,
                local_exchange,
                local_verifier,
                cvc5,
                owner,
            ) as consumer:
                result = dict(consumer(lease))
            if result.get("status") != "sat" or not query_store.complete(
                lease, owner, result
            ):
                raise RuntimeError(f"consumer result failed: {result!r}")
            total_us.append((time.monotonic_ns() - started) // 1000)
            injected.append(int(result["backend_lemma_injected"]))
            backend_checker_us.append(
                int(result["backend_lemma_checker_elapsed_us"])
            )
            stored = json.loads(
                (
                    query_store.result_dir
                    / lease.query_id[:2]
                    / f"{lease.query_id}.json"
                ).read_text(encoding="ascii")
            )
            store_checker_us.append(
                int(stored["store_lemma_checker_elapsed_us"])
            )

        source = record["source_context"]
        source_plan = contexts.resolve(str(source["context_sha256"]))
        sibling_term = "(= symcc_input_2 (_ bv9 8))"
        sibling = contexts.publish_chain(
            (source_plan.root_hashes[0], _term_digest(sibling_term)),
            (source_plan.terms[0], sibling_term),
            capability_sha256=source_plan.capability_sha256,
        )
        if sibling is None:
            raise RuntimeError("sibling context publication failed")

        sibling_rejected = 0
        invalid_lemma_rejected = 0
        record_tamper_rejected = 0
        proof_tamper_rejected = 0
        try:
            exchange.verify_record_for_target(
                record, sibling.context_sha256, timeout_ms=30_000
            )
        except LemmaExchangeError:
            sibling_rejected = 1
        try:
            exchange.certify_and_publish(
                source_plan.context_sha256,
                "(= symcc_input_1 #b01000011)",
                category="preprocess",
                timeout_ms=30_000,
            )
        except LemmaExchangeError:
            invalid_lemma_rejected = 1
        tampered = copy.deepcopy(record)
        tampered["lemma"] = "(= symcc_input_1 #b00000000)"
        try:
            normalize_lemma_record(tampered)
        except LemmaExchangeError:
            record_tamper_rejected = 1

        receipt = verifier.store.load_receipt(record["proof_receipt_sha256"])
        proof_path = verifier.store._proof_path(str(receipt["proof_sha256"]))
        proof_path.write_bytes(b"tampered\n")
        try:
            exchange.verify_record_for_target(
                record, source_plan.context_sha256, timeout_ms=30_000
            )
        except (LemmaExchangeError, ProofVerificationError):
            proof_tamper_rejected = 1

        false_authorizations = sum(
            int(value != 1)
            for value in (
                sibling_rejected,
                invalid_lemma_rejected,
                record_tamper_rejected,
                proof_tamper_rejected,
            )
        ) + sum(int(value < 1) for value in injected)
        return {
            "producer": {
                "candidates": first["backend_lemma_publish_candidates"],
                "published": first["backend_lemma_published"],
                "record_sha256": record_sha256,
                "lemma": record["lemma"],
            },
            "consumers": {
                "runs": repetitions,
                "injected": injected,
                "backend_checker_us": backend_checker_us,
                "store_checker_us": store_checker_us,
                "total_us": total_us,
                "total_us_median": int(statistics.median(total_us)),
            },
            "negative_checks": {
                "sibling_rejected": sibling_rejected,
                "invalid_lemma_rejected": invalid_lemma_rejected,
                "record_tamper_rejected": record_tamper_rejected,
                "proof_tamper_rejected": proof_tamper_rejected,
                "false_authorizations": false_authorizations,
            },
            "stores": {
                "contexts": contexts.stats(),
                "proofs": verifier.store.stats(),
                "lemmas": exchange.store.stats(),
                "query_results": query_store.stats()["verified_lemma_results"],
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tool-root",
        type=Path,
        default=Path.home() / ".local" / "share" / "symcc-cpc-1.3.4",
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    repetitions = max(1, min(args.repetitions, 32))
    tool_root = args.tool_root.resolve()
    cvc5 = tool_root / "bin" / "cvc5"
    ethos = tool_root / "bin" / "ethos"
    if not cvc5.is_file() or not ethos.is_file():
        raise SystemExit("run install_cvc5_cpc_ethos_1_3_4.sh first")

    summary = {
        "schema": "symcc-f428-qfbv-verified-lemma-oracle-v1",
        "finite_entailment": _finite_entailment_oracle(),
        "live": _live_oracle(tool_root, repetitions),
        "toolchain": {
            "cvc5_version": subprocess.run(
                [str(cvc5), "--version"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()[0],
            "cvc5_sha256": _sha256(cvc5),
            "ethos_sha256": _sha256(ethos),
            "signature_files": len(
                list((tool_root / "share" / "cpc").rglob("*.eo"))
            ),
        },
        "claim": (
            "finite semantics and mechanism evidence only; not a public-target "
            "coverage or end-to-end speedup result"
        ),
    }
    encoded = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="ascii")
    sys.stdout.write(encoded)
    return 0 if summary["live"]["negative_checks"]["false_authorizations"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
