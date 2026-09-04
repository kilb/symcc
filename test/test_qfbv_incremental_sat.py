#!/usr/bin/env python3
# RUN: python3 %s

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from cadical_qfbv_backend import (  # noqa: E402
    CadicalQfbvSolver,
    PersistentCadicalQfbvSolver,
)
from qfbv_incremental_proof import (  # noqa: E402
    CLAUSE_RECORD_SCHEMA,
    CLAUSE_PROTOCOL,
    PROOF_FRAGMENT_SCHEMA,
    IncrementalProofChecker,
    IncrementalProofError,
    IncrementalProofStore,
    check_lrup,
    lift_ascii_lrat_proof,
    make_rup_clause_record,
    make_unsat_result_receipt,
)
from qfbv_incremental_sat import (  # noqa: E402
    ASSUMPTION_SCHEMA,
    BITBLAST_SCHEMA,
    INCREMENT_SCHEMA,
    QfbvBitBlastError,
    bitblast_qfbv_query,
    parse_dimacs_model,
)
from qfbv_artifact_lifecycle import ArtifactLifecycleRegistry  # noqa: E402
from query_store import QueryStore  # noqa: E402
from symcc_query_service import _load_portfolio  # noqa: E402


def _bool_contradiction():
    expressions = {
        "true": {
            "op": "bool", "bits": 1, "children": [],
            "attrs": {"value": True},
        },
        "false": {
            "op": "bool", "bits": 1, "children": [],
            "attrs": {"value": False},
        },
    }
    return bitblast_qfbv_query("bool-contradiction", ["true", "false"], expressions)


def _operator_plan(op, left, right, expected, *, width=8):
    expressions = {
        "left": {
            "op": "read", "bits": 8, "children": [], "attrs": {"index": 0}
        },
        "right": {
            "op": "read", "bits": 8, "children": [], "attrs": {"index": 1}
        },
    }
    left_name = "left"
    right_name = "right"
    if width != 8:
        expressions["left-narrow"] = {
            "op": "extract", "bits": width, "children": ["left"],
            "attrs": {"index": 0},
        }
        expressions["right-narrow"] = {
            "op": "extract", "bits": width, "children": ["right"],
            "attrs": {"index": 0},
        }
        left_name, right_name = "left-narrow", "right-narrow"
    result_bits = 1 if op in {
        "ult", "ule", "ugt", "uge", "slt", "sle", "sgt", "sge",
        "equal", "distinct",
    } else width
    expressions["operation"] = {
        "op": op,
        "bits": result_bits,
        "children": [left_name, right_name],
        "attrs": {},
    }
    if result_bits == 1:
        if bool(expected):
            root = "operation"
        else:
            expressions["root"] = {
                "op": "lnot", "bits": 1, "children": ["operation"], "attrs": {}
            }
            root = "root"
    else:
        expressions["expected"] = {
            "op": "constant", "bits": width, "children": [],
            "attrs": {"value_hex": format(expected & ((1 << width) - 1), "x")},
        }
        expressions["root"] = {
            "op": "equal", "bits": 1,
            "children": ["operation", "expected"], "attrs": {},
        }
        root = "root"
    return bitblast_qfbv_query(f"{op}-{left}-{right}", [root], expressions)


def _fixed_formula_satisfied(plan, values):
    assignment = {}
    for offset, literals in plan.input_literals:
        value = values[offset]
        for bit, literal in enumerate(literals):
            assignment[abs(literal)] = bool((value >> bit) & 1) == (literal > 0)
    for literal in plan.assumptions:
        assignment[abs(literal)] = literal > 0
    changed = True
    while changed:
        changed = False
        for clause in plan.clauses:
            unassigned = []
            satisfied = False
            for literal in clause:
                value = assignment.get(abs(literal))
                if value is None:
                    unassigned.append(literal)
                elif value == (literal > 0):
                    satisfied = True
                    break
            if satisfied:
                continue
            if not unassigned:
                return False
            if len(unassigned) == 1:
                literal = unassigned[0]
                previous = assignment.setdefault(abs(literal), literal > 0)
                if previous != (literal > 0):
                    return False
                changed = True
    return all(any(
        assignment.get(abs(literal)) == (literal > 0) for literal in clause
    ) for clause in plan.clauses)


def _envelope():
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "incremental-proof-test",
        "nodes": [
            {"id": 0, "op": "bool", "bits": 1, "children": [],
             "attrs": {"value": True}},
            {"id": 1, "op": "bool", "bits": 1, "children": [],
             "attrs": {"value": False}},
        ],
        "prefix_roots": [0],
        "target_root": 1,
        "input_hex": "",
        "timeout_ms": 2000,
        "metadata": {"source": "incremental-proof-test"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert false)\n",
    }


class IncrementalBitBlastTest(unittest.TestCase):
    def test_increment_and_assumption_identities_are_deterministic(self):
        first = _bool_contradiction()
        second = _bool_contradiction()
        self.assertEqual(first, second)
        self.assertEqual(first.certificate["schema"], BITBLAST_SCHEMA)
        self.assertEqual(first.increments[0].as_dict()["schema"], INCREMENT_SCHEMA)
        self.assertEqual(len(first.assumptions), 2)
        self.assertEqual(first.increments[-1].formula_sha256, first.formula_sha256)
        self.assertIn(ASSUMPTION_SCHEMA, json.dumps({
            "schema": ASSUMPTION_SCHEMA,
            "digest": first.assumption_sha256,
        }))
        self.assertEqual(first.dimacs().splitlines()[0], "p cnf 3 3")
        self.assertEqual(
            first.dimacs(assumptions_as_units=True).splitlines()[0],
            "p cnf 3 5",
        )

    def test_arithmetic_division_shift_rotate_and_comparison_edges(self):
        cases = [
            ("add", 255, 2, 1, 8),
            ("mul", 0x81, 3, 0x83, 8),
            ("udiv", 0x81, 0, 0xff, 8),
            ("urem", 0x81, 0, 0x81, 8),
            ("sdiv", 0x80, 0xff, 0x80, 8),
            ("sdiv", 0x80, 0, 1, 8),
            ("srem", 0x81, 3, 0xff, 8),
            ("shl", 0x81, 8, 0, 8),
            ("ashr", 0x81, 8, 0xff, 8),
            ("rol", 0b10001, 7, 0b00110, 5),
            ("ror", 0b10001, 7, 0b01100, 5),
            ("slt", 0x80, 0x7f, True, 8),
            ("uge", 0x80, 0x7f, True, 8),
        ]
        for op, left, right, expected, width in cases:
            with self.subTest(op=op, left=left, right=right, width=width):
                plan = _operator_plan(op, left, right, expected, width=width)
                self.assertTrue(_fixed_formula_satisfied(plan, {0: left, 1: right}))

    def test_wrong_expected_value_is_rejected_by_cnf(self):
        plan = _operator_plan("udiv", 0x42, 3, 0x15)
        self.assertFalse(_fixed_formula_satisfied(plan, {0: 0x42, 1: 3}))

    def test_model_parser_is_strict_and_recovers_input(self):
        plan = _operator_plan("add", 1, 2, 3)
        positives = {literal for _, bits in plan.input_literals for literal in bits}
        output = "s SATISFIABLE\nv " + " ".join(map(str, sorted(positives))) + " 0\n"
        status, model = parse_dimacs_model(output)
        self.assertEqual(status, "sat")
        self.assertEqual(plan.input_bytes_from_model(model), {0: 255, 1: 255})
        with self.assertRaisesRegex(QfbvBitBlastError, "conflicting"):
            parse_dimacs_model("s SATISFIABLE\ns UNSATISFIABLE\n")
        with self.assertRaisesRegex(QfbvBitBlastError, "terminated"):
            parse_dimacs_model("s SATISFIABLE\nv 1\n")

    def test_malformed_graph_and_resource_bounds_fail_closed(self):
        with self.assertRaisesRegex(QfbvBitBlastError, "cycle"):
            bitblast_qfbv_query("q", ["x"], {
                "x": {"op": "lnot", "bits": 1, "children": ["x"], "attrs": {}}
            })
        with self.assertRaisesRegex(QfbvBitBlastError, "variable limit"):
            bitblast_qfbv_query("q", ["x"], {
                "x": {"op": "read", "bits": 8, "children": [], "attrs": {"index": 0}}
            }, max_variables=1)


class IncrementalProofTest(unittest.TestCase):
    def test_lrup_clause_and_result_receipt_replay(self):
        plan = _bool_contradiction()
        failed = plan.assumptions
        record = make_rup_clause_record(
            plan,
            tuple(-literal for literal in failed),
            dependency_assumptions=failed,
            source_worker="worker-0",
            worker_epoch=4,
            sequence=7,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = IncrementalProofStore(directory)
            digest, created = store.publish(record)
            self.assertTrue(created)
            checker = IncrementalProofChecker(store)
            authorization = checker.verify_clause_record(plan, store.load(digest))
            self.assertEqual(authorization.clause, tuple(-x for x in failed))
            receipt = make_unsat_result_receipt(plan, digest, failed)
            result = checker.verify_result_receipt(plan, receipt)
            self.assertEqual(result.status, "unsat")
            wrong_protocol = dict(receipt)
            wrong_protocol["protocol"] = "unrelated-proof-protocol-v1"
            wrong_protocol.pop("receipt_sha256")
            with self.assertRaisesRegex(IncrementalProofError, "protocol"):
                checker.verify_result_receipt(plan, wrong_protocol)

    def test_ascii_lrat_assumption_lifting(self):
        plan = _bool_contradiction()
        # Base clauses are 1..3, temporary assumption units are 4 and 5.
        proof = "6 0 4 1 5 3 0\n"
        record = lift_ascii_lrat_proof(
            plan,
            proof,
            source_worker="worker-lrat",
            worker_epoch=1,
            sequence=0,
        )
        self.assertEqual(record["shared_clause"], [-2, -3])
        self.assertEqual(record["proof_steps"][0]["hints"], [1, 3])
        authorization = IncrementalProofChecker().verify_clause_record(plan, record)
        self.assertEqual(authorization.propagation_count, 1)

    def test_imported_fragment_is_recursively_rechecked(self):
        plan = _bool_contradiction()
        source = make_rup_clause_record(
            plan,
            [-plan.assumptions[1], -1],
            dependency_assumptions=[plan.assumptions[1]],
            source_worker="source",
            worker_epoch=0,
            sequence=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = IncrementalProofStore(directory)
            source_digest, _ = store.publish(source)
            imported_id = len(plan.clauses) + 1
            body = {
                "schema": CLAUSE_RECORD_SCHEMA,
                "fragment_schema": PROOF_FRAGMENT_SCHEMA,
                "protocol": CLAUSE_PROTOCOL,
                "formula_sha256": plan.formula_sha256,
                "cnf_sha256": source["cnf_sha256"],
                "base_clause_count": len(plan.clauses),
                "max_variable": plan.max_variable,
                "source_worker": "target",
                "worker_epoch": 1,
                "sequence": 0,
                "dependency_assumptions": [plan.assumptions[1]],
                "imports": [{
                    "receipt_sha256": source_digest,
                    "local_clause_id": imported_id,
                    "clause": source["shared_clause"],
                }],
                "proof_steps": [{
                    "clause_id": imported_id + 1,
                    "clause": source["shared_clause"],
                    "hints": [imported_id],
                }],
                "shared_clause": source["shared_clause"],
            }
            import hashlib
            body["record_sha256"] = hashlib.sha256(json.dumps(
                body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("ascii")).hexdigest()
            checker = IncrementalProofChecker(store)
            authorization = checker.verify_clause_record(plan, body)
            self.assertEqual(authorization.import_count, 1)
            tampered = dict(body)
            tampered["imports"] = [dict(body["imports"][0], clause=[-2])]
            tampered.pop("record_sha256")
            with self.assertRaises(IncrementalProofError):
                checker.verify_clause_record(plan, tampered)

    def test_fragment_cannot_import_from_a_descendant_increment(self):
        plan = _bool_contradiction()
        descendant = make_rup_clause_record(
            plan,
            [1],
            formula_sha256=plan.increments[-1].formula_sha256,
            source_worker="descendant",
            worker_epoch=0,
            sequence=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = IncrementalProofStore(directory)
            descendant_digest, _ = store.publish(descendant)
            first = plan.increments[0]
            ancestor_scope = make_rup_clause_record(
                plan,
                [1],
                formula_sha256=first.formula_sha256,
                source_worker="ancestor-scope",
                worker_epoch=0,
                sequence=0,
            )
            imported_id = first.last_clause_id + 1
            body = {
                "schema": CLAUSE_RECORD_SCHEMA,
                "fragment_schema": PROOF_FRAGMENT_SCHEMA,
                "protocol": CLAUSE_PROTOCOL,
                "formula_sha256": first.formula_sha256,
                "cnf_sha256": ancestor_scope["cnf_sha256"],
                "base_clause_count": first.last_clause_id,
                "max_variable": first.max_variable,
                "source_worker": "ancestor",
                "worker_epoch": 0,
                "sequence": 0,
                "dependency_assumptions": [],
                "imports": [{
                    "receipt_sha256": descendant_digest,
                    "local_clause_id": imported_id,
                    "clause": [1],
                }],
                "proof_steps": [{
                    "clause_id": imported_id + 1,
                    "clause": [1],
                    "hints": [imported_id],
                }],
                "shared_clause": [1],
            }
            import hashlib
            body["record_sha256"] = hashlib.sha256(json.dumps(
                body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("ascii")).hexdigest()
            with self.assertRaisesRegex(IncrementalProofError, "descendant"):
                IncrementalProofChecker(store).verify_clause_record(plan, body)

    def test_lrup_rejects_nonunit_hint_and_scope_tampering(self):
        with self.assertRaisesRegex(IncrementalProofError, "neither unit"):
            check_lrup({1: (1, 2)}, (3,), (1,), max_variable=3)
        plan = _bool_contradiction()
        record = make_rup_clause_record(
            plan, [-2, -3], dependency_assumptions=plan.assumptions,
            source_worker="w", worker_epoch=0, sequence=0,
        )
        record["formula_sha256"] = "0" * 64
        record.pop("record_sha256")
        with self.assertRaises(IncrementalProofError):
            IncrementalProofChecker().verify_clause_record(plan, record)

        variable_tamper = make_rup_clause_record(
            plan, [-2, -3], dependency_assumptions=plan.assumptions,
            source_worker="w", worker_epoch=0, sequence=1,
        )
        variable_tamper["max_variable"] = plan.max_variable + 1
        variable_tamper.pop("record_sha256")
        with self.assertRaisesRegex(
            IncrementalProofError, "max variable|variable domain"
        ):
            IncrementalProofChecker().verify_clause_record(plan, variable_tamper)

    def test_store_rejects_replaced_shard_directory(self):
        plan = _bool_contradiction()
        record = make_rup_clause_record(
            plan, [-2, -3], dependency_assumptions=plan.assumptions,
            source_worker="w", worker_epoch=0, sequence=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = IncrementalProofStore(root / "proofs")
            digest, _ = store.publish(record)
            shard = store._path(digest).parent
            displaced = shard.with_name(f"{shard.name}.displaced")
            os.rename(shard, displaced)
            outside = root / "outside"
            outside.mkdir()
            shard.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                store.load(digest)

    def test_lifecycle_tracks_import_edges_and_gc_order(self):
        plan = _bool_contradiction()
        source = make_rup_clause_record(
            plan, [-2, -3], dependency_assumptions=plan.assumptions,
            source_worker="source", worker_epoch=0, sequence=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = ArtifactLifecycleRegistry(Path(directory) / "lifecycle")
            store = IncrementalProofStore(
                Path(directory) / "proofs", lifecycle=lifecycle
            )
            source_digest, _ = store.publish(source)
            imported_id = len(plan.clauses) + 1
            target = {
                "schema": CLAUSE_RECORD_SCHEMA,
                "fragment_schema": PROOF_FRAGMENT_SCHEMA,
                "protocol": CLAUSE_PROTOCOL,
                "formula_sha256": plan.formula_sha256,
                "cnf_sha256": source["cnf_sha256"],
                "base_clause_count": len(plan.clauses),
                "max_variable": plan.max_variable,
                "source_worker": "target",
                "worker_epoch": 1,
                "sequence": 0,
                "dependency_assumptions": list(plan.assumptions),
                "imports": [{
                    "receipt_sha256": source_digest,
                    "local_clause_id": imported_id,
                    "clause": source["shared_clause"],
                }],
                "proof_steps": [{
                    "clause_id": imported_id + 1,
                    "clause": source["shared_clause"],
                    "hints": [imported_id],
                }],
                "shared_clause": source["shared_clause"],
            }
            import hashlib
            target["record_sha256"] = hashlib.sha256(json.dumps(
                target, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("ascii")).hexdigest()
            target_digest, _ = store.publish(target)
            self.assertEqual(lifecycle.stats()["artifacts"], 2)
            self.assertEqual(lifecycle.stats()["edges"], 1)
            self.assertEqual(
                store.synchronize_lifecycle(max_entries=8)["complete"], True
            )
            collected = lifecycle.collect(
                store.delete_lifecycle_artifact,
                grace_seconds=0,
                max_objects=8,
                max_bytes=1 << 20,
                time_budget_ms=10_000,
                now=time.time() + 10,
            )
            self.assertEqual(
                [item.digest for item in collected.deleted],
                [target_digest, source_digest],
            )

    def test_backend_and_query_store_independently_authorize_unsat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake_cadical.py"
            fake.write_text(
                "import pathlib,sys\n"
                "pathlib.Path(sys.argv[-1]).write_text('6 0 4 1 5 3 0\\n', "
                "encoding='ascii')\n"
                "print('s UNSATISFIABLE')\n"
                "raise SystemExit(20)\n",
                encoding="ascii",
            )
            query_store = QueryStore(root / "queries")
            query_store.ingest(_envelope())
            lease = query_store.claim("worker")
            self.assertIsNotNone(lease)
            proof_store = IncrementalProofStore(root / "proofs")
            checker = IncrementalProofChecker(proof_store)
            query_store.register_qfbv_incremental_proof_checker(checker)
            backend = CadicalQfbvSolver(
                query_store,
                [
                    sys.executable, str(fake), "--plain", "--lrat",
                    "--no-binary", "{cnf}", "{proof}",
                ],
                name="fake-cadical",
                proof_store=proof_store,
                proof_checker=checker,
                capabilities={"incremental": True},
            )
            result = dict(backend(lease))
            self.assertEqual(result["status"], "unsat")
            self.assertTrue(query_store.complete(lease, "worker", result))
            stats = query_store.stats()
            self.assertEqual(stats["incremental_sat_results"], 1)
            self.assertEqual(stats["incremental_sat_verified_unsat"], 1)
            self.assertEqual(stats["incremental_sat_proof_fragments_created"], 1)
            self.assertEqual(stats["incremental_sat_imported_clauses"], 0)
            self.assertGreater(stats["incremental_sat_checker_elapsed_us"], 0)
            fake.write_text(
                "print('s SATISFIABLE\\nv 1 0')\nraise SystemExit(20)\n",
                encoding="ascii",
            )
            mismatch = dict(backend(lease))
            self.assertEqual(mismatch["status"], "error")
            self.assertIn("exit code", mismatch["reason"])
            result["bitblast_certificate"] = dict(result["bitblast_certificate"])
            result["bitblast_certificate"]["cnf_sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                query_store._validate_result(result)

    def test_native_ipasir_context_is_reused_and_still_requires_lrup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fake_cadical.c"
            library = root / "libcadical.so"
            source.write_text(
                "#include <stdlib.h>\n"
                "typedef struct { int terminated; } Solver;\n"
                "const char* ccadical_signature(void){return \"cadical-3.0.1-test\";}\n"
                "void* ccadical_init(void){return calloc(1,sizeof(Solver));}\n"
                "void ccadical_release(void*p){free(p);}\n"
                "void ccadical_add(void*p,int l){(void)p;(void)l;}\n"
                "void ccadical_assume(void*p,int l){(void)p;(void)l;}\n"
                "int ccadical_solve(void*p){return ((Solver*)p)->terminated?0:20;}\n"
                "int ccadical_val(void*p,int l){(void)p;return l;}\n"
                "int ccadical_failed(void*p,int l){(void)p;(void)l;return 1;}\n"
                "void ccadical_terminate(void*p){((Solver*)p)->terminated=1;}\n"
                "void ccadical_set_terminate(void*p,void*s,void*f){"
                "(void)p;(void)s;(void)f;}\n",
                encoding="ascii",
            )
            subprocess.run(
                ["cc", "-shared", "-fPIC", str(source), "-o", str(library)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            fake = root / "fake_proof.py"
            fake.write_text(
                "import pathlib,sys\n"
                "cnf=pathlib.Path(sys.argv[-2]).read_text().splitlines()\n"
                "count=int(cnf[0].split()[3])\n"
                "line=('6 0 4 1 5 3 0\\n' if count==5 "
                "else '7 0 5 6 4 0\\n')\n"
                "pathlib.Path(sys.argv[-1]).write_text(line,encoding='ascii')\n"
                "print('s UNSATISFIABLE')\n"
                "raise SystemExit(20)\n",
                encoding="ascii",
            )
            query_store = QueryStore(root / "queries")
            query_store.ingest(_envelope())
            lease = query_store.claim("worker")
            proof_store = IncrementalProofStore(root / "proofs")
            checker = IncrementalProofChecker(proof_store)
            query_store.register_qfbv_incremental_proof_checker(checker)
            command = [
                sys.executable, str(fake), "--plain", "--lrat",
                "--no-binary", "{cnf}", "{proof}",
            ]
            with PersistentCadicalQfbvSolver(
                query_store,
                library,
                command,
                name="native-test",
                proof_store=proof_store,
                proof_checker=checker,
                capabilities={"incremental": True},
            ) as backend:
                first = dict(backend(lease))
                second = dict(backend(lease))
            self.assertEqual(first["status"], "unsat")
            self.assertFalse(first["backend_native_context_cache_hit"])
            self.assertTrue(second["backend_native_context_cache_hit"])
            self.assertEqual(second["backend_native_context_solve_count"], 2)
            self.assertEqual(second["backend_incremental_imported_clauses"], 1)
            self.assertEqual(
                query_store._validate_result(second)["status"], "unsat"
            )
            bad_signature = dict(second)
            bad_signature["backend_native_signature"] = "cadical-3.1.0"
            with self.assertRaisesRegex(ValueError, "signature"):
                query_store._validate_result(bad_signature)

    def test_portfolio_contract_requires_explicit_incremental_mode(self):
        base = {
            "name": "cadical3",
            "kind": "bitblast-cadical-qfbv",
            "persistent": False,
            "command": [
                "cadical", "--plain", "--lrat", "--no-binary",
                "{cnf}", "{proof}",
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "advertise incremental"):
            _load_portfolio(json.dumps([base]))
        base["capabilities"] = {"incremental": True}
        parsed = _load_portfolio(json.dumps([base]))
        self.assertEqual(parsed[0]["kind"], "bitblast-cadical-qfbv")
        persistent = dict(base, persistent=True, native_library="/lib/libcadical.so")
        parsed = _load_portfolio(json.dumps([persistent]))
        self.assertTrue(parsed[0]["persistent"])


if __name__ == "__main__":
    unittest.main()
