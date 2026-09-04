// REQUIRES: qsym
// RUN: clang -O1 -fPIC -shared -DDATA_COVERAGE_DSO %s -o %t-lib.so
// RUN: %symcc -O1 %s %t-lib.so -o %t
// RUN: mkdir -p %t-out-a %t-out-b
// RUN: printf '\x02' | env SYMCC_OUTPUT_DIR=%t-out-a SYMCC_TELEMETRY_OUT=%t-a.json SYMCC_DATA_COVERAGE=1 %t
// RUN: printf '\x02' | env SYMCC_OUTPUT_DIR=%t-out-b SYMCC_TELEMETRY_OUT=%t-b.json SYMCC_DATA_COVERAGE=1 %t
// RUN: %python -c "import json; a=json.load(open(r'%t-a.json')); b=json.load(open(r'%t-b.json')); assert a['static_data_objects']==0 and a['static_data_segments']>=1; f=[x for x in a['static_data_features'] if x[4]==0 and x[3]==32]; assert len(f)==1; g=[x for x in b['static_data_features'] if x[4]==0 and x[3]==32]; assert {(x[0],x[1],x[3]) for x in f}=={(x[0],x[1],x[3]) for x in g}; assert a['path_hash']==b['path_hash']"

#include <stdint.h>
#include <unistd.h>

#ifdef DATA_COVERAGE_DSO

static const uint32_t external_nodes[4] = {
    0x11121314U,
    0x21222324U,
    0x31323334U,
    0x41424344U,
};

const uint32_t *data_coverage_external_nodes(void) {
  return external_nodes;
}

#else

extern const uint32_t *data_coverage_external_nodes(void);
static volatile uint32_t sink;

int main(void) {
  uint8_t index = 0;
  if (read(STDIN_FILENO, &index, sizeof(index)) != sizeof(index))
    return 1;
  const uint32_t *nodes = data_coverage_external_nodes();
  sink = nodes[index & 3];
  return 0;
}

#endif
