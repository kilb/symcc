# RUN: python3 %s

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from analyze_ablation import (  # noqa: E402
    attach_parser_artifact_metrics,
    attach_pcfg_artifact_metrics,
    attach_timeseries_metrics,
    holm_adjust,
    load_parser_artifacts,
    load_pcfg_artifacts,
    load_rows,
    randomization_p_value,
    summarize,
    vargha_delaney,
    write_csv,
)
from research_protocol import (  # noqa: E402
    content_digest,
    parser_command_digest,
)


class AblationAnalysisTests(unittest.TestCase):
    def test_vargha_delaney_orders_better_samples_above_half(self):
        self.assertGreater(vargha_delaney([3.0, 4.0], [1.0, 2.0]), 0.5)
        self.assertEqual(vargha_delaney([1.0], [1.0]), 0.5)

    def test_verified_pcfg_artifact_metrics_join_by_protocol_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = {
                "schema": "symcc-pcfg-research-artifact-v1",
                "semantic_state_schema": 25,
                "pcfg_context_order": 4,
                "pcfg_context_level": "history",
                "metadata": {"run_id": "run-history"},
                "metrics": {
                    "pcfg_context_order": 4,
                    "pcfg_context_level": "history",
                    "pcfg_history_adaptive_robust_gain_bits": 9.25,
                    "rules": 10,
                },
            }
            artifact = {
                "artifact_sha256": content_digest(core),
                **core,
            }
            path = Path(tmp) / "pcfg_research_artifact.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            loaded = load_pcfg_artifacts([tmp])
            rows = [{
                "run_id": "inner-run",
                "protocol_run_id": "run-history",
                "target": "parser",
                "configuration": "pcfg-history",
                "np": "4",
            }]
            attach_pcfg_artifact_metrics(rows, loaded)
            self.assertEqual(rows[0]["pcfg_artifact_verified"], "1")
            self.assertEqual(
                float(rows[0][
                    "pcfg_history_adaptive_robust_gain_bits"]),
                9.25,
            )
            self.assertNotIn("rules", rows[0])

    def test_verified_parser_artifact_metrics_join_by_protocol_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = {
                "schema": "symcc-parser-research-artifact-v1",
                "proposal_state_schema": 15,
                "parser_cache_enabled": True,
                "parser_command_sha256": parser_command_digest(""),
                "parser_forest_grammar_sha256s": [],
                "parser_cross_command_pairs": [],
                "parser_cross_symbol_correspondence": [],
                "parser_cross_production_correspondence": [],
                "metadata": {"run_id": "run-parser"},
                "metrics": {
                    "proposal_parser_validations": 4,
                    "proposal_parser_cache_requests": 4,
                    "proposal_parser_cache_incremental_offers": 3,
                    "proposal_parser_incremental_receipts": 3,
                    "proposal_parser_incremental_zero_reuse": 0,
                    "proposal_parser_node_id_proofs": 3,
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
                    "proposal_parser_mean_wall_time_us": 125.5,
                    "proposal_records": 4,
                },
            }
            artifact = {
                "artifact_sha256": content_digest(core),
                **core,
            }
            path = Path(tmp) / "parser_research_artifact.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            loaded = load_parser_artifacts([tmp])
            rows = [{
                "run_id": "inner-run",
                "protocol_run_id": "run-parser",
                "target": "parser",
                "configuration": "parser-incremental",
                "np": "4",
            }]
            attach_parser_artifact_metrics(rows, loaded)
            self.assertEqual(rows[0]["parser_artifact_verified"], "1")
            self.assertEqual(
                float(rows[0][
                    "proposal_parser_mean_wall_time_us"]),
                125.5,
            )
            self.assertNotIn("proposal_records", rows[0])

    def test_summary_compares_modes_per_target_and_np(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "benchmark_data.csv"
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=["target", "mode", "np", "round", "edge_cov"])
                writer.writeheader()
                for round_id, cov in enumerate([10.0, 12.0, 14.0], 1):
                    writer.writerow({
                        "target": "parser",
                        "mode": "baseline",
                        "np": "4",
                        "round": round_id,
                        "edge_cov": cov,
                    })
                for round_id, cov in enumerate([13.0, 16.0, 19.0], 1):
                    writer.writerow({
                        "target": "parser",
                        "mode": "poly",
                        "np": "4",
                        "round": round_id,
                        "edge_cov": cov,
                    })

            rows = summarize(load_rows([str(path)]), "edge_cov", "baseline")
            poly = [row for row in rows if row["mode"] == "poly"][0]
            self.assertEqual(poly["n"], 3)
            self.assertGreater(poly["delta_vs_baseline"], 0)
            self.assertGreater(poly["a12_vs_baseline"], 0.5)
            self.assertEqual(poly["paired_n"], 3)
            self.assertGreater(poly["cliffs_delta_favorable"], 0)

            out = Path(tmp) / "summary.csv"
            write_csv(rows, str(out))
            self.assertIn("a12_vs_baseline", out.read_text(encoding="utf-8"))

    def test_cli_default_matches_benchmark_csv_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "benchmark_data.csv"
            with source.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=["target", "mode", "np", "edge_cov_pct"],
                )
                writer.writeheader()
                writer.writerow({
                    "target": "parser",
                    "mode": "baseline",
                    "np": "2",
                    "edge_cov_pct": "12.5",
                })
            output = Path(tmp) / "summary"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "benchmark" / "analyze_ablation.py"),
                    str(source),
                    "--output-dir",
                    str(output),
                ],
                check=True,
            )
            with (output / "ablation_summary.csv").open(
                    newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["metric"], "edge_cov_pct")

    def test_failures_censoring_and_lower_is_better_are_retained(self):
        rows = []
        for repeat in range(1, 21):
            pair = f"pair-{repeat}"
            rows.append({
                "target": "parser", "mode": "baseline", "np": "4",
                "pair_id": pair, "phase": "confirmatory",
                "status": "success", "wall_time_sec": str(20 + repeat),
            })
            rows.append({
                "target": "parser", "mode": "full", "np": "4",
                "pair_id": pair, "phase": "confirmatory",
                "status": "timeout" if repeat == 20 else "success",
                "wall_time_sec": str(10 + repeat),
                "time_to_target_censored": "1" if repeat == 20 else "0",
            })
        summary = summarize(
            rows, "wall_time_sec", "baseline",
            bootstrap_samples=300, permutation_samples=1000)
        full = next(row for row in summary if row["mode"] == "full")
        self.assertEqual(full["n_total"], 20)
        self.assertEqual(full["n"], 20)
        self.assertEqual(full["n_timeout"], 1)
        self.assertEqual(full["n_censored"], 1)
        self.assertEqual(full["direction"], "lower")
        self.assertGreater(full["favorable_delta"], 0)
        self.assertLess(full["a12_favorable"], 1.0)
        self.assertEqual(full["evidence_grade"], "exploratory")

    def test_timeout_majority_cannot_be_reported_as_large_favorable_effect(self):
        rows = []
        for repeat in range(1, 21):
            rows.append({
                "target": "parser", "mode": "baseline", "np": "4",
                "pair_id": f"pair-{repeat}", "status": "success",
                "time_to_target_sec": "100",
            })
            rows.append({
                "target": "parser", "mode": "candidate", "np": "4",
                "pair_id": f"pair-{repeat}",
                "status": "success" if repeat <= 5 else "timeout",
                "time_to_target_sec": "10" if repeat <= 5 else "100",
                "time_to_target_censored": "0" if repeat <= 5 else "1",
            })
        summary = summarize(
            rows,
            "time_to_target_sec",
            "baseline",
            bootstrap_samples=300,
            permutation_samples=1000,
        )
        candidate = next(row for row in summary if row["mode"] == "candidate")
        self.assertEqual(candidate["n"], 20)
        self.assertEqual(candidate["n_censored"], 15)
        self.assertEqual(candidate["favorable_delta"], 0.0)
        self.assertLess(candidate["a12_favorable"], 0.5)
        self.assertEqual(candidate["evidence_grade"], "exploratory")

    def test_time_to_target_censoring_does_not_penalize_coverage_metric(self):
        rows = [
            {
                "target": "parser", "mode": "baseline", "np": "4",
                "status": "success", "edge_cov_pct": "10",
                "time_to_target_censored": "1",
            },
            {
                "target": "parser", "mode": "candidate", "np": "4",
                "status": "success", "edge_cov_pct": "20",
                "time_to_target_censored": "1",
            },
        ]
        summary = summarize(
            rows, "edge_cov_pct", "baseline", bootstrap_samples=100)
        candidate = next(row for row in summary if row["mode"] == "candidate")
        self.assertEqual(candidate["a12_favorable"], 1.0)
        self.assertIn(
            "not-applicable", str(candidate["censoring_method"]))

    def test_time_to_target_rejects_higher_is_better_override(self):
        rows = [{
            "target": "parser", "mode": "baseline", "np": "4",
            "status": "success", "time_to_target_sec": "10",
        }]
        with self.assertRaisesRegex(ValueError, "lower-is-better"):
            summarize(rows, "time_to_target_sec", direction="higher")

    def test_paired_randomization_holm_and_timeseries_auc(self):
        self.assertLess(
            randomization_p_value([5.0] * 10), 0.01)
        adjusted = holm_adjust([0.01, 0.04, 0.03])
        self.assertEqual(len(adjusted), 3)
        self.assertTrue(all(0 <= value <= 1 for value in adjusted))

        rows = [{
            "target": "parser", "mode": "full", "np": "4", "round": "1",
            "wall_time_sec": "10", "status": "success",
        }]
        points = [
            {
                "target": "parser", "mode": "full", "np": "4", "round": "1",
                "timestamp_sec": "0", "edges_found": "10",
            },
            {
                "target": "parser", "mode": "full", "np": "4", "round": "1",
                "timestamp_sec": "10", "edges_found": "30",
            },
        ]
        attach_timeseries_metrics(rows, points, target_threshold=25)
        self.assertEqual(float(rows[0]["coverage_auc"]), 20)
        self.assertEqual(float(rows[0]["time_to_target_sec"]), 10)
        self.assertEqual(rows[0]["time_to_target_censored"], "0")


if __name__ == "__main__":
    unittest.main()
