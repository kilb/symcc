#!/usr/bin/env python3
# RUN: python3 %s

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))
ROOT = Path(__file__).resolve().parents[1]

from transform_artifact_bundle import (  # noqa: E402
    BundleError,
    atomic_extract,
    canonical_bytes,
    digest_value,
    generate_keypair,
    load_log_records,
    publish_bundle,
    verify_bundle,
)


class TransformArtifactBundleTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sign_private = self.root / "sign-private.pem"
        self.sign_public = self.root / "sign-public.pem"
        self.log_private = self.root / "log-private.pem"
        self.log_public = self.root / "log-public.pem"
        generate_keypair(self.sign_private, self.sign_public)
        generate_keypair(self.log_private, self.log_public)
        self.log = self.root / "transparency.jsonl"
        self.seal, self.artifacts = self._sealed_artifacts()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _identity(path):
        content = path.read_bytes()
        return {
            "name": path.name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def _sealed_artifacts(self):
        contents = {
            "input-ir": ("input.ll", b"define i32 @f() { ret i32 1 }\n"),
            "lowered-ir": (
                "lowered.ll",
                b"define i32 @f() { ret i32 2 }\n",
            ),
            "compiler": ("libsymcc.so", b"compiler-binary"),
            "llvm-tool": ("opt", b"llvm-tool-binary"),
            "manifest:hydra": (
                "hydra.jsonl",
                b'{"schema":"test-manifest"}\n',
            ),
        }
        paths = {}
        for role, (name, content) in contents.items():
            path = self.root / name
            path.write_bytes(content)
            paths[role] = path
        paths["compiler"].chmod(0o755)
        paths["llvm-tool"].chmod(0o755)
        envelope = {
            "schema": "symcc-transformation-seal-v1",
            "pipeline": "hydra",
            "replay_configuration": {
                "passes": "hydra-transform",
                "site": 1,
                "mode": "safe",
            },
            "manifests": [{
                "kind": "hydra",
                "record_count": 1,
                "proof_identities": ["1"],
                "artifact": self._identity(paths["manifest:hydra"]),
            }],
            "input_ir": self._identity(paths["input-ir"]),
            "lowered_ir": self._identity(paths["lowered-ir"]),
            "compiler": self._identity(paths["compiler"]),
            "llvm_tool": self._identity(paths["llvm-tool"]),
            "llvm_identity": "test LLVM",
        }
        envelope["seal_sha256"] = hashlib.sha256(
            canonical_bytes(envelope)).hexdigest()
        seal = self.root / "transform.seal"
        seal.write_bytes(canonical_bytes(envelope) + b"\n")
        return seal, paths

    def _publish(self, name):
        path = self.root / name
        metadata = publish_bundle(
            seal_path=self.seal,
            artifact_arguments=self.artifacts,
            private_key=self.sign_private,
            public_key=self.sign_public,
            log_path=self.log,
            log_private_key=self.log_private,
            log_public_key=self.log_public,
            output_path=path,
        )
        return path, metadata

    def _cli_publish_command(self, output):
        tool = ROOT / "util" / "transform_artifact_bundle.py"
        command = [
            sys.executable,
            str(tool),
            "publish",
            "--seal",
            str(self.seal),
        ]
        for role, path in sorted(self.artifacts.items()):
            command.extend(["--artifact", f"{role}={path}"])
        command.extend([
            "--private-key",
            str(self.sign_private),
            "--public-key",
            str(self.sign_public),
            "--log",
            str(self.log),
            "--log-private-key",
            str(self.log_private),
            "--log-public-key",
            str(self.log_public),
            "--output",
            str(output),
        ])
        return command

    @staticmethod
    def _rewrite(source, destination, mutate_metadata=None,
                 mutate_blobs=None):
        with zipfile.ZipFile(source, "r") as archive:
            members = {
                item.filename: archive.read(item)
                for item in archive.infolist()
            }
        if mutate_metadata is not None:
            metadata = json.loads(members["bundle.json"])
            mutate_metadata(metadata)
            unsigned = dict(metadata)
            unsigned.pop("bundle_sha256", None)
            metadata["bundle_sha256"] = digest_value(unsigned)
            members["bundle.json"] = canonical_bytes(metadata) + b"\n"
        if mutate_blobs is not None:
            mutate_blobs(members)
        with zipfile.ZipFile(
                destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)

    def test_signed_inclusion_and_cross_host_extraction(self):
        first, first_metadata = self._publish("first.bundle.zip")
        second, second_metadata = self._publish("second.bundle.zip")
        self.assertEqual(first_metadata["transparency"]["index"], 0)
        self.assertEqual(second_metadata["transparency"]["index"], 1)
        self.assertEqual(
            len(second_metadata["transparency"]["inclusion_proof"]), 1)

        verified = verify_bundle(
            second,
            public_key=self.sign_public,
            log_public_key=self.log_public,
            log_path=self.log,
        )
        output = self.root / "imported"
        receipt = atomic_extract(second, verified, output)
        self.assertEqual(set(receipt), {"seal", *self.artifacts})
        for role, source in {"seal": self.seal, **self.artifacts}.items():
            self.assertEqual(
                (output / receipt[role]).read_bytes(),
                source.read_bytes(),
            )
        self.assertTrue((output / "receipt.json").is_file())
        with self.assertRaisesRegex(BundleError, "already exists"):
            atomic_extract(second, verified, output)

        records, _ = load_log_records(self.log, self.log_public)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            verify_bundle(
                first,
                public_key=self.sign_public,
                log_public_key=self.log_public,
            )["transparency"]["index"],
            0,
        )

    def test_signature_proof_and_blob_tampering_are_rejected(self):
        self._publish("first.bundle.zip")
        bundle, _ = self._publish("second.bundle.zip")

        signature_tamper = self.root / "signature-tamper.zip"

        def change_signature(metadata):
            signature = bytearray(base64.b64decode(metadata["signature"]))
            signature[0] ^= 1
            metadata["signature"] = base64.b64encode(signature).decode()

        self._rewrite(bundle, signature_tamper, change_signature)
        with self.assertRaises(BundleError):
            verify_bundle(
                signature_tamper,
                public_key=self.sign_public,
                log_public_key=self.log_public,
            )

        proof_tamper = self.root / "proof-tamper.zip"

        def change_proof(metadata):
            proof = metadata["transparency"]["inclusion_proof"]
            proof[0] = ("0" if proof[0][0] != "0" else "1") + proof[0][1:]

        self._rewrite(bundle, proof_tamper, change_proof)
        with self.assertRaisesRegex(BundleError, "proof root mismatch"):
            verify_bundle(
                proof_tamper,
                public_key=self.sign_public,
                log_public_key=self.log_public,
            )

        blob_tamper = self.root / "blob-tamper.zip"

        def change_blob(members):
            name = next(name for name in members if name.startswith("blobs/"))
            members[name] += b"tamper"

        self._rewrite(bundle, blob_tamper, mutate_blobs=change_blob)
        with self.assertRaises(BundleError):
            verify_bundle(
                blob_tamper,
                public_key=self.sign_public,
                log_public_key=self.log_public,
            )

        verified = verify_bundle(
            bundle,
            public_key=self.sign_public,
            log_public_key=self.log_public,
        )
        replaced = self.root / "replacement.zip"
        self._rewrite(bundle, replaced, mutate_blobs=change_blob)
        replaced.replace(bundle)
        with self.assertRaisesRegex(BundleError, "changed before extraction"):
            atomic_extract(
                bundle, verified, self.root / "toctou-import")

    def test_wrong_trust_root_and_log_rewrite_are_rejected(self):
        self._publish("first.bundle.zip")
        bundle, _ = self._publish("second.bundle.zip")
        other_private = self.root / "other-private.pem"
        other_public = self.root / "other-public.pem"
        generate_keypair(other_private, other_public)
        with self.assertRaisesRegex(BundleError, "trusted signer"):
            verify_bundle(
                bundle,
                public_key=other_public,
                log_public_key=self.log_public,
            )

        records = [
            json.loads(line)
            for line in self.log.read_text(encoding="ascii").splitlines()
        ]
        records[1]["tree_head"]["previous_root_hash"] = "0" * 64
        records[1]["entry_sha256"] = digest_value({
            key: value for key, value in records[1].items()
            if key != "entry_sha256"
        })
        self.log.write_text(
            "\n".join(
                json.dumps(row, sort_keys=True, separators=(",", ":"))
                for row in records
            ) + "\n",
            encoding="ascii",
        )
        with self.assertRaises(BundleError):
            load_log_records(self.log, self.log_public)

    def test_seal_role_and_key_pair_mismatch_fail_closed(self):
        missing = dict(self.artifacts)
        del missing["compiler"]
        with self.assertRaisesRegex(BundleError, "roles"):
            publish_bundle(
                seal_path=self.seal,
                artifact_arguments=missing,
                private_key=self.sign_private,
                public_key=self.sign_public,
                log_path=self.log,
                log_private_key=self.log_private,
                log_public_key=self.log_public,
                output_path=self.root / "missing.zip",
            )

        other_private = self.root / "other-private.pem"
        other_public = self.root / "other-public.pem"
        generate_keypair(other_private, other_public)
        with self.assertRaisesRegex(BundleError, "do not match"):
            publish_bundle(
                seal_path=self.seal,
                artifact_arguments=self.artifacts,
                private_key=other_private,
                public_key=self.sign_public,
                log_path=self.log,
                log_private_key=self.log_private,
                log_public_key=self.log_public,
                output_path=self.root / "mismatch.zip",
            )

        with self.assertRaisesRegex(BundleError, "transparency-log keys"):
            publish_bundle(
                seal_path=self.seal,
                artifact_arguments=self.artifacts,
                private_key=self.sign_private,
                public_key=self.sign_public,
                log_path=self.log,
                log_private_key=other_private,
                log_public_key=self.log_public,
                output_path=self.root / "log-mismatch.zip",
            )

    def test_cli_publish_verify_extract_and_audit(self):
        tool = ROOT / "util" / "transform_artifact_bundle.py"
        cli_private = self.root / "cli-private.pem"
        cli_public = self.root / "cli-public.pem"
        subprocess.run([
            sys.executable,
            str(tool),
            "keygen",
            "--private-key",
            str(cli_private),
            "--public-key",
            str(cli_public),
        ], check=True, stdout=subprocess.PIPE)
        self.assertEqual(cli_private.stat().st_mode & 0o777, 0o600)
        self.assertEqual(cli_public.stat().st_mode & 0o777, 0o644)
        bundle = self.root / "cli.bundle.zip"
        subprocess.run(
            self._cli_publish_command(bundle),
            check=True,
            stdout=subprocess.PIPE,
        )
        imported = self.root / "cli-imported"
        subprocess.run([
            sys.executable,
            str(tool),
            "verify",
            str(bundle),
            "--public-key",
            str(self.sign_public),
            "--log-public-key",
            str(self.log_public),
            "--log",
            str(self.log),
            "--extract-dir",
            str(imported),
        ], check=True, stdout=subprocess.PIPE)
        audit = subprocess.run([
            sys.executable,
            str(tool),
            "audit-log",
            str(self.log),
            "--log-public-key",
            str(self.log_public),
        ], check=True, stdout=subprocess.PIPE)
        self.assertEqual(json.loads(audit.stdout)["entries"], 1)

    def test_concurrent_publishers_serialize_log_indices(self):
        bundles = [
            self.root / f"concurrent-{index}.zip"
            for index in range(8)
        ]
        processes = [
            subprocess.Popen(
                self._cli_publish_command(bundle),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            for bundle in bundles
        ]
        failures = []
        for index, process in enumerate(processes):
            output, _ = process.communicate(timeout=30)
            if process.returncode != 0:
                failures.append(
                    f"publisher {index}: {output.decode(errors='replace')}")
        self.assertEqual(failures, [])
        records, _ = load_log_records(self.log, self.log_public)
        self.assertEqual(
            [record["index"] for record in records], list(range(8)))
        for bundle in bundles:
            self.assertTrue(verify_bundle(
                bundle,
                public_key=self.sign_public,
                log_public_key=self.log_public,
                log_path=self.log,
            ))


if __name__ == "__main__":
    unittest.main()
