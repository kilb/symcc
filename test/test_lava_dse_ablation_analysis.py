# RUN: python3 %s

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from analyze_lava_dse_ablation import load_cases, summarize  # noqa: E402


class LavaDseAblationAnalysisTests(unittest.TestCase):
    def test_listed_and_extra_hits_are_separated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for profile, listed, extra in [
                ("strict-z3", [1, 2], [999]),
                ("runtime-full", [2, 3], [999, 1000]),
            ]:
                run = root / profile / "seed"
                run.mkdir(parents=True)
                (run / "lava_case_result.json").write_text(json.dumps({
                    "program": "base64",
                    "profile": profile,
                    "generated": 10,
                    "unique_generated": 8,
                    "elapsed_seconds": 1.0,
                    "timed_out": False,
                    "strategy_outputs": {"strict": 10},
                    "telemetry": {"z3_solves": 4, "fast_solves": 1},
                    "lava_bug_replay": {
                        "listed_hit_count": len(listed),
                        "listed_total": 44,
                        "listed_hits": listed,
                        "extra_hits": extra,
                    },
                }))
            rows = summarize(load_cases(root))
        by_profile = {row["profile"]: row for row in rows}
        self.assertEqual(by_profile["strict-z3"]["listed_union_count"], 2)
        self.assertEqual(by_profile["strict-z3"]["extra_union"], [999])
        self.assertEqual(by_profile["runtime-full"]["listed_union_count"], 2)
        self.assertEqual(by_profile["runtime-full"]["extra_union"], [999, 1000])


if __name__ == "__main__":
    unittest.main()
