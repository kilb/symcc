// RUN: not env SYMCC_UCSAN_ENTRY=missing %symcc -O0 %s -o %t-missing 2>&1 | FileCheck %s --check-prefix=MISSING
// RUN: not env SYMCC_UCSAN_ENTRY=variadic %symcc -O0 %s -o %t-variadic 2>&1 | FileCheck %s --check-prefix=VARIADIC

int variadic(int first, ...) { return first; }
int main(void) { return 0; }

// MISSING: configured entry function was not found: missing
// VARIADIC: variadic entry functions are unsupported: variadic
