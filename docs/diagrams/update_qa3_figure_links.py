#!/usr/bin/env python3
"""Replace named ASCII figure blocks in Architecture_QA3.md with image links."""

from __future__ import annotations

from pathlib import Path


DOC = Path(__file__).resolve().parents[1] / "Architecture_QA3.md"

FIGURES = {
    "### 图 1-0 两条执行流及其共同仲裁点": (
        "fig-1-0-execution-modes",
        "Native concolic 与 Continuation CAS 两条执行流及共同的新颖性仲裁",
    ),
    "### 图 1-1 进程拓扑": ("fig-1-1-process-topology", "SymCC-Parallel 进程拓扑与共享目录边界"),
    "### 图 1-2 五张位图 —— 谁在哪、什么哈希空间、谁读谁写": (
        "fig-1-2-bitmap-spaces",
        "AFL 与 QSYM 位图、哈希空间及传播关系",
    ),
    "### 图 1-3 一个工作项的完整时序": (
        "fig-1-3-worker-sequence",
        "单个工作项在 Master、Worker、求解器和 AFL 之间的执行次序",
    ),
    "### 图 1-4 四层过滤漏斗（实测，xml，8 worker × 322 工作项，180 s）": (
        "fig-1-4-filter-funnel",
        "Concolic 候选经过内容、Worker 位图与 Master 位图过滤的漏斗",
    ),
    "### 图 1-5 时间花在哪（实测，8 worker 合计 / 322 工作项）": (
        "fig-1-5-phase-time",
        "Worker 与 Master 已记账相位耗时",
    ),
    "### 图 1-6 concolic 产出到底有没有送到 AFL 手里（实测）": (
        "fig-1-6-afl-delivery",
        "Concolic 产出到 AFL 的三条交付路径及失效点",
    ),
    "### 图 2-1 同一段源码，优化器决定你拿到哪种约束": (
        "fig-2-1-constraint-shapes",
        "同一比较在不同优化形态下形成的三类符号约束",
    ),
    "### 图 2-2 形态 A 的表达式：8 字节 load 的 Concat 链": (
        "fig-2-2-concat-expression",
        "八字节读取形成的 Concat 表达式树",
    ),
    "### 图 2-3 哪个二进制带哪种技术 —— laf-intel 到底作用在谁身上": (
        "fig-2-3-binary-variants",
        "目标二进制变体与 SymCC、LAF、CmpLog、CTX、NGRAM 的作用域",
    ),
    "### 图 2-4 正常编译 vs 自测试：两条不同的管线": (
        "fig-2-4-build-test-pipelines",
        "真实 benchmark 与 lit 自测试的不同编译执行管线",
    ),
    "### 图 3-1 `negatePath` 决策树（默认配置走粗线）": (
        "fig-3-1-solve-decision",
        "negatePath 的严格求解、backsolve 与乐观回退决策树",
    ),
    '### 图 3-2 三种"丢前缀"的粒度对比': (
        "fig-3-2-prefix-dropping",
        "依赖切片、backsolve 与乐观求解的前缀保留粒度",
    ),
    "### 图 3-3 求解策略占比（实测，6 目标，默认配置）": (
        "fig-3-3-strategy-mix",
        "六个目标中 nominal 与 optimistic 保存产出的构成",
    ),
    "### 图 3-4 乐观求解到底浪不浪费？取决于目标格式": (
        "fig-3-4-optimistic-format",
        "XML 与 PNG 中 nominal 和 optimistic 候选的单位覆盖贡献",
    ),
    "### 图 3-5 嵌套深度：取头 FIFO 为何解不出，覆盖率制导为何能": (
        "fig-3-5-nested-frontier",
        "深层嵌套中 FIFO 前沿膨胀与覆盖率制导前沿",
    ),
    "### 图 4-1 一次执行 = 路径树上的一条线 + 一步邻居": (
        "fig-4-1-path-neighborhood",
        "一次动态符号执行覆盖的路径与一步邻居",
    ),
    "### 图 4-2 同一个二进制，只改种子长度": (
        "fig-4-2-seed-length",
        "四字节与八字节种子对符号可见性和可达深度的影响",
    ),
    "### 图 4-3 符号字节 vs 具体字节": (
        "fig-4-3-focus-bytes",
        "focus_bytes 门控下的符号字节与具体字节",
    ),
    "### 图 5-0A 统一再评估的平均边覆盖": (
        "fig-5-0a-icse23-edge-coverage",
        "ICSE 2023 统一再评估中 hybrid 与传统 fuzzer 的平均边覆盖",
    ),
    "### 图 5-0B 统一再评估的 unique crashes": (
        "fig-5-0b-icse23-unique-crashes",
        "ICSE 2023 统一再评估中 hybrid 与传统 fuzzer 的 unique crashes",
    ),
    "### 图 5-1 等 CPU 分配扫描：concolic 不是越多越好": (
        "fig-5-1-cpu-allocation",
        "固定 CPU 预算下 AFL 与 Concolic worker 的分配扫描",
    ),
    "### 图 6-1 LAVA-M base64：注入 bug 的发现个数与速度": (
        "fig-6-1-lava-endpoints",
        "LAVA-M base64 历史运行的 listed bug endpoint 与预算",
    ),
    "### 图 6-2 持久模式：带错一个 @@ 就等于没测": (
        "fig-6-2-persistent-mode",
        "持久模式带或不带文件占位符时的吞吐、队列和覆盖率",
    ),
    "### 图 6-3 吞吐 ≠ 覆盖率（xml，120 s，`benchmark_results_scaling_hybrid`）": (
        "fig-6-3-scaling-vs-coverage",
        "Pure MPI 与 Hybrid 的吞吐扩展和覆盖率扩展",
    ),
}


def replace_figure(lines: list[str], heading: str, stem: str, alt: str) -> bool:
    try:
        heading_index = next(i for i, line in enumerate(lines) if line.rstrip("\n") == heading)
    except StopIteration as exc:
        raise RuntimeError(f"Missing figure heading: {heading}") from exc

    next_heading = next(
        (i for i in range(heading_index + 1, len(lines)) if lines[i].startswith("#")),
        len(lines),
    )
    if any(f"diagrams/qa3/{stem}.svg" in line for line in lines[heading_index:next_heading]):
        return False

    try:
        fence_start = next(
            i for i in range(heading_index + 1, next_heading) if lines[i].rstrip("\n") == "```"
        )
        fence_end = next(
            i for i in range(fence_start + 1, next_heading) if lines[i].rstrip("\n") == "```"
        )
    except StopIteration as exc:
        raise RuntimeError(f"Missing ASCII block after: {heading}") from exc

    dot_source = Path(__file__).resolve().parent / "qa3" / "src" / f"{stem}.dot"
    source_link = (
        f"[DOT 图源](diagrams/qa3/src/{stem}.dot)"
        if dot_source.exists()
        else "[绘图脚本](diagrams/render_qa3_diagrams.py)"
    )
    replacement = [
        "\n",
        f"![{alt}](diagrams/qa3/{stem}.svg)\n",
        "\n",
        f"> 可缩放 SVG；[PNG 版本](diagrams/qa3/{stem}.png)；{source_link}。\n",
        "\n",
    ]
    lines[fence_start:fence_end + 1] = replacement
    return True


def main() -> None:
    lines = DOC.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = 0
    for heading, (stem, alt) in FIGURES.items():
        changed += int(replace_figure(lines, heading, stem, alt))
    DOC.write_text("".join(lines), encoding="utf-8")
    print(f"Updated {changed} figure blocks in {DOC}")


if __name__ == "__main__":
    main()
