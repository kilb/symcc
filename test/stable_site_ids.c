// REQUIRES: qsym
// RUN: rm -rf %t-out && mkdir %t-out
// RUN: rm -f %t-one.dist %t-two.dist %t.json %t.map
// RUN: env SYMCC_COLOR_TARGETS=target SYMCC_COLORATION_OUT=%t-one.dist %symcc -g -O0 %s -o %t-one
// RUN: env SYMCC_COLOR_TARGETS=target SYMCC_COLORATION_OUT=%t-two.dist %symcc -g -O0 %s -o %t-two
// RUN: %python -c "def ids(p): return sorted(int(line.split()[1]) for line in open(p) if line.startswith('#SITE ')); a=ids(r'%t-one.dist'); b=ids(r'%t-two.dist'); assert a and a == b, (a,b)"
// RUN: printf "\0" | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t.map SYMCC_TELEMETRY_OUT=%t.json %t-one
// RUN: %python -c "import json; static={int(line.split()[1]) for line in open(r'%t-one.dist') if line.startswith('#SITE ')}; dynamic={row[3] for row in json.load(open(r'%t.json'))['branch_trace']}; assert dynamic and dynamic <= static, (dynamic,static)"

#include <unistd.h>

static int target(unsigned char value) {
  if (value == 7)
    _exit(7);
  return 0;
}

int main(void) {
  unsigned char value = 0;
  if (read(STDIN_FILENO, &value, 1) != 1)
    return 1;
  return target(value);
}
