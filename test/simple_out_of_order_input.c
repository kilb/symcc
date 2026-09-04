// REQUIRES: simple
// RUN: %symcc -O0 %s -o %t
// RUN: %t 2>&1 | %filecheck %s --check-prefix=SIMPLE-OFFSET

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

typedef void *SymExpr;

extern SymExpr _sym_get_input_byte(size_t offset, uint8_t concrete_value);
extern const char *_sym_expr_to_string(SymExpr expr);

int main(void) {
#if SIZE_MAX > UINT32_MAX
  const size_t sparse_offset = 1099511627776ULL;
#else
  const size_t sparse_offset = 1073741824U;
#endif
  SymExpr high = _sym_get_input_byte(sparse_offset, 0);
  SymExpr low = _sym_get_input_byte(1, 0);
  SymExpr high_again = _sym_get_input_byte(sparse_offset, 0);

  if (high == NULL || low == NULL || high != high_again || high == low)
    return 1;

  printf("low=%s\n", _sym_expr_to_string(low));
  printf("high=%s\n", _sym_expr_to_string(high));

  // SIMPLE-OFFSET: low=stdin1
  // SIMPLE-OFFSET: high=stdin{{(1099511627776|1073741824)}}
  return 0;
}
