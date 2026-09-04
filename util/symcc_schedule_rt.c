/*
 * Lightweight schedule trace and prefix replay runtime.
 *
 * Intended for LD_PRELOAD in SymCC worker executions.  The runtime records
 * pthread synchronization scheduling points and optionally delays threads to
 * match a logical-thread-id prefix produced by the Python bounded-DPOR layer.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <link.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifndef SYMCC_SCHEDULE_MAX_PREFIX
#define SYMCC_SCHEDULE_MAX_PREFIX 4096U
#endif

#ifndef SYMCC_SCHEDULE_MAX_THREADS
#define SYMCC_SCHEDULE_MAX_THREADS 4096U
#endif

#ifndef SYMCC_SCHEDULE_MAX_READY_TRACE
#define SYMCC_SCHEDULE_MAX_READY_TRACE 64U
#endif

typedef int (*pthread_create_fn)(pthread_t *, const pthread_attr_t *,
                                 void *(*)(void *), void *);
typedef int (*pthread_join_fn)(pthread_t, void **);
typedef int (*pthread_detach_fn)(pthread_t);
typedef int (*pthread_cancel_fn)(pthread_t);
typedef int (*pthread_mutex_lock_fn)(pthread_mutex_t *);
typedef int (*pthread_mutex_trylock_fn)(pthread_mutex_t *);
typedef int (*pthread_mutex_unlock_fn)(pthread_mutex_t *);
typedef int (*pthread_rwlock_rdlock_fn)(pthread_rwlock_t *);
typedef int (*pthread_rwlock_wrlock_fn)(pthread_rwlock_t *);
typedef int (*pthread_rwlock_unlock_fn)(pthread_rwlock_t *);
typedef int (*pthread_cond_wait_fn)(pthread_cond_t *, pthread_mutex_t *);
typedef int (*pthread_cond_timedwait_fn)(pthread_cond_t *, pthread_mutex_t *,
                                         const struct timespec *);
typedef int (*pthread_cond_signal_fn)(pthread_cond_t *);
typedef int (*pthread_cond_broadcast_fn)(pthread_cond_t *);

static pthread_create_fn real_pthread_create;
static pthread_join_fn real_pthread_join;
static pthread_detach_fn real_pthread_detach;
static pthread_cancel_fn real_pthread_cancel;
static pthread_mutex_lock_fn real_pthread_mutex_lock;
static pthread_mutex_trylock_fn real_pthread_mutex_trylock;
static pthread_mutex_unlock_fn real_pthread_mutex_unlock;
static pthread_rwlock_rdlock_fn real_pthread_rwlock_rdlock;
static pthread_rwlock_wrlock_fn real_pthread_rwlock_wrlock;
static pthread_rwlock_unlock_fn real_pthread_rwlock_unlock;
static pthread_cond_wait_fn real_pthread_cond_wait;
static pthread_cond_timedwait_fn real_pthread_cond_timedwait;
static pthread_cond_signal_fn real_pthread_cond_signal;
static pthread_cond_broadcast_fn real_pthread_cond_broadcast;

static int schedule_enabled;
static int memory_trace_enabled;
static int memory_provenance_enabled;
static int memory_filter_stack;
static int memory_filter_owner;
static int atomic_commit_enabled;
static int enabled_trace_enabled;
static int trace_all_modules;
static int trace_fd = -1;
static unsigned long prefix[SYMCC_SCHEDULE_MAX_PREFIX];
static unsigned prefix_len;
static size_t memory_trace_max_bytes = 8;
static size_t memory_owner_slots = 65536;
static long wait_timeout_ms = 100;
static long enabled_settle_us = 1000;
static size_t enabled_trace_max_threads = 32;
static _Atomic unsigned prefix_index;
static _Atomic unsigned long event_seq;
static _Atomic unsigned long decision_seq;
static _Atomic unsigned long atomic_group_seq;
static _Atomic unsigned long next_tid = 1;
static atomic_flag log_lock = ATOMIC_FLAG_INIT;
static atomic_flag replay_gate_lock = ATOMIC_FLAG_INIT;
static atomic_flag owner_lock = ATOMIC_FLAG_INIT;
static atomic_flag thread_identity_lock = ATOMIC_FLAG_INIT;
static _Thread_local unsigned long logical_tid = (unsigned long)-1;
static _Thread_local int in_schedule_runtime;
static _Thread_local uintptr_t stack_low;
static _Thread_local uintptr_t stack_high;
static _Thread_local uintptr_t last_schedule_read;
static _Thread_local size_t last_schedule_read_size;
static _Thread_local uintptr_t lowered_atomic_address;
static _Thread_local unsigned lowered_atomic_reads;
static _Thread_local unsigned lowered_atomic_writes;
static _Thread_local uint64_t pending_atomic_group;
static _Thread_local unsigned pending_atomic_prefix_index;
static _Thread_local int pending_atomic_commit;

struct traced_module_range {
  uintptr_t lower;
  uintptr_t upper;
};

static struct traced_module_range traced_modules[128];
static size_t traced_module_count;
static char main_executable[PATH_MAX];
static const char *module_allowlist;

struct memory_owner_entry {
  uintptr_t address;
  unsigned long owner_tid;
  unsigned char occupied;
  unsigned char shared;
  unsigned char last_write;
};

static struct memory_owner_entry *memory_owners;

struct thread_start {
  void *(*start_routine)(void *);
  void *arg;
  unsigned long tid;
};

struct ready_entry {
  unsigned long tid;
  const char *op;
  uintptr_t object;
  uintptr_t auxiliary;
  unsigned char occupied;
};

static struct ready_entry ready_entries[SYMCC_SCHEDULE_MAX_THREADS];
static size_t ready_count;
static int ready_incomplete;

struct thread_identity {
  pthread_t handle;
  unsigned long tid;
  unsigned char occupied;
  unsigned char handle_valid;
  unsigned char detached;
  unsigned char exited;
  unsigned char cancel_requested;
};

static struct thread_identity
    thread_identities[SYMCC_SCHEDULE_MAX_THREADS];

static int env_disabled(const char *name) {
  const char *value = getenv(name);
  if (!value || !*value)
    return 0;
  return !strcmp(value, "0") || !strcasecmp(value, "false") ||
         !strcasecmp(value, "off") || !strcasecmp(value, "no");
}

static int env_enabled(const char *name) {
  const char *value = getenv(name);
  if (!value || !*value)
    return 0;
  return !env_disabled(name);
}

static int module_name_allowed(const char *path) {
  if (!path)
    return 0;
  if (*path == '\0')
    return 1;

  char resolved[PATH_MAX];
  const char *normalized = path;
  if (realpath(path, resolved))
    normalized = resolved;
  if (main_executable[0] && !strcmp(normalized, main_executable))
    return 1;

  const char *base = strrchr(normalized, '/');
  base = base ? base + 1 : normalized;
  const char *cursor = module_allowlist;
  while (cursor && *cursor) {
    const char *end = strchr(cursor, ':');
    size_t length = end ? (size_t)(end - cursor) : strlen(cursor);
    if (length && ((strlen(normalized) == length &&
                    !strncmp(cursor, normalized, length)) ||
                   (strlen(base) == length &&
                    !strncmp(cursor, base, length))))
      return 1;
    cursor = end ? end + 1 : NULL;
  }
  return 0;
}

static int collect_traced_module(struct dl_phdr_info *info, size_t size,
                                 void *opaque) {
  (void)size;
  (void)opaque;
  const char *name = info->dlpi_name;
  if (!module_name_allowed(name))
    return 0;
  uintptr_t lower = UINTPTR_MAX;
  uintptr_t upper = 0;
  for (ElfW(Half) index = 0; index < info->dlpi_phnum; index++) {
    const ElfW(Phdr) *header = &info->dlpi_phdr[index];
    if (header->p_type != PT_LOAD || header->p_memsz == 0)
      continue;
    uintptr_t segment_lower =
        (uintptr_t)info->dlpi_addr + (uintptr_t)header->p_vaddr;
    uintptr_t segment_upper = segment_lower + (uintptr_t)header->p_memsz;
    if (segment_upper < segment_lower)
      continue;
    if (segment_lower < lower)
      lower = segment_lower;
    if (segment_upper > upper)
      upper = segment_upper;
  }
  if (lower < upper && traced_module_count <
                           sizeof(traced_modules) / sizeof(traced_modules[0])) {
    traced_modules[traced_module_count].lower = lower;
    traced_modules[traced_module_count].upper = upper;
    traced_module_count++;
  }
  return 0;
}

static void initialize_module_filter(void) {
  ssize_t length = readlink(
      "/proc/self/exe", main_executable, sizeof(main_executable) - 1);
  if (length > 0)
    main_executable[length] = '\0';
  else
    main_executable[0] = '\0';
  module_allowlist = getenv("SYMCC_SCHEDULE_MODULES");
  trace_all_modules = env_enabled("SYMCC_SCHEDULE_ALL_MODULES");
  traced_module_count = 0;
  if (!trace_all_modules)
    (void)dl_iterate_phdr(collect_traced_module, NULL);
}

static int caller_is_traced(const void *return_address) {
  if (in_schedule_runtime || !schedule_enabled)
    return 0;
  if (trace_all_modules)
    return 1;
  uintptr_t address = (uintptr_t)return_address;
  for (size_t index = 0; index < traced_module_count; index++) {
    if (address >= traced_modules[index].lower &&
        address < traced_modules[index].upper)
      return 1;
  }
  return 0;
}

static unsigned long current_tid(void) {
  if (logical_tid == (unsigned long)-1)
    logical_tid = 0;
  return logical_tid;
}

static long monotonic_ms(void) {
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
    return 0;
  return (long)(now.tv_sec * 1000L + now.tv_nsec / 1000000L);
}

static void resolve_symbols(void) {
  if (real_pthread_mutex_lock)
    return;
  in_schedule_runtime++;
  real_pthread_create =
      (pthread_create_fn)dlsym(RTLD_NEXT, "pthread_create");
  real_pthread_join = (pthread_join_fn)dlsym(RTLD_NEXT, "pthread_join");
  real_pthread_detach = (pthread_detach_fn)dlsym(RTLD_NEXT, "pthread_detach");
  real_pthread_cancel = (pthread_cancel_fn)dlsym(RTLD_NEXT, "pthread_cancel");
  real_pthread_mutex_lock =
      (pthread_mutex_lock_fn)dlsym(RTLD_NEXT, "pthread_mutex_lock");
  real_pthread_mutex_trylock =
      (pthread_mutex_trylock_fn)dlsym(RTLD_NEXT, "pthread_mutex_trylock");
  real_pthread_mutex_unlock =
      (pthread_mutex_unlock_fn)dlsym(RTLD_NEXT, "pthread_mutex_unlock");
  real_pthread_rwlock_rdlock =
      (pthread_rwlock_rdlock_fn)dlsym(RTLD_NEXT, "pthread_rwlock_rdlock");
  real_pthread_rwlock_wrlock =
      (pthread_rwlock_wrlock_fn)dlsym(RTLD_NEXT, "pthread_rwlock_wrlock");
  real_pthread_rwlock_unlock =
      (pthread_rwlock_unlock_fn)dlsym(RTLD_NEXT, "pthread_rwlock_unlock");
  real_pthread_cond_wait =
      (pthread_cond_wait_fn)dlsym(RTLD_NEXT, "pthread_cond_wait");
  real_pthread_cond_timedwait =
      (pthread_cond_timedwait_fn)dlsym(RTLD_NEXT, "pthread_cond_timedwait");
  real_pthread_cond_signal =
      (pthread_cond_signal_fn)dlsym(RTLD_NEXT, "pthread_cond_signal");
  real_pthread_cond_broadcast =
      (pthread_cond_broadcast_fn)dlsym(RTLD_NEXT, "pthread_cond_broadcast");
  in_schedule_runtime--;
}

static void log_event_for_tid_tag(const char *op, const void *object,
                                  unsigned long tid, const char *tag) {
  if (!schedule_enabled || trace_fd < 0 || in_schedule_runtime)
    return;

  char buffer[4096];
  unsigned long seq = atomic_fetch_add_explicit(
      &event_seq, 1, memory_order_relaxed);
  int n = 0;
  if (tag && *tag) {
    n = snprintf(buffer, sizeof(buffer), "%lu %lu %s 0x%lx %s\n", seq, tid, op,
                 (unsigned long)(uintptr_t)object, tag);
  } else {
    n = snprintf(buffer, sizeof(buffer), "%lu %lu %s 0x%lx\n", seq, tid, op,
                 (unsigned long)(uintptr_t)object);
  }
  if (n <= 0)
    return;
  if (n > (int)sizeof(buffer))
    n = (int)sizeof(buffer);

  while (atomic_flag_test_and_set_explicit(&log_lock, memory_order_acquire)) {
  }
  (void)syscall(SYS_write, trace_fd, buffer, (size_t)n);
  atomic_flag_clear_explicit(&log_lock, memory_order_release);
}

static void log_event_for_tid(const char *op, const void *object,
                              unsigned long tid) {
  log_event_for_tid_tag(op, object, tid, NULL);
}

static void log_event(const char *op, const void *object) {
  log_event_for_tid(op, object, current_tid());
}

static const void *thread_object(unsigned long tid) {
  return (const void *)(uintptr_t)tid;
}

static int reserve_thread_identity(unsigned long tid, int detached) {
  int reserved = 0;
  while (atomic_flag_test_and_set_explicit(
      &thread_identity_lock, memory_order_acquire)) {
  }
  for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
    if (thread_identities[index].occupied)
      continue;
    thread_identities[index].tid = tid;
    thread_identities[index].occupied = 1;
    thread_identities[index].handle_valid = 0;
    thread_identities[index].detached = (unsigned char)(detached != 0);
    thread_identities[index].exited = 0;
    thread_identities[index].cancel_requested = 0;
    reserved = 1;
    break;
  }
  atomic_flag_clear_explicit(&thread_identity_lock, memory_order_release);
  return reserved;
}

static void discard_thread_identity(unsigned long tid) {
  while (atomic_flag_test_and_set_explicit(
      &thread_identity_lock, memory_order_acquire)) {
  }
  for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
    if (!thread_identities[index].occupied ||
        thread_identities[index].tid != tid)
      continue;
    thread_identities[index].occupied = 0;
    break;
  }
  atomic_flag_clear_explicit(&thread_identity_lock, memory_order_release);
}

static int bind_thread_identity(unsigned long tid, pthread_t handle,
                                int *retired) {
  int bound = 0;
  *retired = 0;
  while (atomic_flag_test_and_set_explicit(
      &thread_identity_lock, memory_order_acquire)) {
  }
  for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
    struct thread_identity *entry = &thread_identities[index];
    if (!entry->occupied || entry->tid != tid)
      continue;
    entry->handle = handle;
    entry->handle_valid = 1;
    bound = 1;
    if (entry->detached && entry->exited) {
      entry->occupied = 0;
      *retired = 1;
    }
    break;
  }
  atomic_flag_clear_explicit(&thread_identity_lock, memory_order_release);
  return bound;
}

static unsigned long lookup_thread_identity(pthread_t handle) {
  unsigned long tid = 0;
  while (atomic_flag_test_and_set_explicit(
      &thread_identity_lock, memory_order_acquire)) {
  }
  for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
    if (!thread_identities[index].occupied ||
        !thread_identities[index].handle_valid ||
        !pthread_equal(thread_identities[index].handle, handle))
      continue;
    tid = thread_identities[index].tid;
    break;
  }
  atomic_flag_clear_explicit(&thread_identity_lock, memory_order_release);
  return tid;
}

static int mark_thread_exited(unsigned long tid, int *detached,
                              int *cancel_requested) {
  int retired = 0;
  *detached = 0;
  *cancel_requested = 0;
  while (atomic_flag_test_and_set_explicit(
      &thread_identity_lock, memory_order_acquire)) {
  }
  for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
    struct thread_identity *entry = &thread_identities[index];
    if (!entry->occupied || entry->tid != tid)
      continue;
    entry->exited = 1;
    *detached = entry->detached;
    *cancel_requested = entry->cancel_requested;
    if (entry->detached && entry->handle_valid) {
      entry->occupied = 0;
      retired = 1;
    }
    break;
  }
  atomic_flag_clear_explicit(&thread_identity_lock, memory_order_release);
  return retired;
}

static int mark_thread_detached(pthread_t handle) {
  int retired = 0;
  while (atomic_flag_test_and_set_explicit(
      &thread_identity_lock, memory_order_acquire)) {
  }
  for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
    struct thread_identity *entry = &thread_identities[index];
    if (!entry->occupied || !entry->handle_valid ||
        !pthread_equal(entry->handle, handle))
      continue;
    entry->detached = 1;
    if (entry->exited) {
      entry->occupied = 0;
      retired = 1;
    }
    break;
  }
  atomic_flag_clear_explicit(&thread_identity_lock, memory_order_release);
  return retired;
}

static void mark_thread_cancel_requested(pthread_t handle) {
  while (atomic_flag_test_and_set_explicit(
      &thread_identity_lock, memory_order_acquire)) {
  }
  for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
    struct thread_identity *entry = &thread_identities[index];
    if (!entry->occupied || !entry->handle_valid ||
        !pthread_equal(entry->handle, handle))
      continue;
    entry->cancel_requested = 1;
    break;
  }
  atomic_flag_clear_explicit(&thread_identity_lock, memory_order_release);
}

static int retire_joined_thread(pthread_t handle) {
  int retired = 0;
  while (atomic_flag_test_and_set_explicit(
      &thread_identity_lock, memory_order_acquire)) {
  }
  for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
    struct thread_identity *entry = &thread_identities[index];
    if (!entry->occupied || !entry->handle_valid ||
        !pthread_equal(entry->handle, handle))
      continue;
    entry->occupied = 0;
    retired = 1;
    break;
  }
  atomic_flag_clear_explicit(&thread_identity_lock, memory_order_release);
  return retired;
}

static int is_pthread_controlled_op(const char *op) {
  return !strcmp(op, "lock") || !strcmp(op, "trylock") ||
         !strcmp(op, "rdlock") || !strcmp(op, "wrlock") ||
         !strcmp(op, "wait") || !strcmp(op, "join") ||
         !strcmp(op, "detach") || !strcmp(op, "cancel");
}

/* replay_gate_lock must be held by the caller. */
static uintptr_t ready_auxiliary(const char *tag) {
  if (!tag || strncmp(tag, "mutex=", 6))
    return 0;
  char *end = NULL;
  unsigned long parsed = strtoul(tag + 6, &end, 0);
  return end == tag + 6 ? 0 : (uintptr_t)parsed;
}

static void ready_register_locked(unsigned long tid, const char *op,
                                  const void *object, const char *tag) {
  uintptr_t auxiliary = ready_auxiliary(tag);
  size_t free_index = SYMCC_SCHEDULE_MAX_THREADS;
  for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
    if (ready_entries[index].occupied && ready_entries[index].tid == tid) {
      ready_entries[index].op = op;
      ready_entries[index].object = (uintptr_t)object;
      ready_entries[index].auxiliary = auxiliary;
      return;
    }
    if (!ready_entries[index].occupied &&
        free_index == SYMCC_SCHEDULE_MAX_THREADS)
      free_index = index;
  }
  if (free_index == SYMCC_SCHEDULE_MAX_THREADS) {
    ready_incomplete = 1;
    return;
  }
  ready_entries[free_index].tid = tid;
  ready_entries[free_index].op = op;
  ready_entries[free_index].object = (uintptr_t)object;
  ready_entries[free_index].auxiliary = auxiliary;
  ready_entries[free_index].occupied = 1;
  ready_count++;
}

/* replay_gate_lock must be held by the caller. */
static void ready_unregister_locked(unsigned long tid) {
  for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
    if (!ready_entries[index].occupied || ready_entries[index].tid != tid)
      continue;
    ready_entries[index].occupied = 0;
    ready_entries[index].op = NULL;
    ready_entries[index].object = 0;
    ready_entries[index].auxiliary = 0;
    if (ready_count)
      ready_count--;
    break;
  }
  if (!ready_count)
    ready_incomplete = 0;
}

static void sort_ready_tids(unsigned long *tids, size_t count) {
  for (size_t index = 1; index < count; index++) {
    unsigned long value = tids[index];
    size_t insertion = index;
    while (insertion && tids[insertion - 1] > value) {
      tids[insertion] = tids[insertion - 1];
      insertion--;
    }
    tids[insertion] = value;
  }
}

/* replay_gate_lock must be held by the caller. */
static void log_ready_snapshot_locked(const void *object,
                                      unsigned long tid,
                                      unsigned long decision,
                                      int prefix_controlled,
                                      int fallback) {
  unsigned long tids[SYMCC_SCHEDULE_MAX_READY_TRACE];
  size_t cap = enabled_trace_max_threads;
  if (cap > SYMCC_SCHEDULE_MAX_READY_TRACE)
    cap = SYMCC_SCHEDULE_MAX_READY_TRACE;
  size_t count = 0;
  for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
    if (!ready_entries[index].occupied)
      continue;
    if (count < cap)
      tids[count++] = ready_entries[index].tid;
  }
  sort_ready_tids(tids, count);

  char tag[3072];
  int complete = !ready_incomplete && ready_count <= cap;
  int written = snprintf(
      tag, sizeof(tag),
      "decision=%lu chosen=%lu tids=", decision, tid);
  if (written < 0)
    return;
  size_t offset = (size_t)written < sizeof(tag)
                      ? (size_t)written
                      : sizeof(tag) - 1;
  for (size_t index = 0; index < count && offset < sizeof(tag) - 1; index++) {
    written = snprintf(
        tag + offset, sizeof(tag) - offset,
        "%s%lu", index ? "," : "", tids[index]);
    if (written < 0)
      break;
    size_t added = (size_t)written;
    if (added >= sizeof(tag) - offset) {
      offset = sizeof(tag) - 1;
      break;
    }
    offset += added;
  }
  (void)snprintf(
      tag + offset, sizeof(tag) - offset,
      " complete=%d prefix=%d fallback=%d offers=",
      complete, prefix_controlled, fallback);
  offset = strnlen(tag, sizeof(tag));
  for (size_t tid_index = 0;
       tid_index < count && offset < sizeof(tag) - 1;
       tid_index++) {
    const struct ready_entry *entry = NULL;
    for (size_t index = 0; index < SYMCC_SCHEDULE_MAX_THREADS; index++) {
      if (ready_entries[index].occupied &&
          ready_entries[index].tid == tids[tid_index]) {
        entry = &ready_entries[index];
        break;
      }
    }
    if (!entry)
      continue;
    written = snprintf(
        tag + offset, sizeof(tag) - offset,
        "%s%lu:%s:0x%lx:0x%lx",
        tid_index ? "," : "",
        entry->tid,
        entry->op ? entry->op : "unknown",
        (unsigned long)entry->object,
        (unsigned long)entry->auxiliary);
    if (written < 0)
      break;
    size_t added = (size_t)written;
    if (added >= sizeof(tag) - offset) {
      offset = sizeof(tag) - 1;
      break;
    }
    offset += added;
  }
  log_event_for_tid_tag("ready", object, tid, tag);
}

static void log_controlled_decision(const char *op, const void *object,
                                    unsigned long tid, const char *tag,
                                    unsigned long decision) {
  char combined[256];
  if (tag && *tag)
    (void)snprintf(
        combined, sizeof(combined), "%s decision=%lu", tag, decision);
  else
    (void)snprintf(
        combined, sizeof(combined), "decision=%lu", decision);
  log_event_for_tid_tag(op, object, tid, combined);
}

static int schedule_gate_and_log(const char *op, const void *object,
                                 const char *tag,
                                 uint64_t deferred_atomic_group) {
  if (!schedule_enabled || in_schedule_runtime)
    return 0;

  unsigned long tid = current_tid();
  int capture_ready = (
      enabled_trace_enabled && is_pthread_controlled_op(op)
  );
  if (capture_ready) {
    while (atomic_flag_test_and_set_explicit(
        &replay_gate_lock, memory_order_acquire)) {
    }
    ready_register_locked(tid, op, object, tag);
    atomic_flag_clear_explicit(&replay_gate_lock, memory_order_release);
    if (enabled_settle_us > 0) {
      struct timespec settle;
      settle.tv_sec = enabled_settle_us / 1000000L;
      settle.tv_nsec = (enabled_settle_us % 1000000L) * 1000L;
      (void)syscall(SYS_nanosleep, &settle, NULL);
    }
  }

  long start_ms = monotonic_ms();
  for (;;) {
    while (atomic_flag_test_and_set_explicit(
        &replay_gate_lock, memory_order_acquire)) {
    }
    unsigned index = atomic_load_explicit(&prefix_index, memory_order_acquire);
    int prefix_active = prefix_len > 0 && index < prefix_len;
    if (!prefix_active) {
      if (capture_ready) {
        unsigned long decision = atomic_fetch_add_explicit(
            &decision_seq, 1, memory_order_relaxed);
        log_ready_snapshot_locked(
            object, tid, decision, 0, 0);
        log_controlled_decision(op, object, tid, tag, decision);
        ready_unregister_locked(tid);
        atomic_flag_clear_explicit(
            &replay_gate_lock, memory_order_release);
        return 1;
      }
      atomic_flag_clear_explicit(&replay_gate_lock, memory_order_release);
      return 0;
    }
    if (prefix[index] == tid) {
      if (capture_ready) {
        unsigned long decision = atomic_fetch_add_explicit(
            &decision_seq, 1, memory_order_relaxed);
        log_ready_snapshot_locked(
            object, tid, decision, 1, 0);
        log_controlled_decision(op, object, tid, tag, decision);
        ready_unregister_locked(tid);
      } else {
        log_event_for_tid_tag(op, object, tid, tag);
      }
      if (deferred_atomic_group) {
        if (!pending_atomic_commit) {
          pending_atomic_group = deferred_atomic_group;
          pending_atomic_prefix_index = index;
          pending_atomic_commit = 1;
        } else {
          char conflict_tag[192];
          (void)snprintf(
              conflict_tag, sizeof(conflict_tag),
              "group=%llu pending-group=%llu prefix-index=%u",
              (unsigned long long)deferred_atomic_group,
              (unsigned long long)pending_atomic_group,
              pending_atomic_prefix_index);
          log_event_for_tid_tag(
              "atomic_pending_conflict", object, tid, conflict_tag);
        }
      } else {
        atomic_store_explicit(&prefix_index, index + 1, memory_order_release);
      }
      atomic_flag_clear_explicit(&replay_gate_lock, memory_order_release);
      return 1;
    }
    long now_ms = monotonic_ms();
    if (wait_timeout_ms >= 0 && now_ms - start_ms >= wait_timeout_ms) {
      if (capture_ready) {
        unsigned long decision = atomic_fetch_add_explicit(
            &decision_seq, 1, memory_order_relaxed);
        char fallback_tag[64];
        (void)snprintf(
            fallback_tag, sizeof(fallback_tag), "decision=%lu", decision);
        log_ready_snapshot_locked(
            object, tid, decision, 1, 1);
        log_event_for_tid_tag(
            "fallback", object, tid, fallback_tag);
        log_controlled_decision(op, object, tid, tag, decision);
        ready_unregister_locked(tid);
      } else {
        log_event_for_tid("fallback", object, tid);
        log_event_for_tid_tag(op, object, tid, tag);
      }
      atomic_store_explicit(&prefix_index, index + 1, memory_order_release);
      atomic_flag_clear_explicit(&replay_gate_lock, memory_order_release);
      return 1;
    }
    atomic_flag_clear_explicit(&replay_gate_lock, memory_order_release);
    struct timespec req;
    req.tv_sec = 0;
    req.tv_nsec = 1000000L;
    (void)syscall(SYS_nanosleep, &req, NULL);
  }
}

static void controlled_event(const char *op, const void *object) {
  if (!schedule_gate_and_log(op, object, NULL, 0))
    log_event(op, object);
}

static void controlled_event_tag(const char *op, const void *object,
                                 const char *tag) {
  if (!schedule_gate_and_log(op, object, tag, 0))
    log_event_for_tid_tag(op, object, current_tid(), tag);
}

static void controlled_atomic_event_tag(const char *op, const void *object,
                                        const char *tag, uint64_t group) {
  uint64_t deferred = atomic_commit_enabled ? group : 0;
  if (!schedule_gate_and_log(op, object, tag, deferred))
    log_event_for_tid_tag(op, object, current_tid(), tag);
}

static int parse_memory_filter_token(const char *token, size_t length) {
  if (!token || !length)
    return 0;
  if ((length == 3 && !strncasecmp(token, "all", length)) ||
      (length == 4 && !strncasecmp(token, "both", length))) {
    memory_filter_stack = 1;
    memory_filter_owner = 1;
    return 1;
  }
  if ((length == 5 && !strncasecmp(token, "stack", length)) ||
      (length == 12 && !strncasecmp(token, "thread-stack", length))) {
    memory_filter_stack = 1;
    return 1;
  }
  if ((length == 5 && !strncasecmp(token, "owner", length)) ||
      (length == 6 && !strncasecmp(token, "shared", length)) ||
      (length == 7 && !strncasecmp(token, "dynamic", length))) {
    memory_filter_owner = 1;
    return 1;
  }
  if ((length == 3 && !strncasecmp(token, "tag", length)) ||
      (length == 4 && !strncasecmp(token, "tags", length)) ||
      (length == 10 && !strncasecmp(token, "provenance", length))) {
    memory_provenance_enabled = 1;
    return 1;
  }
  if (length == 4 && !strncasecmp(token, "none", length)) {
    memory_filter_stack = 0;
    memory_filter_owner = 0;
    return 1;
  }
  return 0;
}

static void parse_memory_filter(const char *value) {
  if (!value || !*value)
    return;
  const char *start = value;
  for (const char *cursor = value;; ++cursor) {
    if (*cursor != ',' && *cursor != ':' && *cursor != ';' && *cursor != ' ' &&
        *cursor != '\t' && *cursor != '\0')
      continue;
    if (cursor > start)
      (void)parse_memory_filter_token(start, (size_t)(cursor - start));
    if (*cursor == '\0')
      break;
    start = cursor + 1;
  }
}

static int current_stack_contains(const void *address) {
  if (!address)
    return 0;
  uintptr_t value = (uintptr_t)address;
  if (stack_low && value >= stack_low && value < stack_high)
    return 1;

  pthread_attr_t attr;
  void *base = NULL;
  size_t size = 0;
  int matched = 0;
  in_schedule_runtime++;
  if (pthread_getattr_np(pthread_self(), &attr) == 0) {
    if (pthread_attr_getstack(&attr, &base, &size) == 0 && base && size) {
      stack_low = (uintptr_t)base;
      stack_high = stack_low + size;
      matched = value >= stack_low && value < stack_high;
    }
    pthread_attr_destroy(&attr);
  }
  in_schedule_runtime--;
  return matched;
}

static const char *maps_provenance(const void *address) {
  uintptr_t value = (uintptr_t)address;
  const char *result = "prov=unknown";
  in_schedule_runtime++;
  FILE *maps = fopen("/proc/self/maps", "r");
  if (!maps) {
    in_schedule_runtime--;
    return result;
  }
  char line[512];
  while (fgets(line, sizeof(line), maps)) {
    unsigned long start = 0;
    unsigned long end = 0;
    char perms[8] = {0};
    char path[160] = {0};
    int fields = sscanf(line, "%lx-%lx %7s %*s %*s %*s %159s", &start, &end,
                        perms, path);
    if (fields < 3)
      continue;
    if (value < (uintptr_t)start || value >= (uintptr_t)end)
      continue;
    if (fields >= 4 && !strcmp(path, "[heap]"))
      result = "prov=heap";
    else if (fields >= 4 && !strncmp(path, "[stack", 6))
      result = "prov=stack";
    else if (fields >= 4 && path[0] == '[')
      result = "prov=system";
    else if (fields >= 4 && path[0])
      result = "prov=module";
    else
      result = "prov=anonymous";
    break;
  }
  fclose(maps);
  in_schedule_runtime--;
  return result;
}

static const char *memory_provenance_tag(const void *address) {
  if (!memory_provenance_enabled || !address)
    return NULL;
  if (current_stack_contains(address))
    return "prov=stack";
  return maps_provenance(address);
}

static int memory_owner_probe(uintptr_t address,
                              struct memory_owner_entry **entry) {
  if (!memory_owners || memory_owner_slots == 0)
    return 0;
  size_t start = ((address >> 3) ^ (address >> 17)) % memory_owner_slots;
  for (size_t i = 0; i < 16 && i < memory_owner_slots; ++i) {
    struct memory_owner_entry *candidate =
        &memory_owners[(start + i) % memory_owner_slots];
    if (!candidate->occupied || candidate->address == address) {
      *entry = candidate;
      return 1;
    }
  }
  return 0;
}

static int memory_owner_should_log(const char *op, const void *address,
                                   const char **prior_op,
                                   unsigned long *prior_tid) {
  *prior_op = NULL;
  *prior_tid = 0;
  if (!memory_filter_owner)
    return 1;

  uintptr_t key = (uintptr_t)address;
  unsigned long tid = current_tid();
  int is_write = !strcmp(op, "write");
  int should_log = 1;
  while (atomic_flag_test_and_set_explicit(&owner_lock, memory_order_acquire)) {
  }
  struct memory_owner_entry *entry = NULL;
  if (!memory_owner_probe(key, &entry)) {
    atomic_flag_clear_explicit(&owner_lock, memory_order_release);
    return 1;
  }
  if (!entry->occupied) {
    entry->occupied = 1;
    entry->address = key;
    entry->owner_tid = tid;
    entry->shared = 0;
    entry->last_write = (unsigned char)is_write;
    should_log = 0;
  } else if (entry->shared) {
    should_log = 1;
    if (is_write)
      entry->last_write = 1;
  } else if (entry->owner_tid == tid) {
    if (is_write)
      entry->last_write = 1;
    should_log = 0;
  } else {
    entry->shared = 1;
    if (entry->last_write || is_write) {
      *prior_op = entry->last_write ? "write" : "read";
      *prior_tid = entry->owner_tid;
      should_log = 1;
    } else {
      should_log = 0;
    }
    entry->last_write = (unsigned char)(entry->last_write || is_write);
  }
  atomic_flag_clear_explicit(&owner_lock, memory_order_release);
  return should_log;
}

static void memory_event(const char *op, const void *address,
                         size_t byte_length) {
  if (!schedule_enabled || !memory_trace_enabled || !address || !byte_length ||
      in_schedule_runtime)
    return;
  if ((uintptr_t)address == lowered_atomic_address) {
    unsigned *remaining = (
        !strcmp(op, "read")
            ? &lowered_atomic_reads : &lowered_atomic_writes
    );
    if (*remaining) {
      (*remaining)--;
      if (!lowered_atomic_reads && !lowered_atomic_writes)
        lowered_atomic_address = 0;
      return;
    }
  }
  size_t count = byte_length;
  if (count > memory_trace_max_bytes)
    count = memory_trace_max_bytes;
  if (!strcmp(op, "read")) {
    last_schedule_read = (uintptr_t)address;
    last_schedule_read_size = count;
  }
  const unsigned char *bytes = (const unsigned char *)address;
  for (size_t i = 0; i < count; ++i) {
    if (memory_filter_stack && current_stack_contains(bytes + i))
      continue;
    const char *provenance = memory_provenance_tag(bytes + i);
    char tag[96];
    (void)snprintf(
        tag, sizeof(tag), "atomic=0 bytes=1%s%s",
        provenance ? " " : "",
        provenance ? provenance : "");
    const char *prior_op = NULL;
    unsigned long prior_tid = 0;
    if (!memory_owner_should_log(op, bytes + i, &prior_op, &prior_tid))
      continue;
    if (prior_op)
      log_event_for_tid_tag(prior_op, bytes + i, prior_tid, tag);
    controlled_event_tag(op, bytes + i, tag);
  }
}

static void parse_prefix_file(const char *path) {
  if (!path || !*path)
    return;
  FILE *file = fopen(path, "r");
  if (!file)
    return;
  while (prefix_len < SYMCC_SCHEDULE_MAX_PREFIX) {
    unsigned long tid = 0;
    int rc = fscanf(file, " %lu", &tid);
    if (rc == 1) {
      prefix[prefix_len++] = tid;
      continue;
    }
    if (rc == EOF)
      break;
    int ch = fgetc(file);
    if (ch == EOF)
      break;
  }
  fclose(file);
}

__attribute__((constructor)) static void schedule_constructor(void) {
  resolve_symbols();
  if (env_disabled("SYMCC_SCHEDULE") || env_disabled("SYMCC_DPOR"))
    return;

  const char *trace_path = getenv("SYMCC_SCHEDULE_TRACE");
  if (!trace_path || !*trace_path)
    return;
  trace_fd = open(trace_path, O_CREAT | O_WRONLY | O_APPEND | O_CLOEXEC, 0600);
  if (trace_fd < 0)
    return;
  initialize_module_filter();
  schedule_enabled = 1;

  const char *wait_env = getenv("SYMCC_SCHEDULE_WAIT_MS");
  if (wait_env && *wait_env) {
    char *end = NULL;
    long parsed = strtol(wait_env, &end, 10);
    if (end != wait_env)
      wait_timeout_ms = parsed;
  }
  memory_trace_enabled =
      env_enabled("SYMCC_SCHEDULE_MEMORY") || env_enabled("SYMCC_DPOR_MEMORY");
  atomic_commit_enabled = env_enabled("SYMCC_SCHEDULE_ATOMIC_COMMIT");
  memory_provenance_enabled =
      env_enabled("SYMCC_SCHEDULE_MEMORY_PROVENANCE");
  enabled_trace_enabled = env_enabled("SYMCC_SCHEDULE_ENABLED");
  const char *enabled_settle_env = getenv(
      "SYMCC_SCHEDULE_ENABLED_SETTLE_US");
  if (enabled_settle_env && *enabled_settle_env) {
    char *end = NULL;
    long parsed = strtol(enabled_settle_env, &end, 10);
    if (end != enabled_settle_env && parsed >= 0)
      enabled_settle_us = parsed > 1000000L ? 1000000L : parsed;
  }
  const char *enabled_max_env = getenv(
      "SYMCC_SCHEDULE_ENABLED_MAX_THREADS");
  if (enabled_max_env && *enabled_max_env) {
    char *end = NULL;
    unsigned long parsed = strtoul(enabled_max_env, &end, 10);
    if (end != enabled_max_env && parsed > 0) {
      if (parsed > SYMCC_SCHEDULE_MAX_READY_TRACE)
        parsed = SYMCC_SCHEDULE_MAX_READY_TRACE;
      enabled_trace_max_threads = (size_t)parsed;
    }
  }
  parse_memory_filter(getenv("SYMCC_SCHEDULE_MEMORY_FILTER"));
  if (env_enabled("SYMCC_SCHEDULE_MEMORY_STACK"))
    memory_filter_stack = 1;
  if (env_enabled("SYMCC_SCHEDULE_MEMORY_OWNER"))
    memory_filter_owner = 1;
  const char *memory_bytes_env = getenv("SYMCC_SCHEDULE_MEMORY_BYTES");
  if (memory_bytes_env && *memory_bytes_env) {
    char *end = NULL;
    unsigned long parsed = strtoul(memory_bytes_env, &end, 10);
    if (end != memory_bytes_env && parsed > 0)
      memory_trace_max_bytes = parsed > 4096UL ? 4096UL : (size_t)parsed;
  }
  const char *memory_owner_env = getenv("SYMCC_SCHEDULE_MEMORY_OWNERS");
  if (memory_owner_env && *memory_owner_env) {
    char *end = NULL;
    unsigned long parsed = strtoul(memory_owner_env, &end, 10);
    if (end != memory_owner_env && parsed > 0)
      memory_owner_slots = parsed > 1048576UL ? 1048576UL : (size_t)parsed;
  }
  if (memory_filter_owner) {
    in_schedule_runtime++;
    memory_owners = (struct memory_owner_entry *)calloc(
        memory_owner_slots, sizeof(struct memory_owner_entry));
    in_schedule_runtime--;
    if (!memory_owners)
      memory_filter_owner = 0;
  }
  parse_prefix_file(getenv("SYMCC_SCHEDULE_PREFIX"));
  log_event("runtime_start", NULL);
}

__attribute__((destructor)) static void schedule_destructor(void) {
  if (trace_fd >= 0) {
    log_event("runtime_stop", NULL);
    close(trace_fd);
    trace_fd = -1;
  }
  if (memory_owners) {
    in_schedule_runtime++;
    free(memory_owners);
    in_schedule_runtime--;
    memory_owners = NULL;
  }
}

struct thread_cleanup_state {
  unsigned long tid;
  unsigned char normal_return;
};

struct join_cleanup_state {
  const void *object;
  unsigned long target_tid;
  unsigned char attempt_logged;
};

static void thread_exit_cleanup(void *opaque) {
  struct thread_cleanup_state *state =
      (struct thread_cleanup_state *)opaque;
  int detached = 0;
  int cancel_requested = 0;
  int retired = mark_thread_exited(
      state->tid, &detached, &cancel_requested);
  char tag[96];
  (void)snprintf(
      tag, sizeof(tag), "normal=%u detached=%d cancel_requested=%d",
      (unsigned)state->normal_return, detached, cancel_requested);
  log_event_for_tid_tag(
      "thread_exit", thread_object(state->tid), state->tid, tag);
  if (retired) {
    log_event_for_tid_tag(
        "thread_retire", thread_object(state->tid), state->tid,
        "cause=detach");
  }
}

static void join_cancel_cleanup(void *opaque) {
  const struct join_cleanup_state *state =
      (const struct join_cleanup_state *)opaque;
  if (!state->attempt_logged) {
    log_event_for_tid_tag(
        "join", state->object, current_tid(),
        state->target_tid ? "mapped=1" : "mapped=0");
  }
  log_event_for_tid_tag(
      "join_cancelled", state->object, current_tid(),
      state->target_tid ? "mapped=1" : "mapped=0");
}

static void *thread_trampoline(void *opaque) {
  struct thread_start *start = (struct thread_start *)opaque;
  void *(*start_routine)(void *) = start->start_routine;
  void *arg = start->arg;
  unsigned long tid = start->tid;
  logical_tid = tid;
  in_schedule_runtime++;
  free(start);
  in_schedule_runtime--;
  log_event("thread_start", thread_object(tid));
  void *result = NULL;
  struct thread_cleanup_state cleanup = {
      .tid = tid,
      .normal_return = 0,
  };
  pthread_cleanup_push(thread_exit_cleanup, &cleanup);
  result = start_routine(arg);
  cleanup.normal_return = 1;
  pthread_cleanup_pop(1);
  return result;
}

int pthread_create(pthread_t *thread, const pthread_attr_t *attr,
                   void *(*start_routine)(void *), void *arg) {
  resolve_symbols();
  if (!real_pthread_create)
    return EAGAIN;
  if (!caller_is_traced(__builtin_return_address(0)))
    return real_pthread_create(thread, attr, start_routine, arg);

  in_schedule_runtime++;
  struct thread_start *start =
      (struct thread_start *)malloc(sizeof(struct thread_start));
  in_schedule_runtime--;
  if (!start)
    return real_pthread_create(thread, attr, start_routine, arg);

  start->start_routine = start_routine;
  start->arg = arg;
  unsigned long child_tid = atomic_fetch_add_explicit(
      &next_tid, 1, memory_order_relaxed);
  start->tid = child_tid;
  int detached = 0;
  if (attr) {
    int detach_state = PTHREAD_CREATE_JOINABLE;
    if (pthread_attr_getdetachstate(attr, &detach_state) == 0)
      detached = detach_state == PTHREAD_CREATE_DETACHED;
  }
  int reserved = reserve_thread_identity(child_tid, detached);
  log_event_for_tid_tag(
      "create", thread_object(child_tid), current_tid(),
      detached ? "detached=1" : "detached=0");
  int rc = real_pthread_create(thread, attr, thread_trampoline, start);
  if (rc != 0) {
    log_event("create_fail", thread_object(child_tid));
    if (reserved)
      discard_thread_identity(child_tid);
    in_schedule_runtime++;
    free(start);
    in_schedule_runtime--;
  } else {
    int retired = 0;
    int registered = (
        reserved && bind_thread_identity(child_tid, *thread, &retired)
    );
    char tag[64];
    (void)snprintf(
        tag, sizeof(tag), "mapped=%d detached=%d",
        registered, detached);
    log_event_for_tid_tag(
        "create_success", thread_object(child_tid), current_tid(),
        tag);
    if (retired) {
      log_event_for_tid_tag(
          "thread_retire", thread_object(child_tid), current_tid(),
          "cause=detach");
    }
  }
  return rc;
}

int pthread_join(pthread_t thread, void **retval) {
  resolve_symbols();
  if (!real_pthread_join)
    return EINVAL;
  if (!caller_is_traced(__builtin_return_address(0)))
    return real_pthread_join(thread, retval);
  unsigned long target_tid = lookup_thread_identity(thread);
  const void *object = (
      target_tid ? thread_object(target_tid)
                 : (const void *)(uintptr_t)thread
  );
  int rc = 0;
  struct join_cleanup_state cleanup = {
      .object = object,
      .target_tid = target_tid,
      .attempt_logged = 0,
  };
  pthread_cleanup_push(join_cancel_cleanup, &cleanup);
  if (target_tid)
    controlled_event_tag("join", object, "mapped=1");
  else
    controlled_event_tag("join", object, "mapped=0");
  cleanup.attempt_logged = 1;
  rc = real_pthread_join(thread, retval);
  pthread_cleanup_pop(0);
  log_event_for_tid_tag(
      rc == 0 ? "join_success" : "join_fail", object, current_tid(),
      target_tid ? "mapped=1" : "mapped=0");
  if (rc == 0 && target_tid && retire_joined_thread(thread)) {
    log_event_for_tid_tag(
        "thread_retire", object, current_tid(), "cause=join");
  }
  return rc;
}

int pthread_detach(pthread_t thread) {
  resolve_symbols();
  if (!real_pthread_detach)
    return EINVAL;
  if (!caller_is_traced(__builtin_return_address(0)))
    return real_pthread_detach(thread);
  unsigned long target_tid = lookup_thread_identity(thread);
  const void *object = (
      target_tid ? thread_object(target_tid)
                 : (const void *)(uintptr_t)thread
  );
  controlled_event_tag(
      "detach", object, target_tid ? "mapped=1" : "mapped=0");
  int rc = real_pthread_detach(thread);
  log_event_for_tid_tag(
      rc == 0 ? "detach_success" : "detach_fail",
      object,
      current_tid(),
      target_tid ? "mapped=1" : "mapped=0");
  if (rc == 0 && target_tid && mark_thread_detached(thread)) {
    log_event_for_tid_tag(
        "thread_retire", object, current_tid(), "cause=detach");
  }
  return rc;
}

int pthread_cancel(pthread_t thread) {
  resolve_symbols();
  if (!real_pthread_cancel)
    return ESRCH;
  if (!caller_is_traced(__builtin_return_address(0)))
    return real_pthread_cancel(thread);
  unsigned long target_tid = lookup_thread_identity(thread);
  const void *object = (
      target_tid ? thread_object(target_tid)
                 : (const void *)(uintptr_t)thread
  );
  controlled_event_tag(
      "cancel", object, target_tid ? "mapped=1" : "mapped=0");
  int rc = real_pthread_cancel(thread);
  if (rc == 0 && target_tid)
    mark_thread_cancel_requested(thread);
  log_event_for_tid_tag(
      rc == 0 ? "cancel_success" : "cancel_fail",
      object,
      current_tid(),
      target_tid ? "mapped=1" : "mapped=0");
  return rc;
}

int pthread_mutex_lock(pthread_mutex_t *mutex) {
  resolve_symbols();
  if (!real_pthread_mutex_lock)
    return EINVAL;
  int trace = caller_is_traced(__builtin_return_address(0));
  if (trace)
    controlled_event("lock", mutex);
  int rc = real_pthread_mutex_lock(mutex);
  if (trace)
    log_event(rc == 0 ? "acquire" : "lock_fail", mutex);
  return rc;
}

int pthread_mutex_trylock(pthread_mutex_t *mutex) {
  resolve_symbols();
  if (!real_pthread_mutex_trylock)
    return EINVAL;
  int trace = caller_is_traced(__builtin_return_address(0));
  if (trace)
    controlled_event("trylock", mutex);
  int rc = real_pthread_mutex_trylock(mutex);
  if (trace)
    log_event(rc == 0 ? "acquire" : "trylock_fail", mutex);
  return rc;
}

int pthread_mutex_unlock(pthread_mutex_t *mutex) {
  resolve_symbols();
  if (!real_pthread_mutex_unlock)
    return EINVAL;
  if (caller_is_traced(__builtin_return_address(0)))
    log_event("unlock", mutex);
  return real_pthread_mutex_unlock(mutex);
}

int pthread_rwlock_rdlock(pthread_rwlock_t *lock) {
  resolve_symbols();
  if (!real_pthread_rwlock_rdlock)
    return EINVAL;
  int trace = caller_is_traced(__builtin_return_address(0));
  if (trace)
    controlled_event("rdlock", lock);
  int rc = real_pthread_rwlock_rdlock(lock);
  if (trace)
    log_event(rc == 0 ? "acquire" : "rdlock_fail", lock);
  return rc;
}

int pthread_rwlock_wrlock(pthread_rwlock_t *lock) {
  resolve_symbols();
  if (!real_pthread_rwlock_wrlock)
    return EINVAL;
  int trace = caller_is_traced(__builtin_return_address(0));
  if (trace)
    controlled_event("wrlock", lock);
  int rc = real_pthread_rwlock_wrlock(lock);
  if (trace)
    log_event(rc == 0 ? "acquire" : "wrlock_fail", lock);
  return rc;
}

int pthread_rwlock_unlock(pthread_rwlock_t *lock) {
  resolve_symbols();
  if (!real_pthread_rwlock_unlock)
    return EINVAL;
  if (caller_is_traced(__builtin_return_address(0)))
    log_event("rwunlock", lock);
  return real_pthread_rwlock_unlock(lock);
}

int pthread_cond_wait(pthread_cond_t *cond, pthread_mutex_t *mutex) {
  resolve_symbols();
  if (!real_pthread_cond_wait)
    return EINVAL;
  int trace = caller_is_traced(__builtin_return_address(0));
  if (trace) {
    char tag[64];
    (void)snprintf(
        tag, sizeof(tag), "mutex=0x%lx timed=0",
        (unsigned long)(uintptr_t)mutex);
    controlled_event_tag("wait", cond, tag);
    log_event("wait_mutex_release", mutex);
  }
  int rc = real_pthread_cond_wait(cond, mutex);
  if (trace) {
    if (rc == 0 || rc == ETIMEDOUT)
      log_event("wait_mutex_acquire", mutex);
    log_event(rc == 0 ? "wake"
                      : (rc == ETIMEDOUT ? "wait_timeout" : "wait_fail"),
              cond);
  }
  return rc;
}

int pthread_cond_timedwait(pthread_cond_t *cond, pthread_mutex_t *mutex,
                           const struct timespec *abstime) {
  resolve_symbols();
  if (!real_pthread_cond_timedwait)
    return EINVAL;
  int trace = caller_is_traced(__builtin_return_address(0));
  if (trace) {
    char tag[64];
    (void)snprintf(
        tag, sizeof(tag), "mutex=0x%lx timed=1",
        (unsigned long)(uintptr_t)mutex);
    controlled_event_tag("wait", cond, tag);
    log_event("wait_mutex_release", mutex);
  }
  int rc = real_pthread_cond_timedwait(cond, mutex, abstime);
  if (trace) {
    if (rc == 0 || rc == ETIMEDOUT)
      log_event("wait_mutex_acquire", mutex);
    log_event(rc == 0 ? "wake"
                      : (rc == ETIMEDOUT ? "wait_timeout" : "wait_fail"),
              cond);
  }
  return rc;
}

int pthread_cond_signal(pthread_cond_t *cond) {
  resolve_symbols();
  if (!real_pthread_cond_signal)
    return EINVAL;
  if (caller_is_traced(__builtin_return_address(0)))
    log_event("signal", cond);
  return real_pthread_cond_signal(cond);
}

int pthread_cond_broadcast(pthread_cond_t *cond) {
  resolve_symbols();
  if (!real_pthread_cond_broadcast)
    return EINVAL;
  if (caller_is_traced(__builtin_return_address(0)))
    log_event("broadcast", cond);
  return real_pthread_cond_broadcast(cond);
}

void _sym_notify_schedule_read(const void *address, size_t byte_length) {
  memory_event("read", address, byte_length);
}

void _sym_notify_schedule_write(const void *address, size_t byte_length) {
  memory_event("write", address, byte_length);
}

static const char *schedule_atomic_order_name(uint8_t order) {
  switch (order) {
  case 2:
    return "acquire";
  case 3:
    return "release";
  case 4:
    return "acq_rel";
  case 5:
    return "seq_cst";
  default:
    return "relaxed";
  }
}

uint64_t _sym_notify_schedule_atomic(const void *address, size_t byte_length,
                                     uint8_t kind, uint8_t success_order,
                                     uint8_t failure_order,
                                     uint8_t operation) {
  if (!schedule_enabled || !memory_trace_enabled || !address ||
      in_schedule_runtime)
    return 0;
  unsigned long group = atomic_fetch_add_explicit(
      &atomic_group_seq, 1, memory_order_relaxed) + 1;
  const char *op = "rmw";
  const char *kind_name = "rmw";
  if (kind == 0) {
    op = "read";
    kind_name = "load";
  } else if (kind == 1) {
    op = "write";
    kind_name = "store";
  } else if (kind == 3) {
    kind_name = "cmpxchg";
  } else if (kind == 4) {
    op = "fence";
    kind_name = "fence";
  }
  if (kind != 4 && byte_length == 0)
    return 0;
  if (kind != 4) {
    lowered_atomic_address = (uintptr_t)address;
    lowered_atomic_reads = (
        kind == 0 || kind == 2 || kind == 3
    );
    lowered_atomic_writes = (
        kind == 1 || kind == 2 || kind == 3
    );
  }
  if (kind != 4 && memory_filter_stack &&
      current_stack_contains(address))
    return group;
  if (kind == 0 || kind == 2 || kind == 3) {
    last_schedule_read = (uintptr_t)address;
    last_schedule_read_size = byte_length;
  }

  const char *provenance = (
      kind == 4 ? NULL : memory_provenance_tag(address)
  );
  char tag[320];
  (void)snprintf(
      tag, sizeof(tag),
      "atomic=1 kind=%s group=%lu bytes=%zu mo=%s "
      "failure-mo=%s operation=%u%s%s",
      kind_name, group, byte_length,
      schedule_atomic_order_name(success_order),
      schedule_atomic_order_name(failure_order),
      (unsigned)operation,
      provenance ? " " : "",
      provenance ? provenance : "");

  if (kind != 4 && memory_filter_owner) {
    const char *prior_op = NULL;
    unsigned long prior_tid = 0;
    const char *owner_op = kind == 0 ? "read" : "write";
    if (!memory_owner_should_log(
            owner_op, address, &prior_op, &prior_tid))
      return group;
    if (prior_op)
      log_event_for_tid_tag(
          prior_op, address, prior_tid, provenance);
  }
  controlled_atomic_event_tag(op, address, tag, group);
  return group;
}

void _sym_notify_schedule_atomic_result(uint64_t group, const void *address,
                                        _Bool success) {
  if (!schedule_enabled || !memory_trace_enabled || !group ||
      in_schedule_runtime)
    return;
  char tag[96];
  (void)snprintf(
      tag, sizeof(tag), "group=%llu success=%u",
      (unsigned long long)group, success ? 1U : 0U);
  log_event_for_tid_tag(
      "atomic_result", address, current_tid(), tag);
  lowered_atomic_address = 0;
  lowered_atomic_reads = 0;
  lowered_atomic_writes = 0;
}

static const char *schedule_atomic_value_role_name(uint8_t role) {
  switch (role) {
  case 0:
    return "read";
  case 1:
    return "write";
  case 2:
    return "operand";
  case 3:
    return "expected";
  case 4:
    return "desired";
  default:
    return "unknown";
  }
}

void _sym_notify_schedule_atomic_value(uint64_t group, const void *address,
                                       uint64_t value, uint8_t bits,
                                       uint8_t role) {
  if (!schedule_enabled || !memory_trace_enabled || !group || !address ||
      bits == 0 || bits > 64 || role > 4 || in_schedule_runtime)
    return;
  char tag[192];
  (void)snprintf(
      tag, sizeof(tag),
      "group=%llu role=%s bits=%u value=0x%llx",
      (unsigned long long)group, schedule_atomic_value_role_name(role),
      (unsigned)bits, (unsigned long long)value);
  log_event_for_tid_tag(
      "atomic_value", address, current_tid(), tag);
  if (role == 0 || role == 1) {
    lowered_atomic_address = 0;
    lowered_atomic_reads = 0;
    lowered_atomic_writes = 0;
  }
}

void _sym_notify_schedule_atomic_commit(uint64_t group, const void *address) {
  if (!schedule_enabled || !memory_trace_enabled || !group || !address ||
      in_schedule_runtime)
    return;
  int advanced = 0;
  int mismatch = 0;
  unsigned committed_index = 0;
  if (atomic_commit_enabled) {
    atomic_thread_fence(memory_order_seq_cst);
    while (atomic_flag_test_and_set_explicit(
        &replay_gate_lock, memory_order_acquire)) {
    }
    if (pending_atomic_commit) {
      unsigned current = atomic_load_explicit(
          &prefix_index, memory_order_acquire);
      committed_index = pending_atomic_prefix_index;
      if (pending_atomic_group == group &&
          current == pending_atomic_prefix_index) {
        atomic_store_explicit(
            &prefix_index, current + 1, memory_order_release);
        advanced = 1;
      } else {
        mismatch = 1;
      }
      pending_atomic_commit = 0;
      pending_atomic_group = 0;
      pending_atomic_prefix_index = 0;
    }
    atomic_flag_clear_explicit(
        &replay_gate_lock, memory_order_release);
  }
  char tag[160];
  (void)snprintf(
      tag, sizeof(tag),
      "group=%llu mode=%u advanced=%u mismatch=%u prefix-index=%u",
      (unsigned long long)group, atomic_commit_enabled ? 1U : 0U,
      advanced ? 1U : 0U, mismatch ? 1U : 0U, committed_index);
  log_event_for_tid_tag(
      "atomic_commit", address, current_tid(), tag);
  lowered_atomic_address = 0;
  lowered_atomic_reads = 0;
  lowered_atomic_writes = 0;
}

void _sym_notify_schedule_block(uintptr_t site_id) {
  if (!schedule_enabled || !memory_trace_enabled || in_schedule_runtime)
    return;
  log_event("action", (const void *)site_id);
}

void _sym_notify_schedule_branch(uintptr_t site_id, uint64_t outcome,
                                 uintptr_t successor_id) {
  if (!schedule_enabled || !memory_trace_enabled || in_schedule_runtime)
    return;
  char tag[192];
  (void)snprintf(
      tag, sizeof(tag),
      "outcome=0x%llx next=0x%lx last-read=0x%lx last-read-bytes=%zu",
      (unsigned long long)outcome,
      (unsigned long)successor_id,
      (unsigned long)last_schedule_read,
      last_schedule_read_size);
  log_event_for_tid_tag(
      "constraint", (const void *)site_id, current_tid(), tag);
}
