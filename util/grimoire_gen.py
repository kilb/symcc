#!/usr/bin/env python3
"""GRIMOIRE 风格无语法结构合成生成器（CPU，纯 Python）。

思想（Blazytko et al., USENIX Sec'19）：不需要人写语法，从已有覆盖率增益的输入中
学习"结构片段"，再通过**替换/拼接**在结构边界上重组，生成语法有效的新输入，触达
AFL 随机变异到不了的深层解析逻辑。本实现为可跑的工程化版本：

  1. 片段提取：按结构边界（分隔符 + 学到的多字节 token）把语料切成 fragment 库；
     同时把每条输入表示为"槽序列"（fragment 之间可替换的位置）。
  2. 泛化（轻量）：分隔符之间的 fragment 视为可替换槽（对应 GRIMOIRE 的 gap）。
  3. 重组生成：
       - 替换：把输入 A 的某个 fragment 换成库中/另一输入的 fragment；
       - 拼接：在分隔符边界拼 A 的前缀 + B 的后缀；
       - token 插入：在槽处插入字典 token。
  4. 去重后写入输出目录，由 AFL 通过 -F 导入（融入集成框架）。

作为集成成员周期性运行；通过 SYMCC 已有的 hints/extras 复用约束派生 token。
"""
import argparse
import hashlib
import os
import random
import shutil
import struct
import subprocess
import sys
import time

# 结构分隔符：覆盖 SQL/XML/归档/通用文本的常见边界字符
DELIMS = b" \t\r\n,;()[]{}<>=\"'/\\|&:.-"


class ShowmapOracle:
    """持久 afl-showmap -S 覆盖率预言机（~0.6ms/次），用于覆盖率引导的泛化。"""

    # edge 数上限：防止读到损坏的 count 后进入超长循环（afl map 上限 ~2^16）
    _MAX_EDGES = 1 << 20

    def __init__(self, afl_showmap: str, target_cmd: list[str]) -> None:
        cmd = [afl_showmap, "-S", "-t", "2000", "-m", "none", "--"] + target_cmd
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        self._dead = False   # 一旦协议 EOF/进程死亡即置位，后续 cover() 直接返回 None

    def _read_exact(self, n: int) -> bytes | None:
        """精确读取 n 字节；EOF/进程死亡时返回 None（并标记 oracle 已死）。
        注意：afl-showmap 的崩溃/超时响应仍是**完整成帧**的（status 位标识），
        只有真正的短读/EOF 才意味着进程死亡——此时不再复用被截断的管道。"""
        buf = bytearray()
        rd = self._proc.stdout
        while len(buf) < n:
            chunk = rd.read(n - len(buf))
            if not chunk:                 # EOF → 进程已死，协议不可再同步
                self._dead = True
                return None
            buf += chunk
        return bytes(buf)

    def cover(self, content: bytes) -> frozenset | None:
        """返回该输入触达的 edge_id 集合；oracle 死亡或协议错误返回 None。
        崩溃/超时输入仍返回其（可能为空的）覆盖集，不会毒化后续查询。"""
        if self._dead:
            return None
        try:
            self._proc.stdin.write(struct.pack("<I", len(content)))
            self._proc.stdin.write(content)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._dead = True
            return None
        raw = self._read_exact(2)                       # u16 status
        if raw is None:
            return None
        raw = self._read_exact(4)                       # u32 edge count
        if raw is None:
            return None
        cnt = struct.unpack("<I", raw)[0]
        if cnt > self._MAX_EDGES:                        # 损坏的 count 防护
            self._dead = True
            return None
        edges = set()
        pair = self._read_exact(5 * cnt)                # (u32 eid, u8 cnt) × cnt
        if pair is None:
            return None
        for i in range(cnt):
            edges.add(struct.unpack_from("<I", pair, i * 5)[0])
        for _ in range(2):                               # 排空 stdout / stderr 块
            lraw = self._read_exact(4)
            if lraw is None:
                return None
            blen = struct.unpack("<I", lraw)[0]
            if blen and self._read_exact(blen) is None:
                return None
        return frozenset(edges)

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self._proc.kill()
            try:
                self._proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass


def split_fragments(data: bytes) -> list[bytes]:
    """按分隔符把输入切为 fragment（保留分隔符作为独立片段以维持结构）。"""
    frags = []
    cur = bytearray()
    for b in data:
        if b in DELIMS:
            if cur:
                frags.append(bytes(cur))
                cur = bytearray()
            frags.append(bytes([b]))
        else:
            cur.append(b)
    if cur:
        frags.append(bytes(cur))
    return frags


def load_tokens(extras_dir: str) -> list[bytes]:
    """加载 AFL extras / SymCC hint token（约束派生的多字节 token）。"""
    tokens = []
    if extras_dir and os.path.isdir(extras_dir):
        for name in os.listdir(extras_dir):
            p = os.path.join(extras_dir, name)
            try:
                with open(p, "rb") as f:
                    t = f.read(256)
                if t:
                    tokens.append(t)
            except OSError:
                pass
    return tokens


class Grimoire:
    _MAX_SEEN_OUT = 200000                     # seen_out 去重集上限（避免长驻内存膨胀）

    def __init__(self, out_dir: str, tokens: list[bytes],
                 oracle: "ShowmapOracle | None" = None,
                 max_len: int = 16384, max_frags_probe: int = 80) -> None:
        self.out_dir = out_dir
        self.tokens = tokens
        self.oracle = oracle                  # ShowmapOracle 或 None（无则退化为盲切分）
        self.max_len = max_len
        self.max_frags_probe = max_frags_probe
        self.frag_lib: list[bytes] = []       # 全局 fragment 库（含 gap 填充料）
        self.templates: list[list[tuple]] = []  # 泛化模板：[(frag, is_gap), ...]
        self.seen_out: set[str] = set()
        self.gen_id = 0
        self.gap_fills: list[bytes] = []      # 从 gap 处观测到的可替换内容
        os.makedirs(out_dir, exist_ok=True)

    def generalize(self, data: bytes) -> list[tuple] | None:
        """覆盖率引导的**多粒度** gap 检测（更贴近真 GRIMOIRE：找最大可移除子串）。

        分治：对 fragment 下标区间 [lo,hi) 尝试**整段移除**；若覆盖率 ⊇ 原覆盖率，则整段
        判为一个**极大 gap**（标记并停止细分）；否则二分递归到更小区间。这样一个可整体
        移除的"子句"只需 1 次探测即成一个大 gap，而非逐 fragment；大 gap 给重组更大自由度。
        返回模板 [(frag_bytes, is_gap), ...]；无 oracle/覆盖率时返回 None。"""
        if self.oracle is None:
            return None
        base = self.oracle.cover(data)
        if not base:
            return None
        frags = split_fragments(data)
        n = len(frags)
        if n == 0:
            return None
        gap = [False] * n
        calls = 0
        cap = self.max_frags_probe            # 每输入的 oracle 调用预算
        stack = [(0, n)]
        while stack and calls < cap:
            lo, hi = stack.pop()
            if lo >= hi:
                continue
            reduced = b"".join(frags[:lo]) + b"".join(frags[hi:])
            # 不允许把整个输入判为 gap：空 reduced 常走目标的错误/早退路径而覆盖
            # 一个"超集"，会误判整段可移除、摧毁结构。空 reduced 直接细分。
            if not reduced:
                if hi - lo > 1:
                    mid = (lo + hi) // 2
                    stack.append((mid, hi))
                    stack.append((lo, mid))
                continue
            cov = self.oracle.cover(reduced)
            calls += 1
            if cov is not None and base.issubset(cov):
                # 整段可移除 → 极大 gap，不再细分（内容由下方合并 pass 收集为填充料）
                for i in range(lo, hi):
                    gap[i] = True
            elif hi - lo > 1:
                mid = (lo + hi) // 2
                # 先探大区间：把较大的两半后压栈（LIFO 先弹右半，顺序不影响正确性）
                stack.append((mid, hi))
                stack.append((lo, mid))
            # size==1 且不可移除 → 结构必需字面量（gap[lo] 保持 False）
        # 合并相邻 gap fragment 为整体填充料（提供更大粒度的可替换内容）
        i = 0
        while i < n:
            if gap[i]:
                j = i
                while j < n and gap[j]:
                    j += 1
                seg = b"".join(frags[i:j])
                if len(seg) > 1:
                    self.gap_fills.append(seg)
                i = j
            else:
                i += 1
        return [(frags[i], gap[i]) for i in range(n)]

    def ingest(self, data: bytes) -> list[bytes]:
        """切片并入库；有 oracle 时同时做覆盖率引导泛化，产出模板。"""
        frags = split_fragments(data)
        # split_fragments 保证分隔符恒为单字节 fragment，故 len>1 即为非分隔符内容 run
        for fr in frags:
            if len(fr) > 1:
                self.frag_lib.append(fr)
        if len(self.frag_lib) > 20000:
            self.frag_lib = self.frag_lib[-20000:]
        tmpl = self.generalize(data)
        if tmpl and any(g for _, g in tmpl):   # 有 gap 才是有用模板
            self.templates.append(tmpl)
            if len(self.templates) > 4000:
                self.templates = self.templates[-4000:]
        if len(self.gap_fills) > 20000:
            self.gap_fills = self.gap_fills[-20000:]
        return frags

    def _emit(self, data: bytes) -> bool:
        if not data or len(data) > self.max_len:
            return False
        h = hashlib.sha256(data).hexdigest()
        if h in self.seen_out:
            return False
        if len(self.seen_out) >= self._MAX_SEEN_OUT:
            self.seen_out.clear()   # 有界内存：超限则清空（偶尔重发可接受）
        self.seen_out.add(h)
        # 说明：GRIMOIRE 输出本身即"结构有效"的重组（按构造高价值），直接产出全部去重结果。
        # 不在此做逐候选覆盖率过滤——(a) 单持久 oracle 对狂野生成输入易 desync；(b) 种子片段
        # 重组极少逐条新增边（虽整体并集增覆盖）。高/低价值的下游筛选交给 SymCC triage 与
        # AFL -F 导入（各自按真实覆盖率去重）。generalize() 仍用 oracle 做 gap 检测。
        try:
            with open(os.path.join(self.out_dir,
                                   f"grimoire_{self.gen_id:08d}"), "wb") as f:
                f.write(data)
            self.gen_id += 1
            return True
        except OSError:
            return False

    def _fill_template(self, tmpl: list[tuple], pool: list[bytes]) -> bytes:
        """按模板重组：结构必需 fragment 保留；**连续 gap 合并为一个整体 gap 单元**
        （利用多粒度大 gap），整体随机 {留空 / 换 pool 填充料 / 保留原内容}。"""
        out = bytearray()
        k = 0
        m = len(tmpl)
        while k < m:
            fr, is_gap = tmpl[k]
            if not is_gap:
                out += fr
                k += 1
                continue
            # 合并整段连续 gap
            span = bytearray(fr)
            k += 1
            while k < m and tmpl[k][1]:
                span += tmpl[k][0]
                k += 1
            r = random.random()
            if r < 0.30:
                pass                          # 整段留空
            elif r < 0.75 and pool:
                out += random.choice(pool)    # 整段换填充料/token
            else:
                out += bytes(span)            # 保留原内容
        return bytes(out)

    def generate(self, seqs: list[list[bytes]], budget: int) -> int:
        """优先用覆盖率引导的泛化模板（填 gap）；无模板时退化为盲重组。"""
        n = 0
        pool = self.gap_fills + self.frag_lib + self.tokens
        for _ in range(budget * 5):
            if n >= budget:
                break
            use_tmpl = self.templates and random.random() < 0.75
            if use_tmpl:
                # GRIMOIRE 核心：在泛化模板的 gap 上重组（结构保真）
                t = random.choice(self.templates)
                if random.random() < 0.25 and len(self.templates) > 1:
                    # 模板拼接：A 的前半 + B 的后半（在 gap/边界处）
                    t2 = random.choice(self.templates)
                    cut1 = random.randint(1, len(t))
                    cut2 = random.randint(0, len(t2))
                    cand = (self._fill_template(t[:cut1], pool)
                            + self._fill_template(t2[cut2:], pool))
                else:
                    cand = self._fill_template(t, pool)
            else:
                # 盲重组回退（无模板或概率）
                if not seqs or not pool:
                    continue
                a = random.choice(seqs)
                if not a:
                    continue
                op = random.random()
                if op < 0.5:
                    idx = [i for i, fr in enumerate(a) if len(fr) > 1]
                    if not idx:
                        continue
                    j = random.choice(idx)
                    cand = b"".join(a[:j] + [random.choice(pool)] + a[j + 1:])
                else:
                    b = random.choice(seqs)
                    if not b:
                        continue
                    cand = (b"".join(a[:random.randint(1, len(a))])
                            + b"".join(b[random.randint(0, len(b)):]))
            if self._emit(cand):
                n += 1
        return n


def main() -> int:
    ap = argparse.ArgumentParser(description="GRIMOIRE-style structural synthesizer")
    ap.add_argument("--corpus", required=True, help="源语料目录（如 AFL queue）")
    ap.add_argument("--out", required=True, help="输出目录（由 AFL -F 导入）")
    ap.add_argument("--extras", default="", help="AFL extras / hint token 目录")
    ap.add_argument("--interval", type=float, default=10.0, help="扫描间隔秒")
    ap.add_argument("--batch", type=int, default=500, help="每轮生成数")
    ap.add_argument("--once", action="store_true", help="只跑一轮（用于测试）")
    ap.add_argument("--afl-binary", default="",
                    help="afl-showmap 可测的目标二进制；提供则启用覆盖率引导泛化")
    ap.add_argument("--uses-stdin", action="store_true",
                    help="目标从 stdin 读输入（否则用 @@ 文件参数）")
    ap.add_argument("--seed", type=int, default=None,
                    help="随机种子，用于基准可复现（默认不固定）")
    args = ap.parse_args()

    # 可复现：本脚本作为独立进程运行，固定全局 random 即可，无需私有 RNG。
    if args.seed is not None:
        random.seed(args.seed)

    oracle = None
    if args.afl_binary:
        showmap = shutil.which("afl-showmap")
        if showmap and os.path.exists(args.afl_binary):
            tcmd = [args.afl_binary] if args.uses_stdin else [args.afl_binary, "@@"]
            try:
                oracle = ShowmapOracle(showmap, tcmd)
                print(f"[grimoire] coverage-guided generalization ON "
                      f"(oracle: {os.path.basename(args.afl_binary)})", flush=True)
            except (OSError, ValueError) as e:
                # afl-showmap 启动失败（缺文件/权限/参数错误）→ 退化为盲重组
                print(f"[grimoire] oracle init failed ({e}); blind mode", flush=True)
    if oracle is None:
        print("[grimoire] blind recombination mode (no --afl-binary)", flush=True)

    g = Grimoire(args.out, load_tokens(args.extras), oracle=oracle)
    seen_inputs: set[str] = set()
    total = 0
    try:
        while True:
            seqs = []
            try:
                names = os.listdir(args.corpus)
            except OSError:
                names = []
            for name in names:
                p = os.path.join(args.corpus, name)
                if p in seen_inputs or not os.path.isfile(p):
                    continue
                seen_inputs.add(p)
                try:
                    with open(p, "rb") as f:
                        data = f.read(16384)
                except OSError:
                    continue
                if data:
                    seqs.append(g.ingest(data))
            # 只要有新种子、或已积累了覆盖率引导模板，就持续合成：语料停止增长时
            # generate() 的模板路径（占 75%，仅用 templates+pool，不依赖 seqs）仍可
            # 持续向集成贡献结构重组；否则语料一停生成器就永久静默，违背其用途。
            if seqs or g.templates:
                made = g.generate(seqs, args.batch)
                total += made
                print(f"[grimoire] +{made} synthesized (total {total}, "
                      f"frag_lib {len(g.frag_lib)})", flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        if oracle is not None:
            oracle.close()          # 关闭持久 afl-showmap forkserver，避免泄漏
    return 0


if __name__ == "__main__":
    sys.exit(main())
