# RUN: python3 %s

import csv
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from analyze_current_evaluation import (  # noqa: E402
    compare,
    load_groups,
    parse_number,
    render_svg,
    summarize,
)


class CurrentEvaluationAnalysisTests(unittest.TestCase):
    def _write_campaign(self, path, hybrid_offset):
        fields = [
            "status", "target", "mode", "round", "edge_cov_pct",
            "afl_bitmap_cvg", "generated", "afl_execs_done",
        ]
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for repeat in range(1, 21):
                writer.writerow({
                    "status": "success",
                    "target": "parser",
                    "mode": "hybrid",
                    "round": repeat,
                    "edge_cov_pct": 10.0 + repeat / 10.0 + hybrid_offset,
                    "afl_bitmap_cvg": f"{9.0 + repeat / 10.0:.2f}%",
                    "generated": 100 + repeat,
                    "afl_execs_done": 1000 + repeat,
                })
                writer.writerow({
                    "status": "success",
                    "target": "parser",
                    "mode": "afl-only",
                    "round": repeat,
                    "edge_cov_pct": 8.0 + repeat / 10.0,
                    "afl_bitmap_cvg": f"{8.0 + repeat / 10.0:.2f}%",
                    "generated": 50 + repeat,
                    "afl_execs_done": 500 + repeat,
                })

    def test_percent_values_are_parsed(self):
        self.assertEqual(parse_number("12.5%"), 12.5)
        self.assertEqual(parse_number(" 7 "), 7.0)
        self.assertIsNone(parse_number(""))
        self.assertIsNone(parse_number("not-a-number"))

    def test_campaign_uses_independent_samples_and_svg_is_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy.csv"
            current = root / "current.csv"
            self._write_campaign(legacy, 0.0)
            self._write_campaign(current, 2.0)
            rows = load_groups([
                f"legacy={legacy}",
                f"current={current}",
            ])

            summaries = summarize(rows, ["edge_cov_pct"])
            self.assertEqual(len(summaries), 4)
            comparisons = compare(
                rows,
                ["edge_cov_pct"],
                ["current/hybrid,legacy/hybrid"],
            )
            self.assertEqual(len(comparisons), 1)
            self.assertEqual(comparisons[0]["treatment_n"], 20)
            self.assertEqual(comparisons[0]["baseline_n"], 20)
            self.assertAlmostEqual(
                comparisons[0]["mean_delta"], 2.0
            )
            self.assertGreater(comparisons[0]["a12"], 0.5)

            svg = root / "distribution.svg"
            render_svg(rows, svg)
            document = ElementTree.parse(svg)
            self.assertTrue(document.getroot().tag.endswith("svg"))

    def test_benchmark_report_persists_contribution_breakdown(self):
        specification = importlib.util.spec_from_file_location(
            "run_benchmark", ROOT / "benchmark" / "run_benchmark.py"
        )
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        self.assertEqual(module._parse_symcc_interesting(
            "[Master] 3 interesting / 10 generated\n"
            "[Master] 8 interesting / 20 generated\n"
            "[Master] Final stats: 4 ok, 0 failed, "
            "11 interesting / 30 total\n"
        ), 11)
        self.assertEqual(
            module._parse_auxiliary_compute_slots(
                "[Master] Auxiliary compute slots: 7 "
                '({"coverage": 1, "query": 6})\n'
            ),
            7,
        )
        self.assertIsNone(module._parse_auxiliary_compute_slots("no ledger"))
        self.assertIsNone(module._parse_auxiliary_compute_slots(
            "[Master] Auxiliary compute slots: 4097\n"
        ))
        self.assertIsNone(module._parse_auxiliary_compute_slots(
            "[Master] Auxiliary compute slots: " + "9" * 100_000 + "\n"
        ))
        self.assertIsNone(module._parse_auxiliary_compute_slots(
            "[Master] Auxiliary compute slots: seven\n"
        ))
        result = {
            "target": "parser",
            "mode": "hybrid",
            "np": 8,
            "round": 1,
            "wall_time": 1.0,
            "generated": 13,
            "generated_kind": "afl-executions-plus-symcc-candidates",
            "afl_generated": 8,
            "afl_executions": 8,
            "symcc_generated": 5,
            "symcc_interesting": 3,
            "symcc_peer_published": 3,
            "symcc_peer_scanned": 3,
            "symcc_peer_imported": 2,
            "symcc_peer_not_retained": 1,
            "symcc_peer_sync_complete": 1,
            "afl_master_corpus_imported": 2,
            "afl_corpus_imported": 2,
            "afl_master_sync_time": 42,
            "afl_sync_time_minutes": 1,
            "unique": 13,
            "throughput": 13.0,
            "throughput_kind": (
                "afl-executions-plus-symcc-candidates-per-second"),
            "coverage_sampled": True,
            "coverage_sampled_cases": 13,
            "coverage_total_cases": 21,
            "auxiliary_compute_slots": 7,
        }
        with tempfile.TemporaryDirectory() as temporary:
            module.generate_report([result], temporary)
            with (Path(temporary) / "benchmark_data.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                records = list(csv.reader(stream))
            self.assertEqual(len(records), 2)
            self.assertEqual(len(records[0]), len(records[1]))
            row = dict(zip(records[0], records[1], strict=True))
            self.assertEqual(row["afl_generated"], "8")
            self.assertEqual(row["afl_executions"], "8")
            self.assertEqual(
                row["generated_kind"],
                "afl-executions-plus-symcc-candidates",
            )
            self.assertEqual(row["symcc_generated"], "5")
            self.assertEqual(row["symcc_interesting"], "3")
            self.assertEqual(row["symcc_peer_published"], "3")
            self.assertEqual(row["symcc_peer_scanned"], "3")
            self.assertEqual(row["symcc_peer_imported"], "2")
            self.assertEqual(row["symcc_peer_not_retained"], "1")
            self.assertEqual(row["symcc_peer_sync_complete"], "1")
            self.assertEqual(row["afl_master_corpus_imported"], "2")
            self.assertEqual(row["afl_corpus_imported"], "2")
            self.assertEqual(row["afl_master_sync_time"], "42")
            self.assertEqual(row["afl_sync_time_minutes"], "1")
            self.assertEqual(row["coverage_sampled"], "1")
            self.assertEqual(row["coverage_sampled_cases"], "13")
            self.assertEqual(row["coverage_total_cases"], "21")
            self.assertEqual(row["auxiliary_compute_slots"], "7")

    def test_report_does_not_compare_incompatible_throughput_units(self):
        specification = importlib.util.spec_from_file_location(
            "run_benchmark_units", ROOT / "benchmark" / "run_benchmark.py"
        )
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        common = {
            "target": "parser", "round": 1, "wall_time": 1.0,
            "unique": 2, "edge_cov": 10.0,
        }
        rows = [
            {
                **common, "mode": "serial", "np": 1, "generated": 10,
                "symcc_generated": 10, "throughput": 10.0,
                "throughput_kind": "symcc-candidates-per-second",
            },
            {
                **common, "mode": "afl-only", "np": 1,
                "generated": 1000, "afl_executions": 1000,
                "afl_execs_done": 1000, "throughput": 1000.0,
                "throughput_kind": "afl-executions-per-second",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(module.generate_report(rows, temporary)).read_text(
                encoding="utf-8"
            )
        self.assertIn("different unit: afl-executions-per-second", report)
        self.assertNotIn("100.0x", report)


if __name__ == "__main__":
    unittest.main()
