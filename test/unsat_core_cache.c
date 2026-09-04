// REQUIRES: qsym
// RUN: %symcc %s -o %t
// RUN: rm -rf %t-out && mkdir %t-out
// RUN: rm -f %t.json %t.poly
// RUN: echo -ne "\x01\x01" | env SYMCC_OUTPUT_DIR=%t-out SYMCC_TELEMETRY_OUT=%t.json SYMCC_POLY_CACHE=%t.poly SYMCC_UNSAT_CORE_CACHE=1 %t
// RUN: python3 -c "import json; d=json.load(open(r'%t.json')); assert d['linear_subsumption_prunes'] >= 1 and d['unsat_core_entries'] >= 1 and d['unsat_core_clauses'] >= 1 and d['unsat_core_hits'] >= 1 and d['unsat_core_unification_hits'] >= 1"

#include <stdint.h>
#include <unistd.h>

int main(void) {
  uint8_t values[2] = {0, 0};
  if (read(0, values, 2) != 2)
    return 0;
  if (values[0] == 1) {
    if (values[0] == 2)
      return 1;
  }
  if (values[1] == 1) {
    if (values[1] == 2)
      return 2;
  }
  return 0;
}
