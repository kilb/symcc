// RUN: env SYMCC_DPOR_MEMORY=1 %symcc -O0 %s -o %t -pthread
// RUN: rm -f %t.trace
// RUN: env LD_PRELOAD=%schedrt SYMCC_DPOR=1 SYMCC_SCHEDULE_TRACE=%t.trace SYMCC_SCHEDULE_MEMORY=1 SYMCC_SCHEDULE_MEMORY_BYTES=4 %t
// RUN: python3 -c "rows=[l.split() for l in open(r'%t.trace')]; assert any(len(r)>=4 and r[2]=='write' for r in rows); assert any(len(r)>=4 and r[2]=='read' for r in rows)"
// RUN: python3 -c "rows=[l.split() for l in open(r'%t.trace')]; assert any(len(r)>=4 and r[2]=='action' for r in rows); constraints=[r for r in rows if len(r)>=7 and r[2]=='constraint']; assert constraints; assert any(any(t.startswith('outcome=') for t in r[4:]) and any(t.startswith('next=') for t in r[4:]) and any(t.startswith('last-read=') for t in r[4:]) for r in constraints)"

static int shared_value;
static volatile int branch_sink;

__attribute__((noinline)) static int read_shared(void) {
  return shared_value;
}

int main(void) {
  shared_value = 7;
  if (read_shared() == 7) {
    branch_sink = 1;
    return 0;
  }
  branch_sink = 2;
  return 1;
}
