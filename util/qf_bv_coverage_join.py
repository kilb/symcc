#!/usr/bin/env python3
"""Join verified QF_BV strategy results to replayed AFL edge/data coverage."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import resource
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from offline_policy import TrajectoryEvent
from qf_bv_campaign import _patched_candidate
from qf_bv_strategy_campaign import (
    query_context,
    verify_strategy_campaign,
)
from smt_sequence_training import (
    FEATURE_SCHEMA,
    LEGACY_FEATURE_SCHEMA,
    context_feature_vector,
)


TARGET_SCHEMA = "symcc-afl-coverage-target-v1"
CAMPAIGN_SCHEMA = "symcc-qfbv-coverage-join-v1"
REPLAY_SCHEMA = "symcc-qfbv-coverage-join-replay-v1"
CALIBRATION_SCHEMA = "symcc-qfbv-feature-calibration-v1"
MAX_INPUTS = 16384
MAX_FEATURES = 1 << 20
MIN_CONFIRMATORY_MAP_REPETITIONS = 2
_SOLVED = frozenset({"sat", "unsat"})


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
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_digest(target: Mapping[str, Any]) -> str:
    return _digest({
        key: value
        for key, value in target.items()
        if key != "target_sha256"
    })


def campaign_digest(campaign: Mapping[str, Any]) -> str:
    return _digest({
        key: value
        for key, value in campaign.items()
        if key != "campaign_sha256"
    })


def _resolve_executable(value: str) -> str:
    candidate = shutil.which(value)
    if candidate is None:
        path = Path(value).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            candidate = str(path)
    if candidate is None:
        raise FileNotFoundError(f"executable is unavailable: {value}")
    return str(Path(candidate).resolve())


def _version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0][:512] if output else ""


def seal_coverage_target(
    command: Sequence[str],
    *,
    showmap: str = "afl-showmap",
    data_preload: str,
    timeout_ms: int = 1000,
) -> dict[str, Any]:
    if (
        not isinstance(command, Sequence)
        or isinstance(command, (str, bytes))
        or not command
        or any(not isinstance(item, str) or not item for item in command)
        or sum(item.count("@@") for item in command) != 1
    ):
        raise ValueError("target command must contain exactly one @@ placeholder")
    timeout_ms = int(timeout_ms)
    if not 1 <= timeout_ms <= 3_600_000:
        raise ValueError("coverage timeout_ms must be in 1--3600000")
    target_executable = _resolve_executable(str(command[0]))
    showmap_executable = _resolve_executable(showmap)
    preload_path = str(Path(data_preload).expanduser().resolve())
    if not Path(preload_path).is_file():
        raise FileNotFoundError("data-coverage preload is unavailable")
    normalized_command = [target_executable, *map(str, command[1:])]
    target: dict[str, Any] = {
        "schema": TARGET_SCHEMA,
        "command": normalized_command,
        "target_executable": target_executable,
        "target_executable_sha256": _file_digest(target_executable),
        "showmap_executable": showmap_executable,
        "showmap_executable_sha256": _file_digest(showmap_executable),
        "showmap_version": _version(showmap_executable),
        "data_preload": preload_path,
        "data_preload_sha256": _file_digest(preload_path),
        "timeout_ms": timeout_ms,
        "protocol": "afl-showmap-streaming-sparse-v1",
        "coverage_mode": (
            "paired-preload-edge-presence-plus-reserved-data-namespace"),
    }
    target["target_sha256"] = target_digest(target)
    return target


def verify_coverage_target(
    target: Mapping[str, Any],
    *,
    check_current: bool = False,
) -> bool:
    try:
        command = target.get("command")
        if (
            target.get("schema") != TARGET_SCHEMA
            or target.get("target_sha256") != target_digest(target)
            or not isinstance(command, list)
            or not command
            or sum(str(item).count("@@") for item in command) != 1
            or command[0] != target.get("target_executable")
            or not 1 <= int(target["timeout_ms"]) <= 3_600_000
            or target.get("protocol")
            != "afl-showmap-streaming-sparse-v1"
            or target.get("coverage_mode")
            != "paired-preload-edge-presence-plus-reserved-data-namespace"
        ):
            return False
        identities = (
            ("target_executable", "target_executable_sha256"),
            ("showmap_executable", "showmap_executable_sha256"),
            ("data_preload", "data_preload_sha256"),
        )
        for path_key, digest_key in identities:
            path = str(target[path_key])
            digest = str(target[digest_key])
            if (
                not Path(path).is_absolute()
                or len(digest) != 64
                or any(character not in "0123456789abcdef"
                       for character in digest)
            ):
                return False
            if check_current and (
                not Path(path).is_file() or _file_digest(path) != digest
            ):
                return False
        return (
            not check_current
            or _version(str(target["showmap_executable"]))
            == target.get("showmap_version")
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


class _StreamingShowmap:
    _MAX_OUTPUT = 64 * 1024 * 1024

    def __init__(
        self,
        target: Mapping[str, Any],
        *,
        data_coverage: bool,
    ):
        environment = os.environ.copy()
        environment["AFL_QUIET"] = "1"
        # Both oracles preload the runtime so its PCGUARD namespace reservation
        # produces identical edge IDs.  The edge oracle disables only writes.
        environment["AFL_PRELOAD"] = str(target["data_preload"])
        if data_coverage:
            environment["SYMCC_AFL_DATA_COVERAGE"] = "1"
            environment["AFL_DATA_COVERAGE"] = "1"
        else:
            environment.pop("LD_PRELOAD", None)
            environment["SYMCC_AFL_DATA_COVERAGE"] = "0"
            environment["AFL_DATA_COVERAGE"] = "0"
        command = [
            str(target["showmap_executable"]),
            "-S",
            "-e",
            "-t",
            str(target["timeout_ms"]),
            "-m",
            "none",
            "--",
            *map(str, target["command"]),
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
        )

    def _read_exact(self, size: int) -> bytes:
        if size < 0 or size > self._MAX_OUTPUT:
            raise RuntimeError("afl-showmap response size is invalid")
        assert self.process.stdout is not None
        output = bytearray()
        while len(output) < size:
            chunk = self.process.stdout.read(size - len(output))
            if not chunk:
                raise RuntimeError("afl-showmap streaming response was truncated")
            output.extend(chunk)
        return bytes(output)

    def observe(self, content: bytes) -> tuple[int, list[list[int]], int]:
        if len(content) > 256 * 1024 * 1024:
            raise ValueError("coverage input exceeds 256 MiB")
        assert self.process.stdin is not None
        started = time.monotonic_ns()
        self.process.stdin.write(struct.pack("<I", len(content)))
        self.process.stdin.write(content)
        self.process.stdin.flush()
        status = struct.unpack("<H", self._read_exact(2))[0]
        count = struct.unpack("<I", self._read_exact(4))[0]
        if count > MAX_FEATURES:
            raise RuntimeError("afl-showmap returned too many features")
        raw_features = self._read_exact(5 * count)
        features = [
            [int(identifier), int(value)]
            for identifier, value in struct.iter_unpack("<IB", raw_features)
        ]
        for _ in range(2):
            length = struct.unpack("<I", self._read_exact(4))[0]
            self._read_exact(length)
        elapsed_us = max(0, (time.monotonic_ns() - started) // 1000)
        normalized = sorted({
            int(identifier): int(value)
            for identifier, value in features
            if int(value) > 0
        }.items())
        return status, [[key, value] for key, value in normalized], elapsed_us

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        if self.process.stdout is not None and not self.process.stdout.closed:
            self.process.stdout.close()

    def __enter__(self) -> "_StreamingShowmap":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _holdout_rows(
    strategy_campaign: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    return {
        str(row["query_id"]): row
        for row in strategy_campaign["corpus"]["queries"]
        if row["split"] == "holdout"
    }


def _expected_materialization(
    strategy_campaign: Mapping[str, Any],
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    holdout = _holdout_rows(strategy_campaign)
    inputs: dict[str, bytes] = {}
    rows: list[dict[str, Any]] = []
    for result in strategy_campaign["results"]:
        query_id = str(result["query_id"])
        envelope = holdout[query_id]["envelope"]
        witness = bytes.fromhex(str(envelope.get("input_hex", "")))
        witness_sha256 = hashlib.sha256(witness).hexdigest()
        inputs[witness_sha256] = witness
        candidate_sha256 = ""
        if result["status"] == "sat":
            candidate = _patched_candidate(
                str(envelope.get("input_hex", "")),
                result["assignments"],
            )
            if candidate is None:
                raise ValueError("verified SAT assignment cannot be materialized")
            candidate_sha256 = hashlib.sha256(candidate).hexdigest()
            inputs[candidate_sha256] = candidate
        rows.append({
            "query_id": query_id,
            "strategy": str(result["strategy"]),
            "repetition": int(result["repetition"]),
            "solver_status": str(result["status"]),
            "witness_sha256": witness_sha256,
            "candidate_sha256": candidate_sha256,
            "solver_child_cpu_us": int(result["child_cpu_us"]),
            "solver_par2_us": int(result["par2_us"]),
        })
    if len(inputs) > MAX_INPUTS:
        raise ValueError("coverage campaign exceeds the input bound")
    return inputs, rows


def _feature_ids(observation: Mapping[str, Any], mode: str) -> set[int]:
    return {
        int(item[0])
        for item in observation[mode]["features"]
    }


def _coverage_rows(
    base_rows: Sequence[Mapping[str, Any]],
    observations: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for base in base_rows:
        witness = observations[str(base["witness_sha256"])]
        candidate_sha = str(base["candidate_sha256"])
        edge_new: set[int] = set()
        combined_new: set[int] = set()
        data_new: set[int] = set()
        if candidate_sha:
            candidate = observations[candidate_sha]
            witness_edge = _feature_ids(witness, "edge")
            candidate_edge = _feature_ids(candidate, "edge")
            witness_combined = _feature_ids(witness, "combined")
            candidate_combined = _feature_ids(candidate, "combined")
            witness_data = witness_combined - witness_edge
            candidate_data = candidate_combined - candidate_edge
            edge_new = candidate_edge - witness_edge
            combined_new = candidate_combined - witness_combined
            data_new = candidate_data - witness_data
        rows.append({
            **dict(base),
            "edge_new_features": len(edge_new),
            "combined_new_features": len(combined_new),
            "data_signal_new_features": len(data_new),
            "edge_new_ids": sorted(edge_new),
            "combined_new_ids": sorted(combined_new),
            "data_signal_new_ids": sorted(data_new),
        })
    return rows


def _ratio(numerator: int, cpu_us: int) -> float:
    return round(
        numerator * 1_000_000.0 / cpu_us, 12
    ) if cpu_us > 0 else 0.0


def aggregate_coverage(
    strategy_campaign: Mapping[str, Any],
    observations: Mapping[str, Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    strategy_names = [
        str(strategy["name"]) for strategy in strategy_campaign["strategies"]]
    baseline_hashes = {str(row["witness_sha256"]) for row in rows}
    baseline_edge = set().union(*(
        _feature_ids(observations[digest], "edge")
        for digest in baseline_hashes
    ))
    baseline_combined = set().union(*(
        _feature_ids(observations[digest], "combined")
        for digest in baseline_hashes
    ))
    baseline_data = baseline_combined - baseline_edge
    by_strategy: dict[str, Any] = {}
    strategy_sets: dict[str, tuple[set[int], set[int], set[int]]] = {}
    for strategy in strategy_names:
        selected = [row for row in rows if row["strategy"] == strategy]
        candidate_hashes = {
            str(row["candidate_sha256"]) for row in selected
            if row["candidate_sha256"]
        }
        candidate_edge = set().union(baseline_edge, *(
            _feature_ids(observations[digest], "edge")
            for digest in candidate_hashes
        ))
        candidate_combined = set().union(baseline_combined, *(
            _feature_ids(observations[digest], "combined")
            for digest in candidate_hashes
        ))
        candidate_data = set().union(baseline_data, *(
            _feature_ids(observations[digest], "combined")
            - _feature_ids(observations[digest], "edge")
            for digest in candidate_hashes
        ))
        edge_gain = candidate_edge - baseline_edge
        combined_gain = candidate_combined - baseline_combined
        data_gain = candidate_data - baseline_data
        cpu_us = sum(int(row["solver_child_cpu_us"]) for row in selected)
        par2_us = sum(int(row["solver_par2_us"]) for row in selected)
        by_strategy[strategy] = {
            "runs": len(selected),
            "sat_candidates": sum(bool(row["candidate_sha256"])
                                  for row in selected),
            "unique_candidates": len(candidate_hashes),
            "edge_union_features": len(candidate_edge),
            "edge_union_gain": len(edge_gain),
            "combined_union_features": len(candidate_combined),
            "combined_union_gain": len(combined_gain),
            "data_signal_union_features": len(candidate_data),
            "data_signal_union_gain": len(data_gain),
            "solver_child_cpu_us": cpu_us,
            "solver_par2_us": par2_us,
            "edge_gain_per_solver_cpu_second": _ratio(
                len(edge_gain), cpu_us),
            "data_signal_gain_per_solver_cpu_second": _ratio(
                len(data_gain), cpu_us),
        }
        strategy_sets[strategy] = (edge_gain, data_gain, combined_gain)
    pairs: list[dict[str, Any]] = []
    for left_index, left in enumerate(strategy_names):
        for right in strategy_names[left_index + 1:]:
            row: dict[str, Any] = {"left": left, "right": right}
            for label, index in (
                ("edge", 0), ("data_signal", 1), ("combined", 2),
            ):
                left_set = strategy_sets[left][index]
                right_set = strategy_sets[right][index]
                union = left_set | right_set
                row.update({
                    f"{label}_shared": len(left_set & right_set),
                    f"{label}_left_only": len(left_set - right_set),
                    f"{label}_right_only": len(right_set - left_set),
                    f"{label}_jaccard": round(
                        len(left_set & right_set) / len(union), 12
                    ) if union else 1.0,
                })
            pairs.append(row)
    return {
        "baseline": {
            "unique_witnesses": len(baseline_hashes),
            "edge_union_features": len(baseline_edge),
            "combined_union_features": len(baseline_combined),
            "data_signal_union_features": len(baseline_data),
        },
        "strategies": by_strategy,
        "pairs": pairs,
    }


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        exponential = math.exp(-min(60.0, value))
        return 1.0 / (1.0 + exponential)
    exponential = math.exp(max(-60.0, value))
    return exponential / (1.0 + exponential)


def _fit_logistic(
    samples: Sequence[tuple[Sequence[float], int]],
) -> list[float]:
    if not samples:
        raise ValueError("calibration requires training samples")
    dimension = len(samples[0][0])
    weights = [0.0] * dimension
    for iteration in range(400):
        gradient = [0.0] * dimension
        for vector, label in samples:
            prediction = _sigmoid(sum(
                weight * value for weight, value in zip(weights, vector)))
            residual = prediction - label
            for index, value in enumerate(vector):
                gradient[index] += residual * float(value)
        rate = 0.3 / (1.0 + iteration / 100.0)
        for index in range(dimension):
            regularization = 0.0 if index == 0 else 0.01 * weights[index]
            weights[index] -= rate * (
                gradient[index] / len(samples) + regularization)
    return [round(value, 15) for value in weights]


def _calibration_metrics(
    predictions: Sequence[tuple[float, int]],
) -> dict[str, Any]:
    if not predictions:
        return {
            "rows": 0,
            "positives": 0,
            "brier": 0.0,
            "log_loss": 0.0,
            "ece_5": 0.0,
        }
    brier = sum((value - label) ** 2
                for value, label in predictions) / len(predictions)
    log_loss = -sum(
        label * math.log(max(1e-12, value))
        + (1 - label) * math.log(max(1e-12, 1.0 - value))
        for value, label in predictions
    ) / len(predictions)
    bins: list[list[tuple[float, int]]] = [[] for _ in range(5)]
    for value, label in predictions:
        bins[min(4, int(value * 5.0))].append((value, label))
    ece = 0.0
    for items in bins:
        if not items:
            continue
        confidence = sum(value for value, _ in items) / len(items)
        accuracy = sum(label for _, label in items) / len(items)
        ece += len(items) / len(predictions) * abs(confidence - accuracy)
    return {
        "rows": len(predictions),
        "positives": sum(label for _, label in predictions),
        "brier": round(brier, 15),
        "log_loss": round(log_loss, 15),
        "ece_5": round(ece, 15),
    }


def feature_calibration(
    strategy_campaign: Mapping[str, Any],
    coverage_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    events: list[TrajectoryEvent] = []
    for raw in strategy_campaign["policy_bundle"]["training_events"]:
        event = TrajectoryEvent.from_mapping(raw)
        if event is None:
            raise ValueError("policy bundle contains an invalid event")
        events.append(event)
    holdout = _holdout_rows(strategy_campaign)
    output: dict[str, Any] = {
        "schema": CALIBRATION_SCHEMA,
        "evaluation_scope": "individual-backend-holdout-results",
        "label": "edge-new-features-positive",
        "schemas": {},
    }
    for schema in (LEGACY_FEATURE_SCHEMA, FEATURE_SCHEMA):
        models: dict[str, list[float]] = {}
        for backend in strategy_campaign["backend_names"]:
            selected = [event for event in events if event.action == backend]
            samples = [(
                context_feature_vector(event.context, schema=schema),
                int(event.coverage_delta > 0),
            ) for event in selected]
            models[str(backend)] = _fit_logistic(samples)
        predictions: list[dict[str, Any]] = []
        metrics_input: list[tuple[float, int]] = []
        for row in coverage_rows:
            strategy = str(row["strategy"])
            if not strategy.startswith("individual:"):
                continue
            backend = strategy.removeprefix("individual:")
            vector = context_feature_vector(
                query_context(holdout[str(row["query_id"])]),
                schema=schema,
            )
            probability = _sigmoid(sum(
                weight * value
                for weight, value in zip(models[backend], vector)
            ))
            probability = round(probability, 15)
            label = int(int(row["edge_new_features"]) > 0)
            metrics_input.append((probability, label))
            predictions.append({
                "query_id": str(row["query_id"]),
                "backend": backend,
                "repetition": int(row["repetition"]),
                "probability": probability,
                "label": label,
            })
        output["schemas"][schema] = {
            "models": models,
            "predictions": predictions,
            "metrics": _calibration_metrics(metrics_input),
        }
    return output


def campaign_semantic_digest(campaign: Mapping[str, Any]) -> str:
    observations = campaign.get("observations", ())
    projected_observations = []
    if isinstance(observations, Sequence) and not isinstance(
            observations, (str, bytes)):
        for row in observations:
            if not isinstance(row, Mapping):
                continue
            projected_observations.append({
                "input_sha256": row.get("input_sha256"),
                "input_hex": row.get("input_hex"),
                "edge_status": row.get("edge", {}).get("status")
                if isinstance(row.get("edge"), Mapping) else None,
                "edge_features": row.get("edge", {}).get("features")
                if isinstance(row.get("edge"), Mapping) else None,
                "combined_status": row.get("combined", {}).get("status")
                if isinstance(row.get("combined"), Mapping) else None,
                "combined_features": row.get("combined", {}).get("features")
                if isinstance(row.get("combined"), Mapping) else None,
            })
    return _digest({
        "strategy_semantic_sha256": (
            campaign.get("strategy_campaign", {}).get("semantic_sha256")
            if isinstance(campaign.get("strategy_campaign"), Mapping)
            else ""
        ),
        "target_sha256": (
            campaign.get("target", {}).get("target_sha256")
            if isinstance(campaign.get("target"), Mapping) else ""),
        "protocol": campaign.get("protocol"),
        "observations": projected_observations,
        "coverage_rows": campaign.get("coverage_rows"),
        "aggregate": campaign.get("aggregate"),
        "feature_calibration": campaign.get("feature_calibration"),
    })


def _run_observations(
    inputs: Mapping[str, bytes],
    target: Mapping[str, Any],
    repetitions: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    observations: list[dict[str, Any]] = []
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    wall_started = time.monotonic_ns()
    with (
        _StreamingShowmap(target, data_coverage=False) as edge_oracle,
        _StreamingShowmap(target, data_coverage=True) as combined_oracle,
    ):
        for input_sha256, content in sorted(inputs.items()):
            modes: dict[str, Any] = {}
            for mode, oracle in (
                ("edge", edge_oracle),
                ("combined", combined_oracle),
            ):
                runs = [
                    oracle.observe(content) for _ in range(repetitions)
                ]
                signatures = {
                    _digest({"status": status, "features": features})
                    for status, features, _ in runs
                }
                if len(signatures) != 1:
                    raise RuntimeError(
                        f"{mode} bitmap is unstable for {input_sha256}")
                status, features, _ = runs[0]
                modes[mode] = {
                    "status": status,
                    "features": features,
                    "features_sha256": _digest(features),
                    "repetitions": repetitions,
                    "stable": True,
                    "wall_us": sum(elapsed for _, _, elapsed in runs),
                }
            observations.append({
                "input_sha256": input_sha256,
                "input_hex": content.hex(),
                **modes,
            })
    wall_us = max(0, (time.monotonic_ns() - wall_started) // 1000)
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu_us = int(round((
        usage_after.ru_utime + usage_after.ru_stime
        - usage_before.ru_utime - usage_before.ru_stime
    ) * 1_000_000))
    return observations, {
        "wall_us": wall_us,
        "showmap_child_cpu_us": max(0, child_cpu_us),
    }


def run_coverage_join(
    strategy_campaign: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    map_repetitions: int = 2,
    confirmatory: bool = False,
) -> dict[str, Any]:
    if not verify_strategy_campaign(strategy_campaign):
        raise ValueError("QF_BV strategy campaign is invalid")
    if not verify_coverage_target(target, check_current=True):
        raise ValueError("AFL coverage target identity is invalid")
    map_repetitions = int(map_repetitions)
    if not 1 <= map_repetitions <= 100:
        raise ValueError("map_repetitions must be in 1--100")
    if (
        confirmatory
        and map_repetitions < MIN_CONFIRMATORY_MAP_REPETITIONS
    ):
        raise ValueError("confirmatory joins require at least two map replays")
    if (
        confirmatory
        and strategy_campaign["protocol"].get("confirmatory") is not True
    ):
        raise ValueError(
            "confirmatory coverage requires a confirmatory strategy campaign")
    inputs, base_rows = _expected_materialization(strategy_campaign)
    observations, replay_cost = _run_observations(
        inputs, target, map_repetitions)
    by_digest = {row["input_sha256"]: row for row in observations}
    coverage_rows = _coverage_rows(base_rows, by_digest)
    campaign: dict[str, Any] = {
        "schema": CAMPAIGN_SCHEMA,
        "generated_unix_ms": int(time.time() * 1000),
        "strategy_campaign": copy.deepcopy(dict(strategy_campaign)),
        "target": copy.deepcopy(dict(target)),
        "protocol": {
            "map_repetitions": map_repetitions,
            "minimum_confirmatory_map_repetitions": (
                MIN_CONFIRMATORY_MAP_REPETITIONS),
            "confirmatory": bool(confirmatory),
            "input_order": "sha256-sorted",
            "map_semantics": "edge-presence",
            "data_signal_semantics": (
                "paired-preload-combined-minus-disabled-data-baseline"),
            "performance_claims": bool(confirmatory),
        },
        "coverage_replay_cost": replay_cost,
        "observations": observations,
        "coverage_rows": coverage_rows,
        "aggregate": aggregate_coverage(
            strategy_campaign, by_digest, coverage_rows),
        "feature_calibration": feature_calibration(
            strategy_campaign, coverage_rows),
    }
    campaign["semantic_sha256"] = campaign_semantic_digest(campaign)
    campaign["campaign_sha256"] = campaign_digest(campaign)
    return campaign


def _verify_mode(mode: Mapping[str, Any], repetitions: int) -> bool:
    try:
        features = mode.get("features")
        if (
            not isinstance(features, list)
            or len(features) > MAX_FEATURES
            or mode.get("features_sha256") != _digest(features)
            or mode.get("repetitions") != repetitions
            or mode.get("stable") is not True
            or int(mode["wall_us"]) < 0
            or not 0 <= int(mode["status"]) <= 0xffff
        ):
            return False
        previous = -1
        for item in features:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], int)
                or not isinstance(item[1], int)
                or item[0] <= previous
                or not 0 <= item[0] < (1 << 32)
                or not 1 <= item[1] <= 255
            ):
                return False
            previous = item[0]
        return True
    except (KeyError, TypeError, ValueError):
        return False


def verify_coverage_join(campaign: Mapping[str, Any]) -> bool:
    try:
        if (
            campaign.get("schema") != CAMPAIGN_SCHEMA
            or campaign.get("campaign_sha256") != campaign_digest(campaign)
            or campaign.get("semantic_sha256")
            != campaign_semantic_digest(campaign)
        ):
            return False
        strategy = campaign.get("strategy_campaign")
        target = campaign.get("target")
        protocol = campaign.get("protocol")
        if (
            not isinstance(strategy, Mapping)
            or not verify_strategy_campaign(strategy)
            or not isinstance(target, Mapping)
            or not verify_coverage_target(target)
            or not isinstance(protocol, Mapping)
        ):
            return False
        repetitions = int(protocol["map_repetitions"])
        confirmatory = protocol.get("confirmatory") is True
        if (
            not 1 <= repetitions <= 100
            or protocol.get("minimum_confirmatory_map_repetitions") != 2
            or (confirmatory and repetitions < 2)
            or (confirmatory
                and strategy["protocol"].get("confirmatory") is not True)
            or protocol.get("input_order") != "sha256-sorted"
            or protocol.get("map_semantics") != "edge-presence"
            or protocol.get("data_signal_semantics")
            != "paired-preload-combined-minus-disabled-data-baseline"
            or protocol.get("performance_claims") is not confirmatory
        ):
            return False
        inputs, base_rows = _expected_materialization(strategy)
        observations = campaign.get("observations")
        if (
            not isinstance(observations, list)
            or len(observations) != len(inputs)
            or [row.get("input_sha256") for row in observations
                if isinstance(row, Mapping)] != sorted(inputs)
        ):
            return False
        by_digest: dict[str, Mapping[str, Any]] = {}
        for row in observations:
            if not isinstance(row, Mapping):
                return False
            digest = str(row["input_sha256"])
            content = bytes.fromhex(str(row["input_hex"]))
            if (
                digest != hashlib.sha256(content).hexdigest()
                or inputs.get(digest) != content
                or not isinstance(row.get("edge"), Mapping)
                or not isinstance(row.get("combined"), Mapping)
                or not _verify_mode(row["edge"], repetitions)
                or not _verify_mode(row["combined"], repetitions)
            ):
                return False
            edge_ids = _feature_ids(row, "edge")
            combined_ids = _feature_ids(row, "combined")
            if (
                row["edge"]["status"] != row["combined"]["status"]
                or not edge_ids.issubset(combined_ids)
                or any(identifier >= 65536
                       for identifier in combined_ids - edge_ids)
            ):
                return False
            by_digest[digest] = row
        expected_rows = _coverage_rows(base_rows, by_digest)
        if campaign.get("coverage_rows") != expected_rows:
            return False
        if (
            campaign.get("aggregate")
            != aggregate_coverage(strategy, by_digest, expected_rows)
            or campaign.get("feature_calibration")
            != feature_calibration(strategy, expected_rows)
        ):
            return False
        replay_cost = campaign.get("coverage_replay_cost")
        return (
            isinstance(replay_cost, Mapping)
            and int(replay_cost["wall_us"]) >= 0
            and int(replay_cost["showmap_child_cpu_us"]) >= 0
            and int(campaign.get("generated_unix_ms", -1)) >= 0
        )
    except (
        KeyError, OSError, RuntimeError, TypeError, ValueError, OverflowError,
    ):
        return False


def replay_coverage_join(campaign: Mapping[str, Any]) -> dict[str, Any]:
    if not verify_coverage_join(campaign):
        raise ValueError("source coverage-join campaign is invalid")
    if not verify_coverage_target(campaign["target"], check_current=True):
        raise ValueError("current AFL coverage target identity differs")
    protocol = campaign["protocol"]
    replay = run_coverage_join(
        campaign["strategy_campaign"],
        campaign["target"],
        map_repetitions=protocol["map_repetitions"],
        confirmatory=protocol["confirmatory"],
    )
    result = {
        "schema": REPLAY_SCHEMA,
        "source_campaign_sha256": campaign["campaign_sha256"],
        "source_semantic_sha256": campaign["semantic_sha256"],
        "replay_semantic_sha256": replay["semantic_sha256"],
        "semantic_match": (
            replay["semantic_sha256"] == campaign["semantic_sha256"]),
        "replay": replay,
    }
    result["replay_sha256"] = _digest(result)
    return result


def _load_json(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="ascii"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_or_print(value: Mapping[str, Any], output: str | None) -> None:
    payload = _canonical_json(value) + b"\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    else:
        print(payload.decode("ascii"), end="")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--verify")
    action.add_argument("--replay")
    parser.add_argument("--strategy-campaign")
    parser.add_argument("--target-command-json")
    parser.add_argument("--showmap", default="afl-showmap")
    parser.add_argument("--data-preload")
    parser.add_argument("--timeout-ms", type=int, default=1000)
    parser.add_argument("--map-repetitions", type=int, default=2)
    parser.add_argument("--confirmatory", action="store_true")
    parser.add_argument("--output")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.verify:
        verified = verify_coverage_join(_load_json(args.verify))
        print(json.dumps({"verified": verified}, sort_keys=True))
        return 0 if verified else 1
    if args.replay:
        replay = replay_coverage_join(_load_json(args.replay))
        _write_or_print(replay, args.output)
        return 0 if replay["semantic_match"] else 1
    if not all((
        args.strategy_campaign,
        args.target_command_json,
        args.data_preload,
    )):
        raise ValueError(
            "--strategy-campaign, --target-command-json and "
            "--data-preload are required")
    command = json.loads(args.target_command_json)
    if not isinstance(command, list):
        raise ValueError("--target-command-json must be an argv JSON list")
    target = seal_coverage_target(
        command,
        showmap=args.showmap,
        data_preload=args.data_preload,
        timeout_ms=args.timeout_ms,
    )
    campaign = run_coverage_join(
        _load_json(args.strategy_campaign),
        target,
        map_repetitions=args.map_repetitions,
        confirmatory=args.confirmatory,
    )
    _write_or_print(campaign, args.output)
    return 0 if verify_coverage_join(campaign) else 1


if __name__ == "__main__":
    raise SystemExit(main())
