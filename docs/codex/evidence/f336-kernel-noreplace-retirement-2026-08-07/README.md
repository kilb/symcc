# F336 Kernel-Enforced No-Clobber Retirement Evidence

本目录保存 F336“内核强制 no-clobber epoch 退役”的自动化测试、双进程竞争、真实 Open MPI 冲突恢复、
syscall 轨迹和本机机制成本。生产完成路径使用 Linux `renameat2(RENAME_NOREPLACE)`，不再以
`lexists + os.replace` 近似目标不存在语义；persistent service 启动前还在真实 output root 上验证一次
成功 rename 和一次 `EEXIST`。

## 文件

| 文件 | 内容 |
| --- | --- |
| `run_noreplace_retirement_mpi.py` | 7-rank/2-master 真实 MPI 目标冲突、Abort(72) 与同 epoch 恢复驱动 |
| `noreplace-retirement-mpi.json` | 机器可读拓扑、ACK、退出码、双名称保持、GC 和 corpus 观察 |
| `noreplace-retirement-mpi.log` | 两次真实 MPI 原始 stdout/stderr |
| `benchmark_noreplace_retirement.py` | durable replace/no-replace 与 startup probe 交错机制基准 |
| `noreplace-retirement-cost.json` | 60+60+60 个 raw 样本和重算摘要 |
| `noreplace-retirement-cost.log` | 基准原始输出 |
| `trace_noreplace_syscall.py` | 成功 rename 与既有目标拒绝的最小复现 |
| `noreplace-syscall.strace` | 真实 `RENAME_NOREPLACE=0`、parent fsync、`EEXIST` syscall |
| `noreplace-syscall.log` | syscall 驱动输出 |
| `directed-tests.log` | distributed/lifecycle/filesystem 定向回归 |
| `integration-tests.log` | 六模块相关回归 |
| `full-tests.log` | 完整 warnings-as-errors Python 回归 |
| `checks.txt` | 环境、结果和严格证明边界 |
| `SHA256SUMS.txt` | 本目录完整性清单 |

## 复现

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -q -W error \
  test/test_distributed_state.py \
  test/test_mpi_lifecycle.py \
  test/test_mpi_filesystem_qualification.py

PYTHONDONTWRITEBYTECODE=1 pytest -q -W error \
  test/test_mpi_filesystem_qualification.py \
  test/test_mpi_lifecycle.py \
  test/test_distributed_state.py \
  test/test_afl_profile_orchestration.py \
  test/test_hybrid_feedback.py \
  test/test_tace_profile.py

PYTHONDONTWRITEBYTECODE=1 pytest -q -W error test/test_*.py

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f336-kernel-noreplace-retirement-2026-08-07/\
run_noreplace_retirement_mpi.py --output /tmp/f336-mpi.json

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f336-kernel-noreplace-retirement-2026-08-07/\
benchmark_noreplace_retirement.py --output /tmp/f336-cost.json

PYTHONDONTWRITEBYTECODE=1 strace -f -qq \
  -e trace=renameat2,fsync -o /tmp/f336.strace \
  python3 docs/codex/evidence/\
f336-kernel-noreplace-retirement-2026-08-07/trace_noreplace_syscall.py
```

## 关键结果

- 定向测试：199 passed + 30 subtests；关联测试：316 passed + 38 subtests；完整回归：
  712 passed + 58 subtests；
- 双进程竞争测试要求严格一个 publisher、一个 `EEXIST` rejection，输家 source 保留；
- syscall 直接记录一次成功的 `renameat2(..., RENAME_NOREPLACE)`、一次 parent `fsync`，以及同目标第二次
  `renameat2` 返回 `EEXIST`；
- 真实 MPI：7 ranks、2 masters、5 workers、同机真实 transport、无 synthetic topology。3/3 + 2/2
  worker ACK 后固定目标冲突，active 与已 fsync 的 retired marker 均保留，exit 72；相同 epoch 重启 GC
  一个冲突根，再正常完成 exit 0；
- 60+60 个交错 rename 样本中，durable replace/no-replace 中位数为 2598.252/2603.320 us，差
  5.068 us、比率 1.002x；
- 60 个完整 startup probe 样本中位 11.944 ms、P95 13.024 ms；该成本在 persistent service 前支付
  一次，不在 worker solver 热路径。

## 证明边界

真实 MPI、syscall 和成本证据均来自同一物理主机的 overlayfs。目标冲突由测试 wrapper 确定性创建，
不是自然随机碰撞。当前数据不证明真实多机共享存储、NFS/Lustre/GPFS 跨客户端 flag 语义、server
failover、remote power loss、网络分区、ULFM/rank repair，也不测量 DSE throughput、solver、coverage、
bug discovery 或 LAVA-M 提升。F335 的 GC limit 仍只约束根数量，不约束单棵树的 entry 数或 wall time。
