#!/usr/bin/env python3
"""Atomically seal IFSS loop/continuation and Hydra transformations."""

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from verify_hydra_transform_manifest import verify_path as verify_hydra
from verify_ifss_continuation_manifest import (
    verify_path as verify_continuation,
)
from verify_ifss_loop_exit_manifest import verify_path as verify_loop_exit
from verify_ifss_loop_recurrence_manifest import (
    verify_path as verify_loop_recurrence,
)


SCHEMA = "symcc-transformation-seal-v1"
MAX_IDENTITY_BYTES = 16384
PIPELINE_KINDS = {
    "continuation": {"continuation"},
    "loop": {"loop-recurrence", "loop-exit"},
    "hydra": {"hydra"},
}
VERIFIERS = {
    "continuation": verify_continuation,
    "loop-recurrence": verify_loop_recurrence,
    "loop-exit": verify_loop_exit,
    "hydra": verify_hydra,
}
IDENTITY_FIELDS = {
    "continuation": "proof_fingerprint",
    "loop-recurrence": "proof_fingerprint",
    "loop-exit": "proof_fingerprint",
    "hydra": "structure_fingerprint",
}


class EvidenceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def artifact(path, label):
    resolved = Path(path).resolve(strict=True)
    require(stat.S_ISREG(resolved.stat().st_mode), f"{label} is not regular")
    size, digest = sha256_file(resolved)
    return {
        "name": resolved.name,
        "size": size,
        "sha256": digest,
    }


def llvm_identity(path):
    resolved = Path(path).resolve(strict=True)
    try:
        process = subprocess.run(
            [str(resolved), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError(f"cannot identify LLVM tool: {error}") from error
    require(
        0 < len(process.stdout) <= MAX_IDENTITY_BYTES,
        "LLVM identity output is empty or too large",
    )
    try:
        identity = process.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise EvidenceError("LLVM identity is not UTF-8") from error
    require(identity, "LLVM identity is empty")
    return identity


def records(path):
    result = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                result.append(json.loads(line))
    return result


def parse_manifests(values):
    result = {}
    for value in values:
        kind, separator, path = value.partition("=")
        require(separator and path, "manifest must be KIND=PATH")
        require(kind in VERIFIERS, f"unknown manifest kind: {kind}")
        require(kind not in result, f"duplicate manifest kind: {kind}")
        result[kind] = path
    require(result, "at least one manifest is required")
    return result


def validate_pipeline(pipeline, manifests):
    allowed = PIPELINE_KINDS[pipeline]
    kinds = set(manifests)
    require(kinds <= allowed, "manifest kind does not match pipeline")
    if pipeline in ("continuation", "hydra"):
        require(kinds == allowed, f"{pipeline} manifest is required")
    else:
        require(kinds, "loop pipeline has no proof manifest")


def manifest_evidence(kind, path):
    before = artifact(path, f"{kind} manifest")
    count = VERIFIERS[kind](path)
    parsed = records(path)
    require(len(parsed) == count, f"{kind} manifest changed during read")
    after = artifact(path, f"{kind} manifest")
    require(
        before == after,
        f"{kind} manifest changed while evidence was constructed",
    )
    field = IDENTITY_FIELDS[kind]
    identities = []
    for record in parsed:
        identity = record.get(field)
        require(
            isinstance(identity, str) and identity,
            f"{kind} record has no canonical proof identity",
        )
        identities.append(identity)
    return {
        "kind": kind,
        "record_count": count,
        "proof_identities": identities,
        "artifact": after,
    }, parsed


def replay_configuration(pipeline, parsed):
    if pipeline == "hydra":
        hydra = parsed["hydra"]
        require(
            1 <= len(hydra) <= 4,
            "Hydra seal record count is outside the bounded range",
        )
        modes = {record.get("mode") for record in hydra}
        require(
            len(modes) == 1
            and next(iter(modes))
            in ("safe-alu", "aggressive-memory"),
            "Hydra mode is invalid",
        )
        mode = next(iter(modes))
        if len(hydra) == 1:
            site = hydra[0].get("site")
            require(
                isinstance(site, int)
                and not isinstance(site, bool)
                and site > 0,
                "Hydra site is invalid",
            )
            return {
                "passes": "hydra-transform",
                "site": site,
                "mode": "safe" if mode == "safe-alu" else "aggressive",
            }
        sites = [record.get("site") for record in hydra]
        require(
            all(
                isinstance(site, int)
                and not isinstance(site, bool)
                and site > 0
                for site in sites
            ),
            "Hydra transaction site is invalid",
        )
        require(
            all(
                record.get("multi_site_transaction") is True
                and record.get("transaction_ordinal") == ordinal
                and [
                    int(site)
                    for site in record.get("transaction_sites", [])
                ]
                == sites
                for ordinal, record in enumerate(hydra)
            ),
            "Hydra transaction records disagree",
        )
        return {
            "passes": "hydra-transform",
            "sites": sites,
            "mode": "safe" if mode == "safe-alu" else "aggressive",
        }
    if pipeline == "loop":
        return {
            "passes": "ifss-loop-summary",
            "manifests": sorted(parsed),
        }
    continuation = parsed["continuation"]
    memory_enabled = any(
        record.get("analysis") != "structural-only-v1"
        for record in continuation
    )
    return {
        "passes": "ifss-continuation-lowering,ifss-continuation-memory",
        "continuation_state": True,
        "continuation_memory": memory_enabled,
    }


def normalize_arguments(arguments):
    manifests = parse_manifests(arguments.manifest)
    validate_pipeline(arguments.pipeline, manifests)
    return manifests


def build_envelope(arguments):
    manifests = normalize_arguments(arguments)
    evidence = []
    parsed = {}
    for kind in sorted(manifests):
        item, manifest_records = manifest_evidence(kind, manifests[kind])
        evidence.append(item)
        parsed[kind] = manifest_records
    envelope = {
        "schema": SCHEMA,
        "pipeline": arguments.pipeline,
        "replay_configuration": replay_configuration(
            arguments.pipeline, parsed
        ),
        "manifests": evidence,
        "input_ir": artifact(arguments.input_ir, "input IR"),
        "lowered_ir": artifact(arguments.lowered_ir, "lowered IR"),
        "compiler": artifact(arguments.compiler, "compiler"),
        "llvm_tool": artifact(arguments.llvm_tool, "LLVM tool"),
        "llvm_identity": llvm_identity(arguments.llvm_tool),
    }
    envelope["seal_sha256"] = hashlib.sha256(
        canonical_bytes(envelope)
    ).hexdigest()
    return envelope


def atomic_write_fresh(path, payload):
    output = Path(path)
    parent = output.parent.resolve(strict=True)
    require(parent.is_dir(), "seal output parent is not a directory")
    lock_path = output.with_name(output.name + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    temporary = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        require(not output.exists(), "seal output already exists")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=output.name + ".tmp.",
            dir=parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def seal(arguments):
    envelope = build_envelope(arguments)
    atomic_write_fresh(
        arguments.output, canonical_bytes(envelope) + b"\n"
    )
    count = sum(item["record_count"] for item in envelope["manifests"])
    print(f"sealed {count} {arguments.pipeline} transformation record(s)")


def load_seal(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        envelope = json.load(stream)
    require(isinstance(envelope, dict), "seal is not an object")
    require(envelope.get("schema") == SCHEMA, "unknown seal schema")
    actual_hash = envelope.get("seal_sha256")
    require(
        isinstance(actual_hash, str)
        and len(actual_hash) == 64
        and all(character in "0123456789abcdef" for character in actual_hash),
        "seal SHA-256 is not canonical",
    )
    unsigned = dict(envelope)
    del unsigned["seal_sha256"]
    expected_hash = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
    require(actual_hash == expected_hash, "seal SHA-256 mismatch")
    return envelope


def verify(arguments):
    envelope = load_seal(arguments.seal)
    expected = build_envelope(arguments)
    require(
        envelope == expected,
        "sealed identities do not match supplied artifacts",
    )
    count = sum(item["record_count"] for item in envelope["manifests"])
    print(f"verified {count} sealed transformation record(s)")
    return envelope


def add_artifact_arguments(parser):
    parser.add_argument(
        "--pipeline",
        required=True,
        choices=sorted(PIPELINE_KINDS),
    )
    parser.add_argument(
        "--manifest",
        action="append",
        required=True,
        metavar="KIND=PATH",
    )
    parser.add_argument("--input-ir", required=True)
    parser.add_argument("--lowered-ir", required=True)
    parser.add_argument("--compiler", required=True)
    parser.add_argument("--llvm-tool", required=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    seal_parser = commands.add_parser("seal")
    add_artifact_arguments(seal_parser)
    seal_parser.add_argument("--output", required=True)
    verify_parser = commands.add_parser("verify")
    add_artifact_arguments(verify_parser)
    verify_parser.add_argument("--seal", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "seal":
            seal(arguments)
        else:
            verify(arguments)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(f"transformation seal failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
