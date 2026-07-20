#!/usr/bin/env bash
#
# setup.sh —— 一键部署脚本：从零开始把整个项目跑起来。
#
# 面向「完全没有背景知识」的使用者：在一台干净的 Ubuntu 机器上执行本脚本，
# 它会自动完成以下全部工作：
#   1. 安装系统依赖   （clang/LLVM 18、Z3、cmake、ninja、OpenMPI、Python 等）
#   2. 安装 AFL++     （混合模糊测试需要；若已安装则跳过）
#   3. 创建 Python 虚拟环境并安装 requirements.txt
#   4. 拉取 git 子模块（SymCC 运行时 + QSYM 后端）
#   5. 编译 SymCC     （调用 build.sh）
#   6. 运行冒烟测试   （确认一切正常）
#
# 【离线 / 内网】若本目录存在 offline/ 依赖包（由分发者 `./package.sh --offline` 生成），
# 脚本会【自动】切换到离线模式：完全不访问外网,从 offline/ 安装 apt 依赖、pip 包与 AFL++。
# 适用于无外网的内网机器。用 --online 可强制走联网安装,--offline 可强制离线。
#
# 用法:
#   ./setup.sh                 # 完整安装（推荐；可重复执行,幂等；有 offline/ 则自动离线）
#   ./setup.sh --check         # 只检查依赖,报告缺什么,不做任何改动
#   ./setup.sh --offline       # 强制离线安装（用随包 offline/ 依赖,不联网）
#   ./setup.sh --online        # 强制联网安装（忽略 offline/,走 apt/pip 在线源）
#   ./setup.sh --skip-apt      # 跳过系统包安装（无 sudo / 依赖已装好时）
#   ./setup.sh --skip-afl      # 跳过 AFL++ 安装（只用纯符号执行/MPI,不用混合模糊）
#   ./setup.sh --venv <path>   # 指定虚拟环境路径（默认 ./.venv；若已激活 venv 则复用之）
#
set -uo pipefail   # 注意：此处不用 -e，个别可选步骤失败不应中断整个部署

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------- 彩色输出 ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[部署]${NC} $*"; }
step()  { echo -e "\n${BLUE}${BOLD}==> $*${NC}"; }
warn()  { echo -e "${YELLOW}[警告]${NC} $*"; }
error() { echo -e "${RED}[错误]${NC} $*" >&2; }
ok()    { echo -e "  ${GREEN}✔${NC} $*"; }
bad()   { echo -e "  ${RED}✗${NC} $*"; }

# 查找 AFL++ 持久模式驱动静态库 libAFLDriver.a（逐个路径判断，避免 `ls a b` 因某个
# 路径不存在而整体返回非零导致的误判）。找到则打印路径并返回 0，否则返回 1。
find_afldriver() {
    local p
    for p in /usr/local/lib/afl/libAFLDriver.a /usr/lib/afl/libAFLDriver.a; do
        [ -f "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}

# 用法说明（heredoc,避免早前 sed 行号截取的脆弱写法）
usage() {
    cat <<'EOF'
setup.sh —— 一键部署 SymCC 并行混合模糊测试项目（联网或内网离线均可）。

若同目录存在 offline/（由 ./package.sh --offline 生成）,自动切换离线模式,全程不联网。

用法:
  ./setup.sh                 # 完整安装（有 offline/ 则自动离线,否则联网）
  ./setup.sh --check         # 只检查依赖,报告缺什么,不做改动
  ./setup.sh --offline       # 强制离线（用随包 offline/ 依赖,不访问外网）
  ./setup.sh --online        # 强制联网（忽略 offline/,走在线 apt/pip 源）
  ./setup.sh --skip-apt      # 跳过系统包安装（无 sudo / 依赖已就绪时）
  ./setup.sh --skip-afl      # 跳过 AFL++（只用纯符号执行/MPI,不用混合模糊）
  ./setup.sh --venv <path>   # 指定虚拟环境路径（默认 ./.venv；已激活 venv 则复用）
EOF
}

# ---------- 参数 ----------
LLVM_VER=18
CHECK_ONLY=false
SKIP_APT=false
SKIP_AFL=false
OFFLINE=auto            # auto|true|false —— auto 时按 offline/ 是否存在自动判定
VENV_DIR="$SCRIPT_DIR/.venv"
# 若当前已激活某个虚拟环境，则默认复用它
[ -n "${VIRTUAL_ENV:-}" ] && VENV_DIR="$VIRTUAL_ENV"

while [ $# -gt 0 ]; do
    case "$1" in
        --check)    CHECK_ONLY=true; shift ;;
        --offline)  OFFLINE=true; shift ;;
        --online)   OFFLINE=false; shift ;;
        --skip-apt) SKIP_APT=true; shift ;;
        --skip-afl) SKIP_AFL=true; shift ;;
        --venv)     [ $# -ge 2 ] || { error "--venv 需要一个路径参数"; exit 1; }; VENV_DIR="$2"; shift 2 ;;
        -h|--help)  usage; exit 0 ;;
        *) error "未知参数: $1（用 --help 查看用法）"; exit 1 ;;
    esac
done

# 判断 offline/debs 里是否真的有 .deb（仅目录存在不算——空目录会让后续 glob 变字面量）
OFFLINE_DIR="$SCRIPT_DIR/offline"
have_offline_debs() { ls "$OFFLINE_DIR"/debs/*.deb >/dev/null 2>&1; }
# 解析离线模式：auto → 随包带了可用的 offline/debs 则离线,否则联网
if [ "$OFFLINE" = auto ]; then
    if have_offline_debs; then OFFLINE=true; else OFFLINE=false; fi
fi
if [ "$OFFLINE" = true ] && ! have_offline_debs; then
    error "指定了离线安装,但 offline/debs/ 不存在或为空（本包不含离线依赖）。"
    error "请让分发者用 ./package.sh --offline 重新打包,或改用 --online 联网安装。"
    exit 1
fi
[ "$OFFLINE" = true ] && info "离线模式：从 $OFFLINE_DIR 安装依赖,全程不访问外网。"

# root 用户无需 sudo；否则用 sudo
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

# ---------- 需要的系统包（Ubuntu / Debian）----------
APT_PACKAGES=(
    build-essential git curl pkg-config
    cmake ninja-build
    "clang-${LLVM_VER}" "llvm-${LLVM_VER}-dev" "llvm-${LLVM_VER}-tools"
    libz3-dev zlib1g-dev
    python3 python3-venv python3-pip
    libopenmpi-dev openmpi-bin
)

# ============================================================
#  依赖检查（--check 模式，以及安装后的复核）
# ============================================================
check_dependencies() {
    local all_ok=true
    echo -e "${BOLD}系统工具:${NC}"
    for t in gcc g++ make git curl cmake ninja mpirun; do
        if command -v "$t" >/dev/null 2>&1; then ok "$t ($($t --version 2>/dev/null | head -1 | cut -c1-48))"
        else bad "$t 缺失"; all_ok=false; fi
    done

    echo -e "${BOLD}LLVM / Clang:${NC}"
    if command -v "clang-${LLVM_VER}" >/dev/null 2>&1; then ok "clang-${LLVM_VER}"
    elif command -v clang >/dev/null 2>&1;               then ok "clang ($(clang --version | head -1))"
    else bad "clang 缺失"; all_ok=false; fi
    if [ -d "/usr/lib/llvm-${LLVM_VER}/lib/cmake/llvm" ] || ls -d /usr/lib/llvm-*/lib/cmake/llvm >/dev/null 2>&1; then
        ok "LLVM CMake 模块"
    else bad "LLVM 开发包（LLVMConfig.cmake）缺失"; all_ok=false; fi

    echo -e "${BOLD}Z3:${NC}"
    if [ -f /usr/include/z3++.h ] && ls /usr/lib/*/libz3.so* >/dev/null 2>&1; then ok "libz3-dev"
    else bad "libz3-dev 缺失"; all_ok=false; fi

    echo -e "${BOLD}Python:${NC}"
    if command -v python3 >/dev/null 2>&1; then ok "python3 ($(python3 --version 2>&1))"; else bad "python3 缺失"; all_ok=false; fi
    if python3 -c "import mpi4py" 2>/dev/null; then ok "mpi4py (import 成功)"; else warn "  mpi4py 尚未安装（虚拟环境创建后会装上）"; fi

    echo -e "${BOLD}AFL++（混合模糊测试需要，可选）:${NC}"
    if command -v afl-fuzz >/dev/null 2>&1; then ok "afl-fuzz ($(afl-fuzz --version 2>&1 | head -1 || true))"
    else warn "  afl-fuzz 缺失——纯符号执行/MPI 不受影响，但混合模式不可用"; fi
    if find_afldriver >/dev/null; then ok "libAFLDriver.a（持久模式驱动） -> $(find_afldriver)"
    else warn "  libAFLDriver.a 缺失（持久模式基准需要）"; fi

    echo -e "${BOLD}构建产物:${NC}"
    if [ -x "$SCRIPT_DIR/build/symcc" ]; then ok "build/symcc 已存在"; else warn "  尚未构建（运行本脚本或 ./build.sh）"; fi

    $all_ok && return 0 || return 1
}

if [ "$CHECK_ONLY" = true ]; then
    step "依赖检查（只读，不做任何修改）"
    if check_dependencies; then
        echo; info "核心依赖齐全，可以直接构建：./build.sh"
    else
        echo; warn "存在缺失项。运行不带参数的 ./setup.sh 即可自动安装。"
    fi
    exit 0
fi

# ============================================================
#  1. 安装系统依赖
# ============================================================
if [ "$SKIP_APT" = true ]; then
    step "跳过系统包安装（--skip-apt）"
elif [ "$OFFLINE" = true ]; then
    step "安装系统依赖（离线 .deb,不联网）"
    ndeb=$(ls "$OFFLINE_DIR"/debs/*.deb 2>/dev/null | wc -l)
    info "从 offline/debs 安装 $ndeb 个 .deb（apt 在本地文件间解析依赖,--no-download 保证不联网）..."
    # apt-get install ./*.deb 会在【提供的本地 deb + 已装包】之间解析依赖并按序配置;
    # --no-download 杜绝联网,--allow-downgrades 容忍随包版本略低于目标机已装版本。
    if $SUDO apt-get install -y --no-download --allow-downgrades "$OFFLINE_DIR"/debs/*.deb; then
        info "离线系统依赖安装完成。"
    else
        warn "apt 本地安装未完全成功,回退到 dpkg -i（跑两遍解决依赖顺序）..."
        $SUDO dpkg -i "$OFFLINE_DIR"/debs/*.deb >/dev/null 2>&1 || true
        $SUDO dpkg -i "$OFFLINE_DIR"/debs/*.deb >/dev/null 2>&1 || true
        # 校验【构建实际用到的】关键件都到位——不只 cmake/ninja/clang,还含 LLVM cmake 模块、
        # Z3 头文件、libmpi 运行库;否则某个 -dev 包未配置好会让后续 CMake 在远处报错、难排查。
        miss=""
        command -v cmake  >/dev/null 2>&1 || miss="$miss cmake"
        command -v ninja  >/dev/null 2>&1 || miss="$miss ninja"
        command -v "clang-${LLVM_VER}" >/dev/null 2>&1 || miss="$miss clang-${LLVM_VER}"
        [ -d "/usr/lib/llvm-${LLVM_VER}/lib/cmake/llvm" ] || miss="$miss llvm-${LLVM_VER}-dev(cmake模块)"
        [ -f /usr/include/z3++.h ] || miss="$miss libz3-dev"
        ls /usr/lib/*/libmpi.so* >/dev/null 2>&1 || miss="$miss libopenmpi(运行库)"
        if [ -z "$miss" ]; then
            info "离线系统依赖安装完成（经 dpkg 回退,关键件齐全）。"
        else
            error "离线安装后关键件仍缺失:$miss"
            error "目标机可能不是标准 Ubuntu ${LLVM_VER} 环境,缺更底层的基础包。"
            error "可在一台联网的同版本机器上 ./package.sh --offline 重新生成更完整的离线包。"
            exit 1
        fi
    fi
else
    step "安装系统依赖（apt）"
    if ! command -v apt-get >/dev/null 2>&1; then
        error "未检测到 apt-get。本脚本自动安装仅支持 Ubuntu/Debian。"
        error "请参考 README.md『先决条件』一节手动安装依赖，然后用 --skip-apt 重跑。"
        exit 1
    fi
    info "更新包索引..."
    $SUDO apt-get update -qq || warn "apt-get update 失败，继续尝试安装..."
    # 必须在 apt-get update【之后】再判断目标 LLVM 版本是否可用：全新机器上包列表陈旧/为空，
    # 明明可装的 clang-${LLVM_VER} 会被误判为不可用（Candidate: none），从而无谓退回默认 clang
    # （在非 24.04 发行版上默认 clang 未必是 ${LLVM_VER}，可能导致构建异常）。
    # 不能写成 `apt-cache ... | grep -q`：grep -q 命中后立即关闭管道，上游 apt-cache 收到
    # SIGPIPE 退出 141，在 set -o pipefail 下整条管道被判为“失败”→ 明明命中却误判为不可用而退回。
    # 故先把输出捕获到变量，再用 here-string 喂给 grep（无管道，无 SIGPIPE）。
    llvm_policy="$(apt-cache policy "clang-${LLVM_VER}" 2>/dev/null || true)"
    if ! grep -q "Candidate: [0-9]" <<<"$llvm_policy"; then
        warn "当前 apt 源没有 clang-${LLVM_VER}；改用发行版默认的 clang / llvm-dev。"
        warn "（若构建失败，请从 https://apt.llvm.org 安装 LLVM ${LLVM_VER} 后用 --skip-apt 重跑）"
        APT_PACKAGES=("${APT_PACKAGES[@]/clang-${LLVM_VER}/clang}")
        APT_PACKAGES=("${APT_PACKAGES[@]/llvm-${LLVM_VER}-dev/llvm-dev}")
        APT_PACKAGES=("${APT_PACKAGES[@]/llvm-${LLVM_VER}-tools/llvm}")
    fi
    info "安装: ${APT_PACKAGES[*]}"
    if $SUDO apt-get install -y "${APT_PACKAGES[@]}"; then
        info "系统依赖安装完成。"
    else
        error "apt 安装失败。请检查报错，或参考 README.md 手动安装后用 --skip-apt 重跑。"
        exit 1
    fi
fi

# ============================================================
#  2. 安装 AFL++（混合模糊测试所需；已存在则跳过）
# ============================================================
if [ "$SKIP_AFL" = true ]; then
    step "跳过 AFL++ 安装（--skip-afl）"
elif command -v afl-fuzz >/dev/null 2>&1 && find_afldriver >/dev/null; then
    step "AFL++ 已安装，跳过"
    ok "$(afl-fuzz --version 2>&1 | head -1 || echo afl-fuzz)"
elif [ "$OFFLINE" = true ]; then
    step "安装 AFL++（离线：解包随包预编译产物到 /usr/local）"
    afl_tar="$OFFLINE_DIR/afl/afl-usr-local.tar.gz"
    if [ -f "$afl_tar" ]; then
        # 解包到 /usr/local 即可被 PATH 与 find_afldriver 命中。
        $SUDO tar -C /usr/local -xzf "$afl_tar"
        # afl-fuzz 动态链接 libpython3.12.so.1.0；极简目标机可能缺此 .so。若缺,用随包
        # libpython3.12t64 以 --force-depends 补装(仅为提供该 .so,绕开其严格 = 版本锁)。
        if ! ls /usr/lib/*/libpython3.12.so.1.0 >/dev/null 2>&1; then
            pylib=$(ls "$OFFLINE_DIR"/afl/libpython3.12t64_*.deb 2>/dev/null | head -1)
            [ -n "$pylib" ] && $SUDO dpkg -i --force-depends "$pylib" >/dev/null 2>&1 || true
        fi
        # 真正校验 afl-fuzz 能加载运行(仅检查存在会漏掉"缺 .so 无法加载"的情况)
        if command -v afl-fuzz >/dev/null 2>&1 && find_afldriver >/dev/null \
           && ! ldd "$(command -v afl-fuzz)" 2>/dev/null | grep -q "not found"; then
            info "AFL++ 离线安装完成（含 libAFLDriver.a,afl-fuzz 可正常加载）。"
        elif command -v afl-fuzz >/dev/null 2>&1 && ldd "$(command -v afl-fuzz)" 2>/dev/null | grep -q "not found"; then
            warn "afl-fuzz 已解包但缺少运行库($(ldd "$(command -v afl-fuzz)" 2>/dev/null | awk '/not found/{print $1}' | tr '\n' ' '))"
            warn "——混合模糊测试不可用;目标机装上对应库即可。核心(纯符号执行/MPI)不受影响。"
        else
            warn "AFL++ 离线解包后仍未就绪——混合模糊测试不可用,项目其余部分不受影响。"
        fi
    else
        warn "离线包未含 AFL++（offline/afl/ 为空）——混合模糊测试不可用,其余功能正常。"
    fi
else
    step "从源码安装 AFL++（混合模糊测试需要）"
    warn "AFL++ 编译需要几分钟；若失败，纯符号执行/MPI 仍可正常使用（仅混合模式不可用）。"
    AFL_SRC="$SCRIPT_DIR/third_party/AFLplusplus"
    (
        set -e
        mkdir -p "$SCRIPT_DIR/third_party"
        # 上次 clone 若被中断，会留下无 .git 的残目录，导致 git clone 报 "already exists"——先清掉
        if [ -e "$AFL_SRC" ] && [ ! -d "$AFL_SRC/.git" ]; then
            rm -rf "$AFL_SRC"
        fi
        if [ ! -d "$AFL_SRC/.git" ]; then
            git clone --depth 1 https://github.com/AFLplusplus/AFLplusplus.git "$AFL_SRC"
        fi
        cd "$AFL_SRC"
        # LLVM_CONFIG 指向已装版本，确保 afl-clang-fast / libAFLDriver.a 一并构建
        make -j"$(nproc)" all LLVM_CONFIG="$(command -v llvm-config-${LLVM_VER} llvm-config 2>/dev/null | head -1)"
        $SUDO make install
    )
    # make all 用 `-$(MAKE) -C utils/aflpp_driver` 构建驱动，前导 `-` 会吞掉其失败并仍返回 0，
    # 故此处显式校验 libAFLDriver.a，避免"afl-fuzz 在、但驱动缺失"被误报为成功。
    if command -v afl-fuzz >/dev/null 2>&1 && find_afldriver >/dev/null; then
        info "AFL++ 安装完成（含 libAFLDriver.a）。"
    elif command -v afl-fuzz >/dev/null 2>&1; then
        warn "afl-fuzz 已装，但未找到 libAFLDriver.a——持久模式基准不可用（普通混合模糊仍可用）。"
    else
        warn "AFL++ 安装未成功——混合模糊测试将不可用，但项目其余部分不受影响。"
    fi
fi

# ============================================================
#  3. Python 虚拟环境 + 依赖
# ============================================================
step "创建 Python 虚拟环境并安装依赖"
if [ "$OFFLINE" = true ]; then
    # 离线：用 --without-pip 建 venv——只依赖 base 标准库,【不】需要 apt 的 pythonX.Y-venv,
    # 从而彻底避开它与目标机 python3.12 的严格 = 版本锁;再从随包 pip wheel 引导出 pip。
    if [ ! -d "$VENV_DIR" ]; then
        info "创建虚拟环境（--without-pip,离线）: $VENV_DIR"
        python3 -m venv --without-pip "$VENV_DIR" || { error "创建 venv 失败（目标机缺 base python3?）。"; exit 1; }
    else
        info "复用已有虚拟环境: $VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    # 引导 pip：venv 是 --without-pip 建的,直接用 wheel 内自带的 pip 模块装出 pip（zip 可执行）
    if ! python3 -m pip --version >/dev/null 2>&1; then
        pipwhl="$(ls "$OFFLINE_DIR"/wheels/pip-*.whl 2>/dev/null | head -1)"
        if [ -z "$pipwhl" ]; then error "离线包缺 pip wheel,无法引导 venv。"; exit 1; fi
        info "从 wheel 引导 pip: $(basename "$pipwhl")"
        python3 "$pipwhl/pip" install --no-index --find-links "$OFFLINE_DIR/wheels" pip setuptools wheel \
            || { error "pip 引导失败。"; exit 1; }
    fi
    # mpi4py 用 manylinux 预编译 wheel,运行期 dlopen 系统 libmpi.so.40（由离线 openmpi deb 提供）
    if python3 -m pip install --quiet --no-index --find-links "$OFFLINE_DIR/wheels" \
         -r "$SCRIPT_DIR/requirements.txt"; then
        info "Python 依赖离线安装完成（mpi4py / lit / ruff,来自 offline/wheels）。"
    else
        error "离线 pip 安装失败：offline/wheels 内缺少匹配目标机 Python 版本的 wheel。"
        error "（离线包按打包机的 Python 版本构建,目标机 Python 主次版本需一致,如同为 3.12。）"
        exit 1
    fi
else
    if [ ! -d "$VENV_DIR" ]; then
        info "创建虚拟环境: $VENV_DIR"
        python3 -m venv "$VENV_DIR" || { error "创建 venv 失败（请确认已装 python3-venv）。"; exit 1; }
    else
        info "复用已有虚拟环境: $VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    python3 -m pip install --quiet --upgrade pip
    if python3 -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt"; then
        info "Python 依赖安装完成（mpi4py / lit / ruff）。"
    else
        error "pip 安装失败。请确认已安装 libopenmpi-dev（mpi4py 编译需要）。"
        exit 1
    fi
fi

# ============================================================
#  4. 拉取子模块
# ============================================================
step "准备运行时源码（SymCC 运行时 + QSYM 后端）"
if [ -e "$SCRIPT_DIR/runtime/CMakeLists.txt" ]; then
    # 源码已就绪：可能来自压缩包解压（子模块随包提供）或子模块已初始化——无需 git
    info "运行时源码已就绪（随包提供或子模块已初始化）。"
elif [ "$OFFLINE" = true ]; then
    # 离线模式绝不联网:此处缺源码只能报错,不能 git submodule(会访问外网,违背离线承诺)
    error "离线模式下缺少运行时源码（runtime/）,且不能联网拉取子模块。"
    error "说明离线包不完整——请让分发者用 ./package.sh --offline 重新打包（须含 runtime/）。"
    exit 1
elif [ -f "$SCRIPT_DIR/.gitmodules" ] && git -C "$SCRIPT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    # 是 git 仓库但子模块尚未拉取（用 rev-parse 判定,兼容 worktree/子模块签出时 .git 为文件的情形）
    git -C "$SCRIPT_DIR" submodule update --init --recursive && info "子模块就绪。" \
        || warn "子模块拉取失败，请检查网络后重试：git submodule update --init --recursive"
else
    error "缺少运行时源码（runtime/），且当前不是 git 仓库，无法自动获取。"
    error "若你是从压缩包解压的，说明打包不完整——请让分发者用 ./package.sh 重新打包。"
    exit 1
fi

# ============================================================
#  5. 编译 SymCC
# ============================================================
step "编译 SymCC（调用 build.sh）"
if bash "$SCRIPT_DIR/build.sh"; then
    BUILD_OK=true
else
    BUILD_OK=false
    error "构建失败，请检查上方 CMake/ninja 报错。"
fi

# ============================================================
#  5.5 编译 SymSan（第二 concolic 引擎,--engine symsan）——仅当随包提供了子包
# ============================================================
# 设计：SymSan 是【可选】引擎。缺子包时安静跳过（SymCC 主线不受影响);有子包才构建。
# 构建失败也【不】让整个 setup 失败——SymCC 已经能用,SymSan 缺失只是少一个引擎。
SYMSAN_DIR="$OFFLINE_DIR/symsan"
SYMSAN_OK=skipped
if [ -f "$SYMSAN_DIR/symsan-src.tar.gz" ]; then
    step "编译 SymSan 引擎（第二 concolic 引擎,--engine symsan）"
    SYMSAN_ROOT="$SCRIPT_DIR/third_party/symsan"
    if [ -f "$SYMSAN_ROOT/install/bin/ko-clang" ]; then
        info "SymSan 已构建过（$SYMSAN_ROOT/install/bin/ko-clang）——跳过。"
        SYMSAN_OK=true
    else
        mkdir -p "$SYMSAN_ROOT"
        # 解包 vendored 源码（补丁已在打包时预先应用,故此处无需 git/patch）
        if [ ! -f "$SYMSAN_ROOT/CMakeLists.txt" ]; then
            info "解包 SymSan 源码到 third_party/symsan ..."
            tar -C "$SYMSAN_ROOT" -xzf "$SYMSAN_DIR/symsan-src.tar.gz"
        fi
        # 专用 Z3（>= 4.8.15;系统 libz3-dev 4.8.12 对 SymSan 过旧）
        if [ -f "$SYMSAN_DIR/z3/bin/libz3.so" ]; then
            export Z3_ROOT="$SYMSAN_DIR/z3"
            info "使用随包 Z3: $(cat "$SYMSAN_DIR/z3/VERSION" 2>/dev/null || echo '?')"
        else
            warn "随包未含专用 Z3——将尝试系统 Z3（4.8.12 通常会编译失败）。"
        fi
        export SYMSAN_SRC="$SYMSAN_ROOT" SYMSAN_INSTALL="$SYMSAN_ROOT/install"
        if bash "$SCRIPT_DIR/scripts/build_symsan.sh"; then
            SYMSAN_OK=true
            info "SymSan 构建成功。"
        else
            SYMSAN_OK=false
            warn "SymSan 构建失败——SymCC 主线不受影响,--engine symsan 暂不可用。"
            warn "可稍后单独重跑:  SYMSAN_SRC=$SYMSAN_ROOT Z3_ROOT=$SYMSAN_DIR/z3 ./scripts/build_symsan.sh"
        fi
    fi
fi

# ============================================================
#  收尾：复核 + 使用提示
# ============================================================
step "部署结果复核"
check_dependencies || true

echo
if [ "${BUILD_OK:-false}" = true ]; then
    echo -e "${GREEN}${BOLD}==================== 部署成功 ✔ ====================${NC}"
    echo
    echo -e "${BOLD}激活虚拟环境（每个新终端都要执行一次）:${NC}"
    echo "    source ${VENV_DIR}/bin/activate"
    echo
    echo -e "${BOLD}最简单的上手命令（纯符号执行）:${NC}"
    echo "    echo 'test' | build/symcc --help    # 查看编译器封装用法"
    echo
    if [ "$SYMSAN_OK" = true ]; then
        _ss="$SCRIPT_DIR/third_party/symsan"
        echo -e "${BOLD}第二 concolic 引擎 SymSan 已就绪,这样切换:${NC}"
        echo "    export SYMSAN_FGTEST=${_ss}/install/bin/fgtest"
        echo "    export SYMSAN_KO_CLANG=${_ss}/install/bin/ko-clang"
        echo "    python benchmark/run_benchmark.py --engine symsan ..."
        echo "    # SOTA 求解栈(I2S→JIGSAW→Z3): 再加 SYMSAN_SOLVER=rgd"
        echo
    fi
    echo -e "${BOLD}完整的『如何运行』说明见:${NC} README.md"
    echo "    - 纯符号执行（单核）"
    echo "    - MPI 并行符号执行（多核）"
    echo "    - AFL++ 与 SymCC 混合模糊测试"
    echo "    - 双引擎切换:  --engine symcc|symsan（见 docs/engine_abstraction.md）"
    echo "    - 一键基准测试:  python benchmark/run_benchmark.py"
else
    echo -e "${YELLOW}${BOLD}依赖已就绪，但 SymCC 构建失败。${NC}"
    echo "请查看上方报错；修复后可单独重跑：./build.sh"
fi
