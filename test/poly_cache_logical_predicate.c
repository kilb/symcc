// REQUIRES: qsym
// RUN: %symcc -O1 %s -o %t
// RUN: rm -rf %t-out && mkdir %t-out
// RUN: rm -f %t.map %t.telemetry.json %t.cache
// RUN: printf "\0\0" | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t.map SYMCC_TELEMETRY_OUT=%t.telemetry.json SYMCC_POLY_CACHE=%t.cache %t
// RUN: %python -c "import json; d=json.load(open(r'%t.telemetry.json')); assert d['generated'] >= 1"

#include <unistd.h>

static volatile int sink;

int main(void) {
  unsigned char input[2] = {0, 0};
  if (read(STDIN_FILENO, input, sizeof(input)) != sizeof(input))
    return 1;

  if ((input[0] == 1) | (input[1] == 2))
    sink++;

  return sink;
}
