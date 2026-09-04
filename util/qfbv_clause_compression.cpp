#include "qfbv_clause_compression.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <vector>

#if defined(__GNUC__)
#define SYMCC_COMPRESSION_EXPORT __attribute__((visibility("default")))
#else
#define SYMCC_COMPRESSION_EXPORT
#endif

namespace {

int error_result (symcc::qfbv::ClauseCodecStatus status) {
  return -static_cast<int> (status);
}

} // namespace

extern "C" {

SYMCC_COMPRESSION_EXPORT const char *
symcc_qfbv_clause_compression_protocol () {
  return symcc::qfbv::CLAUSE_COMPRESSION_PROTOCOL;
}

SYMCC_COMPRESSION_EXPORT std::uint64_t
symcc_qfbv_clause_compression_inline_limit () {
  return symcc::qfbv::CLAUSE_INLINE_BYTES;
}

SYMCC_COMPRESSION_EXPORT int symcc_qfbv_clause_compress (
    const int *literals, int count, std::uint8_t *output,
    std::uint64_t capacity, std::uint64_t *encoded_size,
    int *uses_inline_storage) {
  if (count < 0 || !encoded_size || !uses_inline_storage ||
      capacity > std::numeric_limits<std::size_t>::max ())
    return error_result (symcc::qfbv::ClauseCodecStatus::invalid_argument);
  try {
    symcc::qfbv::CompressedClause clause;
    const symcc::qfbv::ClauseCodecStatus status =
        symcc::qfbv::CompressedClause::encode (
            literals, static_cast<std::size_t> (count), clause);
    if (status != symcc::qfbv::ClauseCodecStatus::ok)
      return error_result (status);
    *encoded_size = clause.encoded_size ();
    *uses_inline_storage = clause.is_inline () ? 1 : 0;
    if (capacity < clause.encoded_size ())
      return 0;
    if (clause.encoded_size () && !output)
      return error_result (symcc::qfbv::ClauseCodecStatus::invalid_argument);
    std::memcpy (output, clause.bytes (), clause.encoded_size ());
    return 1;
  } catch (...) {
    return error_result (symcc::qfbv::ClauseCodecStatus::resource_limit);
  }
}

SYMCC_COMPRESSION_EXPORT int symcc_qfbv_clause_decompress (
    const std::uint8_t *data, std::uint64_t encoded_size, int *output,
    std::uint64_t capacity, std::uint64_t *literal_count) {
  if (!literal_count || encoded_size > std::numeric_limits<std::size_t>::max () ||
      capacity > std::numeric_limits<std::size_t>::max ())
    return error_result (symcc::qfbv::ClauseCodecStatus::invalid_argument);
  try {
    std::vector<int> literals;
    const symcc::qfbv::ClauseCodecStatus status =
        symcc::qfbv::CompressedClause::decode_bytes (
            data, static_cast<std::size_t> (encoded_size), literals);
    if (status != symcc::qfbv::ClauseCodecStatus::ok)
      return error_result (status);
    *literal_count = literals.size ();
    if (capacity < literals.size ())
      return 0;
    if (!literals.empty () && !output)
      return error_result (symcc::qfbv::ClauseCodecStatus::invalid_argument);
    if (!literals.empty ())
      std::copy (literals.begin (), literals.end (), output);
    return 1;
  } catch (...) {
    return error_result (symcc::qfbv::ClauseCodecStatus::resource_limit);
  }
}

} // extern "C"
