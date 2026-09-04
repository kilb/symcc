#!/usr/bin/env python3
"""Independent finite and native-replay oracles for native ConDPOR."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from native_condpor_campaign import (  # noqa: E402
    _atomic_commit_summary,
    run_native_condpor_campaign,
    verify_native_condpor_campaign_certificate,
)
from schedule_exploration import (  # noqa: E402
    native_condpor_memory_graph_certificate,
    parse_schedule_trace,
    verify_native_condpor_memory_graph_certificate,
)


def _linear_extensions(
    events: tuple[str, ...], order: set[tuple[str, str]]
) -> Iterable[tuple[str, ...]]:
    for candidate in itertools.permutations(events):
        positions = {event: index for index, event in enumerate(candidate)}
        if all(positions[left] < positions[right] for left, right in order):
            yield candidate


def _sc_store_buffering_outcomes() -> set[tuple[int, int]]:
    outcomes: set[tuple[int, int]] = set()
    for order in _linear_extensions(
        ("wx", "ry", "wy", "rx"), {("wx", "ry"), ("wy", "rx")}
    ):
        memory = {"x": 0, "y": 0}
        reads: dict[str, int] = {}
        for event in order:
            if event == "wx":
                memory["x"] = 1
            elif event == "wy":
                memory["y"] = 1
            elif event == "rx":
                reads["rx"] = memory["x"]
            else:
                reads["ry"] = memory["y"]
        outcomes.add((reads["ry"], reads["rx"]))
    return outcomes


def _production_memory_oracles() -> dict[str, Any]:
    store_buffering = parse_schedule_trace(
        "0 1 write x atomic=1 bytes=1 mo=relaxed\n"
        "1 1 read y atomic=1 bytes=1 mo=relaxed\n"
        "2 2 write y atomic=1 bytes=1 mo=relaxed\n"
        "3 2 read x atomic=1 bytes=1 mo=relaxed\n"
    )
    certificates = {
        model: native_condpor_memory_graph_certificate(
            store_buffering, memory_model=model
        )
        for model in ("SC", "TSO", "RA")
    }
    assert all(
        verify_native_condpor_memory_graph_certificate(certificate)
        for certificate in certificates.values()
    )
    signatures = {
        model: {
            tuple(int(row["source_index"]) for row in graph["read_from"])
            for graph in certificate["graphs"]
        }
        for model, certificate in certificates.items()
    }
    source_outcomes = _sc_store_buffering_outcomes()
    assert source_outcomes == {(0, 1), (1, 0), (1, 1)}
    assert (-1, -1) not in signatures["SC"]
    assert (-1, -1) in signatures["TSO"]
    assert (-1, -1) in signatures["RA"]

    message_passing = native_condpor_memory_graph_certificate(
        parse_schedule_trace(
            "0 1 write data atomic=1 bytes=1 mo=relaxed\n"
            "1 1 write flag atomic=1 bytes=1 mo=release\n"
            "2 2 read flag atomic=1 bytes=1 mo=acquire\n"
            "3 2 read data atomic=1 bytes=1 mo=relaxed\n"
        ),
        memory_model="RA",
    )
    assert verify_native_condpor_memory_graph_certificate(message_passing)
    message_signatures = {
        tuple(int(row["source_index"]) for row in graph["read_from"])
        for graph in message_passing["graphs"]
    }
    assert (1, -1) not in message_signatures

    three_writers = native_condpor_memory_graph_certificate(
        parse_schedule_trace(
            "0 1 write x atomic=1 bytes=1 mo=relaxed\n"
            "1 2 write x atomic=1 bytes=1 mo=relaxed\n"
            "2 3 write x atomic=1 bytes=1 mo=relaxed\n"
        ),
        memory_model="RA",
    )
    assert verify_native_condpor_memory_graph_certificate(three_writers)
    coherence_orders = {
        tuple(graph["modification_orders"][0]["event_indices"])
        for graph in three_writers["graphs"]
    }
    assert coherence_orders == set(itertools.permutations((0, 1, 2)))
    return {
        "sc_source_outcomes": sorted([list(value) for value in source_outcomes]),
        "store_buffering_status_counts": {
            model: certificate["status_counts"]
            for model, certificate in certificates.items()
        },
        "weak_model_both_initial": {
            model: (-1, -1) in model_signatures
            for model, model_signatures in signatures.items()
        },
        "ra_message_passing_stale_data_rejected": True,
        "ra_message_passing_status_counts": message_passing["status_counts"],
        "three_writer_coherence_orders": len(coherence_orders),
        "memory_certificate_sha256": {
            model: certificate["certificate_sha256"]
            for model, certificate in certificates.items()
        },
    }


def _tag(tags: tuple[str, ...], key: str) -> str:
    prefix = f"{key}="
    return next((tag[len(prefix):] for tag in tags if tag.startswith(prefix)), "")


def _native_replay_oracle(
    symcc: Path, runtime: Path, repetitions: int
) -> dict[str, Any]:
    symcc = symcc.resolve(strict=True)
    runtime = runtime.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="symcc-native-condpor-oracle-") as tmp:
        work = Path(tmp)
        target = work / "atomic-replay"
        compile_env = os.environ.copy()
        compile_env.update({
            "SYMCC_DPOR_ATOMIC_ONLY": "1",
            "SYMCC_DPOR_SCHEDULE_ONLY": "1",
        })
        subprocess.run(
            [
                str(symcc),
                "-std=c11",
                "-O0",
                str(ROOT / "test" / "schedule_atomic_commit_replay.c"),
                "-pthread",
                "-o",
                str(target),
            ],
            env=compile_env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        cases = {
            "reader_first": ((2, 1), "0x0"),
            "writer_first": ((1, 2), "0x1"),
        }
        results: dict[str, Any] = {}
        total_runs = 0
        for case, (prefix, expected) in cases.items():
            observed: list[str] = []
            trace_digests: list[str] = []
            for iteration in range(repetitions):
                prefix_path = work / f"{case}-{iteration}.prefix"
                trace_path = work / f"{case}-{iteration}.trace"
                prefix_path.write_text(
                    "".join(f"{tid}\n" for tid in prefix), encoding="ascii"
                )
                env = os.environ.copy()
                env.update({
                    "LD_PRELOAD": str(runtime),
                    "SYMCC_DPOR": "1",
                    "SYMCC_SCHEDULE_MEMORY": "1",
                    "SYMCC_SCHEDULE_ATOMIC_COMMIT": "1",
                    "SYMCC_SCHEDULE_WAIT_MS": "2000",
                    "SYMCC_SCHEDULE_PREFIX": str(prefix_path),
                    "SYMCC_SCHEDULE_TRACE": str(trace_path),
                })
                completed = subprocess.run(
                    [str(target)],
                    env=env,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
                assert completed.returncode == 0, completed.stderr.decode(
                    errors="replace"
                )
                raw = trace_path.read_text(encoding="utf-8")
                events = parse_schedule_trace(raw)
                reads = [
                    _tag(event.tags, "read-value") or _tag(event.tags, "value")
                    for event in events
                    if event.op == "read" and _tag(event.tags, "atomic") == "1"
                ]
                assert reads == [expected]
                commits = [event for event in events if event.op == "atomic_commit"]
                assert len(commits) == 2
                assert sum(_tag(event.tags, "advanced") == "1" for event in commits) == 2
                assert not any(
                    event.op in {"fallback", "atomic_pending_conflict"}
                    or (
                        event.op == "atomic_commit"
                        and _tag(event.tags, "mismatch") == "1"
                    )
                    for event in events
                )
                commit_summary = _atomic_commit_summary(events, "SC")
                assert commit_summary["sc_reads_from_hardware_enforced"]
                observed.extend(reads)
                trace_digests.append(hashlib.sha256(raw.encode()).hexdigest())
                total_runs += 1
            results[case] = {
                "prefix": list(prefix),
                "expected_read": expected,
                "runs": repetitions,
                "matching_reads": sum(value == expected for value in observed),
                "unique_raw_trace_digests": len(set(trace_digests)),
            }

        campaign = run_native_condpor_campaign(
            [str(target)],
            schedule_runtime=runtime,
            cwd=work,
            memory_model="SC",
            max_runs=8,
            max_prefixes=16,
            max_successors_per_run=8,
            max_graph_candidates=64,
            timeout_seconds=5,
        )
        assert verify_native_condpor_campaign_certificate(campaign)
        assert campaign["invalid_run_count"] == 0
        return {
            "target_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "total_replay_runs": total_runs,
            "cases": results,
            "campaign_status": campaign["status"],
            "campaign_run_count": campaign["run_count"],
            "campaign_unique_prefix_count": campaign["unique_prefix_count"],
            "campaign_path_regeneration_count": campaign[
                "path_regeneration_count"
            ],
            "campaign_invalid_run_count": campaign["invalid_run_count"],
            "campaign_certificate_sha256": campaign["certificate_sha256"],
        }


def run_oracle(
    *, symcc: Path | None, runtime: Path | None, repetitions: int
) -> dict[str, Any]:
    finite = _production_memory_oracles()
    native = None
    if symcc is not None or runtime is not None:
        if symcc is None or runtime is None:
            raise ValueError("--symcc and --runtime must be supplied together")
        native = _native_replay_oracle(symcc, runtime, repetitions)
    return {
        "schema": "symcc-native-condpor-c11-oracle-v1",
        "all_passed": True,
        "finite_memory_model": finite,
        "native_replay": native,
        "claim_boundary": (
            "Bounded SC/TSO/RA litmus relations and finite native SC atomic "
            "replay; not a full ISO C11 model, herd7 corpus, unbounded ConDPOR "
            "proof, or public-target performance result"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symcc", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repetitions < 1 or args.repetitions > 1000:
        parser.error("--repetitions must be in [1, 1000]")
    payload = run_oracle(
        symcc=args.symcc,
        runtime=args.runtime,
        repetitions=args.repetitions,
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if args.output is not None:
        args.output.write_text(encoded + "\n", encoding="ascii")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
