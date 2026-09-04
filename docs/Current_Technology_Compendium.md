# SymCC-Parallel 当前新增技术全景与实现说明

- 快照日期：2026-07-30
- 覆盖范围：当前工作树相对上游 SymCC 新增或显著扩展的 `F00-F296`
- 读者对象：第一次接触符号执行的工程人员、系统研究人员、项目汇报与论文写作者
- 权威明细：[`New_Implementation_Archive.md`](New_Implementation_Archive.md)
- 完整配置：[`Configuration.txt`](Configuration.txt)
- 历史追踪：[`Development_History_Traceability.md`](Development_History_Traceability.md)

## 1. 先给出结论

当前项目已经不再只是“给 SymCC 加一个 MPI master”。它形成了七个彼此连接的
研究层：

1. **编译与语义层**：在 LLVM IR 中提取稳定站点、控制依赖、数据依赖、内存状态
   和可恢复 continuation，并对难以直接符号化的控制流做有界、证明门控的变换；
2. **运行时观测层**：记录路径、求解、数据进展、字符串操作、线程事件和候选来源；
3. **统一表示层**：用 PrefixDAG、ECT、Query IR、grammar/SPPF、schedule artifact
   和 content-addressed live state 保存可复用的结构；
4. **求解与候选层**：组合精确 Z3、QF_BV/String portfolio、Pangolin 式上下文复用、
   PSCache 式部分解、Backsolver、乐观/选择性求解和结构化候选生成；
5. **并行探索层**：同时支持 seed 级 MPI workers、solver 级 portfolio race、
   schedule 级 DPOR 和 continuation state 级 frontier；
6. **反馈闭环层**：普通 corpus 由 AFL edge novelty 接纳；data coverage、内容摘要、
   结构新颖度和执行成本作为调度、去重与独立证据 campaign 的信号；
7. **科研证据层**：把 manifest、seal、独立 replay、随机化配对协议、消融和
   claim gate 作为实现的一部分，而不是实验结束后临时补日志。

最重要的系统不变量是：

> **输入 proposal 不能绕过完整当前约束验证和真实目标程序重放；编译变换不能绕过
> proof gate 与未变换基线 replay；进入普通 corpus 的候选最终必须通过全局 AFL
> edge novelty 仲裁。**

这使项目可以激进地引入新技术，同时把不完备方法隔离在正确性可信边界之外。

![当前技术栈总览](diagrams/technology_compendium/technology-stack.svg)

### 1.1 阅读路线

- **五分钟了解系统**：阅读第 1、3、4、22 节，先建立执行流、bitmap 和可信边界；
- **准备部署或调试**：继续阅读第 2、7、20、21 节，重点核对默认开关、产物和测试；
- **理解算法实现**：按第 9-18 节的顺序阅读，从 PrefixDAG、Query IR 到 solver、
  grammar、schedule、continuation 和 CFG 变换；
- **准备论文或实验**：阅读第 19、23-25 节，并回到逐项档案核查 ID、证据和已知边界。

### 1.2 术语速查

| 术语 | 本文中的含义 |
| --- | --- |
| concolic / DSE | 用一次真实执行收集符号表达式，再求解相反分支约束的动态符号执行 |
| prefix | 到当前目标分支之前已经走过的路径约束序列 |
| candidate / proposal | 求解器或启发式方法生成的候选输入；尚未因此获得接纳资格 |
| replay | 用候选重新运行 evaluator、未变换基线目标或 AFL-instrumented target |
| Query IR | 与具体 solver 解耦、可持久化并内容寻址的查询表示 |
| ITE | `if-then-else` 表达式，用一个值表达多条控制流路径的合并结果 |
| QF_BV | 无量词位向量逻辑，SymCC 整数路径约束的主要 SMT 子域 |
| CAS | content-addressed storage，以内容摘要而不是可变文件名寻址对象 |
| ECT | Expressive Coverage Tree，保存路径/上下文/结构新颖度的树 |
| SPPF / PCFG | 共享打包解析森林 / 概率上下文无关文法 |
| DPOR | Dynamic Partial-Order Reduction，用依赖关系减少等价线程调度 |
| SC / TSO / RA | 顺序一致、总存储序和 release-acquire 内存模型 |
| I2S / JIGSAW | input-to-state 直接反演 / JIT 梯度约束搜索；由 SymSan RGD 栈编排 |
| seal | 绑定输入、工具、配置和产物摘要的本地证据封装，不是数字签名 |

## 2. 如何理解“已经实现”

### 2.1 结果等级

项目文档使用两类标记，不能把它们读成一条线性的成熟度阶梯：

| 等级 | 含义 | 可以说什么 | 不能说什么 |
| --- | --- | --- | --- |
| I | 已接入真实代码路径 | “已经实现并集成” | 不能据此声称性能提升 |
| T | 有单元、LLVM lit 或集成测试 | “已通过功能回归” | 不能代表所有真实程序 |
| B | 有明确环境和预算的本地测量 | “在该配置下测得……” | 不能外推为普遍优势 |
| R | 等 CPU、多轮、置信区间、效应量和消融 | “实验支持该性能结论” | 仍不能超出实验总体外推 |

`I/T/B/R` 描述证据成熟度；`E` 是正交的**可执行证据属性**，表示有 manifest、
seal、独立 replay 或 claim gate 可重算部分结论。一个功能可以是 `I/T/E`，但并不
因此达到 `B` 或 `R`；`E` 也不是形式化证明、密码学签名或远程证明。

当前多数功能达到 I/T；后期研究路径大量达到 I/T/E；早期并行、公开 benchmark 和
SymSan 路径包含 B 级历史结果。**最新全功能组合尚未整体达到 R**，所以本文不会把
“机制已实现”写成“系统已在公开 benchmark 上达到 SOTA”。

### 2.2 启用方式

功能是否存在与是否默认启用是两件事：

| 运行方式 | 实际默认行为 | 需要显式启用的代表能力 |
| --- | --- | --- |
| 单独运行 instrumented target | 普通 SymCC/QSYM concolic；Backsolver 默认可用，但仅对含 ITE 的目标生效；telemetry 文件和 data-coverage telemetry 默认不输出 | `SYMCC_TELEMETRY_OUT`、`SYMCC_DATA_COVERAGE=1`、跨前缀 poly reuse、portfolio |
| `mpi_fuzzing_helper.py` | `SYMCC_ADAPTIVE_SCHEDULER=1`；worker 自动设置 telemetry 与 `SYMCC_DATA_COVERAGE=1`；PrefixDAG/ECT/self-config 等自适应组件默认打开；poly cache 可用时 helper 以 `setdefault` 打开跨前缀、投影和字段重命名 | 可以逐项设 `0` 做消融；async query workers、外部 solver portfolio 仍需配置 |
| AFL 原生 data coverage | 只有将 `libafl_data_coverage_rt.so` 通过 `AFL_PRELOAD`/`LD_PRELOAD` 注入 AFL target 时，comparison progress 才写入 AFL 共享 map | preload 路径及 `AFL_DATA_COVERAGE`；它与 `SYMCC_DATA_COVERAGE` telemetry 不是同一开关 |
| 编译期研究变换 | pass 会注册，但 IFSS/Hydra/UCSan 的变换开关默认关闭 | `SYMCC_IFSS_*`、`SYMCC_HYDRA`、`SYMCC_UCSAN_ENTRY` 等必须在编译目标时设置 |
| 状态/调度/语法研究路径 | 普通 seed 闭环不自动启动 live continuation、DPOR、native parser campaign 或独立 evidence campaign | `SYMCC_LIVE_*`、`SYMCC_DPOR`、parser/PCFG、query portfolio 和 seal/replay 配置 |

因此“项目记录了 297 个功能 ID（F00–F296）”不表示一次 campaign 会同时执行 297 条路径。
超预算、证据不完整、语义不支持或验证失败时，功能应 fail closed 或退回受支持的
精确路径，而不是静默近似程序语义。

`Configuration.txt` 当前记录 375 个唯一 `SYMCC_*` 配置名。本文只解释配置族，
具体默认值、上限和产物 schema 以该文件为准。

### 2.3 学术技术来源与本项目差异

“借鉴某论文”不等于“复现该论文”。下表明确区分原始思想和本项目实际落地：

| 来源 | 对应实现 | 本项目采用的核心思想 | 没有冒充的部分 |
| --- | --- | --- | --- |
| [QSYM](https://www.usenix.org/conference/usenixsecurity18/presentation/yun) | 上游 QSYM backend；F00/F01/F03 的部分集成基础 | native execution、轻量表达式、混合 fuzzing | F02 Backsolver、F04 data coverage 等扩展不是 QSYM 原功能 |
| [S2F](https://arxiv.org/abs/2601.10068) | F05 | PrefixDAG、high/low queue、困难分支 sampling | 不是论文完整系统与实验复现 |
| [TACO-Fuzz](https://2026.splashcon.org/details/oopsla-2026/35/Efficient-Directed-Hybrid-Fuzzing-via-Target-Centric-Seed-Selection-and-Generation) / [MultiGo](https://doi.org/10.1145/3735555) | F05 的目标调度；F06 提供部分静态基础 | target-centric、路径多样性、Poisson 难度 | 仅实现可审计的有界调度子集 |
| [CoFuzz](https://doi.org/10.1109/ICSE48619.2023.00045) | F08/F15 的在线收益与离线观测；F17-F22 提供协调基础 | 联合观察调度、同步和后续 coverage yield | 当前策略不是 CoFuzz 边级回归的忠实复现 |
| [Pangolin](https://doi.org/10.1109/SP40000.2020.00063) | F03、F176-F185 | polyhedral context、增量复用、多解采样 | 新增跨前缀/跨布局路径也不传播 UNSAT |
| [GenSlv](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/44/Generator-Solving-for-Symbolic-Execution) | F29、F177-F178、F181 | reusable generator、range sampler、model converter | generator 输出必须经过原查询复核 |
| [PSCache](https://doi.org/10.1145/3660817) | F32、F35、F236 | conflict-derived partial assignment 与相关查询复用 | 未宣称实现论文全部 bit-blast trail 技术 |
| [SMTgazer](https://conf.researchr.org/details/ase-2025/ase-2025-papers/47/SMTgazer-Learning-to-Schedule-SMT-Algorithms-via-Bayesian-Optimization) | F09、F239-F243 | censored-cost algorithm sequence 和 SMBO | 不是私有模型/数据集的复刻 |
| [SymCC-str](http://theory.stanford.edu/~barrett/pubs/CB26-abstract.html) | F30、F33、F193-F200 | BV/String 双表示和 libc string artifact | 只覆盖明确列出的 C string/conversion 子域 |
| [Lase](https://2026.splashcon.org/details/oopsla-2026/43/Online-Input-Grammar-Synthesis-Aided-Symbolic-Execution) | F36、F187、F190-F192 及后续 grammar 机制 | 在线 token grammar、结构化输入生成 | parser/PCFG 扩展是本项目有界实现 |
| [Cottontail](https://mboehme.github.io/paper/SP26-cottontail.pdf) | F07、ECT 与部分 agentic 接口 | context/path-sensitive expressive coverage | 没有把 ECT 写成 Cottontail 完整 LLM solve-complete 复现 |
| [Gordian](https://arxiv.org/abs/2603.19239) / [NeuroSCA](https://arxiv.org/abs/2603.01272) | F10 的 proposal/verifier **扩展点** | inverse/exemplar/core proposal 应位于不可信平面 | 未实现论文完整 ghost-code 迭代或训练型神经算术 solver，不计作已实现 backend |
| [ParaSuit](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/222/Enhancing-Symbolic-Execution-with-Self-Configuring-Parameters) | F186 | 条件参数图、上下文选择和 transfer prior | 未复刻 KLEE 参数爬取和论文聚类实验 |
| [UCSan](https://www.usenix.org/conference/osdi26/presentation/yin) | F11 | 编译式 under-constrained harness、对象图和 shadow pointer | 结果仍需真实调用环境验证 |
| [GenSym](https://conf.researchr.org/details/icse-2023/icse-2023-technical-track/39/Compiling-Parallel-Symbolic-Execution-with-Continuations) | F38、F59、F66、F68-F175 | continuation、状态级并行、持久 store/memory | 只支持保守 LLVM 子集，不是完整 GenSym 编译器 |
| [ConDPOR](https://doi.org/10.4230/LIPIcs.CONCUR.2025.26) | F39-F58、F61-F65、F221 | 输入与 schedule 联合、po/rf/co、backward revisit | 当前是 bounded exploration，不声称完备证明 |
| Backsolver / IFSS / [Hydra](https://doi.org/10.1145/3798202) | F02、F37、F248-F296 | ITE/state merge、fork elision、failure-preserving transformation | IFSS 是项目内有界实现族；Hydra 变换默认关闭且必须 replay/manifest 验证 |

完整论文链接、论文原始结论和逐项差距见
[`sota_hybrid_execution_2026.md`](sota_hybrid_execution_2026.md) 与
[`SOTA_Gap_Audit_2026-07-24.md`](SOTA_Gap_Audit_2026-07-24.md)。

## 3. 两条执行主线

### 3.1 Native concolic + AFL++ 闭环

这是当前最成熟的执行主线，单个工作项按以下顺序运行：

1. AFL++ 或 corpus 产生种子；
2. coordinator 扫描队列，结合 PrefixDAG、ECT、目标距离、历史收益和 worker 状态
   选择工作项；
3. master 向 worker 下发种子对象、executor/profile、focus set、目标分支、schedule
   prefix 和 AFL edge-bitmap 版本或增量；
4. instrumented target 真实执行，QSYM/SymCC runtime 构造符号表达式和路径约束；
5. QSYM 的私有 branch-interest bitmap 判断某分支是否值得再次求解；
6. runtime 内的 exact、tailored/Backsolver、sampling 或本地 string backend 可直接
   产生候选；QueryStore、grammar、agentic 等异步路径通过各自 artifact/sidecar
   产生候选，但最后汇入相同 triage；
7. worker 先用 128-bit BLAKE2b 摘要做有界的实用内容去重，再用
   AFL-instrumented target 的 streaming/batched showmap 重放候选；
8. worker 的本地 AFL edge bitmap 做预过滤，只上传本地判新的 sparse edges；
9. master 在自己的会话级 edge bitmap，或多 coordinator coverage-owner shard 上
   做最终 novelty claim；只有全局 edge delta 非零的候选才写入普通 SymCC/AFL queue；
10. master 消费 telemetry，更新 PrefixDAG、ECT、data-progress tracker、策略后验、
    coverage owner 和下一轮工作优先级。

![单工作项执行次序](diagrams/qa3/fig-1-3-worker-sequence.svg)

#### 3.1.1 一个最小端到端例子

假设输入 `A0` 到达 `if (x == 0x42)`，当前走 false 分支：

1. runtime 用稳定 site ID 和 taken=false 更新 QSYM branch-interest map，并构造
   `x != 0x42` 的已走路径与 `x == 0x42` 的翻转目标；
2. direct fast solve 可能直接把依赖字节改成 `0x42`；复杂情况再进入当前 prefix
   context、Backsolver 或 Z3；
3. 候选 `B0` 先在 worker 内按内容摘要去重，然后由 AFL target 重放得到
   `[(edge_17, count_1), (edge_31, count_1)]`；
4. 若 worker 本地 bitmap 已有两条边，候选在 worker 被丢弃；否则 sparse edges
   随候选上传；
5. 若 master 全局 bitmap 仍缺 `edge_31`，master 原子接纳 `B0` 并广播 coverage
   delta；若另一 worker 已先声明该边，`B0` 不进入普通 queue；
6. 无论最终是否接纳，solver 状态、候选数、data progress 和耗时都会进入反馈，
   但调度奖励不能代替全局 edge novelty。

### 3.2 Continuation/CAS 状态级执行

该路径不是“多启动几个相同进程”，而是把可恢复符号状态作为工作：

1. LLVM 子集被降低为 executable continuation IR；
2. checkpoint 保存 program counter、symbolic store、path-condition root、
   page-COW memory、对象生命周期和动态选择计数器；
3. 状态内容以 CAS 哈希寻址，coordinator 只租约分发状态描述符；
4. worker 恢复 continuation，在有界步数内执行并在符号分支处 fork；
5. child 的 path condition 追加一个 solver-frame delta；symbolic store 重新内容
   寻址，未改 memory page 通过 page-COW root 共享；
6. halted candidate、frontier checkpoint 和失败原因一起提交；
7. master 通过 fencing token 和内容哈希拒绝过期、重复或被篡改提交；
8. 可具体化的输入仍进入与 native 路径相同的目标重放和 AFL 仲裁。

![Native 与 continuation 两条执行流](diagrams/qa3/fig-1-0-execution-modes.svg)

Continuation 路径当前覆盖的是**明确有界的 LLVM 语义子集**，并不是任意 C/C++ 程序
的完整 GenSym 复刻。遇到异常处理、无法证明的别名、超预算 pointer domain、
不支持的 intrinsic 或复杂循环时会保守拒绝 lowering。

## 4. 位图、集合与哈希空间

项目中“bitmap”不是一个对象的多个名字。混淆它们会直接导致重复求解判断错误。
下表的 `B1-B6` 是本文和 `Architecture_QA3.md` 为讲解定义的标签，不是源码类型名。

| 名称 | 生成者 | 主要用途 | 是否拥有最终接纳权 |
| --- | --- | --- | --- |
| B1：master AFL coverage | AFL-instrumented target 的 showmap 结果 | 会话级 edge novelty 最终仲裁 | 是 |
| B2：worker local coverage | worker 对候选重放后的稀疏 edge | 本地预过滤、减少 MPI 流量 | 否，需提交 B1 |
| B3：QSYM branch-interest map | symbolic branch site/outcome 哈希 | 判断路径分支是否值得求解 | 否 |
| B4：coverage owner shards | 多 coordinator 的分片持久状态 | 跨 master ownership、claim 与 gossip | 仅在多 master 模式代表全局 claim，受 fencing/owner 规则约束 |
| B5：AFL 原生 data namespace | preload runtime 写入 AFL shared map | 让 AFL 自身把 `memcmp` 等 prefix progress 当作 map feature | 注入 AFL target 时由 AFL 与 edge 共同保留；普通 MPI triage 未注入时无此权力 |
| B6：grammar bitmap | parser production/rule ID | 结构化输入新颖度和调度 | 否 |
| 显式键 data novelty set | `(object, offset, width, kind)` | scheduler 内按结构键记录 data-progress dominance | 否 |
| 内容摘要 | worker 输入用 128-bit BLAKE2b；持久对象/队列另有 SHA-256 | 实用去重与内容寻址 | 否；摘要碰撞概率极低，但不是数学上的字节等价证明 |

B3 与 B1/B2 使用不同 ID 空间：B3 关注符号分支及其上下文，B1/B2 关注 AFL
instrumented binary 的覆盖边。`SYMCC_AFL_COVERAGE_MAP` 在 worker 中实际指向
131072-byte 的 QSYM `qsym_bitmap`，不能拿 AFL edge ID 去索引它。B5 是兼容 AFL
的有限 namespace，仍可能发生哈希碰撞；scheduler 的 data tracker 则用显式结构键
记录最佳 matched bits。F247 是独立研究流程：在同一 instrumentation identity 下
分别用 edge-only 与 edge+data showmap 重放相同 candidate/witness，封存 sparse map、
重复稳定性和 feature calibration；它不改变普通 MPI queue 的 edge-only 接纳条件。

![位图与哈希空间](diagrams/qa3/fig-1-2-bitmap-spaces.svg)

## 5. 技术族总表

| 技术族 | 主要功能 ID | 解决的问题 | 主要入口 | 成熟度概览 |
| --- | --- | --- | --- | --- |
| 基础正确性 | F00、F26 | 位宽、线程局部状态、稳定 ID、cache/replay soundness | `compiler/`, `runtime/src/` | I/T |
| MPI 与 hybrid 基础 | F17-F25 | 并行吞吐、AFL 协同、引擎抽象、交付 | `util/mpi_*`, `benchmark/` | I/T/B |
| 遥测与覆盖 | F01、F04、F06-F07、F22、F28、F247 | 看见路径、数据和结构进展并正确判新 | `hybrid_feedback.py`, coverage runtime | I/T/E |
| 调度与目标引导 | F05、F08-F10、F14-F16、F186 | 把有限 CPU 分给更有价值的种子和策略 | adaptive/self-config/scheduler | I/T，部分 B/E |
| Query IR 与生成器 | F27、F29、F181 | 持久化查询、重放 converter、批量生成解 | `query_store.py`, `solution_generator.py` | I/T/E |
| 求解复用 | F03、F32、F35、F176-F185、F236、F238 | 减少重复翻译、重复 SAT/UNSAT 和相关查询成本 | QSYM solver、QueryStore | I/T/E |
| Solver portfolio | F02、F31、F34、F177-F178、F188-F189、F237-F246 | 处理长尾、选择算法、控制近似 | query service、QF_BV tools | I/T/E |
| 字符串双表示 | F30、F33、F193-F200 | 把 libc/string 语义接到 BV 和 String solver | wrappers、string tools | I/T/E |
| 语法与解析森林 | F36、F187、F190-F192、F201-F220、F222-F235 | 学习并验证结构化输入、PCFG 和 parser 状态 | semantic/parser modules | I/T/E |
| Under-constrained execution | F11 | 自动构造函数级 harness 与对象图 | `compiler/UCSan.*`, runtime | I/T |
| 并发状态空间 | F13、F39-F58、F61-F65、F221 | 联合探索输入、schedule 和 memory model | schedule runtime/exploration | I/T/E |
| Live continuation | F12、F38、F59、F66、F68-F175 | 状态级并行、可恢复内存和 LLVM 语义 | continuation compiler/runtime | I/T/E，有界子集 |
| IFSS/Hydra 变换 | F37、F248-F296 | 减少 fork、恢复多出口/循环/内存状态 | compiler transformation passes | I/T/E，默认关闭 |
| 证据与 benchmark | F16、F21、F25、F60、F67 及各 sealed campaign | 可复现评测、独立复核和克制的科研表述 | benchmark/research tools | I/T/B/E |

技术族是多对多视图，同一 ID 可以同时服务调度、solver 和 evidence。第 24 节为避免
重复只给每个 ID 分配一个“主层”；表中范围重叠不表示重复实现。

下面按执行链解释每个技术族。

## 6. 编译器与运行时正确性基础

### 6.1 位宽、线程和站点身份

F00 修复并扩展了符号表达式的基础语义：

- `inttoptr`、`ptrtoint` 按 LLVM `DataLayout` 做扩展或截断，而不是假定 64 位；
- runtime 参数表达式槽和返回表达式改为 `thread_local`，避免多线程目标互相污染；
- test-case handler 可显式保存已经验证的 concrete values；
- QSYM 专属测试按 backend capability gate 运行。

F26 是一次跨层 correctness hardening：

- bit-vector 线性化只有在证明无回绕时才进入整数/polyhedral reasoning；
- poly cache key 绑定完整前缀和表达式上下文；
- fast solve、cache model 和外来 poly sample 都重新验证当前目标；
- LLVM site ID 由稳定模块/位置身份产生，避免 ASLR 和构建顺序污染；
- UCSan shadow copy、multi-master fencing 和 benchmark 字段一致性被重新审计。

这些工作不直接增加覆盖率，却决定所有后续优化是否有可信基础。尤其是“把位向量
当整数”和“跨前缀复用 UNSAT”都可能产生静默漏解，因此项目只允许经过证明的
线性子域，并禁止跨前缀传播 UNSAT。

## 7. MPI 并行与 AFL++ 混合闭环

### 7.1 从串行 seed 到分布式工作

F17 建立 master/worker 协议：worker 声明就绪，master 分发内容寻址的种子与策略，
worker 返回稀疏覆盖和小型 telemetry。协议包含原子文件、非阻塞消息资源回收、
终止握手、超时区分和大规模多 coordinator 路径。

F18 把 AFL queue 增量送入 SymCC，把经过真实 showmap 判新的候选送回 AFL。关键优化
包括 worker-side triage、streaming/batched showmap、增量目录扫描、内容哈希预去重、
coverage delta 和各阶段交错执行。历史固定配置测量曾记录明显的 I/O 与通信下降，
但这些数字是 B 级机制结果，不是跨目标平均加速。

F19 继续解决扩展性：

- focus bytes/focus set 对输入依赖域分区；
- branch-density-balanced partition 避免等长切分造成难度偏斜；
- work stealing 和 adaptive granularity 消化 worker 空闲；
- persistent mode 与 shared-memory testcase 减少启动/I/O；
- 报告 active worker 和总 CPU，而不是只报 `-np`。

### 7.2 异构执行引擎

F20 把 LAF、CTX/NGRAM、CmpLog、MOpt、GRIMOIRE 和 honggfuzz 作为互补候选来源；
F23 用 `ConcolicEngine` 契约统一 SymCC 与 SymSan；F24 把选择性符号化、dictionary、
multi-field hints 和 RGD 的 I2S/JIGSAW/Z3 级联接入 SymSan。所有引擎仍共享
coverage triage、调度和冗余统计，避免评测逻辑因 backend 不同而漂移。

F25 提供可重复安装、离线 bundle、相对 RPATH 和 bare-machine/debootstrap 验证。
它解决“代码能跑”和“别人能重建同一实验环境”之间的工程缺口。

## 8. 遥测、覆盖与结构反馈

### 8.1 统一 SolverTelemetry

F01 把每次执行归一为 engine-neutral telemetry：

- 进程状态：signed return code、timeout、killed、elapsed；
- 路径状态：path hash、open branches、bounded branch trace；
- 求解状态：SAT/UNSAT/unknown/timeout、算法、成本、cache hit；
- 依赖状态：comparison taint、input offsets、结构 context；
- 结果状态：generated、validated、accepted 和拒绝原因；
- capability/missing fields：避免把 backend 缺失字段误当作零。

这使 scheduler 能同时比较 SymCC、SymSan、exact、sampling 和 semantic proposal，
又不会依赖某个求解器的进程内对象。

### 8.2 Data Coverage

F04/F28 将“距离常量还有多远”变成独立反馈：

- 整数 equality 使用相等 bit 数；
- ordered comparison 使用相等高位前缀；
- switch 只探测相邻 case 或范围端点；
- `memcmp/bcmp/strcmp/strncmp` 记录有界字节区域进展；
- 编译期常量全局对象用稳定 module/object ID 注册；
- 未插桩 DSO 的只读 `PT_LOAD` 可在启动时注册；
- runtime 地址归一化为 `(object_id, byte_offset, width, kind)`。

显式结构键 novelty set 负责逐 feature 的精确 dominance；有限的 AFL data
namespace 负责与 AFL 生态兼容。只有 matched bits 严格提升时才替换 scheduler 中的
feature winner，而且该 tracker 不会删除 AFL 自有 queue 文件。

### 8.3 结构覆盖和目标引导

F06 组合 directed coloration、静态依赖、并发提示和结构任务图：

- ColorGo 风格的静态/动态 coloration 从 target 反向计算 CFG/call distance；
- DynamiQ 风格的结构任务把递归 call-graph SCC 压缩成稳定 region，再按收益、成本、
  停滞和 queue pressure 分配 ownership/work stealing；
- 静态 branch-to-byte 摘要可在不额外执行目标的情况下生成 replay focus；
- 并发图标记 thread lifecycle、lock/condition/barrier/semaphore、atomic 和 fence。

F07 用 Expressive Coverage Tree 表示 observed/open outcome、上下文、重复形状和结构
稀有度；Empc 风格 multiple minimum path cover 先折叠 loop SCC，再用不同 maximum
matching 产生多组路径覆盖，并依据真实 trace 缩小 seed 的活动 cover。目标不可行时
退回 predecessor/dependence，而不是持续选择同一条最短路径。

F22 把总耗时拆为取任务、concolic、扫描、去重、showmap、发送等 phase，并建立
redundancy funnel：同 worker 重复、跨 item 重复、跨 worker 重复、coverage 已见和
最终 accepted。没有这些字段，学习型 scheduler 很容易把 I/O 拥塞误认为求解无效。

## 9. PrefixDAG、目标任务与自适应调度

F05 用 PrefixDAG 保存跨 seed 的路径前缀，而不是把一次 execution 当作孤立事件：

- S2F 式 actionseed 把同一 seed 的多个高价值 open branch 批量化；
- exact executor 优先处理明确目标，tailored executor 处理 ITE/隐式流；
- sampling 只分给困难但曾产生收益的前缀；
- TACO 风格目标路径关注 under-explored near-target prefixes；
- MultiGo 风格评分联合目标距离、路径频率、难度和探索保留。

F08 把选择扩展到 seed、worker、executor、strategy 和 parameter；F09 学习
SMT algorithm sequence；F10 提供确定性的 semantic fallback、内置 route、异步
provider 接口和 verifier-gated proposal。当前仓库实现的是 inverse/exemplar/token/
object-partition 等有界数据候选，不包含 Gordian 完整 ghost-code 编译迭代，也没有
把 NeuroSCA 训练模型作为 solver backend。所有外部输出都经过 schema 检查、
Query IR 或 concrete evaluator 验证和目标重放。

具体调度器不是单一 bandit：

- SimiFuzz 风格 LinUCB 对 seed-worker pair 建模，并按固定 time slice 聚合反馈；
- K-Scheduler 风格 frontier score 使用 showmap cache 的稀有边权重；
- TACE 两阶段先执行 bounded no-solve dependency profile，再只符号化 sparse focus；
- Strategy/Component portfolio 分开学习 exact/tailored/sampling、solver、focus 和
  sampling 组件，避免把组合收益错误归给每个开关；
- AdaptiveParallelismController 用带迟滞的 hill climbing 调整 active symbolic
  workers，防止并行度频繁抖动；
- SYMCTS 风格 prefix/edge-dependence 状态为 under-explored branch pair 保留 replay
  机会，而不是只依赖 AFL 的粗粒度边图。

F14 将上述能力组织成 xFUZZ/KRAKEN 风格 AFL profile，组合 LAF/CompCov、CTX、
Ngram、CmpLog、MOpt 和 power schedule；F15 保存 propensity、并行干扰和离线
trajectory，用 clipped IPS、SNIPS、doubly robust、ESS 和 lower confidence bound
做保守 off-policy 评估。F186 又将 ParaSuit 式参数空间升级为带 `active_when`
依赖的机器 schema、上下文后验和只读 transfer prior。失活参数不会被错误归因，
exact profile 也会显式下发关闭近似路径的 hard guard。

这里的“学习”是资源分配，不是把神经网络放进 correctness oracle。预测错误最多浪费
预算，不能让非法候选进入 corpus。

## 10. Query IR 与持久化查询服务

### 10.1 为什么需要 Query IR

直接保存一段 solver 日志很难跨进程、跨 backend 或跨实验复用。F27 建立内容寻址、
版本化的 Query IR：

- 节点、位宽、输入 offset、prefix roots 和 target root 都有规范表示；
- query trie 共享共同前缀；
- QueryStore 以 digest 管理提交、lease、结果、候选和证据；
- async query service 可在 worker 之外消费查询；
- prefix-keyed incremental solver 避免重复翻译同一断言向量。

F29 从 Query IR 中提取可逆 equality、模型和 polyhedral domain，生成多个候选；
F181 把 converter recipe 持久化并逐步/累积重放。未知 operation、冲突 assignment、
越界 offset 或关闭 full-validation obligation 都会被拒绝。

Query IR 已成为多个技术之间的“公共语言”：Pangolin reuse、PSCache、string solver、
grammar hole、schedule joint solving、QF_BV portfolio 和研究证据都可以引用同一查询
身份，而不是各自发明不可比较的日志格式。

## 11. Pangolin 式 polyhedral/Z3 context 复用

### 11.1 同前缀复用

F03 在 QSYM 内保存：

- SAT/UNSAT/timeout 结果；
- Z3 model 修改的输入字节；
- 依赖字节的可行区间 box；
- 规范化线性约束；
- prefix assertion vector 和可重用 Z3 translation context；
- 小 UNSAT core 及其结构 fingerprint。

同一完整 key 下可重放 SAT model、跳过已知 UNSAT、从 polytope 采样或复用 assertion
vector。UNSAT core 只有在当前约束包含被证明的 core 时才能使用。

### 11.2 跨前缀与跨布局 SAT 候选

F176-F185 将复用扩展为一条逐步加固的链：

1. F176：比较不同前缀的规范线性系统，只召回 SAT 候选；
2. F179：对缺失字节做 `[0,255]` 区间消元，得到保守投影 envelope；
3. F180：搜索保持系数/约束结构的字段 byte bijection；
4. F182：用有界整数 Z3 公式精确检查双向蕴含和交集；
5. F183：通过系数结构证明跨长度、跨 offset、跨 endian 对齐；
6. F184/F185：widening/narrowing 必须经过 exact existential projection；
7. F176/F179 的 interval/polytope 只负责排序和采样，完整当前表达式仍负责接纳。

采样器可构造完整 `A x <= b`，使用 John/Dikin/coordinate walk；每个取整后的 byte
assignment 都重新检查整数约束。奇异、边界或超维情况退回 hit-and-run/coordinate
或普通 Z3。

**不能跨前缀复用 UNSAT。** 即使两个线性抽象看起来等价，未抽象的非线性、数组、
字符串或控制依赖也可能不同。跨前缀路径只把外来 SAT model 当 proposal。

## 12. 多层求解、Backsolver 与 Solver Portfolio

### 12.1 概念上的决策层，而非固定调用栈

一次目标翻转并不总是直接调用完整 Z3。下面是理解成本层次的概念顺序，实际代码会
根据 executor profile、query shape、cache 命中和异步 ownership 跳过、交换或并发
执行其中若干层：

1. branch-interest、依赖和 prefix policy 判断是否值得求解；
2. exact cache、prefix context、PSCache 或已验证 generator model；
3. direct/optimistic generator 和 Backsolver 的有界候选；
4. selective concolic partition 或 string/grammar 专用 backend；
5. 单 solver exact 求解；
6. bounded parallel portfolio race；
7. timeout/unknown 时保留证据并回退或延后。

QSYM 主路径的关键次序更具体：fast solve 成功会直接返回；否则默认先做带完整 prefix
的 strict solve。strict UNSAT/unknown 后，普通目标可尝试只保留目标的 optimistic
solve；含 ITE 的目标进入 Backsolver，后者才可能选择性丢弃与 ITE controller 依赖
重叠的 prefix。只有设置 `SYMCC_OPTIMISTIC_FIRST=1` 才会先解只含翻转目标的
target-only 公式：结果为 SAT 时保存临时 model 并继续 strict solve；结果为 UNSAT
时，目标本身已经不可满足，可以直接结束；结果为 unknown/timeout 时按当前预算停止。
只有后续 strict solve 未得到 SAT 时，先前的 optimistic model 才作为
Backsolver/保存路径的候选，仍不因此取得 corpus 接纳资格。

求解策略**没有静态占比**。占比由实际 workload 与 profile 决定，应从
`solver_queries`、`fast_solve_count`、`backsolver_*`、cache/poly/generator hits、
portfolio attempts 和 accepted outputs 等 telemetry 计算；没有 campaign 原始计数时，
不能给出“Z3/乐观/丢前缀各占多少”的百分比。

![求解决策](diagrams/qa3/fig-3-1-solve-decision.svg)

### 12.2 Backsolver 与 bounded Veritesting

F02 在 LLVM 中把 canonical `select`、双前驱 PHI 和有界多臂区域表示为 ITE。
Backsolver 先枚举附近 controller outcome，直接反演并用 lightweight evaluator
验证；若失败，再尝试删除与 ITE controller 依赖重叠的部分前缀并调用 Z3。

丢弃前缀只发生在**明确的 tailored/Backsolver fallback**，而不是普通 exact solve。
实现只删除与 ITE controller input dependencies 重叠的 prefix constraints，保留其余
约束并把目标翻转加入 Z3 或直接反演候选。副作用是：

- 候选可能满足目标却不满足原始路径；
- 嵌套 ITE 会造成候选组合增长；
- 删除过多会把成本从求解器转移到大量无效 concrete replay；
- 因此前缀被删除后，候选必须对保留约束和目标重新求值，并最终真实执行。它并不
  保证满足被删除的原始路径，所以 concrete replay 失败属于预期代价而非 solver bug。

### 12.3 PSCache 与部分解

F32/F35/F236 用 solver assignment、assumption conflict 和最小化 UNSAT core 构造
部分输入 assignment。命中时必须：

- 重建 signed prefix/off-path literal；
- 证明 assignment 与当前 Query IR 相容；
- 对当前完整 roots 重新求值；
- 把固有 UNSAT、空 core、缺字段和 proof tamper 显式拒绝。

它不是“看到相同几个字节就复制答案”，也不实现论文中所有 bit-blast trail 技术。

### 12.4 QF_BV portfolio 与 SMTgazer 式调度

F31/F34 建立异步和并行 portfolio，记录每个 backend 的 SAT/UNSAT、模型验证、
disagreement、child CPU、取消和 timeout。F237-F246 继续加入：

- backend-neutral QF_BV lowering 与 capability certificate；
- prefix-keyed persistent SMT-LIB process；
- cvc5/Bitwuzla/Z3 conformance 和模型双验证；
- censored-cost X-means/BIC prior；
- bagging/boosting schedule optimizer；
- expected-improvement SMBO；
- SAT consensus grace 与可取消 process group；
- Query IR feature schema v2；
- sealed paired holdout 与 leakage check。

UNSAT 只有在 backend 具备授权证据时才用于剪枝；SAT 必须有可验证模型。训练集、
holdout、solver binary identity、配置和内部 attempt 都绑定进 evidence，防止
post-hoc 选择最好结果。

F177/F178 的乐观 generator 和 Z3 tactic model converter同样遵守“proposal +
full validation”：simplify/solve-eqs 可以消掉变量，但必须通过 model converter
恢复 assignment，并对原始完整公式检查。

## 13. 字符串与位向量双表示

F30 先从 `memcmp/strcmp/strncmp` 导出 offset-aware concrete token 和 patch artifact；
F33 加入可执行 string solver 和 MPI/CLI materialization。F193-F200 将其扩展为：

- 同一 query context 中同时声明 String 和逐 offset 8-bit BV；
- 字符、first-NUL、长度与输入 byte 精确连接；
- `contains/indexof/substr` 和 byte predicate 联合求解；
- `strlen/strchr/strstr` 的有界 operation artifact；
- Z3/cvc5 等 backend 的 validation-first portfolio；
- `atoi/strtol/strtoul` 在明确子域内的 regex、String integer 和 BV fold 一致编码；
- wrapper 先用 concrete `strtol/strtoul` 结果、`ERANGE`、base/endptr、位宽和逐十进制
  step bound 判断是否落在精确子域；进入 symbolic 路径后把 digit/range/overflow guard
  编码为约束，并由 string query evaluator 再复核候选；
- contextual backend selector 只学习已通过 concrete evaluator 的结果。

这是 SymCC-str 思想的保守可执行子集，不是完整 C 字符串库模型。非空 `endptr`、
base 0/2-36、Unicode、复杂 alias/lifetime、中间 string object 和域外转换会 fail
closed 或回退普通 BV 路径。

## 14. 在线语法、解析森林与 PCFG

### 14.1 从 token 到可验证 grammar hole

F36 提供 token grammar proposal；F187 根据 comparison-taint span、分隔符和历史
accepted seed 在线学习稳定 production，并允许有界变长 splice。F190 把 Query IR
中的未约束 input bytes 标记为 grammar hole，候选同时经过 grammar evaluator 和
Query IR evaluator。F191 只在 coverage plateau 后从保留历史中采集结构，F192 用
独立 parser oracle 拒绝错误泛化，并按上下文拆分冲突规则。

### 14.2 解析结构与候选事务

F201-F212 建立结构化 parser trace、rule coverage、递归/互递归 CFG fragment、
incremental ECT subtree correspondence、Pareto corpus、packed forest、nullable SCC
fixed point、多 nonterminal 原子 transaction 和 content-addressed parser cache。

这里的关键不是“模型觉得输入像 JSON”，而是每个候选都能指出：

- 来源 production、nonterminal 和 byte span；
- packed forest 中的 alternative 或 nullable proof；
- 与目标 ECT subtree 的 correspondence；
- 修改前后 parser acceptance；
- 被替换 seed 和结构 feature 的 ownership。

### 14.3 概率语法与漂移控制

F213-F226 在 accepted forest 上建立 PCFG：

- Dirichlet posterior 与 inside/outside mass；
- parent、grandparent、ordered sibling 和 second-order sibling context；
- relation-aware synchronized slots；
- prequential log-loss calibration；
- recency window、stale/OOD detection 和恢复；
- Hoeffding cut、anytime-valid drift certificate、跨 context FWER；
- 五级 context order 的 sealed confirmatory ablation。

概率只改变候选排序。校准变差、证据过期或上下文样本不足时回退到更低阶/global
posterior，不会把低概率规则当作不可能。

### 14.4 多解析器与独立证据

F227-F235 接入 persistent Tree-sitter、generalized Earley/Lark、Parglare GLR/SPPF，
并构造：

- exact edit 与增量 tree reuse proof；
- complete SPPF/selected-tree paired telemetry；
- parser-neutral span/boundary correspondence；
- cross-parser calibration；
- grammar correspondence certificate；
- 小范围 exhaustive language differential。

这些机制用于发现单一 parser adapter 的盲点。不同 parser 同意可提高证据强度，
不同意则进入四象限统计和 fail-closed 路径，而不是多数投票修改程序语义。

## 15. UCSan 式 Under-Constrained Execution

F11 允许从指定函数入口开始，而不必手写完整应用 harness：

- 编译器建立函数参数、对象和 pointer shadow 操作；
- structured seed 描述 object graph、大小、内容和引用关系；
- runtime 为 pseudo pointer 提供真实 backing object；
- partial shadow、copy、load/store 和 external policy 有明确配置；
- seed 可 inspect、normalize、learn 和 dump。

它适合库函数、深层模块和难以从程序入口到达的代码，但 under-constrained execution
天然可能产生调用环境中不可达的状态。因此生成结果仍需回到真实 harness 或目标
程序验证；对象图大小、外部函数和 pointer scope 也都有上限。

## 16. 并发调度与内存模型探索

### 16.1 从 trace 到 schedule constraint

F13/F39-F44 建立线程、锁、条件变量、内存读写和 provenance trace，构造
happens-before、lockset conflict 和 schedule artifact。F45-F55 逐步加入：

- bounded SC replay-prefix SMT；
- mutex/rwlock lifecycle；
- condition wait/signal/broadcast；
- create/start/exit/join/detach/cancel identity；
- partial-order/linear-extension encoding；
- sparse lifecycle anchor；
- constructive topology certificate；
- lazy critical-section refinement；
- 系统 libz3 model extraction 和 runtime replay materialization。

### 16.2 Source/Optimal-DPOR 与弱内存

F56 保存 Source-DPOR source/sleep sets；F61 建 wakeup tree；F62 构造 bounded
ConDPOR execution graph 和 backward revisit；F63/F64 根据 path-dependent event
重新生成 transition 并检查 enabledness；F57 联合 Query IR、schedule 和 read-from
求解；F58/F65 支持有界 SC、TSO、RA 与 native atomic trace。F221 修复 signed
integer SMT-LIB 和跨 solver 可移植证据。

当前实现的准确表述是**bounded schedule diversification 和可执行证据框架**：

- event、thread、memory 和 query 数有硬上限；
- 只覆盖已建模 pthread/atomic 生命周期；
- memory model 是参数化有界编码；
- 尚不能声称任意 C/C++ 并发程序的完备模型检查或论文级 ConDPOR 等价证明。

## 17. Content-Addressed Live Continuation

### 17.1 状态数据面

F12 先提供 content-addressed 分布式状态、lease、fencing、coverage gossip 和
multi-coordinator 控制面；F38 定义 canonical continuation descriptor；F59 保存
solver stack、symbolic store 和 page-COW memory；F66 让 continuation IR 可执行并
接入 MPI frontier。

checkpoint descriptor 的内容身份同时绑定 frames/PC、path-condition root、
symbolic-store root、memory root、program root、parent 和 target branch。不同数据面
使用不同持久结构：path condition 是 parent solver frame + assertion delta；
symbolic store 是规范化全量映射 root；memory 是 page-level COW root，未修改页共享
摘要。worker 可以从 CAS 完整恢复这些 root。

同一 worker 对相邻 child 复用 prefix-keyed incremental solver 与已物化 SMT
fragment；迁移到其他 worker 时从持久 solver-frame chain 重建。进程内 solver-frame、
fragment 和 process cache 不进入 checkpoint identity，丢失它们只影响性能，不改变
checkpoint 语义。

### 17.2 LLVM 到 continuation 的有界语义

F68-F175 是一条很长但逻辑一致的工程链，可分为四段：

| 范围 | 主要能力 | 保守边界 |
| --- | --- | --- |
| F68-F80 | integer SSA、静态对象、input ABI、stack/heap、有限 alias、pointer PHI/select、跨函数/间接调用 | 无法证明的 alias、递归和 typed ABI 拒绝 |
| F81-F95 | memcmp/copy/string/search summary、pointer-valued memory、invoke、freeze、scalar intrinsic | 只支持有界长度和已列白名单 |
| F96-F141 | bit intrinsic、overflow/poison/definedness、跨调用和多路径 memory sidecar | 不近似未证明的 poison 或 clobber |
| F142-F175 | 多 cell、符号索引区间、byte-lane definedness、循环与递归 source tree | region、tree、lane、depth 均有预算 |

关键对象包括：

- **对象身份**：global、frame-local alloca、heap site/instance、input buffer；
- **生命周期**：alloc/free/realloc、frame depth、live/init marker；
- **指针 provenance**：有限候选对象、guard、offset domain、caller certificate；
- **内存**：page-COW byte memory、pointer sidecar、definedness sidecar；
- **未定义行为**：除零、shift、overflow、exact/nuw/nsw、freeze 和 poison；
- **外部语义**：只对可证明长度/对象域的 libc/intrinsic 建 summary；
- **恢复身份**：program counter、frame、dynamic choice counter 和 path root 均入状态。

后期 byte-lane 技术之所以细分许多 ID，是因为“一个 load 的值是否定义”可能来自
不同路径上的多个部分写入。实现从单 cell PHI 演进到多 cell、循环 carry、递归
condition tree、同源/多源 group 和 forwarded corridor，每次扩展都要求独立的
拓扑与 source-membership 证明。它们不是 30 个互不相关的功能。

## 18. IFSS、Hydra 与证明携带的 CFG 变换

### 18.1 为什么在编译期改控制流

复杂分支会产生大量短路径和嵌套 ITE。IFSS/Veritesting 尝试把多个路径合成一个
表达式状态，Hydra 尝试把相似控制流臂合并，从而减少 fork 和重复执行。错误变换会
直接改变程序语义，所以该技术族默认关闭并使用“证明、原程序重放、denylist”三层门。

F37 最初只生成 targeted transformation proposal；F248 将 profile-guided Hydra
接入真实 LLVM pass，并用未变换基线目标/原始 IR 生成的目标作为权威 replay oracle。
只有“变换目标出现失败而基线目标没有”的 site 才形成 spurious-failure 证据并进入
denylist；这符合 Hydra 的 failure-preserving 而非完整 semantics-preserving 边界。

### 18.2 多臂 IFSS、switch 与 continuation tuple

F249-F259 逐步实现：

- 3-8 臂 region-state worklist；
- MemorySSA/AA 证明的 memory-state merge 和 NoMod def-chain；
- data/memory 共享 partition 与 condition DAG ownership；
- 多 return exit state；
- switch linear、unsigned balanced 和 profile-optimal alphabetic tree；
- shared-destination edge/PHI multiplicity；
- branch-weight import 和可重放 switch manifest。

F260-F270 再将多出口区域表示为 exit-id + live-out tuple：

- scalar 和 MemorySSA-proven memory slot；
- affine natural-loop closed form；
- upper-triangular recurrence；
- post-update break/continue；
- nested acyclic MemoryPhi provenance；
- manifest、SHA-256 seal 和 fresh LLVM process replay。

F271-F274 扩展多 break priority、unequal linear arms、internal-tree Hydra，并把三条
transformation pipeline 统一成 sealed independent replay。seal 绑定 input/lowered
IR、compiler、LLVM tool、配置和 ordered proof identities；它能发现本地 artifact
漂移与篡改，但不是数字签名或远程 attestation。F275进一步恢复双端序、
constant-offset byte-lane partial-overlap continuation memory，并复检实际组合IR；
F276再增加有界two-arm pointer-select alias partition与guarded lane replay。
F277把共同循环前缀上的canonical两输入MemoryPhi恢复为真实整数byte-lane fixed
point：entry提供初值，latch逐lane选择partial store或self-carry，且独立replay核对
实际header PHI与端序表达式。它仍不等于conditional/multi-latch general-memory
loop summary。
F278再把一个pointer store扩展为depth-2/4-leaf finite union并重放实际select tree；
F279允许两条guarded write按MemorySSA顺序组合，保证后写select位于外层并具有覆盖
优先级。两者仍是有界值恢复，不是general points-to或heap fixed point。
F280进一步识别单回边上的strict write/carry diamond：actual latch MemoryPhi的一臂
执行partial update，另一臂self-carry；lowering在byte层保留原guard与极性，v9重放
branch/arm/guard拓扑和递归PHI。它仍不支持multi-latch或一般循环内存fixed point。
F281另行接纳恰好两个unconditional latch，为每个latch独立构造partial-store/carry
transfer并保留actual三输入PHI predecessor语义；v10不等于任意回边数或一般heap
fixed point。
F282进一步组合一个最新depth-two pointer-union writer和一个旧single-level
guarded writer。Lowering先构造旧guard的fallback byte，再把它嵌入最新pointer
tree，因此严格保留MemorySSA最近写者语义；v11独立重放两层actual select。该能力
仍限定为一条union和一条旧guard，不等于一般points-to或多writer heap graph。
F283把F280 conditional transfer与F281 two-latch header组合：两个backedge各自
保留partial-store/self-carry和guard polarity，header仍是actual三输入recursive
PHI，不构造虚假跨latch priority。V12独立重放每条普通或conditional transfer；
共享dominant guard、大端lane映射和fail-closed边界均有专项测试。该能力仍是严格
two-latch/one-diamond/constant-offset byte-lane子集，不等于general-memory loop。
F284把header扩为一个entry加2--4个latch。每条backedge仍独立证明ordinary或strict
conditional partial-store/carry，lowering保留actual 3--5输入PHI；v13为每条
transfer显式编码presence tag，并要求slot至少有一个3/4-latch状态。4-latch混合
语义、大端3-latch全无条件和5-latch拒绝均有专项证据；v1--v13兼容组26/26、
全量LLVM/lit 177/177及Python 459/459通过。它仍不支持nested predicate、任意
latch数、symbolic address或一般heap fixed point，上述结果也不是公开target的
性能比较。
F285进一步允许一个backedge由最多3个内部条件、4个叶子的unique-parent二叉树
选择。每个叶子独立证明pure carry或partial-store/carry，并在原叶子块物化完整值；
latch上的actual leaf PHI按真实predecessor选择，再反馈到1--4 latch header PHI。
V14绑定node/child/leaf拓扑、NoMod链和逐lane provenance，独立verifier检查唯一根、
单位入度和前向引用。最大树5120组差分、大端tree+ordinary双latch、双seal/replay、
Backsolver及5-leaf拒绝均有专项证据；v1--v14兼容29/29、LLVM/lit 180/180及
Python 459/459通过。它仍是3-node/4-leaf reducible-tree预算，不等于共享
predicate DAG、symbolic address或一般heap fixed point。
F286再把pointer-partition与guarded store统一为newest-first writer序列：最多4层，
其中最多2个depth-two partition和2个single-level guard。Lowering按oldest-to-newest
应用更新，最新写成为actual select最外层；较新无条件定义会mask较老lane。V15逐层
绑定ordinal/kind、guard极性、lane来源和完整partition tree。最大四层交错192组
差分还显式检查被遮蔽旧lane变为fallback；大端双partition、Backsolver、双
seal/replay、三类tamper及第三partition拒绝均有专项证据；v1--v15兼容32/32。
它仍是constant-offset byte-lane有界表达式，不等于一般points-to、symbolic array
或heap fixed point。
F287则把Hydra的F273 unique-parent tree扩为真正含local reconvergence的有界
acyclic SESE DAG。最近公共post-dominator确定外部merge，stable-site拓扑排序确保
前驱定义先于local PHI；incoming-edge guards经OR形成block guard，局部PHI再折叠
为ITE并进入predicated LCS alignment。V4完整绑定predecessor/successor、local
merge/PHI、alignment/output与指纹，独立verifier从successor反推拓扑。最大
2+1 local merge和aggressive readback-store正例各通过256输入差分、symbolization、
seal/replay及tamper rejection；external predecessor、cycle和第4个local merge
fail closed。它仍是10-block/3-local-merge的reducible proof budget，不是任意
Hydra/DARM region或公开性能结论。
F288补齐profile到实验权威程序的身份链：v2 profile封存产生遥测的resolved
command和executable SHA-256，compiler manifest绑定profile/command/selected-site
统计，replay-v2再要求campaign original与其完全一致。Malformed或空profile
fail closed，v1只保留兼容而不声称binary identity。四类compiler manifest还统一
使用`flock`、`O_APPEND`、partial-write循环和`fsync`；24进程竞争测试得到24/24条
完整record。它解决本机协作进程的完整性，不等于签名、透明日志或跨主机证明。
F289再把F274 seal及其全部artifact role装入content-addressed ZIP64，以external
trust-root Ed25519签署canonical descriptor；分离log key签署RFC9162-style
Merkle tree head，bundle携带offline inclusion proof。接收端先验签、复算root、
检查精确ZIP成员/全部blob/嵌入seal，再原子导入。8并发publisher和6项篡改/传输
测试通过。它仍不是公共透明服务、key lifecycle、timestamp、gossip anti-rollback
或reproducible-build证明。
F290把Hydra的LLVM语义边界变成显式可复检契约：每个alignment slot只保留一个
动态`freeze`实例，非活动除数以1安全化，其他非活动operand以零屏蔽；`invoke`、
EH pad和非branch terminator仍fail closed。Manifest绑定LLVM版本、语义策略和四类
freeze计数，独立verifier从alignment重算。跨major工具先重放F274 baseline，再用
ABI匹配的candidate plugin运行相同pass；去掉仅允许变化的版本字段后，proof
identity和manifest必须相等，textual IR必须SHA-256/size完全相同，并通过LLVM
verifier和candidate `llvm-diff`。当前只在LLVM 18.1.3与17.0.6上证明一个Hydra
fixture，不是任意IR的跨版本形式化等价。同时，QueryStore现在拒绝未通过完整
Query IR回放的partial model，fixed/converter补全也必须经过同一validator。
F291把continuation memory从有限pointer-select leaf推进到固定heap region上的精确
symbolic byte index。对`malloc/calloc`常量大小对象，编译器不枚举64-bit地址域，而
是按load lane和store byte生成等式
`index = load_offset + lane - source_byte - writer_base_offset`，再以有界
`icmp/select`链保持last-writer语义。V16把allocation、extent、index位宽、有符号
case和writer次序写入proof；compiler从actual IR剥离表达式，Python独立重算同一
指纹。Malloc 768组差分、真实Backsolver、calloc非零base、大端、负例和LLVM17/18
exact replay均通过。它只覆盖固定单对象、线性MemorySSA和最多8层writer，不是一般
heap graph、symbolic base、realloc或循环array fixed point。
F292进一步把同一overlap equality放进一个有限循环递推。Compiler只维护最终固定
read window：每个lane是整数PHI的一部分，本轮index命中时选择stored byte，否则
carry上一迭代byte。表达式大小与`load_width × store_width`相关而不随trip count
增长。V17把cycle topology、entry byte state、all-carry backedge和region writer
绑定；actual PHI/select/icmp、Python fingerprint、双seal和LLVM17/18 exact replay
均复验。当前只接受单entry、单unconditional backedge和单dynamic writer，不是
一般array或symbolic-base heap summary；这是v17自身的边界，F294另以v18扩展
conditional/multi-latch子域。

F293把Hydra DAG的可证明域扩到每arm 14 blocks、5 branch、10 leaf edge、4个
local merge、12个local PHI和96条指令，但旧v4候选仍优先匹配，因此已有制品和
lowering不漂移。V5对每条`(block,successor)` edge guard只物化一次，local PHI、
后继block和final leaf共享该SSA值；两个arm还按lowered predicate身份共享反值。
Manifest绑定完整predicate序列、canonical guard graph、结构/运行时复用计数，
Python verifier从拓扑和predicate等价类重算后纳入指纹。13-block正例完成256输入
差分、seal/replay与LLVM17/18 exact replay；16-block/5-local-merge仍拒绝。该复用
仅在一个selected region内，不等于跨region全局CSE或任意CFG lowering。

F294把F292的fixed-window symbolic-region transfer扩展到2--4个回边，并允许每个
latch上的单一writer受一层branch guard控制。关键语义不是把latch writer按顺序
应用，而是让MemoryPhi在entry和各latch transfer之间选择：内层equality/select
表达`index`命中本lane，外层guard/select表达该latch是否真正执行store；guard为假
时精确carry旧byte。V18为每个transfer绑定writer presence、allocator/extent、
store/index/guard identity、polarity和signed overlap cases，compiler从actual
PHI/guard/equality链重放，Python独立重算presence-aware fingerprint。双回边
conditional/unconditional正例完成126组差分和真实Backsolver，PowerPC64四回边
i16正例覆盖大端上界；同latch双dynamic writer、动态extent和五回边fail closed。
LLVM17/18 exact certificate进一步固定proof、manifest和lowered IR。这仍不是同一
latch多writer、nested predicate、dynamic extent、symbolic base或一般SMT array。

F295首次让一次Hydra pass显式处理同一函数中2--4个selected v5 region。只有当给定
site顺序形成strict dominance chain、arm-owned block两两不相交且所有region共享
至少一个原始SSA predicate时才准入。Function-scoped cache保存predicate反值及其
producer site；每次跨region命中都用当前函数的新DominatorTree证明旧反值支配当前
branch，否则重新物化。V6的transaction fingerprint绑定有序sites和共同predicate
集合，每条record再绑定ordinal、实际cross reuse和前序producer；独立verifier在
JSONL层检查完整事务，统一seal/replay按同一site数组重编译。双13-block/4-merge
fixture把独立构造的8条i1反值降为5条并通过256输入差分与LLVM17/18 exact replay。
它不跨函数共享任意表达式，也不支持非支配、overlapping/nested或一般CFG region。

F296处理的是与F294不同的顺序语义：同一latch的一次迭代内2--4个dynamic writer
全部执行，最新MemorySSA定义必须优先。Proof以ordinal 0记录newest writer；lowering
先把oldest equality/select包在carry byte外，再逐层加入newer writer，最终实际
表达式的最外层就是最后写者。Compiler replay从外向内剥离并要求终点是同一cycle
PHI byte；Python verifier独立检查writer数量、连续ordinal、不同store site、共享
固定allocation/extent及v17/v19字段互斥，再重算fingerprint。小端3 writer正例
完成105组差分和真实Backsolver，PowerPC64双writer覆盖大端，5 writer与unknown
ModRef拒绝，LLVM17/18 exact replay一致。它仍不是nested/conditional writer tree、
multi-latch内multiwriter、symbolic base、dynamic heap或一般SMT array模型。

### 18.3 编译 pass 次序

LLVM new pass manager 把工作拆为 module pipeline start 与 vectorizer start 两段：

```text
Module pipeline start:
IFSSSwitchLowering
  -> IFSSLoopSummary
  -> IFSSExitLowering
  -> IFSSContinuationLowering
  -> IFSSContinuationMemory
  -> HydraTransformation
  -> LiveContinuationExport
  -> SymbolizePass(module)

Function pipeline at vectorizer start:
Scalarizer
  -> LowerAtomic
  -> SymbolizePass(function)
```

module 版 `SymbolizePass` 初始化 stable site IDs，插入 UCSan/atomic schedule
instrumentation，导出静态依赖/距离图，重命名 wrapper 并建立 runtime constructor；
function 版才 lower intrinsic/inline assembly、建立参数表达式、插入 basic-block
notification 并逐 instruction 符号化。LLVM 15 及以下的 legacy pipeline 保持相同的
逻辑变换顺序，但把 `Scalarizer -> LowerAtomic -> SymbolizeLegacyPass` 串在同一注册点。

这个顺序保证 CFG/continuation 变换先完成、atomic/vector 形态先规范化，再由普通
symbolization 看最终 IR。各变换对 proof failure 必须 fail closed；对于先构建再验证的
局部变换，回滚后不能留下孤立 runtime call、不完整 PHI 或残余 metadata。

## 19. 科研证据、实验协议与可复现性

F16/F21 统一 benchmark schema、公开目标、coverage/crash 口径、AFL stats、抽样和
统计；F60 把 backend oracle、semantic gate、digest 和 claim gate 写成可执行证据；
F67 提供 sealed randomized paired protocol。后续各技术族继续把证据内建：

- F198：String/BV cross-solver conformance；
- F221：schedule SMT 跨 solver 可移植性；
- F226：PCFG 五档 confirmatory ablation；
- F228/F230-F235：parser cache/forest/cross-parser calibration；
- F244-F246：固定 solver identity、holdout、leakage check；
- F247：AFL edge/data coverage join；
- F263-F296：compiler manifest、seal、profile identity、signed transport、LLVM
  semantic contract、symbolic heap-region proof和cross-major exact replay。

一份可用于科研结论的运行至少应保存：

1. source revision 与 dirty-worktree 状态；
2. compiler、LLVM、Z3/cvc5/Bitwuzla、AFL++ 和 target binary identity；
3. corpus、dictionary、random seed、CPU affinity 和总 CPU 预算；
4. 完整配置与 capability；
5. 原始 telemetry、coverage maps、query/schedule/parser/transform artifacts；
6. baseline、逐项消融和 full system；
7. 多轮结果、coverage AUC、time-to-target、accepted/CPU-hour、solver CPU；
8. median、bootstrap confidence interval、effect size 和失败运行。

项目现有 B 级历史数字可以说明某优化机制在特定环境可工作，但不能与不同 harness、
CPU 配额或版本的结果直接拼成 SOTA 对比。

## 20. 配置使用导航

| 目的 | 主要配置族 | 提醒 |
| --- | --- | --- |
| 基础 concolic | `SYMCC_INPUT_*`, `OUTPUT_DIR`, `TIMEOUT` | 先确认目标输入 ABI |
| 自适应 hybrid | `TELEMETRY`, `PREFIX_DAG`, `ECT`, `DATA_COVERAGE` | helper 会自动设置部分默认 |
| Poly/Z3 复用 | `POLY_*`, `PREFIX_CONTEXT_CACHE`, `UNSAT_CORE_*` | 跨前缀只复用 SAT proposal |
| Query service | `QUERY_*`, `GENERATOR_*`, `PARTIAL_SOLUTION_*` | 保存 Query IR 与 solver identity |
| Solver portfolio | `QUERY_SOLVER_PORTFOLIO_*`, `SMT_*`, `SOLVER_PSCACHE_*` | 控制总 CPU，不只看 wall time |
| String | `STRING_*` | 域外 libc 语义会保守回退 |
| Grammar/parser | `GRAMMAR_*`, `PARSER_*`, `PCFG_*`, `PROPOSAL_*` | 研究 artifact 可设为强制 |
| 并发 | `SCHEDULE_*`, `DPOR_*` | event/thread/memory bound 必须记录 |
| Live state | `LIVE_*`, `STATE_*`, `OBJECT_TRANSPORT` | 仅适用受支持 LLVM 子集 |
| UCSan | `UCSAN_*` | 结果需真实 harness 验证 |
| IFSS/Hydra | `IFSS_*`, `HYDRA_*`, manifest/seal | compile-time、默认关闭 |
| 科研协议 | `RESEARCH_*`, `EXPERIMENT_ID`, `PAIR_ID`, `RANDOM_SEED` | 不允许调参数据泄漏到 holdout |

建议从已定义 profile 启用功能，不要一次手工打开数十个开关。profile 会同步设置
依赖、hard guard、预算和输出路径；自定义实验则应保存规范化后的完整环境。

## 21. 代码与测试导航

当前快照包含：

- `compiler/` 顶层 25 个 C++/header 文件；
- `util/` 顶层 66 个 Python/C 工具；
- `test/` 顶层 198 个 `.py/.c/.ll` 测试源；
- `Configuration.txt` 中 375 个唯一 `SYMCC_*` 配置名；
- `New_Implementation_Archive.md` 中连续的 297 个功能 ID（F00–F296）。

这些数量只能说明覆盖面，不能替代测试结果。常用入口如下：

| 主题 | 代码 | 代表测试 |
| --- | --- | --- |
| Compiler/Backsolver | `compiler/Symbolizer.cpp`, `compiler/Pass.cpp` | `backsolver_*.ll` |
| Continuation | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | `live_continuation_*.ll` |
| IFSS/Hydra | `compiler/IFSS*.cpp`, `compiler/HydraTransformation.cpp` | `hydra_*.ll`, `backsolver_*ifss*.ll` |
| QSYM/Query | QSYM `solver.*`, `query_solver.cpp`, `util/query_store.py` | `query_*.py`, `poly_*.c` |
| Feedback | `util/hybrid_feedback.py`, `util/expressive_coverage.py` | `test_hybrid_feedback.py` |
| String | `runtime/src/LibcWrappers.cpp`, `util/string_constraints.py` | `string_*.py/.c` |
| Grammar/parser | `util/semantic_proposals.py`, parser adapters | `test_*parser*.py`, `test_semantic_proposals.py` |
| Schedule | `util/symcc_schedule_rt.c`, `util/schedule_exploration.py` | `test_schedule_exploration.py` |
| Distributed/live state | `util/distributed_state.py`, `util/symcc_live_state.py` | `test_distributed_state.py` |
| QF_BV campaigns | `util/qf_bv_*.py`, SMT scheduler modules | `test_qf_bv_*.py`, `test_smt_*.py` |
| Evidence | `util/research_evidence.py`, benchmark tools | `test_research_*.py` |

本快照在 2026-07-30 重新执行了两套全量门禁：

```text
PATH=/usr/lib/llvm-18/bin:$PATH cmake --build build -j16
  -> passed

PATH=/usr/lib/llvm-17/bin:$PATH cmake --build build-llvm17 -j16
  -> passed

PATH=/usr/lib/llvm-18/bin:$PATH /home/ubuntu/venv/symcc/bin/lit -sv -j16 build/test
  -> 206/206 passed

PATH=/usr/lib/llvm-17/bin:$PATH /home/ubuntu/venv/symcc/bin/lit -sv -j16 build-llvm17/test
  -> 205 passed, 1 unsupported

python3 -m unittest discover -s test -p 'test_*.py' -v
  -> 475/475 passed (2026-07-30 current rerun)
```

本轮最终验证把两个LLVM套件和Python顺序执行，LLVM 18、LLVM 17与Python分别耗时
135.31 s、139.22 s和80.736 s（对应shell总墙钟135.361 s、139.276 s和
81.265 s）。此前把两套默认
192-worker lit与Python同时启动时，
两个资源敏感用例各失败一次；隔离复测均通过，固定`-j16`后的两套全量门禁也通过。
这说明失败来自同机过度并发争用，而不是F293语义回归。上述耗时只作为可复现实验
日志，不作为性能基准。

文档自身还执行 `generate_current_technology_index.py --check`、本地 Markdown 链接
检查、Python `py_compile` 和 `git diff --check`。这些结果证明当前机制回归通过，
不替代真实目标的等 CPU、多轮覆盖率实验。

## 22. 当前边界与不应夸大的结论

1. **实现不等于 SOTA 实验结论。** 最新 full system 缺少统一版本下的等 CPU、多轮、
   全消融 R 级结果。
2. **GenSym/ConDPOR/IFSS/SymCC-str 均为有界实现。** 项目覆盖论文思想的重要可执行
   子集，但不能用论文名称暗示已复刻全部语义或证明。
3. **学习和 agentic 模块不可信。** 它们只能排序或提议，不能授权 UNSAT、覆盖或
   程序等价。
4. **跨前缀复用不传播 UNSAT。** SAT model、poly sample 和字段重命名始终需要当前
   完整约束验证。
5. **AFL data namespace 可能碰撞。** scheduler 的显式结构键 tracker 才能区分
   `(object, offset, width, kind)`，但它记录的是观测到的最佳进展，不是程序语义完备性。
6. **多 coordinator 依赖共享存储语义。** 当前不是无共享存储的拜占庭一致性系统。
7. **proof artifact 有边界。** seal/replay 可检测本地漂移和证据篡改，不等于形式化
   验证、密码学签名或远程 attestation。
8. **LLVM lowering fail closed 会损失覆盖范围。** 保守拒绝是 correctness 选择，
   不是“所有输入程序都已经支持”。

## 23. 可归纳的研究创新

不借用论文名称，仅从当前系统本身看，可以归纳出五个有研究价值的组合创新：

1. **多粒度并行统一闭环**：seed、query、schedule 和 continuation state 使用版本化、
   可互操作的 telemetry/artifact contract，并在具体化后汇入 coverage ownership 与
   可信接纳边界；它们不是一个完全相同的 JSON schema；
2. **Query IR 作为跨层证据总线**：求解复用、字符串、语法补洞、schedule 和
   holdout campaign 共享内容身份和验证语义；
3. **双覆盖平面**：AFL 兼容位图保证生态互操作，显式结构键的
   data/grammar/structure 状态提高调度与研究测量的可解释性；
4. **激进 proposal、保守 acceptance**：polyhedral renaming、optimistic slice、
   grammar/PCFG、agentic route 和 Hydra transformation 都可增加召回，但正确性由
   独立 validator/replay 保持；
5. **证明携带的系统优化**：编译变换、parser forest、solver strategy 和 coverage
   join 不只输出结果，还输出可重算的结构证据和 sealed experiment identity。

这些是合理的系统设计贡献候选。是否能形成论文主张，仍应由 R 级公开 benchmark、
消融和与最强 baseline 的公平对照决定。

## 24. 完整功能索引

下表由 `New_Implementation_Archive.md` 和
`Development_History_Traceability.md` 自动抽取。每个 `F00-F296` 必须恰好出现一次；
生成器在缺号、重号、标题缺失或状态缺失时失败。

<!-- FEATURE_INDEX_START -->
| ID | 技术名称 | 主层 | 当前证据 | 权威明细 |
| --- | --- | --- | --- | --- |
| F00 | 编译/runtime 基础正确性 | 基础正确性 | I/T | [档案 §3.1](New_Implementation_Archive.md) |
| F01 | 统一执行遥测与后端抽象 | 遥测与覆盖 | I/T | [档案 §4](New_Implementation_Archive.md) |
| F02 | Backsolver 与 bounded Veritesting | Query / solver | I/T | [档案 §5](New_Implementation_Archive.md) |
| F03 | 分层求解、Pangolin 上下文复用与 UNSAT 复用 | Query / solver | I/T | [档案 §6](New_Implementation_Archive.md) |
| F04 | data coverage、comparison taint 与 AFL 原生 map | 遥测与覆盖 | I/T | [档案 §7](New_Implementation_Archive.md) |
| F05 | S2F actionseed、PrefixDAG、TACO/MultiGo | 调度与目标引导 | I/T | [档案 §8](New_Implementation_Archive.md) |
| F06 | directed coloration、并发引导和结构任务图 | 遥测与覆盖 | I/T | [档案 §9](New_Implementation_Archive.md) |
| F07 | Expressive Coverage Tree 与 minimum path cover | 遥测与覆盖 | I/T | [档案 §9](New_Implementation_Archive.md) |
| F08 | 学习型 seed/worker/策略/参数调度 | 调度与目标引导 | I/T | [档案 §10](New_Implementation_Archive.md) |
| F09 | SMTgazer 式算法序列调度 | 调度与目标引导 | I/T | [档案 §10](New_Implementation_Archive.md) |
| F10 | 语义 fallback、agentic route 与验证式 proposal | 调度与目标引导 | I/T | [档案 §11](New_Implementation_Archive.md) |
| F11 | UCSan 式 under-constrained execution | UCSan | I/T | [档案 §12](New_Implementation_Archive.md) |
| F12 | content-addressed 分布式状态、fenced lease 与 coverage gossip | Live continuation | I/T | [档案 §13](New_Implementation_Archive.md) |
| F13 | bounded-DPOR 调度探索 | 并发状态空间 | I/T | [档案 §14](New_Implementation_Archive.md) |
| F14 | AFL++ hint mutator 与 profile 编排 | 调度与目标引导 | I/T | [档案 §15](New_Implementation_Archive.md) |
| F15 | 干扰感知离线策略评估 | 调度与目标引导 | I/T | [档案 §11](New_Implementation_Archive.md) |
| F16 | benchmark、消融与统计报告 | 证据与 benchmark | I/T | [档案 §16](New_Implementation_Archive.md) |
| F17 | MPI 并行执行基础与可靠协议 | MPI / hybrid | I/T/B | [档案 §17.1](New_Implementation_Archive.md) |
| F18 | AFL++/SymCC 闭环与吞吐优化 | MPI / hybrid | I/T/B | [档案 §17.2](New_Implementation_Archive.md) |
| F19 | 可扩展工作分解与资源控制 | MPI / hybrid | I/T/B | [档案 §17.3](New_Implementation_Archive.md) |
| F20 | 结构化输入与异构 fuzzing 协同 | MPI / hybrid | I/T/B | [档案 §17.4](New_Implementation_Archive.md) |
| F21 | 公开 benchmark 与测量正确性 | MPI / hybrid | I/T/B | [档案 §17.5](New_Implementation_Archive.md) |
| F22 | 阶段计时与冗余归因 | MPI / hybrid | I/T/B | [档案 §17.6](New_Implementation_Archive.md) |
| F23 | 可配置 SymCC/SymSan 引擎抽象 | MPI / hybrid | I/T/B | [档案 §17.7](New_Implementation_Archive.md) |
| F24 | SymSan 技术迁移与 RGD 求解栈 | MPI / hybrid | I/T/B | [档案 §17.8](New_Implementation_Archive.md) |
| F25 | 可复现安装与离线交付 | MPI / hybrid | I/T/B | [档案 §17.9](New_Implementation_Archive.md) |
| F26 | 跨层正确性审查修复 | 基础正确性 | I/T | [档案 §21](New_Implementation_Archive.md) |
| F27 | 持久化 Query IR、异步 query trie 与增量 Z3 服务 | Query / solver | I/T | [档案 §22](New_Implementation_Archive.md) |
| F28 | 完整 static Data Coverage 与独立 novelty/dominance | 遥测与覆盖 | I/T | [档案 §23](New_Implementation_Archive.md) |
| F29 | reusable solution generator 与 full-matrix AFL mutator | Query / solver | I/T | [档案 §24](New_Implementation_Archive.md) |
| F30 | string-constraint artifact 与 offset-aware candidate | 字符串双表示 | I/T | [档案 §25](New_Implementation_Archive.md) |
| F31 | asynchronous solver portfolio 与 disagreement telemetry | Query / solver | I/T | [档案 §26](New_Implementation_Archive.md) |
| F32 | PSCache-inspired partial solution cache 与 Query IR verified reuse | Query / solver | I/T | [档案 §27](New_Implementation_Archive.md) |
| F33 | SymCC-str phase-2 string theory backend 与 MPI/CLI materialization | 字符串双表示 | I/T | [档案 §28](New_Implementation_Archive.md) |
| F34 | Bounded parallel solver portfolio racing | Query / solver | I/T | [档案 §29](New_Implementation_Archive.md) |
| F35 | Solver-helper internal PSCache assignment probing | Query / solver | I/T | [档案 §30](New_Implementation_Archive.md) |
| F36 | Lase/Cottontail-inspired token grammar solve-complete proposals | 语法与解析 | I/T | [档案 §31](New_Implementation_Archive.md) |
| F37 | IFSS/Hydra-style targeted transformation proposals | IFSS / Hydra | I/T | [档案 §32](New_Implementation_Archive.md) |
| F38 | GenSym-style live continuation checkpoint descriptors | Live continuation | I/T | [档案 §33](New_Implementation_Archive.md) |
| F39 | ConDPOR-style HB/lockset schedule conflict analysis | 并发状态空间 | I/T | [档案 §34](New_Implementation_Archive.md) |
| F40 | ConDPOR-style memory read/write schedule trace instrumentation | 并发状态空间 | I/T | [档案 §35](New_Implementation_Archive.md) |
| F41 | ConDPOR-style schedule constraint artifact export | 并发状态空间 | I/T | [档案 §36](New_Implementation_Archive.md) |
| F42 | Query IR × schedule artifact joint replay validation | 并发状态空间 | I/T | [档案 §37](New_Implementation_Archive.md) |
| F43 | Schedule memory provenance filtering | 并发状态空间 | I/T | [档案 §38](New_Implementation_Archive.md) |
| F44 | Schedule memory provenance tags and artifact propagation | 并发状态空间 | I/T | [档案 §39](New_Implementation_Archive.md) |
| F45 | Bounded SC schedule-SMT replay-prefix encoding | 并发状态空间 | I/T | [档案 §40](New_Implementation_Archive.md) |
| F46 | Mutex/rwlock lifecycle-state schedule-SMT | 并发状态空间 | I/T | [档案 §41](New_Implementation_Archive.md) |
| F47 | Condition-variable wait/wake operational schedule-SMT | 并发状态空间 | I/T | [档案 §42](New_Implementation_Archive.md) |
| F48 | Thread create/join operational schedule-SMT | 并发状态空间 | I/T | [档案 §43](New_Implementation_Archive.md) |
| F49 | Lifecycle partial-order / linear-extension schedule-SMT v5 | 并发状态空间 | I/T/B | [档案 §44](New_Implementation_Archive.md) |
| F50 | Scaled-anchor sparse lifecycle order links | 并发状态空间 | I/T/B | [档案 §45](New_Implementation_Archive.md) |
| F51 | Constructive linear-extension certificate checker | 并发状态空间 | I/T | [档案 §46](New_Implementation_Archive.md) |
| F52 | Verified topology-to-runtime replay materialization | 并发状态空间 | I/T | [档案 §47](New_Implementation_Archive.md) |
| F53 | Violation-driven lazy critical-section refinement | 并发状态空间 | I/T/B | [档案 §48](New_Implementation_Archive.md) |
| F54 | Direct system-libz3 model extraction and solve CLI | 并发状态空间 | I/T | [档案 §49](New_Implementation_Archive.md) |
| F55 | Detach/cancel/join-cancelled identity lifecycle | 并发状态空间 | I/T/B | [档案 §50](New_Implementation_Archive.md) |
| F56 | Bounded Source-DPOR certificate、source set 与 sleep-set persistence | 并发状态空间 | I/T | [档案 §51](New_Implementation_Archive.md) |
| F57 | Single-context Query IR × schedule × read-from solving | 并发状态空间 | I/T | [档案 §52](New_Implementation_Archive.md) |
| F58 | Parameterized SC/TSO/RA bounded memory-model constraints | 并发状态空间 | I/T | [档案 §53](New_Implementation_Archive.md) |
| F59 | Content-addressed live state、solver stack 与 page-COW memory | Live continuation | I/T | [档案 §54](New_Implementation_Archive.md) |
| F60 | Executable research-evidence bundle and claim gates | 证据与 benchmark | I/T | [档案 §55](New_Implementation_Archive.md) |
| F61 | Bounded Optimal-DPOR wakeup tree and cooperative ready evidence | 并发状态空间 | I/T | [档案 §56](New_Implementation_Archive.md) |
| F62 | Bounded ConDPOR execution graph、backward revisit 与 maximal extension | 并发状态空间 | I/T | [档案 §57](New_Implementation_Archive.md) |
| F63 | Path-dependent branch/action schedule events 与 causal regeneration | 并发状态空间 | I/T | [档案 §58](New_Implementation_Archive.md) |
| F64 | Operational enabledness offers 与可复检证书 | 并发状态空间 | I/T | [档案 §59](New_Implementation_Archive.md) |
| F65 | Native atomic trace 与扩展 bounded C11 RA 语义 | 并发状态空间 | I/T | [档案 §60](New_Implementation_Archive.md) |
| F66 | Executable continuation IR 与 MPI live-state fork/resume | Live continuation | I/T | [档案 §61](New_Implementation_Archive.md) |
| F67 | Sealed randomized research protocol 与配对统计 | 证据与 benchmark | I/T | [档案 §62](New_Implementation_Archive.md) |
| F68 | Conservative LLVM-to-continuation lowering 与 feasibility pruning | Live continuation | I/T | [档案 §63](New_Implementation_Archive.md) |
| F69 | Object-aware static memory continuation lowering | Live continuation | I/T | [档案 §64](New_Implementation_Archive.md) |
| F70 | Explicit-size symbolic input-buffer entry ABI | Live continuation | I/T | [档案 §65](New_Implementation_Archive.md) |
| F71 | Frame-local fixed stack objects 与 resumable lifetime | Live continuation | I/T | [档案 §66](New_Implementation_Archive.md) |
| F72 | Bounded call-site heap objects 与 alloc/free lifetime | Live continuation | I/T | [档案 §67](New_Implementation_Archive.md) |
| F73 | Bounded symbolic offset/alias enumeration 与 finite ITE memory | Live continuation | I/T | [档案 §68](New_Implementation_Archive.md) |
| F74 | Guarded pointer PHI/select provenance union | Live continuation | I/T | [档案 §69](New_Implementation_Archive.md) |
| F75 | Bounded multi-instance heap identity 与 canonical slot reuse | Live continuation | I/T | [档案 §70](New_Implementation_Archive.md) |
| F76 | Bounded cross-function pointer/object references | Live continuation | I/T | [档案 §71](New_Implementation_Archive.md) |
| F77 | CAS-rooted state-local incremental solver contexts | Live continuation | I/T | [档案 §72](New_Implementation_Archive.md) |
| F78 | Nullable runtime-sized allocator 与 conditional heap state | Live continuation | I/T | [档案 §73](New_Implementation_Archive.md) |
| F79 | Caller-domain symbolic pointer certificate | Live continuation | I/T | [档案 §74](New_Implementation_Archive.md) |
| F80 | Bounded indirect-call points-to dispatch 与 typed continuation ABI | Live continuation | I/T | [档案 §75](New_Implementation_Archive.md) |
| F81 | Deterministic external memory-compare effect summary | Live continuation | I/T | [档案 §76](New_Implementation_Archive.md) |
| F82 | Bounded external region-write effect summaries | Live continuation | I/T | [档案 §77](New_Implementation_Archive.md) |
| F83 | Pointer-returning bounded indirect dispatch | Live continuation | I/T | [档案 §78](New_Implementation_Archive.md) |
| F84 | Guarded memory access 与 bounded NUL-aware string summaries | Live continuation | I/T | [档案 §79](New_Implementation_Archive.md) |
| F85 | Bounded pointer-returning memory/string search summaries | Live continuation | I/T | [档案 §80](New_Implementation_Archive.md) |
| F86 | Guarded memory write 与 bounded string-copy summaries | Live continuation | I/T | [档案 §81](New_Implementation_Archive.md) |
| F87 | LLVM defined-value guards for integer UB/poison conditions | Live continuation | I/T | [档案 §82](New_Implementation_Archive.md) |
| F88 | Bounded pointer-valued memory 与 finite provenance sidecar | Live continuation | I/T | [档案 §83](New_Implementation_Archive.md) |
| F89 | Scalar function-pointer memory 与 typed target recovery | Live continuation | I/T | [档案 §84](New_Implementation_Archive.md) |
| F90 | Bounded global pointer/function-pointer tables | Live continuation | I/T | [档案 §85](New_Implementation_Archive.md) |
| F91 | Direct-predecessor pointer-memory merge | Live continuation | I/T | [档案 §86](New_Implementation_Archive.md) |
| F92 | Proven-`nounwind` invoke normal-edge lowering | Live continuation | I/T | [档案 §87](New_Implementation_Archive.md) |
| F93 | Stable dynamic nondeterministic freeze choice | Live continuation | I/T | [档案 §88](New_Implementation_Archive.md) |
| F94 | Bounded acyclic pointer-memory SSA closure | Live continuation | I/T | [档案 §89](New_Implementation_Archive.md) |
| F95 | Deterministic scalar external/intrinsic summaries | Live continuation | I/T | [档案 §90](New_Implementation_Archive.md) |
| F96 | Bounded bit-count intrinsic summaries | Live continuation | I/T | [档案 §91](New_Implementation_Archive.md) |
| F97 | Direct deferred-poison freeze | Live continuation | I/T | [档案 §92](New_Implementation_Archive.md) |
| F98 | Bounded cyclic pointer-memory SSA fixed point | Live continuation | I/T | [档案 §93](New_Implementation_Archive.md) |
| F99 | Canonical constant-GEP pointer-cell identity | Live continuation | I/T | [档案 §94](New_Implementation_Archive.md) |
| F100 | Bounded bit-permutation/funnel-shift intrinsics | Live continuation | I/T | [档案 §95](New_Implementation_Archive.md) |
| F101 | Bounded saturating add/sub intrinsics | Live continuation | I/T | [档案 §96](New_Implementation_Archive.md) |
| F102 | Bounded saturating shift intrinsics | Live continuation | I/T | [档案 §97](New_Implementation_Archive.md) |
| F103 | Bounded scalar abs/min/max intrinsics | Live continuation | I/T | [档案 §98](New_Implementation_Archive.md) |
| F104 | LLVM branch-hint identity intrinsics | Live continuation | I/T | [档案 §99](New_Implementation_Archive.md) |
| F105 | Bounded static objectsize intrinsic | Live continuation | I/T | [档案 §100](New_Implementation_Archive.md) |
| F106 | Bounded dynamic objectsize intrinsic | Live continuation | I/T | [档案 §101](New_Implementation_Archive.md) |
| F107 | Bounded overflow arithmetic aggregates | Live continuation | I/T | [档案 §102](New_Implementation_Archive.md) |
| F108 | Integer/data/function-pointer `ssa.copy` | Live continuation | I/T | [档案 §103](New_Implementation_Archive.md) |
| F109 | Dynamic realloc objectsize | Live continuation | I/T | [档案 §104](New_Implementation_Archive.md) |
| F110 | Transitive deferred poison chain | Live continuation | I/T | [档案 §105](New_Implementation_Archive.md) |
| F111 | Totalized deferred division freeze | Live continuation | I/T | [档案 §106](New_Implementation_Archive.md) |
| F112 | Path-sensitive select poison | Live continuation | I/T | [档案 §107](New_Implementation_Archive.md) |
| F113 | PHI edge-definedness merge | Live continuation | I/T | [档案 §108](New_Implementation_Archive.md) |
| F114 | Straight-line memory poison sidecar | Live continuation | I/T | [档案 §109](New_Implementation_Archive.md) |
| F115 | Canonical-address memory poison | Live continuation | I/T | [档案 §110](New_Implementation_Archive.md) |
| F116 | Linear-CFG memory poison | Live continuation | I/T | [档案 §111](New_Implementation_Archive.md) |
| F117 | Direct-call return poison ABI | Live continuation | I/T | [档案 §112](New_Implementation_Archive.md) |
| F118 | Direct-call argument poison ABI | Live continuation | I/T | [档案 §113](New_Implementation_Archive.md) |
| F119 | Bounded multi-callsite poison ABI | Live continuation | I/T | [档案 §114](New_Implementation_Archive.md) |
| F120 | Symbolic pointer-memory + initial definition merge | Live continuation | I/T | [档案 §115](New_Implementation_Archive.md) |
| F121 | Transitive argument-to-return poison | Live continuation | I/T | [档案 §116](New_Implementation_Archive.md) |
| F122 | Bounded multi-consumer deferred poison | Live continuation | I/T | [档案 §117](New_Implementation_Archive.md) |
| F123 | Bounded multi-load memory poison | Live continuation | I/T | [档案 §118](New_Implementation_Archive.md) |
| F124 | Acyclic branch memory poison | Live continuation | I/T | [档案 §119](New_Implementation_Archive.md) |
| F125 | Path-dependent memory definedness PHI | Live continuation | I/T | [档案 §120](New_Implementation_Archive.md) |
| F126 | Interprocedural memory definedness PHI | Live continuation | I/T | [档案 §121](New_Implementation_Archive.md) |
| F127 | Multilevel memory definedness PHI | Live continuation | I/T | [档案 §122](New_Implementation_Archive.md) |
| F128 | Cyclic memory definedness PHI | Live continuation | I/T | [档案 §123](New_Implementation_Archive.md) |
| F129 | Initial memory definedness merge | Live continuation | I/T | [档案 §124](New_Implementation_Archive.md) |
| F130 | Initial subobject definedness merge | Live continuation | I/T | [档案 §125](New_Implementation_Archive.md) |
| F131 | Cyclic no-write definedness carry | Live continuation | I/T | [档案 §126](New_Implementation_Archive.md) |
| F132 | Conditional store/carry transfer | Live continuation | I/T | [档案 §127](New_Implementation_Archive.md) |
| F133 | Forwarded conditional carry | Live continuation | I/T | [档案 §128](New_Implementation_Archive.md) |
| F134 | Multi-arm equivalent-source carry | Live continuation | I/T | [档案 §129](New_Implementation_Archive.md) |
| F135 | Equivalent defined-store carry | Live continuation | I/T | [档案 §130](New_Implementation_Archive.md) |
| F136 | Shared-poison writer carry | Live continuation | I/T | [档案 §131](New_Implementation_Archive.md) |
| F137 | Nested conditional writer carry | Live continuation | I/T | [档案 §132](New_Implementation_Archive.md) |
| F138 | Bounded recursive writer/carry tree | Live continuation | I/T | [档案 §133](New_Implementation_Archive.md) |
| F139 | Grouped recursive source tree | Live continuation | I/T | [档案 §134](New_Implementation_Archive.md) |
| F140 | Repeated-source recursive tree | Live continuation | I/T | [档案 §135](New_Implementation_Archive.md) |
| F141 | Multi-carry recursive tree | Live continuation | I/T | [档案 §136](New_Implementation_Archive.md) |
| F142 | Disjoint multi-cell definedness PHI | Live continuation | I/T | [档案 §137](New_Implementation_Archive.md) |
| F143 | Identified-object multi-cell definedness PHI | Live continuation | I/T | [档案 §138](New_Implementation_Archive.md) |
| F144 | Fixed-heap-object multi-cell definedness PHI | Live continuation | I/T | [档案 §139](New_Implementation_Archive.md) |
| F145 | Finite pointer-domain multi-cell definedness PHI | Live continuation | I/T | [档案 §140](New_Implementation_Archive.md) |
| F146 | Guard-correlated pointer-domain multi-cell PHI | Live continuation | I/T | [档案 §141](New_Implementation_Archive.md) |
| F147 | PHI-correlated pointer-domain multi-cell PHI | Live continuation | I/T | [档案 §142](New_Implementation_Archive.md) |
| F148 | Symbolic-index interval multi-cell PHI | Live continuation | I/T | [档案 §143](New_Implementation_Archive.md) |
| F149 | Byte-lane memory definedness composition | Live continuation | I/T | [档案 §144](New_Implementation_Archive.md) |
| F150 | Direct-predecessor byte-lane definedness PHI | Live continuation | I/T | [档案 §145](New_Implementation_Archive.md) |
| F151 | Cyclic byte-lane definedness PHI | Live continuation | I/T | [档案 §146](New_Implementation_Archive.md) |
| F152 | Conditional cyclic byte-lane carry | Live continuation | I/T | [档案 §147](New_Implementation_Archive.md) |
| F153 | Forwarded conditional cyclic byte-lane carry | Live continuation | I/T | [档案 §148](New_Implementation_Archive.md) |
| F154 | Multi-arm conditional cyclic byte-lane carry | Live continuation | I/T | [档案 §149](New_Implementation_Archive.md) |
| F155 | Forwarded multi-arm cyclic byte-lane carry | Live continuation | I/T | [档案 §150](New_Implementation_Archive.md) |
| F156 | Recursive conditional cyclic byte-lane carry | Live continuation | I/T | [档案 §151](New_Implementation_Archive.md) |
| F157 | Forwarded recursive cyclic byte-lane carry | Live continuation | I/T | [档案 §152](New_Implementation_Archive.md) |
| F158 | Grouped recursive cyclic byte-lane source | Live continuation | I/T | [档案 §153](New_Implementation_Archive.md) |
| F159 | Repeated-source recursive cyclic byte-lane transfer | Live continuation | I/T | [档案 §154](New_Implementation_Archive.md) |
| F160 | Composed repeated-source recursive byte-lane transfer | Live continuation | I/T | [档案 §155](New_Implementation_Archive.md) |
| F161 | Multi-carry composed recursive byte-lane tree | Live continuation | I/T | [档案 §156](New_Implementation_Archive.md) |
| F162 | Multiple internal recursive byte-lane groups | Live continuation | I/T | [档案 §157](New_Implementation_Archive.md) |
| F163 | Mixed multi-group recursive byte-lane composition | Live continuation | I/T | [档案 §158](New_Implementation_Archive.md) |
| F164 | Forwarded grouped recursive byte-lane source | Live continuation | I/T | [档案 §159](New_Implementation_Archive.md) |
| F165 | Forwarded repeated-source recursive byte-lane transfer | Live continuation | I/T | [档案 §160](New_Implementation_Archive.md) |
| F166 | Forwarded composed repeated-source byte-lane transfer | Live continuation | I/T | [档案 §161](New_Implementation_Archive.md) |
| F167 | Forwarded multi-group recursive byte-lane tree | Live continuation | I/T | [档案 §162](New_Implementation_Archive.md) |
| F168 | Forwarded mixed multi-group byte-lane composition | Live continuation | I/T | [档案 §163](New_Implementation_Archive.md) |
| F169 | Forwarded multi-carry composed byte-lane tree | Live continuation | I/T | [档案 §164](New_Implementation_Archive.md) |
| F170 | Three-group recursive byte-lane tree | Live continuation | I/T | [档案 §165](New_Implementation_Archive.md) |
| F171 | Double-composed multi-group byte-lane tree | Live continuation | I/T | [档案 §166](New_Implementation_Archive.md) |
| F172 | Forwarded double-composed multi-group tree | Live continuation | I/T | [档案 §167](New_Implementation_Archive.md) |
| F173 | Forwarded Three-Group Recursive Byte-Lane Tree | Live continuation | I/T | [档案 §168](New_Implementation_Archive.md) |
| F174 | Composed Three-Group Recursive Byte-Lane Tree | Live continuation | I/T | [档案 §169](New_Implementation_Archive.md) |
| F175 | Forwarded Composed Three-Group Byte-Lane Tree | Live continuation | I/T | [档案 §170](New_Implementation_Archive.md) |
| F176 | Validated Cross-Prefix Polyhedral Reuse | Query / solver | I/T/E | [档案 §171](New_Implementation_Archive.md) |
| F177 | Verifier-Gated Optimistic Generator Simplification | Query / solver | I/T/E | [档案 §172](New_Implementation_Archive.md) |
| F178 | Native Z3 Tactic Model-Converter Generation | Query / solver | I/T/E | [档案 §173](New_Implementation_Archive.md) |
| F179 | Validated Cross-Size Polyhedral Projection | Query / solver | I/T/E | [档案 §174](New_Implementation_Archive.md) |
| F180 | Bounded Structure-Preserving Polyhedral Field Renaming | Query / solver | I/T/E | [档案 §175](New_Implementation_Archive.md) |
| F181 | Persistent Query-IR Converter Replay | Query / solver | I/T/E | [档案 §176](New_Implementation_Archive.md) |
| F182 | SMT-Validated Exact Integer Projection Relations | Query / solver | I/T/E | [档案 §177](New_Implementation_Archive.md) |
| F183 | Cross-Size Endian-Aware Field Alignment | Query / solver | I/T/E | [档案 §178](New_Implementation_Archive.md) |
| F184 | Exact-Proof Widening Field Alignment | Query / solver | I/T/E | [档案 §179](New_Implementation_Archive.md) |
| F185 | Exact-Proof Narrowing Field Alignment | Query / solver | I/T/E | [档案 §180](New_Implementation_Archive.md) |
| F186 | Conditional Contextual ParaSuit Parameter Graph | 调度与目标引导 | I/T/E | [档案 §181](New_Implementation_Archive.md) |
| F187 | Feedback-Validated Online Token Grammar | 语法与解析 | I/T/E | [档案 §182](New_Implementation_Archive.md) |
| F188 | Fail-Soft Per-Query Selective Concolic Partitioning | Query / solver | I/T/E | [档案 §183](New_Implementation_Archive.md) |
| F189 | Persistent Cost-Aware Mixed Completion Policy | Query / solver | I/T/E | [档案 §184](New_Implementation_Archive.md) |
| F190 | Query-IR-Verified Grammar Hole Completion | 语法与解析 | I/T/E | [档案 §185](New_Implementation_Archive.md) |
| F191 | Plateau-Gated Retained-History Acquisition | 语法与解析 | I/T/E | [档案 §186](New_Implementation_Archive.md) |
| F192 | Independent Parser Oracle and Contextual Grammar Conflict Splitting | 语法与解析 | I/T/E | [档案 §187](New_Implementation_Archive.md) |
| F193 | Exact String/BV Dual-View Query Contract | 字符串双表示 | I/T/E | [档案 §188](New_Implementation_Archive.md) |
| F194 | Guarded Runtime String-Operation Query Artifacts | 字符串双表示 | I/T/E | [档案 §189](New_Implementation_Archive.md) |
| F195 | Validation-First Parallel String Backend Portfolio | 字符串双表示 | I/T/E | [档案 §190](New_Implementation_Archive.md) |
| F196 | Exact-Subdomain Decimal Conversion Semantics | 字符串双表示 | I/T/E | [档案 §191](New_Implementation_Archive.md) |
| F197 | Width-Bound Signed strtol Contract | 字符串双表示 | I/T/E | [档案 §192](New_Implementation_Archive.md) |
| F198 | Executable Cross-Solver String Conformance Gate | 字符串双表示 | I/T/E | [档案 §193](New_Implementation_Archive.md) |
| F199 | Validation-Aware Contextual String Backend Selection | 字符串双表示 | I/T/E | [档案 §194](New_Implementation_Archive.md) |
| F200 | Width-Bound Unsigned strtoul and Overflow-Proof Decimal Folds | 字符串双表示 | I/T/E | [档案 §195](New_Implementation_Archive.md) |
| F201 | Parser-State Structural Trace and Nonterminal-Aware Grammar Context | 语法与解析 | I/T/E | [档案 §196](New_Implementation_Archive.md) |
| F202 | Structural Rule-Coverage-Aware Grammar Scheduling | 语法与解析 | I/T/E | [档案 §197](New_Implementation_Archive.md) |
| F203 | Recursive Parser Production Induction and Independent Grammar Bitmap | 语法与解析 | I/T/E | [档案 §198](New_Implementation_Archive.md) |
| F204 | Bounded Multi-Slot/Mutual CFG Fragments and Derivation Search | 语法与解析 | I/T/E | [档案 §199](New_Implementation_Archive.md) |
| F205 | Incremental ECT Subtree Correspondence and Verified Substitution | 语法与解析 | I/T/E | [档案 §200](New_Implementation_Archive.md) |
| F206 | Seven-Objective Pareto Grammar/ECT Scheduling | 语法与解析 | I/T/E | [档案 §201](New_Implementation_Archive.md) |
| F207 | Epsilon-Pareto Corpus Metadata Ownership and Replacement | 语法与解析 | I/T/E | [档案 §202](New_Implementation_Archive.md) |
| F208 | Packed Parser Forest Families and Verified Epsilon Derivation | 语法与解析 | I/T/E | [档案 §203](New_Implementation_Archive.md) |
| F209 | Shared Packed DAG and Deep Alternative ECT Correspondence | 语法与解析 | I/T/E | [档案 §204](New_Implementation_Archive.md) |
| F210 | Nullable SCC Least Fixed Point and Proof-Carrying Deletion | 语法与解析 | I/T/E | [档案 §205](New_Implementation_Archive.md) |
| F211 | Atomic Multi-Nonterminal ECT Transactions | 语法与解析 | I/T/E | [档案 §206](New_Implementation_Archive.md) |
| F212 | Content-Addressed Incremental Parser Cache Protocol | 语法与解析 | I/T/E | [档案 §207](New_Implementation_Archive.md) |
| F213 | Accepted-Forest PCFG Posterior and Inside/Outside Scheduling | 语法与解析 | I/T/E | [档案 §208](New_Implementation_Archive.md) |
| F214 | Relation-Aware Synchronized Slots with Exploration Reserve | 语法与解析 | I/T/E | [档案 §209](New_Implementation_Archive.md) |
| F215 | Hierarchical Context-Conditioned PCFG and Context-State Forest Mass | 语法与解析 | I/T/E | [档案 §210](New_Implementation_Archive.md) |
| F216 | Prequential Context Calibration and Drift-Safe Global Fallback | 语法与解析 | I/T/E | [档案 §211](New_Implementation_Archive.md) |
| F217 | Recency-Window Calibration, Stale Detection and Recovery | 语法与解析 | I/T/E | [档案 §212](New_Implementation_Archive.md) |
| F218 | Bounded Adaptive Log-Loss Window and Verifiable Cut Certificate | 语法与解析 | I/T/E | [档案 §213](New_Implementation_Archive.md) |
| F219 | Verifier-Gated Grandparent Multi-Context Probabilistic Circuit | 语法与解析 | I/T/E | [档案 §214](New_Implementation_Archive.md) |
| F220 | Bounded Ordered-Sibling Autoregressive PCFG Factor | 语法与解析 | I/T/E | [档案 §215](New_Implementation_Archive.md) |
| F221 | Portable SMT-LIB Signed-Integer Schedule Evidence | 并发状态空间 | I/T/E | [档案 §216](New_Implementation_Archive.md) |
| F222 | Fail-Closed MPI Grammar-Hole QueryStore Wiring | 语法与解析 | I/T/E | [档案 §217](New_Implementation_Archive.md) |
| F223 | Context-Wise Anytime-Valid PCFG Drift Certificates | 语法与解析 | I/T/E | [档案 §218](New_Implementation_Archive.md) |
| F224 | Bounded Second-Order Sibling-History PCFG Factor | 语法与解析 | I/T/E | [档案 §219](New_Implementation_Archive.md) |
| F225 | Cross-Context Infinite-Horizon FWER Audit | 语法与解析 | I/T/E | [档案 §220](New_Implementation_Archive.md) |
| F226 | Five-Level PCFG Confirmatory Ablation and Sealed Artifact | 语法与解析 | I/T/E | [档案 §221](New_Implementation_Archive.md) |
| F227 | Persistent Native Tree-sitter Incremental Parser | 语法与解析 | I/T/E | [档案 §222](New_Implementation_Archive.md) |
| F228 | Proof-Carrying Parser Telemetry and Sealed Cost Ablation | 语法与解析 | I/T/E | [档案 §223](New_Implementation_Archive.md) |
| F229 | Persistent Generalized Earley Complete-SPPF Adapter | 语法与解析 | I/T/E | [档案 §224](New_Implementation_Archive.md) |
| F230 | Proof-Carrying Complete-Forest Telemetry and Calibration | 语法与解析 | I/T/E | [档案 §225](New_Implementation_Archive.md) |
| F231 | Candidate-Paired Cross-Parser Calibration | 语法与解析 | I/T/E | [档案 §226](New_Implementation_Archive.md) |
| F232 | Parser-Neutral Structural Correspondence Certificate | 语法与解析 | I/T/E | [档案 §227](New_Implementation_Archive.md) |
| F233 | Independent GLR/SPPF Differential Oracle | 语法与解析 | I/T/E | [档案 §228](New_Implementation_Archive.md) |
| F234 | Proof-Carrying Cross-Parser Grammar Correspondence | 语法与解析 | I/T/E | [档案 §229](New_Implementation_Archive.md) |
| F235 | Bounded Exhaustive Parser-Language Differential Certificate | 语法与解析 | I/T/E | [档案 §230](New_Implementation_Archive.md) |
| F236 | Proof-Carrying Assumption-Conflict PSCache | Query / solver | I/T/E | [档案 §231](New_Implementation_Archive.md) |
| F237 | Proof-Carrying Backend-Neutral QF_BV Portfolio | Query / solver | I/T/E | [档案 §232](New_Implementation_Archive.md) |
| F238 | Prefix-Keyed Persistent SMT-LIB QF_BV Contexts | Query / solver | I/T/E | [档案 §233](New_Implementation_Archive.md) |
| F239 | Proof-Carrying X-means/BIC Censored Sequence Prior | Query / solver | I/T/E | [档案 §234](New_Implementation_Archive.md) |
| F240 | Bagged/Boosted Censored-Cost Budgeted Sequence Optimizer | Query / solver | I/T/E | [档案 §235](New_Implementation_Archive.md) |
| F241 | Bounded Expected-Improvement Schedule SMBO | Query / solver | I/T/E | [档案 §236](New_Implementation_Archive.md) |
| F242 | SAT-Gated Bounded-Consensus Portfolio Cancellation | Query / solver | I/T/E | [档案 §237](New_Implementation_Archive.md) |
| F243 | Query-IR Structural Context Feature Schema v2 | Query / solver | I/T/E | [档案 §238](New_Implementation_Archive.md) |
| F244 | Fixed-Version Bitwuzla QF_BV Conformance Evidence | Query / solver | I/T/E | [档案 §239](New_Implementation_Archive.md) |
| F245 | Sealed Paired QF_BV Holdout Campaign | Query / solver | I/T/E | [档案 §240](New_Implementation_Archive.md) |
| F246 | Leakage-Checked Solver-Strategy Holdout Campaign | Query / solver | I/T/E | [档案 §241](New_Implementation_Archive.md) |
| F247 | Sealed AFL Edge/Data Coverage Join 与 Feature Calibration | 遥测与覆盖 | I/T/E | [档案 §242](New_Implementation_Archive.md) |
| F248 | Profile-Guided Hydra Control-Flow Melding 与 Original Replay | IFSS / Hydra | I/T/E | [档案 §243](New_Implementation_Archive.md) |
| F249 | Bounded Multi-Arm IFSS Region-State Worklist | IFSS / Hydra | I/T | [档案 §244](New_Implementation_Archive.md) |
| F250 | MemorySSA/AA-Proven IFSS Memory-State Merge | IFSS / Hydra | I/T | [档案 §245](New_Implementation_Archive.md) |
| F251 | Bounded NoMod MemorySSA Def-Chain Recovery | IFSS / Hydra | I/T | [档案 §246](New_Implementation_Archive.md) |
| F252 | Shared Multi-Arm IFSS Data/Memory Partition | IFSS / Hydra | I/T | [档案 §247](New_Implementation_Archive.md) |
| F253 | Partition-Scoped Condition DAG Cache 与 Ownership | IFSS / Hydra | I/T | [档案 §248](New_Implementation_Archive.md) |
| F254 | Bounded Multi-Return Exit-State Lowering | IFSS / Hydra | I/T | [档案 §249](New_Implementation_Archive.md) |
| F255 | Bounded Switch-to-IFSS Chain Lowering | IFSS / Hydra | I/T | [档案 §250](New_Implementation_Archive.md) |
| F256 | Unsigned Range-Balanced Switch Tree | IFSS / Hydra | I/T | [档案 §251](New_Implementation_Archive.md) |
| F257 | Profile-Weighted Optimal Alphabetic Switch Tree | IFSS / Hydra | I/T | [档案 §252](New_Implementation_Archive.md) |
| F258 | Shared-Destination Switch Edge/PHI Multiplicity Proof | IFSS / Hydra | I/T | [档案 §253](New_Implementation_Archive.md) |
| F259 | LLVM Branch-Weight Import 与可重放 Switch Manifest | IFSS / Hydra | I/T | [档案 §254](New_Implementation_Archive.md) |
| F260 | Bounded Multi-Continuation Exit-ID/Live-Out Tuple | IFSS / Hydra | I/T | [档案 §255](New_Implementation_Archive.md) |
| F261 | Proof-Carrying Bounded Affine Natural-Loop Summary | IFSS / Hydra | I/T | [档案 §256](New_Implementation_Archive.md) |
| F262 | MemorySSA/AA-Proven Continuation Memory Tuple | IFSS / Hydra | I/T | [档案 §257](New_Implementation_Archive.md) |
| F263 | Replay-Verifiable Continuation CFG/Tuple Manifest | IFSS / Hydra | I/T/E | [档案 §258](New_Implementation_Archive.md) |
| F264 | Atomic SHA-256 Continuation Artifact Seal | IFSS / Hydra | I/T/E | [档案 §259](New_Implementation_Archive.md) |
| F265 | Independent Bitcode MemorySSA/AA Proof Replay | IFSS / Hydra | I/T/E | [档案 §260](New_Implementation_Archive.md) |
| F266 | Upper-Triangular Affine Loop Closed Form and Manifest | IFSS / Hydra | I/T/E | [档案 §261](New_Implementation_Archive.md) |
| F267 | Bounded Post-Update Break/Continue Exit Tuple | IFSS / Hydra | I/T/E | [档案 §262](New_Implementation_Archive.md) |
| F268 | Bounded Multi-Block Linear-Arm Hydra Alignment | IFSS / Hydra | I/T/E | [档案 §263](New_Implementation_Archive.md) |
| F269 | Partial Store/Live-on-Entry Continuation Memory Tuple | IFSS / Hydra | I/T/E | [档案 §264](New_Implementation_Archive.md) |
| F270 | Bounded Acyclic Nested-MemoryPhi Provenance Tree | IFSS / Hydra | I/T/E | [档案 §265](New_Implementation_Archive.md) |
| F271 | Bounded Multi-Break Exit-Priority Circuit | IFSS / Hydra | I/T/E | [档案 §266](New_Implementation_Archive.md) |
| F272 | Proof-Carrying Unequal Linear-Arm Hydra Alignment | IFSS / Hydra | I/T/E | [档案 §267](New_Implementation_Archive.md) |
| F273 | Proof-Carrying Bounded Internal-Tree Hydra Melding | IFSS / Hydra | I/T/E | [档案 §268](New_Implementation_Archive.md) |
| F274 | Unified Sealed Independent Transformation Replay | IFSS / Hydra | I/T/E | [档案 §269](New_Implementation_Archive.md) |
| F275 | Proof-Carrying Continuation Byte-Lane Partial Overlap | IFSS / Hydra | I/T/E | [档案 §270](New_Implementation_Archive.md) |
| F276 | Guarded Two-Arm Symbolic Alias Partition | IFSS / Hydra | I/T/E | [档案 §271](New_Implementation_Archive.md) |
| F277 | Proof-Carrying Cyclic Byte-Lane Continuation MemoryPhi | IFSS / Hydra | I/T/E | [档案 §272](New_Implementation_Archive.md) |
| F278 | Bounded Multi-Level Finite Pointer-Union Partition | IFSS / Hydra | I/T/E | [档案 §273](New_Implementation_Archive.md) |
| F279 | Two-Write Guarded Priority Composition | IFSS / Hydra | I/T/E | [档案 §274](New_Implementation_Archive.md) |
| F280 | Conditional Cyclic Byte-Lane Transfer | IFSS / Hydra | I/T/E | [档案 §275](New_Implementation_Archive.md) |
| F281 | Two-Latch Cyclic Byte-Lane MemoryPhi | IFSS / Hydra | I/T/E | [档案 §276](New_Implementation_Archive.md) |
| F282 | Pointer-Union Writer Priority Composition | IFSS / Hydra | I/T/E | [档案 §277](New_Implementation_Archive.md) |
| F283 | Conditional Multi-Latch Cyclic Byte-Lane MemoryPhi | IFSS / Hydra | I/T/E | [档案 §278](New_Implementation_Archive.md) |
| F284 | Bounded 2--4-Latch Cyclic Byte-Lane MemoryPhi | IFSS / Hydra | I/T/E | [档案 §279](New_Implementation_Archive.md) |
| F285 | Nested-Predicate Cyclic Byte-Lane Transfer Tree | IFSS / Hydra | I/T/E | [档案 §280](New_Implementation_Archive.md) |
| F286 | Bounded Ordered Pointer/Guard Writer Graph | IFSS / Hydra | I/T/E | [档案 §281](New_Implementation_Archive.md) |
| F287 | Bounded Acyclic SESE DAG and Local-PHI Predication | IFSS / Hydra | I/T/E | [档案 §282](New_Implementation_Archive.md) |
| F288 | Profile-Bound and Concurrent-Safe Transformation Evidence | IFSS / Hydra | I/T/E | [档案 §283](New_Implementation_Archive.md) |
| F289 | Signed Merkle-Logged Cross-Host Transformation Bundle | IFSS / Hydra | I/T/E | [档案 §284](New_Implementation_Archive.md) |
| F290 | LLVM Semantic Refinement and Cross-Major Exact Replay | IFSS / Hydra | I/T/E | [档案 §285](New_Implementation_Archive.md) |
| F291 | Exact Symbolic-Index Heap-Region Writer Graph | IFSS / Hydra | I/T/E | [档案 §286](New_Implementation_Archive.md) |
| F292 | Symbolic-Region Cyclic Byte Fixed Point | IFSS / Hydra | I/T/E | [档案 §287](New_Implementation_Archive.md) |
| F293 | Bounded Shared-Predicate DAG Guard Hash-Consing | IFSS / Hydra | I/T/E | [档案 §288](New_Implementation_Archive.md) |
| F294 | Conditional Multi-Latch Symbolic-Region Transfer | IFSS / Hydra | I/T/E | [档案 §289](New_Implementation_Archive.md) |
| F295 | Dominance-Proven Cross-Region Guard Hash-Consing | IFSS / Hydra | I/T/E | [档案 §290](New_Implementation_Archive.md) |
| F296 | Same-Latch Ordered Symbolic-Region Multiwriter | IFSS / Hydra | I/T/E | [档案 §291](New_Implementation_Archive.md) |
<!-- FEATURE_INDEX_END -->

## 25. 关联文档

- [`Architecture_QA3.md`](Architecture_QA3.md)：执行次序、位图、约束形态、求解策略、
  种子影响、ICSE'23 和 benchmark 的逐问分析；
- [`Correctness_Review_Fixes_2026-07.md`](Correctness_Review_Fixes_2026-07.md)：
  correctness 审查和修复；
- [`sota_hybrid_execution_2026.md`](sota_hybrid_execution_2026.md)：学术来源、SOTA
  映射和边界；
- [`SOTA_Gap_Audit_2026-07-24.md`](SOTA_Gap_Audit_2026-07-24.md)：逐论文语义差距；
- [`Recent_Work_and_Next_Research_Plan.md`](Recent_Work_and_Next_Research_Plan.md)：
  近期实现和后续研究；
- [`../benchmark/README.md`](../benchmark/README.md)：benchmark 命令、schema 和产物。

主要技术来源包括
[QSYM](https://www.usenix.org/conference/usenixsecurity18/presentation/yun)、
[Backsolver](https://doi.org/10.1145/3712194)、
[Pangolin](https://doi.org/10.1109/SP40000.2020.00063)、
[GenSlv](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/44/Generator-Solving-for-Symbolic-Execution)、
[PSCache](https://doi.org/10.1145/3660817)、
[SymCC-str](http://theory.stanford.edu/~barrett/pubs/CB26-abstract.html)、
[Lase](https://2026.splashcon.org/details/oopsla-2026/43/Online-Input-Grammar-Synthesis-Aided-Symbolic-Execution)、
[Data Coverage](https://www.usenix.org/conference/usenixsecurity24/presentation/wang-mingzhe)、
[CoFuzz](https://doi.org/10.1109/ICSE48619.2023.00045)、
[GenSym](https://conf.researchr.org/details/icse-2023/icse-2023-technical-track/39/Compiling-Parallel-Symbolic-Execution-with-Continuations)、
[ConDPOR](https://doi.org/10.4230/LIPIcs.CONCUR.2025.26)、
[Hydra](https://doi.org/10.1145/3798202)、
[SMTgazer](https://conf.researchr.org/details/ase-2025/ase-2025-papers/47/SMTgazer-Learning-to-Schedule-SMT-Algorithms-via-Bayesian-Optimization)、
[Cottontail](https://mboehme.github.io/paper/SP26-cottontail.pdf) 和
[UCSan](https://www.usenix.org/conference/osdi26/presentation/yin)。
论文中的性能数字、证明和适用范围属于原论文；本文只描述本仓库已经实现并有证据
支持的部分。
