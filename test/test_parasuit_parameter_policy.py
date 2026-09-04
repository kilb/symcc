# RUN: env PYTHONPATH=%S/../util python3 %s

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

import parasuit_parameter_policy  # noqa: E402
from parasuit_parameter_policy import (  # noqa: E402
    CoverageObservation,
    ParaSuitSelfConfiguringPolicy,
    analyze_parameter_space,
    branch_outcome_features,
    native_parameter_selection_provider_payload,
)


PROFILES = [
    {"SYMCC_EXECUTOR_CLASS": "exact"},
    {"SYMCC_EXECUTOR_CLASS": "sampling", "SYMCC_POLY_CACHE": "1"},
]
A = "SYMCC_BACKSOLVER"
B = "SYMCC_PREFIX_CONTEXT_CACHE"


def paper_oracle() -> list[CoverageObservation]:
    return [
        CoverageObservation((A,), ("11:0", "12:1"), True, "ctx", 1),
        CoverageObservation((B,), ("13:0",), True, "ctx", 2),
        CoverageObservation((A, B), ("11:0",), False, "ctx", 3),
        CoverageObservation((B,), ("13:0", "14:1"), False, "ctx", 4),
    ]


class ParaSuitParameterPolicyTests(unittest.TestCase):
    def test_provider_is_campaign_scoped_and_registry_controls_are_not_task_arms(self):
        provider = native_parameter_selection_provider_payload()
        self.assertEqual(provider["schema"], "symcc-parameter-provider-v1")
        self.assertEqual(provider["scope"], "coordinator-campaign")
        self.assertEqual(len(provider["parameters"]), 4)
        self.assertEqual(
            provider["parameters"]["SYMCC_SELF_CONFIG_MAX_COVERAGE_FEATURES"]["max"],
            128,
        )

        policy = ParaSuitSelfConfiguringPolicy(
            None, PROFILES, provider_commands=[], seed=1
        )
        self.assertEqual(policy.registry_parameter_count, 53)
        self.assertEqual(len(policy.parameters), 28)
        self.assertEqual(policy.snapshot()["registry_providers"], 3)
        self.assertNotIn("SYMCC_SELF_CONFIG_PARAMETER_POLICY", policy.parameter_names)

    def test_branch_trace_normalization_is_stable_strict_and_bounded(self):
        trace = [(1, 2, 3, site, site % 2, 6) for site in range(1, 30)] + [
            (1, 2, 3, 1, 1, 6),
            (1, 2, 3, 0, 1, 6),
            (1, 2, 3, True, 1, 6),
            (1, 2, 3, 9, True, 6),
            (1, 2, 3),
            "invalid",
        ]
        features = branch_outcome_features(trace, maximum=1)
        self.assertEqual(len(features), 16)
        self.assertEqual(features, tuple(sorted(set(features))))
        self.assertTrue(all(item.endswith((":0", ":1")) for item in features))
        self.assertNotIn("0:1", features)

    def test_paper_rarity_baseline_and_synergy_penalty_have_exact_oracle(self):
        analysis = analyze_parameter_space((A, B), paper_oracle())
        self.assertEqual(
            analysis.feature_frequency,
            {"11:0": 2, "12:1": 1, "13:0": 2, "14:1": 1},
        )
        self.assertEqual(analysis.baseline, {A: 1.5, B: 0.5})
        self.assertEqual(analysis.combined, {A: 0.0, B: 1.0})
        self.assertEqual(analysis.normalized, {A: 0.0, B: 1.0})
        self.assertEqual(analysis.penalized_samples, {A: 1, B: 0})
        self.assertTrue(analysis.ready)

    def test_inverse_frequency_oracle_detects_upstream_branch_count_substitution(self):
        observations = [
            CoverageObservation((A,), ("21:0",), True, "ctx", 1),
            CoverageObservation((B,), ("22:1",), True, "ctx", 2),
            CoverageObservation((A,), ("21:0",), False, "ctx", 3),
            CoverageObservation((A,), ("21:0",), False, "ctx", 4),
            CoverageObservation((A,), ("21:0",), False, "ctx", 5),
        ]
        analysis = analyze_parameter_space((A, B), observations)
        self.assertEqual(analysis.baseline[A], 0.25)
        self.assertEqual(analysis.baseline[B], 1.0)
        self.assertGreater(analysis.normalized[B], analysis.normalized[A])

    def test_parasuit_selection_uses_branch_rarity_when_evidence_is_ready(self):
        policy = ParaSuitSelfConfiguringPolicy(
            None,
            PROFILES,
            provider_commands=[],
            parameter_policy="parasuit",
            max_parameters=1,
            seed=7,
        )
        policy.coverage_history = paper_oracle()
        selected = policy._select_parameters({}, "ctx")
        self.assertEqual([spec.name for spec in selected], [B])
        self.assertEqual(policy.parameter_policy_counts["parasuit"], 1)
        self.assertEqual(policy.last_parameter_analysis["normalized"][B], 1.0)

    def test_extraction_isolates_one_value_with_only_transitive_activation_guards(self):
        policy = ParaSuitSelfConfiguringPolicy(
            None, PROFILES, provider_commands=[], seed=3
        )
        for spec in policy.parameters.values():
            for posterior in spec.posteriors.values():
                posterior.pulls = 1
        target = "SYMCC_POLY_RENAME_EXACT_PROBES"
        policy.parameters[target].posteriors["2"].pulls = 0

        assignment = policy.select(
            {A: "1", B: "1"}, context={"phase": "warm", "input_bytes": 64}
        )
        pending = policy.pending[assignment.token]
        self.assertEqual(pending["choices"], {target: "2"})
        self.assertEqual(pending["selection_phase"], "extraction")
        self.assertEqual(assignment.overrides[target], "2")
        self.assertEqual(assignment.overrides["SYMCC_EXECUTOR_CLASS"], "sampling")
        self.assertNotIn(A, assignment.overrides)
        self.assertNotIn(B, assignment.overrides)

        self.assertTrue(
            policy.observe(
                assignment.token,
                reward=0.8,
                elapsed=0.2,
                coverage_features=("17:1", "invalid", "0:1", "17:1"),
            )
        )
        self.assertFalse(
            policy.observe(
                assignment.token,
                reward=1.0,
                elapsed=0.1,
                coverage_features=("18:0",),
            )
        )
        self.assertEqual(policy.coverage_history[0].features, ("17:1",))
        self.assertTrue(policy.coverage_history[0].extraction)

    def test_invalid_controls_totalize_and_missing_coverage_is_not_fabricated(self):
        policy = ParaSuitSelfConfiguringPolicy(
            None,
            PROFILES,
            provider_commands=[],
            parameter_policy="invalid",
            selection_window=-1,
            max_coverage_features=1 << 20,
            parasuit_weight="nan",
        )
        self.assertEqual(policy.parameter_policy, "hybrid")
        self.assertEqual(policy.selection_window, 16)
        self.assertEqual(policy.max_coverage_features, 128)
        self.assertEqual(policy.parasuit_weight, 0.6)
        assignment = policy.select()
        self.assertTrue(policy.observe(assignment.token, reward=0.1, elapsed=0.1))
        self.assertEqual(policy.coverage_history, [])
        self.assertEqual(policy.parameter_policy_counts["missing_coverage"], 1)

    def test_three_file_state_replay_and_fail_closed_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = str(Path(temporary) / "self-config.json")
            first = ParaSuitSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a --mode test",
                seed=5,
            )
            assignment = first.select()
            self.assertTrue(
                first.observe(
                    assignment.token,
                    reward=0.8,
                    elapsed=0.2,
                    coverage_features=("101:1", "102:0"),
                )
            )
            first.save()

            value_path = Path(f"{state}.value-space.json")
            selection_path = Path(f"{state}.parameter-selection.json")
            selection = json.loads(selection_path.read_text())
            self.assertEqual(
                selection["base_state_sha256"],
                hashlib.sha256(Path(state).read_bytes()).hexdigest(),
            )
            self.assertEqual(
                selection["value_state_sha256"],
                hashlib.sha256(value_path.read_bytes()).hexdigest(),
            )

            restored = ParaSuitSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a --mode test",
                seed=5,
            )
            self.assertEqual(restored.observations, 1)
            self.assertEqual(len(restored.coverage_history), 1)
            self.assertEqual(restored.parameter_policy_counts["state_rejections"], 0)

            torn = dict(selection)
            torn["base_state_sha256"] = "0" * 64
            selection_path.write_text(json.dumps(torn), encoding="utf-8")
            rejected = ParaSuitSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a --mode test",
                seed=5,
            )
            self.assertEqual(rejected.observations, 0)
            self.assertEqual(rejected.value_history, {})
            self.assertEqual(rejected.coverage_history, [])
            self.assertEqual(rejected.parameter_policy_counts["state_rejections"], 1)

            first.save()
            selection_path.unlink()
            missing = ParaSuitSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a --mode test",
            )
            self.assertEqual(missing.observations, 0)
            self.assertEqual(missing.parameter_policy_counts["state_rejections"], 1)

            first.save()
            selection_path.unlink()
            selection_path.symlink_to(Path(state))
            symlinked = ParaSuitSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a --mode test",
            )
            self.assertEqual(symlinked.observations, 0)
            self.assertEqual(symlinked.parameter_policy_counts["state_rejections"], 1)

    def test_selection_import_does_not_reopen_the_authenticated_state_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = str(Path(temporary) / "self-config.json")
            first = ParaSuitSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-a",
                seed=9,
            )
            assignment = first.select()
            first.observe(
                assignment.token,
                reward=0.7,
                elapsed=0.1,
                coverage_features=("77:1",),
            )
            first.save()
            selection_path = f"{state}.parameter-selection.json"
            original = parasuit_parameter_policy._bounded_json_state
            calls: list[str] = []

            def replace_after_selection_read(path):
                calls.append(path)
                snapshot = original(path)
                if path == selection_path and snapshot is not None:
                    Path(state).write_text("{}\n", encoding="utf-8")
                    Path(f"{state}.value-space.json").write_text(
                        "{}\n", encoding="utf-8"
                    )
                return snapshot

            with mock.patch(
                "parasuit_parameter_policy._bounded_json_state",
                side_effect=replace_after_selection_read,
            ):
                restored = ParaSuitSelfConfiguringPolicy(
                    state,
                    PROFILES,
                    provider_commands=[],
                    program_key="target-a",
                    seed=9,
                )
            self.assertEqual(calls, [selection_path])
            self.assertEqual(restored.observations, 1)
            self.assertEqual(len(restored.coverage_history), 1)

    def test_pending_extraction_phase_survives_bound_state_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = str(Path(temporary) / "self-config.json")
            first = ParaSuitSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-pending",
                seed=4,
            )
            assignment = first.select()
            self.assertEqual(
                first.pending[assignment.token]["selection_phase"], "extraction"
            )
            first.save()

            restored = ParaSuitSelfConfiguringPolicy(
                state,
                PROFILES,
                provider_commands=[],
                program_key="target-pending",
                seed=4,
            )
            self.assertEqual(
                restored.pending[assignment.token]["selection_phase"], "extraction"
            )
            self.assertTrue(
                restored.observe(
                    assignment.token,
                    reward=0.5,
                    elapsed=0.1,
                    coverage_features=("55:0",),
                )
            )
            self.assertTrue(restored.coverage_history[0].extraction)

    def test_cli_and_mpi_master_use_branch_outcome_parameter_policy(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "util" / "parasuit_parameter_policy.py"),
                "--print-parameters",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            native_parameter_selection_provider_payload(),
        )
        master = (ROOT / "util" / "mpi_fuzzing_helper.py").read_text()
        self.assertIn("ParaSuitSelfConfiguringPolicy(", master)
        self.assertIn("coverage_features=branch_outcome_features(", master)
        self.assertIn("telemetry.branch_trace if telemetry is not None", master)


if __name__ == "__main__":
    unittest.main()
