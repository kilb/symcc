# F332 运行期资格配置共识证据

本目录保存 F332“epoch-bound renewal configuration consensus”的可复现测试、真实 MPI 接线实验与
机制成本样本。F332 不再假定不同 MPI rank 继承了完全相同的环境变量：每个 master 对有效
`interval/timeout/jitter`形成规范 binary64 指纹，在任何运行期资格代次之前通过有界 point-to-point
exchange 达成精确一致；请求 schema v2 在每代再次绑定该指纹。

## 文件说明

- `run_configuration_consensus_mpi.py`、`configuration-consensus-mpi.json`、
  `configuration-consensus-repeat.json`及对应`.log`：固定epoch的真实 Open MPI 7-rank健康作业与
  逐rank配置漂移故障注入，两次独立复跑验证确定性指纹；
- `benchmark_configuration_consensus.py`、`configuration-consensus-cost.json`及`.log`：2/4/8
  master 合成控制面上，裸有界 exchange 与完整记录验证的20次交错机制成本；
- `directed-tests.log`：规范化、精确共识、三参数漂移、请求绑定、发送前/提交前运行中突变、畸形记录与缺失 peer；
- `integration-tests.log`：MPI、distributed state、hybrid/AFL profile相关回归；
- `full-tests.log`：完整 warnings-as-errors Python回归；
- `checks.txt`：环境、静态检查、结果与证明边界；
- `SHA256SUMS.txt`：除自身外全部证据文件的SHA-256摘要。

## 真实 MPI 结果

健康作业使用 Open MPI 4.1.6、7 ranks、2 master + 5 worker、合成processor拓扑，并把work epoch
封印为`f332`重复16次。两次独立复跑都形成同一个64字符（256位）SHA-256配置指纹，随后双方各完成
3代资格续期，失败数为0，worker按`3/3`与`2/2`完成ACK，两个作业均退出0。控制器默认未封印时
`due()`、root begin、peer accept和complete都拒绝；只有有界共识成功才记录精确本地指纹。

漂移作业只在测试包装器中把world rank 1的jitter从`0.1`改为`0.25`。双方观察到相同的
`rank:fingerprint-prefix`差异摘要，均保持`generation=0, attempts=0`，没有进入任何运行期资格轮次。
两个master分别完成`3/3`与`2/2`本地worker ACK，再经过有界master-only rendezvous；两次作业分别在
0.912403和0.875331秒内以70失败关闭。这个实验同时发现并修复了root可能在peer完成本地ACK前调用
`MPI_Abort`的关闭次序缺陷。

## 机制成本

同机Python线程和fake MPI bus在2次warm-up后执行20次交错样本。裸有界exchange与完整F332共识的
中位时间分别为：

| masters | exchange | F332 consensus | 增量 | 比率 |
|---:|---:|---:|---:|---:|
| 2 | 1.211421 ms | 1.261927 ms | 0.050506 ms | 1.041691x |
| 4 | 1.349709 ms | 1.454180 ms | 0.104471 ms | 1.077403x |
| 8 | 1.535734 ms | 1.874998 ms | 0.339265 ms | 1.220914x |

该测量包含Python线程创建与调度，只量化“相同exchange之上的规范记录验证成本”，不代表真实网络、
共享存储、符号执行或fuzzing吞吐。

## 严格证明边界

```text
actual_mpi_transport=true
synthetic_topology=true
actual_multi_host=false
deployment_evidence=false
synthetic_control_plane=true
dse_throughput_evidence=false
```

当前证据证明生产MPI接线、健康共识、逐rank漂移检测、generation-0失败关闭、无续期尝试、两组worker
ACK与有界master rendezvous。processor名称和漂移均由测试包装器注入；没有第二台真实client，没有
真实NFS/Lustre/CephFS，没有MPI rank crash、network partition、ULFM communicator repair、DSE、
coverage或LAVA-M实验。F332检测配置不一致，但不在rank失效后重建communicator。
