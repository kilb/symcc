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
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time

from mpi4py import MPI

# MPI tags
TAG_WORK = 1
TAG_RESULT = 2
TAG_STOP = 3
TAG_READY = 4

TIMEOUT_SEC = 10  # hybrid 模式下用短超时，快速轮转大量输入
SHOWMAP_TIMEOUT_MS = "5000"
STATS_INTERVAL_SEC = 60


def file_hash(path):
    """Return SHA-256 hash of file contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


class AflConfig:
    """AFL fuzzer configuration, read from fuzzer_stats."""

    def __init__(self, fuzzer_output_dir):
        self.queue = os.path.join(fuzzer_output_dir, "queue")
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
            self.target_command = parts[dash_idx:]  # includes '--'
        except ValueError:
            self.target_command = parts[-1:]

        self.use_stdin = "@@" not in self.target_command
        self.use_qemu = "-Q" in parts

    def best_new_testcases(self, seen, batch_size=None):
        """
        Return a list of unseen test cases from the AFL queue,
        sorted by priority (new coverage first, then seed-derived, then by size).
        """
        candidates = []
        if not os.path.isdir(self.queue):
            return candidates

        for fname in os.listdir(self.queue):
            fpath = os.path.join(self.queue, fname)
            if not os.path.isfile(fpath):
                continue
            if fpath in seen:
                continue

            # Score: (new_coverage, derived_from_seed, -file_size)
            has_cov = fname.endswith("+cov")
            from_seed = "orig:" in fname
            try:
                size = os.path.getsize(fpath)
            except OSError:
                size = 0
            candidates.append((has_cov, from_seed, -size, fpath))

        # Sort descending by score
        candidates.sort(reverse=True)
        paths = [c[3] for c in candidates]

        if batch_size is not None:
            return paths[:batch_size]
        return paths

    def run_showmap(self, testcase, bitmap_path):
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
        except Exception as e:
            print(f"[Master] afl-showmap error: {e}", file=sys.stderr)
            return "error", None


class CoverageBitmap:
    """使用边集合追踪覆盖率，merge 操作 O(新边数) 而非 O(bitmap大小)。"""

    def __init__(self):
        self.data = None
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

    def merge(self, new_data):
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
        interesting = bool(new_int & ~old_int)
        if interesting:
            merged = old_int | new_int
            self.data[:] = merged.to_bytes(len(self.data), 'little')
            # 更新边集合
            for i, b in enumerate(new_data):
                if b:
                    self.edges.add(i)
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
        return interesting


class Stats:
    """Execution statistics."""

    def __init__(self):
        self.total_count = 0
        self.total_time = 0.0
        self.failed_count = 0
        self.failed_time = 0.0
        self.generated_count = 0
        self.interesting_count = 0

    def add_execution(self, elapsed, killed):
        if killed:
            self.failed_count += 1
            self.failed_time += elapsed
        else:
            self.total_count += 1
            self.total_time += elapsed

    def log(self, f):
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


def run_symcc_worker(target_cmd, input_file, output_dir, timeout_sec, use_stdin,
                     base_env=None, afl_showmap=None, afl_target_cmd=None):
    """Run SymCC on a single input. Returns (new_tests_data, retcode, elapsed).

    如果提供了 afl_showmap 和 afl_target_cmd，会在 worker 端为每个输出
    运行 afl-showmap 收集 bitmap，这样 master 只需内存中比较 bitmap。
    """
    os.makedirs(output_dir, exist_ok=True)

    if base_env is not None:
        env = dict(base_env)  # 浅拷贝，避免修改调用方字典
    else:
        env = os.environ.copy()
    env["SYMCC_OUTPUT_DIR"] = output_dir
    env["SYMCC_ENABLE_LINEARIZATION"] = "1"

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
    except Exception as e:
        print(f"[Worker {MPI.COMM_WORLD.Get_rank()}] Error: {e}", file=sys.stderr)
        retcode = -1

    elapsed = time.monotonic() - start
    killed = retcode in (124, -9, 137)  # timeout codes

    # Collect test cases + 可选地在 worker 端收集 bitmap
    new_tests = []
    bitmap_path = os.path.join(output_dir, ".worker_bitmap")
    if os.path.isdir(output_dir):
        for fname in os.listdir(output_dir):
            if fname.startswith("."):
                continue
            fpath = os.path.join(output_dir, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "rb") as f:
                        content = f.read()
                    tc_entry = {"name": fname, "content": content}

                    # 在 worker 端收集 bitmap，避免 master 逐个 fork showmap
                    if afl_showmap and afl_target_cmd:
                        bm = _run_showmap_fast(
                            afl_showmap, afl_target_cmd, fpath, bitmap_path
                        )
                        if bm is not None:
                            tc_entry["bitmap"] = bm

                    new_tests.append(tc_entry)
                except (IOError, OSError):
                    pass

    return new_tests, retcode, elapsed, killed


def _run_showmap_fast(afl_showmap, target_cmd, testcase, bitmap_path):
    """运行 afl-showmap，返回稀疏边列表 [(edge_id, hit_count), ...] 或 None。

    使用文本模式输出 (不加 -b)，解析 "edge_id:count" 格式，
    只传输非零边（通常 ~500 条），避免序列化/反序列化 8MB bitmap。
    """
    cmd = [afl_showmap, "-t", "5000", "-m", "none",
           "-o", bitmap_path]
    for arg in target_cmd:
        if arg == "@@":
            cmd.append(str(testcase))
        else:
            cmd.append(arg)
    try:
        subprocess.run(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10
        )
        edges = []
        with open(bitmap_path, "r") as f:
            for line in f:
                line = line.strip()
                if ":" in line:
                    parts = line.split(":")
                    edge_id = int(parts[0])
                    count = int(parts[1])
                    edges.append((edge_id, count))
        return edges
    except Exception:
        return None


def master(comm, args):
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

    stats_file = open(os.path.join(symcc_dir, "stats"), "w")
    bitmap_path_triage = os.path.join(symcc_dir, ".triage_bitmap")

    # Load AFL config
    try:
        afl_config = AflConfig(afl_queue_dir)
    except Exception as e:
        print(f"Error loading AFL config: {e}", file=sys.stderr)
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
    active_workers = {}  # rank -> input_path
    queue_id = 0
    last_stats_time = time.monotonic()

    # SymCC 产生的有趣测试用例队列，会被重新分发给 workers
    symcc_feedback_queue: list[str] = []

    # --save-all: 保存所有生成的测试用例（不经过滤）
    save_all_dir = None
    save_all_id = 0
    if args.save_all:
        save_all_dir = args.save_all
        os.makedirs(save_all_dir, exist_ok=True)
        print(f"[Master] Saving all test cases to: {save_all_dir}")

    # 信号处理：收到 SIGTERM/SIGINT 时优雅退出
    shutdown_requested = False

    def _signal_handler(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True
        print(f"\n[Master] Received signal {signum}, shutting down...",
              file=sys.stderr, flush=True)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    while not shutdown_requested:
        # 合并输入源：SymCC 反馈用例优先，然后是 AFL queue 的新文件
        pending_feedback = list(symcc_feedback_queue)
        symcc_feedback_queue.clear()
        new_inputs = afl_config.best_new_testcases(
            processed_files, batch_size=num_workers * 2
        )
        work_queue = pending_feedback + new_inputs

        # Distribute work to ready workers
        dispatched = 0
        for input_file in work_queue:
            if not comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_READY):
                break

            status = MPI.Status()
            comm.recv(source=MPI.ANY_SOURCE, tag=TAG_READY, status=status)
            worker_rank = status.Get_source()

            # Send input content + cumulative bitmap + AFL target cmd to worker
            try:
                with open(input_file, "rb") as f:
                    content = f.read()
                comm.send({
                    "path": input_file,
                    "content": content,
                    "bitmap": bytes(coverage.data) if coverage.data else None,
                    "afl_target_cmd": afl_config.target_command,
                }, dest=worker_rank, tag=TAG_WORK)
                active_workers[worker_rank] = input_file
                processed_files.add(input_file)
                dispatched += 1
            except (IOError, OSError) as e:
                print(f"[Master] Error reading {input_file}: {e}",
                      file=sys.stderr)
                continue

        # 未分发完的 SymCC 反馈用例放回队列（AFL 输入不放回，下轮自然重取）
        undispatched_feedback = [
            f for f in pending_feedback
            if f not in processed_files
        ]
        symcc_feedback_queue.extend(undispatched_feedback)

        # Collect results from workers — 批量收集后批量 triage
        pending_results: list[tuple[int, str, list, int, float, bool]] = []
        while comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_RESULT):
            status = MPI.Status()
            result = comm.recv(source=MPI.ANY_SOURCE, tag=TAG_RESULT, status=status)
            worker_rank = status.Get_source()
            input_path = active_workers.pop(worker_rank, "unknown")
            pending_results.append((
                worker_rank, input_path,
                result.get("new_tests", []),
                result.get("retcode", 0),
                result.get("elapsed", 0),
                result.get("killed", False),
            ))

        # 批量 triage：先写所有临时文件到一个目录，然后一次 afl-showmap -C
        if pending_results:
            t_triage_start = time.monotonic()
            # 收集所有待 triage 的测试用例
            triage_dir = os.path.join(symcc_dir, ".triage_batch")
            os.makedirs(triage_dir, exist_ok=True)

            # tc_id -> (worker_rank, input_path, tc_content, bitmap_or_None)
            tc_map: dict[str, tuple[int, str, bytes, bytes | None]] = {}
            has_worker_bitmaps = 0
            for worker_rank, input_path, new_tests, retcode, elapsed, killed in pending_results:
                stats.add_execution(elapsed, killed)
                for tc in new_tests:
                    stats.generated_count += 1
                    tc_content = tc["content"]
                    tc_bitmap = tc.get("bitmap")
                    if tc_bitmap:
                        has_worker_bitmaps += 1
                    tc_id = hashlib.sha256(tc_content).hexdigest()[:16]

                    # --save-all
                    if save_all_dir is not None:
                        h = hashlib.sha256(tc_content).hexdigest()
                        save_path = os.path.join(save_all_dir, h)
                        if not os.path.exists(save_path):
                            try:
                                with open(save_path, "wb") as sf:
                                    sf.write(tc_content)
                                save_all_id += 1
                            except (IOError, OSError):
                                pass

                    # 写入 triage 目录（仅在无 worker bitmap 时需要）
                    if tc_id not in tc_map:
                        if not tc_bitmap:
                            tc_path = os.path.join(triage_dir, tc_id)
                            with open(tc_path, "wb") as f:
                                f.write(tc_content)
                        tc_map[tc_id] = (worker_rank, input_path, tc_content, tc_bitmap)

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

            # Triage：优先使用 worker 端收集的稀疏边列表（无需 fork showmap）
            num_interesting_total = 0
            for tc_id, (worker_rank, input_path, tc_content, tc_bitmap) in tc_map.items():
                if tc_bitmap is not None:
                    # 直接用 worker 提供的稀疏边列表做内存比较 — 极快
                    bitmap_data = tc_bitmap  # list[(edge_id, count)]
                    result_type = "success"
                else:
                    # 回退：master 端运行 showmap
                    tc_path = os.path.join(triage_dir, tc_id)
                    result_type, bitmap_data = afl_config.run_showmap(
                        tc_path, bitmap_path_triage
                    )
                if result_type == "success" and bitmap_data:
                    is_new = coverage.merge(bitmap_data)
                    if is_new:
                        orig_name = os.path.basename(input_path)
                        src_id = "000000"
                        if orig_name.startswith("id:") and len(orig_name) >= 9:
                            src_id = orig_name[3:9]
                        new_name = f"id:{queue_id:06d},src:{src_id}"
                        dest = os.path.join(queue_dir, new_name)
                        with open(dest, "wb") as f:
                            f.write(tc_content)
                        symcc_feedback_queue.append(dest)
                        if os.path.isdir(afl_sync_queue):
                            afl_dest = os.path.join(
                                afl_sync_queue,
                                f"id:symcc_{queue_id:06d},src:{src_id}"
                            )
                            try:
                                with open(afl_dest, "wb") as f:
                                    f.write(tc_content)
                            except OSError:
                                pass
                        queue_id += 1
                        num_interesting_total += 1
                        stats.interesting_count += 1
                elif result_type == "crash":
                    orig_name = os.path.basename(input_path)
                    src_id = "000000"
                    if orig_name.startswith("id:") and len(orig_name) >= 9:
                        src_id = orig_name[3:9]
                    crash_name = f"id:{queue_id:06d},src:{src_id}"
                    with open(os.path.join(crashes_dir, crash_name), "wb") as f:
                        f.write(tc_content)
                    queue_dest = os.path.join(queue_dir, crash_name)
                    with open(queue_dest, "wb") as f:
                        f.write(tc_content)
                    symcc_feedback_queue.append(queue_dest)
                    if os.path.isdir(afl_sync_queue):
                        try:
                            with open(os.path.join(
                                afl_sync_queue,
                                f"id:symcc_{queue_id:06d},src:{src_id}"
                            ), "wb") as f:
                                f.write(tc_content)
                        except OSError:
                            pass
                    queue_id += 1
                    num_interesting_total += 1
                    stats.interesting_count += 1

            # 清理 triage 目录
            shutil.rmtree(triage_dir, ignore_errors=True)

            triage_time = time.monotonic() - t_triage_start
            total_tcs = sum(len(r[2]) for r in pending_results)
            worker_info = ", ".join(
                f"W{r[0]}={len(r[2])}tc/{r[4]:.1f}s"
                for r in pending_results
            )
            print(f"[Master] Batch triage: {total_tcs} tc -> "
                  f"{num_interesting_total} interesting in {triage_time:.1f}s "
                  f"(w_bm={has_worker_bitmaps}) [{worker_info}]")

        # Periodic stats output
        if time.monotonic() - last_stats_time > STATS_INTERVAL_SEC:
            stats.log(stats_file)
            last_stats_time = time.monotonic()
            print(f"[Master] Stats: {stats.total_count} ok, "
                  f"{stats.failed_count} failed, "
                  f"{stats.interesting_count} interesting / "
                  f"{stats.generated_count} total")

        # 无输入且无活跃 worker 时等待 AFL 产生新用例
        if not work_queue and not active_workers and not symcc_feedback_queue:
            time.sleep(2)
        elif symcc_feedback_queue:
            pass  # 有反馈用例时立即分发
        else:
            time.sleep(0.05)

    # --- 优雅关闭 ---
    # 先输出最终统计（在尝试与 worker 通信之前，因为 worker 可能已被 SIGTERM 杀死）
    stats.log(stats_file)
    print(f"[Master] Final stats: {stats.total_count} ok, "
          f"{stats.failed_count} failed, "
          f"{stats.interesting_count} interesting / "
          f"{stats.generated_count} total")
    sys.stdout.flush()
    stats_file.close()

    # 尝试发送 TAG_STOP（worker 可能已经死了，忽略错误）
    print("[Master] Shutting down workers...")
    for rank in range(1, size):
        try:
            # 排空该 worker 的 TAG_READY 和 TAG_RESULT
            while comm.iprobe(source=rank, tag=TAG_READY):
                comm.recv(source=rank, tag=TAG_READY)
            while comm.iprobe(source=rank, tag=TAG_RESULT):
                comm.recv(source=rank, tag=TAG_RESULT)
            comm.send(None, dest=rank, tag=TAG_STOP)
        except Exception:
            pass  # worker 可能已经被 SIGTERM 杀死


def worker(comm, args):
    """Worker process: receives inputs, runs SymCC, sends back results."""
    rank = comm.Get_rank()
    target_cmd = args.target
    use_stdin = "@@" not in target_cmd

    worker_dir = tempfile.mkdtemp(prefix=f"symcc_mpi_w{rank}_")

    # 构建 worker 环境变量字典（不修改全局 os.environ）
    bitmap_file = os.path.join(worker_dir, "bitmap")
    worker_env = os.environ.copy()
    worker_env["SYMCC_AFL_COVERAGE_MAP"] = bitmap_file

    # 查找 afl-showmap 和 AFL target command（用于 worker 端 triage）
    afl_showmap = shutil.which("afl-showmap")
    afl_target_cmd = None

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

        input_content = msg["content"]

        # 从 master 获取 AFL target command（仅第一次）
        if afl_target_cmd is None and msg.get("afl_target_cmd"):
            afl_target_cmd = msg["afl_target_cmd"]

        # 将 master 发送的累积 bitmap 写入 SYMCC_AFL_COVERAGE_MAP
        bitmap_data = msg.get("bitmap")
        if bitmap_data:
            with open(bitmap_file, "wb") as bf:
                bf.write(bitmap_data)

        # Write input to local file
        local_input = os.path.join(worker_dir, "current_input")
        with open(local_input, "wb") as f:
            f.write(input_content)

        # Run SymCC
        run_output = os.path.join(worker_dir, f"output_{time.monotonic_ns()}")

        try:
            new_tests, retcode, elapsed, killed = run_symcc_worker(
                target_cmd, local_input, run_output, TIMEOUT_SEC, use_stdin,
                base_env=worker_env,
                afl_showmap=afl_showmap,
                afl_target_cmd=afl_target_cmd,
            )

            result = {
                "new_tests": new_tests,
                "retcode": retcode,
                "elapsed": elapsed,
                "killed": killed,
            }
        except Exception as e:
            print(f"[Worker {rank}] Error: {e}", file=sys.stderr)
            result = {
                "new_tests": [],
                "retcode": -1,
                "elapsed": 0,
                "killed": False,
            }

        # Clean up output
        shutil.rmtree(run_output, ignore_errors=True)

        # Send result
        comm.send(result, dest=0, tag=TAG_RESULT)

    shutil.rmtree(worker_dir, ignore_errors=True)


def parse_args():
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
    parser.add_argument("target", nargs=argparse.REMAINDER,
                        help="Target command (after '--')")

    args = parser.parse_args()

    if args.target and args.target[0] == "--":
        args.target = args.target[1:]

    if not args.target:
        parser.error("No target command. Use: -- TARGET [ARGS...]")

    return args


def main():
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()

    args = parse_args()

    if rank == 0:
        master(comm, args)
    else:
        worker(comm, args)

    comm.Barrier()
    MPI.Finalize()


if __name__ == "__main__":
    main()
