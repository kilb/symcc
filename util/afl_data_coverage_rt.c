/*
 * Native AFL++ data-coverage feedback for constant/data comparisons.
 *
 * The library is intended to be injected with AFL_PRELOAD, so afl-fuzz itself is
 * not preloaded while the target process is.  It writes comparison-prefix
 * progress into AFL's native shared bitmap (__afl_area_ptr when exported, or
 * the SysV shared memory map pointed to by __AFL_SHM_ID).  More matched prefix
 * bytes touch more bitmap slots, making data progress visible to AFL's normal
 * queue retention logic.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <ctype.h>
#include <pthread.h>
#include <sys/shm.h>

#ifndef MAP_SIZE
#define MAP_SIZE 65536U
#endif

/*
 * Keep data-progress features in a fixed low namespace.  AFL++ PCGUARD targets
 * export __afl_final_loc; reserving this prefix before their constructors run
 * makes edge IDs start after the data namespace and includes both namespaces
 * in forkserver map-size negotiation.
 */
extern uint32_t __afl_final_loc __attribute__((weak));

static unsigned char **afl_area_ptr_addr;
static unsigned char *afl_area_direct;
static size_t afl_data_map_size = MAP_SIZE;
static int data_cov_enabled = 1;
static __thread int data_cov_recording;
static __thread int data_cov_initializing;
static pthread_once_t data_cov_once = PTHREAD_ONCE_INIT;

#define SITE_CACHE_SIZE 64U
struct stable_site_entry {
  uintptr_t address;
  uint64_t identity;
};
static __thread struct stable_site_entry site_cache[SITE_CACHE_SIZE];

typedef int (*memcmp_fn)(const void *, const void *, size_t);
typedef int (*bcmp_fn)(const void *, const void *, size_t);
typedef int (*strcmp_fn)(const char *, const char *);
typedef int (*strncmp_fn)(const char *, const char *, size_t);
typedef int (*strcasecmp_fn)(const char *, const char *);
typedef int (*strncasecmp_fn)(const char *, const char *, size_t);

static memcmp_fn real_memcmp;
static bcmp_fn real_bcmp;
static strcmp_fn real_strcmp;
static strncmp_fn real_strncmp;
static strcasecmp_fn real_strcasecmp;
static strncasecmp_fn real_strncasecmp;

/*
 * dlsym may itself compare strings.  These volatile loops are deliberately
 * kept independent of libc so a comparison re-entering this interposer while
 * pthread_once is active cannot recurse or deadlock.  Volatile accesses also
 * prevent an optimizing compiler from replacing a loop with memcmp/strcmp.
 */
static __attribute__((noinline)) int raw_memcmp(const void *a, const void *b,
                                                size_t n) {
  const volatile unsigned char *left =
      (const volatile unsigned char *)a;
  const volatile unsigned char *right =
      (const volatile unsigned char *)b;
  for (size_t i = 0; i < n; ++i) {
    if (left[i] != right[i])
      return (int)left[i] - (int)right[i];
  }
  return 0;
}

static __attribute__((noinline)) int raw_strcmp(const char *a,
                                                const char *b) {
  const volatile unsigned char *left =
      (const volatile unsigned char *)a;
  const volatile unsigned char *right =
      (const volatile unsigned char *)b;
  for (size_t i = 0;; ++i) {
    if (left[i] != right[i] || left[i] == 0)
      return (int)left[i] - (int)right[i];
  }
}

static __attribute__((noinline)) int raw_strncmp(const char *a, const char *b,
                                                 size_t n) {
  const volatile unsigned char *left =
      (const volatile unsigned char *)a;
  const volatile unsigned char *right =
      (const volatile unsigned char *)b;
  for (size_t i = 0; i < n; ++i) {
    if (left[i] != right[i] || left[i] == 0)
      return (int)left[i] - (int)right[i];
  }
  return 0;
}

static unsigned char ascii_fold(unsigned char value) {
  return value >= 'A' && value <= 'Z' ? (unsigned char)(value + ('a' - 'A'))
                                      : value;
}

static __attribute__((noinline)) int raw_strncasecmp(const char *a,
                                                     const char *b,
                                                     size_t n) {
  const volatile unsigned char *left =
      (const volatile unsigned char *)a;
  const volatile unsigned char *right =
      (const volatile unsigned char *)b;
  for (size_t i = 0; i < n; ++i) {
    unsigned char ca = left[i];
    unsigned char cb = right[i];
    unsigned char fa = ascii_fold(ca);
    unsigned char fb = ascii_fold(cb);
    if (fa != fb || ca == 0 || cb == 0)
      return (int)fa - (int)fb;
  }
  return 0;
}

static __attribute__((noinline)) int raw_strcasecmp(const char *a,
                                                    const char *b) {
  return raw_strncasecmp(a, b, (size_t)-1);
}

__attribute__((constructor)) static void data_cov_reserve_namespace(void) {
  if (&__afl_final_loc && __afl_final_loc < MAP_SIZE)
    __afl_final_loc = MAP_SIZE;
}

static uint64_t fnv_mix(uint64_t value, uint64_t word) {
  value ^= word;
  return value * 1099511628211ULL;
}

static uint64_t stable_site_identity(uintptr_t site) {
  size_t slot = (size_t)((site >> 4) % SITE_CACHE_SIZE);
  if (site_cache[slot].address == site)
    return site_cache[slot].identity;

  uint64_t identity = (uint64_t)site;
  Dl_info info;
  if (dladdr((void *)site, &info) && info.dli_fbase) {
    identity = 1469598103934665603ULL;
    const char *name = info.dli_fname ? info.dli_fname : "";
    const char *base = strrchr(name, '/');
    base = base ? base + 1 : name;
    for (const unsigned char *cursor = (const unsigned char *)base; *cursor;
         ++cursor)
      identity = fnv_mix(identity, *cursor);
    identity =
        fnv_mix(identity, (uint64_t)(site - (uintptr_t)info.dli_fbase));
  }
  site_cache[slot].address = site;
  site_cache[slot].identity = identity;
  return identity;
}

static int env_disabled(const char *name) {
  const char *value = getenv(name);
  if (!value || !*value)
    return 0;
  return raw_strcmp(value, "0") == 0 || raw_strcasecmp(value, "false") == 0 ||
         raw_strcasecmp(value, "off") == 0 ||
         raw_strcasecmp(value, "no") == 0;
}

static void data_cov_initialize_once(void) {
  data_cov_initializing = 1;
  data_cov_enabled = !env_disabled("AFL_DATA_COVERAGE") &&
                     !env_disabled("SYMCC_AFL_DATA_COVERAGE");

  afl_area_ptr_addr = (unsigned char **)dlsym(RTLD_DEFAULT, "__afl_area_ptr");
  if (!afl_area_ptr_addr)
    afl_area_ptr_addr =
        (unsigned char **)dlsym(RTLD_DEFAULT, "__symcc_afl_area_ptr");
  if (!afl_area_ptr_addr && !afl_area_direct) {
    const char *shm_id_env = getenv("__AFL_SHM_ID");
    if (shm_id_env && *shm_id_env) {
      char *end = NULL;
      long shm_id = strtol(shm_id_env, &end, 10);
      if (end != shm_id_env && end && *end == '\0' && shm_id >= 0) {
        void *area = shmat((int)shm_id, NULL, 0);
        if (area != (void *)-1)
          afl_area_direct = (unsigned char *)area;
      }
    }
  }

  const char *map_size_env = getenv("SYMCC_AFL_DATA_MAP_SIZE");
  if (map_size_env && *map_size_env) {
    char *end = NULL;
    unsigned long parsed = strtoul(map_size_env, &end, 10);
    if (end != map_size_env && end && *end == '\0' &&
        parsed > 0 && parsed <= MAP_SIZE)
      afl_data_map_size = (size_t)parsed;
  }

  real_memcmp = (memcmp_fn)dlsym(RTLD_NEXT, "memcmp");
  real_bcmp = (bcmp_fn)dlsym(RTLD_NEXT, "bcmp");
  real_strcmp = (strcmp_fn)dlsym(RTLD_NEXT, "strcmp");
  real_strncmp = (strncmp_fn)dlsym(RTLD_NEXT, "strncmp");
  real_strcasecmp = (strcasecmp_fn)dlsym(RTLD_NEXT, "strcasecmp");
  real_strncasecmp = (strncasecmp_fn)dlsym(RTLD_NEXT, "strncasecmp");
  data_cov_initializing = 0;
}

static void data_cov_init(void) {
  if (!data_cov_initializing)
    (void)pthread_once(&data_cov_once, data_cov_initialize_once);
}

static void increment_map_byte(unsigned char *slot) {
  unsigned char observed = __atomic_load_n(slot, __ATOMIC_RELAXED);
  for (;;) {
    unsigned char desired =
        observed == 255 ? 1 : (unsigned char)(observed + 1);
    if (__atomic_compare_exchange_n(slot, &observed, desired, 1,
                                    __ATOMIC_RELAXED, __ATOMIC_RELAXED))
      return;
  }
}

static void data_cov_record(uintptr_t site, size_t matched, size_t limit) {
  data_cov_init();
  unsigned char *map = NULL;
  if (afl_area_ptr_addr)
    map = __atomic_load_n(afl_area_ptr_addr, __ATOMIC_ACQUIRE);
  else
    map = afl_area_direct;
  if (!data_cov_enabled || !map || afl_data_map_size == 0)
    return;
  if (data_cov_recording)
    return;
  data_cov_recording = 1;
  if (matched > limit)
    matched = limit;

  uint64_t base = 1469598103934665603ULL;
  base = fnv_mix(base, stable_site_identity(site));
  base = fnv_mix(base, (uint64_t)limit);

  /*
   * Emit one slot for each achieved prefix bucket. A mutation that improves
   * from N to N+1 bytes therefore creates at least one genuinely new AFL map
   * bit/byte, which AFL can retain with its existing coverage machinery.
   */
  for (size_t prefix = 0; prefix <= matched; ++prefix) {
    uint64_t h = fnv_mix(base, (uint64_t)prefix);
    size_t idx = (size_t)(h % afl_data_map_size);
    increment_map_byte(&map[idx]);
  }
  data_cov_recording = 0;
}

static size_t common_prefix(const unsigned char *a, const unsigned char *b,
                            size_t n) {
  size_t matched = 0;
  while (matched < n && a[matched] == b[matched])
    matched++;
  return matched;
}

static size_t common_string_prefix(const char *a, const char *b, size_t n,
                                   int fold_case) {
  size_t matched = 0;
  while (matched < n) {
    unsigned char ca = (unsigned char)a[matched];
    unsigned char cb = (unsigned char)b[matched];
    unsigned char fa = fold_case ? (unsigned char)tolower(ca) : ca;
    unsigned char fb = fold_case ? (unsigned char)tolower(cb) : cb;
    if (fa != fb)
      break;
    if (ca == '\0' || cb == '\0') {
      matched++;
      break;
    }
    matched++;
  }
  return matched;
}

int memcmp(const void *a, const void *b, size_t n) {
  if (data_cov_initializing)
    return raw_memcmp(a, b, n);
  data_cov_init();
  int result = real_memcmp ? real_memcmp(a, b, n) : raw_memcmp(a, b, n);
  if (n)
    data_cov_record((uintptr_t)__builtin_return_address(0),
                    common_prefix((const unsigned char *)a,
                                  (const unsigned char *)b, n),
                    n);
  return result;
}

int bcmp(const void *a, const void *b, size_t n) {
  if (data_cov_initializing)
    return raw_memcmp(a, b, n);
  data_cov_init();
  int result = real_bcmp ? real_bcmp(a, b, n) :
                           (real_memcmp ? real_memcmp(a, b, n)
                                        : raw_memcmp(a, b, n));
  if (n)
    data_cov_record((uintptr_t)__builtin_return_address(0),
                    common_prefix((const unsigned char *)a,
                                  (const unsigned char *)b, n),
                    n);
  return result;
}

int strcmp(const char *a, const char *b) {
  if (data_cov_initializing)
    return raw_strcmp(a, b);
  data_cov_init();
  int result = real_strcmp ? real_strcmp(a, b) : raw_strcmp(a, b);
  {
    size_t limit = strlen(a);
    size_t other = strlen(b);
    if (other > limit)
      limit = other;
    data_cov_record((uintptr_t)__builtin_return_address(0),
                    common_string_prefix(a, b, limit + 1, 0), limit + 1);
  }
  return result;
}

int strncmp(const char *a, const char *b, size_t n) {
  if (data_cov_initializing)
    return raw_strncmp(a, b, n);
  data_cov_init();
  int result = real_strncmp ? real_strncmp(a, b, n) : raw_strncmp(a, b, n);
  if (n)
    data_cov_record((uintptr_t)__builtin_return_address(0),
                    common_string_prefix(a, b, n, 0), n);
  return result;
}

int strcasecmp(const char *a, const char *b) {
  if (data_cov_initializing)
    return raw_strcasecmp(a, b);
  data_cov_init();
  int result = real_strcasecmp ? real_strcasecmp(a, b)
                               : raw_strcasecmp(a, b);
  {
    size_t limit = strlen(a);
    size_t other = strlen(b);
    if (other > limit)
      limit = other;
    data_cov_record((uintptr_t)__builtin_return_address(0),
                    common_string_prefix(a, b, limit + 1, 1), limit + 1);
  }
  return result;
}

int strncasecmp(const char *a, const char *b, size_t n) {
  if (data_cov_initializing)
    return raw_strncasecmp(a, b, n);
  data_cov_init();
  int result = real_strncasecmp ? real_strncasecmp(a, b, n)
                                : raw_strncasecmp(a, b, n);
  if (n)
    data_cov_record((uintptr_t)__builtin_return_address(0),
                    common_string_prefix(a, b, n, 1), n);
  return result;
}
