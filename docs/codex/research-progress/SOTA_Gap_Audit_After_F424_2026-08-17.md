# F424 后 SOTA 技术缺口审计

- 审计日期：2026-08-17
- 当前复核基线：F00--F434 当前工作树；第2--4节保留F424时点的原始缺口及其演进依据
- 结论：仍有未完成工作；F425--F433已关闭原生原子回放、跨worker公式/证明/lemma/生命周期、
  Linux同机Z3原生状态、input-byte变量置换core，以及bit-blasted/activation-scoped/持久proof-DAG
  和project-native mid-solve checked proof stream阶段，但不等于原生wire互操作、动态分布式资源重调度和公开实验全部复现

## 1. 判断标准

本审计把“实现”拆为四层，避免把同名配置或有限 oracle 当成 SOTA 全量复现：

| 等级 | 含义 |
| --- | --- |
| I | 生产执行路径存在，默认或显式配置后可被真实 worker 消费 |
| T | 正反例、持久恢复、版本兼容和失败关闭测试通过 |
| E | 有独立 oracle 或机制实验，结论严格限定于支持域 |
| R | 在公开目标上完成等 CPU、多轮、可复现的统计实验 |

只有论文核心语义进入生产 consumer 并达到 I/T/E，才称“机制落地”；只有达到 R，才允许声称
覆盖率、time-to-coverage 或 defect yield 提升。论文使用不同语言、执行引擎或威胁目标时，还要区分
“迁移其一般原理”和“复现其完整系统”。

### 1.1 F425--F433后的关闭状态

| 原缺口 | 当前状态 | 尚未关闭的严格边界 |
| --- | --- | --- |
| native ConDPOR/C11 | F425完成native原子值、两阶段commit、SC/TSO/RA关系图证书与fresh-process回放 | RC11 mixed-size/fence/RMW、herd7/diy/GenMC对拍、弱内存RF驱动执行与无界最优性 |
| 跨worker增量context | F426--F432完成formula/proof/lemma/lifecycle/COW/substitution/bit-blasting和持久proof DAG；F433完成project-native mid-solve proof event、CaDiCaL IPASIR-UP checked import、native ACK和learned RUP回写 | ImpCheck原生wire adapter、Mallob动态重调度、failed-set最小化、自适应流控、跨地域存储、形式化checker与公开多节点实验 |
| solver-native state | F430完成Z3父prefix预热、Linux COW per-target child、timeout隔离和严格状态协议 | 一父多child自适应配额、跨节点可移植state、跨版本协商；后两项仍是研究问题而非现成Z3 ABI |
| Agolic运行级规划 | F374 planner与F423有界continuation runner已进入生产路径 | 真实KLEE/公开目标adapter、七目标等CPU多轮复现 |

因此下文原P0第3、4项不能再表述为“完全未实现”，而应表述为“受限域I/T/E完成，论文完整域和
R级证据未完成”。F430--F433的权威边界分别见
[`Native_QFBV_Solver_State_Fork_Reuse_F430_2026-08-17.md`](Native_QFBV_Solver_State_Fork_Reuse_F430_2026-08-17.md)和
[`Variable_Substitution_UNSAT_Core_Reuse_F431_2026-08-17.md`](Variable_Substitution_UNSAT_Core_Reuse_F431_2026-08-17.md)、
[`Verified_Incremental_QFBV_SAT_and_Proof_DAG_F432_2026-08-17.md`](Verified_Incremental_QFBV_SAT_and_Proof_DAG_F432_2026-08-17.md)和
[`Realtime_Checked_QFBV_Proof_Stream_F433_2026-08-18.md`](Realtime_Checked_QFBV_Proof_Stream_F433_2026-08-18.md)。

### 1.2 2026-08-17新增一手文献缺口

1. **分布式增量SAT实时证明检查**：F433已经实现TACAS 2026/ImpCheck启发的project-native实时
   checked-import语义，但载荷是JSON proof CAS/SQLite event stream，不是论文工具的原生wire协议。
   剩余P1是adapter与自适应流控，P0是公开多节点R级效果验证。
   [TACAS 2026](https://doi.org/10.1007/978-3-032-22752-2_18)
2. **并行持久证明制品**：SAT 2026 PalRUP以分散并行文件和小型可信组件形成可持久证明；当前CPC
   proof CAS没有并行clause-sharing proof DAG，应与上一项共同评估，而不是直接替换SMT权威链。
   [SAT 2026 PalRUP](https://doi.org/10.4230/LIPIcs.SAT.2026.17)
3. **多模态SMT选择**：CP 2026 SMT-Select融合公式AST图和应用上下文文本，并在九种logic上评测；
   当前项目有结构特征、portfolio、序列优化和SMBO，但没有该图/语言联合表示、版本化模型制品及
   跨logic训练/回放合同，应列为P1研究增强。
   [CP 2026 SMT-Select](https://doi.org/10.4230/LIPIcs.CP.2026.41)
4. **最新增量SAT能力面**：CaDiCaL 3.0增加clausal congruence closure、equivalence sweeping、
   bounded variable addition、assumption下implied literal和带hint线性proof；项目尚无将该能力作为
   bit-blasted QF_BV可验证backend的生产适配。
   [SAT 2026 CaDiCaL 3.0](https://doi.org/10.4230/LIPIcs.SAT.2026.40)

## 2. F424 已关闭与未关闭的边界

F424 已完成：

1. persistent QF_BV helper 的加权 input-variable relation graph；
2. 共享边界 `PC_c/PC_r`、partial-model binding 和有界 completion；
3. 原完整 prefix+target SAT 复核，局部 UNSAT/unknown/timeout/exception 全部 fallback；
4. Prefix DAG 的 Laplace branch transition、novelty reward 和循环同步 value iteration；
5. QueryStore 严格 telemetry、自配置、独立有限 oracle、双 LLVM 和完整 Python 门禁。

仍未关闭：论文的 METIS graph partition、operator Bag-of-Words SVM timeout predictor、JFS、KLEE、
QF_BVFP，以及 GSL/Cephes/FDLIBM 公开实验。因此 F424 是 I/T/E-mechanism，不是 R。权威来源：
[Selective Concolic Testing, FM 2026](https://link.springer.com/chapter/10.1007/978-3-032-26220-2_14)。

## 3. 尚未完成的核心工作

### P0：直接影响并行符号执行主链

| 顺序 | 缺口 | 当前已有 | 完成定义 |
| --- | --- | --- | --- |
| 1 | FM 2026 完整 solver/partition/predictor 域 | F424 QF_BV 静态风险切分与精确 fallback | 版本化 predictor artifact、可替换图划分、BVFP/JFS 等价 backend、公开函数消融与 R 级实验 |
| 2 | 原生/公开目标 Agolic adapter | F374 planner + F423 有界 continuation BSE runner | 调用真实 KLEE/目标程序的 adapter、运行身份与资源隔离、artifact replay、七目标或等价公开集的等 CPU 多轮实验 |
| 3 | native ConDPOR + C11/C++ 内存模型 | pthread preload trace/prefix、Source-DPOR、SC interpreter、独立 TSO/RA SMT 编码 | LLVM/pthread 动态事件进入可重生成 execution graph；SC/TSO/RA/C11 同一 consumer；herd7/diy 对拍；maximal extension 证据 |
| 4 | 跨 worker 增量 solver context | F426--F432已有formula/proof/lemma/lifecycle/COW/substitution/bit-blasted context；F433已有mid-solve checked clause/proof event和native ACK | 原生wire adapter、failed-set最小化、自适应流控、动态分布式资源重调度、形式化checker及多节点R级实验 |

其中 ConDPOR 的权威机制边界来自
[CONCUR 2025 paper](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.CONCUR.2025.26)；
Agolic 的公开设计与结果来自
[Agentic Planning for Symbolic Execution](https://arxiv.org/abs/2608.06397)。

### P1：扩大语义完备域

| 顺序 | 缺口 | 尚缺内容 |
| --- | --- | --- |
| 5 | 通用 native/external/heap continuation | callback、varargs、custom allocator、线程共享对象、未知外部 effect、更加一般的 heap/points-to 和跨过程恢复 |
| 6 | 完整结构化输入求解 | Cottontail ECT 的生产级 LLM solve/seed acquisition；Lase token-level online grammar synthesis/search；SymCC-str mixed String/BV/FP/Array 与 conversion/endptr/base 语义 |
| 7 | 完整 Pangolin/TaichiPoly generator | 非线性保守抽象、sign/bitfield pack-unpack、可验证整数 projection artifact、OMT 最优 bounds、真实 cache-hit/yield 实验 |
| 8 | 完整 UCSan/POSE 与 Hydra/IFSS | global/DFSan/callback/varargs/concurrent shadow、initial symbolic heap、一般 nested/overlapping/EH region 和跨函数转换 |

结构化输入方向的主来源是
[Cottontail, IEEE S&P 2026](https://mboehme.github.io/paper/SP26-cottontail.pdf) 与
[Lase, OOPSLA 2026](https://zbchen.github.io/files/oopsla2026.pdf)；polyhedral 路径抽象的基线是
[Pangolin](https://home.cse.ust.hk/~charlesz/papers/pangolin.pdf)。当前工程包含 ECT、grammar/SPPF、
String backend 和多类精确 poly reuse，但没有达到这些系统的完整支持域和公开实验。

### P2：研究增强与确认性证据

| 顺序 | 缺口 | 准入原则 |
| --- | --- | --- |
| 9 | reactive agentic discovery | 实现 coverage plateau / solver failure / library barrier 触发器；LLM 只提 proposal，solver 或 concrete execution 永远负责裁决 |
| 10 | pointer-risk MCTS 等专项策略 | 静态 type-unsafe pointer 证据进入 MCTS/Prefix DAG，但不得把风险分数当作错误证明；先做 coverage-neutral 消融 |
| 11 | 完整 R 级实验 | 相同 CPU、初始 corpus、随机种子和预算；不少于 20 次独立运行或预注册功效设计；AUC、最终覆盖、solver CPU、候选有效率；paired bootstrap/randomization、多重校正和原始数据封存 |

Reactive LLM discovery 的最新直接参照是
[ConcoLixir](https://arxiv.org/abs/2606.26545)，其关键原则同样是“LLM 为 discovery oracle，
观察到的执行结果才是反馈”；专项 pointer-risk 路径调度参照
[Vital](https://arxiv.org/abs/2408.08772)。二者对当前 C/C++ coverage 主线是 P2，不应抢在并发语义、
跨 worker solver context 和公开实验之前。

## 4. F433后的推荐实施顺序

1. F432已完成bit-blasted QF_BV、activation assumptions、solve-boundary checked clause和持久proof DAG；
2. F433已完成project-native ImpCheck/LIDRUP式mid-solve proof stream、checked-import ACK和learned RUP回写；
3. 优先执行公开多节点R级评测，再依据checker/transport数据实现原生wire adapter、自适应流控和Mallob式动态资源重调度；
4. 完成native ConDPOR的RC11 mixed-size/fence/RMW与herd7/diy/GenMC对拍；
5. 完成真实Agolic/KLEE公开目标adapter，并冻结等CPU运行身份；
6. 扩大continuation/external/heap、Cottontail/Lase/String、Pangolin和UCSan/POSE一般域；
7. 将SMT-Select式模型和ConcoLixir式reactive agent仅接入不可信proposal/selection plane；
8. 冻结版本后执行不少于20轮等CPU公开实验。

这个顺序先封闭“结果是否可信”和“状态能否真实执行”，再优化路径选择。否则调度器即使在有限
oracle 上表现更好，也无法区分算法收益、语义缺失和实验噪声。

## 5. 当前可对外表述

可以表述：项目已有435个连续功能ID，覆盖并行调度、持久Prefix DAG、solver portfolio/cache、
结构化输入、并发trace/有限interpreter、continuation、polyhedral reuse、agentic proposal、
F424 selective-concolic机制，以及F425--F433原子回放、跨worker QF_BV制品、本地原生状态、
proof-carrying变量置换core、增量SAT proof-DAG与mid-solve checked stream；当前大量模块已达到I/T/E。

不能表述：所有 SOTA 已完整实现，或论文报告的提升已由本项目复现。剩余 P0/P1 是实际语义和系统
工程缺口，P2/R 是将“机制正确”升级为“公开条件下有效”的证据缺口。
