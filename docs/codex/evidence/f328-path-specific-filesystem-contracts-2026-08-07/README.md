# F328 Path-Specific Filesystem Contract Evidence

本目录保存 F328“按路径区分的共享文件系统能力契约”的可复核证据。核心口径是：
standalone publication、multi-master lease table 与 coverage-owner gossip 使用不同的存储操作集合；
未执行的能力在 v2 快照中记录为 JSON `null`，不等同于“不支持”。

- `benchmark_requirement_profiles.py` 与 `profile-benchmark.json`：真实执行 full、lease、coverage
  三种 profile，核对三态快照、逐操作调用计数、交错重复启动延迟与残留；
- `fault-injection-tests.log`：required operation 失败关闭、skipped operation 不被调用、非法与合并
  profile、constructor 默认 profile 以及 hybrid 前置 gate；
- `integration-tests.log` 与 `full-tests.log`：分布式状态、MPI/AFL 编排和完整 warnings-as-errors 回归；
- `checks.txt`：静态检查、语法编译、文档/图表结构及证明边界。

所有延迟均为本机一次性启动探针成本。操作数量减少是确定性的实现事实；本机延迟差异可能受两个
隔离锁 child 的固定成本和调度噪声支配，不能解释为 coverage、solver 或 DSE throughput 提升。
本证据也未验证远端 NFS/CIFS/Lustre client、掉电恢复、server failover 或 network partition。

## 结果摘要

- profile/故障定向：17 passed，143 deselected；相关集成：216 passed + 21 subtests；
- 完整warnings-as-errors回归：673 passed + 41 subtests；
- full/lease/coverage各40次平衡交错，启动成本中位为51.763/42.038/36.918 ms；
- replace调用为3/1/1，hard link为1/0/0，durable unlink为1/1/0；
- 0 invalid snapshot、0 operation-count mismatch、0 probe residue；
- `probe_scope=same-host-subprocess-v1`，`cluster_lock_verified=false`。

`SHA256SUMS.txt` 覆盖本目录内除其自身以外的全部证据文件。
