// REQUIRES: qsym
// RUN: %symcc -O0 -fno-builtin %s -o %t
// RUN: mkdir -p %t-out %t-hints
// RUN: echo -ne "MXXXX\x00" | env SYMCC_OUTPUT_DIR=%t-out SYMCC_TELEMETRY_OUT=%t.json SYMCC_DATA_COVERAGE=1 SYMCC_DATA_CMP_BYTES=8 SYMCC_STRING_HINT_DIR=%t-hints %t
// RUN: python3 -c "import json; d=json.load(open(r'%t.json')); assert d['data_comparisons'] >= 10; assert d['data_coverage_map_updates'] > 0; assert len(d['data_features']) >= 10"
// RUN: python3 -c "import os; toks=[open(os.path.join(r'%t-hints', n),'rb').read() for n in os.listdir(r'%t-hints')]; assert b'MAGIC' in toks and b'MASON' in toks"

#include <string.h>
#include <unistd.h>

unsigned char data_map[65536];
unsigned char *__symcc_afl_area_ptr = data_map;

int main(void) {
  char buf[8] = {0};
  if (read(STDIN_FILENO, buf, 6) <= 0)
    return 1;

  if (memcmp(buf, "MAGIC", 5) == 0)
    return 2;
  if (strcmp(buf, "MAGIC") == 0)
    return 3;
  if (strncmp(buf, "MASON", 5) == 0)
    return 4;
  unsigned touched = 0;
  for (unsigned i = 0; i < sizeof(data_map); ++i)
    touched += data_map[i] != 0;
  if (touched == 0)
    return 5;
  return 0;
}
