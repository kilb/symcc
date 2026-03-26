#!/usr/bin/env bash
#
# 一键测试脚本：对 LAVA-M 与 Google Fuzzer Test Suite 进行不同并行度的测试
#
# 用法:
#   ./run_public_benchmarks.sh <并行进程数>
#   ./run_public_benchmarks.sh <进程数1,进程数2,...>
#
# 示例:
#   ./run_public_benchmarks.sh 4           # 使用 4 个 MPI 进程测试
#   ./run_public_benchmarks.sh 2,4,8       # 分别使用 2/4/8 个进程测试
#   ./run_public_benchmarks.sh 2,4 --timeout 120 --rounds 2
#
# 前置条件:
#   - 已安装 MPI: apt install openmpi-bin libopenmpi-dev
#   - 已安装 mpi4py: pip install mpi4py
#   - benchmark/public/ 下已有编译好的二进制和种子文件
#     (若未编译可加 --build 自动编译)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PUBLIC_DIR="$SCRIPT_DIR/public"
BIN_DIR="$PUBLIC_DIR/bin"
SEEDS_DIR="$PUBLIC_DIR/seeds"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

usage() {
    echo "用法: $0 <并行进程数> [选项]"
    echo ""
    echo "对 LAVA-M 和 Google Fuzzer Test Suite 进行不同并行度的 MPI 测试。"
    echo ""
    echo "参数:"
    echo "  <并行进程数>         MPI 进程数，支持逗号分隔 (如 2,4,8)"
    echo ""
    echo "选项:"
    echo "  --build              先编译测试程序 (使用 gcc 模拟模式)"
    echo "  --compiler CC        编译器 (默认: gcc)"
    echo "  --timeout SECS       每轮测试超时秒数 (默认: 60)"
    echo "  --rounds N           每个配置运行轮数 (默认: 1)"
    echo "  --output DIR         结果输出目录 (默认: benchmark_results_public)"
    echo "  --targets NAMES      逗号分隔的目标名 (默认: 全部可用)"
    echo "  --lava-only          只测试 LAVA-M"
    echo "  --google-fts-only    只测试 Google FTS"
    echo "  --hybrid             同时运行 AFL+SymCC 混合模式"
    echo "  --afl-only           同时运行 AFL-only 基准模式"
    echo "  --help               显示帮助"
    echo ""
    echo "示例:"
    echo "  $0 4                             # 4 进程，默认参数"
    echo "  $0 2,4,8 --timeout 120 --rounds 3"
    echo "  $0 2,4 --build --lava-only"
}

# 默认值
NP_LIST=""
DO_BUILD=false
COMPILER="gcc"
TIMEOUT=300
ROUNDS=1
OUTPUT_DIR=""
TARGETS_FILTER=""
LAVA_ONLY=false
GOOGLE_ONLY=false
HYBRID=false
AFL_ONLY=false
EXTRA_ARGS=()

# 解析参数
if [ $# -eq 0 ]; then
    usage
    exit 0
fi

# 第一个非 -- 开头的参数视为进程数
FIRST_ARG=true
while [ $# -gt 0 ]; do
    case "$1" in
        --build)       DO_BUILD=true ;;
        --compiler)    shift; COMPILER="$1" ;;
        --timeout)     shift; TIMEOUT="$1" ;;
        --rounds)      shift; ROUNDS="$1" ;;
        --output)      shift; OUTPUT_DIR="$1" ;;
        --targets)     shift; TARGETS_FILTER="$1" ;;
        --lava-only)   LAVA_ONLY=true ;;
        --google-fts-only) GOOGLE_ONLY=true ;;
        --hybrid)      HYBRID=true ;;
        --afl-only)    AFL_ONLY=true ;;
        --help|-h)     usage; exit 0 ;;
        -*)            EXTRA_ARGS+=("$1") ;;
        *)
            if $FIRST_ARG; then
                NP_LIST="$1"
                FIRST_ARG=false
            else
                EXTRA_ARGS+=("$1")
            fi
            ;;
    esac
    shift
done

if [ -z "$NP_LIST" ]; then
    error "请指定并行进程数，如: $0 4 或 $0 2,4,8"
    exit 1
fi

# 设置默认输出目录
if [ -z "$OUTPUT_DIR" ]; then
    OUTPUT_DIR="$SCRIPT_DIR/benchmark_results_public"
fi

# ================================================================
# Step 0: 检查依赖
# ================================================================
echo ""
echo "================================================================"
echo "  SymCC MPI 公开测试集一键测试"
echo "  NP=$NP_LIST  Timeout=${TIMEOUT}s  Rounds=$ROUNDS"
echo "================================================================"
echo ""

# 检查 MPI
if ! command -v mpirun >/dev/null 2>&1; then
    error "mpirun 未找到。请安装: sudo apt install openmpi-bin libopenmpi-dev"
    exit 1
fi

# 检查 mpi4py
if ! python3 -c "import mpi4py" 2>/dev/null; then
    error "mpi4py 未找到。请安装: pip install mpi4py"
    exit 1
fi

# 检查 MPI 脚本
if [ ! -f "$SCRIPT_DIR/../util/mpi_concolic_execution.py" ]; then
    error "找不到 MPI 脚本: util/mpi_concolic_execution.py"
    exit 1
fi

# ================================================================
# Step 1: 编译测试程序 (可选)
# ================================================================
if $DO_BUILD; then
    info "Step 1: 编译测试程序..."

    BUILD_TARGETS=""
    if $LAVA_ONLY; then
        BUILD_TARGETS="--lava"
    elif $GOOGLE_ONLY; then
        BUILD_TARGETS="--google-fts"
    else
        BUILD_TARGETS="--lava --google-fts"
    fi

    bash "$SCRIPT_DIR/compile_public_benchmarks.sh" \
        --compiler "$COMPILER" $BUILD_TARGETS --with-coverage

    echo ""
fi

# ================================================================
# Step 2: 发现可用的目标
# ================================================================
info "Step 2: 发现可用测试目标..."

PUBLIC_SPECS=()

# 扫描 LAVA-M 目标
if ! $GOOGLE_ONLY; then
    if [ -d "$BIN_DIR/lava-m" ]; then
        for bin_file in "$BIN_DIR/lava-m"/*; do
            if [ -f "$bin_file" ] && [ -x "$bin_file" ]; then
                name=$(basename "$bin_file")
                seed_dir="$SEEDS_DIR/lava-m/$name"
                if [ -d "$seed_dir" ]; then
                    PUBLIC_SPECS+=("lava-$name:$bin_file:$seed_dir")
                    info "  LAVA-M: $name (种子: $(ls "$seed_dir" | wc -l) 个)"
                else
                    warn "  LAVA-M: $name 缺少种子目录 $seed_dir"
                fi
            fi
        done
    else
        warn "  LAVA-M 二进制目录不存在: $BIN_DIR/lava-m/"
        warn "  请先运行: $0 <np> --build"
    fi
fi

# 扫描 Google FTS 目标
if ! $LAVA_ONLY; then
    if [ -d "$BIN_DIR/google-fts" ]; then
        for bin_file in "$BIN_DIR/google-fts"/*; do
            if [ -f "$bin_file" ] && [ -x "$bin_file" ]; then
                name=$(basename "$bin_file")
                seed_dir="$SEEDS_DIR/google-fts/$name"
                if [ -d "$seed_dir" ]; then
                    PUBLIC_SPECS+=("gfts-$name:$bin_file:$seed_dir")
                    info "  Google FTS: $name (种子: $(ls "$seed_dir" | wc -l) 个)"
                else
                    warn "  Google FTS: $name 缺少种子目录 $seed_dir"
                fi
            fi
        done
    else
        warn "  Google FTS 二进制目录不存在: $BIN_DIR/google-fts/"
        warn "  请先运行: $0 <np> --build"
    fi
fi

if [ ${#PUBLIC_SPECS[@]} -eq 0 ]; then
    error "没有找到可用的测试目标。请先编译:"
    error "  $0 <np> --build"
    exit 1
fi

# 应用目标过滤
if [ -n "$TARGETS_FILTER" ]; then
    FILTERED_SPECS=()
    IFS=',' read -ra FILTER_LIST <<< "$TARGETS_FILTER"
    for spec in "${PUBLIC_SPECS[@]}"; do
        spec_name="${spec%%:*}"
        for filter_name in "${FILTER_LIST[@]}"; do
            if [[ "$spec_name" == *"$filter_name"* ]]; then
                FILTERED_SPECS+=("$spec")
                break
            fi
        done
    done
    PUBLIC_SPECS=("${FILTERED_SPECS[@]}")
fi

echo ""
info "共发现 ${#PUBLIC_SPECS[@]} 个测试目标"

# ================================================================
# Step 3: 运行测试
# ================================================================
info "Step 3: 运行 MPI 并行测试..."
echo ""

# 构建 --public 参数
PUBLIC_ARGS=()
for spec in "${PUBLIC_SPECS[@]}"; do
    PUBLIC_ARGS+=("$spec")
done

# 运行 run_benchmark.py
BENCH_EXTRA=()
if $HYBRID; then
    BENCH_EXTRA+=("--hybrid")
fi
if $AFL_ONLY; then
    BENCH_EXTRA+=("--afl-only")
fi

python3 "$SCRIPT_DIR/run_benchmark.py" \
    --no-default \
    --public "${PUBLIC_ARGS[@]}" \
    --np-list "$NP_LIST" \
    --rounds "$ROUNDS" \
    --timeout "$TIMEOUT" \
    --output "$OUTPUT_DIR" \
    "${BENCH_EXTRA[@]}" \
    "${EXTRA_ARGS[@]}"

echo ""
info "测试完成！结果保存在: $OUTPUT_DIR/"
echo ""
echo "  查看报告: cat $OUTPUT_DIR/benchmark_report.txt"
echo "  查看数据: cat $OUTPUT_DIR/benchmark_data.csv"
