#!/usr/bin/env python3
"""Independently rebuild sealed IFSS/Hydra transformation artifacts."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from seal_transform_artifact import (
    EvidenceError,
    add_artifact_arguments,
    manifest_evidence,
    normalize_arguments,
    records,
    sha256_file,
    verify as verify_seal,
)


TRANSFORMATION_ENV_PREFIXES = (
    "SYMCC_HYDRA",
    "SYMCC_IFSS_LOOP",
    "SYMCC_IFSS_CONTINUATION",
)


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def clean_environment():
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith(TRANSFORMATION_ENV_PREFIXES):
            del environment[key]
    return environment


def replay_environment(pipeline, envelope, manifest_paths):
    environment = clean_environment()
    if pipeline == "hydra":
        configuration = envelope["replay_configuration"]
        environment["SYMCC_HYDRA"] = "1"
        if "sites" in configuration:
            environment["SYMCC_HYDRA_SITES"] = ",".join(
                str(site) for site in configuration["sites"]
            )
        else:
            environment["SYMCC_HYDRA_SITE"] = str(
                configuration["site"]
            )
        environment["SYMCC_HYDRA_MODE"] = configuration["mode"]
        environment["SYMCC_HYDRA_MANIFEST_OUT"] = str(
            manifest_paths["hydra"]
        )
    elif pipeline == "loop":
        environment["SYMCC_IFSS_LOOP_SUMMARY"] = "1"
        if "loop-recurrence" in manifest_paths:
            environment["SYMCC_IFSS_LOOP_MANIFEST_OUT"] = str(
                manifest_paths["loop-recurrence"]
            )
        if "loop-exit" in manifest_paths:
            environment["SYMCC_IFSS_LOOP_EXIT_MANIFEST_OUT"] = str(
                manifest_paths["loop-exit"]
            )
    else:
        configuration = envelope["replay_configuration"]
        environment["SYMCC_IFSS_CONTINUATION_STATE"] = "1"
        environment["SYMCC_IFSS_CONTINUATION_MEMORY"] = (
            "1" if configuration["continuation_memory"] else "0"
        )
        environment["SYMCC_IFSS_CONTINUATION_MANIFEST_OUT"] = str(
            manifest_paths["continuation"]
        )
    return environment


def run_checked(command, environment=None, timeout=120):
    try:
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError(f"independent LLVM replay failed: {error}") from error
    output = process.stdout[-8192:].decode("utf-8", errors="replace")
    require(
        process.returncode == 0,
        f"independent LLVM replay returned {process.returncode}: {output}",
    )


def replay(arguments):
    manifests = normalize_arguments(arguments)
    envelope = verify_seal(arguments)
    original_records = {
        kind: records(path) for kind, path in manifests.items()
    }
    with tempfile.TemporaryDirectory(
        prefix="symcc-transform-replay-"
    ) as directory:
        root = Path(directory)
        replay_manifests = {
            kind: root / f"{kind}.jsonl" for kind in manifests
        }
        replay_ir = root / "lowered.ll"
        environment = replay_environment(
            arguments.pipeline, envelope, replay_manifests
        )
        configuration = envelope["replay_configuration"]
        run_checked(
            [
                str(Path(arguments.llvm_tool).resolve(strict=True)),
                "-load-pass-plugin="
                + str(Path(arguments.compiler).resolve(strict=True)),
                "-passes=" + configuration["passes"],
                "-S",
                str(Path(arguments.input_ir).resolve(strict=True)),
                "-o",
                str(replay_ir),
            ],
            environment=environment,
        )
        run_checked(
            [
                str(Path(arguments.llvm_tool).resolve(strict=True)),
                "-passes=verify",
                "-disable-output",
                str(replay_ir),
            ]
        )
        for kind in sorted(manifests):
            require(
                replay_manifests[kind].is_file(),
                f"replay emitted no {kind} manifest",
            )
            manifest_evidence(kind, replay_manifests[kind])
            require(
                records(replay_manifests[kind]) == original_records[kind],
                f"replayed {kind} records differ from the seal",
            )
        require(
            sha256_file(replay_ir)
            == sha256_file(Path(arguments.lowered_ir).resolve(strict=True)),
            "independently lowered IR differs from the seal",
        )
        verify_seal(arguments)
    count = sum(len(value) for value in original_records.values())
    print(
        f"replayed {count} sealed {arguments.pipeline} record(s)"
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    add_artifact_arguments(parser)
    parser.add_argument("--seal", required=True)
    arguments = parser.parse_args(argv)
    try:
        replay(arguments)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(f"transformation replay failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
