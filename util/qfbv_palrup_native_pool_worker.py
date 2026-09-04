#!/usr/bin/env python3
"""Fault-isolated ctypes entry point for the clause-sharing PalRUP pool."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import stat
from typing import Sequence


POOL_RESULT_SCHEMA = "symcc-qfbv-native-palrup-pool-result-v1"
POOL_PROTOCOL = "symcc-qfbv-native-palrup-clause-sharing-pool-v1"
PRODUCER_SOURCE_COMMIT = "be7a0f84190b3216c589696b2010e8cbf8a8252e"
RESULT_FIELDS_PER_RANK = 14


class _FailClosedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RuntimeError(f"invalid native pool arguments: {message}")


def _emit(payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    os.write(1, encoded.encode("ascii") + b"\n")


def _identity(function: object) -> str:
    function.restype = ctypes.c_char_p  # type: ignore[attr-defined]
    value = function()  # type: ignore[operator]
    if not isinstance(value, bytes):
        raise RuntimeError("native pool identity function returned no bytes")
    return value.decode("ascii", "strict")


def _parser() -> argparse.ArgumentParser:
    parser = _FailClosedArgumentParser(add_help=False)
    parser.add_argument("--library", required=True)
    parser.add_argument("--formula", required=True)
    parser.add_argument("--output", action="append", required=True)
    parser.add_argument("--solver-count", required=True, type=int)
    parser.add_argument("--original-clause-count", required=True, type=int)
    parser.add_argument("--skipped-epochs", required=True, type=int)
    parser.add_argument("--timeout-ms", required=True, type=int)
    parser.add_argument("--maximum-shared-clause-length", required=True, type=int)
    parser.add_argument("--queue-capacity-clauses", required=True, type=int)
    return parser


def _remove_partial_outputs(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            status = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(status.st_mode) and not path.is_symlink():
            try:
                path.unlink()
            except OSError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    output_paths: list[Path] = []
    try:
        arguments = _parser().parse_args(argv)
        library_path = Path(arguments.library)
        formula_path = Path(arguments.formula)
        output_paths = [Path(value) for value in arguments.output]
        if len(output_paths) != arguments.solver_count:
            raise RuntimeError("native pool output count differs from solver count")
        for path in (library_path, formula_path):
            status = path.lstat()
            if path.is_symlink() or not path.is_file() or status.st_size <= 0:
                raise RuntimeError("native pool input is not a regular file")
        if any(path.exists() or path.is_symlink() for path in output_paths):
            raise RuntimeError("native pool output already exists")

        library = ctypes.CDLL(
            str(library_path), mode=getattr(os, "RTLD_LOCAL", 0) | os.RTLD_NOW
        )
        protocol = _identity(library.symcc_qfbv_palrup_pool_protocol)
        source_commit = _identity(library.symcc_qfbv_palrup_pool_source_commit)
        fields_per_rank = library.symcc_qfbv_palrup_pool_result_fields_per_rank
        fields_per_rank.argtypes = []
        fields_per_rank.restype = ctypes.c_size_t
        if (
            protocol != POOL_PROTOCOL
            or source_commit != PRODUCER_SOURCE_COMMIT
            or int(fields_per_rank()) != RESULT_FIELDS_PER_RANK
        ):
            raise RuntimeError("native pool identity differs from worker policy")

        produce = library.symcc_qfbv_palrup_produce_pool
        produce.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint64,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_size_t,
        ]
        produce.restype = ctypes.c_int
        encoded_outputs = (ctypes.c_char_p * len(output_paths))(
            *(os.fsencode(path) for path in output_paths)
        )
        field_count = arguments.solver_count * RESULT_FIELDS_PER_RANK
        fields = (ctypes.c_uint64 * field_count)()
        status = int(
            produce(
                os.fsencode(formula_path),
                encoded_outputs,
                arguments.solver_count,
                arguments.original_clause_count,
                arguments.skipped_epochs,
                arguments.timeout_ms,
                arguments.maximum_shared_clause_length,
                arguments.queue_capacity_clauses,
                fields,
                field_count,
            )
        )
        workers: list[dict[str, object]] = []
        for rank in range(arguments.solver_count):
            offset = rank * RESULT_FIELDS_PER_RANK
            workers.append(
                {
                    "rank": rank,
                    "native_status": int(fields[offset]),
                    "statistics": {
                        "variables": int(fields[offset + 1]),
                        "original_clauses": int(fields[offset + 2]),
                        "conflicts": int(fields[offset + 3]),
                        "decisions": int(fields[offset + 4]),
                        "propagations": int(fields[offset + 5]),
                        "restarts": int(fields[offset + 6]),
                        "imported": int(fields[offset + 7]),
                        "discarded": int(fields[offset + 8]),
                        "exported": int(fields[offset + 9]),
                        "delivered": int(fields[offset + 10]),
                        "dropped": int(fields[offset + 11]),
                        "pending": int(fields[offset + 12]),
                        "elapsed_us": int(fields[offset + 13]),
                    },
                }
            )
        _emit(
            {
                "schema": POOL_RESULT_SCHEMA,
                "protocol": protocol,
                "source_commit": source_commit,
                "native_status": status,
                "solver_count": arguments.solver_count,
                "skipped_epochs": arguments.skipped_epochs,
                "maximum_shared_clause_length": (
                    arguments.maximum_shared_clause_length
                ),
                "queue_capacity_clauses": arguments.queue_capacity_clauses,
                "workers": workers,
            }
        )
        if status in (0, 10, 20):
            return 0
        _remove_partial_outputs(output_paths)
        return 70
    except (OSError, ValueError, RuntimeError, UnicodeError, AttributeError) as error:
        _remove_partial_outputs(output_paths)
        _emit(
            {
                "schema": POOL_RESULT_SCHEMA,
                "status": "worker-error",
                "error": type(error).__name__,
            }
        )
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
