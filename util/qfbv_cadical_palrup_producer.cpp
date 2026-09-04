// Native PalRUP fragment production with the SAT 2026 CaDiCaL fork.

#include "cadical.hpp"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <limits>
#include <new>
#include <string>
#include <sys/stat.h>

#if defined(__GNUC__)
#define SYMCC_EXPORT __attribute__((visibility("default")))
#else
#define SYMCC_EXPORT
#endif

namespace {

constexpr const char *PROTOCOL = "symcc-qfbv-native-palrup-producer-v1";
constexpr const char *SOURCE_COMMIT =
    "be7a0f84190b3216c589696b2010e8cbf8a8252e";
constexpr std::uint32_t MAX_SOLVERS = 16384;
constexpr std::uint32_t MAX_EPOCHS = 2000000000U;
constexpr std::uint64_t MAX_TIMEOUT_MS = 24ULL * 60ULL * 60ULL * 1000ULL;
constexpr std::size_t RESULT_FIELDS = 8;

class DeadlineTerminator final : public CaDiCaL::Terminator {
public:
  explicit DeadlineTerminator (std::uint64_t timeout_ms)
      : deadline (std::chrono::steady_clock::now () +
                  std::chrono::milliseconds (timeout_ms)) {}

  bool terminate () override {
    return std::chrono::steady_clock::now () >= deadline;
  }

private:
  std::chrono::steady_clock::time_point deadline;
};

bool regular_nofollow (const char *path) {
  struct stat status {};
  return path && !lstat (path, &status) && S_ISREG (status.st_mode) &&
         !S_ISLNK (status.st_mode);
}

bool output_is_absent (const char *path) {
  if (!path)
    return false;
  struct stat status {};
  if (lstat (path, &status) == 0)
    return false;
  return errno == ENOENT;
}

std::uint64_t nonnegative (std::int64_t value) {
  return value < 0 ? 0U : static_cast<std::uint64_t> (value);
}

} // namespace

extern "C" {

SYMCC_EXPORT const char *symcc_qfbv_palrup_producer_protocol () {
  return PROTOCOL;
}

SYMCC_EXPORT const char *symcc_qfbv_palrup_producer_source_commit () {
  return SOURCE_COMMIT;
}

// Return CaDiCaL's 10/20/0 result or a negative producer error.
SYMCC_EXPORT int symcc_qfbv_palrup_produce (
    const char *formula_path, const char *output_path, std::uint32_t rank,
    std::uint32_t solver_count, std::uint32_t original_clause_count,
    std::uint32_t skipped_epochs, std::uint64_t timeout_ms,
    std::uint64_t *result_fields, std::size_t result_field_count) {
  if (!formula_path || !output_path || !result_fields ||
      result_field_count != RESULT_FIELDS || !solver_count ||
      solver_count > MAX_SOLVERS || rank >= solver_count ||
      !original_clause_count ||
      original_clause_count >
          static_cast<std::uint32_t> (std::numeric_limits<int>::max ()) ||
      skipped_epochs > MAX_EPOCHS || !timeout_ms ||
      timeout_ms > MAX_TIMEOUT_MS || !regular_nofollow (formula_path) ||
      !output_is_absent (output_path))
    return -1;

  std::fill (result_fields, result_fields + result_field_count, 0U);
  try {
    CaDiCaL::Solver solver;
    if (!solver.set ("quiet", 1) || !solver.set ("lrat", 1) ||
        !solver.set ("lratpalrup", 1) || !solver.set ("binary", 1) ||
        !solver.set ("lratdeletelines", 1) ||
        !solver.set ("lratsolverid", static_cast<int> (rank)) ||
        !solver.set ("lratsolvercount", static_cast<int> (solver_count)) ||
        !solver.set ("lratorigclscount",
                     static_cast<int> (original_clause_count)) ||
        !solver.set ("lratskippedepochs", static_cast<int> (skipped_epochs)))
      return -2;
    if (!solver.trace_proof (output_path))
      return -3;

    int variables = 0;
    const char *parse_error = solver.read_dimacs (formula_path, variables, 1);
    if (parse_error) {
      solver.close_proof_trace (false);
      return -4;
    }

    DeadlineTerminator terminator (timeout_ms);
    solver.connect_terminator (&terminator);
    const int status = solver.solve ();
    solver.disconnect_terminator ();
    solver.flush_proof_trace (false);
    solver.close_proof_trace (false);

    const CaDiCaL::Solver::Statistics statistics = solver.get_stats ();
    result_fields[0] = static_cast<std::uint64_t> (variables);
    result_fields[1] = original_clause_count;
    result_fields[2] = nonnegative (statistics.conflicts);
    result_fields[3] = nonnegative (statistics.decisions);
    result_fields[4] = nonnegative (statistics.propagations);
    result_fields[5] = nonnegative (statistics.restarts);
    result_fields[6] = statistics.imported;
    result_fields[7] = statistics.discarded;
    return status;
  } catch (const std::bad_alloc &) {
    return -5;
  } catch (...) {
    return -6;
  }
}

} // extern "C"
