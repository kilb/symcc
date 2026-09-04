// RUN: clang -O0 -fPIC -shared -DSCHEDULE_FILTER_DSO %s -o %t-lib.so -pthread
// RUN: clang -O0 %s %t-lib.so -Wl,-rpath,%T -o %t -pthread
// RUN: rm -f %t-default.trace %t-allow.trace
// RUN: env LD_PRELOAD=%schedrt SYMCC_DPOR=1 SYMCC_SCHEDULE_TRACE=%t-default.trace %t
// RUN: env LD_PRELOAD=%schedrt SYMCC_DPOR=1 SYMCC_SCHEDULE_TRACE=%t-allow.trace SYMCC_SCHEDULE_MODULES=%t-lib.so %t
// RUN: python3 -c "rows=[l.split() for l in open(r'%t-default.trace')]; ops=[r[2] for r in rows]; assert (ops.count('lock'),ops.count('acquire'),ops.count('unlock')) == (1,1,1), ops"
// RUN: python3 -c "rows=[l.split() for l in open(r'%t-allow.trace')]; ops=[r[2] for r in rows]; assert (ops.count('lock'),ops.count('acquire'),ops.count('unlock')) == (2,2,2), ops"

#include <pthread.h>

#ifdef SCHEDULE_FILTER_DSO

static pthread_mutex_t library_lock = PTHREAD_MUTEX_INITIALIZER;

void schedule_filter_library_call(void) {
  pthread_mutex_lock(&library_lock);
  pthread_mutex_unlock(&library_lock);
}

#else

extern void schedule_filter_library_call(void);
static pthread_mutex_t main_lock = PTHREAD_MUTEX_INITIALIZER;

int main(void) {
  pthread_mutex_lock(&main_lock);
  pthread_mutex_unlock(&main_lock);
  schedule_filter_library_call();
  return 0;
}

#endif
