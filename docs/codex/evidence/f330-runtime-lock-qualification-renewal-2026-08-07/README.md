# F330 运行期共享锁资格续期证据

本目录保存 F330“generation-fenced runtime lock qualification renewal”的可复现实验和测试证据。
F330 将 F329 的一次性启动资格扩展为由 rank 0 周期发起、全体 standalone master 参与的运行时
语义断言；一旦跨 client 排他或释放语义漂移，不继续使用陈旧快照。

## 文件说明

- `run_synthetic_runtime_mpi.py`、`runtime-mpi.json`、`runtime-mpi.log`：真实 Open MPI 7-rank、
  2-master 主循环实验；使用测试包装器注入不同 processor 名称，因此是合成拓扑；
- `benchmark_runtime_protocol.py`、`runtime-protocol-benchmark.json`及`.log`：2/3/4-master、2次
  warm-up、20次交错测量，比较直接资格与增加代次请求/校验/记账后的同一状态机；
- `directed-tests.log`：协议代次、连续续期、防重放、部分发送、超时和语义漂移定向测试；
- `integration-tests.log`：MPI、distributed state、hybrid/AFL profile相关回归；
- `full-tests.log`：完整 warnings-as-errors Python 回归；
- `checks.txt`：环境、静态检查、证据断言和结论边界；
- `SHA256SUMS.txt`：除自身外本目录全部文件的 SHA-256 摘要。

## 结果摘要

- 定向测试：13 passed；相关回归：292 passed + 21 subtests；完整回归：688 passed + 41 subtests；
- 真实 MPI/合成拓扑成功作业：2 master、5 worker、两个 master 各完成3代续期，退出码0；
- 同一真实 MPI 主循环在第2代注入排他语义漂移：两个 master 均记录`generation=2`、
  `successes=1`、`failures=1`，随后作业以退出码70失败关闭；
- 合成控制面微基准中，2/3/4-master续期中位分别为13.218、18.811和24.317 ms；相对同一
  资格状态机直接调用的中位增量分别为0.008 ms（0.062%）、0.048 ms（0.254%）和
  -0.002 ms（-0.008%，测量噪声）；全部20次零失败且证据数保持2/2/2、3/6/3、4/12/4。

## 严格证明边界

`runtime-mpi.json`显式保存：

```text
synthetic_topology=true
deployment_evidence=false
actual_mpi_transport=true
actual_multi_host=false
```

该实验真实执行 Open MPI communicator、standalone 主循环、worker 生命周期和本机 overlayfs
`flock`，但 processor 名称由测试包装器注入；它证明运行期调度、代次、防重放、续期和失败关闭接线，
不证明远端 NFS/SMB/Lustre/CephFS 锁语义。微基准使用同机 Python thread 消息 bus，也不包含真实网络、
本机九项 operation probe、续期前 work-lease heartbeat、符号执行 target、solver、coverage 或 DSE
吞吐。任何负增量都属于测量噪声，不得表述为性能提升。

本轮仍没有第二台真实 client、server/lock-manager restart、network partition、MPI rank failure、
物理断电或 LAVA-M campaign，因此不能据此声称真实多机容错、存储后端恢复能力或 fuzzing 覆盖率提升。
