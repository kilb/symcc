#!/usr/bin/env python3
# REQUIRES: qfbv-z3-forkserver
# RUN: env SYMCC_QFBV_FORKSERVER=%qfbvforkserver python3 %s

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))
sys.path.insert(0, str(ROOT / "test"))

from qf_bv_backend import (  # noqa: E402
    NATIVE_STATE_FORK_PROTOCOL,
    PersistentSmtLibQfbvSolver,
    parse_native_state_fork_metadata,
)
from qf_bv_conformance import build_operator_matrix_envelope  # noqa: E402
from query_store import QueryStore  # noqa: E402
from symcc_query_service import _load_portfolio  # noqa: E402
from test_qf_bv_backend import _envelope  # noqa: E402


def _forkserver() -> Path | None:
    configured = os.environ.get("SYMCC_QFBV_FORKSERVER")
    candidates = [
        Path(configured) if configured else None,
        ROOT / "build" / "symcc-qfbv-z3-forkserver",
        ROOT / "build-llvm17" / "symcc-qfbv-z3-forkserver",
    ]
    return next(
        (candidate for candidate in candidates if candidate and candidate.is_file()),
        None,
    )


FORKSERVER = _forkserver()


def _metadata_text(**overrides: object) -> str:
    values: dict[str, object] = {
        "snapshot-generation": 1,
        "snapshot-queries": 1,
        "warm-checks": 1,
        "forked": 1,
        "child-pid": 123,
        "child-status": "sat",
        "child-timed-out": 0,
        "child-solve-us": 10,
        "fork-roundtrip-us": 20,
        "child-minor-faults": 3,
        "child-major-faults": 0,
        "child-max-rss-kib": 100,
        "warm-status": "sat",
    }
    values.update(overrides)
    rows = "\n".join(f" ({key} {value})" for key, value in values.items())
    return f"sat\n({NATIVE_STATE_FORK_PROTOCOL}\n{rows})\n"


class NativeStateForkProtocolTest(unittest.TestCase):
    def test_metadata_parser_is_fail_closed(self):
        parsed = parse_native_state_fork_metadata(_metadata_text())
        self.assertEqual(
            parsed["backend_native_state_protocol"],
            NATIVE_STATE_FORK_PROTOCOL,
        )
        self.assertEqual(parsed["backend_native_snapshot_generation"], 1)
        self.assertFalse(parsed["backend_native_child_timed_out"])
        for mutation, pattern in (
            (
                _metadata_text(**{"snapshot-generation": 2}),
                "warm-check count",
            ),
            (
                _metadata_text(**{"child-status": "unsat"}),
                "disagrees",
            ),
            (
                _metadata_text(**{"forked": 0}),
                "one child",
            ),
            (
                _metadata_text(**{"child-solve-us": 30}),
                "timing",
            ),
            ("sat\n", "protocol metadata"),
        ):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ValueError, pattern):
                    parse_native_state_fork_metadata(mutation)

    def test_portfolio_contract_is_explicit_and_separate_from_capabilities(self):
        valid = {
            "solvers": [
                {
                    "name": "z3-native",
                    "kind": "smtlib-qfbv",
                    "command": ["symcc-qfbv-z3-forkserver"],
                    "persistent": True,
                    "native_state_fork": True,
                    "capabilities": {"incremental": True},
                }
            ]
        }
        loaded = _load_portfolio(json.dumps(valid))
        self.assertTrue(loaded[0]["native_state_fork"])
        self.assertNotIn("native_state_fork", loaded[0]["capabilities"])
        invalid_type = json.loads(json.dumps(valid))
        invalid_type["solvers"][0]["native_state_fork"] = "true"
        with self.assertRaisesRegex(RuntimeError, "must be Boolean"):
            _load_portfolio(json.dumps(invalid_type))
        nonpersistent = json.loads(json.dumps(valid))
        nonpersistent["solvers"][0]["persistent"] = False
        with self.assertRaisesRegex(RuntimeError, "requires persistence"):
            _load_portfolio(json.dumps(nonpersistent))
        learned = json.loads(json.dumps(valid))
        learned["solvers"][0].update(
            {
                "unsat_proof": {
                    "format": "cpc",
                    "generator_command": ["cvc5", "{query}"],
                    "checker_command": ["ethos", "{proof}"],
                    "signature_root": "/tmp/cpc",
                },
                "learned_lemmas": {"type": "preprocess"},
            }
        )
        with self.assertRaisesRegex(RuntimeError, "cannot publish"):
            _load_portfolio(json.dumps(learned))

    @unittest.skipUnless(FORKSERVER, "Z3 forkserver was not built")
    def test_helper_reuses_and_advances_warmed_snapshot(self):
        assert FORKSERVER is not None
        process = subprocess.Popen(
            [str(FORKSERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        def request(script: str, marker: str) -> str:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(script)
            if script and not script.endswith("\n"):
                process.stdin.write("\n")
            process.stdin.write(f'(echo "{marker}")\n')
            process.stdin.flush()
            rows: list[str] = []
            while True:
                row = process.stdout.readline()
                self.assertTrue(row, "forkserver closed stdout")
                if row.strip() == f'"{marker}"':
                    return "".join(rows)
                rows.append(row)

        try:
            initialization = request(
                "(set-logic QF_BV)\n"
                "(set-option :produce-models true)\n"
                "(set-option :timeout 1000)\n"
                "(declare-fun symcc_input_0 () (_ BitVec 8))\n"
                "(assert (bvuge symcc_input_0 #x40))\n",
                "INIT",
            )
            self.assertEqual(initialization, "")
            first = request(
                "(push 1)\n"
                "(assert (= symcc_input_0 #x42))\n"
                "(check-sat)\n"
                "(get-value (symcc_input_0))\n"
                "(pop 1)\n",
                "FIRST",
            )
            first_metadata = parse_native_state_fork_metadata(first)
            self.assertIn("((symcc_input_0 #x42))", first)
            self.assertEqual(
                first_metadata["backend_native_snapshot_generation"], 1
            )
            second = request(
                "(push 1)\n"
                "(assert (= symcc_input_0 #x43))\n"
                "(check-sat)\n"
                "(get-value (symcc_input_0))\n"
                "(pop 1)\n",
                "SECOND",
            )
            second_metadata = parse_native_state_fork_metadata(second)
            self.assertEqual(
                second_metadata["backend_native_snapshot_generation"], 1
            )
            self.assertEqual(second_metadata["backend_native_snapshot_queries"], 2)
            self.assertNotEqual(
                first_metadata["backend_native_child_pid"],
                second_metadata["backend_native_child_pid"],
            )
            self.assertEqual(
                request(
                    "(assert (bvule symcc_input_0 #x50))\n", "EXTEND"
                ),
                "",
            )
            third = request(
                "(push 1)\n"
                "(assert (= symcc_input_0 #x44))\n"
                "(check-sat)\n"
                "(get-value (symcc_input_0))\n"
                "(pop 1)\n",
                "THIRD",
            )
            third_metadata = parse_native_state_fork_metadata(third)
            self.assertEqual(
                third_metadata["backend_native_snapshot_generation"], 2
            )
            self.assertEqual(third_metadata["backend_native_warm_checks"], 2)
            children = Path(
                f"/proc/{process.pid}/task/{process.pid}/children"
            )
            if children.is_file():
                self.assertEqual(children.read_text(encoding="ascii").strip(), "")
        finally:
            if process.stdin is not None and process.poll() is None:
                process.stdin.write("(exit)\n")
                process.stdin.flush()
            process.wait(timeout=2)
            stderr = process.stderr.read() if process.stderr is not None else ""
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            self.assertEqual(stderr, "")
            self.assertEqual(process.returncode, 0)

    @unittest.skipUnless(FORKSERVER, "Z3 forkserver was not built")
    def test_backend_timeout_kills_only_child_and_reuses_parent_snapshot(self):
        assert FORKSERVER is not None
        with tempfile.TemporaryDirectory() as directory:
            store = QueryStore(directory)
            backend = PersistentSmtLibQfbvSolver(
                store,
                [str(FORKSERVER), "--child-start-delay-ms", "100"],
                name="z3-native-timeout",
                capabilities={"incremental": True},
                native_state_fork=True,
            )

            def solve(target: int, timeout_ms: int, owner: str) -> dict:
                envelope = _envelope(rotate=True)
                envelope["nodes"][3]["attrs"]["value_hex"] = f"{target:02x}"
                envelope["timeout_ms"] = timeout_ms
                store.ingest(envelope)
                lease = store.claim(owner)
                self.assertIsNotNone(lease)
                assert lease is not None
                return dict(backend(lease))

            try:
                timed_out = solve(0x84, 10, "timeout")
                recovered = solve(0x86, 1000, "recovered")
            finally:
                backend.close()
            self.assertEqual(timed_out["status"], "unknown")
            self.assertTrue(timed_out["backend_native_child_timed_out"])
            self.assertEqual(recovered["status"], "sat")
            self.assertEqual(recovered["assignments"], {"0": 0x43})
            self.assertTrue(recovered["prefix_cache_hit"])
            self.assertFalse(recovered["backend_native_child_timed_out"])
            self.assertEqual(
                recovered["backend_native_snapshot_generation"],
                timed_out["backend_native_snapshot_generation"],
            )
            self.assertEqual(recovered["backend_native_snapshot_queries"], 2)

    @unittest.skipUnless(FORKSERVER, "Z3 forkserver was not built")
    def test_backend_accepts_full_operator_matrix_and_store_revalidation(self):
        assert FORKSERVER is not None
        with tempfile.TemporaryDirectory() as directory:
            store = QueryStore(directory)
            query_id, _ = store.ingest(build_operator_matrix_envelope())
            lease = store.claim("operator-matrix")
            self.assertIsNotNone(lease)
            assert lease is not None
            backend = PersistentSmtLibQfbvSolver(
                store,
                [str(FORKSERVER)],
                name="z3-native-matrix",
                capabilities={"incremental": True},
                native_state_fork=True,
            )
            try:
                result = dict(backend(lease))
                self.assertTrue(store.complete(lease, "operator-matrix", result))
            finally:
                backend.close()
            self.assertEqual(result["status"], "sat")
            self.assertEqual(result["assignments"], {"0": 0x42, "1": 0x03})
            self.assertTrue(result["backend_model_verified"])
            self.assertEqual(
                result["lowering_certificate"]["query_id"], query_id
            )
            self.assertEqual(
                result["backend_native_state_protocol"],
                NATIVE_STATE_FORK_PROTOCOL,
            )

    @unittest.skipUnless(FORKSERVER, "Z3 forkserver was not built")
    def test_backend_solves_a_constant_target_without_get_value(self):
        assert FORKSERVER is not None
        with tempfile.TemporaryDirectory() as directory:
            store = QueryStore(directory)
            envelope = _envelope(rotate=True)
            envelope["prefix_roots"] = []
            envelope["target_root"] = 5
            store.ingest(envelope)
            lease = store.claim("constant-target")
            self.assertIsNotNone(lease)
            assert lease is not None
            backend = PersistentSmtLibQfbvSolver(
                store,
                [str(FORKSERVER)],
                name="z3-native-constant",
                capabilities={"incremental": True},
                native_state_fork=True,
            )
            try:
                result = dict(backend(lease))
            finally:
                backend.close()
            self.assertEqual(result["status"], "sat")
            self.assertEqual(result["assignments"], {})
            self.assertTrue(result["backend_model_verified"])

    def test_backend_rejects_a_solver_that_omits_native_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "not_native.py"
            helper.write_text(
                "import sys\n"
                "for line in sys.stdin:\n"
                " line=line.strip()\n"
                " if line == '(check-sat)': print('sat', flush=True)\n"
                " elif line.startswith('(get-value'):\n"
                "  print('((symcc_input_0 #x42))', flush=True)\n"
                " elif line.startswith('(echo '): print(line[6:-1], flush=True)\n",
                encoding="ascii",
            )
            store = QueryStore(root / "store")
            store.ingest(_envelope(rotate=True))
            lease = store.claim("missing-metadata")
            self.assertIsNotNone(lease)
            assert lease is not None
            backend = PersistentSmtLibQfbvSolver(
                store,
                [sys.executable, "-u", str(helper)],
                name="not-native",
                capabilities={"incremental": True},
                native_state_fork=True,
            )
            try:
                result = dict(backend(lease))
            finally:
                backend.close()
            self.assertEqual(result["status"], "error")
            self.assertIn("protocol metadata", result["reason"])
            self.assertEqual(result["prefix_cache_entries"], 0)

    def test_backend_rejects_regressing_native_query_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "regressing_native.py"
            metadata = _metadata_text().splitlines()[1:]
            metadata_script = repr("\n".join(metadata))
            helper.write_text(
                "import sys\n"
                f"metadata={metadata_script}\n"
                "for line in sys.stdin:\n"
                " line=line.strip()\n"
                " if line == '(check-sat)': print('sat', flush=True)\n"
                " elif line.startswith('(get-value'):\n"
                "  print('((symcc_input_0 #x42))', flush=True)\n"
                " elif line == '(pop 1)': print(metadata, flush=True)\n"
                " elif line.startswith('(echo '): print(line[6:-1], flush=True)\n",
                encoding="ascii",
            )
            store = QueryStore(root / "store")
            store.ingest(_envelope(rotate=True))
            lease = store.claim("regression")
            self.assertIsNotNone(lease)
            assert lease is not None
            backend = PersistentSmtLibQfbvSolver(
                store,
                [sys.executable, "-u", str(helper)],
                name="regressing-native",
                capabilities={"incremental": True},
                native_state_fork=True,
            )
            try:
                first = dict(backend(lease))
                second = dict(backend(lease))
            finally:
                backend.close()
            self.assertEqual(first["status"], "sat")
            self.assertEqual(second["status"], "error")
            self.assertIn("sequence is not monotonic", second["reason"])
            self.assertEqual(second["prefix_cache_entries"], 0)


if __name__ == "__main__":
    unittest.main()
