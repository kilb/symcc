# F331 确定性有界续期抖动证据

本目录保存 F331“deterministic bounded renewal jitter”的可复现测试与实验。F331 在 F330
运行期共享锁资格续期之上，按工作 epoch 和 renewal generation 生成仅延后的确定性抖动，分散同时
启动的独立 MPI 作业对共享存储与锁管理器的周期请求，同时保留明确的最小间隔与最坏检测窗口。

## 文件说明

- `benchmark_cohort_schedule.py`、`cohort-schedule.json`及`.log`：1024 个独立 epoch、12 代、
  100 ms 时间桶的固定节拍/抖动节拍反事实，以及 2 次 warm-up、20 次交错调度成本测量；
- `run_synthetic_jitter_mpi.py`、`synthetic-jitter-mpi.json`及`.log`：真实 Open MPI 7-rank、
  2-master/5-worker 主循环，测试包装器注入两个 processor 名称；
- `run_same_host_no_activation.py`、`same-host-no-activation.json`及`.log`：真实同机 Open MPI
  反误报，显式配置高频率和 50% 抖动仍不得启用运行期资格续期；
- `directed-tests.log`：代次、防重放、边界、缓存和 1024-epoch 分散测试；
- `integration-tests.log`：MPI、distributed state、hybrid/AFL profile 相关回归；
- `full-tests.log`：完整 warnings-as-errors Python 回归；
- `checks.txt`：环境、静态检查、实验断言和证明边界；
- `SHA256SUMS.txt`：除自身外本目录全部文件的 SHA-256 摘要。

## 结果摘要

在 1024 个作业同时从 `t=0` 开始、每个作业 12 次续期的合成计划实验中，两种策略都严格保存
12288 个事件。固定 60 秒节拍在每代形成 1024 请求/100 ms 的峰值，只占 12 个时间桶；10% 仅延后
抖动将全局峰值降到 26 请求/100 ms，非空桶增至 2232 个，计划峰值下降 97.4609375%。抖动的单次
间隔观测为 60.000908 至 65.999095 秒，严格落在 `[60, 66)` 内；12 代逐代峰值为
`26/26/20/22/18/18/15/13/13/14/14/15`。

调度函数对 12288 个决策执行 2 次 warm-up 和 20 次交错测量：固定节拍中位为
1.102851 us/decision，SHA-256 抖动为 1.766609 us/decision。生产控制器在初始化和每代完成后各计算
一次并缓存结果；完成时把实际使用的`next`直接提升为`last`，再计算新的`next`。等待期间的 `due()`
轮询和 metrics `snapshot()`均不重复哈希；因此该实验是每代计划计算成本，不是事件循环每轮成本，也
不是 DSE 吞吐。

真实 MPI/合成拓扑作业以 `interval=0.12`、`jitter=0.5` 完成 5 代；两个 master 都输出
`symcc-cluster-lock-renewal-metrics-v2`，本次随机 epoch 下上一代计划值均为 0.145894947 秒，下一代均为
0.126152756 秒，且都严格位于 `[0.12, 0.18)`；5 次尝试全部成功，5/5 worker 完成 ACK，进程退出码为
0。具体浮点值随每次作业的新 epoch 改变，跨 master 一致性与有界性才是协议不变量。真实同机作业配置
`interval=0.05`和`jitter=0.5`后仍保持`cluster_lock_verified=false`、0 个跨主机轮次且完全没有
runtime-renewal指标，证明抖动不会绕过 F329/F330 的启动资格门。

## 严格证明边界

群体实验只计算计划时间：

```text
synthetic_schedule=true
deployment_evidence=false
actual_mpi_transport=false
filesystem_io_measured=false
dse_throughput_evidence=false
```

97.46% 是 100 ms 合成时间桶内的计划到达峰值下降，不是 IOPS、锁吞吐、MPI 吞吐、求解速度、
coverage 或 LAVA-M 漏洞发现提升。真实 MPI 合成拓扑确实经过生产配置、master 主循环、资格状态机和
worker 生命周期，但两个 processor 名称来自测试包装器，仍不证明 NFS/SMB/Lustre/CephFS 的真实
远端锁语义。真实同机运行只证明不会误启用。

本轮没有第二台真实 client、共享存储 server/lock-manager restart、network partition、MPI rank
failure、物理断电、真实符号执行 target 或 fuzzing campaign。抖动只分散正常续期，不重试失败探针，
也不改变 F330 的失败关闭规则。最大陈旧证据窗口相应变为
`(1 + jitter) * interval + effective_timeout + scheduling_delay`。
