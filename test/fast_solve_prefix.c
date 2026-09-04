// REQUIRES: qsym
// RUN: %symcc -O0 %s -o %t
// RUN: rm -rf %t-out && mkdir %t-out
// RUN: rm -f %t.map %t.json
// RUN: printf "\5" | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t.map SYMCC_FAST_SOLVE=1 SYMCC_TELEMETRY_OUT=%t.json %t
// RUN: %python -c "from pathlib import Path; values=[p.read_bytes() for p in Path(r'%t-out').iterdir() if p.is_file()]; assert any(v not in {b'\x04',b'\x05'} for v in values), values"
// RUN: %python -c "import json; assert json.load(open(r'%t.json'))['z3_solves'] >= 1"

#include <unistd.h>

static volatile unsigned sink;

int main(void) {
  unsigned char value = 0;
  if (read(STDIN_FILENO, &value, 1) != 1)
    return 1;
  if (value != 4 && value == 5)
    sink++;
  return 0;
}
