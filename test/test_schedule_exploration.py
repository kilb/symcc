# RUN: python3 %s

import ctypes
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from schedule_exploration import (  # noqa: E402
    BoundedWakeupTree,
    CONDPOR_GRAPH_SCHEMA,
    DporScheduleExplorer,
    JOINT_PATH_SCHEDULE_SCHEMA,
    NATIVE_CONDPOR_MEMORY_GRAPH_SCHEMA,
    ScheduleEvent,
    SCHEDULE_CONSTRAINT_SCHEMA,
    SCHEDULE_SMT_SCHEMA,
    SOURCE_DPOR_SCHEMA,
    WAKEUP_TREE_SCHEMA,
    append_schedule_constraint_artifact,
    append_schedule_smt_artifact,
    annotate_schedule,
    classify_schedule_conflicts,
    condpor_execution_graph_certificate,
    happens_before,
    materialize_schedule_smt_query,
    native_condpor_memory_graph_certificate,
    normalize_schedule_prefix,
    operational_enabledness_certificate,
    parse_schedule_trace,
    prepend_ld_preload,
    propose_source_replay_prefixes,
    runtime_ready_evidence,
    schedule_constraint_artifact,
    schedule_linear_extension_certificate,
    schedule_smt_artifact,
    solve_schedule_smt_query,
    solve_joint_path_schedule_query,
    source_dpor_certificate,
    wakeup_tree_certificate,
    verify_schedule_linear_extension_certificate,
    verify_joint_path_schedule_result,
    verify_native_condpor_memory_graph_certificate,
    verify_operational_enabledness_certificate,
    verify_condpor_execution_graph_certificate,
    verify_source_dpor_certificate,
    verify_wakeup_tree_certificate,
    write_schedule_linear_extension_prefix,
    write_schedule_prefix,
)


def evaluate_with_system_z3(smt2):
    z3_path = ctypes.util.find_library("z3")
    if not z3_path:
        return None
    z3 = ctypes.CDLL(z3_path)
    z3.Z3_mk_config.restype = ctypes.c_void_p
    z3.Z3_mk_context.argtypes = [ctypes.c_void_p]
    z3.Z3_mk_context.restype = ctypes.c_void_p
    z3.Z3_eval_smtlib2_string.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
    ]
    z3.Z3_eval_smtlib2_string.restype = ctypes.c_char_p
    z3.Z3_del_context.argtypes = [ctypes.c_void_p]
    z3.Z3_del_config.argtypes = [ctypes.c_void_p]
    config = z3.Z3_mk_config()
    context = z3.Z3_mk_context(config)
    try:
        result = z3.Z3_eval_smtlib2_string(
            context,
            smt2.encode("utf-8"),
        )
        return result.decode("utf-8")
    finally:
        z3.Z3_del_context(context)
        z3.Z3_del_config(config)


class ScheduleExplorationTests(unittest.TestCase):
    def test_trace_parser_keeps_controlled_events_in_order(self):
        trace = """
        # comment
        2 1 unlock 0xaaa
        0 1 lock 0xaaa
        1 2 lock 0xaaa
        malformed
        """
        events = parse_schedule_trace(trace)
        self.assertEqual([event.seq for event in events], [0, 1, 2])
        self.assertEqual(
            [(event.tid, event.op) for event in events if event.controlled],
            [(1, "lock"), (2, "lock")],
        )

    def test_trace_parser_resolves_cmpxchg_result(self):
        failed = parse_schedule_trace(
            "0 1 rmw 0x100 atomic=1 kind=cmpxchg group=7 "
            "bytes=4 mo=seq_cst failure-mo=acquire\n"
            "1 1 atomic_result 0x100 group=7 success=0\n"
        )
        self.assertEqual(failed[0].op, "read")
        self.assertFalse(failed[0].write)
        self.assertIn("success=0", failed[0].tags)

        succeeded = parse_schedule_trace(
            "0 1 rmw 0x100 atomic=1 kind=cmpxchg group=8 "
            "bytes=4 mo=seq_cst failure-mo=acquire\n"
            "1 1 atomic_result 0x100 group=8 success=1\n"
        )
        self.assertEqual(succeeded[0].op, "rmw")
        self.assertTrue(succeeded[0].write)

    def test_trace_parser_attaches_bounded_atomic_values(self):
        events = parse_schedule_trace(
            "0 1 write 0x100 atomic=1 kind=store group=7 bytes=4 mo=release\n"
            "1 1 atomic_value 0x100 group=7 role=write bits=32 value=0x2a\n"
            "2 2 read 0x100 atomic=1 kind=load group=8 bytes=4 mo=acquire\n"
            "3 2 atomic_value 0x100 group=8 role=read bits=32 value=0x2a\n"
            "4 2 rmw 0x100 atomic=1 kind=cmpxchg group=9 bytes=4 mo=seq_cst\n"
            "5 2 atomic_value 0x100 group=9 role=expected bits=32 value=0x2a\n"
            "6 2 atomic_value 0x100 group=9 role=desired bits=32 value=0x2b\n"
            "7 2 atomic_value 0x100 group=9 role=read bits=32 value=0x2a\n"
            "8 2 atomic_result 0x100 group=9 success=1\n"
        )

        store, load, compare = events[0], events[2], events[4]
        self.assertIn("value=0x2a", store.tags)
        self.assertIn("value-bits=32", store.tags)
        self.assertIn("value=0x2a", load.tags)
        self.assertIn("read-value=0x2a", compare.tags)
        self.assertIn("expected-value=0x2a", compare.tags)
        self.assertIn("desired-value=0x2b", compare.tags)
        self.assertNotIn("value=0x2a", compare.tags)

    def test_trace_parser_rejects_ambiguous_or_unbound_atomic_values(self):
        ambiguous = parse_schedule_trace(
            "0 1 read 0x100 atomic=1 kind=load group=7 bytes=1 mo=acquire\n"
            "1 1 atomic_value 0x100 group=7 role=read bits=8 value=0x2a\n"
            "2 1 atomic_value 0x100 group=7 role=read bits=8 value=0x2b\n"
        )
        self.assertNotIn("value=0x2a", ambiguous[0].tags)
        self.assertNotIn("value=0x2b", ambiguous[0].tags)

        unbound = parse_schedule_trace(
            "0 1 write 0x100 atomic=1 kind=store group=8 bytes=1 mo=release\n"
            "1 2 atomic_value 0x100 group=8 role=write bits=8 value=0x2a\n"
            "2 1 atomic_value 0x101 group=9 role=write bits=8 value=0x2a\n"
            "3 1 atomic_value 0x100 group=8 role=read bits=8 value=0x100\n"
        )
        self.assertFalse(any(tag.startswith("value=") for tag in unbound[0].tags))

    def test_trace_parser_binds_unique_atomic_commit_evidence(self):
        events = parse_schedule_trace(
            "0 1 read 0x100 atomic=1 kind=load group=7 bytes=4 mo=seq_cst\n"
            "1 1 atomic_commit 0x100 group=7 mode=1 advanced=1 "
            "mismatch=0 prefix-index=2\n"
        )
        self.assertIn("commit-mode=1", events[0].tags)
        self.assertIn("commit-advanced=1", events[0].tags)
        self.assertIn("commit-mismatch=0", events[0].tags)
        self.assertIn("commit-prefix-index=2", events[0].tags)

        ambiguous = parse_schedule_trace(
            "0 1 write 0x100 atomic=1 kind=store group=8 bytes=4 mo=seq_cst\n"
            "1 1 atomic_commit 0x100 group=8 mode=1 advanced=1 "
            "mismatch=0 prefix-index=0\n"
            "2 1 atomic_commit 0x100 group=8 mode=1 advanced=0 "
            "mismatch=1 prefix-index=0\n"
        )
        self.assertFalse(any(
            tag.startswith("commit-") for tag in ambiguous[0].tags
        ))

    def test_bounded_dpor_generates_conflicting_mutex_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "dpor.json")
            explorer = DporScheduleExplorer(state, max_depth=8)
            added = explorer.observe(
                os.path.join(tmp, "seed"),
                "0 1 lock 0xbeef\n1 2 lock 0xbeef\n2 1 unlock 0xbeef\n",
            )
            self.assertEqual(added, 1)
            jobs = explorer.pop_pending(4)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].prefix, (2,))

            reloaded = DporScheduleExplorer(state, max_depth=8)
            self.assertEqual(reloaded.pending_count(), 0)

    def test_independent_objects_do_not_generate_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = DporScheduleExplorer(os.path.join(tmp, "dpor.json"))
            added = explorer.observe(
                os.path.join(tmp, "seed"),
                "0 1 lock 0x1\n1 2 lock 0x2\n",
            )
            self.assertEqual(added, 0)

    def test_happens_before_tracks_lock_release_acquire(self):
        events = parse_schedule_trace(
            "0 1 lock L\n"
            "1 1 unlock L\n"
            "2 2 lock L\n"
            "3 2 unlock L\n"
        )
        points = annotate_schedule(events)
        by_seq = {point.event.seq: point for point in points}
        self.assertTrue(happens_before(by_seq[1], by_seq[2]))

    def test_memory_conflict_generates_source_backtrack_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "dpor.json")
            explorer = DporScheduleExplorer(state, max_depth=8)
            trace = "0 1 write 0x100\n1 2 read 0x100\n"
            conflicts = classify_schedule_conflicts(parse_schedule_trace(trace))
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].kind, "memory")
            self.assertEqual(explorer.observe(os.path.join(tmp, "seed"), trace), 1)
            self.assertEqual(explorer.pop_pending(1)[0].prefix, (2,))

    def test_trace_parser_preserves_memory_provenance_tags(self):
        events = parse_schedule_trace(
            "0 1 write 0x100 prov=module mod=main\n"
            "1 2 read 0x100 prov=heap\n"
        )
        self.assertEqual(events[0].tags, ("prov=module", "mod=main"))
        conflicts = classify_schedule_conflicts(events)
        self.assertEqual(len(conflicts), 1)
        artifact = schedule_constraint_artifact(events, input_id="seed")
        self.assertEqual(artifact["provenance_counts"], {
            "heap": 1,
            "module": 1,
        })
        self.assertEqual(artifact["events"][0]["tags"],
                         ["prov=module", "mod=main"])

    def test_schedule_constraint_artifact_matches_replay_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = "0 1 write 0x100\n1 2 read 0x100\n"
            events = parse_schedule_trace(trace)
            artifact = schedule_constraint_artifact(
                events,
                input_id="seed-id",
                target_branch=17,
                max_depth=8,
            )
            self.assertEqual(artifact["schema"], SCHEDULE_CONSTRAINT_SCHEMA)
            self.assertEqual(artifact["target_branch"], 17)
            self.assertEqual(artifact["replay_prefixes"], [[2]])
            self.assertEqual(artifact["memory_conflict_count"], 1)
            self.assertEqual(artifact["events"][0]["op"], "write")
            self.assertEqual(
                artifact["condpor_execution_graph"]["schema"],
                CONDPOR_GRAPH_SCHEMA,
            )
            self.assertTrue(
                verify_condpor_execution_graph_certificate(
                    artifact["condpor_execution_graph"]
                )
            )
            self.assertEqual(
                propose_source_replay_prefixes(events, max_depth=8),
                ((2,),),
            )

            out = os.path.join(tmp, "constraints.jsonl")
            self.assertTrue(append_schedule_constraint_artifact(out, artifact))
            self.assertEqual(
                json.loads(Path(out).read_text())["trace_digest"],
                artifact["trace_digest"],
            )

            state = os.path.join(tmp, "dpor.json")
            explorer = DporScheduleExplorer(
                state,
                max_depth=8,
                constraint_path=out,
            )
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            self.assertEqual(explorer.observe(
                seed,
                trace,
                target_branch=17,
            ), 1)
            rows = [
                json.loads(line)
                for line in Path(out).read_text().splitlines()
            ]
            self.assertEqual(rows[-1]["replay_prefixes"], [[2]])
            self.assertEqual(rows[-1]["target_branch"], 17)

    def test_source_dpor_certificate_checks_equivalence_classes(self):
        independent_a = parse_schedule_trace(
            "0 1 write 0x100\n"
            "1 2 write 0x200\n"
        )
        independent_b = parse_schedule_trace(
            "0 2 write 0x200\n"
            "1 1 write 0x100\n"
        )
        first = source_dpor_certificate(independent_a)
        second = source_dpor_certificate(independent_b)
        self.assertEqual(first["schema"], SOURCE_DPOR_SCHEMA)
        self.assertFalse(first["optimality_claimed"])
        self.assertEqual(
            first["dependency_graph"]["equivalence_sha256"],
            second["dependency_graph"]["equivalence_sha256"],
        )
        self.assertTrue(verify_source_dpor_certificate(first))

        dependent_a = source_dpor_certificate(parse_schedule_trace(
            "0 1 write 0x100\n"
            "1 2 read 0x100\n"
        ))
        dependent_b = source_dpor_certificate(parse_schedule_trace(
            "0 2 read 0x100\n"
            "1 1 write 0x100\n"
        ))
        self.assertNotEqual(
            dependent_a["dependency_graph"]["equivalence_sha256"],
            dependent_b["dependency_graph"]["equivalence_sha256"],
        )
        tampered = json.loads(json.dumps(dependent_a))
        tampered["replay_prefixes"] = [[99]]
        self.assertFalse(verify_source_dpor_certificate(tampered))

    def test_source_dpor_uses_causal_wakeup_sequence(self):
        certificate = source_dpor_certificate(parse_schedule_trace(
            "0 1 write 0x100\n"
            "1 2 write 0x200\n"
            "2 2 read 0x100\n"
        ))
        self.assertEqual(certificate["replay_prefixes"], [[2, 2]])
        self.assertEqual(len(certificate["source_sets"]), 1)
        source_set = certificate["source_sets"][0]
        self.assertEqual(source_set["base_prefix"], [])
        self.assertEqual(source_set["threads"], [2])
        self.assertEqual(
            source_set["wakeup_sequences"][0]["sequence"],
            [2, 2],
        )
        self.assertTrue(verify_source_dpor_certificate(certificate))

    def test_source_dpor_persists_bounded_sleep_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "dpor.json")
            seed = os.path.join(tmp, "seed")
            explorer = DporScheduleExplorer(state, max_depth=8)
            self.assertEqual(explorer.observe(
                seed,
                "0 1 write 0x100\n1 2 read 0x100\n",
            ), 1)
            self.assertEqual(explorer.pop_pending(1)[0].prefix, (2,))
            self.assertEqual(explorer.observe(
                seed,
                "0 2 read 0x100\n1 1 write 0x100\n",
                current_prefix=(2,),
            ), 0)
            self.assertGreaterEqual(explorer.sleep_pruned_prefixes, 1)

            reloaded = DporScheduleExplorer(state, max_depth=8)
            self.assertGreaterEqual(reloaded.sleep_pruned_prefixes, 1)
            payload = json.loads(Path(state).read_text())
            self.assertEqual(payload["schema"], 4)
            input_id = explorer.input_key(seed)
            self.assertEqual(payload["sleep_sets"][input_id][""], [1, 2])
            self.assertIn("", payload["wakeup_trees"][input_id])

    def test_source_dpor_tracks_equivalent_observed_traces(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = DporScheduleExplorer(
                os.path.join(tmp, "dpor.json"),
                max_depth=8,
            )
            seed = os.path.join(tmp, "seed")
            self.assertEqual(explorer.observe(
                seed,
                "0 1 write 0x100\n1 2 write 0x200\n",
            ), 0)
            self.assertEqual(explorer.observe(
                seed,
                "0 2 write 0x200\n1 1 write 0x100\n",
            ), 0)
            self.assertEqual(explorer.equivalent_traces, 1)

    def test_wakeup_tree_elides_weak_initial_equivalent_leaf(self):
        independent = parse_schedule_trace(
            "0 2 write 0x100\n"
            "1 2 write 0x101\n"
            "2 3 write 0x200\n"
            "3 3 write 0x201\n"
        )
        tree = BoundedWakeupTree(independent, ())
        self.assertTrue(tree.insert((2, 2))["inserted"])
        redundant = tree.insert((3, 3))
        self.assertFalse(redundant["inserted"])
        self.assertEqual(
            redundant["reason"],
            "existing_leaf_weak_initial",
        )

        dependent = BoundedWakeupTree(parse_schedule_trace(
            "0 4 write 0x300\n"
            "1 5 read 0x300\n"
        ), ())
        self.assertTrue(dependent.insert((4,))["inserted"])
        self.assertTrue(dependent.insert((5,))["inserted"])

    def test_wakeup_tree_certificate_checks_runtime_ready_root(self):
        ready = wakeup_tree_certificate(parse_schedule_trace(
            "0 2 ready 0x100 decision=0 chosen=2 "
            "tids=1,2 complete=1 prefix=1 fallback=0\n"
            "1 1 write 0x100\n"
            "2 2 read 0x100\n"
        ))
        self.assertEqual(ready["schema"], WAKEUP_TREE_SCHEMA)
        self.assertTrue(ready["ready_evidence_complete"])
        self.assertTrue(ready["insert_attempts"][0]["inserted"])
        self.assertTrue(verify_wakeup_tree_certificate(ready))

        disabled = wakeup_tree_certificate(parse_schedule_trace(
            "0 1 ready 0x100 decision=0 chosen=1 "
            "tids=1 complete=1 prefix=0 fallback=0\n"
            "1 1 write 0x100\n"
            "2 2 read 0x100\n"
        ))
        self.assertFalse(disabled["insert_attempts"][0]["inserted"])
        self.assertEqual(
            disabled["insert_attempts"][0]["reason"],
            "root_not_ready",
        )
        tampered = json.loads(json.dumps(ready))
        tampered["trees"][0]["leaves"] = [[99]]
        self.assertFalse(verify_wakeup_tree_certificate(tampered))

    def test_operational_enabledness_and_terminal_certificate(self):
        certificate = operational_enabledness_certificate(
            parse_schedule_trace(
                "0 1 ready 0x10 decision=0 chosen=1 tids=1,2 "
                "complete=1 prefix=0 fallback=0 "
                "offers=1:lock:0x10:0x0,2:lock:0x10:0x0\n"
                "1 1 lock 0x10 decision=0\n"
                "2 1 acquire 0x10\n"
                "3 1 unlock 0x10\n"
                "4 2 ready 0x10 decision=1 chosen=2 tids=2 "
                "complete=1 prefix=0 fallback=0 "
                "offers=2:lock:0x10:0x0\n"
                "5 2 lock 0x10 decision=1\n"
                "6 2 acquire 0x10\n"
                "7 2 unlock 0x10\n"
                "8 0 runtime_stop 0x0\n"
            )
        )
        self.assertTrue(certificate["offers_complete"])
        self.assertTrue(certificate["all_offers_classified"])
        self.assertTrue(certificate["all_chosen_completion_enabled"])
        self.assertTrue(
            certificate["bounded_terminal_execution_witnessed"]
        )
        self.assertEqual(
            certificate["decisions"][0]["enabled_threads"], [1, 2]
        )
        self.assertTrue(
            verify_operational_enabledness_certificate(certificate)
        )
        tampered = json.loads(json.dumps(certificate))
        tampered["decisions"][0]["enabled_threads"] = [99]
        self.assertFalse(
            verify_operational_enabledness_certificate(tampered)
        )

        wait_certificate = operational_enabledness_certificate(
            parse_schedule_trace(
                "0 1 lock 0x20\n"
                "1 1 acquire 0x20\n"
                "2 1 ready 0x30 decision=0 chosen=1 tids=1 "
                "complete=1 prefix=0 fallback=0 "
                "offers=1:wait:0x30:0x20\n"
                "3 1 wait 0x30 mutex=0x20 timed=0 decision=0\n"
                "4 1 wait_mutex_release 0x20\n"
                "5 1 wait_mutex_acquire 0x20\n"
                "6 1 wake 0x30\n"
                "7 1 unlock 0x20\n"
                "8 0 runtime_stop 0x0\n"
            )
        )
        self.assertEqual(
            wait_certificate["decisions"][0]["chosen_status"],
            "enabled",
        )
        self.assertTrue(wait_certificate["all_offers_classified"])
        self.assertTrue(
            verify_operational_enabledness_certificate(wait_certificate)
        )

    def test_condpor_backward_revisit_and_maximal_extension(self):
        certificate = condpor_execution_graph_certificate(
            parse_schedule_trace(
                "0 1 read 0x100 init=0x00\n"
                "1 1 constraint branch outcome=0 model-outcome=1\n"
                "2 2 write 0x100 value=0x01\n"
            ),
            max_depth=8,
        )
        self.assertEqual(certificate["schema"], CONDPOR_GRAPH_SCHEMA)
        self.assertTrue(certificate["causal_acyclic"])
        self.assertEqual(certificate["revisit_count"], 1)
        revisit = certificate["revisits"][0]
        self.assertEqual(revisit["old_read_from"], "init:0x100")
        self.assertEqual(revisit["new_read_from"], "t2:0")
        self.assertEqual(revisit["replay_prefix"], [2])
        self.assertEqual(
            revisit["extension"]["deleted_events"],
            ["t1:1"],
        )
        self.assertEqual(
            revisit["extension"]["choices"][0]["rule"],
            "deterministic_model_tiebreak",
        )
        self.assertTrue(
            verify_condpor_execution_graph_certificate(certificate)
        )
        tampered = json.loads(json.dumps(certificate))
        tampered["revisits"][0]["new_read_from"] = "init:0x100"
        self.assertFalse(
            verify_condpor_execution_graph_certificate(tampered)
        )

    def test_condpor_uses_value_bound_native_read_from_evidence(self):
        certificate = condpor_execution_graph_certificate(
            parse_schedule_trace(
                "0 1 write 0x100 atomic=1 kind=store group=1 bytes=1 mo=relaxed\n"
                "1 1 atomic_value 0x100 group=1 role=write bits=8 value=0x2a\n"
                "2 1 write 0x100 atomic=1 kind=store group=2 bytes=1 mo=relaxed\n"
                "3 1 atomic_value 0x100 group=2 role=write bits=8 value=0x2b\n"
                "4 2 read 0x100 atomic=1 kind=load group=3 bytes=1 mo=relaxed\n"
                "5 2 atomic_value 0x100 group=3 role=read bits=8 value=0x2a\n"
            )
        )

        self.assertEqual(
            certificate["read_from_inference"],
            [{
                "read": "t2:0",
                "source": "t1:0",
                "inference": "latest-matching-value",
            }],
        )
        self.assertTrue(
            verify_condpor_execution_graph_certificate(certificate)
        )

    def test_condpor_rejects_causal_or_protected_revisit(self):
        causal = condpor_execution_graph_certificate(
            parse_schedule_trace(
                "0 1 read 0x100 init=0\n"
                "1 1 write 0x100 value=1\n"
            )
        )
        self.assertEqual(causal["revisit_count"], 0)

        protected = condpor_execution_graph_certificate(
            parse_schedule_trace(
                "0 1 lock L\n"
                "1 1 read 0x100 init=0\n"
                "2 1 unlock L\n"
                "3 2 lock L\n"
                "4 2 write 0x100 value=1\n"
                "5 2 unlock L\n"
            )
        )
        self.assertEqual(protected["revisit_count"], 0)
        self.assertTrue(any(
            row["reason"]
            == "not_unordered_unprotected_memory_conflict"
            for row in protected["rejected_revisits"]
        ))

    def test_condpor_withholds_path_dependent_suffix_for_replay(self):
        certificate = condpor_execution_graph_certificate(
            parse_schedule_trace(
                "0 1 read 0x103 init=0\n"
                "1 1 constraint branch last-read=0x100 "
                "last-read-bytes=4 outcome=0 next=a\n"
                "2 1 action a\n"
                "3 2 write 0x103 value=1\n"
            )
        )
        self.assertEqual(certificate["revisit_count"], 1)
        extension = certificate["revisits"][0]["extension"]
        self.assertTrue(extension["regeneration_required"])
        self.assertEqual(
            extension["regeneration_frontier"], ["t1:1"]
        )
        self.assertEqual(
            extension["withheld_path_dependent_events"],
            ["t1:1", "t1:2"],
        )
        self.assertEqual(extension["extension_order"], [])
        self.assertTrue(
            verify_condpor_execution_graph_certificate(certificate)
        )

    def test_condpor_revisit_identity_persists_across_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "dpor.json")
            seed = os.path.join(tmp, "seed")
            trace = (
                "0 1 read 0x100 init=0\n"
                "1 2 write 0x100 value=1\n"
            )
            explorer = DporScheduleExplorer(state, max_depth=8)
            self.assertEqual(explorer.observe(seed, trace), 1)
            self.assertEqual(explorer.condpor_revisits, 1)
            explorer.observe(seed, trace)
            self.assertEqual(explorer.condpor_duplicate_revisits, 1)
            payload = json.loads(Path(state).read_text())
            input_id = explorer.input_key(seed)
            self.assertEqual(payload["schema"], 4)
            self.assertEqual(
                len(payload["condpor_revisit_seen"][input_id]),
                1,
            )

    def test_schedule_smt_artifact_encodes_partial_order_and_prefix(self):
        events = parse_schedule_trace(
            "0 1 write 0x100 prov=module\n"
            "1 2 read 0x100 prov=heap\n"
            "2 1 write 0x200 prov=module\n"
        )
        artifact = schedule_smt_artifact(
            events,
            input_id="seed-id",
            target_branch=23,
            max_depth=8,
        )
        self.assertEqual(artifact["schema"], SCHEDULE_SMT_SCHEMA)
        self.assertEqual(artifact["memory_model"], "SC")
        self.assertEqual(artifact["query_count"], 1)
        self.assertEqual(artifact["queries"][0]["prefix"], [2])
        self.assertEqual(
            artifact["queries"][0]["conflict"]["kind"],
            "memory",
        )
        self.assertEqual(artifact["queries"][0]["program_order_count"], 1)
        self.assertEqual(len(artifact["artifact_sha256"]), 64)
        smt2 = materialize_schedule_smt_query(artifact, 0)
        self.assertIn("(set-logic QF_LIA)", smt2)
        self.assertIn("(declare-fun pos_0 () Int)", smt2)
        self.assertIn(
            "(assert (! (< pos_0 pos_2) :named po_0_2))",
            smt2,
        )
        self.assertIn(
            "(assert (! (= pos_1 0) :named q0_replay_slot_0))",
            smt2,
        )
        self.assertIn(
            "(assert (! (< pos_1 pos_0) :named q0_conflict_reversal))",
            smt2,
        )
        self.assertIn("(push 1)", artifact["incremental_smt2"])
        self.assertNotIn("delta_smt2", artifact["base_smt2"])
        self.assertIn("general_runtime_enabledness", artifact["not_encoded"])
        self.assertEqual(
            schedule_smt_artifact(
                list(reversed(events)),
                input_id="seed-id",
                target_branch=23,
                max_depth=8,
            )["artifact_sha256"],
            artifact["artifact_sha256"],
        )

        result = evaluate_with_system_z3(smt2)
        if result is not None:
            self.assertTrue(result.startswith("sat\n"))
        incremental_result = evaluate_with_system_z3(
            artifact["incremental_smt2"]
        )
        if incremental_result is not None:
            self.assertEqual(incremental_result.count("sat\n"), 1)

    def test_joint_path_schedule_read_from_solver(self):
        query_smt2 = (
            "(set-logic QF_BV)\n"
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x42))\n"
            "(check-sat)\n"
        )
        satisfiable = schedule_smt_artifact(parse_schedule_trace(
            "0 1 write 0x100 value=0x99\n"
            "1 2 read 0x100 sym-byte=0 init=0x42\n"
        ))
        result = solve_joint_path_schedule_query(
            satisfiable,
            query_smt2,
            query_id="a" * 64,
        )
        self.assertEqual(result["schema"], JOINT_PATH_SCHEDULE_SCHEMA)
        self.assertEqual(result["status"], "sat")
        self.assertTrue(result["single_solver_context"])
        self.assertEqual(result["model"]["input_bytes"], {"0": 66})
        self.assertEqual(result["model"]["read_from"], {"rf_1": -1})
        self.assertEqual(result["read_from"]["value_bridge_count"], 1)
        self.assertTrue(verify_joint_path_schedule_result(
            satisfiable,
            query_smt2,
            result,
        ))

        unsatisfiable = schedule_smt_artifact(parse_schedule_trace(
            "0 1 write 0x100 value=0x42\n"
            "1 2 read 0x100 sym-byte=0 init=0x00\n"
        ))
        rejected = solve_joint_path_schedule_query(
            unsatisfiable,
            query_smt2,
        )
        self.assertEqual(rejected["status"], "unsat")
        self.assertTrue(verify_joint_path_schedule_result(
            unsatisfiable,
            query_smt2,
            rejected,
        ))

    def test_sc_tso_and_ra_store_buffering_litmus(self):
        events = parse_schedule_trace(
            "0 1 write x\n"
            "1 1 read y\n"
            "2 2 write y\n"
            "3 2 read x\n"
        )
        outcomes = (
            "(assert (= rf_1 (- 1)))\n"
            "(assert (= rf_3 (- 1)))\n"
        )
        sc = schedule_smt_artifact(events, memory_model="SC")
        tso = schedule_smt_artifact(events, memory_model="TSO")
        ra = schedule_smt_artifact(events, memory_model="RA")
        for artifact in (sc, tso, ra):
            self.assertIn("(- 1)", artifact["base_smt2"])
            self.assertNotIn(" -1", artifact["base_smt2"])
        self.assertEqual(
            solve_schedule_smt_query(
                sc,
                None,
                extra_smt2=outcomes,
                extra_integer_model_names=("rf_1", "rf_3"),
            )["status"],
            "unsat",
        )
        tso_result = solve_schedule_smt_query(
            tso,
            None,
            extra_smt2=outcomes,
            extra_integer_model_names=("rf_1", "rf_3"),
        )
        self.assertEqual(tso_result["status"], "sat")
        self.assertFalse(tso_result["runtime_replayable"])
        self.assertEqual(
            tso_result["model"]["extra_integers"],
            {"rf_1": -1, "rf_3": -1},
        )
        self.assertEqual(
            solve_schedule_smt_query(
                ra,
                None,
                extra_smt2=outcomes,
                extra_integer_model_names=("rf_1", "rf_3"),
            )["status"],
            "sat",
        )
        self.assertEqual(tso["memory_consistency"]["model"], "TSO")
        self.assertIn(
            "fifo-store-buffer",
            tso["memory_consistency"]["semantics"],
        )
        self.assertGreater(
            ra["memory_consistency"]["hb_variable_count"],
            0,
        )

    def test_tso_store_forwarding_rejects_stale_same_thread_read(self):
        artifact = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 write x\n"
                "1 1 read x\n"
            ),
            memory_model="TSO",
        )
        result = solve_schedule_smt_query(
            artifact,
            None,
            extra_smt2="(assert (= rf_1 (- 1)))\n",
            extra_integer_model_names=("rf_1",),
        )
        self.assertEqual(result["status"], "unsat")

    def test_ra_release_acquire_message_passing(self):
        release_acquire = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 write data mo=relaxed\n"
                "1 1 write flag mo=release\n"
                "2 2 read flag mo=acquire\n"
                "3 2 read data mo=relaxed\n"
            ),
            memory_model="RA",
        )
        outcome = (
            "(assert (= rf_2 1))\n"
            "(assert (= rf_3 (- 1)))\n"
        )
        self.assertEqual(
            solve_schedule_smt_query(
                release_acquire,
                None,
                extra_smt2=outcome,
                extra_integer_model_names=("rf_2", "rf_3"),
            )["status"],
            "unsat",
        )

        relaxed = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 write data mo=relaxed\n"
                "1 1 write flag mo=release\n"
                "2 2 read flag mo=relaxed\n"
                "3 2 read data mo=relaxed\n"
            ),
            memory_model="RA",
        )
        result = solve_schedule_smt_query(
            relaxed,
            None,
            extra_smt2=outcome,
            extra_integer_model_names=("rf_2", "rf_3"),
        )
        self.assertEqual(result["status"], "sat")
        self.assertEqual(
            result["model"]["extra_integers"],
            {"rf_2": 1, "rf_3": -1},
        )

    def test_ra_rmw_release_sequence_and_atomicity(self):
        release_sequence = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 write 0x100 atomic=1 bytes=4 mo=release\n"
                "1 2 rmw 0x100 atomic=1 bytes=4 mo=relaxed\n"
                "2 3 read 0x100 atomic=1 bytes=4 mo=acquire\n"
            ),
            memory_model="RA",
        )
        result = solve_schedule_smt_query(
            release_sequence,
            None,
            extra_smt2=(
                "(assert (= rf_1 0))\n"
                "(assert (= rf_2 1))\n"
            ),
        )
        self.assertEqual(result["status"], "sat")
        consistency = release_sequence["memory_consistency"]
        self.assertEqual(consistency["atomic_rmw_count"], 1)
        self.assertGreater(
            consistency["release_sequence_constraint_count"], 0
        )

        non_atomic_rmw = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 write 0x100 atomic=1 bytes=4 mo=release\n"
                "1 2 write 0x100 atomic=1 bytes=4 mo=relaxed\n"
                "2 3 rmw 0x100 atomic=1 bytes=4 mo=relaxed\n"
            ),
            memory_model="RA",
        )
        rejected = solve_schedule_smt_query(
            non_atomic_rmw,
            None,
            extra_smt2=(
                "(assert (= rf_2 0))\n"
                "(assert (< mo_0 mo_1))\n"
                "(assert (< mo_1 mo_2))\n"
            ),
        )
        self.assertEqual(rejected["status"], "unsat")

    def test_ra_fence_sc_order_race_and_mixed_size_rules(self):
        fences = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 fence f1 atomic=1 bytes=0 mo=release\n"
                "1 1 write 0x100 atomic=1 bytes=4 mo=relaxed\n"
                "2 2 read 0x100 atomic=1 bytes=4 mo=relaxed\n"
                "3 2 fence f2 atomic=1 bytes=0 mo=acquire\n"
            ),
            memory_model="RA",
        )
        self.assertEqual(
            solve_schedule_smt_query(
                fences,
                None,
                extra_smt2=(
                    "(assert (= rf_2 1))\n"
                    "(assert (not hb_0_3))\n"
                ),
            )["status"],
            "unsat",
        )
        self.assertGreater(
            fences["memory_consistency"]["fence_count"], 0
        )

        seq_cst = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 write 0x100 atomic=1 bytes=4 mo=seq_cst\n"
                "1 2 write 0x100 atomic=1 bytes=4 mo=seq_cst\n"
                "2 3 read 0x100 atomic=1 bytes=4 mo=seq_cst\n"
            ),
            memory_model="RA",
        )
        self.assertEqual(
            solve_schedule_smt_query(
                seq_cst,
                None,
                extra_smt2=(
                    "(assert (= rf_2 0))\n"
                    "(assert (< mo_0 mo_1))\n"
                    "(assert (< sc_1 sc_2))\n"
                ),
            )["status"],
            "unsat",
        )
        self.assertEqual(
            seq_cst["memory_consistency"]["seq_cst_event_count"], 3
        )

        race = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 write 0x100 atomic=0 bytes=1\n"
                "1 2 read 0x100 atomic=0 bytes=1\n"
            ),
            memory_model="RA",
        )
        self.assertEqual(
            solve_schedule_smt_query(race, None)["status"], "unsat"
        )
        self.assertEqual(
            race["memory_consistency"][
                "non_atomic_race_candidate_count"
            ],
            1,
        )

        mixed = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 write 0x100 atomic=1 bytes=4 mo=release\n"
                "1 2 read 0x102 atomic=1 bytes=2 mo=acquire\n"
            ),
            memory_model="RA",
        )
        self.assertEqual(
            solve_schedule_smt_query(
                mixed,
                None,
                extra_smt2="(assert (= rf_1 0))\n",
            )["status"],
            "sat",
        )
        self.assertEqual(
            mixed["memory_consistency"]["mixed_size_overlap_count"], 1
        )
        partial = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 write 0x100 atomic=1 bytes=1 mo=release\n"
                "1 2 read 0x100 atomic=1 bytes=4 mo=acquire\n"
            ),
            memory_model="RA",
        )
        self.assertEqual(
            solve_schedule_smt_query(
                partial,
                None,
                extra_smt2="(assert (= rf_1 0))\n",
            )["status"],
            "unsat",
        )

    def test_native_condpor_distinguishes_sc_tso_and_ra_store_buffering(self):
        events = parse_schedule_trace(
            "0 1 write x atomic=1 bytes=1 mo=relaxed\n"
            "1 1 read y atomic=1 bytes=1 mo=relaxed\n"
            "2 2 write y atomic=1 bytes=1 mo=relaxed\n"
            "3 2 read x atomic=1 bytes=1 mo=relaxed\n"
        )
        certificates = {
            model: native_condpor_memory_graph_certificate(
                events, memory_model=model
            )
            for model in ("SC", "TSO", "RA")
        }

        def signatures(certificate):
            return {
                tuple(
                    int(row["source_index"])
                    for row in graph["read_from"]
                )
                for graph in certificate["graphs"]
            }

        self.assertEqual(
            certificates["SC"]["schema"],
            NATIVE_CONDPOR_MEMORY_GRAPH_SCHEMA,
        )
        self.assertNotIn((-1, -1), signatures(certificates["SC"]))
        self.assertIn((-1, -1), signatures(certificates["TSO"]))
        self.assertIn((-1, -1), signatures(certificates["RA"]))
        self.assertEqual(
            certificates["SC"]["status_counts"],
            {"sat": 3, "unsat": 1, "unknown": 0},
        )
        for certificate in certificates.values():
            self.assertTrue(certificate["bounded_exhaustive"])
            self.assertTrue(
                verify_native_condpor_memory_graph_certificate(certificate)
            )

    def test_native_condpor_ra_release_acquire_forbids_stale_data(self):
        certificate = native_condpor_memory_graph_certificate(
            parse_schedule_trace(
                "0 1 write data atomic=1 bytes=1 mo=relaxed\n"
                "1 1 write flag atomic=1 bytes=1 mo=release\n"
                "2 2 read flag atomic=1 bytes=1 mo=acquire\n"
                "3 2 read data atomic=1 bytes=1 mo=relaxed\n"
            ),
            memory_model="RA",
        )
        signatures = {
            tuple(
                int(row["source_index"])
                for row in graph["read_from"]
            )
            for graph in certificate["graphs"]
        }
        self.assertNotIn((1, -1), signatures)
        self.assertEqual(certificate["status_counts"]["unsat"], 1)
        self.assertTrue(
            verify_native_condpor_memory_graph_certificate(certificate)
        )

    def test_native_condpor_enumerates_coherence_and_reports_truncation(self):
        events = parse_schedule_trace(
            "0 1 write x atomic=1 bytes=1 mo=relaxed\n"
            "1 2 write x atomic=1 bytes=1 mo=relaxed\n"
            "2 3 write x atomic=1 bytes=1 mo=relaxed\n"
        )
        complete = native_condpor_memory_graph_certificate(
            events, memory_model="RA"
        )
        orders = {
            tuple(graph["modification_orders"][0]["event_indices"])
            for graph in complete["graphs"]
        }
        self.assertEqual(complete["candidate_space"], 8)
        self.assertEqual(complete["status_counts"]["sat"], 6)
        self.assertEqual(len(orders), 6)

        truncated = native_condpor_memory_graph_certificate(
            events, memory_model="RA", max_candidates=2
        )
        self.assertEqual(truncated["status"], "truncated")
        self.assertFalse(truncated["bounded_exhaustive"])
        self.assertEqual(truncated["enumerated_candidate_count"], 2)
        self.assertTrue(truncated["truncated"]["candidate_space"])

        memory_truncated = native_condpor_memory_graph_certificate(
            events, memory_model="RA", max_memory_events=1
        )
        self.assertEqual(memory_truncated["status"], "truncated")
        self.assertTrue(memory_truncated["truncated"]["source_events"])
        self.assertEqual(
            memory_truncated["memory_consistency"]["modeled_memory_event_count"],
            1,
        )

        seq_cst_sc = native_condpor_memory_graph_certificate(
            parse_schedule_trace(
                "0 1 write x atomic=1 bytes=1 mo=seq_cst\n"
                "1 2 read x atomic=1 bytes=1 mo=seq_cst\n"
            ),
            memory_model="SC",
        )
        self.assertTrue(seq_cst_sc["bounded_exhaustive"])
        self.assertTrue(
            verify_native_condpor_memory_graph_certificate(seq_cst_sc)
        )

    def test_native_condpor_binds_value_evidence_and_rejects_tamper(self):
        certificate = native_condpor_memory_graph_certificate(
            parse_schedule_trace(
                "0 1 write x atomic=1 kind=store group=1 bytes=1 mo=relaxed\n"
                "1 1 atomic_value x group=1 role=write bits=8 value=0x2a\n"
                "2 2 write x atomic=1 kind=store group=2 bytes=1 mo=relaxed\n"
                "3 2 atomic_value x group=2 role=write bits=8 value=0x2b\n"
                "4 3 read x atomic=1 kind=load group=3 bytes=1 mo=relaxed\n"
                "5 3 atomic_value x group=3 role=read bits=8 value=0x2a\n"
            ),
            memory_model="RA",
        )
        evidence = {
            graph["read_from"][0]["source_index"]: graph["value_evidence"]
            for graph in certificate["graphs"]
        }
        self.assertEqual(evidence[0]["confirmed"], 1)
        self.assertTrue(evidence[0]["fully_value_witnessed"])
        self.assertEqual(evidence[1]["contradicted"], 1)
        self.assertFalse(evidence[1]["hardware_compatible"])
        self.assertTrue(
            verify_native_condpor_memory_graph_certificate(certificate)
        )
        tampered = json.loads(json.dumps(certificate))
        tampered["graphs"][0]["graph_sha256"] = "0" * 64
        self.assertFalse(
            verify_native_condpor_memory_graph_certificate(tampered)
        )

    def test_schedule_explorer_writes_bounded_smt_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            smt_out = os.path.join(tmp, "schedule-smt.jsonl")
            explorer = DporScheduleExplorer(
                os.path.join(tmp, "dpor.json"),
                max_depth=8,
                smt_path=smt_out,
                smt_max_events=2,
            )
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            self.assertEqual(explorer.observe(
                seed,
                "0 1 write 0x100\n1 2 read 0x100\n",
                target_branch=29,
            ), 1)
            row = json.loads(Path(smt_out).read_text())
            self.assertEqual(row["schema"], SCHEDULE_SMT_SCHEMA)
            self.assertEqual(row["target_branch"], 29)
            self.assertEqual(row["modeled_event_count"], 2)
            self.assertEqual(row["query_count"], 1)
            self.assertEqual(row["lifecycle_order_encoding"], "partial")

            copied = os.path.join(tmp, "copied-smt.jsonl")
            self.assertTrue(append_schedule_smt_artifact(copied, row))
            self.assertEqual(
                json.loads(Path(copied).read_text())["artifact_sha256"],
                row["artifact_sha256"],
            )

    def test_schedule_smt_hb_assumptions_use_complete_trace(self):
        events = parse_schedule_trace(
            "0 1 lock 0xaa\n"
            "1 1 write 0x100\n"
            "2 1 unlock 0xaa\n"
            "3 2 lock 0xaa\n"
            "4 2 write 0x200\n"
            "5 3 read 0x200\n"
        )
        artifact = schedule_smt_artifact(events, max_depth=8)
        self.assertEqual(artifact["query_count"], 1)
        self.assertGreater(artifact["observed_hb_assumption_count"], 0)
        self.assertEqual(
            artifact["observed_hb_assumption_count"],
            len(artifact["observed_hb_assumptions"]),
        )
        self.assertIn("(declare-fun observed_hb_", artifact["base_smt2"])
        self.assertIn(
            "observed_vector_clock_happens_before",
            artifact["optional_constraints"],
        )

    def test_schedule_smt_reports_candidates_outside_event_bound(self):
        artifact = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 write 0x100\n"
                "1 1 write 0x200\n"
                "2 2 read 0x100\n"
            ),
            max_depth=8,
            max_events=2,
        )
        self.assertEqual(artifact["candidate_count"], 1)
        self.assertEqual(artifact["query_count"], 0)
        self.assertEqual(artifact["dropped_query_count"], 1)
        self.assertEqual(artifact["dropped_event_bound_count"], 1)
        self.assertEqual(artifact["dropped_query_limit_count"], 0)
        self.assertTrue(artifact["truncated"]["events"])
        self.assertTrue(artifact["truncated"]["queries"])

    def test_schedule_smt_bounds_query_count(self):
        artifact = schedule_smt_artifact(
            [
                ScheduleEvent(index, 1 + (index % 3), "write", "0x100")
                for index in range(12)
            ],
            max_depth=12,
            max_queries=2,
        )
        self.assertGreater(artifact["candidate_count"], 2)
        self.assertEqual(artifact["query_count"], 2)
        self.assertEqual(artifact["bounds"]["max_queries"], 2)
        self.assertEqual(
            artifact["dropped_query_limit_count"],
            artifact["candidate_count"] - 2,
        )
        self.assertTrue(artifact["truncated"]["queries"])
        result = evaluate_with_system_z3(artifact["incremental_smt2"])
        if result is not None:
            self.assertEqual(result.count("sat\n"), 2)

    def test_schedule_smt_partial_order_elides_total_permutation(self):
        trace = parse_schedule_trace(
            "0 0 create 0x1\n"
            "1 1 thread_start 0x1\n"
            "2 0 create_success 0x1 mapped=1\n"
            "3 0 join 0x1 mapped=1\n"
            "4 1 thread_exit 0x1\n"
            "5 0 join_success 0x1 mapped=1\n"
        )
        partial = schedule_smt_artifact(
            trace,
            order_encoding="partial",
        )
        permutation = schedule_smt_artifact(
            trace,
            order_encoding="permutation",
        )
        self.assertEqual(partial["lifecycle_order_encoding"], "partial")
        self.assertTrue(
            partial["sync_state"]["linear_extension_semantics"]
        )
        self.assertEqual(
            partial["sync_state"]["order_bound_constraint_count"],
            0,
        )
        self.assertEqual(
            partial["sync_state"]["order_distinct_constraint_count"],
            0,
        )
        self.assertNotIn("sync_bound_", partial["base_smt2"])
        self.assertNotIn(":named sync_permutation", partial["base_smt2"])
        self.assertIn(
            "sync_lifecycle_partial_order_linear_extension",
            partial["hard_constraints"],
        )

        lifecycle_count = (
            permutation["sync_state"]["modeled_lifecycle_event_count"]
        )
        self.assertEqual(
            permutation["sync_state"]["order_bound_constraint_count"],
            lifecycle_count,
        )
        self.assertEqual(
            permutation["sync_state"]["order_distinct_constraint_count"],
            1,
        )
        self.assertIn("sync_bound_", permutation["base_smt2"])
        self.assertIn(
            ":named sync_permutation",
            permutation["base_smt2"],
        )

        for artifact in (partial, permutation):
            thread = artifact["sync_state"]["thread_lifecycles"][0]
            join = artifact["sync_state"]["joins"][0]
            sat_query = (
                artifact["base_smt2"]
                + f"(assert (< {join['join_var']} "
                + f"{thread['exit_var']}))\n"
                + "(check-sat)\n"
            )
            result = evaluate_with_system_z3(sat_query)
            if result is not None:
                self.assertTrue(result.startswith("sat\n"))
            unsat_query = (
                artifact["base_smt2"]
                + f"(assert (< {thread['start_var']} "
                + f"{thread['create_var']}))\n"
                + "(check-sat)\n"
            )
            result = evaluate_with_system_z3(unsat_query)
            if result is not None:
                self.assertTrue(result.startswith("unsat\n"))
        self.assertLess(
            len(partial["base_smt2"]),
            len(permutation["base_smt2"]),
        )

    def test_schedule_smt_partial_order_links_are_strict(self):
        artifact = schedule_smt_artifact(parse_schedule_trace(
            "0 1 wait 0xaa\n"
            "1 2 wait 0xaa\n"
        ))
        self.assertIn(":named sync_anchor_0", artifact["base_smt2"])
        self.assertIn(":named sync_anchor_1", artifact["base_smt2"])
        self.assertNotIn(":named sync_link_0_1", artifact["base_smt2"])
        result = evaluate_with_system_z3(
            artifact["base_smt2"]
            + "(assert (= sync_ord_0 sync_ord_1))\n"
            + "(check-sat)\n"
        )
        if result is not None:
            self.assertTrue(result.startswith("unsat\n"))

    def test_schedule_smt_scaled_anchors_replace_quadratic_links(self):
        events = [
            ScheduleEvent(index, index + 1, "wait", "0xaa")
            for index in range(24)
        ]
        partial = schedule_smt_artifact(
            events,
            order_encoding="partial",
        )
        permutation = schedule_smt_artifact(
            events,
            order_encoding="permutation",
        )
        partial_state = partial["sync_state"]
        permutation_state = permutation["sync_state"]
        semantic_pairs = 24 * 23 // 2
        self.assertEqual(
            partial_state["order_link_encoding"],
            "scaled-anchor",
        )
        self.assertEqual(
            partial_state["order_link_constraint_count"],
            24,
        )
        self.assertEqual(
            partial_state["order_link_semantic_pair_count"],
            semantic_pairs,
        )
        self.assertEqual(partial_state["controlled_anchor_count"], 24)
        self.assertEqual(partial_state["anchor_stride"], 25)
        self.assertEqual(
            permutation_state["order_link_encoding"],
            "pairwise",
        )
        self.assertEqual(
            permutation_state["order_link_constraint_count"],
            semantic_pairs,
        )
        self.assertNotIn(":named sync_link_", partial["base_smt2"])
        self.assertIn(":named sync_link_", permutation["base_smt2"])
        self.assertLess(
            len(partial["base_smt2"]),
            len(permutation["base_smt2"]) // 3,
        )
        for artifact in (partial, permutation):
            result = evaluate_with_system_z3(
                artifact["base_smt2"] + "(check-sat)\n"
            )
            if result is not None:
                self.assertTrue(result.startswith("sat\n"))

    def test_schedule_linear_extension_certificate_and_replay_projection(self):
        artifact = schedule_smt_artifact(parse_schedule_trace(
            "0 1 wait 0xaa\n"
            "1 2 wait 0xaa\n"
        ))
        observed = artifact["observed_linear_extension"]
        self.assertTrue(verify_schedule_linear_extension_certificate(
            artifact,
            observed,
        ))
        self.assertEqual(
            artifact["observed_topology_replay_prefix"],
            [1, 2],
        )

        reverse = schedule_linear_extension_certificate(
            artifact,
            event_positions=[1, 0],
            lifecycle_ranks=[3, 0],
            query_index=0,
        )
        self.assertEqual(reverse["source"], "solver_query_model")
        self.assertEqual(reverse["query_index"], 0)
        self.assertEqual(reverse["linear_extension"], [1, 0])
        self.assertEqual(reverse["replay_prefix"], [2, 1])
        self.assertTrue(verify_schedule_linear_extension_certificate(
            artifact,
            reverse,
        ))

        tampered = dict(reverse)
        tampered["replay_prefix"] = [1, 2]
        self.assertFalse(verify_schedule_linear_extension_certificate(
            artifact,
            tampered,
        ))
        with self.assertRaisesRegex(
            ValueError,
            "do not satisfy schedule query",
        ):
            schedule_linear_extension_certificate(
                artifact,
                event_positions=[0, 1],
                lifecycle_ranks=[0, 3],
                query_index=0,
            )
        program_order_artifact = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 wait 0xaa\n"
                "1 2 wait 0xaa\n"
                "2 1 wait 0xbb\n"
            )
        )
        with self.assertRaisesRegex(
            ValueError,
            "violate per-thread program order",
        ):
            schedule_linear_extension_certificate(
                program_order_artifact,
                event_positions=[2, 0, 1],
                lifecycle_ranks=[8, 0, 4],
            )
        memory_artifact = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 write 0x100\n"
                "1 2 read 0x100\n"
            )
        )
        memory_certificate = schedule_linear_extension_certificate(
            memory_artifact,
            event_positions=[1, 0],
            lifecycle_ranks=[],
            query_index=0,
        )
        self.assertFalse(memory_certificate["runtime_replayable"])
        self.assertTrue(verify_schedule_linear_extension_certificate(
            memory_artifact,
            memory_certificate,
        ))
        with tempfile.TemporaryDirectory() as memory_tmp:
            self.assertFalse(write_schedule_linear_extension_prefix(
                os.path.join(memory_tmp, "memory.prefix"),
                memory_artifact,
                memory_certificate,
            ))
        with tempfile.TemporaryDirectory() as tmp:
            prefix_path = os.path.join(tmp, "topology.prefix")
            self.assertTrue(write_schedule_linear_extension_prefix(
                prefix_path,
                artifact,
                reverse,
            ))
            self.assertEqual(
                Path(prefix_path).read_text().split(),
                ["2", "1"],
            )
            artifact_path = Path(tmp) / "schedule.jsonl"
            model_path = Path(tmp) / "model.json"
            cli_prefix = Path(tmp) / "cli.prefix"
            cli_certificate = Path(tmp) / "cli-certificate.json"
            artifact_path.write_text(json.dumps(artifact) + "\n")
            model_path.write_text(json.dumps({
                "event_positions": {"pos_0": 1, "pos_1": 0},
                "lifecycle_ranks": {
                    "sync_ord_0": 3,
                    "sync_ord_1": 0,
                },
            }))
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "symcc_schedule_linearize.py"),
                    str(artifact_path),
                    "--model", str(model_path),
                    "--query-index", "0",
                    "--prefix-out", str(cli_prefix),
                    "--certificate-out", str(cli_certificate),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(cli_prefix.read_text().split(), ["2", "1"])
            cli_row = json.loads(completed.stdout)
            self.assertEqual(cli_row["source"], "solver_query_model")
            self.assertTrue(verify_schedule_linear_extension_certificate(
                artifact,
                json.loads(cli_certificate.read_text()),
            ))

    def test_schedule_linear_extension_certificate_checks_choices(self):
        artifact = schedule_smt_artifact(parse_schedule_trace(
            "0 1 lock 0xaa\n"
            "1 1 acquire 0xaa\n"
            "2 1 unlock 0xaa\n"
            "3 2 lock 0xaa\n"
            "4 2 acquire 0xaa\n"
            "5 2 unlock 0xaa\n"
        ))
        order_ir = artifact["sync_state"]["order_ir"]
        self.assertEqual(order_ir["schema"], "symcc-lifecycle-order-ir-v1")
        self.assertEqual(len(order_ir["choice_constraints"]), 1)
        certificate = artifact["observed_linear_extension"]
        self.assertEqual(
            certificate["selected_choices"],
            [{"name": "sync_exclusion_0_1", "alternative": 0}],
        )
        self.assertTrue(verify_schedule_linear_extension_certificate(
            artifact,
            certificate,
        ))
        invalid = dict(certificate)
        invalid["selected_choices"] = [{
            "name": "sync_exclusion_0_1",
            "alternative": 1,
        }]
        invalid["certificate_sha256"] = hashlib.sha256(json.dumps(
            {
                key: value
                for key, value in invalid.items()
                if key != "certificate_sha256"
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        self.assertFalse(verify_schedule_linear_extension_certificate(
            artifact,
            invalid,
        ))

    def test_schedule_smt_mutex_state_rejects_forced_overlap(self):
        artifact = schedule_smt_artifact(parse_schedule_trace(
            "0 1 lock 0xaa\n"
            "1 1 acquire 0xaa\n"
            "2 2 lock 0xaa\n"
            "3 1 unlock 0xaa\n"
            "4 2 acquire 0xaa\n"
            "5 2 unlock 0xaa\n"
        ))
        self.assertEqual(artifact["schema"], "symcc-schedule-smt-v7")
        state = artifact["sync_state"]
        self.assertEqual(state["complete_section_count"], 2)
        self.assertEqual(state["exclusion_constraint_count"], 1)
        self.assertEqual(state["lazy_refinement_count"], 1)
        self.assertEqual(
            state["lazy_refinements"][0]["name"],
            "sync_exclusion_0_1",
        )
        self.assertNotIn(
            ":named sync_exclusion_0_1",
            artifact["relaxed_base_smt2"],
        )
        self.assertIn(
            ":named sync_exclusion_0_1",
            artifact["base_smt2"],
        )
        sections = {section["tid"]: section for section in state["sections"]}
        first = sections[1]
        second = sections[2]
        forced_overlap = (
            artifact["base_smt2"]
            + f"(assert (< {first['acquire_var']} "
            + f"{second['acquire_var']}))\n"
            + f"(assert (< {second['acquire_var']} "
            + f"{first['release_var']}))\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(forced_overlap)
        if result is not None:
            self.assertTrue(result.startswith("unsat\n"))
        relaxed_overlap = (
            artifact["relaxed_base_smt2"]
            + f"(assert (< {first['acquire_var']} "
            + f"{second['acquire_var']}))\n"
            + f"(assert (< {second['acquire_var']} "
            + f"{first['release_var']}))\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(relaxed_overlap)
        if result is not None:
            self.assertTrue(result.startswith("sat\n"))
        replay_result = evaluate_with_system_z3(
            materialize_schedule_smt_query(artifact, 0)
        )
        if replay_result is not None:
            self.assertTrue(replay_result.startswith("sat\n"))

    def test_schedule_solver_lazy_refinement_and_direct_model(self):
        if not ctypes.util.find_library("z3"):
            self.skipTest("system libz3 unavailable")
        artifact = schedule_smt_artifact(parse_schedule_trace(
            "0 1 lock 0xaa\n"
            "1 1 acquire 0xaa\n"
            "2 2 lock 0xaa\n"
            "3 1 unlock 0xaa\n"
            "4 2 acquire 0xaa\n"
            "5 2 unlock 0xaa\n"
        ))
        solved = solve_schedule_smt_query(artifact, 0)
        self.assertEqual(solved["status"], "sat")
        self.assertEqual(solved["solver"], "system-libz3-c-api")
        self.assertEqual(
            solved["mode"], "violation-driven-lazy-refinement"
        )
        self.assertTrue(verify_schedule_linear_extension_certificate(
            artifact, solved["certificate"]
        ))
        self.assertEqual(
            len(solved["model"]["event_positions"]),
            artifact["modeled_event_count"],
        )

        sections = {
            section["tid"]: section
            for section in artifact["sync_state"]["sections"]
        }
        forced_overlap = (
            f"(assert (< {sections[1]['acquire_var']} "
            + f"{sections[2]['acquire_var']}))\n"
            + f"(assert (< {sections[2]['acquire_var']} "
            + f"{sections[1]['release_var']}))\n"
        )
        refined = solve_schedule_smt_query(
            artifact, 0, extra_smt2=forced_overlap
        )
        self.assertEqual(refined["status"], "unsat")
        self.assertEqual(refined["solver_check_count"], 2)
        self.assertEqual(refined["refinement_round_count"], 1)
        self.assertEqual(
            refined["activated_refinements"],
            ["sync_exclusion_0_1"],
        )
        limited = solve_schedule_smt_query(
            artifact,
            0,
            extra_smt2=forced_overlap,
            max_refinement_rounds=0,
        )
        self.assertEqual(limited["status"], "refinement_limit")
        self.assertFalse(limited["exact_semantics"])
        eager = solve_schedule_smt_query(
            artifact,
            0,
            lazy_refinement=False,
            extra_smt2=forced_overlap,
        )
        self.assertEqual(eager["status"], "unsat")
        self.assertEqual(eager["solver_check_count"], 1)
        tampered = dict(artifact)
        tampered["relaxed_base_smt2"] += "(assert true)\n"
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            solve_schedule_smt_query(tampered, 0)

        with tempfile.TemporaryDirectory() as tmp:
            artifact_path = Path(tmp) / "schedule.jsonl"
            result_path = Path(tmp) / "result.json"
            certificate_path = Path(tmp) / "certificate.json"
            prefix_path = Path(tmp) / "schedule.prefix"
            artifact_path.write_text(json.dumps(artifact) + "\n")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "symcc_schedule_solve.py"),
                    str(artifact_path),
                    "--result-out", str(result_path),
                    "--certificate-out", str(certificate_path),
                    "--prefix-out", str(prefix_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            cli_result = json.loads(completed.stdout)
            self.assertEqual(cli_result["status"], "sat")
            self.assertEqual(
                json.loads(result_path.read_text())["status"], "sat"
            )
            self.assertTrue(verify_schedule_linear_extension_certificate(
                artifact, json.loads(certificate_path.read_text())
            ))
            self.assertEqual(
                prefix_path.read_text().split(),
                [str(tid) for tid in cli_result["replay_prefix"]],
            )

    def test_schedule_smt_allows_overlapping_rwlock_readers(self):
        artifact = schedule_smt_artifact(parse_schedule_trace(
            "0 1 rdlock 0xaa\n"
            "1 1 acquire 0xaa\n"
            "2 2 rdlock 0xaa\n"
            "3 2 acquire 0xaa\n"
            "4 1 rwunlock 0xaa\n"
            "5 2 rwunlock 0xaa\n"
        ))
        state = artifact["sync_state"]
        self.assertEqual(state["complete_section_count"], 2)
        self.assertEqual(state["exclusion_constraint_count"], 0)
        sections = {section["tid"]: section for section in state["sections"]}
        first = sections[1]
        second = sections[2]
        overlapping_readers = (
            artifact["base_smt2"]
            + f"(assert (< {first['acquire_var']} "
            + f"{second['acquire_var']}))\n"
            + f"(assert (< {second['acquire_var']} "
            + f"{first['release_var']}))\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(overlapping_readers)
        if result is not None:
            self.assertTrue(result.startswith("sat\n"))
        reader_writer = schedule_smt_artifact(parse_schedule_trace(
            "0 1 rdlock 0xaa\n"
            "1 1 acquire 0xaa\n"
            "2 2 wrlock 0xaa\n"
            "3 1 rwunlock 0xaa\n"
            "4 2 acquire 0xaa\n"
            "5 2 rwunlock 0xaa\n"
        ))
        self.assertEqual(
            reader_writer["sync_state"]["exclusion_constraint_count"],
            1,
        )

    def test_schedule_smt_trylock_failure_is_optional_busy_witness(self):
        artifact = schedule_smt_artifact(parse_schedule_trace(
            "0 1 lock 0xaa\n"
            "1 1 acquire 0xaa\n"
            "2 2 trylock 0xaa\n"
            "3 2 trylock_fail 0xaa\n"
            "4 1 unlock 0xaa\n"
        ))
        state = artifact["sync_state"]
        self.assertEqual(state["trylock_failure_count"], 1)
        self.assertEqual(
            state["trylock_assumptions"],
            ["sync_trylock_busy_0"],
        )
        section = state["sections"][0]
        trylock_attempt = state["trylock_failures"][0]["attempt_var"]
        busy = (
            artifact["base_smt2"]
            + "(assert sync_trylock_busy_0)\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(busy)
        if result is not None:
            self.assertTrue(result.startswith("sat\n"))
        impossible_busy = (
            artifact["base_smt2"]
            + "(assert sync_trylock_busy_0)\n"
            + f"(assert (< {section['release_var']} {trylock_attempt}))\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(impossible_busy)
        if result is not None:
            self.assertTrue(result.startswith("unsat\n"))

    def test_schedule_smt_condition_wait_split_and_wake_witness(self):
        artifact = schedule_smt_artifact(parse_schedule_trace(
            "0 1 lock M\n"
            "1 1 acquire M\n"
            "2 1 wait C\n"
            "3 1 wait_mutex_release M\n"
            "4 2 lock M\n"
            "5 2 acquire M\n"
            "6 2 signal C\n"
            "7 2 unlock M\n"
            "8 1 wait_mutex_acquire M\n"
            "9 1 wake C\n"
            "10 1 unlock M\n"
        ))
        self.assertEqual(
            artifact["encoding"],
            (
                "bounded-sc-partial-order-lock-condition-thread-state-v7-"
                "lazy-refinement"
            ),
        )
        state = artifact["sync_state"]
        self.assertEqual(state["complete_condition_wait_count"], 1)
        self.assertEqual(state["successful_condition_wait_count"], 1)
        self.assertEqual(
            state["condition_wake_assumptions"],
            ["cond_wake_signal_0"],
        )
        wait = state["condition_waits"][0]
        self.assertEqual(wait["mutex"], "m")
        self.assertTrue(wait["complete"])
        self.assertEqual(wait["wake_candidates"][0]["op"], "signal")
        self.assertEqual(
            [
                section["origin"]
                for section in state["sections"]
                if section["tid"] == 1
            ],
            ["lock", "condition_reacquire"],
        )
        witnessed = (
            artifact["base_smt2"]
            + "(assert cond_wake_signal_0)\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(witnessed)
        if result is not None:
            self.assertTrue(result.startswith("sat\n"))
        outside_wait_interval = (
            artifact["base_smt2"]
            + "(assert cond_wake_signal_0)\n"
            + f"(assert (< {wait['reacquire_var']} sync_ord_6))\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(outside_wait_interval)
        if result is not None:
            self.assertTrue(result.startswith("unsat\n"))

    def test_schedule_smt_signal_is_unique_but_broadcast_is_reusable(self):
        common = (
            "0 1 wait C\n"
            "1 1 wait_mutex_release M1\n"
            "2 2 wait C\n"
            "3 2 wait_mutex_release M2\n"
        )
        suffix = (
            "5 1 wait_mutex_acquire M1\n"
            "6 1 wake C\n"
            "7 2 wait_mutex_acquire M2\n"
            "8 2 wake C\n"
        )
        signal = schedule_smt_artifact(parse_schedule_trace(
            common + "4 3 signal C\n" + suffix
        ))
        signal_state = signal["sync_state"]
        self.assertEqual(
            signal_state["condition_wake_assumptions"],
            ["cond_wake_signal_0", "cond_wake_signal_1"],
        )
        self.assertEqual(signal_state["wake_witness_uniqueness_count"], 1)
        both_signal = (
            signal["base_smt2"]
            + "(assert cond_wake_signal_0)\n"
            + "(assert cond_wake_signal_1)\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(both_signal)
        if result is not None:
            self.assertTrue(result.startswith("unsat\n"))

        broadcast = schedule_smt_artifact(parse_schedule_trace(
            common + "4 3 broadcast C\n" + suffix
        ))
        self.assertEqual(
            broadcast["sync_state"]["wake_witness_uniqueness_count"],
            0,
        )
        both_broadcast = (
            broadcast["base_smt2"]
            + "(assert cond_wake_signal_0)\n"
            + "(assert cond_wake_signal_1)\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(both_broadcast)
        if result is not None:
            self.assertTrue(result.startswith("sat\n"))

    def test_schedule_smt_timed_wait_reacquires_mutex_without_wake_witness(self):
        artifact = schedule_smt_artifact(parse_schedule_trace(
            "0 1 lock M\n"
            "1 1 acquire M\n"
            "2 1 wait C\n"
            "3 1 wait_mutex_release M\n"
            "4 1 wait_mutex_acquire M\n"
            "5 1 wait_timeout C\n"
            "6 1 unlock M\n"
        ))
        state = artifact["sync_state"]
        self.assertEqual(state["complete_condition_wait_count"], 1)
        self.assertEqual(state["timed_out_condition_wait_count"], 1)
        self.assertEqual(state["condition_wake_assumptions"], [])
        self.assertEqual(state["complete_section_count"], 2)
        self.assertEqual(
            state["sections"][1]["origin"],
            "condition_reacquire",
        )
        result = evaluate_with_system_z3(
            artifact["base_smt2"] + "(check-sat)\n"
        )
        if result is not None:
            self.assertTrue(result.startswith("sat\n"))

    def test_condition_wait_updates_lockset_without_assuming_wake_hb(self):
        points = annotate_schedule(parse_schedule_trace(
            "0 1 lock M\n"
            "1 1 wait C\n"
            "2 1 wait_mutex_release M\n"
            "3 1 write X\n"
            "4 2 signal C\n"
            "5 1 wait_mutex_acquire M\n"
            "6 1 wake C\n"
            "7 1 read Y\n"
        ))
        by_seq = {point.event.seq: point for point in points}
        self.assertNotIn("m", by_seq[3].lockset)
        self.assertIn("m", by_seq[7].lockset)
        self.assertFalse(happens_before(by_seq[4], by_seq[6]))

    def test_schedule_smt_thread_create_and_join_operational_order(self):
        artifact = schedule_smt_artifact(parse_schedule_trace(
            "0 0 create 0x1\n"
            "1 1 thread_start 0x1\n"
            "2 1 write 0x100\n"
            "3 0 create_success 0x1 mapped=1\n"
            "4 0 join 0x1 mapped=1\n"
            "5 1 thread_exit 0x1\n"
            "6 0 join_success 0x1\n"
            "7 0 read 0x200\n"
        ))
        state = artifact["sync_state"]
        self.assertEqual(state["complete_thread_lifecycle_count"], 1)
        self.assertEqual(state["thread_spawn_constraint_count"], 1)
        self.assertEqual(state["join_completion_constraint_count"], 1)
        thread = state["thread_lifecycles"][0]
        join = state["joins"][0]
        self.assertTrue(thread["identity_consistent"])
        self.assertTrue(thread["complete"])
        self.assertTrue(join["complete"])

        normal = (
            artifact["base_smt2"]
            + f"(assert (< {thread['start_var']} "
            + f"{thread['create_outcome_var']}))\n"
            + f"(assert (< {join['join_var']} {thread['exit_var']}))\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(normal)
        if result is not None:
            self.assertTrue(result.startswith("sat\n"))

        start_before_create = (
            artifact["base_smt2"]
            + f"(assert (< {thread['start_var']} "
            + f"{thread['create_var']}))\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(start_before_create)
        if result is not None:
            self.assertTrue(result.startswith("unsat\n"))

        join_returns_before_exit = (
            artifact["base_smt2"]
            + f"(assert (< {join['outcome_var']} "
            + f"{thread['exit_var']}))\n"
            + "(check-sat)\n"
        )
        result = evaluate_with_system_z3(join_returns_before_exit)
        if result is not None:
            self.assertTrue(result.startswith("unsat\n"))

    def test_schedule_smt_incomplete_thread_evidence_stays_conservative(self):
        artifact = schedule_smt_artifact(parse_schedule_trace(
            "0 0 create 0x1\n"
            "1 0 create_fail 0x1\n"
            "2 0 create 0x2\n"
            "3 0 create_success 0x2 mapped=1\n"
            "4 2 thread_start 0x2\n"
            "5 0 join 0x2 mapped=1\n"
            "6 0 join_success 0x2\n"
            "7 0 join 0xdead mapped=0\n"
            "8 0 join_fail 0xdead\n"
        ))
        state = artifact["sync_state"]
        self.assertEqual(state["thread_lifecycle_count"], 2)
        self.assertEqual(state["failed_create_count"], 1)
        self.assertEqual(state["complete_thread_lifecycle_count"], 0)
        self.assertEqual(state["thread_spawn_constraint_count"], 1)
        self.assertEqual(state["join_completion_constraint_count"], 0)
        self.assertEqual(state["failed_join_count"], 1)
        self.assertFalse(state["joins"][1]["mapped"])
        result = evaluate_with_system_z3(
            artifact["base_smt2"] + "(check-sat)\n"
        )
        if result is not None:
            self.assertTrue(result.startswith("sat\n"))

    def test_thread_lifecycle_vector_clocks_suppress_joined_conflict(self):
        events = parse_schedule_trace(
            "0 0 create 0x1\n"
            "1 1 thread_start 0x1\n"
            "2 0 create_success 0x1 mapped=1\n"
            "3 1 write 0x100\n"
            "4 1 thread_exit 0x1\n"
            "5 0 join 0x1 mapped=1\n"
            "6 0 join_success 0x1\n"
            "7 0 read 0x100\n"
        )
        points = {
            point.event.seq: point for point in annotate_schedule(events)
        }
        self.assertTrue(happens_before(points[0], points[1]))
        self.assertTrue(happens_before(points[3], points[7]))
        self.assertEqual(classify_schedule_conflicts(events), ())
        unmapped = annotate_schedule(parse_schedule_trace(
            "0 1 thread_exit 0x1\n"
            "1 0 join_success 0x1 mapped=0\n"
        ))
        self.assertFalse(happens_before(unmapped[0], unmapped[1]))

    def test_schedule_smt_sync_state_can_be_disabled_and_legacy_materialized(self):
        artifact = schedule_smt_artifact(
            parse_schedule_trace(
                "0 1 lock 0xaa\n"
                "1 1 acquire 0xaa\n"
                "2 2 lock 0xaa\n"
                "3 1 unlock 0xaa\n"
                "4 2 acquire 0xaa\n"
                "5 2 unlock 0xaa\n"
            ),
            sync_state=False,
        )
        self.assertFalse(artifact["sync_state"]["enabled"])
        self.assertNotIn("sync_ord_", artifact["base_smt2"])
        self.assertIn(
            "mutex_and_rwlock_ownership_state",
            artifact["not_encoded"],
        )
        for schema in (
            "symcc-schedule-smt-v1",
            "symcc-schedule-smt-v2",
            "symcc-schedule-smt-v3",
            "symcc-schedule-smt-v4",
            "symcc-schedule-smt-v5",
            "symcc-schedule-smt-v6",
        ):
            legacy = dict(artifact)
            legacy["schema"] = schema
            result = evaluate_with_system_z3(
                materialize_schedule_smt_query(legacy, 0)
            )
            if result is not None:
                self.assertTrue(result.startswith("sat\n"))

    def test_lockset_suppresses_protected_memory_conflict(self):
        events = parse_schedule_trace(
            "0 1 lock 0xaa\n"
            "1 1 write 0x100\n"
            "2 1 unlock 0xaa\n"
            "3 2 lock 0xaa\n"
            "4 2 read 0x100\n"
            "5 2 unlock 0xaa\n"
        )
        self.assertEqual(classify_schedule_conflicts(events), ())

    def test_rwunlock_releases_lockset_before_later_memory_access(self):
        events = parse_schedule_trace(
            "0 1 rdlock 0xaa\n"
            "1 1 acquire 0xaa\n"
            "2 1 rwunlock 0xaa\n"
            "3 1 write 0x100\n"
            "4 2 rdlock 0xaa\n"
            "5 2 acquire 0xaa\n"
            "6 2 read 0x100\n"
        )
        conflicts = classify_schedule_conflicts(events)
        memory_conflicts = [
            conflict for conflict in conflicts
            if conflict.kind == "memory"
        ]
        self.assertEqual(len(memory_conflicts), 1)

    def test_prefix_file_and_preload_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix_path = os.path.join(tmp, "prefix")
            self.assertTrue(write_schedule_prefix(prefix_path, [2, "1", -1]))
            self.assertEqual(Path(prefix_path).read_text().split(), ["2", "1"])
        self.assertEqual(normalize_schedule_prefix("2, 1;bad"), (2, 1))
        self.assertEqual(prepend_ld_preload("/tmp/a.so", "/tmp/b.so:/tmp/a.so"),
                         "/tmp/a.so:/tmp/b.so")


class ScheduleRuntimeTests(unittest.TestCase):
    def _build_schedule_runtime(self, tmp_path: Path) -> Path:
        cc = os.environ.get("CC", "cc")
        so_path = tmp_path / "libsymcc_schedule_rt.so"
        subprocess.run(
            [
                cc, "-shared", "-fPIC", "-pthread",
                str(ROOT / "util" / "symcc_schedule_rt.c"),
                "-ldl", "-o", str(so_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return so_path

    def test_preload_runtime_records_and_replays_mutex_prefix(self):
        cc = os.environ.get("CC", "cc")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            so_path = self._build_schedule_runtime(tmp_path)
            bin_path = tmp_path / "schedule_target"
            prefix_path = tmp_path / "prefix"
            trace_path = tmp_path / "trace"
            src_path = tmp_path / "schedule_target.c"
            src_path.write_text(textwrap.dedent(r"""
                #include <pthread.h>
                #include <stdatomic.h>
                #include <unistd.h>

                static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
                static atomic_int ready = 0;

                static void *worker(void *arg) {
                  (void)arg;
                  atomic_fetch_add(&ready, 1);
                  while (atomic_load(&ready) < 3) {
                  }
                  pthread_mutex_lock(&lock);
                  usleep(1000);
                  pthread_mutex_unlock(&lock);
                  return 0;
                }

                int main(void) {
                  pthread_t first, second;
                  pthread_create(&first, 0, worker, 0);
                  pthread_create(&second, 0, worker, 0);
                  while (atomic_load(&ready) < 2) {
                  }
                  atomic_store(&ready, 3);
                  pthread_join(first, 0);
                  pthread_join(second, 0);
                  return 0;
                }
            """))
            subprocess.run(
                [cc, "-pthread", str(src_path), "-o", str(bin_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            prefix_path.write_text("2\n")
            env = os.environ.copy()
            env.update({
                "LD_PRELOAD": str(so_path),
                "SYMCC_DPOR": "1",
                "SYMCC_SCHEDULE_TRACE": str(trace_path),
                "SYMCC_SCHEDULE_PREFIX": str(prefix_path),
                "SYMCC_SCHEDULE_WAIT_MS": "1000",
                "SYMCC_SCHEDULE_ENABLED": "1",
                "SYMCC_SCHEDULE_ENABLED_SETTLE_US": "10000",
            })
            subprocess.run(
                [str(bin_path)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            events = parse_schedule_trace(trace_path.read_text())
            controlled = [
                event for event in events
                if event.op == "lock"
            ]
            self.assertGreaterEqual(len(controlled), 2)
            self.assertEqual(controlled[0].tid, 2)
            ready_sets = runtime_ready_evidence(events)
            self.assertIn(0, ready_sets)
            self.assertEqual(ready_sets[0]["chosen"], 2)
            self.assertTrue(ready_sets[0]["complete"])
            self.assertTrue({1, 2}.issubset(
                ready_sets[0]["threads"]
            ))
            self.assertTrue(any(
                offer["tid"] == 2 and offer["op"] == "lock"
                for offer in ready_sets[0]["offers"]
            ))
            operational = operational_enabledness_certificate(events)
            self.assertTrue(
                verify_operational_enabledness_certificate(operational)
            )
            self.assertTrue(
                operational["bounded_terminal_execution_witnessed"]
            )
            artifact = schedule_smt_artifact(events)
            self.assertGreaterEqual(
                artifact["sync_state"]["complete_section_count"],
                2,
            )
            self.assertGreaterEqual(
                artifact["sync_state"]["exclusion_constraint_count"],
                1,
            )
            result = evaluate_with_system_z3(
                artifact["base_smt2"] + "(check-sat)\n"
            )
            if result is not None:
                self.assertTrue(result.startswith("sat\n"))

            topology_prefix = tmp_path / "topology.prefix"
            replay_trace = tmp_path / "topology.trace"
            self.assertTrue(write_schedule_linear_extension_prefix(
                str(topology_prefix),
                artifact,
            ))
            replay_values = [
                int(value)
                for value in topology_prefix.read_text().split()
            ]
            self.assertTrue(replay_values)
            env["SYMCC_SCHEDULE_PREFIX"] = str(topology_prefix)
            env["SYMCC_SCHEDULE_TRACE"] = str(replay_trace)
            subprocess.run(
                [str(bin_path)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            replayed_controlled = [
                event
                for event in parse_schedule_trace(replay_trace.read_text())
                if event.controlled
            ]
            self.assertTrue(replayed_controlled)
            self.assertEqual(
                replayed_controlled[0].tid,
                replay_values[0],
            )

    def test_preload_runtime_records_condition_mutex_lifecycle(self):
        cc = os.environ.get("CC", "cc")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            so_path = self._build_schedule_runtime(tmp_path)
            bin_path = tmp_path / "condition_target"
            trace_path = tmp_path / "trace"
            src_path = tmp_path / "condition_target.c"
            src_path.write_text(textwrap.dedent(r"""
                #include <pthread.h>
                #include <stdatomic.h>

                static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
                static pthread_cond_t condition = PTHREAD_COND_INITIALIZER;
                static atomic_int waiting = 0;
                static int predicate = 0;

                static void *waiter(void *arg) {
                  (void)arg;
                  pthread_mutex_lock(&lock);
                  atomic_store(&waiting, 1);
                  while (!predicate)
                    pthread_cond_wait(&condition, &lock);
                  pthread_mutex_unlock(&lock);
                  return 0;
                }

                int main(void) {
                  pthread_t thread;
                  if (pthread_create(&thread, 0, waiter, 0) != 0)
                    return 2;
                  while (!atomic_load(&waiting)) {
                  }
                  pthread_mutex_lock(&lock);
                  predicate = 1;
                  pthread_cond_signal(&condition);
                  pthread_mutex_unlock(&lock);
                  pthread_join(thread, 0);
                  return 0;
                }
            """))
            subprocess.run(
                [cc, "-pthread", str(src_path), "-o", str(bin_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            env.update({
                "LD_PRELOAD": str(so_path),
                "SYMCC_DPOR": "1",
                "SYMCC_SCHEDULE_TRACE": str(trace_path),
                "SYMCC_SCHEDULE_WAIT_MS": "1000",
            })
            subprocess.run(
                [str(bin_path)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            events = parse_schedule_trace(trace_path.read_text())
            wait_event = next(event for event in events if event.op == "wait")
            waiter_ops = [
                event.op for event in events if event.tid == wait_event.tid
            ]
            for op in (
                "wait_mutex_release",
                "wait_mutex_acquire",
                "wake",
            ):
                self.assertIn(op, waiter_ops)
            self.assertLess(
                waiter_ops.index("wait"),
                waiter_ops.index("wait_mutex_release"),
            )
            self.assertLess(
                waiter_ops.index("wait_mutex_release"),
                waiter_ops.index("wait_mutex_acquire"),
            )
            self.assertLess(
                waiter_ops.index("wait_mutex_acquire"),
                waiter_ops.index("wake"),
            )

            artifact = schedule_smt_artifact(events)
            state = artifact["sync_state"]
            self.assertEqual(state["complete_condition_wait_count"], 1)
            self.assertEqual(
                state["condition_wake_assumptions"],
                ["cond_wake_signal_0"],
            )
            self.assertGreaterEqual(state["complete_section_count"], 3)
            result = evaluate_with_system_z3(
                artifact["base_smt2"]
                + "(assert cond_wake_signal_0)\n"
                + "(check-sat)\n"
            )
            if result is not None:
                self.assertTrue(result.startswith("sat\n"))

    def test_preload_runtime_records_timed_wait_mutex_reacquire(self):
        cc = os.environ.get("CC", "cc")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            so_path = self._build_schedule_runtime(tmp_path)
            bin_path = tmp_path / "timed_condition_target"
            trace_path = tmp_path / "trace"
            src_path = tmp_path / "timed_condition_target.c"
            src_path.write_text(textwrap.dedent(r"""
                #include <errno.h>
                #include <pthread.h>
                #include <time.h>

                static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
                static pthread_cond_t condition = PTHREAD_COND_INITIALIZER;

                int main(void) {
                  struct timespec deadline;
                  clock_gettime(CLOCK_REALTIME, &deadline);
                  deadline.tv_nsec += 10000000;
                  if (deadline.tv_nsec >= 1000000000) {
                    deadline.tv_sec++;
                    deadline.tv_nsec -= 1000000000;
                  }
                  pthread_mutex_lock(&lock);
                  int rc = pthread_cond_timedwait(
                      &condition, &lock, &deadline);
                  pthread_mutex_unlock(&lock);
                  return rc == ETIMEDOUT ? 0 : 2;
                }
            """))
            subprocess.run(
                [cc, "-pthread", str(src_path), "-o", str(bin_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            env.update({
                "LD_PRELOAD": str(so_path),
                "SYMCC_DPOR": "1",
                "SYMCC_SCHEDULE_TRACE": str(trace_path),
            })
            subprocess.run(
                [str(bin_path)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            events = parse_schedule_trace(trace_path.read_text())
            operations = [event.op for event in events]
            release = operations.index("wait_mutex_release")
            acquire = operations.index("wait_mutex_acquire")
            timeout = operations.index("wait_timeout")
            self.assertLess(release, acquire)
            self.assertLess(acquire, timeout)

            state = schedule_smt_artifact(events)["sync_state"]
            self.assertEqual(state["timed_out_condition_wait_count"], 1)
            self.assertEqual(state["condition_wake_assumptions"], [])
            self.assertEqual(state["complete_section_count"], 2)

    def test_preload_runtime_records_stable_create_join_lifecycle(self):
        cc = os.environ.get("CC", "cc")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            so_path = self._build_schedule_runtime(tmp_path)
            bin_path = tmp_path / "thread_lifecycle_target"
            trace_path = tmp_path / "trace"
            src_path = tmp_path / "thread_lifecycle_target.c"
            src_path.write_text(textwrap.dedent(r"""
                #include <pthread.h>
                #include <stdint.h>

                static void *worker(void *arg) {
                  (void)arg;
                  pthread_exit((void *)(uintptr_t)42);
                }

                int main(void) {
                  pthread_t thread;
                  void *result = 0;
                  if (pthread_create(&thread, 0, worker, 0) != 0)
                    return 2;
                  if (pthread_join(thread, &result) != 0)
                    return 3;
                  return (uintptr_t)result == 42 ? 0 : 4;
                }
            """))
            subprocess.run(
                [cc, "-pthread", str(src_path), "-o", str(bin_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            env.update({
                "LD_PRELOAD": str(so_path),
                "SYMCC_DPOR": "1",
                "SYMCC_SCHEDULE_TRACE": str(trace_path),
            })
            subprocess.run(
                [str(bin_path)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            events = parse_schedule_trace(trace_path.read_text())
            by_op = {
                op: [event for event in events if event.op == op]
                for op in (
                    "create",
                    "create_success",
                    "thread_start",
                    "thread_exit",
                    "join",
                    "join_success",
                )
            }
            self.assertTrue(all(len(rows) == 1 for rows in by_op.values()))
            objects = {
                rows[0].obj for rows in by_op.values()
            }
            self.assertEqual(len(objects), 1)
            self.assertIn("mapped=1", by_op["create_success"][0].tags)
            self.assertIn("mapped=1", by_op["join"][0].tags)
            self.assertIn("mapped=1", by_op["join_success"][0].tags)

            artifact = schedule_smt_artifact(events)
            state = artifact["sync_state"]
            self.assertEqual(state["complete_thread_lifecycle_count"], 1)
            self.assertEqual(state["join_completion_constraint_count"], 1)
            self.assertTrue(state["thread_lifecycles"][0]["complete"])
            self.assertTrue(state["joins"][0]["complete"])
            result = evaluate_with_system_z3(
                artifact["base_smt2"] + "(check-sat)\n"
            )
            if result is not None:
                self.assertTrue(result.startswith("sat\n"))

    def test_preload_runtime_retires_detached_thread_identity(self):
        cc = os.environ.get("CC", "cc")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            so_path = self._build_schedule_runtime(tmp_path)
            bin_path = tmp_path / "detach_target"
            trace_path = tmp_path / "trace"
            src_path = tmp_path / "detach_target.c"
            src_path.write_text(textwrap.dedent(r"""
                #include <pthread.h>
                #include <stdatomic.h>
                #include <unistd.h>

                static atomic_int completed;

                static void *worker(void *arg) {
                  (void)arg;
                  usleep(10000);
                  atomic_store_explicit(
                      &completed, 1, memory_order_release);
                  return 0;
                }

                int main(void) {
                  pthread_t thread;
                  if (pthread_create(&thread, 0, worker, 0) != 0)
                    return 2;
                  if (pthread_detach(thread) != 0)
                    return 3;
                  while (!atomic_load_explicit(
                      &completed, memory_order_acquire)) {
                  }
                  usleep(20000);
                  return 0;
                }
            """))
            subprocess.run(
                [cc, "-pthread", str(src_path), "-o", str(bin_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            env.update({
                "LD_PRELOAD": str(so_path),
                "SYMCC_DPOR": "1",
                "SYMCC_SCHEDULE_TRACE": str(trace_path),
            })
            subprocess.run(
                [str(bin_path)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            events = parse_schedule_trace(trace_path.read_text())
            artifact = schedule_smt_artifact(events)
            state = artifact["sync_state"]
            self.assertEqual(state["detach_count"], 1)
            self.assertEqual(state["successful_detach_count"], 1)
            self.assertEqual(state["thread_retire_count"], 1)
            self.assertEqual(
                state["thread_retirement_constraint_count"], 2
            )
            self.assertTrue(state["detaches"][0]["mapped"])
            self.assertTrue(state["detaches"][0]["complete"])
            self.assertTrue(state["thread_lifecycles"][0]["detached"])
            self.assertTrue(state["thread_retirements"][0]["complete"])
            self.assertEqual(
                state["thread_retirements"][0]["cause"], "detach"
            )
            result = evaluate_with_system_z3(
                artifact["base_smt2"] + "(check-sat)\n"
            )
            if result is not None:
                self.assertTrue(result.startswith("sat\n"))

    def test_preload_runtime_preserves_target_after_cancelled_join(self):
        cc = os.environ.get("CC", "cc")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            so_path = self._build_schedule_runtime(tmp_path)
            bin_path = tmp_path / "cancelled_join_target"
            trace_path = tmp_path / "trace"
            prefix_path = tmp_path / "prefix"
            src_path = tmp_path / "cancelled_join_target.c"
            src_path.write_text(textwrap.dedent(r"""
                #include <pthread.h>
                #include <stdint.h>
                #include <unistd.h>

                static pthread_mutex_t lock =
                    PTHREAD_MUTEX_INITIALIZER;
                static pthread_cond_t condition =
                    PTHREAD_COND_INITIALIZER;
                static int release_target;
                static pthread_t target;

                static void *worker(void *arg) {
                  (void)arg;
                  pthread_mutex_lock(&lock);
                  while (!release_target)
                    pthread_cond_wait(&condition, &lock);
                  pthread_mutex_unlock(&lock);
                  return (void *)(uintptr_t)7;
                }

                static void *joiner(void *arg) {
                  (void)arg;
                  return (void *)(uintptr_t)pthread_join(target, 0);
                }

                int main(void) {
                  pthread_t waiter;
                  if (pthread_create(&target, 0, worker, 0) != 0)
                    return 2;
                  if (pthread_create(&waiter, 0, joiner, 0) != 0)
                    return 3;
                  usleep(100000);
                  if (pthread_cancel(waiter) != 0)
                    return 4;
                  void *cancel_result = 0;
                  if (pthread_join(waiter, &cancel_result) != 0)
                    return 5;
                  if (cancel_result != PTHREAD_CANCELED)
                    return 6;
                  pthread_mutex_lock(&lock);
                  release_target = 1;
                  pthread_cond_broadcast(&condition);
                  pthread_mutex_unlock(&lock);
                  void *target_result = 0;
                  if (pthread_join(target, &target_result) != 0)
                    return 7;
                  return target_result == (void *)(uintptr_t)7 ? 0 : 8;
                }
            """))
            subprocess.run(
                [cc, "-pthread", str(src_path), "-o", str(bin_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            prefix_path.write_text("0\n")
            env.update({
                "LD_PRELOAD": str(so_path),
                "SYMCC_DPOR": "1",
                "SYMCC_SCHEDULE_TRACE": str(trace_path),
                "SYMCC_SCHEDULE_PREFIX": str(prefix_path),
                "SYMCC_SCHEDULE_WAIT_MS": "1000",
            })
            subprocess.run(
                [str(bin_path)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            events = parse_schedule_trace(trace_path.read_text())
            state = schedule_smt_artifact(events)["sync_state"]
            self.assertEqual(state["cancel_count"], 1)
            self.assertEqual(state["successful_cancel_count"], 1)
            self.assertEqual(state["cancelled_join_count"], 1)
            self.assertEqual(state["successful_join_count"], 2)
            cancelled = next(
                join for join in state["joins"]
                if join["outcome_op"] == "join_cancelled"
            )
            later = next(
                join for join in state["joins"]
                if (
                    join["target_object"]
                    == cancelled["target_object"]
                    and join["outcome_op"] == "join_success"
                )
            )
            self.assertTrue(cancelled["mapped"])
            self.assertFalse(cancelled["complete"])
            self.assertTrue(later["mapped"])
            self.assertTrue(later["complete"])
            self.assertEqual(state["thread_retire_count"], 2)
            self.assertEqual(
                state["thread_retirement_constraint_count"], 4
            )
            result = evaluate_with_system_z3(
                schedule_smt_artifact(events)["base_smt2"]
                + "(check-sat)\n"
            )
            if result is not None:
                self.assertTrue(result.startswith("sat\n"))

    def test_preload_runtime_reuses_detached_registry_slots(self):
        cc = os.environ.get("CC", "cc")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            so_path = self._build_schedule_runtime(tmp_path)
            bin_path = tmp_path / "detach_stress_target"
            trace_path = tmp_path / "trace"
            src_path = tmp_path / "detach_stress_target.c"
            src_path.write_text(textwrap.dedent(r"""
                #include <pthread.h>
                #include <sched.h>
                #include <stdatomic.h>
                #include <unistd.h>

                #define THREAD_COUNT 4104U
                static atomic_uint completed;

                static void *worker(void *arg) {
                  (void)arg;
                  atomic_fetch_add_explicit(
                      &completed, 1, memory_order_release);
                  return 0;
                }

                int main(void) {
                  pthread_attr_t attr;
                  if (pthread_attr_init(&attr) != 0)
                    return 2;
                  if (pthread_attr_setdetachstate(
                      &attr, PTHREAD_CREATE_DETACHED) != 0)
                    return 3;
                  for (unsigned i = 0; i < THREAD_COUNT; ++i) {
                    pthread_t thread;
                    if (pthread_create(
                        &thread, &attr, worker, 0) != 0)
                      return 4;
                    while (atomic_load_explicit(
                        &completed, memory_order_acquire) <= i)
                      sched_yield();
                  }
                  pthread_attr_destroy(&attr);
                  usleep(100000);
                  return 0;
                }
            """))
            subprocess.run(
                [
                    cc, "-O2", "-pthread", str(src_path),
                    "-o", str(bin_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            env.update({
                "LD_PRELOAD": str(so_path),
                "SYMCC_DPOR": "1",
                "SYMCC_SCHEDULE_TRACE": str(trace_path),
            })
            subprocess.run(
                [str(bin_path)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            trace = trace_path.read_text()
            self.assertNotIn("create_success", "\n".join(
                line for line in trace.splitlines()
                if "mapped=0" in line
            ))
            self.assertEqual(
                trace.count("create_success"), 4104
            )
            self.assertEqual(
                trace.count("thread_retire"), 4104
            )

    def test_memory_stack_filter_skips_current_thread_stack(self):
        cc = os.environ.get("CC", "cc")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            so_path = self._build_schedule_runtime(tmp_path)
            bin_path = tmp_path / "memory_filter_target"
            trace_path = tmp_path / "trace"
            addrs_path = tmp_path / "addresses"
            src_path = tmp_path / "memory_filter_target.c"
            src_path.write_text(textwrap.dedent(r"""
                #include <dlfcn.h>
                #include <stdint.h>
                #include <stdio.h>
                #include <stdlib.h>

                typedef void (*notify_fn)(const void *, size_t);
                static int global_value;

                int main(void) {
                  int local_value = 0;
                  const char *path = getenv("ADDR_OUT");
                  FILE *out = fopen(path, "w");
                  if (!out) {
                    return 2;
                  }
                  fprintf(out, "%p %p\n", (void *)&local_value,
                          (void *)&global_value);
                  fclose(out);
                  notify_fn notify_write = (notify_fn)dlsym(
                      RTLD_DEFAULT, "_sym_notify_schedule_write");
                  notify_fn notify_read = (notify_fn)dlsym(
                      RTLD_DEFAULT, "_sym_notify_schedule_read");
                  if (!notify_write || !notify_read) {
                    return 3;
                  }
                  notify_write(&local_value, sizeof(local_value));
                  notify_read(&local_value, sizeof(local_value));
                  notify_write(&global_value, sizeof(global_value));
                  notify_read(&global_value, sizeof(global_value));
                  return 0;
                }
            """))
            subprocess.run(
                [cc, str(src_path), "-ldl", "-o", str(bin_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            env.update({
                "LD_PRELOAD": str(so_path),
                "SYMCC_DPOR": "1",
                "SYMCC_SCHEDULE_TRACE": str(trace_path),
                "SYMCC_SCHEDULE_MEMORY": "1",
                "SYMCC_SCHEDULE_MEMORY_FILTER": "stack",
                "SYMCC_SCHEDULE_MEMORY_BYTES": "4",
                "ADDR_OUT": str(addrs_path),
            })
            subprocess.run(
                [str(bin_path)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            local_raw, global_raw = addrs_path.read_text().split()
            local = int(local_raw, 16)
            global_addr = int(global_raw, 16)
            rows = [
                event for event in parse_schedule_trace(trace_path.read_text())
                if event.memory
            ]
            objects = {int(event.obj, 16) for event in rows}
            self.assertFalse(any(local <= obj < local + 4 for obj in objects))
            self.assertTrue(any(global_addr <= obj < global_addr + 4
                                for obj in objects))

    def test_memory_provenance_tags_are_emitted_for_memory_rows(self):
        cc = os.environ.get("CC", "cc")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            so_path = self._build_schedule_runtime(tmp_path)
            bin_path = tmp_path / "memory_provenance_target"
            trace_path = tmp_path / "trace"
            addrs_path = tmp_path / "addresses"
            src_path = tmp_path / "memory_provenance_target.c"
            src_path.write_text(textwrap.dedent(r"""
                #include <dlfcn.h>
                #include <stdint.h>
                #include <stdio.h>
                #include <stdlib.h>

                typedef void (*notify_fn)(const void *, size_t);
                static int global_value;

                int main(void) {
                  int local_value = 0;
                  const char *path = getenv("ADDR_OUT");
                  FILE *out = fopen(path, "w");
                  if (!out) {
                    return 2;
                  }
                  fprintf(out, "%p %p\n", (void *)&local_value,
                          (void *)&global_value);
                  fclose(out);
                  notify_fn notify_write = (notify_fn)dlsym(
                      RTLD_DEFAULT, "_sym_notify_schedule_write");
                  if (!notify_write) {
                    return 3;
                  }
                  notify_write(&local_value, sizeof(local_value));
                  notify_write(&global_value, sizeof(global_value));
                  return 0;
                }
            """))
            subprocess.run(
                [cc, str(src_path), "-ldl", "-o", str(bin_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            env.update({
                "LD_PRELOAD": str(so_path),
                "SYMCC_DPOR": "1",
                "SYMCC_SCHEDULE_TRACE": str(trace_path),
                "SYMCC_SCHEDULE_MEMORY": "1",
                "SYMCC_SCHEDULE_MEMORY_PROVENANCE": "1",
                "SYMCC_SCHEDULE_MEMORY_BYTES": "4",
                "ADDR_OUT": str(addrs_path),
            })
            subprocess.run(
                [str(bin_path)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            local_raw, global_raw = addrs_path.read_text().split()
            local = int(local_raw, 16)
            global_addr = int(global_raw, 16)
            events = [
                event for event in parse_schedule_trace(trace_path.read_text())
                if event.memory
            ]
            by_object = {int(event.obj, 16): event for event in events}
            local_tags = [
                event.tags for obj, event in by_object.items()
                if local <= obj < local + 4
            ]
            global_tags = [
                event.tags for obj, event in by_object.items()
                if global_addr <= obj < global_addr + 4
            ]
            self.assertTrue(any("prov=stack" in tags for tags in local_tags))
            self.assertTrue(any(any(tag.startswith("prov=") for tag in tags)
                                for tags in global_tags))

    def test_memory_owner_filter_emits_compressed_shared_transition(self):
        cc = os.environ.get("CC", "cc")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            so_path = self._build_schedule_runtime(tmp_path)
            bin_path = tmp_path / "memory_owner_target"
            trace_path = tmp_path / "trace"
            addrs_path = tmp_path / "addresses"
            src_path = tmp_path / "memory_owner_target.c"
            src_path.write_text(textwrap.dedent(r"""
                #include <dlfcn.h>
                #include <pthread.h>
                #include <stdatomic.h>
                #include <stdint.h>
                #include <stdio.h>
                #include <stdlib.h>

                typedef void (*notify_fn)(const void *, size_t);
                static int shared_value;
                static atomic_int ready = 0;
                static notify_fn notify_write;
                static notify_fn notify_read;

                static void *writer(void *arg) {
                  (void)arg;
                  notify_write(&shared_value, sizeof(shared_value));
                  atomic_store(&ready, 1);
                  return 0;
                }

                static void *reader(void *arg) {
                  (void)arg;
                  while (atomic_load(&ready) == 0) {
                  }
                  notify_read(&shared_value, sizeof(shared_value));
                  return 0;
                }

                int main(void) {
                  const char *path = getenv("ADDR_OUT");
                  FILE *out = fopen(path, "w");
                  if (!out) {
                    return 2;
                  }
                  fprintf(out, "%p\n", (void *)&shared_value);
                  fclose(out);
                  notify_write = (notify_fn)dlsym(
                      RTLD_DEFAULT, "_sym_notify_schedule_write");
                  notify_read = (notify_fn)dlsym(
                      RTLD_DEFAULT, "_sym_notify_schedule_read");
                  if (!notify_write || !notify_read) {
                    return 3;
                  }
                  pthread_t first;
                  pthread_t second;
                  if (pthread_create(&first, 0, writer, 0) != 0) {
                    return 4;
                  }
                  if (pthread_create(&second, 0, reader, 0) != 0) {
                    return 5;
                  }
                  pthread_join(first, 0);
                  pthread_join(second, 0);
                  return 0;
                }
            """))
            subprocess.run(
                [cc, "-pthread", str(src_path), "-ldl", "-o", str(bin_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            env.update({
                "LD_PRELOAD": str(so_path),
                "SYMCC_DPOR": "1",
                "SYMCC_SCHEDULE_TRACE": str(trace_path),
                "SYMCC_SCHEDULE_MEMORY": "1",
                "SYMCC_SCHEDULE_MEMORY_FILTER": "owner",
                "SYMCC_SCHEDULE_MEMORY_BYTES": "4",
                "ADDR_OUT": str(addrs_path),
            })
            subprocess.run(
                [str(bin_path)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            shared = int(addrs_path.read_text().strip(), 16)
            rows = [
                event for event in parse_schedule_trace(trace_path.read_text())
                if event.memory and shared <= int(event.obj, 16) < shared + 4
            ]
            self.assertTrue(any(event.op == "write" for event in rows))
            self.assertTrue(any(event.op == "read" for event in rows))
            self.assertGreaterEqual(len({event.tid for event in rows}), 2)


if __name__ == "__main__":
    unittest.main()
