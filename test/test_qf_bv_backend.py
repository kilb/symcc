#!/usr/bin/env python3
# RUN: python3 %s

import hashlib
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qf_bv_backend import (  # noqa: E402
    CAPABILITY_SCHEMA,
    LOWERING_SCHEMA,
    PersistentSmtLibQfbvSolver,
    SmtLibQfbvSolver,
    lower_qfbv_query,
    normalize_qfbv_capabilities,
    parse_qfbv_response,
)
from qf_bv_conformance import (  # noqa: E402
    build_operator_matrix_envelope,
)
from query_store import PortfolioSolver, QueryStore, solve_one  # noqa: E402
from symcc_query_service import _load_portfolio, main as query_service_main  # noqa: E402
from cross_worker_context import CrossWorkerContextStore  # noqa: E402


def _envelope(
    *,
    contradictory: bool = False,
    rotate: bool = False,
) -> dict:
    nodes = [
        {
            "id": 0,
            "op": "read",
            "bits": 8,
            "children": [],
            "attrs": {"index": 0},
        },
    ]
    if rotate:
        nodes.extend([
            {
                "id": 1,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "01"},
            },
            {
                "id": 2,
                "op": "rol",
                "bits": 8,
                "children": [0, 1],
                "attrs": {},
            },
            {
                "id": 3,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "84"},
            },
            {
                "id": 4,
                "op": "equal",
                "bits": 1,
                "children": [2, 3],
                "attrs": {},
            },
            {
                "id": 5,
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": True},
            },
        ])
        prefix_roots = [5]
        target_root = 4
    else:
        nodes.extend([
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
            {
                "id": 5,
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": True},
            },
        ])
        prefix_roots = [2] if contradictory else [5]
        target_root = 4
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "qfbv-test",
        "nodes": nodes,
        "prefix_roots": prefix_roots,
        "target_root": target_root,
        "input_hex": "00",
        "timeout_ms": 2000,
        "metadata": {"source": "qfbv-test"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


def _prefix_chain_envelope(prefix_depth: int, target_value: int) -> dict:
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
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": "40"},
        },
        {
            "id": 2,
            "op": "uge",
            "bits": 1,
            "children": [0, 1],
            "attrs": {},
        },
        {
            "id": 3,
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": "50"},
        },
        {
            "id": 4,
            "op": "ule",
            "bits": 1,
            "children": [0, 3],
            "attrs": {},
        },
        {
            "id": 5,
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": f"{target_value:02x}"},
        },
        {
            "id": 6,
            "op": "equal",
            "bits": 1,
            "children": [0, 5],
            "attrs": {},
        },
    ]
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "qfbv-context-test",
        "nodes": nodes,
        "prefix_roots": [2, 4][:prefix_depth],
        "target_root": 6,
        "input_hex": "00",
        "timeout_ms": 2000,
        "metadata": {"source": "qfbv-context-test"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


class QfBvBackendTest(unittest.TestCase):
    def test_query_service_exposes_shared_context_stats_and_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    query_service_main([
                        "--store",
                        str(Path(directory) / "queries"),
                        "--qfbv-context-store",
                        str(Path(directory) / "contexts"),
                        "--stats",
                    ]),
                    0,
                )
            stats = json.loads(output.getvalue())
            self.assertIn("store", stats)
            self.assertEqual(stats["qfbv_context_store"]["contexts"], 0)
            with self.assertRaisesRegex(ValueError, "max-active"):
                query_service_main([
                    "--store",
                    str(Path(directory) / "invalid"),
                    "--qfbv-context-max-active",
                    "0",
                    "--stats",
                ])

    def test_capability_and_response_protocol_are_canonical(self):
        capabilities = normalize_qfbv_capabilities({
            "operators": ["equal", "read", "constant", "bool"],
            "max_bits": 64,
            "accept_unsat": True,
        })
        self.assertEqual(capabilities["schema"], CAPABILITY_SCHEMA)
        self.assertEqual(
            capabilities["operators"],
            ["bool", "constant", "equal", "read"],
        )
        legacy = dict(capabilities)
        legacy.pop("capability_sha256")
        legacy.pop("incremental")
        legacy["capability_sha256"] = hashlib.sha256(
            json.dumps(
                legacy, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
        ).hexdigest()
        normalized_legacy = QueryStore._validate_result({
            "status": "unknown",
            "assignments": {},
            "solver": "legacy",
            "backend_kind": "smtlib-qfbv",
            "backend_capabilities": legacy,
            "capability_status": "unsupported",
        })
        self.assertNotIn(
            "incremental", normalized_legacy["backend_capabilities"])
        status, assignments = parse_qfbv_response(
            "success\nsat\n((symcc_input_0 #x42)"
            " (symcc_input_1 (_ bv67 8)))\n",
            (0, 1),
        )
        self.assertEqual(status, "sat")
        self.assertEqual(assignments, {"0": 66, "1": 67})
        with self.assertRaises(ValueError):
            parse_qfbv_response(
                "sat\n((symcc_input_0 #x42))\n", (0, 1))

    def test_lowering_certificate_and_capability_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QueryStore(directory)
            query_id, _ = store.ingest(_envelope(rotate=True))
            loaded = store.load_query_ir(query_id)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            smt2, certificate, offsets = lower_qfbv_query(
                query_id,
                loaded[0],
                loaded[1],
                normalize_qfbv_capabilities(None),
            )
            self.assertEqual(certificate["schema"], LOWERING_SCHEMA)
            self.assertTrue(certificate["sort_verified"])
            self.assertEqual(certificate["operator_counts"]["rol"], 1)
            self.assertEqual(offsets, (0,))
            self.assertIn("(bvurem", smt2)

            lease = store.claim("unsupported")
            self.assertIsNotNone(lease)
            assert lease is not None
            backend = SmtLibQfbvSolver(
                store,
                ["/command/must/not/run"],
                name="restricted",
                capabilities={
                    "operators": ["bool", "constant", "equal", "read"],
                },
            )
            result = backend(lease)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["capability_status"], "unsupported")
            self.assertIn("rol", result["reason"])

    def test_portfolio_parser_preserves_qfbv_capabilities(self):
        specs = _load_portfolio(json.dumps({
            "solvers": [{
                "kind": "smtlib-qfbv",
                "name": "cvc5",
                "command": [
                    "cvc5", "--lang", "smt2", "--incremental",
                    "--produce-models",
                ],
                "persistent": True,
                "prefix_cache": 7,
                "capabilities": {
                    "accept_unsat": True,
                    "incremental": True,
                    "max_bits": 2048,
                },
            }],
        }))
        self.assertEqual(specs[0]["kind"], "smtlib-qfbv")
        self.assertTrue(specs[0]["persistent"])
        self.assertEqual(specs[0]["prefix_cache"], 7)
        self.assertEqual(
            specs[0]["capabilities"]["schema"], CAPABILITY_SCHEMA)
        self.assertTrue(specs[0]["capabilities"]["accept_unsat"])
        self.assertTrue(specs[0]["capabilities"]["incremental"])
        with self.assertRaisesRegex(RuntimeError, "advertise incremental"):
            _load_portfolio(json.dumps({
                "solvers": [{
                    "kind": "smtlib-qfbv",
                    "name": "invalid",
                    "command": ["cvc5"],
                    "persistent": True,
                }],
            }))

    def test_persistent_timeout_drops_context_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_incremental.py"
            first_check = root / "first-check"
            script.write_text(
                "import pathlib, sys, time\n"
                "flag = pathlib.Path(sys.argv[1])\n"
                "for line in sys.stdin:\n"
                "    line = line.strip()\n"
                "    if line == '(check-sat)':\n"
                "        if not flag.exists():\n"
                "            flag.write_text('seen')\n"
                "            time.sleep(10)\n"
                "        print('sat', flush=True)\n"
                "    elif line.startswith('(get-value'):\n"
                "        print('((symcc_input_0 #x42))', flush=True)\n"
                "    elif line.startswith('(echo '):\n"
                "        print(line[6:-1], flush=True)\n",
                encoding="ascii",
            )
            store = QueryStore(root / "store")
            envelope = _envelope(rotate=True)
            envelope["timeout_ms"] = 1
            store.ingest(envelope)
            lease = store.claim("recovery")
            self.assertIsNotNone(lease)
            assert lease is not None
            backend = PersistentSmtLibQfbvSolver(
                store,
                [sys.executable, "-u", str(script), str(first_check)],
                name="fake-incremental",
                capabilities={"incremental": True},
                prefix_cache_entries=1,
            )
            closed_contexts = []
            close_context = backend._close_context

            def track_close(context):
                close_context(context)
                closed_contexts.append(context)

            backend._close_context = track_close
            try:
                timed_out = dict(backend(lease))
                self.assertEqual(len(closed_contexts), 1)
                timed_out_process = closed_contexts[0].process
                self.assertIsNotNone(timed_out_process.poll())
                self.assertTrue(timed_out_process.stdin.closed)
                self.assertTrue(timed_out_process.stdout.closed)
                self.assertTrue(timed_out_process.stderr.closed)
                recovered = dict(backend(lease))
            finally:
                backend.close()
            self.assertEqual(timed_out["status"], "unknown")
            self.assertIn("timeout", timed_out["reason"])
            self.assertEqual(timed_out["prefix_cache_entries"], 0)
            self.assertEqual(recovered["status"], "sat")
            self.assertEqual(recovered["assignments"], {"0": 0x42})
            self.assertFalse(recovered["prefix_cache_hit"])

    def test_portfolio_cancels_one_shot_qfbv_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "qfbv-started"
            script = (
                "import pathlib,time;"
                f"pathlib.Path({str(marker)!r}).write_text('1');"
                "time.sleep(5)"
            )
            store = QueryStore(root / "store")
            store.ingest(_envelope(rotate=True))
            lease = store.claim("cancel-qfbv")
            self.assertIsNotNone(lease)
            assert lease is not None
            slow = SmtLibQfbvSolver(
                store,
                [sys.executable, "-c", script],
                name="slow-qfbv",
            )

            def fast(_lease) -> dict:
                deadline = time.monotonic() + 5.0
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertTrue(
                    marker.exists(),
                    "slow backend did not reach its cancellable request",
                )
                return {
                    "status": "sat",
                    "assignments": {"0": 0x42},
                    "solver": "fast",
                }

            started = time.monotonic()
            result = PortfolioSolver((
                ("slow-qfbv", slow),
                ("fast", fast),
            ), parallelism=2, cancel_grace_ms=0)(lease)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(result["status"], "sat")
            self.assertEqual(result["portfolio"]["cancelled_attempts"], 1)
            self.assertTrue(result["portfolio"]["attempts"][0]["cancelled"])

    def test_cancelled_incremental_qfbv_context_recovers_cold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "cancel_incremental.py"
            first_check = root / "first-check"
            script.write_text(
                "import pathlib, sys, time\n"
                "flag = pathlib.Path(sys.argv[1])\n"
                "for line in sys.stdin:\n"
                "    line = line.strip()\n"
                "    if line == '(check-sat)':\n"
                "        if not flag.exists():\n"
                "            flag.write_text('seen')\n"
                "            time.sleep(10)\n"
                "        print('sat', flush=True)\n"
                "    elif line.startswith('(get-value'):\n"
                "        print('((symcc_input_0 #x42))', flush=True)\n"
                "    elif line.startswith('(echo '):\n"
                "        print(line[6:-1], flush=True)\n",
                encoding="ascii",
            )
            store = QueryStore(root / "store")
            envelope = _envelope(rotate=True)
            envelope["timeout_ms"] = 10000
            store.ingest(envelope)
            lease = store.claim("cancel-incremental")
            self.assertIsNotNone(lease)
            assert lease is not None
            backend = PersistentSmtLibQfbvSolver(
                store,
                [sys.executable, "-u", str(script), str(first_check)],
                name="cancel-incremental",
                capabilities={"incremental": True},
                prefix_cache_entries=1,
            )

            def fast(_lease) -> dict:
                deadline = time.monotonic() + 5.0
                while not first_check.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertTrue(
                    first_check.exists(),
                    "incremental backend did not reach its cancellable request",
                )
                return {
                    "status": "sat",
                    "assignments": {"0": 0x42},
                    "solver": "fast",
                }

            try:
                cancelled = PortfolioSolver((
                    ("incremental", backend),
                    ("fast", fast),
                ), parallelism=2, cancel_grace_ms=0)(lease)
                self.assertEqual(
                    cancelled["portfolio"]["cancelled_attempts"], 1)
                recovered = dict(backend(lease))
            finally:
                backend.close()
            self.assertEqual(recovered["status"], "sat")
            self.assertEqual(recovered["assignments"], {"0": 0x42})
            self.assertFalse(recovered["prefix_cache_hit"])

    def test_incremental_cancel_before_backend_registration_is_sticky(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "cancel_before_begin.py"
            first_check = root / "first-check"
            script.write_text(
                "import pathlib, sys, time\n"
                "flag = pathlib.Path(sys.argv[1])\n"
                "for line in sys.stdin:\n"
                "    line = line.strip()\n"
                "    if line == '(check-sat)':\n"
                "        if not flag.exists():\n"
                "            flag.write_text('seen')\n"
                "            time.sleep(10)\n"
                "        print('sat', flush=True)\n"
                "    elif line.startswith('(get-value'):\n"
                "        print('((symcc_input_0 #x42))', flush=True)\n"
                "    elif line.startswith('(echo '):\n"
                "        print(line[6:-1], flush=True)\n",
                encoding="ascii",
            )
            store = QueryStore(root / "store")
            envelope = _envelope(rotate=True)
            envelope["timeout_ms"] = 10000
            store.ingest(envelope)
            lease = store.claim("cancel-before-begin")
            self.assertIsNotNone(lease)
            assert lease is not None

            entered = threading.Event()
            release = threading.Event()

            class DelayedPersistentSolver(PersistentSmtLibQfbvSolver):
                def __call__(self, current_lease):
                    entered.set()
                    self.assert_release()
                    return super().__call__(current_lease)

                def assert_release(self):
                    if not release.wait(timeout=2.0):
                        raise RuntimeError("cancel did not release delayed backend")

                def cancel(self, current_lease):
                    try:
                        return super().cancel(current_lease)
                    finally:
                        first_check.write_text("cancelled", encoding="ascii")
                        release.set()

            backend = DelayedPersistentSolver(
                store,
                [sys.executable, "-u", str(script), str(first_check)],
                name="cancel-before-begin",
                capabilities={"incremental": True},
                prefix_cache_entries=1,
            )

            def fast(_lease) -> dict:
                self.assertTrue(entered.wait(timeout=1.0))
                return {
                    "status": "sat",
                    "assignments": {"0": 0x42},
                    "solver": "fast",
                }

            try:
                cancelled = PortfolioSolver((
                    ("incremental", backend),
                    ("fast", fast),
                ), parallelism=2, cancel_grace_ms=0)(lease)
                self.assertEqual(
                    cancelled["portfolio"]["cancelled_attempts"], 1)
                recovered = dict(backend(lease))
            finally:
                backend.close()
            self.assertEqual(recovered["status"], "sat")
            self.assertEqual(recovered["assignments"], {"0": 0x42})
            self.assertFalse(recovered["prefix_cache_hit"])

    @unittest.skipUnless(shutil.which("cvc5"), "cvc5 is not installed")
    def test_real_cvc5_sat_model_is_verified_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QueryStore(directory)
            query_id, _ = store.ingest(_envelope(rotate=True))
            backend = SmtLibQfbvSolver(
                store,
                ["cvc5", "--lang", "smt2", "--produce-models", "{query}"],
                name="cvc5-qfbv",
            )
            self.assertEqual(solve_one(store, "cvc5", backend), "sat")
            result_path = (
                Path(directory) / "results" / query_id[:2] /
                f"{query_id}.json"
            )
            result = json.loads(result_path.read_text(encoding="ascii"))
            self.assertEqual(result["assignments"], {"0": 0x42})
            self.assertTrue(result["backend_model_verified"])
            self.assertEqual(
                result["lowering_certificate"]["query_id"], query_id)
            candidates = sorted(
                (Path(directory) / "candidates").glob("*/*.bin"))
            self.assertEqual([path.read_bytes() for path in candidates], [b"B"])

    @unittest.skipUnless(shutil.which("cvc5"), "cvc5 is not installed")
    def test_real_cvc5_accepts_the_full_query_ir_operator_matrix(self):
        envelope = build_operator_matrix_envelope()
        with tempfile.TemporaryDirectory() as directory:
            store = QueryStore(directory)
            query_id, _ = store.ingest(envelope)
            backend = SmtLibQfbvSolver(
                store,
                ["cvc5", "--lang", "smt2", "--produce-models", "{query}"],
                name="cvc5-qfbv",
            )
            self.assertEqual(solve_one(store, "operator-matrix", backend), "sat")
            with store._connect() as database:
                raw = database.execute(
                    "SELECT result_json FROM results "
                    "WHERE query_id = ?", (query_id,)).fetchone()[0]
            result = json.loads(raw)
            self.assertEqual(result["assignments"], {"0": 0x42, "1": 0x03})
            self.assertEqual(
                set(result["lowering_certificate"]["operator_counts"]),
                {
                    "add", "and", "ashr", "bool", "concat", "constant",
                    "distinct", "equal", "extract", "ite", "land", "lnot",
                    "lor", "lshr", "mul", "neg", "not", "or", "read", "rol",
                    "ror", "sdiv", "sext", "sge", "sgt", "shl", "sle", "slt",
                    "srem", "sub", "udiv", "uge", "ugt", "ule", "ult", "urem",
                    "xor", "zext",
                },
            )

    @unittest.skipUnless(shutil.which("cvc5"), "cvc5 is not installed")
    def test_persistent_cvc5_reuses_and_evicts_prefix_contexts(self):
        def rotated(target: int) -> dict:
            envelope = _envelope(rotate=True)
            envelope["nodes"][3]["attrs"]["value_hex"] = f"{target:02x}"
            return envelope

        with tempfile.TemporaryDirectory() as directory:
            store = QueryStore(directory)
            backend = PersistentSmtLibQfbvSolver(
                store,
                [
                    "cvc5", "--lang", "smt2", "--incremental",
                    "--produce-models",
                ],
                name="cvc5-incremental",
                capabilities={"incremental": True},
                prefix_cache_entries=1,
            )

            def run(envelope: dict, owner: str) -> dict:
                query_id, _ = store.ingest(envelope)
                lease = store.claim(owner)
                self.assertIsNotNone(lease)
                assert lease is not None
                result = dict(backend(lease))
                self.assertTrue(store.complete(lease, owner, result))
                self.assertEqual(
                    result["backend_context_protocol"],
                    "smtlib-prefix-process-push-pop-v1",
                )
                self.assertEqual(
                    result["lowering_certificate"]["query_id"], query_id)
                return result

            try:
                first = run(rotated(0x84), "first")
                second = run(rotated(0x86), "second")
                other = run(_envelope(contradictory=True), "other")
                after_eviction = run(rotated(0x88), "after-eviction")
            finally:
                backend.close()

            self.assertEqual(first["status"], "sat")
            self.assertEqual(first["assignments"], {"0": 0x42})
            self.assertFalse(first["prefix_cache_hit"])
            self.assertEqual(second["status"], "sat")
            self.assertEqual(second["assignments"], {"0": 0x43})
            self.assertTrue(second["prefix_cache_hit"])
            self.assertEqual(other["status"], "unknown")
            self.assertEqual(other["backend_status"], "unsat")
            self.assertFalse(other["prefix_cache_hit"])
            self.assertEqual(after_eviction["status"], "sat")
            self.assertEqual(after_eviction["assignments"], {"0": 0x44})
            self.assertFalse(after_eviction["prefix_cache_hit"])
            self.assertEqual(after_eviction["prefix_cache_entries"], 1)

    @unittest.skipUnless(shutil.which("cvc5"), "cvc5 is not installed")
    def test_cross_worker_context_reconstructs_exact_and_extends_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            query_store = QueryStore(root / "queries")
            context_store = CrossWorkerContextStore(root / "contexts")
            command = [
                "cvc5",
                "--lang",
                "smt2",
                "--incremental",
                "--produce-models",
            ]

            def claim(envelope: dict, owner: str):
                query_store.ingest(envelope)
                lease = query_store.claim(owner)
                self.assertIsNotNone(lease)
                return lease

            first_lease = claim(_prefix_chain_envelope(1, 0x42), "worker-a-1")
            assert first_lease is not None
            first_backend = PersistentSmtLibQfbvSolver(
                query_store,
                command,
                name="cvc5-shared-a",
                capabilities={"incremental": True},
                prefix_cache_entries=2,
                shared_context_store=context_store,
                context_owner="worker-a",
            )
            try:
                first = dict(first_backend(first_lease))
                self.assertTrue(
                    query_store.complete(first_lease, "worker-a-1", first)
                )
                second_lease = claim(
                    _prefix_chain_envelope(2, 0x43), "worker-a-2"
                )
                assert second_lease is not None
                second = dict(first_backend(second_lease))
                self.assertTrue(
                    query_store.complete(second_lease, "worker-a-2", second)
                )
            finally:
                first_backend.close()

            self.assertEqual(first["status"], "sat")
            self.assertEqual(first["assignments"], {"0": 0x42})
            self.assertFalse(first["backend_shared_context_exact_hit"])
            self.assertFalse(first["backend_parent_context_reused"])
            self.assertEqual(first["backend_shared_context_depth"], 1)
            self.assertEqual(second["status"], "sat")
            self.assertEqual(second["assignments"], {"0": 0x43})
            self.assertTrue(second["backend_parent_context_reused"])
            self.assertEqual(
                second["backend_shared_context_materialization"], "leased"
            )
            self.assertEqual(second["backend_shared_context_created"], 1)
            self.assertEqual(second["backend_shared_context_existing"], 1)

            third_lease = claim(_prefix_chain_envelope(2, 0x44), "worker-b")
            assert third_lease is not None
            second_backend = PersistentSmtLibQfbvSolver(
                query_store,
                command,
                name="cvc5-shared-b",
                capabilities={"incremental": True},
                prefix_cache_entries=2,
                shared_context_store=CrossWorkerContextStore(root / "contexts"),
                context_owner="worker-b",
            )
            try:
                third = dict(second_backend(third_lease))
                self.assertTrue(
                    query_store.complete(third_lease, "worker-b", third)
                )
                fourth_lease = claim(
                    _prefix_chain_envelope(2, 0x45), "worker-b-tamper"
                )
                assert fourth_lease is not None
                fourth = dict(second_backend(fourth_lease))
                tampered = dict(fourth)
                tampered["backend_shared_context_sha256"] = "0" * 64
                with self.assertRaisesRegex(
                    ValueError, "identity does not match Query IR"
                ):
                    query_store.complete(
                        fourth_lease, "worker-b-tamper", tampered
                    )
                self.assertTrue(
                    query_store.complete(
                        fourth_lease, "worker-b-tamper", fourth
                    )
                )
            finally:
                second_backend.close()
            self.assertEqual(third["status"], "sat")
            self.assertEqual(third["assignments"], {"0": 0x44})
            self.assertTrue(third["backend_shared_context_exact_hit"])
            self.assertFalse(third["prefix_cache_hit"])
            self.assertFalse(third["backend_parent_context_reused"])
            self.assertEqual(
                third["backend_shared_context_materialization"], "leased"
            )
            self.assertEqual(context_store.stats()["contexts"], 2)
            query_stats = query_store.stats()
            self.assertEqual(query_stats["cross_worker_context_results"], 4)
            self.assertEqual(query_stats["cross_worker_context_exact_hits"], 2)
            self.assertEqual(query_stats["cross_worker_context_parent_reuses"], 1)
            self.assertEqual(
                query_stats["cross_worker_context_quota_timeouts"], 0
            )

            held = context_store.claim_materialization(
                third["backend_shared_context_sha256"],
                "held-by-other-worker",
                lease_seconds=30.0,
            )
            self.assertIsNotNone(held)
            fifth_envelope = _prefix_chain_envelope(2, 0x46)
            fifth_envelope["timeout_ms"] = 20
            fifth_lease = claim(fifth_envelope, "worker-c")
            assert fifth_lease is not None
            third_backend = PersistentSmtLibQfbvSolver(
                query_store,
                command,
                name="cvc5-shared-c",
                capabilities={"incremental": True},
                prefix_cache_entries=2,
                shared_context_store=CrossWorkerContextStore(
                    root / "contexts"
                ),
                context_owner="worker-c",
            )
            try:
                fifth = dict(third_backend(fifth_lease))
                self.assertTrue(
                    query_store.complete(fifth_lease, "worker-c", fifth)
                )
            finally:
                third_backend.close()
                assert held is not None
                context_store.release_materialization(held)
            self.assertEqual(fifth["status"], "unknown")
            self.assertEqual(
                fifth["backend_shared_context_materialization"],
                "quota-timeout",
            )
            self.assertEqual(
                query_store.stats()["cross_worker_context_quota_timeouts"],
                1,
            )

            held_for_cancel = context_store.claim_materialization(
                third["backend_shared_context_sha256"],
                "held-for-cancellation",
                lease_seconds=30.0,
            )
            self.assertIsNotNone(held_for_cancel)
            sixth_lease = claim(
                _prefix_chain_envelope(2, 0x47), "worker-d"
            )
            assert sixth_lease is not None
            fourth_backend = PersistentSmtLibQfbvSolver(
                query_store,
                ["/command/must/not/run"],
                name="cancel-shared-context",
                capabilities={"incremental": True},
                prefix_cache_entries=1,
                shared_context_store=CrossWorkerContextStore(
                    root / "contexts"
                ),
                context_owner="worker-d",
            )
            holder: dict[str, dict] = {}
            thread = threading.Thread(
                target=lambda: holder.setdefault(
                    "result", dict(fourth_backend(sixth_lease))
                )
            )
            thread.start()
            time.sleep(0.05)
            self.assertTrue(fourth_backend.cancel(sixth_lease))
            thread.join(timeout=1.0)
            fourth_backend.close()
            assert held_for_cancel is not None
            context_store.release_materialization(held_for_cancel)
            self.assertFalse(thread.is_alive())
            self.assertEqual(holder["result"]["status"], "unknown")
            self.assertTrue(holder["result"]["cancelled"])

    @unittest.skipUnless(shutil.which("cvc5"), "cvc5 is not installed")
    def test_unsat_requires_explicit_backend_authorization(self):
        command = [
            "cvc5", "--lang", "smt2", "--produce-models", "{query}",
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = QueryStore(directory)
            store.ingest(_envelope(contradictory=True))
            backend = SmtLibQfbvSolver(
                store, command, name="cvc5-observation")
            self.assertEqual(solve_one(store, "untrusted", backend), "unknown")
            with store._connect() as database:
                raw = database.execute(
                    "SELECT result_json FROM results").fetchone()[0]
            result = json.loads(raw)
            self.assertEqual(result["backend_status"], "unsat")
            self.assertFalse(result["backend_unsat_authorized"])

        with tempfile.TemporaryDirectory() as directory:
            store = QueryStore(directory)
            store.ingest(_envelope(contradictory=True))
            backend = SmtLibQfbvSolver(
                store,
                command,
                name="cvc5-exact",
                capabilities={"accept_unsat": True},
            )
            self.assertEqual(solve_one(store, "trusted", backend), "unsat")

    @unittest.skipUnless(shutil.which("cvc5"), "cvc5 is not installed")
    def test_store_rejects_model_tampering_after_backend_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QueryStore(directory)
            store.ingest(_envelope(rotate=True))
            lease = store.claim("tamper")
            self.assertIsNotNone(lease)
            assert lease is not None
            backend = SmtLibQfbvSolver(
                store,
                ["cvc5", "--lang", "smt2", "--produce-models", "{query}"],
                name="cvc5-qfbv",
            )
            result = dict(backend(lease))
            self.assertEqual(result["status"], "sat")
            certificate_tamper = json.loads(json.dumps(result))
            counts = certificate_tamper[
                "lowering_certificate"]["operator_counts"]
            counts["rol"] += 1
            certificate = certificate_tamper["lowering_certificate"]
            certificate_body = dict(certificate)
            certificate_body.pop("certificate_sha256")
            certificate["certificate_sha256"] = hashlib.sha256(
                json.dumps(
                    certificate_body,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
            ).hexdigest()
            with self.assertRaisesRegex(
                    ValueError, "operator counts"):
                store._validate_result(certificate_tamper)

            result["assignments"] = {"0": 0}
            with self.assertRaisesRegex(
                    ValueError, "independent store validation"):
                store.complete(lease, "tamper", result)


if __name__ == "__main__":
    unittest.main()
