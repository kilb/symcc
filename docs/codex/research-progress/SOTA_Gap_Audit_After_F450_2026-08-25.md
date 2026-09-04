# F450 后 SOTA 缺口审计与实施顺序

## 1. 审计结论

截至 F450，项目已经具备并行 hybrid 调度、Prefix-DAG/S2F、Backsolver/Veritesting、结构化输入
选择、增量 QF_BV、可检查 clause/proof 交换、PalRUP wire/check pipeline、可塑 worker 控制、
ULFM 连续恢复、证明前缀分区、认证 cube 执行和闭包绑定证明重放缓存。仍不能据此表述为“所有
SOTA 已完整复现”：有些工作只实现了核心机制，有些只缺生产连接，有些只缺 R 级证据。

本审计采用四级证据：I=实现、T=测试、E=机制实验、R=公共目标等资源重复实验。只有论文关键
执行闭环、失败语义和适用域均具备 I/T，并有匹配范围的 E/R，才称为完整落地。

## 2. 已闭合与部分闭合

| 技术主线 | 当前证据 | 准确判断 |
| --- | --- | --- |
| proof-prefix cube 生成与执行 | F448/F449：完备互斥 partition、lease、SAT 抢先取消、UNSAT 聚合 | 本地 query 内机制闭合；跨节点执行未闭合 |
| 增量 proof checking / exchange | F432--F437：LRUP DAG、event stream、checked import、activity/utility | project-native 闭合；不是 TACAS checker 全部 native 机制 |
| PalRUP | F439 binary wire；F440 官方 local/redistribute/confirm pipeline | consumer/checker 闭合；solver-native producer 未闭合 |
| Mallob 式资源管理 | F438 逻辑伸缩；F441/F443/F446/F447 物理故障恢复与 warm spare | worker substrate 闭合；F449 cube 尚未成为跨节点 job |
| Smart Cubing 前置能力 | F436 activity、F448 checked activity ordering、F449 executor | 有静态/checked signals；缺在线 prerun/cost policy 闭环 |
| LLM/agentic concolic | verified proposal loop、F36、F399、agentic hooks | LLM 只提议、verifier 决策的安全边界已存在；Cottontail/HyLLfuzz 完整协议未复现 |
| heap path optimality | F405--F422 的 bounded C-heap/MemoryPhi/loop summaries | POSE-inspired 有界适配；不是 initial symbolic heap 的完整 POSE |
| 证明重复重放 | F450 plan/policy/closure-bound cache，10 轮 3.90x--25.39x | 本地机制闭合；native checker compression 未闭合 |

## 3. 剩余实施顺序

| 顺序 | 优先级 / 候选编号 | 真正缺口 | 完成定义 |
| ---: | --- | --- | --- |
| 1 | P0 / F451 | **跨节点认证 cube 执行**：F449 task ledger 尚未映射到 F438/F447 stable endpoint、generation、worker catalog 和 ULFM recovery | cube lease/heartbeat/completion 带 generation fence；节点退出后未决 cube 可恢复；SAT winner 全局取消；UNSAT 聚合不丢叶；同机与双节点故障 oracle |
| 2 | P0 / F452 | **native checker clause compression**：F450 降低重复 CPU，但 hot path 仍读取/哈希完整 closure，未实现 TACAS 2026 checker 内压缩 | 固定论文/源码合同；unsigned literal sorting、delta + variable-byte encoding、<=7-byte inline；round-trip/overflow/corruption/endianness oracle；内存与时间消融 |
| 3 | P1 / F453 | **online activity/cost-guided cubing**：F436/F448 signals 尚未形成 query family 的 prerun→partition→outcome 学习闭环 | 只消费 checked activity/observed solve cost；policy identity 持久化；探索预算和 fallback；static/activity/cost 三路等资源对照 |
| 4 | P1 / F454 | **solver-native PalRUP production**：当前只能转换/检查外部或 fixture fragments | CaDiCaL worker 原生 rank fragment；稳定 rank/ID namespace；crash-close；官方 checker E2E；不得由项目 parser 自证 |
| 5 | P1 / F455 | **结构化输入的完整 LLM concolic 回路**：已有 alpha-normalized constraint selection 和 bounded proposals，缺 history-guided seed acquisition / iterative solve-complete | 模型仅提出结构/输入；本地 parser、Query IR 和目标执行验证；budget/cancellation/replay；无模型时确定性 fallback；公开结构化 parser 消融 |
| 6 | P2 / F456 | **完整 POSE initial symbolic heap 域**：当前是 bounded C heap adaptation | fresh/null/alias materialization、field update、heap quotient、只在真实 CFG branch 分叉；形式不变量和公开 heap benchmark |
| 7 | R-track | **长时、多节点、公共目标证据** | 预注册 1/2/4/8/16/32/64 workers，6h/24h，多轮独立 seed，AFL-only/hybrid/feature ablation；报告覆盖 AUC、time-to-edge、失败率、CPU-hours 和置信区间 |

## 4. 为什么按此顺序

1. F449 已形成可执行 cube ledger，F438/F447 已形成 stable endpoint 与恢复 substrate；先连接二者能最短
   路径获得真正的跨节点 query-internal parallelism，而不是再增加一个孤立算法模块。
2. TACAS 2026 明确把 checker 内存和实时确认作为大规模增量 SAT 的工程瓶颈；F450 只关闭重复重放，
   因此 native compression 是可信求解链的下一性能边界。
3. Smart Cubing 的 10,000 CPU-hour 实验说明 prerun learned constraints、cubing strategy 与参数配置
   需要联合评估；本项目已有可信 signal，但不能把静态排序误称为完整 smart cubing。
4. PalRUP 的贡献是 solver 并行地产生并行持久证明；只有 consumer pipeline 而没有 producer 不能称为
   完整 PalRUP。
5. Cottontail、HyLLfuzz 等 LLM 路线有明显目标依赖和不确定性。LLM 必须留在 proposal plane，任何
   coverage admission、SAT/UNSAT 结论和输入有效性仍由确定性执行与 checker 决定。

## 5. 主要 SOTA 依据

- [Real-time Proof Checking for Distributed Incremental SAT Solving, TACAS 2026](https://publikationen.bibliothek.kit.edu/1000193848)：增量 assumptions、checked clause sharing、动态资源和在线 checker compression；
- [Mallob: Scalable Automated Reasoning on Demand, CAV 2026](https://link.springer.com/chapter/10.1007/978-3-032-32526-6_5)：proof-checked、incremental、malleable distributed reasoning；
- [A Natively Parallel Proof Framework for Clause-Sharing SAT Solving, SAT 2026](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2026.17)：PalRUP 持久并行 proof/checking，实验到 3072 cores；
- [Smart Cubing for Graph Search, 2025](https://arxiv.org/abs/2501.17201)：prerun、cubing strategy、algorithm configuration 和 LLM-generated design suggestions 的联合实验；
- [Cottontail, 2025/IEEE S&P 2026](https://arxiv.org/abs/2504.17542)：ESCT、LLM solve-complete 与 history-guided seed acquisition；
- [HyLLfuzz, 2024](https://arxiv.org/abs/2412.15931)：trace slicing 与 LLM input modification 的 hybrid 回路；
- [S2F, 2026](https://arxiv.org/abs/2601.10068)：fuzzing、精确 symbolic execution、tailored execution 与 sampling 的分工；
- [POSE, SANER 2026](https://conf.researchr.org/details/saner-2026/saner-2026-papers/7/Path-Optimal-Symbolic-Execution-of-Heap-Manipulating-Programs)：heap alias relation 与 path optimality。

## 6. 声明边界

此清单是截至 2026-08-25 的工程研究路线，不是“文献中出现的每个算法都必须逐字复刻”。完成某项
要求的是与本项目执行域相匹配的机制、失败语义、测试和证据，而不是复用论文名称。R-track 是产品
效果证据，不能用 unit test 或 synthetic microbenchmark 替代；反过来，短时 coverage 波动也不能否定
已经由 proof/oracle 证明的正确性机制。
