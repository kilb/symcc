// RUN: env SYMCC_UCSAN_CONFIG=%S/ucsan_uninitialized.yaml SYMCC_UCSAN_SYMBOLIZE=0 %symcc -O0 -Wno-uninitialized %s -o %t
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-heap --root 0=00000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-heap SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.branch --root 0=01000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.branch SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=BRANCH
// RUN: %python %S/../util/ucsan_seed.py create %t.pointer --root 0=02000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.pointer SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=POINTER
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-calloc --root 0=03000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-calloc SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.copy --root 0=04000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.copy SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=BRANCH
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-memset --root 0=05000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-memset SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.scalar-copy --root 0=06000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.scalar-copy SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=BRANCH
// RUN: %python %S/../util/ucsan_seed.py create %t.cross-scalar --root 0=07000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.cross-scalar SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=BRANCH
// RUN: %python %S/../util/ucsan_seed.py create %t.cross-pointer --root 0=08000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.cross-pointer SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=POINTER
// RUN: %python %S/../util/ucsan_seed.py create %t.realloc-growth --root 0=09000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.realloc-growth SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=BRANCH
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-realloc --root 0=0a000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-realloc SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.extent --root 0=0b000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.extent SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=EXTENT
// RUN: %python %S/../util/ucsan_seed.py create %t.store --root 0=0c000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.store SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=BRANCH
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-overwrite --root 0=0d000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-overwrite SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.phi --root 0=0e000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.phi SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=BRANCH
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-pointer-store --root 0=0f000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-pointer-store SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-atomic --root 0=10000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-atomic SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.atomic-rmw --root 0=11000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.atomic-rmw SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=BRANCH
// RUN: %python %S/../util/ucsan_seed.py create %t.cmpxchg --root 0=12000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.cmpxchg SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=BRANCH
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-cmpxchg-success --root 0=13000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-cmpxchg-success SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-cmpxchg-failure --root 0=14000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-cmpxchg-failure SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-atomic-xchg-pointer --root 0=15000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-atomic-xchg-pointer SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.external-pointer --root 0=16000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.external-pointer SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=POINTER
// RUN: %python %S/../util/ucsan_seed.py create %t.arbitrary-invalidate --root 0=17000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.arbitrary-invalidate SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=UAF
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-zero-copy --root 0=18000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-zero-copy SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.stack-branch --root 0=19000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.stack-branch SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=BRANCH
// RUN: %python %S/../util/ucsan_seed.py create %t.stack-pointer --root 0=1a000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.stack-pointer SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=POINTER
// RUN: %python %S/../util/ucsan_seed.py create %t.valid-stack --root 0=1b000000
// RUN: env SYMCC_UCSAN_INPUT=%t.valid-stack SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: not env SYMCC_UCSAN_INPUT=%t.valid-heap SYMCC_UCSAN_SYMBOLIZE=0 SYMCC_UCSAN_MAX_EXPLICIT_SHADOW_BYTES=1 %t 2>&1 | FileCheck %s --check-prefix=BUDGET
// RUN: env SYMCC_UCSAN_CONFIG=%S/ucsan_uninitialized.yaml %symcc -O0 -Wno-uninitialized -S -emit-llvm %s -o - | FileCheck %s --check-prefix=IR

#include <stdint.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

__attribute__((noinline)) int forward_int(int value) { return value + 1; }

__attribute__((noinline)) int *forward_pointer(int *value) { return value; }

extern uintptr_t _sym_ucsan_load_shadow(const void *, const void *);

__attribute__((noinline)) void unused_pointer(int *value) { (void)value; }

__attribute__((noinline)) void arbitrary_touch(int *value) { (void)value; }

__attribute__((noinline)) void exercise(unsigned mode) {
  switch (mode) {
  case 0: {
    int *value = (int *)malloc(sizeof(int));
    *value = 7;
    if (*value != 7)
      _Exit(90);
    free(value);
    return;
  }
  case 1: {
    int *value = (int *)malloc(sizeof(int));
    if (*value == 7)
      _Exit(91);
    return;
  }
  case 2: {
    int **box = (int **)malloc(sizeof(int *));
    int *value = *box;
    *value = 7;
    return;
  }
  case 3: {
    int *value = (int *)calloc(1, sizeof(int));
    if (*value != 0)
      _Exit(92);
    free(value);
    return;
  }
  case 4: {
    unsigned char *source = (unsigned char *)malloc(2);
    unsigned char *destination = (unsigned char *)malloc(2);
    source[0] = 1;
    __builtin_memcpy(destination, source, 2);
    if (destination[1] != 0)
      _Exit(93);
    return;
  }
  case 5: {
    unsigned char *value = (unsigned char *)malloc(4);
    __builtin_memset(value, 0, 4);
    if (value[2] != 0)
      _Exit(94);
    free(value);
    return;
  }
  case 6: {
    unsigned char *source = (unsigned char *)malloc(2);
    uint16_t destination;
    source[0] = 1;
    __builtin_memcpy(&destination, source, sizeof(destination));
    if (destination != 1)
      _Exit(95);
    return;
  }
  case 7: {
    int *value = (int *)malloc(sizeof(int));
    if (forward_int(*value) == 7)
      _Exit(96);
    return;
  }
  case 8: {
    int **box = (int **)malloc(sizeof(int *));
    int *value = forward_pointer(*box);
    *value = 8;
    return;
  }
  case 9: {
    unsigned char *value = (unsigned char *)malloc(4);
    __builtin_memset(value, 1, 4);
    value = (unsigned char *)realloc(value, 8);
    if (value == NULL)
      _Exit(97);
    if (value[4] != 0)
      _Exit(98);
    return;
  }
  case 10: {
    unsigned char *value = (unsigned char *)malloc(4);
    value[0] = 3;
    value = (unsigned char *)realloc(value, 2);
    if (value == NULL || value[0] != 3)
      _Exit(99);
    free(value);
    return;
  }
  case 11: {
    size_t *length = (size_t *)malloc(sizeof(size_t));
    unsigned char *value = (unsigned char *)malloc(8);
    __builtin_memset(value, 0, *length);
    return;
  }
  case 12: {
    int *source = (int *)malloc(sizeof(int));
    int *destination = (int *)malloc(sizeof(int));
    *destination = *source;
    if (*destination != 0)
      _Exit(100);
    return;
  }
  case 13: {
    int *source = (int *)malloc(sizeof(int));
    int value = *source;
    value = 5;
    if (value != 5)
      _Exit(101);
    free(source);
    return;
  }
  case 14: {
    int *source = (int *)malloc(sizeof(int));
    int value;
    if (mode == 14)
      value = *source;
    else
      value = 0;
    if (value != 0)
      _Exit(102);
    return;
  }
  case 15: {
    int *child = (int *)malloc(sizeof(int));
    int **box = (int **)malloc(sizeof(int *));
    *box = child;
    int *value = *box;
    *value = 15;
    if (*child != 15)
      _Exit(103);
    free(box);
    free(child);
    return;
  }
  case 16: {
    _Atomic int *value = (_Atomic int *)malloc(sizeof(_Atomic int));
    atomic_store(value, 16);
    if (atomic_load(value) != 16)
      _Exit(104);
    free(value);
    return;
  }
  case 17: {
    _Atomic int *value = (_Atomic int *)malloc(sizeof(_Atomic int));
    int previous = atomic_fetch_add(value, 1);
    if (previous != 0)
      _Exit(105);
    return;
  }
  case 18: {
    _Atomic int *value = (_Atomic int *)malloc(sizeof(_Atomic int));
    int expected = 0;
    if (atomic_compare_exchange_strong(value, &expected, 1))
      _Exit(106);
    return;
  }
  case 19: {
    int *first = (int *)malloc(sizeof(int));
    int *second = (int *)malloc(sizeof(int));
    _Atomic(int *) *slot = (_Atomic(int *) *)malloc(sizeof(_Atomic(int *)));
    atomic_store(slot, first);
    uintptr_t first_shadow = _sym_ucsan_load_shadow(slot, first);
    int *expected = first;
    if (!atomic_compare_exchange_strong(slot, &expected, second))
      _Exit(107);
    if (_sym_ucsan_load_shadow(slot, second) == 0 ||
        _sym_ucsan_load_shadow(slot, second) == first_shadow)
      _Exit(108);
    free(slot);
    free(second);
    free(first);
    return;
  }
  case 20: {
    int *first = (int *)malloc(sizeof(int));
    int *second = (int *)malloc(sizeof(int));
    _Atomic(int *) *slot = (_Atomic(int *) *)malloc(sizeof(_Atomic(int *)));
    atomic_store(slot, first);
    uintptr_t first_shadow = _sym_ucsan_load_shadow(slot, first);
    int *expected = second;
    if (atomic_compare_exchange_strong(slot, &expected, second))
      _Exit(109);
    if (_sym_ucsan_load_shadow(slot, first) != first_shadow)
      _Exit(110);
    free(slot);
    free(second);
    free(first);
    return;
  }
  case 21: {
    int *first = (int *)malloc(sizeof(int));
    int *second = (int *)malloc(sizeof(int));
    _Atomic(int *) *slot = (_Atomic(int *) *)malloc(sizeof(_Atomic(int *)));
    atomic_store(slot, first);
    uintptr_t first_shadow = _sym_ucsan_load_shadow(slot, first);
    int *previous = atomic_exchange(slot, second);
    if (previous != first || _sym_ucsan_load_shadow(slot, second) == 0 ||
        _sym_ucsan_load_shadow(slot, second) == first_shadow)
      _Exit(111);
    *previous = 21;
    free(slot);
    free(second);
    free(first);
    return;
  }
  case 22: {
    uintptr_t *value = (uintptr_t *)malloc(sizeof(uintptr_t));
    unused_pointer((int *)*value);
    return;
  }
  case 23: {
    int *value = (int *)malloc(sizeof(int));
    *value = 23;
    arbitrary_touch(value);
    *value = 24;
    return;
  }
  case 24: {
    unsigned char **box =
        (unsigned char **)malloc(sizeof(unsigned char *));
    unsigned char *destination = (unsigned char *)malloc(1);
    volatile size_t length = 0;
    __builtin_memcpy(destination, *box, length);
    free(destination);
    free(box);
    return;
  }
  case 25: {
    int value;
    if (value)
      _Exit(112);
    return;
  }
  case 26: {
    int *value;
    *value = 26;
    return;
  }
  case 27: {
    int value = 27;
    if (value != 27)
      _Exit(113);
    return;
  }
  default:
    return;
  }
}

// BRANCH: SymCC UCSan: explicit object violation: use-before-initialization in branch condition
// POINTER: SymCC UCSan: explicit object violation: use-before-initialization in pointer dereference
// EXTENT: SymCC UCSan: explicit object violation: use-before-initialization in memory access extent
// BUDGET: SymCC UCSan: explicit initialization shadow bytes budget exceeded
// UAF: SymCC UCSan: explicit object violation: use-after-free
// IR: call i8 @_sym_ucsan_load_uninitialized
// IR: call void @_sym_ucsan_store_uninitialized
// IR: call void @_sym_ucsan_check_initialized
// IR: call void @_sym_ucsan_set_argument_uninitialized
// IR: call i8 @_sym_ucsan_get_return_uninitialized
