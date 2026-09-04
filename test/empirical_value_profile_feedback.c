// REQUIRES: qsym
// RUN: rm -rf %t-out* && mkdir %t-out-zero %t-out-one %t-out-solve-zero %t-out-solve-one
// RUN: %symcc -O0 %s -o %t
// RUN: printf '\000' | env SYMCC_OUTPUT_DIR=%t-out-zero SYMCC_TELEMETRY_OUT=%t.zero.json SYMCC_VALUE_PROFILE=1 SYMCC_VALUE_PROFILE_CONTEXT=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee %t
// RUN: printf '\001' | env SYMCC_OUTPUT_DIR=%t-out-one SYMCC_TELEMETRY_OUT=%t.one.json SYMCC_VALUE_PROFILE=1 SYMCC_VALUE_PROFILE_CONTEXT=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee %t
// RUN: %python %S/../util/empirical_value_profile.py %t.zero.json %t.one.json --min-observations 2 --max-distinct-values 2 --output %t.profile.json --runtime-output %t.runtime
// RUN: printf '\000' | env SYMCC_OUTPUT_DIR=%t-out-solve-zero SYMCC_TELEMETRY_OUT=%t.solve-zero.json SYMCC_VALUE_PROFILE_IN=%t.runtime SYMCC_VALUE_PROFILE_CONTEXT=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee %t
// RUN: printf '\001' | env SYMCC_OUTPUT_DIR=%t-out-solve-one SYMCC_TELEMETRY_OUT=%t.solve-one.json SYMCC_VALUE_PROFILE_IN=%t.runtime SYMCC_VALUE_PROFILE_CONTEXT=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee %t
// RUN: %python -c "import json; ds=[json.load(open(p)) for p in [r'%t.solve-zero.json',r'%t.solve-one.json']]; rows=[r for d in ds for r in d['empirical_domain_feedback'] if r[2] == [1,2]]; assert rows, ds; assert all(len(r) == 12 and r[3] == r[4] + r[5] and r[5] == r[6] + r[9] + r[10] and r[6] == r[7] + r[8] for r in rows), rows; assert sum(r[5] for r in rows) >= 2, rows; assert sum(r[9] for r in rows) >= 2, rows; assert sum(r[7] for r in rows) == 0, rows; assert sum(r[11] for r in rows) > 0, rows"

#include <stdint.h>
#include <unistd.h>

static volatile unsigned sink;

int main(void) {
  uint8_t input = 0;
  if (read(STDIN_FILENO, &input, 1) != 1)
    return 1;

  if (input & 1)
    sink += 1;
  else
    sink += 2;

  uint32_t normalized = (input & 1) + 1;
  if (normalized == 1)
    sink += 4;
  return 0;
}
