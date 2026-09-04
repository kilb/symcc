// CaDiCaL 3.0 C ABI for checked clause import during the CDCL loop.

#include "cadical.hpp"
#include "qfbv_clause_compression.hpp"

#include <atomic>
#include <cstdlib>
#include <cstdint>
#include <deque>
#include <limits>
#include <mutex>
#include <new>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#if defined(__GNUC__)
#define SYMCC_EXPORT __attribute__((visibility("default")))
#else
#define SYMCC_EXPORT
#endif

namespace {

constexpr const char *PROTOCOL = "symcc-qfbv-realtime-lidrup-stream-v1";
constexpr const char *ACTIVITY_PROTOCOL =
    "symcc-qfbv-native-clause-activity-v1";

struct ImportItem {
  std::uint64_t token;
  symcc::qfbv::CompressedClause clause;
  symcc::qfbv::CompressedClause::Cursor cursor;
};

struct DeliveryAck {
  std::uint64_t token;
  std::uint64_t solve_generation;
  std::uint64_t delivery_ordinal;
};

struct ClauseActivity {
  std::uint64_t token;
  std::uint64_t solve_generation;
  std::uint64_t activity_ordinal;
  std::uint64_t decision_level;
  int kind;
  int unit_literal;
  std::vector<int> falsifying_assignments;
};

struct TrackedImport {
  std::uint64_t token;
  symcc::qfbv::CompressedClause clause;
  std::size_t unassigned = 0;
  std::size_t satisfied = 0;
  bool activated = false;
};

struct ImportOccurrence {
  std::size_t import_index;
  int literal;
};

class StreamContext final : public CaDiCaL::ExternalPropagator,
                            public CaDiCaL::Learner,
                            public CaDiCaL::Terminator {
public:
  StreamContext (int learned_length, std::size_t import_capacity,
                 std::size_t import_literal_capacity,
                 std::size_t learned_capacity)
      : max_learned_length (learned_length),
        max_imports (import_capacity),
        max_import_literals (import_literal_capacity),
        max_learned (learned_capacity) {
    solver.connect_external_propagator (this);
    solver.connect_learner (this);
    solver.connect_terminator (this);
  }

  ~StreamContext () override {
    solver.disconnect_terminator ();
    solver.disconnect_learner ();
    solver.disconnect_external_propagator ();
  }

  bool terminate () override { return termination_requested.load (); }

  bool learning (int size) override {
    std::lock_guard<std::mutex> guard (mutex);
    learned_current.clear ();
    learned_expected = 0;
    accepting_learned = false;
    if (size < 0 || size > max_learned_length ||
        learned_queue.size () >= max_learned) {
      ++learned_dropped;
      return false;
    }
    learned_current.reserve (static_cast<std::size_t> (size));
    learned_expected = size;
    accepting_learned = true;
    return true;
  }

  void learn (int literal) override {
    std::lock_guard<std::mutex> guard (mutex);
    if (!accepting_learned)
      return;
    if (literal) {
      learned_current.push_back (literal);
      return;
    }
    if (static_cast<int> (learned_current.size ()) == learned_expected &&
        learned_queue.size () < max_learned) {
      learned_queue.push_back (learned_current);
      ++learned_exported;
    } else {
      ++learned_dropped;
    }
    learned_current.clear ();
    learned_expected = 0;
    accepting_learned = false;
  }

  void maybe_record_activity (std::size_t index) {
    TrackedImport &item = tracked_imports[index];
    if (item.activated || item.satisfied || item.unassigned > 1)
      return;
    ClauseActivity event{item.token, solve_generation.load (),
                         ++activity_ordinal,
                         observed_trail.size () - 1,
                         item.unassigned ? 1 : 2, 0, {}};
    std::vector<int> literals;
    if (!decode_clause (item.clause, literals))
      return;
    event.falsifying_assignments.reserve (literals.size ());
    for (const int literal : literals) {
      const auto found = assignments.find (std::abs (literal));
      if (found == assignments.end ()) {
        if (event.unit_literal)
          return;
        event.unit_literal = literal;
      } else if (found->second == (literal > 0)) {
        return;
      } else {
        event.falsifying_assignments.push_back (-literal);
      }
    }
    if ((event.kind == 1 && (!event.unit_literal ||
                            event.falsifying_assignments.size () + 1 !=
                                literals.size ())) ||
        (event.kind == 2 && (event.unit_literal ||
                            event.falsifying_assignments.size () !=
                                literals.size ())))
      return;
    item.activated = true;
    if (event.kind == 1)
      ++imports_activated_unit;
    else
      ++imports_activated_conflict;
    activity_queue.push_back (std::move (event));
  }

  bool decode_clause (const symcc::qfbv::CompressedClause &clause,
                      std::vector<int> &literals) {
    literals.clear ();
    literals.reserve (clause.literal_count ());
    symcc::qfbv::CompressedClause::Cursor cursor;
    if (clause.start (cursor) != symcc::qfbv::ClauseCodecStatus::ok)
      return false;
    while (true) {
      int literal = 0;
      bool has_literal = false;
      const symcc::qfbv::ClauseCodecStatus status =
          clause.next (cursor, literal, has_literal);
      if (status != symcc::qfbv::ClauseCodecStatus::ok)
        return false;
      if (!has_literal)
        return literals.size () == clause.literal_count ();
      literals.push_back (literal);
      ++compressed_literals_decoded;
    }
  }

  bool register_delivered_import (ImportItem &item) {
    std::vector<int> literals;
    if (!decode_clause (item.clause, literals))
      return false;
    TrackedImport tracked{item.token, std::move (item.clause), literals.size (),
                          0, false};
    const std::size_t index = tracked_imports.size ();
    for (const int literal : literals) {
      const auto found = assignments.find (std::abs (literal));
      if (found != assignments.end ()) {
        --tracked.unassigned;
        tracked.satisfied += found->second == (literal > 0);
      }
    }
    tracked_imports.push_back (std::move (tracked));
    for (const int literal : literals)
      import_occurrences[std::abs (literal)].push_back (
          ImportOccurrence{index, literal});
    maybe_record_activity (index);
    return true;
  }

  void notify_assignment (const std::vector<int> &literals) override {
    std::lock_guard<std::mutex> guard (mutex);
    if (observed_trail.empty ())
      observed_trail.emplace_back ();
    for (const int assigned_literal : literals) {
      const int variable = std::abs (assigned_literal);
      const bool value = assigned_literal > 0;
      const auto inserted = assignments.emplace (variable, value);
      if (!inserted.second) {
        if (inserted.first->second != value)
          termination_requested.store (true);
        continue;
      }
      observed_trail.back ().push_back (assigned_literal);
      const auto occurrences = import_occurrences.find (variable);
      if (occurrences == import_occurrences.end ())
        continue;
      for (const ImportOccurrence occurrence : occurrences->second) {
        TrackedImport &item = tracked_imports[occurrence.import_index];
        if (item.activated)
          continue;
        if (item.unassigned)
          --item.unassigned;
        item.satisfied += value == (occurrence.literal > 0);
        maybe_record_activity (occurrence.import_index);
      }
    }
  }

  void notify_new_decision_level () override {
    std::lock_guard<std::mutex> guard (mutex);
    observed_trail.emplace_back ();
  }

  void notify_backtrack (std::size_t new_level) override {
    std::lock_guard<std::mutex> guard (mutex);
    while (observed_trail.size () > new_level + 1) {
      for (auto iterator = observed_trail.back ().rbegin ();
           iterator != observed_trail.back ().rend (); ++iterator) {
        const int assigned_literal = *iterator;
        const int variable = std::abs (assigned_literal);
        const bool value = assigned_literal > 0;
        const auto occurrences = import_occurrences.find (variable);
        if (occurrences != import_occurrences.end ()) {
          for (const ImportOccurrence occurrence : occurrences->second) {
            TrackedImport &item = tracked_imports[occurrence.import_index];
            if (item.activated)
              continue;
            ++item.unassigned;
            item.satisfied -= value == (occurrence.literal > 0);
          }
        }
        assignments.erase (variable);
      }
      observed_trail.pop_back ();
    }
  }
  bool cb_check_found_model (const std::vector<int> &) override { return true; }

  bool cb_has_external_clause (bool &forgettable) override {
    std::lock_guard<std::mutex> guard (mutex);
    forgettable = false;
    if (active_import.token)
      return true;
    if (import_queue.empty ())
      return false;
    active_import = std::move (import_queue.front ());
    import_queue.pop_front ();
    queued_import_literals -= active_import.clause.literal_count ();
    queued_import_encoded_bytes -= active_import.clause.encoded_size ();
    if (active_import.clause.start (active_import.cursor) !=
        symcc::qfbv::ClauseCodecStatus::ok) {
      ++compression_failures;
      termination_requested.store (true);
      pending_tokens.erase (active_import.token);
      active_import = ImportItem{};
      ++imports_rejected;
      return false;
    }
    return true;
  }

  int cb_add_external_clause_lit () override {
    std::lock_guard<std::mutex> guard (mutex);
    if (!active_import.token)
      return 0;
    int literal = 0;
    bool has_literal = false;
    const symcc::qfbv::ClauseCodecStatus status =
        active_import.clause.next (active_import.cursor, literal, has_literal);
    if (status != symcc::qfbv::ClauseCodecStatus::ok) {
      ++compression_failures;
      termination_requested.store (true);
      pending_tokens.erase (active_import.token);
      active_import = ImportItem{};
      ++imports_rejected;
      return 0;
    }
    if (has_literal) {
      ++compressed_literals_decoded;
      return literal;
    }
    if (activity_enabled && !register_delivered_import (active_import)) {
      ++compression_failures;
      termination_requested.store (true);
      pending_tokens.erase (active_import.token);
      active_import = ImportItem{};
      ++imports_rejected;
      return 0;
    }
    acknowledged_tokens.push_back (
        DeliveryAck{active_import.token, solve_generation.load (),
                    ++delivery_ordinal});
    pending_tokens.erase (active_import.token);
    active_import = ImportItem{};
    ++imports_delivered;
    return 0;
  }

  CaDiCaL::Solver solver;
  const int max_learned_length;
  const std::size_t max_imports;
  const std::size_t max_import_literals;
  const std::size_t max_learned;
  std::atomic<bool> termination_requested{false};
  std::atomic<bool> solving{false};
  std::mutex api_mutex;
  std::mutex mutex;
  std::deque<ImportItem> import_queue;
  std::deque<DeliveryAck> acknowledged_tokens;
  std::deque<ClauseActivity> activity_queue;
  std::deque<std::vector<int>> learned_queue;
  std::unordered_set<std::uint64_t> pending_tokens;
  std::vector<TrackedImport> tracked_imports;
  std::unordered_map<int, std::vector<ImportOccurrence>> import_occurrences;
  std::unordered_map<int, bool> assignments;
  std::vector<std::vector<int>> observed_trail{1};
  ImportItem active_import{};
  std::size_t queued_import_literals = 0;
  std::size_t queued_import_encoded_bytes = 0;
  std::vector<int> learned_current;
  int learned_expected = 0;
  bool accepting_learned = false;
  bool activity_enabled = false;
  int observed_max = 0;
  std::uint64_t imports_enqueued = 0;
  std::uint64_t imports_delivered = 0;
  std::uint64_t imports_rejected = 0;
  std::uint64_t learned_exported = 0;
  std::uint64_t learned_dropped = 0;
  std::atomic<std::uint64_t> solve_generation{0};
  std::uint64_t delivery_ordinal = 0;
  std::uint64_t activity_ordinal = 0;
  std::uint64_t imports_activated_unit = 0;
  std::uint64_t imports_activated_conflict = 0;
  std::uint64_t import_uncompressed_bytes = 0;
  std::uint64_t import_compressed_bytes = 0;
  std::uint64_t import_inline_clauses = 0;
  std::uint64_t import_heap_clauses = 0;
  std::uint64_t compressed_literals_decoded = 0;
  std::uint64_t compression_failures = 0;
};

StreamContext *as_context (void *opaque) {
  return static_cast<StreamContext *> (opaque);
}

bool valid_literal (int literal) {
  return literal != 0 && literal != std::numeric_limits<int>::min ();
}

} // namespace

extern "C" {

SYMCC_EXPORT const char *symcc_qfbv_realtime_protocol () { return PROTOCOL; }

SYMCC_EXPORT const char *symcc_qfbv_realtime_activity_protocol () {
  return ACTIVITY_PROTOCOL;
}

SYMCC_EXPORT const char *symcc_qfbv_realtime_compression_protocol () {
  return symcc::qfbv::CLAUSE_COMPRESSION_PROTOCOL;
}

SYMCC_EXPORT const char *symcc_qfbv_realtime_signature () {
  static const std::string signature =
      std::string ("symcc-qfbv-realtime-v1|") + CaDiCaL::Solver::signature ();
  return signature.c_str ();
}

SYMCC_EXPORT void *symcc_qfbv_realtime_init (
    int max_learned_length, std::uint64_t max_imports,
    std::uint64_t max_import_literals, std::uint64_t max_learned) {
  if (max_learned_length < 0 || max_learned_length > 65536 ||
      max_imports > 4096 || max_import_literals == 0 ||
      max_import_literals > (1u << 24) || max_learned > 65536)
    return nullptr;
  try {
    return new StreamContext (
        max_learned_length, static_cast<std::size_t> (max_imports),
        static_cast<std::size_t> (max_import_literals),
        static_cast<std::size_t> (max_learned));
  } catch (...) {
    return nullptr;
  }
}

SYMCC_EXPORT void symcc_qfbv_realtime_release (void *opaque) {
  delete as_context (opaque);
}

SYMCC_EXPORT int symcc_qfbv_realtime_add (void *opaque, int literal) {
  StreamContext *context = as_context (opaque);
  if (!context || literal == std::numeric_limits<int>::min ())
    return -1;
  std::lock_guard<std::mutex> state_guard (context->api_mutex);
  if (context->solving.load ())
    return -1;
  try {
    context->solver.add (literal);
    return 0;
  } catch (...) {
    return -2;
  }
}

SYMCC_EXPORT int symcc_qfbv_realtime_assume (void *opaque, int literal) {
  StreamContext *context = as_context (opaque);
  if (!context || !valid_literal (literal))
    return -1;
  std::lock_guard<std::mutex> state_guard (context->api_mutex);
  if (context->solving.load ())
    return -1;
  try {
    context->solver.assume (literal);
    return 0;
  } catch (...) {
    return -2;
  }
}

SYMCC_EXPORT int symcc_qfbv_realtime_observe (void *opaque, int maximum) {
  StreamContext *context = as_context (opaque);
  if (!context || maximum < 0)
    return -1;
  std::lock_guard<std::mutex> state_guard (context->api_mutex);
  if (context->solving.load ())
    return -1;
  try {
    for (int variable = context->observed_max + 1; variable <= maximum;
         ++variable)
      context->solver.add_observed_var (variable);
    if (maximum > context->observed_max)
      context->observed_max = maximum;
    return 0;
  } catch (...) {
    return -2;
  }
}

SYMCC_EXPORT int symcc_qfbv_realtime_solve (void *opaque) {
  StreamContext *context = as_context (opaque);
  if (!context)
    return -1;
  {
    std::lock_guard<std::mutex> state_guard (context->api_mutex);
    if (context->solving.load ())
      return -1;
    context->solving.store (true);
  }
  ++context->solve_generation;
  {
    std::lock_guard<std::mutex> guard (context->mutex);
    context->delivery_ordinal = 0;
    context->activity_ordinal = 0;
  }
  try {
    const int result = context->solver.solve ();
    {
      std::lock_guard<std::mutex> state_guard (context->api_mutex);
      context->solving.store (false);
    }
    return result;
  } catch (...) {
    {
      std::lock_guard<std::mutex> state_guard (context->api_mutex);
      context->solving.store (false);
    }
    return -2;
  }
}

SYMCC_EXPORT int symcc_qfbv_realtime_clear_termination (void *opaque) {
  StreamContext *context = as_context (opaque);
  if (!context)
    return -1;
  std::lock_guard<std::mutex> state_guard (context->api_mutex);
  if (context->solving.load ())
    return -1;
  context->termination_requested.store (false);
  return 0;
}

SYMCC_EXPORT int symcc_qfbv_realtime_val (void *opaque, int literal) {
  StreamContext *context = as_context (opaque);
  if (!context || !valid_literal (literal))
    return 0;
  std::lock_guard<std::mutex> state_guard (context->api_mutex);
  if (context->solving.load ())
    return 0;
  try {
    return context->solver.val (literal);
  } catch (...) {
    return 0;
  }
}

SYMCC_EXPORT int symcc_qfbv_realtime_failed (void *opaque, int literal) {
  StreamContext *context = as_context (opaque);
  if (!context || !valid_literal (literal))
    return 0;
  std::lock_guard<std::mutex> state_guard (context->api_mutex);
  if (context->solving.load ())
    return 0;
  try {
    return context->solver.failed (literal) ? 1 : 0;
  } catch (...) {
    return 0;
  }
}

SYMCC_EXPORT int symcc_qfbv_realtime_terminate (void *opaque) {
  StreamContext *context = as_context (opaque);
  if (!context)
    return -1;
  try {
    if (!context->termination_requested.exchange (true))
      context->solver.terminate ();
    return 0;
  } catch (...) {
    return -2;
  }
}

SYMCC_EXPORT int symcc_qfbv_realtime_enqueue (
    void *opaque, std::uint64_t token, const int *literals, int size) {
  StreamContext *context = as_context (opaque);
  if (!context || !token || size < 0 || size > 65536 ||
      (size && !literals))
    return -1;
  try {
    std::unordered_set<int> seen;
    for (int index = 0; index < size; ++index) {
      const int literal = literals[index];
      if (!valid_literal (literal) ||
          std::abs (static_cast<long long> (literal)) >
              context->observed_max ||
          seen.count (literal) || seen.count (-literal))
        return -2;
      seen.insert (literal);
    }
    symcc::qfbv::CompressedClause clause;
    const symcc::qfbv::ClauseCodecStatus compression_status =
        symcc::qfbv::CompressedClause::encode (
            literals, static_cast<std::size_t> (size), clause);
    if (compression_status != symcc::qfbv::ClauseCodecStatus::ok)
      return -2;
    std::lock_guard<std::mutex> guard (context->mutex);
    const std::size_t queued = context->import_queue.size () +
                               (context->active_import.token ? 1u : 0u);
    if (queued >= context->max_imports ||
        context->queued_import_literals + clause.literal_count () >
            context->max_import_literals) {
      ++context->imports_rejected;
      return 0;
    }
    if (!context->pending_tokens.insert (token).second)
      return -3;
    context->queued_import_literals += clause.literal_count ();
    context->queued_import_encoded_bytes += clause.encoded_size ();
    context->import_uncompressed_bytes +=
        clause.literal_count () * sizeof (int);
    context->import_compressed_bytes += clause.encoded_size ();
    if (clause.is_inline ())
      ++context->import_inline_clauses;
    else
      ++context->import_heap_clauses;
    context->import_queue.push_back (
        ImportItem{token, std::move (clause), {}});
    ++context->imports_enqueued;
    return 1;
  } catch (...) {
    return -4;
  }
}

SYMCC_EXPORT int symcc_qfbv_realtime_dequeue_ack (
    void *opaque, std::uint64_t *token, std::uint64_t *solve_generation,
    std::uint64_t *delivery_ordinal) {
  StreamContext *context = as_context (opaque);
  if (!context || !token || !solve_generation || !delivery_ordinal)
    return -1;
  std::lock_guard<std::mutex> guard (context->mutex);
  if (context->acknowledged_tokens.empty ())
    return 0;
  const DeliveryAck ack = context->acknowledged_tokens.front ();
  *token = ack.token;
  *solve_generation = ack.solve_generation;
  *delivery_ordinal = ack.delivery_ordinal;
  context->acknowledged_tokens.pop_front ();
  return 1;
}

SYMCC_EXPORT int symcc_qfbv_realtime_dequeue_activity (
    void *opaque, std::uint64_t *token, std::uint64_t *solve_generation,
    std::uint64_t *activity_ordinal, std::uint64_t *decision_level,
    int *kind, int *unit_literal, int *falsifying_assignments,
    int capacity, int *size) {
  StreamContext *context = as_context (opaque);
  if (!context || !token || !solve_generation || !activity_ordinal ||
      !decision_level || !kind || !unit_literal || !size || capacity < 0 ||
      (capacity && !falsifying_assignments))
    return -1;
  std::lock_guard<std::mutex> guard (context->mutex);
  if (context->activity_queue.empty ())
    return 0;
  const ClauseActivity &event = context->activity_queue.front ();
  *size = static_cast<int> (event.falsifying_assignments.size ());
  if (static_cast<std::size_t> (capacity) <
      event.falsifying_assignments.size ())
    return -2;
  *token = event.token;
  *solve_generation = event.solve_generation;
  *activity_ordinal = event.activity_ordinal;
  *decision_level = event.decision_level;
  *kind = event.kind;
  *unit_literal = event.unit_literal;
  for (std::size_t index = 0;
       index < event.falsifying_assignments.size (); ++index)
    falsifying_assignments[index] = event.falsifying_assignments[index];
  context->activity_queue.pop_front ();
  return 1;
}

SYMCC_EXPORT int symcc_qfbv_realtime_enable_activity (void *opaque,
                                                       int enabled) {
  StreamContext *context = as_context (opaque);
  if (!context || (enabled != 0 && enabled != 1))
    return -1;
  std::lock_guard<std::mutex> state_guard (context->api_mutex);
  if (context->solving.load ())
    return -1;
  std::lock_guard<std::mutex> guard (context->mutex);
  context->activity_enabled = enabled != 0;
  context->activity_queue.clear ();
  context->tracked_imports.clear ();
  context->import_occurrences.clear ();
  return 0;
}

SYMCC_EXPORT int symcc_qfbv_realtime_dequeue_learned (
    void *opaque, int *literals, int capacity, int *size) {
  StreamContext *context = as_context (opaque);
  if (!context || !size || capacity < 0 || (capacity && !literals))
    return -1;
  std::lock_guard<std::mutex> guard (context->mutex);
  if (context->learned_queue.empty ())
    return 0;
  const std::vector<int> &clause = context->learned_queue.front ();
  *size = static_cast<int> (clause.size ());
  if (static_cast<std::size_t> (capacity) < clause.size ())
    return -2;
  for (std::size_t index = 0; index < clause.size (); ++index)
    literals[index] = clause[index];
  context->learned_queue.pop_front ();
  return 1;
}

SYMCC_EXPORT std::uint64_t symcc_qfbv_realtime_stat (void *opaque, int key) {
  StreamContext *context = as_context (opaque);
  if (!context)
    return 0;
  std::lock_guard<std::mutex> guard (context->mutex);
  switch (key) {
  case 0: return context->imports_enqueued;
  case 1: return context->imports_delivered;
  case 2: return context->imports_rejected;
  case 3: return context->learned_exported;
  case 4: return context->learned_dropped;
  case 5: return context->import_queue.size ();
  case 6: return context->learned_queue.size ();
  case 7: return context->acknowledged_tokens.size ();
  case 8: return context->solve_generation.load ();
  case 9: return context->solving.load () ? 1 : 0;
  case 10: return context->imports_activated_unit;
  case 11: return context->imports_activated_conflict;
  case 12: return context->activity_queue.size ();
  case 13: return context->tracked_imports.size ();
  case 14: return context->import_uncompressed_bytes;
  case 15: return context->import_compressed_bytes;
  case 16: return context->import_inline_clauses;
  case 17: return context->import_heap_clauses;
  case 18: return context->queued_import_encoded_bytes;
  case 19: return context->compressed_literals_decoded;
  case 20: return context->compression_failures;
  default: return 0;
  }
}

SYMCC_EXPORT int symcc_qfbv_realtime_reset_queues (void *opaque) {
  StreamContext *context = as_context (opaque);
  if (!context)
    return -1;
  std::lock_guard<std::mutex> state_guard (context->api_mutex);
  if (context->solving.load ())
    return -1;
  std::lock_guard<std::mutex> guard (context->mutex);
  context->import_queue.clear ();
  context->acknowledged_tokens.clear ();
  context->activity_queue.clear ();
  context->learned_queue.clear ();
  context->pending_tokens.clear ();
  context->tracked_imports.clear ();
  context->import_occurrences.clear ();
  context->activity_enabled = false;
  context->active_import = ImportItem{};
  context->queued_import_literals = 0;
  context->queued_import_encoded_bytes = 0;
  context->learned_current.clear ();
  context->learned_expected = 0;
  context->accepting_learned = false;
  return 0;
}

} // extern "C"
