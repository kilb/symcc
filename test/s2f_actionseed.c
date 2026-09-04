// REQUIRES: qsym
// RUN: %symcc -O0 %s -o %t
// RUN: %python %S/../util/check_s2f_actionseed.py %t

#include <stdio.h>

int main(int argc, char **argv) {
  unsigned char byte = 0;
  if (argc < 2)
    return 2;
  FILE *input = fopen(argv[1], "rb");
  if (input == 0)
    return 3;
  fread(&byte, 1, 1, input);
  fclose(input);
  if (byte == 'Z')
    return 1;
  return 0;
}
