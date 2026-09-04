// REQUIRES: qsym
// RUN: rm -rf %t-out* && mkdir %t-out-one %t-out-two
// RUN: %symcc -O0 %s -o %t
// RUN: echo -ne "\x01" | env SYMCC_OUTPUT_DIR=%t-out-one SYMCC_TELEMETRY_OUT=%t.one.json SYMCC_VALUE_PROFILE=1 SYMCC_VALUE_PROFILE_CONTEXT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa %t
// RUN: echo -ne "\x02" | env SYMCC_OUTPUT_DIR=%t-out-two SYMCC_TELEMETRY_OUT=%t.two.json SYMCC_VALUE_PROFILE=1 SYMCC_VALUE_PROFILE_CONTEXT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa %t
// RUN: %python -c "import json; d=json.load(open(r'%t.one.json')); assert d['empirical_value_profile_context'] == 'a' * 64; p=d['empirical_value_profiles']; assert any(row[1] == 32 and row[2] == 8 and row[3] == 0 and row[4] == [[1, 8]] for row in p), p; assert any(row[1] == 32 and row[2] >= 9 and row[3] == 1 and len(row[4]) == 8 for row in p), p"
// RUN: %python %S/../util/empirical_value_profile.py %t.one.json %t.two.json --output %t.profile.json --runtime-output %t.runtime
// RUN: mkdir %t-out-hit %t-out-fallback %t-out-mismatch %t-out-malformed
// RUN: echo -ne "\x07" | env SYMCC_OUTPUT_DIR=%t-out-hit SYMCC_TELEMETRY_OUT=%t.hit.json SYMCC_VALUE_PROFILE_IN=%t.runtime SYMCC_VALUE_PROFILE_CONTEXT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa %t
// RUN: %python -c "import json; d=json.load(open(r'%t.hit.json')); assert d['empirical_domain_profiles_loaded'] >= 1, d; assert d['empirical_domain_attempts'] >= 1, d; assert d['empirical_domain_solver_queries'] >= 1, d; assert d['empirical_domain_sat'] >= 1, d; assert d['empirical_domain_validated'] >= 1, d; f=d['empirical_domain_feedback']; assert f, d; assert all(r[3] == r[4] + r[5] and r[5] == r[6] + r[9] + r[10] and r[6] == r[7] + r[8] for r in f), f; assert sum(r[7] for r in f) == d['empirical_domain_validated'], (f,d)"
// RUN: echo -ne "\x01" | env SYMCC_OUTPUT_DIR=%t-out-fallback SYMCC_TELEMETRY_OUT=%t.fallback.json SYMCC_VALUE_PROFILE_IN=%t.runtime SYMCC_VALUE_PROFILE_CONTEXT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa %t
// RUN: %python -c "import json; d=json.load(open(r'%t.fallback.json')); assert d['empirical_domain_attempts'] >= 1, d; assert d['empirical_domain_prefilter_rejects'] == d['empirical_domain_attempts'], d; assert d['empirical_domain_solver_queries'] == 0, d; assert d['empirical_domain_unsat_fallbacks'] >= 1, d; f=d['empirical_domain_feedback']; assert f and sum(r[4] for r in f) == d['empirical_domain_attempts'], (f,d); assert d['generated'] >= 1, d"
// RUN: echo -ne "\x07" | env SYMCC_OUTPUT_DIR=%t-out-mismatch SYMCC_TELEMETRY_OUT=%t.mismatch.json SYMCC_VALUE_PROFILE_IN=%t.runtime SYMCC_VALUE_PROFILE_CONTEXT=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb %t
// RUN: %python -c "import json; d=json.load(open(r'%t.mismatch.json')); assert d['empirical_domain_profiles_loaded'] == 0, d; assert d['empirical_domain_context_skips'] >= 1, d; assert d['empirical_domain_attempts'] == 0, d"
// RUN: cp %t.runtime %t.malformed && echo trailing-token >> %t.malformed
// RUN: echo -ne "\x07" | env SYMCC_OUTPUT_DIR=%t-out-malformed SYMCC_TELEMETRY_OUT=%t.malformed.json SYMCC_VALUE_PROFILE_IN=%t.malformed SYMCC_VALUE_PROFILE_CONTEXT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa %t
// RUN: %python -c "import json; d=json.load(open(r'%t.malformed.json')); assert d['empirical_domain_profiles_loaded'] == 0, d; assert d['empirical_domain_parse_failures'] == 1, d; assert d['empirical_domain_attempts'] == 0, d"
// RUN: echo -ne "\x01" | env SYMCC_OUTPUT_DIR=%t-out-one SYMCC_TELEMETRY_OUT=%t.invalid.json SYMCC_VALUE_PROFILE=1 SYMCC_VALUE_PROFILE_CONTEXT=invalid %t
// RUN: %python -c "import json; d=json.load(open(r'%t.invalid.json')); assert d['empirical_value_profile_context'] == ''; assert d['empirical_value_profiles'] == []"

#include <stdint.h>
#include <unistd.h>

static volatile unsigned sink;

int main(void) {
  uint8_t value = 0;
  if (read(STDIN_FILENO, &value, 1) != 1)
    return 1;
  for (unsigned iteration = 0; iteration < 8; ++iteration) {
    if (value == 7)
      sink++;
  }
  for (unsigned iteration = 0; iteration < 9; ++iteration) {
    if ((unsigned)value + iteration == 1000)
      sink++;
  }
  return 0;
}
