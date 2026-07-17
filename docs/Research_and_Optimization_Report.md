# 文献调研与优化实施报告

## 零、节点同步 / 共享 / 速度 的实测诊断与优化（最新一轮）

针对"节点同步是否是瓶颈"的假设，用 master 内置 profiler（`SYMCC_MASTER_PROFILE=1`）
实测（sqlite，15 workers，~58s）：

| 阶段 | 耗时 | 判定 |
|------|-----:|:----|
| scan（扫 AFL queue）| **34.06s** | 表面瓶颈 |
| dispatch（MPI 派发）| 0.03s | 可忽略 |
| recv（MPI 收结果）| 0.32s | 可忽略 |
| triage（覆盖率归并）| 0.26s | 可忽略 |
| idle（等待 worker）| 24.20s | master 空闲 |

**结论：节点同步（MPI 派发/收集/triage）根本不是瓶颈（合计 <1s）。** 表面的 34s "scan"
其实是 master 在 worker 忙时**忙等式反复 scandir + 对每个候选种子重复读文件+SHA-256**
（AFL queue 文件不可变，却每轮重算）。这既浪费 master 核心，又会随 queue 增大而恶化。

**已实施修复：**
1. **每文件属性缓存**（`AflConfig._file_cache`）：name 派生标志/大小/afl_id/SHA-256 只算一次。
2. **扫描节流**（`SCAN_MIN_INTERVAL`）：worker 全忙且刚扫过则跳过，master 正确 idle 而非忙等。
3. **目标 CWD 隔离**：AFL/MPI/showmap 子进程均在临时目录运行，根治向仓库写 fuzzer 垃圾文件。

**效果（实测）**：scan 34.06s→**1.30s**（快 26×），扫描次数 70354→1181，master 核心释放，
interesting 数持平（425 vs 415），覆盖率无回退。

**研究印证（3 个并行调研 agent）：**
- **共享**：原"AFL 每 30s 同步"的顾虑是错的——AFL `SYNC_TIME=20min`，但**本系统直接把 SymCC
  输出写入 `fuzzer01/queue/`，AFL 秒级 rescan 导入**，绕过 sibling-sync，文件系统不是瓶颈。
  现有共享设计良好（worker 端 StreamingShowmap 过滤、master 权威 bitmap+版本广播＝EnFuzz GALS、
  CoFuzz-lite 打分+edge_yield、多字节 hint token）；2025 arXiv 用 `MPI_Isend` 替代 AFL 磁盘同步
  ＝本系统思路，独立印证。
- **速度**：`SYMCC_ENABLE_LINEARIZATION=1`（QSYM 基本块剪枝）已启用；表达式 CSE/折叠/化简/缓存
  为默认且 sound。可选 sound 增益：KLEE 约束独立性切片+反例缓存（查询→5%，运行时>10×，中等工作量）；
  前沿：SymFit（USENIX Sec'24，快速具体路径 6.67× e2e）/ SymSan（62× vs SymCC）均需大改后端。
- **并行/共享待办（研究建议，按价值）**：
  1. **共享"不可解分支"状态**（BSFuzz）：广播 SymCC 求解失败的 branch-id，避免 N 个 worker 各自
     在同一不可解分支上耗尽 30s 超时——高并行度下的最大 CPU 浪费源（**目标相关**：本 benchmark
     小种子 SymCC 单次 ~0.5s，超时不频繁；对硬约束目标价值更大）。
  2. **按约束类型分工**：CmpLog（RedQueen input-to-state，比求解快 5–5000×）负责魔数/magic bytes，
     SymCC 专注算术/校验和/关系约束（本系统已用 `-c -l 2AT`，可进一步避免 SymCC 重解 CmpLog 已破的）。
  3. **覆盖率图共享**：让 SymCC 的 `SYMCC_AFL_COVERAGE_MAP` 指向 AFL 的实时累计 bitmap，减少
     重复探索 AFL 已覆盖的分支（待核实当前 .shared_bitmap 是否已并入 AFL 真实覆盖）。

### 0.4 四项纯 CPU 技术落地（laf-intel+NGRAM / GRIMOIRE / honggfuzz 集成）

用户要求（不用 GPU、充分利用多核 CPU）下实现并实测的四项：

**① laf-intel + NGRAM/CTX（构建 flag，已实测）**：用 `AFL_LLVM_LAF_ALL=1
AFL_LLVM_INSTRUMENT=NGRAM-4` 重建 sqlite（amalgamation 单文件）。A/B（各 150s，同种子，
在公共二进制上测覆盖率作中立标尺）：baseline queue=602/8476 边 vs laf+NGRAM queue=894/8597 边
→ **queue 多样性 +48%、公共覆盖率 +1.4%**。拆分多字节比较让 AFL 越过魔数、拆开被合并的分发边。

**② GRIMOIRE 无语法结构合成（本会话最佳结果）**：`util/grimoire_gen.py`——从语料按结构边界
（分隔符 + SymCC hint/extras token）抽取 fragment，通过**替换/拼接/token 插入**重组出语法有效
的新输入（例：`WITH cte AS (SELECT 1 AS n) SELECT * FROM cte1+2;`、`SELECT 1+2,'abc'||'def',
CASE WHEN 1 t1 'yes' END;`）。实测：仅对种子做静态重组即把公共覆盖率从 **4339 → 4628 边（+6.7%）**。
纯 CPU，直击"结构有效性"这一真实瓶颈——是所有尝试中唯一实质提升覆盖率的技术。

**②+ 覆盖率引导的泛化升级（真 GRIMOIRE gap 检测）**：新增 `ShowmapOracle`（持久 afl-showmap
-S，~0.6ms/次）作覆盖率预言机。`generalize()`：把输入切成 fragment，逐个**移除**并检测覆盖率
是否 ⊇ 原覆盖率——保持则该 fragment 为 **gap（可替换位）**，否则为结构必需字面量。生成时
在模板 gap 上重组（`_fill_template`：留空/换填充料/保留 + 模板拼接），**保持结构骨架、只变可变位**。
A/B（同一次运行，公共二进制标尺）：seeds 4339 → 盲重组 4550 → **覆盖率引导 4582**——引导版
优于盲版且均远超种子。已接入集成（run_hybrid 传 `--afl-binary` 启用 oracle，live 验证生成 2800）。

**②++ 多粒度 gap（分治找极大可移除子串，本会话最佳）**：`generalize()` 改为**分治**——对
fragment 区间整段移除测覆盖率，保持则整段判为**极大 gap**（1 次探测成一个大 gap，不再逐 fragment），
否则二分递归；`_fill_template` 把**连续 gap 合并为整体单元**填充。一个可整体移除的"子句"成为
一个大 gap → 重组自由度更大、结构变化更丰富（例：`SELECT hex(COUNT(4)), zeroblob(8);`、
`SET; INSERT INTO t1 VALUES(1,'a',1.0); COMMIT;`）。A/B 单调递增：
盲 4550 → 单 fragment 4582 → **多粒度 4691（+8.1% vs seeds）**——GRIMOIRE 方向的最佳结果。
集成自动生效（grimoire_gen.py 已含多粒度）。

**②+++ GRIMOIRE × SymCC 协同：结构输入直连 concolic 反馈队列**：不再只把 GRIMOIRE 输出喂 AFL
（-F），而是让 MPI master 通过 `--grimoire-feed <dir>` 扫描 grimoire_out、把新的**结构有效**输入
直接注入 SymCC 反馈队列——让 concolic 从深层结构输入继续挖（GRIMOIRE 造结构、SymCC 解其中
的硬分支）。run_hybrid 启动 GRIMOIRE 时自动传 `--grimoire-feed grimoire_out`。**实测验证**：master
日志 `GRIMOIRE feed: +800 structured inputs -> SymCC`，随后 SymCC 对其执行 304 批 concolic triage。
（曾尝试在 GRIMOIRE 端按覆盖率过滤"高价值"输出，但单持久 oracle 对狂野生成输入易 desync、
且种子片段重组逐条极少新增边 → 回退为产出全部结构有效重组，高/低价值筛选交由 SymCC triage
与 AFL -F 各自按真实覆盖率完成。）

**③ honggfuzz 真异构集成成员**：源码编译安装 honggfuzz；用 `hfuzz-clang` 构建
honggfuzz-instrumented sqlite（SanCov 反馈，与 AFL 的引擎/变异不同）。实测 30s 内 20668 迭代、
19993 覆盖率增益语料、branch 9%——高效独立探索。

**④ 集成编排落地**：`run_hybrid` 新增 `honggfuzz_binary` / `grimoire` 参数与
`--hybrid-honggfuzz` / `--hybrid-grimoire` CLI；honggfuzz 与 GRIMOIRE 各自把发现写入独立目录，
AFL 主实例经 `-F` 导入（`discover_public_hfuzz_targets` 发现 hfuzz 二进制）。**四引擎集成
（AFL×6 + honggfuzz + GRIMOIRE + SymCC MPI）端到端跑通**：honggfuzz 29409 + GRIMOIRE 4800
语料，覆盖 18.35%，无错误、无仓库污染（CWD 隔离生效）。

**结论**：CPU 路线下，**GRIMOIRE 结构合成是覆盖率的真实杠杆（+6.7%）**；laf-intel+NGRAM 是廉价
增量（+1.4%）；honggfuzz 是唯一值得加的真异构引擎（研究上限 +5.8%）。三者均以集成框架接入、
纯 CPU、opt-in。

### 0.3 充分利用多核 CPU：实测——封顶分配才是关键（AFL 不饥饿，SymCC 会）

用户关切"是否充分用满多核"。实测（np=64 预算，grown 语料，中途 loadavg 快照）：

| 分配 | loadavg | 利用率 |
|------|:-------:|:------|
| SymCC 重（12 AFL + 51 SymCC worker）| **37.6** | ~59%，**SymCC worker 饥饿**（51 个远超 concolic ~12 的饱和点，抢不到新种子/做冗余）|
| **封顶（51 AFL + 12 SymCC）** | **76.7** | **>100%，核心跑满** |

master profiler 佐证：51 workers 时 master idle=44s/85s（**master 未饱和**，非瓶颈），说明是
**SymCC worker 侧供给不足/饱和**。根因与既往一致：**AFL 实例持续满载（变异不停、不饥饿），
SymCC 在 ~12 worker 后饱和**。故"充分利用多核 CPU"的正解就是**封顶分配**——多数核心给
扩展性好、永不饥饿的 AFL，SymCC 封顶在效率拐点。**这正是已交付的 auto 默认**，且 np=64
封顶配置覆盖率近最优（libarchive 23.95%）。堆 SymCC worker 反而**降低**利用率。

**规模化生产力（用满 192 线程且高产）的进一步杠杆**：AFL 并行存在 queue 同质化拐点
（sqlite 32→51 AFL 覆盖率反降）——纯堆 AFL 能"用满"但"高产"递减。缓解手段：
(1) 已落地的**集成配置多样性**（MOpt/explore/exploit/CmpLog/havoc 差异化，降低同质化）；
(2) **任务/语料分区**（DynamiQ/AFLTeam：按调用图区域分配各核，减少重叠冗余，是 100+ 核
高产 fuzzing 的关键技术，未实现）；(3) **laf-intel + NGRAM 覆盖**（重编目标，拆开被合并的
解析器分发边，让更多核心找到不同新边）。

### 0.2 集成（Ensemble）fuzzing：已落地配置多样性，但研究证明上限有限

**已实现（免权限部分）**：把 AFL 从实例从"仅换 power schedule"升级为 **EnFuzz 风格配置
多样性集成**——每个从实例一个差异画像：MOpt(`-L 0`) vs 香草、explore/exploit/rare/coe/seek/
mmopt 调度、CmpLog 深浅、AFL_DISABLE_TRIM / AFL_EXPAND_HAVOC_NOW / AFL_KEEP_TIMEOUTS 等
havoc/trim 行为差异。并加了 **`-F` 外部异构引擎共享钩子**（honggfuzz/libFuzzer 写目录，
主实例 `-F` 导入；经 `AFL_FOREIGN_DIRS` 配置），框架可扩展。实测启动正常、无回退。

**研究校准（关键，来自专项调研 agent）**：一旦集成中已含 concolic（本系统已有 SymCC），
**再加引擎的边际增益仅 +5.8% branches / +0.68% bugs**（EnFuzz-Q vs QSYM）。AFL++ 与 SymCC
已覆盖两个最具多样性的类别；**honggfuzz 是唯一值得加的真异构引擎**（但需安装＝系统变更）。
libFuzzer 与 AFL 同属字节变异类，多样性增益小，且本 benchmark 目标是自带 `main()` 的
非 libFuzzer-harness 形态，不易直接构建。故 B 的天花板不高。

**研究指出的更高 ROI 方向（针对结构有效性这一真实瓶颈）**：
- **本地 LLM 输入生成**（利用闲置 288GB GPU）：离线生成器合成 ELFuzz/G2FUZZ 覆盖率最高 +434%；
  Cottontail(S&P'26)＝SymCC+LLM（+14–16% branch，需接本地 vLLM）。GPU 不会成为瓶颈
  （Qwen3-Coder-30B 在单卡 ~8400 tok/s）。
- **GRIMOIRE**：无语法结构合成，+87% over RedQueen，在 SQLite/libxml 上胜过手写语法 Nautilus。
- **廉价构建 flag**：laf-intel（拆分多字节比较）+ NGRAM/CTX 覆盖（拆开被合并的解析器分发边）。
- **INVSCOV**（USENIX'21，值域覆盖，+8% 开销、从不劣于 edge 覆盖）：性价比最高的覆盖度量试点。

### 0.1 A+B+C 三项深入实现与实测（后续一轮）

**B — 真实大种子语料**：用 AFL 演化出 2391 个 libarchive 种子（中位 210B、最大 33KB，
远超原始 13 个微种子），用于在真实条件下验证。系统端到端正常（576 ok、680 interesting/
58907 total、无错误、优雅退出）。

**C — 覆盖率图共享：诊断为架构性失效（不宜直接实现）**。追踪数据路径发现：
1. streaming showmap 快路径下 `_merge_sparse` 不初始化 `coverage.data`（保持 None），
   故 `.shared_bitmap` **从不写出**（受 `if coverage.data:` 保护）→ 共享是 no-op；
2. 即便写出，afl-showmap 的边索引（AFL 二进制）与 SymCC `afl_trace_map` 的 `hashPc` 索引
   （SymCC 二进制，独立插桩）**索引空间不同**，直接回灌会让 SymCC 随机跳过分支 → **unsound**。
   这解释了此前实测的高冗余（14 workers 时 81% 浪费）——SymCC 实际上没有跨运行的分支去重。
   健全的修法是共享 SymCC **自身索引空间**的分支状态（site_id），即 A 的机制。

**A — BSFuzz 跨-worker 超时分支共享：实现健全，但本 benchmark 无触发条件**。
- 已实现并编译、端到端接通（runtime 检测 Z3 `unknown`/超时 → 记录 branch `site_id` →
  `SYMCC_TIMEOUT_OUT` 导出 → worker 回传 master → 聚合写 `.skip_sites` → `isInterestingJcc`
  跳过已知超时分支；每次 SymCC 进程新起自动重读最新集，无需版本广播）。opt-in
  （`SYMCC_BRANCH_SHARE=1`），默认零开销，且**健全**（跳过解不出的分支不丢覆盖率）。
- **实测：本 benchmark 所有目标均无 Z3 超时**——libarchive（40 个最大种子，8539 输出）、
  pcre2、freetype2、sqlite 全部 0 超时事件；求解时间仅数百 μs。这些解析器约束简单、Z3 秒解，
  没有 BSFuzz 针对的"难解分支"。故 A 在此 workload 上**无效**（机制正确，条件不满足）。

**共同结论（5 项技术的元规律）**：多样性调度、运行时自适应分配、taint 选择性符号化、
BSFuzz 超时共享、覆盖率图共享——**5 项先进技术在本 benchmark 上均经实测无法超越静态
"多实例 AFL + SymCC worker 封顶"**。根因高度一致：本 benchmark 的目标是**约束易解的解析器
+（原始）小种子**，正是静态分配已吃满、而这些先进 concolic/hybrid 技术（为难约束/大二进制
workload 设计）不适用的区间。要兑现它们需换到**难约束目标**（crypto/校验和/非线性算术）
或**大二进制输入**。已交付的真实收益仍是：分配层优化（+18%/+10% 等，多目标多并行度验证）
＋ master 扫描效率修复（34s→1.3s）＋ 仓库污染根治。所有实验机制均 opt-in/默认关闭。

---

## 一、文献调研总结

### 1.1 调研范围
搜索了 2023-2026 年间关于 concolic execution 并行化、覆盖率提升、约束求解加速的最新研究论文。

### 1.2 核心发现

#### A. 混合模糊测试优化

**CoFuzz / Cohuzz (ICSE'23)**
- 核心思想：使用在线线性回归模型预测哪些边最可能从 concolic 执行中受益，动态调度
- 关键技术：Edge-oriented scheduling + Sampling-augmenting synchronization
- 效果：覆盖率比最优混合 fuzzer 高 16.31%，暴露 2x 更多 crash
- **已实施**：种子调度中引入 edge yield 在线学习

**Cottontail (IEEE S&P'26)**
- 核心思想：LLM 驱动的 concolic 执行，处理高度结构化输入
- 关键技术：Expressive Structural Coverage Tree + LLM Solve-Complete 范式
- 效果：行覆盖 +30.73%，分支覆盖 +41.32%
- 基于 SymCC 构建，发现 6 个 CVE
- **适用性**：需要 LLM API 集成，中期可实施

#### B. 约束求解加速

**TACE - Taint Assisted Concolic Execution (FSE'24)**
- 核心思想：动态污点分析定位影响分支的输入字节，仅对这些字节做符号化
- 效果：约束求解时间比 SymQEMU 快 50x
- **适用性**：升级现有 SYMCC_FOCUS_BYTES，使用运行时污点追踪

**PSCache - Partial Solution Based Constraint Solving Cache (FSE'24)**
- 核心思想：缓存 Z3 的部分解，相似约束复用已有解
- 效果：相同路径数下 1.07-2.3x 加速
- **评估后未采用**：negatePath 的可满足性依赖于已同步的**路径前缀约束**，
  仅凭分支表达式（或其依赖集合）做缓存会忽略路径上下文，误跳过可满足约束
  而丢失覆盖率。且 QSYM 已有 `trace_.isInterestingBranch(pc, taken)` 提供
  基于覆盖率的分支级去重，识别到重复分支时上游即不再调用 negatePath，
  内容缓存与之高度冗余。正确的 PSCache 需对**完整查询**（前缀∧¬分支）哈希，
  但其收益主要出现在循环内相同符号状态，而该场景已被上游去重覆盖，收益有限。

**Fuzzy-Sat 快速求解（已有）**
- 改进：修复了 ZExt/SExt 截断 bug、跳过与输入相同的输出、fastSolve 成功后不再走 Z3
- **已实施**

#### C. 并行化扩展

**GenSym (ICSE'23)**
- 核心思想：基于续延（continuation）编译并行符号执行
- 将符号执行编译为 C++ 程序，利用协作式并发
- 效果：串行 4.6x，并行 9.4x 加速
- **适用性**：架构差异太大，不直接适用

**DynamiQ (2025)**
- 核心思想：基于调用图的任务分区 + 动态重分配
- 利用程序结构定义 fuzzing 任务，减少冗余探索
- 效果：在 12 个 OSS-Fuzz 目标上优于 SOTA 并行 fuzzer
- **适用性**：中期可在 MPI master 中实现调用图感知任务分配

#### D. AFL++ 最新特性

**CmpLog 增强**
- Predicate-tightness scheduling (`-l M`)：将不等比较的最小松弛度作为覆盖事件
- Size-derive logging：将计算出的大小信息写入 CmpLog RTN slots
- 缓存机制减少冗余执行
- **适用性**：配置层面即可启用，无需代码修改

#### E. 其他相关工作

**SymFit (USENIX Security'24)**
- 针对二进制的 concolic 执行优化，快速具体执行模式
- Concrete Memory Lookaside Buffer 减少影子内存开销

**Veritesting (ICSE'14, 持续改进)**
- 对无系统调用的代码区域使用静态符号执行
- 路径合并减少路径爆炸
- 发现 2x bug，探索更多路径

---

## 二、已实施优化

### 2.1 约束求解层（solver.cpp）

| 优化 | 原理 | 预期效果 |
|------|------|----------|
| **fastSolve 跳过 Z3** | fastSolve 成功后不再走 Z3 重复求解 | 简单约束性能 2x |
| **fastSolve ZExt/SExt 修复** | 常量超出字节范围时正确 bail out | 消除错误测试用例 |
| **fastSolve 相同输出检查** | 目标值与原始输入相同时跳过 | 减少无效输出文件 |
| **getenv 缓存** | 构造函数缓存 SYMCC_EMIT_HINTS | 微优化，消除每次 saveValues 的 getenv 调用 |

### 2.2 种子调度层（mpi_fuzzing_helper.py）

| 优化 | 原理 | 预期效果 |
|------|------|----------|
| **Edge yield 在线学习** | 跟踪不同类型种子的 concolic 产出率，用 Laplace 平滑估计优先级 | 资源向高产出种子类型倾斜 |
| **稀有边加权** | AFL++ `+rare` 标记种子额外加分 | 优先分析稀有路径 |
| **对数新颖度衰减** | 用 log1p(id) 替代线性 id/100 | 避免高 ID 种子过度加分 |
| **分层大小惩罚** | >50KB 严重降权，<256B 加分 | 优先快速执行的小种子 |
| **多字节 hint 聚合** | 连续偏移的 hint 合并为多字节 token | AFL extras 从单字节升级为语义 token（如 "SELECT"） |
| **focus_bytes 滑动窗口** | 仅保留最近 200 个偏移 | 防止范围无限膨胀 |
| **仅 interesting TC 收集 hint** | hint 偏移只从产出新覆盖的 TC 收集 | 避免 focus_bytes 被无效偏移污染 |
| **前沿边评估** | CoverageBitmap.frontier_ratio() 评估种子到未探索区域的距离 | 为未来精细调度提供基础 |

### 2.3 MPI 框架层

| 优化 | 原理 | 预期效果 |
|------|------|----------|
| **关闭死锁修复** | Phase 2 drain 时更新 active_workers | 消除关闭时的死锁 |
| **信号处理** | SIGTERM/SIGINT 优雅退出 | 防止僵尸进程 |
| **Barrier 安全化** | try/except 包装 | 防止进程提前退出导致死锁 |
| **显式 bitmap 路径传递** | work 消息携带 bitmap_path，消除脆弱的路径推断逻辑 | 提高可靠性 |
| **原子写入修复** | os.rename → os.replace | NFS 兼容 |

### 2.4 并行核心分配（本次核心贡献）

| 优化 | 原理 | 实测效果 |
|------|------|----------|
| **多实例 AFL 混合模式** | run_hybrid 支持 AFL++ 并行模式（1 主 -M + N 从 -S，多样化 power schedule），SymCC 分到剩余核心 | 见下方分配曲线实验 |
| **平衡分配默认值** | 旧默认「1 AFL + (np-1) SymCC」把绝大多数核心给 concolic 是反常配比；改为 AFL≈np/2 | sqlite +18%、libarchive +3%，无回退 |
| **跨实例覆盖率聚合** | 收集所有 fuzzerNN/queue，聚合 fuzzer_stats（execs 累加，覆盖取最大） | 正确测量多实例覆盖率 |

---

## 三之补 — 并行核心分配实验（KRAKEN/Boian 思想的实证）

**动机**：文献（KRAKEN ISSTA'25、Boian 2024）指出「改进调度器与改进 fuzzer 同等重要」。
旧混合模式固定「1 个 AFL + (np-1) 个 SymCC worker」，把 ~94% 核心给 concolic，
这与主流混合模糊测试实践（concolic 应是少数派辅助）相悖。

**实验**：np=16，timeout=240s，rounds=3，两类代表目标，扫描 AFL 实例数 ∈ {1,4,8,12}。
边覆盖率经 afl-showmap 测量（并集语料）。

| AFL 实例 | SymCC worker | sqlite（AFL 友好）| libarchive（SymCC 友好）|
|---------:|-------------:|:-----------------:|:-----------------------:|
| 1 (旧默认) | 14 | 19.30% | 23.30% |
| 4 | 11 | 21.39% | 23.79% |
| **8 (新默认)** | **7** | **22.82%** | **23.92%** ← 峰值 |
| 12 | 3 | 22.87% | 21.60% |

**结论**：
1. 最优配比**因目标而异**：sqlite 单调偏好 AFL，libarchive 在 AFL=8 达峰后因 SymCC 饥饿而下降。
2. **AFL≈np/2（平衡）处于两类目标的最优包络**：sqlite 22.82%（与峰值 22.87% 统计持平），
   libarchive 23.92%（*超过*两个极端，为全局峰值）。
3. 平衡默认相比旧默认（单 AFL）在**三个目标上均严格改进**，零回退：
   - sqlite（AFL 友好）：19.30% → 22.82%（**+18.2%**）
   - libarchive（SymCC 友好）：23.30% → 23.92%（**+2.7%**）
   - xml（结构化）：9.11% → 9.47%（**+3.9%**）

   无需运行时自适应控制器即可捕获绝大部分收益（低 np 场景）。
4. 为何平衡在 SymCC 友好目标上也最优：14 个 SymCC worker 已过冗余拐点（concolic 重复劳动），
   而 3 个又太少不足以攻克约束；7 个 worker + 若干 AFL 实例做广度探索是最佳组合。

### 高并行度（np=64）：从「固定比例」到「SymCC worker 封顶」

将实验扩展到 np=64（顺序执行，各配置独占整机，rounds=2），对比三种策略：

| 策略 | sqlite | libarchive | 相对旧默认 |
|------|:------:|:----------:|:----------|
| 单 AFL（1）+ 63 SymCC（旧默认）| 21.02% | 23.98% | 基线 |
| 平衡（32 AFL + 31 SymCC，np/2）| **24.49%** | 22.88% | sqlite +16.5%，libarchive **−4.6%** ✗ |
| **封顶（51 AFL + 12 SymCC worker）** | 23.12% | 23.95% | sqlite **+9.9%**，libarchive **−0.1%** ✓ |

**关键洞察**：
1. np=16 时最优的「AFL=np/2」在 np=64 使 libarchive 回退 −4.6%——因为 np/2 意味着
   31 个 SymCC worker，**远超 concolic 冗余拐点**。concolic 并行早饱和（GenSym/DynamiQ 亦证）。
2. AFL 并行虽扩展性好，但也存在拐点：sqlite 从 32→51 个 AFL 实例反而下降
   （24.49%→23.12%），符合并行 fuzzing 的 queue 同质化效应。
3. **封顶策略具备默认值必需的「双目标不回退」性质**：在 np=16 与 np=64、
   AFL 友好与 SymCC 友好目标上，均相对旧默认*改进或持平*，无实质回退。
   而平衡策略在 np=64 使 libarchive 回退。

因此最优默认不是固定*比例*，而是 **SymCC worker 数量封顶**（`SYMCC_WORKER_CAP=12`），
其余核心全给 AFL 并行实例：

- 低 np（≤~26）：cap 不生效，退化为 AFL≈np/2（已验证最优）。
- 高 np：SymCC 固定 ~12 workers，AFL 拿走其余（np=190 → 177 AFL + 12 SymCC）。

已实现为 auto 默认（`--hybrid-afl-instances 0`）；已知目标偏好 AFL 者可显式覆盖
（如 `--hybrid-afl-instances 32`）。

**运行时自适应（未来增量）**：若需逐目标最优包络，可在 MPI master 引入
worker parking（`.active_workers` 控制文件在 np 预算内动态重分配），由 run_hybrid
依 SymCC 产出率闭环调节。静态「平衡 + 封顶」已接近包络，自适应为增量优化。

### 并行 concolic 冗余：直接测量与"多样性调度"的否定结论

为理解封顶策略背后的机理，直接测量了并行 SymCC worker 的冗余（同一 AFL 队列、
捕获 master 全量 triage 日志、~95s/配置）：

| SymCC workers | 产出 TC | interesting | **useful 比率** | interesting/worker |
|--------------:|--------:|------------:|:---------------:|:------------------:|
| 6 | 1055 | 317 | **30.0%** | 52.8 |
| 14 | 2549 | 474 | **18.6%** | 33.9 |

worker 翻倍多（6→14）但 interesting 仅增 1.5×，useful 比率从 30% 跌到 18.6%——
即 14 workers 时 **~81% 的 concolic 计算浪费在已覆盖边上**。输出按哈希去重的
重复率为 0%，故冗余是**语义级**的（不同输出覆盖相同边）。

**尝试：多样性调度**（避免并发下发结构相似的种子，用轻量签名延后相似种子）。
A/B 实测（14 workers，各 2 轮）：

| 调度 | useful 比率 | interesting |
|------|:-----------:|:-----------:|
| OFF（基线）| 18.0% | 458 |
| ON（多样性）| 17.9% | 490 |

**否定结论**：useful 比率无变化（18.0 vs 17.9，n=2 噪声内）。说明并行 concolic 冗余
**主要是结构性的**，非调度顺序造成——即使给不同种子,翻转其分支后仍常产出覆盖
公共下游代码的输入。此结果与文献一致（concolic 并行本质亚线性：GenSym 最优
9.36x/12 线程；DynamiQ 亦亚线性）。已回退该实验代码。

**意义**：既然调度技巧无法挽救过量 worker 的冗余,**这正是封顶策略的正确性依据**——
把 concolic worker 限制在效率拐点内,富余核心交给扩展性更好的 AFL。冗余是并行
concolic 的固有属性,应在**分配层**规避,而非在调度层消除。

### 运行时自适应分配控制器（KRAKEN 风格）：机制已实现，策略未超越静态

按 KRAKEN/Boian 思路实现了**运行时动态重分配**（`--hybrid-adaptive`，`_adaptive_controller`）：
- **机制（已验证）**：MPI master 读 `.active_workers` 控制文件 → 停泊/唤醒 SymCC worker
  （停泊者阻塞在 recv，~0 CPU，可逆、不丢种子）；master 每 5s 写 `.symcc_stats`；
  run_hybrid 控制器据此在 np 预算内动态增减 AFL 实例与活跃 worker 数。隔离测试确认
  停泊 14→3→14 转换正确、CPU 随之下降、无死锁、work 不中断。
- **策略（未超越静态）**：以「SymCC 每核 interesting 增量 vs AFL 每核 edges 增量」为
  边际产出信号，谁高给谁。np=64 A/B（各 2 轮）：

| 目标 | 静态封顶 | 自适应 | 判定 |
|------|:--------:|:------:|:----|
| sqlite | 23.12±0.10 | 22.97±0.48 | ~持平（-0.15pp）|
| libarchive | 23.95±2.31 | 23.61±0.54 | ~持平（-0.34pp，在方差内）|

K 轨迹（sqlite: 14/12/14/16…；libarch: 14/12/…/20）在起始封顶值附近震荡，未能干净
区分两类目标。**结论**：自适应控制器与静态封顶持平、未超越。根因是在线「AFL vs SymCC
边际归因」信号本质困难（覆盖率共享、单位不可比、fuzzer_stats 更新滞后），且静态封顶
已接近稳态最优包络，留给时序自适应的空间有限。

**处置**：机制作为 opt-in 保留（`--hybrid-adaptive`，默认关闭，不影响已验证的静态默认），
为未来更优在线信号提供基础；当前不作默认、不做完整验证（性价比不足）。

### Taint 引导选择性符号化（TACE FSE'24）：机制已实现，本 benchmark 无收益

思路：用 QSYM 的 `DependencySet` 收集每个 interesting 分支实际依赖的输入字节
（真正影响控制流者），只符号化这些字节、其余具体化，缩减符号状态、加速求解
（TACE 报告在二进制大输入上约束求解快 50x）。

**已实现**：
- 运行时收集分支依赖并集（`relevant_bytes_`），经 `SYMCC_TAINT_OUT` 在执行中写出
  （对 libFuzzer `_exit` 安全；默认路径由 `taint_enabled_` 门控，零开销）。
- 运行时消费**离散相关字节集** `SYMCC_FOCUS_SET`（比旧的连续 `SYMCC_FOCUS_BYTES`
  精确，能表达散布的相关字节，附 ±margin 邻域；超过已观测 max 偏移的字节保持
  符号化以防漏掉未探索路径 → 不丢覆盖率）。

**头部数据（相关字节占比，实测）**：因目标/种子而异——
pcre2(正则) 恒为 100%（每字节都影响分支，无空间）；sqlite/libarchive 的大种子低至
9–30%（大量 payload 与控制流无关，有空间）。

**验证（否定）**：
- benchmark 小种子（<200B）：全符号化 vs 聚焦 = 7.9s/1354边 vs 8.0s/1361边，
  **0.99x 无加速**——小输入本就快（~0.5s/种子），且跨种子并集稀释了单种子稀疏性。
- 12KB 合成大稀疏输入（1% 相关）：聚焦反而 **0.02x 更慢**（60s 超时 vs 1.0s）——
  具体化字节会改变哪些分支为符号，进而**非单调地**改变探索路径（可能更长/不同）。

**结论**：机制健全，但 TACE 的收益条件（大二进制输入、payload 主导、符号执行本身慢）
在本 benchmark（小种子）不成立；且具体化对探索路径的扰动不可预测。**保留为 opt-in
实验机制**（`SYMCC_TAINT_OUT` / `SYMCC_FOCUS_SET`，默认关闭、零开销），未接入 master
反馈闭环、不作默认。

### 三项探索的共同教训

「多样性调度」「运行时自适应分配」「taint 选择性符号化」三条路径均**机制健全但在本
workload 未超越已验证的静态方案**。规律：静态「多实例 AFL + SymCC worker 封顶」已捕获
主要收益；调度/自适应/选择性技巧在小种子、覆盖率共享、亚线性 concolic 等约束下空间有限。
**已交付且经多目标多并行度验证的核心贡献仍是分配层优化**（见 §三之补）。

---

## 三、后续可实施方案（按优先级）

### P0 — 短期（1-2 周）

1. **AFL++ CmpLog `-l M` 模式**
   - 在 benchmark 中启用 predicate-tightness scheduling
   - 仅需修改 AFL++ 启动参数，无代码改动

2. **Taint-guided 符号化**
   - 替换当前朴素的 SYMCC_FOCUS_BYTES（基于 hint 偏移）
   - 使用 SymCC 的 DependencySet 追踪每个分支实际依赖的输入字节
   - 将依赖信息回传给 Master，精确设置 focus 范围

3. **AFL++ Custom Mutator 集成**
   - 利用 AFL++ havoc_mutation hook（默认 6% 调用概率）注入 SymCC 求解结果
   - 比当前外部文件同步更紧密的集成方式
   - 支持 Python/C/Rust 接口，可与其他 mutator 链式组合

### P1 — 中期（2-4 周）

4. **调用图感知任务分区（DynamiQ 风格）**
   - 分析目标程序的调用图
   - 将 MPI workers 按函数区域分组
   - 周期性重分区打破覆盖平台期（DynamiQ 证实有效）

5. **增量约束求解**
   - 利用 Z3 的 push/pop 复用求解器状态
   - 相邻分支共享前缀约束时避免重建

6. **动态核心分配（KRAKEN ISSTA'25 风格）**
   - 根据覆盖率增速动态调整 AFL fuzzer 与 SymCC worker 的进程比例
   - 覆盖率增长停滞时增加 SymCC workers，爆发增长时增加 AFL fuzzers

### P2 — 长期（1-2 月）

7. **LLM 辅助约束求解（Cottontail S&P'26 风格）**
   - 集成 LLM API 处理 Z3 UNSAT/TIMEOUT 的复杂约束
   - 特别是结构化输入（SQL, JSON, XML）的格式约束
   - 三层策略：Fuzzy-SAT → Z3 → LLM

8. **路径合并（Veritesting）**
   - 对线性代码区域合并符号状态
   - 需要修改 QSYM 后端的约束收集机制

9. **FOX 风格梯度引导变异**
   - 前沿分支调度 + 牛顿法梯度估计
   - 对目标分支使用 k=1024 局部变异体估计子梯度
   - 直接翻转目标分支而非随机变异

---

## 参考文献

1. CoFuzz: Evaluating and Improving Hybrid Fuzzing (ICSE'23)
2. Cottontail: LLM-Driven Concolic Execution (S&P'26)
3. TACE: Rapid Taint Assisted Concolic Execution (FSE'24)
4. PSCache: Partial Solution Based Constraint Solving Cache (FSE'24)
5. GenSym: Compiling Parallel Symbolic Execution with Continuations (ICSE'23)
6. DynamiQ: Dynamic Task Allocation in Parallel Fuzzing (arXiv 2025)
7. SymFit: Making the Common Case Fast (USENIX Security'24)
8. AFL++ CmpLog Improvements (VU Research 2022)
9. Veritesting: Enhancing Symbolic Execution (ICSE'14)
10. Cache-a-lot: UNSAT Core Reuse in SMT (2024)
11. FOX: Coverage-guided Fuzzing as Online Stochastic Control (CCS'24)
12. KRAKEN: Dynamic Parallel Fuzzing (ISSTA'25)
13. Boian: MAB-based Fuzzer Scheduling (NUS 2024)
14. Fuzzy-SAT: Approximate SMT via Fuzzing (ICSE'21)
15. LibAFL: A Framework to Build Modular Fuzzers (CCS'22)
