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
# 用法:
#   ./package.sh                 # 打包已提交源码（含子模块），剔除未使用的 PIN/Z3 大块
#   ./package.sh --all           # 额外包含未提交但未被忽略的文件（如 docs/ 下的报告、PDF）
#   ./package.sh --keep-vendored # 保留 qsym 自带的 PIN 发行版与内置 Z3 源码（体积大很多）
#   ./package.sh -o <file>       # 指定输出文件名；格式由扩展名决定：
#                                #   .zip -> zip 包；.tar.gz / .tgz -> tar 包（默认 symcc-package.tar.gz）
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[打包]${NC} $*"; }
step()  { echo -e "\n${BLUE}${BOLD}==> $*${NC}"; }
warn()  { echo -e "${YELLOW}[警告]${NC} $*"; }
error() { echo -e "${RED}[错误]${NC} $*" >&2; }

OUT="symcc-package.tar.gz"
INCLUDE_UNTRACKED=false
KEEP_VENDORED=false
while [ $# -gt 0 ]; do
    case "$1" in
        --all)           INCLUDE_UNTRACKED=true; shift ;;
        --keep-vendored) KEEP_VENDORED=true; shift ;;
        -o|--output)     OUT="$2"; shift 2 ;;
        -h|--help) sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) error "未知参数: $1（用 --help 查看用法）"; exit 1 ;;
    esac
done

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
if [ "$FORMAT" = zip ] && ! command -v zip >/dev/null 2>&1; then
    error "生成 zip 需要 zip 命令：sudo apt-get install -y zip"; exit 1
fi
# 顶层目录名（解压后得到 <name>/，避免文件散落到当前目录）；去掉扩展名
TOPDIR="$(basename "$OUT")"; TOPDIR="${TOPDIR%.zip}"; TOPDIR="${TOPDIR%.tar.gz}"; TOPDIR="${TOPDIR%.tgz}"

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

step "生成压缩包：$OUT（格式：$FORMAT）"
rm -f "$OUT"
if [ "$FORMAT" = tar ]; then
    # --transform 让所有成员落入顶层目录 $TOPDIR/，解压得到干净的一个目录。
    # 标志 rhS：改写普通成员名(r)与硬链接目标(h)，但【不】改写符号链接目标(S)——
    # 本仓库有 8 个相对符号链接（如 test/README -> ../docs/Testing.txt），若给其目标
    # 也加上前缀会变成 symcc/../... 而失效。
    tar --null --files-from="$filelist" \
        --transform "s,^,${TOPDIR}/,rhS" \
        --owner=0 --group=0 \
        -czf "$OUT"
else
    # zip 没有 --transform：先把清单里的文件（用 tar 管道拷贝，原样保留符号链接与权限）
    # 落入临时 $TOPDIR/ 目录，再 zip -r -y（-y=保留符号链接）打包，得到顶层唯一目录的干净 zip。
    OUT_ABS="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
    STAGE="$(mktemp -d)"
    mkdir -p "$STAGE/$TOPDIR"
    tar --null --files-from="$filelist" -cf - | tar -C "$STAGE/$TOPDIR" -xf -
    ( cd "$STAGE" && zip -q -r -y "$OUT_ABS" "$TOPDIR" )
fi

# ---- 自检：确认关键内容在、垃圾内容不在 ----
step "自检打包结果"
if [ "$FORMAT" = tar ]; then listing="$(tar tzf "$OUT")"; else listing="$(unzip -Z1 "$OUT")"; fi
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
# benchmark/public 下已提交的是小型种子/靶子（约 11M），应当包含；1.6G 的下载物是【未提交】
# 的，git ls-files 天然排除。这里只做体积保护：若不慎混入大额下载物，包会异常大。
n_pub=$(echo "$listing" | grep -c "${TOPDIR}/benchmark/public/" || true)
info "  benchmark/public 已提交文件 $n_pub 个（小型种子/靶子；1.6G 下载物已排除）"
size_mb=$(du -m "$OUT" | cut -f1)
if [ "$size_mb" -lt 300 ]; then
    echo -e "  ${GREEN}✔${NC} 体积正常（${size_mb} MB，未混入大额下载物）"
else
    echo -e "  ${YELLOW}⚠${NC} 体积偏大（${size_mb} MB）——请确认未混入 benchmark/public 的下载物"
fi

size="$(du -h "$OUT" | cut -f1)"
sha="$(sha256sum "$OUT" | cut -d' ' -f1)"
echo
echo -e "${GREEN}${BOLD}==================== 打包完成 ✔ ====================${NC}"
echo "  文件:   $OUT   （$size）"
echo "  SHA256: $sha"
echo
if [ "$FORMAT" = zip ]; then extract_cmd="unzip $(basename "$OUT")"; else extract_cmd="tar xzf $(basename "$OUT")"; fi
echo -e "${BOLD}发给对方后，对方只需（无需 git）：${NC}"
echo "    ${extract_cmd}"
echo "    cd ${TOPDIR}"
echo "    ./setup.sh          # 装依赖 + 编译（全新机器）"
echo "    # 或者，若依赖已就绪： ./build.sh"
echo
echo "  完整运行/开发说明见解压后的 README.md。"
