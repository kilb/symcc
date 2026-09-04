// REQUIRES: qsym
// RUN: %symcc -O0 %s -o %t
// RUN: rm -rf %t-out && mkdir %t-out
// RUN: printf "\0\0" | env SYMCC_OUTPUT_DIR=%t-out SYMCC_UNSAT_CORE_CACHE=1 %t
// RUN: %python -c "from pathlib import Path; values=[p.read_bytes() for p in Path(r'%t-out').iterdir() if p.is_file()]; assert any(len(v) >= 2 and int.from_bytes(v[:2], 'little') == 1200 for v in values), values"

#include <stdint.h>
#include <unistd.h>

static volatile unsigned sink;

int main(void) {
  uint16_t value = 0;
  if (read(STDIN_FILENO, &value, sizeof(value)) != sizeof(value))
    return 1;

  for (uint16_t forbidden = 1; forbidden <= 1100; ++forbidden) {
    if (value == forbidden)
      sink++;
  }

  if (value == 1200)
    sink += 2;
  return 0;
}
