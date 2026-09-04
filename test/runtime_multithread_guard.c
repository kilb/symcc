// RUN: %symcc -O0 %s -o %t -pthread
// RUN: not %t 2>&1 | %filecheck %s
// ANY: SymCC runtime error: native multi-thread input/path solving is not supported

#include <pthread.h>

static void *worker(void *argument) { return argument; }

int main(void) {
  pthread_t thread;
  if (pthread_create(&thread, 0, worker, 0) != 0)
    return 1;
  return pthread_join(thread, 0);
}
