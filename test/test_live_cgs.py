# RUN: python3 %s

import copy
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
import live_state_search  # noqa: E402
from live_state_search import (  # noqa: E402
    LiveProgramGraph,
    LiveStateSearchFeatures,
    LiveStateSearchPolicy,
)


COMPARISONS = (
    "eq", "ne", "ult", "ule", "ugt", "uge",
    "slt", "sle", "sgt", "sge",
)


def _constant(value, bits):
    return {"const": value, "bits": bits}


def comparison_program(
    operator="ugt",
    *,
    bits=8,
    constant=2,
    swapped=False,
    transform=None,
    transform_constant=0,
    identities=0,
    guarded_store=False,
    symbolic_address=False,
    store_bits=None,
    constant_bits=None,
    mask_bits=None,
):
    store_width = bits if store_bits is None else store_bits
    address = {"var": "address"} if symbolic_address else _constant(0, 64)
    store = {
        "op": "store",
        "address": address,
        "value": _constant(1, store_width),
        "bits": store_width,
    }
    if guarded_store:
        store["guard"] = _constant(1, 1)
    instructions = [store, {
        "op": "load",
        "dst": "loaded",
        "address": _constant(0, 64),
        "bits": bits,
    }]
    value = {"var": "loaded"}
    for index in range(identities):
        name = f"identity{index}"
        instructions.append({
            "op": "unary",
            "operator": "identity",
            "dst": name,
            "value": value,
            "bits": bits,
        })
        value = {"var": name}
    if transform is not None:
        instructions.append({
            "op": "binary",
            "operator": transform,
            "dst": "transformed",
            "left": value,
            "right": _constant(
                transform_constant,
                bits if mask_bits is None else mask_bits,
            ),
            "bits": bits,
        })
        value = {"var": "transformed"}
    constant_value = _constant(
        constant,
        bits if constant_bits is None else constant_bits,
    )
    instructions.extend(({
        "op": "binary",
        "operator": operator,
        "dst": "condition",
        "left": constant_value if swapped else value,
        "right": value if swapped else constant_value,
        "bits": 1,
    }, {
        "op": "branch",
        "condition": {"var": "condition"},
        "true": "yes",
        "false": "no",
        "site": 300,
    }))
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "memory_size": 8,
        "functions": {"main": {
            "entry": "entry",
            "blocks": {
                "entry": instructions,
                "yes": [{"op": "halt", "value": 1}],
                "no": [{"op": "halt", "value": 0}],
            },
        }},
    }


def scheduling_program():
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 2,
        "memory_size": 1,
        "functions": {"main": {
            "entry": "entry",
            "blocks": {
                "entry": [
                    {"op": "input", "dst": "x", "offset": 0},
                    {
                        "op": "binary", "operator": "eq", "dst": "root",
                        "left": {"var": "x"}, "right": _constant(0, 8),
                        "bits": 1,
                    },
                    {
                        "op": "branch", "condition": {"var": "root"},
                        "true": "seed_store", "false": "split",
                        "site": 100,
                    },
                ],
                "seed_store": [{
                    "op": "store", "address": _constant(0, 64),
                    "value": _constant(1, 8), "bits": 8,
                }, {"op": "jump", "target": "check"}],
                "split": [
                    {"op": "input", "dst": "y", "offset": 1},
                    {
                        "op": "binary", "operator": "eq", "dst": "fork",
                        "left": {"var": "y"}, "right": _constant(0, 8),
                        "bits": 1,
                    },
                    {
                        "op": "branch", "condition": {"var": "fork"},
                        "true": "distractor", "false": "target_store",
                        "site": 200,
                    },
                ],
                "distractor": [{"op": "halt", "value": 20}],
                "target_store": [{
                    "op": "store", "address": _constant(0, 64),
                    "value": _constant(4, 8), "bits": 8,
                }, {"op": "jump", "target": "check"}],
                "check": [
                    {
                        "op": "load", "dst": "loaded",
                        "address": _constant(0, 64), "bits": 8,
                    },
                    {
                        "op": "binary", "operator": "ugt", "dst": "target",
                        "left": {"var": "loaded"},
                        "right": _constant(2, 8), "bits": 1,
                    },
                    {
                        "op": "branch", "condition": {"var": "target"},
                        "true": "target_hit", "false": "seed_done",
                        "site": 300,
                    },
                ],
                "target_hit": [{"op": "halt", "value": 30}],
                "seed_done": [{"op": "halt", "value": 10}],
            },
        }},
    }


def _signed(value, bits):
    value &= (1 << bits) - 1
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


def _reference(operator, value, constant, bits):
    mask = (1 << bits) - 1
    left = value & mask
    right = constant & mask
    if operator.startswith("s"):
        left = _signed(left, bits)
        right = _signed(right, bits)
    relation = operator[-2:]
    return {
        "eq": left == right,
        "ne": left != right,
        "lt": left < right,
        "le": left <= right,
        "gt": left > right,
        "ge": left >= right,
    }[relation]


class CGSStaticAnalysisTests(unittest.TestCase):
    def test_exact_store_load_predicate_and_pending_guidance(self):
        graph = LiveProgramGraph(comparison_program(), cgs_enabled=True)
        token = graph.cgs_store_identity("main", "entry", 0)
        self.assertIsNotNone(token)
        assert token is not None
        self.assertEqual(graph.cgs_telemetry()["branches_admitted"], 1)
        unknown = graph.cgs_guidance("main:entry", {}, ((300, True),))
        invalid = graph.cgs_guidance(
            "main:no", {token: 1}, ((300, True),),
        )
        valid = graph.cgs_guidance(
            "main:no", {token: 4}, ((300, True),),
        )
        assert unknown is not None and invalid is not None and valid is not None
        self.assertEqual((unknown.priority, invalid.priority, valid.priority),
                         (0, 0, 2))

    def test_swapped_signed_and_masked_predicates(self):
        swapped = LiveProgramGraph(
            comparison_program("slt", constant=-2, swapped=True),
            cgs_enabled=True,
        )
        token = swapped.cgs_store_identity("main", "entry", 0)
        assert token is not None
        # -2 < -1 is true after the comparison is normalized to -1 > -2.
        guidance = swapped.cgs_guidance(
            "main:no", {token: 0xFF}, ((300, True),),
        )
        assert guidance is not None
        self.assertEqual(guidance.priority, 2)

        masked = LiveProgramGraph(
            comparison_program(
                "eq", constant=5, transform="and", transform_constant=0x0F,
            ),
            cgs_enabled=True,
        )
        token = masked.cgs_store_identity("main", "entry", 0)
        assert token is not None
        guidance = masked.cgs_guidance(
            "main:no", {token: 0xA5}, ((300, True),),
        )
        assert guidance is not None
        self.assertEqual(guidance.priority, 2)

    def test_unsupported_alias_width_and_ambiguity_fail_open(self):
        cases = (
            comparison_program(guarded_store=True),
            comparison_program(symbolic_address=True),
            comparison_program(store_bits=16),
            comparison_program(constant_bits=16),
            comparison_program(
                transform="or", transform_constant=1, mask_bits=16,
            ),
        )
        for program in cases:
            with self.subTest(program=program):
                graph = LiveProgramGraph(program, cgs_enabled=True)
                self.assertFalse(graph.cgs_has_target(300))
                guidance = graph.cgs_guidance(
                    "main:entry", {}, ((300, True),),
                )
                assert guidance is not None
                self.assertEqual(guidance.priority, 0)

        overlapping = comparison_program()
        overlapping["functions"]["main"]["blocks"]["entry"].insert(1, {
            "op": "store", "address": _constant(0, 64),
            "value": _constant(0xFFFF, 16), "bits": 16,
        })
        self.assertFalse(LiveProgramGraph(
            overlapping, cgs_enabled=True,
        ).cgs_has_target(300))

        calling = comparison_program()
        calling["functions"]["main"]["blocks"]["entry"].insert(1, {
            "op": "call", "function": "callee", "args": [],
        })
        calling["functions"]["callee"] = {
            "entry": "entry",
            "blocks": {"entry": [{"op": "return", "value": 0}]},
        }
        call_graph = LiveProgramGraph(calling, cgs_enabled=True)
        self.assertFalse(call_graph.cgs_has_target(300))
        self.assertEqual(
            call_graph.cgs_telemetry()["functions_memory_ambiguous"], 1,
        )

        repeated = comparison_program()
        blocks = repeated["functions"]["main"]["blocks"]
        blocks["yes"] = copy.deepcopy(blocks["entry"])
        blocks["yes"][1]["dst"] = "loaded2"
        blocks["yes"][2]["dst"] = "condition2"
        blocks["yes"][2]["left"] = {"var": "loaded2"}
        blocks["yes"][-1]["condition"] = {"var": "condition2"}
        blocks["yes"][-1].update(true="done", false="done")
        blocks["done"] = [{"op": "halt", "value": 0}]
        graph = LiveProgramGraph(repeated, cgs_enabled=True)
        self.assertFalse(graph.cgs_has_target(300))
        self.assertEqual(graph.cgs_telemetry()["ambiguous_branch_sites"], 1)

    def test_store_token_collision_and_analysis_budgets_fail_open(self):
        program = comparison_program()
        program["functions"]["main"]["blocks"]["entry"].insert(1, {
            "op": "store", "address": _constant(0, 64),
            "value": _constant(2, 8), "bits": 8,
        })
        with mock.patch.object(live_state_search, "cgs_store_token", return_value=7):
            graph = LiveProgramGraph(program, cgs_enabled=True)
        self.assertFalse(graph.cgs_has_target(300))
        self.assertEqual(graph.cgs_telemetry()["ambiguous_store_tokens"], 1)

        oversized = comparison_program()
        blocks = oversized["functions"]["main"]["blocks"]
        for index in range(20):
            blocks[f"unused{index}"] = [{"op": "halt", "value": index}]
        telemetry = LiveProgramGraph(
            oversized, cgs_enabled=True, cgs_max_function_nodes=16,
        ).cgs_telemetry()
        self.assertEqual(telemetry["functions_skipped_oversized"], 1)
        self.assertEqual(telemetry["branches_admitted"], 0)

        branch_limited = comparison_program()
        blocks = branch_limited["functions"]["main"]["blocks"]
        blocks["yes"] = copy.deepcopy(blocks["entry"])
        blocks["yes"][-1].update(site=301, true="done", false="done")
        blocks["done"] = [{"op": "halt", "value": 0}]
        telemetry = LiveProgramGraph(
            branch_limited, cgs_enabled=True, cgs_max_branches=1,
        ).cgs_telemetry()
        self.assertEqual(telemetry["functions_skipped_branch_limit"], 1)
        self.assertEqual(telemetry["branches_admitted"], 0)

    def test_deep_identity_chain_is_iterative(self):
        graph = LiveProgramGraph(
            comparison_program(identities=1500), cgs_enabled=True,
        )
        self.assertTrue(graph.cgs_has_target(300))

    def test_independent_bitvector_oracle(self):
        for bits in range(1, 9):
            for operator in COMPARISONS:
                constant = ((1 << bits) - 1) // 3
                graph = LiveProgramGraph(
                    comparison_program(
                        operator, bits=bits, constant=constant,
                    ),
                    cgs_enabled=True,
                )
                token = graph.cgs_store_identity("main", "entry", 0)
                assert token is not None
                for value in range(1 << bits):
                    expected = _reference(operator, value, constant, bits)
                    for desired in (False, True):
                        guidance = graph.cgs_guidance(
                            "main:no", {token: value}, ((300, desired),),
                        )
                        assert guidance is not None
                        self.assertEqual(
                            guidance.priority == 2,
                            expected == desired,
                            (operator, bits, value, desired),
                        )


class CGSPolicyTests(unittest.TestCase):
    def test_targets_complete_and_rotate_deterministically(self):
        policy = LiveStateSearchPolicy(
            ("cgs",), cgs_target_limit=2, cgs_rotation_instructions=10,
        )
        for site in (10, 20, 30):
            policy.observe_cgs_branch(site, False)
        self.assertEqual(policy.active_cgs_targets(), ((10, True), (20, True)))
        policy.observe_cgs_instructions(10)
        self.assertEqual(policy.active_cgs_targets(), ((30, True), (10, True)))
        policy.observe_cgs_branch(30, True)
        self.assertEqual(policy.active_cgs_targets(), ((10, True), (20, True)))

    def test_priority_snapshot_and_zero_random_draws(self):
        policy = LiveStateSearchPolicy(
            ("cgs",), seed=99, cgs_target_limit=3,
            cgs_rotation_instructions=17, cgs_max_function_nodes=128,
            cgs_max_branches=19,
        )
        policy.observe_cgs_instructions(11)
        policy.observe_cgs_branch(300, False)
        self.assertEqual(policy.select_index((
            LiveStateSearchFeatures("ordinary", "main:a", cgs_priority=0),
            LiveStateSearchFeatures("pending", "main:b", cgs_priority=1),
            LiveStateSearchFeatures("valid", "main:c", cgs_priority=2),
        )), 2)
        snapshot = policy.snapshot()
        restored = LiveStateSearchPolicy.from_snapshot(snapshot)
        self.assertEqual(restored.snapshot(), snapshot)
        self.assertEqual(snapshot["schema"], "symcc-live-state-search-snapshot-v5")
        self.assertEqual(snapshot["random"]["draws"], 0)

    def test_snapshot_and_transition_validation_reject_corruption(self):
        policy = LiveStateSearchPolicy(("cgs",))
        previous = policy.snapshot()
        policy.observe_cgs_instructions(5)
        policy.observe_cgs_branch(300, False)
        updated = policy.snapshot()
        LiveStateSearchPolicy.validate_observation_transition(previous, updated)

        missing_observation = copy.deepcopy(updated)
        missing_observation["cgs_observations"] = 0
        with self.assertRaisesRegex(ValueError, "CGS observations"):
            LiveStateSearchPolicy.from_snapshot(missing_observation)

        impossible_delta = copy.deepcopy(updated)
        impossible_delta["cgs_branch_outcomes"] = [[300, 3]]
        impossible_delta["cgs_partial_order"] = []
        with self.assertRaisesRegex(ValueError, "CGS observations"):
            LiveStateSearchPolicy.from_snapshot(impossible_delta)

        disabled = LiveStateSearchPolicy(("bfs",)).snapshot()
        disabled["cgs_instruction_count"] = 1
        with self.assertRaisesRegex(ValueError, "disabled live CGS"):
            LiveStateSearchPolicy.from_snapshot(disabled)
        disabled_policy = LiveStateSearchPolicy(("bfs",))
        with self.assertRaisesRegex(ValueError, "require the CGS strategy"):
            disabled_policy.observe_cgs_instructions(1)
        with self.assertRaisesRegex(ValueError, "require the CGS strategy"):
            disabled_policy.observe_cgs_branch(300, False)


class CGSExecutionTests(unittest.TestCase):
    @staticmethod
    def _run(strategy):
        temporary = tempfile.TemporaryDirectory()
        with mock.patch.dict(os.environ, {"SYMCC_LIVE_SEARCH": strategy}):
            store = LiveStateStore(temporary.name)
            executor = LiveContinuationExecutor(store)
            root = executor.create(scheduling_program(), input_bytes=b"\x00\x00")
            result = executor.resume(root, max_steps=100, max_states=16)
            executor.close()
        return temporary, result

    def test_cgs_prioritizes_target_store_without_pruning(self):
        bfs_temporary, bfs = self._run("bfs")
        cgs_temporary, cgs = self._run("cgs")
        self.addCleanup(bfs_temporary.cleanup)
        self.addCleanup(cgs_temporary.cleanup)
        self.assertEqual(
            [row["value"] for row in bfs["halted"]], [10, 20, 30],
        )
        self.assertEqual(
            [row["value"] for row in cgs["halted"]], [10, 30, 20],
        )
        self.assertEqual(
            sorted(row["value"] for row in bfs["halted"]),
            sorted(row["value"] for row in cgs["halted"]),
        )
        telemetry = cgs["state_search"]
        self.assertEqual(telemetry["cgs_graph"]["branches_admitted"], 1)
        self.assertEqual(telemetry["cgs_execution"]["branch_observations"], 2)
        self.assertGreaterEqual(
            telemetry["cgs_execution"]["store_value_updates"], 2,
        )
        self.assertEqual(telemetry["cgs_active_targets"], 0)
        self.assertEqual(telemetry["random_draws"], 0)

    def test_persistent_restart_replays_cgs_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            store_root = str(Path(temporary) / "store")
            frontier_root = str(Path(temporary) / "frontier")
            with mock.patch.dict(os.environ, {
                "SYMCC_LIVE_SEARCH": "cgs",
                "SYMCC_LIVE_CGS_TARGETS": "3",
                "SYMCC_LIVE_CGS_ROTATION_INSTRUCTIONS": "17",
            }):
                first = LiveContinuationExecutor(LiveStateStore(store_root))
                root = first.create(
                    scheduling_program(), input_bytes=b"\x00\x00",
                )
                paused = first.resume_persistent(
                    root, frontier_root, owner="first", max_claims=2,
                    max_steps_per_claim=32, max_states_per_claim=1,
                )
                first.close()
            snapshot = PersistentLiveStateFrontier(frontier_root).snapshot()
            self.assertEqual(snapshot.search["strategies"], ["cgs"])
            self.assertEqual(snapshot.search["cgs_target_limit"], 3)
            self.assertEqual(snapshot.search["cgs_rotation_instructions"], 17)
            self.assertGreater(snapshot.search["cgs_instruction_count"], 0)
            self.assertGreaterEqual(snapshot.search["cgs_observations"], 1)
            self.assertGreaterEqual(
                paused["state_search"]["cgs_execution"]["branch_observations"],
                1,
            )

            with mock.patch.dict(os.environ, {
                "SYMCC_LIVE_SEARCH": "bfs",
                "SYMCC_LIVE_CGS_TARGETS": "99",
            }):
                restarted = LiveContinuationExecutor(
                    LiveStateStore(store_root),
                )
                completed = restarted.resume_persistent(
                    root, frontier_root, owner="second", max_claims=16,
                    max_steps_per_claim=32, max_states_per_claim=1,
                )
                restarted.close()
            final = PersistentLiveStateFrontier(frontier_root).snapshot()
            self.assertEqual(final.search["strategies"], ["cgs"])
            self.assertEqual(final.search["cgs_target_limit"], 3)
            self.assertEqual(completed["frontier"]["ready"], 0)
            halted = [
                row["value"]
                for execution in (*paused["executions"], *completed["executions"])
                for row in execution["result"]["halted"]
            ]
            self.assertEqual(sorted(halted), [10, 20, 30])


if __name__ == "__main__":
    unittest.main()
