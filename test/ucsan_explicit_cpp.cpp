// RUN: env SYMCC_UCSAN_CONFIG=%S/ucsan_explicit_cpp.yaml SYMCC_UCSAN_SYMBOLIZE=0 SYMCC_REGULAR_LIBCXX=1 %symxx -O0 %s -o %t
// RUN: %python %S/../util/ucsan_seed.py create %t.good --root 0=00000000
// RUN: env SYMCC_UCSAN_INPUT=%t.good SYMCC_UCSAN_SYMBOLIZE=0 %t
// RUN: %python %S/../util/ucsan_seed.py create %t.oob --root 0=01000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.oob SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=OOB
// RUN: %python %S/../util/ucsan_seed.py create %t.uaf --root 0=02000000
// RUN: not env SYMCC_UCSAN_INPUT=%t.uaf SYMCC_UCSAN_SYMBOLIZE=0 %t 2>&1 | FileCheck %s --check-prefix=UAF
// RUN: env SYMCC_UCSAN_CONFIG=%S/ucsan_explicit_cpp.yaml SYMCC_REGULAR_LIBCXX=1 %symxx -O0 -S -emit-llvm %s -o - | FileCheck %s --check-prefix=IR

extern "C" __attribute__((noinline)) void exercise_cpp(unsigned mode) {
  int *values = new int[2];
  values[0] = 1;
  values[1] = 2;
  if (mode == 0) {
    delete[] values;
    return;
  }
  if (mode == 1) {
    values[2] = 3;
    return;
  }
  int *alias = values;
  delete[] values;
  alias[0] = 4;
}

// OOB: SymCC UCSan: explicit object violation: out-of-bounds access
// UAF: SymCC UCSan: explicit object violation: use-after-free
// IR: call i64 @_sym_ucsan_register_explicit
// IR: call void @_sym_ucsan_release_explicit
