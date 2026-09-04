#!/usr/bin/env python3
"""Reproducible mechanism oracle for F452 native clause compression."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_clause_compression import (  # noqa: E402
    CLAUSE_COMPRESSION_PROTOCOL,
    NativeClauseCompressor,
)
from qfbv_realtime_stream import NativeRealtimeCadical  # noqa: E402


SCHEMA = "symcc-f452-native-clause-compression-oracle-v1"
UPSTREAM_COMMIT = "b5f37b21385ee802ce015103b23aff62f92b1734"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def verify_oracle_result(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("F452 oracle result must be an object")
    result = dict(raw)
    artifact = result.pop("artifact_sha256", None)
    if not isinstance(artifact, str) or artifact != _digest(result):
        raise ValueError("F452 oracle artifact identity is invalid")
    if (
        result.get("schema") != SCHEMA
        or result.get("status") != "pass"
        or result.get("protocol") != CLAUSE_COMPRESSION_PROTOCOL
        or result.get("upstream_commit") != UPSTREAM_COMMIT
    ):
        raise ValueError("F452 oracle protocol identity is invalid")
    if type(result.get("seed")) is not int or type(result.get("cases")) is not int:
        raise ValueError("F452 oracle counts must be exact integers")
    workloads = result.get("workloads")
    if not isinstance(workloads, list) or not workloads:
        raise ValueError("F452 oracle workloads are missing")
    for workload in workloads:
        if not isinstance(workload, dict):
            raise ValueError("F452 oracle workload is malformed")
        required_ints = (
            "literal_count",
            "clauses",
            "original_literal_payload_bytes",
            "compressed_bytes",
            "inline_clauses",
            "encode_median_ns_per_clause",
            "decode_median_ns_per_clause",
        )
        if any(type(workload.get(key)) is not int for key in required_ints):
            raise ValueError("F452 oracle workload counters are not exact")
        if (
            workload["clauses"] <= 0
            or workload["compressed_bytes"] <= 0
            or not 0 <= workload["inline_clauses"] <= workload["clauses"]
        ):
            raise ValueError("F452 oracle workload accounting is invalid")
    properties = result.get("properties")
    if properties != {
        "canonical_roundtrips": result["cases"],
        "corrupt_encodings_rejected": 7,
        "partial_writes_observed": 0,
    }:
        raise ValueError("F452 property oracle is incomplete")
    real = result.get("real_cadical")
    if real is not None:
        if not isinstance(real, dict) or real.get("compression_failures") != 0:
            raise ValueError("F452 real CaDiCaL evidence is invalid")
        if (
            real.get("imports_enqueued") != 2
            or real.get("imports_delivered") != 2
            or real.get("acks") != [[101, 1, 1], [102, 1, 2]]
            or real.get("queued_import_encoded_bytes") != 0
        ):
            raise ValueError("F452 real CaDiCaL delivery is incomplete")
    if result.get("claim_boundary") != "checker storage/codec mechanism only":
        raise ValueError("F452 claim boundary changed")
    result["artifact_sha256"] = artifact
    return result


def _compile_codec(output: Path) -> None:
    subprocess.run(
        [
            "g++", "-std=c++17", "-fPIC", "-shared",
            "-fvisibility=hidden", "-Wall", "-Wextra", "-Werror", "-O2",
            str(ROOT / "util" / "qfbv_clause_compression.cpp"),
            "-o", str(output),
        ],
        check=True,
    )


def _clauses(
    random_source: random.Random, length: int, count: int
) -> tuple[tuple[int, ...], ...]:
    clauses = []
    for index in range(count):
        if index % 2 == 0:
            base = random_source.randrange(1, 1_000_000)
            variables = range(base, base + length)
        else:
            variables = random_source.sample(range(1, 10_000_000), length)
        clauses.append(tuple(
            variable if random_source.getrandbits(1) else -variable
            for variable in variables
        ))
    return tuple(clauses)


def _measure_workload(
    codec: NativeClauseCompressor,
    clauses: Sequence[tuple[int, ...]],
    repeats: int,
) -> dict[str, int | str]:
    encoded = tuple(codec.encode(clause) for clause in clauses)
    expected = tuple(
        tuple(sorted(
            clause,
            key=lambda literal: 2 * (abs(literal) - 1) + (literal > 0),
        ))
        for clause in clauses
    )
    assert tuple(codec.decode(item.data) for item in encoded) == expected
    encode_samples = []
    decode_samples = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        tuple(codec.encode(clause) for clause in clauses)
        encode_samples.append((time.perf_counter_ns() - started) // len(clauses))
        started = time.perf_counter_ns()
        tuple(codec.decode(item.data) for item in encoded)
        decode_samples.append((time.perf_counter_ns() - started) // len(clauses))
    original = sum(4 * len(clause) for clause in clauses)
    compressed = sum(len(item.data) for item in encoded)
    return {
        "name": f"mixed-dense-sparse-{len(clauses[0])}",
        "literal_count": len(clauses[0]),
        "clauses": len(clauses),
        "original_literal_payload_bytes": original,
        "compressed_bytes": compressed,
        "payload_reduction_permille": (
            0 if not original else (original - compressed) * 1000 // original
        ),
        "inline_clauses": sum(item.inline for item in encoded),
        "encode_median_ns_per_clause": int(statistics.median(encode_samples)),
        "decode_median_ns_per_clause": int(statistics.median(decode_samples)),
    }


def _property_oracle(codec: NativeClauseCompressor, seed: int, cases: int) -> None:
    random_source = random.Random(seed)
    for _ in range(cases):
        length = random_source.randrange(65)
        variables = random_source.sample(range(1, 10_000_000), length)
        clause = tuple(
            variable if random_source.getrandbits(1) else -variable
            for variable in variables
        )
        expected = tuple(sorted(
            clause, key=lambda literal: 2 * (abs(literal) - 1) + (literal > 0)
        ))
        assert codec.decode(codec.encode(clause).data) == expected
    corrupt = (
        b"\x02", b"\x81\x00", b"\x03\x80\x00", b"\x01\x00",
        b"\x03\x01\x00", b"\x03\x00\x01", b"\x06\xff\xff\xff\xff\x0f",
    )
    for encoded in corrupt:
        try:
            codec.decode(encoded)
        except ValueError:
            continue
        raise AssertionError("corrupt encoding was accepted")


def _partial_write_oracle(codec: NativeClauseCompressor) -> int:
    literals = (ctypes.c_int * 7)(*range(1, 8))
    encoded_size = ctypes.c_uint64()
    inline = ctypes.c_int()
    output = (ctypes.c_uint8 * 7)(*[0xA5] * 7)
    result = codec.library.symcc_qfbv_clause_compress(
        literals,
        7,
        output,
        len(output),
        ctypes.byref(encoded_size),
        ctypes.byref(inline),
    )
    if result != 0 or encoded_size.value != 8:
        raise AssertionError("bounded compression query failed")
    return sum(value != 0xA5 for value in output)


def _compile_realtime(source: Path, output: Path) -> None:
    include = source / "src"
    library = source / "build"
    subprocess.run(
        [
            "g++", "-std=c++17", "-fPIC", "-shared",
            "-fvisibility=hidden", "-Wall", "-Wextra", "-Werror", "-pthread",
            f"-I{include}",
            str(ROOT / "util" / "qfbv_cadical_realtime.cpp"),
            str(ROOT / "util" / "qfbv_clause_compression.cpp"),
            f"-L{library}", "-lcadical", f"-Wl,-rpath,{library}",
            "-o", str(output),
        ],
        check=True,
    )


def _real_cadical(source: Path, library: Path) -> dict[str, Any]:
    _compile_realtime(source, library)
    native = NativeRealtimeCadical(library, require_clause_compression=True)
    context = native.new_context(
        max_learned_length=0,
        max_imports=8,
        max_import_literals=256,
        max_learned=0,
    )
    try:
        context.add(1)
        context.add(0)
        context.observe(128)
        assert context.enqueue(101, (-1, 2, 3, 4, 5, 6))
        assert context.enqueue(102, tuple(range(7, 39)))
        solve_result = context.solve()
        acknowledgements = []
        while True:
            ack = context.dequeue_ack()
            if ack is None:
                break
            acknowledgements.append(list(ack))
        stats = context.stats()
        return {
            "signature": native.signature,
            "solve_result": solve_result,
            "acks": acknowledgements,
            **{
                key: stats[key]
                for key in (
                    "imports_enqueued", "imports_delivered",
                    "import_uncompressed_bytes", "import_compressed_bytes",
                    "import_inline_clauses", "import_heap_clauses",
                    "queued_import_encoded_bytes", "compressed_literals_decoded",
                    "compression_failures",
                )
            },
        }
    finally:
        context.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=0xF452)
    parser.add_argument("--cases", type=int, default=20_000)
    parser.add_argument("--clauses", type=int, default=512)
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--cadical-source", type=Path)
    arguments = parser.parse_args()
    if arguments.cases <= 0 or arguments.clauses <= 0 or arguments.repeats <= 0:
        raise SystemExit("oracle counts must be positive")
    with tempfile.TemporaryDirectory(prefix="symcc-f452-oracle-") as directory:
        temporary = Path(directory)
        codec_library = temporary / "libsymcc_qfbv_clause_compression.so"
        _compile_codec(codec_library)
        codec = NativeClauseCompressor(codec_library)
        _property_oracle(codec, arguments.seed, arguments.cases)
        partial_writes = _partial_write_oracle(codec)
        random_source = random.Random(arguments.seed)
        workloads = [
            _measure_workload(
                codec,
                _clauses(random_source, length, arguments.clauses),
                arguments.repeats,
            )
            for length in (1, 3, 6, 7, 8, 16, 32, 64, 256)
        ]
        real = None
        if arguments.cadical_source is not None:
            real = _real_cadical(
                arguments.cadical_source.resolve(strict=True),
                temporary / "libsymcc_qfbv_cadical_realtime.so",
            )
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "pass",
        "protocol": CLAUSE_COMPRESSION_PROTOCOL,
        "upstream_commit": UPSTREAM_COMMIT,
        "seed": arguments.seed,
        "cases": arguments.cases,
        "workloads": workloads,
        "properties": {
            "canonical_roundtrips": arguments.cases,
            "corrupt_encodings_rejected": 7,
            "partial_writes_observed": partial_writes,
        },
        "real_cadical": real,
        "claim_boundary": "checker storage/codec mechanism only",
    }
    result["artifact_sha256"] = _digest(result)
    verify_oracle_result(result)
    rendered = json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="ascii")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
