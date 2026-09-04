# F321 失败原子 frontier 机制证据

- 日期：2026-08-07
- 环境：Linux，Open MPI 4.1.6，`mpi4py`
- 被测入口：`util/mpi_concolic_execution.py`
- 证据等级：I/T/E-mechanism
- 边界：验证分布式协议与故障拒绝，不是覆盖率或求解吞吐 benchmark

## 场景与参数

前两项健康运行使用 8 个 rank：2 masters、每组 3 workers；输入为12个内容不同的seed。第一项
以`/bin/true`验证无子输出的全局所有权，第二项令每次执行生成相同的
`f321-shared-staged-child\n`，验证worker隐藏暂存、master摘要检查、父任务不可逆commit fence和
content-addressed去重后的公开发布。第三项以1 master + 2 workers、2个seed生成同一个child，
验证没有共享work coordinator时仍走相同stage/verify/promote路径。故障注入使用1 master +
2 workers，在输出目录预置一个
“文件名是期望内容 SHA-256、实际字节却不同”的对象。第一项健康运行和故障注入目标为
`/bin/true`，shutdown/finalize grace均为5秒；暂存场景使用下列shell目标和默认120/30秒grace。
所有场景的execution timeout与idle window均为1秒。

暂存子输出运行的等价重放命令：

```bash
mpirun -np 8 python3 util/mpi_concolic_execution.py \
  -i "$CASE/in" -o "$CASE/out" -t 1 --max-idle 1 \
  --workers-per-master 3 -- /bin/sh -c \
  'mkdir -p "$SYMCC_OUTPUT_DIR"; printf "f321-shared-staged-child\n" \
   > "$SYMCC_OUTPUT_DIR/out"; cat >/dev/null'
```

故障注入在运行前执行：

```bash
printf 'expected-corpus-object\n' > "$CASE/in/seed"
work_hash=$(sha256sum "$CASE/in/seed" | cut -d' ' -f1)
printf 'corrupted-object\n' > "$CASE/out/$work_hash"
```

随后以`-np 3`、1秒timeout/idle、5秒shutdown/finalize grace和`--wall-timeout 10`运行框架。

## 观测结果

同一最终代码状态的静态门禁、定向测试和完整测试分别为：`py_compile`通过、Ruff通过、
`107 passed + 10 subtests`（5.51秒）和`618 passed + 38 subtests`（82.66秒）。这些摘要记录在
`checks.txt`；MPI日志独立保存如下。

| 场景 | 结果 | 关键证据 |
| --- | --- | --- |
| 健康双 master | exit 0；12 observations；master 0/1 = 6/6；corpus=12 | 两组均 3/3 shutdown ACK；global quiescence committed；lease 目录清零 |
| 暂存子输出双 master | exit 0；13 observations；master 0/1 = 7/6；corpus=13 | 12个seed只公开1个唯一child；两组3/3 ACK；隐藏job-state目录清零 |
| 暂存子输出单 master | exit 0；3 observations；master 0 = 3；corpus=3 | 2个seed只公开1个唯一child；2/2 ACK；隐藏job-state目录清零 |
| corpus 摘要错配 | exit 70；0 次 worker execution | expected/observed SHA-256 同时进入控制错误；2/2 shutdown ACK；随后显式 MPI Abort(70) |

故障场景的期望摘要为
`296d2b7cf9b0b70d9d915f72ce7b993a0f564838fa7576bb82faf20fcbebad47`，实际摘要为
`1b5a2d2c5f5dce29936736a63fb08b39a3f6fadc5a36a4e9b19d229de13f781f`。错误在初始
corpus 准入阶段发现，日志中没有 analysis observation；这证明错误字节未进入 worker，但不证明
真实符号执行目标上的覆盖率收益。

原始日志：

- [`healthy-two-master.log`](healthy-two-master.log)
- [`staged-child-two-master.log`](staged-child-two-master.log)
- [`staged-child-single-master.log`](staged-child-single-master.log)
- [`corrupt-corpus-fail-closed.log`](corrupt-corpus-fail-closed.log)
- [`checks.txt`](checks.txt)

目录内 [`SHA256SUMS.txt`](SHA256SUMS.txt) 覆盖上述五项原始证据；顶层
`docs/codex/verify_delivery.py` 还会解析关键计数、ACK、摘要和退出边界。
