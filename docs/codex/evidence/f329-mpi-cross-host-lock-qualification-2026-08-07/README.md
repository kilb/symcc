# F329 MPI Cross-Host Lock Qualification Evidence

本目录保存F329“MPI跨主机共享锁资格协议”的可复核证据。核心口径是：生产frontend只用真实
`MPI.Get_processor_name()`决定是否具备跨host成员；测试注入的合成processor名称只覆盖状态机，
不能作为NFS/CIFS/Lustre部署证明。

## 文件说明

- `benchmark_synthetic_protocol.py`：线程化MPI消息bus与合成processor拓扑的可复现实验；
- `synthetic-protocol-baseline.json`：固定10 ms轮询的2/3/4-master各20次基线；
- `synthetic-protocol-benchmark.json`及`.log`：0.5 ms起步、指数退避后的相同实验；
- `run_same_host_mpi.py`、`same-host-mpi.json`及`.log`：真实Open MPI 7-rank、2-master同机作业；
- `fault-injection-tests.log`：跨host状态机、排他失效、本机probe失败、deadline、记录代次与反误报；
- `integration-tests.log`：distributed state、MPI qualification/lifecycle相关回归；
- `full-tests.log`：完整warnings-as-errors Python回归；
- `checks.txt`：环境、静态检查、语法编译、diff与JSON证据断言；
- `SHA256SUMS.txt`：除自身外本目录全部文件的摘要。

## 结果摘要

- F329定向/故障：8 passed、160 deselected；相关：168 passed + 13 subtests；
- 完整回归：681 passed + 41 subtests，93.83 s；
- 真实同机Open MPI：2 master、5 worker、exit 0、5/5 exact ACK；两个master的实际processor均为
  `cuda-ke`，因此零跨host轮次且`cluster_lock_verified=false`；
- 合成2/3/4 processor状态机分别完成2/6/12次contention和2/3/4次release，20次均零失败；
- 自适应轮询中位13.205/18.661/24.267 ms，相对固定10 ms基线
  123.249/174.823/225.942 ms降低89.29%/89.33%/89.26%。

## 证明边界

真实MPI实验只有一台机器，直接证明的是同机拓扑不会误置跨机flag、资格对象可注入且作业能完整
关闭。合成实验的JSON显式包含`synthetic_topology=true`和`deployment_evidence=false`，只支持协议
流程、检查基数和轮询before/after结论。当前没有真实多client后端产生
`cluster_lock_verified=true`，也没有server failover、network partition、kernel panic、掉电、运行期
remount、cache coherence、真实target或coverage/solver实验。
