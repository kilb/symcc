// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry check
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 41 --expect-values 0,66
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.memory.json --entry memory_check
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory.json --input-hex 05000000 --expect-values 6
// RUN: env SYMCC_LIVE_INPUT_BUFFER_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.buffer.json --entry buffer_check
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.buffer.json --input-hex 414243 --expect-values 0,0,66 --expect-input-buffer
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.buffer.json --input-hex 41 --expect-values 9 --expect-input-buffer
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.stack.json --entry stack_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.stack.json --input-hex 05000000 --expect-values 12 --expect-stack
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.heap.json --entry heap_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap.json --input-hex 05000000 --expect-values 12 --expect-heap
// RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=2 %python %S/../util/llvm_to_continuation.py %s --output %t.heap-pool.json --entry heap_pool_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-pool.json --expect-values 17 --expect-heap-pool --expect-pointer-union
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.cross-pointer.json --entry cross_pointer_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.cross-pointer.json --input-hex 07 --expect-values 7 --expect-stack --expect-cross-function-pointer
// RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=2 %python %S/../util/llvm_to_continuation.py %s --output %t.cross-pointer-return.json --entry cross_pointer_return_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.cross-pointer-return.json --expect-values 7 --expect-heap-pool --expect-cross-function-pointer
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.alias.json --entry alias_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.alias.json --input-hex 05000000 --expect-values 7 --expect-heap --expect-alias
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-union.json --entry pointer_union_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-union.json --input-hex 00 --expect-values 1,2 --expect-pointer-union
// RUN: env SYMCC_LIVE_DYNAMIC_HEAP_LIMIT=8 %python %S/../util/llvm_to_continuation.py %s --output %t.dynamic-heap.json --entry dynamic_heap_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.dynamic-heap.json --input-hex 01 --expect-values 0,1 --expect-nullable-heap --expect-pointer-union
// RUN: env SYMCC_LIVE_DYNAMIC_HEAP_LIMIT=8 %python %S/../util/llvm_to_continuation.py %s --output %t.calloc-heap.json --entry calloc_heap_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.calloc-heap.json --input-hex 02 --expect-values 0,1 --expect-nullable-heap --expect-pointer-union
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.realloc-heap.json --entry realloc_heap_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.realloc-heap.json --input-hex 02 --expect-values 1,52 --expect-heap-pool --expect-pointer-union
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.cross-symbolic-domain.json --entry cross_symbolic_domain_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.cross-symbolic-domain.json --input-hex 00 --expect-values 66 --expect-pointer-union --expect-cross-function-pointer
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.indirect-select.json --entry indirect_select_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.indirect-select.json --input-hex 0005 --expect-values 4,7 --expect-indirect-call
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.indirect-pointer-return.json --entry indirect_pointer_return_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.indirect-pointer-return.json --input-hex 00 --expect-values 65,66 --expect-indirect-call --expect-pointer-union --expect-cross-function-pointer
// RUN: env SYMCC_LIVE_INPUT_BUFFER_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.memory-compare.json --entry memory_compare_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-compare.json --input-hex 414243 --expect-values 0,1 --expect-input-buffer --expect-external-summary
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.region-move.json --entry region_move_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.region-move.json --expect-values 67 --expect-region-summary
// RUN: env SYMCC_LIVE_INPUT_BUFFER_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.region-set.json --entry region_set_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.region-set.json --input-hex 4142 --expect-values 90 --expect-input-buffer --expect-region-summary
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-length.json --entry string_length_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-length.json --input-hex 00 --expect-values 0,1 --expect-string-summary --expect-pointer-union
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-compare.json --entry string_compare_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-compare.json --input-hex 00 --expect-values 0,1 --expect-string-summary --expect-pointer-union
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.memory-search.json --entry memory_search_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-search.json --expect-values 66 --expect-pointer-search --expect-pointer-union
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-search.json --entry string_search_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-search.json --expect-values 66 --expect-pointer-search --expect-pointer-union
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-copy.json --entry string_copy_source --compiler-args="-fno-builtin-strcpy -fno-builtin-strncpy"
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-copy.json --expect-values 1509966401 --expect-string-copy
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-n-copy.json --entry string_n_copy_source --compiler-args="-fno-builtin-strcpy -fno-builtin-strncpy"
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-n-copy.json --expect-values 16961 --expect-string-copy
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.integer-ub.json --entry integer_ub_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.integer-ub.json --input-hex 0803 --expect-values 4,9 --expect-ub-guards
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory.json --entry pointer_memory_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory.json --expect-values 77 --expect-pointer-memory --expect-pointer-union
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.function-pointer-memory.json --entry function_pointer_memory_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.function-pointer-memory.json --input-hex 05 --expect-values 4 --expect-pointer-memory --expect-function-pointer-memory --expect-indirect-call
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.data-pointer-table.json --entry data_pointer_table_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.data-pointer-table.json --input-hex 00 --expect-values 1,2 --expect-pointer-memory --expect-pointer-table --expect-pointer-union --expect-alias
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.scalar-abs.json --entry scalar_abs_source --compiler-args="-fno-builtin-abs -fno-builtin-ntohl"
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.scalar-abs.json --input-hex fbffffff --expect-values 1,2 --expect-scalar-summary --expect-external-summary --expect-ub-guards
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.scalar-byte-order.json --entry scalar_byte_order_source --compiler-args="-fno-builtin-abs -fno-builtin-ntohl"
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.scalar-byte-order.json --expect-values 67305985 --expect-scalar-summary --expect-external-summary
// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bitcount.json --entry bitcount_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.bitcount.json --input-hex 03 --expect-values 1,2 --expect-bitcount-intrinsic

void *malloc(unsigned long size);
void *calloc(unsigned long count, unsigned long size);
void *realloc(void *pointer, unsigned long size);
void free(void *pointer);
int memcmp(const void *left, const void *right, unsigned long size);
void *memmove(void *destination, const void *source, unsigned long size);
void *memset(void *destination, int value, unsigned long size);
unsigned long strlen(const char *value);
int strcmp(const char *left, const char *right);
void *memchr(const void *value, int needle, unsigned long size);
char *strchr(const char *value, int needle);
char *strcpy(char *destination, const char *source);
char *strncpy(char *destination, const char *source, unsigned long size);
int abs(int value);
unsigned ntohl(unsigned value);

static unsigned global_value = 0x11223344U;
static unsigned char pointer_left = 65;
static unsigned char pointer_right = 66;
static unsigned char domain_values[2] = {65, 66};
static const unsigned char compare_expected[3] = {'A', 'B', 'C'};
static unsigned char region_move_bytes[4] = {'A', 'B', 'C', 'D'};
static const char string_empty_source[1] = {'\0'};
static const char string_ab_source[3] = {'A', 'B', '\0'};
static const char string_ac_source[3] = {'A', 'C', '\0'};
static char string_copy_destination_source[4] = {'W', 'X', 'Y', 'Z'};
static char string_n_copy_destination_source[4] = {'W', 'X', 'Y', 'Z'};
unsigned char pointer_memory_target_source = 77;
unsigned char *pointer_memory_slot_source = &pointer_memory_target_source;
unsigned char *data_pointer_table_source_values[2] = {
    &pointer_left, &pointer_right};

unsigned char check(unsigned char input) {
  if (input == 65)
    return 66;
  return 0;
}

unsigned memory_check(unsigned input) {
  global_value = input;
  return global_value + 1U;
}

unsigned char buffer_check(const unsigned char *input, unsigned long size) {
  if (size < 3)
    return 9;
  if (input[0] == 'A' && input[2] == 'C')
    return input[1];
  return 0;
}

__attribute__((optnone))
unsigned stack_source(unsigned input) {
  unsigned values[2];
  values[0] = input;
  values[1] = 7;
  return values[0] + values[1];
}

__attribute__((optnone))
unsigned heap_source(unsigned input) {
  unsigned *values = (unsigned *)malloc(2 * sizeof(unsigned));
  values[0] = input;
  values[1] = 7;
  unsigned result = values[0] + values[1];
  free(values);
  return result;
}

__attribute__((optnone))
unsigned char heap_pool_source(void) {
  unsigned char iteration = 0;
  unsigned char sum = 0;
  unsigned char *value;
loop:
  value = (unsigned char *)malloc(1);
  *value = iteration;
  if (*value == 0)
    sum |= 1;
  else
    sum |= 16;
  iteration ^= 1;
  if (iteration != 0)
    goto loop;
  return sum;
}

__attribute__((noinline, optnone))
static unsigned char cross_pointer_helper(unsigned char *pointer,
                                          unsigned char value) {
  *pointer = value;
  return *pointer;
}

__attribute__((optnone))
unsigned char cross_pointer_source(unsigned char input) {
  unsigned char slot;
  return cross_pointer_helper(&slot, input);
}

__attribute__((noinline, optnone))
static unsigned char *cross_make_heap_pointer(void) {
  unsigned char *pointer = (unsigned char *)malloc(1);
  *pointer = 7;
  return pointer;
}

__attribute__((optnone))
unsigned char cross_pointer_return_source(void) {
  unsigned char *pointer = cross_make_heap_pointer();
  unsigned char value = *pointer;
  free(pointer);
  return value;
}

__attribute__((optnone))
unsigned alias_source(unsigned input) {
  unsigned char *values = (unsigned char *)malloc(4);
  unsigned index = input & 3U;
  unsigned char *slot = values + index;
  *slot = 7;
  unsigned result = *slot;
  free(values);
  return result;
}

__attribute__((optnone))
unsigned char pointer_union_source(unsigned char input) {
  unsigned char *pointer =
      (input & 1U) ? &pointer_left : &pointer_right;
  if (*pointer == 65)
    return 1;
  return 2;
}

__attribute__((optnone))
unsigned char dynamic_heap_source(unsigned char size) {
  unsigned char *memory = (unsigned char *)malloc(size);
  if (memory == 0)
    return 1;
  *memory = 7;
  free(memory);
  return 0;
}

__attribute__((optnone))
unsigned char calloc_heap_source(unsigned char count) {
  unsigned char *memory = (unsigned char *)calloc(count, 1);
  if (memory == 0)
    return 1;
  unsigned char value = *memory;
  free(memory);
  return value;
}

__attribute__((optnone))
unsigned char realloc_heap_source(unsigned char size) {
  unsigned short *memory = (unsigned short *)malloc(4);
  *memory = 4660;
  unsigned char *resized = (unsigned char *)realloc(memory, size);
  if (resized == 0)
    return 1;
  unsigned char value = *resized;
  free(resized);
  return value;
}

__attribute__((noinline, optnone))
static unsigned char domain_reader(unsigned char *pointer) {
  return pointer[1];
}

__attribute__((optnone))
unsigned char cross_symbolic_domain_source(unsigned char index) {
  return domain_reader(domain_values + index);
}

typedef unsigned char (*byte_transform)(unsigned char);

__attribute__((noinline, optnone))
static unsigned char indirect_left_source(unsigned char value) {
  return value ^ 1;
}

__attribute__((noinline, optnone))
static unsigned char indirect_right_source(unsigned char value) {
  return value ^ 2;
}

byte_transform function_pointer_memory_slot_source =
    indirect_left_source;

__attribute__((optnone))
unsigned char indirect_select_source(
    unsigned char choose, unsigned char value) {
  byte_transform target =
      choose ? indirect_left_source : indirect_right_source;
  return target(value);
}

__attribute__((optnone))
unsigned char function_pointer_memory_source(unsigned char value) {
  return function_pointer_memory_slot_source(value);
}

typedef unsigned char *(*pointer_identity)(unsigned char *);

__attribute__((noinline, optnone))
static unsigned char *indirect_pointer_left_source(
    unsigned char *value) {
  (void)value;
  return &pointer_left;
}

__attribute__((noinline, optnone))
static unsigned char *indirect_pointer_right_source(
    unsigned char *value) {
  return value;
}

__attribute__((optnone))
unsigned char indirect_pointer_return_source(unsigned char choose) {
  pointer_identity target =
      choose ? indirect_pointer_left_source
             : indirect_pointer_right_source;
  unsigned char *argument =
      choose ? &pointer_left : &pointer_right;
  return *target(argument);
}

__attribute__((optnone))
unsigned char memory_compare_source(
    const unsigned char *input, unsigned long size) {
  if (size < 3)
    return 9;
  if (memcmp(input, compare_expected, 3) == 0)
    return 1;
  return 0;
}

__attribute__((optnone))
unsigned char region_move_source(void) {
  memmove(region_move_bytes + 1, region_move_bytes, 3);
  return region_move_bytes[3];
}

__attribute__((optnone))
unsigned char region_set_source(
    unsigned char *input, unsigned long size) {
  if (size < 2)
    return 9;
  memset(input, 90, 2);
  return input[0];
}

__attribute__((optnone))
unsigned char string_length_source(unsigned char choose) {
  const char *value =
      choose ? string_empty_source : string_ab_source;
  if (strlen(value) == 2)
    return 1;
  return 0;
}

__attribute__((optnone))
unsigned char string_compare_source(unsigned char choose) {
  const char *value =
      choose ? string_ab_source : string_ac_source;
  if (strcmp(value, string_ab_source) == 0)
    return 1;
  return 0;
}

__attribute__((optnone))
unsigned char memory_search_source(void) {
  const unsigned char *found =
      (const unsigned char *)memchr(compare_expected, 'B', 3);
  return *found;
}

__attribute__((optnone))
unsigned char string_search_source(void) {
  const char *found = strchr(string_ab_source, 'B');
  return (unsigned char)*found;
}

__attribute__((optnone))
unsigned string_copy_source(void) {
  strcpy(string_copy_destination_source, string_ab_source);
  return (unsigned char)string_copy_destination_source[0] |
         ((unsigned)(unsigned char)string_copy_destination_source[1] << 8) |
         ((unsigned)(unsigned char)string_copy_destination_source[2] << 16) |
         ((unsigned)(unsigned char)string_copy_destination_source[3] << 24);
}

__attribute__((optnone))
unsigned string_n_copy_source(void) {
  strncpy(string_n_copy_destination_source, string_ab_source, 4);
  return (unsigned char)string_n_copy_destination_source[0] |
         ((unsigned)(unsigned char)string_n_copy_destination_source[1] << 8) |
         ((unsigned)(unsigned char)string_n_copy_destination_source[2] << 16) |
         ((unsigned)(unsigned char)string_n_copy_destination_source[3] << 24);
}

__attribute__((optnone))
unsigned char integer_ub_source(
    unsigned char left, unsigned char right) {
  if (right == 0)
    return 9;
  return (unsigned char)(left / right + left % right);
}

__attribute__((optnone))
unsigned char pointer_memory_source(void) {
  return *pointer_memory_slot_source;
}

__attribute__((optnone))
unsigned char data_pointer_table_source(unsigned char index) {
  unsigned char value =
      *data_pointer_table_source_values[index & 1U];
  if (value == 65)
    return 1;
  return 2;
}

__attribute__((optnone))
unsigned char scalar_abs_source(int value) {
  if (abs(value) == 5)
    return 1;
  return 2;
}

__attribute__((optnone))
unsigned scalar_byte_order_source(void) {
  return ntohl(0x01020304U);
}

__attribute__((optnone))
unsigned char bitcount_source(unsigned char value) {
  if (__builtin_popcount((unsigned)value) == 2)
    return 1;
  return 2;
}
