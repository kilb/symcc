// REQUIRES: qsym
// RUN: %symcc -O2 %s -o %t
// RUN: mkdir -p %t-first %t-second %t-target
// RUN: rm -f %t-map
// RUN: echo -ne "\x05\x00\x00\x00" | env SYMCC_OUTPUT_DIR=%t-first SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t-first.json SYMCC_DATA_COVERAGE=1 %t
// RUN: python3 -c "import json; d=json.load(open(r'%t-first.json')); assert d['schema']==3; assert d['input_bytes']==4; assert d['symbolic_branches']>=1; assert d['solver_queries']>=1; assert d['generated']>=1; assert d['path_hash']!=0; assert d['open_branches']==[]; assert len(d['branch_trace'])>=1; assert d['data_comparisons']>=1; assert len(d['data_features'])>=1; assert len(d['comparison_taints'])>=1; assert 'static_data_regions' in d and 'static_data_objects' in d and 'static_data_segments' in d and 'static_data_accesses' in d and 'static_data_features' in d; assert 'prefix_context_hits' in d and 'prefix_context_entries' in d and 'unsat_core_clauses' in d and 'unsat_core_unification_hits' in d and 'backsolver_constraints_kept' in d and 'backsolver_constraints_dropped' in d and 'backsolver_direct_attempts' in d and 'backsolver_direct_sat' in d and 'backsolver_validations' in d and 'backsolver_validation_failures' in d and 'backsolver_z3_fallbacks' in d and 'poly_dense_walks' in d and 'poly_john_steps' in d and 'poly_dense_fallbacks' in d"
// RUN: echo -ne "\x05\x00\x00\x00" | env SYMCC_OUTPUT_DIR=%t-second SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t-second.json SYMCC_DATA_COVERAGE=1 %t
// RUN: python3 -c "import json; d=json.load(open(r'%t-second.json')); assert d['solver_queries']==0; assert len(d['open_branches'])>=1"
// RUN: python3 -c "import json, os, subprocess; d=json.load(open(r'%t-second.json')); e=os.environ.copy(); e.update({'SYMCC_OUTPUT_DIR':r'%t-target','SYMCC_AFL_COVERAGE_MAP':r'%t-map','SYMCC_TELEMETRY_OUT':r'%t-target.json','SYMCC_TARGET_BRANCH':str(d['open_branches'][0]),'SYMCC_DATA_COVERAGE':'1'}); subprocess.run([r'%t'], input=bytes([5,0,0,0]), env=e, check=True)"
// RUN: python3 -c "import json; d=json.load(open(r'%t-target.json')); assert d['target_reached']; assert d['target_status']=='sat'; assert d['solver_queries']==1; assert d['generated']==1"

#include <stdint.h>
#include <unistd.h>

int main(void) {
  uint32_t value;
  if (read(STDIN_FILENO, &value, sizeof(value)) != sizeof(value))
    return 1;
  if (value == 0x12345678U)
    return write(STDOUT_FILENO, &value, 1) != 1;
  return 0;
}
