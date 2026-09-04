# F338 Streaming Bounded-Memory Retired Discovery Evidence

本目录保存 F338“完整预验证、流式有界内存 retired-root 发现”的自动化回归、真实 Open MPI 集成结果和
本机算法成本。F338 保留 F337 的完整 reserved namespace 预验证与确定性词法选择，但不再把全部
`os.DirEntry` 和候选名称同时物化后全排序，而是只保留不超过 root limit 的词法最小候选。

## 文件

| 文件 | 内容 |
| --- | --- |
| `benchmark_streaming_discovery.py` | 旧 tuple/list/full-sort 与生产 streaming top-k 的交错时间和 `tracemalloc` 峰值驱动 |
| `streaming-discovery-cost.json` | 180 个 timing、60 个 memory raw samples、摘要和严格边界 |
| `streaming-discovery-cost.log` | 完整机器输出 |
| `run_streaming_discovery_mpi.py` | 17 候选、root limit 4 的 7-rank/2-master 真实 MPI 驱动 |
| `streaming-discovery-mpi.json` | 扫描统计、选择结果、ACK、退出码、状态守恒和证明范围 |
| `streaming-discovery-mpi.log` | 真实 MPI stdout/stderr 与机器结果 |
| `directed-tests.log` | distributed/lifecycle/filesystem 定向回归 |
| `integration-tests.log` | 六模块 MPI/distributed/hybrid 关联回归 |
| `full-tests.log` | 完整 warnings-as-errors Python 回归 |
| `checks.txt` | 实现、环境、结果和严格证明边界 |
| `SHA256SUMS.txt` | 本目录完整性清单 |

## 复现

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error \
  test/test_distributed_state.py \
  test/test_mpi_lifecycle.py \
  test/test_mpi_filesystem_qualification.py

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error \
  test/test_mpi_filesystem_qualification.py \
  test/test_mpi_lifecycle.py \
  test/test_distributed_state.py \
  test/test_afl_profile_orchestration.py \
  test/test_hybrid_feedback.py \
  test/test_tace_profile.py

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error test/test_*.py

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f338-streaming-retired-discovery-2026-08-07/\
benchmark_streaming_discovery.py \
  --output /tmp/f338-cost.json

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f338-streaming-retired-discovery-2026-08-07/\
run_streaming_discovery_mpi.py \
  --output /tmp/f338-mpi.json
```

## 关键结果

- 定向回归：205 passed + 43 subtests；六模块关联回归：322 passed + 51 subtests；完整回归：
  718 passed + 71 subtests；
- 真实 Open MPI：7 ranks、2 masters、5 workers，同一物理主机和 local overlayfs。共享根扫描 19 个条目，
  识别 17 个候选并选择词法最小的 4 个；4 个空根以 4 次 `rmdir` 回收，13 个 fixture 根保留，本次 epoch
  正常产生 1 个新 retired 根，因此最终严格为 14 个。两组 worker ACK 为 3/3 和 2/2，exit 0；
- 本机成本：每个规模 3 warm-up，旧/新算法各 30 个交错、交替 timing 样本和各 10 个交错、交替
  `tracemalloc` 峰值样本；选择 limit 为 8，目录构造不计时；
- 128/1024/4096 候选时，旧算法 Python traced peak 中位为 66,130/502,762/1,995,370 B，新算法为
  3,586/3,652/3,652 B，分别降低 18.44x/137.67x/546.38x；
- 同规模旧算法时间中位为 299.475/2,195.427/9,245.085 us，新算法为
  318.804/2,284.486/9,356.031 us，即新路径分别慢 6.45%/4.06%/1.20%。F338 不宣称时间提升。

## 证明边界

两条算法都完整扫描 shared root 并验证每个 reserved candidate，因而 F338 约束的是 Python 保留对象数量，
不是 `getdents/is_dir` 次数或硬 wall-clock。`tracemalloc` 不覆盖内核目录缓冲、页缓存、文件系统服务端、
所有 native allocator 或整个进程 RSS。真实 MPI 仍是同机 local overlayfs，未证明 NFS/Lustre/GPFS
多客户端、server failover、非协作写者、长期公平性、DSE throughput、solver、coverage、bug discovery
或 LAVA-M 提升。
