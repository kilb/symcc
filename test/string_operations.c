// REQUIRES: qsym
// RUN: %symcc -O0 -fno-builtin-strlen -fno-builtin-strchr -fno-builtin-strstr -fno-builtin-atoi -fno-builtin-strtol -fno-builtin-strtoul %s -o %t
// RUN: mkdir -p %t-out
// RUN: rm -f %t.jsonl
// RUN: printf 'ABC\000ABC\000ABC\000123\000-123\000456\000' | env SYMCC_OUTPUT_DIR=%t-out SYMCC_STRING_CONSTRAINT_OUT=%t.jsonl %t
// RUN: %python %S/../util/check_string_operations.py %t.jsonl 414243004142430041424300313233002d3132330034353600 %querysolver

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
  char input[25];
  if (fread(input, 1, sizeof(input), stdin) != sizeof(input))
    return 1;
  volatile size_t length = strlen(input);
  volatile const char *character = strchr(input + 4, 'Z');
  volatile const char *substring = strstr(input + 8, "BC");
  volatile int number = atoi(input + 12);
  volatile long signed_number = strtol(input + 16, NULL, 10);
  volatile unsigned long unsigned_number = strtoul(input + 21, NULL, 10);
  char *end = NULL;
  volatile long unsupported = strtol(input + 16, &end, 16);
  char *unsigned_end = NULL;
  volatile unsigned long unsupported_unsigned =
      strtoul(input + 21, &unsigned_end, 16);
  volatile unsigned long negative_unsigned =
      strtoul(input + 16, NULL, 10);
  return length == 99 || character != NULL || substring == NULL ||
         number != 123 || signed_number != -123 || unsigned_number != 456 ||
         unsupported != -0x123 || end == NULL || *end != '\0' ||
         unsupported_unsigned != 0x456 || unsigned_end == NULL ||
         *unsigned_end != '\0' || negative_unsigned != 0UL - 123UL;
}
