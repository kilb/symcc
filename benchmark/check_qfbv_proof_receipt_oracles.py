#!/usr/bin/env python3
"""Run the real cvc5 CPC/Ethos F427 proof-receipt oracle."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qf_bv_backend import SmtLibQfbvSolver  # noqa: E402
from qfbv_proof_receipt import (  # noqa: E402
    ProofVerificationError,
    QfbvProofStore,
    QfbvProofVerifier,
)
from query_store import QueryStore  # noqa: E402


def _envelope() -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f427-independent-oracle",
        "nodes": [
            {
                "id": 0,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": 0},
            },
            {
                "id": 1,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "41"},
            },
            {
                "id": 2,
                "op": "equal",
                "bits": 1,
                "children": [0, 1],
                "attrs": {},
            },
            {
                "id": 3,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "42"},
            },
            {
                "id": 4,
                "op": "equal",
                "bits": 1,
                "children": [0, 3],
                "attrs": {},
            },
        ],
        "prefix_roots": [2],
        "target_root": 4,
        "input_hex": "00",
        "timeout_ms": 30_000,
        "metadata": {"source": "f427-independent-oracle"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verifier(tool_root: Path, store: QfbvProofStore) -> QfbvProofVerifier:
    return QfbvProofVerifier(
        store,
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


def _run_worker(
    root: Path,
    proof_store_root: Path,
    tool_root: Path,
    *,
    worker: str,
    primary_command: list[str],
) -> tuple[dict, QueryStore, QfbvProofVerifier]:
    query_store = QueryStore(root / worker)
    query_store.ingest(_envelope())
    proof_store = QfbvProofStore(proof_store_root)
    verifier = _verifier(tool_root, proof_store)
    query_store.register_qfbv_proof_verifier(verifier)
    lease = query_store.claim(worker)
    if lease is None:
        raise RuntimeError("oracle worker could not claim its query")
    backend = SmtLibQfbvSolver(
        query_store,
        primary_command,
        name=f"oracle-{worker}",
        proof_verifier=verifier,
    )
    result = dict(backend(lease))
    if result.get("status") != "unsat":
        raise RuntimeError(f"oracle worker returned {result!r}")
    if not query_store.complete(lease, worker, result):
        raise RuntimeError("oracle worker result became stale")
    return result, query_store, verifier


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tool-root",
        type=Path,
        default=Path.home() / ".local" / "share" / "symcc-cpc-1.3.4",
    )
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    repetitions = max(1, min(args.repetitions, 100))
    tool_root = args.tool_root.resolve()
    cvc5 = tool_root / "bin" / "cvc5"
    ethos = tool_root / "bin" / "ethos"
    if not cvc5.is_file() or not ethos.is_file():
        raise SystemExit("run install_cvc5_cpc_ethos_1_3_4.sh first")

    generation_us: list[int] = []
    backend_check_us: list[int] = []
    store_check_us: list[int] = []
    reuse_total_us: list[int] = []
    receipt_digests: set[str] = set()
    false_authorizations = 0
    reference_tamper_rejected = 0
    cas_tamper_rejected = 0

    with tempfile.TemporaryDirectory(prefix="symcc-f427-oracle-") as directory:
        root = Path(directory)
        proof_root = root / "shared-proofs"
        first_result, first_store, first_verifier = _run_worker(
            root,
            proof_root,
            tool_root,
            worker="worker-a",
            primary_command=[
                str(cvc5),
                "--lang=smt2",
                "--produce-models",
                "{query}",
            ],
        )
        generation_us.append(
            int(first_result["backend_unsat_proof_generator_elapsed_us"])
        )
        backend_check_us.append(
            int(first_result["backend_unsat_proof_checker_elapsed_us"])
        )
        receipt = first_result["backend_unsat_proof_receipt"]
        receipt_digests.add(str(receipt["receipt_sha256"]))
        stored_path = (
            first_store.result_dir
            / str(receipt["query_id"])[:2]
            / f"{receipt['query_id']}.json"
        )
        stored = json.loads(stored_path.read_text(encoding="ascii"))
        store_check_us.append(
            int(stored["store_unsat_proof_checker_elapsed_us"])
        )

        loaded = first_store.load_query_ir(str(receipt["query_id"]))
        if loaded is None:
            raise RuntimeError("oracle Query IR disappeared")
        from qf_bv_backend import lower_qfbv_proof_problem

        (
            _smt2,
            _proof_query,
            reference,
            _certificate,
            offsets,
            _terms,
            _context,
        ) = lower_qfbv_proof_problem(
            str(receipt["query_id"]),
            loaded[0],
            loaded[1],
            first_result["backend_capabilities"],
        )
        proof_body = first_verifier.store.load_proof(str(receipt["proof_sha256"]))
        satisfiable_reference = reference.replace(
            "#b01000010", "#b01000001"
        ).encode("ascii")
        try:
            first_verifier.check_proof_body(
                proof_body,
                satisfiable_reference,
                offsets,
            )
            false_authorizations += 1
        except ProofVerificationError:
            reference_tamper_rejected += 1

        for index in range(repetitions):
            started = time.monotonic_ns()
            reused, reused_store, _ = _run_worker(
                root,
                proof_root,
                tool_root,
                worker=f"worker-reuse-{index}",
                primary_command=["/primary/solver/must/not/run"],
            )
            reuse_total_us.append((time.monotonic_ns() - started) // 1000)
            if not reused["backend_unsat_proof_reused"]:
                false_authorizations += 1
            receipt_digests.add(
                str(reused["backend_unsat_proof_receipt"]["receipt_sha256"])
            )
            reused_stored = json.loads(
                next(reused_store.result_dir.glob("*/*.json")).read_text(
                    encoding="ascii"
                )
            )
            store_check_us.append(
                int(reused_stored["store_unsat_proof_checker_elapsed_us"])
            )

        proof_path = first_verifier.store._proof_path(str(receipt["proof_sha256"]))
        proof_path.write_bytes(b"tampered\n")
        try:
            first_verifier.store.load_proof(str(receipt["proof_sha256"]))
            false_authorizations += 1
        except ProofVerificationError:
            cas_tamper_rejected += 1

        store_stats = QfbvProofStore(proof_root).stats()

    cvc5_version = subprocess.run(
        [str(cvc5), "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()[0]
    summary = {
        "schema": "symcc-f427-qfbv-proof-receipt-oracle-v1",
        "toolchain": {
            "cvc5_version": cvc5_version,
            "cvc5_sha256": _sha256(cvc5),
            "ethos_sha256": _sha256(ethos),
            "signature_files": len(list((tool_root / "share" / "cpc").rglob("*.eo"))),
        },
        "checks": {
            "generated_checked_unsat": 1,
            "cross_worker_reuses": repetitions,
            "reference_tamper_rejected": reference_tamper_rejected,
            "cas_tamper_rejected": cas_tamper_rejected,
            "false_authorizations": false_authorizations,
            "unique_receipts": len(receipt_digests),
        },
        "timing_us": {
            "generation": generation_us,
            "backend_checker": backend_check_us,
            "store_checker": store_check_us,
            "reuse_total": reuse_total_us,
            "reuse_total_median": int(statistics.median(reuse_total_us)),
        },
        "proof_store": store_stats,
    }
    encoded = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="ascii")
    sys.stdout.write(encoded)
    return 0 if false_authorizations == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
