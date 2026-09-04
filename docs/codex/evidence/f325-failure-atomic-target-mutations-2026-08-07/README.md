# F325 Prevalidated Target-Group Mutation Batching Evidence

本目录保存F325“target-group全组预验证与分片mutation batching”的可复核证据：

1. `fault-injection-tests.log`：13项target-table测试，包含陈旧token零变更、claim/release
   每shard同步一次、write/unlink/directory barrier失败回滚和序列化预检；
2. `benchmark_target_mutations.py`与`microbenchmark.txt`：64-target claim+release在1/8个
   shard上的逐record屏障与batched屏障交错7轮对比；
3. `verify_multiprocess_target_fencing.py`与`multiprocess-race.txt`：两个独立进程、32-target
   group、16-target overlap、8 shards的50轮共享表竞争；
4. `integration-tests.log`：MPI lifecycle与AFL profile orchestration回归；
5. `full-tests.log`：仓库`test/`范围的完整Python回归；
6. `checks.txt`：工具版本、静态门禁、结果摘要和明确的外推边界。

## 实验口径

- 微基准的reference/batch路径使用同一份当前claim/release实现、全组校验、每条JSON/file
  `fsync`/rename/unlink和锁；reference仅把每个directory barrier立即执行，batch按shard合并。
- 每个场景先预热，再交错运行reference/batch各7轮。`P95`是7个样本排序后使用脚本定义的位置值，
  不是大样本统计推断。
- 50轮多进程验证的是共享文件表的single-winner、完整publication、完整release和无hang；37/13
  winner分布不用于公平性结论。
- 测试没有运行能产生真实S2F action target-group的完整hybrid campaign，不能给出coverage、solver
  throughput、LAVA-M time-to-bug或端到端DSE提升。

## 正确性边界

证据支持以下结论：正常schema/token冲突在首个mutation前拒绝；检测到且恢复存储可用的I/O失败会
按快照补偿；成功claim/release只在全部命中shard完成目录屏障后返回。

证据不支持跨文件crash atomicity。进程在mutation中途崩溃或rollback期间持续收到EIO时，可能留下
部分target状态；要升级该保证必须引入durable group intent/WAL和重启recovery。未执行物理掉电、
NFS/Lustre、网络分区或同epoch脑裂实验。

`SHA256SUMS.txt`覆盖除其自身外的全部证据文件。
