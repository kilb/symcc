#!/usr/bin/env python3
"""Build and validate the canonical pytest node-id inventory."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Sequence

import pytest


SCHEMA = "symcc-pytest-nodeid-manifest-v1"
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_NODEIDS = 1_000_000
_MAX_NODEID_BYTES = 4096


class ManifestError(ValueError):
    """The node-id manifest is malformed or internally inconsistent."""


@dataclass(frozen=True)
class NodeIdManifest:
    root: str
    nodeids: tuple[str, ...]
    sha256: str

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "root": self.root,
            "count": len(self.nodeids),
            "nodeids_sha256": self.sha256,
            "nodeids": list(self.nodeids),
        }


def _validate_root(root: object) -> str:
    if type(root) is not str or not root:
        raise ManifestError("root must be a nonempty string")
    path = PurePosixPath(root)
    if path.is_absolute() or path.as_posix() != root or ".." in path.parts:
        raise ManifestError("root must be a normalized relative POSIX path")
    if root == "." or "\x00" in root or "\n" in root or "\r" in root:
        raise ManifestError("root contains a forbidden component or character")
    return root


def canonical_nodeid_bytes(nodeids: Sequence[str]) -> bytes:
    return "".join(f"{nodeid}\n" for nodeid in nodeids).encode("utf-8")


def nodeid_digest(nodeids: Sequence[str]) -> str:
    return hashlib.sha256(canonical_nodeid_bytes(nodeids)).hexdigest()


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ManifestError(f"duplicate JSON object member {key!r}")
        value[key] = item
    return value


def build_nodeid_manifest(root: str, nodeids: Sequence[str]) -> NodeIdManifest:
    normalized_root = _validate_root(root)
    if len(nodeids) > _MAX_NODEIDS:
        raise ManifestError(f"node-id count exceeds {_MAX_NODEIDS}")

    normalized: list[str] = []
    prefix = f"{normalized_root}/"
    for index, nodeid in enumerate(nodeids):
        if type(nodeid) is not str or not nodeid:
            raise ManifestError(f"nodeids[{index}] must be a nonempty string")
        if any(character in nodeid for character in ("\x00", "\n", "\r")):
            raise ManifestError(f"nodeids[{index}] contains a forbidden character")
        if len(nodeid.encode("utf-8")) > _MAX_NODEID_BYTES:
            raise ManifestError(
                f"nodeids[{index}] exceeds {_MAX_NODEID_BYTES} UTF-8 bytes"
            )
        if not nodeid.startswith(prefix):
            raise ManifestError(
                f"nodeids[{index}] is outside declared root {normalized_root!r}"
            )
        normalized.append(nodeid)

    ordered = tuple(sorted(normalized))
    if len(set(ordered)) != len(ordered):
        raise ManifestError("nodeids must be unique")
    return NodeIdManifest(
        root=normalized_root,
        nodeids=ordered,
        sha256=nodeid_digest(ordered),
    )


def load_nodeid_manifest(path: Path) -> NodeIdManifest:
    try:
        with path.open("rb") as stream:
            encoded = stream.read(_MAX_MANIFEST_BYTES + 1)
    except OSError as error:
        raise ManifestError(f"cannot read manifest: {error}") from error
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"manifest exceeds {_MAX_MANIFEST_BYTES} bytes"
        )
    try:
        text = encoded.decode("utf-8")
        payload = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read manifest: {error}") from error
    if type(payload) is not dict:
        raise ManifestError("manifest must be a JSON object")
    expected_keys = {"schema", "root", "count", "nodeids_sha256", "nodeids"}
    if set(payload) != expected_keys:
        raise ManifestError("manifest fields do not match the v1 schema")
    if payload["schema"] != SCHEMA:
        raise ManifestError(f"unsupported manifest schema {payload['schema']!r}")
    if type(payload["nodeids"]) is not list:
        raise ManifestError("nodeids must be a JSON array")

    manifest = build_nodeid_manifest(payload["root"], payload["nodeids"])
    if tuple(payload["nodeids"]) != manifest.nodeids:
        raise ManifestError("nodeids must be sorted lexicographically")
    if type(payload["count"]) is not int or payload["count"] != len(
        manifest.nodeids
    ):
        raise ManifestError("count does not match nodeids")
    if payload["nodeids_sha256"] != manifest.sha256:
        raise ManifestError("nodeids_sha256 does not match canonical nodeids")
    return manifest


def write_nodeid_manifest(path: Path, manifest: NodeIdManifest) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(manifest.to_json(), indent=2, sort_keys=True) + "\n",
            encoding="ascii",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@dataclass
class CollectionRecorder:
    nodeids: list[str] = field(default_factory=list)
    deselected: int = 0
    collection_errors: int = 0

    def pytest_collection_finish(self, session: object) -> None:
        self.nodeids = [
            str(getattr(item, "nodeid", ""))
            for item in getattr(session, "items", ())
        ]

    def pytest_deselected(self, items: Sequence[object]) -> None:
        self.deselected += len(items)

    def pytest_collectreport(self, report: object) -> None:
        if getattr(report, "failed", False):
            self.collection_errors += 1


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", default="test")
    parser.add_argument("--min-collected", type=_nonnegative, default=1)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    pytest_args = list(arguments.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args.pop(0)
    if not pytest_args:
        pytest_args = ["-p", "no:terminal", "-p", "no:cacheprovider"]

    recorder = CollectionRecorder()
    exit_code = int(
        pytest.main(
            ["--collect-only", *pytest_args, arguments.root],
            plugins=[recorder],
        )
    )
    failures: list[str] = []
    if exit_code != int(pytest.ExitCode.OK):
        failures.append(f"pytest exit code is {exit_code}, expected 0")
    if len(recorder.nodeids) < arguments.min_collected:
        failures.append(
            f"collected {len(recorder.nodeids)} is below floor "
            f"{arguments.min_collected}"
        )
    if recorder.deselected:
        failures.append(f"deselected {recorder.deselected} items")
    if recorder.collection_errors:
        failures.append(f"collection errors observed: {recorder.collection_errors}")

    try:
        manifest = build_nodeid_manifest(arguments.root, recorder.nodeids)
    except ManifestError as error:
        failures.append(str(error))
        manifest = None
    if failures or manifest is None:
        for failure in failures:
            print(f"python-test-inventory: {failure}", file=sys.stderr)
        return 1

    write_nodeid_manifest(arguments.output, manifest)
    print(
        "python-test-inventory: PASS "
        f"(collected={len(manifest.nodeids)}, sha256={manifest.sha256})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
