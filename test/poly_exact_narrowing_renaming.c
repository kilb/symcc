// REQUIRES: qsym
// RUN: %symcc -O0 %s -o %t
// RUN: rm -rf %t-narrowed %t-disabled && mkdir %t-narrowed %t-disabled
// RUN: rm -f %t-narrowed.map %t-disabled.map %t-narrowed.json %t-disabled.json %t.base %t-narrowed.cache %t-disabled.cache
// RUN: printf "1 sat 4 0:0,1:0,2:1,3:0 - -1:-1:0=-65536,1=-16777216,2=-1,3=-256\n" > %t.base
// RUN: cp %t.base %t-narrowed.cache && cp %t.base %t-disabled.cache
// RUN: printf "\0\0" | env SYMCC_OUTPUT_DIR=%t-narrowed SYMCC_AFL_COVERAGE_MAP=%t-narrowed.map SYMCC_TELEMETRY_OUT=%t-narrowed.json SYMCC_POLY_CACHE=%t-narrowed.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_PROJECTED_REUSE=1 SYMCC_POLY_EXACT_PROJECTION=1 SYMCC_POLY_EXACT_PROJECTION_VARS=2 SYMCC_POLY_EXACT_PROJECTION_TIMEOUT=1000 SYMCC_POLY_FIELD_RENAMING=1 SYMCC_POLY_RENAME_VARS=6 SYMCC_POLY_RENAME_ATTEMPTS=128 SYMCC_POLY_RENAME_EXACT_PROBES=8 SYMCC_POLY_CROSS_PREFIX_PROBES=32 %t
// RUN: %python -c "from pathlib import Path; values=[p.read_bytes() for p in Path(r'%t-narrowed').iterdir() if p.is_file()]; assert any(v == b'\x01\0' for v in values), values"
// RUN: %python -c "import json; data=json.load(open(r'%t-narrowed.json')); assert data['poly_renaming_relations'] >= 1, data; assert data['poly_renaming_hits'] >= 1, data; assert data['poly_exact_projection_relations'] >= 1, data; assert data['poly_exact_projection_hits'] >= 1, data"
// RUN: printf "\0\0" | env SYMCC_OUTPUT_DIR=%t-disabled SYMCC_AFL_COVERAGE_MAP=%t-disabled.map SYMCC_TELEMETRY_OUT=%t-disabled.json SYMCC_POLY_CACHE=%t-disabled.cache SYMCC_POLY_CROSS_PREFIX=1 SYMCC_POLY_PROJECTED_REUSE=1 SYMCC_POLY_EXACT_PROJECTION=1 SYMCC_POLY_FIELD_RENAMING=0 SYMCC_POLY_CROSS_PREFIX_PROBES=32 %t
// RUN: %python -c "import json; data=json.load(open(r'%t-disabled.json')); assert data['poly_renaming_attempts'] == 0, data; assert data['poly_renaming_hits'] == 0, data"

#include <stdint.h>
#include <string.h>
#include <unistd.h>

int main(void) {
  unsigned char values[2] = {0, 0};
  if (read(STDIN_FILENO, values, sizeof(values)) != sizeof(values))
    return 1;
  uint16_t little_endian = 0;
  memcpy(&little_endian, values, sizeof(little_endian));
  if (little_endian == 1)
    return 8;
  return 0;
}
