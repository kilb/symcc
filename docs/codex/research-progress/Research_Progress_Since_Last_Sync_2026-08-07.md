# 自上次同步以来的研究进展

- 日期：2026-08-07
- 范围：自上次“并行混合符号执行框架、Backsolver/Veritesting、Prefix DAG、AFL data coverage、agentic concolic execution”同步之后新增的研究与工程进展
- 对应实现：F297-F319
- 证据口径：本文区分“机制已实现并通过回归”和“公开 benchmark 性能结论”。当前新增工作主要达到 I/T/E-mechanism，部分具备 B-mechanism 证据；除已单独说明的实验外，不把机制测试解释为通用覆盖率或求解速度提升。

## 1. 总体进展概述

上次同步时，项目已经完成了并行混合符号执行框架的主体设计：把路径、约束、输入结构、目标分支和执行状态统一抽象为可调度任务，并围绕 Backsolver、Veritesting、Prefix DAG、S2F action seed、polyhedral/Z3 cache、AFL data coverage、comparison taint、under-constrained execution 和 agentic concolic execution 建立了较完整的研究原型。

本阶段的工作重点从“能力扩展”进一步推进到“高可信闭环”和“边级高价值调度”。核心变化包括：

1. 将编译期语义、运行时求解、AFL 覆盖反馈、MPI 在线画像和 benchmark 归因放入同一条可审计证据链；
2. 把经验值域画像从离线辅助信息升级为在线、版本化、可撤销、可重放恢复的 MPI solver-feedback 闭环；
3. 构建 fail-closed 覆盖率归因 oracle，解决流式 showmap、输入 ABI、terminal status 和时序漂移导致的覆盖率误判；
4. 将 edge-dependence replay 从 seed 级重播升级为 branch-targeted directed utility replay，减少重复探索同一种子内低价值边的概率；
5. 持续补充测试、文档、机制图和 SHA-256 清单，使新增技术可解释、可复核、可用于后续汇报和论文材料。
6. 将未完成的target solve纳入统一调度状态，以有界group租约抑制跨lane、跨worker重复分派；
7. 将候选评分与全局准入分离，只有真正派发的任务才提交attempt、cooldown和游标，并在冲突后回填worker配额。
8. 将跨主target-group续期改为失败原子更新，并隔离单条共享租约I/O故障，避免控制面抖动终止整个coordinator。
9. 将MPI result owner从长期rank扩展为per-dispatch generation token，在任何状态消费前隔离迟到、未受托和畸形结果。
10. 将RESULT隔离扩展为generation-aware READY/RESULT join、精确代际补偿、有界重排队与重启可恢复的deferred journal，补齐异常协议的活性。
11. 为完全静默的ACTIVE派发增加generation-fenced watchdog，将语义任务迁移与worker endpoint恢复分离，避免超时后复用仍在运行的rank。
12. 以READY门控、tokenized STOP、cleanup ACK和有界Ibarrier闭合MPI生命周期，使parked或失联worker不能把关闭阶段重新变成无限等待点。
13. 将关闭协议抽取为两MPI前端共享模块，修复standalone READY/RESULT跨tag乱序、关闭期结果丢账、阻塞master stats与失真idle deadline。

截至本阶段，Codex 归档的新增能力从 F00-F296 扩展到 F00-F319，共 320 个功能 ID。Python 回归规模从 F297 阶段的 475 项增长到 F319 后的 605 项加 37 个subtest。LLVM lit 门禁从 F297 后的 LLVM18 210/210、LLVM17 209 passed + 1 unsupported，推进到 F307 的 LLVM18 221 passed + 1 unsupported、LLVM17 220 passed + 2 unsupported。F319 lifecycle/AFL编排为63 passed加13个subtest，七个相关模块为268 passed加17个subtest，并完成Open MPI 4.1.6真实单/双master健康路径、精确1秒idle deadline及静默stats peer Abort(70)故障注入。

## 2. 编译语义和端到端正确性进一步加固

本阶段首先补齐了普通 Symbolizer 对 LLVM 标量整数 min/max intrinsic 的精确 bit-vector 语义。F297 为 `llvm.smin/smax/umin/umax` 增加公共 runtime builder，使 signed/unsigned comparison 与 ITE 表达式能够在普通符号执行路径中保留，而不是在优化后退化为 concrete warning。该工作同时扩展了 static data-origin 追踪，遍历 numeric intrinsic 的全部参数，从而让后续 data coverage 和 solver telemetry 可以看到更完整的数据依赖。

这一改动的挑战在于：LLVM intrinsic 并不是普通分支，但其语义会影响路径条件、数据依赖和后续变换。实现必须同时兼顾不同 LLVM 版本、QSYM runtime、普通 Symbolizer 和 data-origin 统计。机制验证中，双 LLVM plugin/runtime 构建通过，定向测试实际求得目标输入；在 libarchive 7zip 对象相同 `-O3` 命令下，对应 concretize warning 从 10 条降为 0。该结果证明语义保留机制有效，但尚不声称通用性能提升。

F299 进一步进行了跨层正确性加固。过去单个模块“局部可运行”并不足以保证端到端实验可信：AFL 发布可能提前 claim，showmap 失败可能被解释成零覆盖，长前缀 UNSAT 可能被预算截断，LLVM 普通值可能缺少 `freeze` 语义，DPOR trace 可能被系统库内部同步噪声污染，benchmark 还可能混用不等价 CPU 单位。F299 将这些问题作为一个统一证据链修复：

- AFL peer 只发布完整、fsync 后的连续 ID，disk failure 不提前 claim；
- showmap oracle 失败必须显式恢复，不能静默变成“无新覆盖”；
- 严格 Z3 查询不再被短 cache budget 截断；
- LLVM 普通 symbolic operand 增加 `freeze` 保护；
- 静态依赖区间用 128-bit 中间算术，避免宽整数溢出污染依赖判断；
- DPOR 默认只记录主程序同步，显式 allowlist 才纳入启动时 DSO；
- benchmark 中 AFL-only 按等核实例启动，禁止跨 throughput kind 计算 speedup。

这部分工作的创新点不是单一算法，而是把符号执行系统中容易被忽略的“实验可信边界”前移到实现层。它使后续所有优化不再只依赖人工解释，而是通过 durable publish、oracle 恢复、静态算术边界和可复核测试共同约束。

## 3. AFL++ 原生协同从离线导入升级为在线 peer

F298 将 `symcc01/queue` 作为 AFL++ campaign 的原生 peer，而不是只在离线阶段把 SymCC 生成的输入批量导入。该机制维护 queue/hang/crash 独立连续 ID，使用 native cursor 同步 AFL foreign queue，并精确区分发布、扫描、导入和未保留输入。协调顺序采用 producer-before-consumer，避免 hard stop 时最后一批输入丢失或被错误归因。

该设计提升了并行混合执行框架的工程完整性：SymCC worker 生成的输入可以以 AFL++ 原生方式进入协同循环，同时仍保留来源 SHA-256、queue ID 和导入路径。75 秒真实运行中发布 23 个、扫描 22 个、精确导入 4 个，4/4 source SHA-256 一致；配套测试覆盖 orchestration、report、cursor、来源隔离和 CSV 持久化。

这项工作的研究价值在于解决 hybrid fuzzing 中常见的“生成了输入但无法可信归因到 AFL 覆盖变化”的问题。它为后续比较 AFL-only、SymCC-only 和 full hybrid 提供更清楚的数据流基础。

## 4. 经验值域画像闭环：从离线 profile 到在线自适应求解消费

本阶段最重的一组工作是 F300-F305：围绕 empirical value profiling 构建了完整的采集、消费、反馈、抑制、成本归因和恢复重放链路。

F300 修复 simple backend 的稀疏输入表达式问题：按最大 offset 扩张且包含空槽的 vector 改为按真实 offset 保存表达式的 sparse map，使首次访问 1 TiB 逻辑 offset 也只产生一个符号条目。随后引入 executable-bound empirical value profiling：每个 stable comparison/switch site 记录观察数和最多 8 个有限值，绑定目标可执行文件 SHA-256，避免不同构建或不同上下文的 site 被错误合并。

F301 把画像变成可被 solver 使用的严格 runtime sidecar。经验域不直接替代完整公式，而是作为 SAT-only 探针接入：只有在完整相关 prefix 和相反分支都由 Z3 返回 SAT，且模型通过表达式级复验后，候选才被接受；UNSAT、UNKNOWN 或验证失败都会删除经验域并回退完整公式。这样既利用了有限值域的速度潜力，又避免把经验域 UNSAT 当成真实不可达。

F302 将手工 sidecar 流程接入 MPI campaign。master 在有界滚动窗口中聚合 worker telemetry，发布内容寻址的语义代际；worker 仅在本地版本落后时接收 bytes，校验 SHA-256 后原子安装。当 profile 失去 limited 资格时，必须发布 `profile_count 0` tombstone，防止旧假设长期污染求解。真实 1-master/1-worker 运行产生 17 条记录、3 个语义代际、4 次 attempts、1 次 validated 和 3 次 fallback，最终完成撤销。

F303 继续区分“值域稳定”和“值域有收益”。QSYM 按 `(executable context, site, bits, exact values)` 发出 attempt、prefilter、query、SAT、validated、fallback 等守恒计数。默认至少 8 次真实 query 且 validated/query 低于 12.5% 时，只抑制该精确域；值集合变化不会继承旧失败，旧证据离开滚动窗口后自动重新准入。这实现了有界重新探索，而不是永久封杀一个站点。

F304 在 F303 的查询/收益门上加入真实 Z3 `check()` 累计成本。只有 query 数、validated 比例和 solver_time_us 同时满足阈值，才抑制精确域。这样廉价失败不会被过度优化，昂贵低收益域才成为优先削减对象。真实同源反馈中，2 query / 2 UNSAT / 142 us 在成本门 0 时被抑制，在 143 us 门下被保留，证明成本门能区分“失败很多但很便宜”和“失败且昂贵”的求解区域。

F305 解决在线状态恢复的可信性问题。过去 state 中的 artifact 标签与 runtime 一致，并不能证明滚动记录在当前策略下仍能导出同一个域。恢复流程现在从记录重新执行与 publish 相同的聚合、准入和物化，再比较完整 runtime 语义；畸形、不完整或不一致都 fail-open 并重建。checkpoint 写失败独立计数并保持 dirty，下一次 semantic no-op 仍会重试落盘。可执行证据证明系统能在 artifact 标签保持时检出 `[1] -> [2]` 记录漂移，并在一次 state 写失败后收敛。

这一组工作的创新性在于把传统 profile-guided solving 做成在线、有证据、有撤销、有成本、有恢复验证的闭环，而不是一次性离线 hint。其挑战集中在四点：跨 worker 版本一致性、经验域和完整公式之间的 soundness 边界、低收益域的自适应抑制、以及长时间 MPI campaign 重启后的状态可信恢复。

## 5. 覆盖率归因 oracle：从“测到覆盖”推进到“可信测量覆盖”

F306-F307 针对 benchmark 和覆盖归因进行了系统重构。此前，流式 `afl-showmap -S` 的异常、空 map、畸形 sparse map、时序漂移、输入 ABI 不一致和 terminal status 都可能被错误解释为“没有新覆盖”。这会直接污染 hybrid 方法最核心的指标：候选输入是否真正带来 AFL edge novelty。

F306 抽出受界流式 showmap 协议，保留 status/detail/raw status、完整 sparse edge、stdout/stderr 和兼容 `get_edges()`。输入大小、edge 数、辅助输出和 telemetry 均有硬上界；未知状态、保留位、短读、重复 edge、空 map 或畸形行都 fail closed。每个 campaign 首个输入同时走流式和隔离 one-shot，两者状态和完整 edge-ID 集合必须完全一致，否则整个 campaign 统一回退 one-shot。

F306 还引入交错复测：默认 3 轮、轮间隔 1 秒，每轮对 corpus 做循环位移；strict 模式遇到任一边漂移即拒绝，intersection 给出保守欠近似，union 只表示可能覆盖。真实 XML 机制 campaign 中，20 输入 × 3 轮共 60 次正式观测，另有 2 次兼容探针；探针发现 streaming `timeout/933` 与 one-shot `ok/1019` 不一致并触发 fallback；11 个输入出现漂移，46 个不稳定边事件。

F307 进一步修复输入 ABI 和 terminal status 分层。项目 XML harness 的 persistent/stdin 入口与 `argv[1]` 文件入口会产生不同边集，因此新增 `--input-mode auto|stdin|file`，把 stdin/file 路由写入 schema。新增 `--terminal-status-policy normal-only|stratified`：normal-only 下 crash、timeout、error 都 fail closed；stratified 下 terminal 输出只进入 terminal stratum，不参与 landing、novel union、exclusive edges 或 worker bitmap 合并。MPI batch/streaming triage 也只在 `ok` 且边集非空时更新 bitmap。

F307 的真实 evidence 使用 `input_mode=stdin` 和 `terminal_status_policy=normal-only`。streaming probe 得到 `timeout/933`，one-shot stdin 得到 `ok/935`，因此 oracle 统一 one-shot fallback。20 个唯一输入中 baseline 不计入 candidate，19 个 nominal candidate 全部有 seed-novel edge，novel union 为 1374；同时记录 20 个漂移输入和 57 个不稳定边事件。

这一阶段的先进性在于把覆盖率从“工具输出的数字”提升为受输入 ABI、terminal status、时序稳定性和同源观测约束的可复算指标。它不直接增加覆盖率，但显著提高了后续实验结论的可信度。

## 6. 边级 directed utility replay 调度

F308 将 edge-dependence replay 从 seed 级重播升级为 branch-targeted directed utility replay。此前 `EdgeDependenceCoverage` 只能把高价值种子重新放回队列，返回 `ReplayJob(path, 0)`；worker 不知道应该优先反转哪一个分支，导致同一种子中的多个已知边、已失败目标和低价值路径可能被重复求解。

新机制从 `branch_trace` 中提取 `site/taken`，哈希为 edge-dependence row，同时保存 trace 中的 opposite branch ID 作为 `target_branches` 队列。每个 row 持久化 `site_id`、目标游标、局部 corpus、co-occurrence cell、`reward_ema`、`cost_ema`、`terminal_failures`、`successful_targets` 和 `target_distance`。调度分数综合 under-explored 程度、结构 co-occurrence、静态 directed distance、收益/成本 EMA、显式目标存在性、成功目标数、冷却年龄和 terminal failure 惩罚。

该机制与 Prefix DAG、AFL bitmap 的边界保持清楚：F308 只决定下一次 replay 应指向哪个 branch target；候选输入能否进入普通 corpus，仍由 AFL edge/data coverage oracle 仲裁。也就是说，它是“目标选择和资源分配”的优化，不绕过真实重放和覆盖准入。

工程上，`AdaptiveHybridScheduler.observe()` 将 coverage delta、interesting case、elapsed 和 killed 传给 edge-dependence oracle；`replay_candidates()` 现在能返回 `ReplayJob(path, target_branch)`。snapshot 升级为 schema 13，旧 schema 1-12 继续可读；新增 `SYMCC_EDGE_DEP_DISTANCE`，没有该距离图时自动退化为普通 edge-dependence replay，有距离图时更靠近目标的 site 优先，但仍会受失败和成本反馈抑制。

F308 定向测试覆盖非零目标分支、静态距离优先、收益/成本/失败反馈恢复和 scheduler 级目标输出；`pytest test/test_hybrid_feedback.py -q` 为 45/45，完整 Python 回归为 542/542。该机制对齐 directed hybrid fuzzing、target-centric seed selection 和 selective concolic testing 的研究方向，把调度粒度从 seed 推进到 edge/branch target，是并行符号执行系统减少冗余求解的重要一步。

## 7. 在途目标租约与调度完整性

F309对F308进行深度状态审查，发现四类会破坏长期调度质量的问题：branch row裁剪后其他
source的`row_cells`没有随destination cell删除而减少；重复观察已有target会重排tuple并
破坏round-robin游标；terminal outcome在空trace时无法退休；CSTG在target已到达但有限
trace未包含它时反而记录divergence。上述问题均已用可复现测试固定并修复。

在正确性修复之上，F309借鉴并行MCTS/WU-UCT对unobserved work的显式统计原则，把尚未
返回的符号目标纳入下一次选择状态。Prefix DAG、CSTG、hierarchical concurrency、
edge-dependence和fallback replay共享target lease registry；primary target与非skip S2F
action形成原子group，worker结果释放整组，worker失联则由默认120秒的deadline恢复。
snapshot升级到schema 14并暴露inflight、reservation和suppression计数，旧schema继续可读，
active lease则不跨coordinator重启恢复。

该实现不等价于WU-UCT公式，也不继承其regret或加速结论。它只迁移“未完成工作不能对
调度器不可见”的设计原则，并将其适配到昂贵branch-target solve。完整状态机、不变量、
学术映射和实验边界见
[`F309研究报告`](In_Flight_Target_Leasing_F309_2026-08-07.md)。

## 8. 事务式目标准入与冲突回填

F310继续审查F309租约接入后的状态提交次序。旧实现中，Prefix DAG、CSTG和concurrency
在本地`select()`阶段先更新attempt、`last_scheduled`、queue epoch或target cursor，统一
registry随后才可能拒绝冲突group。这使没有进入worker队列的proposal也被记为已调度，
污染探索统计并触发虚假cooldown。edge lane也存在租约失败前递增row计数的同类问题。

新协议把调度拆成无副作用proposal、原子group admission和接纳后commit。三个lane新增
兼容的`commit=False`与`commit_jobs()`；scheduler只把lease成功集合交回对应lane提交。
Prefix/CSTG target lane继续交错以保持模型公平性，但以accepted数量计算配额，冲突后继续
扫描后备proposal。回归明确证明请求2个任务时，重复target 77被抑制后仍返回`[77,88]`，
且被拒绝的CSTG/concurrency记录不更新cooldown。

该设计借鉴WU-UCT未完成工作显式化和Sparrow late binding原则，但不是这些算法的复现。
其贡献是让`state == leased == dispatched`成为可测试的并行符号执行调度不变量。完整实现、
机制图和科研边界见
[`F310研究报告`](Transactional_Target_Admission_F310_2026-08-07.md)。

## 9. 跨协调器 target-group fencing

F311把F309/F310的单coordinator不变量扩展到multi-master部署。已有共享work lease包含seed、
focus、策略和continuation，因此不同seed求解同一target仍会产生不同work ID。新增target-only
共享表把primary和全部非`skip` S2F action组成重叠group；每个target映射到确定性shard，
claim按digest全序持有全部短update lock、先检查全组、再用同一opaque token发布记录。
heartbeat和release必须匹配current token，过期旧owner不能删除新租约。

scheduler将其作为第二级`external_admission`：本地reserve成功后申请共享group，两个层级都
成功才提交attempt/cooldown/cursor。外部冲突还推动selector将worker配额和proposal扫描深度
分离，在不改变Prefix高低队列配额的前提下继续回填。MPI侧管理pending/active heartbeat、
派发前token复验、exact-work冲突回滚、triage后释放和队列截断清理；已有targeted job的
target/actions被冻结为派发契约，agent hint不能绕过已取得的group。

这里的token是current-record compare-and-match，不是全局单调fencing number；target lease
只优化重复计算，结果正确性仍由exact-work `begin_commit()`控制。完整失败模型、机制图、
配置与实验边界见
[`F311研究报告`](Cross_Coordinator_Target_Fencing_F311_2026-08-07.md)。

## 10. 失败原子租约续期与故障隔离

F312继续审查F311的异常路径。旧group heartbeat逐成员更新时间，后续成员写失败时会形成
不同expiry point；共享存储`OSError`还会穿透MPI主循环。新实现先保存全组record快照，
只在副本写统一时间，任一成员失败则在全组锁内恢复已写成员。MPI把active work、active
target和pending target逐条续期，一条异常只进入`work/target ok/failed`计数，不阻止其他
租约；未完成原子replace的临时record也由`finally`清理。

该回滚是best effort，不是跨文件事务；持续存储故障仍可能留下由current token保护、TTL
回收的保守租约。完整实现、故障模型和实验计划见
[`F312研究报告`](Failure_Atomic_Lease_Heartbeat_F312_2026-08-07.md)。

## 11. 事务化 MPI 派发交接与结果提交

F313继续沿worker派发的完整生命周期审查F311/F312。master在`comm.send()`之前会依次占用
target、exact-work、state shard、self-config parameter、SMT algorithm stage、seed-worker
pairing和verified proposal。过去这些状态由分散的手工清理维护；send失败、共享work冲突、
stale fenced result或结果提交中断可能只释放其中一部分，留下无法学习、无法再调度或要等待
TTL的悬挂状态。CAS对象ID还可能先于内容送达写入worker缓存视图。

新协议把一次派发显式划分为PREPARING、ACTIVE、COMMITTING和COMMITTED。所有reservation
按获取顺序登记补偿，回滚时逆序执行；未发送路径回退虚假pull/attempt和stage游标，已发送但
结果失信路径保留真实派发计数、丢弃反馈。exact-work `begin_commit()`通过后事务才进入
COMMITTING，triage及work/target/state完成都走完后才清空补偿栈。任一补偿失败不阻塞其余
资源回收；非send准备异常也会扫描三张registry立即清理。

算法stage精确撤销同时恢复active sequence、prior统计、RNG与全局`sequence_number`，因此
连续未派发任务按LIFO回滚后能重放同一stage和同一token。对象缓存只在send返回后更新，避免
下一次派发错误省略真实内容。完整状态机、资源语义矩阵、send投递歧义和实验计划见
[`F313研究报告`](Transactional_MPI_Dispatch_Handoff_F313_2026-08-07.md)。该实现仍不是跨
文件和覆盖状态的分布式两阶段提交；真实多rank失联/EIO/kill实验尚未完成。

## 12. 重启安全的本地 lease fencing

F314继续检查F313补偿栈所调用的底层状态容器，发现默认单master路径仍允许同一活动work或
state ID被第二个worker覆盖；旧completion/rollback又没有owner门，会删除后来worker的状态。
此外，本地journal和state snapshot持久化的是`time.monotonic()`，该值跨系统重启没有共同
epoch；旧uptime大于新uptime时会出现负年龄，使unfinished task无法恢复。

修复后，work/state lease都实行单owner准入，错误worker的complete/abandon/discard无副作用。
MPI结果使用master活动state ID和source rank组成提交身份，worker报告不再覆盖master状态。
新record使用显式`clock=unix`；legacy无标记记录视为stale，zero-TTL resume无条件回收；
coordinator restart保留累计state统计但使旧rank所有权失效。协议图、迁移规则和边界见
[`F314研究报告`](Reboot_Safe_Local_Lease_Fencing_F314_2026-08-07.md)。

## 13. MPI 派发代次身份与结果隔离

F315验证了F314结尾提出的generation风险：rank只标识长期worker，不能区分同一rank上前后两次
任务。旧result handler在校验可选shared-work fence前就按rank弹出active状态；默认不启用共享
租约时，未受托或迟到结果可以进入当前任务的triage、coverage和学习反馈。

修复后，master以随机epoch、rank和单调sequence派生per-dispatch token，事务与`TAG_WORK`共同
持有。worker所有结果出口统一回显；master在任何状态消费前将结果分类为current、unowned、
missing、malformed或stale，只有current进入提交链，其余按原因隔离并计数。transport token不
参与语义work ID，故重试和恢复去重保持稳定。协议图、可信边界和故障实验计划见
[`F315研究报告`](Dispatch_Generation_Result_Fencing_F315_2026-08-07.md)。

## 14. Generation-aware READY 与持久协议恢复

F316审查F315的活性边界：missing/malformed current result虽然不会污染triage，却会永久保留
ACTIVE事务；旧READY又只标识rank，无法证明它属于当前代次。RESULT与READY使用不同MPI tag，
master还可能先观察后发送的READY，因此接收顺序不能作为提交或回滚依据。

worker现在在RESULT sender返回后保存权威token，并由下一条READY回显。master用
`_DispatchGenerationGate`独立记录同代READY和invalid RESULT，在排空两类消息后才sweep。
合法current result会清除invalid；stale result不影响当前代次；异常READY也不更新coverage/
profile版本或idle集合。只有`ready[rank]==T && invalid[rank]==T`时，系统才按rank与exact token
调用F313 post-send补偿、清除全部side table并立即保存state snapshot。

恢复任务使用不含transport token的稳定语义payload。进程内立即重试由
`SYMCC_DISPATCH_PROTOCOL_RETRIES`限制为0--16次，默认1；耗尽后写入独立append-only journal，
`SYMCC_RESUME=1`时zero-TTL回收。写盘失败则保留内存重排队，不能把I/O错误变成静默任务丢失。
完整协议、状态机图、验证和边界见
[`F316研究报告`](Generation_Aware_READY_Recovery_F316_2026-08-07.md)。

## 15. Generation-fenced dispatch watchdog

F317处理F316无法覆盖的完全静默worker。固定timeout只表示suspicion，不能证明rank已经死亡；
因此系统在deadline后迁移语义任务，但不直接复用原rank。事务只在TAG_WORK send返回后记录
monotonic时间，RESULT和F316 join优先排空，已有同代READY时不因跨tag可见性差异误回滚。

超时路径按exact `(rank, token)`执行F313补偿，并与invalid-result共享稳定语义ID、有界重试、
deferred JSONL和I/O失败内存fallback。原rank进入`_RetiredDispatchGate`，迟到RESULT继续被
generation fence拒绝，错误READY保持parked；只有retired exact-token READY证明旧worker循环
结束后才恢复idle。这使false suspicion影响重复CPU而不影响提交正确性。完整设计、MPI 5.0/
ULFM边界、协议图和实验计划见
[`F317研究报告`](Generation_Fenced_Dispatch_Watchdog_F317_2026-08-07.md)。

## 16. Acknowledged bounded MPI shutdown

F318审查了F317之后的进程生命周期：旧master在三条退出路径中仍向全部rank阻塞发送STOP，
随后所有进程无条件进入Barrier。parked、busy或失联rank未必已经posted receive；捕获MPI异常
不能给尚未返回的调用设置deadline，因此运行期活性修复会在关闭阶段失效。

新`_ShutdownGenerationGate`要求READY消息或权威idle记录先证明worker位于receive boundary，
再按随机epoch/rank派生token并用`isend`发送一次STOP。worker校验token，在profile、showmap、
live-state和临时目录清理完成后回exact ACK；source rank、载荷rank或token任一不一致都不能完成。
关闭循环同时有界排空RESULT，解除大结果发送反压。shutdown grace仍有pending时rank 0显式
Abort；全ACK后也只执行有deadline的`Ibarrier()+Request.Test()`，避免最后一个无限等待点。

除3项状态机/静默rank/barrier单测外，当前Open MPI 4.1.6上真实1 master + 2 worker完成2/2 ACK、
pending为空和有界barrier；完整helper早期失败入口正常退出；另一次注入让rank 2完全静默，
master在0.150064秒准确报告pending并Abort(70)，没有触发5秒外层timeout。这是协议与有界失败
机制证据，不是通用故障恢复性能结论。完整设计、协议图和fault-injection计划见
[`F318研究报告`](Acknowledged_Bounded_MPI_Shutdown_F318_2026-08-07.md)。

## 17. 共享 MPI lifecycle 与有序静止

F319审查发现standalone runner没有同步获得F318保护，且其master按不同tag
选择性接收READY和RESULT时，可以先观察下一轮READY并覆盖active rank记账，
随后旧RESULT又会错误清除新任务。新`_WorkerAvailabilityGate`将READY停泊到
active ownership由严格RESULT consumer退役之后，实现观察顺序无关的可用性join。
该consumer同时接入shutdown drain，使wall-time/signal关闭期晚到结果仍进入统计和hash队列。

两前端现共享`mpi_lifecycle.py`中的tag、token/ACK gate、有界shutdown和Ibarrier。
standalone的跨master统计另改为strict schema、random token、nonblocking send和exact ACK；
pending、MPI error或result callback error任一存在都不能报告clean。`--max-idle`从5秒轮次
近似改为monotonic deadline，真实复测的1秒配置均报1.000秒。

Open MPI单master + 2 workers完成2/2 ACK，双master + 6 workers完成各3/3 ACK和统计
确认，两者均exit 0。静默rank 2的stats故障注入使root在0.150484秒得到
`received=(1), pending=(2)`并Abort(70)，不是外层5秒timeout。双master运行也暴露
单seed形成2次analysis observation的全局work-routing效率缺口；该现象已记为下一轮
基线，不被解释为F319性能收益。完整协议、图与边界见
[`F319研究报告`](Shared_MPI_Lifecycle_and_Ordered_Quiescence_F319_2026-08-07.md)。

## 18. 工作量与验证规模

本阶段不仅实现算法，也持续补齐文档、图示、证据和回归门禁。主要工作量体现在：

- 新增或归档 F297-F319 共 23 个功能 ID；
- Python 回归从 F297 后的 475 项增长到 F319 后的 605 项加37 subtests；
- F306 后新增 QA3 覆盖归因测试 16 项、MPI 编排测试 26 项；
- F307 后 QA3 定向测试增至 19 项、MPI/AFL profile 编排测试增至 31 项；
- F308 后 `test_hybrid_feedback.py` 增至 45 项；
- F309 后 `test_hybrid_feedback.py` 增至 53 项，相关调度/编排组合为 89 passed 加 8 subtests；
- F310 后 `test_hybrid_feedback.py` 增至 56 项，相关调度/编排组合为 92 passed 加 8 subtests；
- F311 新增8项故障导向测试，组合为167 passed加12 subtests，完整Python增至561项；
- F312 新增3项故障注入测试，组合为170 passed加12 subtests，完整Python增至564项；
- F313 新增10项派发事务、状态守恒和故障补偿回归，组合为213 passed加12 subtests，完整Python增至574项；
- F314 新增6项单owner、错误owner、clock迁移和result identity回归，直接相关定向为123 passed加12 subtests，扩展相关组合为219 passed加12 subtests，完整Python增至580项；
- F315 新增4项派发代次、畸形结果、统一sender和quarantine统计回归，MPI编排为42 passed加8 subtests，相关组合为223 passed加12 subtests，完整Python增至584项；
- F316 新增6项跨tag乱序join、READY分类、代际精确回滚、稳定恢复payload、deferred journal重启和统计回归，MPI编排为48 passed加8 subtests，六模块为253 passed加12 subtests，完整Python增至590项；
- F317 新增3项deadline/config边界、parked-rank恢复和retry/defer/EIO不丢任务回归，MPI编排为51 passed加8 subtests，六模块为256 passed加12 subtests，完整Python增至593项；
- F318 新增3项READY/STOP/ACK状态机、静默rank有界退出和Ibarrier deadline回归，MPI编排为54 passed加8 subtests，六模块为259 passed加12 subtests，完整Python增至596项，并完成真实三进程健康协议、完整入口和静默rank Abort(70)测试；
- F319 新增9项共享实现、READY停泊、严格结果、晚到结果、假clean、master stats和idle deadline回归；lifecycle/AFL编排63 passed加13 subtests，七模块268 passed加17 subtests，完整Python增至605 passed加37 subtests；
- Codex 文档校验将覆盖本地链接、46 组 SVG/PNG 汇报图、F299-F319 专题示意图、320个连续功能 ID、397 个 `SYMCC_*` 配置名、245 个 `test/` 一级普通文件、PPTX 结构、证据目录 SHA-256 和 reviewed ZIP CRC；
- F302-F307 均保留机制 evidence、manifest 或复算器，F308-F319 增加调度/容错技术归档。

最近一次完整验证结果：

| 验证项 | 结果 |
| --- | --- |
| `ruff check`（F319实现与测试） | 通过 |
| `pytest` F319 七个相关模块 | 268 passed，17 subtests passed（13.22 秒） |
| `pytest -q test` | 605 passed，37 subtests passed（83.07 秒） |
| Open MPI 4.1.6真实单/双master与stats故障注入 | 单master 2/2 ACK、双master各3/3 ACK且exit 0；1秒idle均报1.000秒；静默stats peer在0.150484秒pending并Abort(70)，外层未超时 |
| `python3 docs/codex/verify_delivery.py` | 全部通过 |
| `git diff --check -- util/mpi_fuzzing_helper.py test/test_afl_profile_orchestration.py docs/Configuration.txt docs/codex` | 通过 |

LLVM lit 最近的完整门禁来自 F307：LLVM 18 为 221 passed + 1 unsupported，LLVM 17 为 220 passed + 2 unsupported。F308-F319 修改集中在 Python 调度器、分布式状态、MPI编排、测试和文档，未新增 LLVM pass，因此本轮未重复运行 LLVM lit。

## 19. 先进性、创新性与挑战性总结

本阶段的先进性主要体现在三个层面。

第一，系统不只追求更多求解技巧，而是把“正确测量”和“可信接纳”作为研究对象。F306/F307 的 fail-closed oracle、输入 ABI 显式化和 terminal status 分层，使 coverage 结论不再依赖脆弱脚本假设。这对并行混合符号执行尤其关键，因为多 worker、多路径、多状态下的测量误差会被快速放大。

第二，solver feedback 从静态 hint 发展为在线自适应闭环。F300-F305 把经验值域画像做成采集、严格消费、跨 worker 发布、低收益抑制、成本感知准入和恢复重放的完整协议。它允许系统积极尝试经验域优化，同时把不完备性约束在 SAT-only 探针和完整公式回退之后。

第三，调度粒度从 seed 推进到 branch/edge target。F308 的 directed utility replay 直接回应并行符号执行中的冗余问题：并行 worker 不应只知道“哪个输入值得重跑”，还应知道“这个输入上哪个目标分支值得尝试”。这为后续多节点环境中的任务切分、目标导向探索和 solver 预算分配提供了更细粒度的控制面。

第四，F309把调度反馈从“已完成结果”扩展到“正在进行的昂贵求解”。在途group租约使多个
异构候选lane共享最小而明确的并发状态，解决了并行扩展时常见的重复target solve；同时以
deadline、结果释放和restart fail-open约束失联风险。这是面向并行符号执行调度语义的系统创新，
但其收益仍需实验而不是由机制测试推断。

第五，F310把租约从“冲突过滤器”提升为调度状态的提交边界。无副作用proposal避免回滚
异构lane状态，接纳后commit保证学习统计只包含真实执行，冲突回填则避免去重机制降低
worker利用率。这一改动同时处理正确性和并行效率，但仍只把机制结果归为I/T。

第六，F311把在途target从进程内启发式状态提升为跨coordinator共享资源，同时没有把exact
work和target identity错误合并。重叠S2F action group的有序全组claim、current-token释放、
派发契约及proposal深度回填共同形成多主控制面。共享文件表没有共识层，故文档明确保留
时钟偏差、误过期和部分发布的边界，不把工程原型描述为通用分布式锁服务。

第七，F312把续期失败从未建模异常提升为协议状态：group成员共享成功或回滚的expiry point，
独立lease之间隔离I/O故障，并让未发布持久化制品有确定清理路径。这提高了多主控制面的可恢复
性，但真实共享文件系统的故障实验仍是其从I/T走向E-mechanism的必要条件。

第八，F313把一次MPI派发从“发消息并维护若干旁路map”提升为显式的资源交接协议。它针对
不同资源区分未发送撤销与已执行丢弃，保留真实计算成本但拒绝失信反馈，并把fence接受和triage
完成设置为两个独立提交门。这一设计提高了异常路径的可解释性与可恢复性；挑战在于MPI send
投递歧义、跨介质副作用无法由进程内补偿完全线性化，因此文档没有把它描述为通用分布式事务。

第九，F314把“本地状态不需要fencing”的隐含假设改为可验证协议。即使只有一个master，
并行worker、过期恢复和晚到消息也会形成owner竞争；持久化clock还必须明确boot epoch边界。
单owner准入、master权威identity与clock-domain迁移共同填补了从内存调度到长期恢复之间的
正确性空白。当前机制利用一rank一活动事务约束，未来pipeline需要generation token扩展。

第十，F315把该generation token扩展从计划变成强制协议。任务语义身份、worker身份和派发代次
被明确分离；任何结果只有在与ACTIVE transaction token一致时才可消费状态。统一sender与
reasoned quarantine使这一不变量同时覆盖成功、失败和观测面。它提高的是反馈闭环可信度，
仍需真实故障campaign测量恢复代价与冗余CPU。

第十一，F316把fail-closed结果门扩展为顺序无关且不丢任务的恢复协议。它不假设不同MPI tag
的观察顺序，用同代READY和invalid RESULT的join建立恢复证据，再把补偿、稳定语义ID、有界
重试与append-only重启日志连接成闭环。挑战在于既不能让畸形消息释放错误代次，也不能因
保守隔离永久占用worker和lease；当前机制解决了可观察完成场景，worker无任何完成消息时仍需
failure detector或超时协议。

第十二，F317把“timeout后重试”提升为具备代际安全边界的worker隔离协议。任务权利可以在
exact-token补偿后转移，但endpoint资格必须由retired exact READY单独恢复；这避免把慢worker
误判直接转化为同rank双重执行和状态混淆。当前实现保守依赖固定deadline，不冒充ULFM或可靠
failure detector，真正rank死亡仍需要communicator repair与外部编排。

第十三，F318把容错边界从任务生命周期延伸到进程生命周期。READY证明接收能力、STOP携带关闭
代次、ACK证明本地清理完成，且shutdown和collective exit分别有独立deadline；因此框架不会在
结束阶段重新引入运行期已经消除的无界阻塞。显式Abort选择有界失败而不是伪装成功，但它不是
ULFM repair，真实故障注入仍是下一阶段的关键挑战。

第十四，F319将“关闭可达”扩展为“顺序无关且统计守恒的全作业静止”。
READY与RESULT的跨tag观察不再依赖时序巧合；worker cleanup、master stats和全局集合各有
独立的确认与deadline；两个运行入口又共享一份协议实现。挑战在于生命周期
正确性必须与应用统计一起收敛，不能仅以进程退出码代替数据守恒。真实双master
复测进一步暴露全局工作复制，因而下一阶段必须把work ownership与termination
detection联合设计，而不能只增加worker数。

挑战性主要来自跨层一致性。编译语义、runtime telemetry、solver sidecar、MPI state、AFL bitmap、showmap oracle 和 benchmark report 都可能在不同边界失真。本阶段大量工作不是单点功能，而是在这些边界之间建立可恢复、可验证、可解释的协议。例如：经验域 UNSAT 不能污染完整约束；streaming showmap 不能替代不同输入 ABI 的 one-shot；terminal crash/timeout 不能混入 normal coverage；edge-target replay 不能绕过 AFL corpus 仲裁。

## 20. 下一步研究方向

下一阶段建议围绕“从机制正确到实验结论”推进：

1. 对 F308-F319 做等 CPU、多轮、固定随机种子的消融与故障注入实验，报告 redundant target solves、wrong-owner rejection、reboot recovery、shared claim/conflict/expiry、heartbeat failures、dispatch rollback/discard、result/READY quarantine、watchdog false suspicion/recovery、protocol requeue/defer、shutdown exact-ACK/pending/abort、master stats convergence、proposal acceptance、batch fill ratio、coverage AUC、time-to-target、solver cost 和 terminal failure 率；
2. 将 F306/F307 oracle 固化为所有公开 benchmark 的默认测量入口，避免不同实验脚本再次产生不可比结果；
3. 对 F300-F305 的经验值域闭环做目标级 ablation，区分 collection、SAT-only consumption、outcome admission、cost-aware admission 和 replay recovery 的边际贡献；
4. 在多节点 MPI 环境中评估 branch-targeted replay 与 Prefix DAG、S2F action seed、structural task allocator 的协同效果；
5. 继续保留“机制已实现”和“性能已证明”的边界，只在完成等 CPU、多轮统计和消融后再宣称覆盖率或速度提升。
