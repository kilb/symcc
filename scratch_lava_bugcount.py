#!/usr/bin/env python3
"""LAVA-M "listed bug" 计数工具。

用途：把某个 fuzzer 生成的整份语料（corpus）逐个输入回放到对应的 LAVA-M 程序里，
统计一共触发了多少个「不同」的已注入 bug。

原理：LAVA-M 在每个注入点埋了一句
    dprintf(1, "Successfully triggered bug %d, crashing now!\n", bug_num);
（本仓库 base64/md5sum/uniq 的源码用的就是 dprintf(1,...)——它直接写文件描述符 1，
绕过 stdio 缓冲，因此即使随后进程 SEGV 崩溃，这行字也已经进了管道/落了盘。）
所以只要把输入喂进去、抓取 stdout+stderr、正则匹配出所有 bug 编号，去重即可。

注意：本仓库预编译的 who 二进制里该打印被 #if 0 关掉了（只会静默 SEGV，拿不到 bug 号），
需要用「打开了 dprintf 打印」的 who 重新编译（见 --binary 覆盖，或用旁边的 who_countable）。
SymCC 版二进制（bin/lava-m/<prog>）会打印 QSYM 噪声，请勿用于计数——默认走 lava-m-cov。

示例：
    python scratch_lava_bugcount.py base64 benchmark/public/seeds/lava-m/base64
    python scratch_lava_bugcount.py who  /path/to/fuzzer_out/queue --binary /path/to/who_countable
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# 仓库根目录（本脚本就放在仓库根下）
REPO_ROOT: Path = Path(__file__).resolve().parent

# 触发行的正则：匹配 "Successfully triggered bug 123" 里的编号。
# 用 bytes 正则，避免非 UTF-8 输出（LAVA-M 会往 stdout 吐二进制解码结果）解码报错。
_TRIGGER_RE: re.Pattern[bytes] = re.compile(rb"Successfully triggered bug (\d+)")


class ProgSpec:
    """单个 LAVA-M 程序的回放规格。"""

    def __init__(
        self,
        default_binary: str,
        fixed_args: list[str],
        listed_bugs: int,
        listed_file: str,
    ) -> None:
        # default_binary：相对仓库根的默认二进制路径（可被 --binary 覆盖）
        self.default_binary: str = default_binary
        # fixed_args：输入文件路径之前要传的固定参数（如 base64 的 "-d"）
        self.fixed_args: list[str] = fixed_args
        # listed_bugs：官方「listed / validated」bug 数（LAVA 论文口径，= validated_bugs 行数）
        self.listed_bugs: int = listed_bugs
        # listed_file：validated_bugs 文件（每行一个官方认定的 bug 编号），用于「listed 命中」口径
        self.listed_file: str = listed_file


# 每个程序的默认二进制 + 调用方式（来自各自的 validate.sh）：
#   base64  -> base64 -d <file>
#   md5sum  -> md5sum -c <file>
#   uniq    -> uniq       <file>
#   who     -> who        <file>
# 全部以「文件参数」（AFL 里的 @@）方式喂输入，而非 stdin。
# listed_bugs 数字同时也是各程序 validated_bugs 文件的行数（已在本仓库核对）。
_LAVA_ROOT = "benchmark/public/lava_corpus/LAVA-M"
PROGRAMS: dict[str, ProgSpec] = {
    "base64": ProgSpec(
        "benchmark/public/bin/lava-m-cov/base64", ["-d"], 44, f"{_LAVA_ROOT}/base64/validated_bugs"
    ),
    "md5sum": ProgSpec(
        "benchmark/public/bin/lava-m-cov/md5sum", ["-c"], 57, f"{_LAVA_ROOT}/md5sum/validated_bugs"
    ),
    "uniq": ProgSpec(
        "benchmark/public/bin/lava-m-cov/uniq", [], 28, f"{_LAVA_ROOT}/uniq/validated_bugs"
    ),
    # who 默认指向「重新编译、打开了 dprintf 打印」的可计数二进制；
    # 若不存在则回退到 lava-m-cov/who（注意：那个只会静默崩溃，计不出 bug 号）。
    "who": ProgSpec(
        "benchmark/public/bin/lava-m-cov/who_countable", [], 2136, f"{_LAVA_ROOT}/who/validated_bugs"
    ),
}


def _load_listed_bugs(spec: ProgSpec) -> set[int]:
    """读取官方 validated_bugs 文件（每行一个 bug 编号），返回编号集合；不存在则返回空集。"""
    path = REPO_ROOT / spec.listed_file
    if not path.exists():
        return set()
    listed: set[int] = set()
    for line in path.read_text().split():
        try:
            listed.add(int(line))
        except ValueError:
            continue
    return listed


def _resolve_binary(spec: ProgSpec, override: str | None) -> Path:
    """确定实际要执行的二进制路径。"""
    if override is not None:
        p = Path(override)
        return p if p.is_absolute() else (REPO_ROOT / p)
    p = REPO_ROOT / spec.default_binary
    # who 的可计数二进制不存在时，回退到 cov 版并给出告警
    if not p.exists() and spec.default_binary.endswith("who_countable"):
        fallback = REPO_ROOT / "benchmark/public/bin/lava-m-cov/who"
        if fallback.exists():
            print(
                f"[warn] {p} 不存在，回退到 {fallback}；"
                "该二进制不会打印 bug 编号（只会静默 SEGV），who 将计不出任何 bug。",
                file=sys.stderr,
            )
            return fallback
    return p


def _replay_one(
    binary: Path,
    fixed_args: list[str],
    input_path: Path,
    timeout: float,
    use_stdin: bool,
) -> set[int]:
    """回放单个输入文件，返回这次运行触发到的 bug 编号集合。"""
    if use_stdin:
        # 少数用法可能走 stdin；此时不把文件名当参数传
        argv = [str(binary), *fixed_args]
        try:
            stdin_data = input_path.read_bytes()
        except OSError:
            return set()
        stdin_arg: bytes | None = stdin_data
        stdin_src = subprocess.PIPE
    else:
        # LAVA-M 默认：输入作为文件参数（@@）
        argv = [str(binary), *fixed_args, str(input_path)]
        stdin_arg = None
        stdin_src = subprocess.DEVNULL

    try:
        proc = subprocess.run(
            argv,
            input=stdin_arg,
            stdin=None if use_stdin else stdin_src,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # 超时：仍尝试从已有输出里捞 bug 号（dprintf 可能已写出）
        out = (exc.stdout or b"") + b"\n" + (exc.stderr or b"")
        return {int(m) for m in _TRIGGER_RE.findall(out)}
    except (OSError, ValueError):
        # 二进制缺失/参数问题等：跳过该输入
        return set()

    # 注意：程序 SEGV 崩溃（returncode 为负/139）是正常现象，不影响读取输出
    combined = proc.stdout + b"\n" + proc.stderr
    return {int(m) for m in _TRIGGER_RE.findall(combined)}


def count_bugs(
    program: str,
    corpus_dir: Path,
    binary_override: str | None,
    timeout: float,
    use_stdin: bool,
) -> tuple[set[int], int]:
    """回放整份语料，返回 (去重后的 bug 编号集合, 回放的文件数)。"""
    if program not in PROGRAMS:
        raise ValueError(f"未知程序 {program!r}，可选：{', '.join(PROGRAMS)}")
    spec = PROGRAMS[program]
    binary = _resolve_binary(spec, binary_override)
    if not binary.exists():
        raise FileNotFoundError(f"二进制不存在：{binary}")
    if not corpus_dir.is_dir():
        raise NotADirectoryError(f"语料目录不存在：{corpus_dir}")

    triggered: set[int] = set()
    n_files = 0
    # 只回放普通文件（跳过子目录、AFL 的 .state 等）
    for entry in sorted(corpus_dir.iterdir()):
        if not entry.is_file():
            continue
        # 跳过 AFL 元数据文件
        if entry.name.startswith(".") or entry.name in {"README.txt", ".cur_input"}:
            continue
        n_files += 1
        triggered |= _replay_one(binary, spec.fixed_args, entry, timeout, use_stdin)
    return triggered, n_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="统计某份语料在 LAVA-M 程序上触发的不同 bug 数量。",
    )
    parser.add_argument("program", choices=sorted(PROGRAMS), help="LAVA-M 程序名")
    parser.add_argument("corpus_dir", type=Path, help="待回放的语料目录")
    parser.add_argument(
        "--binary", default=None, help="覆盖默认二进制路径（相对仓库根或绝对路径）"
    )
    parser.add_argument(
        "--timeout", type=float, default=5.0, help="单个输入的超时时间（秒），默认 5"
    )
    parser.add_argument(
        "--stdin", action="store_true", help="通过 stdin 而非文件参数喂输入（默认文件参数）"
    )
    args = parser.parse_args(argv)

    spec = PROGRAMS[args.program]
    binary = _resolve_binary(spec, args.binary)
    triggered, n_files = count_bugs(
        args.program, args.corpus_dir, args.binary, args.timeout, args.stdin
    )

    listed = _load_listed_bugs(spec)
    hit_listed = triggered & listed  # 命中「官方 listed 集合」的 bug
    extra = triggered - listed  # 触发了但不在官方 listed 集合里的（属更大的注入总体）

    print(f"程序          : {args.program}")
    print(f"二进制        : {binary}")
    print(f"调用方式      : {binary.name} {' '.join(spec.fixed_args)} <input-file>")
    print(f"语料目录      : {args.corpus_dir}  (回放 {n_files} 个文件)")
    print(f"官方 listed 总数: {spec.listed_bugs}"
          + (f"  (validated_bugs 实读 {len(listed)} 条)" if listed else "  (未找到 validated_bugs)"))
    print(f"触发的不同 bug 数（全部）: {len(triggered)}")
    if listed:
        # 「listed 命中」= 严格按 LAVA 论文口径的分数：与 validated_bugs 求交集
        print(f"其中命中 listed 集合    : {len(hit_listed)} / {spec.listed_bugs}")
        if extra:
            print(f"额外触发（非 listed）   : {len(extra)} 个 -> {sorted(extra)}")
    print(f"bug 编号（升序）: {sorted(triggered)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
