#!/usr/bin/env python3
"""Exercise replay-verified EVP recovery and durable checkpoint retry."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

import online_value_profile as online_module  # noqa: E402
from online_value_profile import OnlineValueProfileCoordinator  # noqa: E402


CONTEXT = "a" * 64


def _telemetry(value: int) -> dict[str, Any]:
    return {
        "empirical_value_profile_context": CONTEXT,
        "empirical_value_profiles": [[11, 8, 1, 0, [[value, 1]]]],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_manifest(directory: Path) -> None:
    rows = [
        f"{_sha256(path)}  {path.relative_to(directory)}"
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS.txt"
    ]
    (directory / "SHA256SUMS.txt").write_text(
        "\n".join(rows) + "\n", encoding="ascii")


def _require_profile(content: bytes, values: str) -> None:
    expected = f"profile {CONTEXT} 11 8 {values}\n".encode("ascii")
    if expected not in content:
        raise RuntimeError(f"runtime is missing {expected!r}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify recovered-record replay and checkpoint retry for online EVP"
        ))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    directory = output / "coordinator"

    coordinator = OnlineValueProfileCoordinator(
        directory, min_observations=1, publish_interval_seconds=0)
    if not coordinator.observe(_telemetry(1)):
        raise RuntimeError("initial telemetry was rejected")
    first = coordinator.publish(force=True)
    if first is None:
        raise RuntimeError("initial domain was not published")
    _require_profile(first.content, "1 1")
    (output / "before-drift.runtime").write_bytes(first.content)

    state_path = directory / "state.json"
    state = json.loads(state_path.read_text(encoding="ascii"))
    state["records"][0] = _telemetry(2)
    drifted_state = (
        json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    )
    (output / "drifted-state.json").write_text(
        drifted_state, encoding="ascii")
    state_path.write_text(drifted_state, encoding="ascii")

    resumed = OnlineValueProfileCoordinator(
        directory, min_observations=1, publish_interval_seconds=0)
    if (
        resumed.current is None
        or not resumed.dirty
        or resumed.recovery_replays != 1
        or resumed.recovery_replay_mismatches != 1
    ):
        raise RuntimeError("recovered record drift was not detected")
    recovered_snapshot = resumed.snapshot()
    second = resumed.publish(force=True)
    if second is None:
        raise RuntimeError("drifted records did not publish a replacement")
    _require_profile(second.content, "1 2")
    if b" 1 1\n" in second.content:
        raise RuntimeError("replacement retained the stale domain")
    (output / "after-replay.runtime").write_bytes(second.content)

    if not resumed.observe(_telemetry(3)):
        raise RuntimeError("checkpoint-failure telemetry was rejected")
    original_write = online_module._atomic_write

    def fail_state(path: str | Path, content: bytes) -> None:
        if Path(path).name == "state.json":
            raise OSError("injected checkpoint failure")
        original_write(path, content)

    with mock.patch.object(
            online_module, "_atomic_write", side_effect=fail_state):
        third = resumed.publish(force=True)
    if third is None or not resumed.dirty or resumed.checkpoint_failures != 1:
        raise RuntimeError("checkpoint failure did not preserve retry state")
    _require_profile(third.content, "2 2 3")
    (output / "checkpoint-failed.runtime").write_bytes(third.content)

    if resumed.publish(force=True) is not None or resumed.dirty:
        raise RuntimeError("checkpoint retry did not converge as a semantic no-op")
    final_state = json.loads(state_path.read_text(encoding="ascii"))
    if final_state.get("checkpoint_failures") != 1:
        raise RuntimeError("checkpoint failure count was not persisted")

    summary = {
        "schema": "symcc-evp-recovery-smoke-v1",
        "initial": {
            "artifact_sha256": first.artifact_sha256,
            "runtime_version": first.version,
            "domain_values": [1],
        },
        "recovery": {
            "artifact_label_preserved": (
                state["current_artifact_sha256"] == first.artifact_sha256),
            "dirty": recovered_snapshot["dirty"],
            "replays": recovered_snapshot["recovery_replays"],
            "mismatches": recovered_snapshot["recovery_replay_mismatches"],
            "replacement_artifact_sha256": second.artifact_sha256,
            "domain_values": [2],
        },
        "checkpoint_retry": {
            "published_artifact_sha256": third.artifact_sha256,
            "dirty_after_injected_failure": True,
            "checkpoint_failures": final_state["checkpoint_failures"],
            "dirty_after_retry": resumed.dirty,
            "semantic_noops": resumed.semantic_noops,
            "domain_values": [2, 3],
        },
        "final_snapshot": resumed.snapshot(),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    _write_manifest(output)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
