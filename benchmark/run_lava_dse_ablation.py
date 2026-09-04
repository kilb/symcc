#!/usr/bin/env python3
"""Run a retained LAVA-M DSE profile ablation over seed corpora."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from count_lava_bugs import SPECS, repo_path


PROFILES = ("strict-z3", "fast-optimistic", "runtime-full")
REPO_ROOT = Path(__file__).resolve().parents[1]


def discover_seeds(program: str, limit: int) -> list[Path]:
    seed_dir = repo_path(f"benchmark/public/seeds/lava-m/{program}")
    seeds = sorted(path for path in seed_dir.iterdir() if path.is_file())
    return seeds[:limit] if limit else seeds


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run LAVA-M DSE ablations and retain per-case evidence.")
    parser.add_argument("--program", choices=sorted(SPECS), default="base64")
    parser.add_argument("--profiles", nargs="+", choices=PROFILES,
                        default=list(PROFILES))
    parser.add_argument("--seed", action="append", type=Path,
                        help="Seed file. Defaults to all public seeds.")
    parser.add_argument("--seed-limit", type=int, default=0,
                        help="Use the first N discovered seeds.")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--replay-timeout", type=float, default=2.0)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    seeds = args.seed if args.seed else discover_seeds(args.program, args.seed_limit)
    if not seeds:
        parser.error("no seeds selected")
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, object] = {
        "schema": "symcc-lava-dse-ablation-manifest-v1",
        "program": args.program,
        "profiles": args.profiles,
        "timeout": args.timeout,
        "replay_timeout": args.replay_timeout,
        "started_at_unix": time.time(),
        "cases": [],
    }
    case_runner = REPO_ROOT / "benchmark" / "run_lava_dse_case.py"
    for seed in seeds:
        seed = seed.resolve()
        for profile in args.profiles:
            run_dir = out_dir / profile / seed.name
            run_dir.mkdir(parents=True, exist_ok=True)
            command = [
                sys.executable,
                str(case_runner),
                "--program", args.program,
                "--seed", str(seed),
                "--profile", profile,
                "--timeout", str(args.timeout),
                "--replay-timeout", str(args.replay_timeout),
                "--run-dir", str(run_dir),
            ]
            completed = subprocess.run(command, check=False)
            manifest["cases"].append({
                "seed": str(seed),
                "profile": profile,
                "run_dir": str(run_dir),
                "returncode": completed.returncode,
            })
            if completed.returncode != 0:
                (out_dir / "manifest.json").write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n")
                return completed.returncode

    manifest["ended_at_unix"] = time.time()
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
