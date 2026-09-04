# F326 Crash-Released Kernel Locks and Coverage-State Integrity Evidence

本目录保存F326“崩溃自动释放的共享状态锁与coverage-owner失败关闭”的可复核证据：

1. `fault-injection-tests.log`：12项定向测试加3个损坏记录subtest，覆盖真实subprocess SIGKILL、
   异常释放、旧mtime不抢锁、deadline、unsupported flock、非法lock path、coverage损坏状态/
   跨shard索引/临时文件/时间验证和配置解析；
2. `verify_crash_reclaim_and_coverage.py`与`multiprocess-crash-and-coverage.txt`：40个持锁进程
   SIGKILL后的work/coverage回收，以及10轮8进程coverage OR-state并发提交；
3. `benchmark_lock_protocol.py`与`microbenchmark.txt`：旧`os.mkdir/os.rmdir`与稳定文件
   nonblocking flock的10000次×9轮交错无竞争短锁对比；
4. `syscall-trace.txt`：真实Linux `openat(O_NOFOLLOW|O_CLOEXEC)`、`flock(LOCK_EX|LOCK_NB)`、
   `flock(LOCK_UN)`序列，并证明预热后没有mkdir/rmdir；
5. `integration-tests.log`与`full-tests.log`：共享状态/MPI/AFL相关回归及仓库`test/`完整Python回归；
6. `checks.txt`：环境、静态门禁、结果摘要和结论边界。

## 实验口径

- crash-reclaim每轮child在内核锁已成功取得后写一个pipe marker，父进程随后发送SIGKILL并等待
  child退出，再执行真实work claim或coverage claim；它不是“先杀后猜测child是否持锁”。
- coverage stress每轮8个独立进程同时向256个相同index提交8个互不相同的bucket bit。每轮期望
  `256 × 8 = 2048`个novel bit，最终每个index必须精确为`0xff`。
- 微基准只测无竞争短锁原语。旧路径直接调用原协议的`os.mkdir/os.rmdir`，新路径调用当前
  `_bounded_advisory_lock`；数字不包含lease JSON、coverage shard、symbolic execution或solver。
- winner公平性、锁排队P99、跨主机锁管理器性能和真实DSE吞吐均未由这些数据证明。

## 语义边界

证据证明本机Linux overlay文件系统上的进程级行为：descriptor close、Python异常或SIGKILL都会
释放内核flock；不支持flock的文件系统失败关闭，不降级为无锁执行。稳定`.lock`文件必须保留，
运行中unlink会造成不同inode并破坏互斥。

证据不证明NFS/Lustre/FUSE的部署语义、网络分区、节点掉电、内核崩溃、同epoch混合版本或
拜占庭writer。旧版本崩溃遗留的`.lock`目录不会自动迁移；新实现把非普通lock path失败关闭，
管理员只有在确认所有旧writer停止后才能清理。未执行coverage或solver性能benchmark。

`SHA256SUMS.txt`覆盖除其自身以外的全部证据文件。
