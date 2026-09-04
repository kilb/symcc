# F337 Resumable Budgeted Retired-Tree GC Evidence

本目录保存 F337“三预算、可恢复 retired-tree GC”的测试、真实 Open MPI 跨启动收敛、descriptor-relative
syscall 轨迹和本机机制成本。生产启动路径同时约束选中根数、成功的 `unlink/rmdir` 目录项变更数，以及
文件系统调用之间检查的协作式时间预算；单个阻塞 syscall 不可由 Python 抢占，因此时间预算不是硬实时
上限。

## 文件

| 文件 | 内容 |
| --- | --- |
| `run_budgeted_gc_mpi.py` | 7-rank/2-master、三次真实 MPI 跨启动部分删除与收敛驱动 |
| `budgeted-gc-mpi.json` | 机器可读拓扑、预算、每次状态、ACK、退出码和边界 |
| `budgeted-gc-mpi.log` | 三次真实 MPI 的完整 stdout/stderr |
| `benchmark_budgeted_gc.py` | 129/1025/4097 文件完整删除与固定 64-entry 单步交错基准 |
| `budgeted-gc-cost.json` | 120 个 raw timing 样本、摘要和默认预算收敛步骤 |
| `budgeted-gc-cost.log` | 机制基准原始输出 |
| `trace_budgeted_gc_syscalls.py` | 4-file 根上执行 2-entry 单步的最小驱动 |
| `budgeted-gc-syscalls.strace` | 包含 Python 启动和夹具的完整 `openat/getdents64/unlinkat/fsync` 轨迹 |
| `budgeted-gc-syscalls-relevant.strace` | F337 根相关的筛选轨迹；核心段恰好两次 `unlinkat` |
| `budgeted-gc-syscalls.json` | `2 removed / incomplete / entry-limit / 2 remaining` 观察 |
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
  docs/codex/evidence/f337-resumable-budgeted-retired-gc-2026-08-07/\
run_budgeted_gc_mpi.py --output /tmp/f337-mpi.json

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f337-resumable-budgeted-retired-gc-2026-08-07/\
benchmark_budgeted_gc.py --output /tmp/f337-cost.json

PYTHONDONTWRITEBYTECODE=1 strace -f -qq -yy \
  -e trace=openat,getdents64,unlinkat,fsync,close \
  -o /tmp/f337.strace python3 \
  docs/codex/evidence/f337-resumable-budgeted-retired-gc-2026-08-07/\
trace_budgeted_gc_syscalls.py --output /tmp/f337-syscalls.json
```

## 关键结果

- 定向测试：204 passed + 43 subtests；关联测试：321 passed + 51 subtests；完整回归：
  717 passed + 71 subtests；
- 真实 MPI：7 ranks、2 masters、5 workers，同一物理主机和 local overlayfs。预置 5-file old retired root，
  root limit 1、entry budget 2、time budget 1 s；三次运行分别得到 5→3、3→1、1+root→absent，每次均
  3/3 + 2/2 worker ACK、exit 0，并正常退役本次 epoch；
- syscall：核心单步以 `O_NOFOLLOW|O_DIRECTORY` 相对 parent fd 打开 root，恰好执行两次
  `unlinkat(tree_fd, "f337-entry-*", 0)`，随后 `fsync(tree_fd)`；机器结果仍有两个文件；
- 每个规模 2 warm-up + 20 完整删除 + 20 固定单步交错样本。129/1025/4097 文件完整删除中位为
  3.372/7.365/28.521 ms，64-entry 单步为 3.089/3.579/3.648 ms；完整路径跨规模增长 8.459x，
  固定单步增长 1.181x；
- 4097 文件用生产默认 4096-entry/50-ms 预算两步收敛：4096 entries / 27.042 ms，再 2 entries /
  2.945 ms，总计 30.032 ms。总成本约为完整删除中位的 1.053x，表明工作被摊销而非消失。

## 证明边界

真实 MPI、syscall 和成本证据均来自同一物理主机的 local overlayfs。时间预算只在文件系统调用之间检查，
不能抢占一个阻塞 syscall；shared root 顶层完整 namespace 扫描也尚未纳入 entry budget。数据不证明真实
多机共享存储、NFS/Lustre/GPFS 多客户端、server failover、remote power loss、网络分区、非协作写者、
ULFM/rank repair、DSE throughput、solver、coverage、bug discovery 或 LAVA-M 提升。
