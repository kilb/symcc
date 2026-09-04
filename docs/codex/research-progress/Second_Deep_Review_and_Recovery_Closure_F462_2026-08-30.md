# F462：第二轮深度审查与恢复路径闭合

> 日期：2026-08-30  
> 范围：MPI hybrid worker/Master、AFL coverage 与 corpus 事务、QueryStore outbox、
> 持久 live-state frontier、覆盖率测量与并行规模证据。  
> 证据边界：本文记录实现、故障注入和回归结果；未新增公开目标长时 campaign，
> 因而不把机制改进表述为覆盖率、缺陷发现率或端到端加速结论。

## 1. 审查结论

F459--F461 已修复第一批持久化和控制面问题，但本轮按“重启、并发、预算耗尽、
编号耗尽、证据缺失”五类反例重新走查后，仍发现若干只会在长时间运行或故障路径
出现的问题。本轮没有增加新的路径启发式，而是把已有技术真正闭合为可恢复协议。

| 问题 | 影响 | 本轮处置 |
| --- | --- | --- |
| worker 在 showmap 前提交内容去重标记 | 后处理预算中断后，未验证候选会永久被当作重复 | 改为 tentative digest；完成覆盖判定后才提交 |
| worker 后处理无统一墙钟预算 | 大输出批次可长期占用 worker，降低并行有效吞吐 | hint、读取、batch/stream/one-shot showmap 共用 deadline |
| 多 Master 覆盖 claim 与 AFL queue 发布缺少本地崩溃事务 | claim 已持久而样本未发布时可失去恢复依据 | prepared/decided manifest、staging、幂等 commit/recover |
| QueryStore outbox 只在启动时有限重放 | 长时进程中的暂时发布失败可能一直积压 | 周期 janitor、退避、dead letter、显式 drain/requeue |
| `complete()` 把 outbox 故障重新解释为求解失败 | 数据库已 `done`，上层却计为 solver error | DB commit 成为权威；发布失败只排队重试 |
| resume 只恢复 AFL 覆盖，遗漏 SymCC 自身 queue | 重启后重复求解、重复发布、奖励虚高 | 启动时建立 AFL + 已提交 SymCC queue 的覆盖并集 |
| 固定六位 AFL ID 解析 | ID 达到 1,000,000 后截断并可能发生编号冲突 | 解析 1--10 位 `uint32`，queue/crash/hang 全部显式耗尽 |
| 快照与稀疏增量边界不完整 | 异常版本或重复稀疏行可泄漏异常/重复计数 | `uint64` 版本、bytes 载荷、精确 union 计数 |
| 查询服务在 Master 初始化中途启动 | 后续初始化异常可遗留子进程与日志句柄 | 完成所有控制面构造后，在主 `try/finally` 内启动 |
| 规模 CSV 缺少覆盖采样来源 | 抽样 endpoint 被误当完整 corpus 形成决策 | v5 provenance 门禁；抽样点仅描述，不生成规模建议 |

## 2. 修复后的执行次序

```text
AFL queue + 已提交 SymCC queue
        |
        v
启动重放并形成单调 coverage union
        |
        v
Master 发布 versioned coverage snapshot
        |
        +------------------------------+
        |                              |
        v                              v
worker 领取 fenced task          AFL 增量异步 replay
        |
        v
concolic 执行 -> 输出目录稳定准入
        |
        v
刷新最新 coverage snapshot
        |
        v
tentative 内容去重 -> deadline-bounded concrete showmap
        |
        v
只回传终态或 worker 视角有新 coverage 的候选
        |
        v
Master generation/admission gate
        |
        v
候选 staging + prepared manifest
        |
        v
全局有序 coverage claim_many
        |
        v
durable decided manifest -> winner 原子进入 gap-free AFL queue
        |
        v
bitmap journal/snapshot + scheduler/agent 确定性反馈
```

该顺序有三个不可交换的约束：候选字节必须先于全局 coverage 决定具备恢复依据；
worker 的跨任务去重只能在 concrete verification 完成后提交；调度与 agent 只能看到
Master 已裁决的 coverage delta，不能把 worker 的局部判断当作权威结果。

## 3. Worker 后处理与覆盖收敛

`SYMCC_WORKER_POSTPROCESS_BUDGET_SEC` 为一次 concolic 执行后的 hint 解析、候选读取、
batch showmap、streaming protocol 和 one-shot recovery 提供共同的绝对 deadline。默认
`0` 保持兼容，即不额外设置总预算；设置正值后，外部 showmap 的超时取其自身上限与
剩余预算的较小值。预算耗尽只停止尚未开始的后处理，不把这些候选写入 `worker_seen`，
下一次仍可重新分析。

Master 默认每 100 ms、且仅在 bitmap 变脏时，以临时文件加 `os.replace` 发布共享覆盖
快照。格式包含 8 字节 magic、`uint64` version、`uint64` payload length 和至多 64 MiB
的 bytes bitmap。worker 先读 24 字节头；version 未推进时不读取 payload。快照是性能
提示而不是唯一持久真相，损坏或陈旧快照只会被忽略，权威状态仍由 Master/coverage
owner 和可重放 queue 决定。

resume 时，`AflCoverageBridge` 不再只重放 fuzzer queue，还会重放恢复完成后的 SymCC
queue。SQLite 身份索引绑定 path、device、inode、size、mtime 和 ctime，bitmap baseline
另带 SHA-256；索引存在但 bitmap 缺失或损坏时，必须废弃索引并重新 concrete replay。

## 4. AFL queue 的崩溃事务

`CoverageQueueTransactionStore` 把多 Master 的一批候选分为三阶段：

1. `prepare`：候选写入 AFL 不可见的 staging 名，逐项记录内容 SHA-256 和来源 ID；
2. 全局 owner 按候选顺序执行 `claim_many`，得到每项真实新增量；
3. `decide` 持久化 claim 结果和首个 queue ID，`commit` 只提升 winner，并压缩为无空洞 ID。

若在 claim 返回后、`decide` 前失效，恢复器无法证明哪些候选赢得了外部持久 claim，
因此保守保留整批；这可能增加冗余，但不会丢掉与权威 coverage 对应的 corpus。若已
`decided`，恢复严格重放原决定。manifest、staging 和 queue namespace 都有目录 fsync；
单 Master lock 防止同一 SymCC queue 出现两个本地 writer。

AFL `id:` 现在按逗号前的 1--10 位十进制解析，范围固定为 `[0, 2^32-1]`。格式化仍用
最少六位补零，但自然允许七位及以上。queue、crash、hang 在没有可表示的下一个槽位时
明确终止提交，不能截断到六位或在重启后从零复用。

## 5. QueryStore outbox 的最终语义

逻辑结果与 `result_publications(published=0)` 在同一 SQLite `BEGIN IMMEDIATE` 事务提交。
该提交是查询完成的权威线性化点；result JSON、generator、candidate 和 runtime output
是可幂等重建的 outbox 副作用。

`complete()` 只给予即时发布最多 64 条、100 ms 的服务预算，并保持当前 query 在列表
首位。发布异常会记录 attempts、last_error、指数退避的 `next_retry`，但不会把已完成
求解改记为失败，也不会消耗新的 solve attempt。构造器执行有界启动对账；常驻服务
周期处理到期记录；`--once` 使用多轮 `drain_result_publications()` 排空调用时已经到期的
记录。达到最大尝试次数的记录进入 dead letter，运维可显式 `requeue`。

这修订了 F461 第 3.4 节的旧表述。旧文所称“即时发布失败必须向求解调用者报错”会
混淆逻辑事务与派生文件 I/O，已由本轮故障注入证明不符合 outbox 语义。

## 6. 控制面与实验可信度

异步查询服务的命令先完成构造，但进程只在 coverage、frontier、admission、scheduler
和所有清理对象就绪后启动，并位于 Master 主 `try/finally` 内。启动失败会关闭日志、
回收子进程，并禁用依赖 QueryStore query holes 的语义提议旁路；普通 concolic 主链
继续运行。正常退出对已经退出、可终止和必须 kill 的三种子进程状态均做有界回收。

并行规模分析 schema 升级为 `symcc-parallel-scale-analysis-v5`。每个 coverage row 必须
携带 denominator kind、measure status、是否抽样、实测/总 corpus 数；字段缺失、互相
矛盾或 endpoint corpus 被抽样都会撤销 `decision_eligible`。固定种子 1337 与排序输入
保证工程抽样可复现，但抽样只适合快速描述，不能用于正式 coverage ceiling。

## 7. 验证

本轮增加或加强了以下反例：

- 七位 AFL queue/source ID 往返、`uint32` queue/crash/hang 耗尽；
- coverage transaction 在 prepared、decided 和部分 rename 后恢复；
- outbox result/candidate 发布故障、restart defer、future-clock retry、dead letter 与 drain；
- 正时间预算至少允许一个原子发布，显式 query ID 也遵守 `next_retry`；
- SymCC 自身 queue 的 bitmap 纳入持久 baseline，重启不重新 showmap；
- 重复稀疏 coverage row 只计算 union 中的新 bit；
- 非法 coverage snapshot version/载荷被稳定拒绝；
- multiprocessing 测试改用 `spawn`，避免 mpi4py/Open MPI native progress thread 下
  `fork()` 的 Python 3.12 弃用告警。

最终验证结果以本文第 9 节和全量测试输出为准；两套 LLVM 门禁已经完成：LLVM 18
发现 348 项，347 passed、1 项 capability-labeled unsupported；LLVM 17 发现 348 项，
346 passed、2 项 capability-labeled unsupported。两者均为 0 failure。`build` 与
`build-llvm17` 增量编译均成功。项目自有 Python 源码 `compileall` 通过；
`benchmark/public` 中上游 Python 2 脚本不属于 Python 3 门禁。

## 8. 仍然存在的边界

1. 本轮没有新的公开目标长时、等 CPU、跨 seed 实验，不能据此给出 coverage 百分比。
2. `SYMCC_BATCH_VERIFY_NEW=0` 的默认值保留短 campaign 性能：batch `-I` 不提供逐输入
   crash/timeout 状态，终态完整分类需显式开启或由 AFL 后续执行识别。
3. coverage snapshot 是同共享文件系统上的易失性能提示，不替代多节点 owner WAL。
4. QueryStore dead letter 需要运维诊断根因后 requeue；自动无限重试会形成 I/O 热循环。
5. 当前工作树仍包含大量历史未提交修改。source-delivery gate 能准确报告差异，但在维护者
   审核并纳入 Git、从干净 recursive clone 复验之前，不能宣称版本控制交付已经闭合。
6. 单一 Master 仍保留最终 coverage/corpus/policy 提交权。这保证顺序语义，但在更高 worker
   数下仍可能成为服务台上限；后续应基于 queue wait 与 service-time profile 决定是否拆分。

## 9. 最终状态

本轮的核心成果不是增加一个独立算法名称，而是消除了“短测通过、重启或长跑重复”的
系统性来源：恢复后的 coverage 与 corpus 一致，求解完成与文件发布解耦，worker 后处理
受共同预算约束，AFL 编号在长期运行中保持可解析，查询服务生命周期进入统一清理边界，
实验分析对抽样 coverage 失败关闭。

最终冻结工作区后执行 `python -m pytest -q test`，结果为 **1638 passed + 609
subtests**，耗时 **332.78 秒**，无 failure。覆盖本轮关键耦合面的组合回归为 **466
passed + 172 subtests**，耗时 **107.50 秒**；Ruff、`compileall` 与 `git diff --check`
均通过。两套 LLVM 门禁结果见第 7 节，均无 failure。

`verify_delivery.py` 的本地链接、当前技术编号、557 个 `SYMCC_*` 配置名和 375 个顶层
测试文件检查通过；总门禁仍报告 14 项历史差异：F360/F361 的精确 CI/测试快照，
F400/F403/F424/F425/F426/F427/F429/F436/F437/F438/F456 的冻结 source manifest，
以及顶层旧交付清单。它们记录各自封存时刻，不应以 2026-08-30 的源码重写。当前工作树
能通过运行时与静态门禁，但版本控制交付仍须维护者审核、提交并在干净 recursive clone
中复验。
