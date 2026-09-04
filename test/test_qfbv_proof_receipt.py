#!/usr/bin/env python3
# RUN: python3 %s

import copy
import io
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

import qf_bv_backend as qfbv_backend  # noqa: E402
from qf_bv_backend import (  # noqa: E402
    PersistentSmtLibQfbvSolver,
    SmtLibQfbvSolver,
    lower_qfbv_proof_problem,
    normalize_qfbv_capabilities,
)
from qfbv_proof_receipt import (  # noqa: E402
    PROOF_PROTOCOL,
    ProofVerificationError,
    QfbvProofStore,
    QfbvProofVerifier,
    normalize_proof_receipt,
)
from query_store import PortfolioSolver, QueryStore  # noqa: E402
from symcc_query_service import (  # noqa: E402
    _load_portfolio,
    main as query_service_main,
)


_PINNED_TOOL_ROOT = Path(
    os.environ.get(
        "SYMCC_TEST_CPC_TOOL_ROOT",
        str(Path.home() / ".local" / "share" / "symcc-cpc-1.3.4"),
    )
)


def _envelope() -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "proof-receipt-test",
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
        "timeout_ms": 10_000,
        "metadata": {"source": "proof-receipt-test"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


class _Fixture:
    def __init__(
        self,
        root: Path,
        *,
        generator_mode: str = "valid",
        checker_mode: str = "valid",
    ):
        self.root = root
        self.signature_root = root / "signatures"
        (self.signature_root / "expert").mkdir(parents=True)
        (self.signature_root / "Cpc.eo").write_text(
            "; fake CPC signature root for protocol tests\n", encoding="ascii"
        )
        (self.signature_root / "expert" / "CpcExpert.eo").write_text(
            "; fake CPC expert signature for protocol tests\n", encoding="ascii"
        )
        self.generator = root / "generator.py"
        self.generator.write_text(
            "import pathlib, sys, time\n"
            "mode, query = sys.argv[1], pathlib.Path(sys.argv[2])\n"
            "query.read_bytes()\n"
            "if mode == 'sleep':\n"
            "    pathlib.Path(sys.argv[3]).write_text('started')\n"
            "    time.sleep(30)\n"
            "elif mode == 'delay':\n"
            "    time.sleep(0.12)\n"
            "print('unsat')\n"
            "print('(')\n"
            "print('(declare-const symcc_input_0 (_ BitVec 8))')\n"
            "print('(assume @p1 (= symcc_input_0 #b01000001))')\n"
            "if mode == 'trust':\n"
            "    print('(step @p2 false :rule trust)')\n"
            "elif mode == 'trustspace':\n"
            "    print('(step @p2 false :rule    trust)')\n"
            "elif mode == 'nonfalse':\n"
            "    print('(step @p2 true :rule fake :premises (@p1))')\n"
            "elif mode == 'exit':\n"
            "    print('(exit)')\n"
            "    print('(step @p2 false :rule fake :premises (@p1))')\n"
            "else:\n"
            "    print('(step @p2 false :rule fake :premises (@p1))')\n"
            "print(')')\n",
            encoding="ascii",
        )
        self.checker = root / "checker.py"
        self.checker.write_text(
            "import pathlib, sys, time\n"
            "mode, proof = sys.argv[1], pathlib.Path(sys.argv[2])\n"
            "text = proof.read_text(encoding='ascii')\n"
            "if '(reference ' not in text or '(step ' not in text:\n"
            "    raise SystemExit(3)\n"
            "if mode == 'incomplete':\n"
            "    print('incomplete')\n"
            "elif mode == 'stderr':\n"
            "    print('correct')\n"
            "    print('unexpected diagnostic', file=sys.stderr)\n"
            "elif mode == 'whitespace':\n"
            "    print('correct ')\n"
            "else:\n"
            "    if mode == 'delay':\n"
            "        time.sleep(0.12)\n"
            "    print('correct')\n",
            encoding="ascii",
        )
        self.solver = root / "solver.py"
        self.solver.write_text("print('unsat')\n", encoding="ascii")
        self.generator_mode = generator_mode
        self.checker_mode = checker_mode

    def verifier(
        self,
        store: QfbvProofStore,
        *,
        timeout_ms: int = 5_000,
    ) -> QfbvProofVerifier:
        generator_command = [
            sys.executable,
            str(self.generator),
            self.generator_mode,
            "{query}",
        ]
        if self.generator_mode == "sleep":
            generator_command.append(str(self.root / "generator-started"))
        return QfbvProofVerifier(
            store,
            generator_command=generator_command,
            checker_command=[
                sys.executable,
                str(self.checker),
                self.checker_mode,
                "{proof}",
            ],
            signature_root=self.signature_root,
            generator_trusted_files=[self.generator],
            checker_trusted_files=[self.checker],
            timeout_ms=timeout_ms,
        )

    def backend(
        self,
        query_store: QueryStore,
        verifier: QfbvProofVerifier,
        *,
        command: list[str] | None = None,
    ) -> SmtLibQfbvSolver:
        return SmtLibQfbvSolver(
            query_store,
            command
            or [sys.executable, str(self.solver), "{query}"],
            name="proof-fixture",
            proof_verifier=verifier,
        )


def _proof_inputs(query_store: QueryStore, query_id: str) -> dict:
    loaded = query_store.load_query_ir(query_id)
    assert loaded is not None
    capabilities = normalize_qfbv_capabilities(None)
    (
        smt2,
        proof_query,
        reference,
        certificate,
        offsets,
        _terms,
        context,
    ) = lower_qfbv_proof_problem(
        query_id,
        loaded[0],
        loaded[1],
        capabilities,
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
        "timeout_ms": 5_000,
    }


def _publish_proof_receipt_process(
    fixture: _Fixture,
    proof_root: Path,
    inputs: dict,
    start,
    results,
) -> None:
    """Publish one receipt in a spawn-safe child process."""
    start.wait()
    try:
        verifier = fixture.verifier(QfbvProofStore(proof_root, max_objects=2))
        authorization = verifier.authorize(**inputs)
        results.put(("ok", authorization.receipt["receipt_sha256"]))
    except BaseException as error:
        results.put(("error", str(error)))


class QfbvProofReceiptTest(unittest.TestCase):
    def test_backend_commit_and_cross_worker_receipt_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root / "fixture")
            proof_store = QfbvProofStore(root / "shared-proofs")

            first_store = QueryStore(root / "worker-a")
            query_id, _ = first_store.ingest(_envelope())
            first_verifier = fixture.verifier(proof_store)
            first_store.register_qfbv_proof_verifier(first_verifier)
            first_lease = first_store.claim("worker-a")
            self.assertIsNotNone(first_lease)
            assert first_lease is not None
            first = dict(fixture.backend(first_store, first_verifier)(first_lease))
            self.assertEqual(first["status"], "unsat")
            self.assertFalse(first["backend_unsat_proof_reused"])
            self.assertTrue(first["backend_unsat_proof_verified"])
            self.assertEqual(
                first["backend_unsat_proof_protocol"], PROOF_PROTOCOL
            )
            self.assertTrue(first_store.complete(first_lease, "worker-a", first))
            stored = json.loads(
                (
                    root
                    / "worker-a"
                    / "results"
                    / query_id[:2]
                    / f"{query_id}.json"
                ).read_text(encoding="ascii")
            )
            self.assertTrue(stored["store_unsat_proof_verified"])

            second_store = QueryStore(root / "worker-b")
            second_id, _ = second_store.ingest(_envelope())
            self.assertEqual(second_id, query_id)
            second_verifier = fixture.verifier(
                QfbvProofStore(root / "shared-proofs")
            )
            second_store.register_qfbv_proof_verifier(second_verifier)
            second_lease = second_store.claim("worker-b")
            self.assertIsNotNone(second_lease)
            assert second_lease is not None
            second = dict(
                fixture.backend(
                    second_store,
                    second_verifier,
                    command=["/primary/solver/must/not/run"],
                )(second_lease)
            )
            self.assertEqual(second["status"], "unsat")
            self.assertTrue(second["backend_unsat_proof_reused"])
            self.assertTrue(
                second_store.complete(second_lease, "worker-b", second)
            )
            self.assertEqual(proof_store.stats()["proofs"], 1)
            self.assertEqual(proof_store.stats()["result_keys"], 1)
            self.assertEqual(
                second_store.stats()["proof_receipt_reuses"], 1
            )

    def test_store_requires_registered_checker_and_rejects_receipt_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root / "fixture")
            proof_store = QfbvProofStore(root / "proofs")
            verifier = fixture.verifier(proof_store)
            store = QueryStore(root / "queries")
            store.ingest(_envelope())
            lease = store.claim("unregistered")
            self.assertIsNotNone(lease)
            assert lease is not None
            result = dict(fixture.backend(store, verifier)(lease))
            with self.assertRaisesRegex(ValueError, "not registered"):
                store.complete(lease, "unregistered", result)

            tampered = copy.deepcopy(result)
            tampered["backend_unsat_proof_receipt"]["proof_bytes"] += 1
            with self.assertRaisesRegex(ValueError, "invalid QF_BV"):
                QueryStore._validate_result(tampered)

    def test_tampered_cas_and_policy_drift_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root / "fixture")
            proof_store = QfbvProofStore(root / "proofs")
            verifier = fixture.verifier(proof_store)
            query_store = QueryStore(root / "queries")
            query_id, _ = query_store.ingest(_envelope())
            authorization = verifier.authorize(**_proof_inputs(query_store, query_id))
            receipt = normalize_proof_receipt(authorization.receipt)
            proof_path = proof_store._proof_path(receipt["proof_sha256"])
            proof_path.write_bytes(b"tampered\n")
            with self.assertRaisesRegex(
                ProofVerificationError, "digest mismatch"
            ):
                verifier.try_reuse(**_proof_inputs(query_store, query_id))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root / "fixture")
            proof_store = QfbvProofStore(root / "proofs")
            verifier = fixture.verifier(proof_store)
            query_store = QueryStore(root / "queries")
            query_store.ingest(_envelope())
            lease = query_store.claim("policy-drift")
            self.assertIsNotNone(lease)
            assert lease is not None
            fixture.checker.write_text(
                fixture.checker.read_text(encoding="ascii") + "# drift\n",
                encoding="ascii",
            )
            result = dict(fixture.backend(query_store, verifier)(lease))
            self.assertEqual(result["status"], "unknown")
            self.assertIn("policy identity changed", result["reason"])

    def test_incomplete_trust_nonfalse_and_stderr_are_rejected(self):
        cases = (
            ("trust", "valid", "incomplete step"),
            ("trustspace", "valid", "incomplete step"),
            ("nonfalse", "valid", "ending in false"),
            ("exit", "valid", "forbidden top-level command"),
            ("valid", "incomplete", "complete refutation"),
            ("valid", "stderr", "complete refutation"),
            ("valid", "whitespace", "complete refutation"),
        )
        for generator_mode, checker_mode, expected in cases:
            with self.subTest(generator=generator_mode, checker=checker_mode):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    fixture = _Fixture(
                        root / "fixture",
                        generator_mode=generator_mode,
                        checker_mode=checker_mode,
                    )
                    proof_store = QfbvProofStore(root / "proofs")
                    verifier = fixture.verifier(proof_store)
                    query_store = QueryStore(root / "queries")
                    query_store.ingest(_envelope())
                    lease = query_store.claim("negative")
                    self.assertIsNotNone(lease)
                    assert lease is not None
                    result = dict(fixture.backend(query_store, verifier)(lease))
                    self.assertEqual(result["status"], "unknown")
                    self.assertIn(expected, result["reason"])
                    self.assertEqual(proof_store.stats()["result_keys"], 0)

    def test_reference_commands_signature_symlinks_and_shared_deadline_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root / "fixture")
            proof_store = QfbvProofStore(root / "proofs")
            verifier = fixture.verifier(proof_store)
            query_store = QueryStore(root / "queries")
            query_id, _ = query_store.ingest(_envelope())
            inputs = _proof_inputs(query_store, query_id)
            authorization = verifier.authorize(**inputs)
            proof_body = proof_store.load_proof(
                str(authorization.receipt["proof_sha256"])
            )
            with self.assertRaisesRegex(
                ProofVerificationError, "declaration/assertion problem"
            ):
                verifier.check_proof_body(
                    proof_body,
                    inputs["reference_smt2"] + b"(check-sat)\n",
                    inputs["offsets"],
                )

            linked = fixture.signature_root / "linked"
            linked.symlink_to(fixture.signature_root / "expert", target_is_directory=True)
            with self.assertRaisesRegex(ProofVerificationError, "symlink"):
                fixture.verifier(QfbvProofStore(root / "symlink-proofs"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(
                root / "fixture",
                generator_mode="delay",
                checker_mode="delay",
            )
            proof_store = QfbvProofStore(root / "proofs")
            verifier = fixture.verifier(proof_store, timeout_ms=180)
            query_store = QueryStore(root / "queries")
            query_id, _ = query_store.ingest(_envelope())
            inputs = _proof_inputs(query_store, query_id)
            inputs["timeout_ms"] = 180
            started = time.monotonic()
            with self.assertRaisesRegex(
                ProofVerificationError, "timeout|deadline"
            ):
                verifier.authorize(**inputs)
            self.assertLess(time.monotonic() - started, 0.45)
            self.assertEqual(proof_store.stats()["result_keys"], 0)

    def test_proof_literal_lowering_requires_structural_equivalence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QueryStore(Path(directory) / "queries")
            query_id, _ = store.ingest(_envelope())
            loaded = store.load_query_ir(query_id)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            original = qfbv_backend._bv_constant

            def divergent(value: int, width: int, *, binary: bool = False) -> str:
                if binary and value == 0x42 and width == 8:
                    return "#b01000011"
                return original(value, width, binary=binary)

            with mock.patch.object(
                qfbv_backend, "_bv_constant", side_effect=divergent
            ), self.assertRaisesRegex(
                qfbv_backend.QfBvLoweringError, "structurally equivalent"
            ):
                lower_qfbv_proof_problem(
                    query_id,
                    loaded[0],
                    loaded[1],
                    normalize_qfbv_capabilities(None),
                )

    def test_concurrent_publication_has_one_result_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root / "fixture")
            proof_store = QfbvProofStore(root / "proofs")
            query_store = QueryStore(root / "queries")
            query_id, _ = query_store.ingest(_envelope())
            inputs = _proof_inputs(query_store, query_id)
            receipts: list[str] = []
            failures: list[BaseException] = []

            def publish() -> None:
                try:
                    authorization = fixture.verifier(
                        QfbvProofStore(root / "proofs")
                    ).authorize(**inputs)
                    receipts.append(
                        str(authorization.receipt["receipt_sha256"])
                    )
                except BaseException as error:
                    failures.append(error)

            threads = [threading.Thread(target=publish) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10.0)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(failures, [])
            self.assertEqual(len(set(receipts)), 1)
            self.assertEqual(proof_store.stats()["proofs"], 1)
            self.assertEqual(proof_store.stats()["receipts"], 1)
            self.assertEqual(proof_store.stats()["result_keys"], 1)

    def test_cross_process_publication_enforces_quota_before_cas_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root / "fixture")
            proof_root = root / "proofs"
            QfbvProofStore(proof_root, max_objects=2)

            first_store = QueryStore(root / "query-a")
            first_id, _ = first_store.ingest(_envelope())
            second_envelope = copy.deepcopy(_envelope())
            second_envelope["nodes"][3]["attrs"]["value_hex"] = "43"
            second_store = QueryStore(root / "query-b")
            second_id, _ = second_store.ingest(second_envelope)
            self.assertNotEqual(first_id, second_id)
            work = (
                _proof_inputs(first_store, first_id),
                _proof_inputs(second_store, second_id),
            )

            # Do not fork a process that may already contain solver/MPI native
            # threads from earlier tests. The fixture and inputs are picklable.
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()

            processes = [
                context.Process(
                    target=_publish_proof_receipt_process,
                    args=(fixture, proof_root, item, start, results),
                )
                for item in work
            ]
            for process in processes:
                process.start()
            start.set()
            outcomes = [results.get(timeout=10.0) for _ in processes]
            for process in processes:
                process.join(timeout=10.0)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(sum(kind == "ok" for kind, _ in outcomes), 1)
            self.assertEqual(sum(kind == "error" for kind, _ in outcomes), 1)
            self.assertIn(
                "quota is exhausted",
                next(value for kind, value in outcomes if kind == "error"),
            )
            proof_store = QfbvProofStore(proof_root, max_objects=2)
            stats = proof_store.stats()
            self.assertEqual(stats["proofs"], 1)
            self.assertEqual(stats["receipts"], 1)
            self.assertEqual(stats["result_keys"], 1)
            self.assertGreater(stats["proof_bytes"], 0)
            self.assertEqual(len(list((proof_root / "proofs").rglob("*.cpc"))), 1)
            self.assertEqual(
                len(list((proof_root / "receipts").rglob("*.json"))), 1
            )

    def test_proof_disabled_backends_do_not_enter_proof_lowering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root / "fixture")
            store = QueryStore(root / "queries")
            store.ingest(_envelope())
            one_shot_lease = store.claim("one-shot")
            self.assertIsNotNone(one_shot_lease)
            assert one_shot_lease is not None
            with mock.patch.object(
                qfbv_backend,
                "lower_qfbv_proof_problem",
                side_effect=AssertionError("proof lowering must remain disabled"),
            ):
                result = SmtLibQfbvSolver(
                    store,
                    [sys.executable, str(fixture.solver), "{query}"],
                    name="legacy-one-shot",
                )(one_shot_lease)
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn("proof lowering", result.get("reason", ""))

            incremental = root / "incremental.py"
            incremental.write_text(
                "import sys\n"
                "for line in sys.stdin:\n"
                "    line = line.strip()\n"
                "    if line == '(check-sat)':\n"
                "        print('unsat', flush=True)\n"
                "    elif line.startswith('(echo '):\n"
                "        print(line[6:-1], flush=True)\n",
                encoding="ascii",
            )
            persistent_store = QueryStore(root / "persistent-queries")
            persistent_store.ingest(_envelope())
            persistent_lease = persistent_store.claim("persistent")
            self.assertIsNotNone(persistent_lease)
            assert persistent_lease is not None
            persistent = PersistentSmtLibQfbvSolver(
                persistent_store,
                [sys.executable, "-u", str(incremental)],
                name="legacy-persistent",
                capabilities={"accept_unsat": True, "incremental": True},
            )
            try:
                with mock.patch.object(
                    qfbv_backend,
                    "lower_qfbv_proof_problem",
                    side_effect=AssertionError(
                        "proof lowering must remain disabled"
                    ),
                ):
                    result = persistent(persistent_lease)
            finally:
                persistent.close()
            self.assertEqual(result["status"], "unsat")

    def test_portfolio_parser_preserves_proof_checker_contract(self):
        proof = {
            "format": "cpc",
            "generator_command": ["cvc5", "--dump-proofs", "{query}"],
            "checker_command": ["ethos", "{proof}"],
            "signature_root": "/opt/cvc5/proofs/eo/cpc",
            "timeout_ms": 1234,
        }
        specs = _load_portfolio(json.dumps({
            "solvers": [{
                "kind": "smtlib-qfbv",
                "name": "proof-cvc5",
                "command": ["cvc5", "{query}"],
                "persistent": False,
                "unsat_proof": proof,
            }],
        }))
        self.assertEqual(specs[0]["unsat_proof"]["format"], "cpc")
        self.assertEqual(specs[0]["unsat_proof"]["timeout_ms"], 1234)
        with self.assertRaisesRegex(RuntimeError, "require smtlib-qfbv"):
            _load_portfolio(json.dumps({
                "solvers": [{
                    "kind": "symcc-json",
                    "name": "invalid",
                    "command": ["solver"],
                    "unsat_proof": proof,
                }],
            }))

    def test_query_service_executes_configured_proof_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root / "fixture")
            query_store = QueryStore(root / "queries")
            query_store.ingest(_envelope())
            portfolio = json.dumps({
                "solvers": [{
                    "kind": "smtlib-qfbv",
                    "name": "proof-fixture",
                    "command": [
                        sys.executable,
                        str(fixture.solver),
                        "{query}",
                    ],
                    "persistent": False,
                    "unsat_proof": {
                        "format": "cpc",
                        "generator_command": [
                            sys.executable,
                            str(fixture.generator),
                            "valid",
                            "{query}",
                        ],
                        "checker_command": [
                            sys.executable,
                            str(fixture.checker),
                            "valid",
                            "{proof}",
                        ],
                        "signature_root": str(fixture.signature_root),
                        "generator_trusted_files": [str(fixture.generator)],
                        "checker_trusted_files": [str(fixture.checker)],
                    },
                }],
            })
            output = io.StringIO()
            with redirect_stdout(output):
                returncode = query_service_main([
                    "--store",
                    str(root / "queries"),
                    "--portfolio",
                    portfolio,
                    "--qfbv-proof-store",
                    str(root / "proofs"),
                    "--once",
                ])
            self.assertEqual(returncode, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["worker"]["unsat"], 1)
            self.assertEqual(summary["qfbv_proof_store"]["proofs"], 1)
            self.assertEqual(
                summary["store"]["proof_authorized_unsat_results"], 1
            )

    def test_portfolio_cancels_proof_generation_after_sat_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root / "fixture", generator_mode="sleep")
            query_store = QueryStore(root / "queries")
            query_store.ingest(_envelope())
            lease = query_store.claim("cancel-proof")
            self.assertIsNotNone(lease)
            assert lease is not None
            verifier = fixture.verifier(QfbvProofStore(root / "proofs"))
            slow = fixture.backend(query_store, verifier)
            marker = fixture.root / "generator-started"

            def fast(_lease) -> dict:
                deadline = time.monotonic() + 2.0
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                return {
                    "status": "sat",
                    "assignments": {"0": 0x42},
                    "solver": "fast",
                }

            started = time.monotonic()
            result = PortfolioSolver(
                (("proof", slow), ("fast", fast)),
                parallelism=2,
                cancel_grace_ms=0,
            )(lease)
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertEqual(result["status"], "sat")
            self.assertEqual(result["portfolio"]["cancelled_attempts"], 1)
            self.assertTrue(result["portfolio"]["attempts"][0]["cancelled"])

    @unittest.skipUnless(
        (_PINNED_TOOL_ROOT / "bin" / "cvc5").is_file()
        and (_PINNED_TOOL_ROOT / "bin" / "ethos").is_file(),
        "pinned cvc5 1.3.4 and Ethos are not installed",
    )
    def test_real_cvc5_cpc_ethos_reference_binding(self):
        tool_root = _PINNED_TOOL_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            query_store = QueryStore(root / "queries")
            query_id, _ = query_store.ingest(_envelope())
            proof_store = QfbvProofStore(root / "proofs")
            verifier = QfbvProofVerifier(
                proof_store,
                generator_command=[
                    str(tool_root / "bin" / "cvc5"),
                    "--lang=smt2",
                    "--safe-mode=safe",
                    "--proof-granularity=dsl-rewrite",
                    "--dump-proofs",
                    "{query}",
                ],
                checker_command=[
                    str(tool_root / "bin" / "ethos"),
                    "{proof}",
                ],
                signature_root=tool_root / "share" / "cpc",
            )
            inputs = _proof_inputs(query_store, query_id)
            authorization = verifier.authorize(**inputs)
            self.assertEqual(authorization.receipt["verdict"], "correct")
            proof_body = proof_store.load_proof(
                authorization.receipt["proof_sha256"]
            )
            satisfiable_reference = inputs["reference_smt2"].replace(
                b"#b01000010", b"#b01000001"
            )
            with self.assertRaisesRegex(
                ProofVerificationError, "complete refutation"
            ):
                verifier.check_proof_body(
                    proof_body,
                    satisfiable_reference,
                    inputs["offsets"],
                )

    @unittest.skipUnless(
        (_PINNED_TOOL_ROOT / "bin" / "cvc5").is_file()
        and (_PINNED_TOOL_ROOT / "bin" / "ethos").is_file(),
        "pinned cvc5 1.3.4 and Ethos are not installed",
    )
    def test_real_persistent_backend_commits_checked_unsat(self):
        tool_root = _PINNED_TOOL_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            query_store = QueryStore(root / "queries")
            query_store.ingest(_envelope())
            lease = query_store.claim("persistent-proof")
            self.assertIsNotNone(lease)
            assert lease is not None
            proof_store = QfbvProofStore(root / "proofs")
            verifier = QfbvProofVerifier(
                proof_store,
                generator_command=[
                    str(tool_root / "bin" / "cvc5"),
                    "--lang=smt2",
                    "--safe-mode=safe",
                    "--proof-granularity=dsl-rewrite",
                    "--dump-proofs",
                    "{query}",
                ],
                checker_command=[
                    str(tool_root / "bin" / "ethos"),
                    "{proof}",
                ],
                signature_root=tool_root / "share" / "cpc",
            )
            query_store.register_qfbv_proof_verifier(verifier)
            backend = PersistentSmtLibQfbvSolver(
                query_store,
                [
                    str(tool_root / "bin" / "cvc5"),
                    "--lang=smt2",
                    "--incremental",
                    "--produce-models",
                ],
                name="cvc5-persistent-proof",
                capabilities={"incremental": True},
                proof_verifier=verifier,
            )
            try:
                result = dict(backend(lease))
            finally:
                backend.close()
            self.assertEqual(result["status"], "unsat")
            self.assertTrue(result["backend_unsat_proof_verified"])
            self.assertTrue(
                query_store.complete(lease, "persistent-proof", result)
            )


if __name__ == "__main__":
    unittest.main()
