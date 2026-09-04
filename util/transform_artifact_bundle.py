#!/usr/bin/env python3
"""Sign, log, transport, and independently verify transformation artifacts."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from seal_transform_artifact import load_seal


BUNDLE_SCHEMA = "symcc-transform-transport-bundle-v1"
DESCRIPTOR_SCHEMA = "symcc-transform-transport-descriptor-v1"
LEAF_SCHEMA = "symcc-transform-transparency-leaf-v1"
HEAD_SCHEMA = "symcc-transform-transparency-head-v1"
LOG_SCHEMA = "symcc-transform-transparency-entry-v1"
SIGNATURE_DOMAIN = b"symcc-transform-bundle-v1\x00"
TREE_HEAD_DOMAIN = b"symcc-transform-tree-head-v1\x00"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_FILES = 32
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_LOG_ENTRIES = 4096
MAX_LOG_BYTES = 64 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


class BundleError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BundleError(message)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_value(value: Any) -> str:
    return digest_bytes(canonical_bytes(value))


def canonical_digest(value: Any, field: str) -> str:
    require(
        isinstance(value, str) and SHA256_RE.fullmatch(value) is not None,
        f"{field} is not a canonical SHA-256 digest",
    )
    return value


def file_identity(path: str | os.PathLike[str]) -> dict[str, Any]:
    resolved = Path(path).resolve(strict=True)
    require(resolved.is_file(), f"artifact is not a regular file: {resolved}")
    digest = hashlib.sha256()
    size = 0
    with resolved.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            require(size <= MAX_FILE_BYTES, "artifact exceeds per-file limit")
    return {
        "path": resolved,
        "name": resolved.name,
        "size": size,
        "sha256": digest.hexdigest(),
        "mode": stat.S_IMODE(resolved.stat().st_mode),
    }


def run_openssl(
    arguments: list[str],
    *,
    input_data: bytes | None = None,
) -> bytes:
    executable = shutil.which("openssl")
    require(executable is not None, "openssl executable is unavailable")
    try:
        process = subprocess.run(
            [executable, *arguments],
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BundleError(f"openssl invocation failed: {error}") from error
    require(
        process.returncode == 0,
        "openssl rejected the operation: "
        + process.stderr[-4096:].decode("utf-8", errors="replace"),
    )
    return process.stdout


def public_key_der(path: str | os.PathLike[str]) -> bytes:
    resolved = Path(path).resolve(strict=True)
    require(resolved.is_file(), "public key is not a regular file")
    return run_openssl([
        "pkey",
        "-pubin",
        "-in",
        str(resolved),
        "-outform",
        "DER",
    ])


def key_fingerprint(path: str | os.PathLike[str]) -> str:
    return digest_bytes(public_key_der(path))


def sign_message(
    private_key: str | os.PathLike[str],
    message: bytes,
) -> bytes:
    resolved = Path(private_key).resolve(strict=True)
    require(resolved.is_file(), "private key is not a regular file")
    with tempfile.NamedTemporaryFile(
            prefix="symcc-message-", delete=True) as temporary:
        temporary.write(message)
        temporary.flush()
        signature = run_openssl([
            "pkeyutl",
            "-sign",
            "-rawin",
            "-inkey",
            str(resolved),
            "-in",
            temporary.name,
        ])
    require(len(signature) == 64, "Ed25519 signature length is not 64 bytes")
    return signature


def verify_signature(
    public_key: str | os.PathLike[str],
    message: bytes,
    signature: bytes,
) -> bool:
    if len(signature) != 64:
        return False
    resolved = Path(public_key).resolve(strict=True)
    with tempfile.TemporaryDirectory(
            prefix="symcc-ed25519-verify-") as temporary:
        root = Path(temporary)
        signature_path = root / "signature"
        message_path = root / "message"
        signature_path.write_bytes(signature)
        message_path.write_bytes(message)
        executable = shutil.which("openssl")
        require(executable is not None, "openssl executable is unavailable")
        try:
            process = subprocess.run(
                [
                    executable,
                    "pkeyutl",
                    "-verify",
                    "-rawin",
                    "-pubin",
                    "-inkey",
                    str(resolved),
                    "-sigfile",
                    str(signature_path),
                    "-in",
                    str(message_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise BundleError(
                f"openssl verification failed: {error}") from error
    return process.returncode == 0


def atomic_write_fresh(
    path: str | os.PathLike[str],
    payload: bytes,
    mode: int,
) -> None:
    output = Path(path)
    parent = output.parent.resolve(strict=True)
    require(parent.is_dir(), "output parent is not a directory")
    lock = output.with_name(output.name + ".lock")
    lock_fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    temporary: Path | None = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        require(not output.exists(), f"output already exists: {output}")
        descriptor, name = tempfile.mkstemp(
            prefix=output.name + ".tmp.", dir=parent)
        temporary = Path(name)
        os.fchmod(descriptor, mode)
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


@contextlib.contextmanager
def exclusive_lock(path: Path):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def generate_keypair(
    private_key: str | os.PathLike[str],
    public_key: str | os.PathLike[str],
) -> str:
    with tempfile.TemporaryDirectory(
            prefix="symcc-ed25519-") as temporary:
        root = Path(temporary)
        private = root / "private.pem"
        public = root / "public.pem"
        run_openssl([
            "genpkey",
            "-algorithm",
            "ED25519",
            "-out",
            str(private),
        ])
        run_openssl([
            "pkey",
            "-in",
            str(private),
            "-pubout",
            "-out",
            str(public),
        ])
        atomic_write_fresh(private_key, private.read_bytes(), 0o600)
        atomic_write_fresh(public_key, public.read_bytes(), 0o644)
    return key_fingerprint(public_key)


def leaf_hash(payload: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + payload).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def largest_power_of_two_less_than(value: int) -> int:
    require(value > 1, "Merkle split requires at least two leaves")
    return 1 << ((value - 1).bit_length() - 1)


def merkle_root(payloads: list[bytes]) -> bytes:
    if not payloads:
        return hashlib.sha256(b"").digest()
    if len(payloads) == 1:
        return leaf_hash(payloads[0])
    split = largest_power_of_two_less_than(len(payloads))
    return node_hash(
        merkle_root(payloads[:split]),
        merkle_root(payloads[split:]),
    )


def inclusion_proof(
    payloads: list[bytes],
    index: int,
) -> list[bytes]:
    require(0 <= index < len(payloads), "inclusion index is out of range")
    if len(payloads) == 1:
        return []
    split = largest_power_of_two_less_than(len(payloads))
    if index < split:
        return (
            inclusion_proof(payloads[:split], index)
            + [merkle_root(payloads[split:])]
        )
    return (
        inclusion_proof(payloads[split:], index - split)
        + [merkle_root(payloads[:split])]
    )


def root_from_inclusion(
    leaf: bytes,
    index: int,
    tree_size: int,
    proof: list[bytes],
) -> bytes:
    require(0 <= index < tree_size, "inclusion coordinates are invalid")
    position = 0

    def reconstruct(local_index: int, count: int) -> bytes:
        nonlocal position
        if count == 1:
            return leaf
        split = largest_power_of_two_less_than(count)
        if local_index < split:
            child = reconstruct(local_index, split)
            require(position < len(proof), "inclusion proof is too short")
            sibling = proof[position]
            position += 1
            return node_hash(child, sibling)
        child = reconstruct(local_index - split, count - split)
        require(position < len(proof), "inclusion proof is too short")
        sibling = proof[position]
        position += 1
        return node_hash(sibling, child)

    result = reconstruct(index, tree_size)
    require(position == len(proof), "inclusion proof has trailing nodes")
    return result


def decode_signature(value: Any, field: str) -> bytes:
    require(isinstance(value, str), f"{field} is not base64 text")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise BundleError(f"{field} is invalid base64") from error
    require(len(decoded) == 64, f"{field} is not an Ed25519 signature")
    return decoded


def expected_artifacts(envelope: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    def identity(value: Any, field: str) -> Mapping[str, Any]:
        require(isinstance(value, Mapping), f"{field} identity is invalid")
        name = value.get("name")
        require(
            isinstance(name, str)
            and 1 <= len(name) <= 255
            and Path(name).name == name
            and name not in (".", ".."),
            f"{field} name is invalid",
        )
        size = value.get("size")
        require(
            isinstance(size, int)
            and not isinstance(size, bool)
            and 0 <= size <= MAX_FILE_BYTES,
            f"{field} size is invalid",
        )
        canonical_digest(value.get("sha256"), f"{field} sha256")
        return value

    expected: dict[str, Mapping[str, Any]] = {}
    for field, role in (
        ("input_ir", "input-ir"),
        ("lowered_ir", "lowered-ir"),
        ("compiler", "compiler"),
        ("llvm_tool", "llvm-tool"),
    ):
        expected[role] = identity(envelope.get(field), field)
    manifests = envelope.get("manifests")
    require(isinstance(manifests, list) and manifests, "seal has no manifests")
    for manifest in manifests:
        require(isinstance(manifest, Mapping), "manifest evidence is invalid")
        kind = manifest.get("kind")
        require(
            isinstance(kind, str) and ROLE_RE.fullmatch(kind) is not None,
            "manifest kind is invalid",
        )
        role = f"manifest:{kind}"
        require(role not in expected, "seal has duplicate manifest kinds")
        expected[role] = identity(
            manifest.get("artifact"), f"manifest:{kind}")
    return expected


def parse_artifact_arguments(
    values: Iterable[str],
) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        role, separator, raw_path = value.partition("=")
        require(separator and raw_path, "artifact must be ROLE=PATH")
        require(
            ROLE_RE.fullmatch(role) is not None,
            f"invalid artifact role: {role}",
        )
        require(role not in result, f"duplicate artifact role: {role}")
        result[role] = Path(raw_path)
    require(len(result) <= MAX_FILES, "too many bundle artifact roles")
    return result


def transport_name(role: str, original_name: str) -> str:
    if role == "seal":
        return "seal.json"
    if role.startswith("manifest:"):
        return "manifest-" + role.split(":", 1)[1] + ".jsonl"
    suffix = Path(original_name).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        suffix = ""
    return role.replace(":", "-") + suffix


def build_descriptor(
    seal_path: str | os.PathLike[str],
    artifact_arguments: Mapping[str, Path],
) -> tuple[dict[str, Any], dict[str, Path]]:
    envelope = load_seal(seal_path)
    expected = expected_artifacts(envelope)
    require(
        set(artifact_arguments) == set(expected),
        "artifact roles do not exactly match the transformation seal",
    )
    paths: dict[str, Path] = {"seal": Path(seal_path)}
    paths.update(artifact_arguments)
    files = []
    total = 0
    names = set()
    for role in sorted(paths):
        identity = file_identity(paths[role])
        if role != "seal":
            sealed = expected[role]
            require(
                identity["size"] == sealed.get("size")
                and identity["sha256"] == sealed.get("sha256"),
                f"{role} does not match the transformation seal",
            )
        name = transport_name(role, identity["name"])
        require(name not in names, "transport filenames collide")
        names.add(name)
        total += identity["size"]
        require(total <= MAX_TOTAL_BYTES, "bundle exceeds total byte limit")
        files.append({
            "role": role,
            "name": identity["name"],
            "transport_name": name,
            "size": identity["size"],
            "sha256": identity["sha256"],
            "mode": identity["mode"],
            "blob": f"blobs/{identity['sha256']}",
        })
        paths[role] = identity["path"]
    descriptor = {
        "schema": DESCRIPTOR_SCHEMA,
        "pipeline": envelope.get("pipeline"),
        "seal_sha256": envelope.get("seal_sha256"),
        "files": files,
    }
    canonical_digest(descriptor["seal_sha256"], "seal_sha256")
    return descriptor, paths


def log_leaf(
    payload_sha256: str,
    signature: bytes,
    signer_key_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": LEAF_SCHEMA,
        "payload_sha256": payload_sha256,
        "signature_sha256": digest_bytes(signature),
        "signer_key_sha256": signer_key_sha256,
    }


def validate_leaf(value: Any) -> dict[str, Any]:
    require(isinstance(value, dict), "transparency leaf is not an object")
    require(value.get("schema") == LEAF_SCHEMA, "unknown leaf schema")
    canonical_digest(value.get("payload_sha256"), "payload_sha256")
    canonical_digest(value.get("signature_sha256"), "signature_sha256")
    canonical_digest(value.get("signer_key_sha256"), "signer_key_sha256")
    require(
        set(value)
        == {
            "schema",
            "payload_sha256",
            "signature_sha256",
            "signer_key_sha256",
        },
        "transparency leaf has unexpected fields",
    )
    return value


def load_log_records(
    log_path: str | os.PathLike[str],
    log_public_key: str | os.PathLike[str],
) -> tuple[list[dict[str, Any]], list[bytes]]:
    path = Path(log_path)
    if not path.exists():
        return [], []
    require(path.is_file(), "transparency log is not a regular file")
    require(
        path.stat().st_size <= MAX_LOG_BYTES,
        "transparency log byte cap exceeded",
    )
    lines = path.read_bytes().splitlines()
    require(len(lines) <= MAX_LOG_ENTRIES, "transparency log is too large")
    fingerprint = key_fingerprint(log_public_key)
    records = []
    payloads: list[bytes] = []
    previous_root = hashlib.sha256(b"").hexdigest()
    for index, line in enumerate(lines):
        require(line and len(line) <= MAX_METADATA_BYTES, "invalid log line")
        try:
            record = json.loads(line.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BundleError("transparency log is not canonical JSONL") from error
        require(isinstance(record, dict), "log record is not an object")
        require(record.get("schema") == LOG_SCHEMA, "unknown log schema")
        require(
            set(record)
            == {
                "schema",
                "index",
                "leaf",
                "leaf_hash",
                "tree_head",
                "tree_head_signature",
                "entry_sha256",
            },
            "transparency log record has unexpected fields",
        )
        require(
            isinstance(record.get("index"), int)
            and not isinstance(record.get("index"), bool)
            and record["index"] == index,
            "transparency log index is not contiguous",
        )
        leaf = validate_leaf(record.get("leaf"))
        payload = canonical_bytes(leaf)
        payloads.append(payload)
        require(
            record.get("leaf_hash") == leaf_hash(payload).hex(),
            "transparency leaf hash mismatch",
        )
        head = record.get("tree_head")
        require(isinstance(head, dict), "tree head is not an object")
        require(head.get("schema") == HEAD_SCHEMA, "unknown tree head schema")
        require(
            set(head)
            == {
                "schema",
                "log_key_sha256",
                "tree_size",
                "root_hash",
                "previous_tree_size",
                "previous_root_hash",
            },
            "tree head has unexpected fields",
        )
        require(head.get("log_key_sha256") == fingerprint, "log key mismatch")
        require(
            isinstance(head.get("tree_size"), int)
            and not isinstance(head.get("tree_size"), bool)
            and head["tree_size"] == index + 1,
            "tree size mismatch",
        )
        require(
            isinstance(head.get("previous_tree_size"), int)
            and not isinstance(head.get("previous_tree_size"), bool)
            and head["previous_tree_size"] == index,
            "previous tree size mismatch",
        )
        require(
            head.get("previous_root_hash") == previous_root,
            "previous tree root mismatch",
        )
        root = merkle_root(payloads).hex()
        require(head.get("root_hash") == root, "Merkle root mismatch")
        signature = decode_signature(
            record.get("tree_head_signature"),
            "tree_head_signature",
        )
        require(
            verify_signature(
                log_public_key,
                TREE_HEAD_DOMAIN + canonical_bytes(head),
                signature,
            ),
            "tree head signature is invalid",
        )
        unsigned = dict(record)
        supplied_digest = unsigned.pop("entry_sha256", None)
        require(
            supplied_digest == digest_value(unsigned),
            "transparency entry digest mismatch",
        )
        previous_root = root
        records.append(record)
    return records, payloads


def append_log(
    log_path: str | os.PathLike[str],
    leaf: Mapping[str, Any],
    log_private_key: str | os.PathLike[str],
    log_public_key: str | os.PathLike[str],
) -> dict[str, Any]:
    path = Path(log_path)
    parent = path.parent.resolve(strict=True)
    require(parent.is_dir(), "log parent is not a directory")
    lock_path = path.with_name(path.name + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        records, payloads = load_log_records(path, log_public_key)
        require(len(records) < MAX_LOG_ENTRIES, "transparency log is full")
        payload = canonical_bytes(validate_leaf(dict(leaf)))
        payloads.append(payload)
        index = len(records)
        previous_root = (
            records[-1]["tree_head"]["root_hash"]
            if records
            else hashlib.sha256(b"").hexdigest()
        )
        fingerprint = key_fingerprint(log_public_key)
        head = {
            "schema": HEAD_SCHEMA,
            "log_key_sha256": fingerprint,
            "tree_size": index + 1,
            "root_hash": merkle_root(payloads).hex(),
            "previous_tree_size": index,
            "previous_root_hash": previous_root,
        }
        signature = sign_message(
            log_private_key,
            TREE_HEAD_DOMAIN + canonical_bytes(head),
        )
        require(
            verify_signature(
                log_public_key,
                TREE_HEAD_DOMAIN + canonical_bytes(head),
                signature,
            ),
            "private and public transparency-log keys do not match",
        )
        record = {
            "schema": LOG_SCHEMA,
            "index": index,
            "leaf": dict(leaf),
            "leaf_hash": leaf_hash(payload).hex(),
            "tree_head": head,
            "tree_head_signature": base64.b64encode(
                signature).decode("ascii"),
        }
        record["entry_sha256"] = digest_value(record)
        encoded = canonical_bytes(record) + b"\n"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                require(written > 0, "transparency log write failed")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {
            "leaf": dict(leaf),
            "index": index,
            "tree_head": head,
            "tree_head_signature": record["tree_head_signature"],
            "inclusion_proof": [
                item.hex()
                for item in inclusion_proof(payloads, index)
            ],
        }
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def zip_info(name: str, mode: int = 0o644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def write_bundle_archive(
    output_path: str | os.PathLike[str],
    metadata: Mapping[str, Any],
    descriptor: Mapping[str, Any],
    paths: Mapping[str, Path],
) -> None:
    output = Path(output_path)
    parent = output.parent.resolve(strict=True)
    lock_path = output.with_name(output.name + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    temporary: Path | None = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        require(not output.exists(), "bundle output already exists")
        descriptor_fd, name = tempfile.mkstemp(
            prefix=output.name + ".tmp.", dir=parent)
        os.close(descriptor_fd)
        temporary = Path(name)
        written_blobs = set()
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            archive.writestr(
                zip_info("bundle.json"),
                canonical_bytes(metadata) + b"\n",
            )
            for item in descriptor["files"]:
                digest = item["sha256"]
                if digest in written_blobs:
                    continue
                written_blobs.add(digest)
                actual_digest = hashlib.sha256()
                actual_size = 0
                with paths[item["role"]].open("rb") as source:
                    with archive.open(
                            zip_info(item["blob"]), "w") as destination:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            actual_size += len(chunk)
                            require(
                                actual_size <= MAX_FILE_BYTES,
                                "artifact changed beyond byte cap",
                            )
                            actual_digest.update(chunk)
                            destination.write(chunk)
                require(
                    actual_size == item["size"]
                    and actual_digest.hexdigest() == digest,
                    f"{item['role']} changed while bundle was published",
                )
        with temporary.open("rb") as stream:
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


def publish_bundle(
    *,
    seal_path: str | os.PathLike[str],
    artifact_arguments: Mapping[str, Path],
    private_key: str | os.PathLike[str],
    public_key: str | os.PathLike[str],
    log_path: str | os.PathLike[str],
    log_private_key: str | os.PathLike[str],
    log_public_key: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
) -> dict[str, Any]:
    output = Path(output_path)
    publish_lock = output.with_name(output.name + ".publish.lock")
    with exclusive_lock(publish_lock):
        require(not output.exists(), "bundle output already exists")
        descriptor, paths = build_descriptor(
            seal_path, artifact_arguments)
        encoded_descriptor = canonical_bytes(descriptor)
        payload_sha256 = digest_bytes(encoded_descriptor)
        signer_fingerprint = key_fingerprint(public_key)
        signature = sign_message(
            private_key, SIGNATURE_DOMAIN + encoded_descriptor)
        require(
            verify_signature(
                public_key,
                SIGNATURE_DOMAIN + encoded_descriptor,
                signature,
            ),
            "private and public signing keys do not match",
        )
        leaf = log_leaf(payload_sha256, signature, signer_fingerprint)
        transparency = append_log(
            log_path,
            leaf,
            log_private_key,
            log_public_key,
        )
        metadata = {
            "schema": BUNDLE_SCHEMA,
            "descriptor": descriptor,
            "signed_payload_sha256": payload_sha256,
            "signer_key_sha256": signer_fingerprint,
            "signature": base64.b64encode(signature).decode("ascii"),
            "transparency": transparency,
        }
        metadata["bundle_sha256"] = digest_value(metadata)
        write_bundle_archive(
            output_path, metadata, descriptor, paths)
        return metadata


def parse_bundle_metadata(archive: zipfile.ZipFile) -> dict[str, Any]:
    infos = archive.infolist()
    names = [item.filename for item in infos]
    require(len(names) == len(set(names)), "bundle has duplicate ZIP members")
    require("bundle.json" in names, "bundle metadata is missing")
    info = archive.getinfo("bundle.json")
    require(
        0 < info.file_size <= MAX_METADATA_BYTES,
        "bundle metadata size is invalid",
    )
    raw = archive.read(info)
    try:
        metadata = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleError("bundle metadata is not ASCII JSON") from error
    require(isinstance(metadata, dict), "bundle metadata is not an object")
    supplied = metadata.get("bundle_sha256")
    unsigned = dict(metadata)
    unsigned.pop("bundle_sha256", None)
    require(
        supplied == digest_value(unsigned),
        "bundle metadata digest mismatch",
    )
    return metadata


def validate_descriptor(value: Any) -> list[dict[str, Any]]:
    require(isinstance(value, dict), "descriptor is not an object")
    require(
        value.get("schema") == DESCRIPTOR_SCHEMA,
        "unknown descriptor schema",
    )
    require(
        set(value) == {"schema", "pipeline", "seal_sha256", "files"},
        "descriptor has unexpected fields",
    )
    require(
        value.get("pipeline") in ("continuation", "loop", "hydra"),
        "descriptor pipeline is invalid",
    )
    canonical_digest(value.get("seal_sha256"), "seal_sha256")
    files = value.get("files")
    require(
        isinstance(files, list) and 2 <= len(files) <= MAX_FILES,
        "descriptor file count is invalid",
    )
    require(
        [item.get("role") for item in files]
        == sorted(item.get("role") for item in files),
        "descriptor roles are not sorted",
    )
    roles = set()
    names = set()
    total = 0
    for item in files:
        require(isinstance(item, dict), "file descriptor is not an object")
        require(
            set(item)
            == {
                "role",
                "name",
                "transport_name",
                "size",
                "sha256",
                "mode",
                "blob",
            },
            "file descriptor has unexpected fields",
        )
        role = item.get("role")
        require(
            isinstance(role, str)
            and ROLE_RE.fullmatch(role) is not None
            and role not in roles,
            "file role is invalid or duplicated",
        )
        roles.add(role)
        original_name = item.get("name")
        require(
            isinstance(original_name, str)
            and 1 <= len(original_name) <= 255
            and Path(original_name).name == original_name
            and original_name not in (".", ".."),
            "original filename is invalid",
        )
        name = item.get("transport_name")
        require(
            isinstance(name, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", name) is not None
            and name not in names,
            "transport filename is invalid or duplicated",
        )
        names.add(name)
        size = item.get("size")
        require(
            isinstance(size, int)
            and not isinstance(size, bool)
            and 0 <= size <= MAX_FILE_BYTES,
            "file size is invalid",
        )
        total += size
        require(total <= MAX_TOTAL_BYTES, "descriptor byte cap exceeded")
        digest = canonical_digest(item.get("sha256"), "file sha256")
        require(item.get("blob") == f"blobs/{digest}", "blob path mismatch")
        mode = item.get("mode")
        require(
            isinstance(mode, int)
            and not isinstance(mode, bool)
            and 0 <= mode <= 0o7777,
            "file mode is invalid",
        )
    require("seal" in roles, "descriptor has no transformation seal")
    return files


def validate_embedded_seal(
    archive: zipfile.ZipFile,
    descriptor: Mapping[str, Any],
    files: list[dict[str, Any]],
) -> None:
    seal_item = next(item for item in files if item["role"] == "seal")
    require(
        seal_item["size"] <= MAX_METADATA_BYTES,
        "embedded transformation seal is too large",
    )
    raw = archive.read(seal_item["blob"])
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleError("embedded transformation seal is invalid") from error
    require(isinstance(envelope, dict), "embedded seal is not an object")
    require(
        envelope.get("schema") == "symcc-transformation-seal-v1",
        "embedded seal schema is unknown",
    )
    supplied = envelope.get("seal_sha256")
    unsigned = dict(envelope)
    unsigned.pop("seal_sha256", None)
    require(
        supplied == digest_value(unsigned)
        and supplied == descriptor["seal_sha256"],
        "embedded transformation seal digest mismatch",
    )
    expected = expected_artifacts(envelope)
    by_role = {item["role"]: item for item in files}
    require(
        set(by_role) == {"seal", *expected},
        "descriptor roles differ from embedded seal",
    )
    for role, identity in expected.items():
        item = by_role[role]
        require(
            item["size"] == identity.get("size")
            and item["sha256"] == identity.get("sha256"),
            f"{role} differs from embedded transformation seal",
        )


def validate_transparency(
    value: Any,
    *,
    payload_sha256: str,
    signature: bytes,
    signer_fingerprint: str,
    log_public_key: str | os.PathLike[str],
) -> dict[str, Any]:
    require(isinstance(value, dict), "transparency proof is not an object")
    require(
        set(value)
        == {
            "leaf",
            "index",
            "tree_head",
            "tree_head_signature",
            "inclusion_proof",
        },
        "transparency proof has unexpected fields",
    )
    expected_leaf = log_leaf(
        payload_sha256, signature, signer_fingerprint)
    require(value.get("leaf") == expected_leaf, "logged leaf mismatch")
    index = value.get("index")
    require(
        isinstance(index, int) and not isinstance(index, bool) and index >= 0,
        "log index is invalid",
    )
    head = value.get("tree_head")
    require(isinstance(head, dict), "tree head is not an object")
    require(head.get("schema") == HEAD_SCHEMA, "unknown tree head schema")
    require(
        set(head)
        == {
            "schema",
            "log_key_sha256",
            "tree_size",
            "root_hash",
            "previous_tree_size",
            "previous_root_hash",
        },
        "tree head has unexpected fields",
    )
    fingerprint = key_fingerprint(log_public_key)
    require(head.get("log_key_sha256") == fingerprint, "log key mismatch")
    tree_size = head.get("tree_size")
    require(
        isinstance(tree_size, int)
        and not isinstance(tree_size, bool)
        and tree_size == index + 1,
        "tree size does not include the published leaf",
    )
    canonical_digest(head.get("root_hash"), "root_hash")
    canonical_digest(head.get("previous_root_hash"), "previous_root_hash")
    require(
        head.get("previous_tree_size") == index,
        "previous tree size mismatch",
    )
    proof_value = value.get("inclusion_proof")
    require(
        isinstance(proof_value, list) and len(proof_value) <= 64,
        "inclusion proof is invalid",
    )
    proof = []
    for item in proof_value:
        canonical_digest(item, "inclusion node")
        proof.append(bytes.fromhex(item))
    payload = canonical_bytes(expected_leaf)
    root = root_from_inclusion(
        leaf_hash(payload), index, tree_size, proof)
    require(root.hex() == head["root_hash"], "inclusion proof root mismatch")
    head_signature = decode_signature(
        value.get("tree_head_signature"), "tree_head_signature")
    require(
        verify_signature(
            log_public_key,
            TREE_HEAD_DOMAIN + canonical_bytes(head),
            head_signature,
        ),
        "tree head signature is invalid",
    )
    return value


def verify_blob_members(
    archive: zipfile.ZipFile,
    files: list[dict[str, Any]],
) -> None:
    expected = {"bundle.json"} | {item["blob"] for item in files}
    names = {item.filename for item in archive.infolist()}
    require(names == expected, "bundle ZIP members do not match descriptor")
    identities: dict[str, tuple[int, str]] = {}
    for item in files:
        identities.setdefault(
            item["blob"], (item["size"], item["sha256"]))
        require(
            identities[item["blob"]]
            == (item["size"], item["sha256"]),
            "shared blob identity is inconsistent",
        )
    total = 0
    for blob, (expected_size, expected_digest) in identities.items():
        info = archive.getinfo(blob)
        require(
            info.file_size == expected_size,
            f"{blob} uncompressed size mismatch",
        )
        digest = hashlib.sha256()
        size = 0
        with archive.open(blob) as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                total += len(chunk)
                require(
                    size <= MAX_FILE_BYTES and total <= MAX_TOTAL_BYTES,
                    "bundle extraction byte cap exceeded",
                )
                digest.update(chunk)
        require(
            size == expected_size
            and digest.hexdigest() == expected_digest,
            f"{blob} content digest mismatch",
        )


def verify_bundle(
    bundle_path: str | os.PathLike[str],
    *,
    public_key: str | os.PathLike[str],
    log_public_key: str | os.PathLike[str],
    log_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    path = Path(bundle_path).resolve(strict=True)
    require(path.is_file(), "bundle is not a regular file")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            metadata = parse_bundle_metadata(archive)
            require(
                metadata.get("schema") == BUNDLE_SCHEMA,
                "unknown bundle schema",
            )
            require(
                set(metadata)
                == {
                    "schema",
                    "descriptor",
                    "signed_payload_sha256",
                    "signer_key_sha256",
                    "signature",
                    "transparency",
                    "bundle_sha256",
                },
                "bundle metadata has unexpected fields",
            )
            descriptor = metadata.get("descriptor")
            files = validate_descriptor(descriptor)
            encoded_descriptor = canonical_bytes(descriptor)
            payload_sha256 = digest_bytes(encoded_descriptor)
            require(
                metadata.get("signed_payload_sha256") == payload_sha256,
                "signed payload digest mismatch",
            )
            fingerprint = key_fingerprint(public_key)
            require(
                metadata.get("signer_key_sha256") == fingerprint,
                "trusted signer key mismatch",
            )
            signature = decode_signature(
                metadata.get("signature"), "signature")
            require(
                verify_signature(
                    public_key,
                    SIGNATURE_DOMAIN + encoded_descriptor,
                    signature,
                ),
                "artifact signature is invalid",
            )
            transparency = validate_transparency(
                metadata.get("transparency"),
                payload_sha256=payload_sha256,
                signature=signature,
                signer_fingerprint=fingerprint,
                log_public_key=log_public_key,
            )
            verify_blob_members(archive, files)
            validate_embedded_seal(archive, descriptor, files)
    except (zipfile.BadZipFile, OSError) as error:
        raise BundleError(f"bundle archive is invalid: {error}") from error

    if log_path is not None:
        records, _ = load_log_records(log_path, log_public_key)
        index = transparency["index"]
        require(index < len(records), "bundle entry is absent from log")
        record = records[index]
        require(
            record["leaf"] == transparency["leaf"]
            and record["tree_head"] == transparency["tree_head"]
            and record["tree_head_signature"]
            == transparency["tree_head_signature"],
            "bundle transparency proof differs from local log",
        )
    return metadata


def atomic_extract(
    bundle_path: str | os.PathLike[str],
    metadata: Mapping[str, Any],
    output_dir: str | os.PathLike[str],
) -> dict[str, str]:
    output = Path(output_dir)
    parent = output.parent.resolve(strict=True)
    require(parent.is_dir(), "extract parent is not a directory")
    lock_path = output.with_name(output.name + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    temporary: Path | None = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        require(not output.exists(), "extract output already exists")
        temporary = Path(tempfile.mkdtemp(
            prefix=output.name + ".tmp.", dir=parent))
        receipt: dict[str, str] = {}
        with zipfile.ZipFile(bundle_path, "r") as archive:
            current = parse_bundle_metadata(archive)
            require(
                current == metadata,
                "bundle changed between verification and extraction",
            )
            files = validate_descriptor(current["descriptor"])
            expected_members = {
                "bundle.json", *(item["blob"] for item in files)
            }
            member_names = [
                item.filename for item in archive.infolist()
            ]
            require(
                len(member_names) == len(set(member_names))
                and set(member_names) == expected_members,
                "bundle members changed before extraction",
            )
            for item in metadata["descriptor"]["files"]:
                destination = temporary / item["transport_name"]
                digest = hashlib.sha256()
                size = 0
                with archive.open(item["blob"]) as source:
                    with destination.open("xb") as target:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            require(
                                size <= MAX_FILE_BYTES,
                                "extracted file exceeds byte cap",
                            )
                            digest.update(chunk)
                            target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                require(
                    size == item["size"]
                    and digest.hexdigest() == item["sha256"],
                    f"{item['role']} changed before extraction",
                )
                os.chmod(destination, item["mode"] & 0o777)
                receipt[item["role"]] = item["transport_name"]
        receipt_payload = {
            "schema": "symcc-transform-transport-receipt-v1",
            "bundle_sha256": metadata["bundle_sha256"],
            "files": dict(sorted(receipt.items())),
        }
        receipt_path = temporary / "receipt.json"
        with receipt_path.open("xb") as stream:
            stream.write(canonical_bytes(receipt_payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(temporary, output)
        temporary = None
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return receipt
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    keygen = commands.add_parser("keygen")
    keygen.add_argument("--private-key", required=True)
    keygen.add_argument("--public-key", required=True)

    publish = commands.add_parser("publish")
    publish.add_argument("--seal", required=True)
    publish.add_argument("--artifact", action="append", default=[])
    publish.add_argument("--private-key", required=True)
    publish.add_argument("--public-key", required=True)
    publish.add_argument("--log", required=True)
    publish.add_argument("--log-private-key", required=True)
    publish.add_argument("--log-public-key", required=True)
    publish.add_argument("--output", required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("bundle")
    verify.add_argument("--public-key", required=True)
    verify.add_argument("--log-public-key", required=True)
    verify.add_argument("--log")
    verify.add_argument("--extract-dir")

    audit = commands.add_parser("audit-log")
    audit.add_argument("log")
    audit.add_argument("--log-public-key", required=True)

    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "keygen":
            fingerprint = generate_keypair(
                arguments.private_key, arguments.public_key)
            print(json.dumps({
                "algorithm": "Ed25519",
                "public_key_sha256": fingerprint,
            }, sort_keys=True))
            return 0
        if arguments.command == "publish":
            metadata = publish_bundle(
                seal_path=arguments.seal,
                artifact_arguments=parse_artifact_arguments(
                    arguments.artifact),
                private_key=arguments.private_key,
                public_key=arguments.public_key,
                log_path=arguments.log,
                log_private_key=arguments.log_private_key,
                log_public_key=arguments.log_public_key,
                output_path=arguments.output,
            )
            print(json.dumps({
                "bundle_sha256": metadata["bundle_sha256"],
                "log_index": metadata["transparency"]["index"],
            }, sort_keys=True))
            return 0
        if arguments.command == "verify":
            metadata = verify_bundle(
                arguments.bundle,
                public_key=arguments.public_key,
                log_public_key=arguments.log_public_key,
                log_path=arguments.log,
            )
            if arguments.extract_dir:
                atomic_extract(
                    arguments.bundle, metadata, arguments.extract_dir)
            print(json.dumps({
                "bundle_sha256": metadata["bundle_sha256"],
                "files": len(metadata["descriptor"]["files"]),
                "verified": True,
            }, sort_keys=True))
            return 0
        records, _ = load_log_records(
            arguments.log, arguments.log_public_key)
        print(json.dumps({
            "entries": len(records),
            "root_hash": (
                records[-1]["tree_head"]["root_hash"]
                if records
                else hashlib.sha256(b"").hexdigest()
            ),
            "verified": True,
        }, sort_keys=True))
        return 0
    except (
        BundleError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ) as error:
        print(f"transformation bundle failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
