# F320 真实 MPI 机制证据

- 日期：2026-08-07
- 主机环境：Linux，Open MPI 4.1.6，`mpi4py`
- 被测入口：`util/mpi_concolic_execution.py`
- 证据范围：全局任务所有权、内容去重、双 master 分配、两阶段静止提交、worker shutdown
- 证据等级：E-mechanism；**不是**真实程序覆盖率或求解吞吐 benchmark

## 1. 固定实验参数

四组运行均使用每次执行 1 秒 timeout、1 秒稳定 idle window、5 秒 shutdown grace 和
5 秒 finalize grace。双 master 使用 8 个 MPI rank：2 masters，各带 3 workers；单 master
使用 3 个 rank：1 master + 2 workers。除共享子输入实验外，目标均为 `/bin/true`，因此结果只
验证分布式控制面与任务面不变量，不衡量 SymCC 的约束生成或覆盖率能力。

```bash
export SYMCC_SHUTDOWN_GRACE_SEC=5
export SYMCC_FINALIZE_GRACE_SEC=5

mpirun -np "$NP" python3 util/mpi_concolic_execution.py \
  -i "$CASE/in" -o "$CASE/out" -t 1 --max-idle 1 \
  --workers-per-master 3 -- "${TARGET[@]}"
```

这里 `$NP` 为 3 或 8；`$CASE` 对应下表中的临时实验目录。该命令是根据实际参数整理的
等价重放形式，原始 stdout/stderr 保存在同表所列日志中。

## 2. 输入与场景

| 场景 | 输入构造 | 目标 | 原始日志 |
| --- | --- | --- | --- |
| 单 master 回归 | `single-master-seed\n` | `/bin/true` | [`single-master.log`](single-master.log) |
| 双 master、单 seed | `seed-one\n` | `/bin/true` | [`two-master-one-seed.log`](two-master-one-seed.log) |
| 双 master、24 seeds | `distribution-seed-%03d\n`，1..24 | `/bin/true` | [`two-master-24-seeds.log`](two-master-24-seeds.log) |
| 双 master、共享 child | `integrity-seed-%03d\n`，1..12 | shell 目标，每次发布相同 child | [`two-master-shared-child.log`](two-master-shared-child.log) |

共享 child 目标的实参等价于：

```bash
/bin/sh -c 'mkdir -p "$SYMCC_OUTPUT_DIR"; \
  printf "shared-generated-child\n" > "$SYMCC_OUTPUT_DIR/out"; \
  cat >/dev/null'
```

## 3. 观测结果

| 场景 | observations | generated | interesting | master 分布 | corpus 文件 | 退出码 |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| 单 master | 1 | 0 | 0 | 0=1 | 1 | 0 |
| 双 master、单 seed | 1 | 0 | 0 | 该次早期日志未输出分解 | 1 | 0 |
| 双 master、24 seeds | 24 | 0 | 0 | 0=12, 1=12 | 24 | 0 |
| 双 master、共享 child | 13 | 13 | 1 | 0=6, 1=7 | 13 | 0 |

四组运行完成后均检查到 0 个 `.standalone-work-*` 目录。四份日志均未出现
`digest mismatch` 或 `input hash mismatch`；详细检查记录见 [`checks.txt`](checks.txt)。

关键解释：

1. 双 master 单 seed 只有 1 次 observation，证明同一内容没有被两个 master 重复执行；
2. 24 个不同 seed 恰有 24 次 observation 和 24 个 corpus 文件，排除“通过漏任务减少执行”
   的解释；
3. 12 个 seed 每个都生成同一 child，`generated=13` 是 12 个初始任务各自上报生成一次，加上
   child 自身执行后再次生成一次；`interesting=1` 和 13 个 corpus 文件证明相同 child 只被发布
   和分析一次；
4. `generated` 是 worker 输出文件观测数，不等于新增唯一输入数，因此不能用 13/1 推导覆盖率。

F319 的双 master 单 seed 基线为 2 次 observation，见 F320 研究报告中的基线记录。本目录没有
保存当时的原始 F319 日志，故不把该基线纳入本目录 SHA-256 原始证据的可重算范围。

## 4. 完整性与复核

[`SHA256SUMS.txt`](SHA256SUMS.txt) 覆盖本目录 README 之外的四份原始日志和检查记录。
仓库交付验证器会检查清单完整性、摘要、关键计数、提交/关闭语句以及“非覆盖率 benchmark”
边界。复核命令：

```bash
(cd docs/codex/evidence/f320-global-work-ownership-2026-08-07 && \
  sha256sum -c SHA256SUMS.txt)
python3 docs/codex/verify_delivery.py
```

