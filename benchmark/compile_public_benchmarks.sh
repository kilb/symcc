#!/usr/bin/env bash
#
# Build public benchmark programs for SymCC MPI benchmarking.
#
# Usage:
#   ./build_public_benchmarks.sh [--compiler CC] [--all | --cgc | --lava | --google-fts]
#
# Defaults to gcc if SymCC is not available (for testing the MPI framework).
# Set --compiler to specify the C compiler (e.g., symcc or path/to/symcc).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PUBLIC_DIR="$SCRIPT_DIR/public"
BUILD_DIR="$SCRIPT_DIR/public/bin"
SEEDS_DIR="$SCRIPT_DIR/public/seeds"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

CC="${CC:-gcc}"
CXX="${CXX:-g++}"
_COMPILER_EXPLICIT=false

############################################################
# CGC cb-multios  (243 challenge binaries)
# Source: https://github.com/trailofbits/cb-multios
# SymCC paper used these for primary evaluation
############################################################
build_cgc() {
    local cgc_dir="$PUBLIC_DIR/cb-multios"
    if [ ! -d "$cgc_dir" ]; then
        error "CGC not found at $cgc_dir"
        error "Run: cd $PUBLIC_DIR && git clone --depth 1 https://github.com/trailofbits/cb-multios.git"
        return 1
    fi

    info "Building CGC challenges with CC=$CC ..."
    mkdir -p "$BUILD_DIR/cgc" "$SEEDS_DIR/cgc"

    cd "$cgc_dir"
    mkdir -p build
    cd build

    # CGC programs are 32-bit. Try 32-bit first, fall back to native.
    if $CC -m32 -x c -o /dev/null /dev/null 2>/dev/null; then
        info "  Building in 32-bit mode"
        CC="$CC" CXX="$CXX" cmake .. -DCMAKE_C_FLAGS="-m32" -DCMAKE_CXX_FLAGS="-m32" \
            -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" \
            -DCMAKE_BUILD_TYPE=Release 2>/dev/null || true
    else
        warn "  32-bit compilation not available, trying native..."
        CC="$CC" CXX="$CXX" cmake .. \
            -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" \
            -DCMAKE_BUILD_TYPE=Release 2>/dev/null || true
    fi

    # Build a subset of well-known challenges (fast to compile, good for benchmarking)
    local built=0
    local failed=0
    local targets=(
        # Small, fast programs good for benchmarking
        "NRFIN_00003" "NRFIN_00006" "NRFIN_00009" "NRFIN_00013"
        "CROMU_00001" "CROMU_00003" "CROMU_00007" "CROMU_00014"
        "KPRCA_00001" "KPRCA_00005" "KPRCA_00011"
        "EAGLE_00004" "EAGLE_00005"
        "YAN01_00001" "YAN01_00004" "YAN01_00007" "YAN01_00012"
    )

    for challenge in "${targets[@]}"; do
        if [ -d "$cgc_dir/challenges/$challenge" ]; then
            make "$challenge" -j$(nproc) 2>/dev/null && {
                # Find built binary
                local bin=$(find . -name "$challenge" -type f -executable 2>/dev/null | head -1)
                if [ -n "$bin" ]; then
                    cp "$bin" "$BUILD_DIR/cgc/"
                    built=$((built + 1))
                fi
            } || {
                failed=$((failed + 1))
            }
        fi
    done

    # Generate simple seeds for CGC programs (they read from stdin)
    for bin_file in "$BUILD_DIR/cgc"/*; do
        if [ -f "$bin_file" ]; then
            local name=$(basename "$bin_file")
            mkdir -p "$SEEDS_DIR/cgc/$name"
            # Generate a few small seed inputs
            echo -n "A" > "$SEEDS_DIR/cgc/$name/seed_01"
            printf '\x00\x00\x00\x00' > "$SEEDS_DIR/cgc/$name/seed_02"
            printf '\x41\x42\x43\x44\x45\x46\x47\x48' > "$SEEDS_DIR/cgc/$name/seed_03"
            head -c 64 /dev/urandom > "$SEEDS_DIR/cgc/$name/seed_04" 2>/dev/null || true
        fi
    done

    cd "$SCRIPT_DIR"
    info "CGC: built $built, failed $failed"
}

############################################################
# LAVA-M targets  (base64, md5sum, uniq, who)
# Source: http://panda.moyix.net/~moyix/lava_corpus.tar.xz
# GNU coreutils 8.24 with injected bugs
############################################################
build_lava() {
    # Locate the LAVA-M corpus.
    # Expected layout: .../LAVA-M/{base64,md5sum,uniq,who}/coreutils-8.24-lava-safe/
    local corpus_dir=""
    for candidate in \
        "$PUBLIC_DIR/lava-m/lava_corpus/LAVA-M" \
        "$PUBLIC_DIR/lava-m/LAVA-M" \
        "$PUBLIC_DIR/LAVA-M" \
        "$PUBLIC_DIR/lava_corpus/LAVA-M"; do
        if [ -d "$candidate/base64" ] || [ -d "$candidate/uniq" ]; then
            corpus_dir="$candidate"
            break
        fi
    done
    if [ -z "$corpus_dir" ]; then
        error "LAVA-M corpus not found (base64, md5sum, uniq, who)."
        error "Run: ./setup_public_benchmarks.sh --lava-m"
        return 1
    fi

    info "Building LAVA-M targets from $corpus_dir with CC=$CC ..."
    mkdir -p "$BUILD_DIR/lava-m" "$SEEDS_DIR/lava-m"

    local built=0

    for prog in base64 md5sum uniq who; do
        # Each program has its own coreutils-8.24-lava-safe source tree
        local src_dir=""
        for d in "$corpus_dir/$prog"/coreutils-*; do
            if [ -d "$d" ]; then
                src_dir="$d"
                break
            fi
        done
        if [ -z "$src_dir" ]; then
            warn "  $prog: source tree not found in $corpus_dir/$prog/"
            continue
        fi

        info "  Building $prog ..."
        cd "$src_dir"

        set +e

        # Apply glibc >= 2.28 compatibility patch if needed.
        # coreutils 8.24 uses internal glibc stdio symbols that were
        # privatized in glibc 2.28.  See:
        #   https://lists.gnu.org/archive/html/coreutils/2019-08/msg00011.html
        if ! grep -q '_IO_EOF_SEEN' lib/freadahead.c 2>/dev/null; then
            info "    Applying glibc compatibility patch..."
            for f in lib/freadahead.c lib/freadptr.c lib/freadseek.c \
                     lib/fseeko.c lib/fseterr.c; do
                if [ -f "$f" ]; then
                    sed -i 's/defined _IO_ftrylockfile/defined _IO_EOF_SEEN || defined _IO_ftrylockfile/' "$f"
                fi
            done
            if [ -f lib/mountlist.c ] && ! grep -q 'sys/sysmacros.h' lib/mountlist.c; then
                sed -i '/#include <stdint.h>/a #include <sys/sysmacros.h>' lib/mountlist.c
            fi
            if [ -f lib/stdio-impl.h ] && ! grep -q '_IO_IN_BACKUP' lib/stdio-impl.h; then
                # 在文件头部（include guard 之后）插入补丁，不要插在注释中间
                sed -i '/#include <errno.h>/i \
/* Glibc 2.28 made _IO_IN_BACKUP private. */\
#if !defined _IO_IN_BACKUP \&\& defined _IO_EOF_SEEN\
# define _IO_IN_BACKUP 0x100\
#endif\
' lib/stdio-impl.h
            fi
        fi

        # O_SEARCH 在部分 Linux 系统上未定义，回退为 O_RDONLY
        if ! echo '#include <fcntl.h>' | $CC -E - 2>/dev/null | grep -q O_SEARCH; then
            info "    Patching O_SEARCH (not defined on this system)..."
            for f in lib/fts.c lib/chdir-long.c lib/openat-proc.c \
                     lib/savewd.c lib/save-cwd.c; do
                if [ -f "$f" ] && ! grep -q 'ifndef O_SEARCH' "$f"; then
                    sed -i '1i\#ifndef O_SEARCH\n# define O_SEARCH O_RDONLY\n#endif' "$f"
                fi
            done
        fi

        # coreutils uses autotools.  Clean and re-configure with our compiler.
        if [ -f Makefile ]; then
            make distclean 2>/dev/null || make clean 2>/dev/null || true
        fi

        if [ -f configure ]; then
            chmod +x configure 2>/dev/null || true
            # SymCC 编译的测试程序运行时需要 SYMCC_OUTPUT_DIR 存在，
            # 否则 configure 的 "can the compiler produce executables" 测试会失败。
            # SymCC 基于 Clang，对隐式函数声明报错，需要 -Wno-implicit-function-declaration
            #
            # 关键：禁用 coreutils 的 unlocked-io 优化。
            # coreutils 的 gnulib 用 fread_unlocked/fwrite_unlocked/getc_unlocked 等
            # 替代标准 I/O 函数，但 SymCC 运行时只包装标准版本（fread_symbolized 等）。
            # 不禁用的话，所有输入读取绕过 SymCC，导致 0 个符号约束、0 个测试用例。
            mkdir -p /tmp/output

            # Patch unlocked-io.h：用空文件替换，禁用所有 *_unlocked 宏替换
            if [ -f lib/unlocked-io.h ]; then
                info "    Patching unlocked-io.h for SymCC compatibility..."
                cat > lib/unlocked-io.h << 'UNLOCKED_PATCH'
/* Patched for SymCC: disable *_unlocked I/O replacements.
   SymCC only wraps standard libc I/O functions (fread, fwrite, getc, etc.),
   not their *_unlocked variants. Using unlocked versions bypasses SymCC's
   symbolic input tracking, resulting in zero test case generation. */
#ifndef UNLOCKED_IO_H
# define UNLOCKED_IO_H 1
# include <stdio.h>
/* Map unlocked -> standard (reverse of original) */
# define clearerr_unlocked(x) clearerr(x)
# define feof_unlocked(x) feof(x)
# define ferror_unlocked(x) ferror(x)
# define fflush_unlocked(x) fflush(x)
# define fgets_unlocked(x,y,z) fgets(x,y,z)
# define fputc_unlocked(x,y) fputc(x,y)
# define fputs_unlocked(x,y) fputs(x,y)
# define fread_unlocked(w,x,y,z) fread(w,x,y,z)
# define fwrite_unlocked(w,x,y,z) fwrite(w,x,y,z)
# define getc_unlocked(x) getc(x)
# define getchar_unlocked() getchar()
# define putc_unlocked(x,y) putc(x,y)
# define putchar_unlocked(x) putchar(x)
#endif
UNLOCKED_PATCH
            fi

            CC="$CC" CFLAGS="-O2 -Wno-implicit-function-declaration" \
                FORCE_UNSAFE_CONFIGURE=1 \
                SYMCC_OUTPUT_DIR=/tmp/output \
                ./configure --quiet 2>&1 | tail -5
            rm -rf /tmp/output/*
        else
            warn "    $prog: no configure script found"
            set -e
            cd "$SCRIPT_DIR"
            continue
        fi

        # 1) 先生成必要的头文件（configmake.h, .version 等）
        # 2) 从顶层目录 make -k 构建，跳过不相关的 lib 编译错误
        # SYMCC_OUTPUT_DIR 在 make 阶段也需要，因为 libtool 链接测试可能运行程序
        mkdir -p /tmp/output
        SYMCC_OUTPUT_DIR=/tmp/output make -k -j$(nproc) 2>&1 | tail -5
        set -e

        # Check for binary
        if [ -f "src/$prog" ]; then
            cp "src/$prog" "$BUILD_DIR/lava-m/${prog}"
            built=$((built + 1))
            info "    -> $prog built successfully"
            # 生成 .args 文件：LAVA-M 程序需要特定参数才能触发丰富的分支
            # base64 需要 -d（解码模式），否则只走编码路径（7%→21% 覆盖率）
            case "$prog" in
                base64) echo "-d" > "$BUILD_DIR/lava-m/${prog}.args" ;;
            esac
        else
            warn "    $prog: binary not found at src/$prog"
        fi

        # Copy seeds from the corpus (fuzzer_input/)
        mkdir -p "$SEEDS_DIR/lava-m/$prog"
        local seed_src="$corpus_dir/$prog/fuzzer_input"
        if [ -d "$seed_src" ]; then
            cp "$seed_src"/* "$SEEDS_DIR/lava-m/$prog/" 2>/dev/null || true
            local nseed=$(ls "$SEEDS_DIR/lava-m/$prog" 2>/dev/null | wc -l)
            info "    Seeds: $nseed files from corpus"
        else
            echo "test input data" > "$SEEDS_DIR/lava-m/$prog/seed_01"
            printf 'AAAAAAAAAAAAAAAA' > "$SEEDS_DIR/lava-m/$prog/seed_02"
        fi

        cd "$SCRIPT_DIR"
    done

    info "LAVA-M: built $built / 4 targets"
}

############################################################
# Google fuzzer-test-suite targets
# Libraries built from source with SymCC for instrumentation,
# with standalone file-reading harnesses.
# Requires internet access to download source tarballs.
############################################################
build_google_fts() {
    info "Building Google fuzzer-test-suite targets with CC=$CC ..."
    mkdir -p "$BUILD_DIR/google-fts" "$SEEDS_DIR/google-fts"

    local built=0
    local work_dir="$PUBLIC_DIR/gfts_build"
    mkdir -p "$work_dir"

    # --- libpng ---
    build_libpng() {
        info "  Building libpng ..."
        cd "$work_dir"
        local tarball="libpng-1.2.56.tar.gz"
        if [ ! -f "$tarball" ]; then
            curl -sL "https://downloads.sourceforge.net/project/libpng/libpng12/older-releases/1.2.56/$tarball" -o "$tarball" || { warn "  Failed to download libpng"; return 1; }
        fi
        rm -rf libpng-1.2.56
        tar xf "$tarball"
        cd libpng-1.2.56

        # CRC bypass: 禁用 CRC 校验，让 SymCC 变异的 chunk 数据不被拒绝
        if [ -f pngrutil.c ] && ! grep -q 'SYMCC_SKIP_CRC' pngrutil.c; then
            info "    Patching CRC check for SymCC exploration..."
            sed -i 's/if (need_crc)/if (need_crc \&\& !getenv("SYMCC_SKIP_CRC"))/' pngrutil.c 2>/dev/null || true
            # 更直接的方法：让 png_crc_error 总是返回 0（无错误）
            sed -i 's/return ((png_ptr->flags & PNG_FLAG_CRC_CRITICAL_MASK) ==/if (getenv("SYMCC_SKIP_CRC")) return 0; return ((png_ptr->flags \& PNG_FLAG_CRC_CRITICAL_MASK) ==/' pngrutil.c 2>/dev/null || true
        fi

        mkdir -p /tmp/output
        CC="$CC" SYMCC_OUTPUT_DIR=/tmp/output ./configure --quiet --disable-shared 2>/dev/null && make -j$(nproc) 2>/dev/null || { warn "  libpng: make failed"; return 1; }
        rm -rf /tmp/output/*

        # Create standalone harness
        cat > /tmp/png_read_fuzzer.c << 'HARNESS_EOF'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "png.h"
struct BufState { const uint8_t *data; size_t bytes_left; };
static void user_read_data(png_structp p, png_bytep d, png_size_t l) {
    struct BufState *b = (struct BufState *)png_get_io_ptr(p);
    if (l > b->bytes_left) png_error(p, "read error");
    memcpy(d, b->data, l); b->bytes_left -= l; b->data += l;
}
int main(int argc, char *argv[]) {
    if (argc != 2) return 1;
    FILE *f = fopen(argv[1], "rb"); if (!f) return 1;
    fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
    if (sz <= 0 || sz > 10*1024*1024) { fclose(f); return 1; }
    uint8_t *data = malloc(sz); fread(data, 1, sz, f); fclose(f);
    if (sz < 8 || png_sig_cmp(data, 0, 8)) { free(data); return 0; }
    png_structp pp = png_create_read_struct(PNG_LIBPNG_VER_STRING, NULL, NULL, NULL);
    png_infop ip = png_create_info_struct(pp);
    if (setjmp(png_jmpbuf(pp))) { png_destroy_read_struct(&pp, &ip, NULL); free(data); return 0; }
    struct BufState bs = { data + 8, sz - 8 };
    png_set_read_fn(pp, &bs, user_read_data); png_set_sig_bytes(pp, 8);
    png_read_info(pp, ip);
    png_uint_32 w, h; int bd, ct;
    png_get_IHDR(pp, ip, &w, &h, &bd, &ct, NULL, NULL, NULL);
    if (h * w > 1000000) { png_destroy_read_struct(&pp, &ip, NULL); free(data); return 0; }
    /* 启用颜色变换以覆盖 pngrtran.c 代码路径 */
    png_set_expand(pp);           /* palette→RGB, gray 1/2/4→8, tRNS→alpha */
    png_set_gray_to_rgb(pp);      /* grayscale→RGB */
    png_set_strip_16(pp);         /* 16-bit→8-bit */
    png_set_add_alpha(pp, 0xFF, PNG_FILLER_AFTER); /* 添加 alpha 通道 */
    png_set_gamma(pp, 2.2, 0.45455); /* gamma 校正 */
    png_read_update_info(pp, ip); /* 应用变换 */
    int passes = png_set_interlace_handling(pp);
    png_bytep row = png_malloc(pp, png_get_rowbytes(pp, ip));
    for (int p2 = 0; p2 < passes; p2++) for (png_uint_32 y = 0; y < h; y++) png_read_row(pp, row, NULL);
    png_free(pp, row); png_destroy_read_struct(&pp, &ip, NULL); free(data); return 0;
}
HARNESS_EOF
        "$CC" -O2 /tmp/png_read_fuzzer.c -I . .libs/libpng.a -lz -lm -o "$BUILD_DIR/google-fts/png_read_fuzzer" 2>/dev/null || { warn "  libpng harness link failed"; return 1; }
        rm -f /tmp/png_read_fuzzer.c

        # Create seeds (minimal valid PNGs)
        mkdir -p "$SEEDS_DIR/google-fts/png_read_fuzzer"
        python3 -c "
import struct, zlib
sig = b'\x89PNG\r\n\x1a\n'
ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
ihdr_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr_data) & 0xFFFFFFFF)
ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + ihdr_crc
raw = b'\x00\xff\xff\xff'
compressed = zlib.compress(raw)
idat_crc = struct.pack('>I', zlib.crc32(b'IDAT' + compressed) & 0xFFFFFFFF)
idat = struct.pack('>I', len(compressed)) + b'IDAT' + compressed + idat_crc
iend_crc = struct.pack('>I', zlib.crc32(b'IEND') & 0xFFFFFFFF)
iend = struct.pack('>I', 0) + b'IEND' + iend_crc
open('$SEEDS_DIR/google-fts/png_read_fuzzer/seed_01.png', 'wb').write(sig + ihdr + idat + iend)
# 2x2 RGBA
ihdr2 = struct.pack('>IIBBBBB', 2, 2, 8, 6, 0, 0, 0)
ihdr2_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr2) & 0xFFFFFFFF)
ihdr2_chunk = struct.pack('>I', 13) + b'IHDR' + ihdr2 + ihdr2_crc
raw2 = b'\x00' + b'\xff\x00\x00\xff' * 2 + b'\x00' + b'\x00\xff\x00\xff' * 2
c2 = zlib.compress(raw2)
idat2_crc = struct.pack('>I', zlib.crc32(b'IDAT' + c2) & 0xFFFFFFFF)
idat2 = struct.pack('>I', len(c2)) + b'IDAT' + c2 + idat2_crc
open('$SEEDS_DIR/google-fts/png_read_fuzzer/seed_02.png', 'wb').write(sig + ihdr2_chunk + idat2 + iend)
# Grayscale 8-bit
ihdr3 = struct.pack('>IIBBBBB', 4, 4, 8, 0, 0, 0, 0)
ihdr3_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr3) & 0xFFFFFFFF)
ihdr3_chunk = struct.pack('>I', 13) + b'IHDR' + ihdr3 + ihdr3_crc
raw3 = b''.join(b'\x00' + bytes([i*64]*4) for i in range(4))
c3 = zlib.compress(raw3)
idat3_crc = struct.pack('>I', zlib.crc32(b'IDAT' + c3) & 0xFFFFFFFF)
idat3 = struct.pack('>I', len(c3)) + b'IDAT' + c3 + idat3_crc
open('$SEEDS_DIR/google-fts/png_read_fuzzer/seed_03_gray.png', 'wb').write(sig + ihdr3_chunk + idat3 + iend)
# 16-bit RGB
ihdr4 = struct.pack('>IIBBBBB', 2, 2, 16, 2, 0, 0, 0)
ihdr4_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr4) & 0xFFFFFFFF)
ihdr4_chunk = struct.pack('>I', 13) + b'IHDR' + ihdr4 + ihdr4_crc
raw4 = b''.join(b'\x00' + b'\xff\x00\x80\x00\x40\x00' * 2 for _ in range(2))
c4 = zlib.compress(raw4)
idat4_crc = struct.pack('>I', zlib.crc32(b'IDAT' + c4) & 0xFFFFFFFF)
idat4 = struct.pack('>I', len(c4)) + b'IDAT' + c4 + idat4_crc
open('$SEEDS_DIR/google-fts/png_read_fuzzer/seed_04_16bit.png', 'wb').write(sig + ihdr4_chunk + idat4 + iend)
# Palette PNG
plte = b'\xff\x00\x00\x00\xff\x00\x00\x00\xff\xff\xff\x00'
plte_crc = struct.pack('>I', zlib.crc32(b'PLTE' + plte) & 0xFFFFFFFF)
plte_chunk = struct.pack('>I', len(plte)) + b'PLTE' + plte + plte_crc
ihdr5 = struct.pack('>IIBBBBB', 4, 4, 8, 3, 0, 0, 0)
ihdr5_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr5) & 0xFFFFFFFF)
ihdr5_chunk = struct.pack('>I', 13) + b'IHDR' + ihdr5 + ihdr5_crc
raw5 = b''.join(b'\x00' + bytes([i % 4]*4) for i in range(4))
c5 = zlib.compress(raw5)
idat5_crc = struct.pack('>I', zlib.crc32(b'IDAT' + c5) & 0xFFFFFFFF)
idat5 = struct.pack('>I', len(c5)) + b'IDAT' + c5 + idat5_crc
open('$SEEDS_DIR/google-fts/png_read_fuzzer/seed_05_palette.png', 'wb').write(sig + ihdr5_chunk + plte_chunk + idat5 + iend)
# Grayscale with Alpha
ihdr6 = struct.pack('>IIBBBBB', 2, 2, 8, 4, 0, 0, 0)
ihdr6_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr6) & 0xFFFFFFFF)
ihdr6_chunk = struct.pack('>I', 13) + b'IHDR' + ihdr6 + ihdr6_crc
raw6 = b'\x00\xff\x80\x00\x80' + b'\x00\x80\xff\xff\x00'
c6 = zlib.compress(raw6)
idat6_crc = struct.pack('>I', zlib.crc32(b'IDAT' + c6) & 0xFFFFFFFF)
idat6 = struct.pack('>I', len(c6)) + b'IDAT' + c6 + idat6_crc
open('$SEEDS_DIR/google-fts/png_read_fuzzer/seed_06_gray_alpha.png', 'wb').write(sig + ihdr6_chunk + idat6 + iend)
" 2>/dev/null
        info "    -> png_read_fuzzer built successfully"
        built=$((built + 1))
        cd "$SCRIPT_DIR"
    }

    # --- libxml2 ---
    build_libxml2() {
        info "  Building libxml2 ..."
        cd "$work_dir"
        local tarball="libxml2-2.9.2.tar.xz"
        if [ ! -f "$tarball" ]; then
            # 使用 GNOME 官方 FTP 发布包（包含预生成的 configure）
            curl -sL "https://download.gnome.org/sources/libxml2/2.9/libxml2-2.9.2.tar.xz" -o "$tarball" || \
            curl -sL "https://github.com/nicerloop/libxml2/releases/download/v2.9.2/libxml2-2.9.2.tar.xz" -o "$tarball" || \
            { warn "  Failed to download libxml2"; return 1; }
        fi
        rm -rf libxml2-2.9.2
        tar xf "$tarball"
        cd libxml2-2.9.2
        mkdir -p /tmp/output
        CC="$CC" SYMCC_OUTPUT_DIR=/tmp/output ./configure --quiet --disable-shared --without-python --without-threads --without-lzma 2>/dev/null && make -j$(nproc) 2>/dev/null || { warn "  libxml2: make failed"; return 1; }
        rm -rf /tmp/output/*

        # Create standalone harness
        cat > /tmp/xml_read_fuzzer.c << 'HARNESS_EOF'
#include <stdio.h>
#include <stdlib.h>
#include <libxml/parser.h>
#include <libxml/tree.h>
#include <libxml/xpath.h>
#include <libxml/xinclude.h>
#include <libxml/xmlschemas.h>
int main(int argc, char *argv[]) {
    if (argc != 2) return 1;
    FILE *f = fopen(argv[1], "rb"); if (!f) return 1;
    fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
    if (sz <= 0 || sz > 1024*1024) { fclose(f); return 1; }
    char *data = malloc(sz); fread(data, 1, sz, f); fclose(f);
    xmlInitParser();
    /* 启用 DTD 验证 + 实体替换 + XInclude */
    int flags = XML_PARSE_NONET | XML_PARSE_RECOVER | XML_PARSE_NOERROR
              | XML_PARSE_NOWARNING | XML_PARSE_DTDLOAD | XML_PARSE_DTDVALID
              | XML_PARSE_NOENT;
    xmlDocPtr doc = xmlReadMemory(data, sz, "input.xml", NULL, flags);
    if (doc) {
        /* XInclude 处理 */
        xmlXIncludeProcess(doc);
        /* XPath 查询以覆盖 XPath 引擎代码路径 */
        xmlXPathContextPtr ctx = xmlXPathNewContext(doc);
        if (ctx) {
            xmlXPathObjectPtr res = xmlXPathEvalExpression(
                (const xmlChar *)"//node()", ctx);
            if (res) xmlXPathFreeObject(res);
            res = xmlXPathEvalExpression(
                (const xmlChar *)"count(//*)", ctx);
            if (res) xmlXPathFreeObject(res);
            xmlXPathFreeContext(ctx);
        }
        /* DTD 验证 */
        xmlValidCtxtPtr vctx = xmlNewValidCtxt();
        if (vctx) {
            xmlValidateDocument(vctx, doc);
            xmlFreeValidCtxt(vctx);
        }
        xmlFreeDoc(doc);
    }
    xmlCleanupParser(); free(data); return 0;
}
HARNESS_EOF
        "$CC" -O2 /tmp/xml_read_fuzzer.c -I include -I include/libxml .libs/libxml2.a -lz -lm -lpthread -o "$BUILD_DIR/google-fts/xml_read_fuzzer" 2>/dev/null || { warn "  libxml2 harness link failed"; return 1; }
        rm -f /tmp/xml_read_fuzzer.c

        # Create seeds
        mkdir -p "$SEEDS_DIR/google-fts/xml_read_fuzzer"
        echo '<?xml version="1.0"?><root><item>test</item></root>' > "$SEEDS_DIR/google-fts/xml_read_fuzzer/seed_01.xml"
        echo '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE t [<!ENTITY f "bar">]><root a="v"><c>&f;</c></root>' > "$SEEDS_DIR/google-fts/xml_read_fuzzer/seed_02.xml"
        echo '<html><head><title>t</title></head><body><p>hello</p></body></html>' > "$SEEDS_DIR/google-fts/xml_read_fuzzer/seed_03.xml"
        # DTD 验证种子
        echo '<?xml version="1.0"?><!DOCTYPE note [<!ELEMENT note (to,from,body)><!ELEMENT to (#PCDATA)><!ELEMENT from (#PCDATA)><!ELEMENT body (#PCDATA)>]><note><to>A</to><from>B</from><body>C</body></note>' > "$SEEDS_DIR/google-fts/xml_read_fuzzer/seed_04_dtd.xml"
        # XPath 查询种子（深层嵌套）
        echo '<?xml version="1.0"?><a><b id="1"><c><d>deep</d></c></b><b id="2"><c>shallow</c></b></a>' > "$SEEDS_DIR/google-fts/xml_read_fuzzer/seed_05_xpath.xml"
        # 命名空间种子
        echo '<?xml version="1.0"?><root xmlns:ns="http://example.com"><ns:item ns:attr="val">namespaced</ns:item></root>' > "$SEEDS_DIR/google-fts/xml_read_fuzzer/seed_06_ns.xml"
        # CDATA 和混合内容
        echo '<?xml version="1.0"?><doc><![CDATA[<not>xml</not>]]><p>mixed <b>bold</b> text</p></doc>' > "$SEEDS_DIR/google-fts/xml_read_fuzzer/seed_07_cdata.xml"
        # 多实体和属性
        echo '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "alpha"><!ENTITY b "beta">]><r x="1" y="2" z="3">&a;&b;</r>' > "$SEEDS_DIR/google-fts/xml_read_fuzzer/seed_08_entity.xml"
        info "    -> xml_read_fuzzer built successfully"
        built=$((built + 1))
        cd "$SCRIPT_DIR"
    }

    # Build each target (errors don't abort script)
    set +e
    build_libpng
    build_libxml2
    set -e

    info "Google FTS: built $built targets"
}

############################################################
# Coverage builds (gcc --coverage -O0 -g)
# 输出到 <suite>-cov/ 目录，并写入 .covdir/.covsrc 元数据
############################################################
build_lava_coverage() {
    local corpus_dir=""
    for candidate in \
        "$PUBLIC_DIR/lava-m/lava_corpus/LAVA-M" \
        "$PUBLIC_DIR/lava-m/LAVA-M" \
        "$PUBLIC_DIR/LAVA-M" \
        "$PUBLIC_DIR/lava_corpus/LAVA-M"; do
        if [ -d "$candidate/base64" ] || [ -d "$candidate/uniq" ]; then
            corpus_dir="$candidate"
            break
        fi
    done
    if [ -z "$corpus_dir" ]; then
        warn "Coverage: LAVA-M corpus not found, skipping"
        return 0
    fi

    info "Building LAVA-M coverage binaries..."
    mkdir -p "$BUILD_DIR/lava-m-cov"

    local built=0
    for prog in base64 md5sum uniq who; do
        local src_dir=""
        for d in "$corpus_dir/$prog"/coreutils-*; do
            if [ -d "$d" ]; then
                src_dir="$d"
                break
            fi
        done
        if [ -z "$src_dir" ]; then
            warn "  Coverage: $prog source tree not found"
            continue
        fi

        info "  Building $prog (coverage)..."
        cd "$src_dir"
        set +e

        # 清理之前的构建（补丁已经应用在源文件中，distclean 不会还原）
        if [ -f Makefile ]; then
            make distclean 2>/dev/null || make clean 2>/dev/null || true
        fi

        if [ -f configure ]; then
            chmod +x configure 2>/dev/null || true
            CC=gcc CFLAGS="--coverage -O0 -g" LDFLAGS="--coverage" \
                FORCE_UNSAFE_CONFIGURE=1 ./configure --quiet 2>&1 | tail -3
        else
            warn "    $prog: no configure script"
            set -e
            cd "$SCRIPT_DIR"
            continue
        fi

        make -k -j$(nproc) 2>&1 | tail -3
        set -e

        if [ -f "src/$prog" ]; then
            cp "src/$prog" "$BUILD_DIR/lava-m-cov/${prog}"
            # 写入覆盖率元数据：gcov 需要知道构建目录和源文件路径
            echo "$src_dir" > "$BUILD_DIR/lava-m-cov/${prog}.covdir"
            echo "src/${prog}.c" > "$BUILD_DIR/lava-m-cov/${prog}.covsrc"
            built=$((built + 1))
            info "    -> $prog coverage binary built"
        else
            warn "    $prog: coverage binary not found"
        fi

        cd "$SCRIPT_DIR"
    done

    info "LAVA-M coverage: built $built / 4"
}

build_google_fts_coverage() {
    local work_dir="$PUBLIC_DIR/gfts_build"

    info "Building Google FTS coverage binaries..."
    mkdir -p "$BUILD_DIR/google-fts-cov"

    local built=0

    # --- libpng coverage ---
    if [ -d "$work_dir/libpng-1.2.56" ]; then
        info "  Building libpng (coverage)..."
        cd "$work_dir/libpng-1.2.56"
        set +e
        make distclean 2>/dev/null || make clean 2>/dev/null || true
        CC=gcc CFLAGS="--coverage -O0 -g" LDFLAGS="--coverage" \
            ./configure --quiet --disable-shared 2>/dev/null && \
            make -j$(nproc) 2>/dev/null
        if [ $? -eq 0 ] && [ -f .libs/libpng.a ]; then
            # 创建临时 harness 文件用于 coverage 编译
            local cov_dir="$BUILD_DIR/google-fts-cov/png_cov"
            mkdir -p "$cov_dir"
            cat > "$cov_dir/png_read_fuzzer.c" << 'HARNESS_EOF'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "png.h"
struct BufState { const uint8_t *data; size_t bytes_left; };
static void user_read_data(png_structp p, png_bytep d, png_size_t l) {
    struct BufState *b = (struct BufState *)png_get_io_ptr(p);
    if (l > b->bytes_left) png_error(p, "read error");
    memcpy(d, b->data, l); b->bytes_left -= l; b->data += l;
}
int main(int argc, char *argv[]) {
    if (argc != 2) return 1;
    FILE *f = fopen(argv[1], "rb"); if (!f) return 1;
    fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
    if (sz <= 0 || sz > 10*1024*1024) { fclose(f); return 1; }
    uint8_t *data = malloc(sz); fread(data, 1, sz, f); fclose(f);
    if (sz < 8 || png_sig_cmp(data, 0, 8)) { free(data); return 0; }
    png_structp pp = png_create_read_struct(PNG_LIBPNG_VER_STRING, NULL, NULL, NULL);
    png_infop ip = png_create_info_struct(pp);
    if (setjmp(png_jmpbuf(pp))) { png_destroy_read_struct(&pp, &ip, NULL); free(data); return 0; }
    struct BufState bs = { data + 8, sz - 8 };
    png_set_read_fn(pp, &bs, user_read_data); png_set_sig_bytes(pp, 8);
    png_read_info(pp, ip);
    png_uint_32 w, h; int bd, ct;
    png_get_IHDR(pp, ip, &w, &h, &bd, &ct, NULL, NULL, NULL);
    if (h * w > 1000000) { png_destroy_read_struct(&pp, &ip, NULL); free(data); return 0; }
    /* 启用颜色变换以覆盖 pngrtran.c 代码路径 */
    png_set_expand(pp);           /* palette→RGB, gray 1/2/4→8, tRNS→alpha */
    png_set_gray_to_rgb(pp);      /* grayscale→RGB */
    png_set_strip_16(pp);         /* 16-bit→8-bit */
    png_set_add_alpha(pp, 0xFF, PNG_FILLER_AFTER); /* 添加 alpha 通道 */
    png_set_gamma(pp, 2.2, 0.45455); /* gamma 校正 */
    png_read_update_info(pp, ip); /* 应用变换 */
    int passes = png_set_interlace_handling(pp);
    png_bytep row = png_malloc(pp, png_get_rowbytes(pp, ip));
    for (int p2 = 0; p2 < passes; p2++) for (png_uint_32 y = 0; y < h; y++) png_read_row(pp, row, NULL);
    png_free(pp, row); png_destroy_read_struct(&pp, &ip, NULL); free(data); return 0;
}
HARNESS_EOF
            gcc --coverage -O0 -g "$cov_dir/png_read_fuzzer.c" \
                -I "$work_dir/libpng-1.2.56" \
                "$work_dir/libpng-1.2.56/.libs/libpng.a" \
                -lz -lm -o "$BUILD_DIR/google-fts-cov/png_read_fuzzer" 2>/dev/null
            if [ $? -eq 0 ]; then
                # .gcno 生成在 -o 所在目录，用 .gcno 文件名作为 gcov 参数
                local gcno_file=$(ls "$BUILD_DIR/google-fts-cov/"*png_read_fuzzer*.gcno 2>/dev/null | head -1)
                echo "$BUILD_DIR/google-fts-cov" > "$BUILD_DIR/google-fts-cov/png_read_fuzzer.covdir"
                echo "$(basename "$gcno_file")" > "$BUILD_DIR/google-fts-cov/png_read_fuzzer.covsrc"
                # 记录库构建目录，用于 lcov 采集完整库覆盖率
                echo "$work_dir/libpng-1.2.56" > "$BUILD_DIR/google-fts-cov/png_read_fuzzer.covlibdirs"
                built=$((built + 1))
                info "    -> png_read_fuzzer coverage built"
            else
                warn "    png_read_fuzzer coverage link failed"
            fi
        else
            warn "    libpng coverage build failed"
        fi
        set -e
        cd "$SCRIPT_DIR"
    fi

    # --- libxml2 coverage ---
    if [ -d "$work_dir/libxml2-2.9.2" ]; then
        info "  Building libxml2 (coverage)..."
        cd "$work_dir/libxml2-2.9.2"
        set +e
        make distclean 2>/dev/null || make clean 2>/dev/null || true
        CC=gcc CFLAGS="--coverage -O0 -g" LDFLAGS="--coverage" \
            ./configure --quiet --disable-shared --without-python \
            --without-threads --without-lzma 2>/dev/null && \
            make -j$(nproc) 2>/dev/null
        if [ $? -eq 0 ] && [ -f .libs/libxml2.a ]; then
            local cov_dir="$BUILD_DIR/google-fts-cov/xml_cov"
            mkdir -p "$cov_dir"
            cat > "$cov_dir/xml_read_fuzzer.c" << 'HARNESS_EOF'
#include <stdio.h>
#include <stdlib.h>
#include <libxml/parser.h>
#include <libxml/tree.h>
#include <libxml/xpath.h>
#include <libxml/xinclude.h>
#include <libxml/xmlschemas.h>
int main(int argc, char *argv[]) {
    if (argc != 2) return 1;
    FILE *f = fopen(argv[1], "rb"); if (!f) return 1;
    fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
    if (sz <= 0 || sz > 1024*1024) { fclose(f); return 1; }
    char *data = malloc(sz); fread(data, 1, sz, f); fclose(f);
    xmlInitParser();
    /* 启用 DTD 验证 + 实体替换 + XInclude */
    int flags = XML_PARSE_NONET | XML_PARSE_RECOVER | XML_PARSE_NOERROR
              | XML_PARSE_NOWARNING | XML_PARSE_DTDLOAD | XML_PARSE_DTDVALID
              | XML_PARSE_NOENT;
    xmlDocPtr doc = xmlReadMemory(data, sz, "input.xml", NULL, flags);
    if (doc) {
        /* XInclude 处理 */
        xmlXIncludeProcess(doc);
        /* XPath 查询以覆盖 XPath 引擎代码路径 */
        xmlXPathContextPtr ctx = xmlXPathNewContext(doc);
        if (ctx) {
            xmlXPathObjectPtr res = xmlXPathEvalExpression(
                (const xmlChar *)"//node()", ctx);
            if (res) xmlXPathFreeObject(res);
            res = xmlXPathEvalExpression(
                (const xmlChar *)"count(//*)", ctx);
            if (res) xmlXPathFreeObject(res);
            xmlXPathFreeContext(ctx);
        }
        /* DTD 验证 */
        xmlValidCtxtPtr vctx = xmlNewValidCtxt();
        if (vctx) {
            xmlValidateDocument(vctx, doc);
            xmlFreeValidCtxt(vctx);
        }
        xmlFreeDoc(doc);
    }
    xmlCleanupParser(); free(data); return 0;
}
HARNESS_EOF
            gcc --coverage -O0 -g "$cov_dir/xml_read_fuzzer.c" \
                -I "$work_dir/libxml2-2.9.2/include" \
                "$work_dir/libxml2-2.9.2/.libs/libxml2.a" \
                -lz -lm -lpthread -o "$BUILD_DIR/google-fts-cov/xml_read_fuzzer" 2>/dev/null
            if [ $? -eq 0 ]; then
                local gcno_file=$(ls "$BUILD_DIR/google-fts-cov/"*xml_read_fuzzer*.gcno 2>/dev/null | head -1)
                echo "$BUILD_DIR/google-fts-cov" > "$BUILD_DIR/google-fts-cov/xml_read_fuzzer.covdir"
                echo "$(basename "$gcno_file")" > "$BUILD_DIR/google-fts-cov/xml_read_fuzzer.covsrc"
                # 记录库构建目录，用于 lcov 采集完整库覆盖率
                echo "$work_dir/libxml2-2.9.2" > "$BUILD_DIR/google-fts-cov/xml_read_fuzzer.covlibdirs"
                built=$((built + 1))
                info "    -> xml_read_fuzzer coverage built"
            else
                warn "    xml_read_fuzzer coverage link failed"
            fi
        else
            warn "    libxml2 coverage build failed"
        fi
        set -e
        cd "$SCRIPT_DIR"
    fi

    info "Google FTS coverage: built $built targets"
}

############################################################
# AFL-instrumented builds (afl-clang-fast)
# 用于 hybrid fuzzing: AFL + SymCC 协同
############################################################
build_google_fts_afl() {
    if ! command -v afl-clang-fast >/dev/null 2>&1; then
        error "afl-clang-fast not found. Install AFL++"
        return 1
    fi

    info "Building Google FTS AFL-instrumented binaries..."
    mkdir -p "$BUILD_DIR/google-fts-afl"

    local built=0
    local work_dir="$PUBLIC_DIR/gfts_build"

    # --- libpng AFL ---
    if [ -d "$work_dir/libpng-1.2.56" ]; then
        info "  Building libpng (AFL)..."
        cd "$work_dir/libpng-1.2.56"
        set +e
        make distclean 2>/dev/null || make clean 2>/dev/null || true
        CC=afl-clang-fast CFLAGS="-O2" \
            ./configure --quiet --disable-shared 2>/dev/null && \
            make -j$(nproc) 2>/dev/null
        if [ $? -eq 0 ] && [ -f .libs/libpng.a ]; then
            afl-clang-fast -O2 "$BUILD_DIR/google-fts-cov/png_cov/png_read_fuzzer.c" \
                -I "$work_dir/libpng-1.2.56" \
                "$work_dir/libpng-1.2.56/.libs/libpng.a" \
                -lz -lm -o "$BUILD_DIR/google-fts-afl/png_read_fuzzer" 2>/dev/null
            if [ $? -eq 0 ]; then
                built=$((built + 1))
                info "    -> png_read_fuzzer AFL built"
            else
                warn "    png_read_fuzzer AFL link failed"
            fi
        else
            warn "    libpng AFL build failed"
        fi
        set -e
        cd "$SCRIPT_DIR"
    else
        warn "  libpng source not found; run --google-fts first to download"
    fi

    # --- libxml2 AFL ---
    if [ -d "$work_dir/libxml2-2.9.2" ]; then
        info "  Building libxml2 (AFL)..."
        cd "$work_dir/libxml2-2.9.2"
        set +e
        make distclean 2>/dev/null || make clean 2>/dev/null || true
        CC=afl-clang-fast CFLAGS="-O2" \
            ./configure --quiet --disable-shared --without-python \
            --without-threads --without-lzma 2>/dev/null && \
            make -j$(nproc) 2>/dev/null
        if [ $? -eq 0 ] && [ -f .libs/libxml2.a ]; then
            afl-clang-fast -O2 "$BUILD_DIR/google-fts-cov/xml_cov/xml_read_fuzzer.c" \
                -I "$work_dir/libxml2-2.9.2/include" \
                "$work_dir/libxml2-2.9.2/.libs/libxml2.a" \
                -lz -lm -lpthread -o "$BUILD_DIR/google-fts-afl/xml_read_fuzzer" 2>/dev/null
            if [ $? -eq 0 ]; then
                built=$((built + 1))
                info "    -> xml_read_fuzzer AFL built"
            else
                warn "    xml_read_fuzzer AFL link failed"
            fi
        else
            warn "    libxml2 AFL build failed"
        fi
        set -e
        cd "$SCRIPT_DIR"
    else
        warn "  libxml2 source not found; run --google-fts first to download"
    fi

    info "Google FTS AFL: built $built targets"
}

############################################################
# LAVA-M AFL-instrumented builds
############################################################
build_lava_afl() {
    if ! command -v afl-clang-fast >/dev/null 2>&1; then
        error "afl-clang-fast not found. Install AFL++"
        return 1
    fi

    local corpus_dir=""
    for candidate in \
        "$PUBLIC_DIR/lava-m/lava_corpus/LAVA-M" \
        "$PUBLIC_DIR/lava-m/LAVA-M" \
        "$PUBLIC_DIR/LAVA-M" \
        "$PUBLIC_DIR/lava_corpus/LAVA-M"; do
        if [ -d "$candidate/base64" ] || [ -d "$candidate/uniq" ]; then
            corpus_dir="$candidate"
            break
        fi
    done
    if [ -z "$corpus_dir" ]; then
        error "LAVA-M corpus not found for AFL build"
        return 1
    fi

    info "Building LAVA-M AFL-instrumented binaries..."
    mkdir -p "$BUILD_DIR/lava-m-afl"

    local built=0
    for prog in base64 md5sum uniq who; do
        local src_dir=""
        for d in "$corpus_dir/$prog"/coreutils-*; do
            [ -d "$d" ] && src_dir="$d" && break
        done
        [ -z "$src_dir" ] && continue

        info "  Building $prog (AFL)..."
        cd "$src_dir"
        set +e
        make distclean 2>/dev/null || make clean 2>/dev/null || true

        if [ -f configure ]; then
            chmod +x configure 2>/dev/null || true

            # 和 SymCC 版本一样 patch unlocked-io.h（保持一致性）
            if [ -f lib/unlocked-io.h ]; then
                cat > lib/unlocked-io.h << 'UNLOCKED_PATCH'
#ifndef UNLOCKED_IO_H
# define UNLOCKED_IO_H 1
# include <stdio.h>
# define clearerr_unlocked(x) clearerr(x)
# define feof_unlocked(x) feof(x)
# define ferror_unlocked(x) ferror(x)
# define fflush_unlocked(x) fflush(x)
# define fgets_unlocked(x,y,z) fgets(x,y,z)
# define fputc_unlocked(x,y) fputc(x,y)
# define fputs_unlocked(x,y) fputs(x,y)
# define fread_unlocked(w,x,y,z) fread(w,x,y,z)
# define fwrite_unlocked(w,x,y,z) fwrite(w,x,y,z)
# define getc_unlocked(x) getc(x)
# define getchar_unlocked() getchar()
# define putc_unlocked(x,y) putc(x,y)
# define putchar_unlocked(x) putchar(x)
#endif
UNLOCKED_PATCH
            fi

            CC=afl-clang-fast CFLAGS="-O2 -Wno-implicit-function-declaration" \
                FORCE_UNSAFE_CONFIGURE=1 \
                ./configure --quiet 2>&1 | tail -3
            make -k -j$(nproc) 2>&1 | tail -3
        fi

        if [ -f "src/$prog" ]; then
            cp "src/$prog" "$BUILD_DIR/lava-m-afl/${prog}"
            built=$((built + 1))
            info "    -> $prog AFL built"
            case "$prog" in
                base64) echo "-d" > "$BUILD_DIR/lava-m-afl/${prog}.args" ;;
            esac
        else
            warn "    $prog AFL build failed"
        fi
        set -e
        cd "$SCRIPT_DIR"
    done

    info "LAVA-M AFL: built $built / 4 targets"
}

############################################################
# Main
############################################################
usage() {
    echo "Usage: $0 [--compiler CC] [--all | --cgc | --lava | --google-fts] [--with-coverage]"
    echo ""
    echo "Build public benchmark programs. Assumes repos are already cloned"
    echo "into benchmark/public/ (use setup_public_benchmarks.sh first)."
    echo ""
    echo "Options:"
    echo "  --compiler CC     C compiler to use (default: gcc, or use 'symcc')"
    echo "  --all             Build all available benchmarks"
    echo "  --cgc             Build CGC cb-multios challenges"
    echo "  --lava            Build LAVA-M targets (base64, md5sum, uniq, who)"
    echo "  --google-fts      Build Google fuzzer-test-suite"
    echo "  --with-coverage   Also build coverage-instrumented binaries (gcc --coverage)"
    echo "  --with-afl        Also build AFL-instrumented binaries (afl-clang-fast)"
    echo ""
    echo "Output:"
    echo "  Binaries:  $BUILD_DIR/<suite>/"
    echo "  Coverage:  $BUILD_DIR/<suite>-cov/"
    echo "  Seeds:     $SEEDS_DIR/<suite>/"
    echo ""
    echo "Example:"
    echo "  # Build with gcc (for MPI framework testing):"
    echo "  $0 --all"
    echo ""
    echo "  # Build with SymCC (for real symbolic execution):"
    echo "  $0 --compiler symcc --all"
    echo ""
    echo "  # Build with coverage for benchmark reports:"
    echo "  $0 --all --with-coverage"
}

# Parse args
BUILD_CGC=false
BUILD_LAVA=false
BUILD_GOOGLE=false
WITH_COVERAGE=false
WITH_AFL=false

if [ $# -eq 0 ]; then
    usage
    exit 0
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --compiler)
            shift
            CC="$1"
            _COMPILER_EXPLICIT=true
            if [ "$CC" = "symcc" ] || [[ "$CC" == *"/symcc" ]]; then
                CXX="${CC%symcc}sym++"
            elif [ "$CC" = "gcc" ]; then
                CXX="g++"
            elif [ "$CC" = "clang" ]; then
                CXX="clang++"
            else
                CXX="${CC}++"
            fi
            ;;
        --all)            BUILD_CGC=true; BUILD_LAVA=true; BUILD_GOOGLE=true ;;
        --cgc)            BUILD_CGC=true ;;
        --lava)           BUILD_LAVA=true ;;
        --google-fts)     BUILD_GOOGLE=true ;;
        --with-coverage)  WITH_COVERAGE=true ;;
        --with-afl)       WITH_AFL=true ;;
        --help|-h)        usage; exit 0 ;;
        *)                error "Unknown option: $1"; usage; exit 1 ;;
    esac
    shift
done

# Auto-detect SymCC only if no --compiler was explicitly given
if [ "$_COMPILER_EXPLICIT" = false ] && [ "$CC" = "gcc" ]; then
    SYMCC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
    if [ -x "$SYMCC_ROOT/build/symcc" ]; then
        CC="$SYMCC_ROOT/build/symcc"
        CXX="$SYMCC_ROOT/build/sym++"
        info "Auto-detected SymCC at $CC"
    elif command -v symcc >/dev/null 2>&1; then
        CC="symcc"
        CXX="sym++"
        info "Auto-detected SymCC in PATH"
    else
        warn "SymCC not found, using gcc (simulation mode only)"
    fi
fi

mkdir -p "$BUILD_DIR" "$SEEDS_DIR"

echo "================================================================"
echo "  Building Public Benchmarks"
echo "  CC=$CC  CXX=$CXX"
echo "================================================================"
echo ""

$BUILD_CGC    && build_cgc
$BUILD_LAVA   && build_lava
$BUILD_GOOGLE && build_google_fts

# AFL builds 必须在 coverage 之前，因为两者都会 distclean 库源码
# Coverage 最后编译确保 .gcno 文件不被后续步骤覆盖
if $WITH_AFL; then
    echo ""
    echo "================================================================"
    echo "  Building AFL-Instrumented Binaries"
    echo "================================================================"
    echo ""
    $BUILD_GOOGLE && build_google_fts_afl
    $BUILD_LAVA   && build_lava_afl
fi

# Coverage builds (使用 gcc --coverage -O0 -g 重新编译) — 必须最后！
if $WITH_COVERAGE; then
    echo ""
    echo "================================================================"
    echo "  Building Coverage-Instrumented Binaries"
    echo "================================================================"
    echo ""
    $BUILD_LAVA   && build_lava_coverage
    $BUILD_GOOGLE && build_google_fts_coverage
fi

echo ""
echo "================================================================"
echo "  Build Complete"
echo "================================================================"
echo "  Binaries:  $BUILD_DIR/"
echo "  Seeds:     $SEEDS_DIR/"
echo ""

# Show what was built
for suite_dir in "$BUILD_DIR"/*/; do
    if [ -d "$suite_dir" ]; then
        local_count=$(find "$suite_dir" -type f -executable 2>/dev/null | wc -l)
        suite_name=$(basename "$suite_dir")
        echo "  $suite_name: $local_count binaries"
    fi
done

echo ""
echo "To run MPI benchmark with these binaries:"
echo "  python3 run_benchmark.py --public \\"
for bin_file in "$BUILD_DIR"/*/*; do
    if [ -f "$bin_file" ] && [ -x "$bin_file" ]; then
        local_name=$(basename "$bin_file")
        local_suite=$(basename "$(dirname "$bin_file")")
        local_seeds="$SEEDS_DIR/$local_suite/$local_name"
        if [ -d "$local_seeds" ]; then
            echo "    '$local_name:$bin_file:$local_seeds' \\"
        fi
    fi
done
echo "    --np-list 2,4,8 --rounds 3 --timeout 120"
