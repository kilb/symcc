# F322 持久结果提交与幂等重放证据

- 日期：2026-08-07
- 环境：Linux，Open MPI 4.1.6，`mpi4py`
- 被测入口：`util/mpi_concolic_execution.py`
- 证据等级：I/T/E-mechanism
- 边界：验证进程失败后的提交重放、租约接管和故障拒绝，不是覆盖率、求解吞吐或系统掉电 benchmark

## 实验设计

所有健康场景使用1秒执行超时、1秒idle窗口、30秒wall上限，shutdown和finalize grace均为
5秒。输出目录同时承载公开的content-addressed corpus和隐藏的
`.standalone-work-<epoch>`状态。健康退出后后者必须为0；不可恢复的日志完整性错误必须保留该目录。

### 1. 普通单master

`-np 3`形成1 master + 2 workers。两个不同seed均令目标生成相同的
`f322-single-child\n`，预期公开语料为2 seed + 1去重child、分析3次、2/2关闭确认。

### 2. 普通双master

`-np 8 --workers-per-master 3`形成2 masters + 6 workers。12个不同seed均生成同一个
`f322-multi-child\n`，预期公开语料13个、分析13次且由两个master分担、双方各3/3关闭确认。

### 3. future-clock旧租约恢复

固定epoch为64个`3`。预先把一个父对象写入公开语料，并用同一epoch写入`leased`记录；记录的
`updated`比重启时钟晚3600秒，而配置TTL仍为120秒。随后以`-np 3`和
`SYMCC_STANDALONE_WORK_EPOCH=<epoch>`立即重启。若启动恢复只做普通TTL判断，该任务至少一小时内
不可接管；正确实现应在一次明确恢复边界中使用zero-TTL重新fencing并执行一次。

### 4. 部分发布的commit重放

固定epoch为64个`4`，预置父对象和状态：

1. 父任务处于`committing`，record内持久化canonical result manifest；
2. manifest声明两个child及原worker/staging identity；
3. child A已从隐藏stage移动到公开corpus；
4. child B仍留在隐藏stage；
5. 以2 masters + 6 workers并发恢复。

这模拟进程在逐对象发布中途崩溃。恢复必须验证`stage ∪ public`、幂等补发child B、由唯一
`complete_once()`胜者记账，并继续调度两个child。

### 5. 缺失manifest的不可恢复提交

固定epoch为64个`6`，预置已经越过不可逆`begin_commit`、但没有durable manifest的旧记录。这类
状态既不能按TTL偷取，也没有足够信息redo。正确行为是完整性错误、2/2 worker有界关闭、
`Abort(70)`并保留状态目录，而不是把epoch误判为空后清理。

## 实测结果

| 场景 | 退出 | 结果 | 状态目录 |
| --- | ---: | --- | ---: |
| 普通单master | 0 | 3 observations；3 generated；1 unique child；2/2 ACK | 0 |
| 普通双master | 0 | 13 observations；13 generated；1 unique child；master 0/1 = 8/5；6/6 ACK | 0 |
| future-clock租约 | 0 | `recovered=1`；1 observation；未等待120秒TTL；2/2 ACK | 0 |
| 部分commit重放 | 0 | `replayed_commits=1`；2 generated children；3 corpus objects；3 observations；6/6 ACK | 0 |
| 缺失manifest | 70 | 明确报告`lacks durable manifest`；0次analysis；2/2 ACK | 1（保留） |

部分commit场景最终三个文件名分别是父对象和两个child的真实SHA-256：

- parent：`e4bdeefdc01f2fae2c68814f4dbcdddf58f87206bf713a5d9af982ad5245018d`
- child A：`69c11a1fb47f5e88a84cd29fd0a639d32c6ee7e076e88d2171b117cae5167bf5`
- child B：`b54c8fba567e8692601844ee970e63e79b2b4c47f9411f8e9da1749bc40d1b20`

`New interesting test cases: 3`采用“最终corpus文件数减本次input目录seed数”的旧统计口径；该
恢复实验的input目录为空，因此它包含预置父对象，不能解释为生成了3个child。可信的child生成数是
manifest和日志共同记录的2。

## 原始制品

- [`normal-single.log`](normal-single.log)
- [`normal-multi.log`](normal-multi.log)
- [`future-lease-setup.txt`](future-lease-setup.txt)
- [`future-lease-recovery.log`](future-lease-recovery.log)
- [`partial-commit-setup.txt`](partial-commit-setup.txt)
- [`partial-commit-recovery.log`](partial-commit-recovery.log)
- [`missing-manifest-setup.txt`](missing-manifest-setup.txt)
- [`missing-manifest-fail-closed.log`](missing-manifest-fail-closed.log)
- [`checks.txt`](checks.txt)

[`SHA256SUMS.txt`](SHA256SUMS.txt)覆盖上述9项原始制品；README本身由顶层交付清单覆盖。

## 结论边界

实验验证的是同一共享文件系统上的MPI进程终止/重启，不是机器掉电、文件系统损坏或网络分区。
它证明content-addressed publication可在durable decision之后redo，并证明一次活跃恢复竞争中的
唯一完成记账；不证明全局exactly-once execution，也不重建前一进程已完成记录的历史统计。
