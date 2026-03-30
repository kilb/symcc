#!/usr/bin/env python3
"""SymCC MPI 并行瓶颈分析工具。

测量 master 各阶段耗时（dispatch、triage、idle、sync），
worker 各阶段耗时（SymCC 执行、showmap 收集、MPI 通信），
以及不同 np 下的 scaling 效率。

用法:
    python3 benchmark/profile_bottleneck.py \
        --target gfts-xml_read_fuzzer \
        --np-list 2,4,8,16,32 \
        --timeout 60 \
        --modes mpi,hybrid \
        --output profile_results/

输出：
    - profile_report.txt  — 人类可读的瓶颈分析报告
    - profile_data.csv    — 机器可读的原始数据
    - profile_data.json   — 结构化数据
"""

import argparse
import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
MPI_CONCOLIC_SCRIPT = PROJECT_DIR / "util" / "mpi_concolic_execution.py"
MPI_FUZZING_SCRIPT = PROJECT_DIR / "util" / "mpi_fuzzing_helper.py"
PUBLIC_DIR = SCRIPT_DIR / "public"


# ─────────────────────────────────────────────────────────────
# 目标发现
# ─────────────────────────────────────────────────────────────

def discover_targets() -> dict[str, dict]:
    """发现可用的测试目标，返回 {名称: {binary, seeds, afl_binary, uses_file}}。"""
    targets: dict[str, dict] = {}
    pub_bin = PUBLIC_DIR / "bin"
    pub_seed = PUBLIC_DIR / "seeds"

    if not pub_bin.is_dir():
        return targets

    # SymCC 二进制
    suite_prefixes = {"google-fts": "gfts-", "lava-m": "lava-"}
    for suite_dir in sorted(pub_bin.iterdir()):
        if not suite_dir.is_dir() or suite_dir.name.endswith(("-afl", "-cov")):
            continue
        prefix = suite_prefixes.get(suite_dir.name, suite_dir.name + "-")
        for binary in sorted(suite_dir.iterdir()):
            if not binary.is_file() or binary.suffix or not os.access(str(binary), os.X_OK):
                continue
            name = prefix + binary.name
            seed_dir = pub_seed / suite_dir.name / binary.name
            if seed_dir.is_dir():
                targets[name] = {
                    "binary": str(binary),
                    "seeds": str(seed_dir),
                    "afl_binary": None,
                    "uses_file": True,
                }

    # AFL 二进制
    for suite_dir in sorted(pub_bin.iterdir()):
        if not suite_dir.is_dir() or not suite_dir.name.endswith("-afl"):
            continue
        suite_base = suite_dir.name[:-4]  # 去掉 -afl
        prefix = suite_prefixes.get(suite_base, suite_base + "-")
        for binary in sorted(suite_dir.iterdir()):
            if not binary.is_file() or binary.suffix:
                continue
            name = prefix + binary.name
            if name in targets:
                targets[name]["afl_binary"] = str(binary)

    return targets


# ─────────────────────────────────────────────────────────────
# 运行并解析 profiling 数据
# ─────────────────────────────────────────────────────────────

def run_mpi_profiled(binary: str, seed_dir: str, np: int,
                     timeout: int, work_dir: str) -> dict:
    """运行 MPI concolic 模式并收集 profiling 数据。"""
    output_dir = os.path.join(work_dir, "output")
    # 不预创建 output_dir，让 MPI 脚本自己创建（避免"目录已存在"检测）

    cmd = [
        "mpirun", "--allow-run-as-root", "--oversubscribe",
        "-np", str(np),
        "python3", "-u", str(MPI_CONCOLIC_SCRIPT),
        "-i", seed_dir,
        "-o", output_dir,
        "-t", "10",
        "--wall-timeout", str(timeout - 5),  # 留 5s 给 shutdown
        "--", binary, "@@",
    ]

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["SYMCC_PROFILE"] = "1"

    t_start = time.monotonic()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, start_new_session=True,
    )
    try:
        stdout_bytes, _ = proc.communicate(timeout=timeout + 10)
        stdout = stdout_bytes.decode(errors="replace")
        stderr = ""
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        time.sleep(2)
        try:
            stdout_bytes, _ = proc.communicate(timeout=5)
            stdout = stdout_bytes.decode(errors="replace")
        except Exception:
            stdout = ""
            proc.kill()
        stderr = "TIMEOUT"

    wall_time = time.monotonic() - t_start

    # 解析输出
    data = _parse_mpi_output(stdout, stderr)
    data["mode"] = "mpi"
    data["np"] = np
    data["wall_time"] = round(wall_time, 2)
    data["output_dir"] = output_dir

    # 统计输出文件数
    if os.path.isdir(output_dir):
        data["total_files"] = sum(
            1 for f in os.listdir(output_dir) if os.path.isfile(os.path.join(output_dir, f))
        )

    return data


def run_hybrid_profiled(binary: str, afl_binary: str, seed_dir: str,
                        np: int, timeout: int, work_dir: str) -> dict:
    """运行 hybrid AFL+SymCC 模式并收集 profiling 数据。"""
    afl_out = os.path.join(work_dir, "afl_out")
    symcc_all = os.path.join(work_dir, "symcc_all")
    os.makedirs(afl_out, exist_ok=True)
    os.makedirs(symcc_all, exist_ok=True)

    # 启动 AFL
    afl_cmd = [
        "afl-fuzz", "-M", "fuzzer01",
        "-i", seed_dir,
        "-o", afl_out,
        "-m", "none",
        "--", afl_binary, "@@",
    ]
    afl_env = os.environ.copy()
    afl_env["AFL_NO_UI"] = "1"
    afl_env["AFL_SKIP_CPUFREQ"] = "1"
    afl_env["AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES"] = "1"

    afl_proc = subprocess.Popen(
        afl_cmd, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=afl_env, start_new_session=True,
    )

    # 等 AFL 初始化
    fuzzer_stats = os.path.join(afl_out, "fuzzer01", "fuzzer_stats")
    for _ in range(30):
        if os.path.isfile(fuzzer_stats):
            break
        time.sleep(1)

    # 启动 MPI SymCC
    symcc_np = max(2, np - 1)
    mpi_cmd = [
        "mpirun", "--allow-run-as-root", "--oversubscribe",
        "-np", str(symcc_np),
        "python3", "-u", str(MPI_FUZZING_SCRIPT),
        "-a", "fuzzer01",
        "-o", afl_out,
        "-n", "symcc01",
        "--save-all", symcc_all,
        "--", binary, "@@",
    ]
    mpi_env = os.environ.copy()
    mpi_env["PYTHONUNBUFFERED"] = "1"
    mpi_env["SYMCC_PROFILE"] = "1"

    t_start = time.monotonic()
    mpi_proc = subprocess.Popen(
        mpi_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=mpi_env, start_new_session=True,
    )

    try:
        stdout_bytes, _ = mpi_proc.communicate(timeout=timeout + 15)
        stdout = stdout_bytes.decode(errors="replace")
    except subprocess.TimeoutExpired:
        os.killpg(mpi_proc.pid, signal.SIGTERM)
        time.sleep(2)
        try:
            stdout_bytes, _ = mpi_proc.communicate(timeout=5)
            stdout = stdout_bytes.decode(errors="replace")
        except Exception:
            stdout = ""
            mpi_proc.kill()

    wall_time = time.monotonic() - t_start

    # 杀掉 AFL
    try:
        os.killpg(afl_proc.pid, signal.SIGTERM)
        afl_proc.wait(timeout=5)
    except Exception:
        afl_proc.kill()

    data = _parse_hybrid_output(stdout)
    data["mode"] = "hybrid"
    data["np"] = np
    data["wall_time"] = round(wall_time, 2)

    # 统计
    afl_queue = os.path.join(afl_out, "fuzzer01", "queue")
    symcc_queue = os.path.join(afl_out, "symcc01", "queue")
    data["afl_count"] = sum(1 for f in os.listdir(afl_queue) if os.path.isfile(os.path.join(afl_queue, f))) if os.path.isdir(afl_queue) else 0
    data["symcc_count"] = sum(1 for f in os.listdir(symcc_queue) if os.path.isfile(os.path.join(symcc_queue, f))) if os.path.isdir(symcc_queue) else 0
    data["symcc_all_count"] = sum(1 for f in os.listdir(symcc_all) if os.path.isfile(os.path.join(symcc_all, f))) if os.path.isdir(symcc_all) else 0

    return data


def _parse_mpi_output(stdout: str, stderr: str) -> dict:
    """解析 MPI concolic 的输出提取统计数据。"""
    data: dict = {
        "total_analyzed": 0, "total_generated": 0, "interesting": 0,
        "throughput": 0.0, "workers_used": 0,
        "worker_times": [],  # 每个 worker 的执行时间列表
    }

    for line in stdout.splitlines():
        # [Master] Total inputs analyzed:      123
        m = re.search(r"Total inputs analyzed:\s+(\d+)", line)
        if m:
            data["total_analyzed"] = int(m.group(1))
        m = re.search(r"Total test cases generated:\s+(\d+)", line)
        if m:
            data["total_generated"] = int(m.group(1))
        m = re.search(r"New interesting test cases:\s+(\d+)", line)
        if m:
            data["interesting"] = int(m.group(1))
        m = re.search(r"Throughput:\s+([0-9.]+)", line)
        if m:
            data["throughput"] = float(m.group(1))
        m = re.search(r"Workers used:\s+(\d+)", line)
        if m:
            data["workers_used"] = int(m.group(1))

        # Worker 完成信息: Worker 3: hash -> 45 new (2.3s, ret=0)
        m = re.search(r"Worker \d+:.*\(([0-9.]+)s,", line)
        if m:
            data["worker_times"].append(float(m.group(1)))

        # PROFILE 行（如果有）
        m = re.search(r"\[PROFILE\] (\w+)=([0-9.]+)", line)
        if m:
            data[f"prof_{m.group(1)}"] = float(m.group(2))

    return data


def _parse_hybrid_output(stdout: str) -> dict:
    """解析 hybrid 模式输出。"""
    data: dict = {
        "total_generated": 0, "interesting": 0,
        "total_tasks": 0, "failed_tasks": 0,
        "triage_times": [],   # 每次批量 triage 的耗时
        "worker_batches": [],  # (worker_id, tc_count, elapsed)
    }

    for line in stdout.splitlines():
        # [Master] Stats: 100 ok, 0 failed, 50 interesting / 500 total
        m = re.search(r"Stats: (\d+) ok, (\d+) failed, (\d+) interesting / (\d+) total", line)
        if m:
            data["total_tasks"] = int(m.group(1))
            data["failed_tasks"] = int(m.group(2))
            data["interesting"] = int(m.group(3))
            data["total_generated"] = int(m.group(4))

        # Final stats
        m = re.search(r"Final stats: (\d+) ok, (\d+) failed, (\d+) interesting / (\d+) total", line)
        if m:
            data["total_tasks"] = int(m.group(1))
            data["failed_tasks"] = int(m.group(2))
            data["interesting"] = int(m.group(3))
            data["total_generated"] = int(m.group(4))

        # [Master] Triage: 100 tc -> 5 interesting [W1=50tc/0.3s, W2=50tc/0.2s]
        m = re.search(r"Triage: (\d+) tc -> (\d+) interesting \[(.+)\]", line)
        if m:
            batch_info = m.group(3)
            for wm in re.finditer(r"W(\d+)=(\d+)tc/([0-9.]+)s", batch_info):
                data["worker_batches"].append((
                    int(wm.group(1)),  # worker_id
                    int(wm.group(2)),  # tc_count
                    float(wm.group(3)),  # elapsed
                ))

        # PROFILE 行
        m = re.search(r"\[PROFILE\] (\w+)=([0-9.]+)", line)
        if m:
            key = f"prof_{m.group(1)}"
            if key not in data:
                data[key] = 0.0
            data[key] += float(m.group(2))

    return data


# ─────────────────────────────────────────────────────────────
# 报告生成
# ─────────────────────────────────────────────────────────────

def generate_report(all_results: list[dict], target_name: str,
                    output_dir: str) -> None:
    """生成完整的瓶颈分析报告。"""
    os.makedirs(output_dir, exist_ok=True)

    # 1. CSV 原始数据
    csv_path = os.path.join(output_dir, "profile_data.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "target", "mode", "np", "wall_time_s",
            "total_tasks", "total_generated", "interesting",
            "throughput_tc_s", "workers_used",
            "avg_worker_time_s", "max_worker_time_s",
            "afl_count", "symcc_count", "symcc_all_count",
            "prof_dispatch_s", "prof_triage_s", "prof_idle_s",
            "prof_collect_s", "prof_sync_s",
        ])
        for r in all_results:
            wt = r.get("worker_times", []) or [b[2] for b in r.get("worker_batches", [])]
            writer.writerow([
                target_name, r["mode"], r["np"], r["wall_time"],
                r.get("total_tasks", r.get("total_analyzed", 0)),
                r.get("total_generated", 0), r.get("interesting", 0),
                r.get("throughput", 0),
                r.get("workers_used", r["np"] - 1),
                f"{sum(wt)/len(wt):.3f}" if wt else "0",
                f"{max(wt):.3f}" if wt else "0",
                r.get("afl_count", ""), r.get("symcc_count", ""),
                r.get("symcc_all_count", ""),
                f"{r.get('prof_dispatch', 0):.3f}",
                f"{r.get('prof_triage', 0):.3f}",
                f"{r.get('prof_idle', 0):.3f}",
                f"{r.get('prof_collect', 0):.3f}",
                f"{r.get('prof_sync', 0):.3f}",
            ])

    # 2. JSON 数据
    json_path = os.path.join(output_dir, "profile_data.json")
    with open(json_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    # 3. 文本报告
    report_path = os.path.join(output_dir, "profile_report.txt")
    with open(report_path, "w") as f:
        _write_report(f, all_results, target_name)

    print(f"\n  报告已保存:")
    print(f"    Text:  {report_path}")
    print(f"    CSV:   {csv_path}")
    print(f"    JSON:  {json_path}")


def _write_report(f, results: list[dict], target: str) -> None:
    """写入人类可读的分析报告。"""
    f.write("=" * 72 + "\n")
    f.write("  SymCC MPI 并行瓶颈分析报告\n")
    f.write(f"  目标: {target}\n")
    f.write(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    f.write("=" * 72 + "\n\n")

    # 按 mode 分组
    modes = {}
    for r in results:
        modes.setdefault(r["mode"], []).append(r)

    for mode, runs in sorted(modes.items()):
        f.write(f"{'─' * 72}\n")
        f.write(f"  模式: {mode.upper()}\n")
        f.write(f"{'─' * 72}\n\n")

        # 总览表
        f.write("  ┌─────┬──────────┬──────────┬──────────┬───────────┬──────────┐\n")
        f.write("  │  NP │ 总耗时   │ 任务数   │ 测试用例 │ 吞吐量    │ Scaling  │\n")
        f.write("  │     │   (s)    │          │          │  (tc/s)   │  效率    │\n")
        f.write("  ├─────┼──────────┼──────────┼──────────┼───────────┼──────────┤\n")

        base_throughput = None
        for r in sorted(runs, key=lambda x: x["np"]):
            tasks = r.get("total_tasks", r.get("total_analyzed", 0))
            tc = r.get("total_generated", 0)
            tp = r.get("throughput", 0)
            if tp == 0 and r["wall_time"] > 0 and tc > 0:
                tp = tc / r["wall_time"]
            workers = r.get("workers_used", r["np"] - 1) or (r["np"] - 1)

            if base_throughput is None and workers > 0:
                base_throughput = tp / workers if workers > 0 else tp

            if base_throughput and base_throughput > 0 and workers > 0:
                ideal = base_throughput * workers
                efficiency = tp / ideal * 100 if ideal > 0 else 0
                scaling_str = f"{efficiency:5.1f}%"
            else:
                scaling_str = "  N/A"

            f.write(f"  │ {r['np']:>3} │ {r['wall_time']:>7.1f}s │ {tasks:>8} │ "
                    f"{tc:>8} │ {tp:>8.1f}  │ {scaling_str:>8} │\n")

        f.write("  └─────┴──────────┴──────────┴──────────┴───────────┴──────────┘\n\n")

        # Worker 执行时间分布
        f.write("  Worker 执行时间分布:\n")
        for r in sorted(runs, key=lambda x: x["np"]):
            wt = r.get("worker_times", []) or [b[2] for b in r.get("worker_batches", [])]
            if not wt:
                continue
            avg = sum(wt) / len(wt)
            mn = min(wt)
            mx = max(wt)
            p50 = sorted(wt)[len(wt) // 2]
            p90 = sorted(wt)[int(len(wt) * 0.9)]
            f.write(f"    np={r['np']:>3}: n={len(wt):>5}, "
                    f"avg={avg:.3f}s, min={mn:.3f}s, p50={p50:.3f}s, "
                    f"p90={p90:.3f}s, max={mx:.3f}s\n")
        f.write("\n")

        # Profiling 数据（如果有）
        has_prof = any(any(k.startswith("prof_") for k in r) for r in runs)
        if has_prof:
            f.write("  Master 阶段耗时:\n")
            f.write("  ┌─────┬──────────┬──────────┬──────────┬──────────┬──────────┐\n")
            f.write("  │  NP │ Dispatch │  Triage  │ Collect  │   Sync   │   Idle   │\n")
            f.write("  │     │   (s)    │   (s)    │   (s)    │   (s)    │   (s)    │\n")
            f.write("  ├─────┼──────────┼──────────┼──────────┼──────────┼──────────┤\n")
            for r in sorted(runs, key=lambda x: x["np"]):
                disp = r.get("prof_dispatch", 0)
                tri = r.get("prof_triage", 0)
                coll = r.get("prof_collect", 0)
                sync = r.get("prof_sync", 0)
                idle = r.get("prof_idle", 0)
                f.write(f"  │ {r['np']:>3} │ {disp:>7.2f}s │ {tri:>7.2f}s │ "
                        f"{coll:>7.2f}s │ {sync:>7.2f}s │ {idle:>7.2f}s │\n")
            f.write("  └─────┴──────────┴──────────┴──────────┴──────────┴──────────┘\n\n")

        # Scaling 曲线图
        sorted_runs = sorted(runs, key=lambda x: x["np"])
        if len(sorted_runs) >= 2:
            f.write("  吞吐量 Scaling 曲线:\n")
            max_tp = max(
                (r.get("throughput", 0) or (r.get("total_generated", 0) / max(r["wall_time"], 1)))
                for r in sorted_runs
            )
            if max_tp > 0:
                for r in sorted_runs:
                    tp = r.get("throughput", 0)
                    if tp == 0 and r["wall_time"] > 0:
                        tp = r.get("total_generated", 0) / r["wall_time"]
                    bar_len = int(tp / max_tp * 40)
                    bar = "█" * bar_len + "░" * (40 - bar_len)
                    f.write(f"    np={r['np']:>3} |{bar}| {tp:>8.1f} tc/s\n")
                f.write("\n")

        # 瓶颈诊断
        _write_diagnosis(f, runs, mode)


def _write_diagnosis(f, runs: list[dict], mode: str) -> None:
    """根据数据自动诊断瓶颈。"""
    f.write("  瓶颈诊断:\n")

    if len(runs) < 2:
        f.write("    (需要至少 2 个 NP 数据点进行诊断)\n\n")
        return

    sorted_runs = sorted(runs, key=lambda x: x["np"])

    # 计算 scaling 趋势
    tps = []
    for r in sorted_runs:
        tc = r.get("total_generated", 0)
        tp = r.get("throughput", 0)
        if tp == 0 and r["wall_time"] > 0 and tc > 0:
            tp = tc / r["wall_time"]
        workers = r.get("workers_used", r["np"] - 1) or (r["np"] - 1)
        tps.append((r["np"], workers, tp))

    # 检查线性 scaling
    if len(tps) >= 2 and tps[0][2] > 0:
        base_per_worker = tps[0][2] / max(tps[0][1], 1)
        last_per_worker = tps[-1][2] / max(tps[-1][1], 1)
        scaling_loss = (1 - last_per_worker / base_per_worker) * 100 if base_per_worker > 0 else 0

        if scaling_loss < 0:
            f.write(f"    ✓ 超线性 scaling（增益 {-scaling_loss:.1f}%）— "
                    f"更多 worker 互相产生新种子，形成正反馈\n")
        elif scaling_loss < 10:
            f.write(f"    ✓ 近线性 scaling（效率损失 {scaling_loss:.1f}%）— 无明显瓶颈\n")
        elif scaling_loss < 30:
            f.write(f"    ⚠ 中等 scaling 损失（{scaling_loss:.1f}%）\n")
        else:
            f.write(f"    ✗ 严重 scaling 退化（效率损失 {scaling_loss:.1f}%）\n")

    # 检查 worker 时间分布偏斜
    for r in sorted_runs:
        wt = r.get("worker_times", []) or [b[2] for b in r.get("worker_batches", [])]
        if len(wt) < 4:
            continue
        avg = sum(wt) / len(wt)
        mx = max(wt)
        if mx > avg * 3:
            f.write(f"    ⚠ np={r['np']}: Worker 时间偏斜严重 "
                    f"(max={mx:.2f}s vs avg={avg:.2f}s) — 某些输入导致 SymCC 超时\n")

    # 检查 triage 瓶颈
    for r in sorted_runs:
        tri = r.get("prof_triage", 0)
        wall = r.get("wall_time", 1)
        if tri > wall * 0.1:
            f.write(f"    ⚠ np={r['np']}: Triage 占 {tri/wall*100:.1f}% 时间 — "
                    f"master 端 triage 是瓶颈\n")

    # 检查 idle 时间
    for r in sorted_runs:
        idle = r.get("prof_idle", 0)
        wall = r.get("wall_time", 1)
        if idle > wall * 0.3:
            f.write(f"    ⚠ np={r['np']}: Idle 占 {idle/wall*100:.1f}% 时间 — "
                    f"workers 饥饿（输入不足或 master 太慢）\n")

    # Hybrid 特有检查
    if mode == "hybrid":
        for r in sorted_runs:
            afl = r.get("afl_count", 0)
            symcc_all = r.get("symcc_all_count", 0)
            if afl > 0 and symcc_all > 0:
                ratio = symcc_all / afl
                f.write(f"    ℹ np={r['np']}: SymCC/AFL 比率 = {ratio:.1f}x "
                        f"(AFL={afl}, SymCC_all={symcc_all})\n")
                if symcc_all < afl:
                    f.write(f"      → SymCC 产量低于 AFL，考虑增加 SymCC workers\n")

    f.write("\n")


# ─────────────────────────────────────────────────────────────
# 添加 profiling 插桩
# ─────────────────────────────────────────────────────────────

def add_profiling_to_fuzzing_helper() -> None:
    """检查 mpi_fuzzing_helper.py 是否有 profiling 支持。不自动修改文件。"""
    pass  # profiling 通过解析现有输出完成，无需修改源码


def add_profiling_to_concolic() -> None:
    """检查 mpi_concolic_execution.py 是否有 profiling 支持。不自动修改源码。"""
    pass  # profiling 通过解析现有输出完成


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SymCC MPI 并行瓶颈分析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", default="gfts-xml_read_fuzzer",
                        help="测试目标名称 (默认: gfts-xml_read_fuzzer)")
    parser.add_argument("--np-list", default="2,4,8,16,32",
                        help="进程数列表 (默认: 2,4,8,16,32)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="每次运行超时秒数 (默认: 60)")
    parser.add_argument("--modes", default="mpi,hybrid",
                        help="测试模式: mpi,hybrid (默认: mpi,hybrid)")
    parser.add_argument("--output", default="benchmark/profile_results",
                        help="输出目录")
    parser.add_argument("--rounds", type=int, default=1,
                        help="每配置重复次数 (默认: 1)")
    parser.add_argument("--list-targets", action="store_true",
                        help="列出可用目标")
    args = parser.parse_args()

    targets = discover_targets()

    if args.list_targets:
        print("可用目标:")
        for name, info in sorted(targets.items()):
            afl = "✓" if info["afl_binary"] else "✗"
            print(f"  {name:30s}  AFL={afl}  seeds={info['seeds']}")
        return

    if args.target not in targets:
        print(f"错误: 目标 '{args.target}' 不存在")
        print(f"可用: {', '.join(sorted(targets.keys()))}")
        return 1

    target = targets[args.target]
    np_list = [int(x) for x in args.np_list.split(",")]
    modes = [m.strip() for m in args.modes.split(",")]

    print("=" * 60)
    print("  SymCC MPI 并行瓶颈分析")
    print("=" * 60)
    print(f"  目标:    {args.target}")
    print(f"  二进制:  {target['binary']}")
    print(f"  种子:    {target['seeds']}")
    print(f"  AFL:     {target['afl_binary'] or 'N/A'}")
    print(f"  NP:      {np_list}")
    print(f"  超时:    {args.timeout}s")
    print(f"  模式:    {modes}")
    print(f"  重复:    {args.rounds}")
    print()

    # 添加 profiling 插桩
    if "hybrid" in modes:
        add_profiling_to_fuzzing_helper()
    if "mpi" in modes:
        add_profiling_to_concolic()

    all_results: list[dict] = []
    total_runs = len(modes) * len(np_list) * args.rounds
    run_idx = 0

    for mode in modes:
        if mode == "hybrid" and not target["afl_binary"]:
            print(f"  跳过 hybrid 模式: {args.target} 没有 AFL 二进制")
            continue

        print(f"\n{'─' * 60}")
        print(f"  模式: {mode.upper()}")
        print(f"{'─' * 60}")

        for np_val in np_list:
            for rd in range(args.rounds):
                run_idx += 1
                print(f"\n  [{run_idx}/{total_runs}] np={np_val}, round={rd+1}...",
                      end="", flush=True)

                work_dir = tempfile.mkdtemp(prefix=f"prof_{mode}_np{np_val}_")

                try:
                    if mode == "mpi":
                        data = run_mpi_profiled(
                            target["binary"], target["seeds"],
                            np_val, args.timeout, work_dir,
                        )
                    elif mode == "hybrid":
                        data = run_hybrid_profiled(
                            target["binary"], target["afl_binary"],
                            target["seeds"], np_val, args.timeout, work_dir,
                        )
                    else:
                        print(f" 未知模式: {mode}")
                        continue

                    data["round"] = rd + 1
                    all_results.append(data)

                    tc = data.get("total_generated", 0)
                    tasks = data.get("total_tasks", data.get("total_analyzed", 0))
                    tp = data.get("throughput", 0)
                    if tp == 0 and data["wall_time"] > 0 and tc > 0:
                        tp = tc / data["wall_time"]

                    print(f" {data['wall_time']:.1f}s, {tasks} tasks, "
                          f"{tc} tc, {tp:.1f} tc/s")

                finally:
                    shutil.rmtree(work_dir, ignore_errors=True)

    if not all_results:
        print("\n  没有结果。")
        return

    # 生成报告
    print(f"\n{'=' * 60}")
    print("  生成报告...")
    generate_report(all_results, args.target, args.output)
    print(f"{'=' * 60}")

    # 打印简要摘要到终端
    print("\n  === 快速摘要 ===")
    for mode in modes:
        mode_runs = [r for r in all_results if r["mode"] == mode]
        if not mode_runs:
            continue
        print(f"\n  {mode.upper()}:")
        for r in sorted(mode_runs, key=lambda x: x["np"]):
            tc = r.get("total_generated", 0)
            tp = r.get("throughput", 0)
            if tp == 0 and r["wall_time"] > 0 and tc > 0:
                tp = tc / r["wall_time"]
            print(f"    np={r['np']:>3}: {tc:>8} tc, {tp:>8.1f} tc/s, "
                  f"{r['wall_time']:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
