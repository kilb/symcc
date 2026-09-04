# F313：事务化 MPI 派发交接与补偿式资源回收

- 日期：2026-08-07
- 范围：MPI master 派发、exact-work/target/state lease、策略 assignment、proposal 和对象传输缓存
- 成熟度：I/T（实现、单元测试与故障注入）；尚无真实 MPI rank 失联 campaign

## 1. 审查问题

F311/F312 已经建立跨 coordinator target/work fencing 和失败原子 heartbeat，但从“选择任务”
到 `comm.send(TAG_WORK)` 返回之间仍会依次修改多个异构状态：

1. target group 与 exact-work lease；
2. self-config parameter assignment 和多阶段 SMT algorithm assignment；
3. state-shard lease 与 SimiFuzz seed-worker assignment；
4. verified proposal 的 `queued -> dispatched` 状态；
5. 本地恢复 journal；
6. worker 已缓存 content-addressed object 的集合。

旧实现没有统一提交边界。共享 exact-work claim 冲突只手工撤销其中少数状态；如果后续
`comm.send()` 抛异常，其余 assignment 会悬挂至 TTL 或永久停在 pending/active 状态。对象 ID
还会在 send 前写入 `worker_objects`，导致下一次派发省略真实内容，而 worker 实际从未收到对象。
stale fenced result 被拒绝时同样只释放 target，state、parameter、algorithm、worker pairing
和 proposal 仍可能保留虚假的在途状态。

## 2. 设计目标与不变量

F313 把一次派发建模为有三阶段的资源交接：

```text
PREPARING --send returns--> ACTIVE --fence accepted--> COMMITTING --triage succeeds--> COMMITTED
     |                         |                           |
     +-- pre-send rollback ----+-- stale-result discard --+-- shutdown compensation
```

核心不变量如下：

| 编号 | 不变量 |
| --- | --- |
| D1 | worker 同一时刻最多属于 preparing、active、committing 三张事务表之一 |
| D2 | send 返回前产生的每一项可变 reservation 都必须注册补偿操作 |
| D3 | send 失败执行 pre-send rollback；不得把未执行任务计入策略 pull/attempt |
| D4 | send 成功但结果被 fencing 拒绝时执行 post-send discard；保留真实派发次数，但不学习不可信反馈 |
| D5 | `begin_commit()` 成功后事务才从 active 移入 committing；I/O 异常期间始终有表持有事务 |
| D6 | 只有 triage、work completion、target/state release 全部走完后才丢弃补偿栈 |
| D7 | worker 对象缓存只在 send 成功后更新 |
| D8 | 消息构造任意阶段抛异常时，必须立即扫描三张事务表并补偿，不能只依赖进程退出清理 |

## 3. 补偿式事务实现

### 3.1 `_DispatchReservationTransaction`

事务保存按获取顺序登记的回调，并在回滚时逆序执行。每项资源可以同时提供：

- `before_send`：任务没有交给 executor 时的精确撤销；
- `after_send`：executor 可能已经运行、但结果不能进入学习或状态提交时的丢弃语义。

一个补偿异常只记录其资源名，不阻止后续回调执行。该故障隔离很重要：例如共享目录暂时
不可写时，仍应释放进程内 target registry、algorithm pending token 和 worker pairing。
`commit()` 清空回调；重复 rollback 是无操作，从而允许 finally 路径安全兜底。

派发调用点另设统一异常边界：即使异常并非来自 `comm.send()`，也会从 preparing、active、
committing 三张表移除该 worker 的全部事务并逐项回滚。清理器对同一事务对象去重；即使 D1
因未来代码回归而被破坏、一个 worker 同时出现在多张表中，也会保守清除全部不同事务。

### 3.2 三张事务表

master 新增：

- `preparing_dispatches[worker]`：正在构造消息和申请资源；
- `active_dispatches[worker]`：send 已返回，等待 worker result；
- `committing_dispatches[worker]`：exact-work fence 已接受，正在执行 batch triage。

结果处理不能先 `pop(active)` 再调用 `begin_commit()`。F313 先保留 active 引用，fence 成功后
才执行 active-to-committing 迁移；因此 `begin_commit()` 的共享存储异常触发 master finally
时仍能找到完整补偿事务。

### 3.3 资源语义

| 资源 | send 前失败 | send 后 stale result |
| --- | --- | --- |
| Shared exact-work | current-token `abandon` | 同一操作；若 token 已被接管则安全失败 |
| Local work journal | 追加 `reclaim` 并移除 lease | 追加 `reclaim`，不标记 done |
| Target group/local target | 释放 | 释放 |
| State task | 撤销 lease 并回退 lease 计数 | 移除 active lease并记录一次失败 |
| Parameter assignment | 删除 pending token | 删除 pending token，不观察 reward |
| Algorithm stage | 恢复多阶段游标、prior计数和 RNG | 只丢弃 pending feedback，不倒退已经执行的阶段 |
| Seed-worker assignment | 删除 active并回退assignment数 | 删除 active但保留真实assignment数 |
| Verified proposal | attempts 回退并恢复 pending | attempts 保留，转 retry/rejected，原因记为 result lost |

algorithm 的精确 rewind 只允许在尚无下一次全局 selection 的 pre-send 窗口执行。事务路径满足
这一条件；如果 API 被延迟调用，则保守地只移除 pending token，不回写可能已被后续选择依赖的
全局状态。精确回滚同时恢复 `sequence_number` 和 RNG；连续未派发选择按 LIFO 回滚后，会重放
相同 stage 和相同 token，避免未执行任务永久消耗序号或阻断更早选择的精确撤销。

### 3.4 本地 journal 顺序

`WorkLeaseJournal` 原先先修改内存 map，再追加 JSONL。追加失败会使本进程误认为 lease/done 已
持久化。F313 把顺序改为：先成功追加事件，再修改内存，再按需 compact；`abandon()` 也使用
明确的 `reclaim` 事件。compaction 因而总能看到与刚追加事件一致的内存状态。

### 3.5 对象传输缓存

CAS import 仍可在 send 前完成并把 `object_content` 放入消息，但 `worker_objects[rank]` 只在
send 返回后加入 object ID。send 抛异常时，下一次派发仍携带完整内容，不会产生虚假的缓存命中。

## 4. Send 歧义与可信边界

MPI blocking send 抛异常时，框架不能一般性证明远端绝对未看到消息。F313 选择结果安全优先：
按 pre-send 路径撤销 exact-work token；如果 worker 实际收到并稍后返回，该结果无法通过
`begin_commit()`，因此不会进入 triage。代价是可能产生一次无效计算，但不会把歧义消息当成
已提交结果。

本实现是单 coordinator 内的补偿式事务，不是分布式两阶段提交。尤其是 triage 可能包含多个
文件与覆盖状态写入，随后 shared/local complete 也可能失败；F313 没有宣称这些跨介质副作用
已经线性化。F321复核后，exact-work `committing` 已改为不可按TTL偷取的不可逆决定；这避免旧
holder恢复后与新owner并发应用triage，但崩溃恢复会保守阻塞，直到未来用持久事务日志明确记录
跨介质副作用的重放/回滚状态。现有幂等与coverage claim仍只能降低重复副作用，不能替代该日志。

## 5. 自动化证据

新增 10 项测试覆盖：

1. 本地 journal abandon 可重放，append 失败不推进内存；
2. shared work abandon 只接受 current、pre-commit token；
3. state abandon 与 stale-result discard 分别回退未派发计数和记录真实失败；
4. self-config 未派发 token 不进入观察；
5. algorithm 未派发 stage 精确重放，而已执行的 discard 不倒退阶段；
6. SimiFuzz release 回退虚假 assignment，discard 保留真实派发计数；
7. proposal 未发送时恢复 pending/attempt，结果丢失时进入 retry 并保留 attempt；
8. 事务按逆序、按阶段选择补偿，并在单项补偿异常后继续清理。
9. 两个未派发 algorithm selection 按 LIFO 回滚后恢复全局序号，并生成相同重放 token；
10. worker 违反 D1 同时出现在两张事务表时，统一异常清理仍会补偿全部不同事务并清空 registry。

定向组合为 `213 passed + 12 subtests`（5.98 秒）。完整 Python 门禁为 `574/574`
（测试计时 82.846 秒，shell 墙钟 83.376 秒），Ruff 与 `py_compile` 通过；文档校验结果见
交付索引。测试证明状态机与故障补偿，不证明真实 MPI 故障下的性能收益。

## 6. 后续实验

后续真实实验应在两个 coordinator 和多个 worker 上分别注入：

1. shared work claim 后、local journal 后、proposal mark 后的 send exception；
2. send 已送达但 master 认为失败的歧义场景；
3. worker 超过 TTL 后返回的 stale result；
4. `begin_commit`、triage、shared complete 各阶段的 EIO 与进程 kill；
5. object-content 首次传输失败后的再次派发。

指标包括 leaked lease/assignment/pending token 数、恢复延迟、stale-result drop、重复执行 CPU、
worker object miss、proposal retry 守恒和 corpus/coverage 一致性。只有这些运行完成后，F313
才具备分布式故障 E-mechanism 证据。
