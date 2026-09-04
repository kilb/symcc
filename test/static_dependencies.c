// RUN: rm -f %t.deps
// RUN: env SYMCC_STATIC_DEPENDENCE_OUT=%t.deps %symcc -O0 %s -o %t
// RUN: %python -c "p=open(r'%t.deps').read().splitlines(); rows=[x.split() for x in p if x and x[0]!='#']; assert any(r[1:3] == ['2','2'] for r in rows), p"

#include <unistd.h>
#include <stdlib.h>

int main(void) {
  unsigned char input[4] = {0};
  if (read(0, input, sizeof(input)) != sizeof(input))
    return 0;
  if (input[2] == 0x5a)
    _Exit(7);
  return 0;
}
