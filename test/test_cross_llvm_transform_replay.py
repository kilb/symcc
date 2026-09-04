#!/usr/bin/env python3
# RUN: python3 %s

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from cross_llvm_transform_replay import (  # noqa: E402
    EvidenceError,
    canonical_bytes,
    load_candidates,
    verify_certificate_value,
)


def artifact(name, digest):
    return {"name": name, "size": 1, "sha256": digest}


class CrossLlvmTransformReplayTests(unittest.TestCase):
    def certificate(self):
        lowered_digest = "1" * 64
        manifest_digest = "2" * 64
        proof = ["hydra:123"]
        value = {
            "schema": "symcc-cross-llvm-transform-replay-v1",
            "pipeline": "hydra",
            "equivalence":
                "exact-ir-plus-verified-manifest-and-llvm-diff-v1",
            "seal_sha256": "3" * 64,
            "baseline": {
                "llvm_major": 18,
                "llvm_identity": "Ubuntu LLVM version 18.1.3",
                "llvm_tool": artifact("opt", "4" * 64),
                "compiler": artifact("libsymcc.so", "5" * 64),
                "lowered_ir": artifact("lowered.ll", lowered_digest),
                "proof_identities": proof,
                "normalized_manifest_sha256": manifest_digest,
                "sealed_replay_passed": True,
            },
            "candidates": [{
                "label": "llvm17",
                "llvm_major": 17,
                "llvm_identity": "Ubuntu LLVM version 17.0.6",
                "llvm_diff_identity": "Ubuntu LLVM version 17.0.6",
                "llvm_tool": artifact("opt", "6" * 64),
                "llvm_diff": artifact("llvm-diff", "7" * 64),
                "compiler": artifact("libsymcc.so", "8" * 64),
                "lowered_ir": artifact("lowered.ll", lowered_digest),
                "manifests": {
                    "hydra": artifact("hydra.jsonl", "9" * 64),
                },
                "proof_identities": proof,
                "normalized_manifest_sha256": manifest_digest,
                "llvm_verifier_passed": True,
                "llvm_diff_passed": True,
                "exact_lowered_ir_passed": True,
            }],
            "verified_llvm_majors": [17, 18],
            "cross_major_verified": True,
        }
        value["certificate_sha256"] = hashlib.sha256(
            canonical_bytes(value)
        ).hexdigest()
        return value

    @staticmethod
    def rehash(value):
        value.pop("certificate_sha256", None)
        value["certificate_sha256"] = hashlib.sha256(
            canonical_bytes(value)
        ).hexdigest()

    def test_certificate_contract_and_inner_tamper(self):
        certificate = self.certificate()
        self.assertIs(verify_certificate_value(certificate), certificate)

        tampered = copy.deepcopy(certificate)
        tampered["candidates"][0]["lowered_ir"]["sha256"] = "a" * 64
        self.rehash(tampered)
        with self.assertRaisesRegex(
                EvidenceError, "lowered IR identity differs"):
            verify_certificate_value(tampered)

        extra = copy.deepcopy(certificate)
        extra["unreviewed_claim"] = True
        self.rehash(extra)
        with self.assertRaisesRegex(EvidenceError, "fields are not canonical"):
            verify_certificate_value(extra)

        wrong_manifest = copy.deepcopy(certificate)
        wrong_manifest["candidates"][0]["manifests"] = {
            "continuation": artifact("continuation.jsonl", "9" * 64),
        }
        self.rehash(wrong_manifest)
        with self.assertRaisesRegex(EvidenceError, "manifest kinds differ"):
            verify_certificate_value(wrong_manifest)

    def test_candidate_document_is_canonical(self):
        opt = shutil.which("opt")
        llvm_diff = shutil.which("llvm-diff")
        if opt is None or llvm_diff is None:
            self.skipTest("LLVM tools are unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiler = root / "libsymcc.so"
            compiler.write_bytes(b"x")
            document = root / "candidates.json"
            document.write_text(json.dumps({
                "schema": "symcc-cross-llvm-candidates-v1",
                "candidates": [{
                    "label": "local",
                    "opt": opt,
                    "compiler": str(compiler),
                    "llvm_diff": llvm_diff,
                }],
            }), encoding="utf-8")
            candidates = load_candidates(document)
            self.assertEqual(candidates[0]["label"], "local")

            duplicated = json.loads(document.read_text(encoding="utf-8"))
            duplicated["candidates"].append(
                dict(duplicated["candidates"][0])
            )
            document.write_text(json.dumps(duplicated), encoding="utf-8")
            with self.assertRaisesRegex(EvidenceError, "duplicated"):
                load_candidates(document)

    def test_major_claim_cannot_be_relabelled(self):
        certificate = self.certificate()
        certificate["candidates"][0]["llvm_major"] = 16
        certificate["verified_llvm_majors"] = [16, 18]
        self.rehash(certificate)
        with self.assertRaisesRegex(EvidenceError, "identity/major mismatch"):
            verify_certificate_value(certificate)


if __name__ == "__main__":
    unittest.main()
