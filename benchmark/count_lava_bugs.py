#!/usr/bin/env python3
"""Replay a corpus against LAVA-M and count distinct listed bug IDs.

LAVA-M emits ``Successfully triggered bug <id>`` immediately before an
injected crash.  This utility intentionally replays with the coverage build,
not the symbolic-execution build: generated candidates may crash and the
coverage build keeps the measurement free of runtime solver diagnostics.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
TRIGGER_RE = re.compile(rb"Successfully triggered bug (\d+)")
SKIP_NAMES = {"README.txt", ".cur_input"}
SKIP_DIRS = {".git", ".state", "hangs", "metadata"}


@dataclass(frozen=True)
class ProgramSpec:
    binary: str
    args: tuple[str, ...]
    listed_bugs: str


LAVA_ROOT = "benchmark/public/lava_corpus/LAVA-M"
SPECS = {
    "base64": ProgramSpec(
        "benchmark/public/bin/lava-m-cov/base64", ("-d",),
        f"{LAVA_ROOT}/base64/validated_bugs"),
    "md5sum": ProgramSpec(
        "benchmark/public/bin/lava-m-cov/md5sum", ("-c",),
        f"{LAVA_ROOT}/md5sum/validated_bugs"),
    "uniq": ProgramSpec(
        "benchmark/public/bin/lava-m-cov/uniq", (),
        f"{LAVA_ROOT}/uniq/validated_bugs"),
    # The normal who coverage binary has the diagnostic print disabled.  The
    # countable variant is produced separately when available.
    "who": ProgramSpec(
        "benchmark/public/bin/lava-m-cov/who_countable", (),
        f"{LAVA_ROOT}/who/validated_bugs"),
}


def repo_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def load_listed(path: Path) -> set[int]:
    return {int(token) for token in path.read_text().split() if token.isdigit()}


def iter_inputs(corpus: Path, recursive: bool) -> Iterable[Path]:
    entries = corpus.rglob("*") if recursive else corpus.iterdir()
    for entry in sorted(entries):
        if not entry.is_file():
            continue
        if entry.name in SKIP_NAMES or entry.name.startswith("."):
            continue
        if any(part in SKIP_DIRS for part in entry.relative_to(corpus).parts[:-1]):
            continue
        yield entry


def replay(binary: Path, fixed_args: tuple[str, ...], input_path: Path,
           timeout: float, use_stdin: bool) -> tuple[set[int], bool]:
    command = [str(binary), *fixed_args]
    kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": False,
    }
    if use_stdin:
        command_stdin = input_path.read_bytes()
        kwargs["input"] = command_stdin
    else:
        command.append(str(input_path))
        kwargs["stdin"] = subprocess.DEVNULL

    try:
        completed = subprocess.run(command, **kwargs)
        output = completed.stdout + b"\n" + completed.stderr
        return {int(item) for item in TRIGGER_RE.findall(output)}, False
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"") + b"\n" + (exc.stderr or b"")
        return {int(item) for item in TRIGGER_RE.findall(output)}, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay a corpus and count distinct LAVA-M listed bugs.")
    parser.add_argument("program", choices=sorted(SPECS))
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--binary", help="Override the default coverage binary.")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="Per-input replay timeout in seconds (default: 5).")
    parser.add_argument("--stdin", action="store_true",
                        help="Pass each corpus entry on stdin instead of as a file path.")
    parser.add_argument("--non-recursive", action="store_true",
                        help="Only inspect files directly under CORPUS.")
    parser.add_argument("--json", dest="json_path", type=Path,
                        help="Write the machine-readable result to this path.")
    args = parser.parse_args(argv)

    spec = SPECS[args.program]
    binary = repo_path(args.binary) if args.binary else repo_path(spec.binary)
    if args.program == "who" and not binary.exists() and not args.binary:
        binary = repo_path("benchmark/public/bin/lava-m-cov/who")
        print("warning: who_countable is unavailable; the fallback may not "
              "print IDs, so its count is not valid", file=sys.stderr)
    if not binary.is_file():
        parser.error(f"binary does not exist: {binary}")
    if not args.corpus.is_dir():
        parser.error(f"corpus does not exist: {args.corpus}")

    listed = load_listed(repo_path(spec.listed_bugs))
    triggered: set[int] = set()
    first_input: dict[int, str] = {}
    timeouts = 0
    files = 0
    for input_path in iter_inputs(args.corpus, not args.non_recursive):
        files += 1
        hits, timed_out = replay(binary, spec.args, input_path, args.timeout,
                                 args.stdin)
        timeouts += int(timed_out)
        for bug_id in hits:
            triggered.add(bug_id)
            first_input.setdefault(bug_id, str(input_path))

    listed_hits = triggered & listed
    result = {
        "program": args.program,
        "binary": str(binary),
        "arguments": list(spec.args),
        "corpus": str(args.corpus.resolve()),
        "files_replayed": files,
        "timeouts": timeouts,
        "listed_total": len(listed),
        "distinct_triggered": sorted(triggered),
        "listed_hits": sorted(listed_hits),
        "extra_hits": sorted(triggered - listed),
        "first_input": {str(key): value for key, value in sorted(first_input.items())},
    }
    print(f"{args.program}: {len(listed_hits)}/{len(listed)} listed bugs "
          f"from {files} inputs ({timeouts} replay timeouts)")
    print("listed IDs:", " ".join(map(str, sorted(listed_hits))) or "none")
    if result["extra_hits"]:
        print("non-listed IDs:", " ".join(map(str, result["extra_hits"])))
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
