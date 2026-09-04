#!/usr/bin/env python3
"""Render the current-technology overview used by the compendium."""

from __future__ import annotations

import subprocess
from pathlib import Path


HERE = Path(__file__).resolve().parent
OUT = HERE / "technology_compendium"
SRC = OUT / "src"
FONT = "WenQuanYi Zen Hei"


DOT = f"""
digraph G {{
  graph [
    bgcolor="white", pad="0.32", nodesep="0.38", ranksep="0.58",
    fontname="{FONT}", fontcolor="#17212B", fontsize=24,
    labelloc="t", labeljust="l",
    label="SymCC-Parallel 当前技术栈：从 LLVM 到可验证研究证据"
  ];
  node [
    shape=box, style="rounded,filled", margin="0.18,0.12",
    fontname="{FONT}", fontcolor="#17212B", fontsize=11,
    color="#CBD5E1", penwidth=1.3, fillcolor="#F8FAFC"
  ];
  edge [
    fontname="{FONT}", fontcolor="#64748B", fontsize=9,
    color="#64748B", penwidth=1.5, arrowsize=0.72
  ];
  rankdir=TB;

  input [label="目标程序 + 初始 corpus + 研究配置", fillcolor="#FEF3C7", color="#D97706"];

  compiler [
    label="编译与语义层\\n稳定 site ID · Backsolver/IFSS · UCSan\\nContinuation lowering · Hydra · definedness",
    fillcolor="#DBEAFE", color="#2563EB"
  ];
  runtime [
    label="运行时观测层\\n分支/依赖 telemetry · libc/string artifact\\ncode/data coverage · schedule trace",
    fillcolor="#CCFBF1", color="#0F766E"
  ];
  ir [
    label="统一中间表示层\\nPrefixDAG / ECT · Query IR · grammar/SPPF\\nCAS live state · schedule artifact",
    fillcolor="#EDE9FE", color="#6D28D9"
  ];

  subgraph cluster_engines {{
    label="候选生成与状态探索（不拥有最终接纳权）";
    color="#FDBA74"; style="rounded,filled"; fillcolor="#FFF7ED";
    solver [
      label="精确与复用求解\\nZ3/QF_BV/String portfolio\\nPangolin/PSCache/generator",
      fillcolor="#FFEDD5", color="#C2410C"
    ];
    semantic [
      label="结构与语义提议\\ntoken grammar · parser/PCFG\\nselective/optimistic proposal",
      fillcolor="#FFEDD5", color="#C2410C"
    ];
    state [
      label="并发与状态级探索\\nMPI seed workers · DPOR/SC/TSO/RA\\nContinuation/CAS frontier",
      fillcolor="#FFEDD5", color="#C2410C"
    ];
  }}

  replay [
    label="可信接纳边界\\n输入：完整约束复核 → 真实目标重放\\n变换：proof → 基线 replay；corpus：AFL edge claim",
    fillcolor="#FEE2E2", color="#B91C1C", penwidth=2.2
  ];
  feedback [
    label="并行反馈闭环\\nseed/worker/solver/parameter 调度\\ncoverage ownership · gossip · work stealing",
    fillcolor="#DCFCE7", color="#15803D"
  ];
  evidence [
    label="科研证据层\\nmanifest/seal/replay · paired protocol\\nablation · CI/effect size · claim gate",
    fillcolor="#F1F5F9", color="#475569"
  ];

  input -> compiler -> runtime -> ir;
  ir -> solver;
  ir -> semantic;
  ir -> state;
  solver -> replay;
  semantic -> replay;
  state -> replay;
  replay -> feedback;
  feedback -> ir [label="更新优先级、缓存与结构状态", color="#15803D"];
  replay -> evidence;

  invariant [
    shape=note,
    label="核心不变量\\nproposal 只有验证和重放后才可信；\\n编译变换须 proof/基线 replay，普通 corpus 须全局 edge novelty。",
    fillcolor="#FFFFFF", color="#B91C1C", fontcolor="#7F1D1D"
  ];
  replay -> invariant [style=dotted, arrowhead=none, color="#B91C1C"];
}}
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)
    dot_path = SRC / "technology-stack.dot"
    dot_path.write_text(DOT, encoding="utf-8")

    for fmt in ("svg", "png"):
        output = OUT / f"technology-stack.{fmt}"
        subprocess.run(
            ["dot", f"-T{fmt}", str(dot_path), "-o", str(output)],
            check=True,
        )


if __name__ == "__main__":
    main()
