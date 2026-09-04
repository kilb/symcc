#!/usr/bin/env python3
"""Profile, validate, and replay Hydra-transformed SymCC programs.

The compiler pass is deliberately allowed to introduce transformed-only
failures in aggressive memory mode.  This driver makes the original program
the authority: transformed failures are never retained without original-binary
replay, and false-positive sites are emitted as a compiler denylist.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from verify_hydra_transform_manifest import (
    VerificationError as ManifestVerificationError,
)
from verify_hydra_transform_manifest import verify_record as verify_manifest_record


PROFILE_SCHEMA_V1 = "symcc-hydra-profile-v1"
PROFILE_SCHEMA = "symcc-hydra-profile-v2"
MANIFEST_SCHEMA = "symcc-hydra-transform-v1"
CAMPAIGN_SCHEMA_V1 = "symcc-hydra-replay-v1"
CAMPAIGN_SCHEMA = "symcc-hydra-replay-v2"
REPLAY_SCHEMA = "symcc-hydra-replay-check-v1"
MAX_PROFILE_INPUTS = 1 << 16
MAX_BRANCH_TRACE = 1 << 12
MAX_REPLAY_INPUTS = 1 << 14
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 256 * 1024 * 1024


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_digest(path: str | os.PathLike[str]) -> str:
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _artifact_digest(artifact: Mapping[str, Any], field: str) -> str:
    return _digest({
        key: value for key, value in artifact.items() if key != field
    })


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed >= 0 else 0


def _load_documents(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        documents: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("telemetry JSONL rows must be objects")
            documents.append(value)
        return documents
    if isinstance(document, dict):
        return [document]
    if isinstance(document, list) and all(
            isinstance(item, dict) for item in document):
        return document
    raise ValueError("telemetry input must be an object, object list, or JSONL")


def build_profile(
    telemetry_paths: Sequence[str | os.PathLike[str]],
    *,
    denied_sites: Iterable[int] = (),
    profiled_command: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Aggregate telemetry and optionally bind it to its producing binary."""
    if not telemetry_paths or len(telemetry_paths) > MAX_PROFILE_INPUTS:
        raise ValueError("telemetry input count is outside the bounded range")
    denied = {
        _nonnegative_int(site) for site in denied_sites
        if _nonnegative_int(site)
    }
    observations: defaultdict[int, int] = defaultdict(int)
    interesting: defaultdict[int, int] = defaultdict(int)
    solver_time: defaultdict[int, int] = defaultdict(int)
    sources: list[dict[str, Any]] = []
    document_count = 0

    for source_path in telemetry_paths:
        path = str(Path(source_path).expanduser().resolve())
        documents = _load_documents(path)
        sources.append({
            "path": path,
            "sha256": _file_digest(path),
            "documents": len(documents),
        })
        for telemetry in documents:
            document_count += 1
            trace = telemetry.get("branch_trace", ())
            if not isinstance(trace, (list, tuple)):
                continue
            parsed: list[tuple[int, bool, bool]] = []
            for row in trace[:MAX_BRANCH_TRACE]:
                if (
                    not isinstance(row, (list, tuple))
                    or len(row) != 6
                ):
                    continue
                site = _nonnegative_int(row[3])
                if not site:
                    continue
                parsed.append((
                    site,
                    bool(_nonnegative_int(row[5])),
                    site in denied,
                ))
            if not parsed:
                continue
            total_solver_us = _nonnegative_int(
                telemetry.get("solver_time_us", 0))
            base, remainder = divmod(total_solver_us, len(parsed))
            for index, (site, is_interesting, is_denied) in enumerate(parsed):
                if is_denied:
                    continue
                observations[site] += 1
                interesting[site] += int(is_interesting)
                solver_time[site] += base + int(index < remainder)

    entries = []
    for site, count in observations.items():
        cost_us = solver_time[site]
        score = (
            float(count)
            + 4.0 * float(interesting[site])
            + 2.0 * math.log1p(cost_us / 1000.0)
        )
        entries.append({
            "site": site,
            "score": round(score, 9),
            "observations": count,
            "interesting": interesting[site],
            "solver_time_us": cost_us,
        })
    entries.sort(key=lambda row: (-row["score"], row["site"]))
    profile: dict[str, Any] = {
        "schema": (
            PROFILE_SCHEMA
            if profiled_command is not None
            else PROFILE_SCHEMA_V1
        ),
        "scoring": (
            "observations + 4*interesting + "
            "2*log1p(attributed_solver_time_us/1000)"
        ),
        "allocation": "per-execution solver time divided across trace rows",
        "documents": document_count,
        "sources": sorted(sources, key=lambda row: row["path"]),
        "denied_sites": sorted(denied),
        "entries": entries,
    }
    if profiled_command is not None:
        command = _command_record(profiled_command)
        profile["profiled_command"] = command
        profile["profiled_command_sha256"] = _digest(command)
    profile["profile_sha256"] = _artifact_digest(
        profile, "profile_sha256")
    return profile


def verify_profile(profile: Mapping[str, Any]) -> bool:
    try:
        schema = profile.get("schema")
        if (
            schema not in (PROFILE_SCHEMA_V1, PROFILE_SCHEMA)
            or profile.get("profile_sha256")
            != _artifact_digest(profile, "profile_sha256")
            or not _is_sha256(profile.get("profile_sha256"))
            or not isinstance(profile.get("entries"), list)
            or not isinstance(profile.get("sources"), list)
        ):
            return False
        if schema == PROFILE_SCHEMA:
            command = profile.get("profiled_command")
            if (
                not _verify_command_record(command)
                or profile.get("profiled_command_sha256")
                != _digest(command)
                or not _is_sha256(
                    profile.get("profiled_command_sha256"))
            ):
                return False
        elif (
            "profiled_command" in profile
            or "profiled_command_sha256" in profile
        ):
            return False
        denied = profile.get("denied_sites")
        if (
            not isinstance(denied, list)
            or any(
                not isinstance(site, int)
                or isinstance(site, bool)
                or site <= 0
                for site in denied
            )
            or denied != sorted(set(denied))
        ):
            return False
        previous: tuple[float, int] | None = None
        sites: set[int] = set()
        for row in profile["entries"]:
            if not isinstance(row, dict):
                return False
            site_value = row.get("site")
            score_value = row.get("score")
            observations = row.get("observations")
            interesting_count = row.get("interesting")
            solver_time_us = row.get("solver_time_us")
            if (
                not isinstance(site_value, int)
                or isinstance(site_value, bool)
                or not isinstance(score_value, (int, float))
                or isinstance(score_value, bool)
                or not isinstance(observations, int)
                or isinstance(observations, bool)
                or not isinstance(interesting_count, int)
                or isinstance(interesting_count, bool)
                or not isinstance(solver_time_us, int)
                or isinstance(solver_time_us, bool)
            ):
                return False
            site = site_value
            score = float(score_value)
            if (
                not site
                or site in sites
                or site in denied
                or not math.isfinite(score)
                or score < 0.0
                or observations <= 0
                or interesting_count < 0
                or interesting_count > observations
                or solver_time_us < 0
            ):
                return False
            order = (-score, site)
            if previous is not None and order < previous:
                return False
            previous = order
            sites.add(site)
        for source in profile["sources"]:
            if (
                not isinstance(source, dict)
                or not _is_sha256(source.get("sha256"))
                or not isinstance(source.get("documents"), int)
                or isinstance(source.get("documents"), bool)
                or source["documents"] <= 0
            ):
                return False
        return True
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def profile_text(profile: Mapping[str, Any]) -> str:
    if not verify_profile(profile):
        raise ValueError("invalid Hydra profile artifact")
    lines = [f"# {profile['schema']}"]
    if profile["schema"] == PROFILE_SCHEMA:
        command = profile["profiled_command"]
        lines.extend([
            f"# profile_sha256 {profile['profile_sha256']}",
            "# profiled_executable_sha256 "
            f"{command['executable_sha256']}",
            "# profiled_command_sha256 "
            f"{profile['profiled_command_sha256']}",
        ])
    lines.append(
        "# site score observations interesting solver_time_us")
    for row in profile["entries"]:
        lines.append(
            f"{row['site']} {row['score']:.9f} "
            f"{row['observations']} {row['interesting']} "
            f"{row['solver_time_us']}"
        )
    return "\n".join(lines) + "\n"


def _resolve_command(command: Sequence[str]) -> list[str]:
    if (
        not isinstance(command, Sequence)
        or isinstance(command, (str, bytes))
        or not command
        or any(not isinstance(item, str) or not item for item in command)
        or sum(item.count("@@") for item in command) > 1
    ):
        raise ValueError("command must be a nonempty argv with at most one @@")
    executable = shutil.which(command[0])
    if executable is None:
        candidate = Path(command[0]).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            executable = str(candidate)
    if executable is None:
        raise FileNotFoundError(f"executable is unavailable: {command[0]}")
    return [str(Path(executable).resolve()), *command[1:]]


def _command_record(command: Sequence[str]) -> dict[str, Any]:
    resolved = _resolve_command(command)
    return {
        "argv": resolved,
        "executable_sha256": _file_digest(resolved[0]),
        "input_mode": (
            "file-placeholder"
            if sum(item.count("@@") for item in resolved) == 1
            else "stdin"
        ),
    }


def _verify_command_record(command: Any) -> bool:
    if not isinstance(command, Mapping):
        return False
    argv = command.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
        or not Path(argv[0]).is_absolute()
        or sum(item.count("@@") for item in argv) > 1
        or not _is_sha256(command.get("executable_sha256"))
    ):
        return False
    expected_mode = (
        "file-placeholder"
        if sum(item.count("@@") for item in argv) == 1
        else "stdin"
    )
    return command.get("input_mode") == expected_mode


def _load_profile_artifact(
    path: str | os.PathLike[str],
) -> dict[str, Any]:
    artifact_path = Path(path).expanduser().resolve()
    encoded = artifact_path.read_bytes()
    profile = json.loads(encoded.decode("utf-8"))
    if (
        not isinstance(profile, dict)
        or profile.get("schema") != PROFILE_SCHEMA
        or not verify_profile(profile)
    ):
        raise ValueError("profile artifact is not a verified v2 profile")
    return {
        "path": str(artifact_path),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "artifact": profile,
    }


def _profile_binding_matches(
    evidence: Any,
    manifest: Mapping[str, Any],
    original: Mapping[str, Any],
) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    profile = evidence.get("artifact")
    if (
        not _is_sha256(evidence.get("sha256"))
        or not isinstance(profile, Mapping)
        or profile.get("schema") != PROFILE_SCHEMA
        or not verify_profile(profile)
        or manifest.get("selection_source") != "profile-v2"
        or manifest.get("profile_schema") != PROFILE_SCHEMA
        or manifest.get("profile_sha256") != profile.get("profile_sha256")
        or manifest.get("profiled_executable_sha256")
        != profile.get("profiled_command", {}).get("executable_sha256")
        or manifest.get("profiled_command_sha256")
        != profile.get("profiled_command_sha256")
    ):
        return False
    command = profile["profiled_command"]
    if (
        command != original
        or command.get("executable_sha256")
        != original.get("executable_sha256")
    ):
        return False
    site = _nonnegative_int(manifest.get("site"))
    selected = [
        row for row in profile["entries"]
        if _nonnegative_int(row.get("site")) == site
    ]
    if len(selected) != 1:
        return False
    entry = selected[0]
    score = manifest.get("profile_score")
    observations = manifest.get("profile_observations")
    interesting = manifest.get("profile_interesting")
    solver_time_us = manifest.get("profile_solver_time_us")
    if (
        not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not math.isfinite(float(score))
        or not isinstance(observations, int)
        or isinstance(observations, bool)
        or not isinstance(interesting, int)
        or isinstance(interesting, bool)
        or not isinstance(solver_time_us, int)
        or isinstance(solver_time_us, bool)
    ):
        return False
    return (
        float(score) == float(entry["score"])
        and observations == entry["observations"]
        and interesting == entry["interesting"]
        and solver_time_us == entry["solver_time_us"]
    )


def _load_manifest(
    path: str | os.PathLike[str],
    selected_site: int,
) -> dict[str, Any]:
    records = _load_documents(path)
    selected = [
        row for row in records
        if (
            row.get("schema") == MANIFEST_SCHEMA
            and _nonnegative_int(row.get("site")) == selected_site
        )
    ]
    if len(records) != 1 or len(selected) != 1:
        raise ValueError(
            "manifest must contain exactly one transformed site record")
    if not bool(selected[0].get("single_site_build")):
        raise ValueError("Hydra replay requires a single-site transformed build")
    if selected[0].get("region_schema") is not None:
        try:
            verify_manifest_record(selected[0])
        except ManifestVerificationError as error:
            raise ValueError(
                f"Hydra transform manifest proof is invalid: {error}"
            ) from error
    return {
        "path": str(Path(path).expanduser().resolve()),
        "sha256": _file_digest(path),
        "record": selected[0],
    }


def _input_records(paths: Sequence[str | os.PathLike[str]]) -> list[dict[str, Any]]:
    if not paths or len(paths) > MAX_REPLAY_INPUTS:
        raise ValueError("replay input count is outside the bounded range")
    unique: dict[str, bytes] = {}
    total = 0
    for raw_path in paths:
        path = Path(raw_path)
        data = path.read_bytes()
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError(f"Hydra replay input is too large: {path}")
        total += len(data)
        if total > MAX_TOTAL_INPUT_BYTES:
            raise ValueError("Hydra replay corpus exceeds the bounded byte cap")
        digest = hashlib.sha256(data).hexdigest()
        unique[digest] = data
    return [
        {
            "sha256": digest,
            "size": len(data),
            "base64": base64.b64encode(data).decode("ascii"),
        }
        for digest, data in sorted(unique.items())
    ]


def _execute(
    command: Mapping[str, Any],
    input_data: bytes,
    input_path: Path,
    timeout_ms: int,
) -> dict[str, Any]:
    argv = [
        item.replace("@@", str(input_path))
        for item in command["argv"]
    ]
    uses_stdin = command["input_mode"] == "stdin"
    started = time.monotonic_ns()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if uses_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(
            input=input_data if uses_stdin else None,
            timeout=timeout_ms / 1000.0,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    elapsed_us = max(0, (time.monotonic_ns() - started) // 1000)
    return_code = int(process.returncode)
    if timed_out:
        status = "timeout"
    elif return_code == 0:
        status = "ok"
    elif return_code < 0:
        status = "signal"
    else:
        status = "nonzero-exit"
    return {
        "status": status,
        "return_code": return_code,
        "signal": -return_code if return_code < 0 else 0,
        "elapsed_us": elapsed_us,
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
    }


def _is_failure(result: Mapping[str, Any]) -> bool:
    return result.get("status") in {"signal", "nonzero-exit"}


def _classification(
    transformed: Mapping[str, Any],
    original: Mapping[str, Any],
) -> str:
    if "timeout" in {transformed.get("status"), original.get("status")}:
        return "inconclusive-timeout"
    transformed_failure = _is_failure(transformed)
    original_failure = _is_failure(original)
    if transformed_failure and original_failure:
        return "real-failure"
    if transformed_failure:
        return "spurious-transformed-failure"
    if original_failure:
        return "failure-preservation-violation"
    return "original-validated"


def _semantic_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "input_sha256": row["input_sha256"],
            "classification": row["classification"],
            "transformed_status": row["transformed"]["status"],
            "transformed_return_code": row["transformed"]["return_code"],
            "original_status": row["original"]["status"],
            "original_return_code": row["original"]["return_code"],
        }
        for row in rows
    ]


def run_campaign(
    original_command: Sequence[str],
    transformed_command: Sequence[str],
    inputs: Sequence[str | os.PathLike[str]],
    *,
    selected_site: int,
    manifest_path: str | os.PathLike[str],
    profile_artifact_path: str | os.PathLike[str] | None = None,
    timeout_ms: int = 1000,
) -> dict[str, Any]:
    selected_site = _nonnegative_int(selected_site)
    timeout_ms = _nonnegative_int(timeout_ms)
    if not selected_site:
        raise ValueError("selected_site must be nonzero")
    if not 1 <= timeout_ms <= 3_600_000:
        raise ValueError("timeout_ms must be in 1--3600000")
    original = _command_record(original_command)
    transformed = _command_record(transformed_command)
    manifest = _load_manifest(manifest_path, selected_site)
    selection_source = manifest["record"].get("selection_source")
    profile_evidence = (
        _load_profile_artifact(profile_artifact_path)
        if profile_artifact_path is not None
        else None
    )
    if selection_source == "profile-v2":
        if not _profile_binding_matches(
                profile_evidence, manifest["record"], original):
            raise ValueError(
                "profile artifact, compiler manifest, and original "
                "binary identity do not match")
    elif profile_evidence is not None:
        raise ValueError(
            "profile artifact supplied for a non-v2 compiler manifest")
    input_records = _input_records(inputs)
    rows: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="symcc-hydra-") as temporary:
        root = Path(temporary)
        for input_record in input_records:
            content = base64.b64decode(
                input_record["base64"], validate=True)
            input_path = root / input_record["sha256"]
            input_path.write_bytes(content)
            transformed_result = _execute(
                transformed, content, input_path, timeout_ms)
            original_result = _execute(
                original, content, input_path, timeout_ms)
            rows.append({
                "input_sha256": input_record["sha256"],
                "classification": _classification(
                    transformed_result, original_result),
                "transformed": transformed_result,
                "original": original_result,
            })

    counts: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["classification"]] += 1
    spurious = counts.get("spurious-transformed-failure", 0)
    violations = counts.get("failure-preservation-violation", 0)
    artifact: dict[str, Any] = {
        "schema": (
            CAMPAIGN_SCHEMA
            if profile_evidence is not None
            else CAMPAIGN_SCHEMA_V1
        ),
        "selected_site": selected_site,
        "timeout_ms": timeout_ms,
        "original": original,
        "transformed": transformed,
        "manifest": manifest,
        "inputs": input_records,
        "rows": rows,
        "counts": dict(sorted(counts.items())),
        "accepted_failure_inputs": sorted(
            row["input_sha256"] for row in rows
            if row["classification"] == "real-failure"
        ),
        "spurious_sites": [selected_site] if spurious else [],
        "failure_preservation_holds": violations == 0,
        "original_is_authority": True,
    }
    if profile_evidence is not None:
        artifact["profile"] = profile_evidence
    artifact["semantic_sha256"] = _digest({
        "selected_site": selected_site,
        "rows": _semantic_rows(rows),
        "spurious_sites": artifact["spurious_sites"],
        "accepted_failure_inputs": artifact["accepted_failure_inputs"],
        "failure_preservation_holds": artifact[
            "failure_preservation_holds"],
    })
    artifact["campaign_sha256"] = _artifact_digest(
        artifact, "campaign_sha256")
    if not verify_campaign(artifact):
        raise RuntimeError("internal Hydra campaign verification failed")
    return artifact


def verify_campaign(artifact: Mapping[str, Any]) -> bool:
    try:
        schema = artifact.get("schema")
        if (
            schema not in (CAMPAIGN_SCHEMA_V1, CAMPAIGN_SCHEMA)
            or artifact.get("campaign_sha256")
            != _artifact_digest(artifact, "campaign_sha256")
            or not bool(artifact.get("original_is_authority"))
            or not isinstance(artifact.get("inputs"), list)
            or not isinstance(artifact.get("rows"), list)
            or len(artifact["inputs"]) != len(artifact["rows"])
        ):
            return False
        selected_site = _nonnegative_int(artifact.get("selected_site"))
        if not selected_site:
            return False
        manifest = artifact.get("manifest")
        if (
            not isinstance(manifest, dict)
            or manifest.get("record", {}).get("schema") != MANIFEST_SCHEMA
            or _nonnegative_int(
                manifest.get("record", {}).get("site")) != selected_site
            or not bool(
                manifest.get("record", {}).get("single_site_build"))
        ):
            return False
        manifest_record = manifest["record"]
        if manifest_record.get("region_schema") is not None:
            verify_manifest_record(manifest_record)
        original = artifact.get("original")
        transformed = artifact.get("transformed")
        if (
            not _verify_command_record(original)
            or not _verify_command_record(transformed)
        ):
            return False
        if schema == CAMPAIGN_SCHEMA:
            if not _profile_binding_matches(
                    artifact.get("profile"), manifest_record, original):
                return False
        elif "profile" in artifact:
            return False
        input_hashes = []
        for row in artifact["inputs"]:
            content = base64.b64decode(row["base64"], validate=True)
            digest = hashlib.sha256(content).hexdigest()
            if (
                digest != row.get("sha256")
                or len(content) != _nonnegative_int(row.get("size"))
            ):
                return False
            input_hashes.append(digest)
        if input_hashes != sorted(set(input_hashes)):
            return False
        rows = artifact["rows"]
        if [row.get("input_sha256") for row in rows] != input_hashes:
            return False
        counts: defaultdict[str, int] = defaultdict(int)
        real = []
        spurious = False
        violation = False
        for row in rows:
            transformed = row.get("transformed")
            original = row.get("original")
            if not isinstance(transformed, dict) or not isinstance(
                    original, dict):
                return False
            classification = _classification(transformed, original)
            if row.get("classification") != classification:
                return False
            counts[classification] += 1
            if classification == "real-failure":
                real.append(row["input_sha256"])
            elif classification == "spurious-transformed-failure":
                spurious = True
            elif classification == "failure-preservation-violation":
                violation = True
        if (
            artifact.get("counts") != dict(sorted(counts.items()))
            or artifact.get("accepted_failure_inputs") != sorted(real)
            or artifact.get("spurious_sites")
            != ([selected_site] if spurious else [])
            or bool(artifact.get("failure_preservation_holds"))
            != (not violation)
        ):
            return False
        semantic = _digest({
            "selected_site": selected_site,
            "rows": _semantic_rows(rows),
            "spurious_sites": artifact["spurious_sites"],
            "accepted_failure_inputs": artifact["accepted_failure_inputs"],
            "failure_preservation_holds": artifact[
                "failure_preservation_holds"],
        })
        return artifact.get("semantic_sha256") == semantic
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        base64.binascii.Error,
    ):
        return False


def replay_campaign(artifact: Mapping[str, Any]) -> dict[str, Any]:
    if not verify_campaign(artifact):
        raise ValueError("invalid Hydra campaign")
    selected_site = _nonnegative_int(artifact["selected_site"])
    rows = []
    with tempfile.TemporaryDirectory(prefix="symcc-hydra-replay-") as temporary:
        root = Path(temporary)
        for input_record in artifact["inputs"]:
            content = base64.b64decode(
                input_record["base64"], validate=True)
            path = root / input_record["sha256"]
            path.write_bytes(content)
            transformed = _execute(
                artifact["transformed"],
                content,
                path,
                _nonnegative_int(artifact["timeout_ms"]),
            )
            original = _execute(
                artifact["original"],
                content,
                path,
                _nonnegative_int(artifact["timeout_ms"]),
            )
            rows.append({
                "input_sha256": input_record["sha256"],
                "classification": _classification(transformed, original),
                "transformed": transformed,
                "original": original,
            })
    semantic = _digest({
        "selected_site": selected_site,
        "rows": _semantic_rows(rows),
        "spurious_sites": (
            [selected_site]
            if any(row["classification"]
                   == "spurious-transformed-failure" for row in rows)
            else []
        ),
        "accepted_failure_inputs": sorted(
            row["input_sha256"] for row in rows
            if row["classification"] == "real-failure"
        ),
        "failure_preservation_holds": not any(
            row["classification"] == "failure-preservation-violation"
            for row in rows
        ),
    })
    result: dict[str, Any] = {
        "schema": REPLAY_SCHEMA,
        "source_campaign_sha256": artifact["campaign_sha256"],
        "source_semantic_sha256": artifact["semantic_sha256"],
        "replay_semantic_sha256": semantic,
        "semantic_match": semantic == artifact["semantic_sha256"],
        "rows": rows,
    }
    result["replay_sha256"] = _digest(result)
    return result


def write_denylist(
    sites: Iterable[int],
    output_path: str | os.PathLike[str],
) -> None:
    normalized = sorted({
        _nonnegative_int(site) for site in sites
        if _nonnegative_int(site)
    })
    payload = (
        "# symcc-hydra-denylist-v1\n"
        + "".join(f"{site}\n" for site in normalized)
    ).encode("ascii")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _write_json(value: Mapping[str, Any], output: str | None) -> None:
    payload = json.dumps(
        value, indent=2, sort_keys=True, ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"
    if output is None:
        print(payload.decode("ascii"), end="")
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _command_json(value: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("command JSON must be an argv list")
    return parsed


def _collect_inputs(inputs: Sequence[str], input_dir: str | None) -> list[str]:
    result = [str(Path(path).resolve()) for path in inputs]
    if input_dir:
        root = Path(input_dir)
        result.extend(
            str(path.resolve()) for path in sorted(root.iterdir())
            if path.is_file()
        )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    profile = subparsers.add_parser("profile")
    profile.add_argument("telemetry", nargs="+")
    profile.add_argument("--denylist")
    profile.add_argument("--profiled-command-json", required=True)
    profile.add_argument("--profile-output", required=True)
    profile.add_argument("--artifact-output", required=True)

    campaign = subparsers.add_parser("campaign")
    campaign.add_argument("--original-command-json", required=True)
    campaign.add_argument("--transformed-command-json", required=True)
    campaign.add_argument("--input", action="append", default=[])
    campaign.add_argument("--input-dir")
    campaign.add_argument("--site", type=int, required=True)
    campaign.add_argument("--manifest", required=True)
    campaign.add_argument("--profile-artifact")
    campaign.add_argument("--timeout-ms", type=int, default=1000)
    campaign.add_argument("--denylist-output")
    campaign.add_argument("--output", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("artifact")

    replay = subparsers.add_parser("replay")
    replay.add_argument("artifact")
    replay.add_argument("--output")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.action == "profile":
        denied: list[int] = []
        if args.denylist:
            denied = [
                _nonnegative_int(line.split()[0])
                for line in Path(args.denylist).read_text(
                    encoding="ascii").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        artifact = build_profile(
            args.telemetry,
            denied_sites=denied,
            profiled_command=_command_json(
                args.profiled_command_json),
        )
        Path(args.profile_output).write_text(
            profile_text(artifact), encoding="ascii")
        _write_json(artifact, args.artifact_output)
        return 0
    if args.action == "campaign":
        artifact = run_campaign(
            _command_json(args.original_command_json),
            _command_json(args.transformed_command_json),
            _collect_inputs(args.input, args.input_dir),
            selected_site=args.site,
            manifest_path=args.manifest,
            profile_artifact_path=args.profile_artifact,
            timeout_ms=args.timeout_ms,
        )
        _write_json(artifact, args.output)
        if args.denylist_output:
            write_denylist(
                artifact["spurious_sites"], args.denylist_output)
        return 0 if artifact["failure_preservation_holds"] else 2
    if args.action == "verify":
        artifact = json.loads(Path(args.artifact).read_text(
            encoding="utf-8"))
        verified = verify_campaign(artifact)
        print(json.dumps({"verified": verified}, sort_keys=True))
        return 0 if verified else 1
    artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    replay = replay_campaign(artifact)
    _write_json(replay, args.output)
    return 0 if replay["semantic_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
