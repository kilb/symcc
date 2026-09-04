# RUN: python3 %s

import hashlib
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from agentic_concolic_hooks import (  # noqa: E402
    AgenticBackend,
    CommandAgenticBackend,
)
from structured_agentic_concolic import (  # noqa: E402
    AgenticProtocolError,
    DecisionLedger,
    RESPONSE_SCHEMA,
    StructuredAgenticController,
    StructuredAgenticPolicy,
    compare_ablation_ledgers,
    summarize_ledger,
)
from verified_proposals import VerifiedProposalManager  # noqa: E402


PROMPT_SHA256 = hashlib.sha256(b"fixed test prompt").hexdigest()


class ScriptedBackend(AgenticBackend):
    name = "scripted"
    provider = "test-provider"
    model = "test-model-2026-08-26"
    prompt_sha256 = PROMPT_SHA256

    def __init__(self, callback):
        self.callback = callback
        self.requests = []

    def query(self, task, timeout):
        self.requests.append(task)
        return self.callback(task)


def response(request, actions, *, input_tokens=0, output_tokens=0, **extra):
    return {
        "schema": RESPONSE_SCHEMA,
        "request_id": request["request_id"],
        "task_sha256": request["task_sha256"],
        "actions": actions,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        **extra,
    }


def policy(mode="online", **overrides):
    values = {
        "mode": mode,
        "strategy_count": 7,
        "experiment_id": "f455-test",
        "program_identity": "program-v1",
        "trigger_mode": "always",
        "max_requests": 16,
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 1_000_000,
        "max_model_time_us": 100_000_000,
        "max_input_tokens_per_request": 16_384,
        "max_output_tokens_per_request": 4_096,
        "timeout_ms": 2_000,
    }
    values.update(overrides)
    return StructuredAgenticPolicy(**values)


def task(path, digest=None):
    content = Path(path).read_bytes()
    return {
        "schema": 1,
        "input_path": str(path),
        "sha256": digest or hashlib.sha256(content).hexdigest(),
        "strategy": 0,
        "target_branch": 77,
        "open_branches": [77, 88],
        "focus_bytes": "0-1",
        "comparison_taints": [[1, 77, 2, 0, 1, 1, 1]],
    }


def drain(controller, timeout=2.0):
    deadline = time.monotonic() + timeout
    decisions = []
    while not decisions and time.monotonic() < deadline:
        decisions = controller.drain()
        if not decisions:
            time.sleep(0.005)
    return decisions


class StructuredAgenticConcolicTests(unittest.TestCase):
    def test_online_schedule_and_candidate_enter_verified_proposal_funnel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"{}")

            def answer(request):
                return response(request, [
                    {
                        "kind": "schedule",
                        "strategy": 4,
                        "target_branch": 77,
                        "focus_bytes": "0-1",
                        "s2f_actions": [[77, "solve"]],
                        "route": "cottontail",
                    },
                    {
                        "kind": "candidate",
                        "proposal_kind": "history_acquisition",
                        "data_hex": "7b2261223a317d",
                        "target_branch": 77,
                    },
                ])

            controller = StructuredAgenticController(
                [ScriptedBackend(answer)], policy(), root / "ledger.jsonl")
            original = task(seed)
            self.assertTrue(controller.submit(original, {"strategy": 1}))
            decision = drain(controller)[0]
            self.assertEqual(decision.source, "model")
            self.assertEqual(decision.hint["strategy"], 4)
            self.assertEqual(len(decision.proposals), 1)
            proposal = decision.proposals[0]
            self.assertEqual(proposal["history_seed_id"], original["sha256"])

            manager = VerifiedProposalManager(
                "", str(root / "proposals"), max_candidate_bytes=1024)
            proposal_id = manager.ingest(proposal)
            self.assertIsNotNone(proposal_id)
            record = manager.records[proposal_id]
            self.assertEqual(record.kind, "history_acquisition")
            self.assertEqual(Path(record.candidate_path).read_bytes(), b'{"a":1}')
            controller.record_candidate_admission(
                decision.decision_id, record.candidate_sha256, proposal_id)
            self.assertEqual(
                controller.decision_for_candidate(record.candidate_sha256),
                decision.decision_id,
            )
            with self.assertRaisesRegex(
                    AgenticProtocolError, "source does not match"):
                controller.record_hint_selection(
                    decision.decision_id,
                    {**original, "sha256": "f" * 64},
                )
            controller.record_hint_selection(decision.decision_id, original)
            controller.observe(decision.decision_id, original, {
                "target_branch": 77,
                "target_reached": True,
                "generated": 1,
                "solver_time_us": 123,
            }, {"elapsed": 0.01, "coverage_delta": 2, "retcode": 0})
            controller.close()

            summary = summarize_ledger(root / "ledger.jsonl")
            self.assertEqual(summary["requests"], 1)
            self.assertEqual(summary["valid_responses"], 1)
            self.assertEqual(summary["candidate_actions"], 1)
            self.assertEqual(summary["outcomes"], 1)
            self.assertEqual(summary["hint_selections"], 1)
            self.assertEqual(summary["target_reached"], 1)
            self.assertEqual(summary["coverage_delta"], 2)

    def test_shadow_validates_model_but_applies_only_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"x")
            backend = ScriptedBackend(lambda request: response(request, [
                {"kind": "schedule", "strategy": 6},
                {
                    "kind": "candidate",
                    "proposal_kind": "solve_complete",
                    "data_hex": "4142",
                    "target_branch": 77,
                },
            ]))
            controller = StructuredAgenticController(
                [backend], policy("shadow"), root / "shadow.jsonl")
            controller.submit(task(seed), {"strategy": 2, "target_branch": 88})
            decision = drain(controller)[0]
            self.assertEqual(decision.source, "fallback")
            self.assertEqual(decision.hint["strategy"], 2)
            self.assertEqual(decision.hint["target_branch"], 88)
            self.assertEqual(decision.proposals, ())
            self.assertEqual(len(decision.model_actions), 2)
            controller.close()
            summary = summarize_ledger(root / "shadow.jsonl")
            self.assertEqual(summary["mode"], "shadow")
            self.assertEqual(summary["valid_responses"], 1)
            self.assertEqual(summary["candidate_actions"], 0)

    def test_fallback_ablation_makes_no_backend_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"x")
            controller = StructuredAgenticController(
                [], policy("fallback"), root / "fallback.jsonl")
            controller.submit(task(seed), {"strategy": 3})
            decision = controller.drain()[0]
            self.assertEqual(decision.hint, {"strategy": 3})
            self.assertEqual(decision.fallback_reason, "ablation_fallback_mode")
            controller.close()
            summary = summarize_ledger(root / "fallback.jsonl")
            self.assertEqual(summary["requests"], 0)
            self.assertEqual(summary["fallbacks"], 1)

    def test_response_binding_unknown_fields_and_noncanonical_hex_fail_closed(self):
        cases = {
            "wrong-binding": lambda request: {
                **response(request, [{"kind": "schedule", "strategy": 1}]),
                "request_id": "0" * 64,
            },
            "unknown-root": lambda request: response(
                request, [{"kind": "schedule", "strategy": 1}], surprise=1),
            "unknown-action": lambda request: response(request, [
                {"kind": "schedule", "strategy": 1, "reasoning": "trust me"}
            ]),
            "uppercase-hex": lambda request: response(request, [{
                "kind": "candidate",
                "proposal_kind": "solve_complete",
                "data_hex": "AB",
                "target_branch": 77,
            }]),
        }
        for name, callback in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                seed = root / "seed"
                seed.write_bytes(b"x")
                controller = StructuredAgenticController(
                    [ScriptedBackend(callback)], policy(), root / "ledger.jsonl")
                controller.submit(task(seed), {"strategy": 5})
                decision = drain(controller)[0]
                self.assertEqual(decision.source, "fallback")
                self.assertEqual(decision.hint["strategy"], 5)
                self.assertEqual(decision.fallback_reason, "invalid_response")
                self.assertGreater(controller.snapshot()["input_tokens"], 0)
                self.assertGreater(controller.snapshot()["output_tokens"], 0)
                controller.close()

    def test_global_request_budget_is_persistent_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"x")
            backend = ScriptedBackend(lambda request: response(
                request, [{"kind": "schedule", "strategy": 1}]))
            bounded = policy(max_requests=1)
            controller = StructuredAgenticController(
                [backend], bounded, root / "ledger.jsonl")
            controller.submit(task(seed), {"strategy": 4})
            self.assertEqual(drain(controller)[0].source, "model")
            controller.close()

            restarted_backend = ScriptedBackend(lambda request: response(
                request, [{"kind": "schedule", "strategy": 6}]))
            restarted = StructuredAgenticController(
                [restarted_backend], bounded, root / "ledger.jsonl")
            restarted.submit(task(seed), {"strategy": 4})
            decision = restarted.drain()[0]
            self.assertEqual(decision.fallback_reason, "budget_exhausted")
            self.assertEqual(restarted_backend.requests, [])
            restarted.close()

    def test_candidate_global_budget_rejects_whole_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"x")
            backend = ScriptedBackend(lambda request: response(request, [{
                "kind": "candidate",
                "proposal_kind": "solve_complete",
                "data_hex": "4142",
                "target_branch": 77,
            }]))
            controller = StructuredAgenticController(
                [backend], policy(max_candidate_bytes=1), root / "ledger.jsonl")
            controller.submit(task(seed), {"strategy": 2})
            decision = drain(controller)[0]
            self.assertEqual(decision.source, "fallback")
            self.assertEqual(decision.fallback_reason, "candidate_budget_exhausted")
            self.assertEqual(decision.proposals, ())
            controller.close()

    def test_provider_usage_dominates_declared_and_byte_estimates(self):
        class MeteredBackend(ScriptedBackend):
            def query_with_metadata(self, request, timeout):
                self.requests.append(request)
                return self.callback(request), {
                    "input_tokens": 999,
                    "output_tokens": 555,
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"x")
            backend = MeteredBackend(lambda request: response(
                request, [{"kind": "schedule", "strategy": 1}]))
            controller = StructuredAgenticController(
                [backend], policy(), root / "ledger.jsonl")
            controller.submit(task(seed))
            self.assertEqual(drain(controller)[0].source, "model")
            snapshot = controller.snapshot()
            self.assertEqual(snapshot["input_tokens"], 999)
            self.assertEqual(snapshot["output_tokens"], 555)
            controller.close()

    def test_execution_outcome_is_injected_into_next_iteration_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"x")
            backend = ScriptedBackend(lambda request: response(
                request, [{"kind": "schedule", "strategy": 1}]))
            controller = StructuredAgenticController(
                [backend], policy(), root / "ledger.jsonl")
            original = task(seed)
            controller.submit(original)
            first = drain(controller)[0]
            controller.observe(first.decision_id, original, {
                "target_branch": 77,
                "target_reached": False,
                "generated": 0,
                "solver_unknown": 1,
            }, {"elapsed": 0.02})
            controller.submit(original)
            drain(controller)
            self.assertEqual(len(backend.requests), 2)
            self.assertEqual(backend.requests[1]["episode"]["iteration"], 1)
            self.assertEqual(len(backend.requests[1]["history"]), 1)
            self.assertEqual(
                backend.requests[1]["history"][0]["decision_id"],
                first.decision_id,
            )
            controller.close()

    def test_reactive_trigger_uses_authoritative_stall_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"x")
            backend = ScriptedBackend(lambda request: response(
                request, [{"kind": "schedule", "strategy": 1}]))
            controller = StructuredAgenticController(
                [backend], policy(
                    trigger_mode="reactive", plateau_threshold=3),
                root / "ledger.jsonl",
            )

            productive = {
                **task(seed),
                "generated": 2,
                "coverage_delta": 1,
                "symbolic_branches": 4,
            }
            self.assertFalse(controller.submit(productive))
            self.assertEqual(backend.requests, [])

            stalled = {
                **task(seed),
                "generated": 0,
                "coverage_delta": 0,
                "symbolic_branches": 4,
                "solver_unknown": 1,
            }
            self.assertTrue(controller.submit(stalled))
            self.assertEqual(drain(controller)[0].source, "model")
            snapshot = controller.snapshot()
            self.assertEqual(snapshot["trigger_evaluations"], 2)
            self.assertEqual(snapshot["triggered_requests"], 1)
            self.assertEqual(snapshot["suppressed_requests"], 1)
            controller.close()

    def test_candidate_outcome_returns_to_originating_episode_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"x")
            candidate = b"candidate"
            candidate_sha = hashlib.sha256(candidate).hexdigest()
            backend = ScriptedBackend(lambda request: response(request, [{
                "kind": "candidate",
                "proposal_kind": "solve_complete",
                "data_hex": candidate.hex(),
                "target_branch": 77,
            }]))
            controller = StructuredAgenticController(
                [backend], policy(), root / "ledger.jsonl")
            original = task(seed)
            controller.submit(original)
            decision = drain(controller)[0]
            controller.record_candidate_admission(
                decision.decision_id, candidate_sha, "a" * 64)
            executed = {**original, "sha256": candidate_sha}
            controller.observe(
                decision.decision_id,
                executed,
                {"generated": 1},
                {"coverage_delta": 2},
            )
            controller.submit(original)
            drain(controller)
            self.assertEqual(
                backend.requests[1]["history"][0]["executed_input_sha256"],
                candidate_sha,
            )
            controller.close()

    def test_reactive_plateau_is_persistent_and_triggers_at_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"x")
            backend = ScriptedBackend(lambda request: response(
                request, [{"kind": "schedule", "strategy": 1}]))
            reactive = policy(
                trigger_mode="reactive", plateau_threshold=2)
            stalled = {
                **task(seed),
                "generated": 1,
                "coverage_delta": 0,
                "symbolic_branches": 0,
            }
            path = root / "ledger.jsonl"
            controller = StructuredAgenticController(
                [backend], reactive, path)
            self.assertFalse(controller.submit(stalled))
            controller.close()

            restarted = StructuredAgenticController(
                [backend], reactive, path)
            self.assertTrue(restarted.submit(stalled))
            self.assertEqual(drain(restarted)[0].source, "model")
            self.assertEqual(restarted.snapshot()["plateau_count"], 2)
            restarted.close()

    def test_ledger_rejects_tamper_duplicate_member_partial_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.jsonl"
            ledger = DecisionLedger(ledger_path)
            ledger.append("event", {"value": 1})
            ledger.close()
            original = ledger_path.read_bytes()

            tampered = bytearray(original)
            tampered[tampered.index(b'"value":1') + len('"value":')] = ord("2")
            ledger_path.write_bytes(tampered)
            with self.assertRaisesRegex(AgenticProtocolError, "bad digest"):
                DecisionLedger.read(ledger_path)

            ledger_path.write_bytes(
                b'{"schema":"symcc-agentic-ledger-v1","schema":"x"}\n')
            with self.assertRaisesRegex(AgenticProtocolError, "duplicate JSON"):
                DecisionLedger.read(ledger_path)

            ledger_path.write_bytes(original[:-1])
            with self.assertRaisesRegex(AgenticProtocolError, "partial"):
                DecisionLedger.read(ledger_path)

            ledger_path.write_bytes(original)
            link = root / "ledger-link"
            link.symlink_to(ledger_path)
            with self.assertRaises(OSError):
                DecisionLedger.read(link)

    def test_ledger_policy_identity_rejects_model_or_mode_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ScriptedBackend(lambda request: response(
                request, [{"kind": "schedule", "strategy": 1}]))
            controller = StructuredAgenticController(
                [backend], policy(), root / "ledger.jsonl")
            controller.close()
            with self.assertRaisesRegex(
                    AgenticProtocolError, "different immutable policy"):
                StructuredAgenticController(
                    [backend], policy("shadow"), root / "ledger.jsonl")

            class ChangedModel(ScriptedBackend):
                model = "changed-model"

            with self.assertRaisesRegex(
                    AgenticProtocolError, "different immutable policy"):
                StructuredAgenticController(
                    [ChangedModel(backend.callback)], policy(), root / "ledger.jsonl")

    def test_command_backend_identity_binds_executable_wrapper_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "backend.py"
            script.write_text("print('{}')\n", encoding="ascii")
            command = f"{sys.executable} {script}"
            backend = CommandAgenticBackend(
                command,
                name="command",
                provider="local",
                model="fixed-model",
                prompt_sha256=PROMPT_SHA256,
            )
            path = root / "ledger.jsonl"
            controller = StructuredAgenticController([backend], policy(), path)
            controller.close()
            script.write_text("print('{\\\"changed\\\":true}')\n", encoding="ascii")
            with self.assertRaisesRegex(
                    AgenticProtocolError, "different immutable policy"):
                StructuredAgenticController([backend], policy(), path)

    def test_close_waits_for_bounded_inflight_request_and_records_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"x")

            def slow(request):
                time.sleep(0.03)
                return response(request, [{"kind": "schedule", "strategy": 2}])

            controller = StructuredAgenticController(
                [ScriptedBackend(slow)], policy(), root / "ledger.jsonl")
            controller.submit(task(seed))
            controller.close()
            summary = summarize_ledger(root / "ledger.jsonl")
            self.assertEqual(summary["requests"], 1)
            self.assertEqual(summary["valid_responses"], 1)

    def test_paired_ablation_requires_same_policy_and_exact_task_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"x")
            backend = ScriptedBackend(lambda request: response(
                request, [{"kind": "schedule", "strategy": 1}]))
            paths = {}
            for mode in ("online", "shadow", "fallback"):
                path = root / f"{mode}.jsonl"
                controller = StructuredAgenticController(
                    [backend], policy(mode), path)
                original = task(seed)
                controller.submit(original, {"strategy": 0})
                decision = drain(controller)[0]
                controller.observe(decision.decision_id, original, {
                    "target_branch": 77,
                    "target_reached": mode == "online",
                    "generated": 1 if mode == "online" else 0,
                }, {"coverage_delta": 1 if mode == "online" else 0})
                controller.close()
                paths[mode] = path
            comparison = compare_ablation_ledgers(paths)
            self.assertEqual(comparison["task_count"], 1)
            self.assertEqual(
                comparison["deltas_vs_fallback"]["online"]["coverage_delta"], 1)
            self.assertGreater(
                comparison["deltas_vs_fallback"]["shadow"]["model_time_us"],
                -1,
            )

            different = root / "different.jsonl"
            controller = StructuredAgenticController(
                [backend], policy("online", experiment_id="other"), different)
            controller.submit(task(seed))
            drain(controller)
            controller.close()
            with self.assertRaisesRegex(AgenticProtocolError, "base policy"):
                compare_ablation_ledgers({
                    "fallback": paths["fallback"],
                    "different": different,
                })

    def test_restart_retires_orphan_request_once_and_restores_fallback_iteration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "online.jsonl"
            backend = ScriptedBackend(lambda request: response(
                request, [{"kind": "schedule", "strategy": 1}]))
            controller = StructuredAgenticController(
                [backend], policy(), path)
            policy_sha = controller.policy_sha256
            controller.close()
            ledger = DecisionLedger(path)
            ledger.append("request_submitted", {
                "policy_sha256": policy_sha,
                "request_id": "1" * 64,
                "episode_id": "2" * 64,
                "iteration": 0,
                "task_sha256": "3" * 64,
                "task": {},
                "history": [],
                "estimated_input_tokens": 1,
            })
            ledger.close()
            recovered = StructuredAgenticController([backend], policy(), path)
            recovered.close()
            recovered_again = StructuredAgenticController([backend], policy(), path)
            recovered_again.close()
            events = [record["event"] for record in DecisionLedger.read(path)]
            self.assertEqual(events.count("request_recovered_cancelled"), 1)

            fallback_path = root / "fallback.jsonl"
            fallback = StructuredAgenticController(
                [backend], policy("fallback"), fallback_path)
            seed = root / "seed"
            seed.write_bytes(b"x")
            fallback.submit(task(seed))
            fallback.close()
            restarted = StructuredAgenticController(
                [backend], policy("fallback"), fallback_path)
            restarted.submit(task(seed))
            restarted.close()
            restored_twice = StructuredAgenticController(
                [backend], policy("fallback"), fallback_path)
            restored_twice.close()
            iterations = [
                record["payload"]["iteration"]
                for record in DecisionLedger.read(fallback_path)
                if record["event"] == "fallback_decision"
            ]
            self.assertEqual(iterations, [0, 1])
            decision_ids = [
                record["payload"]["decision_id"]
                for record in DecisionLedger.read(fallback_path)
                if record["event"] == "fallback_decision"
            ]
            self.assertEqual(len(decision_ids), len(set(decision_ids)))


if __name__ == "__main__":
    unittest.main()
