# F435 后并行符号执行 SOTA 缺口审计

- 审计日期：2026-08-18
- 基线：F00--F435，共 436 个连续功能编号
- 原则：I/T/E 表示生产实现、测试与机制实验；只有公开目标、等 CPU、多轮统计才记为 R

## 1. 最新结论

F435 已关闭“实时 checked clause 遇到瞬时背压即永久丢失”的机制缺口：proof-first admission、
定点可回放策略、有界延期、native outcome feedback、跨 solve source history 与 QueryStore 二次裁决
均进入生产路径。它只完成原 P0“自适应 proof/context 分发”的 admission/backpressure 子项。

以下仍未实现，不能笼统声称所有 SOTA 完成。

## 2. 按顺序的剩余工作

| 顺序 | 缺口 | F435 后的精确状态 | 完成标准 |
| ---: | --- | --- | --- |
| P0-1 | 多节点 R 级实验 | F434 仅本机 5-rank，F435 仅本地同步 burst | 8/32/128 workers、公开长查询/混合 fuzzing、>=20 轮等 CPU、AUC/有效import/checker CPU/network/solve-tail |
| P0-2 | ImpCheck/PalRUP native wire | project-native JSON/SQLite event 与 LRUP/RUP | version negotiation、durable cursor、native message parser、cross-implementation replay、故障注入 |
| P0-3 | proof-aware worker pairing 与效用反馈 | source history只有交付结果，尚未度量consumer实际使用、传播或求解贡献 | 封印native utilization/activity；按可验证效用配对publisher-consumer；paired ablation。LBD只作诊断，不作默认选择信号 |
| P0-4 | Mallob 式 malleability | controller不调整worker数量/角色 | 运行中增减solver资源、迁移/排空、generation fence、收益驱动重调度 |
| P0-5 | IFSE closed-box fuzz solving | 有external model/fallback，无relation artifact或fuzz solver | deferred concretization IR、target生成、SMT/fuzz协同、candidate exact replay、公开目标 |
| P0-6 | 完整 native ConDPOR/RC11 | 有有限SC/TSO/RA回放 | mixed-size/fence/RMW/RF驱动执行，与herd7/diy/GenMC对拍 |
| P0-7 | 真实 Agolic/KLEE adapter | 有planner与有限runner | 隔离KLEE invocation、artifact replay、论文规模公开集等CPU复现 |
| P1-1 | proof-prefix guided partitioning | 有Prefix DAG和proof DAG，但没有从CDCL proof prefix提取分割变量/立方体 | 可回放prefix extractor、覆盖且互斥的cube证明、负载均衡、与lookahead/静态切分对照 |
| P1-2 | WP suffix proof summary | Prefix DAG/loop summary不等于WP sound prune | location/version WP CAS、蕴含proof、prune replay、跨worker复用 |
| P1-3 | Locus verified milestones | agent仅提路径/输入proposal | predicate implication proof、插桩身份、反例细化与Prefix DAG reward |
| P1-4 | 通用heap/external/continuation | 已覆盖大量有界域 | callbacks、varargs、custom allocator、线程共享对象、一般points-to与跨过程恢复 |
| P1-5 | 完整String/BV/FP/Array与结构化输入 | 现有策略分域实现 | mixed-theory conversion、在线grammar、solve-complete迭代与公开效果 |
| P2-1 | 形式化proof checker核 | Python checker + native delivery ACK | CakeML/Isabelle/HOL或同等级机器检查，并证明逐record等价 |
| P2-2 | WAN proof transport | 共享CAS与本机/合格共享FS | at-least-once cursor、partition recovery、版本协商、带宽背压与跨地域实验 |
| P2-3 | reactive agent触发 | agent接口存在，生产触发与成本边界不完整 | plateau/failure/barrier触发、无模型确定性退化、token/API预算 |

## 3. 推荐下一顺序

1. 在可获得真实集群前，先实现 native clause-utilization/activity telemetry 与 proof-aware pairing；
   它直接扩展F435的sealed candidate，不改变proof-first可信边界。SAT 2025 的MallobSat实验显示，
   传递或打乱单条共享clause的LBD没有可测价值，因此LBD只保留为可选诊断字段，不作为默认准入依据；
2. 加入 Mallob 式动态资源控制，但必须用 generation-fenced drain/migrate 协议避免活跃clause丢失；
3. 完成 ImpCheck/PalRUP adapter 与跨实现 oracle；
4. 在 8/32/128 worker 公开目标上冻结预注册实验，决定上述策略是否保留；
5. 加入proof-prefix guided partitioning的可回放cube生成与负载实验；
6. 再进入 IFSE、完整RC11和真实Agolic adapter三条语义主线。

## 4. 当前可对外表述

可以表述：项目已有436个连续功能；F435的自适应准入达到I/T/E-local；六轮构造burst机制实验中
交付由2/8恢复至8/8；每个导入仍有checker、decision、native ACK和QueryStore独立复核。

不能表述：已经复现完整Mallob/ImpCheck/PalRUP；已有utility-aware worker pairing；已获得solver speedup、fuzzing
coverage或缺陷发现提升；已完成多节点R级实验；所有SOTA已经实现。

主要对照来源：

- [Mallob](https://arxiv.org/abs/2205.06590)
- [Painless](https://www.lrde.epita.fr/dload/papers/le-frioux.17.sat.pdf)
- [Streamlining Distributed SAT Solver Design, SAT 2025](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.27)
- [Real-time Proof Checking for Distributed Incremental SAT Solving, TACAS 2026](https://satres.kikit.kit.edu/papers/2026-tacas-distrincproof.pdf)
- [A Natively Parallel Proof Framework for Clause-Sharing SAT Solving, SAT 2026](https://doi.org/10.4230/LIPIcs.SAT.2026.17)
- [Problem Partitioning via Proof Prefixes, SAT 2025](https://doi.org/10.4230/LIPIcs.SAT.2025.3)
- [IFSE, ICSE 2025](https://conf.researchr.org/details/icse-2025/icse-2025-demonstrations/7/IFSE-Taming-Closed-box-Functions-in-Symbolic-Execution-via-Fuzz-Solving)
- [Locus, ICSE 2026](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/61/Agentic-Predicates-Reasoning-for-Directed-Fuzzing)
- [Agolic, 2026](https://arxiv.org/abs/2608.06397)
