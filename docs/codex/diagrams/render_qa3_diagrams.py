#!/usr/bin/env python3
"""Render the Architecture_QA3 figures as reproducible SVG and PNG assets."""

from __future__ import annotations

import html
import os
import re
import subprocess
from pathlib import Path


HERE = Path(__file__).resolve().parent
OUT = HERE / "qa3"
SRC = OUT / "src"
FONT = "WenQuanYi Zen Hei"

INK = "#17212B"
MUTED = "#64748B"
LINE = "#CBD5E1"
PANEL = "#F8FAFC"
BLUE = "#2563EB"
BLUE_BG = "#DBEAFE"
TEAL = "#0F766E"
TEAL_BG = "#CCFBF1"
GREEN = "#15803D"
GREEN_BG = "#DCFCE7"
ORANGE = "#C2410C"
ORANGE_BG = "#FFEDD5"
RED = "#B91C1C"
RED_BG = "#FEE2E2"
VIOLET = "#6D28D9"
VIOLET_BG = "#EDE9FE"


DOT_HEADER = f"""
digraph G {{
  graph [
    bgcolor="white", pad="0.28", nodesep="0.38", ranksep="0.58",
    fontname="{FONT}", fontcolor="{INK}", fontsize=26,
    labelloc="t", labeljust="l"
  ];
  node [
    shape=box, style="rounded,filled", margin="0.18,0.12",
    fontname="{FONT}", fontcolor="{INK}", fontsize=13,
    color="{LINE}", penwidth=1.3, fillcolor="{PANEL}"
  ];
  edge [
    fontname="{FONT}", fontcolor="{MUTED}", fontsize=11,
    color="{MUTED}", penwidth=1.5, arrowsize=0.75
  ];
"""


GRAPHS = {
    "fig-1-0-execution-modes": r"""
  label="图 1-0  Native concolic 与 Continuation/CAS 两条执行流";
  rankdir=TB;

  seed [label="输入种子", fillcolor="#FEF3C7", color="#D97706"];

  subgraph cluster_native {
    label="A. 经典 native concolic（默认）";
    color="#5EEAD4"; style="rounded,filled"; fillcolor="#F0FDFA";
    tag [label="TAG_WORK\n传输内容对象", fillcolor="#CCFBF1", color="#0F766E"];
    proc [label="新起 SymCC/SymSan\n目标进程", fillcolor="#FFEDD5", color="#C2410C"];
    b3n [label="B3 分支兴趣过滤\n严格/乐观求解", fillcolor="#FFEDD5", color="#C2410C"];
    shown [label="AFL showmap\nB2 本地判新", fillcolor="#DBEAFE", color="#2563EB"];
    tag -> proc -> b3n -> shown;
  }

  subgraph cluster_live {
    label="B. Continuation / CAS 状态级执行（opt-in）";
    color="#C4B5FD"; style="rounded,filled"; fillcolor="#F5F3FF";
    cas [label="初始 CAS checkpoint\nfenced state lease", fillcolor="#EDE9FE", color="#6D28D9"];
    restore [label="Worker 恢复 continuation\n有界执行与分叉", fillcolor="#EDE9FE", color="#6D28D9"];
    frontier [label="halted states +\nresumable frontier checkpoints", fillcolor="#EDE9FE", color="#6D28D9"];
    commit [label="Master 校验 fence\ncommit 并重新入队", fillcolor="#EDE9FE", color="#6D28D9"];
    cas -> restore -> frontier -> commit;
    commit -> restore [constraint=false, style=dashed, color="#6D28D9", label="下一 lease"];
  }

  triage [label="共同出口\n目标重放 → AFL bitmap triage → B1", fillcolor="#DBEAFE", color="#2563EB", penwidth=2.4];
  queue [label="被接受候选\n写入 helper / AFL 同步目录", fillcolor="#DCFCE7", color="#15803D"];
  seed -> tag [label="默认"];
  seed -> cas [label="SYMCC_LIVE_*", color="#6D28D9"];
  shown -> triage [label="TAG_RESULT"];
  commit -> triage [label="SAT 输入候选", color="#6D28D9"];
  triage -> queue;
  invariant [shape=note, label="共同不变量\nContinuation 候选也不能绕过\n目标重放和 B1 新颖性仲裁", fillcolor="#FEE2E2", color="#B91C1C"];
  triage -> invariant [style=dotted, arrowhead=none];
""",
    "fig-1-1-process-topology": r"""
  label="图 1-1  SymCC-Parallel 进程拓扑与共享边界";
  rankdir=LR;

  launcher [label="benchmark/run_benchmark.py\nrun_hybrid()", fillcolor="#FEF3C7", color="#D97706"];

  subgraph cluster_afl {
    label="AFL++ 实例群（独立进程，非 MPI）";
    color="#93C5FD"; style="rounded,filled"; fillcolor="#EFF6FF";
    afl_m [label="fuzzer01\n-M · CmpLog", fillcolor="#DBEAFE", color="#2563EB"];
    afl_s [label="fuzzer02…N\n-S · 多调度 profile", fillcolor="#DBEAFE", color="#2563EB"];
  }

  subgraph cluster_mpi {
    label="MPI 通信域（mpirun -np N）";
    color="#5EEAD4"; style="rounded,filled"; fillcolor="#F0FDFA";
    master [label="rank 0\nMaster\n调度 · B1 仲裁", fillcolor="#CCFBF1", color="#0F766E"];
    workers [label="rank 1…N-1\nConcolic workers\nB2 / B3 · 求解", fillcolor="#CCFBF1", color="#0F766E"];
    master -> workers [dir=both, label="TAG_READY / WORK /\nRESULT / STOP", color="#0F766E"];
  }

  subgraph cluster_fs {
    label="共享输出目录 afl_out/";
    color="#FDBA74"; style="rounded,filled"; fillcolor="#FFF7ED";
    fqueue [label="fuzzer01/queue\nAFL 种子来源", fillcolor="#FFEDD5", color="#C2410C"];
    squeue [label="symcc01/queue\n被接受的 concolic 产出", fillcolor="#FFEDD5", color="#C2410C"];
    extras [label="symcc01/extras\nhint token 字典", fillcolor="#FFEDD5", color="#C2410C"];
  }

  launcher -> afl_m [label="先启动"];
  launcher -> master [label="等待 fuzzer_stats 后启动"];
  afl_m -> fqueue [dir=both, label="生成 / 读取"];
  afl_s -> fqueue [style=dashed, label="AFL sync"];
  master -> fqueue [dir=both, label="扫描取种子 / 直写产出\n（运行中 AFL 不重扫）"];
  master -> squeue [label="原子写入"];
  master -> extras [label="写 hint"];
  squeue -> afl_m [style=dashed, color="#B91C1C", fontcolor="#B91C1C",
                   label="默认 10 min 后兄弟同步\n短轮次内未送达"];
  note [shape=note, label="核数分配\nSymCC worker 上限 12\n其余核分给 AFL", fillcolor="#F1F5F9"];
  launcher -> note [style=dotted, arrowhead=none];
""",
    "fig-1-2-bitmap-spaces": r"""
  label="图 1-2  位图与哈希空间：生成者、消费者和传播方向";
  rankdir=TB;

  subgraph cluster_afl_space {
    label="AFL edge / feature ID 空间";
    color="#93C5FD"; style="rounded,filled"; fillcolor="#EFF6FF";
    afl [label="AFL 目标执行\nPCGUARD edge", fillcolor="#DBEAFE", color="#2563EB"];
    b5 [label="B5  AFL 共享 shm\n数据进展 feature\nAFL_PRELOAD 写入", fillcolor="#DBEAFE", color="#2563EB"];
    showmap [label="afl-showmap\n运行 AFL 插桩二进制\n产生稀疏边列表", fillcolor="#DBEAFE", color="#2563EB"];
    b2 [label="B2  worker_cov\nworker 私有内存\n候选预过滤", fillcolor="#CCFBF1", color="#0F766E"];
    b1 [label="B1  master coverage\n会话内存 · 最终裁决", fillcolor="#CCFBF1", color="#0F766E"];
    b4 [label="B4  owner-shard gossip\n跨 master JSON 分片\nopt-in", fillcolor="#EDE9FE", color="#6D28D9"];
    afl -> b5 [dir=both, label="同一 shm"];
    showmap -> b2 [label="edge list"];
    b2 -> b1 [label="TAG_RESULT\n稀疏 delta"];
    b1 -> b2 [label="版本 + delta / snapshot", color="#0F766E"];
    b1 -> b4 [dir=both, style=dashed, label="跨 master 仲裁"];
  }

  subgraph cluster_qsym_space {
    label="QSYM 独立哈希空间";
    color="#FDBA74"; style="rounded,filled"; fillcolor="#FFF7ED";
    branch [label="符号分支\nsite_id + taken", fillcolor="#FFEDD5", color="#C2410C"];
    b3 [label="B3  qsym_bitmap\n每 worker 私有文件 · 131072 B\ntrace 64 KB + context 64 KB", fillcolor="#FFEDD5", color="#C2410C"];
    branch -> b3 [dir=both, label="XXH32 + prev_loc\n兴趣判断 / commit"];
  }

  content [label="内容哈希（非位图）\nblake2b 字节级预去重", fillcolor="#F1F5F9"];
  b6 [label="B6  grammar bitmap\n结构化提议新颖性\nadaptive 路径", fillcolor="#DCFCE7", color="#15803D"];
  candidate [label="concolic 候选输入", fillcolor="#FEF3C7", color="#D97706"];
  candidate -> branch [label="执行时"];
  candidate -> content [label="产出后"];
  content -> showmap [label="内容新颖才重放"];
  candidate -> b6 [style=dashed, label="语法提议时"];

  barrier [shape=note, label="严格隔离\nB3 不读写 B1/B2\n索引空间不可互换", fillcolor="#FEE2E2", color="#B91C1C"];
  b3 -> barrier [style=dotted, arrowhead=none, color="#B91C1C"];
  barrier -> b2 [style=dotted, arrowhead=none, color="#B91C1C"];
""",
    "fig-1-3-worker-sequence": r"""
  label="图 1-3  单个工作项的跨进程执行次序";
  rankdir=TB;

  n1 [label="① Worker → Master\nTAG_READY {rank, bitmap_version}", fillcolor="#CCFBF1", color="#0F766E"];
  n2 [label="② Master\n扫描 AFL queue、打分与选种", fillcolor="#DBEAFE", color="#2563EB"];
  n3 [label="③ Master → Worker\nTAG_WORK：内容对象、策略、B1 delta", fillcolor="#CCFBF1", color="#0F766E"];
  n4 [label="④ Worker\nbmsync 合并 B1→B2；import 落地输入", fillcolor="#CCFBF1", color="#0F766E"];
  n5 [label="⑤ Concolic 子进程\n执行目标；B3 过滤分支；Z3/fast 求解；写候选", fillcolor="#FFEDD5", color="#C2410C"];
  n6 [label="⑥ Worker\nblake2b 内容预去重", fillcolor="#F1F5F9"];
  n7 [label="⑦ afl-showmap\n批量重放 AFL 插桩二进制，返回稀疏边", fillcolor="#DBEAFE", color="#2563EB"];
  n8 [label="⑧ Worker\nB2 合并判新，仅保留本地新覆盖", fillcolor="#CCFBF1", color="#0F766E"];
  n9 [label="⑨ Worker → Master\nTAG_RESULT：new_tests、hints、telemetry", fillcolor="#CCFBF1", color="#0F766E"];
  n10 [label="⑩ Master\nB1 最终裁决；原子写 queue / extras；回压反馈队列", fillcolor="#DBEAFE", color="#2563EB"];
  n11 [label="AFL 导入\n兄弟目录默认等待 10 min\n短 benchmark 未到达", fillcolor="#FEE2E2", color="#B91C1C"];
  n12 [label="反馈迭代\naccepted input 代数 +1，再进入调度", fillcolor="#DCFCE7", color="#15803D"];

  {rank=same; n1; n2; n3; n4;}
  {rank=same; n5; n6; n7; n8;}
  {rank=same; n9; n10;}
  n1 -> n2 -> n3 -> n4 -> n5 -> n6 -> n7 -> n8 -> n9 -> n10;
  n10 -> n11 [label="解的交付", color="#B91C1C"];
  n10 -> n12 [label="helper 内闭环", color="#15803D"];
  n12 -> n2 [constraint=false, style=dashed, color="#15803D"];

  early [shape=note, label="continuation_id 提前分支\n恢复 checkpoint → 有界推进 → TAG_RESULT\n跳过 native 步骤 ④–⑧", fillcolor="#EDE9FE", color="#6D28D9"];
  n3 -> early [style=dashed, color="#6D28D9"];
  early -> n10 [style=dashed, color="#6D28D9"];
""",
    "fig-1-6-afl-delivery": r"""
  label="图 1-6  Concolic 产出到 AFL 的三条交付路径";
  rankdir=TB;

  accepted [label="Master B1 接受\n一个 concolic 产出", fillcolor="#CCFBF1", color="#0F766E"];
  sibling [label="(a) symcc01/queue\n兄弟实例同步源", fillcolor="#DBEAFE", color="#2563EB"];
  direct [label="(b) fuzzer01/queue\n直接写运行中实例目录", fillcolor="#DBEAFE", color="#2563EB"];
  hint [label="(c) symcc01/extras\nhint token", fillcolor="#DBEAFE", color="#2563EB"];
  delay [label="同步门\n-M 默认约 10 min", fillcolor="#FEE2E2", color="#B91C1C"];
  noscan [label="运行中 AFL\n不重扫自己的 queue", fillcolor="#FEE2E2", color="#B91C1C"];
  afl [label="AFL 内部 corpus / dictionary", fillcolor="#DCFCE7", color="#15803D"];

  accepted -> sibling [label="写解"];
  accepted -> direct [label="写解"];
  accepted -> hint [label="写 token"];
  sibling -> delay -> afl [label="短轮次未到", color="#B91C1C"];
  direct -> noscan -> afl [label="0/200 被采纳", color="#B91C1C"];
  hint -> afl [label="可用", color="#15803D", penwidth=2.4];

  conclusion [shape=note, label="≤300 s 的历史 benchmark\n只有字典 token 到达 AFL；\n解本身仅进入离线并集语料", fillcolor="#FEF3C7", color="#D97706"];
  afl -> conclusion [style=dotted, arrowhead=none];
  fix [shape=note, label="修复方向\nAFL_SYNC_TIME=1\n或将 symcc01 接入 -F foreign_dirs", fillcolor="#F1F5F9"];
  conclusion -> fix [style=dashed];
""",
    "fig-2-1-constraint-shapes": r"""
  label="图 2-1  同一比较在不同编译形态下形成的符号约束";
  rankdir=TB;

  src [label="源码语义\nmemcmp(buf, \"SYMC\", 4) == 0", fillcolor="#FEF3C7", color="#D97706"];
  b [label="-O0 / -fno-builtin\ncall 指令幸存", fillcolor="#DBEAFE", color="#2563EB"];
  a [label="常见 -O2\n内联、bswap、宽 icmp", fillcolor="#CCFBF1", color="#0F766E"];
  c [label="源码逐字节循环\nN 个独立分支", fillcolor="#FFEDD5", color="#C2410C"];
  sb [label="形态 B · libc 包装器\nn 个 Equal + And 链", fillcolor="#DBEAFE", color="#2563EB"];
  sa [label="形态 A · 宽整数比较\nEqual(Concat, Constant)", fillcolor="#CCFBF1", color="#0F766E"];
  sc [label="形态 C · 单字节约束\nN 条 icmp", fillcolor="#FFEDD5", color="#C2410C"];
  ob [label="否定后任一字节不同即可\n不自然形成逐前缀进展", fillcolor="#F1F5F9"];
  oa [label="纯 2–8 B 常量比较\nfastSolveConcat 可绕过 Z3", fillcolor="#DCFCE7", color="#15803D"];
  oc [label="依赖反馈迭代\nMULTI_SOLVE 可联合翻转", fillcolor="#F1F5F9"];

  src -> {b a c};
  b -> sb -> ob;
  a -> sa -> oa;
  c -> sc -> oc;
""",
    "fig-2-2-concat-expression": r"""
  label="图 2-2  8 字节 little-endian load 的 Concat 表达式树";
  rankdir=TB;

  cmp [label="Equal(Concat(Read7…Read0), 0xaaaabbbbccccdddd)", fillcolor="#FEF3C7", color="#D97706"];
  c7 [label="Concat", fillcolor="#CCFBF1", color="#0F766E"];
  r7 [label="Read(7)\n输入偏移 7", fillcolor="#DBEAFE", color="#2563EB"];
  c6 [label="Concat", fillcolor="#CCFBF1", color="#0F766E"];
  r6 [label="Read(6)", fillcolor="#DBEAFE", color="#2563EB"];
  c5 [label="Concat", fillcolor="#CCFBF1", color="#0F766E"];
  r5 [label="Read(5)", fillcolor="#DBEAFE", color="#2563EB"];
  rest [label="… 继续右倾 …\nRead(4)…Read(0)", fillcolor="#F1F5F9"];
  z3 [label="一次 check-sat\n可同时确定 8 个符号字节", fillcolor="#DCFCE7", color="#15803D"];
  risk [shape=note, label="N 字节读形成深度 O(N) 的右倾链\n大 N 会放大递归遍历成本", fillcolor="#FEE2E2", color="#B91C1C"];

  cmp -> c7;
  c7 -> r7; c7 -> c6;
  c6 -> r6; c6 -> c5;
  c5 -> r5; c5 -> rest;
  cmp -> z3 [style=dashed, color="#15803D"];
  rest -> risk [style=dashed, color="#B91C1C"];
""",
    "fig-2-3-binary-variants": r"""
  label="图 2-3  同一源码的二进制变体与技术作用域";
  rankdir=LR;

  src [label="同一份目标源码", fillcolor="#FEF3C7", color="#D97706"];
  concolic [label="*_symcc / *_symsan\nsymcc -O2 / ko-clang -O1", fillcolor="#CCFBF1", color="#0F766E"];
  afl [label="*_afl\nAFL PCGUARD", fillcolor="#DBEAFE", color="#2563EB"];
  laf [label="*_afl_laf\nAFL_LLVM_LAF_ALL=1", fillcolor="#DBEAFE", color="#2563EB"];
  ctx [label="*_afl_ctx / *_ngram4\nCTX / NGRAM", fillcolor="#DBEAFE", color="#2563EB"];
  cmplog [label="*-cmplog\nAFL_LLVM_CMPLOG=1", fillcolor="#DBEAFE", color="#2563EB"];
  cov [label="*-cov\ngcc --coverage", fillcolor="#F1F5F9"];
  solver [label="SymCC/SymSan 约束形态\n由该二进制的 LLVM 优化结果决定", fillcolor="#CCFBF1", color="#0F766E"];
  fuzzer [label="AFL 反馈与比较引导\n随机变异侧", fillcolor="#DBEAFE", color="#2563EB"];
  measure [label="离线覆盖率测量", fillcolor="#F1F5F9"];

  src -> {concolic afl laf ctx cmplog cov};
  concolic -> solver [penwidth=2.4, color="#0F766E"];
  {afl laf ctx cmplog} -> fuzzer [color="#2563EB"];
  cov -> measure;
  orth [shape=note, label="正交关系\nLAF / CmpLog / CTX / NGRAM\n不改变 concolic 看到的约束", fillcolor="#FEE2E2", color="#B91C1C"];
  fuzzer -> orth [style=dotted, arrowhead=none];
  solver -> orth [style=dotted, arrowhead=none];
""",
    "fig-2-4-build-test-pipelines": r"""
  label="图 2-4  真实 benchmark 与 lit 自测试的执行管线";
  rankdir=TB;

  subgraph cluster_prod {
    label="真实目标 / benchmark";
    color="#5EEAD4"; style="rounded,filled"; fillcolor="#F0FDFA";
    psrc [label="源码", fillcolor="#FEF3C7", color="#D97706"];
    psym [label="symcc / ko-clang\n-O2 / -O1", fillcolor="#CCFBF1", color="#0F766E"];
    pafl [label="afl-clang-fast\nPCGUARD / CmpLog", fillcolor="#DBEAFE", color="#2563EB"];
    pexec [label="afl-fuzz + MPI helper\n真实输入、真实位图", fillcolor="#DCFCE7", color="#15803D"];
    psrc -> psym -> pexec;
    psrc -> pafl -> pexec;
  }

  subgraph cluster_test {
    label="lit / unittest";
    color="#FDBA74"; style="rounded,filled"; fillcolor="#FFF7ED";
    tsrc [label=".c / .ll / .py", fillcolor="#FEF3C7", color="#D97706"];
    tbuild [label="%symcc 或 %opt\n常见 -O0；可强制 -fno-builtin", fillcolor="#FFEDD5", color="#C2410C"];
    tcheck [label="单次运行\nFileCheck / telemetry / Python 断言", fillcolor="#DCFCE7", color="#15803D"];
    tsrc -> tbuild -> tcheck;
  }

  prod [shape=note, label="回答：真实优化与 campaign 中是否触发、是否增益", fillcolor="#F1F5F9"];
  test [shape=note, label="回答：机制是否存在、表达式与产出是否正确", fillcolor="#F1F5F9"];
  pexec -> prod;
  tcheck -> test;
  warning [shape=note, label="两条管线可能走不同约束形态\n自测试通过 ≠ -O2 目标必然触发", fillcolor="#FEE2E2", color="#B91C1C"];
  prod -> warning [style=dotted, arrowhead=none];
  test -> warning [style=dotted, arrowhead=none];
""",
    "fig-3-1-solve-decision": r"""
  label="图 3-1  negatePath 求解决策与回退路径";
  rankdir=TB; ranksep=0.62; nodesep=0.42;

  branch [label="符号分支 e + 具体方向 taken", fillcolor="#FEF3C7", color="#D97706"];
  interesting [shape=diamond, label="B3 判定\ninteresting?", fillcolor="#FFEDD5", color="#C2410C"];
  skip [label="不求解", fillcolor="#F1F5F9"];
  gates [label="可选前置路径\nspool → fastSolve → poly/core cache\noptimistic-first", fillcolor="#EDE9FE", color="#6D28D9"];
  strictsolve [label="默认：严格求解\nreset + 依赖切片 + ¬branch\nZ3 timeout 10 s", fillcolor="#CCFBF1", color="#0F766E", penwidth=2.4];
  strictsat [shape=diamond, label="strict SAT?", fillcolor="#CCFBF1", color="#0F766E"];
  nominal [label="保存 nominal\n无策略后缀", fillcolor="#DCFCE7", color="#15803D"];
  ite [shape=diamond, label="目标表达式\n包含 Ite?", fillcolor="#FFEDD5", color="#C2410C"];
  back [label="tryBacksolve\n具体枚举优先；失败后\n仅丢 ITE 控制字节相关前缀", fillcolor="#FFEDD5", color="#C2410C"];
  optimistic [label="乐观回退\nreset 后只保留 ¬branch\n保存 -optimistic", fillcolor="#FEE2E2", color="#B91C1C"];
  replay [label="目标重放 + AFL bitmap triage\n过滤不可达或无新覆盖候选", fillcolor="#DBEAFE", color="#2563EB"];

  {rank=same; branch; interesting;}
  {rank=same; skip; gates; strictsolve;}
  {rank=same; nominal; strictsat; ite;}
  {rank=same; back; optimistic;}
  branch -> interesting;
  interesting -> skip [label="否"];
  interesting -> gates [label="是"];
  gates -> strictsolve [label="默认开关均关闭时"];
  strictsolve -> strictsat;
  strictsat -> nominal [label="是", color="#15803D"];
  strictsat -> ite [label="否 / unknown", color="#B91C1C"];
  ite -> back [label="是"];
  ite -> optimistic [label="否"];
  {nominal back optimistic} -> replay;
""",
    "fig-3-2-prefix-dropping": r"""
  label="图 3-2  前缀约束保留粒度与可达性风险";
  rankdir=TB;

  prefix [label="目标相关路径切片\nC1 ∧ C2 ∧ C3 ∧ C4 ∧ C5 ∧ ¬branch", fillcolor="#FEF3C7", color="#D97706"];
  slice [label="依赖切片 / nominal\n保留所有与目标共享输入的约束\nC1 C2 C3 C4 C5 + ¬branch", fillcolor="#DCFCE7", color="#15803D"];
  back [label="backsolve Z3 回落\n仅去掉与 ITE 控制变量重叠的约束\nC1 · C3 · C5 + ¬branch", fillcolor="#FFEDD5", color="#C2410C"];
  opt [label="optimistic\n清空全部前缀\n仅 ¬branch", fillcolor="#FEE2E2", color="#B91C1C"];
  valid1 [label="满足已加载路径切片\n可达性保证最强", fillcolor="#DCFCE7", color="#15803D"];
  valid2 [label="重验目标与保留前缀\n仍有部分不可达风险", fillcolor="#FFEDD5", color="#C2410C"];
  valid3 [label="模型可能到不了目标分支\n依赖具体重放与 showmap 筛除", fillcolor="#FEE2E2", color="#B91C1C"];

  prefix -> {slice back opt};
  slice -> valid1;
  back -> valid2;
  opt -> valid3;
  nonexistent [shape=note, label="实现中不存在\n“只保留最后 N 条”的窗口式策略", fillcolor="#F1F5F9"];
  prefix -> nonexistent [style=dashed];
""",
    "fig-4-1-path-neighborhood": r"""
  label="图 4-1  一次动态符号执行只覆盖一条路径及其一步邻居";
  rankdir=TB;

  e [label="entry", shape=circle, fixedsize=true, width=0.62, fillcolor="#FEF3C7", color="#D97706"];
  p1 [label="已走分支", fillcolor="#CCFBF1", color="#0F766E"];
  n1 [label="一步邻居\n可由 negatePath 生成", fillcolor="#DBEAFE", color="#2563EB"];
  p2 [label="已走分支", fillcolor="#CCFBF1", color="#0F766E"];
  n2 [label="一步邻居", fillcolor="#DBEAFE", color="#2563EB"];
  end [label="本次执行终点", fillcolor="#CCFBF1", color="#0F766E"];
  deep1 [label="更深路径\n本次不可见", style="rounded,dashed,filled", fillcolor="#F1F5F9"];
  deep2 [label="更深路径\n需新种子再执行", style="rounded,dashed,filled", fillcolor="#F1F5F9"];
  loop [label="外层反馈循环\n候选 → 新种子 → 再执行\n逐代增加深度", fillcolor="#DCFCE7", color="#15803D"];

  e -> p1 [color="#0F766E", penwidth=2.6];
  e -> n1 [color="#2563EB"];
  p1 -> p2 [color="#0F766E", penwidth=2.6];
  p1 -> n2 [color="#2563EB"];
  p2 -> end [color="#0F766E", penwidth=2.6];
  end -> deep1 [style=dashed];
  end -> deep2 [style=dashed];
  n1 -> loop [style=dashed, color="#15803D"];
  n2 -> loop [style=dashed, color="#15803D"];
  loop -> e [constraint=false, style=dashed, color="#15803D"];
""",
    "fig-4-2-seed-length": r"""
  label="图 4-2  种子长度决定可见符号变量与可达深度";
  rankdir=TB;

  target [label="目标链\nSYMC → n≥8 → buf[4]=3 → checksum=0x42", fillcolor="#FEF3C7", color="#D97706"];
  long [label="8 字节种子 AAAAAAAA\n偏移 0…7 均可被读取和符号化", fillcolor="#CCFBF1", color="#0F766E"];
  short [label="4 字节种子 AAAA\n只有偏移 0…3 存在", fillcolor="#FFEDD5", color="#C2410C"];
  l1 [label="gen 0→4\nA… → S… → SY… → SYM… → SYMC…", fillcolor="#DBEAFE", color="#2563EB"];
  l2 [label="gen 5\n求解 buf[4] = 3", fillcolor="#DBEAFE", color="#2563EB"];
  l3 [label="gen 6\n一次 Z3 求解 3 字节 checksum\n命中深层代码", fillcolor="#DCFCE7", color="#15803D"];
  s1 [label="gen 0→4\n同样得到 SYMC", fillcolor="#DBEAFE", color="#2563EB"];
  eof [label="n = read() 返回值 = 4\n具体值，不是符号分支", fillcolor="#FEE2E2", color="#B91C1C"];
  stuck [label="不动点\n求解器不能创造未建模字节", fillcolor="#FEE2E2", color="#B91C1C"];

  target -> {long short};
  long -> l1 -> l2 -> l3;
  short -> s1 -> eof -> stuck;
""",
}


def dot_source(body: str) -> str:
    return DOT_HEADER + body + "\n}\n"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def text(x: float, y: float, value: str, *, size: int = 18, weight: int = 400,
         fill: str = INK, anchor: str = "start") -> str:
    return (
        f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{esc(value)}</text>'
    )


def rect(x: float, y: float, width: float, height: float, *, fill: str = PANEL,
         stroke: str = LINE, radius: int = 8, stroke_width: float = 1.5) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>'
    )


def line(x1: float, y1: float, x2: float, y2: float, *, stroke: str = LINE,
         width: float = 1.5, dash: str | None = None) -> str:
    extra = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
        f'stroke="{stroke}" stroke-width="{width}"{extra}/>'
    )


def svg_document(title: str, subtitle: str, body: str, width: int, height: int) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
     viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
  <title id="title">{esc(title)}</title>
  <desc id="desc">{esc(subtitle)}</desc>
  <rect width="{width}" height="{height}" fill="white"/>
  {text(48, 54, title, size=28, weight=700)}
  {text(48, 86, subtitle, size=15, fill=MUTED)}
  {body}
</svg>
"""


def write_svg(name: str, title: str, subtitle: str, body: str,
              width: int = 1600, height: int = 900) -> None:
    (OUT / f"{name}.svg").write_text(
        svg_document(title, subtitle, body, width, height), encoding="utf-8"
    )


def chart_filter_funnel() -> None:
    stages = [
        ("Concolic 原始产出", 3154, "100.0%", BLUE, BLUE_BG),
        ("内容级 blake2b 去重后", 2056, "65.2%", TEAL, TEAL_BG),
        ("Worker B2 判新后", 299, "9.5%", ORANGE, ORANGE_BG),
        ("Master B1 最终接受", 167, "5.3%", GREEN, GREEN_BG),
    ]
    parts = []
    center = 800
    max_width = 1250
    y = 145
    for index, (label, value, pct, color, bg) in enumerate(stages):
        width = max(260, max_width * value / 3154)
        x = center - width / 2
        parts.append(rect(x, y, width, 104, fill=bg, stroke=color, radius=8, stroke_width=2))
        parts.append(text(center, y + 42, label, size=19, weight=700, anchor="middle"))
        parts.append(text(center, y + 76, f"{value:,}  ·  {pct}", size=18, fill=color, anchor="middle"))
        if index < len(stages) - 1:
            removed = value - stages[index + 1][1]
            parts.append(line(center, y + 104, center, y + 142, stroke=MUTED, width=2))
            parts.append(text(center + 18, y + 128, f"过滤 {removed:,}", size=14, fill=MUTED))
        y += 150
    parts.append(rect(190, 758, 1220, 84, fill="#FEF3C7", stroke="#D97706"))
    parts.append(text(800, 792, "B3 在产出前过滤分支，不计入 3,154；AFL 最终 virgin-state 导入在本轮未发生。", size=16, anchor="middle"))
    parts.append(text(800, 820, "反馈链达到 max_depth = 3，说明 helper 内部的候选回喂确实发生。", size=16, fill=MUTED, anchor="middle"))
    write_svg(
        "fig-1-4-filter-funnel",
        "图 1-4  四层过滤漏斗",
        "XML · 8 workers · 322 个工作项 · 180 s；面积按候选数缩放",
        "".join(parts),
        height=900,
    )


def chart_phase_time() -> None:
    worker = [
        ("send", 64.96, 39.1, BLUE),
        ("exec", 52.82, 31.8, TEAL),
        ("wait", 43.09, 25.9, ORANGE),
        ("showmap_dedup", 4.54, 2.7, VIOLET),
        ("import", 0.69, 0.4, GREEN),
        ("bmsync", 0.01, 0.0, RED),
    ]
    master = [
        ("scan", 83.60, BLUE),
        ("idle", 41.32, ORANGE),
        ("triage", 4.65, TEAL),
        ("dispatch", 2.41, VIOLET),
        ("recv", 0.14, GREEN),
    ]
    parts = [text(65, 135, "Worker 已记账活跃相位（166 s）", size=20, weight=700)]
    y = 175
    for name, sec, pct, color in worker:
        parts.append(text(65, y + 25, name, size=16, weight=600))
        parts.append(rect(235, y, 910, 34, fill="#F1F5F9", stroke="#E2E8F0", radius=4))
        parts.append(rect(235, y, max(3, 910 * pct / 40), 34, fill=color, stroke=color, radius=4))
        parts.append(text(1170, y + 24, f"{sec:.2f} s  ·  {pct:.1f}%", size=15))
        y += 58
    parts.append(text(65, 560, "Master 已测相位（180 s）", size=20, weight=700))
    y = 600
    max_sec = max(item[1] for item in master)
    for name, sec, color in master:
        parts.append(text(65, y + 25, name, size=16, weight=600))
        parts.append(rect(235, y, 910, 34, fill="#F1F5F9", stroke="#E2E8F0", radius=4))
        parts.append(rect(235, y, max(3, 910 * sec / max_sec), 34, fill=color, stroke=color, radius=4))
        parts.append(text(1170, y + 24, f"{sec:.2f} s", size=15))
        y += 50
    parts.append(rect(1220, 160, 320, 620, fill=PANEL, stroke=LINE))
    parts.append(text(1380, 205, "解释边界", size=19, weight=700, anchor="middle"))
    notes = [
        ("exec", "最大计算项", TEAL),
        ("send", "最大已记账相位；阻塞等待 master", BLUE),
        ("bmsync", "约 0.02 ms/项，可忽略", RED),
        ("scan", "master 最大候选瓶颈", ORANGE),
        ("记账率", "仅约 12%，不能外推完整 CPU 构成", VIOLET),
    ]
    yy = 260
    for key, value, color in notes:
        parts.append(rect(1250, yy - 22, 12, 12, fill=color, stroke=color, radius=2))
        parts.append(text(1275, yy - 10, key, size=15, weight=700))
        parts.append(text(1275, yy + 16, value, size=14, fill=MUTED))
        yy += 92
    write_svg(
        "fig-1-5-phase-time",
        "图 1-5  Worker 与 Master 的相位耗时",
        "不同侧的相位分别归一化；该图显示已记账时间，不代表完整 CPU profile",
        "".join(parts),
        height=850,
    )


def chart_strategy_mix() -> None:
    rows = [
        ("base64_h", 8, 41),
        ("sqlite", 14, 31),
        ("libarchive", 8, 14),
        ("png", 51, 81),
        ("pcre2", 52, 79),
        ("xml", 114, 111),
    ]
    parts = []
    max_total = max(a + b for _, a, b in rows)
    y = 160
    for name, nominal, optimistic in rows:
        total = nominal + optimistic
        x = 250
        width = 1050 * total / max_total
        nw = width * nominal / total
        ow = width - nw
        parts.append(text(65, y + 27, name, size=17, weight=600))
        parts.append(rect(x, y, nw, 38, fill=TEAL, stroke=TEAL, radius=3))
        parts.append(rect(x + nw, y, ow, 38, fill=ORANGE, stroke=ORANGE, radius=3))
        parts.append(text(x + width + 18, y + 27, f"{nominal}+{optimistic}={total} · {optimistic/total:.1%}", size=15))
        y += 82
    parts.append(rect(250, 685, 20, 20, fill=TEAL, stroke=TEAL, radius=3))
    parts.append(text(282, 701, "nominal：严格 SAT", size=15))
    parts.append(rect(520, 685, 20, 20, fill=ORANGE, stroke=ORANGE, radius=3))
    parts.append(text(552, 701, "optimistic：丢全部前缀后的 SAT", size=15))
    parts.append(rect(250, 742, 1160, 72, fill=PANEL, stroke=LINE))
    parts.append(text(830, 774, "六条单种子 trace 中 z3_solves = interesting_branches，且 z3_timeouts = 0。", size=16, anchor="middle"))
    parts.append(text(830, 800, "条长表示保存产出数，不是 campaign 中被 AFL 接受的贡献。", size=15, fill=MUTED, anchor="middle"))
    write_svg(
        "fig-3-3-strategy-mix",
        "图 3-3  默认求解策略的保存产出构成",
        "六个真实目标的专项短测；横向条长按 nominal + optimistic 产出数缩放",
        "".join(parts),
        height=850,
    )


def chart_optimistic_format() -> None:
    groups = [
        ("XML · 宽容文本格式", [("nominal", 3.89, 84, TEAL), ("optimistic", 4.12, 97, ORANGE)]),
        ("PNG · 严格二进制 + CRC", [("nominal", 1.20, 31, TEAL), ("optimistic", 0.51, 11, ORANGE)]),
    ]
    parts = []
    x_origins = [100, 850]
    max_value = 4.5
    for (title_, entries), x0 in zip(groups, x_origins):
        parts.append(rect(x0, 135, 650, 570, fill=PANEL, stroke=LINE))
        parts.append(text(x0 + 325, 180, title_, size=21, weight=700, anchor="middle"))
        y_base = 600
        for idx, (name, value, unique, color) in enumerate(entries):
            x = x0 + 115 + idx * 265
            height = 330 * value / max_value
            parts.append(rect(x, y_base - height, 150, height, fill=color, stroke=color, radius=5))
            parts.append(text(x + 75, y_base - height - 18, f"{value:.2f} 边/产出", size=16, weight=700, anchor="middle"))
            parts.append(text(x + 75, y_base + 30, name, size=16, anchor="middle"))
            parts.append(text(x + 75, y_base + 57, f"独有新边 {unique}", size=14, fill=MUTED, anchor="middle"))
    parts.append(rect(175, 755, 1250, 82, fill="#FEF3C7", stroke="#D97706"))
    parts.append(text(800, 790, "XML 中 optimistic 单产出价值略高；PNG 中仅为 nominal 的 42.5%。", size=17, weight=700, anchor="middle"))
    parts.append(text(800, 817, "这是一次性重放的互补性观察，未计入求解、启动、排队和长期反馈成本。", size=15, fill=MUTED, anchor="middle"))
    write_svg(
        "fig-3-4-optimistic-format",
        "图 3-4  乐观求解的价值取决于输入格式",
        "柱高为每个保存产出的新边数；标签同时给出独有 edge ID 数",
        "".join(parts),
        height=880,
    )


def chart_nested_frontier() -> None:
    outputs = [1, 2, 4, 7, 12, 20, 33, 54, 88, 143, 118, 82, 36, 9, 1]
    dedup = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 54, 28, 8, 1, 0]
    frontier = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 64, 54, 28, 8, 1]
    parts = []
    left, top, width, height = 90, 155, 1020, 460
    parts.append(rect(left, top, width, height, fill=PANEL, stroke=LINE))
    for tick in [0, 32, 64, 96, 128, 160]:
        y = top + height - tick / 160 * height
        parts.append(line(left, y, left + width, y, stroke="#E2E8F0"))
        parts.append(text(left - 15, y + 5, str(tick), size=13, fill=MUTED, anchor="end"))
    series = [
        ("每代产出", outputs, ORANGE),
        ("内容去重后候选", dedup, BLUE),
        ("下一代实际前沿", frontier, TEAL),
    ]
    for label, values, color in series:
        points = []
        for idx, value in enumerate(values):
            x = left + 35 + idx * (width - 70) / (len(values) - 1)
            y = top + height - value / 160 * height
            points.append(f"{x},{y}")
            parts.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{color}"/>')
            if label == "每代产出":
                parts.append(text(x, top + height + 30, str(idx + 1), size=12, fill=MUTED, anchor="middle"))
        parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="3"/>')
    cap_y = top + height - 64 / 160 * height
    parts.append(line(left, cap_y, left + width, cap_y, stroke=RED, width=2, dash="8 6"))
    parts.append(text(left + width - 5, cap_y - 10, "FIFO 前沿上限 64", size=14, fill=RED, anchor="end"))
    parts.append(text(left + 510, top + height + 60, "代数", size=15, fill=MUTED, anchor="middle"))
    legend_x = 145
    for label, _, color in series:
        parts.append(rect(legend_x, 670, 18, 18, fill=color, stroke=color, radius=3))
        parts.append(text(legend_x + 28, 685, label, size=14))
        legend_x += 250
    parts.append(rect(1160, 155, 355, 460, fill="#F0FDFA", stroke="#5EEAD4"))
    parts.append(text(1338, 210, "Coverage-guided", size=20, weight=700, anchor="middle"))
    parts.append(text(1338, 244, "前沿恒为 1", size=18, fill=TEAL, anchor="middle"))
    parts.append(text(1338, 292, "深度 16", size=16, weight=700, anchor="middle"))
    parts.append(text(1338, 320, "16 次执行 · 第 16 代解出", size=15, anchor="middle"))
    parts.append(text(1338, 370, "深度 32", size=16, weight=700, anchor="middle"))
    parts.append(text(1338, 398, "32 次执行 · 第 32 代解出", size=15, anchor="middle"))
    parts.append(text(1338, 456, "取头 FIFO · cap 64", size=16, weight=700, anchor="middle"))
    parts.append(text(1338, 484, "298 次执行 · 止步深度 10", size=15, fill=RED, anchor="middle"))
    parts.append(text(1338, 530, "无截断 FIFO", size=16, weight=700, anchor="middle"))
    parts.append(text(1338, 558, "2,583 次执行 · 第 16 代解出", size=15, anchor="middle"))
    parts.append(text(1338, 590, "说明失败来自取头截断", size=14, fill=MUTED, anchor="middle"))
    parts.append(rect(150, 735, 1300, 102, fill="#FEF3C7", stroke="#D97706"))
    parts.append(text(800, 770, "截断只发生一次：第 10 代产生 89 个去重候选，下一代只保留前 64 个。", size=17, weight=700, anchor="middle"))
    parts.append(text(800, 800, "唯一正确前缀排第 88 位而被丢弃；coverage-guided 以新边信号保住它。", size=16, fill=RED, anchor="middle"))
    parts.append(text(800, 824, "结论限于 nxt[:64] 取头截断，不代表任意 FIFO 或任意截断策略。", size=14, fill=MUTED, anchor="middle"))
    write_svg(
        "fig-3-5-nested-frontier",
        "图 3-5  取头 FIFO 的唯一截断点与覆盖率制导前沿",
        "深度 16；三条曲线分别区分产出、内容去重候选和下一代实际执行前沿",
        "".join(parts),
        height=880,
    )


def chart_focus_bytes() -> None:
    parts = []
    values = ["53", "59", "4d", "43", "03", "41", "41", "41"]
    x0, y0, cell = 240, 225, 120
    for idx, value in enumerate(values):
        symbolic = idx <= 3
        color = TEAL if symbolic else MUTED
        bg = TEAL_BG if symbolic else "#F1F5F9"
        parts.append(rect(x0 + idx * cell, y0, 92, 92, fill=bg, stroke=color, radius=6, stroke_width=2))
        parts.append(text(x0 + idx * cell + 46, y0 + 49, value, size=22, weight=700, anchor="middle"))
        parts.append(text(x0 + idx * cell + 46, y0 + 120, str(idx), size=15, fill=MUTED, anchor="middle"))
        parts.append(text(x0 + idx * cell + 46, y0 + 158, "符号" if symbolic else "具体", size=16, weight=700, fill=color, anchor="middle"))
        parts.append(line(x0 + idx * cell + 46, y0 + 96, x0 + idx * cell + 46, y0 + 137, stroke=color, width=2))
    parts.append(text(800, 170, "种子  SYMC\\x03AAA  ·  SYMCC_FOCUS_BYTES=0-3", size=20, weight=700, anchor="middle"))
    cards = [
        ("无限制", "6 个产出", 6, BLUE),
        ("focus 0-3", "4 个产出", 4, TEAL),
        ("focus 5-7", "1 个产出", 1, ORANGE),
        ("focus 0-1", "2 个产出", 2, VIOLET),
    ]
    xx = 180
    for label, result, count, color in cards:
        parts.append(rect(xx, 520, 280, 118, fill=PANEL, stroke=color, radius=8, stroke_width=2))
        parts.append(text(xx + 140, 562, label, size=17, weight=700, anchor="middle"))
        parts.append(text(xx + 140, 605, result, size=22, weight=700, fill=color, anchor="middle"))
        xx += 320
    parts.append(rect(185, 700, 1230, 96, fill="#FEE2E2", stroke=RED))
    parts.append(text(800, 738, "范围外字节返回 nullptr：依赖它们的分支不会生成查询，也不会产生候选。", size=17, weight=700, anchor="middle"))
    parts.append(text(800, 770, "偏移仍在门控前记录，因此 focus 不等同于截短输入。", size=15, fill=MUTED, anchor="middle"))
    write_svg(
        "fig-4-3-focus-bytes",
        "图 4-3  Focus bytes 改变符号可见性",
        "偏移是 Z3 输入变量身份；绿色字节产生 ReadExpr，灰色字节保持具体",
        "".join(parts),
        height=850,
    )


def chart_cpu_allocation() -> None:
    configs = [
        ("1 AFL + 14 SymCC", 19.30, 23.30),
        ("4 AFL + 11 SymCC", 21.39, 23.79),
        ("8 AFL + 7 SymCC", 22.82, 23.92),
        ("12 AFL + 3 SymCC", 22.87, 21.60),
    ]
    parts = []
    base_x, max_width = 360, 980
    y = 165
    for name, sqlite, archive in configs:
        parts.append(text(65, y + 32, name, size=16, weight=600))
        sw = max_width * sqlite / 26
        aw = max_width * archive / 26
        parts.append(rect(base_x, y, sw, 28, fill=TEAL, stroke=TEAL, radius=3))
        parts.append(text(base_x + sw + 14, y + 21, f"SQLite {sqlite:.2f}%", size=14))
        parts.append(rect(base_x, y + 38, aw, 28, fill=BLUE, stroke=BLUE, radius=3))
        parts.append(text(base_x + aw + 14, y + 59, f"libarchive {archive:.2f}%", size=14))
        y += 120
    parts.append(rect(360, 675, 20, 20, fill=TEAL, stroke=TEAL, radius=3))
    parts.append(text(392, 691, "SQLite", size=15))
    parts.append(rect(500, 675, 20, 20, fill=BLUE, stroke=BLUE, radius=3))
    parts.append(text(532, 691, "libarchive", size=15))
    parts.append(rect(190, 735, 1220, 82, fill="#FEF3C7", stroke="#D97706"))
    parts.append(text(800, 770, "np=16、总核数固定：SQLite 单点最高为 12+3，libarchive 单点最高为 8+7。", size=16, weight=700, anchor="middle"))
    parts.append(text(800, 798, "8 AFL + 7 SymCC 是跨目标折中；np=64 扩展实验据此将 SYMCC_WORKER_CAP 固定为 12。", size=15, fill=MUTED, anchor="middle"))
    write_svg(
        "fig-5-1-cpu-allocation",
        "图 5-1  等 CPU 预算下的 AFL / Concolic 分配扫描",
        "np=16 · rounds=3 · 240 s；横条为覆盖率，比较的是资源分配而非增加总核数",
        "".join(parts),
        height=860,
    )


def chart_icse23_coverage() -> None:
    rows = [
        ("H QSYM", 5763, 13.87, TEAL),
        ("H Pangolin", 5561, 9.88, TEAL),
        ("H Angora", 5517, 9.01, TEAL),
        ("H MEUZZ", 5422, 7.13, TEAL),
        ("H Eclipser", 5323, 5.18, TEAL),
        ("H DigFuzz", 5321, 5.14, TEAL),
        ("H Intriguer", 5254, 3.81, TEAL),
        ("C AFL++", 5180, 2.35, BLUE),
        ("C FairFuzz", 5171, 2.17, BLUE),
        ("C AFL", 5061, 0.00, BLUE),
    ]
    parts = []
    x0, full, y = 300, 1040, 135
    for name, edges, uplift, color in rows:
        parts.append(text(65, y + 22, name, size=15, weight=600))
        parts.append(rect(x0, y, full, 30, fill="#F1F5F9", stroke="#E2E8F0", radius=3))
        if uplift:
            parts.append(rect(x0, y, full * uplift / 15, 30, fill=color, stroke=color, radius=3))
        else:
            parts.append(line(x0, y, x0, y + 30, stroke=color, width=4))
        parts.append(text(x0 + full + 16, y + 22, f"{edges:,} edges · +{uplift:.2f}%", size=14))
        y += 62
    parts.append(rect(365, 770, 20, 20, fill=TEAL, stroke=TEAL, radius=3))
    parts.append(text(397, 786, "H：Hybrid fuzzer", size=15))
    parts.append(rect(650, 770, 20, 20, fill=BLUE, stroke=BLUE, radius=3))
    parts.append(text(682, 786, "C：双实例传统 CGF", size=15))
    parts.append(text(1040, 786, "横条为相对 AFL 的平均边覆盖增幅", size=14, fill=MUTED))
    write_svg(
        "fig-5-0a-icse23-edge-coverage",
        "图 5-0A  ICSE'23 统一再评估：平均边覆盖",
        "15 个真实程序 · 24 h × 5 轮；数值标签同时给出平均边数和相对 AFL 增幅",
        "".join(parts),
        height=840,
    )


def chart_icse23_crashes() -> None:
    rows = [
        ("H QSYM", 147, TEAL),
        ("H Angora", 129, TEAL),
        ("C AFL++", 128, BLUE),
        ("H Pangolin", 127, TEAL),
        ("H MEUZZ", 123, TEAL),
        ("H Intriguer", 119, TEAL),
        ("C FairFuzz", 116, BLUE),
        ("C AFL", 109, BLUE),
        ("H Eclipser", 107, TEAL),
        ("H DigFuzz", 105, TEAL),
    ]
    parts = []
    x0, full, y = 300, 1040, 135
    for name, crashes, color in rows:
        parts.append(text(65, y + 22, name, size=15, weight=600))
        parts.append(rect(x0, y, full, 30, fill="#F1F5F9", stroke="#E2E8F0", radius=3))
        parts.append(rect(x0, y, full * crashes / 160, 30, fill=color, stroke=color, radius=3))
        parts.append(text(x0 + full + 16, y + 22, f"{crashes} unique crashes", size=14))
        y += 62
    parts.append(rect(365, 770, 20, 20, fill=TEAL, stroke=TEAL, radius=3))
    parts.append(text(397, 786, "H：Hybrid fuzzer", size=15))
    parts.append(rect(650, 770, 20, 20, fill=BLUE, stroke=BLUE, radius=3))
    parts.append(text(682, 786, "C：传统 CGF", size=15))
    parts.append(text(980, 786, "10 个存在 crash 的目标求和", size=14, fill=MUTED))
    write_svg(
        "fig-5-0b-icse23-unique-crashes",
        "图 5-0B  ICSE'23 统一再评估：Unique crashes",
        "10 个存在 crash 的真实程序合计；QSYM 是唯一呈现明显优势的 hybrid",
        "".join(parts),
        height=840,
    )


def chart_lava_endpoints() -> None:
    rows = [
        ("AFL-only · 8 instances", 0, "240 s", "~1,920", MUTED),
        ("Hybrid · np=8", 29, "~18 s", "~144", ORANGE),
        ("Concolic MPI · np=8", 42, "104 s", "~832", TEAL),
        ("Concolic MPI · np=32", 44, "105 s", "~3,360", GREEN),
    ]
    parts = []
    x0, full = 400, 930
    y = 175
    for label, bugs, wall, cpu, color in rows:
        parts.append(text(65, y + 28, label, size=16, weight=600))
        parts.append(rect(x0, y, full, 38, fill="#F1F5F9", stroke="#E2E8F0", radius=4))
        if bugs:
            parts.append(rect(x0, y, full * bugs / 44, 38, fill=color, stroke=color, radius=4))
        parts.append(text(x0 + full + 18, y + 27, f"{bugs}/44", size=16, weight=700))
        parts.append(text(1435, y + 27, f"{wall} · CPU·s {cpu}", size=14, fill=MUTED))
        y += 100
    parts.append(text(400, 630, "0", size=13, fill=MUTED, anchor="middle"))
    parts.append(text(865, 630, "22", size=13, fill=MUTED, anchor="middle"))
    parts.append(text(1330, 630, "44 listed bugs", size=13, fill=MUTED, anchor="middle"))
    parts.append(rect(150, 690, 1300, 116, fill="#FEE2E2", stroke=RED))
    parts.append(text(800, 728, "四行来自不同历史运行，CPU·秒相差超过 20 倍；墙钟是 campaign 预算，不是首次发现时间。", size=16, weight=700, anchor="middle"))
    parts.append(text(800, 760, "因此只能说明 endpoint 机制能力，不能计算速度提升、显著性或系统排名。", size=16, fill=RED, anchor="middle"))
    parts.append(text(800, 788, "LAVA-M 规模小且 endpoint 接近上限；这些非等预算历史结果不能外推到大型真实程序。", size=14, fill=MUTED, anchor="middle"))
    write_svg(
        "fig-6-1-lava-endpoints",
        "图 6-1  LAVA-M base64 的 endpoint 结果",
        "条长为触发的 listed bug 数；右侧同时标注墙钟预算与近似 CPU·秒",
        "".join(parts),
        height=850,
    )


def chart_persistent_mode() -> None:
    rows = [
        ("XML 带 @@", 15191, 20, 0.00, RED),
        ("XML 不带 @@", 40787, 4144, 10.51, TEAL),
        ("PNG 带 @@", 16461, 11, 0.07, RED),
        ("PNG 不带 @@", 143718, 193, 16.80, BLUE),
    ]
    parts = []
    columns = [("execs/s", 260, 143718), ("AFL queue", 740, 4144), ("bitmap coverage", 1160, 18)]
    for label, x, _ in columns:
        parts.append(text(x + 145, 135, label, size=18, weight=700, anchor="middle"))
    y = 185
    for name, eps, queue, cov, color in rows:
        parts.append(text(55, y + 24, name, size=16, weight=600))
        for value, (_, x, max_value) in zip((eps, queue, cov), columns):
            parts.append(rect(x, y, 290, 32, fill="#F1F5F9", stroke="#E2E8F0", radius=3))
            width = 290 * value / max_value if value else 0
            if width:
                parts.append(rect(x, y, max(2, width), 32, fill=color, stroke=color, radius=3))
            label = f"{value:,.0f}" if isinstance(value, int) else f"{value:.2f}%"
            parts.append(text(x + 300, y + 23, label, size=14))
        y += 105
    parts.append(rect(145, 655, 1310, 128, fill="#FEE2E2", stroke=RED))
    parts.append(text(800, 695, "带 @@ 的配置看似仍有 15–16k exec/s，但 bitmap 覆盖率接近 0。", size=17, weight=700, anchor="middle"))
    parts.append(text(800, 728, "吞吐不等于有效测试：持久 + shmem 目标必须使用正确的无 @@ 调用方式。", size=16, fill=RED, anchor="middle"))
    parts.append(text(800, 760, "持久模式识别使用 ##SIG_AFL_PERSISTENT##，不能用共享内存符号替代。", size=14, fill=MUTED, anchor="middle"))
    write_svg(
        "fig-6-2-persistent-mode",
        "图 6-2  持久模式调用方式的 A/B 对照",
        "同一 AFL 二进制 · 60 s；三列分别独立归一化，不能按条长跨列比较",
        "".join(parts),
        height=830,
    )


def chart_scaling_vs_coverage() -> None:
    np_values = [2, 8, 32, 128, 190]
    mpi_t = [128, 1604, 7683, 15022, 13906]
    mpi_c = [5.67, 5.88, 6.13, 6.26, 6.17]
    hyb_t = [27, 55, 187, 497, 590]
    hyb_c = [7.81, 8.37, 8.82, 8.84, 8.80]
    parts = []
    panels = [
        (70, 155, 700, 500, "Pure MPI", mpi_t, mpi_c, 16000, (5.5, 6.4)),
        (830, 155, 700, 500, "Hybrid", hyb_t, hyb_c, 650, (7.5, 9.0)),
    ]
    for x, y, width, height, title_, throughput, coverage, tmax, (cmin, cmax) in panels:
        parts.append(rect(x, y, width, height, fill=PANEL, stroke=LINE))
        parts.append(text(x + width / 2, y + 38, title_, size=20, weight=700, anchor="middle"))
        points_t, points_c = [], []
        for idx, nproc in enumerate(np_values):
            px = x + 75 + idx * (width - 150) / (len(np_values) - 1)
            py_t = y + height - 70 - throughput[idx] / tmax * (height - 150)
            py_c = y + height - 70 - (coverage[idx] - cmin) / (cmax - cmin) * (height - 150)
            points_t.append(f"{px},{py_t}")
            points_c.append(f"{px},{py_c}")
            parts.append(text(px, y + height - 35, f"np={nproc}", size=13, fill=MUTED, anchor="middle"))
            parts.append(f'<circle cx="{px}" cy="{py_t}" r="5" fill="{BLUE}"/>')
            parts.append(f'<circle cx="{px}" cy="{py_c}" r="5" fill="{ORANGE}"/>')
            parts.append(text(px, py_t - 12, f"{throughput[idx]:,}", size=12, fill=BLUE, anchor="middle"))
            parts.append(text(px, py_c + 24, f"{coverage[idx]:.2f}%", size=12, fill=ORANGE, anchor="middle"))
        parts.append(f'<polyline points="{" ".join(points_t)}" fill="none" stroke="{BLUE}" stroke-width="4"/>')
        parts.append(f'<polyline points="{" ".join(points_c)}" fill="none" stroke="{ORANGE}" stroke-width="4"/>')
    parts.append(rect(430, 705, 20, 20, fill=BLUE, stroke=BLUE, radius=3))
    parts.append(text(462, 721, "吞吐 tc/s（各面板左尺度）", size=15))
    parts.append(rect(850, 705, 20, 20, fill=ORANGE, stroke=ORANGE, radius=3))
    parts.append(text(882, 721, "边覆盖率（各面板独立范围）", size=15))
    parts.append(rect(170, 770, 1260, 82, fill="#FEF3C7", stroke="#D97706"))
    parts.append(text(800, 805, "Pure MPI 吞吐约增 117×，边覆盖只从 5.67% 增至 6.26%；吞吐与覆盖明显脱钩。", size=16, weight=700, anchor="middle"))
    parts.append(text(800, 833, "大语料 showmap 最多抽样 20,000 文件，图中覆盖率不能用于精确效率排名。", size=14, fill=MUTED, anchor="middle"))
    write_svg(
        "fig-6-3-scaling-vs-coverage",
        "图 6-3  并行吞吐扩展不等于覆盖率扩展",
        "gfts-xml · 120 s · 单轮；蓝线与橙线使用不同尺度，数值标签给出原始数据",
        "".join(parts),
        height=900,
    )


def render_dot_assets() -> None:
    for name, body in GRAPHS.items():
        source = dot_source(body)
        dot_path = SRC / f"{name}.dot"
        dot_path.write_text(source, encoding="utf-8")
        subprocess.run(["dot", "-Tsvg", str(dot_path), "-o", str(OUT / f"{name}.svg")], check=True)
        subprocess.run(
            ["dot", "-Tpng", "-Gdpi=170", str(dot_path), "-o", str(OUT / f"{name}.png")],
            check=True,
        )


def render_custom_pngs() -> None:
    chrome = os.environ.get("CHROME", "/usr/bin/google-chrome")
    custom_names = sorted(
        path.stem for path in OUT.glob("*.svg") if not (SRC / f"{path.stem}.dot").exists()
    )
    for name in custom_names:
        svg_path = (OUT / f"{name}.svg").resolve()
        png_path = (OUT / f"{name}.png").resolve()
        dimensions = re.search(
            r'<svg[^>]+width="(\d+)"[^>]+height="(\d+)"',
            svg_path.read_text(encoding="utf-8"),
        )
        if dimensions is None:
            raise RuntimeError(f"Cannot determine SVG dimensions: {svg_path}")
        width, height = dimensions.groups()
        screenshot_height = int(height) + 120
        subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                f"--window-size={width},{screenshot_height}",
                f"--screenshot={png_path}",
                svg_path.as_uri(),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)
    render_dot_assets()
    chart_filter_funnel()
    chart_phase_time()
    chart_strategy_mix()
    chart_optimistic_format()
    chart_nested_frontier()
    chart_focus_bytes()
    chart_cpu_allocation()
    chart_icse23_coverage()
    chart_icse23_crashes()
    chart_lava_endpoints()
    chart_persistent_mode()
    chart_scaling_vs_coverage()
    render_custom_pngs()
    print(f"Rendered {len(list(OUT.glob('*.svg')))} SVG and {len(list(OUT.glob('*.png')))} PNG figures")


if __name__ == "__main__":
    main()
