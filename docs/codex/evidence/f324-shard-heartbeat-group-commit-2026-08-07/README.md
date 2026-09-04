# F324 Shard-Level Heartbeat Group Commit Evidence

本目录保存F324“分片级租约心跳group commit”的可复核证据：

1. `fault-injection-tests.log`：同步次数、stale token、later-record失败、directory barrier失败、
   跨目录误用、target-group和MPI入口的9项定向测试；
2. `benchmark_group_commit.py`与`microbenchmark.txt`：64条record在1/8个shard上的scalar/batch
   可执行对比及全部7轮原始样本；
3. `normal-multi.log`与`mpi-artifacts.txt`：TTL=1秒、4 shards的真实Open MPI双master强制续租；
4. `full-tests.log`：明确限定仓库`test/`的完整Python回归；
5. `checks.txt`：工具版本、静态门禁、实验摘要与不成立的外推边界。

## 实验口径

- 微基准的两条路径都执行64次JSON序列化、file `fsync`和rename，只改变parent directory
  `fsync`次数；数字不是DSE吞吐、coverage或solver性能。
- 真实MPI目标固定sleep 1.2秒，使1秒lease TTL的`TTL/3` heartbeat实际发生；2.6 tc/s主要由该
  人工sleep决定，不是baseline/full-system性能对比。
- MPI的13次observation是12个seed加1个内容寻址child；`generated=13`不是13个unique child。
- `pytest -q test`是本仓库Python回归范围。未构建QSYM Python扩展时，不把其上游自带测试纳入
  本证据，也不宣称该后端测试通过。

## 持久化边界

证据证明实现调用、错误传播、可见中间态和真实MPI健康路径；不是物理断电实验。结论要求部署
文件系统正确实现regular-file `fsync`、atomic same-directory rename和directory `fsync`。
不覆盖控制器虚假flush、网络分区、同epoch脑裂、跨文件原子可见或全局exactly-once execution。

`SHA256SUMS.txt`覆盖除其自身以外的全部证据文件。
