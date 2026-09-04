// RUN: %symcc -O0 %s -o %t
// RUN: %t

#include <stddef.h>
#include <stdint.h>

extern void _sym_ucsan_store_shadow(void *, uintptr_t, size_t);
extern uintptr_t _sym_ucsan_load_shadow(const void *, const void *);
extern void _sym_ucsan_copy_shadow(void *, const void *, size_t);

int main(void) {
  uintptr_t source = 0;
  uintptr_t destination = 0;
  _sym_ucsan_store_shadow(&source, 0x1234, sizeof(source));
  _sym_ucsan_copy_shadow(&destination, &source, 1);
  return _sym_ucsan_load_shadow(&destination, &destination) != 0;
}
