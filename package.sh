#!/usr/bin/env bash
#
# package.sh —— 把整个项目打包成一个自包含压缩包，供他人【无需 git】解压后直接构建/开发。
#
# 为什么需要它：本项目用到 git 子模块（运行时 / QSYM / Z3），直接 `git archive` 会漏掉
# 子模块源码，而 `tar` 整个目录又会带上 1.6G 的下载物、85M 的旧 build/ 和临时垃圾文件。
# 本脚本用 `git ls-files --recurse-submodules` 精确收集【全部源码（含子模块）+ 文档 +
# 脚本】，自动排除 .git、build/、.venv/、third_party/、benchmark/public/、__pycache__ 及
# 各类临时文件（遵循 .gitignore）。解压后 ./setup.sh 或 ./build.sh 均可直接使用，无需 git。
#
# 另外默认剔除两块【本项目构建不使用】的巨型 vendored 二进制/源码：qsym 子模块自带的
# Intel PIN 2.14 发行版（约 200M——SymCC 用假的 pin.H 桩，不链接真 PIN）与内置 Z3 源码
# （约 27M——构建走系统 libz3-dev，即 Z3_TRUST_SYSTEM_VERSION）。实测剔除二者后仍能完整
# 构建并生成测试用例，包体积从 ~245M 降到 ~18M。如确需保留，加 --keep-vendored。
#
# 【离线 / 内网】加 --offline 会把【全部系统与 Python 依赖】一并打进包（offline/ 目录）：
# apt 依赖（.deb,含 clang/LLVM-18、Z3、cmake/ninja、OpenMPI 运行库等）、pip wheels
# （mpi4py/lit/ruff,预编译）、本机预编译的 AFL++。目标内网机解压后 `./setup.sh` 全程不联网。
# 离线包约 +340M。须在联网机上生成,且目标机应为同架构同版本（默认 Ubuntu 24.04 amd64）。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[打包]${NC} $*"; }
step()  { echo -e "\n${BLUE}${BOLD}==> $*${NC}"; }
warn()  { echo -e "${YELLOW}[警告]${NC} $*"; }
error() { echo -e "${RED}[错误]${NC} $*" >&2; }

LLVM_VER=18

usage() {
    cat <<'EOF'
package.sh —— 把项目打包成自包含压缩包,供他人【无需 git】解压后构建/开发/运行。

用法:
  ./package.sh                 # 打包已提交源码（含子模块）,剔除未用的 PIN/Z3 大块
  ./package.sh --all           # 额外含未提交但未忽略的文件（docs 报告、PDF 等）
  ./package.sh --offline       # 【内网】再打进全部 apt/pip/AFL 依赖,目标机可离线部署(+~340M)
  ./package.sh --keep-vendored # 保留 qsym 自带的 PIN 发行版与内置 Z3 源码（体积大很多）
  ./package.sh --with-symsan   # 【第二引擎】再打进 SymSan 源码 + 专用 Z3 + 其 apt 依赖(+~50M)
  ./package.sh --no-symsan     # 关掉 SymSan（--offline 下默认是【开】的）
  ./package.sh -o <file>       # 输出文件名;格式看扩展名: .zip -> zip, .tar.gz/.tgz -> tar

联网/在线包默认名 symcc-package.tar.gz;离线包默认名 symcc-offline-package.tar.gz。

关于 SymSan（--engine symsan 的第二个 concolic 引擎）:
  SymSan 源码【不是】本仓库的子模块（上游 R-Fuzz/symsan,只读),故不在 git 清单里。
  --with-symsan 会把它连同【已打好本项目移植补丁】的源码一起 vendored 进 offline/symsan/,
  目标机 setup.sh 直接从该目录离线构建,不需要 git、不需要访问 GitHub。
  源码位置默认取 $SYMSAN_SRC;未设时按常见路径搜索,搜不到则报错(--no-symsan 可跳过)。
  另需专用 Z3 >= 4.8.15（Ubuntu 24.04 的 libz3-dev 是 4.8.12,过旧,SymSan 编不过),
  故另行 vendored 一份精简 Z3（仅 libz3.so + 头文件）。
EOF
}

# ---- 定位 SymSan 源码树（供 --with-symsan vendoring）----
# 顺序：$SYMSAN_SRC → 若干常见位置。必须看起来确实是 symsan（有 driver/fgtest.cpp）。
locate_symsan_src() {
    local c
    for c in "${SYMSAN_SRC:-}" "$HOME/symsan" "$HOME/code/symsan" \
             "$SCRIPT_DIR/../symsan" "/opt/symsan"; do
        [ -n "$c" ] && [ -f "$c/driver/fgtest.cpp" ] && [ -f "$c/runtime/dfsan/dfsan_flags.inc" ] \
            && { echo "$c"; return 0; }
    done
    return 1
}

# ---- 定位可用的 Z3 >= 4.8.15（SymSan 编译期需要;系统 4.8.12 不够）----
# 顺序：$Z3_ROOT → 常见解包位置。判定标准：有 bin/libz3.so 与 include/z3.h。
locate_z3_root() {
    local c
    for c in "${Z3_ROOT:-}" "$HOME"/z3-* /opt/z3-* /usr/local/z3-*; do
        [ -n "$c" ] && [ -f "$c/bin/libz3.so" ] && [ -f "$c/include/z3.h" ] \
            && { echo "$c"; return 0; }
    done
    return 1
}

# ---- 生成 SymSan 离线子包到 $1（布局: <dest>/{symsan-src.tar.gz,z3/,MANIFEST.txt}）----
generate_symsan_bundle() {
    local dest="$1" ss z3r
    mkdir -p "$dest"

    step "SymSan① vendoring 源码（含已应用的移植补丁）"
    if ! ss="$(locate_symsan_src)"; then
        error "找不到 SymSan 源码。请设 SYMSAN_SRC 指向 symsan 源码树,或用 --no-symsan 跳过。"
        error "  获取: git clone https://github.com/R-Fuzz/symsan"
        return 1
    fi
    info "SymSan 源码: $ss"

    # 把源码拷到暂存区。只去掉 .git(7M,目标机不需要 git)与【顶层】build/(本机 cmake 产物)。
    #
    # 注意 --exclude=./build 里的 "./" 不能省:写成 --exclude=build 会连同
    # libcxx/build_taint、libcxx/build_native 一起匹配掉。同理【不能】笼统排除 *.a/*.o ——
    # libcxx/build_taint/lib/{libc++,libc++abi,libunwind}.a 是【污点插桩版 libc++ 的预建产物】,
    # 是源码树的一部分而非临时产物:libcxx/CMakeLists.txt 会安装它们,C++ 目标要靠它们。
    # 一旦漏掉,目标机上 `make install` 会以
    #   "file INSTALL cannot find .../build_taint/lib/libc++.a" 失败,
    # 而重新生成它们要跑 libcxx/rebuild.sh —— 那是要联网拉 LLVM 源码的,离线机上根本无从补救。
    local tmpsrc; tmpsrc="$(mktemp -d)"
    tar -C "$ss" --exclude=.git --exclude=./build -cf - . | tar -C "$tmpsrc" -xf -

    # 【关键】把本项目的移植补丁【预先打进】vendored 源码。
    # 目标机没有 git,build_symsan.sh 里的 `git apply` 用不了;预打好后该步骤会被它的
    # 幂等检查（dfsan_flags.inc 已含 focus_bytes 即视为已打）自动跳过,故目标机全程无需 git。
    local patch="$SCRIPT_DIR/scripts/symsan_patches/symsan_ported_techniques.patch"
    if [ ! -f "$patch" ]; then
        error "缺少移植补丁: $patch"; rm -rf "$tmpsrc"; return 1
    fi
    if grep -q "focus_bytes" "$tmpsrc/runtime/dfsan/dfsan_flags.inc" 2>/dev/null; then
        info "源码树已含移植补丁（focus_bytes 已在）——直接沿用"
    else
        # 用 patch(1) 而非 git apply：vendored 树已无 .git
        if ( cd "$tmpsrc" && patch -p1 --silent < "$patch" ); then
            info "移植补丁已预先应用（④选择性符号化 ③字典 ②hint ①多字段组合 + 评审修复）"
        else
            error "移植补丁应用失败——vendored 的 SymSan 版本可能与补丁不匹配。"
            rm -rf "$tmpsrc"; return 1
        fi
    fi
    # 校验关键修复确实在 vendored 源码里（避免打出一个"看着有、其实没打上"的包）
    local k miss=0
    for k in "driver/symcc_techniques.h:env_enabled" \
             "driver/fgtest_rgd.cpp:__out_cap" \
             "driver/launcher/launch.c:SYMSAN_INVALID_ARGS" \
             "runtime/dfsan/dfsan_flags.inc:focus_bytes"; do
        local f="${k%%:*}" pat="${k##*:}"
        grep -q "$pat" "$tmpsrc/$f" 2>/dev/null || { error "vendored SymSan 缺: $f ($pat)"; miss=1; }
    done
    # 预建的插桩版 libc++ 必须在(否则目标机 make install 会失败,且离线无法补建,见上)
    local a
    for a in libc++.a libc++abi.a libunwind.a; do
        [ -f "$tmpsrc/libcxx/build_taint/lib/$a" ] \
            || { error "vendored SymSan 缺预建插桩 libc++: libcxx/build_taint/lib/$a"
                 error "  源码树里没有它 —— 请先在源码树跑 libcxx/rebuild.sh 再打包。"; miss=1; }
    done
    [ "$miss" = 0 ] || { rm -rf "$tmpsrc"; return 1; }

    ( cd "$tmpsrc" && tar czf "$dest/symsan-src.tar.gz" . )
    rm -rf "$tmpsrc"
    info "symsan-src.tar.gz: $(du -h "$dest/symsan-src.tar.gz" | cut -f1)"

    step "SymSan② vendoring 专用 Z3（>= 4.8.15;系统 libz3-dev 4.8.12 过旧编不过）"
    if ! z3r="$(locate_z3_root)"; then
        error "找不到 Z3 >= 4.8.15 的解包目录。请设 Z3_ROOT 指向它,或用 --no-symsan 跳过。"
        error "  获取: https://github.com/Z3Prover/z3/releases （取 z3-*-x64-glibc-*.zip 解压）"
        return 1
    fi
    # 只取 libz3.so + 头文件：完整 bin/ 有 140M(含 z3 可执行与各语言 binding),构建只需这两样
    mkdir -p "$dest/z3/bin" "$dest/z3/include"
    cp "$z3r/bin/libz3.so" "$dest/z3/bin/"
    cp -r "$z3r/include/." "$dest/z3/include/"
    [ -f "$z3r/LICENSE.txt" ] && cp "$z3r/LICENSE.txt" "$dest/z3/" || true
    echo "$(basename "$z3r")" > "$dest/z3/VERSION"
    info "Z3: $(basename "$z3r") —— 精简后 $(du -sh "$dest/z3" | cut -f1)（原 $(du -sh "$z3r" | cut -f1)）"

    {
        echo "SymSan 离线子包（--engine symsan 的第二 concolic 引擎）"
        echo "symsan-src.tar.gz : 上游 R-Fuzz/symsan 源码 + 本项目移植补丁（已预打,目标机无需 git）"
        echo "z3/               : Z3 $(cat "$dest/z3/VERSION")（仅 libz3.so + 头文件）"
        echo "构建: setup.sh 检测到本目录会自动调用 scripts/build_symsan.sh 离线构建。"
        echo "产物: ko-clang（编 *_symsan 目标）、fgtest（进程内 Z3）、fgtest_rgd（I2S→JIGSAW→Z3）"
    } > "$dest/MANIFEST.txt"
    info "SymSan 子包就绪: $(du -sh "$dest" | cut -f1)"
}

# ---- 生成离线依赖包到 $1（本机需联网 + apt/pip 可用）----
# 布局: <dest>/{debs,wheels,afl,MANIFEST.txt}
generate_offline_bundle() {
    local dest="$1"
    mkdir -p "$dest/debs" "$dest/wheels" "$dest/afl"
    local arch ubu; arch="$(dpkg --print-architecture 2>/dev/null || echo unknown)"
    ubu="$( . /etc/os-release 2>/dev/null && echo "${VERSION_ID:-?}" || echo '?')"

    # ① apt 依赖(.deb)：构建+运行 SymCC 所需的完整工具链 + MPI 运行库。
    #    用 openmpi-bin(不用 libopenmpi-dev,后者拖入 gfortran/flang 上百 M 且用不到);
    #    mpi4py 用预编译 wheel,运行期只需 libmpi.so.40(由 openmpi-bin 依赖的运行库提供)。
    step "离线① 下载 apt 依赖 (.deb) —— 约 300M,视网速需数分钟"
    # 不含 python3-venv/python3-pip：目标机已自带 base python3;Python 侧改由 setup.sh 用
    # `python3 -m venv --without-pip` + 随包 pip wheel 引导(避开 pythonX.Y-venv 与目标机
    # python3.12 的严格版本锁)。故 apt 侧完全不碰 Python 解释器,只装工具链与 MPI 运行库。
    # libzstd-dev / libncurses-dev：llvm-18-dev 的 cmake 导出(LLVMExports)里 LLVMSupport 引用了
    # 导入目标 zstd::libzstd_shared 与 Terminfo::terminfo,消费方 find_package(LLVM) 时需这两个
    # -dev 才能创建它们;它们【不是】llvm-18-dev 的硬依赖,故不在递归闭包里——极简目标机(无 -dev)
    # 上构建会因"target not found"失败。经裸机 chroot 实测:补上这两个即可正常构建。
    local refined=(build-essential cmake ninja-build \
        "clang-${LLVM_VER}" "llvm-${LLVM_VER}-dev" "llvm-${LLVM_VER}-tools" \
        libz3-dev zlib1g-dev libzstd-dev libncurses-dev openmpi-bin unzip pkg-config)
    # SymSan 引擎的额外构建依赖（仅 --with-symsan 时纳入闭包）:
    #  libc++/libc++abi/libunwind-18 —— SymSan 的 runtime 与 libcxx 桩按 LLVM 18 的 libc++ 编译;
    #  libboost-container-dev        —— rgd 解析器/任务队列用到 boost 容器;
    #  protobuf                      —— jigsaw 的 AST 序列化;
    #  libgoogle-perftools-dev       —— fgtest_rgd 链接 tcmalloc/profiler;
    #  libbsd-dev                    —— 上游若干工具函数。
    # 注意：Z3 不在此列 —— 系统 libz3-dev 是 4.8.12,对 SymSan 太旧,走 vendored 的 offline/symsan/z3。
    if $WITH_SYMSAN; then
        refined+=("libc++-${LLVM_VER}-dev" "libc++abi-${LLVM_VER}-dev" "libunwind-${LLVM_VER}-dev" \
                  libboost-container-dev protobuf-compiler libprotobuf-dev \
                  libgoogle-perftools-dev libbsd-dev)
    fi
    # 递归依赖【硬】闭包,剔除两类:
    #  ① python 解释器核心(python3 / python3.12 / libpython3*)——目标机 base 必已自带,且这些
    #     包彼此有严格 = 版本互锁,随包版本与目标机不一致会冲突;Python 侧改由 wheel 满足。
    #  ② make-guile —— 与 make 互斥(build-essential 依赖 `make | make-guile`,apt-cache --recurse
    #     会把两个候选都收进来,同时安装会 Conflicts;保留标准的 make 即可)。
    # 其余(含 openmpi 硬依赖的 ucx→amdhip→libllvm17 链、llvm-18-tools 的 python3-pygments/yaml
    # 等)全部保留。经依赖闭包静态校验 + apt 离线模拟:无未满足依赖、无包冲突。
    local closure
    closure="$(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
        --no-breaks --no-replaces --no-enhances --no-pre-depends "${refined[@]}" 2>/dev/null \
        | grep -E '^[a-z0-9]' | sort -u \
        | grep -vE '^(python3|python3-minimal|python3\.12|python3\.12-minimal|make-guile)$' \
        | grep -vE '^libpython3')"
    info "依赖闭包 $(echo "$closure" | wc -l) 个包,开始下载..."
    (
        cd "$dest/debs"
        apt-get download $closure 2>/dev/null || true
        # 补偿：部分包的候选版位于 -updates/-security,若本机镜像/代理未缓存该 pocket 会 404。
        # 对【尚未下到】的包逐个回退到 base pocket 的版本(pkg=版本号)重试——base 版本用于离线
        # 开发完全够用(只是不含最新安全更新)。此循环仅对缺失包触发,不影响已下到的包。
        cn="$( . /etc/os-release && echo "$VERSION_CODENAME")"
        for p in $closure; do
            ls "${p}"_*.deb >/dev/null 2>&1 && continue
            bver="$(apt-cache madison "$p" 2>/dev/null | awk -F'|' -v c="$cn" \
                '$3 ~ (c"/") && $3 !~ /(updates|security)/ {gsub(/ /,"",$2); print $2; exit}')"
            [ -n "$bver" ] && apt-get download "${p}=${bver}" 2>/dev/null || true
        done
    )
    # 校验关键包确已下到(glob 命中即可)。不含 Python：Python 侧走 wheel,不依赖 apt。
    local must=(cmake ninja-build "clang-${LLVM_VER}" "llvm-${LLVM_VER}-dev" \
        libz3-dev libz3-4 zlib1g-dev libzstd-dev libncurses-dev \
        libopenmpi3t64 openmpi-bin "libclang-cpp${LLVM_VER}")
    if $WITH_SYMSAN; then
        must+=("libc++-${LLVM_VER}-dev" "libc++abi-${LLVM_VER}-dev" libboost-container-dev \
               libprotobuf-dev libgoogle-perftools-dev libbsd-dev)
    fi
    local m miss=0
    for m in "${must[@]}"; do
        ls "$dest/debs/${m}"_*.deb >/dev/null 2>&1 || { error "离线 .deb 缺关键包: $m"; miss=1; }
    done
    [ "$miss" = 0 ] || { error "离线依赖不完整,请在联网机重试。"; return 1; }
    info "已下载 $(ls "$dest/debs"/*.deb 2>/dev/null | wc -l) 个 .deb（$(du -sh "$dest/debs" | cut -f1)）"

    # ② pip wheels（含 pip/setuptools/wheel 以便目标机在 --without-pip 的 venv 里引导 pip;
    #    mpi4py 有 manylinux 预编译 wheel,目标机无需编译）
    step "离线② 下载 pip wheels（pip/setuptools/wheel + mpi4py / lit / ruff）"
    if python3 -m pip download --quiet --dest "$dest/wheels" \
         pip setuptools wheel -r "$SCRIPT_DIR/requirements.txt"; then
        info "wheels: $(ls "$dest/wheels"/*.whl 2>/dev/null | wc -l) 个（$(du -sh "$dest/wheels" | cut -f1)）"
    else
        error "pip wheels 下载失败。"; return 1
    fi

    # ③ 本机预编译 AFL++（避免目标机离线编译 AFL;仅依赖 libc/libz/libexpat,通用）。
    #    要求 /usr/local/bin 下确有 afl-*（apt 装的 AFL 在 /usr/bin,不在此;避免打出空/残包）。
    step "离线③ 打包本机预编译 AFL++"
    local afl_tar="$dest/afl/afl-usr-local.tar.gz"
    if ls /usr/local/bin/afl-fuzz >/dev/null 2>&1 && [ -d /usr/local/lib/afl ]; then
        ( cd /usr/local && tar czf "$afl_tar" bin/afl-* lib/afl 2>/dev/null ) || true
        # 校验产出的 tar 确含 afl-fuzz 与持久模式驱动,否则删掉残包并如实标记未含。
        # 不能写 `tar tzf | grep -q`：grep -q 命中即关管道,tar 收 SIGPIPE 退 141,pipefail 下
        # 整条管道判失败 → 明明命中却误判缺失。故先把清单捕获到变量,再用 here-string 校验。
        afl_list="$(tar tzf "$afl_tar" 2>/dev/null || true)"
        if grep -q 'bin/afl-fuzz' <<<"$afl_list" && grep -q 'lib/afl/libAFLDriver.a' <<<"$afl_list"; then
            info "AFL++: $(du -h "$afl_tar" | cut -f1)（解压到目标机 /usr/local）"
        else
            rm -f "$afl_tar"
            warn "AFL++ 打包内容不完整（缺 afl-fuzz/libAFLDriver.a）——离线包将不含 AFL。"
        fi
        # afl-fuzz 动态链接 libpython3.12.so.1.0（AFL 的 Python mutator）。极简目标机可能无此 .so,
        # 而 libpython3.12t64 与 python3.12 有严格 = 版本锁,放进主 apt 事务会有冲突风险。故单独抓到
        # offline/afl/,由 setup.sh 仅在 .so 缺失时用 dpkg --force-depends 补装(只为给 afl 提供 .so)。
        (
            cd "$dest/afl"
            apt-get download libpython3.12t64 2>/dev/null || true
            # 同主闭包一样:候选版若在 -updates/-security 未被镜像缓存(404),回退到 base pocket 版本
            if ! ls libpython3.12t64_*.deb >/dev/null 2>&1; then
                cn="$( . /etc/os-release && echo "$VERSION_CODENAME")"
                bv="$(apt-cache madison libpython3.12t64 2>/dev/null | awk -F'|' -v c="$cn" \
                    '$3 ~ (c"/") && $3 !~ /(updates|security)/ {gsub(/ /,"",$2); print $2; exit}')"
                [ -n "$bv" ] && apt-get download "libpython3.12t64=${bv}" 2>/dev/null || true
            fi
        )
        ls "$dest"/afl/libpython3.12t64_*.deb >/dev/null 2>&1 \
            && info "  已备 libpython3.12t64（供极简目标机上 afl 补装 .so）" \
            || warn "  未能获取 libpython3.12t64（极简目标机上 afl 混合模糊或不可用,不影响核心功能）"
    else
        warn "本机 /usr/local 下无预编译 AFL++——离线包不含 AFL（混合模糊不可用,其余正常）"
    fi

    # MANIFEST
    {
        echo "SymCC 离线依赖包"
        echo "target-platform: Ubuntu ${ubu} ${arch}（目标机须同架构同版本）"
        echo "python: $(python3 --version 2>&1)（目标机 Python 主次版本需一致）"
        echo "debs: $(ls "$dest/debs"/*.deb 2>/dev/null | wc -l) 个"
        echo "wheels: $(ls "$dest/wheels"/*.whl 2>/dev/null | wc -l) 个"
        echo "afl: $([ -f "$dest/afl/afl-usr-local.tar.gz" ] && echo '已含预编译' || echo '未含')"
        echo "用法: 解压后 ./setup.sh 会自动检测本目录并离线安装。"
    } > "$dest/MANIFEST.txt"
    info "离线依赖包就绪：$(du -sh "$dest" | cut -f1)"
}

OUT=""
INCLUDE_UNTRACKED=false
KEEP_VENDORED=false
OFFLINE=false
WITH_SYMSAN=auto        # auto|true|false —— auto 时:离线包默认带,在线包默认不带
while [ $# -gt 0 ]; do
    case "$1" in
        --all)           INCLUDE_UNTRACKED=true; shift ;;
        --keep-vendored) KEEP_VENDORED=true; shift ;;
        --offline)       OFFLINE=true; shift ;;
        --with-symsan)   WITH_SYMSAN=true; shift ;;
        --no-symsan)     WITH_SYMSAN=false; shift ;;
        -o|--output)     [ $# -ge 2 ] || { error "-o/--output 需要一个文件名参数"; exit 1; }; OUT="$2"; shift 2 ;;
        -h|--help)       usage; exit 0 ;;
        *) error "未知参数: $1（用 --help 查看用法）"; exit 1 ;;
    esac
done
# 解析 SymSan：离线包默认带上（无网机器拿不到上游源码,不带等于第二引擎不可用);
# 在线包默认不带（目标机能自己 git clone,不必让包大 50M）。
if [ "$WITH_SYMSAN" = auto ]; then
    if $OFFLINE; then WITH_SYMSAN=true; else WITH_SYMSAN=false; fi
fi
# --with-symsan 但不是离线包：SymSan 子包放在 offline/ 布局下,故隐含开启离线打包
if $WITH_SYMSAN && ! $OFFLINE; then
    info "--with-symsan 需要 offline/ 布局,自动启用 --offline"
    OFFLINE=true
fi
# 默认输出名：离线包与在线包区分开
if [ -z "$OUT" ]; then
    if $OFFLINE; then OUT="symcc-offline-package.tar.gz"; else OUT="symcc-package.tar.gz"; fi
fi

# 必须在 git 仓库内运行——打包依赖 git 正确处理子模块与 .gitignore 排除规则
if ! git rev-parse --git-dir >/dev/null 2>&1; then
    error "请在项目的 git 仓库根目录运行本脚本（打包需要 git 来收集子模块源码）。"
    exit 1
fi

# 输出格式由扩展名决定：.zip -> zip；.tar.gz / .tgz -> tar
case "$OUT" in
    *.zip)          FORMAT=zip ;;
    *.tar.gz|*.tgz) FORMAT=tar ;;
    *) error "无法从扩展名判断格式：$OUT（请用 .zip 或 .tar.gz）"; exit 1 ;;
esac
# zip 格式：打包需要 zip,自检列目录需要 unzip——两者都要有,否则包生成后自检会中止
if [ "$FORMAT" = zip ]; then
    for c in zip unzip; do
        command -v "$c" >/dev/null 2>&1 || { error "生成/校验 zip 需要 $c：sudo apt-get install -y zip unzip"; exit 1; }
    done
fi
# 顶层目录名（解压后得到 <name>/，避免文件散落到当前目录）；去掉扩展名
TOPDIR="$(basename "$OUT")"; TOPDIR="${TOPDIR%.zip}"; TOPDIR="${TOPDIR%.tar.gz}"; TOPDIR="${TOPDIR%.tgz}"

# 尽早校验并解析输出路径(fail-fast):-o 指定的目录必须存在,否则 `cd "$(dirname)"` 会失败、
# OUT_ABS 退化成 /文件名 而写到根目录。在收集文件/下载依赖之前就拦下。
out_dir="$(dirname "$OUT")"
if [ ! -d "$out_dir" ]; then
    error "输出目录不存在: $out_dir（请先创建,或换一个 -o 路径）"; exit 1
fi
OUT_ABS="$(cd "$out_dir" && pwd)/$(basename "$OUT")"

step "收集文件清单"
filelist="$(mktemp)"; STAGE=""
trap 'rm -f "$filelist"; [ -n "$STAGE" ] && rm -rf "$STAGE"' EXIT

# 已提交文件（含所有层级子模块的源码）
git ls-files --recurse-submodules -z > "$filelist"
n_tracked=$(tr -cd '\0' < "$filelist" | wc -c)
info "已提交文件（含子模块源码）：$n_tracked"

# 未提交但未被忽略的文件（默认不含；--all 纳入）
mapfile -t untracked < <(git ls-files --others --exclude-standard)
if $INCLUDE_UNTRACKED; then
    if [ "${#untracked[@]}" -gt 0 ]; then
        git ls-files --others --exclude-standard -z >> "$filelist"
        info "额外纳入 ${#untracked[@]} 个未提交文件："
        printf '    %s\n' "${untracked[@]}"
    fi
else
    if [ "${#untracked[@]}" -gt 0 ]; then
        warn "以下 ${#untracked[@]} 个未提交且未被忽略的文件【未】包含"
        warn "（如需一并发送，加 --all 重新打包，或先 git add）："
        printf '    %s\n' "${untracked[@]}"
    fi
fi

# 剔除本项目构建【不使用】的 vendored 大块：qsym 自带的 Intel PIN 发行版（SymCC 用假 pin.H
# 桩，剔除后经实测仍可完整构建）与内置 Z3 源码（构建走系统 Z3）。--keep-vendored 可保留。
# 注意模式只匹配 third_party/ 下的 pin-*/ 与 z3/，不会误伤 SymCC 的假 pin.H（在 qsym/ 根下）。
if ! $KEEP_VENDORED; then
    before=$(tr -cd '\0' < "$filelist" | wc -c)
    if grep -zvE 'third_party/(pin-[^/]*|z3)/' "$filelist" > "$filelist.f"; then
        mv "$filelist.f" "$filelist"
    fi
    rm -f "$filelist.f"
    after=$(tr -cd '\0' < "$filelist" | wc -c)
    info "剔除未使用的 PIN/Z3 vendored 文件：$((before - after)) 个（如需保留：--keep-vendored）"
fi

step "组织打包内容到临时目录（顶层唯一目录 ${TOPDIR}/）"
# 统一走「暂存目录」：把清单文件用 tar 管道原样拷入 $STAGE/$TOPDIR（保留符号链接与权限），
# 再从暂存目录归档。较之前的 tar --transform 更简单,且对符号链接、附加 offline/ 目录都天然正确。
# （OUT_ABS 已在前面 fail-fast 校验并解析。）
STAGE="$(mktemp -d)"
mkdir -p "$STAGE/$TOPDIR"
tar --null --files-from="$filelist" -cf - | tar -C "$STAGE/$TOPDIR" -xf -

# 离线模式：把全部依赖打进 $TOPDIR/offline/
if $OFFLINE; then
    generate_offline_bundle "$STAGE/$TOPDIR/offline" || { error "离线依赖生成失败,已中止。"; exit 1; }
fi
# SymSan 第二引擎：源码（含预打补丁）+ 专用 Z3 → $TOPDIR/offline/symsan/
if $WITH_SYMSAN; then
    generate_symsan_bundle "$STAGE/$TOPDIR/offline/symsan" \
        || { error "SymSan 子包生成失败,已中止（如不需要该引擎,加 --no-symsan）。"; exit 1; }
fi

step "生成压缩包：$OUT（格式：$FORMAT）"
rm -f "$OUT_ABS"
if [ "$FORMAT" = tar ]; then
    tar -C "$STAGE" --owner=0 --group=0 -czf "$OUT_ABS" "$TOPDIR"
else
    ( cd "$STAGE" && zip -q -r -y "$OUT_ABS" "$TOPDIR" )
fi

# ---- 自检：确认关键内容在、垃圾内容不在 ----
step "自检打包结果"
if [ "$FORMAT" = tar ]; then listing="$(tar tzf "$OUT_ABS")"; else listing="$(unzip -Z1 "$OUT_ABS")"; fi
check() { # $1=描述 $2=期望(present/absent) $3=grep模式
    local cnt; cnt=$(echo "$listing" | grep -c "$3" || true)
    if { [ "$2" = present ] && [ "$cnt" -gt 0 ]; } || { [ "$2" = absent ] && [ "$cnt" -eq 0 ]; }; then
        echo -e "  ${GREEN}✔${NC} $1"
    else
        echo -e "  ${RED}✗${NC} $1（匹配 $cnt 项，期望 $2）"
    fi
}
check "含改造后的运行时源码 (solver.cpp)" present "qsym/pintool/solver.cpp"
check "含构建/部署脚本 (setup.sh)"        present "${TOPDIR}/setup.sh"
check "含文档 (README.md)"                present "${TOPDIR}/README.md"
check "不含 .git 目录"                     absent  "${TOPDIR}/\.git/"
check "不含旧 build/ 产物"                 absent  "${TOPDIR}/build/"
check "不含 .venv 虚拟环境"                absent  "${TOPDIR}/\.venv/"
if $OFFLINE; then
    check "离线 apt 依赖 (offline/debs)"   present "${TOPDIR}/offline/debs/.*\.deb"
    check "离线 pip wheels"                present "${TOPDIR}/offline/wheels/.*\.whl"
    check "离线依赖清单 MANIFEST"          present "${TOPDIR}/offline/MANIFEST.txt"
fi
if $WITH_SYMSAN; then
    check "SymSan 源码 (offline/symsan)"   present "${TOPDIR}/offline/symsan/symsan-src.tar.gz"
    check "SymSan 专用 Z3 (libz3.so)"      present "${TOPDIR}/offline/symsan/z3/bin/libz3.so"
    check "SymSan 移植补丁 (仓库内)"        present "${TOPDIR}/scripts/symsan_patches/"
    check "SymSan 构建脚本"                present "${TOPDIR}/scripts/build_symsan.sh"
    # 注意用方括号而不是 \+ 转义:check() 用的是 grep BRE,其中 `\+` 是【一个或多个】量词,
    # 写成 libc\+\+-18-dev 会被解析成 "lib" + c的重复,永远匹配不上(曾误报"缺包")。
    check "SymSan apt 依赖 (libc++-18-dev)" present "offline/debs/libc[+][+]-${LLVM_VER}-dev"
    check "SymSan apt 依赖 (libc++abi)"     present "offline/debs/libc[+][+]abi-${LLVM_VER}-dev"
    check "SymSan apt 依赖 (boost/protobuf/tcmalloc)" present "offline/debs/libgoogle-perftools-dev"
fi
# benchmark/public 下已提交的是小型种子/靶子（约 11M），应当包含；1.6G 的下载物是【未提交】
# 的，git ls-files 天然排除。这里只做体积保护：若不慎混入大额下载物，包会异常大。
n_pub=$(echo "$listing" | grep -c "${TOPDIR}/benchmark/public/" || true)
info "  benchmark/public 已提交文件 $n_pub 个（小型种子/靶子；1.6G 下载物已排除）"
size_mb=$(du -m "$OUT_ABS" | cut -f1)
# 在线包应 <300M；离线包含 ~340M 依赖,阈值放宽到 700M
limit=$([ "$OFFLINE" = true ] && echo 700 || echo 300)
if [ "$size_mb" -lt "$limit" ]; then
    echo -e "  ${GREEN}✔${NC} 体积正常（${size_mb} MB）"
else
    echo -e "  ${YELLOW}⚠${NC} 体积偏大（${size_mb} MB）——请确认未混入意外的大文件"
fi

size="$(du -h "$OUT_ABS" | cut -f1)"
sha="$(sha256sum "$OUT_ABS" | cut -d' ' -f1)"
echo
echo -e "${GREEN}${BOLD}==================== 打包完成 ✔ ====================${NC}"
echo "  文件:   $OUT   （$size）"
echo "  SHA256: $sha"
echo
if [ "$FORMAT" = zip ]; then extract_cmd="unzip $(basename "$OUT")"; else extract_cmd="tar xzf $(basename "$OUT")"; fi
if $OFFLINE; then
    echo -e "${BOLD}内网/离线部署——把本文件拷到目标机（无需外网、无需 git）：${NC}"
    echo "    ${extract_cmd}"
    echo "    cd ${TOPDIR}"
    echo "    ./setup.sh          # 自动检测 offline/ 并【离线】装依赖 + 编译"
    echo
    echo "  注：目标机须与打包机同架构同版本（默认 Ubuntu 24.04 amd64）、Python 主次版本一致；"
    echo "      apt 装依赖仍需 sudo（本地 .deb,不联网）。详见解压后 README.md『内网离线部署』。"
else
    echo -e "${BOLD}发给对方后，对方只需（无需 git）：${NC}"
    echo "    ${extract_cmd}"
    echo "    cd ${TOPDIR}"
    echo "    ./setup.sh          # 装依赖 + 编译（全新机器,需联网）"
    echo "    # 或者，若依赖已就绪： ./build.sh"
    echo
    echo "  完整运行/开发说明见解压后的 README.md。"
fi
