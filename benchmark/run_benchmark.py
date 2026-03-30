#!/usr/bin/env python3
"""
SymCC MPI Parallelization Benchmark Suite

Tests the MPI-parallel concolic execution at different parallelism levels
and generates a comparison report.

Usage:
    python3 run_benchmark.py [options]

Options:
    --symcc PATH     Path to SymCC compiler (default: symcc in PATH)
    --np-list LIST   Comma-separated list of process counts (default: 1,2,4,8)
    --targets LIST   Comma-separated target names (default: all)
    --rounds N       Rounds per configuration (default: 3)
    --timeout T      Per-execution timeout in seconds (default: 60)
    --output DIR     Output directory for results (default: benchmark_results)
    --skip-build     Skip compilation step (use existing binaries)

The script:
  1. Compiles target programs with SymCC (or gcc for simulation mode)
  2. Runs the serial baseline (pure_concolic_execution.sh)
  3. Runs MPI-parallel at each configured parallelism level
  4. Measures: wall-clock time, test cases generated, unique paths found
  5. Generates a comparison report
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
SYMCC_ROOT = SCRIPT_DIR.parent
TARGETS_DIR = SCRIPT_DIR / "targets"
SEEDS_DIR = SCRIPT_DIR / "seeds"
MPI_SCRIPT = SYMCC_ROOT / "util" / "mpi_concolic_execution.py"
MPI_FUZZING_SCRIPT = SYMCC_ROOT / "util" / "mpi_fuzzing_helper.py"
SERIAL_SCRIPT = SYMCC_ROOT / "util" / "pure_concolic_execution.sh"

# Target configs: name -> (source, input_len, seed_prefix, uses_file_arg)
TARGETS = {
    "maze":          ("maze.c",          16, "maze_",    True),
    "parser":        ("parser.c",        32, "parser_",  True),
    "deep_branches": ("deep_branches.c",  8, "deep_",    True),
    "crypto_check":  ("crypto_check.c",  16, "crypto_",  True),
}

# Public benchmark targets (set up via setup_public_benchmarks.sh)
# Format: name -> (binary_path_relative_to_public_dir, seed_dir, args_template)
# These are populated at runtime via --public flag
PUBLIC_DIR = SCRIPT_DIR / "public"


MIN_SYMCC_SYMBOLS = 5  # threshold to consider a binary SymCC-instrumented


def _has_symcc_instrumentation(binary_path):
    """Check if a binary contains SymCC instrumentation symbols."""
    try:
        result = subprocess.run(
            ["nm", binary_path], capture_output=True, text=True, timeout=10
        )
        # SymCC 插桩会插入 __sym_ctor 符号
        count = sum(1 for line in result.stdout.splitlines()
                    if "__sym_ctor" in line or "_sym_build" in line
                    or "SymExpr" in line)
        return count >= MIN_SYMCC_SYMBOLS
    except Exception:
        return True  # assume instrumented if we can't check


def _resolve_path(p):
    """Resolve a path: try absolute, then relative to cwd, then relative to SCRIPT_DIR."""
    p = str(p)
    if os.path.isabs(p):
        return p
    # Try relative to cwd
    abs_cwd = os.path.abspath(p)
    if os.path.exists(abs_cwd):
        return abs_cwd
    # Try relative to benchmark/ (SCRIPT_DIR)
    abs_script = os.path.abspath(os.path.join(str(SCRIPT_DIR), p))
    if os.path.exists(abs_script):
        return abs_script
    # Try relative to project root (SYMCC_ROOT)
    abs_root = os.path.abspath(os.path.join(str(SYMCC_ROOT), p))
    if os.path.exists(abs_root):
        return abs_root
    # Fall back to cwd-relative (will fail later with a clear error)
    return abs_cwd


def run_cmd(cmd, timeout=300, capture=True):
    """Run a command and return (returncode, stdout, stderr, elapsed)."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd, capture_output=capture, text=True, timeout=timeout
        )
        elapsed = time.monotonic() - start
        if capture:
            return result.returncode, result.stdout, result.stderr, elapsed
        return result.returncode, "", "", elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return -1, "", "TIMEOUT", elapsed


def find_symcc():
    """Find the SymCC compiler."""
    for candidate in ["symcc", str(SYMCC_ROOT / "build" / "symcc")]:
        if shutil.which(candidate):
            return candidate
    return None


def build_targets(compiler, output_dir):
    """Compile all target programs."""
    binaries = {}
    os.makedirs(output_dir, exist_ok=True)

    for name, (source, _, _, _) in TARGETS.items():
        src_path = TARGETS_DIR / source
        bin_path = Path(output_dir) / f"{name}_symcc"

        print(f"  Compiling {name}... ", end="", flush=True)
        ret, _, stderr, elapsed = run_cmd(
            [compiler, "-O2", str(src_path), "-o", str(bin_path)]
        )
        if ret == 0:
            print(f"OK ({elapsed:.1f}s)")
            binaries[name] = str(bin_path)
        else:
            print(f"FAILED (ret={ret})")
            if stderr:
                print(f"    {stderr[:200]}")
    return binaries


def build_targets_gcc(output_dir):
    """Compile with gcc as fallback (for simulation/framework testing)."""
    binaries = {}
    os.makedirs(output_dir, exist_ok=True)

    for name, (source, _, _, _) in TARGETS.items():
        src_path = TARGETS_DIR / source
        bin_path = Path(output_dir) / f"{name}_native"

        print(f"  Compiling {name} (gcc)... ", end="", flush=True)
        ret, _, stderr, elapsed = run_cmd(
            ["gcc", "-O2", str(src_path), "-o", str(bin_path)]
        )
        if ret == 0:
            print(f"OK ({elapsed:.1f}s)")
            binaries[name] = str(bin_path)
        else:
            print("FAILED")
    return binaries


def build_coverage_targets(output_dir):
    """Compile targets with gcc --coverage for coverage measurement."""
    cov_binaries = {}
    cov_dirs = {}
    os.makedirs(output_dir, exist_ok=True)

    for name, (source, _, _, _) in TARGETS.items():
        src_path = TARGETS_DIR / source
        cov_dir = os.path.join(output_dir, f"cov_{name}")
        os.makedirs(cov_dir, exist_ok=True)

        # Copy source to cov dir so gcno/gcda files are co-located
        cov_src = os.path.join(cov_dir, source)
        shutil.copy2(str(src_path), cov_src)

        # Binary name must match source basename for gcov to find .gcno/.gcda
        base_name = os.path.splitext(source)[0]
        bin_path = os.path.join(cov_dir, base_name)
        print(f"  Compiling {name} (coverage)... ", end="", flush=True)
        ret, _, stderr, elapsed = run_cmd(
            ["gcc", "--coverage", "-O0", "-g", cov_src, "-o", bin_path],
            timeout=60
        )
        if ret == 0:
            print(f"OK ({elapsed:.1f}s)")
            cov_binaries[name] = bin_path
            cov_dirs[name] = cov_dir
        else:
            print("FAILED")
            if stderr:
                print(f"    {stderr[:200]}")

    return cov_binaries, cov_dirs


def discover_public_coverage_targets() -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, list[str]]]:
    """发现已编译的 public benchmark 覆盖率二进制文件。

    查找 public/bin/<suite>-cov/ 目录下的覆盖率二进制和元数据文件
    （.covdir 和 .covsrc 由 compile_public_benchmarks.sh --with-coverage 生成）。

    Returns:
        (cov_binaries, cov_dirs, cov_sources, cov_libdirs) 四个字典，键为目标名
        cov_libdirs: 库构建目录列表，用于 lcov 采集完整覆盖率
    """
    cov_binaries: dict[str, str] = {}
    cov_dirs: dict[str, str] = {}
    cov_sources: dict[str, str] = {}
    cov_libdirs: dict[str, list[str]] = {}

    pub_bin_dir = PUBLIC_DIR / "bin"
    if not pub_bin_dir.is_dir():
        return cov_binaries, cov_dirs, cov_sources, cov_libdirs

    for suite_cov_dir in sorted(pub_bin_dir.iterdir()):
        if not suite_cov_dir.is_dir() or not suite_cov_dir.name.endswith("-cov"):
            continue
        # 从 "lava-m-cov" 得到 suite 前缀 "lava-m" -> target 前缀 "lava-"
        suite_name = suite_cov_dir.name[:-4]  # 去掉 "-cov"
        if suite_name == "lava-m":
            prefix = "lava-"
        elif suite_name == "google-fts":
            prefix = "gfts-"
        else:
            prefix = suite_name + "-"

        for binary in sorted(suite_cov_dir.iterdir()):
            if not binary.is_file() or binary.suffix:
                continue  # 跳过 .covdir, .covsrc 等元数据文件
            if not os.access(str(binary), os.X_OK):
                continue

            prog_name = binary.name
            target_name = prefix + prog_name
            covdir_file = suite_cov_dir / f"{prog_name}.covdir"
            covsrc_file = suite_cov_dir / f"{prog_name}.covsrc"

            if covdir_file.exists() and covsrc_file.exists():
                cov_dir_path = covdir_file.read_text().strip()
                cov_src_path = covsrc_file.read_text().strip()
                cov_binaries[target_name] = str(binary)
                cov_dirs[target_name] = cov_dir_path
                cov_sources[target_name] = cov_src_path

                # 读取库构建目录列表（如果存在）
                covlibdirs_file = suite_cov_dir / f"{prog_name}.covlibdirs"
                if covlibdirs_file.exists():
                    lib_dirs = [
                        d.strip() for d in covlibdirs_file.read_text().strip().splitlines()
                        if d.strip() and os.path.isdir(d.strip())
                    ]
                    if lib_dirs:
                        cov_libdirs[target_name] = lib_dirs

    return cov_binaries, cov_dirs, cov_sources, cov_libdirs


def _clean_gcda_files(directories: list[str]) -> None:
    """清理多个目录下的 .gcda 文件。"""
    for d in directories:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.endswith(".gcda"):
                    os.remove(os.path.join(root, f))


def _measure_with_lcov(all_cov_dirs: list[str]) -> tuple[float, float]:
    """使用 lcov 从多个目录采集覆盖率，返回 (line_cov, branch_cov)。

    lcov 能聚合所有 --coverage 编译的源文件（harness + 库），
    比 gcov 单文件测量更全面。
    """
    import tempfile
    line_cov = 0.0
    branch_cov = 0.0

    with tempfile.NamedTemporaryFile(suffix=".info", delete=False) as tmp:
        info_file = tmp.name

    try:
        # 构建 lcov 命令：从所有目录采集覆盖率
        cmd = ["lcov", "--capture", "--rc", "branch_coverage=1", "--quiet"]
        for d in all_cov_dirs:
            cmd.extend(["--directory", d])
        cmd.extend(["--output-file", info_file])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return 0.0, 0.0

        # 用 lcov --summary 获取汇总覆盖率
        result = subprocess.run(
            ["lcov", "--summary", info_file, "--rc", "branch_coverage=1"],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout + result.stderr  # lcov summary 输出到 stderr

        # 解析: "lines......: 45.2% (1234 of 2734 lines)"
        m = re.search(r"lines\.*:\s*(\d+\.?\d*)%", output)
        if m:
            line_cov = float(m.group(1))

        # 解析: "branches...: 32.1% (567 of 1765 branches)"
        m = re.search(r"branches\.*:\s*(\d+\.?\d*)%", output)
        if m:
            branch_cov = float(m.group(1))

    except (subprocess.TimeoutExpired, Exception):
        pass
    finally:
        try:
            os.remove(info_file)
        except OSError:
            pass

    return line_cov, branch_cov


def _measure_with_gcov(cov_dir: str, source_file: str) -> tuple[float, float]:
    """使用 gcov 从单个源文件测量覆盖率（旧方法，仅测 harness）。"""
    line_cov = 0.0
    branch_cov = 0.0

    try:
        # autotools 的 per-program CFLAGS 会生成形如 src_<prog>-<source>.gcno
        # 的文件名（如 src_md5sum-md5sum.gcno），直接用源文件名调用 gcov 会
        # 找不到对应的 gcno 文件。这里先检查是否存在 autotools 风格的 gcno，
        # 如果有则用 gcno 文件名调用 gcov。
        src_base = os.path.splitext(os.path.basename(source_file))[0]
        gcov_target = source_file  # 默认用源文件名
        gcno_dir = os.path.join(cov_dir, os.path.dirname(source_file))
        if os.path.isdir(gcno_dir):
            exact_gcno = os.path.join(gcno_dir, f"{src_base}.gcno")
            if not os.path.isfile(exact_gcno):
                import glob as _glob
                candidates = _glob.glob(
                    os.path.join(gcno_dir, f"*-{src_base}.gcno")
                )
                for c in candidates:
                    cname = os.path.basename(c)
                    if f"src_{src_base}-{src_base}.gcno" == cname:
                        gcov_target = os.path.join(
                            os.path.dirname(source_file), cname)
                        break
                else:
                    if candidates:
                        gcov_target = os.path.join(
                            os.path.dirname(source_file),
                            os.path.basename(candidates[0]))

        result = subprocess.run(
            ["gcov", "-b", gcov_target],
            capture_output=True, text=True, cwd=cov_dir, timeout=30
        )
        output = result.stdout

        m = re.search(r"Lines executed:(\d+\.\d+)% of (\d+)", output)
        if m:
            line_cov = float(m.group(1))

        m = re.search(r"Taken at least once:(\d+\.\d+)% of (\d+)", output)
        if m:
            branch_cov = float(m.group(1))
        else:
            m = re.search(r"Branches executed:(\d+\.\d+)% of (\d+)", output)
            if m:
                branch_cov = float(m.group(1))
    except Exception:
        pass

    return line_cov, branch_cov


def measure_coverage(cov_binary, cov_dir, source_file, test_case_dir,
                     uses_file=True, timeout_per_case=5,
                     max_cases=200000, lib_dirs=None):
    """
    Run all test cases through the coverage binary and measure coverage.

    Uses a shell loop to batch-execute test cases, avoiding per-file
    subprocess fork overhead (~100x faster for thousands of test cases).

    If lib_dirs is provided, uses lcov to aggregate coverage across harness
    and library source files (much more comprehensive than gcov single-file).

    If there are more than max_cases test cases, a stratified sample is used.

    Returns dict with: line_cov, branch_cov, crashes, total_cases
    """
    # 确定所有需要清理和采集的覆盖率目录
    all_cov_dirs = [cov_dir]
    if lib_dirs:
        all_cov_dirs.extend(lib_dirs)

    # 清理旧的 .gcda 文件（递归，支持多目录）
    _clean_gcda_files(all_cov_dirs)

    crashes = 0
    total_cases = 0

    if not os.path.isdir(test_case_dir):
        return {"line_cov": 0.0, "branch_cov": 0.0, "crashes": 0, "total_cases": 0}

    test_files = [os.path.join(test_case_dir, f)
                  for f in os.listdir(test_case_dir)
                  if os.path.isfile(os.path.join(test_case_dir, f))]
    total_cases = len(test_files)

    if total_cases == 0:
        return {"line_cov": 0.0, "branch_cov": 0.0, "crashes": 0, "total_cases": 0}

    # 采样策略：超过 max_cases 时使用分层采样
    sampled = False
    if total_cases > max_cases:
        test_files.sort()
        stride = total_cases / max_cases
        stride_set = set()
        stride_sample = []
        for i in range(max_cases):
            idx = int(i * stride)
            stride_set.add(idx)
            stride_sample.append(test_files[idx])

        bucket_extras = []
        prev_prefix = None
        for idx, fp in enumerate(test_files):
            fname = os.path.basename(fp)
            prefix = fname[:2] if len(fname) >= 2 else fname
            if prefix != prev_prefix:
                if idx not in stride_set:
                    bucket_extras.append(fp)
                if prev_prefix is not None and (idx - 1) not in stride_set:
                    bucket_extras.append(test_files[idx - 1])
                prev_prefix = prefix
        if test_files and (len(test_files) - 1) not in stride_set:
            bucket_extras.append(test_files[-1])

        test_files = stride_sample + bucket_extras
        sampled = True
    else:
        test_files.sort()

    # 批量执行测试用例
    list_file = os.path.join(cov_dir, "_test_list.txt")
    with open(list_file, "w") as lf:
        for fp in test_files:
            lf.write(fp + "\n")

    if uses_file:
        run_cmd_part = f'"{cov_binary}" "$f"'
    else:
        run_cmd_part = f'"{cov_binary}" < "$f"'

    script = (
        f'crashes=0; '
        f'while IFS= read -r f; do '
        f'  {run_cmd_part} >/dev/null 2>&1; '
        f'  rc=$?; '
        f'  [ $rc -gt 128 ] && crashes=$((crashes+1)); '
        f'done < "{list_file}"; '
        f'echo "$crashes"'
    )

    try:
        batch_timeout = max(60, len(test_files) * 2)
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, timeout=batch_timeout
        )
        if result.stdout.strip().isdigit():
            crashes = int(result.stdout.strip())
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass

    try:
        os.remove(list_file)
    except OSError:
        pass

    # 测量覆盖率：有库目录时用 lcov（完整覆盖率），否则用 gcov（仅 harness）
    line_cov = 0.0
    branch_cov = 0.0

    use_lcov = lib_dirs and shutil.which("lcov")
    if use_lcov:
        line_cov, branch_cov = _measure_with_lcov(all_cov_dirs)
    else:
        line_cov, branch_cov = _measure_with_gcov(cov_dir, source_file)

    return {
        "line_cov": line_cov,
        "branch_cov": branch_cov,
        "crashes": crashes,
        "total_cases": total_cases,
        "sampled": sampled,
        "sampled_cases": len(test_files) if sampled else total_cases,
    }


def measure_coverage_afl(afl_binary: str, test_case_dir: str,
                         uses_file: bool = True,
                         timeout_per_case: int = 5000) -> dict:
    """使用 afl-showmap -C 测量 AFL 边覆盖率。

    通过 afl-showmap 的批量收集模式（-C -i dir）一次性处理所有测试用例，
    输出边覆盖率百分比。比 gcov/lcov 快得多，且不需要特殊的 coverage 二进制。

    Args:
        afl_binary: AFL-instrumented 二进制路径
        test_case_dir: 包含测试用例的目录
        uses_file: True 表示目标从文件读取输入，False 表示从 stdin
        timeout_per_case: 每个测试用例超时（毫秒）

    Returns:
        dict with: edge_cov (%), edges_found, edges_total, crashes
    """
    if not os.path.isdir(test_case_dir):
        return {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0,
                "crashes": 0, "total_cases": 0}

    test_files = [f for f in os.listdir(test_case_dir)
                  if os.path.isfile(os.path.join(test_case_dir, f))]
    total_cases = len(test_files)
    if total_cases == 0:
        return {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0,
                "crashes": 0, "total_cases": 0}

    afl_showmap = shutil.which("afl-showmap")
    if not afl_showmap:
        return {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0,
                "crashes": 0, "total_cases": total_cases}

    out_file = tempfile.mktemp(prefix=".afl_cov_", suffix=".map")
    cmd = [
        afl_showmap,
        "-t", str(timeout_per_case),
        "-m", "none",
        "-C",
        "-i", test_case_dir,
        "-o", out_file,
        "--", afl_binary,
    ]
    if uses_file:
        cmd.append("@@")

    try:
        batch_timeout = max(60, total_cases * 2)
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=batch_timeout
        )
        stderr = result.stderr + result.stdout  # afl-showmap 输出到 stderr

        # 解析 "A coverage of N edges were achieved out of M existing (X%)"
        edge_cov = 0.0
        edges_found = 0
        edges_total = 0
        m = re.search(
            r"coverage of (\d+) edges were achieved out of (\d+) existing "
            r"\(([0-9.]+)%\)",
            stderr
        )
        if m:
            edges_found = int(m.group(1))
            edges_total = int(m.group(2))
            edge_cov = float(m.group(3))
        else:
            # 备用：从 "Captured N tuples" 解析
            m2 = re.search(r"Captured (\d+) tuples \(map size (\d+)", stderr)
            if m2:
                edges_found = int(m2.group(1))
                edges_total = int(m2.group(2))
                if edges_total > 0:
                    edge_cov = edges_found / edges_total * 100.0

    except subprocess.TimeoutExpired:
        edge_cov = 0.0
        edges_found = 0
        edges_total = 0
    except Exception:
        edge_cov = 0.0
        edges_found = 0
        edges_total = 0

    try:
        os.remove(out_file)
    except OSError:
        pass

    return {
        "edge_cov": round(edge_cov, 2),
        "edges_found": edges_found,
        "edges_total": edges_total,
        "crashes": 0,  # afl-showmap -C 不单独报告 crash 数
        "total_cases": total_cases,
    }


def discover_afl_coverage_binaries() -> dict[str, str]:
    """发现所有可用于 AFL 覆盖率测量的二进制文件。

    扫描 public/bin/ 下所有 *-afl 目录，返回 {目标名: AFL二进制路径}。
    """
    afl_cov_binaries: dict[str, str] = {}
    pub_bin_dir = PUBLIC_DIR / "bin"
    if not pub_bin_dir.is_dir():
        return afl_cov_binaries

    for suite_dir in sorted(pub_bin_dir.iterdir()):
        if not suite_dir.is_dir() or not suite_dir.name.endswith("-afl"):
            continue
        # 从 "google-fts-afl" 得到 suite 名 "google-fts"
        suite_name = suite_dir.name[:-4]  # 去掉 "-afl"
        if suite_name == "lava-m":
            prefix = "lava-"
        elif suite_name == "google-fts":
            prefix = "gfts-"
        else:
            prefix = suite_name + "-"

        for binary in sorted(suite_dir.iterdir()):
            if binary.suffix:
                continue
            if binary.is_file() and os.access(str(binary), os.X_OK):
                target_name = prefix + binary.name
                afl_cov_binaries[target_name] = str(binary)

    return afl_cov_binaries


def measure_coverage_timeseries_afl(
    afl_binary: str,
    test_case_dir: str,
    interval: int = 30,
    max_duration: int = 600,
    uses_file: bool = True,
) -> list[dict]:
    """在后台线程中定期采样 AFL 边覆盖率，生成时间序列数据。

    每隔 interval 秒运行一次 afl-showmap 覆盖率测量，记录当前时间点的覆盖率。
    返回 [{timestamp_sec, edge_cov, edges_found, edges_total, total_cases}, ...] 列表。
    """
    timeseries: list[dict] = []
    start_time = time.monotonic()

    while time.monotonic() - start_time < max_duration:
        elapsed = time.monotonic() - start_time
        try:
            cov_data = measure_coverage_afl(
                afl_binary, test_case_dir, uses_file=uses_file,
            )
            timeseries.append({
                "timestamp_sec": round(elapsed, 1),
                "edge_cov": cov_data["edge_cov"],
                "edges_found": cov_data["edges_found"],
                "edges_total": cov_data["edges_total"],
                "total_cases": cov_data["total_cases"],
            })
        except Exception:
            pass
        # 等待到下一个采样点
        next_sample = start_time + len(timeseries) * interval
        sleep_time = next_sample - time.monotonic()
        if sleep_time > 0:
            time.sleep(sleep_time)

    return timeseries


def run_with_timeseries(
    run_fn,
    run_kwargs: dict,
    afl_binary: str,
    interval: int = 30,
    uses_file: bool = True,
    timeout: int = 300,
) -> tuple[dict, list[dict]]:
    """运行基准测试同时在后台采样 AFL 边覆盖率时间序列。

    run_fn: 实际执行函数 (run_mpi, run_hybrid, etc.)
    run_kwargs: 传递给 run_fn 的参数
    返回 (run_result, timeseries)
    """
    result_container = [None]
    error_container = [None]

    def run_benchmark():
        try:
            result_container[0] = run_fn(**run_kwargs)
        except Exception as e:
            error_container[0] = e

    bench_thread = threading.Thread(target=run_benchmark, daemon=True)
    bench_thread.start()

    # 等待输出目录出现
    output_dir = run_kwargs.get("work_dir", "")
    np_val = run_kwargs.get("np")
    if np_val:
        candidate_dir = os.path.join(output_dir, f"mpi_np{np_val}_output")
    else:
        candidate_dir = os.path.join(output_dir, "output")

    wait_start = time.monotonic()
    while not os.path.isdir(candidate_dir) and time.monotonic() - wait_start < 10:
        time.sleep(0.5)

    if not os.path.isdir(candidate_dir):
        bench_thread.join(timeout=timeout + 60)
        result = result_container[0]
        if error_container[0]:
            raise error_container[0]
        return result, []

    # 在后台采样 AFL 边覆盖率
    timeseries = measure_coverage_timeseries_afl(
        afl_binary, candidate_dir,
        interval=interval, max_duration=timeout + 30,
        uses_file=uses_file,
    )

    bench_thread.join(timeout=60)
    result = result_container[0]
    if error_container[0]:
        raise error_container[0]

    return result, timeseries


def count_output_files(directory):
    """Count test case files in a directory."""
    if not os.path.isdir(directory):
        return 0
    count = 0
    for f in os.listdir(directory):
        if os.path.isfile(os.path.join(directory, f)):
            count += 1
    return count


def get_unique_hashes(directory):
    """Get set of unique file content hashes.

    Optimization: if filenames look like hex SHA-256 hashes (64 hex chars),
    use the filename directly instead of re-reading and re-hashing file contents.
    Both the serial script and MPI master use hash-based naming.
    """
    hashes = set()
    if not os.path.isdir(directory):
        return hashes
    hex64_re = re.compile(r'^[0-9a-f]{64}$')
    for f in os.listdir(directory):
        fpath = os.path.join(directory, f)
        if os.path.isfile(fpath):
            if hex64_re.match(f):
                # Filename is the hash — skip expensive re-read
                hashes.add(f)
            else:
                with open(fpath, "rb") as fh:
                    h = hashlib.sha256(fh.read()).hexdigest()
                    hashes.add(h)
    return hashes


def _simulate_serial(binary, seed_dir, output_dir, timeout, uses_file,
                     max_files=10000):
    """模拟模式的串行执行：运行目标二进制并生成随机变异测试用例。

    限制最大文件数以避免产生过多文件导致后续计数和清理太慢。
    """
    import random as _random

    env = os.environ.copy()
    magic_path = os.path.join(os.path.dirname(binary), "magic.mgc")
    if os.path.isfile(magic_path):
        env["MAGIC"] = magic_path

    # 收集种子文件
    seeds = [os.path.join(seed_dir, f) for f in sorted(os.listdir(seed_dir))
             if os.path.isfile(os.path.join(seed_dir, f))]
    if not seeds:
        return

    queue = list(seeds)
    start = time.monotonic()
    file_count = 0

    while time.monotonic() - start < timeout and queue and file_count < max_files:
        input_file = queue.pop(0)
        try:
            with open(input_file, "rb") as f:
                data = f.read()
        except (IOError, OSError):
            continue

        if not data:
            continue

        # 运行目标二进制
        if uses_file:
            cmd = ["timeout", "-k", "2", "5", binary, input_file]
        else:
            cmd = ["timeout", "-k", "2", "5", binary]

        try:
            if uses_file:
                subprocess.run(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=env)
            else:
                with open(input_file, "rb") as inf:
                    subprocess.run(cmd, stdin=inf,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=env)
        except Exception:
            pass

        # 生成变异测试用例
        for i in range(5):
            if file_count >= max_files:
                break
            mutated = bytearray(data)
            num_bytes = _random.randint(1, min(3, len(mutated)))
            for _ in range(num_bytes):
                pos = _random.randint(0, len(mutated) - 1)
                mutated[pos] = _random.randint(0, 255)
            h = hashlib.sha256(bytes(mutated)).hexdigest()
            out_path = os.path.join(output_dir, h)
            if not os.path.exists(out_path):
                with open(out_path, "wb") as f:
                    f.write(bytes(mutated))
                queue.append(out_path)
                file_count += 1


def run_serial(binary, target_name, seed_dir, timeout, work_dir,
               simulate=False):
    """Run the serial pure_concolic_execution.sh baseline."""
    output_dir = os.path.join(work_dir, "serial_output")
    os.makedirs(output_dir, exist_ok=True)

    uses_file = TARGETS[target_name][3] if target_name in TARGETS else True

    if simulate:
        # 模拟模式：直接在 Python 中运行目标并生成变异
        timed_out = False
        start = time.monotonic()
        try:
            _simulate_serial(binary, seed_dir, output_dir, timeout, uses_file)
        except Exception:
            pass
        elapsed = time.monotonic() - start
        timed_out = elapsed >= timeout * 0.95
        retcode = 0
    else:
        if uses_file:
            cmd = [
                "bash", str(SERIAL_SCRIPT),
                "-i", seed_dir,
                "-o", output_dir,
                binary, "@@"
            ]
        else:
            cmd = [
                "bash", str(SERIAL_SCRIPT),
                "-i", seed_dir,
                "-o", output_dir,
                binary
            ]

        # Set up environment: auto-detect magic.mgc for 'file' binary
        env = None
        magic_path = os.path.join(os.path.dirname(binary), "magic.mgc")
        if os.path.isfile(magic_path):
            env = os.environ.copy()
            env["MAGIC"] = magic_path

        # The serial script runs forever, so we use timeout.
        # Use start_new_session so we can kill the entire process group on timeout
        # (otherwise SymCC children spawned by the shell script become orphans).
        # Use DEVNULL instead of PIPE to avoid deadlock — we don't need the output,
        # and PIPE with only wait() (no communicate()) deadlocks when the 64KB
        # pipe buffer fills up.
        timed_out = False
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=env
            )
            proc.wait(timeout=timeout)
            retcode = proc.returncode
        except subprocess.TimeoutExpired:
            # Kill the entire process group (shell + all children)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
            retcode = -1
            timed_out = True

        elapsed = time.monotonic() - start

    num_generated = count_output_files(output_dir)
    unique = get_unique_hashes(output_dir)

    return {
        "wall_time": elapsed,
        "generated": num_generated,
        "unique": len(unique),
        "throughput": num_generated / elapsed if elapsed > 0 else 0,
        "output_dir": output_dir,
        "retcode": retcode,
        "timed_out": timed_out,
    }


def run_mpi(binary, target_name, seed_dir, np, timeout, work_dir,
            simulate=False):
    """Run MPI-parallel concolic execution."""
    output_dir = os.path.join(work_dir, f"mpi_np{np}_output")
    os.makedirs(output_dir, exist_ok=True)

    uses_file = TARGETS[target_name][3] if target_name in TARGETS else True
    max_idle = max(10, timeout // 6)  # shorter idle wait for benchmarks

    # Give MPI script a wall timeout slightly less than the benchmark timeout
    # so it can shut down gracefully before the outer subprocess kills it.
    # 留 10% 或至少 5 秒余量给关闭过程
    wall_timeout = max(10, int(timeout * 0.9) - 5)
    # 每次执行的超时应远小于总超时，避免单次执行耗尽全部时间
    # 保持较短以最大化探索的输入数量（广度优先优于深度优先）
    per_exec_timeout = min(30, max(5, timeout // 4))
    cmd = [
        "mpirun", "--allow-run-as-root", "--oversubscribe",
        "-np", str(np),
        "python3", str(MPI_SCRIPT),
        "-i", seed_dir,
        "-o", output_dir,
        "-t", str(per_exec_timeout),
        "--max-idle", str(max_idle),
        "--wall-timeout", str(wall_timeout),
    ]
    if simulate:
        cmd.append("--simulate")
    cmd.extend(["--", binary])

    if uses_file:
        cmd.append("@@")

    # Set up environment: auto-detect magic.mgc for 'file' binary
    env = None
    magic_path = os.path.join(os.path.dirname(binary), "magic.mgc")
    if os.path.isfile(magic_path):
        env = os.environ.copy()
        env["MAGIC"] = magic_path

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 30,
            env=env
        )
        stdout = proc.stdout
        stderr = proc.stderr
        retcode = proc.returncode
    except subprocess.TimeoutExpired:
        stdout = ""
        stderr = "TIMEOUT"
        retcode = -1

    elapsed = time.monotonic() - start
    # Detect timeout: outer kill, or MPI master hit its wall-timeout
    hard_timeout = (retcode == -1 and stderr == "TIMEOUT") or elapsed >= timeout + 25
    wall_timeout_hit = False
    if stdout and "Wall-clock timeout" in stdout:
        wall_timeout_hit = True
    timed_out = hard_timeout or wall_timeout_hit

    # Parse the MPI master's stdout for stats
    mpi_total_generated = None
    mpi_total_interesting = None
    mpi_num_masters = None
    mpi_num_workers = None
    mpi_throughput = None
    if stdout:
        m = re.search(r"Total test cases generated:\s*(\d+)", stdout)
        if m:
            mpi_total_generated = int(m.group(1))
        # "New interesting test cases" is the ground-truth file count
        # from shared_dir minus seeds (accurate even in multi-master mode).
        m = re.search(r"New interesting test cases:\s*(\d+)", stdout)
        if m:
            mpi_total_interesting = int(m.group(1))
        m = re.search(r"Masters used:\s*(\d+)", stdout)
        if m:
            mpi_num_masters = int(m.group(1))
        m = re.search(r"Workers used:\s*(\d+)", stdout)
        if m:
            mpi_num_workers = int(m.group(1))
        m = re.search(r"Throughput:\s*([\d.]+)\s*tc/s", stdout)
        if m:
            mpi_throughput = float(m.group(1))

    # Use MPI master's parsed stats when available (avoids expensive directory traversal).
    if mpi_total_generated is not None:
        num_generated = mpi_total_generated
        num_unique = mpi_total_interesting if mpi_total_interesting is not None else len(get_unique_hashes(output_dir))
    else:
        unique = get_unique_hashes(output_dir)
        num_generated = count_output_files(output_dir)
        num_unique = len(unique)

    return {
        "wall_time": elapsed,
        "generated": num_generated,
        "unique": num_unique,
        "output_dir": output_dir,
        "stdout": stdout[-500:] if stdout else "",
        "stderr": stderr[-500:] if stderr else "",
        "retcode": retcode,
        "timed_out": timed_out,
        "num_masters": mpi_num_masters,
        "num_workers": mpi_num_workers,
        "throughput": mpi_throughput or (num_generated / elapsed if elapsed > 0 else 0),
    }


def discover_public_afl_targets() -> dict[str, str]:
    """发现所有 AFL-instrumented 二进制文件。

    扫描 public/bin/ 下所有 *-afl 目录（google-fts-afl、lava-m-afl 等）。
    返回 {目标名: AFL 二进制路径} 字典。
    """
    afl_binaries: dict[str, str] = {}
    pub_bin = PUBLIC_DIR / "bin"
    if not pub_bin.is_dir():
        return afl_binaries

    suite_prefixes = {"google-fts": "gfts-", "lava-m": "lava-"}
    for suite_dir in sorted(pub_bin.iterdir()):
        if not suite_dir.is_dir() or not suite_dir.name.endswith("-afl"):
            continue
        suite_base = suite_dir.name[:-4]  # 去掉 -afl
        prefix = suite_prefixes.get(suite_base, suite_base + "-")
        for binary in sorted(suite_dir.iterdir()):
            if binary.is_file() and not binary.suffix and os.access(str(binary), os.X_OK):
                afl_binaries[prefix + binary.name] = str(binary)

    return afl_binaries


def run_hybrid(symcc_binary: str, afl_binary: str, target_name: str,
               seed_dir: str, np: int, timeout: int, work_dir: str) -> dict:
    """运行 AFL + MPI SymCC 混合模式。

    1. 启动 AFL fuzzer (afl-fuzz -M fuzzer01)
    2. 等待 AFL 初始化
    3. 启动 MPI SymCC workers (mpi_fuzzing_helper.py)
    4. 等待 timeout
    5. 终止两个进程
    6. 收集结果
    """
    afl_out_dir = os.path.join(work_dir, "afl_out")
    os.makedirs(afl_out_dir, exist_ok=True)

    # 启动 AFL fuzzer
    afl_cmd = [
        "afl-fuzz",
        "-M", "fuzzer01",
        "-i", seed_dir,
        "-o", afl_out_dir,
        "-m", "none",
        "--", afl_binary, "@@",
    ]

    print(f"      Starting AFL: {' '.join(afl_cmd[:8])}...")
    afl_env = os.environ.copy()
    afl_env["AFL_NO_UI"] = "1"  # 无 UI 模式，避免终端干扰
    afl_env["AFL_SKIP_CPUFREQ"] = "1"
    afl_env["AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES"] = "1"

    afl_proc = subprocess.Popen(
        afl_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=afl_env,
    )

    # 等待 AFL 初始化（fuzzer_stats 文件出现）
    fuzzer_dir = os.path.join(afl_out_dir, "fuzzer01")
    stats_path = os.path.join(fuzzer_dir, "fuzzer_stats")
    start = time.monotonic()
    afl_ready = False
    while time.monotonic() - start < 30:
        if os.path.isfile(stats_path):
            afl_ready = True
            break
        # 检查 AFL 是否崩溃
        if afl_proc.poll() is not None:
            print(f"      AFL exited early (ret={afl_proc.returncode})")
            return {
                "wall_time": time.monotonic() - start,
                "generated": 0, "unique": 0, "output_dir": afl_out_dir,
                "retcode": afl_proc.returncode, "timed_out": False,
                "throughput": 0, "stdout": "", "stderr": "",
                "afl_generated": 0, "symcc_interesting": 0,
            }
        time.sleep(0.5)

    if not afl_ready:
        print("      WARNING: AFL did not initialize in 30s, continuing anyway")

    # 保存所有 SymCC 输出（不经 afl-showmap 过滤）用于覆盖率测量
    symcc_all_dir = os.path.join(work_dir, "symcc_all_outputs")
    os.makedirs(symcc_all_dir, exist_ok=True)

    # 启动 MPI SymCC workers
    symcc_np = max(2, np - 1)  # 留 1 个核给 AFL
    mpi_cmd = [
        "mpirun", "--allow-run-as-root", "--oversubscribe",
        "-np", str(symcc_np),
        "python3", "-u", str(MPI_FUZZING_SCRIPT),
        "-a", "fuzzer01",
        "-o", afl_out_dir,
        "-n", "symcc01",
        "--save-all", symcc_all_dir,
        "--", symcc_binary, "@@",
    ]

    print(f"      Starting MPI SymCC (np={symcc_np})...")
    mpi_env = os.environ.copy()
    mpi_env["PYTHONUNBUFFERED"] = "1"
    mpi_proc = subprocess.Popen(
        mpi_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=mpi_env,
    )

    # 等待 timeout
    remaining = timeout - (time.monotonic() - start)
    try:
        mpi_stdout_bytes, _ = mpi_proc.communicate(timeout=max(10, remaining))
    except subprocess.TimeoutExpired:
        mpi_stdout_bytes = b""

    # 终止进程
    for proc, name in [(mpi_proc, "MPI"), (afl_proc, "AFL")]:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()

    elapsed = time.monotonic() - start

    # 读取 MPI 输出
    mpi_stdout = ""
    try:
        mpi_stdout = mpi_stdout_bytes.decode(errors="replace")
    except Exception:
        pass

    # 收集结果
    # AFL 生成的测试用例在 fuzzer01/queue/
    # SymCC 反馈的用例在 symcc01/queue/
    afl_queue = os.path.join(afl_out_dir, "fuzzer01", "queue")
    symcc_queue = os.path.join(afl_out_dir, "symcc01", "queue")

    afl_count = count_output_files(afl_queue) if os.path.isdir(afl_queue) else 0
    symcc_count = count_output_files(symcc_queue) if os.path.isdir(symcc_queue) else 0

    # 合并所有测试用例到一个目录用于覆盖率测量
    combined_dir = os.path.join(work_dir, "combined_output")
    os.makedirs(combined_dir, exist_ok=True)

    # 复制种子
    for f in os.listdir(seed_dir):
        src = os.path.join(seed_dir, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(combined_dir, f"seed_{f}"))

    # 复制 AFL queue
    if os.path.isdir(afl_queue):
        for f in os.listdir(afl_queue):
            src = os.path.join(afl_queue, f)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(combined_dir, f"afl_{f}"))

    # 复制 SymCC queue (afl-showmap 过滤后的 interesting)
    if os.path.isdir(symcc_queue):
        for f in os.listdir(symcc_queue):
            src = os.path.join(symcc_queue, f)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(combined_dir, f"symcc_{f}"))

    # 复制所有 SymCC 输出（未过滤）— 这些可能有 lcov 覆盖率提升
    symcc_all_count = 0
    if os.path.isdir(symcc_all_dir):
        for f in os.listdir(symcc_all_dir):
            src = os.path.join(symcc_all_dir, f)
            if os.path.isfile(src):
                dest = os.path.join(combined_dir, f"symcc_all_{f}")
                if not os.path.exists(dest):
                    shutil.copy2(src, dest)
                    symcc_all_count += 1

    total_generated = afl_count + symcc_all_count

    # 解析 MPI 输出中的 interesting count
    symcc_interesting = 0
    m = re.search(r"(\d+) interesting", mpi_stdout)
    if m:
        symcc_interesting = int(m.group(1))

    # 从 fuzzer_stats 解析 AFL 指标
    afl_bitmap_cvg = ""
    afl_execs_done = 0
    afl_execs_per_sec = 0.0
    if os.path.isfile(stats_path):
        try:
            with open(stats_path) as f:
                for line in f:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if key == "bitmap_cvg":
                        afl_bitmap_cvg = val
                    elif key == "execs_done":
                        afl_execs_done = int(val)
                    elif key == "execs_per_sec":
                        afl_execs_per_sec = float(val)
        except Exception:
            pass

    return {
        "wall_time": elapsed,
        "generated": total_generated,
        "unique": total_generated,
        "output_dir": combined_dir,
        "retcode": mpi_proc.returncode or 0,
        "timed_out": elapsed >= timeout * 0.95,
        "throughput": total_generated / elapsed if elapsed > 0 else 0,
        "stdout": mpi_stdout[-500:] if mpi_stdout else "",
        "stderr": "",
        "afl_generated": afl_count,
        "symcc_interesting": symcc_interesting,
        "afl_bitmap_cvg": afl_bitmap_cvg,
        "afl_execs_done": afl_execs_done,
        "afl_execs_per_sec": afl_execs_per_sec,
        "num_workers": symcc_np - 1,
    }


def run_afl_only(afl_binary: str, target_name: str,
                 seed_dir: str, timeout: int, work_dir: str) -> dict:
    """运行 AFL-only 基准模式（无 SymCC）。

    仅启动 AFL fuzzer，作为 hybrid 模式的对照基准。
    """
    afl_out_dir = os.path.join(work_dir, "afl_out")
    os.makedirs(afl_out_dir, exist_ok=True)

    afl_cmd = [
        "afl-fuzz",
        "-M", "fuzzer01",
        "-i", seed_dir,
        "-o", afl_out_dir,
        "-m", "none",
        "--", afl_binary, "@@",
    ]

    print(f"      Starting AFL-only: {' '.join(afl_cmd[:8])}...")
    afl_env = os.environ.copy()
    afl_env["AFL_NO_UI"] = "1"
    afl_env["AFL_SKIP_CPUFREQ"] = "1"
    afl_env["AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES"] = "1"

    start = time.monotonic()
    afl_proc = subprocess.Popen(
        afl_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=afl_env,
    )

    # 等待 AFL 初始化
    fuzzer_dir = os.path.join(afl_out_dir, "fuzzer01")
    stats_path = os.path.join(fuzzer_dir, "fuzzer_stats")
    afl_ready = False
    while time.monotonic() - start < 30:
        if os.path.isfile(stats_path):
            afl_ready = True
            break
        if afl_proc.poll() is not None:
            print(f"      AFL exited early (ret={afl_proc.returncode})")
            return {
                "wall_time": time.monotonic() - start,
                "generated": 0, "unique": 0, "output_dir": afl_out_dir,
                "retcode": afl_proc.returncode, "timed_out": False,
                "throughput": 0, "stdout": "", "stderr": "",
            }
        time.sleep(0.5)

    if not afl_ready:
        print("      WARNING: AFL did not initialize in 30s, continuing anyway")

    # 等待 timeout
    remaining = timeout - (time.monotonic() - start)
    if remaining > 0:
        try:
            afl_proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass

    # 终止 AFL
    if afl_proc.poll() is None:
        try:
            os.killpg(afl_proc.pid, signal.SIGTERM)
            afl_proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(afl_proc.pid, signal.SIGKILL)
                afl_proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                afl_proc.kill()

    elapsed = time.monotonic() - start

    # 收集结果
    afl_queue = os.path.join(afl_out_dir, "fuzzer01", "queue")
    afl_count = count_output_files(afl_queue) if os.path.isdir(afl_queue) else 0

    # 合并种子和 AFL queue 用于覆盖率测量
    combined_dir = os.path.join(work_dir, "combined_output")
    os.makedirs(combined_dir, exist_ok=True)

    for f in os.listdir(seed_dir):
        src = os.path.join(seed_dir, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(combined_dir, f"seed_{f}"))

    if os.path.isdir(afl_queue):
        for f in os.listdir(afl_queue):
            src = os.path.join(afl_queue, f)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(combined_dir, f"afl_{f}"))

    # 从 fuzzer_stats 解析关键指标
    afl_bitmap_cvg = ""
    afl_execs_done = 0
    afl_execs_per_sec = 0.0
    afl_corpus_count = 0
    if os.path.isfile(stats_path):
        try:
            with open(stats_path) as f:
                for line in f:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if key == "bitmap_cvg":
                        afl_bitmap_cvg = val
                    elif key == "execs_done":
                        afl_execs_done = int(val)
                    elif key == "execs_per_sec":
                        afl_execs_per_sec = float(val)
                    elif key == "corpus_count":
                        afl_corpus_count = int(val)
        except Exception:
            pass

    return {
        "wall_time": elapsed,
        "generated": afl_count,       # queue 中的 interesting 用例数
        "unique": afl_count,
        "output_dir": combined_dir,
        "retcode": afl_proc.returncode or 0,
        "timed_out": elapsed >= timeout * 0.95,
        "throughput": afl_count / elapsed if elapsed > 0 else 0,
        "stdout": "",
        "stderr": "",
        "afl_bitmap_cvg": afl_bitmap_cvg,
        "afl_execs_done": afl_execs_done,
        "afl_execs_per_sec": afl_execs_per_sec,
        "afl_corpus_count": afl_corpus_count,
    }


def format_time(seconds):
    """Format seconds as human-readable."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m{secs:.1f}s"


def generate_report(results, output_dir):
    """Generate the performance comparison report."""
    report_path = os.path.join(output_dir, "benchmark_report.txt")
    csv_path = os.path.join(output_dir, "benchmark_data.csv")
    json_path = os.path.join(output_dir, "benchmark_data.json")

    # CSV output
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "target", "mode", "np", "round",
            "wall_time_sec", "generated", "unique", "throughput_tc_s",
            "edge_cov_pct", "edges_found", "edges_total", "crashes",
            "speedup", "efficiency"
        ])
        for row in results:
            writer.writerow([
                row["target"], row["mode"], row["np"], row["round"],
                f"{row['wall_time']:.2f}", row["generated"], row["unique"],
                f"{row.get('throughput', 0.0):.2f}",
                f"{row.get('edge_cov', 0.0):.2f}",
                row.get("edges_found", 0),
                row.get("edges_total", 0),
                row.get("crashes", 0),
                f"{row.get('speedup', 1.0):.2f}",
                f"{row.get('efficiency', 100.0):.1f}"
            ])

    # JSON output
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    # Text report
    with open(report_path, "w") as f:
        f.write("=" * 80 + "\n")
        f.write("  SymCC MPI Parallelization Benchmark Report\n")
        f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 80 + "\n\n")

        # Group by target
        by_target = defaultdict(lambda: defaultdict(list))
        for row in results:
            key = (row["mode"], row["np"])
            by_target[row["target"]][key].append(row)

        for target in sorted(by_target.keys()):
            configs = by_target[target]
            f.write(f"\n{'─' * 80}\n")
            f.write(f"  Target: {target}\n")
            f.write(f"{'─' * 80}\n\n")

            # Compute averages
            summaries = []
            for (mode, np_val), rows in sorted(configs.items()):
                avg_time = sum(r["wall_time"] for r in rows) / len(rows)
                avg_gen = sum(r["generated"] for r in rows) / len(rows)
                avg_uniq = sum(r["unique"] for r in rows) / len(rows)
                avg_edge_cov = sum(r.get("edge_cov", 0) for r in rows) / len(rows)
                avg_edges_found = sum(r.get("edges_found", 0) for r in rows) / len(rows)
                avg_edges_total = sum(r.get("edges_total", 0) for r in rows) / len(rows)
                total_crashes = sum(r.get("crashes", 0) for r in rows)
                avg_workers = sum(r.get("num_workers", np_val - 1) for r in rows) / len(rows)
                avg_throughput = sum(r.get("throughput", 0) for r in rows) / len(rows)
                summaries.append({
                    "mode": mode,
                    "np": np_val,
                    "avg_time": avg_time,
                    "avg_generated": avg_gen,
                    "avg_unique": avg_uniq,
                    "avg_throughput": avg_throughput,
                    "avg_edge_cov": avg_edge_cov,
                    "avg_edges_found": avg_edges_found,
                    "avg_edges_total": avg_edges_total,
                    "total_crashes": total_crashes,
                    "avg_workers": avg_workers,
                    "rounds": len(rows),
                })

            # Find serial baseline and np=2 baseline for speedup/efficiency
            serial_throughput = None
            base_mpi_throughput = None
            for s in summaries:
                if s["mode"] == "serial":
                    serial_throughput = s["avg_throughput"]
                if s["mode"] == "mpi" and s["np"] == 2:
                    base_mpi_throughput = s["avg_throughput"]

            # Check if any coverage data is present
            has_cov = any(s["avg_edge_cov"] > 0 for s in summaries)

            # Table header
            hdr = (f"  {'Mode':<10} {'NP':>4} {'Avg Time':>12} "
                   f"{'Generated':>10} {'Unique':>8} {'tc/s':>10} ")
            sep = (f"  {'─'*10} {'─'*4} {'─'*12} "
                   f"{'─'*10} {'─'*8} {'─'*10} ")
            if has_cov:
                hdr += f"{'EdgeCov':>10} {'Edges':>14} {'Crashes':>8} "
                sep += f"{'─'*10} {'─'*14} {'─'*8} "
            hdr += f"{'Speedup':>8} {'Efficiency':>10}\n"
            sep += f"{'─'*8} {'─'*10}\n"
            f.write(hdr)
            f.write(sep)

            for s in summaries:
                # Speedup = tc/s ratio vs serial baseline
                # Efficiency = 并行扩展效率，以 np=2（单 worker）为基线
                if (serial_throughput and serial_throughput > 0
                        and s["mode"] != "serial"):
                    speedup = (s["avg_throughput"] / serial_throughput
                               if serial_throughput > 0 else 0)
                    workers = s.get("avg_workers") or (s["np"] - 1)
                    if (base_mpi_throughput and base_mpi_throughput > 0
                            and workers > 0):
                        efficiency = (s["avg_throughput"]
                                      / base_mpi_throughput
                                      / workers * 100)
                    elif workers > 0:
                        efficiency = (speedup / workers * 100)
                    else:
                        efficiency = 0.0
                else:
                    speedup = 1.0
                    efficiency = 100.0

                tp_str = (f"{s['avg_throughput']:>10.1f}"
                          if s["avg_throughput"] >= 1
                          else f"{s['avg_throughput']:>10.2f}")

                line = (f"  {s['mode']:<10} {s['np']:>4} "
                        f"{format_time(s['avg_time']):>12} "
                        f"{s['avg_generated']:>10.1f} "
                        f"{s['avg_unique']:>8.1f} {tp_str} ")
                if has_cov:
                    edges_str = f"{int(s['avg_edges_found'])}/{int(s['avg_edges_total'])}"
                    line += (f"{s['avg_edge_cov']:>9.2f}% "
                             f"{edges_str:>14} "
                             f"{s['total_crashes']:>8} ")
                line += f"{speedup:>7.2f}x {efficiency:>9.1f}%\n"
                f.write(line)

            f.write("\n")

            # Edge Coverage chart (ASCII) - most important metric
            if has_cov:
                max_cov = max((s["avg_edge_cov"] for s in summaries), default=1)
                scale = max(max_cov, 1.0)  # 动态缩放
                f.write("  Edge Coverage Chart:\n")
                for s in summaries:
                    label = f"  np={s['np']:>2}" if s["mode"] != "serial" else "  serial"
                    cov = s["avg_edge_cov"]
                    bar_len = int(cov / scale * 40)
                    bar = "█" * bar_len + "░" * max(0, 40 - bar_len)
                    f.write(f"  {label:>8} |{bar}| {cov:.2f}%\n")
                f.write("\n")

            # Throughput Speedup chart (ASCII)
            f.write("  Throughput Speedup Chart (tc/s ratio vs serial):\n")
            max_speedup = 1.0
            speedups = []
            for s in summaries:
                if (serial_throughput and serial_throughput > 0
                        and s["mode"] != "serial"):
                    sp = (s["avg_throughput"] / serial_throughput
                          if serial_throughput > 0 else 0)
                else:
                    sp = 1.0
                speedups.append(sp)
                max_speedup = max(max_speedup, sp)
            scale = 40 / max_speedup if max_speedup > 0 else 1
            for s, sp in zip(summaries, speedups):
                bar_len = int(sp * scale)
                label = f"  np={s['np']:>3}" if s["mode"] != "serial" else "  serial"
                bar = "█" * bar_len + "░" * max(0, 40 - bar_len)
                f.write(f"  {label} |{bar}| {sp:.1f}x\n")
            f.write("\n")

        # Overall summary
        f.write(f"\n{'=' * 80}\n")
        f.write("  OVERALL SUMMARY\n")
        f.write(f"{'=' * 80}\n\n")

        # Find best config per target (by coverage first, then throughput)
        for target in sorted(by_target.keys()):
            configs = by_target[target]
            best = None
            best_score = -1
            for (mode, np_val), rows in configs.items():
                avg_time = sum(r["wall_time"] for r in rows) / len(rows)
                avg_gen = sum(r["generated"] for r in rows) / len(rows)
                avg_edge_cov = sum(r.get("edge_cov", 0) for r in rows) / len(rows)
                total_crashes = sum(r.get("crashes", 0) for r in rows)
                throughput = avg_gen / avg_time if avg_time > 0 else 0
                # Score: prioritize coverage, then throughput
                score = avg_edge_cov * 1000 + throughput
                if score > best_score:
                    best_score = score
                    best = {
                        "mode": mode, "np": np_val,
                        "time": avg_time, "gen": avg_gen,
                        "throughput": throughput,
                        "edge_cov": avg_edge_cov,
                        "crashes": total_crashes,
                    }

            if best:
                info = (f"  {target}: best = {best['mode']} np={best['np']} "
                        f"({format_time(best['time'])}, "
                        f"{best['gen']:.0f} test cases, "
                        f"{best['throughput']:.1f} tc/s")
                if best["edge_cov"] > 0:
                    info += f", edge_cov={best['edge_cov']:.2f}%"
                if best["crashes"] > 0:
                    info += f", crashes={best['crashes']}"
                info += ")\n"
                f.write(info)

        f.write("\n  Report files:\n")
        f.write(f"    Text:  {report_path}\n")
        f.write(f"    CSV:   {csv_path}\n")
        f.write(f"    JSON:  {json_path}\n")
        f.write(f"\n{'=' * 80}\n")

    return report_path


def main():
    parser = argparse.ArgumentParser(
        description="SymCC MPI Parallelization Benchmark"
    )
    parser.add_argument("--symcc", default=None,
                        help="Path to SymCC compiler")
    parser.add_argument("--np-list", default="1,2,4,8",
                        help="Comma-separated process counts (default: 1,2,4,8)")
    parser.add_argument("--targets", default=None,
                        help="Comma-separated target names (default: all)")
    parser.add_argument("--rounds", type=int, default=3,
                        help="Rounds per configuration (default: 3)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="Timeout per run in seconds (default: 60)")
    parser.add_argument("--output", default="benchmark_results",
                        help="Output directory (default: benchmark_results)")
    parser.add_argument("--skip-build", action="store_true",
                        help="Skip compilation step")
    parser.add_argument("--simulation", action="store_true",
                        help="Use gcc instead of SymCC (tests MPI framework only)")
    parser.add_argument("--public", nargs="*", metavar="NAME:BINARY:SEEDDIR",
                        help="Add public benchmark targets. "
                             "With no args: auto-discover compiled targets in benchmark/public/bin/. "
                             "With args: name:binary_path:seed_dir "
                             "e.g., 'file:./benchmark/public/bin/lava/file:./benchmark/public/seeds/lava/file'.")
    parser.add_argument("--no-default", action="store_true",
                        help="Skip built-in benchmarks (maze, parser, etc.), run only public targets")
    parser.add_argument("--no-public", action="store_true",
                        help="Disable auto-discovery of public benchmarks")
    parser.add_argument("--no-coverage", action="store_true",
                        help="Skip coverage measurement (faster but less metrics)")
    parser.add_argument("--hybrid", action="store_true",
                        help="Also run hybrid AFL+SymCC mode (requires AFL-instrumented binaries)")
    parser.add_argument("--afl-only", action="store_true",
                        help="Also run AFL-only baseline (requires AFL-instrumented binaries)")
    parser.add_argument("--timeseries", type=int, default=0, metavar="INTERVAL",
                        help="Enable time-series coverage sampling every N seconds (default: disabled)")

    args = parser.parse_args()

    np_list = [int(x) for x in args.np_list.split(",")]
    if args.targets:
        target_names = args.targets.split(",")
    elif args.no_default:
        target_names = []  # will be populated by public auto-discovery
    else:
        target_names = list(TARGETS.keys())

    output_dir = os.path.abspath(args.output)
    bin_dir = os.path.join(output_dir, "bin")
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 70)
    print("  SymCC MPI Parallelization Benchmark")
    print("=" * 70)
    print(f"  Targets:     {', '.join(target_names)}")
    print(f"  NP values:   {np_list}")
    print(f"  Rounds:      {args.rounds}")
    print(f"  Timeout:     {args.timeout}s per run")
    print(f"  Coverage:    {'enabled' if not args.no_coverage else 'disabled'}")
    print(f"  Output:      {output_dir}")
    print()

    # Build step
    binaries = {}
    if args.no_default:
        print("Step 1: Skipping built-in targets (--no-default)")
        print("-" * 40)
    elif not args.skip_build:
        print("Step 1: Compiling target programs")
        print("-" * 40)

        if args.simulation:
            print("  (Simulation mode: using gcc)")
            binaries = build_targets_gcc(bin_dir)
        else:
            symcc = args.symcc or find_symcc()
            if symcc:
                print(f"  Using SymCC: {symcc}")
                binaries = build_targets(symcc, bin_dir)
            else:
                print("  SymCC not found, falling back to gcc (simulation mode)")
                print("  NOTE: simulation mode tests the MPI framework overhead,")
                print("        not actual symbolic execution performance.")
                binaries = build_targets_gcc(bin_dir)
                args.simulation = True
    else:
        # Find existing binaries
        for name in target_names:
            for suffix in ["_symcc", "_native"]:
                path = os.path.join(bin_dir, f"{name}{suffix}")
                if os.path.isfile(path):
                    binaries[name] = path
                    break

    # Add public benchmark targets.
    # Auto-discover from benchmark/public/bin/ unless --no-public is passed.
    # Explicit --public specs override auto-discovery.
    public_targets = {}
    public_seed_dirs = {}
    if not args.no_public:
        public_specs = list(args.public) if args.public is not None else []

        # Auto-discover from benchmark/public/bin/ if no explicit specs
        if not public_specs:
            pub_bin_dir = PUBLIC_DIR / "bin"
            pub_seed_dir = PUBLIC_DIR / "seeds"
            if pub_bin_dir.is_dir():
                for suite_dir in sorted(pub_bin_dir.iterdir()):
                    if not suite_dir.is_dir():
                        continue
                    # 使用与覆盖率发现相同的命名前缀
                    suite_name = suite_dir.name
                    if suite_name == "lava-m":
                        prefix = "lava-"
                    elif suite_name == "google-fts":
                        prefix = "gfts-"
                    else:
                        prefix = suite_name + "-"
                    for binary in sorted(suite_dir.iterdir()):
                        if binary.is_file() and os.access(str(binary), os.X_OK):
                            bname = binary.name
                            # Skip non-ELF files (wrappers, data)
                            if bname.endswith((".sh", ".mgc", ".txt")):
                                continue
                            seed_candidate = pub_seed_dir / suite_dir.name / bname
                            if seed_candidate.is_dir():
                                if not args.simulation and not _has_symcc_instrumentation(str(binary)):
                                    print(f"  Skipping {bname}: no SymCC instrumentation (gcc-compiled)")
                                    continue
                                target_name = prefix + bname
                                public_specs.append(
                                    f"{target_name}:{binary}:{seed_candidate}"
                                )

        if public_specs:
            print("\n  Adding public benchmark targets:")

        for spec in public_specs:
            parts = spec.split(":")
            if len(parts) != 3:
                print(f"    WARNING: invalid format '{spec}', expected name:binary:seeddir")
                continue
            name, binary_path, seed_path = parts

            # Resolve paths: try as-is first, then relative to SCRIPT_DIR
            binary_path = _resolve_path(binary_path)
            seed_path = _resolve_path(seed_path)

            if not os.path.isfile(binary_path):
                print(f"    WARNING: binary not found: {binary_path}")
                continue
            if not os.path.isdir(seed_path):
                print(f"    WARNING: seed dir not found: {seed_path}")
                continue
            binaries[name] = binary_path
            public_seed_dirs[name] = seed_path
            public_targets[name] = True
            # 仅在用户未指定 --targets 时自动添加到运行列表
            if not args.targets and name not in target_names:
                target_names.append(name)
            print(f"    {name}: {binary_path} (seeds: {seed_path})")

    if not binaries:
        print("\nERROR: No target binaries available.")
        sys.exit(1)

    available_targets = [t for t in target_names if t in binaries]
    print(f"\n  Available targets: {', '.join(available_targets)}")

    # Check MPI
    if not shutil.which("mpirun"):
        print("\nERROR: mpirun not found. Install OpenMPI: apt install openmpi-bin")
        sys.exit(1)

    # Build/discover AFL coverage binaries
    afl_cov_binaries: dict[str, str] = {}
    enable_coverage = not args.no_coverage
    if enable_coverage:
        if not shutil.which("afl-showmap"):
            print("\n  WARNING: afl-showmap not found, disabling coverage measurement")
            enable_coverage = False
        else:
            # 发现 AFL-instrumented 二进制（用于覆盖率测量）
            afl_cov_binaries = discover_afl_coverage_binaries()
            if afl_cov_binaries:
                print(f"\n  Discovered {len(afl_cov_binaries)} AFL coverage binaries:")
                for name in sorted(afl_cov_binaries):
                    print(f"    {name}: {afl_cov_binaries[name]}")

            if not afl_cov_binaries:
                print("  WARNING: no AFL coverage binaries found, disabling coverage")
                enable_coverage = False

    # Prepare seed directories per target
    seed_dirs = {}
    for target in available_targets:
        if target in public_seed_dirs:
            # Public benchmark: use the provided seed directory directly
            seed_dirs[target] = public_seed_dirs[target]
        elif target in TARGETS:
            # Built-in benchmark: copy matching seeds
            prefix = TARGETS[target][2]
            target_seed_dir = os.path.join(output_dir, f"seeds_{target}")
            os.makedirs(target_seed_dir, exist_ok=True)
            for f in SEEDS_DIR.iterdir():
                if f.name.startswith(prefix):
                    shutil.copy2(str(f), target_seed_dir)
            seed_dirs[target] = target_seed_dir

    # Run benchmarks
    print("\nStep 2: Running benchmarks")
    print("-" * 40)

    all_results = []
    all_timeseries = []
    current_run = 0

    for target in available_targets:
        binary = binaries[target]
        seed_dir = seed_dirs[target]

        print(f"\n  Target: {target}")
        print(f"  Binary: {binary}")
        print(f"  Seeds:  {seed_dir} ({len(os.listdir(seed_dir))} files)")

        # Serial baseline
        print("\n  [Serial baseline]")
        for r in range(args.rounds):
            current_run += 1
            work_dir = tempfile.mkdtemp(prefix=f"bench_{target}_serial_r{r}_")

            print(f"    Round {r+1}/{args.rounds}... ", end="", flush=True)
            result = run_serial(binary, target, seed_dir, args.timeout, work_dir,
                                simulate=args.simulation)

            # 使用 AFL 边覆盖率测量
            cov_data = {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0, "crashes": 0}
            if enable_coverage and target in afl_cov_binaries:
                uses_file = TARGETS[target][3] if target in TARGETS else True
                # 将种子文件复制到输出目录，确保覆盖率测量包含种子覆盖
                for sf in os.listdir(seed_dir):
                    sp = os.path.join(seed_dir, sf)
                    dp = os.path.join(result["output_dir"], f"seed_{sf}")
                    if os.path.isfile(sp) and not os.path.exists(dp):
                        shutil.copy2(sp, dp)
                cov_data = measure_coverage_afl(
                    afl_cov_binaries[target], result["output_dir"],
                    uses_file=uses_file
                )

            cov_str = ""
            if enable_coverage and target in afl_cov_binaries:
                cov_str = (f", edge={cov_data['edge_cov']:.2f}% "
                           f"({cov_data['edges_found']}/{cov_data['edges_total']}), "
                           f"crashes={cov_data['crashes']}")
            timeout_str = ""
            if result.get("timed_out"):
                timeout_str = " [TIMEOUT]"
            print(f"time={format_time(result['wall_time'])}, "
                  f"gen={result['generated']}, uniq={result['unique']}, "
                  f"ret={result.get('retcode', '?')}"
                  f"{cov_str}{timeout_str}")

            all_results.append({
                "target": target,
                "mode": "serial",
                "np": 1,
                "round": r + 1,
                "wall_time": result["wall_time"],
                "generated": result["generated"],
                "unique": result["unique"],
                "throughput": result.get("throughput", 0),
                "edge_cov": cov_data.get("edge_cov", 0.0),
                "edges_found": cov_data.get("edges_found", 0),
                "edges_total": cov_data.get("edges_total", 0),
                "crashes": cov_data.get("crashes", 0),
            })

            shutil.rmtree(work_dir, ignore_errors=True)

        # MPI parallel
        for np_val in np_list:
            if np_val < 2:
                # np=1 doesn't make sense for MPI (need master + 1 worker)
                # Use np=2 instead
                actual_np = 2
            else:
                actual_np = np_val

            # Predict master/worker layout (matches compute_roles() in MPI script)
            wpm = 45  # workers_per_master default
            num_avail = actual_np - 1
            if num_avail <= wpm:
                pred_masters, pred_workers = 1, num_avail
            else:
                nm = (num_avail + wpm - 1) // wpm
                nm = min(nm, num_avail // 3)
                nm = max(1, nm)
                pred_masters, pred_workers = nm, actual_np - nm
            if pred_masters > 1:
                print(f"\n  [MPI np={actual_np} "
                      f"({pred_masters} masters, {pred_workers} workers)]")
            else:
                print(f"\n  [MPI np={actual_np} ({pred_workers} workers)]")

            for r in range(args.rounds):
                current_run += 1
                work_dir = tempfile.mkdtemp(
                    prefix=f"bench_{target}_mpi{actual_np}_r{r}_"
                )

                print(f"    Round {r+1}/{args.rounds}... ", end="", flush=True)
                result = run_mpi(
                    binary, target, seed_dir, actual_np,
                    args.timeout, work_dir,
                    simulate=args.simulation
                )

                # 使用 AFL 边覆盖率测量
                cov_data = {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0, "crashes": 0}
                if enable_coverage and target in afl_cov_binaries:
                    uses_file = TARGETS[target][3] if target in TARGETS else True
                    cov_data = measure_coverage_afl(
                        afl_cov_binaries[target], result["output_dir"],
                        uses_file=uses_file
                    )

                cov_str = ""
                if enable_coverage and target in afl_cov_binaries:
                    cov_str = (f", edge={cov_data['edge_cov']:.2f}% "
                               f"({cov_data['edges_found']}/{cov_data['edges_total']}), "
                               f"crashes={cov_data['crashes']}")
                timeout_str = ""
                if result.get("timed_out"):
                    timeout_str = " [TIMEOUT]"
                print(f"time={format_time(result['wall_time'])}, "
                      f"gen={result['generated']}, uniq={result['unique']}, "
                      f"ret={result.get('retcode', '?')}"
                      f"{cov_str}{timeout_str}")
                # Print stderr summary for non-zero retcodes to aid diagnosis
                retcode = result.get("retcode", 0)
                stderr_text = result.get("stderr", "")
                if retcode != 0 and stderr_text and stderr_text != "TIMEOUT":
                    # Show last few meaningful lines
                    err_lines = [ln for ln in stderr_text.strip().splitlines() if ln.strip()]
                    if err_lines:
                        print(f"      stderr: {err_lines[-1][:200]}")

                # Compute throughput speedup and parallel efficiency.
                # Speedup = tc/s ratio vs serial baseline.
                # Efficiency = parallel scaling vs single MPI worker (np=2),
                # 因为串行模式是紧密 Python 循环，与 MPI 执行模型不同，
                # 用串行做基线会导致 efficiency 失真（<1%）。
                # 以 np=2 (1 worker) 为基线能真实反映并行扩展效率。
                mpi_tp = result.get("throughput", 0)
                serial_tp_sum = 0
                serial_count = 0
                for sr in all_results:
                    if sr["target"] == target and sr["mode"] == "serial":
                        serial_tp_sum += sr.get("throughput", 0)
                        serial_count += 1
                if serial_count > 0 and serial_tp_sum > 0:
                    serial_tp_avg = serial_tp_sum / serial_count
                    speedup = mpi_tp / serial_tp_avg if serial_tp_avg > 0 else 0
                    # 并行效率：以 np=2（单 worker）吞吐量为基线
                    workers = result.get("num_workers") or (actual_np - 1)
                    if actual_np == 2:
                        # np=2 本身就是基线，efficiency 定义为 100%
                        efficiency = 100.0
                    else:
                        base_tp_sum = 0
                        base_count = 0
                        for sr in all_results:
                            if (sr["target"] == target and sr["mode"] == "mpi"
                                    and sr["np"] == 2):
                                base_tp_sum += sr.get("throughput", 0)
                                base_count += 1
                        if base_count > 0 and base_tp_sum > 0 and workers > 0:
                            base_tp = base_tp_sum / base_count
                            efficiency = (mpi_tp / base_tp / workers * 100)
                        elif workers > 0:
                            efficiency = (speedup / workers * 100)
                        else:
                            efficiency = 0.0
                elif serial_count > 0:
                    # Serial throughput is 0 — can't compute meaningful speedup
                    speedup = 0.0
                    efficiency = 0.0
                else:
                    speedup = 1.0
                    efficiency = 100.0

                all_results.append({
                    "target": target,
                    "mode": "mpi",
                    "np": actual_np,
                    "round": r + 1,
                    "wall_time": result["wall_time"],
                    "generated": result["generated"],
                    "unique": result["unique"],
                    "throughput": result.get("throughput", 0),
                    "edge_cov": cov_data.get("edge_cov", 0.0),
                    "edges_found": cov_data.get("edges_found", 0),
                    "edges_total": cov_data.get("edges_total", 0),
                    "crashes": cov_data.get("crashes", 0),
                    "speedup": speedup,
                    "efficiency": efficiency,
                    "num_workers": result.get("num_workers") or (actual_np - 1),
                })

                shutil.rmtree(work_dir, ignore_errors=True)

        # Hybrid AFL + SymCC
        if args.hybrid and target in public_targets:
            afl_targets = discover_public_afl_targets()
            afl_binary = afl_targets.get(target)
            if afl_binary:
                for np_val in np_list:
                    actual_np = max(2, np_val)
                    symcc_workers = max(1, actual_np - 2)  # -1 for AFL, -1 for MPI master
                    print(f"\n  [Hybrid AFL+SymCC np={actual_np} "
                          f"(AFL=1, SymCC workers={symcc_workers})]")

                    for r in range(args.rounds):
                        current_run += 1
                        work_dir = tempfile.mkdtemp(
                            prefix=f"bench_{target}_hybrid{actual_np}_r{r}_"
                        )

                        print(f"    Round {r+1}/{args.rounds}... ", end="", flush=True)
                        result = run_hybrid(
                            binary, afl_binary, target, seed_dir,
                            actual_np, args.timeout, work_dir
                        )

                        # 使用 AFL 边覆盖率测量
                        cov_data = {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0, "crashes": 0}
                        if enable_coverage and target in afl_cov_binaries:
                            cov_data = measure_coverage_afl(
                                afl_cov_binaries[target], result["output_dir"],
                                uses_file=True
                            )

                        cov_str = ""
                        if enable_coverage and target in afl_cov_binaries:
                            cov_str = (f", edge={cov_data['edge_cov']:.2f}% "
                                       f"({cov_data['edges_found']}/{cov_data['edges_total']}), "
                                       f"crashes={cov_data['crashes']}")

                        afl_gen = result.get("afl_generated", 0)
                        symcc_int = result.get("symcc_interesting", 0)
                        bitmap_cvg = result.get("afl_bitmap_cvg", "")
                        timeout_str = " [TIMEOUT]" if result.get("timed_out") else ""
                        print(f"time={format_time(result['wall_time'])}, "
                              f"afl={afl_gen}, symcc_interesting={symcc_int}, "
                              f"total={result['generated']}"
                              f"{cov_str}{timeout_str}"
                              f"{' bitmap=' + bitmap_cvg if bitmap_cvg else ''}")

                        all_results.append({
                            "target": target,
                            "mode": "hybrid",
                            "np": actual_np,
                            "round": r + 1,
                            "wall_time": result["wall_time"],
                            "generated": result["generated"],
                            "unique": result["unique"],
                            "throughput": result.get("throughput", 0),
                            "edge_cov": cov_data.get("edge_cov", 0.0),
                            "edges_found": cov_data.get("edges_found", 0),
                            "edges_total": cov_data.get("edges_total", 0),
                            "crashes": cov_data.get("crashes", 0),
                            "speedup": 0,
                            "efficiency": 0,
                            "num_workers": result.get("num_workers", actual_np - 2),
                        })

                        shutil.rmtree(work_dir, ignore_errors=True)
            else:
                print(f"\n  [Hybrid] No AFL binary found for {target}, skipping")

        # AFL-only baseline
        if args.afl_only and target in public_targets:
            afl_targets = discover_public_afl_targets()
            afl_binary = afl_targets.get(target)
            if afl_binary:
                print("\n  [AFL-only baseline]")

                for r in range(args.rounds):
                    current_run += 1
                    work_dir = tempfile.mkdtemp(
                        prefix=f"bench_{target}_aflonly_r{r}_"
                    )

                    print(f"    Round {r+1}/{args.rounds}... ", end="", flush=True)
                    result = run_afl_only(
                        afl_binary, target, seed_dir,
                        args.timeout, work_dir
                    )

                    # 使用 AFL 边覆盖率测量
                    cov_data = {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0, "crashes": 0}
                    if enable_coverage and target in afl_cov_binaries:
                        cov_data = measure_coverage_afl(
                            afl_cov_binaries[target], result["output_dir"],
                            uses_file=True
                        )

                    cov_str = ""
                    if enable_coverage and target in afl_cov_binaries:
                        cov_str = (f", edge={cov_data['edge_cov']:.2f}% "
                                   f"({cov_data['edges_found']}/{cov_data['edges_total']}), "
                                   f"crashes={cov_data['crashes']}")

                    afl_execs = result.get("afl_execs_done", 0)
                    afl_eps = result.get("afl_execs_per_sec", 0)
                    bitmap_cvg = result.get("afl_bitmap_cvg", "")
                    timeout_str = " [TIMEOUT]" if result.get("timed_out") else ""
                    print(f"time={format_time(result['wall_time'])}, "
                          f"queue={result['generated']}, "
                          f"execs={afl_execs}"
                          f"{cov_str}{timeout_str}"
                          f"{' bitmap=' + bitmap_cvg if bitmap_cvg else ''}"
                          f"{f' ({afl_eps:.0f} exec/s)' if afl_eps else ''}")

                    all_results.append({
                        "target": target,
                        "mode": "afl-only",
                        "np": 1,
                        "round": r + 1,
                        "wall_time": result["wall_time"],
                        "generated": result["generated"],
                        "unique": result["unique"],
                        "throughput": result.get("throughput", 0),
                        "edge_cov": cov_data.get("edge_cov", 0.0),
                        "edges_found": cov_data.get("edges_found", 0),
                        "edges_total": cov_data.get("edges_total", 0),
                        "crashes": cov_data.get("crashes", 0),
                        "speedup": 0,
                        "efficiency": 0,
                        "afl_execs_done": afl_execs,
                        "afl_execs_per_sec": afl_eps,
                    })

                    shutil.rmtree(work_dir, ignore_errors=True)
            else:
                print(f"\n  [AFL-only] No AFL binary found for {target}, skipping")

    # Generate report
    print("\n\nStep 3: Generating report")
    print("-" * 40)
    report_path = generate_report(all_results, output_dir)

    # Print the report to stdout
    with open(report_path) as f:
        print(f.read())

    # 保存时间序列数据（如果有）
    if all_timeseries:
        ts_path = os.path.join(output_dir, "coverage_timeseries.csv")
        with open(ts_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["target", "mode", "np", "round",
                             "timestamp_sec", "edge_cov_pct",
                             "edges_found", "edges_total", "total_cases"])
            for entry in all_timeseries:
                for point in entry["timeseries"]:
                    writer.writerow([
                        entry["target"], entry["mode"], entry["np"],
                        entry["round"],
                        point["timestamp_sec"], point["edge_cov"],
                        point["edges_found"], point["edges_total"],
                        point["total_cases"],
                    ])
        print(f"\n  Time-series data saved to: {ts_path}")

    print(f"\nBenchmark complete. Results in: {output_dir}/")


if __name__ == "__main__":
    main()
