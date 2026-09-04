#!/usr/bin/env python3
# RUN: python3 %s

import hashlib
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

import online_value_profile as online_value_profile_module  # noqa: E402
from online_value_profile import (  # noqa: E402
    OnlineValueProfileCoordinator,
    install_value_profile_update,
    value_profile_update_payload,
)


class OnlineValueProfileTests(unittest.TestCase):
    CONTEXT_A = "a" * 64
    CONTEXT_B = "b" * 64

    @staticmethod
    def telemetry(
        context: str, value: int, *, observations: int = 4, site: int = 11
    ) -> dict:
        return {
            "empirical_value_profile_context": context,
            "empirical_value_profiles": [[
                site, 8, observations, 0, [[value, observations]],
            ]],
        }

    def test_cross_worker_admission_and_semantic_generations(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, min_observations=8,
                max_distinct_values=2,
                publish_interval_seconds=0)
            first_observation = self.telemetry(self.CONTEXT_A, 1)
            first_observation["empirical_domain_attempts"] = 3
            first_observation["empirical_domain_validated"] = 1
            self.assertTrue(coordinator.observe(first_observation))
            self.assertIsNone(coordinator.publish(force=True))

            self.assertTrue(coordinator.observe(
                self.telemetry(self.CONTEXT_A, 1)))
            first = coordinator.publish(force=True)
            self.assertIsNotNone(first)
            assert first is not None
            self.assertEqual(first.profile_count, 1)
            self.assertIn(
                f"profile {self.CONTEXT_A} 11 8 1 1\n".encode("ascii"),
                first.content,
            )
            uncached = value_profile_update_payload(first, "")
            self.assertEqual(uncached["empirical_profile_content"], first.content)
            cached = value_profile_update_payload(first, first.version)
            self.assertNotIn("empirical_profile_content", cached)
            self.assertEqual(cached["empirical_profile_version"], first.version)

            # Counts and evidence digest change, but the solver domain does
            # not. Keep one generation so workers receive no redundant bytes.
            coordinator.observe(self.telemetry(self.CONTEXT_A, 1))
            self.assertIsNone(coordinator.publish(force=True))
            self.assertEqual(coordinator.current.version, first.version)

            coordinator.observe(self.telemetry(self.CONTEXT_A, 2))
            second = coordinator.publish(force=True)
            self.assertIsNotNone(second)
            assert second is not None
            self.assertNotEqual(second.version, first.version)
            self.assertIn(
                f"profile {self.CONTEXT_A} 11 8 2 1 2\n".encode("ascii"),
                second.content,
            )
            self.assertEqual(coordinator.snapshot()[
                "empirical_domain_attempts"], 3)
            self.assertEqual(coordinator.snapshot()[
                "empirical_domain_validated"], 1)

            # A third value exceeds the admitted domain. Publish an explicit
            # empty generation so workers do not retain the stale two-value
            # sidecar indefinitely.
            coordinator.observe(self.telemetry(self.CONTEXT_A, 3))
            withdrawn = coordinator.publish(force=True)
            self.assertIsNotNone(withdrawn)
            assert withdrawn is not None
            self.assertEqual(withdrawn.profile_count, 0)
            self.assertIn(b"profile_count 0\n", withdrawn.content)
            self.assertEqual(coordinator.current.version, withdrawn.version)

    def test_contexts_stay_separate_and_resume_is_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, min_observations=1,
                publish_interval_seconds=0)
            coordinator.observe_many([
                self.telemetry(self.CONTEXT_A, 1, observations=1),
                self.telemetry(self.CONTEXT_B, 2, observations=1),
            ])
            published = coordinator.publish(force=True)
            self.assertIsNotNone(published)
            assert published is not None
            self.assertEqual(published.profile_count, 2)

            resumed = OnlineValueProfileCoordinator(
                temporary, min_observations=1,
                publish_interval_seconds=0)
            self.assertIsNotNone(resumed.current)
            self.assertEqual(resumed.current.version, published.version)
            self.assertEqual(len(resumed.records), 2)

            runtime = Path(temporary) / "current.runtime"
            runtime.write_bytes(runtime.read_bytes() + b"trailing\n")
            corrupt = OnlineValueProfileCoordinator(
                temporary, min_observations=1,
                publish_interval_seconds=0)
            self.assertIsNone(corrupt.current)

    def test_install_requires_matching_content_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "profile.runtime"
            content = b"strict sidecar bytes\n"
            version = hashlib.sha256(content).hexdigest()
            installed, enabled = install_value_profile_update(
                {
                    "empirical_profile_version": version,
                    "empirical_profile_content": content,
                },
                destination,
                "",
            )
            self.assertTrue(enabled)
            self.assertEqual(installed, version)
            self.assertEqual(destination.read_bytes(), content)

            cached, enabled = install_value_profile_update(
                {"empirical_profile_version": version},
                destination,
                version,
            )
            self.assertTrue(enabled)
            self.assertEqual(cached, version)

            destination.write_bytes(b"truncated")
            rejected, enabled = install_value_profile_update(
                {"empirical_profile_version": version},
                destination,
                version,
            )
            self.assertFalse(enabled)
            self.assertEqual(rejected, "")

            restored, enabled = install_value_profile_update(
                {
                    "empirical_profile_version": version,
                    "empirical_profile_content": content,
                },
                destination,
                "",
            )
            self.assertTrue(enabled)
            self.assertEqual(restored, version)

            rejected, enabled = install_value_profile_update(
                {
                    "empirical_profile_version": "f" * 64,
                    "empirical_profile_content": content,
                },
                destination,
                version,
            )
            self.assertFalse(enabled)
            self.assertEqual(rejected, "")
            self.assertEqual(destination.read_bytes(), content)

    def test_state_window_and_malformed_profiles_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, window=2, min_observations=1,
                publish_interval_seconds=0)
            malformed = self.telemetry(self.CONTEXT_A, 1)
            malformed["empirical_value_profiles"][0][4] = [
                [value, 1] for value in range(65)
            ]
            self.assertFalse(coordinator.observe(malformed))
            for site in range(1, 4):
                coordinator.observe(self.telemetry(
                    self.CONTEXT_A, site, observations=1, site=site))
            coordinator.publish(force=True)
            self.assertEqual(len(coordinator.records), 2)
            state = json.loads(
                (Path(temporary) / "state.json").read_text(encoding="ascii"))
            self.assertEqual(len(state["records"]), 2)

            resized = OnlineValueProfileCoordinator(
                temporary, window=1, min_observations=1,
                publish_interval_seconds=0)
            self.assertTrue(resized.dirty)
            replacement = resized.publish(force=True)
            self.assertIsNotNone(replacement)
            assert replacement is not None
            self.assertEqual(replacement.profile_count, 1)
            self.assertIn(b"profile_count 1\n", replacement.content)

            stricter = OnlineValueProfileCoordinator(
                temporary, window=1, min_observations=2,
                publish_interval_seconds=0)
            self.assertTrue(stricter.dirty)
            withdrawal = stricter.publish(force=True)
            self.assertIsNotNone(withdrawal)
            assert withdrawal is not None
            self.assertEqual(withdrawal.profile_count, 0)

    def test_publication_io_failure_is_fail_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "not-a-directory"
            blocker.write_bytes(b"x")
            coordinator = OnlineValueProfileCoordinator(
                blocker / "profile", min_observations=1,
                publish_interval_seconds=0)
            coordinator.observe(self.telemetry(
                self.CONTEXT_A, 1, observations=1))
            self.assertIsNone(coordinator.publish(force=True))
            self.assertTrue(coordinator.dirty)
            self.assertEqual(coordinator.publication_failures, 1)

    def test_publish_interval_retries_without_another_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, min_observations=1,
                publish_interval_seconds=10)
            coordinator.observe(self.telemetry(
                self.CONTEXT_A, 1, observations=1))
            self.assertIsNone(coordinator.publish(now=5))
            self.assertTrue(coordinator.dirty)
            published = coordinator.publish(now=11)
            self.assertIsNotNone(published)
            self.assertFalse(coordinator.dirty)

    def test_low_yield_domain_is_suppressed_then_reexplored(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, window=2, min_observations=1,
                publish_interval_seconds=0,
                feedback_min_solver_queries=2,
                feedback_min_validated_ratio_ppm=125_000,
                feedback_min_solver_time_us=0,
            )
            clean = self.telemetry(
                self.CONTEXT_A, 1, observations=1)
            coordinator.observe(clean)
            admitted = coordinator.publish(force=True)
            self.assertIsNotNone(admitted)
            assert admitted is not None
            self.assertEqual(admitted.profile_count, 1)

            failed = self.telemetry(
                self.CONTEXT_A, 1, observations=1)
            failed["empirical_domain_feedback"] = [[
                11, 8, [1], 1, 0, 1, 0, 0, 0, 1, 0,
            ]]
            coordinator.observe(failed)
            self.assertIsNone(coordinator.publish(force=True))
            coordinator.observe(failed)
            suppressed = coordinator.publish(force=True)
            self.assertIsNotNone(suppressed)
            assert suppressed is not None
            self.assertEqual(suppressed.profile_count, 0)
            self.assertEqual(suppressed.suppressed_count, 1)
            self.assertEqual(coordinator.snapshot()[
                "suppression_generations"], 1)

            # One fresh record evicts one of the two failed samples. The
            # evidence drops below the gate and bounded exploration resumes.
            coordinator.observe(clean)
            reprobed = coordinator.publish(force=True)
            self.assertIsNotNone(reprobed)
            assert reprobed is not None
            self.assertEqual(reprobed.profile_count, 1)
            self.assertEqual(reprobed.suppressed_count, 0)

    def test_cost_floor_preserves_cheap_failures_until_budget_is_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, window=3, min_observations=1,
                publish_interval_seconds=0,
                feedback_min_solver_queries=2,
                feedback_min_validated_ratio_ppm=125_000,
                feedback_min_solver_time_us=1_000,
            )
            clean = self.telemetry(
                self.CONTEXT_A, 1, observations=1)
            coordinator.observe(clean)
            admitted = coordinator.publish(force=True)
            self.assertIsNotNone(admitted)

            cheap_failure = self.telemetry(
                self.CONTEXT_A, 1, observations=1)
            cheap_failure["empirical_domain_feedback"] = [[
                11, 8, [1], 1, 0, 1, 0, 0, 0, 1, 0, 400,
            ]]
            coordinator.observe(cheap_failure)
            self.assertIsNone(coordinator.publish(force=True))
            coordinator.observe(cheap_failure)
            self.assertIsNone(coordinator.publish(force=True))
            self.assertEqual(coordinator.current.profile_count, 1)

            # Three failed queries cross both the query-count and cumulative
            # cost gates. The proof records the exact 1,200 us budget.
            coordinator.observe(cheap_failure)
            suppressed = coordinator.publish(force=True)
            self.assertIsNotNone(suppressed)
            assert suppressed is not None
            self.assertEqual(suppressed.profile_count, 0)
            artifact_path = (
                Path(temporary) / "generations"
                / f"{suppressed.artifact_sha256}.json"
            )
            artifact = json.loads(artifact_path.read_text(encoding="ascii"))
            proof = artifact["online_admission"]["suppressed"][0]
            self.assertEqual(proof["solver_queries"], 3)
            self.assertEqual(proof["solver_time_us"], 1_200)
            self.assertEqual(
                artifact["online_admission"]["min_solver_time_us"], 1_000)

            # Evicting one failure leaves 800 us of recent cost and restores
            # the optional probe even though two failed queries remain.
            coordinator.observe(clean)
            readmitted = coordinator.publish(force=True)
            self.assertIsNotNone(readmitted)
            assert readmitted is not None
            self.assertEqual(readmitted.profile_count, 1)

    def test_truncated_checkpoint_is_rematerialized_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, window=8, min_observations=1,
                publish_interval_seconds=0)
            for site in range(1, 9):
                coordinator.observe(self.telemetry(
                    self.CONTEXT_A, site, observations=1, site=site))
            initial = coordinator.publish(force=True)
            self.assertIsNotNone(initial)
            assert initial is not None
            self.assertEqual(initial.profile_count, 8)

            # Force only the checkpoint through a small capacity. The runtime
            # and its artifact remain intact, but two oldest policy records do
            # not survive persistence.
            with mock.patch.object(
                    online_value_profile_module, "MAX_INPUT_BYTES", 2000):
                self.assertTrue(coordinator._checkpoint())
            state_path = Path(temporary) / "state.json"
            state = json.loads(state_path.read_text(encoding="ascii"))
            self.assertFalse(state["records_complete"])
            self.assertLess(len(state["records"]), 8)

            resumed = OnlineValueProfileCoordinator(
                temporary, window=8, min_observations=1,
                publish_interval_seconds=0)
            self.assertIsNotNone(resumed.current)
            self.assertTrue(resumed.dirty)

            # Old v1 checkpoints did not carry records_complete. A nonzero
            # truncation count is the conservative compatibility signal.
            state.pop("records_complete")
            state_path.write_text(
                json.dumps(state, sort_keys=True) + "\n", encoding="ascii")
            legacy = OnlineValueProfileCoordinator(
                temporary, window=8, min_observations=1,
                publish_interval_seconds=0)
            self.assertIsNotNone(legacy.current)
            self.assertTrue(legacy.dirty)
            replacement = legacy.publish(force=True)
            self.assertIsNotNone(replacement)
            assert replacement is not None
            self.assertEqual(replacement.profile_count, len(state["records"]))

    def test_runtime_and_checkpoint_generation_must_match_on_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, window=8, min_observations=1,
                publish_interval_seconds=0)
            coordinator.observe(self.telemetry(
                self.CONTEXT_A, 1, observations=1, site=11))
            first = coordinator.publish(force=True)
            self.assertIsNotNone(first)
            assert first is not None
            state_path = Path(temporary) / "state.json"
            first_state = state_path.read_bytes()

            coordinator.observe(self.telemetry(
                self.CONTEXT_A, 2, observations=1, site=12))
            second = coordinator.publish(force=True)
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(second.profile_count, 2)

            # Simulate a crash after the runtime generation was replaced but
            # before its matching policy checkpoint reached stable storage.
            state_path.write_bytes(first_state)
            mismatched = OnlineValueProfileCoordinator(
                temporary, window=8, min_observations=1,
                publish_interval_seconds=0)
            self.assertIsNotNone(mismatched.current)
            self.assertTrue(mismatched.dirty)
            reconciled = mismatched.publish(force=True)
            self.assertIsNotNone(reconciled)
            assert reconciled is not None
            self.assertEqual(reconciled.profile_count, 1)
            self.assertEqual(reconciled.artifact_sha256, first.artifact_sha256)

            # A runtime without any policy checkpoint is also unproven. The
            # conservative reconstruction from an empty record set withdraws
            # it instead of silently retaining the stale domain.
            state_path.unlink()
            stateless = OnlineValueProfileCoordinator(
                temporary, window=8, min_observations=1,
                publish_interval_seconds=0)
            self.assertIsNotNone(stateless.current)
            self.assertTrue(stateless.dirty)
            withdrawn = stateless.publish(force=True)
            self.assertIsNotNone(withdrawn)
            assert withdrawn is not None
            self.assertEqual(withdrawn.profile_count, 0)

    def test_policy_only_change_publishes_new_evidence_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, min_observations=1,
                publish_interval_seconds=0,
                feedback_min_solver_time_us=1_000)
            coordinator.observe(self.telemetry(
                self.CONTEXT_A, 1, observations=1))
            first = coordinator.publish(force=True)
            self.assertIsNotNone(first)
            assert first is not None

            changed = OnlineValueProfileCoordinator(
                temporary, min_observations=1,
                publish_interval_seconds=0,
                feedback_min_solver_time_us=2_000)
            self.assertTrue(changed.dirty)
            second = changed.publish(force=True)
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(second.profile_count, first.profile_count)
            self.assertNotEqual(second.version, first.version)
            artifact = json.loads((
                Path(temporary) / "generations"
                / f"{second.artifact_sha256}.json"
            ).read_text(encoding="ascii"))
            self.assertEqual(
                artifact["online_admission"]["min_solver_time_us"], 2_000)

            stable = OnlineValueProfileCoordinator(
                temporary, min_observations=1,
                publish_interval_seconds=0,
                feedback_min_solver_time_us=2_000)
            self.assertFalse(stable.dirty)
            self.assertEqual(stable.current.version, second.version)

    def test_records_without_a_sidecar_are_reconsidered_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, min_observations=2,
                publish_interval_seconds=0)
            coordinator.observe(self.telemetry(
                self.CONTEXT_A, 1, observations=1))
            self.assertIsNone(coordinator.publish(force=True))
            self.assertFalse((Path(temporary) / "current.runtime").exists())

            relaxed = OnlineValueProfileCoordinator(
                temporary, min_observations=1,
                publish_interval_seconds=0)
            self.assertTrue(relaxed.dirty)
            published = relaxed.publish(force=True)
            self.assertIsNotNone(published)
            assert published is not None
            self.assertEqual(published.profile_count, 1)

    def test_recovery_replays_records_not_only_generation_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, min_observations=1,
                publish_interval_seconds=0)
            coordinator.observe(self.telemetry(
                self.CONTEXT_A, 1, observations=1))
            original = coordinator.publish(force=True)
            self.assertIsNotNone(original)
            assert original is not None

            # Preserve the matching artifact label but replace the recovered
            # record with a different, individually valid domain.
            state_path = Path(temporary) / "state.json"
            state = json.loads(state_path.read_text(encoding="ascii"))
            state["records"][0] = self.telemetry(
                self.CONTEXT_A, 2, observations=1)
            state_path.write_text(
                json.dumps(state, sort_keys=True) + "\n", encoding="ascii")

            resumed = OnlineValueProfileCoordinator(
                temporary, min_observations=1,
                publish_interval_seconds=0)
            self.assertIsNotNone(resumed.current)
            self.assertTrue(resumed.dirty)
            self.assertEqual(resumed.recovery_replays, 1)
            self.assertEqual(resumed.recovery_replay_mismatches, 1)
            replacement = resumed.publish(force=True)
            self.assertIsNotNone(replacement)
            assert replacement is not None
            self.assertIn(
                f"profile {self.CONTEXT_A} 11 8 1 2\n".encode("ascii"),
                replacement.content,
            )

    def test_checkpoint_failure_keeps_publication_dirty_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = OnlineValueProfileCoordinator(
                temporary, min_observations=1,
                publish_interval_seconds=0)
            coordinator.observe(self.telemetry(
                self.CONTEXT_A, 1, observations=1))
            original_write = online_value_profile_module._atomic_write

            def fail_state(path, content):
                if Path(path).name == "state.json":
                    raise OSError("injected checkpoint failure")
                original_write(path, content)

            with mock.patch.object(
                    online_value_profile_module, "_atomic_write",
                    side_effect=fail_state):
                published = coordinator.publish(force=True)
            self.assertIsNotNone(published)
            self.assertTrue(coordinator.dirty)
            self.assertEqual(coordinator.checkpoint_failures, 1)

            # The runtime is already current, so the retry is a semantic no-op
            # that must nevertheless persist the failed checkpoint counters.
            self.assertIsNone(coordinator.publish(force=True))
            self.assertFalse(coordinator.dirty)
            state = json.loads((
                Path(temporary) / "state.json"
            ).read_text(encoding="ascii"))
            self.assertEqual(state["checkpoint_failures"], 1)


if __name__ == "__main__":
    unittest.main()
