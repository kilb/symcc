# F461：并行控制面深度复审与算法边界收敛

> 日期：2026-08-28  
> 范围：并行规模模型、分布式 coverage authority、持久 frontier、Hybrid Master、QueryStore、QF_BV 持久 solver 以及原生并发运行时。  
> 证据等级：本记录主要是实现、回归和机制微基准证据；不将其外推为覆盖率或多节点线性加速结论。

## 1. 本轮结论

前一阶段已经具备较完整的并行功能，但本轮复审仍发现六类会影响科研结论可信度或长时稳定性的问题：规模模型存在伪重复和资源混杂，dense coverage delta 有高额 Python 对象放大，frontier 每次修改重写整份快照，Master 校验阻塞主循环，多 Master 会留下全局准入失败的 AFL queue 文件，QueryStore 启动重放可被大量积压或单个损坏记录阻断。此外，普通 QSYM/simple runtime 的全局状态与原生多线程语义不相容，却缺少机器可检验的失败关闭。

| 问题 | 已落地处置 | 直接结果 |
| --- | --- | --- |
| 规模结论把同种子重复当独立样本 | 完整资源分配分组、配对 seed-block bootstrap、等暴露和混杂因子门禁 | 不合格 campaign 只输出诊断，不再输出“上限”决策 |
| dense delta 内存放大 | 流式严格校验、bitset 去重、`array("Q")` 分片打包、全批总预算 | 256 KiB 输入峰值由约 24.21 MiB 降至 2.08 MiB |
| frontier 修改是 O(状态总数) | 小型代次 transition log，周期快照压缩，进程内完整性 cache | 25k 状态 hot claim 约快 4.0×，hot snapshot 约快 956× |
| frontier 字节上限可在压缩时延迟失败 | 按变化量精确核算下一快照的 canonical envelope 尺寸 | 超限在写 transition 前拒绝，调用失败不再隐式提交 |
| Master 等待结果校验 | 有界异步、有序提交管线 | 校验未完成时主循环仍可派发、心跳和收尾 |
| 全局 coverage 竞争留下冗余 queue entry | 全局事务后删除 loser，winner 收紧为无空洞 ID 前缀 | 正常路径不再持续积累跨 Master 重复样本 |
| QueryStore 启动重放无界且可被坏记录阻断 | 条数/时间双预算、单记录隔离、可观测进度 | 默认启动最多处理 64 条或 2 s，坏记录不阻塞后续发布 |
| incremental QF_BV 超时淘汰与 pipe 关闭边界不清 | 淘汰时检查子进程与三条 pipe，存活才再关闭 | 冷恢复前不留存活子进程或未关文件，且避免重复关闭 |
| 普通 runtime 误入原生多线程 | 初始化线程绑定，第二线程进入基本块立即终止 | 不再静默产生竞态污染的路径结果 |

## 2. 架构与执行次序

```text
worker RESULT
   |
   v
MPI generation/active-dispatch gate
   |
   v   capacity-bounded receive
ordered asynchronous admission ----------> master loop continues dispatch/heartbeat
   |
   v   only completed ordered prefix
lease + target + scheduler state commit
   |
   v
candidate prepublication -> global sharded coverage claim
   |                          |
   | loser                    | winner
   v                          v
unlink provisional file      compact to gap-free AFL queue ID
   \__________________________/
               |
               v
      authoritative feedback / next scheduling round
```

关键次序不能交换：

1. MPI 结果先经过 dispatch generation 和 active-worker 检查，过期结果不能影响当前状态。
2. 主循环只在 admission 存在容量时接收；校验在有界队列中异步执行，但副作用仍按接收顺序提交。
3. candidate 必须先持久预发布并同步目录，才能申请全局 coverage；否则会出现“已占用覆盖但无可恢复样本”。
4. 全局 coverage authority 一次裁决整批，再根据每个 candidate 的真实新增量删除 loser 并收紧 winner ID。
5. 只有全局 winner 进入 AFL queue 和正向调度反馈；loser 的 digest 仍记入已分析集，防止同步路径反复发现它。

## 3. 算法正确性修复

### 3.1 配对的规模证据

原分析将每个并行度的观测独立 bootstrap，会破坏“同一随机种子跨规模观测”的配对结构，也可能把同 seed 重复运行当成独立样本。现在以 `(np, workers, masters, afl_instances, modeled_parallelism)` 作为完整分配身份，仅在所有分配都存在的 random-seed block 上成簇重采样。

正式决策必须同时通过：每分配至少 3 个独立 seed；seed block 跨规模完整；有稳定 run ID；暴露时间完整且默认相对差不超过 1%；至少 4 个并行度；每个模型 x 值只对应一种资源分配；Hybrid 的 AFL:SymCC 比例和 coordinator 数固定；USL 与 coverage saturation 均有效且 `R² >= 0.50`。任一条失败都会保留诊断原因，但不允许程序给出可执行的规模上限建议。

### 3.2 覆盖事务的有界内存

delta 现在边迭代边验证，不再构造 `(index,bits)` Python tuple 列表。小批用 `set` 检查重复 index，大批改用 1 MiB bitset；每个特征编码为一个 64-bit word 写入分片 `array("Q")`。全批超过 `2^18` 个特征时，在获取任何分片锁或发布部分事务前整批拒绝。这保留了原子性，同时将 dense 输入的内存放大从约 91--92倍降至 8.2--8.8倍。

### 3.3 持久 frontier 的日志与压缩

每次 claim/complete/abandon/recover 只持久一条包含 before/after generation、operation、canonical payload 和 SHA-256 的 transition。启动或冷读时从 base snapshot 按代次重放；长运行进程用 base 和全部 journal identity 验证 cache，任何记录消失、篡改、断代或非规范编码都失败关闭。默认累积 64 条后先发布新 base，再删除旧 transition；因此压缩中途崩溃时，最多留下可重放的冗余日志，不会丢状态。

`max_bytes` 的判定也在 transition 发布前完成。为避免每次重新编码全部状态，实现以上一代精确尺寸为基准，只计算 generation 数字长度、ready/done 列表项与分隔符、新增/删除 lease 的 canonical JSON 以及 search 变化。测试将该 O(变化量) 结果与完整 envelope 编码逐字节比较；超限 completion 后 generation、journal 数和重启快照均保持不变。

`recover_expired()` 先无锁读 heartbeat，只对最早且看似过期的有界批次加锁，然后在锁内重读并裁决。这不改变心跳与恢复的线性化关系，但避免单次恢复扫描锁住全部 lease。

### 3.4 QueryStore 的运行时恢复边界

启动对账不再等价于“清空全部 outbox”。构造器默认在 64 条和 2 s 两个预算中先触发者为准，记录 attempted/published/failed/elapsed 以供健康检查。单条损坏记录会累加 attempt 并保留 error，然后继续后续记录。

> **F462 修订（2026-08-30）：** 后续故障注入确认，SQLite 中 `done + result +
> publication` 的同事务提交才是逻辑完成点；文件发布属于可重做的 outbox 副作用。
> 因而 `complete()` 已改为最多 64 条、100 ms 的尽力即时发布，失败进入退避重试，
> 不再向 solver 调用者上抛并把已完成查询误记为求解失败。持续 janitor、dead letter
> 和 `--once` drain 的最终协议见
> [`F462`](Second_Deep_Review_and_Recovery_Closure_F462_2026-08-30.md)。

### 3.5 QF_BV 超时 context 的完整生命周期

在双工具链验证中，一次过度并发的 lit 运行暴露了 incremental QF_BV cancellation/timeout 边界：旧代码的超时 catch 只从 LRU 删除 context，对“`_request()` 已关闭”和“其他阶段抛出 TimeoutError”没有统一的生命周期判断。现在 `_drop_context()` 在移除 LRU 身份后，检查子进程和 stdin/stdout/stderr：只要任一资源仍存活就执行完整 terminate/kill/wait/close；若 request 已完成关闭则不重复操作。原超时-冷恢复测试增加了子进程退出以及三条 pipe 已关闭的显式断言。

## 4. 实验与回归结果

### 4.1 定量微基准

| 项目 | 修复前 | 修复后 | 结论边界 |
| --- | ---: | ---: | --- |
| dense coverage 分组，16 KiB | 约 91--92×输入放大 | 143,660 B，8.77× | Python 内存机制微基准 |
| dense coverage 分组，64 KiB | 约 91--92× | 535,340 B，8.17× | 同上 |
| dense coverage 分组，256 KiB | 24.21 MiB | 2,182,444 B，8.33× | 同上 |
| frontier hot claim，25k states | 81.07 ms | 20.264 ms | 约 4.0×，最终 v2 基准、31 个 snapshot samples |
| frontier hot snapshot，25k states | 30.60 ms | 0.032 ms | 约 956×，cache 命中路径 |
| frontier cold snapshot，25k states | 未作同版本对照 | 58.85 ms | 冷启动仍需完整重放，不应宣称同等加速 |

frontier v2 基准修正了旧脚本中逻辑时钟与墙钟混用造成的 stale completion/abandon，并将持久字节改为 base+journal。最终 hot claim 在 1k/5k/10k/25k states 分别是 16.334/17.145/17.362/20.264 ms，呈现近似稳定的追加开销。25k 时 base/journal 为 1,198/1,680,166 B。该数据不包含 NFS/Lustre、多节点锁竞争、真实 solver 或 AFL 吞吐，因此不能换算为端到端 coverage 收益。

### 4.2 自动化验证

| 门禁 | 结果 | 覆盖边界 |
| --- | --- | --- |
| 五模块耦合 Python 回归 | 328 passed + 116 subtests | scale/frontier/coverage/master/QueryStore |
| 上述五模块 + QF_BV backend | 342 passed + 116 subtests | 增加 timeout/cancel/cold recovery 生命周期 |
| QueryStore 完整专项 | 33 passed + 25 subtests | outbox、预算、poison record、幂等发布 |
| 全仓 Python 能力门禁 | 1611 passed + 607 subtests | 最终源码复验耗时 5 分 23 秒；4 条既有 multiprocessing/fork 弃用警告，无失败 |
| QF_BV backend 专项 | 14 passed | timeout/cancel/cold recovery 与 pipe 关闭 |
| 原生多线程边界 + schedule-only | 2 lit tests passed | 普通 runtime 失败关闭与独立 replay artifact |
| LLVM 18 全量 lit，`-j 16` | 346 passed + 1 unsupported | 347 discovered，无失败 |
| LLVM 17 全量 lit，`-j 16` | 345 passed + 2 unsupported | 347 discovered，无失败 |
| Python 风格/静态检查 | ruff passed | 本轮修改文件 |

可机器读取的本轮微基准和验证摘要位于 [`../evidence/f461-control-plane-remediation-2026-08-28/`](../evidence/f461-control-plane-remediation-2026-08-28/README.md)。

## 5. 仍存在的局限和下一步

1. **需重跑正式规模 campaign。** 新 v3 分析器会拒绝缺 seed、暴露不等或资源配比变化的历史数据。这是修正结论边界，不是新的加速实验；现有“上限”必须重新审核。
2. **frontier 冷启动仍是 O(日志+状态)。** 默认 64 条压缩已限制日志，但从不可信持久状态恢复时必须做全量完整性验证，不能为速度绕过。
3. **Master admission 不等于 Python 校验多核加速。** 默认单线程是因为校验多为 GIL 路径；本次解决的是主循环可进展性和内存背压，不是宣称校验吞吐线性扩展。
4. **多 Master queue 收紧是正常路径收敛。** 在 coverage 事务已提交而收紧前崩溃，可能保留可恢复但冗余的预发布文件；它不会丢失 coverage winner，后续可增加独立的孤儿对账。
5. **原生多线程输入/路径求解尚未实现。** 当前正确语义是“普通 runtime 明确拒绝，schedule-only artifact 专用于调度探针”。若要支持真正多线程 DSE，需要 per-thread shadow stack/memory、跨线程内存模型、solver context 合并和确定性 replay，属于独立研究课题。
6. **源码交付门禁仍需仓库维护者完成正式提交。** 当前工作树含大量历史未跟踪文件和 runtime submodule 修改。本轮不会擅自清理或覆盖这些用户内容；干净 checkout 可重现性仍依赖审核后纳入 Git。

最终 `verify_delivery.py` 确认本地 Markdown/入口链接 0 缺失，F461 内层与顶层哈希均通过，但总门禁仍报 13 项：F360/F361 两个历史 CI/测试清单精确快照，F400/F403/F424/F425/F426/F427/F436/F437/F438/F456 十个冻结 source manifest 与后续演进源码不同，以及顶层清单中 F442/F458/总技术档案三个本轮未修改文档的历史哈希漂移。重写冻结证据会破坏原阶段可复验性，因此本轮只封存 F461 新证据，不伪造旧 manifest 为“当前源码”。

## 6. 审查结论

本轮修复了会直接导致假规模结论、内存放大、主循环停滞、重复 corpus 累积、启动不可用和并发状态静默污染的问题。修复后的设计边界是：持久事务优先正确与可恢复，在此基础上再用有界批处理、追加日志和异步管线减少串行开销；科研结论必须由独立、配对、等暴露、无混杂的实验支持。
