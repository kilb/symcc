# F467：第七轮深度审查修复闭环

- 日期：2026-09-05
- 基线：主仓库 F466 修复分支，runtime 子模块 `c75f5ed`
- 范围：LLVM/runtime 位向量语义、QueryStore 租约领取、solver portfolio、MPI hybrid 结果准入、AFL 终止状态、覆盖 bitmap 合并、result object 生命周期、CPU placement 与规模模型证据门禁
- 方法：先复现 F466 反例，再按正确性优先级修复；每个修复均补最小回归测试，随后执行多模块 pytest、双 LLVM lit 与编译验证

## 1. 总体结论

本轮工作将 F466 中确认的关键问题从“已复现缺陷”推进到“代码闭合、测试闭合、文档闭合”。
优先级最高的变化集中在三条链路：第一，修正合法 LLVM intrinsic 在符号运行时中的位宽和打包语义，避免符号执行在目标程序进入 funnel shift、overflow、宽整数路径时崩溃或生成错误约束；第二，缩短 QueryStore 领取租约的 SQLite 写事务，避免短租约在返回时已经过期，并解除大 artifact I/O 对并行求解入口的串行化放大；第三，修正 hybrid worker 结果的 admission、终止状态和内容寻址传输生命周期，使 master 在高并行下能够持续接收、复验和回收结果。

这些改动不是新的 benchmark 结论，而是并行符号执行框架的正确性与可扩展性基础设施。当前证据可以证明：原有反例已被测试覆盖，相关 Python 回归稳定通过，LLVM 17/18 下新增 runtime 反例均可执行通过。性能提升仍需后续长时等 CPU benchmark 单独量化。

## 2. 关键修复与实现细节

| 方向 | 问题 | 修复 |
| --- | --- | --- |
| runtime intrinsic 语义 | `llvm.fshl/fshr` 对 `2N` 位 concat 使用 `N` 位 shift，且 extract 范围错误 | 将 shift 零扩展到 `2N`；`fshl` 提取 `[2N-1:N]`，`fshr` 提取 `[N-1:0]` |
| overflow intrinsic | unsigned add/sub 用 `BV1 == Bool`，unsigned mul 只检查最高位 | add/sub 通过 `_sym_build_bit_to_bool` 转 Bool；mul 检查整个高半区是否非零 |
| struct `{iN, i1}` 打包 | overflow 返回值的小端打包依赖 `bswap`，i24 等非 16 倍数位宽会断言 | 改为按 byte lane extract，支持任意整字节宽度 |
| aggregate 小端打包 | `insertvalue`/struct 常量路径复用 `_sym_build_bswap`，其 16 位倍数断言仍会拒绝合法 i24 字段 | 将 `_sym_build_bswap` 放宽到任意整字节位宽，并为 8 位值保留恒等返回 |
| 宽整数常量 | `i129/i256` 常量被截断到 128 位或位宽参数溢出 | compiler 对 `>128` 位整数走 `_sym_build_integer_from_buffer`；simple/QSYM runtime 支持任意正位宽 buffer 常量 |
| QueryStore claim | lease deadline 在等待写锁前计算，且写锁包住三份 artifact I/O | 写锁内重新取时钟并只完成领取；query body 与 sealed artifact 在锁外验证，失败时用 owner/token 精确释放 claim |
| portfolio 求解 | SAT winner 后仍等待不可取消的 tail future；若完全不等待，又会让 persistent solver 的取消恢复协议与后续调用竞争 | SAT 后记录取消尝试，普通不可取消 callable 不再阻塞返回；带 `cancel()` 协议的 solver 给予短暂 cooperative cleanup，并用真实 attempt/result 覆盖 cancelled 占位 |
| batch AFL 状态 | batch showmap 只给覆盖图，不能区分崩溃/超时 | 新增 `SYMCC_BATCH_VERIFY_STATUS`，默认对 batch 候选执行有界 status 复验；旧的 novelty-only 开关不再隐式关闭状态复验，ok 且无边时保留 batch 边 |
| signal 分类 | 旧逻辑只看 `retcode > 128`，会把 SIGTERM/SIGINT 误当崩溃，且漏掉 Python `subprocess` 的负信号编码 | 统一 `_returncode_indicates_crash`：在负信号或 shell `128+signal` 编码中仅接受 SIGABRT/SIGBUS/SIGFPE/SIGILL/SIGSEGV/SIGTRAP；timeout 与关停信号不入崩溃目录 |
| result admission | 只提交完成队列的有序前缀，慢头项阻塞后续已完成结果 | `has_ready()` 扫描所有 pending future；`collect_ready()` 释放已完成 tail，避免 admission capacity 被队头占满 |
| `.result_objects` 生命周期 | worker CAS 结果对象运行期只增长，异常或长跑会积累 | master 周期扫描 `.result_objects`，保护 pending admission 引用，按 age grace 删除孤儿对象并输出统计 |
| coverage bitmap | dense map 每次整图转超大整数；稀疏 hit=0 会污染 edge 集 | full bitmap 改为 16 KiB chunk bit-count/OR；稀疏合并忽略 0 hit 行 |
| CPU placement | 基于整机 `os.cpu_count()` 计算高位核，在 cpuset 中可能全越界 | 基于 `sched_getaffinity(0)` 的实际可用核选择 SymCC 保留核；rank0 master 使用保留核集合，worker 单核绑定 |
| hybrid 资源账本 | honggfuzz/Grimoire 等外部辅助进程未计入模型 | benchmark 汇总把外部辅助槽加入 `auxiliary_compute_slots`，scale model 的资源上限据此扣减 |

## 3. 执行流程影响

修复后的 hybrid 主流程保持原架构：AFL 产生输入，master 读取队列并通过 coverage bridge 更新全局 edge bitmap；master 将候选输入或 live-state continuation 以 dispatch token 分配给 worker；worker 运行 SymCC/SymSan，生成候选、bitmap、telemetry、proposal 和终止状态；结果通过 MPI 返回 master，先进入 bounded admission service 做 schema、预算、CAS 和状态复验；通过后再进入 `_batch_triage`，由 master 统一决定是否写入 AFL queue、crashes、hangs、extras 和 feedback queue。

本轮变化主要改变三个位置。第一，worker 侧 batch 候选不再只依赖 coverage map 判断进程状态，而是默认补一次 status 复验。第二，master 侧 admission 不再被早到的慢 future 阻塞，任意已完成复验都能先释放容量。第三，`.result_objects` 成为有保护集的运行期 CAS：pending result 引用在 admission service 中显式 pin，提交或拒绝后由 grace-based GC 回收未再被引用的对象。

## 4. 规模模型与证据边界

规模模型保持保守策略：可用于汇报决策的 ceiling 必须同时满足成功率、独立随机种子、完整 seed block、相同 wall budget、非采样 endpoint coverage、固定 hybrid AFL/SymCC allocation ray、稳定辅助槽账本、足够并行层级和足够 bootstrap 成功率。覆盖饱和模型仍然 fail-closed：如果观测均值非单调或曲线拟合质量不足，模型不输出正式推荐上限，只保留探索性估计和拒绝原因。

本轮没有把随机噪声“平滑”成看似更好的结论。这样做的好处是汇报材料不会把短时或采样实验误写成规模规律；代价是很多现有短实验只能用于定位瓶颈，不能用于证明并行上限。后续若要提升模型可用性，应采用 paired-seed 增量模型或 isotonic latent coverage，再保留原始均值、置信区间和单调性违反幅度。

## 5. 新增/更新测试

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test/funnel_shift_symbolic.ll` | 符号 `llvm.fshl/fshr` 的 runtime 执行语义，包含超过位宽的 shift amount 取模边界 |
| `test/aggregate_i24_symbolic.ll` | `insertvalue`/`extractvalue` 在 i24 小端 aggregate 打包中的 runtime 执行语义 |
| `test/unsigned_overflow_symbolic.ll` | signed/unsigned add/sub/mul overflow 的 Bool/BV 与乘法高半区判定 |
| `test/uadd_sat_i256.ll` | `i256` 饱和算术和宽整数常量路径 |
| `test/test_query_store.py` | claim deadline、锁外 artifact materialization、portfolio SAT 取消 tail、cooperative cancel 账本修正 |
| `test/test_mpi_lifecycle.py` | CPU affinity、batch status、result object GC、dense/sparse coverage merge、signal 分类 |
| `test/test_afl_profile_orchestration.py` | result admission tail-ready 无队头阻塞、父输入负信号保存 |
| `test/test_current_evaluation_analysis.py` | cpuset 内 SymCC CPU list 计算 |
| `test/test_parallel_scale_model.py` | 规模模型的证据门禁、bootstrap 噪声、资源账本与 hybrid allocation ray |

## 6. 验证结果

| 验证项 | 结果 |
| --- | --- |
| `test/test_query_store.py` | 45 passed, 50 subtests passed |
| `test/test_qf_bv_backend.py test/test_qf_bv_strategy_campaign.py` | 19 passed |
| `test/test_mpi_lifecycle.py test/test_afl_profile_orchestration.py` | 181 passed, 92 subtests passed |
| `test/test_current_evaluation_analysis.py test/test_topseed_selector.py test/test_parallel_scale_model.py` | 49 passed, 7 subtests passed |
| 全量 Python pytest：`test/` | 1682 passed, 638 subtests passed |
| LLVM 18 lit：新增 4 个 runtime 反例 | 4 passed |
| LLVM 17 lit：新增 4 个 runtime 反例 | 4 passed |
| `ninja SymCCRuntime SymCC`（LLVM 18 build） | passed |
| `ninja SymCCRuntime SymCC`（LLVM 17 build） | passed |
| `python3 -m compileall` 关键 Python 模块 | passed |

## 7. 结论边界与后续工作

本轮可以对外表述为：并行混合符号执行框架完成了一次反例驱动的正确性和扩展性闭环，覆盖了 runtime 位向量语义、异步 query 领取、结果准入、覆盖反馈、终止状态和资源模型这些影响实验可信度的基础路径。

仍不能表述为：这些修复已经在公共 benchmark 上证明覆盖率或速度有稳定百分比提升。原因是本轮主要是缺陷修复和机制闭合，性能效果必须通过长时、等 CPU、固定随机种子、固定 allocation ray 的 benchmark 重新采集。下一阶段建议优先安排三类实验：LAVA-M/CGC/真实 parser 的 hybrid 对照，1/2/4/8/16/32 workers 的多轮并行扩展实验，以及 admission/QueryStore/coverage merge 的 master 热路径 profile。
