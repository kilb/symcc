# F463：第三轮深度审查与失败原子性闭合

> 日期：2026-08-31  
> 范围：覆盖队列事务、QueryStore 结果 outbox、异步查询服务、AFL 覆盖基线、
> 实验统计与并行规模模型。  
> 证据边界：本轮是正确性和可恢复性审查，没有新增公开目标长时 campaign；因此不把
> 回归测试结果解释为覆盖率、缺陷发现率或端到端性能提升。

## 1. 审查目标与方法

F462 已闭合恢复基线、corpus 事务和持续 outbox，但“正常路径能完成”仍不足以证明并发
系统正确。本轮继续沿每个持久化决定点构造反例，重点检查以下问题：

1. 原子替换已经发生、随后目录 `fsync` 失败时，可见状态是否仍可恢复；
2. 多个 outbox janitor 同时失败时，尝试次数是否丢失；
3. 查询达到最大重试次数时，任务状态、结果和发布记录是否在同一事务提交；
4. 查询服务在启动、运行中退出和强制停止三个阶段是否会泄漏资源或留下求解器子进程；
5. AFL 基线重放失败后是否会被目录代次缓存长期推迟；
6. 并行规模模型是否会因有限搜索区间、零方差、浮点计数或统计口径给出伪上限。

审查采用“故障点 -> 可见状态 -> 恢复动作 -> 不变量”的顺序。每项修复都增加了可复现
反例；没有用扩大超时或降低断言来掩盖失败。

## 2. 发现与处置总览

| 类别 | 反例 | 原影响 | 修复后的不变量 |
| --- | --- | --- | --- |
| coverage transaction | manifest 已 rename，目录 fsync 抛错 | cleanup 只删 staging，可能留下指向缺失文件的 manifest | 异常路径先删可见 manifest，再删 staging 并同步目录 |
| outbox 并发 | 两个 janitor 读到同一 attempts 后同时失败 | 两次失败可能只增加一次计数，dead letter 被延迟 | 失败后在 `BEGIN IMMEDIATE` 内重读权威行并线性递增 |
| query terminal fail | 达到最大 solve attempts 后先提交、再调用 `complete()` | 两个事务间崩溃或 lease 到期可使任务再次被 claim | `done`、error result 和 publication outbox 同事务提交 |
| AFL baseline retry | showmap 失败且 queue 目录代次不变 | 重试依赖统计周期或周期性全目录 audit | 每个 coverage poll 先调度 retry set，再扫描新 queue 项 |
| query service config | AFL 配置无效发生在控制对象构造之后 | Master lock 和已构造资源可能未释放，且可错误返回成功 | 配置提前验证；失败统一停 worker、释放锁并返回失败 |
| query service runtime | 服务启动后退出 | 依赖仍向失效 QueryStore 提议，日志未及时关闭 | 主循环检测退出、关闭日志、禁用依赖并继续主 concolic 链 |
| process descendants | Master kill 只作用于服务 leader | 长求解器子进程可能成为孤儿 | 服务独立 session 启动，TERM/KILL 均作用于完整进程组 |
| AFL command parser | 引号参数、嵌入式 `@@`、空 target | 参数被错误拆分，placeholder 不替换或出现索引异常 | `shlex` 解析、任意参数内替换、空/无 target 明确拒绝 |
| scale ceiling | 将 `2n` 截断到搜索 horizon | 线性曲线也会在 horizon 附近被误判为饱和 | 只比较真实 `n -> 2n`；区间不能证明时返回 `None` |
| evidence statistics | 常量观测、ratio-of-means、`float` 计数 | 无定义 R² 被当作 1；不等时长产生偏差；大计数舍入 | 零方差拒绝；总计数比；CSV 计数使用精确十进制整数 |

## 3. 持久化与恢复协议

### 3.1 Coverage queue 的异常可见性

`prepare()` 的线性化点是 manifest 的原子替换，但原子替换与目录持久化不是同一件事。
旧异常处理假定 `_atomic_publish()` 抛错意味着目标不可见；实际上 `os.replace()` 成功后
的目录 `fsync` 仍可失败。此时 manifest 已进入 namespace，若只清理 staging，恢复器会
看到一个永久引用缺失阶段文件的事务。

修复后，`prepare()` 的异常路径按“manifest -> staging -> root fsync”顺序清理。故障注入
测试精确地让 manifest 目录同步失败，验证目录最终为空且 `recover()` 不产生伪工作。
这不承诺底层文件系统在掉电后提供超出 preflight 契约的语义，但保证所有 Python 可观测
异常路径不会主动制造悬挂 manifest。

### 3.2 QueryStore outbox 的并发尝试计数

文件发布是 SQLite 逻辑完成后的可重试副作用。两个 janitor 可以合法地同时选择一条尚未
发布的记录；若二者都基于旧 attempts 写回 `attempts + 1`，一次更新会覆盖另一次。修复
把失败登记放入独立的 `BEGIN IMMEDIATE` 临界区：锁内重新读取仍 active 的行，基于当前
值增加一次，并以 `published=0 AND dead_letter=0` 约束更新。若另一个执行者已经发布或
转入 dead letter，当前执行者不再覆盖终态。

并发 barrier 测试让两个线程在旧选择之后同时失败。包含初始发布失败在内，数据库最终
精确记录 3 次失败，并在配置上限 3 进入 dead letter；不再出现 lost update。

### 3.3 最大 solve attempts 的单事务终态

`claim()` 会递增 solve attempts。未达到上限时，`fail()` 仍把任务带 fence 地退回
`pending`；达到上限时，修复后的单个 SQLite 写事务同时完成：

```text
leased(query_id, owner, token, unexpired)
        |
        | BEGIN IMMEDIATE + 完整 lease fence
        v
done(query) + error(result) + pending(result_publication)
        |
        v
COMMIT  <- 唯一逻辑完成点
        |
        v
bounded outbox reconciliation
```

因此数据库中不会出现“已耗尽但仍 leased、没有结果”或“done 但没有 outbox”的中间提交。
即时文件发布即使失败，任务也不会重新进入求解；发布记录按既有退避策略恢复。`max_attempts`
同时改为严格正整数，拒绝 Boolean、浮点和零值，消除 Python `True == 1` 的隐式别名。

## 4. 查询服务与 AFL 覆盖闭环

### 4.1 查询服务生命周期

AFL 配置现在在 Master 共享文件系统 preflight 之后、所有持久控制对象构造之前验证。
配置失败会协作式停止 worker、关闭 Master queue lock，并无条件向调用者返回失败。

服务成功启动后，Master 每轮检查进程状态。运行中退出会关闭日志并将
`SemanticProposalGenerator.query_store` 置空，防止继续产生依赖已失效异步求解服务的
提议；已经落盘的 candidate spool 仍独立扫描，避免遗漏服务退出前刚发布的结果。

查询服务使用 `start_new_session=True`，其持久 solver、proof checker 和其它子进程继承
独立进程组。正常停止先向进程组发 TERM 并等待；超时后向同一组发 KILL。leader 意外退出
时也会尽力终止残留组。这样 Master 的清理边界覆盖完整 solver subtree，而不只覆盖 Python
服务进程。

### 4.2 AFL 配置与基线重试

`fuzzer_stats` 的 `command_line` 使用 `shlex.split()`，保留带空格的引号参数；`@@` 可以
位于完整参数或 `--input=@@` 等嵌入位置。缺失字段、空命令和 `--` 后无 target 均返回明确
配置错误，不再落入 `IndexError` 或执行错误目标。

`AflCoverageBridge` 的失败对象保存在有界 retry set。主循环现在每个 queue poll 都先调用
`schedule_failed()`，再调用 `schedule_queue()`。即使目录 device/inode/mtime/ctime 完全
不变，失败项也无需等待统计周期或第 N 次完整 audit。异步执行仍受统一 pending capacity
约束，完成结果只在 Master 线程合并到权威 coverage bitmap。

兼容 API `CoverageBitmap.init_from_afl()` 也在 `finally` 中关闭临时 bridge，确保其线程池和
SQLite index 连接不会因一次性初始化而泄漏。

## 5. 并行规模模型的证据修正

### 5.1 有限 horizon 不是饱和证据

旧实现以 `predict(min(maximum, 2*n))` 近似 doubling。当 `n` 接近 maximum 时，比较对象
被截断为 maximum；即使模型严格线性，增益也会人为变小并产生一个“上限”。现在只在
`2*n <= maximum` 时比较真正的 doubling。若观测 horizon 内没有证据，ceiling 为 `None`，
由资源上限或其它合格模型决定，而不是把搜索区间伪装成系统瓶颈。

coverage saturation 的 edge-gain ceiling 使用同一原则。模型仍要求至少四个并行层级、
单调 endpoint、有效 coverage universe 和最低拟合质量。

### 5.2 拒绝无定义与不一致统计

- 加权观测零方差时，R² 在数学上无定义；USL 和 coverage fit 现在明确拒绝，而不是返回 1；
- `minimum_r_squared`、doubling/edge threshold、resource ceiling 拒绝 Boolean、非数和非有限值；
- `unique > generated` 违反同一 measurement universe 的计数守恒，分析器立即拒绝；
- retention/acceptance 改为 `sum(unique) / sum(generated)`，不再计算不同墙钟时长下的
  “平均速率之比”；
- CSV 的角色数、执行数、候选数和 corpus 数用 `Decimal` 验证后转为精确 `uint64` 整数。
  测试覆盖 `9,007,199,254,740,993`，证明不会像 IEEE-754 double 那样舍入到相邻偶数；
  超大指数在整数物化前拒绝；
- 模型观测还会在网格搜索前验证转换和关键乘积处于有限浮点范围。`10**10000` 以及会让
  `weight * throughput^2` 溢出的输入稳定返回 `ValueError`，不进入 NaN 拟合。

这些修复提高的是结论可信度。它们可能使以前能够生成 point estimate 的数据被拒绝；这
是预期的 fail-closed 行为，不应描述为模型性能下降。

## 6. 验证范围与结果

本轮新增或加强的关键反例包括：

- manifest rename 后目录 fsync 失败；
- 两个 publication janitor 的并发失败计数；
- terminal query failure 不调用第二个 completion 事务；
- AFL retry 在 queue audit 之前恢复；
- 查询服务运行中退出、完整进程组 TERM/KILL 升级；
- 引号 argv、嵌入 placeholder、空 target 和伪 `command_line` 字段；
- 线性 USL 不因 horizon 产生上限、零方差拒绝、大整数计数精确保留；
- 不等墙钟观测使用计数加权 retention，并拒绝 `unique > generated`。

受影响模块的组合回归为 **157 passed + 78 subtests**；Ruff、`py_compile` 和
`git diff --check` 均通过。静止工作树下的最终全量 Python 门禁为 **1650 passed +
615 subtests**，耗时 **350.77 秒**，零 failure。此前一轮在测试执行期间修改文档，
`test_research_protocol` 正确报告 provenance digest 漂移；该用例在停止并发写入后单独通过，
最终冻结轮也完整通过，因此没有把环境扰动误记为产品缺陷。本轮未修改 C++/LLVM 代码，
F462 已通过的 LLVM 18/17 门禁不被重新解释为本轮新增证据。

`verify_delivery.py` 已确认本地 Markdown/入口链接、F463 技术编号、557 个已审计
`SYMCC_*` 名称、375 个顶层测试文件以及本轮更新文档的 SHA-256。总交付门禁仍报告
14 项历史冻结差异，与 F462 相同：F360/F361 的旧 CI/测试快照，F400、F403、F424、
F425、F426、F427、F429、F436、F437、F438、F456 的封存 source manifest，以及顶层
旧交付清单。它们用于证明各阶段当时的源码，不应以 2026-08-31 工作树回填覆盖。

## 7. 结论与剩余边界

本轮消除了六类会扭曲长跑结果的低频问题：可见 manifest 悬挂、outbox lost update、
最大尝试后重复求解、AFL 基线重试延迟、solver 子进程泄漏，以及规模 horizon/浮点口径
产生的伪结论。系统的关键状态转换现在有更清晰的线性化点，恢复动作也与该点一致。

仍需保持以下边界：

1. 当前结论是机制正确性，不是公开 benchmark 上的 coverage 或 speedup 结论；
2. 共享文件系统故障语义仍受启动 preflight 和实际挂载实现约束；
3. 单 Master 的最终 coverage/corpus 顺序提交仍可能限制极高并行规模；
4. outbox dead letter 需要诊断根因后显式 requeue，不能用无限重试替代运维；
5. 当前工作树包含历史未提交实现，干净 clone 的版本控制交付仍需维护者提交后复验。
