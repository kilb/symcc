# F437 后并行符号执行 SOTA 缺口审计

- 审计日期：2026-08-18
- 当前连续功能：F00--F437，共 438 项
- 证据等级：I=生产实现，T=自动测试，E=机制实验，R=公开目标确认性实验
- 结论：**仍有未实现的 SOTA，不能声称全部完成**

## 1. 本轮变化

F437 关闭了 F436 后审计的首个 P0 缺口：系统现在按
`publisher x consumer x formula-family` 持久学习 checked-clause 效用，使用不确定性、delivery/activity、
checker 成本和 staleness 决策，并保留低样本和周期探索。decision/outcome/snapshot 可重放；QueryStore
独立复核后原子 checkpoint，backend 重启恢复。

两组本机 5-rank paired CaDiCaL 实验共 24 个机会，抑制 4 个 native 导入，baseline/treatment 都保留
12 次 activation；这是 I/T/E-local 机制证据，不是 solve-time 或 fuzzing 收益。

## 2. P0：并行求解主线

| 顺序 | 未完成技术 | 已有基础 | 关闭标准 |
| ---: | --- | --- | --- |
| P0-1 / F438 | Mallob 式 malleability | F437 有持久 pair utility；coordinator 角色仍固定 | generation-fenced grow/shrink；drain/migrate；proof/lease 不丢；收益驱动重分配；资源守恒和故障注入 |
| P0-2 / F439 | PalRUP/ImpCheck 原生互操作 | project-native JSON/RUP/LRUP/event stream | version negotiation；PalRUP/LIDRUP parser/writer；durable cursor；跨实现 replay；截断/重复/乱序/重连矩阵 |
| P0-3 | 8/32/128 worker 多节点 R 级实验 | 本机 5-rank I/T/E-local | 公开长 QF_BV 与 hybrid 目标；至少 20 paired repeats；等 CPU；AUC、checker CPU、network、tail、CI |
| P0-4 | proof-prefix guided partitioning | Prefix DAG、proof DAG、checked stream | proof prefix 变量提取；覆盖且互斥 cubes；partition proof；负载估计；与随机/lookahead/static split 等 CPU 对照 |

F437 的下一依赖必须先做 F438，而不是继续在固定 worker 数上堆评分：只有资源可以安全 grow/shrink，
pair utility 才能转化为 cluster allocation。随后冻结 wire protocol，再做 F439 和大规模实验，避免实验中途改变
artifact 格式。

## 3. P1：符号执行语义与搜索前沿

| 顺序 | 未完成技术 | 当前差距 |
| ---: | --- | --- |
| P1-1 | GenSlv 式 generator solving | 已有 Pangolin/optimistic generator 和候选复核，但没有面向一般约束合成可重复 generator、跨 KLEE/Angr/TritonDSE/SymCC 的公开复现 |
| P1-2 | IFSE closed-box fuzz solving | 缺 deferred concretization IR、closed-box relation artifact、fuzz/SMT 联合求解和公开目标 |
| P1-3 | 完整 native ConDPOR/RC11 | F392/F425 只覆盖有界 SC/TSO/RA；缺 mixed-size、fence、RMW、RF 驱动和 herd7/diy/GenMC 全 corpus 对拍 |
| P1-4 | WP suffix proof summary | 缺 location/version 绑定的 WP CAS、蕴含 proof、sound prune replay 和失效协议 |
| P1-5 | Locus 式 verified milestones | agent 可提议目标，但缺 predicate implication proof、instrumentation identity、反例细化和 Prefix DAG reward 闭环 |
| P1-6 | 真实 Agolic/KLEE adapter | 已有 planner/有限 BSE runner，缺隔离 KLEE invocation、真实 state/coverage artifact、论文规模等 CPU 复现 |
| P1-7 | 通用 heap/external/continuation | callback、varargs、custom allocator、线程共享对象、一般 points-to、non-local continuation 仍未闭合 |
| P1-8 | mixed String/BV/FP/Array 与结构输入联合求解 | 缺跨理论转换证明、partial model/nonterminal hole 统一求解和公开结构化输入实验 |

ICSE 2026 的 GenSlv 报告在四种 symbolic executor 上合成 constraint generator，并在多种 symbolic/hybrid
设置下提升覆盖；仓库现有“生成若干候选并逐个验证”是重要基础，但不能等同于完整 generator solving。

## 4. P2：可信计算与跨域容错

| 顺序 | 未完成技术 | 关闭标准 |
| ---: | --- | --- |
| P2-1 | 形式化 proof checker 核 | CakeML/Isabelle/HOL 等机器检查；parser/checker 语义等价；生产 receipt 连接 |
| P2-2 | WAN proof transport | at-least-once durable cursor；版本协商；partition/reconnect；带宽背压；跨地域实验 |
| P2-3 | MPI rank repair | ULFM revoke/shrink 或等价；generation 更替；lease/proof ownership 恢复；成员变化下 exactly-once |
| P2-4 | reactive agent trigger | plateau/failure/barrier 触发；token/API 硬预算；超时/不可用确定性退化；所有 proposal 经过 proof/replay |
| P2-5 | 因果 clause utility | shadow/holdout 或受控介入；paired counterfactual 近似；不能把 activation 直接解释为 speedup |

## 5. 建议实施顺序

1. F438：用 F437 utility 做 generation-fenced solver 资源伸缩；
2. F439：PalRUP/ImpCheck wire 互操作和跨实现 oracle；
3. 8/32/128 worker R 级实验，决定 pairing/malleability 是否默认开启；
4. proof-prefix partitioning，与 portfolio sharing 形成互补；
5. GenSlv generator solving 和 IFSE closed-box 语义；
6. 完整 RC11、WP、Locus/Agolic、通用 heap/external；
7. 形式化 checker、WAN 和 rank repair。

## 6. 当前允许与禁止的表述

允许：

- 已连续实现 F00--F437 共 438 项；
- F437 达到 I/T/E-local，具备 proof-first pairing、native outcome、探索保底、密封 replay、持久恢复；
- 两组本机 paired 机制实验中 native 导入减少 16.7%，activation 保留 12/12；
- activation rate 从 50% 到 60% 仅描述该机制 fixture。

禁止：

- 所有并行符号执行 SOTA 已完成；
- F437 已证明 solve-time、coverage 或漏洞/错误发现提升；
- 已实现 Mallob 完整 malleability、PalRUP/ImpCheck 互操作或 proof-prefix partitioning；
- 本机 5-rank 结果可推广到 8/32/128 worker 多节点部署。

## 7. 主要一手来源

- [Mallob: Scalable Job Scheduling for Parallel SAT Solving](https://arxiv.org/abs/2205.06590)
- [Streamlining Distributed SAT Solver Design, SAT 2025](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.27)
- [Problem Partitioning via Proof Prefixes, SAT 2025](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.3)
- [Real-time Proof Checking for Distributed Incremental SAT Solving, TACAS 2026](https://satres.kikit.kit.edu/papers/2026-tacas-distrincproof.pdf)
- [A Natively Parallel Proof Framework for Clause-Sharing SAT Solving, SAT 2026](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2026.17)
- [Generator Solving for Symbolic Execution, ICSE 2026](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/44/Generator-Solving-for-Symbolic-Execution)
- [IFSE, ICSE 2025](https://conf.researchr.org/details/icse-2025/icse-2025-demonstrations/7/IFSE-Taming-Closed-box-Functions-in-Symbolic-Execution-via-Fuzz-Solving)
- [Locus, ICSE 2026](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/61/Agentic-Predicates-Reasoning-for-Directed-Fuzzing)
- [Agentic Planning for Symbolic Execution, 2026](https://arxiv.org/abs/2608.06397)
