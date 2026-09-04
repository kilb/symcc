// REQUIRES: qsym
// RUN: rm -rf %t-out* && mkdir %t-out-neg %t-out-pos
// RUN: %symcc -O0 %s -o %t
// RUN: printf '\377' | env SYMCC_OUTPUT_DIR=%t-out-neg SYMCC_TELEMETRY_OUT=%t.neg.json SYMCC_VALUE_PROFILE=1 SYMCC_VALUE_PROFILE_CONTEXT=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc %t
// RUN: printf '\001' | env SYMCC_OUTPUT_DIR=%t-out-pos SYMCC_TELEMETRY_OUT=%t.pos.json SYMCC_VALUE_PROFILE=1 SYMCC_VALUE_PROFILE_CONTEXT=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc %t
// RUN: %python %S/../util/empirical_value_profile.py %t.neg.json %t.pos.json --min-observations 2 --output %t.profile.json --runtime-output %t.runtime
// RUN: mkdir %t-out-solve
// RUN: printf '\005' | env SYMCC_OUTPUT_DIR=%t-out-solve SYMCC_TELEMETRY_OUT=%t.solve.json SYMCC_VALUE_PROFILE_IN=%t.runtime SYMCC_VALUE_PROFILE_CONTEXT=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc %t
// RUN: %python -c "import json; d=json.load(open(r'%t.solve.json')); assert d['empirical_domain_profiles_loaded'] >= 1, d; assert d['empirical_domain_attempts'] == 1, d; assert d['empirical_domain_solver_queries'] == 1, d; assert d['empirical_domain_sat'] == 1, d; assert d['empirical_domain_validated'] == 1, d; assert d['empirical_domain_unsat_fallbacks'] == 0, d"

#include <stdint.h>
#include <unistd.h>

static volatile unsigned sink;

int main(void) {
  uint8_t raw = 0;
  if (read(STDIN_FILENO, &raw, 1) != 1)
    return 1;
  int32_t value = (int8_t)raw;
  if (value < 0)
    sink++;
  return 0;
}
