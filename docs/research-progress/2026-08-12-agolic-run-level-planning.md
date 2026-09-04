# Agolic 式跨运行规划器实现记录

## 目标与依据

本阶段实现论文 *Agolic: Agentic Planning for Symbolic Execution* 所描述的跨运行控制层。规划器位于多个 bounded symbolic execution (BSE) 运行之间；单个运行内部的状态选择仍由 SymCC/KLEE 执行器负责。实现边界依据论文的 round-level procedure：回放语料、生成并审查目标、隔离执行、按完成顺序回放并记录证据、进入下一轮。

## 实现方案

核心模块为 `util/agolic_planning.py`，由两个组件组成：

1. `AgolicRunLevelPlanner`：维护实验/程序身份、回放覆盖、目标候选、已发出计划、历史、资源 profile 和规划失败计数。计划由目标、模式、profile、时间/内存上限、环境、符号输入和可选 witness 组成；计划指纹由规范化 JSON 的 SHA-256 计算，同一精确规格不会重复派发。
2. `AgolicRoundController`：启动可选的 continuous run，在每轮调用 `replay_coverage`，将 frontier 交给 planner，经 `preflight` 审查后并行调用 `execute_run`。worker 只返回运行产物；完成后由 coordinator 串行调用 `replay_run`，把本次产物加入语料、执行原生回放，再调用 `record_outcome`。因此覆盖增量严格按照当时的语料基线归因，避免并行 worker 直接写共享覆盖造成重复计数。

状态文件采用版本化 JSON、重复键/非有限数拒绝、SHA-256 身份绑定、规范化字段校验和临时文件 + `fsync` + 原子替换。加载时验证计划、目标、profile、witness、结果证据之间的一致性。历史达到上限时 fail-closed，不再通过截断历史破坏精确去重保证。

## 证据与模式

覆盖只接受 concrete replay 的结果。已记录覆盖必须是新回放覆盖的子集；target reach 由回放得到的函数集合决定，外部声明若冲突则拒绝。结果分为 `new-reach`、`increased-target-coverage`、`reached-no-gain`、`not-reached` 和未验证结果。支持 Harness-Entry 与 Witness-Guided 两种模式；后者必须引用 frontier 中审查过的 witness 及 release function/branch。仅支持 witness 的目标不会被错误降级为 harness 模式。

## 测试与结果

新增 `test/test_agolic_planning.py`，共 10 个单元/集成测试，覆盖：重启恢复与精确去重、四类回放证据、累计覆盖单调性、回放身份复用检测、witness 模式选择、未知字段和未审查 witness 拒绝、历史容量 fail-closed、原子提交失败回滚、symlink/重复 JSON/身份不匹配拒绝，以及规划失败重试与并行执行后串行回放。

验证命令：

```text
python3 test/test_agolic_planning.py
python3 -m ruff check util/agolic_planning.py test/test_agolic_planning.py
python3 -m py_compile util/agolic_planning.py
```

当前结果：10/10 测试通过，Ruff 与 Python 编译检查通过。该模块已提供生产级 coordinator API，但尚未把具体 benchmark 的 replay、preflight、BSE 进程隔离器绑定进来；下一步应在 benchmark runner 中实现这三个 callback，并使用真实 GCov/AFL corpus 做确认性实验。

## 已知边界

Python `ThreadPoolExecutor` 本身不能终止失控的外部 BSE 进程，因此 `execute_run` 必须由进程级 runner 执行 time/memory/cancellation 限制。规划器只负责资源字段的 admission 和状态审计。连续运行与 BSE 结果的 corpus 锁也必须由 `replay_run` 的拥有者实现。
