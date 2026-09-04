# RUN: python3 %s

import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from hybrid_feedback import SolverTelemetry  # noqa: E402
from query_store import QueryStore  # noqa: E402
from semantic_proposals import (  # noqa: E402
    SemanticProposalGenerator,
    _grammar_context_id,
    extract_constraint_cores,
    infer_token_spans,
    ifss_relevance_slices,
    synthesize_token_rules,
)
from ucsan_seed import (  # noqa: E402
    OBJECT_ENTRY,
    POINTEE,
    ROOT_ENTRY,
    SeedEntry,
    parse_seed,
    serialize_seed,
)
from verified_proposals import VerifiedProposalManager  # noqa: E402


class SemanticProposalTests(unittest.TestCase):
    @staticmethod
    def _query_envelope(content: bytes) -> dict:
        return {
            "schema": "symcc-query-ir-v1",
            "producer": "query-hole-test",
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
                    "attrs": {"value_hex": "7b"},
                },
                {
                    "id": 2,
                    "op": "equal",
                    "bits": 1,
                    "children": [0, 1],
                    "attrs": {},
                },
            ],
            "prefix_roots": [],
            "target_root": 2,
            "input_hex": content.hex(),
            "timeout_ms": 1000,
            "metadata": {"source": "query-hole", "output_dir": ""},
            "prefix_smt2": "(declare-fun |0| () (_ BitVec 8))\n",
            "target_smt2": (
                "(declare-fun |0| () (_ BitVec 8))\n"
                "(assert (= |0| #x7b))\n"
            ),
            "smt2": (
                "(declare-fun |0| () (_ BitVec 8))\n"
                "(assert (= |0| #x7b))\n"
            ),
        }

    def test_core_inverse_candidates_are_bounded_and_ingested(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "seed")
            Path(source).write_bytes(b"0123456789")
            token_dir = os.path.join(tmp, "tokens")
            os.mkdir(token_dir)
            Path(token_dir, "magic").write_bytes(b"OK")
            telemetry = SolverTelemetry(
                comparison_taints=((11, 99, 2, 2, 3, 1, 1),))
            cores = extract_constraint_cores(telemetry, 10)
            self.assertEqual((cores[0].lo, cores[0].hi), (2, 3))

            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "proposals"))
            generator = SemanticProposalGenerator(
                None, token_paths=(token_dir,), max_proposals_per_observation=8)
            added = generator.generate_into(manager, source, telemetry)
            self.assertGreaterEqual(added, 3)
            self.assertTrue(all(
                record.target_branch == 99
                for record in manager.records.values()))
            self.assertTrue(any(
                Path(record.candidate_path).read_bytes()[2:4] == b"OK"
                for record in manager.records.values()))

    def test_autosave_can_be_deferred_to_coordinator_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "seed")
            state = os.path.join(tmp, "semantic-state.json")
            Path(source).write_bytes(b"0123456789")
            telemetry = SolverTelemetry(
                comparison_taints=((11, 99, 2, 2, 3, 1, 1),)
            )
            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "proposals")
            )
            generator = SemanticProposalGenerator(state, autosave=False)

            self.assertGreater(generator.generate_into(manager, source, telemetry), 0)
            self.assertFalse(os.path.exists(state))

            generator.save()
            restored = SemanticProposalGenerator(state, autosave=False)
            self.assertEqual(restored.observations, generator.observations)
            self.assertEqual(restored.generated, generator.generated)

    def test_pcfg_context_order_is_cost_faithful_and_artifact_is_sealed(self):
        def digest(value: str) -> str:
            return hashlib.sha256(value.encode()).hexdigest()

        parser = "pcfg-ablation-fixture"
        specifications = (
            ("root", "document", ("record",)),
            ("record", "body", ("a", "b", "c")),
            ("a", "atom", ()),
            ("b", "atom", ()),
            ("c", "atom", ()),
        )
        productions = {}
        nodes = {}
        instances = []
        node_ids = {
            symbol: digest(f"node-{symbol}")
            for symbol, _, _ in specifications
        }
        for order, (symbol, state, children) in enumerate(
                specifications):
            production_id = digest(f"production-{symbol}")
            node_id = node_ids[symbol]
            productions[production_id] = {
                "id": production_id,
                "parser": parser,
                "lhs": symbol,
                "state": state,
                "shape_id": digest(f"shape-{symbol}"),
                "rhs": [
                    {
                        "symbol": child,
                        "state": (
                            "body" if child == "record" else "atom"),
                        "recursive": False,
                    }
                    for child in children
                ],
                "alternative": 0,
            }
            nodes[node_id] = {
                "node_id": node_id,
                "parser": parser,
                "symbol": symbol,
                "state": state,
                "order": order,
            }
            instances.append({
                "instance_id": digest(f"instance-{symbol}"),
                "node_id": node_id,
                "production_id": production_id,
                "alternative": 0,
                "child_edge_ids": [],
            })
        edges = {}
        for parent, _, children in specifications:
            parent_instance = next(
                instance for instance in instances
                if instance["node_id"] == node_ids[parent]
            )
            for slot, child in enumerate(children):
                edge_id = digest(f"edge-{parent}-{child}")
                edges[edge_id] = {
                    "edge_id": edge_id,
                    "parent_id": node_ids[parent],
                    "child_id": node_ids[child],
                    "slot": slot,
                    "alternative": 0,
                }
                parent_instance["child_edge_ids"].append(edge_id)

        expected_nonempty = (
            (),
            ("parent",),
            ("parent", "circuit"),
            ("parent", "circuit", "sibling"),
            ("parent", "circuit", "sibling", "history"),
        )
        for order, expected in enumerate(expected_nonempty):
            generator = SemanticProposalGenerator(
                None, pcfg_context_order=order)
            generator.cfg_productions = dict(productions)
            generator.packed_nodes = dict(nodes)
            generator.packed_edges = dict(edges)
            generator.packed_root_ids = {node_ids["root"]}
            generator.ect_instances = {
                instance["instance_id"]: dict(instance)
                for instance in instances
            }
            generator._observe_pcfg_fragment(
                digest(f"fragment-{order}"),
                parser,
                productions,
                instances,
                edges,
                [node_ids["root"]],
            )
            stores = {
                "parent": generator.pcfg_contexts,
                "circuit": generator.pcfg_circuit_contexts,
                "sibling": generator.pcfg_sibling_contexts,
                "history": generator.pcfg_history_contexts,
            }
            self.assertEqual(
                {name for name, store in stores.items() if store},
                set(expected),
            )
            snapshot = generator.grammar_snapshot()
            self.assertEqual(snapshot["pcfg_context_order"], order)
            self.assertEqual(
                snapshot["pcfg_context_level"],
                generator.PCFG_CONTEXT_LEVELS[order],
            )
            self.assertEqual(
                [
                    snapshot["pcfg_parent_enabled"],
                    snapshot["pcfg_circuit_enabled"],
                    snapshot["pcfg_sibling_enabled"],
                    snapshot["pcfg_history_enabled"],
                ],
                [int(order >= level) for level in range(1, 5)],
            )
            if order == 0:
                self.assertFalse(
                    generator.
                    packed_context_alternative_probabilities)
            else:
                artifacts = tuple(
                    generator.
                    packed_context_alternative_probabilities.values()
                )
                self.assertTrue(artifacts)
                self.assertEqual(
                    any(item["circuit_context_id"] for item in artifacts),
                    order >= 2,
                )
                self.assertEqual(
                    any(item["sibling_context_id"] for item in artifacts),
                    order >= 3,
                )
                self.assertEqual(
                    any(item["history_context_id"] for item in artifacts),
                    order >= 4,
                )

        history_generator = generator
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "history-state.json")
            history_generator.state_path = state_path
            history_generator.save()
            global_restore = SemanticProposalGenerator(
                state_path, pcfg_context_order="global")
            self.assertTrue(
                global_restore.pcfg_state_order_mismatch_reset)
            self.assertFalse(global_restore.pcfg_observations)
            self.assertFalse(
                global_restore.
                packed_context_alternative_probabilities)

            path = os.path.join(tmp, "pcfg-research.json")
            generator = SemanticProposalGenerator(
                None, pcfg_context_order="history")
            self.assertTrue(generator.write_research_artifact(
                path,
                {
                    "run_id": "run-1",
                    "pair_id": "pair-1",
                    "configuration": "pcfg-history",
                },
            ))
            artifact = json.loads(Path(path).read_text())
            self.assertTrue(
                generator.verify_research_artifact(artifact))
            artifact["metrics"]["pcfg_context_order"] = 3
            self.assertFalse(
                generator.verify_research_artifact(artifact))

        with self.assertRaises(ValueError):
            SemanticProposalGenerator(
                None, pcfg_context_order="unbounded")

    def test_token_grammar_rule_synthesis(self):
        rules = synthesize_token_rules((
            b'"MAGIC"',
            b"type=image",
            b"/api/v1",
        ))
        rendered = {rule.kind: rule.render() for rule in rules}
        self.assertIn("literal", rendered)
        self.assertTrue(any(
            rule.kind == "delimited"
            and rule.render() == b'"MAGIC"'
            for rule in rules))
        self.assertTrue(any(
            rule.kind == "key_value"
            and rule.render() == b"type=image"
            for rule in rules))
        self.assertTrue(any(
            rule.kind == "segment"
            and rule.render() == b"api"
            for rule in rules))

    def test_solve_complete_uses_token_grammar_and_core_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "seed")
            Path(source).write_bytes(b'aa""--------')
            token_dir = os.path.join(tmp, "tokens")
            os.mkdir(token_dir)
            Path(token_dir, "magic").write_bytes(b"MAGIC")
            telemetry = SolverTelemetry(
                comparison_taints=((5, 42, 1, 2, 3, 1, 1),))
            generator = SemanticProposalGenerator(
                None, token_paths=(token_dir,), max_proposals_per_observation=16)
            proposals = generator.propose(source, telemetry)
            completed = [
                bytes.fromhex(proposal["candidate"]["hex"])
                for proposal in proposals
                if proposal["kind"] == "solve_complete"
            ]
            self.assertTrue(completed)
            self.assertTrue(any(
                candidate[2:9] == b'"MAGIC"'
                for candidate in completed))

    def test_online_grammar_spans_variable_length_and_feedback_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "seed")
            state = os.path.join(tmp, "grammar-state.json")
            Path(source).write_bytes(b"kind=A;tail")
            token_dir = os.path.join(tmp, "tokens")
            os.mkdir(token_dir)
            Path(token_dir, "long").write_bytes(b"LONGVALUE")
            telemetry = SolverTelemetry(
                comparison_taints=((7, 77, 1, 5, 5, 1, 1),))
            cores = extract_constraint_cores(telemetry, 11)
            spans = infer_token_spans(b"kind=A;tail", cores)
            self.assertEqual(
                [(span.lo, span.hi, span.token) for span in spans],
                [(5, 6, b"A")],
            )

            generator = SemanticProposalGenerator(
                state, token_paths=(token_dir,),
                max_proposals_per_observation=64)
            proposals = generator.propose(source, telemetry)
            grammar = [
                proposal for proposal in proposals
                if proposal["kind"] == "solve_complete"
                and "grammar_rule_id" in proposal
            ]
            self.assertTrue(grammar)
            variable = [
                proposal for proposal in grammar
                if len(bytes.fromhex(proposal["candidate"]["hex"])) >
                len(b"kind=A;tail")
            ]
            self.assertTrue(variable)

            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "proposals"))
            proposal_id = manager.ingest(variable[0])
            self.assertIsNotNone(proposal_id)
            record = manager.records[str(proposal_id)]
            self.assertEqual(
                record.grammar_rule_id, variable[0]["grammar_rule_id"])
            self.assertTrue(generator.observe_grammar_validation(
                record.grammar_rule_id, valid=True))
            self.assertTrue(generator.observe_grammar_retention(
                record.grammar_rule_id, 3))
            generator.save()

            restored = SemanticProposalGenerator(state)
            rule = restored.grammar_rules[record.grammar_rule_id]
            self.assertEqual(
                (rule.validations, rule.verified, rule.retained,
                 rule.coverage_features),
                (1, 1, 1, 3),
            )
            snapshot = restored.grammar_snapshot()
            self.assertGreaterEqual(snapshot["retained_rules"], 1)

    def test_query_ir_verified_grammar_hole_completion_and_tamper_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = b'{"k":"x"}'
            store_root = os.path.join(tmp, "query-store")
            store = QueryStore(store_root)
            query_id, created = store.ingest(self._query_envelope(content))
            self.assertTrue(created)
            lease = store.claim("query-hole-worker")
            self.assertIsNotNone(lease)
            assert lease is not None
            self.assertTrue(store.complete(
                lease,
                "query-hole-worker",
                {
                    "status": "sat",
                    "assignments": {"0": ord("{")},
                    "solver": "test",
                    "elapsed_us": 1,
                },
            ))
            candidate = store.materialize_candidates(
                query_id,
                {
                    "status": "sat",
                    "assignments": {"0": ord("{")},
                    "solver": "test",
                    "elapsed_us": 1,
                },
            )[0]
            manifest_path = candidate.with_suffix(".json")
            manifest = json.loads(
                manifest_path.read_text(encoding="ascii"))
            self.assertEqual(manifest["query_input_offsets"], [0])
            self.assertTrue(manifest["query_ir_verified"])

            token_dir = os.path.join(tmp, "tokens")
            os.mkdir(token_dir)
            Path(token_dir, "long").write_bytes(b"LONGVALUE")
            telemetry = SolverTelemetry(
                comparison_taints=((9, 77, 1, 6, 6, 1, 1),))
            generator = SemanticProposalGenerator(
                os.path.join(tmp, "grammar.json"),
                token_paths=(token_dir,),
                max_proposals_per_observation=64,
                query_store_root=store_root,
            )
            proposals = generator.propose(str(candidate), telemetry)
            holes = [
                proposal for proposal in proposals
                if proposal["kind"] == "query_hole_completion"
            ]
            self.assertTrue(holes)
            completed = [
                bytes.fromhex(proposal["candidate"]["hex"])
                for proposal in holes
            ]
            self.assertTrue(any(
                b"LONGVALUE" in value for value in completed))
            self.assertTrue(all(
                store.validate_candidate(query_id, value)
                for value in completed))
            certificate = holes[0]["query_hole_certificate"]
            self.assertEqual(certificate["query_id"], query_id)
            self.assertEqual(certificate["query_input_offsets"], [0])
            self.assertTrue(certificate["query_ir_verified"])

            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "proposals"))
            proposal_id = manager.ingest(holes[0])
            self.assertIsNotNone(proposal_id)
            record = manager.records[str(proposal_id)]
            self.assertEqual(record.kind, "query_hole_completion")
            self.assertEqual(record.query_id, query_id)
            self.assertTrue(record.query_ir_verified)
            self.assertEqual(
                (record.query_hole_lo, record.query_hole_hi), (6, 7))

            tampered_span = dict(holes[0])
            tampered_span["candidate"] = {"hex": "7b7d"}
            tampered_span["grammar_span"] = [0, 1]
            tampered_manager = VerifiedProposalManager(
                "", os.path.join(tmp, "tampered-span"))
            self.assertIsNone(tampered_manager.ingest(tampered_span))
            self.assertEqual(
                list((Path(tmp) / "tampered-span" / "candidates").iterdir()),
                [],
            )

            # A manifest cannot hide a constrained byte to authorize a hole.
            manifest["query_input_offsets"] = []
            manifest_path.write_text(
                json.dumps(manifest), encoding="ascii")
            rejected = SemanticProposalGenerator(
                None,
                token_paths=(token_dir,),
                max_proposals_per_observation=64,
                query_store_root=store_root,
            ).propose(str(candidate), telemetry)
            self.assertFalse(any(
                proposal["kind"] == "query_hole_completion"
                for proposal in rejected))
            snapshot = generator.grammar_snapshot()
            self.assertGreater(snapshot["query_hole_verified"], 0)

    def test_retained_history_acquisition_is_plateau_gated_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            retained = os.path.join(tmp, "retained")
            current = os.path.join(tmp, "current")
            state = os.path.join(tmp, "history-state.json")
            Path(retained).write_bytes(b"kind=LONGVALUE;")
            Path(current).write_bytes(b"kind=x;")
            telemetry = SolverTelemetry(
                comparison_taints=((3, 55, 1, 5, 5, 1, 1),))
            generator = SemanticProposalGenerator(
                state,
                max_proposals_per_observation=64,
                plateau_observations=2,
            )
            seed_id = generator.observe_history_seed(retained, 7)
            self.assertEqual(len(seed_id), 64)

            before = generator.propose(
                current, telemetry, coverage_delta=0)
            self.assertFalse(any(
                proposal["kind"] == "history_acquisition"
                for proposal in before))
            after = generator.propose(
                current, telemetry, coverage_delta=0)
            history = [
                proposal for proposal in after
                if proposal["kind"] == "history_acquisition"
            ]
            self.assertTrue(history)
            self.assertTrue(any(
                b"LONGVALUE" in bytes.fromhex(
                    proposal["candidate"]["hex"])
                for proposal in history))
            self.assertTrue(all(
                proposal["history_seed_id"] == seed_id
                for proposal in history))

            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "history-proposals"))
            proposal_id = manager.ingest(history[0])
            self.assertIsNotNone(proposal_id)
            record = manager.records[str(proposal_id)]
            self.assertEqual(record.history_seed_id, seed_id)
            manager.save()
            manager_restored = VerifiedProposalManager(
                "", os.path.join(tmp, "history-proposals"))
            self.assertEqual(
                manager_restored.records[str(proposal_id)].history_seed_id,
                seed_id,
            )
            self.assertTrue(generator.observe_history_validation(
                seed_id, valid=True))
            self.assertTrue(generator.observe_history_retention(seed_id, 3))
            generator.save()

            restored = SemanticProposalGenerator(
                state, plateau_observations=2)
            seed = restored.history_seeds[seed_id]
            self.assertEqual((seed.validations, seed.verified), (1, 1))
            self.assertGreaterEqual(seed.coverage_features, 10)
            reset = restored.propose(
                current, telemetry, coverage_delta=1)
            self.assertFalse(any(
                proposal["kind"] == "history_acquisition"
                for proposal in reset))
            self.assertEqual(restored.plateau_count, 0)

    def test_parser_rejection_splits_rule_by_local_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "seed")
            state = os.path.join(tmp, "parser-context.json")
            Path(source).write_bytes(b"kind=x;")
            token_dir = os.path.join(tmp, "tokens")
            os.mkdir(token_dir)
            Path(token_dir, "value").write_bytes(b"LONG")
            telemetry = SolverTelemetry(
                comparison_taints=((4, 88, 1, 5, 5, 1, 1),))
            generator = SemanticProposalGenerator(
                state,
                token_paths=(token_dir,),
                max_proposals_per_observation=64,
            )
            proposals = generator.propose(source, telemetry)
            grammar = next(
                proposal for proposal in proposals
                if proposal["kind"] == "solve_complete"
                and proposal.get("grammar_context_id")
            )
            rule_id = grammar["grammar_rule_id"]
            context_id = grammar["grammar_context_id"]
            for _ in range(2):
                self.assertTrue(generator.observe_grammar_validation(
                    rule_id,
                    valid=False,
                    parser_valid=False,
                    context_id=context_id,
                ))
            generator.save()

            restored = SemanticProposalGenerator(
                state,
                token_paths=(token_dir,),
                max_proposals_per_observation=64,
            )
            self.assertTrue(
                restored.grammar_rules[rule_id].context_rejected(context_id))
            blocked = restored.propose(source, telemetry)
            self.assertFalse(any(
                proposal.get("grammar_rule_id") == rule_id
                and proposal.get("grammar_context_id") == context_id
                for proposal in blocked))

            self.assertTrue(restored.observe_grammar_validation(
                rule_id,
                valid=True,
                parser_valid=True,
                context_id=context_id,
            ))
            reopened = restored.propose(source, telemetry)
            self.assertTrue(any(
                proposal.get("grammar_rule_id") == rule_id
                and proposal.get("grammar_context_id") == context_id
                for proposal in reopened))

    def test_structural_parser_context_replaces_lexical_context_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "seed")
            state = os.path.join(tmp, "structural-context.json")
            Path(source).write_bytes(b"kind=x;")
            token_dir = os.path.join(tmp, "tokens")
            os.mkdir(token_dir)
            Path(token_dir, "value").write_bytes(b"LONG")
            telemetry = SolverTelemetry(
                comparison_taints=((4, 88, 1, 5, 5, 1, 1),))
            generator = SemanticProposalGenerator(
                state,
                token_paths=(token_dir,),
                max_proposals_per_observation=64,
            )
            proposal = next(
                item for item in generator.propose(source, telemetry)
                if item["kind"] == "solve_complete"
                and item.get("grammar_span")
            )
            source_context = proposal["grammar_source_context_id"]
            self.assertEqual(
                proposal["grammar_context_id"], source_context)
            self.assertEqual(len(proposal["grammar_span"]), 2)

            script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "json.dump({'schema':'symcc-parser-structural-trace-v1',"
                "'parser':'fixture-v1','accepted':False,'nodes':["
                "{'symbol':'document','state':'root','start':0,'end':len(data),"
                "'parent':-1},{'symbol':'field_value','state':'after-equals',"
                "'start':0,'end':len(data),'parent':0}]},"
                "open(sys.argv[2],'w'));raise SystemExit(3)"
            )
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "proposals"),
                parser_command=(
                    sys.executable, "-c", script, "{input}", "{trace}"),
            )
            proposal_id = manager.ingest(proposal)
            self.assertIsNotNone(proposal_id)
            self.assertTrue(generator.observe_grammar_validation(
                proposal["grammar_rule_id"],
                valid=True,
            ))
            for _ in range(2):
                self.assertFalse(manager.validate(
                    str(proposal_id),
                    SolverTelemetry(
                        target_branch=int(proposal["target_branch"]),
                        target_reached=True,
                    ),
                    retcode=0,
                    killed=False,
                ))
                record = manager.records[str(proposal_id)]
                self.assertEqual(record.parser_symbol, "field_value")
                self.assertTrue(generator.observe_grammar_validation(
                    record.grammar_rule_id,
                    valid=False,
                    parser_valid=False,
                    context_id=record.parser_context_id,
                    source_context_id=record.grammar_source_context_id,
                ))
            structural_context = manager.records[
                str(proposal_id)].parser_context_id
            self.assertEqual(
                generator._resolved_parser_context(
                    proposal["grammar_rule_id"], source_context),
                structural_context,
            )
            self.assertTrue(
                generator.grammar_rules[
                    proposal["grammar_rule_id"]].context_rejected(
                        structural_context))
            self.assertTrue(generator.observe_grammar_retention(
                proposal["grammar_rule_id"],
                7,
                context_id=structural_context,
            ))
            rule = generator.grammar_rules[proposal["grammar_rule_id"]]
            self.assertEqual(
                rule.structural_coverage[structural_context], 7)
            self.assertLess(
                rule.score_for_context(structural_context),
                rule.score_for_context("f" * 64),
            )
            generator.save()

            restored = SemanticProposalGenerator(
                state,
                token_paths=(token_dir,),
                max_proposals_per_observation=64,
            )
            self.assertEqual(
                restored._resolved_parser_context(
                    proposal["grammar_rule_id"], source_context),
                structural_context,
            )
            self.assertEqual(
                restored.grammar_rules[
                    proposal["grammar_rule_id"]].structural_coverage[
                        structural_context],
                7,
            )
            blocked = restored.propose(source, telemetry)
            self.assertFalse(any(
                item.get("grammar_rule_id") == proposal["grammar_rule_id"]
                and item.get("grammar_context_id") == structural_context
                for item in blocked))

    def test_surrogate_transfers_higher_progress_exemplar(self):
        with tempfile.TemporaryDirectory() as tmp:
            high = os.path.join(tmp, "high")
            low = os.path.join(tmp, "low")
            Path(high).write_bytes(b"xxMAGICyy")
            Path(low).write_bytes(b"xx------yy")
            generator = SemanticProposalGenerator(
                None, max_proposals_per_observation=16)
            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "proposals"))
            high_telemetry = SolverTelemetry(
                comparison_taints=((1, 7, 5, 2, 7, 1, 1),),
                data_features=((123, 40, 48),),
            )
            low_telemetry = SolverTelemetry(
                comparison_taints=((1, 7, 5, 2, 7, 1, 1),),
                data_features=((123, 8, 48),),
            )
            generator.generate_into(manager, high, high_telemetry)
            before = set(manager.records)
            generator.generate_into(manager, low, low_telemetry)
            new_records = [
                record for key, record in manager.records.items()
                if key not in before and record.kind == "surrogate"
            ]
            self.assertTrue(new_records)
            self.assertTrue(any(
                Path(record.candidate_path).read_bytes()[2:8] == b"MAGICy"
                for record in new_records))

    def test_recursive_production_and_independent_grammar_bitmap_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "seed")
            state = os.path.join(tmp, "grammar-state.json")
            Path(source).write_bytes(b"x")
            token_dir = os.path.join(tmp, "tokens")
            os.mkdir(token_dir)
            Path(token_dir, "atom").write_bytes(b"A")
            telemetry = SolverTelemetry(
                comparison_taints=((5, 42, 1, 0, 0, 1, 1),))
            generator = SemanticProposalGenerator(
                state,
                token_paths=(token_dir,),
                max_proposals_per_observation=64,
            )
            proposals = generator.propose(source, telemetry)
            grammar = next(
                proposal for proposal in proposals
                if proposal["kind"] == "solve_complete")
            rule_id = grammar["grammar_rule_id"]
            source_context = grammar["grammar_source_context_id"]
            context_id = "c" * 64
            production_id = "d" * 64
            self.assertTrue(generator.observe_grammar_validation(
                rule_id,
                valid=True,
                parser_valid=True,
                context_id=context_id,
                source_context_id=source_context,
                production_id=production_id,
                recursion_prefix_hex="28",
                recursion_suffix_hex="29",
            ))
            recursive_rules = [
                rule for rule in generator.grammar_rules.values()
                if rule.kind == "recursive"
            ]
            self.assertEqual(len(recursive_rules), 1)
            self.assertEqual(recursive_rules[0].render(), b"()")
            self.assertEqual(
                generator._resolved_parser_production(
                    rule_id, source_context),
                production_id,
            )
            self.assertEqual(
                generator._grammar_bitmap_count(
                    rule_id, context_id, production_id),
                1,
            )

            cores = extract_constraint_cores(telemetry, 1)
            spans = infer_token_spans(b"x", cores)
            candidates = list(generator._solve_complete_candidates(
                b"x", cores, spans))
            self.assertTrue(any(
                candidate[2] == b"(x)"
                and candidate[3] == recursive_rules[0].rule_id
                for candidate in candidates
            ))
            snapshot = generator.grammar_snapshot()
            self.assertEqual(snapshot["grammar_bitmap_slots"], 1)
            self.assertEqual(snapshot["grammar_bitmap_events"], 1)
            self.assertEqual(snapshot["recursive_rules"], 1)
            generator.save()

            restored = SemanticProposalGenerator(state)
            self.assertEqual(restored.SCHEMA, 25)
            self.assertEqual(
                restored.grammar_snapshot()["grammar_bitmap_slots"], 1)
            self.assertEqual(
                restored._resolved_parser_production(
                    rule_id, source_context),
                production_id,
            )
            self.assertEqual(
                restored.grammar_rules[recursive_rules[0].rule_id].kind,
                "recursive",
            )

    def test_mutual_cfg_cycle_drives_bounded_derivation_and_tamper_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "json.dump({'schema':'symcc-parser-structural-trace-v1',"
                "'parser':'mutual-fixture-v1','accepted':True,'nodes':["
                "{'symbol':'a','state':'outer','start':0,'end':len(data),"
                "'parent':-1},{'symbol':'b','state':'bridge','start':0,"
                "'end':len(data),'parent':0},{'symbol':'a','state':'inner',"
                "'start':1,'end':len(data)-1,'parent':1},"
                "{'symbol':'value','state':'atom','start':1,"
                "'end':len(data)-1,'parent':2}]},open(sys.argv[2],'w'))"
            )
            state = os.path.join(tmp, "cfg-state.json")
            generator = SemanticProposalGenerator(
                state, max_cfg_derivation_depth=3)
            source_rule = generator._learn_rule("literal", b"", b"x")
            self.assertIsNotNone(source_rule)
            assert source_rule is not None
            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "proposals"),
                parser_command=(
                    sys.executable, "-c", parser_script, "{input}", "{trace}"),
            )
            source_context = "a" * 64
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "<x>"},
                "target_branch": 0,
                "grammar_rule_id": source_rule.rule_id,
                "grammar_context_id": source_context,
                "grammar_source_context_id": source_context,
                "grammar_span": [1, 2],
            })
            self.assertIsNotNone(proposal_id)
            self.assertTrue(manager.validate(
                str(proposal_id), None, retcode=0, killed=False))
            record = manager.records[str(proposal_id)]
            fragment = json.loads(record.parser_cfg_fragment_json)
            self.assertEqual(len(fragment["cycles"]), 1)
            self.assertEqual(fragment["cycles"][0]["kind"], "mutual")
            self.assertEqual(
                [item[0] for item in fragment["cycles"][0]["path"]],
                ["a", "b", "a"],
            )
            self.assertTrue(generator.observe_grammar_validation(
                source_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=record.parser_context_id,
                source_context_id=source_context,
                production_id=record.parser_production_id,
                cfg_fragment_json=record.parser_cfg_fragment_json,
                cfg_fragment_sha256=record.parser_cfg_fragment_sha256,
            ))
            recursive_rule = next(
                rule for rule in generator.grammar_rules.values()
                if rule.kind == "recursive")
            self.assertEqual(recursive_rule.render(), b"<>")
            cores = extract_constraint_cores(
                SolverTelemetry(
                    comparison_taints=((1, 9, 1, 0, 0, 1, 1),)),
                1,
            )
            spans = infer_token_spans(b"x", cores)
            generated = {
                candidate[2]
                for candidate in generator._solve_complete_candidates(
                    b"x", cores, spans)
                if candidate[3] == recursive_rule.rule_id
            }
            self.assertEqual(
                generated, {b"<x>", b"<<x>>", b"<<<x>>>"})
            size_bounded = SemanticProposalGenerator(
                None,
                max_input_bytes=4,
                max_cfg_derivation_depth=8,
            )
            bounded_rule = size_bounded._learn_rule(
                "recursive", b"<", b"", b">")
            assert bounded_rule is not None
            self.assertEqual(
                size_bounded._rule_renderings(bounded_rule, b"x"),
                ((b"<x>", 1),),
            )
            snapshot = generator.grammar_snapshot()
            self.assertEqual(snapshot["cfg_productions"], 4)
            self.assertEqual(snapshot["cfg_cycles"], 1)
            self.assertEqual(snapshot["cfg_mutual_cycles"], 1)
            self.assertEqual(snapshot["cfg_derivation_depth"], 3)
            generator.save()

            restored = SemanticProposalGenerator(
                state, max_cfg_derivation_depth=3)
            self.assertEqual(restored.grammar_snapshot()["cfg_cycles"], 1)
            self.assertIn(
                fragment["cycles"][0]["cycle_id"],
                restored.recursive_rule_cycles[recursive_rule.rule_id],
            )

            rejected = SemanticProposalGenerator(None)
            rejected_rule = rejected._learn_rule("literal", b"", b"x")
            assert rejected_rule is not None
            self.assertTrue(rejected.observe_grammar_validation(
                rejected_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=record.parser_context_id,
                source_context_id=source_context,
                production_id=record.parser_production_id,
                cfg_fragment_json=record.parser_cfg_fragment_json + " ",
                cfg_fragment_sha256=record.parser_cfg_fragment_sha256,
            ))
            self.assertEqual(rejected.grammar_snapshot()["cfg_cycles"], 0)
            self.assertFalse(any(
                rule.kind == "recursive"
                for rule in rejected.grammar_rules.values()))

    def test_incremental_ect_subtree_correspondence_and_tamper_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "json.dump({'schema':'symcc-parser-structural-trace-v1',"
                "'parser':'ect-fixture-v1','accepted':True,'nodes':["
                "{'symbol':'document','state':'root','start':0,"
                "'end':len(data),'parent':-1},"
                "{'symbol':'value','state':'atom','start':0,"
                "'end':len(data),'parent':0}]},open(sys.argv[2],'w'))"
            )
            state = os.path.join(tmp, "ect-state.json")
            generator = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            source_rule = generator._learn_rule("literal", b"", b"x")
            assert source_rule is not None
            source_span = infer_token_spans(
                b"x",
                extract_constraint_cores(
                    SolverTelemetry(
                        comparison_taints=((1, 9, 1, 0, 0, 1, 1),)),
                    1,
                ),
            )[0]
            source_context = _grammar_context_id(b"x", source_span)
            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "ect-proposals"),
                parser_command=(
                    sys.executable, "-c", parser_script, "{input}", "{trace}"),
            )
            records = []
            for value in ("A", "B"):
                proposal_id = manager.ingest({
                    "kind": "solve_complete",
                    "candidate": {"text": value},
                    "target_branch": 0,
                    "grammar_rule_id": source_rule.rule_id,
                    "grammar_context_id": source_context,
                    "grammar_source_context_id": source_context,
                    "grammar_span": [0, 1],
                })
                self.assertIsNotNone(proposal_id)
                self.assertTrue(manager.validate(
                    str(proposal_id), None, retcode=0, killed=False))
                record = manager.records[str(proposal_id)]
                records.append(record)
                self.assertTrue(generator.observe_grammar_validation(
                    source_rule.rule_id,
                    valid=True,
                    parser_valid=True,
                    context_id=record.parser_context_id,
                    source_context_id=source_context,
                    candidate_context_id=(
                        record.grammar_candidate_context_id),
                    production_id=record.parser_production_id,
                    cfg_fragment_json=record.parser_cfg_fragment_json,
                    cfg_fragment_sha256=record.parser_cfg_fragment_sha256,
                ))

            self.assertEqual(
                records[0].parser_ect_shape_id,
                records[1].parser_ect_shape_id,
            )
            self.assertNotEqual(
                records[0].parser_ect_instance_id,
                records[1].parser_ect_instance_id,
            )
            snapshot = generator.grammar_snapshot()
            self.assertEqual(snapshot["ect_shapes"], 2)
            self.assertEqual(snapshot["ect_instances"], 4)
            self.assertEqual(snapshot["ect_correspondence_classes"], 2)
            self.assertEqual(snapshot["ect_subtree_rules"], 2)

            cores = extract_constraint_cores(
                SolverTelemetry(
                    comparison_taints=((1, 9, 1, 0, 0, 1, 1),)),
                1,
            )
            spans = infer_token_spans(b"x", cores)
            rendered = {
                candidate[2]
                for candidate in generator._solve_complete_candidates(
                    b"x", cores, spans)
                if generator.grammar_rules[
                    candidate[3]].kind == "subtree"
            }
            self.assertEqual(rendered, {b"A", b"B"})
            wrong_span = type(spans[0])(
                spans[0].lo, spans[0].hi, spans[0].token, 10)
            self.assertFalse(any(
                generator.grammar_rules[candidate[3]].kind == "subtree"
                for candidate in generator._solve_complete_candidates(
                    b"x", cores, (wrong_span,))
            ))

            generator.save()
            restored = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            self.assertEqual(
                restored.grammar_snapshot()["ect_correspondence_classes"], 2)
            self.assertEqual(
                {
                    candidate[2]
                    for candidate in restored._solve_complete_candidates(
                        b"x", cores, spans)
                    if restored.grammar_rules[
                        candidate[3]].kind == "subtree"
                },
                {b"A", b"B"},
            )

            tampered_fragment = json.loads(
                records[0].parser_cfg_fragment_json)
            selected = next(
                item for item in tampered_fragment["instances"]
                if item["selected"])
            selected["yield_hex"] = "43"
            tampered_json = json.dumps(
                tampered_fragment,
                sort_keys=True,
                separators=(",", ":"),
            )
            rejected = SemanticProposalGenerator(None)
            rejected_rule = rejected._learn_rule("literal", b"", b"x")
            assert rejected_rule is not None
            self.assertTrue(rejected.observe_grammar_validation(
                rejected_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=records[0].parser_context_id,
                source_context_id=source_context,
                production_id=records[0].parser_production_id,
                cfg_fragment_json=tampered_json,
                cfg_fragment_sha256=hashlib.sha256(
                    tampered_json.encode("utf-8")).hexdigest(),
            ))
            self.assertEqual(rejected.grammar_snapshot()["ect_instances"], 0)

    def test_packed_epsilon_trace_learns_context_safe_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "nodes=(["
                "{'symbol':'choice','state':'ambiguous','start':0,'end':1,"
                "'parent':-1,'alternatives':[[1],[2]]},"
                "{'symbol':'token','state':'atom','start':0,'end':1,"
                "'parent':0},"
                "{'symbol':'empty','state':'epsilon','start':0,'end':0,"
                "'parent':0,'epsilon':True}] if data else ["
                "{'symbol':'empty','state':'epsilon','start':0,'end':0,"
                "'parent':-1,'epsilon':True}]);"
                "json.dump({'schema':'symcc-parser-structural-trace-v2',"
                "'parser':'packed-epsilon-fixture-v2','accepted':True,"
                "'nodes':nodes},open(sys.argv[2],'w'))"
            )
            state = os.path.join(tmp, "epsilon-state.json")
            source = os.path.join(tmp, "source")
            Path(source).write_bytes(b"x")
            generator = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            source_rule = generator._learn_rule("literal", b"", b"x")
            assert source_rule is not None
            telemetry = SolverTelemetry(
                comparison_taints=((1, 9, 1, 0, 0, 1, 1),))
            cores = extract_constraint_cores(telemetry, 1)
            spans = infer_token_spans(b"x", cores)
            source_context = _grammar_context_id(b"x", spans[0])
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "epsilon-proposals"),
                parser_command=(
                    sys.executable,
                    "-c",
                    parser_script,
                    "{input}",
                    "{trace}",
                ),
            )
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "source_path": source,
                "candidate": {"text": "x"},
                "target_branch": 9,
                "grammar_rule_id": source_rule.rule_id,
                "grammar_context_id": source_context,
                "grammar_source_context_id": source_context,
                "grammar_span": [0, 1],
            })
            self.assertIsNotNone(proposal_id)
            self.assertTrue(manager.validate(
                str(proposal_id),
                SolverTelemetry(target_branch=9, target_reached=True),
                retcode=0,
                killed=False,
            ))
            packed_record = manager.records[str(proposal_id)]
            self.assertTrue(generator.observe_grammar_validation(
                source_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=packed_record.parser_context_id,
                source_context_id=source_context,
                candidate_context_id=(
                    packed_record.grammar_candidate_context_id),
                production_id=packed_record.parser_production_id,
                cfg_fragment_json=packed_record.parser_cfg_fragment_json,
                cfg_fragment_sha256=(
                    packed_record.parser_cfg_fragment_sha256),
            ))
            self.assertEqual(
                generator.grammar_snapshot()[
                    "cfg_ambiguous_productions"],
                1,
            )
            self.assertFalse(any(
                rule.kind == "epsilon"
                for rule in generator.grammar_rules.values()
            ))

            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "source_path": source,
                "candidate": {"hex": ""},
                "target_branch": 9,
                "grammar_rule_id": source_rule.rule_id,
                "grammar_context_id": source_context,
                "grammar_source_context_id": source_context,
                "grammar_span": [0, 0],
            })
            self.assertIsNotNone(proposal_id)
            self.assertTrue(manager.validate(
                str(proposal_id),
                SolverTelemetry(target_branch=9, target_reached=True),
                retcode=0,
                killed=False,
            ))
            record = manager.records[str(proposal_id)]
            self.assertTrue(generator.observe_grammar_validation(
                source_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=record.parser_context_id,
                source_context_id=source_context,
                candidate_context_id=(
                    record.grammar_candidate_context_id),
                production_id=record.parser_production_id,
                cfg_fragment_json=record.parser_cfg_fragment_json,
                cfg_fragment_sha256=record.parser_cfg_fragment_sha256,
            ))
            epsilon_rule = next(
                rule for rule in generator.grammar_rules.values()
                if rule.kind == "epsilon")
            generated = list(generator._solve_complete_candidates(
                b"x", cores, spans))
            self.assertTrue(any(
                candidate[2] == b"" and
                candidate[3] == epsilon_rule.rule_id
                for candidate in generated
            ))
            wrong_span = type(spans[0])(
                spans[0].lo, spans[0].hi, spans[0].token, 10)
            self.assertFalse(any(
                generator.grammar_rules[candidate[3]].kind == "epsilon"
                for candidate in generator._solve_complete_candidates(
                    b"x", cores, (wrong_span,))
            ))
            snapshot = generator.grammar_snapshot()
            self.assertEqual(snapshot["cfg_ambiguous_productions"], 1)
            self.assertEqual(snapshot["cfg_epsilon_productions"], 1)
            self.assertEqual(snapshot["epsilon_rules"], 1)
            self.assertEqual(snapshot["ect_epsilon_rules"], 1)
            generator.save()

            restored = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            self.assertEqual(restored.SCHEMA, 25)
            self.assertEqual(
                restored.grammar_snapshot()["epsilon_rules"], 1)
            self.assertTrue(any(
                candidate[2] == b"" and
                restored.grammar_rules[candidate[3]].kind == "epsilon"
                for candidate in restored._solve_complete_candidates(
                    b"x", cores, spans)
            ))

            tampered = json.loads(record.parser_cfg_fragment_json)
            selected = next(
                instance for instance in tampered["instances"]
                if instance["selected"])
            selected["yield_hex"] = "41"
            tampered_json = json.dumps(
                tampered, sort_keys=True, separators=(",", ":"))
            rejected = SemanticProposalGenerator(None)
            rejected_rule = rejected._learn_rule(
                "literal", b"", b"x")
            assert rejected_rule is not None
            self.assertTrue(rejected.observe_grammar_validation(
                rejected_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=record.parser_context_id,
                source_context_id=source_context,
                candidate_context_id=(
                    record.grammar_candidate_context_id),
                production_id=record.parser_production_id,
                cfg_fragment_json=tampered_json,
                cfg_fragment_sha256=hashlib.sha256(
                    tampered_json.encode("utf-8")).hexdigest(),
            ))
            self.assertEqual(rejected.grammar_snapshot()["epsilon_rules"], 0)
            self.assertEqual(rejected.grammar_snapshot()["ect_instances"], 0)

    def test_shared_packed_dag_learns_deep_context_safe_subtree(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,sys;"
                "json.dump({'schema':'symcc-parser-structural-trace-v3',"
                "'parser':'shared-packed-fixture-v3','accepted':True,"
                "'roots':[0],'nodes':["
                "{'symbol':'root','state':'document','start':0,'end':3,"
                "'alternatives':[[1,4],[4,2]]},"
                "{'symbol':'value','state':'atom','start':0,'end':1},"
                "{'symbol':'wrapper','state':'alternative','start':2,'end':3,"
                "'alternatives':[[3]]},"
                "{'symbol':'value','state':'atom','start':2,'end':3},"
                "{'symbol':'delimiter','state':'comma','start':1,'end':2}"
                "]},open(sys.argv[2],'w'))"
            )
            source = os.path.join(tmp, "source")
            Path(source).write_bytes(b"a")
            state = os.path.join(tmp, "shared-packed-state.json")
            generator = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            source_rule = generator._learn_rule("literal", b"", b"a")
            assert source_rule is not None
            telemetry = SolverTelemetry(
                comparison_taints=((1, 9, 1, 0, 0, 1, 1),))
            cores = extract_constraint_cores(telemetry, 1)
            spans = infer_token_spans(b"a", cores)
            source_context = _grammar_context_id(b"a", spans[0])
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "shared-packed-proposals"),
                parser_command=(
                    sys.executable,
                    "-c",
                    parser_script,
                    "{input}",
                    "{trace}",
                ),
            )
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "source_path": source,
                "candidate": {"text": "a,b"},
                "target_branch": 9,
                "grammar_rule_id": source_rule.rule_id,
                "grammar_context_id": source_context,
                "grammar_source_context_id": source_context,
                "grammar_span": [0, 1],
            })
            self.assertIsNotNone(proposal_id)
            self.assertTrue(manager.validate(
                str(proposal_id),
                SolverTelemetry(target_branch=9, target_reached=True),
                retcode=0,
                killed=False,
            ))
            record = manager.records[str(proposal_id)]
            self.assertTrue(generator.observe_grammar_validation(
                source_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=record.parser_context_id,
                source_context_id=source_context,
                candidate_context_id=(
                    record.grammar_candidate_context_id),
                production_id=record.parser_production_id,
                cfg_fragment_json=record.parser_cfg_fragment_json,
                cfg_fragment_sha256=record.parser_cfg_fragment_sha256,
            ))
            snapshot = generator.grammar_snapshot()
            self.assertEqual(snapshot["packed_nodes"], 5)
            self.assertEqual(snapshot["packed_edges"], 5)
            self.assertEqual(snapshot["packed_shared_nodes"], 1)
            self.assertGreaterEqual(snapshot["packed_deep_instances"], 1)
            generated = list(generator._solve_complete_candidates(
                b"a", cores, spans))
            self.assertTrue(any(
                candidate[2] == b"b" and
                generator.grammar_rules[candidate[3]].kind == "subtree"
                for candidate in generated
            ))
            wrong_span = type(spans[0])(
                spans[0].lo, spans[0].hi, spans[0].token, 10)
            self.assertFalse(any(
                candidate[2] == b"b" and
                generator.grammar_rules[candidate[3]].kind == "subtree"
                for candidate in generator._solve_complete_candidates(
                    b"a", cores, (wrong_span,))
            ))
            generator.save()

            restored = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            self.assertEqual(restored.SCHEMA, 25)
            self.assertEqual(
                restored.grammar_snapshot()["packed_shared_nodes"], 1)
            self.assertTrue(any(
                candidate[2] == b"b" and
                restored.grammar_rules[candidate[3]].kind == "subtree"
                for candidate in restored._solve_complete_candidates(
                    b"a", cores, spans)
            ))
            deep_instance = next(
                instance for instance in restored.ect_instances.values()
                if instance.get("yield_hex") == "62"
            )
            deep_pair = tuple(deep_instance["node_path"][-2:])
            deep_edge_id = next(
                edge_id
                for edge_id, edge in restored.packed_edges.items()
                if (
                    edge["parent_id"],
                    edge["child_id"],
                ) == deep_pair
            )
            restored._drop_packed_edge(deep_edge_id)
            self.assertNotIn(
                deep_instance["instance_id"], restored.ect_instances)
            self.assertFalse(any(
                candidate[2] == b"b" and
                restored.grammar_rules[candidate[3]].kind == "subtree"
                for candidate in restored._solve_complete_candidates(
                    b"a", cores, spans)
            ))

            tampered = json.loads(record.parser_cfg_fragment_json)
            tampered["packed_edges"][0]["alternative"] = 7
            tampered_json = json.dumps(
                tampered, sort_keys=True, separators=(",", ":"))
            rejected = SemanticProposalGenerator(None)
            rejected_rule = rejected._learn_rule(
                "literal", b"", b"a")
            assert rejected_rule is not None
            self.assertTrue(rejected.observe_grammar_validation(
                rejected_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=record.parser_context_id,
                source_context_id=source_context,
                candidate_context_id=(
                    record.grammar_candidate_context_id),
                production_id=record.parser_production_id,
                cfg_fragment_json=tampered_json,
                cfg_fragment_sha256=hashlib.sha256(
                    tampered_json.encode("utf-8")).hexdigest(),
            ))
            self.assertEqual(rejected.grammar_snapshot()["packed_nodes"], 0)
            self.assertEqual(rejected.grammar_snapshot()["ect_instances"], 0)

    def test_pcfg_posterior_inside_outside_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,sys;"
                "data=open(sys.argv[1],'rb').read();"
                "left=data.startswith(b'L');"
                "alternatives=[[1],[2]] if left else [[2],[1]];"
                "json.dump({"
                "'schema':'symcc-parser-structural-trace-v3',"
                "'parser':'pcfg-inside-outside-fixture-v3',"
                "'accepted':True,'roots':[0],'nodes':["
                "{'symbol':'root','state':'document','start':0,'end':2,"
                "'alternatives':alternatives},"
                "{'symbol':'left','state':'atom','start':0,'end':2},"
                "{'symbol':'right','state':'atom','start':0,'end':2}"
                "]},open(sys.argv[2],'w'))"
            )
            source = os.path.join(tmp, "source")
            state = os.path.join(tmp, "pcfg-state.json")
            Path(source).write_bytes(b"L0")
            generator = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            source_rule = generator._learn_rule(
                "literal", b"", b"L0")
            assert source_rule is not None
            telemetry = SolverTelemetry(
                comparison_taints=((1, 9, 2, 0, 1, 1, 1),))
            spans = infer_token_spans(
                b"L0", extract_constraint_cores(telemetry, 2))
            source_context = _grammar_context_id(b"L0", spans[0])
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "pcfg-proposals"),
                parser_command=(
                    sys.executable,
                    "-c",
                    parser_script,
                    "{input}",
                    "{trace}",
                ),
            )
            records = []
            for candidate in (b"L0", b"L1", b"L2", b"R0"):
                proposal_id = manager.ingest({
                    "kind": "solve_complete",
                    "source_path": source,
                    "candidate": {"hex": candidate.hex()},
                    "target_branch": 9,
                    "grammar_rule_id": source_rule.rule_id,
                    "grammar_context_id": source_context,
                    "grammar_source_context_id": source_context,
                    "grammar_span": [0, 2],
                })
                self.assertIsNotNone(proposal_id)
                self.assertTrue(manager.validate(
                    str(proposal_id),
                    SolverTelemetry(
                        target_branch=9, target_reached=True),
                    retcode=0,
                    killed=False,
                ))
                record = manager.records[str(proposal_id)]
                records.append(record)
                self.assertTrue(generator.observe_grammar_validation(
                    source_rule.rule_id,
                    valid=True,
                    parser_valid=True,
                    context_id=record.parser_context_id,
                    source_context_id=source_context,
                    candidate_context_id=(
                        record.grammar_candidate_context_id),
                    production_id=record.parser_production_id,
                    cfg_fragment_json=record.parser_cfg_fragment_json,
                    cfg_fragment_sha256=(
                        record.parser_cfg_fragment_sha256),
                ))

            root_productions = [
                production
                for production in generator.cfg_productions.values()
                if production["lhs"] == "root"
            ]
            self.assertEqual(len(root_productions), 2)
            shapes = {
                production["rhs"][0]["symbol"]:
                    production["shape_id"]
                for production in root_productions
            }
            family_id, _ = generator._pcfg_family(
                "pcfg-inside-outside-fixture-v3",
                "root",
                "document",
            )
            self.assertEqual(
                generator.pcfg_counts[family_id],
                {shapes["left"]: 3, shapes["right"]: 1},
            )
            self.assertAlmostEqual(
                generator._pcfg_probability(
                    family_id, shapes["left"]),
                0.7,
            )
            self.assertAlmostEqual(
                generator._pcfg_probability(
                    family_id, shapes["right"]),
                0.3,
            )
            self.assertEqual(
                generator._pcfg_shape_statistics(shapes["right"])[1],
                1,
            )
            self.assertAlmostEqual(
                generator._pcfg_shape_statistics(
                    shapes["right"])[0],
                0.3,
            )
            right_count = generator.pcfg_counts[
                family_id].pop(shapes["right"])
            generator._refresh_probabilistic_state()
            self.assertAlmostEqual(
                generator._pcfg_shape_statistics(
                    shapes["right"])[0],
                0.125,
            )
            generator.pcfg_counts[family_id][
                shapes["right"]] = right_count
            generator._refresh_probabilistic_state()

            root_ids = {
                node_id
                for node_id, node in generator.packed_nodes.items()
                if node["symbol"] == "root"
            }
            self.assertEqual(len(root_ids), 4)
            self.assertTrue(all(
                abs(generator.packed_inside[root_id] - 1.0) < 1e-12
                for root_id in root_ids
            ))
            left_alternatives = [
                alternative
                for alternative in
                generator.packed_alternative_probabilities.values()
                if alternative["shape_id"] == shapes["left"]
            ]
            right_alternatives = [
                alternative
                for alternative in
                generator.packed_alternative_probabilities.values()
                if alternative["shape_id"] == shapes["right"]
            ]
            self.assertEqual(
                (len(left_alternatives), len(right_alternatives)),
                (4, 4),
            )
            self.assertTrue(all(
                abs(alternative["posterior"] - 0.7) < 1e-12 and
                abs(alternative["outside_mass"] - 0.7) < 1e-12
                for alternative in left_alternatives
            ))
            self.assertTrue(all(
                abs(alternative["posterior"] - 0.3) < 1e-12 and
                abs(alternative["outside_mass"] - 0.3) < 1e-12
                for alternative in right_alternatives
            ))
            before = sum(
                sum(counts.values())
                for counts in generator.pcfg_counts.values())
            duplicate = records[-1]
            self.assertTrue(generator.observe_grammar_validation(
                source_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=duplicate.parser_context_id,
                source_context_id=source_context,
                candidate_context_id=(
                    duplicate.grammar_candidate_context_id),
                production_id=duplicate.parser_production_id,
                cfg_fragment_json=duplicate.parser_cfg_fragment_json,
                cfg_fragment_sha256=(
                    duplicate.parser_cfg_fragment_sha256),
            ))
            self.assertEqual(
                sum(
                    sum(counts.values())
                    for counts in generator.pcfg_counts.values()),
                before,
            )
            snapshot = generator.grammar_snapshot()
            self.assertEqual(snapshot["pcfg_observations"], 4)
            self.assertEqual(snapshot["pcfg_selected_productions"], 8)
            self.assertEqual(snapshot["pcfg_inside_nodes"], 12)
            self.assertEqual(snapshot["pcfg_packed_alternatives"], 16)
            self.assertEqual(snapshot["packed_root_evidence"], 4)
            subtree_rule = next(
                rule
                for rule in generator.grammar_rules.values()
                if rule.kind == "subtree"
            )
            self.assertEqual(
                len(generator._pareto_objectives(
                    subtree_rule, source_context)),
                9,
            )
            generator.save()

            restored = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            self.assertEqual(restored.SCHEMA, 25)
            self.assertEqual(
                restored.pcfg_counts[family_id],
                generator.pcfg_counts[family_id],
            )
            self.assertEqual(
                restored.pcfg_observations,
                generator.pcfg_observations,
            )
            self.assertAlmostEqual(
                restored._pcfg_probability(
                    family_id, shapes["left"]),
                0.7,
            )
            self.assertTrue(all(
                abs(restored.packed_inside[root_id] - 1.0) < 1e-12
                for root_id in root_ids
            ))
            self.assertEqual(len(restored.packed_root_evidence), 4)

            serialized = json.loads(Path(state).read_text())
            tampered_state = os.path.join(tmp, "tampered-pcfg.json")
            tampered = json.loads(json.dumps(serialized))
            tampered["pcfg_counts"][family_id][shapes["left"]] = -1
            tampered["packed_root_evidence"][0][
                "evidence_id"] = "0" * 64
            Path(tampered_state).write_text(json.dumps(tampered))
            rejected = SemanticProposalGenerator(tampered_state)
            self.assertNotIn(
                shapes["left"],
                rejected.pcfg_counts.get(family_id, {}),
            )
            self.assertTrue(all(
                count > 0
                for counts in rejected.pcfg_counts.values()
                for count in counts.values()
            ))
            self.assertEqual(len(rejected.packed_root_ids), 3)

            legacy_state = os.path.join(tmp, "legacy-pcfg.json")
            legacy = json.loads(json.dumps(serialized))
            legacy["schema"] = 14
            legacy.pop("pcfg_families", None)
            legacy.pop("pcfg_counts", None)
            legacy.pop("pcfg_observations", None)
            legacy.pop("packed_roots", None)
            legacy.pop("packed_root_evidence", None)
            Path(legacy_state).write_text(json.dumps(legacy))
            migrated = SemanticProposalGenerator(legacy_state)
            self.assertFalse(migrated.pcfg_counts)
            self.assertEqual(len(migrated.packed_root_ids), 4)
            self.assertTrue(all(
                evidence["source"] == "legacy-frontier"
                for evidence in
                migrated.packed_root_evidence.values()
            ))
            self.assertTrue(all(
                abs(migrated.packed_inside[root_id] - 1.0) < 1e-12
                for root_id in root_ids
            ))

            left_edge_id = next(
                edge_id
                for edge_id, edge in restored.packed_edges.items()
                if (
                    edge["parent_id"] in root_ids and
                    restored.packed_nodes[
                        edge["child_id"]]["symbol"] == "left"
                )
            )
            orphaned_child_id = restored.packed_edges[
                left_edge_id]["child_id"]
            alternatives_before = len(
                restored.packed_alternative_probabilities)
            restored._drop_packed_edge(left_edge_id)
            self.assertEqual(
                len(restored.packed_alternative_probabilities),
                alternatives_before - 2,
            )
            restored.save()
            after_eviction = SemanticProposalGenerator(state)
            self.assertNotIn(
                orphaned_child_id, after_eviction.packed_root_ids)

    def test_hierarchical_parent_conditioned_pcfg_and_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "is_a=data[:1]==b'A';is_x=data[1:2]==b'X';"
                "tag='tag_a' if is_a else 'tag_b';"
                "value_alts=[[4],[5]] if is_x else [[5],[4]];"
                "nodes=["
                "{'symbol':'root','state':'document','start':0,"
                "'end':len(data),'alternatives':[[1]]},"
                "{'symbol':'record','state':'body','start':0,'end':2,"
                "'alternatives':[[2,3]]},"
                "{'symbol':tag,'state':'tag','start':0,'end':1},"
                "{'symbol':'value','state':'choice','start':1,'end':2,"
                "'alternatives':value_alts},"
                "{'symbol':'x','state':'atom','start':1,'end':2},"
                "{'symbol':'y','state':'atom','start':1,'end':2}];"
                "json.dump({"
                "'schema':'symcc-parser-structural-trace-v3',"
                "'parser':'hierarchical-pcfg-fixture-v3',"
                "'accepted':len(data)==3 and data[:1] in (b'A',b'B')"
                " and data[1:2] in (b'X',b'Y'),"
                "'roots':[0],'nodes':nodes"
                "},open(sys.argv[2],'w'))"
            )
            source = os.path.join(tmp, "source")
            state = os.path.join(tmp, "hierarchical-pcfg.json")
            Path(source).write_bytes(b"AX0")
            generator = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            source_rule = generator._learn_rule(
                "literal", b"", b"AX0")
            assert source_rule is not None
            telemetry = SolverTelemetry(
                comparison_taints=((1, 9, 3, 0, 2, 1, 1),))
            spans = infer_token_spans(
                b"AX0", extract_constraint_cores(telemetry, 3))
            source_context = _grammar_context_id(b"AX0", spans[0])
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "hierarchical-proposals"),
                parser_command=(
                    sys.executable,
                    "-c",
                    parser_script,
                    "{input}",
                    "{trace}",
                ),
            )
            records = []
            for candidate in (
                b"AX0", b"AX1", b"AX2", b"AY0",
                b"BX0", b"BY0", b"BY1", b"BY2",
            ):
                proposal_id = manager.ingest({
                    "kind": "solve_complete",
                    "source_path": source,
                    "candidate": {"hex": candidate.hex()},
                    "target_branch": 9,
                    "grammar_rule_id": source_rule.rule_id,
                    "grammar_context_id": source_context,
                    "grammar_source_context_id": source_context,
                    "grammar_span": [0, 3],
                })
                self.assertIsNotNone(proposal_id)
                assert proposal_id is not None
                self.assertTrue(manager.validate(
                    proposal_id,
                    SolverTelemetry(
                        target_branch=9, target_reached=True),
                    retcode=0,
                    killed=False,
                ))
                record = manager.records[proposal_id]
                records.append(record)
                self.assertTrue(generator.observe_grammar_validation(
                    source_rule.rule_id,
                    valid=True,
                    parser_valid=True,
                    context_id=record.parser_context_id,
                    source_context_id=source_context,
                    candidate_context_id=(
                        record.grammar_candidate_context_id),
                    production_id=record.parser_production_id,
                    cfg_fragment_json=record.parser_cfg_fragment_json,
                    cfg_fragment_sha256=(
                        record.parser_cfg_fragment_sha256),
                ))

            value_family_id, _ = generator._pcfg_family(
                "hierarchical-pcfg-fixture-v3",
                "value",
                "choice",
            )
            value_productions = [
                production
                for production in generator.cfg_productions.values()
                if (
                    production["lhs"] == "value" and
                    production["state"] == "choice"
                )
            ]
            value_shapes = {
                production["rhs"][0]["symbol"]:
                    production["shape_id"]
                for production in value_productions
            }
            self.assertEqual(
                generator.pcfg_counts[value_family_id],
                {
                    value_shapes["x"]: 4,
                    value_shapes["y"]: 4,
                },
            )
            self.assertAlmostEqual(
                generator._pcfg_probability(
                    value_family_id, value_shapes["x"]),
                0.5,
            )

            record_shapes = {
                production["rhs"][0]["symbol"]:
                    production["shape_id"]
                for production in generator.cfg_productions.values()
                if (
                    production["lhs"] == "record" and
                    production["state"] == "body"
                )
            }
            context_ids = {}
            for tag, parent_shape_id in record_shapes.items():
                context_id, _ = generator._pcfg_context(
                    "hierarchical-pcfg-fixture-v3",
                    value_family_id,
                    parent_shape_id,
                    1,
                )
                context_ids[tag] = context_id
            self.assertEqual(
                generator.pcfg_context_counts[
                    context_ids["tag_a"]],
                {
                    value_shapes["x"]: 3,
                    value_shapes["y"]: 1,
                },
            )
            self.assertEqual(
                generator.pcfg_context_counts[
                    context_ids["tag_b"]],
                {
                    value_shapes["x"]: 1,
                    value_shapes["y"]: 3,
                },
            )
            self.assertAlmostEqual(
                generator._pcfg_context_probability(
                    context_ids["tag_a"], value_shapes["x"]),
                2.0 / 3.0,
            )
            self.assertAlmostEqual(
                generator._pcfg_context_probability(
                    context_ids["tag_a"], value_shapes["y"]),
                1.0 / 3.0,
            )
            self.assertAlmostEqual(
                generator._pcfg_context_probability(
                    context_ids["tag_b"], value_shapes["x"]),
                1.0 / 3.0,
            )
            self.assertAlmostEqual(
                generator._pcfg_context_probability(
                    context_ids["tag_b"], value_shapes["y"]),
                2.0 / 3.0,
            )
            contextual = list(
                generator.packed_context_alternative_probabilities.values())
            for tag, expected_x in (
                ("tag_a", 2.0 / 3.0),
                ("tag_b", 1.0 / 3.0),
            ):
                matching = [
                    alternative
                    for alternative in contextual
                    if (
                        alternative["context_id"] ==
                        context_ids[tag] and
                        alternative["shape_id"] ==
                        value_shapes["x"]
                    )
                ]
                self.assertTrue(matching)
                self.assertTrue(all(
                    abs(alternative["raw_posterior"] - expected_x) <
                    1e-12
                    for alternative in matching
                ))
            snapshot = generator.grammar_snapshot()
            self.assertEqual(snapshot["pcfg_context_observations"], 8)
            self.assertEqual(
                snapshot["pcfg_context_selected_productions"], 40)
            self.assertGreater(
                snapshot["pcfg_context_inside_states"], 0)
            self.assertGreater(
                snapshot["pcfg_context_alternatives"], 0)
            self.assertGreater(
                snapshot["pcfg_context_mean_nll_bits"], 0.0)
            self.assertEqual(
                snapshot["pcfg_prequential_receipts"], 40)
            self.assertEqual(
                snapshot["pcfg_prequential_observations"], 40)
            self.assertGreater(
                snapshot["pcfg_prequential_context_wins"], 0)
            self.assertGreaterEqual(
                snapshot["pcfg_calibrated_contexts"], 1)
            self.assertTrue(math.isfinite(
                snapshot["pcfg_prequential_global_nll_bits"]))
            self.assertTrue(math.isfinite(
                snapshot["pcfg_prequential_context_nll_bits"]))
            self.assertGreater(
                float(generator.pcfg_context_calibration_stats[
                    context_ids["tag_b"]]["weight"]),
                0.0,
            )
            calibrated_b_x = [
                alternative
                for alternative in contextual
                if (
                    alternative["context_id"] ==
                    context_ids["tag_b"] and
                    alternative["shape_id"] ==
                    value_shapes["x"]
                )
            ]
            self.assertTrue(calibrated_b_x)
            self.assertTrue(all(
                1.0 / 3.0 < alternative["posterior"] < 0.5 and
                alternative["calibration_weight"] > 0.0
                for alternative in calibrated_b_x
            ))

            counts_before = json.loads(json.dumps(
                generator.pcfg_context_counts))
            duplicate = records[-1]
            self.assertTrue(generator.observe_grammar_validation(
                source_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=duplicate.parser_context_id,
                source_context_id=source_context,
                candidate_context_id=(
                    duplicate.grammar_candidate_context_id),
                production_id=duplicate.parser_production_id,
                cfg_fragment_json=duplicate.parser_cfg_fragment_json,
                cfg_fragment_sha256=(
                    duplicate.parser_cfg_fragment_sha256),
            ))
            self.assertEqual(
                generator.pcfg_context_counts, counts_before)

            generator.save()
            serialized = json.loads(Path(state).read_text())
            self.assertEqual(serialized["schema"], 25)
            restored = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            self.assertEqual(
                restored.pcfg_context_counts,
                generator.pcfg_context_counts,
            )
            self.assertAlmostEqual(
                restored._pcfg_context_probability(
                    context_ids["tag_a"], value_shapes["x"]),
                2.0 / 3.0,
            )
            self.assertEqual(
                restored.pcfg_prequential_receipts,
                generator.pcfg_prequential_receipts,
            )
            self.assertEqual(
                restored.pcfg_context_calibration_stats,
                generator.pcfg_context_calibration_stats,
            )

            legacy_receipts = json.loads(json.dumps(serialized))
            legacy_receipts["schema"] = 18
            contexts_by_id = {
                context["context_id"]: context
                for context in legacy_receipts["pcfg_contexts"]
            }
            converted_receipts = []
            for item in legacy_receipts[
                    "pcfg_prequential_receipts"]:
                converted_receipts.append(
                    generator._pcfg_prequential_receipt(
                        fragment_sha256=
                            item["fragment_sha256"],
                        node_id=item["node_id"],
                        context=contexts_by_id[
                            item["context_id"]],
                        shape_id=item["shape_id"],
                        global_selected_before=
                            item["global_selected_before"],
                        global_total_before=
                            item["global_total_before"],
                        known_shapes=item["known_shapes"],
                        context_selected_before=
                            item["context_selected_before"],
                        context_total_before=
                            item["context_total_before"],
                    )
                )
            legacy_receipts[
                "pcfg_prequential_receipts"] = converted_receipts
            legacy_receipt_state = os.path.join(
                tmp, "legacy-v18-receipts.json")
            Path(legacy_receipt_state).write_text(
                json.dumps(legacy_receipts))
            migrated_receipts = SemanticProposalGenerator(
                legacy_receipt_state)
            self.assertEqual(
                len(migrated_receipts.pcfg_prequential_receipts),
                len(generator.pcfg_prequential_receipts),
            )
            self.assertFalse(any(
                int(stats["recency_mode"]) > 0
                for stats in
                migrated_receipts.
                pcfg_context_calibration_stats.values()
            ))

            tampered_receipt = json.loads(json.dumps(serialized))
            tampered_receipt["pcfg_prequential_receipts"][0][
                "global_total_before"] += 1
            tampered_receipt_state = os.path.join(
                tmp, "tampered-prequential-receipt.json")
            Path(tampered_receipt_state).write_text(
                json.dumps(tampered_receipt))
            rejected_receipt = SemanticProposalGenerator(
                tampered_receipt_state)
            self.assertEqual(
                len(rejected_receipt.pcfg_prequential_receipts),
                len(generator.pcfg_prequential_receipts) - 1,
            )
            self.assertEqual(
                rejected_receipt.pcfg_context_counts,
                generator.pcfg_context_counts,
            )

            inflated = json.loads(json.dumps(serialized))
            inflated["pcfg_context_counts"][
                context_ids["tag_a"]][value_shapes["x"]] += 1
            inflated_state = os.path.join(
                tmp, "inflated-context-count.json")
            Path(inflated_state).write_text(json.dumps(inflated))
            rejected_inflation = SemanticProposalGenerator(
                inflated_state)
            self.assertNotIn(
                value_shapes["x"],
                rejected_inflation.pcfg_context_counts.get(
                    context_ids["tag_a"], {}),
            )
            rejected_inflation.save()
            reconciled_again = SemanticProposalGenerator(
                inflated_state)
            self.assertEqual(
                reconciled_again.pcfg_context_counts,
                rejected_inflation.pcfg_context_counts,
            )
            self.assertEqual(
                reconciled_again.pcfg_context_observations,
                rejected_inflation.pcfg_context_observations,
            )

            invalid_context = json.loads(json.dumps(serialized))
            context_item = next(
                item
                for item in invalid_context["pcfg_contexts"]
                if item["context_id"] == context_ids["tag_b"]
            )
            context_item["slot"] = 7
            invalid_context_state = os.path.join(
                tmp, "invalid-context-id.json")
            Path(invalid_context_state).write_text(
                json.dumps(invalid_context))
            rejected_context = SemanticProposalGenerator(
                invalid_context_state)
            self.assertNotIn(
                context_ids["tag_b"], rejected_context.pcfg_contexts)

            uncalibrated = json.loads(json.dumps(serialized))
            uncalibrated["schema"] = 17
            uncalibrated.pop("pcfg_prequential_receipts", None)
            uncalibrated_state = os.path.join(
                tmp, "legacy-uncalibrated-pcfg.json")
            Path(uncalibrated_state).write_text(
                json.dumps(uncalibrated))
            migrated_uncalibrated = SemanticProposalGenerator(
                uncalibrated_state)
            self.assertEqual(
                migrated_uncalibrated.pcfg_context_counts,
                generator.pcfg_context_counts,
            )
            self.assertFalse(
                migrated_uncalibrated.pcfg_prequential_receipts)
            self.assertFalse(any(
                float(alternative["calibration_weight"]) > 0.0
                for alternative in
                migrated_uncalibrated.
                packed_context_alternative_probabilities.values()
            ))
            self.assertEqual(
                {
                    round(float(alternative["posterior"]), 12)
                    for alternative in
                    migrated_uncalibrated.
                    packed_context_alternative_probabilities.values()
                    if alternative["shape_id"] in value_shapes.values()
                },
                {0.5},
            )

            legacy = json.loads(json.dumps(serialized))
            legacy["schema"] = 16
            legacy.pop("pcfg_contexts", None)
            legacy.pop("pcfg_context_counts", None)
            legacy.pop("pcfg_context_observations", None)
            legacy_state = os.path.join(
                tmp, "legacy-unconditional-pcfg.json")
            Path(legacy_state).write_text(json.dumps(legacy))
            migrated = SemanticProposalGenerator(legacy_state)
            self.assertFalse(migrated.pcfg_contexts)
            self.assertFalse(migrated.pcfg_context_counts)
            self.assertTrue(
                migrated.packed_context_alternative_probabilities)
            migrated_value_posteriors = {
                round(float(alternative["posterior"]), 12)
                for alternative in
                migrated.packed_context_alternative_probabilities.values()
                if alternative["shape_id"] in value_shapes.values()
            }
            self.assertEqual(migrated_value_posteriors, {0.5})

    def test_grandparent_circuit_is_calibrated_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "is_a=data[:1]==b'A';is_x=data[1:2]==b'X';"
                "tag='tag_a' if is_a else 'tag_b';"
                "value_alts=[[4],[5]] if is_x else [[5],[4]];"
                "nodes=["
                "{'symbol':'root','state':'document','start':0,"
                "'end':len(data),'alternatives':[[3,1]]},"
                "{'symbol':'record','state':'body','start':1,'end':2,"
                "'alternatives':[[2]]},"
                "{'symbol':'value','state':'choice','start':1,'end':2,"
                "'alternatives':value_alts},"
                "{'symbol':tag,'state':'tag','start':0,'end':1},"
                "{'symbol':'x','state':'atom','start':1,'end':2},"
                "{'symbol':'y','state':'atom','start':1,'end':2}];"
                "json.dump({"
                "'schema':'symcc-parser-structural-trace-v3',"
                "'parser':'grandparent-circuit-fixture-v3',"
                "'accepted':len(data)==3 and data[:1] in (b'A',b'B')"
                " and data[1:2] in (b'X',b'Y'),"
                "'roots':[0],'nodes':nodes"
                "},open(sys.argv[2],'w'))"
            )
            source = os.path.join(tmp, "source")
            state = os.path.join(tmp, "circuit-state.json")
            Path(source).write_bytes(b"AX0")
            generator = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            source_rule = generator._learn_rule(
                "literal", b"", b"AX0")
            assert source_rule is not None
            telemetry = SolverTelemetry(
                comparison_taints=((1, 9, 3, 0, 2, 1, 1),))
            spans = infer_token_spans(
                b"AX0", extract_constraint_cores(telemetry, 3))
            source_context = _grammar_context_id(b"AX0", spans[0])
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "circuit-proposals"),
                parser_command=(
                    sys.executable,
                    "-c",
                    parser_script,
                    "{input}",
                    "{trace}",
                ),
            )
            candidates = tuple(
                candidate
                for suffix in range(16)
                for candidate in (
                    (
                        b"AX" + bytes([suffix])
                        if suffix < 14 else
                        b"AY" + bytes([suffix])
                    ),
                    (
                        b"BY" + bytes([suffix])
                        if suffix < 14 else
                        b"BX" + bytes([suffix])
                    ),
                )
            )
            for candidate in candidates:
                proposal_id = manager.ingest({
                    "kind": "solve_complete",
                    "source_path": source,
                    "candidate": {"hex": candidate.hex()},
                    "target_branch": 9,
                    "grammar_rule_id": source_rule.rule_id,
                    "grammar_context_id": source_context,
                    "grammar_source_context_id": source_context,
                    "grammar_span": [0, 3],
                })
                self.assertIsNotNone(proposal_id)
                assert proposal_id is not None
                self.assertTrue(manager.validate(
                    proposal_id,
                    SolverTelemetry(
                        target_branch=9, target_reached=True),
                    retcode=0,
                    killed=False,
                ))
                record = manager.records[proposal_id]
                self.assertTrue(generator.observe_grammar_validation(
                    source_rule.rule_id,
                    valid=True,
                    parser_valid=True,
                    context_id=record.parser_context_id,
                    source_context_id=source_context,
                    candidate_context_id=(
                        record.grammar_candidate_context_id),
                    production_id=record.parser_production_id,
                    cfg_fragment_json=record.parser_cfg_fragment_json,
                    cfg_fragment_sha256=(
                        record.parser_cfg_fragment_sha256),
                ))

            parser = "grandparent-circuit-fixture-v3"
            value_family_id, _ = generator._pcfg_family(
                parser, "value", "choice")
            value_shapes = {
                production["rhs"][0]["symbol"]:
                    production["shape_id"]
                for production in generator.cfg_productions.values()
                if (
                    production["lhs"] == "value" and
                    production["state"] == "choice"
                )
            }
            record_shape = next(
                production["shape_id"]
                for production in generator.cfg_productions.values()
                if (
                    production["lhs"] == "record" and
                    production["state"] == "body"
                )
            )
            root_shapes = {
                production["rhs"][0]["symbol"]:
                    production["shape_id"]
                for production in generator.cfg_productions.values()
                if (
                    production["lhs"] == "root" and
                    production["state"] == "document"
                )
            }
            parent_context_id, _ = generator._pcfg_context(
                parser,
                value_family_id,
                record_shape,
                0,
            )
            self.assertEqual(
                generator.pcfg_context_counts[parent_context_id],
                {
                    value_shapes["x"]: 16,
                    value_shapes["y"]: 16,
                },
            )
            circuit_ids = {}
            for tag, ancestor_shape in root_shapes.items():
                circuit_id, circuit_context = (
                    generator._pcfg_circuit_context(
                        parser,
                        value_family_id,
                        record_shape,
                        0,
                        ancestor_shape,
                        1,
                    )
                )
                circuit_ids[tag] = circuit_id
                expected_counts = (
                    {
                        value_shapes["x"]: 14,
                        value_shapes["y"]: 2,
                    }
                    if tag == "tag_a" else
                    {
                        value_shapes["x"]: 2,
                        value_shapes["y"]: 14,
                    }
                )
                self.assertEqual(
                    generator.pcfg_circuit_counts[circuit_id],
                    expected_counts,
                )
                _, parent_context = generator._pcfg_context(
                    parser,
                    value_family_id,
                    record_shape,
                    0,
                )
                expected_x = (
                    5.0 / 6.0 if tag == "tag_a" else 1.0 / 6.0
                )
                self.assertAlmostEqual(
                    generator._pcfg_circuit_probability(
                        circuit_id,
                        value_shapes["x"],
                        context=circuit_context,
                        parent_context=parent_context,
                    ),
                    expected_x,
                )
                self.assertGreater(
                    float(generator.pcfg_circuit_calibration_stats[
                        circuit_id]["weight"]),
                    0.0,
                )

            artifacts = list(
                generator.
                packed_context_alternative_probabilities.values()
            )
            for tag, direction in (("tag_a", 1), ("tag_b", -1)):
                matches = [
                    artifact
                    for artifact in artifacts
                    if (
                        artifact["circuit_context_id"] ==
                        circuit_ids[tag] and
                        artifact["shape_id"] == value_shapes["x"]
                    )
                ]
                self.assertTrue(matches)
                self.assertTrue(all(
                    (
                        float(artifact["posterior"]) - 0.5
                    ) * direction > 0.0 and
                    float(artifact[
                        "circuit_calibration_weight"]) > 0.0
                    for artifact in matches
                ))
                for state_id in {
                    artifact["context_state_id"]
                    for artifact in matches
                }:
                    self.assertAlmostEqual(
                        sum(
                            float(artifact["posterior"])
                            for artifact in artifacts
                            if artifact["context_state_id"] == state_id
                        ),
                        1.0,
                    )
            snapshot = generator.grammar_snapshot()
            self.assertGreaterEqual(
                snapshot["pcfg_circuit_contexts"], 2)
            self.assertEqual(
                snapshot["pcfg_circuit_observations"], 32)
            self.assertGreaterEqual(
                snapshot["pcfg_circuit_calibrated_contexts"], 2)

            generator.save()
            serialized = json.loads(Path(state).read_text())
            self.assertEqual(serialized["schema"], 25)
            restored = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            self.assertEqual(
                restored.pcfg_circuit_counts,
                generator.pcfg_circuit_counts,
            )
            self.assertEqual(
                restored.pcfg_circuit_receipts,
                generator.pcfg_circuit_receipts,
            )
            self.assertEqual(
                restored.pcfg_circuit_calibration_stats,
                generator.pcfg_circuit_calibration_stats,
            )

            tampered_receipt = json.loads(json.dumps(serialized))
            tampered_receipt["pcfg_circuit_receipts"][0][
                "parent_total_before"] += 1
            tampered_receipt_state = os.path.join(
                tmp, "tampered-circuit-receipt.json")
            Path(tampered_receipt_state).write_text(
                json.dumps(tampered_receipt))
            rejected_receipt = SemanticProposalGenerator(
                tampered_receipt_state)
            self.assertEqual(
                len(rejected_receipt.pcfg_circuit_receipts),
                len(generator.pcfg_circuit_receipts) - 1,
            )
            self.assertEqual(
                rejected_receipt.pcfg_circuit_counts,
                generator.pcfg_circuit_counts,
            )

            tampered = json.loads(json.dumps(serialized))
            tampered["pcfg_circuit_counts"][
                circuit_ids["tag_a"]][value_shapes["x"]] += 1
            tampered_state = os.path.join(
                tmp, "tampered-circuit-count.json")
            Path(tampered_state).write_text(json.dumps(tampered))
            rejected = SemanticProposalGenerator(tampered_state)
            self.assertNotIn(
                value_shapes["x"],
                rejected.pcfg_circuit_counts.get(
                    circuit_ids["tag_a"], {}),
            )
            self.assertIn(
                circuit_ids["tag_b"],
                rejected.pcfg_circuit_counts,
            )

            legacy = json.loads(json.dumps(serialized))
            legacy["schema"] = 20
            legacy_state = os.path.join(
                tmp, "legacy-v20-circuit.json")
            Path(legacy_state).write_text(json.dumps(legacy))
            migrated = SemanticProposalGenerator(legacy_state)
            self.assertFalse(migrated.pcfg_circuit_contexts)
            self.assertFalse(migrated.pcfg_circuit_counts)
            self.assertEqual(
                migrated.pcfg_context_counts,
                generator.pcfg_context_counts,
            )

    def test_ordered_sibling_factor_is_exact_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "is_a=data[:1]==b'A';is_x=data[1:2]==b'X';"
                "tag_alts=[[4],[5]] if is_a else [[5],[4]];"
                "value_alts=[[6],[7]] if is_x else [[7],[6]];"
                "nodes=["
                "{'symbol':'root','state':'document','start':0,"
                "'end':len(data),'alternatives':[[1]]},"
                "{'symbol':'record','state':'body','start':0,'end':2,"
                "'alternatives':[[2,3]]},"
                "{'symbol':'tag','state':'choice','start':0,'end':1,"
                "'alternatives':tag_alts},"
                "{'symbol':'value','state':'choice','start':1,'end':2,"
                "'alternatives':value_alts},"
                "{'symbol':'tag_a','state':'atom','start':0,'end':1},"
                "{'symbol':'tag_b','state':'atom','start':0,'end':1},"
                "{'symbol':'x','state':'atom','start':1,'end':2},"
                "{'symbol':'y','state':'atom','start':1,'end':2}];"
                "json.dump({"
                "'schema':'symcc-parser-structural-trace-v3',"
                "'parser':'ordered-sibling-fixture-v3',"
                "'accepted':len(data)==3 and data[:1] in (b'A',b'B')"
                " and data[1:2] in (b'X',b'Y'),"
                "'roots':[0],'nodes':nodes"
                "},open(sys.argv[2],'w'))"
            )
            source = os.path.join(tmp, "source")
            state = os.path.join(tmp, "sibling-state.json")
            Path(source).write_bytes(b"AX0")
            generator = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            source_rule = generator._learn_rule(
                "literal", b"", b"AX0")
            assert source_rule is not None
            telemetry = SolverTelemetry(
                comparison_taints=((1, 9, 3, 0, 2, 1, 1),))
            spans = infer_token_spans(
                b"AX0", extract_constraint_cores(telemetry, 3))
            source_context = _grammar_context_id(b"AX0", spans[0])
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "sibling-proposals"),
                parser_command=(
                    sys.executable,
                    "-c",
                    parser_script,
                    "{input}",
                    "{trace}",
                ),
            )
            candidates = tuple(
                candidate
                for suffix in range(16)
                for candidate in (
                    (
                        b"AX" + bytes([suffix])
                        if suffix < 14 else
                        b"AY" + bytes([suffix])
                    ),
                    (
                        b"BY" + bytes([suffix])
                        if suffix < 14 else
                        b"BX" + bytes([suffix])
                    ),
                )
            )
            for candidate in candidates:
                proposal_id = manager.ingest({
                    "kind": "solve_complete",
                    "source_path": source,
                    "candidate": {"hex": candidate.hex()},
                    "target_branch": 9,
                    "grammar_rule_id": source_rule.rule_id,
                    "grammar_context_id": source_context,
                    "grammar_source_context_id": source_context,
                    "grammar_span": [0, 3],
                })
                self.assertIsNotNone(proposal_id)
                assert proposal_id is not None
                self.assertTrue(manager.validate(
                    proposal_id,
                    SolverTelemetry(
                        target_branch=9, target_reached=True),
                    retcode=0,
                    killed=False,
                ))
                record = manager.records[proposal_id]
                self.assertTrue(generator.observe_grammar_validation(
                    source_rule.rule_id,
                    valid=True,
                    parser_valid=True,
                    context_id=record.parser_context_id,
                    source_context_id=source_context,
                    candidate_context_id=(
                        record.grammar_candidate_context_id),
                    production_id=record.parser_production_id,
                    cfg_fragment_json=record.parser_cfg_fragment_json,
                    cfg_fragment_sha256=(
                        record.parser_cfg_fragment_sha256),
                ))

            parser = "ordered-sibling-fixture-v3"
            value_family_id, _ = generator._pcfg_family(
                parser, "value", "choice")
            value_shapes = {
                production["rhs"][0]["symbol"]:
                    production["shape_id"]
                for production in generator.cfg_productions.values()
                if (
                    production["lhs"] == "value" and
                    production["state"] == "choice"
                )
            }
            tag_shapes = {
                production["rhs"][0]["symbol"]:
                    production["shape_id"]
                for production in generator.cfg_productions.values()
                if (
                    production["lhs"] == "tag" and
                    production["state"] == "choice"
                )
            }
            record_shape = next(
                production["shape_id"]
                for production in generator.cfg_productions.values()
                if (
                    production["lhs"] == "record" and
                    production["state"] == "body"
                )
            )
            root_shape = next(
                production["shape_id"]
                for production in generator.cfg_productions.values()
                if (
                    production["lhs"] == "root" and
                    production["state"] == "document"
                )
            )
            parent_context_id, parent_context = (
                generator._pcfg_context(
                    parser,
                    value_family_id,
                    record_shape,
                    1,
                )
            )
            circuit_context_id, circuit_context = (
                generator._pcfg_circuit_context(
                    parser,
                    value_family_id,
                    record_shape,
                    1,
                    root_shape,
                    0,
                )
            )
            balanced = {
                value_shapes["x"]: 16,
                value_shapes["y"]: 16,
            }
            self.assertEqual(
                generator.pcfg_context_counts[parent_context_id],
                balanced,
            )
            self.assertEqual(
                generator.pcfg_circuit_counts[circuit_context_id],
                balanced,
            )

            sibling_ids = {}
            for tag, left_shape_id in tag_shapes.items():
                sibling_context_id, sibling_context = (
                    generator._pcfg_sibling_context(
                        parser,
                        value_family_id,
                        record_shape,
                        1,
                        left_shape_id,
                    )
                )
                sibling_ids[tag] = sibling_context_id
                expected_counts = (
                    {
                        value_shapes["x"]: 14,
                        value_shapes["y"]: 2,
                    }
                    if tag == "tag_a" else
                    {
                        value_shapes["x"]: 2,
                        value_shapes["y"]: 14,
                    }
                )
                self.assertEqual(
                    generator.pcfg_sibling_counts[
                        sibling_context_id],
                    expected_counts,
                )
                self.assertAlmostEqual(
                    generator._pcfg_sibling_probability(
                        sibling_context_id,
                        value_shapes["x"],
                        context=sibling_context,
                        parent_context=parent_context,
                        circuit_context=circuit_context,
                    ),
                    5.0 / 6.0 if tag == "tag_a" else 1.0 / 6.0,
                )
                self.assertGreater(
                    float(generator.pcfg_sibling_calibration_stats[
                        sibling_context_id]["weight"]),
                    0.0,
                )

            artifacts = list(
                generator.
                packed_context_alternative_probabilities.values()
            )
            for tag, direction in (("tag_a", 1), ("tag_b", -1)):
                matches = [
                    artifact
                    for artifact in artifacts
                    if (
                        artifact["sibling_context_id"] ==
                        sibling_ids[tag] and
                        artifact["shape_id"] == value_shapes["x"]
                    )
                ]
                self.assertTrue(matches)
                self.assertTrue(all(
                    (
                        float(artifact["posterior"]) - 0.5
                    ) * direction > 0.0 and
                    float(artifact[
                        "sibling_calibration_weight"]) > 0.0
                    for artifact in matches
                ))
                for state_id in {
                    artifact["context_state_id"]
                    for artifact in matches
                }:
                    self.assertAlmostEqual(sum(
                        float(candidate["posterior"])
                        for candidate in artifacts
                        if candidate["context_state_id"] == state_id
                    ), 1.0)

            record_artifacts = [
                artifact
                for artifact in artifacts
                if artifact["shape_id"] == record_shape
            ]
            self.assertTrue(record_artifacts)
            artifacts_by_id = {
                artifact["artifact_id"]: artifact
                for artifact in artifacts
            }
            expected_child_external = {}
            for artifact in record_artifacts:
                steps = artifact["child_factor_steps"]
                self.assertEqual(len(steps), 2)
                self.assertEqual(
                    {
                        transition["previous_sibling_shape_id"]
                        for transition in steps[1]["transitions"]
                    },
                    set(tag_shapes.values()),
                )
                frontier = {"": 1.0}
                forward = [frontier]
                for step in steps:
                    next_frontier = {}
                    for transition in step["transitions"]:
                        previous = transition[
                            "previous_sibling_shape_id"]
                        selected = transition["selected_shape_id"]
                        next_frontier[selected] = (
                            next_frontier.get(selected, 0.0) +
                            frontier.get(previous, 0.0) *
                            float(transition["inside_mass"])
                        )
                    frontier = next_frontier
                    forward.append(frontier)
                self.assertAlmostEqual(
                    float(artifact["inside_mass"]),
                    float(artifact["posterior"]) *
                    sum(frontier.values()),
                )
                parent_external = (
                    float(artifact["outside_mass"]) /
                    float(artifact["inside_mass"])
                )
                for transition in steps[1]["transitions"]:
                    child_artifact_id = transition[
                        "child_artifact_id"]
                    expected_child_external[child_artifact_id] = (
                        expected_child_external.get(
                            child_artifact_id, 0.0) +
                        parent_external *
                        float(artifact["posterior"]) *
                        forward[1][
                            transition[
                                "previous_sibling_shape_id"]]
                    )
            for child_artifact_id, expected in (
                    expected_child_external.items()):
                child = artifacts_by_id[child_artifact_id]
                child_external = (
                    float(child["outside_mass"]) /
                    float(child["inside_mass"])
                )
                self.assertAlmostEqual(
                    child_external, min(1.0, expected))
            self.assertTrue(all(
                0.0 <= float(artifact["outside_mass"]) <= 1.0
                for artifact in artifacts
            ))
            snapshot = generator.grammar_snapshot()
            self.assertEqual(
                snapshot["pcfg_sibling_observations"], 32)
            self.assertGreaterEqual(
                snapshot["pcfg_sibling_calibrated_contexts"], 2)
            self.assertGreater(
                snapshot["pcfg_sibling_factor_transitions"], 0)

            generator.save()
            serialized = json.loads(Path(state).read_text())
            self.assertEqual(serialized["schema"], 25)
            restored = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            self.assertEqual(
                restored.pcfg_sibling_counts,
                generator.pcfg_sibling_counts,
            )
            self.assertEqual(
                restored.pcfg_sibling_receipts,
                generator.pcfg_sibling_receipts,
            )
            self.assertEqual(
                restored.pcfg_sibling_calibration_stats,
                generator.pcfg_sibling_calibration_stats,
            )

            tampered_receipt = json.loads(json.dumps(serialized))
            tampered_receipt["pcfg_sibling_receipts"][0][
                "sibling_total_before"] += 1
            tampered_receipt_state = os.path.join(
                tmp, "tampered-sibling-receipt.json")
            Path(tampered_receipt_state).write_text(
                json.dumps(tampered_receipt))
            rejected_receipt = SemanticProposalGenerator(
                tampered_receipt_state)
            self.assertEqual(
                len(rejected_receipt.pcfg_sibling_receipts),
                len(generator.pcfg_sibling_receipts) - 1,
            )
            self.assertEqual(
                rejected_receipt.pcfg_sibling_counts,
                generator.pcfg_sibling_counts,
            )

            tampered = json.loads(json.dumps(serialized))
            tampered["pcfg_sibling_counts"][
                sibling_ids["tag_a"]][value_shapes["x"]] += 1
            tampered_state = os.path.join(
                tmp, "tampered-sibling-count.json")
            Path(tampered_state).write_text(json.dumps(tampered))
            rejected = SemanticProposalGenerator(tampered_state)
            self.assertNotIn(
                value_shapes["x"],
                rejected.pcfg_sibling_counts.get(
                    sibling_ids["tag_a"], {}),
            )
            self.assertIn(
                sibling_ids["tag_b"],
                rejected.pcfg_sibling_counts,
            )

            legacy = json.loads(json.dumps(serialized))
            legacy["schema"] = 21
            legacy_state = os.path.join(
                tmp, "legacy-v21-sibling.json")
            Path(legacy_state).write_text(json.dumps(legacy))
            migrated = SemanticProposalGenerator(legacy_state)
            self.assertFalse(migrated.pcfg_sibling_contexts)
            self.assertFalse(migrated.pcfg_sibling_counts)
            self.assertEqual(
                migrated.pcfg_circuit_counts,
                generator.pcfg_circuit_counts,
            )

            legacy_v22 = json.loads(json.dumps(serialized))
            legacy_v22["schema"] = 22
            legacy_v22.pop("pcfg_anytime_certificates", None)
            legacy_v22.pop(
                "pcfg_circuit_anytime_certificates", None)
            legacy_v22.pop(
                "pcfg_sibling_anytime_certificates", None)
            legacy_v22_state = os.path.join(
                tmp, "legacy-v22-anytime.json")
            Path(legacy_v22_state).write_text(
                json.dumps(legacy_v22))
            migrated_v22 = SemanticProposalGenerator(
                legacy_v22_state)
            self.assertEqual(migrated_v22.SCHEMA, 25)
            self.assertEqual(
                migrated_v22.pcfg_sibling_counts,
                generator.pcfg_sibling_counts,
            )
            self.assertEqual(
                migrated_v22.pcfg_sibling_receipts,
                generator.pcfg_sibling_receipts,
            )

    def test_second_order_sibling_history_is_exact_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,pathlib,sys;"
                "d=pathlib.Path(sys.argv[1]).read_bytes();"
                "a=d[:1]==b'A';m=d[1:2]==b'M';x=d[2:3]==b'X';"
                "aa=[[5],[6]] if a else [[6],[5]];"
                "ba=[[7],[8]] if m else [[8],[7]];"
                "va=[[9],[10]] if x else [[10],[9]];"
                "n=["
                "{'symbol':'root','state':'document','start':0,"
                "'end':len(d),'alternatives':[[1]]},"
                "{'symbol':'record','state':'body','start':0,'end':3,"
                "'alternatives':[[2,3,4]]},"
                "{'symbol':'anchor','state':'choice','start':0,'end':1,"
                "'alternatives':aa},"
                "{'symbol':'bridge','state':'choice','start':1,'end':2,"
                "'alternatives':ba},"
                "{'symbol':'value','state':'choice','start':2,'end':3,"
                "'alternatives':va},"
                "{'symbol':'anchor_a','state':'atom','start':0,'end':1},"
                "{'symbol':'anchor_b','state':'atom','start':0,'end':1},"
                "{'symbol':'bridge_m','state':'atom','start':1,'end':2},"
                "{'symbol':'bridge_n','state':'atom','start':1,'end':2},"
                "{'symbol':'x','state':'atom','start':2,'end':3},"
                "{'symbol':'y','state':'atom','start':2,'end':3}];"
                "json.dump({"
                "'schema':'symcc-parser-structural-trace-v3',"
                "'parser':'history-factor-fixture-v3',"
                "'accepted':len(d)==4 and d[:1] in (b'A',b'B') "
                "and d[1:2] in (b'M',b'N') "
                "and d[2:3] in (b'X',b'Y'),"
                "'roots':[0],'nodes':n},open(sys.argv[2],'w'))"
            )
            source = os.path.join(tmp, "source")
            state = os.path.join(tmp, "history-state.json")
            Path(source).write_bytes(b"AMX0")
            generator = SemanticProposalGenerator(
                state, max_proposals_per_observation=128)
            source_rule = generator._learn_rule(
                "literal", b"", b"AMX0")
            assert source_rule is not None
            telemetry = SolverTelemetry(
                comparison_taints=((1, 10, 4, 0, 3, 1, 1),))
            spans = infer_token_spans(
                b"AMX0", extract_constraint_cores(telemetry, 4))
            source_context = _grammar_context_id(
                b"AMX0", spans[0])
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "history-proposals"),
                parser_command=(
                    sys.executable,
                    "-c",
                    parser_script,
                    "{input}",
                    "{trace}",
                ),
            )
            candidates = tuple(
                candidate
                for suffix in range(16)
                for candidate in (
                    (
                        b"AMX" + bytes([suffix])
                        if suffix < 14 else
                        b"AMY" + bytes([suffix])
                    ),
                    (
                        b"BMY" + bytes([suffix])
                        if suffix < 14 else
                        b"BMX" + bytes([suffix])
                    ),
                    (
                        b"ANY" + bytes([suffix])
                        if suffix < 14 else
                        b"ANX" + bytes([suffix])
                    ),
                    (
                        b"BNX" + bytes([suffix])
                        if suffix < 14 else
                        b"BNY" + bytes([suffix])
                    ),
                )
            )
            for candidate in candidates:
                proposal_id = manager.ingest({
                    "kind": "solve_complete",
                    "source_path": source,
                    "candidate": {"hex": candidate.hex()},
                    "target_branch": 10,
                    "grammar_rule_id": source_rule.rule_id,
                    "grammar_context_id": source_context,
                    "grammar_source_context_id": source_context,
                    "grammar_span": [0, 4],
                })
                self.assertIsNotNone(proposal_id)
                assert proposal_id is not None
                self.assertTrue(manager.validate(
                    proposal_id,
                    SolverTelemetry(
                        target_branch=10, target_reached=True),
                    retcode=0,
                    killed=False,
                ))
                record = manager.records[proposal_id]
                self.assertTrue(generator.observe_grammar_validation(
                    source_rule.rule_id,
                    valid=True,
                    parser_valid=True,
                    context_id=record.parser_context_id,
                    source_context_id=source_context,
                    candidate_context_id=(
                        record.grammar_candidate_context_id),
                    production_id=record.parser_production_id,
                    cfg_fragment_json=record.parser_cfg_fragment_json,
                    cfg_fragment_sha256=(
                        record.parser_cfg_fragment_sha256),
                ))

            parser = "history-factor-fixture-v3"

            def shapes(lhs: str) -> dict[str, str]:
                return {
                    production["rhs"][0]["symbol"]:
                        production["shape_id"]
                    for production in generator.cfg_productions.values()
                    if (
                        production["lhs"] == lhs and
                        production["state"] == "choice"
                    )
                }

            value_shapes = shapes("value")
            anchor_shapes = shapes("anchor")
            bridge_shapes = shapes("bridge")
            value_family_id, _ = generator._pcfg_family(
                parser, "value", "choice")
            record_shape = next(
                production["shape_id"]
                for production in generator.cfg_productions.values()
                if production["lhs"] == "record"
            )
            root_shape = next(
                production["shape_id"]
                for production in generator.cfg_productions.values()
                if production["lhs"] == "root"
            )
            parent_context_id, parent_context = (
                generator._pcfg_context(
                    parser, value_family_id, record_shape, 2)
            )
            circuit_context_id, circuit_context = (
                generator._pcfg_circuit_context(
                    parser,
                    value_family_id,
                    record_shape,
                    2,
                    root_shape,
                    0,
                )
            )
            balanced = {
                value_shapes["x"]: 32,
                value_shapes["y"]: 32,
            }
            self.assertEqual(
                generator.pcfg_counts[value_family_id], balanced)
            self.assertEqual(
                generator.pcfg_context_counts[parent_context_id],
                balanced,
            )
            self.assertEqual(
                generator.pcfg_circuit_counts[circuit_context_id],
                balanced,
            )
            sibling_contexts = {}
            sibling_balanced = {
                value_shapes["x"]: 16,
                value_shapes["y"]: 16,
            }
            for bridge, bridge_shape_id in bridge_shapes.items():
                sibling_context_id, sibling_context = (
                    generator._pcfg_sibling_context(
                        parser,
                        value_family_id,
                        record_shape,
                        2,
                        bridge_shape_id,
                    )
                )
                sibling_contexts[bridge] = (
                    sibling_context_id, sibling_context)
                self.assertEqual(
                    generator.pcfg_sibling_counts[
                        sibling_context_id],
                    sibling_balanced,
                )
                self.assertAlmostEqual(
                    generator._pcfg_sibling_probability(
                        sibling_context_id,
                        value_shapes["x"],
                        context=sibling_context,
                        parent_context=parent_context,
                        circuit_context=circuit_context,
                    ),
                    0.5,
                )

            expected_x = {
                ("anchor_a", "bridge_m"): 14,
                ("anchor_b", "bridge_m"): 2,
                ("anchor_a", "bridge_n"): 2,
                ("anchor_b", "bridge_n"): 14,
            }
            history_ids = {}
            for (anchor, bridge), x_count in expected_x.items():
                sibling_context_id, sibling_context = (
                    sibling_contexts[bridge])
                history_context_id, history_context = (
                    generator._pcfg_history_context(
                        parser,
                        value_family_id,
                        record_shape,
                        2,
                        anchor_shapes[anchor],
                        bridge_shapes[bridge],
                    )
                )
                history_ids[(anchor, bridge)] = history_context_id
                self.assertEqual(
                    generator.pcfg_history_counts[
                        history_context_id],
                    {
                        value_shapes["x"]: x_count,
                        value_shapes["y"]: 16 - x_count,
                    },
                )
                self.assertAlmostEqual(
                    generator._pcfg_history_probability(
                        history_context_id,
                        sibling_context_id,
                        value_shapes["x"],
                        context=history_context,
                        sibling_context=sibling_context,
                        parent_context=parent_context,
                        circuit_context=circuit_context,
                    ),
                    5.0 / 6.0 if x_count == 14 else 1.0 / 6.0,
                )
                self.assertGreater(
                    float(generator.pcfg_history_calibration_stats[
                        history_context_id]["weight"]),
                    0.0,
                )

            artifacts = list(
                generator.
                packed_context_alternative_probabilities.values()
            )
            for key, history_context_id in history_ids.items():
                direction = 1 if expected_x[key] == 14 else -1
                matches = [
                    artifact
                    for artifact in artifacts
                    if (
                        artifact["history_context_id"] ==
                        history_context_id and
                        artifact["shape_id"] == value_shapes["x"]
                    )
                ]
                self.assertTrue(matches)
                self.assertTrue(all(
                    (
                        float(artifact["posterior"]) - 0.5
                    ) * direction > 0.0 and
                    float(artifact[
                        "history_calibration_weight"]) > 0.0
                    for artifact in matches
                ))
                for state_id in {
                    artifact["context_state_id"]
                    for artifact in matches
                }:
                    self.assertAlmostEqual(sum(
                        float(candidate["posterior"])
                        for candidate in artifacts
                        if candidate["context_state_id"] == state_id
                    ), 1.0)

            record_artifacts = [
                artifact
                for artifact in artifacts
                if artifact["shape_id"] == record_shape
            ]
            self.assertTrue(record_artifacts)
            for artifact in record_artifacts:
                steps = artifact["child_factor_steps"]
                self.assertEqual(len(steps), 3)
                histories = {
                    tuple(transition[
                        "previous_sibling_shape_ids"])
                    for transition in steps[2]["transitions"]
                }
                self.assertEqual(
                    histories,
                    {
                        (anchor_shape, bridge_shape)
                        for anchor_shape in anchor_shapes.values()
                        for bridge_shape in bridge_shapes.values()
                    },
                )
                frontier = {(): 1.0}
                for step in steps:
                    next_frontier = {}
                    for transition in step["transitions"]:
                        previous = tuple(transition[
                            "previous_sibling_shape_ids"])
                        selected = tuple(transition[
                            "next_sibling_shape_ids"])
                        next_frontier[selected] = (
                            next_frontier.get(selected, 0.0) +
                            frontier.get(previous, 0.0) *
                            float(transition["inside_mass"])
                        )
                    frontier = next_frontier
                self.assertAlmostEqual(
                    float(artifact["inside_mass"]),
                    float(artifact["posterior"]) *
                    sum(frontier.values()),
                )
            self.assertTrue(all(
                0.0 <= float(artifact["outside_mass"]) <= 1.0
                for artifact in artifacts
            ))
            snapshot = generator.grammar_snapshot()
            self.assertEqual(
                snapshot["pcfg_history_observations"], 64)
            self.assertGreaterEqual(
                snapshot["pcfg_history_calibrated_contexts"], 4)
            self.assertGreater(
                snapshot["pcfg_history_factor_states"], 0)
            self.assertEqual(
                snapshot["pcfg_anytime_allocation_ledger_valid"], 1)
            self.assertGreater(
                snapshot["pcfg_anytime_context_allocations"], 0)
            self.assertTrue(all(
                generator._pcfg_anytime_allocation_key(
                    "history", history_context_id
                ) in generator.pcfg_anytime_context_allocations
                for history_context_id in history_ids.values()
            ))

            generator.save()
            serialized = json.loads(Path(state).read_text())
            self.assertEqual(serialized["schema"], 25)
            restored = SemanticProposalGenerator(
                state, max_proposals_per_observation=128)
            self.assertEqual(
                restored.pcfg_history_counts,
                generator.pcfg_history_counts,
            )
            self.assertEqual(
                restored.pcfg_history_receipts,
                generator.pcfg_history_receipts,
            )
            self.assertEqual(
                restored.pcfg_history_calibration_stats,
                generator.pcfg_history_calibration_stats,
            )
            self.assertEqual(
                restored.pcfg_anytime_context_allocations,
                generator.pcfg_anytime_context_allocations,
            )
            self.assertTrue(
                restored.pcfg_anytime_allocation_ledger_valid)

            tampered_allocation = json.loads(
                json.dumps(serialized))
            tampered_allocation[
                "pcfg_anytime_context_allocations"
            ][0]["ordinal"] += 1
            tampered_allocation_state = os.path.join(
                tmp, "tampered-anytime-allocation.json")
            Path(tampered_allocation_state).write_text(
                json.dumps(tampered_allocation))
            rejected_allocation = SemanticProposalGenerator(
                tampered_allocation_state)
            self.assertFalse(
                rejected_allocation.
                pcfg_anytime_allocation_ledger_valid)
            self.assertFalse(
                rejected_allocation.
                pcfg_global_anytime_certificates)

            tampered_receipt = json.loads(json.dumps(serialized))
            tampered_receipt["pcfg_history_receipts"][0][
                "history_total_before"] += 1
            tampered_receipt_state = os.path.join(
                tmp, "tampered-history-receipt.json")
            Path(tampered_receipt_state).write_text(
                json.dumps(tampered_receipt))
            rejected_receipt = SemanticProposalGenerator(
                tampered_receipt_state)
            self.assertEqual(
                len(rejected_receipt.pcfg_history_receipts),
                len(generator.pcfg_history_receipts) - 1,
            )

            tampered = json.loads(json.dumps(serialized))
            first_history_id = next(iter(history_ids.values()))
            tampered["pcfg_history_counts"][
                first_history_id][value_shapes["x"]] += 1
            tampered_state = os.path.join(
                tmp, "tampered-history-count.json")
            Path(tampered_state).write_text(json.dumps(tampered))
            rejected = SemanticProposalGenerator(tampered_state)
            self.assertNotIn(
                value_shapes["x"],
                rejected.pcfg_history_counts.get(
                    first_history_id, {}),
            )

            legacy_v24 = json.loads(json.dumps(serialized))
            legacy_v24["schema"] = 24
            legacy_v24.pop(
                "pcfg_anytime_context_allocations", None)
            legacy_v24.pop(
                "pcfg_global_anytime_certificates", None)
            legacy_v24_state = os.path.join(
                tmp, "legacy-v24-anytime.json")
            Path(legacy_v24_state).write_text(
                json.dumps(legacy_v24))
            migrated_v24 = SemanticProposalGenerator(
                legacy_v24_state)
            self.assertEqual(
                migrated_v24.pcfg_history_counts,
                generator.pcfg_history_counts,
            )
            self.assertTrue(
                migrated_v24.
                pcfg_anytime_allocation_ledger_valid)
            self.assertGreater(
                len(migrated_v24.
                    pcfg_anytime_context_allocations),
                0,
            )

            legacy = json.loads(json.dumps(serialized))
            legacy["schema"] = 23
            legacy_state = os.path.join(
                tmp, "legacy-v23-history.json")
            Path(legacy_state).write_text(json.dumps(legacy))
            migrated = SemanticProposalGenerator(legacy_state)
            self.assertFalse(migrated.pcfg_history_contexts)
            self.assertFalse(migrated.pcfg_history_counts)
            self.assertEqual(
                migrated.pcfg_sibling_counts,
                generator.pcfg_sibling_counts,
            )

    def test_prequential_context_calibration_falls_back_on_drift(self):
        generator = SemanticProposalGenerator(None)
        family_id, family = generator._pcfg_family(
            "prequential-fixture",
            "value",
            "choice",
        )
        parent_shape_id = hashlib.sha256(b"parent-shape").hexdigest()
        context_id, context = generator._pcfg_context(
            "prequential-fixture",
            family_id,
            parent_shape_id,
            0,
        )
        shape_x = hashlib.sha256(b"shape-x").hexdigest()
        shape_y = hashlib.sha256(b"shape-y").hexdigest()
        generator.pcfg_families[family_id] = family
        generator.pcfg_counts[family_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_contexts[context_id] = context
        generator.pcfg_context_counts[context_id] = {
            shape_x: 4,
            shape_y: 1,
        }

        def add_receipt(
            index: int,
            *,
            global_selected: int,
            global_total: int,
            context_selected: int,
            context_total: int,
        ) -> None:
            receipt = generator._pcfg_prequential_receipt(
                fragment_sha256=hashlib.sha256(
                    f"fragment-{index}".encode()).hexdigest(),
                node_id=hashlib.sha256(
                    f"node-{index}".encode()).hexdigest(),
                context=context,
                shape_id=shape_x,
                global_selected_before=global_selected,
                global_total_before=global_total,
                known_shapes=2,
                context_selected_before=context_selected,
                context_total_before=context_total,
            )
            generator.pcfg_prequential_receipts[
                receipt["receipt_id"]] = receipt

        for index in range(4):
            add_receipt(
                index,
                global_selected=0,
                global_total=0,
                context_selected=3,
                context_total=4,
            )
        generator._refresh_pcfg_context_calibration()
        initial_weight = float(
            generator.pcfg_context_calibration_stats[
                context_id]["weight"])
        self.assertGreater(initial_weight, 0.0)
        effective, raw, weight = (
            generator._pcfg_effective_context_probability(
                context_id,
                shape_x,
                context=context,
            )
        )
        self.assertAlmostEqual(raw, 5.0 / 7.0)
        self.assertEqual(weight, initial_weight)
        self.assertGreater(effective, 0.5)

        for index in range(4, 6):
            add_receipt(
                index,
                global_selected=8,
                global_total=9,
                context_selected=0,
                context_total=8,
            )
        generator._refresh_pcfg_context_calibration()
        self.assertLess(
            float(generator.pcfg_context_calibration_stats[
                context_id]["gain_bits"]),
            0.0,
        )
        self.assertEqual(
            float(generator.pcfg_context_calibration_stats[
                context_id]["weight"]),
            0.0,
        )
        effective, raw, weight = (
            generator._pcfg_effective_context_probability(
                context_id,
                shape_x,
                context=context,
            )
        )
        self.assertAlmostEqual(raw, 5.0 / 7.0)
        self.assertEqual(weight, 0.0)
        self.assertAlmostEqual(effective, 0.5)

    def test_recency_calibration_detects_stale_and_recovers(self):
        generator = SemanticProposalGenerator(None)
        family_id, family = generator._pcfg_family(
            "recency-fixture",
            "value",
            "choice",
        )
        parent_shape_id = hashlib.sha256(b"recent-parent").hexdigest()
        context_id, context = generator._pcfg_context(
            "recency-fixture",
            family_id,
            parent_shape_id,
            0,
        )
        other_context_id, other_context = generator._pcfg_context(
            "recency-fixture",
            family_id,
            hashlib.sha256(b"other-parent").hexdigest(),
            0,
        )
        shape_x = hashlib.sha256(b"recent-x").hexdigest()
        shape_y = hashlib.sha256(b"recent-y").hexdigest()
        generator.pcfg_families[family_id] = family
        generator.pcfg_counts[family_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_contexts[context_id] = context
        generator.pcfg_contexts[other_context_id] = other_context
        generator.pcfg_context_counts[context_id] = {
            shape_x: 4,
            shape_y: 1,
        }

        def add_receipt(
            index: int,
            receipt_context: dict[str, object],
            *,
            global_selected: int,
            global_total: int,
            context_selected: int,
            context_total: int,
        ) -> None:
            receipt = generator._pcfg_prequential_receipt(
                fragment_sha256=hashlib.sha256(
                    f"recent-fragment-{index}".encode()).hexdigest(),
                node_id=hashlib.sha256(
                    f"recent-node-{index}".encode()).hexdigest(),
                context=receipt_context,
                shape_id=shape_x,
                global_selected_before=global_selected,
                global_total_before=global_total,
                known_shapes=2,
                context_selected_before=context_selected,
                context_total_before=context_total,
                observation_index=index,
            )
            generator.pcfg_prequential_receipts[
                receipt["receipt_id"]] = receipt

        for index in range(8):
            add_receipt(
                index,
                context,
                global_selected=0,
                global_total=0,
                context_selected=3,
                context_total=4,
            )
        generator._refresh_pcfg_context_calibration()
        initial = generator.pcfg_context_calibration_stats[
            context_id]
        self.assertEqual(initial["recency_mode"], 1)
        self.assertEqual(initial["recent_observations"], 8)
        self.assertGreater(float(initial["weight"]), 0.0)

        for index in (24, 25):
            add_receipt(
                index,
                context,
                global_selected=8,
                global_total=9,
                context_selected=0,
                context_total=8,
            )
        generator._refresh_pcfg_context_calibration()
        drifted = generator.pcfg_context_calibration_stats[
            context_id]
        self.assertEqual(drifted["recent_observations"], 2)
        self.assertLess(float(drifted["recent_gain_bits"]), 0.0)
        self.assertEqual(float(drifted["weight"]), 0.0)

        for index in (40, 41):
            add_receipt(
                index,
                other_context,
                global_selected=0,
                global_total=0,
                context_selected=3,
                context_total=4,
            )
        generator._refresh_pcfg_context_calibration()
        stale = generator.pcfg_context_calibration_stats[
            context_id]
        self.assertEqual(stale["recent_observations"], 0)
        self.assertEqual(stale["stale"], 1)
        self.assertEqual(float(stale["weight"]), 0.0)

        for index in range(42, 50):
            add_receipt(
                index,
                context,
                global_selected=0,
                global_total=0,
                context_selected=3,
                context_total=4,
            )
        generator._refresh_pcfg_context_calibration()
        recovered = generator.pcfg_context_calibration_stats[
            context_id]
        self.assertEqual(recovered["stale"], 0)
        self.assertEqual(recovered["recent_observations"], 8)
        self.assertGreater(float(recovered["recent_gain_bits"]), 0.0)
        self.assertGreater(float(recovered["weight"]), 0.0)
        effective, raw, weight = (
            generator._pcfg_effective_context_probability(
                context_id,
                shape_x,
                context=context,
            )
        )
        self.assertAlmostEqual(raw, 5.0 / 7.0)
        self.assertGreater(weight, 0.0)
        self.assertGreater(effective, 0.5)

    def test_adaptive_calibration_certifies_drift_and_recovery(self):
        generator = SemanticProposalGenerator(None)
        family_id, family = generator._pcfg_family(
            "adaptive-fixture",
            "value",
            "choice",
        )
        context_id, context = generator._pcfg_context(
            "adaptive-fixture",
            family_id,
            hashlib.sha256(b"adaptive-parent").hexdigest(),
            0,
        )
        shape_x = hashlib.sha256(b"adaptive-x").hexdigest()
        shape_y = hashlib.sha256(b"adaptive-y").hexdigest()
        generator.pcfg_families[family_id] = family
        generator.pcfg_counts[family_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_contexts[context_id] = context
        generator.pcfg_context_counts[context_id] = {
            shape_x: 4,
            shape_y: 1,
        }
        generator._allocate_pcfg_anytime_contexts([(
            "parent", context_id, 0,
        )])

        def add_receipt(index: int, *, context_wins: bool) -> None:
            receipt = generator._pcfg_prequential_receipt(
                fragment_sha256=hashlib.sha256(
                    f"adaptive-fragment-{index}".encode()
                ).hexdigest(),
                node_id=hashlib.sha256(
                    f"adaptive-node-{index}".encode()
                ).hexdigest(),
                context=context,
                shape_id=shape_x,
                global_selected_before=0 if context_wins else 99,
                global_total_before=99,
                known_shapes=2,
                context_selected_before=99 if context_wins else 0,
                context_total_before=99,
                observation_index=index,
            )
            generator.pcfg_prequential_receipts[
                receipt["receipt_id"]] = receipt

        for index in range(16):
            add_receipt(index, context_wins=True)
        generator._refresh_pcfg_context_calibration()
        stationary = generator.pcfg_context_calibration_stats[
            context_id]
        self.assertEqual(stationary["adaptive_cut_count"], 0)
        self.assertEqual(stationary["adaptive_fragments"], 16)
        self.assertGreater(float(stationary["weight"]), 0.0)
        self.assertNotIn(
            context_id, generator.pcfg_adaptive_certificates)

        for index in range(16, 32):
            add_receipt(index, context_wins=False)
        generator._refresh_pcfg_context_calibration()
        drifted = generator.pcfg_context_calibration_stats[
            context_id]
        self.assertEqual(drifted["adaptive_cut_count"], 1)
        self.assertIn(drifted["adaptive_start"], range(14, 17))
        self.assertEqual(
            drifted["adaptive_fragments"],
            32 - int(drifted["adaptive_start"]),
        )
        self.assertEqual(
            drifted["adaptive_truncated_fragments"],
            int(drifted["adaptive_start"]),
        )
        self.assertLess(float(drifted["adaptive_gain_bits"]), 0.0)
        self.assertEqual(float(drifted["weight"]), 0.0)
        certificate = generator.pcfg_adaptive_certificates[
            context_id][0]
        self.assertIn(
            certificate["cut_observation_index"], range(14, 17))
        self.assertGreater(
            float(certificate["difference_bits"]),
            float(certificate["epsilon_bits"]),
        )
        self.assertTrue(
            generator.verify_pcfg_adaptive_certificate(certificate))
        tampered = json.loads(json.dumps(certificate))
        tampered["epsilon_bits"] = 0.0
        self.assertFalse(
            generator.verify_pcfg_adaptive_certificate(tampered))
        snapshot = generator.grammar_snapshot()
        self.assertEqual(snapshot["pcfg_adaptive_certificates"], 1)
        self.assertEqual(
            snapshot["pcfg_adaptive_fragments"],
            drifted["adaptive_fragments"],
        )
        serialized = generator.to_mapping()
        self.assertEqual(serialized["schema"], 25)
        self.assertEqual(
            serialized["pcfg_adaptive_certificates"],
            [certificate],
        )

        for index in range(32, 48):
            add_receipt(index, context_wins=True)
        generator._refresh_pcfg_context_calibration()
        recovered = generator.pcfg_context_calibration_stats[
            context_id]
        self.assertEqual(recovered["adaptive_cut_count"], 2)
        self.assertIn(recovered["adaptive_start"], range(31, 33))
        self.assertEqual(
            recovered["adaptive_fragments"],
            48 - int(recovered["adaptive_start"]),
        )
        self.assertGreater(float(recovered["adaptive_gain_bits"]), 0.0)
        self.assertGreater(float(recovered["weight"]), 0.0)
        certificates = generator.pcfg_adaptive_certificates[
            context_id]
        self.assertIn(
            certificates[0]["cut_observation_index"], range(14, 17))
        self.assertIn(
            certificates[1]["cut_observation_index"], range(31, 33))
        self.assertTrue(all(
            generator.verify_pcfg_adaptive_certificate(item)
            for item in certificates
        ))

        generator.pcfg_prequential_receipts.clear()
        for index in range(32):
            add_receipt(index, context_wins=True)
        for index in range(32, 64):
            add_receipt(index, context_wins=False)
        generator._refresh_pcfg_context_calibration()
        anytime_stats = generator.pcfg_context_calibration_stats[
            context_id]
        self.assertEqual(anytime_stats["anytime_certified"], 1)
        anytime = generator.pcfg_anytime_certificates[
            context_id][0]
        self.assertEqual(
            anytime["schema"],
            "symcc-parser-pcfg-anytime-cut-v1",
        )
        self.assertEqual(
            anytime["guarantee"],
            "context-wise-infinite-horizon-pfa",
        )
        self.assertGreater(float(anytime["separation_gap"]), 0.0)
        self.assertTrue(
            generator.verify_pcfg_anytime_certificate(anytime))
        tampered_anytime = json.loads(json.dumps(anytime))
        tampered_anytime["earlier"]["launch_alpha"] = 0.05
        self.assertFalse(
            generator.verify_pcfg_anytime_certificate(
                tampered_anytime))
        spending = sum(
            generator.PCFG_ANYTIME_SPENDING_SCALE / (index ** 2)
            for index in range(1, 10001)
        )
        self.assertLess(spending, 1.0)
        snapshot = generator.grammar_snapshot()
        self.assertEqual(snapshot["pcfg_anytime_certificates"], 1)
        self.assertEqual(
            generator.to_mapping()["pcfg_anytime_certificates"],
            [anytime],
        )
        generator._refresh_pcfg_global_anytime_certificates()
        parent_allocation_key = (
            generator._pcfg_anytime_allocation_key(
                "parent", context_id)
        )
        parent_global_anytime = (
            generator.pcfg_global_anytime_certificates[
                parent_allocation_key][0]
        )
        self.assertEqual(
            parent_global_anytime["context_kind"], "parent")
        self.assertTrue(
            generator.verify_pcfg_global_anytime_certificate(
                parent_global_anytime))

    def test_circuit_adaptive_certificate_uses_two_baselines(self):
        generator = SemanticProposalGenerator(None)
        parser = "circuit-adaptive-fixture"
        family_id, family = generator._pcfg_family(
            parser, "value", "choice")
        parent_shape = hashlib.sha256(
            b"circuit-adaptive-parent").hexdigest()
        ancestor_shape = hashlib.sha256(
            b"circuit-adaptive-ancestor").hexdigest()
        parent_context_id, parent_context = generator._pcfg_context(
            parser, family_id, parent_shape, 0)
        circuit_context_id, circuit_context = (
            generator._pcfg_circuit_context(
                parser,
                family_id,
                parent_shape,
                0,
                ancestor_shape,
                1,
            )
        )
        shape_x = hashlib.sha256(b"circuit-adaptive-x").hexdigest()
        shape_y = hashlib.sha256(b"circuit-adaptive-y").hexdigest()
        generator.pcfg_families[family_id] = family
        generator.pcfg_counts[family_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_contexts[
            parent_context_id] = parent_context
        generator.pcfg_context_counts[parent_context_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_circuit_contexts[
            circuit_context_id] = circuit_context
        generator.pcfg_circuit_counts[circuit_context_id] = {
            shape_x: 4,
            shape_y: 1,
        }
        generator._allocate_pcfg_anytime_contexts([(
            "circuit", circuit_context_id, 0,
        )])

        def add_receipt(index: int, *, circuit_wins: bool) -> None:
            receipt = generator._pcfg_circuit_receipt(
                fragment_sha256=hashlib.sha256(
                    f"circuit-fragment-{index}".encode()
                ).hexdigest(),
                node_id=hashlib.sha256(
                    f"circuit-node-{index}".encode()
                ).hexdigest(),
                context=circuit_context,
                parent_context_id=parent_context_id,
                shape_id=shape_x,
                global_selected_before=(
                    0 if circuit_wins else 99),
                global_total_before=99,
                known_shapes=2,
                parent_selected_before=(
                    0 if circuit_wins else 99),
                parent_total_before=99,
                circuit_selected_before=(
                    99 if circuit_wins else 0),
                circuit_total_before=99,
                observation_index=index,
            )
            generator.pcfg_circuit_receipts[
                receipt["receipt_id"]] = receipt

        for index in range(16):
            add_receipt(index, circuit_wins=True)
        generator._refresh_pcfg_circuit_calibration()
        stationary = generator.pcfg_circuit_calibration_stats[
            circuit_context_id]
        self.assertEqual(stationary["adaptive_cut_count"], 0)
        self.assertGreater(float(stationary["weight"]), 0.0)

        for index in range(16, 32):
            add_receipt(index, circuit_wins=False)
        generator._refresh_pcfg_circuit_calibration()
        drifted = generator.pcfg_circuit_calibration_stats[
            circuit_context_id]
        self.assertEqual(drifted["adaptive_cut_count"], 1)
        self.assertLess(
            float(drifted["adaptive_global_gain_bits"]), 0.0)
        self.assertLess(
            float(drifted["adaptive_parent_gain_bits"]), 0.0)
        self.assertEqual(float(drifted["weight"]), 0.0)
        certificate = generator.pcfg_circuit_certificates[
            circuit_context_id][0]
        self.assertTrue(
            generator.verify_pcfg_circuit_certificate(certificate))
        tampered = json.loads(json.dumps(certificate))
        tampered["epsilon_bits"] = 0.0
        self.assertFalse(
            generator.verify_pcfg_circuit_certificate(tampered))

        for index in range(32, 48):
            add_receipt(index, circuit_wins=True)
        generator._refresh_pcfg_circuit_calibration()
        recovered = generator.pcfg_circuit_calibration_stats[
            circuit_context_id]
        self.assertEqual(recovered["adaptive_cut_count"], 2)
        self.assertGreater(
            float(recovered["adaptive_robust_gain_bits"]), 0.0)
        self.assertGreater(float(recovered["weight"]), 0.0)
        self.assertTrue(all(
            generator.verify_pcfg_circuit_certificate(item)
            for item in generator.pcfg_circuit_certificates[
                circuit_context_id]
        ))

        generator.pcfg_circuit_receipts.clear()
        for index in range(32):
            add_receipt(index, circuit_wins=True)
        for index in range(32, 64):
            add_receipt(index, circuit_wins=False)
        generator._refresh_pcfg_circuit_calibration()
        anytime_stats = generator.pcfg_circuit_calibration_stats[
            circuit_context_id]
        self.assertEqual(anytime_stats["anytime_certified"], 1)
        anytime = generator.pcfg_circuit_anytime_certificates[
            circuit_context_id][0]
        self.assertTrue(
            generator.verify_pcfg_circuit_anytime_certificate(
                anytime))
        tampered_anytime = json.loads(json.dumps(anytime))
        tampered_anytime["separation_gap"] = 0.0
        self.assertFalse(
            generator.verify_pcfg_circuit_anytime_certificate(
                tampered_anytime))
        self.assertEqual(
            generator.grammar_snapshot()[
                "pcfg_circuit_anytime_certificates"],
            1,
        )
        generator._refresh_pcfg_global_anytime_certificates()
        circuit_allocation_key = (
            generator._pcfg_anytime_allocation_key(
                "circuit", circuit_context_id)
        )
        circuit_global_anytime = (
            generator.pcfg_global_anytime_certificates[
                circuit_allocation_key][0]
        )
        self.assertEqual(
            circuit_global_anytime["context_kind"], "circuit")
        self.assertTrue(
            generator.verify_pcfg_global_anytime_certificate(
                circuit_global_anytime))

    def test_sibling_adaptive_certificate_uses_three_baselines(self):
        generator = SemanticProposalGenerator(None)
        parser = "sibling-adaptive-fixture"
        family_id, family = generator._pcfg_family(
            parser, "value", "choice")
        parent_shape = hashlib.sha256(
            b"sibling-adaptive-parent").hexdigest()
        ancestor_shape = hashlib.sha256(
            b"sibling-adaptive-ancestor").hexdigest()
        left_shape = hashlib.sha256(
            b"sibling-adaptive-left").hexdigest()
        parent_context_id, parent_context = generator._pcfg_context(
            parser, family_id, parent_shape, 1)
        circuit_context_id, circuit_context = (
            generator._pcfg_circuit_context(
                parser,
                family_id,
                parent_shape,
                1,
                ancestor_shape,
                0,
            )
        )
        sibling_context_id, sibling_context = (
            generator._pcfg_sibling_context(
                parser,
                family_id,
                parent_shape,
                1,
                left_shape,
            )
        )
        shape_x = hashlib.sha256(b"sibling-adaptive-x").hexdigest()
        shape_y = hashlib.sha256(b"sibling-adaptive-y").hexdigest()
        generator.pcfg_families[family_id] = family
        generator.pcfg_counts[family_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_contexts[
            parent_context_id] = parent_context
        generator.pcfg_context_counts[parent_context_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_circuit_contexts[
            circuit_context_id] = circuit_context
        generator.pcfg_circuit_counts[circuit_context_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_sibling_contexts[
            sibling_context_id] = sibling_context
        generator.pcfg_sibling_counts[sibling_context_id] = {
            shape_x: 4,
            shape_y: 1,
        }
        generator._allocate_pcfg_anytime_contexts([(
            "sibling", sibling_context_id, 0,
        )])

        def add_receipt(index: int, *, sibling_wins: bool) -> None:
            receipt = generator._pcfg_sibling_receipt(
                fragment_sha256=hashlib.sha256(
                    f"sibling-fragment-{index}".encode()
                ).hexdigest(),
                node_id=hashlib.sha256(
                    f"sibling-node-{index}".encode()
                ).hexdigest(),
                context=sibling_context,
                parent_context_id=parent_context_id,
                circuit_context_id=circuit_context_id,
                shape_id=shape_x,
                global_selected_before=(
                    0 if sibling_wins else 99),
                global_total_before=99,
                known_shapes=2,
                parent_selected_before=(
                    0 if sibling_wins else 99),
                parent_total_before=99,
                circuit_selected_before=(
                    0 if sibling_wins else 99),
                circuit_total_before=99,
                sibling_selected_before=(
                    99 if sibling_wins else 0),
                sibling_total_before=99,
                observation_index=index,
            )
            generator.pcfg_sibling_receipts[
                receipt["receipt_id"]] = receipt

        for index in range(16):
            add_receipt(index, sibling_wins=True)
        generator._refresh_pcfg_sibling_calibration()
        stationary = generator.pcfg_sibling_calibration_stats[
            sibling_context_id]
        self.assertEqual(stationary["adaptive_cut_count"], 0)
        self.assertGreater(float(stationary["weight"]), 0.0)

        for index in range(16, 32):
            add_receipt(index, sibling_wins=False)
        generator._refresh_pcfg_sibling_calibration()
        drifted = generator.pcfg_sibling_calibration_stats[
            sibling_context_id]
        self.assertEqual(drifted["adaptive_cut_count"], 1)
        self.assertLess(
            float(drifted["adaptive_robust_gain_bits"]), 0.0)
        self.assertEqual(float(drifted["weight"]), 0.0)
        certificate = generator.pcfg_sibling_certificates[
            sibling_context_id][0]
        self.assertEqual(
            certificate["schema"],
            "symcc-parser-pcfg-sibling-cut-v1",
        )
        self.assertTrue(
            generator.verify_pcfg_sibling_certificate(certificate))
        tampered = json.loads(json.dumps(certificate))
        tampered["epsilon_bits"] = 0.0
        self.assertFalse(
            generator.verify_pcfg_sibling_certificate(tampered))
        snapshot = generator.grammar_snapshot()
        self.assertEqual(
            snapshot["pcfg_sibling_adaptive_certificates"], 1)
        self.assertEqual(
            generator.to_mapping()["pcfg_sibling_certificates"],
            [certificate],
        )

        for index in range(32, 48):
            add_receipt(index, sibling_wins=True)
        generator._refresh_pcfg_sibling_calibration()
        recovered = generator.pcfg_sibling_calibration_stats[
            sibling_context_id]
        self.assertEqual(recovered["adaptive_cut_count"], 2)
        self.assertGreater(
            float(recovered["adaptive_robust_gain_bits"]), 0.0)
        self.assertGreater(float(recovered["weight"]), 0.0)
        self.assertTrue(all(
            generator.verify_pcfg_sibling_certificate(item)
            for item in generator.pcfg_sibling_certificates[
                sibling_context_id]
        ))

        generator.pcfg_sibling_receipts.clear()
        for index in range(32):
            add_receipt(index, sibling_wins=True)
        for index in range(32, 64):
            add_receipt(index, sibling_wins=False)
        generator._refresh_pcfg_sibling_calibration()
        anytime_stats = generator.pcfg_sibling_calibration_stats[
            sibling_context_id]
        self.assertEqual(anytime_stats["anytime_certified"], 1)
        anytime = generator.pcfg_sibling_anytime_certificates[
            sibling_context_id][0]
        self.assertTrue(
            generator.verify_pcfg_sibling_anytime_certificate(
                anytime))
        tampered_anytime = json.loads(json.dumps(anytime))
        tampered_anytime["later"]["running_upper"] = 0.0
        self.assertFalse(
            generator.verify_pcfg_sibling_anytime_certificate(
                tampered_anytime))
        snapshot = generator.grammar_snapshot()
        self.assertEqual(
            snapshot["pcfg_sibling_anytime_certificates"], 1)
        self.assertEqual(
            generator.to_mapping()[
                "pcfg_sibling_anytime_certificates"],
            [anytime],
        )
        generator._refresh_pcfg_global_anytime_certificates()
        sibling_allocation_key = (
            generator._pcfg_anytime_allocation_key(
                "sibling", sibling_context_id)
        )
        sibling_global_anytime = (
            generator.pcfg_global_anytime_certificates[
                sibling_allocation_key][0]
        )
        self.assertEqual(
            sibling_global_anytime["context_kind"], "sibling")
        self.assertTrue(
            generator.verify_pcfg_global_anytime_certificate(
                sibling_global_anytime))

    def test_history_adaptive_certificate_uses_four_baselines(self):
        generator = SemanticProposalGenerator(None)
        parser = "history-adaptive-fixture"
        family_id, family = generator._pcfg_family(
            parser, "value", "choice")
        parent_shape = hashlib.sha256(
            b"history-adaptive-parent").hexdigest()
        ancestor_shape = hashlib.sha256(
            b"history-adaptive-ancestor").hexdigest()
        older_shape = hashlib.sha256(
            b"history-adaptive-older").hexdigest()
        left_shape = hashlib.sha256(
            b"history-adaptive-left").hexdigest()
        parent_context_id, parent_context = generator._pcfg_context(
            parser, family_id, parent_shape, 2)
        circuit_context_id, circuit_context = (
            generator._pcfg_circuit_context(
                parser,
                family_id,
                parent_shape,
                2,
                ancestor_shape,
                0,
            )
        )
        sibling_context_id, sibling_context = (
            generator._pcfg_sibling_context(
                parser,
                family_id,
                parent_shape,
                2,
                left_shape,
            )
        )
        history_context_id, history_context = (
            generator._pcfg_history_context(
                parser,
                family_id,
                parent_shape,
                2,
                older_shape,
                left_shape,
            )
        )
        shape_x = hashlib.sha256(
            b"history-adaptive-x").hexdigest()
        shape_y = hashlib.sha256(
            b"history-adaptive-y").hexdigest()
        generator.pcfg_families[family_id] = family
        generator.pcfg_counts[family_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_contexts[
            parent_context_id] = parent_context
        generator.pcfg_context_counts[parent_context_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_circuit_contexts[
            circuit_context_id] = circuit_context
        generator.pcfg_circuit_counts[circuit_context_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_sibling_contexts[
            sibling_context_id] = sibling_context
        generator.pcfg_sibling_counts[sibling_context_id] = {
            shape_x: 5,
            shape_y: 5,
        }
        generator.pcfg_history_contexts[
            history_context_id] = history_context
        generator.pcfg_history_counts[history_context_id] = {
            shape_x: 4,
            shape_y: 1,
        }
        generator._allocate_pcfg_anytime_contexts([(
            "history", history_context_id, 0,
        )])

        def add_receipt(index: int, *, history_wins: bool) -> None:
            baseline_selected = 0 if history_wins else 99
            receipt = generator._pcfg_history_receipt(
                fragment_sha256=hashlib.sha256(
                    f"history-fragment-{index}".encode()
                ).hexdigest(),
                node_id=hashlib.sha256(
                    f"history-node-{index}".encode()
                ).hexdigest(),
                context=history_context,
                parent_context_id=parent_context_id,
                circuit_context_id=circuit_context_id,
                sibling_context_id=sibling_context_id,
                shape_id=shape_x,
                global_selected_before=baseline_selected,
                global_total_before=99,
                known_shapes=2,
                parent_selected_before=baseline_selected,
                parent_total_before=99,
                circuit_selected_before=baseline_selected,
                circuit_total_before=99,
                sibling_selected_before=baseline_selected,
                sibling_total_before=99,
                history_selected_before=(
                    99 if history_wins else 0),
                history_total_before=99,
                observation_index=index,
            )
            generator.pcfg_history_receipts[
                receipt["receipt_id"]] = receipt

        for index in range(16):
            add_receipt(index, history_wins=True)
        generator._refresh_pcfg_history_calibration()
        stationary = generator.pcfg_history_calibration_stats[
            history_context_id]
        self.assertEqual(stationary["adaptive_cut_count"], 0)
        self.assertGreater(float(stationary["weight"]), 0.0)

        for index in range(16, 32):
            add_receipt(index, history_wins=False)
        generator._refresh_pcfg_history_calibration()
        drifted = generator.pcfg_history_calibration_stats[
            history_context_id]
        self.assertEqual(drifted["adaptive_cut_count"], 1)
        self.assertLess(
            float(drifted["adaptive_robust_gain_bits"]), 0.0)
        self.assertEqual(float(drifted["weight"]), 0.0)
        certificate = generator.pcfg_history_certificates[
            history_context_id][0]
        self.assertEqual(
            certificate["schema"],
            "symcc-parser-pcfg-history-cut-v1",
        )
        self.assertTrue(
            generator.verify_pcfg_history_certificate(certificate))
        tampered = json.loads(json.dumps(certificate))
        tampered["epsilon_bits"] = 0.0
        self.assertFalse(
            generator.verify_pcfg_history_certificate(tampered))
        self.assertEqual(
            generator.grammar_snapshot()[
                "pcfg_history_adaptive_certificates"],
            1,
        )
        self.assertEqual(
            generator.to_mapping()["pcfg_history_certificates"],
            [certificate],
        )

        generator.pcfg_history_receipts.clear()
        for index in range(32):
            add_receipt(index, history_wins=True)
        for index in range(32, 64):
            add_receipt(index, history_wins=False)
        generator._refresh_pcfg_history_calibration()
        anytime_stats = generator.pcfg_history_calibration_stats[
            history_context_id]
        self.assertEqual(anytime_stats["anytime_certified"], 1)
        anytime = generator.pcfg_history_anytime_certificates[
            history_context_id][0]
        self.assertEqual(
            anytime["schema"],
            "symcc-parser-pcfg-history-anytime-cut-v1",
        )
        self.assertTrue(
            generator.verify_pcfg_history_anytime_certificate(
                anytime))
        tampered_anytime = json.loads(json.dumps(anytime))
        tampered_anytime["later"]["running_lower"] = 1.0
        self.assertFalse(
            generator.verify_pcfg_history_anytime_certificate(
                tampered_anytime))
        snapshot = generator.grammar_snapshot()
        self.assertEqual(
            snapshot["pcfg_history_anytime_certificates"], 1)
        self.assertEqual(
            generator.to_mapping()[
                "pcfg_history_anytime_certificates"],
            [anytime],
        )
        generator._refresh_pcfg_global_anytime_certificates()
        allocation_key = generator._pcfg_anytime_allocation_key(
            "history", history_context_id)
        global_anytime = (
            generator.pcfg_global_anytime_certificates[
                allocation_key][0]
        )
        self.assertEqual(
            global_anytime["schema"],
            "symcc-parser-pcfg-global-anytime-cut-v1",
        )
        self.assertEqual(
            global_anytime["guarantee"],
            "cross-context-infinite-horizon-fwer",
        )
        self.assertEqual(global_anytime["context_ordinal"], 1)
        self.assertLess(
            float(global_anytime["context_delta"]),
            generator.PCFG_GLOBAL_FWER_DELTA,
        )
        self.assertEqual(
            global_anytime["cross_context_dependence_assumption"],
            "none",
        )
        self.assertTrue(
            generator.verify_pcfg_global_anytime_certificate(
                global_anytime))
        tampered_global = json.loads(json.dumps(global_anytime))
        tampered_global["allocation_prefix_sha256"] = "0" * 64
        self.assertFalse(
            generator.verify_pcfg_global_anytime_certificate(
                tampered_global))
        generator._allocate_pcfg_anytime_contexts([
            (
                "parent",
                hashlib.sha256(
                    f"global-null-context-{index}".encode()
                ).hexdigest(),
                0,
            )
            for index in range(1, 257)
        ])
        ordinals = sorted(
            int(allocation["ordinal"])
            for allocation in
            generator.pcfg_anytime_context_allocations.values()
        )
        self.assertEqual(
            ordinals, list(range(1, len(ordinals) + 1)))
        generator._refresh_pcfg_global_anytime_certificates()
        global_snapshot = generator.grammar_snapshot()
        self.assertEqual(
            global_snapshot["pcfg_global_anytime_certificates"], 1)
        self.assertLessEqual(
            float(global_snapshot["pcfg_anytime_allocated_delta"]),
            generator.PCFG_GLOBAL_FWER_DELTA,
        )
        self.assertEqual(
            generator.to_mapping()[
                "pcfg_global_anytime_certificates"],
            [global_anytime],
        )

    def test_nullable_scc_fixed_point_enables_context_safe_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,sys;"
                "json.dump({"
                "'schema':'symcc-parser-structural-trace-v4',"
                "'parser':'nullable-scc-fixture-v4','accepted':True,"
                "'roots':[0],'nodes':["
                "{'symbol':'A','state':'q','start':0,'end':1}],"
                "'nullable_rules':["
                "{'lhs':['A','q'],'rhs':[['B','q']]},"
                "{'lhs':['B','q'],'rhs':[['A','q']]},"
                "{'lhs':['B','q'],'rhs':[]}"
                "]},open(sys.argv[2],'w'))"
            )
            source = os.path.join(tmp, "source")
            Path(source).write_bytes(b"x")
            state = os.path.join(tmp, "nullable-state.json")
            generator = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            source_rule = generator._learn_rule("literal", b"", b"x")
            assert source_rule is not None
            telemetry = SolverTelemetry(
                comparison_taints=((1, 9, 1, 0, 0, 1, 1),))
            cores = extract_constraint_cores(telemetry, 1)
            spans = infer_token_spans(b"x", cores)
            source_context = _grammar_context_id(b"x", spans[0])
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "nullable-proposals"),
                parser_command=(
                    sys.executable,
                    "-c",
                    parser_script,
                    "{input}",
                    "{trace}",
                ),
            )
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "source_path": source,
                "candidate": {"text": "x"},
                "target_branch": 9,
                "grammar_rule_id": source_rule.rule_id,
                "grammar_context_id": source_context,
                "grammar_source_context_id": source_context,
                "grammar_span": [0, 1],
            })
            self.assertIsNotNone(proposal_id)
            self.assertTrue(manager.validate(
                str(proposal_id),
                SolverTelemetry(target_branch=9, target_reached=True),
                retcode=0,
                killed=False,
            ))
            record = manager.records[str(proposal_id)]
            self.assertTrue(generator.observe_grammar_validation(
                source_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=record.parser_context_id,
                source_context_id=source_context,
                candidate_context_id=(
                    record.grammar_candidate_context_id),
                production_id=record.parser_production_id,
                cfg_fragment_json=record.parser_cfg_fragment_json,
                cfg_fragment_sha256=record.parser_cfg_fragment_sha256,
            ))
            snapshot = generator.grammar_snapshot()
            self.assertEqual(snapshot["nullable_rules"], 3)
            self.assertEqual(snapshot["nullable_proofs"], 2)
            self.assertEqual(snapshot["nullable_sccs"], 1)
            self.assertEqual(snapshot["nullable_shapes"], 1)
            self.assertEqual(snapshot["nullable_max_depth"], 1)
            epsilon_rule = next(
                rule for rule in generator.grammar_rules.values()
                if rule.kind == "epsilon")
            self.assertTrue(any(
                candidate[2] == b"" and
                candidate[3] == epsilon_rule.rule_id
                for candidate in generator._solve_complete_candidates(
                    b"x", cores, spans)
            ))
            wrong_span = type(spans[0])(
                spans[0].lo, spans[0].hi, spans[0].token, 10)
            self.assertFalse(any(
                candidate[3] == epsilon_rule.rule_id
                for candidate in generator._solve_complete_candidates(
                    b"x", cores, (wrong_span,))
            ))
            generator.save()

            restored = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            self.assertEqual(restored.SCHEMA, 25)
            self.assertEqual(
                restored.grammar_snapshot()["nullable_proofs"], 2)
            selected_instance = next(
                instance_id
                for instance_id, instance in restored.ect_instances.items()
                if instance["selected"]
            )
            restored._drop_ect_instance(selected_instance)
            self.assertTrue(any(
                candidate[2] == b"" and
                candidate[3] == epsilon_rule.rule_id
                for candidate in restored._solve_complete_candidates(
                    b"x", cores, spans)
            ))

            tampered = json.loads(record.parser_cfg_fragment_json)
            tampered["nullable_proofs"][0]["depth"] += 1
            tampered_json = json.dumps(
                tampered, sort_keys=True, separators=(",", ":"))
            rejected = SemanticProposalGenerator(None)
            rejected_rule = rejected._learn_rule(
                "literal", b"", b"x")
            assert rejected_rule is not None
            self.assertTrue(rejected.observe_grammar_validation(
                rejected_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=record.parser_context_id,
                source_context_id=source_context,
                candidate_context_id=(
                    record.grammar_candidate_context_id),
                production_id=record.parser_production_id,
                cfg_fragment_json=tampered_json,
                cfg_fragment_sha256=hashlib.sha256(
                    tampered_json.encode("utf-8")).hexdigest(),
            ))
            self.assertEqual(rejected.grammar_snapshot()["nullable_rules"], 0)
            self.assertEqual(rejected.grammar_snapshot()["epsilon_rules"], 0)

            pure_fragment = json.loads(record.parser_cfg_fragment_json)
            pure_certificate = VerifiedProposalManager._nullable_certificate(
                pure_fragment["parser"],
                [
                    {"lhs": ["A", "q"], "rhs": [["B", "q"]]},
                    {"lhs": ["B", "q"], "rhs": [["A", "q"]]},
                ],
            )
            assert pure_certificate is not None
            pure_fragment.update(pure_certificate)
            pure_json = json.dumps(
                pure_fragment, sort_keys=True, separators=(",", ":"))
            pure_generator = SemanticProposalGenerator(None)
            pure_rule = pure_generator._learn_rule(
                "literal", b"", b"x")
            assert pure_rule is not None
            self.assertTrue(pure_generator.observe_grammar_validation(
                pure_rule.rule_id,
                valid=True,
                parser_valid=True,
                context_id=record.parser_context_id,
                source_context_id=source_context,
                candidate_context_id=(
                    record.grammar_candidate_context_id),
                production_id=record.parser_production_id,
                cfg_fragment_json=pure_json,
                cfg_fragment_sha256=hashlib.sha256(
                    pure_json.encode("utf-8")).hexdigest(),
            ))
            pure_snapshot = pure_generator.grammar_snapshot()
            self.assertEqual(pure_snapshot["nullable_rules"], 2)
            self.assertEqual(pure_snapshot["nullable_proofs"], 0)
            self.assertEqual(pure_snapshot["nullable_sccs"], 1)
            self.assertEqual(pure_snapshot["nullable_shapes"], 0)
            self.assertEqual(pure_snapshot["epsilon_rules"], 0)

            concrete_shape = hashlib.sha256(
                b"concrete-epsilon-shape").hexdigest()
            concrete_production = hashlib.sha256(
                b"concrete-epsilon-production").hexdigest()
            concrete_instance = hashlib.sha256(
                b"concrete-epsilon-instance").hexdigest()
            generator.cfg_productions[concrete_production] = {
                "id": concrete_production,
                "shape_id": concrete_shape,
                "parser": "nullable-scc-fixture-v4",
                "lhs": "empty",
                "state": "epsilon",
                "rhs": [],
                "terminal_gaps": [hashlib.sha256(b"").hexdigest()],
                "schema": "symcc-parser-production-v2",
                "epsilon": True,
                "alternative": 0,
            }
            generator.ect_instances[concrete_instance] = {
                "instance_id": concrete_instance,
                "production_id": concrete_production,
                "shape_id": concrete_shape,
            }
            generator.subtree_rule_instances.setdefault(
                epsilon_rule.rule_id, set()).add(concrete_instance)
            generator._refresh_nullable_state()
            self.assertIn(
                concrete_shape,
                generator.subtree_rule_shapes[
                    epsilon_rule.rule_id],
            )
            self.assertTrue(
                generator.nullable_shape_ids <=
                generator.subtree_rule_shapes[
                    epsilon_rule.rule_id]
            )

    def test_synchronized_multislot_transaction_is_atomic_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "json.dump({"
                "'schema':'symcc-parser-structural-trace-v3',"
                "'parser':'sync-three-slot-fixture-v3','accepted':True,"
                "'roots':[0],'nodes':["
                "{'symbol':'tuple','state':'root','start':0,'end':len(data),"
                "'alternatives':[[1,2,3]]},"
                "{'symbol':'item','state':'s0','start':0,'end':1},"
                "{'symbol':'item','state':'s1','start':2,'end':3},"
                "{'symbol':'item','state':'s2','start':4,'end':5}"
                "]},open(sys.argv[2],'w'))"
            )
            source_content = b"a,c,e"
            second_content = b"b,d,f"
            source = os.path.join(tmp, "source")
            Path(source).write_bytes(source_content)
            state = os.path.join(tmp, "sync-state.json")
            generator = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            source_rule = generator._learn_rule(
                "literal", b"", source_content)
            assert source_rule is not None
            telemetry = SolverTelemetry(
                comparison_taints=((1, 9, 1, 0, 4, 1, 1),))
            cores = extract_constraint_cores(telemetry, len(source_content))
            spans = infer_token_spans(source_content, cores)
            self.assertEqual(
                (spans[0].lo, spans[0].hi), (0, len(source_content)))
            source_context = _grammar_context_id(
                source_content, spans[0])
            manager_root = os.path.join(tmp, "sync-proposals")
            manager = VerifiedProposalManager(
                "",
                manager_root,
                parser_command=(
                    sys.executable,
                    "-c",
                    parser_script,
                    "{input}",
                    "{trace}",
                ),
            )

            for content in (source_content, second_content):
                proposal_id = manager.ingest({
                    "kind": "solve_complete",
                    "source_path": source,
                    "candidate": {"hex": content.hex()},
                    "target_branch": 9,
                    "grammar_rule_id": source_rule.rule_id,
                    "grammar_context_id": source_context,
                    "grammar_source_context_id": source_context,
                    "grammar_span": [0, len(content)],
                })
                self.assertIsNotNone(proposal_id)
                assert proposal_id is not None
                self.assertTrue(manager.validate(
                    proposal_id,
                    SolverTelemetry(
                        target_branch=9, target_reached=True),
                    retcode=0,
                    killed=False,
                ))
                record = manager.records[proposal_id]
                self.assertTrue(generator.observe_grammar_validation(
                    source_rule.rule_id,
                    valid=True,
                    parser_valid=True,
                    context_id=record.parser_context_id,
                    source_context_id=source_context,
                    candidate_context_id=(
                        record.grammar_candidate_context_id),
                    production_id=record.parser_production_id,
                    cfg_fragment_json=record.parser_cfg_fragment_json,
                    cfg_fragment_sha256=(
                        record.parser_cfg_fragment_sha256),
                ))

            snapshot = generator.grammar_snapshot()
            self.assertGreaterEqual(snapshot["sync_transactions"], 3)
            self.assertGreaterEqual(snapshot["synchronized_rules"], 3)
            self.assertEqual(snapshot["sync_max_changed_slots"], 2)
            observed_parent_yields = {source_content, second_content}
            transactions = list(generator.sync_transactions.values())
            for transaction in transactions:
                result = bytes.fromhex(transaction["yield_hex"])
                self.assertNotIn(result, observed_parent_yields)
                self.assertEqual(transaction["changed_slots"], 2)
                self.assertEqual(len(transaction["slots"]), 3)
                self.assertEqual(sum(
                    assignment["changed"]
                    for assignment in transaction["slots"]), 2)
                self.assertTrue(all(
                    assignment["edge_id"] in generator.packed_edges
                    for assignment in transaction["slots"]
                ))
                self.assertEqual(result[1::2], b",,")

            generated = [
                candidate
                for candidate in generator._solve_complete_candidates(
                    source_content, cores, spans)
                if generator.grammar_rules[
                    candidate[3]].kind == "synchronized"
            ]
            self.assertGreaterEqual(len(generated), 3)
            for candidate in generated:
                changed = sum(
                    candidate[2][position] != source_content[position]
                    for position in (0, 2, 4)
                )
                self.assertEqual(changed, 2)
                self.assertEqual(candidate[2][1::2], b",,")
            wrong_span = type(spans[0])(
                spans[0].lo,
                spans[0].hi,
                spans[0].token,
                10,
            )
            self.assertFalse(any(
                generator.grammar_rules[
                    candidate[3]].kind == "synchronized"
                for candidate in generator._solve_complete_candidates(
                    source_content, cores, (wrong_span,))
            ))

            proposals = [
                proposal for proposal in generator.propose(
                    source, telemetry)
                if proposal.get("grammar_sync_transaction_id")
            ]
            self.assertGreaterEqual(len(proposals), 1)
            sync_proposal = proposals[0]
            transaction_id = sync_proposal[
                "grammar_sync_transaction_id"]
            self.assertIn(transaction_id, generator.sync_transactions)
            sync_proposal_id = manager.ingest(sync_proposal)
            self.assertIsNotNone(sync_proposal_id)
            assert sync_proposal_id is not None
            self.assertEqual(
                manager.records[
                    sync_proposal_id].grammar_sync_transaction_id,
                transaction_id,
            )
            self.assertTrue(manager.validate(
                sync_proposal_id,
                SolverTelemetry(target_branch=9, target_reached=True),
                retcode=0,
                killed=False,
            ))
            manager.save()
            restored_manager = VerifiedProposalManager("", manager_root)
            self.assertEqual(restored_manager.snapshot()["schema"], 15)
            self.assertEqual(
                restored_manager.records[
                    sync_proposal_id].grammar_sync_transaction_id,
                transaction_id,
            )
            self.assertIsNone(manager.ingest({
                **sync_proposal,
                "candidate": {"text": "novel"},
                "grammar_sync_transaction_id": "not-a-digest",
            }))

            generator.save()
            serialized = json.loads(Path(state).read_text())
            self.assertEqual(serialized["schema"], 25)
            serialized["sync_transactions"][0]["yield_hex"] = "00"
            Path(state).write_text(
                json.dumps(serialized), encoding="utf-8")
            restored = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            self.assertEqual(restored.SCHEMA, 25)
            self.assertGreaterEqual(
                restored.grammar_snapshot()["sync_transactions"], 3)
            self.assertFalse(any(
                transaction["yield_hex"] == "00"
                for transaction in restored.sync_transactions.values()
            ))
            self.assertTrue(any(
                restored.grammar_rules[
                    candidate[3]].kind == "synchronized"
                for candidate in restored._solve_complete_candidates(
                    source_content, cores, spans)
            ))

            victim = min(restored.sync_transactions.values(), key=lambda item: (
                item["transaction_id"],
            ))
            changed_target = next(
                assignment["target_instance_id"]
                for assignment in victim["slots"]
                if assignment["changed"]
            )
            victim_id = victim["transaction_id"]
            restored._drop_ect_instance(changed_target)
            self.assertNotIn(victim_id, restored.sync_transactions)

    def test_relation_aware_slot_transactions_preserve_exploration(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "parts=data.split(b'|');"
                "a=len(parts[0]);b=a+1+len(parts[1]);"
                "nodes=["
                "{'symbol':'record','state':'root','start':0,"
                "'end':len(data),'alternatives':[[1,2,3]]},"
                "{'symbol':'field','state':'s0','start':0,'end':a},"
                "{'symbol':'field','state':'s1','start':a+1,'end':b},"
                "{'symbol':'field','state':'s2','start':b+1,"
                "'end':len(data)}];"
                "json.dump({"
                "'schema':'symcc-parser-structural-trace-v3',"
                "'parser':'slot-relation-fixture-v3',"
                "'accepted':len(parts)==3,'roots':[0],'nodes':nodes"
                "},open(sys.argv[2],'w'))"
            )
            accepted = (
                b"A|A|tail",
                b"BB|BB|z",
                b"CCC|CCC|qq",
            )
            source = os.path.join(tmp, "source")
            state = os.path.join(tmp, "relation-state.json")
            Path(source).write_bytes(accepted[0])
            generator = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            source_rule = generator._learn_rule(
                "literal", b"", accepted[0])
            assert source_rule is not None
            telemetry = SolverTelemetry(
                comparison_taints=((
                    1, 9, 1, 0, len(accepted[0]) - 1, 1, 1),))
            spans = infer_token_spans(
                accepted[0],
                extract_constraint_cores(
                    telemetry, len(accepted[0])),
            )
            source_context = _grammar_context_id(
                accepted[0], spans[0])
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "relation-proposals"),
                parser_command=(
                    sys.executable,
                    "-c",
                    parser_script,
                    "{input}",
                    "{trace}",
                ),
            )

            def observe(content: bytes) -> None:
                proposal_id = manager.ingest({
                    "kind": "solve_complete",
                    "source_path": source,
                    "candidate": {"hex": content.hex()},
                    "target_branch": 9,
                    "grammar_rule_id": source_rule.rule_id,
                    "grammar_context_id": source_context,
                    "grammar_source_context_id": source_context,
                    "grammar_span": [0, len(content)],
                })
                self.assertIsNotNone(proposal_id)
                assert proposal_id is not None
                self.assertTrue(manager.validate(
                    proposal_id,
                    SolverTelemetry(
                        target_branch=9, target_reached=True),
                    retcode=0,
                    killed=False,
                ))
                record = manager.records[proposal_id]
                self.assertTrue(generator.observe_grammar_validation(
                    source_rule.rule_id,
                    valid=True,
                    parser_valid=True,
                    context_id=record.parser_context_id,
                    source_context_id=source_context,
                    candidate_context_id=(
                        record.grammar_candidate_context_id),
                    production_id=record.parser_production_id,
                    cfg_fragment_json=record.parser_cfg_fragment_json,
                    cfg_fragment_sha256=(
                        record.parser_cfg_fragment_sha256),
                ))

            observe(accepted[0])
            self.assertFalse(generator.slot_relations)
            observe(accepted[1])
            self.assertTrue(generator.slot_relations)
            observe(accepted[2])

            relations = list(generator.slot_relations.values())
            self.assertEqual(len(relations), 1)
            relation = relations[0]
            self.assertEqual(
                (
                    relation["kind"],
                    relation["left_slot"],
                    relation["right_slot"],
                    relation["support"],
                    relation["violations"],
                ),
                ("bytes_equal", 0, 1, 3, 0),
            )
            source_parent = next(
                instance
                for instance in generator.ect_instances.values()
                if (
                    instance.get("yield_hex") == accepted[0].hex() and
                    generator.cfg_productions.get(
                        str(instance.get("production_id", "")), {}).get(
                            "lhs") == "record"
                )
            )
            self.assertEqual(len(source_parent["child_edge_ids"]), 3)
            self.assertEqual(
                [
                    generator.packed_edges[edge_id]["slot"]
                    for edge_id in source_parent["child_edge_ids"]
                ],
                [0, 1, 2],
            )
            transactions = [
                transaction
                for transaction in generator.sync_transactions.values()
                if transaction["parent_instance_id"] ==
                source_parent["instance_id"]
            ]
            self.assertEqual(len(transactions), 4)
            conforming = [
                transaction for transaction in transactions
                if transaction["relation_matches"] ==
                transaction["relation_total"] == 1
            ]
            exploratory = [
                transaction for transaction in transactions
                if transaction["relation_exploration"]
            ]
            self.assertEqual(len(conforming), 3)
            self.assertEqual(len(exploratory), 1)
            self.assertEqual(exploratory[0]["relation_matches"], 0)
            self.assertTrue(all(
                len(transaction["slot_relations"]) == 1
                for transaction in transactions
            ))
            for transaction in conforming:
                fields = bytes.fromhex(
                    transaction["yield_hex"]).split(b"|")
                self.assertEqual(fields[0], fields[1])
            exploration_fields = bytes.fromhex(
                exploratory[0]["yield_hex"]).split(b"|")
            self.assertNotEqual(
                exploration_fields[0], exploration_fields[1])
            snapshot = generator.grammar_snapshot()
            self.assertEqual(snapshot["slot_relations"], 1)
            self.assertEqual(snapshot["slot_relation_support"], 3)
            self.assertGreaterEqual(
                snapshot["sync_relation_conforming"], 3)
            self.assertGreaterEqual(
                snapshot["sync_relation_exploration"], 1)

            generator.save()
            serialized = json.loads(Path(state).read_text())
            self.assertEqual(serialized["schema"], 25)

            tampered_binding = json.loads(json.dumps(serialized))
            persisted_parent = next(
                instance
                for instance in tampered_binding["ect_instances"]
                if instance["instance_id"] == source_parent["instance_id"]
            )
            persisted_parent["child_edge_ids"].reverse()
            tampered_binding_state = os.path.join(
                tmp, "tampered-edge-binding.json")
            Path(tampered_binding_state).write_text(
                json.dumps(tampered_binding))
            rejected_binding = SemanticProposalGenerator(
                tampered_binding_state,
                max_proposals_per_observation=64,
            )
            self.assertNotIn(
                source_parent["instance_id"],
                rejected_binding.ect_instances,
            )
            self.assertFalse(any(
                transaction["parent_instance_id"] ==
                source_parent["instance_id"]
                for transaction in
                rejected_binding.sync_transactions.values()
            ))

            missing_binding = json.loads(json.dumps(serialized))
            incomplete_parent = next(
                instance
                for instance in missing_binding["ect_instances"]
                if instance["instance_id"] == source_parent["instance_id"]
            )
            incomplete_parent["child_edge_ids"].pop()
            incomplete_binding_state = os.path.join(
                tmp, "incomplete-edge-binding.json")
            Path(incomplete_binding_state).write_text(
                json.dumps(missing_binding))
            rejected_incomplete = SemanticProposalGenerator(
                incomplete_binding_state,
                max_proposals_per_observation=64,
            )
            self.assertNotIn(
                source_parent["instance_id"],
                rejected_incomplete.ect_instances,
            )

            serialized["schema"] = 15
            for instance in serialized["ect_instances"]:
                instance.pop("child_edge_ids", None)
            serialized["slot_relations"][0]["kind"] = "length_equal"
            serialized["sync_transactions"][0][
                "relation_score"] = 99.0
            Path(state).write_text(json.dumps(serialized))
            restored = SemanticProposalGenerator(
                state, max_proposals_per_observation=64)
            self.assertEqual(restored.SCHEMA, 25)
            self.assertEqual(
                {relation["kind"]
                 for relation in restored.slot_relations.values()},
                {"bytes_equal"},
            )
            self.assertTrue(all(
                0.0 <= float(transaction["relation_score"]) <= 1.0
                for transaction in restored.sync_transactions.values()
            ))

            generator = restored
            observe(b"D|EEEE|www")
            self.assertFalse(generator.slot_relations)
            self.assertTrue(all(
                transaction["relation_total"] == 0
                for transaction in generator.sync_transactions.values()
            ))

    def test_pareto_rule_frontier_preserves_objective_extremes(self):
        generator = SemanticProposalGenerator(None, pareto_scheduling=True)
        edge_rule = generator._learn_rule("literal", b"", b"edge")
        data_rule = generator._learn_rule("literal", b"", b"data")
        dominated_rule = generator._learn_rule(
            "literal", b"", b"dominated")
        assert edge_rule is not None
        assert data_rule is not None
        assert dominated_rule is not None

        edge_rule.attempts = 10
        edge_rule.validations = 10
        edge_rule.verified = 8
        edge_rule.retained = 6
        edge_rule.coverage_features = 24
        edge_rule.data_observations = 10
        edge_rule.data_quality_sum = 1.0

        data_rule.attempts = 10
        data_rule.validations = 10
        data_rule.verified = 8
        data_rule.retained = 1
        data_rule.coverage_features = 1
        data_rule.data_observations = 10
        data_rule.data_quality_sum = 9.0

        dominated_rule.attempts = 10
        dominated_rule.validations = 10
        dominated_rule.verified = 2
        dominated_rule.retained = 0
        dominated_rule.data_observations = 10
        dominated_rule.data_quality_sum = 1.0
        dominated_rule.string_queries = 10

        ranked = generator._pareto_rank_rules(
            (dominated_rule, edge_rule, data_rule), "a" * 64)
        self.assertEqual(
            {ranked[0].rule_id, ranked[1].rule_id},
            {edge_rule.rule_id, data_rule.rule_id},
        )
        self.assertEqual(ranked[-1].rule_id, dominated_rule.rule_id)
        snapshot = generator.grammar_snapshot()
        self.assertEqual(snapshot["pareto_enabled"], 1)
        self.assertEqual(snapshot["pareto_rankings"], 1)
        self.assertEqual(snapshot["pareto_frontier_rules"], 2)

    def test_pareto_data_and_string_feedback_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "pareto-state.json")
            generator = SemanticProposalGenerator(state)
            rule = generator._learn_rule("literal", b"", b"value")
            assert rule is not None
            telemetry = SolverTelemetry.from_mapping({
                "data_features": [[7, 3, 4], [8, 1, 4]],
                "string_records_loaded": 2,
                "string_solver_queries": 3,
                "string_solver_verified": 2,
                "string_dual_view_verified": 1,
            })
            self.assertEqual(telemetry.string_solver_verified, 2)
            self.assertTrue(generator.observe_grammar_validation(
                rule.rule_id,
                valid=True,
                telemetry=telemetry,
            ))
            self.assertEqual(rule.data_observations, 1)
            self.assertAlmostEqual(rule.data_quality_sum, 0.5)
            self.assertEqual(
                (rule.string_queries, rule.string_verified), (3, 2))
            generator.save()

            restored = SemanticProposalGenerator(state)
            restored_rule = restored.grammar_rules[rule.rule_id]
            self.assertEqual(restored.SCHEMA, 25)
            self.assertEqual(restored_rule.data_observations, 1)
            self.assertAlmostEqual(restored_rule.data_quality_sum, 0.5)
            self.assertEqual(
                (
                    restored_rule.string_queries,
                    restored_rule.string_verified,
                ),
                (3, 2),
            )

    def test_targeted_transform_uses_ifss_relevance_slices(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "seed")
            Path(source).write_bytes(b"ABxxCDzz")
            telemetry = SolverTelemetry(
                target_branch=99,
                comparison_taints=(
                    (1, 99, 4, 0, 1, 1, 1),
                    (2, 99, 4, 4, 5, 1, 1),
                ),
            )
            slices = ifss_relevance_slices(telemetry, 8, target_branch=99)
            self.assertEqual(len(slices), 2)

            generator = SemanticProposalGenerator(
                None, max_proposals_per_observation=32)
            proposals = generator.propose(source, telemetry)
            transformed = [
                proposal for proposal in proposals
                if proposal["kind"] == "targeted_transform"
            ]
            self.assertTrue(transformed)
            self.assertTrue(all(
                proposal["target_branch"] == 99
                for proposal in transformed))
            candidates = {
                bytes.fromhex(proposal["candidate"]["hex"])
                for proposal in transformed
            }
            generators = {proposal["generator"] for proposal in transformed}
            self.assertIn("hydra-copy-core", generators)
            self.assertTrue(
                b"ABxxABzz" in candidates or b"CDxxCDzz" in candidates)

            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "proposals"))
            manager.ingest(transformed[0])
            self.assertTrue(any(
                record.kind == "targeted_transform"
                for record in manager.records.values()))

    def test_heap_partition_generates_split_and_merge_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "graph")
            entries = [
                SeedEntry(ROOT_ENTRY, 1, 0, (1,), b"root"),
                SeedEntry(
                    OBJECT_ENTRY, 1, 0, (1, POINTEE, 0), b"AAAA"),
                SeedEntry(
                    OBJECT_ENTRY, 1, 0, (1, POINTEE, 8), b""),
                SeedEntry(
                    OBJECT_ENTRY, 2, 0, (1, POINTEE, 16), b"BBBB"),
            ]
            Path(source).write_bytes(serialize_seed(entries))
            generator = SemanticProposalGenerator(
                None, max_proposals_per_observation=16)
            proposals = generator.propose(source, SolverTelemetry())
            heap = [
                bytes.fromhex(proposal["candidate"]["hex"])
                for proposal in proposals
                if proposal["kind"] == "heap_partition"
            ]
            self.assertEqual(len(heap), 2)
            object_counts = []
            for candidate in heap:
                parsed = parse_seed(candidate)
                object_counts.append(len({
                    entry.object_id for entry in parsed
                    if entry.flags & OBJECT_ENTRY
                }))
            self.assertEqual(sorted(object_counts), [1, 3])


if __name__ == "__main__":
    unittest.main()
