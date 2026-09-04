#!/usr/bin/env python3
# RUN: python3 %s

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qf_bv_coverage_join import (  # noqa: E402
    CAMPAIGN_SCHEMA,
    campaign_digest,
    campaign_semantic_digest,
    replay_coverage_join,
    run_coverage_join,
    seal_coverage_target,
    verify_coverage_join,
    verify_coverage_target,
)


REAL_AFL = all(
    shutil.which(command)
    for command in ("cc", "afl-clang-fast", "afl-showmap")
)


@unittest.skipUnless(REAL_AFL, "C compiler and AFL++ tools are required")
class QfBvCoverageJoinTest(unittest.TestCase):
    temporary = None
    strategy = None
    target = None
    campaign = None

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        output = Path(cls.temporary.name)
        runtime = output / "libafl_data_coverage_rt.so"
        target = output / "qfbv_coverage_smoke_afl"
        subprocess.run(
            [
                shutil.which("cc"), "-O2", "-shared", "-fPIC",
                str(ROOT / "util" / "afl_data_coverage_rt.c"),
                "-o", str(runtime), "-ldl",
            ],
            check=True,
        )
        subprocess.run(
            [
                shutil.which("afl-clang-fast"), "-O0", "-g",
                "-fno-builtin",
                str(ROOT / "benchmark" / "qfbv_coverage_smoke.c"),
                "-o", str(target),
            ],
            check=True,
        )
        cls.strategy = json.loads((
            ROOT / "benchmark" / "evidence"
            / "qfbv_strategy_f246_smoke.json"
        ).read_text(encoding="ascii"))
        cls.target = seal_coverage_target(
            [str(target), "@@"],
            data_preload=str(runtime),
            timeout_ms=1000,
        )
        cls.campaign = run_coverage_join(
            cls.strategy, cls.target, map_repetitions=2)

    @classmethod
    def tearDownClass(cls):
        if cls.temporary is not None:
            cls.temporary.cleanup()

    def _rehash(self, campaign):
        campaign["semantic_sha256"] = campaign_semantic_digest(campaign)
        campaign["campaign_sha256"] = campaign_digest(campaign)

    def _rehash_features(self, mode):
        mode["features_sha256"] = hashlib.sha256(
            json.dumps(
                mode["features"],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()

    def test_real_paired_edge_data_join_is_verified(self):
        campaign = self.campaign
        self.assertEqual(campaign["schema"], CAMPAIGN_SCHEMA)
        self.assertTrue(verify_coverage_join(campaign))
        self.assertEqual(len(campaign["coverage_rows"]), 24)
        self.assertEqual(len(campaign["observations"]), 5)
        strategies = campaign["aggregate"]["strategies"]
        self.assertEqual(strategies["individual:z3-4.8.12"][
            "data_signal_union_gain"], 1)
        self.assertGreater(strategies["individual:z3-4.8.12"][
            "edge_union_gain"], 0)
        for observation in campaign["observations"]:
            edge = {item[0] for item in observation["edge"]["features"]}
            combined = {
                item[0] for item in observation["combined"]["features"]}
            self.assertTrue(edge.issubset(combined))
            self.assertTrue(all(
                identifier < 65536 for identifier in combined - edge))

    def test_rehashed_coverage_and_namespace_tampering_are_rejected(self):
        changed_row = copy.deepcopy(self.campaign)
        changed_row["coverage_rows"][0]["edge_new_features"] += 1
        self._rehash(changed_row)
        self.assertFalse(verify_coverage_join(changed_row))

        changed_map = copy.deepcopy(self.campaign)
        observation = changed_map["observations"][0]
        edge_feature = observation["edge"]["features"][0]
        observation["combined"]["features"].remove(edge_feature)
        self._rehash_features(observation["combined"])
        self._rehash(changed_map)
        self.assertFalse(verify_coverage_join(changed_map))

        changed_namespace = copy.deepcopy(self.campaign)
        combined = changed_namespace["observations"][0]["combined"]
        combined["features"].append([70000, 1])
        combined["features"].sort()
        self._rehash_features(combined)
        self._rehash(changed_namespace)
        self.assertFalse(verify_coverage_join(changed_namespace))

    def test_target_identity_drift_and_synthetic_confirmatory_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "confirmatory strategy"):
            run_coverage_join(
                self.strategy,
                self.target,
                map_repetitions=2,
                confirmatory=True,
            )

        target = copy.deepcopy(self.target)
        self.assertTrue(verify_coverage_target(target, check_current=True))
        with open(target["target_executable"], "ab") as stream:
            stream.write(b"\0")
        self.assertFalse(verify_coverage_target(target, check_current=True))

    def test_real_coverage_join_semantically_replays(self):
        replay = replay_coverage_join(self.campaign)
        self.assertTrue(replay["semantic_match"])
        self.assertTrue(verify_coverage_join(replay["replay"]))


if __name__ == "__main__":
    unittest.main()
