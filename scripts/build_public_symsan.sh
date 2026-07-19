#!/bin/bash
# 用 ko-clang(FastGen 插桩)把公开 benchmark 目标编成 *_symsan,供 --engine symsan 跑真实套件。
# 目前覆盖 LAVA-M base64(自带 harness,读文件 → 匹配 fgtest 污点契约)。其余 coreutils 目标
# (md5sum/uniq/who)需整棵 autotools 树过 ko-clang,列为后续;C/C++ libFuzzer 套件见 docs。
#
# 用法:  SYMSAN_INSTALL=/path/to/symsan/install  scripts/build_public_symsan.sh
#   (或设 SYMSAN_KO_CLANG 直接指向 ko-clang)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KO="${SYMSAN_KO_CLANG:-${SYMSAN_INSTALL:?设 SYMSAN_INSTALL 或 SYMSAN_KO_CLANG}/bin/ko-clang}"
[ -x "$KO" ] || { echo "ko-clang 不存在: $KO"; exit 1; }
OUT="${OUT_DIR:-$ROOT/benchmark/public/bin/lava-m}"
mkdir -p "$OUT"

# ko-clang FastGen 模式(fgtest 驱动需要);KO_DONT_OPTIMIZE 保留分支,fgtest 才有回调
export KO_CC="${KO_CC:-clang-18}" KO_USE_FASTGEN=1 KO_DONT_OPTIMIZE=1 KO_USE_NATIVE_LIBCXX=1

build_base64() {
  local base="$ROOT/benchmark/public/lava_corpus/LAVA-M/base64/coreutils-8.24-lava-safe/lib"
  local harness="$ROOT/benchmark/public/lava_corpus/harness_base64.c"
  [ -f "$base/base64.c" ] && [ -f "$harness" ] || { echo "跳过 base64:缺 harness/lib(先跑 setup_public_benchmarks.sh)"; return; }
  # 关键:coreutils 的 gnulib 头(unistd.h 等)要求先 include config.h,故 -include 强制前置;
  # 否则 -I<lib> 会用 gnulib 头替换系统头并报 "Please include config.h first"。
  # 命名 base64_harness_symsan:与既有 symcc 目标 base64_harness 同逻辑名,发现层剥掉
  # _symsan 后缀 → 目标名 lava-base64_harness、种子目录 seeds/lava-m/base64_harness 复用。
  "$KO" -O1 -include "$base/config.h" -I "$base" \
        -o "$OUT/base64_harness_symsan" "$harness" "$base/base64.c"
  echo "built $OUT/base64_harness_symsan  (dfsan syms: $(nm "$OUT/base64_harness_symsan" 2>/dev/null | grep -icE 'dfs\$|__dfsw_|__dfsan_'))"
}

build_base64
echo "=== 完成。跑法:SYMSAN_FGTEST=<fgtest> python3 benchmark/run_benchmark.py --engine symsan --targets lava-base64 ... ==="
