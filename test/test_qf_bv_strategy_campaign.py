#!/usr/bin/env python3
# RUN: python3 %s

import copy
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qf_bv_campaign import (  # noqa: E402
    build_smoke_corpus,
    seal_corpus,
)
from qf_bv_conformance import run_qfbv_conformance  # noqa: E402
from qf_bv_strategy_campaign import (  # noqa: E402
    CAMPAIGN_SCHEMA,
    aggregate_strategy_results,
    build_smoke_policy_bundle,
    campaign_semantic_digest,
    campaign_strategy_digest,
    policy_bundle_digest,
    replay_strategy_campaign,
    run_strategy_campaign,
    verify_policy_bundle,
    verify_strategy_campaign,
)


REAL_SOLVERS = all(
    shutil.which(command)
    for command in ("z3", "cvc5", "bitwuzla")
)


@unittest.skipUnless(REAL_SOLVERS, "three QF_BV CLIs are required")
class QfBvStrategyCampaignTest(unittest.TestCase):
    corpus = None
    conformance = None
    bundle = None
    campaign = None

    @classmethod
    def setUpClass(cls):
        cls.conformance = run_qfbv_conformance()
        cls.corpus = seal_corpus(
            build_smoke_corpus(),
            split_seed="f245-smoke",
            split_parts=2,
            holdout_parts=1,
            source_kind="synthetic-smoke",
        )
        names = sorted(
            entry["name"] for entry in cls.conformance["backends"])
        cls.bundle = build_smoke_policy_bundle(
            cls.corpus, names, timeout_ms=2000)
        cls.campaign = run_strategy_campaign(
            cls.corpus,
            cls.conformance,
            cls.bundle,
            timeout_ms=2000,
            cancel_grace_ms=0,
        )

    def test_policy_bundle_rebuilds_and_rejects_holdout_leakage(self):
        assert self.corpus is not None
        bundle = self.bundle
        assert bundle is not None
        self.assertTrue(verify_policy_bundle(bundle, self.corpus))
        holdout_id = next(
            row["query_id"] for row in self.corpus["queries"]
            if row["split"] == "holdout"
        )
        tampered = copy.deepcopy(bundle)
        tampered["training_events"][0]["context"]["query_id"] = holdout_id
        tampered["training_query_ids"] = sorted({
            event["context"]["query_id"]
            for event in tampered["training_events"]
        })
        tampered["bundle_sha256"] = policy_bundle_digest(tampered)
        self.assertFalse(verify_policy_bundle(tampered, self.corpus))

        changed_reward = copy.deepcopy(bundle)
        changed_reward["training_events"][0]["reward"] = 0.25
        changed_reward["bundle_sha256"] = policy_bundle_digest(changed_reward)
        self.assertFalse(verify_policy_bundle(
            changed_reward, self.corpus))

    def test_real_six_arm_campaign_is_verified(self):
        campaign = self.campaign
        assert campaign is not None
        self.assertEqual(campaign["schema"], CAMPAIGN_SCHEMA)
        self.assertTrue(verify_strategy_campaign(campaign))
        self.assertEqual(len(campaign["strategies"]), 6)
        self.assertEqual(len(campaign["results"]), 24)
        names = [strategy["name"] for strategy in campaign["strategies"]]
        self.assertEqual(
            campaign["aggregate"],
            aggregate_strategy_results(campaign["results"], names),
        )
        for row in campaign["results"]:
            if row["kind"] == "sequence":
                self.assertLessEqual(
                    sum(stage["budget_ms"]
                        for stage in row["planned_schedule"]),
                    2000,
                )
                self.assertEqual(
                    [
                        {
                            "backend": attempt["backend"],
                            "budget_ms": attempt["budget_ms"],
                        }
                        for attempt in row["executed_attempts"]
                    ],
                    row["planned_schedule"][
                        :len(row["executed_attempts"])],
                )
        parallel = campaign["aggregate"]["strategies"][
            "f242-parallel-grace-0ms"
        ]
        self.assertEqual(parallel["attempts"], 12)
        self.assertGreaterEqual(parallel["child_cpu_us"], 0)

    def test_rehashed_schedule_and_winner_tampering_are_rejected(self):
        campaign = copy.deepcopy(self.campaign)
        assert campaign is not None
        sequence = next(
            row for row in campaign["results"]
            if row["kind"] == "sequence"
        )
        sequence["planned_schedule"][0]["backend"] = "z3-4.8.12"
        campaign["semantic_sha256"] = campaign_semantic_digest(campaign)
        campaign["campaign_sha256"] = campaign_strategy_digest(campaign)
        self.assertFalse(verify_strategy_campaign(campaign))

        winner_tamper = copy.deepcopy(self.campaign)
        solved = next(
            row for row in winner_tamper["results"]
            if row["status"] in {"sat", "unsat"}
        )
        solved["winner_backend"] = "not-the-winner"
        winner_tamper["campaign_sha256"] = campaign_strategy_digest(
            winner_tamper)
        self.assertFalse(verify_strategy_campaign(winner_tamper))

    def test_rehashed_invalid_outer_model_is_rejected(self):
        campaign = copy.deepcopy(self.campaign)
        assert campaign is not None
        assert self.corpus is not None
        ult_query_id = next(
            row["query_id"] for row in self.corpus["queries"]
            if row["envelope"]["metadata"]["source"].endswith("-ult")
            and row["split"] == "holdout"
        )
        sat = next(
            row for row in campaign["results"]
            if row["query_id"] == ult_query_id and row["status"] == "sat"
        )
        sat["assignments"] = {"0": 0x10}
        campaign["campaign_sha256"] = campaign_strategy_digest(campaign)
        self.assertFalse(verify_strategy_campaign(campaign))

    def test_real_strategy_campaign_semantically_replays(self):
        campaign = self.campaign
        assert campaign is not None
        replay = replay_strategy_campaign(campaign)
        self.assertTrue(replay["semantic_match"])
        self.assertTrue(verify_strategy_campaign(replay["replay"]))


if __name__ == "__main__":
    unittest.main()
