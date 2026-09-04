#!/usr/bin/env python3
"""Stress process-safe compiler manifest publication."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from verify_hydra_transform_manifest import verify_path  # noqa: E402


def main() -> int:
    if len(sys.argv) != 6:
        raise SystemExit(
            "usage: script OPT PASSLIB INPUT MANIFEST SITE")
    opt, passlib, source, manifest, site = sys.argv[1:]
    process_count = 24
    environment = os.environ.copy()
    environment.update({
        "SYMCC_HYDRA": "1",
        "SYMCC_HYDRA_MODE": "safe",
        "SYMCC_HYDRA_SITE": site,
        "SYMCC_HYDRA_MANIFEST_OUT": manifest,
    })
    with tempfile.TemporaryDirectory(
            prefix="symcc-hydra-concurrent-") as temporary:
        root = Path(temporary)
        processes = [
            subprocess.Popen(
                [
                    opt,
                    f"-load-pass-plugin={passlib}",
                    "-passes=hydra-transform",
                    "-S",
                    source,
                    "-o",
                    str(root / f"lowered-{index}.ll"),
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            for index in range(process_count)
        ]
        failures = []
        for index, process in enumerate(processes):
            output, _ = process.communicate(timeout=60)
            if process.returncode != 0:
                failures.append(
                    f"worker {index}: {output.decode(errors='replace')}")
        if failures:
            raise RuntimeError("\n".join(failures))

    path = Path(manifest)
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if len(lines) != process_count:
        raise RuntimeError(
            f"expected {process_count} records, found {len(lines)}")
    records = [json.loads(line) for line in lines]
    if any(
        record.get("site") != int(site)
        or record.get("selection_source") != "explicit-site"
        for record in records
    ):
        raise RuntimeError("concurrent manifest record identity mismatch")
    if verify_path(path) != process_count:
        raise RuntimeError("independent concurrent manifest verification failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
