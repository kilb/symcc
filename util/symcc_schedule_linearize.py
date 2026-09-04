#!/usr/bin/env python3
"""Materialize a verified lifecycle linear extension and replay prefix."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from schedule_exploration import (
    schedule_linear_extension_certificate,
    verify_schedule_linear_extension_certificate,
    write_schedule_linear_extension_prefix,
)


def _load_json_row(path: Path, line: int) -> dict[str, Any]:
    rows = [
        json.loads(raw)
        for raw in path.read_text(encoding="utf-8").splitlines()
        if raw.strip()
    ]
    if not rows:
        raise ValueError("artifact file is empty")
    try:
        row = rows[line]
    except IndexError as exc:
        raise ValueError("artifact line is out of range") from exc
    if not isinstance(row, dict):
        raise ValueError("artifact row must be a JSON object")
    return row


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _model_assignments(path: Path) -> tuple[Any, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("model must be a JSON object")
    event_positions = raw.get("event_positions")
    lifecycle_ranks = raw.get("lifecycle_ranks")
    if event_positions is None or lifecycle_ranks is None:
        raise ValueError(
            "model requires event_positions and lifecycle_ranks"
        )
    return event_positions, lifecycle_ranks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a schedule-SMT lifecycle model, construct a "
            "linear extension, and write its logical-thread replay prefix."
        )
    )
    parser.add_argument("artifact", type=Path)
    parser.add_argument(
        "--line",
        type=int,
        default=-1,
        help="zero-based JSONL row; negative values count from the end",
    )
    parser.add_argument(
        "--model",
        type=Path,
        help=(
            "JSON object containing event_positions and lifecycle_ranks; "
            "without it, use the artifact's observed certificate"
        ),
    )
    parser.add_argument(
        "--query-index",
        type=int,
        help=(
            "also verify that model positions satisfy this query's replay "
            "slots and conflict reversal"
        ),
    )
    parser.add_argument("--prefix-out", type=Path, required=True)
    parser.add_argument("--certificate-out", type=Path)
    args = parser.parse_args(argv)

    try:
        artifact = _load_json_row(args.artifact, args.line)
        if args.model is not None:
            event_positions, lifecycle_ranks = _model_assignments(
                args.model
            )
            certificate = schedule_linear_extension_certificate(
                artifact,
                event_positions=event_positions,
                lifecycle_ranks=lifecycle_ranks,
                query_index=args.query_index,
            )
        else:
            if args.query_index is not None:
                raise ValueError("--query-index requires --model")
            certificate = artifact.get("observed_linear_extension")
            if not isinstance(certificate, dict):
                raise ValueError(
                    "artifact has no observed linear-extension certificate"
                )
        if not verify_schedule_linear_extension_certificate(
            artifact, certificate
        ):
            raise ValueError("linear-extension certificate is invalid")
        if certificate.get("runtime_replayable") is not True:
            raise ValueError(
                "query orders memory events that the pthread runtime "
                "cannot replay directly"
            )
        if not write_schedule_linear_extension_prefix(
            str(args.prefix_out),
            artifact,
            certificate,
        ):
            raise OSError("failed to write replay prefix")
        if args.certificate_out is not None:
            _write_json_atomic(args.certificate_out, certificate)
        print(json.dumps({
            "schema": certificate["schema"],
            "certificate_sha256": certificate["certificate_sha256"],
            "source": certificate["source"],
            "event_count": certificate["event_count"],
            "controlled_event_count": certificate[
                "controlled_event_count"
            ],
            "runtime_replayable": certificate["runtime_replayable"],
            "prefix_out": str(args.prefix_out),
        }, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"symcc_schedule_linearize: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
