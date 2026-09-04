#!/usr/bin/env python3
# RUN: python3 %s

import copy
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qf_bv_campaign import (  # noqa: E402
    CAMPAIGN_SCHEMA,
    aggregate_results,
    build_smoke_corpus,
    campaign_digest,
    campaign_semantic_digest,
    corpus_digest,
    replay_campaign,
    run_campaign,
    seal_corpus,
    verify_campaign,
    verify_corpus,
)
from qf_bv_conformance import (  # noqa: E402
    run_qfbv_conformance,
)


REAL_SOLVERS = all(
    shutil.which(command)
    for command in ("z3", "cvc5", "bitwuzla")
)


class QfBvCampaignTest(unittest.TestCase):
    real_conformance = None
    real_campaign = None

    @classmethod
    def setUpClass(cls):
        if not REAL_SOLVERS:
            return
        cls.real_conformance = run_qfbv_conformance()
        corpus = seal_corpus(
            build_smoke_corpus(),
            split_seed="f245-smoke",
            split_parts=2,
            holdout_parts=1,
            source_kind="synthetic-smoke",
        )
        cls.real_campaign = run_campaign(
            corpus,
            cls.real_conformance,
            timeout_ms=1000,
        )

    def test_corpus_split_is_deterministic_and_tamper_evident(self):
        first = seal_corpus(
            build_smoke_corpus(),
            split_seed="f245-smoke",
            split_parts=2,
            holdout_parts=1,
            source_kind="synthetic-smoke",
        )
        second = seal_corpus(
            build_smoke_corpus(),
            split_seed="f245-smoke",
            split_parts=2,
            holdout_parts=1,
            source_kind="synthetic-smoke",
        )
        self.assertTrue(verify_corpus(first))
        self.assertEqual(first, second)
        self.assertEqual(first["train_count"], 4)
        self.assertEqual(first["holdout_count"], 4)

        tampered = copy.deepcopy(first)
        tampered["queries"][0]["split"] = "train"
        tampered["corpus_sha256"] = corpus_digest(tampered)
        self.assertFalse(verify_corpus(tampered))

    def test_confirmatory_mode_requires_real_disjoint_repetitions(self):
        corpus = seal_corpus(
            build_smoke_corpus(),
            split_seed="f245-smoke",
            split_parts=2,
            holdout_parts=1,
            source_kind="synthetic-smoke",
        )
        with self.assertRaisesRegex(ValueError, "at least 20"):
            run_campaign(
                corpus,
                {},
                repetitions=19,
                confirmatory=True,
            )

    @unittest.skipUnless(REAL_SOLVERS, "three QF_BV CLIs are required")
    def test_real_three_solver_campaign_is_verified(self):
        campaign = self.real_campaign
        assert campaign is not None
        self.assertEqual(campaign["schema"], CAMPAIGN_SCHEMA)
        self.assertTrue(verify_campaign(campaign))
        self.assertEqual(len(campaign["results"]), 12)
        self.assertEqual(
            campaign["aggregate"],
            aggregate_results(
                campaign["results"],
                campaign["backend_names"],
            ),
        )
        self.assertEqual(campaign["aggregate"]["any_backend_solved"], 4)
        for backend in campaign["aggregate"]["backends"].values():
            self.assertEqual(backend["sat"], 3)
            self.assertEqual(backend["unsat"], 1)
            self.assertEqual(backend["solved"], 4)
        self.assertEqual(
            campaign["aggregate"]["backends"]["z3-4.8.12"][
                "status_only_confirmations"
            ],
            1,
        )

    @unittest.skipUnless(REAL_SOLVERS, "three QF_BV CLIs are required")
    def test_inner_model_tamper_is_rejected_after_rehash(self):
        campaign = copy.deepcopy(self.real_campaign)
        assert campaign is not None
        ult_query_id = next(
            row["query_id"]
            for row in campaign["corpus"]["queries"]
            if row["envelope"]["metadata"]["source"].endswith("-ult")
            and row["split"] == "holdout"
        )
        sat = next(
            row for row in campaign["results"]
            if row["status"] == "sat" and row["query_id"] == ult_query_id
        )
        sat["assignments"] = {"0": 0x10}
        campaign["aggregate"] = aggregate_results(
            campaign["results"],
            campaign["backend_names"],
        )
        campaign["semantic_sha256"] = campaign_semantic_digest(campaign)
        campaign["campaign_sha256"] = campaign_digest(campaign)
        self.assertFalse(verify_campaign(campaign))

    @unittest.skipUnless(REAL_SOLVERS, "three QF_BV CLIs are required")
    def test_real_campaign_semantically_replays(self):
        campaign = self.real_campaign
        assert campaign is not None
        replay = replay_campaign(campaign)
        self.assertTrue(replay["semantic_match"])
        self.assertTrue(verify_campaign(replay["replay"]))


if __name__ == "__main__":
    unittest.main()
