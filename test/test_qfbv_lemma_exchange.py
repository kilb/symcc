#!/usr/bin/env python3
# RUN: python3 %s

import copy
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from cross_worker_context import CrossWorkerContextStore  # noqa: E402
from qf_bv_backend import (  # noqa: E402
    PersistentSmtLibQfbvSolver,
    normalize_qfbv_capabilities,
)
from qfbv_lemma_exchange import (  # noqa: E402
    LEMMA_PROTOCOL,
    LemmaExchangeError,
    QfbvLemmaExchange,
    QfbvLemmaStore,
    normalize_lemma_record,
    normalize_lemma_term,
    parse_learned_literal_response,
)
from qfbv_artifact_lifecycle import (  # noqa: E402
    ArtifactLifecycleRegistry,
)
from qfbv_proof_receipt import (  # noqa: E402
    QfbvProofStore,
    QfbvProofVerifier,
)
from query_store import QueryStore  # noqa: E402
from symcc_query_service import _load_portfolio, main as query_service_main  # noqa: E402


_PINNED_TOOL_ROOT = Path(
    os.environ.get(
        "SYMCC_TEST_CPC_TOOL_ROOT",
        str(Path.home() / ".local" / "share" / "symcc-cpc-1.3.4"),
    )
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _capabilities() -> dict:
    return normalize_qfbv_capabilities(
        {"incremental": True, "accept_unsat": False}
    )


def _verifier(root: Path) -> QfbvProofVerifier:
    return QfbvProofVerifier(
        QfbvProofStore(root / "proofs"),
        generator_command=[
            str(_PINNED_TOOL_ROOT / "bin" / "cvc5"),
            "--lang=smt2",
            "--safe-mode=safe",
            "--proof-granularity=dsl-rewrite",
            "--dump-proofs",
            "{query}",
        ],
        checker_command=[
            str(_PINNED_TOOL_ROOT / "bin" / "ethos"),
            "{proof}",
        ],
        signature_root=_PINNED_TOOL_ROOT / "share" / "cpc",
    )


def _exchange(root: Path, context_store: CrossWorkerContextStore):
    verifier = _verifier(root)
    exchange = QfbvLemmaExchange(
        QfbvLemmaStore(root / "lemmas"),
        context_store,
        verifier,
    )
    return verifier, exchange


def _envelope(*, descendant: bool) -> dict:
    nodes = [
        {
            "id": 0,
            "op": "read",
            "bits": 8,
            "children": [],
            "attrs": {"index": 0},
        },
        {
            "id": 1,
            "op": "read",
            "bits": 8,
            "children": [],
            "attrs": {"index": 1},
        },
        {
            "id": 2,
            "op": "read",
            "bits": 8,
            "children": [],
            "attrs": {"index": 2},
        },
        {
            "id": 3,
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": "41"},
        },
        {
            "id": 4,
            "op": "equal",
            "bits": 1,
            "children": [0, 3],
            "attrs": {},
        },
        {
            "id": 5,
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": "42"},
        },
        {
            "id": 6,
            "op": "equal",
            "bits": 1,
            "children": [1, 5],
            "attrs": {},
        },
        {
            "id": 7,
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": "40"},
        },
        {
            "id": 8,
            "op": "equal",
            "bits": 1,
            "children": [0, 7],
            "attrs": {},
        },
        {
            "id": 9,
            "op": "lor",
            "bits": 1,
            "children": [6, 8],
            "attrs": {},
        },
        {
            "id": 10,
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": "07"},
        },
        {
            "id": 11,
            "op": "equal",
            "bits": 1,
            "children": [2, 10],
            "attrs": {},
        },
    ]
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "verified-lemma-test",
        "nodes": nodes,
        "prefix_roots": [4, 9] if descendant else [4],
        "target_root": 11 if descendant else 9,
        "input_hex": "000000",
        "timeout_ms": 20_000,
        "metadata": {"source": "verified-lemma-test"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


@unittest.skipUnless(
    (_PINNED_TOOL_ROOT / "bin" / "cvc5").is_file()
    and (_PINNED_TOOL_ROOT / "bin" / "ethos").is_file(),
    "pinned cvc5 1.3.4 and Ethos are not installed",
)
class QfbvLemmaExchangeTest(unittest.TestCase):
    def test_solver_response_parser_is_bounded_and_command_closed(self):
        output = (
            "sat\n"
            "(\n(= symcc_input_1 #b01000010)\n"
            "(= symcc_input_1 #b01000010)\n)\n"
        )
        self.assertEqual(
            parse_learned_literal_response(
                output,
                allowed_offsets=(0, 1),
                max_lemmas=8,
            ),
            ("(= symcc_input_1 #b01000010)",),
        )
        for malformed in (
            "(= symcc_input_1 #b01000010) (exit)",
            "(= other #b01000010)",
            "(= symcc_input_2 #b01000010)",
            "symcc_input_1",
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises(LemmaExchangeError):
                    normalize_lemma_term(malformed, allowed_offsets=(0, 1))

        for malformed_response in (
            "unknown\n()\n",
            "unsat\n()\n",
            "sat\n",
            "sat\n()\n()\n",
            "success\nsat\n()\n",
            'sat\n(error "unsupported")\n',
        ):
            with self.subTest(malformed_response=malformed_response):
                with self.assertRaises(LemmaExchangeError):
                    parse_learned_literal_response(
                        malformed_response,
                        allowed_offsets=(0, 1),
                        max_lemmas=8,
                    )

    def test_cpc_entailment_receipt_replays_only_on_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contexts = CrossWorkerContextStore(root / "contexts")
            source_terms = (
                "(= symcc_input_0 (_ bv65 8))",
                "(or (= symcc_input_1 (_ bv66 8)) "
                "(= symcc_input_0 (_ bv64 8)))",
            )
            source_roots = tuple(_digest(term) for term in source_terms)
            capability = _capabilities()["capability_sha256"]
            source = contexts.publish_chain(
                source_roots,
                source_terms,
                capability_sha256=capability,
            )
            assert source is not None
            _proof, exchange = _exchange(root, contexts)
            authorization = exchange.certify_and_publish(
                source.context_sha256,
                "(= symcc_input_1 #b01000010)",
                category="preprocess",
                timeout_ms=5_000,
            )
            self.assertEqual(
                authorization.record["protocol"], LEMMA_PROTOCOL
            )
            extension = "(= symcc_input_2 (_ bv7 8))"
            descendant = contexts.publish_chain(
                source_roots + (_digest(extension),),
                source_terms + (extension,),
                capability_sha256=capability,
            )
            assert descendant is not None
            replay = exchange.verify_record_for_target(
                authorization.record,
                descendant.context_sha256,
                timeout_ms=5_000,
            )
            self.assertEqual(
                replay.record["lemma"],
                "(= symcc_input_1 #b01000010)",
            )

            sibling_term = "(= symcc_input_2 (_ bv9 8))"
            sibling = contexts.publish_chain(
                (source_roots[0], _digest(sibling_term)),
                (source_terms[0], sibling_term),
                capability_sha256=capability,
            )
            assert sibling is not None
            with self.assertRaisesRegex(LemmaExchangeError, "not an ancestor"):
                exchange.verify_record_for_target(
                    authorization.record,
                    sibling.context_sha256,
                    timeout_ms=5_000,
                )
            tampered = copy.deepcopy(authorization.record)
            tampered["lemma"] = "(= symcc_input_1 #b00000000)"
            with self.assertRaises(LemmaExchangeError):
                normalize_lemma_record(tampered)

    def test_lifecycle_marks_and_collects_real_context_proof_and_lemma_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = ArtifactLifecycleRegistry(root / "lifecycle")
            lease = lifecycle.start_job(
                "real-graph", "worker-0", lease_seconds=60.0
            )
            contexts = CrossWorkerContextStore(
                root / "contexts",
                lifecycle=lifecycle,
                lifecycle_lease=lease,
            )
            proof_store = QfbvProofStore(
                root / "proofs",
                lifecycle=lifecycle,
                lifecycle_lease=lease,
            )
            verifier = QfbvProofVerifier(
                proof_store,
                generator_command=[
                    str(_PINNED_TOOL_ROOT / "bin" / "cvc5"),
                    "--lang=smt2",
                    "--safe-mode=safe",
                    "--proof-granularity=dsl-rewrite",
                    "--dump-proofs",
                    "{query}",
                ],
                checker_command=[
                    str(_PINNED_TOOL_ROOT / "bin" / "ethos"),
                    "{proof}",
                ],
                signature_root=_PINNED_TOOL_ROOT / "share" / "cpc",
            )
            lemma_store = QfbvLemmaStore(
                root / "lemmas",
                lifecycle=lifecycle,
                lifecycle_lease=lease,
            )
            exchange = QfbvLemmaExchange(lemma_store, contexts, verifier)
            source_terms = (
                "(= symcc_input_0 (_ bv65 8))",
                "(or (= symcc_input_1 (_ bv66 8)) "
                "(= symcc_input_0 (_ bv64 8)))",
            )
            source = contexts.publish_chain(
                tuple(_digest(term) for term in source_terms),
                source_terms,
                capability_sha256=_capabilities()["capability_sha256"],
            )
            assert source is not None
            exchange.certify_and_publish(
                source.context_sha256,
                "(= symcc_input_1 #b01000010)",
                category="preprocess",
                timeout_ms=5_000,
            )
            self.assertEqual(
                contexts.synchronize_lifecycle(max_entries=10),
                {"complete": True, "scanned": 2, "total": 2},
            )
            self.assertEqual(
                proof_store.synchronize_lifecycle(max_entries=10),
                {"complete": True, "scanned": 2, "total": 2},
            )
            self.assertEqual(
                lemma_store.synchronize_lifecycle(max_entries=10),
                {"complete": True, "scanned": 1, "total": 1},
            )
            before = lifecycle.stats()
            self.assertEqual(before["artifacts"], 5)
            self.assertEqual(before["edges"], 4)
            protected = lifecycle.collect(
                lambda _kind, _digest, size: size,
                grace_seconds=0.0,
                max_objects=10,
                max_bytes=before["artifact_bytes"] + 1,
                time_budget_ms=5_000,
                now=time.time() + 1.0,
            )
            self.assertEqual(protected.deleted, ())
            self.assertEqual(protected.protected, 5)

            self.assertTrue(lifecycle.release_job(lease))

            def delete(kind: str, digest: str, size: int) -> int:
                if kind == "context":
                    return contexts.delete_lifecycle_artifact(kind, digest, size)
                if kind in {"proof", "receipt"}:
                    return proof_store.delete_lifecycle_artifact(kind, digest, size)
                return lemma_store.delete_lifecycle_artifact(kind, digest, size)

            collected = lifecycle.collect(
                delete,
                grace_seconds=0.0,
                max_objects=10,
                max_bytes=before["artifact_bytes"] + 1,
                time_budget_ms=5_000,
                now=time.time() + 1.0,
            )
            self.assertEqual(len(collected.deleted), 5)
            self.assertEqual(lifecycle.stats()["artifacts"], 0)
            self.assertEqual(contexts.stats()["contexts"], 0)
            self.assertEqual(proof_store.stats()["proofs"], 0)
            self.assertEqual(proof_store.stats()["receipts"], 0)
            self.assertEqual(lemma_store.stats()["records"], 0)

    def test_persistent_backend_publishes_and_new_worker_injects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            query_store = QueryStore(root / "queries")
            contexts = CrossWorkerContextStore(root / "contexts")
            verifier, exchange = _exchange(root, contexts)
            query_store.register_qfbv_proof_verifier(verifier)
            query_store.register_qfbv_lemma_exchange(exchange)
            command = [
                str(_PINNED_TOOL_ROOT / "bin" / "cvc5"),
                "--lang=smt2",
                "--incremental",
                "--produce-models",
            ]

            query_store.ingest(_envelope(descendant=False))
            first_lease = query_store.claim("worker-a")
            self.assertIsNotNone(first_lease)
            assert first_lease is not None
            with PersistentSmtLibQfbvSolver(
                query_store,
                command,
                name="cvc5-lemma-a",
                capabilities=_capabilities(),
                shared_context_store=contexts,
                proof_verifier=verifier,
                lemma_exchange=exchange,
            ) as first_backend:
                first = dict(first_backend(first_lease))
            self.assertEqual(first["status"], "sat", first)
            self.assertEqual(first["backend_lemma_publish_candidates"], 1)
            self.assertEqual(first["backend_lemma_published"], 1)
            bad_source = copy.deepcopy(first)
            bad_source["backend_lemma_source_context_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "independent store"):
                query_store.complete(first_lease, "worker-a", bad_source)
            missing_record = copy.deepcopy(first)
            missing_record["backend_published_lemma_record_sha256"] = [
                "0" * 64
            ]
            with self.assertRaisesRegex(ValueError, "independent store"):
                query_store.complete(first_lease, "worker-a", missing_record)
            self.assertTrue(
                query_store.complete(first_lease, "worker-a", first)
            )
            first_stored = json.loads(
                (
                    root
                    / "queries"
                    / "results"
                    / first_lease.query_id[:2]
                    / f"{first_lease.query_id}.json"
                ).read_text(encoding="ascii")
            )
            self.assertEqual(
                first_stored["store_verified_published_lemma_count"], 1
            )

            query_store.ingest(_envelope(descendant=True))
            second_lease = query_store.claim("worker-b")
            self.assertIsNotNone(second_lease)
            assert second_lease is not None
            second_verifier = _verifier(root)
            second_exchange = QfbvLemmaExchange(
                QfbvLemmaStore(root / "lemmas"),
                CrossWorkerContextStore(root / "contexts"),
                second_verifier,
            )
            query_store.register_qfbv_proof_verifier(second_verifier)
            query_store.register_qfbv_lemma_exchange(second_exchange)
            with PersistentSmtLibQfbvSolver(
                query_store,
                command,
                name="cvc5-lemma-b",
                capabilities=_capabilities(),
                shared_context_store=second_exchange.context_store,
                proof_verifier=second_verifier,
                lemma_exchange=second_exchange,
            ) as second_backend:
                second = dict(second_backend(second_lease))
            self.assertEqual(second["status"], "sat", second)
            self.assertEqual(second["backend_lemma_injected"], 1)
            self.assertEqual(second["backend_lemma_active"], 1)
            self.assertEqual(
                second["backend_verified_lemma_records"][0]["lemma"],
                "(= symcc_input_1 #b01000010)",
            )
            tampered = copy.deepcopy(second)
            tampered["backend_verified_lemma_records"][0][
                "proof_receipt_sha256"
            ] = "0" * 64
            with self.assertRaises(ValueError):
                query_store.complete(second_lease, "worker-b", tampered)
            with query_store._qfbv_lemma_exchange_lock:
                query_store._qfbv_lemma_exchanges.clear()
            with self.assertRaisesRegex(ValueError, "not registered locally"):
                query_store.complete(second_lease, "worker-b", second)
            query_store.register_qfbv_lemma_exchange(second_exchange)
            self.assertTrue(
                query_store.complete(second_lease, "worker-b", second)
            )
            stored = json.loads(
                (
                    root
                    / "queries"
                    / "results"
                    / second_lease.query_id[:2]
                    / f"{second_lease.query_id}.json"
                ).read_text(encoding="ascii")
            )
            self.assertEqual(stored["store_verified_lemma_count"], 1)
            self.assertEqual(query_store.stats()["verified_lemmas_injected"], 1)

    def test_store_serializes_publication_and_rejects_quota_lock_and_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contexts = CrossWorkerContextStore(root / "contexts")
            terms = (
                "(= symcc_input_0 (_ bv65 8))",
                "(or (= symcc_input_1 (_ bv66 8)) "
                "(= symcc_input_0 (_ bv64 8)))",
            )
            roots = tuple(_digest(term) for term in terms)
            source = contexts.publish_chain(
                roots,
                terms,
                capability_sha256=_capabilities()["capability_sha256"],
            )
            assert source is not None
            _proof, exchange = _exchange(root, contexts)
            first = exchange.certify_and_publish(
                source.context_sha256,
                "(= symcc_input_1 #b01000010)",
                category="preprocess",
                timeout_ms=5_000,
            ).record
            second = exchange.certify_and_publish(
                source.context_sha256,
                "(= symcc_input_0 #b01000001)",
                category="preprocess",
                timeout_ms=5_000,
            ).record

            parallel = QfbvLemmaStore(root / "parallel")
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(
                    pool.map(
                        lambda _index: parallel.publish(
                            first,
                            timeout_ms=5_000,
                        ),
                        range(8),
                    )
                )
            self.assertEqual(parallel.stats()["records"], 1)
            self.assertEqual(
                {row[0]["record_sha256"] for row in results},
                {first["record_sha256"]},
            )

            quota = QfbvLemmaStore(root / "quota", max_records=1)
            quota.publish(first, timeout_ms=5_000)
            with self.assertRaisesRegex(LemmaExchangeError, "quota"):
                quota.publish(second, timeout_ms=5_000)

            locked = QfbvLemmaStore(root / "locked")
            descriptor = os.open(
                locked.publish_lock_path,
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                with self.assertRaisesRegex(LemmaExchangeError, "lock timeout"):
                    locked.publish(first, timeout_ms=10)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

            record_path = parallel._path(first["record_sha256"])
            outside = root / "outside.json"
            outside.write_bytes(record_path.read_bytes())
            record_path.unlink()
            record_path.symlink_to(outside)
            with self.assertRaises(OSError):
                parallel.load(first["record_sha256"])

    def test_portfolio_requires_proof_and_persistent_context(self):
        valid = {
            "solvers": [
                {
                    "name": "cvc5-lemma",
                    "kind": "smtlib-qfbv",
                    "persistent": True,
                    "command": ["cvc5", "--incremental"],
                    "capabilities": {"incremental": True},
                    "unsat_proof": {
                        "format": "cpc",
                        "generator_command": ["cvc5", "{query}"],
                        "checker_command": ["ethos", "{proof}"],
                        "signature_root": "/tmp/cpc",
                    },
                    "learned_lemmas": {
                        "type": "preprocess",
                        "max_per_query": 8,
                        "timeout_ms": 5000,
                    },
                }
            ]
        }
        loaded = _load_portfolio(json.dumps(valid))
        self.assertEqual(loaded[0]["learned_lemmas"]["max_per_query"], 8)
        for mutation in (
            {"persistent": False},
            {"unsat_proof": None},
            {"learned_lemmas": {"type": "bad"}},
        ):
            with self.subTest(mutation=mutation):
                invalid = copy.deepcopy(valid)
                invalid["solvers"][0].update(mutation)
                with self.assertRaises(RuntimeError):
                    _load_portfolio(json.dumps(invalid))

    def test_learned_literal_extractor_timeout_reaps_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "slow_extractor.py"
            script.write_text(
                "import time\n"
                "time.sleep(10)\n",
                encoding="ascii",
            )
            query_store = QueryStore(root / "queries")
            contexts = CrossWorkerContextStore(root / "contexts")
            verifier, exchange = _exchange(root, contexts)
            backend = PersistentSmtLibQfbvSolver(
                query_store,
                [sys.executable, str(script)],
                name="slow-lemma-extractor",
                capabilities=_capabilities(),
                shared_context_store=contexts,
                proof_verifier=verifier,
                lemma_exchange=exchange,
            )
            started = time.monotonic()
            backend._begin_query("extract-timeout")
            try:
                with self.assertRaisesRegex(LemmaExchangeError, "timeout"):
                    backend._extract_learned_literals(
                        ("(= symcc_input_0 (_ bv65 8))",),
                        (0,),
                        query_id="extract-timeout",
                        timeout_ms=10,
                    )
            finally:
                backend._end_query("extract-timeout")
                backend.close()
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(backend._active, {})

    def test_once_summary_reports_explicit_lemma_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = StringIO()
            with redirect_stdout(output):
                status = query_service_main(
                    [
                        "--store",
                        str(root / "queries"),
                        "--once",
                        "--qfbv-lemma-store",
                        str(root / "lemmas"),
                        "--qfbv-artifact-lifecycle-store",
                        str(root / "lifecycle"),
                        "--qfbv-artifact-job-id",
                        "once-summary",
                        "--qfbv-artifact-job-lease-seconds",
                        "0.2",
                    ]
                )
            self.assertEqual(status, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(
                summary["qfbv_lemma_store"],
                {"records": 0, "record_bytes": 0},
            )
            self.assertEqual(
                summary["qfbv_artifact_lifecycle"]["active_jobs"], 0
            )
            self.assertEqual(
                summary["qfbv_artifact_lifecycle"]["jobs"], 1
            )


if __name__ == "__main__":
    unittest.main()
