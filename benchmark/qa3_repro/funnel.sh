#!/usr/bin/env bash
# 端到端漏斗 + worker 相位计时（docs/Architecture_QA3.md §1.6）。
#
# 注意:xml 的 AFL 二进制是【持久+shmem】模式,afl-fuzz 必须【不带 @@】——带了会让目标
# 退回文件模式,覆盖率几乎测不到(注释见 benchmark/run_benchmark.py:821-822),AFL 队列
# 90s 只长到 20 个,漏斗会被严重扭曲。持久性检测:grep -a '##SIG_AFL_PERSISTENT##' <bin>
set -u
W="${WORK:-/tmp/symcc_qa3}/funnel"
R="${SYMCC_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
rm -rf "$W"; mkdir -p "$W/cwd"; cd "$W/cwd" || exit 1
export AFL_NO_UI=1 AFL_SKIP_CPUFREQ=1 AFL_AUTORESUME=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1
# 持久模式:不带 @@,输入经共享内存喂进 __AFL_LOOP
afl-fuzz -M fuzzer01 -i "$R/benchmark/public/seeds/google-fts/xml_read_fuzzer" -o "$W/afl_out" \
  -- "$R/benchmark/public/bin/google-fts-afl/xml_read_fuzzer" > "$W/afl.log" 2>&1 &
AFLPID=$!
for i in $(seq 120); do [ -f "$W/afl_out/fuzzer01/fuzzer_stats" ] && break; sleep 0.5; done
if [ ! -f "$W/afl_out/fuzzer01/fuzzer_stats" ]; then echo "AFL 启动失败"; tail -20 "$W/afl.log"; exit 1; fi
sleep "${WARMUP:-25}"
echo "预热 ${WARMUP:-25}s 后 AFL queue = $(ls "$W/afl_out/fuzzer01/queue" | wc -l)  execs/s = $(grep execs_per_sec "$W/afl_out/fuzzer01/fuzzer_stats" | tr -s ' ')"
SYMCC_WORKER_PROFILE=1 SYMCC_MASTER_PROFILE=1 SYMCC_TIMEOUT="${SYMCC_TIMEOUT:-10}" timeout "${DUR:-180}" \
  mpirun --allow-run-as-root --oversubscribe -np 9 \
  python3 -u "$R/util/mpi_fuzzing_helper.py" -a fuzzer01 -o "$W/afl_out" -n symcc01 \
  -- "$R/benchmark/public/bin/google-fts/xml_read_fuzzer" @@ > "$W/mpi.log" 2>&1
echo "MPI 结束 rc=$?  最终 AFL queue = $(ls "$W/afl_out/fuzzer01/queue" | wc -l)"
kill $AFLPID 2>/dev/null; sleep 2; pgrep -x afl-fuzz | xargs -r kill 2>/dev/null

# ---- 汇总漏斗与相位计时 ----
D="$W/afl_out/symcc01"
python3 - "$D" <<'PYEOF'
import glob, os, sys
d = sys.argv[1]
cols = ["rank","gen","reported","infeasible","worker_fresh","showmap_none","items","snap_none","byte_dup"]
tot = {c: 0 for c in cols[1:]}
for f in sorted(glob.glob(os.path.join(d, "redun_rank*.csv"))):
    p = [int(x) for x in open(f).read().strip().split(",")]
    for c, v in zip(cols[1:], p[1:]):
        tot[c] += v
try:
    acc = int(open(os.path.join(d, "redun_master.csv")).read().strip().split("\n")[1].split(",")[1])
except (IOError, IndexError, ValueError):
    acc = 0
g, r = tot["gen"], tot["reported"]
if g:
    print(f"\n漏斗: generated={g} → reported(过 worker 位图)={r} ({100*r/g:.1f}%) "
          f"→ accepted(过 master 位图)={acc} ({100*acc/g:.1f}%)")
    print(f"  worker 内冗余 = {g-r} ({100*(g-r)/g:.1f}%)"
          f"  其中 byte_identical={tot['byte_dup']} ({100*tot['byte_dup']/g:.1f}%)"
          f"  worker_fresh={tot['worker_fresh']} ({100*tot['worker_fresh']/g:.1f}%)")
    print(f"  worker 间冗余(reported-accepted) = {r-acc} ({100*(r-acc)/g:.1f}%)")
phs = ["wait","bmsync","import","exec","showmap_dedup","send"]
print("\n相位计时(秒): rank items " + " ".join(phs))
for f in sorted(glob.glob(os.path.join(d, "phase_timing_rank*.csv"))):
    print("  " + open(f).read().strip())
PYEOF
grep -o "\[PROF\].*" "$W/mpi.log" | tail -1
