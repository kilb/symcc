# RUN: python3 %s

import hashlib
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

import agolic_planning  # noqa: E402
from agolic_planning import (  # noqa: E402
    AgolicAdmissionError,
    AgolicRoundController,
    AgolicRunLevelPlanner,
    AgolicStateError,
    CoverageSnapshot,
)


PROGRAM_SHA = "1" * 64


def digest(label):
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def target(
    target_id="t1",
    branch=101,
    function="parse",
    *,
    modes=None,
    witnesses=None,
):
    return {
        "target_id": target_id,
        "target_branch": branch,
        "source_file": "parser.c",
        "function": function,
        "line": branch,
        "distance": 2.0,
        "opportunity": "uncovered successor",
        "modes": modes or ["harness-entry"],
        "witnesses": witnesses or [],
    }


def witness(label="seed"):
    return {
        "sha256": digest(label),
        "path": f"corpus/{label}",
        "release_function": "decode",
        "release_branch": 0,
        "provenance": "campaign corpus",
    }


def profiles():
    return {
        "dfs": {
            "modes": ["harness-entry", "witness-guided"],
            "environment": {"SYMCC_SEARCH": "dfs"},
            "symbolic_inputs": {"stdin": "bytes:0:64"},
            "time_limit_seconds": 10.0,
            "memory_limit_mib": 512,
        },
        "covnew": {
            "modes": ["harness-entry", "witness-guided"],
            "environment": {"SYMCC_SEARCH": "covnew"},
            "symbolic_inputs": {"stdin": "bytes:0:64"},
            "time_limit_seconds": 10.0,
            "memory_limit_mib": 512,
        },
    }


def planner(path=None, *, max_history=128):
    return AgolicRunLevelPlanner(
        path,
        experiment_id="exp-1",
        program_id="parser",
        program_sha256=PROGRAM_SHA,
        profiles=profiles(),
        max_history=max_history,
    )


def coverage(label, *, elements=(), branches=(), functions=(), artifacts=()):
    return CoverageSnapshot.from_mapping({
        "elements": list(elements),
        "branches": list(branches),
        "functions": list(functions),
        "corpus_artifacts": list(artifacts),
        "replay_identity": digest(label),
    })


def verified_outcome(
    label,
    *,
    elements,
    branches,
    functions,
    artifacts=(),
    generated_artifacts=(),
    target_elements=(),
):
    return {
        "status": "complete",
        "replay_verified": True,
        "replay_identity": digest(label),
        "coverage_elements": list(elements),
        "covered_branches": list(branches),
        "covered_functions": list(functions),
        "corpus_artifacts": list(artifacts),
        "generated_artifacts": list(generated_artifacts),
        "target_coverage_elements": list(target_elements),
        "elapsed_seconds": 1.5,
        "cpu_seconds": 1.0,
        "solver_time_seconds": 0.25,
    }


class AgolicPlanningTests(unittest.TestCase):
    def test_pending_plan_survives_restart_and_exact_spec_is_not_reissued(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "planner.json"
            first = planner(state)
            first.update_replay_coverage(coverage("initial"))
            proposal = {"target_id": "t1", "mode": "harness-entry", "profile": "dfs"}
            plans, diagnostics = first.plan_round(
                [target()], max_runs=1, proposals=[proposal])
            self.assertTrue(diagnostics[0].accepted)

            recovered = planner(state)
            self.assertEqual(recovered.pending_plans(), plans)
            artifact = digest("artifact-1")
            result = recovered.record_outcome(plans[0]["plan_id"], verified_outcome(
                "run-1",
                elements=["parse:101:T"],
                branches=[99],
                functions=["parse"],
                artifacts=[artifact],
                generated_artifacts=[artifact],
                target_elements=["parse:101:T"],
            ))
            self.assertEqual(result["target_class"], "new-reach")

            no_plans, duplicate = recovered.plan_round(
                [target()], max_runs=1, proposals=[proposal])
            self.assertEqual(no_plans, [])
            self.assertFalse(duplicate[0].accepted)
            self.assertIn("already issued", duplicate[0].reason)

    def test_replay_evidence_produces_all_four_target_classes(self):
        instance = planner()
        instance.update_replay_coverage(coverage("base"))
        cumulative_elements = []
        cumulative_branches = []
        cumulative_functions = []
        expected = [
            (target("new", 101, "parse"), ["parse:101:T"], [101], ["parse"],
             ["parse:101:T"], "new-reach"),
            (target("gain", 102, "parse"), ["parse:102:T"], [102], [],
             ["parse:102:T"], "increased-target-coverage"),
            (target("same", 103, "parse"), [], [], [], [], "reached-no-gain"),
            (target("miss", 104, "emit"), [], [], [], [], "not-reached"),
        ]
        for index, (candidate, new_elements, new_branches, new_functions,
                    target_elements, evidence_class) in enumerate(expected):
            plans, _ = instance.plan_round(
                [candidate],
                max_runs=1,
                proposals=[{
                    "target_id": candidate["target_id"],
                    "mode": "harness-entry",
                    "profile": "dfs",
                }],
            )
            cumulative_elements.extend(new_elements)
            cumulative_branches.extend(new_branches)
            cumulative_functions.extend(new_functions)
            outcome = instance.record_outcome(
                plans[0]["plan_id"],
                verified_outcome(
                    f"class-{index}",
                    elements=cumulative_elements,
                    branches=cumulative_branches,
                    functions=cumulative_functions,
                    target_elements=target_elements,
                ),
            )
            self.assertEqual(outcome["target_class"], evidence_class)

    def test_verified_replay_must_be_cumulative_and_match_target_reach(self):
        instance = planner()
        instance.update_replay_coverage(coverage(
            "base", elements=["entry"], branches=[1], functions=["main"]))
        plans, _ = instance.plan_round([target()], max_runs=1)
        malformed = verified_outcome(
            "bad", elements=[], branches=[], functions=["parse"])
        with self.assertRaisesRegex(AgolicAdmissionError, "include the corpus"):
            instance.record_outcome(plans[0]["plan_id"], malformed)
        self.assertEqual(len(instance.pending_plans()), 1)

        malformed = verified_outcome(
            "bad-claim",
            elements=["entry"],
            branches=[1],
            functions=["main"],
        )
        malformed["target_reached"] = True
        with self.assertRaisesRegex(AgolicAdmissionError, "disagrees"):
            instance.record_outcome(plans[0]["plan_id"], malformed)
        self.assertEqual(len(instance.pending_plans()), 1)

    def test_replay_identity_commits_to_coverage_and_integer_bounds_are_strict(self):
        instance = planner()
        original = coverage(
            "same", elements=["b", "a"], branches=[2, 1], functions=["main"])
        instance.update_replay_coverage(original)
        canonical = instance.snapshot()["coverage"]
        self.assertEqual(canonical["elements"], ["a", "b"])
        self.assertEqual(canonical["branches"], [1, 2])

        reused_identity = CoverageSnapshot.from_mapping({
            **canonical,
            "elements": ["a", "b", "c"],
        })
        with self.assertRaisesRegex(AgolicAdmissionError, "reused"):
            instance.update_replay_coverage(reused_identity)

        with self.assertRaisesRegex(AgolicAdmissionError, "must be an integer"):
            AgolicRunLevelPlanner(
                None,
                experiment_id="exp",
                program_id="program",
                program_sha256=PROGRAM_SHA,
                profiles=profiles(),
                max_history=1.5,
            )

    def test_witness_only_target_and_harness_failure_select_witness_mode(self):
        reviewed_witness = witness()
        instance = planner()
        instance.update_replay_coverage(coverage("base"))
        only = target(
            modes=["witness-guided"], witnesses=[reviewed_witness])
        plans, _ = instance.plan_round([only], max_runs=1)
        self.assertEqual(plans[0]["specification"]["mode"], "witness-guided")
        instance.record_outcome(plans[0]["plan_id"], {
            "status": "timeout",
            "replay_verified": False,
            "replay_identity": digest("base"),
        })

        both = target(
            "t2", 102, modes=["harness-entry", "witness-guided"],
            witnesses=[reviewed_witness])
        harness, _ = instance.plan_round([both], max_runs=1)
        self.assertEqual(harness[0]["specification"]["mode"], "harness-entry")
        instance.record_outcome(harness[0]["plan_id"], {
            "status": "timeout",
            "replay_verified": False,
            "replay_identity": digest("base"),
        })
        guided, _ = instance.plan_round([both], max_runs=1)
        self.assertEqual(guided[0]["specification"]["mode"], "witness-guided")

    def test_admission_rejects_unknown_fields_and_unreviewed_witness(self):
        instance = planner()
        instance.update_replay_coverage(coverage("base"))
        candidate = target(
            modes=["harness-entry", "witness-guided"], witnesses=[witness()])
        plans, diagnostics = instance.plan_round(
            [candidate],
            max_runs=2,
            proposals=[
                {"target_id": "t1", "profile": "dfs", "command": "ignored"},
                {
                    "target_id": "t1",
                    "profile": "dfs",
                    "mode": "witness-guided",
                    "witness_sha256": digest("not-reviewed"),
                },
            ],
        )
        self.assertEqual(plans, [])
        self.assertEqual([item.accepted for item in diagnostics], [False, False])
        self.assertIn("unknown fields", diagnostics[0].reason)
        self.assertIn("not reviewed", diagnostics[1].reason)

    def test_history_capacity_fails_closed_without_losing_deduplication(self):
        instance = planner(max_history=1)
        instance.update_replay_coverage(coverage("base"))
        plans, _ = instance.plan_round([target()], max_runs=1)
        instance.record_outcome(plans[0]["plan_id"], {
            "status": "timeout",
            "replay_verified": False,
            "replay_identity": digest("base"),
        })
        with self.assertRaisesRegex(AgolicAdmissionError, "capacity is exhausted"):
            instance.plan_round([target("t2", 102)], max_runs=1)

    def test_failed_atomic_commit_keeps_memory_and_disk_state_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "planner.json"
            instance = planner(state)
            instance.update_replay_coverage(coverage("base"))
            plans, _ = instance.plan_round([target()], max_runs=1)
            before_memory = instance.snapshot()
            before_disk = state.read_bytes()
            with mock.patch.object(
                    agolic_planning, "_atomic_json", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    instance.record_outcome(plans[0]["plan_id"], {
                        "status": "timeout",
                        "replay_verified": False,
                        "replay_identity": digest("base"),
                    })
            self.assertEqual(instance.snapshot(), before_memory)
            self.assertEqual(state.read_bytes(), before_disk)

    def test_state_reader_rejects_symlink_duplicate_json_and_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real.json"
            real.write_text("{}", encoding="utf-8")
            link = root / "state.json"
            link.symlink_to(real)
            with self.assertRaises(AgolicStateError):
                planner(link)

            duplicate = root / "duplicate.json"
            duplicate.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
            with self.assertRaisesRegex(AgolicStateError, "duplicate"):
                planner(duplicate)

            with tempfile.TemporaryDirectory() as other:
                state = Path(other) / "planner.json"
                original = planner(state)
                original.update_replay_coverage(coverage("base"))
                with self.assertRaisesRegex(AgolicStateError, "another experiment"):
                    AgolicRunLevelPlanner(
                        state,
                        experiment_id="different",
                        program_id="parser",
                        program_sha256=PROGRAM_SHA,
                        profiles=profiles(),
                    )

    def test_controller_retries_planning_and_replays_parallel_runs_sequentially(self):
        instance = planner()
        corpus = {
            "elements": [], "branches": [], "functions": [],
            "corpus_artifacts": [], "replay_identity": digest("empty"),
        }
        events = []
        proposal_calls = 0

        def replay_coverage():
            return dict(corpus)

        def propose(_context):
            nonlocal proposal_calls
            proposal_calls += 1
            if proposal_calls == 1:
                raise RuntimeError("transient model failure")
            return None

        def execute_run(plan):
            if plan["target"]["target_id"] == "slow":
                time.sleep(0.02)
            return {"artifact": digest(plan["plan_id"])}

        def replay_run(plan, run_result, prior):
            self.assertEqual(prior["elements"], corpus["elements"])
            target_data = plan["target"]
            element = f"{target_data['function']}:{target_data['line']}:T"
            corpus["elements"] = sorted(set(corpus["elements"]) | {element})
            corpus["branches"] = sorted(
                set(corpus["branches"]) | {target_data["target_branch"]})
            corpus["functions"] = sorted(
                set(corpus["functions"]) | {target_data["function"]})
            corpus["corpus_artifacts"] = sorted(
                set(corpus["corpus_artifacts"]) | {run_result["artifact"]})
            corpus["replay_identity"] = digest(
                "|".join(corpus["corpus_artifacts"]))
            events.append(("replay", target_data["target_id"]))
            return verified_outcome(
                corpus["replay_identity"],
                elements=corpus["elements"],
                branches=corpus["branches"],
                functions=corpus["functions"],
                artifacts=corpus["corpus_artifacts"],
                generated_artifacts=[run_result["artifact"]],
                target_elements=[element],
            ) | {"replay_identity": corpus["replay_identity"]}

        controller = AgolicRoundController(
            instance,
            replay_coverage=replay_coverage,
            execute_run=execute_run,
            replay_run=replay_run,
            preflight=lambda _plan: (True, "ok"),
            targets=lambda _context: [
                target("slow", 101, "parse"), target("fast", 102, "emit")],
            propose=propose,
            start_continuous=lambda: events.append(("continuous", "start")),
            finalize_continuous=lambda _handle: events.append(
                ("continuous", "stop")),
            max_workers=2,
        )
        summary = controller.run(
            wall_budget_seconds=5.0, max_rounds=4, minimum_round_seconds=0.0)
        self.assertEqual(summary["planning_failures"], 1)
        self.assertEqual(summary["completed_runs"], 2)
        self.assertEqual(len(instance.snapshot()["history"]), 2)
        self.assertEqual(len([event for event in events if event[0] == "replay"]), 2)
        self.assertEqual(events[0], ("continuous", "start"))
        self.assertEqual(events[-1], ("continuous", "stop"))


if __name__ == "__main__":
    unittest.main()
