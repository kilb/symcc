# F436 后并行符号执行 SOTA 缺口审计

- 审计日期：2026-08-18
- 当前连续功能：F00--F436，共 437 项
- 证据等级：I=生产实现，T=自动测试，E=机制实验，R=公开目标上的预注册统计实验
- 原则：论文中出现过、仓库里有接口、或单个 synthetic case 通过，都不等于完整复现

## 1. 本轮结论

F436 已关闭 F435 审计中“consumer 端没有 native utilization/activity 信号”的子项。现在系统能把
checked delivery 与第一次 unit/conflict semantic activation 分开记录，并由 QueryStore 重建 proof、
event、ACK 和 witness。两轮本机 MPI active 实验累计 8/8 delivery、4/8 activation，证明两个量不同。

F436 没有证明单条 clause 对传播、冲突或 solve time 的唯一因果贡献，也没有实现按效用自动选择
publisher/consumer。因此，proof-aware worker pairing 仍是下一个直接依赖项。

结论仍是：**存在未实现的 SOTA，不能声称全部完成。**

## 2. P0：下一阶段必须完成

| 顺序 | 技术缺口 | 当前状态 | 完成标准 |
| ---: | --- | --- | --- |
| P0-1 | F437 utility-aware proof worker pairing | F436只有逐import delivery/activation；F435 source history不区分consumer与formula family | 持久有界pair状态；置信区间/后验不确定性；checker CPU和staleness成本；探索保底；deterministic replay；QueryStore复核；paired ablation |
| P0-2 | Mallob式malleability | coordinator角色固定，不能运行中增减solver资源 | generation-fenced grow/shrink；drain/migrate；活跃proof/lease不丢失；收益驱动重分配；故障注入和资源守恒 |
| P0-3 | PalRUP/ImpCheck原生互操作 | project-native JSON、LRUP/RUP和SQLite event；逻辑可检查但格式不互通 | version negotiation；PalRUP/LIDRUP parser；durable cursor；cross-implementation replay；截断、重复、乱序、重连故障矩阵 |
| P0-4 | 多节点R级实验 | F434/F436仅本机5-rank I/T/E-local | 8/32/128 workers；公开长查询和hybrid fuzzing目标；至少20个paired repeats；等CPU；AUC、有效import、checker CPU、network、solve tail和置信区间 |

### 2.1 F437 的信号边界

默认 reward 不使用 LBD。SAT 2025 对 MallobSat 的系统实验表明，跨worker共享时 LBD没有可测意义，
而 clause length 是更稳定的基本成本信号。F437 应使用：

```text
publisher x consumer x formula-family
  -> delivery probability
  -> activation opportunity probability
  -> checker CPU + queue/network cost + staleness
  -> uncertainty-aware utility
```

`unactivated` 不能直接记为负因果标签：它可能是重复、更强clause已存在、solve提前结束或当前trail尚未
访问相关区域。低样本pair必须保留探索概率，且任何调度决策都不能绕过proof checker。

## 3. P1：语义与搜索能力扩展

| 顺序 | 技术缺口 | 已有基础 | 尚缺内容 |
| ---: | --- | --- | --- |
| P1-1 | proof-prefix guided partitioning | Prefix DAG、proof DAG、checked clause stream | 从CDCL proof prefix提取变量；生成覆盖且互斥cubes；分割证明；负载估计；与lookahead/static split对照 |
| P1-2 | IFSE closed-box fuzz solving | external model、fallback、结构化proposal | deferred concretization IR；closed-box relation artifact；target生成；SMT/fuzz协同；candidate exact replay；公开目标 |
| P1-3 | 完整native ConDPOR/RC11 | F392 interpreter与F425有限SC/TSO/RA原子回放 | mixed-size、fence、RMW、RF驱动执行；完整弱内存约束；与herd7/diy/GenMC对拍 |
| P1-4 | WP suffix proof summary | Prefix DAG和loop summary | location/version绑定WP CAS；蕴含proof；sound prune replay；跨worker共享与失效协议 |
| P1-5 | Locus verified milestones | agent可提出路径和输入 | predicate implication proof；instrumentation identity；反例细化；milestone到Prefix DAG reward闭环 |
| P1-6 | 真实Agolic/KLEE adapter | F374 planner与F423有限BSE runner | 隔离KLEE invocation；真实state/coverage artifact；论文规模公开集；等CPU复现与失败恢复 |
| P1-7 | 通用heap/external/continuation | 多个有界heap、loop、exception合同 | callback/varargs/custom allocator；线程共享对象；一般points-to；non-local continuation与跨过程恢复 |
| P1-8 | mixed String/BV/FP/Array与Solve-Complete结构输入 | parser posterior、string双表示、QF_BV完整子域 | 跨理论转换证明；在线grammar；partial model与nonterminal hole统一求解；公开结构化输入实验 |

Proofix 的核心不是“再加一个分支评分”：它使用非平凡 CDCL proof prefix 中的变量出现频次产生静态
partition，并要求所有 cubes 的析取为重言式。若没有覆盖/互斥证据和负载对照，只能称启发式切分，
不能称 proof-prefix partitioning 复现。

## 4. P2：可信计算、跨域容错与智能触发

| 顺序 | 技术缺口 | 完成标准 |
| ---: | --- | --- |
| P2-1 | 形式化proof checker核 | CakeML/Isabelle/HOL或同等级机器检查；证明逐record parser/checker语义等价；与生产receipt连接 |
| P2-2 | WAN proof transport | at-least-once durable cursor；版本协商；partition/reconnect；带宽背压；跨地域实验；不依赖“同路径即共享FS” |
| P2-3 | MPI rank repair | ULFM revoke/shrink或等价机制；generation更替；lease/proof ownership恢复；成员变化下exactly-once裁决 |
| P2-4 | reactive agent trigger | plateau/failure/barrier触发；token/API硬预算；超时与不可用时确定性退化；proposal必须经现有proof/replay门 |
| P2-5 | 因果clause效用实验 | shadow/holdout或受控介入设计；paired counterfactual近似；禁止把F436 activation直接解释为speedup |

## 5. 实施顺序与依赖

1. F437：先把F436信号变成不确定性感知pairing，但不改变checker可信边界；
2. F438：用F437收益驱动Mallob式资源伸缩，并建立generation-fenced drain/migrate；
3. F439：加入PalRUP/ImpCheck互操作和跨实现oracle；
4. 冻结协议后执行8/32/128 worker R级实验，决定pairing和malleability是否保留；
5. 实现proof-prefix partitioning并与lookahead、随机、静态结构切分做等CPU对照；
6. 按IFSE、完整RC11、WP、Locus/Agolic顺序扩展语义主线；
7. 最后推进形式化checker核、WAN和rank repair，因为它们依赖稳定的wire/proof协议。

## 6. 当前允许与禁止的表述

允许表述：

- 已连续实现F00--F436共437项；
- F436达到I/T/E-local，具备真实CaDiCaL trail activity、密封receipt、QueryStore复核和本机MPI证据；
- 本地构造实验中delivery为8/8而activation为4/8，证明二者是不同观测量；
- 完整Python1274+291 subtests和LLVM17/18各326 discovered门禁无失败。

禁止表述：

- 已实现utility-aware worker pairing、Mallob完整malleability或PalRUP/ImpCheck互操作；
- 50%是公共workload的clause无效率；
- activity证明唯一因果贡献或带来solver/coverage/defect-yield提升；
- 已有8/32/128 worker多节点R级结论；
- 所有并行符号执行SOTA均已完成。

## 7. 主要一手来源

- [Painless: A Framework for Parallel SAT Solving](https://www.lrde.epita.fr/dload/papers/le-frioux.17.sat.pdf)
- [Mallob: Scalable Job Scheduling for Parallel SAT Solving](https://arxiv.org/abs/2205.06590)
- [Streamlining Distributed SAT Solver Design, SAT 2025](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.27)
- [Problem Partitioning via Proof Prefixes, SAT 2025](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.3)
- [Real-time Proof Checking for Distributed Incremental SAT Solving, TACAS 2026](https://satres.kikit.kit.edu/papers/2026-tacas-distrincproof.pdf)
- [A Natively Parallel Proof Framework for Clause-Sharing SAT Solving, SAT 2026](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2026.17)
- [IFSE, ICSE 2025](https://conf.researchr.org/details/icse-2025/icse-2025-demonstrations/7/IFSE-Taming-Closed-box-Functions-in-Symbolic-Execution-via-Fuzz-Solving)
- [Locus, ICSE 2026](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/61/Agentic-Predicates-Reasoning-for-Directed-Fuzzing)
- [Agentic Planning for Symbolic Execution, 2026](https://arxiv.org/abs/2608.06397)
