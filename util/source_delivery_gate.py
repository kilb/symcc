#!/usr/bin/env python3
"""Seal and verify the executable source footprint of a SymCC checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
from typing import Any, Iterable


SCHEMA = "symcc-source-delivery-manifest-v2"
MANIFEST_NAME = "source-delivery-manifest.json"
SOURCE_SUFFIXES = frozenset({
    ".c", ".cc", ".cfg", ".cnf", ".cpp", ".h", ".hpp", ".in", ".inc",
    ".json", ".ll", ".lock", ".patch", ".py", ".rs", ".sh", ".test32",
    ".toml", ".yaml", ".yml",
})
SOURCE_BASENAMES = frozenset({"CMakeLists.txt", "Cargo.lock", "Cargo.toml"})
RECURSIVE_ROOTS = (
    ".github/workflows",
    "compiler",
    "util",
    "test",
    "scripts",
    "benchmark/harnesses",
    "benchmark/qa3_repro",
)
ROOT_FILES = (
    ".adacore-gitlab-ci.yml",
    ".gitmodules",
    ".dockerignore",
    "CMakeLists.txt",
    "Dockerfile",
    "Vagrantfile",
    "build.sh",
    "package.sh",
    "pytest.ini",
    "requirements.txt",
    "requirements-test.txt",
    "sample.cpp",
    "setup.sh",
)


def _policy() -> dict[str, Any]:
    return {
        "recursive_roots": list(RECURSIVE_ROOTS),
        "root_files": list(ROOT_FILES),
        "source_suffixes": sorted(SOURCE_SUFFIXES),
        "source_basenames": sorted(SOURCE_BASENAMES),
        "benchmark_top_level": True,
        "gitlinks": True,
    }


class DeliveryManifestError(ValueError):
    pass


def _git(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise DeliveryManifestError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _source_candidates(root: Path) -> Iterable[Path]:
    for name in ROOT_FILES:
        path = root / name
        if not path.exists() and not path.is_symlink():
            raise DeliveryManifestError(
                f"required source footprint member is missing: {name}"
            )
        yield path
    benchmark = root / "benchmark"
    if not benchmark.is_dir() or benchmark.is_symlink():
        raise DeliveryManifestError(
            "required source footprint directory is missing: benchmark"
        )
    for path in benchmark.iterdir():
        if path.suffix in SOURCE_SUFFIXES or path.name in SOURCE_BASENAMES:
            yield path
    for name in RECURSIVE_ROOTS:
        directory = root / name
        if not directory.is_dir() or directory.is_symlink():
            raise DeliveryManifestError(
                f"required source footprint directory is missing: {name}"
            )
        for path in directory.rglob("*"):
            if "__pycache__" in path.parts or (
                path.suffix not in SOURCE_SUFFIXES
                and path.name not in SOURCE_BASENAMES
            ):
                continue
            yield path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover(root: Path) -> list[dict[str, Any]]:
    root = root.resolve()
    discovered = []
    seen: set[str] = set()
    for path in _source_candidates(root):
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise DeliveryManifestError(
                f"source footprint member is not a regular file: {relative}"
            )
        discovered.append({
            "path": relative,
            "bytes": metadata.st_size,
            "sha256": _sha256(path),
        })
    return sorted(discovered, key=lambda row: row["path"])


def discover_gitlinks(root: Path) -> list[dict[str, str]]:
    """Return tracked submodule identities from the Git index."""
    output = _git(root, "ls-files", "--stage", "-z")
    gitlinks: list[dict[str, str]] = []
    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, object_id, stage = metadata.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except (UnicodeError, ValueError) as error:
            raise DeliveryManifestError("git index contains a malformed entry") from error
        if mode != "160000":
            continue
        if stage != "0":
            raise DeliveryManifestError(f"gitlink has an unresolved stage: {path}")
        checkout_id = _git(root / path, "rev-parse", "--verify", "HEAD").decode(
            "ascii"
        ).strip()
        if checkout_id != object_id:
            raise DeliveryManifestError(
                f"gitlink checkout does not match the index: {path}"
            )
        gitlinks.append({"path": path, "object": object_id})
    return sorted(gitlinks, key=lambda row: row["path"])


def _tree_digest(
    files: list[dict[str, Any]], gitlinks: list[dict[str, str]],
) -> str:
    content = json.dumps(
        {"files": files, "gitlinks": gitlinks},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(content).hexdigest()


def build_manifest(root: Path) -> dict[str, Any]:
    files = discover(root)
    gitlinks = discover_gitlinks(root)
    return {
        "schema": SCHEMA,
        "policy": _policy(),
        "files": files,
        "file_count": len(files),
        "gitlinks": gitlinks,
        "gitlink_count": len(gitlinks),
        "tree_sha256": _tree_digest(files, gitlinks),
    }


def _reject_constant(value: str) -> None:
    raise DeliveryManifestError(f"non-finite JSON constant {value}")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DeliveryManifestError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(
            path.read_text(encoding="ascii"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DeliveryManifestError("source delivery manifest is unreadable") from error
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema", "policy", "files", "file_count", "gitlinks",
        "gitlink_count", "tree_sha256",
    }:
        raise DeliveryManifestError("source delivery manifest envelope is invalid")
    if manifest["schema"] != SCHEMA:
        raise DeliveryManifestError("source delivery manifest schema is invalid")
    if manifest["policy"] != _policy():
        raise DeliveryManifestError("source delivery discovery policy changed")
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != manifest["file_count"]:
        raise DeliveryManifestError("source delivery file count is invalid")
    previous = ""
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
            raise DeliveryManifestError("source delivery member is invalid")
        relative = row["path"]
        if (
            not isinstance(relative, str)
            or not relative
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or relative <= previous
            or type(row["bytes"]) is not int
            or row["bytes"] < 0
            or not isinstance(row["sha256"], str)
            or len(row["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in row["sha256"])
        ):
            raise DeliveryManifestError("source delivery member is not canonical")
        previous = relative
    gitlinks = manifest["gitlinks"]
    if not isinstance(gitlinks, list) or len(gitlinks) != manifest["gitlink_count"]:
        raise DeliveryManifestError("source delivery gitlink count is invalid")
    previous = ""
    for row in gitlinks:
        if not isinstance(row, dict) or set(row) != {"path", "object"}:
            raise DeliveryManifestError("source delivery gitlink is invalid")
        relative = row["path"]
        object_id = row["object"]
        if (
            not isinstance(relative, str)
            or not relative
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or relative <= previous
            or not isinstance(object_id, str)
            or len(object_id) not in {40, 64}
            or any(char not in "0123456789abcdef" for char in object_id)
        ):
            raise DeliveryManifestError("source delivery gitlink is not canonical")
        previous = relative
    if manifest["tree_sha256"] != _tree_digest(files, gitlinks):
        raise DeliveryManifestError("source delivery tree digest is invalid")
    return manifest


def write_manifest(root: Path, path: Path) -> dict[str, Any]:
    manifest = build_manifest(root)
    content = (
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o644,
    )
    try:
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short source delivery manifest write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return manifest


def verify(
    root: Path,
    manifest_path: Path,
    *,
    require_tracked: bool,
    require_clean: bool,
) -> dict[str, Any]:
    expected = load_manifest(manifest_path)
    observed_files = discover(root)
    observed_gitlinks = discover_gitlinks(root)
    expected_by_path = {row["path"]: row for row in expected["files"]}
    observed_by_path = {row["path"]: row for row in observed_files}
    missing = sorted(set(expected_by_path) - set(observed_by_path))
    unexpected = sorted(set(observed_by_path) - set(expected_by_path))
    changed = sorted(
        path for path in set(expected_by_path) & set(observed_by_path)
        if expected_by_path[path] != observed_by_path[path]
    )
    expected_gitlinks = {row["path"]: row["object"] for row in expected["gitlinks"]}
    observed_gitlinks_by_path = {
        row["path"]: row["object"] for row in observed_gitlinks
    }
    missing_gitlinks = sorted(set(expected_gitlinks) - set(observed_gitlinks_by_path))
    unexpected_gitlinks = sorted(set(observed_gitlinks_by_path) - set(expected_gitlinks))
    changed_gitlinks = sorted(
        path
        for path in set(expected_gitlinks) & set(observed_gitlinks_by_path)
        if expected_gitlinks[path] != observed_gitlinks_by_path[path]
    )
    tracked_missing: list[str] = []
    dirty: list[str] = []
    manifest_relative = manifest_path.resolve().relative_to(root.resolve()).as_posix()
    protected = {*expected_by_path, *expected_gitlinks, manifest_relative}
    if require_tracked:
        tracked = set(
            _git(root, "ls-files", "--cached", "-z").decode("utf-8").split("\0")
        )
        tracked_missing = sorted(protected - tracked)
    if require_clean:
        modified = set(
            _git(root, "diff", "--name-only", "-z", "HEAD", "--")
            .decode("utf-8").split("\0")
        )
        dirty = sorted(protected & modified)
    matched = not any((
        missing,
        unexpected,
        changed,
        missing_gitlinks,
        unexpected_gitlinks,
        changed_gitlinks,
        tracked_missing,
        dirty,
    ))
    return {
        "schema": SCHEMA,
        "matched": matched,
        "file_count": len(observed_files),
        "gitlink_count": len(observed_gitlinks),
        "tree_sha256": _tree_digest(observed_files, observed_gitlinks),
        "missing": missing,
        "unexpected": unexpected,
        "changed": changed,
        "missing_gitlinks": missing_gitlinks,
        "unexpected_gitlinks": unexpected_gitlinks,
        "changed_gitlinks": changed_gitlinks,
        "untracked": tracked_missing,
        "dirty": dirty,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--require-tracked", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = (
        args.manifest.resolve()
        if args.manifest is not None
        else root / MANIFEST_NAME
    )
    if args.write:
        payload = write_manifest(root, manifest)
        print(json.dumps({
            "manifest": str(manifest),
            "file_count": payload["file_count"],
            "gitlink_count": payload["gitlink_count"],
            "tree_sha256": payload["tree_sha256"],
        }, sort_keys=True))
        return 0
    try:
        result = verify(
            root,
            manifest,
            require_tracked=args.require_tracked,
            require_clean=args.require_clean,
        )
    except DeliveryManifestError as error:
        print(json.dumps({"matched": False, "error": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["matched"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
