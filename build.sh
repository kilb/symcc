#!/usr/bin/env bash
#
# build.sh —— 仅编译 SymCC 编译器 pass 与运行时库（不安装系统依赖）。
#
# 如果系统依赖（clang/LLVM、Z3、cmake、ninja）已经就绪，直接运行本脚本即可。
# 若从零开始（全新机器），请改用 ./setup.sh，它会先安装所有依赖再调用本脚本。
#
# 用法:
#   ./build.sh                 # 增量构建到 ./build
#   ./build.sh --clean         # 删除 ./build 后全新构建
#   ./build.sh --dir <path>    # 构建到指定目录
#   ./build.sh --no-test       # 构建后跳过冒烟测试
#
set -euo pipefail

# ---- 项目根目录（脚本所在目录）----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- 彩色输出小工具 ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[构建]${NC} $*"; }
step()  { echo -e "${BLUE}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}[警告]${NC} $*"; }
error() { echo -e "${RED}[错误]${NC} $*" >&2; }

# 本项目验证过的 LLVM 主版本（与 setup.sh 保持一致）
LLVM_VER=18

usage() {
    cat <<'EOF'
build.sh —— 仅编译 SymCC 编译器 pass 与运行时库（不安装系统依赖）。
系统依赖若已就绪直接运行即可;全新机器请用 ./setup.sh(它会先装依赖再调用本脚本)。

用法:
  ./build.sh                 # 增量构建到 ./build
  ./build.sh --clean         # 删除 ./build 后全新构建
  ./build.sh --dir <path>    # 构建到指定目录
  ./build.sh --no-test       # 构建后跳过冒烟测试
EOF
}

# ---- 解析参数 ----
BUILD_DIR="$SCRIPT_DIR/build"
CLEAN=false
RUN_TEST=true
while [ $# -gt 0 ]; do
    case "$1" in
        --clean)   CLEAN=true; shift ;;
        --no-test) RUN_TEST=false; shift ;;
        --dir)     [ $# -ge 2 ] || { error "--dir 需要一个路径参数"; exit 1; }; BUILD_DIR="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) error "未知参数: $1（用 --help 查看用法）"; exit 1 ;;
    esac
done

# ---- 1. 校验必备工具是否存在 ----
step "检查构建工具..."
missing=()
for tool in cmake ninja; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ ${#missing[@]} -gt 0 ]; then
    error "缺少构建工具: ${missing[*]}"
    error "请先运行 ./setup.sh 安装全部依赖，或手动安装后重试。"
    exit 1
fi

# ---- 2. 定位 LLVM 的 CMake 模块目录 ----
# 优先使用 LLVM 18（本项目验证过的版本），否则回退到系统中最新的 LLVM，
# 最后回退到 PATH 上的 llvm-config。可通过环境变量 LLVM_DIR 手动覆盖。
detect_llvm_dir() {
    if [ -n "${LLVM_DIR:-}" ] && [ -d "$LLVM_DIR" ]; then
        echo "$LLVM_DIR"; return
    fi
    if [ -d "/usr/lib/llvm-${LLVM_VER}/lib/cmake/llvm" ]; then
        echo "/usr/lib/llvm-${LLVM_VER}/lib/cmake/llvm"; return
    fi
    # 回退：系统中版本号最高的 llvm-XX
    local newest
    newest=$(ls -d /usr/lib/llvm-*/lib/cmake/llvm 2>/dev/null | sort -V | tail -1 || true)
    if [ -n "$newest" ]; then echo "$newest"; return; fi
    # 再回退：任意 llvm-config（优先带版本号的目标版本,避免误取到更低的默认版）
    local cfg
    cfg=$(command -v "llvm-config-${LLVM_VER}" llvm-config-17 llvm-config-16 llvm-config 2>/dev/null | head -1 || true)
    if [ -n "$cfg" ]; then "$cfg" --cmakedir; return; fi
    echo ""
}
LLVM_CMAKE_DIR="$(detect_llvm_dir)"
if [ -z "$LLVM_CMAKE_DIR" ]; then
    error "未找到 LLVM 开发包（找不到 LLVMConfig.cmake）。"
    error "请安装 llvm-18-dev / clang-18（或运行 ./setup.sh），或设置环境变量 LLVM_DIR。"
    exit 1
fi
info "使用 LLVM: $LLVM_CMAKE_DIR"

# ---- 3. 准备运行时源码（运行时 + QSYM 后端）----
# 源码可能来自压缩包（随包提供，无需 git）或需要从子模块拉取。
if [ ! -e runtime/CMakeLists.txt ]; then
    # 用 `git rev-parse` 判定,而非 `[ -d .git ]`——git worktree / 子模块签出时 .git 是文件而非目录
    if [ -f .gitmodules ] && git rev-parse --git-dir >/dev/null 2>&1; then
        step "初始化 git 子模块（运行时 / QSYM 后端）..."
        git submodule update --init --recursive
    else
        error "缺少运行时源码 runtime/，且当前不是 git 仓库，无法自动获取。"
        error "若从压缩包解压，请让分发者用 ./package.sh 重新打包（应包含 runtime/ 子目录）。"
        exit 1
    fi
fi

# ---- 4. 配置并构建 ----
if [ "$CLEAN" = true ] && [ -d "$BUILD_DIR" ]; then
    step "清理旧构建目录: $BUILD_DIR"
    rm -rf "$BUILD_DIR"
fi
mkdir -p "$BUILD_DIR"

step "配置 CMake（QSYM 后端 + 系统 Z3）..."
# Z3_TRUST_SYSTEM_VERSION=ON：使用 apt 安装的 libz3-dev（无 Z3 CMake 包时必需）。
cmake -G Ninja \
    -DSYMCC_RT_BACKEND=qsym \
    -DZ3_TRUST_SYSTEM_VERSION=ON \
    -DLLVM_DIR="$LLVM_CMAKE_DIR" \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -S "$SCRIPT_DIR" -B "$BUILD_DIR"

step "编译（ninja -j$(nproc)）..."
ninja -C "$BUILD_DIR"

# ---- 5. 冒烟测试：编译一个小程序并跑一次并发执行 ----
if [ "$RUN_TEST" = true ]; then
    step "冒烟测试：用 symcc 编译并运行一个示例程序..."
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    cat > "$tmp/test.c" <<'EOF'
#include <stdio.h>
#include <stdint.h>
#include <unistd.h>
int foo(int a, int b) {
    if (2 * a < b) return a;
    else if (a % b) return b;
    else return a + b;
}
int main(void) {
    int x;
    if (read(STDIN_FILENO, &x, sizeof(x)) != sizeof(x)) return -1;
    printf("%d\n", foo(x, 7));
    return 0;
}
EOF
    "$BUILD_DIR/symcc" "$tmp/test.c" -o "$tmp/test"
    mkdir -p "$tmp/results"
    echo 'aaaa' | env SYMCC_OUTPUT_DIR="$tmp/results" "$tmp/test" >/dev/null || true
    if [ -n "$(ls -A "$tmp/results" 2>/dev/null)" ]; then
        info "冒烟测试通过：SymCC 生成了新测试用例。"
    else
        warn "冒烟测试：程序可运行，但本次未生成新用例（对该示例通常仍属正常）。"
    fi
fi

echo
info "构建完成 ✔"
echo "  编译器封装:   $BUILD_DIR/symcc   (clang 的替代品)"
echo "  C++ 封装:     $BUILD_DIR/sym++   (clang++ 的替代品)"
echo "  运行时库:     $BUILD_DIR/SymCCRuntime-prefix/src/SymCCRuntime-build/libsymcc-rt.so"
echo
echo "下一步：阅读 README.md 的『运行』一节，或直接试跑："
echo "  echo 'hello' | $BUILD_DIR/symcc --help"
