#!/usr/bin/env python3
"""Reproducible, randomized benchmark protocol for SymCC experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import resource
import shlex
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


PROTOCOL_SCHEMA = "symcc-research-protocol-v1"
RESULT_SCHEMA = "symcc-research-run-result-v1"
MIN_CONFIRMATORY_REPEATS = 20
PCFG_RESEARCH_ARTIFACT_SCHEMA = "symcc-pcfg-research-artifact-v1"
PARSER_RESEARCH_ARTIFACT_SCHEMA = "symcc-parser-research-artifact-v1"
PCFG_CONTEXT_LEVELS = (
    "global",
    "parent",
    "circuit",
    "sibling",
    "history",
)


def _pcfg_context_order(value: str | int) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid PCFG context level")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in PCFG_CONTEXT_LEVELS:
            return PCFG_CONTEXT_LEVELS.index(normalized)
        value = normalized
    try:
        order = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid PCFG context level {value!r}") from exc
    if not 0 <= order < len(PCFG_CONTEXT_LEVELS):
        raise ValueError("PCFG context level must be between 0 and 4")
    return order


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def content_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def parser_command_digest(command: str) -> str:
    try:
        arguments = shlex.split(str(command)) if str(command).strip() else []
    except ValueError as exc:
        raise ValueError("invalid parser command quoting") from exc
    return hashlib.sha256(json.dumps(
        arguments,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")).hexdigest()


def file_digest(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Hash a file or directory using relative names, types and contents."""
    root = Path(path).resolve()
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    paths = [root] if root.is_file() or root.is_symlink() else sorted(
        candidate for candidate in root.rglob("*")
        if candidate.is_file() or candidate.is_symlink()
    )
    for candidate in paths:
        relative = (
            candidate.name if candidate == root
            else candidate.relative_to(root).as_posix()
        )
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        if candidate.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(candidate).encode(
                "utf-8", errors="surrogateescape"))
            continue
        size = candidate.stat().st_size
        digest.update(b"file\0")
        digest.update(
            f"{candidate.stat().st_mode & 0o777:o}".encode("ascii") + b"\0")
        digest.update(str(size).encode("ascii") + b"\0")
        digest.update(file_digest(candidate).encode("ascii"))
        files += 1
        total_bytes += size
    return {
        "sha256": digest.hexdigest(),
        "files": files,
        "bytes": total_bytes,
    }


def run_artifact_digest(run_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Hash every raw run artifact except the self-referential result record."""
    root = Path(run_dir).resolve()
    aggregate = hashlib.sha256()
    files = 0
    total_bytes = 0
    for candidate in sorted(
        path for path in root.rglob("*")
        if (path.is_file() or path.is_symlink())
        and path.relative_to(root).as_posix() != "result.json"
    ):
        relative = candidate.relative_to(root).as_posix()
        identity = tree_digest(candidate)
        aggregate.update(
            relative.encode("utf-8", errors="surrogateescape") + b"\0"
        )
        aggregate.update(identity["sha256"].encode("ascii"))
        files += int(identity["files"])
        total_bytes += int(identity["bytes"])
    return {
        "sha256": aggregate.hexdigest(),
        "files": files,
        "bytes": total_bytes,
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _command_version(command: str) -> str:
    try:
        completed = subprocess.run(
            [command, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    output = completed.stdout or completed.stderr
    return output.splitlines()[0][:512] if output else ""


def collect_provenance(
    repo_root: str | os.PathLike[str],
    inputs: Iterable[str | os.PathLike[str]] = (),
) -> dict[str, Any]:
    """Collect immutable source/input identities and execution environment."""
    root = Path(repo_root).resolve()

    def git(*arguments: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def working_tree_identity(repository: Path) -> tuple[str, int]:
        try:
            listed = subprocess.run(
                [
                    "git", "-C", str(repository), "ls-files", "-co",
                    "--exclude-standard", "-z",
                ],
                check=False,
                capture_output=True,
                timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            listed = b""
        relative_paths = [
            item.decode("utf-8", errors="surrogateescape")
            for item in listed.split(b"\0") if item
        ]
        aggregate = hashlib.sha256()
        files = 0
        for relative in sorted(set(relative_paths)):
            candidate = repository / relative
            if not candidate.is_file() and not candidate.is_symlink():
                continue
            identity = tree_digest(candidate)
            aggregate.update(
                relative.encode("utf-8", errors="surrogateescape") + b"\0"
            )
            aggregate.update(identity["sha256"].encode("ascii"))
            files += 1
        return (aggregate.hexdigest(), files)

    status = git("status", "--porcelain=v1", "--untracked-files=all")
    diff = b""
    try:
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--binary", "HEAD", "--"],
            check=False,
            capture_output=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pass

    root_worktree_digest, root_worktree_files = working_tree_identity(root)
    submodule_status_lines: list[str] = []
    try:
        submodule_status_lines = subprocess.run(
            [
                "git", "-C", str(root), "submodule", "status", "--recursive",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        pass

    submodules: list[dict[str, Any]] = []
    submodule_paths = []
    for line in submodule_status_lines:
        fields = line.lstrip(" +-U").split()
        if len(fields) >= 2:
            submodule_paths.append(fields[1])
    for relative in sorted(submodule_paths):
        subroot = root / relative
        if not subroot.is_dir():
            continue
        try:
            sub_status = subprocess.run(
                ["git", "-C", str(subroot), "status", "--porcelain=v1",
                 "--untracked-files=all"],
                check=False, capture_output=True, timeout=15,
            ).stdout
            sub_diff = subprocess.run(
                ["git", "-C", str(subroot), "diff", "--binary", "HEAD", "--"],
                check=False, capture_output=True, timeout=30,
            ).stdout
            sub_head = subprocess.run(
                ["git", "-C", str(subroot), "rev-parse", "HEAD"],
                check=False, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        sub_worktree_digest, sub_worktree_files = working_tree_identity(subroot)
        submodules.append({
            "path": relative,
            "commit": sub_head,
            "dirty": bool(sub_status),
            "status_sha256": hashlib.sha256(sub_status).hexdigest(),
            "diff_sha256": hashlib.sha256(sub_diff).hexdigest(),
            "working_tree_sha256": sub_worktree_digest,
            "working_tree_files": sub_worktree_files,
        })

    input_rows = []
    for raw_path in sorted({str(Path(path).resolve()) for path in inputs}):
        path = Path(raw_path)
        exists = path.is_file() or path.is_dir() or path.is_symlink()
        row: dict[str, Any] = {"path": raw_path, "exists": exists}
        if exists:
            row.update(tree_digest(path))
            row["kind"] = (
                "symlink" if path.is_symlink()
                else "directory" if path.is_dir()
                else "file"
            )
        input_rows.append(row)

    cpu_model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text(
                encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                cpu_model = line.partition(":")[2].strip()
                break
    except OSError:
        pass

    return {
        "repository": str(root),
        "git_commit": git("rev-parse", "HEAD"),
        "git_dirty": bool(status),
        "git_status_sha256": hashlib.sha256(
            status.encode("utf-8")).hexdigest(),
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "working_tree_sha256": root_worktree_digest,
        "working_tree_files": root_worktree_files,
        "submodules": submodules,
        "platform": platform.platform(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_model": cpu_model,
        "logical_cpus": os.cpu_count() or 1,
        "python": sys.version.splitlines()[0],
        "tool_versions": {
            name: _command_version(name) for name in ("cc", "clang", "cmake")
        },
        "container_image": (
            os.environ.get("CONTAINER_IMAGE_DIGEST")
            or os.environ.get("IMAGE_DIGEST")
            or ""
        ),
        "inputs": input_rows,
    }


def _normalize_configuration(raw: Mapping[str, Any]) -> dict[str, Any]:
    name = str(raw.get("name", "")).strip()
    command = raw.get("command")
    if not name or len(name) > 128:
        raise ValueError("each configuration needs a bounded non-empty name")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(value, str) and value for value in command)
    ):
        raise ValueError(f"configuration {name!r} needs a command argv list")
    try:
        cpu_cores = max(1, int(raw.get("cpu_cores", 1)))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid cpu_cores for {name!r}") from exc
    environment = raw.get("environment", {})
    if not isinstance(environment, dict):
        raise ValueError(f"invalid environment for {name!r}")
    try:
        wall_grace = float(raw.get("wall_grace_seconds", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid wall_grace_seconds for {name!r}") from exc
    if not math.isfinite(wall_grace) or wall_grace < 0 or wall_grace > 3600:
        raise ValueError(f"invalid wall_grace_seconds for {name!r}")
    return {
        "name": name,
        "command": list(command),
        "environment": {
            str(key): str(value)
            for key, value in sorted(environment.items())
        },
        "cpu_cores": cpu_cores,
        "wall_grace_seconds": wall_grace,
    }


def pcfg_context_ablation_configurations(
    base_configuration: Mapping[str, Any],
    *,
    levels: Iterable[str | int] = PCFG_CONTEXT_LEVELS,
    name_prefix: str = "pcfg",
) -> list[dict[str, Any]]:
    """Expand one command into a cost-faithful five-level PCFG ablation."""
    prefix = str(name_prefix).strip()
    if not prefix or len(prefix) > 96:
        raise ValueError("PCFG ablation name_prefix must be non-empty")
    normalized_levels: list[tuple[int, str]] = []
    for raw_level in levels:
        order = _pcfg_context_order(raw_level)
        normalized_levels.append((order, PCFG_CONTEXT_LEVELS[order]))
    if (
        not normalized_levels or
        len({order for order, _ in normalized_levels}) !=
        len(normalized_levels)
    ):
        raise ValueError("PCFG ablation levels must be non-empty and unique")
    base_environment = base_configuration.get("environment", {})
    if not isinstance(base_environment, Mapping):
        raise ValueError("PCFG base environment must be an object")
    expanded: list[dict[str, Any]] = []
    for order, level in normalized_levels:
        environment = dict(base_environment)
        environment.update({
            "SYMCC_PCFG_CONTEXT_ORDER": str(order),
            "SYMCC_PCFG_REQUIRE_ARTIFACT": "1",
        })
        expanded.append(_normalize_configuration({
            **base_configuration,
            "name": f"{prefix}-{level}",
            "command": list(base_configuration.get("command", ())),
            "environment": environment,
        }))
    return expanded


def parser_incremental_ablation_configurations(
    base_configuration: Mapping[str, Any],
    *,
    name_prefix: str = "parser",
) -> list[dict[str, Any]]:
    """Expand one parser command into equal-CPU cold/incremental cells."""
    prefix = str(name_prefix).strip()
    if not prefix or len(prefix) > 96:
        raise ValueError("parser ablation name_prefix must be non-empty")
    base_environment = base_configuration.get("environment", {})
    if not isinstance(base_environment, Mapping):
        raise ValueError("parser base environment must be an object")
    expanded = []
    for enabled, level in ((False, "cold"), (True, "incremental")):
        environment = dict(base_environment)
        environment.update({
            "SYMCC_PROPOSAL_PARSER_CACHE": "1" if enabled else "0",
            "SYMCC_PARSER_REQUIRE_ARTIFACT": "1",
        })
        expanded.append(_normalize_configuration({
            **base_configuration,
            "name": f"{prefix}-{level}",
            "command": list(base_configuration.get("command", ())),
            "environment": environment,
        }))
    return expanded


def parser_forest_ablation_configurations(
    base_configuration: Mapping[str, Any],
    *,
    selected_parser_command: str,
    forest_parser_command: str,
    forest_grammar_sha256: str,
    name_prefix: str = "parser-forest",
) -> list[dict[str, Any]]:
    """Expand disabled/selected-CST/complete-SPPF cross-parser cells."""
    prefix = str(name_prefix).strip()
    if not prefix or len(prefix) > 96:
        raise ValueError("forest ablation name_prefix must be non-empty")
    selected = str(selected_parser_command).strip()
    forest = str(forest_parser_command).strip()
    if (
        not selected or not forest or
        len(selected) > 4096 or len(forest) > 4096
    ):
        raise ValueError(
            "forest ablation needs bounded selected and forest commands")
    grammar_digest = str(forest_grammar_sha256)
    if (
        len(grammar_digest) != 64 or
        any(character not in "0123456789abcdef"
            for character in grammar_digest)
    ):
        raise ValueError(
            "forest ablation needs the sealed grammar SHA-256")
    base_environment = base_configuration.get("environment", {})
    if not isinstance(base_environment, Mapping):
        raise ValueError("forest base environment must be an object")
    expanded = []
    for mode, command in (
        ("off", ""),
        ("selected", selected),
        ("complete", forest),
    ):
        environment = dict(base_environment)
        environment.update({
            "SYMCC_PROPOSAL_PARSER": command,
            "SYMCC_PROPOSAL_PARSER_CACHE": "0",
            "SYMCC_PARSER_FOREST_MODE": mode,
            "SYMCC_PARSER_FOREST_GRAMMAR_SHA256": (
                grammar_digest if mode == "complete" else ""),
            "SYMCC_PARSER_REQUIRE_ARTIFACT": "1",
        })
        expanded.append(_normalize_configuration({
            **base_configuration,
            "name": f"{prefix}-{mode}",
            "command": list(base_configuration.get("command", ())),
            "environment": environment,
        }))
    return expanded


def parser_cross_calibration_configurations(
    base_configuration: Mapping[str, Any],
    *,
    selected_parser_command: str,
    forest_parser_command: str,
    paired_parser_command: str,
    forest_grammar_sha256: str,
    name_prefix: str = "parser-cross",
) -> list[dict[str, Any]]:
    """Expand selected/complete/paired candidate-identical calibration cells."""
    prefix = str(name_prefix).strip()
    if not prefix or len(prefix) > 96:
        raise ValueError("cross calibration name_prefix must be non-empty")
    selected = str(selected_parser_command).strip()
    forest = str(forest_parser_command).strip()
    paired = str(paired_parser_command).strip()
    if any(
        not command or len(command) > 4096
        for command in (selected, forest, paired)
    ):
        raise ValueError(
            "cross calibration needs bounded parser commands")
    grammar_digest = str(forest_grammar_sha256)
    if (
        len(grammar_digest) != 64 or
        any(character not in "0123456789abcdef"
            for character in grammar_digest)
    ):
        raise ValueError(
            "cross calibration needs the sealed grammar SHA-256")
    primary_digest = parser_command_digest(forest)
    secondary_digest = parser_command_digest(selected)
    if primary_digest == secondary_digest:
        raise ValueError(
            "cross calibration needs independent parser commands")
    base_environment = base_configuration.get("environment", {})
    if not isinstance(base_environment, Mapping):
        raise ValueError("cross calibration environment must be an object")
    expanded = []
    for mode, command, forest_mode in (
        ("selected", selected, "selected"),
        ("complete", forest, "complete"),
        ("paired", paired, "complete"),
    ):
        environment = dict(base_environment)
        environment.update({
            "SYMCC_PROPOSAL_PARSER": command,
            "SYMCC_PROPOSAL_PARSER_CACHE": "0",
            "SYMCC_PARSER_FOREST_MODE": forest_mode,
            "SYMCC_PARSER_FOREST_GRAMMAR_SHA256": (
                grammar_digest if forest_mode == "complete" else ""),
            "SYMCC_PARSER_CROSS_MODE": (
                "paired" if mode == "paired" else "off"),
            "SYMCC_PARSER_CROSS_PRIMARY_SHA256": (
                primary_digest if mode == "paired" else ""),
            "SYMCC_PARSER_CROSS_SECONDARY_SHA256": (
                secondary_digest if mode == "paired" else ""),
            "SYMCC_PARSER_REQUIRE_ARTIFACT": "1",
        })
        expanded.append(_normalize_configuration({
            **base_configuration,
            "name": f"{prefix}-{mode}",
            "command": list(base_configuration.get("command", ())),
            "environment": environment,
        }))
    return expanded


def _valid_parser_label(value: Any, *, allow_empty: bool = False) -> bool:
    minimum = 0 if allow_empty else 1
    return (
        isinstance(value, str) and
        minimum <= len(value) <= 128 and
        value.isascii() and
        all(32 <= ord(character) < 127 for character in value)
    )


def _verify_parser_correspondence(
    symbols: Any,
    productions: Any,
    *,
    symbol_observations: int,
    production_observations: int,
    symbol_mappings: int,
    production_mappings: int,
) -> bool:
    if not isinstance(symbols, list) or not isinstance(productions, list):
        return False
    symbol_keys: list[tuple[str, str, str, str]] = []
    symbol_total = 0
    for item in symbols:
        if (
            not isinstance(item, dict) or
            set(item) != {
                "primary_parser",
                "secondary_parser",
                "primary_symbol",
                "secondary_symbol",
                "observations",
            } or
            any(
                not _valid_parser_label(item.get(key))
                for key in (
                    "primary_parser",
                    "secondary_parser",
                    "primary_symbol",
                    "secondary_symbol",
                )
            ) or
            isinstance(item.get("observations"), bool) or
            not isinstance(item.get("observations"), int) or
            int(item["observations"]) <= 0
        ):
            return False
        symbol_keys.append(tuple(str(item[key]) for key in (
            "primary_parser",
            "secondary_parser",
            "primary_symbol",
            "secondary_symbol",
        )))
        symbol_total += int(item["observations"])
    production_keys: list[
        tuple[str, str, str, str, str, str, str]] = []
    production_total = 0
    for item in productions:
        if (
            not isinstance(item, dict) or
            set(item) != {
                "primary_parser",
                "secondary_parser",
                "primary_symbol",
                "primary_state",
                "secondary_symbol",
                "secondary_state",
                "shape_sha256",
                "observations",
            } or
            any(
                not _valid_parser_label(
                    item.get(key),
                    allow_empty=key.endswith("_state"),
                )
                for key in (
                    "primary_parser",
                    "secondary_parser",
                    "primary_symbol",
                    "primary_state",
                    "secondary_symbol",
                    "secondary_state",
                )
            ) or
            not isinstance(item.get("shape_sha256"), str) or
            len(item["shape_sha256"]) != 64 or
            any(character not in "0123456789abcdef"
                for character in item["shape_sha256"]) or
            isinstance(item.get("observations"), bool) or
            not isinstance(item.get("observations"), int) or
            int(item["observations"]) <= 0
        ):
            return False
        production_keys.append(tuple(str(item[key]) for key in (
            "primary_parser",
            "secondary_parser",
            "primary_symbol",
            "primary_state",
            "secondary_symbol",
            "secondary_state",
            "shape_sha256",
        )))
        production_total += int(item["observations"])
    return (
        symbol_keys == sorted(symbol_keys) and
        len(set(symbol_keys)) == len(symbol_keys) and
        production_keys == sorted(production_keys) and
        len(set(production_keys)) == len(production_keys) and
        symbol_total == symbol_observations and
        production_total == production_observations and
        len(symbols) == symbol_mappings and
        len(productions) == production_mappings
    )


def verify_parser_research_artifact(
    artifact: Mapping[str, Any],
    *,
    schedule_row: Mapping[str, Any] | None = None,
    configuration: Mapping[str, Any] | None = None,
    protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify parser cost/reuse telemetry and its experiment binding."""
    if (
        artifact.get("schema") != PARSER_RESEARCH_ARTIFACT_SCHEMA or
        artifact.get("proposal_state_schema") != 15 or
        not isinstance(artifact.get("parser_cache_enabled"), bool) or
        not isinstance(artifact.get("parser_command_sha256"), str) or
        len(artifact.get("parser_command_sha256", "")) != 64 or
        any(character not in "0123456789abcdef"
            for character in artifact.get("parser_command_sha256", "")) or
        not isinstance(
            artifact.get("parser_forest_grammar_sha256s"), list) or
        not isinstance(
            artifact.get("parser_cross_command_pairs"), list) or
        not isinstance(
            artifact.get("parser_cross_symbol_correspondence"), list) or
        not isinstance(
            artifact.get("parser_cross_production_correspondence"), list)
    ):
        raise ValueError("unsupported parser research artifact schema")
    supplied = str(artifact.get("artifact_sha256", ""))
    core = dict(artifact)
    core.pop("artifact_sha256", None)
    if supplied != content_digest(core):
        raise ValueError("parser research artifact digest mismatch")
    metrics = artifact.get("metrics")
    metadata = artifact.get("metadata")
    grammar_digests = artifact["parser_forest_grammar_sha256s"]
    command_pairs = artifact["parser_cross_command_pairs"]
    symbol_correspondence = artifact[
        "parser_cross_symbol_correspondence"]
    production_correspondence = artifact[
        "parser_cross_production_correspondence"]
    if (
        not isinstance(metrics, dict) or
        not isinstance(metadata, dict) or
        any(
            not isinstance(key, str) or
            not key.startswith("proposal_") or
            isinstance(value, bool) or
            not isinstance(value, (int, float)) or
            isinstance(value, float) and not math.isfinite(value) or
            value < 0
            for key, value in metrics.items()
        ) or
        grammar_digests != sorted(set(grammar_digests)) or
        any(
            not isinstance(value, str) or
            len(value) != 64 or
            any(character not in "0123456789abcdef"
                for character in value)
            for value in grammar_digests
        ) or command_pairs != sorted(
            command_pairs,
            key=lambda item: (
                str(item.get("primary_sha256", ""))
                if isinstance(item, dict) else "",
                str(item.get("secondary_sha256", ""))
                if isinstance(item, dict) else "",
            ),
        ) or len(command_pairs) != len({
            (
                str(item.get("primary_sha256", "")),
                str(item.get("secondary_sha256", "")),
            )
            for item in command_pairs if isinstance(item, dict)
        }) or any(
            not isinstance(item, dict) or
            set(item) != {"primary_sha256", "secondary_sha256"} or
            not isinstance(item["primary_sha256"], str) or
            not isinstance(item["secondary_sha256"], str) or
            len(item["primary_sha256"]) != 64 or
            len(item["secondary_sha256"]) != 64 or
            any(character not in "0123456789abcdef"
                for character in item["primary_sha256"]) or
            any(character not in "0123456789abcdef"
                for character in item["secondary_sha256"]) or
            item["primary_sha256"] == item["secondary_sha256"]
            for item in command_pairs
        )
    ):
        raise ValueError("inconsistent parser research artifact payload")
    try:
        validations = int(metrics["proposal_parser_validations"])
        requests = int(metrics["proposal_parser_cache_requests"])
        offers = int(metrics["proposal_parser_cache_incremental_offers"])
        receipts = int(metrics["proposal_parser_incremental_receipts"])
        zero_reuse = int(
            metrics["proposal_parser_incremental_zero_reuse"])
        proofs = int(metrics["proposal_parser_node_id_proofs"])
        forest_traces = int(metrics["proposal_parser_forest_traces"])
        forest_complete = int(
            metrics["proposal_parser_forest_complete_traces"])
        forest_proofs = int(metrics["proposal_parser_forest_proofs"])
        forest_time = int(
            metrics["proposal_parser_forest_parse_time_us"])
        reported_time = int(
            metrics["proposal_parser_reported_parse_time_us"])
        metric_enabled = int(metrics["proposal_parser_cache_enabled"])
        cross_traces = int(metrics["proposal_parser_cross_traces"])
        cross_agreements = int(
            metrics["proposal_parser_cross_agreements"])
        cross_both_accept = int(
            metrics["proposal_parser_cross_both_accept"])
        cross_primary_only = int(
            metrics["proposal_parser_cross_primary_only"])
        cross_secondary_only = int(
            metrics["proposal_parser_cross_secondary_only"])
        cross_both_reject = int(
            metrics["proposal_parser_cross_both_reject"])
        cross_pairs = int(
            metrics["proposal_parser_cross_command_pairs"])
        cross_structural_pairs = int(
            metrics["proposal_parser_cross_structural_pairs"])
        primary_selected_spans = int(
            metrics["proposal_parser_cross_primary_selected_spans"])
        primary_forest_spans = int(
            metrics["proposal_parser_cross_primary_forest_spans"])
        secondary_spans = int(
            metrics["proposal_parser_cross_secondary_spans"])
        selected_shared_spans = int(
            metrics["proposal_parser_cross_selected_shared_spans"])
        forest_shared_spans = int(
            metrics["proposal_parser_cross_forest_shared_spans"])
        selected_union_spans = int(
            metrics["proposal_parser_cross_selected_union_spans"])
        forest_union_spans = int(
            metrics["proposal_parser_cross_forest_union_spans"])
        primary_boundaries = int(
            metrics["proposal_parser_cross_primary_boundaries"])
        secondary_boundaries = int(
            metrics["proposal_parser_cross_secondary_boundaries"])
        shared_boundaries = int(
            metrics["proposal_parser_cross_shared_boundaries"])
        union_boundaries = int(
            metrics["proposal_parser_cross_union_boundaries"])
        primary_symbols = int(
            metrics["proposal_parser_cross_primary_symbols"])
        secondary_symbols = int(
            metrics["proposal_parser_cross_secondary_symbols"])
        symbol_observations = int(
            metrics["proposal_parser_cross_symbol_correspondences"])
        production_observations = int(
            metrics[
                "proposal_parser_cross_production_correspondences"])
        symbol_mappings = int(
            metrics["proposal_parser_cross_symbol_mappings"])
        production_mappings = int(
            metrics["proposal_parser_cross_production_mappings"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("parser research metrics are incomplete") from exc
    enabled = bool(artifact["parser_cache_enabled"])
    if not (
        requests <= validations and
        offers <= requests and
        receipts <= offers and
        zero_reuse <= receipts and
        proofs <= receipts and
        forest_complete <= forest_proofs <= forest_traces <= validations and
        bool(grammar_digests) == bool(forest_traces) and
        forest_time <= reported_time and
        cross_agreements == cross_both_accept + cross_both_reject and
        cross_traces == (
            cross_both_accept + cross_primary_only +
            cross_secondary_only + cross_both_reject
        ) and
        cross_traces <= validations and
        bool(command_pairs) == bool(cross_traces) and
        cross_pairs == len(command_pairs) and
        cross_structural_pairs == cross_both_accept and
        primary_selected_spans <= primary_forest_spans and
        selected_shared_spans <= primary_selected_spans and
        selected_shared_spans <= secondary_spans and
        forest_shared_spans <= primary_forest_spans and
        forest_shared_spans <= secondary_spans and
        selected_shared_spans <= forest_shared_spans and
        selected_union_spans == (
            primary_selected_spans + secondary_spans -
            selected_shared_spans
        ) and
        forest_union_spans == (
            primary_forest_spans + secondary_spans -
            forest_shared_spans
        ) and
        shared_boundaries <= primary_boundaries and
        shared_boundaries <= secondary_boundaries and
        union_boundaries == (
            primary_boundaries + secondary_boundaries -
            shared_boundaries
        ) and
        symbol_observations <= primary_symbols and
        symbol_observations <= secondary_symbols and
        production_observations <= symbol_observations and
        _verify_parser_correspondence(
            symbol_correspondence,
            production_correspondence,
            symbol_observations=symbol_observations,
            production_observations=production_observations,
            symbol_mappings=symbol_mappings,
            production_mappings=production_mappings,
        ) and
        metric_enabled == int(enabled)
    ):
        raise ValueError("parser research counters are inconsistent")
    if schedule_row is not None:
        for key, expected in {
            "run_id": schedule_row.get("run_id", ""),
            "pair_id": schedule_row.get("pair_id", ""),
            "configuration": schedule_row.get("configuration", ""),
            "random_seed": schedule_row.get("random_seed", ""),
            "cpu_budget_seconds":
                schedule_row.get("cpu_budget_seconds", ""),
            "cpu_cores": schedule_row.get("cpu_cores", ""),
        }.items():
            if str(metadata.get(key, "")) != str(expected):
                raise ValueError(
                    f"parser research artifact changed {key}")
    if protocol is not None:
        for key, expected in {
            "experiment_id": protocol.get("experiment_id", ""),
            "phase": protocol.get("phase", ""),
        }.items():
            if str(metadata.get(key, "")) != str(expected):
                raise ValueError(
                    f"parser research artifact changed {key}")
    if configuration is not None:
        environment = configuration.get("environment", {})
        if not isinstance(environment, Mapping):
            raise ValueError("invalid parser research configuration")
        configured = str(environment.get(
            "SYMCC_PROPOSAL_PARSER_CACHE", "1")).lower()
        expected_enabled = configured not in {"0", "false", "off", "no"}
        if enabled != expected_enabled:
            raise ValueError(
                "parser artifact used another cache mode")
        configured_command = str(environment.get(
            "SYMCC_PROPOSAL_PARSER", ""))
        if str(artifact["parser_command_sha256"]) != (
                parser_command_digest(configured_command)):
            raise ValueError(
                "parser artifact used another parser command")
        forest_mode = str(environment.get(
            "SYMCC_PARSER_FOREST_MODE", "")).strip().lower()
        if forest_mode:
            if forest_mode not in {"off", "selected", "complete"}:
                raise ValueError("invalid configured parser forest mode")
            if str(metadata.get("parser_forest_mode", "")) != forest_mode:
                raise ValueError(
                    "parser artifact used another forest mode")
            if forest_mode != "complete" and forest_traces:
                raise ValueError(
                    "non-forest cell reported complete-forest traces")
            expected_grammar = str(environment.get(
                "SYMCC_PARSER_FOREST_GRAMMAR_SHA256", ""))
            if forest_mode == "complete" and forest_traces and (
                    grammar_digests != [expected_grammar]):
                raise ValueError(
                    "parser artifact used another forest grammar")
            if forest_mode != "complete" and grammar_digests:
                raise ValueError(
                    "non-forest cell reported a forest grammar")
        cross_mode = str(environment.get(
            "SYMCC_PARSER_CROSS_MODE", "")).strip().lower()
        if cross_mode:
            if cross_mode not in {"off", "paired"}:
                raise ValueError("invalid configured parser cross mode")
            if str(metadata.get("parser_cross_mode", "")) != cross_mode:
                raise ValueError(
                    "parser artifact used another cross mode")
            if cross_mode == "off" and (
                    cross_traces or command_pairs):
                raise ValueError(
                    "non-paired cell reported cross-parser traces")
            expected_pair = [{
                "primary_sha256": str(environment.get(
                    "SYMCC_PARSER_CROSS_PRIMARY_SHA256", "")),
                "secondary_sha256": str(environment.get(
                    "SYMCC_PARSER_CROSS_SECONDARY_SHA256", "")),
            }]
            if cross_mode == "paired" and cross_traces and (
                    command_pairs != expected_pair):
                raise ValueError(
                    "parser artifact used another cross-parser pair")
            if cross_mode == "paired" and forest_traces != cross_traces:
                raise ValueError(
                    "paired calibration lacks one forest trace per pair")
    return {
        "verified": True,
        "artifact_sha256": supplied,
        "parser_cache_enabled": enabled,
    }


def verify_pcfg_research_artifact(
    artifact: Mapping[str, Any],
    *,
    schedule_row: Mapping[str, Any] | None = None,
    configuration: Mapping[str, Any] | None = None,
    protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify the content-addressed PCFG telemetry and experiment binding."""
    if (
        artifact.get("schema") != PCFG_RESEARCH_ARTIFACT_SCHEMA or
        artifact.get("semantic_state_schema") != 25
    ):
        raise ValueError("unsupported PCFG research artifact schema")
    supplied = str(artifact.get("artifact_sha256", ""))
    core = dict(artifact)
    core.pop("artifact_sha256", None)
    if supplied != content_digest(core):
        raise ValueError("PCFG research artifact digest mismatch")
    try:
        order = int(artifact.get("pcfg_context_order", -1))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid PCFG research context order") from exc
    if not 0 <= order < len(PCFG_CONTEXT_LEVELS):
        raise ValueError("invalid PCFG research context order")
    level = PCFG_CONTEXT_LEVELS[order]
    metrics = artifact.get("metrics")
    metadata = artifact.get("metadata")
    if (
        str(artifact.get("pcfg_context_level", "")) != level or
        not isinstance(metrics, dict) or
        metrics.get("pcfg_context_order") != order or
        metrics.get("pcfg_context_level") != level or
        not isinstance(metadata, dict)
    ):
        raise ValueError("inconsistent PCFG research artifact payload")
    if schedule_row is not None:
        bindings = {
            "run_id": schedule_row.get("run_id", ""),
            "pair_id": schedule_row.get("pair_id", ""),
            "configuration": schedule_row.get("configuration", ""),
            "random_seed": schedule_row.get("random_seed", ""),
            "cpu_budget_seconds":
                schedule_row.get("cpu_budget_seconds", ""),
            "cpu_cores": schedule_row.get("cpu_cores", ""),
        }
        for key, expected in bindings.items():
            if str(metadata.get(key, "")) != str(expected):
                raise ValueError(
                    f"PCFG research artifact changed {key}")
    if protocol is not None:
        for key, expected in {
            "experiment_id": protocol.get("experiment_id", ""),
            "phase": protocol.get("phase", ""),
        }.items():
            if str(metadata.get(key, "")) != str(expected):
                raise ValueError(
                    f"PCFG research artifact changed {key}")
    if configuration is not None:
        environment = configuration.get("environment", {})
        if not isinstance(environment, Mapping):
            raise ValueError("invalid PCFG research configuration")
        expected_order = environment.get("SYMCC_PCFG_CONTEXT_ORDER")
        if expected_order is not None:
            try:
                normalized_order = _pcfg_context_order(expected_order)
            except ValueError as exc:
                raise ValueError(
                    "invalid configured PCFG context order") from exc
            if order != normalized_order:
                raise ValueError(
                    "PCFG research artifact used another context order")
    return {
        "verified": True,
        "artifact_sha256": supplied,
        "pcfg_context_order": order,
        "pcfg_context_level": level,
    }


def create_protocol(
    *,
    targets: Iterable[str],
    configurations: Iterable[Mapping[str, Any]],
    repeats: int,
    cpu_budget_seconds: float,
    random_seed: int,
    phase: str,
    repo_root: str | os.PathLike[str] = ".",
    inputs: Iterable[str | os.PathLike[str]] = (),
    experiment_id: str = "",
    created_at: str = "",
) -> dict[str, Any]:
    """Create a paired randomized-block schedule and sealed manifest."""
    target_list = [str(target).strip() for target in targets]
    target_list = list(dict.fromkeys(target for target in target_list if target))
    configs = [_normalize_configuration(raw) for raw in configurations]
    names = [config["name"] for config in configs]
    if not target_list:
        raise ValueError("protocol needs at least one target")
    if not configs or len(names) != len(set(names)):
        raise ValueError("configuration names must be non-empty and unique")
    if len({config["wall_grace_seconds"] for config in configs}) != 1:
        raise ValueError(
            "compared configurations must use identical wall_grace_seconds")
    repeats = int(repeats)
    if repeats < 1:
        raise ValueError("repeats must be positive")
    phase = str(phase).lower()
    if phase not in {"tuning", "confirmatory"}:
        raise ValueError("phase must be tuning or confirmatory")
    if phase == "confirmatory" and repeats < MIN_CONFIRMATORY_REPEATS:
        raise ValueError(
            f"confirmatory protocols require at least "
            f"{MIN_CONFIRMATORY_REPEATS} repeats"
        )
    budget = float(cpu_budget_seconds)
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("cpu_budget_seconds must be finite and positive")

    created = created_at or datetime.now(timezone.utc).isoformat()
    provenance = collect_provenance(repo_root, inputs)
    identity_basis = {
        "targets": target_list,
        "configurations": configs,
        "repeats": repeats,
        "cpu_budget_seconds": budget,
        "random_seed": int(random_seed),
        "phase": phase,
        "source": provenance.get("git_commit", ""),
        "created_at": created,
    }
    experiment = experiment_id.strip() or (
        f"symcc-{content_digest(identity_basis)[:16]}"
    )
    rng = random.Random(int(random_seed))
    blocks: list[tuple[str, int]] = [
        (target, repeat)
        for target in target_list
        for repeat in range(1, repeats + 1)
    ]
    rng.shuffle(blocks)
    schedule: list[dict[str, Any]] = []
    for target, repeat in blocks:
        pair_id = content_digest({
            "experiment": experiment,
            "target": target,
            "repeat": repeat,
        })[:24]
        pair_seed = rng.randrange(0, 2**63)
        block_configs = list(configs)
        rng.shuffle(block_configs)
        for config in block_configs:
            run_id = content_digest({
                "pair_id": pair_id,
                "configuration": config["name"],
            })[:24]
            cores = int(config["cpu_cores"])
            schedule.append({
                "order": len(schedule),
                "run_id": run_id,
                "pair_id": pair_id,
                "target": target,
                "repeat": repeat,
                "configuration": config["name"],
                "random_seed": pair_seed,
                "cpu_cores": cores,
                "cpu_budget_seconds": budget,
                "wall_budget_seconds": budget / cores,
                "wall_grace_seconds": float(
                    config["wall_grace_seconds"]),
                "wall_timeout_seconds": (
                    budget / cores
                    + float(config["wall_grace_seconds"])
                ),
            })

    manifest = {
        "schema": PROTOCOL_SCHEMA,
        "experiment_id": experiment,
        "phase": phase,
        "created_at": created,
        "randomization": {
            "algorithm": "paired-randomized-block-v1",
            "seed": int(random_seed),
            "blocking_factors": ["target", "repeat"],
            "common_random_numbers": True,
        },
        "repeats": repeats,
        "cpu_budget_seconds": budget,
        "targets": target_list,
        "configurations": configs,
        "schedule": schedule,
        "provenance": provenance,
    }
    manifest["digest"] = content_digest(manifest)
    verify_protocol(manifest)
    return manifest


def verify_protocol(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Verify schedule completeness, pairing, budgets, provenance and digest."""
    if manifest.get("schema") != PROTOCOL_SCHEMA:
        raise ValueError("unsupported research protocol schema")
    supplied_digest = str(manifest.get("digest", ""))
    unsigned = dict(manifest)
    unsigned.pop("digest", None)
    if supplied_digest != content_digest(unsigned):
        raise ValueError("research protocol digest mismatch")
    phase = str(manifest.get("phase", ""))
    repeats = int(manifest.get("repeats", 0) or 0)
    if phase not in {"tuning", "confirmatory"}:
        raise ValueError("invalid research phase")
    if phase == "confirmatory" and repeats < MIN_CONFIRMATORY_REPEATS:
        raise ValueError("underpowered confirmatory protocol")
    targets = manifest.get("targets")
    configurations = manifest.get("configurations")
    schedule = manifest.get("schedule")
    if not isinstance(targets, list) or not targets:
        raise ValueError("protocol has no targets")
    if not isinstance(configurations, list) or not configurations:
        raise ValueError("protocol has no configurations")
    if not isinstance(schedule, list):
        raise ValueError("protocol has no schedule")
    names = {
        str(config.get("name", ""))
        for config in configurations if isinstance(config, dict)
    }
    expected = {
        (str(target), repeat, name)
        for target in targets
        for repeat in range(1, repeats + 1)
        for name in names
    }
    observed: set[tuple[str, int, str]] = set()
    run_ids: set[str] = set()
    pairs: dict[tuple[str, int], tuple[str, int]] = {}
    protocol_budget = float(manifest.get("cpu_budget_seconds", 0) or 0)
    grace_values: set[float] = set()
    for index, row in enumerate(schedule):
        if not isinstance(row, dict) or int(row.get("order", -1)) != index:
            raise ValueError("schedule order is not canonical")
        key = (
            str(row.get("target", "")),
            int(row.get("repeat", 0) or 0),
            str(row.get("configuration", "")),
        )
        observed.add(key)
        run_id = str(row.get("run_id", ""))
        if not run_id or run_id in run_ids:
            raise ValueError("schedule run ids are missing or duplicated")
        run_ids.add(run_id)
        if not math.isclose(
            float(row.get("cpu_budget_seconds", 0) or 0),
            protocol_budget,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("schedule does not preserve equal CPU budgets")
        grace_values.add(float(row.get("wall_grace_seconds", 0.0) or 0.0))
        pair_key = (key[0], key[1])
        pair_value = (
            str(row.get("pair_id", "")),
            int(row.get("random_seed", -1)),
        )
        previous = pairs.setdefault(pair_key, pair_value)
        if pair_value != previous or not pair_value[0]:
            raise ValueError("paired block has inconsistent identity or seed")
    if observed != expected or len(schedule) != len(expected):
        raise ValueError("schedule is incomplete or contains duplicate cells")
    if len(grace_values) != 1:
        raise ValueError("schedule has unequal teardown grace")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict) or "git_commit" not in provenance:
        raise ValueError("protocol lacks source provenance")
    return {
        "verified": True,
        "experiment_id": str(manifest.get("experiment_id", "")),
        "runs": len(schedule),
        "pairs": len(pairs),
        "confirmatory": phase == "confirmatory",
        "equal_cpu_budget": True,
        "digest": supplied_digest,
    }


def verify_live_provenance(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Reject execution when the sealed source, environment, or inputs drift."""
    verify_protocol(manifest)
    expected = manifest.get("provenance")
    if not isinstance(expected, dict):
        raise ValueError("protocol lacks source provenance")
    repository = str(expected.get("repository", "")).strip()
    input_rows = expected.get("inputs")
    if not repository or not isinstance(input_rows, list):
        raise ValueError("protocol provenance is incomplete")
    input_paths = [
        str(row.get("path", ""))
        for row in input_rows
        if isinstance(row, dict) and str(row.get("path", ""))
    ]
    observed = collect_provenance(repository, input_paths)
    if observed != expected:
        changed = sorted(
            key for key in set(expected) | set(observed)
            if expected.get(key) != observed.get(key)
        )
        detail = ", ".join(changed[:8]) or "unknown"
        raise ValueError(f"research provenance drift: {detail}")
    return {
        "verified": True,
        "working_tree_sha256": str(expected.get(
            "working_tree_sha256", "")),
        "inputs": len(input_paths),
    }


def verify_run_result(
    result: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    artifact_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Verify one result against its scheduled cell and optional raw files."""
    verify_protocol(manifest)
    if result.get("schema") != RESULT_SCHEMA:
        raise ValueError("unsupported research result schema")
    supplied_digest = str(result.get("digest", ""))
    unsigned = dict(result)
    unsigned.pop("digest", None)
    if supplied_digest != content_digest(unsigned):
        raise ValueError("research result digest mismatch")
    if str(result.get("protocol_digest", "")) != str(manifest["digest"]):
        raise ValueError("research result belongs to another protocol")
    run_id = str(result.get("run_id", ""))
    scheduled = next(
        (
            row for row in manifest["schedule"]
            if str(row.get("run_id", "")) == run_id
        ),
        None,
    )
    if scheduled is None:
        raise ValueError("research result run id is not scheduled")
    bound_fields = (
        "order", "run_id", "pair_id", "target", "repeat", "configuration",
        "random_seed", "cpu_cores", "cpu_budget_seconds",
        "wall_budget_seconds", "wall_grace_seconds",
        "wall_timeout_seconds",
    )
    for field in bound_fields:
        expected = scheduled.get(field)
        observed = result.get(field)
        if isinstance(expected, float):
            try:
                matches = math.isclose(
                    float(observed), expected, rel_tol=0.0, abs_tol=1e-9)
            except (TypeError, ValueError, OverflowError):
                matches = False
        else:
            matches = observed == expected
        if not matches:
            raise ValueError(f"research result changed scheduled field {field}")
    if result.get("status") not in {"success", "failed", "timeout"}:
        raise ValueError("research result has invalid completion status")
    if artifact_root is not None:
        root = Path(artifact_root).resolve()
        for field in ("stdout", "stderr"):
            relative = Path(str(result.get(field, "")))
            path = (root / relative).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError("research result artifact escapes root") from exc
            if not path.is_file():
                raise ValueError(f"research result is missing {field}")
            if file_digest(path) != str(result.get(f"{field}_sha256", "")):
                raise ValueError(f"research result {field} digest mismatch")
        run_dir = root / "runs" / run_id
        config = next(
            (
                item for item in manifest["configurations"]
                if str(item.get("name", "")) ==
                str(scheduled["configuration"])
            ),
            None,
        )
        artifact_path = run_dir / "pcfg_research_artifact.json"
        require_pcfg = (
            isinstance(config, dict) and
            str(config.get("environment", {}).get(
                "SYMCC_PCFG_REQUIRE_ARTIFACT", "")).lower()
            in {"1", "true", "yes", "on"}
        )
        if (
            artifact_path.is_file() and
            result.get("status") == "success"
        ):
            try:
                artifact = json.loads(
                    artifact_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError(
                    "invalid PCFG research artifact JSON") from exc
            verify_pcfg_research_artifact(
                artifact,
                schedule_row=scheduled,
                configuration=config,
                protocol=manifest,
            )
        elif require_pcfg and result.get("status") == "success":
            raise ValueError("successful PCFG ablation lacks its artifact")
        parser_artifact_path = (
            run_dir / "parser_research_artifact.json")
        require_parser = (
            isinstance(config, dict) and
            str(config.get("environment", {}).get(
                "SYMCC_PARSER_REQUIRE_ARTIFACT", "")).lower()
            in {"1", "true", "yes", "on"}
        )
        if (
            parser_artifact_path.is_file() and
            result.get("status") == "success"
        ):
            try:
                parser_artifact = json.loads(
                    parser_artifact_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError(
                    "invalid parser research artifact JSON") from exc
            verify_parser_research_artifact(
                parser_artifact,
                schedule_row=scheduled,
                configuration=config,
                protocol=manifest,
            )
        elif require_parser and result.get("status") == "success":
            raise ValueError(
                "successful parser ablation lacks its artifact")
        artifact_identity = run_artifact_digest(run_dir)
        if (
            artifact_identity["sha256"]
            != str(result.get("artifact_tree_sha256", ""))
            or artifact_identity["files"]
            != int(result.get("artifact_tree_files", -1))
            or artifact_identity["bytes"]
            != int(result.get("artifact_tree_bytes", -1))
        ):
            raise ValueError("research result artifact tree digest mismatch")
    return {
        "verified": True,
        "run_id": run_id,
        "status": str(result["status"]),
        "digest": supplied_digest,
    }


def coverage_auc(
    points: Iterable[Mapping[str, Any]],
    budget_seconds: float,
    *,
    value_key: str = "edges_found",
    normalize_time: bool = True,
) -> float:
    """Trapezoidal coverage AUC with endpoint carry-forward."""
    budget = float(budget_seconds)
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("coverage AUC needs a positive finite budget")
    samples: dict[float, float] = {}
    for point in points:
        try:
            timestamp = min(budget, max(0.0, float(
                point.get("timestamp_sec", point.get("time_sec", 0.0)))))
            value = float(point.get(value_key, 0.0))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(timestamp) and math.isfinite(value):
            samples[timestamp] = value
    if not samples:
        return 0.0
    ordered = sorted(samples.items())
    if ordered[0][0] > 0:
        ordered.insert(0, (0.0, ordered[0][1]))
    if ordered[-1][0] < budget:
        ordered.append((budget, ordered[-1][1]))
    area = sum(
        (right_t - left_t) * (left_v + right_v) / 2.0
        for (left_t, left_v), (right_t, right_v)
        in zip(ordered, ordered[1:])
    )
    return area / budget if normalize_time else area


def time_to_target(
    points: Iterable[Mapping[str, Any]],
    threshold: float,
    budget_seconds: float,
    *,
    value_key: str = "edges_found",
) -> tuple[float, bool]:
    """Return first observed threshold time and right-censoring status."""
    budget = float(budget_seconds)
    for point in sorted(
        points,
        key=lambda row: float(
            row.get("timestamp_sec", row.get("time_sec", 0.0)) or 0.0
        ),
    ):
        try:
            timestamp = float(
                point.get("timestamp_sec", point.get("time_sec", 0.0)))
            value = float(point.get(value_key, 0.0))
        except (TypeError, ValueError, OverflowError):
            continue
        if value >= float(threshold):
            return (min(max(0.0, timestamp), budget), False)
    return (budget, True)


def _format_command(
    command: Iterable[str],
    context: Mapping[str, Any],
) -> list[str]:
    return [str(argument).format_map(context) for argument in command]


def execute_protocol(
    manifest: Mapping[str, Any],
    output_dir: str | os.PathLike[str],
    *,
    resume: bool = False,
) -> list[dict[str, Any]]:
    """Execute a sealed protocol sequentially and retain every outcome."""
    verify_protocol(manifest)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    _atomic_json(output / "protocol_manifest.json", dict(manifest))
    configs = {
        str(config["name"]): config for config in manifest["configurations"]
    }
    results: list[dict[str, Any]] = []
    for schedule_row in manifest["schedule"]:
        run_id = str(schedule_row["run_id"])
        run_dir = output / "runs" / run_id
        result_path = run_dir / "result.json"
        if resume and result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                verify_run_result(result, manifest, artifact_root=output)
                results.append(result)
                continue
            except (OSError, ValueError, TypeError):
                pass
        # Recheck before every new cell, not only once at process startup.  A
        # resumed campaign must never combine rows produced by two worktrees,
        # tool environments, submodule states, or input corpora.
        verify_live_provenance(manifest)
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        config = configs[str(schedule_row["configuration"])]
        context = {
            **schedule_row,
            "experiment_id": manifest["experiment_id"],
            "output_dir": str(output),
            "run_dir": str(run_dir),
        }
        command = _format_command(config["command"], context)
        environment = os.environ.copy()
        environment.update(config.get("environment", {}))
        environment.update({
            "SYMCC_EXPERIMENT_ID": str(manifest["experiment_id"]),
            "SYMCC_RUN_ID": run_id,
            "SYMCC_PAIR_ID": str(schedule_row["pair_id"]),
            "SYMCC_RESEARCH_PHASE": str(manifest["phase"]),
            "SYMCC_RESEARCH_CONFIGURATION": str(
                schedule_row["configuration"]),
            "SYMCC_RANDOM_SEED": str(schedule_row["random_seed"]),
            "SYMCC_CPU_BUDGET_SECONDS": str(
                schedule_row["cpu_budget_seconds"]),
            "SYMCC_CPU_CORES": str(schedule_row["cpu_cores"]),
            "SYMCC_RESEARCH_RUN_DIR": str(run_dir),
            "SYMCC_RESEARCH_TARGET": str(schedule_row["target"]),
        })
        started_wall = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        status = "failed"
        failure_reason = ""
        returncode: int | None = None
        stdout = b""
        stderr = b""
        process: subprocess.Popen[bytes] | None = None
        assigned_cpus: tuple[int, ...] = ()
        try:
            requested_cores = int(schedule_row["cpu_cores"])
            available_cpus = (
                sorted(os.sched_getaffinity(0))
                if hasattr(os, "sched_getaffinity")
                else list(range(os.cpu_count() or 1))
            )
            if requested_cores > len(available_cpus):
                raise OSError(
                    f"requested {requested_cores} cores but only "
                    f"{len(available_cpus)} are available"
                )
            assigned_cpus = tuple(available_cpus[:requested_cores])

            def configure_child() -> None:
                os.setsid()
                if hasattr(os, "sched_setaffinity"):
                    os.sched_setaffinity(0, assigned_cpus)

            process = subprocess.Popen(
                command,
                cwd=str(Path(manifest["provenance"]["repository"])),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=configure_child,
            )
            stdout, stderr = process.communicate(
                timeout=float(schedule_row["wall_timeout_seconds"]))
            returncode = process.returncode
            status = "success" if returncode == 0 else "failed"
            if returncode:
                failure_reason = f"exit-{returncode}"
        except subprocess.TimeoutExpired:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    stdout, stderr = process.communicate()
                returncode = process.returncode
            status = "timeout"
            failure_reason = "wall-budget-exhausted"
        except OSError as exc:
            status = "failed"
            failure_reason = f"exec-error:{exc.__class__.__name__}"
            stderr = str(exc).encode("utf-8", errors="replace")
        usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        wall_time = time.monotonic() - started_wall
        cpu_user = usage_after.ru_utime - usage_before.ru_utime
        cpu_system = usage_after.ru_stime - usage_before.ru_stime
        stdout_path = run_dir / "stdout.bin"
        stderr_path = run_dir / "stderr.bin"
        stdout_path.write_bytes(stdout)
        stderr_path.write_bytes(stderr)
        require_pcfg = (
            str(config.get("environment", {}).get(
                "SYMCC_PCFG_REQUIRE_ARTIFACT", "")).lower()
            in {"1", "true", "yes", "on"}
        )
        pcfg_artifact_path = (
            run_dir / "pcfg_research_artifact.json")
        if status == "success" and (
            require_pcfg or pcfg_artifact_path.is_file()
        ):
            try:
                artifact = json.loads(
                    pcfg_artifact_path.read_text(encoding="utf-8"))
                verify_pcfg_research_artifact(
                    artifact,
                    schedule_row=schedule_row,
                    configuration=config,
                    protocol=manifest,
                )
            except (OSError, ValueError, TypeError) as exc:
                status = "failed"
                failure_reason = (
                    "invalid-pcfg-research-artifact:"
                    f"{exc.__class__.__name__}"
                )
        require_parser = (
            str(config.get("environment", {}).get(
                "SYMCC_PARSER_REQUIRE_ARTIFACT", "")).lower()
            in {"1", "true", "yes", "on"}
        )
        parser_artifact_path = (
            run_dir / "parser_research_artifact.json")
        if status == "success" and (
            require_parser or parser_artifact_path.is_file()
        ):
            try:
                parser_artifact = json.loads(
                    parser_artifact_path.read_text(encoding="utf-8"))
                verify_parser_research_artifact(
                    parser_artifact,
                    schedule_row=schedule_row,
                    configuration=config,
                    protocol=manifest,
                )
            except (OSError, ValueError, TypeError) as exc:
                status = "failed"
                failure_reason = (
                    "invalid-parser-research-artifact:"
                    f"{exc.__class__.__name__}"
                )
        artifact_identity = run_artifact_digest(run_dir)
        result = {
            "schema": RESULT_SCHEMA,
            **schedule_row,
            "experiment_id": manifest["experiment_id"],
            "phase": manifest["phase"],
            "started_at": started_at,
            "status": status,
            "failure_reason": failure_reason,
            "returncode": returncode,
            "wall_time_sec": wall_time,
            "cpu_user_sec": cpu_user,
            "cpu_system_sec": cpu_system,
            "cpu_measured_sec": cpu_user + cpu_system,
            "assigned_cpus": list(assigned_cpus),
            "command": command,
            "stdout": str(stdout_path.relative_to(output)),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr": str(stderr_path.relative_to(output)),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "artifact_tree_sha256": artifact_identity["sha256"],
            "artifact_tree_files": artifact_identity["files"],
            "artifact_tree_bytes": artifact_identity["bytes"],
            "protocol_digest": manifest["digest"],
        }
        result["digest"] = content_digest(result)
        verify_run_result(result, manifest)
        _atomic_json(result_path, result)
        results.append(result)

    ordered = sorted(results, key=lambda row: int(row["order"]))
    jsonl_path = output / "research_results.jsonl"
    temporary = jsonl_path.with_name(f".{jsonl_path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for result in ordered:
            stream.write(json.dumps(
                result, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, jsonl_path)
    return ordered


def _load_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create, verify, and execute SymCC research protocols")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("spec", help="JSON experiment specification")
    plan.add_argument("--output", required=True)
    plan.add_argument("--repo-root", default=str(Path(__file__).parents[1]))

    verify = subparsers.add_parser("verify")
    verify.add_argument("manifest")
    verify.add_argument("--results-dir", default="")

    run = subparsers.add_parser("run")
    run.add_argument("manifest")
    run.add_argument("--output-dir", required=True)
    run.add_argument("--resume", action="store_true")

    args = parser.parse_args()
    if args.operation == "plan":
        spec = _load_json(args.spec)
        configurations = spec.get("configurations", ())
        pcfg_ablation = spec.get("pcfg_context_ablation")
        parser_ablation = spec.get("parser_incremental_ablation")
        forest_ablation = spec.get("parser_forest_ablation")
        cross_calibration = spec.get("parser_cross_calibration")
        if sum(
            item is not None
            for item in (
                pcfg_ablation, parser_ablation, forest_ablation,
                cross_calibration)
        ) > 1:
            raise ValueError(
                "only one generated ablation may be selected")
        if pcfg_ablation is not None:
            if configurations:
                raise ValueError(
                    "use configurations or pcfg_context_ablation, not both")
            if not isinstance(pcfg_ablation, dict):
                raise ValueError(
                    "pcfg_context_ablation must be an object")
            base = pcfg_ablation.get("base_configuration")
            if not isinstance(base, dict):
                raise ValueError(
                    "pcfg_context_ablation needs base_configuration")
            configurations = pcfg_context_ablation_configurations(
                base,
                levels=pcfg_ablation.get(
                    "levels", PCFG_CONTEXT_LEVELS),
                name_prefix=str(
                    pcfg_ablation.get("name_prefix", "pcfg")),
            )
        if parser_ablation is not None:
            if configurations:
                raise ValueError(
                    "use configurations or parser_incremental_ablation, "
                    "not both")
            if not isinstance(parser_ablation, dict):
                raise ValueError(
                    "parser_incremental_ablation must be an object")
            base = parser_ablation.get("base_configuration")
            if not isinstance(base, dict):
                raise ValueError(
                    "parser_incremental_ablation needs base_configuration")
            configurations = parser_incremental_ablation_configurations(
                base,
                name_prefix=str(
                    parser_ablation.get("name_prefix", "parser")),
            )
        if forest_ablation is not None:
            if configurations:
                raise ValueError(
                    "use configurations or parser_forest_ablation, "
                    "not both")
            if not isinstance(forest_ablation, dict):
                raise ValueError(
                    "parser_forest_ablation must be an object")
            base = forest_ablation.get("base_configuration")
            if not isinstance(base, dict):
                raise ValueError(
                    "parser_forest_ablation needs base_configuration")
            configurations = parser_forest_ablation_configurations(
                base,
                selected_parser_command=str(forest_ablation.get(
                    "selected_parser_command", "")),
                forest_parser_command=str(forest_ablation.get(
                    "forest_parser_command", "")),
                forest_grammar_sha256=str(forest_ablation.get(
                    "forest_grammar_sha256", "")),
                name_prefix=str(forest_ablation.get(
                    "name_prefix", "parser-forest")),
            )
        if cross_calibration is not None:
            if configurations:
                raise ValueError(
                    "use configurations or parser_cross_calibration, "
                    "not both")
            if not isinstance(cross_calibration, dict):
                raise ValueError(
                    "parser_cross_calibration must be an object")
            base = cross_calibration.get("base_configuration")
            if not isinstance(base, dict):
                raise ValueError(
                    "parser_cross_calibration needs base_configuration")
            configurations = parser_cross_calibration_configurations(
                base,
                selected_parser_command=str(cross_calibration.get(
                    "selected_parser_command", "")),
                forest_parser_command=str(cross_calibration.get(
                    "forest_parser_command", "")),
                paired_parser_command=str(cross_calibration.get(
                    "paired_parser_command", "")),
                forest_grammar_sha256=str(cross_calibration.get(
                    "forest_grammar_sha256", "")),
                name_prefix=str(cross_calibration.get(
                    "name_prefix", "parser-cross")),
            )
        manifest = create_protocol(
            targets=spec.get("targets", ()),
            configurations=configurations,
            repeats=int(spec.get("repeats", 0) or 0),
            cpu_budget_seconds=float(
                spec.get("cpu_budget_seconds", 0) or 0),
            random_seed=int(spec.get("random_seed", 0) or 0),
            phase=str(spec.get("phase", "")),
            repo_root=args.repo_root,
            inputs=spec.get("inputs", ()),
            experiment_id=str(spec.get("experiment_id", "")),
        )
        _atomic_json(Path(args.output), manifest)
        print(json.dumps(verify_protocol(manifest), sort_keys=True))
        return 0
    manifest = _load_json(args.manifest)
    if args.operation == "verify":
        report = verify_protocol(manifest)
        if args.results_dir:
            root = Path(args.results_dir).resolve()
            verified_results = []
            for row in manifest["schedule"]:
                result_path = (
                    root / "runs" / str(row["run_id"]) / "result.json")
                if not result_path.is_file():
                    raise ValueError(
                        f"missing scheduled result {row['run_id']}")
                result = json.loads(result_path.read_text(encoding="utf-8"))
                verified_results.append(verify_run_result(
                    result, manifest, artifact_root=root))
            report["verified_results"] = len(verified_results)
        print(json.dumps(report, sort_keys=True))
        return 0
    results = execute_protocol(
        manifest, args.output_dir, resume=bool(args.resume))
    print(json.dumps({
        "runs": len(results),
        "success": sum(row["status"] == "success" for row in results),
        "timeout": sum(row["status"] == "timeout" for row in results),
        "failed": sum(row["status"] == "failed" for row in results),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
