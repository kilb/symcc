#!/usr/bin/env python3
"""多实例并行调度器。

将种子按功能分组，每组跑一个独立的 MPI SymCC 实例，
最后合并所有输出计算总覆盖率。

用法:
    python3 benchmark/run_multi_instance.py \
        --target sqlite \
        --total-cores 192 \
        --cores-per-instance 32 \
        --timeout 120

原理:
    192 核 / 32 核 = 6 个并行实例
    每个实例用不同的种子分组，探索目标程序的不同子系统
    最后合并所有实例的输出，计算总覆盖率
"""

import argparse
import os
import shutil
import subprocess
import signal
import sys
import tempfile
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
MPI_SCRIPT = PROJECT_DIR / "util" / "mpi_concolic_execution.py"


# ──────────────── 种子分组定义 ────────────────

SEED_GROUPS = {
    "sqlite": {
        "ddl": ["create_table.sql", "create_index.sql", "create_view.sql",
                "create_trigger.sql", "alter.sql", "drop.sql"],
        "dml": ["insert.sql", "select.sql", "update.sql", "delete.sql"],
        "query": ["join.sql", "subquery.sql", "union.sql", "group_by.sql", "cte.sql"],
        "control": ["transaction.sql", "pragma.sql", "explain.sql",
                     "analyze.sql", "vacuum.sql", "reindex.sql"],
        "expr": ["expressions.sql", "functions.sql", "blob_funcs.sql"],
        "multi": ["multi_stmt.sql"],
    },
    "libarchive": {
        "tar": ["seed_tar.tar", "seed_targz.tar.gz", "seed_tarbz2.tar.bz2", "seed_tarxz.tar.xz"],
        "zip": ["seed_zip.zip", "seed_7z_header"],
        "cpio_ar": ["seed_ar.a"],
        "headers": ["seed_gzip_header", "seed_bz2_header", "seed_xz_header",
                     "seed_rar_header", "seed_cab_header"],
    },
    "xml": {
        # xml 的种子用文件名自动分组，每 4 个一组
        "__auto__": True,
    },
}

TARGET_CONFIG = {
    "sqlite": {
        "symcc_binary": "benchmark/public/bin/sqlite/sqlite_fuzzer",
        "afl_binary": "benchmark/public/bin/sqlite-afl/sqlite_fuzzer",
        "seed_dir": "benchmark/public/seeds/sqlite/sqlite_fuzzer",
    },
    "libarchive": {
        "symcc_binary": "benchmark/public/bin/libarchive/archive_fuzzer",
        "afl_binary": "benchmark/public/bin/libarchive-afl/archive_fuzzer",
        "seed_dir": "benchmark/public/seeds/libarchive/archive_fuzzer",
    },
    "xml": {
        "symcc_binary": "benchmark/public/bin/google-fts/xml_read_fuzzer",
        "afl_binary": "benchmark/public/bin/google-fts-afl/xml_read_fuzzer",
        "seed_dir": "benchmark/public/seeds/google-fts/xml_read_fuzzer",
    },
}


def prepare_seed_groups(target: str) -> dict[str, list[str]]:
    """准备种子分组，返回 {组名: [种子文件完整路径]}。"""
    config = TARGET_CONFIG[target]
    seed_dir = Path(config["seed_dir"])
    groups_def = SEED_GROUPS.get(target, {})

    if groups_def.get("__auto__"):
        # 自动分组：每 N 个种子一组
        all_seeds = sorted(f.name for f in seed_dir.iterdir() if f.is_file())
        group_size = max(1, len(all_seeds) // 6)
        groups = {}
        for i in range(0, len(all_seeds), group_size):
            gname = f"group_{i // group_size}"
            groups[gname] = [str(seed_dir / s) for s in all_seeds[i:i + group_size]]
        return groups

    groups = {}
    for gname, fnames in groups_def.items():
        paths = []
        for fn in fnames:
            p = seed_dir / fn
            if p.exists():
                paths.append(str(p))
        if paths:
            groups[gname] = paths
    return groups


def run_instance(instance_id: int, group_name: str, seed_files: list[str],
                 symcc_binary: str, np: int, timeout: int,
                 output_dir: str) -> dict:
    """运行单个 MPI SymCC 实例。"""
    # 为这个实例创建临时种子目录
    seed_tmp = os.path.join(output_dir, f"seeds_{group_name}")
    os.makedirs(seed_tmp, exist_ok=True)
    for sf in seed_files:
        shutil.copy2(sf, seed_tmp)

    out_dir = os.path.join(output_dir, f"output_{group_name}")

    cmd = [
        "mpirun", "--allow-run-as-root", "--oversubscribe",
        "-np", str(np),
        "python3", "-u", str(MPI_SCRIPT),
        "-i", seed_tmp,
        "-o", out_dir,
        "-t", "10",
        "--wall-timeout", str(timeout - 5),
        "--", symcc_binary, "@@",
    ]

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout + 15, env=env,
        )
        stdout = result.stdout
    except subprocess.TimeoutExpired:
        stdout = ""

    elapsed = time.monotonic() - t0

    # 统计输出
    tc_count = 0
    if os.path.isdir(out_dir):
        tc_count = sum(1 for f in os.listdir(out_dir)
                       if os.path.isfile(os.path.join(out_dir, f)))

    # 解析吞吐量
    throughput = 0.0
    for line in stdout.splitlines():
        if "Throughput:" in line:
            try:
                throughput = float(line.split(":")[-1].strip().split()[0])
            except (ValueError, IndexError):
                pass

    return {
        "instance_id": instance_id,
        "group": group_name,
        "seeds": len(seed_files),
        "tc_count": tc_count,
        "throughput": throughput,
        "elapsed": round(elapsed, 1),
        "output_dir": out_dir,
    }


def measure_coverage(afl_binary: str, test_dirs: list[str],
                     label: str = "") -> dict:
    """用 afl-showmap -C 测量合并覆盖率。"""
    # 合并所有输出到临时目录
    combined = tempfile.mkdtemp(prefix="cov_combined_")
    total_files = 0
    for d in test_dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            src = os.path.join(d, f)
            if os.path.isfile(src):
                # 用目录名做前缀避免冲突
                dst = os.path.join(combined, f"{os.path.basename(d)}_{f}")
                try:
                    os.link(src, dst)  # 硬链接，不复制
                except OSError:
                    shutil.copy2(src, dst)
                total_files += 1

    if total_files == 0:
        shutil.rmtree(combined, ignore_errors=True)
        return {"edges": 0, "total": 0, "pct": 0.0, "files": 0}

    afl_showmap = shutil.which("afl-showmap")
    out_file = tempfile.mktemp(prefix=".afl_cov_", suffix=".map")
    cmd = [
        afl_showmap, "-t", "5000", "-m", "none", "-C",
        "-i", combined, "-o", out_file,
        "--", afl_binary, "@@",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=max(120, total_files * 2),
        )
        import re
        m = re.search(
            r"coverage of (\d+) edges were achieved out of (\d+) existing "
            r"\(([0-9.]+)%\)",
            result.stderr + result.stdout,
        )
        if m:
            edges = int(m.group(1))
            total = int(m.group(2))
            pct = float(m.group(3))
        else:
            edges = total = 0
            pct = 0.0
    except Exception:
        edges = total = 0
        pct = 0.0

    try:
        os.remove(out_file)
    except OSError:
        pass
    shutil.rmtree(combined, ignore_errors=True)

    return {"edges": edges, "total": total, "pct": pct, "files": total_files,
            "label": label}


def main():
    parser = argparse.ArgumentParser(description="多实例并行 SymCC 调度器")
    parser.add_argument("--target", default="sqlite",
                        choices=list(TARGET_CONFIG.keys()))
    parser.add_argument("--total-cores", type=int, default=192)
    parser.add_argument("--cores-per-instance", type=int, default=32)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output", default="benchmark/multi_instance_results")
    parser.add_argument("--also-single", action="store_true",
                        help="同时跑单实例作为对照")
    args = parser.parse_args()

    config = TARGET_CONFIG[args.target]
    groups = prepare_seed_groups(args.target)
    n_instances = min(len(groups), args.total_cores // args.cores_per_instance)
    np_per = args.cores_per_instance

    print("=" * 65)
    print(f"  多实例并行 SymCC — {args.target}")
    print("=" * 65)
    print(f"  总核数:     {args.total_cores}")
    print(f"  每实例核数: {np_per}")
    print(f"  实例数:     {n_instances}")
    print(f"  种子分组:   {len(groups)} ({', '.join(groups.keys())})")
    print(f"  超时:       {args.timeout}s")
    print()

    os.makedirs(args.output, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix="multi_instance_")

    # ──── 并行运行多个实例 ────
    print(f"  启动 {n_instances} 个并行实例...")
    t_start = time.monotonic()

    group_list = list(groups.items())[:n_instances]
    results = []

    with ProcessPoolExecutor(max_workers=n_instances) as executor:
        futures = {}
        for i, (gname, seeds) in enumerate(group_list):
            future = executor.submit(
                run_instance, i, gname, seeds,
                config["symcc_binary"], np_per, args.timeout, work_dir,
            )
            futures[future] = gname

        for future in as_completed(futures):
            gname = futures[future]
            try:
                r = future.result()
                results.append(r)
                print(f"    [{r['instance_id']}] {gname}: {r['tc_count']} tc, "
                      f"{r['throughput']:.0f} tc/s, {r['elapsed']:.0f}s")
            except Exception as e:
                print(f"    [{gname}] ERROR: {e}")

    total_time = time.monotonic() - t_start
    print(f"\n  全部完成: {total_time:.1f}s")

    # ──── 测量覆盖率 ────
    print(f"\n  测量覆盖率...")
    afl_bin = config["afl_binary"]

    # 每个实例的单独覆盖率
    instance_coverages = []
    for r in sorted(results, key=lambda x: x["instance_id"]):
        if r["tc_count"] > 0:
            cov = measure_coverage(afl_bin, [r["output_dir"]], r["group"])
            instance_coverages.append(cov)
            print(f"    {r['group']}: {cov['edges']}/{cov['total']} "
                  f"({cov['pct']:.2f}%)")

    # 合并覆盖率：从每个实例采样最多 5000 个文件（避免 afl-showmap 超时）
    sampled_dir = tempfile.mkdtemp(prefix="merged_sample_")
    total_sampled = 0
    for r in results:
        if not os.path.isdir(r["output_dir"]):
            continue
        files = sorted(os.listdir(r["output_dir"]))[:5000]
        for f in files:
            src = os.path.join(r["output_dir"], f)
            dst = os.path.join(sampled_dir, f"{r['group']}_{f}")
            if os.path.isfile(src):
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
                total_sampled += 1
    # 加入种子
    for f in os.listdir(config["seed_dir"]):
        src = os.path.join(config["seed_dir"], f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(sampled_dir, f"seed_{f}"))
            total_sampled += 1

    merged = measure_coverage(afl_bin, [sampled_dir], "merged")
    shutil.rmtree(sampled_dir, ignore_errors=True)
    print(f"\n    合并: {merged['edges']}/{merged['total']} "
          f"({merged['pct']:.2f}%) [{merged['files']} files]")

    # ──── 对照：单实例使用全部种子 ────
    single_cov = None
    if args.also_single:
        print(f"\n  对照: 单实例 np={np_per}, 全部种子, {args.timeout}s...")
        single_dir = os.path.join(work_dir, "single_output")
        single_result = run_instance(
            99, "single", [str(p) for g in groups.values() for p in g],
            config["symcc_binary"], np_per, args.timeout, work_dir,
        )
        # 改名
        if os.path.isdir(single_result["output_dir"]):
            os.rename(single_result["output_dir"], single_dir)
            single_result["output_dir"] = single_dir
        print(f"    single: {single_result['tc_count']} tc, "
              f"{single_result['throughput']:.0f} tc/s")
        single_cov = measure_coverage(
            afl_bin, [single_dir, config["seed_dir"]], "single"
        )
        print(f"    single coverage: {single_cov['edges']}/{single_cov['total']} "
              f"({single_cov['pct']:.2f}%)")

    # ──── 报告 ────
    total_tc = sum(r["tc_count"] for r in results)
    total_tp = sum(r["throughput"] for r in results)

    print(f"\n{'=' * 65}")
    print(f"  结果汇总")
    print(f"{'=' * 65}")
    print(f"  多实例 ({n_instances} × np={np_per}):")
    print(f"    总 tc: {total_tc:,}")
    print(f"    总吞吐: {total_tp:,.0f} tc/s")
    print(f"    合并覆盖: {merged['edges']}/{merged['total']} ({merged['pct']:.2f}%)")

    if single_cov:
        diff = merged['edges'] - single_cov['edges']
        pct_diff = merged['pct'] - single_cov['pct']
        print(f"\n  单实例 (np={np_per}, 全部种子):")
        print(f"    覆盖: {single_cov['edges']}/{single_cov['total']} "
              f"({single_cov['pct']:.2f}%)")
        print(f"\n  多实例 vs 单实例:")
        print(f"    覆盖率差: {diff:+d} edges ({pct_diff:+.2f}%)")
        if diff > 0:
            print(f"    ✓ 多实例多发现了 {diff} 条边")
        else:
            print(f"    → 覆盖率相同或更低")

    # 保存结果
    import json
    report = {
        "target": args.target,
        "config": {
            "total_cores": args.total_cores,
            "cores_per_instance": np_per,
            "n_instances": n_instances,
            "timeout": args.timeout,
        },
        "instances": results,
        "merged_coverage": merged,
        "single_coverage": single_cov,
    }
    report_path = os.path.join(args.output, f"{args.target}_multi_instance.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  报告保存: {report_path}")

    # 保留输出目录用于后续分析
    print(f"  工作目录: {work_dir}")


if __name__ == "__main__":
    main()
