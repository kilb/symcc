// RUN: env SYMCC_UCSAN_CONFIG=%S/ucsan_jit.yaml SYMCC_UCSAN_SYMBOLIZE=0 %symcc -O0 %s -o %t
// RUN: %python %S/../util/ucsan_seed.py create %t.seed --root 0=0100000000000000 --object 0@-8=280000000000000002000000000000000000000000000000
// RUN: %python %S/../util/expect_exit.py 42 SYMCC_UCSAN_INPUT=%t.seed SYMCC_UCSAN_SYMBOLIZE=0 -- %t
// RUN: env SYMCC_UCSAN_CONFIG=%S/ucsan_jit.yaml %symcc -O0 -S -emit-llvm %s -o - | FileCheck %s

#include <stdlib.h>
#include <stddef.h>

typedef struct Node {
  unsigned value;
  struct Node *next;
} Node;

typedef struct Outer {
  unsigned prefix;
  Node node;
} Outer;

void cal(Node *node) {
  if (node == 0)
    _Exit(7);
  Outer *outer = (Outer *)((char *)node - offsetof(Outer, node));
  node->next = node;
  _Exit((int)(outer->prefix + node->next->value));
}

int main(void) { return 99; }

// CHECK-LABEL: define {{.*}}@cal
// CHECK-DAG: call i64 @_sym_ucsan_get_argument_shadow
// CHECK-DAG: call ptr @_sym_ucsan_check
// CHECK-DAG: call void @_sym_ucsan_store_shadow
// CHECK-LABEL: define i32 @main()
// CHECK: call void @_sym_ucsan_root_value
