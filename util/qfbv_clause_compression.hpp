#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <utility>
#include <vector>

namespace symcc::qfbv {

inline constexpr const char *CLAUSE_COMPRESSION_PROTOCOL =
    "symcc-qfbv-native-clause-compression-v1";
inline constexpr std::size_t CLAUSE_INLINE_BYTES = 7;
inline constexpr std::size_t MAX_CLAUSE_LITERALS = 65536;
inline constexpr std::size_t MAX_COMPRESSED_CLAUSE_BYTES =
    MAX_CLAUSE_LITERALS * 5 + 5;

enum class ClauseCodecStatus : int {
  ok = 0,
  invalid_argument = 1,
  too_many_literals = 2,
  invalid_literal = 3,
  duplicate_literal = 4,
  tautological_clause = 5,
  truncated = 6,
  noncanonical_varint = 7,
  integer_overflow = 8,
  encoded_size_mismatch = 9,
  resource_limit = 10,
};

inline std::size_t clause_varint_size (std::uint32_t value) noexcept {
  std::size_t size = 1;
  while (value >= 0x80) {
    value >>= 7;
    ++size;
  }
  return size;
}

inline void append_clause_varint (std::uint32_t value,
                                  std::vector<std::uint8_t> &output) {
  do {
    std::uint8_t byte = static_cast<std::uint8_t> (value & 0x7f);
    value >>= 7;
    if (value)
      byte |= 0x80;
    output.push_back (byte);
  } while (value);
}

inline ClauseCodecStatus read_clause_varint (
    const std::uint8_t *data, std::size_t limit, std::size_t &offset,
    std::uint32_t &value) noexcept {
  if (!data || offset >= limit)
    return ClauseCodecStatus::truncated;
  const std::size_t start = offset;
  std::uint64_t accumulator = 0;
  unsigned shift = 0;
  while (offset < limit && offset - start < 5) {
    const std::uint8_t byte = data[offset++];
    accumulator |= static_cast<std::uint64_t> (byte & 0x7f) << shift;
    if (!(byte & 0x80)) {
      if (accumulator > std::numeric_limits<std::uint32_t>::max ())
        return ClauseCodecStatus::integer_overflow;
      value = static_cast<std::uint32_t> (accumulator);
      if (clause_varint_size (value) != offset - start)
        return ClauseCodecStatus::noncanonical_varint;
      return ClauseCodecStatus::ok;
    }
    shift += 7;
  }
  if (offset == limit)
    return ClauseCodecStatus::truncated;
  return ClauseCodecStatus::integer_overflow;
}

inline ClauseCodecStatus internalize_clause_literal (
    int literal, std::uint32_t &internal) noexcept {
  if (!literal || literal == std::numeric_limits<int>::min ())
    return ClauseCodecStatus::invalid_literal;
  const std::uint32_t magnitude = static_cast<std::uint32_t> (
      literal < 0 ? -static_cast<std::int64_t> (literal) : literal);
  internal = 2u * (magnitude - 1u) + (literal > 0 ? 1u : 0u);
  return ClauseCodecStatus::ok;
}

inline ClauseCodecStatus externalize_clause_literal (
    std::uint32_t internal, int &literal) noexcept {
  const std::uint64_t magnitude =
      static_cast<std::uint64_t> (internal / 2u) + 1u;
  if (magnitude > static_cast<std::uint64_t> (
                      std::numeric_limits<int>::max ()))
    return ClauseCodecStatus::integer_overflow;
  const int value = static_cast<int> (magnitude);
  literal = (internal & 1u) ? value : -value;
  return ClauseCodecStatus::ok;
}

class CompressedClause final {
public:
  struct Cursor {
    std::size_t offset = 0;
    std::uint32_t last = 0;
    std::size_t emitted = 0;
    bool initialized = false;
  };

  CompressedClause () = default;
  CompressedClause (CompressedClause &&) noexcept = default;
  CompressedClause &operator= (CompressedClause &&) noexcept = default;
  CompressedClause (const CompressedClause &) = delete;
  CompressedClause &operator= (const CompressedClause &) = delete;

  static ClauseCodecStatus encode (const int *literals, std::size_t count,
                                   CompressedClause &output) {
    if (count > MAX_CLAUSE_LITERALS)
      return ClauseCodecStatus::too_many_literals;
    if (count && !literals)
      return ClauseCodecStatus::invalid_argument;

    std::vector<std::uint32_t> internal;
    internal.reserve (count);
    for (std::size_t index = 0; index < count; ++index) {
      std::uint32_t value = 0;
      const ClauseCodecStatus status =
          internalize_clause_literal (literals[index], value);
      if (status != ClauseCodecStatus::ok)
        return status;
      internal.push_back (value);
    }
    std::sort (internal.begin (), internal.end ());
    for (std::size_t index = 1; index < internal.size (); ++index) {
      if (internal[index] / 2u != internal[index - 1] / 2u)
        continue;
      return internal[index] == internal[index - 1]
                 ? ClauseCodecStatus::duplicate_literal
                 : ClauseCodecStatus::tautological_clause;
    }

    std::vector<std::uint8_t> payload;
    payload.reserve (count * 2);
    std::uint32_t last = 0;
    for (std::size_t index = 0; index < internal.size (); ++index) {
      const std::uint32_t delta = internal[index] - last;
      append_clause_varint (delta, payload);
      last = internal[index];
    }

    std::size_t header_size = 1;
    while (true) {
      const std::size_t total_size = payload.size () + header_size;
      if (total_size > MAX_COMPRESSED_CLAUSE_BYTES ||
          total_size > std::numeric_limits<std::uint32_t>::max ())
        return ClauseCodecStatus::resource_limit;
      const std::size_t next_header = clause_varint_size (
          static_cast<std::uint32_t> (total_size));
      if (next_header == header_size)
        break;
      header_size = next_header;
    }

    std::vector<std::uint8_t> encoded;
    encoded.reserve (payload.size () + header_size);
    append_clause_varint (
        static_cast<std::uint32_t> (payload.size () + header_size), encoded);
    encoded.insert (encoded.end (), payload.begin (), payload.end ());

    CompressedClause candidate;
    candidate.size_ = static_cast<std::uint32_t> (encoded.size ());
    candidate.literal_count_ = static_cast<std::uint32_t> (count);
    if (encoded.size () <= CLAUSE_INLINE_BYTES) {
      std::copy (encoded.begin (), encoded.end (), candidate.inline_.begin ());
    } else {
      candidate.heap_ = std::make_unique<std::uint8_t[]> (encoded.size ());
      std::memcpy (candidate.heap_.get (), encoded.data (), encoded.size ());
    }
    output = std::move (candidate);
    return ClauseCodecStatus::ok;
  }

  static ClauseCodecStatus decode_bytes (const std::uint8_t *data,
                                         std::size_t size,
                                         std::vector<int> &output) {
    output.clear ();
    if (!data || !size)
      return ClauseCodecStatus::invalid_argument;
    if (size > MAX_COMPRESSED_CLAUSE_BYTES ||
        size > std::numeric_limits<std::uint32_t>::max ())
      return ClauseCodecStatus::resource_limit;

    std::size_t offset = 0;
    std::uint32_t advertised_size = 0;
    ClauseCodecStatus status =
        read_clause_varint (data, size, offset, advertised_size);
    if (status != ClauseCodecStatus::ok)
      return status;
    if (advertised_size != size)
      return ClauseCodecStatus::encoded_size_mismatch;

    std::uint32_t last = 0;
    bool first = true;
    output.reserve (std::min<std::size_t> (size - offset,
                                          MAX_CLAUSE_LITERALS));
    while (offset < size) {
      if (output.size () >= MAX_CLAUSE_LITERALS)
        return ClauseCodecStatus::too_many_literals;
      std::uint32_t delta = 0;
      status = read_clause_varint (data, size, offset, delta);
      if (status != ClauseCodecStatus::ok)
        return status;
      if (!first && !delta)
        return ClauseCodecStatus::duplicate_literal;
      if (delta > std::numeric_limits<std::uint32_t>::max () - last)
        return ClauseCodecStatus::integer_overflow;
      const std::uint32_t internal = last + delta;
      if (!first && internal / 2u == last / 2u)
        return ClauseCodecStatus::tautological_clause;
      int literal = 0;
      status = externalize_clause_literal (internal, literal);
      if (status != ClauseCodecStatus::ok)
        return status;
      output.push_back (literal);
      last = internal;
      first = false;
    }
    return ClauseCodecStatus::ok;
  }

  ClauseCodecStatus start (Cursor &cursor) const noexcept {
    cursor = Cursor{};
    if (!size_)
      return ClauseCodecStatus::invalid_argument;
    std::uint32_t advertised_size = 0;
    ClauseCodecStatus status =
        read_clause_varint (bytes (), size_, cursor.offset, advertised_size);
    if (status != ClauseCodecStatus::ok)
      return status;
    if (advertised_size != size_)
      return ClauseCodecStatus::encoded_size_mismatch;
    cursor.initialized = true;
    return ClauseCodecStatus::ok;
  }

  ClauseCodecStatus next (Cursor &cursor, int &literal,
                          bool &has_literal) const noexcept {
    has_literal = false;
    if (!cursor.initialized)
      return ClauseCodecStatus::invalid_argument;
    if (cursor.offset == size_)
      return cursor.emitted == literal_count_
                 ? ClauseCodecStatus::ok
                 : ClauseCodecStatus::encoded_size_mismatch;
    std::uint32_t delta = 0;
    ClauseCodecStatus status =
        read_clause_varint (bytes (), size_, cursor.offset, delta);
    if (status != ClauseCodecStatus::ok)
      return status;
    if (cursor.emitted && !delta)
      return ClauseCodecStatus::duplicate_literal;
    if (delta > std::numeric_limits<std::uint32_t>::max () - cursor.last)
      return ClauseCodecStatus::integer_overflow;
    const std::uint32_t internal = cursor.last + delta;
    if (cursor.emitted && internal / 2u == cursor.last / 2u)
      return ClauseCodecStatus::tautological_clause;
    status = externalize_clause_literal (internal, literal);
    if (status != ClauseCodecStatus::ok)
      return status;
    cursor.last = internal;
    ++cursor.emitted;
    if (cursor.emitted > literal_count_)
      return ClauseCodecStatus::encoded_size_mismatch;
    has_literal = true;
    return ClauseCodecStatus::ok;
  }

  const std::uint8_t *bytes () const noexcept {
    return size_ <= CLAUSE_INLINE_BYTES ? inline_.data () : heap_.get ();
  }
  std::size_t encoded_size () const noexcept { return size_; }
  std::size_t literal_count () const noexcept { return literal_count_; }
  bool is_inline () const noexcept { return size_ <= CLAUSE_INLINE_BYTES; }

private:
  std::array<std::uint8_t, CLAUSE_INLINE_BYTES> inline_{};
  std::unique_ptr<std::uint8_t[]> heap_;
  std::uint32_t size_ = 0;
  std::uint32_t literal_count_ = 0;
};

} // namespace symcc::qfbv

