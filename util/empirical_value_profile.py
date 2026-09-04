#!/usr/bin/env python3
"""Aggregate bounded runtime value profiles for profile-guided solving."""

from __future__ import annotations

import argparse
import copy
import heapq
import hashlib
import json
import math
import os
import tempfile
from collections import defaultdict
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROFILE_SCHEMA = "symcc-empirical-value-profile-v1"
LEGACY_ONLINE_PROFILE_SCHEMA = "symcc-empirical-value-profile-v2"
ONLINE_PROFILE_SCHEMA = "symcc-empirical-value-profile-v3"
LEGACY_ONLINE_ADMISSION_SCHEMA = "symcc-empirical-domain-admission-v1"
ONLINE_ADMISSION_SCHEMA = "symcc-empirical-domain-admission-v2"
RUNTIME_SCHEMA = "symcc-empirical-value-runtime-v1"
PROFILE_POLICY = "empirical-sat-accept-unsat-full-formula-fallback-v1"
MAX_INPUT_FILES = 10_000
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_PROFILES = 4096
MAX_VALUES = 64
MAX_RUNTIME_VALUES = 8
MAX_DOMAIN_FEEDBACK = 512
UINT64_MAX = (1 << 64) - 1


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body.pop("profile_sha256", None)
    return hashlib.sha256(_canonical_json(body)).hexdigest()


def _bounded_int(value: Any, lower: int, upper: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if lower <= value <= upper else None


def _normalized_context(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    if any(byte not in "0123456789abcdef" for byte in value):
        return None
    return value


def _normalized_entry(raw: Any) -> tuple[
        int, int, int, bool, tuple[tuple[int, int], ...]] | None:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    if len(raw) != 5:
        return None
    site = _bounded_int(raw[0], 1, UINT64_MAX)
    bits = _bounded_int(raw[1], 1, 64)
    observations = _bounded_int(raw[2], 1, UINT64_MAX)
    saturated_raw = _bounded_int(raw[3], 0, 1)
    values_raw = raw[4]
    if (site is None or bits is None or observations is None
            or saturated_raw is None
            or not isinstance(values_raw, Sequence)
            or isinstance(values_raw, (str, bytes))
            or len(values_raw) > MAX_VALUES):
        return None
    maximum = (1 << bits) - 1
    values: list[tuple[int, int]] = []
    seen: set[int] = set()
    for pair in values_raw:
        if (not isinstance(pair, Sequence)
                or isinstance(pair, (str, bytes)) or len(pair) != 2):
            return None
        value = _bounded_int(pair[0], 0, maximum)
        count = _bounded_int(pair[1], 1, UINT64_MAX)
        if value is None or count is None or value in seen:
            return None
        seen.add(value)
        values.append((value, count))
    recorded = sum(count for _, count in values)
    if not values or recorded > observations:
        return None
    if (not saturated_raw and recorded != observations
            or saturated_raw and recorded >= observations):
        return None
    return site, bits, observations, bool(saturated_raw), tuple(values)


def _normalized_domain_feedback(raw: Any) -> tuple[
        int, int, tuple[int, ...], int, int, int, int, int, int, int, int, int
] | None:
    """Validate one exact-domain outcome row emitted by the solver runtime."""
    if (not isinstance(raw, Sequence) or isinstance(raw, (str, bytes))
            or len(raw) not in {11, 12}):
        return None
    site = _bounded_int(raw[0], 1, UINT64_MAX)
    bits = _bounded_int(raw[1], 1, 64)
    raw_values = raw[2]
    if (site is None or bits is None
            or not isinstance(raw_values, Sequence)
            or isinstance(raw_values, (str, bytes))
            or not 0 < len(raw_values) <= MAX_RUNTIME_VALUES):
        return None
    maximum = (1 << bits) - 1
    values: list[int] = []
    for raw_value in raw_values:
        value = _bounded_int(raw_value, 0, maximum)
        if value is None or value in values:
            return None
        values.append(value)
    values.sort()
    counts = tuple(
        _bounded_int(value, 0, UINT64_MAX) for value in raw[3:11]
    )
    if any(value is None for value in counts):
        return None
    (attempts, prefilter_rejects, solver_queries, sat, validated,
     validation_failures, solver_unsat, unknown) = counts
    assert all(value is not None for value in counts)
    if (
        attempts == 0
        or prefilter_rejects + solver_queries != attempts
        or sat + solver_unsat + unknown != solver_queries
        or validated + validation_failures != sat
    ):
        return None
    solver_time_us = (
        _bounded_int(raw[11], 0, UINT64_MAX) if len(raw) == 12 else 0)
    if solver_time_us is None:
        return None
    return (
        site, bits, tuple(values), attempts, prefilter_rejects,
        solver_queries, sat, validated, validation_failures, solver_unsat,
        unknown, solver_time_us,
    )


def _profile_priority(key: tuple[str, int, int]) -> int:
    context, site, bits = key
    encoded = f"{context}:{site}:{bits}".encode("ascii")
    return int.from_bytes(hashlib.sha256(encoded).digest(), "big")


def normalize_value_profile_telemetry(
    document: Any,
) -> dict[str, Any] | None:
    """Return one bounded canonical runtime telemetry document."""
    if not isinstance(document, Mapping):
        return None
    context = _normalized_context(
        document.get("empirical_value_profile_context"))
    raw_profiles = document.get("empirical_value_profiles", ())
    if (
        context is None
        or not isinstance(raw_profiles, Sequence)
        or isinstance(raw_profiles, (str, bytes, bytearray))
    ):
        return None
    profiles: list[list[Any]] = []
    for raw in raw_profiles[:MAX_PROFILES]:
        entry = _normalized_entry(raw)
        if entry is None:
            continue
        site, bits, observations, saturated, values = entry
        profiles.append([
            site,
            bits,
            observations,
            int(saturated),
            [[value, count] for value, count in values],
        ])
    feedback: list[list[Any]] = []
    seen_feedback: set[tuple[int, int, tuple[int, ...]]] = set()
    raw_feedback = document.get("empirical_domain_feedback", ())
    if (isinstance(raw_feedback, Sequence)
            and not isinstance(raw_feedback, (str, bytes, bytearray))):
        for raw in raw_feedback[:MAX_DOMAIN_FEEDBACK]:
            entry = _normalized_domain_feedback(raw)
            if entry is None:
                continue
            key = entry[0], entry[1], entry[2]
            if key in seen_feedback:
                continue
            seen_feedback.add(key)
            feedback.append([
                entry[0], entry[1], list(entry[2]), *entry[3:],
            ])
    return {
        "empirical_value_profile_context": context,
        "empirical_value_profiles": profiles,
        "empirical_domain_feedback": feedback,
    }


def apply_online_admission_policy(
    artifact: Mapping[str, Any],
    telemetry: Iterable[Mapping[str, Any]],
    *,
    min_solver_queries: int = 8,
    min_validated_ratio_ppm: int = 125_000,
    min_solver_time_us: int = 1_000,
) -> dict[str, Any]:
    """Attach proof-carrying, rolling low-yield suppression to a v1 profile.

    Suppression is exact-domain scoped. Once old feedback leaves the caller's
    rolling window, a domain is automatically eligible for exploration again.
    """
    if not verify_value_profile(artifact):
        raise ValueError("invalid empirical value profile artifact")
    minimum = max(1, min(int(min_solver_queries), UINT64_MAX))
    ratio_ppm = max(0, min(int(min_validated_ratio_ppm), 1_000_000))
    minimum_time = max(0, min(int(min_solver_time_us), UINT64_MAX))
    totals: dict[
        tuple[str, int, int, tuple[int, ...]], list[int]
    ] = {}
    overflowed: set[tuple[str, int, int, tuple[int, ...]]] = set()
    for document in islice(telemetry, MAX_INPUT_FILES):
        normalized = normalize_value_profile_telemetry(document)
        if normalized is None:
            continue
        context = normalized["empirical_value_profile_context"]
        for raw in normalized["empirical_domain_feedback"]:
            entry = _normalized_domain_feedback(raw)
            if entry is None:
                continue
            key = context, entry[0], entry[1], entry[2]
            destination = totals.setdefault(key, [0] * 9)
            for index, increment in enumerate(entry[3:]):
                value = destination[index] + increment
                if value > UINT64_MAX:
                    overflowed.add(key)
                    break
                destination[index] = value

    suppressed = []
    materializable = 0
    for profile in artifact["profiles"]:
        values = tuple(item["value"] for item in profile["values"])
        if (not profile["limited_domain"] or profile["saturated"]
                or not 0 < len(values) <= MAX_RUNTIME_VALUES):
            continue
        materializable += 1
        key = (
            profile["context_sha256"], profile["site"], profile["bits"],
            values,
        )
        if key in overflowed:
            continue
        counts = totals.get(key)
        if counts is None:
            continue
        (attempts, prefilter_rejects, solver_queries, sat, validated,
         validation_failures, solver_unsat, unknown, solver_time_us) = counts
        if (
            solver_queries >= minimum
            and validated * 1_000_000 < solver_queries * ratio_ppm
            and solver_time_us >= minimum_time
        ):
            suppressed.append({
                "context_sha256": key[0],
                "site": key[1],
                "bits": key[2],
                "domain_values": list(key[3]),
                "attempts": attempts,
                "prefilter_rejects": prefilter_rejects,
                "solver_queries": solver_queries,
                "sat": sat,
                "validated": validated,
                "validation_failures": validation_failures,
                "solver_unsat": solver_unsat,
                "unknown": unknown,
                "solver_time_us": solver_time_us,
                "reason": "costly-low-validated-query-ratio-v2",
            })

    result = copy.deepcopy(dict(artifact))
    result.pop("profile_sha256", None)
    result["schema"] = ONLINE_PROFILE_SCHEMA
    result["runtime_domain_count"] = materializable - len(suppressed)
    result["online_admission"] = {
        "schema": ONLINE_ADMISSION_SCHEMA,
        "min_solver_queries": minimum,
        "min_validated_ratio_ppm": ratio_ppm,
        "min_solver_time_us": minimum_time,
        "suppressed": suppressed,
    }
    result["profile_sha256"] = _digest(result)
    if not verify_value_profile(result):
        raise ValueError("failed to construct online admission artifact")
    return result


def aggregate_value_profiles(
    telemetry: Iterable[Mapping[str, Any]],
    *,
    min_observations: int = 8,
    max_distinct_values: int = 4,
) -> dict[str, Any]:
    """Build a deterministic limited-domain artifact from telemetry records.

    A saturated runtime profile is never admitted as a limited domain.  The
    artifact is evidence for an optimistic first pass only; UNSAT under these
    empirical domains must be retried without them.
    """
    minimum = max(1, min(int(min_observations), UINT64_MAX))
    distinct_limit = max(1, min(int(max_distinct_values), MAX_VALUES))
    counts: dict[tuple[str, int, int], dict[int, int]] = defaultdict(dict)
    observations: dict[tuple[str, int, int], int] = defaultdict(int)
    runs: dict[tuple[str, int, int], int] = defaultdict(int)
    saturated: set[tuple[str, int, int]] = set()
    contexts: set[str] = set()
    record_count = 0

    valid_documents: list[tuple[str, Sequence[Any]]] = []
    selected_keys: set[tuple[str, int, int]] = set()
    selected_heap: list[tuple[int, tuple[str, int, int]]] = []
    for document in islice(telemetry, MAX_INPUT_FILES):
        if not isinstance(document, Mapping):
            continue
        context = _normalized_context(
            document.get("empirical_value_profile_context"))
        if context is None:
            continue
        record_count += 1
        contexts.add(context)
        raw_profiles = document.get("empirical_value_profiles", ())
        if (not isinstance(raw_profiles, Sequence)
                or isinstance(raw_profiles, (str, bytes))):
            raw_profiles = ()
        valid_documents.append((context, raw_profiles))
        for raw in raw_profiles[:MAX_PROFILES]:
            entry = _normalized_entry(raw)
            if entry is None:
                continue
            site, bits, _, _, _ = entry
            key = (context, site, bits)
            if key in selected_keys:
                continue
            priority = _profile_priority(key)
            heap_entry = (-priority, key)
            if len(selected_keys) < MAX_PROFILES:
                selected_keys.add(key)
                heapq.heappush(selected_heap, heap_entry)
            elif priority < -selected_heap[0][0]:
                _, removed = heapq.heapreplace(selected_heap, heap_entry)
                selected_keys.remove(removed)
                selected_keys.add(key)

    for context, raw_profiles in valid_documents:
        for raw in raw_profiles[:MAX_PROFILES]:
            entry = _normalized_entry(raw)
            if entry is None:
                continue
            site, bits, observed, was_saturated, values = entry
            key = (context, site, bits)
            if key not in selected_keys:
                continue
            runs[key] = min(UINT64_MAX, runs[key] + 1)
            observations[key] = min(
                UINT64_MAX, observations[key] + observed)
            if was_saturated:
                saturated.add(key)
            destination = counts[key]
            for value, count in values:
                if value not in destination and len(destination) >= MAX_VALUES:
                    saturated.add(key)
                    continue
                destination[value] = min(
                    UINT64_MAX, destination.get(value, 0) + count)

    profiles = []
    admitted = 0
    for key in sorted(observations):
        context, site, bits = key
        value_counts = counts[key]
        total = observations[key]
        is_saturated = key in saturated
        is_limited = (
            total >= minimum
            and not is_saturated
            and 0 < len(value_counts) <= distinct_limit
            and sum(value_counts.values()) == total
        )
        if is_limited:
            admitted += 1
        complete_counts = sum(value_counts.values()) == total
        entropy = None
        if complete_counts:
            probabilities = [
                count / total for count in value_counts.values()]
            computed_entropy = -sum(
                probability * math.log2(probability)
                for probability in probabilities if probability > 0.0
            )
            entropy = 0.0 if computed_entropy == 0.0 else round(
                computed_entropy, 9)
        profiles.append({
            "context_sha256": context,
            "site": site,
            "bits": bits,
            "runs": runs[key],
            "observations": total,
            "distinct_values": len(value_counts),
            "saturated": is_saturated,
            "limited_domain": is_limited,
            "entropy_bits": entropy,
            "values": [
                {"value": value, "count": count}
                for value, count in sorted(value_counts.items())
            ],
        })

    artifact = {
        "schema": PROFILE_SCHEMA,
        "policy": PROFILE_POLICY,
        "input_records": record_count,
        "contexts": sorted(contexts),
        "profile_count": len(profiles),
        "limited_domain_count": admitted,
        "min_observations": minimum,
        "max_distinct_values": distinct_limit,
        "profiles": profiles,
    }
    artifact["profile_sha256"] = _digest(artifact)
    return artifact


def _verify_online_admission(artifact: Mapping[str, Any]) -> bool:
    admission = artifact.get("online_admission")
    modern = artifact.get("schema") == ONLINE_PROFILE_SCHEMA
    expected_admission_fields = {
        "schema", "min_solver_queries", "min_validated_ratio_ppm",
        "suppressed",
    }
    expected_admission_schema = LEGACY_ONLINE_ADMISSION_SCHEMA
    if modern:
        expected_admission_fields.add("min_solver_time_us")
        expected_admission_schema = ONLINE_ADMISSION_SCHEMA
    if (not isinstance(admission, Mapping)
            or set(admission) != expected_admission_fields
            or admission.get("schema") != expected_admission_schema):
        return False
    minimum = _bounded_int(
        admission.get("min_solver_queries"), 1, UINT64_MAX)
    ratio_ppm = _bounded_int(
        admission.get("min_validated_ratio_ppm"), 0, 1_000_000)
    minimum_time = (
        _bounded_int(admission.get("min_solver_time_us"), 0, UINT64_MAX)
        if modern else 0
    )
    suppressed = admission.get("suppressed")
    runtime_count = _bounded_int(
        artifact.get("runtime_domain_count"), 0, MAX_PROFILES)
    if (minimum is None or ratio_ppm is None or minimum_time is None
            or runtime_count is None
            or not isinstance(suppressed, list)
            or len(suppressed) > MAX_PROFILES):
        return False

    materializable: set[tuple[str, int, int, tuple[int, ...]]] = set()
    try:
        for profile in artifact["profiles"]:
            values = tuple(item["value"] for item in profile["values"])
            if (profile["limited_domain"] and not profile["saturated"]
                    and 0 < len(values) <= MAX_RUNTIME_VALUES):
                materializable.add((
                    profile["context_sha256"], profile["site"],
                    profile["bits"], values,
                ))
    except (KeyError, TypeError):
        return False

    expected_fields = {
        "context_sha256", "site", "bits", "domain_values", "attempts",
        "prefilter_rejects", "solver_queries", "sat", "validated",
        "validation_failures", "solver_unsat", "unknown", "reason",
    }
    if modern:
        expected_fields.add("solver_time_us")
    seen: set[tuple[str, int, int, tuple[int, ...]]] = set()
    for item in suppressed:
        if not isinstance(item, Mapping) or set(item) != expected_fields:
            return False
        context = _normalized_context(item.get("context_sha256"))
        site = _bounded_int(item.get("site"), 1, UINT64_MAX)
        bits = _bounded_int(item.get("bits"), 1, 64)
        raw_values = item.get("domain_values")
        if (context is None or site is None or bits is None
                or not isinstance(raw_values, list)
                or not 0 < len(raw_values) <= MAX_RUNTIME_VALUES):
            return False
        maximum = (1 << bits) - 1
        normalized_values: list[int] = []
        for raw_value in raw_values:
            value = _bounded_int(raw_value, 0, maximum)
            if value is None or value in normalized_values:
                return False
            normalized_values.append(value)
        values = tuple(normalized_values)
        if values != tuple(sorted(values)):
            return False
        key = context, site, bits, values
        if key not in materializable or key in seen:
            return False
        seen.add(key)
        count_names = [
            "attempts", "prefilter_rejects", "solver_queries", "sat",
            "validated", "validation_failures", "solver_unsat", "unknown",
        ]
        if modern:
            count_names.append("solver_time_us")
        counts = tuple(
            _bounded_int(item.get(name), 0, UINT64_MAX)
            for name in count_names
        )
        if any(value is None for value in counts):
            return False
        (attempts, prefilter_rejects, solver_queries, sat, validated,
         validation_failures, solver_unsat, unknown) = counts[:8]
        solver_time_us = counts[8] if modern else 0
        expected_reason = (
            "costly-low-validated-query-ratio-v2"
            if modern else "low-validated-query-ratio-v1"
        )
        if (
            attempts == 0
            or prefilter_rejects + solver_queries != attempts
            or sat + solver_unsat + unknown != solver_queries
            or validated + validation_failures != sat
            or solver_queries < minimum
            or validated * 1_000_000 >= solver_queries * ratio_ppm
            or (modern and solver_time_us < minimum_time)
            or item.get("reason") != expected_reason
        ):
            return False
    return runtime_count == len(materializable) - len(seen)


def verify_value_profile(artifact: Any) -> bool:
    if not isinstance(artifact, Mapping):
        return False
    schema = artifact.get("schema")
    if schema not in {
        PROFILE_SCHEMA, LEGACY_ONLINE_PROFILE_SCHEMA, ONLINE_PROFILE_SCHEMA,
    }:
        return False
    digest = artifact.get("profile_sha256")
    try:
        digest_matches = isinstance(digest, str) and digest == _digest(artifact)
    except (TypeError, ValueError, OverflowError):
        digest_matches = False
    if not digest_matches:
        return False
    profiles = artifact.get("profiles")
    if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES:
        return False
    try:
        input_records = _bounded_int(
            artifact["input_records"], 0, MAX_INPUT_FILES)
        if input_records is None:
            return False
        artifact_contexts = artifact["contexts"]
        if (not isinstance(artifact_contexts, list)
                or artifact_contexts != sorted(set(artifact_contexts))
                or any(_normalized_context(value) is None
                       for value in artifact_contexts)
                or len(artifact_contexts) > input_records):
            return False
        grouped: dict[str, list[list[Any]]] = {
            context: [] for context in artifact_contexts
        }
        for profile in profiles:
            if _bounded_int(profile["runs"], 1, input_records) is None:
                return False
            context = profile["context_sha256"]
            grouped.setdefault(context, []).append([
                profile["site"],
                profile["bits"],
                profile["observations"],
                int(profile["saturated"]),
                [[item["value"], item["count"]]
                 for item in profile["values"]],
            ])
        rebuilt = aggregate_value_profiles(
            [
                {
                    "empirical_value_profile_context": context,
                    "empirical_value_profiles": grouped[context],
                }
                for context in sorted(grouped)
            ],
            min_observations=int(artifact["min_observations"]),
            max_distinct_values=int(artifact["max_distinct_values"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if (schema in {LEGACY_ONLINE_PROFILE_SCHEMA, ONLINE_PROFILE_SCHEMA}
            and not _verify_online_admission(artifact)):
        return False
    comparable = dict(artifact)
    comparable.pop("profile_sha256", None)
    if schema in {LEGACY_ONLINE_PROFILE_SCHEMA, ONLINE_PROFILE_SCHEMA}:
        comparable.pop("online_admission", None)
        comparable.pop("runtime_domain_count", None)
        comparable["schema"] = PROFILE_SCHEMA
    rebuilt_body = dict(rebuilt)
    rebuilt_body.pop("profile_sha256", None)
    # Aggregating an aggregate collapses its source-run count and input count;
    # all semantic profile fields must otherwise remain canonical.
    rebuilt_body["input_records"] = comparable.get("input_records")
    for source, replay in zip(
            comparable.get("profiles", ()), rebuilt_body.get("profiles", ())):
        replay["runs"] = source.get("runs")
    return rebuilt_body == comparable


def materialize_runtime_profile(artifact: Mapping[str, Any]) -> bytes:
    """Emit the strict, dependency-free sidecar consumed by the QSYM runtime.

    Only verified, unsaturated limited domains are emitted. Runtime domains
    remain bounded by the collector's per-site capacity; wider aggregate
    domains stay in the research artifact but are deliberately not injected.
    """
    if not verify_value_profile(artifact):
        raise ValueError("invalid empirical value profile artifact")

    suppressed: set[tuple[str, int, int, tuple[int, ...]]] = set()
    if artifact.get("schema") in {
        LEGACY_ONLINE_PROFILE_SCHEMA, ONLINE_PROFILE_SCHEMA,
    }:
        suppressed = {
            (
                item["context_sha256"], item["site"], item["bits"],
                tuple(item["domain_values"]),
            )
            for item in artifact["online_admission"]["suppressed"]
        }
    rows: list[tuple[str, int, int, tuple[int, ...]]] = []
    for profile in artifact["profiles"]:
        values = tuple(item["value"] for item in profile["values"])
        key = (
            profile["context_sha256"], profile["site"], profile["bits"],
            values,
        )
        if (profile["limited_domain"]
                and not profile["saturated"]
                and 0 < len(values) <= MAX_RUNTIME_VALUES
                and key not in suppressed):
            rows.append((
                profile["context_sha256"],
                profile["site"],
                profile["bits"],
                values,
            ))

    lines = [
        RUNTIME_SCHEMA,
        f"artifact_sha256 {artifact['profile_sha256']}",
        f"policy {PROFILE_POLICY}",
        f"profile_count {len(rows)}",
    ]
    for context, site, bits, values in rows:
        lines.append(" ".join([
            "profile", context, str(site), str(bits), str(len(values)),
            *(str(value) for value in values),
        ]))
    return ("\n".join(lines) + "\n").encode("ascii")


def _load_documents(paths: Sequence[Path]) -> list[Mapping[str, Any]]:
    documents: list[Mapping[str, Any]] = []
    for path in paths[:MAX_INPUT_FILES]:
        try:
            with path.open("rb") as stream:
                encoded = stream.read(MAX_INPUT_BYTES + 1)
            if len(encoded) > MAX_INPUT_BYTES:
                continue
            value = json.loads(encoded.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            documents.append(value)
    return documents


def _load_artifact(path: Path) -> Mapping[str, Any] | None:
    try:
        with path.open("rb") as stream:
            encoded = stream.read(MAX_INPUT_BYTES + 1)
        if len(encoded) > MAX_INPUT_BYTES:
            return None
        artifact = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return artifact if isinstance(artifact, Mapping) else None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate SymCC empirical value profile telemetry")
    parser.add_argument("telemetry", nargs="*", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--runtime-output", type=Path)
    parser.add_argument("--min-observations", type=int, default=8)
    parser.add_argument("--max-distinct-values", type=int, default=4)
    args = parser.parse_args()
    if args.verify is not None:
        artifact = _load_artifact(args.verify)
        verified = artifact is not None and verify_value_profile(artifact)
        if verified and args.runtime_output is not None:
            _atomic_write(
                args.runtime_output, materialize_runtime_profile(artifact))
        print(json.dumps({"verified": verified}, sort_keys=True))
        return 0 if verified else 1
    artifact = aggregate_value_profiles(
        _load_documents(args.telemetry),
        min_observations=args.min_observations,
        max_distinct_values=args.max_distinct_values,
    )
    encoded = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        _atomic_write(args.output, encoded.encode("ascii"))
    if args.runtime_output is not None:
        _atomic_write(
            args.runtime_output, materialize_runtime_profile(artifact))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
