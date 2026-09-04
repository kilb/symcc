// RUN: env SYMCC_UCSAN_CONFIG=%S/ucsan_explicit_unwind.yaml SYMCC_UCSAN_SYMBOLIZE=0 SYMCC_REGULAR_LIBCXX=1 %symxx -O0 %s -o %t
// RUN: %python %S/../util/ucsan_seed.py create %t.cleanup --root 0=00000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.cleanup SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=UAF
// RUN: %python %S/../util/ucsan_seed.py create %t.naked --root 0=01000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.naked SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=UAF
// RUN: env SYMCC_UCSAN_CONFIG=%S/ucsan_explicit_unwind.yaml SYMCC_REGULAR_LIBCXX=1 %symxx -O0 -S -emit-llvm %s -o - | FileCheck %s --check-prefix=IR

static int *escaped;

struct Cleanup {
  __attribute__((noinline)) ~Cleanup() {}
};

extern "C" __attribute__((noinline)) void cleanup_thrower() {
  Cleanup cleanup;
  int local[2] = {1, 2};
  escaped = local;
  throw 7;
}

extern "C" __attribute__((noinline)) void naked_thrower() {
  int local[2] = {3, 4};
  escaped = local;
  throw 9;
}

extern "C" __attribute__((noinline)) void exercise_exception(unsigned mode) {
  try {
    if (mode == 0)
      cleanup_thrower();
    else
      naked_thrower();
  } catch (...) {
    escaped[0] = 11;
  }
}

// UAF: SymCC UCSan: explicit object violation: use-after-free
// IR: call void @_sym_ucsan_pop_frame
// IR: resume
