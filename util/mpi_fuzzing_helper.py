#!/usr/bin/env python3
"""
MPI-parallel fuzzing helper for SymCC + AFL integration.

This is the MPI-parallel equivalent of symcc_fuzzing_helper. It monitors
an AFL fuzzer's queue and distributes SymCC executions across MPI workers.
New test cases that produce novel coverage are fed back to AFL.

Architecture:
    Rank 0 (Master): Monitors AFL queue, distributes inputs, triages results
    Ranks 1..N-1 (Workers): Run SymCC on assigned inputs

Usage:
    mpirun -np <N> python3 mpi_fuzzing_helper.py \
        -a <fuzzer_name> -o <afl_output_dir> -n <symcc_name> -- TARGET [ARGS...]

Requirements:
    - mpi4py  (pip install mpi4py)
    - An MPI implementation (OpenMPI, MPICH, etc.)
    - AFL (afl-showmap must be available)
    - SymCC-instrumented target binary

Example:
    # Start AFL first:
    afl-fuzz -M fuzzer01 -i seeds -o /tmp/afl_out -- ./target_afl @@

    # Then start SymCC MPI helper:
    mpirun -np 8 python3 mpi_fuzzing_helper.py \
        -a fuzzer01 -o /tmp/afl_out -n symcc -- ./target_symcc @@
"""

import argparse
import hashlib
import heapq
import math
import os
import random
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import typing

from mpi4py import MPI

# MPI tags
TAG_WORK = 1
TAG_RESULT = 2
TAG_STOP = 3
TAG_READY = 4
# 注：bitmap 版本经 TAG_WORK 消息载荷（"bitmap_version"）随派发传播，无需独立 tag。

TIMEOUT_SEC = int(os.environ.get("SYMCC_TIMEOUT", "30"))  # SymCC 执行超时（秒），默认 30s
SHOWMAP_TIMEOUT_MS = "5000"
_WORKER_SEEN_CAP = 300_000   # 跨 item 内容去重集上限(约 300k×~50B≈15MB);超限清空,只损失去重机会
STATS_INTERVAL_SEC = 60
MAX_GENERATION_DEPTH = int(os.environ.get("SYMCC_MAX_DEPTH", "0"))  # 最大迭代深度，0=无限
# AFL extras hint token 文件数上限：循环复用固定文件池，避免长时间运行产生数百万小文件
# 耗尽 inode。AFL 字典体量本就有限，几千个 token 已充分。
MAX_HINT_FILES = 4096
# AflConfig._file_cache 条目上限：AFL queue 极长时防止无界内存增长。
MAX_FILE_CACHE = 200000
# 去重/跟踪容器（processed_files / _content_hashes / file_generation / grimoire_seen）
# 硬上限：超长 campaign 下这些集合随派发数无界增长。超限清空（代价是少量重复分析，
# 有界且不影响正确性）。基准运行通常远达不到，仅为病态长运行兜底。
MAX_DEDUP_ENTRIES = 5000000

# AFL 覆盖率 bitmap 的初始大小（afl-showmap 自动按目标实际边数调整 map size，实测这些
# 目标为几千到几万；此为惰性分配的初值，_merge_sparse 遇到更大的 edge_id 会自动增长）。
_AFL_MAP_SIZE = 65536

# showmap 稀疏边记录：u32 edge_id + u8 hit-count（'<' 无填充 → 每条恰 5 字节）。
# 用 Struct.iter_unpack 一次性 C 层批量解码整段，替代逐边 unpack_from 的 Python 热循环
# （实测 3.5-3.9x，此为 get_edges 每个 concolic 产出的用例都跑的最热函数）。
_EDGE_STRUCT = struct.Struct("<IB")

# 细粒度并行分解（opt-in，SYMCC_WORKER_DIVERSITY=1）：给每个 worker 一个不同的 concolic
# 策略画像 + 不相交的符号化字节区间，使相似种子在不同 worker 上产出发散（非重叠）的输入。
# 目的：突破并行 concolic 的"下游冗余"瓶颈（相同翻转→相同下游代码），让 worker 数可扩展到
# 远超 ~12 的经验饱和点——每个 worker 分到 P(区间)×S(策略) 网格中的一格不重复的工作。
# 策略轴（受 AFL ensemble 配置多样性启发，实测对 AFL 有效，此为其 concolic 侧类比）：
SYMCC_STRATEGY_PROFILES: list[dict[str, str]] = [
    {},                            # 严格单分支（基线）
    {"SYMCC_MULTI_SOLVE": "1"},    # 连续字节多分支联合求解
    {"SYMCC_MULTI_SOLVE": "2"},    # + switch-case / 结构体字段（更深）
    {"SYMCC_FAST_SOLVE": "1"},     # Fuzzy-Sat 快速求解（不同分支选择 / 更快周转）
]


def _pin_self_to_reserved_core(rank: int) -> None:
    """按 SYMCC_CPU_LIST 把本 rank 钉到保留逻辑核（其派生的 SymCC 子进程会继承亲和性）。

    编排层（run_benchmark）在高并行度下计算与 AFL 自动绑核互斥的保留核段并经此环境变量
    传入，消除 MPI rank 在 AFL 已绑核上漂移造成的核冲突/迁移。未设置则不钉核（保持默认
    调度）。OpenMPI 启动时的绑核会被此处的 sched_setaffinity 覆盖（已验证）。"""
    spec = os.environ.get("SYMCC_CPU_LIST")
    if not spec or not hasattr(os, "sched_setaffinity"):
        return
    try:
        cores = [int(x) for x in spec.split(",") if x.strip()]
    except ValueError:
        return
    if not cores:
        return
    try:
        os.sched_setaffinity(0, {cores[rank % len(cores)]})
    except OSError:
        pass  # 核号无效/平台不支持 → 退回默认调度，不影响正确性


def _balanced_regions(density: "list[int]", per: int) -> "list[tuple[int, int]]":
    """把 [0,len) 划分为 per 个连续字节区间，使各区间累计分支密度尽量相等（热点字节→窄
    区间隔离，冷区→宽区间）。闭区间 [(lo,hi),...]。密度全 0 时退回等宽。"""
    L = len(density)
    if per <= 1 or L == 0:
        return [(0, max(0, L - 1))]
    total = sum(density)
    if total <= 0:  # 无密度信息 → 等宽
        return [((i * L) // per, ((i + 1) * L) // per - 1) for i in range(per)]
    target = total / per
    regions: "list[tuple[int, int]]" = []
    lo = 0
    acc = 0
    for i in range(L):
        acc += density[i]
        remaining_cuts = per - 1 - len(regions)
        # 累计越过下一目标线、还需切点、且剩余字节够分给剩余区间时切一刀
        if (remaining_cuts > 0 and acc >= target * (len(regions) + 1)
                and (L - 1 - i) >= remaining_cuts):
            regions.append((lo, i))
            lo = i + 1
    regions.append((lo, L - 1))
    # 密度集中在尾部时贪心可能切不满 per 个区间（早期未越过目标线、末尾已无空间下刀）。
    # 补切最宽区间直到凑够 per 个，保证工作项数可预期、不让分到该种子的 worker 空闲。
    while len(regions) < per:
        widest = max(range(len(regions)),
                     key=lambda k: regions[k][1] - regions[k][0])
        wlo, whi = regions[widest]
        if whi <= wlo:
            break  # 全为单字节区间，无法再细分
        mid = (wlo + whi) // 2
        regions[widest:widest + 1] = [(wlo, mid), (mid + 1, whi)]
    return regions


def _build_work_items(paths: "list[str]", target_count: int, diversity: bool,
                      focus_parts: int,
                      density_fn: "typing.Callable[[str], list[int] | None] | None" = None
                      ) -> "list[tuple[str, str | None]]":
    """把种子路径构建为工作项 [(path, focus_or_None)]，focus 为 "lo-hi" 或 None（整段符号化）。

    动态工作窃取（自适应粒度）：默认每种子 1 个整-种子项（无冗余 CPU）。多样性模式下，
    当整-种子项数 < target_count（=活跃 worker 数）即出现空闲产能时，把种子按不相交字节
    区间细分为子项来填满空闲 worker——种子充足时不细分，避免种子够用时 P 倍重复执行的浪费。
    实测（pcre2）：等宽字节分区负载不均（符号化工作集中在少数驱动分支的字节），故仅用它
    填补"本会空闲"的产能，而非无条件细分。target 个工作项按种子近似均分（前 rem 个种子多
    分一段），仅按需细分，每种子上限 focus_parts 段。"""
    base: "list[tuple[str, str | None]]" = [(p, None) for p in paths]
    if not diversity or not base or len(base) >= target_count:
        return base
    n = len(base)
    # 只细分到"恰好填满空闲产能"的程度：把 target_count 个工作项尽量均匀分到 n 个种子——
    # 前 rem 个种子多分一段（k=base_k+1），其余分 base_k 段。避免此前 per=ceil(target/n)
    # 一刀切导致"种子略少于 target 时全部 2 倍细分"的浪费（seeds=23、target=24 → 46 项）。
    base_k = target_count // n
    rem = target_count % n
    items: "list[tuple[str, str | None]]" = []
    for idx, path in enumerate(paths):
        k = min(focus_parts, base_k + (1 if idx < rem else 0))
        if k <= 1:                         # 该种子无需细分（整段一项即可填满其份额）
            items.append((path, None))
            continue
        try:
            flen = os.path.getsize(path)
        except OSError:
            flen = 0
        if flen <= k:                      # 太短，不细分（避免空/退化区间）
            items.append((path, None))
            continue
        # 优先按分支密度均衡划分（热点字节隔离到窄区间），无密度信息则退回等宽。
        density = density_fn(path) if density_fn is not None else None
        if density and len(density) == flen and sum(density) > 0:
            for lo, hi in _balanced_regions(density, k):
                items.append((path, f"{lo}-{hi}"))
        else:
            for i in range(k):
                lo = (i * flen) // k
                hi = ((i + 1) * flen) // k - 1   # 闭区间，减 1 使相邻不重叠
                items.append((path, f"{lo}-{max(lo, hi)}"))
    return items


class AflConfig:
    """AFL fuzzer configuration, read from fuzzer_stats."""

    def __init__(self, fuzzer_output_dir: str) -> None:
        self.queue = os.path.join(fuzzer_output_dir, "queue")
        # 每文件静态属性缓存：AFL queue 文件不可变，故 name 派生标志/大小/afl_id/
        # SHA-256 只需计算一次。消除 best_new_testcases 每轮重复 scandir+读文件+哈希
        # 的开销（实测该扫描是 master 的主要瓶颈，34s/58s @ 15 workers）。
        self._file_cache: dict[str, dict] = {}
        stats_path = os.path.join(fuzzer_output_dir, "fuzzer_stats")

        with open(stats_path) as f:
            stats = f.read()

        # Parse the command line from fuzzer_stats
        for line in stats.splitlines():
            if line.startswith("command_line"):
                cmd_str = line.split(":", 1)[1].strip()
                parts = cmd_str.split()
                break
        else:
            raise RuntimeError("Could not find command_line in fuzzer_stats")

        # 查找 afl-showmap：先从 afl-fuzz 同目录找，再从 PATH 找
        afl_binary = parts[0]
        afl_dir = os.path.dirname(afl_binary)
        if afl_dir:
            candidate = os.path.join(afl_dir, "afl-showmap")
            if os.path.isfile(candidate):
                self.show_map = candidate
            else:
                self.show_map = shutil.which("afl-showmap") or "afl-showmap"
        else:
            # afl-fuzz 是通过 PATH 调用的，afl-showmap 也应该在 PATH 中
            self.show_map = shutil.which("afl-showmap") or "afl-showmap"

        # Extract target command (after --)
        try:
            dash_idx = parts.index("--")
            self.target_command = parts[dash_idx + 1:]  # skip '--'
        except ValueError:
            self.target_command = parts[-1:]

        self.use_stdin = "@@" not in self.target_command
        self.use_qemu = "-Q" in parts

    def best_new_testcases(self, seen: set[str], batch_size: int | None = None,
                           analyzed_hashes: set[str] | None = None,
                           edge_yield: dict[str, float] | None = None,
                           frontier_fn: "typing.Callable[[str], float] | None" = None
                           ) -> list[str]:
        """
        Return a list of unseen test cases from the AFL queue, scored by priority.

        增强种子调度策略（受 CoFuzz ICSE'23 启发）：
        1. 边产出率加权：历史上 concolic 分析后产出新覆盖的种子类型优先
        2. +cov 标记：AFL 认为发现新覆盖 → 高优先
        3. 稀有边覆盖：触达稀有边的种子优先（AFL 文件名中的 +rare）
        4. 文件大小效率：根据 size/yield 比率动态调整
        5. 新颖度衰减：越新的种子优先，但对极新种子不再过度加分
        """
        if not os.path.isdir(self.queue):
            return []

        cache = self._file_cache
        # 硬上限：dict 保持插入序，超限时按 FIFO 逐出最旧条目——它们多为最早发现、
        # 早已派发（在 seen 中）的 queue 文件。偶尔逐出未处理条目仅导致其下轮被重新
        # stat/哈希，代价有界（O(超出量)/次，非每次 O(cache) 重建列表）且不影响正确性。
        while len(cache) > MAX_FILE_CACHE:
            cache.pop(next(iter(cache)))
        new_candidates = []
        try:
            for entry in os.scandir(self.queue):
                fpath = entry.path
                if fpath in seen:
                    continue

                # 每文件静态属性只计算一次（AFL queue 文件不可变）
                attrs = cache.get(fpath)
                if attrs is None:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    name = entry.name
                    try:
                        fsize = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        fsize = 0
                    afl_id = 0
                    if name.startswith("id:"):
                        try:
                            afl_id = int(name[3:9])
                        except (ValueError, IndexError):
                            pass
                    # 大小/新颖度得分（与 edge_yield 无关，可预计算并缓存）
                    static_score = 0.0
                    if "+cov" in name:
                        static_score += 100.0
                    if "+rare" in name:
                        static_score += 60.0
                    if "symcc_" in name:
                        static_score += 20.0
                    if fsize > 50 * 1024:
                        static_score -= 40.0
                    elif fsize > 10240:
                        static_score -= min(30.0, (fsize - 10240) / 1024.0)
                    elif fsize < 256:
                        static_score += 10.0
                    static_score += min(50.0, math.log1p(afl_id) * 5.0)
                    seed_type = "cov" if "+cov" in name else (
                        "symcc" if "symcc_" in name else "normal")
                    # 内容哈希只算一次
                    chash = None
                    try:
                        with open(fpath, "rb") as f:
                            chash = hashlib.sha256(f.read()).hexdigest()
                    except (IOError, OSError):
                        pass
                    attrs = {"name": name, "static": static_score,
                             "type": seed_type, "hash": chash}
                    cache[fpath] = attrs

                # 内容去重（用缓存哈希，不再重复读文件）
                if analyzed_hashes is not None and attrs["hash"] in analyzed_hashes:
                    continue

                # 动态部分：edge_yield 每轮变化 → 廉价的算术叠加
                score = attrs["static"]
                if edge_yield is not None:
                    score += edge_yield.get(attrs["type"], 0.0) * 30.0
                # K-Scheduler 风格前沿加权（opt-in，SYMCC_KSCHED=1）：优先覆盖当前稀有边
                # （≈ 覆盖前沿/CFG 中心性）的种子，把 concolic 预算投向最可能触达未探索区域处。
                if frontier_fn is not None:
                    score += frontier_fn(fpath) * 40.0

                new_candidates.append((score, attrs["name"], fpath))
        except OSError:
            return []

        # 只需取分数最高的 batch_size 个：heapq.nlargest 是 O(U) 选择，优于 O(U log U)
        # 全排序（U=本轮未派发候选数；队列大/爆发后 U 可能很大，而消费的 batch_size 恒小）。
        if batch_size is not None:
            top = heapq.nlargest(batch_size, new_candidates, key=lambda c: c[0])
        else:
            top = sorted(new_candidates, key=lambda c: -c[0])
        return [c[2] for c in top]

    def run_showmap(self, testcase: str,
                    bitmap_path: str) -> tuple[str, bytes | None]:
        """
        Run afl-showmap on a test case.

        Returns:
            ("success", bitmap_data) | ("hang", None) | ("crash", None)
        """
        cmd = [self.show_map]
        if self.use_qemu:
            cmd.append("-Q")
        cmd.extend(["-t", SHOWMAP_TIMEOUT_MS, "-m", "none", "-b", "-o", bitmap_path])

        # Build target command with @@ replaced
        for arg in self.target_command:
            if arg == "@@":
                cmd.append(str(testcase))
            else:
                cmd.append(arg)

        try:
            run_timeout = 10  # subprocess 级别超时，防止 afl-showmap 挂起
            if self.use_stdin:
                with open(testcase, "rb") as inf:
                    proc = subprocess.run(
                        cmd, stdin=inf, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=run_timeout
                    )
            else:
                proc = subprocess.run(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=run_timeout
                )

            if proc.returncode == 0:
                with open(bitmap_path, "rb") as f:
                    bitmap_data = f.read()
                return "success", bitmap_data
            elif proc.returncode == 1:
                return "hang", None
            elif proc.returncode == 2:
                return "crash", None
            else:
                return "error", None
        except subprocess.TimeoutExpired:
            return "hang", None
        except FileNotFoundError:
            print(f"[Master] afl-showmap not found at: {self.show_map}",
                  file=sys.stderr)
            return "error", None
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[Master] afl-showmap error: {e}", file=sys.stderr)
            return "error", None


class CoverageBitmap:
    """使用边集合追踪覆盖率，merge 操作 O(新边数) 而非 O(bitmap大小)。"""

    def __init__(self) -> None:
        self.data: bytearray | None = None
        self.edges: set[int] = set()

    def init_from_afl(self, afl_config: AflConfig, queue_dir: str,
                      max_entries: int = 50, time_budget: float = 5.0) -> None:
        """从 AFL queue 中已有的测试用例初始化 bitmap。

        这样 SymCC 只会报告 AFL 尚未发现的新覆盖，避免重复计算。
        限制处理数量和时间，避免在 AFL 快速生成大量用例时阻塞太久。
        """
        if not os.path.isdir(queue_dir):
            return
        bitmap_path = os.path.join(
            os.path.dirname(queue_dir), ".init_bitmap"
        )
        # 取最新的 max_entries 个文件（按名称排序，AFL 的 ID 递增）
        files = sorted(os.listdir(queue_dir))
        if len(files) > max_entries:
            files = files[-max_entries:]  # 取最新的
        count = 0
        start_time = time.monotonic()
        for fname in files:
            if time.monotonic() - start_time > time_budget:
                print(f"[Master] Bitmap init time budget ({time_budget}s) exceeded, "
                      f"processed {count}/{len(files)}")
                break
            fpath = os.path.join(queue_dir, fname)
            if not os.path.isfile(fpath):
                continue
            result_type, bitmap_data = afl_config.run_showmap(
                fpath, bitmap_path
            )
            if result_type == "success" and bitmap_data:
                self.merge(bitmap_data)
                count += 1
        try:
            os.unlink(bitmap_path)
        except OSError:
            pass
        elapsed = time.monotonic() - start_time
        print(f"[Master] Initialized bitmap from {count} AFL queue entries "
              f"({elapsed:.1f}s)")

    def merge(self, new_data: "bytes | list[tuple[int, int]]") -> bool:
        """Merge new bitmap data. Returns True if new coverage found.

        接受 bytes (完整 bitmap) 或 list[(index, value)] (稀疏边列表)。
        稀疏格式只需检查非零边，O(~500) 而非 O(8M)。
        """
        # 支持稀疏格式: [(edge_id, hit_count), ...]
        if isinstance(new_data, list):
            return self._merge_sparse(new_data)

        if self.data is None:
            self.data = bytearray(new_data)
            for i, b in enumerate(new_data):
                if b:
                    self.edges.add(i)
            return True

        if len(self.data) != len(new_data):
            return False

        # 完整 bitmap：用大整数快速检查
        old_int = int.from_bytes(self.data, 'little')
        new_int = int.from_bytes(new_data, 'little')
        diff = new_int & ~old_int
        interesting = bool(diff)
        if interesting:
            merged = old_int | new_int
            self.data[:] = merged.to_bytes(len(self.data), 'little')
            # 仅将新增边加入集合：从位差 diff 中提取置位所在字节索引，
            # O(新增位数) 而非每次 O(map_size) 全字节扫描。已在集合中的字节
            # （旧数据非零处）无需重加，集合去重保证正确。
            while diff:
                lsb = diff & -diff
                self.edges.add((lsb.bit_length() - 1) // 8)
                diff &= diff - 1
        return interesting

    def _merge_sparse(self, edges: list) -> bool:
        """合并稀疏边列表 [(edge_id, hit_count), ...]。极快。

        始终维护稠密 data（惰性分配 + 按需增长），data[edge_id] 累积各边的命中桶。
        这样 (1) master 能把全局 bitmap 写入共享文件供 worker 播种本地 dedup；(2) 已知
        边上的新命中桶（AFL 视为新覆盖）也能被判为 interesting。此前 data 为 None 时只按
        边"存在"去重，既漏掉新桶覆盖，又使共享 bitmap 永不写出（worker 无法播种全局边）。
        """
        if self.data is None:
            self.data = bytearray(_AFL_MAP_SIZE)
        interesting = False
        for edge_id, hit in edges:
            if edge_id >= len(self.data):     # 目标 map 大于初值 → 增长以容纳
                self.data.extend(b"\x00" * (edge_id + 1 - len(self.data)))
            old = self.data[edge_id]
            # 用 data 判"是否见过"（byte==0 即未覆盖）而非 edges 集：worker 从共享 bitmap
            # 播种时只需批量拷贝 data，免去每次版本更新 O(map) 的 edges 集重建（showmap 桶
            # 恒 >=1，故 data==0 严格等价于未覆盖）。edges 仍维护，供 master 报告边数。
            if old == 0:
                interesting = True
                self.edges.add(edge_id)
                self.data[edge_id] = hit
            elif old | hit != old:
                interesting = True
                self.data[edge_id] = old | hit
        return interesting


class Stats:
    """Execution statistics."""

    def __init__(self) -> None:
        self.total_count = 0
        self.total_time = 0.0
        self.failed_count = 0
        self.failed_time = 0.0
        self.generated_count = 0
        self.interesting_count = 0

    def add_execution(self, elapsed: float, killed: bool) -> None:
        if killed:
            self.failed_count += 1
            self.failed_time += elapsed
        else:
            self.total_count += 1
            self.total_time += elapsed

    def log(self, f: "typing.TextIO") -> None:
        f.write(f"Successful executions: {self.total_count}\n")
        f.write(f"Time in successful executions: {self.total_time*1000:.0f}ms\n")
        if self.total_count > 0:
            avg = self.total_time / self.total_count * 1000
            f.write(f"Avg time per successful execution: {avg:.0f}ms\n")
        f.write(f"Failed executions: {self.failed_count}\n")
        f.write(f"Time in failed executions: {self.failed_time*1000:.0f}ms\n")
        if self.failed_count > 0:
            avg = self.failed_time / self.failed_count * 1000
            f.write(f"Avg time per failed execution: {avg:.0f}ms\n")
        f.write(f"Total test cases generated: {self.generated_count}\n")
        f.write(f"Interesting test cases: {self.interesting_count}\n")
        f.write("-" * 80 + "\n")
        f.flush()


def run_symcc_worker(target_cmd: list[str], input_file: str, output_dir: str,
                     timeout_sec: int, use_stdin: bool,
                     base_env: "dict[str, str] | None" = None,
                     streaming_showmap: "StreamingShowmap | None" = None,
                     worker_coverage: "CoverageBitmap | None" = None,
                     inflight: "dict | None" = None,
                     redun: "dict | None" = None,
                     worker_seen: "set[bytes] | None" = None
                     ) -> "tuple[list[dict], int, int, float, bool, float]":
    """在单个输入上运行 SymCC。

    返回 ``(new_tests, total_generated, retcode, elapsed, killed)``：
      - new_tests: list[dict]，每项含 "content"（bytes），可选 "bitmap"（稀疏边列表）
        和 "hints"（约束提示）。
      - total_generated: int，SymCC 本次生成的测试用例总数（含被 dedup 过滤的）。
      - retcode: int，SymCC 进程返回码（超时/被杀为负）。
      - elapsed: float，执行耗时（秒）。
      - killed: bool，是否因超时被杀。

    若提供 streaming_showmap 与 worker_coverage，会在 worker 端为每个输出运行
    afl-showmap（流式 fork server）收集稀疏边，并用 worker_coverage 本地 dedup，
    仅回传发现新覆盖的用例，master 只需内存中比较稀疏边列表。
    """
    os.makedirs(output_dir, exist_ok=True)

    if base_env is not None:
        env = dict(base_env)  # 浅拷贝，避免修改调用方字典
    else:
        env = os.environ.copy()
    env["SYMCC_OUTPUT_DIR"] = output_dir
    env["SYMCC_ENABLE_LINEARIZATION"] = "1"
    # 输出约束 hint 文件；默认开启，但允许上游 env 显式关闭（用于消融实验 ③ hint 传递）
    env["SYMCC_EMIT_HINTS"] = os.environ.get("SYMCC_EMIT_HINTS", "1")

    # #10 拆分：在 SymCC 运行【之前】快照【全局】已覆盖位图（SYMCC_AFL_COVERAGE_MAP,由 master
    # 的 .shared_bitmap 经 bmsync 播种而来）。据此把冗余输出分为"没打到任何全局新边(乐观求解
    # 不可行/冗余)"与"打到全局新边但已被覆盖(新鲜度间隙)"。首个 item 全局图尚未就绪时 _snap 为空。
    _snap = None
    if redun is not None:
        _snap_path = env.get("SYMCC_AFL_COVERAGE_MAP")
        if _snap_path and os.path.isfile(_snap_path):
            try:
                with open(_snap_path, "rb") as _sf:
                    _snap = _sf.read()
            except (IOError, OSError):
                _snap = None
        redun["items"] = redun.get("items", 0) + 1
        if _snap is None:
            redun["snap_none"] = redun.get("snap_none", 0) + 1

    if use_stdin:
        cmd = ["timeout", "-k", "5", str(timeout_sec)] + target_cmd
    else:
        env["SYMCC_INPUT_FILE"] = str(input_file)
        cmd = ["timeout", "-k", "5", str(timeout_sec)] + [
            arg.replace("@@", str(input_file)) for arg in target_cmd
        ]

    start = time.monotonic()
    python_timeout = timeout_sec + 15
    try:
        if use_stdin:
            with open(input_file, "rb") as inf:
                proc = subprocess.run(
                    cmd, stdin=inf, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=env,
                    timeout=python_timeout
                )
        else:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env,
                timeout=python_timeout
            )
        retcode = proc.returncode
    except subprocess.TimeoutExpired:
        print(f"[Worker {MPI.COMM_WORLD.Get_rank()}] Python-level timeout "
              f"({python_timeout}s)", file=sys.stderr, flush=True)
        retcode = 124
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[Worker {MPI.COMM_WORLD.Get_rank()}] Error: {e}", file=sys.stderr)
        retcode = -1

    elapsed = time.monotonic() - start
    killed = retcode in (124, -9, 137)  # timeout codes

    # 相位 F 计时起点：输出收集 + afl-showmap 取边 + worker 端 coverage dedup。
    # 这段【不】计入上面的 elapsed（相位 E=concolic 执行），用于区分"执行慢"还是
    # "showmap/dedup 后处理慢"——瓶颈分析的关键。
    _post_start = time.monotonic()
    # 通知调用方相位已从 exec 转入 showmap_dedup：若此后被 SIGTERM 打断，在途时长归到正确相位，
    # 并记下已完成的 exec 时长（elapsed），供 flush 在 item 未跑完时补计到 exec（否则会丢失）。
    if inflight is not None:
        inflight["phase"] = "showmap_dedup"
        inflight["start"] = _post_start
        inflight["exec_done"] = elapsed

    # Collect test cases + worker 端 coverage dedup
    # Worker 有 master bitmap 副本，在本地做 coverage merge
    # 只传 interesting 的 TC（~3% 的输出），消息 323KB → 10KB
    new_tests = []
    total_generated = 0
    # (_snap 已在 SymCC 运行前从全局位图快照，见函数上方)
    if os.path.isdir(output_dir):
        try:
            entries = list(os.scandir(output_dir))
        except OSError:
            entries = []

        # 先收集 hint 文件
        hint_map: dict[str, list[tuple[int, int, int]]] = {}  # base_name -> [(offset, old, new)]
        for entry in entries:
            if entry.name.endswith(".hints") and entry.is_file():
                base = entry.name[:-6]  # 去掉 .hints 后缀
                try:
                    hints = []
                    with open(entry.path, "r") as hf:
                        for line in hf:
                            line = line.strip()
                            if not line:
                                continue
                            parts = line.split(":")
                            if len(parts) == 3:
                                hints.append((
                                    int(parts[0]),
                                    int(parts[1], 16),
                                    int(parts[2], 16),
                                ))
                    if hints:
                        hint_map[base] = hints
                except (IOError, OSError, ValueError):
                    pass

        # 内容级预去重(#1,跨 item):SymCC 常吐出【字节完全相同】的重复输出(实测约 23–28%);字节相同
        # → 边集必然相同 → dedup 结果必与首次相同(不可能为"新"),可跳过昂贵的 showmap。用 worker 生命
        # 周期的有界集(worker_seen)兼吃 item 内与 item 间重复;未提供则退回 per-item 集。
        seen_content = worker_seen if worker_seen is not None else set()
        if len(seen_content) > _WORKER_SEEN_CAP:   # 有界:超限清空(仅损失少量去重机会,不影响正确性)
            seen_content.clear()

        # pass 1:读内容 + 预去重,收集唯一测试用例
        uniq: "list[tuple[object, bytes]]" = []
        for entry in entries:
            if entry.name.startswith(".") or entry.name.endswith(".hints") or not entry.is_file():
                continue
            total_generated += 1
            if redun is not None:
                redun["gen"] += 1
            try:
                with open(entry.path, "rb") as f:
                    content = f.read()
            except (IOError, OSError):
                continue
            _ckey = hashlib.blake2b(content, digest_size=16).digest()
            if _ckey in seen_content:
                if redun is not None:
                    redun["byte_dup"] = redun.get("byte_dup", 0) + 1
                continue
            seen_content.add(_ckey)
            uniq.append((entry, content))

        # pass 2a:批量 showmap(#2,afl-showmap -I 一次 C 侧 forkserver 循环跑完,实测 ~25us/输入,比逐个
        # 流式 get_edges(~600us,含 Python 每次管道往返)快约一个数量级)。afl-showmap -I 失败→退回逐个流式。
        batch_edges: "dict[str, list[tuple[int, int]]]" = {}
        use_batch = False
        if (worker_coverage is not None and uniq and streaming_showmap is not None
                and os.environ.get("SYMCC_BATCH_SHOWMAP", "1") != "0"
                and getattr(streaming_showmap, "_afl_showmap", None)):
            batch_edges = batch_showmap_edges(
                streaming_showmap._afl_showmap, streaming_showmap._target_cmd,
                streaming_showmap._uses_shmem, [e.path for e, _ in uniq], output_dir)
            use_batch = bool(batch_edges)

        # pass 2b:合并 + 收集 interesting(批量命中用 batch_edges,否则退回流式 get_edges)
        for entry, content in uniq:
            try:
                if worker_coverage is not None and (use_batch or streaming_showmap is not None):
                    edges = (batch_edges.get(entry.path) if use_batch
                             else streaming_showmap.get_edges(content))
                    if edges is None:                 # 无 map(超时/崩溃)或流式进程死亡
                        if redun is not None:
                            redun["showmap_none"] += 1
                        continue
                    has_new_vs_snap = True             # 相对本 item 起点是否有新边(边级)
                    if redun is not None and _snap is not None:
                        has_new_vs_snap = any(e >= len(_snap) or _snap[e] == 0 for e in edges)
                    is_new = worker_coverage.merge(edges)
                    if redun is not None:
                        if is_new:
                            redun["reported"] += 1        # worker 判新 → 上报 master
                        elif not has_new_vs_snap:
                            redun["infeasible"] += 1      # 没打到任何新边(乐观求解不可行/冗余)
                        else:
                            redun["worker_fresh"] += 1    # 打到新边但本 item 内已被自己覆盖
                    if is_new:
                        tc_entry = {"content": content, "bitmap": edges}
                        if entry.name in hint_map:        # 附加约束 hint 信息
                            tc_entry["hints"] = hint_map[entry.name]
                        new_tests.append(tc_entry)
                else:
                    tc_entry = {"content": content}
                    if entry.name in hint_map:
                        tc_entry["hints"] = hint_map[entry.name]
                    new_tests.append(tc_entry)
            except (IOError, OSError):
                pass

    post_elapsed = time.monotonic() - _post_start
    return new_tests, total_generated, retcode, elapsed, killed, post_elapsed


class StreamingShowmap:
    """afl-showmap -S 流式模式封装。

    维持持久 fork server，通过 stdin/stdout 管道传输测试用例。
    每次调用 ~0.6ms（vs fork 模式 ~12ms，19x 加速）。
    """

    _MAX_EDGES = 1 << 20   # edge count 上限，防止损坏 count 导致超长循环

    def __init__(self, afl_showmap: str, target_cmd: list[str]):
        self._proc = None    # 先置空：若下方 Popen 抛异常，__del__→close() 也能安全跳过
        self._dead = True
        # 存下配置供批量路径(batch_showmap_edges)复用,免在调用点再传一遍
        self._afl_showmap = afl_showmap
        self._target_cmd = list(target_cmd)
        try:                 # 持久/shmem 目标(含 ##SIG_AFL_PERSISTENT##)批量时无需 @@
            with open(target_cmd[0], "rb") as _bf:
                self._uses_shmem = b"##SIG_AFL_PERSISTENT##" in _bf.read()
        except (IOError, OSError, IndexError):
            self._uses_shmem = False
        cmd = [afl_showmap, "-S", "-t", "5000", "-m", "none", "--"]
        cmd.extend(target_cmd)
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._dead = False   # 协议 EOF/进程死亡后置位，避免复用被截断的管道

    def _read_exact(self, n: int) -> bytes | None:
        """精确读取 n 字节；EOF/进程死亡返回 None 并标记 oracle 已死。"""
        buf = bytearray()
        rd = self._proc.stdout
        while len(buf) < n:
            chunk = rd.read(n - len(buf))
            if not chunk:
                self._dead = True
                return None
            buf += chunk
        return bytes(buf)

    def get_edges(self, content: bytes) -> list[tuple[int, int]] | None:
        """发送测试用例内容，返回稀疏边列表 [(edge_id, count), ...]。
        崩溃/超时输入仍返回其（可能为空的）边列表；仅进程死亡才返回 None（且不再复用）。"""
        if self._dead:
            return None
        try:
            self._proc.stdin.write(struct.pack("<I", len(content)))
            self._proc.stdin.write(content)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._dead = True
            return None
        # 接收: [u16 status][u32 edges_count][(u32 eid, u8 count) × N]
        #        [u32 stdout_len][stdout][u32 stderr_len][stderr]
        if self._read_exact(2) is None:                       # status
            return None
        raw = self._read_exact(4)                             # edges_count
        if raw is None:
            return None
        edges_count = struct.unpack("<I", raw)[0]
        if edges_count > self._MAX_EDGES:                     # 损坏 count 防护
            self._dead = True
            return None
        pair = self._read_exact(5 * edges_count)              # (u32 eid, u8 cnt) × N
        if pair is None:
            return None
        edges = list(_EDGE_STRUCT.iter_unpack(pair))
        for _ in range(2):                                    # 排空 stdout/stderr
            lraw = self._read_exact(4)
            if lraw is None:
                return None
            blen = struct.unpack("<I", lraw)[0]
            if blen and self._read_exact(blen) is None:
                return None
        return edges

    def close(self) -> None:
        if self._proc is None:   # Popen 未成功创建 → 无需清理
            return
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

    def __del__(self):
        self.close()


def batch_showmap_edges(afl_showmap: str, target_cmd: "list[str]", uses_shmem: bool,
                        file_paths: "list[str]", work_dir: str,
                        timeout_ms: int = 5000) -> "dict[str, list[tuple[int, int]]]":
    """批量 showmap:用 afl-showmap -I filelist 在【一次】C 侧 forkserver 循环里跑完所有输入。

    比逐个流式 get_edges(Python 每次管道往返，实测标称 ~600us/输入)快约一个数量级
    (afl-showmap -I 实测 ~25us/输入),因整个 forkserver 循环在 C 里跑、无 Python 逐次开销。
    返回 {文件路径: 稀疏边列表 [(edge_id, count)]};某输入超时/崩溃→其键缺失(调用方按 showmap_none 处理);
    afl-showmap 整体失败(不支持 -I/报错/无输出)→ 返回 {},调用方退回逐个流式。"""
    if not file_paths:
        return {}
    # 自建专属临时目录(放 work_dir 下,多在 tmpfs)并在最后清理,避免污染/污读 output_dir
    try:
        tmp = tempfile.mkdtemp(prefix="_bsm_", dir=work_dir)
    except (OSError, IOError):
        return {}
    listf = os.path.join(tmp, "flist")
    mapdir = os.path.join(tmp, "maps")
    try:
        os.makedirs(mapdir, exist_ok=True)
        with open(listf, "w") as f:
            f.write("\n".join(file_paths) + "\n")
        cmd = [afl_showmap, "-I", listf, "-o", mapdir, "-t", str(timeout_ms),
               "-m", "none", "-q", "--"]
        cmd.extend(target_cmd)
        if not uses_shmem and "@@" not in target_cmd:
            cmd.append("@@")       # 文件型目标:afl-showmap 把每个输入写临时文件替换 @@
        # afl-showmap -I 内部逐输入处理并各自应用 -t 超时;整体给宽松上限,封顶 30min
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       timeout=min(1800, max(60, len(file_paths) + 30)))
        # afl-showmap -I 以【输入文件名(basename)】命名各自 map;output_dir 内文件名唯一
        out: "dict[str, list[tuple[int, int]]]" = {}
        for p in file_paths:
            try:
                with open(os.path.join(mapdir, os.path.basename(p))) as mf:
                    edges: "list[tuple[int, int]]" = []
                    for line in mf:                   # map 每行 "edge_id:count"
                        line = line.strip()
                        if not line:
                            continue
                        eid, _, cnt = line.partition(":")
                        edges.append((int(eid), int(cnt) if cnt else 0))
            except (IOError, OSError, ValueError):
                continue                              # 无 map(超时/崩溃)→ 调用方视为 showmap_none
            out[p] = edges
        return out
    except (OSError, subprocess.SubprocessError):
        return {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _batch_triage(
    batch_results: list[tuple],
    stats: "Stats",
    coverage: "CoverageBitmap",
    afl_config: "AflConfig",
    queue_dir: str,
    crashes_dir: str,
    hangs_dir: str,
    afl_sync_queue: str,
    save_all_dir: str | None,
    symcc_dir: str,
    bitmap_path_triage: str,
    symcc_feedback_queue: list[tuple[str, int]],
    queue_id_ref: list[int],
    file_generation: dict[str, int] | None = None,
    afl_extras_dir: str | None = None,
    hint_id_ref: list[int] | None = None,
    recent_byte_offsets: list[int] | None = None,
    focus_bytes_window: int = 200,
    yield_callback: "typing.Callable[[str, bool], None] | None" = None,
    analyzed_hashes_ref: "set[str] | None" = None,
) -> bool:
    """批量 triage worker 返回的结果。返回 bitmap 是否有变化。"""
    queue_id = queue_id_ref[0]
    bitmap_changed = False

    for worker_rank, input_path, new_tests, retcode, elapsed, killed in batch_results:
        input_produced_interesting = False
        # 跳过 worker 端"非执行"错误结果（文件缺失/派发前异常：elapsed==0 且
        # retcode==-1 且无输出），否则会以 0 耗时的"成功执行"稀释平均耗时统计。
        # 注：run_symcc_worker 内部异常虽也置 retcode=-1，但 elapsed 已计量（>0）。
        if not (elapsed == 0 and retcode == -1 and not new_tests):
            stats.add_execution(elapsed, killed)
        # new_tests 现在只含 interesting 的 TC（worker 端已做 dedup）

        for tc in new_tests:
            tc_content = tc["content"]
            tc_bitmap = tc.get("bitmap")  # 稀疏边列表 [(edge_id, count)]
            # 内容哈希只算一次，供 save-all / 回退临时名 / AFL-sync 去重复用
            # （避免对同一内容重复 SHA-256 2~3 次）。
            _tc_hash = hashlib.sha256(tc_content).hexdigest()

            # --save-all
            if save_all_dir is not None:
                save_path = os.path.join(save_all_dir, _tc_hash)
                if not os.path.exists(save_path):
                    try:
                        with open(save_path, "wb") as sf:
                            sf.write(tc_content)
                    except (IOError, OSError):
                        pass

            # Triage：优先用 worker 端的稀疏边列表
            if tc_bitmap is not None:
                bitmap_data = tc_bitmap
                result_type = "success"
            else:
                # 回退：写临时文件并运行 showmap
                tc_id = _tc_hash[:16]
                tc_path = os.path.join(symcc_dir, f".tc_{tc_id}")
                with open(tc_path, "wb") as f:
                    f.write(tc_content)
                result_type, bitmap_data = afl_config.run_showmap(
                    tc_path, bitmap_path_triage
                )
                try:
                    os.unlink(tc_path)
                except OSError:
                    pass

            if result_type == "success" and bitmap_data:
                is_new = coverage.merge(bitmap_data)
                if is_new:
                    bitmap_changed = True
                    input_produced_interesting = True
                    orig_name = os.path.basename(input_path)
                    src_id = "000000"
                    if orig_name.startswith("id:") and len(orig_name) >= 9:
                        src_id = orig_name[3:9]
                    new_name = f"id:{queue_id:06d},src:{src_id}"
                    dest = os.path.join(queue_dir, new_name)
                    # 原子写入：先写临时文件再 rename，避免消费者（AFL/master）读到半截种子
                    _dtmp = dest + ".tmp"
                    try:
                        with open(_dtmp, "wb") as f:
                            f.write(tc_content)
                        os.replace(_dtmp, dest)
                    except OSError as _e:
                        # 磁盘满/FS 错误 → 跳过该 TC，绝不让异常冒泡出 _batch_triage 杀死
                        # master（否则所有 worker 阻塞在 recv 上挂死）。与本函数其余写操作
                        # （save-all / crash / hang）的容错一致。
                        print(f"[Master] 队列写入失败，跳过 {new_name}: {_e}",
                              flush=True)
                        continue
                    # 计算迭代代数：输入的代数 + 1
                    parent_gen = 0
                    if file_generation is not None:
                        parent_gen = file_generation.get(input_path, 0)
                    child_gen = parent_gen + 1
                    if file_generation is not None:
                        file_generation[dest] = child_gen
                    # 深度限制检查
                    if MAX_GENERATION_DEPTH <= 0 or child_gen <= MAX_GENERATION_DEPTH:
                        symcc_feedback_queue.append((dest, child_gen))
                    if os.path.isdir(afl_sync_queue):
                        try:
                            _sdest = os.path.join(
                                afl_sync_queue,
                                f"id:symcc_{queue_id:06d},src:{src_id}")
                            _stmp = _sdest + ".tmp"
                            with open(_stmp, "wb") as f:
                                f.write(tc_content)
                            os.replace(_stmp, _sdest)  # 原子：AFL 不会读到半截
                            # 登记内容哈希：best_new_testcases 扫描 AFL 队列时据此跳过
                            # SymCC 刚同步进去的自身输出，避免把 concolic 产物当"新种子"
                            # 重扫再分析（它们已在 symcc_feedback_queue 中处理）——省一次
                            # 冗余 concolic 执行。
                            if analyzed_hashes_ref is not None:
                                analyzed_hashes_ref.add(_tc_hash)
                        except OSError:
                            pass
                    queue_id += 1
                    stats.interesting_count += 1

                    # 将约束 hint 写入 AFL extras 目录（多字节聚合）
                    # 将连续偏移的 hint 聚合为多字节 token，
                    # AFL extras 期望多字节 token（如 "SELECT"）而非单字节
                    tc_hints = tc.get("hints")
                    if tc_hints and afl_extras_dir and hint_id_ref is not None:
                        sorted_hints = sorted(tc_hints, key=lambda h: h[0])
                        tokens: list[bytes] = []
                        cur_token = bytearray()
                        prev_off = -2
                        for _off, _old, _new in sorted_hints:
                            if _off == prev_off + 1:
                                cur_token.append(_new)
                            else:
                                if cur_token:
                                    tokens.append(bytes(cur_token))
                                cur_token = bytearray([_new])
                            prev_off = _off
                        if cur_token:
                            tokens.append(bytes(cur_token))
                        for token in tokens:
                            # 循环复用固定文件池，上限 MAX_HINT_FILES，避免 inode 耗尽
                            hint_path = os.path.join(
                                afl_extras_dir,
                                f"hint_{hint_id_ref[0] % MAX_HINT_FILES:06d}"
                            )
                            try:
                                with open(hint_path, "wb") as hf:
                                    hf.write(token)
                                hint_id_ref[0] += 1
                            except OSError:
                                pass

                    # 仅从 interesting TC 收集偏移用于 focus_bytes
                    if tc_hints and recent_byte_offsets is not None:
                        for _off, _old, _new in tc_hints:
                            recent_byte_offsets.append(_off)
                        # 滑动窗口：只保留最近的偏移
                        if len(recent_byte_offsets) > focus_bytes_window:
                            del recent_byte_offsets[:-focus_bytes_window]

        # 更新种子类型产出率
        if yield_callback is not None:
            yield_callback(input_path, input_produced_interesting)

        if killed:
            orig_name = os.path.basename(input_path)
            src_id = "000000"
            if orig_name.startswith("id:") and len(orig_name) >= 9:
                src_id = orig_name[3:9]
            hang_name = f"id:{queue_id:06d},src:{src_id}"
            try:
                _hdest = os.path.join(hangs_dir, hang_name)
                shutil.copy2(input_path, _hdest + ".tmp")
                os.replace(_hdest + ".tmp", _hdest)  # 原子落盘，避免外部观察者读到半截
                queue_id += 1
            except (IOError, OSError):
                pass
        elif retcode > 128 and retcode != 137:
            # 目标在 timeout 包装下被致命信号终止（SIGSEGV=139/SIGABRT=134/
            # SIGFPE=136 等；排除超时的 SIGKILL=137，那已由 killed 归入 hangs）
            # → 保存触发崩溃的输入供分析，否则 concolic 发现的崩溃种子被静默丢弃。
            orig_name = os.path.basename(input_path)
            src_id = "000000"
            if orig_name.startswith("id:") and len(orig_name) >= 9:
                src_id = orig_name[3:9]
            crash_name = f"id:{queue_id:06d},src:{src_id}"
            try:
                _cdest = os.path.join(crashes_dir, crash_name)
                shutil.copy2(input_path, _cdest + ".tmp")
                os.replace(_cdest + ".tmp", _cdest)  # 原子落盘
                queue_id += 1
            except (IOError, OSError):
                pass

    queue_id_ref[0] = queue_id
    # 注：不再每批 print 三元组统计（热路径去除 f-string 格式化 + stdout I/O，
    # 与 mpi_concolic_execution 的 master 修复一致）；进度由 master 循环中每 2s 的
    # 轻量汇总行 + 周期性完整 Stats 行输出，聚合计数走全局 stats。
    return bitmap_changed


def master(comm: "MPI.Intracomm", args: argparse.Namespace) -> None:
    """Master process: monitors AFL queue, distributes work, triages results."""
    size = comm.Get_size()
    num_workers = size - 1

    if num_workers == 0:
        print("Error: need at least 2 MPI processes.", file=sys.stderr)
        return

    # Setup
    afl_queue_dir = os.path.join(args.output_dir, args.fuzzer_name)
    symcc_dir = os.path.join(args.output_dir, args.name)

    if os.path.exists(symcc_dir):
        print(f"Error: {symcc_dir} already exists. "
              f"We don't support resuming.", file=sys.stderr)
        for rank in range(1, size):
            while comm.iprobe(source=rank, tag=TAG_READY):
                comm.recv(source=rank, tag=TAG_READY)
            comm.send(None, dest=rank, tag=TAG_STOP)
        return

    os.makedirs(symcc_dir)
    queue_dir = os.path.join(symcc_dir, "queue")
    hangs_dir = os.path.join(symcc_dir, "hangs")
    crashes_dir = os.path.join(symcc_dir, "crashes")
    os.makedirs(queue_dir)
    os.makedirs(hangs_dir)
    os.makedirs(crashes_dir)

    # AFL 反馈目录：将有趣的 SymCC 输出同步回 AFL 的 queue，形成双向反馈环
    afl_sync_queue = os.path.join(afl_queue_dir, "queue")  # fuzzer01/queue/

    # 约束 hint 目录：写到 symcc_dir/extras，与 run_hybrid 传给 grimoire_gen 的 --extras
    # 路径一致（此前写到 output_dir/extras 与之不符 → GRIMOIRE 读空、hint 同步静默失效）。
    # 注：AFL 不会自动加载此目录，需显式 -x 才作字典；GRIMOIRE 经 --extras 消费这些 token。
    afl_extras_dir = os.path.join(symcc_dir, "extras")
    os.makedirs(afl_extras_dir, exist_ok=True)
    hint_id_ref = [0]

    # 选择性符号化：跟踪近期产出 interesting 结果的字节偏移范围
    # 使用滑动窗口避免范围无限膨胀（只保留最近 200 个偏移）
    recent_byte_offsets: list[int] = []
    FOCUS_BYTES_WINDOW = 200
    focus_bytes_str = ""

    stats_file = open(os.path.join(symcc_dir, "stats"), "w")
    bitmap_path_triage = os.path.join(symcc_dir, ".triage_bitmap")

    # Load AFL config
    try:
        afl_config = AflConfig(afl_queue_dir)
    except (OSError, RuntimeError, ValueError, IndexError) as e:
        print(f"Error loading AFL config: {e}", file=sys.stderr)
        stats_file.close()
        for rank in range(1, size):
            while comm.iprobe(source=rank, tag=TAG_READY):
                comm.recv(source=rank, tag=TAG_READY)
            comm.send(None, dest=rank, tag=TAG_STOP)
        return

    print("[Master] SymCC MPI Fuzzing Helper")
    print(f"[Master] Workers: {num_workers}")
    print(f"[Master] AFL queue: {afl_config.queue}")
    print(f"[Master] AFL showmap: {afl_config.show_map}")
    print(f"[Master] SymCC output: {symcc_dir}")

    coverage = CoverageBitmap()
    # 跳过耗时的 bitmap 初始化 — 前几个 triage 结果会自然建立 bitmap，
    # 代价是初期可能有少量假阳性 (interesting)，但不影响正确性
    stats = Stats()
    processed_files = set()
    processed_content_hashes = set()  # SHA-256 of already-analyzed file contents
    active_workers = {}  # rank -> input_path
    queue_id_ref = [0]  # 可变引用，供 _batch_triage 更新
    last_stats_time = time.monotonic()
    # 轻量进度汇总节流（替代每批 triage print）：每 2s 一行，聚合计数走全局 stats
    last_progress_time = time.monotonic()
    prog_prev_generated = 0
    PROGRESS_INTERVAL = 2.0

    # SymCC 产生的有趣测试用例队列，会被重新分发给 workers
    # 每个元素是 (path, generation_depth)，depth=0 为 AFL 种子，depth=N 为第 N 代 SymCC 输出
    symcc_feedback_queue: list[tuple[str, int]] = []
    # 记录每个文件的迭代代数
    file_generation: dict[str, int] = {}
    max_generation_reached = 0

    # GRIMOIRE 高价值输入直连 SymCC：master 扫描 grimoire-feed 目录，把新文件注入
    # 反馈队列，让 concolic 直接从结构有效的深层输入继续挖（结构合成 × 约束求解协同）。
    grimoire_feed_dir = args.grimoire_feed
    grimoire_seen: set[str] = set()
    last_grimoire_scan = 0.0

    # 细粒度并行分解 / 动态工作窃取（opt-in）：空闲产能出现时把种子细分为不相交字节区间
    # 子项填补 worker（见 _build_work_items）。worker 侧还按 rank 分配不同求解策略。
    _diversity = os.environ.get("SYMCC_WORKER_DIVERSITY") == "1"
    _focus_parts = max(1, int(os.environ.get("SYMCC_FOCUS_PARTITIONS", "8")))
    # 按分支密度均衡划分（opt-in SYMCC_DENSITY_BALANCE=1，需密度剖析版 runtime）：细分前
    # 先用无求解的密度剖析 profile 种子，把热点字节隔离到窄区间，使各子项工作量更均衡。
    _density_balance = _diversity and os.environ.get("SYMCC_DENSITY_BALANCE") == "1"
    _target_cmd = args.target
    _use_stdin = "@@" not in _target_cmd
    _density_cache: "dict[str, list[int] | None]" = {}
    # 每轮冷剖析预算：限制单轮 _build_work_items 内"新种子"的阻塞式 profile 数，避免
    # master 在一批全新种子上串行剖析而停服 MPI（超预算的种子本轮退回等宽，下轮再试）。
    _profile_budget = [8]
    _prof_dir = (tempfile.mkdtemp(prefix="symcc_dprof_")
                 if _density_balance else None)

    def _profile_density(path: str) -> "list[int] | None":
        """无求解密度剖析：运行 SymCC（SYMCC_DENSITY_OUT）统计每字节被多少分支依赖。
        按内容哈希缓存；失败/无密度返回 None（调用方退回等宽）。"""
        try:
            with open(path, "rb") as _pf:
                content = _pf.read()
        except OSError:
            return None
        h = hashlib.sha256(content).hexdigest()
        if h in _density_cache:
            return _density_cache[h]
        if _profile_budget[0] <= 0:
            return None  # 本轮冷剖析预算耗尽 → 退回等宽（不写缓存，下轮可再试）
        _profile_budget[0] -= 1
        flen = len(content)
        dfile = os.path.join(_prof_dir, "density.txt")
        try:
            os.unlink(dfile)
        except OSError:
            pass
        penv = dict(os.environ)
        penv["SYMCC_DENSITY_OUT"] = dfile
        penv["SYMCC_OUTPUT_DIR"] = _prof_dir           # runtime 要求存在
        penv["SYMCC_ENABLE_LINEARIZATION"] = "1"
        penv.pop("SYMCC_WORKER_DIVERSITY", None)       # 剖析子进程不需要
        stdin_arg: "typing.Any" = subprocess.DEVNULL
        if _use_stdin:
            cmd = ["timeout", "-k", "2", "10"] + _target_cmd
        else:
            penv["SYMCC_INPUT_FILE"] = path
            cmd = ["timeout", "-k", "2", "10"] + [
                a.replace("@@", path) for a in _target_cmd]
        result: "list[int] | None" = None
        try:
            if _use_stdin:
                stdin_arg = open(path, "rb")
            subprocess.run(cmd, env=penv, stdin=stdin_arg,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        except (OSError, subprocess.SubprocessError):
            _density_cache[h] = None
            return None
        finally:
            if _use_stdin and hasattr(stdin_arg, "close"):
                try:
                    stdin_arg.close()
                except OSError:
                    pass
        dens = [0] * flen
        try:
            with open(dfile) as df:
                for line in df:
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.split()
                    if len(parts) == 2:
                        off = int(parts[0])
                        if 0 <= off < flen:
                            dens[off] = int(parts[1])
        except (OSError, ValueError):
            _density_cache[h] = None
            return None
        result = dens if any(dens) else None
        _density_cache[h] = result
        return result

    # 边产出率在线学习（CoFuzz + T-Scheduler 风格）：
    # 跟踪每种种子类型被 concolic 分析后产出 interesting 结果的概率
    # 使用 Beta-Bernoulli Thompson Sampling（T-Scheduler AsiaCCS'24）
    edge_yield_counts: dict[str, list[int]] = {
        "cov": [1, 1],     # [alpha (successes+1), beta (failures+1)]，先验 Beta(1,1)
        "symcc": [1, 1],
        "normal": [1, 1],
    }

    def _update_edge_yield(input_path: str, produced_interesting: bool) -> None:
        """更新种子类型的 Beta 分布参数。"""
        name = os.path.basename(input_path)
        if "+cov" in name:
            seed_type = "cov"
        elif "symcc_" in name:
            seed_type = "symcc"
        else:
            seed_type = "normal"
        if produced_interesting:
            edge_yield_counts[seed_type][0] += 1  # alpha++
        else:
            edge_yield_counts[seed_type][1] += 1  # beta++

    def _get_edge_yield() -> dict[str, float]:
        """Thompson Sampling：从各类型的 Beta 分布中采样，作为优先级分数。

        比 Laplace 平滑更优：自动在探索（数据少时高方差）
        和利用（数据多时收敛到真实率）之间平衡。
        """
        result = {}
        for k, (alpha, beta) in edge_yield_counts.items():
            result[k] = random.betavariate(alpha, beta)
        return result

    # --save-all: 保存所有生成的测试用例（不经过滤）
    save_all_dir = None
    if args.save_all:
        save_all_dir = args.save_all
        os.makedirs(save_all_dir, exist_ok=True)
        print(f"[Master] Saving all test cases to: {save_all_dir}")

    # #10 重复求解拆分：master 侧落 accepted(=interesting_count,真正判新纳入的)与 generated,
    # 供跨-worker 冗余 = reported(worker 上报) - accepted 计算。
    _wprof = os.environ.get("SYMCC_WORKER_PROFILE") == "1"
    _wprof_dir = os.environ.get("SYMCC_WPROF_DIR") or symcc_dir

    def _flush_master_redun() -> None:
        if not _wprof:
            return
        try:
            os.makedirs(_wprof_dir, exist_ok=True)
            with open(os.path.join(_wprof_dir, "redun_master.csv"), "w") as _f:
                _f.write(f"generated,accepted\n{stats.generated_count},{stats.interesting_count}\n")
        except (IOError, OSError):
            pass

    # 信号处理：收到 SIGTERM/SIGINT 时优雅退出
    shutdown_requested = False

    def _signal_handler(signum: int, frame: object) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        _flush_master_redun()
        print(f"\n[Master] Received signal {signum}, shutting down...",
              file=sys.stderr, flush=True)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Bitmap 版本号：只在有新覆盖时写入共享文件，workers 按版本号决定是否重读
    bitmap_version = 0
    bitmap_shared_path = os.path.join(symcc_dir, ".shared_bitmap")

    # 性能计时器（环境变量 SYMCC_MASTER_PROFILE=1 时输出）
    _prof = os.environ.get("SYMCC_MASTER_PROFILE") == "1"
    _t_scan = 0.0     # AFL queue 扫描耗时
    _t_dispatch = 0.0  # MPI send (dispatch) 耗时
    _t_recv = 0.0      # MPI recv (result) 耗时
    _t_triage = 0.0    # batch_triage 耗时
    _t_idle = 0.0      # sleep 耗时
    _n_scan = 0
    _n_dispatch = 0
    _n_recv = 0
    _n_triage = 0
    _n_recv_bytes = 0   # 估算 MPI recv 数据量

    # 注：曾尝试"多样性调度"（避免并发下发相似种子以降低冗余），但 A/B 实测
    # 14 workers 下 useful 比率 18.0%(off) vs 17.9%(on) 无差异——并行 concolic 冗余
    # 主要是结构性的（不同种子翻转分支后仍产出覆盖公共下游代码的输入），
    # 与文献一致（concolic 并行本质亚线性扩展）。故不采用调度层去冗余，
    # 转而通过 SYMCC_WORKER_CAP 限制 concolic worker 数、把富余核心给扩展性更好的 AFL。

    # 运行时自适应分配（KRAKEN/Boian 风格）：
    #  - control_file (.active_workers)：run_hybrid 控制器写入期望活跃 worker 数 K，
    #    master 只向 rank 1..K 派发，rank K+1..N 被"停泊"（消费其 READY 后不派发，
    #    worker 阻塞在 recv 上，~0 CPU，释放核心给 AFL）。K 增大时主动直接派发唤醒。
    #    停泊可逆、不丢弃任何种子 → 不丢覆盖率。
    #  - stats_out_file (.symcc_stats)：master 周期性写出累计产出，供控制器读取产出率。
    control_file = os.path.join(symcc_dir, ".active_workers")
    stats_out_file = os.path.join(symcc_dir, ".symcc_stats")
    max_active_workers = num_workers      # 默认全部活跃
    # idle_ranks：已发 READY、正阻塞在 recv 等待工作的 worker。
    # 其中 rank > max_active_workers 者被"停泊"（不派发 → ~0 CPU）；
    # K 增大时它们自动变为可派发（无需额外唤醒逻辑）。
    idle_ranks: set[int] = set()
    last_control_check = 0.0
    last_stats_out = 0.0                   # .symcc_stats 快速写出节流（供控制器）
    last_scan_time = 0.0                   # AFL queue 扫描节流
    SCAN_MIN_INTERVAL = 0.1                # worker 都在忙时，最多每 100ms 扫一次队列
    # BSFuzz 跨-worker 超时分支共享聚合
    branch_share_master = os.environ.get("SYMCC_BRANCH_SHARE") == "1"
    skip_sites_master_path = os.path.join(symcc_dir, ".skip_sites")
    global_timeout_sites: set[int] = set()

    def _read_active_workers() -> int:
        try:
            with open(control_file) as cf:
                k = int(cf.read().strip())
            return max(1, min(num_workers, k))
        except (IOError, OSError, ValueError):
            return max_active_workers  # 无文件/无效 → 保持

    def _write_symcc_stats() -> None:
        try:
            tmp = stats_out_file + ".tmp"
            with open(tmp, "w") as sf:
                sf.write("%d %d %d %d\n" % (
                    stats.interesting_count, stats.generated_count,
                    len(coverage.edges), max_active_workers))
            os.replace(tmp, stats_out_file)
        except OSError:
            pass

    # 动态工作窃取的"未派发工作项"跨轮结转：一轮内因 worker 全忙而未及派发的（细分）
    # 工作项原样保留到下一轮，避免按路径去重误将同种子的其余字节区间丢弃（否则该种子
    # 只分析了首个区间就再不复访）。carried_paths 用于把已在结转队列中的路径排除出本轮
    # 重新细分，避免与 AFL 队列重扫产生重复项。
    carried_items: "list[tuple[str, str | None]]" = []
    carried_paths: set[str] = set()

    # K-Scheduler 风格前沿调度（opt-in，SYMCC_KSCHED=1）：按"覆盖当前稀有边（≈覆盖前沿/
    # CFG 中心性）"给 AFL 候选种子加权，把 concolic 预算投向最可能触达未探索区域处
    # （She et al. S&P'22）。用 afl-showmap 取每种子边集（按内容哈希缓存、每轮限流预算），
    # rarity=Σ 1/(freq+1)。默认关闭：零成本、不影响既有调度。失败一律返回 0（优雅退化）。
    _ksched = os.environ.get("SYMCC_KSCHED") == "1"
    _edge_freq: dict[int, int] = {}
    _frontier_cache: dict[str, float] = {}
    _frontier_budget = [16]
    _frontier_bm = os.path.join(symcc_dir, ".frontier_bm")

    def _frontier_score(path: str) -> float:
        try:
            with open(path, "rb") as _ff:
                h = hashlib.sha256(_ff.read()).hexdigest()
        except OSError:
            return 0.0
        cached = _frontier_cache.get(h)
        if cached is not None:
            return cached
        if _frontier_budget[0] <= 0:
            return 0.0                       # 本轮 showmap 预算耗尽 → 暂记 0，下轮再算
        _frontier_budget[0] -= 1
        try:
            rtype, data = afl_config.run_showmap(path, _frontier_bm)
        except (OSError, subprocess.SubprocessError):
            data, rtype = None, "err"
        if rtype != "success" or not data:
            _frontier_cache[h] = 0.0
            return 0.0
        score = 0.0
        for e, b in enumerate(data):
            if b:
                f = _edge_freq.get(e, 0)
                score += 1.0 / (f + 1)       # 稀有边贡献大（前沿）
                _edge_freq[e] = f + 1
        _frontier_cache[h] = score
        return score

    try:
        while not shutdown_requested:
            # 读取自适应控制：期望活跃 worker 数（限流，避免每轮 IO）
            _now = time.monotonic()
            if _now - last_control_check > 1.0:
                max_active_workers = _read_active_workers()
                last_control_check = _now
                # 内存安全阀：去重/跟踪容器超限时清空以限制内存（有界重复分析，
                # 不影响正确性）。~1s 一次的廉价 len 检查。
                for _c, _nm in ((processed_files, "processed_files"),
                                (processed_content_hashes,
                                 "processed_content_hashes"),
                                (grimoire_seen, "grimoire_seen"),
                                (_density_cache, "_density_cache"),
                                (_frontier_cache, "_frontier_cache"),
                                (file_generation, "file_generation")):
                    if len(_c) > MAX_DEDUP_ENTRIES:
                        _c.clear()
                        print(f"[Master] {_nm} 超过 {MAX_DEDUP_ENTRIES} 条，"
                              f"已清空以限制内存", flush=True)
            # 快速写出产出统计（每 ~5s），供 run_hybrid 控制器及时响应
            if _now - last_stats_out > 5.0:
                _write_symcc_stats()
                last_stats_out = _now
            # 扫描 GRIMOIRE 高价值馈送目录，新文件注入反馈队列（限流 ~3s）
            if grimoire_feed_dir and _now - last_grimoire_scan > 3.0:
                last_grimoire_scan = _now
                _gnew = 0
                try:
                    for gname in os.listdir(grimoire_feed_dir):
                        gp = os.path.join(grimoire_feed_dir, gname)
                        if gp in grimoire_seen or not os.path.isfile(gp):
                            continue
                        grimoire_seen.add(gp)
                        # 代数记为 0（视作新种子级）；结构有效 → concolic 深挖
                        file_generation.setdefault(gp, 0)
                        symcc_feedback_queue.append((gp, 0))
                        _gnew += 1
                except OSError:
                    pass
                if _gnew:
                    print(f"[Master] GRIMOIRE feed: +{_gnew} structured inputs -> "
                          f"SymCC ({len(grimoire_seen)} total)", flush=True)
            # 合并输入源：SymCC 反馈用例优先，然后是 AFL queue 的新文件
            # 提取反馈队列：(path, generation) 元组
            pending_feedback_tuples = list(symcc_feedback_queue)
            symcc_feedback_queue.clear()
            # 按代数降序排列：深度优先，优先探索最新一代的输出
            pending_feedback_tuples.sort(key=lambda x: x[1], reverse=True)
            pending_feedback = [p for p, _g in pending_feedback_tuples]
            # 记录最大代数
            for _p, _g in pending_feedback_tuples:
                if _g > max_generation_reached:
                    max_generation_reached = _g

            # 扫描 AFL queue 的条件（避免 worker 全忙时忙等式重复 scandir）：
            #  - 有足够反馈用例可分发 → 跳过扫描；
            #  - 有空闲 worker 需要喂 → 立即扫描（响应性）；
            #  - 否则按 SCAN_MIN_INTERVAL 节流（worker 都在忙时最多 10 次/秒）。
            idle_worker_waiting = len(active_workers) < max_active_workers
            scan_due = (_now - last_scan_time) >= SCAN_MIN_INTERVAL
            # 阈值/批量都以"活跃" worker 数为准：停泊的 worker 不消费候选，
            # 用 num_workers 会在停泊时过度取用并过度扫描。
            if pending_feedback and len(pending_feedback) >= max_active_workers:
                new_inputs = []
            elif not (idle_worker_waiting or scan_due):
                new_inputs = []  # 节流：worker 都在忙且刚扫过 → 跳过
            else:
                _t0 = time.monotonic()
                _frontier_budget[0] = 16       # 重置本轮前沿 showmap 预算（见 _frontier_score）
                new_inputs = afl_config.best_new_testcases(
                    processed_files, batch_size=max_active_workers * 4,
                    analyzed_hashes=processed_content_hashes,
                    edge_yield=_get_edge_yield(),
                    frontier_fn=_frontier_score if _ksched else None,
                )
                last_scan_time = _now
                # AFL 种子代数为 0
                for inp in new_inputs:
                    if inp not in file_generation:
                        file_generation[inp] = 0
                if _prof:
                    _t_scan += time.monotonic() - _t0
                    _n_scan += 1

            # 动态工作窃取：整-种子项不足以填满活跃 worker 时，把种子细分为不相交字节
            # 区间子项填补空闲产能（多样性模式；否则等价于原来的整-种子列表）。上一轮
            # 未派发完的工作项（carried_items）优先结转到本轮队首，且其路径不再参与本轮
            # 细分，避免重复；剩余待填产能 = 活跃 worker 数 - 已结转项数。
            _profile_budget[0] = 8         # 重置本轮冷剖析预算（见 _profile_density）
            _src = [p for p in (pending_feedback + new_inputs)
                    if p not in carried_paths]
            _remaining_target = max(1, max_active_workers - len(carried_items))
            work_queue = carried_items + _build_work_items(
                _src, _remaining_target,
                _diversity, _focus_parts,
                density_fn=_profile_density if _density_balance else None)

            # 交替处理 READY 和 RESULT 消息，避免单方向阻塞
            work_idx = 0
            any_progress = True

            def _dispatch_to(wr: int, item: "tuple[str, str | None]") -> None:
                # item = (种子路径, focus 区间或 None)。只发路径 + focus + bitmap 版本号，
                # 不发内容（worker 自己读文件）。focus 为工作窃取分配的不相交字节区间。
                input_file, item_focus = item
                comm.send({
                    "path": input_file,
                    "bitmap_version": bitmap_version,
                    "bitmap_path": bitmap_shared_path,
                    "focus_bytes": item_focus if item_focus is not None
                    else focus_bytes_str,
                }, dest=wr, tag=TAG_WORK)
                active_workers[wr] = input_file
                processed_files.add(input_file)
                # 复用 best_new_testcases 已缓存的内容哈希（AFL 种子在 _file_cache 中已算过）
                # → 派发热路径上省去重复 open+read+SHA-256；反馈用例不在缓存则回退现算。
                _ch = afl_config._file_cache.get(input_file, {}).get("hash")
                if _ch is None:
                    try:
                        with open(input_file, "rb") as _f:
                            _ch = hashlib.sha256(_f.read()).hexdigest()
                    except (IOError, OSError):
                        _ch = None
                if _ch is not None:
                    processed_content_hashes.add(_ch)

            while any_progress:
                any_progress = False

                # 排空所有 READY 到 idle 集合（一并消费，避免遗留缓冲）
                _rstatus = MPI.Status()
                while comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_READY,
                                  status=_rstatus):
                    wr = _rstatus.Get_source()
                    comm.recv(source=wr, tag=TAG_READY)
                    idle_ranks.add(wr)
                    any_progress = True

                # 仅向"活跃"（rank <= max_active_workers）的空闲 worker 派发；
                # 超额 rank 保持在 idle_ranks 中停泊（不占 CPU）。
                if work_idx < len(work_queue) and idle_ranks:
                    _t0 = time.monotonic()
                    for wr in sorted(idle_ranks):
                        if work_idx >= len(work_queue):
                            break
                        if wr > max_active_workers:
                            continue  # 停泊
                        _dispatch_to(wr, work_queue[work_idx])
                        work_idx += 1
                        idle_ranks.discard(wr)
                        any_progress = True
                    if _prof:
                        _t_dispatch += time.monotonic() - _t0
                        _n_dispatch += 1

                # 收集已完成 workers 的结果（非阻塞）
                if comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_RESULT):
                    any_progress = True
                    # 批量收集所有可用结果
                    batch_results: list[tuple] = []
                    _t0 = time.monotonic()
                    while comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_RESULT):
                        status = MPI.Status()
                        result = comm.recv(
                            source=MPI.ANY_SOURCE, tag=TAG_RESULT, status=status
                        )
                        wr = status.Get_source()
                        ip = active_workers.pop(wr, "unknown")
                        new_tcs = result.get("new_tests", [])
                        total_gen = result.get("total_generated", len(new_tcs))
                        stats.generated_count += total_gen
                        # BSFuzz：聚合超时分支 site_id，增长时写出共享跳过集
                        if branch_share_master:
                            ts = result.get("timeout_sites")
                            if ts:
                                before_n = len(global_timeout_sites)
                                global_timeout_sites.update(ts)
                                if len(global_timeout_sites) > before_n:
                                    try:
                                        _tmp = skip_sites_master_path + ".tmp"
                                        with open(_tmp, "w") as _sf:
                                            _sf.write("\n".join(
                                                str(s) for s in global_timeout_sites))
                                        os.replace(_tmp, skip_sites_master_path)
                                    except OSError:
                                        pass
                        if _prof:
                            _n_recv_bytes += sum(
                                len(tc.get("content", b"")) for tc in new_tcs
                            )
                        batch_results.append((
                            wr, ip, new_tcs,
                            result.get("retcode", 0),
                            result.get("elapsed", 0),
                            result.get("killed", False),
                        ))
                    if _prof:
                        _t_recv += time.monotonic() - _t0
                        _n_recv += len(batch_results)

                    # 批量 triage
                    _t0 = time.monotonic()
                    if batch_results:
                        bitmap_changed = _batch_triage(
                            batch_results, stats, coverage, afl_config,
                            queue_dir, crashes_dir, hangs_dir, afl_sync_queue,
                            save_all_dir, symcc_dir, bitmap_path_triage,
                            symcc_feedback_queue, queue_id_ref,
                            file_generation=file_generation,
                            afl_extras_dir=afl_extras_dir,
                            hint_id_ref=hint_id_ref,
                            recent_byte_offsets=recent_byte_offsets,
                            focus_bytes_window=FOCUS_BYTES_WINDOW,
                            yield_callback=_update_edge_yield,
                            analyzed_hashes_ref=processed_content_hashes,
                        )
                        if bitmap_changed:
                            bitmap_version += 1
                            if coverage.data:
                                tmp_path = bitmap_shared_path + ".tmp"
                                with open(tmp_path, "wb") as f:
                                    f.write(bytes(coverage.data))
                                os.replace(tmp_path, bitmap_shared_path)
                        # 更新 focus_bytes：仅从 interesting TCs 收集偏移
                        if len(recent_byte_offsets) >= 5:
                            min_off = max(0, min(recent_byte_offsets) - 32)
                            max_off = max(recent_byte_offsets) + 32
                            focus_bytes_str = f"{min_off}-{max_off}"

                    if _prof:
                        _t_triage += time.monotonic() - _t0
                        _n_triage += 1

            # 未派发完的工作项（含细分字节区间）按项结转到下一轮，避免按路径去重把同
            # 种子的其余区间丢弃。代数信息保留在 file_generation 中，派发时可查。
            carried_items = work_queue[work_idx:]
            # 安全阀：持续高产饱和时积压可能增长，超上限则截断以限制内存。被截断的项不会
            # 永久丢失覆盖率——其内容也已写入 AFL 队列/同步副本，会被后续扫描重新纳入（仅
            # 丢失代数深度、多做少量重扫）。carried_items 仅存 (path, focus) 小元组，故上限
            # 设得较宽，正常运行几乎不触发。
            _carry_cap = max(4096, max_active_workers * (_focus_parts + 1) * 4)
            if len(carried_items) > _carry_cap:
                print(f"[Master] carried_items 积压 {len(carried_items)} 超 "
                      f"{_carry_cap}，截断以限制内存", flush=True)
                carried_items = carried_items[:_carry_cap]
            carried_paths = {p for p, _ in carried_items}

            # 旧的 collect/triage 代码已移到 while 循环内的交替处理中

            # 轻量进度汇总（每 2s，替代每批 triage print）：热路径外、走全局计数
            _pnow = time.monotonic()
            if _pnow - last_progress_time >= PROGRESS_INTERVAL:
                _dt = _pnow - last_progress_time
                _rate = (stats.generated_count - prog_prev_generated) / _dt if _dt > 0 else 0
                print(f"[Master] {stats.interesting_count} interesting / "
                      f"{stats.generated_count} generated, "
                      f"{len(active_workers)} busy ({_rate:.0f} tc/s)", flush=True)
                last_progress_time = _pnow
                prog_prev_generated = stats.generated_count

            # Periodic stats output
            stats_interval = 15 if _prof else STATS_INTERVAL_SEC
            if time.monotonic() - last_stats_time > stats_interval:
                stats.log(stats_file)
                last_stats_time = time.monotonic()
                yields = _get_edge_yield()
                yield_str = " ".join(
                    f"{k}={v:.2f}(a={edge_yield_counts[k][0]},b={edge_yield_counts[k][1]})"
                    for k, v in yields.items()
                )
                _active_now = min(max_active_workers, num_workers)
                print(f"[Master] Stats: {stats.total_count} ok, "
                      f"{stats.failed_count} failed, "
                      f"{stats.interesting_count} interesting / "
                      f"{stats.generated_count} total, "
                      f"max_depth={max_generation_reached}, "
                      f"active_workers={_active_now}/{num_workers}, "
                      f"yield=[{yield_str}]")
                # 写出产出统计供 run_hybrid 自适应控制器读取
                _write_symcc_stats()
                if _prof and _n_scan > 0:
                    print(f"[PROF] scan={_t_scan:.2f}s/{_n_scan}x "
                          f"dispatch={_t_dispatch:.2f}s/{_n_dispatch}x "
                          f"recv={_t_recv:.2f}s/{_n_recv}x({_n_recv_bytes//1024}KB) "
                          f"triage={_t_triage:.2f}s/{_n_triage}x "
                          f"idle={_t_idle:.2f}s")
                    sys.stdout.flush()

            # 无输入且无活跃 worker 时等待 AFL 产生新用例
            _t0 = time.monotonic()
            # 是否存在可立即派发的空闲"活跃"worker（rank<=上限；停泊 rank 不算）。
            # 上面的派发循环已把所有可派发的工作排空，故若反馈仍被放回队列，
            # 通常意味着无空闲活跃 worker——此时必须 sleep，否则 100% 忙等空转
            # 直到某 worker 返回（最长 TIMEOUT_SEC）。
            _dispatchable_idle = any(r <= max_active_workers for r in idle_ranks)
            if not work_queue and not active_workers and not symcc_feedback_queue:
                time.sleep(2)
            elif symcc_feedback_queue and _dispatchable_idle:
                pass  # 有反馈且有空闲活跃 worker → 立即分发，不睡
            else:
                # 有活跃 worker 在跑 → RESULT 很快到达：用更短轮询间隔把"结果到达→再派发"
                # 的服务延迟从 50ms 降到 5ms（master 负载 <10%、idle 57%，多轮询开销可忽略；
                # fastSolve 让许多求解变快后，50ms 占单个工作项比例更大）。否则用较长间隔省 CPU。
                time.sleep(0.005 if active_workers else 0.05)
            if _prof:
                _t_idle += time.monotonic() - _t0
    finally:
        # --- 优雅关闭 ---
        # 清理密度剖析临时目录
        if _prof_dir:
            shutil.rmtree(_prof_dir, ignore_errors=True)
        # 输出 profiling 数据
        if _prof:
            wall = time.monotonic() - last_stats_time + STATS_INTERVAL_SEC
            print(f"[PROF] scan:     {_t_scan:>7.2f}s ({_n_scan} calls, "
                  f"avg {_t_scan/_n_scan*1000:.1f}ms)" if _n_scan else "")
            print(f"[PROF] dispatch: {_t_dispatch:>7.2f}s ({_n_dispatch} sends, "
                  f"avg {_t_dispatch/_n_dispatch*1000:.2f}ms)" if _n_dispatch else "")
            print(f"[PROF] recv:     {_t_recv:>7.2f}s ({_n_recv} results, "
                  f"avg {_t_recv/max(_n_recv,1)*1000:.1f}ms, "
                  f"~{_n_recv_bytes/1024/1024:.1f}MB total)")
            print(f"[PROF] triage:   {_t_triage:>7.2f}s ({_n_triage} batches)")
            print(f"[PROF] idle:     {_t_idle:>7.2f}s")
            print(f"[PROF] wall:     {wall:>7.2f}s (总墙钟，含各阶段与 idle)")
            sys.stdout.flush()

        # 先输出最终统计（在尝试与 worker 通信之前，因为 worker 可能已被 SIGTERM 杀死）
        stats.log(stats_file)
        print(f"[Master] Final stats: {stats.total_count} ok, "
              f"{stats.failed_count} failed, "
              f"{stats.interesting_count} interesting / "
              f"{stats.generated_count} total")
        sys.stdout.flush()
        try:
            stats_file.close()
        except OSError:
            pass

        # 尝试发送 TAG_STOP（worker 可能已经死了，忽略错误）
        print("[Master] Shutting down workers...")
        for rank in range(1, size):
            try:
                while comm.iprobe(source=rank, tag=TAG_READY):
                    comm.recv(source=rank, tag=TAG_READY)
                while comm.iprobe(source=rank, tag=TAG_RESULT):
                    comm.recv(source=rank, tag=TAG_RESULT)
                comm.send(None, dest=rank, tag=TAG_STOP)
            except MPI.Exception:
                pass
        # 排空 TAG_STOP 后可能到达的 TAG_READY
        for rank in range(1, size):
            try:
                while comm.iprobe(source=rank, tag=TAG_READY):
                    comm.recv(source=rank, tag=TAG_READY)
            except Exception:
                pass


def worker(comm: "MPI.Intracomm", args: argparse.Namespace) -> None:
    """Worker process: receives inputs, runs SymCC, sends back results."""
    rank = comm.Get_rank()
    target_cmd = args.target
    use_stdin = "@@" not in target_cmd

    worker_dir = tempfile.mkdtemp(prefix=f"symcc_mpi_w{rank}_")

    # 构建 worker 环境变量字典（不修改全局 os.environ）
    bitmap_file = os.path.join(worker_dir, "bitmap")
    worker_env = os.environ.copy()
    worker_env["SYMCC_AFL_COVERAGE_MAP"] = bitmap_file

    # BSFuzz 跨-worker 超时分支共享（opt-in，SYMCC_BRANCH_SHARE=1）：
    #  - SYMCC_SKIP_SITES：master 聚合的全局超时 site_id 文件（每次 SymCC 进程新起，
    #    自动重读最新版，无需版本广播）；
    #  - SYMCC_TIMEOUT_OUT：本 worker 本次运行导出的超时 site_id（随后回传 master 聚合）。
    branch_share = os.environ.get("SYMCC_BRANCH_SHARE") == "1"
    symcc_dir_w = os.path.join(args.output_dir, args.name)
    skip_sites_path = os.path.join(symcc_dir_w, ".skip_sites")
    timeout_out_file = os.path.join(worker_dir, "timeout_sites")
    if branch_share:
        worker_env["SYMCC_SKIP_SITES"] = skip_sites_path

    # 细粒度并行分解（opt-in，SYMCC_WORKER_DIVERSITY=1）：
    #  - 策略轴（本处，per-rank）：每个 worker 一个不同的求解策略画像 —— 负载均衡（各做
    #    完整分析），使相似种子在不同 worker 上产出发散输入；
    #  - 空间轴（focus 字节区间）：由 MASTER 按工作项动态分配（见 _build_work_items 的
    #    动态工作窃取），本 worker 直接采用 WORK 消息里的 focus_bytes。
    diversity = os.environ.get("SYMCC_WORKER_DIVERSITY") == "1"
    div_slot = rank - 1  # rank 0 为 master，worker 从 1 起
    if diversity:
        _prof = SYMCC_STRATEGY_PROFILES[div_slot % len(SYMCC_STRATEGY_PROFILES)]
        for _k, _v in _prof.items():
            worker_env[_k] = _v

    # 初始化 streaming showmap（持久 fork server，~0.6ms/call）
    afl_showmap_path = shutil.which("afl-showmap")
    streaming_sm: StreamingShowmap | None = None
    _sm_init_tries = 0
    _sm_disabled_logged = False
    _SM_MAX_INIT_TRIES = 5   # AFL 首次尚未就绪时给几次重试，之后放弃（避免每轮重建）
    if afl_showmap_path is None:
        # 无 afl-showmap → 无法本地 dedup，每个 TC 全量回传 master 且走全量 showmap
        # triage（MPI/CPU 开销显著上升）。显式告警，避免静默降级不可见。
        print(f"[Worker {rank}] WARNING: afl-showmap 不在 PATH，"
              f"流式 dedup 关闭（每个 TC 全量回传 master，开销上升）", flush=True)
    # Worker 端 coverage bitmap 副本 — 用于本地 dedup
    worker_cov = CoverageBitmap()
    worker_seen: set[bytes] = set()   # 跨 item 内容去重(#1);worker 生命周期,有界见 _WORKER_SEEN_CAP
    current_bitmap_version = -1

    # 每 worker 分相位计时（opt-in SYMCC_WORKER_PROFILE=1，热路径几乎零开销：
    # 每工作项约 6 次 time.monotonic()）。相位：wait=等待/取件, bmsync=位图同步,
    # import=输入拷入, exec=concolic 执行, showmap_dedup=showmap 取边+本地去重,
    # send=回传 master。用于瓶颈分析（如 ~12 worker 饱和点到底卡在执行/后处理/等待）。
    _wprof = os.environ.get("SYMCC_WORKER_PROFILE") == "1"
    _pt = {"wait": 0.0, "bmsync": 0.0, "import": 0.0, "exec": 0.0,
           "showmap_dedup": 0.0, "send": 0.0, "items": 0}
    # #10 重复求解拆分计数：gen=生成总数, reported=worker 判新上报数,
    # infeasible=没打到新边(乐观求解不可行/冗余), worker_fresh=打到新边但本 item 内已被自己覆盖,
    # showmap_none=showmap 无输出。worker-内部冗余=gen-reported;worker-间冗余=reported-accepted(master)。
    _redun = {"gen": 0, "reported": 0, "infeasible": 0, "worker_fresh": 0,
              "showmap_none": 0, "items": 0, "snap_none": 0, "byte_dup": 0}
    # 各 worker 各自把分相位计时落盘：编排层用 SIGTERM 杀 mpirun，进程内 MPI gather 来不及，
    # 故落 per-rank 文件，跑完由 aggregate_phase_timing() 事后合并。目录优先 SYMCC_WPROF_DIR。
    _wprof_dir = os.environ.get("SYMCC_WPROF_DIR") or symcc_dir_w
    _phs = ["wait", "bmsync", "import", "exec", "showmap_dedup", "send"]
    # 在途相位标记 {"phase": 名称|None, "start": 时刻}。被 SIGTERM 打断时把这段"在途"时长
    # 计入【正确的】相位——否则慢目标上单个工作项可能横跨整个窗口、到被杀时相位时长仍为 0
    #（只在完成后累加），导致严重低估。run_symcc_worker 会在 exec→showmap_dedup 转换处更新它。
    _inflight = {"phase": None, "start": 0.0}

    def _flush_prof() -> None:
        if _inflight["phase"] is not None:
            _pt[_inflight["phase"]] += time.monotonic() - _inflight["start"]
            # exec 已完成但（因 item 未跑完）尚未提交的部分，补计到 exec
            _pt["exec"] += _inflight.get("exec_done", 0.0)
            _inflight["phase"] = None
        try:
            os.makedirs(_wprof_dir, exist_ok=True)
            with open(os.path.join(_wprof_dir, f"phase_timing_rank{rank}.csv"), "w") as _f:
                _f.write(f"{rank},{_pt['items']}," +
                         ",".join(f"{_pt[_p]:.4f}" for _p in _phs) + "\n")
            with open(os.path.join(_wprof_dir, f"redun_rank{rank}.csv"), "w") as _f:
                _f.write(f"{rank}," + ",".join(
                    str(_redun.get(_k, 0)) for _k in
                    ["gen", "reported", "infeasible", "worker_fresh", "showmap_none",
                     "items", "snap_none", "byte_dup"]) + "\n")
        except (IOError, OSError):
            pass

    if _wprof:
        # SIGTERM 可捕获，落盘后退出（SIGKILL 不可捕获，但编排层通常先发 SIGTERM 再宽限）
        def _on_term(_signum: "int", _frame: "object") -> None:
            _flush_prof()
            os._exit(0)
        try:
            signal.signal(signal.SIGTERM, _on_term)
        except (ValueError, OSError):
            pass

    while True:
        # Signal ready
        comm.send(rank, dest=0, tag=TAG_READY)

        # Wait for work or stop
        status = MPI.Status()
        _t = time.monotonic() if _wprof else 0.0
        msg = comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
        if _wprof:
            _pt["wait"] += time.monotonic() - _t

        if status.Get_tag() == TAG_STOP:
            break

        if status.Get_tag() != TAG_WORK:
            continue

        input_path = msg["path"]
        bm_version = msg.get("bitmap_version", 0)
        if _wprof:
            _pt["items"] += 1
            _t = time.monotonic()

        # 仅在 bitmap 版本更新时重读共享 bitmap 文件
        if bm_version > current_bitmap_version:
            shared_bm = msg.get("bitmap_path", "")
            if shared_bm and os.path.isfile(shared_bm):
                try:
                    shutil.copy2(shared_bm, bitmap_file)
                    current_bitmap_version = bm_version
                    # 用全局已覆盖边播种本地 dedup（worker_cov）。共享 bitmap 与
                    # streaming showmap 的 get_edges() 同属 afl-showmap 边空间
                    #（同一目标二进制），byte i 非零 ⇔ 边 i 已覆盖。否则新进程的
                    # worker_cov 从空开始，会把全局已知边误判为"新"而重复回传 master。
                    with open(bitmap_file, "rb") as _bmf:
                        _bm_data = _bmf.read()
                    # 播种稠密 data（保留命中桶）+ edges 集合（存在性）。只播种 edges 会
                    # 丢桶信息，使 worker 把"已知边的新命中桶"误判为非 interesting 而丢弃。
                    # SYMCC_NO_WORKER_SEED=1 关闭播种（仅用于 A/B：复现"修复前"worker 从空
                    # dedup 起步、把全局已知边误报为新而过量回传 master 的行为）。
                    if os.environ.get("SYMCC_NO_WORKER_SEED") != "1":
                        # 只需批量拷贝 data；_merge_sparse 用 data==0 判存在，无需 O(map)
                        # 重建 edges 集（worker 不读 worker_cov.edges）。
                        worker_cov.data = bytearray(_bm_data)
                except (IOError, OSError):
                    pass
        if _wprof:
            _pt["bmsync"] += time.monotonic() - _t

        # 延迟初始化 streaming showmap（首次需要 AFL 已写出 fuzzer_stats/命令行）。
        # 限制重试次数：AFL 就绪前给几次机会，之后放弃并告警，避免每个工作项都
        # 重新读盘构造 AflConfig（永久失败时会变成 worker 热路径上的反复 I/O）。
        if (streaming_sm is None and afl_showmap_path
                and _sm_init_tries < _SM_MAX_INIT_TRIES):
            _sm_init_tries += 1
            try:
                afl_cfg = AflConfig(os.path.join(
                    args.output_dir, args.fuzzer_name
                ))
                streaming_sm = StreamingShowmap(
                    afl_showmap_path, afl_cfg.target_command
                )
            except (OSError, RuntimeError, ValueError, IndexError,
                    subprocess.SubprocessError) as e:
                if (_sm_init_tries >= _SM_MAX_INIT_TRIES
                        and not _sm_disabled_logged):
                    _sm_disabled_logged = True
                    print(f"[Worker {rank}] WARNING: 流式 showmap 初始化连续 "
                          f"{_SM_MAX_INIT_TRIES} 次失败（{e}），退化为全量 showmap "
                          f"triage（MPI/CPU 开销上升）", flush=True)

        # 直接读取文件（路径协议，无需通过 MPI 传输内容）
        local_input = os.path.join(worker_dir, "current_input")
        _t = time.monotonic() if _wprof else 0.0
        try:
            shutil.copy2(input_path, local_input)
        except (IOError, OSError):
            # 文件可能被 AFL 删除，跳过
            result = {"new_tests": [], "retcode": -1, "elapsed": 0,
                      "killed": False, "total_generated": 0}
            comm.send(result, dest=0, tag=TAG_RESULT)
            continue
        if _wprof:
            _pt["import"] += time.monotonic() - _t

        # 选择性符号化 focus_bytes：直接采用 WORK 消息里的区间。多样性模式下这是 master
        # 动态工作窃取分配的不相交字节区间（见 _build_work_items）；否则是 master 的全局
        # focus（若有）。空则整段符号化。
        focus = msg.get("focus_bytes", "")
        if focus:
            worker_env["SYMCC_FOCUS_BYTES"] = focus
        elif "SYMCC_FOCUS_BYTES" in worker_env:
            del worker_env["SYMCC_FOCUS_BYTES"]

        # Run SymCC
        run_output = os.path.join(worker_dir, f"output_{time.monotonic_ns()}")

        if branch_share:
            try:
                os.unlink(timeout_out_file)  # 清除上次残留
            except OSError:
                pass
            worker_env["SYMCC_TIMEOUT_OUT"] = timeout_out_file

        try:
            if _wprof:
                _inflight["phase"] = "exec"        # 进入执行；run_symcc_worker 会在转入后处理时改标记
                _inflight["start"] = time.monotonic()
                _inflight["exec_done"] = 0.0        # 本 item 已完成的 exec 时长（转入后处理时填）
            new_tests, total_gen, retcode, elapsed, killed, post_elapsed = \
                run_symcc_worker(
                    target_cmd, local_input, run_output, TIMEOUT_SEC, use_stdin,
                    base_env=worker_env,
                    streaming_showmap=streaming_sm,
                    worker_coverage=worker_cov,
                    inflight=_inflight if _wprof else None,
                    redun=_redun if _wprof else None,
                    worker_seen=worker_seen,
                )
            if _wprof:
                _inflight["phase"] = None          # 正常完成：用真实分段值,不用在途估计
                _pt["exec"] += elapsed              # 相位 E：concolic 执行
                _pt["showmap_dedup"] += post_elapsed  # 相位 F：showmap 取边 + 本地去重

            # 读取本次超时分支 site_id，回传 master 聚合
            timeout_sites = []
            if branch_share:
                try:
                    with open(timeout_out_file) as tf:
                        timeout_sites = [int(x) for x in tf.read().split()]
                except (IOError, OSError, ValueError):
                    pass

            result = {
                "new_tests": new_tests,    # 只含 interesting 的 TC
                "total_generated": total_gen,  # 总生成数（含被过滤的）
                "retcode": retcode,
                "elapsed": elapsed,
                "killed": killed,
                "timeout_sites": timeout_sites,
            }
        except (OSError, subprocess.SubprocessError, ValueError,
                RuntimeError) as e:
            # worker 弹性边界：I/O / 子进程 / 解析 / 运行时错误不应拖垮整个 MPI 作业，
            # 回传错误结果并继续。真正意外的异常（编程 bug）仍会向上抛出以暴露问题。
            print(f"[Worker {rank}] Error: {e}", file=sys.stderr)
            result = {
                "new_tests": [],
                "total_generated": 0,
                "retcode": -1,
                "elapsed": 0,
                "killed": False,
            }

        # Clean up output
        shutil.rmtree(run_output, ignore_errors=True)

        # Send result
        _t = time.monotonic() if _wprof else 0.0
        comm.send(result, dest=0, tag=TAG_RESULT)
        if _wprof:
            _pt["send"] += time.monotonic() - _t
            # 每个工作项后落盘一次（覆盖写,~ms 级）：编排层 SIGTERM→SIGKILL 常在 worker 阻塞于
            # showmap/子进程等原生调用时到达,信号处理器来不及跑;增量落盘保证数据不丢。
            _flush_prof()

    if _wprof:
        _flush_prof()   # 正常收到 TAG_STOP 退出时也落盘
    if streaming_sm is not None:
        streaming_sm.close()
    shutil.rmtree(worker_dir, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MPI-parallel SymCC + AFL fuzzing helper",
        usage="mpirun -np <N> python3 %(prog)s -a FUZZER -o DIR -n NAME -- TARGET [ARGS...]",
    )
    parser.add_argument("-a", "--fuzzer-name", required=True,
                        help="AFL fuzzer instance name")
    parser.add_argument("-o", "--output-dir", required=True,
                        help="AFL output directory")
    parser.add_argument("-n", "--name", required=True,
                        help="Name for this SymCC instance")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output")
    parser.add_argument("--save-all", default=None, metavar="DIR",
                        help="保存所有 SymCC 生成的测试用例到指定目录（不经 afl-showmap 过滤）")
    parser.add_argument("--grimoire-feed", default=None, metavar="DIR",
                        help="GRIMOIRE 高价值（结构有效、覆盖率增益）输入目录；master 会把其中"
                             "新文件直接注入 SymCC 反馈队列，让 concolic 从深层结构输入继续挖")
    parser.add_argument("target", nargs=argparse.REMAINDER,
                        help="Target command (after '--')")

    args = parser.parse_args()

    if args.target and args.target[0] == "--":
        args.target = args.target[1:]

    if not args.target:
        parser.error("No target command. Use: -- TARGET [ARGS...]")

    return args


def _WPROF_PHASES() -> list:
    return ["wait", "bmsync", "import", "exec", "showmap_dedup", "send"]


def aggregate_phase_timing(prof_dir: str) -> "str | None":
    """把各 worker 写出的 phase_timing_rank*.csv 汇总成 phase_timing.csv（每 worker 一行
    + TOTAL + MEAN_PCT），返回汇总文件路径。供跑完后离线聚合（worker 是被 SIGTERM 杀掉的，
    无法在进程内做 MPI gather，故各自落盘、事后合并）。"""
    phases = _WPROF_PHASES()
    rows = []
    try:
        names = sorted(n for n in os.listdir(prof_dir)
                       if n.startswith("phase_timing_rank") and n.endswith(".csv"))
    except OSError:
        return None
    for n in names:
        try:
            with open(os.path.join(prof_dir, n)) as f:
                parts = f.readline().strip().split(",")
            if len(parts) >= 2 + len(phases):
                rows.append(parts)
        except (IOError, OSError, ValueError):
            continue
    if not rows:
        return None
    totals = [0.0] * len(phases)
    n_items = 0
    for r in rows:
        n_items += int(r[1])
        for i in range(len(phases)):
            totals[i] += float(r[2 + i])
    grand = sum(totals) or 1.0
    out = os.path.join(prof_dir, "phase_timing.csv")
    with open(out, "w") as f:
        f.write("rank,items," + ",".join(phases) + ",total_s\n")
        for r in rows:
            tot = sum(float(r[2 + i]) for i in range(len(phases)))
            f.write(",".join(r[:2 + len(phases)]) + f",{tot:.4f}\n")
        f.write(f"TOTAL,{n_items}," + ",".join(f"{t:.4f}" for t in totals) + f",{grand:.4f}\n")
        f.write("MEAN_PCT,," + ",".join(f"{100 * t / grand:.1f}" for t in totals) + ",100.0\n")
    return out


def aggregate_redundancy(prof_dir: str) -> "str | None":
    """合并各 worker 的 redun_rank*.csv 与 redun_master.csv，产出 #10 重复求解拆分报告。
    两层拆分：
      (A) worker-内部冗余 vs worker-间冗余 vs 有效(accepted)，均以生成总数为分母；
      (B) 冗余的两类根因：乐观求解不可行(infeasible,没打到新边) vs 新鲜度间隙(freshness,
          打到新边但已被覆盖=worker 内自复 + worker 间被抢先)。"""
    gen = reported = infeasible = worker_fresh = showmap_none = 0
    tot_items = tot_snap_none = byte_dup = 0
    nworkers = 0
    try:
        names = [n for n in os.listdir(prof_dir)
                 if n.startswith("redun_rank") and n.endswith(".csv")]
    except OSError:
        return None
    for n in names:
        try:
            with open(os.path.join(prof_dir, n)) as f:
                p = f.readline().strip().split(",")
            if len(p) >= 6:
                gen += int(p[1])
                reported += int(p[2])
                infeasible += int(p[3])
                worker_fresh += int(p[4])
                showmap_none += int(p[5])
                if len(p) >= 8:
                    tot_items += int(p[6])
                    tot_snap_none += int(p[7])
                if len(p) >= 9:
                    byte_dup += int(p[8])          # 字节相同被预去重跳过 showmap 的数量
                nworkers += 1
        except (IOError, OSError, ValueError):
            continue
    if gen == 0:
        return None
    accepted = None
    try:
        with open(os.path.join(prof_dir, "redun_master.csv")) as f:
            f.readline()  # header
            accepted = int(f.readline().strip().split(",")[1])
    except (IOError, OSError, ValueError, IndexError):
        accepted = None
    # accepted 不可得时,退化用 reported 作上界(worker-间冗余记为未知)
    acc = accepted if accepted is not None else reported
    worker_internal = gen - reported                    # worker 自身 dedup 丢掉的
    worker_between = max(0, reported - acc)              # master 全局 dedup 丢掉的 = bitmap 新鲜度间隙
    redundant = worker_internal + worker_between
    out = os.path.join(prof_dir, "redundancy.csv")
    with open(out, "w") as f:
        f.write(f"# #10 重复求解拆分 (workers={nworkers}, accepted=master interesting_count)\n")
        f.write("# 漏斗: generated → reported(过 worker 自身 dedup) → accepted(过 master 全局 dedup)\n")
        f.write("stage,count,pct_of_generated\n")
        f.write(f"generated,{gen},100.0\n")
        f.write(f"reported(worker判新上报),{reported},{100*reported/gen:.1f}\n")
        f.write(f"accepted(master全局判新),{acc},{100*acc/gen:.1f}\n")
        f.write("\n# 冗余(generated-accepted)按发生位置拆分\n")
        f.write("where,count,pct_of_generated,pct_of_redundant\n")
        f.write(f"worker_internal(worker内自复),{worker_internal},"
                f"{100*worker_internal/gen:.1f},{100*worker_internal/max(1,redundant):.1f}\n")
        # worker_internal 中【字节完全相同】的一类：已由内容级预去重跳过 showmap(纯节省,不影响正确性)
        f.write(f"  └ 其中 byte_identical(已跳过showmap),{byte_dup},"
                f"{100*byte_dup/gen:.1f},{100*byte_dup/max(1,redundant):.1f}\n")
        f.write(f"worker_between(worker间/bitmap新鲜度间隙),{worker_between},"
                f"{100*worker_between/gen:.1f},{100*worker_between/max(1,redundant):.1f}\n")
        # 根因子拆分仅当全局快照可用时才有意义（tot_snap_none < tot_items）
        snap_ok = tot_items - tot_snap_none
        f.write("\n# 根因子拆分 (需 SymCC 运行前的全局位图快照)\n")
        f.write(f"# items={tot_items}, 有全局快照的 items={snap_ok}\n")
        if snap_ok > 0:
            f.write("cause,count\n")
            f.write(f"optimistic_infeasible(没打到任何全局新边),{infeasible}\n")
            f.write(f"freshness_within_worker(打到新边但本item内自复),{worker_fresh}\n")
        else:
            f.write("# 本次运行 worker 未获全局位图播种(每 worker 仅 1 个长 item,先于 master 写\n")
            f.write("# .shared_bitmap 完成),无法可靠区分 乐观求解不可行 vs 新鲜度间隙。\n")
            f.write("# 可测下界: worker_between 即 bitmap 新鲜度间隙(跨 worker)部分。\n")
    return out


def main() -> None:
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()

    # 高并行度下把本 rank 钉到编排层预留的核（与 AFL 自动绑核互斥），消除核争用/迁移
    _pin_self_to_reserved_core(rank)

    args = parse_args()

    if rank == 0:
        master(comm, args)
    else:
        worker(comm, args)

    comm.Barrier()
    MPI.Finalize()


if __name__ == "__main__":
    main()
