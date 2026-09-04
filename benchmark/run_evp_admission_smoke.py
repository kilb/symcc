#!/usr/bin/env python3
"""Run a real QSYM feedback/suppression/re-exploration smoke experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from empirical_value_profile import verify_value_profile  # noqa: E402
from online_value_profile import OnlineValueProfileCoordinator  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_process_result(
    directory: Path, name: str, result: subprocess.CompletedProcess[bytes]
) -> None:
    (directory / f"{name}.stdout").write_bytes(result.stdout)
    (directory / f"{name}.stderr").write_bytes(result.stderr)


def _run_target(
    target: Path,
    directory: Path,
    name: str,
    payload: bytes,
    context: str,
    runtime_profile: Path | None = None,
) -> dict[str, Any]:
    output = directory / f"{name}.output"
    telemetry = directory / f"{name}.json"
    output.mkdir()
    environment = os.environ.copy()
    environment.update({
        "SYMCC_OUTPUT_DIR": str(output),
        "SYMCC_TELEMETRY_OUT": str(telemetry),
        "SYMCC_VALUE_PROFILE": "1",
        "SYMCC_VALUE_PROFILE_CONTEXT": context,
    })
    if runtime_profile is not None:
        environment["SYMCC_VALUE_PROFILE_IN"] = str(runtime_profile)
    result = subprocess.run(
        [str(target)],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    _write_process_result(directory, name, result)
    if result.returncode != 0:
        raise RuntimeError(f"{name} exited with {result.returncode}")
    document = json.loads(telemetry.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise RuntimeError(f"{name} did not produce a telemetry object")
    return document


def _load_artifact(
    coordinator: OnlineValueProfileCoordinator,
) -> Mapping[str, Any]:
    if coordinator.current is None:
        raise RuntimeError("coordinator did not publish a runtime generation")
    path = (
        coordinator.generations
        / f"{coordinator.current.artifact_sha256}.json"
    )
    artifact = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(artifact, Mapping) or not verify_value_profile(artifact):
        raise RuntimeError("coordinator published an invalid artifact")
    return artifact


def _feedback_rows(
    document: Mapping[str, Any], key: tuple[int, int, tuple[int, ...]]
) -> list[list[Any]]:
    rows = []
    for row in document.get("empirical_domain_feedback", ()):  # type: ignore[union-attr]
        if (
            isinstance(row, list)
            and len(row) in {11, 12}
            and (row[0], row[1], tuple(row[2])) == key
        ):
            rows.append(row)
    return rows


def _assert_feedback_conservation(rows: list[list[Any]]) -> None:
    for row in rows:
        if not (
            row[3] == row[4] + row[5]
            and row[5] == row[6] + row[9] + row[10]
            and row[6] == row[7] + row[8]
        ):
            raise RuntimeError(f"feedback conservation failed: {row!r}")


def _copy_runtime(coordinator: OnlineValueProfileCoordinator, path: Path) -> None:
    if coordinator.current is None:
        raise RuntimeError("runtime generation is absent")
    path.write_bytes(coordinator.current.content)


def _write_manifest(directory: Path) -> None:
    rows = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            rows.append(f"{_sha256(path)}  {path.relative_to(directory)}")
    (directory / "SHA256SUMS").write_text(
        "\n".join(rows) + "\n", encoding="ascii")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify empirical-domain admission, low-yield suppression, and "
            "rolling re-exploration with the real QSYM runtime"
        ))
    parser.add_argument("--compiler", type=Path, default=ROOT / "build" / "symcc")
    parser.add_argument(
        "--source",
        type=Path,
        default=ROOT / "test" / "empirical_value_profile_feedback.c",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    compiler = args.compiler.resolve()
    source = args.source.resolve()
    target = output / "target"

    version = subprocess.run(
        [str(compiler), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    _write_process_result(output, "compiler-version", version)
    compile_result = subprocess.run(
        [str(compiler), "-O0", str(source), "-o", str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    _write_process_result(output, "compile", compile_result)
    if compile_result.returncode != 0:
        raise RuntimeError(f"compilation failed with {compile_result.returncode}")
    context = _sha256(target)

    coordinator = OnlineValueProfileCoordinator(
        output / "coordinator",
        window=2,
        min_observations=2,
        max_distinct_values=2,
        publish_interval_seconds=0,
        feedback_min_solver_queries=2,
        feedback_min_validated_ratio_ppm=125_000,
        feedback_min_solver_time_us=0,
    )

    collect_zero = _run_target(target, output, "collect-zero", b"\x00", context)
    collect_one = _run_target(target, output, "collect-one", b"\x01", context)
    if coordinator.observe_many((collect_zero, collect_one)) != 2:
        raise RuntimeError("collection telemetry was rejected")
    admitted = coordinator.publish(force=True)
    if admitted is None or admitted.profile_count == 0:
        raise RuntimeError("initial empirical domains were not admitted")
    _copy_runtime(coordinator, output / "admitted.runtime")
    admitted_artifact = _load_artifact(coordinator)

    probe_zero = _run_target(
        target, output, "probe-zero", b"\x00", context,
        output / "admitted.runtime")
    probe_one = _run_target(
        target, output, "probe-one", b"\x01", context,
        output / "admitted.runtime")
    if coordinator.observe_many((probe_zero, probe_one)) != 2:
        raise RuntimeError("probe telemetry was rejected")
    suppressed = coordinator.publish(force=True)
    if suppressed is None or suppressed.suppressed_count == 0:
        raise RuntimeError("low-yield empirical domain was not suppressed")
    _copy_runtime(coordinator, output / "suppressed.runtime")
    suppressed_artifact = _load_artifact(coordinator)
    suppressed_proofs = suppressed_artifact["online_admission"]["suppressed"]
    proof = suppressed_proofs[0]
    exact_key = (
        int(proof["site"]),
        int(proof["bits"]),
        tuple(int(value) for value in proof["domain_values"]),
    )
    probe_rows = (
        _feedback_rows(probe_zero, exact_key)
        + _feedback_rows(probe_one, exact_key)
    )
    _assert_feedback_conservation(probe_rows)
    if (
        sum(row[5] for row in probe_rows) < 2
        or sum(row[7] for row in probe_rows) != 0
    ):
        raise RuntimeError("suppression was not backed by two failed real queries")
    exact_solver_time_us = sum(
        row[11] if len(row) == 12 else 0 for row in probe_rows)
    if exact_solver_time_us <= 0:
        raise RuntimeError("real empirical queries did not report solver cost")

    # Replay the same real observations through a counterfactual cost floor.
    # One microsecond above the measured total must preserve both domains even
    # though the query-count and validated-ratio gates already fail.
    cost_guarded = OnlineValueProfileCoordinator(
        output / "cost-guarded-coordinator",
        window=2,
        min_observations=2,
        max_distinct_values=2,
        publish_interval_seconds=0,
        feedback_min_solver_queries=2,
        feedback_min_validated_ratio_ppm=125_000,
        feedback_min_solver_time_us=exact_solver_time_us + 1,
    )
    if cost_guarded.observe_many((probe_zero, probe_one)) != 2:
        raise RuntimeError("cost-guard replay telemetry was rejected")
    guarded = cost_guarded.publish(force=True)
    if guarded is None or guarded.profile_count != 2 or guarded.suppressed_count:
        raise RuntimeError("cost floor did not preserve the cheap exact domain")
    _copy_runtime(cost_guarded, output / "cost-guarded.runtime")
    guarded_artifact = _load_artifact(cost_guarded)

    post_zero = _run_target(
        target, output, "suppressed-zero", b"\x00", context,
        output / "suppressed.runtime")
    post_one = _run_target(
        target, output, "suppressed-one", b"\x01", context,
        output / "suppressed.runtime")
    if _feedback_rows(post_zero, exact_key) or _feedback_rows(post_one, exact_key):
        raise RuntimeError("suppressed exact domain was still queried")
    if coordinator.observe_many((post_zero, post_one)) != 2:
        raise RuntimeError("post-suppression telemetry was rejected")
    readmitted = coordinator.publish(force=True)
    if readmitted is None or readmitted.suppressed_count != 0:
        raise RuntimeError("rolling-window expiry did not re-admit the domain")
    _copy_runtime(coordinator, output / "readmitted.runtime")
    readmitted_artifact = _load_artifact(coordinator)

    reprobe = _run_target(
        target, output, "reprobe", b"\x00", context,
        output / "readmitted.runtime")
    reprobe_rows = _feedback_rows(reprobe, exact_key)
    _assert_feedback_conservation(reprobe_rows)
    if not reprobe_rows or sum(row[5] for row in reprobe_rows) == 0:
        raise RuntimeError("re-admitted domain was not explored again")

    summary = {
        "schema": "symcc-evp-admission-smoke-v1",
        "compiler": str(compiler),
        "source": str(source),
        "target_sha256": context,
        "window": 2,
        "feedback_min_solver_queries": 2,
        "feedback_min_validated_ratio_ppm": 125_000,
        "feedback_min_solver_time_us": 0,
        "exact_domain": {
            "site": exact_key[0],
            "bits": exact_key[1],
            "values": list(exact_key[2]),
        },
        "admitted": {
            "artifact_sha256": admitted_artifact["profile_sha256"],
            "runtime_profiles": admitted.profile_count,
            "suppressed_profiles": admitted.suppressed_count,
        },
        "probe_feedback": {
            "rows": len(probe_rows),
            "attempts": sum(row[3] for row in probe_rows),
            "prefilter_rejects": sum(row[4] for row in probe_rows),
            "solver_queries": sum(row[5] for row in probe_rows),
            "sat": sum(row[6] for row in probe_rows),
            "validated": sum(row[7] for row in probe_rows),
            "validation_failures": sum(row[8] for row in probe_rows),
            "solver_unsat": sum(row[9] for row in probe_rows),
            "unknown": sum(row[10] for row in probe_rows),
            "solver_time_us": exact_solver_time_us,
        },
        "cost_guarded": {
            "artifact_sha256": guarded_artifact["profile_sha256"],
            "min_solver_time_us": exact_solver_time_us + 1,
            "runtime_profiles": guarded.profile_count,
            "suppressed_profiles": guarded.suppressed_count,
        },
        "suppressed": {
            "artifact_sha256": suppressed_artifact["profile_sha256"],
            "runtime_profiles": suppressed.profile_count,
            "suppressed_profiles": suppressed.suppressed_count,
            "post_suppression_exact_rows": 0,
        },
        "readmitted": {
            "artifact_sha256": readmitted_artifact["profile_sha256"],
            "runtime_profiles": readmitted.profile_count,
            "suppressed_profiles": readmitted.suppressed_count,
            "reprobe_exact_rows": len(reprobe_rows),
            "reprobe_solver_queries": sum(row[5] for row in reprobe_rows),
        },
        "coordinator": coordinator.snapshot(),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    _write_manifest(output)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
