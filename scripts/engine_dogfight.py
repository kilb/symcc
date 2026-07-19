#!/usr/bin/env python3
"""引擎对拍 demo:同一 worker 路径(run_symcc_worker + concolic_engine.get_engine)下,
用 SymCC 与 SymSan 两个引擎跑同一目标,反馈循环逐层解开嵌套魔数,验证 --engine 可切换且等价。

前置:
  1. 三份 parser 二进制:
       build/symcc -O2 benchmark/targets/parser.c -o /tmp/parser_symcc
       KO_CC=clang-18 KO_USE_FASTGEN=1 KO_DONT_OPTIMIZE=1 <ko-clang> -o /tmp/parser_symsan benchmark/targets/parser.c
  2. SymSan 构建产出的 fgtest,经 SYMSAN_FGTEST 指定(见 scripts/build_symsan.sh)。
用法:  SYMSAN_FGTEST=/path/to/fgtest python3 scripts/engine_dogfight.py
"""
import os
import sys
import hashlib
import shutil
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "util"))
import mpi_fuzzing_helper as m  # noqa: E402

MAGIC = bytes([0x53, 0x59, 0x4D, 0x01])  # parser.c 的魔数 "SYM\x01"


def concolic_loop(engine: str, target_cmd: "list[str]", rounds: int = 8) -> "tuple[int, int, int]":
    """反馈式 concolic 循环:种子→引擎求解→新输入喂回,直到解出完整魔数或轮数用尽。
    返回 (达成轮次, 最深匹配字节数, 累计唯一输入数)。"""
    os.environ["SYMCC_ENGINE"] = engine
    seen: "set[bytes]" = set()
    queue: "list[bytes]" = [b"A" * 32]
    best = 0
    for r in range(rounds):
        nxt: "list[bytes]" = []
        for inp in queue:
            sd = tempfile.mkdtemp()
            p = os.path.join(sd, "in")
            with open(p, "wb") as f:
                f.write(inp)
            od = tempfile.mkdtemp(prefix=f"{engine}_")
            new_tests, _gen, _rc, _el, _k, _post = m.run_symcc_worker(
                target_cmd, p, od, 20, False)  # 无 showmap:收全部输出
            for tc in new_tests:
                c = tc["content"]
                h = hashlib.md5(c).digest()
                if h in seen:
                    continue
                seen.add(h)
                nxt.append(c)
                best = max(best, sum(1 for i in range(4) if i < len(c) and c[i] == MAGIC[i]))
                if c[:4] == MAGIC:
                    shutil.rmtree(sd, ignore_errors=True)
                    shutil.rmtree(od, ignore_errors=True)
                    return r + 1, 4, len(seen)
            shutil.rmtree(sd, ignore_errors=True)
            shutil.rmtree(od, ignore_errors=True)
        queue = nxt[:20]
    return rounds, best, len(seen)


def main() -> None:
    if "SYMSAN_FGTEST" not in os.environ:
        print("提示:未设 SYMSAN_FGTEST,symsan 引擎会找不到 fgtest。见 scripts/build_symsan.sh。")
    for engine, target_cmd in [("symcc", ["/tmp/parser_symcc", "@@"]),
                               ("symsan", ["/tmp/parser_symsan"])]:
        rnd, best, tot = concolic_loop(engine, target_cmd)
        ok = "✓ 解出完整魔数 SYM\\x01" if best == 4 else f"最深匹配 {best}/4 字节"
        print(f"  {engine:7}: {ok}  (第 {rnd} 轮达成, 累计 {tot} 个唯一输入)")


if __name__ == "__main__":
    main()
