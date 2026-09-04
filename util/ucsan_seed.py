#!/usr/bin/env python3
"""Create and inspect structured seeds for SymCC UCSan."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
import stat
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MAGIC = b"SYMUCS1\0"
VERSION = 1
ROOT_ENTRY = 1
OBJECT_ENTRY = 2
POINTEE = -(1 << 63)
HEADER = struct.Struct("<8sII")
ENTRY = struct.Struct("<IIQqQ")
I64 = struct.Struct("<q")
MAX_ENTRIES = 65536
MAX_PATH_COMPONENTS = 1024
MAX_GRAPH_BYTES = 64 * 1024 * 1024
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1
U64_MAX = (1 << 64) - 1


@dataclass(frozen=True)
class SeedEntry:
    flags: int
    object_id: int
    lower: int
    path: tuple[int, ...]
    data: bytes


def validate_seed(entries: Iterable[SeedEntry]) -> list[SeedEntry]:
    """Validate the closed v1 object graph without repairing malformed data."""
    materialized = list(entries)
    if len(materialized) > MAX_ENTRIES:
        raise ValueError("seed contains too many object-graph entries")
    roots: set[int] = set()
    paths: set[tuple[int, ...]] = set()
    payload_objects: set[int] = set()
    graph_bytes = HEADER.size
    for entry in materialized:
        if entry.flags not in {ROOT_ENTRY, OBJECT_ENTRY}:
            raise ValueError("seed entry has an unknown or combined kind")
        if not 0 <= entry.object_id <= U64_MAX:
            raise ValueError("object id is outside uint64")
        if not I64_MIN <= entry.lower <= I64_MAX:
            raise ValueError("object lower bound is outside int64")
        if len(entry.path) > MAX_PATH_COMPONENTS:
            raise ValueError("object path exceeds the depth limit")
        if any(not I64_MIN <= component <= I64_MAX for component in entry.path):
            raise ValueError("object path component is outside int64")
        if entry.lower + len(entry.data) > I64_MAX:
            raise ValueError("object upper bound overflows int64")
        graph_bytes += ENTRY.size + len(entry.path) * I64.size + len(entry.data)
        if graph_bytes > MAX_GRAPH_BYTES:
            raise ValueError("seed exceeds the object-graph size limit")

        if entry.flags == ROOT_ENTRY:
            if (
                len(entry.path) != 1
                or entry.lower != 0
                or (entry.path[0] & U64_MAX) != entry.object_id
            ):
                raise ValueError("root entry has an invalid identity or shape")
            if entry.object_id in roots:
                raise ValueError("seed contains a duplicate root")
            roots.add(entry.object_id)
            continue

        if entry.object_id == 0 or not _valid_object_path(entry.path):
            raise ValueError("object entry has an invalid id or path")
        if entry.path in paths:
            raise ValueError("seed contains a duplicate object path")
        paths.add(entry.path)
        if entry.data and entry.object_id in payload_objects:
            raise ValueError("object id has more than one payload")
        if entry.data:
            payload_objects.add(entry.object_id)
    return materialized


def serialize_seed(entries: Iterable[SeedEntry]) -> bytes:
    materialized = validate_seed(entries)
    output = bytearray(HEADER.pack(MAGIC, VERSION, len(materialized)))
    for entry in materialized:
        if len(entry.path) > MAX_PATH_COMPONENTS:
            raise ValueError("object path exceeds the depth limit")
        output.extend(
            ENTRY.pack(
                entry.flags,
                len(entry.path),
                entry.object_id,
                entry.lower,
                len(entry.data),
            )
        )
        for component in entry.path:
            output.extend(I64.pack(component))
        output.extend(entry.data)
        if len(output) > MAX_GRAPH_BYTES:
            raise ValueError("seed exceeds the object-graph size limit")
    return bytes(output)


def write_seed(path: Path, entries: Iterable[SeedEntry]) -> None:
    payload = serialize_seed(entries)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def parse_seed(raw: bytes) -> list[SeedEntry]:
    if len(raw) > MAX_GRAPH_BYTES:
        raise ValueError("seed exceeds the object-graph size limit")
    if len(raw) < HEADER.size:
        raise ValueError("seed is shorter than its header")
    magic, version, count = HEADER.unpack_from(raw)
    if magic != MAGIC or version != VERSION:
        raise ValueError("unsupported UCSan seed")
    if count > MAX_ENTRIES:
        raise ValueError("seed contains too many object-graph entries")
    offset = HEADER.size
    result = []
    for _ in range(count):
        if offset + ENTRY.size > len(raw):
            raise ValueError("truncated entry header")
        flags, path_length, object_id, lower, size = ENTRY.unpack_from(raw, offset)
        offset += ENTRY.size
        if path_length > MAX_PATH_COMPONENTS:
            raise ValueError("object path exceeds the depth limit")
        path_size = path_length * I64.size
        if offset + path_size + size > len(raw):
            raise ValueError("truncated entry")
        components = tuple(
            I64.unpack_from(raw, offset + index * I64.size)[0]
            for index in range(path_length)
        )
        offset += path_size
        data = raw[offset : offset + size]
        offset += size
        result.append(SeedEntry(flags, object_id, lower, components, data))
    if offset != len(raw):
        raise ValueError("trailing bytes after UCSan seed")
    return validate_seed(result)


def read_seed(path: Path) -> list[SeedEntry]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("O_NOFOLLOW is required for UCSan seed admission")
    descriptor = os.open(path, flags | nofollow)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("UCSan seed is not a regular file")
        if before.st_size > MAX_GRAPH_BYTES:
            raise ValueError("seed exceeds the object-graph size limit")
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("seed changed while it was read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)

        def identity(value: os.stat_result) -> tuple[int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
            )

        if identity(before) != identity(after):
            raise ValueError("seed changed while it was read")
        return parse_seed(b"".join(chunks))
    finally:
        os.close(descriptor)


def _valid_object_path(path: tuple[int, ...]) -> bool:
    return len(path) >= 2 and path[1] == POINTEE


def canonicalize(entries: Iterable[SeedEntry]) -> list[SeedEntry]:
    """Normalize IDs/order while preserving every path and alias relation."""
    entries = validate_seed(entries)
    roots: dict[int, SeedEntry] = {}
    path_entries: dict[tuple[int, ...], SeedEntry] = {}
    object_payloads: dict[int, tuple[int, bytes]] = {}
    for entry in entries:
        if entry.flags & ROOT_ENTRY:
            if len(entry.path) == 1:
                roots.setdefault(entry.path[0], entry)
            continue
        if (
            not (entry.flags & OBJECT_ENTRY)
            or not _valid_object_path(entry.path)
            or entry.object_id <= 0
        ):
            continue
        path_entries.setdefault(entry.path, entry)
        if entry.data:
            current = object_payloads.get(entry.object_id)
            if current is None or len(entry.data) > len(current[1]):
                object_payloads[entry.object_id] = (entry.lower, entry.data)

    groups: dict[int, list[tuple[int, ...]]] = defaultdict(list)
    for path, entry in path_entries.items():
        groups[entry.object_id].append(path)
    ordered_groups = sorted(groups.items(), key=lambda item: min(item[1]))
    remapped = {
        old_id: index + 1 for index, (old_id, _paths) in enumerate(ordered_groups)
    }

    result = [
        SeedEntry(ROOT_ENTRY, root_id, 0, (root_id,), roots[root_id].data)
        for root_id in sorted(roots)
    ]
    for old_id, paths in ordered_groups:
        payload = object_payloads.get(old_id, (0, b""))
        sorted_paths = sorted(paths)
        data_path = sorted_paths[0]
        emission_order = [data_path] + [
            path for path in sorted_paths if path != data_path
        ]
        for index, path in enumerate(emission_order):
            lower, data = payload if index == 0 else (0, b"")
            result.append(SeedEntry(OBJECT_ENTRY, remapped[old_id], lower, path, data))
    return result


class _DisjointPaths:
    def __init__(self, paths: Iterable[tuple[int, ...]]) -> None:
        self.parent = {path: path for path in paths}

    def find(self, path: tuple[int, ...]) -> tuple[int, ...]:
        parent = self.parent[path]
        while parent != self.parent[parent]:
            parent = self.parent[parent]
        while path != parent:
            next_path = self.parent[path]
            self.parent[path] = parent
            path = next_path
        return parent

    def union(self, left: tuple[int, ...], right: tuple[int, ...]) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        self.parent[second] = first


def learn_object_graph(
    seed_sets: Iterable[Iterable[SeedEntry]],
    *,
    min_support: int = 0,
) -> list[SeedEntry]:
    """Infer stable paths and alias classes from concrete UCSan graph dumps."""
    materialized = [canonicalize(entries) for entries in seed_sets]
    if not materialized:
        return []
    threshold = (
        max(1, int(min_support))
        if min_support
        else max(1, math.ceil(len(materialized) / 2))
    )

    root_values: dict[int, Counter[bytes]] = defaultdict(Counter)
    path_support: Counter[tuple[int, ...]] = Counter()
    seed_objects: list[dict[tuple[int, ...], int]] = []
    seed_payloads: list[dict[int, tuple[int, bytes]]] = []
    for entries in materialized:
        object_paths: dict[tuple[int, ...], int] = {}
        payloads: dict[int, tuple[int, bytes]] = {}
        for entry in entries:
            if entry.flags & ROOT_ENTRY and len(entry.path) == 1:
                root_values[entry.path[0]][entry.data] += 1
            elif entry.flags & OBJECT_ENTRY:
                object_paths[entry.path] = entry.object_id
                path_support[entry.path] += 1
                if entry.data:
                    payloads[entry.object_id] = (entry.lower, entry.data)
        seed_objects.append(object_paths)
        seed_payloads.append(payloads)

    stable_paths = sorted(
        path
        for path, support in path_support.items()
        if support >= threshold and _valid_object_path(path)
    )
    disjoint = _DisjointPaths(stable_paths)
    for index, left in enumerate(stable_paths):
        for right in stable_paths[index + 1 :]:
            cooccurrences = 0
            aliases = 0
            for objects in seed_objects:
                if left not in objects or right not in objects:
                    continue
                cooccurrences += 1
                aliases += int(objects[left] == objects[right])
            if (
                cooccurrences >= threshold
                and aliases >= threshold
                and aliases * 2 > cooccurrences
            ):
                disjoint.union(left, right)

    groups: dict[tuple[int, ...], list[tuple[int, ...]]] = defaultdict(list)
    for path in stable_paths:
        groups[disjoint.find(path)].append(path)

    learned: list[SeedEntry] = []
    for root_id in sorted(root_values):
        data, support = root_values[root_id].most_common(1)[0]
        if support >= threshold:
            learned.append(SeedEntry(ROOT_ENTRY, root_id, 0, (root_id,), data))

    ordered_groups = sorted(
        (sorted(paths) for paths in groups.values()), key=lambda paths: paths[0]
    )
    for object_id, paths in enumerate(ordered_groups, 1):
        candidates: Counter[tuple[int, bytes]] = Counter()
        for objects, payloads in zip(seed_objects, seed_payloads):
            source_ids = {objects[path] for path in paths if path in objects}
            for source_id in source_ids:
                payload = payloads.get(source_id)
                if payload is not None:
                    candidates[payload] += 1
        payload = candidates.most_common(1)[0][0] if candidates else (0, b"")
        for index, path in enumerate(paths):
            lower, data = payload if index == 0 else (0, b"")
            learned.append(SeedEntry(OBJECT_ENTRY, object_id, lower, path, data))
    return canonicalize(learned)


def parse_assignment(value: str) -> tuple[str, bytes]:
    try:
        name, encoded = value.split("=", 1)
        return name, bytes.fromhex(encoded)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected NAME=HEX_BYTES") from error


def parse_object_path(value: str) -> tuple[tuple[int, ...], int]:
    path_text, separator, lower_text = value.partition("@")
    try:
        components = [int(piece, 0) for piece in path_text.split("/") if piece]
        if not components:
            raise ValueError
        lower = int(lower_text, 0) if separator else 0
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "object path must be ROOT[/POINTER_OFFSET...][@LOWER]"
        ) from error
    return (components[0], POINTEE, *components[1:]), lower


def parse_alias(value: str) -> tuple[tuple[int, ...], int, int]:
    try:
        path_text, object_text = value.rsplit("=", 1)
        path, lower = parse_object_path(path_text)
        object_id = int(object_text, 0)
        if object_id <= 0:
            raise ValueError
    except (ValueError, argparse.ArgumentTypeError) as error:
        raise argparse.ArgumentTypeError(
            "alias must be PATH[@LOWER]=POSITIVE_OBJECT_ID"
        ) from error
    return path, lower, object_id


def create(args: argparse.Namespace) -> None:
    entries: list[SeedEntry] = []
    for assignment in args.root:
        root_text, data = parse_assignment(assignment)
        root_id = int(root_text, 0)
        entries.append(SeedEntry(ROOT_ENTRY, root_id, 0, (root_id,), data))
    next_object_id = 1
    for assignment in args.object:
        path_text, data = parse_assignment(assignment)
        path, lower = parse_object_path(path_text)
        entries.append(SeedEntry(OBJECT_ENTRY, next_object_id, lower, path, data))
        next_object_id += 1
    for assignment in args.alias:
        path, lower, object_id = parse_alias(assignment)
        entries.append(SeedEntry(OBJECT_ENTRY, object_id, lower, path, b""))
    write_seed(args.output, entries)


def inspect(args: argparse.Namespace) -> None:
    rendered = []
    materialized_objects: set[int] = set()
    for entry in read_seed(args.seed):
        if entry.flags & ROOT_ENTRY:
            kind = "root"
            path = str(entry.path[0])
        else:
            kind = (
                "alias"
                if not entry.data and entry.object_id in materialized_objects
                else "object"
            )
            visible = (entry.path[0], *entry.path[2:])
            path = "/".join(str(component) for component in visible)
            if entry.data:
                materialized_objects.add(entry.object_id)
        rendered.append(
            {
                "kind": kind,
                "object_id": entry.object_id,
                "path": path,
                "lower": entry.lower,
                "size": len(entry.data),
                "data": entry.data.hex(),
            }
        )
    print(json.dumps(rendered, indent=2, sort_keys=True))


def normalize(args: argparse.Namespace) -> None:
    write_seed(args.output, canonicalize(read_seed(args.seed)))


def verify(args: argparse.Namespace) -> None:
    raw_entries = read_seed(args.seed)
    canonical_entries = canonicalize(raw_entries)
    canonical = raw_entries == canonical_entries
    if args.require_canonical and not canonical:
        raise SystemExit("UCSan snapshot is valid but non-canonical")
    raw = serialize_seed(raw_entries)
    object_ids = {
        entry.object_id for entry in raw_entries if entry.flags == OBJECT_ENTRY
    }
    report = {
        "schema": "symcc-ucsan-snapshot-verification-v1",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "canonical": canonical,
        "entries": len(raw_entries),
        "roots": sum(entry.flags == ROOT_ENTRY for entry in raw_entries),
        "objects": len(object_ids),
        "paths": sum(entry.flags == OBJECT_ENTRY for entry in raw_entries),
        "materialized_object_bytes": sum(
            len(entry.data) for entry in raw_entries if entry.flags == OBJECT_ENTRY
        ),
        "serialized_bytes": len(raw),
    }
    print(json.dumps(report, sort_keys=True))


def learn(args: argparse.Namespace) -> None:
    seeds = [read_seed(path) for path in args.seeds]
    learned = learn_object_graph(seeds, min_support=args.min_support)
    if not learned:
        raise SystemExit("no object-graph entries met the support threshold")
    write_seed(args.output, learned)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create")
    create_parser.add_argument("output", type=Path)
    create_parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="ID=HEX",
        help="serialized entry argument or root value",
    )
    create_parser.add_argument(
        "--object",
        action="append",
        default=[],
        metavar="PATH[@LOWER]=HEX",
        help="object reached from ROOT[/POINTER_OFFSET...]",
    )
    create_parser.add_argument(
        "--alias",
        action="append",
        default=[],
        metavar="PATH[@LOWER]=OBJECT_ID",
        help=(
            "map another pointer path to an existing object id; repeated ids "
            "encode sharing and paths below the same id encode cycles"
        ),
    )
    create_parser.set_defaults(handler=create)

    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("seed", type=Path)
    inspect_parser.set_defaults(handler=inspect)

    normalize_parser = commands.add_parser("normalize")
    normalize_parser.add_argument("seed", type=Path)
    normalize_parser.add_argument("output", type=Path)
    normalize_parser.set_defaults(handler=normalize)

    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("seed", type=Path)
    verify_parser.add_argument("--require-canonical", action="store_true")
    verify_parser.set_defaults(handler=verify)

    learn_parser = commands.add_parser("learn")
    learn_parser.add_argument("output", type=Path)
    learn_parser.add_argument("seeds", type=Path, nargs="+")
    learn_parser.add_argument(
        "--min-support",
        type=int,
        default=0,
        help="minimum dumps containing a root/path (default: majority)",
    )
    learn_parser.set_defaults(handler=learn)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
