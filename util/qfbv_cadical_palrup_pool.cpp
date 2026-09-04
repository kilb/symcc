// Clause-sharing PalRUP pool for the SAT 2026 CaDiCaL fork.

#include "cadical.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <deque>
#include <limits>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <sys/stat.h>
#include <thread>
#include <vector>

#if defined(__GNUC__)
#define SYMCC_EXPORT __attribute__((visibility("default")))
#else
#define SYMCC_EXPORT
#endif

namespace {

constexpr const char *PROTOCOL =
    "symcc-qfbv-native-palrup-clause-sharing-pool-v1";
constexpr const char *SOURCE_COMMIT =
    "be7a0f84190b3216c589696b2010e8cbf8a8252e";
constexpr std::uint32_t MAX_SOLVERS = 256;
constexpr std::uint32_t MAX_EPOCHS = 2000000000U;
constexpr std::uint32_t MAX_SHARED_CLAUSE_LENGTH = 1024;
constexpr std::uint32_t MAX_QUEUE_CLAUSES = 1000000;
constexpr std::uint64_t MAX_TIMEOUT_MS = 24ULL * 60ULL * 60ULL * 1000ULL;
constexpr std::size_t RESULT_FIELDS_PER_RANK = 14;

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

class StartGate final {
public:
  explicit StartGate (std::size_t expected) : expected (expected) {}

  bool arrive (bool okay) {
    std::unique_lock<std::mutex> lock (mutex);
    if (!okay)
      failed = true;
    ++arrived;
    condition.notify_all ();
    condition.wait (lock, [this] {
      return cancelled || failed || arrived == expected;
    });
    return !cancelled && !failed;
  }

  void cancel () {
    std::lock_guard<std::mutex> lock (mutex);
    cancelled = true;
    condition.notify_all ();
  }

private:
  const std::size_t expected;
  std::size_t arrived = 0;
  bool failed = false;
  bool cancelled = false;
  std::mutex mutex;
  std::condition_variable condition;
};

struct SharedClause {
  std::uint64_t id = 0;
  int glue = 0;
  std::vector<int> literals;
};

struct Inbox {
  std::mutex mutex;
  std::deque<SharedClause> clauses;
};

class ClauseBus final {
public:
  ClauseBus (std::uint32_t solvers, std::uint32_t maximum_length,
             std::uint32_t queue_capacity)
      : maximum_length (maximum_length), queue_capacity (queue_capacity),
        exported (solvers), delivered (solvers), dropped (solvers) {
    inboxes.reserve (solvers);
    for (std::uint32_t rank = 0; rank < solvers; ++rank)
      inboxes.emplace_back (new Inbox ());
  }

  void publish (std::uint32_t source, std::uint64_t id, const int *literals,
                int size, int glue) {
    if (!id || !literals || size <= 0 ||
        static_cast<std::uint32_t> (size) > maximum_length || glue <= 0)
      return;
    SharedClause clause;
    clause.id = id;
    clause.glue = std::min (glue, size);
    clause.literals.assign (literals, literals + size);
    // PalRUP-Check's confirmation merger requires communication clauses to
    // use the same sorted representation as Mallob's sharing manager.
    std::sort (clause.literals.begin (), clause.literals.end ());
    for (std::size_t index = 0; index < clause.literals.size (); ++index) {
      const int literal = clause.literals[index];
      if (!literal || literal == std::numeric_limits<int>::min () ||
          (index && clause.literals[index - 1] == literal) ||
          std::binary_search (clause.literals.begin (), clause.literals.end (),
                              static_cast<int> (
                                  -static_cast<std::int64_t> (literal))))
        return;
    }
    exported[source].fetch_add (1, std::memory_order_relaxed);
    for (std::uint32_t target = 0; target < inboxes.size (); ++target) {
      if (target == source)
        continue;
      Inbox &inbox = *inboxes[target];
      std::lock_guard<std::mutex> lock (inbox.mutex);
      if (inbox.clauses.size () >= queue_capacity) {
        dropped[source].fetch_add (1, std::memory_order_relaxed);
        continue;
      }
      inbox.clauses.push_back (clause);
      delivered[source].fetch_add (1, std::memory_order_relaxed);
    }
  }

  bool available (std::uint32_t rank) {
    Inbox &inbox = *inboxes[rank];
    std::lock_guard<std::mutex> lock (inbox.mutex);
    return !inbox.clauses.empty ();
  }

  bool pop (std::uint32_t rank, SharedClause &clause) {
    Inbox &inbox = *inboxes[rank];
    std::lock_guard<std::mutex> lock (inbox.mutex);
    if (inbox.clauses.empty ())
      return false;
    clause = std::move (inbox.clauses.front ());
    inbox.clauses.pop_front ();
    return true;
  }

  std::uint64_t export_count (std::uint32_t rank) const {
    return exported[rank].load (std::memory_order_relaxed);
  }

  std::uint64_t delivery_count (std::uint32_t rank) const {
    return delivered[rank].load (std::memory_order_relaxed);
  }

  std::uint64_t drop_count (std::uint32_t rank) const {
    return dropped[rank].load (std::memory_order_relaxed);
  }

  std::uint64_t pending_count (std::uint32_t rank) {
    Inbox &inbox = *inboxes[rank];
    std::lock_guard<std::mutex> lock (inbox.mutex);
    return inbox.clauses.size ();
  }

private:
  const std::uint32_t maximum_length;
  const std::uint32_t queue_capacity;
  std::vector<std::unique_ptr<Inbox>> inboxes;
  std::vector<std::atomic<std::uint64_t>> exported;
  std::vector<std::atomic<std::uint64_t>> delivered;
  std::vector<std::atomic<std::uint64_t>> dropped;
};

class RankSource final : public CaDiCaL::LearnSource {
public:
  RankSource (ClauseBus &bus, std::uint32_t rank) : bus (bus), rank (rank) {}

  bool hasNextClause () override { return ready || bus.available (rank); }

  const std::vector<int> &getNextClause (
      std::uint64_t &id, int &glue,
      std::vector<std::uint8_t> *&signature) override {
    if (!ready)
      ready = bus.pop (rank, current);
    if (!ready)
      std::abort ();
    id = current.id;
    glue = current.glue;
    signature = nullptr;
    ready = false;
    return current.literals;
  }

private:
  ClauseBus &bus;
  const std::uint32_t rank;
  SharedClause current;
  bool ready = false;
};

} // namespace

extern "C" {

SYMCC_EXPORT const char *symcc_qfbv_palrup_pool_protocol () {
  return PROTOCOL;
}

SYMCC_EXPORT const char *symcc_qfbv_palrup_pool_source_commit () {
  return SOURCE_COMMIT;
}

SYMCC_EXPORT std::size_t symcc_qfbv_palrup_pool_result_fields_per_rank () {
  return RESULT_FIELDS_PER_RANK;
}

// Return the common CaDiCaL status or a negative pool error.
SYMCC_EXPORT int symcc_qfbv_palrup_produce_pool (
    const char *formula_path, const char *const *output_paths,
    std::uint32_t solver_count, std::uint32_t original_clause_count,
    std::uint32_t skipped_epochs, std::uint64_t timeout_ms,
    std::uint32_t maximum_shared_clause_length,
    std::uint32_t queue_capacity_clauses, std::uint64_t *result_fields,
    std::size_t result_field_count) {
  if (!formula_path || !output_paths || !result_fields || !solver_count ||
      solver_count > MAX_SOLVERS || !original_clause_count ||
      original_clause_count >
          static_cast<std::uint32_t> (std::numeric_limits<int>::max ()) ||
      skipped_epochs > MAX_EPOCHS || !timeout_ms ||
      timeout_ms > MAX_TIMEOUT_MS || !maximum_shared_clause_length ||
      maximum_shared_clause_length > MAX_SHARED_CLAUSE_LENGTH ||
      !queue_capacity_clauses ||
      queue_capacity_clauses > MAX_QUEUE_CLAUSES ||
      result_field_count != solver_count * RESULT_FIELDS_PER_RANK ||
      !regular_nofollow (formula_path))
    return -1;
  for (std::uint32_t rank = 0; rank < solver_count; ++rank) {
    if (!output_is_absent (output_paths[rank]))
      return -1;
    for (std::uint32_t previous = 0; previous < rank; ++previous)
      if (!std::string (output_paths[rank]).compare (output_paths[previous]))
        return -1;
  }

  std::fill (result_fields, result_fields + result_field_count, 0U);
  try {
    ClauseBus bus (solver_count, maximum_shared_clause_length,
                   queue_capacity_clauses);
    StartGate gate (solver_count);
    std::vector<int> statuses (solver_count, -9);
    std::vector<std::thread> threads;
    threads.reserve (solver_count);
    try {
      for (std::uint32_t rank = 0; rank < solver_count; ++rank) {
        threads.emplace_back ([&, rank] {
          bool arrived = false;
          try {
            const auto started = std::chrono::steady_clock::now ();
            CaDiCaL::Solver solver;
            bool configured =
                solver.set ("quiet", 1) && solver.set ("lrat", 1) &&
                solver.set ("lratpalrup", 1) && solver.set ("binary", 1) &&
                solver.set ("lratdeletelines", 1) &&
                solver.set ("lratsolverid", static_cast<int> (rank)) &&
                solver.set ("lratsolvercount",
                            static_cast<int> (solver_count)) &&
                solver.set ("lratorigclscount",
                            static_cast<int> (original_clause_count)) &&
                solver.set ("lratskippedepochs",
                            static_cast<int> (skipped_epochs)) &&
                solver.set ("seed", static_cast<int> (rank)) &&
                solver.set ("phase", static_cast<int> (rank & 1U));
            if (configured)
              configured = solver.trace_proof (output_paths[rank]);
            if (configured) {
              solver.trace_proof_internally (
                  [&, rank] (unsigned long id, const int *literals, int size,
                             const unsigned long *, int, int glue) {
                    bus.publish (rank, id, literals, size, glue);
                  });
            }
            RankSource source (bus, rank);
            if (configured)
              solver.connect_learn_source (&source);
            int variables = 0;
            if (configured)
              configured = !solver.read_dimacs (formula_path, variables, 1);
            arrived = true;
            if (!gate.arrive (configured)) {
              statuses[rank] = -2;
              if (configured) {
                solver.disconnect_learn_source ();
                solver.close_proof_trace (false);
              }
              return;
            }
            DeadlineTerminator terminator (timeout_ms);
            solver.connect_terminator (&terminator);
            statuses[rank] = solver.solve ();
            solver.disconnect_terminator ();
            solver.disconnect_learn_source ();
            solver.flush_proof_trace (false);
            solver.close_proof_trace (false);
            const CaDiCaL::Solver::Statistics statistics = solver.get_stats ();
            std::uint64_t *fields =
                result_fields + rank * RESULT_FIELDS_PER_RANK;
            fields[0] = static_cast<std::uint64_t> (statuses[rank]);
            fields[1] = static_cast<std::uint64_t> (variables);
            fields[2] = original_clause_count;
            fields[3] = nonnegative (statistics.conflicts);
            fields[4] = nonnegative (statistics.decisions);
            fields[5] = nonnegative (statistics.propagations);
            fields[6] = nonnegative (statistics.restarts);
            fields[7] = statistics.imported;
            fields[8] = statistics.discarded;
            fields[9] = bus.export_count (rank);
            fields[10] = bus.delivery_count (rank);
            fields[11] = bus.drop_count (rank);
            fields[12] = bus.pending_count (rank);
            fields[13] = static_cast<std::uint64_t> (
                std::chrono::duration_cast<std::chrono::microseconds> (
                    std::chrono::steady_clock::now () - started)
                    .count ());
          } catch (...) {
            statuses[rank] = -3;
            if (!arrived)
              gate.arrive (false);
          }
        });
      }
    } catch (...) {
      gate.cancel ();
      for (std::thread &thread : threads)
        if (thread.joinable ())
          thread.join ();
      return -4;
    }
    for (std::thread &thread : threads)
      thread.join ();
    // A rank can finish while peers are still publishing into its inbox. Take
    // one pool-wide terminal snapshot after all publishers have stopped.
    for (std::uint32_t rank = 0; rank < solver_count; ++rank)
      result_fields[rank * RESULT_FIELDS_PER_RANK + 12] =
          bus.pending_count (rank);
    if (std::any_of (statuses.begin (), statuses.end (),
                     [] (int status) { return status < 0; }))
      return -5;
    const int common = statuses.front ();
    if (std::any_of (statuses.begin (), statuses.end (),
                     [common] (int status) { return status != common; }))
      return -6;
    return common;
  } catch (const std::bad_alloc &) {
    return -7;
  } catch (...) {
    return -8;
  }
}

} // extern "C"
