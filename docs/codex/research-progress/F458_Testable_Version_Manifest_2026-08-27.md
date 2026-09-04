# F458 可测试版本清单

**日期：** 2026-08-27  
**用途：** 固定本轮深度审查后的测试入口、关键文件和证据边界，防止打包时遗漏未跟踪源码。

## 1. 必须随版本交付的实现

| 子系统 | 关键文件 |
| --- | --- |
| MPI/ULFM 恢复 | `util/mpi_ulfm_recovery.py`、`util/mpi_concolic_execution.py` |
| AFL/SymCC 编排 | `util/mpi_fuzzing_helper.py`、`util/concolic_engine.py` |
| Query 与 continuation 租约 | `util/query_store.py`、`util/live_continuation.py`、`util/live_state_frontier.py` |
| 全局覆盖事务 | `util/distributed_state.py` |
| 实验统计与规模模型 | `benchmark/analyze_ablation.py`、`benchmark/analyze_parallel_scaling.py`、`util/parallel_scale_model.py` |
| SymSan 参数协议 | `scripts/build_symsan.sh`、`scripts/symsan_patches/symsan_target_argv.patch` |

对应测试至少包括 `test/test_mpi_ulfm_recovery.py`、`test/test_mpi_lifecycle.py`、
`test/test_afl_profile_orchestration.py`、`test/test_query_store.py`、
`test/test_persistent_live_continuation.py`、`test/test_persistent_live_state_frontier.py`、
`test/test_distributed_state.py`、`test/test_ablation_analysis.py`、
`test/test_parallel_scale_model.py`、`test/test_concolic_engine.py`、
`test/poly_exact_widening_renaming.c` 和 `test/poly_exact_narrowing_renaming.c`。

## 2. 可重复测试入口

```bash
python3 util/python_test_gate.py \
  --output python-test-gate.json \
  --min-collected 778 --max-skips 0 --max-xfails 0 --max-xpasses 0 \
  --max-deselected 0 --max-missing-nodeids 0 --max-unexpected-nodeids 0 \
  --require-nodeid-manifest test/pytest-nodeids.json \
  --require-command cc --require-command z3 --require-command cvc5 \
  --require-command bitwuzla --require-command afl-clang-fast \
  --require-command afl-showmap --require-command mpiexec \
  --require-command openssl --require-command opt --require-command llvm-diff \
  --require-module mpi4py --require-module tree_sitter \
  --require-module tree_sitter_json --require-module lark \
  --require-module parglare --require-library z3 \
  -- -q -W error -p no:cacheprovider

ninja -C build
ninja -C build check
```

最终本机结果：Python 为 `1576 passed + 581 subtests`，严格门禁的所有失败、跳过和清单漂移
计数均为零；QSYM/Clang lit 为 `344 passed + 1 unsupported`，无失败。测试环境与逐项结果见
`python-test-gate.json` 和 F458 研究报告。

## 3. 证据身份

| 对象 | SHA-256 |
| --- | --- |
| canonical node-id 集合 | `f4fe4bbb81ca0dce757bc48a7d8188764647deb208d3156cbfd3734b60cf6006` |
| `test/pytest-nodeids.json` 文件 | `0c215ad5534f80a4e3b8b8f4b1d66e4ca2ccc402b138c7aab5db36fcaa3a7597` |
| `python-test-gate.json` | `15b9b6871803f872f2b23692abee12dca2ab7dc6f2d8c6dd8318208500b13226` |
| SymSan argv patch | `08f84a7ba2d913aef42e578551468e319672eb23df289dd8048531d48df9bdde` |

SymSan 补丁已在上游提交 `1d5c77521ff3ef183a77f662ddce0e00b3ed5fba` 应用既有基础补丁后的
源码上通过 `git apply --check`。这证明补丁上下文可套用，不等价于本轮重新完成整个 SymSan 构建矩阵。

## 4. 发布边界

审计结束时共享工作树仍有 27 个已跟踪修改和 631 个未跟踪路径，其中若干关键实现与测试属于
未跟踪文件。该状态来自持续开发工作树，本轮没有擅自重置、删除或提交其他改动。因此：

1. 当前目录是已通过测试的本地版本；
2. 直接执行只包含 `git diff` 的打包会漏掉核心文件；
3. 正式发布必须先由仓库负责人确认文件归属，将上表文件与测试清单全部纳入提交或内容清单；
4. 本轮测试证明机制与回归正确性，不证明公共 benchmark 上的 coverage、speedup 或缺陷发现率提升。

完整设计、错误机理和结果见
[`Deep_Correctness_and_Scaling_Review_F458_2026-08-27.md`](Deep_Correctness_and_Scaling_Review_F458_2026-08-27.md)。
