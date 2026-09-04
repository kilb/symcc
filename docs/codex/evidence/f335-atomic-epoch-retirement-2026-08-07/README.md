# F335 Atomic Completed-Epoch Retirement Evidence

本目录保存 F335“完成 epoch 原子退役与延迟回收”的自动化测试、真实 Open MPI 故障恢复和本机
分层机制成本。F335 保留 F334 的 pre/post-cleanup MPI 门控，但把显式输出目录中的完成动作从递归
删除改为同目录 `rename + parent fsync`；递归空间回收移到后续启动，并由稳定的 no-follow advisory
lock 串行化。

## 文件

| 文件 | 内容 |
| --- | --- |
| `run_atomic_epoch_retirement_mpi.py` | 7-rank/2-master 真实 MPI 后改名故障与同 epoch 收敛驱动 |
| `atomic-retirement-mpi.json` | 机器可读拓扑、退出码、命名空间和 corpus 完整性观察 |
| `atomic-retirement-mpi.log` | 两次真实 MPI 原始 stdout/stderr |
| `benchmark_atomic_epoch_retirement.py` | 3/129/1025 文件分层交错机制基准 |
| `atomic-retirement-cost.json` | 每层 30+30 个原始样本及重算摘要 |
| `atomic-retirement-cost.log` | 基准原始输出 |
| `directed-tests.log` | distributed/lifecycle/filesystem 定向回归 |
| `integration-tests.log` | 六模块相关回归 |
| `full-tests.log` | 完整 warnings-as-errors Python 回归 |
| `checks.txt` | 环境、结果和严格证明边界 |
| `SHA256SUMS.txt` | 本目录完整性清单 |

## 复现

```bash
pytest -q \
  test/test_distributed_state.py \
  test/test_mpi_lifecycle.py \
  test/test_mpi_filesystem_qualification.py

pytest -q \
  test/test_mpi_filesystem_qualification.py \
  test/test_mpi_lifecycle.py \
  test/test_distributed_state.py \
  test/test_afl_profile_orchestration.py \
  test/test_hybrid_feedback.py \
  test/test_tace_profile.py

pytest -q -W error test/test_*.py

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f335-atomic-epoch-retirement-2026-08-07/\
run_atomic_epoch_retirement_mpi.py \
  --output /tmp/f335-mpi.json

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f335-atomic-epoch-retirement-2026-08-07/\
benchmark_atomic_epoch_retirement.py \
  --output /tmp/f335-cost.json
```

## 关键结果

- 定向测试：195 passed + 30 subtests；关联测试：312 passed + 38 subtests；完整回归：
  708 passed + 58 subtests；
- 真实 MPI：`actual_mpi_transport=true`、7 ranks、2 masters、5 workers、
  `actual_multi_host=false`、`synthetic_topology=false`；
- 故障注入发生在 rank 0 已执行改名、但未执行父目录 fsync 的位置。两组 worker 已完成
  3/3 + 2/2 ACK，作业 exit 72；active 名称不存在，一个 retired 根保留 `state.json` 和 fenced
  record；
- 相同 epoch 重启时 `Retired GC: 1 root(s) reclaimed`，旧 retired 根消失；本次正常完成 exit 0，
  active 名称仍不存在，生成不同 retirement id 的新 retired 根，corpus seed 摘要保持正确；
- 本机 3 文件树的名称退役中位数 2620.398 us，略慢于递归删除 2593.562 us（0.990x）；129 文件
  为 2638.7405 vs 3280.479 us（1.243x）；1025 文件为 2875.461 vs 9008.331 us（3.133x）。

## 证明边界

真实 MPI 证据来自同一物理主机，故障由测试 wrapper 注入，不是断电或真实设备 `fsync` 失败。
微基准只计完成关键路径，不计树构造和后续回收，所以不能解释为端到端 campaign 加速。当前没有证明
真实多机共享存储、跨作业锁排他、NFS server failover、remote power loss、ULFM/rank repair、网络
分区、DSE throughput、solver/coverage 或 LAVA-M 提升。自动回收的 `limit` 约束根目录数量，不约束
单棵树的文件数或存储服务时延。
