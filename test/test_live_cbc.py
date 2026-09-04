# RUN: python3 %s

import itertools
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402
from live_state_frontier import PersistentLiveStateFrontier  # noqa: E402
from live_state_search import (  # noqa: E402
    LiveProgramGraph,
    LiveStateSearchFeatures,
    LiveStateSearchPolicy,
    search_decision_token,
)


def sequential_program(
    branches=4,
    *,
    shared_input=False,
    correlated_assume=False,
    repeated_site=False,
):
    instructions = []
    for index in range(branches):
        instructions.extend((
            {"op": "input", "dst": f"x{index}", "offset": index},
            {
                "op": "binary",
                "operator": "eq",
                "dst": f"c{index}",
                "left": {"var": "x0" if shared_input else f"x{index}"},
                "right": {"const": 65 + index, "bits": 8},
                "bits": 1,
            },
        ))
    if correlated_assume:
        instructions.extend((
            {
                "op": "binary",
                "operator": "eq",
                "dst": "inputs_equal",
                "left": {"var": "x0"},
                "right": {"var": "x1"},
                "bits": 1,
            },
            {"op": "assume", "condition": {"var": "inputs_equal"}},
        ))

    def site(index):
        return 100 if repeated_site else 100 + index

    blocks = {
        "entry": instructions + [{
            "op": "branch",
            "condition": {"var": "c0"},
            "true": "t0",
            "false": "f0",
            "site": site(0),
        }],
    }
    for index in range(branches):
        successor = f"b{index + 1}" if index + 1 < branches else "exit"
        blocks[f"t{index}"] = [{"op": "jump", "target": successor}]
        blocks[f"f{index}"] = [{"op": "jump", "target": successor}]
        if index + 1 < branches:
            blocks[successor] = [{
                "op": "branch",
                "condition": {"var": f"c{index + 1}"},
                "true": f"t{index + 1}",
                "false": f"f{index + 1}",
                "site": site(index + 1),
            }]
    blocks["exit"] = [{"op": "halt", "value": 0}]
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": branches,
        "functions": {
            "main": {"entry": "entry", "blocks": blocks},
        },
    }


def nested_program():
    program = sequential_program(2)
    blocks = program["functions"]["main"]["blocks"]
    blocks["entry"][-1].update(true="inner", false="exit")
    blocks["inner"] = blocks.pop("b1")
    blocks["t0"] = [{"op": "jump", "target": "inner"}]
    blocks["f0"] = [{"op": "jump", "target": "exit"}]
    return program


class CBCStaticAnalysisTests(unittest.TestCase):
    def test_independent_branches_form_one_compatible_group(self):
        graph = LiveProgramGraph(sequential_program(3), cbc_enabled=True)
        all_true = tuple(
            search_decision_token(100 + index, True) for index in range(3)
        )
        mixed = (
            search_decision_token(100, True),
            search_decision_token(101, False),
            search_decision_token(102, True),
        )
        accepted = graph.cbc_guidance(all_true)
        rejected = graph.cbc_guidance(mixed)
        assert accepted is not None and rejected is not None
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.compatible_groups, 1)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.inconsistent_groups, 1)
        self.assertEqual(graph.cbc_telemetry()["branches_analyzable"], 3)

    def test_shared_data_control_dependency_and_assume_prevent_grouping(self):
        cases = (
            sequential_program(3, shared_input=True),
            nested_program(),
            sequential_program(2, correlated_assume=True),
        )
        paths = (
            (
                search_decision_token(100, True),
                search_decision_token(101, False),
                search_decision_token(102, True),
            ),
            (
                search_decision_token(100, True),
                search_decision_token(101, False),
            ),
            (
                search_decision_token(100, True),
                search_decision_token(101, False),
            ),
        )
        for program, path in zip(cases, paths):
            with self.subTest(program=program):
                guidance = LiveProgramGraph(
                    program, cbc_enabled=True,
                ).cbc_guidance(path)
                assert guidance is not None
                self.assertTrue(guidance.accepted)
                self.assertEqual(guidance.compatible_groups, 0)

    def test_unknown_memory_and_ambiguous_sites_fail_open(self):
        program = sequential_program(2)
        entry = program["functions"]["main"]["blocks"]["entry"]
        entry.insert(0, {
            "op": "load", "dst": "loaded", "address": 0, "bits": 8,
        })
        entry[-1]["condition"] = {"var": "loaded"}
        graph = LiveProgramGraph(program, cbc_enabled=True)
        guidance = graph.cbc_guidance((
            search_decision_token(100, True),
            search_decision_token(101, False),
        ))
        assert guidance is not None
        self.assertTrue(guidance.accepted)
        self.assertEqual(guidance.analyzable_decisions, 1)
        self.assertEqual(guidance.compatible_groups, 0)

        ambiguous = LiveProgramGraph(
            sequential_program(2, repeated_site=True), cbc_enabled=True,
        ).cbc_guidance((
            search_decision_token(100, True),
            search_decision_token(100, False),
        ))
        assert ambiguous is not None
        self.assertTrue(ambiguous.accepted)
        self.assertEqual(ambiguous.recognized_decisions, 0)
        self.assertEqual(ambiguous.ambiguous_tokens, 2)

        interprocedural = sequential_program(2)
        interprocedural["functions"]["main"]["blocks"]["t0"].insert(0, {
            "op": "call", "function": "opaque", "args": [], "dst": "result",
        })
        call_guidance = LiveProgramGraph(
            interprocedural, cbc_enabled=True,
        ).cbc_guidance((
            search_decision_token(100, True),
            search_decision_token(101, False),
        ))
        assert call_guidance is not None
        self.assertTrue(call_guidance.accepted)
        self.assertEqual(call_guidance.analyzable_decisions, 0)
        self.assertEqual(call_guidance.compatible_groups, 0)

    def test_analysis_budgets_fail_open(self):
        oversized = sequential_program(6)
        graph = LiveProgramGraph(
            oversized,
            cbc_enabled=True,
            cbc_max_function_nodes=16,
        )
        telemetry = graph.cbc_telemetry()
        self.assertEqual(telemetry["functions_admitted"], 0)
        self.assertEqual(telemetry["functions_skipped_oversized"], 1)
        guidance = graph.cbc_guidance((search_decision_token(100, True),))
        assert guidance is not None
        self.assertTrue(guidance.accepted)
        self.assertEqual(guidance.unknown_tokens, 1)

        branch_limited = LiveProgramGraph(
            sequential_program(3),
            cbc_enabled=True,
            cbc_max_branches=2,
        ).cbc_telemetry()
        self.assertEqual(branch_limited["functions_admitted"], 0)
        self.assertEqual(branch_limited["functions_skipped_branch_limit"], 1)

    def test_deep_definition_chain_is_iterative(self):
        instructions = [{"op": "input", "dst": "v0", "offset": 0}]
        for index in range(1, 1500):
            instructions.append({
                "op": "unary",
                "operator": "identity",
                "dst": f"v{index}",
                "value": {"var": f"v{index - 1}"},
                "bits": 8,
            })
        instructions.append({
            "op": "branch",
            "condition": {"var": "v1499"},
            "true": "yes",
            "false": "no",
            "site": 9001,
        })
        program = {"functions": {"main": {
            "entry": "entry",
            "blocks": {
                "entry": instructions,
                "yes": [{"op": "halt", "value": 1}],
                "no": [{"op": "halt", "value": 0}],
            },
        }}}
        telemetry = LiveProgramGraph(
            program, cbc_enabled=True,
        ).cbc_telemetry()
        self.assertEqual(telemetry["branches_analyzable"], 1)

    def test_independent_pattern_oracle_keeps_both_outcomes(self):
        branch_count = 6
        graph = LiveProgramGraph(
            sequential_program(branch_count), cbc_enabled=True,
        )
        accepted = []
        for pattern in itertools.product((False, True), repeat=branch_count):
            guidance = graph.cbc_guidance(tuple(
                search_decision_token(100 + index, choice)
                for index, choice in enumerate(pattern)
            ))
            assert guidance is not None
            if guidance.accepted:
                accepted.append(pattern)
        self.assertEqual(accepted, [
            (False,) * branch_count,
            (True,) * branch_count,
        ])
        for index in range(branch_count):
            self.assertEqual({pattern[index] for pattern in accepted}, {False, True})


class CBCExecutionTests(unittest.TestCase):
    @staticmethod
    def _run_execution(strategy, *, threshold=1):
        temporary = tempfile.TemporaryDirectory()
        environment = {
            "SYMCC_LIVE_SEARCH": strategy,
            "SYMCC_LIVE_CBC_STATE_THRESHOLD": str(threshold),
        }
        with mock.patch.dict(os.environ, environment):
            store = LiveStateStore(temporary.name)
            executor = LiveContinuationExecutor(store)
            root = executor.create(
                sequential_program(4), input_bytes=b"AAAA"
            )
            result = executor.resume(root, max_steps=1000, max_states=128)
            executor.close()
        return temporary, store, result

    def test_cbc_reduces_independent_cartesian_paths_and_preserves_branches(self):
        baseline_tmp, _baseline_store, baseline = self._run_execution("bfs")
        cbc_tmp, cbc_store, cbc = self._run_execution("cbc")
        self.addCleanup(baseline_tmp.cleanup)
        self.addCleanup(cbc_tmp.cleanup)
        self.assertEqual(len(baseline["halted"]), 16)
        self.assertEqual(len(cbc["halted"]), 2)
        self.assertEqual(baseline["generated_checkpoints"].__len__(), 30)
        self.assertEqual(cbc["generated_checkpoints"].__len__(), 8)
        self.assertEqual(cbc["state_search"]["cbc_execution"]["pruned_states"], 6)

        paths = [
            cbc_store.restore_continuation(row["checkpoint"])
            .descriptor.search_branch_path
            for row in cbc["halted"]
        ]
        for index in range(4):
            self.assertTrue(any(
                search_decision_token(100 + index, choice) in path
                for path in paths
                for choice in (False, True)
            ))
            observed = {
                choice
                for choice in (False, True)
                if any(
                    search_decision_token(100 + index, choice) in path
                    for path in paths
                )
            }
            self.assertEqual(observed, {False, True})

    def test_snapshot_restart_preserves_cbc_configuration_and_zero_draws(self):
        policy = LiveStateSearchPolicy(
            ("cbc", "path-cover"),
            seed=77,
            cbc_state_threshold=3,
            cbc_max_function_nodes=128,
            cbc_max_branches=17,
        )
        policy.select_index((
            LiveStateSearchFeatures("a", "main:a"),
            LiveStateSearchFeatures("b", "main:b"),
        ))
        snapshot = policy.snapshot()
        restored = LiveStateSearchPolicy.from_snapshot(snapshot)
        self.assertEqual(restored.snapshot(), snapshot)
        self.assertEqual(snapshot["schema"], "symcc-live-state-search-snapshot-v5")
        self.assertEqual(snapshot["random"]["draws"], 0)

    def test_persistent_pressure_and_restart_apply_cbc(self):
        with tempfile.TemporaryDirectory() as temporary:
            store_root = str(Path(temporary) / "store")
            frontier_root = str(Path(temporary) / "frontier")
            with mock.patch.dict(os.environ, {
                "SYMCC_LIVE_SEARCH": "cbc",
                "SYMCC_LIVE_CBC_STATE_THRESHOLD": "3",
            }):
                first = LiveContinuationExecutor(LiveStateStore(store_root))
                root = first.create(
                    sequential_program(3), input_bytes=b"AAA"
                )
                paused = first.resume_persistent(
                    root,
                    frontier_root,
                    owner="first",
                    max_claims=1,
                    max_steps_per_claim=32,
                    max_states_per_claim=1,
                )
                first.close()
            self.assertEqual(paused["frontier"]["ready"], 2)

            with mock.patch.dict(os.environ, {
                "SYMCC_LIVE_SEARCH": "bfs",
                "SYMCC_LIVE_CBC_STATE_THRESHOLD": "999",
            }):
                restarted = LiveContinuationExecutor(LiveStateStore(store_root))
                completed = restarted.resume_persistent(
                    root,
                    frontier_root,
                    owner="second",
                    max_claims=20,
                    max_steps_per_claim=32,
                    max_states_per_claim=1,
                )
                restarted.close()
            snapshot = PersistentLiveStateFrontier(frontier_root).snapshot()
            self.assertEqual(snapshot.search["strategies"], ["cbc"])
            self.assertEqual(snapshot.search["cbc_state_threshold"], 3)
            self.assertEqual(completed["frontier"]["ready"], 0)
            self.assertEqual(
                completed["state_search"]["cbc_execution"]["pruned_states"],
                4,
            )
            halted = [
                row
                for execution in completed["executions"]
                for row in execution["result"]["halted"]
            ]
            self.assertEqual(len(halted), 2)


if __name__ == "__main__":
    unittest.main()
