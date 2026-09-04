# F459：代码、架构与算法深度审查

> 日期：2026-08-28  
> 范围：并行 hybrid 主链路、QueryStore、持久 live-state frontier、全局 coverage owner、并行规模模型、运行时并发边界与交付可复现性。  
> 性质：本轮只做审查、反例验证和优化排序，不修改生产实现。文中的“通过测试”不等于“没有发现缺陷”。

## 1. 结论摘要

当前系统已经形成清晰的五层结构：LLVM 编译与插桩、SymCC/QSYM 运行时、MPI worker 计算面、Master 权威裁决面、持久化与实验分析面。内容寻址、稳定文件读取、generation/token fencing、严格 worker 结果准入和能力闭合测试门禁是当前实现中最可靠的部分。

本轮仍确认了四项可执行反例能够触发的正确性缺陷，以及两类会限制继续扩展的架构问题：

1. QueryStore 在 SQLite 标记 `done` 后才发布结果文件和候选输入，提交后的进程失效会留下不可重新领取、但派生制品不完整的查询。
2. coverage owner 的公共 delta 归一化会把 `-1` 转成 `0xff`，造成虚假覆盖；它也没有限制最大 bitmap 索引。
3. live-state frontier 的 `complete`/`abandon` 只比较 token，没有像 heartbeat 一样核对 owner、worker、claim generation 和租约时间单调性。
4. 并行规模分析器静默排除失败/超时样本，并接受不可能的 MPI 角色配置，可能给出幸存者偏差或基于虚构并行度的上限。
5. coverage 分片提交在持锁区内反复扫描全部协调器心跳和全部待恢复 WAL，分片数与 coordinator 数增加后会出现锁放大。
6. hybrid Master 仍是 4856 行的单体事件循环；结果接收、验证、triage、coverage 提交、策略反馈和持久化共享同一服务线程，是进一步提高 worker 数时最明确的排队瓶颈。

此外，当前工作树有 24,297 个未跟踪文件，`query_store.py`、`distributed_state.py`、`live_state_frontier.py` 和规模分析器等核心实现均不在 `git ls-files` 中。即使本机测试通过，从干净 clone 或 CI checkout 也不能重现当前系统。这是形成“可测试版本”前必须先解决的交付阻断项。

## 2. 当前架构与执行主链

```text
源程序
  -> LLVM Pass / Symbolizer
  -> 插桩目标 + SymCC runtime
  -> MPI worker 执行输入、收集路径约束、求解并产生候选/telemetry
  -> Master 严格验证 worker result 和当前 generation/lease
  -> concrete showmap/终止状态复核
  -> 全局 coverage owner 裁决新颖度
  -> corpus/crash/hang 持久发布
  -> scheduler、TopSeed、Prefix-DAG/MDP、agentic policy 接收确定性反馈
  -> 新任务再次派发给 worker

旁路持久服务：
  QueryStore：Query IR、SMT 制品、solver lease、结果与候选
  Live-state CAS/frontier：checkpoint、ready/leased/done、搜索策略快照
  Distributed state：跨 Master work/target/coverage 所有权与恢复日志
  Benchmark/evidence：实验协议、原始数据、删失统计、USL/coverage 模型
```

核心边界总体合理：worker 负责昂贵且可重做的计算，Master 保留 coverage 和最终 corpus 准入权；持久状态使用内容身份、代次和租约阻止旧结果覆盖新状态；LLM/agent 只影响建议与优先级，不应直接决定 SAT、coverage 或持久提交。

## 3. 分级发现

### P0：当前工作树无法由版本控制重现

**证据。** `git ls-files --others --exclude-standard | wc -l` 返回 24,297；对下列核心文件执行 `git ls-files --error-unmatch` 均失败：

- `util/query_store.py`
- `util/distributed_state.py`
- `util/live_state_frontier.py`
- `benchmark/analyze_parallel_scaling.py`

CI 在 [`.github/workflows/run_tests.yml`](../../../.github/workflows/run_tests.yml) 第 36--38 行从 Git checkout 开始，因此不会得到这些未跟踪实现和相应测试。本机生成的 1576-node manifest 不能替代版本化输入。

**影响。** 当前测试结论只证明本机工作树，不证明提交、分支、归档包或 CI 中的版本。任何后续性能比较也无法绑定到唯一代码基线。

**修复顺序。** 先建立受审文件清单，区分源码、测试、文档和生成 evidence；把源码、测试、固定 manifest 与子模块提交绑定，生成数据留在有 seal 的外部制品；最后从干净 worktree 重跑能力门禁。

### P0：QueryStore 存在提交后派生制品丢失窗口

**代码。** [`util/query_store.py`](../../../util/query_store.py) 第 5873--5969 行先在一个 SQLite 事务中把查询置为 `done` 并写入 `results`；第 5970--5997 行才写 result JSON、generator 和候选输入。构造函数第 1322--1356 行只初始化目录和 schema，没有扫描 `done JOIN results` 的启动补偿。再次 ingest 同一 query 时第 2704--2712 行能够修复，但系统不能保证相同 query 会再次到达。

**反例。** 在第一个 result 文件 `_atomic_write` 处注入 `OSError`：

```text
completion_exception = simulated post-commit crash
db_done = 1
result_file = False
reclaimable = False
```

`solve_claimed` 第 9226--9239 行会捕获异常并调用 `fail`，但查询已经不是 `leased`，无法回退或重做。SAT 查询在同一窗口会进一步丢失本应送入 fuzzing corpus 的候选。

**建议。** 在 SQLite 内增加 durable outbox/publication 状态：事务只记录逻辑结果和待发布任务；幂等发布器以内容哈希写文件和候选；全部副作用完成后再将 publication 标记完成。启动时必须重放未完成 outbox，审计时校验 `done`、result、candidate 三者闭合。

### P1：coverage delta 归一化会制造虚假新颖度

**代码。** [`util/distributed_state.py`](../../../util/distributed_state.py) 第 2578--2596 行使用 `int(bits) & 0xff`，并静默跳过格式错误的行。`[(5, -1)]` 因此变为 `[(5, 255)]`。最大 index 也没有上限；后续 [`util/mpi_fuzzing_helper.py`](../../../util/mpi_fuzzing_helper.py) 第 1835--1845 行会按 index 扩展 dense bitmap。

**反例。** 实际调用 `claim([(5, -1)])` 返回 8，并持久化 `(5, 255)`，即凭空产生 8 个 coverage features。

主 worker 协议第 2643--2668 行已有严格检查，因此正常 MPI 结果不直接触发该反例；问题在于 coverage authority 自身的契约比调用者弱，其他恢复、测试或新入口可绕过上游检查。

**建议。** authority 层必须独立失败关闭：要求 `type(index) is int`、`type(bits) is int`、`0 <= index < negotiated_map_size`、`1 <= bits <= 255`、index 唯一；任一错误拒绝整个候选或 batch，不能截断、取模或静默丢行。

### P1：frontier 完成与心跳使用了不同强度的租约栅栏

**代码。** [`util/live_state_frontier.py`](../../../util/live_state_frontier.py) 第 659--719 行的 heartbeat 比较 token、owner、worker、claim generation、expiry 单调性和当前有效期；第 775--840 行的 complete 与第 842--903 行的 abandon 只比较 token 和权威 expiry。

**反例。** 保留合法 token，但替换 `owner="forged"`、`worker=99`、`claim_generation += 100`、`expires=0.1`，`complete` 仍返回 `completed`。

**影响。** token 仍阻挡普通旧租约，因此这不是任意第三方接管；但同一 API 对同一 lease 对象采用不一致的身份语义，调用者状态串线、反序列化错误或未来协议扩展可能被静默接受。

**建议。** 抽取单一 `_matches_authoritative_lease`，heartbeat、complete、abandon 和 recovery 共同使用；比较所有不可变字段，并只允许权威 expiry 相对调用者不回退。

### P1：并行规模分析存在幸存者偏差和角色账本缺口

**代码。** [`benchmark/analyze_parallel_scaling.py`](../../../benchmark/analyze_parallel_scaling.py) 第 68--150 行在第 84--85 行直接丢弃非 `success` 行，输出中没有失败率、删失比例或可靠性门禁。第 86--113 行只单独检查各角色数量，没有验证角色之和与 `np` 一致。

**反例一。** 输入四个成功点，并在最高并行度加入 30 个 timeout，loader 仍只返回四个成功点，高并行度只有一个“成功观测”进入 USL。

**反例二。** `mpi,np=1` 被解释为 1 个 worker、0 个 Master；`hybrid,np=9,num_workers=8,num_masters=8,afl_instances=8` 被解释为 16 个 compute workers。两者在物理上均不可能。

**影响。** 当前仓库 51 份 `benchmark_data.csv` 的 235 行均为 success，因此这些反例没有改写既有数值；但分析器无法在未来失败实验中保持结论可信。

**建议。** 输入层严格验证模式角色方程；按配置输出 attempted/success/timeout/failed；性能拟合要求预注册的完整 repeat/block，失败使用可靠性门禁或删失模型，不能仅删行。若任一规模成功率低于阈值，应拒绝推荐 ceiling。

### P1：coverage 分片协议在锁内发生乘法级元数据扫描

**代码。** `claim_many` 第 2637--2676 行持有全部 touched shard locks，并对每个变化 shard 调用 `owner_for_shard`；后者第 2202--2253 行扫描全部 coordinator heartbeat 文件。16 个变化 shard、16 个 coordinator 会尝试 256 次稳定文件读取。`_locked_shards_after_recovery` 第 2547--2574 行还会在持锁后扫描并解析整个待恢复事务目录。

**复杂度。** 一批提交的元数据成本近似：

```text
O(changed_shards * coordinator_count + pending_WAL_bytes)
```

并且该成本位于锁的占用时间内，远端并行文件系统上的 tail latency 会直接阻塞其他 Master。

**建议。** 每批在加 shard 锁前只计算一次带 generation/TTL 的 live coordinator snapshot；WAL 建立 shard -> transaction 的有界索引，正常无故障路径不扫描全部目录；把 fsync、锁等待和 recovery scan 的 P50/P95/P99 暴露到现有 profile。

### P2：Master 事件循环仍是主要扩展上限

[`util/mpi_fuzzing_helper.py`](../../../util/mpi_fuzzing_helper.py) 的 `master` 从第 4776 行延续约 4856 行，结果批次在第 8478 行以后依次完成接收、协议校验、lease commit、telemetry 聚合、agentic 回馈和 `_batch_triage`；后者本身从第 4111 行起约 571 行，并执行 showmap 复核、文件发布、coverage claim 和策略观察。

批处理和异步 showmap 已经降低固定开销，但控制面仍可近似为一个单服务台：

```text
lambda_worker_result + lambda_AFL_entry < 1 / E[S_master]
```

当到达率接近服务率时，增加 worker 只会增加队列等待和重复候选，不会线性增加有效覆盖。

**建议。** 保持一个最终状态机，但把 admission、concrete verification、coverage commit 和 policy observation 拆为有界 mailbox service；消息只传内容身份、租约和小型 delta。先以 shadow pipeline 比较新旧裁决一致性，再逐步启用并发服务，避免一次性重写 10k 行控制器。

### P2：规模模型只有点估计，统计结构没有进入拟合

[`benchmark/analyze_parallel_scaling.py`](../../../benchmark/analyze_parallel_scaling.py) 第 52--65 行对每个配置独立 bootstrap，`_summaries` 第 153--191 行只按并行度/角色数分组，不保留 round、seed、A-B-A block 或 campaign 时间。[`util/parallel_scale_model.py`](../../../util/parallel_scale_model.py) 第 107--125 行允许四个并行度拟合三个 USL 参数；四点只满足最低可识别条件，不能提供稳定的外推不确定度。

**建议。** 至少五到六个规模点；按 seed/round/block 分层 bootstrap 整条曲线，报告参数和 ceiling 的分布；使用 held-out prediction error、残差图和失败率门禁。非单调 endpoint coverage 不应强行拟合，可采用单调潜变量/区间结论，但不能把随机均值波动直接解释为机制回退。

### P2：多线程符号执行能力边界必须继续保持显式

QSYM runtime 的 solver、expression builder、expression map 和 shadow pages 是进程全局状态；项目文档已在 [`docs/Configuration.txt`](../../Configuration.txt) 第 3208--3215 行明确把并发 schedule-only artifact 与 input/path solving artifact 分离，避免假设 QSYM shadow map 可被目标线程并发访问。simple backend 的 [`runtime/src/backends/simple/Runtime.cpp`](../../../runtime/src/backends/simple/Runtime.cpp) 第 55--65 行也保留 global solver 的 thread-local TODO。

这属于已经文档化的能力边界，不应误报为当前 schedule-only 路径的回归；但任何“原生多线程目标上同时做路径求解”的新功能，都必须先实现 per-thread expression context 或全局线性化协议，并增加 ThreadSanitizer/真实 pthread 差分测试。

## 4. 验证结果

### 4.1 现有回归

本轮重新运行四个高风险模块：

```text
test/test_query_store.py
test/test_distributed_state.py
test/test_persistent_live_state_frontier.py
test/test_parallel_scale_model.py

233 passed, 51 subtests passed in 25.44s
```

2026-08-27 的完整基线仍为：Python 1576 passed + 581 subtests；LLVM lit 344 passed、1 capability-labeled unsupported。工作树在本轮未修改生产实现。

### 4.2 为什么测试通过仍发现错误

四个反例均位于现有测试未覆盖的组合边界：

- SQLite commit 成功、随后派生文件发布失败；
- authority API 直接收到负 coverage bits；
- 合法 token 搭配篡改的其他 lease 字段；
- 规模 CSV 同时包含成功和大量超时，或角色数互相矛盾。

因此下一轮测试重点不应继续增加同类 happy-path 数量，而应增加状态机 kill-point、跨层契约 differential test 和带失败样本的统计 property test。

## 5. 优先实施路线

### 第一阶段：形成可重现且不会静默丢结果的版本

1. 清理并版本化源码/测试基线，干净 clone 重跑完整门禁。
2. 为 QueryStore 增加 transactional outbox、启动 reconciliation 和 kill-point 测试。
3. 统一 coverage authority 严格 schema，并为 bitmap index 设置协商上限。
4. 统一 frontier 四类操作的 full lease fence。

### 第二阶段：修正实验决策输入

1. 规模分析记录所有状态，验证角色账本和 repeat 完整性。
2. 引入可靠性门禁、分层/配对 bootstrap、ceiling 置信分布与 held-out error。
3. 把代码 commit、子模块 commit、环境、seed、campaign id 和分析器版本绑定进 evidence seal。

### 第三阶段：解除并行控制面上限

1. 缓存 coordinator liveness，建立 shard-indexed recovery WAL。
2. 将 Master 热路径拆成有界 mailbox pipeline，并保留单一最终裁决器。
3. 用 queue wait、service time、duplicate ratio、accepted coverage/CPU-second 驱动 worker 弹性；LLM 只负责阶段变化诊断和策略建议，结果必须经过确定性 policy gate 与 shadow arm。

## 6. 最终判断

当前实现不是“整体设计错误”：其身份、租约、内容寻址和失败关闭思想已经形成一致框架，且大量机制测试有效。问题在于少数跨持久层提交窗口和跨模块契约没有完全闭合，同时单 Master 的服务台结构已接近继续扩展时的主导成本。

在修复 P0/P1 之前，不应新增更多高复杂度 SOTA 策略，因为新策略会继续放大不可重现基线、派生结果丢失和不可信规模决策。完成前两阶段后，最有价值的研究增量不是再叠加一个启发式，而是把“有效 coverage/CPU-second”做成带可靠性约束的闭环资源控制问题，并用真实长时、等 CPU、跨 seed 的公开目标验证。
