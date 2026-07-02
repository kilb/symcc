#!/usr/bin/env bash
###############################################################################
# 将已构建的 FTS AFL 目标重链接为 AFL 持久模式 + 共享内存输入。
#
# 消除每次执行的 fork()/execve + 文件 I/O；实测吞吐提升 7-35x（png 18x、xml 7.5x）。
# 做法：这些是 LLVMFuzzerTestOneInput（libarchive/pcre2）或 ossfuzz(sqlite) 风格 harness，
# 只需链接 AFL++ 的 libAFLDriver.a（它提供 __AFL_LOOP 持久 main + __AFL_FUZZ_TESTCASE_BUF
# 共享内存输入），无需改 harness 源码。运行后 run_benchmark.py 会自动检测（强符号
# `D __afl_sharedmem_fuzzing`）并以"不带 @@"方式驱动它们（afl-fuzz 与 afl-showmap 均然）。
#
# 注：png / xml 的持久化由 compile_public_benchmarks.sh 的 dual-mode heredoc 处理
# （它们是自定义 file-main harness，编译期即带持久循环），不在此脚本内重复。
#
# 幂等：已是持久的目标默认跳过（FORCE=1 可强制重链接）。缺少构件时优雅跳过（不报错）。
# 用法：bash make_afl_targets_persistent.sh   （在 --with-afl 构建之后运行）
###############################################################################
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUBLIC="$SCRIPT_DIR/public"
FTS="$PUBLIC/fuzzer-test-suite"
GFTS="$PUBLIC/gfts_build"
FORCE="${FORCE:-0}"

# 定位 AFL++ 持久驱动库 libAFLDriver.a
DRIVER=""
for c in /usr/local/lib/afl/libAFLDriver.a \
         "$(dirname "$(command -v afl-fuzz 2>/dev/null)" 2>/dev/null)/../lib/afl/libAFLDriver.a"; do
    [ -f "$c" ] && DRIVER="$c" && break
done
if ! command -v afl-clang-fast >/dev/null 2>&1 || [ -z "$DRIVER" ]; then
    echo "[persistent] 缺少 afl-clang-fast 或 libAFLDriver.a，跳过（无回归）"
    exit 0
fi
echo "[persistent] using driver: $DRIVER"

is_persistent() { nm "$1" 2>/dev/null | grep -q "D __afl_sharedmem_fuzzing"; }
# 取匹配某 glob 的首个存在路径（版本目录健壮性）
first() { local p; for p in $1; do [ -e "$p" ] && { echo "$p"; return; }; done; }

CONVERTED=0; SKIPPED=0

# 通用：链接后校验持久符号
finish() {  # $1=target名 $2=输出二进制
    if is_persistent "$2"; then
        echo "[persistent] $1 -> PERSISTENT ✓"; CONVERTED=$((CONVERTED + 1))
    else
        echo "[persistent] $1 -> 链接后未检出持久符号，失败"; SKIPPED=$((SKIPPED + 1))
    fi
}

# --- libarchive（LLVMFuzzerTestOneInput，.cc）---
do_libarchive() {
    local out="$PUBLIC/bin/libarchive-afl/archive_fuzzer"
    local src lib inc
    src="$(first "$FTS/libarchive-*/libarchive_fuzzer.cc")"
    lib="$(first "$GFTS/libarchive-src/build_afl/libarchive/libarchive.a")"
    inc="$(first "$GFTS/libarchive-src/libarchive")"
    [ -f "$out" ] || { echo "[persistent] libarchive: 无 -afl 二进制，跳过"; SKIPPED=$((SKIPPED+1)); return; }
    if [ "$FORCE" != 1 ] && is_persistent "$out"; then echo "[persistent] libarchive: 已持久，跳过"; SKIPPED=$((SKIPPED+1)); return; fi
    [ -n "$src" ] && [ -n "$lib" ] && [ -n "$inc" ] || { echo "[persistent] libarchive: 构件缺失，跳过"; SKIPPED=$((SKIPPED+1)); return; }
    AFL_QUIET=1 afl-clang-fast++ -O2 "$src" -I "$inc" "$lib" "$DRIVER" \
        -lz -lbz2 -llzma -lxml2 -lcrypto -lpthread -o "$out" 2>/dev/null
    finish libarchive "$out"
}

# --- pcre2（LLVMFuzzerTestOneInput，.cc）---
do_pcre2() {
    local out="$PUBLIC/bin/pcre2-afl/pcre2_fuzzer"
    local src lib libp inc1 inc2
    src="$(first "$FTS/pcre2-*/target.cc")"
    lib="$(first "$GFTS/pcre2-src/build_afl/libpcre2-8.a")"
    libp="$(first "$GFTS/pcre2-src/build_afl/libpcre2posix.a")"
    inc1="$(first "$GFTS/pcre2-src/build_afl")"          # 生成的 pcre2.h
    inc2="$(first "$GFTS/pcre2-src/src")"                # pcre2posix.h
    [ -f "$out" ] || { echo "[persistent] pcre2: 无 -afl 二进制，跳过"; SKIPPED=$((SKIPPED+1)); return; }
    if [ "$FORCE" != 1 ] && is_persistent "$out"; then echo "[persistent] pcre2: 已持久，跳过"; SKIPPED=$((SKIPPED+1)); return; fi
    [ -n "$src" ] && [ -n "$lib" ] && [ -n "$inc1" ] && [ -n "$inc2" ] || { echo "[persistent] pcre2: 构件缺失，跳过"; SKIPPED=$((SKIPPED+1)); return; }
    AFL_QUIET=1 afl-clang-fast++ -O2 -DPCRE2_CODE_UNIT_WIDTH=8 "$src" -I "$inc1" -I "$inc2" \
        -Wl,--whole-archive "$lib" ${libp:+"$libp"} -Wl,-no-whole-archive "$DRIVER" \
        -o "$out" 2>/dev/null
    finish pcre2 "$out"
}

# --- sqlite（ossfuzz.c + sqlite3.c 合并源，重新编译）---
do_sqlite() {
    local out="$PUBLIC/bin/sqlite-afl/sqlite_fuzzer"
    local dir amalg oss
    dir="$(first "$FTS/sqlite-*")"
    [ -f "$out" ] || { echo "[persistent] sqlite: 无 -afl 二进制，跳过"; SKIPPED=$((SKIPPED+1)); return; }
    if [ "$FORCE" != 1 ] && is_persistent "$out"; then echo "[persistent] sqlite: 已持久，跳过"; SKIPPED=$((SKIPPED+1)); return; fi
    amalg="$dir/sqlite3.c"; oss="$dir/ossfuzz.c"
    [ -n "$dir" ] && [ -f "$amalg" ] && [ -f "$oss" ] || { echo "[persistent] sqlite: 构件缺失，跳过"; SKIPPED=$((SKIPPED+1)); return; }
    local tmp; tmp="$(mktemp -d)"
    AFL_QUIET=1 afl-clang-fast -O2 -DSQLITE_THREADSAFE=0 -DSQLITE_OMIT_LOAD_EXTENSION \
        -c "$amalg" -o "$tmp/sqlite3.o" 2>/dev/null
    AFL_QUIET=1 afl-clang-fast -O2 -I "$dir" -c "$oss" -o "$tmp/ossfuzz.o" 2>/dev/null
    AFL_QUIET=1 afl-clang-fast++ -O2 "$tmp/sqlite3.o" "$tmp/ossfuzz.o" "$DRIVER" \
        -ldl -lpthread -o "$out" 2>/dev/null
    rm -rf "$tmp"
    finish sqlite "$out"
}

do_libarchive
do_pcre2
do_sqlite

echo "[persistent] 完成：$CONVERTED 个转为持久，$SKIPPED 个跳过。"
echo "[persistent] （png/xml 由 compile_public_benchmarks.sh 的 dual-mode heredoc 保证持久。）"
