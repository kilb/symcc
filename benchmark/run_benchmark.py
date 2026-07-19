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
import typing
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
SYMCC_ROOT = SCRIPT_DIR.parent
TARGETS_DIR = SCRIPT_DIR / "targets"
SEEDS_DIR = SCRIPT_DIR / "seeds"

sys.path.insert(0, str(SYMCC_ROOT / "util"))
from concolic_engine import get_engine  # noqa: E402  concolic 引擎抽象(symcc/symsan)
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

# 混合模式 auto 分配：SymCC concolic worker 数量上限。
# concolic 执行并行扩展性有限（路径探索冗余，见 GenSym/DynamiQ），
# 超过此值后新增 SymCC worker 收益递减，核心更应给 AFL 并行实例。
# 实验依据：np=16 时 7 workers 最优；np=64 时 31 workers 使 libarchive 回退。
SYMCC_WORKER_CAP = 12


def _has_symcc_instrumentation(binary_path, engine=None):
    """检查二进制是否含当前引擎的插桩符号(symcc:__sym_ctor;symsan:__taint/dfsan)。"""
    engine = engine or get_engine()
    syms = engine.detect_symbols
    try:
        result = subprocess.run(
            ["nm", binary_path], capture_output=True, text=True, timeout=10
        )
        count = sum(1 for line in result.stdout.splitlines()
                    if any(s in line for s in syms))
        return count >= MIN_SYMCC_SYMBOLS
    except (OSError, subprocess.SubprocessError, ValueError):
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


def _resolve_engine_compiler(engine):
    """解析当前引擎的编译器路径。symcc→find_symcc();symsan→SYMSAN_KO_CLANG(已构建的 ko-clang)。"""
    if engine.name == "symsan":
        ko = os.environ.get("SYMSAN_KO_CLANG")
        if ko and (shutil.which(ko) or os.path.isfile(ko)):
            return ko
        return None
    return os.environ.get("SYMCC_CC") or find_symcc()


def build_targets(output_dir, engine=None):
    """用当前 concolic 引擎编译所有微目标(symcc→*_symcc / symsan→*_symsan,FastGen 模式)。"""
    engine = engine or get_engine()
    compiler = _resolve_engine_compiler(engine)
    binaries = {}
    os.makedirs(output_dir, exist_ok=True)
    if not compiler:
        hint = " (设 SYMSAN_KO_CLANG 指向 scripts/build_symsan.sh 构建出的 ko-clang)" \
            if engine.name == "symsan" else ""
        print(f"  {engine.name} 编译器未找到{hint}")
        return binaries
    print(f"  引擎={engine.name},编译器={compiler}")
    for name, (source, _, _, _) in TARGETS.items():
        src_path = TARGETS_DIR / source
        bin_path = Path(output_dir) / f"{name}{engine.binary_suffix}"
        cmd, extra_env = engine.build_argv(compiler, str(src_path), str(bin_path))
        env = dict(os.environ)
        env.update(extra_env)
        print(f"  Compiling {name}... ", end="", flush=True)
        start = time.monotonic()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
            elapsed = time.monotonic() - start
            if r.returncode == 0:
                print(f"OK ({elapsed:.1f}s)")
                binaries[name] = str(bin_path)
            else:
                print(f"FAILED (ret={r.returncode})")
                if r.stderr:
                    print(f"    {r.stderr[:200]}")
        except (OSError, subprocess.SubprocessError) as e:
            print(f"FAILED ({e})")
    return binaries


def build_afl_targets(output_dir):
    """用 afl-clang-fast 编译微目标的 AFL 二进制(*_afl),供 hybrid 的 AFL 侧 + showmap 覆盖测量。
    引擎无关(AFL 侧不随 concolic 引擎变);afl-clang-fast 不可用时返回空。"""
    afl_cc = shutil.which("afl-clang-fast")
    binaries = {}
    if not afl_cc:
        return binaries
    os.makedirs(output_dir, exist_ok=True)
    for name, (source, _, _, _) in TARGETS.items():
        out = Path(output_dir) / f"{name}_afl"
        try:
            r = subprocess.run(
                [afl_cc, "-O2", str(TARGETS_DIR / source), "-o", str(out)],
                capture_output=True, text=True, timeout=180,
                env={**os.environ, "AFL_QUIET": "1"})
            if r.returncode == 0 and out.exists():
                binaries[name] = str(out)
        except (OSError, subprocess.SubprocessError):
            pass
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

    except (subprocess.SubprocessError, OSError, ValueError):
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
    except (subprocess.SubprocessError, OSError, ValueError):
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
    except (subprocess.SubprocessError, OSError, ValueError):
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
                         timeout_per_case: int = 5000,
                         extra_args: list[str] | None = None,
                         max_cases: int = 20000) -> dict:
    """使用 afl-showmap -C 测量 AFL 边覆盖率。

    通过 afl-showmap 的批量收集模式（-C -i dir）一次性处理所有测试用例，
    输出边覆盖率百分比。比 gcov/lcov 快得多，且不需要特殊的 coverage 二进制。

    当测试用例数超过 max_cases 时，随机抽样以避免超长测量时间。

    Args:
        afl_binary: AFL-instrumented 二进制路径
        test_case_dir: 包含测试用例的目录
        uses_file: True 表示目标从文件读取输入，False 表示从 stdin
        timeout_per_case: 每个测试用例超时（毫秒）
        max_cases: 最大测量用例数，超过时随机抽样（默认 20000）

    Returns:
        dict with: edge_cov (%), edges_found, edges_total, crashes, total_cases, sampled
    """
    import random

    if not os.path.isdir(test_case_dir):
        return {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0,
                "crashes": 0, "total_cases": 0, "sampled": False}

    test_files = [f for f in os.listdir(test_case_dir)
                  if os.path.isfile(os.path.join(test_case_dir, f))]
    total_cases = len(test_files)
    if total_cases == 0:
        return {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0,
                "crashes": 0, "total_cases": 0, "sampled": False}

    afl_showmap = shutil.which("afl-showmap")
    if not afl_showmap:
        return {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0,
                "crashes": 0, "total_cases": total_cases, "sampled": False}

    # 当 TC 数量超过上限时，创建临时目录并随机抽样
    sampled = False
    actual_dir = test_case_dir
    sample_dir = None
    if max_cases > 0 and total_cases > max_cases:
        sampled = True
        sample_dir = tempfile.mkdtemp(prefix=".afl_cov_sample_")
        # 固定种子 + 排序 → 大语料抽样可复现（覆盖率数值与"最佳配置"排名不再随运行抖动）
        sample_files = random.Random(1337).sample(sorted(test_files), max_cases)
        for f in sample_files:
            src = os.path.join(test_case_dir, f)
            shutil.copy2(src, os.path.join(sample_dir, f))
        actual_dir = sample_dir
        print(f"      [showmap] Sampled {max_cases}/{total_cases} TCs for measurement")

    # mkstemp（非废弃的 mktemp）：原子创建，避免 TOCTOU 竞争；立即关闭 fd，
    # afl-showmap 会用 -o 覆写该文件。
    _out_fd, out_file = tempfile.mkstemp(prefix=".afl_cov_", suffix=".map")
    os.close(_out_fd)
    cmd = [
        afl_showmap,
        "-t", str(timeout_per_case),
        "-m", "none",
        "-C",
        "-i", actual_dir,
        "-o", out_file,
        "--", afl_binary,
    ]
    if extra_args:
        cmd.extend(extra_args)
    # 持久+shmem 目标：afl-showmap 必须不带 @@，经共享内存喂输入并进入持久循环，否则
    # @@ 会让目标落入文件模式/参数错乱、几乎测不到覆盖率（实测 2 边 vs 正确的 399 边）。
    if uses_file and not _afl_binary_uses_shmem(afl_binary):
        cmd.append("@@")

    measure_ok = False  # 是否成功解析到覆盖率（区分"真 0 覆盖"与"showmap 失败"）
    try:
        # 仅按【实际测量】的 TC 数(抽样后 = max_cases)算超时,避免大语料(如 sqlite 百万级 TC)
        # 用 total_cases 导致 batch_timeout 溢出(poll() 的 ms 超 C int → OverflowError: timeout
        # is too large)。再封顶 1h:单批 showmap 不应超过 1h。
        n_measured = max_cases if sampled else total_cases
        batch_timeout = min(3600, max(60, n_measured * 2))
        # 在临时目录中运行 showmap：目标（如 sqlite_fuzzer）会向 CWD 写临时文件，
        # 隔离避免污染仓库/benchmark 目录。
        _shm_cwd = tempfile.mkdtemp(prefix="showmap_cwd_")
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=batch_timeout,
                cwd=_shm_cwd
            )
        finally:
            shutil.rmtree(_shm_cwd, ignore_errors=True)
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
            measure_ok = True
        else:
            # 备用：从 "Captured N tuples" 解析
            m2 = re.search(r"Captured (\d+) tuples \(map size (\d+)", stderr)
            if m2:
                edges_found = int(m2.group(1))
                edges_total = int(m2.group(2))
                if edges_total > 0:
                    edge_cov = edges_found / edges_total * 100.0
                measure_ok = True
        if not measure_ok:
            # 既无 coverage 行也无 Captured 行 → showmap 未正常完成（fork server 握手
            # 失败/启动即崩溃/map 不匹配等）。区别于"真 0 覆盖"：告警 + 返回码，供调用方据
            # measure_ok 丢弃该点，而非把 0.0 当真实数据污染均值/最佳配置选择。
            print(f"[warn] afl-showmap 未产出覆盖率 (rc={result.returncode}); "
                  f"输出尾部: {stderr[-300:]!r}", file=sys.stderr)

    except subprocess.TimeoutExpired:
        edge_cov = 0.0
        edges_found = 0
        edges_total = 0
    except (subprocess.SubprocessError, OSError, ValueError):
        edge_cov = 0.0
        edges_found = 0
        edges_total = 0

    try:
        os.remove(out_file)
    except OSError:
        pass

    # 清理抽样临时目录
    if sample_dir:
        shutil.rmtree(sample_dir, ignore_errors=True)

    return {
        "edge_cov": round(edge_cov, 2),
        "edges_found": edges_found,
        "edges_total": edges_total,
        "crashes": 0,  # afl-showmap -C 不单独报告 crash 数
        "total_cases": total_cases,
        "sampled": sampled,
        "measure_ok": measure_ok,  # False = showmap 失败（非真 0 覆盖），调用方应丢弃
    }


def parse_bitmap_cvg(cvg_str: str) -> float:
    """解析 fuzzer_stats 中的 bitmap_cvg 字符串为浮点百分比。

    例如 "3.14%" -> 3.14, "0.00%" -> 0.0
    """
    if not cvg_str:
        return 0.0
    m = re.search(r"([0-9.]+)%", cvg_str)
    return float(m.group(1)) if m else 0.0


def measure_seed_coverage_afl(afl_binary: str, seed_dir: str,
                               uses_file: bool = True,
                               extra_args: list[str] | None = None) -> dict:
    """使用 afl-showmap 测量纯种子覆盖率（不经过 fuzzing）。

    Returns:
        dict with: edge_cov (%), edges_found, edges_total
    """
    return measure_coverage_afl(afl_binary, seed_dir,
                                uses_file=uses_file,
                                extra_args=extra_args)


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
            if cov_data.get("measure_ok", True):   # 跳过 showmap 失败的采样点，不污染序列
                timeseries.append({
                    "timestamp_sec": round(elapsed, 1),
                    "edge_cov": cov_data["edge_cov"],
                    "edges_found": cov_data["edges_found"],
                    "edges_total": cov_data["edges_total"],
                    "total_cases": cov_data["total_cases"],
                })
        except (OSError, KeyError, ValueError, subprocess.SubprocessError):
            pass
        # 等到下一个 interval 边界（按墙钟对齐，而非样本计数）：采样失败或 showmap 慢于
        # interval 时也照常前进到未来的边界，避免"失败→next_sample 不变→紧忙循环空转"
        # 与"showmap 比 interval 慢→sleep<=0→背靠背采样、节奏塌陷"两种问题。
        now = time.monotonic()
        next_tick = int((now - start_time) // interval) + 1
        sleep_time = start_time + next_tick * interval - now
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

    def run_benchmark() -> None:
        # 线程边界：必须捕获全部异常并转交主线程重新抛出（下方 raise error_container[0]），
        # 否则守护线程内的异常会静默丢失、主线程只看到 result=None。此为线程错误编组
        # 的惯用正确模式，故此处的宽 except 是必要的。
        try:
            result_container[0] = run_fn(**run_kwargs)
        except Exception as e:  # noqa: BLE001 — 线程错误编组，见上
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

    # 与上面的 join 一致用 timeout+60：run_hybrid 的清理最坏情况随 AFL 实例数递增
    # （每个存活进程 SIGTERM 等 10s + SIGKILL 等 5s），固定 60s 在高 np 下可能过短而
    # 丢弃/截断结果。
    bench_thread.join(timeout=timeout + 60)
    result = result_container[0]
    if error_container[0]:
        raise error_container[0]

    return result, timeseries


def _compute_symcc_cpu_list(afl_instances: int, symcc_np: int) -> "str | None":
    """高并行度下为 MPI SymCC 作业计算与 AFL 自动绑核互斥的保留核段（逻辑核高位）。

    AFL 默认自低位向上把每个实例绑到空闲核 [0, afl_instances)；把 symcc_np 个 MPI rank
    钉到高位 [total-symcc_np, total)，两者互不重叠，消除 SymCC 子进程在 AFL 已绑核上
    漂移造成的核争用与迁移（本机单 NUMA，无跨节点局部性考量）。低并行度核充裕、钉核反而
    妨碍调度器均衡，返回 None 表示不钉核（保持默认 oversubscribe + 自动绑核行为）。
    传给 mpi_fuzzing_helper 的 SYMCC_CPU_LIST，由各 rank 自钉（覆盖 OpenMPI 启动绑核）。"""
    total = os.cpu_count() or 0
    # 门槛：AFL 实例 < 16（并行度低）时核充裕、无争用 → 不钉核
    if total < 8 or symcc_np <= 0 or afl_instances < 16:
        return None
    # 需容纳 AFL[0,afl_instances) 与 SymCC[total-symcc_np,total) 互斥布局
    if afl_instances + symcc_np > total:
        return None
    base = total - symcc_np
    return ",".join(str(c) for c in range(base, total))


_shmem_detect_cache: "dict[tuple[str, float, int], bool]" = {}


def _afl_binary_uses_shmem(afl_binary: str) -> bool:
    """检测 AFL 目标是否为持久模式（→ 不带 @@ 运行；且 cmplog 伴随二进制须同为持久）。

    判据用 afl-fuzz 自身识别持久模式的标记字符串 ##SIG_AFL_PERSISTENT##（由 __AFL_LOOP
    宏注入二进制），这与 afl-fuzz 的 check_binary 完全一致，是可靠信号。

    切勿改用 __afl_sharedmem_fuzzing 符号判断：该符号由 AFL 运行时在 *fork 与持久* 二进制
    中都定义（binding/section/静态值静态不可辨），会把 fork 模式的 cmplog 伴随二进制（本仓库
    pcre2-cmplog / sqlite-cmplog 实测 ##SIG_AFL_PERSISTENT## 缺失=fork）误判为持久 → 持久性
    匹配门禁放行 cmplog → 持久主二进制 + fork cmplog 不匹配 → afl-fuzz "Fork server handshake
    failed"、所有 hybrid/afl-only 轮次覆盖率归零。用 SIG 字符串则正确判 cmplog 为非持久、
    优雅关闭 cmplog（afl 正常跑）。<binary>.forkmode 标记可强制判为非持久。
    结果按 (路径, mtime, size) 记忆化；读文件/异常一律返回 False（安全默认：带 @@）。"""
    if os.path.exists(afl_binary + ".forkmode"):
        return False
    try:
        st = os.stat(afl_binary)
    except OSError:
        return False
    key = (afl_binary, st.st_mtime, st.st_size)
    cached = _shmem_detect_cache.get(key)
    if cached is not None:
        return cached
    verdict = False
    try:
        with open(afl_binary, "rb") as f:
            verdict = b"##SIG_AFL_PERSISTENT##" in f.read()
    except OSError:
        verdict = False
    _shmem_detect_cache[key] = verdict
    return verdict


def _link_or_copy(src: str, dst: str) -> None:
    """将 src 硬链接到 dst（同一文件系统近乎零成本）；跨盘/已存在等失败时退回 copy2。

    覆盖率测量的合并语料只读，硬链接即可，避免对数万 queue 文件逐个整块复制
    （数万次 open+read+write），大幅降低合并阶段的墙钟与磁盘 I/O。
    """
    try:
        os.link(src, dst)
    except OSError:
        # 跨文件系统 / 目标已存在 / 不支持硬链接 → 退回复制（copy2 覆盖既有）
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            # 硬链接与复制双双失败（源消失/磁盘满/权限）→ 该文件缺席会低估合并语料的
            # 覆盖率；告警而非静默吞掉，便于发现数据质量问题。
            print(f"[warn] _link_or_copy 跳过 {src}: {e}", file=sys.stderr)


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
        except (OSError, subprocess.SubprocessError):
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


def run_serial(binary: str, target_name: str, seed_dir: str, timeout: int,
               work_dir: str, simulate: bool = False):
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
        except (OSError, subprocess.SubprocessError, ValueError):
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


def run_mpi(binary: str, target_name: str, seed_dir: str, np: int, timeout: int,
            work_dir: str, simulate: bool = False,
            extra_args: list[str] | None = None):
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
    if extra_args:
        cmd.extend(extra_args)

    if uses_file:
        cmd.append("@@")

    # Set up environment: auto-detect magic.mgc for 'file' binary
    env = None
    magic_path = os.path.join(os.path.dirname(binary), "magic.mgc")
    if os.path.isfile(magic_path):
        env = os.environ.copy()
        env["MAGIC"] = magic_path

    # 用 Popen + start_new_session：hard-timeout 时可 killpg 整个进程组回收，否则
    # mpirun 派生的 worker/SymCC 子进程会成为孤儿继续占满所有核心，污染后续轮次的
    # 计时/吞吐（对照 run_serial 的处理）。stdout 重定向到日志文件（而非 PIPE）：
    # MPI master 会一直运行到 wall-timeout，PIPE 写满 64KB 会死锁，且 communicate
    # 超时会丢弃已产出的统计。stderr 并入日志便于排查。
    mpi_log_path = os.path.join(work_dir, f"mpi_np{np}.log")
    start = time.monotonic()
    timed_out_hard = False
    with open(mpi_log_path, "wb") as _log:
        try:
            proc = subprocess.Popen(
                cmd, stdout=_log, stderr=subprocess.STDOUT,
                start_new_session=True, env=env,
            )
        except (OSError, subprocess.SubprocessError) as e:
            # mpirun 不在 PATH / fork 资源耗尽等 → 本轮记为失败，不让整个 sweep 崩溃退出
            print(f"[error] run_mpi 无法启动 mpirun（{e}）；本轮记为失败",
                  file=sys.stderr)
            proc = None
        if proc is None:
            retcode = -1
        else:
            try:
                proc.wait(timeout=timeout + 30)
                retcode = proc.returncode
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        proc.kill()
                retcode = -1
                timed_out_hard = True

    elapsed = time.monotonic() - start
    # 读回日志（含 stdout+stderr）供统计解析；hard-timeout 也能拿到已产出的部分统计
    try:
        with open(mpi_log_path, "r", errors="replace") as _f:
            stdout = _f.read()
    except OSError:
        stdout = ""
    stderr = "TIMEOUT" if timed_out_hard else ""
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


def discover_public_hfuzz_targets() -> dict[str, str]:
    """发现 honggfuzz-instrumented 二进制（public/bin/*-hfuzz/，用 hfuzz-clang 构建）。"""
    hf: dict[str, str] = {}
    pub_bin = PUBLIC_DIR / "bin"
    if not pub_bin.is_dir():
        return hf
    suite_prefixes = {"google-fts": "gfts-", "lava-m": "lava-"}
    for suite_dir in sorted(pub_bin.iterdir()):
        if not suite_dir.is_dir() or not suite_dir.name.endswith("-hfuzz"):
            continue
        suite_base = suite_dir.name[:-6]  # 去掉 -hfuzz
        prefix = suite_prefixes.get(suite_base, suite_base + "-")
        for binary in sorted(suite_dir.iterdir()):
            if binary.is_file() and not binary.suffix and os.access(str(binary), os.X_OK):
                hf[prefix + binary.name] = str(binary)
    return hf


def _read_symcc_stats(symcc_dir: str) -> tuple[int, int, int, int] | None:
    """读取 master 写出的 .symcc_stats：(interesting, generated, edges, active)。"""
    try:
        with open(os.path.join(symcc_dir, ".symcc_stats")) as f:
            parts = f.read().split()
        return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
    except (IOError, OSError, ValueError, IndexError):
        return None


def _read_afl_edges(afl_out_dir: str) -> int:
    """汇总所有 AFL 实例 fuzzer_stats 的 edges_found（取最大，queue 已同步）。"""
    best = 0
    try:
        for inst in os.listdir(afl_out_dir):
            if not inst.startswith("fuzzer"):
                continue
            sp = os.path.join(afl_out_dir, inst, "fuzzer_stats")
            try:
                with open(sp) as f:
                    for line in f:
                        k, _, v = line.partition(":")
                        if k.strip() == "edges_found":
                            best = max(best, int(v.strip()))
                            break
            except (IOError, OSError, ValueError):
                pass
    except OSError:
        pass
    return best


def _adaptive_controller(mpi_proc: "subprocess.Popen | None",
                         afl_procs: "list[subprocess.Popen]",
                         symcc_dir: str, afl_out_dir: str, np: int,
                         timeout: int, start: float, symcc_worker_launch: int,
                         next_afl_idx: int,
                         spawn_afl: "typing.Callable[[int], subprocess.Popen]"
                         ) -> None:
    """KRAKEN 风格运行时自适应分配控制器。

    信号：SymCC 近窗 useful 比率（interesting/generated 增量）——直接度量 concolic
    核心是否被有效利用。高 → 保留/增加 SymCC worker（unpark + 杀 1 个 AFL）；
    低 → 停泊 SymCC（park + 增 1 个 AFL），把核心让给扩展性更好的 AFL。
    死区滞回避免抖动。K（活跃 SymCC worker 数）∈ [2, symcc_worker_launch]。
    """
    ctrl_file = os.path.join(symcc_dir, ".active_workers")
    K_MIN = 2
    K_MAX = symcc_worker_launch
    STEP = 2
    INTERVAL = 25.0             # 贴近 AFL fuzzer_stats 更新节奏，降低读数噪声
    MARGIN = 1.25                # 需超过对方每核产出的 25% 才移动（滞回）

    # 等待 helper 创建 symcc_dir 后再写控制文件（预建会触发其 resume 退出）
    for _ in range(150):
        if os.path.isdir(symcc_dir):
            break
        if mpi_proc.poll() is not None:
            return
        time.sleep(0.2)

    # 等待超时仍未出现 symcc_dir（helper 未就绪/已挂）→ 直接返回：不向一个不会读控制文件
    # 的作业写控制文件，更不要为它铺满 AFL 实例（那些实例随后被统一清理，只是白耗核）。
    if not os.path.isdir(symcc_dir):
        return

    # 初始 K = 封顶默认（起步即处于已验证的良好稳态）
    K = max(K_MIN, min(K_MAX, SYMCC_WORKER_CAP))
    try:
        with open(ctrl_file + ".tmp", "w") as f:
            f.write(str(K))
        os.replace(ctrl_file + ".tmp", ctrl_file)
    except OSError:
        pass
    # 初始把 AFL 实例补足到 A_target = np-1-K
    afl_idx = next_afl_idx
    target_afl = max(1, np - 1 - K)
    while len(afl_procs) < target_afl:
        afl_procs.append(spawn_afl(afl_idx))
        afl_idx += 1

    # 等 helper 首次写出 .symcc_stats 再取基线（它每 ~5s 写一次；文件未就绪时 prev 为
    # None）。否则首个调节 tick 会把"从 0 到首读"的整段累计当成一次 INTERVAL 增量，单侧
    # 夸大 SymCC 每核产出、令第一次再平衡决策失真。有界重试，最多 ~5s。
    prev = _read_symcc_stats(symcc_dir)
    for _ in range(50):
        if prev is not None or mpi_proc.poll() is not None:
            break
        time.sleep(0.1)
        prev = _read_symcc_stats(symcc_dir)
    prev_i = prev[0] if prev else 0
    prev_afl = _read_afl_edges(afl_out_dir)
    last_adjust = time.monotonic()
    history = []

    while True:
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0 or mpi_proc.poll() is not None:
            break
        time.sleep(min(2.0, max(0.1, remaining)))
        now = time.monotonic()
        if now - last_adjust < INTERVAL:
            continue
        last_adjust = now

        st = _read_symcc_stats(symcc_dir)
        if st is None:
            continue
        i_cum = st[0]
        afl_edges = _read_afl_edges(afl_out_dir)
        # 每核边际产出：SymCC 用 interesting 增量（=对共享覆盖新增的贡献，
        # AFL 覆盖不到的格式约束越多则越高）；AFL 用 edges_found 增量。
        d_symcc = max(0, i_cum - prev_i)
        d_afl = max(0, afl_edges - prev_afl)
        prev_i, prev_afl = i_cum, afl_edges
        # 按实际在跑的 AFL 实例数归一（而非目标 np-1-K）：ramp-up 或 spawn 失败时二者
        # 不等，用实际值才是真实的每实例产出。注：两侧信号量纲不同（SymCC 为 interesting
        # 计数增量、AFL 为共享 edges 饱和增量），MARGIN 滞回吸收此启发式的不精确。
        A = max(1, len(afl_procs))
        symcc_per_core = d_symcc / max(1, K)
        afl_per_core = d_afl / A

        old_K = K
        # 谁的每核产出更高就把核心给谁（带滞回 margin）
        if symcc_per_core > afl_per_core * MARGIN and K < K_MAX:
            K = min(K_MAX, K + STEP)
        elif afl_per_core > symcc_per_core * MARGIN and K > K_MIN:
            K = max(K_MIN, K - STEP)
        history.append((round(symcc_per_core, 2), round(afl_per_core, 2), K))

        if K != old_K:
            try:
                with open(ctrl_file + ".tmp", "w") as f:
                    f.write(str(K))
                os.replace(ctrl_file + ".tmp", ctrl_file)
            except OSError:
                pass
            # 调整 AFL 实例数以填满 np 预算：A = np-1-K
            target_afl = max(1, np - 1 - K)
            while len(afl_procs) < target_afl:      # 扩容
                afl_procs.append(spawn_afl(afl_idx))
                afl_idx += 1
            while len(afl_procs) > target_afl and len(afl_procs) > 1:  # 缩容（保留主实例）
                victim = afl_procs.pop()
                try:
                    os.killpg(victim.pid, signal.SIGTERM)
                    # 必须 wait() 回收，否则被杀实例长期滞留为僵尸进程；
                    # SIGTERM 未及时退出则升级 SIGKILL。
                    try:
                        victim.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(victim.pid, signal.SIGKILL)
                        victim.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            print(f"      [adaptive] symcc/core={symcc_per_core:.2f} "
                  f"afl/core={afl_per_core:.2f} -> SymCC workers K={K}, "
                  f"AFL={len(afl_procs)}", flush=True)

    if history:
        ks = [h[2] for h in history]
        print(f"      [adaptive] K trajectory: {ks}", flush=True)


def run_hybrid(symcc_binary: str, afl_binary: str, target_name: str,
               seed_dir: str, np: int, timeout: int, work_dir: str,
               extra_args: list[str] | None = None,
               cmplog_binary: str | None = None,
               afl_instances: int = 1,
               adaptive: bool = False,
               honggfuzz_binary: str | None = None,
               grimoire: bool = False,
               symcc_diversity: bool = False,
               symcc_density_balance: bool = False) -> dict:
    """运行 AFL + MPI SymCC 混合模式。

    并行核心分配（受 KRAKEN ISSTA'25 / Boian 2024 启发）：
    - 不再固定「1 个 AFL + (np-1) 个 SymCC」这种把绝大多数核心给
      concolic 的反常配比。concolic 在混合模糊测试中应是**少数派辅助**。
    - afl_instances 个 AFL 实例（AFL++ 并行模式：1 主 -M + 其余 -S 从），
      SymCC 分到剩余核心。AFL++ 会在各实例间自动同步 queue。

    1. 启动 afl_instances 个 AFL 实例（并行模式，power schedule 多样化）
    2. 等待主实例 fuzzer01 初始化
    3. 启动 MPI SymCC workers (mpi_fuzzing_helper.py)，喂给 fuzzer01
    4. 等待 timeout
    5. 终止所有进程
    6. 收集所有实例 + SymCC 的输出，测量并集覆盖率
    """
    afl_out_dir = os.path.join(work_dir, "afl_out")
    os.makedirs(afl_out_dir, exist_ok=True)
    # 目标进程的工作目录：某些目标（如 sqlite_fuzzer）会向 CWD 写入临时文件
    # （DB、journal 等）。隔离到 work_dir 下的临时目录，避免污染仓库/benchmark。
    target_cwd = os.path.join(work_dir, "target_cwd")
    os.makedirs(target_cwd, exist_ok=True)

    # 异构集成成员：honggfuzz（不同引擎/反馈/变异，研究证实是唯一值得加的真异构引擎）。
    # 它把发现的语料写入 hf_out，AFL 主实例通过 -F 导入（见下方 foreign_dirs）。
    honggfuzz_proc = None
    honggfuzz_out = os.path.join(work_dir, "honggfuzz_out")
    if honggfuzz_binary and shutil.which("honggfuzz"):
        os.makedirs(honggfuzz_out, exist_ok=True)
        hf_cwd = os.path.join(work_dir, "hf_cwd")
        os.makedirs(hf_cwd, exist_ok=True)
        hf_cmd = ["honggfuzz", "-i", seed_dir, "-o", honggfuzz_out,
                  "-n", "2", "--exit_upon_crash",
                  "--", honggfuzz_binary, "___FILE___"]
        honggfuzz_proc = subprocess.Popen(
            hf_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, cwd=hf_cwd)
        print(f"      Starting honggfuzz ensemble member -> {honggfuzz_out}")

    # GRIMOIRE 风格结构合成生成器：监视 AFL 主实例 queue，重组出结构有效的新输入写入
    # grimoire_out，AFL 主实例 -F 导入。纯 CPU，直击结构化解析器的"结构有效性"瓶颈。
    grimoire_proc = None
    grimoire_out = os.path.join(work_dir, "grimoire_out")
    if grimoire:
        os.makedirs(grimoire_out, exist_ok=True)
        gscript = os.path.join(SYMCC_ROOT, "util", "grimoire_gen.py")
        gcmd = [sys.executable, "-u", gscript,
                "--corpus", os.path.join(afl_out_dir, "fuzzer01", "queue"),
                "--out", grimoire_out,
                "--extras", os.path.join(afl_out_dir, "symcc01", "extras"),
                "--interval", "8", "--batch", "400",
                "--afl-binary", afl_binary]  # 启用覆盖率引导的泛化（gap 检测）
        grimoire_proc = subprocess.Popen(
            gcmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        print(f"      Starting GRIMOIRE synthesizer -> {grimoire_out}")

    afl_instances = max(1, afl_instances)
    # 至少给 SymCC 留 2 个 rank（1 master + 1 worker）
    afl_instances = min(afl_instances, max(1, np - 2))

    # 自适应模式：以"SymCC 富余"方式启动，使控制器可通过停泊 worker 遍历
    # [SymCC 重 ... AFL 重] 全谱。W_launch = SymCC worker 数上限（≈np/2，覆盖
    # SymCC 友好目标所需）；初始 AFL 实例数取小基数，控制器再动态扩容。
    symcc_worker_launch = 0
    if adaptive:
        symcc_worker_launch = max(2, np // 2)              # 启动的 SymCC worker 数
        afl_instances = max(1, np - 1 - symcc_worker_launch)  # 初始 AFL 基数

    afl_env = os.environ.copy()
    afl_env["AFL_NO_UI"] = "1"  # 无 UI 模式，避免终端干扰
    afl_env["AFL_SKIP_CPUFREQ"] = "1"
    afl_env["AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES"] = "1"
    afl_env["AFL_AUTORESUME"] = "1"

    # 集成（EnFuzz 风格）配置多样性：与其让从实例只换 power schedule，不如给每个
    # 从实例一个差异明显的"策略画像"（MOpt vs 香草、explore vs exploit、CmpLog 深浅、
    # 不同 havoc/trim 行为）——单引擎内尽量逼近异构集成的探索多样性。
    # profile: (power_schedule, use_mopt, use_cmplog, extra_env)
    ensemble_profiles = [
        ("explore", False, True,  {}),                              # CmpLog + 广探索
        ("fast",    True,  False, {}),                              # MOpt + fast
        ("exploit", False, True,  {"AFL_DISABLE_TRIM": "1"}),       # 深挖 + 不裁剪
        ("rare",    False, False, {"AFL_EXPAND_HAVOC_NOW": "1"}),   # 稀有边 + 强 havoc
        ("coe",     True,  True,  {}),                              # MOpt + CmpLog + coe
        ("seek",    False, False, {"AFL_KEEP_TIMEOUTS": "1"}),      # seek + 保留超时
        ("mmopt",   True,  False, {}),                              # MOpt + mmopt
        ("lin",     False, True,  {}),                              # CmpLog + lin
        ("quad",    False, False, {}),                              # 香草 + quad
    ]

    # AFL++ 外部（异构）引擎共享目录：honggfuzz 等把种子写到这些目录，主实例通过
    # -F 导入（需 -M）。honggfuzz 启用时自动加入其 -o 目录。
    foreign_dirs = os.environ.get("AFL_FOREIGN_DIRS", "")
    _ext_dirs = [d for d, on in ((honggfuzz_out, honggfuzz_proc is not None),
                                 (grimoire_out, grimoire_proc is not None)) if on]
    if _ext_dirs:
        foreign_dirs = ":".join(_ext_dirs + ([foreign_dirs] if foreign_dirs else []))

    # 检测 AFL 目标是否为持久模式 + 共享内存（dual-mode）：若是，afl-fuzz 不带 @@ 运行，
    # 经 shmem 喂输入并进入 __AFL_LOOP 持久循环（实测 ~35x 吞吐）；否则保持带 @@（fork/文件）。
    afl_persistent = _afl_binary_uses_shmem(afl_binary)
    if afl_persistent:
        print("      AFL target is persistent+shmem -> feeding via shared memory "
              "(no @@); expect large exec/s gain")
    # cmplog 二进制的持久性必须与主二进制一致，否则 afl-fuzz 在 fork server 握手时
    # PROGRAM ABORT。不一致则跳过 cmplog（保证不崩，代价是失去该目标的 RedQueen）。
    cmplog_ok = bool(cmplog_binary) and (
        _afl_binary_uses_shmem(cmplog_binary) == afl_persistent)
    if cmplog_binary and not cmplog_ok:
        print(f"      WARNING: cmplog binary persistence != main "
              f"(main persistent={afl_persistent}) -> disabling cmplog to avoid "
              f"fork-server-handshake abort (rebuild cmplog persistent to re-enable)")

    afl_procs: list[subprocess.Popen] = []

    def _spawn_afl(idx: int) -> subprocess.Popen:
        """启动第 idx 个 AFL 实例（idx=0 为主 -M，其余为差异化策略的从 -S）。"""
        fuzzer_name = f"fuzzer{idx + 1:02d}"
        is_master = (idx == 0)
        inst_env = dict(afl_env)
        cmd = ["afl-fuzz"]
        if is_master:
            cmd += ["-M", fuzzer_name]
            # 主实例导入外部异构引擎的语料（集成共享）
            for d in foreign_dirs.split(":"):
                if d and os.path.isdir(d):
                    cmd += ["-F", d]
            use_cmplog = True
        else:
            prof = ensemble_profiles[(idx - 1) % len(ensemble_profiles)]
            sched, use_mopt, use_cmplog, extra_env = prof
            cmd += ["-S", fuzzer_name, "-p", sched]
            if use_mopt:
                cmd += ["-L", "0"]  # 启用 MOpt 变异调度
            inst_env.update(extra_env)
        cmd += ["-i", seed_dir, "-o", afl_out_dir, "-m", "none"]
        if cmplog_ok and use_cmplog:
            cmd += ["-c", cmplog_binary, "-l", "2AT"]
        cmd += ["--", afl_binary]
        if extra_args:
            cmd += extra_args
        if not afl_persistent:
            cmd.append("@@")   # 非持久目标：文件输入
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True, env=inst_env,
                                cwd=target_cwd)

    for i in range(afl_instances):
        afl_procs.append(_spawn_afl(i))
    # 下一个可用的 AFL 实例序号（控制器动态扩容时递增，名字不复用）
    next_afl_idx = afl_instances

    print(f"      Starting {afl_instances} AFL instance(s) "
          f"(1 master + {afl_instances - 1} secondary)"
          f"{' [ADAPTIVE]' if adaptive else ''}...")
    # 向后兼容：保留 afl_proc 指向主实例
    afl_proc = afl_procs[0]

    # 等待 AFL 初始化（fuzzer_stats 文件出现）
    fuzzer_dir = os.path.join(afl_out_dir, "fuzzer01")
    stats_path = os.path.join(fuzzer_dir, "fuzzer_stats")
    start = time.monotonic()
    afl_ready = False
    while time.monotonic() - start < 30:
        if os.path.isfile(stats_path):
            afl_ready = True
            break
        # 检查主 AFL 实例是否崩溃
        if afl_proc.poll() is not None:
            print(f"      AFL exited early (ret={afl_proc.returncode})")
            # 清理其余已启动的实例 + 异构集成成员（honggfuzz / GRIMOIRE），避免泄漏
            for p in (afl_procs
                      + [q for q in (honggfuzz_proc, grimoire_proc)
                         if q is not None]):
                if p.poll() is None:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                        p.wait(timeout=5)  # 回收，避免僵尸进程滞留
                    except (OSError, subprocess.TimeoutExpired):
                        pass
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

    # 启动 MPI SymCC workers：剩余核心分给 SymCC（至少 2 rank）
    if adaptive:
        symcc_np = symcc_worker_launch + 1   # +1 master
    else:
        symcc_np = max(2, np - afl_instances)
    mpi_cmd = [
        "mpirun", "--allow-run-as-root", "--oversubscribe",
        "-np", str(symcc_np),
        "python3", "-u", str(MPI_FUZZING_SCRIPT),
        "-a", "fuzzer01",
        "-o", afl_out_dir,
        "-n", "symcc01",
        "--save-all", symcc_all_dir,
    ]
    # GRIMOIRE 高价值输入直连 SymCC 反馈队列（结构合成 × concolic 协同）
    if grimoire_proc is not None:
        mpi_cmd += ["--grimoire-feed", grimoire_out]
    mpi_cmd += ["--", symcc_binary]
    if extra_args:
        mpi_cmd.extend(extra_args)
    mpi_cmd.append("@@")

    print(f"      Starting MPI SymCC (np={symcc_np})...")
    mpi_env = os.environ.copy()
    mpi_env["PYTHONUNBUFFERED"] = "1"
    # 细粒度并行分解（opt-in）：每 worker 不同策略 + 不相交符号化区间，降低下游冗余、
    # 突破 ~12 worker 饱和点（尤其对大/难目标）。P×S 个不重复工作格，见 mpi_fuzzing_helper。
    if symcc_diversity or symcc_density_balance:
        mpi_env["SYMCC_WORKER_DIVERSITY"] = "1"
        print("      SymCC worker diversity ON (per-worker strategy + disjoint "
              "focus-byte regions)")
    if symcc_density_balance:
        # 需密度剖析版 runtime + 用该 runtime 编译的 symcc 目标；否则 profile 无输出，
        # _build_work_items 自动退回等宽分区（安全）。
        mpi_env["SYMCC_DENSITY_BALANCE"] = "1"
        print("      SymCC density-balanced partitioning ON (profile hot bytes -> "
              "equal-density regions)")
    # CPU 亲和性（高并行度）：把 MPI SymCC ranks 钉到与 AFL 自动绑核互斥的保留高位核段，
    # 消除 SymCC 子进程在 AFL 已绑核上漂移造成的争用/迁移。低并行度返回 None（不钉核）。
    _cpu_list = _compute_symcc_cpu_list(afl_instances, symcc_np)
    if _cpu_list:
        mpi_env["SYMCC_CPU_LIST"] = _cpu_list
        _cs = _cpu_list.split(",")
        print(f"      CPU affinity: SymCC ranks pinned to cores "
              f"[{_cs[0]}-{_cs[-1]}], AFL auto-binds low cores "
              f"[0-{afl_instances - 1}] (disjoint)")
    symcc_dir = os.path.join(afl_out_dir, "symcc01")
    mpi_stdout_bytes = b""
    # 两种模式都把 master stdout 重定向到日志文件（而非 PIPE）：helper 自身不会退出，
    # 会一直运行到被 SIGTERM。PIPE 在 64KB 写满后会死锁 master；且 communicate(timeout)
    # 因 helper 永不自退而必然超时、丢弃已产出的 stdout（导致 symcc_interesting 恒为 0）。
    # 写文件无容量上限、事后可完整读回。
    mpi_log_path = os.path.join(work_dir, "mpi_master.log")
    mpi_log_fh = open(mpi_log_path, "wb")
    mpi_proc = None
    try:
        try:
            mpi_proc = subprocess.Popen(
                mpi_cmd, stdout=mpi_log_fh, stderr=subprocess.DEVNULL,
                start_new_session=True, env=mpi_env, cwd=target_cwd,
            )
        except (OSError, subprocess.SubprocessError) as e:
            # MPI helper 启动失败时不抛出（否则跳过下方统一清理 → 已启动的 AFL/ensemble
            # 进程沦为占满 CPU 的孤儿）；置 None，走正常清理回收所有兄弟进程。
            print(f"[error] run_hybrid 无法启动 MPI helper（{e}）；"
                  f"将回收已启动的 AFL/ensemble 进程", file=sys.stderr)
        if mpi_proc is not None and adaptive:
            # 不预建 symcc_dir：helper 若发现其已存在会判定为 resume 并立即退出。
            # 控制器会等待 helper 创建该目录后再写控制文件。
            _adaptive_controller(
                mpi_proc, afl_procs, symcc_dir, afl_out_dir, np, timeout, start,
                symcc_worker_launch, next_afl_idx, _spawn_afl,
            )
        elif mpi_proc is not None:
            # helper 不会自退，让它运行满剩余时间，到时由下方统一 SIGTERM 终止；
            # 期间若意外早退则提前结束等待。
            deadline = time.monotonic() + max(
                10, timeout - (time.monotonic() - start))
            while time.monotonic() < deadline and mpi_proc.poll() is None:
                time.sleep(1.0)
    finally:
        # try/finally 确保控制器/等待循环即使抛异常也不泄漏文件句柄
        try:
            mpi_log_fh.close()
        except OSError:
            pass
    try:
        with open(mpi_log_path, "rb") as _f:
            mpi_stdout_bytes = _f.read()
    except OSError:
        pass

    # 终止进程：MPI + 所有 AFL 实例 + honggfuzz + GRIMOIRE
    for proc in (afl_procs
                 + [p for p in (mpi_proc, honggfuzz_proc, grimoire_proc)
                    if p is not None]):
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
    except (AttributeError, UnicodeDecodeError):
        pass

    # 收集结果
    # AFL 生成的测试用例分布在各实例的 fuzzerNN/queue/
    # SymCC 反馈的用例在 symcc01/queue/
    symcc_queue = os.path.join(afl_out_dir, "symcc01", "queue")
    # 枚举所有 AFL 实例的 queue 目录
    afl_queues = []
    try:
        for entry in sorted(os.listdir(afl_out_dir)):
            if entry.startswith("fuzzer"):
                q = os.path.join(afl_out_dir, entry, "queue")
                if os.path.isdir(q):
                    afl_queues.append(q)
    except OSError:
        pass

    afl_count = sum(count_output_files(q) for q in afl_queues)

    # 合并所有测试用例到一个目录用于覆盖率测量
    combined_dir = os.path.join(work_dir, "combined_output")
    os.makedirs(combined_dir, exist_ok=True)

    # 复制种子（硬链接，只读合并语料无需整块复制）
    for f in os.listdir(seed_dir):
        src = os.path.join(seed_dir, f)
        if os.path.isfile(src):
            _link_or_copy(src, os.path.join(combined_dir, f"seed_{f}"))

    # 复制所有 AFL 实例的 queue（跨实例文件名可能重复，加实例前缀去重）
    for qi, afl_queue in enumerate(afl_queues):
        inst = os.path.basename(os.path.dirname(afl_queue))
        for f in os.listdir(afl_queue):
            src = os.path.join(afl_queue, f)
            if os.path.isfile(src):
                _link_or_copy(src, os.path.join(combined_dir, f"afl_{inst}_{f}"))

    # 复制 SymCC queue (afl-showmap 过滤后的 interesting)
    if os.path.isdir(symcc_queue):
        for f in os.listdir(symcc_queue):
            src = os.path.join(symcc_queue, f)
            if os.path.isfile(src):
                _link_or_copy(src, os.path.join(combined_dir, f"symcc_{f}"))

    # 复制所有 SymCC 输出（未过滤）— 这些可能有 lcov 覆盖率提升
    symcc_all_count = 0
    if os.path.isdir(symcc_all_dir):
        for f in os.listdir(symcc_all_dir):
            src = os.path.join(symcc_all_dir, f)
            if os.path.isfile(src):
                dest = os.path.join(combined_dir, f"symcc_all_{f}")
                if not os.path.exists(dest):
                    _link_or_copy(src, dest)
                    symcc_all_count += 1

    total_generated = afl_count + symcc_all_count

    # 解析 MPI 输出中的 interesting count
    symcc_interesting = 0
    m = re.search(r"(\d+) interesting", mpi_stdout)
    if m:
        symcc_interesting = int(m.group(1))

    # 从各实例 fuzzer_stats 聚合 AFL 指标：
    # execs 累加（总吞吐），bitmap_cvg/edges 取最大（各实例 queue 已同步，覆盖近似一致）
    afl_bitmap_cvg = ""
    afl_execs_done = 0
    afl_execs_per_sec = 0.0
    afl_edges_found = 0
    afl_total_edges = 0
    _best_bitmap = -1.0
    for inst_dir in sorted(os.listdir(afl_out_dir)) if os.path.isdir(afl_out_dir) else []:
        if not inst_dir.startswith("fuzzer"):
            continue
        sp = os.path.join(afl_out_dir, inst_dir, "fuzzer_stats")
        if not os.path.isfile(sp):
            continue
        try:
            with open(sp) as f:
                for line in f:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if key == "bitmap_cvg":
                        try:
                            bc = float(val.rstrip("%"))
                            if bc > _best_bitmap:
                                _best_bitmap = bc
                                afl_bitmap_cvg = val
                        except ValueError:
                            pass
                    elif key == "execs_done":
                        afl_execs_done += int(val)
                    elif key == "execs_per_sec":
                        afl_execs_per_sec += float(val)
                    elif key == "edges_found":
                        afl_edges_found = max(afl_edges_found, int(val))
                    elif key == "total_edges":
                        afl_total_edges = max(afl_total_edges, int(val))
        except (OSError, ValueError):
            pass

    return {
        "wall_time": elapsed,
        "generated": total_generated,
        "unique": total_generated,
        "output_dir": combined_dir,
        "retcode": (mpi_proc.returncode or 0) if mpi_proc is not None else -1,
        "timed_out": elapsed >= timeout * 0.95,
        "throughput": total_generated / elapsed if elapsed > 0 else 0,
        "stdout": mpi_stdout[-500:] if mpi_stdout else "",
        "stderr": "",
        "afl_generated": afl_count,
        "symcc_interesting": symcc_interesting,
        "afl_bitmap_cvg": afl_bitmap_cvg,
        "afl_execs_done": afl_execs_done,
        "afl_execs_per_sec": afl_execs_per_sec,
        "afl_edges_found": afl_edges_found,
        "afl_total_edges": afl_total_edges,
        "num_workers": symcc_np - 1,
    }


def run_afl_only(afl_binary: str, target_name: str,
                 seed_dir: str, timeout: int, work_dir: str,
                 extra_args: list[str] | None = None,
                 cmplog_binary: str | None = None) -> dict:
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
    ]
    _persistent = _afl_binary_uses_shmem(afl_binary)
    # cmplog 持久性须与主二进制一致，否则 afl-fuzz 握手 ABORT；不一致则跳过 cmplog
    if cmplog_binary and _afl_binary_uses_shmem(cmplog_binary) == _persistent:
        afl_cmd.extend(["-c", cmplog_binary, "-l", "2AT"])
    afl_cmd.extend(["--", afl_binary])
    if extra_args:
        afl_cmd.extend(extra_args)
    # 持久+shmem 目标不带 @@（经共享内存进入 __AFL_LOOP，~35x 吞吐）；否则文件输入
    if not _persistent:
        afl_cmd.append("@@")

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
    afl_edges_found = 0
    afl_total_edges = 0
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
                    elif key == "edges_found":
                        afl_edges_found = int(val)
                    elif key == "total_edges":
                        afl_total_edges = int(val)
        except (OSError, ValueError):
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
        "afl_edges_found": afl_edges_found,
        "afl_total_edges": afl_total_edges,
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
            "speedup", "efficiency",
            "afl_bitmap_cvg", "afl_edges_found", "afl_total_edges",
            "afl_execs_done", "afl_execs_per_sec"
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
                f"{row.get('efficiency', 100.0):.1f}",
                row.get("afl_bitmap_cvg", ""),
                row.get("afl_edges_found", 0),
                row.get("afl_total_edges", 0),
                row.get("afl_execs_done", 0),
                row.get("afl_execs_per_sec", 0),
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
                # fuzzer_stats 指标（仅 hybrid 和 afl-only 有值）
                avg_afl_edges_found = sum(r.get("afl_edges_found", 0) for r in rows) / len(rows)
                avg_afl_total_edges = sum(r.get("afl_total_edges", 0) for r in rows) / len(rows)
                avg_afl_bitmap_cvg = sum(parse_bitmap_cvg(r.get("afl_bitmap_cvg", "")) for r in rows) / len(rows)
                avg_afl_execs_done = sum(r.get("afl_execs_done", 0) for r in rows) / len(rows)
                avg_afl_execs_per_sec = sum(r.get("afl_execs_per_sec", 0) for r in rows) / len(rows)
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
                    "avg_afl_edges_found": avg_afl_edges_found,
                    "avg_afl_total_edges": avg_afl_total_edges,
                    "avg_afl_bitmap_cvg": avg_afl_bitmap_cvg,
                    "avg_afl_execs_done": avg_afl_execs_done,
                    "avg_afl_execs_per_sec": avg_afl_execs_per_sec,
                })

            # Find serial baseline for speedup（下方 ASCII 图用）
            serial_throughput = None
            for s in summaries:
                if s["mode"] == "serial":
                    serial_throughput = s["avg_throughput"]

            # Check if any coverage data is present
            has_cov = any(s["avg_edge_cov"] > 0 for s in summaries)

            # 查找种子基线
            seed_cov = 0.0
            seed_edges = 0
            for s in summaries:
                if s["mode"] == "seed":
                    seed_cov = s["avg_edge_cov"]
                    seed_edges = int(s["avg_edges_found"])
                    break

            # 检查是否有 fuzzer_stats 数据
            has_fstats = any(s.get("avg_afl_edges_found", 0) > 0 for s in summaries)

            # Table header
            hdr = (f"  {'Mode':<10} {'NP':>4} {'Time':>10} "
                   f"{'Gen':>8} ")
            sep = (f"  {'─'*10} {'─'*4} {'─'*10} "
                   f"{'─'*8} ")
            if has_cov:
                hdr += f"{'ShowmapCov':>11} {'Edges':>14} "
                sep += f"{'─'*11} {'─'*14} "
            if has_fstats:
                hdr += f"{'FstatsCov':>10} {'FEdges':>14} "
                sep += f"{'─'*10} {'─'*14} "
            hdr += f"{'Execs':>10} {'exec/s':>8}\n"
            sep += f"{'─'*10} {'─'*8}\n"
            f.write(hdr)
            f.write(sep)

            for s in summaries:
                line = (f"  {s['mode']:<10} {s['np']:>4} "
                        f"{format_time(s['avg_time']):>10} "
                        f"{s['avg_generated']:>8.0f} ")
                if has_cov:
                    edges_str = f"{int(s['avg_edges_found'])}/{int(s['avg_edges_total'])}"
                    line += (f"{s['avg_edge_cov']:>10.2f}% "
                             f"{edges_str:>14} ")
                if has_fstats:
                    afl_ef = int(s.get("avg_afl_edges_found", 0))
                    afl_te = int(s.get("avg_afl_total_edges", 0))
                    afl_cvg = s.get("avg_afl_bitmap_cvg", 0.0)
                    if afl_ef > 0:
                        fedges_str = f"{afl_ef}/{afl_te}"
                        line += f"{afl_cvg:>9.2f}% {fedges_str:>14} "
                    else:
                        line += f"{'N/A':>10} {'N/A':>14} "
                afl_execs = int(s.get("avg_afl_execs_done", 0))
                afl_eps = s.get("avg_afl_execs_per_sec", 0)
                line += f"{afl_execs:>10} {afl_eps:>8.0f}\n"
                f.write(line)

            f.write("\n")

            # Edge Coverage chart (ASCII) - most important metric
            if has_cov:
                max_cov = max((s["avg_edge_cov"] for s in summaries), default=1)
                scale = max(max_cov, 1.0)  # 动态缩放
                f.write("  Edge Coverage Chart (afl-showmap):\n")
                for s in summaries:
                    if s["mode"] == "seed":
                        label = "seed"
                    elif s["mode"] == "serial":
                        label = "serial"
                    elif s["mode"] in ("hybrid", "afl-only"):
                        label = f"{s['mode']} np={s['np']}"
                    else:
                        label = f"mpi np={s['np']}"
                    cov = s["avg_edge_cov"]
                    bar_len = int(cov / scale * 40)
                    bar = "█" * bar_len + "░" * max(0, 40 - bar_len)
                    f.write(f"  {label:>16} |{bar}| {cov:.2f}%\n")
                f.write("\n")

            # SymCC 贡献分析
            if seed_cov > 0:
                f.write("  SymCC Contribution Analysis:\n")
                f.write(f"    Seed baseline: {seed_cov:.2f}% ({seed_edges} edges)\n")
                for s in summaries:
                    if s["mode"] in ("mpi", "hybrid", "afl-only") and s["avg_edge_cov"] > 0:
                        abs_gain = s["avg_edge_cov"] - seed_cov
                        rel_gain = (abs_gain / seed_cov * 100) if seed_cov > 0 else 0
                        label = f"{s['mode']} np={s['np']}"
                        f.write(f"    {label:>16}: {s['avg_edge_cov']:.2f}% "
                                f"(+{abs_gain:.2f}pp, +{rel_gain:.1f}% relative)\n")
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
    parser.add_argument("--engine", choices=["symcc", "symsan"], default=None,
                        help="concolic engine (default: symcc). symsan is EXPERIMENTAL — "
                             "requires scripts/build_symsan.sh + SYMSAN_FGTEST + *_symsan targets")
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
    parser.add_argument("--hybrid-afl-instances", type=int, default=0, metavar="N",
                        help="Number of parallel AFL instances in hybrid mode. "
                             "0=auto balanced (~np/2 AFL, rest SymCC; validated optimal); "
                             "1=legacy behavior (single AFL + np-1 SymCC)")
    parser.add_argument("--hybrid-adaptive", action="store_true",
                        help="Runtime-adaptive AFL/SymCC allocation (KRAKEN-style): "
                             "dynamically parks/unparks SymCC workers and scales AFL "
                             "instances based on live SymCC useful-ratio. Overrides "
                             "--hybrid-afl-instances.")
    parser.add_argument("--hybrid-grimoire", action="store_true",
                        help="Add GRIMOIRE-style grammar-free structural synthesizer as an "
                             "ensemble member (feeds AFL via -F). CPU-only; helps structured parsers.")
    parser.add_argument("--hybrid-honggfuzz", action="store_true",
                        help="Add honggfuzz as a heterogeneous ensemble member (needs a "
                             "hfuzz-clang-instrumented binary in public/bin/<suite>-hfuzz/).")
    parser.add_argument("--symcc-diversity", action="store_true",
                        help="Fine-grained concolic parallelism: give each SymCC worker a "
                             "distinct strategy + disjoint symbolized byte-region, reducing "
                             "downstream redundancy so >~12 workers contribute non-overlapping "
                             "coverage (tune SYMCC_FOCUS_PARTITIONS). Best on large/hard targets.")
    parser.add_argument("--symcc-density-balance", action="store_true",
                        help="With work-stealing splits, profile per-byte branch density "
                             "(no-solve pass) and cut equal-density regions (isolate hot "
                             "bytes) instead of equal-width. Implies --symcc-diversity; "
                             "needs a density-profiling SymCC runtime (else falls back).")
    parser.add_argument("--afl-only", action="store_true",
                        help="Also run AFL-only baseline (requires AFL-instrumented binaries)")
    parser.add_argument("--no-serial", action="store_true",
                        help="Skip serial baseline runs")
    parser.add_argument("--no-mpi", action="store_true",
                        help="Skip MPI-only parallel runs")
    parser.add_argument("--timeseries", type=int, default=0, metavar="INTERVAL",
                        help="Enable time-series coverage sampling every N seconds (default: disabled)")

    args = parser.parse_args()

    # 选定 concolic 引擎 → 经 SYMCC_ENGINE 透传给各 MPI worker(os.environ.copy 会带上),
    # worker 的 run_symcc_worker 据此走 SymCC(默认)或 SymSan(fgtest)路径。
    if args.engine:
        os.environ["SYMCC_ENGINE"] = args.engine

    try:
        np_list = [int(x) for x in args.np_list.split(",")]
    except ValueError:
        parser.error(f"--np-list 需为逗号分隔的整数，得到: {args.np_list!r}")
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
    micro_afl = {}   # 微目标的 AFL 二进制(afl-clang-fast),供 hybrid 的 AFL 侧 + 覆盖测量
    if args.no_default:
        print("Step 1: Skipping built-in targets (--no-default)")
        print("-" * 40)
    elif not args.skip_build:
        print("Step 1: Compiling target programs")
        print("-" * 40)

        _engine = get_engine()
        if args.symcc and _engine.name == "symcc":
            os.environ["SYMCC_CC"] = args.symcc   # 让 build_targets 用指定的 symcc
        if args.simulation:
            print("  (Simulation mode: using gcc)")
            binaries = build_targets_gcc(bin_dir)
        else:
            binaries = build_targets(bin_dir, _engine)   # 按 --engine 选 symcc/ko-clang
            if not binaries and _engine.name == "symcc":
                print("  SymCC not found, falling back to gcc (simulation mode)")
                print("  NOTE: simulation mode tests the MPI framework overhead,")
                print("        not actual symbolic execution performance.")
                binaries = build_targets_gcc(bin_dir)
                args.simulation = True
            if not args.simulation:
                micro_afl = build_afl_targets(bin_dir)  # 微目标 AFL 二进制(hybrid 需要)
    else:
        # Find existing binaries(按当前引擎的后缀优先)
        _suffixes = [get_engine().binary_suffix, "_symcc", "_native"]
        for name in target_names:
            for suffix in _suffixes:
                path = os.path.join(bin_dir, f"{name}{suffix}")
                if os.path.isfile(path):
                    binaries[name] = path
                    break
            afl_p = os.path.join(bin_dir, f"{name}_afl")
            if os.path.isfile(afl_p):
                micro_afl[name] = afl_p

    # Add public benchmark targets.
    # Auto-discover from benchmark/public/bin/ unless --no-public is passed.
    # Explicit --public specs override auto-discovery.
    public_targets = {}
    public_seed_dirs = {}
    target_extra_args: dict[str, list[str]] = {}  # 目标额外参数，如 base64 的 "-d"
    target_cmplog: dict[str, str] = {}  # 目标的 cmplog 二进制路径
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
                                # 读取 .args 文件（如有），如 base64.args 包含 "-d"
                                args_file = suite_dir / f"{bname}.args"
                                if args_file.is_file():
                                    extra = args_file.read_text().strip().split()
                                    if extra:
                                        target_extra_args[target_name] = extra
                                # 查找 cmplog 二进制（同名目录加 -cmplog 后缀）
                                cmplog_dir = pub_bin_dir / (suite_dir.name + "-cmplog")
                                cmplog_bin = cmplog_dir / bname
                                if cmplog_bin.is_file() and os.access(str(cmplog_bin), os.X_OK):
                                    target_cmplog[target_name] = str(cmplog_bin)

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

    if target_cmplog:
        print(f"\n  CmpLog binaries ({len(target_cmplog)}):")
        for name in sorted(target_cmplog):
            print(f"    {name}: {target_cmplog[name]}")

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

        # 种子基线覆盖率测量（使用 afl-showmap -C）
        seed_cov_data = {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0}
        if enable_coverage and target in afl_cov_binaries:
            seed_cov_data = measure_seed_coverage_afl(
                afl_cov_binaries[target], seed_dir,
                uses_file=True,
                extra_args=target_extra_args.get(target),
            )
            print(f"  Seed coverage: {seed_cov_data['edge_cov']:.2f}% "
                  f"({seed_cov_data['edges_found']}/{seed_cov_data['edges_total']})")
            all_results.append({
                "target": target,
                "mode": "seed",
                "np": 0,
                "round": 1,
                "wall_time": 0,
                "generated": len(os.listdir(seed_dir)),
                "unique": len(os.listdir(seed_dir)),
                "throughput": 0,
                "edge_cov": seed_cov_data.get("edge_cov", 0.0),
                "edges_found": seed_cov_data.get("edges_found", 0),
                "edges_total": seed_cov_data.get("edges_total", 0),
                "crashes": 0,
            })

        # Serial baseline
        if args.no_serial:
            print("\n  [Serial baseline] SKIPPED (--no-serial)")
        else:
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
                        uses_file=uses_file,
                        extra_args=target_extra_args.get(target),
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
        if args.no_mpi:
            print("\n  [MPI parallel] SKIPPED (--no-mpi)")
        for np_val in (np_list if not args.no_mpi else []):
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
                _mpi_kwargs = {
                    "binary": binary, "target_name": target,
                    "seed_dir": seed_dir, "np": actual_np,
                    "timeout": args.timeout, "work_dir": work_dir,
                    "simulate": args.simulation,
                    "extra_args": target_extra_args.get(target),
                }
                # --timeseries（opt-in）：后台线程周期性用 afl-showmap 采样覆盖率曲线
                if args.timeseries > 0 and target in afl_cov_binaries:
                    uses_file = TARGETS[target][3] if target in TARGETS else True
                    result, _ts = run_with_timeseries(
                        run_mpi, _mpi_kwargs, afl_cov_binaries[target],
                        interval=args.timeseries, uses_file=uses_file,
                        timeout=args.timeout,
                    )
                    if _ts:
                        all_timeseries.append({
                            "target": target, "mode": "mpi",
                            "np": actual_np, "round": r + 1,
                            "timeseries": _ts,
                        })
                else:
                    result = run_mpi(**_mpi_kwargs)

                # 使用 AFL 边覆盖率测量
                cov_data = {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0, "crashes": 0}
                if enable_coverage and target in afl_cov_binaries:
                    uses_file = TARGETS[target][3] if target in TARGETS else True
                    cov_data = measure_coverage_afl(
                        afl_cov_binaries[target], result["output_dir"],
                        uses_file=uses_file,
                        extra_args=target_extra_args.get(target),
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
        if args.hybrid and (target in public_targets or target in micro_afl):
            afl_targets = discover_public_afl_targets()
            afl_binary = afl_targets.get(target) or micro_afl.get(target)  # 微目标也可 hybrid
            if afl_binary:
                for np_val in np_list:
                    actual_np = max(2, np_val)
                    # 决定 AFL 实例数
                    if args.hybrid_afl_instances > 0:
                        afl_inst = args.hybrid_afl_instances
                    else:
                        # auto: SymCC worker 数量封顶，其余核心全给 AFL 并行实例。
                        # 依据分配曲线实验（np=16 与 np=64）：
                        #  - 低 np：AFL≈np/2 处于最优包络（sqlite +18%、libarchive +3%）；
                        #  - 高 np：concolic 存在冗余拐点，SymCC worker 超过 ~12 后收益递减，
                        #    固定 np/2 比例会把过多核心浪费在冗余 concolic 上（libarchive 在
                        #    np=64、31 workers 时回退 -4.6%）。文献亦证 concolic 并行早饱和、
                        #    而 AFL 并行扩展性好。
                        # 故：SymCC ranks = min(np//2, CAP+1)，AFL 拿走其余。
                        symcc_ranks = min(actual_np // 2, SYMCC_WORKER_CAP + 1)
                        symcc_ranks = max(2, symcc_ranks)  # 至少 master+1 worker
                        afl_inst = max(1, actual_np - symcc_ranks)
                    afl_inst = min(afl_inst, max(1, actual_np - 2))
                    symcc_workers = max(1, actual_np - afl_inst - 1)  # -afl_inst, -1 master
                    if args.hybrid_adaptive:
                        print(f"\n  [Hybrid AFL+SymCC np={actual_np} (ADAPTIVE: "
                              f"runtime AFL/SymCC rebalancing)]")
                    else:
                        print(f"\n  [Hybrid AFL+SymCC np={actual_np} "
                              f"(AFL={afl_inst}, SymCC workers={symcc_workers})]")

                    for r in range(args.rounds):
                        current_run += 1
                        work_dir = tempfile.mkdtemp(
                            prefix=f"bench_{target}_hybrid{actual_np}_r{r}_"
                        )

                        print(f"    Round {r+1}/{args.rounds}... ", end="", flush=True)
                        result = run_hybrid(
                            binary, afl_binary, target, seed_dir,
                            actual_np, args.timeout, work_dir,
                            extra_args=target_extra_args.get(target),
                            cmplog_binary=target_cmplog.get(target),
                            afl_instances=afl_inst,
                            adaptive=args.hybrid_adaptive,
                            grimoire=args.hybrid_grimoire,
                            honggfuzz_binary=(
                                discover_public_hfuzz_targets().get(target)
                                if args.hybrid_honggfuzz else None),
                            symcc_diversity=args.symcc_diversity,
                            symcc_density_balance=args.symcc_density_balance,
                        )

                        # 使用 AFL 边覆盖率测量
                        cov_data = {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0, "crashes": 0}
                        if enable_coverage and target in afl_cov_binaries:
                            cov_data = measure_coverage_afl(
                                afl_cov_binaries[target], result["output_dir"],
                                uses_file=True,
                                extra_args=target_extra_args.get(target),
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
                            "afl_bitmap_cvg": result.get("afl_bitmap_cvg", ""),
                            "afl_edges_found": result.get("afl_edges_found", 0),
                            "afl_total_edges": result.get("afl_total_edges", 0),
                            "afl_execs_done": result.get("afl_execs_done", 0),
                            "afl_execs_per_sec": result.get("afl_execs_per_sec", 0),
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
                        args.timeout, work_dir,
                        extra_args=target_extra_args.get(target),
                        cmplog_binary=target_cmplog.get(target),
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
                        "afl_bitmap_cvg": result.get("afl_bitmap_cvg", ""),
                        "afl_edges_found": result.get("afl_edges_found", 0),
                        "afl_total_edges": result.get("afl_total_edges", 0),
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
