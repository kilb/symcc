#!/usr/bin/env python3
"""Atomically seal and verify IFSS continuation research artifacts."""

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

from verify_ifss_continuation_manifest import (
    VerificationError,
    verify_path,
)


SCHEMA = "symcc-ifss-continuation-seal-v1"
MAX_IDENTITY_BYTES = 16384


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def artifact(path, label):
    resolved = Path(path).resolve(strict=True)
    mode = resolved.stat().st_mode
    require(stat.S_ISREG(mode), f"{label} is not a regular file")
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
        raise VerificationError(f"cannot identify LLVM tool: {error}") from error
    require(
        0 < len(process.stdout) <= MAX_IDENTITY_BYTES,
        "LLVM identity output is empty or too large",
    )
    try:
        identity = process.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise VerificationError("LLVM identity is not UTF-8") from error
    require(identity, "LLVM identity is empty")
    return identity


def manifest_fingerprints(path):
    count = verify_path(path)
    fingerprints = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                fingerprints.append(json.loads(line)["proof_fingerprint"])
    require(len(fingerprints) == count, "manifest record count changed")
    return fingerprints


def canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def build_envelope(arguments):
    fingerprints = manifest_fingerprints(arguments.manifest)
    envelope = {
        "schema": SCHEMA,
        "record_count": len(fingerprints),
        "proof_fingerprints": fingerprints,
        "manifest": artifact(arguments.manifest, "manifest"),
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
    payload = canonical_bytes(envelope) + b"\n"
    atomic_write_fresh(arguments.output, payload)
    print(
        f"sealed {envelope['record_count']} IFSS continuation record(s)"
    )


def verify(arguments):
    with Path(arguments.seal).open("r", encoding="utf-8") as stream:
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

    expected = build_envelope(arguments)
    require(
        envelope == expected,
        "sealed identities do not match supplied artifacts",
    )
    print(
        f"verified {envelope['record_count']} sealed continuation record(s)"
    )


def add_artifact_arguments(parser):
    parser.add_argument("--manifest", required=True)
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
    except (OSError, json.JSONDecodeError, VerificationError) as error:
        print(f"continuation seal failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
