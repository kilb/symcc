# RUN: python3 %s

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from research_protocol import (  # noqa: E402
    content_digest,
    coverage_auc,
    create_protocol,
    execute_protocol,
    parser_command_digest,
    parser_cross_calibration_configurations,
    parser_forest_ablation_configurations,
    parser_incremental_ablation_configurations,
    pcfg_context_ablation_configurations,
    time_to_target,
    verify_pcfg_research_artifact,
    verify_parser_research_artifact,
    verify_live_provenance,
    verify_protocol,
    verify_run_result,
)


class ResearchProtocolTests(unittest.TestCase):
    def _protocol(self, tmp, repeats=20, phase="confirmatory"):
        return create_protocol(
            targets=["parser", "maze"],
            configurations=[
                {
                    "name": "baseline",
                    "command": [
                        sys.executable, "-c",
                        "print('baseline:{target}:{random_seed}')",
                    ],
                    "cpu_cores": 1,
                    "wall_grace_seconds": 3,
                },
                {
                    "name": "full",
                    "command": [
                        sys.executable, "-c",
                        "print('full:{target}:{random_seed}')",
                    ],
                    "cpu_cores": 2,
                    "wall_grace_seconds": 3,
                },
            ],
            repeats=repeats,
            cpu_budget_seconds=4,
            random_seed=73,
            phase=phase,
            repo_root=ROOT,
            experiment_id="protocol-test",
            created_at="2026-07-27T00:00:00+00:00",
        )

    def test_confirmatory_plan_is_complete_paired_and_equal_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._protocol(tmp)
            verified = verify_protocol(manifest)
            self.assertEqual(verified["runs"], 80)
            self.assertEqual(verified["pairs"], 40)
            for target in ("parser", "maze"):
                for repeat in range(1, 21):
                    rows = [
                        row for row in manifest["schedule"]
                        if row["target"] == target
                        and row["repeat"] == repeat
                    ]
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(len({row["pair_id"] for row in rows}), 1)
                    self.assertEqual(
                        len({row["random_seed"] for row in rows}), 1)
                    self.assertEqual(
                        {row["cpu_budget_seconds"] for row in rows}, {4.0})
                    self.assertEqual(
                        sorted(row["wall_budget_seconds"] for row in rows),
                        [2.0, 4.0],
                    )
                    self.assertEqual(
                        sorted(row["wall_timeout_seconds"] for row in rows),
                        [5.0, 7.0],
                    )

    def test_pcfg_ablation_matrix_and_artifact_binding(self):
        base = {
            "name": "ignored",
            "command": [sys.executable, "-c", "print('pcfg')"],
            "cpu_cores": 4,
            "wall_grace_seconds": 5,
            "environment": {"COMMON": "1"},
        }
        configurations = pcfg_context_ablation_configurations(base)
        self.assertEqual(
            [item["name"] for item in configurations],
            [
                "pcfg-global",
                "pcfg-parent",
                "pcfg-circuit",
                "pcfg-sibling",
                "pcfg-history",
            ],
        )
        self.assertEqual(
            [
                item["environment"]["SYMCC_PCFG_CONTEXT_ORDER"]
                for item in configurations
            ],
            ["0", "1", "2", "3", "4"],
        )
        self.assertEqual(
            {item["cpu_cores"] for item in configurations}, {4})
        manifest = create_protocol(
            targets=["parser"],
            configurations=configurations,
            repeats=20,
            cpu_budget_seconds=40,
            random_seed=4,
            phase="confirmatory",
            repo_root=ROOT,
            experiment_id="pcfg-matrix",
            created_at="2026-07-28T00:00:00+00:00",
        )
        self.assertEqual(verify_protocol(manifest)["runs"], 100)

        row = next(
            item for item in manifest["schedule"]
            if item["configuration"] == "pcfg-history"
        )
        metrics = {
            "pcfg_context_order": 4,
            "pcfg_context_level": "history",
            "pcfg_history_adaptive_robust_gain_bits": 7.5,
        }
        core = {
            "schema": "symcc-pcfg-research-artifact-v1",
            "semantic_state_schema": 25,
            "pcfg_context_order": 4,
            "pcfg_context_level": "history",
            "metadata": {
                "run_id": row["run_id"],
                "pair_id": row["pair_id"],
                "configuration": row["configuration"],
                "random_seed": row["random_seed"],
                "cpu_budget_seconds": row["cpu_budget_seconds"],
                "cpu_cores": row["cpu_cores"],
                "experiment_id": manifest["experiment_id"],
                "phase": manifest["phase"],
            },
            "metrics": metrics,
        }
        artifact = {
            "artifact_sha256": content_digest(core),
            **core,
        }
        config = next(
            item for item in manifest["configurations"]
            if item["name"] == "pcfg-history"
        )
        self.assertTrue(verify_pcfg_research_artifact(
            artifact,
            schedule_row=row,
            configuration=config,
            protocol=manifest,
        )["verified"])
        artifact["metrics"]["pcfg_context_order"] = 3
        with self.assertRaisesRegex(ValueError, "digest"):
            verify_pcfg_research_artifact(
                artifact,
                schedule_row=row,
                configuration=config,
                protocol=manifest,
            )

        with tempfile.TemporaryDirectory() as tmp:
            missing_artifact_manifest = create_protocol(
                targets=["parser"],
                configurations=pcfg_context_ablation_configurations(
                    base, levels=["global"]),
                repeats=1,
                cpu_budget_seconds=2,
                random_seed=5,
                phase="tuning",
                repo_root=ROOT,
                experiment_id="pcfg-missing-artifact",
                created_at="2026-07-28T00:00:00+00:00",
            )
            result = execute_protocol(
                missing_artifact_manifest, tmp)[0]
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["failure_reason"].startswith(
                "invalid-pcfg-research-artifact:"))

            corrupt_base = {
                **base,
                "command": [
                    sys.executable,
                    "-c",
                    (
                        "import os,pathlib;"
                        "pathlib.Path(os.environ["
                        "'SYMCC_RESEARCH_RUN_DIR'],"
                        "'pcfg_research_artifact.json').write_text(chr(123))"
                    ),
                ],
            }
            corrupt_manifest = create_protocol(
                targets=["parser"],
                configurations=pcfg_context_ablation_configurations(
                    corrupt_base, levels=["global"]),
                repeats=1,
                cpu_budget_seconds=2,
                random_seed=6,
                phase="tuning",
                repo_root=ROOT,
                experiment_id="pcfg-corrupt-artifact",
                created_at="2026-07-28T00:00:00+00:00",
            )
            corrupt_dir = Path(tmp) / "corrupt"
            corrupt_result = execute_protocol(
                corrupt_manifest, corrupt_dir)[0]
            self.assertEqual(corrupt_result["status"], "failed")
            self.assertTrue(verify_run_result(
                corrupt_result,
                corrupt_manifest,
                artifact_root=corrupt_dir,
            )["verified"])

    def test_parser_ablation_matrix_and_artifact_binding(self):
        base = {
            "name": "ignored",
            "command": [sys.executable, "-c", "print('parser')"],
            "cpu_cores": 3,
            "wall_grace_seconds": 4,
            "environment": {"COMMON": "1"},
        }
        configurations = parser_incremental_ablation_configurations(base)
        self.assertEqual(
            [item["name"] for item in configurations],
            ["parser-cold", "parser-incremental"],
        )
        self.assertEqual(
            [
                item["environment"]["SYMCC_PROPOSAL_PARSER_CACHE"]
                for item in configurations
            ],
            ["0", "1"],
        )
        self.assertEqual(
            {item["cpu_cores"] for item in configurations}, {3})
        manifest = create_protocol(
            targets=["parser"],
            configurations=configurations,
            repeats=20,
            cpu_budget_seconds=30,
            random_seed=7,
            phase="confirmatory",
            repo_root=ROOT,
            experiment_id="parser-matrix",
            created_at="2026-07-28T00:00:00+00:00",
        )
        self.assertEqual(verify_protocol(manifest)["runs"], 40)
        row = next(
            item for item in manifest["schedule"]
            if item["configuration"] == "parser-incremental"
        )
        metrics = {
            "proposal_parser_validations": 10,
            "proposal_parser_cache_requests": 10,
            "proposal_parser_cache_incremental_offers": 8,
            "proposal_parser_incremental_receipts": 7,
            "proposal_parser_incremental_zero_reuse": 1,
            "proposal_parser_node_id_proofs": 7,
            "proposal_parser_forest_traces": 0,
            "proposal_parser_forest_complete_traces": 0,
            "proposal_parser_forest_proofs": 0,
            "proposal_parser_forest_parse_time_us": 0,
            "proposal_parser_reported_parse_time_us": 0,
            "proposal_parser_cache_enabled": 1,
            "proposal_parser_cross_traces": 0,
            "proposal_parser_cross_agreements": 0,
            "proposal_parser_cross_both_accept": 0,
            "proposal_parser_cross_primary_only": 0,
            "proposal_parser_cross_secondary_only": 0,
            "proposal_parser_cross_both_reject": 0,
            "proposal_parser_cross_command_pairs": 0,
            "proposal_parser_cross_structural_pairs": 0,
            "proposal_parser_cross_primary_selected_spans": 0,
            "proposal_parser_cross_primary_forest_spans": 0,
            "proposal_parser_cross_secondary_spans": 0,
            "proposal_parser_cross_selected_shared_spans": 0,
            "proposal_parser_cross_forest_shared_spans": 0,
            "proposal_parser_cross_selected_union_spans": 0,
            "proposal_parser_cross_forest_union_spans": 0,
            "proposal_parser_cross_primary_boundaries": 0,
            "proposal_parser_cross_secondary_boundaries": 0,
            "proposal_parser_cross_shared_boundaries": 0,
            "proposal_parser_cross_union_boundaries": 0,
            "proposal_parser_cross_primary_symbols": 0,
            "proposal_parser_cross_secondary_symbols": 0,
            "proposal_parser_cross_symbol_correspondences": 0,
            "proposal_parser_cross_ambiguous_symbol_spans": 0,
            "proposal_parser_cross_production_correspondences": 0,
            "proposal_parser_cross_symbol_mappings": 0,
            "proposal_parser_cross_production_mappings": 0,
            "proposal_parser_mean_wall_time_us": 120.0,
        }
        core = {
            "schema": "symcc-parser-research-artifact-v1",
            "proposal_state_schema": 15,
            "parser_cache_enabled": True,
            "parser_command_sha256": parser_command_digest(""),
            "parser_forest_grammar_sha256s": [],
            "parser_cross_command_pairs": [],
            "parser_cross_symbol_correspondence": [],
            "parser_cross_production_correspondence": [],
            "metadata": {
                "run_id": row["run_id"],
                "pair_id": row["pair_id"],
                "configuration": row["configuration"],
                "random_seed": row["random_seed"],
                "cpu_budget_seconds": row["cpu_budget_seconds"],
                "cpu_cores": row["cpu_cores"],
                "experiment_id": manifest["experiment_id"],
                "phase": manifest["phase"],
            },
            "metrics": metrics,
        }
        artifact = {"artifact_sha256": content_digest(core), **core}
        config = next(
            item for item in manifest["configurations"]
            if item["name"] == "parser-incremental"
        )
        self.assertTrue(verify_parser_research_artifact(
            artifact,
            schedule_row=row,
            configuration=config,
            protocol=manifest,
        )["verified"])
        artifact["metrics"]["proposal_parser_node_id_proofs"] = 8
        with self.assertRaisesRegex(ValueError, "digest"):
            verify_parser_research_artifact(
                artifact,
                schedule_row=row,
                configuration=config,
                protocol=manifest,
            )

        with tempfile.TemporaryDirectory() as tmp:
            missing_manifest = create_protocol(
                targets=["parser"],
                configurations=parser_incremental_ablation_configurations(
                    base)[:1],
                repeats=1,
                cpu_budget_seconds=2,
                random_seed=8,
                phase="tuning",
                repo_root=ROOT,
                experiment_id="parser-missing-artifact",
                created_at="2026-07-28T00:00:00+00:00",
            )
            result = execute_protocol(missing_manifest, tmp)[0]
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["failure_reason"].startswith(
                "invalid-parser-research-artifact:"))
            self.assertTrue(verify_run_result(
                result,
                missing_manifest,
                artifact_root=tmp,
            )["verified"])

    def test_complete_forest_ablation_matrix_and_artifact_binding(self):
        base = {
            "name": "ignored",
            "command": [sys.executable, "-c", "print('forest')"],
            "cpu_cores": 2,
            "wall_grace_seconds": 4,
            "environment": {"COMMON": "1"},
        }
        configurations = parser_forest_ablation_configurations(
            base,
            selected_parser_command=(
                "python3 tree.py --input {input} --trace {trace}"),
            forest_parser_command=(
                "python3 forest.py --input {input} --trace {trace}"),
            forest_grammar_sha256="a" * 64,
        )
        self.assertEqual(
            [item["name"] for item in configurations],
            [
                "parser-forest-off",
                "parser-forest-selected",
                "parser-forest-complete",
            ],
        )
        self.assertEqual(
            [
                item["environment"]["SYMCC_PARSER_FOREST_MODE"]
                for item in configurations
            ],
            ["off", "selected", "complete"],
        )
        self.assertEqual(
            {
                item["environment"]["SYMCC_PROPOSAL_PARSER_CACHE"]
                for item in configurations
            },
            {"0"},
        )
        manifest = create_protocol(
            targets=["parser"],
            configurations=configurations,
            repeats=20,
            cpu_budget_seconds=30,
            random_seed=9,
            phase="confirmatory",
            repo_root=ROOT,
            experiment_id="forest-matrix",
            created_at="2026-07-28T00:00:00+00:00",
        )
        self.assertEqual(verify_protocol(manifest)["runs"], 60)
        row = next(
            item for item in manifest["schedule"]
            if item["configuration"] == "parser-forest-complete"
        )
        metrics = {
            "proposal_parser_validations": 10,
            "proposal_parser_cache_requests": 0,
            "proposal_parser_cache_incremental_offers": 0,
            "proposal_parser_incremental_receipts": 0,
            "proposal_parser_incremental_zero_reuse": 0,
            "proposal_parser_node_id_proofs": 0,
            "proposal_parser_forest_traces": 10,
            "proposal_parser_forest_complete_traces": 9,
            "proposal_parser_forest_proofs": 10,
            "proposal_parser_forest_parse_time_us": 500,
            "proposal_parser_reported_parse_time_us": 500,
            "proposal_parser_cache_enabled": 0,
            "proposal_parser_cross_traces": 0,
            "proposal_parser_cross_agreements": 0,
            "proposal_parser_cross_both_accept": 0,
            "proposal_parser_cross_primary_only": 0,
            "proposal_parser_cross_secondary_only": 0,
            "proposal_parser_cross_both_reject": 0,
            "proposal_parser_cross_command_pairs": 0,
            "proposal_parser_cross_structural_pairs": 0,
            "proposal_parser_cross_primary_selected_spans": 0,
            "proposal_parser_cross_primary_forest_spans": 0,
            "proposal_parser_cross_secondary_spans": 0,
            "proposal_parser_cross_selected_shared_spans": 0,
            "proposal_parser_cross_forest_shared_spans": 0,
            "proposal_parser_cross_selected_union_spans": 0,
            "proposal_parser_cross_forest_union_spans": 0,
            "proposal_parser_cross_primary_boundaries": 0,
            "proposal_parser_cross_secondary_boundaries": 0,
            "proposal_parser_cross_shared_boundaries": 0,
            "proposal_parser_cross_union_boundaries": 0,
            "proposal_parser_cross_primary_symbols": 0,
            "proposal_parser_cross_secondary_symbols": 0,
            "proposal_parser_cross_symbol_correspondences": 0,
            "proposal_parser_cross_ambiguous_symbol_spans": 0,
            "proposal_parser_cross_production_correspondences": 0,
            "proposal_parser_cross_symbol_mappings": 0,
            "proposal_parser_cross_production_mappings": 0,
        }
        core = {
            "schema": "symcc-parser-research-artifact-v1",
            "proposal_state_schema": 15,
            "parser_cache_enabled": False,
            "parser_command_sha256": parser_command_digest(
                "python3 forest.py --input {input} --trace {trace}"),
            "parser_forest_grammar_sha256s": ["a" * 64],
            "parser_cross_command_pairs": [],
            "parser_cross_symbol_correspondence": [],
            "parser_cross_production_correspondence": [],
            "metadata": {
                "run_id": row["run_id"],
                "pair_id": row["pair_id"],
                "configuration": row["configuration"],
                "random_seed": row["random_seed"],
                "cpu_budget_seconds": row["cpu_budget_seconds"],
                "cpu_cores": row["cpu_cores"],
                "experiment_id": manifest["experiment_id"],
                "phase": manifest["phase"],
                "parser_forest_mode": "complete",
            },
            "metrics": metrics,
        }
        artifact = {"artifact_sha256": content_digest(core), **core}
        config = next(
            item for item in manifest["configurations"]
            if item["name"] == "parser-forest-complete"
        )
        self.assertTrue(verify_parser_research_artifact(
            artifact,
            schedule_row=row,
            configuration=config,
            protocol=manifest,
        )["verified"])
        core["metadata"]["parser_forest_mode"] = "selected"
        artifact = {"artifact_sha256": content_digest(core), **core}
        with self.assertRaisesRegex(ValueError, "forest mode"):
            verify_parser_research_artifact(
                artifact,
                schedule_row=row,
                configuration=config,
                protocol=manifest,
            )
        core["metadata"]["parser_forest_mode"] = "complete"
        core["parser_command_sha256"] = parser_command_digest("other")
        artifact = {"artifact_sha256": content_digest(core), **core}
        with self.assertRaisesRegex(ValueError, "parser command"):
            verify_parser_research_artifact(
                artifact,
                schedule_row=row,
                configuration=config,
                protocol=manifest,
            )
        core["parser_command_sha256"] = parser_command_digest(
            "python3 forest.py --input {input} --trace {trace}")
        core["parser_forest_grammar_sha256s"] = ["b" * 64]
        artifact = {"artifact_sha256": content_digest(core), **core}
        with self.assertRaisesRegex(ValueError, "forest grammar"):
            verify_parser_research_artifact(
                artifact,
                schedule_row=row,
                configuration=config,
                protocol=manifest,
            )

    def test_confirmatory_plan_rejects_underpowered_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "at least 20"):
                self._protocol(tmp, repeats=19)
            tuning = self._protocol(tmp, repeats=2, phase="tuning")
            self.assertFalse(verify_protocol(tuning)["confirmatory"])

    def test_candidate_paired_cross_parser_calibration_is_sealed(self):
        base = {
            "name": "ignored",
            "command": [sys.executable, "-c", "print('cross')"],
            "cpu_cores": 2,
            "wall_grace_seconds": 4,
            "environment": {"COMMON": "1"},
        }
        selected = "python3 selected.py {input} {trace}"
        forest = "python3 forest.py {input} {trace}"
        paired = (
            "python3 cross.py --input {input} --trace {trace} "
            "--primary forest --secondary selected"
        )
        configurations = parser_cross_calibration_configurations(
            base,
            selected_parser_command=selected,
            forest_parser_command=forest,
            paired_parser_command=paired,
            forest_grammar_sha256="c" * 64,
        )
        self.assertEqual(
            [item["name"] for item in configurations],
            [
                "parser-cross-selected",
                "parser-cross-complete",
                "parser-cross-paired",
            ],
        )
        self.assertEqual(
            [
                item["environment"]["SYMCC_PARSER_CROSS_MODE"]
                for item in configurations
            ],
            ["off", "off", "paired"],
        )
        manifest = create_protocol(
            targets=["parser"],
            configurations=configurations,
            repeats=20,
            cpu_budget_seconds=30,
            random_seed=19,
            phase="confirmatory",
            repo_root=ROOT,
            experiment_id="cross-matrix",
            created_at="2026-07-28T00:00:00+00:00",
        )
        self.assertEqual(verify_protocol(manifest)["runs"], 60)
        row = next(
            item for item in manifest["schedule"]
            if item["configuration"] == "parser-cross-paired"
        )
        command_pairs = [{
            "primary_sha256": parser_command_digest(forest),
            "secondary_sha256": parser_command_digest(selected),
        }]
        metrics = {
            "proposal_parser_validations": 10,
            "proposal_parser_cache_requests": 0,
            "proposal_parser_cache_incremental_offers": 0,
            "proposal_parser_incremental_receipts": 0,
            "proposal_parser_incremental_zero_reuse": 0,
            "proposal_parser_node_id_proofs": 0,
            "proposal_parser_forest_traces": 10,
            "proposal_parser_forest_complete_traces": 6,
            "proposal_parser_forest_proofs": 10,
            "proposal_parser_forest_parse_time_us": 500,
            "proposal_parser_reported_parse_time_us": 500,
            "proposal_parser_cache_enabled": 0,
            "proposal_parser_cross_traces": 10,
            "proposal_parser_cross_agreements": 7,
            "proposal_parser_cross_both_accept": 4,
            "proposal_parser_cross_primary_only": 2,
            "proposal_parser_cross_secondary_only": 1,
            "proposal_parser_cross_both_reject": 3,
            "proposal_parser_cross_command_pairs": 1,
            "proposal_parser_cross_structural_pairs": 4,
            "proposal_parser_cross_primary_selected_spans": 10,
            "proposal_parser_cross_primary_forest_spans": 12,
            "proposal_parser_cross_secondary_spans": 8,
            "proposal_parser_cross_selected_shared_spans": 6,
            "proposal_parser_cross_forest_shared_spans": 7,
            "proposal_parser_cross_selected_union_spans": 12,
            "proposal_parser_cross_forest_union_spans": 13,
            "proposal_parser_cross_primary_boundaries": 5,
            "proposal_parser_cross_secondary_boundaries": 4,
            "proposal_parser_cross_shared_boundaries": 3,
            "proposal_parser_cross_union_boundaries": 6,
            "proposal_parser_cross_primary_symbols": 6,
            "proposal_parser_cross_secondary_symbols": 5,
            "proposal_parser_cross_symbol_correspondences": 4,
            "proposal_parser_cross_ambiguous_symbol_spans": 1,
            "proposal_parser_cross_production_correspondences": 2,
            "proposal_parser_cross_symbol_mappings": 2,
            "proposal_parser_cross_production_mappings": 1,
        }
        core = {
            "schema": "symcc-parser-research-artifact-v1",
            "proposal_state_schema": 15,
            "parser_cache_enabled": False,
            "parser_command_sha256": parser_command_digest(paired),
            "parser_forest_grammar_sha256s": ["c" * 64],
            "parser_cross_command_pairs": command_pairs,
            "parser_cross_symbol_correspondence": [
                {
                    "primary_parser": "earley-v1",
                    "secondary_parser": "glr-v1",
                    "primary_symbol": "A",
                    "secondary_symbol": "X",
                    "observations": 3,
                },
                {
                    "primary_parser": "earley-v1",
                    "secondary_parser": "glr-v1",
                    "primary_symbol": "B",
                    "secondary_symbol": "Y",
                    "observations": 1,
                },
            ],
            "parser_cross_production_correspondence": [{
                "primary_parser": "earley-v1",
                "secondary_parser": "glr-v1",
                "primary_symbol": "A",
                "primary_state": "complete",
                "secondary_symbol": "X",
                "secondary_state": "complete",
                "shape_sha256": "d" * 64,
                "observations": 2,
            }],
            "metadata": {
                "run_id": row["run_id"],
                "pair_id": row["pair_id"],
                "configuration": row["configuration"],
                "random_seed": row["random_seed"],
                "cpu_budget_seconds": row["cpu_budget_seconds"],
                "cpu_cores": row["cpu_cores"],
                "experiment_id": manifest["experiment_id"],
                "phase": manifest["phase"],
                "parser_forest_mode": "complete",
                "parser_cross_mode": "paired",
            },
            "metrics": metrics,
        }
        artifact = {"artifact_sha256": content_digest(core), **core}
        config = next(
            item for item in manifest["configurations"]
            if item["name"] == "parser-cross-paired"
        )
        self.assertTrue(verify_parser_research_artifact(
            artifact,
            schedule_row=row,
            configuration=config,
            protocol=manifest,
        )["verified"])

        core["metrics"]["proposal_parser_cross_primary_only"] = 3
        artifact = {"artifact_sha256": content_digest(core), **core}
        with self.assertRaisesRegex(ValueError, "counters"):
            verify_parser_research_artifact(
                artifact,
                schedule_row=row,
                configuration=config,
                protocol=manifest,
            )
        core["metrics"]["proposal_parser_cross_primary_only"] = 2
        core["parser_cross_symbol_correspondence"][0]["observations"] = 4
        artifact = {"artifact_sha256": content_digest(core), **core}
        with self.assertRaisesRegex(ValueError, "counters"):
            verify_parser_research_artifact(
                artifact,
                schedule_row=row,
                configuration=config,
                protocol=manifest,
            )
        core["parser_cross_symbol_correspondence"][0]["observations"] = 3
        core["metrics"]["proposal_parser_cross_selected_union_spans"] = 13
        artifact = {"artifact_sha256": content_digest(core), **core}
        with self.assertRaisesRegex(ValueError, "counters"):
            verify_parser_research_artifact(
                artifact,
                schedule_row=row,
                configuration=config,
                protocol=manifest,
            )

    def test_cli_plans_complete_forest_ablation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {
                "experiment_id": "forest-cli-test",
                "phase": "tuning",
                "repeats": 1,
                "cpu_budget_seconds": 2,
                "random_seed": 11,
                "targets": ["parser"],
                "parser_forest_ablation": {
                    "selected_parser_command": (
                        "tree --input {input} --trace {trace}"),
                    "forest_parser_command": (
                        "forest --input {input} --trace {trace}"),
                    "forest_grammar_sha256": "a" * 64,
                    "base_configuration": {
                        "name": "ignored",
                        "command": [
                            sys.executable, "-c", "print('forest-cli')"],
                        "cpu_cores": 1,
                    },
                },
            }
            spec_path = root / "spec.json"
            manifest_path = root / "manifest.json"
            spec_path.write_text(json.dumps(spec))
            tool = str(ROOT / "benchmark" / "research_protocol.py")
            subprocess.run(
                [
                    sys.executable,
                    tool,
                    "plan",
                    str(spec_path),
                    "--output",
                    str(manifest_path),
                    "--repo-root",
                    str(ROOT),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(
                [
                    item["name"]
                    for item in manifest["configurations"]
                ],
                [
                    "parser-forest-off",
                    "parser-forest-selected",
                    "parser-forest-complete",
                ],
            )

    def test_cli_plans_cross_parser_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {
                "experiment_id": "cross-cli-test",
                "phase": "tuning",
                "repeats": 1,
                "cpu_budget_seconds": 2,
                "random_seed": 21,
                "targets": ["parser"],
                "parser_cross_calibration": {
                    "selected_parser_command":
                        "selected {input} {trace}",
                    "forest_parser_command":
                        "forest {input} {trace}",
                    "paired_parser_command":
                        "paired {input} {trace}",
                    "forest_grammar_sha256": "d" * 64,
                    "base_configuration": {
                        "name": "ignored",
                        "command": [
                            sys.executable, "-c", "print('cross-cli')"],
                        "cpu_cores": 1,
                    },
                },
            }
            spec_path = root / "spec.json"
            manifest_path = root / "manifest.json"
            spec_path.write_text(json.dumps(spec))
            tool = str(ROOT / "benchmark" / "research_protocol.py")
            subprocess.run(
                [
                    sys.executable,
                    tool,
                    "plan",
                    str(spec_path),
                    "--output",
                    str(manifest_path),
                    "--repo-root",
                    str(ROOT),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(
                [
                    item["name"]
                    for item in manifest["configurations"]
                ],
                [
                    "parser-cross-selected",
                    "parser-cross-complete",
                    "parser-cross-paired",
                ],
            )

    def test_manifest_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._protocol(tmp)
            manifest["schedule"][0]["cpu_budget_seconds"] = 9
            with self.assertRaisesRegex(ValueError, "digest"):
                verify_protocol(manifest)
            manifest["digest"] = content_digest({
                key: value for key, value in manifest.items()
                if key != "digest"
            })
            with self.assertRaisesRegex(ValueError, "equal CPU"):
                verify_protocol(manifest)

    def test_executor_rejects_input_drift_after_sealing(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "seed"
            input_path.write_bytes(b"sealed")
            manifest = create_protocol(
                targets=["parser"],
                configurations=[{
                    "name": "baseline",
                    "command": [sys.executable, "-c", "print('unused')"],
                }],
                repeats=1,
                cpu_budget_seconds=1,
                random_seed=9,
                phase="tuning",
                repo_root=ROOT,
                inputs=[input_path],
                experiment_id="input-drift-test",
                created_at="2026-08-25T00:00:00+00:00",
            )
            self.assertTrue(verify_live_provenance(manifest)["verified"])
            input_path.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "provenance drift"):
                execute_protocol(manifest, Path(tmp) / "results")

    def test_auc_and_right_censored_time_to_target(self):
        points = [
            {"timestamp_sec": 0, "edges_found": 10},
            {"timestamp_sec": 5, "edges_found": 20},
            {"timestamp_sec": 10, "edges_found": 30},
        ]
        self.assertEqual(coverage_auc(points, 10), 20)
        self.assertEqual(time_to_target(points, 20, 10), (5.0, False))
        self.assertEqual(time_to_target(points, 40, 10), (10.0, True))

    def test_executor_retains_success_failure_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = create_protocol(
                targets=["parser"],
                configurations=[
                    {
                        "name": "ok",
                        "command": [sys.executable, "-c", "print('ok')"],
                    },
                    {
                        "name": "fail",
                        "command": [sys.executable, "-c", "raise SystemExit(3)"],
                    },
                    {
                        "name": "timeout",
                        "command": [
                            sys.executable, "-c",
                            "import time; time.sleep(2)",
                        ],
                    },
                ],
                repeats=1,
                cpu_budget_seconds=0.2,
                random_seed=1,
                phase="tuning",
                repo_root=ROOT,
                inputs=[ROOT / "benchmark" / "seeds"],
                experiment_id="execute-test",
                created_at="2026-07-27T00:00:00+00:00",
            )
            seed_identity = manifest["provenance"]["inputs"][0]
            self.assertEqual(seed_identity["kind"], "directory")
            self.assertGreater(seed_identity["files"], 0)
            self.assertTrue(manifest["provenance"]["working_tree_sha256"])
            results = execute_protocol(manifest, Path(tmp) / "results")
            self.assertEqual(
                {row["configuration"]: row["status"] for row in results},
                {"ok": "success", "fail": "failed", "timeout": "timeout"},
            )
            self.assertTrue(
                (Path(tmp) / "results" / "research_results.jsonl").is_file()
            )
            rerun = execute_protocol(
                manifest, Path(tmp) / "results", resume=True)
            self.assertEqual(
                [row["digest"] for row in rerun],
                [row["digest"] for row in results],
            )
            result = json.loads(
                (Path(tmp) / "results" / "runs"
                 / results[0]["run_id"] / "result.json").read_text()
            )
            self.assertEqual(result["protocol_digest"], manifest["digest"])
            verified = verify_run_result(
                result, manifest, artifact_root=Path(tmp) / "results")
            self.assertTrue(verified["verified"])
            stdout_path = Path(tmp) / "results" / result["stdout"]
            original_stdout = stdout_path.read_bytes()
            stdout_path.write_bytes(original_stdout + b"tamper")
            with self.assertRaisesRegex(ValueError, "stdout|artifact tree"):
                verify_run_result(
                    result, manifest, artifact_root=Path(tmp) / "results")
            stdout_path.write_bytes(original_stdout)
            result["status"] = "failed"
            with self.assertRaisesRegex(ValueError, "digest"):
                verify_run_result(
                    result, manifest, artifact_root=Path(tmp) / "results")

    def test_cli_plan_run_and_verify_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            spec = {
                "experiment_id": "cli-test",
                "phase": "tuning",
                "repeats": 1,
                "cpu_budget_seconds": 2,
                "random_seed": 8,
                "targets": ["parser"],
                "configurations": [{
                    "name": "baseline",
                    "command": [sys.executable, "-c", "print('cli-ok')"],
                    "cpu_cores": 1,
                }],
            }
            spec_path = tmp_path / "spec.json"
            manifest_path = tmp_path / "protocol.json"
            result_dir = tmp_path / "results"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            tool = str(ROOT / "benchmark" / "research_protocol.py")
            subprocess.run([
                sys.executable, tool, "plan", str(spec_path),
                "--output", str(manifest_path), "--repo-root", str(ROOT),
            ], check=True, capture_output=True, text=True)
            subprocess.run([
                sys.executable, tool, "run", str(manifest_path),
                "--output-dir", str(result_dir),
            ], check=True, capture_output=True, text=True)
            verified = subprocess.run([
                sys.executable, tool, "verify", str(manifest_path),
                "--results-dir", str(result_dir),
            ], check=True, capture_output=True, text=True)
            report = json.loads(verified.stdout)
            self.assertEqual(report["verified_results"], 1)


if __name__ == "__main__":
    unittest.main()
