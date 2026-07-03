#!/usr/bin/env bash
###############################################################################
# 将已构建的 FTS AFL 目标重链接为 AFL 持久模式 + 共享内存输入。
#
# 消除每次执行的 fork()/execve + 文件 I/O；实测吞吐提升 7-35x（png 18x、xml 7.5x）。
# 做法：这些是 LLVMFuzzerTestOneInput（libarchive/pcre2）或 ossfuzz(sqlite) 风格 harness，
# 只需链接 AFL++ 的 libAFLDriver.a（它提供 __AFL_LOOP 持久 main + __AFL_FUZZ_TESTCASE_BUF
# 共享内存输入），无需改 harness 源码。运行后 run_benchmark.py 会自动检测（存在已定义的
# `__afl_sharedmem_fuzzing` 符号）并以"不带 @@"方式驱动它们（afl-fuzz 与 afl-showmap 均然）。
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

# 用 afl-fuzz 自身识别持久模式的标记字符串 ##SIG_AFL_PERSISTENT##（__AFL_LOOP 注入）判断。
# 切勿用 __afl_sharedmem_fuzzing 符号：它由 AFL 运行时在 fork 与持久二进制中都定义，会把
# fork 模式的 cmplog 伴随二进制误判为已持久而跳过重建（详见 run_benchmark._afl_binary_uses_shmem）。
is_persistent() { grep -q -a "##SIG_AFL_PERSISTENT##" "$1" 2>/dev/null; }
# 取匹配某 glob 的首个存在路径（版本目录健壮性）
first() { local p; for p in $1; do [ -e "$p" ] && { echo "$p"; return; }; done; }

CONVERTED=0; SKIPPED=0

# 通用：校验临时产物的持久符号后原子提升到最终路径。链接失败时 linker 会删除其输出
# 文件；若直接写 $out，一次失败的重链接会摧毁原有可用的 fork 模式二进制。故先链到临时
# 文件、校验通过才 mv 覆盖，失败则删临时文件、保留原二进制。
finish() {  # $1=target名 $2=临时产物 $3=最终输出
    if is_persistent "$2"; then
        mv -f "$2" "$3"
        echo "[persistent] $1 -> PERSISTENT ✓"; CONVERTED=$((CONVERTED + 1))
    else
        rm -f "$2"
        echo "[persistent] $1 -> 链接后未检出持久符号，保留原二进制，跳过"
        SKIPPED=$((SKIPPED + 1))
    fi
}

# --- libarchive（LLVMFuzzerTestOneInput，.cc）---
do_libarchive() {
    local src lib inc
    src="$(first "$FTS/libarchive-*/libarchive_fuzzer.cc")"
    lib="$(first "$GFTS/libarchive-src/build_afl/libarchive/libarchive.a")"
    inc="$(first "$GFTS/libarchive-src/libarchive")"
    [ -n "$src" ] && [ -n "$lib" ] && [ -n "$inc" ] || { echo "[persistent] libarchive: 构件缺失，跳过"; SKIPPED=$((SKIPPED+1)); return; }
    # 主 + cmplog 伴随都重建为持久（二者持久性须一致，见 do_pcre2 注释）。
    local spec lbl o clenv t
    for spec in "libarchive|$PUBLIC/bin/libarchive-afl/archive_fuzzer|" \
                "libarchive-cmplog|$PUBLIC/bin/libarchive-cmplog/archive_fuzzer|AFL_LLVM_CMPLOG=1"; do
        IFS='|' read -r lbl o clenv <<< "$spec"
        [ -f "$o" ] || { echo "[persistent] $lbl: 无二进制，跳过"; SKIPPED=$((SKIPPED+1)); continue; }
        if [ "$FORCE" != 1 ] && is_persistent "$o"; then echo "[persistent] $lbl: 已持久，跳过"; SKIPPED=$((SKIPPED+1)); continue; fi
        t="$o.tmp.$$"
        AFL_QUIET=1 ${clenv:+env "$clenv"} afl-clang-fast++ -O2 "$src" -I "$inc" "$lib" "$DRIVER" \
            -lz -lbz2 -llzma -lxml2 -lcrypto -lpthread -o "$t" 2>/dev/null
        finish "$lbl" "$t" "$o"
    done
}

# --- pcre2（LLVMFuzzerTestOneInput，.cc）---
do_pcre2() {
    local src lib libp inc1 inc2
    src="$(first "$FTS/pcre2-*/target.cc")"
    lib="$(first "$GFTS/pcre2-src/build_afl/libpcre2-8.a")"
    libp="$(first "$GFTS/pcre2-src/build_afl/libpcre2posix.a")"
    inc1="$(first "$GFTS/pcre2-src/build_afl")"          # 生成的 pcre2.h
    inc2="$(first "$GFTS/pcre2-src/src")"                # pcre2posix.h
    [ -n "$src" ] && [ -n "$lib" ] && [ -n "$inc1" ] && [ -n "$inc2" ] || { echo "[persistent] pcre2: 构件缺失，跳过"; SKIPPED=$((SKIPPED+1)); return; }
    # 主二进制与 cmplog 伴随都重建为持久：二者持久性必须一致，否则"持久主 + fork cmplog"
    # 会让 afl-fuzz "Fork server handshake failed"、被迫关闭 cmplog（丢 RedQueen）。仅
    # cmplog 版加 AFL_LLVM_CMPLOG=1，编译/链接其余完全相同。
    local spec lbl o clenv t
    for spec in "pcre2|$PUBLIC/bin/pcre2-afl/pcre2_fuzzer|" \
                "pcre2-cmplog|$PUBLIC/bin/pcre2-cmplog/pcre2_fuzzer|AFL_LLVM_CMPLOG=1"; do
        IFS='|' read -r lbl o clenv <<< "$spec"
        [ -f "$o" ] || { echo "[persistent] $lbl: 无二进制，跳过"; SKIPPED=$((SKIPPED+1)); continue; }
        if [ "$FORCE" != 1 ] && is_persistent "$o"; then echo "[persistent] $lbl: 已持久，跳过"; SKIPPED=$((SKIPPED+1)); continue; fi
        t="$o.tmp.$$"
        AFL_QUIET=1 ${clenv:+env "$clenv"} afl-clang-fast++ -O2 -DPCRE2_CODE_UNIT_WIDTH=8 "$src" -I "$inc1" -I "$inc2" \
            -Wl,--whole-archive "$lib" ${libp:+"$libp"} -Wl,-no-whole-archive "$DRIVER" \
            -o "$t" 2>/dev/null
        finish "$lbl" "$t" "$o"
    done
}

# --- sqlite（ossfuzz.c + sqlite3.c 合并源，重新编译）---
do_sqlite() {
    local dir amalg oss
    dir="$(first "$FTS/sqlite-*")"
    amalg="$dir/sqlite3.c"; oss="$dir/ossfuzz.c"
    [ -n "$dir" ] && [ -f "$amalg" ] && [ -f "$oss" ] || { echo "[persistent] sqlite: 构件缺失，跳过"; SKIPPED=$((SKIPPED+1)); return; }
    # 主 + cmplog 伴随都重建为持久；cmplog 版编译期即加 AFL_LLVM_CMPLOG=1（.o 与链接都用）。
    local spec lbl o clenv tmp
    for spec in "sqlite|$PUBLIC/bin/sqlite-afl/sqlite_fuzzer|" \
                "sqlite-cmplog|$PUBLIC/bin/sqlite-cmplog/sqlite_fuzzer|AFL_LLVM_CMPLOG=1"; do
        IFS='|' read -r lbl o clenv <<< "$spec"
        [ -f "$o" ] || { echo "[persistent] $lbl: 无二进制，跳过"; SKIPPED=$((SKIPPED+1)); continue; }
        if [ "$FORCE" != 1 ] && is_persistent "$o"; then echo "[persistent] $lbl: 已持久，跳过"; SKIPPED=$((SKIPPED+1)); continue; fi
        tmp="$(mktemp -d)"
        AFL_QUIET=1 ${clenv:+env "$clenv"} afl-clang-fast -O2 -DSQLITE_THREADSAFE=0 -DSQLITE_OMIT_LOAD_EXTENSION \
            -c "$amalg" -o "$tmp/sqlite3.o" 2>/dev/null
        AFL_QUIET=1 ${clenv:+env "$clenv"} afl-clang-fast -O2 -I "$dir" -c "$oss" -o "$tmp/ossfuzz.o" 2>/dev/null
        AFL_QUIET=1 ${clenv:+env "$clenv"} afl-clang-fast++ -O2 "$tmp/sqlite3.o" "$tmp/ossfuzz.o" "$DRIVER" \
            -ldl -lpthread -o "$tmp/sqlite_fuzzer" 2>/dev/null
        finish "$lbl" "$tmp/sqlite_fuzzer" "$o"
        rm -rf "$tmp"
    done
}

do_libarchive
do_pcre2
do_sqlite

echo "[persistent] 完成：$CONVERTED 个转为持久，$SKIPPED 个跳过。"
echo "[persistent] （png/xml 由 compile_public_benchmarks.sh 的 dual-mode heredoc 保证持久。）"
