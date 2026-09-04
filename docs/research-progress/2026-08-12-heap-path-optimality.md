# UCSan/POSE Heap Path Optimality 实现记录

新增 `util/heap_path_optimality.py`，把 heap/object graph 中的候选路径统一表示为对象路径、深度、分配大小、alias 数量、目标距离和新颖性。优化器以目标距离、新颖性、alias 关系和内存/深度成本构成可解释评分，在有界 frontier 中淘汰最低分路径，并输出 `symcc-heap-path-optimality-v1` 证据摘要。

该模块只负责 frontier 选择，不伪造 heap 语义；真实对象图仍由 UCSan seed/运行时提供。这样可以将 POSE 式对象路径优先级接入现有 worker 调度，同时保留具体执行和回放作为正确性来源。

专项测试覆盖 alias/目标距离排序、容量淘汰、非有限数和伪整数拒绝，结果 3/3 通过，Ruff 检查通过。
