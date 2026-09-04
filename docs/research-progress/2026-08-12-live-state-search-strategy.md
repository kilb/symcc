# Live Symbolic-State Search Strategy 实现记录

## 背景

项目原有 `AdaptiveHybridScheduler` 已经能够对 seed、路径和 solver strategy 做上下文 bandit 选择，但它的调度单位仍然是输入路径。现代并行符号执行还需要在 worker 队列中直接比较活动状态：目标距离、路径深度、约束求解成本、控制流新颖性、数据覆盖新颖性和待探索分支数必须共同参与排序。

## 方案

新增 `util/live_state_scheduler.py`。`LiveState` 是与执行器解耦的状态摘要，字段包括 `state_id`、`prefix_id`、深度、目标距离、约束成本、控制流/数据新颖性、待探索分支数和 worker 亲和性。`LiveStateScheduler` 提供：

- `upsert`：有界状态表，超过容量时淘汰当前综合分最低的状态；
- `select`：按多目标启发式 + UCB 不确定性选择 ready states，可按 worker 过滤；
- `observe`：记录奖励、耗时和 killed 结果，更新后续选择；
- `snapshot`：输出可审计的统计摘要。

评分同时考虑新颖性、数据反馈、目标距离、待探索分支、路径深度、历史 reward/成本、探索不确定性和失败惩罚。状态选择本身不执行 solver，也不改变 path condition；因此可由 MPI coordinator 或本地 worker 调用，真正的执行与资源强制仍留在现有引擎。

## Review 与验证

专项 review 检查了 NaN/Inf、负数、布尔伪整数、状态数量和选择批次上限；调度器使用 `RLock` 保护状态表、统计和总 pull 计数，避免并发 worker 更新产生部分状态。新增 `test/test_live_state_scheduler.py` 覆盖多目标选择、反馈更新、worker 过滤、容量淘汰和非法浮点输入。

验证结果：

```text
python3 -m pytest -q test/test_live_state_scheduler.py test/test_agolic_planning.py
12 passed
python3 -m ruff check util/live_state_scheduler.py test/test_live_state_scheduler.py
passed
```

## 集成边界

当前模块是稳定的调度策略 API，尚未替换 C++ 执行器内部的 pending-state 容器。接入时应在 worker 产生/完成状态的边界填充 `LiveState`，将实际覆盖、求解耗时和 killed 状态映射到 `observe`；不能把 Python 评分器当作资源隔离器或可抢占执行器。
