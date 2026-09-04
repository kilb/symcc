# RUN: python3 %s

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

import mpi_fuzzing_helper as mpi_helper  # noqa: E402
from topseed_selector import (  # noqa: E402
    TOPSEED_POLICIES,
    TopSeedSelector,
)


def identity(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class TopSeedSelectorTests(unittest.TestCase):
    def selector(self, **overrides) -> TopSeedSelector:
        arguments = {
            "program_context": identity("program"),
            "seed": 17,
            "explore_ratio": 1.0,
            "learn_interval": 2,
            "max_candidates": 16,
            "max_runs": 16,
            "max_features": 64,
        }
        arguments.update(overrides)
        return TopSeedSelector(**arguments)

    def test_bitmap_features_preserve_bucket_bits(self) -> None:
        dense = TopSeedSelector.coverage_features_from_bitmap(
            bytes([0b00000101, 0, 0b10000000])
        )
        sparse = TopSeedSelector.coverage_features_from_bitmap(
            [(2, 0b10000000), (0, 0b00000101)]
        )
        self.assertEqual(dense, (0, 2, 23))
        self.assertEqual(sparse, dense)
        self.assertEqual(
            TopSeedSelector.coverage_features_from_bitmap(
                bytes([0xFF, 0xFF]), maximum=3
            ),
            (0, 1, 2),
        )

    def test_branch_trace_tokens_are_bounded_and_stable(self) -> None:
        trace = [
            (0, 0, 0, 11, False, False),
            (0, 0, 0, 11, True, False),
            (0, 0, 0, 2, True, False),
            ("short",),
        ]
        self.assertEqual(
            TopSeedSelector.path_condition_from_branch_trace(trace),
            (5, 22, 23),
        )
        self.assertEqual(
            len(TopSeedSelector.path_condition_from_branch_trace(
                trace, maximum=2
            )),
            2,
        )
        high = TopSeedSelector.path_condition_from_branch_trace([
            (0, 0, 0, (1 << 64) - 1, True, False)
        ])
        self.assertEqual(len(high), 1)
        self.assertGreaterEqual(high[0], 0)
        self.assertLess(high[0], 1 << 63)

    def test_explore_groups_by_exact_coverage_and_uses_five_features(self) -> None:
        selector = self.selector()
        first = identity("first")
        second = identity("second")
        third = identity("third")
        self.assertTrue(selector.admit(
            first, "/seed/first", [1, 2], path_condition=[1]
        ))
        self.assertTrue(selector.admit(
            second, "/seed/second", [1, 2], path_condition=[1, 2, 3]
        ))
        self.assertTrue(selector.admit(
            third, "/seed/third", [3], path_condition=[7, 8]
        ))
        with (
            mock.patch.object(
                selector, "_sample_weight", return_value=(1, 0, 0, 0, 1)
            ),
            mock.patch.object(selector, "_sample_policy", return_value="long"),
        ):
            proposal = selector.propose([
                "/seed/first", "/seed/second", "/seed/third"
            ])
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal.mode, "explore")
        self.assertEqual(proposal.candidate_id, second)
        self.assertEqual(
            selector._group_key(selector.candidates[first].coverage),
            selector._group_key(selector.candidates[second].coverage),
        )

    def test_all_four_in_group_policies(self) -> None:
        selector = self.selector()
        candidates = []
        for name, condition in (
            ("a", [1, 2]),
            ("b", [2]),
            ("c", [3, 4, 5]),
        ):
            candidate = identity(name)
            selector.admit(candidate, f"/seed/{name}", [9], path_condition=condition)
            candidates.append(candidate)
        self.assertEqual(selector._choose_candidate(candidates, "long"), identity("c"))
        self.assertEqual(selector._choose_candidate(candidates, "short"), identity("b"))
        self.assertEqual(selector._choose_candidate(candidates, "unique"), identity("c"))
        with mock.patch.object(selector.random, "randrange", return_value=1):
            self.assertEqual(
                selector._choose_candidate(candidates, "random"),
                sorted(candidates)[1],
            )

    def test_commit_boundary_and_stale_observation(self) -> None:
        selector = self.selector()
        candidate = identity("one")
        selector.admit(candidate, "/seed/one", [1])
        proposal = selector.propose(["/seed/one"])
        assert proposal is not None
        self.assertEqual(selector.selections, 0)
        self.assertTrue(selector.discard(proposal.token))
        with self.assertRaisesRegex(ValueError, "missing"):
            selector.commit(proposal.token)

        proposal = selector.propose(["/seed/one"])
        assert proposal is not None
        run = selector.commit(proposal.token)
        self.assertEqual(selector.selections, 1)
        self.assertTrue(selector.observe(run, [10, 11], path_condition=[4]))
        self.assertFalse(selector.observe(run, [12]))
        self.assertEqual(selector.candidates[candidate].generated_coverage, (10, 11))

    def test_exploit_clusters_seed_yield_by_inverse_frequency(self) -> None:
        selector = self.selector(explore_ratio=0.0)
        ids = [identity(name) for name in ("a", "b", "c")]
        for index, candidate in enumerate(ids):
            selector.admit(candidate, f"/seed/{index}", [index + 1])
            selector.candidates[candidate].uses = 1
            selector.used_groups.add(selector._group_key((index + 1,)))
        selector.candidates[ids[0]].generated_coverage = (10, 11)
        selector.candidates[ids[1]].generated_coverage = (10,)
        selector.candidates[ids[2]].generated_coverage = (10,)
        with mock.patch.object(selector, "_sample_policy", return_value="long"):
            proposal = selector.propose([f"/seed/{index}" for index in range(3)])
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal.mode, "exploit")
        self.assertEqual(proposal.candidate_id, ids[0])

    def test_learning_updates_weight_and_policy_distributions(self) -> None:
        selector = self.selector(learn_interval=2)
        ids = [identity("good"), identity("bad")]
        for index, candidate in enumerate(ids):
            selector.admit(candidate, f"/seed/{index}", [index + 1])

        with (
            mock.patch.object(
                selector, "_sample_weight", return_value=(0.8,) * 5
            ),
            mock.patch.object(selector, "_sample_policy", return_value="unique"),
        ):
            good = selector.propose(["/seed/0", "/seed/1"])
        assert good is not None
        selector.commit(good.token)
        selector.observe(good.token, [100, 101])

        with (
            mock.patch.object(
                selector, "_sample_weight", return_value=(-0.8,) * 5
            ),
            mock.patch.object(selector, "_sample_policy", return_value="random"),
        ):
            bad = selector.propose(["/seed/0", "/seed/1"])
        assert bad is not None
        selector.commit(bad.token)
        selector.observe(bad.token, [100])

        self.assertEqual(selector.learning_rounds, 1)
        self.assertTrue(all(
            distribution[0] == "truncated-normal"
            for distribution in selector.weight_distributions
        ))
        self.assertEqual(selector.policy_probabilities, [0.25] * 4)

        for policy, generated in (("long", [102, 103]), ("short", [102])):
            with (
                mock.patch.object(
                    selector, "_sample_weight", return_value=(0.5,) * 5
                ),
                mock.patch.object(selector, "_sample_policy", return_value=policy),
            ):
                proposal = selector.propose(["/seed/0", "/seed/1"])
            assert proposal is not None
            selector.commit(proposal.token)
            selector.observe(proposal.token, generated)

        self.assertEqual(selector.learning_rounds, 2)
        probabilities = dict(zip(TOPSEED_POLICIES, selector.policy_probabilities))
        self.assertGreater(probabilities["unique"], probabilities["random"])
        self.assertGreater(probabilities["long"], probabilities["short"])

    def test_snapshot_roundtrip_preserves_next_random_proposal(self) -> None:
        selector = self.selector()
        for index in range(3):
            selector.admit(
                identity(str(index)), f"/seed/{index}", [index + 1],
                path_condition=range(index + 1),
            )
        first = selector.propose(["/seed/0", "/seed/1", "/seed/2"])
        assert first is not None
        selector.commit(first.token)
        selector.observe(first.token, [20])
        restored = TopSeedSelector.from_snapshot(selector.snapshot())
        self.assertEqual(restored.snapshot(), selector.snapshot())
        available = ["/seed/0", "/seed/1", "/seed/2"]
        self.assertEqual(restored.propose(available), selector.propose(available))

    def test_pending_run_can_be_retired_after_restart(self) -> None:
        selector = self.selector()
        selector.admit(identity("x"), "/seed/x", [1])
        proposal = selector.propose(["/seed/x"])
        assert proposal is not None
        selector.commit(proposal.token)
        restored = TopSeedSelector.from_snapshot(selector.snapshot())
        self.assertEqual(restored.fail_pending_runs(), 1)
        self.assertFalse(restored.observe(proposal.token, [2]))
        self.assertFalse(restored.observe(identity("stale"), [2]))
        self.assertTrue(restored.runs[proposal.token].failed)

    def test_atomic_file_roundtrip_and_context_binding(self) -> None:
        selector = self.selector()
        selector.admit(identity("x"), "/seed/x", [1])
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "selector.json")
            selector.save(path)
            restored = TopSeedSelector.load(path)
            self.assertEqual(restored.snapshot(), selector.snapshot())
            stale = os.path.join(
                temporary, f".selector.json.{os.getpid()}.tmp"
            )
            with open(stale, "wb") as stream:
                stream.write(b"interrupted publication")
            selector.save(path)
            self.assertEqual(
                TopSeedSelector.load(path).snapshot(), selector.snapshot()
            )
            self.assertTrue(os.path.exists(stale))
            raw = restored.snapshot()
            raw["program_context"] = identity("other")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(raw, stream)
            changed = TopSeedSelector.load(path)
            self.assertEqual(changed.program_context, identity("other"))

    def test_snapshot_rejects_corruption(self) -> None:
        selector = self.selector()
        selector.admit(identity("x"), "/seed/x", [1])
        cases = []
        wrong_probability = selector.snapshot()
        wrong_probability["policy_probabilities"] = [0.5] * 4
        cases.append(wrong_probability)
        empty_coverage = selector.snapshot()
        empty_coverage["candidates"][0]["coverage"] = []
        cases.append(empty_coverage)
        duplicate = selector.snapshot()
        duplicate["candidates"].append(dict(duplicate["candidates"][0]))
        cases.append(duplicate)
        noncanonical = selector.snapshot()
        noncanonical["weight_distributions"][0] = ["uniform", 0.1, 0]
        cases.append(noncanonical)
        extra = selector.snapshot()
        extra["unexpected"] = True
        cases.append(extra)
        unsorted = selector.snapshot()
        unsorted["candidates"][0]["coverage"] = [2, 1]
        cases.append(unsorted)
        string_boolean = selector.snapshot()
        string_boolean["candidates"][0]["triggers_bug"] = "false"
        cases.append(string_boolean)
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    TopSeedSelector.from_snapshot(case)

    def test_snapshot_load_rejects_symbolic_link(self) -> None:
        selector = self.selector()
        with tempfile.TemporaryDirectory() as temporary:
            target = os.path.join(temporary, "target.json")
            link = os.path.join(temporary, "link.json")
            selector.save(target)
            os.symlink(target, link)
            with self.assertRaisesRegex(ValueError, "regular file"):
                TopSeedSelector.load(link)

    def test_conflicting_coverage_is_rejected_without_mutation(self) -> None:
        selector = self.selector()
        candidate = identity("same")
        self.assertTrue(selector.admit(candidate, "/seed/a", [1]))
        before = selector.snapshot()
        self.assertFalse(selector.admit(candidate, "/seed/b", [2]))
        self.assertEqual(selector.admission_conflicts, 1)
        after = selector.snapshot()
        before["admission_conflicts"] = 1
        self.assertEqual(after, before)

    def test_candidate_and_run_caps_only_evict_safe_history(self) -> None:
        selector = self.selector(max_candidates=2, max_runs=2)
        first = identity("first")
        second = identity("second")
        third = identity("third")
        selector.admit(first, "/seed/first", [1])
        proposal = selector.propose(["/seed/first"])
        assert proposal is not None
        selector.commit(proposal.token)
        selector.observe(proposal.token, [10])
        selector.admit(second, "/seed/second", [2])
        self.assertTrue(selector.admit(third, "/seed/third", [3]))
        self.assertIn(first, selector.candidates)
        self.assertNotIn(second, selector.candidates)
        self.assertIn(third, selector.candidates)
        self.assertEqual(selector.evicted_candidates, 1)

        saturated = self.selector(max_candidates=4, max_runs=2)
        saturated_paths = []
        for index in range(3):
            path = f"/seed/saturated-{index}"
            saturated.admit(identity(path), path, [index + 1])
            saturated_paths.append(path)
        first_proposal = saturated.propose([saturated_paths[0]])
        assert first_proposal is not None
        saturated.commit(first_proposal.token)
        second_proposal = saturated.propose([saturated_paths[1]])
        third_proposal = saturated.propose([saturated_paths[2]])
        assert second_proposal is not None
        assert third_proposal is not None
        saturated.commit(second_proposal.token)
        with self.assertRaisesRegex(ValueError, "no evictable entry"):
            saturated.commit(third_proposal.token)
        self.assertTrue(saturated.discard(third_proposal.token))
        self.assertFalse(saturated.pending)
        self.assertEqual(saturated.selections, 2)
        self.assertIsNone(saturated.propose(["/seed/saturated-2"]))
        self.assertEqual(saturated.dropped_observations, 1)

    def test_independent_rarity_oracle_matches_cluster_preference(self) -> None:
        coverage_sets = [{1, 2, 3}, {1, 2}, {1}, {4}]
        expected = []
        for coverage in coverage_sets:
            score = 0.0
            for branch in coverage:
                score += 1.0 / sum(branch in other for other in coverage_sets)
            expected.append(score)
        self.assertEqual(TopSeedSelector._rarity_scores(coverage_sets), expected)
        high = TopSeedSelector._high_cluster(expected)
        self.assertIn(0, high)
        self.assertNotIn(2, high)

    def test_mpi_triage_reports_fenced_generated_coverage(self) -> None:
        observations = []
        with tempfile.TemporaryDirectory() as temporary:
            root = os.path.abspath(temporary)
            queue = os.path.join(root, "queue")
            crashes = os.path.join(root, "crashes")
            hangs = os.path.join(root, "hangs")
            os.makedirs(queue)
            os.makedirs(crashes)
            os.makedirs(hangs)
            source = os.path.join(root, "id:000001,orig:seed")
            with open(source, "wb") as stream:
                stream.write(b"seed")
            mpi_helper._batch_triage(
                [(
                    3,
                    source,
                    [{"content": b"next", "bitmap": [(7, 0b101)]}],
                    139,
                    0.1,
                    False,
                    0,
                    None,
                    (),
                    "",
                    {},
                )],
                mpi_helper.Stats(),
                mpi_helper.CoverageBitmap(),
                object(),
                queue,
                crashes,
                hangs,
                None,
                None,
                root,
                os.path.join(root, ".triage"),
                [],
                [0],
                topseed_observation_callback=lambda *args: observations.append(args),
            )
        self.assertEqual(len(observations), 1)
        worker, observed_path, features, telemetry, killed, retcode = observations[0]
        self.assertEqual(worker, 3)
        self.assertEqual(observed_path, source)
        self.assertEqual(features, ((7 << 3), (7 << 3) | 2))
        self.assertIsNone(telemetry)
        self.assertFalse(killed)
        self.assertEqual(retcode, 139)


if __name__ == "__main__":
    unittest.main()
