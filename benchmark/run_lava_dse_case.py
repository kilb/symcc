#!/usr/bin/env python3
"""Run one reproducible LAVA-M dynamic-symbolic-execution case.

The script is deliberately single-process.  It is the unit executed by
``research_protocol.py`` for runtime-solver ablations; MPI and AFL campaigns
remain the responsibility of ``run_benchmark.py``.  Each invocation retains
the generated corpus, solver telemetry, strategy labels, and LAVA listed-bug
replay result in one directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from count_lava_bugs import SPECS, iter_inputs, load_listed, replay, repo_path


REPO_ROOT = Path(__file__).resolve().parents[1]
LABEL_RE = re.compile(r"^\d+(?:-(.+))?$")


def profile_environment(profile: str, run_dir: Path) -> dict[str, str]:
    """Return a closed runtime profile for a fair solver comparison."""
    disabled = {
        "SYMCC_FAST_SOLVE": "0",
        "SYMCC_OPTIMISTIC_FIRST": "0",
        "SYMCC_BACKSOLVER": "0",
        "SYMCC_MULTI_SOLVE": "0",
        "SYMCC_POLY_CACHE": "0",
        "SYMCC_PREFIX_CONTEXT_CACHE": "0",
        "SYMCC_UNSAT_CORE_CACHE": "0",
        "SYMCC_SELECTIVE_QUERY": "0",
        "SYMCC_DATA_COVERAGE": "0",
        "SYMCC_POLY_CROSS_PREFIX": "0",
    }
    if profile == "strict-z3":
        return disabled
    if profile == "fast-optimistic":
        return {
            **disabled,
            "SYMCC_FAST_SOLVE": "1",
            "SYMCC_OPTIMISTIC_FIRST": "1",
            "SYMCC_BACKSOLVER": "1",
            "SYMCC_SELECTIVE_QUERY": "1",
        }
    if profile == "runtime-full":
        return {
            **disabled,
            "SYMCC_FAST_SOLVE": "1",
            "SYMCC_OPTIMISTIC_FIRST": "1",
            "SYMCC_BACKSOLVER": "1",
            "SYMCC_MULTI_SOLVE": "2",
            "SYMCC_POLY_CACHE": str(run_dir / "poly_cache.jsonl"),
            "SYMCC_PREFIX_CONTEXT_CACHE": "1",
            "SYMCC_UNSAT_CORE_CACHE": "1",
            "SYMCC_SELECTIVE_QUERY": "1",
            "SYMCC_DATA_COVERAGE": "1",
            "SYMCC_POLY_CROSS_PREFIX": "1",
        }
    raise ValueError(f"unknown profile: {profile}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def count_strategy_labels(corpus: Path) -> dict[str, int]:
    labels: Counter[str] = Counter()
    for entry in corpus.iterdir():
        if not entry.is_file() or entry.name.endswith(".hints"):
            continue
        match = LABEL_RE.match(entry.name)
        labels[match.group(1) if match and match.group(1) else "strict"] += 1
    return dict(sorted(labels.items()))


def count_lava_hits(program: str, corpus: Path, timeout: float) -> dict[str, object]:
    spec = SPECS[program]
    binary = repo_path(spec.binary)
    if program == "who" and not binary.exists():
        binary = repo_path("benchmark/public/bin/lava-m-cov/who")
    listed = load_listed(repo_path(spec.listed_bugs))
    hits: set[int] = set()
    first_input: dict[str, str] = {}
    timeouts = 0
    files = 0
    for input_path in iter_inputs(corpus, recursive=False):
        files += 1
        found, timed_out = replay(binary, spec.args, input_path, timeout, False)
        timeouts += int(timed_out)
        for bug_id in found:
            hits.add(bug_id)
            first_input.setdefault(str(bug_id), input_path.name)
    listed_hits = hits & listed
    return {
        "replay_binary": str(binary),
        "files_replayed": files,
        "replay_timeouts": timeouts,
        "listed_total": len(listed),
        "listed_hits": sorted(listed_hits),
        "listed_hit_count": len(listed_hits),
        "all_hits": sorted(hits),
        "extra_hits": sorted(hits - listed),
        "first_input": dict(sorted(first_input.items(), key=lambda item: int(item[0]))),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute one retained LAVA-M DSE solver-ablation case.")
    parser.add_argument("--program", choices=sorted(SPECS), required=True)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--profile", choices=("strict-z3", "fast-optimistic", "runtime-full"),
                        required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--replay-timeout", type=float, default=2.0)
    parser.add_argument("--run-dir", type=Path,
                        default=os.environ.get("SYMCC_RESEARCH_RUN_DIR", ""))
    parser.add_argument("--binary", type=Path)
    args = parser.parse_args()

    if not args.seed.is_file():
        parser.error(f"seed does not exist: {args.seed}")
    if not args.run_dir:
        parser.error("--run-dir or SYMCC_RESEARCH_RUN_DIR is required")
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    corpus = run_dir / "corpus"
    corpus.mkdir(exist_ok=True)
    telemetry_path = run_dir / "telemetry.json"
    target_stdout = run_dir / "target.stdout.bin"
    target_stderr = run_dir / "target.stderr.bin"
    target_binary = (args.binary.resolve() if args.binary else
                     REPO_ROOT / "benchmark/public/bin/lava-m" / args.program)
    if not target_binary.is_file():
        parser.error(f"symbolic binary does not exist: {target_binary}")

    environment = os.environ.copy()
    environment.update(profile_environment(args.profile, run_dir))
    environment.update({
        "SYMCC_OUTPUT_DIR": str(corpus),
        "SYMCC_INPUT_FILE": str(args.seed.resolve()),
        "SYMCC_TELEMETRY_OUT": str(telemetry_path),
        "SYMCC_ENABLE_LINEARIZATION": "1",
    })
    command = [str(target_binary), *SPECS[args.program].args, str(args.seed.resolve())]
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            command, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=args.timeout, check=False)
        returncode = completed.returncode
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = None
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
    elapsed = time.monotonic() - started
    target_stdout.write_bytes(stdout)
    target_stderr.write_bytes(stderr)

    telemetry: dict[str, object] = {}
    if telemetry_path.is_file():
        try:
            telemetry = json.loads(telemetry_path.read_text())
        except json.JSONDecodeError:
            telemetry = {"parse_error": True}
    outputs = [
        entry for entry in corpus.iterdir()
        if entry.is_file() and not entry.name.endswith(".hints")
    ]
    result = {
        "schema": "symcc-lava-dse-case-v1",
        "program": args.program,
        "profile": args.profile,
        "seed": str(args.seed.resolve()),
        "seed_sha256": sha256(args.seed),
        "target_binary": str(target_binary),
        "target_binary_sha256": sha256(target_binary),
        "command": command,
        "profile_environment": profile_environment(args.profile, run_dir),
        "elapsed_seconds": elapsed,
        "timed_out": timed_out,
        "returncode": returncode,
        "generated": len(outputs),
        "unique_generated": len({sha256(entry) for entry in outputs}),
        "strategy_outputs": count_strategy_labels(corpus),
        "telemetry": telemetry,
        "lava_bug_replay": count_lava_hits(
            args.program, corpus, args.replay_timeout),
    }
    (run_dir / "lava_case_result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "program": args.program,
        "profile": args.profile,
        "generated": result["generated"],
        "unique_generated": result["unique_generated"],
        "listed_hits": result["lava_bug_replay"]["listed_hit_count"],
        "listed_total": result["lava_bug_replay"]["listed_total"],
        "elapsed_seconds": round(elapsed, 6),
        "timed_out": timed_out,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
