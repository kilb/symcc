// REQUIRES: qsym
// RUN: %symcc -O0 %s -o %t
// RUN: rm -rf %t-left %t-right %t-disabled && mkdir %t-left %t-right %t-disabled
// RUN: rm -f %t-left.map %t-right.map %t-disabled.map %t-left.json %t-right.json %t-disabled.json %t.cache
// RUN: printf "\0\0\0\0" | env SYMCC_OUTPUT_DIR=%t-left SYMCC_AFL_COVERAGE_MAP=%t-left.map SYMCC_TELEMETRY_OUT=%t-left.json SYMCC_POLY_CACHE=%t.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_FIELD_RENAMING=1 SYMCC_POLY_SAMPLES=2 %t
// RUN: printf "\0\0\0\0" | env SYMCC_OUTPUT_DIR=%t-right SYMCC_AFL_COVERAGE_MAP=%t-right.map SYMCC_TELEMETRY_OUT=%t-right.json SYMCC_POLY_CACHE=%t.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_FIELD_RENAMING=1 SYMCC_POLY_RENAME_VARS=6 SYMCC_POLY_RENAME_ATTEMPTS=128 SYMCC_POLY_CROSS_PREFIX_PROBES=32 SYMCC_POLY_SAMPLES=2 %t shifted
// RUN: %python -c "from pathlib import Path; values=[p.read_bytes() for p in Path(r'%t-right').iterdir() if p.is_file()]; assert any(v == b'\0\0\x01\0' for v in values), values"
// RUN: %python -c "import json; data=json.load(open(r'%t-right.json')); assert data['poly_renaming_attempts'] >= 1; assert data['poly_renaming_relations'] >= 1; assert data['poly_renaming_hits'] >= 1; assert data['poly_cross_prefix_hits'] >= 1"
// RUN: printf "\0\0\0\0" | env SYMCC_OUTPUT_DIR=%t-disabled SYMCC_AFL_COVERAGE_MAP=%t-disabled.map SYMCC_TELEMETRY_OUT=%t-disabled.json SYMCC_POLY_CACHE=%t.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_FIELD_RENAMING=0 SYMCC_POLY_CROSS_PREFIX_PROBES=32 %t shifted
// RUN: %python -c "import json; data=json.load(open(r'%t-disabled.json')); assert data['poly_renaming_attempts'] == 0; assert data['poly_renaming_hits'] == 0"

#include <unistd.h>

int main(int argc, char **argv) {
  (void)argv;
  unsigned char values[4] = {0, 0, 0, 0};
  if (read(STDIN_FILENO, values, sizeof(values)) != sizeof(values))
    return 1;
  if (argc == 1) {
    if (values[0] <= 2 && values[1] <= 2 && values[0] == 1)
      return 7;
  } else {
    if (values[2] <= 3 && values[3] <= 3 && values[2] == 1)
      return 8;
  }
  return 0;
}
