#!/usr/bin/env python3
"""Replay continuation MemorySSA/AA proofs in an independent LLVM process."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from seal_ifss_continuation_artifact import verify as verify_seal
from verify_ifss_continuation_manifest import (
    VerificationError,
    verify_path,
)


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def records(path):
    result = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                result.append(json.loads(line))
    return result


def replay(arguments):
    verify_seal(arguments)
    original_count = verify_path(arguments.manifest)
    original_records = records(arguments.manifest)
    require(
        len(original_records) == original_count,
        "original manifest changed during replay",
    )

    with tempfile.TemporaryDirectory(
        prefix="symcc-continuation-replay-"
    ) as directory:
        replay_manifest = Path(directory) / "replay.jsonl"
        environment = os.environ.copy()
        environment["SYMCC_IFSS_CONTINUATION_MEMORY"] = "0"
        environment["SYMCC_IFSS_CONTINUATION_STATE"] = "0"
        environment["SYMCC_IFSS_CONTINUATION_MANIFEST_OUT"] = str(
            replay_manifest
        )
        try:
            process = subprocess.run(
                [
                    str(Path(arguments.llvm_tool).resolve(strict=True)),
                    "-load-pass-plugin="
                    + str(Path(arguments.compiler).resolve(strict=True)),
                    "-passes=ifss-continuation-memory",
                    "-disable-output",
                    str(Path(arguments.lowered_ir).resolve(strict=True)),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise VerificationError(
                f"independent LLVM replay failed: {error}"
            ) from error
        output = process.stdout[-4096:].decode("utf-8", errors="replace")
        require(
            process.returncode == 0,
            f"independent LLVM replay returned {process.returncode}: {output}",
        )
        require(replay_manifest.is_file(), "replay emitted no manifest")
        replay_count = verify_path(replay_manifest)
        replay_records = records(replay_manifest)
        require(
            replay_count == original_count,
            "replay record count mismatch",
        )
        require(
            replay_records == original_records,
            "replayed MemorySSA/AA records differ from the seal",
        )
    print(
        f"replayed {original_count} continuation proof record(s)"
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--seal", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--input-ir", required=True)
    parser.add_argument("--lowered-ir", required=True)
    parser.add_argument("--compiler", required=True)
    parser.add_argument("--llvm-tool", required=True)
    arguments = parser.parse_args(argv)
    try:
        replay(arguments)
    except (OSError, json.JSONDecodeError, VerificationError) as error:
        print(f"continuation replay failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
