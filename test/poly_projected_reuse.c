// REQUIRES: qsym
// RUN: %symcc -O0 %s -o %t
// RUN: rm -rf %t-long %t-short && mkdir %t-long %t-short
// RUN: rm -f %t-long.map %t-short.map %t-long.json %t-short.json %t.cache
// RUN: printf "\0\0" | env SYMCC_OUTPUT_DIR=%t-long SYMCC_AFL_COVERAGE_MAP=%t-long.map SYMCC_TELEMETRY_OUT=%t-long.json SYMCC_POLY_CACHE=%t.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_PROJECTED_REUSE=1 SYMCC_POLY_SAMPLES=2 %t
// RUN: printf "\0" | env SYMCC_OUTPUT_DIR=%t-short SYMCC_AFL_COVERAGE_MAP=%t-short.map SYMCC_TELEMETRY_OUT=%t-short.json SYMCC_POLY_CACHE=%t.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_PROJECTED_REUSE=1 SYMCC_POLY_CROSS_PREFIX_PROBES=32 SYMCC_POLY_SAMPLES=2 %t
// RUN: %python -c "from pathlib import Path; values=[p.read_bytes() for p in Path(r'%t-short').iterdir() if p.is_file()]; assert any(v == b'\x01' for v in values), values"
// RUN: %python -c "import json; data=json.load(open(r'%t-short.json')); assert data['poly_projection_attempts'] >= 1; assert data['poly_projection_relations'] >= 1; assert data['poly_projection_hits'] >= 1; assert data['poly_cross_prefix_hits'] >= 1"
// RUN: rm -rf %t-disabled && mkdir %t-disabled
// RUN: printf "\0" | env SYMCC_OUTPUT_DIR=%t-disabled SYMCC_AFL_COVERAGE_MAP=%t-disabled.map SYMCC_TELEMETRY_OUT=%t-disabled.json SYMCC_POLY_CACHE=%t.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_PROJECTED_REUSE=0 SYMCC_POLY_CROSS_PREFIX_PROBES=32 %t
// RUN: %python -c "import json; data=json.load(open(r'%t-disabled.json')); assert data['poly_projection_attempts'] == 0; assert data['poly_projection_hits'] == 0"

#include <unistd.h>

int main(void) {
  unsigned char values[2] = {0, 0};
  ssize_t length = read(STDIN_FILENO, values, sizeof(values));
  if (length == 2) {
    if (values[0] <= 2 && values[1] <= 2 && values[0] == 1)
      return 7;
  } else if (length == 1) {
    if (values[0] <= 3 && values[0] == 1)
      return 8;
  }
  return 0;
}
