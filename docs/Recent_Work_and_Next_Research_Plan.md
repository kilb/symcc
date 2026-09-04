# 并行符号执行框架近期工作与下一步科研计划

## 摘要

本项目围绕“利用并行符号执行提升 fuzzing 覆盖率”这一目标，对 SymCC/QSYM
式编译型 concolic execution 进行了系统性重构。近期工作不再把符号执行视为
单次路径翻转器，而是把它提升为一个跨编译器、运行时、求解器、AFL 覆盖反馈、
MPI 分布式调度和 agentic 路由的混合执行框架。

核心思想是：将符号执行中的路径、约束、输入结构、目标分支、worker 状态和
覆盖收益统一建模为可学习、可恢复、可分片的执行任务。这样可以减少并行
concolic execution 的重复求解，提升对结构化输入和隐式信息流的覆盖能力，并
为后续科研实验提供可消融、可复现的工程基础。

本阶段已完成从“并行运行多个 SymCC 实例”到“上下文感知的并行混合符号执行
系统”的关键跃迁。下一步工作将集中在三个研究问题上：

1. 如何让 concolic execution 在大规模并行下保持接近线性的有效覆盖收益。
2. 如何把路径树、结构化输入、solver-hostile 约束和 LLM/agentic 推理结合起来。
3. 如何用严格实验、消融和统计检验验证这些优化确实优于现有 SOTA 组合。

## 一、研究定位

项目定位是开发通用并行符号执行框架，并以 fuzzing 覆盖率提升作为主要评价指标。
相关论文常发表于 Security、Software Engineering、Systems 等会议，但本项目的
核心任务是测试生成、路径探索和覆盖优化，不涉及攻击链、利用生成或漏洞武器化。

当前系统的研究对象包括：

- 编译型 concolic instrumentation 的表达能力和低开销传播；
- SMT、polyhedral abstraction、近似求解和反向路径适配的组合；
- AFL bitmap、data coverage、comparison taint 和路径 DAG 的统一反馈；
- MPI 多 worker 下的 seed-worker-state 联合调度；
- 面向结构化输入的 agentic/LLM 路由和验证式回灌。

## 二、近期已完成工作

### 1. Backsolver + Veritesting：从常量 PHI 到 bounded easy-region 合并

近期对 compiler instrumentation 做了关键扩展。此前 Backsolver 只依赖 LLVM
`select` 和少量常量 PHI 生成 ITE 表达式，覆盖范围有限。现在编译器可以识别
canonical two-arm diamond，并将 PHI incoming 值恢复为 ITE：

- incoming 值可以是常量；
- 可以是 merge 点可见的符号 SSA 值；
- 可以是分支内部的 bounded side-effect-free SSA 表达式；
- 支持整数/浮点/指针标量上的算术、比较、cast、select；
- 支持 bounded acyclic region 内部的两输入 PHI 递归折叠，形成 nested ITE；
- 对未经 MemorySSA/AA 证明的 load、store、call、division、remainder、loop 和
  非规范 CFG 保守回退；可证明无干扰的 bounded read-only load snapshot 已纳入。

这使 Backsolver 不只处理“常量分类标签”这类隐式流，也能处理分支内部计算后再
merge 的实际程序模式。运行时侧继续使用已有的 ITE controller 枚举、直接候选
验证和 Z3 fallback，保证输出仍经过 concrete replay/表达式求值验证。

相关测试：

- `test/backsolver_symbolic_phi.ll`
- `test/backsolver_veritesting_region.ll`
- `test/backsolver_nested_veritesting.ll`
- `test/backsolver_implicit_flow.ll`
- `test/backsolver_selective_prefix.ll`

科研意义：

- 这是 Backsolver 思想在编译型 concolic 框架中的轻量化实现；
- 它引入了 Veritesting 的静态合并精神，但避免全 CFG 静态符号执行的复杂度；
- 后续可以继续扩展到 loop template、multi-exit region 和 control-dependence IR。

### 2. S2F 式 dual executor 与 actionseed

系统已加入 S2F 风格 actionseed 机制。MPI coordinator 从 prefix DAG 中选择同一
seed 下的多个高价值 open branch，并生成动作文件：

- `solve`：强制精确求解该目标；
- `sample`：使用 sampling executor profile；
- `skip`：记录但跳过低价值分支。

QSYM runtime 读取 `SYMCC_S2F_ACTIONS`，在目标分支 prefix 达到时绕过普通 pruning。
这让符号执行不再只在当前执行路径上“看到一个分支、翻转一个分支”，而是可以由
coordinator 显式注入同一 seed 的多分支计划。

科研意义：

- 将求解策略从 runtime 局部启发式上移到全局 prefix DAG；
- 为 TACO-Fuzz 的 extended path conditions 和 ConcoLLMic 式 agentic route 提供执行载体；
- 允许对 exact/tailored/sampling executor 做细粒度消融。

### 3. TACO-Fuzz + MultiGo：目标中心路径选择与多路径难度建模

`PrefixDAG` 已加入 target-centric 和 multi-path directed hybrid fuzzing 逻辑：

- 维护 site frequency 与 Poisson-style path difficulty；
- 为 target path 记录 visits、reward、distance、under-exploration；
- 在 replay/actionseed 选择中融合 target distance、path difficulty、mutation rarity；
- 对高价值同 seed 分支批量生成 actionseed；
- 将 under-explored target path 作为 seed selection/generation 的核心信号。

科研意义：

- 传统 directed fuzzing 往往只看静态距离，容易反复挖同一条近但不可达路径；
- 当前实现把“路径难度”和“目标路径多样性”显式加入调度；
- 后续可以研究基于 causal bandit 的目标路径因果收益估计。

### 4. Pangolin/GenSlv 式上下文复用与 solution generator

运行时已经提供 prefix-sensitive polyhedral/Z3 context reuse，并在异步 Query IR
服务上新增 reusable solution generator：

- 约束上下文按 prefix key 缓存；
- 保存 SAT/UNSAT/model/linear context；
- 支持 polyhedral byte-box sampling、template pair 和 John-walk 风格采样；
- query solver 在 SAT 后计算 exact byte ranges、field hints 和 solver-verified
  extra models；
- query store 持久化 `symcc-solution-generator-v1`，并把所有 verified models 物化为
  content-addressed candidates；
- AFL hint mutator 读取 poly cache full-matrix 字段，按相关线性约束采样，不再把
  box 作为独立 byte 区间处理；
- 与 S2F sampling action、AFL hint mutator 和 coordinator telemetry 打通。

科研意义：

- 把符号执行从“一次 query 一个 model”推进到“一个可复用 path context 产生多个候选”；
- 减少多 worker 对相似 prefix 的重复 Z3 调用；
- 为 solver portfolio 学习提供可观测的 cache hit/sample yield。

### 5. AFL 原生 data coverage map 与 comparison taint

编译器/runtime/coordinator 已支持 AFL 原生 data coverage：

- 记录常量比较的 matched prefix bits；
- 记录 comparison taint locality；
- 通过 AFL bitmap feature 和 coordinator dominance policy 进行筛选；
- 生成 `.compact_focus_set`，作为 Gordian-style compact input linearization 的输入。

科研意义：

- code coverage 不足以描述“距离解析成功还差多少字节”；
- data coverage 能提供连续信号，帮助 fuzzer 和 concolic executor 选择更有潜力的 seed；
- comparison taint 是 Cottontail 式结构化路径表示的低成本近似。

### 5.1 SymCC-str-inspired string constraint bridge and string-theory backend

G04 已新增 string/BV 候选桥，并完成受限的 string-theory candidate backend：

- `memcmp/strcmp/strncmp` wrapper 在一侧 concrete、另一侧为 direct input byte 时写出
  `symcc-string-constraint-v1`；
- artifact 记录 concrete token、NUL 终止和 exact input-offset patches；
- AFL hint mutator 增加 `symcc_string` stage，可直接把 seed patch 成目标字符串候选；
- `symcc-string-query-v1` 支持 `concat/substr` term，以及
  `equal/distinct/prefixof/suffixof/contains/length/indexof/char_at` predicate；
- `symcc-query-solver --generic` 可解析 SMT-LIB String 公式并把 Z3 String model 映射
  回输入 offset；
- MPI helper 默认捕获隐藏 string artifact，生成 `string-solver-*` 候选，再走原有
  showmap/worker coverage/master triage；
- `util/symcc_string_solver.py` 支持离线 benchmark 复现同一 materialization 语义；
- 候选仍经过 string-query verifier、concrete replay 和 AFL novelty triage，不替代
  BV solver。

这仍不是完整 SymCC-str。剩余研究任务是 smt-switch 级 BV/String 同步视图、
cvc5/Princess/Z3str3 portfolio 对拍、symbolic length 的 runtime query 生成、
`strlen/strchr/strstr` model、persistent string-server 和与 G08/G11 structured
completion 的统一语义层。

### 5.2 Query-service solver portfolio

G06 已在异步 Query IR 服务中落地，并完成 bounded parallel racing：

- `PortfolioSolver` 可对同一 query lease 运行多个 solver helper；
- result 记录 `symcc-solver-portfolio-v1` attempt telemetry、winner 和 disagreement；
- SAT/UNSAT 冲突保守降级为 unknown，不进入确定模型或 UNSAT cache；
- `SYMCC_QUERY_SOLVER_PORTFOLIO`/`--portfolio` 支持 persistent 与 one-shot helper。
- `SYMCC_QUERY_SOLVER_PORTFOLIO_PARALLELISM`/`--portfolio-parallelism` 控制同一 query
  内的并发 solver attempts；结果记录 `mode/parallelism/elapsed_us`。

F237/F238已补充backend-neutral capability/lowering及prefix-keyed persistent
context；F239已补充真实X-means/BIC、删失PAR-2和proof-carrying offline prior。
F240已补充bagged/boosted cost surrogate与budgeted sequence beam search。
F241已补充严格预算的schedule-level Expected Improvement acquisition及可重放
trace；F242已补充SAT-gated running-attempt cancellation。Bitwuzla实机与跨worker
solver state仍未实现。

### 6. UCSan 式 under-constrained execution

系统已实现 opt-in 的 compilation-based under-constrained execution：

- 支持函数入口 harness 生成；
- structured seed import/export；
- pseudo/shadow pointer metadata；
- lazy JIT object provisioning；
- pointer load/store/memcpy shadow propagation；
- external policy 与 wrapper 配置。

科研意义：

- 允许符号执行从完整程序 fuzzing 进入函数级、对象图级测试；
- 可以弥补 fuzzing 对深层库函数入口到达困难的问题；
- 后续可与 LLM/object-seed synthesis 结合，生成结构化对象初始状态。

### 7. Seed-replay state 分片（非 GenSym continuation）

新增 `StateShardCoordinator`，把一个 work item 抽象为可分片的 replay task：

```text
state = input_digest/path + focus_slice + target_branch + s2f_actionseed
```

该 state 被 hash 到 `SYMCC_STATE_TASK_SHARDS`，并映射到 owner worker。MPI master
派发时优先选择 worker 本地 shard，必要时允许 bounded work stealing。状态任务的
lease、completion、reward 和 failure 会持久化到 `.state_tasks.json`。

科研意义：

- 复用了 GenSym 所强调的 ownership、locality 和 stealing 控制面思想；
- 当前 task 不包含 PC、symbolic store、memory、path condition 或 solver stack，
  因此只能称为 seed-replay state sharding，不能称为 continuation-level execution；
- 论文语义的 live continuation engine 仍是后续独立工作，严格差距和实施顺序见
  [`SOTA_Gap_Audit_2026-07-24.md`](SOTA_Gap_Audit_2026-07-24.md)。

### 8. Cottontail / ConcoLLMic / Gordian 式 agentic route

`agentic_concolic_hooks.py` 已从简单 hint 加载扩展为 route-aware planner：

- `cottontail` route：从 comparison taint 学习结构局部 focus slice；
- `concollmic` route：把 target branch 映射为 actionseed solve；
- `gordian` route：把 solver-hostile 的 timeout/unknown/no-output 任务送入 sampling route；
- `hybrid` route：三者同时启用；
- 外部 hints 可覆盖 `focus_bytes`、`focus_set`、`target_branch`、`strategy`、`s2f_actions`、`route`。

默认内置 planner 是确定性的，不主动调用模型；外部 LLM/agent 可以通过
`SYMCC_AGENTIC_OUT`、`SYMCC_AGENTIC_HINTS`、`SYMCC_AGENTIC_CMD` 参与调度。

科研意义：

- 避免把 LLM 放进不可控的 trusted execution loop；
- 让 LLM 作为“可验证候选生成器/调度建议器”，输出仍由 concrete replay 与 coverage triage 验证；
- 为比较 Cottontail、ConcoLLMic、Gordian 风格方法提供同一工程接口。

### 9. Engine-neutral Expressive Coverage Tree

新增 `util/expressive_coverage.py`，将 compiler structural summary 与 runtime
telemetry 汇合成受限大小的 expressive coverage tree：

- 节点区分 observed outcome 与尚未覆盖的 open outcome；
- 保留 prefix branch id、parent、site、source location、function context、
  opcode/switch 类型、depth 和 seed provenance；
- 汇总 comparison dependency、data quality、SAT/UNSAT/timeout、solver cost、
  generated/retained quality；
- constraint-shape key 移除绝对输入 offset，用 dependency count/span 识别
  “结构等价但位于不同输入位置”的解析约束；
- 对重复 site/outcome/context 进行 loop/repeated-parser 压缩；
- ECT priority 已接入 target replay 与普通 replay 的排序；
- scheduler state 可恢复，`SYMCC_ECT_OUT` 可原子导出 versioned JSON。

这比 flat `branch_trace` 更接近 Cottontail 的 expressive coverage 表示，同时保持
backend-neutral：树中不保存 Z3 AST，因此 SymCC、未来 SymSan 或其他执行引擎只需
产生相同 telemetry schema。

### 10. 多 coordinator coverage-owner shard 与 gossip

新增 `CoverageOwnerShardGossip`，把最终 AFL hit-count bitmap novelty 从单 master
内存状态提升为共享的 authoritative shard：

- bitmap index 确定性映射到 owner shard；
- candidate bits 在短 shard lock 内执行原子 OR 与 novelty 判定；
- 同一 hit-count bucket 在并发 coordinator 间最多被认领一次；
- shard epoch 单调递增，其他 coordinator 周期性 pull 并写入本地 worker delta journal；
- coordinator heartbeat 决定 preferred owner；owner 超时后可 fail over；
- OR 操作满足交换/结合/幂等性，因此任一 coordinator 都可在锁内 proxy commit。

该设计已经解决共享文件系统部署中的最终 coverage 去重问题。尚未覆盖的工程边界
是跨非共享文件系统的 quorum/CAS transport；这需要独立的一致性服务或对象存储
条件写，而不能仅依赖当前 atomic mkdir/rename。

### 11. Verified semantic proposal channel

新增 `util/verified_proposals.py` 与 MPI worker/master 全链路。外部 inverse、
surrogate、heap-partition 或 solve-completion planner 只能提交 bounded data
transformation：

```json
{"kind":"inverse","source_path":"/tmp/seed","target_branch":123,
 "patches":[{"offset":4,"hex":"504b0304"}]}
```

系统不执行 proposal 中的代码。候选被 content-addressed materialization 后，必须
由真实 target concrete execution 验证；若声明 `target_branch`，telemetry 必须同时
满足 branch id 一致与 `target_reached=true`。验证通过后仍需经过 authoritative AFL
bitmap triage，只有产生全局新 coverage bit 才标记 retained。该 acceptance funnel
把 Gordian 式 semantic proposal 与符号执行正确性、全局覆盖收益解耦，可分别测量
proposal validity、target reachability 和 coverage utility。

### 12. Nested bounded Veritesting

compiler easy-region synthesizer 现在可递归处理区域内部的两输入 PHI。对每个内部
PHI，编译器识别最近公共 dominator 上的 conditional branch，验证两臂是有界无环
region，重建 condition 与纯 SSA incoming value，并在外层 merge 点生成 nested ITE。
实现继续使用硬边界：

- expression recursion depth 最大 12；
- 每臂 CFG 最大 32 个 basic block；
- memory write、模糊 alias、call、terminator value、division/remainder、loop 和
  non-canonical merge 直接回退；MemorySSA/AA 可证明无干扰的 bounded read-only
  load snapshot 已在后续实现中纳入；
- 所有生成模型继续走 Backsolver evaluator 与 concrete corpus triage。

这一步实现了“multi-block + chained PHI/ITE”的主路径，后续又完成了
MemorySSA/AA 证明的 read-only memory snapshot。它仍不是完整 IFSS：
multi-exit region、loop summary 和 LLVM poison/freeze 语义仍是后续 verifier 工作。

## 三、当前创新点

### 创新点 1：跨层统一的路径上下文反馈

系统把路径 prefix、solver telemetry、AFL bitmap、data coverage、comparison taint、
target branch、worker 状态和 actionseed 统一进 coordinator 的决策模型。这区别于
传统 hybrid fuzzing 中“fuzzer 管 corpus，symbolic executor 管约束”的松耦合结构。

### 创新点 2：Backsolver 与 Veritesting 的编译型轻量融合

不是实现一个完整静态 symbolic executor，而是在 SymCC instrumentation 阶段恢复
控制依赖值为 ITE，并交给运行时 Backsolver 验证。这是一条工程复杂度较低、适合
现有编译型 concolic 框架的路径。

### 创新点 3：continuation state 分片，而不是 seed 文件分片

并行符号执行的重复主要来自“不同 seed 触发相似 symbolic state”。因此调度单位
从文件提升为：

```text
input + focus + target + action plan
```

这比普通 seed queue 更接近符号执行真实的任务边界，也更适合 work stealing 和恢复。

### 创新点 4：LLM route 与 SMT/replay 的可验证组合

系统不让 LLM 直接决定结果正确性。LLM 或内置 agent 只能改变 route、focus、strategy
和 actionseed，最终候选必须由 target execution、telemetry 和 AFL coverage triage
验证。这为 LLM-assisted symbolic execution 提供了可复现、可消融的实验边界。

## 四、主要技术挑战

### 挑战 1：并行 concolic execution 的结构性冗余

多 worker 并发时，即便输入不同，也可能反复翻转相同下游 branch，产生高度重复的
候选。简单增加 worker 数通常会导致有效覆盖收益快速饱和。

当前缓解手段：

- seed-worker contextual bandit；
- state shard owner-local dispatch；
- prefix DAG replay cooldown；
- bitmap/data feature dominance；
- S2F actionseed 让同一 seed 的多个分支计划化。

后续挑战是证明这些机制在真实 benchmark 上能提高 CPU-hour normalized coverage。

### 挑战 2：静态 region merging 的 soundness 与覆盖范围

当前 Veritesting easy-region 只复制 side-effect-free SSA 和经 MemorySSA/AA 证明的
bounded read-only snapshot。扩展到复杂 CFG、loop、模糊 memory read 和 external
call 会带来 dominance、UB、alias 和路径条件组合问题。

后续需要：

- 明确 region admissibility 判定；
- 建立 instrumentation soundness 约束；
- 引入 loop template 或 bounded unrolling；
- 对每类 IR pattern 增加 lit + differential replay 测试。

### 挑战 3：LLM/agentic route 的可控性

LLM 可以产生结构化输入，但也可能生成无效、不可复现或污染实验的数据。

当前约束：

- 默认 deterministic planner；
- 外部 hint 只影响调度，不直接接受输出；
- 所有候选仍走 concrete execution 和 coverage triage；
- route policy 可用 `SYMCC_AGENTIC_ROUTE` 消融。

后续需要在实验中区分“LLM 语义收益”和“额外 mutation budget 收益”。

### 挑战 4：评价指标的学术严格性

仅报告 generated count 或单次 coverage 曲线不够。需要 equal CPU budget、重复实验、
置信区间、消融和统计检验。

建议采用：

- edge coverage / branch coverage / data feature coverage；
- time-to-first-target；
- unique useful testcases per CPU-hour；
- solver time、Z3 timeout、cache hit、backsolver sat；
- worker redundancy、state shard steal rate；
- Mann-Whitney U 或 bootstrap confidence interval。

## 四点五、本轮 P1/P2 落地更新

本轮继续把前一版计划中的 P1/P2 主路径推进到可运行、可测试的系统机制：

1. **bounded-DPOR 并发调度探索**：新增 `util/symcc_schedule_rt.c` 作为
   `LD_PRELOAD` pthread 同步 trace/replay runtime，记录 mutex/rwlock/cond/join
   scheduling point；新增 `util/schedule_exploration.py`，从 trace 中为同一同步对象上的
   dependent pair 生成 logical-thread-id replay prefix，并在 MPI master 中以高优先级工作项
   重新派发。
2. **多 master fenced lease**：新增 `FencedWorkLeaseTable`，使用短 update lock 与 fencing
   token 保护共享 work id。master 派发前必须 claim，worker 完成时必须携带匹配 token；
   过期 lease 可被其他 coordinator 偷取，旧 master 的 stale completion 会被拒绝。
3. **语义约束 fallback routing**：新增 `SemanticFallbackPlanner`，将 telemetry 的
   comparison taint、data features、timeout、生成收益抽象为 byte-local、token-progress、
   wide/nonlinear、timeout-prone、opaque 等类别，并转化为 S2F actionseed、strategy 和
   focus hint，让 executor portfolio 在 exact/tailored/sampling/skip 间做更可解释的 fallback。
4. **P2.1 零执行依赖推断已经接入 replay focus**：compiler 可在构建时输出静态输入依赖摘要，
   MPI master 能把 runtime open branch 映射到静态 byte interval，并为 target replay 生成
   sparse focus set。

这些机制均有独立单元测试，并已进入默认 lit 测试集；其中 DPOR 和 multi-master fenced lease
默认关闭，semantic fallback 随 adaptive scheduler 默认开启。

## 四点六、本轮继续完成的 P1/P2 SOTA 主路径

在前述基础上，本轮把“论文计划中的机制”进一步推进为可测试的 benchmark 系统：

1. **MemorySSA/AA 只读快照复用**：Backsolver/Veritesting 不再一概拒绝含内存读的
   easy region。编译 pass 在 MemorySSA/AA 能证明无写入干扰、无逃逸 alias 且公式规模受控时，
   把局部只读 load snapshot 纳入 ITE/region 合成；存在不明确 memory side effect 时仍保守拒绝。
2. **UCSan 图学习与上下文规范化**：`util/ucsan_seed.py` 新增对象图 canonicalization、
   alias-class learning、cycle rejection、稳定 path serialization 和 normalize/learn CLI，
   为 under-constrained function-entry fuzzing 提供可复用结构种子。
3. **统一 backend telemetry**：SymCC/SymSan worker 结果被归并到同一
   `SolverTelemetry` schema，包含 engine、solver algorithm、capabilities、partial report、
   signed return code、timeout/killed、comparison taint、data feature 和 generated count。
   这让 adaptive scheduler、semantic planner、proposal verifier 与 benchmark 报告共享同一观测口径。
4. **SMTgazer 式算法序列调度**：新增 `util/smt_algorithm_scheduler.py`，用 online
   context clustering + Bayesian/Thompson selection 学习 `exact`、`fast-exact`、
   `optimistic-layered`、`polyhedral-exact` 等 solver sequence；每个 assignment 记录
   action propensity，并以 timeout-censored PAR-2 cost 更新 posterior。
5. **Gordian/NeuroSCA 风格语义 proposal 生成器**：新增 `util/semantic_proposals.py`，
   从 comparison-taint constraint core 中生成 inverse candidates，从 data coverage/token
   exemplar 中做 surrogate transfer，并对 UCSan heap/alias partition 做 split/merge mutation。
   所有候选都进入 `VerifiedProposalManager` 的真实目标 replay 和 AFL bitmap triage，
   因此 proposal 后端不进入 trusted path。
6. **离线轨迹、保守评估与并行干扰建模**：新增 `util/offline_policy.py`，记录
   JSONL trajectory，按 generated/interesting/concurrent workers 估计冗余干扰，并用
   SNIPS + doubly robust + effective sample size + lower confidence bound 进行保守策略门控。
   `benchmark/run_benchmark.py` 现在把 `.offline_trajectory.jsonl`、`.offline_policy.json`
   和 `.smt_algorithm_state.json` 写入 `benchmark_data.*`，可直接复现实验分析。

这一轮之后，P1/P2 的主要工程机制已经落地。剩余挑战集中在科研验证和更强模型：
equal-CPU 多轮公开 benchmark、Memory/LLVM poison/freeze 的更完整语义、multi-exit/loop
region synthesis、UCSan pseudo/shadow pointer grammar、非共享文件系统 CAS/consensus、以及带
校准误差界的 neural arithmetic proposal backend。

## 五、下一步科研计划

### P0：实验严谨化与论文级评估

目标：把当前工程成果转化为可投稿/可复现实验结果。

任务：

1. 建立 benchmark matrix：
   - text parser：json、xml、pcre2、sqlite；
   - binary parser：libpng、libjpeg、woff/font；
   - compression/archive：zlib、libarchive；
   - coreutils-style utility programs；
   - synthetic microbenchmarks for implicit flow、state sharding、data coverage。
2. 设计消融组：
   - baseline SymCC + AFL；
   - + telemetry scheduler；
   - + Pangolin poly cache；
   - + Backsolver/Veritesting；
   - + S2F actionseed；
   - + TACO/MultiGo；
   - + state shard coordinator；
   - + agentic route。
3. 固定 equal CPU budget：
   - 1、4、8、16、32 workers；
   - 30min、2h、6h 三档；
   - 每组至少 20 次随机重复。
4. 输出统计报告：
   - coverage curve；
   - AUC；
   - final coverage；
   - confidence interval；
   - solver cost breakdown；
   - worker redundancy heatmap。

预期论文贡献：

- 一个完整的 parallel hybrid symbolic execution 框架；
- 一组可复现的跨层消融结果；
- 对并行 concolic 冗余瓶颈的实证分析。

### P0 已完成主路径：ESCT/ECT 路径结构表示

当前状态：engine-neutral ECT、结构元数据 join、节点质量/solver 汇总、重复
constraint-shape 识别、loop compression、bounded pruning、scheduler priority、state
recovery 和 JSON export 已实现。

剩余研究任务不再是“有无 ECT”，而是表示质量评估：

1. 增加精确 call/return context，而不只使用 bounded function transition context；
2. 对结构等价约束的归一化加入表达式 operator skeleton；
3. 测量 ECT priority 对 structured parser coverage 的独立贡献；
4. 与 flat PrefixDAG、comparison-taint-only 做等预算消融；
5. 设计 backend-neutral schema compatibility test。

### P0 已完成主路径：持久化 Query IR 与异步增量求解

当前状态：`symcc-query-ir-v1`、表达式内容寻址、SQLite prefix trie、fenced work
lease、持久 Z3 helper、按前缀分片、target `push/pop`、exact result cache 和
UNSAT clause-subset pruning 已实现。runtime 可在 defer 模式只记录 query，独立服务
在 tracer 退出后生成候选并回到 MPI/AFL replay。

验证证据：5 个 QueryStore 单元测试、公共前缀 cache-hit lit 测试和完整 runtime
导出/退出后求解 lit 测试均通过。该项为 I/T，不代表已经证明覆盖率收益。

G01 仍需补公开 benchmark 的 query redundancy、cache hit、solver CPU/RSS、
valid models 和 coverage/CPU-hour 消融。

### P0 已完成主路径：Static Data Coverage 与独立 novelty

当前状态：LLVM static-load provenance、constant-global registry、已加载 ELF
read-only segment 后备表、simple/load-based/region/predicate/switch access abstraction、
ASLR 稳定 `(object,offset,width)`、位级有效长度和 switch 邻接探测已进入真实
compiler/runtime。协调器在独立持久状态中维护无碰撞 winner，并只对相同 path
fingerprint 的严格进度提升记录 code/data refinement。

验证证据：40 个 hybrid-feedback unittest、4 个 data/telemetry lit 测试和 2 个 AFL
preload unittest 通过；其中包含未插桩 DSO lookup table、static graph、6/8 equal-bit
语义、四 case switch 的 2-probe 上界和跨 ASLR 重跑。该项为 I/T，不代表已经证明
真实 benchmark 覆盖收益。

G02/G18 已完成主路径并复用 G01 的 content-addressed query/result store。G04 已完成
offset-aware string candidate 与受限 Z3 String backend 的主路径。G06 已完成
query-service portfolio、disagreement telemetry 和 bounded parallel racing。G07 已完成
QueryStore 层 partial-solution cache 与 persistent solver-helper recent-SAT assignment
probe：SAT byte assignments 可在后续 query 上经 Query IR 验证后生成
`async-partial-*` candidate，也可在 helper 内部短超时 probe 当前 solver context 并在
Z3 返回 SAT 后用 `z3-pscache` 短路。F236进一步完成proof-carrying
assumption-conflict extraction、core最小化与signed prefix/off-path索引；下一阶段
以同CPU消融判断是否值得维护自建Z3的逐CDCL-trail采样后端。G06 的后续重点转为 backend-neutral
cvc5/Bitwuzla lowering、capability matrix 与 offline/online policy 回灌；其中F237
已完成QF_BV Query IR lowering、显式能力合同和cvc5实机模型双验证，下一步收窄为
Bitwuzla实机conformance、incremental context协议及策略回灌；G04 的后续
重点转为 runtime string-query 扩展与多 string solver 对拍。G08/G11 已新增
token-grammar `solve_complete` proposal baseline，后续重点是完整 online CFG/rule
coverage、query/string/ECT metadata 统一序列化和 history-guided structured seed
acquisition。G09 已新增 IFSS/Hydra-style data-only targeted transformation
proposal baseline：comparison-taint core 被压缩为 relevance slices，并生成
`targeted_transform` 的 core copy/swap、boundary perturbation 和 bounded truncate
candidate；所有结果仍经原程序 replay、target telemetry 与 AFL novelty 验证。后续
G09 的真正缺口是 compiler/runtime 级 region state、transformed exploration 和
spurious-result taxonomy。G05 已新增 `symcc-live-continuation-v1` checkpoint
descriptor/control-plane baseline：future engine 可把 PC frames、path-condition root、
symbolic store root、symbolic memory root 和 parent checkpoint 纳入 state-task id、
shard owner 与 lease/recovery；当前仍不暂停真实进程或迁移 live symbolic memory。
G10/G13 已新增 ConDPOR-style trace-level HB/lockset baseline、runtime memory
evidence、schedule-constraint artifact、Query IR joint replay validation，以及 memory
provenance filtering/tagging：
`SYMCC_DPOR_MEMORY=1` 可在编译期插入 load/store read/write 通知，
`SYMCC_SCHEDULE_MEMORY=1` 可在 preload runtime 中输出 byte-granular memory trace，
`SYMCC_SCHEDULE_MEMORY_FILTER=stack|owner|all` 可跳过当前 thread stack rows，并用
bounded owner table 压缩首次跨线程共享 transition，
`SYMCC_SCHEDULE_MEMORY_PROVENANCE=1` 可追加 `prov=*` trace tag，
`SYMCC_SCHEDULE_CONSTRAINT_OUT=PATH` 可由 MPI master 追加
`symcc-schedule-constraint-v1` JSONL，`SYMCC_SCHEDULE_SMT_OUT=PATH` 可追加
`symcc-schedule-smt-v7` bounded SC event-position/lifecycle-state SMT-LIB2 query，
`SYMCC_SCHEDULE_QUERY_VALIDATION_OUT=PATH`
可由 query service 追加 `symcc-joint-schedule-query-v1` JSONL。schedule explorer
对这些 `read/write` rows 做 SC vector-clock annotation、lockset pruning 和
source-style replay prefix generation；QueryStore 再把 replay prefix、target branch、
trace digest、bounded conflict summary 与 Query IR prefix/target roots、solver result
和 bounded evaluator status 固化为可复现实验记录。F45 进一步把 event-position
permutation、per-thread PO、replay prefix 与 conflict reversal 编成可由 libz3
直接求解的 QF_LIA；公共约束通过 base/delta 与 push/pop 增量脚本复用 solver
context，prefix slot 精确绑定 next per-thread event；观测 HB 只作为 assumption，
避免把动态同步边错误固化。F46 又恢复完整 mutex/rwlock critical sections，用
`sync_ord_*` lifecycle permutation 连接 lock attempt、outcome 与 release；不兼容
区间必须不重叠，rdlock readers 可重叠，trylock busy 只作为 optional assumption。
F47 继续把 `pthread_cond_wait/timedwait` 拆成 condition wait、关联 mutex release、
mutex reacquire 和 wake/timeout；release 关闭 wait 前 section，reacquire 打开新
section。成功 wake 可选择性断言同 condition 的 signal/broadcast witness 必须位于
release/reacquire 之间；plain signal 是一对一资源，broadcast 可复用，默认不断言
以保留伪唤醒。F48 又以 logical child id 统一 create/start/exit/join identity，用 cleanup handler
记录普通 return、`pthread_exit` 和 cancellation 的 exit frontier；v4 强制
create-before-start 与 exit-before-join-success，同时允许 child 在 create return
前启动、join 在 exit 前阻塞。create/join failure、unmapped 或截断证据保持
conservative。F49 随后审计 lifecycle constraint language，将内部 `sync_ord_*`
默认从 bounds + all-different 全排列改为严格部分序 rank；任一满足模型可通过
拓扑排序得到保持已编码原子的 SC 线性扩展。replay-visible `pos_*` 仍保留精确
permutation，受控事件的双位置连接改成严格二方向析取，避免相等 rank 逃逸。
`SYMCC_SCHEDULE_SMT_ORDER_ENCODING=permutation` 保留旧式编码用于差分验证和消融。
64-thread、384-lifecycle-event 单次压力样例中，partial 与 permutation 均为 SAT，
base SMT 从 590,652 降到 554,560 bytes，系统 libz3 求解从约 8178.5 ms 降到
173.9 ms；这是 B 级工程测量，不是公开 benchmark 的性能结论。
F50 又以 scaled-anchor
`sync_ord_i=(L+1)*pos_j` 取代 partial 模式的 controlled pairwise links，将 C 个
受控 lifecycle events 的实际连接数从 O(C²) 降为 O(C)，同时用 L 个整数间隙保证
非受控 outcome/release/start/exit 可嵌入。F51 将程序序/thread 固定边、section
direction choices 与 anchors 导出为 `symcc-lifecycle-order-ir-v1`，并提供构造式
topological extension 与独立 certificate checker；checker 重新验证 source ranks、
query slots/conflict reversal、所有选择边、总序保持、投影和 digest。F52 再把通过
checker 的 controlled total order 投影成 logical-thread prefix，并提供
`symcc_schedule_linearize.py` CLI 与真实 pthread replay 测试；含 memory slot 或
conflict endpoint 的 query 会标为不可由 pthread prefix 直接 replay 并拒绝物化。
相同 64-thread、384-lifecycle-event v6 压力样例中，partial 用 64 anchors 表达
2016 semantic pairs；base SMT 为 255,663 bytes、单次 SAT 约 66.4 ms，legacy
permutation 为 587,016 bytes、约 3609.9 ms，二者证书均通过。该结果仍为 B 级。
F53 将 complete-section exclusions 拆成 v7 exact/relaxed 双基线；内置 solver
检查每轮 model 的结构化 direction choices，只用 libz3 C API 构造并断言真实违例
AST。64 sections/2016 choices 的普通 SAT 五次本地测量中，lazy 中位数
124.696 ms，eager 366.579 ms；全局强制重叠 UNSAT 会激活全部 choices，lazy
182.359 ms、eager 140.513 ms，证明收益依赖违例稀疏性。F54 再用
`Z3_model_eval(..., model_completion=true)` 直接提取 positions/ranks，并由
`symcc_schedule_solve.py` 一次完成 digest validation、refinement、certificate
check 与 prefix 输出。F55 在 create 前预留 identity，补齐 detach/cancel
interposition、`join_cancelled` cleanup 和显式 `thread_retire`；真实测试中被取消
joiner 未消费 target，另一线程仍可 mapped join，4104 个 detached threads 全部
mapped 并回收。
完整 optimal DPOR、取消/异常 wait path、double join/detach-join undefined races、
read-from/object identity
与 Query IR 的联合 schedule-SMT proof、ASLR-stable global identity、以及 live-state search policy
仍是后续工作。
G03 仍需真实
flex/bison、DSL/parser 的等 CPU 多轮消融，
以及基于真实 edge feature set、比完整 path fingerprint 更宽松的安全 corpus
dominance。

### P0 已完成共享文件系统主路径：多 master coverage ownership

当前状态：fenced work lease、过期 stealing、stale completion fencing、authoritative
coverage shard、hit-count bit 原子 novelty、epoch gossip 和 heartbeat failover 已实现。

剩余研究/部署任务：

1. 将 `.state_tasks.json` 拆为 append-only shard logs 并实现 compaction fencing；
2. 为非共享文件系统提供网络 CAS/quorum adapter；
3. 对 coordinator crash、network partition 和 delayed gossip 做 fault injection；
4. 测量 owner locality、proxy commit 和 shard count 的可扩展性；
5. 形式化证明 OR-state convergence 与 corpus retention 的 at-most-once novelty。

### P1 已完成主路径：Backsolver/Veritesting 语义扩展

当前状态：bounded multi-block acyclic region 验证、post-dominator outer merge、
select/PHI 链式 ITE folding、nested two-input PHI 和 MemorySSA/AA 证明的只读 load
snapshot 已实现。

剩余研究任务：

1. 显式建模 LLVM poison/undef/freeze，避免克隆表达式扩大 UB；
2. 扩展到 multi-exit/multi-way region，同时限制 formula growth；
3. 加入 differential concrete replay，系统性验证原 IR 与 synthesized ITE；
4. 研究 loop template 或 bounded unrolling，而不是直接接受循环 region。

挑战：

- alias analysis 精度不足时必须保守；
- LLVM poison/undef/freeze 语义需要显式处理；
- 复杂 region 可能增加公式规模，反而降低求解效率。

### P1 已完成主路径：UCSan 对象图 seed synthesis

当前状态：结构化 seed replay、稳定序列化、canonical object path、alias-class
learning、cycle rejection、normalize/learn CLI 和 graph-derived proposal hint 已实现。

剩余研究任务：

1. 推进到更强的 pseudo/shadow pointer metadata；
2. 从大量 successful replay 中学习 minimal object grammar；
3. 将 agentic route 用于 object seed mutation，并保持 verifier 预算可核算；
4. 对函数入口覆盖和全程序 fuzzing 覆盖做联合评价。

挑战：

- pointer validity 与 symbolic pointer arithmetic 的平衡；
- 外部函数 stub 的 soundness；
- seed 空间爆炸与 object graph 最小化。

### P1 已完成工程接口：SymSan/taint backend 对照实验

当前状态：SymCC/SymSan 结果已经统一到 engine-neutral telemetry，并能在同一
MPI/coordinator 管线下被 adaptive scheduler、semantic fallback 和 benchmark 消费。

剩余研究任务：

1. 在公开 benchmark 上系统对比 runtime overhead、coverage、solver query quality；
2. 扩展 backend-neutral conformance tests，覆盖 partial telemetry、timeout、killed、
   generated count 和 comparison-taint 等边界；
3. 将更多 focus_set 生成逻辑提升到完全 backend-neutral 的 schema compatibility 层。

挑战：

- backend 语义差异导致 telemetry 不完全等价；
- taint-only 路径可能生成更少 SMT 表达式，但也可能丢失精确求解机会。

### P2 已完成主路径：学习型调度从 bandit 推进到因果/离线评估

当前状态：SMTgazer-style sequence scheduler、trajectory recorder、interference-aware
reward、SNIPS/DR conservative evaluation、policy-shift gate 和 benchmark artifact export
已实现。

剩余研究任务：

1. 构建 counterfactual replay simulator；
2. 对 LinUCB、Thompson sampling、contextual MDP、offline RL 做 equal-budget 比较；
3. 在不同 target family 上测量 policy transfer 与 non-stationarity；
4. 将 worker redundancy penalty 与 coverage ownership/shard locality 的因果影响分离。

挑战：

- fuzzing 环境非平稳；
- 同一 action 的收益依赖 concurrent workers；
- offline policy 容易过拟合 benchmark。

### P2：面向论文的系统化理论抽象

目标：将工程系统抽象为一套可解释的模型，提升学术贡献清晰度。

可能抽象：

```text
Hybrid symbolic fuzzing = coverage-guided stochastic search
                         + constraint-guided state transition
                         + distributed continuation scheduling
                         + verified neural/agentic proposal.
```

需要形式化的问题：

- continuation state 的等价关系；
- prefix context reuse 的 soundness 边界；
- verified LLM proposal 的 acceptance criterion；
- data coverage 与 code coverage 的联合 dominance。

## 六、预期论文结构

一个可能的论文框架：

1. Introduction：并行 hybrid fuzzing 的冗余和结构化输入瓶颈；
2. Motivation：三个 motivating examples：
   - implicit flow；
   - solver-hostile structured parser；
   - multi-worker duplicate continuation；
3. Design：
   - compiler/runtime telemetry；
   - prefix DAG/actionseed；
   - state shard coordinator；
   - agentic route；
4. Implementation：SymCC/QSYM/AFL/MPI 细节；
5. Evaluation：
   - coverage；
   - scaling；
   - ablation；
   - overhead；
   - route quality；
6. Discussion：
   - soundness boundary；
   - LLM verification；
   - limitations；
7. Related Work：
   - QSYM/SymCC/SymSan；
   - Pangolin；
   - Backsolver；
   - Veritesting；
   - GenSym；
   - Cottontail/ConcoLLMic/Gordian；
   - directed hybrid fuzzing。

## 七、参考文献与技术来源

- Backsolver: Adapting Preceding Execution Paths to Solve Constraints, ACM TOSEM 2025. https://dl.acm.org/doi/10.1145/3712194
- Enhancing Symbolic Execution with Veritesting, ICSE 2014. https://dl.acm.org/doi/10.1145/2568225.2568293
- GenSym: Compiling Parallel Symbolic Execution with Continuations, ICSE 2023. https://dl.acm.org/doi/10.1109/ICSE48619.2023.00116
- Pangolin: Incremental Hybrid Fuzzing with Polyhedral Path Abstraction, IEEE S&P 2020. https://doi.org/10.1109/SP40000.2020.00063
- Cottontail: Large Language Model-Driven Concolic Execution for Highly Structured Test Input Generation, IEEE S&P 2026. https://mboehme.github.io/paper/SP26-cottontail.pdf
- ConcoLLMic: Agentic Concolic Execution, IEEE S&P 2026. https://github.com/ConcoLLMic/ConcoLLMic
- Gordian: LLM-assisted hybrid symbolic execution with ghost code, 2026 preprint. https://arxiv.org/html/2603.19239v1
- Data Coverage for Guided Fuzzing, USENIX Security 2024. https://www.usenix.org/conference/usenixsecurity24/presentation/wang-mingzhe
- MultiGo: Not All Paths Are Equal, ACM TOSEM 2025. https://dl.acm.org/doi/10.1145/3735555
- TACO-Fuzz: Efficient Directed Hybrid Fuzzing via Target-Centric Seed Selection and Generation, OOPSLA 2026. https://2026.splashcon.org/details/oopsla-2026/35/Efficient-Directed-Hybrid-Fuzzing-via-Target-Centric-Seed-Selection-and-Generation

## 八、近期验证状态

最近一次完整验证结果（2026-07-24）：

- `for t in test/test_*.py; do python3 "$t"; done`：108 个 unittest case 通过；
- `cmake --build build -j2`：通过；
- `cmake --build build --target check -j2`：63 个 lit 测试通过；
- `git diff --check`：通过。

这些测试覆盖 compiler instrumentation、Backsolver/Veritesting、UCSan seed、
S2F actionseed、AFL data coverage、distributed state、bounded-DPOR schedule replay、
multi-master fenced lease、coverage-owner gossip、ECT、verified semantic proposal、
semantic fallback、IFSS/Hydra-style targeted transform proposal、agentic hooks、
GenSym-style live continuation descriptor、ConDPOR-style HB/lockset schedule
analysis、ConDPOR-style memory read/write trace、schedule constraint artifact、
Query IR joint replay validation、schedule memory provenance filtering/tagging、
bounded SC schedule-SMT structural/mutex-rwlock-condition-thread lifecycle-state
scaled-anchor partial-order、checked linear extension 与 topology replay、
hybrid feedback 和
核心运行时集成。

最近一次增量验证结果（2026-07-27）：

- `python3 test/test_solution_generator.py -v`：5 个 unittest 通过；
- `python3 test/test_string_constraints.py -v`：2 个 unittest 通过；
- `python3 test/test_afl_hint_mutator.py -v`：4 个 unittest 通过；
- `lit -sv build/test/query_generator.py`：通过；
- `lit -sv build/test/query_ir.c`：通过；
- `lit -sv build/test/query_prefix_reuse.py`：通过；
- `lit -sv build/test/string_constraints.c`：通过；
- `cmake --build build`：通过；
- `python3 test/test_schedule_exploration.py -v`：42 个 unittest 通过，覆盖
  schedule constraint artifact 与 explorer queue 一致性、stack/owner memory filter
  runtime behavior、provenance tag parser/runtime emission、schedule-SMT PO/prefix/
  conflict encoding、完整 trace HB assumptions、incremental context reuse、event/query
  truncation accounting、mutex forced-overlap UNSAT、rwlock reader-overlap SAT、
  trylock busy assumptions、rwunlock lockset release、condition wait mutex split、
  signal/broadcast wake witness SAT/UNSAT、timeout ownership recovery、真实 pthread
  mutex/condition wake/timeout、stable create/join/`pthread_exit` lifecycle recovery、
  lifecycle partial/permutation 差分 SAT/UNSAT、scaled-anchor 线性计数、
  certificate/query/tamper checker、topology 与 direct-solve CLI、真实
  topology-prefix replay、v7 exact/relaxed lazy refinement、结构化 libz3 model
  extraction、detach/cancel/join-cancelled/retire lifecycle、4104 detached-thread
  registry recycling、MPI-side JSONL export，以及系统 libz3 SMT-LIB2 parse/solve。
- `python3 test/test_query_store.py -v`：12 个 unittest 通过，覆盖 schedule artifact
  与 Query IR 的 joint validation、JSONL 输出和 QueryStore stats。
- `python3 -m unittest discover -s test -p 'test_*.py' -v`：193/193 通过；
- `cmake --build build --target check -j2`：87/87 通过；
- `git diff --check`：通过。

增量测试覆盖 reusable solution generator、converter-chain 提取、field-aware sampling、
query-store generator artifact、真实 persistent query solver 集成、异步 Query IR、
prefix reuse、AFL full-matrix polytope mutator 和 offset-aware string-constraint
candidate，以及 query-service solver portfolio disagreement telemetry。

## 九、2026-07-27 F56--F62 研究实现

### 9.1 从 conflict heuristic 到可检查的 bounded Source-DPOR

F56 为 trace 建立稳定 event identity、thread program order 与 dependency graph，
以规范拓扑线性化定义 Mazurkiewicz equivalence digest；source analysis 会保留目标
线程的因果前驱，输出可检查 wakeup sequence/source set。explorer state schema v4
持久化 bounded sleep sets、wakeup leaves、已观察 equivalence classes 和 ConDPOR
revisit identities，从而减少跨重启重复。
`symcc-bounded-source-dpor-v1` 证书绑定 trace、edges、canonical order、sources、
bounds 和 SHA-256，独立 verifier 会重算全部字段。

这比原先“找到冲突对就翻转”的候选生成更接近 Source-DPOR。F61 又补上 bounded
wakeup tree 与 cooperative ready evidence，但仍没有完整 operational enabled-set、
unbounded completeness 或每个等价类一次的 optimality proof。

### 9.2 单一 SMT context 中的 path、schedule 与 read-from

F57 的 `symcc-joint-path-schedule-rf-v1` 把 Query IR QF_BV、schedule QF_LIA 和
memory read-from/byte bridge 放入同一个 system-libz3 context。memory trace 的
`sym-byte=N`、`init=0xNN` 与 write `value=0xNN` tags 提供显式跨域等式；
QueryStore 按 content hash 读取 Query IR SMT，求解后同时运行 Query IR evaluator、
schedule certificate checker 和 RF verifier。

关键回归不是只验证 joint SAT，而是构造“path 单独 SAT、schedule 单独 SAT、二者
bridge 后 UNSAT”的反例，证明实现确实排除了分离 witness。该机制仍只对 bounded、
tagged memory events 完整，不推测缺失的 runtime value。

### 9.3 SC/TSO/RA 参数化有界内存模型

F58 让 schedule authoritative base 真正包含 read-from/coherence constraints。
SC 使用 total order 与 last global write；TSO 放宽 store-to-load global PO，同时强制
FIFO store 和 same-thread buffer forwarding；RA 建立 rf、per-location mo rank、
Boolean hb closure、release/acquire synchronizes-with 与 coherence constraints。

当前 litmus evidence 覆盖 Store Buffering 的 SC/TSO/RA 差异、TSO stale
same-thread read rejection，以及 RA release/acquire message passing。RA 明确是
bounded subset，不覆盖 fence、release sequence、RMW、SC atomics total order、
non-atomic race UB、mixed-size tearing 或完整 x86 TSO。

### 9.4 可迁移 symbolic-state 数据面

F59 把 F38 descriptor 扩展为 `LiveStateStore`：canonical JSON SHA CAS、
parent-linked solver frames、symbolic store、fixed-page concrete/symbolic memory 和
page-COW forks。checkpoint id 就是 continuation CAS digest；restore 检查 digest、
schema、引用、cycle 和 depth。CLI 可 inspect checkpoint 或比较两个 memory roots。

这解决了“状态能否稳定序列化、去重、增量 fork 和验证”的数据面问题，但没有解决
“如何在任意 native instruction 恢复执行”。要达到 GenSym 主语义，仍需 CPS lowering
或等价的 PC/register/call-stack resume adapter，并处理 TLS、signal、syscall 与外部
resource state。

### 9.5 可执行研究证据与 claim gate

F60 的 builder 当前生成 `symcc-research-evidence-v2`，把 F56 equivalence、F57
joint solve、F58 memory-model litmus、F59 COW/restore、F61 wakeup/ready 和 F62
graph/revisit 的输入、bounds、hash、environment、oracle 与 claim exclusions 打包；
verifier 保持读取历史 v1。backend validator 自动探测 system-libz3、Z3 CLI 与 cvc5
CLI；缺少独立 solver family 时如实记录，不生成“共识”声明。semantic verifier
明确拒绝把当前证据扩展为 unbounded Optimal-DPOR、完整 ConDPOR/C11、native
resume 或论文级性能结论。

生成与复检命令：

```bash
python3 util/research_evidence.py --output evidence.json
python3 util/research_evidence.py --verify evidence.json
```

### 9.6 Bounded Optimal-DPOR wakeup tree

F61 按 POPL 2014 的 weak-initial 递归实现 ordered wakeup-tree insertion。每个 base
prefix 的 leaves 持久化，插入会检查 complete cooperative-ready root、sleep-set、
existing-leaf 和 ordered-sibling weak-initial 条件。preload runtime 在受控 pthread
点记录 `ready` snapshot，并通过同一 replay gate lock 原子化“snapshot、chosen
event、prefix index publication”，修复了 replay trace 偶发首事件反转。

`symcc-bounded-wakeup-tree-v1` 独立重算 trace、Source certificate、ready evidence、
insertion reasons 和 tree。这里的 ready 只表示已经到达 cooperative scheduling
point 的线程，不等同于程序语义上的完整 enabled set，因此不能推出 Optimal-DPOR
的 unbounded optimality。

### 9.7 Bounded ConDPOR execution graph 与 backward revisit

F62 建立稳定 action/read/write/constraint/init 节点及 `po/rf/co/add-order` relations。
对后加入 write 与先前 read，只有在 HB/lockset classifier 保留且 read 不因果先于
write 时才执行 backward revisit：删除 read 的严格 `(po union rf)+` 后继，令新 write
成为 read-from source，再以 co-maximal read、co-after-all write 和
model-outcome constraint tie-break 规则恢复 bounded maximal extension。

`symcc-bounded-condpor-graph-v1` 对 graph、revisit 和整份 certificate 分层哈希，
explorer 跨 trace 持久化 revisit identity；候选仍绑定 Source-DPOR/wakeup-tree 的
实际 replay prefix，不旁路现有调度 gate。关键边界是 read value 改变后事件存在性
也可能改变：当前 extension 只恢复 observed suffix，没有 interpreter-level
path-dependent event generation 或完整 memory-model consistency oracle。

### 9.8 当前自动化证据

- `test_schedule_exploration.py`：55 项，覆盖 F56 Source-DPOR、F58
  SC/TSO/RA litmus、F61 wakeup tree/ready evidence 和 F62 execution graph/revisit，
  并保留 F39--F55 全部 schedule 回归；
- `test_query_store.py`：13 项，新增 F57 disjoint-SAT/joint-UNSAT 与 joint-SAT
  verified witness；
- `test_distributed_state.py`：19 项，新增 F59 COW/restore/CLI/tamper；
- `test_research_evidence.py`：3 项，覆盖 F60 backend oracle、claim gate、digest
  tamper 和 CLI round trip。

最终全量验证（含 replay gate 线性化修复）：

- `python3 -m unittest discover -s test -p 'test_*.py' -v`：212/212 通过；
- `cmake --build build --target check -j2`：88/88 通过；
- `cmake --build build -j2`：通过；
- schedule runtime 严格 `-Wall -Wextra -Werror` 编译：通过；
- 真实 mutex prefix replay 独立进程压力重跑：50/50 通过；
- F61 cooperative-ready mutex replay 本轮追加压力重跑：20/20 通过；
- `python3 -m py_compile ...` 与 `git diff --check`：通过。

## 十、后续科研路线与严格未完成项

当前剩余工作已经从“核心数据结构缺失”转为四个需要独立研究阶段的大问题：

1. **Optimal-DPOR/完整 ConDPOR**：在现有 bounded wakeup/revisit artifact 上实现
   interpreter-level dynamic enabled set、path-dependent event existence、完整
   consistency oracle，并建立 unbounded soundness/completeness/optimality 论证；
2. **完整语言级弱内存**：加入 C11/C++ fence、release sequence、RMW、SC order、
   non-atomic race semantics 和 mixed-size access，再与 herd/diy litmus corpus 对拍；
3. **真正 live-state execution**：F66/F77 已将 CAS bundle 接到 portable CPS、
   MPI steal/recovery 和 state-local incremental solver；剩余工作是完整
   LLVM/native semantics 与真实 live-state search，而不是只调度 seed replay；
4. **R 级实证**：固定版本与容器，在公开 sequential/concurrent targets 上做等 CPU、
   至少 20 次重复、逐项与 interaction ablation，报告 coverage AUC、schedule class
   coverage、solver cost、memory/state amplification、置信区间和效应量。

因此，F56--F62 可以表述为“已实现并通过功能测试的有界 SOTA 子集”，不能表述为
“已完整复现 Optimal-DPOR、ConDPOR、C11 或 GenSym”，也不能在完成 R 级实验前宣称
覆盖率达到 SOTA。

补充技术依据：

- Source-DPOR / Optimal-DPOR, POPL 2014:
  https://user.it.uu.se/~parosha/publications/papers/popl2014.pdf
- ConDPOR, CONCUR 2025:
  https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.CONCUR.2025.26
- GenSym, ICSE 2023:
  https://continuation.passing.style/static/papers/icse23.pdf
- Stateless Model Checking under TSO/PSO, TACAS 2015:
  https://doi.org/10.1007/978-3-662-46681-0_28
- USENIX Artifact Appendix Guidelines:
  https://www.usenix.org/conference/usenixsecurity22/artifact-appendix-guidelines

## 十一、F63--F74 收口：从 observed graph 到 LLVM 可执行状态与科研协议

本轮继续完成了此前“严格未完成项”中可在现有架构内形成闭环的部分。

### 11.1 并发语义闭环

F63 把 branch/switch constraint 与 action basic block 作为 schedule event 记录，并
用最近 read 的 byte interval 建 path dependence。ConDPOR revisit 不再机械恢复
read-dependent suffix，而是将 constraint 与后续 action 标记为需重新执行生成。

F64 将 cooperative ready 从 tid 集扩展为 operation offer 集；证书恢复
mutex/rwlock/join/wait abstract state，把 offer 分为 enabled、blocked、unknown。
只有显式 runtime stop 且 registry 为空时才形成 bounded terminal witness。

F65 在 LLVM LowerAtomic 前记录 atomic load/store/RMW/cmpxchg/fence，避免 lowering
丢失 memory order 或与普通 memory notification 重复。RA subset 已加入 RMW
atomicity、release sequence、fence synchronizes-with、seq_cst rank、non-atomic race
rejection 和 mixed-size interval consistency。

这三项显著缩小了 F62 的 observed-control-flow/RA 缺口，但仍不构成 unbounded
ConDPOR proof 或完整 ISO C/C++ executable model。

### 11.2 可执行 live continuation

F66 在 F59 CAS 之上新增 `symcc-live-program-v1` 与
`LiveContinuationExecutor`。状态可以从 checkpoint 恢复 PC、solver frames、symbolic
values 和 page-COW memory，执行 call/return，并在 symbolic branch 上生成两个
child checkpoint。MPI master/worker 现可按 lease budget 执行和重新排队 frontier。

这使项目第一次具备“真正执行 checkpoint”的 portable research path，而不只是
descriptor/control plane。其严格边界是：目前需要手写/外部生成 continuation IR，
尚未自动把一般 LLVM/native target lower 到该表示。

### 11.3 R 级实验工具链

F67 新增 sealed paired randomized-block protocol：

- target×repeat 为 block，configuration 顺序和 block 顺序均由固定 seed 随机化；
- confirmatory phase 强制至少 20 repeats，tuning 与 final comparison 分离；
- 固定相同 CPU-second budget，按 core allocation 计算 wall budget并实际限核；
- 封存 commit/diff/status、CPU/kernel/toolchain/container 和 input SHA-256；
- 每个 run 原子保存 raw stdout/stderr/result，保留 nonzero failure 和 timeout；
- 覆盖曲线导出 normalized AUC 与 right-censored time-to-target；
- 统计报告使用 paired median bootstrap、sign-flip randomization、direction-aware
  A12/Cliff's delta 与 Holm correction。

同时修复了旧时间序列线程在被测任务结束后仍固定运行到 `timeout+30s` 的测量偏差。
serial、MPI、hybrid 与 AFL-only 现在都生成同口径曲线；hybrid 采样对 seed、全部
AFL/SymCC queue、原始 SymCC 输出与可选 GRIMOIRE/honggfuzz corpus 做 hard-link
快照，待 campaign 完全停止后才内容去重并执行 showmap，避免测量线程争用实验 CPU；
target extra arguments 贯穿执行和所有 replay。protocol 还把 manifest `cpu_cores`
显式传入 inner runner，避免按 mode/np 猜测造成预算与 AUC 分母错误。
F67 使 R 级 campaign 可执行、可复检，但当前尚未实际完成公开目标的 20-repeat
campaign，所以历史性能结论仍保持 B 级。

### 11.4 LLVM-to-continuation lowering 与 feasibility

F68 新增 legacy/new-PM `live-continuation-export` pass，将入口可达的 bounded
integer SSA 编译到 F66 IR。entry integer arguments 按 little-endian input bytes
绑定；select、switch、direct internal call 和 PHI 均保留。PHI 在 predecessor edge
block 中使用两阶段 copy，先保存所有 incoming，再统一写 destination，避免 loop 中
交叉 PHI 被顺序赋值破坏。

lowerer 采用 fail-closed artifact contract：只有所有可达 instruction 都在可信子集内
才写 `symcc-live-program-v1`；pointer/memory、FP/vector、exception、indirect/
external/variadic call、reachable recursion、division、symbolic shift 和
poison-generating flag 输出结构化 rejection。`llvm_to_continuation.py` 支持 LLVM
IR/bitcode 与 C/C++，`run-llvm` 完成 lowering 到 checkpoint execution，
`SYMCC_LIVE_LLVM` 将它接入 MPI。

executor 同时把 CAS expression DAG 与全部 solver frames lowering 为 QF_BV，复用
现有 `symcc-query-solver` 做 branch/assume feasibility。UNSAT child 在 checkpoint
提交前剪除；unknown 或 solver 不可用时保守继续，并输出 check/pruned/unknown
telemetry。该实现把 F66 从手写 research IR 推进到真实 C/LLVM 的整数 SSA 子集，但
仍不是 arbitrary LLVM/native continuation。

### 11.5 Object-aware static memory lowering

F69 在 F68 frontend 上加入 bounded static-global memory。lowerer 只为入口可达代码
实际引用的 initialized global 分配 synthetic address，按 LLVM `DataLayout` 保存
initializer、alignment、target endianness 与 object bounds。constant GEP 被归一为
object-relative offset，integer load/store 最多 8 bytes；null、越界、只读写入、
TLS/nonzero address space 和 symbolic GEP 均产生 rejection report。

executor 将多字节 symbolic store 拆为 byte-expression CAS roots，page-COW 只复制
被修改页；load 再按端序组合成 QF_BV expression，因此 memory-derived branch 可以
继续进入 F68 feasibility solver。为避免只信 compiler，program validation 还独立
检查 memory image 大小、object non-overlap/bounds/read-only；symbolic address
现在明确拒绝，不再按当前 concrete witness 静默访问。

该阶段使包含 mutable global、constant table/string 和 constant field access 的真实
C 函数可以进入 live continuation，但不包含 frame-local stack、heap、pointer input
buffer、symbolic alias 或 external resources。

### 11.6 Explicit-size symbolic input-buffer ABI

F70 将真实 parser 常见的 exact `entry(ptr,size)` 签名映射到 continuation memory。
pointer 指向独立 synthetic input object；新 `input_size` 指令读取当前 seed 的实际
长度，而不是消耗额外 symbolic bytes。每个 seed byte 的 canonical input expression
同时写入该对象的 page-COW symbolic slot，因此由 input load 产生的表达式可以直接
进入 F68 QF_BV feasibility solver。

program 使用 `symcc-live-input-buffer-v1` 描述 address/capacity/size width，并以
`kind=input,logical_size=input-length` 标注对象。executor 独立检查 descriptor、
object 和 instruction width 的一致性；每次访问按实际 seed length 收紧 capacity。
这消除了“预留零填充被解释为合法短输入后缀”的不健全行为，也使 MPI checkpoint
在 AFL 移动原 seed 后仍可独立恢复。

capacity 同时受 `SYMCC_LIVE_INPUT_BUFFER_LIMIT`、全局 memory limit 和 size 参数
位宽约束。当前 compiler 仍只接受 input object 内的 constant GEP；dynamic/symbolic
index、pointer 跨函数传播和 array-theory memory 明确保留到后续阶段。

### 11.7 Frame-local fixed stack objects

F71 将 mem2reg 后残留的 fixed entry-block alloca 映射为 owner-function stack
objects。compiler 使用 DataLayout/Align 建确定性地址，并要求每个 load 都存在覆盖
完整字节范围的 dominating store；dynamic/scalable alloca、symbolic offset 与
pointer escape 继续产生 rejection artifact。

executor 不把 stack backing zero 当作 LLVM 初始化值。每次 stack store 写入
`@stack:init:<depth>:<address>` CAS markers，load 同时检查 top-frame owner 和逐字节
marker。markers 随 checkpoint pause/fork/resume；return 删除该深度的 markers 与
SSA locals。因此非递归 call graph 上同一 callee 的顺序重入可以复用 synthetic
slot，而不会读取前一次 invocation 的陈旧状态。

该方案是 bounded frame virtualization，不是 native stack capture。它保守拒绝复杂
path-sensitive must-initialize、递归 frame、跨函数 stack pointer、dynamic alloca
和 exception unwinding。

### 11.8 Bounded call-site heap lifetime

F72 把 direct `malloc(constant-size)` 编译为固定 synthetic call-site slot，并以
`heap_alloc`/`heap_free` continuation ops 显式改变生命周期。artifact 保存
`kind=heap`、stable site、size、`runtime-alloc-free` 和
`bounded-infallible` contract；单对象大小由
`SYMCC_LIVE_HEAP_OBJECT_LIMIT` 与全局 memory limit 共同约束。

executor 不把预留 heap backing bytes 当作已分配或已初始化内存。allocation 写
`@heap:live:<base>`，store 写逐字节 `@heap:init:<address>`；load 要求两类 marker，
free 删除它们。同调用点在释放后可以重入并复用槽，但不能读取旧内容；UAF、
double-free、double-allocation 和未初始化读取均 fail closed。lifetime state 完整
进入 symbolic-store CAS，因此 pause/fork/迁移恢复不会丢失。

F72 阶段的模型有意采用 bounded-infallible allocation，并限制一个调用点最多一个
live instance；F75 已用固定容量 pool 取代该单实例限制。当前整体仍不模拟
target-visible 资源耗尽、任意 allocator、pointer escape 或 heap graph path
equivalence，因此是向完整 heap semantics 迈进的可审计子集，不是完整 libc heap
或 POSE 复现。

### 11.9 Bounded symbolic aliases and finite ITE memory

F73 使用 LLVM GEP offset decomposition 保留一个 symbolic integer index 的 object
provenance。compiler 按 base/size/scale/access width 枚举同一对象中的 defined
addresses，并以 `SYMCC_LIVE_ALIAS_LIMIT` 限制集合；`pointer_offset` 保存真实 modular
地址表达式，load/store 携带地址集合和 signed alias-index interval 组成的独立可
复检 contract。该 interval 与地址 guard 共同入 solver frame，排除大索引经过
BV wrap 落回对象而使 `inbounds` poison 路径伪装成合法 alias 的情况。
链式 GEP 还分别约束 `inbounds` 动态结果，以及 constant-`inbounds` 操作的动态
base 与 result，防止非法中间指针被后续常量偏移带回对象后逃过检查。

executor 对 load 构造 address-guarded ITE value，对 store 构造逐字节 guarded
read-over-write，并把 alias-domain OR 写入 path condition。候选区间重叠时也组合
全部条件。stack/heap init marker 同步变为布尔表达式，使 symbolic store 后的
conditional initialization 可以跨 checkpoint 恢复。memory op 本身不 eager fork；
后续控制流依赖 ITE 时才分裂状态。

F73 单独使用时只支持单对象、单 variable term、pointer/index width 相同、
no-wrap subdomain 和最多 256 aliases，不是通用 SMT Array。F74 在此基础上补充
pointer PHI/select 的跨对象 union，但每个 arm 仍必须能分解为上述有限对象内
domain。

### 11.10 Guarded pointer provenance union

F74 对 pointer select/PHI 同时保留 address BV 与有限 object provenance。
select alternatives 使用条件真假 guard；PHI edge block写 32-bit incoming
discriminator，避免不同 object 恰好使用同一 synthetic address 时丢失来源。memory
artifact 用 `alias_cases` 表示多个 object-local candidate set；每个 case 的控制流
guard、signed no-wrap index domain 和 address equality 在 executor 中相与。

该方法支持 cross-object finite ITE load、guarded byte store，以及每个 select arm
各自包含 one-term symbolic GEP。null load arm、read-only store arm和 runtime
inactive stack/heap arm通过 memory domain剪枝，而不是按当前 concrete witness
继续执行。compiler 的 `SYMCC_LIVE_ALIAS_LIMIT` 约束整个 union 的地址总量；
executor 又独立验证每 case object ownership、guards、index interval 和硬上限。

F75 已在此基础上支持 GEP-after-union 和 fixed-capacity multi-instance heap；
F76 又补充 direct-call pointer argument/return，F78 补充 pointer eq/ne 与
runtime-sized nullable heap。当前仍拒绝 cyclic pointer PHI、pointer-valued
memory、ordered pointer comparison、multi-term GEP、moving heap graph 和超出
finite bound 的 union。这是 path-sensitive finite value-set memory，不是全程序
points-to、SMT Array fallback 或完整 LLVM pointer semantics。

### 11.11 Bounded multi-instance heap identity

F75 将 F72 的单槽 allocation site 扩展为 stable slot pool。
`SYMCC_LIVE_HEAP_SITE_CAPACITY` 默认 4、硬上限 64；每个 slot 独立保存 address、
live/init markers 和 `slot/capacity` artifact metadata。`heap_alloc` 每次选择第一个
free slot，所有 slot 均 live 时显式报告 bounded-model exhaustion。该 deterministic
first-free 规则为当前不可观察 slot 命名提供 canonical representative，减少等价
空闲对象排列造成的状态差异，同时不假装模拟宿主 allocator。

malloc result 的每个可能 slot 都成为 guarded pointer alternative，guard 为
`result==slot_base`。后续 load/store 因此能复用 `alias_cases` 对每个 instance
独立复检 bounds、liveness 与 initialization。GEP 也改为对每个 base alternative
递归变换，同时从真实 base SSA 计算 pointer BV；这消除了 F74 的
GEP-after-union 缺口。union load 的 must-initialize proof 按 object base、offset、
index/scale 和访问宽度比较 store/load alternatives，不依赖恰好相同的 pointer SSA。

LLVM/C tests 覆盖同站点两个同时存活实例、第三次分配 exhaustion 和 union 后 GEP；
executor tests 覆盖第一次分配后 pause、恢复并分配第二个 slot，以及 incomplete/
reordered pool artifact rejection。该实现是后续 heap-isomorphism quotient 与
POSE 风格路径等价研究的稳定 identity 基础，但尚无 graph isomorphism、path-optimal
merge、dynamic allocator 或 target-visible OOM branch。

### 11.12 Bounded cross-function pointer/object references

F76 在 acyclic direct-call graph 上为 pointer formal 和 pointer return 构造 finite
object-reference summary。callee 参数候选来自全部可达 call actual，provenance
guard 重写为 `formal==candidate_address`；caller 中的 pointer call result则使用
`result==candidate_address`。返回 expression digest 原样跨 frame 绑定，因此数值
关系仍由真实 SSA DAG 保留，summary 只负责 object/lifetime 身份。

Artifact 以 `pointer_params/pointer_return_bits` 描述函数 signature，call/return
分别携带 matching pointer width contract。validator 和 runtime 双重检查 index、
arity、destination 与 expression width。caller stack pointer 在 callee 中仍引用
原 owner function；executor 根据无递归 frame stack 唯一定位 owner depth，使
callee store 的 init marker写在 caller depth 并可跨 checkpoint 恢复。任何 rooted
in callee alloca 的 pointer return 在 lowering 阶段拒绝。

LLVM/C tests 覆盖 global union 参数、constant GEP、pointer echo forwarding、caller
stack store/load、heap pointer return/free；executor tests 在 callee 内暂停并复检
depth-0 marker，同时覆盖 signature tamper。caller-side symbolic-GEP actual 暂时
拒绝，因为只传地址会丢失原 signed/no-wrap index domain；后续必须显式传递 domain
certificate，不能用地址枚举替代。

### 11.13 CAS-rooted state-local incremental solver

F77 以 immutable `path_condition_root` 作为 worker-local QF_BV context key，并扩展
既有 solver-helper server 协议。一个 state 的多个 candidate assertion 在 exact
context 上用 Z3 push/pop；child root 缺失而 parent root 存在时，仅复制 parent
assertions 并解析新 CAS frame；冷 worker 或 eviction 后则从完整 CAS chain
重建。cache 因此不进入 checkpoint identity，崩溃或迁移只影响性能，不影响可恢复
语义。

Executor 将 frame 规范化成 declaration/assertion fragments，使用有界 LRU 保存
prefix/delta/target SMT、CAS frame metadata 与 fragments。MPI worker 现在跨 lease
复用同一 executor/solver process。协议错误、pipe failure 或 server timeout 会熔断
incremental path 并回到原 `--generic` one-shot；UNKNOWN 继续保守保留 state。
执行结果单独报告 exact hit、parent hit、cold rebuild、fallback 和 materialization，
从而支持结构性消融。

两层独立 branch 的定向实验包含 6 次 feasibility checks：1 cold rebuild、2 parent
derivations、3 exact hits；同 executor warm-resume 为 2 exact hits、0 rebuild，
新 executor cold-resume 为 1 rebuild + 1 exact hit。显式关闭增量时 6 次 one-shot
checks 仍产生相同四条结果。该结果证明机制与语义，不替代公开 benchmark 上的
solver-time/coverage-per-CPU 统计。

### 11.14 Nullable runtime-sized allocator

F78 将 F75 的固定、耗尽报错 heap pool扩展为
`bounded-pool-nullable`。dynamic malloc/calloc 的物理 slot 容量与
`@heap:size:<base>` 逻辑长度分离；first-free、live、size 和 init 均为条件
bit-vector expression。零/超界请求与容量耗尽返回 null，解引用同时证明对象存活和
`offset+width<=logical_size`，因此预留 backing bytes 不会被误当成可访问空间。

Calloc 保留 count/element-size 双 operand，在目标位宽检查乘法 wrap 后对逻辑范围做
条件零写。Realloc 采用 bounded in-place strategy：成功保持地址并保留
`min(old,new)` 初始化，增长区未初始化，零大小释放，超界失败保留旧对象。Compiler
还支持 pointer `eq/ne`，使真实 LLVM/C 的 OOM branch 可进入 state fork。

定向测试覆盖 nullable OOM、pool exhaustion、logical OOB、calloc zero/overflow
结构、realloc failure/growth/zero 和 checkpoint markers；LLVM IR 与 C frontend
均执行 dynamic malloc/calloc/realloc。该阶段仍是有限 allocation-site model，
不是 moving allocator、heap graph quotient 或 POSE path-optimal merging。

### 11.15 Caller-domain symbolic pointer certificate

F79 消除了 F76 在 direct-call 边界丢失 symbolic GEP 定义域的问题。Compiler
不再只传 numeric pointer，而是为每个 pointer actual 构造 1-bit certificate：
每个 provenance arm 合取 select/PHI/heap guards 与 signed index min/max，所有 arms
再析取。该 expression 作为隐藏实参传给 callee；callee 的 finite alias case 同时
要求 pointer address 相等与 certificate 为真。

Artifact 以 function/call 两侧精确匹配的 `pointer_domains` 映射记录隐藏
parameter/argument，pointer return 另以 `pointer_return_domain`、
return-side `pointer_domain` 和 call-side `pointer_domain_dst` 转发证明。
Validator 在 checkpoint 创建前检查索引、arity 与 mapping，executor 在 call/return
时复检 1-bit 宽度。LLVM IR、真实 C、手写 artifact tests 覆盖 symbolic actual、
identity return、false certificate pruning 和 mapping tamper rejection。

该实现是 bounded proof-carrying provenance，不是通用 points-to analysis。它仅有限
展开 object 内 offset，保守排除 one-past summary，并继续拒绝 multi-term GEP、
递归、indirect call、pointer-valued memory、callee-stack escape 和 external
pointer effects。

### 11.16 Bounded indirect-call points-to dispatch

F80 把由 internal function constant/cast、`select` 或 acyclic PHI 形成的有限函数
指针集合纳入 live continuation。Address-taken targets 使用稳定非零
`function_id`；`indirect_call` 同时携带 BV selector 与每个 target 的
select/PHI guards。Executor 对 `selector==id AND guards` 做 QF_BV feasibility，
多个可行目标像 symbolic branch 一样产生独立 CAS children，并压入普通 call
frame。

该阶段同时补上 `param_bits`、`return_bits` 与 call-side `result_bits`，避免
artifact 仅凭参数个数宣称间接目标同签名。Reachability、recursive-SCC analysis、
validator 和 runtime 都纳入间接边/typed widths。LLVM select/PHI、真实 C function
pointer 与 artifact ID/signature tamper tests 已通过。

当前边界是最多 64 个 internal non-variadic targets；尚不从 pointer-valued memory
或函数参数恢复 targets，也不支持 pointer-returning indirect call、external
target 和 recursive SCC。

### 11.17 Deterministic external memory-compare summary

F81 为 constant-length `memcmp`/`bcmp` 建立第一组 external effect summary。Call
不会交给 host libc，也没有新增 executor opcode；compiler 将最多 64 bytes 的两侧
读取展开为既有 object-aware byte loads，并以 equality、unsigned ordering 和
nested select 构造 first-difference result。返回值规范化为 `-1/0/+1`，严格对应
C 只承诺符号的语义。

这使 input logical length、heap live/size、stack owner、initialization、
pointer-union/caller-domain guards 和 QF_BV pruning 全部自然复用。零长度不解析
pointer；symbolic/over-bound length fail closed。LLVM/C equality、三向顺序、
zero/null、pointer union 和 length rejection tests 已覆盖。

### 11.18 Bounded external region-write summaries

F82 为 constant-length `memcpy`/`memmove`/`memset` 建立确定性 write-effect
virtualization，并同时覆盖 libc declaration 与 Clang 产生的 LLVM memory
intrinsic。最多 64 bytes 的调用被展开成既有 byte loads/stores，不增加 executor
专用 opcode，因此 pointer-union、caller-domain、logical bounds、heap liveness、
stack ownership、initialization 和 page-COW checkpoint 语义继续由同一可信核心
执行。

`memmove` 在第一条 destination store 前先生成全部 source loads，显式保存重叠区域
的调用时快照。`memcpy` 对 source/destination 的所有有限 alias candidate 做区间
不相交证明，任一候选不能证明即 fail closed。`memset` 保留 value operand 的
symbolic low byte。零长度不访问 memory，libc declaration 仍返回原 destination
pointer。LLVM/C tests 覆盖 overlapping move、disjoint copy、symbolic fill、
input-buffer、zero/null 和 overlap rejection。

### 11.19 Pointer-returning bounded indirect dispatch

F83 移除了 F80 对 pointer-returning indirect call 的保守缺口，同时保持有限 target
和 proof-carrying object domain。Compiler 对每个 target 的 pointer returns 做
bounded provenance expansion，并把所有 finite object alternatives 合并到 call
result；实际被选 callee 的普通 return frame产生 1-bit pointer-domain certificate，
caller 通过 `pointer_domain_dst` 接收。Indirect call site 现在也参与 pointer formal
summary，使 callback 可以返回其 pointer actual。

Runtime 没有专用返回路径；target fork、typed call frame、return binding 和
checkpoint migration 完全复用已有机制。Validator 精确比较所有 target 的 pointer
parameter/domain mapping、return width/domain 与 result destination。LLVM、真实 C
和手写 artifact tests 覆盖固定对象/identity return、65/66 target fork，以及缺失
destination/异构 target domain拒绝。

### 11.20 Guarded access 与 bounded NUL-aware strings

F84 为 core `load` 增加 1-bit guard：定义域从无条件 `D` 变成
`NOT guard OR D`，结果为 `ITE(guard,value,0)`。因此 short-circuit library
summary 可以保留 input logical length、heap size/liveness、stack ownership、
initialization 和 finite alias 证明，而不会错误读取 NUL 后的 backing memory。
Validator 限制 guard 只能用于 load，executor 复检 1-bit width。

Compiler 在此基础上把最多 64 bytes 的 `strlen`、`strcmp` 和 constant-length
`strncmp` 展开为 core loads/BV/select/assume。`strlen` 记录 first NUL；
`strcmp` 在 first difference 或共同 NUL 后停止；`strncmp` 到 n 即停止且 n=0
不解析 pointer。LLVM/C、不同大小 pointer union、logical input boundary、
zero/null、symbolic n rejection 和手写 guard-domain tests 已覆盖。

### 11.21 Bounded pointer-returning search

F85 将 `memchr` 和 `strchr` 展开为 core byte loads 与 BV/select DAG，并保留
first-match 语义。`memchr` 接受 constant `n<=64`，`n=0` 不解析 pointer；
`strchr` 复用 guarded load，在命中或 NUL 后停止。返回值同时生成 null 与有限
source object/offset provenance，因此可继续 load、跨 frame传递或比较 null。
LLVM/C tests覆盖 symbolic needle、first match、short source union 与 extent拒绝。

### 11.22 Guarded write 与 bounded string copy

F86 为 core `store` 增加对偶的 1-bit guard：
`NOT guard OR access_defined`，memory byte 为 `ITE(guard,new,old)`，stack/heap
initialization marker也条件更新。Compiler 先快照所有可能 source bytes，再把
`strcpy` 写到 first NUL 并保留 suffix；`strncpy` 对 constant `n<=64` 写恰好 n
bytes并在 NUL 后填零。所有有限 source/destination object pair必须可证明不同，
返回值保留 destination provenance。LLVM/C、source union、n=0/null、overlap/
symbolic n rejection 和手写 guard-domain tests 已覆盖。

### 11.23 LLVM integer defined-value guards

F87 不再整体拒绝 div/rem、symbolic shifts 和 optimizer flags。Compiler 在 core
operation前生成 divisor nonzero、signed division overflow和 shift range
assumptions；`nuw/nsw` 用 unsigned bound、sign transition或 reversible shift/
quotient证明，`exact` 用 remainder或 reverse shift证明。这些 constraint进入同一
CAS solver root并标记 `llvm-defined-value-guards`。Direct freeze undef/poison仍因
缺少稳定 nondeterministic choice而拒绝，明确保持 defined-value under-approximation。
LLVM/C SAT/UNSAT与 artifact tamper tests 已覆盖。

### 11.24 Bounded pointer-valued memory

F88 将 pointer-width numeric bytes继续放在普通 page-COW memory中，同时为受支持
load恢复有限 provenance。Scalar global pointer initializer可指向 bounded data
global/null；stack/heap cell要求 exact storage SSA和唯一支配 pointer store。
Loaded-value equality与stored select/PHI guards共同构成 sidecar，load后可继续
GEP/dereference。Conditional reaching stores、non-pointer覆盖和function-pointer
cell fail closed。LLVM/C global/stack/heap/null与负例 tests已覆盖。

### 11.25 Function-pointer memory、pointer table 与 bounded memory merge

F89 使用稳定 internal function ID表示 pointer-width memory中的函数地址，并从
scalar global或exact-slot dynamic cell恢复 target-wise equality guards；既有 typed
indirect-call ABI负责执行和复检。F90 将同一机制扩展到固定一维 global data/function
pointer table：symbolic GEP先证明element地址有限，load再按address/ID过滤全表
provenance。F91 支持常见 if/else cell merge，但仅在每个直接 predecessor恰有一个
同 slot pointer store、且不存在其他写或非 pointer覆盖时接受。LLVM/C tests覆盖
data/function table、global/stack cell、双分支合流与缺失 predecessor拒绝；三项能力
分别声明 `bounded-function-pointer-memory`、`bounded-pointer-table` 和
`bounded-pointer-memory-merge`。

### 11.26 Proven-nounwind invoke

F92 对 call-site或每个有限 internal target均有 `nounwind` 保证的 LLVM `invoke`
保留 normal edge：artifact call携带显式 `normal_target`，callee return先绑定结果，
再进入可能存在的 PHI edge-copy block。Continuation reachability只对这种已证明调用
删除 unwind successor，因此 dead landingpad不会触发伪 rejection；可能抛出的
direct/indirect invoke仍 fail closed。Direct/indirect执行、PHI、may-unwind和
target-tamper tests已覆盖，能力声明为 `bounded-nounwind-invoke`。

### 11.27 Stable dynamic nondeterministic freeze

F93 新增typed `nondet` core instruction，支持direct integer
`freeze undef/poison`。Token由stable LLVM site与checkpoint-persisted 64-bit动态
counter组成，同一SSA use共享CAS digest，循环re-entry与恢复后执行获得不同实例。
One-shot/incremental QF_BV都声明精确位宽的独立symbol，input数字变量命名保持不变。
LLVM poison/undef双路径、循环两次执行、暂停恢复、counter与duplicate-site tests已
覆盖，能力声明为 `stable-nondeterministic-freeze`。Deferred poison propagation
仍是下一层语义工作。

### 11.28 Bounded acyclic pointer-memory SSA

F94 将F91的direct-predecessor规则替换为bounded backward last-writer dataflow。
Compiler从load位置逆向扫描exact slot；无writer的block递归全部predecessors，每条
entry路径必须有定义。这样支持entry default、nested overwrite与forwarding block，
同时排除unreachable/overwritten旧写。Data/function target discovery复用同一
closure，4096-node/256-store bounds、cycle、missing、non-pointer和atomic/volatile
diagnostics有自动化覆盖，能力声明为`acyclic-pointer-memory-ssa`。

### 11.29 Deterministic scalar external summaries

F95 将strict-ABI `abs/labs/llabs`、`htons/ntohs/htonl/ntohl`和LLVM bswap降为普通
BV/select DAG。Abs的`INT_MIN`不可表示条件进入solver assume；network order按target
DataLayout而非host决定identity/swap，frontend builtin folding前后共享语义。LLVM/C
symbolic abs、ntohl/bswap常量和wrong-ABI tests已覆盖，能力声明为
`bounded-scalar-external-summary`。

### 11.30 Bounded bit-count intrinsic summaries

F96 将scalar integer `llvm.ctpop/ctlz/cttz`展开为O(w) core-BV DAG，覆盖1..64位。
Population count求逐位和；leading/trailing count求MSB/LSB zero-prefix和。
`is_zero_poison=true`生成nonzero assume，false时零输入返回位宽。LLVM/C frontend、
symbolic分支、zero结果与infeasible definedness tests已覆盖，能力声明为
`bounded-bitcount-intrinsic`。

### 11.31 Direct deferred-poison freeze

F97对single-use direct `freeze(add/sub/mul nuw/nsw)`延迟definedness，不再在算术点
提前assume。Freeze结果为`ite(defined, wrapped, stable-nondet)`；overflow与defined
tests分别证明任意choice和原值保留，多consumer继续fail closed。能力声明为
`bounded-deferred-poison-freeze`，完整per-SSA poison lattice仍列为后续工作。

### 11.32 Bounded cyclic pointer-memory SSA fixed point

F98以CFG `IN/OUT={last-writer set, may-uninitialized}` fixed point替代遇回边即拒绝的
backward DFS。Full-width exact-slot store执行kill，writer-free block合并所有live
predecessors；4096 transfer/256 store bounds保持显式。Loop-invariant与loop-carried
writer tests分别返回65/66，未初始化循环拒绝；能力声明为
`bounded-cyclic-pointer-memory-ssa`。

### 11.33 Canonical constant-GEP pointer-cell identity

F99使用target DataLayout将constant GEP chain规范化为
`(base, pointer-width modular offset)`，fixed-point不再要求重复计算的非零field
pointer具有相同SSA identity。Equivalent与distinct-offset tests分别验证正确合并和
隔离，能力声明为`canonical-pointer-cell-identity`。

### 11.34 Bounded bit-permutation intrinsics

F100按LLVM LangRef将scalar `bitreverse/fshl/fshr`降为core BV permutation。
Funnel amount先对bitwidth取模，shift=0/大shift不进入LLVM poison边界。Symbolic
reverse与官方风格funnel examples通过，能力声明为
`bounded-bit-permutation-intrinsic`。

### 11.35 Bounded saturation arithmetic

F101以wrap/sign predicates与select clamp支持scalar signed/unsigned
add/sub saturation；F102以shift-range assume和logical/arithmetic reverse check扩展
signed/unsigned saturating shl。Packed extrema、symbolic saturation和out-of-range
poison tests已覆盖，统一声明`bounded-saturating-arithmetic-intrinsic`。

### 11.36 Scalar selection and branch-hint intrinsics

F103支持LLVM scalar abs/min/max，精确区分INT_MIN poison flag；F104按LangRef将
expect/probability严格降为第一个value，不将branch hint误作constraint。Packed、
symbolic和poison tests通过，能力分别为`bounded-scalar-selection-intrinsic`与
`llvm-optimization-hint-identity`。

### 11.37 Bounded static objectsize

F105复用finite pointer provenance，对fixed global/stack/heap、null和guarded union
实现LLVM objectsize remaining-size ITE，严格验证min/null/dynamic flags。Stack、
不同size union与null tests通过；runtime input拒绝防止capacity/logical混淆。

### 11.38 Bounded dynamic objectsize

F106在F78 runtime-sized对象上加入logical-size provenance：pointer-size input使用
实际input length，malloc/calloc使用请求size/product，并在目标pointer width计算
current pointer到object base的offset。动态查询返回有界`max(size-offset,0)`；
dynamic=false保守返回unknown，绝不泄漏静态池capacity。Input interior、dynamic
malloc和static-runtime unknown三个执行用例通过，能力为
`bounded-dynamic-objectsize-intrinsic`。Realloc-current-size、custom allocator与
unknown external object在F106阶段仍未覆盖；其中bounded in-place realloc由F109
补齐，moving/custom allocator仍未覆盖。

### 11.39 Overflow aggregates、ssa.copy 与 realloc objectsize

F107把六类scalar `*.with.overflow`的wrapped value与overflow bit桥接到bounded
extractvalue；F108对integer/data-pointer/function-pointer `ssa.copy`保持identity、
finite provenance与typed target discovery；F109修复realloc result可能沿用旧
malloc size的问题。realloc provenance按pointer alternative保存，因而
select/PHI混合fixed object或多个realloc site时不会共享错误长度；成功缩容返回
new request，失败遵循null semantics。Packed、symbolic、pointer/function
dispatch及realloc成功/失败/union用例均通过。

### 11.40 Transitive deferred poison

F110沿single-consumer cast/BV/icmp/ssa.copy链传递definedness，到freeze才用stable
nondet恢复poison语义；multi-use和非线性consumer继续defined-only。F111将deferred
division/exact remainder totalize为safe divisor，消除eager evaluator对未选择
wrapped child的除零。Poison/defined cast、comparison、ssa.copy、div-zero、
sdiv overflow与exact tests通过。

### 11.41 Select、PHI 与 straight-line memory poison

F112按`cond_defined ∧ ite(cond,T_defined,F_defined)`实现integer select的臂敏感
definedness；F113把PHI value/defined bit一起放入two-phase CFG edge copy；F114只对
same-block、exact pointer SSA、single store/load、无其他memory/call的路径建立
memory definedness sidecar。F115进一步用DataLayout证明相同base与相同constant
offset，使不同但等价的GEP SSA可以安全共享sidecar，同时以distinct-offset负例约束
边界。F116沿最多64个unique-successor/unique-predecessor edge组成的无memory
effect corridor传播sidecar，并在merge处fail closed。选中/未选poison
arm/predecessor、poison condition、global store/load、canonical GEP、cross-block
corridor均有执行证据；F116时期的merge fallback已由F124--F149的edge/lane contracts
扩展。

### 11.42 Direct-call poison ABI

F117为唯一direct callsite的bounded integer return加入显式defined-bit ABI：
function/return/call分别声明`return_defined`、`defined`和`defined_dst`，validator
检查三端一致，executor在callee frame pop前把condition传回caller。Poison return
经caller freeze恢复双路径，defined return精确返回2；多callsite回退与缺失ABI
endpoint均有负证据。F118以`defined_params`/`defined_args`增加相反方向的typed
argument condition；callee参数可在内部freeze恢复poison，且复用普通frame参数
transport。Return contract detection同时收紧为actual return operand backward
slice，避免argument-only poison误标返回。F119把ABI推广到最多64个全部可证明的
direct callsites：return方向要求所有results均到freeze，argument方向允许每个
callsite分别传condition或true；mixed ordinary return consumer继续fail closed。

### 11.43 Symbolic pointer-memory 与 initial definition

F120修复global pointer initializer掩盖runtime stores以及dynamic GEP pointer被
静态base guard折叠的问题。完整dynamic identity进入dedup key；finite symbolic
pointer先枚举concrete address再按loaded value选择；data/function pointer
provenance和target discovery统一合并initializer与reaching stores。Mutable global
cell上的A/B roundtrip、function target overwrite和conditional initializer
fallback均有双路径执行证据。

### 11.44 Transitive argument-to-return poison

F121组合F117 return channel与F118 argument channel，修复formal argument经透明
integer slice返回时definedness在callee return处断开的错误。Return backward
slice现在可识别带deferred-poison contract的Argument，并沿bounded BV、cast、
icmp、select、PHI和`ssa.copy`传播；poison/defined两个direct callsites均通过同一
passthrough callee，validator复检`defined_params`/`defined_args`与
`return_defined`/`defined`/`defined_dst`的双向端点。Artifact显式声明
`bounded-transitive-call-deferred-poison`，便于调度器和科研证据区分该有限语义
与general interprocedural poison analysis。

F121后续审查还发现capability误标条件：函数内部freeze参数但返回独立local
poison时，双向ABI同时存在却没有argument-to-return依赖。实现已将能力判定收紧到
实际return backward slice，并增加禁止误标的负例。

### 11.45 Bounded multi-consumer poison DAG

F122把single-use sink walk推广为最多256次value访问的all-use proof：每个use必须
终止于freeze或经过已支持的透明integer/PHI/memory/direct-call节点继续满足全称
条件。共享definedness condition沿fan-out传播，每个freeze保留独立stable choice；
mixed ordinary consumer、循环和预算溢出继续fail closed。正例覆盖同一poison的两个
freeze sink，负例覆盖一个freeze加一个普通consumer，并声明可复检能力
`bounded-multiconsumer-deferred-poison`。

### 11.46 Bounded multi-load memory poison

F123把exact scalar memory sidecar从单个load推广到最多64个线性同址load，并以
同址全宽store clobber或函数出口结束memory version。Forward scan收集全部load，
reverse scan允许跨过同址earlier loads寻找最近store；F122再证明每个loaded value
的全部use均到freeze。任何未知memory effect、不同地址、分支/合流、循环或预算溢出
都fail closed，artifact声明`bounded-multiaccess-memory-deferred-poison`。原有
memory正例补入显式clobber，修复“首个load后停止、忽略后续consumer”的证明漏洞。

### 11.47 Acyclic branch memory poison

F124把线性memory version推广到最多64个acyclic blocks。Forward DFS枚举store后的
全部路径；每个load再对所有predecessors执行reverse proof，只有所有路径得到同一
exact `StoreInst`才传播definedness。共同store支配diamond的正例对两项selector
状态产生`1,1,2,2`；没有共同runtime definition的路径只保留global initial
value并裁掉poison路径。Artifact声明
`bounded-branch-memory-deferred-poison`。

### 11.48 Path-dependent memory definedness PHI

F125进一步支持merge的每个direct predecessor各自以同址full store结束。编译器在
预建edge block中写入对应store的i1 condition，merge load使用随实际路径到达的统一
defined destination；poison/defined两臂产生`1,2,2`。Function artifact包含
`memory_defined_phis`端点合同，production validator复检每个edge恰有一个i1
assignment且jump到声明merge；删除端点的tamper oracle必须失败。能力标记为
`bounded-memory-definedness-phi`。

### 11.49 Interprocedural memory definedness PHI

F126把F117 return与F118 argument defined-bit组合进F125。只有
`argumentHasDeferredPoison`或callee return backward slice已经证明的direct-call
端点才作为incoming source；edge block直接复用function context中的ABI bit。
Argument和return两个方向分别以poison/defined store diamond得到`1,2,2`，artifact
声明`bounded-interprocedural-memory-definedness-phi`并在PHI metadata标记
`interprocedural`。

### 11.50 Multilevel and cyclic memory definedness PHI

F127允许每个incoming edge通过最多64个memory-free forwarding blocks反向解析到
唯一store；condition仍在最终edge block赋值，并覆盖defined clobber与ancestor
poison store的非对称`1,2,2`。F128进一步把相同edge contract作为
loop迭代memory state：entry/backedge均有exact store时，每次到header都更新defined
bit。两迭代正例保存第一次poison freeze并由第二次defined store结束，结果1/2；
backedge missing-store负例保持不可行。Capabilities分别为
`bounded-multilevel-memory-definedness-phi`和
`bounded-cyclic-memory-definedness-phi`。

F129把direct scalar global ConstantInt initializer作为valid defined entry state。
No-store incoming edge写true，poison-store edge写condition；initial value 0与
poison freeze组合得到`1,2,2`，并声明
`bounded-initial-memory-definedness-merge`。

F130把该证明扩展到constant-offset global aggregate subobject。编译器以target
`DataLayout`规范化GEP后，使用LLVM `ConstantFoldLoadFromConst`验证目标typed
integer cell确实具有可折叠的`ConstantInt`初值；独立重算的store/load GEP继续由
canonical-address proof绑定。Artifact同时标记`initial`与`initial_subobject`，
validator强制前者是后者的父合同。数组element 2的poison-store/no-store路径产生
`1,2,2`，删除父标记的tampered artifact被生产执行器拒绝；能力标记为
`bounded-initial-subobject-definedness-merge`。

F131把memory-PHI incoming source从nullable store重构为`Store/Initial/Carry`
三态。Reverse proof回到目标loop-header load且本迭代尚无writer时产生carry；
backedge edge block以defined sidecar自赋值保留上一迭代状态。Artifact同时标记
`cyclic`、`carry`和具体carry endpoint，validator复检自引用i1 assignment及
`carry => cyclic`。Entry poison store加纯no-write backedge的第二次freeze产生
1/2；同一最终edge前混合defined store/carry的负例继续fail closed。

F132覆盖canonical conditional store/carry diamond：reverse proof要求exact store
臂与carry臂各自直接来自同一个conditional branch，最终backedge生成
`select(condition, store_defined, prior_defined)`。Artifact在contract/endpoint
标记`conditional_carry`，validator要求恰一臂自引用prior state并复检层级标记。
Symbolic selector的defined-store与carry路径产生`1,2,2`；带额外arm forwarding的
负例保持fail closed。

F133允许store/carry arm经过bounded unique-predecessor forwarding链。Analysis在
共享64-block预算内分别枚举conditional-arm祖先，要求store链确实经过exact writer，
并只在两臂找到同一branch、不同successor时复用F132 transfer。Artifact新增
`forwarded_conditional_carry`层级标记；forwarded正例产生`1,2,2`，三路join负例
继续拒绝。

F134把最终join candidates按完整reaching-source tuple分组；多个endpoint只有在
解析到同一StoreInst且每一个都可映射到共同外层branch的同一arm时才合并。三路
store/store/carry正例产生`1,2,2,2`，两个不同StoreInst加carry的三source-group
负例保持fail closed；artifact新增`multiarm_conditional_carry`层级合同。

F135进一步在definedness域合并不同但都经bounded source analysis证明必定defined
的StoreInst。每个endpoint仍必须用自己的writer通过arm-membership proof，numeric
memory仍写实际7/8，只有sidecar统一为true。Distinct constants正例产生
`1,2,2,2`；defined/poison mixed writers负例继续拒绝。

F136合并写入同一poison-capable SSA value的不同writers。Reaching source新增完整
writer-membership集合，修复了最初只保留代表writer、导致其他store无法传播condition
的错误。两个shared-poison paths各产生1/2，defined carry产生2，总计
`1,1,2,2,2`；独立producer不等价。

F137对两个独立writers加carry实现固定深度2的condition tree。Static proof要求
三个singleton source groups、inner store split、outer store/carry split和
DominatorTree确认inner condition及两个stored values在最终edge可eager求值。
Backedge先计算inner writer-defined select，再与prior state做outer select；正例
得到`1,1,2,2,2`，四source负例只保留carry返回2。

F138以扁平proof IR替代继续增加固定writer字段。对4--8个singleton Store/Carry
sources，analysis收集每个leaf的unique-predecessor conditional ancestors，递归选取
能把当前leaf集合严格二分的共同branch，并在深度3--6内重建完整树。所有predicate
和stored values仍必须支配最终edge；lowering以后序i1 selects生成临时definedness，
根节点直接更新loop-carried sidecar。Validator独立复检root-reachable无环树、精确
深度/叶数、full-binary node count和唯一carry。三个独立poison writers加carry的
深度3正例得到`1,1,1,2,2,2,2`；路径局部producer版本继续只保留carry结果2。

F139允许递归tree leaf携带一个完全同源的endpoint group。对每个候选branch，
analysis逐一检查组内所有endpoint都包含该conditional ancestor且映射到同一
successor，之后才按source group而非代表candidate进行递归分区。同一writer在
store后的symbolic fanout因此保持两个真实执行路径，但只占一个definedness leaf；
三个writer source加carry仍形成4叶深度3树，执行得到
`1,1,1,1,2,2,2,2,2`。跨不同tree结构位置复用同一source仍需要DAG归约，未被
F139扩大声明。

F140处理同一Store source跨不同tree位置的有界情形。若组内endpoints对同一个
ancestor branch映射到不同successors，analysis将其判定为position conflict并按
endpoint展开为多个结构leaf；每个leaf引用同一个已证明definedness condition，
但分别参与递归partition。展开后仍要求4--8 leaves、深度3--6、唯一carry和全部
eager operands支配最终edge。两个StoreInst在不同嵌套位置写同一poison SSA value，
加两个独立writers与carry形成深度4、5叶树，得到
`1,1,1,1,2,2,2,2,2`；重复carry、Boolean minimization和无界DAG仍未声明。

F141把同样的位置展开扩展到exact no-write Carry source。递归root不再要求存在
恰一个直接self arm，validator从root select递归计数所有prior-sidecar leaves，
并与新增`condition_tree_carry_leaves`精确对照。普通递归树仍要求1个carry leaf；
multi-carry要求2--8且依附repeated-source父合同。三个poison stores与两个carry
位置形成深度4、5叶树，得到`1,1,1,2,2,2,2,2`。

F142允许同一loop header内多个静态不相交scalar cells各自建立definedness PHI。
Header pre-scan和writer reverse scan仅越过由target DataLayout constant region
proof确认不重叠的load/store；每个cell仍独立运行原有Store/Carry/tree analysis并
在edge block更新自己的sidecar。两个相邻aggregate subcells正例得到`1,1,2`，
同cell双load对照组不声明multi-cell capability；删除一个`multicell` marker会
形成孤立合同并被production validator拒绝。

F143把F142的不同base证明推进到lowerer能够完整表示的identified static objects：
不同global或entry-block static alloca。Reverse/header scan可以越过另一对象的
full-width scalar access；合同新增`identified_objects`层级并强制蕴含
`multicell`。两个独立`[1 x i8]`栈对象在同一loop header各自建立sidecar，执行
得到`1,1,2`；F142同aggregate用例与同cell负例均拒绝F143 capability，删除一个
identified marker会被production validator拒绝。动态alloca、heap/noalias与
symbolic-base points-to尚未被该有界证明覆盖。

F144继续把multi-cell sidecar扩展到两个不同fixed `malloc` allocation sites。
证明要求direct external call、非零常量size、address space 0且落在heap object
上限内，并与现有site-local bounded slot pool完全对齐。Header/reverse scan可以
越过另一heap/static object的scalar access；合同新增`fixed_heap_objects`并强制
蕴含`multicell`。两个`malloc(1)` sites的loop执行得到`1,1,2`，删除单个marker被
production validator拒绝，F142/F143对照组不声明F144 capability。

F145新增无副作用finite pointer-region collector，递归展开pointer select与acyclic
PHI、累计constant GEP offset，并对两个最多16-alternative域执行完整product
disjointness proof。只有所有pairs均不相交才允许独立sidecars；正例
`{offset0,offset1}`对`{offset2,offset3}`在select和前置diamond pointer-PHI两种
形态下均执行得到`1,1,2`，含offset1交集的负例保持guarded/infeasible且不声明
capability。合同新增`finite_pointer_domains`，validator复检其`multicell`
父合同与同block多项不变量。

F146为select alternatives保留literal `(condition SSA, polarity)`关系。空间上
重叠的region pair只有在两侧要求同一condition的相反极性时才可排除；任一guards
兼容的重叠立即拒绝整个证明。A `{0,1}`、B `{1,2}`共享同一select condition时，
唯一overlap来自不可同时成立的A.false/B.true，执行得到`1,1,2`；将B.true改为
offset0后，A.true/B.true实际重叠，负例保持infeasible。合同新增
`guard_correlated_pointer_domains`并与F145 all-pairs capability明确区分。

F147为pointer PHI alternatives记录`(merge block, incoming predecessor)`关系。
同一merge的不同predecessor choices互斥，same-predecessor choices仍兼容。Diamond
中A `{left:0,right:1}`、B `{left:1,right:2}`的唯一空间重叠跨不同edges，执行
得到`1,1,2`；B.left改为offset0后same-edge实际重叠并保持infeasible。合同新增
`phi_correlated_pointer_domains`，与F145/F146分类分别验证。

F148把one-term symbolic GEP提升为保守offset interval。LLVM ConstantRange给出
signed index域，constant/scale经`__int128`溢出检查映射到region起始范围；同base
只有完整区间加access width仍分离才组合。两个subarray共享`and i8,1`索引的正例
得到`1,1,2`，同subarray负例保持infeasible；合同标记
`symbolic_index_intervals`。

### 11.51 当前真正未完成的 SOTA 工作

后续不应再以增加 schema 字段替代算法完成度。剩余工作按依赖顺序为：

1. **完整 LLVM continuation semantics**：在 F78 runtime-sized allocator 上实现
   完整 exception unwinding、symbolic-alias/partial-overlap poison propagation、
   unbounded/Boolean-minimized condition DAG 与 multi-cell MemorySSA、general heap
   points-to/alias-aware MemorySSA，以及
   moving/custom allocator 与 heap graph summary；
2. **Unbounded verified ConDPOR/Optimal-DPOR**：扩展 arbitrary operation enabled
   semantics、动态 event generation 和 maximal execution，建立 soundness、
   completeness 与 optimality 论证；
3. **完整 C11/C++ 对拍**：补 consume/dependency、完整 SC/UB/lifetime/tearing 和
   x86 propagation，使用 herd7/diy litmus differential oracle；
4. **真实 live-state search policy**：在自动生成的 live states 上实现 Empc/CBC/
   CGS/Vital 类 coverage compatibility、path cover 和 MCTS，而不是对 post-trace
   seed replay 重新命名；
5. **执行 F67 confirmatory campaign**：公开 sequential/concurrent targets，
   baseline、逐项与 interaction ablation，至少 20 paired repeats，报告 coverage
   AUC/CPU-hour、schedule class、solver cost、state amplification 和失败样本。

优先级上，1 是 4 的硬依赖；2 与 3 可以在 bounded interpreter 上并行验证；5 可以
先验证当前工程组合，但不能替代 1--4 的语义研究。

### 11.52 最终验证

- 全量 Python unittest：233/233；
- CMake/LLVM lit：90/90；
- `cmake --build build -j2`：通过；
- schedule preload runtime 严格 `-Wall -Wextra -Werror`：通过；
- 新增 protocol、paired analysis、continuation resume、atomic/path/enabledness
  针对性 tests：全部通过；
- 实际 `afl-showmap` 时间序列冒烟：maze MPI simulation 生成三点曲线、
  `coverage_auc=17.0`，统计分析器成功消费 benchmark/timeseries CSV；
- `python3 -m py_compile` 与 `git diff --check`：通过。

F68 完成后的全量验证为 Python unittest 236/236、LLVM/CMake lit 92/92、完整
compiler build、Python syntax 和 whitespace checks 通过。新增递归拒绝、
`unreachable` pruning、交叉 PHI、C source、CLI report、derived UNSAT pruning 和
64-bit concrete bit-vector 回归均包含在上述结果内。

F69 定向验证为 continuation state 27/27、LLVM/C memory lowering 2/2；本阶段
全量验证为 Python unittest 239/239、LLVM/CMake lit 92/92、完整 compiler build、
Python syntax 和 whitespace checks 通过。

F70 定向验证为 continuation state 30/30、LLVM/C pointer-size lowering 2/2；
本阶段全量验证为 Python unittest 242/242、LLVM/CMake lit 92/92、完整 compiler
build、Python syntax 和 whitespace checks 通过。

F71 定向验证为 continuation state 33/33、LLVM/C stack lowering 2/2；本阶段全量
验证为 Python unittest 245/245、LLVM/CMake lit 92/92、完整 compiler build、
Python syntax 和 whitespace checks 通过。

F72 定向验证为 continuation state 37/37、LLVM/C heap lowering 2/2；本阶段全量
验证为 Python unittest 249/249、LLVM/CMake lit 92/92、完整 compiler build、
Python syntax 和 whitespace checks 通过。测试包含 checkpoint lifetime marker、
释放后重分配、UAF、double-free、未初始化读取、dynamic malloc、interior free、
重复 site contract 和 recursive reentry rejection。

F73 定向验证为 continuation state 43/43、LLVM/C alias lowering 3/3；本阶段全量
验证为 Python unittest 255/255、LLVM/CMake lit 93/93、完整 compiler build、
Python syntax、whitespace 与 Markdown link checks 通过。测试覆盖 finite ITE
load、guarded store、overlapping multi-byte aliases、conditional heap
initialization checkpoint、dynamic/nested GEP C/LLVM frontend、signed no-wrap
index domain、alias limit、pointer/index-width mismatch、missing/duplicate address
contract 和 zero-scale rejection。

F74 新增 3 个 executor contract/semantic tests，并扩展既有 LLVM/C lowering tests；
本阶段全量验证为 Python unittest 258/258、LLVM/CMake lit 93/93、完整 compiler
build 通过。测试覆盖 pointer select/PHI cross-object load、guarded store、每臂
dynamic GEP、null arm domain pruning、union-wide alias limit、cyclic provenance
拒绝以及 malformed `alias_cases` contract。

F75 新增 2 个 executor pool/checkpoint/contract tests，并扩展既有 LLVM/C lowering
tests，加入 same-site multi-live、pool exhaustion 与 GEP-after-union；contract
测试同时覆盖 missing/reordered slots、过窄 pointer width 和缺失 explicit
capacity。最终验证为 Python unittest 260/260、LLVM/CMake lit 93/93、
`test_distributed_state.py` 48/48、完整 compiler build、Python syntax 与
whitespace checks 通过。

F76 新增 2 个 executor owner-frame/signature tests，并扩展 LLVM/C lowering tests，
覆盖 pointer argument/return/forwarding、caller-stack checkpoint、heap return、
callee-stack escape 和 caller symbolic-domain 拒绝。定向验证为
`test_distributed_state.py` 50/50、LLVM/C lowering lit 2/2；最终全量验证为
Python unittest 262/262、LLVM/CMake lit 93/93、完整 compiler build、Python
syntax 与 whitespace checks 通过。

F77 新增 exact/parent/cold context、warm/cold checkpoint resume 和 one-shot
ablation/circuit-breaker tests；定向 3/3、legacy QueryStore tests 13/13 和
persistent server/PSCache lit 1/1 通过。最终全量验证为 Python unittest
265/265、`test_distributed_state.py` 53/53、LLVM/CMake lit 93/93、完整 build、
Python warnings/syntax 与 whitespace checks 通过。

F78 新增 nullable heap/OOM/logical-bound/calloc/realloc/mixed-pool 6 个 executor
tests，并扩展两组
LLVM/C lowering tests覆盖 dynamic malloc、calloc、realloc 与 pointer-null
branch。定向结果包含在 `test_distributed_state.py` 61/61、LLVM/CMake lit 93/93
中，完整 compiler `-Werror` build、Python syntax 与 whitespace checks 通过。

F79 新增 caller-domain argument 与 pointer-return certificate executor tests，
并扩展 LLVM/C lowering tests 覆盖跨函数 symbolic GEP。完整 compiler build、
定向 executor tests 2/2、`test_distributed_state.py` 61/61、全仓 Python unittest
273/273、LLVM/CMake lit 93/93、Python syntax 与 whitespace checks 均通过。

F80 新增 bounded indirect-call executor test，并扩展 LLVM/C lowering tests 覆盖
function-pointer select/PHI 与真实 C。`test_distributed_state.py` 62/62、
LLVM/CMake lit 93/93、完整 compiler build、Python syntax 与 whitespace checks
通过；全仓 Python unittest 274/274。

F81 扩展 LLVM/C lowering tests覆盖 memcmp equality、lexicographic sign、
zero/null、pointer union 与 length-bound rejection；`test_distributed_state.py`
62/62、全仓 Python unittest 274/274、LLVM/CMake lit 93/93、compiler build、
Python syntax 与 whitespace checks 均通过。

F82 扩展 LLVM/C lowering tests覆盖 libc declaration 与 LLVM intrinsic 的
memcpy/memmove/memset、重叠 source snapshot、symbolic fill、zero/null 和
disjointness rejection；全仓 Python unittest 274/274、LLVM/CMake lit 93/93、
完整 compiler/runtime build 均通过。

F83 新增 LLVM/C pointer-returning indirect dispatch 与 artifact domain-contract
tests；F84 新增 guarded load、strlen/strcmp/strncmp、short-object union 和 input
logical-tail tests。两阶段合并后的全量验证为 Python unittest 276/276、
`test_distributed_state.py` 64/64、LLVM/CMake lit 93/93、完整 build 通过。

F85 扩展 LLVM/C lowering tests覆盖 memchr/strchr first-match、symbolic needle、
zero/null、short pointer union 与 finite pointer result provenance。F86 新增
guarded store runtime/domain test，并扩展 LLVM/C tests覆盖 strcpy/strncpy 的
source snapshot、terminator、padding、destination return、source union、
zero/null、overlap和 symbolic length拒绝。两阶段最终验证为 Python unittest
277/277、`test_distributed_state.py` 65/65、LLVM/CMake lit 93/93、完整
compiler/runtime build、Python syntax 与 whitespace checks 均通过。

F107--F129阶段最终全量验证为 Python unittest 284/284、LLVM/CMake lit 94/94、
完整 compiler/runtime `-Werror` build、Python syntax与`git diff --check`通过。
新增证据覆盖overflow/ssa.copy/objectsize、select/PHI/memory/cross-function
definedness、canonical address、linear CFG、typed return/argument ABI、最多64个
direct callsites、argument-to-return双向ABI组合、bounded all-use freeze sink、
finite multi-load memory version，
acyclic common-store branch memory proof，
direct-predecessor path-dependent memory definedness PHI，
argument/return双向interprocedural memory-PHI composition，
multilevel forwarding与exact-store loop memory state，
global initial/runtime poison-definedness merge，
以及malformed ABI
endpoint/mapping拒绝。
F120额外覆盖symbolic pointer roundtrip、runtime function-target overwrite和
initializer/runtime reaching-definition merge；F121覆盖poison/defined
multi-call passthrough与capability复检；F122覆盖multi-freeze与mixed-consumer
正负例；F123覆盖multi-load、exact clobber与旧memory路径回归；上述全量结果在
F129后重新执行，F124--F129另有diamond、mixed-clobber、edge tamper、双向call
ABI、multilevel、loop与initial-state正负定向证据。

F130定向验证通过LLVM 18 `-Werror` compiler build、constant-GEP aggregate
subobject lowering、`1,2,2` execution oracle、canonical/initial/subobject
capability复检、父合同tamper rejection与Python syntax检查。其全量回归结果记录在
后述F130--F137汇总中。

F131定向验证通过LLVM 18 `-Werror` compiler build、两迭代carry execution 1/2、
carry/cyclic capability与endpoint metadata复检、父合同tamper rejection，以及
mixed store/carry负例的defined-only不可行行为；结果纳入后述F130--F137汇总。

F132定向验证通过conditional edge-select execution `1,2,2`、conditional/carry/
cyclic capability和metadata复检、top-level标记tamper rejection，以及forwarded
arm负例；结果纳入后述F130--F137汇总。

F133定向验证通过forwarded conditional edge transfer `1,2,2`、multilevel/
forwarded-conditional capability与metadata复检、层级tamper rejection，以及
three-way join负例；结果纳入后述F130--F137汇总。

F134定向验证通过multi-arm equivalent-source transfer `1,2,2,2`、
multiarm/forwarded/conditional层级capability与metadata复检、tamper rejection，
以及distinct-store三source-group负例；结果纳入后述F130--F137汇总。

F135定向验证通过equivalent-defined-store transfer `1,2,2,2`、层级capability/
metadata与tamper复检，以及mixed defined/poison writer负例；结果纳入后述
F130--F137汇总。

F136定向验证通过shared-poison writer transfer `1,1,2,2,2`、writer-membership
修复、shared-poison capability/metadata与tamper复检，以及distinct-producer
负例。

F137定向验证通过two-level nested transfer `1,1,2,2,2`、eager-dominance gate、
inner/outer select artifact结构、nested capability/metadata与tamper复检，以及
four-source负例。

F130--F137阶段最终全量验证为Python unittest 284/284、LLVM/CMake lit 94/94、
完整LLVM 18 compiler/runtime `-Werror` build、Python syntax和
`git diff --check`通过。最终large continuation regression同时覆盖initial
subobject、pure carry、direct/forwarded/multiarm conditional transfer、
equivalent-defined/shared-poison writer groups、全部对应负例与artifact hierarchy
tamper oracles。

F138定向验证通过depth-3 recursive transfer `1,1,1,2,2,2,2`、4-leaf/unique-carry
tree contract、root-reachable validator和metadata tamper rejection；路径局部
producer负例不声明recursive capability。加入F138后的全量结果为Python unittest
284/284、LLVM/CMake lit 94/94、LLVM 18 `-Werror`增量构建、Python syntax和
`git diff --check`通过。

F139定向验证通过post-store fanout grouped recursive transfer
`1,1,1,1,2,2,2,2,2`、grouped capability/top-endpoint hierarchy和parent-marker
tamper rejection；普通F138正例显式拒绝grouped capability。加入F139后的全量
结果为Python unittest 284/284、LLVM/CMake lit 94/94、LLVM 18 `-Werror`
增量构建、Python syntax和`git diff --check`通过。

F140定向验证通过depth-4/five-leaf repeated-source transfer
`1,1,1,1,2,2,2,2,2`、repeated-source capability/top-endpoint hierarchy和
parent-marker tamper rejection；F139正例显式拒绝repeated-source capability。
加入F140后的全量结果为Python unittest 284/284、LLVM/CMake lit 94/94、
LLVM 18 `-Werror`增量构建、Python syntax和`git diff --check`通过。

F141定向验证通过depth-4/five-leaf/two-carry transfer
`1,1,1,2,2,2,2,2`、实际carry-leaf计数合同、multi-carry hierarchy和count tamper
rejection；重新生成的F140正例仍为单carry并显式拒绝F141 capability。
加入F141后的全量结果为Python unittest 284/284、LLVM/CMake lit 94/94、
LLVM 18 `-Werror`增量构建、Python syntax和`git diff --check`通过。

F142定向验证通过same-aggregate offset0/1双cell conditional carry transfer
`1,1,2`、两个独立contracts、multi-cell capability与孤立marker tamper rejection；
同cell双load负例执行同样结果但显式拒绝capability。加入F142后的全量结果为
Python unittest 284/284、LLVM/CMake lit 94/94、LLVM 18 `-Werror`增量构建、
Python syntax和`git diff --check`通过。

F143定向验证通过two-static-alloca conditional carry transfer `1,1,2`、
identified-object/multi-cell capability hierarchy与孤立marker tamper rejection；
F142同aggregate正例显式拒绝F143 capability，同cell负例同时拒绝两级capability。
加入F143后的全量结果为Python unittest 284/284、LLVM/CMake lit 94/94、
完整compiler/runtime build、Python syntax和`git diff --check`通过。

F144定向验证通过two-fixed-malloc-site conditional carry transfer `1,1,2`、
fixed-heap/multi-cell capability hierarchy与孤立marker tamper rejection；
F143 static-alloca、F142 same-aggregate和same-cell负例均拒绝F144 capability；
dynamic-size双malloc对照组保持guarded/infeasible并拒绝multi-cell capability。
加入F144后的全量结果为Python unittest 284/284、LLVM/CMake lit 94/94、
完整compiler/runtime build、Python syntax和`git diff --check`通过。

F145定向验证通过select与acyclic pointer-PHI两种two-by-two finite domain
conditional carry transfer `1,1,2`、finite-domain/multi-cell capability hierarchy
与孤立marker tamper rejection；cross-product存在重叠的对照组保持
guarded/infeasible并拒绝全部multi-cell capability，F143/F144正例不被错误升级。
加入F145后的全量结果为Python unittest 284/284、LLVM/CMake lit 94/94、
完整compiler/runtime build、Python syntax和`git diff --check`通过。

F146定向验证通过mutually-exclusive overlapping pointer domains的conditional
carry transfer `1,1,2`、guard-correlated/multi-cell capability hierarchy与孤立
marker tamper rejection；guards兼容的actual-overlap对照组保持infeasible且不声明
multi-cell，F145 select/PHI正例仍只声明all-pairs capability。加入F146后的全量
结果为Python unittest 284/284、LLVM/CMake lit 94/94、完整compiler/runtime build、
Python syntax和`git diff --check`通过。

F147定向验证通过edge-exclusive overlapping pointer-PHI domains的conditional
carry transfer `1,1,2`、PHI-correlated/multi-cell hierarchy与孤立marker tamper；
same-edge actual-overlap负例保持infeasible，F145 PHI与F146 select正例保持原
capability。加入F147后的全量结果为Python unittest 284/284、LLVM/CMake lit
94/94、完整compiler/runtime build、Python syntax和`git diff --check`通过。

F148定向验证通过symbolic-index interval-separated conditional carry transfer
`1,1,2`、interval/multi-cell hierarchy与孤立marker tamper；same-subarray重叠
负例保持infeasible，F145/F147正例不被错误升级。加入F148后的全量结果为Python
unittest 284/284、LLVM/CMake lit 94/94、完整compiler/runtime build、Python syntax
和`git diff --check`通过。

### F149：Byte-Lane Memory Definedness Composition

F149把memory poison sidecar从“一个同宽writer对应一个load”扩展为可审计的
address-order byte-lane domain。对2--8 byte、byte-aligned integer load执行最多
64-block的反向有界MemorySSA：每个lane选择最近的overlapping scalar writer，
已证明不相交的访问可跨越；若到达entry仍有缺口，只有typed constant global load
可把缺失lane补为initial。未知clobber、多前驱、循环、atomic/volatile和宽度外访问
均保持fail closed。

每个真正成为lane source且可能产生poison的store写独立一位sidecar；load把唯一
source sidecars做AND。这个结构既支持两个窄写组合一个宽load，也支持重叠overwrite：
后写只替换覆盖lane，旧writer仍负责其余lane。JSON
`byte_lane_memory_definedness`合同逐lane记录store identity、store-local byte、
store width和defined sidecar；production validator重建完整覆盖、紧邻store
sidecar、post-load AND dataflow与`initial`/`cross_block`标记，并要求
`bounded-byte-lane-memory-definedness` capability双向一致。

定向证据覆盖：

1. 两个跨block i8 writers组成i16 load，poison freeze返回`1,2`；
2. 一个i8 poison writer与global initial byte共同组成i16 load；
3. 两个实际defined的nsw i8 writers精确返回little-endian `1026`；
4. i16 poison writer被后续i8高字节写部分覆盖，lane0/lane1选择不同writers；
5. F149阶段branch两臂分别写不同byte的用例不声明直线capability，随后由F150
   的独立edge合同覆盖；
6. 删除lane与删除capability两种tamper均被production validator拒绝。

阶段验证为Python unittest `284/284`、LLVM/CMake lit `94/94`、LLVM 18 compiler
target build、`llvm-as`、Python syntax和`git diff --check`通过。F149的下一依赖
不是继续添加直线特例，而是在同一lane IR上实现direct-predecessor per-lane PHI，
随后再做cyclic lane carry和region-write source summary。完整alias-aware
MemorySSA与公开benchmark的R级证据仍未完成。

### F150：Direct-Predecessor Byte-Lane Definedness PHI

F150在F149的lane IR上增加path-sensitive edge transfer。位于2--64-way join中的
wide load对每个直接predecessor独立运行bounded reverse scan；每条路径必须完整
解释所有lanes，并且未覆盖lane只能沿unique-predecessor corridor继续追踪或由typed
global initial state补足。未覆盖lane若需要越过另一个join或cycle，证明返回unknown。

每个accepted endpoint在已有synthetic edge block中把该路径唯一的writer sidecars
做AND，并写入同一个`load__byte_phi_defined`。因此未执行路径的poison writer不会
污染当前freeze。`byte_lane_memory_definedness_phis` artifact记录merge、width和
逐endpoint lane vectors；validator复检store width/local byte、initial、sidecar、
edge-local AND/true dataflow以及末端jump。Accepted/rejected raw sink索引会在筛选后
重建，避免保守失败的candidate意外为poison producer提供sink资格。

定向结果包括：

1. poison+defined路径与全defined路径合流，symbolic edge/freeze得到`1,2,2`；
2. 两条路径分别只写低/高byte，其余lane由initial补足，得到`1,1,2,2`；
3. 删除一个endpoint lane和删除PHI capability均被validator拒绝；
4. 未覆盖lane必须穿越上游multi-predecessor join的用例保持infeasible且不声明
   F150 capability。

F150定向检查与compiler build通过；综合lit首次运行发现F149旧负例被新能力正确升级，
调整为initial-lane PHI正例。F151完成后统一回归结果为Python unittest `284/284`、
LLVM/CMake lit `94/94`及完整build通过。下一依赖是backedge上的cyclic lane carry
fixed point，再后续才是recursive conditional lane source tree和memcpy/memmove
region-source definedness。

### F151：Cyclic Byte-Lane Definedness PHI

F151完成了上述backedge依赖，但没有复用scalar cyclic memory-phi的一位state。
原因是partial overwrite要求分别保留未写lane的旧definedness和已写lane的新
definedness；aggregate carry无法从一位信息中恢复这个向量。实现因此为每个2--8
byte load分配有序`lane_defined`变量，并在每条入口/回边edge逐lane执行identity
transfer。Header numeric load后构造完整AND，再把aggregate defined bit交给freeze。

候选证明同时要求：

1. 单header、2--64个direct predecessors、每个endpoint只有header这一successor；
2. 每条路径的bounded reverse scan能把每个lane解释为nearest store、typed initial
   或遇到同一header load时的carry；
3. 至少存在真实partial source集合和poison-capable writer；
4. 函数内除合同writers和目标load外没有任何可能alias的memory access或未知effect。

第4项是soundness关键。旧前向sink分析在cycle处返回unknown，F151只为通过
alias-closure的store使用合同目标load作为sink；raw candidates筛选后，F149--F151
的store/load索引会整体重建，rejected proof不能残留。Production artifact
`cyclic_byte_lane_memory_definedness_phis`记录lane state与每条edge的
store/initial/carry vector。Validator要求每个lane destination恰好一次合法transfer，
carry读取自身、store读取匹配sidecar或true，并用dependency sets证明header AND
实际覆盖所有lanes。

定向结果为：entry完整i16 writer、回边poison i8低byte writer和高byte carry得到
freeze结果`1,2`；加入额外aliasing i8 load后保持infeasible且不声明F151。把artifact
中的carry伪造成initial或删除capability均被拒绝。LLVM 18 compiler build、
`llvm-as`、Python syntax、定向执行及完整`live_continuation_lowering.ll`已通过；
全项目结果为Python unittest `284/284`、LLVM/CMake lit `94/94`及完整build通过。

下一顺序依赖是conditional per-lane carry/source tree，其后是memcpy/memmove
region-source definedness。General MemorySSA、symbolic-overlap loop state和公开
benchmark消融仍是更高层研究缺口，不能由当前I/T级证据替代。

### F152：Conditional Cyclic Byte-Lane Carry

F152实现第一个有界conditional lane update，但刻意没有在latch生成
`select(store_sidecar, carry)`。Continuation executor会在构造ITE时求值两个operands，
skip路径上的writer sidecar并未定义，这种latch select会产生错误。因此实现把状态
更新前移到互斥的arm-to-join synthetic edges：

1. store edge仅对覆盖lanes写writer sidecar/true，其余lane self-carry；
2. carry edge对全部lanes self-carry；
3. join到header的backedge只转发已经更新的完整vector。

Proof scope要求一个branch的两个direct arms具有共同join，且只有一臂含唯一partial
writer。函数级alias closure继续排除额外alias access。Artifact新增
`conditional_cyclic_byte_lane_memory_definedness_phis`，显式记录branch、arms、
join、两个edges、polarity和lane vector。Validator不仅检查metadata，还重建真实
branch targets、四条jump关系、join流向header all-carry endpoint、完整non-carry
seed endpoint及两个edge上每条lane唯一赋值。

Symbolic write/skip基准得到`1,1,2`；carry arm加入aliasing load后常量write路径恢复
eager guard并不可行，不声明F152。翻转`store_when_true`与删除capability均被拒绝，
完整`live_continuation_lowering.ll`通过；全项目结果为Python unittest `284/284`、
LLVM/CMake lit `94/94`及完整build通过。下一项按依赖扩展forwarded arm corridor，
再处理multiarm和recursive conditional lane source tree。

### F153：Forwarded Conditional Cyclic Byte-Lane Carry

F153把F152的direct arms放宽为有界linear corridors。Lowerer从join的两个直接
predecessors反向走unique-predecessor/unconditional chains，最多16 blocks，并要求
两条chain命中同一nearest conditional branch的不同successors。Writer/alias扫描覆盖
整个corridor，而lane state仍只在最后的arm-to-join synthetic edges更新。

Artifact以独立
`forwarded_conditional_cyclic_byte_lane_memory_definedness_phis`记录branch targets
`store_successor`/`carry_successor`、实际edge endpoints、`forwarded=true`和逐lane
transfer。Production validator不信任这些声明：它从完整blocks图重建每个目标的
predecessor set，沿真实jump链从successor走到endpoint，并要求header predecessors
等于incoming合同、每个corridor block只有唯一期望parent、join predecessors恰为
两个synthetic edges。

Write/carry各经过两个forwarding blocks的正例得到`1,1,2`；write路径加入第二个
conditional导致tail多前驱时proof拒绝，常量write路径保持infeasible且不声明F153。
把`store_successor`伪造成endpoint或删除capability均被validator拒绝。Python
unittest `284/284`、LLVM/CMake lit `94/94`和完整build通过。

### F154：Multi-Arm Conditional Cyclic Byte-Lane Carry

F154把循环lane fixed point从单个Store/Carry选择扩展到一个严格两级三叶条件树。
Root branch的一条边进入第一个direct arm，另一条边唯一进入inner branch；inner
true/false构成另外两个direct arms，三者汇入同一join。候选证明要求恰有两个arm
各包含一个partial scalar writer，剩余arm为纯carry，且整个函数对目标load保持
alias closed。

两个writer arms分别在自己的synthetic edge上更新覆盖lane并self-carry其余lane，
carry edge对完整vector self-carry。Artifact
`multiarm_conditional_cyclic_byte_lane_memory_definedness_phis`记录root/inner
branches、route-labelled arms、edges和逐lane来源；capability为
`bounded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi`。

Production validator从blocks图重建两级branch targets、inner唯一入口、三个arms与
edges的精确predecessor sets、全部jumps和join三edge集合；同时核验两个不同store
IDs及其sidecars、一个all-carry arm、join-to-header carry、完整seed和header AND。
两个symbolic条件选择low-byte write、high-byte write或carry时得到
`1,1,1,2,2`。四臂树超过预算后不声明F154且常量poison路径infeasible；重复route
和删除capability均被拒绝。Python unittest `284/284`、LLVM/CMake lit `94/94`
和完整build通过。

下一依赖是forwarded multiarm与recursive per-lane source tree，再后续处理
memcpy/memmove region-source definedness。

### F155：Forwarded Multi-Arm Cyclic Byte-Lane Carry

F155把F154的三个direct arms分别放宽为最多16个unconditional、
unique-predecessor forwarding blocks。Lowerer从join endpoints反向寻找最近
conditional branch，两条path必须对应inner true/false，第三条对应root的另一边；
三条corridor互不相交，root到inner decision仍保持direct且唯一。

Writer扫描覆盖完整corridor，但lane状态只在endpoint-to-join synthetic edges更新。
独立artifact
`forwarded_multiarm_conditional_cyclic_byte_lane_memory_definedness_phis`
记录每个arm的branch `successor`、真实endpoint和edge，并声明`forwarded=true`。
Production validator从完整blocks图逐jump重建corridors、精确predecessor sets和
writer membership，再复用two-writer/one-carry、seed、backedge carry与header AND
不变量。

三条arms各经head/tail blocks的正例仍得到`1,1,1,2,2`；low arm加入nested branch
并形成multi-predecessor tail时不声明F155，常量poison路径infeasible。把
`successor`伪造成endpoint或删除capability均被拒绝。下一依赖是bounded recursive
per-lane source tree。Python unittest `284/284`、LLVM/CMake lit `94/94`和完整
build通过。

### F156：Recursive Conditional Cyclic Byte-Lane Carry

F156不再固定三叶形状，而是证明一个4--8 leaves、最大深度6、总节点不超过64的
strict binary source tree。候选root必须递归覆盖join的精确predecessor集合，每个
child只有当前branch这一parent；shared leaf、cycle、额外出口或多个完整roots均
fail closed。Leaves中至少两个包含不同partial writers，至少一个为纯carry。

状态仍只在leaf-to-join synthetic edges逐lane更新。Artifact
`recursive_conditional_cyclic_byte_lane_memory_definedness_phis`保存显式
branch hierarchy、root/join/depth和leaf lane vectors。Validator从真实terminators
重建树、重新计算最大深度和完整partition，再校验writer sidecars、edge transfers、
join、seed、backedge carry和header AND。

深度3的四叶正例使用三个poison writer leaves和一个carry leaf，三个symbolic条件
得到`1,1,1,1,2,2,2`。额外predecessor造成shared leaf时不声明F156，常量poison
路径infeasible；伪造depth和删除capability均被拒绝。Python unittest `284/284`、
LLVM/CMake lit `94/94`和完整build通过。下一依赖是forwarded recursive lane tree
及grouped/repeated lane sources。

### F157：Forwarded Recursive Cyclic Byte-Lane Carry

F157允许F156树的每条branch-to-child edge经过最多16个unconditional、
unique-predecessor blocks，不仅覆盖leaf arms，也覆盖decision-to-decision
corridors。全树intermediate blocks不可复用，总branch、leaf和corridor节点仍受
64-node预算约束。Leaf writer扫描覆盖其terminal corridor，状态更新仍只发生在
实际leaf endpoint edge。

Artifact以
`forwarded_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
区分F156 direct tree，保留真实branch targets、leaf `successor`和
`forwarded=true`。Validator从每个branch target逐jump找到下一个声明branch/leaf，
核验每个block唯一parent、全树corridor disjointness、实际depth与完整partition，
再执行writer、lane、join、seed和header AND验证。

Decision corridor和四条leaf corridors同时存在的正例保持
`1,1,1,1,2,2,2`；writer corridor含nested branch并形成multi-predecessor tail时
不声明F157且常量poison路径infeasible。伪造leaf successor或删除capability均被
拒绝。阶段全量回归为Python unittest `284/284`、LLVM/CMake lit `94/94`。
下一依赖是grouped/repeated recursive lane sources。

### F158：Grouped Recursive Cyclic Byte-Lane Source

F158允许一个internal conditional node在分支前执行partial writer，并让其严格
subtree的多个leaves共享同一defined sidecar。Compiler沿unique-parent tree证明
writer block是至少两个leaves的结构祖先，组内leaf不得再有override writer；树外
还必须存在独立leaf writer和carry leaf。

Artifact
`grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
设置`grouped=true`，组内lane vectors重复引用同一store ID。Validator从真实tree
children递归计算internal writer的完整descendant-leaf集合，并要求它与所有重复
store引用leaves精确相等；其他store IDs必须leaf-local且唯一。

Low-byte group writer覆盖两个leaves，另一子树含high-byte writer与carry的正例得到
`1,1,1,1,2,2,2`。两个internal group writers被compiler拒绝，常量第一组poison
路径infeasible；清除grouped marker或删除capability均被拒绝。阶段全量回归为
Python unittest `284/284`、LLVM/CMake lit `94/94`。下一依赖是repeated-source
recursive lane transfer。

### F159：Repeated-Source Recursive Cyclic Byte-Lane Transfer

F159允许F158组内至少一个descendant leaf用更近writer覆盖internal source，同时
至少两个其他descendant leaves继续重复引用上游source。当前证明要求override与
internal writer具有相同canonical base、constant offset和byte width；这是必要的
保守边界，因为只把整个leaf切换到override而不比较lane region会丢失未覆盖的
internal definedness。

Lowerer用结构祖先关系识别唯一internal writer group，逐leaf选择上游source或
leaf-local override，并在各自synthetic edge上读取实际执行writer的defined
sidecar。Artifact
`repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
设置`repeated_source=true`。Validator从真实branch tree重新计算internal source
descendant集合，以“完整descendants减去仍引用上游source的leaves”得到override
positions；每个位置必须有唯一leaf-local writer，且其store-lane集合与group source
完全相同。其他sources只能leaf-local且只出现一次。

五叶正例让low-byte internal source保留于两个leaves、在第三个descendant由同范围
writer覆盖，并在另一子树保留high-byte writer与carry，四个symbolic条件得到
`1,1,1,1,1,2,2,2,2`。Different-lane override负例被proof拒绝且常量poison路径
infeasible；清除marker或删除capability均被production validator拒绝。阶段全量
回归为Python unittest `284/284`、LLVM/CMake lit `94/94`。下一依赖是在同一个
leaf中按lane组合internal source、partial override与carry，而不是把leaf整体归给
一个writer；随后处理多个carry source groups。

### F160：Composed Repeated-Source Recursive Byte-Lane Transfer

F160把F159的leaf级替换提升为真正的per-lane nearest-source composition。一个
descendant leaf中的更近writer可只覆盖internal writer区间的一部分；lowerer逐lane
优先选择local override、再选择结构祖先source、最后carry。递归路径上的alias
closure、poison判定、sidecar注册、使用统计和edge赋值也全部改为读取
`lane.store`，因此同一leaf可安全组合两个writer，而不会把所有lanes错误绑定到一个
leaf级store。

Production artifact新增
`composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
及对应bounded capability。Validator为每个store/leaf重建lane set，要求至少两个
pure group leaves给出一致的完整group set；composed leaf恰有一个leaf-local source，
两者必须相交，且该leaf保留的group lanes精确等于`group_set - local_set`并非空。

i32正例的internal i16 writer覆盖lanes 0--1，leaf-local i8 writer覆盖lane 1，
artifact中的目标leaf为`internal, override, carry, carry`，五叶树执行得到
`1,1,1,1,1,2,2,2,2`。完全遮蔽group的负例保持infeasible且不声明F160；
marker/capability tamper均拒绝。下一依赖是一个递归lane tree中多个独立carry
source groups，再处理multiple internal writer groups。

### F161：Multi-Carry Composed Recursive Byte-Lane Tree

F161在F160 proof上允许2--8个互斥pure all-carry leaves，并把数量作为可验证合同，
而非仅保留“存在carry”的布尔标记。Compiler从无store的完整lane vectors计数；
artifact设置`multicarry=true`与`carry_leaves=N`。Validator重建tree与每个leaf，
独立重算all-carry数量，并要求其与2--8范围内的声明精确一致；普通F160合同则规范
为恰好一个pure carry leaf，防止类别歧义。

六叶正例包含两个group leaves、一个composed leaf、一个独立writer和两个carry
leaves，执行得到`1,1,1,1,1,1,2,2,2,2`。共享同一carry leaf的branch被
unique-parent proof拒绝且常量poison路径infeasible；计数和capability tamper均拒绝。
下一依赖是同一递归lane tree中的多个互不嵌套internal writer groups。

### F162：Multiple Internal Recursive Byte-Lane Groups

F162允许恰好两个direct、互不嵌套的internal writer groups。Compiler为每个leaf沿
unique-parent tree寻找ancestor group；若同时命中两个groups即拒绝。每个writer
必须对应至少两个pure leaves，两个完整descendant sets互不相交，树外保留carry，
且合同中不能混入第三个或leaf-local source。

Artifact新增`multiple_groups=true`和`group_count=2`。Validator独立识别两个重复
store IDs，分别计算store branch的完整descendant leaves并与引用集合精确相等，
同时核验两个集合disjoint、组内lane sets一致、tree/edge/sidecar/carry/seed/header
AND完整。

Low/high byte两个groups各含两个leaves、外加一个carry leaf的正例得到
`1,1,1,1,1,2,2,2,2`。嵌套group负例保持infeasible；group count与capability
tamper均拒绝。下一依赖是bounded mixed multi-group composition，再评估region
source与general cyclic MemorySSA的可实施边界。

### F163：Mixed Multi-Group Recursive Byte-Lane Composition

F163组合F160与F162：两个disjoint internal groups中，恰好一个group允许恰好一个
partial override leaf，另一个group保持pure。Compiler要求source集合为两个internal
writers加一个local writer；每组仍至少两个pure leaves，完全遮蔽与exact/partial
混合拒绝。

Validator分别恢复两个完整group lane sets，并要求唯一双source leaf的local lanes
与所属group相交、剩余internal lanes精确等于差集且非空。i32正例中第一组组合
lanes 0--1与lane-1 override，第二组覆盖lane 2，六叶执行得到
`1,1,1,1,1,1,2,2,2,2,2`；full-shadow负例和count/capability tamper均拒绝。
下一依赖是为grouped/repeated/multigroup tree证明bounded forwarding corridors。

### F164：Forwarded Grouped Recursive Byte-Lane Source

F164组合F157 corridor reconstruction与F158 grouped source proof。Single group
tree的decision和leaf edges均可经过最多16个unique-predecessor unconditional
blocks；validator逐jump恢复完整tree后，再证明internal writer的descendant集合与
重复source引用集合精确相等。

Decision与四条leaf corridors同时存在的正例保持`1,1,1,1,2,2,2`；nested branch
造成multi-predecessor endpoint时不声明F164且常量poison路径infeasible；
forwarded marker与capability tamper拒绝。下一依赖是forwarded same-range
repeated source和per-lane composed source。

### F165：Forwarded Repeated-Source Recursive Byte-Lane Transfer

F165组合F157 corridor reconstruction与F159 same-range override proof。Compiler
仅放开forwarded exact override，不改变partial-overlap边界；artifact独立声明
`forwarded=true`和`repeated_source=true`。Validator先重建所有unique-predecessor
jump corridors，再重算internal writer的完整descendant partition、pure lane set
和same-range override集合。

五叶正例在decision与leaf edges插入forwarding blocks后仍得到
`1,1,1,1,1,2,2,2,2`。Branching corridor/multi-predecessor endpoint负例保持
infeasible；两个marker与capability tamper均拒绝。下一依赖是forwarded per-lane
composed source，然后扩展到multi-group proof。

### F166：Forwarded Composed Repeated-Source Byte-Lane Transfer

F166组合F157与F160，并限制为一个pure carry leaf。Compiler在bounded corridors
上保留per-lane nearest writer选择；validator先恢复真实tree edges，再独立证明
pure group lane set、leaf-local partial writer与`group_set - local_set`非空差集。
Artifact同时声明`forwarded=true`和`composed_repeated_source=true`。

i16 internal source、lane-1 local override、独立lane-2 writer与carry构成的i32五叶
正例得到`1,1,1,1,1,2,2,2,2`。Branching corridor负例保持infeasible，两个
marker和capability tamper拒绝。下一依赖是forwarded pure/mixed multigroup tree。

### F167：Forwarded Multi-Group Recursive Byte-Lane Tree

F167组合F157与F162。Validator在恢复bounded unique-predecessor corridors和完整
tree之后，分别证明两个internal writer的descendant partition与store-ID引用集合
相等，并要求两组disjoint、lane sets一致且无第三source。

Low/high byte双group与carry构成的forwarded五叶正例得到
`1,1,1,1,1,2,2,2,2`；branching leaf corridor负例infeasible，marker、count和
capability tamper拒绝。下一阶段把其中一组扩展为唯一per-lane composed leaf。

### F168：Forwarded Mixed Multi-Group Byte-Lane Composition

F168组合F157、F160与F163。两个disjoint groups中仍只有一个group含唯一partial
override leaf；validator先恢复corridor tree，再分别证明两组完整partitions，并
对composed leaf重算leaf-local source与非空internal差集。Artifact绑定forwarded、
multiple-groups、mixed-groups和两个精确count markers。

六叶i32正例在group-entry corridor存在时得到
`1,1,1,1,1,1,2,2,2,2,2`；branching merge负例infeasible，marker/count和
capability tamper拒绝。下一依赖是forwarded multi-carry composed tree。

### F169：Forwarded Multi-Carry Composed Byte-Lane Tree

F169组合F157、F160与F161，在forwarded composed tree中接受2--8个distinct
all-carry leaves。Validator先恢复corridors与unique-parent tree，再从完整lane
vectors重算carry count，并复检partial override的internal-set difference。
Artifact绑定forwarded、composed、multicarry与精确count。

两个carry leaves的六叶i32正例得到`1,1,1,1,1,1,2,2,2,2`；branching merge
负例infeasible，marker/count和capability tamper拒绝。下一阶段重新审计三组、
多composed group与general cyclic MemorySSA，而不把未证明范围包装成已有能力。

### F170：Three-Group Recursive Byte-Lane Tree

F170在现有8-leaf预算内接受三个direct、pure、互不嵌套的internal groups。每组
至少两个leaves，validator恢复三个完整descendant/reference sets并检查全部
pairwise disjoint关系、组内lane一致性和精确source集合。Artifact声明
`triple_groups=true`与`group_count=3`。

三个i8 groups加一个carry的七叶i32正例得到十三状态；nested-group负例infeasible，
marker/count与capability tamper拒绝。下一阶段允许两个groups各自含一个受限
per-lane composed leaf。

### F171：Double-Composed Multi-Group Byte-Lane Tree

F171允许两个direct groups各有一个partial override leaf，并新增per-group计数，
避免全局count=2却集中在同一组的错误接受。Validator分别恢复两个pure lane sets，
要求两个distinct leaf-local IDs，各自证明非空`group_set - local_set`。

两个i16 groups及两个i8 local writers构成的七叶i32正例得到十三状态；`[2,0]`
same-group override负例infeasible，double marker/count与capability tamper拒绝。
下一阶段以统一schema表达2--3 groups、optional forwarding、composition和carry
count，停止为每个笛卡尔积单独增加artifact。

### F172：Forwarded Double-Composed Multi-Group Tree

F172组合F157与F171，保持两个groups各一个composed leaf的`[1,1]`计数与两个
internal-set differences，只增加bounded corridor reconstruction。七叶正例在
group-entry forwarding后保持十三状态；branching merge负例infeasible，marker和
capability tamper拒绝。

实现中由capability tamper发现并修复validator dispatch过早引用
`normalized_contract`的问题，新增路径现同时覆盖功能与错误报告稳定性。下一阶段
补forwarded three-group tree。

### F173：Forwarded Three-Group Recursive Byte-Lane Tree

F173组合F157与F170，使三个pure internal groups的每条逻辑tree edge均可经过
bounded unique-predecessor forwarding corridor。Validator先恢复实际branch tree，
再独立重建三个完整descendant/reference partitions并执行全部pairwise disjoint
检查；artifact同时绑定forwarded、multiple-groups、triple-groups与精确count。

七叶i32正例在第一组入口加入forwarding block后仍得到十三状态；branching merge
负例infeasible，forwarded/triple markers、count和capability tamper拒绝。下一阶段
完成现有8-leaf预算内最后可表达的三组partial-composition直接与forwarded变体，
随后收敛为统一schema并重新审计general cyclic MemorySSA边界。

### F174：Composed Three-Group Recursive Byte-Lane Tree

F174把mixed partition proof从固定两组泛化为三组，并在8-leaf预算内允许其中恰好
一组含一个partial-override leaf。Validator对三个完整member sets执行全对
disjoint，逐组验证pure lane set，再对唯一local writer证明leaf locality、相交
关系与非空`group_set - local_set`。

i16 group、lane-1 local override、两个独立i8 groups及carry构成的八叶正例得到
十五状态；同范围i16 full-shadow被识别为exact override并拒绝，marker/count及
capability tamper拒绝。本阶段保留direct-edge边界，下一阶段只增加bounded
unique-predecessor corridor reconstruction。

### F175：Forwarded Composed Three-Group Byte-Lane Tree

F175在不扩大八叶/六深度预算的前提下，为F174加入bounded unique-predecessor
corridor reconstruction。Validator先恢复actual tree，再执行三个完整partitions、
全对disjoint及唯一local-source difference证明；forwarded与composed-triple均由
独立artifact/capability双向绑定。

八叶正例加入group-entry corridor后仍得到十五状态；branching merge产生多前驱
terminal并保持infeasible，双类别marker、count和capability tamper拒绝。至此当前
8-leaf范围内的pure/composed、2/3 groups与direct/forwarded组合闭合；下一阶段不再
枚举同构artifact，而转向统一contract schema、general multi-cell cyclic
MemorySSA、cross-prefix solver context subsumption与可复现实验。

### F176：Validated Cross-Prefix Polyhedral Reuse

F176解除poly cache只能exact-key复用的限制。Runtime把cached/current全矩阵约束
规范化，并按equivalent、cached-subset、current-subset与compatible排序；这些关系
只缩小搜索，不承担soundness。跨prefix只复用SAT artifact，UNSAT仍严格绑定exact
key；每个外来model及其John/Dikin/integer-walk sample在写盘前都重新求值当前完整
prefix与target。

`SYMCC_POLY_CROSS_PREFIX_PROBES`提供有界扫描，六项telemetry分离relation召回、
有效命中和验证失败。Sampling profile、MPI helper与self-config已接入。Lit证明
`value<=2`生成的polytope可在不同key的`value<=3`prefix命中，同时旧负例继续证明
窄prefix UNSAT不能跨上下文剪枝。下一阶段应以公开benchmark测量hit/validity/
coverage-per-CPU，再决定是否加入变量投影或字段级重命名。

### F177：Verifier-Gated Optimistic Generator Simplification

F177把GenSlv式optimistic simplification落实为严格的proposal/verification双层。
Persistent helper从prefix/target载入边界构造只含target assertions的弱化solver，
在其上计算byte range和采样；原solver保留完整path condition，并逐个验证全部byte
assignment。弱化结果只进入候选生成域，`unknown`与不满足完整PC的model绝不保存。

Generator artifact新增版本化弱化证书，记录原query hash、完整kept/dropped断言
partition、蕴含方向与强制full-validation义务。Normalizer独立检查partition、target
保留和验证计数守恒。真实helper测试把完整`[8,10]`约束扩到`[8,255]`，观察到至少
两个候选被完整solver拒绝且所有保留model仍在`[8,10]`；关闭开关恢复精确范围。
下一阶段应在公开benchmark上量化valid-model ratio与coverage/CPU成本。

### F178：Native Z3 Tactic Model-Converter Generation

F178不再由Query IR手写逆运算近似Z3 converter。Helper对proposal goal执行
`simplify & solve-eqs`，枚举单一subgoal中的surviving byte variables，并通过
Z3原生goal model converter恢复被消去的输入。每个converted model仍返回完整
prefix solver验证；tactic超时、异常、多subgoal或空blocking set均局部降级。

新的converter artifact记录pipeline、query hash、subgoal/model数量和full-validation
漏斗，normalizer对pipeline、identity与计数守恒fail closed。真实测试在
`x=y+1, 8<=y<=10`上得到多个转换模型，全部维持原关系。当前未序列化Z3内部
converter对象；下一步科研工作是对native converter、syntax converter、range-only
与optimistic组合做等CPU消融。

### F179：Validated Cross-Size Polyhedral Projection

F179把F176从同长度坐标系扩到共享offset子空间。Cache loader按artifact自身
`input_size`保留旧矩阵；relation层对被删除byte的`[0,255]`贡献做per-row区间消元，
使用饱和`__int128`计算投影bounds。跨长度entry必须显式通过projection分类，不能因
共享子空间direct equivalent而绕开维度检查。

该投影是保守召回，不是SAT证明：只复用SAT model，UNSAT不跨prefix；外来model和
projected samples全部由当前完整prefix/target evaluator复检。两字节
`x/y` artifact已在一字节查询上投影命中`x=1`，关闭开关不产生projection计数。
后续只应在有公开benchmark收益时研究字段重命名或精确整数消元。

### F180：Bounded Structure-Preserving Polyhedral Field Renaming

F180为跨prefix SAT polytope加入有界变量置换。Runtime从线性约束超图提取变量的
系数/行出现签名作为确定性搜索顺序，在6变量默认预算、8变量硬上限内枚举双射，并
只接受重命名后可由现有矩阵relation分类器证明的候选。命中后同一映射同时作用于
full matrix、cached model和byte box。

该机制不把结构相似性当作SAT证明：只扫描同长度SAT artifact，不跨prefix传播
UNSAT，也不与维度projection组合；所有model/sample仍需通过当前完整表达式验证。
四字节lit已证明左侧`(x0,x1)`约束可复用到右侧`(x2,x3)`并产生
`00 00 01 00`，关闭开关后重命名计数保持为零。后续研究重点是公开benchmark消融
和可证明的width/endianness转换，而不是无界枚举字段置换。

### F181：Persistent Query-IR Converter Replay

F181修复了generator持久化链路中的执行缺口。`converter_chain`现在有严格的版本化
schema和invertible-step白名单；离线sampler既尝试单recipe，也累积无冲突recipe，
并把全部候选交给调用方verifier。QueryStore加载content-addressed Query IR，对
完整prefix和target roots执行有界解释，只物化通过验证的候选。

Manifest明确区分solver model与Query-IR-verified generator replay，并保留后续
concrete replay义务。两条direct equality累积测试及range generator的QueryStore
集成测试均已通过。Z3原生converter闭包仍是context-local对象；F181持久化的是独立
可审计的Query IR inverse recipe，而不是伪造Z3 converter序列化。

### F182：SMT-Validated Exact Integer Projection Relations

F182没有把整数投影错误简化成一个凸矩阵，而是采用双层表示：F179区间消元保留为
采样外包络，Z3则对cached/current有界Int系统分别存在量化被删除byte，并检查双向
蕴含和交集。由此得到精确的equivalent/subset/compatible/none关系；任何候选仍由
当前完整表达式复检。

变量、约束、单次超时和每分支probe均有独立上限，`unknown`回退区间路径。奇偶反例
`exists y. x=2y`与`x=1`已证明：精确层直接排除伪关系，关闭后外包络会产生一个随后
被validator拒绝的proposal。后续重点转向公开benchmark上的relation precision、
saved validations和coverage/CPU消融。

### F183：Cross-Size Endian-Aware Field Alignment

F183解除字段重命名的总输入长度限制。由于linear IR已把多字节字段编码为byte
coefficients，大端`256*x0+x1`与位于更长record中的小端`x2+256*x3`可通过
coefficient-preserving bijection建立结构证明。同一映射同时变换matrix、model和
box，最终仍由当前完整表达式验证。

两字节到四字节的端序/offset集成测试已命中`00 00 01 00`；关闭renaming后相关计数
为零。该机制只覆盖相同byte数量的置换，不声称实现width extension、bitfield packing
或语义schema推断。

### F184：Exact-Proof Widening Field Alignment

F184允许cached字段比current字段窄，但把精确投影设为强制证明门。系统搜索cached
bytes到current bytes的注入映射，随后存在量化未映射current bytes；只有F182精确
关系成功时才接受，区间外包络不能单独授权widening。Matrix、model和box使用同一
组合映射。

两字节big-endian artifact已复用到四字节little-endian `uint32_t`条件，生成
`01 00 00 00`并同时命中renaming/exact-projection telemetry。当前只覆盖unsigned
byte-aligned widening，sign extension和bitfield packing仍保守拒绝。

### F185：Exact-Proof Narrowing Field Alignment

F185闭合相反的宽到窄方向。Runtime从更宽cached系统中有界选择与current变量具有
约束签名或规范化系数证据的子字段，把其余cached bytes重写为synthetic存在量变量；
只有F182精确整数投影返回非空关系时才接纳映射。零结构重叠的任意排列不会交给SMT
猜测，区间外包络也不能授权narrowing。

四字节artifact中位于offset 2/3的低16位已成功重命名到两字节little-endian查询，
生成`01 00`；F182奇偶反例继续保持无relation，证明新路径没有绕过旧soundness
边界。当前width转换已覆盖可精确证明的unsigned byte-aligned widening/narrowing；
sign extension、bitfield packing、scale和非线性编码仍保守回退。

### F186：Conditional Contextual ParaSuit Parameter Graph

F186把原先丢弃`context`的参数posterior升级为机器schema驱动的条件策略。版本化
registry声明参数值、数值范围与`active_when`依赖，循环和未知parent拒绝；MPI任务
按program phase、targeted状态、结构任务和输入规模选择context-local value与
pair-interaction posterior。Reward只回写token实际采样的参数，不再更新全部registry
的unset值。

跨campaign state可作为随本地证据衰减的只读prior，但target内observations、
contexts与interactions保持从零开始。八项单元测试验证条件图、上下文隔离、交互
持久化和迁移边界。该阶段完成了ParaSuit机制级工程路径；公开目标上的等CPU消融与
论文结果复现仍属于R级证据工作。

### F187：Feedback-Validated Online Token Grammar

F187把静态token overwrite升级为在线、有身份、可反馈的grammar plane。Comparison
taint core先扩展到有界词法span，再学习literal/choice/sequence/optional/repetition
等production；grammar splice可改变输入长度。每个candidate携带stable rule ID，
真实target replay决定rule validity，全局AFL novelty retention决定rule coverage
yield，二者共同调度后续production。

状态artifact记录规则漏斗与grammar-rule coverage，规则数和span均有显式预算。新增
测试验证长token扩展输入、规则provenance和反馈恢复。剩余挑战是parser accept/reject
驱动的冲突拆分、CFG级nonterminal/hole，以及与Query IR partial model联合补全。

### F188：Fail-Soft Per-Query Selective Concolic Partitioning

F188在persistent Query IR solver内建立assertion-byte依赖超图，从target variables
做闭包，只把闭包外分量固定到concrete witness。该probe仍包含完整prefix/target，
因此SAT可作为精确模型；UNSAT/unknown/timeout没有证明权，必须pop并执行完整Z3。

集成测试既覆盖合法witness的`z3-selective`命中，也故意让witness违反disconnected
prefix，确认局部UNSAT最终仍回退得到SAT。变量、fix数量与短超时预算均进入F186
self-config schema。下一步研究差距是学习型solver-timeout predictor、FP/nonlinear
mixed symbolic/random partition和等CPU收益评估。

### F189：Persistent Cost-Aware Mixed Completion Policy

F189把F188的固定结构阈值升级为persistent contextual cost policy。Helper按query
断言规模、变量规模、target closure比例、fixed规模、AST规模和高成本BV算子分桶，
在线维护selective命中率及full/selective耗时EWMA。冷启动后按optimistic expected
saving决定attempt/skip，并从历史耗时收紧短probe timeout；完整Z3 timeout不受影响。

Disconnected concrete domain新增bounded completion portfolio：witness、zero、
`0xff`和query-seeded random assignment在同一总预算中去重探索。每次completion
都保留完整prefix+target，只有SAT有权返回；局部UNSAT/unknown/exception仍强制
full-solver fallback。Query service按worker/backend隔离版本化policy state，
result记录context、decision、prediction、EWMA、completion漏斗。

集成测试已证明all-`0xff` completion可命中、连续低收益后会learned-skip，且新helper
能恢复skip决策。G12在QF_BV byte domain内已具备dependency partition、mixed
completion、timeout predictor和在线成本决策；剩余科研任务收敛为FP/String/Array
domain-specific partition，以及在公开目标上报告saved solver time、false-local-
UNSAT fallback、coverage/CPU和策略校准误差。

### F190：Query-IR-Verified Grammar Hole Completion

F190首次把persistent Query IR模型与F187 online grammar放入同一验证链。QueryStore
candidate manifest携带从expression DAG重算的input read set、model assignment set和
实际evaluator结果；grammar层只处理不与query read set相交的lexical hole，并对每个
变长补全再次执行完整Query IR evaluator。Sidecar SHA、query ID、读集、hole和rule ID
进入版本化certificate，篡改读集不能授权补全。

通过Query IR的候选仍只是第一层：proposal manager要求真实target replay，最终是否
保留仍由AFL全局novelty决定。JSON示例中solver只约束首字节`{`，grammar把未约束
`"x"`补成`"LONGVALUE"`且保持Query IR成立；伪造sidecar和矛盾fake SAT均被新的
gate拒绝赋予verified身份。

这完成了G11“partial model + syntax completion”的非LLM、可审计baseline。下一依赖
转为history/plateau-triggered seed acquisition，以及parser accept/reject证据驱动
的nonterminal冲突拆分；完整Cottontail structural path representation与LLM生成仍
必须作为untrusted proposal plane单独评估。

### F191：Plateau-Gated Retained-History Acquisition

F191把“历史输入”从无差别fragment升级为只由AFL全局novelty授权的bounded seed
bank。每个retained seed以content SHA标识，记录coverage yield、proposal attempts、
target validity和再次retention；平台期按yield、验证率与探索bonus选取历史token，
splice到当前comparison span并携带`history_seed_id`进入原有验证漏斗。

触发条件使用authoritative coverage delta：连续零增益达到阈值前不执行history
route，任何正增益立即复位。State schema v4保存plateau与seed漏斗，测试覆盖阈值前
不生成、阈值后跨seed补全、target/retention反馈、重启恢复和新覆盖复位。

F191阶段G11已有online grammar、variable-length completion、Query IR partial-model
hole gate、真实target/AFL三层验证和plateau-guided history baseline；当时未完成的是
parser accept/reject驱动的nonterminal冲突拆分、ECT/context-sensitive structural
path统一表示，以及独立parser-validity oracle。

### F192：Independent Parser Oracle and Contextual Grammar Conflict Splitting

F192把语法有效性从目标程序的偶然接受中分离出来。用户可配置独立parser命令；它只在
真实target/branch gate通过后运行，退出码0才授权proposal继续进入AFL novelty triage，
非零、启动错误和timeout均保守拒绝。Target replay、parser validity和coverage
retention因此形成三个职责互不替代的证据层。

在线grammar为每个候选计算branch、输入长度桶及hole左右词法邻域组成的稳定context
ID。同一rule在同一context两次parser失败后只局部屏蔽；其他位置继续可用，后续成功
又会解除该context。Schema v5持久化parser漏斗和bounded conflict contexts，MPI只
回传本轮`last_reason`，避免重试时复用旧的累计acceptance。

测试覆盖独立进程oracle的接受/拒绝、两次context rejection、重启恢复与acceptance
重新开放；semantic/proposal/QueryStore回归和query lit均通过。G11下一步不再是添加
parser gate，而是把启发式context升级为parser-state/nonterminal与ECT structural
path统一表示，并评估额外parser调用的validity/coverage/CPU收益。

### F193：Exact String/BV Dual-View Query Contract

F193把原先“String模型再转byte”的弱桥接升级为同一SMT context内的显式双表示。
`symcc-string-query-v2`为每个String span同时声明逐offset 8-bit BV，精确链接字符、
symbolic length和first-NUL；BV equality/order可与contains/indexof/substr等String
predicate联合出现。V1继续兼容读取，所有model再由独立concrete evaluator复核两个
view。

该阶段同时修复两个可信边界：NUL变量现在必须在capacity内真实终止，fixed byte
string必须具有capacity长度；`str.contains` lowering从错误的
`contains(needle, haystack)`改为SMT-LIB规定的`contains(haystack, needle)`。显式
BV assignment在C++ helper中覆盖String C API序列化值，因此binary和内嵌控制字节
不会受UTF-8/NUL表示影响。

真实Z3 helper已经通过联合contains/BV和`41 01 42` binary literal，runtime也完成
重编译。G04下一步是把runtime artifact扩展到strlen/strchr/strstr与转换操作，并为
多个String backend建立exact concrete validation下的portfolio/disagreement机制。

### F194：Guarded Runtime String-Operation Query Artifacts

F194把F193的solver能力接到真实libc调用。新
`symcc-string-operation-v1`只记录可证明连续的direct-input C string span、first-NUL、
符号角色、常量侧和观察到的length/index；Python将`strlen`转成不同长度目标，把
`strchr/strstr`转成不同`indexof`目标。Operation row不作为patch使用，solver model
仍必须通过双view evaluator和正常coverage triage。

Runtime新增`strlen/strstr` interception并增强`strchr`。`strlen`在当前path内构造
first-zero ITE返回值；`strchr`加入found equality/not-found terminator；`strstr`
只在16384 comparison预算内生成BV contains，超预算不近似。真实插桩测试从三个
`ABC\\0` span导出length 3、miss -1、hit 1，三条都由真实Z3生成有效alternative。

G04接下来的实现顺序是conversion semantics、backend-neutral String IR lowering、
并行多backend disagreement/validation，再进入SymCC-str公开目标等CPU消融。

### F195：Validation-First Parallel String Backend Portfolio

F195提供最多8路并发String backend。`symcc-json`复用现有helper协议；
`smtlib` adapter追加`check-sat/get-value`，只读取F193链接的8-bit symbols，从而避开
不同solver的String model打印差异。每个SAT都先通过完整dual-view evaluator，
UNSAT只作观测，SAT/UNSAT disagreement不会形成剪枝或proof cache。

Materializer能接纳多个不同合法model，并把invalid SAT、duplicate agreement、
unknown/error和disagreement分开计数。MPI环境与离线CLI都支持JSON/文件配置。
Unit测试覆盖并发UNSAT/伪SAT/合法/重复模型与三种SMT-LIB BV值格式；真实双Z3 smoke
确认2 SAT被归为1 verified + 1 duplicate而非错误rejection。

F195阶段本机没有cvc5/Princess/Z3str3；F198后来已完成cvc5 1.1.2真实
parser/model conformance，Princess/Z3str3仍需固定版本验证。之后还需把backend成本、
validity与coverage reward接入在线选择策略。

### F196：Exact-Subdomain Decimal Conversion Semantics

F196加入第一个跨String/Integer conversion，但严格限制语义域：只处理1--10位、
无符号、无空白、first-NUL结束且结果不超过`INT_MAX`的ASCII decimal `atoi`。
Query同时要求`[0-9]+` regex与`str.to_int != observed`，所以无效字符串的SMT
`-1`不能冒充C model；runtime返回表达式是32-bit逐digit乘十加法，并带digit range
与terminator约束。

真实operation测试现在从`123\\0`导出observed value 123，Z3生成仍在decimal域内的
alternative。其余C atoi语义全部保守回退。下一conversion增量应以
`strtol(base,endptr)`的显式消费长度和overflow/errno contract为中心，而不是放宽
当前artifact的可信边界。

### F197：Width-Bound Signed strtol Contract

F197实现`strtol`的严格signed decimal子域：仅`endptr == NULL`、concrete base 10、
可选`-`、digits、first-NUL、无ERANGE且结果适配target `long`。Artifact绑定
`integer_bits`，query同时约束类型上下界和different value；F196的atoi也因此补上
`0..INT_MAX`范围，消除了合法observed query生成overflow candidate的漏洞。

Runtime在host overflow发生前用unsigned limit预检，在32/64-bit long宽度构建BV
decimal fold，并保存/恢复errno。真实测试证明base10/null-endptr会导出-123，
base16/non-null-endptr不会误导出。后续若扩展endptr，必须把消费offset和pointer
memory effect一并建模；不能仅增加返回值predicate。

### F198：Executable Cross-Solver String Conformance Gate

F198把“外部String backend可用”从配置假设变成可执行证据。固定矩阵覆盖binary
byte、String+BV联合约束、negative indexof、atoi和signed strtol10；每个SAT model
必须在独立concrete evaluator中再次成立，portfolio中的任一空结果、unknown、
UNSAT、不完整assignment或语义偏差都会使门禁非零退出。

真实cvc5 1.1.2对拍发现并修复`bv2int`、点式`str.to.int/str.to.re`和负数词法常量
四类方言依赖，统一为`bv2nat`、`str.to_int`、`str.to_re`和`(- N)`。内置Z3与cvc5
现均通过5/5 SAT和5/5 concrete verification。下一阶段仍需固定Princess/Z3str3，
并把conformance与公开目标的coverage/CPU消融分开报告。

### F199：Validation-Aware Contextual String Backend Selection

F199把F195固定全量portfolio升级为显式启用的预算化在线选择。稳定context由String
operation族、capacity桶、变量数和BV联合约束组成；global/context arm只把F193
concrete evaluator确认的SAT计为成功，并结合实际耗时、cold-start和周期探索选择
最多`max_backends`路。默认仍执行全部backend，UNSAT依旧没有剪枝权。

策略状态以`symcc-string-backend-policy-v1`原子持久化，POSIX文件锁保护MPI进程间
read-modify-write，context有界淘汰。测试证明重启后会继续选择低成本有效backend，
同时F198 conformance无条件绕过学习选择。真实Z3+cvc5在正常预算为一路时，门禁仍
对五类query执行十个结果并全部验证。下一步科研验证是公开目标上报告backend调用
节省、wall time、有效候选率和等CPU coverage，而不是仅报告bandit内部收益。

### F200：Width-Bound Unsigned strtoul and Overflow-Proof Decimal Folds

F200增加严格`strtoul(base10,NULL)`无符号十进制子域，artifact与query覆盖目标
32/64-bit `unsigned long`全范围，包括大于`INT64_MAX`的值；sign、base autodetect/
2--36、endptr副作用、whitespace、prefix、suffix和ERANGE均保守回退。

本阶段同时修复F196/F197的潜在跨输入错误：仅验证当前witness不溢出不足以保证
symbolic alternative。三种decimal wrapper现在每一步都约束
`acc < limit/10 || (acc == limit/10 && digit <= limit%10)`，所以BV fold不会进入
与libc不一致的回绕/饱和域。真实strtoul artifact、域外不导出、Z3/cvc5六case门禁
和重复lit执行均通过。下一步若支持non-null endptr，必须同时表示消费长度、指针写回
与errno状态，不能只扩展返回值范围。

### F201：Parser-State Structural Trace and Nonterminal-Aware Context

F201把F192的parser布尔oracle升级为版本化结构证据。配置命令通过`{trace}`写出
parser标识、verdict和有parent关系的bounded nonterminal/state byte spans；manager
验证exit/verdict一致性、树包含关系及candidate范围，并选择覆盖grammar replacement
span的最小节点。无合法enclosing node即使exit 0也拒绝。

Grammar状态现在按`(rule, lexical context)`持久映射到parser implementation与祖先
nonterminal/state链哈希，避免不同rule共享一个易覆盖的全局别名。测试证明拒绝
`field_value/after-equals`两次后只屏蔽对应结构context，且重启恢复；target未命中时
parser仍不会运行。下一阶段是把该trace与ECT structural path及rule-coverage feature
统一，而不是继续扩展词法邻域哈希。

### F202：Structural Rule-Coverage-Aware Grammar Scheduling

F202把F201结构context接入实际候选排序。只有通过target/parser并被AFL全局novelty
保留的proposal，才给对应`(rule, structural context)`累计coverage features；每个
span内以全局rule质量加`0.5/sqrt(1+features)`有限奖励排序，优先尚未覆盖的结构组合。
每rule最多保存256个context，重启恢复。

Retention API同时强制`retained < verified`，防止无验证证据或重复回灌破坏状态
不变量。下一步需要在公开parser目标上将structural novelty与global-only/random
rule policy做等CPU消融，并进一步把ECT node与独立grammar-rule coverage bitmap
纳入多目标调度。

### F203：Recursive Production Induction and Independent Grammar Bitmap

F203把F201的结构context进一步解释为可审计的parser production。验证器从最小
replacement node向祖先寻找唯一direct self-recursive child，以parser实现、
LHS/state、ordered RHS和terminal-gap digest形成稳定production ID；祖先超过32层、
sibling span重叠、多个recursive child或wrapper超过128 bytes均不归纳。合法
`A -> prefix A suffix`在parser接受后转成一次有界递归splice，parser拒绝只更新冲突
反馈，不会污染生成语法。

同时新增与AFL bitmap完全分离的64K稀疏grammar count bitmap，键为
`rule/path/production`，8-bit饱和并记录owner digest和碰撞。候选排序把其递减新颖性
与F202的AFL-retained结构收益并列使用；proposal v3和semantic v7状态在重启后保持
production alias、递归规则和bitmap。下一步G08不再是“是否支持递归”，而是mutual/
multi-slot recursion、完整nonterminal CFG、跨parser校准，以及edge/data/string/
grammar多目标Pareto策略的公开目标消融。

### F204：Bounded Multi-Slot/Mutual CFG Fragment and Derivation Search

F204把单wrapper扩展为canonical parser CFG fragment。验证器沿replacement ancestry
保存去重production、ordered RHS、terminal-gap digest，并分别识别direct same-symbol
slots和重复nonterminal组成的mutual cycle。`Expr -> Expr ',' Expr`的两个slot不会再
因“递归child不唯一”被丢弃，`A -> B -> A`也会保留完整symbol/state cycle path。

Semantic层重新计算全部production/cycle ID后才学习，状态v8保存最多4096个production/
cycle和一对多rule provenance。Recursive rule按默认depth 1--3生成独立candidate，
每层受input size和parser/target/AFL三层门禁；篡改fragment不能污染图。下一阶段聚焦
incremental ECT subtree correspondence、跨candidate production alignment，以及
edge/data/string/grammar的Pareto frontier，而不是继续提高无证据递归深度。

### F205：Incremental ECT Subtree Correspondence

F205把F204的digest-only production扩展成shape与concrete instance两层。Shape ID
固定parser、nonterminal/state和ordered RHS而忽略terminal bytes；instance ID绑定
production、完整subtree yield、gap bytes和root-to-node derivation path。只有真实
target与独立parser均接受后，semantic层复算全部ID与hash并合入持久图。

相同shape的不同yield形成可执行`subtree` alternatives，但仅在此前被parser认证为该
shape的source/candidate lexical context中参与排序。真实subprocess测试证明`A/B`
同形实例可跨candidate替换、错误branch context被预过滤、内部yield篡改被拒绝且v9
重启恢复。32层大fragment还验证了64 KiB下的确定性证据降级。下一步优先实现显式
edge/data/string/grammar/ECT多目标Pareto frontier；epsilon/ambiguity/SPPF和跨parser
校准仍保持为独立、需要更强oracle的研究问题。

### F206：Seven-Objective Pareto Grammar/ECT Scheduling

F206移除了grammar候选选择中的固定加权和默认路径。每个context现在对target validity、
AFL edge yield、data progress、concrete-verified String yield、grammar novelty、ECT
novelty和age做非支配排序；最多256个按各维极值轮转形成的arm进入精确front/crowding
计算。缺失data/String观测使用neutral prior，不能被解释成成功。

Worker把本次String solver artifact的query/verified计数并入proposal telemetry，规则
状态v10保存data/String反馈和last-attempt。Age维修复了局部parser冲突解除后可能饿死
的规则；`SYMCC_GRAMMAR_PARETO=0`保留旧scalar策略用于消融。下一阶段不再继续人工调
固定权重，而应扩展到corpus ownership/replacement的Pareto frontier，并按F67协议报告
hypervolume、coverage/CPU、invalid ratio和各目标贡献。

### F207：Epsilon-Pareto Corpus Metadata Archive

F207把多目标选择从grammar arm推进到seed replay metadata。Archive分别保留edge、
data、concrete-verified String、structural sites、path ownership和reward/cost；strict
dominance负责直接admission/replacement，容量冲突使用保护各维极值的epsilon-density
驱逐。Adaptive state v12持久化完整漏斗与eviction telemetry。

该功能有意不删除AFL queue文件：Pareto只影响SymCC调度元数据和bounded priority，
避免在distributed coverage ownership尚未证明线性化前破坏权威corpus。下一步若继续
推进物理corpus minimization，必须先把F43/F124的coverage claim与master epoch/fence
连接到archive decision，并用故障注入证明不会误删唯一owner。

### F208：Packed Parser Families and Verified Epsilon Derivation

F208把parser structural trace升级为可表达零宽归约和最多8路bounded alternatives的
v2协议。Alternative 0明确代表当前concrete parse；replacement context只沿该主树
定位，其他备选只形成规范化production family，从而避免packed forest节点劫持当前
上下文。Primary ancestry优先占用总计32条production预算，instance仍受32项和64 KiB
证据上限。

Semantic层对v3 fragment重新计算production/shape/instance identity，只允许
empty-RHS、empty-gap、empty-yield三者一致的epsilon实例学习删除规则。该规则必须与
source lexical context的selected ECT shape相交才会生成，且生成后仍接受target、
独立parser和AFL novelty门禁。Proposal/semantic状态分别升级v6/v11并保持旧版本兼容。
全量329项Python、4项subtest、原生build/lit与Z3/cvc5门禁通过。

下一阶段不应把这个bounded family层误称为完整SPPF。优先研究共享packed node/edge的
增量深层correspondence、nullable-cycle fixed point和多nonterminal同步substitution；
随后才加入PCFG/inside-outside概率与跨parser symbol calibration。物理corpus删除仍
依赖distributed ownership线性化证明，不能由F208绕过。

### F209：Shared Packed DAG and Deep Alternative Correspondence

F209把F208的single-parent alternative lists升级为真正可共享node的forward DAG。
V3 trace显式给出roots和packed child edges，alternative 0仍必须形成单父selected
tree；共享只允许存在于ambiguity evidence，不得改变当前replacement ancestry。完整
trace受4096 nodes/16384 edge occurrences/1 MiB限制，提取的v4 projection进一步受
32 nodes、128 edges、32 productions和64 KiB限制。

每个packed node/edge都有可重算identity，deep instance v2绑定node及完整root path。
Semantic v12复检DAG、selected path和全部内部hash后，才把非主深层yield加入ECT
shape class。真实共享DAG测试中，非主wrapper下的`value("b")`可在主树
`value("a")` context替换；错误branch、edge篡改和path驱逐均不能保留该能力。
Proposal v7和semantic v12跨重启恢复，332项Python与原生/双solver门禁通过。

下一依赖是nullable-cycle fixed point：当前forward index有意排除graph cycle，
epsilon只表达叶归约。应先对zero-width nullable dependency graph做SCC、最小yield和
深度/长度收敛证明，再允许nullable recursive family参与derivation；之后才实现多个
nonterminal同步substitution和packed-edge概率学习。

### F210：Nullable SCC Least Fixed Point and Proof-Carrying Deletion

F210把“观察到一个epsilon leaf”提升为bounded grammar-level nullability。V4 parser
trace在F209 forward DAG之外提供最多32条`(symbol,state)` dependency rules；manager
计算SCC和least fixed point，并在v5 fragment中携带规范化rule/proof/SCC certificate。
空RHS是depth 0基例，只有全部RHS已证明时才能传播，故有基例的互递归收敛，而纯
`A <-> B`不会被循环本身错误证明为nullable。

Semantic v13对certificate做独立精确重算，按parser增量合并规则并在重启、产生式/
实例淘汰后重新求固定点。只有nullable LHS/state对应的production shape，且source
lexical context已由selected tree认证为该shape，才能授权epsilon删除；packed ambiguity
本身仍无执行权限。Proof篡改、纯循环、错误context、restart与proof-backed shape
eviction均有回归。全量334项Python、4项subtest、原生build/lit以及Z3/cvc5各6/6通过。

下一项依赖是多nonterminal同步substitution：一个production中多个相互约束的slot必须
在同一derivation transaction中选择兼容ECT instances，并把完整slot assignment、
terminal gaps和source contexts绑定到certificate。单slot逐次替换会暴露parser-invalid
中间态，不能作为等价实现。其后再进入incremental parse cache与packed-edge
inside/outside概率学习。

### F211：Atomic Multi-Nonterminal ECT Transactions

F211已经把多槽替换实现为一个原子parent transaction，而不是依次投递多个单槽
candidate。系统从已复检的alternative-0 parent/child graph重建source yield，按child
shape索引accepted alternatives，并在严格预算内组合2--4个变化槽。Transaction绑定
parent production/instance、全部slot edges、source/target instances、terminal gaps和
result hash；已观察parent yield不会重复生成。

执行门同时要求parent shape context和source parent bytes精确匹配，所以transaction
不能在同shape的另一个source上退化成少槽变化。三槽集成fixture从`a,c,e`与`b,d,f`
得到`b,d,e`、`b,c,f`和`a,d,f`，每个候选均一次splice、改变两槽并保持分隔符。
Semantic v14把transaction视为派生状态，重启时从graph重算；proposal v9只保存其
provenance digest。全量335项Python、4项subtest、原生build/lit和双solver门禁通过。

下一阶段进入content-addressed incremental parse cache：需要定义input edit script、
受影响span/ancestor invalidation frontier、可复用packed subgraph以及完整parser结果
的一致性证书。缓存命中只能减少parser工作，不能绕过最终parser acceptance。

### F212：Content-Addressed Incremental Parser Cache Protocol

F212为外部parser增加`{cache}` manifest接口。Manager只缓存已经通过完整结构验证的
raw trace，按source input和parser command双重identity查找base；局部edit由最长公共
prefix/suffix精确描述，edit两侧16-byte guard内的node全部失效，稳定前后缀最多512个
node携带mapped span与yield hash供parser复用。

Parser仍须输出完整trace。可选reuse receipt的每个base/candidate node映射都会重新
检查manifest digest、symbol/state/span/epsilon和candidate yield；receipt缺失按full
parse fallback处理，伪造receipt拒绝。Cache file损坏则删除entry并cold parse，不影响
合法candidate。Proposal v10保存cache request/offer/hit/reuse/invalidation漏斗。
64-byte fixture验证cold、两node hit、重启hit、receipt tamper和corrupt-cache降级；
全量336项Python及原生/双solver门禁通过。

下一项是packed-edge probability与inside/outside调度。概率只能从独立parser接受的
forest observations更新，并需要Dirichlet平滑、context分层、截断质量单独计量；它
只能安排候选预算，不能把低概率production当作不可达或跳过验证。

### F213：Accepted-Forest PCFG Posterior and Inside/Outside Scheduling

F213已经把F209的bounded packed DAG从结构证据升级为概率调度证据。系统只计数独立
parser接受后、由root 0和alternative 0确定的concrete derivation；完整fragment
SHA-256负责幂等，8192项硬上限采用饱和停止而不是驱逐重计。每个
`(parser,LHS,state)` family使用`alpha=0.5`对称Dirichlet posterior，使已知但尚未
选择的production仍有非零探索质量。

概率推断不依赖parser局部alternative编号。全局hyperedge由node、production和ordered
children规范化，forward DAG上执行inside逆拓扑与outside正拓扑传播；显式root集合
解决内容寻址节点在不同forest中兼任root/child时的质量遗漏。计数和观测持久化，
inside/outside在重启或引用驱逐后从已复检图重算。

Grammar/ECT Pareto现在由七维扩展到九维，新增posterior likelihood和
information/reachability。二者只影响候选预算顺序，不能拒绝低概率rule，也不能绕过
target、parser或AFL novelty。3:1左右分支fixture得到0.7/0.3 posterior、root
inside=1和对应outside mass；重复、篡改、v14迁移与edge撤销均有回归。

下一项依赖是relation-aware synchronized slots。应从同一accepted parent内学习
slot-pair relation（相等、长度、tag/value和有限联合兼容类），以proof-carrying
certificate约束F211组合，但继续把最终parser replay作为权威。随后再比较普通PCFG、
hierarchical context model与ExplainFuzz式概率电路，并对真实tree-sitter/GLR cache
报告parser CPU、reuse ratio、perplexity、valid candidate/CPU和coverage/CPU。

### F214：Relation-Aware Synchronized Slots with Exploration Reserve

F214已把F211的无条件多槽笛卡尔组合升级为accepted-parent relation-aware调度。
系统只从能够用alternative-0 child edges、terminal gaps和primary instances逐byte
重建的parent采样；同一parent shape至少两个观测且零当前反例时，依次尝试byte相等、
十进制数值相等、数值/对侧长度和长度相等。每个slot pair只保留当前最强关系，新反例
到达时从可信图全量重算并降级或撤销，不把旧状态当作先验权限。

候选仍改变2--4个slot，但单parent最多枚举256个组合并输出四个。前三个按relation
满足率优先；只要存在非一致候选，第四个显式保留为exploration reserve。V2
transaction把relation ID和每条satisfied bit纳入canonical digest，执行端仍要求
source parent bytes、shape/context、target replay、独立parser和AFL novelty全部成立。
因此关系学习只节省预算，不会成为不完备的hard grammar。

本轮同时补上packed-instance到本地child-edge sequence的精确绑定。Semantic v16要求
v16状态中的`child_edge_ids`与production RHS形成完整有序双射；乱序、缺边、跨parent
引用或edge驱逐都会撤销依赖instance及派生transaction。V15仅在edge set唯一完整时
迁移。三组相等字段fixture验证support=3、三条conforming加一条exploration、状态
篡改重算和不等反例撤销。

下一项是conditional/hierarchical structural model：把当前pairwise硬谓词作为可解释
feature，与parent/ancestor context和F213 posterior联合校准，但继续保留无条件基线及
探索预算。进入概率电路前必须先定义训练/确认数据隔离、online forgetting、
out-of-distribution fallback和perplexity/valid-candidate/CPU/coverage-AUC联合消融。

### F215：Hierarchical Context-Conditioned PCFG

F215已在F213全局PCFG之上加入一级parent-shape/slot条件。Accepted concrete tree的
每个node同时贡献全局family selection和`(parent shape,slot)` context selection；
root使用无条件分布。非root后验以`beta=2`向全局Dirichlet posterior收缩，所以小样本
上下文既能表达偏好，也不会把未观察shape压到零。

由于一个content-addressed packed node可被不同parent复用，推断状态从`node`扩展为
`(node,context)`。最多32768个context states分别执行inside/outside，contextual
hyperedge质量进入已有九维Pareto的likelihood与information/reachability目标。无条件
PCFG始终存在；条件概率只排序，不改变parser acceptance、ECT authorization或AFL
retention。

计数路径也完成了一次正确性加固：root-0树必须有完整primary instance和本地
`child_edge_ids`闭包，任何缺口使整个fragment零计数。V17状态把context/count/
fragment witness分开保存，加载时用原子固定点对账，伪造一个selection会连同其
fragment的其他selection一起撤销，并在第一次重启后稳定。V16无条件迁移不会猜测
不存在的历史context。

八样本fixture让共享value family全局x/y为4:4，但两个parent分别为3:1和1:3；
hierarchical posterior得到0.5以及2/3、1/3，context forest、重启、篡改和fallback
均有回归。

下一依赖是prequential calibration与bounded multi-context circuit。需要先按时间顺序
用过去posterior预测下一accepted tree，报告held-out NLL、ECE、context sparsity和
fallback率；随后才把ancestor、sibling relation、coverage target作为有限feature
组合，而不是直接宣称实现ExplainFuzz完整概率电路。

### F216：Prequential Calibration and Drift-Safe Fallback

F216已把F215的训练内NLL与真正的online预测证据分开。每个accepted tree selection
在任何count更新前记录global/context selected count、total和known-shape数；因此
receipt能重算“用过去预测当前”的log loss，不会读取当前样本更新后的posterior。
同一fragment采用batch-before-update，duplicate digest不重复生成receipt。

每个context比较自身与F213全局PCFG的累计prequential NLL。至少两个事件且条件模型
累计获益时，才按样本量与gain把weight从0渐进提高；context-state inside/outside使用
全局与raw context posterior的凸组合。后续错误预测把gain拉回非正时，weight归零，
调度自动恢复全局模型。

V18状态只保存可重算receipt，不保存校准表或forest mass。恢复时检查event唯一性、
context/fragment membership、canonical ID和before-count严格早于最终count；坏receipt
只撤销调度证据。V17迁移即使拥有context counts，也因没有update-before证据而保持
全局effective posterior，避免把训练拟合误当作泛化。

下一项是recency-aware calibration：当前32768项窗口饱和停止且历史等权，能响应累计
drift但不能快速定位change point。应增加bounded epoch summary、指数遗忘或
ADWIN/CUSUM式告警，同时保证旧epoch驱逐不会把同一fragment重新计数；随后才进入
ancestor/sibling/target多feature circuit。

### F217：Recency-Window Calibration and Recovery

F217为新prequential receipt加入accepted-fragment index，并用最近16个fragment决定
context gate。窗口由全部accepted PCFG observations推进，不依赖某个context是否
出现或receipt是否还能写入；因此近期无证据会标记stale并恢复全局posterior。

Recent gain覆盖累计gain用于实际weight，但两套指标都保留。历史优胜、近期失准会
立即关闭；历史失准、近期连续优胜可以恢复。V18无序receipt继续使用累计模式，
V19有序receipt要求fragment-index双射和v2 digest，迁移不会伪造时间顺序。

阶段回归覆盖positive、recent adverse、stale和recovery四种状态。这个实现是固定窗口
baseline，不是ADWIN：下一步需要对log-loss difference做自适应切分，输出可复检的
cut certificate与误报控制，并与cumulative/fixed-16做等预算消融。

### F218：Bounded Adaptive Log-Loss Window

F218已把F217的有序receipt流升级为ADWIN-inspired自适应窗口。每个context先将同一
accepted fragment内的多个node receipt聚合为一个独立时间样本，检测量是裁剪到
`[-4,4]` bits的平均`log2(P_context/P_global)`。系统按时间重放最近最多64个
context-bearing fragments，候选切点两侧各至少8个fragment；每轮用
`delta/m`分配0.05显著性预算，并用有界两样本Hoeffding阈值选择正margin最大的切点。

检测到切点后，旧段从当前校准证据中撤销。实际posterior gate不使用裁剪值，而在
保留后缀上计算每fragment平均的原始global/context prequential NLL，因此一个大型
parse tree不会只因node更多就伪造校准样本量。F217的fixed-16 recent统计继续保留为
baseline和stale保险：最近16个全局fragment完全没有该context时，即使自适应历史仍
为正也回退全局posterior。

每次截断产生`symcc-parser-pcfg-adaptive-cut-v1`：绑定context、窗口、cut、两侧
fragment/receipt数、均值、Hoeffding epsilon、delta/m、clip bound和receipt-ID摘要。
公开验证函数从canonical receipt重算全部字段及certificate ID。Semantic v20写出
derived证书用于实验审计，但恢复时不信任该副本，而从v1/v2 receipt重新生成。

强positive、强adverse、再次positive三阶段回归产生disable/recovery和两张可复算
证书；平稳阶段不误切，epsilon篡改被拒绝。当前界是每次扫描内的多切点union bound，
不是anytime-valid无限horizon误报保证。下一研究步骤应先对
cumulative/fixed-16/adaptive做公开等预算drift benchmark，再把ancestor、sibling、
relation和coverage target组合为有界多context概率电路；任何高阶模型仍必须通过
prequential校准与独立parser/replay验证。

### F219：Grandparent Multi-Context Probabilistic Circuit

F219已在global与immediate-parent PCFG之上增加grandparent path expert。二级context
由`(grandparent shape/slot,parent shape/slot,child family)`唯一确定，只从完整
accepted primary tree提取。Grandparent posterior以`beta=2`收缩到parent raw
posterior；无二级样本时两者严格相同。

新update-before circuit receipt同时保存global、parent、grandparent三层充分统计。
其校准收益取grandparent相对global和parent的两个prequential gain中的较小值，所以
二级expert必须证明自己提供了parent之外的新预测信息。校准继续使用F218的
per-fragment聚合、自适应窗口与可复算cut，但证书采用独立circuit schema。

Packed-DAG推断状态扩展为`(node,parent context,circuit context)`。一个shared node经
不同grandparent到达时不再合并；alternative仍计算posterior与child mass的乘积，
同state仍求和。最终分布是parent effective posterior与grandparent raw posterior
按校准weight的凸组合，因此保持归一化，并在零/负/stale证据时精确退回F218。

真实fixture让parent value分布保持16:16，同时两个grandparent path分别形成14:2与
2:14；二级posterior达到5/6与1/6，双基线gate均启用。V21持久化context/count/
fragment receipt，但重算所有mass、weight与certificate；v20不推断不存在的历史
二级证据。

下一项应是ordered sibling autoregressive factor：在一个parent alternative内按slot
顺序把已选择的左sibling shape作为后续child expert条件，同时限制最大slot和状态数，
并用同样的update-before/双基线/adaptive certificate门控。只有grandparent与sibling
均通过独立消融后，才考虑更深ancestor、symbol-table或checksum factor。

### F220：Bounded Ordered-Sibling Autoregressive Factor

F220已经把F219的祖先条件扩展到同一parent alternative内的一阶有序兄弟条件。新
context由`(parent shape,current slot,left sibling selected shape,child family)`
确定；slot 0保持原分布。Sibling raw posterior以`beta=2`收缩到可用的最细
parent/grandparent raw posterior，最终只在独立校准weight下与F219 effective
posterior做归一化凸混合。

新update-before receipt保存global、parent、optional grandparent circuit和sibling
四层充分统计。Sibling robust gain取相对前三个模型收益的最小值，因此更细factor
不能靠重复祖先层已经解释的信号获得weight。漂移检测继续以accepted fragment为
独立样本、使用64-fragment bounded window和最小8+8 Hoeffding cut，并输出可从
canonical receipts复算的`symcc-parser-pcfg-sibling-cut-v1`。

推断不再把同一parent的children视为独立。Context-state v3把左兄弟shape纳入状态，
对每个parent artifact运行shape-indexed forward message；inside是全部合法有序
child derivations的质量和。Outside使用artifact级外部系数和反向suffix message，
分别把前缀、parent posterior与后缀质量传给每个child artifact，避免共享DAG上把
不同右侧条件错误合并。每一步保留previous/selected shape、child state/artifact与
inside mass，支持审计和复杂度计量。

真实32-fragment fixture固定parent与grandparent value分布为16:16，仅让左tag内部
production变化；两个sibling context分别学习14:2与2:14，得到5/6与1/6并通过三基线
gate。V22固定点恢复、count/receipt篡改和v21 fallback均有回归。独立48-event
positive/adverse/recovery序列产生两张可复算cut certificate。30项semantic、
全量346项Python与4项subtest、原生build、2项String lit、Z3/cvc5各6/6及静态门禁
全部通过。

下一研究阶段不应直接增加无界context。优先做四层等预算消融：
`global -> parent -> grandparent -> sibling`，在tuning/confirmatory隔离下报告
prequential NLL、fallback rate、factor transition数、parser CPU、
valid-candidate/CPU与coverage-AUC。只有结果证明局部一阶factor仍系统性欠拟合，才
按顺序研究bounded arbitrary-depth ancestor、non-adjacent sibling或
symbol-table/checksum expert；native tree-sitter/GLR incremental integration和
anytime-valid change detection仍是独立未完成工作。

### F221：Portable Signed-Integer Schedule SMT Evidence

补装cvc5 1.1.2后，research-evidence暴露出schedule SMT printer依赖Z3宽松词法：
reads-from初始源写成裸`-1`，而标准SMT-LIB整数项应写成`(- 1)`。这使同一个F58
SC/TSO/RA obligation在Z3中有结果、在cvc5中parse error，并让已安装第二后端的机器
反而无法生成有效证据。

F221把负整数序列化集中到公共helper，并覆盖SC/TSO domain与implication、RA
read-from/coherence、RMW、seq_cst和Query-IR RF-value bridge；research-evidence
附加outcome同步使用规范形式。RF模型和Python artifact仍使用整数`-1`，所以没有改变
memory-model语义或schema。Validator继续把已安装但报错的backend视为失败，未通过
忽略cvc5来规避门禁。

真实cvc5现对store-buffering SC/TSO/RA分别返回UNSAT/SAT/SAT，并与system-libz3
一致；schedule/evidence定向63项和全量346项Python回归通过。后续所有新增SMT sort
或operator都应经统一printer与至少两个真实solver的parse/oracle门禁，不能只依赖
Z3接受某种方言。

### F222：Fail-Closed Grammar-Hole QueryStore Lifecycle

全目录静态检查发现MPI master在semantic generator初始化时使用两个不存在的变量，
会在启用semantic proposals时产生`NameError`。修复后，master先统一解析
`SYMCC_QUERY_STORE`，但只有异步query service进程已经成功启动且grammar-hole开关
启用时才把store交给`SemanticProposalGenerator`。Workers为0或服务启动失败时显式
降级为无QueryStore的普通grammar模式，保留现有target/parser/AFL验证链。

同轮清理确认无引用的import与局部变量，使`ruff check util test benchmark`覆盖整个
当前工作树并通过。受影响定向套件127项、全量346项Python与4项subtest通过。后续应
增加可控MPI integration fixture，分别覆盖service成功、workers=0和启动失败三种
lifecycle；当前静态和单元门禁证明初始化闭合，但不替代多rank进程级故障注入。

Markdown 相对链接检查在当前权威文档中通过。只读历史快照
`docs/SymCC_Upstream_README.md` 仍含 8 个因从仓库根移动到 `docs/` 后失效的上游
相对链接；该历史快照未被本轮改写，也不作为当前文档入口。

### F223：Anytime-Valid Repeated Forward-CS Drift Audit

F223解决了F218--F220反复扫描时不能累计解释`delta/m`的问题，同时保留原有cut作为
低延迟运行门控。每个context-local fragment启动一条forward confidence sequence；
启动轮次和序列内查看轮次分别使用`6/(pi^2 n^2)`分配0.05预算。Clipped gain被映射
到`[0,1]`，每次查看使用双侧Hoeffding区间，CS取running intersection。两个相隔至少
8个fragment且各有8个样本的CS不相交时，输出proof-carrying anytime certificate。

在bounded constant-conditional-mean零假设下，双重预算对所有launch和look求和不超过
0.05，因此单个context在无限扫描时域和可选停止下的首次误报概率受控。这个保证不跨
context联合，也不假设自适应scheduler下条件均值必然稳定。代码把这两个限制写入
certificate；parent、grandparent circuit、sibling三类robust gain分别有独立schema
和从canonical receipts重算的verifier。

Semantic state升级v23并兼容v1--v22。Anytime证书仍是derived audit artifact；v22迁移
删除新字段后由validated receipts重建，不信任持久化证书。三类强漂移、稳定流、
预算求和、数值篡改和迁移均有回归；全量346项Python与4项subtest、Ruff、原生build、
String lit、Z3/cvc5 conformance与research-evidence均通过。下一项按依赖顺序转向
bounded non-adjacent/
long-range grammar factor；其新增context必须继续经过update-before prequential
比较、响应性gate和本次anytime审计，不能仅凭训练内likelihood启用。

### F224：Bounded Second-Order Sibling-History Factor

F224已经把F220的一阶ordered factor扩展为有界二阶history。Slot `i>=2`的child
同时以`shape[i-2]`和`shape[i-1]`为条件；raw history posterior以immediate-sibling
raw posterior为beta-2先验，最终只通过独立weight与F220 effective posterior混合。
这使系统能表示“紧邻bridge相同，但更早anchor决定当前value”的非相邻依赖，同时
在无新增预测证据时严格回退一阶模型。

Update-before receipt现在包含global、parent、optional grandparent circuit、
immediate sibling与history五层充分统计。Robust gain取history相对四个baseline收益
的最小值；响应性cut和F223 anytime证书分别使用独立history schema并从canonical
receipt复算。在线与重启统一按receipt ID确定性重放，消除了序列化排序造成的浮点
派生状态差异。

Context-state v4把forward frontier升级为最近两个selected shapes的tuple。Inside对
全部合法order-2 derivation精确求和；outside用tuple-indexed prefix/suffix message
传播artifact级外部系数。V24恢复验证前二/前一/current RHS shape、count/witness
固定点、五层before/final count与共享fragment-index双射，v23不猜测history evidence。

真实64-fragment parser fixture让global/parent/circuit保持32:32、每个immediate
sibling保持16:16，而四个history context得到14:2或2:14和5/6或1/6 posterior。
端到端测试覆盖tuple inside、outside、重启、count/receipt篡改及v23 fallback；独立
64-event测试覆盖四基线adaptive cut、anytime certificate与篡改拒绝。当前32项
semantic suite、全量348项Python与4项subtest、106项LLVM lit、Ruff、原生build、
Z3/cvc5各6/6 String conformance及双family research evidence全部通过。

下一阶段首先执行`global -> parent -> circuit -> sibling -> history`等CPU消融。
工程实现优先级转为native incremental complete-forest parser与跨context统计预算；
更远距离factor只在held-out prequential residual证明order-2仍系统性欠拟合后引入，
并继续要求有界状态、update-before证据、conservative fallback和独立parser/target/
AFL验证。

### F225：Cross-Context Online FWER Audit

F225补上F223明确留下的跨context多重检验边界。Parent、circuit、sibling和history
context按首次注册顺序进入append-only ledger；第`c`项获得
`0.05*6/(pi^2*c^2)`。Context内部继续对forward-CS launch和look各做一次逆平方
spending，因此所有context、launch和look的总alpha不超过0.05。

该选择比Online Fallback/ADDIS保守，但不要求共享grammar流之间独立或只局部依赖。
在每个真实null context内部满足bounded constant-conditional-mean时，三层union
bound控制任意context间依赖、无限扫描与可选停止下至少一个错误global certificate
的FWER。原响应性cut、PCFG weight和F223单context证书保持不变。

V25 ledger绑定kind/context、连续ordinal、first observation、精确alpha和不可回收
contract。Context失活不释放预算，32768项饱和后新context不再被global认证。
注册用的首条observation不进入global CS，使context alpha在被检验结果之前固定。
`symcc-parser-pcfg-global-anytime-cut-v1`同时绑定allocation prefix digest与四类
model-specific robust-gain evidence；统一verifier从canonical receipts重建所有三层
预算和interval。V25任一ledger缺失/篡改关闭global层；v24从validated ordered
receipts确定性迁移。

专项测试覆盖四种expert、257项外层预算、prefix篡改、真实parser重启、allocation
篡改fail-closed和v24迁移；32项semantic、全量348项Python与4项subtest、106项lit、
Ruff、原生build、双solver各6/6 conformance与research evidence均通过。下一步应把
global证书纳入confirmatory artifact schema，报告certificate delay、未使用alpha和ledger容量；
只有观察到可验证的local-dependence/conservative-null结构后，才研究ADDIS式预算回收。

### F226：Five-Level PCFG Confirmatory Evidence Pipeline

F226已经把`global -> parent -> circuit -> sibling -> history`从文档计划变为真实运行
档位。每一档不仅限制最终posterior，还停止更高层context、receipt、certificate和
factor-state构造；level 0不执行contextual inside/outside，level 3只保留order-1
frontier，level 4才启用order-2 tuple。这使coverage/CPU和状态成本差异具有机制解释。

MPI master现在输出带canonical SHA-256的`pcfg_research_artifact.json`，绑定
experiment/run/pair/configuration/seed/CPU budget、阶数与完整grammar snapshot。
Sealed protocol可以从一条base configuration自动扩展五档随机配对矩阵，并把成功但
缺失或错误绑定的工件保留为failed run。分析器验证工件后按run ID连接prequential
NLL、robust gain、certificate delay、alpha、context/receipt/state/transition与
coverage AUC，继续使用paired bootstrap、sign-flip、A12/Cliff和Holm校正。

定向semantic/protocol/analysis分别33/7/6项通过；全量351项Python与4项subtest、
106项LLVM lit、原生build、Ruff、全目录`py_compile`及`git diff --check`通过。
真实Z3与cvc5 1.1.2各完成6/6 concrete-verified conformance；重新生成的research
evidence v2含8项通过的obligation，SC/TSO/RA在两个solver family中无分歧。

下一步不再增加第三阶history。先用少量tuning排除解析器未触发、artifact saturation
和预算设置错误，然后封存至少20-repeat confirmatory manifest。判据是history相对
sibling同时改善held-out NLL或有效候选/CPU，并在coverage AUC上有一致方向；若只有
训练内NLL或状态数增长，则保持低阶默认候选。公开campaign尚未执行，因此F226当前是
I/T/E而非R。

### F227：Persistent Native Tree-sitter Incremental Parser

F227把F212的parser-neutral cache协议接到了真实Tree-sitter增量API。持久服务按输入
SHA-256保留native `TSTree`；局部candidate严格重建manifest edit，复制旧树后调用
`Tree.edit`和`Parser.parse(candidate, old_tree)`。服务只把跨old/new tree保持相同
native node ID的offered node写入reuse receipt，manager仍逐项复检symbol/state/span/
epsilon/yield hash，因此性能复用没有获得新的语法授权能力。

官方Tree-sitter 0.25.2与JSON grammar 0.24.8已在真实进程验证局部替换、Unix RPC和
完整`VerifiedProposalManager`链路；fake native tree覆盖坏edit降级、容量、拒绝树、
原子协议与ID映射。专项10项、受影响面60项、全量361项Python与4项subtest、107项
LLVM lit、原生build、Ruff、`py_compile`、双solver conformance与research evidence
验签均通过。该实现输出concrete CST v2，
不声称Tree-sitter能提供完整歧义SPPF；下一步是native GLR/GLL complete-forest
adapter和parser wall-time/reuse/coverage的sealed消融，而不是把CST备选路径虚构为
packed alternatives。

### F228：Proof-Carrying Parser Telemetry and Sealed Cost Ablation

F228把F227从“可运行”推进到“可证伪”。Proposal state v11累计manager wall time、
native reported parse time、trace bytes、request/offer/receipt/zero-reuse、
node-ID proof和reused/invalidated nodes；恢复检查严格counter lattice与64-bit边界。
`SYMCC_PROPOSAL_PARSER_CACHE=0`保留同一parser/trace验证但完全停止manifest/cache，
形成cost-faithful cold control。

Sealed protocol可从一条base生成`parser-cold/parser-incremental`等CPU矩阵。MPI
artifact绑定cache mode和实验身份；成功却缺失、篡改或跨run的artifact失败保留。
分析器按protocol run连接验签后的`proposal_parser_*`指标。专项已覆盖cache-off、
错误telemetry、真实Tree-sitter cost/proof、artifact round-trip/tamper、40-cell矩阵、
missing artifact和metric join。下一步必须执行公开目标campaign，同时比较manager
wall time、native time、valid-candidate/CPU和coverage AUC；仅有reuse rate不能支持
增量加速结论。受影响面77项、全量365项Python与4项subtest、107项LLVM lit、原生
build、Ruff、`py_compile`、双solver conformance与research evidence验签均通过。

### F229：Persistent Generalized Earley Complete-SPPF Adapter

F229关闭了“只有手写v3 fixture、没有真实完整歧义森林provider”的缺口。新增
`util/lark_sppf_parser.py`，以Lark 1.3 Earley的`ambiguity="forest"`直接取得SPPF，
并用`use_bytes=True`、`dynamic_complete`保留byte-accurate span和词法歧义。持久
Unix服务只编译一次immutable grammar；每次候选仍执行完整Earley parse。

编码不先枚举parse trees，而是保留Symbol、LR(0) intermediate、Packed production与
Token。每个Packed production成为独立wrapper，因此相同child list但不同rule不会
塌缩；非主路径共享仍保持DAG。Alternative zero按Lark稳定优先级形成concrete selected
derivation；若SPPF共享会使该projection出现多个primary parent，只在主projection
复制节点，满足manager的tree定位不变量。

Lark能表示零宽递归子森林，而trace-v3要求epsilon为叶。适配器对此提升为v4：折叠
零宽子图并输出全部不同nullable依赖，让manager重算SCC与least fixed point。循环
SPPF代表无限歧义，无法无损写入forward DAG；节点超过4096、边超过16384、单symbol
超过8个alternative、nullable rule超过32或trace超过1 MiB也全部fail closed，不把
截断结果标成complete。真实`aaa`二叉歧义、shared incoming edges、双epsilon、UTF-8
原始byte span、syntax rejection、非法/循环grammar、node cap、Unix RPC与
VerifiedProposalManager端到端已有8项专项回归。

该实现准确称为generalized Earley/SPPF provider，不称为native C GLR/GLL，也没有
incremental forest reuse。下一步应把`forest_telemetry`纳入proposal state与sealed
artifact，做Tree-sitter selected-CST、Lark complete-SPPF和关闭forest learning的
跨parser校准；之后才评估原生GLR/GLL或增量SPPF后端的工程收益。

### F230：Proof-Carrying Complete-Forest Telemetry and Calibration

F230不再把F229的`complete`与规模字段当作普通JSON备注。Manager验证
`lark-earley-complete-sppf-v1` proof、grammar SHA-256、Lark version，并从已经通过
v3/v4结构检查的nodes/rules重新计算encoded nodes、alternatives、edges和nullable
rules；同时检查accepted/complete、raw/clone/encoded关系、elapsed上限，以及Lark
forest与Tree-sitter incremental telemetry不能并存。任一不一致使整个parser trace
失败。

Proposal state升级v12并累计forest trace/complete/proof漏斗、raw/encoded nodes、
primary clones、packed alternatives、edges、nullable rules和Earley time；v1--v11
仍可迁移。Restore检查`complete <= proof <= trace <= parser validation`和全部
64-bit/cost关系。现有parser research artifact自动携带这些标量和complete rate、
mean raw/encoded nodes、mean edges/alternatives/time，artifact verifier重复检查
counter lattice。Artifact还封存规范化parser command digest和实际观察到的grammar
digest集合；state中缺失grammar identity或同一record中途切换grammar均fail closed。

新的`parser_forest_ablation`从一个base生成同CPU的`off/selected/complete`三档，
分别关闭parser、使用Tree-sitter selected CST、使用Lark complete SPPF。三档都关闭
增量cache以隔离语义因素，artifact metadata绑定forest mode；成功但缺工件、mode
错配、command/预声明grammar digest错配或非forest档声称forest trace的run记为failed。
当前专项覆盖telemetry edge篡改、
artifact proof篡改、state proof篡改、60-cell矩阵、mode错配和CLI expansion。

这项机制使cross-parser calibration可证伪，但没有消除grammar实现差异。正式实验
必须同时报告acceptance disagreement、valid candidates/CPU、manager/Earley time、
trace规模和coverage AUC；只有complete档相对selected档在这些条件下稳定获益，才有
理由继续开发native或incremental complete-forest后端。

最终验证为376项Python与4项subtest、108项LLVM lit、原生build、Ruff、全目录
`py_compile`、`git diff --check`和文档链接检查全部通过；Z3/cvc5 1.1.2各6/6
String/BV case concrete-verified，research evidence v2的8项obligation验签通过。

### F231：Candidate-Paired Cross-Parser Calibration

复审F230后发现一个实验设计错误：三个独立cell的candidate分布随parser gate和后续
feedback改变，不能把各自accepted总数拼成confusion matrix。F231因此加入
`cross_parser_oracle.py`，在同一proposal validation中并发运行complete-SPPF primary
与selected-CST secondary。Primary仍独占授权；secondary只是配对观测。

Wrapper绑定candidate、两个child argv、两个canonical trace、parser identity、
return code、accept bit和耗时。Manager把secondary交回原v1--v4 trace validator，
再复算所有摘要和等价关系。Proposal state v13记录both-accept、primary-only、
secondary-only、both-reject，验证四格总和与agreement对角线，并在artifact中封存
有序command pair。`parser_cross_calibration`生成同CPU、cache-off的
`selected/complete/paired`确认性矩阵；paired档还要求每个pair都有一个primary
complete-forest trace。

真实Lark daemon与独立selected fixture覆盖both-accept、secondary-only、primary
拒绝但校准仍计数、嵌套trace篡改、相同命令拒绝、restart和artifact。Protocol覆盖
60-cell矩阵、child digest binding、四格篡改和CLI。当前全量381项Python、109项
LLVM lit、原生build、Ruff/py_compile/diff/docs-link检查全部通过；Z3/cvc5各6/6
String/BV conformance与research evidence验签通过。

本轮官方能力审计没有找到可直接满足“原生+增量+完整forest”的现成落点。Bison GLR
没有通用SPPF输出；Tree-sitter增量API只暴露selected tree；Iguana暴露GLL/SPPF但需要
JVM且没有可核实的incremental forest更新承诺。下一步先执行小规模paired tuning，
根据`secondary-only/primary-only`、每侧耗时、forest规模和valid-candidate/CPU判断：
若语义分歧大，先统一grammar/lexer；若分歧小且Earley成本主导，再评估Iguana独立
GLL oracle或实现受限grammar的增量chart/SPPF复用。不能在这些证据之前宣称native
incremental complete-forest已完成。

### F232：Parser-Neutral Structural Correspondence Certificate

F232把F231的配对结论从accept/reject推进到结构层。Manager在两份trace都独立通过
v1--v4验证后，分别提取primary alternative-zero、primary完整forest和secondary
selected tree的非epsilon byte-yield span；同span的Lark Symbol/LR item/Packed
wrapper去重，并独立比较内部byte boundary。这一表示不要求两个grammar共享symbol
命名，适合作为跨parser对齐的第一层可证伪指标。

Proposal state v14累计两种primary span、secondary span、selected/forest intersection
与union，以及boundary交并。Restore、manager artifact verifier和protocol verifier
都复检subset与`union=left+right-intersection`，structural pair必须恰好等于
both-accept。Artifact导出selected与forest两套micro precision/recall/Jaccard和
boundary Jaccard。V13旧state保留接受象限但历史结构计数为零。

真实Lark与逐byte selected fixture得到非零shared span并通过所有集合恒等式；非共同
接受样本不进入结构分母，union篡改被protocol拒绝。下一步的实验判据顺序是：
acceptance四象限、boundary Jaccard、selected-span Jaccard、forest support/recall、
每侧CPU。只有前四项表明grammar语义足够一致，才解释性能差异或继续做symbol/
production mapping。

当前验证基线保持381项Python、109项LLVM lit和原生build全部通过；Ruff、
`py_compile`、diff/docs-link、Z3/cvc5各6/6 conformance及research evidence验签
也全部通过。

### F233：Independent GLR/SPPF Differential Oracle

F233增加第二个真实complete-forest producer。Parglare GLR直接暴露
`Parent -> possibilities -> NodeNonTerm.children`共享图；新daemon把该图编码为与
Lark相同的trace v3/v4，不调用solutions/tree iterator。Production和terminal各自
拥有wrapper，shared Parent保持共享，nullable/cycle/primary clone/topological与
全部资源边界复用F229编码器。

Manager新增Parglare provider schema/proof/version组合，但不放松结构授权。真实
二叉歧义、nullable、negative parse、cycle/bound、RPC和artifact测试通过；另外同时
启动Lark Earley与Parglare GLR服务，经F231双oracle和F232结构证书验证同一`aaa`
candidate，得到both-accept与非零forest shared span。

这个实现把“complete forest是否只是Lark特例”变成可执行differential experiment，
但Parglare同样是pure Python且每次完整reparse。下一步应先对同一语言的等价Lark/
Parglare grammar做双向paired tuning；只有acceptance与boundary/span证据一致时，
才把两者cost差解释为Earley/GLR算法差异。

最终验证为388项Python、110项LLVM lit、原生build与全部静态/依赖/文档门禁通过；
Z3/cvc5各6/6 conformance和research evidence验签保持通过。

### F234：Proof-Carrying Symbol/Production Correspondence

F234把F232的无名称span证据推进到保守grammar关系恢复。双方共同接受后，manager
透明展开provider-specific packed/item/token wrapper，对真实symbol计算包含byte
extent、有序相对child extent与递归child摘要的parser-neutral shape。一个symbol
pair只有在同一`span+shape`桶两侧均唯一时才计数；仍有多解的桶只增加ambiguity，
不按名称、node index或遍历顺序猜测。Production pair还要求去wrapper后的非空
direct-child partition完全相同。

Proposal state v15保存每个record的canonical correspondence JSON及摘要，并检查
observation sum、mapping唯一性与`production <= symbol <= 两侧semantic node`。
Sealed artifact输出带parser identity、两侧label/state、shape摘要和support的聚合
mapping表；manager与protocol verifier各自复检。真实Lark/fixture已产生symbol
mapping，真实Lark/Parglare歧义grammar同时产生symbol与production mapping；同span
同shape多解、1500层深链、state/artifact篡改均有回归。

这不是CFG语言等价证明。下一步确认性实验应先以holdout candidate检验mapping
稳定性和ambiguity rate，再考虑把高support mapping用于grammar差异诊断；不能把
观测对应直接变成候选授权。原生/增量complete forest仍是独立性能缺口：libmarpa
公开C API不提供packed bocage节点遍历，Tree-sitter只提供selected incremental tree。

当前验证基线为390项Python；最终门禁继续复核110项LLVM lit、原生build、静态检查、
Z3/cvc5 conformance、文档链接和research evidence。

### F235：Bounded Exhaustive Parser-Language Differential

F235把“采样中没有差异”升级为精确定义有限域内的穷举结论。新工具按shortlex枚举
指定byte alphabet上长度0到N的全部字符串，域最多65536个case。每个case都经过F231
双parser并发wrapper和manager v1--v4独立trace复检，而不是只比较退出码。

Artifact封存规范alphabet、精确域基数、四象限、command digest、全case transcript
digest和shortlex最小反例。`--require-equivalent`可作为CI gate；普通模式保留差异
artifact。Fake parser的7-case一致/不一致域、篡改和bounds均有回归；真实Lark/
Parglare在`{a}^{0..3}`上完成4-case一致审计。

真实审计还发现并修复Parglare 0.21.1拒绝空输入时错误消息构造抛`IndexError`的问题。
兼容路径严格限定空candidate和精确异常文本，其他内部错误仍fail closed。

一般CFG等价不可判定，F235只证明artifact声明的有限域。下一步若要提高证明强度，
应针对regular/deterministic/其他可判定子类接入理论equivalence checker，或实现
bounded CFG SAT encoding；扩大alphabet/length但无法穷举时应转入coverage-guided
grammar differential testing，不能继续使用“exhaustive”标签。

最终393项Python、111项LLVM lit、原生build、Ruff/py_compile/diff/pip和9份权威
文档链接检查通过；Z3/cvc5各6/6 conformance与research evidence验签通过。

### F236：Proof-Carrying Assumption-Conflict PSCache

F236重新按PSCache原论文Algorithm 2--4核对G07。原方法在CDCL冲突时保存输入bit
投影并把可满足的候选挂到prefix/off-path约束；本项目此前只有最终SAT model cache。
由于实际helper链接系统Z3而非vendored source，本轮没有写一个不会进入binary的私有
SAT patch，而是在稳定assumption API上实现语义等价、可独立复核的冲突采集层。

当前witness和失败cache probe都以输入byte等式作为assumptions检查完整query。
UNSAT后必须得到非空assumption core；删除式最小化在预算内证明每个保留byte是否
必要，最后再次验证core UNSAT。固有UNSAT公式会缩到空core并被拒绝。版本化结果记录
完整assignment、core assignment、来源、proof、minimality和check数。

QueryStore再做第二条可信链：严格规范协议和certificate hash，然后用Query IR证明
该assignment确实与源query冲突。新增signed-literal倒排索引把嵌套逻辑非规范化为
base expression与polarity，使正prefix和off-path反分支可参与同一相关性排序。任何
复用候选仍需完整目标Query IR验证，不能把conflict core当成新query的UNSAT proof。

真实Z3 helper已覆盖单byte最小core、后续SAT cache hit与assignment-independent
UNSAT拒绝；QueryStore覆盖conflict candidate落盘及空core、错值subset、false proof、
offset alias篡改。下一步科研工作不是再扩大声明，而是做G07同CPU消融：SAT-model-only、
witness-conflict、cached-miss conflict、core budget及full组合，报告core size、
retention/hit、lookup CPU、candidate replay validity、solver time和coverage/CPU-hour。
直接CDCL trail采样仍是独立可选后端研究，必须先构建并版本锁定自有Z3，且与F236
backend-neutral路径对拍。

### F237：Proof-Carrying Backend-Neutral QF_BV Portfolio

F237补齐G06此前“能race helper、但没有异构语义层”的缺口。新的
`smtlib-qfbv` backend不读取Z3打印的SMT文本，而是从QueryStore的content-addressed
expression DAG重新构造标准QF_BV。递归lowerer对每个节点验证operator、arity、
Bool/BV sort、result width、extract/extension范围、8-bit input协议和Bool root；
超出显式capability matrix的query在外部进程启动前返回unknown。

版本化capability与lowering证书绑定operator集合、资源上限、精确operator计数、
input集合、最大位宽及最终SMT-LIB摘要。QueryStore不仅重算摘要，还检查计数总和、
advertised operator和预算关系，因此修改字段并重算外层hash仍不能把不一致证书
写入结果。Variable rotate以标准BV组合实现，不依赖Z3方言。

SAT模型通过标准`get-value`提取全部input byte，有界S-expression parser兼容
hex/binary/decimal BV。Adapter先用当前witness做完整Query IR复核，store再从CAS
独立加载表达式做第二次复核；错误或缺失模型只得到unknown/error。UNSAT授权是
capability中的显式选择且默认false，未授权UNSAT只保留raw telemetry。Portfolio
继续在任何SAT/UNSAT disagreement时返回unknown。

真实cvc5 1.1.2已经通过全部38类Query IR operator组成的单查询matrix并恢复
`0x42,0x03`，另有SAT双验证、UNSAT trust差分、pre-launch capability rejection、
模型和证书篡改等共7项定向测试。F237阶段环境没有Bitwuzla executable，所以当时
不能把其标准CLI适配配置记作实机结果；该历史缺口已由F244固定0.9.1实机证据关闭。

下一阶段G06按以下顺序推进：

1. 在固定版本容器安装Bitwuzla，运行与cvc5相同的operator/model/UNSAT对拍；
2. 把Query IR operator histogram、width/node/input规模加入F239 feature contract，
   并做向后兼容的schema v2迁移；
3. 在equal-CPU公开query corpus比较静态序列、旧在线split、F239 X-means/BIC prior；
4. F240/F241已实现bagged/boosted cost surrogate、dataset-level beam及严格预算
   expected-improvement SMBO；下一步执行cost-matched holdout对拍；
5. 实现可取消的running attempt及native/cross-worker context transport。

F237是可执行的异构solver基础层，不是SMTgazer效果复现。正式结论仍需Z3-only、
cvc5-only、sequential/parallel组合和不同UNSAT trust配置的同CPU多轮消融。
最终回归为402项Python与112项LLVM lit；原生build、Ruff、全目录`py_compile`、
`git diff --check`、依赖闭包、Z3/cvc5各6/6 String/BV conformance和research
evidence验签全部通过。

### F238：Prefix-Keyed Persistent SMT-LIB QF_BV Contexts

F238把F237的异构QF_BV路径从one-shot推进到真正的prefix增量协议。每个exact
`prefix_key`对应一个interactive solver process，prefix roots只assert一次；target
在独立push frame中check/model后pop。新target-only input只增加declaration，
不会修改prefix assertions。重新lower出的prefix term digest必须与context一致。

Pool按1--64个process做LRU；这比一个process反复reset更耗内存，但隔离了每个prefix
的assertion stack，也避免solver方言相关的context snapshot。`echo` marker为多行
响应提供request framing，8 MiB输出预算和外部deadline防止失步或无界输出。
Timeout/EOF/broken pipe会销毁单个context，后续请求只能cold rebuild。

Capability合同新增`incremental=true`，persistent command禁止file/timeout
placeholder；query service通过worker `ExitStack`关闭process pool。结果记录
`smtlib-prefix-process-push-pop-v1`、cache hit和entry数。F237的sort/lowering证书、
两级SAT模型验证、显式UNSAT授权和disagreement隔离全部保持不变。

真实cvc5在相同prefix的第二个target上产生cache hit；容量1时切到另一个prefix会
驱逐旧process，切回后严格cold miss。Fake solver首个check挂起后被deadline杀死，
cache归零，同一lease冷启恢复成功。下一步要在公开query corpus上测量prefix
redundancy、hit、RSS、parse CPU和coverage/CPU-hour，再决定是否值得实现native
cvc5/Bitwuzla多context API或跨worker clause/context transport。

最终门禁为404项Python、112项LLVM lit；原生build、Ruff、全目录`py_compile`、
`git diff --check`、依赖、文档链接、Z3/cvc5各6/6 String/BV conformance和
research evidence验签均通过。

### F239：Proof-Carrying X-means/BIC Censored Sequence Prior

F239纠正了旧文档中“online X-means-style”容易被扩大解释的问题。旧调度器仍保留为
低开销moment cluster和Thompson arm；新增离线训练器保存实例特征，执行确定性
two-means refinement，并只在spherical-Gaussian BIC严格改善时接受局部二分。最大
cluster、最小cluster样本、Lloyd迭代和artifact大小均有显式边界。

训练目标把明确超时/kill作为右删失失败并收取PAR-2 `2T`，其他cost截断到`2T`。
每个context/action保存inverse-propensity充分统计量，以全局action estimate收缩，
再用ESS和LCB共同决定是否建议。无统计支持时输出空建议，避免把探索噪声升级为
系统默认。轨迹同时新增unknown ratio、branch pressure和明确`timed_out`，与在线
九维context对齐且兼容旧记录。

`symcc-smt-sequence-policy-v1`不只带SHA-256：验证器利用封存的一、二阶加权矩独立
重算ESS、PAR-2、局部/收缩utility、标准误、LCB与support；利用cluster size/SSE
重算每次parent/child BIC和最终BIC，并核对样本、删失、behavior及recommendation
守恒。在线scheduler只在没有显式offline gate建议时将最近verified centroid建议
作为90/10 preference，继续保留探索概率和多stage progression。

新增7项测试及500-event/8-cluster压力构造；完整门禁为411项Python、113项LLVM lit。
F240已继续实现boosting/bagging surrogate与dataset-level budgeted beam，但F239本身
仍只是单action prior。Bayesian acquisition、Bitwuzla实机和equal-CPU性能证据仍是
后续独立工作。

### F240：Bagged/Boosted Budgeted Sequence Optimizer

F240把F239的“每context一个action”推进为共享timeout内的可执行sequential
portfolio。新工具为每个action训练completion、PAR-2 ratio和adjusted reward三类
模型；bagging使用稳定bootstrap shallow CART，boosting使用残差树与shrinkage，
并以bag方差和residual RMSE给出保守uncertainty。实现只依赖Python标准库，所有树深、
数量、leaf、threshold和训练事件均有硬边界。

Optimizer在整数秒slice和不重复action上执行bounded beam search。每个候选按
completion LCB、cost UCB、reward LCB推演reach probability和PAR-2尾罚；multi-action
只有严格改善best single才被采用。它同时输出cluster-weighted dataset schedule与
per-cluster localized schedules。Artifact嵌入F239 base proof，验证器从sealed tree
和centroid重新执行搜索并复核全部目标字段。

在线scheduler加载同一路径下的F239或F240 schema，保持90/10探索。F240命中后跨replay
推进action schedule，但先完成action内部原有stages；stage budget通过
`SYMCC_ALGORITHM_BUDGET_SEC`进入worker实际timeout。轨迹新增input SHA-256 identity、
solved和budget，删失PAR-2按stage budget计费。

5项新optimizer测试和1项trajectory协议测试覆盖互补schedule、保守single退化、
篡改拒绝、CLI与真实调度推进；完整基线为417项Python、114项lit。该搜索仍是beam，
不是Bayesian acquisition；下一个独立工作包是schedule-level SMBO/EI及其
cost-matched ablation。

### F241：Bounded Expected-Improvement Schedule SMBO

F241在F240封存的action ensemble之上增加schedule-level sequential model-based
optimization。候选使用位置化action one-hot和归一化整数预算表示；固定初始设计
强制覆盖全部single-action/budget基线，后续以bagged/boosted shallow-tree代理的
解析Expected Improvement逐点选择。候选数、schedule长度、树深、ensemble规模、
threshold数和selection evaluation均有硬上限。

实现特意把“选择预算”和“验证诊断”分离：acquisition期间仅惰性计算已选择候选；
完成后才穷举封存模型目标以报告oracle与simple regret，且单独记录诊断调用数。
Versioned artifact封存F240 artifact、候选池摘要、初始设计及逐轮
`mu/sigma/EI/incumbent/observation`，验证器重放global和所有X-means cluster的完整
优化。在线scheduler保留90/10探索并执行真实秒级stage timeout。

当前证据是模型内一致性、确定性和篡改拒绝，不是实际求解速度声明。下一步顺序为：

1. 对F240 beam与F241 SMBO做相同候选池、selection budget和optimizer CPU的holdout
   消融，报告simple regret及真实PAR-2；
2. F242已实现winner产生后的SAT-gated running-attempt cancellation与helper
   interrupt；下一步测量尾部CPU节省与incomplete-consensus代价；
3. 固定Bitwuzla版本完成38-operator/model/UNSAT trust conformance；
4. 把Query IR operator histogram、node/width/input规模迁入context feature
   schema v2；
5. 只有equal-CPU corpus证明收益后，继续native/cross-worker clause/context
   transport。

### F242：SAT-Gated Portfolio Cancellation

F242修复了旧parallel portfolio“并发但等待全部attempt”的尾部CPU浪费。默认
`cancel_grace_ms=-1`保留完整SAT/UNSAT观测；显式启用后，仅首个SAT启动bounded
cross-check grace，UNSAT仍等待全部backend。Grace内的冲突继续保守返回unknown，
结束后取消queued Future并通过query-scoped `cancel(lease)`终止running helper的
完整process group。

One-shot JSON/QF_BV helper现在暴露active process；persistent JSON helper可在
阻塞line protocol时被异步中断并于下一query冷重启；prefix-persistent QF_BV取消后
删除对应LRU context，禁止复用可能失步的push/pop状态。Result和QueryStore stats
分别记录请求、实际取消及`consensus_complete`，支持把CPU收益和交叉检查损失同时
量化。

下一阶段按以下顺序推进：

1. 固定Bitwuzla版本，复用F237的38-operator/model/UNSAT trust matrix；
2. F243已将Query IR operator histogram、AST node/width/input规模纳入context
   feature schema v2，并完成F239--F241 artifact/state兼容迁移；
3. 在公开query holdout上联合消融F240/F241与F242 grace，报告optimizer和solver
   CPU而非只报告wall time；
4. 基于prefix hit/RSS/cancel数据决定native/cross-worker context transport；
5. CDCL trail/partial-clause transport仍需单独的soundness certificate。

### F243：Query-IR Structural Context Feature Schema v2

F243已把F237 lowering证书中可观察的Query IR形态提升为真实调度特征。QSYM在成功
导出每个IR DAG时原位累计node、不同input offset、最大bit width及comparison、
nonlinear、bitwise、structural四组operator计数；这些字段经过`SolverTelemetry`、
MPI结果和offline trajectory进入F239训练器及在线scheduler。

新`features-v2`在原9维后追加7个有界维度：按成功query平均的node/input对数规模、
最大width对数和四组operator/node比例。F239的X-means/BIC、F240 CART/beam和F241
SMBO均从sealed artifact自身推导9或16维，验证器据此重算几何、BIC、tree index和
schedule。历史v1 artifact无需改摘要即可加载；在线16维context对v1 prior截取前9维，
历史9维moment state则零扩展到16维。

F244已完成原计划第1项：固定官方Bitwuzla 0.9.1 commit，新增可重放QF_BV
conformance工具，并让cvc5 1.1.2与Bitwuzla共同通过38-operator、双模型验证及
UNSAT拒绝/授权矩阵。正式artifact和构建脚本已归档，不再把Bitwuzla CLI配置当作
未执行假设。

下一阶段按以下顺序推进：

1. 在公开query holdout上做feature v1/v2、F240 beam/F241 EI与F242 grace的等CPU
   联合消融，报告cluster stability、校准、PAR-2和solver CPU；
2. 在同一holdout加入Z3、cvc5 1.1.2、Bitwuzla 0.9.1单独/顺序/并行arm，严格按
   CPU-hour而非wall time比较；
3. 基于prefix hit/RSS/cancel数据决定native multi-context和跨worker transport；
4. 更细operator/depth/topology特征只在v2消融显示增益后进入新schema，避免维度膨胀；
5. CDCL trail/partial-clause transport继续要求独立soundness certificate。

### F244：固定版本Bitwuzla与可重放QF_BV可信矩阵

F244把F237的Bitwuzla“适配配置”升级为实机证据。官方0.9.1 tag/commit被固定在
可复现安装脚本中；conformance工具对每个backend运行全部38类Query IR operator的
单一SAT矩阵、未授权UNSAT拒绝和显式授权UNSAT接受。SAT结果在adapter与QueryStore
分别做一次完整Query IR验证，证据verifier还从规范envelope重建query ID和lowering
certificate。

artifact同时绑定版本输出、命令、二进制摘要、capability/lowering摘要和三项结果；
完整摘要用于本次运行归档，排除运行时间与本机路径的semantic摘要用于跨运行重放。
正式归档中Z3 4.8.12、cvc5 1.1.2与Bitwuzla 0.9.1均覆盖38/38 operator，语义
重放摘要一致。
可复现安装脚本从固定commit在clean prefix完成222步release构建并产生与正式安装
相同的binary SHA。最终门禁为439项Python、116项LLVM lit、QF_BV专项16项、
Z3/cvc5 String各6/6、双family research evidence、原生build及全量静态检查。

下一阶段的高优先级不再是增加solver名字，而是获得可证伪的效果证据：

1. 生成disjoint train/holdout Query IR corpus，冻结query/capability/solver binary
   摘要；
2. 等CPU比较Z3、cvc5、Bitwuzla单solver、静态portfolio、F240 beam和F241 SMBO；
3. 联合报告F243 v1/v2 calibration、F242 cancellation CPU收益与遗漏disagreement；
4. 只有prefix reuse和互补性结果显著时才实现native multi-context或跨worker
   context transport；
5. UNSAT proof checking、CDCL trail/partial clause reuse继续作为独立soundness
   工作包，不由本次conformance自动授权。

### F245：Sealed Paired QF_BV Holdout Campaign

F245完成了上述第1项的执行框架。Corpus artifact嵌入完整Query IR并重建CAS identity/
lowering certificate，使用query ID hash做稳定train/holdout切分；campaign只运行
holdout，并要求本机solver version/binary SHA与F244 conformance完全一致。每个
query/backend/repetition得到相同wall timeout，任务顺序稳定随机化，额外测量child
CPU并以PAR-2收费。

Verifier不调用solver即可重建split、任务笛卡尔积、lowering、SAT witness和全部
backend/pair aggregate。Replay重新运行固定binary，但semantic digest允许SAT选择
不同的合法模型。三solver接入还发现并修复了Z3 UNSAT后`get-value`的code-1差异：
只有剩余deadline内的status-only第二次UNSAT确认才可回到原授权路径。

本阶段checked smoke为8 query、4/4 split、三solver各4次，均3 SAT/1 UNSAT且无分歧。
最终444项Python、117项LLVM lit、Z3/cvc5各6/6 String/BV conformance、三solver
F244/F245语义重放、research evidence v2验签和原生runtime build均通过。它只是
I/T级管线证据。下一阶段顺序为：

1. 从多个公开target的QSYM spool封存真实Query IR，固定target/corpus/source commit；
2. 用至少20 repetitions执行confirmatory individual-solver基线，报告paired PAR-2、
   child CPU、timeout与互补性；
3. 在同一task manifest加入F240 beam/F241 EI sequence和F242 grace arm，保持总资源
   合同并测量额外进程CPU；
4. 把solver结果与原seed replay的coverage增量join，执行F243 feature v1/v2
   calibration与coverage/CPU-hour分析；
5. 只有真实prefix hit、互补性和CPU结果支持时再做native/cross-worker context或
   clause transport。

### F246：Leakage-Checked Solver-Strategy Holdout Campaign

F246完成了上述第3项的执行机制。新的policy bundle不只引用F240/F241文件，而是嵌入
带train query ID的规范化trajectory；verifier拒绝任一holdout ID，并从事件完整重训
F240，再从F240完整重跑F241，要求两个artifact逐字段相等。每个holdout Query IR的
F243 feature vector、cluster和budgeted schedule也在离线验签时重算。

同一随机化manifest现在包含三单solver、F240 beam、F241 SMBO和F242
parallel-cancellation六臂。Sequence stage预算总和不得超过共同wall budget；
parallel仅由SAT触发grace，内部attempt完整保留。所有内部和winner SAT都通过封存
Query IR复核，UNSAT必须授权。Artifact分别报告wall、child CPU、PAR-2、取消与
incomplete consensus，并明确并行CPU没有预先等量化。

Checked synthetic证据为4个holdout、24任务，各臂3 SAT/1 UNSAT；`grace=0`在三个
SAT任务中共取消6个attempt，UNSAT任务保留三后端完整结果。5项专项测试及真实semantic
replay通过；最终449项Python、118项LLVM lit、双String conformance、三份QF_BV
artifact验签、research evidence和原生build均通过。该结果只证明机制，不能用于
性能排名。后续顺序收敛为：

1. 从多个公开target冻结真实QSYM Query IR及train trajectory，执行至少20次六臂
   confirmatory campaign；
2. 用外层CPU quota或coverage/CPU-hour归一化并行臂，而不是把共享wall budget误称
   为等CPU；
3. join seed、target、AFL edge/data bitmap，完成F243 v1/v2 calibration、PAR-2与
   coverage效应的配对分析；
4. 单独扫描F242 grace `-1/0/10/50/200 ms`，报告节省CPU与遗漏共识的Pareto前沿；
5. 只有真实prefix复用和solver互补性支持时才实现native/cross-worker context或
   clause transport；UNSAT proof与CDCL clause transport保持独立soundness工作包。

### F247：Sealed AFL Edge/Data Coverage Join 与结构特征校准

F247完成了F246结果到真实fuzzing反馈的闭环。每个SAT model重新patch为具体input，
与原始witness按内容SHA-256去重后进入AFL++ `afl-showmap -S -e`持久forkserver。
Artifact同时封存target/showmap/preload binary identity、重复稳定性、完整sparse map、
candidate-vs-witness novelty、六臂union/complementarity及solver-child-CPU归一化结果；
离线verifier重新materialize input并重算所有统计，replay则对当前binary重新执行并
要求semantic摘要一致。

实现中修复了两个原生data-map正确性问题。PIE/DSO compare site不再使用受ASLR影响的
绝对return address，而使用module basename与module-relative offset。更关键的是，
AFL++内部`__AFL_MAP_SIZE`是分配上限，不是forkserver协商的有效map；旧hook可能把
data feature写到扫描范围之外。Preload现在在PCGUARD constructor之前预留低64 KiB
data namespace，使edge ID从其后分配。Edge与combined oracle都加载同一preload，
仅关闭/开启data写入，从而保持完全相同的edge ID布局。

F243 v1/v2的确定性L2 logistic calibrator只使用F246嵌入的train split trajectory，
再在individual-backend holdout replay标签上报告Brier、log loss与ECE。Checked
synthetic证据包含5个唯一input、24条join；原witness为6 edge/1 data，Z3 arm在该微型
target上新增6 edge/1 data，其余arm新增4--5 edge。v1/v2各评估12行、6个正例，
Brier为0.2807135/0.2810055。该结果只验证测量和校准机制，不能说明Z3优于其他策略。

下一阶段按科研优先级推进：

1. 从至少三个公开target冻结实际QSYM Query IR、原seed、target binary、train
   trajectory和source commit，并生成非synthetic F245--F247 artifacts；
2. 用F67 randomized blocks执行至少20次六臂confirmatory run，外层约束总CPU，
   报告coverage AUC/final coverage/PAR-2/CPU-hour及paired区间；
3. 扫描F242 grace `-1/0/10/50/200 ms`，形成coverage、CPU与incomplete-consensus
   的Pareto前沿；
4. 在真实数据上比较v1/v2 calibration、cluster stability和决策收益；若v2无稳定
   增益，不继续扩充operator/topology维度；
5. 只有prefix hit、RSS、cancel与互补性证据支持时，才进入native multi-context或
   跨worker context transport；proof-producing UNSAT与clause transport仍是独立
   soundness工作包。

### F248：画像驱动 Hydra 分支消除与原程序可信回放

F248把此前 data-only `targeted_transform`推进到 LLVM CFG。编译器在常规 SymCC
instrumentation 前读取稳定 site profile，只选择一个严格 diamond，用 LCS 对齐两臂
指令；匹配 ALU 通过 operand select 合并，未匹配 ALU 插入安全 extra operand。
激进模式还线性执行不同地址 load，并用“立即读取旧值再原值/旧值选择写回”完成
store alignment。新增 select 带专用 metadata，只构造 ITE、不重新调用路径约束，
所以被移除 branch 不会以多个 select fork 的形式返回。

由于激进 memory 变换只保证 failure-preserving，Python driver把 original binary
设为唯一 authority。每个 transformed candidate 都在原程序重放；只有两者都失败的
输入才进入 accepted failure 集，transformed-only failure 自动生成 site denylist，
原程序独有 failure 则作为 preservation violation 令 campaign fail closed。Artifact
绑定两二进制、单站点 manifest、输入与完整分类，并支持离线验签和真实semantic
replay。

Checked native smoke得到1个real failure和1个transformed-only failure，后者正确
归因到site `424248`；campaign/semantic摘要为`650c350c...`/`b9ed9cd1...`且重放
一致。这是机制证据，不是性能结果。

下一阶段继续完成 G09，而不是把单块 diamond 子集写成“通用 IFSS/Hydra”：

1. 建立显式 IFSS region-state/worklist、control/data relevance和symbol substitution；
2. 从单块 diamond 推广到有界 multi-block SESE region，并对 multi-exit 建
   exit-selector/state merge；
3. 接入 MemorySSA/AA，给 alias-sensitive load/store alignment 产生可验证 proof；
4. 只在 LLVM poison/undef/freeze、exception edge 和 loop summary 证明闭合后扩大
   eligible region；
5. 在公开 target 上做 original/safe/aggressive 三臂等 CPU profile-guided campaign，
   报告 fork、solver CPU、coverage、spurious率与 denylist收敛。

### F249：有界多臂 IFSS Region-State Worklist

F249完成了上述计划的第一步并覆盖一部分第二步。Compiler现在能从一个3--8 incoming
PHI反向找到共同 controller，再以最多32 blocks/64 paths的显式 worklist枚举
branch-only SESE region。每条路径被转成条件文字的 conjunction，同一 incoming
predecessor的多路径做 disjunction；各incoming值通过现有无副作用SSA和
MemorySSA/AA只读快照合成器替换，最后形成从后向前的 ITE region-state chain。
路径终点集合必须与 PHI incoming集合精确相等，不完整或歧义图一律拒绝。

这项实现也补上了 speculative instrumentation 的事务边界。最后一臂由已证明完备的
路径分区作为default state，不再生成无消费者的符号predicate；任何late synthesis
failure都会删除从原始merge insertion point开始插入的全部临时IR。该机制修复了
原型中孤立`_sym_build_equal(expr, null)`导致QSYM `map::at`异常的问题。

正向原生测试从`AX`得到`00 58`，在保持第二字节`X`时跨越root branch并选择PHI值20；
telemetry为Backsolver `1 target / 1 attempt / 1 SAT`，direct路径为
`4 attempts / 1 SAT`。拒绝测试让最后一臂依赖opaque call，证明前两臂已经开始合成
后的回滚仍产生合法LLVM IR且无IFSS runtime call残留。这些是机制证据，不是性能结果。

G09后续顺序更新为：

1. 为多出口SESE region建立显式exit selector，并分别合并return/continuation state；
2. 将现有只读load snapshot扩展为带MemorySSA/AA证书的alias-sensitive memory
   state merge，保持volatile/atomic和不确定ModRef fail closed；
3. 增加switch edge splitting与受限natural-loop summary，显式处理poison、
   undef、freeze和异常边；
4. 把Hydra strict diamond推广到profile选择的multi-block isomorphic region，
   并以original replay持续否决spurious站点；
5. 在公开target执行等CPU的关闭/two-arm/multi-arm及original/safe/aggressive
   campaign，报告coverage AUC、solver CPU、ITE规模、拒绝率和denylist收敛。

### F250：MemorySSA/AA 证明的双臂 Memory-State Merge

F250补上了此前只读snapshot之外的第一种写状态恢复。若merge-local simple load的
MemorySSA defining access是二输入MemoryPhi，两个incoming分别来自直接进入merge的
simple store，且AA对load/store均给出等宽`MustAlias`，compiler会在load处重建
`ite(controller, stored_true, stored_false)`并替换原shadow-read expression的所有
下游use。Region内其他写必须由MemorySSA标识且对目标location无Mod；MayAlias、
PartialAlias、unknown call、volatile/atomic和额外同址写全部拒绝。

ITE携带`must-alias-memoryssa-v1` metadata以及load/controller/两个store的稳定site，
用于审计实际接受了哪个memory merge；它是依赖LLVM分析的结构证书，不是假装独立
证明。stored value late synthesis使用load前anchor事务回滚，共享region合成器也
递归拒绝undef/poison。

原生smoke从`A`得到候选`00`，Backsolver为`1/1/1`，direct为`2/1`；NoAlias、
opaque late failure和poison三条拒绝路径均保持IR verifier-clean且无memory-state
残留。下一阶段依赖顺序为：

1. 沿MemorySSA def chain跳过经AA证明NoAlias的definition，并生成可复核chain
   metadata；
2. 把F249的3--8臂path predicate与F250的MemoryPhi incoming state统一，支持有界
   多臂memory state；
3. 在此状态模型上建立multi-exit selector，分别表示continuation identity和每个
   live-out value/memory location；
4. 再进入switch edge splitting、natural-loop summary和multi-block Hydra
   alignment，继续保持exception/atomic/不确定alias fail closed；
5. 以公开target的候选接受率、拒绝taxonomy、solver CPU、coverage AUC和等CPU
   paired run决定是否扩大alias与区域边界。

### F251：有界 NoMod MemorySSA Def-Chain

F251完成F250计划的第一步。每个MemoryPhi incoming现在可向后越过最多8个
MemoryDef，但每一步都必须由AA证明对目标load location不含Mod；遇到等宽simple
MustAlias store才结束。MayAlias、PartialAlias、nested MemoryPhi、liveOnEntry、
第9个definition和目标上的不兼容store均拒绝。NoMod并不足以放宽并发语义，因此
volatile、atomic RMW/CmpXchg和fence也显式拒绝。

有跳过项的ITE使用`must-alias-memoryssa-chain-v1` metadata，在四个核心site后按
true/false arm分别编码skip count和nearest-first definition sites。正向test两臂
各跳过一个独立alloca store，得到`00`与Backsolver `1/1/1`、direct `2/1`；
MayAlias、9-hop预算和volatile-chain负向路径均无残留。

下一实现目标是把这条可审计memory chain与F249多臂path worklist统一。不能为每个
MemoryPhi arm独立复制一套分支枚举，否则公共条件会重复合成且容易再次产生孤立
expression；应先抽出共享的region path partition，再让data PHI和memory PHI消费
同一组relevance predicate，最后才构建multi-exit continuation selector。

### F252：Data/Memory 共用的多臂 IFSS Partition

F252完成了上述统一。`buildIFSSRegionPartition`现在是data PHI与MemoryPhi的共同
声音性入口：2--8 endpoints、共同dominator、postdom merge、全region最多32 blocks、
全局最多64 paths、branch-only、无环、endpoint集合精确匹配。原F249实现已切换到
该partition，MemoryPhi则从二臂扩展到3--8臂；每个arm继续消费F251的MustAlias/
NoMod chain，前N-1臂构造AND/OR relevance，最后臂作为完备default，形成N-1层
memory-state ITE。

二臂proof schema保持兼容；多臂schema为
`must-alias-memoryssa-multi-v1`，逐arm绑定incoming block、store、path count、
skip count和definition sites。三臂native test同时要求每臂one-hop def chain，
从`AX`生成`00 58`，Backsolver `1/1/1`、direct `4/1`；一个MayAlias arm会拒绝
整个merge。

下一步先做partition-scoped condition cache与单一computation ownership，消除多个
relevance predicate重复合成相同branch expression；随后把partition state从
`value/memory`扩展成`exit-id + live-outs + memory locations`，为真正multi-exit
selector提供基础。没有exit identity和live-out完备性证明前，不把多入口到同一PHI
误标为multi-exit。

### F253：Partition-Scoped Condition Cache

F253完成condition DAG ownership。每个partition先按稳定path遍历收集前N-1臂的唯一
LLVM condition，并在任何predicate之前合成；cache条目只借用concrete/expression，
不携带computation。关键修正是每个唯一condition独立登记short-circuit computation，
使每个结果各自拥有fast/slow出口PHI。把多个condition合成一个range会让较早结果
不支配后续use，四臂原型已用LLVM verifier和QSYM `map::at`失败证明该做法错误。

四臂data与memory测试各只生成2个内部condition cache，共4个
`partition-condition-cache-v1`标记；输入`AXY`得到`41 00 59`，Backsolver
`1/1/1`、direct `5/1`。下一步可以安全进入exit state设计：

1. 先定义`ExitState { exit_id, relevance, live_outs, memory_states }`及完整性条件；
2. 只接受所有exit最终进入一个synthetic dispatch continuation、且无异常/indirect
   edge的有界区域；
3. 为每个exit建立与F252相同的路径partition和F253 condition ownership；
4. 对每个live-out要求支配值或可合成RegionValue，对memory要求F250--F252 proof；
5. 以exit-id ITE选择continuation，不线性执行不可投机副作用；无法封闭的exit整体
   fail closed。

### F254：有界 Multi-Return Exit State

F254先完成最清晰且可执行验证的真正multi-exit子集。新的
`IFSSExitLowering` module pass在AA/MemorySSA构建前识别2--8个normal return出口；
共同controller以下必须single-entry、branch-only、acyclic，最多32 blocks/64
paths，且枚举return集合与函数全部return精确相等。Pass把每个return改为进入
synthetic dispatch的branch，以`ifss.exit.state` PHI承载返回状态，再由F249--F253
构造跨出口ITE。它不移动或投机执行arm内指令，因此副作用仍只在原路径发生。

每条arm branch、state PHI和统一return都带
`bounded-return-exit-state-v1` metadata，记录controller、ordinal、原return site
及arms/blocks/paths。Switch、loop、EH/indirect形状、九出口、aggregate、
undef/poison、musttail和同一Value的无收益返回全部拒绝；无候选时也不写site
metadata，从而保持分析preservation契约。

三返回callee的native test从实际返回10的`AX`生成跨出口20候选，并保持第二字节
`X`；Backsolver和direct validation均成功，LLVM verifier通过。下一阶段不把
return归并夸大为general multi-exit，而按以下顺序扩展：

1. 建立有界多continuation dispatch，exit-id必须成为真实控制consumer；
2. 为每个continuation计算live-in tuple，逐槽应用F249/F253 RegionValue proof；
3. 只对F250--F252可证明的scalar memory location加入memory tuple；
4. 在所有tuple元素成功后一次性commit dispatch，任何槽失败整体回滚；
5. 最后才研究switch edge split与natural-loop exit summary。

### F255：有界 Switch Chain 与共享 IFSS State

F255没有为switch复制一套state engine，而是在所有analysis构建前把严格fanout转换
成确定性`icmp eq` branch chain。接受switch限2--8个唯一case/default目标；各arm
只能直接进入同一个exact-predecessor merge，或全部直接return。共享目标、外部
predecessor、conditional arm、mixed exit和九臂fail closed。生成comparison/branch
以`bounded-switch-chain-v1`绑定原switch site、case ordinal/value与arm count，
default edge有独立metadata。

这使三层能力组合在同一测试中成立：F252枚举case/default路径，F253为不支配merge
的内部case condition提供独立short-circuit ownership，F250--F252从switch arms
恢复MustAlias memory state。输入`A`可生成case `B`的validated candidate；data与
memory partition各只增加一个内部condition cache。另一个direct-return switch在
同一pipeline中先lower为branch chain，再由F254形成return-state PHI，验证pass依赖
顺序。

下一步优先级调整为：

1. profile-aware balanced switch tree，避免linear chain在case多时放大路径深度；
2. shared-destination edge splitting与PHI multiplicity proof；
3. general multi-continuation dispatch的exit-id/live-out tuple；
4. 可证明scalar memory tuple；
5. natural-loop单迭代/小固定界summary。

### F258：Shared-Destination Edge/PHI Multiplicity

F258允许2--8条逻辑switch edge映射到至少两个unique destination。Collector显式
证明每个目标的重复predecessor edge数及PHI同值incoming；lowering删除原重复
incoming后，按新CFG edge逐一重建。Linear示例保持`3→3`，balanced/profile因每个
leaf都含default miss而精确扩张为`3→5`。Root和PHI分别携
`shared-switch-edge-split-v1`及`shared-switch-phi-multiplicity-v1`。

这项工作同时修复一个真实声音性问题：共享switch可在merge只留下两个unique
incoming，旧两臂shortcut会用root condition错误代表整个endpoint。现在带shared
证书的两臂data PHI强制走F252完整path AND/OR partition；MemorySSA也强制输出
multi-path proof。Shared return还可继续进入F254 exit-state。测试覆盖三种switch
mode、PHI multiplicity、data/memory/return组合，原生输入`A`得到`B`候选。

后续依赖顺序为：

1. profile/tree独立manifest及LLVM branch-weight安全导入；
2. general multi-continuation dispatch的exit-id与live-out tuple；
3. continuation tuple中的可证明scalar memory state；
4. natural-loop单迭代/小固定界summary；
5. multi-block/isomorphic Hydra region alignment与等CPU公开评估。

### F259：LLVM Branch Weights 与 Proof-Carrying Tree Manifest

F259完成profile/tree artifact闭环。Profile mode先使用显式stable-site文件；未指定
或对应site缺失时，可从LLVM标准`!prof branch_weights`提取default+case向量。
显式文件损坏/不完整不会被metadata静默替代，zero/invalid权重也分类回退balanced。
Profile root proof现在记录来源，指纹同时绑定source和全部规范化权重。

`SYMCC_IFSS_SWITCH_MANIFEST_OUT`输出每个switch的JSONL record：requested/effective
mode、profile/fallback、preorder tree、case→destination、original/lowered
multiplicity、objective及双指纹。独立
`verify_ifss_switch_manifest.py`重算DP、tree、multiplicity与fingerprint；测试确认
external和LLVM来源objective均为2028、zero权回退，并拒绝root单字段篡改。

下一阶段进入general continuation tuple：

1. 先定义可验证`exit_id`及synthetic dispatch的真实控制consumer；
2. 为每个continuation计算确定性live-out slot schema；
3. 每槽复用F249/F253 RegionValue与condition ownership；
4. 仅在全部slot和exit路径封闭后原子commit；
5. 再加入F250--F252可证明的scalar memory tuple与natural-loop summary。

### F260：有界 Multi-Continuation Exit-ID/Live-Out Tuple

F260完成了general continuation state的第一个可执行子集。新的
`IFSSContinuationLowering`在symbolization前枚举branch-only、single-entry、acyclic
区域，限制2--8条exit、至少两个continuation、32 blocks、64 paths和1--8个scalar
PHI slot。每条exit经独立capture进入dispatch；`i8 exit_id` PHI由真实branch chain
消费，每个live-out slot在dispatch形成状态PHI，再经resume trampoline回写原
destination PHI。原路径上的副作用不移动。

实现对重复predecessor edge做精确multiplicity验证，并以
`bounded-continuation-tuple-v1`绑定controller、原exit terminator、successor index、
destination、slot与原PHI site。单destination、循环、vector/aggregate、poison、
不完整incoming或预算超限整体拒绝。强制partition metadata使两臂共享consumer不走
不完备的root-dominance shortcut。

三exit/两destination/两slot测试在symbolized IR中形成6个ITE；从实际第三出口输入
`C`生成跨destination输入`B`。共享source双edge测试保持两个逻辑incoming，拒绝矩阵
保持verifier-clean，flag-off不修改CFG。F260后全量compiler/lit为139/139。

后续依赖顺序收敛为：

1. 只把F250--F252可证明MustAlias/NoMod的scalar memory location加入continuation
   tuple，并要求全部location一次性成功；
2. 实现受限natural-loop单迭代/小固定界summary，显式建模loop exit identity与
   loop-carried scalar state；
3. 为continuation proof增加可重放CFG/tuple manifest和独立验证器；
4. 扩展multi-block/isomorphic Hydra alignment，并继续由original replay作故障权威；
5. 在公开target执行flag、tuple宽度和loop-bound的等CPU消融，避免以139项功能测试
   代替coverage/CPU证据。

### F261：有界 Affine Natural-Loop 闭式摘要

F261实现第一个声音性可执行的loop acceleration子集。`IFSSLoopSummary`借助LLVM
LoopInfo只接受preheader/header/latch/exit构成的两block natural loop：header条件
必须为`iv<T`，`iv`从0以plain add 1递增，且可证明`T<=8`。1--8个integer state均
须满足独立常量加法递推；header/latch不存在memory、call、atomic或其他操作。

Lowering在preheader构造`initial + T*step (mod 2^w)`，先给exit PHI新增preheader
incoming，再重定向edge并删除header/latch。Plain add的位向量模算术给出闭式等价；
`nuw/nsw`因poison触发条件可能不同而拒绝。`bounded-affine-loop-summary-v1`绑定
header/branch、最大trip、state ordinal及原PHI/update site。

测试覆盖单/双state、i8→i16 trip cast和exit PHI，摘要前后对32个输入做`lli`差分；
输入`00`时直接solver 1 query/1 SAT得到`05`。Bound15、store、非仿射、signed、
`nuw`和poison均fail closed。全量compiler/lit为141/141。

下一步不直接扩大到任意循环，而按证明依赖推进：

1. continuation tuple加入F250--F252证明的scalar memory location；
2. loop summary扩展到同一header/latch内的upper-triangular affine state，并用
   bounded differential/SMT equivalence certificate验证；
3. 将单exit摘要与F260 exit-id组合，支持有界break/continue而不执行arm副作用；
4. 构建continuation/loop独立manifest，重算CFG、recurrence和closed form；
5. 公开target等CPU消融后，依据接受率与solver CPU决定是否研究多block/memory loop。

### F262：MemorySSA/AA 证明的 Continuation Memory Tuple

F262完成F260后续的scalar memory state。新的
`IFSSContinuationMemory` Function pass运行在F260 CFG lowering之后、普通
Symbolizer之前。它要求destination simple scalar load的MemoryUse由dispatch
MemoryPhi定义，并逐一校验F260 capture/resume metadata、exit ordinal和destination
predecessor集合。这样memory state与真实逻辑exit绑定，而不是从运行时已选择路径
猜测未执行值。

每个相关exit沿MemorySSA incoming向后最多检查8个MemoryDef：只跨AA证明NoMod的
definition，最终必须到达等宽simple MustAlias store。Store/value还必须支配capture；
MayAlias、nested MemoryPhi、atomic/fence、缺store或预算超限均回退。成功后dispatch
新增最多4个memory PHI slots，相关exit携store value，其他continuation携不会被消费
的zero/null。原load仍执行以保持trap和访存时序，其data uses改为已证明等价的PHI。

Proof `must-alias-continuation-memory-tuple-v1`记录controller/load/store、slot/
destination/exit ordinal以及逐项NoMod chain。正例覆盖3 exits、2 destinations、
2 locations和one-hop NoMod chain，flag-off/lowered对256输入`lli`差分一致；输入
`C`产生跨destination的`B` witness，记录Backsolver `3/3/2`、direct `9/2`和一次
Z3 fallback。负例覆盖MayAlias、缺store、volatile、external predecessor、
destination前缀写以及第5 slot的整controller回退。

后续按证明依赖继续：

1. 为F260/F262构建独立CFG、tuple和ModRef replay manifest，避免只信IR metadata；
2. 把F261扩展为受限upper-triangular affine multi-state recurrence，并生成可重算
   recurrence/closed-form certificate；
3. 组合loop exit identity与continuation tuple，支持有界break/continue；
4. 实现multi-block isomorphic Hydra alignment并维持original-authoritative replay；
5. 在公开target上做continuation SSA/memory、loop和Hydra的等CPU独立/交互消融。

### F263：可重放 Continuation CFG/Tuple Manifest

F263完成continuation artifact的第一个闭环。
`SYMCC_IFSS_CONTINUATION_MANIFEST_OUT`让F262分析pass在输出前重新收集F260
capture/resume结构，并验证exit ordinal、原terminator/successor、destination
partition、scalar slot和预算。Memory slot不会直接信任metadata：pass按原load site
重新运行MemoryUse→dispatch MemoryPhi、MustAlias/NoMod chain和dominance检查，只有
逐项store/skipped site相同才标记`llvm-memoryssa-aa-revalidated-v1`。

JSONL schema `symcc-ifss-continuation-manifest-v1`绑定module/function、controller、
全部count、edge、destination、scalar PHI和memory chain，并产生规范64位内容指纹。
独立Python verifier重算结构、partition、chain bound和fingerprint。正例同时验证
memory-on与structural-only record，并拒绝destination、NoMod site和hash篡改。

仍需按顺序完成：

1. 给manifest增加compiler/LLVM binary identity、输入/输出IR SHA-256及并发安全封存；
2. 提供可选bitcode replay verifier，在独立进程重新运行MemorySSA/AA并核对chain；
3. 扩展F261到受限upper-triangular affine multi-state recurrence；
4. 将loop多exit identity与F260/F262 tuple组合；
5. 实现multi-block Hydra并执行公开target的等CPU消融。

### F264：Continuation Artifact 原子强身份封存

F264补上F263明确留下的artifact identity边界。新的
`seal_ifss_continuation_artifact.py`在build完成后先验签manifest，再对manifest、
输入IR、最终lowered IR、SymCC pass binary和实际LLVM tool流式计算SHA-256/size，
同时记录有deadline/size bound的LLVM version identity。全部proof fingerprints和
文件身份进入canonical JSON的外层SHA-256。

封存通过`flock`、fresh-output gate、同目录临时文件、file/directory `fsync`和
atomic replace提交，拒绝覆盖既有证据。Verify必须重新提供全部artifact，重跑
manifest verifier、文件hash和LLVM identity；不能只依赖envelope自证。测试覆盖
正常seal/verify、重复seal、lowered IR漂移和envelope字段篡改。

下一依赖现在是：

1. 可选独立bitcode MemorySSA/AA replay，把F263的compiler-time ModRef重放升级为
   sealed external verification；
2. F261 upper-triangular affine multi-state闭式与独立recurrence manifest；
3. bounded loop break/continue exit identity和F260/F262组合；
4. multi-block Hydra alignment及original-authoritative replay；
5. 将verified seal纳入F67公开confirmatory campaign。

### F265：独立 Bitcode MemorySSA/AA 证明重放

F265完成F263/F264留下的ModRef replay依赖。新的
`replay_ifss_continuation_artifact.py`先完整验证seal及其绑定的manifest、输入/输出
IR、SymCC plugin和`opt`，再启动独立LLVM进程，仅在sealed lowered IR上运行
`ifss-continuation-memory`证明重建。Continuation和memory lowering均被强制关闭，
因此子进程不会重新改写CFG，只能用当前MemorySSA/AA重新验证已有tuple并输出manifest。

重建record先通过F263 Python verifier，再与sealed record逐对象完全相等。正例完成
3-exit/2-destination/2-location replay；反例把一个NoMod noise store改为对目标
location的写。这个修改后的IR仍是合法LLVM IR，也能重新生成有效SHA-256 seal，但
独立replay会拒绝，证明F265检查的是alias语义而不只是文件完整性。

下一依赖现在是：

1. F261 upper-triangular affine multi-state闭式及独立recurrence manifest；
2. bounded loop break/continue exit identity和F260/F262 tuple组合；
3. multi-block Hydra alignment及original-authoritative replay；
4. continuation partial-overlap/initial-state/nested MemoryPhi的分层证明；
5. 将seal+replay双证据纳入F67公开confirmatory campaign。

### F266：Upper-Triangular Affine Multi-State Loop Closed Form

F266把F261从独立constant-add state扩展到2--4状态的同位宽unit-diagonal
upper-triangular recurrence。Update DAG可由plain `add/sub`和常数乘法组成；编译器
解析出`x'=Ax+b`并拒绝逆三角依赖、非unit diagonal、跨位宽、超过32项的表达式及
既有side-effect边界。

实现最初验证了按0--7 trip枚举的语义，但该形式使Symbolizer在单块中拆分数百条
operation并产生非法CFG。最终实现使用`N=A-I`的nilpotence，生成
`A^T=sum C(T,k)N^k`和累计offset闭式；二项式系数在`T<=8`证明下用i16精确递推。
这把trip枚举select降为共享binomial DAG，并让symbolized IR恢复LLVM verifier-clean。

新的`SYMCC_IFSS_LOOP_MANIFEST_OUT`记录`A,b`、初值/sites和trip 0..bound的全部矩阵
幂/offset；独立verifier重算模矩阵代数和内容指纹。三状态链经32输入差分一致，
输入`00`用1 query/1 SAT求得trip 5；基矩阵、派生power和fingerprint篡改均拒绝。

下一依赖现在是：

1. bounded loop break/continue exit identity和F260/F262 tuple组合；
2. multi-block isomorphic Hydra alignment及original-authoritative replay；
3. continuation partial-overlap/initial-state/nested MemoryPhi的分层证明；
4. loop manifest的IR/binary seal与独立bitcode recurrence replay；
5. 公开target上loop independent/triangular/off三臂等CPU消融。

### F267：Bounded Post-Update Break/Continue Exit Tuple

F267在F261/F266 canonical两块循环上增加一个受限multi-exit形态：header false edge
正常退出，latch完成state update后以`iv==break_at`选择break或continue。严格公式为
`break_taken=break_at<trip`与
`executions=break_taken ? break_at+1 : trip`；独立和upper-triangular closed form
均复用这个实际迭代数。

Normal exit PHI只能消费header state，break exit PHI只能消费latch update，确保
零迭代、更新前正常退出和更新后break不混淆。Lowering为两组PHI补preheader live-out
后生成exit branch并删除循环。两字节回归对64组trip/break组合差分一致，从`00 07`
生成`break_at=2, trip>=3`的result-119候选。

`SYMCC_IFSS_LOOP_EXIT_MANIFEST_OUT`记录两出口sites、state phase映射和完整8×8
execution truth table；独立verifier重算公式与fingerprint。Table、live-out和hash
篡改均拒绝；`ne`、`iv_next`比较、反向branch和direct break use在有界trip下分别
fail closed。

下一依赖现在是：

1. multi-block isomorphic Hydra alignment及original-authoritative replay；
2. continuation partial-overlap/initial-state/nested MemoryPhi的分层证明；
3. 多break/continue site的bounded predicate/exit-priority circuit；
4. loop recurrence+exit manifest统一seal与独立bitcode CFG replay；
5. 公开target上loop off/independent/triangular/break等CPU消融。

### F268：Bounded Multi-Block Linear-Arm Hydra Alignment

F268把F248的single-block strict diamond扩展为两侧各1--4个、块数相等的线性arm。
每个内部edge必须无条件跳转且目标只有该arm前驱，两个tail汇入同一二前驱merge；
跨块SSA只能留在本arm或成为merge PHI输入。编译器按CFG顺序压平两侧instruction，
在整个region上执行compatible-operation LCS，因此能对齐跨块def-use，同时对
不同长度arm、内部branch、循环、escaping SSA和既有unsupported operation保持
fail closed。

新的`bounded-multiblock-linear-hydra-v1` metadata和manifest绑定左右block sites、
逐alignment slot的instruction site/opcode、output PHI sites和结构指纹，并对safe
与aggressive模式都要求original-authoritative coverage replay。独立verifier重算
结构约束和fingerprint；campaign对新record强制验签，同时兼容无新字段的F248历史
artifact。

2×2 block正例在全部256个一字节输入上与原IR差分一致，lowered和symbolized IR均
通过LLVM verifier；block、alignment和hash篡改被拒绝，2×1 unequal-arm保持原CFG，
F248原回归不退化。

下一依赖现在是：

1. continuation initial-state/partial-overlap/nested MemoryPhi的分层证明；
2. 多break/continue site的bounded predicate/exit-priority circuit；
3. Hydra不同长度arm的proof-carrying edit alignment与bounded internal branch tree；
4. continuation/loop/Hydra manifest的统一seal和独立bitcode replay；
5. 公开target上的continuation、loop和Hydra等CPU confirmatory campaign。

### F269：Partial Store/Live-on-Entry Continuation Memory Tuple

F269完成continuation memory分层证明的第一层。一个destination的相关exits现在可以
混合F262 MustAlias store与经最多8项NoMod chain到达Function
`MemoryLiveOnEntry`的状态；candidate仍必须至少包含一条store路径。Initial路径的
load pointer必须支配该capture，snapshot只插入其路径专属capture末端，不会在其他
continuation路径上提前解引用。

新的v2 memory proof逐exit绑定store/live-on-entry kind、source site和NoMod chain；
manifest增加state schema/kind及v2 analysis分类，独立verifier兼容旧all-store
record并重算新指纹。语义replay同时升级为核对dispatch PHI的实际incoming：store
必须携原stored operand，initial必须携同capture、同pointer/type/alignment且后续
无write的proof-bound snapshot，无关destination必须携typed neutral。

三exit正例对256输入差分一致，symbolized IR通过verifier，从`C`生成跨initial路径
的`B`候选。Kind篡改被独立verifier拒绝；合法IR把snapshot incoming改为常量99后
可重新seal，但独立MemorySSA/AA replay拒绝。Partial路径MayAlias write与原有拒绝
矩阵保持fail closed。

下一依赖现在是：

1. bounded acyclic nested MemoryPhi provenance DAG与独立replay；
2. 多break/continue site的bounded predicate/exit-priority circuit；
3. Hydra不同长度arm的proof-carrying edit alignment与bounded internal branch tree；
4. continuation/loop/Hydra manifest的统一seal与公开campaign绑定；
5. byte-lane partial overlap、symbolic alias split和公开等CPU消融。

### F270：Bounded Acyclic Nested-MemoryPhi Provenance Tree

F270完成continuation memory分层证明的第二层。每条相关dispatch incoming不再局限于
线性MemoryDef chain，而可递归穿过支配下游点的非dispatch MemoryPhi。每个phi只允许
2--4个唯一incoming block；每个exit最多4个phi、16个节点，每条边仍最多8项NoMod。
叶节点必须是F262的equal-width simple MustAlias store或F269 LiveOnEntry。Active-set、
dominance、MayAlias/Mod、atomic/fence和slot至少一条store的约束共同保证失败时完整
回退。

Lowering按证明树在原MemoryPhi block物化scalar PHI，再把根值送入dispatch tuple。
新的v3 metadata/manifest按canonical preorder绑定节点kind/source、NoMod chain、
incoming block site和child ordinal；独立verifier检查tree规范并重算内容指纹，独立
LLVM replay递归检查实际nested PHI和dispatch PHI incoming。两层正例对1024组输入
差分一致，从`C 03`生成`A 00(mod 4)`候选；edge篡改与重新seal后的最深incoming
常量篡改均被拒绝，MayAlias和5-arm phi反例保持原load。

该测试还定位并修复了Symbolizer的通用拓扑错误：父concrete clone前移到symbolic
short-circuit边界时可能越过子clone。现在前移操作递归处理同block concrete operand
closure，保证依赖先于使用；两层nested PHI的symbolized IR因此通过LLVM verifier。

下一依赖现在是：

1. 多break/continue site的bounded predicate/exit-priority circuit；
2. Hydra不同长度arm的proof-carrying edit alignment与bounded internal branch tree；
3. continuation/loop/Hydra artifact的统一seal与独立CFG/rewrite replay；
4. continuation byte-lane partial overlap与symbolic alias partition；
5. 公开target上continuation/loop/Hydra的等CPU confirmatory campaign。

### F271：Bounded Multi-Break Exit-Priority Circuit

F271把F267的单post-update equality break扩展为2--3个有序check site。Canonical
loop在唯一update block完成state/induction update，随后各check只比较
`iv==break_at[i]`；true进入独立break exit，false继续下一check或header。第四site、
非eq、反向successor、错误phase PHI和额外指令完整回退。

Closed form按CFG ordinal扫描，用strict
`break_at[i] < winner_at`选择winner；因此先选择最小命中迭代，并在相等迭代保留
较小ordinal。`winner_at+1`成为F261/F266共享execution count。Lowering建立显式
winner dispatch chain，normal消费header-phase summary，每个break只消费
post-update summary。

V2 exit manifest保存有序break identity、全部phase live-out和完整
trip×break-vector truth table；独立verifier重算lexicographic winner、execution
count和内容指纹。2-break正例对512组输入一致，从`00 07 07`求得break1目标候选；
3-break上限又与upper-triangular recurrence组合并对256组输入一致。Winner/site/
live-out/hash篡改及第四break、`ne`、polarity反例均拒绝。

下一依赖现在是：

1. Hydra不同长度arm的proof-carrying edit alignment；
2. Hydra bounded internal branch tree与general SESE ownership proof；
3. continuation/loop/Hydra artifact统一seal及独立bitcode CFG/rewrite replay；
4. continuation byte-lane partial overlap与symbolic alias partition；
5. 公开target上的等CPU、多轮confirmatory campaign。

### F272：Proof-Carrying Unequal Linear-Arm Hydra Alignment

F272移除了F268的左右block count相等限制。两侧仍分别受1--4块、64条instruction、
unique-predecessor、unconditional edge和escaping-use证明约束，但flattened LCS与
全region value map现在可直接处理不同数量的线性block。Lowering算法不需要复制
block边界，只按完整def-use序列执行matched/extra operation并原子删除两侧region。

等长record继续使用原v1 schema。Unequal record使用v2，分别绑定左右块数/指令数、
每个edit slot的site/opcode/block ordinal、one-sided edit distance和结构指纹。
独立verifier检查两侧instruction完整消费与block顺序，campaign gate对v2正例直接
验签并在执行前拒绝ordinal篡改。

2×1正例包含一次matched add和一次left-only mul，1×4正例把四项matched def-use分布
在右侧四块；两者各对256输入差分一致并通过symbolized verifier。Edit/ordinal
tamper、1×5超预算与internal condition tree均拒绝，F268 v1保持兼容。

下一依赖现在是：

1. Hydra bounded internal branch tree alignment与path-predicate ownership proof；
2. continuation/loop/Hydra artifact统一seal和独立bitcode rewrite replay；
3. continuation byte-lane partial overlap与symbolic alias partition；
4. general-memory loop与cyclic provenance研究；
5. 公开target上的等CPU、多轮confirmatory campaign。

### F273：Proof-Carrying Bounded Internal-Tree Hydra Melding

F273把linear arm扩展为有界无环branch tree：每侧最多7块、3个内部condition、4条
merge leaf edge和64条instruction。所有非根块必须唯一parent，DFS preorder不允许
tree内汇合；两侧leaf必须精确覆盖共同merge predecessor。纯线性region仍保持F272
的4块上限。第4 branch、第5 leaf、内部PHI/reconvergence、empty alignment和proof
identity碰撞在IR修改前完整回退。

Lowering为每个block生成outer/root与内部edge condition的合取guard。Matched与extra
ALU以block guard选择真实或opcode-safe operand，merge PHI按完整leaf guard链重建；
aggressive store用guarded old-value readback保持非活动路径的内存值。新v3 manifest
记录完整parent/successor topology、leaf edge、alignment block ordinal和内容指纹，
独立verifier重建tree indegree、preorder、merge-edge集合及identity唯一性，campaign
gate直接消费同一证明。

3×1最小树、7×7最大双树和aggressive同址matched-store树均对256输入差分一致并通过
symbolized verifier。Parent/leaf/successor篡改拒绝，第4内部branch、empty tree与
内部reconvergence保持原CFG。

F273完成时的下一依赖是（其中第1项已由F274完成）：

1. continuation/loop/Hydra artifact统一seal与独立bitcode CFG/rewrite replay；
2. continuation byte-lane partial overlap与symbolic alias partition；
3. general-memory loop、cyclic provenance与一般SESE DAG研究；
4. profile binary identity、并发artifact封存和远端透明日志；
5. 公开target上的等CPU、多轮confirmatory campaign。

### F256：Unsigned Range-Balanced Switch Tree

F256完成上述第一项。`SYMCC_IFSS_SWITCH_MODE=balanced`先按unsigned case value排序，
内部节点用`ule`二分case集合，叶节点用`eq`精确匹配，所有leaf false edge进入
default。7-case/8-arm时，最坏判定从linear的7层降到3个range+1个equality；代价是
comparison总数从7增至13。

Default现在有7条gap路径。实现不会用一个虚假default条件掩盖它们，而是复制原
default edge-PHI value到每个leaf predecessor；F252显式枚举并OR这些路径。静态
test验证linear/balanced comparison数和1→7 PHI incoming，symbolic test验证12个
condition cache、7层ITE，原生输入`00`得到case6的`06`候选。

下一阶段先实现profile-weighted tree而不是继续增加case bound：

1. 从stable switch site读取case/default observation profile；
2. 以expected comparison cost为目标构造确定性weighted tree；
3. manifest绑定profile hash、site、weights、tree和fallback reason；
4. 对missing/stale/zero-mass profile回退balanced；
5. 做linear/balanced/profile三臂等CPU消融后，再进入shared-destination splitting。

### F257：Profile-Weighted Optimal Alphabetic Switch Tree

F257完成profile-aware tree主路径。`SYMCC_IFSS_SWITCH_MODE=profile`从
`SYMCC_IFSS_SWITCH_PROFILE`按stable switch site读取完整正权重case/default集合。
对unsigned有序case使用`O(C^3)`区间动态规划，最小化所有case的weighted range
depth；每个leaf固定一次exact equality。相同目标成本选择最小split，profile内容
以完整64位指纹、权重和objective绑定到root proof metadata。

七case测试令case0权重1000、其余为1，root bound从balanced的2移动为0；IR仍为
6个range+7个case equality并通过verifier。缺项、不可读文件和同site重复记录都
确定性回退balanced，且分别输出proof-carrying reason。原生输入`00`仍可生成
case6的`06`候选，说明weighted shape没有改变switch语义或IFSS state恢复。

重要限制是聚合default计数没有value-gap位置：本实现记录但不把default weight
加入split，避免用无证据的gap假设伪造最优性。后续顺序调整为：

1. shared-destination case edge splitting与edge-specific PHI multiplicity proof；
2. 独立profile/tree manifest、LLVM branch-weight导入和三模式等CPU消融；
3. general multi-continuation dispatch的exit-id与live-out tuple；
4. 可证明scalar memory tuple；
5. natural-loop单迭代/小固定界summary。

### F274：Unified Sealed Independent Transformation Replay

F274建立了continuation、loop和Hydra共同的
`symcc-transformation-seal-v1`。Seal不只保存manifest hash，而是先调用对应的独立
verifier，提取有序proof/structure fingerprints，再绑定input IR、lowered textual
IR、compiler plugin、LLVM tool和`opt --version`。Pipeline配置也进入canonical
envelope：Hydra绑定site和safe/aggressive mode；loop绑定实际输出的recurrence/exit
清单集合；continuation根据proof analysis恢复memory-on/off。Manifest在验证与读取
前后必须保持同一size/SHA-256，seal以lock、fresh gate、双fsync和atomic rename
提交。

独立replay从原input IR重新执行完整pass，不再相信已lowered IR。重放进程先清除三类
变换环境变量，再从seal恢复唯一配置；新manifest需再次通过各自verifier并与原record
完全相等，新lowered IR需与sealed产物逐字节SHA-256一致且通过LLVM verifier。重放
结束后再次校验seal和全部外部制品，避免执行窗口内静默替换。

Continuation nested-MemoryPhi memory-on和structural-only memory-off、三break
upper-triangular loop、最大internal-tree Hydra均完成seal/verify/replay。三类
verifier-clean lowered-IR篡改即使重新封存，也因无法由输入IR确定性重建而拒绝；
重复seal output和envelope字段篡改同样拒绝。该机制把F263--F273的结构证明与实际
rewrite纳入统一可复检边界，但不替代数字签名、远端透明日志、跨LLVM版本等价证明或
公开等CPU campaign。F274四项定向测试通过，最终全量回归为155/155通过
（97.79秒）。

F274完成时，后续研究队列为：

1. continuation byte-lane partial overlap与symbolic alias partition；
2. general-memory loop、cyclic provenance与一般SESE DAG；
3. 签名/远端透明日志及跨主机artifact transport；
4. 公开target上的等CPU、多轮confirmatory campaign。

### F275：Proof-Carrying Continuation Byte-Lane Partial Overlap

F275已完成上述第1项的constant-offset byte-lane部分。旧v1--v3 scalar证明失败时，
2--8 byte整数load可在最多16个线性MemoryDef内，为每个address-order lane选择最近
的同base、inbounds常量区间store byte；未写lane由capture-local LiveOnEntry snapshot
补齐。每条相关exit必须完整覆盖且包含真实store，NoMod skip仍受8项预算。动态offset、
不确定别名、MemoryPhi、volatile/atomic及非整数region fail closed。

Lowering按DataLayout端序建立extract/zext/shift/or链。V4 proof和manifest绑定
endianness、byte width、NoMod sites及逐lane source kind/site/byte/width；
Python verifier重算规范结构与fingerprint，独立LLVM replay重新推导MemorySSA/AA并
核对实际表达式链。Little-endian两种组合对256输入一致并从`C`生成`B`模型；
big-endian专项通过，lane和fresh-sealed store-value篡改拒绝，v1--v3保持兼容。

下一实施顺序更新为：

1. continuation bounded symbolic alias partition，要求guard/offset域可证明互斥完备；
2. continuation loop-carried/cyclic byte-lane MemoryPhi；
3. general-memory loop与一般acyclic SESE DAG；
4. 签名、远端透明日志与跨主机artifact transport；
5. 公开target等CPU、多轮confirmatory campaign。

### F276：Guarded Two-Arm Symbolic Alias Partition

F276完成F275之后的第一层symbolic alias支持。一个路径上的一个simple store可以
通过pointer select选择overlap地址或disjoint地址；guard必须是有stable site且支配
capture的instruction，两个arm必须分别由same-base constant interval或AA NoAlias
封闭分类。命中一个arm的lane形成guarded overlay，未命中时精确回退到F275继续追踪的
older store/LiveOnEntry byte。

V5 proof在每条lane绑定guard presence/site、true/false polarity、store site和source
byte/width。Lowering在i8层构造select后才做端序position；独立replay既重做逐臂AA/
interval证明，也核对实际select condition和operand顺序。正例经1024输入差分、双
seal/replay和symbolized verifier；guard与selected byte tamper拒绝。未知MayAlias
arm、第二guarded store和无stable site的argument guard回退。

下一实施顺序为：

1. continuation bounded cyclic/loop-carried byte-lane MemoryPhi；
2. 多层finite pointer union与guarded-write priority composition；
3. general-memory loop与一般acyclic SESE DAG；
4. 签名/透明日志/跨主机transport和公开confirmatory campaign。

### F277：Proof-Carrying Cyclic Byte-Lane Continuation MemoryPhi

F277完成上述第1项。它只接受一个两输入MemoryPhi构成的canonical loop：entry和
latch都以唯一无条件边进入header，header支配capture，entry在header支配域外而
latch在域内。Entry复用F275逐byte最近写者证明；latch从incoming MemoryAccess反向
追踪到同一MemoryPhi，以store更新被覆盖lane，以cycle PHI自身携带未写lane。Store与
carry必须同时存在，因此这里证明的是partial-update fixed point，而不是把普通
full-width writer伪装成循环摘要。

Lowering先生成entry composition，再在原header建立整数cycle PHI，最后在latch按
DataLayout端序生成carry/store的extract-zext-shift-or transfer。循环退出后的F260
capture直接携带该值。V6 metadata/manifest绑定header、entry、backedge site、
prefix/latch NoMod、entry lanes和backedge store/carry lanes；编译器独立replay解析
实际递归PHI及两个incoming表达式，Python verifier重算规范结构和fingerprint。

Little-endian正例完成1024组语义差分、symbolized verifier、Backsolver模型、两套
seal/replay与两类tamper rejection；big-endian专项和五类fail-closed负例通过。
F277不声称支持conditional/multi-latch/nested loop、symbolic offset、guarded
backedge、aggregate/atomic或general heap graph。

下一实施顺序更新为：

1. continuation多层finite pointer union与guarded-write priority composition；
2. conditional/multi-latch cyclic transfer及general-memory loop；
3. Hydra一般acyclic SESE DAG、tree-local PHI与multiple merge；
4. artifact签名/透明日志/跨主机transport；
5. 公开target等CPU、多轮confirmatory campaign。

### F278/F279：Finite Pointer Union 与 Guarded-Write Priority

F278完成多层finite pointer union的第一层工程闭包。单个store允许深度2、最多3个
内部select和4个leaf；每个leaf独立做same-base interval/AA NoAlias分类，tree必须
unique-parent且每个lane至少保留一个fallback。V7绑定完整node/leaf拓扑，lowering和
replay递归生成/解析实际i8 select tree。最大树经1024输入差分、双seal/replay、
Backsolver模型和两类tamper rejection；depth3、unknown leaf、shared DAG和第二
partition store拒绝。

F279随后允许同路径两条single-level guarded writes。Reverse MemorySSA给newest
writer priority 0、older writer priority 1；lowering按反序应用，使latest select位于
actual expression外层。V8逐lane记录稀疏但有序的guarded sources，replay由外到内
剥离并最终验证base。四个guard组合明确验证new覆盖old；第三writer继续拒绝。

下一实施顺序更新为：

1. conditional cyclic byte-lane transfer，再扩展multi-latch规范化子集；
2. pointer-union writer与priority composition的受控组合；
3. general-memory loop与Hydra一般acyclic SESE DAG；
4. artifact签名/透明日志/跨主机transport；
5. 公开target等CPU、多轮confirmatory campaign。

### F280：Conditional Cyclic Byte-Lane Transfer

F280完成F277之后的第一层conditional fixed-point扩展。它识别真实MemorySSA中的
严格diamond：header MemoryPhi的backedge来自latch MemoryPhi；write arm沿有界
MemoryDef链回到header，carry arm直接引用同一header状态。两个arm必须共享一个
instruction-valued i1 guard前驱、各自只有一个前驱，并以唯一无条件边汇入latch。
这组拓扑约束使条件的互斥完备性来自实际CFG，而不是从路径条件文本猜测。

Lowering对store lane生成`guard ? stored : carried`，对其他lane直接self-carry，
再按DataLayout端序合成递归integer PHI。V9绑定branch/write-arm/carry-arm/guard
site与极性；独立compiler replay重做MemorySSA/AA并解析actual select，Python
verifier重算拓扑和fingerprint。Little-endian正例完成1024组差分、Backsolver模型、
双seal/replay与tamper rejection；big-endian false-polarity和四类新增fail-closed
负例通过，v1--v9兼容回归16/16。

下一实施顺序更新为：

1. multi-latch规范化子集，先定义可证明的latch priority与PHI映射；
2. pointer-union writer与priority composition的受控组合；
3. general-memory loop与Hydra一般acyclic SESE DAG；
4. artifact签名/透明日志/跨主机transport；
5. 公开target等CPU、多轮confirmatory campaign。

### F281：Two-Latch Cyclic Byte-Lane MemoryPhi

F281把canonical cycle从一个latch扩展到恰好两个latch，同时保留LLVM原生PHI
predecessor语义。Header MemoryPhi必须有一个域外entry和两个域内无条件backedge；
每个latch独立证明有界partial-store/self-carry transfer，任一transfer不完整都会
拒绝整个memory slot。Lowering在header生成三输入integer PHI，并在两个latch各自
生成端序感知的byte composition，不引入没有CFG依据的writer priority。

V10把两个backedge按MemoryPhi incoming顺序记录，各自绑定site、NoMod链和lane
vector；compiler replay解析actual三输入recursive PHI与两条表达式，Python verifier
检查canonical transfer和fingerprint。交替写低/高byte的正例完成1024组差分、
Backsolver模型、双seal/replay及两种tamper rejection；third latch、full-width
latch和nested conditional latch负例通过，v1--v10兼容18/18。

下一实施顺序更新为：

1. pointer-union writer与two-write priority的受控组合；
2. arbitrary-predicate/general-memory loop summary；
3. Hydra一般acyclic SESE DAG、tree-local PHI与multiple merge；
4. artifact签名/透明日志/跨主机transport；
5. 公开target等CPU、多轮confirmatory campaign。

### F282：Pointer-Union Writer Priority Composition

F282完成F281队列中的第1项受控组合。它接受一个最新F278 depth-two
pointer-union store和一个更旧F276 single-level guarded store。逆向MemorySSA次序
是proof的一部分：partition必须先于旧guard被发现；lowering则从base开始先应用旧
guard，再把所得byte作为partition所有fallback leaf，从而精确实现
`union-hit ? union-byte : (old-guard ? old-byte : base-byte)`。

V11同时记录旧writer的guard/site/polarity/source-byte与最新writer的完整
node/child/leaf tree，fingerprint覆盖二者。Compiler replay重新推导AA/MemorySSA，
从actual nested select中先核对guarded fallback，再核对partition topology；
Python verifier独立检查tree ownership、lane coverage、writer presence和fingerprint。
正例完成1024组差分、symbolized verifier、Backsolver模型、两套seal/replay和
polarity/value tamper rejection；反向写入次序与两个旧guarded writer负例均
fail closed，v1--v11兼容20/20。

下一实施顺序更新为：

1. conditional与multi-latch cyclic transfer组合；
2. arbitrary-predicate/general-memory loop summary；
3. Hydra一般acyclic SESE DAG、tree-local PHI与multiple merge；
4. 超出F282固定上界的多union/多guard writer graph，仅在能保持canonical replay时扩展；
5. artifact签名/透明日志/跨主机transport与公开confirmatory campaign。

### F283：Conditional Multi-Latch Cyclic Byte-Lane MemoryPhi

F283完成上述第1项。它保留F281实际三输入header MemoryPhi，并允许两个latch各自
采用普通partial-store/carry或F280 strict write/carry diamond；至少一条transfer
必须conditional。每条latch只解释自己的incoming，LLVM PHI predecessor决定运行时
选择，因而不需要也不允许构造跨latch writer priority。Conditional lane保留原guard
和true/false store polarity，未写lane继续从同一recursive integer PHI self-carry。

V12为每个ordered backedge记录transfer kind；conditional记录额外绑定branch、
write/carry arm、instruction guard和polarity。Compiler replay重建MemorySSA/AA并
解析actual三输入PHI以及两条普通或guarded composition；Python verifier重算
canonical structure和fingerprint。审查同时修复了verifier过强的site-uniqueness
假设：两个branch block必须互异，但同一个dominant comparison可以合法复用为两个
latch的data guard。

Little-endian混合transfer正例完成1024组差分、symbolized verifier、Backsolver
模型、双seal/replay和两类tamper rejection；big-endian专项使用两个conditional
latch、共享guard、相反polarity并通过独立replay。Argument-only guard、两臂都写和
unknown ModRef的专用负例确认continuation先lower而memory tuple保持原load。F283
仍限定为恰好two-latch、strict single-diamond、constant-offset integer byte lanes。
最终v1--v12兼容组23/23、LLVM/lit 174/174（113.11秒）和Python 459/459
（106.166秒）通过。

下一实施顺序更新为：

1. bounded 3--4-latch cycle；
2. arbitrary-predicate/general-memory loop summary；
3. Hydra一般acyclic SESE DAG、tree-local PHI与multiple merge；
4. 超出F282固定上界的多union/多guard writer graph；
5. artifact可信传输、LLVM语义边界与公开confirmatory campaign。

### F284：Bounded 2--4-Latch Cyclic Byte-Lane MemoryPhi

F284完成第1项，把实际header MemoryPhi从一个entry加两个latch扩展为一个entry加
2--4个latch。每个backedge继续独立使用F281 ordinary或F283 strict conditional
partial-store/self-carry proof，lowering生成actual 3--5 incoming integer PHI，
所以运行时选择仍完全由LLVM predecessor决定，没有跨latch priority。

V13只在slot至少有一个3/4-transfer状态时启用；其他相关状态允许2--4 transfer。
每条transfer都写presence tag和kind，即使所有transfer都unconditional，proof布局
也不依赖偶然的guard存在性。Compiler replay重建N-input PHI及全部expression；
Python verifier检查2--4 ordinal、CFG ownership、共享dominant guard、lane coverage、
至少一个expanded state和fingerprint。纯two-latch继续保持v10/v12。

4-latch mixed正例完成2560组差分、symbolized verifier、Backsolver模型、双
seal/replay和两类tamper rejection；3-latch all-unconditional big-endian正例与
5-latch拒绝通过。最终continuation-memory v1--v13兼容组26/26（67.75秒）、
LLVM/lit 177/177和Python 459/459（107.757秒）通过；构建、`py_compile`、技术
索引`--check`和`git diff --check`均通过。F284仍不是任意latch数或一般MemorySSA
fixed point，这些功能回归也不构成公开target上的性能结论。

### F285：Nested-Predicate Cyclic Byte-Lane Transfer Tree

F285完成下一项循环状态缺口。一个backedge不再局限于一层store/carry diamond，
而可由最多3个内部`br i1`节点和4个叶子的unique-parent二叉树决定。每个叶子独立
证明为pure carry或same-base constant-offset partial-store/carry；lowering在原
叶子块物化完整整数，并在latch构造实际3/4-input leaf PHI。该设计不把嵌套条件
提前合取，避免改变按路径求值与LLVM poison/undef边界。

V14为每条header transfer编码ordinary、conditional或predicate-tree kind。Tree
proof包含preorder node、branch/guard site、typed child引用、canonical leaf、
NoMod链和逐byte来源；compiler replay核对实际inner PHI及表达式，Python verifier
独立检查唯一根、单位入度、前向child、站点所有权、lane完备和fingerprint。能力可
用于单latch或2--4 latch header，其他latch仍可使用F281/F283 transfer。

单latch最大4-leaf little-endian正例完成5120组差分、symbolization、Backsolver、
双seal/replay和两类tamper rejection；PowerPC64 big-endian正例验证3-leaf tree与
ordinary第二latch的混合v14编码；5-leaf形态fail closed。它仍不支持共享predicate
DAG、switch tree、任意叶数、symbolic address或一般heap fixed point。最终
continuation-memory v1--v14兼容组29/29（131.86秒）、LLVM/lit 180/180和Python
459/459（108.233秒）通过；构建、`py_compile`、技术索引`--check`及
`git diff --check`均通过。

下一实施顺序更新为：

1. 超出F282的多union/多guard writer graph；
2. Hydra一般acyclic SESE DAG、tree-local PHI与multiple merge；
3. profile binary identity、并发seal、签名/透明日志/跨主机transport；
4. poison/undef/freeze/exception与跨LLVM replay；
5. 公开等CPUconfirmatory campaign。

### F286：Bounded Ordered Pointer/Guard Writer Graph

F286完成上述第1项。MemorySSA仍按最新到最旧回溯，但现在把两类条件写统一编码为
最多4层的writer sequence：最多2个F278 depth-two pointer partition和2个F276
single-level guarded writer。Guard层记录逐lane source byte与极性，partition层
记录完整select tree。较新无条件写已经覆盖的lane会从较老layer中mask为fallback，
因此proof不会把不可见旧写重新引入。

Lowering从base byte开始，按最旧到最新应用layer，最终最新写成为actual select的
最外层。Compiler replay则反向从外向内剥离表达式，并重做MemorySSA/AA；v15
metadata、manifest与fingerprint绑定全局ordinal、kind、store/guard、逐lane极性和
partition topology。独立Python verifier还要求最多2+2的kind预算、canonical
newest-first序列、fallback/polarity一致及至少一个真正超出v7/v8/v11的状态。

最大little-endian正例包含old partition、old guard、new partition、new guard，
覆盖64种writer条件与三个continuation selector共192组差分；其中old partition
的high lane随后被无条件写覆盖，manifest必须把该旧来源mask为fallback。测试并
通过symbolization、Backsolver、双seal/replay及三类tamper rejection。PowerPC64
双partition验证大端地址序lane；第三partition fail closed。旧v7/v8/v11制品保持
原schema，兼容组v1--v15为32/32（132.21秒）；最终LLVM/lit 183/183
（135.31秒）与Python 459/459（77.937秒）通过。

下一实施顺序更新为：

1. Hydra一般acyclic SESE DAG、tree-local PHI与multiple merge；
2. profile binary identity、并发seal、签名/透明日志/跨主机transport；
3. poison/undef/freeze/exception与跨LLVM replay；
4. general symbolic-address/heap-region memory summary；
5. 公开等CPUconfirmatory campaign。

### F287：Bounded Acyclic SESE DAG and Local-PHI Predication

F287完成上述第1项的有界可执行子集。F273的unique-parent tree不再承担所有CFG：
当左右arm到最近公共post-dominator之间存在真实多前驱local merge时，compiler
改用v4 DAG分析。每侧最多10个block、4个条件、8条最终merge leaf edge、3个local
merge、8个local PHI和64条指令；稳定site ID打破Kahn拓扑排序并列。Root必须只有
outer entry前驱，其余前驱必须完全位于同一arm且指向更大的ordinal，左右arm不得
重叠。没有真实local merge的超长linear/branch chain不会借v4绕过旧上界。

Lowering先从root outer guard出发，计算每条`block_guard AND edge_condition`，
多前驱block以OR合并incoming edge guards。拓扑序确保前驱值已经映射，局部PHI再按
canonical incoming edge折叠为ITE；随后才进入predicated LCS ALU/memory alignment。
最终output PHI仍按到外部merge的真实leaf edge选择。Safe模式保持无副作用，
aggressive load/store仍是failure-preserving并依赖原binary replay。

`bounded-acyclic-sese-dag-hydra-v4` manifest记录完整predecessor/successor拓扑、
local merge/PHI身份、alignment、output和指纹。独立verifier从successor反推
predecessor和merge数，并要求local-PHI site与alignment中的LLVM PHI opcode完全
一致。最大2+1 local-merge正例与aggressive readback-store正例各完成256输入差分；
symbolization、campaign loader、seal/replay和三类tamper rejection通过。External
predecessor、cycle和第4个local merge fail closed；Hydra v1--v4兼容组9/9通过。
最终LLVM/lit 186/186（135.16秒）与Python unittest 459/459（76.879秒）通过；
LLVM 18构建、`py_compile`、技术索引、Markdown链接和diff检查同样纳入门禁。

下一实施顺序更新为：

1. profile binary identity、并发seal、签名/透明日志/跨主机transport；
2. poison/undef/freeze/exception与跨LLVM replay；
3. general symbolic-address/heap-region memory summary；
4. 超出F285/F287预算的共享predicate/CFG DAG与general-memory loop；
5. 公开等CPUconfirmatory campaign。

### F288：Profile-Bound and Concurrent-Safe Transformation Evidence

F288完成了上述第1项中的本机身份与并发部分。新
`symcc-hydra-profile-v2`不再只列遥测site：profile CLI必须接收产生遥测的原始
command，artifact封存解析后的argv、input mode、executable SHA-256和command
SHA-256，文本profile再把artifact/executable/command摘要传给compiler。Compiler
严格解析v2，缺头、重复site、无效统计或不可读文件都使Hydra fail closed；合法
空profile也不会退化成隐式选择。V1继续兼容，`SYMCC_HYDRA_SITE`继续作为明确的
实验/replay覆盖路径。

Compiler manifest通过`selection_source`区分implicit、explicit、v1和v2；v2 record
绑定profile及原始binary/command摘要。Replay-v2 campaign要求profile artifact，
逐字段验证其内部seal、manifest摘要、selected-site统计及original authority command，
因此更换原始可执行文件或参数即使重新计算campaign外层摘要也被拒绝。该闭环修复了
“profile来自A、实验却用B作权威”的实验污染风险。

并发方面，Hydra、switch、loop和continuation四个manifest writer统一到
`ManifestWriter.h`：完整payload在锁外构造，对目标文件执行exclusive advisory
`flock`，在`O_APPEND`下循环完成partial write，`fsync`后解锁。24个同时运行的
`opt`进程最终产生24/24条完整、可解析、可独立证明的Hydra record。CLI端到端测试
还覆盖v2 profile生成、compiler绑定、campaign验证及替换original拒绝；Python
Hydra专项6/6、F288定向lit 3/3和LLVM 18 Werror构建通过。并发测试连续重跑两次
保持绿色；最终全量LLVM/lit 187/187（135.52秒）与Python unittest 461/461
（77.260秒）通过。

下一实施顺序更新为：

1. Ed25519签名、append-only透明日志、inclusion proof与跨主机transport；
2. poison/undef/freeze/exception与跨LLVM replay；
3. general symbolic-address/heap-region memory summary；
4. 超出F285/F287预算的共享predicate/CFG DAG与general-memory loop；
5. 公开等CPUconfirmatory campaign。

### F289：Signed Merkle-Logged Cross-Host Transformation Bundle

F289完成了上述第1项的有界可执行子集。新
`transform_artifact_bundle.py`从F274 seal反推必须存在的artifact roles，拒绝缺失、
多余或digest不符的input/lowered IR、compiler、LLVM tool与manifest。Canonical
descriptor用独立artifact key执行Ed25519签名；public key不从bundle自信任，而由
接收端显式提供并以SPKI SHA-256固定。签名message与tree-head message使用不同domain，
避免同一key误用时的跨协议解释。

透明日志以RFC 9162的`0x00` leaf、`0x01` internal-node方式构造SHA-256 Merkle tree。
每次append先在exclusive lock内审计全部历史entry、连续index、previous-root chain、
Merkle root和signed head，再写入并`fsync`。Bundle保存发布时的signed tree head和
inclusion path；远端只持trusted artifact/log public keys即可offline验证，持有完整
log的monitor还能核对对应历史entry。8个并发publisher实测得到连续0--7 index且每个
bundle都通过log交叉验证。

跨主机载体是有界ZIP64：精确成员集合、content-addressed blob、uncompressed size、
SHA-256、嵌入F274 seal和role映射全部验证后，才向临时目录提取，逐文件/目录
`fsync`并原子rename，生成role receipt。6项测试覆盖API/CLI/keygen/audit/import、
双tree head、signature/proof/blob篡改、错误trust root、previous-root改写、角色和
两类key-pair mismatch、验签后bundle替换及重复import；定向lit 1/1通过。最终
全量LLVM/lit 188/188（134.79秒）与Python unittest 467/467（79.299秒）通过。

下一实施顺序更新为：

1. poison/undef/freeze/exception与跨LLVM replay；
2. general symbolic-address/heap-region memory summary；
3. 超出F285/F287预算的共享predicate/CFG DAG与general-memory loop；
4. 公开等CPUconfirmatory campaign；
5. F289的HSM/key rotation/revocation、timestamp与外部checkpoint gossip服务。

### F290：LLVM Semantic Refinement and Cross-Major Exact Replay

F290完成了上述第1项“poison/undef/freeze/exception与跨LLVM replay”的有界可执行
子集。Hydra不再仅依赖“LLVM verifier没有报错”这一弱条件：manifest显式记录
LLVM版本、`llvm-poison-undef-freeze-refinement-v1`契约、inactive operand安全化、
freeze实例策略和exception拒绝策略。Compiler统计两侧freeze、成对alignment和
单边extra freeze，独立verifier从alignment opcode重新推导计数。配对freeze只合并
同一个alignment slot的动态实例，不把不同slot的不确定选择折叠；单边freeze先由
outer condition屏蔽非活动值。整数div/rem的非活动除数用1，其他operand用零，从而
避免predication自身制造即时UB。含`invoke`、EH pad或非branch terminator的region
继续保留原CFG。

新`cross_llvm_transform_replay.py`以F274 seal为baseline authority。它先在封存的
LLVM/tool/plugin上完成独立exact replay，再用每个ABI匹配的candidate plugin运行同一
pass。Candidate必须通过LLVM verifier、独立manifest verifier和本版本
`llvm-diff`；规范化时只删除`llvm_version`与`llvm_major`，ordered proof identity、
其余manifest和textual IR SHA-256/size必须完全相等。证书要求至少两个major，并把
tool/plugin/artifact identity、所有gate和规范化摘要绑定进canonical自哈希结构。
LLVM 18.1.3 baseline与17.0.6 candidate已经生成持久证书，lowered IR和规范化
manifest摘要完全相同。

这轮交叉测试还揭示并修复了一个与实验有效性相关的Query链回归：native tactic
只给offset 0的partial assignment时，旧QueryStore虽计算
`query_ir_verified=false`仍会写出候选；当converter的完整赋值已经被`fixed`写入
base时，generator又不会提出该base。现在fixed-base先作为proposal进入完整Query
IR verifier，primary/verified model若不满足prefix+target则完全不物化。清空旧
SQLite/output后的32-bit等式用例稳定得到唯一正确输入`78 56 34 12`。

测试覆盖paired/one-sided freeze、inactive `udiv`、EH拒绝、manifest计数篡改、
LLVM 18→17 exact replay、same-major拒绝、major/kind/digest重哈希篡改、fixed-base
补全及矛盾partial model门禁。全量结果为LLVM 18 191/191，LLVM 17
190 passed/1 unsupported（cross-major主驱动只在18套件启用）和Python 471/471。

必须限制结论：项目声明LLVM 8--18，但本项只构建并比较17/18；官方LLVM 21.1已经
发布，19--21不在当前支持声明内。Exact textual IR相同只证明这个sealed fixture与
pass配置的结构重放，不是任意IR的refinement theorem。Exception是fail-closed拒绝
而非异常语义建模，证书自哈希也不是签名。认证传输应使用F289封装。

下一实施顺序更新为：

1. general symbolic-address/heap-region memory summary；
2. 超出F285/F287预算的共享predicate/CFG DAG与general-memory loop；
3. 公开等CPUconfirmatory campaign；
4. public transparency checkpoint witness/gossip和F289 key lifecycle；
5. LLVM 19--21适配应先扩展项目支持矩阵，再逐major运行同一exact gate。

### F291：Exact Symbolic-Index Heap-Region Writer Graph

F291完成了上述第1项的有界精确子集。Continuation MemorySSA不再只识别常量地址和
有限pointer-select：当load与动态store属于同一个常量大小`malloc/calloc`对象、
store pointer是单一pointer-width `inbounds gep i8`时，compiler把“哪一个store
byte覆盖哪一个load lane”编译为有限索引等式，而不是枚举整个地址空间：

```text
index = load_offset + load_lane - store_source_byte - writer_base_offset
```

每个load lane最多产生8个case，actual IR由`icmp eq`和`select`组成。MemorySSA
继续newest-first收集，降低时oldest-to-newest应用，因而last writer保持在表达式
外层；被较新无条件store遮蔽的旧动态case会被删除。V16同时把总writer上限从v15的
4层扩为8层，原第三个pointer partition因此可精确接管，但完全符合旧预算的制品
仍使用v15。

Proof不仅记录store和index，还绑定allocator site、region extent、index bits、
signed base offset和逐lane signed case。Compiler输出前重跑MemorySSA/AA并检查
actual `select/icmp`；独立Python verifier检查有符号值域、规范case次序和完整FNV
fingerprint。Manifest tamper、fresh-sealed lowered IR tamper及未知extent/external
base均被拒绝。

验证覆盖`malloc(8)`的16-bit动态写和32-bit readback、768组差分、负index等式、
真实Backsolver从`A 00`求得`index mod 7 = 2`、`calloc(4,2)`与非零base offset、
PowerPC64大端、动态extent/external identity拒绝，以及v16第三partition。LLVM
17/18定向组均通过；持久cross-major证书证明两者proof identity、规范manifest和
textual IR完全相同。

边界必须保持清楚：当前是固定单对象、线性MemorySSA、byte-address、2--8 byte
load、1--8 byte store和8层writer预算。它没有实现symbolic base/points-to、pointer PHI、
multi-dimensional/strided GEP、realloc/lifetime、动态extent、循环MemoryPhi array
fixed point或一般heap graph，也尚无公开target的coverage/CPU盈利性证据。

F291完成后的全量门禁为LLVM 18 195/195、LLVM 17 194 passed/1 unsupported
（cross-major主驱动只在LLVM 18套件启用）和Python 471/471。并行门禁的墙钟时间
会受同机资源竞争影响，因此这里只把通过计数作为正确性证据，不把该次耗时作为
性能结论。

下一实施顺序更新为：

1. general-memory loop：把F277--F285的constant-offset cyclic byte fixed point扩展到
   有界symbolic-index region transfer，并先定义可终止的array summary domain；
2. 超出F287预算的shared predicate/CFG DAG与跨region structural hash-consing；
3. 公开等CPUconfirmatory campaign，单独报告F291触发率、IR增长、solver query/
   CPU、Backsolver yield与coverage AUC；
4. public transparency checkpoint witness/gossip和F289 key lifecycle；
5. LLVM 19--21与一般exception语义按支持矩阵逐major扩展。

### F292：Symbolic-Region Cyclic Byte Fixed Point

F292完成了general-memory loop的第一个精确子域。它没有把循环逐次展开，也没有把
整个heap object送入SMT array，而是只维护continuation实际读取的固定byte window。
对lane `l`、store source byte `s`和迭代`t`，回边transfer为：

```text
X[t+1,l] =
  ite(index[t] = load_offset + l - s - writer_base_offset,
      byte(value[t], s),
      X[t,l])
```

Lowering把`X`实现为原load整数宽度的循环PHI；每个lane从PHI取carry byte，再应用
F291同一套有符号equality/select case。IR规模只依赖
`load_width × store_width`，不会随trip count增长；运行时每个迭代仍使用当次
index/value，因此写历史由递推PHI精确保留。

V17把cycle header/entry/backedge、entry byte state、all-carry回边和完整region
writer record绑定在同一proof。Compiler从actual PHI及其`select/icmp`链重放；
Python verifier独立检查all-carry、allocator/index/extent、signed case和FNV
fingerprint；F274 seal从原始IR重编译。标准allocator准入也同时收紧为非变参、
标准参数个数且size位宽等于pointer-index位宽，错误的同名`calloc(i32,i32)`会拒绝。

主测试覆盖270组原/变换差分、manifest与fresh-sealed IR篡改、双replay、符号化和
真实Backsolver；大端i16 writer验证source-byte方向；argument base、动态extent、
两个动态writer及常量/动态混合全部fail closed。LLVM17/18聚焦组均为6/6。持久
cross-major证书记录相同proof identity `11600885392756402730`、lowered SHA-256
`6e5e4c5c...91189`和规范manifest SHA-256 `2eb7bf05...1901`。
最终全量门禁为LLVM 18 198/198、LLVM 17 197 passed/1 unsupported
（cross-major主驱动只在LLVM 18套件启用）和Python 471/471。

边界必须保持清楚：当前只接受一个固定对象、一个固定read window、单entry、
单unconditional backedge和单dynamic writer。Conditional/multi-latch/multiple
dynamic writer、symbolic base、动态extent、一般array/heap graph、realloc/lifetime
及公开target盈利性均未实现或未证明。

下一实施顺序更新为：

1. shared predicate/CFG DAG：突破F287的unique-parent与局部预算，同时保持actual
   ownership proof和有界hash-consing；
2. 在证据支持后扩展F292到conditional/multi-latch dynamic transfer；先定义writer
   冲突次序与成本预算，不直接开放一般array；
3. 公开等CPUconfirmatory campaign，报告F291/F292触发率、IR增长、solver query/
   CPU、Backsolver yield与coverage AUC；
4. public transparency checkpoint witness/gossip、F289 key lifecycle及LLVM
   19--21支持矩阵。

### F293：Bounded Shared-Predicate DAG Guard Hash-Consing

F293完成了F287之后shared CFG/predicate DAG的下一层有界子域，但没有把任意CFG
伪装成可安全lowering。Compiler保持tree、v4 DAG、v5 DAG的严格优先级：落在F287
预算内的region继续产生原v4制品，只有旧分析失败后才尝试14-block、5-branch、
10-leaf、4-local-merge、12-local-PHI、96-instruction的v5。Single-entry、nearest
common post-dominator、arm disjointness、全部前驱闭包和严格前向topology仍是硬门。

V5同时处理了扩大证明域带来的表达式增长。每个arm用
`(predecessor ordinal, successor index)`索引canonical edge guard，同一edge在
block reachability、local PHI和final leaf中只生成一次；两个arm共享lowered
predicate反值cache。因此优化只消除结构相同的guard子表达式，不合并不同edge，
也不改变PHI incoming ownership。最大正例的18条CFG edge产生8次edge-cache hit，
5个predicate occurrence归为3个identity，其中重复predicate的false edge共享反值。

`bounded-shared-predicate-sese-dag-hydra-v5`绑定完整v4 topology和新增predicate/
guard graph。Verifier从successor row反推canonical edge数，从site序列重算unique/
reused predicate，并要求至少一个维度真实超出v4上界；结构计数和实际cache hit均
进入FNV fingerprint。13-block/4-local-merge正例完成256输入差分、symbolization、
manifest/campaign loader、seal/replay和三类tamper rejection；16-block/5-local-
merge拒绝。LLVM17/18的Hydra v1--v5兼容组均为9/9。

持久cross-major证书
`benchmark/evidence/hydra_f293_cross_llvm_certificate.json`记录proof identity
`3502535688576112958`、lowered SHA-256
`cd12b34975f22a06c2c8de323509511dc576b94693ed38f1f767df7dc0e6d700`和
normalized manifest SHA-256
`409fe0b27aaf5699d4f86f3e30f14322fad721e730f6dcbd5e13754ed17f3e9f`；
LLVM18 baseline与LLVM17 candidate通过exact IR、`llvm-diff`和manifest gate。

边界必须保留：当前hash-consing只覆盖一个selected Hydra region，predicate
negation可跨该region的左右arm共享，但不跨多个region、函数或模块；external
predecessor、switch/invoke/EH、cyclic/irreducible CFG和任意memory/call effect仍
拒绝。当前也没有公开target上IR size、solver CPU或coverage收益的等CPU结论。

下一实施顺序更新为：

1. conditional/multi-latch symbolic-region transfer：在F292 fixed-window domain
   内先定义多个dynamic writer的MemorySSA次序和per-latch冲突证明；
2. 跨selected-region structural hash-consing与多site Hydra transaction：先解决
   dominance、site选择和proof identity组合，不能直接共享裸`Value*`；
3. critical external predecessor/switch region的edge splitting与完整PHI ownership；
4. 公开等CPUconfirmatory campaign，分别报告F291--F293触发率、IR节点变化、
   solver query/CPU、Backsolver yield与coverage AUC；
5. public transparency checkpoint witness/gossip、F289 key lifecycle及LLVM
   19--21支持矩阵。

### F294：Conditional Multi-Latch Symbolic-Region Transfer

F294完成了上述第一项的声音性有界子域。设计的核心是区分两种“多个writer”：
同一latch内多个MemoryDef具有真实newest/oldest顺序，而不同latch是MemoryPhi的
互斥incoming state。当前只实现后者；如果把多个latch按ordinal串行应用，会构造
一条原CFG中不存在的执行。因此对第`j`个latch和lane `l`，先计算symbolic-region
更新`R_j`，再按store guard决定是否应用：

```text
R_j(X,l) = ite(index_j == overlap_case, byte(value_j), X_l)
T_j(X,l) = ite(guard_j, R_j(X,l), X_l)  # conditional latch
```

Header PHI从entry状态及每个`T_j`中选择一个incoming值。无条件latch省略外层guard；
多byte store的多个overlap case仍沿用F291/F292的规范有符号equality链。该表达式
只与固定2--8 byte观察窗口、1--8 byte writer和最多4个latch相关，不随trip count
增长。

V18 schema
`symbolic-region-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v18`
为每个transfer增加writer presence及完整region/index/case/guard记录。Fingerprint
先混入presence bit，再混入writer字段，防止“无writer carry transfer”与零值伪造
record混淆。Compiler重新执行MemorySSA/AA，并从actual cycle PHI逐层验证外guard、
内equality/select和最终carry；Python verifier独立检查2--4 latch、至少一个
symbolic writer、all-carry普通lane state、field exclusivity、signed case及相同
fingerprint。旧v17、v10/v12/v13制品保持规范稳定。

x86正例有两个回边：一个条件dynamic store，一个无条件dynamic store。3种
continuation selector、7种迭代次数、2种write选择和3组value共126组原/变换执行
一致；seal/replay、manifest extent/source tamper、fresh-sealed equality tamper、
symbolization和真实Backsolver均通过。PowerPC64正例达到4-latch上界并以i16 writer
验证大端source-byte映射。负例证明同latch两个dynamic writer、dynamic allocation
extent和5个latch全部fail closed。LLVM17/18聚焦组均为3/3。

持久cross-major证书
`benchmark/evidence/ifss_f294_cross_llvm_certificate.json`绑定proof identity
`2389278380394154034`、lowered SHA-256
`c2802fe2c1015fc0117954e0f74c3edcf19d6cac2d67cabd400825ebe87f3769`和
normalized manifest SHA-256
`296583a8c561d62aa2abaecca7e844a7c5edb1aa4c5dcecca7ef51638e0ca409`；
LLVM18 baseline与LLVM17 candidate通过独立manifest verifier、sealed replay、
LLVM verifier、`llvm-diff`和byte-identical gate。

最终完整门禁按顺序执行：LLVM 18 lit 202/202（134.31秒），LLVM 17 lit
201 passed/1 unsupported（134.07秒），Python unittest 471/471（79.584秒）；
双LLVM构建、`py_compile`、索引检查、证书复验和`git diff --check`同时通过。

边界仍然明确：同一latch内ordered multiwriter、nested-predicate symbolic
transfer、symbolic base、dynamic extent、pointer PHI、strided/multidimensional
GEP、一般heap graph和SMT array都未实现。当前证据也不是公开target上的盈利性
结论。

下一实施顺序更新为：

1. 跨selected-region structural hash-consing与多site Hydra transaction：建立
   function-scoped canonical key、dominance可用性和组合proof identity；
2. same-latch ordered multiwriter与nested-predicate symbolic-region transfer，
   在固定window/region域内分别定义last-writer和guard-tree预算；
3. critical external predecessor/switch region的edge splitting与完整PHI ownership；
4. 公开等CPUconfirmatory campaign，分别报告F291--F294触发率、IR节点变化、
   solver query/CPU、Backsolver yield与coverage AUC；
5. public transparency checkpoint witness/gossip、F289 key lifecycle及LLVM
   19--21支持矩阵。

### F295：Dominance-Proven Cross-Region Guard Hash-Consing

F295完成了F294之后计划中的第一个结构复用子域，也修正了一个架构事实：F293虽然
称predicate cache可在一个region的左右arm间共享，但module pass只会选择一个
candidate，所以当时不存在真正的cross-selected-region lifetime。F295以新的
`SYMCC_HYDRA_SITES`显式列出2--4个site，默认单site/profile路径保持制品兼容。

准入先于任何IR编辑完成。每个site必须独立形成F293 v5 DAG，全部位于同一Function，
arm-owned blocks两两不相交，任何entry/merge都不由另一region拥有；给定site顺序
还必须形成entry-block dominance chain。Compiler随后对所有internal conditional
的原始`Value*`求交集，交集为空或stable-site ID发生非一一映射时整批拒绝。这里
刻意使用SSA identity而不是opcode/文本hash，避免把数值偶然相同但定义域不同的
predicate合并。

真正的复用发生在function-scoped predicate-negation cache。Entry记录producer
site；同region命中直接复用，跨region命中则用变换后当前Function重新构建的
DominatorTree验证旧negation instruction支配当前outer branch。失败时只在当前
region重新生成，而不会引用非支配SSA。Per-arm edge guard仍是region-local，因此
F295没有跨region混淆PHI incoming edge或path ownership。

V6把有序transaction sites与共同predicate sites先封成transaction fingerprint，
每个record再记录ordinal、size、实际cross count和唯一producer list，并把它们混入
完整v5 structure fingerprint。Python verifier逐record重算两层FNV，再在JSONL层
要求record按ordinal连续、同函数、字段一致、首record无cross claim、后续source只
来自前序site且总复用非零。Unified seal/replay现可封存一个legacy record或一个完整
v6 transaction，重放时恢复精确site数组。旧v1--v5 verifier与seal行为不变。

主测试串联两个13-block/4-local-merge region，共享三个predicate；反值指令由独立
lowering的8条降为5条，第二record至少3次命中site 7001。256输入差分、LLVM verifier、
symbolization、双record seal/replay、actual IR重封篡改和ordinal/source/order
篡改均通过。逆dominance、重复、缺失site，以及同函数但predicate交集为空的
7001/7003组合均在编辑前拒绝。LLVM17/18 Hydra v1--v6兼容组均为11/11。

持久证书`benchmark/evidence/hydra_f295_cross_llvm_certificate.json`绑定structure
identity `8175852705754774924`/`1889164356607597381`、lowered SHA-256
`a0df2ebd9b22a950a6301330cdbdc421fbc6bf35cc3b4eab0a458c2cf957a937`和
normalized manifest SHA-256
`31a953eeb4e74106e7bbadb2e66af29fa725db8e8745cc4c43827cbf5942d4c8`；
LLVM18/17通过双record exact replay。

最终门禁按顺序执行：LLVM 18全量lit为203/203（134.10秒），LLVM 17为
202 passed加1项预期unsupported（133.26秒），Python全量unittest为471/471
（79.599秒；shell总墙钟80.120秒）。

边界必须保留：当前只共享原始predicate的一元反值，不共享edge guard、ALU/ITE、
memory或跨函数表达式；不支持非支配兄弟、overlapping/nested region或自动多site
profile选择。`util/hydra_transform.py` campaign仍是单site权威流程，因此当前没有
多site公开target盈利性结论。

下一实施顺序更新为：

1. same-latch ordered multiwriter symbolic-region transfer，在固定region/window内
   定义newest-first MemorySSA proof与有界case composition；
2. nested-predicate symbolic-region transfer，复用F285 guard-tree规范但保持
   dynamic writer预算；
3. critical external predecessor/switch region的edge splitting与PHI ownership；
4. F295多site profile/campaign protocol及公开等CPU F291--F295消融；
5. 跨函数结构reuse、LLVM19--21/exception及透明日志生命周期。

### F296：Same-Latch Ordered Symbolic-Region Multiwriter

F296关闭了F292/F294之间一个不能靠“多latch”替代的语义缺口：同一次循环迭代内的
连续dynamic store都实际执行，必须遵循最后写者优先；不同latch则是互斥incoming
state，不能串行组合。新实现只进入单入口、单无条件回边、固定`malloc`/`calloc`
allocation和2--8 byte观察窗口，并把相关MemorySSA链预算限制为2--4 writer。

MemorySSA逆向遍历首先遇到最新定义，所以proof数组规定ordinal 0为newest。对每个
byte lane，语义是`newest hit ? newest byte : ... : oldest hit ? oldest byte :
carry byte`。Lowering从oldest向newest包裹select，以便newest出现在实际表达式
最外层；重新收集制品时，compiler从外向内按newest-first剥离每条`icmp eq`和
`select`，最终必须落到同一cycle PHI的lane byte。这样writer排序既不是JSON约定，
也不是只由fingerprint间接声称，而是由actual IR结构复验。

V19 metadata先写writer count，再按newest-first写完整store/base/extent/index/
base-offset/signed overlap cases。Manifest字段`symbolic_region_writers`为每条记录
增加连续ordinal。独立Python verifier要求2--4条、store site互异、共享allocation
identity/extent、all-carry基础状态，并拒绝v17记录夹带v19数组或反向夹带legacy
单writer字段；随后按相同次序重算continuation proof fingerprint。V17单writer、
v18互斥multi-latch以及v16 acyclic writer graph均保持兼容。

小端正例每次迭代执行old/middle/new三个dynamic store，其中old和middle命中同一
地址，new写`index+1`。105组差分覆盖3个selector、0--6次迭代和5组writer values，
并通过LLVM verifier、SymCC symbolization、真实Backsolver、legacy/unified
seal/replay、writer顺序/extent篡改和fresh-sealed actual equality篡改。PowerPC64
正例以两个i16 writer验证大端source-byte映射；5 writer和writer间unknown ModRef
保留原load。LLVM 17/18的v16--v19兼容组均为12/12。

持久证书`benchmark/evidence/ifss_f296_cross_llvm_certificate.json`绑定proof
identity `3118488025292340061`、lowered SHA-256
`cd776afe464d40533caae83d942d1eded06b5174652585128feae7ad313e03c5`和
normalized manifest SHA-256
`d83e318f2ec82a93aab8894eab29c278b5a91a9254a1470cb35221c4f7fabe55`；
LLVM 18 baseline和LLVM 17 candidate通过sealed replay、独立manifest verifier、
LLVM verifier、`llvm-diff`和byte-identical gate。

最终门禁顺序执行：双LLVM Werror构建通过；LLVM 18全量lit为206/206
（135.31秒），LLVM 17为205 passed加1项预期unsupported（139.22秒），Python
全量unittest为471/471（80.736秒）。

边界仍然是有界且可证的：不支持nested-predicate/conditional same-latch writer、
每个multi-latch transfer内多个writer、symbolic base、dynamic allocation、
pointer-PHI、跨对象heap graph或一般SMT array。下一技术顺序为nested-predicate
symbolic-region transfer、critical predecessor/switch region、多site profile与
公开等CPU消融；按用户要求，F296完整收口后暂停，不自动进入下一项。
