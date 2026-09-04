# RUN: python3 %s

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from verifier_loop import VerifierInTheLoop  # noqa: E402


class VerifierLoopTests(unittest.TestCase):
    def test_concrete_replay_and_quorum_accept(self):
        loop = VerifierInTheLoop({
            "concrete_replay": lambda _p: {"passed": True, "target_reached": True},
            "semantic": lambda _p: {"passed": True, "certificate": "proof"},
        }, quorum=2)
        decision = loop.verify({"proposal_id": "p", "target_branch": 7})
        self.assertTrue(decision.accepted)
        self.assertEqual(loop.verify({"proposal_id": "p"}), decision)

    def test_failed_concrete_gate_rejects_even_with_other_evidence(self):
        loop = VerifierInTheLoop({
            "concrete_replay": lambda _p: {"passed": False},
            "semantic": lambda _p: {"passed": True},
        }, quorum=1)
        self.assertFalse(loop.verify({"proposal_id": "p", "target_branch": 7}).accepted)

    def test_malformed_validator_is_a_negative_evidence(self):
        loop = VerifierInTheLoop({"concrete_replay": lambda _p: {"passed": "yes"}})
        decision = loop.verify({"proposal_id": "p"})
        self.assertFalse(decision.accepted)
        self.assertEqual(loop.snapshot()["decisions"], 1)

    def test_slow_validator_is_rejected_after_return(self):
        loop = VerifierInTheLoop(
            {"concrete_replay": lambda _p: {"passed": True}},
            timeout_seconds=0.01,
        )
        original = loop.validators["concrete_replay"]
        def slow(proposal):
            import time
            time.sleep(0.02)
            return original(proposal)
        loop.validators["concrete_replay"] = slow
        self.assertFalse(loop.verify({"proposal_id": "slow", "target_branch": 1}).accepted)


if __name__ == "__main__":
    unittest.main()
