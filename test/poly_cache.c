// REQUIRES: qsym
// RUN: %symcc %s -o %t
// RUN: rm -rf %t-out1 %t-out2 && mkdir %t-out1 %t-out2
// RUN: rm -f %t-map1 %t-map2 %t-one.json %t-two.json %t-poly.cache
// RUN: echo -ne "\x00\x00\x00\x00" | env SYMCC_OUTPUT_DIR=%t-out1 SYMCC_AFL_COVERAGE_MAP=%t-map1 SYMCC_TELEMETRY_OUT=%t-one.json SYMCC_POLY_CACHE=%t-poly.cache SYMCC_POLY_RANGE_BYTES=4 SYMCC_POLY_TEMPLATE_BYTES=4 SYMCC_POLY_TEMPLATE_PAIRS=4 SYMCC_POLY_WALK=john SYMCC_POLY_DENSE_DIM=4 SYMCC_POLY_SAMPLES=2 %t
// RUN: test -s %t-poly.cache
// RUN: python3 -c "lines=[line.split() for line in open(r'%t-poly.cache')]; assert any(len(fields) >= 6 and fields[5] != '-' and fields[5].count(';') >= 2 for fields in lines)"
// RUN: python3 -c "import json; d=json.load(open(r'%t-one.json')); assert d['poly_template_constraints'] >= 2; assert d['poly_dense_walks'] >= 1; assert d['poly_john_steps'] >= 1"
// RUN: echo -ne "\x00\x00\x00\x00" | env SYMCC_OUTPUT_DIR=%t-out2 SYMCC_AFL_COVERAGE_MAP=%t-map2 SYMCC_TELEMETRY_OUT=%t-two.json SYMCC_POLY_CACHE=%t-poly.cache SYMCC_POLY_RANGE_BYTES=4 SYMCC_POLY_TEMPLATE_BYTES=4 SYMCC_POLY_TEMPLATE_PAIRS=4 SYMCC_POLY_WALK=john SYMCC_POLY_DENSE_DIM=4 SYMCC_POLY_SAMPLES=2 %t
// RUN: python3 -c "import json; d=json.load(open(r'%t-two.json')); assert d['poly_cache_hits'] >= 1; assert d['generated'] >= 1"

#include <stdint.h>
#include <unistd.h>

static volatile uint32_t sink;

int main(void) {
  union {
    uint32_t value;
    uint8_t bytes[4];
  } input = {0};
  if (read(0, &input.value, sizeof(input.value)) != sizeof(input.value))
    return 1;
  if ((uint16_t)input.bytes[0] + (uint16_t)input.bytes[1] < 20)
    sink++;
  if (input.value == 0x78563412)
    return 0;
  return 0;
}
