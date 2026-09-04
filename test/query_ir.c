// REQUIRES: qsym
// RUN: rm -rf %t-out %t-spool %t-store %t-async %t-telemetry.json
// RUN: %symcc -O2 %s -o %t
// RUN: mkdir -p %t-out %t-spool %t-store %t-async
// RUN: %python -c "import subprocess; subprocess.run([r'%t'], input=bytes([5,0,0,0]), env={**__import__('os').environ,'SYMCC_OUTPUT_DIR':r'%t-out','SYMCC_QUERY_SPOOL':r'%t-spool','SYMCC_QUERY_OUTPUT_DIR':r'%t-async','SYMCC_QUERY_DEFER':'1','SYMCC_TELEMETRY_OUT':r'%t-telemetry.json'}, check=True)"
// RUN: %python -c "import json; d=json.load(open(r'%t-telemetry.json')); assert d['query_exports']==1 and d['query_export_failures']==0 and d['query_deferred']==1; assert d['query_ir_nodes']>0 and d['query_ir_input_bytes']==4 and d['query_ir_max_bits']>=32 and d['query_ir_comparison_ops']>0; assert d['solver_queries']==0 and d['generated']==0"
// RUN: %queryservice --store %t-store --spool %t-spool --solver %querysolver --jobs 2 --once
// RUN: %python -c "import glob,json,sqlite3; candidates=glob.glob(r'%t-async/async-*'); assert len(candidates)==1 and open(candidates[0],'rb').read()==bytes.fromhex('78563412'); db=sqlite3.connect(r'%t-store/index.sqlite3'); assert db.execute(\"select status from queries\").fetchone()[0]=='done'; result=json.loads(db.execute(\"select result_json from results\").fetchone()[0]); assert result['status']=='sat'"

#include <stdint.h>
#include <unistd.h>

int main(void) {
  uint32_t value = 0;
  if (read(STDIN_FILENO, &value, sizeof(value)) != sizeof(value))
    return 1;
  if (value == 0x12345678U)
    (void)write(STDOUT_FILENO, &value, 1);
  return 0;
}
