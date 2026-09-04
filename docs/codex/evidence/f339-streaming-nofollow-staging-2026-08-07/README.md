# F339 Streaming Expected-First No-Follow Staging Evidence

本目录保存 F339“流式、expected-first、descriptor/no-follow staged corpus 完整性验证”的自动化回归、
真实 Open MPI 生产链路和本机机制成本证据。F339 保持 F321-F323 的 worker-private stage、durable commit
manifest 和 crash replay 语义，但不再物化全部 `DirEntry`，未知名称在任何内容摘要前拒绝，摘要只接受通过
`O_NOFOLLOW` 打开且同 descriptor `fstat` 为 regular file 的对象。

## 文件

| 文件 | 内容 |
| --- | --- |
| `benchmark_staging_verification.py` | pre-F339 tuple/path verifier 与 F339 生产 helper 的交错时间、`tracemalloc`、异常读取和 no-follow 检查 |
| `staging-verification-cost.json` | 180 个合法集合 timing、60 个 memory、60 个异常 timing raw samples及摘要 |
| `staging-verification-cost.log` | 完整机器输出 |
| `run_staging_mpi.py` | 4-rank 真实 MPI、确定性 output-contract child 的生产 staging/promotion 驱动 |
| `staging-verification-mpi.json` | public digest、staging residue、ACK、退出码和证明范围 |
| `staging-verification-mpi.log` | 真实 MPI stdout/stderr 与机器结果 |
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
  docs/codex/evidence/f339-streaming-nofollow-staging-2026-08-07/\
benchmark_staging_verification.py \
  --output /tmp/f339-cost.json

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f339-streaming-nofollow-staging-2026-08-07/\
run_staging_mpi.py \
  --output /tmp/f339-mpi.json
```

## 关键结果

- 定向回归：209 passed + 43 subtests；六模块关联回归：326 passed + 51 subtests；完整回归：
  722 passed + 71 subtests；
- 真实 Open MPI：4 ranks、1 master、3 workers，同一物理主机和 local overlayfs。确定性 synthetic target
  产生 1 个 child，最终 seed/child 两个 public 对象均为真实 regular file 且名称等于内容 SHA-256；无 staged
  file/symlink 残留，3/3 worker ACK、exit 0、耗时 1.773135 s；
- 合法集合机制成本：每规模 3 warm-up，旧/新各 30 个交错、交替 timing 和各 10 个 `tracemalloc` peak
  样本；内容 hash 使用共同 basename stub 以隔离目录/控制结构；
- 128/1024/4096 对象时，旧算法 traced peak 中位 53,216/383,328/1,532,256 B，新算法为
  22,775/141,433/562,297 B，旧/新为 2.34x/2.71x/2.72x；
- 新算法常规时间中位分别慢 3.82%/3.26%/3.17%，不宣称正常路径 speedup；
- 单个 16 MiB 非 manifest、但名称与自身内容摘要一致的 regular file，30 次旧验证发生 30 次摘要、累计请求
  503,316,480 B（480 MiB），F339 为 0 次、0 B；1406.04x 本机热缓存时间比率不外推 campaign 性能。

## 证明边界

`tracemalloc` 不覆盖 RSS、内核目录/page cache、所有 native allocation 或远端文件系统服务端。合法集合实验
刻意 stub 内容摘要，只证明 Python 目录/集合控制结构；摘要正确性由真实文件、symlink、错误注入测试和 MPI
public digest 独立验证。真实 MPI 仍是同机 local overlayfs，synthetic target 只实现 output-directory 契约，
不调用求解器。没有真实多主机、NFS/Lustre/GPFS failover、掉电、Byzantine worker、DSE throughput、solver、
coverage、bug discovery 或 LAVA-M 提升结论。
