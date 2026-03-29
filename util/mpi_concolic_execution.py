#!/usr/bin/env python3
"""
MPI-parallel concolic execution driver for SymCC.

Uses a Master-Worker pattern with automatic multi-master scaling.
Workers write results directly to a shared directory and communicate
only hashes over MPI, minimizing serialization overhead.

Architecture auto-selection based on process count:
  np <= 46:  1 master  + (np-1) workers          (single-master)
  np > 46:   M masters + (np-M) workers           (multi-master)
             where M = ceil((np-1) / workers_per_master)

In multi-master mode, each master manages its own worker group via a
MPI sub-communicator (comm.Split). Masters periodically synchronize
discovered hashes so all groups explore the same frontier.

Usage:
    mpirun -np <N> python3 mpi_concolic_execution.py \
        -i INPUT_DIR [-o OUTPUT_DIR] [-t TIMEOUT] -- TARGET [ARGS...]

Requirements:
    - mpi4py  (pip install mpi4py)
    - An MPI implementation (OpenMPI, MPICH, etc.)
    - SymCC-instrumented target binary
"""

import argparse
import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque

from mpi4py import MPI

# --- 辅助函数 ---


def _atomic_write(dest: str, content: bytes) -> None:
    """原子写入文件：先写临时文件再 rename，避免并发写入导致数据损坏。"""
    tmp = dest + f".tmp.{os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            f.write(content)
        os.rename(tmp, dest)
    except OSError:
        # 清理临时文件（rename 失败时）
        try:
            os.unlink(tmp)
        except OSError:
            pass


# MPI message tags — group communicator (master <-> workers)
TAG_WORK = 1       # Master -> Worker: hash string of input to process
TAG_RESULT = 2     # Worker -> Master: dict with new hashes
TAG_STOP = 3       # Master -> Worker: shut down
TAG_READY = 4      # Worker -> Master: ready for work

# MPI message tags — global communicator (master <-> master)
TAG_HASH_SYNC = 10   # Sub-master -> Root: batch of new hashes
TAG_HASH_BCAST = 11  # Root -> Sub-masters: merged hash updates
TAG_MASTER_STATS = 12  # Sub-masters -> Root: final statistics


def compute_roles(comm_size, workers_per_master=45):
    """Auto-compute master/worker role assignment.

    Returns:
        master_ranks: sorted list of ranks acting as masters
        worker_groups: dict mapping master_rank -> [worker_ranks]
    """
    if comm_size <= 2:
        return [0], {0: list(range(1, comm_size))}

    num_avail = comm_size - 1  # rank 0 is always a master

    if num_avail <= workers_per_master:
        # Single master suffices
        return [0], {0: list(range(1, comm_size))}

    # Multiple masters needed
    num_masters = (num_avail + workers_per_master - 1) // workers_per_master
    # Each master needs at least 3 workers to be worthwhile
    num_masters = min(num_masters, num_avail // 3)
    num_masters = max(1, num_masters)

    master_ranks = list(range(num_masters))
    worker_ranks = list(range(num_masters, comm_size))

    # Round-robin assignment for even distribution
    groups = {m: [] for m in master_ranks}
    for i, w in enumerate(worker_ranks):
        m = master_ranks[i % num_masters]
        groups[m].append(w)

    return master_ranks, groups


def _simulate_mutations(input_file: str, output_dir: str,
                        num_mutations: int = 5) -> list[str]:
    """在模拟模式下，对输入文件进行随机变异生成测试用例。

    模拟 SymCC 的行为：读取输入，生成若干变异版本。
    用于 gcc 编译的二进制（无 SymCC 插桩）的框架性能测试。
    """
    try:
        with open(input_file, "rb") as f:
            data = f.read()
    except (IOError, OSError):
        return []

    if not data:
        return []

    os.makedirs(output_dir, exist_ok=True)
    new_tests = []

    for i in range(num_mutations):
        mutated = bytearray(data)
        # 随机变异 1~3 个字节
        num_bytes = random.randint(1, min(3, len(mutated)))
        for _ in range(num_bytes):
            pos = random.randint(0, len(mutated) - 1)
            mutated[pos] = random.randint(0, 255)

        out_path = os.path.join(output_dir, f"sim_{i:04d}")
        with open(out_path, "wb") as f:
            f.write(bytes(mutated))
        new_tests.append(out_path)

    return new_tests


def run_symcc(target_cmd, input_file, output_dir, timeout_sec, use_stdin,
              base_env=None, simulate=False):
    """
    Run the SymCC-instrumented target on the given input.

    When simulate=True, runs the target normally but generates synthetic
    mutations if no SymCC output is produced (for gcc-compiled binaries).

    Returns:
        (list_of_new_testcase_paths, return_code, elapsed_seconds)
    """
    os.makedirs(output_dir, exist_ok=True)

    if base_env is None:
        env = os.environ.copy()
        env["SYMCC_ENABLE_LINEARIZATION"] = "1"
    else:
        env = dict(base_env)  # 浅拷贝，避免修改调用方的字典
    env["SYMCC_OUTPUT_DIR"] = output_dir

    if use_stdin:
        cmd = ["timeout", "-k", "5", str(timeout_sec)] + target_cmd
    else:
        env["SYMCC_INPUT_FILE"] = str(input_file)
        cmd = ["timeout", "-k", "5", str(timeout_sec)] + [
            arg.replace("@@", str(input_file)) for arg in target_cmd
        ]

    start = time.monotonic()
    py_timeout = timeout_sec + 15  # Python-side backstop for hung processes
    try:
        if use_stdin:
            with open(input_file, "rb") as inf:
                proc = subprocess.run(
                    cmd, stdin=inf, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=env,
                    timeout=py_timeout
                )
        else:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env,
                timeout=py_timeout
            )
        retcode = proc.returncode
    except subprocess.TimeoutExpired:
        print(f"[Worker {MPI.COMM_WORLD.Get_rank()}] Python-side timeout "
              f"after {py_timeout}s", file=sys.stderr)
        retcode = -1
    except Exception as e:
        print(f"[Worker {MPI.COMM_WORLD.Get_rank()}] Error running SymCC: {e}",
              file=sys.stderr)
        retcode = -1

    elapsed = time.monotonic() - start

    new_tests = []
    if os.path.isdir(output_dir):
        for fname in os.listdir(output_dir):
            fpath = os.path.join(output_dir, fname)
            if os.path.isfile(fpath):
                new_tests.append(fpath)

    # 模拟模式：如果目标二进制没有产生 SymCC 输出，生成随机变异
    if simulate and not new_tests:
        new_tests = _simulate_mutations(input_file, output_dir)

    return new_tests, retcode, elapsed


def master_loop(global_comm, group_comm, args, peer_masters, is_root,
                shared_dir, master_ranks, worker_groups):
    """
    Master process main loop.

    Uses group_comm for worker communication (isolated per group via
    comm.Split) and global_comm for inter-master hash synchronization.

    Workers write test cases directly to shared_dir/{hash}. MPI messages
    carry only hash strings (~64 bytes each), not file contents.
    """
    rank = global_comm.Get_rank()
    num_workers = group_comm.Get_size() - 1  # subtract self

    if num_workers == 0:
        print(f"[Master {rank}] Error: no workers assigned.", file=sys.stderr)
        return

    os.makedirs(shared_dir, exist_ok=True)

    analyzed_hashes = set()   # all hashes we've ever seen (queued or processed)
    pending_queue = deque()   # deque of hash strings
    active_workers = {}       # group_rank -> hash being processed
    total_generated = 0
    total_interesting = 0

    # Multi-master hash sync state
    new_hashes_for_sync = []
    last_sync_time = time.monotonic()
    SYNC_INTERVAL = 2.0  # seconds between sync rounds
    pending_sends = []    # track isend requests to avoid GC

    imported_files = set()

    def import_inputs(src_dir):
        """Import seed files: write to shared_dir/{hash}, enqueue hash."""
        count = 0
        if not os.path.isdir(src_dir):
            return count
        for fname in sorted(os.listdir(src_dir)):
            if fname in imported_files:
                continue
            fpath = os.path.join(src_dir, fname)
            if not os.path.isfile(fpath):
                continue
            imported_files.add(fname)
            with open(fpath, "rb") as f:
                content = f.read()
            h = hashlib.sha256(content).hexdigest()
            if h not in analyzed_hashes:
                analyzed_hashes.add(h)
                # 原子写入 shared dir，避免多进程并发写入竞态
                dest = os.path.join(shared_dir, h)
                if not os.path.exists(dest):
                    _atomic_write(dest, content)
                pending_queue.append(h)
                count += 1
        return count

    def sync_hashes():
        """Non-blocking hash sync between masters via global_comm.

        Uses isend (non-blocking) for ALL inter-master sends to prevent
        the classic MPI deadlock where two processes both block in send()
        waiting for each other to recv().
        """
        nonlocal new_hashes_for_sync, last_sync_time

        # Clean up completed async sends
        pending_sends[:] = [r for r in pending_sends if not r.Test()]

        now = time.monotonic()
        if now - last_sync_time < SYNC_INTERVAL:
            return
        last_sync_time = now

        if not peer_masters:
            return

        if is_root:
            # Receive new hashes from sub-masters
            for peer in peer_masters:
                while global_comm.iprobe(source=peer, tag=TAG_HASH_SYNC):
                    incoming = global_comm.recv(source=peer,
                                               tag=TAG_HASH_SYNC)
                    novel = []
                    for h in incoming:
                        if h not in analyzed_hashes:
                            analyzed_hashes.add(h)
                            pending_queue.append(h)
                            novel.append(h)
                    # Forward novel hashes to OTHER sub-masters (async)
                    if novel:
                        for other in peer_masters:
                            if other != peer:
                                req = global_comm.isend(
                                    novel, dest=other,
                                    tag=TAG_HASH_BCAST)
                                pending_sends.append(req)

            # Broadcast root's own discoveries to all sub-masters (async)
            if new_hashes_for_sync:
                for peer in peer_masters:
                    req = global_comm.isend(
                        new_hashes_for_sync, dest=peer,
                        tag=TAG_HASH_BCAST)
                    pending_sends.append(req)
                new_hashes_for_sync = []
        else:
            # Sub-master: receive broadcasts from root FIRST
            # (receive before send to avoid deadlock pattern)
            while global_comm.iprobe(source=0, tag=TAG_HASH_BCAST):
                incoming = global_comm.recv(source=0, tag=TAG_HASH_BCAST)
                for h in incoming:
                    if h not in analyzed_hashes:
                        analyzed_hashes.add(h)
                        pending_queue.append(h)

            # Then send accumulated hashes to root (async)
            if new_hashes_for_sync:
                req = global_comm.isend(
                    new_hashes_for_sync, dest=0,
                    tag=TAG_HASH_SYNC)
                pending_sends.append(req)
                new_hashes_for_sync = []

    # --- Main loop setup ---
    imported = import_inputs(args.input_dir)
    initial_seed_count = imported  # track for final stats
    if is_root:
        total_masters = len(peer_masters) + 1
        print(f"[Master] Imported {imported} initial inputs from "
              f"{args.input_dir}")
        print(f"[Master] Using {total_masters} master(s), "
              f"{num_workers} workers in this group")

    idle_rounds = 0
    max_idle_rounds = max(1, args.max_idle // 5)
    wall_start = time.monotonic()
    wall_timeout = args.wall_timeout

    while True:
        # Check wall-clock time limit
        if wall_timeout > 0 and (time.monotonic() - wall_start) >= wall_timeout:
            if is_root:
                print(f"[Master] Wall-clock timeout ({wall_timeout}s) reached. "
                      f"Shutting down.")
            break

        # Multi-master hash sync
        sync_hashes()

        # Try to import new inputs (from external source like AFL)
        import_inputs(args.input_dir)

        # Distribute work to idle workers via group_comm
        while (pending_queue
               and ((wall_timeout - (time.monotonic() - wall_start))
                    if wall_timeout > 0 else float("inf")) > args.timeout + 5
               and group_comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_READY)):
            status = MPI.Status()
            group_comm.recv(source=MPI.ANY_SOURCE, tag=TAG_READY,
                            status=status)
            worker_group_rank = status.Get_source()

            h = pending_queue.popleft()
            # Send only the hash — worker reads content from shared_dir
            group_comm.send(h, dest=worker_group_rank, tag=TAG_WORK)
            active_workers[worker_group_rank] = h

        # Collect results from workers
        while group_comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_RESULT):
            status = MPI.Status()
            result = group_comm.recv(source=MPI.ANY_SOURCE, tag=TAG_RESULT,
                                     status=status)
            worker_group_rank = status.Get_source()

            new_hashes = result.get("new_hashes", [])
            retcode = result.get("retcode", 0)
            elapsed = result.get("elapsed", 0)
            num_gen = result.get("num_generated", len(new_hashes))
            input_hash = active_workers.pop(worker_group_rank, None)

            total_generated += num_gen
            num_new = 0
            for h in new_hashes:
                if h not in analyzed_hashes:
                    analyzed_hashes.add(h)
                    pending_queue.append(h)
                    new_hashes_for_sync.append(h)
                    num_new += 1
                    total_interesting += 1

            input_name = input_hash[:16] if input_hash else "unknown"
            print(f"[Master {rank}] Worker g{worker_group_rank} finished "
                  f"{input_name}... in {elapsed:.1f}s: {num_gen} generated, "
                  f"{num_new} new (ret={retcode})")

        # Check termination
        if not pending_queue and not active_workers:
            newly_imported = import_inputs(args.input_dir)
            if newly_imported > 0:
                idle_rounds = 0
                continue

            # Check if hash sync might bring new work
            if peer_masters:
                sync_hashes()
                if pending_queue:
                    idle_rounds = 0
                    continue

            idle_rounds += 1
            if idle_rounds >= max_idle_rounds:
                if is_root:
                    print(f"[Master] No more inputs after "
                          f"{max_idle_rounds * 5}s. Shutting down.")
                break

            if idle_rounds == 1 or idle_rounds % 6 == 0:
                print(f"[Master {rank}] Waiting for inputs... "
                      f"({total_generated} generated, "
                      f"{total_interesting} interesting, "
                      f"{len(analyzed_hashes)} analyzed)")
            time.sleep(5)
            continue
        else:
            idle_rounds = 0

        time.sleep(0.05)

    # --- Shutdown: stop all workers in this group ---
    group_size = group_comm.Get_size()
    for w_rank in range(1, group_size):
        while group_comm.iprobe(source=w_rank, tag=TAG_READY):
            group_comm.recv(source=w_rank, tag=TAG_READY)
        group_comm.send(None, dest=w_rank, tag=TAG_STOP)

    for w_rank in range(1, group_size):
        while group_comm.iprobe(source=w_rank, tag=TAG_RESULT):
            group_comm.recv(source=w_rank, tag=TAG_RESULT)

    # Phase 3: blocking recv for workers that were still active when we
    # sent TAG_STOP — they will send TAG_RESULT after finishing their
    # current SymCC run.
    for w_rank in list(active_workers.keys()):
        try:
            group_comm.recv(source=w_rank, tag=TAG_RESULT)
        except Exception:
            pass

    # --- Aggregate statistics across masters ---
    if peer_masters:
        if not is_root:
            # Sub-master: drain any pending broadcasts so root's
            # isends can complete, then send stats (blocking is safe
            # here because root is in recv loop below).
            while global_comm.iprobe(source=0, tag=TAG_HASH_BCAST):
                global_comm.recv(source=0, tag=TAG_HASH_BCAST)
            global_comm.send({
                "generated": total_generated,
                "interesting": total_interesting,
                "analyzed": len(analyzed_hashes),
            }, dest=0, tag=TAG_MASTER_STATS)
        else:
            # Root: collect stats from all sub-masters.
            # Use ANY_TAG to also drain late TAG_HASH_SYNC messages
            # that may still be in flight — otherwise recv(MASTER_STATS)
            # could deadlock if a sub-master's isend hasn't completed.
            received_from = set()
            while len(received_from) < len(peer_masters):
                status = MPI.Status()
                msg = global_comm.recv(source=MPI.ANY_SOURCE,
                                       tag=MPI.ANY_TAG, status=status)
                tag = status.Get_tag()
                src = status.Get_source()
                if tag == TAG_MASTER_STATS:
                    total_generated += msg["generated"]
                    total_interesting += msg["interesting"]
                    received_from.add(src)
                # TAG_HASH_SYNC: silently discard (shutting down)

    # --- Print final summary (root only) ---
    if is_root:
        # Count actual unique files in shared_dir (ground truth).
        # In multi-master mode, `total_interesting` is the sum of per-master
        # counts which double-counts hashes discovered independently by
        # multiple masters before sync propagation. The file count is the
        # true unique count since all test cases use content-addressable
        # naming (filename = SHA-256 hash, idempotent writes).
        # Subtract initial seeds so the count reflects only NEW test cases.
        actual_files = 0
        try:
            for fname in os.listdir(shared_dir):
                if os.path.isfile(os.path.join(shared_dir, fname)):
                    actual_files += 1
        except OSError:
            actual_files = total_interesting + initial_seed_count

        actual_unique = max(0, actual_files - initial_seed_count)

        total_masters = len(peer_masters) + 1
        total_workers_all = sum(
            len(worker_groups[m]) for m in master_ranks
        ) if peer_masters else num_workers

        wall_elapsed = time.monotonic() - wall_start
        throughput = total_generated / wall_elapsed if wall_elapsed > 0 else 0

        print("\n[Master] === Final Statistics ===")
        print(f"[Master] Total inputs analyzed:      {len(analyzed_hashes)}")
        print(f"[Master] Total test cases generated:  {total_generated}")
        print(f"[Master] New interesting test cases:  {actual_unique}")
        print(f"[Master] Throughput:                  {throughput:.1f} tc/s")
        print(f"[Master] Masters used:               {total_masters}")
        print(f"[Master] Workers used:               {total_workers_all}")


def worker_loop(group_comm, args, shared_dir):
    """
    Worker process main loop.

    Reads input from shared_dir/{hash}, runs SymCC, writes outputs
    directly to shared_dir/{hash}, sends only hashes to master.
    """
    group_rank = group_comm.Get_rank()
    global_rank = MPI.COMM_WORLD.Get_rank()
    MASTER = 0  # master is always rank 0 in group_comm

    target_cmd = args.target
    use_stdin = "@@" not in target_cmd
    timeout_sec = args.timeout

    worker_dir = tempfile.mkdtemp(prefix=f"symcc_worker_{global_rank}_")

    # Build env dict once and reuse
    worker_env = os.environ.copy()
    worker_env["SYMCC_ENABLE_LINEARIZATION"] = "1"

    while True:
        # Signal readiness to our master (in group_comm)
        group_comm.send(group_rank, dest=MASTER, tag=TAG_READY)

        # Wait for work or stop
        status = MPI.Status()
        msg = group_comm.recv(source=MASTER, tag=MPI.ANY_TAG, status=status)

        if status.Get_tag() == TAG_STOP:
            break

        if status.Get_tag() != TAG_WORK:
            continue

        input_hash = msg  # just a hash string

        # Read input from shared dir
        shared_path = os.path.join(shared_dir, input_hash)
        input_file = os.path.join(worker_dir, f"input_{input_hash}")
        try:
            shutil.copy2(shared_path, input_file)
        except (IOError, OSError) as e:
            print(f"[Worker {global_rank}] Cannot read {input_hash}: {e}",
                  file=sys.stderr)
            group_comm.send({
                "new_hashes": [], "retcode": -1,
                "elapsed": 0, "num_generated": 0,
            }, dest=MASTER, tag=TAG_RESULT)
            continue

        run_output = os.path.join(worker_dir, f"run_{time.monotonic_ns()}")

        try:
            new_tests, retcode, elapsed = run_symcc(
                target_cmd, input_file, run_output, timeout_sec, use_stdin,
                base_env=worker_env, simulate=args.simulate
            )

            # Hash each output, write to shared_dir, collect hashes
            new_hashes = []
            for tc in new_tests:
                try:
                    with open(tc, "rb") as f:
                        content = f.read()
                    h = hashlib.sha256(content).hexdigest()
                    new_hashes.append(h)
                    # 原子写入：避免多 worker 并发写入同一 hash 文件时损坏
                    dest = os.path.join(shared_dir, h)
                    if not os.path.exists(dest):
                        _atomic_write(dest, content)
                except (IOError, OSError):
                    pass

            result = {
                "new_hashes": new_hashes,
                "retcode": retcode,
                "elapsed": elapsed,
                "num_generated": len(new_tests),
            }

        except Exception as e:
            print(f"[Worker {global_rank}] Error: {e}", file=sys.stderr)
            result = {
                "new_hashes": [], "retcode": -1,
                "elapsed": 0, "num_generated": 0,
            }

        # Clean up run artifacts
        shutil.rmtree(run_output, ignore_errors=True)
        try:
            os.unlink(input_file)
        except OSError:
            pass

        # Send only hashes back (~64B each, not file content)
        group_comm.send(result, dest=MASTER, tag=TAG_RESULT)

    # Final cleanup
    shutil.rmtree(worker_dir, ignore_errors=True)


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="MPI-parallel concolic execution with SymCC",
        usage="mpirun -np <N> python3 %(prog)s -i INPUT_DIR [options] "
              "-- TARGET [ARGS...]",
    )
    parser.add_argument(
        "-i", "--input-dir", required=True,
        help="Directory containing initial seed inputs",
    )
    parser.add_argument(
        "-o", "--output-dir", default=None,
        help="Directory to store all generated test cases",
    )
    parser.add_argument(
        "-f", "--failed-dir", default=None,
        help="Directory to store failing test cases",
    )
    parser.add_argument(
        "-t", "--timeout", type=int, default=90,
        help="Timeout in seconds per SymCC execution (default: 90)",
    )
    parser.add_argument(
        "--max-idle", type=int, default=60,
        help="Seconds to wait without new inputs before stopping (default: 60)",
    )
    parser.add_argument(
        "--wall-timeout", type=int, default=0,
        help="Total wall-clock time limit in seconds (0=unlimited, default: 0)",
    )
    parser.add_argument(
        "--workers-per-master", type=int, default=45,
        help="Target workers per master for auto-scaling (default: 45). "
             "With 160 processes and default 45, creates 4 masters.",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Simulation mode: generate random mutations when target produces "
             "no SymCC output (for gcc-compiled binaries)",
    )
    parser.add_argument(
        "target", nargs=argparse.REMAINDER,
        help="Target command (after '--')",
    )

    args = parser.parse_args()

    # Strip leading '--' from target
    if args.target and args.target[0] == "--":
        args.target = args.target[1:]

    if not args.target:
        parser.error("No target command specified. Use: -- TARGET [ARGS...]")

    return args


def main():
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    size = comm.Get_size()

    args = parse_args()

    # --- Auto-compute role assignment ---
    master_ranks, worker_groups = compute_roles(
        size, args.workers_per_master
    )
    master_set = set(master_ranks)

    # Find each process's group master
    my_master_rank = None
    if rank in master_set:
        my_master_rank = rank
    else:
        for m, workers in worker_groups.items():
            if rank in workers:
                my_master_rank = m
                break

    # Create isolated group communicator: each master + its workers.
    # Within group_comm, the master has rank 0 (lowest global rank in group).
    # This prevents cross-group message interference.
    group_comm = comm.Split(my_master_rank, rank)

    # Determine shared directory (all ranks must agree on the path)
    if args.output_dir:
        shared_dir = os.path.abspath(args.output_dir)
    else:
        # Create temp dir on rank 0 and broadcast to all
        if rank == 0:
            shared_dir = tempfile.mkdtemp(prefix="symcc_shared_")
        else:
            shared_dir = None
        shared_dir = comm.bcast(shared_dir, root=0)

    os.makedirs(shared_dir, exist_ok=True)

    # Print configuration (root only)
    if rank == 0:
        print("SymCC MPI Parallel Concolic Execution")
        print(f"  Processes:     {size}")
        num_masters = len(master_ranks)
        if num_masters == 1:
            print(f"  Mode:          single-master "
                  f"({size - 1} workers)")
        else:
            print(f"  Mode:          multi-master "
                  f"({num_masters} masters, auto-scaled at "
                  f"{args.workers_per_master} workers/master)")
            for m in master_ranks:
                print(f"    Master {m}: {len(worker_groups[m])} workers")
        print(f"  Shared dir:    {shared_dir}")
        print(f"  Input dir:     {args.input_dir}")
        print(f"  Target:        {' '.join(args.target)}")
        print(f"  Timeout:       {args.timeout}s per execution")
        if args.wall_timeout > 0:
            print(f"  Wall timeout:  {args.wall_timeout}s total")
        print()

    # --- Run ---
    if rank in master_set:
        is_root = (rank == 0)
        peer_masters = [m for m in master_ranks if m != rank]
        master_loop(comm, group_comm, args, peer_masters, is_root, shared_dir,
                    master_ranks, worker_groups)
    else:
        worker_loop(group_comm, args, shared_dir)

    # --- Cleanup ---
    group_comm.Free()
    comm.Barrier()

    if rank == 0 and not args.output_dir:
        shutil.rmtree(shared_dir, ignore_errors=True)

    MPI.Finalize()


if __name__ == "__main__":
    main()
