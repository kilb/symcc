#!/usr/bin/env python3
# RUN: python3 %s

import hashlib
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qfbv_incremental_proof import (  # noqa: E402
    CLAUSE_PROTOCOL,
    CLAUSE_RECORD_SCHEMA,
    PROJECT_LRUP_DAG_SCHEMA,
    PROOF_FRAGMENT_SCHEMA,
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
    make_unsat_result_receipt,
)
from qfbv_incremental_sat import (  # noqa: E402
    bitblast_qfbv_query,
    extend_bitblast_assumptions,
)
from cadical_qfbv_backend import CadicalQfbvSolver  # noqa: E402
from qfbv_proof_wire import (  # noqa: E402
    LIDRUP_CHECKER_COMMIT,
    PALRUP_BINARY_PROTOCOL,
    LidrupExternalChecker,
    LidrupWireStore,
    PalrupDelete,
    PalrupFragmentOracle,
    PalrupImport,
    PalrupProduce,
    ProofWireError,
    decode_palrup_fragment,
    encode_palrup_fragment,
    export_lidrup_artifacts,
    import_lidrup_artifacts,
    palrup_fragment_text,
)
from query_store import QueryStore  # noqa: E402


def _canonical_digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()


def _plan():
    return bitblast_qfbv_query(
        "proof-wire-test",
        ["true", "false"],
        {
            "true": {
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": True},
            },
            "false": {
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": False},
            },
        },
    )


def _envelope():
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "proof-wire-test",
        "nodes": [
            {
                "id": 0,
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": True},
            },
            {
                "id": 1,
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": False},
            },
        ],
        "prefix_roots": [0],
        "target_root": 1,
        "input_hex": "",
        "timeout_ms": 2_000,
        "metadata": {"source": "proof-wire-test"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert false)\n",
    }


def _publish_simple(plan, store):
    record = make_rup_clause_record(
        plan,
        tuple(-literal for literal in plan.assumptions),
        dependency_assumptions=plan.assumptions,
        source_worker="wire-source",
        worker_epoch=2,
        sequence=7,
    )
    digest, _created = store.publish(record)
    return record, make_unsat_result_receipt(plan, digest, plan.assumptions)


def _write_tool(root, body, name="tool.py"):
    path = root / name
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="ascii")
    path.chmod(0o755)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _checker_tool(root, *, outcome="verified"):
    behavior = {
        "verified": "print('s VERIFIED')\n",
        "missing": "print('s NOT_VERIFIED')\n",
        "stderr": "print('s VERIFIED')\nprint('diagnostic', file=sys.stderr)\n",
        "large": "print('x' * 70000)\n",
        "sleep": "time.sleep(30)\n",
    }[outcome]
    return _write_tool(
        root,
        "import pathlib, sys, time\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('0.0.7')\n"
        "    raise SystemExit(0)\n"
        "assert sys.argv[1] == '--strict'\n"
        "interaction = pathlib.Path(sys.argv[2]).read_text(encoding='ascii')\n"
        "proof = pathlib.Path(sys.argv[3]).read_text(encoding='ascii')\n"
        "assert interaction.startswith('p icnf\\n')\n"
        "assert proof.startswith('p lidrup\\n')\n" + behavior,
        f"checker-{outcome}.py",
    )


class LidrupWireTest(unittest.TestCase):
    def test_historical_project_schema_is_explicitly_aliased(self):
        self.assertEqual(PROJECT_LRUP_DAG_SCHEMA, PROOF_FRAGMENT_SCHEMA)
        self.assertEqual(PROJECT_LRUP_DAG_SCHEMA, "symcc-qfbv-palrup-proof-fragment-v1")

    def test_canonical_export_import_and_internal_replay(self):
        plan = _plan()
        with tempfile.TemporaryDirectory() as directory:
            store = IncrementalProofStore(directory)
            _record, result = _publish_simple(plan, store)
            artifacts = export_lidrup_artifacts(plan, result, store)
            self.assertEqual(
                artifacts.interaction.decode("ascii"),
                "p icnf\n"
                "i 1 0\n"
                "i -2 1 0\n"
                "i -3 -1 0\n"
                "q 2 3 0\n"
                "s UNSATISFIABLE\n"
                "u 2 3 0\n",
            )
            self.assertEqual(
                artifacts.proof.decode("ascii"),
                "p lidrup\n"
                "i 1 1 0\n"
                "i 2 -2 1 0\n"
                "i 3 -3 -1 0\n"
                "q 2 3 0\n"
                "l 4 -2 -3 0 1 3 0\n"
                "s UNSATISFIABLE\n"
                "u 2 3 0 4 0\n",
            )
            imported, imported_result = import_lidrup_artifacts(
                plan,
                artifacts.interaction,
                artifacts.proof,
                source_worker="wire-import",
                worker_epoch=3,
                sequence=8,
            )
            digest, _created = store.publish(imported)
            self.assertEqual(digest, imported["record_sha256"])
            authorization = IncrementalProofChecker(store).verify_result_receipt(
                plan, imported_result
            )
            self.assertEqual(authorization.failed_assumptions, plan.assumptions)
            metadata = artifacts.metadata()
            self.assertEqual(metadata["learned_clause_count"], 1)
            self.assertEqual(len(metadata["artifact_sha256"]), 64)

    def test_signed_cube_assumptions_round_trip_through_lidrup(self):
        base = _plan()
        plan = extend_bitblast_assumptions(base, [-1])
        with tempfile.TemporaryDirectory() as directory:
            store = IncrementalProofStore(directory)
            record = make_rup_clause_record(
                plan,
                tuple(-literal for literal in plan.assumptions),
                dependency_assumptions=plan.assumptions,
                source_worker="signed-cube",
                worker_epoch=0,
                sequence=0,
            )
            digest, _created = store.publish(record)
            receipt = make_unsat_result_receipt(
                plan, digest, plan.assumptions
            )
            artifacts = export_lidrup_artifacts(plan, receipt, store)
            self.assertIn(b"q 2 3 -1 0\n", artifacts.interaction)
            self.assertIn(b"u -1 2 3 0\n", artifacts.interaction)
            imported, imported_receipt = import_lidrup_artifacts(
                plan,
                artifacts.interaction,
                artifacts.proof,
                source_worker="signed-import",
                worker_epoch=0,
                sequence=1,
            )
            imported_digest, _created = store.publish(imported)
            self.assertEqual(
                imported_digest, imported_receipt["clause_receipt_sha256"]
            )
            authorization = IncrementalProofChecker(
                store
            ).verify_result_receipt(plan, imported_receipt)
            self.assertEqual(
                authorization.failed_assumptions, (-1, 2, 3)
            )

    def test_recursive_imports_are_flattened_and_deduplicated(self):
        plan = _plan()
        with tempfile.TemporaryDirectory() as directory:
            store = IncrementalProofStore(directory)
            child = make_rup_clause_record(
                plan,
                [-plan.assumptions[1], -1],
                dependency_assumptions=[plan.assumptions[1]],
                source_worker="child",
                worker_epoch=0,
                sequence=0,
            )
            child_digest, _created = store.publish(child)
            first_import = len(plan.clauses) + 1
            second_import = first_import + 1
            parent = {
                "schema": CLAUSE_RECORD_SCHEMA,
                "fragment_schema": PROJECT_LRUP_DAG_SCHEMA,
                "protocol": CLAUSE_PROTOCOL,
                "formula_sha256": plan.formula_sha256,
                "cnf_sha256": child["cnf_sha256"],
                "base_clause_count": len(plan.clauses),
                "max_variable": plan.max_variable,
                "source_worker": "parent",
                "worker_epoch": 1,
                "sequence": 0,
                "dependency_assumptions": list(plan.assumptions),
                "imports": [
                    {
                        "receipt_sha256": child_digest,
                        "local_clause_id": first_import,
                        "clause": child["shared_clause"],
                    },
                    {
                        "receipt_sha256": child_digest,
                        "local_clause_id": second_import,
                        "clause": child["shared_clause"],
                    },
                ],
                "proof_steps": [
                    {
                        "clause_id": second_import + 1,
                        "clause": [-2, -3],
                        "hints": [second_import, 2],
                    }
                ],
                "shared_clause": [-2, -3],
            }
            parent["record_sha256"] = _canonical_digest(parent)
            parent_digest, _created = store.publish(parent)
            result = make_unsat_result_receipt(plan, parent_digest, plan.assumptions)
            artifacts = export_lidrup_artifacts(plan, result, store)
            lemmas = [
                line
                for line in artifacts.proof.decode("ascii").splitlines()
                if line.startswith("l ")
            ]
            self.assertEqual(
                lemmas,
                ["l 4 -1 -3 0 3 0", "l 5 -2 -3 0 4 2 0"],
            )
            self.assertEqual(artifacts.learned_clause_count, 2)

    def test_import_rejects_scope_hint_core_and_trailing_tampering(self):
        plan = _plan()
        with tempfile.TemporaryDirectory() as directory:
            store = IncrementalProofStore(directory)
            _record, result = _publish_simple(plan, store)
            artifacts = export_lidrup_artifacts(plan, result, store)
            cases = [
                (
                    artifacts.interaction.replace(b"i 1 0", b"i -1 0"),
                    artifacts.proof,
                    "input differs",
                ),
                (
                    artifacts.interaction,
                    artifacts.proof.replace(b"0 1 3 0", b"0 1 2 0"),
                    "not LRUP",
                ),
                (
                    artifacts.interaction,
                    artifacts.proof.replace(b"u 2 3 0 4 0", b"u 2 0 4 0"),
                    "core",
                ),
                (
                    artifacts.interaction + b"q 2 0\n",
                    artifacts.proof,
                    "trailing",
                ),
            ]
            for interaction, proof, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ProofWireError, message):
                        import_lidrup_artifacts(
                            plan,
                            interaction,
                            proof,
                            source_worker="import",
                            worker_epoch=0,
                            sequence=0,
                        )

    def test_export_and_import_enforce_artifact_budget(self):
        plan = _plan()
        with tempfile.TemporaryDirectory() as directory:
            store = IncrementalProofStore(directory)
            _record, result = _publish_simple(plan, store)
            with self.assertRaisesRegex(ProofWireError, "exceeds"):
                export_lidrup_artifacts(plan, result, store, max_artifact_bytes=8)
            artifacts = export_lidrup_artifacts(plan, result, store)
            with self.assertRaisesRegex(ProofWireError, "oversized"):
                import_lidrup_artifacts(
                    plan,
                    artifacts.interaction,
                    artifacts.proof,
                    source_worker="import",
                    worker_epoch=0,
                    sequence=0,
                    max_artifact_bytes=8,
                )


class LidrupExternalCheckerTest(unittest.TestCase):
    def test_pinned_checker_receipt_and_tamper_detection(self):
        plan = _plan()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = IncrementalProofStore(root / "proofs")
            _record, result = _publish_simple(plan, store)
            path, digest = _checker_tool(root)
            checker = LidrupExternalChecker(path, checker_sha256=digest)
            artifacts, receipt = checker.verify(plan, result, store)
            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(receipt["checker_source_commit"], LIDRUP_CHECKER_COMMIT)
            self.assertEqual(
                checker.validate_receipt(plan, result, store, artifacts, receipt)[
                    "receipt_sha256"
                ],
                receipt["receipt_sha256"],
            )
            tampered = dict(receipt)
            tampered["proof_sha256"] = "0" * 64
            with self.assertRaisesRegex(ProofWireError, "identity"):
                checker.validate_receipt(plan, result, store, artifacts, tampered)

    def test_checker_identity_is_rechecked_before_use(self):
        plan = _plan()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = IncrementalProofStore(root / "proofs")
            _record, result = _publish_simple(plan, store)
            path, digest = _checker_tool(root)
            checker = LidrupExternalChecker(path, checker_sha256=digest)
            path.write_text(path.read_text(encoding="ascii") + "# changed\n")
            with self.assertRaisesRegex(ProofWireError, "identity changed"):
                checker.verify(plan, result, store)

    def test_checker_rejects_wrong_identity_version_output_and_diagnostics(self):
        plan = _plan()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = IncrementalProofStore(root / "proofs")
            _record, result = _publish_simple(plan, store)
            good_path, good_digest = _checker_tool(root)
            with self.assertRaisesRegex(ProofWireError, "content identity"):
                LidrupExternalChecker(good_path, checker_sha256="0" * 64)
            with self.assertRaisesRegex(ProofWireError, "version probe"):
                LidrupExternalChecker(
                    good_path,
                    checker_sha256=good_digest,
                    checker_version="0.0.8",
                )
            for outcome, message in (
                ("missing", "did not verify"),
                ("stderr", "did not verify"),
                ("large", "output exceeds"),
            ):
                path, digest = _checker_tool(root, outcome=outcome)
                checker = LidrupExternalChecker(path, checker_sha256=digest)
                with self.subTest(outcome=outcome):
                    with self.assertRaisesRegex(ProofWireError, message):
                        checker.verify(plan, result, store)
            path, digest = _checker_tool(root, outcome="sleep")
            checker = LidrupExternalChecker(path, checker_sha256=digest, timeout_ms=10)
            with self.assertRaisesRegex(ProofWireError, "timed out"):
                checker.verify(plan, result, store)

    def test_wire_store_round_trip_dedup_quota_and_tamper_detection(self):
        plan = _plan()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proof_store = IncrementalProofStore(root / "proofs")
            _record, result = _publish_simple(plan, proof_store)
            path, digest = _checker_tool(root)
            checker = LidrupExternalChecker(path, checker_sha256=digest)
            artifacts, receipt = checker.verify(plan, result, proof_store)
            wire_store = LidrupWireStore(root / "wire")
            artifact_id, receipt_id, created = wire_store.publish(artifacts, receipt)
            self.assertTrue(created)
            self.assertEqual(
                wire_store.publish(artifacts, receipt),
                (artifact_id, receipt_id, False),
            )
            loaded_artifacts, loaded_receipt = wire_store.load(artifact_id, receipt_id)
            self.assertEqual(loaded_artifacts, artifacts)
            self.assertEqual(loaded_receipt, receipt)
            self.assertEqual(wire_store.stats()["artifacts"], 1)
            quota_store = LidrupWireStore(root / "quota", max_records=1)
            with self.assertRaisesRegex(ProofWireError, "quota"):
                quota_store.publish(artifacts, receipt)
            interrupted_store = LidrupWireStore(root / "interrupted")
            with mock.patch(
                "qfbv_proof_wire.os.link", side_effect=OSError("injected link failure")
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    interrupted_store.publish(artifacts, receipt)
            self.assertFalse(any((root / "interrupted").rglob("*.tmp-*")))
            self.assertTrue(interrupted_store.publish(artifacts, receipt)[2])
            write_interrupted_store = LidrupWireStore(root / "write-interrupted")
            with mock.patch("qfbv_proof_wire.os.write", return_value=0):
                with self.assertRaisesRegex(OSError, "short"):
                    write_interrupted_store.publish(artifacts, receipt)
            self.assertFalse(
                any((root / "write-interrupted").rglob("*.tmp-*"))
            )
            concurrent_store = LidrupWireStore(root / "concurrent")
            with ThreadPoolExecutor(max_workers=8) as executor:
                published = list(
                    executor.map(
                        lambda _index: concurrent_store.publish(artifacts, receipt),
                        range(16),
                    )
                )
            self.assertEqual(sum(int(item[2]) for item in published), 1)
            proof_sha256 = artifacts.metadata()["proof_sha256"]
            proof_path = (
                root / "wire" / "objects" / proof_sha256[:2] / f"{proof_sha256}.lidrup"
            )
            proof_path.write_bytes(artifacts.proof + b"c tampered\n")
            with self.assertRaisesRegex(ProofWireError, "identity changed"):
                wire_store.load(artifact_id, receipt_id)

    def test_cadical_backend_and_query_store_both_verify_wire_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            solver_path = root / "cadical.py"
            solver_path.write_text(
                "import pathlib, sys\n"
                "pathlib.Path(sys.argv[-1]).write_text("
                "'6 0 4 1 5 3 0\\n', encoding='ascii')\n"
                "print('s UNSATISFIABLE')\n"
                "raise SystemExit(20)\n",
                encoding="ascii",
            )
            checker_path, checker_digest = _checker_tool(root)
            query_store = QueryStore(root / "queries")
            query_store.ingest(_envelope())
            lease = query_store.claim("worker")
            self.assertIsNotNone(lease)
            assert lease is not None
            proof_store = IncrementalProofStore(root / "proofs")
            proof_checker = IncrementalProofChecker(proof_store)
            wire_checker = LidrupExternalChecker(
                checker_path, checker_sha256=checker_digest
            )
            wire_store = LidrupWireStore(root / "wire")
            query_store.register_qfbv_incremental_proof_checker(proof_checker)
            query_store.register_qfbv_proof_wire(wire_checker, wire_store)
            backend = CadicalQfbvSolver(
                query_store,
                [
                    sys.executable,
                    str(solver_path),
                    "--plain",
                    "--lrat",
                    "--no-binary",
                    "{cnf}",
                    "{proof}",
                ],
                name="wire-cadical",
                proof_store=proof_store,
                proof_checker=proof_checker,
                proof_wire_checker=wire_checker,
                proof_wire_store=wire_store,
            )
            result = dict(backend(lease))
            self.assertEqual(result["status"], "unsat")
            self.assertTrue(result["backend_lidrup_verified"])
            missing_wire = dict(result)
            missing_wire["backend_lidrup_receipt_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "proof failed"):
                query_store.complete(lease, "worker", missing_wire)
            self.assertTrue(query_store.complete(lease, "worker", result))
            self.assertEqual(wire_store.stats()["artifacts"], 1)
            stats = query_store.stats()
            self.assertEqual(stats["lidrup_wire_results"], 1)
            self.assertEqual(stats["lidrup_wire_verified_results"], 1)
            self.assertEqual(stats["lidrup_wire_artifacts_created"], 1)
            self.assertEqual(stats["lidrup_wire_learned_clauses"], 1)
            self.assertGreater(stats["lidrup_wire_checker_elapsed_us"], 0)


class PalrupWireTest(unittest.TestCase):
    def test_binary_codec_matches_tracer_encoding_exactly(self):
        directives = (
            PalrupImport(4, (1, -2)),
            PalrupProduce(5, (), (1, 3)),
            PalrupDelete((4, 5)),
        )
        encoded = encode_palrup_fragment(directives)
        self.assertEqual(encoded.hex(), "6908020500610a0002060064080a00")
        self.assertEqual(decode_palrup_fragment(encoded), directives)
        self.assertEqual(
            palrup_fragment_text(directives),
            b"i 4 1 -2 0\na 5 0 1 3 0\nd 4 5 0\n",
        )

    def test_codec_handles_signed_limits_and_rejects_noncanonical_streams(self):
        directives = (
            PalrupImport((1 << 63) - 1, (-(1 << 31) + 1, (1 << 31) - 1)),
            PalrupProduce(1, (), ()),
        )
        self.assertEqual(
            decode_palrup_fragment(encode_palrup_fragment(directives)), directives
        )
        malformed = (
            b"x",
            b"a",
            b"a\x82\x00\x00\x00",
            b"a\x00\x00\x00",
            b"d\x00",
        )
        for fragment in malformed:
            with self.subTest(fragment=fragment):
                with self.assertRaises(ProofWireError):
                    decode_palrup_fragment(fragment)
        with self.assertRaisesRegex(ProofWireError, "PalRUP literal"):
            encode_palrup_fragment((PalrupImport(1, (-(1 << 31),)),))
        with self.assertRaisesRegex(ProofWireError, "PalRUP imported ID"):
            encode_palrup_fragment((PalrupImport(1 << 63, (1,)),))
        with self.assertRaisesRegex(ProofWireError, "byte bound"):
            encode_palrup_fragment(directives, max_bytes=2)

    def test_converter_oracle_binds_content_and_scope(self):
        directives = (PalrupImport(4, (1, -2)), PalrupDelete((4,)))
        fragment = encode_palrup_fragment(directives)
        expected = palrup_fragment_text(directives)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            converter, digest = _write_tool(
                root,
                "import pathlib, sys\n"
                f"pathlib.Path(sys.argv[2]).write_bytes({expected!r})\n"
                "print('official-style converter metadata')\n",
                "converter.py",
            )
            oracle = PalrupFragmentOracle(converter, converter_sha256=digest)
            actual, receipt = oracle.verify(fragment)
            self.assertEqual(actual, expected)
            self.assertEqual(receipt["protocol"], PALRUP_BINARY_PROTOCOL)
            self.assertEqual(
                receipt["scope"],
                "fragment-syntax-interoperability-not-global-unsat",
            )
            converter.write_text(
                converter.read_text(encoding="ascii") + "# changed\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(ProofWireError, "identity changed"):
                oracle.verify(fragment)


if __name__ == "__main__":
    unittest.main()
