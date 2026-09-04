// RUN: env SYMCC_UCSAN_CONFIG=%S/ucsan_explicit_objects.yaml SYMCC_UCSAN_SYMBOLIZE=0 %symcc -O0 %s -o %t
// RUN: %python %S/../util/ucsan_seed.py create %t.good --root 0=00000000
// RUN: env SYMCC_UCSAN_INPUT=%t.good SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.stack-oob --root 0=01000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.stack-oob SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=OOB
// RUN: %python %S/../util/ucsan_seed.py create %t.heap-oob --root 0=02000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.heap-oob SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=OOB
// RUN: %python %S/../util/ucsan_seed.py create %t.heap-uaf --root 0=03000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.heap-uaf SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=UAF
// RUN: %python %S/../util/ucsan_seed.py create %t.stack-uar --root 0=04000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.stack-uar SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=UAF
// RUN: %python %S/../util/ucsan_seed.py create %t.double-free --root 0=05000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.double-free SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=DOUBLE
// RUN: %python %S/../util/ucsan_seed.py create %t.stack-free --root 0=06000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.stack-free SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=STACKFREE
// RUN: %python %S/../util/ucsan_seed.py create %t.lower-oob --root 0=07000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.lower-oob SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=OOB
// RUN: %python %S/../util/ucsan_seed.py create %t.calloc-oob --root 0=08000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.calloc-oob SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=OOB
// RUN: %python %S/../util/ucsan_seed.py create %t.memcpy-oob --root 0=09000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.memcpy-oob SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=OOB
// RUN: %python %S/../util/ucsan_seed.py create %t.realloc-fail --root 0=0a000000
// RUN: env SYMCC_UCSAN_INPUT=%t.realloc-fail SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.realloc-shadow --root 0=0b000000
// RUN: env SYMCC_UCSAN_INPUT=%t.realloc-shadow SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.reallocarray-overflow --root 0=0c000000
// RUN: env SYMCC_UCSAN_INPUT=%t.reallocarray-overflow SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.realloc-shadow-range --root 0=0d000000
// RUN: env SYMCC_UCSAN_INPUT=%t.realloc-shadow-range SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: env SYMCC_UCSAN_CONFIG=%S/ucsan_explicit_objects.yaml %symcc -O0 -S -emit-llvm %s -o - | FileCheck %s --check-prefix=IR

#define _GNU_SOURCE
#include <stdlib.h>
#include <stdint.h>
#include <string.h>

extern uintptr_t _sym_ucsan_load_shadow(const void *, const void *);

__attribute__((noinline)) int *escaped_stack(void) {
  int local[2] = {17, 23};
  return local;
}

__attribute__((noinline)) void exercise(unsigned mode) {
  int stack[2] = {1, 2};
  int *heap = (int *)malloc(2 * sizeof(int));
  if (heap == NULL)
    _Exit(90);
  heap[0] = 3;
  heap[1] = 4;

  switch (mode) {
  case 0:
    stack[1] += heap[1];
    heap = (int *)realloc(heap, 4 * sizeof(int));
    if (heap == NULL)
      _Exit(91);
    heap[3] = stack[1];
    int *zeroed = (int *)calloc(2, sizeof(int));
    if (zeroed == NULL)
      _Exit(92);
    zeroed[1] = heap[3];
    free(zeroed);
    free(heap);
    return;
  case 1:
    stack[2] = 9;
    break;
  case 2:
    heap[2] = 9;
    break;
  case 3: {
    int *alias = heap;
    free(heap);
    alias[0] = 9;
    return;
  }
  case 4: {
    int *escaped = escaped_stack();
    escaped[0] = 9;
    break;
  }
  case 5:
    free(heap);
    free(heap);
    return;
  case 6:
    free(stack);
    return;
  case 7:
    stack[-1] = 9;
    break;
  case 8: {
    int *small = (int *)calloc(2, sizeof(int));
    if (small == NULL)
      _Exit(93);
    small[2] = 9;
    break;
  }
  case 9:
    memcpy(heap + 1, stack, sizeof(stack));
    break;
  case 10: {
    volatile size_t huge = (size_t)-4096;
    int *replacement = (int *)realloc(heap, huge);
    if (replacement != NULL)
      _Exit(94);
    heap[0] = 10;
    free(heap);
    return;
  }
  case 11: {
    int *child = (int *)malloc(sizeof(int));
    int **box = (int **)malloc(sizeof(int *));
    if (child == NULL || box == NULL)
      _Exit(95);
    *box = child;
    uintptr_t before = _sym_ucsan_load_shadow(box, *box);
    box = (int **)realloc(box, 1024 * 1024);
    if (box == NULL)
      _Exit(96);
    uintptr_t after = _sym_ucsan_load_shadow(box, *box);
    if (before == 0 || after != before)
      _Exit(97);
    free(child);
    free(box);
    free(heap);
    return;
  }
  case 12: {
    volatile size_t count = (size_t)-1 / sizeof(int) + 1;
    int *replacement = (int *)reallocarray(heap, count, sizeof(int));
    if (replacement != NULL)
      _Exit(98);
    heap[1] = 12;
    free(heap);
    return;
  }
  case 13: {
    int *child = (int *)malloc(sizeof(int));
    int **box = (int **)malloc(2 * sizeof(int *));
    if (child == NULL || box == NULL)
      _Exit(99);
    box[1] = child;
    if (_sym_ucsan_load_shadow(box + 1, child) == 0)
      _Exit(100);
    int **replacement = (int **)realloc(box, sizeof(int *));
    if (replacement == NULL)
      _Exit(101);
    if (_sym_ucsan_load_shadow(replacement + 1, child) != 0)
      _Exit(102);
    free(child);
    free(replacement);
    free(heap);
    return;
  }
  default:
    break;
  }
  free(heap);
}

// OOB: SymCC UCSan: explicit object violation: out-of-bounds access
// UAF: SymCC UCSan: explicit object violation: use-after-free
// DOUBLE: SymCC UCSan: explicit object violation: double free
// STACKFREE: SymCC UCSan: explicit object violation: deallocation of a stack object
// IR: call i64 @_sym_ucsan_push_frame
// IR: call i64 @_sym_ucsan_register_explicit
// IR: call void @_sym_ucsan_validate_reallocate
// IR: call i64 @_sym_ucsan_reallocate_explicit
// IR: call void @_sym_ucsan_release_explicit
// IR: call void @_sym_ucsan_pop_frame
