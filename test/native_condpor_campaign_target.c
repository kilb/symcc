// RUN: cc -std=c11 -O0 -pthread %s -ldl -o %t
// RUN: rm -f %t.trace
// RUN: env LD_PRELOAD=%schedrt SYMCC_DPOR=1 SYMCC_SCHEDULE_MEMORY=1 SYMCC_SCHEDULE_TRACE=%t.trace %t
// RUN: python3 -c "rows=[line.split() for line in open(r'%t.trace')]; assert sum(row[2]=='write' for row in rows)==1; assert sum(row[2]=='read' for row in rows)==1; assert sum(row[2]=='action' for row in rows)==1; assert not any(row[2]=='fallback' for row in rows)"

#include <dlfcn.h>
#include <pthread.h>
#include <stddef.h>
#include <stdatomic.h>
#include <stdint.h>

typedef void (*notify_memory_fn)(const void *, size_t);
typedef void (*notify_block_fn)(uintptr_t);

static _Atomic unsigned char shared_value;
static notify_memory_fn notify_read;
static notify_memory_fn notify_write;
static notify_block_fn notify_block;

static void *writer(void *unused) {
  (void)unused;
  notify_write((const void *)&shared_value, 1);
  atomic_store_explicit(&shared_value, 1, memory_order_relaxed);
  return NULL;
}

static void *reader(void *unused) {
  (void)unused;
  notify_read((const void *)&shared_value, 1);
  unsigned char value = atomic_load_explicit(
      &shared_value, memory_order_relaxed);
  notify_block(value == 1 ? 0x101U : 0x202U);
  return NULL;
}

int main(void) {
  notify_read = (notify_memory_fn)dlsym(
      RTLD_DEFAULT, "_sym_notify_schedule_read");
  notify_write = (notify_memory_fn)dlsym(
      RTLD_DEFAULT, "_sym_notify_schedule_write");
  notify_block = (notify_block_fn)dlsym(
      RTLD_DEFAULT, "_sym_notify_schedule_block");
  if (!notify_read || !notify_write || !notify_block)
    return 90;

  pthread_t write_thread;
  pthread_t read_thread;
  if (pthread_create(&write_thread, NULL, writer, NULL) != 0)
    return 91;
  if (pthread_create(&read_thread, NULL, reader, NULL) != 0)
    return 92;
  if (pthread_join(write_thread, NULL) != 0)
    return 93;
  if (pthread_join(read_thread, NULL) != 0)
    return 94;
  return 0;
}
