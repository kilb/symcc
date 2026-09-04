# F327 Shared-Filesystem Capability Contract Evidence

本目录保存 F327“共享文件系统能力契约与启动前失败关闭”的可复核证据：

1. `verify_filesystem_capabilities.py` 与 `concurrent-probe-and-cost.json`：20 次串行探针和
   5 轮×8 进程并发探针，共 60 次真实执行；保存完整能力快照、逐次延迟、child exit code、
   失败与残留列表；
2. `verify_mpi_preflight.py`、`mpi-smoke-summary.json`、`single-master.log` 和
   `two-master.log`：Open MPI 4.1.6 下 3-rank 单 master 与 8-rank 双 master 健康启动、
   capability snapshot、精确 worker ACK、双 master 全局静止以及 finalize 后清理；
3. `fault-injection-tests.log`：13 项定向测试，覆盖真实 host subprocess、真实 publication
   root、mountinfo 解析、flock 不互斥/不释放、child timeout、replace/EXDEV 故障、非有限和
   超大 timeout、constructor gate、standalone epoch 前置及 hybrid service 前置；
4. `integration-tests.log` 与 `full-tests.log`：共享状态/MPI/AFL 相关回归和完整 Python
   warnings-as-errors 回归；
5. `checks.txt`：环境、配置计数、Ruff、`py_compile`、`git diff --check`、SVG 结构和结论边界。

## 实验结果

- 60/60 capability probe 成功，0 failed result，0 nonzero child exit，0 invalid snapshot，
  0 probe residue；
- 本机 overlayfs 串行一次性启动成本中位数 54.315 ms、经验 P95 57.678 ms、最大
  80.647 ms；8 个探针进程并发时单进程延迟中位数 97.736 ms、经验 P95 112.716 ms、
  最大 115.419 ms；
- 单 master 与双 master MPI case 均 exit 0，分别 `acked=2/2` 和每组 `acked=3/3`，
  双 master 完成 global quiescence，finalize 后 0 hidden epoch state、0 probe residue；
- 定向测试 13 passed，相关回归 211 passed + 21 subtests，完整回归 668 passed +
  41 subtests。

## 证据口径

探针使用当前主机上的全新隔离 Python subprocess 验证 advisory-lock 排斥与 descriptor-close
释放。它没有在第二台物理/虚拟主机运行，因此 `probe_scope=same-host-subprocess-v1` 且
`cluster_lock_verified=false`。mount type/source 与 distributed-filesystem 分类只是诊断信息，
不能把本机测试升级为跨 client 证明。

MPI smoke 的目标是 `/bin/true`，只证明启动门禁和生命周期集成，不包含真实符号执行、solver、
coverage campaign 或 DSE 吞吐。54.315 ms 是一次性检查成本，不是性能提升。实验没有模拟掉电、
kernel crash、server failover 或 network partition，也不证明 NFS/CIFS/Lustre 的远端锁域。

`SHA256SUMS.txt` 覆盖除其自身外的全部证据文件。
