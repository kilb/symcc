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

# Auto-detect SymCC if CC is still default gcc
if [ "$CC" = "gcc" ]; then
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
        warn "For real symbolic execution, build SymCC first or use: --compiler /path/to/symcc"
    fi
fi

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
# LAVA targets  (file, jq, grep, duktape, libyaml)
# Source: https://github.com/panda-re/lava
# Pre-extracted source trees in public/lava-m/build_*
############################################################
build_lava() {
    # Find the LAVA directory.  The pre-extracted build_* source trees
    # may live under public/lava-m/, public/lava/, or public/lava-m/lava/.
    local lava_dir=""
    for candidate in "$PUBLIC_DIR/lava-m" "$PUBLIC_DIR/lava" "$PUBLIC_DIR/lava-m/lava"; do
        # Check for at least one build_* subdirectory
        if ls "$candidate"/build_* 1>/dev/null 2>&1; then
            lava_dir="$candidate"
            break
        fi
    done
    if [ -z "$lava_dir" ]; then
        error "LAVA not found.  Expected build_* directories under one of:"
        error "  $PUBLIC_DIR/lava-m/"
        error "  $PUBLIC_DIR/lava/"
        error "Run: ./setup_public_benchmarks.sh --lava"
        return 1
    fi

    info "Building LAVA targets from $lava_dir with CC=$CC ..."
    mkdir -p "$BUILD_DIR/lava" "$SEEDS_DIR/lava"

    local built=0

    # LAVA target definitions: name, expected_binary_path
    # Each build_<name>/ directory has a pre-extracted, pre-configured
    # source tree.  We auto-discover the subdirectory inside build_*.
    local targets=(
        "file:src/.libs/file"
        "jq:src/jq"
        "grep:src/grep"
        "duktape:src/duk"
        "libyaml:src/libyaml"
    )

    for entry in "${targets[@]}"; do
        IFS=: read -r name bin_rel <<< "$entry"

        # Find the build directory: try build_<name>, then build_<name>_64
        local build_parent=""
        for candidate in "$lava_dir/build_${name}" "$lava_dir/build_${name}_64"; do
            if [ -d "$candidate" ]; then
                build_parent="$candidate"
                break
            fi
        done
        if [ -z "$build_parent" ]; then
            warn "  $name: build directory not found (expected build_${name}/)"
            continue
        fi

        # Auto-discover the source subdirectory (first child dir)
        local src_dir=$(find "$build_parent" -mindepth 1 -maxdepth 1 -type d | head -1)
        if [ -z "$src_dir" ]; then
            warn "  $name: no source subdirectory in $build_parent"
            continue
        fi

        info "  Building $name ..."

        # Clean previous build artifacts
        cd "$src_dir"
        make clean 2>/dev/null || true

        # Patch Makefiles: strip -m32 and -static (SymCC runtime is a
        # shared library), and replace hardcoded gcc with our compiler.
        find . -name Makefile -o -name '*.mk' | while read -r mf; do
            sed -i \
                -e "s|-m32||g" \
                -e "s|-static||g" \
                -e "s|^\(CC\s*=\s*\)gcc|\1$CC|" \
                -e "s|^\(CC\s*=\s*\)/usr[^ ]*/gcc|\1$CC|" \
                "$mf"
        done

        set +e
        local bin_path=""
        case "$name" in
            file)
                # file uses autotools/libtool.  Re-configure from scratch
                # with --disable-shared so libmagic is statically linked
                # and SymCC-instrumented.
                make distclean 2>/dev/null || true
                if CC="$CC" CFLAGS="-O2" ./configure --quiet --disable-shared; then
                    make -j$(nproc)
                else
                    warn "    file: configure failed, trying make with patched Makefile"
                    make CC="$CC" -j$(nproc)
                fi
                # libtool: real binary is in .libs/
                if [ -f "src/.libs/file" ]; then
                    bin_path="src/.libs/file"
                elif [ -f "src/file" ]; then
                    bin_path="src/file"
                fi
                # Copy magic database alongside binary
                if [ -f "magic/magic.mgc" ]; then
                    cp magic/magic.mgc "$BUILD_DIR/lava/magic.mgc"
                fi
                ;;
            *)
                # All other LAVA targets use simple Makefiles.
                # CC= on the command line overrides the Makefile variable.
                make CC="$CC" -j$(nproc)
                bin_path="$bin_rel"
                ;;
        esac
        set -e

        # Check if we got a binary
        if [ -n "$bin_path" ] && [ -f "$bin_path" ]; then
            cp "$bin_path" "$BUILD_DIR/lava/${name}"
            built=$((built + 1))
            info "    -> $name built successfully"
        else
            # Fallback: search for an executable with the target name
            local found=$(find . -maxdepth 3 -name "$name" -type f -executable 2>/dev/null | head -1)
            if [ -z "$found" ]; then
                found=$(find . -maxdepth 3 -name "duk" -type f -executable 2>/dev/null | head -1)
            fi
            if [ -n "$found" ]; then
                cp "$found" "$BUILD_DIR/lava/${name}"
                built=$((built + 1))
                info "    -> $name built successfully (at $found)"
            else
                warn "    $name: no binary found"
            fi
        fi

        # Create seeds (target-specific where needed)
        mkdir -p "$SEEDS_DIR/lava/$name"
        case "$name" in
            file)
                # file: use ELF header and a text file as seeds
                printf '\x7fELF\x02\x01\x01\x00' > "$SEEDS_DIR/lava/$name/seed_01"
                head -c 56 /dev/urandom >> "$SEEDS_DIR/lava/$name/seed_01" 2>/dev/null
                echo '#!/bin/sh' > "$SEEDS_DIR/lava/$name/seed_02"
                printf '\x89PNG\r\n\x1a\n' > "$SEEDS_DIR/lava/$name/seed_03"
                head -c 64 /dev/urandom >> "$SEEDS_DIR/lava/$name/seed_03" 2>/dev/null
                ;;
            jq)
                echo '{"a":1,"b":[2,3]}' > "$SEEDS_DIR/lava/$name/seed_01"
                echo '[1,2,3]' > "$SEEDS_DIR/lava/$name/seed_02"
                echo '"hello"' > "$SEEDS_DIR/lava/$name/seed_03"
                ;;
            grep)
                echo 'hello world' > "$SEEDS_DIR/lava/$name/seed_01"
                printf 'line1\nline2\nline3\n' > "$SEEDS_DIR/lava/$name/seed_02"
                head -c 128 /dev/urandom > "$SEEDS_DIR/lava/$name/seed_03" 2>/dev/null || true
                ;;
            *)
                echo "test input" > "$SEEDS_DIR/lava/$name/seed_01"
                printf 'AAAAAAAAAAAAAAAA' > "$SEEDS_DIR/lava/$name/seed_02"
                head -c 128 /dev/urandom > "$SEEDS_DIR/lava/$name/seed_03" 2>/dev/null || true
                ;;
        esac

        cd "$SCRIPT_DIR"
    done

    info "LAVA: built $built targets"
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
        CC="$CC" ./configure --quiet --disable-shared 2>/dev/null && make -j$(nproc) 2>/dev/null || { warn "  libpng: make failed"; return 1; }

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
    int passes = png_set_interlace_handling(pp); png_start_read_image(pp);
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
" 2>/dev/null
        info "    -> png_read_fuzzer built successfully"
        built=$((built + 1))
        cd "$SCRIPT_DIR"
    }

    # --- libxml2 ---
    build_libxml2() {
        info "  Building libxml2 ..."
        cd "$work_dir"
        local tarball="libxml2-2.9.2.tar.gz"
        if [ ! -f "$tarball" ]; then
            curl -sL "https://github.com/GNOME/libxml2/archive/refs/tags/v2.9.2.tar.gz" -o "$tarball" || { warn "  Failed to download libxml2"; return 1; }
        fi
        rm -rf libxml2-2.9.2
        tar xf "$tarball"
        cd libxml2-2.9.2
        autoreconf -fi 2>/dev/null
        CC="$CC" ./configure --quiet --disable-shared --without-python --without-threads 2>/dev/null && make -j$(nproc) 2>/dev/null || { warn "  libxml2: make failed"; return 1; }

        # Create standalone harness
        cat > /tmp/xml_read_fuzzer.c << 'HARNESS_EOF'
#include <stdio.h>
#include <stdlib.h>
#include <libxml/parser.h>
#include <libxml/tree.h>
int main(int argc, char *argv[]) {
    if (argc != 2) return 1;
    FILE *f = fopen(argv[1], "rb"); if (!f) return 1;
    fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
    if (sz <= 0 || sz > 1024*1024) { fclose(f); return 1; }
    char *data = malloc(sz); fread(data, 1, sz, f); fclose(f);
    xmlInitParser();
    xmlDocPtr doc = xmlReadMemory(data, sz, "input.xml", NULL,
        XML_PARSE_NONET | XML_PARSE_RECOVER | XML_PARSE_NOERROR | XML_PARSE_NOWARNING);
    if (doc) xmlFreeDoc(doc);
    xmlCleanupParser(); free(data); return 0;
}
HARNESS_EOF
        "$CC" -O2 /tmp/xml_read_fuzzer.c -I include .libs/libxml2.a -lz -llzma -lm -lpthread -o "$BUILD_DIR/google-fts/xml_read_fuzzer" 2>/dev/null || { warn "  libxml2 harness link failed"; return 1; }
        rm -f /tmp/xml_read_fuzzer.c

        # Create seeds
        mkdir -p "$SEEDS_DIR/google-fts/xml_read_fuzzer"
        echo '<?xml version="1.0"?><root><item>test</item></root>' > "$SEEDS_DIR/google-fts/xml_read_fuzzer/seed_01.xml"
        echo '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE t [<!ENTITY f "bar">]><root a="v"><c>&f;</c></root>' > "$SEEDS_DIR/google-fts/xml_read_fuzzer/seed_02.xml"
        echo '<html><head><title>t</title></head><body><p>hello</p></body></html>' > "$SEEDS_DIR/google-fts/xml_read_fuzzer/seed_03.xml"
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
# Main
############################################################
usage() {
    echo "Usage: $0 [--compiler CC] [--all | --cgc | --lava | --google-fts]"
    echo ""
    echo "Build public benchmark programs. Assumes repos are already cloned"
    echo "into benchmark/public/ (use setup_public_benchmarks.sh first)."
    echo ""
    echo "Options:"
    echo "  --compiler CC   C compiler to use (default: gcc, or use 'symcc')"
    echo "  --all           Build all available benchmarks"
    echo "  --cgc           Build CGC cb-multios challenges"
    echo "  --lava          Build LAVA target programs (file, jq, grep, etc.)"
    echo "  --google-fts    Build Google fuzzer-test-suite"
    echo ""
    echo "Output:"
    echo "  Binaries: $BUILD_DIR/<suite>/"
    echo "  Seeds:    $SEEDS_DIR/<suite>/"
    echo ""
    echo "Example:"
    echo "  # Build with gcc (for MPI framework testing):"
    echo "  $0 --all"
    echo ""
    echo "  # Build with SymCC (for real symbolic execution):"
    echo "  $0 --compiler symcc --all"
}

# Parse args
BUILD_CGC=false
BUILD_LAVA=false
BUILD_GOOGLE=false

if [ $# -eq 0 ]; then
    usage
    exit 0
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --compiler)
            shift
            CC="$1"
            if [ "$CC" = "symcc" ] || [[ "$CC" == *"/symcc" ]]; then
                # If using symcc, also set CXX to sym++
                CXX="${CC%symcc}sym++"
            fi
            ;;
        --all)        BUILD_CGC=true; BUILD_LAVA=true; BUILD_GOOGLE=true ;;
        --cgc)        BUILD_CGC=true ;;
        --lava)       BUILD_LAVA=true ;;
        --google-fts) BUILD_GOOGLE=true ;;
        --help|-h)    usage; exit 0 ;;
        *)            error "Unknown option: $1"; usage; exit 1 ;;
    esac
    shift
done

mkdir -p "$BUILD_DIR" "$SEEDS_DIR"

echo "================================================================"
echo "  Building Public Benchmarks"
echo "  CC=$CC  CXX=$CXX"
echo "================================================================"
echo ""

$BUILD_CGC    && build_cgc
$BUILD_LAVA   && build_lava
$BUILD_GOOGLE && build_google_fts

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
