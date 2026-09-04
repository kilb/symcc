// REQUIRES: qsym
// RUN: %symcc -O0 -fno-builtin-strcmp %s -o %t
// RUN: rm -rf %t-out && mkdir %t-out
// RUN: %python -c "import subprocess; subprocess.run([r'%t'], input=b'A'*80, env={**__import__('os').environ, 'SYMCC_OUTPUT_DIR':r'%t-out', 'SYMCC_DATA_CMP_BYTES':'64'}, check=True)"
// RUN: %python -c "from pathlib import Path; assert any(p.is_file() for p in Path(r'%t-out').iterdir())"

#include <string.h>
#include <unistd.h>

int main(void) {
  char input[81] = {0};
  char expected[81];
  memset(expected, 'A', 80);
  expected[80] = '\0';
  if (read(STDIN_FILENO, input, 80) != 80)
    return 1;
  return strcmp(input, expected) == 0 ? 0 : 2;
}
