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
TAG_BITMAP_VERSION = 5  # master 通知 workers bitmap 已更新

TIMEOUT_SEC = int(os.environ.get("SYMCC_TIMEOUT", "30"))  # SymCC 执行超时（秒），默认 30s
SHOWMAP_TIMEOUT_MS = "5000"
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
                           edge_yield: dict[str, float] | None = None
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

                new_candidates.append((score, attrs["name"], fpath))
        except OSError:
            return []

        new_candidates.sort(key=lambda c: -c[0])
        paths = [c[2] for c in new_candidates]

        if batch_size is not None:
            return paths[:batch_size]
        return paths

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
        """合并稀疏边列表 [(edge_id, hit_count), ...]。极快。"""
        interesting = False
        for edge_id, hit in edges:
            if edge_id not in self.edges:
                interesting = True
                self.edges.add(edge_id)
                if self.data and edge_id < len(self.data):
                    self.data[edge_id] |= hit
            elif self.data and edge_id < len(self.data):
                old = self.data[edge_id]
                if old | hit != old:
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
                     save_dir: str | None = None
                     ) -> "tuple[list[dict], int, int, float, bool]":
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
    env["SYMCC_EMIT_HINTS"] = "1"  # 输出约束 hint 文件

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

    # Collect test cases + worker 端 coverage dedup
    # Worker 有 master bitmap 副本，在本地做 coverage merge
    # 只传 interesting 的 TC（~3% 的输出），消息 323KB → 10KB
    new_tests = []
    total_generated = 0
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

        for entry in entries:
            if entry.name.startswith(".") or entry.name.endswith(".hints") or not entry.is_file():
                continue
            total_generated += 1
            try:
                with open(entry.path, "rb") as f:
                    content = f.read()

                # Worker 端 streaming showmap + coverage dedup
                if streaming_showmap is not None and worker_coverage is not None:
                    edges = streaming_showmap.get_edges(content)
                    if edges is not None:
                        is_new = worker_coverage.merge(edges)
                        if is_new:
                            tc_entry = {
                                "content": content,
                                "bitmap": edges,
                            }
                            # 附加约束 hint 信息
                            if entry.name in hint_map:
                                tc_entry["hints"] = hint_map[entry.name]
                            new_tests.append(tc_entry)
                    # 不 interesting 的直接跳过，不传
                else:
                    tc_entry = {"content": content}
                    if entry.name in hint_map:
                        tc_entry["hints"] = hint_map[entry.name]
                    new_tests.append(tc_entry)
            except (IOError, OSError):
                pass

    return new_tests, total_generated, retcode, elapsed, killed


class StreamingShowmap:
    """afl-showmap -S 流式模式封装。

    维持持久 fork server，通过 stdin/stdout 管道传输测试用例。
    每次调用 ~0.6ms（vs fork 模式 ~12ms，19x 加速）。
    """

    _MAX_EDGES = 1 << 20   # edge count 上限，防止损坏 count 导致超长循环

    def __init__(self, afl_showmap: str, target_cmd: list[str]):
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
        edges = [(struct.unpack_from("<I", pair, i * 5)[0], pair[i * 5 + 4])
                 for i in range(edges_count)]
        for _ in range(2):                                    # 排空 stdout/stderr
            lraw = self._read_exact(4)
            if lraw is None:
                return None
            blen = struct.unpack("<I", lraw)[0]
            if blen and self._read_exact(blen) is None:
                return None
        return edges

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

    def __del__(self):
        self.close()



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

            # --save-all
            if save_all_dir is not None:
                h = hashlib.sha256(tc_content).hexdigest()
                save_path = os.path.join(save_all_dir, h)
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
                tc_id = hashlib.sha256(tc_content).hexdigest()[:16]
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
                    with open(_dtmp, "wb") as f:
                        f.write(tc_content)
                    os.replace(_dtmp, dest)
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
                shutil.copy2(input_path, os.path.join(hangs_dir, hang_name))
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
                shutil.copy2(input_path, os.path.join(crashes_dir, crash_name))
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

    # AFL extras 目录：约束 hint 写入此处，AFL 自动作为字典 token 使用
    afl_extras_dir = os.path.join(afl_queue_dir, "..", "extras")
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

    # 信号处理：收到 SIGTERM/SIGINT 时优雅退出
    shutdown_requested = False

    def _signal_handler(signum: int, frame: object) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
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
                new_inputs = afl_config.best_new_testcases(
                    processed_files, batch_size=max_active_workers * 4,
                    analyzed_hashes=processed_content_hashes,
                    edge_yield=_get_edge_yield(),
                )
                last_scan_time = _now
                # AFL 种子代数为 0
                for inp in new_inputs:
                    if inp not in file_generation:
                        file_generation[inp] = 0
                if _prof:
                    _t_scan += time.monotonic() - _t0
                    _n_scan += 1

            work_queue = pending_feedback + new_inputs

            # 交替处理 READY 和 RESULT 消息，避免单方向阻塞
            work_idx = 0
            any_progress = True

            def _dispatch_to(wr: int, input_file: str) -> None:
                # 只发路径 + bitmap 版本号，不发内容（worker 自己读文件）
                comm.send({
                    "path": input_file,
                    "bitmap_version": bitmap_version,
                    "bitmap_path": bitmap_shared_path,
                    "focus_bytes": focus_bytes_str,
                }, dest=wr, tag=TAG_WORK)
                active_workers[wr] = input_file
                processed_files.add(input_file)
                try:
                    with open(input_file, "rb") as _f:
                        processed_content_hashes.add(
                            hashlib.sha256(_f.read()).hexdigest())
                except (IOError, OSError):
                    pass

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

            # 未分发完的 SymCC 反馈用例放回队列（保留代数信息）
            for f in pending_feedback:
                if f not in processed_files:
                    gen = file_generation.get(f, 0)
                    symcc_feedback_queue.append((f, gen))

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
                time.sleep(0.05)
            if _prof:
                _t_idle += time.monotonic() - _t0
    finally:
        # --- 优雅关闭 ---
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
    current_bitmap_version = -1

    while True:
        # Signal ready
        comm.send(rank, dest=0, tag=TAG_READY)

        # Wait for work or stop
        status = MPI.Status()
        msg = comm.recv(source=0, tag=MPI.ANY_TAG, status=status)

        if status.Get_tag() == TAG_STOP:
            break

        if status.Get_tag() != TAG_WORK:
            continue

        input_path = msg["path"]
        bm_version = msg.get("bitmap_version", 0)

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
                    worker_cov.edges.update(
                        i for i, b in enumerate(_bm_data) if b)
                except (IOError, OSError):
                    pass

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
        try:
            shutil.copy2(input_path, local_input)
        except (IOError, OSError):
            # 文件可能被 AFL 删除，跳过
            result = {"new_tests": [], "retcode": -1, "elapsed": 0, "killed": False}
            comm.send(result, dest=0, tag=TAG_RESULT)
            continue

        # 选择性符号化：如果 Master 指定了关注字节范围，传递给 SymCC
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
            new_tests, total_gen, retcode, elapsed, killed = run_symcc_worker(
                target_cmd, local_input, run_output, TIMEOUT_SEC, use_stdin,
                base_env=worker_env,
                streaming_showmap=streaming_sm,
                worker_coverage=worker_cov,
            )

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
        comm.send(result, dest=0, tag=TAG_RESULT)

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
