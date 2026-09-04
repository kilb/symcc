# RUN: python3 %s

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from self_config import (  # noqa: E402
    SelfConfiguringPolicy,
    discover_parameter_registry,
    discover_parameter_specs,
    native_parameter_provider_payload,
    sanitize_parameter_overrides,
)


PROFILES = [
    {"SYMCC_EXECUTOR_CLASS": "exact"},
    {"SYMCC_EXECUTOR_CLASS": "tailored", "SYMCC_FAST_SOLVE": "1"},
    {
        "SYMCC_EXECUTOR_CLASS": "sampling",
        "SYMCC_POLY_SAMPLES": "4",
    },
]


class SelfConfiguringPolicyTests(unittest.TestCase):
    def test_builtin_provider_matches_the_legacy_schema_snapshot(self):
        snapshot = json.loads((ROOT / "util" / "self_config_schema.json").read_text())
        provider = native_parameter_provider_payload()
        self.assertEqual(provider["schema"], "symcc-parameter-provider-v1")
        self.assertEqual(provider["parameters"], snapshot["parameters"])

    def test_native_provider_merge_is_atomic_and_records_provenance(self):
        compatible = {
            "schema": "symcc-parameter-provider-v1",
            "provider": "test-solver",
            "parameters": {
                "SYMCC_FAST_SOLVE": {
                    "values": ["0", "1"],
                    "active_when": {
                        "SYMCC_EXECUTOR_CLASS": ["tailored", "sampling"],
                    },
                },
                "SYMCC_TEST_NATIVE_BUDGET": {
                    "values": ["1", "2", "4"],
                    "numeric": True,
                    "min": 1,
                    "max": 8,
                    "active_when": {"SYMCC_FAST_SOLVE": ["1"]},
                },
            },
        }
        registry = discover_parameter_registry(
            PROFILES, provider_commands=[], provider_payloads=[compatible]
        )
        self.assertIn("SYMCC_TEST_NATIVE_BUDGET", registry.specs)
        self.assertEqual(
            [item.name for item in registry.providers],
            ["symcc-coordinator", "test-solver"],
        )
        self.assertEqual(registry.conflicts, [])

        conflicting = json.loads(json.dumps(compatible))
        conflicting["provider"] = "conflicting-solver"
        conflicting["parameters"]["SYMCC_FAST_SOLVE"]["values"] = ["0"]
        conflicting["parameters"]["SYMCC_TEST_REJECTED"] = {
            "values": ["1"],
        }
        rejected = discover_parameter_registry(
            PROFILES, provider_commands=[], provider_payloads=[conflicting]
        )
        self.assertNotIn("SYMCC_TEST_REJECTED", rejected.specs)
        self.assertEqual(len(rejected.conflicts), 1)
        self.assertIn("SYMCC_FAST_SOLVE", rejected.conflicts[0])

    def test_provider_protocol_rejects_malformed_contracts(self):
        malformed = {
            "schema": "symcc-parameter-provider-v1",
            "provider": "bad-provider",
            "parameters": {
                "SYMCC_TEST_BAD": {
                    "values": ["nan"],
                    "numeric": True,
                },
            },
        }
        registry = discover_parameter_registry(
            PROFILES, provider_commands=[], provider_payloads=[malformed]
        )
        self.assertNotIn("SYMCC_TEST_BAD", registry.specs)
        self.assertEqual(len(registry.errors), 1)
        self.assertIn("numeric value", registry.errors[0])

    def test_process_lifecycle_parameters_are_not_sampled_per_task(self):
        service_provider = {
            "schema": "symcc-parameter-provider-v1",
            "provider": "test-service",
            "scope": "query-service",
            "parameters": {
                "SYMCC_TEST_SERVICE_POOL": {
                    "values": ["1", "2", "4"],
                    "numeric": True,
                    "min": 1,
                    "max": 8,
                },
            },
        }
        policy = SelfConfiguringPolicy(
            None,
            PROFILES,
            provider_commands=[],
            provider_payloads=[service_provider],
            seed=1,
        )
        self.assertEqual(policy.registry_parameter_count, 45)
        self.assertEqual(len(policy.parameters), 28)
        self.assertNotIn("SYMCC_TACE", policy.parameters)
        self.assertNotIn("SYMCC_SELECTIVE_QUERY", policy.parameters)
        self.assertNotIn("SYMCC_TEST_SERVICE_POOL", policy.parameters)

    def test_provider_command_has_output_and_time_bounds(self):
        oversized = discover_parameter_registry(
            PROFILES,
            provider_commands=[
                [sys.executable, "-c", "print('x' * (1024 * 1024 + 1))"]
            ],
        )
        self.assertEqual(len(oversized.providers), 1)
        self.assertEqual(len(oversized.errors), 1)
        self.assertIn("exceeds 1 MiB", oversized.errors[0])

        previous = os.environ.get("SYMCC_SELF_CONFIG_PROVIDER_TIMEOUT")
        os.environ["SYMCC_SELF_CONFIG_PROVIDER_TIMEOUT"] = "0.05"
        try:
            timed_out = discover_parameter_registry(
                PROFILES,
                provider_commands=[
                    [sys.executable, "-c", "import time; time.sleep(1)"]
                ],
            )
        finally:
            if previous is None:
                os.environ.pop("SYMCC_SELF_CONFIG_PROVIDER_TIMEOUT", None)
            else:
                os.environ["SYMCC_SELF_CONFIG_PROVIDER_TIMEOUT"] = previous
        self.assertEqual(len(timed_out.providers), 1)
        self.assertEqual(len(timed_out.errors), 1)
        self.assertIn("timed out", timed_out.errors[0])

    def test_discovers_profile_and_custom_parameters(self):
        custom = json.dumps(
            {
                "parameters": {
                    "SYMCC_TEST_BUDGET": {
                        "values": [1, 2, 4],
                        "numeric": True,
                        "min": 1,
                        "max": 16,
                    },
                    "not-an-env-name": [1, 2],
                }
            }
        )
        specs = discover_parameter_specs(PROFILES, custom)
        self.assertIn("SYMCC_FAST_SOLVE", specs)
        self.assertIn("SYMCC_TEST_BUDGET", specs)
        self.assertNotIn("not-an-env-name", specs)
        self.assertIn("4", specs["SYMCC_POLY_SAMPLES"].values)
        self.assertEqual(
            specs["SYMCC_POLY_SAMPLES"].active_when,
            {"SYMCC_POLY_CACHE": ("1",)},
        )

    def test_condition_graph_rejects_cycles_and_prunes_inactive_values(self):
        custom = json.dumps(
            {
                "SYMCC_TEST_A": {
                    "values": [0, 1],
                    "active_when": {"SYMCC_TEST_B": [1]},
                },
                "SYMCC_TEST_B": {
                    "values": [0, 1],
                    "active_when": {"SYMCC_TEST_A": [1]},
                },
            }
        )
        specs = discover_parameter_specs(PROFILES, custom)
        self.assertNotIn("SYMCC_TEST_A", specs)
        self.assertNotIn("SYMCC_TEST_B", specs)

        policy = SelfConfiguringPolicy(None, PROFILES, seed=1)
        guarded = policy._apply_guards(
            {
                "SYMCC_EXECUTOR_CLASS": "sampling",
                "SYMCC_POLY_CACHE": "1",
                "SYMCC_POLY_CROSS_PREFIX": "0",
                "SYMCC_POLY_EXACT_PROJECTION": "1",
                "SYMCC_POLY_EXACT_PROJECTION_TIMEOUT": "50",
            }
        )
        self.assertNotIn("SYMCC_POLY_EXACT_PROJECTION", guarded)
        self.assertNotIn("SYMCC_POLY_EXACT_PROJECTION_TIMEOUT", guarded)

    def test_assignment_token_attributes_reward_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "self-config.json")
            policy = SelfConfiguringPolicy(state, PROFILES, max_parameters=2, seed=7)
            assignment = policy.select(PROFILES[1])
            self.assertTrue(assignment.token.startswith("pcfg-"))
            self.assertTrue(set(assignment.overrides) <= policy.parameter_names)
            self.assertTrue(policy.observe(assignment.token, reward=0.8, elapsed=0.2))
            self.assertFalse(policy.observe(assignment.token, reward=0.8, elapsed=0.2))
            policy.save()

            restored = SelfConfiguringPolicy(state, PROFILES, max_parameters=2, seed=7)
            self.assertEqual(restored.observations, 1)
            self.assertGreaterEqual(len(restored.configurations), 1)

    def test_abandon_removes_undispatched_parameter_assignment(self):
        policy = SelfConfiguringPolicy(None, PROFILES, seed=7)
        assignment = policy.select(PROFILES[1])
        self.assertIn(assignment.token, policy.pending)
        self.assertTrue(policy.abandon(assignment.token))
        self.assertNotIn(assignment.token, policy.pending)
        self.assertFalse(policy.observe(assignment.token, reward=1.0, elapsed=0.1))
        self.assertEqual(policy.observations, 0)

    def test_executor_guards_sampling_and_exact_profiles(self):
        policy = SelfConfiguringPolicy(None, PROFILES, seed=1)
        exact = policy._apply_guards(
            {
                "SYMCC_EXECUTOR_CLASS": "exact",
                "SYMCC_FAST_SOLVE": "1",
                "SYMCC_POLY_SAMPLES": "8",
            }
        )
        self.assertEqual(exact["SYMCC_FAST_SOLVE"], "0")
        self.assertNotIn("SYMCC_POLY_SAMPLES", exact)

        sampling = policy._apply_guards(
            {
                "SYMCC_EXECUTOR_CLASS": "sampling",
                "SYMCC_POLY_SAMPLES": "8",
            }
        )
        self.assertEqual(sampling["SYMCC_POLY_CACHE"], "1")
        self.assertEqual(sampling["SYMCC_POLY_SAMPLES"], "8")

    def test_numeric_space_expands_after_productive_observations(self):
        custom = json.dumps(
            {
                "SYMCC_TEST_BUDGET": {
                    "values": [4],
                    "numeric": True,
                    "min": 1,
                    "max": 32,
                }
            }
        )
        policy = SelfConfiguringPolicy(None, PROFILES, custom_space=custom, seed=3)
        spec = policy.parameters["SYMCC_TEST_BUDGET"]
        posterior = spec.posteriors["4"]
        for _ in range(4):
            posterior.update(1.0, 0.1, False)
        policy.expanded_values += spec.expand_around("4")
        self.assertIn("2", spec.values)
        self.assertIn("8", spec.values)

    def test_context_and_interaction_posteriors_are_isolated_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "self-config.json")
            policy = SelfConfiguringPolicy(state, PROFILES, max_parameters=2, seed=11)
            for spec in policy.parameters.values():
                for posterior in spec.posteriors.values():
                    posterior.pulls = 1
            assignment = policy.select(
                PROFILES[2],
                context={
                    "phase": "warm",
                    "target_branch": 17,
                    "task_region": 9,
                    "input_bytes": 4096,
                    "worker": 99,
                },
            )
            self.assertEqual(
                assignment.context_key,
                "phase=warm|targeted=1|structured=1|input=medium",
            )
            self.assertTrue(policy.observe(assignment.token, reward=1.0, elapsed=0.1))
            self.assertIn(assignment.context_key, policy.context_posteriors)
            self.assertGreaterEqual(len(policy.interactions), 1)
            policy.save()

            restored = SelfConfiguringPolicy(state, PROFILES, max_parameters=2, seed=11)
            self.assertIn(assignment.context_key, restored.context_posteriors)
            self.assertEqual(len(restored.interactions), len(policy.interactions))

    def test_transfer_prior_is_read_only_and_target_statistics_start_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            prior_path = str(Path(tmp) / "prior.json")
            prior = SelfConfiguringPolicy(
                prior_path, PROFILES, max_parameters=1, seed=5
            )
            spec = prior.parameters["SYMCC_BACKSOLVER"]
            spec.posteriors["1"].update(1.0, 0.1, False)
            prior.save()

            target = SelfConfiguringPolicy(
                None, PROFILES, prior_path=prior_path, seed=5
            )
            self.assertIn("SYMCC_BACKSOLVER", target.prior_parameters)
            self.assertEqual(
                target.parameters["SYMCC_BACKSOLVER"].posteriors["1"].pulls,
                0,
            )
            self.assertEqual(target.observations, 0)

    def test_registry_provenance_is_persisted_without_importing_old_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "self-config.json")
            provider = {
                "schema": "symcc-parameter-provider-v1",
                "provider": "ephemeral-provider",
                "parameters": {
                    "SYMCC_TEST_EPHEMERAL": {"values": ["0", "1"]},
                },
            }
            policy = SelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                provider_payloads=[provider],
                seed=3,
            )
            policy.observations = 7
            policy.save()
            saved = json.loads(Path(state).read_text())
            self.assertEqual(saved["schema"], 3)
            self.assertEqual(
                saved["registry_provenance_hash"],
                policy.registry_provenance_hash,
            )
            self.assertEqual(len(saved["registry_provenance"]["providers"]), 2)

            restored = SelfConfiguringPolicy(
                state, PROFILES, provider_commands=[], seed=3
            )
            self.assertNotIn("SYMCC_TEST_EPHEMERAL", restored.parameters)
            self.assertEqual(restored.observations, 0)
            self.assertEqual(restored.snapshot()["registry_providers"], 1)

    def test_parameter_payload_rejects_non_symcc_environment_keys(self):
        self.assertEqual(
            sanitize_parameter_overrides(
                {
                    "SYMCC_FAST_SOLVE": 1,
                    "PATH": "/tmp/override",
                    "SYMCC_BAD-NAME": "1",
                }
            ),
            {"SYMCC_FAST_SOLVE": "1"},
        )


if __name__ == "__main__":
    unittest.main()
