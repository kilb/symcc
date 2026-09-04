# RUN: env PYTHONPATH=%S/../util python3 %s

import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from parasuit_value_policy import (  # noqa: E402
    AdaptiveSelfConfiguringPolicy,
    ValueObservation,
    analyze_value_space,
    native_value_policy_provider_payload,
)
from self_config import ParameterSpec  # noqa: E402
import parasuit_value_policy  # noqa: E402


PROFILES = [
    {"SYMCC_EXECUTOR_CLASS": "exact"},
    {"SYMCC_EXECUTOR_CLASS": "sampling", "SYMCC_POLY_CACHE": "1"},
]


def clustered_observations(context: str = "ctx") -> list[ValueObservation]:
    samples = [
        ("1", 0.05),
        ("1", 0.10),
        ("2", 0.10),
        ("2", 0.15),
        ("9", 0.90),
        ("9", 0.95),
        ("10", 0.90),
        ("10", 1.00),
    ]
    return [
        ValueObservation(value, reward, 1.0, False, context, sequence)
        for sequence, (value, reward) in enumerate(samples, 1)
    ]


class ParaSuitValuePolicyTests(unittest.TestCase):
    def test_native_provider_is_campaign_scoped_and_not_sampled_per_task(self):
        provider = native_value_policy_provider_payload()
        self.assertEqual(provider["schema"], "symcc-parameter-provider-v1")
        self.assertEqual(provider["scope"], "coordinator-campaign")
        self.assertEqual(len(provider["parameters"]), 5)
        self.assertEqual(
            provider["parameters"]["SYMCC_SELF_CONFIG_SILHOUETTE_THRESHOLD"][
                "active_when"
            ],
            {
                "SYMCC_SELF_CONFIG_VALUE_POLICY": ["hybrid", "silhouette"],
            },
        )

        policy = AdaptiveSelfConfiguringPolicy(
            None, PROFILES, provider_commands=[], seed=1
        )
        self.assertEqual(policy.registry_parameter_count, 49)
        self.assertEqual(len(policy.parameters), 28)
        self.assertEqual(policy.snapshot()["registry_providers"], 2)
        self.assertNotIn("SYMCC_SELF_CONFIG_VALUE_POLICY", policy.parameter_names)

    def test_two_separated_value_utility_regions_pass_silhouette_gate(self):
        spec = ParameterSpec(
            "SYMCC_TEST_NUMERIC",
            ["1", "2", "9", "10"],
            numeric=True,
            minimum=1,
            maximum=20,
        )
        analysis = analyze_value_space(
            spec,
            clustered_observations(),
            threshold=0.7,
            min_samples=4,
            window=64,
        )
        self.assertEqual(analysis.mode, "exploit")
        self.assertEqual(analysis.cluster_count, 2)
        self.assertGreater(analysis.silhouette, 0.9)
        self.assertEqual(analysis.labels[:4], (0, 0, 0, 0))
        self.assertEqual(analysis.labels[4:], (1, 1, 1, 1))

        rejected = analyze_value_space(
            spec,
            clustered_observations(),
            threshold=0.95,
            min_samples=4,
        )
        self.assertEqual(rejected.mode, "explore")
        self.assertEqual(rejected.reason, "silhouette-below-threshold")

    def test_degenerate_and_incomplete_samples_fail_closed_to_exploration(self):
        spec = ParameterSpec(
            "SYMCC_TEST_NUMERIC", ["4"], numeric=True, minimum=1, maximum=16
        )
        incomplete = analyze_value_space(
            spec, clustered_observations()[:3], min_samples=4
        )
        self.assertEqual(incomplete.reason, "insufficient-samples")

        constant = [
            ValueObservation("4", reward, 1.0, False, "ctx", index)
            for index, reward in enumerate((0.0, 0.2, 0.8, 1.0), 1)
        ]
        degenerate = analyze_value_space(spec, constant, min_samples=4)
        self.assertEqual(degenerate.mode, "explore")
        self.assertEqual(degenerate.reason, "constant-value")

        yielded = 0

        def long_stream():
            nonlocal yielded
            for index in range(10000):
                yielded += 1
                yield ValueObservation(
                    str(1 + index % 10), index % 2, 1.0, False, "ctx", index
                )

        bounded = analyze_value_space(spec, long_stream(), min_samples=4, window=8)
        self.assertEqual(yielded, 10000)
        self.assertEqual(bounded.sample_count, 8)

        malformed = analyze_value_space(
            spec,
            [
                ValueObservation("4", float("nan"), 1.0, False, "ctx", 1),
                ValueObservation("4", 1.0, float("inf"), False, "ctx", 2),
            ],
            min_samples="invalid",
            window="invalid",
        )
        self.assertEqual(malformed.mode, "explore")
        self.assertEqual(malformed.reason, "insufficient-samples")
        self.assertEqual(malformed.sample_count, 0)

    def test_stable_cluster_exploit_constructs_a_bounded_integer_value(self):
        schema = json.dumps(
            {
                "parameters": {
                    "SYMCC_TEST_NUMERIC": {
                        "values": [1, 2, 9, 10],
                        "numeric": True,
                        "min": 1,
                        "max": 20,
                    }
                }
            }
        )
        policy = AdaptiveSelfConfiguringPolicy(
            None,
            PROFILES,
            schema_space=schema,
            provider_commands=[],
            value_policy="hybrid",
            silhouette_threshold=0.7,
            min_cluster_samples=4,
            exploration_reserve=0,
            seed=7,
        )
        spec = policy.parameters["SYMCC_TEST_NUMERIC"]
        policy.value_history[spec.name] = clustered_observations()
        selected = policy._select_value(spec, "ctx", {})
        self.assertEqual(selected, "10")
        self.assertGreaterEqual(int(selected), 1)
        self.assertLessEqual(int(selected), 20)
        self.assertEqual(policy.value_policy_counts["exploit"], 1)
        self.assertEqual(
            policy.last_value_decisions[spec.name]["reason"],
            "silhouette-admitted",
        )

    def test_observation_attribution_is_bounded_and_ignores_replay(self):
        policy = AdaptiveSelfConfiguringPolicy(
            None, PROFILES, provider_commands=[], seed=3
        )
        name = "SYMCC_BACKSOLVER"
        policy.pending["pcfg-test"] = {
            "profile": {name: "1"},
            "context": "phase=warm|targeted=1|structured=0|input=small",
            "choices": {name: "1"},
        }
        self.assertTrue(
            policy.observe("pcfg-test", reward=float("nan"), elapsed=float("inf"))
        )
        self.assertFalse(policy.observe("pcfg-test", reward=1.0, elapsed=1.0))
        observation = policy.value_history[name][0]
        self.assertEqual(observation.reward, 0.0)
        self.assertEqual(observation.elapsed, 1.0)
        posterior = policy.parameters[name].posteriors["1"]
        self.assertTrue(math.isfinite(posterior.reward_sum))
        self.assertTrue(math.isfinite(posterior.cost_sum))
        self.assertEqual(policy.observations, 1)

    def test_persistence_binds_program_registry_and_base_state_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = str(Path(temporary) / "self-config.json")
            first = AdaptiveSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a --mode test",
                seed=5,
            )
            name = "SYMCC_BACKSOLVER"
            first.pending["pcfg-1"] = {
                "profile": {name: "1"},
                "context": "ctx",
                "choices": {name: "1"},
            }
            self.assertTrue(first.observe("pcfg-1", reward=0.8, elapsed=0.2))
            first.save()

            sidecar_path = Path(f"{state}.value-space.json")
            sidecar = json.loads(sidecar_path.read_text())
            self.assertEqual(sidecar["schema"], "symcc-parasuit-value-space-v1")
            self.assertEqual(
                sidecar["base_state_sha256"],
                hashlib.sha256(Path(state).read_bytes()).hexdigest(),
            )
            self.assertEqual(sidecar["base_sequence"], first.sequence)
            self.assertEqual(sidecar["base_observations"], first.observations)

            restored = AdaptiveSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a --mode test",
                seed=5,
            )
            self.assertEqual(restored.observations, 1)
            self.assertEqual(len(restored.value_history[name]), 1)
            self.assertEqual(restored.value_policy_counts["state_rejections"], 0)

            other_program = AdaptiveSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-b",
                seed=5,
            )
            self.assertEqual(other_program.observations, 0)
            self.assertEqual(other_program.value_history, {})
            self.assertEqual(other_program.value_policy_counts["state_rejections"], 1)

            changed_policy = AdaptiveSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a --mode test",
                silhouette_threshold=0.85,
                seed=5,
            )
            self.assertEqual(changed_policy.observations, 0)
            self.assertEqual(changed_policy.value_history, {})
            self.assertEqual(changed_policy.value_policy_counts["state_rejections"], 1)

            malformed_sidecar = json.loads(json.dumps(sidecar))
            malformed_sidecar["history"][name][0]["reward"] = "not-finite"
            sidecar_path.write_text(json.dumps(malformed_sidecar))
            malformed = AdaptiveSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a --mode test",
                seed=5,
            )
            self.assertEqual(malformed.observations, 0)
            self.assertEqual(malformed.value_history, {})
            self.assertEqual(malformed.value_policy_counts["state_rejections"], 1)

            sidecar_path.write_text(json.dumps(sidecar))
            with Path(state).open("a", encoding="utf-8") as stream:
                stream.write(" ")
            torn = AdaptiveSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a --mode test",
                seed=5,
            )
            self.assertEqual(torn.observations, 0)
            self.assertEqual(torn.value_history, {})
            self.assertEqual(torn.value_policy_counts["state_rejections"], 1)

            first.save()
            sidecar_path.unlink()
            sidecar_path.symlink_to(Path(state))
            symlinked = AdaptiveSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a --mode test",
                seed=5,
            )
            self.assertEqual(symlinked.observations, 0)
            self.assertEqual(symlinked.value_history, {})
            self.assertEqual(symlinked.value_policy_counts["state_rejections"], 1)

    def test_verified_base_snapshot_is_not_reopened_during_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = str(Path(temporary) / "self-config.json")
            first = AdaptiveSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a",
                seed=5,
            )
            name = "SYMCC_BACKSOLVER"
            first.pending["pcfg-1"] = {
                "profile": {name: "1"},
                "context": "ctx",
                "choices": {name: "1"},
            }
            self.assertTrue(first.observe("pcfg-1", reward=0.8, elapsed=0.2))
            first.save()

            original = parasuit_value_policy._bounded_json_state

            def replace_after_read(path):
                snapshot = original(path)
                if path == state and snapshot is not None:
                    replacement = dict(snapshot[0])
                    replacement["observations"] = 999
                    Path(path).write_text(json.dumps(replacement), encoding="utf-8")
                return snapshot

            with mock.patch(
                "parasuit_value_policy._bounded_json_state",
                side_effect=replace_after_read,
            ):
                restored = AdaptiveSelfConfiguringPolicy(
                    state,
                    PROFILES,
                    provider_commands=[],
                    program_key="target-a",
                    seed=5,
                )
            self.assertEqual(restored.observations, 1)
            self.assertEqual(len(restored.value_history[name]), 1)

    def test_invalid_control_values_are_totalized_to_bounded_defaults(self):
        policy = AdaptiveSelfConfiguringPolicy(
            None,
            PROFILES,
            provider_commands=[],
            value_policy="invalid",
            silhouette_threshold="nan",
            cluster_window=1 << 20,
            min_cluster_samples=-1,
            exploration_reserve=9,
        )
        self.assertEqual(policy.value_policy, "hybrid")
        self.assertEqual(policy.silhouette_threshold, 0.7)
        self.assertEqual(policy.cluster_window, 256)
        self.assertEqual(policy.min_cluster_samples, 4)
        self.assertEqual(policy.exploration_reserve, 0.5)

        negative = ParameterSpec(
            "SYMCC_TEST_NEGATIVE",
            ["-10", "-4"],
            numeric=True,
            minimum=-20,
            maximum=-1,
        )
        explored = policy._sample_explore(
            negative,
            [ValueObservation("-10", 0.8, 1.0, False, "ctx", 1)],
        )
        self.assertGreaterEqual(int(explored), -20)
        self.assertLessEqual(int(explored), -1)

    def test_cli_contract_and_mpi_master_use_the_adaptive_policy(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "util" / "parasuit_value_policy.py"),
                "--print-parameters",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout), native_value_policy_provider_payload()
        )
        master = (ROOT / "util" / "mpi_fuzzing_helper.py").read_text()
        self.assertIn(
            "from parasuit_parameter_policy import (", master
        )
        self.assertIn("ParaSuitSelfConfiguringPolicy(", master)
        self.assertIn("program_key=_self_config_program_key(args.target)", master)


if __name__ == "__main__":
    unittest.main()
