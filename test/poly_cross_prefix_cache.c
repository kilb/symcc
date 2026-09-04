// REQUIRES: qsym
// RUN: %symcc -O0 %s -o %t
// RUN: rm -rf %t-first %t-second && mkdir %t-first %t-second
// RUN: rm -f %t-first.map %t-second.map %t-first.json %t-second.json %t.cache
// RUN: printf "\0" | env SYMCC_OUTPUT_DIR=%t-first SYMCC_AFL_COVERAGE_MAP=%t-first.map SYMCC_TELEMETRY_OUT=%t-first.json SYMCC_POLY_CACHE=%t.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_SAMPLES=2 %t
// RUN: printf "\0" | env SYMCC_OUTPUT_DIR=%t-second SYMCC_AFL_COVERAGE_MAP=%t-second.map SYMCC_TELEMETRY_OUT=%t-second.json SYMCC_POLY_CACHE=%t.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_CROSS_PREFIX_PROBES=16 SYMCC_POLY_SAMPLES=2 %t wider-prefix
// RUN: %python -c "from pathlib import Path; values=[p.read_bytes() for p in Path(r'%t-second').iterdir() if p.is_file()]; assert any(v == b'\x01' for v in values), values"
// RUN: %python -c "import json; first=json.load(open(r'%t-first.json')); second=json.load(open(r'%t-second.json')); assert first['poly_sample_validations'] >= 1; assert second['poly_cross_prefix_relations'] >= 1; assert second['poly_cross_prefix_probes'] >= 1; assert second['poly_cross_prefix_hits'] >= 1"

#include <unistd.h>

int main(int argc, char **argv) {
  (void)argv;
  unsigned char value = 0;
  unsigned char limit = argc > 1 ? 3 : 2;
  if (read(STDIN_FILENO, &value, 1) != 1)
    return 1;
  if (value <= limit && value == 1)
    return 7;
  return 0;
}
