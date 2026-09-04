#!/usr/bin/env python3
"""Fail-closed structural replay of a sealed transform across LLVM majors."""

import argparse
import copy
import hashlib
import json
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from replay_transform_artifact import (
    replay as replay_sealed,
    replay_environment,
    run_checked,
)
from seal_transform_artifact import (
    EvidenceError,
    PIPELINE_KINDS,
    VERIFIERS,
    add_artifact_arguments,
    artifact,
    atomic_write_fresh,
    canonical_bytes,
    normalize_arguments,
    records,
    verify as verify_seal,
)


SCHEMA = "symcc-cross-llvm-transform-replay-v1"
CANDIDATES_SCHEMA = "symcc-cross-llvm-candidates-v1"
EQUIVALENCE = "exact-ir-plus-verified-manifest-and-llvm-diff-v1"
MAX_CANDIDATES = 7
MAX_IDENTITY_BYTES = 16384
LABEL_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
LLVM_MAJOR_RE = re.compile(r"(?:LLVM version|version)\s+([0-9]+)(?:\.|$)")
HYDRA_SEMANTICS_POLICY = "llvm-poison-undef-freeze-refinement-v1"
HYDRA_VERSION_FIELDS = {"llvm_version", "llvm_major"}


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def sha256(value, field):
    require(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value),
        f"{field} is not a canonical SHA-256 digest",
    )
    return value


def identify_tool(path, label):
    resolved = Path(path).resolve(strict=True)
    require(stat.S_ISREG(resolved.stat().st_mode), f"{label} is not regular")
    try:
        process = subprocess.run(
            [str(resolved), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError(f"cannot identify {label}: {error}") from error
    require(process.returncode == 0, f"{label} --version failed")
    require(
        0 < len(process.stdout) <= MAX_IDENTITY_BYTES,
        f"{label} identity is empty or too large",
    )
    try:
        identity = process.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise EvidenceError(f"{label} identity is not UTF-8") from error
    match = LLVM_MAJOR_RE.search(identity)
    require(match is not None, f"{label} identity has no LLVM major")
    major = int(match.group(1))
    require(8 <= major <= 18, f"{label} LLVM major is unsupported")
    return resolved, identity, major


def load_candidates(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        document = json.load(stream)
    require(isinstance(document, dict), "candidate document is not an object")
    require(
        document.get("schema") == CANDIDATES_SCHEMA,
        "unknown candidate schema",
    )
    rows = document.get("candidates")
    require(
        isinstance(rows, list) and 1 <= len(rows) <= MAX_CANDIDATES,
        "candidate count is out of range",
    )
    result = []
    labels = set()
    for row in rows:
        require(isinstance(row, dict), "candidate is not an object")
        require(
            set(row) in (
                {"label", "opt", "compiler"},
                {"label", "opt", "compiler", "llvm_diff"},
            ),
            "candidate fields are not canonical",
        )
        label = row.get("label")
        require(
            isinstance(label, str) and LABEL_RE.fullmatch(label),
            "candidate label is invalid",
        )
        require(label not in labels, "candidate label is duplicated")
        labels.add(label)
        opt, identity, major = identify_tool(row.get("opt"), f"{label} opt")
        compiler = Path(row.get("compiler")).resolve(strict=True)
        require(
            stat.S_ISREG(compiler.stat().st_mode),
            f"{label} compiler is not regular",
        )
        llvm_diff_path = row.get("llvm_diff")
        if llvm_diff_path is None:
            llvm_diff_path = opt.with_name("llvm-diff")
        llvm_diff, diff_identity, diff_major = identify_tool(
            llvm_diff_path, f"{label} llvm-diff"
        )
        require(
            diff_major == major,
            f"{label} opt/llvm-diff major mismatch",
        )
        result.append({
            "label": label,
            "opt": opt,
            "compiler": compiler,
            "llvm_diff": llvm_diff,
            "llvm_identity": identity,
            "llvm_diff_identity": diff_identity,
            "llvm_major": major,
        })
    return sorted(result, key=lambda item: item["label"])


def normalized_manifest(
    pipeline,
    manifest_paths,
    expected_llvm_major=None,
):
    parsed = {}
    proof_identities = []
    for kind in sorted(manifest_paths):
        path = manifest_paths[kind]
        VERIFIERS[kind](path)
        rows = records(path)
        require(rows, f"{kind} manifest is empty")
        normalized = copy.deepcopy(rows)
        if kind == "hydra":
            for original, row in zip(rows, normalized):
                require(
                    original.get("llvm_ir_semantics")
                    == HYDRA_SEMANTICS_POLICY,
                    "Hydra manifest has no explicit LLVM semantics contract",
                )
                if expected_llvm_major is not None:
                    require(
                        original.get("llvm_major") == expected_llvm_major,
                        "Hydra manifest LLVM major differs from the tool",
                    )
                for field in HYDRA_VERSION_FIELDS:
                    require(field in row, f"Hydra manifest has no {field}")
                    del row[field]
        parsed[kind] = normalized
        identity_field = (
            "structure_fingerprint"
            if kind == "hydra"
            else "proof_fingerprint"
        )
        for row in rows:
            identity = row.get(identity_field)
            require(
                isinstance(identity, str) and identity,
                f"{kind} manifest has no proof identity",
            )
            proof_identities.append(f"{kind}:{identity}")
    digest = hashlib.sha256(canonical_bytes(parsed)).hexdigest()
    return parsed, proof_identities, digest


def run_candidate(arguments, envelope, candidate, baseline):
    manifests = normalize_arguments(arguments)
    tool_artifacts = {
        "llvm_tool": artifact(candidate["opt"], "candidate LLVM tool"),
        "llvm_diff": artifact(candidate["llvm_diff"], "candidate llvm-diff"),
        "compiler": artifact(candidate["compiler"], "candidate compiler"),
    }
    with tempfile.TemporaryDirectory(
        prefix=f"symcc-cross-llvm-{candidate['label']}-"
    ) as directory:
        root = Path(directory)
        replay_manifests = {
            kind: root / f"{kind}.jsonl" for kind in manifests
        }
        replay_ir = root / "lowered.ll"
        environment = replay_environment(
            arguments.pipeline, envelope, replay_manifests
        )
        configuration = envelope["replay_configuration"]
        run_checked(
            [
                str(candidate["opt"]),
                "-load-pass-plugin=" + str(candidate["compiler"]),
                "-passes=" + configuration["passes"],
                "-S",
                str(Path(arguments.input_ir).resolve(strict=True)),
                "-o",
                str(replay_ir),
            ],
            environment=environment,
        )
        run_checked([
            str(candidate["opt"]),
            "-passes=verify",
            "-disable-output",
            str(replay_ir),
        ])
        for kind, path in replay_manifests.items():
            require(path.is_file(), f"candidate emitted no {kind} manifest")
        _, proof_identities, manifest_digest = normalized_manifest(
            arguments.pipeline,
            replay_manifests,
            candidate["llvm_major"],
        )
        require(
            proof_identities == baseline["proof_identities"],
            "cross-LLVM proof identities differ",
        )
        require(
            manifest_digest == baseline["normalized_manifest_sha256"],
            "cross-LLVM normalized manifests differ",
        )
        run_checked([
            str(candidate["llvm_diff"]),
            str(Path(arguments.lowered_ir).resolve(strict=True)),
            str(replay_ir),
        ])
        lowered = artifact(replay_ir, "candidate lowered IR")
        require(
            lowered["size"] == baseline["lowered_ir"]["size"]
            and lowered["sha256"] == baseline["lowered_ir"]["sha256"],
            "cross-LLVM lowered IR is not byte-identical",
        )
        manifest_artifacts = {
            kind: artifact(path, f"candidate {kind} manifest")
            for kind, path in sorted(replay_manifests.items())
        }
        require(
            tool_artifacts
            == {
                "llvm_tool": artifact(
                    candidate["opt"], "candidate LLVM tool"),
                "llvm_diff": artifact(
                    candidate["llvm_diff"], "candidate llvm-diff"),
                "compiler": artifact(
                    candidate["compiler"], "candidate compiler"),
            },
            "candidate tool or compiler changed during replay",
        )
    return {
        "label": candidate["label"],
        "llvm_major": candidate["llvm_major"],
        "llvm_identity": candidate["llvm_identity"],
        "llvm_diff_identity": candidate["llvm_diff_identity"],
        "llvm_tool": tool_artifacts["llvm_tool"],
        "llvm_diff": tool_artifacts["llvm_diff"],
        "compiler": tool_artifacts["compiler"],
        "lowered_ir": lowered,
        "manifests": manifest_artifacts,
        "proof_identities": proof_identities,
        "normalized_manifest_sha256": manifest_digest,
        "llvm_verifier_passed": True,
        "llvm_diff_passed": True,
        "exact_lowered_ir_passed": True,
    }


def parse_sealed_major(identity):
    require(isinstance(identity, str), "sealed LLVM identity is invalid")
    match = LLVM_MAJOR_RE.search(identity)
    require(match is not None, "sealed LLVM identity has no major")
    return int(match.group(1))


def audit(arguments):
    envelope = verify_seal(arguments)
    replay_sealed(arguments)
    manifests = normalize_arguments(arguments)
    baseline_major = parse_sealed_major(envelope.get("llvm_identity"))
    _, proof_identities, manifest_digest = normalized_manifest(
        arguments.pipeline,
        manifests,
        baseline_major,
    )
    baseline = {
        "llvm_major": baseline_major,
        "llvm_identity": envelope["llvm_identity"],
        "llvm_tool": envelope["llvm_tool"],
        "compiler": envelope["compiler"],
        "lowered_ir": envelope["lowered_ir"],
        "proof_identities": proof_identities,
        "normalized_manifest_sha256": manifest_digest,
        "sealed_replay_passed": True,
    }
    candidates = [
        run_candidate(arguments, envelope, candidate, baseline)
        for candidate in load_candidates(arguments.candidates)
    ]
    majors = sorted({
        baseline_major,
        *(candidate["llvm_major"] for candidate in candidates),
    })
    require(
        len(majors) >= 2,
        "cross-LLVM audit requires at least two distinct LLVM majors",
    )
    require(
        verify_seal(arguments) == envelope,
        "sealed baseline changed during cross-LLVM replay",
    )
    certificate = {
        "schema": SCHEMA,
        "pipeline": arguments.pipeline,
        "equivalence": EQUIVALENCE,
        "seal_sha256": envelope["seal_sha256"],
        "baseline": baseline,
        "candidates": candidates,
        "verified_llvm_majors": majors,
        "cross_major_verified": True,
    }
    certificate["certificate_sha256"] = hashlib.sha256(
        canonical_bytes(certificate)
    ).hexdigest()
    verify_certificate_value(certificate)
    atomic_write_fresh(
        arguments.output, canonical_bytes(certificate) + b"\n"
    )
    print(
        "verified sealed transform across LLVM majors "
        + ",".join(str(major) for major in majors)
    )


def artifact_value(value, field):
    require(isinstance(value, dict), f"{field} is not an object")
    require(
        set(value) == {"name", "size", "sha256"},
        f"{field} fields are not canonical",
    )
    require(
        isinstance(value["name"], str)
        and value["name"]
        and "/" not in value["name"]
        and "\x00" not in value["name"],
        f"{field} name is invalid",
    )
    require(
        isinstance(value["size"], int)
        and not isinstance(value["size"], bool)
        and 0 <= value["size"] <= (1 << 40),
        f"{field} size is invalid",
    )
    sha256(value["sha256"], f"{field} SHA-256")


def verify_certificate_value(certificate):
    require(isinstance(certificate, dict), "certificate is not an object")
    require(
        set(certificate)
        == {
            "schema",
            "pipeline",
            "equivalence",
            "seal_sha256",
            "baseline",
            "candidates",
            "verified_llvm_majors",
            "cross_major_verified",
            "certificate_sha256",
        },
        "certificate fields are not canonical",
    )
    require(certificate.get("schema") == SCHEMA, "unknown certificate schema")
    require(
        certificate.get("pipeline") in ("continuation", "hydra", "loop"),
        "unknown certificate pipeline",
    )
    require(
        certificate.get("equivalence") == EQUIVALENCE,
        "unknown equivalence contract",
    )
    supplied = sha256(
        certificate.get("certificate_sha256"), "certificate SHA-256"
    )
    unsigned = dict(certificate)
    del unsigned["certificate_sha256"]
    require(
        hashlib.sha256(canonical_bytes(unsigned)).hexdigest() == supplied,
        "certificate SHA-256 mismatch",
    )
    sha256(certificate.get("seal_sha256"), "seal SHA-256")
    baseline = certificate.get("baseline")
    require(isinstance(baseline, dict), "baseline is not an object")
    require(
        set(baseline)
        == {
            "llvm_major",
            "llvm_identity",
            "llvm_tool",
            "compiler",
            "lowered_ir",
            "proof_identities",
            "normalized_manifest_sha256",
            "sealed_replay_passed",
        },
        "baseline fields are not canonical",
    )
    major = baseline.get("llvm_major")
    require(
        isinstance(major, int)
        and not isinstance(major, bool)
        and 8 <= major <= 18,
        "baseline LLVM major is invalid",
    )
    require(
        isinstance(baseline.get("llvm_identity"), str)
        and 0 < len(baseline["llvm_identity"]) <= MAX_IDENTITY_BYTES
        and parse_sealed_major(baseline["llvm_identity"]) == major,
        "baseline LLVM identity/major mismatch",
    )
    require(
        baseline.get("sealed_replay_passed") is True,
        "sealed baseline was not independently replayed",
    )
    for field in ("llvm_tool", "compiler", "lowered_ir"):
        artifact_value(baseline.get(field), f"baseline {field}")
    proof_identities = baseline.get("proof_identities")
    require(
        isinstance(proof_identities, list)
        and proof_identities
        and all(isinstance(item, str) and item for item in proof_identities),
        "baseline proof identities are invalid",
    )
    require(
        len(set(proof_identities)) == len(proof_identities),
        "baseline proof identities are duplicated",
    )
    proof_kinds = {
        identity.partition(":")[0] for identity in proof_identities
    }
    require(
        all(
            ":" in identity
            and identity.partition(":")[0] in VERIFIERS
            and identity.partition(":")[2]
            for identity in proof_identities
        )
        and proof_kinds <= PIPELINE_KINDS[certificate["pipeline"]],
        "baseline proof identities do not match the pipeline",
    )
    if certificate["pipeline"] in ("continuation", "hydra"):
        require(
            proof_kinds == PIPELINE_KINDS[certificate["pipeline"]],
            "baseline proof identities are incomplete",
        )
    normalized_digest = sha256(
        baseline.get("normalized_manifest_sha256"),
        "baseline normalized manifest SHA-256",
    )
    rows = certificate.get("candidates")
    require(
        isinstance(rows, list) and 1 <= len(rows) <= MAX_CANDIDATES,
        "certificate candidate count is invalid",
    )
    labels = []
    majors = {major}
    for row in rows:
        require(isinstance(row, dict), "certificate candidate is not an object")
        require(
            set(row)
            == {
                "label",
                "llvm_major",
                "llvm_identity",
                "llvm_diff_identity",
                "llvm_tool",
                "llvm_diff",
                "compiler",
                "lowered_ir",
                "manifests",
                "proof_identities",
                "normalized_manifest_sha256",
                "llvm_verifier_passed",
                "llvm_diff_passed",
                "exact_lowered_ir_passed",
            },
            "certificate candidate fields are not canonical",
        )
        label = row.get("label")
        require(
            isinstance(label, str) and LABEL_RE.fullmatch(label),
            "certificate candidate label is invalid",
        )
        labels.append(label)
        candidate_major = row.get("llvm_major")
        require(
            isinstance(candidate_major, int)
            and not isinstance(candidate_major, bool)
            and 8 <= candidate_major <= 18,
            "certificate candidate LLVM major is invalid",
        )
        majors.add(candidate_major)
        require(
            isinstance(row.get("llvm_identity"), str)
            and 0 < len(row["llvm_identity"]) <= MAX_IDENTITY_BYTES
            and parse_sealed_major(row["llvm_identity"]) == candidate_major,
            "candidate LLVM identity/major mismatch",
        )
        require(
            isinstance(row.get("llvm_diff_identity"), str)
            and 0 < len(row["llvm_diff_identity"]) <= MAX_IDENTITY_BYTES
            and parse_sealed_major(row["llvm_diff_identity"])
            == candidate_major,
            "candidate llvm-diff identity/major mismatch",
        )
        for field in (
            "llvm_tool",
            "llvm_diff",
            "compiler",
            "lowered_ir",
        ):
            artifact_value(row.get(field), f"candidate {field}")
        manifest_artifacts = row.get("manifests")
        require(
            isinstance(manifest_artifacts, dict) and manifest_artifacts,
            "candidate manifests are invalid",
        )
        require(
            set(manifest_artifacts) == proof_kinds,
            "candidate manifest kinds differ from the sealed baseline",
        )
        for kind, value in manifest_artifacts.items():
            require(kind in VERIFIERS, "candidate manifest kind is invalid")
            artifact_value(value, f"candidate {kind} manifest")
        require(
            row.get("proof_identities") == proof_identities,
            "candidate proof identities differ",
        )
        require(
            row.get("normalized_manifest_sha256") == normalized_digest,
            "candidate normalized manifest differs",
        )
        require(
            row.get("llvm_verifier_passed") is True
            and row.get("llvm_diff_passed") is True
            and row.get("exact_lowered_ir_passed") is True,
            "candidate did not pass every replay gate",
        )
        require(
            row["lowered_ir"]["size"] == baseline["lowered_ir"]["size"]
            and row["lowered_ir"]["sha256"]
            == baseline["lowered_ir"]["sha256"],
            "candidate lowered IR identity differs",
        )
    require(labels == sorted(set(labels)), "candidate labels are not canonical")
    claimed_majors = certificate.get("verified_llvm_majors")
    require(
        claimed_majors == sorted(majors) and len(majors) >= 2,
        "verified LLVM major set is invalid",
    )
    require(
        certificate.get("cross_major_verified") is True,
        "certificate does not claim cross-major verification",
    )
    return certificate


def verify_certificate(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        certificate = json.load(stream)
    verify_certificate_value(certificate)
    print(
        "verified cross-LLVM certificate for majors "
        + ",".join(
            str(major) for major in certificate["verified_llvm_majors"]
        )
    )
    return certificate


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    audit_parser = commands.add_parser("audit")
    add_artifact_arguments(audit_parser)
    audit_parser.add_argument("--seal", required=True)
    audit_parser.add_argument("--candidates", required=True)
    audit_parser.add_argument("--output", required=True)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--certificate", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "audit":
            audit(arguments)
        else:
            verify_certificate(arguments.certificate)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(f"cross-LLVM replay failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
