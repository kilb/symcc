// RUN: rm -f %t.conc
// RUN: env SYMCC_CONCURRENCY_OUT=%t.conc %symcc -g -O0 %s -o %t -pthread
// RUN: grep -q "symcc-concurrency-guidance-v1" %t.conc
// RUN: grep -q "#CONC .*thread_create" %t.conc
// RUN: grep -q "#CONC .*lock" %t.conc
// RUN: python3 -c "rows=[l for l in open(r'%t.conc') if l and l[0].isdigit()]; assert rows"
// RUN: env SYMCC_DPOR_SCHEDULE_ONLY=1 %symcc -g -O0 %s -o %t.schedule -pthread
// RUN: rm -f %t.trace
// RUN: env LD_PRELOAD=%schedrt SYMCC_DPOR=1 SYMCC_SCHEDULE_TRACE=%t.trace %t.schedule
// RUN: python3 -c "rows=[l.split() for l in open(r'%t.trace')]; ops=[r[2] for r in rows]; assert ops.count('create') == 1; assert ops.count('create_success') == 1; assert ops.count('lock') == 1; assert ops.count('acquire') == 1; assert ops.count('unlock') == 1; assert ops.count('join') == 1; assert ops.count('join_success') == 1; assert len(rows) <= 12"

#include <pthread.h>

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;

static void *worker(void *arg) {
  pthread_mutex_lock(&lock);
  pthread_mutex_unlock(&lock);
  return arg;
}

int main(void) {
  pthread_t thread;
  if (pthread_create(&thread, 0, worker, 0) != 0)
    return 1;
  pthread_join(thread, 0);
  return 0;
}
