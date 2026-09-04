# F460：深度审查问题修复与并行控制面收敛

> 日期：2026-08-28  
> 对应审查：F459 代码、架构与算法深度审查  
> 范围：QueryStore、全局 coverage authority、live-state frontier、规模决策模型、Hybrid Master 热路径和源码交付门禁。

## 1. 本轮结论

本轮没有继续叠加路径启发式，而是优先闭合会影响结果正确性、实验可信度和并行扩展性的基础边界。F459 中可在当前工作树内处理的问题均已完成实现级修复；“当前工作树未被 Git 完整跟踪”已增加机器可执行的交付门禁，但仍需由仓库维护者审核并正式提交清单中的源码、测试和 manifest，才算完成版本控制层面的收尾。

| F459 问题 | 修复状态 | 核心结果 |
| --- | --- | --- |
| QueryStore 提交后制品丢失窗口 | 已修复 | SQLite outbox 与启动重放闭合 DB、result、generator、candidate |
| 非法 coverage delta 产生虚假覆盖 | 已修复 | authority 层失败关闭，整批拒绝负值、越界、重复和类型混淆 |
| coverage 分片锁放大 | 已修复 | 每批一次 liveness、pull 分批锁、WAL 常态预解析移出锁区 |
| frontier 租约字段校验不一致 | 已修复 | heartbeat、complete、abandon 复用完整身份与 expiry 单调性规则 |
| 规模模型幸存者偏差与虚构角色 | 已修复 | 全状态计数、角色守恒、证据门槛、bootstrap ceiling 区间 |
| Master 单批无界接收与长期目录扫描 | 已修复 | 有界 admission 服务、并行纯校验、终止样本 digest 增量缓存 |
| clean checkout 无法重现当前源码 | 门禁已实现，Git 纳入待完成 | 709 个普通文件和 1 个 submodule gitlink 已封存；当前准确报告 618 个未跟踪成员和 20 个脏成员 |

## 2. QueryStore：从“数据库完成”改为“可恢复发布”

### 2.1 原故障窗口

旧流程先把 query 和 subsumed query 在 SQLite 中置为 `done`，提交后才写 result JSON、generator 和 SAT candidate。若进程在提交后、文件发布前失效，query 已不可重新领取，fuzzing 侧却得不到候选输入。

### 2.2 新事务协议

新增 `result_publications` outbox。主 query、被 subsume 的 query、逻辑 result 和待发布记录在同一 SQLite 事务中提交：

```text
lease/current fence
  -> BEGIN IMMEDIATE
  -> 写 results + query=done
  -> 写 result_publications(published=0)
  -> COMMIT
  -> 幂等发布 result/generator/candidate
  -> published=1
```

构造 `QueryStore` 时自动执行 `reconcile_result_publications()`。发布器从数据库中的规范 JSON 重建文件，使用原子替换，只有所有副作用成功后才标记完成；失败次数和最近错误保留在 outbox。SAT candidate 的集中物化也可以在重启后补做。

原子发布不只对临时文件执行 `fsync`，还在 `replace` 后同步父目录，使新目录项在掉电边界上可恢复。result、generator、主 candidate、partial candidate 和 replay manifest 均使用 exact publication：若内容寻址路径已经存在，会先稳定读取并比较完整字节；内容不一致时原子纠正，不能因“文件名存在”而把损坏制品误认为已发布。

### 2.3 可观测性与验证

`stats()` 新增 pending、completed 和 retry 计数。测试分别在 result 文件写入和 candidate `.bin` 写入处注入故障，确认：数据库保持 `done`、publication 保持 pending、query 不会被重复领取；重启后制品自动恢复且 pending 清零。

## 3. Coverage authority：严格契约与锁区缩短

### 3.1 严格 delta 准入

公共 authority 不再使用 `int(bits) & 0xff`。当前规则为：

- index 和 bits 必须是精确 `int`，拒绝 `bool`、字符串和隐式转换；
- `0 <= index < 2^23`，`1 <= bits <= 255`；
- 单个 delta 最多 `2^20` 个稀疏 edge，index 必须唯一；
- 任一行非法则拒绝整个 delta，不允许部分 claim。

因此 `[(5, -1)]`、重复 index、mixed good/bad batch 和超大 index 都不能改变全局 bitmap。

### 3.2 锁复杂度变化

原 `claim_many` 对每个变化 shard 重新扫描所有 coordinator heartbeat，锁内元数据成本近似 `O(S*C + WAL)`。现在每批只构建一次 live-coordinator snapshot，再对各 shard 做纯映射，常态部分变为 `O(C + S)`。

`pull()` 不再同时持有全部请求 shard lock，而是按最多 8 个 shard 分批。恢复路径先在锁外枚举并解析 pending WAL；进入锁区后核对路径集合，只有集合发生并发变化才重新解析。新增 `recovery_scans_outside_lock` 和 `recovery_rescans_under_lock` 用于确认快路径是否成立。必须注意：若恢复中的事务跨越额外 shard，为保证事务闭包，恢复路径仍会锁住该事务触及的完整 shard 集；“最多 8 个”是无 pending WAL 的常态 pull 批次边界，不是恢复路径的绝对上限。

公共资源边界也已失败关闭：shard 和 coordinator 数各不超过 4096，单次 `claim_many` 不超过 4096 个 candidate，环境变量不能绕过这些上限。`pull()` 对迭代器逐项读取，最多消费 `shard_count + 1` 项；因此超长或无限生成器会有界失败，不再因先执行 `list(shards)` 耗尽内存。

该优化保留故障恢复的二次校验，没有把“锁外预读”误当成权威数据。

## 4. Live-state frontier：统一完整 lease fence

新增两条共享规则：

1. 不可变身份相等：`checkpoint_id/token/owner/worker/claim_generation` 全部一致；
2. 权威 sidecar 的 expiry 不得小于调用者所见 expiry，且必须仍未过期。

`heartbeat`、`complete` 和 `abandon` 现在使用同一规则。frontier 主快照可保留最初 expiry，sidecar 可通过 heartbeat 单调续期；因此旧 lease 对象仍可合法完成，但伪造 owner、worker、generation 或超前 expiry 会返回 stale。新增测试证明所有伪造字段均不改变 frontier，原租约随后仍可正常完成或放弃。

## 5. 并行规模模型：从曲线拟合改为证据受限决策

### 5.1 不再丢弃失败运行

分析器读取 `success/failed/timeout` 全部记录，并按 allocation 输出 attempted、successful、failed、timed-out 和 success rate。默认 `--minimum-success-rate=1.0`；低于门槛时，USL、coverage saturation 和 component fit 全部停发，不能从幸存样本外推。

反例由 15 次成功和 30 次 timeout 组成。新结果保留 45 次 attempted、成功率 1/3，主模型为 `null`，recommended ceiling 为 `null`。

### 5.2 角色账本与并行坐标

- MPI 必须满足 `workers + masters == np`，两者均至少为 1；
- Hybrid 必须满足 `workers + masters + afl_instances == np`，三类角色均至少为 1；
- Hybrid 的模型横轴仍为 concolic workers 与 AFL instances 之和，coordinator 不计入 compute axis；
- 缺失角色可以按明确规则推导，但任何角色推导都会使结果只能作为描述性拟合，不能形成扩容建议。

成功运行的 `wall_time`、generated、unique、edges found 和 edges total 现在都是必填测量值，不能再用零值静默填补。离散计数必须是非负整数，coverage universe 必须为正，始终满足 `edges_found <= edges_total`；提供 seed baseline 时还必须满足 `seed_edges <= edges_found`。Hybrid 还必须满足 `generated = afl_executions + symcc_generated`，使总吞吐和组件吞吐属于同一测量空间。

每个 attempted run 必须有跨文件唯一的稳定 `run_id`；旧数据若缺失 ID，只能得到可追踪的推导 ID，并撤销决策资格。分析器要求所有输入使用一致的 seed baseline 和 coverage universe，拒绝把不同插桩空间的 edge 数拼成一条扩展曲线。命令行 seed/total 参数只能补充 CSV 缺失值；若与 CSV 实测值冲突则直接报错，不能静默覆盖证据。

### 5.3 决策门槛与不确定性

默认要求至少 5 个并行规模档位、每档至少 3 次成功运行。满足门槛后，分析器在每个 allocation 内分层重采样 200 次，输出 USL 参数、coverage saturation 参数和 recommended ceiling 的 95% bootstrap 区间。少于 80% 的重采样能形成有效模型时，决策资格被撤销。

四点或单轮数据仍可显示描述性曲线，但 JSON 中 `decision_eligible=false`，并把探索性点估计与正式 recommendation 分开。这样既保留已有数据的可读性，也避免最低可识别拟合被当作稳定扩容结论。

## 6. Hybrid Master：有界 admission 与长期热点消除

### 6.1 结果入口服务

新增 `_HybridResultAdmissionService`，职责仅限纯 worker-result 协议校验：

```text
MPI 主线程接收（最多 capacity 条）
  -> generation/token gate
  -> bounded admission workers 并行校验
  -> 按接收顺序返回 valid/error
  -> 主线程执行 lease、coverage、corpus 和 policy 的唯一提交
```

MPI API、租约状态和 bitmap 不进入验证线程，避免依赖 `MPI_THREAD_MULTIPLE`，也保持权威提交顺序不变。默认并发度根据 worker 数取 1--4，最大可配置 8；默认 batch capacity 至少 16，参数分别为：

- `SYMCC_MASTER_ADMISSION_JOBS`
- `SYMCC_MASTER_ADMISSION_CAPACITY`

服务输出 batches、submitted、invalid、maximum batch 和 validation seconds。这里的并发只针对纯校验，不代表已证明端到端 speedup。

### 6.2 终止样本去重缓存

旧 `_batch_triage` 每批都 `listdir(crashes)` 和 `listdir(hangs)` 重建 digest 集，长时复杂度会随历史终止样本数持续上升。现在 Master 启动或 resume 时扫描一次，运行中在成功发布后增量更新两个集合。回归测试连续处理重复终止样本，并禁止 `os.listdir`，确认只保留一个制品且 ID 不跳增。

## 7. 源码交付门禁

新增 `util/source_delivery_gate.py` 和根目录 `source-delivery-manifest.json`。v2 清单覆盖 compiler、util、test、scripts、benchmark 顶层可执行脚本、QA3、harness、全部 CI workflow、根构建入口和 `.gitmodules`，并记录每个普通文件的 byte size 与 SHA-256。文件类型不只包含 C/C++、LLVM IR、Python 和 shell，还包含 Rust/Cargo、CMake、compiler 模板、Lit 配置、YAML oracle、`.test32`、生成式 include、CNF fixture 和构建补丁。

早期策略只按少量后缀发现文件，会漏掉上述 46 个构建或测试成员，并把不存在的 `Dockerfile.simple` 静默跳过。当前策略要求所有声明的根文件和递归根真实存在，`--write` 也不能通过重写 manifest 把缺失入口移出清单。它还从 Git index 读取 mode `160000` 的 gitlink，要求已检出的 submodule `HEAD` 与 index object 完全一致；因此主仓库文件不变但 runtime 漂移，也会失败关闭。当前清单为：

```text
schema = symcc-source-delivery-manifest-v2
file_count = 709
gitlink_count = 1
runtime = 344620afba252b36b05a7bdb032b554aa7d62262
tree_sha256 = c2f6ed35fafcf7c3c67a1c7296bef82ea4603e4961844060b7d2796c152c642d
```

CI 在安装依赖前执行：

```bash
python util/source_delivery_gate.py \
  --manifest source-delivery-manifest.json \
  --require-tracked \
  --require-clean
```

门禁同时检查文件集合、内容摘要、manifest 规范性、Git tracked 状态和相对 `HEAD` 的修改。写 manifest 的临时文件会在写入或替换失败后清理。临时 Git 仓库测试覆盖精确 checkout、内容篡改、等量测试替换、未跟踪 Python/Rust/CI workflow、必需根文件缺失、重复 JSON member，以及 submodule index/checkout commit 不一致。

必须明确：内容级检查已经通过，普通文件和 gitlink 均无 missing、unexpected 或 changed 项；但当前工作树的严格 Git 检查仍失败，准确报告 618 个受保护成员未被跟踪、20 个受保护成员相对 `HEAD` 有修改。整个仓库当前另有 634 个未跟踪 status entry 和 27 个 tracked dirty entry。门禁解决的是“以后不能静默漏交付”，不等于已经替用户选择、提交或覆盖这些既有修改。完成正式版本仍需审核变更归属，执行 Git 纳入和提交，并在干净 recursive clone 中重跑全门禁。

## 8. 测试结果

最终验证分为专项回归、capability-closed Python 门禁和编译器双版本门禁。专项集合用于定位变更影响，集合之间存在重叠，不能相加为测试总数：

| 测试范围 | 结果 |
| --- | ---: |
| QueryStore + source delivery | 36 passed，25 subtests passed |
| distributed state + AFL profile orchestration（最终修改后） | 242 passed，77 subtests passed |
| scale model 专项 | 12 passed |

权威 Python 门禁在最终源码清单生成后运行，结果如下：

| 项目 | 结果 |
| --- | ---: |
| 固定 node ID | 1597 / 1597 matched |
| pytest | 1597 passed，607 subtests passed |
| 耗时 | 328.39 s |
| skipped / xfailed / xpassed / deselected | 0 / 0 / 0 / 0 |
| collection errors / missing node IDs / unexpected node IDs | 0 / 0 / 0 |
| 必需能力 | Z3、cvc5、Bitwuzla、AFL++、MPI、LLVM、OpenSSL 与五个 Python 模块全部存在 |

`python-test-gate.json` 的 SHA-256 为 `ff2e6b7595befe39b972dc476b6fc839742a7eedc602a9a4d916c238ee966891`；node-ID 集合摘要为 `54b09456f4e0c6871ca8c8edb75d3f2fd0184145eae524a6c2670d04270f4928`。编译器/IR 门禁结果为：

| 工具链 | 发现 | 通过 | capability-labeled unsupported | 失败 | 耗时 |
| --- | ---: | ---: | ---: | ---: | ---: |
| LLVM 18.1.3 | 346 | 345 | 1 | 0 | 297.39 s |
| LLVM 17.0.6 | 346 | 344 | 2 | 0 | 298.27 s |

unsupported 项由工具链能力显式标记；LLVM 17 比 LLVM 18 多一个 cross-LLVM replay 不可用项。上述结果证明当前实现的测试契约和双 LLVM 行为门禁通过，不等同于 admission 端到端吞吐已提升，也不替代多机共享文件系统上的长期故障注入实验。

## 9. 边界与下一步

1. 当前 admission 多线程是否提升吞吐取决于 payload 结构、Python GIL 和批量大小；需要以 `validation_seconds`、master service time 和 worker queue wait 做 A/B，当前只证明有界性与语义不变。
2. WAL 已把常态解析移出锁区，但尚未建立持久 shard-to-transaction 索引；只有 pending WAL 很多且频繁恢复时，才值得引入更复杂的索引一致性协议。
3. bootstrap 区间刻画采样波动，不修正目标、seed、预算或机器选择偏差；正式比较仍需等 CPU、跨 seed、长时 blocked design。
4. 版本化交付是当前唯一未闭合阻断项。其原因不是源码内容不一致，而是当前仓库包含大量未提交、且归属不全由本轮决定的既有修改；不能为了让门禁变绿而擅自提交。只有 `source_delivery_gate --require-tracked --require-clean` 在 clean recursive clone 通过，才能把本机通过表述为可复现版本。

本轮修复的创新点不是增加一个孤立启发式，而是把“结果发布、覆盖所有权、租约身份、规模决策和源码交付”统一为可恢复、失败关闭、可审计的证据链。它降低了后续 SOTA 调度和智能策略建立在错误状态或偏置实验之上的风险。
