#!/usr/bin/env python3
"""引擎覆盖率对拍:同一 worker 路径(run_symcc_worker + concolic_engine)下,SymCC 与 SymSan
在同一目标、同时间预算跑反馈式 concolic campaign,用 afl-showmap 量各自语料的边覆盖。

前置(以 deep_branches 为例):
  build/symcc -O2 benchmark/targets/deep_branches.c -o /tmp/db_symcc
  KO_CC=clang-18 KO_USE_FASTGEN=1 KO_DONT_OPTIMIZE=1 <ko-clang> -O2 -o /tmp/db_symsan benchmark/targets/deep_branches.c
  afl-clang-fast -O2 benchmark/targets/deep_branches.c -o /tmp/db_afl
用法:  SYMSAN_FGTEST=/path/to/fgtest \
        python3 scripts/engine_coverage_dogfight.py /tmp/db_symcc /tmp/db_symsan /tmp/db_afl 8
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "util"))
import mpi_fuzzing_helper as m  # noqa: E402


def coverage(afl_bin: str, corpus: str) -> "tuple[int, int]":
    """afl-showmap -C 量 corpus 的边覆盖,返回 (命中边, 总边)。"""
    cwd = tempfile.mkdtemp()
    try:
        r = subprocess.run(
            ["afl-showmap", "-C", "-i", corpus, "-o", "/dev/null", "-m", "none",
             "-t", "2000", "--", afl_bin, "@@"],
            capture_output=True, text=True, cwd=cwd)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    mm = re.search(r"coverage of (\d+) edges .* out of (\d+)", r.stderr + r.stdout)
    return (int(mm.group(1)), int(mm.group(2))) if mm else (0, 0)


def campaign(engine: str, target_cmd: "list[str]", afl_bin: str,
             in_len: int, budget: float = 45.0) -> "tuple[int, int, int, float]":
    """反馈式 concolic campaign,返回 (唯一输入数, 命中边, 总边, 耗时)。"""
    os.environ["SYMCC_ENGINE"] = engine
    corpus = tempfile.mkdtemp(prefix=f"c_{engine}_")
    seed = b"A" * in_len
    with open(os.path.join(corpus, "seed"), "wb") as f:
        f.write(seed)
    seen = {hashlib.md5(seed).digest()}
    queue = [seed]
    n = 1
    t0 = time.monotonic()
    while queue and time.monotonic() - t0 < budget:
        inp = queue.pop(0)
        sd = tempfile.mkdtemp()
        p = os.path.join(sd, "in")
        with open(p, "wb") as f:
            f.write(inp)
        od = tempfile.mkdtemp(prefix=f"o_{engine}_")
        try:
            new_tests = m.run_symcc_worker(target_cmd, p, od, 15, False)[0]
        except Exception:
            new_tests = []
        for tc in new_tests:
            c = tc["content"]
            h = hashlib.md5(c).digest()
            if h in seen:
                continue
            seen.add(h)
            n += 1
            queue.append(c)
            with open(os.path.join(corpus, f"id{n}"), "wb") as f:
                f.write(c)
        shutil.rmtree(sd, ignore_errors=True)
        shutil.rmtree(od, ignore_errors=True)
    dt = time.monotonic() - t0
    e, tot = coverage(afl_bin, corpus)
    shutil.rmtree(corpus, ignore_errors=True)
    return n, e, tot, dt


def main() -> None:
    symcc_bin, symsan_bin, afl_bin, in_len = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    print(f"{'engine':8}{'inputs':>9}{'edges':>8}{'/total':>8}{'time':>8}")
    for engine, tc in [("symcc", [symcc_bin, "@@"]), ("symsan", [symsan_bin])]:
        n, e, tot, dt = campaign(engine, tc, afl_bin, in_len)
        print(f"{engine:8}{n:>9}{e:>8}{'/' + str(tot):>8}{dt:>6.1f}s")


if __name__ == "__main__":
    main()
