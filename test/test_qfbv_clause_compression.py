from __future__ import annotations

# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

import ctypes
import json
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))
sys.path.insert(0, str(ROOT / "benchmark"))

from qfbv_clause_compression import (  # noqa: E402
    CLAUSE_COMPRESSION_PROTOCOL,
    ClauseCompressionError,
    NativeClauseCompressor,
)
from symcc_query_service import _load_portfolio  # noqa: E402
from check_qfbv_clause_compression_oracles import (  # noqa: E402
    verify_oracle_result,
)


@pytest.fixture(scope="module")
def native_codec() -> NativeClauseCompressor:
    with tempfile.TemporaryDirectory(prefix="symcc-f452-") as directory:
        library = Path(directory) / "libsymcc_qfbv_clause_compression.so"
        subprocess.run(
            [
                "g++",
                "-std=c++17",
                "-fPIC",
                "-shared",
                "-fvisibility=hidden",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-O2",
                str(ROOT / "util" / "qfbv_clause_compression.cpp"),
                "-o",
                str(library),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        yield NativeClauseCompressor(library)


def test_known_encoding_is_canonical_and_sorted(
    native_codec: NativeClauseCompressor,
) -> None:
    encoded = native_codec.encode((3, -1, 2))
    assert encoded.data == bytes((4, 0, 3, 2))
    assert encoded.inline
    assert native_codec.decode(encoded.data) == (-1, 2, 3)
    assert native_codec.protocol == CLAUSE_COMPRESSION_PROTOCOL


def test_empty_extremes_and_inline_boundary(
    native_codec: NativeClauseCompressor,
) -> None:
    for clause in ((), (-(2**31) + 1,), ((2**31) - 1,)):
        encoded = native_codec.encode(clause)
        assert native_codec.decode(encoded.data) == tuple(clause)
    inline = native_codec.encode(tuple(range(1, 7)))
    heap = native_codec.encode(tuple(range(1, 8)))
    assert len(inline.data) == 7 and inline.inline
    assert len(heap.data) == 8 and not heap.inline


@pytest.mark.parametrize(
    ("clause", "message"),
    [
        ((0,), "invalid literal"),
        ((-(2**31),), "invalid literal"),
        ((1, 1), "duplicate literal"),
        ((1, -1), "tautological clause"),
        ((True,), "must be integers"),
        (((2**31),), "exceeds signed int32"),
    ],
)
def test_encoder_rejects_invalid_semantics(
    native_codec: NativeClauseCompressor,
    clause: tuple[int, ...],
    message: str,
) -> None:
    with pytest.raises(ClauseCompressionError, match=message):
        native_codec.encode(clause)


@pytest.mark.parametrize(
    ("encoded", "message"),
    [
        (b"", "non-empty bytes"),
        (b"\x02", "encoded-size mismatch"),
        (b"\x81\x00", "noncanonical varint"),
        (b"\x03\x80\x00", "noncanonical varint"),
        (b"\x01\x00", "encoded-size mismatch"),
        (b"\x03\x01\x00", "duplicate literal"),
        (b"\x03\x00\x01", "tautological clause"),
        (b"\x06\xff\xff\xff\xff\x0f", "integer overflow"),
        (b"\x06\x80\x80\x80\x80\x10", "integer overflow"),
    ],
)
def test_decoder_rejects_corrupt_or_noncanonical_data(
    native_codec: NativeClauseCompressor,
    encoded: bytes,
    message: str,
) -> None:
    with pytest.raises(ClauseCompressionError, match=message):
        native_codec.decode(encoded)


def test_capacity_queries_do_not_partially_write(
    native_codec: NativeClauseCompressor,
) -> None:
    api = native_codec.library
    literals = (ctypes.c_int * 7)(*range(1, 8))
    encoded_size = ctypes.c_uint64()
    inline = ctypes.c_int()
    output = (ctypes.c_uint8 * 7)(*[0xA5] * 7)
    result = api.symcc_qfbv_clause_compress(
        literals,
        7,
        output,
        len(output),
        ctypes.byref(encoded_size),
        ctypes.byref(inline),
    )
    assert result == 0 and encoded_size.value == 8
    assert bytes(output) == b"\xa5" * 7

    encoded = native_codec.encode((1, 2, 3)).data
    data = (ctypes.c_uint8 * len(encoded)).from_buffer_copy(encoded)
    decoded = (ctypes.c_int * 2)(91, 92)
    literal_count = ctypes.c_uint64()
    result = api.symcc_qfbv_clause_decompress(
        data,
        len(encoded),
        decoded,
        len(decoded),
        ctypes.byref(literal_count),
    )
    assert result == 0 and literal_count.value == 3
    assert tuple(decoded) == (91, 92)


def test_seeded_property_roundtrip(native_codec: NativeClauseCompressor) -> None:
    random_source = random.Random(0xF452)
    for _ in range(5_000):
        variables = random_source.sample(range(1, 1_000_000), random_source.randrange(65))
        clause = tuple(
            variable if random_source.getrandbits(1) else -variable
            for variable in variables
        )
        encoded = native_codec.encode(clause)
        assert native_codec.decode(encoded.data) == tuple(
            sorted(clause, key=lambda literal: 2 * (abs(literal) - 1) + (literal > 0))
        )
        assert encoded.inline == (len(encoded.data) <= 7)


def test_concurrent_codec_calls_are_isolated(
    native_codec: NativeClauseCompressor,
) -> None:
    def exercise(worker: int) -> int:
        random_source = random.Random(0xF452 + worker)
        total = 0
        for _ in range(500):
            variables = random_source.sample(range(1, 100_000), 32)
            clause = tuple(
                value if random_source.getrandbits(1) else -value
                for value in variables
            )
            encoded = native_codec.encode(clause)
            assert len(native_codec.decode(encoded.data)) == len(clause)
            total += len(encoded.data)
        return total

    with ThreadPoolExecutor(max_workers=8) as executor:
        totals = tuple(executor.map(exercise, range(16)))
    assert all(total > 0 for total in totals)


def test_portfolio_requires_compression_explicitly() -> None:
    solver = {
        "name": "f452",
        "kind": "bitblast-cadical-qfbv",
        "persistent": True,
        "native_library": "/opt/cadical/lib/libcadical.so",
        "command": ["cadical", "{cnf}", "{proof}"],
        "capabilities": {"incremental": True},
        "realtime_stream": {
            "library": "/opt/cadical/lib/libsymcc.so",
            "require_clause_compression": True,
        },
    }
    parsed = _load_portfolio(json.dumps([solver]))[0]
    assert parsed["realtime_stream"]["require_clause_compression"] is True
    solver["realtime_stream"]["require_clause_compression"] = "yes"
    with pytest.raises(RuntimeError, match="clause-compression"):
        _load_portfolio(json.dumps([solver]))


def test_production_build_and_hot_path_are_wired() -> None:
    installer = (ROOT / "benchmark" / "install_cadical_3_0_1.sh").read_text()
    realtime = (ROOT / "util" / "qfbv_cadical_realtime.cpp").read_text()
    assert '"${COMPRESSION_SOURCE}"' in installer
    assert "symcc_qfbv_clause_compress$" in installer
    assert "symcc_qfbv_realtime_compression_protocol" in realtime
    assert "CompressedClause clause" in realtime
    assert "queued_import_encoded_bytes" in realtime


def test_oracle_artifact_is_replayable_and_tamper_evident(tmp_path: Path) -> None:
    output = tmp_path / "oracle.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark" / "check_qfbv_clause_compression_oracles.py"),
            "--cases", "100", "--clauses", "16", "--repeats", "1",
            "--output", str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(output.read_text(encoding="ascii"))
    assert verify_oracle_result(result)["cases"] == 100
    result["workloads"][0]["compressed_bytes"] += 1
    with pytest.raises(ValueError, match="identity"):
        verify_oracle_result(result)
