# F333 Durable Renewal Configuration Manifest Evidence

本目录保存 F333“跨重启的续期配置持久承诺”的自动化测试、真实 Open MPI 接线实验和机制成本数据。
F333 位于 F332 的 live master 配置共识之后：只有已封印的 controller 才能把 canonical 配置以
create-once manifest 绑定到 work epoch；同 epoch 恢复时必须 exact-byte 匹配，不能在一次重启中
静默采用新的 interval、有效 timeout 或 jitter。

## 文件

| 文件 | 内容 |
| --- | --- |
| `run_durable_configuration_manifest_mpi.py` | 7 rank/2 master 真实 MPI 三阶段驱动：落盘后崩溃、统一漂移恢复、原配置恢复 |
| `durable-configuration-mpi.json` | 可机读配置、manifest、状态观察、metrics、退出码与严格边界 |
| `durable-configuration-mpi.log` | 三阶段完整 stdout/stderr 与 JSON 回显 |
| `benchmark_manifest_reuse.py` | exact-read 快路径与旧式临时文件 I/O 反事实的交错微基准 |
| `manifest-reuse-cost.json/.log` | 100 次交错复用与 30 次首次发布的统计汇总（未保存逐次样本） |
| `directed-tests.log` | lifecycle/filesystem qualification 定向回归 |
| `integration-tests.log` | MPI/distributed/hybrid 六模块关联回归 |
| `full-tests.log` | 完整 warnings-as-errors Python 回归 |
| `checks.txt` | 审计摘要、结论和证明边界 |
| `SHA256SUMS.txt` | 本目录文件完整性清单 |

## 复现

```bash
pytest -q test/test_mpi_lifecycle.py test/test_mpi_filesystem_qualification.py

pytest -q \
  test/test_mpi_filesystem_qualification.py \
  test/test_mpi_lifecycle.py \
  test/test_distributed_state.py \
  test/test_afl_profile_orchestration.py \
  test/test_hybrid_feedback.py \
  test/test_tace_profile.py

pytest -q -W error test/test_*.py

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f333-durable-renewal-configuration-manifest-2026-08-07/\
run_durable_configuration_manifest_mpi.py \
  --output /tmp/f333-mpi.json

PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f333-durable-renewal-configuration-manifest-2026-08-07/\
benchmark_manifest_reuse.py \
  --output /tmp/f333-cost.json
```

## 关键结果

- 定向测试：53 passed + 14 subtests；关联测试：306 passed + 29 subtests；完整回归：
  702 passed + 49 subtests。
- 固定 epoch 的初始作业在 root 报告 manifest 后接受进程组 `SIGKILL`，退出 `-9`；落盘 manifest
  指纹为 `1c94067d4355899cf3af9ecefc65785ecca2fa3db1c50586dfa59d1931c96f30`，文件
  SHA-256 为 `8d27f0a74d20883b35cbc3a2f6a732fd8a10d8d9af2f98462504b9bbcc244c9e`。
- 所有 rank 统一改用 jitter `0.25` 后，live master 共识本身可以成立，但两个 master 都在 durable
  compare 处拒绝；双方 generation/attempts 保持 0/0，3/3 + 2/2 worker ACK 后以 70 退出。
- 漂移恢复没有覆盖旧承诺：失败前后 manifest SHA-256 完全相同，work state 保留供正确配置重试。
- 恢复 jitter `0.1` 后，双方完成 3 代、3 attempts/3 successes/0 failures，以 0 退出并清理 epoch
  状态目录。
- 本机交错机制微基准中，exact-read 复用中位 `10.481 us`，删除前的临时文件反事实中位
  `2698.4085 us`，中位节省 `2687.9275 us`，比率 `257.457x`；首次发布中位
  `7372.645 us`。该结果主要由 overlayfs 上的 file `fsync` 成本决定。

## 证明边界

`actual_mpi_transport=true`，但 `synthetic_topology=true`、`actual_multi_host=false`、
`deployment_evidence=false`。真实 MPI 实验验证生产接线、崩溃后状态保留、exact durable compare、
worker ACK 次序和成功恢复；processor identity 与进程组崩溃由测试驱动注入。它不证明远端共享存储
语义、MPI rank repair、网络分区恢复、ULFM、符号执行吞吐、求解速度、覆盖率或 LAVA-M 提升。
