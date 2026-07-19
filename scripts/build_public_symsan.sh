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

# libxml2 xml_read_fuzzer:格式解析器,concolic 的强项。需用 ko-clang 整体重编 libxml2.a
# (~200 文件,较重),再链接独立 harness。libxml2.a 为构建产物(gitignored),重编不影响仓库。
# 仅当设 BUILD_XML=1 时执行(避免默认跑重构建)。
build_libxml2() {
  [ "${BUILD_XML:-0}" = "1" ] || { echo "跳过 libxml2(设 BUILD_XML=1 启用,较重)"; return; }
  local xmldir="$ROOT/benchmark/public/gfts_build/libxml2-2.9.2"
  local compile="$ROOT/benchmark/compile_public_benchmarks.sh"
  [ -f "$xmldir/configure" ] && [ -f "$compile" ] || { echo "跳过 libxml2:缺源码/编译脚本"; return; }
  # 从 compile_public_benchmarks.sh 抽取独立 xml harness(读文件 → 匹配 fgtest 契约)
  local harness; harness="$(mktemp --suffix=.c)"
  awk "/cat > \\/tmp\\/xml_read_fuzzer.c << 'HARNESS_EOF'/{f=1;next} /^HARNESS_EOF/{if(f)exit} f" "$compile" > "$harness"
  [ -s "$harness" ] || { echo "跳过 libxml2:未能抽取 harness"; rm -f "$harness"; return; }
  ( cd "$xmldir" && make clean >/dev/null 2>&1; make CC="$KO" libxml2.la -j"$(nproc)" >/dev/null 2>&1 )
  "$KO" -O1 "$harness" -I "$xmldir/include" -I "$xmldir/include/libxml" \
        "$xmldir/.libs/libxml2.a" -lz -lm -lpthread \
        -o "$ROOT/benchmark/public/bin/google-fts/xml_read_fuzzer_symsan"
  rm -f "$harness"
  echo "built .../google-fts/xml_read_fuzzer_symsan  (dfsan syms: $(nm "$ROOT/benchmark/public/bin/google-fts/xml_read_fuzzer_symsan" 2>/dev/null | grep -icE 'dfs\$|__dfsw_|__dfsan_'))"
}

build_base64
build_libxml2
echo "=== 完成。跑法:SYMSAN_FGTEST=<fgtest> python3 benchmark/run_benchmark.py --engine symsan --targets lava-base64 ... ==="
