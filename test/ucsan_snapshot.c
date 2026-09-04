// RUN: env SYMCC_UCSAN_CONFIG=%S/ucsan_jit.yaml SYMCC_UCSAN_SYMBOLIZE=0 %symcc -O0 %s -o %t
// RUN: %python %S/../util/ucsan_seed.py create %t.seed --root 0=0100000000000000 --object 0=2a000000000000000100000000000000 --alias 0/8=1
// RUN: env SYMCC_UCSAN_INPUT=%t.seed SYMCC_UCSAN_DUMP=%t.dump1 SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py verify %t.dump1 --require-canonical | FileCheck %s --check-prefix=VERIFY
// RUN: env SYMCC_UCSAN_INPUT=%t.dump1 SYMCC_UCSAN_DUMP=%t.dump2 SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: cmp %t.dump1 %t.dump2
// RUN: %python -c "from pathlib import Path; p=Path(r'%t.bad'); p.write_bytes(Path(r'%t.dump1').read_bytes()+b'x')"
// RUN: not env SYMCC_UCSAN_INPUT=%t.bad SYMCC_UCSAN_DUMP=%t.bad.dump SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=INVALID
// RUN: %python %S/../util/ucsan_seed.py create %t.objects --root 0=0100000000000000 --object 0=2a000000000000000100000000000000 --alias 0/8=2
// RUN: not env SYMCC_UCSAN_INPUT=%t.objects SYMCC_UCSAN_DUMP=%t.limit.dump SYMCC_UCSAN_MAX_OBJECTS=1 SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=LIMIT

#include <stdint.h>
#include <stdlib.h>

typedef struct Node {
  uint32_t value;
  uint32_t padding;
  struct Node *next;
} Node;

extern int _sym_ucsan_snapshot(void);

int cal(Node *node) {
  if (node == 0 || node->next == 0)
    return 1;
  uint32_t observed = node->value + node->next->value;
  node->value = 43;
  if (_sym_ucsan_snapshot() != 0)
    abort();
  return observed == 84 || observed == 86 ? 0 : 2;
}

int main(void) { return 99; }

// VERIFY: "canonical": true
// VERIFY-SAME: "entries": 3
// VERIFY-SAME: "materialized_object_bytes": 16
// VERIFY-SAME: "objects": 1
// VERIFY-SAME: "paths": 2
// INVALID: SymCC UCSan: invalid structured seed: trailing bytes
// LIMIT: SymCC UCSan: invalid structured seed: object or shadow count budget exceeded
