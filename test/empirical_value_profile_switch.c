// REQUIRES: qsym
// RUN: rm -rf %t-out* && mkdir %t-out-one %t-out-two
// RUN: %symcc -O0 %s -o %t
// RUN: printf '\001' | env SYMCC_OUTPUT_DIR=%t-out-one SYMCC_TELEMETRY_OUT=%t.one.json SYMCC_VALUE_PROFILE=1 SYMCC_VALUE_PROFILE_CONTEXT=dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd %t
// RUN: printf '\002' | env SYMCC_OUTPUT_DIR=%t-out-two SYMCC_TELEMETRY_OUT=%t.two.json SYMCC_VALUE_PROFILE=1 SYMCC_VALUE_PROFILE_CONTEXT=dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd %t
// RUN: %python %S/../util/empirical_value_profile.py %t.one.json %t.two.json --min-observations 2 --output %t.profile.json --runtime-output %t.runtime
// RUN: mkdir %t-out-solve
// RUN: printf '\001' | env SYMCC_OUTPUT_DIR=%t-out-solve SYMCC_TELEMETRY_OUT=%t.solve.json SYMCC_VALUE_PROFILE_IN=%t.runtime SYMCC_VALUE_PROFILE_CONTEXT=dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd %t
// RUN: %python -c "import json; d=json.load(open(r'%t.solve.json')); assert d['empirical_domain_profiles_loaded'] == 1, d; assert d['empirical_domain_attempts'] >= 1, d; assert d['empirical_domain_solver_queries'] >= 1, d; assert d['empirical_domain_sat'] >= 1, d; assert d['empirical_domain_validated'] >= 1, d"

#include <stdint.h>
#include <unistd.h>

static volatile unsigned sink;

int main(void) {
  uint8_t value = 0;
  if (read(STDIN_FILENO, &value, 1) != 1)
    return 1;
  switch (value) {
  case 1:
    sink += 1;
    break;
  case 2:
    sink += 2;
    break;
  case 7:
    sink += 7;
    break;
  default:
    break;
  }
  return 0;
}
