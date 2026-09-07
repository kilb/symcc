# F466：第六轮深度审查——运行时语义、并行扩展与实验有效性

- 日期：2026-09-04
- 审查基线：主仓库 `e0344dc`，runtime 子模块 `c75f5ed`
- 范围：LLVM 插桩与 runtime、QueryStore、MPI 结果接收、AFL 覆盖/终止状态、
  solver portfolio、CAS 生命周期、benchmark 资源控制与规模上限模型
- 方法：控制流逐路径审查、合法 LLVM IR 最小反例、锁竞争与异步乱序注入、
  本机 AFL++ 4.40c 行为核验、现存实验 artifact 复算

## 1. 总体结论

当前主线的正常回归仍然稳定，但本轮通过测试套件之外的反例确认了 **3 组 P0
语义/租约错误、7 组 P1 并行或测量错误，以及 4 个 P2 长期运行与模型改进点**。
其中最紧迫的不是再增加调度启发式，而是先修复合法 LLVM intrinsic 会使符号运行时
崩溃、短 lease 返回时已经失效、batch showmap 丢失逐输入终止状态这三条正确性链路。

并行架构已经具备 dispatch generation、READY/STOP/ACK fence、CAS 输入传输、异步结果
复验、分布式 coverage claim 和持久 query service 等完整部件；当前扩展瓶颈主要落在三个
串行化点：QueryStore 在全局 SQLite 写锁内读取/散列大 artifact，结果接收只提交完成队列的
有序前缀，以及高并行绑核把 master 的辅助线程和 query service 一起限制在单核。

新版规模分析器能拒绝证据质量不足的结论，这是正确行为。但仓库里尚无一组通过 v6 门控的
真实并行规模实验；现有数据只能支持探索性瓶颈定位，不能支持“已验证规模天花板”的汇报表述。

## 2. 已确认缺陷（按优先级）

### P0-1：`claim()` 可能返回已经过期的 QueryStore lease

`QueryStore.claim()` 在获取 SQLite 写锁之前计算 `now` 和 `lease_until`
（`util/query_store.py:3231-3233`），随后才执行 `BEGIN IMMEDIATE`
（`3277-3279`）。事务持锁期间还要验证 query JSON，并对 full/prefix/target 三个 artifact
执行完整读取、SHA-256 和 sealed memfd 构造（`3302-3321`），最后才写入 deadline 并提交
（`3322-3347`）。锁等待和 artifact I/O 都会消耗租期。

反例让另一个连接持有写锁 250 ms，再以 50 ms 租期领取。调用耗时 335 ms；返回瞬间
`query_lease_is_active()` 已为假，另一 worker 随即以 token 2 领到同一 query：

```text
elapsed_seconds=0.335
first_token=1
first_active_on_return=false
second_token=2
same_query=true
```

这会制造重复求解和 stale commit。修复应在取得写锁后重新取时钟，并把 lease deadline
尽可能靠近原子 UPDATE；更彻底的方案是将大 artifact 的验证/密封移出全局写事务，使用
“预验证 + 身份重验 + 短事务领取”或“短事务领取 + 失败时 fenced requeue”。

### P0-2：合法 `llvm.fshl/fshr` 会在符号运行时崩溃

`runtime/src/RuntimeCommon.cpp:473-487` 先构造 `2N` 位 concat，却直接用 `N` 位 shift
表达式执行移位；SMT 位向量移位要求两个操作数同宽。随后 `_sym_extract_helper(..., 0,
bits)` 还把 high/low 顺序传反。simple 和 QSYM backend 的接口均为 high bit 在前
（`runtime/src/backends/simple/Runtime.cpp:496-498`、
`runtime/src/backends/qsym/Runtime.cpp:422-424`）。

一个包含符号 i8 和 `llvm.fshl.i8` 的合法 IR 可以成功插桩，但 QSYM 执行以 255 退出并报告
左右位宽不相等。正确实现应把 `shift % N` 零扩展到 `2N`；`fshl` 从移位后的 concat 提取
`[2N-1:N]`，`fshr` 提取 `[N-1:0]`。现有 continuation 测试只检查常量 lowering，不执行
runtime，因此没有发现该错误。

### P0-3：无符号 overflow intrinsic 同时存在 sort 错误和乘法判定错误

无符号 add/sub/mul 在 `runtime/src/RuntimeCommon.cpp:418-466` 将提取出的 `BV1` 与
`_sym_build_true()` 的 SMT Bool 直接比较。二者 sort 不兼容；符号 i8
`llvm.umul.with.overflow.i8` 已复现 Z3 exception 和进程 abort。

add/sub 应通过 `_sym_build_bit_to_bool()` 把额外位转换为 Bool。mul 还有第二个独立错误：
当前只检查 `2N` 位乘积的最高一位。无符号乘法溢出条件应是整个高 `N` 位非零；例如 i8
的 `16 * 16 = 0x0100` 已经溢出，但 bit 15 为零，单比特判定会漏报。修复后必须用
symbolic operand 执行六种 add/sub/mul signed/unsigned runtime 测试，而不能只检查 IR
转换结果。

### P1-1：batch showmap 把覆盖图误当成逐输入终止状态

`batch_showmap_edges()` 声称崩溃/超时输入不会生成 map
（`util/mpi_fuzzing_helper.py:4713-4718`），但本机 AFL++ 4.40c 的 `-I` 模式会为崩溃输入
照常写 map，且整个批次返回 0。实测 abort 输入和正常输入各生成一张包含 2 条边的 map。

worker 默认 `SYMCC_BATCH_VERIFY_NEW=0`（`4201-4227`），只在 map 缺失或显式开启且覆盖新增
时才回退到带 status 的流式执行（`4401-4427`）。结果是：有新覆盖的异常终止输入被当成普通
语料，没有新覆盖的异常终止输入被直接去重。覆盖 bitmap 不能编码进程终止原因。生产修复应
让 batch 协议返回每个输入的 status sidecar，或对所有候选执行有界 status 复验；仅把默认
开关改为 1 仍会漏掉无新覆盖的异常终止输入。

### P1-2：父输入的 signal 返回码按 shell 约定解析，与 `subprocess` 不符

`subprocess.run()` 对信号终止返回负信号号，例如 SIGABRT 为 `-6`；当前 `_batch_triage`
却只用 `retcode > 128` 保存该输入（`util/mpi_fuzzing_helper.py:5536-5549`）。直接启动和经
GNU `timeout` 启动的 abort 目标在 Python 中都复现为 `-6`，因此父输入的真实 signal
终止不会进入对应目录；反而程序主动 `return 139` 可能被误判。应统一为结构化 execution
outcome：负数表示 signal，124/有明确 orchestrator provenance 的 137 表示 timeout，正常
正退出码不自动等价于 signal。

### P1-3：高并行绑核使用整机 CPU 编号，破坏研究协议的 cpuset

`benchmark/run_benchmark.py:1506-1522` 使用 `os.cpu_count()` 并假设 AFL 占用
`[0, afl_instances)`、SymCC 占用整机最高编号的一段。研究协议则把子实验限制到
`sched_getaffinity()` 返回集合的前 `cpu_cores` 个核（`benchmark/research_protocol.py:
1575-1599`）。被限制的进程中 `os.cpu_count()` 仍返回整机核数。

本机反例将父进程限制到 `0-31` 后计算 19 个 AFL + 13 个 MPI rank，得到 SymCC 列表
`179-191`，13 个编号全部在有效 cpuset 之外。worker 的
`os.sched_setaffinity()` 失败又被静默忽略（`util/mpi_fuzzing_helper.py:1317-1335`），所以
日志可以宣称已隔离，实际没有隔离。应基于 `sched_getaffinity()` 的实际集合分配，显式给
AFL 和 MPI 两组进程设置互斥 affinity，并将任何绑核失败升级为实验失败而非静默降级。

### P1-4：辅助并行池被绑在 master 单核，资源账本与执行拓扑不一致

每个 MPI rank 在进入 master/worker 逻辑前被固定到一个核
（`util/mpi_fuzzing_helper.py:11919-11927`）。之后 master 创建 result-admission、coverage、
density、aux-showmap 线程池，并启动带多个线程的 query-service 子进程；线程和子进程均继承
master 的单核 affinity。与此同时，系统把这些并发度相加为
`auxiliary_compute_slots`（`8146-8165`），规模模型又把它们视为独立占用的物理核。

这既会把 CPU 密集型验证/求解串在 coordinator 核上，也会错误估计资源天花板。修复需要一份
真正可执行的 CPU placement plan：master 一核、worker/AFL 各自一核、辅助池使用显式预留
cpuset；若没有为辅助池分配独立核，就必须记作 master 内并发而不是额外物理 slot。可选的
honggfuzz `-n 2` 和 Grimoire 进程也未进入该 ledger（`benchmark/run_benchmark.py:
2437-2468`），启用时会进一步破坏等核对比。

### P1-5：QueryStore 的全局写锁包住三次大 artifact I/O

`_sealed_verified_artifact()` 会读取、散列、复制并密封完整 artifact
（`util/query_store.py:2935-2954`）；`claim()` 在 `BEGIN IMMEDIATE` 内连续执行三次
（`3277-3321`）。4 个线程领取 4 个互不相同 query，并给每次密封注入 50 ms 延迟时，12 次
密封的最大并发数仍为 1，总墙钟 0.801 s。这证明所有 solver 的领取入口被单一 SQLite 写锁
串行化；artifact 上限达到 128 MiB 或位于共享文件系统时会成为明显扩展上限。

应将内容验证移到锁外，仅在短事务内验证 query/artifact identity 与 token 并更新 lease。
需要保留 sealed descriptor 与 digest 的不可变绑定，不能为性能退回到“先看路径、后相信内容”。

### P1-6：结果复验的有序前缀提交造成接收端队头阻塞

`_HybridResultAdmissionService.has_ready()` 只检查 deque 首项
（`util/mpi_fuzzing_helper.py:3571-3572`），`collect_ready()` 遇到第一个未完成 future 就停止
（`3581-3587`）。master 只有 admission 有容量时才继续接收新结果（`9501-9506`）。注入“慢的
第一项、快的第二项”后，第二项 future 已完成，但 `has_ready=false`、收集数为 0、可用容量为
0，直到第一项结束才一次提交 `[1,2]`。

worker 的不同 dispatch generation 可以先独立完成内容复验。建议把“并行验证完成队列”与
“需要确定性的状态提交序列”分离：任何已完成验证先释放 admission capacity，再由小型 sequencer
按需要提交会影响全局顺序的事件；无全局顺序依赖的统计和失败回执可立即处理。

### P1-7：大于 128 位的合法整数被静默降为 128 位表达式

`compiler/Symbolizer.cpp:2418-2445` 的注释说明最大支持 128 位，但代码对所有 `bits > 64`
都调用 `_sym_build_integer128`，没有拒绝 `i129/i256`。同时通用整数 ABI 的位宽参数仍是
`uint8_t`（`runtime/include/RuntimeCommon.h:52`），饱和算术 helper 也使用 `uint8_t`。
合法 i256 `llvm.uadd.sat` 可以成功插桩，运行时随后因表达式位宽不一致退出。

最低风险修复是在编译器对 `>128` 位路径显式 concretize/诊断，避免产生内部不一致表达式；
完整方案是统一使用足够宽的 bit-width ABI，并通过已有
`_sym_build_integer_from_buffer(..., unsigned num_bits)` 构造任意宽 APInt 常量。

## 3. P2 风险与进一步优化

### 3.1 Portfolio 的 cancel grace 不约束实际返回延迟

`PortfolioSolver` 在 SAT winner 后等待 grace 并调用 `future.cancel()`/solver `cancel()`，但随后
仍对全部 pending future 执行 `future.result()`，最终 `shutdown(wait=True)`
（`util/query_store.py:9555-9597`）。一个快速 SAT solver 加一个不响应取消、睡眠 400 ms 的
solver，在 `cancel_grace_ms=5` 时仍用 401 ms 才返回。对于内置可中断 solver 影响较小，但
当前通用 callable 契约并不保证可中断。应强制 deadline/cancel 契约，或用可回收进程隔离；
线程池无法安全终止正在运行的 Python callable。

### 3.2 `.result_objects` 在运行期没有回收协议

worker 将结果对象持续写入 campaign 级 `.result_objects`
（`util/mpi_fuzzing_helper.py:11533-11560`），只有所有 worker 干净 ACK 后才整目录删除
（`10672-10678`）。`ContentAddressedInputStore` 只有有界 inventory，没有 delete/GC API。
长时间运行、异常退出或恢复 campaign 会无限保留已消费 transport object。应为在途
dispatch/result 建 pin/refcount，提交或拒绝后释放，并以 generation + grace 扫描孤儿；不能在
不知道在途引用时按 mtime 直接删除。

### 3.3 dense coverage merge 的 master 成本随 map 大小线性增长

`CoverageBitmap.merge_delta()` 对完整 map 每次构造两个 Python 大整数
（`util/mpi_fuzzing_helper.py:1937-1957`）。重复 map 的本机微基准为：64 KiB 0.033 ms、
1 MiB 0.481 ms、8 MiB 3.982 ms；8 MiB dense fallback 单此一步上限约 250 次/秒。正常 sparse
路径没有该问题。应先增加 dense/sparse 次数和字节量遥测；若真实 profile 证明 dense 占比高，
再采用分块 native/SIMD OR，而不是无依据引入 NumPy 依赖。

### 3.4 覆盖饱和模型对随机噪声过于刚性

`fit_coverage_saturation()` 要求各并行度的聚合覆盖严格单调，否则直接拒绝
（`util/parallel_scale_model.py:386-390`）。这能防止用错误曲线形成正式决策，但随机 fuzzing
的有限轮均值可能轻微下降，现有 synthetic 数据正因此无法拟合。后续可在保持 fail-closed
决策门的前提下，对每个 paired seed 建增量模型，或先做带权 isotonic regression，再对单调
潜变量拟合饱和曲线；必须继续输出原始均值与违反单调性的幅度，不能用平滑隐藏负结果。

## 4. 已核对且不应误修的路径

- MPI 非法 result 不会立即释放 worker：同 token 的 READY 由 generation gate 识别，随后走
  `_recover_active_dispatch()`。这是有意的 fenced lifecycle，不是 worker 永久丢失。
- multi-master corpus 先 staging、再 coverage claim、再 rename/目录同步，journal 也先提交事务
  后发布 shard；当前顺序符合失败原子性约束。
- 覆盖饱和模型以最小观测并行度为基点而非直接使用 seed 点，数学上可重参数化为同一指数曲线；
  `seed_edges` 主要承担边界和 provenance 校验，不能仅凭“参数未进入预测式”判为错误。

## 5. 实验与证据状态

### 5.1 本轮验证

| 项目 | 结果 | 结论 |
| --- | --- | --- |
| QueryStore + scale model 现有测试 | 69 passed + 50 subtests，22.34 s | 基线绿，但不含本轮反例 |
| lease 锁等待反例 | 返回时 lease 已失效；同 query 被 token 2 立即领取 | P0 已复现 |
| QueryStore 密封并发反例 | 4 claim、12 次密封，最大并发 1，0.801 s | 全局串行化已复现 |
| admission 乱序反例 | 第二项完成但容量仍为 0 | 队头阻塞已复现 |
| portfolio 取消反例 | grace=5 ms，实际 401 ms | grace 不限制尾延迟 |
| i8 funnel shift | 插桩成功，QSYM runtime 以位宽断言退出 | P0 已复现 |
| i8 unsigned overflow | Z3 Bool/BV sort exception，进程 abort | P0 已复现 |
| i256 saturation | 插桩成功，runtime 位宽断言退出 | P1 已复现 |
| AFL++ batch status | abort/normal 均生成 map，批次 rc=0 | API 假设被证伪 |
| 32-core cpuset 绑核 | 计算出 13 个全部越界的 CPU ID | 实验隔离失效已复现 |

### 5.2 规模模型证据边界

当前 `analyze_parallel_scaling.py` v6 要求完整的 paired seed block、至少 5 个规模点、每点至少
3 个独立 seed、相同 wall budget、稳定角色比例、完整 coverage provenance 和一致辅助资源。
这是合理的最低门槛，并提供 paired cluster bootstrap 置信区间。

仓库现存 synthetic 与 GFTS scale artifact 仍是旧采集数据；用新版规则复析后都为
`decision_eligible=false`。具体原因包括重复 seed、角色由旧字段推断、覆盖 provenance 缺失、
wall budget 不等，以及 hybrid AFL:SymCC 比例变化。旧报告中的 7、9、49、50 等 ceiling 只能
标为 exploratory，不能作为生产部署或科研结论。必须在修复 CPU placement 后重新执行完整
矩阵，模型才有资格回答“扩到多少 worker”。

## 6. 建议实施顺序与验收标准

1. **P0 runtime 语义**：修复 overflow 与 funnel shift；新增 LLVM 17/18、simple/QSYM、
   symbolic operand、关键边界值的真实插桩执行测试。验收要求不是“不崩溃”，而是生成输入能
   命中与 LLVM concrete oracle 一致的分支。
2. **P0 lease 时钟与事务缩短**：先修返回即过期，再迁移 artifact I/O；增加真实线程竞争、
   128 MiB 上限和 stale-token 回归。验收记录 claim p50/p95/p99、锁等待、重复领取率。
3. **终止状态统一**：建立 batch status sidecar 和统一 outcome 类型；覆盖正常退出、正非零退出、
   signal、per-input timeout、orchestrator kill。禁止从 bitmap 推断终止状态。
4. **CPU placement/资源账本**：从有效 affinity 集合生成可验证 placement，辅助池和可选引擎全部
   入账；启动后读取每个 PID/TID 的实际 affinity 作 artifact。任何偏离使该轮实验失败。
5. **移除接收与领取队头阻塞**：QueryStore 短事务化，admission 完成队列与确定性提交分离；用
   大小混合 artifact、慢首项和 1/4/8/16 worker 压测，报告 master utilization 与 backpressure。
6. **长跑治理**：实现 result CAS pin/GC，再处理 dense bitmap native merge 和 non-cooperative
   portfolio 隔离。
7. **重做规模实验**：固定 AFL:SymCC 比例与辅助 slot，至少 5 个规模点、每点相同的独立 paired
   seeds；先做短 tuning，再冻结配置执行确认性长跑。只有 v6 `decision_eligible=true` 且 ceiling
   CI 足够窄时，才发布规模上限。

## 7. 架构优化判断

下一阶段最有价值的创新不是继续堆叠路径评分器，而是把系统从“单 coordinator 上的多线程功能
集合”升级为 **数据面与控制面分离的分层并行架构**：worker 只运行目标和产生带 provenance 的
结果；独立 admission/coverage/query shard 消费者并行处理重 I/O；master 只维护小型 fenced
状态和全局策略。所有跨层数据继续使用 digest、generation、lease token 和 WAL receipt 绑定。

该方向同时解决当前三个实测问题：SQLite 临界区过长、结果接收队头阻塞、辅助池争用 master
单核。其挑战在于不能以吞吐为由放弃确定性、失败原子性和可恢复性；因此应先完成上述 P0/P1
闭环，再通过 profile 驱动拆分，而不是一次性重写 coordinator。
