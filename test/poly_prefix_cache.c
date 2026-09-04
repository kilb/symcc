// REQUIRES: qsym
// RUN: %symcc -O0 %s -o %t
// RUN: rm -rf %t-first %t-second && mkdir %t-first %t-second
// RUN: rm -f %t-first.map %t-second.map %t.cache
// RUN: printf "\0" | env SYMCC_OUTPUT_DIR=%t-first SYMCC_AFL_COVERAGE_MAP=%t-first.map SYMCC_POLY_CACHE=%t.cache %t
// RUN: printf "\0" | env SYMCC_OUTPUT_DIR=%t-second SYMCC_AFL_COVERAGE_MAP=%t-second.map SYMCC_POLY_CACHE=%t.cache %t wider-prefix
// RUN: %python -c "from pathlib import Path; values=[p.read_bytes() for p in Path(r'%t-second').iterdir() if p.is_file()]; assert any(v == b'\x01' for v in values), values"

#include <unistd.h>

int main(int argc, char **argv) {
  (void)argv;
  unsigned char value = 0;
  unsigned char limit = argc > 1 ? 2 : 0;
  if (read(STDIN_FILENO, &value, 1) != 1)
    return 1;
  if (value <= limit && value == 1)
    return 7;
  return 0;
}
