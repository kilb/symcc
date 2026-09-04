#!/usr/bin/env python3
"""Strict ctypes adapter for native checker-clause compression."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


CLAUSE_COMPRESSION_PROTOCOL = "symcc-qfbv-native-clause-compression-v1"
CLAUSE_COMPRESSION_INLINE_BYTES = 7
MAX_CLAUSE_LITERALS = 65_536
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

_STATUS_NAMES = {
    1: "invalid argument",
    2: "too many literals",
    3: "invalid literal",
    4: "duplicate literal",
    5: "tautological clause",
    6: "truncated encoding",
    7: "noncanonical varint",
    8: "integer overflow",
    9: "encoded-size mismatch",
    10: "resource limit",
}


class ClauseCompressionError(ValueError):
    """The compression ABI or a clause encoding failed closed."""


@dataclass(frozen=True)
class EncodedClause:
    data: bytes
    inline: bool


def _raise_native_error(result: int, operation: str) -> None:
    status = _STATUS_NAMES.get(-result, f"unknown status {-result}")
    raise ClauseCompressionError(f"native clause {operation} failed: {status}")


class NativeClauseCompressor:
    """Bounded, canonical encoder/decoder backed by the production C++ codec."""

    def __init__(self, library_path: str | Path) -> None:
        path = Path(library_path).resolve(strict=True)
        if not path.is_file():
            raise ClauseCompressionError("compression library is not a regular file")
        self.path = path
        self.library = ctypes.CDLL(str(path))
        self._configure_api()
        protocol_raw = self.library.symcc_qfbv_clause_compression_protocol()
        if protocol_raw is None:
            raise ClauseCompressionError("compression library lacks an identity")
        try:
            self.protocol = protocol_raw.decode("ascii", "strict")
        except UnicodeError as error:
            raise ClauseCompressionError(
                "compression protocol identity is not ASCII"
            ) from error
        if self.protocol != CLAUSE_COMPRESSION_PROTOCOL:
            raise ClauseCompressionError("compression protocol is unsupported")
        self.inline_limit = int(
            self.library.symcc_qfbv_clause_compression_inline_limit()
        )
        if self.inline_limit != CLAUSE_COMPRESSION_INLINE_BYTES:
            raise ClauseCompressionError("compression inline-storage contract changed")

    def _configure_api(self) -> None:
        api = self.library
        required = (
            "symcc_qfbv_clause_compression_protocol",
            "symcc_qfbv_clause_compression_inline_limit",
            "symcc_qfbv_clause_compress",
            "symcc_qfbv_clause_decompress",
        )
        if not all(hasattr(api, name) for name in required):
            raise ClauseCompressionError("native compression ABI is incomplete")
        api.symcc_qfbv_clause_compression_protocol.argtypes = []
        api.symcc_qfbv_clause_compression_protocol.restype = ctypes.c_char_p
        api.symcc_qfbv_clause_compression_inline_limit.argtypes = []
        api.symcc_qfbv_clause_compression_inline_limit.restype = ctypes.c_uint64
        api.symcc_qfbv_clause_compress.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_int),
        ]
        api.symcc_qfbv_clause_compress.restype = ctypes.c_int
        api.symcc_qfbv_clause_decompress.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint64),
        ]
        api.symcc_qfbv_clause_decompress.restype = ctypes.c_int

    @staticmethod
    def _normalize_literals(literals: Sequence[int]) -> tuple[int, ...]:
        if isinstance(literals, (str, bytes, bytearray)):
            raise ClauseCompressionError("clause literals must be an integer sequence")
        try:
            normalized = tuple(literals)
        except TypeError as error:
            raise ClauseCompressionError(
                "clause literals must be an integer sequence"
            ) from error
        if len(normalized) > MAX_CLAUSE_LITERALS:
            raise ClauseCompressionError("clause has too many literals")
        for literal in normalized:
            if type(literal) is not int:
                raise ClauseCompressionError("clause literals must be integers")
            if not _INT32_MIN <= literal <= _INT32_MAX:
                raise ClauseCompressionError("clause literal exceeds signed int32")
        return normalized

    def encode(self, literals: Sequence[int]) -> EncodedClause:
        normalized = self._normalize_literals(literals)
        values_type = ctypes.c_int * max(1, len(normalized))
        values = values_type(*(normalized or (0,)))
        encoded_size = ctypes.c_uint64()
        uses_inline = ctypes.c_int()
        result = int(
            self.library.symcc_qfbv_clause_compress(
                values,
                len(normalized),
                None,
                0,
                ctypes.byref(encoded_size),
                ctypes.byref(uses_inline),
            )
        )
        if result < 0:
            _raise_native_error(result, "encoding")
        if result != 0 or not 1 <= encoded_size.value <= 5 * MAX_CLAUSE_LITERALS + 5:
            raise ClauseCompressionError("native clause size query violated its contract")
        output_type = ctypes.c_uint8 * encoded_size.value
        output = output_type()
        second_size = ctypes.c_uint64()
        second_inline = ctypes.c_int()
        result = int(
            self.library.symcc_qfbv_clause_compress(
                values,
                len(normalized),
                output,
                encoded_size.value,
                ctypes.byref(second_size),
                ctypes.byref(second_inline),
            )
        )
        if result < 0:
            _raise_native_error(result, "encoding")
        if (
            result != 1
            or second_size.value != encoded_size.value
            or second_inline.value not in {0, 1}
            or second_inline.value != uses_inline.value
        ):
            raise ClauseCompressionError("native clause encoder violated its contract")
        return EncodedClause(bytes(output), bool(second_inline.value))

    def decode(self, encoded: bytes) -> tuple[int, ...]:
        if type(encoded) is not bytes or not encoded:
            raise ClauseCompressionError("encoded clause must be non-empty bytes")
        data_type = ctypes.c_uint8 * len(encoded)
        data = data_type.from_buffer_copy(encoded)
        literal_count = ctypes.c_uint64()
        result = int(
            self.library.symcc_qfbv_clause_decompress(
                data, len(encoded), None, 0, ctypes.byref(literal_count)
            )
        )
        if result < 0:
            _raise_native_error(result, "decoding")
        if result not in {0, 1} or literal_count.value > MAX_CLAUSE_LITERALS:
            raise ClauseCompressionError("native clause size query violated its contract")
        if literal_count.value == 0:
            if result != 1:
                raise ClauseCompressionError("empty-clause query violated its contract")
            return ()
        if result != 0:
            raise ClauseCompressionError("native clause decoder skipped its size query")
        output_type = ctypes.c_int * literal_count.value
        output = output_type()
        second_count = ctypes.c_uint64()
        result = int(
            self.library.symcc_qfbv_clause_decompress(
                data,
                len(encoded),
                output,
                literal_count.value,
                ctypes.byref(second_count),
            )
        )
        if result < 0:
            _raise_native_error(result, "decoding")
        if result != 1 or second_count.value != literal_count.value:
            raise ClauseCompressionError("native clause decoder violated its contract")
        return tuple(int(output[index]) for index in range(second_count.value))
