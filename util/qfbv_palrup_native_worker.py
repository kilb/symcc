#!/usr/bin/env python3
"""Fault-isolated ctypes entry point for the native PalRUP producer ABI."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
from typing import Sequence


WORKER_SCHEMA = "symcc-qfbv-native-palrup-worker-result-v1"
PRODUCER_PROTOCOL = "symcc-qfbv-native-palrup-producer-v1"
PRODUCER_SOURCE_COMMIT = "be7a0f84190b3216c589696b2010e8cbf8a8252e"
RESULT_FIELDS = 8


def _emit(payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    os.write(1, encoded.encode("ascii") + b"\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--library", required=True)
    parser.add_argument("--formula", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rank", required=True, type=int)
    parser.add_argument("--solver-count", required=True, type=int)
    parser.add_argument("--original-clause-count", required=True, type=int)
    parser.add_argument("--skipped-epochs", required=True, type=int)
    parser.add_argument("--timeout-ms", required=True, type=int)
    return parser


def _identity(function: object) -> str:
    function.restype = ctypes.c_char_p  # type: ignore[attr-defined]
    value = function()  # type: ignore[operator]
    if not isinstance(value, bytes):
        raise RuntimeError("native identity function returned no bytes")
    return value.decode("ascii", "strict")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        library_path = Path(arguments.library)
        formula_path = Path(arguments.formula)
        output_path = Path(arguments.output)
        for path in (library_path, formula_path):
            status = path.lstat()
            if path.is_symlink() or not path.is_file() or status.st_size <= 0:
                raise RuntimeError("native worker input is not a regular file")
        if output_path.exists() or output_path.is_symlink():
            raise RuntimeError("native worker output already exists")

        library = ctypes.CDLL(
            str(library_path), mode=getattr(os, "RTLD_LOCAL", 0) | os.RTLD_NOW
        )
        protocol = _identity(library.symcc_qfbv_palrup_producer_protocol)
        source_commit = _identity(
            library.symcc_qfbv_palrup_producer_source_commit
        )
        if protocol != PRODUCER_PROTOCOL or source_commit != PRODUCER_SOURCE_COMMIT:
            raise RuntimeError("native producer identity differs from worker policy")

        produce = library.symcc_qfbv_palrup_produce
        produce.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_size_t,
        ]
        produce.restype = ctypes.c_int
        fields = (ctypes.c_uint64 * RESULT_FIELDS)()
        status = int(
            produce(
                os.fsencode(formula_path),
                os.fsencode(output_path),
                arguments.rank,
                arguments.solver_count,
                arguments.original_clause_count,
                arguments.skipped_epochs,
                arguments.timeout_ms,
                fields,
                RESULT_FIELDS,
            )
        )
        payload: dict[str, object] = {
            "schema": WORKER_SCHEMA,
            "protocol": protocol,
            "source_commit": source_commit,
            "rank": arguments.rank,
            "solver_count": arguments.solver_count,
            "skipped_epochs": arguments.skipped_epochs,
            "native_status": status,
            "statistics": {
                "variables": int(fields[0]),
                "original_clauses": int(fields[1]),
                "conflicts": int(fields[2]),
                "decisions": int(fields[3]),
                "propagations": int(fields[4]),
                "restarts": int(fields[5]),
                "imported": int(fields[6]),
                "discarded": int(fields[7]),
            },
        }
        _emit(payload)
        return 0 if status in (0, 10, 20) else 70
    except (OSError, ValueError, RuntimeError, UnicodeError, AttributeError) as error:
        _emit(
            {
                "schema": WORKER_SCHEMA,
                "status": "worker-error",
                "error": type(error).__name__,
            }
        )
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
