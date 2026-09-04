# RUN: python3 %s

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from verified_proposals import VerifiedProposalManager  # noqa: E402


class VerifiedProposalManagerTests(unittest.TestCase):
    def test_parser_cache_can_be_cost_faithfully_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "json.dump({'schema':'symcc-parser-structural-trace-v1',"
                "'parser':'cold-only-fixture','accepted':True,'nodes':["
                "{'symbol':'document','state':'root','start':0,"
                "'end':len(data),'parent':-1}]},open(sys.argv[2],'w'))"
            )
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "cold-only"),
                parser_command=(
                    sys.executable,
                    "-c",
                    script,
                    "{input}",
                    "{trace}",
                    "{cache}",
                ),
                parser_cache_enabled=False,
            )
            proposal_id = manager.ingest({
                "kind": "semantic",
                "candidate": {"text": "x"},
                "target_branch": 0,
            })
            self.assertIsNotNone(proposal_id)
            assert proposal_id is not None
            self.assertTrue(manager.validate(
                proposal_id, None, retcode=0, killed=False))
            record = manager.records[proposal_id]
            self.assertFalse(record.parser_cache_manifest_sha256)
            self.assertEqual(manager.snapshot()["parser_cache_requests"], 0)
            self.assertEqual(manager.snapshot()["parser_cache_enabled"], 0)
            self.assertEqual(manager.parser_cache_entries, {})

    def test_inconsistent_native_parser_telemetry_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "json.dump({'schema':'symcc-parser-structural-trace-v2',"
                "'parser':'bad-native-telemetry','accepted':True,'nodes':["
                "{'symbol':'document','state':'root','start':0,"
                "'end':len(data),'parent':-1,'epsilon':False}],"
                "'incremental_telemetry':{"
                "'schema':'symcc-tree-sitter-incremental-telemetry-v1',"
                "'mode':'incremental','elapsed_us':1,'nodes':1,"
                "'reused_node_ids':0,'cache_entries_before_store':0}},"
                "open(sys.argv[2],'w'))"
            )
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "bad-telemetry"),
                parser_command=(
                    sys.executable,
                    "-c",
                    script,
                    "{input}",
                    "{trace}",
                ),
            )
            proposal_id = manager.ingest({
                "kind": "semantic",
                "candidate": {"text": "x"},
                "target_branch": 0,
            })
            self.assertIsNotNone(proposal_id)
            assert proposal_id is not None
            self.assertFalse(manager.validate(
                proposal_id, None, retcode=0, killed=False))
            self.assertEqual(
                manager.records[proposal_id].last_reason,
                "parser-trace-invalid",
            )

    def test_independent_parser_oracle_is_required_after_target_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = (
                sys.executable,
                "-c",
                (
                    "import pathlib,sys;"
                    "raise SystemExit(0 if b'GOOD' in "
                    "pathlib.Path(sys.argv[1]).read_bytes() else 3)"
                ),
                "{input}",
            )
            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "parser"),
                parser_command=command,
                parser_timeout=1.0,
            )
            good = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "GOOD"},
                "target_branch": 0,
            })
            bad = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "BAD"},
                "target_branch": 0,
            })
            self.assertIsNotNone(good)
            self.assertIsNotNone(bad)
            self.assertTrue(manager.validate(
                str(good), None, retcode=0, killed=False))
            self.assertFalse(manager.validate(
                str(bad), None, retcode=0, killed=False))
            self.assertEqual(
                manager.records[str(good)].last_reason,
                "parser-accepted",
            )
            self.assertEqual(
                manager.records[str(bad)].last_reason,
                "parser-rejected-3",
            )
            self.assertEqual(manager.snapshot()["parser_validations"], 2)
            self.assertEqual(manager.snapshot()["parser_accepted"], 1)

    def test_versioned_parser_trace_extracts_and_persists_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "ok=b'GOOD' in data;"
                "json.dump({'schema':'symcc-parser-structural-trace-v1',"
                "'parser':'fixture-v1','accepted':ok,'nodes':["
                "{'symbol':'document','state':'root','start':0,'end':len(data),"
                "'parent':-1},{'symbol':'value','state':'string','start':0,"
                "'end':len(data),'parent':0}]},open(sys.argv[2],'w'));"
                "raise SystemExit(0 if ok else 3)"
            )
            root = os.path.join(tmp, "structured")
            manager = VerifiedProposalManager(
                "",
                root,
                parser_command=(
                    sys.executable, "-c", script, "{input}", "{trace}"),
            )
            context_id = "a" * 64
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "GOOD"},
                "target_branch": 0,
                "grammar_rule_id": "b" * 64,
                "grammar_context_id": context_id,
                "grammar_source_context_id": context_id,
                "grammar_span": [0, 4],
            })
            self.assertIsNotNone(proposal_id)
            self.assertTrue(manager.validate(
                str(proposal_id), None, retcode=0, killed=False))
            record = manager.records[str(proposal_id)]
            self.assertEqual(record.parser_symbol, "value")
            self.assertEqual(record.parser_state, "string")
            self.assertEqual(len(record.parser_context_id), 64)
            self.assertEqual(len(record.parser_trace_sha256), 64)
            self.assertEqual(record.parser_trace_nodes, 2)
            self.assertEqual(len(record.parser_production_id), 64)
            self.assertEqual(record.parser_production_lhs, "value")
            self.assertEqual(record.parser_recursive_depth, 0)
            fragment = json.loads(record.parser_cfg_fragment_json)
            self.assertEqual(
                fragment["schema"], "symcc-parser-cfg-fragment-v2")
            self.assertEqual(record.parser_ect_instances, 2)
            self.assertEqual(len(record.parser_ect_shape_id), 64)
            self.assertEqual(len(record.parser_ect_instance_id), 64)
            self.assertEqual(len(record.grammar_candidate_context_id), 64)
            self.assertEqual(
                sum(item["selected"] for item in fragment["instances"]), 1)
            self.assertEqual(
                manager.snapshot()["parser_structural_contexts"], 1)
            manager.save()

            restored = VerifiedProposalManager("", root)
            restored_record = restored.records[str(proposal_id)]
            self.assertEqual(
                restored_record.parser_context_id,
                record.parser_context_id,
            )
            self.assertEqual(restored.snapshot()["schema"], 15)
            self.assertEqual(
                restored_record.parser_ect_instance_id,
                record.parser_ect_instance_id,
            )

    def test_recursive_parser_production_induces_bounded_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "json.dump({'schema':'symcc-parser-structural-trace-v1',"
                "'parser':'recursive-fixture-v1','accepted':True,'nodes':["
                "{'symbol':'expr','state':'nested','start':0,'end':len(data),"
                "'parent':-1},{'symbol':'expr','state':'atom','start':1,"
                "'end':len(data)-1,'parent':0},{'symbol':'value',"
                "'state':'identifier','start':1,'end':len(data)-1,"
                "'parent':1}]},open(sys.argv[2],'w'))"
            )
            root = os.path.join(tmp, "recursive")
            manager = VerifiedProposalManager(
                "",
                root,
                parser_command=(
                    sys.executable, "-c", script, "{input}", "{trace}"),
            )
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "(GOOD)"},
                "target_branch": 0,
                "grammar_rule_id": "b" * 64,
                "grammar_context_id": "a" * 64,
                "grammar_source_context_id": "a" * 64,
                "grammar_span": [1, 5],
            })
            self.assertIsNotNone(proposal_id)
            self.assertTrue(manager.validate(
                str(proposal_id), None, retcode=0, killed=False))
            record = manager.records[str(proposal_id)]
            self.assertEqual(record.parser_symbol, "value")
            self.assertEqual(record.parser_production_lhs, "expr")
            self.assertEqual(record.parser_production_arity, 1)
            self.assertEqual(record.parser_recursive_depth, 2)
            self.assertEqual(record.parser_recursion_prefix_hex, "28")
            self.assertEqual(record.parser_recursion_suffix_hex, "29")
            self.assertEqual(
                manager.snapshot()["parser_recursive_productions"], 1)
            self.assertEqual(record.parser_cfg_cycles, 1)
            self.assertEqual(record.parser_cfg_productions, 3)
            self.assertEqual(len(record.parser_cfg_fragment_sha256), 64)
            manager.save()

            restored = VerifiedProposalManager("", root)
            restored_record = restored.records[str(proposal_id)]
            self.assertEqual(
                restored_record.parser_production_id,
                record.parser_production_id,
            )
            self.assertEqual(restored_record.parser_recursive_depth, 2)

    def test_multislot_cfg_fragment_preserves_both_recursive_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "json.dump({'schema':'symcc-parser-structural-trace-v1',"
                "'parser':'multislot-fixture-v1','accepted':True,'nodes':["
                "{'symbol':'expr','state':'binary','start':0,'end':len(data),"
                "'parent':-1},{'symbol':'expr','state':'left','start':0,"
                "'end':1,'parent':0},{'symbol':'expr','state':'right',"
                "'start':2,'end':3,'parent':0}]},open(sys.argv[2],'w'))"
            )
            root = os.path.join(tmp, "multislot")
            manager = VerifiedProposalManager(
                "",
                root,
                parser_command=(
                    sys.executable, "-c", script, "{input}", "{trace}"),
            )
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "x,x"},
                "target_branch": 0,
                "grammar_rule_id": "b" * 64,
                "grammar_context_id": "a" * 64,
                "grammar_source_context_id": "a" * 64,
                "grammar_span": [0, 1],
            })
            self.assertIsNotNone(proposal_id)
            self.assertTrue(manager.validate(
                str(proposal_id), None, retcode=0, killed=False))
            record = manager.records[str(proposal_id)]
            fragment = json.loads(record.parser_cfg_fragment_json)
            self.assertEqual(record.parser_cfg_cycles, 2)
            self.assertEqual(
                {cycle["slot"] for cycle in fragment["cycles"]}, {0, 1})
            self.assertTrue(all(
                cycle["kind"] == "direct"
                and cycle["recursive_slots"] == 2
                for cycle in fragment["cycles"]
            ))
            self.assertEqual(
                manager.snapshot()["parser_cfg_recursive_slots"], 4)

    def test_repeated_recursive_instances_deduplicate_cfg_productions(self):
        trace = {
            "schema": "symcc-parser-structural-trace-v1",
            "parser": "dedupe-fixture-v1",
            "accepted": True,
            "nodes": [
                {
                    "symbol": "expr", "state": "recursive",
                    "start": 0, "end": 7, "parent": -1,
                },
                {
                    "symbol": "expr", "state": "recursive",
                    "start": 1, "end": 6, "parent": 0,
                },
                {
                    "symbol": "expr", "state": "recursive",
                    "start": 2, "end": 5, "parent": 1,
                },
            ],
        }
        structural = VerifiedProposalManager._structural_context(
            trace, 2, 5, b"(((x)))")
        self.assertIsNotNone(structural)
        assert structural is not None
        fragment = json.loads(structural["cfg_fragment_json"])
        self.assertEqual(len(fragment["productions"]), 2)
        self.assertEqual(len(fragment["cycles"]), 2)
        self.assertEqual(
            len({item["id"] for item in fragment["productions"]}), 2)

    def test_large_ect_evidence_prunes_instances_without_losing_fragment(self):
        nodes = []
        for depth in range(32):
            nodes.append({
                "symbol": f"symbol-{depth:02d}-" + "s" * 100,
                "state": f"state-{depth:02d}-" + "t" * 100,
                "start": depth,
                "end": 64 - depth,
                "parent": depth - 1,
            })
        structural = VerifiedProposalManager._structural_context(
            {
                "parser": "large-ect-fixture-v1",
                "accepted": True,
                "nodes": nodes,
            },
            31,
            33,
            b"x" * 64,
        )
        self.assertIsNotNone(structural)
        assert structural is not None
        encoded = structural["cfg_fragment_json"].encode("utf-8")
        fragment = json.loads(encoded)
        self.assertLessEqual(len(encoded), 65536)
        self.assertLess(len(fragment["instances"]), 32)
        self.assertEqual(
            sum(item["selected"] for item in fragment["instances"]), 1)

    def test_parser_trace_is_fail_closed_on_exit_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "json.dump({'schema':'symcc-parser-structural-trace-v1',"
                "'parser':'fixture-v1','accepted':False,'nodes':["
                "{'symbol':'document','start':0,'end':len(data),"
                "'parent':-1}]},open(sys.argv[2],'w'))"
            )
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "invalid-trace"),
                parser_command=(
                    sys.executable, "-c", script, "{input}", "{trace}"),
            )
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "GOOD"},
                "target_branch": 0,
                "grammar_rule_id": "b" * 64,
                "grammar_context_id": "a" * 64,
                "grammar_span": [0, 4],
            })
            self.assertIsNotNone(proposal_id)
            self.assertFalse(manager.validate(
                str(proposal_id), None, retcode=0, killed=False))
            self.assertEqual(
                manager.records[str(proposal_id)].last_reason,
                "parser-trace-invalid",
            )

    def test_trace_v2_preserves_packed_alternatives_and_epsilon(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "json.dump({'schema':'symcc-parser-structural-trace-v2',"
                "'parser':'packed-epsilon-fixture-v2','accepted':True,"
                "'nodes':["
                "{'symbol':'document','state':'root','start':0,'end':1,"
                "'parent':-1},"
                "{'symbol':'choice','state':'ambiguous','start':0,'end':1,"
                "'parent':0,'alternatives':[[2],[3]]},"
                "{'symbol':'empty','state':'epsilon','start':0,'end':0,"
                "'parent':1,'epsilon':True},"
                "{'symbol':'token','state':'atom','start':0,'end':1,"
                "'parent':1}]},open(sys.argv[2],'w'))"
            )
            root = os.path.join(tmp, "packed-epsilon")
            manager = VerifiedProposalManager(
                "",
                root,
                parser_command=(
                    sys.executable, "-c", script, "{input}", "{trace}"),
            )
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "x"},
                "target_branch": 9,
                "grammar_rule_id": "b" * 64,
                "grammar_context_id": "a" * 64,
                "grammar_source_context_id": "a" * 64,
                "grammar_span": [0, 0],
            })
            self.assertIsNotNone(proposal_id)
            self.assertTrue(manager.validate(
                str(proposal_id),
                SimpleNamespace(target_branch=9, target_reached=True),
                retcode=0,
                killed=False,
            ))
            record = manager.records[str(proposal_id)]
            fragment = json.loads(record.parser_cfg_fragment_json)
            self.assertEqual(
                fragment["schema"], "symcc-parser-cfg-fragment-v3")
            self.assertEqual(record.parser_symbol, "empty")
            self.assertEqual(record.parser_cfg_productions, 4)
            self.assertEqual(record.parser_cfg_alternatives, 1)
            self.assertEqual(record.parser_epsilon_productions, 1)
            epsilon = next(
                production for production in fragment["productions"]
                if production["epsilon"])
            self.assertEqual(epsilon["rhs"], [])
            self.assertEqual(epsilon["alternative"], 0)
            selected = next(
                instance for instance in fragment["instances"]
                if instance["selected"])
            self.assertEqual(selected["production_id"], epsilon["id"])
            self.assertEqual(selected["yield_hex"], "")
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["parser_cfg_alternatives"], 1)
            self.assertEqual(snapshot["parser_epsilon_productions"], 1)
            manager.save()

            restored = VerifiedProposalManager("", root)
            restored_record = restored.records[str(proposal_id)]
            self.assertEqual(restored_record.parser_cfg_alternatives, 1)
            self.assertEqual(
                restored_record.parser_epsilon_productions, 1)

    def test_trace_v2_rejects_incomplete_packed_child_partition(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import json,pathlib,sys;"
                "json.dump({'schema':'symcc-parser-structural-trace-v2',"
                "'parser':'invalid-packed-fixture-v2','accepted':True,"
                "'nodes':["
                "{'symbol':'choice','start':0,'end':1,'parent':-1,"
                "'alternatives':[[1]]},"
                "{'symbol':'left','start':0,'end':0,'parent':0,"
                "'epsilon':True},"
                "{'symbol':'right','start':0,'end':1,'parent':0}]},"
                "open(sys.argv[2],'w'))"
            )
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "invalid-packed"),
                parser_command=(
                    sys.executable, "-c", script, "{input}", "{trace}"),
            )
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "x"},
                "target_branch": 0,
                "grammar_rule_id": "b" * 64,
                "grammar_context_id": "a" * 64,
                "grammar_span": [0, 0],
            })
            self.assertIsNotNone(proposal_id)
            self.assertFalse(manager.validate(
                str(proposal_id), None, retcode=0, killed=False))
            self.assertEqual(
                manager.records[str(proposal_id)].last_reason,
                "parser-trace-invalid",
            )

    def test_packed_production_families_are_deterministically_bounded(self):
        nodes = [
            {
                "symbol": f"chain-{depth}",
                "state": f"state-{depth}",
                "start": 0,
                "end": 1,
                "parent": depth - 1,
                "epsilon": False,
                "alternatives": [],
            }
            for depth in range(16)
        ]
        for depth in range(16):
            children = ([depth + 1] if depth < 15 else [])
            leaf_count = 7 if depth < 15 else 8
            for leaf in range(leaf_count):
                children.append(len(nodes))
                nodes.append({
                    "symbol": f"leaf-{depth}-{leaf}",
                    "state": "atom",
                    "start": 0,
                    "end": 1,
                    "parent": depth,
                    "epsilon": False,
                    "alternatives": [],
                })
            nodes[depth]["alternatives"] = [
                [child] for child in children
            ]
        structural = VerifiedProposalManager._structural_context(
            {
                "schema": "symcc-parser-structural-trace-v2",
                "parser": "bounded-packed-fixture-v2",
                "accepted": True,
                "nodes": nodes,
            },
            0,
            1,
            b"x",
        )
        self.assertIsNotNone(structural)
        assert structural is not None
        fragment = json.loads(structural["cfg_fragment_json"])
        self.assertEqual(len(fragment["productions"]), 32)
        self.assertEqual(structural["cfg_alternatives"], 15)
        self.assertLessEqual(len(fragment["instances"]), 32)
        self.assertEqual(
            len({item["id"] for item in fragment["productions"]}), 32)

    def test_trace_v3_extracts_shared_dag_and_deep_alternative_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
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
            root = os.path.join(tmp, "shared-packed")
            manager = VerifiedProposalManager(
                "",
                root,
                parser_command=(
                    sys.executable, "-c", script, "{input}", "{trace}"),
            )
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "a,b"},
                "target_branch": 0,
                "grammar_rule_id": "b" * 64,
                "grammar_context_id": "a" * 64,
                "grammar_span": [0, 1],
            })
            self.assertIsNotNone(proposal_id)
            self.assertTrue(manager.validate(
                str(proposal_id), None, retcode=0, killed=False))
            record = manager.records[str(proposal_id)]
            fragment = json.loads(record.parser_cfg_fragment_json)
            self.assertEqual(
                fragment["schema"], "symcc-parser-cfg-fragment-v4")
            self.assertEqual(record.parser_packed_nodes, 5)
            self.assertEqual(record.parser_packed_edges, 5)
            self.assertFalse(fragment["truncated"])
            self.assertEqual(len(fragment["selected_path"]), 2)
            incoming = {}
            for edge in fragment["packed_edges"]:
                incoming[edge["child_id"]] = (
                    incoming.get(edge["child_id"], 0) + 1)
            self.assertIn(2, incoming.values())
            deep_value = next(
                instance for instance in fragment["instances"]
                if (
                    not instance["selected"] and
                    len(instance["node_path"]) == 3 and
                    instance["yield_hex"] == "62"
                )
            )
            self.assertEqual(
                deep_value["schema"],
                "symcc-parser-subtree-instance-v2",
            )
            self.assertEqual(
                fragment["selected_path"],
                next(
                    instance["node_path"]
                    for instance in fragment["instances"]
                    if instance["selected"]
                ),
            )
            manager.save()
            restored = VerifiedProposalManager("", root)
            self.assertEqual(
                restored.records[str(proposal_id)].parser_packed_edges, 5)

    def test_trace_v3_rejects_shared_node_in_selected_primary_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import json,sys;"
                "json.dump({'schema':'symcc-parser-structural-trace-v3',"
                "'parser':'invalid-primary-dag-v3','accepted':True,"
                "'roots':[0],'nodes':["
                "{'symbol':'root','start':0,'end':1,"
                "'alternatives':[[1,2]]},"
                "{'symbol':'left','start':0,'end':1,"
                "'alternatives':[[3]]},"
                "{'symbol':'right','start':0,'end':1,"
                "'alternatives':[[3]]},"
                "{'symbol':'shared','start':0,'end':1}"
                "]},open(sys.argv[2],'w'))"
            )
            manager = VerifiedProposalManager(
                "",
                os.path.join(tmp, "invalid-primary-dag"),
                parser_command=(
                    sys.executable, "-c", script, "{input}", "{trace}"),
            )
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "x"},
                "target_branch": 0,
                "grammar_rule_id": "b" * 64,
                "grammar_context_id": "a" * 64,
                "grammar_span": [0, 1],
            })
            self.assertIsNotNone(proposal_id)
            self.assertFalse(manager.validate(
                str(proposal_id), None, retcode=0, killed=False))
            self.assertEqual(
                manager.records[str(proposal_id)].last_reason,
                "parser-trace-invalid",
            )

    def test_trace_v4_certifies_nullable_scc_fixed_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
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
            root = os.path.join(tmp, "nullable-scc")
            manager = VerifiedProposalManager(
                "",
                root,
                parser_command=(
                    sys.executable, "-c", script, "{input}", "{trace}"),
            )
            proposal_id = manager.ingest({
                "kind": "solve_complete",
                "candidate": {"text": "x"},
                "target_branch": 0,
                "grammar_rule_id": "b" * 64,
                "grammar_context_id": "a" * 64,
                "grammar_span": [0, 1],
            })
            self.assertIsNotNone(proposal_id)
            self.assertTrue(manager.validate(
                str(proposal_id), None, retcode=0, killed=False))
            record = manager.records[str(proposal_id)]
            fragment = json.loads(record.parser_cfg_fragment_json)
            self.assertEqual(
                fragment["schema"], "symcc-parser-cfg-fragment-v5")
            self.assertEqual(record.parser_nullable_rules, 3)
            self.assertEqual(record.parser_nullable_proofs, 2)
            self.assertEqual(record.parser_nullable_sccs, 1)
            self.assertEqual(
                {
                    (proof["symbol"], proof["state"]): proof["depth"]
                    for proof in fragment["nullable_proofs"]
                },
                {("A", "q"): 1, ("B", "q"): 0},
            )
            self.assertTrue(all(
                proof["scc_id"] == fragment["nullable_sccs"][0]["scc_id"]
                for proof in fragment["nullable_proofs"]
            ))
            manager.save()
            restored = VerifiedProposalManager("", root)
            self.assertEqual(restored.snapshot()["schema"], 15)
            self.assertEqual(
                restored.records[
                    str(proposal_id)].parser_nullable_proofs,
                2,
            )

        pure_cycle = VerifiedProposalManager._nullable_certificate(
            "pure-cycle-fixture-v4",
            [
                {"lhs": ["A", "q"], "rhs": [["B", "q"]]},
                {"lhs": ["B", "q"], "rhs": [["A", "q"]]},
            ],
        )
        self.assertIsNotNone(pure_cycle)
        assert pure_cycle is not None
        self.assertEqual(pure_cycle["nullable_proofs"], [])
        self.assertEqual(len(pure_cycle["nullable_sccs"]), 1)

    def test_incremental_parser_cache_reuse_receipt_is_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import hashlib,json,pathlib,sys;"
                "data=pathlib.Path(sys.argv[1]).read_bytes();"
                "cache_path=pathlib.Path(sys.argv[3]);"
                "cache=json.loads(cache_path.read_text());"
                "trace={'schema':'symcc-parser-structural-trace-v3',"
                "'parser':'incremental-cache-fixture-v1','accepted':True,"
                "'roots':[0],'nodes':["
                "{'symbol':'root','state':'document','start':0,"
                "'end':len(data),'alternatives':[[1,2,3]]},"
                "{'symbol':'left','state':'stable','start':0,'end':8},"
                "{'symbol':'middle','state':'edited','start':24,'end':40},"
                "{'symbol':'right','state':'stable','start':56,'end':64}]};"
                "receipt=({'schema':'symcc-parser-incremental-reuse-v1',"
                "'manifest_sha256':hashlib.sha256("
                "cache_path.read_bytes()).hexdigest(),"
                "'reused_nodes':[{'base_index':item['base_index'],"
                "'candidate_index':item['base_index']} for item in "
                "cache['reusable_nodes']]} if "
                "cache['mode']=='incremental' else None);"
                "trace.update({'incremental_cache':receipt} "
                "if receipt is not None else {});"
                "trace.get('incremental_cache',{}).update("
                "{'manifest_sha256':'0'*64} if data[31]==33 else {});"
                "json.dump(trace,open(sys.argv[2],'w'))"
            )
            root = os.path.join(tmp, "incremental")
            source = os.path.join(tmp, "source")
            base = (
                b"L" * 8 + b"." * 16 + b"M" * 16 +
                b"." * 16 + b"R" * 8
            )
            self.assertEqual(len(base), 64)
            Path(source).write_bytes(base)
            command = (
                sys.executable,
                "-c",
                script,
                "{input}",
                "{trace}",
                "{cache}",
            )
            manager = VerifiedProposalManager(
                "", root, parser_command=command)

            def candidate(position, value):
                content = bytearray(base)
                content[position] = value
                return bytes(content)

            base_id = manager.ingest({
                "kind": "semantic",
                "source_path": source,
                "candidate": {"hex": base.hex()},
                "target_branch": 0,
            })
            self.assertIsNotNone(base_id)
            assert base_id is not None
            self.assertTrue(manager.validate(
                base_id, None, retcode=0, killed=False))
            base_record = manager.records[base_id]
            self.assertTrue(base_record.parser_cache_manifest_sha256)
            self.assertFalse(base_record.parser_cache_base_sha256)
            self.assertFalse(base_record.parser_cache_hit)
            self.assertEqual(len(manager.parser_cache_entries), 1)

            edited = candidate(32, ord("X"))
            edited_id = manager.ingest({
                "kind": "semantic",
                "source_path": source,
                "candidate": {"hex": edited.hex()},
                "target_branch": 0,
            })
            self.assertIsNotNone(edited_id)
            assert edited_id is not None
            self.assertTrue(manager.validate(
                edited_id, None, retcode=0, killed=False))
            edited_record = manager.records[edited_id]
            self.assertEqual(
                edited_record.parser_cache_base_sha256,
                hashlib.sha256(base).hexdigest(),
            )
            self.assertTrue(edited_record.parser_cache_hit)
            self.assertEqual(edited_record.parser_cache_reused_nodes, 2)
            self.assertEqual(edited_record.parser_cache_invalidated_nodes, 2)
            self.assertEqual(manager.snapshot()["parser_cache_hits"], 1)
            manager.save()

            restored = VerifiedProposalManager(
                "", root, parser_command=command)
            self.assertEqual(restored.snapshot()["schema"], 15)
            self.assertGreaterEqual(
                restored.snapshot()["parser_cache_entries"], 2)
            restarted = candidate(33, ord("Y"))
            restarted_id = restored.ingest({
                "kind": "semantic",
                "source_path": source,
                "candidate": {"hex": restarted.hex()},
                "target_branch": 0,
            })
            self.assertIsNotNone(restarted_id)
            assert restarted_id is not None
            self.assertTrue(restored.validate(
                restarted_id, None, retcode=0, killed=False))
            self.assertTrue(
                restored.records[restarted_id].parser_cache_hit)
            self.assertEqual(
                restored.records[
                    restarted_id].parser_cache_reused_nodes,
                2,
            )

            bad_receipt = candidate(31, 33)
            bad_id = restored.ingest({
                "kind": "semantic",
                "source_path": source,
                "candidate": {"hex": bad_receipt.hex()},
                "target_branch": 0,
            })
            self.assertIsNotNone(bad_id)
            assert bad_id is not None
            self.assertFalse(restored.validate(
                bad_id, None, retcode=0, killed=False))
            self.assertEqual(
                restored.records[bad_id].last_reason,
                "parser-trace-invalid",
            )

            base_digest = hashlib.sha256(base).hexdigest()
            base_cache_path = restored.parser_cache_entries[
                base_digest]["trace_path"]
            Path(base_cache_path).write_text("{}", encoding="utf-8")
            cold_fallback = candidate(34, ord("Z"))
            cold_id = restored.ingest({
                "kind": "semantic",
                "source_path": source,
                "candidate": {"hex": cold_fallback.hex()},
                "target_branch": 0,
            })
            self.assertIsNotNone(cold_id)
            assert cold_id is not None
            self.assertTrue(restored.validate(
                cold_id, None, retcode=0, killed=False))
            cold_record = restored.records[cold_id]
            self.assertFalse(cold_record.parser_cache_base_sha256)
            self.assertFalse(cold_record.parser_cache_hit)
            self.assertNotIn(
                base_digest, restored.parser_cache_entries)

    def test_query_hole_certificate_is_strict_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "state")
            rule_id = "b" * 64
            certificate = {
                "schema": "symcc-query-grammar-hole-v1",
                "query_id": "a" * 64,
                "source_manifest_sha256": "c" * 64,
                "hole": [2, 3],
                "grammar_rule_id": rule_id,
                "query_ir_verified": True,
                "target_replay_required": True,
                "coverage_retention_required": True,
            }
            raw = {
                "kind": "query_hole_completion",
                "candidate": {"hex": "7b7d"},
                "target_branch": 7,
                "grammar_rule_id": rule_id,
                "query_hole_certificate": certificate,
            }
            manager = VerifiedProposalManager("", root)
            proposal_id = manager.ingest(raw)
            self.assertIsNotNone(proposal_id)
            record = manager.records[str(proposal_id)]
            self.assertEqual(record.query_id, "a" * 64)
            self.assertTrue(record.query_ir_verified)
            self.assertEqual(
                (record.query_hole_lo, record.query_hole_hi), (2, 3))
            manager.save()
            restored = VerifiedProposalManager("", root)
            self.assertEqual(
                restored.records[str(proposal_id)].query_id, "a" * 64)
            self.assertEqual(
                restored.snapshot()["query_hole_query_ir_verified"], 1)

            invalid_root = os.path.join(tmp, "invalid")
            invalid = VerifiedProposalManager("", invalid_root)
            tampered = dict(raw)
            tampered["query_hole_certificate"] = dict(certificate)
            tampered["query_hole_certificate"]["grammar_rule_id"] = "d" * 64
            self.assertIsNone(invalid.ingest(tampered))
            self.assertEqual(invalid.rejected_inputs, 1)
            self.assertEqual(
                list((Path(invalid_root) / "candidates").iterdir()), [])

    def test_patch_candidate_requires_target_validation_before_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "seed")
            Path(source).write_bytes(b"AAAA")
            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "proposals"))
            proposal_id = manager.ingest({
                "kind": "inverse",
                "source_path": source,
                "target_branch": 99,
                "patches": [{"offset": 1, "hex": "4243"}],
                "append": {"text": "!"},
            }, now=10.0)
            self.assertIsNotNone(proposal_id)
            record = manager.records[proposal_id]
            self.assertEqual(Path(record.candidate_path).read_bytes(), b"ABCA!")

            pending = manager.claim_pending(1, now=10.0)
            self.assertEqual(pending[0].proposal_id, proposal_id)
            manager.mark_dispatched(proposal_id)
            self.assertFalse(manager.validate(
                proposal_id,
                SimpleNamespace(target_branch=99, target_reached=False),
                retcode=0, killed=False))

            manager.records[proposal_id].updated = 0.0
            manager.records[proposal_id].status = "retry"
            self.assertEqual(len(manager.claim_pending(1, now=100.0)), 1)
            manager.mark_dispatched(proposal_id)
            self.assertTrue(manager.validate(
                proposal_id,
                SimpleNamespace(target_branch=99, target_reached=True),
                retcode=0, killed=False))
            manager.record_retention(proposal_id, 3)
            self.assertEqual(manager.records[proposal_id].status, "retained")
            self.assertEqual(manager.snapshot()["coverage_features"], 3)

    def test_dispatch_abandon_distinguishes_unsent_and_lost_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "seed")
            Path(source).write_bytes(b"AAAA")
            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "proposals"), max_attempts=2)
            proposal_id = manager.ingest({
                "kind": "inverse",
                "source_path": source,
                "target_branch": 99,
                "patches": [{"offset": 0, "hex": "42"}],
            }, now=10.0)
            self.assertIsNotNone(proposal_id)

            manager.claim_pending(1, now=10.0)
            manager.mark_dispatched(proposal_id)
            self.assertTrue(manager.abandon_dispatch(
                proposal_id, executed=False, now=11.0))
            record = manager.records[proposal_id]
            self.assertEqual(record.status, "pending")
            self.assertEqual(record.attempts, 0)
            self.assertEqual(record.last_reason, "dispatch-not-sent")

            manager.claim_pending(1, now=12.0)
            manager.mark_dispatched(proposal_id)
            self.assertTrue(manager.abandon_dispatch(
                proposal_id, executed=True, now=13.0))
            self.assertEqual(record.status, "retry")
            self.assertEqual(record.attempts, 1)
            self.assertEqual(record.last_reason, "dispatch-result-lost")

    def test_jsonl_scan_deduplicates_and_state_recovers_inflight_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            proposals = os.path.join(tmp, "proposals.jsonl")
            raw = {
                "id": "a" * 64,
                "kind": "solve_complete",
                "candidate": {"hex": "7b7d"},
                "target_branch": 7,
            }
            Path(proposals).write_text(
                json.dumps(raw) + "\n" + json.dumps(raw) + "\n")
            root = os.path.join(tmp, "state")
            manager = VerifiedProposalManager(proposals, root)
            self.assertEqual(manager.scan(), 1)
            self.assertEqual(manager.scan(), 0)
            record = manager.claim_pending(1)[0]
            manager.mark_dispatched(record.proposal_id)
            manager.save()

            restored = VerifiedProposalManager(proposals, root)
            restored_record = restored.records[record.proposal_id]
            self.assertEqual(restored_record.status, "retry")
            self.assertEqual(
                Path(restored_record.candidate_path).read_bytes(), b"{}")
if __name__ == "__main__":
    unittest.main()
