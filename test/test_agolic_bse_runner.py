# RUN: python3 %s

from pathlib import Path
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from agolic_bse_runner import (  # noqa: E402
    AgolicBSERunnerError,
    AgolicContinuationBSERunner,
)
from agolic_planning import (  # noqa: E402
    AgolicRunLevelPlanner,
)
from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


def witness_release_program():
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 2,
        "functions": {
            "main": {
                "entry": "entry",
                "blocks": {
                    "entry": [
                        {"op": "input", "dst": "route", "offset": 0},
                        {"op": "input", "dst": "payload", "offset": 1},
                        {
                            "op": "binary",
                            "operator": "eq",
                            "dst": "setup_ok",
                            "left": {"var": "route"},
                            "right": {"const": 65, "bits": 8},
                            "bits": 1,
                        },
                        {
                            "op": "branch",
                            "condition": {"var": "setup_ok"},
                            "true": "ready",
                            "false": "reject",
                            "site": 8001,
                        },
                    ],
                    "ready": [{
                        "op": "call",
                        "function": "release",
                        "args": [{"var": "payload"}],
                        "dst": "result",
                    }, {"op": "return", "value": {"var": "result"}}],
                    "reject": [{"op": "return", "value": 3}],
                },
            },
            "release": {
                "entry": "entry",
                "params": ["payload"],
                "blocks": {
                    "entry": [
                        {
                            "op": "binary",
                            "operator": "eq",
                            "dst": "is_target",
                            "left": {"var": "payload"},
                            "right": {"const": 66, "bits": 8},
                            "bits": 1,
                        },
                        {
                            "op": "branch",
                            "condition": {"var": "is_target"},
                            "true": "target",
                            "false": "other",
                            "site": 9001,
                        },
                    ],
                    "target": [{"op": "return", "value": 1}],
                    "other": [{"op": "return", "value": 2}],
                },
            },
        },
    }


def digest(content):
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def planned_witness_run(
    program_sha, witness_path, witness_bytes, *, target_function="release"
):
    planner = AgolicRunLevelPlanner(
        None,
        experiment_id="agolic-bse-test",
        program_id="continuation-fixture",
        program_sha256=program_sha,
        profiles={
            "bounded": {
                "modes": ["witness-guided"],
                "time_limit_seconds": 5.0,
                "memory_limit_mib": 512,
            }
        },
    )
    target = {
        "target_id": "release-target",
        "target_branch": 9001,
        "source_file": "fixture.c",
        "function": target_function,
        "line": 12,
        "distance": 1.0,
        "opportunity": "uncovered target branch",
        "modes": ["witness-guided"],
        "witnesses": [{
            "sha256": digest(witness_bytes),
            "path": str(witness_path),
            "release_function": "release",
            "release_branch": 0,
            "provenance": "unit-test",
        }],
    }
    plans, _diagnostics = planner.plan_round([target], max_runs=1)
    assert len(plans) == 1
    return planner, plans[0]


def planned_harness_run(program_sha):
    planner = AgolicRunLevelPlanner(
        None,
        experiment_id="agolic-harness-test",
        program_id="continuation-fixture",
        program_sha256=program_sha,
        profiles={
            "bounded": {
                "modes": ["harness-entry"],
                "time_limit_seconds": 5.0,
                "memory_limit_mib": 512,
                "symbolic_inputs": {"seed_hex": "4100"},
            }
        },
    )
    target = {
        "target_id": "release-target",
        "target_branch": 9001,
        "source_file": "fixture.c",
        "function": "release",
        "line": 12,
        "distance": 1.0,
        "opportunity": "uncovered target branch",
        "modes": ["harness-entry"],
        "witnesses": [],
    }
    plans, _diagnostics = planner.plan_round([target], max_runs=1)
    assert len(plans) == 1
    return planner, plans[0]


class AgolicBSERunnerTests(unittest.TestCase):
    def test_harness_entry_explores_from_the_program_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program_path = root / "program.json"
            encoded = json.dumps(
                witness_release_program(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            program_path.write_bytes(encoded)
            _planner, plan = planned_harness_run(digest(encoded))
            runner = AgolicContinuationBSERunner(
                program_path,
                root / "campaign",
                program_sha256=digest(encoded),
                max_steps=128,
                max_states=16,
                max_artifacts=8,
            )

            result = runner.execute_plan(plan)
            self.assertFalse(result["execution"]["witness_guidance"]["enabled"])
            self.assertFalse(result["release_reached"])
            self.assertGreaterEqual(result["execution"]["forks"], 2)
            outcome = runner.replay_result(plan, result)
            self.assertEqual(
                outcome["target_coverage_elements"],
                ["branch:9001:F", "branch:9001:T"],
            )

    def test_state_limit_with_outstanding_frontier_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            with LiveContinuationExecutor(store) as executor:
                root = executor.create(
                    witness_release_program(), input_bytes=b"A\x00"
                )
                result = executor.resume(
                    root,
                    max_steps=128,
                    max_states=1,
                    witness_release_function="release",
                )

                self.assertTrue(result["frontier"])
                self.assertTrue(result["bounded"])
                self.assertFalse(result["timed_out"])

    def test_witness_prefix_releases_to_solver_backed_fork(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            with LiveContinuationExecutor(store) as executor:
                root = executor.create(
                    witness_release_program(), input_bytes=b"A\x00",
                    target_branch=9001,
                )
                result = executor.resume(
                    root,
                    max_steps=128,
                    max_states=16,
                    witness_release_function="release",
                )

                self.assertEqual(result["forks"], 1)
                self.assertEqual(result["witness_guidance"], {
                    "enabled": True,
                    "concrete_replay": False,
                    "release_function": "release",
                    "release_branch": 0,
                    "release_reached": True,
                    "pre_release_decisions": 1,
                    "pre_release_solver_queries": 0,
                    "pre_release_forks": 0,
                })
                self.assertEqual(
                    result["executed_branch_outcomes"],
                    [
                        {"site": 8001, "taken": True},
                        {"site": 9001, "taken": False},
                        {"site": 9001, "taken": True},
                    ],
                )
                target = next(
                    item for item in result["halted"]
                    if item["status"] == "returned" and item["value"] == 1
                )
                generated = executor.materialize_input(
                    target["checkpoint"], fallback_input=b"A\x00"
                )
                self.assertEqual(generated, b"AB")

    def test_materialized_input_has_zero_fork_concrete_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            with LiveContinuationExecutor(store) as executor:
                root = executor.create(
                    witness_release_program(), input_bytes=b"A\x00",
                    target_branch=9001,
                )
                result = executor.resume(
                    root,
                    max_steps=128,
                    max_states=16,
                    witness_release_function="release",
                )
                target = next(
                    item for item in result["halted"] if item.get("value") == 1
                )
                generated = executor.materialize_input(
                    target["checkpoint"], fallback_input=b"A\x00"
                )
                replay_root = executor.create(
                    witness_release_program(), input_bytes=generated,
                    target_branch=9001,
                )
                replay = executor.resume(
                    replay_root,
                    max_steps=128,
                    max_states=1,
                    concrete_replay=True,
                )

                self.assertEqual(replay["forks"], 0)
                self.assertFalse(replay["bounded"])
                self.assertEqual(
                    replay["witness_guidance"]["pre_release_solver_queries"],
                    0,
                )
                self.assertIn(
                    {"site": 9001, "taken": True},
                    replay["executed_branch_outcomes"],
                )
                self.assertEqual(
                    {item["value"] for item in replay["halted"]}, {1}
                )

    def test_unreached_release_stays_single_state_and_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            with LiveContinuationExecutor(store) as executor:
                root = executor.create(
                    witness_release_program(), input_bytes=b"Z\x00"
                )
                result = executor.resume(
                    root,
                    max_steps=128,
                    max_states=16,
                    witness_release_function="release",
                )

                self.assertEqual(result["forks"], 0)
                self.assertFalse(
                    result["witness_guidance"]["release_reached"]
                )
                self.assertEqual(
                    result["witness_guidance"]["pre_release_decisions"], 1
                )
                self.assertEqual(
                    {item["value"] for item in result["halted"]}, {3}
                )

    def test_guidance_configuration_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            with LiveContinuationExecutor(store) as executor:
                root = executor.create(
                    witness_release_program(), input_bytes=b"A\x00"
                )
                with self.assertRaisesRegex(ValueError, "release function"):
                    executor.resume(
                        root, witness_release_function="missing"
                    )
                with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                    executor.resume(
                        root,
                        concrete_replay=True,
                        witness_release_function="release",
                    )

    def test_target_branch_must_belong_to_the_target_function(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            encoded = json.dumps(
                witness_release_program(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            program_path = root / "program.json"
            program_path.write_bytes(encoded)
            witness_path = root / "witness"
            witness_path.write_bytes(b"A\x00")
            _planner, plan = planned_witness_run(
                digest(encoded),
                witness_path,
                b"A\x00",
                target_function="main",
            )
            runner = AgolicContinuationBSERunner(
                program_path,
                root / "campaign",
                program_sha256=digest(encoded),
            )

            self.assertEqual(runner.preflight(plan)[0], False)
            with self.assertRaisesRegex(
                AgolicBSERunnerError, "decision in the target function"
            ):
                runner.execute_plan(plan)

    def test_witness_is_revalidated_at_execution_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            encoded = json.dumps(
                witness_release_program(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            program_path = root / "program.json"
            program_path.write_bytes(encoded)
            witness_path = root / "witness"
            witness_path.write_bytes(b"A\x00")
            _planner, plan = planned_witness_run(
                digest(encoded), witness_path, b"A\x00"
            )
            runner = AgolicContinuationBSERunner(
                program_path,
                root / "campaign",
                program_sha256=digest(encoded),
            )
            normalized = runner._normalize_plan(plan)
            witness_path.write_bytes(b"Z\x00")

            with self.assertRaisesRegex(
                AgolicBSERunnerError, "witness identity mismatch"
            ):
                runner._seed(normalized)

    def test_guided_checkpoint_recovery_is_bound_to_release_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            with LiveContinuationExecutor(store) as executor:
                root = executor.create(
                    witness_release_program(), input_bytes=b"A\x00"
                )
                partial = executor.resume(
                    root,
                    max_steps=5,
                    max_states=1,
                    witness_release_function="release",
                )
                self.assertTrue(partial["bounded"])
                self.assertEqual(len(partial["frontier"]), 1)
                with self.assertRaisesRegex(ValueError, "configuration mismatch"):
                    executor.resume(
                        partial["frontier"][0],
                        witness_release_branch=9001,
                    )
                resumed = executor.resume(
                    partial["frontier"][0],
                    max_steps=64,
                    max_states=8,
                    witness_release_function="release",
                )
                self.assertTrue(
                    resumed["witness_guidance"]["release_reached"]
                )
                self.assertEqual(resumed["forks"], 1)

    def test_plan_to_replay_to_planner_outcome_is_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program_path = root / "program.json"
            encoded = json.dumps(
                witness_release_program(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            program_path.write_bytes(encoded)
            witness_path = root / "witness"
            witness_path.write_bytes(b"A\x00")
            planner, plan = planned_witness_run(
                digest(encoded), witness_path, b"A\x00"
            )
            runner = AgolicContinuationBSERunner(
                program_path,
                root / "campaign",
                program_sha256=digest(encoded),
                max_steps=128,
                max_states=16,
                max_artifacts=8,
            )

            self.assertEqual(runner.preflight(plan), (True, "ok"))
            result = runner.execute_plan(plan)
            self.assertTrue(result["release_reached"])
            self.assertEqual(result["execution"]["forks"], 1)
            self.assertEqual(len(result["candidates"]), 2)
            self.assertIn(
                digest(b"AB"),
                {item["sha256"] for item in result["candidates"]},
            )
            outcome = runner.replay_result(plan, result)
            self.assertTrue(outcome["replay_verified"])
            self.assertEqual(outcome["covered_branches"], [8001, 9001])
            self.assertEqual(
                outcome["target_coverage_elements"],
                ["branch:9001:F", "branch:9001:T"],
            )
            self.assertEqual(
                outcome["covered_functions"], ["main", "release"]
            )
            recorded = planner.record_outcome(plan["plan_id"], outcome)
            self.assertEqual(recorded["target_class"], "new-reach")
            self.assertEqual(len(recorded["artifacts"]), 2)

    def test_runner_emits_no_candidate_before_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program_path = root / "program.json"
            encoded = json.dumps(
                witness_release_program(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            program_path.write_bytes(encoded)
            witness_path = root / "witness"
            witness_path.write_bytes(b"Z\x00")
            planner, plan = planned_witness_run(
                digest(encoded), witness_path, b"Z\x00"
            )
            runner = AgolicContinuationBSERunner(
                program_path,
                root / "campaign",
                program_sha256=digest(encoded),
                max_steps=128,
                max_states=16,
            )

            result = runner.execute_plan(plan)
            self.assertFalse(result["release_reached"])
            self.assertEqual(result["candidates"], [])
            outcome = runner.replay_result(plan, result)
            self.assertEqual(outcome["reason"], "release-not-reached")
            recorded = planner.record_outcome(plan["plan_id"], outcome)
            self.assertEqual(recorded["target_class"], "not-reached")

    def test_replay_rejects_candidate_metadata_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program_path = root / "program.json"
            encoded = json.dumps(
                witness_release_program(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            program_path.write_bytes(encoded)
            witness_path = root / "witness"
            witness_path.write_bytes(b"A\x00")
            _planner, plan = planned_witness_run(
                digest(encoded), witness_path, b"A\x00"
            )
            runner = AgolicContinuationBSERunner(
                program_path,
                root / "campaign",
                program_sha256=digest(encoded),
                max_steps=128,
                max_states=16,
            )
            result = runner.execute_plan(plan)
            result["candidates"][0]["relative_path"] = "../escape"
            with self.assertRaisesRegex(
                AgolicBSERunnerError, "candidate path"
            ):
                runner.replay_result(plan, result)

    def test_replay_rejects_terminal_and_status_mutations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program_path = root / "program.json"
            encoded = json.dumps(
                witness_release_program(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            program_path.write_bytes(encoded)
            witness_path = root / "witness"
            witness_path.write_bytes(b"A\x00")
            _planner, plan = planned_witness_run(
                digest(encoded), witness_path, b"A\x00"
            )
            runner = AgolicContinuationBSERunner(
                program_path,
                root / "campaign",
                program_sha256=digest(encoded),
                max_steps=128,
                max_states=16,
            )
            result = runner.execute_plan(plan)

            checkpoint_mutation = copy.deepcopy(result)
            checkpoint_mutation["candidates"][0]["checkpoint"] = "0" * 64
            with self.assertRaisesRegex(
                AgolicBSERunnerError, "terminal checkpoint"
            ):
                runner.replay_result(plan, checkpoint_mutation)

            status_mutation = copy.deepcopy(result)
            status_mutation["status"] = "error"
            with self.assertRaisesRegex(
                AgolicBSERunnerError, "status is inconsistent"
            ):
                runner.replay_result(plan, status_mutation)

    def test_replay_binds_the_controller_prior_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program_path = root / "program.json"
            encoded = json.dumps(
                witness_release_program(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            program_path.write_bytes(encoded)
            witness_path = root / "witness"
            witness_path.write_bytes(b"A\x00")
            _planner, plan = planned_witness_run(
                digest(encoded), witness_path, b"A\x00"
            )
            runner = AgolicContinuationBSERunner(
                program_path,
                root / "campaign",
                program_sha256=digest(encoded),
                max_steps=128,
                max_states=16,
            )
            prior = runner.replay_coverage()
            result = runner.execute_plan(plan)
            stale = dict(prior)
            stale["replay_identity"] = "0" * 64

            with self.assertRaisesRegex(
                AgolicBSERunnerError, "does not match the current corpus"
            ):
                runner.replay_result(plan, result, stale)
            outcome = runner.replay_result(plan, result, prior)
            self.assertTrue(outcome["replay_verified"])

    def test_cli_enforces_memory_limit_and_replays_serially(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program_path = root / "program.json"
            encoded = json.dumps(
                witness_release_program(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            program_path.write_bytes(encoded)
            witness_path = root / "witness"
            witness_path.write_bytes(b"A\x00")
            _planner, plan = planned_witness_run(
                digest(encoded), witness_path, b"A\x00"
            )
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(plan, sort_keys=True, separators=(",", ":")),
                encoding="ascii",
            )
            workspace = root / "campaign"
            common = [
                sys.executable,
                str(ROOT / "util" / "agolic_bse_runner.py"),
                "--program", str(program_path),
                "--program-sha256", digest(encoded),
                "--workspace", str(workspace),
                "--max-steps", "128",
                "--max-states", "16",
                "--max-artifacts", "8",
            ]
            executed = subprocess.run(
                [*common, "execute", str(plan_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            result = json.loads(executed.stdout)
            self.assertTrue(
                result["resource_limits"]["memory_limit_enforced"]
            )
            result_path = root / "result.json"
            result_path.write_text(
                json.dumps(result, sort_keys=True, separators=(",", ":")),
                encoding="ascii",
            )
            replayed = subprocess.run(
                [*common, "replay", str(plan_path), str(result_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            outcome = json.loads(replayed.stdout)
            self.assertTrue(outcome["replay_verified"])
            self.assertEqual(outcome["covered_branches"], [8001, 9001])


if __name__ == "__main__":
    unittest.main()
