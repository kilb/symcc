// RUN: env SYMCC_DPOR_MEMORY=1 %symcc -O0 %s -o %t -pthread
// RUN: rm -f %t.trace
// RUN: env LD_PRELOAD=%schedrt SYMCC_DPOR=1 SYMCC_SCHEDULE_TRACE=%t.trace SYMCC_SCHEDULE_MEMORY=1 SYMCC_SCHEDULE_ATOMIC_COMMIT=1 %t
// RUN: python3 -c "rows=[l.split() for l in open(r'%t.trace')]; tags=lambda r:set(r[4:]); source=[r for r in rows if any(t.startswith('kind=') for t in tags(r))]; values=[r for r in rows if r[2]=='atomic_value']; commits=[r for r in rows if r[2]=='atomic_commit']; assert len(source)==6, source; assert len(values)==8, values; assert len(commits)==6, commits; assert all('mode=1' in tags(r) and 'mismatch=0' in tags(r) for r in commits); assert sum(r[2]=='atomic_result' for r in rows)==1; assert any(r[2]=='read' and 'kind=load' in tags(r) and 'mo=acquire' in tags(r) for r in rows); assert any(r[2]=='write' and 'kind=store' in tags(r) and 'mo=release' in tags(r) for r in rows); assert any(r[2]=='rmw' and 'kind=rmw' in tags(r) and 'mo=acq_rel' in tags(r) for r in rows); assert any(r[2]=='rmw' and 'kind=cmpxchg' in tags(r) and 'mo=seq_cst' in tags(r) for r in rows); assert any(r[2]=='atomic_result' and 'success=1' in tags(r) for r in rows); assert any('role=write' in tags(r) and 'value=0x1' in tags(r) for r in values); assert any('role=desired' in tags(r) and 'value=0x9' in tags(r) for r in values); assert any('role=read' in tags(r) and 'value=0x9' in tags(r) for r in values); assert any(r[2]=='fence' and 'kind=fence' in tags(r) and 'mo=seq_cst' in tags(r) for r in rows); assert not ({'lock','trylock','acquire','unlock','rdlock','wrlock','rwunlock','wait','signal','broadcast'} & {r[2] for r in rows})"

#include <stdatomic.h>

static atomic_int shared_value;

int main(void) {
  atomic_store_explicit(&shared_value, 1, memory_order_release);
  int value = atomic_load_explicit(&shared_value, memory_order_acquire);
  value = atomic_fetch_add_explicit(
      &shared_value, value, memory_order_acq_rel);
  int expected = value + 1;
  if (!atomic_compare_exchange_strong_explicit(
          &shared_value, &expected, 9,
          memory_order_seq_cst, memory_order_acquire))
    return 1;
  atomic_thread_fence(memory_order_seq_cst);
  return atomic_load_explicit(
             &shared_value, memory_order_relaxed) == 9
             ? 0
             : 2;
}
