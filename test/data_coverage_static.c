// REQUIRES: qsym
// RUN: %symcc -O1 %s -o %t
// RUN: mkdir -p %t-out-a %t-out-b
// RUN: printf '\x02\xe1MAGIC\x0e' | env SYMCC_OUTPUT_DIR=%t-out-a SYMCC_TELEMETRY_OUT=%t-a.json SYMCC_DATA_COVERAGE=1 %t
// RUN: printf '\x02\xe1MAGIC\x0e' | env SYMCC_OUTPUT_DIR=%t-out-b SYMCC_TELEMETRY_OUT=%t-b.json SYMCC_DATA_COVERAGE=1 %t
// RUN: %python -c "import json; a=json.load(open(r'%t-a.json')); b=json.load(open(r'%t-b.json')); assert a['schema']==3 and a['static_data_objects']>=2 and a['static_data_segments']>=1; assert a['static_data_regions']==a['static_data_objects']+a['static_data_segments']; assert a['static_data_accesses']>=3; assert a['data_switches']==1 and a['data_switch_probes']==2; f=a['static_data_features']; assert {x[4] for x in f} >= {0,1,2}; assert any(x[2:5]==[6,8,1] for x in f); assert {(x[0],x[1],x[3]) for x in f} == {(x[0],x[1],x[3]) for x in b['static_data_features']}; assert a['path_hash']==b['path_hash']; assert len(a['data_features'])>=1"

#include <stdint.h>
#include <string.h>
#include <unistd.h>

static const uint8_t transitions[4] = {1, 3, 0xf0, 15};
static const uint32_t graph_nodes[4] = {
    0x10203040U,
    0x50607080U,
    0x90a0b0c0U,
    0xd0e0f000U,
};
static volatile uint32_t sink;

__attribute__((noinline, optnone))
static void consume_simple_load(uint8_t index) {
  sink = graph_nodes[index & 3];
}

int main(void) {
  uint8_t input[8] = {0};
  if (read(STDIN_FILENO, input, sizeof(input)) != sizeof(input))
    return 1;

  consume_simple_load(input[0]);
  if (transitions[input[0] & 3] == input[1])
    sink++;
  if (memcmp(input + 2, "MAGIC", 5) == 0)
    sink++;
  switch (input[7]) {
  case 1:
  case 7:
  case 15:
  case 31:
    sink++;
    break;
  default:
    break;
  }
  return 0;
}
