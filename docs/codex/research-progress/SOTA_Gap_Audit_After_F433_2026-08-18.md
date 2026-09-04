# F433 后并行符号执行 SOTA 缺口审计

- 审计日期：2026-08-18
- 代码基线：审计起点 F00--F433；当前已推进至 F434，共 435 个连续功能编号
- 验证基线：1255 个 Python 测试、291 个 subtests；LLVM 17/18 各 324 个 lit 测试
- 判断原则：论文名或配置项存在不等于完整复现；生产路径、正确性验证、机制实验、公开目标效果分级记录

## 1. 结论

当前项目已经不是“缺少主要符号执行机制”的原型。它已覆盖并行任务与 Prefix DAG、AFL data
coverage、结构化输入、reusable generator、Z3 model converter、polyhedral context reuse、并发
trace/有限 ConDPOR、continuation/loop summary、agentic proposal、跨 worker QF_BV proof artifact、
CaDiCaL exact context，以及 F433 的求解中 checked clause stream。

但仍不能表述为“所有 SOTA 已实现”。剩余工作分成三类：

1. **核心语义未闭合**：closed-box function fuzz solving、完整 RC11、通用 heap/external/
   continuation、完整结构化输入与跨理论求解；
2. **分布式系统未闭合**：ImpCheck 原生互操作、自适应 proof 流控、动态资源重调度、跨地域
   transport 与形式化 checker；
3. **效果证据未闭合**：真实 KLEE/Agolic adapter、多节点公开 benchmark、至少 20 轮等 CPU
   统计实验。

因此，当前状态应表述为：**大量 SOTA 机制达到 I/T/E，但完整论文域和 R 级效果仍有明确缺口。**

## 2. 分级标准

| 级别 | 完成含义 |
| --- | --- |
| I | 真实 worker 可通过生产配置进入该路径 |
| T | 正例、反例、故障、持久恢复和兼容测试通过 |
| E | 有独立 oracle 或机制实验，且结论限定在支持域 |
| R | 公开目标、等 CPU、多随机种子、统计检验和原始制品封存 |

“机制已实现”至少要求 I/T/E；覆盖率、速度或缺陷发现提升只允许由 R 级实验支持。

## 3. 最新文献与仓库对照

### 3.1 已实现，不再列为缺口

1. **Generator Solving / GenSlv（ICSE 2026）**：论文提出 reusable generator、Z3
   invertible model converter、hierarchical range sampler 和 optimistic simplification。仓库的
   F29、F177、F178、F181 已分别覆盖生成器、range/polytope sampling、原生 model converter、
   乐观 proposal 后的原查询复核；不再把“GenSlv 是否实现”列为开放项。
2. **实时分布式 SAT proof checking（TACAS 2026）**：F433 已实现 project-native
   proof-event stream、LRUP/RUP checked import、CaDiCaL external-clause callback、native ACK、
   learned RUP 回写和 QueryStore 二次裁决。未完成的是 wire 互操作、多节点效果和形式化可信核，
   不是求解中导入机制本身。
3. **Agolic 核心规划语义（2026）**：F374/F423 已有跨运行 proposal、有限 BSE runner、求模、
   串行 concrete replay 和失败原子记录；缺少真实 KLEE/public-target adapter 与论文规模复现。

主要来源：

- [Generator Solving for Symbolic Execution, ICSE 2026](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/44/Generator-Solving-for-Symbolic-Execution)
- [Real-time Proof Checking for Distributed Incremental SAT Solving, TACAS 2026](https://doi.org/10.1007/978-3-032-22752-2_18)
- [Agentic Planning for Symbolic Execution](https://arxiv.org/abs/2608.06397)

### 3.2 最新确认的未实现项

1. **IFSE closed-box deferred concretization + fuzz solving**：ICSE 2025 IFSE 为无法建模的
   system/library/closed-box call 保留参数与返回值关系，把相关约束编译成 fuzz target，再与 SMT
   部分协同求解。仓库有声明式 pure external model 和 semantic fallback，但没有 deferred
   concretization artifact、closed-box relation IR、fuzz-solver consumer 与模型回填验证。
   [IFSE](https://conf.researchr.org/details/icse-2025/icse-2025-demonstrations/7/IFSE-Taming-Closed-box-Functions-in-Symbolic-Execution-via-Fuzz-Solving)
2. **WP suffix summary 的有证明剪枝**：ECOOP 2026 的工作用已探索 suffix 的 weakest
   precondition 建立 summary，并以 `PC_new AND NOT WP_suffix` 的 UNSAT 证明判断后缀冗余。
   当前 Prefix DAG、veritesting 和 loop summary 没有 location-scoped WP CAS、覆盖关系证明及
   sound-prune consumer。
   [Efficient Symbolic Execution of Software Under Fault Attacks, ECOOP 2026](https://doi.org/10.4230/LIPIcs.ECOOP.2026.4)
3. **Locus 式 verified semantic milestones**：ICSE 2026 Locus 让 agent 提议通向目标的中间
   predicates，并用符号执行证明 predicate 是目标状态的安全松弛。当前 agentic 层能提出路径/
   输入方案，但没有 predicate implication proof、instrumentation artifact 和迭代反例细化。
   [Agentic Predicates Reasoning for Directed Fuzzing, ICSE 2026](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/61/Agentic-Predicates-Reasoning-for-Directed-Fuzzing)
4. **ConcoLixir 式 reactive discovery**：当前 LLM/agent 接口缺少 coverage plateau、solver
   failure、library barrier 三类生产触发器和成本预算；应保持“LLM 只提 proposal，真实执行给
   反馈”的信任边界。
   [ConcoLixir](https://arxiv.org/abs/2606.26545)
5. **PalRUP/ImpCheck 完整分布式证明生态**：F433 使用项目内 JSON/SQLite 协议，不是原生 wire；
   也没有 Mallob 式动态 worker 资源调整、proof-aware worker 配对或形式化 checker。
   [PalRUP, SAT 2026](https://doi.org/10.4230/LIPIcs.SAT.2026.17)

## 4. 优先级清单

### P0：下一阶段主线

| 顺序 | 工作 | 当前基础 | 完成定义 |
| ---: | --- | --- | --- |
| 1 | 多节点 F433 R 级实验 | F434 已完成 MPI 角色/身份/共享状态资格、active solve 因果门禁、root ACK 重放和 5-rank 本机机制实验 | 仍需 8/32/128 worker；有效 import、checker CPU、网络/CAS 放大、solve-tail、coverage AUC；多轮等 CPU |
| 2 | 自适应 proof/context 分发 | 静态 poll/budget、project-native ACK | ImpCheck adapter、queue/solve-age/LBD/收益驱动流控、failed-set 最小化、动态 worker 配对与重调度 |
| 3 | IFSE 式 closed-box fuzz solving | pure external model、fallback、AFL 协同 | relation IR、deferred concretization、fuzz target、SMT/fuzz 协同、候选模型 exact replay、Coreutils 对照 |
| 4 | 完整 native ConDPOR/RC11 | F425 有限 SC/TSO/RA 原子回放 | mixed-size、fence、RMW、RF 驱动执行；herd7/diy/GenMC 对拍；maximal-extension 证据 |
| 5 | 真实 Agolic/KLEE adapter | F374/F423 planner 与有限 runner | 隔离的真实 KLEE invocation、运行身份、artifact replay、七目标或等价公开集等 CPU 复现 |

P0 中第 1 项不是新算法，却是当前最重要的科研缺口。没有它，不能证明 F433 在长查询和真实
并行负载下抵消了约 2.3 ms idle-session 固定成本，也不能声称覆盖率提升。

### P1：扩大通用语义与调度能力

| 顺序 | 工作 | 缺失核心 |
| ---: | --- | --- |
| 6 | WP suffix proof summary | location/version-scoped WP、增量合并、蕴含 proof、sound-prune replay、跨 worker CAS |
| 7 | Locus verified milestones | agent predicate proposal、目标松弛证明、插桩身份、反例细化、Prefix DAG reward 接线 |
| 8 | 通用 continuation/heap/external | callback、varargs、custom allocator、线程共享对象、unknown effects、一般 points-to 与跨过程恢复 |
| 9 | Cottontail/Lase/SymCC-str 完整域 | solve-complete 迭代、在线 grammar、mixed String/BV/FP/Array、conversion/endptr/base |
| 10 | Pangolin/TaichiPoly 完整域 | sign/bitfield pack-unpack、非线性保守抽象、整数 projection artifact、OMT bounds、真实 yield |
| 11 | 多模态 SMT selection | AST 图 + 应用上下文表示、版本化模型、跨 logic 训练、可解释 fallback 与回放合同 |

### P2：可信性和研究增强

| 顺序 | 工作 | 准入约束 |
| ---: | --- | --- |
| 12 | 形式化 LRUP/checker 核 | CakeML/Isabelle/HOL 或同等级机器检查；先定义与 Python checker 的逐记录等价 |
| 13 | ConcoLixir reactive agent | plateau/failure/barrier 触发，确定性无模型退化，严格 token/API 成本预算 |
| 14 | 跨地域 proof transport | durable cursor、at-least-once 重放、CAS 事实源、背压、分区恢复和版本协商 |
| 15 | 完整 R 级总评 | 不少于 20 轮或预注册功效设计；paired bootstrap/randomization、多重校正、原始数据封存 |

## 5. 推荐实施顺序

1. F434 已在 F433 上建立多 rank 可观测性和失败关闭资格协议；冻结完整回归基线后进入集群；
2. 用同一 F434 sealed protocol 运行 8/32/128 worker 预实验，测出 proof checker、transport、solve tail 的真实瓶颈；
3. 依据测量实现自适应流控、worker pairing 和动态资源重调度，而不是先猜权重；
4. 并行推进 IFSE closed-box relation IR，因为它直接补齐真实程序常见 library barrier；
5. 完成 RC11 与真实 Agolic/KLEE adapter，关闭并发和 agentic 两条主线的论文完整域；
6. 再引入 WP suffix proof summary、Locus predicate proof 和多模态 SMT selection；
7. 最后冻结版本，执行公开目标等 CPU 多轮实验和统计报告。

## 6. 当前可对外表述

可以表述：

- 435 个连续技术功能已经进入统一档案；
- F433 的 project-native 求解中 proof stream 已达到 I/T/E；
- 两轮 512-case 真实 CaDiCaL oracle 均 0 mismatch、512/512 native ACK；
- 完整 Python 为 1255 passed + 291 subtests，LLVM 17/18 各 324 discovered 且零失败；
- GenSlv 核心机制已经落地，不属于未实现项。
- F434 已证明本机 5-rank active proof delivery 与 root replay；这关闭的是实验协议缺口，不是多节点 R 级效果缺口。

不能表述：

- 已实现 ImpCheck 原生 wire 或完整 Mallob 分布式调度；
- 已支持完整 RC11、closed-box fuzz solving、WP suffix proof pruning 或 Locus predicate proof；
- F433 已提升公开 benchmark 的 coverage/defect yield；
- 所有 SOTA 已实现。

## 7. 与 F433 证据的关系

F433 的实现、测试和原始数据位于：

- [`Realtime_Checked_QFBV_Proof_Stream_F433_2026-08-18.md`](Realtime_Checked_QFBV_Proof_Stream_F433_2026-08-18.md)
- [`../evidence/f433-realtime-proof-stream-2026-08-18/`](../evidence/f433-realtime-proof-stream-2026-08-18/)

本审计只判断“接下来还缺什么”，不把未来论文结果或外部工具数字当作本项目实验结果。
