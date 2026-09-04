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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
SYMCC_ROOT = SCRIPT_DIR.parent
TARGETS_DIR = SCRIPT_DIR / "targets"
SEEDS_DIR = SCRIPT_DIR / "seeds"

sys.path.insert(0, str(SYMCC_ROOT / "util"))
from concolic_engine import get_engine  # noqa: E402  concolic 引擎抽象(symcc/symsan)
try:  # CLI execution and package-style unit imports use different roots.
    from research_protocol import content_digest, coverage_auc  # noqa: E402
except ImportError:  # pragma: no cover - exercised by package import tests
    from benchmark.research_protocol import content_digest, coverage_auc  # noqa: E402
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


@dataclass(frozen=True)
class AflBuildVariant:
    """AFL++ compile-time instrumentation variant."""

    name: str
    suffix: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AflRuntimeProfile:
    """One AFL++ worker profile used by the hybrid ensemble."""

    name: str
    schedule: str
    variant: str = "default"
    use_mopt: bool = False
    use_cmplog: bool = False
    env: dict[str, str] = field(default_factory=dict)


AFL_BUILD_VARIANTS = [
    AflBuildVariant("default", "_afl", {}),
    AflBuildVariant("laf", "_afl_laf", {"AFL_LLVM_LAF_ALL": "1"}),
    AflBuildVariant("ctx", "_afl_ctx", {"AFL_LLVM_CTX": "1"}),
    AflBuildVariant("ngram4", "_afl_ngram4", {"AFL_LLVM_NGRAM_SIZE": "4"}),
    AflBuildVariant(
        "laf_ctx",
        "_afl_laf_ctx",
        {"AFL_LLVM_LAF_ALL": "1", "AFL_LLVM_CTX": "1"},
    ),
]

AFL_RUNTIME_PROFILES = [
    AflRuntimeProfile("explore-cmplog", "explore", "default", use_cmplog=True),
    AflRuntimeProfile("mopt-fast", "fast", "default", use_mopt=True),
    AflRuntimeProfile(
        "laf-exploit", "exploit", "laf", use_cmplog=True,
        env={"AFL_DISABLE_TRIM": "1"}),
    AflRuntimeProfile(
        "ctx-rare", "rare", "ctx",
        env={"AFL_EXPAND_HAVOC_NOW": "1"}),
    AflRuntimeProfile("ngram-coe", "coe", "ngram4", use_mopt=True),
    AflRuntimeProfile(
        "laf-seek", "seek", "laf", use_cmplog=True,
        env={"AFL_KEEP_TIMEOUTS": "1"}),
    AflRuntimeProfile("ctx-mmopt", "mmopt", "ctx", use_mopt=True),
    AflRuntimeProfile("ngram-lin", "lin", "ngram4", use_cmplog=True),
    AflRuntimeProfile("lafctx-quad", "quad", "laf_ctx"),
]


def resolve_afl_profile_mode(raw_mode: str, hybrid: bool,
                             adaptive: bool, afl_only: bool) -> str:
    """Resolve --aflpp-profiles auto/basic/full/off."""
    if raw_mode != "auto":
        return raw_mode
    if adaptive:
        return "full"
    if hybrid or afl_only:
        return "basic"
    return "off"


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


def directed_distance_map_has_rows(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if line and not line.startswith("#"):
                    return True
    except OSError:
        return False
    return False


def merge_directed_distance_map(path: str) -> bool:
    """Merge module-local SymCC coloration fragments in-place when possible."""
    script = SYMCC_ROOT / "util" / "merge_directed_distance.py"
    if not script.is_file() or not os.path.isfile(path):
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(script), path, "--output", path],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def build_targets(output_dir, engine=None, directed_targets: str | None = None,
                  structural_tasks: bool = False):
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
        task_graph_path = Path(output_dir) / f"{name}.task_graph"
        if structural_tasks and engine.name == "symcc":
            try:
                task_graph_path.unlink()
            except OSError:
                pass
            env["SYMCC_TASK_GRAPH_OUT"] = str(task_graph_path)
        if directed_targets and engine.name == "symcc":
            distance_path = Path(output_dir) / f"{name}.directed_distance"
            try:
                distance_path.unlink()
            except OSError:
                pass
            if "-g" not in cmd:
                cmd.insert(1, "-g")
            env["SYMCC_COLOR_TARGETS"] = directed_targets
            env["SYMCC_COLORATION_OUT"] = str(distance_path)
        print(f"  Compiling {name}... ", end="", flush=True)
        start = time.monotonic()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
            elapsed = time.monotonic() - start
            if r.returncode == 0:
                if directed_targets and engine.name == "symcc":
                    merge_directed_distance_map(str(distance_path))
                if structural_tasks and engine.name == "symcc":
                    merge_directed_distance_map(str(task_graph_path))
                print(f"OK ({elapsed:.1f}s)")
                binaries[name] = str(bin_path)
            else:
                print(f"FAILED (ret={r.returncode})")
                if r.stderr:
                    print(f"    {r.stderr[:200]}")
        except (OSError, subprocess.SubprocessError) as e:
            print(f"FAILED ({e})")
    return binaries


def build_afl_targets(output_dir, profile_mode: str = "off"):
    """Build AFL++ micro-target binaries.

    The default variant keeps the historical *_afl path used for showmap and
    simple hybrid runs. In profile mode we also build AFL++ instrumentation
    variants (LAF/CompCov, CTX, Ngram) plus a CmpLog companion so the hybrid
    runner can assemble an xFUZZ/KRAKEN-style ensemble without requiring users
    to hand-wire binaries.
    """
    afl_cc = shutil.which("afl-clang-fast")
    binaries: dict[str, str] = {}
    variants: dict[str, dict[str, str]] = defaultdict(dict)
    cmplog: dict[str, str] = {}
    if not afl_cc:
        return binaries, variants, cmplog
    os.makedirs(output_dir, exist_ok=True)
    build_variants = profile_mode in {"basic", "full"}
    selected_variants = AFL_BUILD_VARIANTS
    if profile_mode == "basic":
        selected_variants = AFL_BUILD_VARIANTS[:3]
    elif not build_variants:
        selected_variants = [AFL_BUILD_VARIANTS[0]]
    for name, (source, _, _, _) in TARGETS.items():
        src = str(TARGETS_DIR / source)
        for variant in selected_variants:
            out = Path(output_dir) / f"{name}{variant.suffix}"
            env = {**os.environ, "AFL_QUIET": "1", **variant.env}
            try:
                r = subprocess.run(
                    [afl_cc, "-O2", src, "-o", str(out)],
                    capture_output=True, text=True, timeout=180, env=env)
                if r.returncode == 0 and out.exists():
                    variants[name][variant.name] = str(out)
                    if variant.name == "default":
                        binaries[name] = str(out)
            except (OSError, subprocess.SubprocessError):
                pass
        if build_variants:
            out = Path(output_dir) / f"{name}_afl_cmplog"
            env = {**os.environ, "AFL_QUIET": "1", "AFL_LLVM_CMPLOG": "1"}
            try:
                r = subprocess.run(
                    [afl_cc, "-O2", src, "-o", str(out)],
                    capture_output=True, text=True, timeout=180, env=env)
                if r.returncode == 0 and out.exists():
                    cmplog[name] = str(out)
            except (OSError, subprocess.SubprocessError):
                pass
    return binaries, variants, cmplog


def build_afl_data_coverage_runtime(output_dir: str) -> str | None:
    """Build the AFL_PRELOAD data-coverage runtime used by hybrid mode."""
    src = SYMCC_ROOT / "util" / "afl_data_coverage_rt.c"
    if not src.is_file():
        return None
    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("clang")
    if not cc:
        return None
    out = Path(output_dir) / "libafl_data_coverage_rt.so"
    cmd = [cc, "-O2", "-shared", "-fPIC", str(src), "-o", str(out), "-ldl"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return str(out) if result.returncode == 0 and out.is_file() else None


def afl_supports_python_mutators() -> bool:
    """Return whether the installed afl-fuzz advertises Python mutator support."""
    afl_fuzz = shutil.which("afl-fuzz")
    if not afl_fuzz:
        return False
    try:
        result = subprocess.run(
            [afl_fuzz, "-hh"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    output = (result.stdout or "") + (result.stderr or "")
    return "AFL_PYTHON_MODULE" in output and "Python" in output


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


def parse_afl_showmap_coverage(output: str) -> dict[str, object]:
    """Parse AFL++ corpus coverage without confusing capacity and universe."""
    measurement: dict[str, object] = {
        "edge_cov": 0.0,
        "edges_found": 0,
        "edges_total": 0,
        "measure_ok": False,
        "edge_count_ok": False,
        "coverage_denominator_kind": "unavailable",
        "coverage_map_size": 0,
    }
    summary = re.search(
        r"coverage of (\d+) edges were achieved out of (\d+) existing "
        r"\(([0-9.]+)%\)",
        output,
    )
    if summary is not None:
        found = int(summary.group(1))
        total = int(summary.group(2))
        percentage = float(summary.group(3))
        if total <= 0 or found > total or not 0.0 <= percentage <= 100.0:
            return measurement
        measurement.update({
            "edge_cov": percentage,
            "edges_found": found,
            "edges_total": total,
            "measure_ok": True,
            "edge_count_ok": True,
            "coverage_denominator_kind": "existing_edges",
        })
        return measurement

    captured = re.search(r"Captured (\d+) tuples \(map size (\d+)", output)
    if captured is not None:
        # map size is the bitmap capacity in bytes, not the number of
        # instrumented edges. The tuple count remains useful, but no coverage
        # percentage or saturation universe can be derived from this line.
        measurement.update({
            "edges_found": int(captured.group(1)),
            "edge_count_ok": True,
            "coverage_map_size": int(captured.group(2)),
        })
    return measurement


def coverage_measurement_metadata(measurement: typing.Mapping[str, object]) -> dict:
    """Return explicit coverage provenance for result and CSV records."""
    return {
        "coverage_measure_ok": bool(measurement.get("measure_ok", False)),
        "coverage_edge_count_ok": bool(
            measurement.get("edge_count_ok", False)
        ),
        "coverage_denominator_kind": str(
            measurement.get("coverage_denominator_kind", "unavailable")
        ),
        "coverage_map_size": int(measurement.get("coverage_map_size", 0) or 0),
        "coverage_sampled": bool(measurement.get("sampled", False)),
        "coverage_sampled_cases": int(
            measurement.get(
                "sampled_cases", measurement.get("total_cases", 0)
            )
            or 0
        ),
        "coverage_total_cases": int(measurement.get("total_cases", 0) or 0),
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
        ``measure_ok`` is true only when AFL reports an existing-edge
        denominator. ``edge_count_ok`` may still be true for tuple-only output.
    """
    import random

    if not os.path.isdir(test_case_dir):
        return {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0,
                "crashes": 0, "total_cases": 0, "sampled": False,
                "measure_ok": False, "edge_count_ok": False,
                "coverage_denominator_kind": "unavailable",
                "coverage_map_size": 0}

    test_files = [f for f in os.listdir(test_case_dir)
                  if os.path.isfile(os.path.join(test_case_dir, f))]
    total_cases = len(test_files)
    if total_cases == 0:
        return {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0,
                "crashes": 0, "total_cases": 0, "sampled": False,
                "measure_ok": False, "edge_count_ok": False,
                "coverage_denominator_kind": "unavailable",
                "coverage_map_size": 0}

    afl_showmap = shutil.which("afl-showmap")
    if not afl_showmap:
        return {"edge_cov": 0.0, "edges_found": 0, "edges_total": 0,
                "crashes": 0, "total_cases": total_cases, "sampled": False,
                "measure_ok": False, "edge_count_ok": False,
                "coverage_denominator_kind": "unavailable",
                "coverage_map_size": 0}

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

    parsed_coverage = parse_afl_showmap_coverage("")
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

        parsed_coverage = parse_afl_showmap_coverage(stderr)
        if not parsed_coverage["edge_count_ok"]:
            # 既无 coverage 行也无 Captured 行 → showmap 未正常完成（fork server 握手
            # 失败/启动即崩溃/map 不匹配等）。区别于"真 0 覆盖"：告警 + 返回码，供调用方据
            # measure_ok 丢弃该点，而非把 0.0 当真实数据污染均值/最佳配置选择。
            print(f"[warn] afl-showmap 未产出覆盖率 (rc={result.returncode}); "
                  f"输出尾部: {stderr[-300:]!r}", file=sys.stderr)
        elif not parsed_coverage["measure_ok"]:
            print(
                "[warn] afl-showmap 只报告 tuple 数和 bitmap 容量；"
                "缺少 existing-edge 分母，本次覆盖百分比不可用",
                file=sys.stderr,
            )

    except subprocess.TimeoutExpired:
        pass
    except (subprocess.SubprocessError, OSError, ValueError):
        pass

    try:
        os.remove(out_file)
    except OSError:
        pass

    # 清理抽样临时目录
    if sample_dir:
        shutil.rmtree(sample_dir, ignore_errors=True)

    return {
        "edge_cov": round(float(parsed_coverage["edge_cov"]), 2),
        "edges_found": int(parsed_coverage["edges_found"]),
        "edges_total": int(parsed_coverage["edges_total"]),
        "crashes": 0,  # afl-showmap -C 不单独报告 crash 数
        "total_cases": total_cases,
        "sampled": sampled,
        "sampled_cases": n_measured,
        "measure_ok": bool(parsed_coverage["measure_ok"]),
        "edge_count_ok": bool(parsed_coverage["edge_count_ok"]),
        "coverage_denominator_kind": parsed_coverage[
            "coverage_denominator_kind"
        ],
        "coverage_map_size": int(parsed_coverage["coverage_map_size"]),
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


CorpusSource = (
    str
    | os.PathLike
    | typing.Iterable[str | os.PathLike]
    | typing.Callable[
        [], str | os.PathLike | typing.Iterable[str | os.PathLike]
    ]
)


def _resolve_corpus_dirs(source: CorpusSource) -> list[str]:
    """Resolve a static or live corpus provider into stable unique directories."""
    value = source() if callable(source) else source
    if isinstance(value, (str, os.PathLike)):
        values = [value]
    else:
        values = list(value)

    resolved: list[str] = []
    seen: set[str] = set()
    for item in values:
        path = os.path.abspath(os.fspath(item))
        if path not in seen and os.path.isdir(path):
            seen.add(path)
            resolved.append(path)
    return resolved


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def measure_coverage_corpora_afl(
    afl_binary: str,
    corpus_source: CorpusSource,
    uses_file: bool = True,
    extra_args: list[str] | None = None,
) -> dict:
    """Measure the content-deduplicated union of one or more live corpora.

    AFL, hybrid SymCC, and structural generators publish inputs into different
    immutable queue directories. A content-addressed hard-link snapshot gives
    afl-showmap one flat, point-in-time corpus without biasing capped sampling
    toward duplicated seeds synchronized between queues.
    """
    corpus_dirs = _resolve_corpus_dirs(corpus_source)
    snapshot_dir = tempfile.mkdtemp(prefix=".afl_cov_union_")
    try:
        seen_hashes: set[str] = set()
        for corpus_dir in corpus_dirs:
            try:
                entries = sorted(os.scandir(corpus_dir), key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=True):
                        continue
                    digest = _file_sha256(entry.path)
                    if digest in seen_hashes:
                        continue
                    seen_hashes.add(digest)
                    _link_or_copy(entry.path, os.path.join(snapshot_dir, digest))
                except OSError:
                    # Live queues may rotate an entry between scandir and open.
                    continue
        return measure_coverage_afl(
            afl_binary,
            snapshot_dir,
            uses_file=uses_file,
            extra_args=extra_args,
        )
    finally:
        shutil.rmtree(snapshot_dir, ignore_errors=True)


def _hybrid_live_corpora(seed_dir: str, work_dir: str) -> list[str]:
    """Return all currently published hybrid corpora for one sampling tick."""
    paths = [
        seed_dir,
        os.path.join(work_dir, "symcc_all_outputs"),
        os.path.join(work_dir, "honggfuzz_out"),
        os.path.join(work_dir, "grimoire_out"),
    ]
    afl_out_dir = os.path.join(work_dir, "afl_out")
    try:
        for entry in os.scandir(afl_out_dir):
            if entry.is_dir() and (
                entry.name.startswith("fuzzer")
                or entry.name.startswith("symcc")
            ):
                paths.append(os.path.join(entry.path, "queue"))
    except OSError:
        pass
    return paths


def _snapshot_live_corpora(
    corpus_source: CorpusSource,
    snapshot_dir: str,
) -> None:
    """Hard-link one instantaneous corpus view without hashing file contents.

    Content hashing and showmap replay are intentionally deferred until the
    campaign is over. During the measured interval this performs directory
    enumeration and metadata-only links on the common-filesystem fast path.
    """
    os.makedirs(snapshot_dir, exist_ok=True)
    seen_inodes: set[tuple[int, int]] = set()
    output_index = 0
    for source_index, corpus_dir in enumerate(
        _resolve_corpus_dirs(corpus_source)
    ):
        try:
            entries = sorted(os.scandir(corpus_dir), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=True):
                    continue
                stat = entry.stat(follow_symlinks=True)
                inode = (stat.st_dev, stat.st_ino)
                if inode in seen_inodes:
                    continue
                seen_inodes.add(inode)
                destination = os.path.join(
                    snapshot_dir,
                    f"{source_index:04d}_{output_index:012d}",
                )
                _link_or_copy(entry.path, destination)
                output_index += 1
            except OSError:
                # A producer may publish/retire an entry during enumeration.
                continue


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
    corpus_source: CorpusSource,
    interval: int = 30,
    max_duration: int = 600,
    uses_file: bool = True,
    extra_args: list[str] | None = None,
    stop_event: threading.Event | None = None,
    wait_for_stop_before_replay: bool = False,
    snapshot_parent: str | None = None,
) -> list[dict]:
    """Snapshot a live corpus periodically, then replay snapshots with showmap.

    The measured campaign only pays metadata/hard-link snapshot overhead.
    Expensive content de-duplication and afl-showmap replay happen afterwards,
    so coverage measurement cannot consume the CPU allocation under comparison.
    """
    snapshots: list[tuple[float, str]] = []
    snapshot_root = tempfile.mkdtemp(
        prefix=".afl_cov_series_",
        dir=snapshot_parent,
    )
    start_time = time.monotonic()

    def take_snapshot(elapsed: float) -> None:
        snapshot_dir = os.path.join(
            snapshot_root, f"sample_{len(snapshots):06d}")
        _snapshot_live_corpora(corpus_source, snapshot_dir)
        snapshots.append((round(elapsed, 1), snapshot_dir))

    try:
        first_sample = True
        while (
            time.monotonic() - start_time < max_duration
            and (
                first_sample
                or stop_event is None
                or not stop_event.is_set()
            )
        ):
            first_sample = False
            take_snapshot(time.monotonic() - start_time)
            now = time.monotonic()
            next_tick = int((now - start_time) // interval) + 1
            sleep_time = start_time + next_tick * interval - now
            sleep_time = min(
                sleep_time,
                max(0.0, start_time + max_duration - now),
            )
            if sleep_time > 0:
                if stop_event is None:
                    time.sleep(sleep_time)
                elif stop_event.wait(sleep_time):
                    break

        # Preserve the corpus at completion/budget even when it falls between
        # ticks. Duplicate points are harmless and retain the final observation.
        elapsed = min(float(max_duration), time.monotonic() - start_time)
        take_snapshot(elapsed)

        if (
            wait_for_stop_before_replay
            and stop_event is not None
            and not stop_event.is_set()
        ):
            stop_event.wait()

        timeseries: list[dict] = []
        for timestamp, snapshot_dir in snapshots:
            try:
                cov_data = measure_coverage_corpora_afl(
                    afl_binary,
                    snapshot_dir,
                    uses_file=uses_file,
                    extra_args=extra_args,
                )
                if not cov_data.get("measure_ok", False):
                    continue
                timeseries.append({
                    "timestamp_sec": timestamp,
                    "edge_cov": cov_data["edge_cov"],
                    "edges_found": cov_data["edges_found"],
                    "edges_total": cov_data["edges_total"],
                    "total_cases": cov_data["total_cases"],
                })
            except (
                OSError,
                KeyError,
                ValueError,
                subprocess.SubprocessError,
            ):
                continue
        return timeseries
    finally:
        shutil.rmtree(snapshot_root, ignore_errors=True)


def run_with_timeseries(
    run_fn,
    run_kwargs: dict,
    afl_binary: str,
    interval: int = 30,
    uses_file: bool = True,
    timeout: int = 300,
    corpus_source: CorpusSource | None = None,
    extra_args: list[str] | None = None,
) -> tuple[dict, list[dict]]:
    """运行基准测试同时在后台采样 AFL 边覆盖率时间序列。

    run_fn: 实际执行函数 (run_mpi, run_hybrid, etc.)
    run_kwargs: 传递给 run_fn 的参数
    返回 (run_result, timeseries)
    """
    result_container = [None]
    error_container = [None]
    benchmark_done = threading.Event()

    def run_benchmark() -> None:
        # 线程边界：必须捕获全部异常并转交主线程重新抛出（下方 raise error_container[0]），
        # 否则守护线程内的异常会静默丢失、主线程只看到 result=None。此为线程错误编组
        # 的惯用正确模式，故此处的宽 except 是必要的。
        try:
            result_container[0] = run_fn(**run_kwargs)
        except Exception as e:  # noqa: BLE001 — 线程错误编组，见上
            error_container[0] = e
        finally:
            benchmark_done.set()

    bench_thread = threading.Thread(target=run_benchmark, daemon=True)
    bench_thread.start()

    # Legacy/default source for MPI callers. Other modes pass an explicit live
    # provider because their corpus is split across multiple queue directories.
    if corpus_source is None:
        output_dir = run_kwargs.get("work_dir", "")
        np_val = run_kwargs.get("np")
        if np_val:
            corpus_source = os.path.join(
                output_dir, f"mpi_np{np_val}_output")
        else:
            corpus_source = os.path.join(output_dir, "output")

    wait_start = time.monotonic()
    while (
        not _resolve_corpus_dirs(corpus_source)
        and time.monotonic() - wait_start < 10
    ):
        if benchmark_done.is_set():
            break
        time.sleep(0.5)

    if not _resolve_corpus_dirs(corpus_source):
        _join_benchmark_thread(bench_thread, timeout + 60)
        result = result_container[0]
        if error_container[0]:
            raise error_container[0]
        return result, []

    # Capture cheap hard-link snapshots during the campaign. Showmap replay is
    # deferred until benchmark_done, so it cannot steal the measured CPU share.
    timeseries = measure_coverage_timeseries_afl(
        afl_binary, corpus_source,
        interval=interval, max_duration=timeout,
        uses_file=uses_file,
        extra_args=extra_args,
        stop_event=benchmark_done,
        wait_for_stop_before_replay=True,
        snapshot_parent=run_kwargs.get("work_dir"),
    )

    # 与上面的 join 一致用 timeout+60：run_hybrid 的清理最坏情况随 AFL 实例数递增
    # （每个存活进程 SIGTERM 等 10s + SIGKILL 等 5s），固定 60s 在高 np 下可能过短而
    # 丢弃/截断结果。
    _join_benchmark_thread(bench_thread, timeout + 60)
    result = result_container[0]
    if error_container[0]:
        raise error_container[0]

    return result, timeseries


def _join_benchmark_thread(
    thread: threading.Thread,
    timeout: float,
) -> None:
    """Reject a truncated benchmark instead of returning a partial result."""
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(
            "benchmark runner did not finish within its execution and cleanup budget"
        )


def annotate_research_results(
    results: list[dict],
    timeseries: list[dict],
) -> None:
    """Attach stable run/pair identity, failure state, CPU budget and AUC."""
    external_experiment = os.environ.get("SYMCC_EXPERIMENT_ID", "")
    external_run = os.environ.get("SYMCC_RUN_ID", "")
    external_pair = os.environ.get("SYMCC_PAIR_ID", "")
    phase = os.environ.get("SYMCC_RESEARCH_PHASE", "engineering")
    configuration = os.environ.get("SYMCC_RESEARCH_CONFIGURATION", "")
    try:
        common_seed = int(os.environ.get("SYMCC_RANDOM_SEED", "0") or 0)
    except ValueError:
        common_seed = 0
    experiment_id = external_experiment or (
        "symcc-" + content_digest({
            "created": datetime.now().isoformat(),
            "rows": [
                (row.get("target"), row.get("mode"), row.get("np"))
                for row in results
            ],
        })[:16]
    )
    measured_rows = [
        row for row in results if str(row.get("mode", "")) != "seed"
    ]
    if external_run and len(measured_rows) != 1:
        raise ValueError(
            "a research protocol cell must produce exactly one non-seed "
            "benchmark row; use --rounds 1 and select one execution mode"
        )

    by_key: dict[tuple[str, str, int, int], dict] = {}
    for row in results:
        target = str(row.get("target", ""))
        mode = str(row.get("mode", ""))
        np_value = int(row.get("np", 0) or 0)
        repeat = int(row.get("round", 0) or 0)
        pair_id = external_pair or content_digest({
            "experiment_id": experiment_id,
            "target": target,
            "np": np_value,
            "repeat": repeat,
        })[:24]
        run_id = (
            external_run
            if external_run and mode != "seed"
            else content_digest({
                "pair_id": pair_id,
                "mode": mode,
            })[:24]
        )
        row["experiment_id"] = experiment_id
        row["protocol_run_id"] = external_run
        row["run_id"] = run_id
        row["pair_id"] = pair_id
        row["phase"] = phase
        row["configuration"] = configuration or mode
        row["random_seed"] = common_seed
        row.setdefault("status", "success")
        row.setdefault("failure_reason", "")
        try:
            allocated = int(os.environ.get("SYMCC_CPU_CORES", "") or 0)
        except ValueError:
            allocated = 0
        if allocated <= 0:
            allocated = (
                max(1, np_value) if mode in {"mpi", "hybrid", "afl-only"}
                else 1
            )
        row["allocated_cpu_cores"] = allocated
        declared_budget = os.environ.get("SYMCC_CPU_BUDGET_SECONDS", "")
        try:
            cpu_budget = float(declared_budget)
        except (TypeError, ValueError):
            cpu_budget = float(row.get("wall_time", 0.0) or 0.0) * allocated
        row["cpu_budget_seconds"] = cpu_budget
        try:
            row["wall_budget_seconds"] = (
                cpu_budget / allocated if declared_budget else
                float(row.get("wall_time", 0.0) or 0.0)
            )
        except (TypeError, ValueError, ZeroDivisionError):
            row["wall_budget_seconds"] = 0.0
        by_key[(target, mode, np_value, repeat)] = row

    for entry in timeseries:
        key = (
            str(entry.get("target", "")),
            str(entry.get("mode", "")),
            int(entry.get("np", 0) or 0),
            int(entry.get("round", 0) or 0),
        )
        row = by_key.get(key)
        if row is None:
            continue
        entry["experiment_id"] = experiment_id
        entry["run_id"] = row["run_id"]
        entry["pair_id"] = row["pair_id"]
        entry["configuration"] = row["configuration"]
        points = entry.get("timeseries", ())
        if points:
            budget = max(
                float(row.get("wall_budget_seconds", 0.0) or 0.0),
                max(float(point.get("timestamp_sec", 0.0) or 0.0)
                    for point in points),
            )
            if budget > 0:
                row["coverage_auc"] = coverage_auc(points, budget)


def benchmark_outcome(result: dict) -> dict[str, object]:
    """Normalize subprocess completion without dropping failures/timeouts."""
    if result.get("timed_out"):
        # Fuzzing campaigns normally end at their declared time budget.  The
        # final corpus is a valid observation, not a failed run.
        return {
            "status": "success",
            "failure_reason": "",
            "retcode": result.get("retcode"),
            "budget_exhausted": True,
        }
    try:
        returncode = int(result.get("retcode", 0) or 0)
    except (TypeError, ValueError):
        returncode = -1
    return {
        "status": "success" if returncode == 0 else "failed",
        "failure_reason": "" if returncode == 0 else f"exit-{returncode}",
        "retcode": returncode,
        "budget_exhausted": False,
    }


def benchmark_matrix_exit_code(results: list[dict]) -> int:
    """Propagate a measured child failure after preserving its report."""
    return 3 if any(
        row.get("mode") != "seed" and row.get("status") != "success"
        for row in results
    ) else 0


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


def _link_or_copy(src: str, dst: str) -> bool:
    """将 src 硬链接到 dst（同一文件系统近乎零成本）；跨盘/已存在等失败时退回 copy2。

    覆盖率测量的合并语料只读，硬链接即可，避免对数万 queue 文件逐个整块复制
    （数万次 open+read+write），大幅降低合并阶段的墙钟与磁盘 I/O。
    """
    try:
        os.link(src, dst)
        return True
    except OSError:
        # 跨文件系统 / 目标已存在 / 不支持硬链接 → 退回复制（copy2 覆盖既有）
        try:
            shutil.copy2(src, dst)
            return True
        except OSError as e:
            # 硬链接与复制双双失败（源消失/磁盘满/权限）→ 该文件缺席会低估合并语料的
            # 覆盖率；告警而非静默吞掉，便于发现数据质量问题。
            print(f"[warn] _link_or_copy 跳过 {src}: {e}", file=sys.stderr)
            return False


def count_output_files(directory):
    """Count test case files in a directory."""
    if not os.path.isdir(directory):
        return 0
    count = 0
    for f in os.listdir(directory):
        if (_is_corpus_filename(f)
                and os.path.isfile(os.path.join(directory, f))):
            count += 1
    return count


def _is_corpus_filename(filename: str) -> bool:
    return not filename.startswith(".") and not filename.endswith(".tmp")


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
        if _is_corpus_filename(f) and os.path.isfile(fpath):
            if hex64_re.match(f):
                # Filename is the hash — skip expensive re-read
                hashes.add(f)
            else:
                try:
                    with open(fpath, "rb") as fh:
                        h = hashlib.sha256(fh.read()).hexdigest()
                except OSError:
                    # A live AFL queue may rotate an entry between listdir and
                    # open. Missing it in this sample is preferable to aborting
                    # the entire measurement.
                    continue
                hashes.add(h)
    return hashes


def get_unique_hashes_many(directories):
    """Return the content-hash union of multiple corpus directories."""
    hashes = set()
    for directory in directories:
        hashes.update(get_unique_hashes(directory))
    return hashes


def _simulate_serial(binary, seed_dir, output_dir, timeout, uses_file,
                     extra_args=None, max_files=10000):
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
            cmd = [
                "timeout", "-k", "2", "5", binary,
                *(extra_args or []), input_file,
            ]
        else:
            cmd = ["timeout", "-k", "2", "5", binary, *(extra_args or [])]

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
               work_dir: str, simulate: bool = False,
               extra_args: list[str] | None = None):
    """Run the serial pure_concolic_execution.sh baseline."""
    output_dir = os.path.join(work_dir, "serial_output")
    os.makedirs(output_dir, exist_ok=True)

    uses_file = TARGETS[target_name][3] if target_name in TARGETS else True

    if simulate:
        # 模拟模式：直接在 Python 中运行目标并生成变异
        timed_out = False
        start = time.monotonic()
        try:
            _simulate_serial(
                binary, seed_dir, output_dir, timeout, uses_file,
                extra_args=extra_args,
            )
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
                binary, *(extra_args or []), "@@",
            ]
        else:
            cmd = [
                "bash", str(SERIAL_SCRIPT),
                "-i", seed_dir,
                "-o", output_dir,
                binary, *(extra_args or []),
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
        "generated_kind": "symcc-candidates",
        "symcc_generated": num_generated,
        "unique": len(unique),
        "throughput": num_generated / elapsed if elapsed > 0 else 0,
        "throughput_kind": "symcc-candidates-per-second",
        "output_dir": output_dir,
        "retcode": retcode,
        "timed_out": timed_out,
    }


def _mpi_timeout_budget(timeout: int) -> tuple[int, int, int]:
    """Return (wall, per-exec, idle) limits that never exceed the campaign."""
    budget = max(1, int(timeout))
    wall_timeout = budget
    per_exec_timeout = min(30, max(1, budget // 4), wall_timeout)
    max_idle = min(max(1, budget // 6), wall_timeout)
    return wall_timeout, per_exec_timeout, max_idle


def run_mpi(binary: str, target_name: str, seed_dir: str, np: int, timeout: int,
            work_dir: str, simulate: bool = False,
            extra_args: list[str] | None = None):
    """Run MPI-parallel concolic execution."""
    output_dir = os.path.join(work_dir, f"mpi_np{np}_output")
    os.makedirs(output_dir, exist_ok=True)

    uses_file = TARGETS[target_name][3] if target_name in TARGETS else True
    # The MPI exploration window is the full declared campaign. Teardown and
    # post-run measurement belong to the protocol's separate wall grace.
    # All nested limits remain bounded by the campaign; a historical 10s floor
    # silently overran short engineering runs.
    wall_timeout, per_exec_timeout, max_idle = _mpi_timeout_budget(timeout)
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
        "generated_kind": "symcc-candidates",
        "symcc_generated": num_generated,
        "unique": num_unique,
        "output_dir": output_dir,
        "log_path": mpi_log_path,
        "stdout": stdout[-500:] if stdout else "",
        "stderr": stderr[-500:] if stderr else "",
        "retcode": retcode,
        "timed_out": timed_out,
        "num_masters": mpi_num_masters,
        "num_workers": mpi_num_workers,
        # The standalone MPI driver has no compute pools outside its rank
        # ledger. Measurement helpers run after the campaign window.
        "auxiliary_compute_slots": 0,
        "throughput": mpi_throughput or (num_generated / elapsed if elapsed > 0 else 0),
        "throughput_kind": "symcc-candidates-per-second",
    }


def _public_suite_prefix(suite_name: str) -> str:
    suite_prefixes = {"google-fts": "gfts-", "lava-m": "lava-"}
    return suite_prefixes.get(suite_name, suite_name + "-")


def discover_public_afl_variants() -> dict[str, dict[str, str]]:
    """Discover public AFL++ instrumentation variants."""
    variants: dict[str, dict[str, str]] = defaultdict(dict)
    pub_bin = PUBLIC_DIR / "bin"
    if not pub_bin.is_dir():
        return variants

    suffixes = [
        ("-afl-laf-ctx", "laf_ctx"),
        ("-afl-ngram4", "ngram4"),
        ("-afl-ngram", "ngram4"),
        ("-afl-laf", "laf"),
        ("-afl-ctx", "ctx"),
        ("-afl", "default"),
    ]
    for suite_dir in sorted(pub_bin.iterdir()):
        if not suite_dir.is_dir():
            continue
        suite_base = None
        variant_name = None
        for suffix, candidate in suffixes:
            if suite_dir.name.endswith(suffix):
                suite_base = suite_dir.name[: -len(suffix)]
                variant_name = candidate
                break
        if not suite_base or not variant_name:
            continue
        prefix = _public_suite_prefix(suite_base)
        for binary in sorted(suite_dir.iterdir()):
            if binary.is_file() and not binary.suffix and os.access(str(binary), os.X_OK):
                variants[prefix + binary.name][variant_name] = str(binary)
    return variants


def discover_public_afl_targets() -> dict[str, str]:
    """发现所有默认 AFL-instrumented 二进制文件。"""
    return {
        target: paths["default"]
        for target, paths in discover_public_afl_variants().items()
        if "default" in paths
    }


def discover_public_hfuzz_targets() -> dict[str, str]:
    """发现 honggfuzz-instrumented 二进制（public/bin/*-hfuzz/，用 hfuzz-clang 构建）。"""
    hf: dict[str, str] = {}
    pub_bin = PUBLIC_DIR / "bin"
    if not pub_bin.is_dir():
        return hf
    for suite_dir in sorted(pub_bin.iterdir()):
        if not suite_dir.is_dir() or not suite_dir.name.endswith("-hfuzz"):
            continue
        suite_base = suite_dir.name[:-6]  # 去掉 -hfuzz
        prefix = _public_suite_prefix(suite_base)
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


def _parse_symcc_interesting(stdout: str) -> int:
    """Return the final cumulative interesting count from MPI master output."""
    final = re.findall(
        r"Final stats:[^\n]*?(\d+)\s+interesting\b", stdout
    )
    if final:
        return int(final[-1])
    progress = re.findall(r"(\d+)\s+interesting\b", stdout)
    return int(progress[-1]) if progress else 0


def _parse_symcc_generated(stdout: str) -> int:
    """Return the final cumulative SymCC candidate count from master output."""
    final = re.findall(r"Final stats:[^\n]*?/\s*(\d+)\s+total\b", stdout)
    if final:
        return int(final[-1])
    progress = re.findall(
        r"\d+\s+interesting\s*/\s*(\d+)\s+(?:generated|total)\b",
        stdout,
    )
    return int(progress[-1]) if progress else 0


def _parse_auxiliary_compute_slots(stdout: str) -> int | None:
    """Return the hybrid master's declared non-rank compute concurrency."""
    matches = re.findall(r"Auxiliary compute slots:\s*([^\s(]+)", stdout)
    if not matches:
        return None
    token = matches[-1]
    # The current producer is capped at 296 slots.  Keep a larger protocol
    # ceiling for forward compatibility while rejecting corrupt/unbounded
    # decimal fields before Python's integer parser sees them.
    if (
        not token.isascii()
        or not token.isdecimal()
        or len(token) > 4
    ):
        return None
    value = int(token)
    return value if value <= 4096 else None


def _configure_hybrid_afl_sync(environment: dict[str, str]) -> int:
    """Enable periodic and final AFL campaign synchronization.

    Hybrid mode requires synchronization for the SymCC peer queue.  An
    inherited AFL_NO_SYNC would silently break the feedback loop, so it cannot
    remain active in this mode.
    """
    environment.pop("AFL_NO_SYNC", None)
    raw = environment.get("AFL_SYNC_TIME", "1")
    try:
        minutes = int(raw)
        if minutes < 1:
            raise ValueError
    except ValueError:
        minutes = 1
    environment["AFL_SYNC_TIME"] = str(minutes)
    environment["AFL_FINAL_SYNC"] = "1"
    return minutes


def _existing_foreign_queues(paths: typing.Iterable[str]) -> list[str]:
    """Return stable, deduplicated foreign queue directories."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        path = os.path.abspath(raw)
        identity = os.path.realpath(path)
        if identity in seen or not os.path.isdir(path):
            continue
        seen.add(identity)
        result.append(path)
    return result


def _read_afl_stat_fields(path: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        with open(path) as stream:
            for line in stream:
                key, separator, value = line.partition(":")
                if separator:
                    fields[key.strip()] = value.strip()
    except OSError:
        pass
    return fields


def _read_afl_sync_cursor(instance_dir: str, peer_name: str) -> int | None:
    """Read AFL++'s native next-ID cursor for a campaign peer queue."""
    path = os.path.join(instance_dir, ".synced", peer_name)
    try:
        with open(path, "rb") as stream:
            data = stream.read(5)
    except OSError:
        return None
    if len(data) != 4:
        return None
    return int.from_bytes(data, byteorder=sys.byteorder, signed=False)


def _wait_for_afl_peer_sync(
    instance_dir: str,
    peer_name: str,
    published: int,
    process: subprocess.Popen | None,
    timeout: float,
    *,
    cursor_reader: typing.Callable[[str, str], int | None] = (
        _read_afl_sync_cursor
    ),
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> tuple[int | None, bool, float]:
    """Give a live AFL master a bounded chance to consume a frozen peer tail."""
    expected = max(0, int(published))
    budget = max(0.0, float(timeout))
    started = monotonic()
    cursor = cursor_reader(instance_dir, peer_name)
    if expected == 0:
        return cursor, True, 0.0
    while cursor is None or cursor < expected:
        if process is None or process.poll() is not None:
            break
        elapsed = monotonic() - started
        if elapsed >= budget:
            break
        sleep(min(0.05, budget - elapsed))
        cursor = cursor_reader(instance_dir, peer_name)
    elapsed = max(0.0, monotonic() - started)
    return cursor, bool(cursor is not None and cursor >= expected), elapsed


def _count_afl_sync_imports(queue_dir: str, peer_name: str) -> int:
    """Count retained queue entries attributed to one AFL sync peer."""
    marker = f"sync:{peer_name},"
    try:
        return sum(
            marker in name
            for name in os.listdir(queue_dir)
            if os.path.isfile(os.path.join(queue_dir, name))
        )
    except OSError:
        return 0


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


def _count_jsonl_records(path: str) -> int:
    count = 0
    try:
        with open(path, "rb") as stream:
            for line in stream:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count


def _read_json_object(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_policy_artifacts(symcc_dir: str) -> dict:
    trajectory_path = os.path.join(symcc_dir, ".offline_trajectory.jsonl")
    policy_path = os.path.join(symcc_dir, ".offline_policy.json")
    algorithm_path = os.path.join(symcc_dir, ".smt_algorithm_state.json")
    policy = _read_json_object(policy_path)
    algorithm = _read_json_object(algorithm_path)
    clusters = algorithm.get("clusters", ()) if algorithm else ()
    prior_recommendations = int(
        algorithm.get("prior_recommendations", 0) or 0
    ) if algorithm else 0
    prior_matches = int(
        algorithm.get("prior_matches", 0) or 0
    ) if algorithm else 0
    return {
        "offline_trajectory": (
            trajectory_path if os.path.isfile(trajectory_path) else ""),
        "offline_events": _count_jsonl_records(trajectory_path),
        "offline_policy": policy_path if os.path.isfile(policy_path) else "",
        "offline_approved_action": str(policy.get("approved_action", "")),
        "offline_evaluations": int(policy.get("evaluations", 0) or 0)
        if policy else 0,
        "smt_algorithm_state": (
            algorithm_path if os.path.isfile(algorithm_path) else ""),
        "smt_algorithm_observations": int(
            algorithm.get("observations", 0) or 0) if algorithm else 0,
        "smt_algorithm_clusters": (
            len(clusters) if isinstance(clusters, list) else 0),
        "smt_sequence_prior_sha256": str(
            algorithm.get("prior_artifact_sha256", ""))
        if algorithm else "",
        "smt_sequence_prior_kind": str(
            algorithm.get("prior_kind", "")) if algorithm else "",
        "smt_sequence_prior_recommendations": prior_recommendations,
        "smt_sequence_prior_matches": prior_matches,
        "smt_sequence_prior_budgeted_assignments": int(
            algorithm.get("prior_budgeted_assignments", 0) or 0)
        if algorithm else 0,
        "smt_sequence_prior_schedule_completions": int(
            algorithm.get("prior_schedule_completions", 0) or 0)
        if algorithm else 0,
        "smt_sequence_prior_match_rate": (
            prior_matches / prior_recommendations
            if prior_recommendations else 0.0),
    }


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
               afl_variants: dict[str, str] | None = None,
               afl_profile_mode: str = "auto",
               directed_distance: str | None = None,
               task_graph: str | None = None,
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

    # 检测默认 AFL 目标是否为持久模式 + 共享内存（dual-mode）。各 profile 若选择
    # 其他编译变体，会在 spawn 时按实际 binary 重新判断是否需要 @@。
    default_afl_persistent = _afl_binary_uses_shmem(afl_binary)
    if default_afl_persistent:
        print("      AFL target is persistent+shmem -> feeding via shared memory "
              "(no @@); expect large exec/s gain")

    afl_env = os.environ.copy()
    afl_env["AFL_NO_UI"] = "1"  # 无 UI 模式，避免终端干扰
    afl_env["AFL_SKIP_CPUFREQ"] = "1"
    afl_env["AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES"] = "1"
    afl_env["AFL_AUTORESUME"] = "1"
    afl_sync_minutes = _configure_hybrid_afl_sync(afl_env)
    print(f"      AFL online sync ON (native symcc01 peer + final sync; "
          f"AFL_SYNC_TIME={afl_sync_minutes} minute(s))")
    default_data_cov = "1" if (adaptive and not default_afl_persistent) else "0"
    afl_data_cov_enabled = (
        os.environ.get("SYMCC_AFL_DATA_COVERAGE", default_data_cov) != "0")
    if afl_data_cov_enabled:
        data_rt = build_afl_data_coverage_runtime(work_dir)
        if data_rt:
            existing_preload = afl_env.get("AFL_PRELOAD", "")
            afl_env["AFL_PRELOAD"] = (
                data_rt if not existing_preload
                else f"{data_rt}:{existing_preload}")
            afl_env["SYMCC_AFL_DATA_COVERAGE"] = "1"
            print("      AFL native data coverage ON (AFL_PRELOAD comparison map)")
        else:
            print("      WARNING: could not build AFL data coverage preload; "
                  "continuing without native data map")
    elif adaptive and default_afl_persistent:
        print("      AFL native data coverage OFF by default for "
              "persistent+shmem target (set SYMCC_AFL_DATA_COVERAGE=1 to force)")

    default_hint_mutator = "1" if (adaptive and not default_afl_persistent) else "0"
    hint_mutator_enabled = (
        os.environ.get("SYMCC_AFL_HINT_MUTATOR", default_hint_mutator) != "0")
    if hint_mutator_enabled:
        if afl_supports_python_mutators():
            existing_pythonpath = afl_env.get("PYTHONPATH", "")
            afl_env["PYTHONPATH"] = (
                str(SYMCC_ROOT) if not existing_pythonpath
                else f"{SYMCC_ROOT}:{existing_pythonpath}")
            afl_env.setdefault("AFL_PYTHON_MODULE", "util.afl_symcc_hint_mutator")
            afl_env.setdefault(
                "SYMCC_HINT_DIR", os.path.join(afl_out_dir, "symcc01", "extras"))
            afl_env.setdefault(
                "SYMCC_POLY_CACHE_MUTATOR",
                os.path.join(afl_out_dir, "symcc01", ".poly_cache"))
            print("      AFL SymCC hint/poly mutator ON (AFL_PYTHON_MODULE)")
        else:
            print("      WARNING: afl-fuzz lacks Python mutator support; "
                  "continuing without hint mutator")
    elif adaptive and default_afl_persistent:
        print("      AFL SymCC hint/poly mutator OFF by default for "
              "persistent+shmem target (set SYMCC_AFL_HINT_MUTATOR=1 to force)")

    # xFUZZ/KRAKEN 风格 AFL++ profile 编排：不同实例不只换 power schedule，还可换
    # 编译期反馈（LAF/CompCov、CTX、Ngram）、MOpt、CmpLog 和运行时 env。缺少某个
    # 编译变体时回退到 default AFL 二进制，保证旧 benchmark 目录仍可运行。
    default_profile_switch = (
        "0" if afl_profile_mode == "off"
        else "1" if adaptive or afl_profile_mode in {"basic", "full"}
        else "0")
    raw_profile_switch = os.environ.get(
        "SYMCC_AFL_PROFILES", default_profile_switch)
    afl_profile_enabled = raw_profile_switch.lower() not in {
        "0", "false", "off", "no"
    }
    variant_binaries = {"default": afl_binary}
    for variant, path in (afl_variants or {}).items():
        if path and os.path.isfile(path):
            variant_binaries[variant] = path
    ensemble_profiles = (
        AFL_RUNTIME_PROFILES if afl_profile_enabled else [
            AflRuntimeProfile("explore-cmplog", "explore", "default",
                              use_cmplog=True),
            AflRuntimeProfile("mopt-fast", "fast", "default", use_mopt=True),
            AflRuntimeProfile("exploit", "exploit", "default",
                              use_cmplog=True,
                              env={"AFL_DISABLE_TRIM": "1"}),
        ])
    if afl_profile_enabled:
        variants_text = ",".join(sorted(variant_binaries))
        profile_text = ",".join(p.name for p in ensemble_profiles)
        print(f"      AFL++ strategy profiles ON "
              f"(variants={variants_text}; profiles={profile_text})")

    # AFL++ 外部（异构）引擎共享目录：honggfuzz 等把种子写到这些目录，主实例通过
    # -F 导入（需 -M）。SymCC 不走这里：它使用 afl_out/symcc01/queue 这一原生
    # campaign peer，由 AFL 的单调 ID 游标同步，避免 -F 秒级 mtime 的竞态。
    requested_foreign_dirs = os.environ.get(
        "AFL_FOREIGN_DIRS", "").split(":")
    foreign_dirs = _existing_foreign_queues([
        *[d for d, on in (
            (honggfuzz_out, honggfuzz_proc is not None),
            (grimoire_out, grimoire_proc is not None),
        ) if on],
        *requested_foreign_dirs,
    ])

    cmplog_persistent = (
        _afl_binary_uses_shmem(cmplog_binary) if cmplog_binary else None)

    afl_procs: list[subprocess.Popen] = []
    used_afl_profiles: list[str] = []
    warned_cmplog_mismatch: set[str] = set()

    def _spawn_afl(idx: int) -> subprocess.Popen:
        """启动第 idx 个 AFL 实例（idx=0 为主 -M，其余为差异化策略的从 -S）。"""
        fuzzer_name = f"fuzzer{idx + 1:02d}"
        is_master = (idx == 0)
        inst_env = dict(afl_env)
        profile = (AflRuntimeProfile("master-cmplog", "explore", "default",
                                     use_cmplog=True)
                   if is_master
                   else ensemble_profiles[(idx - 1) % len(ensemble_profiles)])
        variant_name = (
            profile.variant if profile.variant in variant_binaries else "default")
        runner_binary = variant_binaries[variant_name]
        runner_persistent = _afl_binary_uses_shmem(runner_binary)
        cmd = ["afl-fuzz"]
        if is_master:
            cmd += ["-M", fuzzer_name]
            # 主实例导入外部异构引擎的语料（集成共享）
            for directory in foreign_dirs:
                cmd += ["-F", directory]
        else:
            cmd += ["-S", fuzzer_name, "-p", profile.schedule]
            if profile.use_mopt:
                cmd += ["-L", "0"]  # 启用 MOpt 变异调度
            inst_env.update(profile.env)
        cmd += ["-i", seed_dir, "-o", afl_out_dir, "-m", "none"]
        cmplog_ok = bool(cmplog_binary) and cmplog_persistent == runner_persistent
        if cmplog_ok and profile.use_cmplog:
            cmd += ["-c", cmplog_binary, "-l", "2AT"]
        elif cmplog_binary and profile.use_cmplog:
            key = f"{variant_name}:{runner_persistent}"
            if key not in warned_cmplog_mismatch:
                warned_cmplog_mismatch.add(key)
                print(f"      WARNING: cmplog persistence mismatch for "
                      f"{variant_name} (main persistent={runner_persistent}); "
                      f"skipping CmpLog for this profile")
        cmd += ["--", runner_binary]
        if extra_args:
            cmd += extra_args
        if not runner_persistent:
            cmd.append("@@")   # 非持久目标：文件输入
        used_afl_profiles.append(f"{fuzzer_name}:{profile.name}:{variant_name}")
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
    initialization_budget = min(30.0, max(0.0, float(timeout)))
    while time.monotonic() - start < initialization_budget:
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
        remaining_init = (
            initialization_budget - (time.monotonic() - start))
        if remaining_init > 0:
            time.sleep(min(0.5, remaining_init))

    if not afl_ready:
        print(f"      WARNING: AFL did not initialize within "
              f"{initialization_budget:g}s campaign allowance")

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
    mpi_env.setdefault("SYMCC_WORKER_POSTPROCESS_BUDGET_SEC", "2.0")
    mpi_env.setdefault("SYMCC_BATCH_VERIFY_NEW", "0")
    mpi_env.setdefault("SYMCC_COVERAGE_GOSSIP", "0")
    mpi_env.setdefault("SYMCC_QUEUE_SCAN_BUDGET_SEC", "0.5")
    mpi_env.setdefault("SYMCC_QUEUE_SCAN_MAX", "4096")
    mpi_env.setdefault("SYMCC_AFL_SHOWMAP_TIMEOUT_MS", "1000")
    mpi_env.setdefault("SYMCC_VERIFY_PROPOSAL_BITMAP", "0")
    if directed_distance:
        mpi_env["SYMCC_DIRECTED_DISTANCE"] = directed_distance
        print(f"      Directed distance map: {directed_distance}")
    if task_graph:
        mpi_env["SYMCC_TASK_GRAPH"] = task_graph
        print(f"      Structural task graph: {task_graph}")
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
    mpi_stderr_bytes = b""
    # 两种模式都把 master stdout 重定向到日志文件（而非 PIPE）：helper 自身不会退出，
    # 会一直运行到被 SIGTERM。PIPE 在 64KB 写满后会死锁 master；且 communicate(timeout)
    # 因 helper 永不自退而必然超时、丢弃已产出的 stdout（导致 symcc_interesting 恒为 0）。
    # 写文件无容量上限、事后可完整读回。
    mpi_log_path = os.path.join(work_dir, "mpi_master.log")
    mpi_err_path = os.path.join(work_dir, "mpi_worker.err")
    mpi_log_fh = open(mpi_log_path, "wb")
    mpi_err_fh = open(mpi_err_path, "wb")
    mpi_proc = None
    try:
        if time.monotonic() - start < timeout:
            try:
                mpi_proc = subprocess.Popen(
                    mpi_cmd, stdout=mpi_log_fh, stderr=mpi_err_fh,
                    start_new_session=True, env=mpi_env, cwd=target_cwd,
                )
            except (OSError, subprocess.SubprocessError) as e:
                # MPI helper 启动失败时不抛出（否则跳过下方统一清理 → 已启动的
                # AFL/ensemble 进程沦为孤儿）；置 None，走正常清理。
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
            deadline = start + timeout
            while time.monotonic() < deadline and mpi_proc.poll() is None:
                time.sleep(max(
                    0.0, min(1.0, deadline - time.monotonic())))
    finally:
        # try/finally 确保控制器/等待循环即使抛异常也不泄漏文件句柄
        try:
            mpi_log_fh.close()
        except OSError:
            pass
        try:
            mpi_err_fh.close()
        except OSError:
            pass
    def _stop_process_groups(
            processes: typing.Iterable[subprocess.Popen]) -> None:
        for proc in processes:
            if proc.poll() is not None:
                continue
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()

    # The experiment window ends before shutdown/drain.  Cleanup latency must
    # not inflate campaign wall time or depress the reported throughput.
    campaign_elapsed = time.monotonic() - start
    cleanup_started = time.monotonic()

    # 先冻结所有语料生产者，再给存活 AFL master 一个有界 drain 窗口，
    # 然后停 secondary，最后停 master。AFL_FINAL_SYNC=1 仍在 master 退出时
    # 执行最后同步；drain 窗口用来吸收结束边界刚发布的 SymCC ID 尾部。
    producers = [
        proc for proc in (mpi_proc, honggfuzz_proc, grimoire_proc)
        if proc is not None
    ]
    _stop_process_groups(producers)
    frozen_symcc_queue = os.path.join(afl_out_dir, "symcc01", "queue")
    frozen_symcc_published = count_output_files(frozen_symcc_queue)
    try:
        sync_drain_budget = min(
            30.0,
            max(0.0, float(os.environ.get("SYMCC_AFL_SYNC_DRAIN_SEC", "5"))),
        )
    except ValueError:
        sync_drain_budget = 5.0
    afl_master_dir = os.path.join(afl_out_dir, "fuzzer01")
    (
        symcc_cursor_before_final_sync,
        symcc_peer_pre_stop_complete,
        symcc_sync_drain_seconds,
    ) = _wait_for_afl_peer_sync(
        afl_master_dir,
        "symcc01",
        frozen_symcc_published,
        afl_procs[0] if afl_procs else None,
        sync_drain_budget,
    )
    _stop_process_groups(afl_procs[1:])
    _stop_process_groups(afl_procs[:1])
    cleanup_elapsed = time.monotonic() - cleanup_started

    # MPI 已退出并刷新日志后再读取，保留最后一批统计与 final stats。
    try:
        with open(mpi_log_path, "rb") as _f:
            mpi_stdout_bytes = _f.read()
    except OSError:
        pass
    try:
        with open(mpi_err_path, "rb") as _f:
            mpi_stderr_bytes = _f.read()
    except OSError:
        pass

    # Worker profiling CSV files can already be redirected outside the
    # temporary campaign directory via SYMCC_WPROF_DIR.  Preserve the master
    # logs there as well: they contain the scan/dispatch/receive/triage timing
    # summary needed to explain scaling bottlenecks after work_dir is removed.
    profile_export_dir = mpi_env.get("SYMCC_WPROF_DIR", "")
    if profile_export_dir:
        try:
            os.makedirs(profile_export_dir, exist_ok=True)
            for source, name in (
                    (mpi_log_path, "mpi_master.log"),
                    (mpi_err_path, "mpi_worker.err")):
                if os.path.isfile(source):
                    shutil.copy2(source, os.path.join(profile_export_dir, name))
        except OSError as e:
            print(f"      WARNING: could not export MPI profile logs: {e}")

    elapsed = campaign_elapsed

    # 读取 MPI 输出
    mpi_stdout = ""
    try:
        mpi_stdout = mpi_stdout_bytes.decode(errors="replace")
    except (AttributeError, UnicodeDecodeError):
        pass
    mpi_stderr = ""
    try:
        mpi_stderr = mpi_stderr_bytes.decode(errors="replace")
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

    afl_retained_files = sum(count_output_files(q) for q in afl_queues)
    afl_unique_hashes = get_unique_hashes_many(afl_queues)

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
            if _is_corpus_filename(f) and os.path.isfile(src):
                _link_or_copy(src, os.path.join(combined_dir, f"afl_{inst}_{f}"))

    # 复制 SymCC queue (afl-showmap 过滤后的 interesting)
    if os.path.isdir(symcc_queue):
        for f in os.listdir(symcc_queue):
            src = os.path.join(symcc_queue, f)
            if _is_corpus_filename(f) and os.path.isfile(src):
                _link_or_copy(src, os.path.join(combined_dir, f"symcc_{f}"))

    # 复制所有 SymCC 输出（未过滤）— 这些可能有 lcov 覆盖率提升
    symcc_all_count = 0
    if os.path.isdir(symcc_all_dir):
        for f in os.listdir(symcc_all_dir):
            src = os.path.join(symcc_all_dir, f)
            if _is_corpus_filename(f) and os.path.isfile(src):
                dest = os.path.join(combined_dir, f"symcc_all_{f}")
                if not os.path.exists(dest):
                    if _link_or_copy(src, dest):
                        symcc_all_count += 1

    # Parse the last cumulative value, not the first progress update.  The
    # on-disk snapshot and accepted queue are conservative fallbacks when MPI
    # is killed before its final log line is flushed.
    symcc_interesting = _parse_symcc_interesting(mpi_stdout)
    symcc_generated = _parse_symcc_generated(mpi_stdout)
    auxiliary_compute_slots = _parse_auxiliary_compute_slots(mpi_stdout)
    stats_snapshot = _read_symcc_stats(symcc_dir)
    if stats_snapshot is not None:
        symcc_interesting = max(symcc_interesting, stats_snapshot[0])
        symcc_generated = max(symcc_generated, stats_snapshot[1])
    symcc_generated = max(symcc_generated, symcc_all_count)
    symcc_interesting = max(
        symcc_interesting, count_output_files(symcc_queue)
    )

    # 从各实例 fuzzer_stats 聚合 AFL 指标：
    # execs 累加（总吞吐），bitmap_cvg/edges 取最大（各实例 queue 已同步，覆盖近似一致）
    afl_bitmap_cvg = ""
    afl_execs_done = 0
    afl_execs_per_sec = 0.0
    afl_edges_found = 0
    afl_total_edges = 0
    afl_corpus_imported = 0
    afl_master_corpus_imported = 0
    afl_master_sync_time = 0
    _best_bitmap = -1.0
    for inst_dir in sorted(os.listdir(afl_out_dir)) if os.path.isdir(afl_out_dir) else []:
        if not inst_dir.startswith("fuzzer"):
            continue
        sp = os.path.join(afl_out_dir, inst_dir, "fuzzer_stats")
        if not os.path.isfile(sp):
            continue
        fields = _read_afl_stat_fields(sp)
        try:
            bitmap = fields.get("bitmap_cvg", "")
            if bitmap:
                bc = float(bitmap.rstrip("%"))
                if bc > _best_bitmap:
                    _best_bitmap = bc
                    afl_bitmap_cvg = bitmap
            afl_execs_done += int(fields.get("execs_done", "0"))
            afl_execs_per_sec += float(fields.get("execs_per_sec", "0"))
            afl_edges_found = max(
                afl_edges_found, int(fields.get("edges_found", "0")))
            afl_total_edges = max(
                afl_total_edges, int(fields.get("total_edges", "0")))
            imported = int(fields.get("corpus_imported", "0"))
            afl_corpus_imported += imported
            if inst_dir == "fuzzer01":
                afl_master_corpus_imported = imported
                afl_master_sync_time = int(fields.get("sync_time", "0"))
        except ValueError:
            pass

    symcc_peer_published = count_output_files(symcc_queue)
    symcc_cursor = _read_afl_sync_cursor(afl_master_dir, "symcc01")
    symcc_peer_scanned = symcc_cursor if symcc_cursor is not None else 0
    symcc_peer_imported = _count_afl_sync_imports(
        os.path.join(afl_master_dir, "queue"), "symcc01")
    symcc_peer_not_retained = max(
        0, symcc_peer_scanned - symcc_peer_imported)
    symcc_peer_sync_complete = int(
        symcc_peer_published == 0
        or (
            symcc_cursor is not None
            and symcc_peer_scanned >= symcc_peer_published
        )
    )

    policy_artifacts = _read_policy_artifacts(symcc_dir)
    retained_unique = len(get_unique_hashes(combined_dir))
    total_executed_or_generated = afl_execs_done + symcc_generated

    return {
        "wall_time": elapsed,
        # AFL does not expose a mutation-generation counter. execs_done is the
        # reproducible execution numerator; SymCC reports produced candidates.
        "generated": total_executed_or_generated,
        "generated_kind": "afl-executions-plus-symcc-candidates",
        "unique": retained_unique,
        "output_dir": combined_dir,
        "retcode": (mpi_proc.returncode or 0) if mpi_proc is not None else -1,
        "timed_out": elapsed >= timeout * 0.95,
        "throughput": (
            total_executed_or_generated / elapsed if elapsed > 0 else 0),
        "throughput_kind": "afl-executions-plus-symcc-candidates-per-second",
        "stdout": mpi_stdout[-500:] if mpi_stdout else "",
        "stderr": mpi_stderr[-500:] if mpi_stderr else "",
        "afl_generated": afl_execs_done,
        "afl_executions": afl_execs_done,
        "symcc_generated": symcc_generated,
        "afl_retained_files": afl_retained_files,
        "afl_retained_unique": len(afl_unique_hashes),
        "symcc_output_files": symcc_all_count,
        "symcc_interesting": symcc_interesting,
        "afl_bitmap_cvg": afl_bitmap_cvg,
        "afl_execs_done": afl_execs_done,
        "afl_execs_per_sec": afl_execs_per_sec,
        "afl_edges_found": afl_edges_found,
        "afl_total_edges": afl_total_edges,
        "afl_corpus_imported": afl_corpus_imported,
        "afl_master_corpus_imported": afl_master_corpus_imported,
        "afl_master_sync_time": afl_master_sync_time,
        "symcc_peer_published": symcc_peer_published,
        "symcc_peer_scanned": symcc_peer_scanned,
        "symcc_peer_imported": symcc_peer_imported,
        "symcc_peer_not_retained": symcc_peer_not_retained,
        "symcc_peer_sync_complete": symcc_peer_sync_complete,
        "symcc_peer_pre_stop_complete": int(symcc_peer_pre_stop_complete),
        "symcc_peer_cursor_before_final_sync": (
            symcc_cursor_before_final_sync
            if symcc_cursor_before_final_sync is not None
            else 0
        ),
        "symcc_peer_frozen_published": frozen_symcc_published,
        "symcc_sync_drain_seconds": symcc_sync_drain_seconds,
        "cleanup_time": cleanup_elapsed,
        "afl_sync_time_minutes": afl_sync_minutes,
        "num_masters": 1,
        "num_workers": symcc_np - 1,
        "auxiliary_compute_slots": auxiliary_compute_slots,
        "afl_instances": len(afl_procs),
        "afl_profiles": ",".join(used_afl_profiles),
        "afl_variants": ",".join(sorted(variant_binaries)),
        **policy_artifacts,
    }


def run_afl_only(afl_binary: str, target_name: str,
                 seed_dir: str, timeout: int, work_dir: str,
                 extra_args: list[str] | None = None,
                 cmplog_binary: str | None = None,
                 instances: int = 1,
                 afl_variants: dict[str, str] | None = None,
                 afl_profile_mode: str = "auto") -> dict:
    """Run an AFL-only baseline using an explicit, reproducible core budget."""
    afl_out_dir = os.path.join(work_dir, "afl_out")
    os.makedirs(afl_out_dir, exist_ok=True)
    target_cwd = os.path.join(work_dir, "target_cwd")
    os.makedirs(target_cwd, exist_ok=True)
    instances = max(1, int(instances))
    afl_env = os.environ.copy()
    afl_env["AFL_NO_UI"] = "1"
    afl_env["AFL_SKIP_CPUFREQ"] = "1"
    afl_env["AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES"] = "1"
    afl_env["AFL_AUTORESUME"] = "1"

    variant_binaries = {"default": afl_binary}
    for variant, path in (afl_variants or {}).items():
        if path and os.path.isfile(path):
            variant_binaries[variant] = path
    profiles = (
        AFL_RUNTIME_PROFILES if afl_profile_mode != "off" else [
            AflRuntimeProfile("explore", "explore", "default")])
    cmplog_persistent = (
        _afl_binary_uses_shmem(cmplog_binary) if cmplog_binary else None)
    afl_procs: list[subprocess.Popen] = []
    used_profiles: list[str] = []

    def terminate_all() -> None:
        for proc in afl_procs:
            if proc.poll() is not None:
                continue
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()

    for index in range(instances):
        name = f"fuzzer{index + 1:02d}"
        is_master = index == 0
        profile = (
            AflRuntimeProfile("master-cmplog", "explore", "default",
                              use_cmplog=True)
            if is_master else profiles[(index - 1) % len(profiles)])
        variant = (
            profile.variant if profile.variant in variant_binaries else "default")
        runner = variant_binaries[variant]
        persistent = _afl_binary_uses_shmem(runner)
        cmd = ["afl-fuzz"]
        if is_master:
            cmd.extend(["-M", name])
        else:
            cmd.extend(["-S", name, "-p", profile.schedule])
            if profile.use_mopt:
                cmd.extend(["-L", "0"])
        cmd.extend(["-i", seed_dir, "-o", afl_out_dir, "-m", "none"])
        if (cmplog_binary and profile.use_cmplog
                and cmplog_persistent == persistent):
            cmd.extend(["-c", cmplog_binary, "-l", "2AT"])
        cmd.extend(["--", runner])
        if extra_args:
            cmd.extend(extra_args)
        if not persistent:
            cmd.append("@@")
        instance_env = dict(afl_env)
        instance_env.update(profile.env)
        try:
            afl_procs.append(subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=instance_env, cwd=target_cwd))
        except (OSError, subprocess.SubprocessError):
            terminate_all()
            return {
                "wall_time": 0, "generated": 0, "unique": 0,
                "output_dir": afl_out_dir, "retcode": -1,
                "timed_out": False, "throughput": 0,
                "stdout": "", "stderr": "AFL spawn failed",
                "afl_execs_done": 0, "afl_execs_per_sec": 0,
                "afl_instances": len(afl_procs),
            }
        used_profiles.append(f"{name}:{profile.name}:{variant}")

    print(f"      Starting AFL-only with {instances} instance(s)...")

    start = time.monotonic()
    afl_proc = afl_procs[0]

    # 等待 AFL 初始化
    fuzzer_dir = os.path.join(afl_out_dir, "fuzzer01")
    stats_path = os.path.join(fuzzer_dir, "fuzzer_stats")
    afl_ready = False
    initialization_budget = min(30.0, max(0.0, float(timeout)))
    while time.monotonic() - start < initialization_budget:
        if os.path.isfile(stats_path):
            afl_ready = True
            break
        if afl_proc.poll() is not None:
            print(f"      AFL exited early (ret={afl_proc.returncode})")
            terminate_all()
            return {
                "wall_time": time.monotonic() - start,
                "generated": 0, "unique": 0, "output_dir": afl_out_dir,
                "retcode": afl_proc.returncode, "timed_out": False,
                "throughput": 0, "stdout": "", "stderr": "",
                "afl_execs_done": 0, "afl_execs_per_sec": 0,
                "afl_instances": instances,
            }
        remaining_init = (
            initialization_budget - (time.monotonic() - start))
        if remaining_init > 0:
            time.sleep(min(0.5, remaining_init))

    if not afl_ready:
        print(f"      WARNING: AFL did not initialize within "
              f"{initialization_budget:g}s campaign allowance")

    # 等待 timeout
    remaining = timeout - (time.monotonic() - start)
    if remaining > 0:
        try:
            afl_proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass

    terminate_all()

    elapsed = time.monotonic() - start

    # 收集结果
    afl_queues = [
        os.path.join(afl_out_dir, f"fuzzer{index + 1:02d}", "queue")
        for index in range(instances)
        if os.path.isdir(os.path.join(
            afl_out_dir, f"fuzzer{index + 1:02d}", "queue"))
    ]
    afl_retained_files = sum(count_output_files(path) for path in afl_queues)
    afl_unique_hashes = get_unique_hashes_many(afl_queues)

    # 合并种子和 AFL queue 用于覆盖率测量
    combined_dir = os.path.join(work_dir, "combined_output")
    os.makedirs(combined_dir, exist_ok=True)

    for f in os.listdir(seed_dir):
        src = os.path.join(seed_dir, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(combined_dir, f"seed_{f}"))

    for queue in afl_queues:
        instance = os.path.basename(os.path.dirname(queue))
        for f in os.listdir(queue):
            src = os.path.join(queue, f)
            if _is_corpus_filename(f) and os.path.isfile(src):
                _link_or_copy(
                    src, os.path.join(combined_dir, f"afl_{instance}_{f}"))

    # 从 fuzzer_stats 解析关键指标
    afl_bitmap_cvg = ""
    afl_execs_done = 0
    afl_execs_per_sec = 0.0
    afl_corpus_count = 0
    afl_edges_found = 0
    afl_total_edges = 0
    best_bitmap = -1.0
    for index in range(instances):
        path = os.path.join(
            afl_out_dir, f"fuzzer{index + 1:02d}", "fuzzer_stats")
        if not os.path.isfile(path):
            continue
        fields = _read_afl_stat_fields(path)
        try:
            bitmap = fields.get("bitmap_cvg", "")
            if bitmap and float(bitmap.rstrip("%")) > best_bitmap:
                best_bitmap = float(bitmap.rstrip("%"))
                afl_bitmap_cvg = bitmap
            afl_execs_done += int(fields.get("execs_done", "0"))
            afl_execs_per_sec += float(fields.get("execs_per_sec", "0"))
            afl_corpus_count += int(fields.get("corpus_count", "0"))
            afl_edges_found = max(
                afl_edges_found, int(fields.get("edges_found", "0")))
            afl_total_edges = max(
                afl_total_edges, int(fields.get("total_edges", "0")))
        except ValueError:
            pass

    return {
        "wall_time": elapsed,
        "generated": afl_execs_done,
        "generated_kind": "afl-executions",
        "unique": len(afl_unique_hashes),
        "output_dir": combined_dir,
        "retcode": afl_proc.returncode or 0,
        "timed_out": elapsed >= timeout * 0.95,
        "throughput": afl_execs_done / elapsed if elapsed > 0 else 0,
        "throughput_kind": "afl-executions-per-second",
        "stdout": "",
        "stderr": "",
        "afl_bitmap_cvg": afl_bitmap_cvg,
        "afl_execs_done": afl_execs_done,
        "afl_execs_per_sec": afl_execs_per_sec,
        "afl_corpus_count": afl_corpus_count,
        "afl_edges_found": afl_edges_found,
        "afl_total_edges": afl_total_edges,
        "afl_generated": afl_execs_done,
        "afl_executions": afl_execs_done,
        "afl_retained_files": afl_retained_files,
        "afl_retained_unique": len(afl_unique_hashes),
        "afl_instances": instances,
        "afl_profiles": ",".join(used_profiles),
        "afl_variants": ",".join(sorted(variant_binaries)),
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
            "experiment_id", "run_id", "protocol_run_id", "pair_id",
            "phase", "random_seed", "status", "failure_reason", "retcode",
            "budget_exhausted",
            "target", "configuration", "mode", "np", "round",
            "allocated_cpu_cores", "num_masters", "num_workers",
            "auxiliary_compute_slots", "cpu_budget_seconds",
            "wall_budget_seconds", "coverage_auc",
            "wall_time_sec", "generated", "generated_kind",
            "afl_generated", "afl_executions",
            "symcc_generated", "symcc_interesting",
            "symcc_peer_published", "symcc_peer_scanned",
            "symcc_peer_imported", "symcc_peer_not_retained",
            "symcc_peer_sync_complete", "symcc_peer_pre_stop_complete",
            "symcc_peer_cursor_before_final_sync",
            "symcc_peer_frozen_published", "symcc_sync_drain_seconds",
            "cleanup_time_sec", "afl_master_corpus_imported",
            "afl_corpus_imported", "afl_master_sync_time",
            "afl_sync_time_minutes",
            "unique", "throughput_per_sec", "throughput_kind",
            "afl_retained_files", "afl_retained_unique",
            "symcc_output_files", "afl_instances",
            "edge_cov_pct", "edges_found", "edges_total",
            "coverage_measure_ok", "coverage_edge_count_ok",
            "coverage_denominator_kind", "coverage_map_size",
            "coverage_sampled", "coverage_sampled_cases",
            "coverage_total_cases", "crashes",
            "speedup", "efficiency",
            "afl_bitmap_cvg", "afl_edges_found", "afl_total_edges",
            "afl_execs_done", "afl_execs_per_sec",
            "afl_profiles", "afl_variants",
            "offline_events", "offline_approved_action",
            "offline_evaluations", "offline_trajectory", "offline_policy",
            "smt_algorithm_observations", "smt_algorithm_clusters",
            "smt_algorithm_state", "smt_sequence_prior_sha256",
            "smt_sequence_prior_kind",
            "smt_sequence_prior_recommendations",
            "smt_sequence_prior_matches",
            "smt_sequence_prior_budgeted_assignments",
            "smt_sequence_prior_schedule_completions",
            "smt_sequence_prior_match_rate",
        ])
        for row in results:
            writer.writerow([
                row.get("experiment_id", ""),
                row.get("run_id", ""),
                row.get("protocol_run_id", ""),
                row.get("pair_id", ""),
                row.get("phase", ""),
                row.get("random_seed", 0),
                row.get("status", ""),
                row.get("failure_reason", ""),
                row.get("retcode", ""),
                int(bool(row.get("budget_exhausted", False))),
                row["target"], row.get("configuration", row["mode"]),
                row["mode"], row["np"], row["round"],
                row.get("allocated_cpu_cores", 1),
                row.get("num_masters", 0),
                row.get("num_workers", 0),
                row.get("auxiliary_compute_slots", ""),
                f"{row.get('cpu_budget_seconds', 0.0):.3f}",
                f"{row.get('wall_budget_seconds', 0.0):.3f}",
                f"{row.get('coverage_auc', 0.0):.6f}",
                f"{row['wall_time']:.2f}", row["generated"],
                row.get("generated_kind", "unspecified"),
                row.get("afl_generated", 0),
                row.get("afl_executions", row.get("afl_execs_done", 0)),
                row.get("symcc_generated", 0),
                row.get("symcc_interesting", 0),
                row.get("symcc_peer_published", 0),
                row.get("symcc_peer_scanned", 0),
                row.get("symcc_peer_imported", 0),
                row.get("symcc_peer_not_retained", 0),
                row.get("symcc_peer_sync_complete", 0),
                row.get("symcc_peer_pre_stop_complete", 0),
                row.get("symcc_peer_cursor_before_final_sync", 0),
                row.get("symcc_peer_frozen_published", 0),
                f"{row.get('symcc_sync_drain_seconds', 0.0):.3f}",
                f"{row.get('cleanup_time', 0.0):.3f}",
                row.get("afl_master_corpus_imported", 0),
                row.get("afl_corpus_imported", 0),
                row.get("afl_master_sync_time", 0),
                row.get("afl_sync_time_minutes", 0),
                row["unique"],
                f"{row.get('throughput', 0.0):.2f}",
                row.get("throughput_kind", "unspecified"),
                row.get("afl_retained_files", 0),
                row.get("afl_retained_unique", 0),
                row.get("symcc_output_files", 0),
                row.get("afl_instances", 0),
                f"{row.get('edge_cov', 0.0):.2f}",
                row.get("edges_found", 0),
                row.get("edges_total", 0),
                int(bool(row.get("coverage_measure_ok", False))),
                int(bool(row.get("coverage_edge_count_ok", False))),
                row.get("coverage_denominator_kind", "unavailable"),
                row.get("coverage_map_size", 0),
                int(bool(row.get("coverage_sampled", False))),
                row.get("coverage_sampled_cases", 0),
                row.get("coverage_total_cases", 0),
                row.get("crashes", 0),
                f"{row.get('speedup', 1.0):.2f}",
                f"{row.get('efficiency', 100.0):.1f}",
                row.get("afl_bitmap_cvg", ""),
                row.get("afl_edges_found", 0),
                row.get("afl_total_edges", 0),
                row.get("afl_execs_done", 0),
                row.get("afl_execs_per_sec", 0),
                row.get("afl_profiles", ""),
                row.get("afl_variants", ""),
                row.get("offline_events", 0),
                row.get("offline_approved_action", ""),
                row.get("offline_evaluations", 0),
                row.get("offline_trajectory", ""),
                row.get("offline_policy", ""),
                row.get("smt_algorithm_observations", 0),
                row.get("smt_algorithm_clusters", 0),
                row.get("smt_algorithm_state", ""),
                row.get("smt_sequence_prior_sha256", ""),
                row.get("smt_sequence_prior_kind", ""),
                row.get("smt_sequence_prior_recommendations", 0),
                row.get("smt_sequence_prior_matches", 0),
                row.get("smt_sequence_prior_budgeted_assignments", 0),
                row.get("smt_sequence_prior_schedule_completions", 0),
                row.get("smt_sequence_prior_match_rate", 0.0),
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
                avg_symcc_generated = sum(
                    r.get(
                        "symcc_generated",
                        r["generated"] if mode in ("serial", "mpi") else 0,
                    )
                    for r in rows
                ) / len(rows)
                avg_afl_executions = sum(
                    r.get("afl_executions", r.get("afl_execs_done", 0))
                    for r in rows
                ) / len(rows)
                avg_uniq = sum(r["unique"] for r in rows) / len(rows)
                avg_edge_cov = sum(r.get("edge_cov", 0) for r in rows) / len(rows)
                avg_edges_found = sum(r.get("edges_found", 0) for r in rows) / len(rows)
                avg_edges_total = sum(r.get("edges_total", 0) for r in rows) / len(rows)
                total_crashes = sum(r.get("crashes", 0) for r in rows)
                avg_workers = sum(r.get("num_workers", np_val - 1) for r in rows) / len(rows)
                avg_throughput = sum(r.get("throughput", 0) for r in rows) / len(rows)
                throughput_kinds = {
                    r.get("throughput_kind", "unspecified") for r in rows
                }
                throughput_kind = (
                    next(iter(throughput_kinds))
                    if len(throughput_kinds) == 1 else "mixed"
                )
                # fuzzer_stats 指标（仅 hybrid 和 afl-only 有值）
                avg_afl_edges_found = sum(r.get("afl_edges_found", 0) for r in rows) / len(rows)
                avg_afl_total_edges = sum(r.get("afl_total_edges", 0) for r in rows) / len(rows)
                avg_afl_bitmap_cvg = sum(parse_bitmap_cvg(r.get("afl_bitmap_cvg", "")) for r in rows) / len(rows)
                avg_afl_execs_done = sum(r.get("afl_execs_done", 0) for r in rows) / len(rows)
                avg_afl_execs_per_sec = sum(r.get("afl_execs_per_sec", 0) for r in rows) / len(rows)
                avg_afl_master_corpus_imported = sum(
                    r.get("afl_master_corpus_imported", 0)
                    for r in rows) / len(rows)
                avg_symcc_peer_published = sum(
                    r.get("symcc_peer_published", 0)
                    for r in rows) / len(rows)
                avg_symcc_peer_scanned = sum(
                    r.get("symcc_peer_scanned", 0)
                    for r in rows) / len(rows)
                avg_symcc_peer_imported = sum(
                    r.get("symcc_peer_imported", 0)
                    for r in rows) / len(rows)
                avg_symcc_peer_not_retained = sum(
                    r.get("symcc_peer_not_retained", 0)
                    for r in rows) / len(rows)
                symcc_peer_sync_complete_runs = sum(
                    int(bool(r.get("symcc_peer_sync_complete", 0)))
                    for r in rows)
                summaries.append({
                    "mode": mode,
                    "np": np_val,
                    "avg_time": avg_time,
                    "avg_symcc_generated": avg_symcc_generated,
                    "avg_afl_executions": avg_afl_executions,
                    "avg_unique": avg_uniq,
                    "avg_throughput": avg_throughput,
                    "throughput_kind": throughput_kind,
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
                    "avg_afl_master_corpus_imported":
                        avg_afl_master_corpus_imported,
                    "avg_symcc_peer_published": avg_symcc_peer_published,
                    "avg_symcc_peer_scanned": avg_symcc_peer_scanned,
                    "avg_symcc_peer_imported": avg_symcc_peer_imported,
                    "avg_symcc_peer_not_retained":
                        avg_symcc_peer_not_retained,
                    "symcc_peer_sync_complete_runs":
                        symcc_peer_sync_complete_runs,
                })

            # Find the serial baseline for like-for-like candidate throughput.
            serial_throughput = None
            serial_throughput_kind = None
            for s in summaries:
                if s["mode"] == "serial":
                    serial_throughput = s["avg_throughput"]
                    serial_throughput_kind = s["throughput_kind"]

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
                   f"{'SymCCGen':>10} {'AFLExec':>10} {'Unique':>8} ")
            sep = (f"  {'─'*10} {'─'*4} {'─'*10} "
                   f"{'─'*10} {'─'*10} {'─'*8} ")
            if has_cov:
                hdr += f"{'ShowmapCov':>11} {'Edges':>14} "
                sep += f"{'─'*11} {'─'*14} "
            if has_fstats:
                hdr += f"{'FstatsCov':>10} {'FEdges':>14} "
                sep += f"{'─'*10} {'─'*14} "
            hdr += f"{'AFL/s':>10}\n"
            sep += f"{'─'*10}\n"
            f.write(hdr)
            f.write(sep)

            for s in summaries:
                line = (f"  {s['mode']:<10} {s['np']:>4} "
                        f"{format_time(s['avg_time']):>10} "
                        f"{s['avg_symcc_generated']:>10.0f} "
                        f"{s['avg_afl_executions']:>10.0f} "
                        f"{s['avg_unique']:>8.0f} ")
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
                afl_eps = s.get("avg_afl_execs_per_sec", 0)
                line += f"{afl_eps:>10.0f}\n"
                f.write(line)

            f.write("\n")

            if any(s["mode"] == "hybrid" for s in summaries):
                f.write("  Online SymCC -> AFL Native Peer Sync:\n")
                for s in summaries:
                    if s["mode"] != "hybrid":
                        continue
                    f.write(
                        f"    hybrid np={s['np']}: "
                        f"published={s['avg_symcc_peer_published']:.1f}, "
                        f"scanned={s['avg_symcc_peer_scanned']:.1f}, "
                        f"imported={s['avg_symcc_peer_imported']:.1f}, "
                        f"not-retained="
                        f"{s['avg_symcc_peer_not_retained']:.1f}, "
                        f"complete-runs="
                        f"{s['symcc_peer_sync_complete_runs']}/"
                        f"{s['rounds']}\n")
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

            # This is an end-to-end coverage delta, not an attribution claim.
            if seed_cov > 0:
                f.write("  Coverage Gain over Seed Baseline:\n")
                f.write(f"    Seed baseline: {seed_cov:.2f}% ({seed_edges} edges)\n")
                for s in summaries:
                    if s["mode"] in ("mpi", "hybrid", "afl-only") and s["avg_edge_cov"] > 0:
                        abs_gain = s["avg_edge_cov"] - seed_cov
                        rel_gain = (abs_gain / seed_cov * 100) if seed_cov > 0 else 0
                        label = f"{s['mode']} np={s['np']}"
                        f.write(f"    {label:>16}: {s['avg_edge_cov']:.2f}% "
                                f"(+{abs_gain:.2f}pp, +{rel_gain:.1f}% relative)\n")
                f.write("\n")

            # Compare throughput only when both rows use exactly the same unit.
            f.write("  Like-for-like Throughput Speedup vs Serial:\n")
            max_speedup = 1.0
            speedups = []
            for s in summaries:
                if (serial_throughput and serial_throughput > 0
                        and s["mode"] != "serial"
                        and s["throughput_kind"] == serial_throughput_kind):
                    sp = (s["avg_throughput"] / serial_throughput
                          if serial_throughput > 0 else 0)
                elif s["mode"] == "serial":
                    sp = 1.0
                else:
                    sp = None
                speedups.append(sp)
                if sp is not None:
                    max_speedup = max(max_speedup, sp)
            scale = 40 / max_speedup if max_speedup > 0 else 1
            for s, sp in zip(summaries, speedups):
                label = f"  {s['mode']} np={s['np']:>3}"
                if sp is None:
                    f.write(
                        f"  {label} |{'N/A':^40}| different unit: "
                        f"{s['throughput_kind']}\n")
                    continue
                bar_len = int(sp * scale)
                bar = "█" * bar_len + "░" * max(0, 40 - bar_len)
                f.write(f"  {label} |{bar}| {sp:.1f}x\n")
            f.write("\n")

        # Overall summary
        f.write(f"\n{'=' * 80}\n")
        f.write("  OVERALL SUMMARY\n")
        f.write(f"{'=' * 80}\n\n")

        # Retained content hashes have one meaning in every mode; generic
        # generated/throughput values do not. Rank by coverage, then corpus
        # cardinality, and report engine-native counters separately.
        for target in sorted(by_target.keys()):
            configs = by_target[target]
            best = None
            best_score = None
            for (mode, np_val), rows in configs.items():
                avg_time = sum(r["wall_time"] for r in rows) / len(rows)
                avg_unique = sum(r["unique"] for r in rows) / len(rows)
                avg_symcc_generated = sum(
                    r.get(
                        "symcc_generated",
                        r["generated"] if mode in ("serial", "mpi") else 0,
                    )
                    for r in rows
                ) / len(rows)
                avg_afl_executions = sum(
                    r.get("afl_executions", r.get("afl_execs_done", 0))
                    for r in rows
                ) / len(rows)
                avg_edge_cov = sum(r.get("edge_cov", 0) for r in rows) / len(rows)
                total_crashes = sum(r.get("crashes", 0) for r in rows)
                score = (avg_edge_cov, avg_unique)
                if best_score is None or score > best_score:
                    best_score = score
                    best = {
                        "mode": mode, "np": np_val,
                        "time": avg_time, "unique": avg_unique,
                        "symcc_generated": avg_symcc_generated,
                        "afl_executions": avg_afl_executions,
                        "edge_cov": avg_edge_cov,
                        "crashes": total_crashes,
                    }

            if best:
                info = (f"  {target}: best = {best['mode']} np={best['np']} "
                        f"({format_time(best['time'])}, "
                        f"{best['unique']:.0f} retained unique, "
                        f"{best['symcc_generated']:.0f} SymCC candidates, "
                        f"{best['afl_executions']:.0f} AFL executions")
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
    parser.add_argument("--bin-dir", default=None,
                        help="Shared directory for built/reused binaries "
                             "(default: OUTPUT/bin)")
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
    parser.add_argument("--aflpp-profiles", choices=["auto", "off", "basic", "full"],
                        default="auto",
                        help="AFL++ strategy-profile orchestration. auto=full for "
                             "--hybrid-adaptive, basic for --hybrid/--afl-only, off "
                             "otherwise. basic builds/uses default+LAF+CTX; full also "
                             "adds Ngram and LAF+CTX variants plus CmpLog companions.")
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
    parser.add_argument("--directed-sites", default=None,
                        help="Comma-separated QSYM telemetry branch site ids to prioritize "
                             "in adaptive hybrid runs. Also accepted through "
                             "SYMCC_DIRECTED_SITES.")
    parser.add_argument("--directed-distance", default=None,
                        help="Path to a directed hybrid site-distance map. Accepts JSON "
                             "or text lines '<site> <distance>' and is forwarded as "
                             "SYMCC_DIRECTED_DISTANCE.")
    parser.add_argument("--directed-targets", default=None,
                        help="Compile-time ColorGo-style target specs for built-in "
                             "SymCC targets. Values are comma-separated function names, "
                             "file:line locations, or numeric site ids. The compiler "
                             "emits per-target *.directed_distance maps.")
    parser.add_argument(
        "--structural-tasks", action=argparse.BooleanOptionalAction,
        default=None,
        help="Compile and use DynamiQ-style call-graph structural tasks. "
             "Defaults to enabled for --hybrid-adaptive and disabled otherwise.")
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
    if args.directed_sites:
        os.environ["SYMCC_DIRECTED_SITES"] = args.directed_sites
    if args.directed_distance:
        os.environ["SYMCC_DIRECTED_DISTANCE"] = args.directed_distance
    structural_tasks_enabled = (
        args.hybrid_adaptive
        if args.structural_tasks is None else args.structural_tasks)

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
    afl_profile_mode = resolve_afl_profile_mode(
        args.aflpp_profiles, args.hybrid, args.hybrid_adaptive, args.afl_only)

    output_dir = os.path.abspath(args.output)
    bin_dir = (
        os.path.abspath(args.bin_dir)
        if args.bin_dir else os.path.join(output_dir, "bin")
    )
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 70)
    print("  SymCC MPI Parallelization Benchmark")
    print("=" * 70)
    print(f"  Targets:     {', '.join(target_names)}")
    print(f"  NP values:   {np_list}")
    print(f"  Rounds:      {args.rounds}")
    print(f"  Timeout:     {args.timeout}s per run")
    print(f"  Coverage:    {'enabled' if not args.no_coverage else 'disabled'}")
    print(f"  AFL++ prof:  {afl_profile_mode}")
    if args.directed_targets:
        print(f"  Directed:    compile-time targets={args.directed_targets}")
    elif args.directed_distance:
        print(f"  Directed:    distance map={args.directed_distance}")
    print(f"  Struct tasks:{' enabled' if structural_tasks_enabled else ' disabled'}")
    print(f"  Output:      {output_dir}")
    print()

    # Build step
    binaries = {}
    micro_afl = {}   # 微目标的 AFL 二进制(afl-clang-fast),供 hybrid 的 AFL 侧 + 覆盖测量
    micro_afl_variants: dict[str, dict[str, str]] = defaultdict(dict)
    micro_cmplog: dict[str, str] = {}
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
            binaries = build_targets(
                bin_dir, _engine, args.directed_targets,
                structural_tasks_enabled)   # 按 --engine 选 symcc/ko-clang
            if not binaries and _engine.name == "symcc":
                print("  SymCC not found, falling back to gcc (simulation mode)")
                print("  NOTE: simulation mode tests the MPI framework overhead,")
                print("        not actual symbolic execution performance.")
                binaries = build_targets_gcc(bin_dir)
                args.simulation = True
            if not args.simulation:
                micro_afl, micro_afl_variants, micro_cmplog = build_afl_targets(
                    bin_dir, afl_profile_mode)  # 微目标 AFL 二进制(hybrid 需要)
    else:
        # Find existing binaries。【只认当前引擎的后缀】:此前会退回 _symcc/_native,
        # 于是 --engine symsan --skip-build 时捡起 SymCC 二进制交给 fgtest 跑,
        # fgtest 找不到 DFSan 回调 → 全程 0 输出,却看起来像"SymSan 效果差"。
        # 宁可缺目标(下面明确提示)也不要跑错引擎的二进制。
        _engine_sfx = (
            "_native" if args.simulation else get_engine().binary_suffix
        )
        for name in target_names:
            path = os.path.join(bin_dir, f"{name}{_engine_sfx}")
            if os.path.isfile(path):
                binaries[name] = path
            afl_p = os.path.join(bin_dir, f"{name}_afl")
            if os.path.isfile(afl_p):
                micro_afl[name] = afl_p
                micro_afl_variants[name]["default"] = afl_p
            for variant in AFL_BUILD_VARIANTS:
                vp = os.path.join(bin_dir, f"{name}{variant.suffix}")
                if os.path.isfile(vp):
                    micro_afl_variants[name][variant.name] = vp
            cp = os.path.join(bin_dir, f"{name}_afl_cmplog")
            if os.path.isfile(cp):
                micro_cmplog[name] = cp
        if not binaries and target_names:
            print(f"  WARNING: --skip-build 下没找到任何 *{_engine_sfx} 二进制"
                  f"(引擎 ={get_engine().name});请先构建或换 --engine")

    # Add public benchmark targets.
    # Auto-discover from benchmark/public/bin/ unless --no-public is passed.
    # Explicit --public specs override auto-discovery.
    public_targets = {}
    public_seed_dirs = {}
    target_extra_args: dict[str, list[str]] = {}  # 目标额外参数，如 base64 的 "-d"
    target_cmplog: dict[str, str] = {}  # 目标的 cmplog 二进制路径
    target_directed_distance: dict[str, str] = {}
    target_task_graph: dict[str, str] = {}
    target_afl_variants: dict[str, dict[str, str]] = {
        name: dict(paths) for name, paths in micro_afl_variants.items()
    }
    target_cmplog.update(micro_cmplog)
    for name in target_names:
        distance_path = os.path.join(bin_dir, f"{name}.directed_distance")
        if directed_distance_map_has_rows(distance_path):
            target_directed_distance[name] = distance_path
        task_graph_path = os.path.join(bin_dir, f"{name}.task_graph")
        if os.path.isfile(task_graph_path):
            target_task_graph[name] = task_graph_path
    if not args.no_public:
        public_afl_variants = discover_public_afl_variants()
        for name, paths in public_afl_variants.items():
            target_afl_variants.setdefault(name, {}).update(paths)
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
                    # 引擎感知发现:symsan 只挑 *_symsan 二进制并剥掉后缀作为逻辑名(→ 与
                    # symcc 目标同名、复用同一种子目录);其它引擎跳过 *_symsan(那是另一引擎的)。
                    _pub_engine = get_engine()
                    for binary in sorted(suite_dir.iterdir()):
                        if binary.is_file() and os.access(str(binary), os.X_OK):
                            bname = binary.name
                            # Skip non-ELF files (wrappers, data)
                            if bname.endswith((".sh", ".mgc", ".txt")):
                                continue
                            if _pub_engine.name == "symsan":
                                if not bname.endswith("_symsan"):
                                    continue  # 非 symsan 二进制,跳过
                                logical = bname[: -len("_symsan")]  # base64_harness_symsan -> base64_harness
                            else:
                                if bname.endswith("_symsan"):
                                    continue  # symsan 二进制不当作 symcc 目标
                                logical = bname
                            seed_candidate = pub_seed_dir / suite_dir.name / logical
                            if seed_candidate.is_dir():
                                if not args.simulation and not _has_symcc_instrumentation(str(binary), _pub_engine):
                                    print(f"  Skipping {bname}: 无 {_pub_engine.name} 插桩")
                                    continue
                                target_name = prefix + logical
                                public_specs.append(
                                    f"{target_name}:{binary}:{seed_candidate}"
                                )
                                # 读取 .args 文件（如有），如 base64.args 包含 "-d"（用逻辑名）
                                args_file = suite_dir / f"{logical}.args"
                                if args_file.is_file():
                                    extra = args_file.read_text().strip().split()
                                    if extra:
                                        target_extra_args[target_name] = extra
                                # 查找 cmplog 二进制（同名目录加 -cmplog 后缀，用逻辑名）
                                cmplog_dir = pub_bin_dir / (suite_dir.name + "-cmplog")
                                cmplog_bin = cmplog_dir / logical
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
    if not available_targets:
        requested = ", ".join(target_names) or "<none>"
        print(f"\nERROR: None of the requested targets are available: {requested}")
        sys.exit(2)

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
            # 微目标的 AFL 二进制(build_afl_targets / --skip-build 时扫到的 *_afl)也是
            # 合法的覆盖测量对象。此前没并进来,于是微目标 hybrid 的 edge= 恒为 0——
            # 看起来像"跑了但没覆盖",实际是压根没测。public 目标不受影响(走 discover)。
            for _name, _bin in micro_afl.items():
                afl_cov_binaries.setdefault(_name, _bin)
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

    if target_directed_distance:
        print(f"\n  Directed distance maps ({len(target_directed_distance)}):")
        for name in sorted(target_directed_distance):
            print(f"    {name}: {target_directed_distance[name]}")
    if target_task_graph:
        print(f"\n  Structural task graphs ({len(target_task_graph)}):")
        for name in sorted(target_task_graph):
            print(f"    {name}: {target_task_graph[name]}")

    profiled_targets = {
        name: paths for name, paths in target_afl_variants.items()
        if len(paths) > 1 or "default" in paths
    }
    if profiled_targets and afl_profile_mode != "off":
        print(f"\n  AFL++ profile variants ({len(profiled_targets)} targets):")
        for name in sorted(profiled_targets):
            print(f"    {name}: {', '.join(sorted(profiled_targets[name]))}")

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
                "generated": count_output_files(seed_dir),
                "generated_kind": "seed-files",
                "unique": len(get_unique_hashes(seed_dir)),
                "throughput": 0,
                "throughput_kind": "not-applicable",
                "edge_cov": seed_cov_data.get("edge_cov", 0.0),
                "edges_found": seed_cov_data.get("edges_found", 0),
                "edges_total": seed_cov_data.get("edges_total", 0),
                "crashes": 0,
                "status": "success",
                "failure_reason": "",
                "retcode": 0,
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
                _serial_kwargs = {
                    "binary": binary,
                    "target_name": target,
                    "seed_dir": seed_dir,
                    "timeout": args.timeout,
                    "work_dir": work_dir,
                    "simulate": args.simulation,
                    "extra_args": target_extra_args.get(target),
                }
                if args.timeseries > 0 and target in afl_cov_binaries:
                    uses_file = (
                        TARGETS[target][3] if target in TARGETS else True)
                    result, _ts = run_with_timeseries(
                        run_serial,
                        _serial_kwargs,
                        afl_cov_binaries[target],
                        interval=args.timeseries,
                        uses_file=uses_file,
                        timeout=args.timeout,
                        corpus_source=lambda: [
                            seed_dir,
                            os.path.join(work_dir, "serial_output"),
                        ],
                        extra_args=target_extra_args.get(target),
                    )
                    if _ts:
                        all_timeseries.append({
                            "target": target, "mode": "serial",
                            "np": 1, "round": r + 1,
                            "timeseries": _ts,
                        })
                else:
                    result = run_serial(**_serial_kwargs)

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
                    "generated_kind": result.get(
                        "generated_kind", "symcc-candidates"),
                    "symcc_generated": result.get(
                        "symcc_generated", result["generated"]),
                    "unique": result["unique"],
                    "throughput": result.get("throughput", 0),
                    "throughput_kind": result.get(
                        "throughput_kind", "symcc-candidates-per-second"),
                    "edge_cov": cov_data.get("edge_cov", 0.0),
                    "edges_found": cov_data.get("edges_found", 0),
                    "edges_total": cov_data.get("edges_total", 0),
                    "crashes": cov_data.get("crashes", 0),
                    **coverage_measurement_metadata(cov_data),
                    **benchmark_outcome(result),
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
            # Keep the reporting/accounting layout aligned with
            # mpi_concolic_execution.py's public CLI default.  A stale value
            # here does not change dispatch, but it mislabels large campaigns
            # and therefore corrupts per-worker scaling metrics.
            wpm = 90  # workers_per_master default
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
                        corpus_source=lambda: [
                            seed_dir,
                            os.path.join(
                                work_dir, f"mpi_np{actual_np}_output"),
                        ],
                        extra_args=target_extra_args.get(target),
                    )
                    if _ts:
                        all_timeseries.append({
                            "target": target, "mode": "mpi",
                            "np": actual_np, "round": r + 1,
                            "timeseries": _ts,
                        })
                else:
                    result = run_mpi(**_mpi_kwargs)

                # The temporary work directory is removed below. Preserve the
                # complete MPI transcript first so an R-grade campaign retains
                # enough evidence to audit failed rounds and parsed metrics.
                raw_log = result.get("log_path")
                if isinstance(raw_log, str) and os.path.isfile(raw_log):
                    log_dir = os.path.join(output_dir, "raw-logs")
                    os.makedirs(log_dir, exist_ok=True)
                    safe_target = re.sub(r"[^A-Za-z0-9_.-]+", "_", target)
                    shutil.copy2(
                        raw_log,
                        os.path.join(
                            log_dir,
                            f"{safe_target}-mpi-np{actual_np}-r{r + 1}.log",
                        ),
                    )

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
                    "generated_kind": result.get(
                        "generated_kind", "symcc-candidates"),
                    "symcc_generated": result.get(
                        "symcc_generated", result["generated"]),
                    "unique": result["unique"],
                    "throughput": result.get("throughput", 0),
                    "throughput_kind": result.get(
                        "throughput_kind", "symcc-candidates-per-second"),
                    "edge_cov": cov_data.get("edge_cov", 0.0),
                    "edges_found": cov_data.get("edges_found", 0),
                    "edges_total": cov_data.get("edges_total", 0),
                    "crashes": cov_data.get("crashes", 0),
                    **coverage_measurement_metadata(cov_data),
                    "speedup": speedup,
                    "efficiency": efficiency,
                    "num_workers": result.get("num_workers") or (actual_np - 1),
                    "num_masters": result.get("num_masters") or pred_masters,
                    "auxiliary_compute_slots": result.get(
                        "auxiliary_compute_slots", 0
                    ),
                    **benchmark_outcome(result),
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
                        _hybrid_kwargs = {
                            "symcc_binary": binary,
                            "afl_binary": afl_binary,
                            "target_name": target,
                            "seed_dir": seed_dir,
                            "np": actual_np,
                            "timeout": args.timeout,
                            "work_dir": work_dir,
                            "extra_args": target_extra_args.get(target),
                            "cmplog_binary": target_cmplog.get(target),
                            "afl_variants": target_afl_variants.get(target),
                            "afl_profile_mode": afl_profile_mode,
                            "directed_distance": (
                                target_directed_distance.get(target)
                                or args.directed_distance
                            ),
                            "task_graph": target_task_graph.get(target),
                            "afl_instances": afl_inst,
                            "adaptive": args.hybrid_adaptive,
                            "grimoire": args.hybrid_grimoire,
                            "honggfuzz_binary": (
                                discover_public_hfuzz_targets().get(target)
                                if args.hybrid_honggfuzz else None
                            ),
                            "symcc_diversity": args.symcc_diversity,
                            "symcc_density_balance": (
                                args.symcc_density_balance),
                        }
                        if (
                            args.timeseries > 0
                            and target in afl_cov_binaries
                        ):
                            result, _ts = run_with_timeseries(
                                run_hybrid,
                                _hybrid_kwargs,
                                afl_cov_binaries[target],
                                interval=args.timeseries,
                                uses_file=True,
                                timeout=args.timeout,
                                corpus_source=lambda: _hybrid_live_corpora(
                                    seed_dir, work_dir),
                                extra_args=target_extra_args.get(target),
                            )
                            if _ts:
                                all_timeseries.append({
                                    "target": target, "mode": "hybrid",
                                    "np": actual_np, "round": r + 1,
                                    "timeseries": _ts,
                                })
                        else:
                            result = run_hybrid(**_hybrid_kwargs)

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
                              f"peer_sync="
                              f"{result.get('symcc_peer_scanned', 0)}/"
                              f"{result.get('symcc_peer_published', 0)}, "
                              f"peer_imported="
                              f"{result.get('symcc_peer_imported', 0)}, "
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
                            "generated_kind": result.get(
                                "generated_kind", "unspecified"),
                            "afl_generated": result.get("afl_generated", 0),
                            "afl_executions": result.get(
                                "afl_executions",
                                result.get("afl_execs_done", 0)),
                            "symcc_generated": result.get(
                                "symcc_generated", 0),
                            "symcc_interesting": result.get(
                                "symcc_interesting", 0),
                            "symcc_peer_published": result.get(
                                "symcc_peer_published", 0),
                            "symcc_peer_scanned": result.get(
                                "symcc_peer_scanned", 0),
                            "symcc_peer_imported": result.get(
                                "symcc_peer_imported", 0),
                            "symcc_peer_not_retained": result.get(
                                "symcc_peer_not_retained", 0),
                            "symcc_peer_sync_complete": result.get(
                                "symcc_peer_sync_complete", 0),
                            "symcc_peer_pre_stop_complete": result.get(
                                "symcc_peer_pre_stop_complete", 0),
                            "symcc_peer_cursor_before_final_sync": result.get(
                                "symcc_peer_cursor_before_final_sync", 0),
                            "symcc_peer_frozen_published": result.get(
                                "symcc_peer_frozen_published", 0),
                            "symcc_sync_drain_seconds": result.get(
                                "symcc_sync_drain_seconds", 0.0),
                            "cleanup_time": result.get("cleanup_time", 0.0),
                            "afl_master_corpus_imported": result.get(
                                "afl_master_corpus_imported", 0),
                            "afl_corpus_imported": result.get(
                                "afl_corpus_imported", 0),
                            "afl_master_sync_time": result.get(
                                "afl_master_sync_time", 0),
                            "afl_sync_time_minutes": result.get(
                                "afl_sync_time_minutes", 0),
                            "unique": result["unique"],
                            "throughput": result.get("throughput", 0),
                            "throughput_kind": result.get(
                                "throughput_kind", "unspecified"),
                            "afl_retained_files": result.get(
                                "afl_retained_files", 0),
                            "afl_retained_unique": result.get(
                                "afl_retained_unique", 0),
                            "symcc_output_files": result.get(
                                "symcc_output_files", 0),
                            "afl_instances": result.get(
                                "afl_instances", afl_inst),
                            "edge_cov": cov_data.get("edge_cov", 0.0),
                            "edges_found": cov_data.get("edges_found", 0),
                            "edges_total": cov_data.get("edges_total", 0),
                            "crashes": cov_data.get("crashes", 0),
                            **coverage_measurement_metadata(cov_data),
                            "speedup": 0,
                            "efficiency": 0,
                            "num_masters": result.get("num_masters", 1),
                            "num_workers": result.get("num_workers", actual_np - 2),
                            "auxiliary_compute_slots": result.get(
                                "auxiliary_compute_slots"
                            ),
                            "afl_bitmap_cvg": result.get("afl_bitmap_cvg", ""),
                            "afl_edges_found": result.get("afl_edges_found", 0),
                            "afl_total_edges": result.get("afl_total_edges", 0),
                            "afl_execs_done": result.get("afl_execs_done", 0),
                            "afl_execs_per_sec": result.get("afl_execs_per_sec", 0),
                            "afl_profiles": result.get("afl_profiles", ""),
                            "afl_variants": result.get("afl_variants", ""),
                            "offline_events": result.get("offline_events", 0),
                            "offline_approved_action": result.get(
                                "offline_approved_action", ""),
                            "offline_evaluations": result.get(
                                "offline_evaluations", 0),
                            "offline_trajectory": result.get(
                                "offline_trajectory", ""),
                            "offline_policy": result.get("offline_policy", ""),
                            "smt_algorithm_observations": result.get(
                                "smt_algorithm_observations", 0),
                            "smt_algorithm_clusters": result.get(
                                "smt_algorithm_clusters", 0),
                            "smt_algorithm_state": result.get(
                                "smt_algorithm_state", ""),
                            **benchmark_outcome(result),
                        })

                        shutil.rmtree(work_dir, ignore_errors=True)
            else:
                print(f"\n  [Hybrid] No AFL binary found for {target}, skipping")

        # AFL-only baseline
        if args.afl_only and target in public_targets:
            afl_targets = discover_public_afl_targets()
            afl_binary = afl_targets.get(target)
            if afl_binary:
                for np_val in np_list:
                    actual_np = max(1, np_val)
                    print(f"\n  [AFL-only equal-core baseline np={actual_np}]")

                    for r in range(args.rounds):
                        current_run += 1
                        work_dir = tempfile.mkdtemp(
                            prefix=(
                                f"bench_{target}_aflonly{actual_np}_r{r}_"))

                        print(f"    Round {r+1}/{args.rounds}... ",
                              end="", flush=True)
                        _afl_kwargs = {
                            "afl_binary": afl_binary,
                            "target_name": target,
                            "seed_dir": seed_dir,
                            "timeout": args.timeout,
                            "work_dir": work_dir,
                            "extra_args": target_extra_args.get(target),
                            "cmplog_binary": target_cmplog.get(target),
                            "instances": actual_np,
                            "afl_variants": target_afl_variants.get(target),
                            "afl_profile_mode": afl_profile_mode,
                        }
                        if args.timeseries > 0 and target in afl_cov_binaries:
                            result, _ts = run_with_timeseries(
                                run_afl_only,
                                _afl_kwargs,
                                afl_cov_binaries[target],
                                interval=args.timeseries,
                                uses_file=True,
                                timeout=args.timeout,
                                corpus_source=lambda n=actual_np, wd=work_dir: [
                                    seed_dir,
                                    *[
                                        os.path.join(
                                            wd, "afl_out", f"fuzzer{i:02d}",
                                            "queue")
                                        for i in range(1, n + 1)
                                    ],
                                ],
                                extra_args=target_extra_args.get(target),
                            )
                            if _ts:
                                all_timeseries.append({
                                    "target": target, "mode": "afl-only",
                                    "np": actual_np, "round": r + 1,
                                    "timeseries": _ts,
                                })
                        else:
                            result = run_afl_only(**_afl_kwargs)

                        cov_data = {
                            "edge_cov": 0.0, "edges_found": 0,
                            "edges_total": 0, "crashes": 0,
                        }
                        if enable_coverage and target in afl_cov_binaries:
                            cov_data = measure_coverage_afl(
                                afl_cov_binaries[target], result["output_dir"],
                                uses_file=True,
                                extra_args=target_extra_args.get(target),
                            )

                        cov_str = ""
                        if enable_coverage and target in afl_cov_binaries:
                            cov_str = (
                                f", edge={cov_data['edge_cov']:.2f}% "
                                f"({cov_data['edges_found']}/"
                                f"{cov_data['edges_total']}), "
                                f"crashes={cov_data['crashes']}")

                        afl_execs = result.get("afl_execs_done", 0)
                        afl_eps = result.get("afl_execs_per_sec", 0)
                        bitmap_cvg = result.get("afl_bitmap_cvg", "")
                        timeout_str = (
                            " [TIMEOUT]" if result.get("timed_out") else "")
                        print(
                            f"time={format_time(result['wall_time'])}, "
                            f"execs={afl_execs}, retained_unique="
                            f"{result['unique']}{cov_str}{timeout_str}"
                            f"{' bitmap=' + bitmap_cvg if bitmap_cvg else ''}"
                            f"{f' ({afl_eps:.0f} exec/s)' if afl_eps else ''}")

                        all_results.append({
                            "target": target,
                            "mode": "afl-only",
                            "np": actual_np,
                            "round": r + 1,
                            "wall_time": result["wall_time"],
                            "generated": result["generated"],
                            "generated_kind": result.get(
                                "generated_kind", "afl-executions"),
                            "afl_generated": result.get("afl_generated", 0),
                            "afl_executions": result.get(
                                "afl_executions",
                                result.get("afl_execs_done", 0)),
                            "symcc_generated": 0,
                            "symcc_interesting": 0,
                            "unique": result["unique"],
                            "throughput": result.get("throughput", 0),
                            "throughput_kind": result.get(
                                "throughput_kind", "unspecified"),
                            "afl_retained_files": result.get(
                                "afl_retained_files", 0),
                            "afl_retained_unique": result.get(
                                "afl_retained_unique", 0),
                            "afl_instances": result.get(
                                "afl_instances", actual_np),
                            "edge_cov": cov_data.get("edge_cov", 0.0),
                            "edges_found": cov_data.get("edges_found", 0),
                            "edges_total": cov_data.get("edges_total", 0),
                            "crashes": cov_data.get("crashes", 0),
                            **coverage_measurement_metadata(cov_data),
                            "speedup": 0,
                            "efficiency": 0,
                            "afl_execs_done": afl_execs,
                            "afl_execs_per_sec": afl_eps,
                            "afl_bitmap_cvg": result.get(
                                "afl_bitmap_cvg", ""),
                            "afl_edges_found": result.get(
                                "afl_edges_found", 0),
                            "afl_total_edges": result.get(
                                "afl_total_edges", 0),
                            "afl_profiles": result.get("afl_profiles", ""),
                            "afl_variants": result.get("afl_variants", ""),
                            **benchmark_outcome(result),
                        })

                        shutil.rmtree(work_dir, ignore_errors=True)
            else:
                print(f"\n  [AFL-only] No AFL binary found for {target}, skipping")

    # Generate report
    print("\n\nStep 3: Generating report")
    print("-" * 40)
    annotate_research_results(all_results, all_timeseries)
    report_path = generate_report(all_results, output_dir)

    # Print the report to stdout
    with open(report_path) as f:
        print(f.read())

    # 保存时间序列数据（如果有）
    if all_timeseries:
        ts_path = os.path.join(output_dir, "coverage_timeseries.csv")
        with open(ts_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["experiment_id", "run_id", "pair_id",
                             "target", "configuration", "mode", "np", "round",
                             "timestamp_sec", "edge_cov_pct",
                             "edges_found", "edges_total", "total_cases"])
            for entry in all_timeseries:
                for point in entry["timeseries"]:
                    writer.writerow([
                        entry.get("experiment_id", ""),
                        entry.get("run_id", ""),
                        entry.get("pair_id", ""),
                        entry["target"],
                        entry.get("configuration", entry["mode"]),
                        entry["mode"], entry["np"],
                        entry["round"],
                        point["timestamp_sec"], point["edge_cov"],
                        point["edges_found"], point["edges_total"],
                        point["total_cases"],
                    ])
        print(f"\n  Time-series data saved to: {ts_path}")

    exit_code = benchmark_matrix_exit_code(all_results)
    if exit_code:
        failed = sum(
            row.get("mode") != "seed" and row.get("status") != "success"
            for row in all_results
        )
        print(f"\nBenchmark completed with {failed} failed measured row(s).")
    print(f"\nBenchmark complete. Results in: {output_dir}/")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
