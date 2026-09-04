// REQUIRES: qsym
// RUN: rm -f %t.dist %t.json
// RUN: env SYMCC_COLOR_TARGETS=goal SYMCC_COLORATION_OUT=%t.dist %symcc -g -O0 %s -o %t.colored
// RUN: python3 -c "vals=[float(l.split()[1]) for l in open(r'%t.dist') if l.strip() and not l.startswith('#')]; assert vals and 0.0 in vals and max(vals) > 0.0"
// RUN: mkdir -p %t-out
// RUN: echo -ne "\x00" | env SYMCC_OUTPUT_DIR=%t-out SYMCC_TELEMETRY_OUT=%t.json SYMCC_DIRECTED_DISTANCE=%t.dist SYMCC_DIRECTED_PRUNE=1 SYMCC_DIRECTED_MAX_DISTANCE=0 %t.colored
// RUN: python3 -c "import json; d=json.load(open(r'%t.json')); assert d['directed_pruned_branches'] >= 1; assert d['skipped_branches'] >= d['directed_pruned_branches']"

#include <stdint.h>
#include <unistd.h>

static int goal(uint8_t x) {
  if (x == 7)
    return 1;
  return 0;
}

int main(void) {
  uint8_t value = 0;
  if (read(STDIN_FILENO, &value, sizeof(value)) != sizeof(value))
    return 1;
  if (value == 'A')
    return goal(value);
  if (value == 'B')
    return 2;
  return 0;
}
