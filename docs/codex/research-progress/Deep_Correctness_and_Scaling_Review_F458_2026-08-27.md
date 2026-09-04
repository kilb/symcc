# F458：并行执行正确性与扩展性深度审查

**日期：** 2026-08-27  
**性质：** 代码、架构、并发协议与实验算法审查  
**范围：** 编译器门禁、AFL/SymCC 覆盖收敛、QueryStore、ULFM、coverage owner、持久 continuation、并行规模模型、消融统计和 master 热路径

## 1. 结论摘要

本轮审查没有继续叠加新的路径搜索策略，而是检查现有技术能否在长时、多 worker、多 master、超时和进程故障条件下保持同一语义。审查发现并修复了四类问题：

1. **错误的工作准入。** AFL concrete replay 失败的输入仍可能进入符号执行，导致 worker 在覆盖基线不完整时重复处理；异步队列饱和后，未提交的 queue 后缀还会被延迟到周期性审计。
2. **租约保护范围不完整。** QueryStore 只在领取时建立租约，慢求解和慢结果校验可能越过 TTL；持久 continuation 在发布 frontier 前停止心跳，留下重复领取窗口；ULFM communicator shrink 使用阻塞操作时无法兑现超时契约。
3. **跨分片与跨实验轮次的错误归因。** coverage owner 的多分片批次不是一个可恢复事务；删失运行被排除后，失败较多的方案可能反而得到更好的中位数和 A12；资源规模统计混淆 MPI rank、master 和 worker。
4. **master 串行热路径与模型失真。** AFL showmap、分支密度和 TopSeed/K-Scheduler profile 会阻塞调度线程；进程内去重集合随 campaign 增长；USL 和覆盖饱和模型即使拟合质量为负或观测非单调仍会给出“规模上限”；自适应控制器还混合了不同量纲的信号。

修复后，系统形成了四条新的强不变量：

- 只有完成 concrete replay 且文件身份未变化的 AFL 输入，才可进入符号执行任务队列；
- query 和 continuation 的租约覆盖“计算 + 校验 + 持久提交”，最终结果仍由 token fencing 裁决；
- 多分片 coverage claim 先发布完整事务意图，再幂等前滚全部 shard，重启不能暴露永久部分提交；
- 实验分析对删失、无效模型和真实 worker 数量失败关闭，不再为不可信数据生成确定性结论。

这些结果证明本轮的**机制正确性和回归完整性**，不等价于已经证明公共 benchmark 上的 coverage、速度或缺陷发现率提升。

## 2. 审查方法与系统边界

### 2.1 按执行链路审查

审查以一次输入从 AFL queue 到再次反馈给 AFL 的完整生命周期为主线：

1. 编译器插桩和测试能力门禁；
2. master 建立 AFL concrete coverage 基线；
3. 调度器选择输入并分配给 concolic worker；
4. worker 产生 query、候选输入或 continuation；
5. QueryStore、frontier 和 coverage owner 接纳结果；
6. master 将最终新颖输入持久化并反馈 AFL；
7. benchmark 汇总吞吐、覆盖、超时和并行规模。

每一步分别检查了身份、所有权、超时、崩溃恢复、资源上限和统计口径。重点不是“正常路径可以运行”，而是以下交错是否仍正确：慢 worker 与租约回收并发、两个 master 同时 claim、多分片写入中途失败、queue 文件在 showmap 时被替换、MPI collective 不返回、实验超时以及 campaign 规模改变。

### 2.2 权威边界

| 数据 | 预过滤者 | 最终裁决者 | 本轮确认的不变量 |
| --- | --- | --- | --- |
| AFL 输入是否已建立基线 | `AflCoverageBridge` | master 上的稳定文件身份索引 | replay 失败或身份变化不能标记为已同步 |
| 符号候选是否新颖 | worker、本地 bitmap | master / coverage owner | 本地过滤只减流量，不能取代全局 claim |
| query 是否可提交 | solver worker | QueryStore lease token | 续租失败或 token 过期的结果不能完成 query |
| continuation 是否可发布 | live-state worker | persistent frontier token | 心跳必须覆盖 frontier `complete/abandon` |
| communicator 是否恢复 | survivor rank | ULFM attestation + agreement | 所有 collective 必须有可轮询的硬截止时间 |
| 并行规模是否可信 | 分析脚本 | 数据有效性门禁 | 无效拟合不产生模型型上限 |

## 3. 发现、错误机理与修复

### 3.1 P0：构建与测试门禁

**问题。** GCC clean build 在 `ContinuationLowering.cpp` 中因仅用于类型判断的未使用变量触发 `-Werror`；三个依赖 QSYM backend 的 lit 测试没有能力标签，simple backend 会把能力缺失误报为实现失败。

**修复。** 类型判断改为 `isa<LandingPadInst>`，不再绑定无用变量；`integer_min_max.ll`、`s2f_actionseed.c` 和 `self_config_native_provider.py` 增加 `REQUIRES: qsym`。测试矩阵现在区分“代码错误”和“backend 不提供该能力”。

### 3.2 P0：AFL 基线失败后的错误派发

**问题。** 新 queue entry 即使 concrete showmap 失败，也可能继续加入 worker 输入列表。此时 master bitmap 不包含该输入的真实覆盖，符号执行结果会相对一个不完整基线判断 novelty，造成重复任务和错误归因。

**修复。** `AflCoverageBridge.is_synchronized()` 同时核对路径和稳定 regular-file identity。新输入完成 `ingest` 后只保留精确同步的对象；失败对象不进入 `processed_files`，后续仍可重试。路径相同但 inode、大小、mtime 或 ctime 改变时视为新对象。

### 3.3 P0：QueryStore 慢求解越过租约

**问题。** 旧流程为 `claim -> solve -> complete`。若 Z3/portfolio 求解或结果检查时间超过 lease TTL，另一 worker 可以回收 query；旧 worker 随后仍会消耗资源并尝试提交。token fencing 能拒绝最终陈旧结果，但不能避免求解期间的重复占用，提交校验本身也没有续租保护。

**修复。** 新增 `QueryLeaseHeartbeat` 和 `solve_claimed()`：

1. 领取后按 `min(30 s, TTL/3)` 周期续租，最小周期 10 ms；
2. 心跳覆盖 solver 调用、结果验证和 `store.complete`；
3. 续租失败转为 `QueryLeaseHeartbeatError`，结果不再被当作成功；
4. `complete` 仍使用原 token 进行最终 fencing，心跳不是提交授权的替代物；
5. 普通 worker 和 malleable worker 共用同一路径，避免两套租约语义漂移。

回归测试使用 120 ms 租约，让 solver 和提交校验分别超过 TTL，并验证第二 worker 始终无法领取同一 query。

### 3.4 P0：ULFM shrink 无硬截止时间

**问题。** 阻塞 `Shrink()` 进入 MPI 后，Python 侧计时器无法使其安全返回，配置的 collective timeout 因而不是硬边界。故障场景下 survivor 可能永久停在恢复路径。

**修复。** `deadline_shrink()` 只接受可轮询的 `Ishrink`，attestation gather、membership agreement 和 survivor discovery 同样只使用可轮询请求；agreement 要求 `Iagree`，不再回退到无期限的阻塞 `Agree`。统一 `_wait_request` 使用 `Test()`、单调时钟截止和有界 sleep。未完成的 MPI request 不能安全 `Free()`：超时后 request 与 communicator 被隔离保留到进程退出，恢复立即以致命错误结束，不能在同一 communicator 上重试。只有请求已经完成时才释放。

该修复证明“调用可在期限内失败”，尚未代替真实多节点进程失效注入实验。

### 3.5 P0：多分片 coverage 永久部分提交

**问题。** 一个候选批次可能更新多个 coverage shard。逐 shard 原子替换只能保证每个文件完整；进程在第一个 shard 写完、第二个 shard 写入前崩溃时，其他 master 会看到永久部分提交。queue replay 最终可能收敛，但 candidate novelty 归因和即时全局视图并不原子。

**修复。** `CoverageOwnerShardGossip.claim_many()` 增加可恢复的前滚事务：

1. 为每个 batch 生成独立、内容校验的 `claim-transactions/<id>.json` WAL；
2. 只按编号获取本批次 shard 以及与遗留 WAL 相交的传递闭包锁；互不相交的 shard 集可并行提交；
3. 基于同一锁定快照计算每个候选的 novelty 和全部最终 shard record；
4. 将完整 records、候选 novelty 向量、coordinator 和 SHA-256 先持久发布；
5. 按 epoch 幂等前滚全部最终 shard record；已出现更高 epoch 时只允许其覆盖当前 WAL 的全部 bit，禁止回退；
6. 全部成功后耐久删除事务文件；新实例构造、下一次相交 claim 或 pull 都先恢复遗留事务。

恢复使用最终状态前滚，而不是尝试回滚已发布 shard；OR 单调性使重复写入安全。WAL、shard 和 coordinator heartbeat 均通过 `O_NOFOLLOW` 的稳定 regular-file snapshot 读取，校验读取前后 inode 身份、大小、重复 JSON 键和有限数；WAL 额外校验内容摘要。写端使用随机命名、`O_CREAT|O_EXCL|O_NOFOLLOW`、文件 `fsync`、同目录原子替换和目录 `fsync`，且写入上限与读端一致。空 WAL、超预算记录及符号链接失败关闭；事务目录以流式扫描同时限制有效 WAL 数和总目录项数，避免恢复前无界分配。并发反例用 barrier 证明两个 coordinator 的不相交 shard 写入可同时进入，而不再被全局锁串行化。

### 3.6 P1：删失数据被排除导致方向反转

**问题。** 仅对成功运行计算中位数和 A12，会把 timeout 较多的方案从样本中“删除”。例如处理组 5 次快速成功、15 次超时，基线 20 次稳定完成时，旧算法可能把处理组报告为显著更快。

**修复。** 所有具有有限指标值的运行都进入描述统计。`time_to_target` 使用右删失的 pairwise concordance：只有事件在对方删失边界之前发生时才能判胜；双方均删失或事件发生在对方观察窗之后时记为不可判定的半分。该指标强制采用 lower-is-better，命令行显式指定相反方向会直接报错。`time_to_target_censored` 不再影响 edge coverage、AUC 等无关指标。非成功运行仍按完成状态保守排序。存在删失或非成功运行时，结论标为 exploratory，不能进入 confirmatory；报告显式记录所用方法。

这是保守的完成度调整效应量，不是完整生存分析。时间到目标的正式论文实验仍应补充 restricted mean survival time、Kaplan-Meier 区间或与实验设计相符的分层检验。

### 3.7 P1：并行规模模型对坏数据仍给结论

**问题。** USL 在拟合比加权均值模型更差时仍输出参数；覆盖观测随 worker 增加而下降时仍强制拟合单调饱和曲线；正态近似区间对小重复数不稳；CSV 中 `np` 被当作 worker 数，忽略 master rank。

**修复。** 

- USL 和 coverage saturation 均预设 `R² >= 0.50` 决策门槛，低质量拟合不产生 ceiling；
- 两种模型都要求至少四个不同并行度。USL 有 scale/contention/coherency 三个参数，覆盖模型在固定基线后有 asymptotic gain/rate 两个参数；四级门禁避免三点 USL 和“基线 + 两点”覆盖曲线成为零残差自由度的插值；
- 并行度必须是正整数，吞吐、覆盖和权重必须有限且在声明边界内，模型搜索上限不能低于实测最大并行度；异常输入在聚合前失败关闭；
- 聚合覆盖非单调时不拟合饱和模型，图表明确写出 withheld 原因；
- round mean 使用固定种子的 5000 次 percentile bootstrap 95% 区间；
- benchmark CSV 新增 `num_masters`、`num_workers`、`afl_instances`，分析优先使用实测角色数；
- MPI 使用 concolic worker 作为模型横轴；hybrid 使用 `concolic_workers + afl_instances` 的总计算并行度拟合 shared-corpus throughput 和 coverage，同时分别以 concolic worker、AFL instance 拟合两个 component USL；
- AFL executions 与 concolic candidates 不是同一工作单位，hybrid 的旧合计 activity rate只为 schema 兼容保留，明确禁止拟合；
- 资源上限与模型横轴同量纲：MPI 扣除 master，hybrid 只扣除 coordinator；
- `combine_ceilings` 接受模型缺失，以资源上限作为明确的保守边界。

模型现在是决策辅助，不再是任何数据都能得到答案的公式生成器。USL 函数形式来自 Gunther 的 Universal Scalability Law；指数 coverage saturation 是本项目对有限预算 endpoint 的经验假设；`N*=min(N_resource,N_master,N_USL,N_novelty)` 是本项目的保守组合规则，不是已有论文定理。`R² >= 0.50` 和四级最小样本也是工程准入门槛，而非统计充分性的证明。正式规模决策仍应使用更多并行度、独立重复、参数 bootstrap 和样本外预测误差。

### 3.8 P1：自适应控制器混合量纲

**问题。** 旧 reward 同时包含边数、测试数和其他计数，再除以时间与 worker 幂。不同目标和 bitmap 规模下，某一大数值分量会支配控制器，A-B-A 比较不再表示同一目标函数。

**修复。** controller 只接收上游已归一化且有界的 `reward_sum`，优化量定义为 `bounded-hybrid-reward-per-wall-second`，snapshot 写入 `objective_kind`。控制器仍执行 baseline-trial-confirm switchback 和 cohort fencing，但不再自行拼接不可比单位。

### 3.9 P1：master 被 showmap 串行阻塞

**问题。** AFL queue 同步、density profile、TopSeed 和 K-Scheduler profile 都可能在 master 主循环同步启动 subprocess。worker 任务变短或并行度升高后，master service time 会直接限制派发吞吐，形成单点排队。

**修复。** 

- AFL coverage bridge 使用有界 `ThreadPoolExecutor` 在后台执行 showmap；只有 master 线程可以合并 bitmap、claim 全局覆盖和写 SQLite；
- density 每个任务使用独立临时目录，未完成时本轮退化为等宽分片；
- TopSeed 与 frontier profile 共享有界辅助 executor，cache miss 本轮返回确定性 fallback；
- 所有队列都有 job/pending 硬上限，关闭阶段显式 drain/cancel；
- 每个并发 showmap 使用唯一 bitmap 路径，避免临时输出互相覆盖。

后台线程只负责可丢弃的 concrete observation；所有权、coverage mutation 和调度状态仍由 master 单线程更新，因此没有引入新的共享状态竞态。

### 3.10 P1：AFL 去重集合无界与突发队列延迟

**问题。** 进程内 `_ingested` 随 campaign queue 增长，重启后又需要完整 replay；异步 pending 达到容量后，本轮未安排的 queue 后缀却可能被目录 generation cache 隐藏到定期 audit。

**修复。** 成功 replay 的文件身份与 AFL-only baseline bitmap 在同一个 SQLite WAL 事务中提交，`synchronous=FULL`。重启校验 baseline schema、map size 和 SHA-256 后先恢复位图，再恢复已 ingest 身份；旧索引没有 baseline 或 baseline 损坏时，清空身份并强制 replay，不能把空位图与“已同步”路径拼接。内存只保留 inflight 和有界 retry。异步队列容量饱和时立即清空已缓存的目录 generation，下一次 poll 即使 mtime 不变也会重新扫描未处理后缀，不等待 120 轮默认审计。

SQLite 将内存无界增长转为与 queue 规模成比例的持久索引。超长 campaign 后的索引压缩和已删除路径 GC 尚未自动化。

### 3.11 P1：continuation 发布窗口未受租约保护

**问题。** `resume_persistent()` 在本地 `resume` 返回后立即停止心跳，随后才执行 frontier `complete`。如果结果持久化、搜索策略合并或文件系统写入较慢，其他 worker 可以回收相同 checkpoint；旧结果最终可能被 fencing 拒绝，但重复执行已经发生。

**修复。** 心跳线程现在覆盖 `resume`、观测整理和 `_complete_persistent_claim`；只有 `complete` 或失败后的 `abandon` 返回后才停止。heartbeat sidecar 不推进 generation；completion 仍需处理其他 frontier 操作造成的 snapshot/expected-generation 冲突，并最终按 lease token 接纳。

回归测试使用事件同步，等待真实 heartbeat 已完成续租后才放行 frontier publication，避免依赖调度时序的 sleep 型脆弱断言；`complete` 仍保留对其他真实 frontier 更新所导致 generation 冲突的重试。

### 3.12 P0：符号执行产生的 crash/timeout 被静默丢弃

**问题。** worker 旧协议只返回有 coverage bitmap 的普通候选；当 SymCC/SymSan 产生的输入在 concrete verification 中 crash 或 timeout 时没有 bitmap，因而在 batch 组装阶段被过滤。系统只保留 AFL 自身终止输入，低估 concolic 路径的结果并丢失复现样本。

**修复。** worker-to-master candidate schema 增加严格枚举的 `terminal_status` 与有界 `terminal_detail`。没有预生成 map 的候选由 streaming showmap 复核；终止候选进入 master triage 的专用路径，原子写入 `crashes/` 或 `hangs/`，文件名携带完整 SHA-256，按 digest 跨 batch、跨重启去重。它们不进入普通 coverage claim，也不伪装成 queue novelty。统计新增 `generated_crashes` 和 `generated_hangs`。

### 3.13 P0/P1：租约边界与 heartbeat 热路径

**问题。** frontier 与 QueryStore 的 token 检查没有统一要求 `lease_until > commit_time`，恰好到期的旧 worker 仍可能提交；frontier 每次 heartbeat 又重写完整 JSON、递增全局 generation，使 worker 数增加后 lease 维护本身成为串行瓶颈。

**修复。** QueryStore 的 claim/renew/complete/fail 和 frontier 的 heartbeat/complete/abandon 全部采用严格半开区间：`expires <= now` 即失效。最终 SQL update 也携带 expiry 条件，覆盖校验期间跨过 TTL 的竞态。frontier 为每个 checkpoint 建立独立 heartbeat sidecar 与独立锁；心跳只读取和原子替换自身不超过 4 KiB 的带摘要记录，不获取全局 frontier lock、不重写全量状态、不增加 generation。续租必须同时匹配 checkpoint、token、owner、worker 和 claim generation，并禁止 expiry 回退；只持有同 token 但伪造其他 fence 字段不能续租。claim/complete/abandon 的锁序固定为 frontier lock 后 lease lock；恢复按 checkpoint 排序锁定并叠加 sidecar 权威 expiry。

后台 heartbeat 以 `max(10 ms, min(30 s, TTL/3))` 调度；原 50 ms 下限与允许的 100 ms 最小 TTL 只留一次续租机会，在高并发 lit 中可被调度延迟击穿。heartbeat 丢失后，旧 token 仍不能 `abandon`；执行器调用权威 `recover_expired()` 重排队，过期 worker 不写入包括失败统计在内的任何结果。

### 3.14 P1：SymSan 静默丢弃目标参数

**问题。** `SymSanEngine.wrap_run()` 只取 `target_cmd[0]`，driver 又固定构造 `[program, input]`，因此 benchmark 的模式参数、选项和值以及 `@@` 位置全部消失。目标可能执行另一条代码路径，但实验仍显示成功。

**修复。** 编排层对 `target_cmd[1:]` 逐参数替换 `@@`，使用 `fgtest target taint-input -- target-arg ...` 协议传递，不经过 shell 拼接，因此空参数和含空格参数保持不变。`fgtest` 与 `fgtest_rgd` 的增量补丁共同构造目标 argv；无额外参数时保留旧 `[program, input]` 契约。`build_symsan.sh` 幂等应用补丁，应用失败时终止构建，禁止回退到静默丢参的 driver。

### 3.15 P0：exact projection 自测试受生产超时影响

**问题。** `poly_exact_widening_renaming.c` 与 narrowing 对照要求 Z3 完成 exact integer projection，却沿用生产默认 10 ms 超时。在 192-way lit 中，widening 证明偶发返回 `unknown`，使测试退化到普通求解并错误失败；单项连续 100 次均通过，说明这是负载相关的测试契约缺失，不是可以忽略的“随机失败”。

**修复。** 两个 exact-projection 语义自测试显式设置允许范围内的 1000 ms 证明预算。该值只作用于自测试进程，不改变生产默认值，也不把 `unknown` 当作 SAT/UNSAT；测试现在验证算法语义，而不是同时隐式考察高并发下 10 ms 时限能否兑现。

## 4. 配置与可观测性

| 配置 | 默认值 | 作用 |
| --- | ---: | --- |
| `SYMCC_AFL_COVERAGE_JOBS` | 1 | AFL baseline 异步 showmap 并发数，范围 1..16 |
| `SYMCC_AFL_COVERAGE_PENDING` | 256 | AFL coverage inflight 上限 |
| `SYMCC_AFL_COVERAGE_RETRIES` | 65536 | 失败路径重试集合上限 |
| `SYMCC_AFL_COVERAGE_RESCAN_POLLS` | 120 | 目录无变化时的完整审计间隔；容量饱和会绕过该间隔 |
| `SYMCC_DENSITY_PROFILE_JOBS` | 1 | density profile 并发数，范围 1..8 |
| `SYMCC_DENSITY_PROFILE_PENDING` | 32 | density profile inflight 上限 |
| `SYMCC_AUX_SHOWMAP_JOBS` | 1 | TopSeed/frontier 共用 profile 并发数，范围 1..8 |
| `SYMCC_AUX_SHOWMAP_PENDING` | 64 | 辅助 profile inflight 总上限 |

`AflCoverageBridge.snapshot()` 新增 async job、pending、capacity skip、持久索引条目、恢复 baseline feature 数、索引失效次数和 retry 数；coverage owner 新增 `recovered_transactions`；MPI 汇总新增 concolic crash/hang；并行规模 JSON 输出各 allocation dimension、component fit、拟合失败原因和资源上限依据；controller snapshot 输出目标函数类型。

## 5. 验证结果

### 5.1 构建与 LLVM lit

- 现有 build 增量编译通过；
- 全新 GCC/simple-backend 目录使用 `-DZ3_TRUST_SYSTEM_VERSION=on` 编译通过；
- GCC/simple lit：344 项，249 passed、95 unsupported、0 failed；
- 最终 QSYM/Clang lit（192 workers）：345 项，344 passed、1 unsupported、0 failed，耗时 304.62 秒；
- exact projection widening 单项连续重放 100 次通过；修复后 widening/narrowing 以 2 workers 成对重放 50 轮、共 100 次通过。

`unsupported` 来自显式能力标签，不是静默跳过失败。

第一次最终 QSYM 门禁为 343 passed、1 unsupported、1 failed，唯一失败是负载相关的 `poly_exact_widening_renaming.c`。该结果没有被丢弃或解释为偶然波动；3.15 所述超时契约修正后，定向压力测试与第二次完整 192-worker 门禁均通过。

### 5.2 Python 回归

- 高风险 11 模块耦合回归：396 passed、129 subtests passed；
- 持久 continuation 专项：9 passed；
- AFL coverage 异步、持久索引和容量恢复专项：4 passed；
- 最终审计四模块定向回归：214 passed、26 subtests passed；
- 修复后 pytest node-id 清单：1576 个稳定测试身份，SHA-256 为 `f4fe4bbb81ca0dce757bc48a7d8188764647deb208d3156cbfd3734b60cf6006`；
- 最终完整严格门禁：1576 passed、581 subtests，耗时 346.01 秒；0 failed、0 collection error、0 skip、0 xfail、0 xpass、0 deselect、0 missing node ID、0 unexpected node ID；
- 门禁使用 Python 3.12.3、pytest 9.1.1、禁用第三方 plugin autoload，并以 `-W error` 将警告升级为失败；机器可读结果保存在仓库根目录 `python-test-gate.json`。

### 5.3 故障与反例覆盖

新增或强化的反例包括：

- solver 和提交校验分别超过 query TTL；
- `Ishrink` request 永不完成，以及 runtime 只有阻塞 `Shrink`；
- 多分片 transaction 在中间 shard 写入失败并由同进程/重启实例恢复；
- WAL/shard/heartbeat 符号链接、空或超预算 WAL、重复或非有限 JSON；
- 处理组多数 timeout、少数快速成功的删失反例；
- time-to-target 方向误配、三点欠定模型、零权重/NaN、USL 比均值模型差、覆盖随并行度非单调；
- showmap 被事件阻塞时 master poll 立即返回；
- queue burst 大于异步容量且目录 metadata 不再变化；
- continuation publication 超过 lease TTL。

## 6. 对架构的判断

### 6.1 当前合理的设计

- worker 负责昂贵计算，master 保留最终 coverage、任务和反馈裁决，权威边界清楚；
- CAS、稳定文件身份、generation 和 token fencing 已形成统一的结果身份体系；
- QueryStore、frontier、coverage owner 均具备可测试的恢复入口，而不是仅依赖进程内状态；
- SOTA 搜索和求解策略大多在可关闭、可退化的策略层，上层失败不应改变核心正确性；
- benchmark 已开始区分机制证据、端到端效果和模型推断。

### 6.2 仍然限制扩展性的结构

`mpi_fuzzing_helper.py` 同时承担 AFL 扫描、调度、coverage、TopSeed、agentic、query、profile 和 shutdown，主循环过大。即使 subprocess 已异步化，完成回调、SQLite commit、coverage transaction、结果 triage 和策略更新仍共享一个 master 事件循环。并行度继续增加后，系统上限近似受以下队列约束：

`lambda_worker_result + lambda_afl_entry < 1 / E[S_master]`

其中 `E[S_master]` 是一次准入、claim、持久化和调度反馈的平均 master service time。当前 telemetry 能测各阶段，但还没有用真实长时数据估计到达率分布和 P95/P99 service time，因此不能宣称已经消除 master 瓶颈。

## 7. 后续优化优先级

### P0：真实环境资格验证

1. 在至少两台主机上执行 ULFM 进程失效注入，验证 `Ishrink -> attestation -> Agree -> replay` 的端到端期限；单元 double 不能覆盖 MPI runtime 实现差异。
2. 对 NFS/并行文件系统执行多 master coverage transaction kill-point 测试，确认锁、rename、fsync 和恢复语义满足部署环境要求。
3. 用长时公开目标重跑 1/2/4/8/16 worker、多个 seed 的等 CPU 实验；本轮没有产生可用于汇报性能提升的新数据。

### P1：解除 coordinator 串行点

1. 对 shard-set WAL 协议做真实共享文件系统 kill-point 与高冲突压测；当前已解除不相交 shard 的全局串行，但重叠热点仍受单 shard 锁限制。
2. 把 master 拆为 admission、coverage commit、scheduler 三个有界 mailbox service；消息只传 immutable identity 和 delta，master 保留最终状态机。
3. 为 SQLite replay index/baseline 增加增量 GC、checkpoint 和磁盘预算；使用 queue 生命周期证明后再删除旧身份。
4. 记录 showmap queue wait、service time、capacity skip 和 rescan cost，依据观测自动调节 job 数，不以 worker 数直接等比例扩张 subprocess。

### P2：提高实验与控制器可信度

1. 时间到目标使用删失感知的 restricted mean survival time，并按 target/seed 分层 bootstrap；当前 completion-adjusted A12 仅为保守门禁。
2. USL 参数和 ceiling 使用按轮次 bootstrap 的分布，而非只对每个规模的均值给区间；至少五个规模点时再报告 held-out prediction error，四点只满足当前最低可识别门槛。
3. 自适应并行度使用排队等待、master utilization、重复率和单位 CPU coverage reward 的多目标约束，A-B-A 仍作为在线反事实校验。
4. LLM/agent 只提出策略或异常诊断，不直接裁决 coverage、SAT 或资源上限。优先使用它识别 telemetry 中的阶段变化，再由确定性 policy gate 和 shadow arm 验证。

## 8. 结论边界

本轮已经修复审查中可复现的高优先级错误，并把新增行为纳入稳定测试清单。可作出的结论是：失败输入不会再越过 AFL baseline 门且重启会恢复真实 baseline；query/continuation 租约覆盖结果提交；不相交 coverage shard 可并行、相交事务可前滚恢复；ULFM 恢复中的 shrink、gather、agree 均有可执行的期限契约；concolic 终止输入不再丢失；坏统计数据不再被强制解释为扩展性结论；SymSan 与 SymCC 使用相同目标 argv 语义；master 的主要 showmap 工作已移出调度线程。

不能作出的结论是：这些修改已经在 LAVA-M、Magma 或其他公共目标上带来某个百分比的覆盖或速度提升。热点 shard 锁可能限制极高多 master 写吞吐，异步 profile 的收益取决于目标执行时间，模型的有效性取决于足够长、足够多轮且 allocation 可辨识的实验。以上问题已列入 P0--P2 后续计划，需用真实环境证据继续关闭。
