// REQUIRES: qsym
// RUN: %symcc -O0 %s -o %t
// RUN: rm -rf %t-exact %t-interval && mkdir %t-exact %t-interval
// RUN: rm -f %t-exact.map %t-interval.map %t-exact.json %t-interval.json %t.base %t-exact.cache %t-interval.cache
// RUN: printf "1 sat 2 0:2,1:1 - 0:0:0=1,1=-2\n" > %t.base
// RUN: cp %t.base %t-exact.cache && cp %t.base %t-interval.cache
// RUN: printf "\0" | env SYMCC_OUTPUT_DIR=%t-exact SYMCC_AFL_COVERAGE_MAP=%t-exact.map SYMCC_TELEMETRY_OUT=%t-exact.json SYMCC_POLY_CACHE=%t-exact.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_PROJECTED_REUSE=1 SYMCC_POLY_EXACT_PROJECTION=1 SYMCC_POLY_EXACT_PROJECTION_VARS=2 SYMCC_POLY_EXACT_PROJECTION_ROWS=32 SYMCC_POLY_EXACT_PROJECTION_TIMEOUT=100 SYMCC_POLY_CROSS_PREFIX_PROBES=32 %t
// RUN: %python -c "import json; data=json.load(open(r'%t-exact.json')); assert data['poly_exact_projection_attempts'] >= 1, data; assert data['poly_exact_projection_relations'] == 0, data; assert data['poly_cross_prefix_validation_failures'] == 0, data"
// RUN: printf "\0" | env SYMCC_OUTPUT_DIR=%t-interval SYMCC_AFL_COVERAGE_MAP=%t-interval.map SYMCC_TELEMETRY_OUT=%t-interval.json SYMCC_POLY_CACHE=%t-interval.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_PROJECTED_REUSE=1 SYMCC_POLY_EXACT_PROJECTION=0 SYMCC_POLY_CROSS_PREFIX_PROBES=32 %t
// RUN: %python -c "import json; data=json.load(open(r'%t-interval.json')); assert data['poly_projection_relations'] >= 1, data; assert data['poly_cross_prefix_validation_failures'] >= 1, data"

#include <unistd.h>

int main(void) {
  unsigned char value = 0;
  if (read(STDIN_FILENO, &value, sizeof(value)) != sizeof(value))
    return 1;
  if (value == 1)
    return 8;
  return 0;
}
