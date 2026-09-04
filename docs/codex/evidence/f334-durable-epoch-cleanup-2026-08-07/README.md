# F334 Durable Completed-Epoch Cleanup Evidence

本目录保存 F334“完成 epoch 持久清理与 finalize 门控”的测试、真实 Open MPI 故障注入和本机机制成本。
F334 修复 clean lifecycle 末尾 `shutil.rmtree(..., ignore_errors=True)` 可能静默失败的问题：所有 rank
先停止工作并进入 pre-cleanup barrier，rank 0 删除 work-state 根目录并 `fsync` 父目录，其他 rank 在
post-cleanup bounded barrier 等待；只有清理已确认后才允许共同 `MPI_Finalize`。

## 文件

| 文件 | 内容 |
| --- | --- |
| `run_durable_epoch_cleanup_mpi.py` | 7 rank/2 master 的删除前 I/O 故障及同 epoch 恢复驱动 |
| `durable-cleanup-mpi.json/.log` | 命令、退出码、ACK 次序、状态观察、严格边界和完整输出 |
| `benchmark_durable_cleanup.py` | 旧式未确认 rmtree 与父目录 fsync 后 rmtree 的交错微基准 |
| `durable-cleanup-cost.json/.log` | 两组各 100 个逐次样本、汇总、增量与证明边界 |
| `directed-tests.log` | distributed state、MPI lifecycle、filesystem qualification 定向回归 |
| `integration-tests.log` | MPI/distributed/hybrid 六模块关联回归 |
| `full-tests.log` | 完整 warnings-as-errors Python 回归 |
| `checks.txt` | 审计摘要和严格证明边界 |
| `SHA256SUMS.txt` | 本目录完整性清单 |

## 复现

```bash
pytest -q \
  test/test_distributed_state.py \
  test/test_mpi_lifecycle.py \
  test/test_mpi_filesystem_qualification.py

pytest -q -W error test/test_*.py

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f334-durable-epoch-cleanup-2026-08-07/\
run_durable_epoch_cleanup_mpi.py \
  --output /tmp/f334-mpi.json

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f334-durable-epoch-cleanup-2026-08-07/\
benchmark_durable_cleanup.py \
  --output /tmp/f334-cost.json
```

## 关键结果

- 定向测试：191 passed + 24 subtests；关联测试：308 passed + 32 subtests；完整回归：
  704 passed + 52 subtests。新增反例证明普通直属目录、短 epoch 和大写 epoch 名称均在删除前
  被拒绝，目录内 marker 保持不变。
- 真实 Open MPI 使用 7 ranks、2 masters、5 workers。同机且未伪造 processor identity，
  `synthetic_topology=false`。
- rank 0 的 work-state 删除前注入 I/O 失败后，worker 已先完成 `3/3 + 2/2` ACK；root 随后
  `Abort(72)`。`state.json` 与 fenced record 仍存在，不会静默报告成功。
- 相同 epoch 取消注入后以 0 返回，完成状态目录已删除；说明失败状态可以由正常恢复路径收敛。
- 单元测试另行覆盖“rmtree 已可见、父目录 fsync 失败”：异常必须传播。此时删除是否跨崩溃持久化
  未知，因此作业仍失败关闭，但不能声称状态必然保留。
- 本机 overlayfs 两文件合成 epoch 的 100 组交错样本中，旧式 rmtree 中位/p95 为
  `70.0455/89.755 us`，durable rmtree 为 `2756.892/3079.947 us`，中位增加
  `2686.8465 us`（`39.3586x`）。这是一次完成时目录屏障的可靠性成本。

## 证明边界

`actual_mpi_transport=true`、`actual_multi_host=false`、`synthetic_topology=false`、
`deployment_evidence=false`。证据验证本机生产接线、ACK-before-cleanup、错误码、状态保留和恢复；
不证明远端共享存储的断电持久性、rank repair、网络分区、ULFM、MPI barrier 的规模成本、DSE 吞吐、
求解速度、覆盖率或 LAVA-M 提升。
