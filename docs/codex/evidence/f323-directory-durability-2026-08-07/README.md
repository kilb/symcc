# F323 Directory Durability Evidence

本目录保存 F323“目录项持久化屏障”的可复核证据。证据回答四个不同问题，不能相互替代：

1. `fault-injection-tests.log`：在目录 `fsync` 报错时，调用方是否失败关闭，已经发生的
   `rename/link/unlink` 中间态是否仍可由重启逻辑识别；
2. `syscall-trace.log`：Linux 实际执行是否为 file `fsync`、跨目录 `rename`、目标目录
   `fsync`、源目录 `fsync` 的顺序；
3. `normal-single.log` 与 `normal-multi.log`：严格屏障接入后，真实 Open MPI 单/双 master
   是否仍完整提交 corpus、收齐 worker ACK 并清理 epoch 状态；
4. `microbenchmark.txt`：本机 overlayfs 上每次同目录耐久 replace 的同步成本量级。

## 口径

- 单 master corpus 的 3 个文件是 2 个初始输入和 1 个按摘要去重的 child；`generated=3`
  是 3 次目标执行各报告 1 个输出，不能解释为生成了 3 个不同 child。
- 双 master corpus 的 13 个文件是 12 个初始输入和 1 个按摘要去重的 child；master
  分配 `0=5, 1=8` 只证明本次运行无漏任务，不是稳定负载比例结论。
- 微基准从文件内容已经 `fsync` 完成后开始计时，只比较 `rename` 与
  `rename + fsync(parent)`；500 个样本的结果不是 DSE 吞吐、覆盖率或求解性能结果。
- 故障注入模拟系统调用返回错误，`strace` 证明调用顺序；二者都不是物理断电实验。
  当前证据因此为 E-mechanism，不升级为真实 power-cut benchmark。

## 存储假设

结论要求 Linux/共享文件系统正确实现 atomic rename、hard link、regular-file `fsync` 和
directory `fsync`。目录同步不受支持或返回 I/O 错误时实现会显式失败，不会静默降级为仅进程
崩溃级保证。网络分区、存储控制器谎报 flush 完成、同 epoch 脑裂、原子多文件可见性和
exactly-once execution 均不在本证据范围内。

`SHA256SUMS.txt` 覆盖除其自身以外的全部证据文件。
