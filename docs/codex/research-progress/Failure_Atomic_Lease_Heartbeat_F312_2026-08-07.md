# F312：失败原子的共享租约续期与故障隔离

- 日期：2026-08-07
- 范围：`FencedWorkLeaseTable`、`FencedTargetLeaseTable`、MPI master 周期 heartbeat
- 成熟度：I/T（实现与故障注入测试）；尚无真实共享 NFS 故障 campaign

## 1. 问题来源

F311 已经用 target-only group lease 抑制不同 coordinator 对重叠目标的并发派发，但深度审查
发现续期路径仍有三个相互关联的故障窗口：

1. 一个 target group 由多份独立 JSON record 表示。旧实现逐成员更新 `updated`，第二个或更后
   成员写失败时，前面成员已经获得新到期时间，group 因而出现分裂的 expiry point；
2. MPI master 直接调用 work/target heartbeat。共享存储的任意一次 `OSError` 会穿透主循环，
   使本来只是控制面短暂不可用的问题升级为整个 coordinator 退出；
3. 原子 `os.replace()` 失败时，随机名 `.tmp` 文件没有清理，反复故障会在共享目录积累垃圾。

这些问题通常不破坏 exact-work `begin_commit()` 的最终结果 fencing，却会降低调度可用性、
扩大重复求解窗口，并使磁盘状态不再容易审计。因此 F312 的目标不是增加新的调度启发式，而是
把 F311 的 lease 协议补成可明确验证的失败原子续期边界。

## 2. 正确性契约

F312 使用以下四条不变量：

| 编号 | 不变量 | 失败时行为 |
| --- | --- | --- |
| H1 | group heartbeat 成功时，全部成员具有相同的新 `updated` | 任一成员失败则本次整体返回失败 |
| H2 | 已写成员必须尽力恢复到续期前 record | 回滚再次失败时保留 current-token 的保守租约，由 TTL 回收 |
| H3 | 一条租约的 I/O 异常不得阻止其他独立租约续期 | 按租约捕获 `OSError`，继续处理后续项 |
| H4 | 未发布的临时 record 不应留在 shard 目录 | `finally` 删除未完成 replace 的临时文件 |

这里的“失败原子”是合作式、尽力回滚语义，不是跨文件系统事务：进程在任意指令处被强制终止，
或回滚期间共享存储持续不可用，仍可能留下部分 record。残留 record 继续携带原 current-token，
只会保守地推迟重新派发，并最终由 TTL 恢复可用性。

## 3. 实现

### 3.1 Group heartbeat 两阶段更新

`FencedTargetLeaseTable.heartbeat_group()` 在持有全部、按 digest 排序的 target lock 后执行：

1. 一次性读取全组 record，并验证每个成员均为 `leased` 且 token 完全相同；
2. 保存每个 record 的独立快照；
3. 对副本写入统一的 `now`，逐个用同目录原子 replace 发布；
4. 若任一写入抛出 `OSError`，仍在持有全组锁时恢复此前已经发布的成员；
5. 只有全部成员写入成功才返回 `True`。

回滚写入旧 record 而不是简单删除，因为续期前的 lease 仍可能合法；删除会过早释放正在运行的
目标任务。成员 record 也不会原地修改，避免“旧值快照”和“新值对象”共享同一个字典。

### 3.2 Master 级逐租约故障隔离

新增 `_heartbeat_fenced_leases()`，统一处理：

- active exact-work lease；
- active target group lease；
- 尚未派发、跨轮携带的 pending target group lease。

函数对每条租约独立调用 heartbeat，并分别统计 `work_ok`、`work_failed`、`target_ok` 和
`target_failed`。一次 `OSError` 或 token 已失效只增加对应失败计数，不中断后续续期，也不直接
伪造成功。主循环按既有最短 TTL 三分之一周期调用，并在存在失败时输出聚合告警。

失败 token 不立即从本地表删除：active worker 返回后仍须经过 exact-work `begin_commit()`；
pending token 在真正派发前会再次验证并尝试重新 claim。这保留了 F311 的晚绑定和 stale-result
拒绝语义。

### 3.3 临时文件生命周期

`FencedWorkLeaseTable._write_record()` 继续采用“写临时文件、flush、fsync、同目录 replace”的
持久化顺序，但增加 `published` 状态和 `finally` 清理。只有 replace 完成后才认为发布成功；
任何写入、同步或替换异常都会删除未发布临时文件并把原异常继续交给上层协议处理。

## 4. 与研究工作的关系

F312 延续 Gray 与 Cheriton 的有限期 lease 恢复原则，以及 Chubby 将低容量协调状态与实际
工作负载分离的系统思想。这里没有复现 Chubby 的共识复制、session 协议或全序 fencing
number；共享文件表仍依赖合作 coordinator、共享时钟误差界限及文件系统对 `mkdir` 和同目录
`replace` 的预期语义。

- Gray and Cheriton, *Leases: An Efficient Fault-Tolerant Mechanism for Distributed File
  Cache Consistency*: <https://web.stanford.edu/class/cs240/readings/leases.pdf>
- Burrows, *The Chubby Lock Service for Loosely-Coupled Distributed Systems*:
  <https://research.google/pubs/the-chubby-lock-service-for-loosely-coupled-distributed-systems/>

本项目的创新点在于把该原则落实到并行混合符号执行的两级身份：exact work 保护结果提交，
target group 抑制不同 seed 上的重叠求解；F312 又使 group 续期和 master 生存期不再由单个
成员 record 的 I/O 成败强耦合。

## 5. 自动化证据

新增 3 项故障导向测试：

1. 第二个 target member 续期写失败后，第一个成员恢复旧时间；TTL 边界到达时整个 group 可
   被另一 owner 一次性重新 claim；
2. work heartbeat 抛出 `OSError`、target heartbeat 同时出现异常和 stale token 时，其他
   独立 lease 仍继续续期，四类计数精确守恒；
3. `os.replace()` 注入失败后，目标 record 不存在且 shard 树中没有 `.tmp` 残留。

本轮定向组合为 `170 passed + 12 subtests`（5.81 秒）；完整 Python unittest 为
`564/564`（测试计时 83.089 秒，shell 墙钟 83.632 秒）；Ruff 与 `py_compile` 通过。
这些测试证明故障状态机和资源清理，不证明共享 NFS 上的延迟、吞吐或覆盖率收益。F312
没有修改 LLVM pass，因此没有把 F307 的既有 LLVM lit 结果冒充为本轮新执行。

## 6. 后续实验

真实部署实验应至少包含：

1. 两个 MPI master、独立 worker 集合和同一个 lease 目录；
2. 在 claim、heartbeat、rollback 和 replace 四个阶段注入延迟、`EIO` 与 coordinator kill；
3. 统计 group expiry skew、heartbeat failure burst、stale-result drop、重复 target solve、
   临时文件数量和恢复时间；
4. 与 F311 基线做固定 CPU、固定随机种子、多轮配对比较；
5. 单独报告正确性/可用性指标，不把故障注入结果外推成常规 campaign 的覆盖率提升。

只有完成上述 campaign 后，F312 才能从 I/T 提升为带真实分布式故障证据的 E-mechanism。
