#!/usr/bin/env python3
"""
MPI-parallel concolic execution driver for SymCC.

Uses a Master-Worker pattern to distribute test inputs across multiple
MPI processes, each running SymCC independently.

Usage:
    mpirun -np <N> python3 mpi_concolic_execution.py \
        -i INPUT_DIR [-o OUTPUT_DIR] [-f FAILED_DIR] [-t TIMEOUT] -- TARGET [ARGS...]

The master process (rank 0) manages the input queue and deduplicates
generated test cases. Worker processes (ranks 1..N-1) each run SymCC on
assigned inputs and return newly generated test cases.

TARGET may contain '@@', which is replaced with the path of the current
input file. If '@@' is absent, the input is fed via stdin.

Requirements:
    - mpi4py  (pip install mpi4py)
    - An MPI implementation (OpenMPI, MPICH, etc.)
    - SymCC-instrumented target binary

Example:
    mpirun -np 8 python3 mpi_concolic_execution.py \
        -i ./seeds -o ./corpus -t 90 -- ./target_symcc @@
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque

from mpi4py import MPI

# MPI message tags
TAG_WORK = 1       # Master -> Worker: here is an input to process
TAG_RESULT = 2     # Worker -> Master: here are the generated test cases
TAG_STOP = 3       # Master -> Worker: no more work, shut down
TAG_READY = 4      # Worker -> Master: I'm ready for work



def run_symcc(target_cmd, input_file, output_dir, timeout_sec, use_stdin,
              base_env=None):
    """
    Run the SymCC-instrumented target on the given input.

    Args:
        base_env: Optional pre-built env dict to reuse (avoids os.environ.copy()
                  on every call). SYMCC_OUTPUT_DIR and SYMCC_INPUT_FILE are
                  updated in-place per invocation.

    Returns:
        (list_of_new_testcases, return_code, elapsed_seconds)
    """
    os.makedirs(output_dir, exist_ok=True)

    if base_env is None:
        env = os.environ.copy()
        env["SYMCC_ENABLE_LINEARIZATION"] = "1"
    else:
        env = base_env
    env["SYMCC_OUTPUT_DIR"] = output_dir

    if use_stdin:
        cmd = ["timeout", "-k", "5", str(timeout_sec)] + target_cmd
    else:
        env["SYMCC_INPUT_FILE"] = str(input_file)
        cmd = ["timeout", "-k", "5", str(timeout_sec)] + [
            arg.replace("@@", str(input_file)) for arg in target_cmd
        ]

    start = time.monotonic()
    try:
        if use_stdin:
            with open(input_file, "rb") as inf:
                proc = subprocess.run(
                    cmd, stdin=inf, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=env
                )
        else:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env
            )
        retcode = proc.returncode
    except Exception as e:
        print(f"[Worker {MPI.COMM_WORLD.Get_rank()}] Error running SymCC: {e}",
              file=sys.stderr)
        retcode = -1

    elapsed = time.monotonic() - start

    # Collect generated test cases
    new_tests = []
    if os.path.isdir(output_dir):
        for fname in os.listdir(output_dir):
            fpath = os.path.join(output_dir, fname)
            if os.path.isfile(fpath):
                new_tests.append(fpath)

    return new_tests, retcode, elapsed


def master(comm, args):
    """
    Master process (rank 0).

    Manages the input queue, distributes work to workers, collects and
    deduplicates results.
    """
    size = comm.Get_size()
    num_workers = size - 1

    if num_workers == 0:
        print("Error: need at least 2 MPI processes (1 master + 1 worker).",
              file=sys.stderr)
        # Send stop to self (won't happen, but for safety)
        return

    # Setup directories
    spool_dir = tempfile.mkdtemp(prefix="symcc_mpi_spool_")
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
    if args.failed_dir:
        os.makedirs(args.failed_dir, exist_ok=True)

    analyzed_hashes = set()   # SHA-256 hashes of inputs we already processed
    pending_queue = deque()   # deque of (hash, content_bytes) waiting to be processed
    active_workers = {}       # rank -> hash string being processed
    total_generated = 0
    total_interesting = 0
    # Memory management: spill pending queue items to disk when queue is large
    QUEUE_MEM_LIMIT = 10000   # keep at most this many items in memory

    def enqueue(h, content):
        """Add item to pending queue, spilling to disk if queue is large."""
        if len(pending_queue) < QUEUE_MEM_LIMIT:
            pending_queue.append((h, content))
        else:
            # Spill to spool dir — store content on disk, queue only the hash
            spool_path = os.path.join(spool_dir, h)
            with open(spool_path, "wb") as f:
                f.write(content)
            pending_queue.append((h, None))  # None = spilled to disk

    def dequeue():
        """Pop from pending queue, reloading from disk if spilled."""
        h, content = pending_queue.popleft()
        if content is None:
            # Reload from spool
            spool_path = os.path.join(spool_dir, h)
            with open(spool_path, "rb") as f:
                content = f.read()
            try:
                os.unlink(spool_path)
            except OSError:
                pass
        return h, content

    imported_files = set()  # track filenames already imported

    def import_inputs(src_dir):
        """Import new inputs from a directory, reading content into memory."""
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
                enqueue(h, content)
                count += 1
        return count

    # Import initial inputs
    imported = import_inputs(args.input_dir)
    print(f"[Master] Imported {imported} initial inputs from {args.input_dir}")
    print(f"[Master] Using {num_workers} worker processes")

    idle_rounds = 0
    max_idle_secs = args.max_idle
    max_idle_rounds = max(1, max_idle_secs // 5)
    wall_start = time.monotonic()
    wall_timeout = args.wall_timeout

    while True:
        # Check wall-clock time limit
        if wall_timeout > 0 and (time.monotonic() - wall_start) >= wall_timeout:
            print(f"[Master] Wall-clock timeout ({wall_timeout}s) reached. "
                  f"Shutting down.")
            break

        # Try to import new inputs (from external source)
        import_inputs(args.input_dir)

        # Distribute work to idle workers (skip if nearing wall timeout)
        wall_remaining = (wall_timeout - (time.monotonic() - wall_start)
                          if wall_timeout > 0 else float("inf"))
        while (pending_queue and wall_remaining > args.timeout + 5
               and comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_READY)):
            status = MPI.Status()
            comm.recv(source=MPI.ANY_SOURCE, tag=TAG_READY, status=status)
            worker_rank = status.Get_source()

            item = dequeue()
            # Send (hash, content) to worker — worker writes to local temp file
            comm.send(item, dest=worker_rank, tag=TAG_WORK)
            active_workers[worker_rank] = item[0]  # store hash

        # Check for results from workers
        while comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_RESULT):
            status = MPI.Status()
            result = comm.recv(source=MPI.ANY_SOURCE, tag=TAG_RESULT, status=status)
            worker_rank = status.Get_source()

            new_tests = result.get("new_tests", [])
            retcode = result.get("retcode", 0)
            elapsed = result.get("elapsed", 0)
            input_hash = active_workers.pop(worker_rank, None)

            # Mark as analyzed
            if input_hash:
                analyzed_hashes.add(input_hash)

            # Process new test cases
            num_new = 0
            for tc_data in new_tests:
                total_generated += 1
                tc_content = tc_data["content"]

                # Use pre-computed hash from worker (avoids rehashing on master)
                h = tc_data.get("hash") or hashlib.sha256(tc_content).hexdigest()
                if h not in analyzed_hashes:
                    analyzed_hashes.add(h)
                    enqueue(h, tc_content)
                    num_new += 1
                    total_interesting += 1

                    # Write to output dir only (skip next_dir to halve disk writes)
                    if args.output_dir:
                        out_dest = os.path.join(args.output_dir, h)
                        with open(out_dest, "wb") as f:
                            f.write(tc_content)

            input_name = input_hash[:16] if input_hash else "unknown"
            print(f"[Master] Worker {worker_rank} finished {input_name}... "
                  f"in {elapsed:.1f}s: {len(new_tests)} generated, "
                  f"{num_new} new (ret={retcode})")

        # If no work pending and no active workers, check if we should stop
        if not pending_queue and not active_workers:
            # Try importing one more time
            newly_imported = import_inputs(args.input_dir)
            if newly_imported > 0:
                idle_rounds = 0
                continue

            idle_rounds += 1
            if idle_rounds >= max_idle_rounds:
                print(f"[Master] No more inputs after {max_idle_rounds * 5}s. "
                      f"Shutting down.")
                break

            if idle_rounds == 1 or idle_rounds % 6 == 0:
                print(f"[Master] Waiting for new inputs... "
                      f"(total: {total_generated} generated, "
                      f"{total_interesting} interesting, "
                      f"{len(analyzed_hashes)} analyzed)")
            time.sleep(5)
            continue
        else:
            idle_rounds = 0

        # Small sleep to avoid busy-waiting
        time.sleep(0.05)

    # Send stop signal to all workers
    print(f"[Master] Sending stop signals to {num_workers} workers...")
    for rank in range(1, size):
        # Drain any pending READY messages first
        while comm.iprobe(source=rank, tag=TAG_READY):
            comm.recv(source=rank, tag=TAG_READY)
        comm.send(None, dest=rank, tag=TAG_STOP)

    # Drain any remaining results
    for rank in range(1, size):
        while comm.iprobe(source=rank, tag=TAG_RESULT):
            comm.recv(source=rank, tag=TAG_RESULT)

    # Summary
    print(f"\n[Master] === Final Statistics ===")
    print(f"[Master] Total inputs analyzed:    {len(analyzed_hashes)}")
    print(f"[Master] Total test cases generated: {total_generated}")
    print(f"[Master] New interesting test cases: {total_interesting}")
    print(f"[Master] Workers used:             {num_workers}")

    # Cleanup spool directory
    shutil.rmtree(spool_dir, ignore_errors=True)


def worker(comm, args):
    """
    Worker process (ranks 1..N-1).

    Receives input files from master, runs SymCC, and returns results.
    """
    rank = comm.Get_rank()
    target_cmd = args.target
    use_stdin = "@@" not in target_cmd
    timeout_sec = args.timeout

    # Create a persistent temp directory for this worker
    worker_dir = tempfile.mkdtemp(prefix=f"symcc_worker_{rank}_")

    # Build env dict once and reuse across all runs (avoids os.environ.copy() per run)
    worker_env = os.environ.copy()
    worker_env["SYMCC_ENABLE_LINEARIZATION"] = "1"

    while True:
        # Signal readiness
        comm.send(rank, dest=0, tag=TAG_READY)

        # Wait for work or stop signal
        status = MPI.Status()
        msg = comm.recv(source=0, tag=MPI.ANY_TAG, status=status)

        if status.Get_tag() == TAG_STOP:
            break

        if status.Get_tag() != TAG_WORK:
            continue

        input_hash, input_content = msg

        # Write input content to a local temp file for SymCC
        input_file = os.path.join(worker_dir, f"input_{input_hash}")
        with open(input_file, "wb") as f:
            f.write(input_content)

        # Create a unique output directory for this run
        run_output = os.path.join(worker_dir, f"run_{time.monotonic_ns()}")

        try:
            new_tests, retcode, elapsed = run_symcc(
                target_cmd, input_file, run_output, timeout_sec, use_stdin,
                base_env=worker_env
            )

            # Read test case contents and pre-compute hashes to send back.
            # Computing hashes on the worker side offloads CPU from the
            # single-threaded master, which is the throughput bottleneck.
            test_data = []
            for tc in new_tests:
                try:
                    with open(tc, "rb") as f:
                        content = f.read()
                    test_data.append({
                        "path": os.path.basename(tc),
                        "content": content,
                        "hash": hashlib.sha256(content).hexdigest(),
                    })
                except (IOError, OSError):
                    pass

            result = {
                "new_tests": test_data,
                "retcode": retcode,
                "elapsed": elapsed,
            }

        except Exception as e:
            print(f"[Worker {rank}] Error: {e}", file=sys.stderr)
            result = {
                "new_tests": [],
                "retcode": -1,
                "elapsed": 0,
            }

        # Clean up run output directory and input file
        shutil.rmtree(run_output, ignore_errors=True)
        try:
            os.unlink(input_file)
        except OSError:
            pass

        # Send results to master
        comm.send(result, dest=0, tag=TAG_RESULT)

    # Cleanup
    shutil.rmtree(worker_dir, ignore_errors=True)


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="MPI-parallel concolic execution with SymCC",
        usage="mpirun -np <N> python3 %(prog)s -i INPUT_DIR [options] -- TARGET [ARGS...]",
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

    args = parse_args()

    if rank == 0:
        print(f"SymCC MPI Parallel Concolic Execution")
        print(f"  Processes: {comm.Get_size()} (1 master + {comm.Get_size()-1} workers)")
        print(f"  Input dir: {args.input_dir}")
        print(f"  Output dir: {args.output_dir or '(none)'}")
        print(f"  Target: {' '.join(args.target)}")
        print(f"  Timeout: {args.timeout}s per execution")
        if args.wall_timeout > 0:
            print(f"  Wall timeout: {args.wall_timeout}s total")
        print()
        master(comm, args)
    else:
        worker(comm, args)

    MPI.Finalize()


if __name__ == "__main__":
    main()
