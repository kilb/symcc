// REQUIRES: qsym
// RUN: %symcc -O0 -fno-builtin-strcmp %s -o %t
// RUN: mkdir -p %t-out
// RUN: printf 'xxxxx\000zz' | env SYMCC_OUTPUT_DIR=%t-out SYMCC_STRING_CONSTRAINT_OUT=%t.jsonl %t
// RUN: %python %S/../util/check_string_constraints.py %t.jsonl 7878787878007a7a MAGIC

#include <stdio.h>
#include <string.h>

int main(void) {
  char input[8] = {0};
  if (fread(input, 1, sizeof(input), stdin) == 0)
    return 1;
  if (strcmp(input, "MAGIC") == 0)
    puts("hit");
  return 0;
}
