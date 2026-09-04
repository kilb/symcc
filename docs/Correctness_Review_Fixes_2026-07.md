# 并行符号执行正确性审查与修复记录（2026-07）

## 1. 审查目标

本轮工作不是新增一组孤立的启发式，而是审查优化是否保持以下端到端不变量：

1. 位向量语义不能被无条件替换为数学整数语义；
2. 任何跳过精确 SMT 查询的 SAT 候选都必须满足目标和相关路径前缀；
3. 任何复用的 UNSAT 结论都必须绑定产生结论的完整约束上下文；
4. 调度器只能根据目标查询自身的证据将目标永久终结；
5. 编译期、运行时和分布式 sidecar 必须共享可复现的标识；
6. 多 master 中只有当前 fencing token 的结果可以修改全局状态；
7. 覆盖反馈的采样预算不能改变 libc 模型的符号语义。

审查发现 10 类问题，覆盖 QSYM 求解器、libc 模型、UCSan、LLVM Pass、MPI
协调器和 benchmark 分析。修复采用“保守拒绝、精确回退”的原则：优化不能证明
安全时回退 Z3 或允许重试，而不是继续使用近似结论。

## 2. 位向量线性化的无回绕证明

### 2.1 错误模型

原 polyhedral 前端把 `Add/Sub/Mul` 位向量直接提取为整数仿射式。例如 8 位
`x + 1` 被解释为整数 `x + 1`，忽略了 `255 + 1 == 0 (mod 256)`。因此路径
`(x + 1) < 2` 与目标 `x == 255` 会被错误判为矛盾，并在 Z3 前提前 UNSAT。

### 2.2 实现

`extractLinearExpression` 现在同时返回：

- 稀疏系数表和常数项；
- 该表达式在字节域上的保守最小值、最大值。

每个 `Add/Sub/constant-Mul` 节点使用 `__int128` 计算系数和区间，仅当整个数学
结果落入该节点的无符号位宽范围 `[0, 2^w-1]` 时才接受线性化。`SExt`、`Neg`
和可能回绕的算术直接拒绝。`ZExt` 与最多 6 字节的 `Concat` 保留，因此
`zext(i8)+zext(i8)` 的宽位无回绕场景仍可使用 Pangolin 式整数模板。

这是一项充分而非必要条件。它可能放弃部分可线性化查询，但不会把模算术错误地
证明为整数矛盾；拒绝后由原有 Z3 路径处理。

### 2.3 验证

`test/poly_modular_soundness.ll` 固定构造 8 位回绕反例，要求启用 poly cache 后
仍生成 `0xff`。该测试直接覆盖此前的错误 UNSAT。

## 3. 上下文绑定的 poly/Z3 复用

### 3.1 错误模型

旧持久化键主要包含目标表达式 hash、输入大小和依赖字节，没有包含实际相关路径
前缀。由 `x <= 0, target x == 1` 得到的 UNSAT 可以污染
`x <= 2, target x == 1` 的 SAT 查询。

SAT delta model 也可能应用到不同的当前输入；即使表达式结构相同，未写入 model
的字节仍可能使前缀失效。

### 3.2 实现

新的 `poly-cache-v2` 键混合：

- 稳定 site ID 和 open-branch hash；
- 实际 taken 方向与输入大小；
- 经排序、去重后的相关前缀表达式 fingerprint；
- 完整目标表达式文本和依赖偏移。

版本 salt 使旧键自然失效。SAT model replay 后还会对目标谓词和所有相关前缀
逐一 concrete-evaluate；验证失败视为 cache miss 并回退 Z3。UNSAT 只有在 v2
完整上下文键命中时才可剪枝。

`test/poly_prefix_cache.c` 用同一二进制、同一目标 site 和共享 cache 连续执行
窄/宽前缀，验证第二次仍产生 `x=1`。

## 4. Fast-solve 的完整路径验证

### 4.1 错误模型

原 fast-solve 只验证候选翻转了当前目标。例如前缀 `x != 4`、目标 `x == 5`、
seed `x=5`，局部反解会选择 `x=4`。它满足目标的否定，却违反前缀；由于 fast
路径返回成功，精确 Z3 查询被错误抑制。

### 4.2 实现

单字节和 `Concat` fast-solve 在写文件前统一调用
`validateCandidateAgainstPrefix`：

1. concrete-evaluate 目标为期望方向；
2. 收集 dependency forest 中与目标相关的前缀；
3. concrete-evaluate 每个前缀为 true。

任何失败都返回 `false`，使 `negatePath` 继续执行 Z3。poly SAT replay 使用同一
验证器，形成统一的候选可信边界。

`test/fast_solve_prefix.c` 要求 fast 模式既生成合法的 `x=4` 前缀翻转，也通过
Z3 生成一个不属于 `{4,5}` 的完整路径候选。

## 5. 目标查询终态证据

### 5.1 错误模型

协调器曾用一次执行的全局 `solver_sat/solver_unsat/generated` 计数推断指定目标
的状态，并把没有到达目标的 deterministic replay 标为永久 `stale`。这些计数
可能来自目标之前的其他分支，因此会错误退休仍可求解的目标。

### 5.2 实现

QSYM 遥测新增 `target_status`：

- `sat`：fast、验证后的 cache replay 或严格查询得到 SAT；
- `unsat`：精确 UNSAT core、可靠线性矛盾或严格查询得到 UNSAT；
- `unknown`：目标查询未知/超时；
- `none`：到达但没有执行目标查询。

Python `SolverTelemetry` 显式解析该字段。`ConstraintSummaryCache` 只把目标自身的
`sat/unsat` 作为永久终态；`unknown` 和未到达分别成为 `timeout/diverged`，使用
有界指数退避后允许重试。历史持久化的 `stale` 在恢复时迁移为 `diverged`。
Prefix DAG、CSTG 和 replay open-branch 删除逻辑均使用相同终态定义。

对应验证位于 `test/telemetry.c` 和
`test/test_hybrid_feedback.py::test_constraint_cache_retries_diverged_target`。

## 6. libc 长字符串语义与 data coverage 解耦

### 6.1 错误模型

`SYMCC_DATA_CMP_BYTES` 的默认 64 字节预算同时限制了 `strcmp/strncmp` 的语义
扫描。超过 64 字节且前缀相等时，wrapper 无法确认比较边界，因而不建立完整
符号相等约束。反馈采样参数由此改变了程序的符号语义。

### 6.2 实现

`comparedCStringBytes` 按 libc 实际停止条件扫描到 NUL、首个差异或 `n`。只有
`notifyByteDataCoverage` 和 concrete hint 继续受 64 字节默认预算限制。
因此求解语义完整，而 coverage-map 成本仍有界。

`test/libc_long_compare.c` 使用 80 字节相等字符串，并显式保持
`SYMCC_DATA_CMP_BYTES=64`，要求仍产生替代输入。

## 7. UCSan shadow 与入口失败原子性

### 7.1 部分复制

旧 `_sym_ucsan_copy_shadow` 只检查 pointer shadow 的起始地址是否落在复制范围，
即使只复制一个字节，也会传播整个指针 shadow。新实现要求
`stored_shadow.size <= copy_size - offset`，只有完整包含的 shadow 才复制。

`test/ucsan_partial_shadow.c` 直接调用 ABI，确认 1 字节复制不会在目标地址产生
完整指针 shadow。

### 7.2 无效入口

旧 Pass 会先重命名 `main`，然后才发现配置入口不存在；缺失入口可能留下不可链接
模块。variadic harness 创建失败也没有被调用者处理。

现在入口不存在、仅声明或 variadic 时在任何重命名之前
`report_fatal_error`。`createHarness` 的空返回也成为 fatal。对应诊断由
`test/ucsan_invalid_entry.c` 覆盖。

## 8. 可复现的 LLVM site ID

### 8.1 错误模型

原 site ID 是 LLVM 编译进程中 `Instruction/BasicBlock` 宿主指针的截断值。
同一源码重复编译会改变 directed distance、static dependency、concurrency
sidecar 和运行时 branch telemetry 的标识，破坏跨运行缓存与实验复现。

### 8.2 实现

新增 `compiler/SiteId.h`。ID 使用 FNV-1a 混合：

- 模块 source identity；
- 函数名；
- value 类型标签；
- 基本块序号、指令序号和 opcode。

模块初始化阶段给原始指令附加 `symcc.site_id` metadata。后续 intrinsic lowering
或符号插桩即使插入新指令，原 branch 仍保留最初 ID。`Pass.cpp` 的所有 sidecar
和 `Symbolizer` 的运行时通知共享该实现；Symbolizer 在修改 IR 前预取函数内
value ID。结构图中的 module/block ID 也不再包含宿主地址。

`test/stable_site_ids.c` 同时验证：

1. 同一源码输出到不同二进制时 `#SITE` 集合完全相同；
2. 运行时 `branch_trace.site` 是静态 sidecar `#SITE` 的成员。

该方案保证相同 IR 结构的确定性，不承诺不同优化级别或实质性源码变更后保持 ID。

## 9. 多 master fencing 的提交阶段

### 9.1 错误模型

旧 master 在 batch triage、覆盖率更新、DPOR、统计和 agentic 状态修改之后才调用
`complete(work_id, token)`，且忽略返回值。租约过期并被另一 master 接管后，旧
worker 的迟到结果仍可污染全局状态。

### 9.2 实现

`FencedWorkLeaseTable` 增加原子 `begin_commit`：

- 只接受状态为 `leased/committing` 且 token 完全匹配的记录；
- 在共享锁内切换到 `committing` 并刷新时间；
- claim、heartbeat、complete 和 crash recovery 都识别该状态。

MPI master 只使用 dispatch 时保存的 active lease/token，不信任 worker 回传字段。
结果到达后，在更新任何统计、DPOR、遥测、proposal 或 coverage 之前调用
`begin_commit`；验证失败立即丢弃并清理本地 active map。triage 完成后再进入
`done`。master 在 committing 阶段崩溃时，记录在 TTL 后可恢复和重新派发。

此外，worker 的 `RESULT` 与下一轮 `READY` 使用不同 MPI tag。master 现在把仍有
active work 的 READY 暂存为 `pending_ready`，只有对应 RESULT 消费后才允许再次
派发，避免新 lease 覆盖旧任务的 active 映射。共享租约模式下缺少可信 active
lease/token 的孤立或重复 RESULT 一律丢弃。

`test/test_distributed_state.py` 覆盖迟到 token 被拒绝、committing 阻止并发 claim
以及 committing 崩溃后的超时恢复。

## 10. Benchmark 消融字段一致性

`run_benchmark.py` 输出 `edge_cov_pct`，而 `analyze_ablation.py` 旧默认值是不存在的
`edge_cov`，导致不带 `--metric` 的标准命令生成空报告。默认值和帮助文本已改为
`edge_cov_pct`；显式指定其他指标仍兼容。CLI schema 回归位于
`test/test_ablation_analysis.py`。

## 11. 可信边界与性能代价

本轮修复把性能优化分为三类：

| 路径 | 接受条件 | 失败行为 |
| --- | --- | --- |
| 线性 UNSAT 剪枝 | 位宽区间证明无回绕，且上下文键完整 | 回退 Z3 |
| fast/cache SAT | concrete target + 全相关前缀验证 | 回退 Z3 |
| 调度终态 | 目标自身精确 `sat/unsat` | 退避后重试 |

额外成本主要是 fingerprint 字符串、候选 concrete evaluation 和编译期 ordinal
扫描。它们只位于 cache/fast/sidecar 路径，不改变普通 concrete 执行的表达式
构造热路径。后续 benchmark 应分别报告：

- fast/cache 命中后验证失败率；
- poly 无回绕拒绝率与 Z3 fallback 成本；
- `diverged/timeout` 重试的最终转化率；
- stable-ID fingerprint 的编译时间与 sidecar 去重收益；
- multi-master stale-result 丢弃数和 committing recovery 数。

## 12. 验证清单

定向验证：

- `poly_modular_soundness.ll`
- `poly_prefix_cache.c`
- `fast_solve_prefix.c`
- `libc_long_compare.c`
- `ucsan_partial_shadow.c`
- `ucsan_invalid_entry.c`
- `stable_site_ids.c`
- `telemetry.c`
- `test_hybrid_feedback.py`
- `test_distributed_state.py`
- `test_ablation_analysis.py`

发布前执行：

```bash
PATH=/usr/lib/llvm-18/bin:$PATH ninja -C build
python3 -m unittest discover -s test -p 'test_*.py' -v
PATH=/usr/lib/llvm-18/bin:$PATH ninja -C build check
git diff --check
```

本轮结果等级为 **T**：正确性反例和全量工程回归构成测试证据。它不等于公开
benchmark 上的性能提升（R 级）；性能主张仍需等 CPU、多轮、置信区间和逐项消融。

2026-07-24 最终验证结果：

- 增量构建：通过；
- 第一方 Python 单元测试：126/126 通过；
- LLVM lit/compiler 集成测试：75/75 通过；
- 第一方 Python `compileall/py_compile`：通过；
- ruff fatal 规则 `E9,F63,F7,F82`：通过；
- 主仓库与 runtime 子模块 `git diff --check`：通过。

## 13. Replay prefix 发布与 trace 线性化（2026-07-27）

### 13.1 错误模型

旧 `schedule_gate()` 在匹配线程成功递增 `prefix_index` 后返回，
`controlled_event()` 才调用 logger。另一个等待线程可在两步之间看到 prefix 已消费，
先记录自己的受控事件。真实执行仍可能正常完成，但 trace 的受控事件序与物化证书
prefix 不一致，导致回放验证偶发出现期望 tid 2、实际首行 tid 1。

### 13.2 实现

`util/symcc_schedule_rt.c` 新增独立 `replay_gate_lock`。存在 active prefix slot 时，
以下操作成为同一临界区：

1. 读取并判断当前 slot；
2. 匹配时记录带原始 tag 的 controlled event，超时时依次记录 fallback 和 event；
3. 最后以 release store 发布新的 `prefix_index`。

等待线程只有在上一个受控事件已经写入后才能观察下一 slot。无 active prefix 时仍走
原有无锁 fast path；logger 继续使用独立 cancellation-safe `log_lock`，不改变
pthread operation 本身的锁语义。

### 13.3 验证

- 原真实 mutex prefix/topology replay 回归通过；
- 同一真实 replay 用例独立进程压力重跑 50/50 通过；
- `test_schedule_exploration.py` 50/50 通过；
- `cc -Wall -Wextra -Werror -fPIC -shared -pthread` 通过；
- 全量 Python 207/207、LLVM lit 88/88、增量构建和 `git diff --check` 通过。

该修复保证 trace scheduling-point 的发布顺序与 prefix 一致；它不保证目标线程在
没有 timeout fallback 的情况下必然 enabled，也不把 pthread-only replay 扩展到
weak-memory propagation。
