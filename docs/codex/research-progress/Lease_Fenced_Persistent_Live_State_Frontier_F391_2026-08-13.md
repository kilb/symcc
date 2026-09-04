# F391：租约栅栏化持久 Live-State Frontier

- 日期：2026-08-13
- 能力：`symcc-persistent-live-state-frontier-v1`
- 成熟度：I/T/E-mechanism
- 代码入口：`util/live_state_frontier.py`、`LiveContinuationExecutor.resume_persistent()`
- 图：[`lease-fenced-persistent-live-state-frontier-f391.svg`](../diagrams/lease-fenced-persistent-live-state-frontier-f391.svg)
- 证据：[`f391-persistent-live-state-frontier-2026-08-13/`](../evidence/f391-persistent-live-state-frontier-2026-08-13/)

## 1. 研究问题

此前的可执行 continuation 已能把程序计数器、调用帧、路径条件、符号值和 COW 内存写入
内容寻址存储；KLEE 风格的 BFS/DFS/random-path/NURS、target-distance、loop-exit 和 subpath
策略也已在真实 `LiveContinuationExecutor.resume()` 队列中选择状态。但执行队列和搜索器统计仍
只存在于一个 Python 进程：

1. 进程在分叉之后、返回 frontier 之前退出，会丢失尚未交给调用者的探索工作；
2. 搜索器的随机流、轮次、覆盖计数和 subpath 计数不能跨进程恢复，重启后探索次序漂移；
3. 多个执行器看见同一个 checkpoint 时没有原子 claim，可能重复执行；
4. 仅用超时把状态重新入队仍不足以保证正确性：旧 worker 可能在新 worker 接管后迟到并提交；
5. 覆盖观测与 frontier 更新若分别持久化，崩溃可能形成“任务已完成、调度反馈未合并”或反向状态。

F391 把这些问题统一为一个持久状态机。它不是新的路径选择启发式，而是让已有搜索策略和 CAS
continuation 在故障与并发下保持可恢复、可审计的执行语义。

## 2. 学术与工程依据

- GenSym/ICSE 2023 说明 continuation-passing style 能把符号路径编译成可调度的协作并发任务，
  并报告其论文实验中的并行加速；F391 延伸的是“continuation 成为工作单元”这一表示，而不是
  复述其性能结论：<https://www.cs.purdue.edu/homes/rompf/papers/wei-icse23.pdf>。
- KLEE 把 live execution state 与 Searcher 接口分离，并提供 random-path、NURS 等搜索族；F391
  持久化的正是该类搜索器所需的有界统计和选择轮次：
  <https://klee-se.org/doxygen/html/classklee_1_1Searcher.html>，原始 OSDI 2008 论文见
  <https://llvm.org/pubs/2008-12-OSDI-KLEE.html>。
- Marco/ICSE 2024 强调异步 concolic explorer 和全局分支调度；这进一步说明异步执行时“选择
  状态”与“接纳迟到结果”必须是不同协议阶段：<https://doi.org/10.1145/3597503.3623301>。
- Gray 与 Cheriton 的 SOSP 1989 lease 工作给出了有限期所有权和故障恢复的经典基础；F391 在
  lease 上再增加随机 token 与 generation CAS，避免把“已过期”错误等同于“旧结果仍可提交”：
  <https://doi.org/10.1145/74850.74870>。

这些来源没有直接给出本项目的组合协议。F391 的工程创新在于把内容寻址 continuation、可恢复
搜索器、租约、generation CAS、原子文件发布和 observation rebase 放进同一个可执行状态机。

## 3. 状态模型

一个 frontier 快照为：

\[
F=(r,p,g,R,L,D,S)
\]

- `r`：根 checkpoint SHA-256；
- `p`：唯一 program root SHA-256；
- `g`：单调 `generation`；
- `R`：有序 ready 队列；
- `L`：按 checkpoint 排序的 lease 集合；
- `D`：排序后的 done 集合；
- `S`：完整搜索器快照。

每个 lease 为：

\[
\ell=(checkpoint,token,owner,worker,expires,claimGeneration)
\]

验证器强制以下不变量：

\[
R\cap L=R\cap D=L\cap D=\varnothing
\]

\[
r\in R\cup L\cup D,\quad |R\cup L\cup D|\le B_{states}
\]

此外，所有 checkpoint/program/token 都必须是小写 64 位十六进制 SHA-256 文本；lease token 与
`claimGeneration` 在活动集合中唯一，且 `claimGeneration <= g`。任何字段缺失、额外字段、顺序
不规范、重复身份、非有限时间、布尔冒充整数或搜索器配置漂移均失败关闭。

## 4. 事务协议

### 4.1 初始化

1. 完整恢复根 continuation，验证其程序根；
2. 对当前搜索策略调用 `snapshot()`；
3. 在全局 advisory lock 内读取现有 frontier；
4. 若不存在，则原子发布 `g=0, R=[root], L=[], D=[]`；
5. 若存在，则根 checkpoint 和 program root 必须精确一致。

初始化是幂等的，但不是“清空并重建”操作。已有 frontier 不能被另一个 seed 复用。

### 4.2 Claim

协调器读取 generation `g`，在最多 `candidate_window` 个 ready 状态上计算已有搜索策略，得到
checkpoint `c`。搜索器快照必须证明恰好发生了一次选择：

- `selection_round' = selection_round + 1`；
- 当前 interleaved strategy 的计数恰好加一，其余不变；
- covered/location/subpath observation 不变；
- SplitMix64 随机流按策略消耗合法数量的 draw，且状态可从旧随机流重放得到。

随后在锁内执行 compare-and-swap：仅当磁盘 generation 仍为 `g` 且 `c in R` 时，将 `c` 从
`R` 移入 `L`、生成不可预测 token、令 `g'=g+1` 并原子发布。失败返回冲突，不产生半个 lease。

### 4.3 Heartbeat 与过期回收

执行器以 `min(30s, TTL/3)` 为周期续租，测试用极短 TTL 时下限为 50 ms。heartbeat 只有在
checkpoint 与 token 同时匹配时才更新 expiry；每次 heartbeat 也产生一个新 generation。

回收器只把 `expires <= now` 的活动 lease 移回 ready。回收本身不授权旧结果提交；一旦新 claim
生成不同 token，旧 worker 的 complete 会得到 `stale`。

### 4.4 Complete 与 observation rebase

worker 执行 continuation 后返回 pending child checkpoint 集合和本次运行观察到的 location 序列。
提交前依次执行：

1. 完整恢复每个 child，并要求 program root 与 frontier 相同；
2. 读取最新 frontier 和搜索快照；
3. 在最新搜索快照上重放本次 location observations；
4. 证明 observation transition 没有改变策略配置、selection round/count 或随机流；
5. 用最新 generation 调用 complete；
6. 若并发 heartbeat/claim 导致 generation 冲突，重新读取、重放，再试，最多 64 次；
7. token 匹配时把父状态从 lease 移入 done，把未见 child 追加到 ready，一次原子发布。

因此并发 worker 的搜索反馈不会由“最后写入者”覆盖。只有 generation 冲突会重放 observation；
`stale` 表示所有权已经改变，迟到结果直接丢弃。

### 4.5 Abandon

执行、child 校验或提交前准备出现普通异常时，当前 token 可把 checkpoint 从 lease 放回 ready。
进程被强制终止而无法 abandon 时，由 TTL 回收。token 不匹配的 abandon 不改变状态。

## 5. 持久格式与崩溃窗口

文件由 envelope 包裹 payload：

```text
schema = symcc-persistent-live-state-frontier-envelope-v1
payload = (root, program, generation, ready, leases, done, search)
sha256 = SHA256(canonical-json(payload))
```

发布顺序是：创建唯一临时文件、写满、`fsync(file)`、`rename` 到公开名、`fsync(directory)`。
读取使用 `O_NOFOLLOW`，先检查 regular file 和大小上限，再从同一 fd 读取并复核 inode/size/
mtime/ctime，最后验证摘要、schema、状态机不变量和 canonical round-trip。

| 故障点 | 磁盘可见状态 | 恢复行为 |
|---|---|---|
| claim 临时文件写入前/中失败 | 旧快照 | 状态仍 ready |
| claim rename 后进程退出 | 新快照 | lease 可 heartbeat 或 TTL 回收 |
| worker 执行中退出 | lease | TTL 后回到 ready |
| complete 前 child 校验失败 | lease/随后 abandon | 不接纳未验证 child |
| complete rename 前失败 | 旧 lease | 可重试或回收 |
| complete rename 后应答丢失 | done + children ready | 重试 token 已不活动，返回 stale，不重复接纳 |
| 旧 worker 在接管后返回 | 新 token 活动 | 旧 token stale，结果拒绝 |
| 摘要或 canonical 状态损坏 | 不可信文件 | 启动失败，不当作空 frontier |

## 6. 搜索器可恢复性

此前使用 Python `random.Random`，其内部快照与 Python 实现耦合。F391 改为版本化
`splitmix64-v1` 流，只保存 64 位 state 和 draw count；`randrange` 使用 rejection sampling，
`random()` 使用高 53 位。完整快照还保存：

- 策略序列、seed、subpath 长度与 counter budget；
- covered location 集；
- location/subpath 计数；
- 各 interleaved strategy 的选择次数和总轮次。

测试在快照前执行 7 次混合策略选择，恢复后连续 20 次选择与原对象逐项一致，最终快照也完全
一致。该结论是搜索器状态恢复一致性，不代表不同策略的覆盖率相同。

## 7. 执行器和 CLI 流程

`LiveContinuationExecutor.resume_persistent()` 的执行次序是：

1. 校验预算与根 continuation；
2. 初始化或恢复 frontier；
3. 回收已过期 lease；
4. 从持久搜索快照恢复 policy；
5. 有界物化候选 continuation，计算 CFG/coverage/target/loop 特征；
6. 选择一次并 CAS claim；
7. 启动 heartbeat 线程；
8. 调用真实 `resume()` 执行/分叉/求解；
9. 停止 heartbeat，检查续租是否丢失；
10. 验证 child，重放 observation，CAS complete；
11. 达到 claim 预算或没有 ready 状态时返回持久 telemetry。

可复现入口：

```bash
python3 util/symcc_live_state.py STORE resume-persistent CHECKPOINT FRONTIER \
  --owner coordinator-a --worker 0 --max-claims 64 \
  --max-steps 1000 --max-states 64 --candidate-window 4096 \
  --frontier-max-states 100000 --lease-ttl 300 --conflict-retries 64

python3 util/symcc_live_state.py STORE frontier-inspect FRONTIER
```

CLI inspection 会重新验证整个 envelope 和搜索快照；lease token 不输出，避免把 inspection 当成
所有权接口。

## 8. 实现映射

| 文件 | 关键实现 |
|---|---|
| `util/live_state_frontier.py` | 快照 schema、不变量、锁、原子发布、claim/heartbeat/recover/complete/abandon |
| `util/live_state_search.py` | SplitMix64、完整 snapshot/restore、selection/observation 转移验证 |
| `util/live_continuation.py` | 真实执行器接入、候选窗口、后台续租、child closure、observation rebase |
| `util/symcc_live_state.py` | `resume-persistent` 与 `frontier-inspect` |
| `benchmark/benchmark_live_state_frontier.py` | 可重复机制开销测量 |
| `test/test_persistent_live_state_frontier.py` | 协议、损坏、并发、故障注入、随机状态机 |
| `test/test_persistent_live_continuation.py` | 执行器、跨进程 CLI、heartbeat 丢失、双执行器 |

## 9. 测试与实测结果

### 9.1 定向正确性

- F391：19 tests passed，6 subtests passed；
- 相关 live-state：109 tests passed，57 subtests passed；
- 并发双执行器测试额外重复 10 轮，均保持两个不同 child 各完成一次；
- 随机协议状态机：固定 seed 的 80 步操作，每步重新打开文件并验证 partition、根可达和 generation
  单调；
- 原子替换故障注入：旧快照保持不变且无临时文件残留；
- digest、non-canonical owner、future claim-generation、错误随机流和策略配置漂移均拒绝。

第一轮 warnings-as-errors 完整门禁还发现 `_FeasibilitySolver` 的 artifact 临时目录依赖
`TemporaryDirectory.__del__`：执行器未被显式关闭时，资源警告会延迟到无关测试。修复将目录改为
`mkdtemp` 加幂等 `weakref.finalize(shutil.rmtree)`，显式 `close()` 与回收路径共享同一清理动作；
`run_program` 和 CLI 的普通 run/resume 入口也改为 context manager。新增回归在删除 executor 后强制
GC，验证目录消失且没有 `ResourceWarning`。

最终完整门禁：Python **938 passed + 235 subtests**，规范 node-ID **938/938** 精确一致，且
skip/xfail/xpass/deselect/missing/unexpected 均为 0；LLVM lit 共发现 **250**，其中 **249 passed +
1 unsupported**、0 failed。

### 9.2 机制微基准

命令：

```bash
python3 benchmark/benchmark_live_state_frontier.py \
  --counts 1,100,1000,5000 --samples 31
```

环境：Linux 7.0.0-28-generic、Python 3.12.3、AMD Ryzen Threadripper PRO 9995WX，工作区位于
overlay filesystem。每个 durable mutation 都包含文件和目录 fsync；数字是当前机器单次实验，
不是跨机器性能结论。

| 累计状态 | frontier bytes | snapshot median | snapshot p95 | claim | abandon |
|---:|---:|---:|---:|---:|---:|
| 2 | 889 | 0.044 ms | 0.100 ms | 6.323 ms | 5.637 ms |
| 101 | 7,522 | 0.176 ms | 0.353 ms | 5.111 ms | 5.026 ms |
| 1,001 | 67,822 | 1.324 ms | 1.619 ms | 8.657 ms | 7.616 ms |
| 5,001 | 335,822 | 5.984 ms | 7.184 ms | 19.082 ms | 17.132 ms |

读和写开销随单文件状态数增长；这验证了有界实现的成本曲线，也直接否定“大规模前沿免费持久化”
的说法。`candidate_window` 只限制 continuation 物化成本，不消除全文件 durable write 成本。

## 10. 明确边界

F391 可以声称：

- 真实 continuation executor 已接入可恢复 frontier；
- 搜索器随机流和有界统计可跨进程精确恢复；
- claim 唯一、迟到结果由 token 栅栏拒绝；
- generation 冲突下 observation 可重放而不覆盖并发反馈；
- 文件发布具有本机文件系统上的 failure-atomic rename 与 durability barrier。

F391 不能声称：

- 已提高 edge coverage、LAVA-M 漏洞数、求解速度或端到端吞吐；
- 单个 JSON frontier 适合十万级高频 heartbeat 的大集群；
- wall-clock lease 在任意时钟回拨环境中自动保持可用性；
- advisory lock/rename 在未经资格验证的所有网络文件系统上都提供跨主机语义；
- 已替代 MPI helper 现有的分片 lease/result admission 协议；二者目前是不同部署入口；
- 已完成 sharded append-only frontier、共识复制或 Byzantine 容错。

下一步若追求大规模部署，应把 `(R,L,D)` 拆为分片日志或内容寻址版本根，用小型 CAS 只发布根
generation，并复用项目 F320--F355 已验证的共享文件系统资格、跨协调器 lease 与结果提交协议。
