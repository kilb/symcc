# RUN: python3 %s

import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from agentic_concolic_hooks import (  # noqa: E402
    AgenticBackendManager,
    CommandAgenticBackend,
    BuiltinAgenticPlanner,
    _decode_backend_hint,
    append_task,
    apply_hint,
    load_hints,
    query_agent,
)


class AgenticConcolicHookTests(unittest.TestCase):
    def test_hint_load_apply_and_task_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hints_path = root / "hints.json"
            hints_path.write_text(json.dumps({
                "hints": [{
                    "sha256": "abc",
                    "focus_bytes": "0-3",
                    "target_branch": 77,
                    "strategy": 2,
                    "s2f_actions": [[77, "solve"], [88, "sample"]],
                    "route": "concollmic",
                }]
            }), encoding="utf-8")

            hints = load_hints(str(hints_path))
            message = {"strategy": 0, "target_branch": 0, "focus_bytes": ""}
            apply_hint(message, hints["abc"], strategy_count=5)
            self.assertEqual(message["focus_bytes"], "0-3")
            self.assertEqual(message["target_branch"], 77)
            self.assertEqual(message["strategy"], 2)
            self.assertEqual(
                message["s2f_actions"], ((77, "solve"), (88, "sample")))
            self.assertEqual(message["agentic_route"], "concollmic")

            tasks_path = root / "tasks.jsonl"
            append_task(str(tasks_path), {"schema": 1, "path": "seed"})
            self.assertEqual(
                json.loads(tasks_path.read_text(encoding="utf-8")),
                {"schema": 1, "path": "seed"},
            )

    def test_query_agent_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "agent.py"
            script.write_text(
                "import json, sys\n"
                "task = json.load(sys.stdin)\n"
                "print(json.dumps({"
                "'strategy': 1, "
                "'focus_bytes': '2-4', "
                "'target_branch': task['open_branches'][0]}))\n",
                encoding="utf-8",
            )
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
            hint = query_agent(command, {"open_branches": [99]}, timeout=1.0)
            self.assertEqual(hint["strategy"], 1)
            self.assertEqual(hint["focus_bytes"], "2-4")
            self.assertEqual(hint["target_branch"], 99)

    def test_query_agent_failure_is_empty_hint(self):
        hint = query_agent("python3 -c 'import sys; sys.exit(2)'", {}, timeout=1.0)
        self.assertEqual(hint, {})

    def test_query_agent_rejects_duplicate_members_and_nonfinite_numbers(self):
        cases = (
            "print('{\"strategy\":1,\"strategy\":2}')",
            "print('{\"strategy\":NaN}')",
        )
        for source in cases:
            with self.subTest(source=source):
                command = (
                    f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
                )
                self.assertEqual(query_agent(command, {}, timeout=1.0), {})

    def test_query_agent_rejects_output_over_hard_byte_limit(self):
        source = (
            "import sys; "
            "sys.stdout.write('{\"strategy\":1}' + ' ' * (1024 * 1024))"
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
        self.assertEqual(query_agent(command, {}, timeout=1.0), {})

    def test_backend_manager_validates_caches_and_runs_async(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "agent.py"
            script.write_text(
                "import json, sys\n"
                "task = json.load(sys.stdin)\n"
                "print(json.dumps({'strategy': 2, 'target_branch': 44, "
                "'unknown': task.get('schema')}))\n",
                encoding="utf-8",
            )
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
            manager = AgenticBackendManager(
                [CommandAgenticBackend(command)],
                strategy_count=3,
                timeout=1.0,
            )
            task = {"schema": 1, "sha256": "abc", "input_path": "seed"}
            self.assertTrue(manager.submit(task))
            deadline = __import__("time").monotonic() + 2.0
            ready = []
            while not ready and __import__("time").monotonic() < deadline:
                ready = manager.drain()
                __import__("time").sleep(0.01)
            manager.close()
            self.assertEqual(ready[0][0], ("abc", "seed"))
            self.assertEqual(
                ready[0][1], {"strategy": 2, "target_branch": 44})
            self.assertNotIn("unknown", ready[0][1])

    def test_chat_completion_response_is_decoded(self):
        wrapped = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "strategy": 1, "s2f_actions": [[7, "solve"]]
                    })
                }
            }]
        }
        self.assertEqual(_decode_backend_hint(wrapped)["strategy"], 1)

    def test_builtin_planner_persists_strategy_and_branch_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "planner.json"
            planner = BuiltinAgenticPlanner(str(state), strategy_count=3,
                                            exploration=0.0)
            task = {
                "strategy": 0,
                "target_branch": 0,
                "open_branches": [101, 202],
                "focus_bytes": "4-8",
            }
            hint = planner.suggest(task)
            self.assertIn(hint["strategy"], {0, 1, 2})
            self.assertIn(hint["target_branch"], {101, 202})
            self.assertEqual(hint["focus_bytes"], "4-8")

            observed = dict(task, strategy=2, target_branch=202)
            planner.observe(observed, {
                "generated": 3,
                "solver_sat": 2,
                "solver_time_us": 1000,
                "target_branch": 202,
                "target_reached": True,
                "poly_cache_hits": 1,
                "poly_samples": 1,
            })
            planner.save()

            reloaded = BuiltinAgenticPlanner(str(state), strategy_count=3,
                                             exploration=0.0)
            self.assertEqual(reloaded.suggest(task)["strategy"], 2)
            self.assertEqual(
                reloaded.state["branches"]["202"]["success"], 1)

    def test_builtin_planner_learns_cottontail_route_from_taints(self):
        planner = BuiltinAgenticPlanner(
            None, strategy_count=7, exploration=0.0, route_mode="cottontail")
        task = {
            "sha256": "b" * 64,
            "input_path": "seed",
            "strategy": 0,
        }
        planner.observe(task, {
            "generated": 1,
            "solver_sat": 1,
            "solver_time_us": 500,
            "comparison_taints": [
                [11, 1234, 3, 4, 6, 1, 1],
            ],
        })
        hint = planner.suggest(task)
        self.assertEqual(hint["route"], "cottontail")
        self.assertEqual(hint["target_branch"], 1234)
        self.assertEqual(hint["focus_bytes"], "2-8")
        self.assertEqual(hint["strategy"], 4)

    def test_builtin_planner_learns_gordian_route_for_solver_hostile_task(self):
        planner = BuiltinAgenticPlanner(
            None, strategy_count=7, exploration=0.0, route_mode="gordian")
        task = {
            "sha256": "c" * 64,
            "input_path": "seed",
            "strategy": 0,
            "target_branch": 999,
        }
        planner.observe(task, {
            "generated": 0,
            "solver_unknown": 1,
            "solver_time_us": 500000,
            "target_branch": 999,
        })
        hint = planner.suggest(task)
        self.assertEqual(hint["route"], "gordian")
        self.assertEqual(hint["target_branch"], 999)
        self.assertEqual(hint["strategy"], 6)
        self.assertEqual(hint["s2f_actions"], [[999, "sample"]])


if __name__ == "__main__":
    unittest.main()
