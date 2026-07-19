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

# 重库目标(格式解析器,concolic 强项):用 ko-clang 整体重编库 .a 再链接独立 harness。
# 库 .a 均为构建产物(gitignored),重编不影响仓库。较重,仅 BUILD_LIBS=1 时执行。
COMPILE="$ROOT/benchmark/compile_public_benchmarks.sh"
# 从 compile_public_benchmarks.sh 抽取某 heredoc harness 到临时文件,回显路径(失败回显空)
extract_harness() {  # $1=/tmp/<name>.c 标记
  local marker="$1" out; out="$(mktemp --suffix=.c)"
  awk "/cat > ${marker//\//\\/} << 'HARNESS_EOF'/{f=1;next} /^HARNESS_EOF/{if(f)exit} f" "$COMPILE" > "$out"
  [ -s "$out" ] && echo "$out" || { rm -f "$out"; echo ""; }
}

build_libxml2() {
  local xmldir="$ROOT/benchmark/public/gfts_build/libxml2-2.9.2"
  [ -f "$xmldir/configure" ] && [ -f "$COMPILE" ] || { echo "跳过 libxml2:缺源码/脚本"; return; }
  local h; h="$(extract_harness /tmp/xml_read_fuzzer.c)"; [ -n "$h" ] || { echo "跳过 libxml2:harness 抽取失败"; return; }
  ( cd "$xmldir" && make clean >/dev/null 2>&1; make CC="$KO" libxml2.la -j"$(nproc)" >/dev/null 2>&1 )
  "$KO" -O1 "$h" -I "$xmldir/include" -I "$xmldir/include/libxml" "$xmldir/.libs/libxml2.a" \
        -lz -lm -lpthread -o "$ROOT/benchmark/public/bin/google-fts/xml_read_fuzzer_symsan"
  rm -f "$h"; echo "built xml_read_fuzzer_symsan"
}

build_libpng() {
  local d="$ROOT/benchmark/public/gfts_build/libpng-1.2.56"
  [ -f "$d/configure" ] && [ -f "$COMPILE" ] || { echo "跳过 libpng:缺源码/脚本"; return; }
  local h; h="$(extract_harness /tmp/png_read_fuzzer.c)"; [ -n "$h" ] || { echo "跳过 libpng:harness 抽取失败"; return; }
  local la; ( cd "$d" && la=$(ls *.la 2>/dev/null | head -1); make clean >/dev/null 2>&1; make CC="$KO" "${la:-libpng12.la}" -j"$(nproc)" >/dev/null 2>&1 )
  "$KO" -O1 "$h" -I "$d" "$d"/.libs/libpng*.a -lz -lm \
        -o "$ROOT/benchmark/public/bin/google-fts/png_read_fuzzer_symsan"
  rm -f "$h"; echo "built png_read_fuzzer_symsan"
}

build_pcre2() {
  local d="$ROOT/benchmark/public/gfts_build/pcre2-src"
  local harness="$ROOT/benchmark/targets/pcre2_harness.c"
  [ -f "$d/CMakeLists.txt" ] && [ -f "$harness" ] || { echo "跳过 pcre2:缺源码/harness"; return; }
  ( cd "$d" && rm -rf build_symsan && mkdir build_symsan && cd build_symsan \
    && cmake -DCMAKE_C_COMPILER="$KO" -DPCRE2_BUILD_PCRE2_8=ON -DPCRE2_BUILD_TESTS=OFF \
             -DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release .. >/dev/null 2>&1 \
    && make pcre2-8 -j"$(nproc)" >/dev/null 2>&1 )
  "$KO" -O1 -DPCRE2_CODE_UNIT_WIDTH=8 -I "$d/build_symsan" "$harness" \
        "$d/build_symsan/libpcre2-8.a" -o "$ROOT/benchmark/public/bin/pcre2/pcre2_fuzzer_symsan"
  echo "built pcre2_fuzzer_symsan"
}
# sqlite:更正——【能编译】。ko-clang 强制的 -O3 会在巨型 sqlite3VdbeExec 上触发向量化 →
# bitcast v2i64→i64 → X86 ISel "Cannot select"(后端崩溃,非 DFSan pass)。用 KO_DONT_OPTIMIZE=1
# (跳过 -O3,fgtest 目标本就用此档)即编译通过。但 concolic 运行期命中 DFSan uninitialized-label →
# 产出 0,故仅编译、不接入发现层(运行期问题待后续)。BUILD_SQLITE=1 启用。
build_sqlite() {
  local d="$ROOT/benchmark/public/fuzzer-test-suite/sqlite-2016-11-14"
  local harness="$ROOT/benchmark/targets/sqlite_harness.c"
  [ -f "$d/sqlite3.c" ] && [ -f "$harness" ] || { echo "跳过 sqlite:缺 amalgamation/harness"; return; }
  # 不传 -O(ko-clang 会 strip);KO_DONT_OPTIMIZE 跳过强制 -O3 → 避开 v2i64 ISel 崩溃
  KO_USE_FASTGEN=1 KO_DONT_OPTIMIZE=1 "$KO" -DSQLITE_THREADSAFE=0 -DSQLITE_OMIT_LOAD_EXTENSION \
    -I "$d" "$harness" "$d/sqlite3.c" -ldl -o "$ROOT/benchmark/public/bin/sqlite/sqlite_fuzzer_symsan" \
    && echo "built sqlite_fuzzer_symsan(仅编译;运行期 uninitialized-label,concolic 产出 0)" \
    || echo "sqlite 编译失败"
}

# coreutils md5sum/uniq/who:整棵 autotools 树用 ko-clang 重编。【能编译】,但因这三个程序
# strcmp/哈希主导,fgtest 下 concolic 无产出(见 docs)。故仅编译、不接入发现层。BUILD_COREUTILS=1 启用。
build_coreutils() {
  local lava="$ROOT/benchmark/public/lava_corpus/LAVA-M"
  for prog in md5sum uniq who; do
    local tree="$lava/$prog/coreutils-8.24-lava-safe"
    [ -f "$tree/Makefile" ] || { echo "跳过 $prog:未配置"; continue; }
    ( cd "$tree" && KO_USE_FASTGEN=1 KO_DONT_OPTIMIZE=1 TAINT_OPTIONS="taint_file=/dev/null output_dir=/tmp" \
        make clean >/dev/null 2>&1; make -k -i CC="$KO" -j"$(nproc)" >/dev/null 2>&1 )
    [ -x "$tree/src/$prog" ] && echo "built $prog (仅编译;concolic 无产出)" || echo "$prog 编译失败"
  done
}

build_base64
if [ "${BUILD_LIBS:-0}" = "1" ]; then
  build_libxml2; build_libpng; build_pcre2
else
  echo "跳过重库目标 libxml2/libpng/pcre2(设 BUILD_LIBS=1 启用,较重)"
fi
[ "${BUILD_COREUTILS:-0}" = "1" ] && build_coreutils
[ "${BUILD_SQLITE:-0}" = "1" ] && build_sqlite
echo "=== 完成。跑法:SYMSAN_FGTEST=<fgtest> python3 benchmark/run_benchmark.py --engine symsan --targets lava-base64 ... ==="
