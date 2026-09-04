// RUN: env SYMCC_DPOR_ATOMIC_ONLY=1 SYMCC_DPOR_SCHEDULE_ONLY=1 %symcc -O0 -S -emit-llvm %s -o %t.ll -pthread
// RUN: grep -q "load atomic" %t.ll
// RUN: grep -q "store atomic" %t.ll
// RUN: env SYMCC_DPOR_ATOMIC_ONLY=1 SYMCC_DPOR_SCHEDULE_ONLY=1 %symcc -O0 %s -o %t -pthread
// RUN: python3 -c "open(r'%t.reader-first.prefix','w').write('2\n1\n')"
// RUN: python3 -c "open(r'%t.writer-first.prefix','w').write('1\n2\n')"
// RUN: rm -f %t.reader-first.trace %t.writer-first.trace
// RUN: env LD_PRELOAD=%schedrt SYMCC_DPOR=1 SYMCC_SCHEDULE_MEMORY=1 SYMCC_SCHEDULE_ATOMIC_COMMIT=1 SYMCC_SCHEDULE_WAIT_MS=2000 SYMCC_SCHEDULE_PREFIX=%t.reader-first.prefix SYMCC_SCHEDULE_TRACE=%t.reader-first.trace %t
// RUN: env LD_PRELOAD=%schedrt SYMCC_DPOR=1 SYMCC_SCHEDULE_MEMORY=1 SYMCC_SCHEDULE_ATOMIC_COMMIT=1 SYMCC_SCHEDULE_WAIT_MS=2000 SYMCC_SCHEDULE_PREFIX=%t.writer-first.prefix SYMCC_SCHEDULE_TRACE=%t.writer-first.trace %t
// RUN: python3 -c "rows=[l.split() for l in open(r'%t.reader-first.trace')]; tags=lambda r:set(r[4:]); commits=[r for r in rows if r[2]=='atomic_commit']; values=[r for r in rows if r[2]=='atomic_value']; assert sum('advanced=1' in tags(r) for r in commits)==2, commits; assert not any(r[2] in {'fallback','atomic_pending_conflict'} or (r[2]=='atomic_commit' and 'mismatch=1' in tags(r)) for r in rows); assert any(r[2]=='atomic_value' and 'role=read' in tags(r) and 'value=0x0' in tags(r) for r in values), values"
// RUN: python3 -c "rows=[l.split() for l in open(r'%t.writer-first.trace')]; tags=lambda r:set(r[4:]); commits=[r for r in rows if r[2]=='atomic_commit']; values=[r for r in rows if r[2]=='atomic_value']; assert sum('advanced=1' in tags(r) for r in commits)==2, commits; assert not any(r[2] in {'fallback','atomic_pending_conflict'} or (r[2]=='atomic_commit' and 'mismatch=1' in tags(r)) for r in rows); assert any(r[2]=='atomic_value' and 'role=read' in tags(r) and 'value=0x1' in tags(r) for r in values), values"

#include <pthread.h>
#include <stdatomic.h>

static _Atomic int shared_value;
static int observed_value;

static void *writer(void *unused) {
  (void)unused;
  atomic_store_explicit(&shared_value, 1, memory_order_seq_cst);
  return 0;
}

static void *reader(void *unused) {
  (void)unused;
  observed_value = atomic_load_explicit(
      &shared_value, memory_order_seq_cst);
  return 0;
}

int main(void) {
  pthread_t write_thread;
  pthread_t read_thread;
  if (pthread_create(&write_thread, 0, writer, 0) != 0)
    return 1;
  if (pthread_create(&read_thread, 0, reader, 0) != 0)
    return 2;
  if (pthread_join(write_thread, 0) != 0)
    return 3;
  if (pthread_join(read_thread, 0) != 0)
    return 4;
  return observed_value < 0;
}
