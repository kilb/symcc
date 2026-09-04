#!/usr/bin/env python3
"""渲染 Project_Report_2026-07 的配图（SVG + PNG，可复现）。

配色、字体与 render_qa3_diagrams.py 完全一致，两套图集混排时视觉统一。
结构图走 Graphviz DOT，定量图直接生成 SVG。所有数值均来自：
  - docs/codex/evidence/current-eval-2026-07-30/independent_comparisons.csv（20 轮独立样本比较）
  - docs/codex/evidence/lava_m_current_2026_07_30/base64_summary.json（LAVA-M 求解消融）
  - docs/symsan_hybrid_benchmark_6targets.md（SymSan 六目标 3 轮均值）
  - docs/codex/Architecture_QA3.md（漏斗、策略占比、LAVA-M、持久模式实测）
不得在此文件内写入未经上述来源核对的数字。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from render_qa3_diagrams import (  # noqa: E402
    BLUE, BLUE_BG, GREEN, GREEN_BG, INK, LINE, MUTED,
    ORANGE, ORANGE_BG, PANEL, RED, RED_BG, TEAL, TEAL_BG, VIOLET, VIOLET_BG,
    dot_source, line, rect, svg_document, text,
)

OUT = HERE / "report"
SRC = OUT / "src"

GREY = "#94A3B8"
GREY_BG = "#F1F5F9"


# ══════════════════════════════ 结构图（Graphviz） ══════════════════════════════

GRAPHS = {
    "fig-r4-modules": r"""
  label="图 R-4  代码模块架构：五层，各层职责与规模";
  rankdir=TB; ranksep=0.34; nodesep=0.18; newrank=true;
  node [fontsize=12, height=0.58];

  subgraph cluster_bench {
    label="④ 评测与证据层　benchmark/"; fontsize=14;
    color="#93C5FD"; style="rounded,filled"; fillcolor="#EFF6FF";
    b1 [label="run_benchmark.py  4.1k\n构建目标变体 · 起 AFL/MPI · 测覆盖率", fillcolor="#DBEAFE", color="#2563EB", penwidth=2, width=3.5];
    b2 [label="research_protocol.py  1.9k\nsealed campaign · manifest · 随机区组", fillcolor="#DBEAFE", color="#2563EB", width=3.5];
    b3 [label="analyze_current_evaluation.py\nbootstrap CI · sign-flip · Holm 校正", fillcolor="#DBEAFE", color="#2563EB", width=3.5];
  }

  subgraph cluster_orch {
    label="③ 编排与算法层　util/　95.1k 行 · 64 模块（Python）"; fontsize=14;
    color="#5EEAD4"; style="rounded,filled"; fillcolor="#F0FDFA";
    u1 [label="mpi_fuzzing_helper.py  5.3k\nmaster/worker 协议 · 位图 B1/B2 · triage", fillcolor="#CCFBF1", color="#0F766E", penwidth=2, width=3.5];
    u2 [label="concolic_engine.py\nConcolicEngine 契约", fillcolor="#CCFBF1", color="#0F766E", width=2.6];
    u3 [label="query_store.py 3.8k\nstring_constraints.py 1.9k", fillcolor="#CCFBF1", color="#0F766E", width=2.6];
    u4 [label="semantic_proposals.py 12.7k\n在线语法 · SPPF · PCFG", fillcolor="#CCFBF1", color="#0F766E", width=2.6];
    u5 [label="live_continuation.py 13.5k\nschedule_exploration.py 6.8k", fillcolor="#CCFBF1", color="#0F766E", width=2.6];
    u6 [label="hybrid_feedback.py 3.9k\ndistributed_state.py 2.1k", fillcolor="#CCFBF1", color="#0F766E", width=2.6];
  }

  subgraph cluster_rt {
    label="② 运行时　runtime/src　5.7k 行（链进目标进程）"; fontsize=14;
    color="#FDBA74"; style="rounded,filled"; fillcolor="#FFFBEB";
    r1 [label="RuntimeCommon · Shadow · LibcWrappers\nGarbageCollection · UCSanRuntime · Config", fillcolor="#FFEDD5", color="#C2410C", width=4.2];
    r2 [label="backends/qsym\n表达式 DAG + Z3 + 分支剪枝图 B3", fillcolor="#FFEDD5", color="#C2410C", width=3.4];
    r3 [label="backends/simple\nSMT-LIB 转储（供 lit 断言）", fillcolor="#FFEDD5", color="#C2410C", width=3.0];
  }

  subgraph cluster_comp {
    label="① 编译期　compiler/　36.6k 行 C++ · 25 文件"; fontsize=14;
    color="#FCA5A5"; style="rounded,filled"; fillcolor="#FEF2F2";
    c1 [label="symcc · sym++\n编译器包装脚本", fillcolor="#FEE2E2", color="#B91C1C", width=2.6];
    c2 [label="Symbolizer · Pass · SiteId\n核心插桩", fillcolor="#FEE2E2", color="#B91C1C", penwidth=2, width=3.0];
    c3 [label="8 个新增 pass：UCSan · Hydra ·\nIFSS{Exit,Switch,LoopSummary,Continuation…}", fillcolor="#FEE2E2", color="#B91C1C", width=4.4];
  }

  subgraph cluster_qa {
    label="⑤ 质量与文档"; fontsize=14;
    color="#C4B5FD"; style="rounded,filled"; fillcolor="#F5F3FF";
    t1 [label="test/  231 个一级目录文件\n102 .ll · 54 .c · 52 .py · 17 .test32", fillcolor="#EDE9FE", color="#6D28D9", width=3.6];
    d1 [label="docs/ 顶层  43.8k 行\n权威档案 F00–F296 · SOTA 审计 · 证据清单", fillcolor="#EDE9FE", color="#6D28D9", width=4.2];
  }

  {rank=same; b1; b2; b3;}
  {rank=same; u1; u2; u3;}
  {rank=same; u4; u5; u6;}
  u2 -> u5 [style=invis];
  {rank=same; r1; r2; r3;}
  {rank=same; c1; c2; c3;}
  {rank=same; t1; d1;}

  b1 -> u1 [label="  起 mpirun", color="#2563EB"];
  b1 -> c1 [label="  构建变体", color="#2563EB"];
  u1 -> r1 [label="  argv + 环境（经 u2）", color="#0F766E"];
  r2 -> u1 [label="  telemetry / 候选", color="#C2410C", dir=back];
  c2 -> r1 [label="  插桩调用 _sym_*", color="#B91C1C"];
  c3 -> r1 [style=invis];
  t1 -> c2 [label="  验证接口与不变量", style=dashed, color="#6D28D9", fontsize=10];
  b3 -> d1 [style=dashed, color="#6D28D9"];
""",

    "fig-r5-artifacts": r"""
  label="图 R-5  一份源码 → 多个变体二进制 → 一次 campaign 的数据流";
  rankdir=TB; ranksep=0.45; nodesep=0.20;

  src [label="目标源码\nlibxml2 / SQLite / libarchive /\npcre2 / libpng / coreutils", fillcolor="#FEF3C7", color="#D97706"];

  subgraph cluster_v {
    label="构建变体（同一份源码，不同插桩）"; fontsize=14;
    color="#FCA5A5"; style="rounded,filled"; fillcolor="#FEF2F2";
    v1 [label="*_symcc\nSymCC pass 插桩\n（concolic worker 跑）", fillcolor="#FEE2E2", color="#B91C1C"];
    v2 [label="*_symsan\nDFSan 标签传播\n（--engine symsan）", fillcolor="#FFEDD5", color="#C2410C"];
    v3 [label="*_afl\nAFL++ PCGUARD\n（AFL 实例 + 所有 showmap 判新）", fillcolor="#DBEAFE", color="#2563EB", penwidth=2.2];
    v4 [label="*_afl_laf / _afl_ctx / _afl_ngram4 / _afl_laf_ctx\n共 5 种 AFL 构建变体 × 9 种运行 profile", fillcolor="#DBEAFE", color="#2563EB"];
    v5 [label="*_afl_cmplog\nRedQueen 伴随二进制", fillcolor="#DBEAFE", color="#2563EB"];
    v6 [label="*_native\n无插桩基线", fillcolor="#F1F5F9", color="#94A3B8"];
  }

  subgraph cluster_run {
    label="一次 campaign 的进程与目录（work_dir/）"; fontsize=14;
    color="#5EEAD4"; style="rounded,filled"; fillcolor="#F0FDFA";
    d1 [label="afl_out/fuzzer01…NN/\nAFL 实例队列与 fuzzer_stats", fillcolor="#CCFBF1", color="#0F766E"];
    d2 [label="symcc_all_outputs/\nconcolic 全部产出（--save-all）", fillcolor="#CCFBF1", color="#0F766E"];
    d3 [label="target_cwd/ · hf_cwd/\nCWD 隔离，避免目标互相写脏", fillcolor="#CCFBF1", color="#0F766E"];
    d4 [label="mpi_master.log\nphase timing · 漏斗计数", fillcolor="#CCFBF1", color="#0F766E"];
    d5 [label="combined_output/\nseed ∪ AFL queues ∪ SymCC 产出", fillcolor="#DCFCE7", color="#15803D", penwidth=2];
  }

  cov [label="afl-showmap 离线重放并集语料\n→ ShowmapCov（主指标）", fillcolor="#EDE9FE", color="#6D28D9", penwidth=2.2];
  csv [label="结果 CSV / JSON\nedge_cov_pct · afl_generated ·\nsymcc_generated · symcc_interesting", fillcolor="#EDE9FE", color="#6D28D9"];

  src -> {v1 v2 v3 v4 v5 v6} [color="#CBD5E1"];
  v3 -> d1 [color="#2563EB"];
  v1 -> d2 [color="#B91C1C"]; v2 -> d2 [color="#C2410C"];
  {d1 d2} -> d5 [color="#0F766E"];
  d5 -> cov [color="#15803D", penwidth=2];
  v3 -> cov [label="  用同一份 AFL 二进制测量", color="#2563EB", style=dashed];
  cov -> csv;
""",

    "fig-r8-mpi-protocol": r"""
  label="图 R-8  一个工作项的 MPI 协议往返：拉模式 + 内容寻址";
  rankdir=LR; ranksep=0.55; nodesep=0.30;

  subgraph cluster_m {
    label="master（1 个）"; fontsize=15;
    color="#93C5FD"; style="rounded,filled"; fillcolor="#EFF6FF";
    m1 [label="① 增量扫描 AFL queue\n（哈希缓存 + 节流）", fillcolor="#DBEAFE", color="#2563EB"];
    m2 [label="③ 按 PrefixDAG / ECT / 目标距离\n选工作项，打包内容对象", fillcolor="#DBEAFE", color="#2563EB"];
    m3 [label="⑦ 会话级位图 B1 最终判新\n边增量≠0 才写入语料", fillcolor="#DBEAFE", color="#2563EB", penwidth=2.2];
    m1 -> m2 [style=invis];
    m2 -> m3 [style=invis];
  }

  subgraph cluster_w {
    label="worker（N 个，各自独立临时目录）"; fontsize=15;
    color="#FDBA74"; style="rounded,filled"; fillcolor="#FFFBEB";
    w1 [label="② 声明就绪\nTAG_READY", fillcolor="#FFEDD5", color="#C2410C"];
    w2 [label="④ 恢复内容对象\n跑插桩目标 + 求解", fillcolor="#FFEDD5", color="#C2410C"];
    w3 [label="⑤ blake2b 内容去重\n→ afl-showmap 重放", fillcolor="#FFEDD5", color="#C2410C"];
    w4 [label="⑥ 本地位图 B2 预过滤\n只上传稀疏边 + 小 telemetry", fillcolor="#FFEDD5", color="#C2410C", penwidth=2.2];
    w1 -> w2 [style=invis]; w2 -> w3; w3 -> w4;
  }

  w1 -> m2 [label="  就绪", color="#C2410C"];
  m2 -> w2 [label="  TAG_WORK\n  内容对象", color="#2563EB"];
  w4 -> m3 [label="  TAG_RESULT\n  稀疏边列表", color="#C2410C", penwidth=2];
  m3 -> m1 [label="  coverage delta 广播", color="#2563EB", style=dashed, constraint=false];

""",

    "fig-r10-site-id": r"""
  label="图 R-10  编译期符号化：站点身份为什么必须稳定";
  rankdir=TB; ranksep=0.75; nodesep=0.35;

  src  [label="源码　if (x == 0x42) …", fillcolor="#FEF3C7", color="#D97706", width=4.6];
  ir   [label="LLVM IR　%c = icmp eq i32 %x, 66 ；br i1 %c, …", fillcolor="#F1F5F9", color="#94A3B8", width=4.6];
  pass [label="SymCC pass：插入 _sym_build_* 与 _sym_push_path_constraint",
        fillcolor="#EDE9FE", color="#6D28D9", penwidth=2.2, width=4.6];
  rt   [label="运行时实参（gdb 可直接看到）\n_sym_push_path_constraint(constraint=0x…, taken=0, site_id=97970033924416)",
        fillcolor="#CCFBF1", color="#0F766E", width=4.6];

  src -> ir -> pass -> rt [color="#CBD5E1"];

  subgraph cluster_ok {
    label="site_id 怎么来：compiler/SiteId.h"; fontsize=13;
    color="#4ADE80"; style="rounded,filled"; fillcolor="#F0FDF4";
    good [label="编译期 FNV 混合模块名 / 函数名 / 指令序号\n不依赖运行地址，也不依赖编译顺序",
          fillcolor="#DCFCE7", color="#15803D", penwidth=2];
    b3   [label="B3 分支兴趣位图\nXXH32(site_id, taken) % 65536\n判断这条分支是否值得再解", fillcolor="#DBEAFE", color="#2563EB"];
    good -> b3 [label="  作为哈希输入", fontsize=11];
  }

  subgraph cluster_bad {
    label="若改用运行地址做身份（反例）"; fontsize=13;
    color="#F87171"; style="rounded,filled"; fillcolor="#FEF2F2";
    bad1 [label="ASLR 下同一分支\n两次运行得到不同 ID", fillcolor="#FEE2E2", color="#B91C1C"];
    bad2 [label="B3 完全失效\n同一分支被反复求解、算力空转", fillcolor="#FEE2E2", color="#B91C1C", penwidth=2];
    bad1 -> bad2;
  }

  rt -> good [style=dashed, color="#6D28D9"];
  rt -> bad1 [style=dashed, color="#B91C1C"];
""",

    "fig-r12-query-ir": r"""
  label="图 R-12  Query IR 作为跨层证据总线";
  rankdir=TB; ranksep=0.45;

  subgraph cluster_p {
    label="生产者：谁都可以提出查询"; fontsize=14;
    color="#FDBA74"; style="rounded,filled"; fillcolor="#FFFBEB";
    a1 [label="concolic 路径约束", fillcolor="#FFEDD5", color="#C2410C"];
    a2 [label="字符串 / libc 语义", fillcolor="#FFEDD5", color="#C2410C"];
    a3 [label="grammar hole 补全", fillcolor="#FFEDD5", color="#C2410C"];
    a4 [label="schedule 约束", fillcolor="#FFEDD5", color="#C2410C"];
  }

  qir [label="Query IR：与具体 solver 解耦 · 内容寻址 · 结构特征 schema v2",
       fillcolor="#EDE9FE", color="#6D28D9", penwidth=2.6, width=7.2];

  subgraph cluster_c {
    label="消费者：复用与调度"; fontsize=14;
    color="#93C5FD"; style="rounded,filled"; fillcolor="#EFF6FF";
    b1 [label="exact cache\n同一查询直接命中", fillcolor="#DBEAFE", color="#2563EB"];
    b2 [label="prefix context 复用\n（Pangolin 式）", fillcolor="#DBEAFE", color="#2563EB"];
    b3 [label="PSCache 部分解", fillcolor="#DBEAFE", color="#2563EB"];
    b4 [label="QF_BV portfolio\ncvc5 / Bitwuzla / Z3", fillcolor="#DBEAFE", color="#2563EB"];
    b5 [label="sealed holdout campaign", fillcolor="#DBEAFE", color="#2563EB"];
  }

  gate [label="复用的三条硬规则\n① 跨前缀复用不传播 UNSAT　② SAT 必须有可验证模型\n③ 命中后一律对当前完整 roots 重新求值",
        fillcolor="#DCFCE7", color="#15803D", penwidth=2.4, width=7.2];

  {a1 a2 a3 a4} -> qir [color="#CBD5E1"];
  qir -> {b1 b2 b3 b4 b5} [color="#CBD5E1"];
  {b1 b2 b3 b4} -> gate [color="#15803D"];
""",

    "fig-r6-layers": r"""
  label="图 R-6  七层研究栈：从 LLVM IR 到可复现证据";
  rankdir=TB; nodesep=0.22; ranksep=0.34;
  node [width=3.5, height=0.52];

  L7 [label="⑦ 科研证据层　manifest / seal / 独立 replay / 随机配对协议 / claim gate", fillcolor="#EDE9FE", color="#6D28D9"];
  L6 [label="⑥ 反馈闭环层　AFL edge novelty 仲裁 · data/结构新颖度 · 执行成本回灌调度", fillcolor="#DBEAFE", color="#2563EB"];
  L5 [label="⑤ 并行探索层　seed 级 MPI worker · solver portfolio race · DPOR schedule · continuation frontier", fillcolor="#CCFBF1", color="#0F766E"];
  L4 [label="④ 求解与候选层　精确 Z3 · QF_BV/String portfolio · 上下文复用 · 部分解 · 乐观/Backsolver · 结构化候选", fillcolor="#DCFCE7", color="#15803D"];
  L3 [label="③ 统一表示层　PrefixDAG · ECT · Query IR · grammar/SPPF · schedule artifact · content-addressed state", fillcolor="#FFEDD5", color="#C2410C"];
  L2 [label="② 运行时观测层　路径 / 求解 / 数据进展 / 字符串操作 / 线程事件 / 候选来源 遥测", fillcolor="#FEF3C7", color="#D97706"];
  L1 [label="① 编译与语义层　稳定站点 · 控制/数据依赖 · 内存状态 · 可恢复 continuation · 证明门控 CFG 变换", fillcolor="#FEE2E2", color="#B91C1C"];

  L1 -> L2 -> L3 -> L4 -> L5 -> L6 -> L7 [color="#CBD5E1", arrowsize=0.6];
  L7 -> L1 [label="  实验结论回灌设计", color="#6D28D9", style=dashed, constraint=false, fontsize=11];
""",

    "fig-r7-trust-boundary": r"""
  label="图 R-7  可信边界：激进提议、保守接纳";
  rankdir=LR; ranksep=0.75;

  subgraph cluster_prop {
    label="提议侧（允许不完备、允许猜）"; fontsize=15;
    color="#FDBA74"; style="rounded,filled"; fillcolor="#FFFBEB";
    p1 [label="Z3 精确求解", fillcolor="#DCFCE7", color="#15803D"];
    p2 [label="乐观求解 / 丢前缀", fillcolor="#FFEDD5", color="#C2410C"];
    p3 [label="Backsolver 反演", fillcolor="#FFEDD5", color="#C2410C"];
    p4 [label="polyhedral / 跨前缀复用", fillcolor="#FFEDD5", color="#C2410C"];
    p5 [label="grammar / PCFG 结构提议", fillcolor="#FFEDD5", color="#C2410C"];
    p6 [label="agentic / 学习式排序", fillcolor="#FEE2E2", color="#B91C1C"];
    p7 [label="Hydra / IFSS 编译变换", fillcolor="#FEE2E2", color="#B91C1C"];
  }

  gate [label="① 完整当前约束重新求值\n② 真实目标程序重放\n③ 编译变换另需 proof gate\n　 + 未变换基线 replay",
        shape=box, fillcolor="#EDE9FE", color="#6D28D9", penwidth=2.4, fontsize=13];

  arb [label="全局 AFL edge novelty 仲裁\n（master 会话级位图 / coverage owner）",
       fillcolor="#DBEAFE", color="#2563EB", penwidth=2.2, fontsize=13];

  corpus [label="普通 corpus\n（唯一有效产出）", fillcolor="#CCFBF1", color="#0F766E", penwidth=2.2];
  drop   [label="丢弃\n（记入 telemetry，不计功）", fillcolor="#F1F5F9", color="#94A3B8"];

  {p1 p2 p3 p4 p5 p6 p7} -> gate [color="#CBD5E1"];
  gate -> arb [label="  通过", color="#15803D", penwidth=2];
  gate -> drop [label="  未通过", color="#94A3B8", style=dashed];
  arb -> corpus [label="  边增量≠0", color="#15803D", penwidth=2];
  arb -> drop [label="  无新边", color="#94A3B8", style=dashed];
""",

    "fig-r9-engine-abstraction": r"""
  label="图 R-9  ConcolicEngine 契约：换引擎不换评测口径";
  rankdir=TB; ranksep=0.85;

  master [label="MPI master　工作项调度 / 全局位图仲裁 / 遥测汇总", fillcolor="#DBEAFE", color="#2563EB", width=6.6];
  iface  [label="ConcolicEngine 契约\nargv 构造 · 环境变量 · 输出目录约定 · telemetry schema",
          fillcolor="#EDE9FE", color="#6D28D9", penwidth=2.2, width=6.6];

  subgraph cluster_symcc {
    label="--engine symcc（默认）"; fontsize=14;
    color="#5EEAD4"; style="rounded,filled"; fillcolor="#F0FDFA";
    c1 [label="LLVM pass 插桩\n编译期符号化", fillcolor="#CCFBF1", color="#0F766E"];
    c2 [label="QSYM 后端 + Z3\n表达式 DAG / 分支剪枝图", fillcolor="#CCFBF1", color="#0F766E"];
    c1 -> c2;
  }

  subgraph cluster_symsan {
    label="--engine symsan"; fontsize=14;
    color="#FDBA74"; style="rounded,filled"; fillcolor="#FFFBEB";
    s1 [label="DFSan 影子内存\n标签传播", fillcolor="#FFEDD5", color="#C2410C"];
    s2 [label="fgtest driver\nRGD: I2S / JIGSAW / Z3 级联", fillcolor="#FFEDD5", color="#C2410C"];
    s1 -> s2;
  }

  triage [label="共享 triage：blake2b 内容去重 → afl-showmap 重放 → B2 预过滤 → B1 全局判新",
          fillcolor="#DCFCE7", color="#15803D", penwidth=2, width=6.6];

  master -> iface;
  iface -> c1; iface -> s1;
  c2 -> triage; s2 -> triage;
""",

    "fig-r11-solve-cost-layers": r"""
  label="图 R-11  求解代价层次";
  rankdir=TB; ranksep=0.30; nodesep=0.18;
  node [width=5.6, height=0.62, fontsize=13];

  s0 [label="0　要不要解\nbranch-interest / 依赖 / prefix 策略", fillcolor="#F1F5F9", color="#94A3B8"];
  s1 [label="1　查已有答案\nexact cache / prefix context / PSCache", fillcolor="#DCFCE7", color="#15803D"];
  s2 [label="2　不用 SMT 直接构造\nfastSolve / 乐观 generator / Backsolver", fillcolor="#CCFBF1", color="#0F766E"];
  s3 [label="3　专用后端\nselective partition / string / grammar", fillcolor="#DBEAFE", color="#2563EB"];
  s4 [label="4　单 solver 精确求解\n完整前缀 + 目标取反 → Z3", fillcolor="#FFEDD5", color="#C2410C"];
  s5 [label="5　有界并行 portfolio\ncvc5 / Bitwuzla / Z3 竞速", fillcolor="#FEE2E2", color="#B91C1C"];
  s6 [label="6　超时 / unknown\n保留证据、回退或延后", fillcolor="#EDE9FE", color="#6D28D9"];
  hit [label="得到候选 → 进入 triage 与全局 edge 仲裁", fillcolor="#DCFCE7", color="#15803D", penwidth=2.2];

  s0 -> s1 -> s2 -> s3 -> s4 -> s5 -> s6 [color="#CBD5E1", arrowsize=0.7];
  s1 -> hit [label="  缓存命中，成本≈0", style=dashed, color="#15803D", constraint=false, fontsize=11];
  s2 -> hit [label="  直接反演，不进 SMT", style=dashed, color="#0F766E", constraint=false, fontsize=11];
  s4 -> hit [label="  SAT", style=dashed, color="#C2410C", constraint=false, fontsize=11];
  s6 -> hit [style=invis];
""",

    "fig-r20-roadmap": r"""
  label="图 R-20  已完成 / 进行中 / 待补：三类工作的边界";
  rankdir=LR; ranksep=0.7;

  subgraph cluster_done {
    label="已完成并有证据"; fontsize=15;
    color="#4ADE80"; style="rounded,filled"; fillcolor="#F0FDF4";
    d1 [label="F00–F296 实现 + lit/单元测试", fillcolor="#DCFCE7", color="#15803D"];
    d2 [label="双引擎端到端跑通\n（symcc / symsan × 6 目标）", fillcolor="#DCFCE7", color="#15803D"];
    d3 [label="20 轮独立样本配置比较\n两目标均显著", fillcolor="#DCFCE7", color="#15803D"];
    d4 [label="机制级实测：漏斗 / 策略占比 /\n嵌套深度 / 持久模式", fillcolor="#DCFCE7", color="#15803D"];
  }

  subgraph cluster_wip {
    label="进行中"; fontsize=15;
    color="#FBBF24"; style="rounded,filled"; fillcolor="#FFFBEB";
    w1 [label="sealed 随机区组确认实验设计", fillcolor="#FEF3C7", color="#D97706"];
    w2 [label="等 CPU-second 在线闭环实验", fillcolor="#FEF3C7", color="#D97706"];
  }

  subgraph cluster_todo {
    label="尚未获得、不能宣称"; fontsize=15;
    color="#F87171"; style="rounded,filled"; fillcolor="#FEF2F2";
    t1 [label="297 项技术各自的独立增益", fillcolor="#FEE2E2", color="#B91C1C"];
    t2 [label="24 h campaign 的漏洞数 /\n首次触发时间 / 生存曲线", fillcolor="#FEE2E2", color="#B91C1C"];
    t3 [label="Magma / FuzzBench / UniBench\n跨项目泛化", fillcolor="#FEE2E2", color="#B91C1C"];
    t4 [label="在线闭环相对离线并集的净收益", fillcolor="#FEE2E2", color="#B91C1C"];
  }

  d3 -> w1 [color="#CBD5E1"];
  w1 -> t2 [label="  sealed confirmatory campaign", color="#94A3B8", style=dashed, fontsize=11];
""",
}


# ══════════════════════════════ 定量图（自绘 SVG） ══════════════════════════════

def write_svg(name: str, title: str, subtitle: str, body: str,
              width: int = 1600, height: int = 900) -> None:
    (OUT / f"{name}.svg").write_text(
        svg_document(title, subtitle, body, width, height), encoding="utf-8"
    )


def chart_headline_forest() -> None:
    """图 R-15：20 轮独立样本配置比较。数据源 independent_comparisons.csv。"""
    rows = [
        ("当前 Hybrid  vs  旧组合 Hybrid", "libarchive", 0.953, 0.582, 1.321, 0.871, "0.00168", True),
        ("当前 Hybrid  vs  旧组合 Hybrid", "SQLite", 1.949, 1.466, 2.441, 0.961, "0.00032", True),
        ("批次 sanity　当前 AFL-only  vs  旧 AFL-only", "libarchive", 0.034, -0.333, 0.389, 0.537, "1.000", False),
        ("批次 sanity　当前 AFL-only  vs  旧 AFL-only", "SQLite", 0.344, -0.257, 0.918, 0.574, "1.000", False),
    ]
    x0, x1 = 620, 1230          # 绘图区
    lo, hi = -0.6, 2.7          # 数据范围（pp）
    top, step = 180, 110

    def px(v: float) -> float:
        return x0 + (v - lo) / (hi - lo) * (x1 - x0)

    parts = [rect(48, 120, 1504, len(rows) * step + 140, fill="#FFFFFF", stroke=LINE)]
    # 网格
    for g in (-0.5, 0, 0.5, 1, 1.5, 2, 2.5):
        gx = px(g)
        parts.append(line(gx, 150, gx, top + len(rows) * step - 18,
                          stroke=INK if g == 0 else "#E2E8F0",
                          width=2 if g == 0 else 1,
                          dash=None if g == 0 else "4 5"))
        parts.append(text(gx, 144, f"{g:+.1f}" if g else "0", size=13,
                          fill=INK if g == 0 else MUTED, anchor="middle"))
    parts.append(text((x0 + x1) / 2, top + len(rows) * step + 16,
                      "独立样本均值差（百分点，边覆盖率）",
                      size=14, fill=MUTED, anchor="middle"))
    parts.append(text(1300, 144, "A12", size=13, fill=MUTED, anchor="middle"))
    parts.append(text(1420, 144, "Holm p", size=13, fill=MUTED, anchor="middle"))

    last_group = None
    for i, (grp, tgt, med, cl, ch, a12, p, sig) in enumerate(rows):
        y = top + i * step
        if grp != last_group:
            parts.append(text(72, y - 22, grp, size=15, weight=700))
            last_group = grp
        col, bg = (GREEN, GREEN_BG) if sig else (GREY, GREY_BG)
        parts.append(text(96, y + 12, f"· {tgt}", size=14, fill=MUTED))
        parts.append(rect(px(cl), y - 5, max(2.0, px(ch) - px(cl)), 22,
                          fill=bg, stroke=col, radius=5, stroke_width=1.4))
        parts.append(line(px(cl), y - 9, px(cl), y + 21, stroke=col, width=2.2))
        parts.append(line(px(ch), y - 9, px(ch), y + 21, stroke=col, width=2.2))
        parts.append(f'<circle cx="{px(med)}" cy="{y + 6}" r="7.5" fill="{col}"/>')
        parts.append(text(px(ch) + 14, y + 12, f"{med:+.3f}", size=15, weight=700,
                          fill=col))
        parts.append(text(1300, y + 12, f"{a12:.3f}", size=14, anchor="middle",
                          fill=INK if sig else MUTED))
        parts.append(text(1420, y + 12, p, size=14, weight=700 if sig else 400,
                          anchor="middle", fill=col))
        parts.append(text(1500, y + 12, "✓" if sig else "—", size=17, weight=700,
                          anchor="middle", fill=col))

    yb = top + len(rows) * step + 36
    parts.append(rect(72, yb, 1456, 62, fill=BLUE_BG, stroke=BLUE, radius=8))
    parts.append(text(96, yb + 26,
                      "读法：点=两组独立样本均值差，方框=10,000 次 independent bootstrap 的 95% 区间；Holm 校正后 p<0.05 记为显著（绿色）。",
                      size=14))
    parts.append(text(96, yb + 48,
                      "后两行是批次 sanity：两批单实例 AFL-only 无显著差异；它不是等 CPU 基线，也不证明 Hybrid 的单项因果收益。",
                      size=14, fill=MUTED))
    write_svg("fig-r15-headline-forest",
              "图 R-15　20 轮独立样本配置比较：当前与旧 Hybrid，以及批次 sanity",
              "Hybrid 均为名义 8 核；每组 n=20、配置预算 15 s；主指标为 afl-showmap 对含 save-all 候选并集的离线边覆盖率",
              "\n  ".join(parts), 1600, top + len(rows) * step + 176)


def chart_eval_groups() -> None:
    """图 R-7：四组配置的 20 轮边覆盖率均值与区间。"""
    data = {
        "libarchive 3.3.2": [
            ("旧批次 AFL-only（1 核 sanity）", 15.038, 13.850, 16.890, GREY, GREY_BG),
            ("旧组合（1 AFL + 6 SymCC，off）", 15.508, 14.080, 16.810, ORANGE, ORANGE_BG),
            ("当前批次 AFL-only（1 核 sanity）", 15.072, 14.140, 16.110, GREY, GREY_BG),
            ("当前（4 AFL + 3 SymCC，basic）", 16.460, 15.310, 17.480, GREEN, GREEN_BG),
        ],
        "SQLite 3.13.0": [
            ("旧批次 AFL-only（1 核 sanity）", 18.437, 17.070, 19.950, GREY, GREY_BG),
            ("旧组合（1 AFL + 6 SymCC，off）", 18.443, 16.820, 19.670, ORANGE, ORANGE_BG),
            ("当前批次 AFL-only（1 核 sanity）", 18.781, 16.780, 19.970, GREY, GREY_BG),
            ("当前（4 AFL + 3 SymCC，basic）", 20.392, 19.350, 21.670, GREEN, GREEN_BG),
        ],
    }
    parts = []
    panel_x, panel_w = 48, 740
    for pi, (target, series) in enumerate(data.items()):
        ox = panel_x + pi * (panel_w + 24)
        parts.append(rect(ox, 120, panel_w, 640, fill="#FFFFFF", stroke=LINE))
        parts.append(text(ox + 28, 156, target, size=19, weight=700))
        lo, hi = 13.0, 22.5
        bx0, bx1 = ox + 400, ox + panel_w - 78

        def px(v: float, _b0=bx0, _b1=bx1, _lo=lo, _hi=hi) -> float:
            return _b0 + (v - _lo) / (_hi - _lo) * (_b1 - _b0)

        for g in range(14, 23, 2):
            parts.append(line(px(g), 182, px(g), 700, stroke="#E2E8F0", dash="4 5"))
            parts.append(text(px(g), 726, str(g), size=12, fill=MUTED, anchor="middle"))
        parts.append(text((bx0 + bx1) / 2, 748, "并集语料边覆盖率（%）", size=13,
                          fill=MUTED, anchor="middle"))
        for i, (lbl, mean, rlo, rhi, col, bg) in enumerate(series):
            y = 214 + i * 122
            parts.append(text(ox + 28, y + 4, lbl, size=14))
            parts.append(rect(px(rlo), y + 24, px(rhi) - px(rlo), 16,
                              fill=bg, stroke=col, radius=4, stroke_width=1.2))
            parts.append(line(px(mean), y + 16, px(mean), y + 48, stroke=col, width=3.4))
            parts.append(text(px(mean), y + 68, f"{mean:.3f}", size=15, weight=700,
                              fill=col, anchor="middle"))
            parts.append(text(ox + 28, y + 26, "n=20，条=20 轮全距，竖线=均值",
                              size=11, fill=MUTED))
    parts.append(rect(48, 782, 1504, 56, fill=GREEN_BG, stroke=GREEN, radius=8))
    parts.append(text(72, 806,
                      "关键结论：在同为名义 8 核的 Hybrid 内部，当前组合配置相对旧组合配置在两个目标上都显著提高离线候选并集覆盖。",
                      size=14))
    parts.append(text(72, 828,
                      "单实例 AFL-only 只用于批次 sanity 和系统配置参照；它不是等 CPU 基线，不能据此宣称 Hybrid 相对纯 AFL 的因果优势。",
                      size=14, fill=MUTED))
    write_svg("fig-r16-eval-groups",
              "图 R-16　四组配置的 20 轮边覆盖率分布",
              "同一台机器、同一份当前 SymCC 目标二进制、同一批种子、15 s 配置预算；名义 8 核 Hybrid vs 1 核 AFL-only",
              "\n  ".join(parts), 1600, 866)


def chart_symsan_targets() -> None:
    """图 R-9：SymSan 引擎六目标 hybrid（3 轮均值）。"""
    rows = [
        ("pcre2", "正则引擎", 118, 99, 144, 46.5, 4520, 9728),
        ("xml", "libxml2 解析器", 89, 0, 139, 9.9, 5019, 50880),
        ("sqlite", "数据库引擎", 86, 83, 89, 20.5, 6480, 31552),
        ("png", "libpng 解析器", 37, 24, 44, 21.7, 665, 3072),
        ("base64", "LAVA-M 编解码", 28, 23, 33, 50.5, 97, 192),
        ("uniq", "LAVA-M coreutils", 8, 8, 8, 11.0, 134, 1216),
    ]
    parts = [rect(48, 120, 1504, 620, fill="#FFFFFF", stroke=LINE)]
    bx0, bx1, top, step = 470, 1060, 196, 92
    mx = 150.0
    parts.append(text(bx0, 162, "concolic interesting（3 轮均值，横条为 3 轮全距）",
                      size=14, weight=700, fill=TEAL))
    parts.append(text(1200, 162, "边覆盖", size=14, weight=700, fill=BLUE))
    for i, (name, kind, mean, rlo, rhi, pct, e, et) in enumerate(rows):
        y = top + i * step
        parts.append(text(72, y + 6, name, size=19, weight=700))
        parts.append(text(72, y + 28, kind, size=13, fill=MUTED))
        w = (bx1 - bx0) * mean / mx
        parts.append(rect(bx0, y - 12, max(3.0, w), 34, fill=TEAL_BG, stroke=TEAL, radius=6))
        rx0 = bx0 + (bx1 - bx0) * rlo / mx
        rx1 = bx0 + (bx1 - bx0) * rhi / mx
        parts.append(line(rx0, y + 5, rx1, y + 5, stroke=TEAL, width=2))
        parts.append(line(rx0, y - 3, rx0, y + 13, stroke=TEAL, width=2))
        parts.append(line(rx1, y - 3, rx1, y + 13, stroke=TEAL, width=2))
        lx = max(bx0 + max(3.0, w), rx1) + 16
        parts.append(text(lx, y + 12, str(mean), size=18, weight=700, fill=TEAL))
        parts.append(text(lx + 46, y + 12, f"[{rlo}–{rhi}]", size=12, fill=MUTED))
        parts.append(rect(1200, y - 10, 210, 30, fill=BLUE_BG, stroke=BLUE, radius=6))
        parts.append(rect(1200, y - 10, 210 * pct / 55.0, 30, fill=BLUE, stroke=BLUE, radius=6))
        parts.append(text(1425, y + 11, f"{pct:.1f}%", size=15, weight=700, fill=BLUE))
        parts.append(text(1470, y + 11, f"{e}/{et}", size=11, fill=MUTED))
    parts.append(rect(48, 756, 1504, 82, fill=ORANGE_BG, stroke=ORANGE, radius=8))
    parts.append(text(72, 782,
                      "该看 concolic 列而不是 edge%：hybrid 里 AFL 每轮产出上千用例、主导原始边覆盖，SymSan 的价值是解出 AFL 猜不到的魔数、校验和与结构键。",
                      size=14))
    parts.append(text(72, 806,
                      "xml 三轮为 139 / 128 / 0——有一轮 concolic worker 在 20 s 内超时未贡献。这正是必须跑多轮并给出全距的原因，单轮数字不可引用。",
                      size=14, fill=MUTED))
    parts.append(text(72, 828,
                      "uniq 稳定为 8（三轮一致）：它需要一处真实的 SymSan runtime 修复（taint_getc 每次 getc 丢污点）加一处构建 flag 修复后才可解。",
                      size=14, fill=MUTED))
    write_svg("fig-r17-symsan-6targets",
              "图 R-17　SymSan 引擎六目标 hybrid 基准（3 轮独立 20 s 均值）",
              "--engine symsan，--np-list 8（3 AFL 实例 + 2 concolic worker），afl-showmap -C 测边覆盖",
              "\n  ".join(parts), 1600, 866)


def chart_scale() -> None:
    """图 R-2：工程规模速览。

    所有计数在渲染时当场从仓库统计，不写死——此前 test/ 文件数随工作树从 230 漂到 232，
    图与正文对不上。口径同时印在图里，读者可以自己复算。
    """
    repo = HERE.parents[2]

    def sh(cmd: list[str]) -> str:
        return subprocess.run(cmd, cwd=repo, capture_output=True, text=True).stdout.strip()

    def count_lines(paths) -> int:
        n = 0
        for f in paths:
            try:
                n += len(f.read_text(encoding="utf-8", errors="ignore").splitlines())
            except OSError:
                pass
        return n

    commits = sh(["git", "rev-list", "--count", "HEAD"]) or "?"
    archive = (HERE.parent / "New_Implementation_Archive.md").read_text(encoding="utf-8")
    feats = len(set(re.findall(r"\bF[0-9]{2,3}\b", archive)))
    test_files = [
        f for f in (repo / "test").iterdir()
        if f.is_file() and not f.is_symlink()
    ]
    by_ext: dict[str, int] = {}
    for f in test_files:
        by_ext[f.suffix.lstrip(".") or "-"] = by_ext.get(f.suffix.lstrip(".") or "-", 0) + 1
    top_ext = sorted(by_ext.items(), key=lambda kv: -kv[1])[:4]
    cfg = len(set(re.findall(r"SYMCC_[A-Z0-9_]+",
                             (repo / "docs/Configuration.txt").read_text(encoding="utf-8"))))
    cpp = count_lines(list((repo / "compiler").glob("*.cpp")) + list((repo / "compiler").glob("*.h")))
    cpp_files = len(list((repo / "compiler").glob("*.cpp")) + list((repo / "compiler").glob("*.h")))
    py = count_lines((repo / "util").glob("*.py"))
    py_files = len(list((repo / "util").glob("*.py")))
    docs = count_lines(list((repo / "docs").glob("*.md")) + list((repo / "docs").glob("*.txt")))
    rt = count_lines(f for f in (repo / "runtime/src").rglob("*")
                     if f.is_file() and f.suffix in {".cpp", ".h"}
                     and "qsym/qsym" not in str(f) and "third_party" not in str(f))
    figs = len(list(OUT.glob("*.svg"))) or len(GRAPHS) + 11

    cards = [
        (commits, "git 提交", ["自 fork 上游 SymCC 起"], BLUE, BLUE_BG),
        (str(feats), "功能条目 F00–F296", ["每条含方案 / 代码 / 测试", "证据等级 / 已知局限"], VIOLET, VIOLET_BG),
        (str(len(test_files)), "test/ 一级目录文件",
         [" + ".join(f"{n} .{e}" for e, n in top_ext[:2]),
          " + ".join(f"{n} .{e}" for e, n in top_ext[2:]) + " 等"], GREEN, GREEN_BG),
        (str(cfg), "已登记 SYMCC_* 名称", ["取自 docs/Configuration.txt", "去重计数"], ORANGE, ORANGE_BG),
        (f"{cpp / 1000:.1f}k", "行 C++（compiler/）", [f"{cpp_files} 个源文件",
         "含 8 个新增 LLVM pass"], TEAL, TEAL_BG),
        (f"{py / 1000:.1f}k", "行 Python（util/）", [f"{py_files} 个模块",
         f"runtime/src 另有 {rt / 1000:.1f}k 行 C++"], RED, RED_BG),
        (f"{docs / 1000:.1f}k", "行文档（docs/ 顶层）", ["含权威档案、SOTA 审计", "与逐项证据清单"], INK, PANEL),
        (str(figs), "张本报告配图", ["全部由 render_project_report.py", "一条命令重出"], MUTED, GREY_BG),
    ]
    parts = []
    for i, (num, label, note, col, bg) in enumerate(cards):
        cx = 48 + (i % 4) * 380
        cy = 140 + (i // 4) * 250
        parts.append(rect(cx, cy, 356, 226, fill=bg, stroke=col, radius=14, stroke_width=1.8))
        parts.append(text(cx + 28, cy + 92, num, size=58, weight=700, fill=col))
        parts.append(text(cx + 28, cy + 132, label, size=19, weight=700))
        for j, ln in enumerate(note):
            parts.append(text(cx + 28, cy + 166 + j * 24, ln, size=13, fill=MUTED))
    parts.append(text(48, 668,
                      "以上数字在渲染时当场统计，不写死：git rev-list --count HEAD；"
                      "New_Implementation_Archive.md 中去重的 F 编号；test/ 一级目录普通文件；"
                      "Configuration.txt 中去重的 SYMCC_* 名称；各目录源文件行数。",
                      size=13, fill=MUTED))
    write_svg("fig-r2-scale", "图 R-2　工程规模速览",
              "统计口径写在图内；因工作树仍在变动，这些数字以重新渲染时为准",
              "\n  ".join(parts), 1600, 700)


def chart_evidence_grades() -> None:
    """图 R-14：证据等级与本报告的数据构成。右栏用两行排版，避免超出画布。"""
    tiers = [
        ("R 级　确认性结论", "sealed 随机区组 + 等 CPU-second + 每格 ≥20 轮 + 消融",
         ["本项目尚无"], RED, RED_BG, 560),
        ("A 级　工程统计", "同二进制与初始语料、每组 n=20 独立样本 + bootstrap CI + label permutation + Holm",
         ["20 轮组合配置比较", "（§4.2）"], GREEN, GREEN_BG, 680),
        ("B 级　多轮描述", "同一配置重复运行，报告多轮均值 + 全距；未做等 CPU 与随机化",
         ["SymSan 六目标（§4.3）"], TEAL, TEAL_BG, 800),
        ("C 级　机制描述", "单次或不同 seed 的有界测量，用于解释机制与量级，不提供重复运行推断",
         ["LAVA 求解消融（§4.4）· 漏斗 / 策略占比", "求解开关 / 种子长度 / 持久模式 / 扩展性"], ORANGE, ORANGE_BG, 920),
        ("D 级　历史阶段数据", "旧工作树单轮结果，只能追溯量级",
         ["300 s 历史三组数据"], GREY, GREY_BG, 1040),
    ]
    parts = []
    for i, (name, rule, use, col, bg, w) in enumerate(tiers):
        y = 152 + i * 122
        x = 40 + (1020 - w) / 2
        parts.append(rect(x, y, w, 100, fill=bg, stroke=col, radius=10, stroke_width=2))
        parts.append(text(x + 24, y + 38, name, size=20, weight=700, fill=col))
        parts.append(text(x + 24, y + 66, rule, size=13, fill=MUTED))
        parts.append(text(1104, y + 30, "本报告对应内容", size=12, fill=MUTED))
        for k, u in enumerate(use):
            parts.append(text(1104, y + 56 + k * 22, u, size=14.5, weight=700, fill=col))
    parts.append(rect(40, 770, 1520, 84, fill=VIOLET_BG, stroke=VIOLET, radius=10))
    parts.append(text(64, 800, "这套分级本身是成果的一部分。", size=17, weight=700, fill=VIOLET))
    parts.append(text(64, 826,
                      "项目要求每个新功能在合入前必须登记方案、代码、测试证据、结果等级和已知局限；等级不足的数字不允许写成结论。",
                      size=14))
    parts.append(text(64, 848,
                      "本报告全部数据按上表标注来源，C 级以下不参与「有效 / 更优」这类判断。",
                      size=14, fill=MUTED))
    write_svg("fig-r14-evidence-grades", "图 R-14　证据等级制度与本报告的数据构成",
              "规则来自 docs/README.md 与 New_Implementation_Archive.md 的强制记录模板",
              "\n  ".join(parts), 1600, 882)


def chart_challenge() -> None:
    """图 R-15：三个层次的技术挑战与本项目的应对。"""
    items = [
        ("挑战一　语义保真", RED, RED_BG,
         ["优化器把 memcmp 内联成宽整数比较、把常量 strcmp 折叠掉，",
          "同一份源码在不同 -O 级别下 concolic 看到的约束形态完全不同；",
          "位宽、线程局部状态、站点身份任何一处出错都会静默丢约束。"],
         ["编译期稳定 site_id（不依赖地址与编译顺序）",
          "libc 包装器白名单 + bcmp 规范化处理",
          "证明门控的 CFG 变换：proof gate + 未变换基线 replay + denylist"]),
        ("挑战二　求解可扩展", ORANGE, ORANGE_BG,
         ["完整路径约束随深度线性增长，深路径上 Z3 极易超时；",
          "丢前缀能救回可满足性，但候选可能根本走不到目标分支；",
          "多层嵌套下取头 FIFO 会让前沿枯竭，深度上不去。"],
         ["七层求解代价阶梯：先查缓存、再免 SMT 构造、最后才上 Z3",
          "fastSolveConcat：宽整数比较直接反演，连 Z3 都不用",
          "乐观求解只作为 UNSAT 回退，且必须重放验证"]),
        ("挑战三　并行有效性", TEAL, TEAL_BG,
         ["worker 之间会大量重复求解同一条边；",
          "concolic 产出 90% 以上是冗余，盲目回灌只会拖慢 AFL；",
          "短预算下 AFL 根本不会重扫自己的队列，闭环并不成立。"],
         ["四层过滤漏斗：内容摘要 → showmap → B2 预过滤 → B1 全局仲裁",
          "位图 / 哈希空间严格分离，B3 与 B1/B2 不可互相索引",
          "组合配置比较：4 AFL + 3 SymCC + basic profiles 提高离线候选并集覆盖"]),
    ]
    parts = []
    for i, (title, col, bg, probs, sols) in enumerate(items):
        y = 132 + i * 250
        parts.append(rect(48, y, 1504, 226, fill="#FFFFFF", stroke=col, radius=12, stroke_width=2))
        parts.append(rect(48, y, 300, 226, fill=bg, stroke=col, radius=12, stroke_width=2))
        parts.append(text(74, y + 60, title.split("　")[0], size=17, weight=700, fill=col))
        parts.append(text(74, y + 96, title.split("　")[1], size=25, weight=700))
        parts.append(text(384, y + 42, "难在哪", size=14, weight=700, fill=MUTED))
        for j, p in enumerate(probs):
            parts.append(text(384, y + 80 + j * 34, p, size=14))
        parts.append(line(956, y + 24, 956, y + 202, stroke="#E2E8F0"))
        parts.append(text(986, y + 42, "本项目的应对", size=14, weight=700, fill=col))
        for j, s in enumerate(sols):
            parts.append(f'<circle cx="{992}" cy="{y + 75 + j * 34}" r="3.5" fill="{col}"/>')
            parts.append(text(1006, y + 80 + j * 34, s, size=14))
    write_svg("fig-r1-challenges", "图 R-1　三个层次的技术挑战与对应设计",
              "每一行左侧为问题本身的难点，右侧为当前实现采取的具体机制",
              "\n  ".join(parts), 1600, 892)



def chart_timeline() -> None:
    """图 R-16：开发阶段时间线。数据源 docs/Development_History_Traceability.md 第 3 节。"""
    phases = [
        ("P01", "02-26–02-28", "MPI 原型成型", "master/worker 协议、benchmark 骨架、公开目标、coverage/crash 口径", "F17,F21", BLUE, BLUE_BG),
        ("P02", "03-10–03-31", "AFL hybrid 闭环", "LAVA-M / Google FTS、worker bitmap、streaming showmap、双向反馈", "F17,F18,F21", BLUE, BLUE_BG),
        ("P03", "04-01–04-20", "扩展性与负结果", "scaling、dictionary、CmpLog、full benchmark、CPU 争用结论修正", "F18,F20,F21", TEAL, TEAL_BG),
        ("P04", "07-02", "吞吐加固", "persistent / shmem、focus partition、work stealing、分支密度平衡", "F18,F19,F20", TEAL, TEAL_BG),
        ("P05", "07-03–07-13", "可交付性", "质量审计、一条命令安装、离线包、裸机验证", "F25", GREEN, GREEN_BG),
        ("P06", "07-15–07-18", "可观测性", "phase timing、SIGTERM 归因、冗余漏斗、内容预去重、batch showmap", "F22", GREEN, GREEN_BG),
        ("P07", "07-19–07-20", "双引擎", "solver telemetry、ConcolicEngine 契约、SymSan 迁移、RGD / JIGSAW", "F23,F24,F25", ORANGE, ORANGE_BG),
        ("P08", "07-24–07-28", "求解与状态层", "求解复用 / portfolio / PSCache、字符串、schedule、continuation、研究协议", "F00–F187", ORANGE, ORANGE_BG),
        ("P09", "07-28–07-29", "结构化与可信求解", "语法解析森林 / PCFG、QF_BV 三后端可信矩阵、sealed holdout", "F188–F259", RED, RED_BG),
        ("P10", "07-29–07-30", "编译期变换", "continuation 多出口 / 循环闭式 / 内存 tuple、Hydra 对齐、统一 seal", "F260 起", VIOLET, VIOLET_BG),
    ]
    y0, step = 176, 64
    panel_h = 76 + len(phases) * step
    parts = [rect(48, 120, 1504, panel_h, fill="#FFFFFF", stroke=LINE)]
    axis = 132
    parts.append(line(axis, y0 - 18, axis, y0 + (len(phases) - 1) * step + 22,
                      stroke="#CBD5E1", width=3))
    for i, (pid, date, title, detail, fid, col, bg) in enumerate(phases):
        y = y0 + i * step
        parts.append(f'<circle cx="{axis}" cy="{y}" r="9" fill="{bg}" stroke="{col}" stroke-width="3"/>')
        parts.append(text(74, y + 6, pid, size=17, weight=700, fill=col))
        parts.append(text(160, y - 4, date, size=12.5, fill=MUTED))
        parts.append(text(160, y + 16, title, size=16, weight=700))
        parts.append(text(430, y + 10, detail, size=13.5))
        parts.append(rect(1330, y - 13, 190, 26, fill=bg, stroke=col, radius=6, stroke_width=1.2))
        parts.append(text(1425, y + 5, fid, size=12.5, weight=650, fill=col, anchor="middle"))
    yb = 120 + panel_h + 20
    parts.append(rect(48, yb, 1504, 62, fill=PANEL, stroke=LINE, radius=8))
    parts.append(text(72, yb + 26,
                      "P01–P07 解决“能不能并行、能不能交付、能不能观测”；P08–P10 把求解、结构化输入、并发调度与编译期变换补齐为可验证的技术族。",
                      size=13.5))
    parts.append(text(72, yb + 48,
                      "日期为 2026 年；提交区间与逐项功能映射见 docs/Development_History_Traceability.md 第 3 节。P08 起为当前工作树，尚未提交。",
                      size=13, fill=MUTED))
    write_svg("fig-r3-timeline", "图 R-3　开发阶段时间线（P01–P10）",
              "从「给 SymCC 加一个 MPI master」到七层研究栈的实际推进顺序",
              "\n  ".join(parts), 1600, yb + 82)


def chart_sota() -> None:
    """图 R-17：学术来源与本项目落地边界。数据源 Current_Technology_Compendium.md §2.3。"""
    rows = [
        ("QSYM", "USENIX Sec’18", "native 执行、轻量表达式、混合 fuzzing", "Backsolver / data coverage 等扩展不是 QSYM 原功能"),
        ("Pangolin", "IEEE S&P’20", "polyhedral path abstraction、增量复用、受约束采样", "当前为有界子集；跨前缀 / 跨布局不传播 UNSAT"),
        ("CoFuzz", "ICSE’23", "联合观察调度、同步与后续 coverage yield", "当前策略不是 CoFuzz 边级回归的忠实复现"),
        ("GenSym", "ICSE’23", "continuation、状态级并行、持久 store/memory", "只支持保守 LLVM 子集，不是完整 GenSym 编译器"),
        ("PSCache", "FSE’24", "conflict 导出的部分赋值与相关查询复用", "未宣称实现论文全部 bit-blast trail 技术"),
        ("ConDPOR", "CONCUR’25", "输入与 schedule 联合、po/rf/co、backward revisit", "bounded exploration，不声称完备证明"),
        ("SMTgazer", "ASE’25", "删失代价的算法序列与 SMBO 调度", "不是私有模型 / 数据集的复刻"),
        ("UCSan", "OSDI’26", "编译式 under-constrained harness、对象图", "结果仍需真实调用环境验证"),
        ("Hydra", "OOPSLA’26", "目标化控制流变换、fork elision、failure-preserving", "默认关闭，必须 replay / manifest 验证"),
    ]
    parts = [rect(48, 126, 1504, 76 + len(rows) * 62, fill="#FFFFFF", stroke=LINE)]
    parts.append(text(74, 162, "来源", size=13.5, weight=700, fill=MUTED))
    parts.append(text(500, 162, "本项目采用的核心思想", size=13.5, weight=700, fill=GREEN))
    parts.append(text(1000, 162, "明确没有冒充的部分", size=13.5, weight=700, fill=RED))
    parts.append(line(486, 142, 486, 152 + len(rows) * 62, stroke="#E2E8F0"))
    parts.append(line(986, 142, 986, 152 + len(rows) * 62, stroke="#E2E8F0"))
    for i, (name, venue, idea, notdone) in enumerate(rows):
        y = 200 + i * 62
        parts.append(text(74, y + 4, name, size=16, weight=700))
        parts.append(text(74, y + 24, venue, size=12, fill=MUTED))
        parts.append(f'<circle cx="504" cy="{y}" r="3.5" fill="{GREEN}"/>')
        parts.append(text(518, y + 5, idea, size=13.5))
        parts.append(f'<circle cx="1004" cy="{y}" r="3.5" fill="{RED}"/>')
        parts.append(text(1018, y + 5, notdone, size=13.5, fill=MUTED))
        if i < len(rows) - 1:
            parts.append(line(74, y + 36, 1520, y + 36, stroke="#F1F5F9"))
    yb = 132 + 76 + len(rows) * 62
    parts.append(rect(48, yb, 1504, 58, fill=VIOLET_BG, stroke=VIOLET, radius=8))
    parts.append(text(72, yb + 26, "「借鉴某论文」不等于「复现该论文」。", size=16, weight=700, fill=VIOLET))
    parts.append(text(72, yb + 48,
                      "完整 18 行对照表见 Current_Technology_Compendium.md §2.3；逐项差距见 SOTA_Gap_Audit_2026-07-24.md。此处仅列 9 个代表性来源。",
                      size=13, fill=MUTED))
    write_svg("fig-r19-sota-boundary", "图 R-19　学术来源与本项目的落地边界",
              "每一行都同时给出「采用了什么」和「没有冒充什么」——这是本项目对外表述的基本纪律",
              "\n  ".join(parts), 1600, yb + 74)


def chart_switches() -> None:
    """图 R-18：三个求解开关的实测效果。数据源 Architecture_QA3.md §3.3。"""
    groups = [
        ("SYMCC_FAST_SOLVE", GREEN, GREEN_BG, "划算（形状匹配时）", [
            ("xml", "225 → 225", "336 → 245", "−22 %", "91/225 = 40 % 的分支不用 Z3"),
            ("pcre2", "131 → 131", "214 → 200", "≈ 0", "只有 14 个分支命中形状"),
            ("png", "132 → 132", "233 → 230", "−9 %", "几乎不触发，形状不匹配"),
        ]),
        ("SYMCC_OPTIMISTIC_FIRST", RED, RED_BG, "在这几个目标上净亏", [
            ("xml", "225 → 225", "336 → 450", "+33 %", "产出一个没多，查询 +34 %"),
        ]),
        ("SYMCC_MULTI_SOLVE", GREY, GREY_BG, "几乎不产出", [
            ("xml", "225 → 225", "336 → 336", "+7 %", "group-opt 产出 0"),
            ("pcre2", "131 → 132", "214 → 216", "+4 %", "group-opt 产出 1"),
        ]),
    ]
    parts = []
    y = 128
    for name, col, bg, verdict, rows in groups:
        h = 58 + len(rows) * 42
        parts.append(rect(48, y, 1504, h, fill="#FFFFFF", stroke=col, radius=10, stroke_width=2))
        parts.append(rect(48, y, 1504, 40, fill=bg, stroke=col, radius=10, stroke_width=2))
        parts.append(text(74, y + 27, name, size=17, weight=700, fill=col))
        parts.append(text(474, y + 27, "→　" + verdict, size=15, weight=700))
        parts.append(text(880, y + 27, "产出数", size=12.5, fill=MUTED, anchor="middle"))
        parts.append(text(1080, y + 27, "Z3 查询数", size=12.5, fill=MUTED, anchor="middle"))
        parts.append(text(1250, y + 27, "求解耗时", size=12.5, fill=MUTED, anchor="middle"))
        for j, (tgt, out, q, t, note) in enumerate(rows):
            ry = y + 66 + j * 42
            parts.append(text(74, ry, tgt, size=15, weight=650))
            parts.append(text(180, ry, note, size=13, fill=MUTED))
            parts.append(text(880, ry, out, size=14, anchor="middle"))
            parts.append(text(1080, ry, q, size=14, anchor="middle"))
            tc = GREEN if t.startswith("−") else (RED if t.startswith("+") else MUTED)
            parts.append(text(1250, ry, t, size=15, weight=700, fill=tc, anchor="middle"))
        y += h + 18
    parts.append(rect(48, y, 1504, 82, fill=PANEL, stroke=LINE, radius=8))
    parts.append(text(72, y + 26,
                      "读法：三个开关都默认关闭，因为它们的收益强依赖目标形状。FAST_SOLVE 只在「宽整数内联比较」形态上命中；",
                      size=14))
    parts.append(text(72, y + 48,
                      "OPTIMISTIC_FIRST 先跑一次乐观查询，而这几个目标的严格查询本来大多就 SAT，那次查询纯属浪费——只有严格查询大量 UNSAT 时才值得开；",
                      size=14))
    parts.append(text(72, y + 70,
                      "MULTI_SOLVE 针对「源码逐字节比较」形态，而这些目标的比较早被 -O2 合并成宽比较了。【C 级】单种子短测，耗时列取 5 次中位数口径。",
                      size=14, fill=MUTED))
    write_svg("fig-r18-solver-switches", "图 R-18　三个求解开关的实测效果",
              "同一目标、同一种子，只改一个环境变量；产出数与 Z3 查询数为「默认 → 开启后」",
              "\n  ".join(parts), 1600, y + 98)


def chart_lava_ablation() -> None:
    """图 R-21：LAVA-M base64 当前求解技术消融。"""
    rows = [
        ("strict-first-z3", 159.31, 84.31, 2742, 4303, 1.019, 0.34, BLUE, BLUE_BG),
        ("fast-optimistic", 203.31, 86.85, 2552, 5005, 1.317, 0.43, ORANGE, ORANGE_BG),
        ("runtime-full", 212.92, 104.15, 1189, 12500, 1.862, 18.25, RED, RED_BG),
    ]
    parts = [
        rect(48, 124, 1504, 526, fill="#FFFFFF", stroke=LINE, radius=10),
        text(72, 160, "候选规模（每个 seed 均值）", size=16, weight=700),
        text(720, 160, "完整 Z3 路径求解（13 seeds 总数）", size=16, weight=700),
        text(1220, 160, "listed bug 并集", size=16, weight=700),
    ]

    candidate_x0, candidate_x1 = 300, 650
    z3_x0, z3_x1 = 930, 1190
    for i, (name, generated, unique, z3_solves, queries, solver_s, elapsed, col, bg) in enumerate(rows):
        y = 222 + i * 128
        parts.append(text(72, y + 6, name, size=18, weight=700, fill=col))

        generated_width = (candidate_x1 - candidate_x0) * generated / 230.0
        unique_width = (candidate_x1 - candidate_x0) * unique / 230.0
        parts.append(rect(candidate_x0, y - 22, generated_width, 30, fill=bg,
                          stroke=col, radius=5, stroke_width=1.5))
        parts.append(rect(candidate_x0, y + 18, unique_width, 24, fill="#FFFFFF",
                          stroke=col, radius=5, stroke_width=1.5))
        parts.append(text(candidate_x0 + generated_width + 12, y,
                          f"{generated:.2f} candidates", size=13, weight=700,
                          fill=col))
        parts.append(text(candidate_x0 + unique_width + 12, y + 37,
                          f"{unique:.2f} unique", size=12.5, fill=MUTED))

        z3_width = (z3_x1 - z3_x0) * z3_solves / 3000.0
        parts.append(rect(z3_x0, y - 18, z3_width, 34, fill=bg, stroke=col,
                          radius=5, stroke_width=1.5))
        parts.append(text(z3_x0 + z3_width + 12, y + 5, f"{z3_solves}",
                          size=15, weight=700, fill=col))
        parts.append(text(z3_x0, y + 39, f"queries {queries} · solver {solver_s:.3f} s · 墙钟 {elapsed:.2f} s",
                          size=12.5, fill=MUTED))

        parts.append(rect(1230, y - 22, 206, 42, fill=GREEN_BG, stroke=GREEN,
                          radius=8, stroke_width=1.5))
        parts.append(text(1333, y + 7, "41 / 44", size=21, weight=700,
                          fill=GREEN, anchor="middle"))

    parts.append(rect(48, 672, 1504, 194, fill=PANEL, stroke=LINE, radius=10))
    parts.append(text(
        72, 704,
        "相对 strict-first-z3，runtime-full：候选 +33.7%，unique +23.5%，完整路径 Z3 调用 −56.6%。",
        size=17, weight=700, fill=BLUE))
    parts.append(text(
        72, 736,
        "代价：solver queries +190.5%，solver time +82.6%，墙钟 ×53.3，候选吞吐 −97.5%，unique 吞吐 −97.7%。",
        size=14, weight=700, fill=RED))
    parts.append(text(
        72, 768,
        "关键边界：三个 profile 的 listed bug 并集完全相同（41/44）。候选空间扩大没有转化为 LAVA listed bug 增量。",
        size=14, weight=700, fill=RED))
    parts.append(text(
        72, 800,
        "复用命中审计：poly cache hits 0 · cross-prefix hits 0 · prefix context entries/hits 0/0；本轮不能归因于上下文复用。",
        size=13.5, weight=700, fill=ORANGE))
    parts.append(text(
        72, 830,
        "范围：13 个 public seeds × 3 profiles = 39 cases，无 timeout。【C 级机制描述】"
        "13 个测量单元是不同 seed 而非重复运行，未给置信区间；不能用于宣称成本下降或达到 SOTA。",
        size=13.5, fill=MUTED))
    write_svg(
        "fig-r21-lava-ablation",
        "图 R-21　LAVA-M base64：完整 Z3 调用下降，但总求解成本与墙钟上升",
        "当前 LLVM17 二进制；13 个 public seeds；strict-first-z3（原始目录名 strict-z3）/ fast-optimistic / runtime-full",
        "\n  ".join(parts), 1600, 900)



def chart_tech_families() -> None:
    """图 R-13：四个补充技术族的原理速览（正文 §3.9.1–§3.9.4）。"""
    cards = [
        ("结构化输入：在线语法与解析森林", "F36 · F187 · F190–F235", VIOLET, VIOLET_BG,
         ["卡住探索的往往不是分支，而是「输入不合法、解析器早早退出」。",
          "① 从已接纳语料在线学 token grammar，把「某处该是什么」表达成 grammar hole；",
          "② 用 SPPF 保存全部可能解析，PCFG 后验给候选排序；",
          "③ 同时跑 Tree-sitter / Earley / GLR 多个解析器互为 oracle，分歧本身是证据。"],
         ["grammar hole 的补全必须经 Query IR 验证——语法只提结构上",
          "合理的候选，无权断言它满足路径约束。"]),
        ("并发状态空间：输入 × 调度联合探索", "F13 · F39–F65 · F221", TEAL, TEAL_BG,
         ["同一份输入配不同线程交错会走不同路径，于是把 schedule 提升为并列维度。",
          "① 运行时记录内存读写与线程事件，构造 happens-before 与 lockset；",
          "② bounded Source-DPOR / Optimal-DPOR 只枚举本质不同的交错；",
          "③ schedule 导出为 artifact，可与 Query IR 联合重放校验。"],
         ["这是 bounded exploration，不声称完备性证明——ConDPOR",
          "原论文的完备性结论不能直接套用。"]),
        ("Under-Constrained 执行：函数级 harness", "F11 · compiler/UCSan.*", ORANGE, ORANGE_BG,
         ["深层缺陷可能需要几十层前置条件才能从 main 走到。",
          "① 直接从目标函数开始执行；",
          "② 把参数与可达对象图当作符号的、未约束的输入；",
          "③ 编译期自动构造 harness 与 shadow pointer。"],
         ["代价是会产生真实调用环境下不可能出现的输入。",
          "定位为候选来源，结果仍需真实调用环境验证。"]),
        ("字符串与位向量双表示", "F30 · F33 · F193–F200", GREEN, GREEN_BG,
         ["strlen / strcmp / atoi 在纯位向量视角下会展开成极长的约束链。",
          "① 同一个值维护 BV 与 String 两套表示；",
          "② 运行时包装 libc 字符串操作，产出可归档 artifact；",
          "③ atoi / strtol / strtoul 做溢出证明的宽度界定。"],
         ["Z3 与 cvc5 之间做 String 一致性对拍；",
          "并行 portfolio 的结果必须先验证再采纳。"]),
    ]
    parts = []
    for i, (title, fid, col, bg, lines, warn) in enumerate(cards):
        x = 48 + (i % 2) * 764
        y = 128 + (i // 2) * 332
        parts.append(rect(x, y, 740, 304, fill="#FFFFFF", stroke=col, radius=12, stroke_width=2))
        parts.append(rect(x, y, 740, 52, fill=bg, stroke=col, radius=12, stroke_width=2))
        parts.append(text(x + 24, y + 33, title, size=18, weight=700, fill=col))
        parts.append(text(x + 716, y + 33, fid, size=12, fill=MUTED, anchor="end"))
        for j, ln in enumerate(lines):
            parts.append(text(x + 24, y + 84 + j * 30, ln, size=13.5))
        parts.append(rect(x + 20, y + 200, 700, 84, fill=bg, stroke=col, radius=8, stroke_width=1.2))
        parts.append(text(x + 36, y + 226, "边界", size=12.5, weight=700, fill=col))
        for k, wl in enumerate(warn):
            parts.append(text(x + 36, y + 250 + k * 22, wl, size=13))
    write_svg("fig-r13-tech-families", "图 R-13　四个补充技术族的原理速览",
              "它们共同的定位：都是候选来源，正确性一律由独立验证与真实重放守住（见图 R-7）",
              "\n  ".join(parts), 1600, 800)


# ══════════════════════════════ 渲染入口 ══════════════════════════════

# ── DOT 图的视觉规范化 ──────────────────────────────────────────────
# 两类图（Graphviz 结构图 / 自绘定量图）混排时必须看起来是一套：
#   ① 标题排版一致：粗体主标题 + 灰色副标题（自绘图在 svg_document 里已经这样）；
#   ② 显示宽度一致：Graphviz 按内容出图，宽度从 593pt 到 1538pt 不等，
#      在 1180px 正文栏里会一大一小；统一缩放到 1000–1500px 区间。
DOT_SUBTITLES = {
    "fig-r4-modules": "依赖方向单向：benchmark → 包装脚本 / util → runtime；compiler 不知道上面几层的存在",
    "fig-r5-artifacts": "判新永远用 *_afl 这一个二进制，因此换引擎不会让覆盖率口径漂移",
    "fig-r6-layers": "第 ⑦ 层是实现的一部分，而不是实验做完之后补的日志",
    "fig-r7-trust-boundary": "不完备方法可以被大胆引入以提高召回，正确性由独立 validator / replay 守住",
    "fig-r8-mpi-protocol": "拉模式让慢 worker 不积压队列；只回传稀疏边使 MPI 流量与 worker 数无关",
    "fig-r9-engine-abstraction": "两个后端实现原理完全不同，但共享同一套 triage、调度与冗余统计",
    "fig-r10-site-id": "站点身份一旦依赖运行地址，ASLR 会让分支剪枝图 B3 直接失效",
    "fig-r11-solve-cost-layers": "概念顺序；实际按 executor profile、查询形状与缓存命中跳层、交换或并发",
    "fig-r12-query-ir": "求解复用、字符串、语法补洞、schedule 与 holdout campaign 共享同一套内容身份",
    "fig-r20-roadmap": "每一类都标注证据等级；不能宣称的部分单独列出，不与已完成项混排",
}


def dot_title(name: str, body: str) -> str:
    """把 label="图 R-N  标题" 换成 HTML-like 的「粗体标题 + 灰色副标题」。"""
    m = re.search(r'label="(图 R-\d+)\s+([^"]+)";', body)
    if not m:
        return body
    num, title = m.group(1), m.group(2).replace("\\n", " ")
    sub = DOT_SUBTITLES.get(name, "")
    rows = (f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="26"><B>{num}　{title}</B></FONT>'
            f'<BR ALIGN="LEFT"/></TD></TR>')
    if sub:
        rows += (f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="14" COLOR="{MUTED}">{sub}</FONT>'
                 f'<BR ALIGN="LEFT"/></TD></TR>')
    html = ('<<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3">'
            + rows + "</TABLE>>")
    return body.replace(m.group(0), f"label={html};")


def normalize_svg_width(path: Path, lo: int = 1000, hi: int = 1500) -> None:
    """把 Graphviz 出的 SVG 显示宽度夹到 [lo, hi]，等比缩放，viewBox 不动。"""
    txt = path.read_text(encoding="utf-8")
    m = re.search(r'width="(\d+)pt" height="(\d+)pt"', txt)
    if not m:
        return
    w, h = int(m.group(1)), int(m.group(2))
    target = max(lo, min(hi, w))
    nh = round(h * target / w)
    path.write_text(txt.replace(m.group(0), f'width="{target}" height="{nh}"'), encoding="utf-8")


def render_dot_assets() -> None:
    for name, body in GRAPHS.items():
        dot_path = SRC / f"{name}.dot"
        dot_path.write_text(dot_source(dot_title(name, body)), encoding="utf-8")
        subprocess.run(["dot", "-Tsvg", str(dot_path), "-o", str(OUT / f"{name}.svg")], check=True)
        subprocess.run(["dot", "-Tpng", "-Gdpi=170", str(dot_path),
                        "-o", str(OUT / f"{name}.png")], check=True)
        normalize_svg_width(OUT / f"{name}.svg")


def render_custom_pngs() -> None:
    chrome = os.environ.get("CHROME", "/usr/bin/google-chrome")
    for path in sorted(OUT.glob("*.svg")):
        if (SRC / f"{path.stem}.dot").exists():
            continue
        svg_path = path.resolve()
        png_path = (OUT / f"{path.stem}.png").resolve()
        dims = re.search(r'<svg[^>]+width="(\d+)"[^>]+height="(\d+)"',
                         svg_path.read_text(encoding="utf-8"))
        if dims is None:
            raise RuntimeError(f"无法确定 SVG 尺寸: {svg_path}")
        width, height = dims.groups()
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--force-device-scale-factor=1",
             f"--window-size={width},{int(height) + 120}",
             f"--screenshot={png_path}", svg_path.as_uri()],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)
    render_dot_assets()
    chart_headline_forest()
    chart_eval_groups()
    chart_symsan_targets()
    chart_scale()
    chart_evidence_grades()
    chart_challenge()
    chart_timeline()
    chart_sota()
    chart_switches()
    chart_lava_ablation()
    chart_tech_families()
    render_custom_pngs()
    print(f"已渲染 {len(list(OUT.glob('*.svg')))} 张 SVG / {len(list(OUT.glob('*.png')))} 张 PNG")


if __name__ == "__main__":
    main()
