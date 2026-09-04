#!/usr/bin/env bash
# 持久模式 A/B：同一个 AFL 二进制，唯一差别是命令行带不带 @@。
# 带 @@ 会让持久+shmem 目标退回文件模式（每次执行 fork+文件 I/O）。
set -u
S="${WORK:-/tmp/symcc_qa3}/persab"
R="${SYMCC_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
DUR=${DUR:-60}
rm -rf "$S"; mkdir -p "$S"

one() {   # $1=tag  $2=bin  $3=seeds  $4="@@" 或空
  local tag="$1" bin="$2" seeds="$3" arg="$4"
  mkdir -p "$S/$tag/cwd"
  cd "$S/$tag/cwd" || return 1
  if [ -n "$arg" ]; then
    AFL_NO_UI=1 AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
      timeout "$DUR" afl-fuzz -M fuzzer01 -i "$seeds" -o "$S/$tag/out" -- "$bin" "$arg" > "$S/$tag/log" 2>&1
  else
    AFL_NO_UI=1 AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
      timeout "$DUR" afl-fuzz -M fuzzer01 -i "$seeds" -o "$S/$tag/out" -- "$bin" > "$S/$tag/log" 2>&1
  fi
  local f="$S/$tag/out/fuzzer01/fuzzer_stats"
  if [ -f "$f" ]; then
    printf '%-28s ' "$tag"
    awk -F': *' '/^execs_per_sec/{e=$2} /^execs_done/{d=$2} /^corpus_count/{c=$2} /^bitmap_cvg/{b=$2}
                 END{printf "execs/s=%-12s execs=%-12s queue=%-8s bitmap=%s\n", e, d, c, b}' "$f"
  else
    printf '%-28s 启动失败:\n' "$tag"; tail -6 "$S/$tag/log"
  fi
}

XB=$R/benchmark/public/bin/google-fts-afl/xml_read_fuzzer
XS=$R/benchmark/public/seeds/google-fts/xml_read_fuzzer
PB=$R/benchmark/public/bin/google-fts-afl/png_read_fuzzer
PS=$R/benchmark/public/seeds/google-fts/png_read_fuzzer

echo "=== 每组 ${DUR}s，同一二进制，唯一差别 = 命令行带不带 @@ ==="
one "xml_带@@(退回文件模式)"  "$XB" "$XS" "@@"
one "xml_不带@@(持久+shmem)"  "$XB" "$XS" ""
one "png_带@@(退回文件模式)"  "$PB" "$PS" "@@"
one "png_不带@@(持久+shmem)"  "$PB" "$PS" ""
