# SymCC-Parallel 新增实现技术档案

- 文档状态：持续维护
- 当前基线：2026-08-25
- 适用范围：本分支相对上游 SymCC 新增或显著扩展的编译器、运行时、求解器、
并行调度、AFL++ 协同、语义候选生成和 benchmark 能力。

## 1. 文档目的

本档案用于回答四个长期问题：

1. 新方案解决了什么研究问题，借鉴了哪类技术思想；
2. 方案在代码中如何实现，跨层数据如何流动；
3. 当前有什么可重复的功能性结果和测试证据；
4. 哪些结论尚未经过等 CPU、多轮公开 benchmark 验证。

本文是“已实现能力”的权威总账。详细环境变量以
[`Configuration.txt`](../Configuration.txt) 为准，最新学术依据与 SOTA 差距以
[`sota_hybrid_execution_2026.md`](../sota_hybrid_execution_2026.md) 为准，阶段计划以
[`Recent_Work_and_Next_Research_Plan.md`](../Recent_Work_and_Next_Research_Plan.md)
为准。

### 1.1 结果等级

为避免把工程完成度误写成科研结论，本文统一使用以下等级：

| 等级 | 含义 | 可以对外表述 |
| --- | --- | --- |
| I：Implemented | 主路径已经接入真实系统 | “已经实现并集成” |
| T：Tested | 有自动化单元、lit 或集成测试 | “已通过功能与回归测试” |
| B：Benchmarked | 有固定预算的本地测量 | “在给定环境测得……” |
| R：Research-validated | 有等 CPU、多轮、置信区间和消融 | “实验支持该方法提升……” |

截至当前版本，本文记录的主路径达到 I/T。历史 benchmark 产物可用于检查评测
管线，但最新组合尚未全部达到 R；因此不声称所有新机制已经独立取得 SOTA 覆盖率。

## 2. 系统总览

新增系统把单次 concolic execution 扩展为跨层闭环：

```text
LLVM instrumentation
  |  branch/site/dependency metadata, ITE, UCSan shadow operations
  v
QSYM/SymCC runtime
  |  path prefix, solver outcome, data progress, generated candidates
  v
engine-neutral SolverTelemetry
  |  PrefixDAG / ECT / structural graph / semantic classification
  v
MPI coordinator
  |  seed-worker-state assignment + solver sequence + replay target
  v
exact / tailored / sampling executors
  |  candidates, semantic proposals, schedule replays
  v
concrete target replay + authoritative AFL bitmap triage
  |  reward, cost, interference, coverage ownership
  +----------------------------------------------------------+
```

可信边界有两条：

- 求解器、采样器、agent 或语义模型只负责提出候选；
- 候选是否到达目标、是否保留，分别由真实执行 telemetry 和全局 AFL bitmap 决定。

## 3. 实现清单

| ID | 实现族 | 主要代码 | 核心测试 | 状态 |
| --- | --- | --- | --- | --- |
| F00 | 编译/runtime 基础正确性 | `compiler/Symbolizer.cpp`, `runtime/src/RuntimeCommon.cpp` | `pointer_width_casts.ll`, 上游回归测试 | I/T |
| F01 | 统一执行遥测与后端抽象 | `runtime/.../solver.*`, `util/hybrid_feedback.py`, `util/executor_portfolio.py` | `telemetry.c`, `test_hybrid_feedback.py`, `test_executor_portfolio.py` | I/T |
| F02 | Backsolver 与 bounded Veritesting | `compiler/Symbolizer.*`, `compiler/Pass.cpp`, `runtime/.../solver.*` | `backsolver_*.ll`, `veritesting_memory_reject.ll` | I/T |
| F03 | 分层求解、Pangolin 上下文复用与 UNSAT 复用 | `runtime/.../solver.*` | `poly_cache.c`, `unsat_core_cache.c` | I/T |
| F04 | data coverage、comparison taint 与 AFL 原生 map | `compiler/Pass.cpp`, `runtime/src/LibcWrappers.cpp`, `util/afl_data_coverage_rt.c` | `data_coverage_libc.c`, `test_afl_data_coverage.py` | I/T |
| F05 | S2F actionseed、PrefixDAG、TACO/MultiGo | `runtime/.../solver.*`, `util/hybrid_feedback.py`, `util/mpi_fuzzing_helper.py` | `s2f_actionseed.c`, `test_hybrid_feedback.py` | I/T |
| F06 | directed coloration、并发引导和结构任务图 | `compiler/Pass.cpp`, `util/merge_directed_distance.py`, `util/structural_tasks.py` | `directed_*.c`, `concurrency_guidance.c`, `test_structural_tasks.py` | I/T |
| F07 | Expressive Coverage Tree 与 minimum path cover | `util/expressive_coverage.py`, `util/path_cover.py` | `test_expressive_coverage.py`, `test_path_cover.py` | I/T |
| F08 | 学习型 seed/worker/策略/参数调度 | `util/hybrid_feedback.py`, `util/adaptive_components.py`, `util/self_config.py` | `test_hybrid_feedback.py`, `test_adaptive_components.py`, `test_self_config.py` | I/T |
| F09 | SMTgazer 式算法序列调度 | `util/smt_algorithm_scheduler.py`, `util/smt_sequence_training.py` | `test_smt_algorithm_scheduler.py`, `test_smt_sequence_training.py` | I/T |
| F10 | 语义 fallback、agentic route 与验证式 proposal | `util/semantic_fallback.py`, `util/agentic_concolic_hooks.py`, `util/verified_proposals.py`, `util/semantic_proposals.py` | 对应 `test_*.py` | I/T |
| F11 | UCSan 式 under-constrained execution | `compiler/UCSan.*`, `runtime/*UCSan*`, `util/ucsan_seed.py` | `ucsan_jit.c`, `test_ucsan_seed.py` | I/T |
| F12 | content-addressed 分布式状态、fenced lease 与 coverage gossip | `util/distributed_state.py`, `util/mpi_fuzzing_helper.py` | `test_distributed_state.py` | I/T |
| F13 | bounded-DPOR 调度探索 | `util/symcc_schedule_rt.c`, `util/schedule_exploration.py` | `test_schedule_exploration.py` | I/T |
| F14 | AFL++ hint mutator 与 profile 编排 | `util/afl_symcc_hint_mutator.py`, `benchmark/run_benchmark.py` | `test_afl_hint_mutator.py`, `test_afl_profile_orchestration.py` | I/T |
| F15 | 干扰感知离线策略评估 | `util/offline_policy.py`, `benchmark/run_benchmark.py` | `test_offline_policy.py` | I/T |
| F16 | benchmark、消融与统计报告 | `benchmark/run_benchmark.py`, `benchmark/analyze_ablation.py` | `test_ablation_analysis.py` | I/T |
| F17 | MPI 并行执行基础与可靠协议 | `util/mpi_concolic_execution.py`, `util/mpi_fuzzing_helper.py` | MPI 集成运行、并行评估报告 | I/T/B |
| F18 | AFL++/SymCC 闭环与吞吐优化 | `util/mpi_fuzzing_helper.py`, `benchmark/profile_bottleneck.py` | hybrid/scaling benchmark、profile 产物 | I/T/B |
| F19 | 可扩展工作分解与资源控制 | `util/mpi_fuzzing_helper.py`, `benchmark/make_afl_targets_persistent.sh` | synthetic/public scaling 运行 | I/T/B |
| F20 | 结构化输入与异构 fuzzing 协同 | `util/grimoire_gen.py`, `benchmark/run_benchmark.py`, QSYM runtime | 固定配置集成实验 | I/T/B |
| F21 | 公开 benchmark 与测量正确性 | `benchmark/run_benchmark.py`, `benchmark/*public*` | 10-target/full benchmark 与报告 | I/T/B |
| F22 | 阶段计时与冗余归因 | `util/mpi_fuzzing_helper.py`, `benchmark/run_benchmark.py` | timing/redundancy benchmark 产物 | I/T/B |
| F23 | 可配置 SymCC/SymSan 引擎抽象 | `util/concolic_engine.py`, `scripts/engine_*dogfight.py` | 微目标、覆盖与 full-hybrid 对拍 | I/T/B |
| F24 | SymSan 技术迁移与 RGD 求解栈 | `scripts/symsan_patches/`, `scripts/build_public_symsan.sh` | 真实目标与 6-target benchmark | I/T/B |
| F25 | 可复现安装与离线交付 | `setup.sh`, `package.sh`, `scripts/build_symsan.sh` | bare-machine/debootstrap 验证 | I/T/B |
| F26 | 跨层正确性审查修复 | solver、SiteId、UCSan、hybrid feedback、distributed state | modular/cache/fast/libc/UCSan/site/fencing 回归 | I/T |
| F27 | 持久化 Query IR、异步 query trie 与增量 Z3 服务 | QSYM `solver.*`, `query_solver.cpp`, `util/query_store.py`, `util/symcc_query_service.py` | `query_ir.c`, `query_prefix_reuse.py`, `test_query_store.py` | I/T |
| F28 | 完整 static Data Coverage 与独立 novelty/dominance | `compiler/Symbolizer.cpp`, `compiler/Pass.cpp`, QSYM `solver.*`, `util/hybrid_feedback.py` | `data_coverage_static.c`, `data_coverage_dso.c`, `test_hybrid_feedback.py` | I/T |
| F29 | reusable solution generator 与 full-matrix AFL mutator | `util/solution_generator.py`, `util/query_store.py`, `util/afl_symcc_hint_mutator.py` | `test_solution_generator.py`, `test_query_store.py`, `test_afl_hint_mutator.py`, `query_generator.py` | I/T |
| F30 | string-constraint artifact 与 offset-aware candidate | `runtime/src/LibcWrappers.cpp`, `util/string_constraints.py`, `util/afl_symcc_hint_mutator.py` | `test_string_constraints.py`, `test_afl_hint_mutator.py`, `string_constraints.c` | I/T |
| F31 | asynchronous solver portfolio 与 disagreement telemetry | `util/query_store.py`, `util/symcc_query_service.py` | `test_query_store.py` | I/T |
| F32 | PSCache-inspired partial solution cache 与 Query IR verified reuse | `util/query_store.py` | `test_query_store.py` | I/T |
| F33 | SymCC-str phase-2 string theory backend 与 MPI/CLI materialization | `util/string_constraints.py`, `runtime/src/backends/qsym/query_solver.cpp`, `util/mpi_fuzzing_helper.py`, `util/symcc_string_solver.py` | `test_string_constraints.py`, `string_solver.py`, CLI smoke | I/T |
| F34 | Bounded parallel solver portfolio racing | `util/query_store.py`, `util/symcc_query_service.py` | `test_query_store.py` | I/T |
| F35 | Solver-helper internal PSCache assignment probing | `runtime/src/backends/qsym/query_solver.cpp`, `util/query_store.py` | `query_solver_pscache.py`, `test_query_store.py` | I/T |
| F36 | Lase/Cottontail-inspired token grammar solve-complete proposals | `util/semantic_proposals.py` | `test_semantic_proposals.py` | I/T |
| F37 | IFSS/Hydra-style targeted transformation proposals | `util/semantic_proposals.py`, `util/verified_proposals.py` | `test_semantic_proposals.py`, `test_verified_proposals.py` | I/T |
| F38 | GenSym-style live continuation checkpoint descriptors | `util/distributed_state.py` | `test_distributed_state.py` | I/T |
| F39 | ConDPOR-style HB/lockset schedule conflict analysis | `util/schedule_exploration.py` | `test_schedule_exploration.py` | I/T |
| F40 | ConDPOR-style memory read/write schedule trace instrumentation | `compiler/Symbolizer.cpp`, `util/symcc_schedule_rt.c`, runtime notification ABI | `schedule_memory_trace.c` | I/T |
| F41 | ConDPOR-style schedule constraint artifact export | `util/schedule_exploration.py`, `util/mpi_fuzzing_helper.py` | `test_schedule_exploration.py` | I/T |
| F42 | Query IR × schedule artifact joint replay validation | `util/query_store.py`, `util/symcc_query_service.py`, `util/mpi_fuzzing_helper.py` | `test_query_store.py` | I/T |
| F43 | Schedule memory provenance filtering | `util/symcc_schedule_rt.c` | `test_schedule_exploration.py` | I/T |
| F44 | Schedule memory provenance tags and artifact propagation | `util/symcc_schedule_rt.c`, `util/schedule_exploration.py`, `util/query_store.py` | `test_schedule_exploration.py`, `test_query_store.py` | I/T |
| F45 | Bounded SC schedule-SMT replay-prefix encoding | `util/schedule_exploration.py`, `util/mpi_fuzzing_helper.py` | `test_schedule_exploration.py`（libz3） | I/T |
| F46 | Mutex/rwlock lifecycle-state schedule-SMT | `util/schedule_exploration.py`, `util/mpi_fuzzing_helper.py` | `test_schedule_exploration.py`（SAT/UNSAT + preload） | I/T |
| F47 | Condition-variable wait/wake operational schedule-SMT | `util/symcc_schedule_rt.c`, `util/schedule_exploration.py` | `test_schedule_exploration.py`（SAT/UNSAT + real pthread preload） | I/T |
| F48 | Thread create/join operational schedule-SMT | `util/symcc_schedule_rt.c`, `util/schedule_exploration.py` | `test_schedule_exploration.py`（SAT/UNSAT + real pthread_exit preload） | I/T |
| F49 | Lifecycle partial-order / linear-extension schedule-SMT v5 | `util/schedule_exploration.py`, `util/mpi_fuzzing_helper.py` | `test_schedule_exploration.py`（partial/permutation differential SAT/UNSAT + strict link） | I/T/B |
| F50 | Scaled-anchor sparse lifecycle order links | `util/schedule_exploration.py` | `test_schedule_exploration.py`（linear-vs-quadratic counts + libz3） | I/T/B |
| F51 | Constructive linear-extension certificate checker | `util/schedule_exploration.py` | `test_schedule_exploration.py`（model/query/choice/tamper checks） | I/T |
| F52 | Verified topology-to-runtime replay materialization | `util/symcc_schedule_linearize.py`, `util/schedule_exploration.py` | CLI + real pthread preload replay | I/T |
| F53 | Violation-driven lazy critical-section refinement | `util/schedule_exploration.py` | exact/relaxed SAT/UNSAT + 64-section ablation | I/T/B |
| F54 | Direct system-libz3 model extraction and solve CLI | `util/schedule_exploration.py`, `util/symcc_schedule_solve.py` | C API model/certificate/CLI/digest tests | I/T |
| F55 | Detach/cancel/join-cancelled identity lifecycle | `util/symcc_schedule_rt.c`, `util/schedule_exploration.py` | real preload detach/cancel + 4104-thread recycling | I/T/B |
| F56 | Bounded Source-DPOR certificate、source set 与 sleep-set persistence | `util/schedule_exploration.py` | equivalence/source/tamper/persistence tests | I/T |
| F57 | Single-context Query IR × schedule × read-from solving | `util/schedule_exploration.py`, `util/query_store.py` | joint SAT/UNSAT、byte bridge、certificate recheck | I/T |
| F58 | Parameterized SC/TSO/RA bounded memory-model constraints | `util/schedule_exploration.py`, `util/mpi_fuzzing_helper.py` | SB、store forwarding、release/acquire litmus | I/T |
| F59 | Content-addressed live state、solver stack 与 page-COW memory | `util/distributed_state.py`, `util/symcc_live_state.py` | fork/restore/diff/tamper/missing-object tests | I/T |
| F60 | Executable research-evidence bundle and claim gates | `util/research_evidence.py` | backend oracle、semantic gate、digest/CLI tests | I/T |
| F61 | Bounded Optimal-DPOR wakeup tree and cooperative ready evidence | `util/schedule_exploration.py`, `util/symcc_schedule_rt.c`, `util/mpi_fuzzing_helper.py` | weak-initial、ready-root、tamper、real pthread replay tests | I/T |
| F62 | Bounded ConDPOR execution graph、backward revisit 与 maximal extension | `util/schedule_exploration.py`, `util/mpi_fuzzing_helper.py` | po/rf/co、cycle/protection、extension、tamper/persistence tests | I/T |
| F63 | Path-dependent branch/action schedule events 与 causal regeneration | compiler schedule instrumentation、`util/symcc_schedule_rt.c`, `util/schedule_exploration.py` | branch/action trace、byte-overlap dependency、suffix deletion tests | I/T |
| F64 | Operational enabledness offers 与可复检证书 | `util/symcc_schedule_rt.c`, `util/schedule_exploration.py` | mutex/rwlock/join/wait offer、terminal/tamper/real replay tests | I/T |
| F65 | Native atomic trace 与扩展 bounded C11 RA 语义 | `compiler/Pass.cpp`, compiler/runtime ABI、`util/schedule_exploration.py` | atomic lit、RMW/release-sequence/fence/SC/race/mixed-size tests | I/T |
| F66 | Executable continuation IR 与 MPI live-state fork/resume | `util/live_continuation.py`, `util/distributed_state.py`, `util/mpi_fuzzing_helper.py` | pause/resume/fork/call/COW/CLI、MPI integration checks | I/T |
| F67 | Sealed randomized research protocol 与配对统计 | `benchmark/research_protocol.py`, `benchmark/analyze_ablation.py`, `benchmark/run_benchmark.py` | protocol/tamper/equal-CPU/execution、paired analysis tests | I/T |
| F68 | Conservative LLVM-to-continuation lowering 与 feasibility pruning | `compiler/ContinuationLowering.*`, `util/llvm_to_continuation.py`, `util/live_continuation.py` | LLVM/C lowering、switch/call/select/交叉 PHI/rejection/solver pruning/CLI tests | I/T |
| F69 | Object-aware static memory continuation lowering | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py`, `util/distributed_state.py` | initializer/GEP/multi-byte/COW/endianness/bounds/readonly/symbolic-address tests | I/T |
| F70 | Explicit-size symbolic input-buffer entry ABI | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py`, `util/mpi_fuzzing_helper.py` | LLVM/C pointer-size lowering、logical bounds、capacity、CAS memory、CLI tests | I/T |
| F71 | Frame-local fixed stack objects 与 resumable lifetime | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | alloca/dominance/reentry/init-marker/inactive/recursive/dynamic rejection tests | I/T |
| F72 | Bounded call-site heap objects 与 alloc/free lifetime | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM/C malloc/free lowering、checkpoint lifetime、reallocation、UAF/double-free/uninitialized/dynamic rejection | I/T |
| F73 | Bounded symbolic offset/alias enumeration 与 finite ITE memory | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM/C dynamic/nested GEP、symbolic load/store、no-wrap index domain、overlap、heap-init checkpoint、alias-contract/limit tests | I/T |
| F74 | Guarded pointer PHI/select provenance union | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM/C select/PHI、cross-object load/store、per-arm dynamic GEP、null pruning、cycle/total-limit/artifact rejection | I/T |
| F75 | Bounded multi-instance heap identity 与 canonical slot reuse | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM/C same-site multi-live、pool exhaustion、checkpoint、malformed pool、GEP-after-union tests | I/T |
| F76 | Bounded cross-function pointer/object references | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM/C pointer arg/return/forwarding、caller-stack checkpoint、heap return、signature tamper、stack escape/domain rejection | I/T |
| F77 | CAS-rooted state-local incremental solver contexts | `runtime/src/backends/qsym/query_solver.cpp`, `util/live_continuation.py`, `util/distributed_state.py`, `util/mpi_fuzzing_helper.py` | exact/parent/cold context、warm/cold resume、one-shot ablation、legacy server compatibility | I/T |
| F78 | Nullable runtime-sized allocator 与 conditional heap state | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM/C dynamic malloc/calloc/realloc、OOM、logical bounds、conditional lifetime/init、checkpoint tests | I/T |
| F79 | Caller-domain symbolic pointer certificate | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM/C symbolic-GEP argument、hidden domain parameter、pointer-return domain、artifact tamper/false-domain pruning tests | I/T |
| F80 | Bounded indirect-call points-to dispatch 与 typed continuation ABI | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM select/PHI、真实 C function pointer、symbolic target fork、ID/signature tamper tests | I/T |
| F81 | Deterministic external memory-compare effect summary | `compiler/ContinuationLowering.cpp`, existing continuation load/BV IR | LLVM/C memcmp、lexicographic sign、zero/null、pointer union、length-bound rejection tests | I/T |
| F82 | Bounded external region-write effect summaries | `compiler/ContinuationLowering.cpp`, existing continuation load/store/BV IR | LLVM intrinsic 与 C libc memcpy/memmove/memset、overlap snapshot、symbolic fill、zero/null、disjointness rejection tests | I/T |
| F83 | Pointer-returning bounded indirect dispatch | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM/C indirect pointer argument/return、target fork、return-domain destination/signature tamper tests | I/T |
| F84 | Guarded memory access 与 bounded NUL-aware string summaries | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM/C strlen/strcmp/strncmp、short-object union、logical input bound、zero/null、symbolic-length/guard tamper tests | I/T |
| F85 | Bounded pointer-returning memory/string search summaries | `compiler/ContinuationLowering.cpp`, existing guarded/core memory IR | LLVM/C memchr/strchr、symbolic needle、first match、zero/null、short union、extent rejection tests | I/T |
| F86 | Guarded memory write 与 bounded string-copy summaries | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM/C strcpy/strncpy、snapshot/terminator/padding/return provenance、source union、zero/null、overlap/symbolic-length/guard tamper tests | I/T |
| F87 | LLVM defined-value guards for integer UB/poison conditions | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM/C div/rem、symbolic shift、nuw/nsw/exact SAT/UNSAT、freeze-poison rejection、artifact tamper tests | I/T |
| F88 | Bounded pointer-valued memory 与 finite provenance sidecar | `compiler/ContinuationLowering.cpp`, existing page-COW memory/provenance guards | LLVM/C global/stack/heap pointer cells、GEP/null、conditional/overwrite/function-pointer rejection tests | I/T |
| F89 | Scalar function-pointer memory 与 typed target recovery | `compiler/ContinuationLowering.cpp`, existing indirect-call ABI/runtime | LLVM/C global/stack function-pointer cells、single/multi-target dispatch、signature checks | I/T |
| F90 | Bounded global pointer/function-pointer tables | `compiler/ContinuationLowering.cpp`, existing static initializer and finite-provenance lowering | LLVM/C symbolic table index、data/function target dispatch、loaded-ID/address guards | I/T |
| F91 | Direct-predecessor pointer-memory merge | `compiler/ContinuationLowering.cpp`, existing PHI edge/provenance machinery | LLVM data/function-pointer conditional merge、incomplete/mixed reaching-definition rejection | I/T |
| F92 | Proven-`nounwind` invoke normal-edge lowering | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | direct/indirect LLVM invoke、PHI edge、dead landingpad pruning、may-unwind/tamper rejection | I/T |
| F93 | Stable dynamic nondeterministic freeze choice | `compiler/ContinuationLowering.cpp`, `util/live_continuation.py` | LLVM poison/undef fork、loop instance、checkpoint resume、site/token tamper tests | I/T |
| F94 | Bounded acyclic pointer-memory SSA closure | `compiler/ContinuationLowering.cpp` | nested/default/forwarding CFG merge、function-target reuse、cycle/missing/overwrite rejection | I/T |
| F95 | Deterministic scalar external/intrinsic summaries | `compiler/ContinuationLowering.cpp`, existing core BV IR | LLVM/C abs、hton/ntoh、llvm.bswap、INT_MIN与ABI rejection tests | I/T |
| F96 | Bounded bit-count intrinsic summaries | `compiler/ContinuationLowering.cpp`, existing core BV IR | LLVM/C ctpop、LLVM ctlz/cttz、zero semantics与zero-poison infeasibility tests | I/T |
| F97 | Direct deferred-poison freeze | `compiler/ContinuationLowering.cpp`, stable nondet core | LLVM overflowing/defined freeze、multi-consumer fallback tests | I/T |
| F98 | Bounded cyclic pointer-memory SSA fixed point | `compiler/ContinuationLowering.cpp` | loop-invariant/loop-carried writer execution、uninitialized-cycle rejection | I/T |
| F99 | Canonical constant-GEP pointer-cell identity | `compiler/ContinuationLowering.cpp` | equivalent nonzero GEP与distinct-offset interference tests | I/T |
| F100 | Bounded bit-permutation/funnel-shift intrinsics | `compiler/ContinuationLowering.cpp`, core BV IR | symbolic bitreverse、mod-width fshl/fshr LangRef examples | I/T |
| F101 | Bounded saturating add/sub intrinsics | `compiler/ContinuationLowering.cpp`, core BV IR | packed signed/unsigned extrema与symbolic saturation tests | I/T |
| F102 | Bounded saturating shift intrinsics | `compiler/ContinuationLowering.cpp`, definedness guards | signed/unsigned clamp与out-of-range poison tests | I/T |
| F103 | Bounded scalar abs/min/max intrinsics | `compiler/ContinuationLowering.cpp`, core BV IR | packed extrema、symbolic selection与INT_MIN poison tests | I/T |
| F104 | LLVM branch-hint identity intrinsics | `compiler/ContinuationLowering.cpp` | symbolic expect/expect.with.probability chain | I/T |
| F105 | Bounded static objectsize intrinsic | `compiler/ContinuationLowering.cpp`, pointer provenance | stack/global/union/null results与dynamic-input rejection | I/T |
| F106 | Bounded dynamic objectsize intrinsic | `compiler/ContinuationLowering.cpp`, runtime logical-size provenance | input interior、dynamic malloc与static-unknown execution tests | I/T |
| F107 | Bounded overflow arithmetic aggregates | `compiler/ContinuationLowering.cpp`, core BV IR | six add/sub/mul variants、symbolic flag、INT_MIN edge tests | I/T |
| F108 | Integer/data/function-pointer `ssa.copy` | `compiler/ContinuationLowering.cpp`, finite provenance | symbolic integer、pointer union与typed indirect dispatch tests | I/T |
| F109 | Dynamic realloc objectsize | `compiler/ContinuationLowering.cpp`, per-alternative in-place realloc provenance | shrink success、growth failure/null与realloc/global union tests | I/T |
| F110 | Transitive deferred poison chain | `compiler/ContinuationLowering.cpp`, stable nondet freeze | cast/BV/icmp/ssa.copy poison与defined tests | I/T |
| F111 | Totalized deferred division freeze | `compiler/ContinuationLowering.cpp`, safe-divisor IR | div-zero、sdiv overflow、exact与defined division tests | I/T |
| F112 | Path-sensitive select poison | `compiler/ContinuationLowering.cpp`, definedness sidecar | selected/unselected arm与poison-condition tests | I/T |
| F113 | PHI edge-definedness merge | `compiler/ContinuationLowering.cpp`, two-phase edge copies | selected/unselected predecessor poison tests | I/T |
| F114 | Straight-line memory poison sidecar | `compiler/ContinuationLowering.cpp`, exact store/load pair | poison/defined与multi-load fallback tests | I/T |
| F115 | Canonical-address memory poison | `compiler/ContinuationLowering.cpp`, DataLayout base+offset proof | equivalent GEP positive与distinct-offset negative tests | I/T |
| F116 | Linear-CFG memory poison | `compiler/ContinuationLowering.cpp`, bounded unique-edge walk | cross-block positive；merge/byte lanes由F124--F149扩展 | I/T |
| F117 | Direct-call return poison ABI | compiler + continuation validator/executor | poison/defined/multi-call与malformed endpoint tests | I/T |
| F118 | Direct-call argument poison ABI | compiler + typed call validator | poison/defined/multi-call与malformed endpoint tests | I/T |
| F119 | Bounded multi-callsite poison ABI | compiler direct-callsite proof | return/argument positive与mixed-sink rejection tests | I/T |
| F120 | Symbolic pointer-memory + initial definition merge | pointer provenance/reaching definitions | dynamic GEP、overwrite与initializer fallback tests | I/T |
| F121 | Transitive argument-to-return poison | composed defined-bit call ABI | BV passthrough poison/defined multi-call test | I/T |
| F122 | Bounded multi-consumer deferred poison | all-use freeze-sink proof | two-freeze positive与mixed-consumer negative tests | I/T |
| F123 | Bounded multi-load memory poison | finite linear memory version | two-load freeze、clobber与mixed-consumer tests | I/T |
| F124 | Acyclic branch memory poison | all-predecessor common-store proof | common-store diamond test | I/T |
| F125 | Path-dependent memory definedness PHI | edge-defined sidecar contract | poison/defined stores与tamper rejection | I/T |
| F126 | Interprocedural memory definedness PHI | call ABI/edge-PHI composition | argument/return双向`1,2,2` tests | I/T |
| F127 | Multilevel memory definedness PHI | bounded predecessor forwarding proof | nested forwarding `1,2,2` test | I/T |
| F128 | Cyclic memory definedness PHI | loop-edge sidecar state | two-iteration positive与missing-store negative | I/T |
| F129 | Initial memory definedness merge | global initializer edge state | poison-store/no-store `1,2,2` test | I/T |
| F130 | Initial subobject definedness merge | DataLayout constant-folded aggregate cell | constant-GEP poison/initial `1,2,2`与tamper test | I/T |
| F131 | Cyclic no-write definedness carry | loop edge sidecar self-carry | two-iteration 1/2、mixed store/carry negative与tamper test | I/T |
| F132 | Conditional store/carry transfer | canonical diamond i1 select edge transfer | symbolic store/carry `1,2,2`、forwarded negative与tamper test | I/T |
| F133 | Forwarded conditional carry | common-split ancestor-chain proof | forwarded store/carry `1,2,2`、multi-branch negative与tamper test | I/T |
| F134 | Multi-arm equivalent-source carry | reaching-source groups + all-endpoint arm proof | three-way `1,2,2,2`、distinct-store negative与tamper test | I/T |
| F135 | Equivalent defined-store carry | poison-free writer equivalence class | distinct constants `1,2,2,2`、mixed poison negative与tamper test | I/T |
| F136 | Shared-poison writer carry | shared SSA condition + writer-membership set | shared poison `1,1,2,2,2`、distinct-producer negative与tamper test | I/T |
| F137 | Nested conditional writer carry | two-level branch tree + eager-dominance proof | distinct poison `1,1,2,2,2`、four-source negative与tamper test | I/T |
| F138 | Bounded recursive writer/carry tree | flat proof IR + recursive CFG partition + postorder edge selects | depth-3 `1,1,1,2,2,2,2`、eager negative与tamper test | I/T |
| F139 | Grouped recursive source tree | all-endpoint same-arm proof per source group | grouped depth-3 nine-state execution、hierarchy tamper | I/T |
| F140 | Repeated-source recursive tree | conflicting-position detection + bounded leaf duplication | depth-4 five-leaf nine-state execution、hierarchy tamper | I/T |
| F141 | Multi-carry recursive tree | repeated carry leaves + exact artifact carry count | depth-4 five-leaf eight-state execution、count tamper | I/T |
| F142 | Disjoint multi-cell definedness PHI | constant region disjointness + independent edge sidecars | aggregate subcells `1,1,2`、same-cell negative与tamper | I/T |
| F143 | Identified-object multi-cell definedness PHI | distinct static object proof + independent edge sidecars | two stack objects `1,1,2`、same-object hierarchy与tamper | I/T |
| F144 | Fixed-heap-object multi-cell definedness PHI | distinct bounded malloc-site proof + independent sidecars | two heap sites `1,1,2`、static hierarchy与tamper | I/T |
| F145 | Finite pointer-domain multi-cell definedness PHI | bounded select/PHI region product proof | disjoint 2x2 domain `1,1,2`、overlap rejection与tamper | I/T |
| F146 | Guard-correlated pointer-domain multi-cell PHI | select-guard relational compatibility proof | mutually exclusive overlap `1,1,2`、compatible-overlap rejection | I/T |
| F147 | PHI-correlated pointer-domain multi-cell PHI | merge/predecessor edge relation proof | edge-exclusive overlap `1,1,2`、same-edge rejection | I/T |
| F148 | Symbolic-index interval multi-cell PHI | LLVM ConstantRange + region interval product | separated subarrays `1,1,2`、overlap rejection | I/T |
| F149 | Byte-lane memory definedness composition | bounded reverse MemorySSA + per-address-byte nearest writer | split/initial/overlap composition、branch-merge rejection、lane/capability tamper | I/T |
| F150 | Direct-predecessor byte-lane definedness PHI | per-edge lane vectors + edge-local sidecar reduction | path-dependent/initial lane PHIs、upstream-join rejection、tamper | I/T |
| F151 | Cyclic byte-lane definedness PHI | per-lane loop-carried state + alias-closed fixed point | entry/backedge transfer、alias negative、edge/capability tamper | I/T |
| F152 | Conditional cyclic byte-lane carry | strict diamond proof + arm-edge lane updates | symbolic write/skip `1,1,2`、alias negative、topology tamper | I/T |
| F153 | Forwarded conditional cyclic byte-lane carry | bounded linear arm corridors + graph reconstruction | forwarded `1,1,2`、nested-branch rejection、corridor tamper | I/T |
| F154 | Multi-arm conditional cyclic byte-lane carry | strict two-level branch tree + three edge-local lane transfers | low/high/carry `1,1,1,2,2`、four-arm rejection、route tamper | I/T |
| F155 | Forwarded multi-arm cyclic byte-lane carry | three bounded arm corridors + two-level tree reconstruction | forwarded `1,1,1,2,2`、nested-corridor rejection、successor tamper | I/T |
| F156 | Recursive conditional cyclic byte-lane carry | bounded strict binary source tree + leaf-local lane transfers | four-leaf `1,1,1,1,2,2,2`、shared-leaf rejection、depth tamper | I/T |
| F157 | Forwarded recursive cyclic byte-lane carry | bounded branch-to-child corridors + recursive graph reconstruction | forwarded four-leaf execution、nested-corridor rejection、successor tamper | I/T |
| F158 | Grouped recursive cyclic byte-lane source | dominating internal writer + exact descendant-leaf group | grouped four-leaf execution、two-group rejection、marker tamper | I/T |
| F159 | Repeated-source recursive cyclic byte-lane transfer | internal source with same-range leaf overrides | five-leaf nine-state execution、mismatched-override rejection、marker tamper | I/T |
| F160 | Composed repeated-source recursive byte-lane transfer | per-lane nearest internal/override/carry composition | mixed-source leaf nine-state execution、full-shadow rejection、marker tamper | I/T |
| F161 | Multi-carry composed recursive byte-lane tree | exact all-carry leaf count + unique-parent proof | six-leaf ten-state execution、shared-carry rejection、count tamper | I/T |
| F162 | Multiple internal recursive byte-lane groups | two disjoint internal writers + exact descendant partitions | dual-group nine-state execution、nested-group rejection、count tamper | I/T |
| F163 | Mixed multi-group recursive byte-lane composition | one composed group + one pure group | mixed six-leaf eleven-state execution、full-shadow rejection、count tamper | I/T |
| F164 | Forwarded grouped recursive byte-lane source | grouped dominance across bounded corridors | forwarded grouped seven-state execution、nested-corridor rejection、marker tamper | I/T |
| F165 | Forwarded repeated-source recursive byte-lane transfer | same-range override proof across bounded corridors | forwarded five-leaf nine-state execution、branching-corridor rejection、dual-marker tamper | I/T |
| F166 | Forwarded composed repeated-source byte-lane transfer | per-lane nearest-source proof across bounded corridors | forwarded composed nine-state execution、branching-corridor rejection、dual-marker tamper | I/T |
| F167 | Forwarded multi-group recursive byte-lane tree | two disjoint group partitions across bounded corridors | forwarded dual-group nine-state execution、branching-corridor rejection、marker/count tamper | I/T |
| F168 | Forwarded mixed multi-group byte-lane composition | one composed group across bounded corridors | forwarded mixed eleven-state execution、branching-corridor rejection、marker/count tamper | I/T |
| F169 | Forwarded multi-carry composed byte-lane tree | exact carry-leaf count across bounded corridors | forwarded six-leaf ten-state execution、branching-corridor rejection、marker/count tamper | I/T |
| F170 | Three-group recursive byte-lane tree | three disjoint complete descendant partitions | seven-leaf thirteen-state execution、nested-group rejection、marker/count tamper | I/T |
| F171 | Double-composed multi-group byte-lane tree | one partial override in each of two groups | seven-leaf thirteen-state execution、same-group override rejection、marker/count tamper | I/T |
| F172 | Forwarded double-composed multi-group tree | two per-group source differences across bounded corridors | forwarded seven-leaf thirteen-state execution、branching-corridor rejection、dual-marker tamper | I/T |

后续章节按此 ID 记录方案、实现细节、结果与边界。ID 表示进入统一总账的顺序，
不表示实现时间；历史时间线与提交边界见
[`Development_History_Traceability.md`](Development_History_Traceability.md)。

### 3.1 F00：编译与 runtime 基础正确性

这些改动不直接构成调度算法，但决定新增并行能力是否具备正确执行基础：

- `inttoptr`/`ptrtoint` 的 symbolic expression 按 LLVM `DataLayout` 指针宽度执行
  zero-extend 或 truncate，避免 32/64/128 位转换产生非法 extract；
- `pointer_width_casts.ll` 同时覆盖窄整数到 64 位指针、指针到 i128 和指针到 i16；
- runtime 的函数参数 expression 槽和 return expression 改为 `thread_local`，避免
  多线程目标的插桩调用互相污染；
- test-case handler 新增显式 `saveConcreteValues` 路径，使 sampling、Backsolver 和
  cache replay 能保存已经验证的候选，而不被当前 concrete input 覆盖；
- 新增 Python 测试作为 lit suffix，并按 backend feature gate 运行 QSYM 专属集成测试。

结果等级为 I/T：指针宽度测试与既有数组、整数、浮点、内存、字符串、函数调用
回归共同通过。线程局部状态后续仍需在线程密集 benchmark 上测量 runtime 开销。

## 4. F01：统一执行遥测与后端抽象

### 4.1 方案

并行调度不能只观察“是否生成新输入”。系统为每次执行建立统一
`SolverTelemetry`，同时描述路径、求解成本、数据依赖、目标到达和进程状态。
SymCC 与 SymSan 的原始结果均在 worker 侧归一化，调度器不依赖特定后端内部对象。

### 4.2 实现细节

- QSYM runtime 原子写出 versioned JSON，避免 coordinator 读取半文件；
- 记录 `engine`、`solver_algorithm`、`capabilities` 和 `missing_fields`，允许部分后端
  显式报告能力缺失；
- 记录 signed return code、timeout、killed、elapsed、generated，避免信号退出被
  错误转换为无符号值；
- 路径层包括 `path_hash`、`open_branches` 和 bounded `branch_trace`；
- 求解层包括 SAT/UNSAT/unknown、时间、依赖字节、Backsolver、poly cache、
  prefix context 和 UNSAT core 统计；
- 数据层包括 `data_features` 与 `comparison_taints`；
- `ExecutorPortfolio` 将 `exact`、`tailored`、`sampling` 映射到 SymCC 或 SymSan
  命令、超时和受限环境变量。

### 4.3 结果与证据

- `test/telemetry.c` 验证 schema、路径指纹、目标 replay、data feature 和核心求解
  统计字段；
- `test/test_hybrid_feedback.py` 覆盖 39 个反馈、解析和调度边界；
- `test/test_executor_portfolio.py` 验证执行器路由、后端选择与配置清洗；
- 当前结论为 schema 和调度接口可用；公开套件上的 SymCC/SymSan 等价性和开销
  对照仍待 R 级实验。

## 5. F02：Backsolver 与 bounded Veritesting

### 5.1 方案

具体执行在前序分支上作出的选择，可能让包含隐式流的后续目标失去可满足解。
编译器将可证明安全的控制依赖值恢复成 ITE，runtime 先尝试轻量反向候选，再在
必要时对不兼容前缀做受限适配求解。

### 5.2 编译器实现

- 识别 `select`、规范 two-arm diamond 和两输入 PHI；
- 支持 bounded multi-block、无环、nested PHI/ITE easy region；
- 克隆整数、浮点、指针标量的纯 SSA 算术、比较、cast 和 select；
- region 最大递归深度 12、每臂最多 32 个 basic block；
- 通过 DominatorTree/PostDominatorTree 验证控制合流；
- 通过 MemorySSA 与 AliasAnalysis 接受只读 load snapshot：只有在区域内无干扰
  写、地址不逃逸、alias 可证明且公式规模受控时才合成；
- call、写内存、模糊 alias、循环、非规范 merge、division/remainder 和不安全
  region 保守回退。

### 5.3 runtime 实现

- 收集 ITE controller，区分目标依赖和可保留前缀；
- 对可逆谓词直接修改候选字节并用表达式求值器验证；
- 直接候选失败后进入 Z3 fallback；
- telemetry 分别记录 direct attempt、validation failure、kept/dropped constraint
  和 Z3 fallback，便于消融。

### 5.4 结果与边界

- `backsolver_select.ll`、`backsolver_symbolic_phi.ll`、
  `backsolver_veritesting_region.ll`、`backsolver_nested_veritesting.ll` 和
  `backsolver_implicit_flow.ll` 验证 ITE 主路径；
- `backsolver_selective_prefix.ll` 验证适配查询只丢弃与 ITE controller 依赖重叠的
  prefix constraint；
- `backsolver_memory_snapshot.ll` 验证可证明只读内存；
- `veritesting_memory_reject.ll` 验证存在写入干扰时拒绝合成；
- 尚未实现完整 multi-exit、loop summary 以及 LLVM poison/undef/freeze 的显式模型。

## 6. F03：分层求解与约束上下文复用

### 6.1 方案

求解路径按成本由低到高组织为 fast、optimistic、polyhedral sampling 和 exact Z3。
相似 prefix 不重复翻译全部约束；已知模型、线性区域与不可满足子集可跨执行复用。

### 6.2 实现细节

- Fuzzy-Sat 风格 fast path 处理简单字节和 bounded concat 比较；
- optimistic-first 与 multi-solve 为相关分支提供低成本先验尝试；
- expression hash-cons cache 有固定容量并在逐出时保持引用安全；
- density-only profile 统计每个输入 offset 参与 interesting branch 的次数；
- BSFuzz 风格 timeout site 可写出并由 coordinator 聚合，其他 worker 在后续执行中
  跳过已知高成本 site；
- prefix key 由路径、目标表达式与依赖上下文构成；
- poly cache 保存 SAT/UNSAT/timeout、模型修改、byte box 和规范化线性约束；
- prefix context cache 保存翻译后的 Z3 assertion vector；
- template extraction 计算单字节、字节对和已知线性表达式的严格 min/max；
- John/Dikin 模式构造 dense `A x <= b`，用 barrier Hessian、Cholesky 和
  Lewis/leverage 近似产生相关方向；
- 奇异矩阵或边界退化时回退 integer hit-and-run/coordinate sampling；
- 每个离散样本再次检查精确整数不等式；
- UNSAT cache 保存 tracked core，经有界最小化后同时生成 exact 与 structural
  fingerprint；结构匹配允许输入索引重命名；
- 线性约束在进入 Z3 前进行 contradiction/subsumption 剪枝。

### 6.3 结果与边界

- `poly_cache.c` 验证 template 约束、dense John walk、持久化 cache 和第二次执行
  命中并生成候选；
- `unsat_core_cache.c` 验证 core 插入、命中、结构统一和线性剪枝；
- 已实现的是有界整数/线性多面体复用，不是通用非线性 polyhedral solver；
- 性能收益需按 cache hit、solver time、accepted candidates/CPU-hour 做独立消融。

## 7. F04：data coverage、comparison taint 与 AFL 原生反馈

### 7.1 方案

边覆盖无法表达“常量比较已经匹配了多少内容”。系统同时提供符号执行侧的细粒度
数据进度和 AFL++ 原生 bitmap 反馈，让解析进展参与 corpus retention 与调度。

### 7.2 SymCC 侧实现

- LLVM pass 对整数常量比较和 switch case 发出 data comparison；
- libc wrapper 覆盖 `memcmp`、`bcmp`、bounded `strcmp`/`strncmp`；
- 每个 site/constant 保存最高 matched-prefix bits；
- comparison taint 记录 branch、依赖字节数、min/max offset、outcome 和
  interesting 标记；
- concrete token 经长度限制和内容去重写入 hint 目录；
- coordinator 用 feature dominance 更新调度状态，并生成 sparse
  `.compact_focus_set`。

### 7.3 AFL++ 侧实现

- `afl_data_coverage_rt.c` 通过 `AFL_PRELOAD` 拦截内存/字符串比较；
- 优先使用 `__afl_area_ptr`，否则连接 `__AFL_SHM_ID`；
- 每增加一个匹配前缀，写入一个确定性的 AFL map slot；
- 复用 AFL 自身 queue retention，不建立无界的独立 data corpus；
- `afl_symcc_hint_mutator.py` 消费 string token、poly model 和 byte box，轮换执行
  token overwrite/insert、model patch、box sampling、splice 和 bounded havoc。

### 7.4 结果与边界

- `data_coverage_libc.c` 验证多个 libc 比较产生 data feature、map update 和
  `MAGIC`/`MASON` token；
- `test_afl_data_coverage.py` 验证原生共享 map 的前缀单调反馈；
- `test_afl_hint_mutator.py` 验证 mutator 可消费提示并产生有效变异；
- 原生 map 存在哈希冲突，属于 AFL bitmap 的预期近似；真实覆盖收益需在
  structured parser 上做消融。

## 8. F05：PrefixDAG、S2F、TACO 与 MultiGo

### 8.1 方案

调度单位从“一个 seed 文件”提升为“seed + prefix + target + action plan”。
运行时暴露尚未覆盖的 opposite outcome，coordinator 可跨执行重放高价值分支。

### 8.2 实现细节

- prefix-sensitive branch id 由父 prefix、site 和 outcome 构成；
- `PrefixDAG` 聚合访问次数、reward、solver cost、distance、difficulty 和 cooldown；
- actionseed 支持 `solve`、`sample`、`skip`；
- exact replay 可绕过普通 coverage pruning 并要求匹配 target branch；
- S2F high/low queue 将困难但高收益分支交给 sampling；
- TACO target-centric score 融合目标距离、under-exploration 和 extended
  path conditions；
- MultiGo 使用 site frequency 与 Poisson-style difficulty，在易路径利用与困难
  路径探索之间切换；
- edge-dependence、dynamic coloration 和 Bellman-style backup 作为软优先级，
  hard directed prune 保持 opt-in。

### 8.3 结果与边界

- `s2f_actionseed.c` 通过真实 runtime 验证 solve/sample/skip 动作；
- `test_hybrid_feedback.py` 覆盖 DAG、目标 replay、S2F、TACO/MultiGo、coloration
  与调度更新；
- 当前 actionseed 是跨进程 continuation 的工程表示，不保存完整 symbolic heap
  或 Z3 solver stack。

## 9. F06/F07：结构化覆盖与目标引导

### 9.1 编译期结构图

- `SYMCC_COLOR_TARGETS` 支持函数名、`file:line` 和 runtime site id；
- 构建模块 CFG、直接调用边和 bounded compatible indirect-call 过近似；
- 从 target 反向 BFS 计算 site distance；
- 多模块 sidecar 由 `merge_directed_distance.py` 合并；
- 并发图标记 thread lifecycle、mutex/rwlock/cond/barrier/semaphore、atomic 和 fence；
- task graph 输出 entry、site、branch、call 与 CFG 边；
- 静态依赖摘要把 branch site 映射到输入 byte interval，零额外目标执行即可产生
  replay focus。

### 9.2 动态结构任务

- `ProgramTaskGraph` 将函数图压缩为 bounded structural regions；
- `DynamicStructuralTaskAllocator` 按 worker 反馈学习 region ownership 并定期再平衡；
- `MinimumPathCoverPlanner` 在函数 CFG 上生成多组最小路径覆盖，避免只追踪单一
  shortest path；
- `ExpressiveCoverageTree` 将 observed/open outcome、function context、opcode、
  dependency shape、data quality、solver outcome 和 seed provenance 统一成树；
- constraint-shape key 不含绝对 byte offset，以合并结构相同但位置不同的 parser
  constraint；
- repeated shape 采用 loop counter 压缩，并在节点上限下优先保留 active
  open/timeout 节点。

### 9.3 结果与边界

- `directed_coloration.c`、`directed_coloration_indirect.c` 和
  `directed_coloration_multimodule.c` 验证直接、间接和多模块 distance；
- `directed_prune.c` 验证 runtime 按距离跳过求解；
- `concurrency_guidance.c` 验证并发 site sidecar；
- `static_dependencies.c` 验证 branch-to-byte 静态摘要；
- `test_structural_tasks.py`、`test_path_cover.py` 和
  `test_expressive_coverage.py` 验证动态分配、路径覆盖和 ECT persistence；
- 当前 function context 是有界近似，尚未保存精确 call/return stack 或表达式
  operator skeleton。

## 10. F08/F09：学习型并行调度与 SMT 算法序列

### 10.1 seed-worker 与策略调度

- seed context 包括输入大小、依赖密度、路径稀有度、data quality、目标距离和历史收益；
- worker context 包括本地路径/site 获取、成本、region affinity 和在途重叠；
- SimiFuzz 风格 LinUCB 对 seed-worker pair 打分，并按 time slice 聚合反馈；
- `StrategyPortfolio` 在 exact/tailored/sampling 等粗粒度 profile 间选择；
- `ComponentPortfolio` 分别学习 solver、focus、sampling 等组件臂；
- `AdaptiveParallelismController` 用带迟滞的 hill climbing 调整 active symbolic
  worker 数，避免频繁抖动；
- `SelfConfiguringPolicy` 从版本化工具schema、profile与自定义空间发现参数，使用
  conditional/context/interaction posterior sampling选择受限override，并可围绕
  有效数值动态扩展搜索空间；F186进一步记录其归因与迁移边界；
- K-Scheduler 风格 frontier score 使用 bounded showmap cache 统计稀有边权重；
- worker diversity 可把大输入划分为不相交 focus partition，并利用 density profile
  把热点字节隔离到较窄分区；
- TACE 两阶段路径先执行 bounded no-solve dependency profile，再只符号化得到的
  sparse focus set；profile 失败、目标 replay 或预分区任务均回退/绕过以保持完整性；
- 所有异步 assignment 都携带 token，完成后准确归因 reward/cost。

### 10.2 SMTgazer 式算法序列

默认序列为：

| 序列 | 阶段 |
| --- | --- |
| `exact` | Z3 exact |
| `fast-exact` | fast linear，然后 exact fallback |
| `optimistic-layered` | optimistic multi-solve，然后带 cache 的 layered Z3 |
| `polyhedral-exact` | polyhedral sampling，然后 exact fallback |

调度器实现：

- 将 input size、difficulty、timeout ratio、unknown ratio、dependency density、
  data quality、targeted 状态等压缩为 9 维 context；
- 低开销在线moment split维护有界context cluster；F239另以离线有界X-means和
  spherical-Gaussian BIC决定可复现的局部二分；
- 每个 cluster/sequence 维护 Bayesian arm；
- Thompson-style sample 平衡探索与利用；
- 同一 seed 后续 replay 推进到下一 algorithm stage；
- timeout 使用 censored PAR-2 cost；
- 每次动作记录真实 propensity，供离线策略评估；
- state 原子持久化，算法空间可由受限 JSON 覆盖；
- F239从轨迹生成带canonical SHA-256的context-specific sequence prior，在线端只把
  通过独立复算的建议作为90/10探索偏好，显式在线门控建议优先。

### 10.3 结果与边界

- `test_adaptive_components.py`、`test_self_config.py` 和
  `test_hybrid_feedback.py` 验证在线选择、状态恢复和归因；
- `test_tace_profile.py` 验证 dependency-density parser 的边界、去重和失败回退；
- `test_smt_algorithm_scheduler.py` 验证 cluster、序列推进、持久化和 propensity；
- `test_smt_sequence_training.py`验证BIC分裂/拒绝、context-specific序列、PAR-2
  删失、支持度门槛、摘要与重哈希篡改拒绝、CLI以及保守在线接入；
- 当前在线Bayesian arm仍是轻量近似，F239也不是SMTgazer完整boosting/bagging
  surrogate；是否优于每个benchmark的最佳静态oracle必须通过同CPU离线估计和在线
  A/B共同验证。

## 11. F10/F15：语义候选、agentic route 与保守离线学习

### 11.1 语义 fallback

`SemanticFallbackPlanner` 将 telemetry 分类为 byte-local、token-progress、
wide/nonlinear、timeout-prone 和 opaque，再映射到：

- exact/tailored/sampling/skip executor；
- target branch；
- bounded focus range；
- S2F actionseed。

分类器是确定性的，可独立消融，不依赖外部模型。

### 11.2 agentic route

- task JSONL 只导出受限路径、依赖、目标和成本摘要；
- 外部 hint 只能修改 allowlist 中的 route、focus、target、strategy 和 action；
- command/HTTP/chat-completions backend 在 MPI 热循环之外异步运行；
- 每个 provider 有 timeout、并发上限、circuit breaker 和 canonical task cache；
- 内置 `cottontail`、`concollmic`、`gordian`、`hybrid` route 保持确定性。

### 11.3 semantic proposal

内置 `SemanticProposalGenerator` 实现：

- 从 comparison taint 提取 bounded constraint core；
- 为位运算、加减、大小端常量关系生成 inverse patch；
- 从 data coverage 高质量 exemplar 和 token 文件迁移 surrogate candidate；
- 从 IFSS-like relevance slice 构造 Hydra-style core copy/swap、boundary perturbation
  和 bounded truncate 的 `targeted_transform` candidate；
- 从 AFL extras/string token 与历史 comparison-core fragment 合成 bounded token grammar，
  生成 `solve_complete` proposal；
- 对 UCSan seed 的 alias class 做 split/compatible merge。

`VerifiedProposalManager` 只接受 bounded data transformation，不执行 proposal 中的
代码。候选按内容寻址落盘，经过：

```text
materialize -> concrete target replay -> target telemetry check
            -> authoritative AFL novelty -> retain/reject
```

### 11.4 离线策略与干扰建模

- 每个算法 assignment 记录 action、propensity、context、reward、cost、coverage、
  generated、interesting、concurrent workers 和 killed；
- 干扰惩罚只在“高并发且 generated 中重复比例高”时增长，不惩罚有效并行；
- evaluator 提供 clipped importance weight、SNIPS、doubly robust、effective sample
  size、standard error 和 lower confidence bound；
- `ConservativePolicyGate` 只有在有效样本足够且 lower-bound improvement 超过阈值
  时才批准 preferred sequence；
- CLI 可从任意 trajectory JSONL 重建报告，benchmark JSON/CSV 保存轨迹和 scheduler
  state 摘要。

### 11.5 结果与边界

- `test_semantic_fallback.py` 验证类别到执行动作的映射；
- `test_agentic_concolic_hooks.py` 验证 hint 清洗、异步 provider、route 和失败恢复；
- `test_semantic_proposals.py` 验证 inverse、exemplar、targeted transform 和 UCSan
  partition proposal；
- `test_verified_proposals.py` 验证 content-addressed materialization 与验证状态；
- `test_offline_policy.py` 验证干扰 reward、轨迹恢复、保守门控和 CLI；
- NeuroSCA 风格训练模型尚未作为默认后端接入；若以后接入，仍必须保持 proposal
  与 verifier 的边界。

## 12. F11：UCSan 式 under-constrained execution

### 12.1 方案

当完整程序入口难以到达深层函数时，由编译器为指定函数生成 harness，并用结构化
seed 表示 root、对象、alias 和对象路径。pseudo pointer 与 shadow metadata 将对象图
恢复推迟到真正解引用时。

### 12.2 编译器与 runtime

- `compiler/UCSan.cpp` 解析受限 YAML，选择 entry、scope、external policy 和 wrapper；
- 原 `main` 被重命名，生成的新 harness 从结构 seed 物化参数；
- scoped load/store/GEP/memcpy/call 维护 pointer shadow；
- JIT provisioning 支持对象下界、container-of 风格负 offset 和有界扩展；
- external policy 包括 `pure`、`arbitrary`、`passthrough`；
- runtime 可 dump 实际访问到的 root/object graph；
- root 与 object bytes 可在原 seed offset 上符号化。

### 12.3 图规范化与学习

`ucsan_seed.py` 提供：

- stable binary serialization 与 parser；
- canonical object path；
- union-find alias class；
- payload 冲突和 cycle rejection；
- 从多份 concrete dump 学习稳定 path、payload 和 alias；
- `create`、`inspect`、`normalize`、`learn` CLI。

### 12.4 结果与边界

- `ucsan_jit.c` 构造带负下界对象的 seed，并验证 JIT 访问返回预期退出码 42；
- `test_ucsan_seed.py` 覆盖 round-trip、canonicalization、alias learning、冲突与循环；
- 尚待研究 minimal object grammar、复杂 symbolic pointer arithmetic 和更完整
  external stub soundness。

## 13. F12：分布式状态与多 coordinator 控制面

### 13.1 对象与 bitmap 传输

- 输入按 SHA-256 content address 发送，worker 命中本地 cache 时不重复传字节；
- AFL bitmap 以 bounded version journal 发送 sparse byte-bit delta；
- worker 落后于 journal 时回退 full snapshot；
- QSYM 内部 pruning map 与 AFL authoritative map 分离。

### 13.2 持久状态与 continuation task

- analyzed input 使用分片 ledger；
- 未完成工作写入 append-only lease journal，restart 后可恢复；
- state task key 包括 input digest/path、focus、target 和 actionseed；
- consistent hash 选择 owner shard/worker，优先本地派发并允许 bounded stealing；
- reward、failure、lease 和 completion 持久化。

### 13.3 多 master 一致性

- `FencedWorkLeaseTable` 为 work id 分配单调 fencing token；
- claim/renew/complete 必须携带 token；
- lease 过期后其他 coordinator 可 steal；
- stale master completion 被拒绝；
- coverage-owner shard 对 AFL hit-count bit 做锁内原子 OR claim；
- shard epoch 单调递增，coordinator 周期 pull/gossip；
- heartbeat 选择 preferred owner，其他存活 coordinator 可 proxy commit；
- OR merge 满足交换、结合和幂等，保证共享文件系统上的最终收敛。

### 13.4 结果与边界

- `test_distributed_state.py` 的 15 个用例覆盖 CAS、delta、shard、lease、fencing、
  stale completion、gossip 和 state task 恢复；
- 当前一致性依赖共享文件系统的 atomic mkdir/rename/replace；
- 非共享文件系统仍需要网络 CAS/quorum adapter 和 partition fault injection。

## 14. F13：bounded-DPOR 并发调度探索

### 14.1 方案与实现

- `libsymcc_schedule_rt.so` 通过 `LD_PRELOAD` 包装 pthread mutex、rwlock、cond 和
  join 等同步点；
- 运行时为线程分配 logical thread id，写出 `seq tid op object` trace；
- replay prefix 指定每个受控同步点应优先运行的 logical thread；
- 等待超过 `SYMCC_SCHEDULE_WAIT_MS` 时放行，避免 replay 永久阻塞；
- `DporScheduleExplorer` 在同一同步对象上的 dependent pair 处生成 alternative prefix；
- explorer 可解析 F40 可选发出的 `read`/`write` trace rows，用 SC vector-clock 和
  lockset 注释 trace，并只为未被 HB/共同 lock 保护的内存冲突生成 source-style replay
  prefix；
- prefix 深度、trace window、队列和每次生成数都有硬上限；
- schedule job 进入 MPI 高优先级 work queue，并与普通 input state 一起持久化。

### 14.2 结果与边界

- `test_schedule_exploration.py` 验证 trace parser、prefix 规范化、dependent
  backtracking、HB/lockset memory-conflict 分类、去重和 persistence；
- 这是面向 fuzzing diversity 的 bounded-DPOR，不声称穷举所有 interleaving；
- 后续需加入真实多线程 benchmark 的 schedule coverage 和 happens-before 消融。

## 15. F14：AFL++ profile 与混合执行编排

### 15.1 实现

- benchmark 可启动 AFL++ baseline、SymCC、MPI parallel 和 hybrid adaptive；
- AFL profile 支持 LAF/CTX/Ngram/CmpLog/MOpt 组合与独立输出；
- adaptive 模式自动连接 data coverage preload、hint mutator、SymCC extras 和 poly cache；
- worker 执行 profile、solver algorithm override、focus、target、actionseed 和 DPOR
  prefix 在单次 message 中组合；
- 目标执行后统一进行 showmap/bitmap triage；
- profile 启动、进程退出和 artifact 路径写入 benchmark result。
- master/worker phase timing 与 redundancy 以 per-rank 文件记录，事后聚合，避免
  在 MPI 热路径上加入同步写；

### 15.2 结果与边界

- `test_afl_profile_orchestration.py` 验证 profile 命令与环境拼装；
- `test_tace_profile.py` 验证大输入选择性 taint profile；
- 实际 AFL++ 版本、map size、CPU affinity 和初始 corpus 必须写入最终实验元数据。

## 16. F16：benchmark、消融与统计

### 16.1 实现

`benchmark/run_benchmark.py` 当前能够：

- 运行 built-in 与 public target；
- 统一记录 serial/parallel/hybrid 的时间、覆盖、生成量和 worker 数；
- 收集 phase timing、redundancy、offline trajectory、approved action、
  policy evaluation 和 SMT scheduler state；
- 输出 human-readable report、CSV 和 JSON；
- 保留每次 run 的原始 artifact 路径。

`benchmark/analyze_ablation.py` 当前能够：

- 合并一份或多份 benchmark CSV；
- 以指定 baseline 对各 mode/feature 组比较；
- 计算 bootstrap interval 与 Vargha-Delaney effect size；
- 生成机器可读和介绍用摘要。

### 16.2 当前结果

当前仓库中的 benchmark result 目录证明历史管线能够产出数据，但不能自动证明最新
全部功能组合的独立收益。最新实现的功能性验证结果为：

- CMake/Ninja 完整构建通过；
- lit：68/68 通过；
- 其中 21 个 Python 测试入口共包含 123 个 `test_*` 单元用例；
- compiler/runtime 集成测试覆盖 telemetry、Backsolver/Veritesting、poly cache、
  UNSAT core、data coverage、S2F、directed guidance 与 UCSan。

最终论文级实验仍应执行：

- 相同初始 corpus、相同 CPU 总预算和固定随机种子记录；
- 每组至少 10 次调参、20 次最终比较；
- baseline、逐项 feature ablation 和 full system；
- coverage AUC、final coverage、time-to-target、accepted/CPU-hour、solver cost、
  worker redundancy；
- median、bootstrap confidence interval 和 Vargha-Delaney effect size。

## 17. 历史实现补档（F17-F25）

本节补录 2026-02-26 至 2026-07-20 已提交但此前分散在阶段报告中的实现。历史
定量结果保留其原始配置和结果等级：有固定预算测量的记为 B，不因结果已经写入
报告而升级为 R。

### 17.1 F17：MPI 并行执行基础与可靠协议

**研究问题。** 单进程 SymCC 的路径翻转受限于串行 seed 消费、求解器长尾和单点
I/O；直接复制进程又会造成重复求解、目录竞争和 coordinator 过载。F17 建立了
纯 MPI concolic 与 hybrid 共用的 master/worker 基础。

**实现。**

- `util/mpi_concolic_execution.py` 提供 seed 分发、worker 执行、生成输入回收和
  反馈式再入队；
- `util/mpi_fuzzing_helper.py` 把同一模型接到 AFL++ 队列与共享 coverage 状态；
- 生成文件采用原子写入，worker 环境使用独立副本，终止阶段显式处理正在求解和
  正在保存的任务；
- 协议由大对象回传演进为路径/内容哈希和稀疏元数据回传；大规模运行按 worker
  数自动启用多 coordinator 与共享目录直写；
- 修复 `np>=64` 的 inter-master 死锁、唯一输入重复计数、serial orphan、超时
  误判和非阻塞消息资源泄漏。

**证据与边界。** MPI、serial 和多 master 路径在项目 benchmark 上完成集成运行，
历史架构和评测见
[`MPI_Parallelization.txt`](../MPI_Parallelization.txt)、
[`Parallel_Architecture_v2.md`](../Parallel_Architecture_v2.md) 和
[`SymCC_MPI_Parallel_Evaluation_Report.md`](../SymCC_MPI_Parallel_Evaluation_Report.md)。
结果等级为 I/T/B；共享目录多 master 仍依赖共享文件系统，不是无共享存储的
分布式一致性协议。

### 17.2 F18：AFL++/SymCC 闭环与吞吐优化

**方案。** AFL++ 负责高吞吐变异，SymCC 负责约束翻转；AFL queue 增量流入
concolic workers，只有经真实 `afl-showmap` 判定带来新 bitmap bit 的候选才回灌
fuzzer。该闭环把候选生成与覆盖所有权严格分离。

**关键优化。**

- coverage triage 下沉到 worker，coordinator 只接收稀疏边与小型元数据；
- coverage bitmap 加版本号，worker 在版本变化时同步，降低陈旧判断；
- showmap 从逐进程执行演进为 streaming fork server，再演进为批量 `-I`；
- AFL queue 扫描改为 `os.scandir`、增量缓存和反馈阶段跳过无效全量扫描；
- 输入先做内容哈希去重，batch 内和跨 batch 再去重，避免为字节相同的候选调用
  showmap；
- dispatch、feedback、队列扫描与 MPI 接收交错执行，避免 phase barrier。

历史固定配置测量记录了 master showmap `103s -> 0`、消息约 `8MB -> 64B`、
单次 showmap `12ms -> 0.6ms`、queue 扫描 `39.5s -> 3.7s`、MPI 接收数据
`34MB -> 3.6MB`。这些数字来自
[`Parallel_Architecture_v2.md`](../Parallel_Architecture_v2.md)，用于说明优化机制，
不能外推为跨目标平均加速，故等级为 I/T/B。

### 17.3 F19：可扩展工作分解与资源控制

F19 针对“增加 worker 后重复路径上升、单 seed 内部并行度不足”的扩展瓶颈：

- AFL persistent mode 与 shared-memory testcase 支持减少目标启动和文件 I/O；
- focus partition 把输入相关字节域切给不同 concolic worker；
- dynamic work stealing 在空闲 worker 出现时拆分未完成任务；
- branch-density-balanced partitioning 使用分支密度而非等长字节区间，降低任务
  难度偏斜；
- adaptive granularity 根据队列压力、worker 空闲和历史成本改变拆分粒度；
- benchmark 记录总 CPU 与 active worker，避免把进程数直接当成有效并行度。

历史 persistent-mode 脚本在特定目标上记录过 `5x-35x` 吞吐变化，synthetic、
libarchive、SQLite 和多实例实验也验证了扩展路径能运行；同时多实例 seed
partition 的负结果被保留，说明“更多独立实例”不自动等于更高覆盖。等级为
I/T/B，尚需在相同总 CPU 和固定 corpus 下重新验证最新 scheduler 组合。

### 17.4 F20：结构化输入与异构 fuzzing 协同

该实现族扩展了纯 SMT 路径翻转的候选空间：

- QSYM runtime 支持 `SYMCC_DICT`，按求解修改位置拼接 AFL dictionary token；
- AFL++ profile 接入 LAF、CTX/NGRAM、CmpLog 与 MOpt，用 comparison 反馈补充
  魔数和状态相关变异；
- `util/grimoire_gen.py` 从 corpus 学习结构 fragment，支持静态重组、多粒度 gap
  泛化和向 SymCC 直接喂入；
- honggfuzz 作为异构覆盖引擎写入独立目录，再通过 AFL `-F` 与 hybrid 闭环共享；
- 编排层可同时运行 AFL、honggfuzz、GRIMOIRE 与 MPI concolic，并保留各来源计数。

历史固定配置中，LAF+NGRAM 曾测得 queue 多样性 `+48%`、公共覆盖 `+1.4%`；
GRIMOIRE 静态重组为 `4339 -> 4628` 边（`+6.7%`），多粒度版本为 `4691`
（相对 seeds `+8.1%`），直接 feed 记录 `800` 个结构输入和 `304` 个 concolic
triage batch。原始条件与异构集成记录见
[`Research_and_Optimization_Report.md`](../Research_and_Optimization_Report.md)。
这些是 B 级历史结果，不是控制全部变量后的独立因果结论。

### 17.5 F21：公开 benchmark 与测量正确性

**覆盖范围。** benchmark 从内置 `maze/parser/deep_branches/crypto_check` 扩展到
LAVA-M、Google Fuzzer Test Suite、libarchive、SQLite、PCRE2 和 FreeType2，并
支持 target-specific seed、参数文件、stdin/file harness 与自动发现。

**测量修正。**

- coverage 与 crash 分开统计，seed 不计入生成输入，吞吐 speedup 以同口径工作量
  计算；
- 读取 AFL `fuzzer_stats` 的 `execs_done/execs_per_sec`，不使用目录大小代替；
- 大 corpus 的 replay 采用分层抽样和上限，保留早期、中期、后期样本；
- 识别 libtool wrapper、非插桩 ELF、LAVA-M 参数和 target suffix，避免把构建成功
  误判为有效 benchmark；
- 修复 batch timeout 溢出，并保存 CSV、JSON、文本报告和原始 artifact 路径。

历史结果涵盖 10-target、8 targets x 4 modes x 3 worker counts、最高 190 ranks
和 public hybrid runs；它们证明评测管线及多种目标可运行。由于不同阶段的 harness、
CPU 分配和版本发生过变化，跨报告数字不得直接拼成一张 SOTA 对比表。

### 17.6 F22：阶段计时与冗余归因

可观测性从“总运行时间”下钻到 worker 内部因果链：

- `SYMCC_WORKER_PROFILE=1` 记录取任务、运行 concolic、扫描输出、内容去重、
  showmap、反馈发送等 phase；
- SIGTERM 中断时间归属到当时 phase，避免 shutdown 时间落入 unknown；
- redundancy funnel 区分同一 worker 内部重复、跨 item 内容重复、跨 worker 重复、
  coverage 已见和最终 accepted；
- hint 可通过环境变量强制开关，支持真实消融；
- benchmark 汇总 phase timing、候选数量、跳过原因和根因比例。

这些字段支持定位“求解重复”和“triage 重复”两类不同问题，也为 F08/F15 的
干扰感知调度提供观测基础。当前为 I/T/B；旧实验没有统一 schema，跨阶段只比较
定义完全一致的字段。

### 17.7 F23：可配置 SymCC/SymSan 引擎抽象

`util/concolic_engine.py` 定义 `ConcolicEngine` 黑盒契约：给定 target、seed 和
输出目录，返回命令、环境与 stdin 策略。`SymCCEngine` 使用进程内 QSYM backend，
`SymSanEngine` 使用 DFSan 插桩目标和 `fgtest` driver；MPI 编排、coverage triage、
K-Scheduler、冗余统计和反馈路径保持共用。

构建与发现同样引擎感知：编译 wrapper、目标后缀和插桩符号检测由 engine 决定。
验证覆盖：

- parser 嵌套 magic 的反馈式 dogfight：两引擎均在第 4 轮到达目标，各 11 个唯一
  输入；
- 45 秒 coverage dogfight：展示 SymCC 的生成吞吐和 SymSan 的目标兼容性差异；
- `run_benchmark --engine {symcc,symsan} --hybrid` 全链路：微目标上均达到
  `30/38` 边。

详见 [`engine_abstraction.md`](../engine_abstraction.md)。以上为特定目标 B 级结果，
不支持“某引擎总体优于另一引擎”的结论。

### 17.8 F24：SymSan 技术迁移与 RGD 求解栈

SymSan 侧完成四项直接迁移：

1. DFSan taint 源处的选择性符号化；
2. driver 层 dictionary-guided candidate；
3. 多分支解的 multi-field 组合；
4. `offset:old:new` hint 旁车输出与编排复用。

第五项 fast-solve 在 SymSan 的 task/JIGSAW 模型中由既有快速路径覆盖，记录为
语义等价分析而非重复实现。新增 `fgtest_rgd` 把 I2S、JIGSAW gradient search 和
Z3 组成级联，并保留 one-shot 输出目录契约。修复了 `taint_getc` 丢 label、
SQLite memory-error 默认退出、优化级别导致的构建问题和退出阶段 crash。

真实目标覆盖 base64、libxml2、libpng、PCRE2、SQLite 和 coreutils/uniq；
6-target、3-round hybrid 均值已经归档。实现与参数见
[`symsan_ported_techniques.md`](../symsan_ported_techniques.md)，实验见
[`symsan_hybrid_benchmark_6targets.md`](../symsan_hybrid_benchmark_6targets.md)。
当前为 I/T/B；C++ libFuzzer harness 的外部 FastGen 链接、离散
`SYMCC_FOCUS_SET` 和最新 F00-F16 telemetry 对齐仍是明确边界。

### 17.9 F25：可复现安装与离线交付

工程交付覆盖三种环境：

- `setup.sh` 完成依赖检测/安装、venv、submodule、构建和 smoke test，支持重复执行；
- `package.sh` 生成 tar/zip、无 Git 源码包和 `--offline` air-gapped bundle；
- 离线包收录 apt 依赖闭包、Python wheels、AFL++ 源码、runtime 源码，可选收录
  SymSan、patch、Z3 与构建脚本；
- 安装树使用相对 RPATH，避免移动目录后 `fgtest` 仍引用打包机绝对路径；
- package/setup 经代码审查修复管道误判、缺失 `apt-get update`、`.gitignore`
  排除和依赖闭包问题，并在 debootstrap bare-machine 环境验证。

该项的 B 表示在固定 Ubuntu/debootstrap 环境完成部署验证，不表示对所有 Linux
发行版提供兼容性保证。交付包仍需保存 OS、LLVM、Z3、AFL++ 和 SymSan revision
作为复现实验元数据。

## 18. 可复现操作

### 18.1 构建与全量回归

```bash
cmake --build build -j2
ninja -C build check
```

### 18.2 离线策略报告

```bash
python3 util/offline_policy.py \
  benchmark_results/.../.offline_trajectory.jsonl
```

### 18.3 消融分析

```bash
python3 benchmark/analyze_ablation.py \
  benchmark_results/benchmark_data.csv \
  --baseline-mode baseline \
  --output-dir benchmark_results/ablation
```

### 18.4 UCSan seed 工作流

```bash
python3 util/ucsan_seed.py inspect seed.bin
python3 util/ucsan_seed.py normalize seed.bin --output normalized.bin
python3 util/ucsan_seed.py learn dumps/*.bin --output learned.bin
```

完整参数、默认值和产物路径见
[`Configuration.txt`](../Configuration.txt) 与
[`benchmark/README.md`](../../benchmark/README.md)。

## 19. 工作介绍建议

### 19.1 30 秒版本

本项目把 SymCC 从单机路径翻转器扩展为并行混合符号执行框架。系统统一了编译器
结构信息、QSYM 求解遥测、AFL code/data coverage 和 MPI worker 状态，并围绕
prefix continuation 调度 exact、tailored、sampling 与语义 proposal。所有近似或
学习型模块只生成候选，最终由真实目标 replay 和全局 AFL bitmap 验证。

### 19.2 三项核心贡献

1. 跨编译器、运行时、求解器与分布式调度的统一路径上下文；
2. Backsolver/Veritesting、Pangolin reuse、UCSan object graph 和 data coverage 的
   编译型工程整合；
3. 带 propensity、并行干扰和保守离线门控的可学习 solver-sequence 调度。

### 19.3 介绍时必须保留的边界

- “已实现/已测试”不等于“已在公开 benchmark 上达到 SOTA”；
- agentic/neural/semantic 模块不进入正确性可信边界；
- DPOR 是 bounded schedule diversification，不是完备模型检查；
- 多 master 一致性当前以共享文件系统为部署前提；
- 最新 full system 的等 CPU、多轮统计验证仍是下一阶段主任务。

## 20. 后续新增功能的强制记录模板

以后每项新增实现必须在合并前更新本档案，并包含以下内容：

```markdown
### Fxx：功能名称

- 日期/负责人：
- 研究问题：
- 技术来源与差异：
- 方案：
- 可信边界与保守回退：
- 主要代码：
- 配置开关及默认值：
- 输入/输出 schema 或持久化产物：
- 单元测试：
- 集成测试：
- benchmark 配置：
- 当前结果等级：I / T / B / R
- 定量结果：
- 已知局限：
- 下一步：
```

同时执行四项同步更新：

1. 在 `docs/Configuration.txt` 记录所有用户可见配置、默认值和副作用；
2. 在本档案清单中增加 ID、代码、测试和状态；
3. 增加至少一个自动化测试，或明确说明无法自动测试的原因；
4. 若影响 benchmark schema，在 `benchmark/README.md` 记录新增字段与复现命令。

只有完成上述记录，功能才视为“工程完成”；只有达到 R 级，才可在论文或汇报中
表述为经实验验证的性能贡献。

## 21. F26：跨层正确性加固（2026-07-24）

- 研究问题：SOTA 求解复用和并行调度优化是否保持位向量、路径上下文与分布式
  提交的一致性；
- 方案：无回绕线性化证明、`poly-cache-v2` 完整前缀键、fast/cache 候选复验、
  目标专属终态、确定性 LLVM site ID、UCSan 完整 shadow copy、multi-master
  `begin_commit` fencing；
- 主要代码：QSYM `solver.cpp/h`、`compiler/SiteId.h`、`compiler/Pass.cpp`、
  `compiler/Symbolizer.*`、`runtime/src/LibcWrappers.cpp`、
  `runtime/src/UCSanRuntime.cpp`、`util/hybrid_feedback.py`、
  `util/distributed_state.py`、`util/mpi_fuzzing_helper.py`；
- 自动化测试：`poly_modular_soundness.ll`、`poly_prefix_cache.c`、
  `fast_solve_prefix.c`、`libc_long_compare.c`、`ucsan_partial_shadow.c`、
  `ucsan_invalid_entry.c`、`stable_site_ids.c` 及三组 Python 回归；
- 当前结果等级：T；
- 详细错误模型、实现不变量、代价与验证：
  [`Correctness_Review_Fixes_2026-07.md`](../Correctness_Review_Fixes_2026-07.md)；
- 局限：确定性 ID 针对相同 IR 结构；poly 证明是保守充分条件；F26阶段曾允许
  committing recovery依赖共享文件系统时钟与lease TTL，F321已因其存在旧holder双写窗口而
  改为只有pre-commit leased状态可恢复，commit后恢复等待持久事务日志；
- 下一步：把 fallback/retry/fencing 指标加入等 CPU 多轮消融，量化正确性加固的
  性能代价和错误候选减少量。

## 22. F27：持久化 Query IR 与异步增量求解（2026-07-24）

- 研究问题：并行 concolic worker 是否可以共享约束而不是反复重放 seed、重建相同
  路径前缀，并允许 tracer 退出后继续求解；
- 技术来源与差异：实现 Triereme/Marco 风格的 trace/solve 解耦和 query-trie
  调度基础。当前复用粒度是规范化完整前缀，不声称已经复现论文的全部全局搜索策略；
- 方案：QSYM 将路径条件和目标条件序列化为 `symcc-query-ir-v1`，其中表达式节点按
  拓扑顺序记录 opcode、位宽、children 和常量/输入偏移。Python 服务验证 envelope，
  将表达式与 SMT-LIB artifact 以 SHA-256 内容寻址，构造持久 prefix trie，再通过
  fenced lease 分配给独立 solver worker；
- 增量上下文：每个 worker 持有长期运行的 `symcc-query-solver --server`。首次看到
  prefix hash 时解析 `prefix_smt2`，后续查询在该 solver 上执行
  `push -> target_smt2 -> check/model -> pop`。prefix ID 对 worker 槽做确定性分片，
  避免共享前缀在不同进程间反复冷启动；
- 缓存与剪枝：相同 query ID 直接复用持久 SAT/UNSAT result；每个查询另存规范化
  clause set。已证明 UNSAT 的集合只在其为新查询子集时剪枝逻辑超集，并记录
  `reused_from`，不使用 branch-only 猜测；
- 并行可靠性：SQLite WAL/FULL、不可变 artifact、原子 spool rename、过期 lease
  recovery 和递增 fencing token 防止 stale worker 覆盖新结果。损坏 envelope 被
  移入 rejected 目录；
- 结果回流：SAT model 只修改 witness 中有效的输入 offset，生成候选及 manifest。
  MPI master 将候选放回普通 feedback queue，最终保留仍由真实目标 replay 和全局
  AFL bitmap 决定；
- 主要代码：QSYM `solver.cpp/h`、`runtime/src/backends/qsym/query_solver.cpp`、
  `util/query_store.py`、`util/symcc_query_service.py`、
  `util/mpi_fuzzing_helper.py`；
- 配置：`SYMCC_QUERY_SPOOL`、`SYMCC_QUERY_DEFER`、
  `SYMCC_QUERY_OUTPUT_DIR`、`SYMCC_QUERY_MAX_NODES`、
  `SYMCC_ASYNC_QUERY_WORKERS`、`SYMCC_QUERY_STORE`、
  `SYMCC_QUERY_SOLVER`、`SYMCC_QUERY_TRAVERSAL` 和
  `SYMCC_QUERY_PREFIX_CACHE`，详见 `Configuration.txt`；
- 持久化产物：`index.sqlite3`、`objects/{smt2,smt2-prefix,smt2-target}`、
  `queries/<sha256>.json`、`results/<sha256>.json`、
  `candidates/<sha256>.bin` 与 spool 的 accepted/rejected 分区；
- 单元测试：`test/test_query_store.py` 覆盖内容寻址/前缀去重、fenced lease、
  model materialization、损坏 spool 隔离和 UNSAT 超集剪枝；
- 集成测试：`test/query_prefix_reuse.py` 证明两个公共前缀查询只构造一个缓存上下文，
  第二个结果包含 `prefix_cache_hit=true`；`test/query_ir.c` 证明 runtime 在
  `SYMCC_QUERY_DEFER=1` 时不进行 inline solve，退出后服务仍求得
  `0x12345678` 对应输入；
- 当前结果等级：I/T，SOTA 审计中的证据等级为 E；
- 定量功能结果：5 个存储层单元测试和 2 个 lit 集成测试通过；共享前缀测试的缓存
  entry 数为 1，第二次查询命中；端到端测试的 runtime `solver_queries=0` 且异步
  候选字节为 `78 56 34 12`；
- 可信边界：IR 校验、SMT 求解和 clause-subset 规则决定逻辑结果；任何异步 candidate
  仍需 concrete replay。solver 进程 crash 会由 fenced retry 处理，不会把错误结果
  当作 SAT；
- 已知局限：当前只有 Z3 QF_BV helper；prefix cache 位于单个 worker 进程内；
  clause-subset cache 保存完整 UNSAT 查询而非最小化 proof core；尚无公开 benchmark
  的 query redundancy、cache hit、coverage/CPU-hour 和内存增长曲线；
- 下一步：G03 使用同一 IR 记录 static-data feature；G02 在 query result 之上持久化
  solution generator；G06/G07 扩展多 solver portfolio 与 proof/partial-solution
  cache。

## 23. F28：Static Data Coverage 与独立数据新颖度（2026-07-24）

- 研究问题：边覆盖和常量比较进度无法区分 lookup table、lexer automaton、静态图等
  “访问了不同常量数据但没有产生新分支”的输入，如何把这类状态转移纳入并行
  concolic 调度；
- 技术来源与差异：依据 USENIX Security 2024 Data Coverage 的静态存储识别、有效
  匹配长度、switch 邻接选择和 code/data 联合 dominance。实现保持 SymCC 的编译型
  传播方式，并增加跨模块稳定对象 ID；AFL preload 兼容平面与无碰撞结构化 novelty
  平面分离；
- 编译方案：module ctor 注册 constant globals；`Symbolizer` 对 load provenance 做
  保守回溯，区分 simple load 和已提升为 predicate 的 load，给整数比较传递 concrete
  operands、静态 origin 和 signed/ordered 类别；switch case 排序后以只读数组一次
  传入 runtime；
- runtime 方案：启动时用 `dl_iterate_phdr` 注册已加载模块的 readable/non-writable
  `PT_LOAD` 段，细粒度 LLVM object 优先于包含它的模块段。特征键为稳定
  `(object_id, byte_offset, effective_width_bits)`，不包含 ASLR load base；
- 有效长度：相等谓词使用 `width - popcount(lhs xor rhs)`，有序谓词使用最高位起的
  相等位数；region comparison 使用完整相等前缀字节加首个差异字节的相等位数；
  simple load 的有效长度为 load width。switch 仅比较 lower/upper neighbor 或范围
  endpoint，并输出实际 switch/probe 计数；
- novelty 与 corpus：telemetry schema 3 输出
  `[object, offset, matched, width, kind]`。协调器以完整三元键保存 winner，严格单调
  增加 matched bits；只有 code summary 相等时记录 refinement。旧 schema-1/2 状态
  可迁移。该机制是独立持久 novelty/dominance，不与 AFL edge map 哈希碰撞；
- 可信边界：普通边覆盖仍负责 trivial branch；AFL preload 仍是 comparison-prefix
  兼容通道。协调器只降低被支配 seed 的调度 bonus，不删除 AFL queue 文件；不同
  路径摘要之间不做激进替换；
- 主要代码：`compiler/Runtime.*`、`compiler/Pass.cpp`、
  `compiler/Symbolizer.cpp`、`runtime/include/RuntimeCommon.h`、
  `runtime/src/LibcWrappers.cpp`、QSYM/simple backend runtime、QSYM
  `solver.cpp/h`、`util/hybrid_feedback.py`、`util/mpi_fuzzing_helper.py`；
- 配置：`SYMCC_DATA_COVERAGE`、`SYMCC_DATA_CMP_BYTES`、
  `SYMCC_TELEMETRY_OUT`，详细 schema 见 `Configuration.txt`；
- 自动化测试：`data_coverage_static.c` 同时覆盖 lookup table、static graph、
  predicate、region compare、switch 邻接、位级 equality 和 ASLR 稳定性；
  `data_coverage_dso.c` 验证未插桩 DSO 的只读表；`telemetry.c` 验证 schema；
  `test_hybrid_feedback.py` 验证不同 width 不碰撞、同 code summary 的 winner
  refinement 和持久化迁移；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：40 个 hybrid-feedback unittest、4 个 data/telemetry lit 集成测试、
  2 个 AFL preload unittest 通过；微基准观察到 kind 0/1/2，位级 equality 为
  6/8，四 case switch 对中间值只产生 2 个 probe，未插桩 DSO 产生稳定 32-bit
  static feature；
- 已知局限：启动后才通过 `dlopen` 加载且未插桩的 DSO 不会自动重扫；code summary
  当前采用完整 path fingerprint，安全但可能漏掉 code-equivalent refinement；
  尚未在真实 flex/bison、static graph 和 DSL parser 上给出等 CPU 多轮收益；
- 下一步：公开 benchmark 上测量 data feature AUC、refinement、edge coverage/CPU、
  instrumentation overhead 与内存上限；随后按审计顺序推进 G02 solution generator。

## 24. F29：Reusable Solution Generator 与原生全矩阵 polytope mutator（2026-07-25）

- 研究问题：传统 concolic 执行通常一次 SMT 查询只返回一个 model，导致并行 worker
  在同类 path prefix 上重复求解。GenSlv 的关键思想是把“一个解”提升为可复用的
  input generator；Pangolin 的关键思想是从同一线性 path abstraction 采样多个解。
  本轮实现把这两条线合并到 G01 异步 query store 和 AFL 原生 mutator；
- 技术来源与差异：实现借鉴 GenSlv 的 reusable generator、层次 range sampler 和
  model-converter 思路，以及 Pangolin 的 full-matrix polyhedral sampling。当前不是
  论文等价复现：没有导出 Z3 tactic 内部 model-converter，也没有跨 prefix 的
  polyhedron inclusion 证明；但已经实现可版本化、可持久化、可验证回放的核心
  generator 语义；
- Generator IR：新增 `symcc-solution-generator-v1`，包含 `query_id`、
  `input_size`、确定性 `seed`、`fixed` byte assignment、`ranges`、连续 byte
  `fields`、`converter_chain`、`verified_models` 与 `metrics`。IR 使用 canonical JSON
  计算 content SHA-256，并在 query store 中单独持久化；
- Converter 子集：`util/solution_generator.py` 从 topological Query IR 中提取可逆
  equality chain，覆盖 `read`、`concat` split、`zext/sext`、`not/neg`、`xor/add/sub`、
  奇数模乘逆元以及常量 `rol/ror`。提取结果只作为 generator 元数据和后续采样提示，
  不绕过求解验证；
- C++ solver 侧：`symcc-query-solver` 在 SAT 后遍历当前 solver assertions，收集
  8-bit input variables，按完整 solver context 对每个变量做有界二分，得到 exact
  feasible unsigned lower/upper。然后按 byte、field boundary/midpoint 和确定性随机
  层次采样生成候选；每个 candidate 都重新加回原 solver 做完整 byte assignment
  SAT check，`unknown` 不被当作有效模型；
- query store 侧：`QueryStore.complete()` 校验并补齐 generator 的 lease `query_id`，
  若 solver helper 没有 converter chain，则从存储的 Query IR 自动派生。generator
  与 result 在同一 SQLite transaction 中提交，并写入
  `generators/<prefix>/<sha>.json`。candidate materialization 同时消费 primary model
  和所有 `verified_models`，manifest 记录 `model_index`、`generator_hash`、
  `solver_verified=true` 和等待 concrete replay 的 `verified=false`；
- Python sampler：`sample_generator()` 对 fixed/range/field offset 做 witness 边界
  检查，优先生成 field lower/mid/upper 候选，再按 generator seed 做确定性随机 range
  采样。所有输出必须通过调用方 verifier，返回 metrics 中的 proposal、verified、
  accepted、field_attempts 和 synthetic_attempts；
- AFL mutator：`util/afl_symcc_hint_mutator.py` 扩展 poly cache 第六列
  `lower:upper:offset=coefficient,...;...`，保存 `(model, byte box, full matrix)`。
  poly 模式先恢复一个满足矩阵的 feasible base，再沿 joint byte box 和完整线性矩阵
  计算整数 direction 的 alpha interval；输出前再次检查矩阵。若无法移动但已有可行
  点，返回该可行点而不回退到普通 havoc，避免破坏跨变量约束；
- 配置：`SYMCC_GENERATOR_SAMPLES`、`SYMCC_GENERATOR_MAX_VARS`、
  `SYMCC_GENERATOR_CHECK_TIMEOUT` 控制 helper 的 generator 预算；
  `SYMCC_HINT_MUTATOR_POLY_ATTEMPTS` 控制 mutator 从不相关 AFL seed 恢复 feasible
  base 的随机尝试次数。AFL mutator 继续读取 `SYMCC_POLY_CACHE_MUTATOR` 或
  `SYMCC_POLY_CACHE`；
- 主要代码：`runtime/src/backends/qsym/query_solver.cpp`、
  `util/solution_generator.py`、`util/query_store.py`、
  `util/afl_symcc_hint_mutator.py`；
- 自动化测试：`test/test_solution_generator.py` 覆盖可逆 word converter、deterministic
  verifier-gated sampling、field boundary models、重复 range 拒绝和 witness 越界拒绝；
  `test/test_query_store.py` 覆盖 generator 持久化、converter augmentation、content
  hash 和多 verified model materialization；`test/query_generator.py` 运行真实
  persistent query solver，验证 `10 <= input[0] <= 20` 的 range/field/generator artifact
  与候选输出；`test/test_afl_hint_mutator.py` 覆盖 full-matrix 相关约束和可行定点
  不退回 havoc；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`test_solution_generator.py` 5 个 unittest、`test_query_store.py`
  6 个 unittest、`test_afl_hint_mutator.py` 3 个 unittest、`query_generator.py`、
  `query_ir.c`、`query_prefix_reuse.py` 三个 lit 集成测试通过；增量
  `cmake --build build` 通过。真实 helper 集成测试中 generator 产生 `[0,10,20]`
  range、`[0]` field、至少 3 个 solver-verified models，query store 物化至少 4 个
  位于 `[10,20]` 的候选；
- 可信边界：generator 只产生候选，不替代 exact solver result；每个 solver-side
  candidate 先由原 SMT context 验证，每个 corpus candidate 仍需 concrete replay 和
  AFL novelty triage。mutator 的矩阵验证只证明 cached linear abstraction 一致，不
  证明目标程序路径一定改变；
- 已知局限：当前 helper 只覆盖 Z3 QF_BV byte variables；converter chain 是 Query IR
  上的有界语法提取，不是 Z3 tactic pipeline 的完整 model-converter；range 是
  per-byte exact interval，未实现 optimistic simplification 的约束弱化证明；Pangolin
  cache 仍缺跨 prefix/polyhedron inclusion/subsumption 索引；尚未在公开 benchmark
  上报告 generator coverage、models/query、valid-model ratio、unique edges/query 和
  coverage/CPU-hour；
- 下一步：在 G06 solver portfolio 中让不同 solver helper 共享同一 generator schema；
  在 G18 后续研究中加入兼容 polyhedron 索引和跨 prefix 复用；在 benchmark 中加入
  single-model、range-generator、poly-generator 和 full-generator 的消融。

## 25. F30：SymCC-str-inspired String Constraint Artifact 与 offset-aware candidate（2026-07-25）

- 研究问题：C 字符串程序经常把多个输入字节作为一个逻辑 string 处理。纯 bit-vector
  路径约束可以求解，但 fuzzing 侧只看到零散 byte hint，难以把 `strcmp/strncmp`
  这类语义作为可复用候选。完整 SymCC-str 需要 SMT string/BV 双表示；本轮实现先
  落地一个保守的 string/BV 桥；
- 技术来源与差异：借鉴 TACAS 2026 SymCC-str 的 libc string 语义提升目标，但不在
  trusted path 中替换现有 QSYM bit-vector solver。实现只在一侧 concrete、另一侧为
  直接 input-byte `ReadExpr` 时记录 offset patch；复杂派生字符串、非直接输入 byte、
  substring/indexof/contains 仍保守回退到现有 BV 约束和 string hint；
- runtime ABI：新增 `_sym_expr_input_offset(SymExpr, uint32_t*)`。QSYM backend 只对
  裸 `ReadExpr` 返回 input offset，simple backend 返回 false。该 hook 不改变表达式
  构造、solver stack 或 path constraint；
- libc wrapper：`memcmp/strcmp/strncmp` 在现有 data coverage、string hint 和
  memory-equality constraint 之外，可选写出 `symcc-string-constraint-v1` JSONL。记录
  字段包括 op、site、result、taken_equal、symbolic_side、token_hex、nul_terminated、
  complete 和 `patches[{offset,value}]`。`strcmp` 在 bounded concrete token 内观察到
  NUL 时加入 NUL patch；
- candidate materialization：新增 `util/string_constraints.py`，严格校验 schema、
  patch offset 唯一性和边界，再按 witness 生成候选；调用方 verifier 必须确认候选
  有效。`util/check_string_constraints.py` 用于 lit 集成检查；
- AFL 原生消费：`util/afl_symcc_hint_mutator.py` 读取 `SYMCC_STRING_CONSTRAINTS` 或
  `SYMCC_STRING_CONSTRAINT_OUT`，按 patch 直接修改 AFL buffer，作为独立
  `symcc_string` mutation stage。offset 超出当前 seed 时保守失败并交给后续 mutation；
- 配置：`SYMCC_STRING_CONSTRAINT_OUT`、`SYMCC_STRING_CONSTRAINT_MAX_BYTES`、
  `SYMCC_STRING_CONSTRAINT_MAX_RECORDS` 控制 runtime artifact；
  `SYMCC_STRING_CONSTRAINTS`、`SYMCC_HINT_MUTATOR_STRING_LINES` 控制 AFL mutator 消费；
- 主要代码：`runtime/include/RuntimeCommon.h`、QSYM/simple backend `Runtime.cpp`、
  `runtime/src/LibcWrappers.cpp`、`util/string_constraints.py`、
  `util/check_string_constraints.py`、`util/afl_symcc_hint_mutator.py`；
- 自动化测试：`test/test_string_constraints.py` 覆盖 schema 校验、offset patch
  materialization 和重复 offset 拒绝；`test/test_afl_hint_mutator.py` 覆盖
  `symcc_string` mutation stage；`test/string_constraints.c` 运行真实 instrumented
  `strcmp` wrapper，从 stdin 符号输入记录 JSONL，再检查 `xxxxx\0zz` 可还原为
  `MAGIC\0zz`；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：新增 2 个 string-constraint unittest、mutator 4 个 unittest、
  `string_constraints.c` lit 集成测试通过；增量 runtime 构建通过；
- 可信边界：string constraint artifact 是候选生成提示，不替代 path constraint 和
  exact solver。只有 direct `ReadExpr` offsets 会进入 patch；所有候选仍需 verifier、
  concrete replay 和 AFL novelty triage；
- 已知局限：尚未实现 SMT string solver、smt-switch 双表示、symbolic length、substring、
  concat、contains、indexof、`strlen/strchr/strstr` 的 string-theory lowering，也未接入
  cvc5。当前机制对直接输入缓冲区的 equality/string-token 约束有效，对复杂 string
  操作只是候选提示；
- 下一步：为 `strlen/strchr/strstr` 加入同 schema 的 guarded patches；在 G04 后续阶段
  设计 backend-neutral string solver API，并在 G06 solver portfolio 中接入 Z3/cvc5
  string backend 对拍。

## 26. F31：Asynchronous Solver Portfolio 与 disagreement telemetry（2026-07-25）

- 研究问题：SMTgazer 调度的是互补 SMT solver/algorithm sequence，而不是同一 Z3
  后端上的环境开关。G01 已经把 query solving 抽象为独立 helper，因此可以先在
  asynchronous query service 边界实现真正的多 helper portfolio；
- 技术来源与差异：借鉴 SMTgazer 的 per-instance solver/algorithm portfolio 和
  exact replay oracle 思路。本轮实现不是完整 X-means/BO 序列学习，也没有内置
  cvc5/Bitwuzla lowering；它先建立可持久化的 portfolio attempt schema、winner
  selection 和 disagreement 保守处理；
- Portfolio runner：新增 `PortfolioSolver`，接受 `(name, solver callable)` 列表，对同
  一 `WorkLease` 运行多个 backend，记录每个 attempt 的 status、solver、elapsed_us
  和 prefix_cache_hit。若至少一个 SAT 且没有 UNSAT，选择 SAT；否则选择 UNSAT；若
  SAT 与 UNSAT 同时出现，返回 `unknown` 并标记 disagreement；
- 可信策略：SAT/UNSAT conflict 不会写成确定 proof 或 model，不进入 UNSAT subset
  cache，也不会 materialize candidate。错误或 unknown attempt 被记录，但不阻止
  其他 exact backend 给出结果；
- Query store schema：result 可包含 `symcc-solver-portfolio-v1`，字段为 `winner`、
  `disagreement` 和 bounded `attempts`。`stats()` 新增 `portfolio_results` 与
  `portfolio_disagreements`；
- Query service：`symcc_query_service.py` 支持 `--portfolio` 或
  `SYMCC_QUERY_SOLVER_PORTFOLIO`。JSON 形如
  `{"solvers":[{"name":"z3","command":"/path/to/helper","persistent":true}]}`。
  persistent backend 使用现有 `--server` prefix-cache 协议；非 persistent backend 逐
  query 调用 helper；
- 主要代码：`util/query_store.py`、`util/symcc_query_service.py`；
- 自动化测试：`test/test_query_store.py` 新增 portfolio winner telemetry 和
  SAT/UNSAT disagreement 降级为 unknown 的回归；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`test_query_store.py` 8 个 unittest 通过；此前 query generator 和
  Query IR lit 集成测试继续通过；
- 已知局限：还没有 backend-neutral SMT lowering 到 cvc5/Bitwuzla；没有 SMTgazer 的
  X-means/BIC、censored PAR-2 objective、BO sequence optimizer 或 boosting/bagging
  surrogate；F34后续补bounded parallel racing，F237--F241补异构context与调度，
  F242再补SAT-gated running-attempt cancellation；Bitwuzla实机仍未完成；
- 下一步：将 Query IR lowering 拆成 backend-neutral SMT-LIB profiles，接入 cvc5 和
  Bitwuzla helper；将 portfolio attempt telemetry 回灌到 `SMTAlgorithmScheduler` 的
  offline/online policy。

## 27. F32：PSCache-inspired Partial Solution Cache 与 Query IR 验证复用（2026-07-25）

- 研究问题：FSE 2024 PSCache 的核心价值不是 branch-only result cache，而是在不同
  path/query 之间复用可解释的 partial assignment。对本项目来说，第一优先级是把
  “可复用部分解”接入异步 Query IR/store，使其能提前产生候选输入，同时保持求解
  结果仍由 exact solver 或可证明 UNSAT subset 规则决定；
- 技术来源与差异：本轮实现采用 PSCache-inspired 工程主路径：复用 SAT primary
  model 与 solver-verified generator model 中的 byte assignment，并在目标 query 上
  做完整 Query IR evaluation 后才 materialize candidate。它不是完整 solver-internal
  CDCL/bit-blast conflict assignment extraction，也不把 partial hit 写成 SAT/UNSAT；
- Cache schema：`util/query_store.py` 新增 `symcc-partial-solution-v1`，SQLite 表
  `partial_solutions` 记录 `solution_hash`、source query、canonical assignments、
  offsets、source clause hashes、model index、generator hash 和 created time；
  `partial_solution_clauses` 建立 solution 到 source clause hash 的倒排索引；
  `partial_solution_candidates` 记录 query/witness/solution/candidate 的复用证据；
- 写入路径：`QueryStore.complete()` 在 SAT completion 同一事务中调用
  `_store_partial_solutions()`。只采集 `assignments` 和 `generator.verified_models`；
  `unknown`、`error`、disagreement portfolio result 和 UNSAT 不进入 partial solution
  cache；
- 读取路径：`QueryStore.ingest()` 在没有 exact cached result 时调用
  `materialize_partial_candidates()`；SAT completion 后额外调用
  `materialize_partial_candidates_for_pending()`，对有限数量 pending query 进行提前
  materialization；lookup 按当前 query 与 source clause 的 overlap 优先级排序，再做
  byte patch 和 Query IR verification；
- Query IR 验证器：新增 bounded evaluator，按 expression closure 从
  content-addressed `expressions` 表重建 DAG，支持常见 QF_BV 节点
  `bool/constant/read/concat/extract/zext/sext/add/sub/mul/div/rem/bitwise/shift/compare/land/lor/lnot/ite/rol/ror`。
  候选必须让所有 prefix roots 和 target root 为真；遇到未知 op、超出预算 bit-width、
  read 越界或除零时保守拒绝复用；
- Candidate manifest：partial candidate 使用既有
  `symcc-query-candidate-v1` manifest，并标注 `solver_verified=false`、
  `partial_solution_hash`、`partial_solution_source_query_id`、
  `partial_solution_verified=true`、`query_ir_verified=true`、`verified=false`。
  外部输出目录写入 `async-partial-*` 文件，仍需 MPI/AFL concrete replay 和 novelty
  triage；
- 配置：`SYMCC_PARTIAL_SOLUTION_CACHE` 控制开关，默认启用；
  `SYMCC_PARTIAL_SOLUTION_LIMIT` 限制每个 query 物化数量；
  `SYMCC_PARTIAL_SOLUTION_SCAN` 限制最近 partial solution 扫描数；
  `SYMCC_PARTIAL_SOLUTION_PENDING_QUERIES` 限制 SAT completion 后扫描 pending query
  的数量；
- 主要代码：`util/query_store.py`；
- 自动化测试：`test/test_query_store.py` 新增 partial cache positive/negative 回归。
  positive 用 source equality model 复用到 range query，检查 external
  `async-partial-*`、manifest flags、stats 和 query 仍保持 pending；negative 检查
  不满足 target 的 partial assignment 不会写 candidate 记录；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`test_query_store.py` 从 8 个扩展为 10 个 unittest 并全部通过；
- 可信边界：partial cache 只产生候选，不改变 solver result cache，也不写 UNSAT
  proof。候选的逻辑前置是 Query IR evaluation，最终保留仍依赖 concrete replay 和 AFL
  coverage novelty；
- 已知局限：F35 已补 persistent solver helper 内部 recent-SAT assignment probe，但
  尚未在 vendored Z3/CDCL/bit-blast 边界采集 conflict assignments，尚未进行 SAT
  variable 到 input bit-vector 的低层重建，也没有 proof-core/minimal partial solution
  extraction；当前 evaluator 对超大 bit-vector 和部分特殊 SMT-LIB 语义采取拒绝复用
  策略；
- 下一步：把 solver helper 暴露的 model/proof telemetry 与此 cache 合并，采集
  partial hit rate、candidate validity、lookup 开销和 solver-time saved；随后实现
  solver-internal conflict assignment extraction 并接入 ParaSuit 自配置预算。

## 28. F33：SymCC-str phase-2 String Theory Backend 与 MPI/CLI Materialization（2026-07-26）

- 研究问题：F30 只把 libc 字符串比较变成 exact offset patches，能快速恢复
  `strcmp/memcmp` 目标 token，但不能表达 `contains/indexof/substr/length` 这类真实
  string theory 关系，也无法在已相等路径上生成“避开当前 token”的 alternative。G04
  第二阶段要把 string/BV bridge 提升为 backend-neutral query layer，并让候选进入并行
  fuzzing 主路径；
- 技术来源与差异：借鉴 SymCC-str 的 bit-vector/string 双表示目标，但本阶段仍保持
  conservative side-channel 设计：string query 只生成候选，不替换 QSYM 的原始 BV
  path constraints。SMT string 模型经本地 byte-level verifier 再落到输入 offset；
  concrete replay 与 AFL bitmap novelty 仍是最终准入条件；
- Query schema：`util/string_constraints.py` 新增 `symcc-string-query-v1`。变量包含
  `name/offset/capacity/min_length/max_length/nul_terminated`，并要求 input span 不重叠；
  term 支持 `var/literal/concat/substr`，predicate 支持 `equal/distinct`、
  `prefixof/suffixof/contains`、`length`、`indexof` 和 `char_at`。所有字符串、hex、
  变量数、递归深度、constraint 数和 input size 都有上界；
- Lowering：`lower_string_query()` 生成 SMT-LIB String 公式，使用带 offset metadata
  的符号名 `|symcc!str!offset!capacity!nul|`。当前实现只把 printable ASCII literal
  下沉到 SMT string；包含任意二进制字节的 token 保守回退到 F30 patch/BV 路径；
- Solver helper：`runtime/src/backends/qsym/query_solver.cpp` 新增 `--generic` one-shot
  模式。普通 Query IR 仍走 QF_BV/generator 路径；generic 模式用 Z3 通用 parser/solver
  检查 SMT-LIB，并从 String model 中解析 UTF-8 byte 序列回填输入 offset。C string
  变量会在容量允许时补 NUL；超出 capacity 或无法映射的 model 被拒绝；
- Backend API：`SymccJsonStringBackend` 通过 `symcc-query-solver --generic` 调用 solver，
  返回现有 JSON model 协议。`materialize_string_candidates()` 会先尝试 string query，
  再回退 exact offset patches；所有 solver candidate 必须通过
  `candidate_satisfies_string_query()`，并且 no-op witness candidate 被过滤；
- MPI 集成：`util/mpi_fuzzing_helper.py` 默认启用
  `SYMCC_STRING_SOLVER_ENABLE=1`。每个 worker execution 写隐藏
  `.string_constraints.jsonl`，执行后最多扫描
  `SYMCC_STRING_SOLVER_QUERY_LIMIT` 条记录，按
  `SYMCC_STRING_SOLVER_TIMEOUT_MS` 调用 backend，并把 verified
  `string-solver-*` 候选写入同一 output dir。随后复用既有 pass 1/2 showmap、worker
  coverage dedup、master triage 和 AFL sync；`.string_solver_metrics.json` 记录
  loaded/scanned/written、solver_queries、solver_sat、solver_verified/rejected/errors；
- 离线 CLI：新增 `util/symcc_string_solver.py`，用于 benchmark 或 paper artifact 复现：
  `symcc_string_solver.py constraints.jsonl witness out --solver ...`。它和 MPI helper
  共用 `string_constraints.py` 的 normalization/lowering/verifier/materializer，不维护
  第二套语义；
- 配置：新增 `SYMCC_STRING_SOLVER_ENABLE`、`SYMCC_STRING_SOLVER`、
  `SYMCC_STRING_SOLVER_QUERY_LIMIT`、`SYMCC_STRING_SOLVER_CANDIDATES`、
  `SYMCC_STRING_SOLVER_TIMEOUT_MS`，详见 `docs/Configuration.txt`。没有可用 solver
  helper 时，MPI helper 仍可应用 exact offset patch，并把 backend 缺失记录到 metrics；
- 主要代码：`util/string_constraints.py`、`runtime/src/backends/qsym/query_solver.cpp`、
  `util/mpi_fuzzing_helper.py`、`util/symcc_string_solver.py`；
- 自动化与 smoke 结果：`python3 test/test_string_constraints.py -v` 6/6 通过；
  `lit -sv build/test/string_solver.py` 通过，覆盖真实 `symcc-query-solver --generic`
  equality、distinct 和 contains query；`python3 -m py_compile
  util/string_constraints.py util/mpi_fuzzing_helper.py util/symcc_string_solver.py`
  通过；CLI smoke 将 `xxxxx\0zz` materialize 为 `MAGIC\0zz`，metrics 中
  `solver_queries=1`、`solver_sat=1`、`solver_verified=1`、`written=1`；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 可信边界：这是 string-theory candidate plane，不是 trusted solver replacement。
  它不会写 SAT/UNSAT proof，不更新 QueryStore exact result cache，也不跳过 concrete
  replay。二进制 string literal、overlapping input spans、超长/递归 query、未知 op、
  model 越界和 verifier 不满足都会拒绝或回退；
- 已知局限：尚未实现 smt-switch 级 BV/String 同步视图，尚未接入 cvc5/Princess/Z3str3
  对拍，尚未在 runtime 中为 `strlen/strchr/strstr` 产生原生 string query；generic
  solver 目前 one-shot 调用，没有 persistent string-server、portfolio racing 或
  learned backend routing；
- 下一步：把 runtime artifact 从 patch schema 扩展为 guarded string-query schema，
  覆盖 symbolic length、substring/indexof/contains 和更多 libc model；随后把 F33
  backend 纳入 G06 portfolio telemetry，并与 G08/G11 的 grammar/solve-complete
  结构化补全共享 token 与 length constraints。

## 29. F34：Bounded Parallel Solver Portfolio Racing（2026-07-26）

- 研究问题：F31 建立了 solver portfolio 的 result schema 与 disagreement 语义，但
  attempt 仍按顺序执行，不能体现 SMTgazer/portfolio solving 的核心工程收益：互补
  solver 同时竞争同一 query 的 wall-clock 时间预算。G06 第二阶段目标是在不降低可信
  边界的前提下并发化 backend attempts；
- 技术来源与差异：借鉴 portfolio SAT/SMT solving 与 SMTgazer per-instance scheduling
  的 racing 思路。本阶段实现 bounded parallel racing，但默认等待所有已启动 attempt
  完成后再选择 winner，因此仍能观察 SAT/UNSAT disagreement。它不是 eager
  cancellation，也不是论文级 X-means/BO scheduler；
- Portfolio runner：`PortfolioSolver` 新增 `parallelism` 参数。默认使用
  `SYMCC_QUERY_SOLVER_PORTFOLIO_PARALLELISM`，未设置时等于 configured solver 数，
  上限 64。每个 solver callable 在独立 thread 中执行，attempt telemetry 按配置顺序
  持久化，terminal results 也按配置顺序决定 SAT 优先、UNSAT 其次的 winner；
- Result schema 扩展：`symcc-solver-portfolio-v1` 新增 `mode`、`parallelism` 和
  portfolio-level `elapsed_us`。`elapsed_us` 现在表示整个 race 的 wall-clock 时间，而
  每个 attempt 仍保留自己的 solver-reported 或 measured `elapsed_us`；
- Service 集成：`util/symcc_query_service.py` 新增 `--portfolio-parallelism`，覆盖环境
  默认值。persistent backend 仍由 `ExitStack` 管理生命周期；同一 service worker 对
  一个 query 内的不同 backend 并发调用，不跨 query 重入同一 backend；
- 可信策略：winner/disagreement 规则完全沿用 F31。若任一 backend SAT 且没有 UNSAT，
  选择配置顺序最靠前的 SAT；若只有 UNSAT，选择配置顺序最靠前的 UNSAT；若 SAT 与
  UNSAT 同时出现，返回 `unknown`，不缓存 proof/model，也不 materialize candidate；
- 主要代码：`util/query_store.py`、`util/symcc_query_service.py`；
- 自动化测试：`test/test_query_store.py::test_portfolio_solver_runs_backends_in_parallel`
  用 `threading.Barrier` 证明两个 backend 同时进入；既有 winner telemetry 和
  SAT/UNSAT disagreement 测试继续覆盖 schema 兼容性；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`python3 test/test_query_store.py -v` 11/11 通过；`python3 -m
  py_compile util/query_store.py util/symcc_query_service.py test/test_query_store.py`
  通过；
- 已知局限：Python thread 层无法强杀已经进入外部 persistent solver 的 attempt，故
  本阶段默认等待全部 attempt 完成。F242后续加入opt-in SAT-gated cancellation、
  helper独立process group和bounded disagreement window；默认全收集语义仍保留；
- 下一步：接入 cvc5/Bitwuzla/string backend 的 capability matrix，并把 per-attempt
  status/elapsed/prefix-cache-hit 回灌到 `SMTAlgorithmScheduler` 和 offline PAR-2
  训练轨迹。

## 30. F35：Solver-helper Internal PSCache Assignment Probing（2026-07-26）

- 研究问题：F32 的 partial-solution cache 位于 QueryStore 层，只能在 solver 完成后为
  后续 query 生成 candidate，不能缩短当前 helper 的 solve path。PSCache 的更核心思想
  是把可复用 assignment 带回求解器内部，在进入昂贵 search 前尝试已知 partial model；
- 技术来源与差异：借鉴 PSCache 的 partial assignment reuse，但当前实现采用
  solver-helper 内部 recent-SAT model probe，而不是修改 Z3 CDCL/bit-blast pipeline。
  它只复用已经由 Z3 SAT 结果验证过的 input-byte assignment，并对当前 prefix+target
  solver context 再次 `check()`；probe miss 不产生 UNSAT 结论；
- Server cache：`symcc-query-solver --server` 新增 `PartialSolutionCache`。每个 SAT
  result 的 `assignment_values` 被裁剪到 `SYMCC_SOLVER_PSCACHE_ASSIGNMENTS` 个 input
  byte，并按 LRU-ish front insertion 保留在进程内 deque 中，上限
  `SYMCC_SOLVER_PSCACHE_SIZE`；
- Probe path：persistent server 在 `solver.push(); solver.from_file(target)` 后，正式
  `checkSolver()` 前调用 `tryPartialSolutions()`。它最多尝试
  `SYMCC_SOLVER_PSCACHE_PROBES` 条 cached assignment，每次用
  `SYMCC_SOLVER_PSCACHE_TIMEOUT` 设置短超时，并把 `input_offset == byte` 断言进当前
  solver。若 Z3 返回 SAT，helper 立即返回 `solver=z3-pscache`，并从当前 model 重新
  collect assignments；否则 pop 后继续下一条或完整求解；
- Result telemetry：helper JSON 新增 `solver_pscache_hit` 和
  `solver_pscache_probes`。`QueryStore._validate_result()` 保留这两个字段，
  `stats()` 新增 `solver_pscache_hits`；
- 配置：`SYMCC_SOLVER_PSCACHE`、`SYMCC_SOLVER_PSCACHE_SIZE`、
  `SYMCC_SOLVER_PSCACHE_ASSIGNMENTS`、`SYMCC_SOLVER_PSCACHE_PROBES`、
  `SYMCC_SOLVER_PSCACHE_TIMEOUT`，详见 `docs/Configuration.txt`；
- 主要代码：`runtime/src/backends/qsym/query_solver.cpp`、`util/query_store.py`；
- 自动化测试：新增 `test/query_solver_pscache.py`。第一条 query 求解
  `input[0] == 0x42`，第二条不同 query 为 `0x40 <= input[0] <= 0x50`；persistent
  helper 必须在第二条返回 `solver_pscache_hit=true`、`solver=z3-pscache`，且
  QueryStore stats 中 `solver_pscache_hits=1`。`test/test_query_store.py` 仍覆盖 stats
  schema 兼容；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`cmake --build build --target SymCCRuntime` 通过；`lit -sv
  build/test/query_solver_pscache.py` 通过；`python3 test/test_query_store.py -v`
  11/11 通过；
- 可信边界：solver PSCache hit 只是“当前 query 在 cached assignment 下 SAT”的
  shortcut，不写 UNSAT proof，不更新 UNSAT subset cache，不跳过 Z3 validation，也不
  直接声明 query solved unless Z3 returned SAT under current constraints；
- 已知局限：仍未采集 PSCache 论文中的 conflict assignments、bit-blast internal
  variables、proof-core-minimized partial solution 或 prefix/off-path 子约束索引；
  当前 recent-SAT assignment 对高度变化的 queries 可能只贡献少量 cheap probes；
- 下一步：在 solver helper 中加入 clause-hash/target-shape fingerprints，把 recent
  assignments 从单纯 LRU 提升为 overlap/range-aware selection；长期方向是修改
  vendored Z3 或接入可暴露 assumption/core/model telemetry 的 backend，采集真正
  conflict-derived partial solutions。

## 31. F36：Lase/Cottontail-inspired Token Grammar Solve-Complete Proposals（2026-07-26）

- 研究问题：已有 semantic proposal 能做 byte inverse、data exemplar transfer 和 UCSan
  object partition，但缺少 Lase 式在线 token grammar synthesis 与 Cottontail
  Solve-Complete 的结构化补全路径。G08/G11 的完整实现很大，本阶段先把 token grammar
  completion 接入 verified proposal plane；
- 技术来源与差异：借鉴 Lase 的 online token grammar 与 Cottontail 的
  “约束核心 + 语法补全 + replay 验证”思路。实现只从 AFL extras/string token 和
  comparison-core fragments 合成 bounded grammar rules，不推导完整 CFG，也不调用
  LLM；所有输出仍是 data-only `solve_complete` proposal；
- Grammar synthesis：新增 `TokenGrammarRule` 与 `synthesize_token_rules()`，从 token
  中抽取 `literal`、`delimited`、`key_value` 和路径/点划线 `segment` 规则。规则数量、
  token 长度和渲染长度均有上界；
- Online state：`SemanticProposalGenerator` 维护 `grammar_fragments`，从每次
  comparison core 的输入片段中采样最多 512 条历史 fragment，并写入
  `.semantic_proposal_generator.json`，供后续 seed 使用；
- Solve-complete stage：在 inverse 与 surrogate 之后、UCSan heap partition 之前新增
  `_solve_complete_candidates()`。它使用当前 core span 的形状保留 quote/bracket wrapper
  或 key-value 左侧，再把 token grammar body 填入，生成 kind=`solve_complete` 的
  candidate。候选仍进入 `VerifiedProposalManager`，后续由 target replay、target
  telemetry 和 AFL novelty 决定是否保留；
- 配置：沿用 `SYMCC_SEMANTIC_PROPOSALS` 和 `SYMCC_SEMANTIC_TOKEN_DIR`，详见
  `docs/Configuration.txt`。不新增默认外部依赖；
- 主要代码：`util/semantic_proposals.py`；
- 自动化测试：`test/test_semantic_proposals.py` 新增
  `test_token_grammar_rule_synthesis` 和
  `test_solve_complete_uses_token_grammar_and_core_shape`，覆盖 delimited/key-value/
  segment rule synthesis 与 `""` core 补全为 `"MAGIC"` 的 structured candidate；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`python3 test/test_semantic_proposals.py -v` 5/5 通过；`python3 -m
  py_compile util/semantic_proposals.py test/test_semantic_proposals.py` 通过；
- 可信边界：这是 untrusted proposal plane，不改变 symbolic solver result，不绕过
  `VerifiedProposalManager`，也不把 grammar completion 视为 parser acceptance proof；
- 已知局限：还没有完整 online CFG/nonterminal inference、grammar-rule coverage、
  precision/recall feedback、conflict rule splitting、variable-length append/truncate
  planning 或 history-guided seed acquisition；当前实现是 Lase/Cottontail 的 bounded
  deterministic baseline；
- 下一步：把 G04 string-query metadata、G03 data features 和 ECT node ids 一起写入
  solve-complete proposal metadata，再用 replay 结果学习 grammar-rule success/invalid
  ratio，并为 G11 的 full structured seed acquisition 提供实验数据。

## 32. F37：IFSS/Hydra-style Targeted Transformation Proposals（2026-07-26）

- 研究问题：现有 Backsolver/Veritesting 已能处理受限 side-effect-free region，但还没有
  IFSS worklist、region state merge 和 Hydra 式 targeted control-flow transformation。
  编译器级替换需要完整 LLVM memory/poison/exception 语义，本阶段先把相关思想落到
  verified proposal plane；
- 技术来源与差异：借鉴 IFSS 的 relevance slice、state merge、symbol substitution，
  以及 Hydra 的 failure-preserving targeted transformation。实现不生成或运行变换后
  程序，只在输入数据上进行受限 core fragment 迁移和边界扰动，随后用原程序 replay
  过滤 spurious result；
- Relevance slice：新增 `RelevanceSlice` 与 `ifss_relevance_slices()`，从
  `comparison_taints` 中提取 bounded constraint core，并按 target branch、interesting
  标记、依赖密度和 span 长度排序；
- Targeted transform stage：`SemanticProposalGenerator` 新增
  `_targeted_transform_candidates()`，在 inverse/surrogate 后、solve-complete 前以
  有界预算生成 kind=`targeted_transform` 的候选：
  - `hydra-copy-core`：在等宽 comparison core 间复制高相关 fragment；
  - `hydra-swap-cores`：交换等宽 core fragment，模拟状态合并后的 symbol substitution；
  - `hydra-boundary-zero/ff/highbit/step`：对 slice 边界做 parser/control-flow 常见扰动；
  - `hydra-truncate-after-core`：对 interesting core 做受限 prefix preservation；
- 预算与可信边界：targeted stage 最多占 `max_proposals_per_observation` 的小比例，
  避免饿死 solve-complete/heap-partition baseline。所有候选仍由
  `VerifiedProposalManager` materialize、原程序 target telemetry 验证和 AFL novelty
  triage 决定是否保留；
- 配置：沿用 `SYMCC_SEMANTIC_PROPOSALS`；`SYMCC_VERIFIED_PROPOSALS` 的 accepted kinds
  新增 `targeted_transform`，详见 `docs/Configuration.txt`；
- 主要代码：`util/semantic_proposals.py`、`util/verified_proposals.py`；
- 自动化测试：`test/test_semantic_proposals.py` 新增
  `test_targeted_transform_uses_ifss_relevance_slices`，覆盖 relevance slice 排序、
  `hydra-copy-core` candidate 和 `targeted_transform` ingest；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`python3 test/test_semantic_proposals.py -v` 6/6 通过；
  `python3 test/test_verified_proposals.py -v` 2/2 通过；`python3 -m py_compile
  util/semantic_proposals.py util/verified_proposals.py test/test_semantic_proposals.py
  test/test_verified_proposals.py` 通过；
- 已知局限：尚未实现 compiler/runtime 级 IFSS continuation state、multi-exit region、
  loop summary、MemorySSA alias-sensitive merge、LLVM poison/undef/freeze、transformed
  binary exploration 或 spurious-result taxonomy。当前实现是 SOTA 思想在可信 proposal
  边界内的 aggressive baseline；
- 下一步：把 expensive-branch profiling、Query IR prefix roots、static dependency map
  和 ECT node metadata 写入 transform proposal state，形成可学习的
  transformation-success/invalid-ratio 统计，再评估是否推进真正的 compiler-level
  IFSS/Hydra engine mode。

## 33. F38：GenSym-style Live Continuation Checkpoint Descriptors（2026-07-26）

- 研究问题：`StateShardCoordinator` 已能把 seed/focus/target/action/schedule prefix
  切成可 lease 的 state task，但 G05 需要的是可恢复 symbolic continuation：程序位置、
  path condition、symbolic store、symbolic memory 和 parent delta 都必须有稳定身份。
  本阶段先实现 canonical descriptor 与 checkpoint id，作为后续 live-state engine 的
  控制面 schema；
- 技术来源与差异：借鉴 GenSym continuation-passing symbolic execution 的“可调度
  continuation frame”思想。当前实现不暂停进程、不序列化真实 heap，也不迁移正在
  执行的 solver context；它只定义 future engine 可交给 MPI coordinator 的
  `symcc-live-continuation-v1` 描述；
- Descriptor schema：新增 `ContinuationFrame` 与 `LiveContinuationDescriptor`。
  descriptor 包含 engine、最多 32 个 PC frame、content-addressed
  `path_condition_root`、`symbolic_store_root`、`symbolic_memory_root`、parent checkpoint
  和 target branch。所有 digest 字段必须是 SHA-256 hex，否则被清空；
- Checkpoint identity：`LiveContinuationDescriptor.checkpoint_id()` 对 canonical JSON
  做 SHA-256，保证同一 continuation 在不同 coordinator/path 表示下得到稳定 id；
- State task integration：`StateShardCoordinator.payload_from_item()` 接受可选第 6 个
  tuple 元素作为 continuation descriptor，将 canonical `continuation` 和
  `continuation_id` 写入 `state_payload`，因此 task id、shard owner、lease/recovery 都可
  对 live-state identity 生效；
- 配置：沿用 `SYMCC_STATE_PARALLEL` 控制面；F38 阶段的 replay mode 不传输 live
  memory pages，F59 后可引用 portable page-COW bundle，但仍不具备 native resume；
- 主要代码：`util/distributed_state.py`；
- 自动化测试：`test/test_distributed_state.py` 新增
  `test_live_continuation_descriptor_is_canonical_state_identity`，覆盖 checkpoint id
  稳定性、state-task id 对 PC 变化敏感，以及 descriptor payload 的恢复语义；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`python3 test/test_distributed_state.py -v` 17/17 通过；`python3 -m
  py_compile util/distributed_state.py test/test_distributed_state.py` 通过；
- 后续状态：F59 已补 content-addressed solver frames、symbolic store、COW memory
  pages、fork/restore 和 checkpoint integrity；仍没有 live symbolic VM、
  CPS/native PC-register-call-stack resume、external-resource virtualization、
  state-local execution integration、fault-injection recovery 或 locality-aware
  live-state stealing benchmark。F38 是 G05 的 schema/control-plane 前置层，不是完整
  GenSym engine；
- 下一步：在 compiler/runtime 层产出可恢复 CPS task 或等价 native continuation，
  把 F59 bundle 接入单步/单分支 executor 与 MPI stealing/recovery。

## 34. F39：ConDPOR-style HB/Lockset Schedule Conflict Analysis（2026-07-26）

- 研究问题：已有 bounded-DPOR 只把同一同步对象上的 pair 当作 dependent，缺少
  ConDPOR/DPOR 系列所需的 happens-before、lockset 和普通 shared-memory conflict
  语义。完整 ConDPOR 还需要输入约束和调度约束联合求解，本阶段先补 trace-level
  冲突分类；
- 技术来源与差异：借鉴 ConDPOR 的 concolic data nondeterminism + schedule
  nondeterminism 联合视角，以及 Source-DPOR 的 source backtracking。实现只在 Python
  trace explorer 中做 SC vector-clock 和 lockset 分析，不声称 optimal、complete 或
  memory-model-parametric；
- Trace schema：`parse_schedule_trace()` 继续接受 `seq tid op object`，并扩展支持
  F40 可选发出的 `read`/`write` rows。`symcc_schedule_rt.c` 默认仍只记录 pthread
  同步点；
- HB/lockset：新增 `SchedulePoint`、`annotate_schedule()` 和 `happens_before()`。
  acquire 同步点合并对应 object 的 release clock，release 写回 clock；memory event
  记录当前 thread 持有的 lockset；
- Conflict classification：新增 `ScheduleConflict` 与
  `classify_schedule_conflicts()`。同步冲突保留未 HB ordered 的 same-object controlled
  pair；内存冲突要求 same-object、跨线程、至少一侧 write、无 HB order 且无共同 lock；
- Replay generation：`DporScheduleExplorer.propose_prefixes()` 改由 conflict
  classification 生成 source-style prefix，使 F40 的 `read/write` trace 能直接进入
  bounded replay queue，同时保持原同步点测试兼容；
- 配置：沿用 `SYMCC_DPOR`、`SYMCC_DPOR_MAX_DEPTH`、`SYMCC_DPOR_WINDOW`、
  `SYMCC_DPOR_PREFIXES` 和 `SYMCC_DPOR_PENDING`；
- 主要代码：`util/schedule_exploration.py`；
- 自动化测试：`test/test_schedule_exploration.py` 新增
  `test_happens_before_tracks_lock_release_acquire`、
  `test_memory_conflict_generates_source_backtrack_prefix` 和
  `test_lockset_suppresses_protected_memory_conflict`，覆盖 HB、unprotected memory
  conflict 和共同 lock pruning；原 LD_PRELOAD runtime replay 测试继续通过；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`python3 test/test_schedule_exploration.py -v` 8/8 通过；`python3 -m
  py_compile util/schedule_exploration.py test/test_schedule_exploration.py` 通过；
- 已知局限：没有 vector-clock compression、source-set/sleep-set optimality、
  schedule constraint SMT encoding、输入约束与 schedule 约束联合 query、SC 完整性
  证明或 TSO/RA memory model。F39 是 G10/G13 的
  trace-analysis baseline，不是完整 ConDPOR；
- 下一步：在 F42 bounded joint replay validation 基础上，推进完整 schedule-SMT
  encoding、source/sleep set optimality 和 memory-model proof。

## 35. F40：ConDPOR-style Memory Read/Write Schedule Trace Instrumentation（2026-07-26）

- 研究问题：F39 的 HB/lockset analyzer 已能消费 `read/write` trace rows，但 runtime
  仍只记录 pthread 同步点。要推进 G10/G13，必须让 instrumented target 可选择性输出
  普通 shared-memory access evidence；
- 技术来源与差异：借鉴 ConDPOR 对 data nondeterminism 和 schedule nondeterminism 的
  联合建模。当前实现只提供可采样的 memory-access trace，不做 race detection 结论，
  不声称 source-set optimality，也不把内存冲突直接转化为 bug；
- 编译器插桩：`SYMCC_DPOR_MEMORY=1` 时，`Symbolizer::visitLoadInst()` 与
  `visitStoreInst()` 在非 atomic、非 volatile load/store 周围插入
  `_sym_notify_schedule_read/write(address, byte_length)`。byte length 来自 LLVM
  `DataLayout`，并复用已有 pointer-size lowering；
- Runtime ABI：`compiler/Runtime.*` 导入两个通知函数；
  `runtime/include/RuntimeCommon.h` 声明 ABI；QSYM/simple runtime 提供 no-op 定义，
  确保未 preload schedule runtime 时链接和普通执行保持兼容；
- Preload runtime：`util/symcc_schedule_rt.c` 在运行期
  `SYMCC_SCHEDULE_MEMORY=1` 或 `SYMCC_DPOR_MEMORY=1` 时把通知转成 `read`/`write`
  rows。`SYMCC_SCHEDULE_MEMORY_BYTES` 限制单次 load/store 最多展开多少 byte-granular
  object，默认 8、最大 4096；
- 与 F39 联动：byte-granular object rows 进入 `parse_schedule_trace()`，再由
  SC vector-clock、lockset pruning 和 source-style prefix generator 处理；
- 配置：详见 `docs/Configuration.txt` 的 `SYMCC_DPOR_MEMORY`、
  `SYMCC_SCHEDULE_MEMORY` 和 `SYMCC_SCHEDULE_MEMORY_BYTES`；
- 主要代码：`compiler/Symbolizer.cpp`、`compiler/Runtime.cpp`、`compiler/Runtime.h`、
  `runtime/include/RuntimeCommon.h`、QSYM/simple runtime、`util/symcc_schedule_rt.c`；
- 自动化测试：新增 `test/schedule_memory_trace.c`，编译期打开
  `SYMCC_DPOR_MEMORY=1`，运行期 preload `libsymcc_schedule_rt.so` 并打开
  `SYMCC_SCHEDULE_MEMORY=1`，验证 trace 中出现 `read` 和 `write` rows；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`lit -sv build/test/schedule_memory_trace.c` 通过；`cmake --build
  build --target SymCC SymCCRuntime symcc_schedule_rt -j2` 通过；
- 已知局限：内存 trace 可能很大，当前按访问起始地址展开固定数量 byte；F43 已补
  stack/owner 过滤 baseline，但仍不做对象级 alias recovery、global/static provenance、
  采样自适应、shadow memory ownership、source/sleep set optimality 或 schedule-SMT
  联合验证；
- 下一步：在 F43 provenance filtering 基础上继续加入 object/global/static
  provenance，随后把 F42 validation 扩展为真正的 schedule-SMT encoding 与 concrete
  schedule replay oracle。

## 36. F41：ConDPOR-style Schedule Constraint Artifact Export（2026-07-26）

- 研究问题：F39/F40 已能从同步点和 byte-granular memory trace 生成 source-style replay
  prefix，但 prefix 队列只保存“要重放什么”，没有保存“为什么重放”。这不利于 benchmark
  消融、后续 schedule-SMT encoding，也不利于论文实验复现；
- 技术来源与差异：借鉴 ConDPOR 将输入 nondeterminism 与 schedule nondeterminism
  统一为可检查约束对象的思想。当前实现导出约束证据 artifact，而不是完整 optimal DPOR
  proof：它记录 SC vector-clock/HB、lockset、冲突窗口和 replay prefix 生成结果，为后续
  source/sleep sets、joint SMT validation 和 live-state search 提供稳定边界；
- Schema：新增 `symcc-schedule-constraint-v1` JSONL。每行包含 `input_id`、
  `target_branch`、`trace_digest`、`current_prefix`、bounds、event/conflict 计数、
  sync/memory conflict 计数、bounded schedulable event 摘要、bounded conflict 摘要、
  `replay_prefixes` 和 truncation 标记；
- Prefix 生成一致性：新增 `propose_source_replay_prefixes()`，由 explorer 队列和 artifact
  共同调用，避免实验记录和实际 replay 的 prefix 发生语义漂移；
- Explorer 接入：`DporScheduleExplorer(..., constraint_path=...)` 在 `observe()` 中对每条
  有效 trace 追加 artifact；默认 `constraint_path` 为空，不影响既有 fuzzing 路径；
- MPI 接入：`util/mpi_fuzzing_helper.py` 读取
  `SYMCC_SCHEDULE_CONSTRAINT_OUT=PATH`，master 在处理 worker 回传的 schedule trace
  时把 artifact 写到该 JSONL 路径，并携带当前 target branch 与 replay prefix；
- 主要代码：`util/schedule_exploration.py`、`util/mpi_fuzzing_helper.py`；
- 自动化测试：`test/test_schedule_exploration.py` 新增
  `test_schedule_constraint_artifact_matches_replay_queue`，验证 schema、trace digest、
  memory conflict 计数、artifact JSONL 写出，以及 artifact 中的 replay prefix 与
  explorer pending queue 一致；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`python3 test/test_schedule_exploration.py -v` 9/9 通过；`python3 -m
  py_compile util/schedule_exploration.py util/mpi_fuzzing_helper.py
  test/test_schedule_exploration.py` 通过；
- 已知局限：artifact 仍是 bounded trace evidence，不是完整 schedule constraint SMT。
  F42 已把 Query IR/path roots 与 schedule prefix 做 bounded replay validation，F43 已
  补 stack/owner provenance filter baseline，但没有 source-set/sleep-set optimality、SC
  完整性证明、TSO/RA 模型、object/global/static provenance 或完整联合 SMT proof。

## 37. F42：Query IR × Schedule Artifact Joint Replay Validation（2026-07-26）

- 研究问题：F41 已保存 schedule conflict 和 replay prefix 证据，但它仍和输入约束
  分离。ConDPOR-style 系统需要回答“这个 schedule prefix 是否对应某个可达 path
  condition/query root”，否则调度探索和输入求解很难做联合消融；
- 技术来源与差异：借鉴 ConDPOR 的 data nondeterminism + schedule nondeterminism
  联合建模，但当前实现是 bounded replay validation baseline：它把 schedule artifact
  与 Query IR evaluator/solver result 对齐，不编码完整 schedule SMT，也不证明
  optimal DPOR；
- Schema：新增 `symcc-joint-schedule-query-v1`。每条记录包含 schedule trace digest、
  target branch、current/replay prefixes、conflict 计数、query id、prefix/target hash、
  query metadata、matching source、path validation status 和 joint status；
- Query 匹配：`QueryStore.validate_schedule_artifact()` 优先使用 artifact 中的
  `query_id/query_ids`。否则按 `target_branch` 与 Query IR metadata 的 `site`、
  `target_branch`、`target_site` 或 `branch` 匹配，并用 `max_queries/max_scan` 做
  上界；
- Path validation：对 pending query，bounded evaluator 检查 concrete witness 是否
  satisfied prefix roots、是否已经 satisfied prefix+target roots。对 SAT result，先用
  QueryStore 的 result normalizer 和 assignment sets（含 generator verified models），
  将 assignment patch 到 witness 后再执行 Query IR evaluation。UNSAT result 保守记录
  `path_unsat`；
- Joint status：`ready` 表示存在 replay prefix 且某个 solver/witness candidate 通过
  Query IR roots；`path_target_unsolved` 表示 witness 观察到 prefix 但 target 仍未验证；
  `path_unsat` 表示 path side 已 UNSAT；`schedule_no_replay` 表示 schedule artifact 没有
  replay prefix；其余为 `path_unknown`；
- 持久化：新增 SQLite 表 `joint_schedule_validations`，并把 canonical JSON 写到
  `$SYMCC_QUERY_STORE/joint_schedule/<hash-prefix>/<hash>.json`。可选
  `SYMCC_SCHEDULE_QUERY_VALIDATION_OUT`/`--schedule-validation-out` 追加 JSONL；
- 服务接入：`symcc_query_service.py` 新增 `--schedule-constraints`、
  `--schedule-validation-out`、`--schedule-validation-max-artifacts` 和
  `--schedule-validation-only`。MPI helper 在 `SYMCC_ASYNC_QUERY_WORKERS>0` 且
  `SYMCC_SCHEDULE_CONSTRAINT_OUT` 设置时自动把该文件传给 query service；
- 主要代码：`util/query_store.py`、`util/symcc_query_service.py`、
  `util/mpi_fuzzing_helper.py`；
- 自动化测试：`test/test_query_store.py` 新增
  `test_schedule_artifact_joint_query_validation`，覆盖 pending query 的 prefix-observed
  状态、SAT result 的 Query IR verified `ready` 状态、explicit query-id 匹配、
  no-replay 状态、JSONL 输出和 QueryStore stats；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`python3 test/test_query_store.py -v` 12/12 通过；`python3 -m
  py_compile util/query_store.py util/symcc_query_service.py util/mpi_fuzzing_helper.py
  test/test_query_store.py` 通过；
- 已知局限：仍没有真正的 schedule-SMT encoding、schedule equivalence proof、
  source/sleep set optimality、SC/TSO/RA memory-model proof、跨 worker live-state
  validation 或 concrete schedule replay oracle。`ready` 只表示“输入侧 Query IR 已验证
  且存在 bounded schedule replay prefix”，不是完整 ConDPOR 可达性定理。

## 38. F43：Schedule Memory Provenance Filtering（2026-07-26）

- 研究问题：F40 的 `SYMCC_DPOR_MEMORY` 可以把普通 load/store 转成 byte-granular
  schedule trace，但真实程序会产生大量明显 thread-local 访问。F41/F42 已经把 trace
  evidence 接入 artifact 与 Query IR validation，因此下一步需要在 runtime 侧降低噪声，
  否则 benchmark 的 trace size、DPOR window 和 JSONL artifact 都会被无共享证据的内存
  rows 淹没；
- 技术来源与差异：借鉴 ConDPOR/data-race 工具常用的 ownership/provenance pruning
  思想，但当前实现是 conservative engineering filter，不是 sound alias analysis。默认
  关闭，只有显式配置后才改变 memory row 输出；
- Stack filter：`SYMCC_SCHEDULE_MEMORY_FILTER=stack` 或
  `SYMCC_SCHEDULE_MEMORY_STACK=1` 时，preload runtime 使用
  `pthread_getattr_np(pthread_self())` 与 `pthread_attr_getstack()` lazy cache 当前线程栈
  范围，跳过落在当前线程栈范围内的 `_sym_notify_schedule_read/write` byte rows；
- Dynamic owner filter：`SYMCC_SCHEDULE_MEMORY_FILTER=owner/shared/dynamic` 或
  `SYMCC_SCHEDULE_MEMORY_OWNER=1` 时，runtime 维护 bounded byte-address owner table。
  首次同线程访问只记录 owner/last-op，不写 trace；同一 owner 的重复访问继续被压缩；
  第一次跨逻辑线程访问同一 byte 时标记为 shared，并在至少一侧为 write 时输出一条
  compressed prior-access row 加当前 row，使 Python HB/lockset analyzer 仍能看到可生成
  replay prefix 的 read/write 证据；
- Bounds：`SYMCC_SCHEDULE_MEMORY_OWNERS` 控制 owner table 大小，默认 65536、最大
  1048576。probe window 命中失败时回退为直接记录当前访问，避免因过滤器容量不足
  静默丢失共享证据；
- Replay 兼容性：真实当前 memory row 仍经过 `controlled_event()` 和 logical-thread-id
  prefix gate；compressed prior-access row 只作为 evidence 写出，不参与 gate，因为它
  代表已经发生的首次 owner-side access；
- 配置：详见 `docs/Configuration.txt` 的 `SYMCC_SCHEDULE_MEMORY_FILTER`、
  `SYMCC_SCHEDULE_MEMORY_STACK`、`SYMCC_SCHEDULE_MEMORY_OWNER` 和
  `SYMCC_SCHEDULE_MEMORY_OWNERS`；
- 主要代码：`util/symcc_schedule_rt.c`；
- 自动化测试：`test/test_schedule_exploration.py` 新增两个 runtime tests。第一个手动
  调用 `_sym_notify_schedule_read/write`，验证 `stack` filter 不记录当前线程栈地址但
  保留 global 地址；第二个用两个 pthread 对同一 global byte 做 write/read，验证
  `owner` filter 在首次跨线程共享时输出 compressed write/read transition；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`python3 test/test_schedule_exploration.py -v` 11/11 通过；
  `cmake --build build --target symcc_schedule_rt -j2` 通过；`lit -sv
  build/test/schedule_memory_trace.c` 通过；全量 `cmake --build build --target check
  -j2` 87/87 通过；
- 已知局限：owner table 以运行时地址为 key，不做 object/field identity、heap lifetime、
  ASLR-stable global id、static LLVM provenance、alias recovery、sampling controller 或
  memory-model proof；跨线程首次共享前的历史访问是 compressed evidence，不是完整
  trace reconstruction。

## 39. F44：Schedule Memory Provenance Tags and Artifact Propagation（2026-07-26）

- 研究问题：F43 能减少明显 thread-local memory rows，但后续 benchmark 仍需要知道
  进入 DPOR analyzer 的 memory rows 大致来自 stack、heap、module/static mapping 还是
  anonymous memory。否则 trace reduction、object provenance、schedule-window tuning 和
  Query IR joint validation 很难做分层消融；
- 技术来源与差异：借鉴并发测试与 race detector 中的 memory-region provenance
  metadata。当前实现只追加 bounded evidence tag，不建立 sound object identity，也不
  把 provenance tag 用作 correctness proof；
- Trace tag：`SYMCC_SCHEDULE_MEMORY_PROVENANCE=1` 时，`symcc_schedule_rt.c` 对
  memory read/write row 追加兼容第五列，如 `prov=stack`、`prov=heap`、
  `prov=module`、`prov=anonymous`、`prov=system` 或 `prov=unknown`。旧 parser 仍可按
  前四列读取；
- Runtime classification：stack 仍来自 lazy cached pthread stack bounds；非 stack
  地址用 bounded `/proc/self/maps` lookup 分类。`SYMCC_SCHEDULE_MEMORY_FILTER=tag`、
  `tags` 或 `provenance` 也可打开 tag 输出；
- Python parser：`ScheduleEvent` 新增 `tags` 字段；`parse_schedule_trace()` 保存第 5
  至第 12 个 token；`schedule_trace_digest()` 把 tags 纳入 digest，保证带 provenance
  和不带 provenance 的 trace artifact 可区分；
- Artifact propagation：`schedule_constraint_artifact()` 新增 `provenance_counts`，
  同时每个 event mapping 带可选 `tags`；`QueryStore.validate_schedule_artifact()`
  保留 `provenance_counts` 并写入 `symcc-joint-schedule-query-v1`；
- 主要代码：`util/symcc_schedule_rt.c`、`util/schedule_exploration.py`、
  `util/query_store.py`；
- 自动化测试：`test/test_schedule_exploration.py` 新增 parser/artifact tag 测试和
  runtime provenance tag 测试，验证 local stack row 带 `prov=stack`，global row 至少带
  一个 `prov=` tag；`test/test_query_store.py` 验证 joint validation 不丢失
  `provenance_counts`；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`python3 test/test_schedule_exploration.py -v` 13/13 通过；
  `python3 test/test_query_store.py -v` 12/12 通过；`lit -sv
  build/test/schedule_memory_trace.c` 通过；Python unittest sweep 24/24 通过；
  全量 `cmake --build build --target check -j2` 87/87 通过；`python3 -m py_compile
  util/schedule_exploration.py util/query_store.py test/test_schedule_exploration.py
  test/test_query_store.py` 通过；
- 已知局限：`prov=module` 仍是 process mapping 级别，不是 compiler-emitted
  global/static object id；`/proc/self/maps` lookup 是可选慢路径，适合 profiling 和
  benchmark evidence，不适合默认高吞吐 fuzzing；尚未建 heap allocation lifetime、
  object/field identity、ASLR-stable global ids 或 alias-aware memory partitions。

## 40. F45：Bounded SC Schedule-SMT Replay-Prefix Encoding（2026-07-27）

- 研究问题：F41 已导出 schedule conflict/replay-prefix evidence，F42 已把该 evidence
  与 Query IR/result 做记录级联合验证，但 replay prefix 本身还不是求解器可检查的
  调度约束。需要一个稳定、可复现、能由 Z3 等 SMT-LIB2 solver 直接解析的中间层，
  同时不能把缺失 enabledness、read-from 和 path semantics 的工程子集误称为完整
  ConDPOR proof；
- 技术来源与差异：借鉴 ConDPOR 对 data/schedule nondeterminism 进行约束化建模的
  思路，以及 bounded model checking 常用的 event-position encoding。当前实现是
  SC structural-admissibility baseline，不是 sound/complete/optimal DPOR：每条 query
  证明或否定的是一个有界 partial-order skeleton，而不是具体程序状态中的调度可达性；
- Schema：新增 `symcc-schedule-smt-v1` JSONL。顶层记录 input/branch/trace identity、
  SC memory-model 标签、encoding/version、hard/optional/not-encoded 语义清单、线程和
  event 统计、bounds、candidate/query/drop 计数、truncation 状态及 artifact SHA-256；
  公共部分保存 `base_smt2`/hash 和可直接运行的 `incremental_smt2`/hash，每个 query
  保存 replay prefix、source conflict、PO 计数、`delta_smt2`/hash 及物化后独立
  query 的 hash；
- Event-position encoding：对 bounded schedulable events 建立 `pos_i : Int`，
  约束 `0 <= pos_i < N` 与 `distinct(pos_0...pos_N)`，从而得到事件位置排列；同一
  logical thread 的相邻 modeled events 加入命名 `po_i_j` 程序序约束；
- Prefix/conflict encoding：replay prefix 中某 thread 的第 k 次选择直接绑定该线程
  的第 k 个 modeled event，避免对同线程全部事件构造冗余析取；同时加入
  `conflict_reversal`，要求生成该 prefix
  的 source conflict 右端事件先于左端事件。这个显式反序避免“prefix 字面满足但目标
  conflict 没有真正翻转”的弱约束；
- HB 语义边界：vector-clock HB 从包含 unlock/signal 等 release event 的完整 trace
  计算，但在 SMT 中编码为 `observed_hb_i_j` Boolean assumption implication，默认不
  强制。原因是观测 execution 的动态 synchronization edge 可能在替代 schedule 中
  改变；每条 query 直接列出 `observed_hb_assumptions` 名称，消费者可在诊断观测
  等价性时选择 `check-sat-assuming`，结构候选筛选则只使用 schedule-invariant PO
  hard constraints；
- Incremental context reuse：公共 declaration、event bounds、permutation、PO 与
  observed-HB guards 只生成一次 `base_smt2`；每个候选仅保存 `delta_smt2`。artifact
  同时提供 `incremental_smt2`，用 `push/check-sat/pop` 在同一 solver context 中依次
  检查候选，并提供 `materialize_schedule_smt_query()` 在需要时拼出独立 query；
- Bounds 与可观察失败：`SYMCC_SCHEDULE_SMT_MAX_EVENTS` 默认 128、范围 2--512，
  `SYMCC_SCHEDULE_SMT_MAX_QUERIES` 默认 64、范围 1--512。
  source conflict endpoint 超出 modeled event window 时不生成残缺 query，而是增加
  `dropped_event_bound_count`；超过 query evidence budget 时增加
  `dropped_query_limit_count`。二者汇总到 `dropped_query_count` 并设置相应
  truncation flags，但 query budget 不改变 explorer 的 replay queue；
- MPI 接入：`DporScheduleExplorer` 新增 `smt_path/smt_max_events`，MPI master 读取
  `SYMCC_SCHEDULE_SMT_OUT`、`SYMCC_SCHEDULE_SMT_MAX_EVENTS` 与
  `SYMCC_SCHEDULE_SMT_MAX_QUERIES`。默认输出路径为空，
  因而既有 fuzzing 热路径不增加 SMT artifact 体积；
- 主要代码：`util/schedule_exploration.py`、`util/mpi_fuzzing_helper.py`；
- 自动化测试：`test/test_schedule_exploration.py` 覆盖 deterministic artifact hash、
  event bounds/permutation、program order、thread-prefix、conflict reversal、完整
  trace HB assumption、shared/incremental context、JSONL explorer export、
  event-window truncation 与 per-trace query budget；测试通过
  `ctypes` 调用系统 `libz3` 的 `Z3_eval_smtlib2_string`，实际解析并求解生成的
  SMT-LIB2，结果为 SAT；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`python3 test/test_schedule_exploration.py -v` 18/18 通过；
  `python3 -m py_compile util/schedule_exploration.py util/mpi_fuzzing_helper.py
  test/test_schedule_exploration.py` 通过；生成 query 已由系统 libz3 真实 parse/solve；
  Python unittest discover 169/169 通过；`cmake --build build --target check -j2`
  87/87 通过；`git diff --check` 通过；
- Artifact 体积压力检查：对 128 个 same-object write events、4 个 logical threads、
  256 个 source candidates 的确定性合成 trace，原始“每候选复制完整 query”JSON 为
  65,752,864 bytes；shared base + exact next-event delta + 默认 64-query evidence
  budget 后为 558,547 bytes，降低约 99.15%。这是 formula-volume 工程测量，不是
  coverage/throughput benchmark，因而不据此升级为 R 级证据；
- 已知局限：hard constraints 尚未编码 runtime enabledness、mutex ownership/
  condition wakeup、read-from/coherence、alias/object equivalence、Query IR path roots
  或 TSO/RA axioms；SAT 只表示当前 SC partial-order skeleton 结构可容纳候选，
  UNSAT 只对已编码硬约束有效。下一步应把 Query IR expression roots 与调度状态转移、
  object identity 和 concrete replay oracle 纳入同一 solver query，再讨论 SC 的
  soundness/completeness，之后才能扩展到 TSO/RA 与 optimal DPOR。

## 41. F46：Mutex/RWLock Lifecycle-State Schedule-SMT v2（2026-07-27）

- 研究问题：F45 的 SC event-position model 可以判断 replay prefix、程序序和目标
  conflict reversal 是否结构相容，但它不知道一次成功 lock 已经拥有对象，也不知道
  unlock 之前另一个不兼容 acquire 不能成功。于是某些结构 SAT model 仍可能包含
  mutex/rwlock 临界区重叠。F46 的目标是利用 preload 已经记录的 outcome/release
  evidence 补上有界同步状态，同时不因 trace 截断产生错误 UNSAT；
- Runtime 证据语义：`symcc_schedule_rt.c` 在 pthread 调用前记录受控
  `lock/trylock/rdlock/wrlock`，调用返回后记录 `acquire` 或 `*_fail`，释放调用前记录
  `unlock/rwunlock`。F46 不修改该 ABI，而是在 Python artifact 层按
  `(logical_tid, object)` 配对 attempt、outcome 和 release；
- Schema 演进：当前 exporter 输出 `symcc-schedule-smt-v2`，encoding 标记为
  `bounded-sc-sync-state-v2`。`materialize_schedule_smt_query()` 同时接受 v1/v2，
  保留已经产生的 F45 artifact 的物化兼容性；
- 双位置空间：F45 的 `pos_i` 继续表示可被 prefix 选择的 lock/memory schedule
  points；F46 为 attempt/outcome/release lifecycle 建立 `sync_ord_i : Int` 的独立
  bounded permutation，并施加同线程 lifecycle program order。所有同时进入两个
  空间的受控 lock attempts 用 pairwise order-equivalence 连接，避免 prefix 顺序和
  同步状态顺序各自形成互不相干的 model；
- Critical-section recovery：成功 `acquire` 继承其 pending attempt 的 mode：
  mutex/trylock 为 `mutex_write`，rdlock 为 `rw_read`，wrlock 为 `rw_write`；
  随后的匹配 `unlock/rwunlock` 关闭区间。artifact 的 `sync_state.sections` 保存
  attempt/acquire/release seq、lifecycle index 和 SMT variable name，便于实验复核；
- Hard enabledness subset：只有 acquire 与 release 都位于 bounded lifecycle window
  的完整区间参与 hard constraints。同对象、跨线程且至少一个区间不是 `rw_read` 时，
  编码 `release_A < acquire_B OR release_B < acquire_A`；两个 rwlock reader 允许
  重叠。open、unmatched 或因 window 截断的区间只计数，不加入 exclusion，从而避免
  用不完整 trace evidence 证明伪 UNSAT；
- Trylock failure：`trylock_fail` 的“attempt 被某个不兼容完整区间覆盖”编码为
  `sync_trylock_busy_i` optional assumption。默认 structural replay query 不断言它；
  需要诊断观测一致性时，消费者可以显式 assert/`check-sat-assuming`。metadata 保存
  failure 对应的 thread/object/seq/SMT variable、assumption name 和候选区间数；
- HB/lockset 修复：`rwunlock` 加入 `RELEASE_OPS`。此前 Python analyzer 会在
  rwlock 释放后继续把对象保留在 held lockset，可能错误抑制后续 memory conflict；
  新回归验证释放后的访问不再继承该 lockset；
- Bounds：同步 lifecycle window 取 `3 * max_events` 并封顶 512，用于容纳常见的
  attempt/outcome/release 三元组。`sync_state` 明确记录原始/建模 lifecycle 数、
  complete/open/pending/unmatched 数、order-link/exclusion 数和 truncation；
- 配置与 MPI 接入：新增 `SYMCC_SCHEDULE_SMT_SYNC_STATE=0/1`，默认 1。MPI master
  把该值传入 `DporScheduleExplorer.smt_sync_state`；设为 0 时保留 F45 的结构模型，
  但仍输出 v2 schema 并在 `not_encoded` 标注 ownership state 缺失；
- 主要代码：`util/schedule_exploration.py`、`util/mpi_fuzzing_helper.py`；
- 自动化测试：`test/test_schedule_exploration.py` 新增五类验证：
  1）两个完整 mutex 区间恢复为一个 exclusion，人工强制交叠后系统 libz3 返回
  UNSAT，而正常 replay query 为 SAT；2）两个 rdlock reader 人工交叠仍为 SAT；
  3）trylock busy assumption 在合理覆盖下 SAT，强制 attempt 位于 release 后则
  UNSAT；4）`rwunlock` 后的 memory access 不再被旧 lockset 错误保护；5）关闭
  sync-state 时不生成 `sync_ord_*`，且 v1 artifact 仍可物化。真实 preload
  mutex replay 测试也会把 runtime trace 转成 v2 state，验证至少两个完整区间、
  exclusion constraint 和 base SMT SAT；
- 当前结果等级：I/T，SOTA 审计证据等级 E；
- 定量功能结果：`python3 test/test_schedule_exploration.py -v` 23/23 通过；
  `python3 -m py_compile util/schedule_exploration.py util/mpi_fuzzing_helper.py
  test/test_schedule_exploration.py` 通过；Python unittest discover 174/174 通过；
  `cmake --build build --target check -j2` 87/87 通过；定向
  `lit -sv build/test/schedule_memory_trace.c` 通过；`git diff --check` 通过；
- Formula-volume 压力检查：确定性构造 64 个完整 mutex sections、4 个 logical
  threads、同一 object，恢复 1536 个 exclusion constraints；artifact JSON
  1,214,525 bytes，生成约 8.8 ms，base SMT 567,842 bytes，系统 libz3 单次
  parse/solve 为 SAT、约 0.55 s。该结果用于确认默认 bounds 下公式规模，不作为
  coverage/throughput 的 R 级实验结论；
- 已知局限：这是 complete-section nonoverlap baseline，不是完整 pthread operational
  semantics。尚未编码 condition wait 的 mutex 释放/重获和 wakeup choice、join/create
  enabledness、mutex type/recursive/error-check、rwlock upgrade/fairness、spurious
  wakeup、取消/异常退出、memory read-from/coherence、对象跨运行稳定 identity，亦未
  与 Query IR path roots 在同一 solver 中求解。只有 hard exclusion 子集的 UNSAT
  可按该子集解释，不能宣称 SC sound/complete 或 ConDPOR optimal。

## 42. F47：Condition-Variable Operational Schedule-SMT v3（2026-07-27）

- 研究问题：`pthread_cond_wait(cond, mutex)` 不是普通阻塞事件。调用者必须在进入
  wait 时原子地释放关联 mutex，并在返回前重新获得它。F46 只看到 condition
  object 上的 `wait/wake`，因此会错误地把 wait 前持有的 mutex 延伸到整个阻塞区间，
  既可能抑制真实 memory conflict，也无法判断某个 `signal/broadcast` 是否位于可解释
  唤醒的 release/reacquire 窗口。F47 把这段隐式 pthread 状态变化显式化；
- Runtime trace ABI：`symcc_schedule_rt.c` 在受控 `wait` 后、进入 libc wait 前记录
  `wait_mutex_release <mutex>`；正常返回或 `ETIMEDOUT` 后记录
  `wait_mutex_acquire <mutex>`，再分别记录 `wake` 或 `wait_timeout <cond>`。其他错误
  记录 `wait_fail`，不虚构 mutex 已重获。该设计保留 condition 和 mutex 两种 object
  identity，而不是把二者拼成不可分析的字符串；
- HB/lockset 语义：`annotate_schedule()` 把 `wait_mutex_release` 当作 mutex release，
  更新 release clock 并移出 held lockset；把 `wait_mutex_acquire` 当作 mutex acquire，
  合并 release clock 并恢复 held lockset。成功 `wake` 不会无条件合并 condition
  上时间最近的 `signal/broadcast`：该 signal 可能已丢失，wake 也可能是伪唤醒；
  因而 signal-to-wake 因果只由后述 optional witness 表达，避免错误 HB pruning。
  wait 阻塞区内也不再错误携带 mutex lockset；
- Schema 演进：新 exporter 输出 `symcc-schedule-smt-v3`，encoding 为
  `bounded-sc-lock-condition-state-v3`；`materialize_schedule_smt_query()` 继续接受
  v1/v2，避免已有实验 artifact 失去物化能力。`SYMCC_SCHEDULE_SMT_SYNC_STATE=0`
  仍可关闭全部 lifecycle-state 扩展，但输出 schema 保持 v3；
- Wait recovery：Python 层按 logical thread 关联
  `wait -> wait_mutex_release -> wait_mutex_acquire -> wake/timeout/fail`。release 关闭
  wait 前的 mutex critical section，reacquire 建立 origin 为
  `condition_reacquire` 的新 section，随后由普通 `unlock` 或下一次 wait release
  关闭。循环谓词导致的多次 wait 因而形成连续而不重叠的所有权区间；
- 双位置空间连接：condition `wait` 是可被 replay prefix 选择的 `pos_*` event，
  同时也属于 `sync_ord_*` lifecycle。v3 将所有同时出现在两种空间的 controlled
  lifecycle event 做 pairwise order-equivalence，不再只连接 lock attempts；
- Wake witness：每个完整成功 wait 声明 optional Boolean
  `cond_wake_signal_i` 和 integer `cond_witness_i`。断言该 assumption 时，witness
  必须选择相同 condition 上的一次 `signal` 或 `broadcast`，且严格满足
  `wait_mutex_release < witness < wait_mutex_acquire`。没有候选时 assumption 推导
  `false`，但默认 base query 仍允许 POSIX 伪唤醒；
- Signal/broadcast 基数：同一次 plain `signal` 不能同时成为两个已断言成功 wait 的
  witness；SMT 中对共享 signal candidate 加条件化 one-to-one 约束。
  `broadcast` candidate 可被多个 wait 复用。`wait_timeout` 不创建 wake witness，
  但仍恢复 mutex ownership；
- 不完整证据策略：只有 release、reacquire 和 outcome 都位于 bounded lifecycle
  window 的 wait 才产生 wake witness。缺失 marker/outcome、截断 wait 和 unmatched
  event 只进入 metadata，不生成会造成假 UNSAT 的 hard constraint。
  `sync_state.condition_waits` 保存 condition/mutex、四阶段 seq/index/SMT variable、
  完整性、候选和 assumption；同时统计 complete/success/timeout/unmatched 及 signal
  uniqueness 数量；
- 主要代码：`util/symcc_schedule_rt.c`、`util/schedule_exploration.py`；
- 自动化测试：`test/test_schedule_exploration.py` 增加四组机制验证：
  1）单 wait 的 mutex split 恢复两个 section，合法 signal witness 为 SAT，强制
  signal 位于 reacquire 后为 UNSAT；2）两个 wake 同时选择一个 signal 为 UNSAT，
  改为 broadcast 后为 SAT；3）timedwait 超时恢复 mutex section 但不生成 signal
  assumption；4）编译并通过 `LD_PRELOAD` 运行真实 pthread condition 程序，检查
  四阶段 trace、完整 section、signal candidate 和 libz3 SAT；5）运行真实
  `pthread_cond_timedwait` 超时路径，确认 reacquire marker、timeout outcome 和两个
  mutex sections；
- 当前结果等级：I/T，SOTA 审计证据等级 E。这里的创新点是把真实 runtime
  wait/mutex evidence、临界区恢复和可开关唤醒因果统一在可增量复用的 schedule-SMT
  context 中；尚不能据此声称完整 ConDPOR；
- 定量功能结果：`python3 test/test_schedule_exploration.py -v` 29/29 通过；
  `python3 -m py_compile util/schedule_exploration.py
  test/test_schedule_exploration.py` 与 preload runtime 独立编译通过；全部新增
  SAT/UNSAT 判据均由系统 libz3 实际 parse/solve，不以字符串匹配代替求解；
- 完整回归：Python unittest discover 180/180 通过；
  `cmake --build build --target check -j2` 87/87 通过；
  `git diff --check` 通过；
- Formula-volume 压力检查：构造 32 个 waiters、一个 broadcast、129 个 lifecycle
  events，并同时断言 32 个 wake assumptions；artifact JSON 为 244,924 bytes，
  base SMT 为 82,374 bytes，生成约 6.6 ms，系统 libz3 返回 SAT、约 0.99 s。
  这是公式规模工程测量，不是 coverage/throughput 的 R 级 benchmark；
- 已知局限：runtime marker 位于 libc wait 调用边界，尚未证明与不同 libc 的内部
  cancellation/error path 等价；伪唤醒只通过“不默认断言 witness”保留，未建模其
  原因；尚未编码 join/create enabledness、mutex type/recursive/error-check、
  rwlock fairness/upgrade、cancellation cleanup、read-from/coherence、跨运行稳定
  object identity、weak-memory axioms，亦未把 Query IR path roots 与 schedule
  transition 放进同一求解上下文。v3 的 UNSAT 只能解释为已编码有界子集不可满足。

## 43. F48：Thread Create/Join Operational Schedule-SMT v4（2026-07-27）

- 研究问题：F47 以前的 runtime 在成功 `pthread_create` 后把调用者提供的
  `pthread_t *` 存储地址记为 `create` object，而 `pthread_join` 使用 opaque
  `pthread_t` 值作为 object；两者通常不相等，也随进程地址和 libc 表示变化。
  trace 因而无法回答某个 join 等待哪一个 logical child，也没有 thread termination
  marker，无法建立 create/start 或 exit/join 的 operational enabledness；
- 稳定 identity：runtime 在 create 前分配单调 logical child id，并以
  `0x<logical-id>` 统一标识 `create`、`create_success/create_fail`、
  `thread_start/thread_exit`、`join` 和 `join_success/join_fail`。SMT identity 不再
  依赖 ASLR、调用者局部变量地址或 opaque pthread handle；
- Create 边界：pre-call `create` marker 必定早于子线程执行；调用返回后记录
  `create_success` 或 `create_fail`。子线程完全可能在父线程获得 success outcome
  之前进入 `thread_start`，因此 v4 只硬约束 `create < thread_start`，不错误要求
  `create_success < thread_start`；
- Exit 完整性：trampoline 在调用用户 start routine 前压入 pthread cleanup handler。
  正常 return 通过 `pthread_cleanup_pop(1)` 执行它，直接 `pthread_exit` 和
  cancellation 展开 cleanup stack 时也执行它，因此 `thread_exit` 位于用户 cleanup
  之后、线程真正终止之前。该 marker 捕获子线程完成时的 vector-clock frontier；
- Join identity registry：runtime 用固定 4096-entry 表保存
  `(pthread_t handle, logical child id)`，查找使用 `pthread_equal` 并由 atomic flag
  保护。mapped join 以 logical id 记录，成功返回后释放表项；查找失败保留实际 handle
  且标 `mapped=0`，Python 不将其猜测为任何 child。这里记录的是 F48 当时状态；
  detached retirement、create/register race 与 join cancellation 后续由 F55 修复；
- Schema 演进：exporter 输出 `symcc-schedule-smt-v4`，encoding 为
  `bounded-sc-lock-condition-thread-state-v4`；materializer 继续接受 v1/v2/v3。
  `sync_state.semantics` 升级为 `lock-condition-thread-operational-state-v3`；
- Lifecycle recovery：bounded trace 按 stable object 恢复 parent/create outcome、
  child start/exit、joiner/join outcome，并验证 start/exit 的 event tid 与 object
  logical id 一致。metadata 保存各阶段 seq、`sync_ord_*` variable、mapped flag、
  identity consistency、complete 状态和 unmatched 计数；
- Hard operational subset：有 create 与一致 child start 时加入
  `create < thread_start`；mapped successful join 且存在目标 exit 时加入
  `thread_exit < join_success`。join attempt 可以在 exit 前发生并阻塞；create/join
  failure、unmapped handle、缺失 exit、identity mismatch 和 window truncation只计数，
  不制造 completion constraint，保持 incomplete evidence 下的保守 SAT；
- HB/DPOR：`annotate_schedule()` 在 child `thread_start` 合并 parent create clock，
  在 parent `join_success` 合并 target exit clock。因此 create 前的父线程访问 HB
  child，child 退出前的访问 HB join 后的父线程访问；已 join 的同地址访问不再被
  错误分类为可交换 memory conflict；
- Bounds：thread lifecycle 使一次受控 join 最多伴随 create/outcome/start/exit/join
  outcome 等证据，lifecycle window 从 `3 * max_events` 提高到
  `5 * max_events`，仍封顶 512。open/unmatched/truncated 统计保留在 artifact；
- 主要代码：`util/symcc_schedule_rt.c`、`util/schedule_exploration.py`；
- 自动化测试：`test/test_schedule_exploration.py` 新增四类验证：
  1）child start 早于 create return 且 join attempt 早于 exit 为 SAT；
  2）强制 start 早于 create 或 join_success 早于 exit 均为 UNSAT；
  3）create failure、unmapped join 和缺失 exit 不生成错误 completion constraint；
  4）真实 preload 目标由 worker 调用 `pthread_exit`，验证六类事件共享同一 stable
  object、mapped tags、完整 thread/join metadata 和 libz3 SAT。另有 vector-clock
  回归证明 joined memory conflict 被 HB 抑制；
- 当前结果等级：I/T，SOTA 审计证据等级 E。`python3
  test/test_schedule_exploration.py -v` 33/33 通过；Python/C 严格编译与系统 libz3
  SAT/UNSAT 求解均通过；
- 完整回归：Python unittest discover 184/184 通过；
  `cmake --build build --target check -j2` 87/87 通过；
  `git diff --check` 通过；
- Formula-volume 压力结果：64 个完整 threads 与 joins 产生 384 个 lifecycle
  events、64 个 spawn constraints 和 64 个 join completion constraints。artifact
  JSON 为 1,047,123 bytes，base SMT 为 468,265 bytes，生成约 15.8 ms，系统 libz3
  返回 SAT但耗时约 13.5 s。该结果暴露 `sync_ord_*` 全排列在高线程数下的主要瓶颈，
  不是性能收益结论；后续应研究 partial-order clock、interval order 或分层 context；
- F48 当时局限：4096-entry registry 对 detached threads 不回收，join cancellation
  只留下无 outcome evidence；这些项已由 F55 的预留/绑定/退休状态机和
  `join_cancelled` marker 修复。仍未建模 double join、detach/join 未定义竞态、
  process-shared synchronization、fork 后线程状态、线程优先级或公平性。
  create/join hard constraints 仍处于 bounded SC total-order context，尚无
  sound/complete/optimal DPOR 证明，也尚未与 Query IR path roots、read-from/coherence
  和 weak-memory axioms联合求解。v4 UNSAT 只对已编码 operational subset 有效。

## 44. F49：Lifecycle Partial-Order / Linear-Extension Schedule-SMT v5（2026-07-27）

- 研究问题：F48 为 384 个 lifecycle events 同时声明整数位置范围和全局
  `distinct`，即使 operational constraints 只需要比较少量先后关系，也强制 Z3
  构造一个完整排列。64-thread 压力样例的单次 SAT 求解约 13.5 s，表明瓶颈来自
  lifecycle 全排列，而不是 create/join 约束本身；
- 约束语言审计：当前 `sync_ord_*` 只参与严格先后边、区间不相交的二选一方向、
  condition witness 候选选择，以及 controlled event 与 `pos_*` 的相对次序连接。
  它不使用 rank 加减、相邻位置、绝对槽位或“恰好第 k 个 lifecycle event”等性质。
  因此任一满足赋值定义一个无环严格部分序；对该图做拓扑排序可获得保持所有严格边
  和二选一方向的线性扩展。反向上，任何旧 permutation 模型显然也是 partial 模型，
  所以在当前已编码语言内两种模式等可满足；
- 严格连接修复：旧连接
  `(= (< pos_a pos_b) (< sync_a sync_b))` 在去掉 `sync_ord_*` 的全局
  `distinct` 后允许两者 rank 相等，从而逃逸受控事件顺序。v5 改为
  `(or (and (< pos_a pos_b) (< sync_a sync_b))
       (and (< pos_b pos_a) (< sync_b sync_a)))`，
  同时禁止相等并保持两个位置空间的相对方向；
- 双层次序编码：replay-visible `pos_*` 仍是 `0..N-1` 的全排列，因为 prefix slot
  和 conflict reversal 必须引用精确事件位置；只有内部 lifecycle `sync_ord_*`
  默认改成无界整数 rank，不再生成逐事件 bounds 和全局 `distinct`。求解器得到的
  lifecycle partial model 可通过线性扩展解释为某个满足同一约束的 SC 总序；
- 双模式与消融：新增
  `SYMCC_SCHEDULE_SMT_ORDER_ENCODING=partial|permutation`，默认 `partial`；
  `permutation` 保留 v4 风格 bounds + all-different，作为差分验证、回归定位和论文
  消融基线。MPI master 将该设置传入 `DporScheduleExplorer`，未知值保守回退
  `partial`；
- Schema 与可复现性：exporter 输出 `symcc-schedule-smt-v5`，encoding 为
  `bounded-sc-partial-order-lock-condition-thread-state-v5` 或对应
  `permutation` 变体；materializer 继续接受 v1--v4。artifact 顶层记录
  `lifecycle_order_encoding`，`sync_state` 记录 `linear_extension_semantics`、
  `order_bound_constraint_count` 和 `order_distinct_constraint_count`，使实验不能在
  未知编码下混合统计；
- 自动化验证：`test_schedule_exploration.py` 新增相同 thread lifecycle trace 的
  partial/permutation 差分求解。两者对合法 join-before-exit 阻塞轨迹均为 SAT，
  对人工强制 start-before-create 均为 UNSAT；另一个受控 wait 顺序测试强制两个
  `sync_ord_*` 相等并验证 UNSAT，专门防止弱 order-link 回归。原有 mutex/rwlock、
  condition、thread 与真实 preload 测试继续覆盖默认 partial 模式；
- 定量功能结果：定向 schedule suite 35/35 通过。对相同 64-thread、384-event
  deterministic trace，partial artifact 为 1,219,136 bytes，base SMT 为
  554,560 bytes，生成约 17.0 ms，系统 libz3 单次 SAT 约 173.9 ms；permutation
  artifact 为 1,292,095 bytes，base SMT 为 590,652 bytes，生成约 15.0 ms，SAT
  约 8178.5 ms。当前样例上求解时间约缩短 47 倍，base formula 约缩小 6.1%；
  这是单机单次 B 级工程压力测量，不是公开 benchmark 的 R 级性能结论；
- 当前结果等级：I/T/B，SOTA 审计证据等级 E。创新性在于对真实 pthread lifecycle
  operational subset 先审计可观察约束语言，再利用线性扩展保持 SAT/UNSAT，同时
  保留 replay event 的精确排列和可切换旧编码，而不是无条件把所有调度变量改成
  不可解释的松弛 rank；
- 可信边界：等可满足性论证只覆盖当前正向严格序原子及其布尔组合。未来若加入
  rank distance、adjacency、cardinality、绝对 slot 或优化目标，必须重新证明或
  自动切回 permutation。当前没有 machine-checked proof，也没有从部分序模型输出
  具体拓扑排序 replay；pairwise controlled order links 和 section exclusions 仍可
  二次增长。这里列出的 detach/cancellation completion 与二次 exclusion 已分别由
  F55/F53 完成；read-from/coherence、TSO/RA、Query IR path roots 联合求解以及
  sound/complete/optimal DPOR 仍未实现。

## 45. F50：Scaled-Anchor Sparse Lifecycle Order Links（2026-07-27）

- 研究问题：F49 已移除 lifecycle bounds/all-different，但每两个受控 lifecycle
  events 仍生成一个 `pos_*`/`sync_ord_*` 相对顺序连接。若有 C 个受控事件，公式
  需要 `C(C-1)/2` 个四重严格比较；64-thread create/join trace 的 64 个受控 join
  因而产生 2016 个连接，仍占 base SMT 的主要体积；
- 线性编码：v6 partial 模式对第 i 个受控 lifecycle event 只生成一个
  `sync_ord_i = (L+1) * pos_j`，其中 L 是 bounded lifecycle event 数。
  `pos_j` 的全排列直接保证所有 anchors 严格有序，连接数量从 O(C²) 降为 O(C)；
- 槽位充分性：相邻 `pos_*` anchors 的 rank 差至少为 `L+1`，中间有 L 个可用整数。
  整个窗口最多只有 L 个 lifecycle events，因此任何原 partial model 的非受控
  outcome/release/start/exit 均可按拓扑序嵌入 anchors 之间；首 anchor 前和末 anchor
  后使用无界负/正整数。该构造保持 F49 当前严格顺序语言的等可满足性；
- 兼容与消融：`SYMCC_SCHEDULE_SMT_ORDER_ENCODING=permutation` 继续使用 bounded
  all-different 与旧 pairwise links，不改变 legacy 对拍含义。metadata 新增
  `order_link_encoding=scaled-anchor|pairwise`、实际 link count、semantic pair count、
  `controlled_anchor_count` 和 `anchor_stride`；
- Schema：当前 exporter 输出 `symcc-schedule-smt-v6`，encoding 为
  `bounded-sc-*-order-lock-condition-thread-state-v6`；reader 继续接受 v1--v5；
- 测试：24 个受控 waits 的 partial 公式只含 24 个 anchors，permutation 含 276
  个 pairwise links；两者均由系统 libz3 返回 SAT，partial base 小于 permutation
  的三分之一。原 strict-link 回归改为强制两个 anchored ranks 相等并得到 UNSAT；
- 64-thread 压力测量：相同 384-event deterministic trace 上，v6 partial 的
  64 anchors 表达 2016 个 semantic pairs，base SMT 255,663 bytes，artifact
  723,091 bytes，生成约 18.0 ms，libz3 SAT 约 66.4 ms；v6 permutation base
  587,016 bytes，artifact 1,389,790 bytes，SAT 约 3609.9 ms。两者证书均通过；
  这是单机单次 B 级公式压力结果，不是 R 级 coverage/throughput 结论；
- 当前等级：I/T/B。剩余的主要二次项已转为同一同步对象上 complete-section
  exclusions；若要 lazy refinement，必须先提供模型检查、违反区间提取和迭代
  re-solve，而不能简单删除 hard exclusions 造成假 SAT。

## 46. F51：Constructive Linear-Extension Certificate Checker（2026-07-27）

- 研究问题：F49 的线性扩展论证此前只存在于文档，artifact 没有机器可读的边，
  也无法证明某份 solver assignment 确实能构造满足当前 operational subset 的总序；
- Order IR：v6 在 `sync_state.order_ir` 写出
  `symcc-lifecycle-order-ir-v1`，包含 bounded lifecycle events、程序序/thread
  固定边、critical-section nonoverlap 的方向 alternatives、controlled anchors、
  link encoding 和 stride。IR 与 SMT 由同一恢复过程产生，但证书验证不解析注释或
  依赖命名字符串；
- 构造器：`schedule_linear_extension_certificate()` 接收完整 `pos_*` 与
  `sync_ord_*` assignment；按源 ranks 选择每个已满足 exclusion direction，加入
  anchors 所确定的 controlled order，再以确定性 Kahn/priority-queue 算法生成拓扑
  总序。未提供 model 时，将 observed trace 总序嵌入同一 scaled-anchor 空间；
- 独立检查器：`verify_schedule_linear_extension_certificate()` 重新检查 event
  position permutation 与 per-thread program order、partial anchor equality 或 legacy pairwise direction、
  source ranks 上的固定/选择边、总序对所有边的保持、choice 完整性、controlled
  projection、trace/base digest 和 certificate SHA-256。篡改 replay、choice 或
  digest 均拒绝；
- Query 绑定：证书可携带 `query_index`，检查器会重新计算该 query 的第 k 次
  per-thread replay slot，并验证 source conflict 的 right event 严格早于 left
  event；因此任意 base model 不能冒充目标 query model；
- Artifact：每个有 lifecycle state 的 v6 artifact 自动包含
  `observed_linear_extension` 证书和 `observed_topology_replay_prefix`，便于实验
  数据先验证再消费；
- 当前等级：I/T。这里的“machine checked”指项目内独立可执行 certificate checker，
  不是 Coq/Lean 等定理证明。证书 scope 明确为 hard lifecycle/order IR 与指定
  query；optional HB、trylock busy、condition wake assumptions、path constraints、
  read-from 和具体 runtime reachability 均未认证。

## 47. F52：Verified Topology-to-Runtime Replay Materialization（2026-07-27）

- 研究问题：partial SMT 的 `sync_ord_*` model 不能直接写入 preload runtime；
  runtime 消费的是每个受控调度点的 logical-thread-id prefix。此前缺少从已验证总序
  到该 ABI 的明确投影；
- 投影规则：证书按 total lifecycle order 选出同时具有 controlled anchor 的
  lock/trylock/rdlock/wrlock/wait/join events，并输出其 logical tid。outcome、
  release、thread marker 等非受控 lifecycle events 保留在证书中但不写 prefix；
  memory trace events 也不伪装成 runtime scheduling points。若指定 query 的任一
  replay slot 或 conflict endpoint 是 read/write，证书标记
  `runtime_replayable=false`，writer/CLI 拒绝物化；
- API：`write_schedule_linear_extension_prefix()` 只接受能通过独立 checker 的
  certificate，随后复用原有 atomic `write_schedule_prefix()`；篡改或跨 artifact
  certificate 不会写出；
- CLI：`util/symcc_schedule_linearize.py` 支持 JSON/JSONL artifact、负数行索引、
  `--model`、`--query-index`、`--prefix-out` 和 `--certificate-out`。model JSON
  接受数组或 `pos_i`/`sync_ord_i` map；没有 model 时物化 artifact 内嵌 observed
  certificate；
- 自动化证据：两 wait query 的反序 model 产生 `[2,1]` prefix，CLI 输出的证书再次
  通过 checker；不满足 query replay slot 的 model 被拒绝。真实 pthread preload
  测试把 observed certificate 写成 prefix 后重新执行 target，并确认下一次 trace
  的首个 controlled logical tid 与证书一致；
- 当前等级：I/T。该 prefix 只表达受控点的优先线程，不保证目标线程当时 enabled；
  runtime 仍受 `SYMCC_SCHEDULE_WAIT_MS` 有界等待和 fallback 约束。证书也不是路径
  可达性、完整 operational semantics 或 optimal DPOR 的证明。
- 2026-07-27 线性化修复：原 gate 在匹配线程 CAS 递增 `prefix_index` 后才记录
  controlled event，等待线程可观察到索引推进并抢先写 trace，使物化 prefix 偶发与
  trace 首事件不一致。runtime 现在用独立 `replay_gate_lock` 原子化“匹配/timeout
  决策、controlled/fallback 记录、prefix index 发布”；无 prefix 的普通记录不经过
  该锁。真实 mutex replay 50 次压力重跑全部保持首事件一致，schedule suite 50/50
  与严格 `-Wall -Wextra -Werror` 编译通过。

## 48. F53：Violation-Driven Lazy Critical-Section Refinement（2026-07-27）

- 研究问题：F50 已把 controlled/lifecycle 连接从二次规模降为线性，但同一对象上
  S 个不兼容完整临界区仍产生 `S(S-1)/2` 个 nonoverlap 析取。直接删除这些 hard
  constraints 会接受不可执行的重叠模型；预先用 activation implication 包裹全部
  析取又不能降低 parser 和初始 context 体积；
- v7 双上下文：`base_smt2` 仍是权威精确公式，包含全部 exclusion，现有
  materializer、incremental script 和外部 solver 行为不放宽；新增
  `relaxed_base_smt2` 及独立 SHA-256，只供内置 refinement solver 使用。宽松基线
  省略 exclusion AST，但保留 declarations、event/lifecycle PO、scaled anchors、
  condition/thread enabledness 和 query delta；
- Refinement IR：`symcc-lifecycle-order-ir-v1.choice_constraints` 保存每个
  critical-section choice 的稳定名称及两组方向边；`sync_state.lazy_refinements`
  只保存 name/kind/choice index，避免重复保存 SMT 文本。内置 solver 从 model 的
  `sync_ord_*` 检查每个 choice，收集当前真正违反的集合，并用 `Z3_mk_lt`、
  `Z3_mk_or` 和 `Z3_solver_assert` 直接加入 AST，再增量 re-check；
- 精确终止条件：只有当前模型满足全部 choice 时才生成 certificate 并返回 SAT。
  active hard choice 仍被模型违反会作为内部一致性错误拒绝；到达
  `max_refinement_rounds` 时返回 `refinement_limit` 和
  `exact_semantics=false`，不得冒充 SAT。UNSAT 在已加入的 hard refinements 下成立；
- 兼容与消融：schema 升为 `symcc-schedule-smt-v7`，reader 继续接受 v1--v6。
  `lazy_refinement=False` 与 CLI `--eager` 使用完整基线，作为小实例、对抗性 UNSAT
  和差分验证路径；
- 自动化证据：同一强制重叠约束在 relaxed base 为 SAT、exact base 为 UNSAT；
  lazy solver 第一轮得到重叠模型、加入 `sync_exclusion_0_1`，第二轮得到 UNSAT。
  零 refinement 预算明确返回 limit；eager 一次检查为 UNSAT；
- 64-section 公式压力消融：192 lifecycle events、2016 choices，exact base
  426,631 bytes，relaxed base 218,101 bytes。普通 SAT 五次本地测量的 lazy 中位数
  124.696 ms（106.250--126.677），eager 中位数 366.579 ms
  （348.431--380.498）；人工强制全局重叠的三次 UNSAT 中，lazy 激活全部 2016
  choices、两次 check，中位数 182.359 ms，eager 一次 check 中位数 140.513 ms。
  该 B 级结果说明 lazy 的收益取决于违例稀疏性，因此保留 eager 消融，不宣称对
  所有查询单调加速；
- 当前等级：I/T/B。该循环只细化完整 critical-section nonoverlap；condition wake、
  trylock busy、read-from、path constraints 和 weak-memory axioms 尚未纳入同一
  counterexample-guided refinement loop。

## 49. F54：Direct System-libz3 Model Extraction and Solver CLI（2026-07-27）

- 研究问题：F51/F52 已能检查完整 `pos_*`/`sync_ord_*` assignment，但此前必须由
  外部脚本解析 Z3 文本 model 再转换 JSON，存在版本格式差异、遗漏 unconstrained
  常量和人工工件错配风险；
- C API：`_SystemZ3Solver` 通过 `ctypes` 加载系统 `libz3`，使用
  `Z3_parse_smtlib2_string`、incremental solver API 和引用计数 context。每个整数
  变量由 `Z3_model_eval(..., model_completion=true)` 与
  `Z3_get_numeral_int64` 结构化读取，不解析 `model.to_string()`；
- 完整链路：`solve_schedule_smt_query()` 校验 base/delta SHA-256，解析一次
  base+query，运行 F53 refinement，提取 event positions/lifecycle ranks，调用 F51
  构造器，并再次运行独立 checker。SAT result 包含 model、certificate、
  runtime-replayability 与 prefix；UNSAT/unknown/limit 不伪造证书；
- CLI：`util/symcc_schedule_solve.py` 支持 JSON/JSONL、query/base-only、
  lazy/eager、round bound、附加研究断言，以及原子写 result/certificate/prefix。
  其 prefix 输出沿用 F52 的 runtime-replayability gate；
- 自动化证据：覆盖直接 SAT model 与 certificate、lazy forced-overlap UNSAT、
  eager 消融、digest tamper 拒绝、CLI result/certificate/prefix 三类产物；系统
  没有 `libz3` 时 API 明确报告 unavailable，而不是静默降级到未验证文本解析；
- 当前等级：I/T。当前 binding 只服务本模块已生成的 QF_LIA schedule schema，
  不是通用 Python Z3 replacement；未实现 unsat core/proof object，certificate
  证明的是 SAT model 对 order IR/query 的一致性，不证明 concrete path reachability。

## 50. F55：Detach/Cancel/Join-Cancelled Identity Lifecycle（2026-07-27）

- 研究问题：F48 在 `pthread_create` 成功返回后才注册 handle，快速退出 child 可先于
  注册；detached entry 永不回收，超过固定 4096 项后稳定 identity 退化；取消一个
  正阻塞于 `pthread_join` 的 joiner 又只留下无 outcome attempt，难以区分 trace
  截断与 cancellation；
- 竞态安全 registry：runtime 在调用真实 `pthread_create` 前按 logical child id
  预留 entry，成功后绑定 handle，失败时丢弃 reservation。entry 显式记录
  `handle_valid/detached/exited/cancel_requested`；joinable exit 保留到成功 join，
  detached identity 只在 handle 已绑定且 exit/detach 两条件都满足时退休；
- Runtime ABI：新增 `pthread_detach`、`pthread_cancel` interposition，以及
  `detach[_success|_fail]`、`cancel[_success|_fail]`、`join_cancelled` 和
  `thread_retire cause=join|detach`。join wrapper 在真实 cancellation point 外压入
  cleanup handler；若 joiner 被取消，先记录 `join_cancelled`，不删除 target
  mapping，另一线程仍可完成 join；
- Cancellation-safe tracing：replay gate 与持有 `log_lock` 的 logger 不再调用
  libc cancellation points，分别改用 `SYS_nanosleep` 与 `SYS_write`。否则取消可在
  logger 持锁时展开 cleanup，cleanup 再记录 `join_cancelled/thread_exit` 时永久
  自锁；join cleanup 现在覆盖 gate 与真实 join，提前取消也会补齐 attempt marker；
- 创建即 detached：runtime 读取 `pthread_attr_getdetachstate`，在 create markers
  写 `detached=1`。若 child 在父线程绑定 handle 前已经退出，bind 操作完成退休并
  发出显式 `thread_retire`，避免 exit/register 竞态泄漏；
- Operational IR：生命周期恢复器新增 detach/cancel/join-cancelled/retire rows。
  mapped successful join 仍要求 `thread_exit < join_success`；cancelled join 不合并
  target exit clock，也不增加 completion edge。完整 retire 产生
  `thread_exit < thread_retire`，并根据原因增加
  `join_success|detach_success|detached-create-success < thread_retire`；
- 自动化证据：真实 preload detach target 产生完整 detach/exit/retire metadata 和
  两条 retirement hard edges；真实 joiner cancellation 后 trace 含 mapped
  `join_cancelled`，主线程随后仍以 mapped join 成功回收原 target。另一个压力目标
  顺序创建 4104 个 `PTHREAD_CREATE_DETACHED` threads，全部 create-success 为
  `mapped=1`，全部发出 `thread_retire`，证明跨越 4096 容量后槽位持续复用；
  cancellation test 同时启用 replay prefix，另做 50 次连续压力重跑未再出现日志锁
  死锁；
- 测试状态：schedule suite 42/42，严格
  `cc -Wall -Wextra -Werror -fPIC -shared -pthread` 通过。当前等级 I/T/B；
- 可信边界：记录 successful cancellation request 不等于证明目标已接收或执行
  cancellation；`thread_exit normal=0 cancel_requested=1` 是 runtime evidence，
  不是 POSIX delivery proof。double join、detach 与并发 join 的未定义竞态、fork、
  process-shared objects、优先级与公平性仍未建模。

## 51. F56：Bounded Source-DPOR Certificate and Sleep Sets（2026-07-27）

- 研究问题：原 explorer 由冲突对直接生成 thread prefix，缺少可检查的依赖图、
  Mazurkiewicz 等价类、source set 和跨重启 sleep set；因此“source-style”只是候选
  启发式，不能解释为何两个独立交换属于同一类，也不能验证回溯目标的因果前驱；
- 技术依据：实现参考 Source-DPOR 的 source-set/sleep-set 架构和 Optimal-DPOR
  对 wakeup-tree 必要性的分析。当前版本刻意命名为 bounded Source-DPOR
  certificate，不宣称具备论文中的 unbounded completeness 或 optimality；
- 依赖与等价：`_bounded_dependency_graph()` 为每个事件建立稳定的
  `(tid, per-thread-index)` identity，加入 thread program order 与同步/内存冲突边，
  计算规范拓扑线性化及其 SHA-256 Mazurkiewicz class。独立事件互换得到相同 class，
  dependent 事件互换得到不同 class；
- Source 分析：`_source_replay_analysis()` 从目标 backtracking event 逆向加入同
  thread 的因果前驱，形成可执行 wakeup sequence、source set 和有界 replay
  candidate。比如目标线程在冲突点前已有一个事件时，候选会保留两个 thread slot，
  而不是构造不可达的单元素跳转；
- 可检查证据：`source_dpor_certificate()` 输出
  `symcc-bounded-source-dpor-v1`，绑定 normalized trace digest、依赖边、canonical
  order、equivalence digest、source records 和明确 bounds；
  `verify_source_dpor_certificate()` 独立重算并拒绝 trace、edge、source 或 digest
  篡改。schedule constraint artifact 同步内嵌该证书；
- 持久探索：F56 首版 `DporScheduleExplorer` state schema v2 持久化已观察等价类
  和 bounded sleep sets，过滤已探索的首步选择，并统计
  `sleep_pruned_prefixes` 与 `equivalent_traces`；F61/F62 后续把 schema 升到 v4，
  加入 wakeup leaves 和 ConDPOR revisit identities。旧 state 仍可读取并升级；
- 自动化证据：`test_schedule_exploration.py` 覆盖 independent-swap 等价、
  dependent-swap 区分、因果 wakeup、证书篡改、sleep persistence 和等价类计数；
- 当前等级 I/T。F56 阶段尚未实现 wakeup tree；该项随后由 F61 的 bounded
  weak-initial tree 补齐。动态 operational enabled set、unbounded race reversal
  和 optimality proof 仍未实现；sleep pruning 只在当前 bounded contract 内有效，
  不能扩展解释为完整模型检查。

## 52. F57：Single-Context Path/Schedule/Read-From Solving（2026-07-27）

- 研究问题：F42 只把 Query IR 和 schedule artifact 放在同一 JSON 记录中分别验证；
  path SAT 与 schedule SAT 仍可能由互不相容的 input-byte/read-from witness 支持。
  ConDPOR 所需的关键步骤是让 data 与 schedule nondeterminism 在同一 solver
  context 中共享变量；
- 联合公式：`solve_joint_path_schedule_query()` 去除两侧 SMT-LIB2 的
  `check-sat/get-model/exit` 控制命令，在一个 system-libz3 context 中组合 Query IR
  QF_BV、schedule QF_LIA 和 read-from constraints。`_SystemZ3Solver` 扩展为结构化
  提取额外 integer 与 byte bit-vector model，不解析 solver 打印文本；
- Read-from 桥：memory event tag 使用显式 `sym-byte=N` 把 read value 连接到输入
  byte，`init=0xNN` 表示初始内存，write 的 `value=0xNN` 表示 concrete write
  witness。联合结果携带 event positions、RF selector 和 input bytes；
- 持久化入口：`QueryStore.solve_joint_schedule_smt()` 按 Query IR content hash
  读取已存 SMT，执行联合求解，并用 bounded Query IR evaluator 再检查 byte model。
  SQLite stats 增加 `joint_smt_solves` 和 `joint_smt_verified`；
- 独立复检：`verify_joint_path_schedule_result()` 重验 Query/schedule/delta digest、
  F51 schedule certificate、RF 结构和 byte bridge，并重新调用 solver，防止把过期
  或跨 artifact model 当作有效证据；
- 自动化证据：测试构造 path 单独 SAT、schedule 单独 SAT、但 read-from byte bridge
  冲突的实例，联合结果必须 UNSAT；把 initial write 改为兼容值后得到 SAT，提取的
  input byte 同时通过 Query IR evaluator 与联合 verifier；
- 当前等级 I/T。该公式只覆盖 artifact 中存在的 byte tags 和 bounded memory
  events；缺 tag 的 concrete runtime value 不会被推测。它不是任意程序路径的
  operational reachability proof，也未实现 ConDPOR 的 backward revisit/maximal
  extension 算法。

## 53. F58：Parameterized Bounded SC/TSO/RA Memory Models（2026-07-27）

- 研究问题：此前 schedule-SMT 只约束 SC event order，没有 authoritative
  read-from/coherence 语义；这既会接受 stale read，也无法研究 weak-memory 下
  fuzzing schedule 与 input 的交互；
- 模型选择：`schedule_smt_artifact(..., memory_model=...)` 和 MPI 环境变量
  `SYMCC_SCHEDULE_MEMORY_MODEL=SC|TSO|RA` 选择语义。memory event window 由
  `SYMCC_SCHEDULE_SMT_MAX_MEMORY_EVENTS` 控制，默认 32、硬上限 64，超过边界明确
  截断并记录 metadata；
- SC：所有内存事件共享总位置序；每个 read 选择 initial value 或位置上早于它的
  write，并要求选择的 write 是该地址在 read 前的最后一次 global write；
- TSO：保留 load/load、load/store 和 store/store program order，允许
  store/load global-order relaxation；同线程未 flush 的最新 store 必须通过
  forwarding 被后续 load 读取，否则才从 global last write 读取。这里是 bounded
  store-buffer observational subset；
- RA：为每个地址建立 read-from、per-location modification-order ranks 和 Boolean
  happens-before closure。thread PO、RF causal rank、release-write 到
  acquire-read 的 synchronizes-with，以及 WW/WR/RW/RR coherence 共同排除 thin-air
  和违反 release/acquire message-passing 的 witness。trace tag
  `mo=relaxed|release|acquire|acq_rel|seq_cst` 指定原子意图；
- 证书边界：SC 可继续检查严格 program order；TSO/RA 证书 checker 使用
  model-aware PO。由于 pthread runtime prefix 不能强制 CPU memory order，非 SC
  certificate 标记 `runtime_replayable=false`；
- Litmus 证据：Store Buffering 两个 load 都读 initial 在 SC 下 UNSAT、TSO/RA 下
  SAT；TSO 同线程 store 后 stale initial read 因 forwarding 为 UNSAT；RA 的
  release/acquire flag 读取配合 stale data 为 UNSAT，而 relaxed flag 允许该 witness；
- 当前等级 I/T。RA 是 bounded axiomatic subset，不是完整 C11/C++ memory model；
  fences、release sequence、RMW、SC atomics 总序、non-atomic race UB、mixed-size
  tearing、x86 locked operations 和完整 TSO propagation 均未建模。

## 54. F59：Content-Addressed Live State and Page-COW Memory（2026-07-27）

- 研究问题：F38 只有 continuation descriptor，无法迁移真实 solver stack、
  symbolic store 和 memory image。GenSym 式并行状态 fork 需要深状态可复制，但
  全量序列化每次分支会造成 O(memory) 放大；
- CAS：`LiveStateStore` 以 canonical JSON 的 SHA-256 为对象 ID，使用原子 rename
  写入，读取时重验 digest、schema、大小和引用。表达式节点、solver frame、
  symbolic store、memory page、memory root 分别有版本化 schema；
- 增量 solver stack：frame 以 parent digest 连接，仅存本层 constraint expression
  references。`restore_solver_frames()` 检查深度、循环和对象 schema，恢复从 root
  到 leaf 的约束序列；
- Symbolic store：变量名映射到 expression CAS object；恢复时验证所有引用存在且
  类型正确，不允许 dangling symbolic value；
- Page-COW memory：默认 4096-byte page 保存 concrete bytes 和稀疏 symbolic-byte
  references。`fork_memory()` 只重写受影响 page 和 memory root，未修改 page digest
  在父子状态间共享；concrete overwrite 默认清除对应 symbolic byte，避免 stale
  expression；
- Continuation bundle：`put_continuation()` 绑定 descriptor、solver/store/memory
  roots，checkpoint id 就是 CAS id；`restore_continuation()` 返回
  `LiveContinuationBundle`。`symcc_live_state.py inspect|memory-diff` 提供离线验证和
  比较，并明确输出 `native_resume_supported=false`；
- 自动化证据：测试验证跨页写只复制一个 page、未修改 page digest 共享、solver
  stack/store/memory 完整恢复、CLI inspect/diff，以及 tamper、缺失对象、错误
  reference 全部拒绝；
- 当前等级 I/T。这是 portable live-state persistence contract，不是任意 native
  instruction 的 resume engine。当前 SymCC instrumentation 不是 CPS compiler，
  尚无 register/PC/call-stack 恢复、external resource virtualization 或 MPI
  scheduler 对该 bundle 的实际执行适配。

## 55. F60：Executable Research Evidence and Claim Gates（2026-07-27）

- 研究问题：单元测试数量不能直接支持论文声明；需要把输入、bounds、环境、公式
  hash、checker 结论与不支持的 claim 放入可机器复检的实验单元，防止实现继续演化
  后历史结果失去语义；
- Evidence builder：`build_research_evidence()` 当前生成
  `symcc-research-evidence-v2`，运行 F56 equivalence certificate、F57 joint
  path/schedule/RF、F58 SC/TSO/RA litmus、F59 CAS/COW restore、F61 wakeup/ready
  certificate 和 F62 graph/revisit certificate。每项记录精确输入、schema/digest、
  bounds、protocol、oracle 与结果；verifier 仍可读取历史 v1；
- Solver 对拍：`validate_smt_backends()` 自动探测 system-libz3 C API、Z3 CLI 和
  cvc5 CLI，以预期 SAT/UNSAT oracle 验证可用 backend。当前环境只有 libz3 时，
  evidence 会如实记录单一 solver family，不虚构 cross-family consensus；
- Claim gate：`verify_research_evidence()` 先验证整个 bundle digest，再执行 semantic
  gates。claim scope 明确排除 optimal/unbounded DPOR、完整 C11/fence/UB、任意 native
  resume 和未由独立 solver family 复现的共识；
- CLI：`python3 util/research_evidence.py --output evidence.json` 生成 artifact，
  `python3 util/research_evidence.py --verify evidence.json` 在另一个环境复检；
- 自动化证据：`test_research_evidence.py` 覆盖 backend capability/oracle、
  claim/digest gate、tamper rejection 和 CLI round trip；
- 当前等级 I/T。该 artifact 是可执行证据与声明约束，不替代等 CPU、多目标、多轮
  benchmark、统计效应量、artifact evaluation 或形式化证明。达到 R 级仍需固定
  commit/container、公开 corpus、失败样本和至少一个独立 solver family。

## 56. F61：Bounded Optimal-DPOR Wakeup Tree and Ready Evidence（2026-07-27）

- 研究问题：F56 已有 source set、causal wakeup sequence 和 sleep-set persistence，
  但没有 Optimal-DPOR 的有序 wakeup tree。只按 prefix hash 去重无法检查“较早
  sibling 是否已是新 leaf 的 weak initial”，也无法用运行时 evidence 排除根节点
  尚未到达调度点的候选；
- 技术依据：按 Optimal-DPOR（POPL 2014）Lemma 6.1 实现 weak-initial 递归，并在
  bounded deterministic-next-event 抽象中检查 wakeup-tree leaf/sibling/sleep
  invariants。候选 process sequence 的首事件要么能从 reference 中跨越独立事件
  成为 initial，要么与 reference 全部独立后递归；
- 数据结构：`BoundedWakeupTree` 以 base prefix 为根，保存有序 leaves。插入拒绝
  超界、complete-ready 集之外的首线程、sleep weak initial、existing-leaf weak
  initial 和 ordered-sibling weak initial；state schema v3 持久化 leaves，并在
  replay 实际观察到对应 prefix 后删除已探索 leaf；
- 运行时 enabled evidence：preload runtime 可记录
  `ready decision=N chosen=T tids=... complete=...`。controlled pthread 调用先在
  bounded registry 注册，再由 `replay_gate_lock` 原子化 ready snapshot、选择事件
  记录和 prefix-index 发布；配置可限制 settle 时间与最多参与线程。该 ready 集表示
  已到达 cooperative scheduling point 的线程，不等同于任意程序状态的 enabled set；
- 可检查证书：`symcc-bounded-wakeup-tree-v1` 绑定 trace、Source-DPOR certificate、
  bounds、ready evidence、sleep sets、每次 insertion reason 和最终 tree；独立
  verifier 从原始 trace 重建全部字段并拒绝 tamper；
- 自动化证据：测试区分 independent sequence 的 weak-initial 等价与 dependent
  leaves，检查 complete ready root 接受/拒绝、证书篡改、state persistence；真实
  mutex replay 在 ready settle 下确认首决策 snapshot 包含竞争线程且选择与 prefix
  一致。schedule suite 在本阶段为 52/52；
- 正确性修复：原 replay gate 先发布 prefix index、后写 trace，等待线程可抢先记录。
  现在选择、记录与发布在同一 gate lock 临界区完成，真实 replay 压力测试不再出现
  首事件线性化反转；
- 当前等级 I/T。实现证明的是 bounded trace abstraction 下的 tree invariants；
  没有证明运行时 enabledness 完备、termination/maximal execution、state-dependent
  dependence 或 unbounded Optimal-DPOR 每个 Mazurkiewicz class 恰好一次。

## 57. F62：Bounded ConDPOR Graph and Backward Revisit（2026-07-27）

- 研究问题：F57 将 path、schedule 与 RF 放进单一 solver context，F58 提供
  parameterized memory subset，但此前没有 ConDPOR 的 execution graph、backward
  revisit、causal-successor deletion 和 maximal extension 工件，跨 trace 也无法按
  `po/rf/co` 结果去重；
- 技术依据：参考 ConDPOR（CONCUR 2025）的 execution graph。当前实现将 runtime
  trace 映射为稳定 `t<tid>:<thread-index>` 节点与每地址 `init:<object>` 节点，
  事件分为 read/write/constraint/action，显式保存 program order、read-from、
  coherence 和 observed add order。因果关系严格取 `(po union rf)+`；
- RF 与一致性：显式 `rf=`/`rf-seq=` tag 优先；否则使用同地址、先出现且 value
  匹配的最新 write，再退化到 latest observed write 或 init。每地址 coherence 从
  init 开始按 observed writes 建立。inference reason 进入证书，未把猜测伪装成
  concrete value proof；
- backward revisit：对后加入 write 和先前同址 read，仅保留现有 HB/lockset
  classifier 认定为 unordered/unprotected 的 pair；若 read 因果先于 write 则拒绝，
  否则删除 read 的严格因果后继并改写 `rf(write, read)`。所有 relation 重新检查
  acyclic，拒绝任何 causal-cycle candidate；
- bounded maximal extension：按稳定 observed position 恢复删除事件。恢复 read
  选择当前 coherence-maximal write；恢复 write 放到当前同址 writes 之后；
  constraint 优先使用 `model-outcome=` 作为确定性 tie-break，否则显式标注
  observed-outcome fallback；action 只恢复观察到的 thread successor。extension
  保存删除集、顺序、选择、最终 RF/co 和 model-witness 计数；
- 可复检与探索：`symcc-bounded-condpor-graph-v1` 对 graph identity、每个 revisit
  identity 和整个 certificate 分层 SHA-256。独立 verifier 从 trace 重算。explorer
  state schema v4 持久化每个 input 的 revisit digest，统计首次/重复 revisit；
  revisit 绑定同一 Source-DPOR base/wakeup/replay prefix，所以由现有 bounded queue
  实际执行，不创建不受 wakeup-tree gate 约束的旁路；
- 开关与产物：MPI 默认启用 `SYMCC_DPOR_CONDPOR_GRAPH=1`；schedule constraint
  JSONL 内嵌 execution graph。关闭后只停用 explorer 跨 trace revisit bookkeeping，
  不影响旧 Source-DPOR queue 或 schedule-SMT；
- 自动化证据：synthetic trace 覆盖 `init -> read` RF、write-to-read revisit、
  read-successor constraint deletion、model-outcome maximal extension、replay
  prefix、certificate tamper；另覆盖同线程因果拒绝、mutex/HB 保护拒绝，以及两次
  observe 后 revisit identity persistence/dedup。schedule suite 为 55/55；
- 全量回归：Python unittest 212/212、LLVM lit 88/88、完整 build、严格 runtime
  `-Wall -Wextra -Werror` 编译、cooperative-ready mutex replay 20/20、
  `py_compile` 与 `git diff --check` 全部通过；
- 当前等级 I/T。此实现刻意命名为 bounded observed-control-flow ConDPOR graph。
  read 值改变后控制流可能产生不同事件，当前只能恢复已观察 suffix；没有完整
  interpreter、path-dependent event existence、memory-model consistency oracle 或
  sound/complete/optimal 证明，不能对外表述为完整 ConDPOR 复现。

## 58. F63：Path-Dependent Events and Causal Regeneration（2026-07-27）

- 研究问题：F62 的 backward revisit 能改写 read-from 并删除 read 的因果后继，但
  compiler/runtime trace 只有 read/write 和 pthread rows。若新 read value 改变
  branch，原 suffix 中的事件可能根本不存在；继续机械恢复 observed suffix 会构造
  不可执行 execution graph；
- Compiler trace：schedule instrumentation 为 LLVM conditional branch、switch 和
  action basic block 增加 `constraint`/`action` 通知。runtime 把最近一次 schedule
  read 的 object、address 和 byte width 关联到 branch row，形成显式
  `last-read=<id>`/`last-read-bytes=<N>` evidence，而不是只比较相同字符串地址；
- 依赖判定：`_constraint_depends_on_read()` 同时接受显式 event identity、相同 object
  和 byte interval overlap。mixed-size read 只要地址区间相交，就将 constraint 判为
  path-dependent；不相交 row 不被扩大删除；
- ConDPOR regeneration：backward revisit 删除目标 read 的 causal successors 后，
  继续删除依赖该 read 的 constraint 及其同线程控制流 suffix。extension 不再把这些
  rows 当成 observed action 恢复，而是要求下一次 concrete/symbolic execution重新
  生成；artifact 记录 withheld/regeneration 原因；
- 自动化证据：`schedule_memory_trace.c` 使用不可被优化掉的 volatile side effect
  产生真实 BranchInst；lit 检查 read、constraint 和 action rows。Python tests
  覆盖显式 id、object+byte-overlap 和 causal suffix withholding；
- 当前等级 I/T。事件重生成仍依赖下一轮 instrumented concrete execution，不是
  ConDPOR 论文中的 interpreter-level symbolic transition generator。异常边、间接
  跳转、signal 和外部 I/O 引起的 event existence 尚未统一进入该模型。

## 59. F64：Operational Enabledness Certificate（2026-07-27）

- 研究问题：F61 ready snapshot 只说明线程已到达 cooperative gate，无法区分一个
  mutex/rwlock/join/wait 操作在当前 abstract state 中是真正 enabled、blocked 还是
  evidence 不足。把 ready 直接等同于 enabled 会错误剪枝 wakeup leaf；
- Runtime offers：bounded TLS-safe registry 的每个 ready entry 保存 logical tid、
  operation、primary object 和 auxiliary object。trace tag
  `offers=tid:op:object:auxiliary,...` 将 mutex、rwlock、join、condition wait 及其
  associated mutex 与同一 decision 绑定；wait rows另记录 timed 属性；
- 状态恢复：`operational_enabledness_certificate()` 从 lifecycle trace 重建
  mutex owner、rwlock readers/writer、join target completion 和 wait mutex state。
  每个 offer 被分类为 `enabled`、`blocked` 或 `unknown`，并检查 chosen operation
  与恢复状态一致；缺失 mutex type、截断 lifecycle 或 unsupported operation 保持
  unknown，不猜测；
- Terminal witness：只有 runtime 显式 stop 且 registry 无 pending offer 时，证书才
  产生 bounded terminal witness。settle timeout 或 snapshot `complete=1` 单独都
  不被解释为整个程序的 maximal termination；
- 可检查性：证书 schema 为 `symcc-operational-enabledness-v1`，其 digest/summary
  进入 wakeup、ConDPOR 和 schedule constraint artifacts。独立 verifier 从原 trace
  重建并拒绝 offer、status、chosen 或 terminal 字段篡改；
- 自动化证据：synthetic tests 覆盖 enabled/blocked/unknown、wait auxiliary mutex、
  terminal 和 tamper；真实 pthread replay 检查 runtime offer 与所选线程一致；
- 当前等级 I/T。这是 bounded pthread subset 的 operational evidence。robust/
  recursive/error-checking mutex、process-shared objects、priority/fairness、futex、
  signal 和 scheduler enforcement 不在 claim scope，不能据此证明完整 enabled set。

## 60. F65：Native Atomics and Extended Bounded RA（2026-07-27）

- 研究问题：LLVM pipeline 在 Symbolizer 前运行 `LowerAtomicPass`，若只在 visitor
  中处理 atomic instruction，真正看到的已是普通 load/store，既丢 memory order，
  又会把 lowered RMW 重复记为非 atomic memory event；
- Pre-lowering instrumentation：`compiler/Pass.cpp` 在 LowerAtomic 前遍历 atomic
  load/store、`atomicrmw`、`cmpxchg` 和 fence。compiler/runtime ABI 新增
  `_sym_notify_schedule_atomic` 与 result 通知；cmpxchg pre/result 通过 group id
  关联 success/failure，普通 lowered notification 由 TLS address/read/write counter
  抑制；
- Trace schema：atomic rows保存 `kind`、byte width、success/failure memory order、
  RMW operation、group 和 `atomic=1`；ordinary memory rows显式写 `atomic=0`。
  `cmpxchg` failure 规范化为 read，success 规范化为 RMW，fence 作为独立 schedulable
  event；
- Bounded RA：每个 overlap-connected location component 建 modification order。
  RMW 必须从紧邻的 MO predecessor 读取；release sequence 按 release head、
  same-thread continuation 和 RMW 递推；release/acquire fence 建 synchronizes-with；
  seq_cst atomics/fences共享 total rank，并同时受 HB/MO 与 latest-SC-write 约束；
- Race 与 mixed size：在 runtime 提供 atomic classification 时，跨线程 conflicting
  non-atomic access 且无 HB 被拒绝为 data-race execution。地址区间按 byte overlap
  建 component；read-from source 必须完整包含 read interval，部分覆盖不会被当作
  无撕裂 witness；
- 自动化证据：`schedule_atomic_trace.c` 的 lit 检查 load/store/RMW/cmpxchg result/
  fence；Python litmus 覆盖 release sequence、RMW atomicity、fence synchronization、
  stale SC read、non-atomic race、mixed-size full/partial source 和 cmpxchg normalization；
- 当前等级 I/T。语义名为
  `bounded-c11-rf-mo-hb-release-sequence-fence-sc-rmw`，明确不是 ISO C/C++ 完整
  executable model。consume、dependency ordering、full SC axioms、undefined behavior
  全传播、object lifetime、unaligned tearing 和 herd7 全 corpus 对拍仍未完成。

## 61. F66：Executable Continuation IR and MPI State Search（2026-07-27）

- 研究问题：F59 checkpoint 能持久化 PC descriptor、solver stack、symbolic store
  与 page-COW memory，却不能执行；state coordinator 实际仍只 lease seed replay
  tuple，无法证明 checkpoint 是可恢复的 symbolic work；
- Executable IR：新增 `symcc-live-program-v1`，支持
  `const/input/binary/unary/load/store/assume/branch/jump/call/return/halt`。
  `LiveContinuationExecutor` 从 CAS 恢复 frame PC、value-expression roots、path frames
  和 memory root，按 step/state budget 执行；
- Symbolic fork：symbolic branch按 concrete-first 顺序生成正/负 condition solver
  frame，为两个 child 提交 canonical checkpoint。超出本 lease state/step budget
  的 child 进入 frontier；call/return 维护 frame stack，symbolic store 使用
  depth-qualified local name，memory write继续复用 F59 page-COW；
- MPI 接入：`SYMCC_LIVE_PROGRAM` 启用 executable state mode。master 从输入 seed
  建初始 checkpoint，将 descriptor/identity纳入 fenced lease 与 state shard；
  worker 按 `SYMCC_LIVE_STEPS_PER_LEASE`、`SYMCC_LIVE_STATES_PER_LEASE` 恢复执行，
  master 校验 frontier checkpoint 后重新排队，实现跨 worker bounded fork/resume；
- 正确性复核：修复 state coordinator tuple 构造中 continuation 被误传为
  `tuple()` 第二参数的集成错误；调度 reward 只统计仍可 resume 的 frontier，不把
  已在本 lease halt 的临时 fork checkpoint 计作生成吞吐；halt/return 状态提交最终
  CAS checkpoint，结果不再指向写内存前的 stale parent；worker 在导入可能已被 AFL
  移动的 seed 之前优先恢复 self-contained checkpoint；超过
  `SYMCC_MAX_TRANSPORT_INPUT` 的 live seed 显式拒绝，不构造超大 expression graph；
- CLI：`symcc_live_state.py run-program|resume` 可创建、暂停和继续 IR；`inspect`
  对该 engine 报告 `continuation_ir_resume_supported=true`，同时继续明确
  `native_instruction_resume_supported=false`；
- 自动化证据：测试在第二条 instruction 暂停并恢复 PC，随后对 symbolic byte
  fork 两条路径，执行 nested call，得到 0/66 两个结果并检查 symbolic COW memory；
  另覆盖 CLI round trip、无效 target rejection、CAS tamper 与 state identity；
- 当前等级 I/T。F66 是可执行的 portable CPS-like research IR，不是自动从任意
  LLVM/native 程序生成 continuation。缺 compiler CPS lowering、完整 LLVM IR/
  memory/exception semantics、SAT feasibility pruning、TLS/syscall/external resource
  virtualization，以及 non-shared filesystem CAS replication。

## 62. F67：Sealed Randomized Research Protocol and Paired Analysis（2026-07-27）

- 研究问题：历史 runner 按 mode 成块执行，默认三轮，统计器只做未配对中位数
  bootstrap 与 A12；它不能控制热漂移/顺序效应、证明等 CPU 预算、保留失败/删失，
  也无法把结果绑定到 source/corpus/environment；
- Sealed protocol：`research_protocol.py plan` 从 target/configuration spec 生成
  `symcc-research-protocol-v1`。target×repeat 是配对 block，block 顺序和 block 内
  configuration 顺序由固定 seed 随机化；同一 block 共享 pair id 和 common random
  number。confirmatory phase 少于 20 repeats 直接拒绝，tuning phase 显式分离；
- Provenance 与预算：manifest 记录 git commit、dirty/status/diff digest、
  tracked+untracked working-tree digest、recursive dirty submodule digest、CPU、
  kernel、Python/compiler、container image 和输入/corpus tree SHA-256。所有 cell
  使用相同 CPU-second budget，wall budget按 declared cores 计算；executor 将进程组
  限制到指定 CPU affinity，记录实际 user/system CPU、assigned CPUs、command 和
  artifact digests。MPI teardown 可使用单独封存的 wall grace，不进入 campaign
  coverage/CPU 分母；
- Failure-safe execution：每个 run 独立保存 stdout/stderr/result，原子写入并支持
  resume。success、nonzero failure、wall timeout 全部保留；timeout 终止整个 process
  group，避免残留 MPI/fuzzer 污染后续 block。manifest/result 以及排除自引用 result
  的整个 raw artifact tree 均 content-addressed，tamper 或不完整 schedule 被
  verifier 拒绝；
- Coverage outcomes：`coverage_auc()` 对 endpoint carry-forward 的覆盖曲线做
  trapezoidal time-normalized AUC；`time_to_target()` 返回首次到达时间或
  right-censored budget。runner 修复时间序列线程固定多跑 `timeout+30s` 的偏差，
  benchmark 完成即停止并补一次 final-corpus sample，避免短任务 AUC 低估。时间
  序列现统一覆盖 serial、MPI、hybrid 与 AFL-only。campaign 期间每个 tick 只枚举
  目录并建立 metadata-only hard-link snapshot；被测作业完全结束后才对 snapshot
  做 content deduplication 和串行 showmap replay，避免 coverage measurement 与
  fuzzing/concolic 争抢封存 CPU。hybrid snapshot 包含初始 seed、所有 AFL/SymCC
  queue、未过滤 SymCC 输出以及 GRIMOIRE/honggfuzz 语料，内容去重避免同步副本在
  20k 上限抽样中挤掉独立输入。showmap 的 target extra arguments 同时用于所有
  中间点和最终点，空目录/缺失 showmap 标记为 measurement failure 而非真实零覆盖；
  CSV 写
  experiment/run/pair/status/budget/AUC；
- Cross-layer budget fidelity：protocol executor 通过 `SYMCC_CPU_CORES` 把 manifest
  的权威核数传给 inner runner，CSV 的 `allocated_cpu_cores` 与
  `wall_budget_seconds` 不再依赖 mode/np 猜测。serial 目标执行也透传公开 benchmark
  `.args`，使被测语义与 showmap replay 一致。MPI 的 wall/per-execution/idle
  timeout 以及 hybrid/AFL 初始化等待都被 campaign timeout 上界约束，移除了短任务
  会被内部 10/30 秒下界静默延长的问题；
- 配对统计：`analyze_ablation.py` 使用 pair id（legacy 回退 round）计算 paired
  median-difference bootstrap CI、two-sided sign-flip randomization p-value、
  direction-aware Vargha-Delaney A12/Cliff's delta 和 Holm family-wise correction。
  summary 显式报告 total/success/failure/timeout/censored/paired counts；少于 20 个
  有效 pairs 或含缺失 pair 的比较只能标为 exploratory；
- 自动化证据：`test_research_protocol.py` 覆盖 20-repeat 完整性、随机配对、等 CPU、
  underpowered/tamper rejection、AUC/censoring、success/failure/timeout/resume；
  `test_ablation_analysis.py` 覆盖 lower-is-better、failure retention、paired test、
  Holm、AUC 和 CLI schema；`test_afl_profile_orchestration.py` 另验证 live-corpus
  union 去重、extra-argument routing、manifest core override 与 final sampling；
- 工程冒烟结果：临时构建 maze native/AFL 二进制，以 `np=2`、2 秒 MPI simulation、
  1 秒采样运行完整 runner；实际 `afl-showmap` 产出 `t=0/1/2s` 三点、17/64 edges、
  `coverage_auc=17.0`，`analyze_ablation.py` 成功读取 benchmark/timeseries CSV 并
  生成两行统计工件。该结果验证链路，不构成性能优越性证据；
- 本轮全量回归：Python unittest 233/233、LLVM lit 90/90、完整 build、schedule
  runtime 严格 `-Wall -Wextra -Werror`、`py_compile` 与 `git diff --check` 均通过；
- 当前等级 I/T。框架现在能生成 R 级实验所需工件，但仓库尚未实际完成多目标
  20-repeat confirmatory campaign，因此现有算法效果仍不能升级为 R。CPU affinity
  控制单机 process tree，不等价于跨节点频率、NUMA、温控和共享存储完全隔离；这些
  必须在实验 manifest 与论文 threats-to-validity 中另行控制。时间序列快照仍有
  目录遍历/硬链接 metadata 开销；跨文件系统时会退回复制，因此正式实验应把 work
  directory 与 snapshot 放在同一文件系统，并把 post-campaign showmap 时间计入各
  配置相同的 `wall_grace_seconds`，但不计入 coverage/CPU 分母。

## 63. F68：Conservative LLVM-to-Continuation Lowering and Feasibility Pruning（2026-07-27）

- 研究问题：F66 已能执行、暂停和迁移手写 `symcc-live-program-v1`，但真实
  LLVM/C target 无法进入该路径；branch fork 也没有检查完整 path condition，
  会保留明显 UNSAT 的 child。F68 的目标是建立一个可运行、可拒绝、可扩展的
  compiler lowering 入口，而不是用部分翻译伪装成完整 LLVM 语义；
- 技术依据：GenSym 将 symbolic execution task 编译为 continuation，并用
  cooperative concurrency 暴露并行状态。实现参考
  [GenSym ICSE 2023 论文](https://continuation.passing.style/static/papers/icse23.pdf)
  的 compiled-continuation 方向；PHI 的 edge semantics、poison/undefined behavior
  边界以 [LLVM Language Reference](https://llvm.org/docs/LangRef.html) 和
  [LLVM Undefined Behavior Manual](https://llvm.org/docs/UndefinedBehavior.html)
  为规范依据；
- Compiler pass：新增 legacy/new-PM `live-continuation-export` module pass。
  `SYMCC_LIVE_PROGRAM_OUT` 启用导出，`SYMCC_LIVE_ENTRY` 选择入口，
  `SYMCC_LIVE_STRICT=1` 将保守拒绝升级为编译失败。pass 先对 promotable scalar
  allocas 执行 mem2reg，再只遍历入口可达的 direct internal call graph；
- 支持子集：最多 64-bit integer arguments、little-endian symbolic input binding、
  integer arithmetic/bitwise、icmp、trunc/zext/sext/bitcast、select、conditional
  branch、switch dispatch chain、direct non-variadic internal call、return、
  `llvm.assume`、lifetime/debug intrinsic，以及由 `assume false` 表示的
  `unreachable` path pruning；
- PHI 正确性：每条进入 PHI block 的 CFG edge 生成独立 continuation block。
  incoming values 先全部写入 `phi_in_*` temporaries，再统一 commit 到 PHI
  destinations，保持 LLVM PHI 的 parallel assignment 语义；交叉 PHI loop 测试
  会在一次迭代中交换两个值并返回 `2`；
- 保守拒绝：pointer/vector/FP、不可提升 memory、indirect/external/variadic call、
  `invoke`/`callbr`、reachable recursion、exception、division/remainder、
  symbolic/out-of-range shift、`nsw`/`nuw`/`exact` 和其他可能越过当前
  poison/UB 模型的 instruction 不产生半可执行程序，而是输出
  `symcc-llvm-continuation-lowering-v1` rejection report。完整 lower 成功时才输出
  `symcc-live-program-v1`；
- Feasibility：`LiveContinuationExecutor` 将 CAS expression DAG 和全部 parent-linked
  solver frames lowering 为 QF_BV SMT-LIB，调用已有 `symcc-query-solver --generic`。
  `assume` 和 symbolic branch 在提交 child 前查询 SAT；UNSAT child 被剪除，
  timeout/unavailable/unsupported 为 `unknown` 并保守保留。结果记录 solver 名称、
  check/pruned/unknown counters；
- Concrete bit-vector 修复：constant/input 按声明位宽归一化；`eq/ne` 因此正确处理
  `i8 -1 == 255`。有符号除法和余数改用精确整数的 toward-zero 计算，不再经 Python
  float 丢失 64-bit 精度。LLVM exporter 仍拒绝 division，因为 LLVM 的除零和
  signed overflow 需要额外 UB guard；
- 驱动与 MPI：`llvm_to_continuation.py` 可从 `.ll/.bc` 经 `opt`，或从 C/C++ 经
  Clang plugin 原子发布 artifact；`symcc_live_state.py run-llvm` 完成
  lower/create/resume。MPI 的 `SYMCC_LIVE_LLVM` 自动生成程序并进入 F66 state
  lease；显式 `SYMCC_LIVE_PROGRAM` 优先，不会被 LLVM source 意外覆盖；
- 自动化证据：`live_continuation_lowering.ll` 覆盖 direct call、switch、select、
  symbolic fork、普通 PHI、交叉 PHI、unsupported load rejection、独立 lowering
  CLI 和 `run-llvm`；`live_continuation_source.c` 覆盖 C source 编译入口；
  `test_distributed_state.py` 覆盖 derived infeasible branch pruning、negative
  bit-vector normalization 和 exact signed division；同一 lit 还验证 recursion
  rejection report 会由 CLI 保留并以非零退出，以及 `unreachable` path 不产生
  spurious return。本轮全量结果为 Python unittest 236/236、LLVM lit 92/92、
  完整 compiler build、`py_compile` 和 `git diff --check` 通过；
- 当前等级 I/T。F68 是 conservative bounded integer-SSA frontend，不是完整
  GenSym 或 arbitrary native continuation。下一阶段需实现 object-aware pointer/
  memory lowering、allocation/lifetime、exception/indirect/external effect
  virtualization、可信 poison/freeze 模型、跨节点 CAS replication，并执行 F67
  定义的公开目标等 CPU confirmatory campaign。

## 64. F69：Object-Aware Static Memory Continuation Lowering（2026-07-27）

- 研究问题：F68 对任何 LLVM load/store 一律拒绝，而 F66 手写 IR 的 memory op
  又固定为单字节，并会把 symbolic address 直接 concretize。前者使真实 parser/
  stateful C 函数难以进入 continuation path，后者会在未知地址上产生不健全执行；
- 设计原则：本阶段采用 object-aware、fail-closed 的静态内存子集。只为入口可达
  instruction 实际引用的、已初始化、address-space 0、非 TLS global 分配确定性
  synthetic address；每个对象保留 base/size/read-only metadata。64-byte null
  redzone 与对象边界检查阻止 null/out-of-object access；
- Initializer lowering：按 LLVM `DataLayout` 的 alloc/store size、ABI/explicit
  alignment 和 target endianness 序列化最多 64-bit integer、
  `ConstantDataSequential`、array、struct、zero aggregate 与 null pointer。
  undef/poison、relocation expression、external/TLS global 或超过
  `SYMCC_LIVE_MEMORY_LIMIT` 的 image 输出 rejection；
- Pointer subset：`GEPOperator::accumulateConstantOffset()` 计算 constant GEP，
  并相对原 object 验证负偏移、one-past 与 access width。dynamic/symbolic GEP、
  nonzero address space、pointer argument、stack/heap object 和未知 provenance
  保守拒绝。当前不使用 synthetic numeric address 推断 LLVM pointer provenance；
- LLVM memory lowering：non-volatile/non-atomic integer load/store 最多 8 bytes，
  artifact 显式记录 `bits`、`bytes`、endianness、initial `memory_hex` 和
  `memory_objects`。写 constant global、越界、null 或跨对象 access 在 compiler
  阶段拒绝；
- Executor memory semantics：multi-byte load 将 concrete/symbolic page bytes 按
  target endianness组合为 bounded BV；symbolic store 使用 `lshr/trunc/zext`
  拆为独立 byte-expression roots，再复用 F59 page-COW。写入前先对 snapshot 做
  bounds read，禁止 `fork_memory()` 原有的隐式扩容掩盖 OOB；
- Defense in depth：`_validate_program()` 独立规范化 initial memory，限制 image
  为 4 MiB，检查 object bounds/non-overlap；执行每次 memory op 时再次检查 declared
  object 和 read-only 属性。没有 object metadata 的历史手写 IR 保持兼容；
- Symbolic address policy：executor 现在检测 address expression 是否依赖 input。
  发现 symbolic address 即显式拒绝，不再沿 concrete witness 静默读取单一地址。
  完整支持需要 array theory、bounded alias enumeration 或对象级 fork/ITE；
- 自动化证据：LLVM lit 覆盖 little-endian 32-bit initializer、constant string GEP、
  symbolic 4-byte store/load、memory-derived branch feasibility、constant-global
  store rejection 和 symbolic-GEP rejection；C source test 覆盖 mutable global
  round trip。Python tests 覆盖 big-endian 16-bit load、symbolic-address rejection
  以及对 tampered read-only object contract 的 executor recheck；
- 当前定向结果为 `test_distributed_state.py` 27/27、两个 continuation lowering lit
  files 2/2；本阶段全量结果为 Python unittest 239/239、LLVM/CMake lit 92/92，
  完整 compiler build、`py_compile` 与 `git diff --check` 通过；
- 当前等级 I/T。F69 不包含 non-promotable alloca、heap allocator、entry pointer
  buffer ABI、symbolic index/alias、pointer load/store/compare、relocation、TLS、
  atomic/volatile、exception 或 syscall state。下一阶段应先实现有显式 size contract
  的 input-buffer pointer ABI 和 frame-local stack objects，再研究 bounded symbolic
  address enumeration；不能把当前 static-global subset 称为完整 LLVM memory model。

## 65. F70：Explicit-Size Symbolic Input-Buffer Entry ABI（2026-07-27）

- 研究问题：F68 的 entry integer-argument ABI 适合小型函数验证，却不能表达 parser
  常用的 `parse(bytes, size)` 接口。仅把 pointer concretize 为任意整数会丢失对象
  provenance；按预留容量访问又会把短 seed 后面的零填充误当作合法输入；
- ABI contract：LLVM exporter 仅自动识别参数恰为
  `(address-space-0 pointer, integer <= 64 bits)` 的 entry。pointer 绑定到独立
  synthetic input object，size 由新 `input_size` continuation instruction 绑定到
  当前 seed 的实际字节数。其他含 pointer 的 entry 继续 fail closed；
- Bounded layout：输入对象位于 64-byte null redzone 之后，默认预留 65536 bytes。
  `SYMCC_LIVE_INPUT_BUFFER_LIMIT`、`SYMCC_LIVE_MEMORY_LIMIT` 和 size 参数可表示的
  unsigned range 共同限定最终 capacity；超过 capacity 的 seed 在 checkpoint
  创建前拒绝；
- Artifact contract：program 新增 `symcc-live-input-buffer-v1`
  `{address,capacity,size_bits}`，对象 metadata 使用
  `kind=input,logical_size=input-length`。validator 要求 descriptor 与唯一 input
  object 完全一致、普通 `input_size` 指令位宽一致且 legacy `input_size=0`，防止
  手工或篡改 artifact 混淆两种 ABI；
- Symbolic memory initialization：`LiveContinuationExecutor.create()` 仍为每个 seed
  byte 建立 canonical input expression，同时把 digest 写入输入对象对应的
  page-COW symbolic byte slot，并以 `@input:length` constant root 保存逻辑长度。
  program、symbolic store、memory pages 和 length 因而全部进入 checkpoint CAS；
  MPI worker 恢复时不需要重新读取 AFL seed 文件；
- Object safety：compiler 只 lowering capacity 内的 constant GEP/load/store；
  executor 每次访问再以 `min(capacity,current_seed_length)` 计算 input object 的
  effective size。短输入即使拥有更大的零填充 backing memory，也无法越过逻辑
  长度；symbolic/dynamic GEP 仍显式拒绝；
- 自动化证据：LLVM lit 覆盖 pointer/size metadata、实际长度分支、symbolic input
  memory branch、短 seed、runtime logical-OOB 和 `run-llvm` CLI；C source test
  覆盖短路条件产生的三条真实路径；Python tests 覆盖 seed-to-COW mapping、
  `@input:length`、capacity rejection、logical OOB 和 tampered contract；
- 结果：`test_distributed_state.py` 30/30、两个 continuation lowering lit 2/2；
  全量 Python unittest 242/242、LLVM/CMake lit 92/92、完整 compiler build、
  `py_compile` 与 `git diff --check` 通过；
- 研究依据与定位：该实现遵循 KLEE 的 object-bounded symbolic memory原则和
  GenSym 的 portable continuation/state 思路，但目前是可审计的 constant-offset
  子集，不是 array-theory memory 或任意 native continuation。参考：
  KLEE OSDI 2008
  https://www.usenix.org/legacy/event/osdi08/tech/full_papers/cadar/cadar.pdf ，
  GenSym ICSE 2023 https://continuation.passing.style/static/papers/icse23.pdf ，
  LLVM LangRef https://llvm.org/docs/LangRef.html ；
- 当前等级 I/T。F70 不包含 dynamic/symbolic index、pointer PHI/select/call
  propagation、frame-local stack、heap/lifetime、pointer load/store/compare 或
  external resources。下一阶段按依赖实现 frame-local stack object 与
  lifetime-aware frame reclamation，再引入 bounded alias enumeration。

## 66. F71：Frame-Local Fixed Stack Objects and Resumable Lifetime（2026-07-27）

- 研究问题：F70 已让 parser 输入进入 object memory，但局部 array/struct 等不能被
  mem2reg 的 alloca 仍会阻断真实 C 函数。若仅为 alloca 分配固定 backing bytes，
  顺序再次调用同一函数可能读取上次调用的陈旧内容，checkpoint 也无法区分哪些
  stack bytes 已在当前 frame 初始化；
- Compiler object model：lowerer 为 entry block 中 fixed nonzero、address-space 0、
  sized、non-scalable alloca 分配 ABI/explicit-aligned synthetic object。对象 metadata
  为 `kind=stack,function=<owner>,initialization=runtime-write-tracked`；所有对象仍受
  `SYMCC_LIVE_MEMORY_LIMIT` 和 non-overlap validation 约束；
- Conservative initialization proof：每个 stack load 必须有同一 alloca 上覆盖完整
  load byte range 的 non-atomic store，并由 LLVM `DominatorTree` 证明该 store
  dominates load。该证明支持较宽 store 覆盖较窄 constant-offset load，但保守拒绝
  分支分别初始化后 merge 的更复杂 must-initialize 情形；
- Pointer/lowering subset：alloca 与其 constant GEP 被 lowering 为确定性 pointer
  constant，最多 8-byte integer load/store 复用 F69/F70 endian-aware memory。
  dynamic/scalable alloca、symbolic GEP、pointer PHI/select、stack pointer 传入 callee、
  pointer-valued memory 和 escaping address 均不发布部分 artifact；
- Runtime frame safety：executor 只有在 object owner 等于当前 top frame function
  时才允许 stack access。store 为每个字节写 CAS symbolic-store marker
  `@stack:init:<depth>:<address>`；load 必须找到覆盖全部字节的 marker，防止 backing
  zero 或旧调用内容被解释为已初始化 LLVM value；
- Lifetime/reentry：markers 随 pause/fork checkpoint 持久化。return 在弹栈前删除
  当前 depth 的全部 marker 和 `<depth>:<ssa-local>`，因此同一非递归 callee 的顺序
  重入不会继承上一 invocation 的栈或局部绑定。program validator 在存在 stack
  object 时独立拒绝 reachable recursive call graph；
- 自动化证据：LLVM IR 覆盖 stack store/load、memory-derived symbolic branch、
  同一 callee 顺序两次调用、缺少 dominating store 和 dynamic alloca rejection；
  C source 以 `optnone` 局部数组验证真实 Clang alloca/GEP/lifetime 形态。Python
  tests 覆盖 marker 跨 pause/resume、return 清理、uninitialized/inactive access 和
  recursive contract rejection；
- 结果：`test_distributed_state.py` 33/33、两个 continuation lowering lit 2/2；
  全量 Python unittest 245/245、LLVM/CMake lit 92/92、完整 compiler build、
  `py_compile` 与 `git diff --check` 通过；
- 研究定位：当前使用非递归 call graph 上的 per-function synthetic slot，加上
  per-depth initialization/liveness state，等价于 bounded frame-local object
  virtualization；不是 native stack capture，也没有实现 LLVM undef 的任意值语义。
  研究依据包括 KLEE object memory、GenSym continuation state 与 LLVM alloca/
  lifetime semantics：
  https://www.usenix.org/legacy/event/osdi08/tech/full_papers/cadar/cadar.pdf ，
  https://continuation.passing.style/static/papers/icse23.pdf ，
  https://llvm.org/docs/LangRef.html ；
- 当前等级 I/T。F71 不包含 dynamic alloca、递归 frame address、跨函数 stack
  pointer、path-sensitive must-initialize dataflow、heap allocation/free、
  use-after-return poison、symbolic alias 或 exception unwinding。下一阶段应实现
  bounded heap allocation/lifetime；随后统一 stack/input/heap 的 bounded alias
  enumeration。

## 67. F72：Bounded Call-Site Heap Objects and Resumable Lifetime（2026-07-27）

- 研究问题：F71 已能虚拟化固定栈对象，但真实 C 数据结构仍常通过 `malloc/free`
  获得动态生命周期。直接把 heap backing bytes 当作永久 static memory 会让
  use-after-free、double-free、未初始化读取和同调用点重分配读取旧内容在迁移后被
  静默接受；把宿主 allocator 地址写入 checkpoint 又会破坏跨进程可移植性；
- Compiler allocation model：lowerer 仅识别 direct declaration
  `malloc(constant nonzero size)`，为每条 allocation call site 分配 16-byte aligned
  synthetic slot。单对象默认上限为 65536 bytes，由
  `SYMCC_LIVE_HEAP_OBJECT_LIMIT` 控制，并继续受全局
  `SYMCC_LIVE_MEMORY_LIMIT`、64-bit continuation operand 和目标 pointer range
  限制。动态 size、非零 address space、无效 pointer width 或越界 image 输出
  structured rejection；
- Artifact/lowering contract：对象 metadata 为
  `kind=heap,site=<stable-id>,lifetime=runtime-alloc-free,
  allocation=bounded-infallible`。新增 `heap_alloc` continuation op 返回该调用点的
  synthetic base，`heap_free` 只接受 null 或已支持 heap object 的精确 base；
  interior pointer free、calloc/realloc 和未知 allocator 不做不可信近似；
- Pointer 与 initialization 子集：malloc pointer 及 constant GEP 延续 object
  provenance。最多 8-byte integer load/store 复用 F69 的 endian-aware page-COW。
  compiler 要求 heap load 由 allocation 和覆盖完整字节范围的 store 支配；runtime
  仍逐次复检，因此 loop/reallocation 等动态路径不会只依赖静态证明；
- Resumable lifetime：allocation 在 symbolic store 写
  `@heap:live:<base>`，store 为每个被写字节写
  `@heap:init:<address>`。load 必须同时满足 declared bounds、live marker 和全部
  init markers；free 删除 live 以及对象范围内全部 init markers。marker 作为
  checkpoint CAS 的一部分随 fork、pause、迁移和恢复保持一致；
- Reallocation 与错误语义：释放后重新执行同一 allocation site 可复用固定 slot，
  但旧 backing bytes 因 init markers 已清除而不可读。重复执行仍存活的 allocator
  报 already allocated；double-free 报 not allocated；free 后访问报 inactive
  heap；未初始化读取独立报错。`free(NULL)` 为 no-op；
- Defense in depth：program validator 独立验证 heap metadata、site 唯一性、
  allocation instruction 与对象 address/size/site 的一一对应及 object
  non-overlap。由于每个 call site 只有一个静态 slot，含 stack 或 heap object 的
  reachable recursive call graph 均 fail closed，避免递归重入产生地址别名；
- 有意建模边界：本阶段采用 bounded-infallible allocation，即合法 constant-size
  malloc 总返回非 null synthetic pointer，不探索资源耗尽分支。一个 call site
  不能同时拥有多个 live instances；pointer PHI/select、跨函数/内存 escape、
  symbolic index、allocator family、native allocator metadata 和任意 heap graph
  不在可信子集内。这是可迁移 object lifetime virtualization，不是完整 libc heap，
  也不是 POSE 的 path-optimal heap search；
- 自动化证据：LLVM IR 覆盖 round trip、memory-derived symbolic branch、UAF、
  double-free、未初始化 load、dynamic-size malloc 和 interior free；C source
  覆盖真实 Clang malloc/store/load/free 形态。Python tests 覆盖 live/init marker
  跨新 executor 恢复、free 清理、同调用点 loop 重分配、UAF/double-free/
  uninitialized、篡改 contract、重复 site 和 recursive reentry rejection；
- 结果：`test_distributed_state.py` 37/37、两个 continuation lowering lit 2/2；
  全量 Python unittest 249/249、LLVM/CMake lit 92/92、完整 compiler build、
  `py_compile` 与 `git diff --check` 通过；
- 研究定位与依据：object-bounded access 与显式 lifetime 继承 KLEE memory-object
  设计，portable checkpoint/continuation 继承 GenSym 的状态编译方向；POSE 提醒
  heap-manipulating path exploration 还需更强的路径等价与别名处理。参考：
  KLEE OSDI 2008
  https://www.usenix.org/legacy/event/osdi08/tech/full_papers/cadar/cadar.pdf ，
  GenSym ICSE 2023 https://continuation.passing.style/static/papers/icse23.pdf ，
  POSE SANER 2026
  https://conf.researchr.org/details/saner-2026/saner-2026-papers/7/Path-Optimal-Symbolic-Execution-of-Heap-Manipulating-Programs ，
  LLVM LangRef https://llvm.org/docs/LangRef.html ；
- 当前等级 I/T。下一阶段应统一 input/global/stack/heap pointer provenance，实施
  bounded symbolic offset/alias enumeration 与 ITE memory；再扩展 pointer
  PHI/select、跨函数对象引用、calloc/realloc 和多 live instance allocator。

## 68. F73：Bounded Symbolic Alias Enumeration and Finite ITE Memory（2026-07-27）

- 研究问题：F69--F72 的 object bounds 已阻止未知地址按 concrete witness 静默执行，
  但 constant-only GEP 仍无法处理 array/table/parser 的 symbolic index。若直接对
  当前 seed 地址读写，会丢失其他可行索引；若每次为所有地址 fork 完整 state，又会
  造成 alias count × branch count 的过早 state amplification；
- Compiler GEP decomposition：LLVM 18 `collectOffset()` 将 GEP 分为 constant
  offset 和 `Value -> APInt scale`。lowerer 仅接受一个 integer variable term、
  pointer/index width <= 64 bits、pointer width 等于 DataLayout GEP index width、
  非零 signed scale，以及已知 input/global/stack/heap base provenance。动态 GEP
  后的 constant GEP 继续传播原始 index/scale/object provenance；第二个 variable
  term、pointer escape 和非标准 pointer/index-width 组合继续 fail closed；
- Finite alias contract：对每次 load/store，compiler 根据 object base/size、
  constant offset、signed index range、scale 和 access byte width 枚举 no-wrap
  defined subdomain 内的全部 in-object candidate addresses。默认最多 16 个，由
  `SYMCC_LIVE_ALIAS_LIMIT` 调整，硬上限 256；集合为空或超限输出 rejection。
  artifact 以 `pointer_offset` op 保存 modular
  `base + sext(index) * scale`；memory op 同时保存去重 alias address list、
  `alias_index`、`alias_index_bits` 和闭区间
  `alias_index_min/max`，并发布 `bounded-symbolic-alias` capability；
- LLVM wrap/poison soundness：仅有 `address == alias` 不足以排除大索引经 BV
  multiplication wrap 后重新落回对象的模型，尤其会错误接纳 `inbounds` poison。
  executor 因此把 signed `min <= alias_index <= max` 与每个 address guard 相与后
  才形成 domain。非 `inbounds` GEP 的合法 wrapped-object 路径当前也被保守剪除，
  这是显式的 no-wrap bounded subset，而不是把 wrapped witness 当作普通别名。
  每个 `inbounds` 动态结果都与已有区间求交；若 constant `inbounds` GEP 的 base
  已是动态指针，则 base 和 result 的对象内区间都求交，避免中间 poison 被最终
  对象内地址掩盖；
- Independent validation：executor validator 检查 pointer width、index width、
  nonzero constant scale、alias index signed bounds、alias count/uniqueness，以及
  所有 candidate 必须完整落入同一 compatible declared object。runtime 复检
  pointer/index/scale operand width，再应用 input actual length、top stack frame、
  heap liveness 和 read-only contract；artifact 不能借 alias list 跨对象写入；
- Symbolic load：executor 为每个 candidate 读取 endian-aware COW bytes，生成
  `address == candidate` guard，并以 nested ITE 合成一个 BV value。所有合法
  guard 的 OR 被加入 CAS path-condition frame；stack/heap candidate 还与逐字节
  initialization condition 相与。因此 OOB 或未初始化 alias 是不可行的 LLVM
  defined-execution path，而不是被 backing zero 补全；
- Symbolic store：value 先按端序拆为 byte expressions；每个可能被写 byte 更新为
  `ite(address == candidate, new_byte, old_byte)`。候选访问区间重叠时，更新以原
  byte 为底递归组合，不按循环顺序丢失其他 alias。只有这些 byte pages 进入 F59
  page-COW fork，不为每个 alias 复制整份 checkpoint；
- Conditional initialization：stack/heap init marker 从“仅检查 key 是否存在”提升为
  1-bit CAS expression。symbolic store 将 marker 更新为
  `old_init OR address_guard`；symbolic load 的 domain 同时要求相应 marker。
  marker 随 pause/fork/resume，heap free 和 frame return 仍按生命周期清理；
- Delayed state splitting：alias load/store 本身保持单状态 finite functional
  memory；只有后续 branch/assume 依赖 ITE value 时才由既有 feasibility path fork。
  这与 eager per-alias state fork 语义等价于当前 bounded QF_BV subset，同时降低
  checkpoint 和 solver-frame 扩增；
- 自动化证据：LLVM IR 覆盖 readonly symbolic load、guarded global store、
  dynamic heap/stack/input store/load、constant-after-dynamic nested GEP、multi-term/
  alias-limit/pointer-index-width rejection；C source 覆盖真实
  `values[input & 3]` pointer reuse。Python tests 覆盖 finite ITE branch fork、
  symbolic store、2-byte overlapping alias update、heap conditional init marker
  跨 executor checkpoint、wrapped-index infeasibility、两种 chained-inbounds
  intermediate-domain、missing/duplicate alias contract 和 zero-scale rejection；
- 结果：`test_distributed_state.py` 43/43、continuation lowering lit 3/3；
  全量 Python unittest 255/255、LLVM/CMake lit 93/93、完整 compiler build、
  `py_compile`、文档链接与 whitespace checks 通过；
- 研究定位：这是 KLEE-style object bounds 上的 finite alias expansion，并借鉴
  symbolic-array read-over-write 的 ITE encoding；它不是通用 SMT Array memory。
  当前没有跨对象 alias、pointer PHI/select、pointer load/store、两个 symbolic
  index term、unbounded table、nonstandard pointer/index-width DataLayout、native
  address space、wrapped non-inbounds alias completeness 或 alias-set subsumption。
  alias domain 作为 path condition 保存，但 executor 不为新 domain立即求一个新
  concrete model；最终候选仍需既有 solver/materialization 与 concrete replay；
- 当前等级 I/T。下一阶段应实现 pointer PHI/select 的 provenance union 和
  path-sensitive alias-set merge，再研究跨函数 object references、multi-instance
  heap identity 与按需 SMT Array fallback；超过 finite bound 时继续拒绝，不能
  静默 concretize。

## 69. F74：Guarded Pointer PHI/Select Provenance Union（2026-07-27）

- 研究问题：F73 只允许一个 memory op 的所有 candidate 属于同一 object，因此
  `select cond, &a[i], &b[i]` 和由控制流 merge 产生的 pointer PHI 会被拒绝。若只
  保留最终 numeric address，就会丢失“哪条边选择了哪个 object”的 provenance；
  这对地址相同的 frame-local slot、heap lifetime、read-only 属性和 null/UB 分支
  尤其不可靠；
- 双轨 pointer 表示：pointer SSA 仍以目标 pointer width 的 BV 执行普通
  `select` 或 PHI edge copy，保持真实地址依赖；compiler 另行递归构造
  `PointerAlternative={StaticPointer,guards}`。select 两臂分别附加
  `condition==1/0`，PHI 则为每个 incoming edge 写入独立 32-bit discriminator，
  再附加 `pointer_tag==incoming_index`。因此 provenance 不依赖 synthetic address
  是否唯一，也不会把相同地址的不同控制流来源错误合并；
- Artifact contract：union memory op 使用 `alias_cases`，每个 case 保存一个对象内
  的非空去重 `addresses`、非空 `guards`，以及可选的 F73
  `alias_index/index_bits/min/max`。一个 case 内必须属于同一 compatible object，
  但不同 case 可以属于不同 global/input/stack/heap object。旧的单对象
  `aliases` schema 保持兼容，二者同时出现会被 validator 拒绝；
- 有界性：递归 provenance 最多 256 alternatives，单 case 和整个 union 的地址
  总数均受 `SYMCC_LIVE_ALIAS_LIMIT` 约束，executor 对外部 artifact 另设 256
  aliases 和每 case 64 guards 的硬上限。limit 作用于 union 总量，不能通过拆成
  多个对象 case 绕过；
- Executor 语义：`_memory_alias_candidates()` 对每个 case 独立恢复 owner，
  将 selection/PHI guards、可选 signed no-wrap index domain 和
  `address==candidate` 相与。load 在全部 guarded candidates 上生成 finite ITE；
  store 按 candidate owner 生成逐字节 guarded read-over-write。所有有效
  candidate condition 的 OR 进入 path frame，memory op 本身仍不 eager fork；
- Object/lifetime 复检：每个 case 独立应用 input actual length、top stack frame、
  heap live marker、read-only 和 byte bounds。未选择的 inactive stack/heap case
  不制造 runtime error；null load arm和 read-only store arm不生成 candidate，
  其 guard 因 union domain 被剪成不可行。含 stack/heap 的 union load仍要求同一
  pointer SSA 上有 dominating full-width store，heap allocation必须支配 load；
- 自动化证据：LLVM lit 覆盖跨 global pointer select/PHI load、跨对象 guarded
  store、每臂 one-term dynamic GEP、null arm pruning、整个 union alias limit 和
  cyclic PHI fail-closed；C source 以真实 ternary pointer 和显式
  memory-derived branch 验证 Clang frontend。Python tests 覆盖手工 artifact 的
  cross-object load fork、per-object guarded store，以及 empty guards、
  mixed-object case、legacy/union contract 冲突拒绝；
- 结果：全量 Python unittest 258/258、LLVM/CMake lit 93/93、
  `cmake --build build -j2` 通过。F74 定向 LLVM、C 与 executor tests 全部通过；
  `py_compile`、Markdown links 与 whitespace 检查见本阶段最终验证记录；
- 研究定位：该设计接近 object-sensitive value-set analysis 与 guarded symbolic
  memory expansion的交叉点，并延续 GenSym portable continuation 与 KLEE
  object-bound memory 的原则。它保留 path guard 后再做 finite expansion，而不是
  根据当前 concrete pointer 猜唯一对象；但它仍不是全程序 points-to analysis、
  通用 SMT Array memory 或完整 LLVM pointer semantics；
- F74 阶段仍保守拒绝 cyclic pointer PHI、以 select/PHI 为 base 的后续 GEP、
  pointer load/store/compare、跨函数 pointer 参数/返回/escape、multi-term GEP、
  dynamic/multi-instance allocator、nonzero address space 和超过 finite bound 的
  union。F75 已补上 bounded multi-instance identity 与 GEP-after-union；其余边界
  见下一节。未通过 R 级 benchmark 前不宣称 coverage 或性能达到 SOTA。

## 70. F75：Bounded Multi-Instance Heap Identity（2026-07-28）

- 研究问题：F72 为每个 `malloc` call site 只预留一个对象，循环或多路径中第二个
  未释放实例会触发 double-allocation。直接把 site 当作 object identity 会错误合并
  同站点的不同 lifetime；使用宿主 allocator 地址又会破坏 checkpoint 的跨进程
  可移植性。F75 因此把 “allocation site” 与 “runtime instance slot” 分开建模；
- Compiler pool layout：每个 direct `malloc(constant nonzero size)` site 预留固定
  容量的 16-byte aligned synthetic slots。`SYMCC_LIVE_HEAP_SITE_CAPACITY` 默认 4，
  用户范围 1--64；每个 slot 仍受 `SYMCC_LIVE_HEAP_OBJECT_LIMIT`、全局
  `SYMCC_LIVE_MEMORY_LIMIT` 和目标 pointer range 约束。空间不足时整个 lowering
  structured reject，不生成截断 pool；
- Stable identity 与 artifact：每个 heap object 保存同一 stable `site` 及不同
  `slot`，并携带 `capacity`、`allocation=bounded-pool-infallible`、
  `lifetime=runtime-alloc-free`。`heap_alloc` 保存严格按 slot 排序的 `addresses`
  和 `capacity`；validator 要求一个 site 恰有 `capacity` 个对象、slot 完整覆盖
  `[0,capacity)`、大小一致、地址不重叠且只有一条 allocation instruction。旧的
  `bounded-infallible`/`address` 单槽 artifact 仍可读取，但不能与 pool contract
  混合；
- Canonical allocation semantics：executor 每次选择地址序中第一个没有
  `@heap:live:<base>` marker 的 slot。该 first-free 规则在当前不允许 pointer
  ordering、跨函数 escape 和任意 allocator metadata 的子集中，给 heap-isomorphic
  allocation history 一个确定性代表；它减少仅由可交换空闲 slot 命名造成的状态
  分裂，但不是对所有 C heap observation 的等价性证明；
- Exhaustion 与 lifetime：所有 slot 存活时，执行器显式报
  `continuation heap pool is exhausted`。当前 bounded-infallible 模型不合成
  `malloc == NULL` child，因此 exhaustion 是模型边界而非目标程序可见分配失败。
  `heap_free` 清理被选 slot 的 live/init markers；后续分配可 first-free 复用该
  slot，且旧 backing bytes 仍因 init markers 清除而不可读；
- Pointer provenance：malloc SSA 不再绑定一个静态地址，而是产生每个 slot 一个
  `PointerAlternative`，guard 为 `malloc_result == slot_base`。普通 load/store
  复用 F74 `alias_cases`，把 slot guard、对象 owner、可选 F73 index domain 和
  numeric address equality 一起验证；因此同站点多个 live instance 不会因共享
  site ID 被错误合并；
- GEP-after-union：GEP provenance 改为递归应用到每个 base alternative；生成的
  runtime pointer BV 则从真实 base SSA 加 constant/one-term symbolic offset，
  而不是从某个 synthetic static base 重建。这同时支持 malloc pool pointer 和
  select/PHI 后的 GEP，并保持 address semantics 与 object provenance 双轨一致；
- Initialization proof 修正：union load 的 dominating-store 检查不再要求 store
  与 load 使用同一个 pointer SSA，而是比较每个 alternative 的 object/slot base、
  offset、dynamic index、scale 与覆盖宽度。这既处理 Clang `optnone` 生成的等价
  不同 GEP SSA，也保持“每个可选对象均已完整初始化”的 must-property；
- 自动化证据：LLVM IR 在同一 loop allocation site 连续产生两个同时 live 的
  实例并返回 21；容量 2 的第三次分配验证 exhaustion。C source 使用真实 malloc
  loop 验证 frontend。新增 union 后 GEP 用例区分两个 global array。Python
  executor tests 在第一次分配后暂停，确认只有 slot 0 live，恢复后确认 slot 0/1
  的 live/init markers 均持久化；并验证 exhaustion、缺失 slot 和地址乱序
  artifact fail closed；
- 验证结果：`cmake --build build -j2`、全量 Python unittest 260/260、
  LLVM/CMake lit 93/93、`test_distributed_state.py` 48/48、Python syntax 和
  whitespace checks 通过。pool contract tests 还覆盖过窄 pointer width 与缺失
  explicit capacity 的载入期拒绝；
- 研究定位：这是 allocation-site abstraction、object-sensitive finite value set
  与 canonical heap naming 的有界组合。它为后续 POSE 风格 heap-state quotient
  和 live-state search 提供稳定 instance identity，但尚未实现 graph
  isomorphism、lazy object materialization、path-optimal state merging 或完整
  libc allocator；
- F75 阶段仍保守拒绝 dynamic malloc size、calloc/realloc/custom allocator、
  symbolic pointer-union free、pointer-valued memory/compare、跨函数 pointer
  参数/返回/escape、cyclic pointer PHI、multi-term GEP、超过 finite alias/pool
  bound、nonzero address space 和 resource-exhaustion branch。F76 已补上 bounded
  direct-call pointer/object references；其余边界见下一节。在真实
  heap-manipulating benchmark 完成等 CPU 多轮实验前，不宣称 POSE 等价或
  coverage/performance SOTA。

## 71. F76：Bounded Cross-Function Pointer/Object References（2026-07-28）

- 研究问题：F68--F75 的 direct internal call 只接受 integer 参数/返回值。把 pointer
  仅作为目标宽度 BV 传入 callee 虽能保留数值，却会丢失 underlying
  global/input/stack/heap object、read-only、lifetime 和 initialization owner；
  反过来把 caller 的 LLVM `Value*` guard直接写进 callee artifact 又会跨越 frame
  namespace，无法 checkpoint/replay。F76 因此在数值 SSA 之外生成 bounded
  interprocedural object-reference summary；
- Summary 构造：对每个 non-entry pointer formal，compiler 扫描全部可达 direct
  call sites，递归恢复 actual 的 F74/F75 alternatives，去重
  `(object,address,offset)` 后在 callee 中重写为 `formal_arg==candidate_address`
  guards。synthetic memory objects 全局不重叠、call graph 无递归，因此数值相等可
  作为当前 bounded subset 的稳定 object discriminator；候选硬上限 256，memory
  operation 仍受 `SYMCC_LIVE_ALIAS_LIMIT` 总量限制；
- Pointer return：internal pointer call 汇总 callee 所有 return sites 的 object
  alternatives，并在 caller 重写为 `call_result==candidate_address`。返回 formal
  pointer 可以继续跨多层 forwarding；global/input 和仍存活的 heap object 可以
  返回。数值依赖不由 summary 重新构造，而由 executor 把 callee return expression
  digest 原样绑定到 caller call SSA，因此 guard 与真实 return relation共享一个
  expression DAG；
- Stack lifetime：caller stack pointer 传入 callee 时，memory object 仍声明原
  owner function。executor 在无递归 frame stack 中唯一定位 owner depth，并以该
  depth 读取/写入 `@stack:init:<depth>:<address>`；callee 内 pause/checkpoint 后
  owner frame 与 markers 一起恢复。callee return 时只清理自己的 depth，所以对
  caller stack 的初始化效果保留；
- Escape 边界：任何 return alternative 若 rooted in 当前 callee 的 alloca，
  lowering 立即以 `pointer return escapes the callee stack frame` 拒绝，不能依赖
  return 后的 runtime failure 假装安全。经 formal 传入、实际属于更外层 caller
  的 stack object 可原样返回，因为 owner frame 仍活动；recursive call graph
  继续 fail closed，避免 owner function 对应多个 depth；
- Initialization proof：local stack/heap alternatives 仍要求同函数 dominating
  full-width store 和 heap allocation dominance。标记为 interprocedural 的
  alternatives 不伪造跨函数 dominator proof，而由 runtime live/init marker
  强制检查；未初始化 caller stack、inactive heap、read-only store 和 frame
  失活仍在执行时 fail closed；
- Artifact contract：internal function 可带
  `pointer_params=[{index,bits}]` 和 `pointer_return_bits`；call 必须带完全一致的
  `pointer_args` 与可选 `pointer_result_bits`，pointer return instruction 带
  `pointer_bits`。validator 独立复检 parameter index 唯一性、1--64 bit width、
  call arity、callee signature 和 result destination；runtime 再对 expression
  digest 的真实 width检查。旧 integer-only hand artifact 无需新增字段；
- 有意拒绝 symbolic actual：若 actual pointer 自身含 caller-side symbolic GEP
  term，F76 报 `cross-function symbolic pointer requires caller-domain transport`。
  仅把它展开成地址候选会丢失原 index 的 signed/no-wrap domain，并可能重新接纳
  wrapped pointer path。当前支持 constant/interior pointer、select/PHI union、
  heap-pool alternatives 和任意层数的 formal forwarding；后续必须把 caller
  domain certificate 作为显式 hidden argument 才能放宽；
- 自动化证据：LLVM lit 覆盖 pointer select 传给 helper 后 constant GEP/read、
  pointer formal 经 echo return 再传给第二 helper、callee 写入并读取 caller
  stack、callee 返回已初始化 heap pointer并由 caller load/free；负例覆盖 callee
  stack escape 和 caller symbolic-GEP domain。C source 使用 `noinline,optnone`
  helper 验证真实 Clang stack pointer 参数与 heap pointer 返回；
- Executor tests：手工 artifact 在 callee store 后暂停，确认 frame 为
  `[main,helper]`、marker 写在 depth 0 而非 depth 1，恢复后返回 41；pointer echo
  contract 返回 static byte 42，并对缺失 `pointer_args`、错误 return width
  fail closed；
- 验证结果：`cmake --build build -j2`、全量 Python unittest 262/262、
  LLVM/CMake lit 93/93、`test_distributed_state.py` 50/50、Python syntax 与
  whitespace checks 全部通过；两个 continuation lowering lit files 定向为
  2/2；
- 研究定位：这是 acyclic direct-call graph 上的 finite object-sensitive value-set
  summary，连接 GenSym portable frames 与 KLEE-style object bounds。它不是
  context-sensitive whole-program points-to、general escape analysis、separation
  logic heap summary 或 native ABI capture；候选数可能随 call sites 增长，超过
  bound 必须拒绝而非 concretize；
- 当前等级 I/T。仍拒绝 indirect/function-pointer call、recursion、varargs、
  caller-side symbolic-GEP actual、pointer-valued memory、pointer comparison、
  callee-stack escape、external allocator/resource、nonzero address space 和
  exception unwinding。F77 已补上 state-local incremental solver context；
  cross-function dynamic-domain transport 与 richer escape summary 作为后续
  pointer semantics 工作包。

## 72. F77：CAS-Rooted State-Local Incremental Solver Contexts（2026-07-28）

- 研究问题：F68 的 feasibility pruning 在每次 branch/assume probe 时恢复完整
  parent-linked CAS solver-frame chain、递归 lowering 全部 expression、写临时 SMT2，
  再启动一次 `symcc-query-solver --generic`。同一 symbolic branch 的 true/false
  alternatives 具有完全相同的 path prefix，却重复承担 O(path depth) 解析和进程
  启动成本；MPI worker 又按 lease 重建 executor，使 checkpoint 邻近性无法转化为
  solver locality。F77 将 immutable `path_condition_root` 直接用作 context identity；
- 三级 context acquisition：
  1. exact-root hit 直接取得已存在的 `z3::solver`，对 candidate assertion 执行
     `push/from_file/check/pop`；
  2. root miss 但 parent-root hit 时，新建 solver、复制 parent 的 asserted formula
     vector，只解析当前 CAS frame delta；
  3. worker 冷启动或 parent eviction 时，从完整 CAS frame chain 重建目标 root。
  第三级保证 cache 完全可丢弃，checkpoint 在不同进程/节点恢复时不依赖隐藏 solver
  pointer；
- Helper protocol：`symcc-query-solver --server` 保留既有 QueryStore 五字段请求，
  新增八字段 live request：
  `(request,timeout,root,parent,full-prefix,frame-delta,target,check-only)`。
  `check-only` 禁止为仅需 SAT/UNSAT 的 live probe 构造 solution generator；响应新增
  `prefix_parent_hit`，与既有 `prefix_cache_hit` 区分结构派生和 exact reuse。
  QueryStore/PSCache 的旧协议与输出字段保持兼容；
- Prefix representation：executor 把每个 CAS frame 规范化为
  `(input declarations, assertion lines)` fragment。child prefix 通过 declarations
  set union 和 assertion concatenation 构造，避免重复 declare 或重新遍历整个
  expression DAG。prefix/delta/target SMT files、frame metadata 和 fragment 均为
  有界 LRU/FIFO cache；CAS digest 使缓存键同时具备内容身份与跨 checkpoint 稳定性；
- MPI 生命周期：live executor 从 per-lease 临时对象提升为 per-worker 长寿命对象。
  `resume()` 只重置当次 telemetry，不清空 solver process、context、fragment 或 frame
  cache；正常 `TAG_STOP`/context-manager/析构路径关闭 helper 并删除临时 artifacts；
- 可信退化：`SYMCC_LIVE_INCREMENTAL_SOLVER=0` 可执行严格 one-shot 消融。server
  startup、协议、pipe、timeout 或 JSON 失败会打开 executor-local circuit breaker，
  关闭残留进程并回到完整 CAS path 的 `--generic` query。SAT/UNSAT 决策仍来自同一
  QF_BV formula；UNKNOWN、unsupported lowering 和 solver unavailable 继续保守保留
  state，不能因优化路径故障剪枝；
- 可观测性：execution result 分别记录 exact hit、parent hit、cold rebuild、当前
  context entries、server starts/failures、one-shot fallback、SMT artifact/
  fragment 与 CAS frame metadata cache hit/miss。计数按 `resume()` 清零，但 cache
  持续存在，因此 warm checkpoint resume 可以直接验证跨 lease/local invocation
  复用；
- 自动化证据：两层独立 symbolic branch 产生四条可行路径、六次 feasibility
  checks。结构性结果固定为 1 次 cold rebuild、2 次 parent derivation、3 次 exact
  hit；同一 executor 再恢复第一层 checkpoint 时为 2 次 exact hit、0 rebuild、
  0 process start。新 executor 冷恢复同一 checkpoint 为 1 rebuild + 1 exact hit，
  证明 CAS portability；关闭增量模式后六次 one-shot checks 仍得到相同四个结果。
  helper 在 server handshake 前退出会触发一次 circuit breaker，随后六次 one-shot
  仍保持等价；该负例还在 `PYTHONWARNINGS=error` 下验证 stdin/stdout/stderr 与
  子进程全部关闭。原 derived-UNSAT pruning 与 QueryStore PSCache server tests
  同时通过；
- 验证结果：`cmake --build build -j2`、全量 Python unittest 265/265、
  LLVM/CMake lit 93/93、`test_distributed_state.py` 53/53、F77 定向 3/3、
  legacy QueryStore tests 13/13 与 PSCache lit 1/1、Python warnings/syntax、
  whitespace checks 全部通过；
- 研究定位：该实现对应 Triereme/Marco 类 trace/solve 解耦中的 persistent prefix
  context，以及经典 incremental SMT 的 assumption-local push/pop。创新点不是重新
  命名 solver cache，而是把分布式 continuation 的 CAS root、solver context identity
  和可恢复 parent delta 统一为同一内容寻址坐标。parent-derived solver 当前复制
  asserted formulas，不声称复制 Z3 内部 learned clauses；exact-root context 可保留
  solver 自身在 push/pop 后保留的内部增量状态；
- 当前等级 I/T。已消除单 worker/state-local 的重复 solver process 和 prefix parse，
  但尚无 R 级公开 benchmark 证据，也未实现跨 worker solver-context transport、
  learned-clause serialization、distributed shared solver service、unsat-core-guided
  state subsumption 或 backend-neutral incremental protocol。F78 已在其上接入
  runtime-sized nullable allocator；后续应以 F77 telemetry 做 solver-cost/
  state-amplification 消融。

## 73. F78：Nullable Runtime-Sized Allocator 与 Conditional Heap State（2026-07-28）

- 研究问题：F72/F75 的 heap pool 为每个固定大小 `malloc` 调用点预留稳定对象，
  但把 live marker 的“键存在”当作已分配，并在容量耗尽时抛出执行器异常。该模型
  无法表达 `malloc == NULL` 分支、符号请求大小、`calloc` 溢出/零初始化或
  `realloc` 的条件 lifetime，也会把物理预留容量误当成目标程序可访问大小。F78
  将 heap state 从集合成员关系升级为内容寻址的布尔/位向量表达式；
- 双重边界：动态站点的每个 slot 由 `SYMCC_LIVE_DYNAMIC_HEAP_LIMIT` 给出物理上界，
  `@heap:size:<base>` 保存当前运行时逻辑长度。每次 concrete 或 finite-alias
  load/store 都把 `live(base) AND offset+width<=logical_size(base)` 加入 defined
  domain。于是 backing image 只提供有限枚举空间，不会授权越过请求长度的访问；
- Nullable first-free：`bounded-pool-nullable` allocation 对每个 slot 构造
  `select_i = valid_size AND all_previous_live AND NOT live_i`，返回按稳定地址排序的
  nested ITE 或 null。新 live、size 和逐字节 init marker 都以 `select_i` 条件更新，
  因此不同 OOM/slot 路径可以共享一个 expression DAG 并跨 checkpoint 恢复。零大小、
  超界和池耗尽成为目标可见 null 结果，不再是宿主异常；
- LLVM pointer failure path：compiler 支持 pointer `icmp eq/ne`，并继续以
  `result==slot_base` guards 保留 object provenance。null arm 不进入 memory alias
  domain；目标程序显式检查分配失败时可以正常 fork，未检查而直接解引用时则由
  defined-domain 约束排除 null/OOM 路径；
- Calloc：`calloc(count,size)` artifact 保存两个原始 bit-vector operands，不能先把
  它们在 compiler host 上相乘。executor 同时检查因子非零、wrapped product 除以
  safe count 后仍等于 element size，以及 product 不超过物理上界。选中 slot 的每个
  `offset<logical_size` 字节通过条件 ITE 写零并标记初始化；复用 slot 时旧内容不能
  泄漏到新的 calloc 对象；
- Realloc：当前实现选择 C 标准允许的 bounded in-place strategy，而不伪造任意
  moving allocator。`heap_realloc` 只接受有限 provenance 中的非空 heap base：
  `new_size==0` 条件释放并返回 null；`0<new_size<=capacity` 保持地址，保留
  `min(old,new)` 已初始化字节并把扩展区设为未初始化；超界返回 null且旧对象的
  live/size/init 全部保持。结果仍携带原对象 alternatives，可继续 GEP/load/free；
- Conditional free：dynamic allocator 返回值是符号 pointer BV，free 不再要求宿主
  可具体化地址。executor 对 null 或每个 `(address==base AND live)` 构造合法域，并
  条件清理 live/size/init。非法 base 与 double free fail closed；固定大小旧
  artifact 继续使用原有 concrete fast path；
- Artifact 与可信边界：validator 独立复检 nullable pool 的完整 slot 集合、
  `runtime-allocation-size`、operand width、physical max、calloc zero contract 和
  realloc base/strategy。新 lowering capabilities 为 `bounded-nullable-heap`、
  `runtime-sized-heap` 和 `bounded-in-place-realloc`。旧
  `bounded-infallible`/`bounded-pool-infallible` artifact 保持可读；
- 自动化证据：Python tests 覆盖 zero/超界 OOM、第三次分配池耗尽返回 null、物理
  槽内但逻辑对象外的访问、realloc 失败保留、扩展区未初始化和零长度释放；
  LLVM IR 与真实 C frontend 均覆盖 dynamic malloc、calloc、realloc 和 pointer-null
  branch。定向结果为 F78 continuation tests 6/6、`test_distributed_state.py` 61/61、
  LLVM/CMake lit 93/93，compiler `-Werror` build、Python syntax 与 whitespace
  checks 通过；包含 F79 后的全仓 Python unittest 为 273/273；
- 研究定位：该实现把 KLEE 风格 object bounds、allocation-site abstraction 和
  GenSym-style persistent state结合为有限、可迁移的 allocator relation。它不是
  glibc/jemalloc 模型，也不声称 POSE heap path optimality。仍未实现 moving
  realloc、`realloc(NULL,n)`、custom allocator summary、heap graph isomorphism、
  pointer-valued memory、unbounded object count或外部 allocator state；
- 当前等级 I/T。下一依赖项是 caller-domain symbolic pointer certificate，使跨函数
  symbolic GEP 不丢 no-wrap/domain 证明；随后是 indirect-call points-to 和
  effect-summary 驱动的 external virtualization。公开 benchmark 的 equal-CPU
  20-repeat 结果仍应单独记录为 R 级证据，不能由本节功能测试替代。

## 74. F79：Caller-Domain Symbolic Pointer Certificate（2026-07-28）

- 研究问题：F76 能把有限 object/address provenance 汇总到 direct callee，但遇到
  caller 内的 symbolic GEP 时只能丢弃动态 index，因而无法证明实际地址仍属于有限
  候选域。只传 numeric pointer 会把“当前 concrete 地址可用”误当成“所有 symbolic
  模型均合法”；只在 callee 重新做地址相等判断又会遗漏 caller 的 signed
  no-wrap/index range。F79 把这个定义域证明提升为显式、可求解且可恢复的调用约定；
- Certificate 构造：每个 pointer actual 的所有 provenance arms 分别合取
  select/PHI/heap-result guards 与动态 index 的 `sge(min)`、`sle(max)`，再对 arms
  做析取。结果不是 host boolean 或 metadata hint，而是 continuation expression
  DAG 中的 1-bit BV，因此可以依赖 symbolic input、跨 CAS checkpoint 保留并进入
  QF_BV feasibility check；
- 参数传输：compiler 在原始 ABI 参数之后追加稳定命名的
  `__ptr_domain_<argument-index>` hidden parameter，并输出 function-side
  `pointer_domains=[{index,parameter}]` 与 call-side
  `pointer_domains=[{index,argument}]`。Callee 的每个 finite object alternative
  同时带 `formal==candidate_address` 和 `domain==1` guards。动态 actual 被有限展开
  为 object 内候选 offset，默认排除 one-past 地址，最大集合继续受 256 项
  provenance bound 约束；
- 返回传输：pointer-returning function 声明 `pointer_return_domain=true`，每条
  pointer return 携带 `pointer_domain` expression；调用点用
  `pointer_domain_dst` 接收该表达式。后续 GEP/load/store/free 对 call-result
  provenance 加入 `result_domain==1`，因此经 identity/selector helper 多次转发后
  仍不能绕过原 caller 的定义域；
- Artifact 可信边界：validator 精确匹配 pointer 参数索引、hidden domain
  parameter/argument 位置、call arity、1-bit runtime width、pointer-return domain
  与 destination。缺失、重复、错位或与 callee contract 不一致的 mapping 在创建
  checkpoint 前拒绝；runtime 还复检每个 domain actual/return expression 的位宽。
  旧 F76 artifact 未声明 domain contract 时保持可读，但新 compiler artifact 以
  `caller-domain-pointer-certificate` capability 标明更强合约；
- 自动化证据：手写 continuation tests 覆盖合法 symbolic address、false
  certificate 导致 infeasible、缺失 call mapping、pointer-return domain 正向传输
  以及缺失 return/destination 的 tamper rejection。LLVM IR 与真实 C frontend 均
  将 `global + symbolic_index` 传给 direct callee 并正确读取；既有 pointer
  argument/echo/heap-return/caller-stack 用例继续回归。F79 定向 executor tests
  2/2、`test_distributed_state.py` 61/61、全仓 Python unittest 273/273、
  LLVM/CMake lit 93/93、compiler build、syntax 与 whitespace checks 均通过；
- 正确性边界：certificate 只证明 caller 已枚举的 finite object domain，不提供
  任意整数到指针的 provenance 恢复。当前仍保守拒绝多动态项 GEP、超过 alias bound
  的对象、递归、indirect call、callee-stack escape、pointer-valued memory 与
  external pointer effects。排除 one-past summary 是保守不完备选择，不会接纳
  LLVM UB 路径；
- 研究定位：该实现接近 capability-carrying pointer / proof-carrying state 的
  工程化形式，把 interprocedural points-to summary 与 path condition 放入同一
  content-addressed continuation state。它解决了 F76 的 caller-domain 丢失，但不
  等同于完整的 context-sensitive Andersen/SeaDsa heap graph。当前等级 I/T；
  下一依赖项是 bounded indirect-call points-to dispatch，再以 effect summary
  处理纯函数、只读 libc 与显式外部状态虚拟化。

## 75. F80：Bounded Indirect-Call Points-to Dispatch 与 Typed ABI（2026-07-28）

- 研究问题：此前任何 `getCalledFunction()==nullptr` 都与未知 external effect 一起
  被拒绝，即使 LLVM 已把目标限制成两个内部函数的 `select` 或 acyclic PHI。直接
  按 concrete witness 选择目标会漏掉路径；仅保存函数名列表又无法证明 runtime
  selector、PHI edge 和目标一致。F80 为 address-taken internal functions 建立有限、
  可验证的 continuation dispatch；
- 有限 points-to：compiler 递归解析 function constant/pointer cast、`select`、
  acyclic pointer PHI 和 null arm，最多保留 64 个 target cases。Reachability 与
  recursion analysis 同时加入全部间接边，不再只扫描 direct call。目标必须是
  non-variadic、非 intrinsic、非 external internal function，且 LLVM parameter/
  return types与 call site 精确一致；
- Stable function identity：address-taken targets 按函数名排序分配非零
  `function_id`；function-pointer SSA 以目标位宽的 BV ID 执行。`indirect_call`
  artifact 保存 `target` selector、`target_bits` 和
  `{function,id,guards}` cases。Runtime 对每个 case 同时证明
  `selector==function_id` 与 select/PHI discriminator guards，调用 Z3 筛除
  infeasible cases，并为多个可行目标创建独立 CAS continuation children；
- Typed continuation ABI：深度审查发现旧 schema 只显式记录 pointer widths，不能
  独立证明两个 integer callees 真正同签名。F80 因而为 compiler functions 增加
  `param_bits`/`return_bits`，call 增加 `result_bits`。Validator 要求 indirect
  targets 都具备 typed signature 且完整 tuple 相同；runtime 在参数绑定、callee
  return 和 caller result binding 三处复检 expression width。旧手写/direct-call
  artifact 未声明 typed fields 时保持兼容；
- Checkpoint/return：每个 dispatch child 保留相同 caller PC、symbolic store、
  COW memory 与 path root，只追加 target assertion 并压入普通
  `ContinuationFrame`。因此既有 return destination、pause/resume、lease/fencing
  和 caller-domain pointer argument逻辑无需旁路；solver UNKNOWN 时保守保留
  target，而不是错误剪枝；
- Artifact 可信边界：validator 复检 function ID 非零且全局唯一、case ID 与 callee
  metadata 一致、selector/guard width、target 数量、call arity、pointer-domain
  mappings、typed parameters/return 和 call graph。错误 ID、异构签名、缺失
  target、超过 bound 或递归间接边在初始 checkpoint 前 fail closed；
- 自动化证据：LLVM tests 分别覆盖 symbolic `select` 和 PHI function pointer，
  真实 C frontend 覆盖 typedef function pointer；两目标路径分别得到不同返回值。
  手写 artifact test 固定观察一次 target fork，并篡改 function ID 与 typed
  signature 验证拒绝。F80 阶段 `test_distributed_state.py` 62/62、
  LLVM/CMake lit 93/93、compiler `-Werror` build、Python syntax 与 whitespace
  checks 通过；
- 正确性边界：当前不支持从 pointer argument/heap/global memory 加载的 function
  pointer、超过 64 target、pointer-returning indirect call、varargs、musttail、
  inline asm、external target 或 recursive indirect SCC。Null arm没有 dispatch
  case，因此被定义域约束变成 infeasible。该实现是 bounded context-sensitive
  dispatch，不声称完整 Andersen/Steensgaard points-to；
- 研究定位：F79 让数据 pointer 携带 domain proof，F80 则让 control pointer
  携带有限 target proof，两者共享 QF_BV/CAS state 和 fork mechanism。当前等级
  I/T；下一项依赖是 effect-summary 驱动的 external-call virtualization，优先从
  deterministic/pure libc 与显式 memory region effects 开始。

## 76. F81：Deterministic External Memory-Compare Effect Summary（2026-07-28）

- 研究问题：将所有 external call 一律拒绝能保持保守性，却使真实 parser 中最常见
  的 magic/header 比较无法进入 live-state engine；把宿主 libc 结果具体化又会冻结
  symbolic bytes。F81 首先选择无写副作用、语义可有限展开的 `memcmp`/`bcmp`，
  建立 external effect virtualization 的最小可信基线；
- Lowering strategy：长度必须是 compile-time constant 且不超过 64 bytes。
  Compiler 为两侧每个 byte 生成既有 `load`，再用 `eq`、unsigned `ult` 与 nested
  `select` 构造 first-difference lexicographic result。结果规范化为目标返回位宽的
  `-1/0/+1`；这满足 C 对 `memcmp` 只保证返回值符号的契约，不能声称复刻特定 libc
  的字节差数值；
- No privileged opcode：summary 不增加 executor opcode，也不调用 host libc。
  每个 generated load 继续经过 object bounds、input logical length、heap
  live/logical-size、stack owner、read-only、per-byte initialization、finite alias
  与 caller-domain guards；CAS expression DAG、QF_BV solver、checkpoint 和 MPI
  resume 因而自动适用。Artifact capability 仅标记
  `bounded-external-effect-summary`，语义本体仍是可审计的普通 IR；
- Pointer unions：对 select/PHI/heap/cross-function pointer alternatives，byte
  offset 同时作用于 runtime numeric pointer 与每臂 object-relative provenance。
  Null/OOB/uninitialized arm不产生定义良好的 load path，defined-domain
  constraint 会剪掉相应状态；跨对象候选仍受全局 alias limit；
- Zero/bound contract：`n==0` 直接生成零值，不解析或解引用 pointer，因此
  `memcmp(NULL,NULL,0)` 保持定义良好。Symbolic length、`n>64`、非整数/过窄返回
  或非 pointer operands 结构化拒绝，不按 concrete witness 截断。64 是 artifact
  和 expression-size bound，不是目标程序 ABI 假设；
- 自动化证据：LLVM/C input-buffer tests 对 `ABC` 与 symbolic bytes 探索 equal/
  unequal；LLVM lexicographic test分别覆盖 `<`、`==`、`>`；zero/null、65-byte
  rejection 和 cross-object pointer-union compare 均有定向 RUN。F81 使用现有
  executor contract，因此不新增手写 schema。阶段 compiler `-Werror` build、
  `test_distributed_state.py` 62/62、全仓 Python unittest 274/274、LLVM/CMake lit
  93/93、Python syntax 与 whitespace checks 均通过；
- 正确性边界：尚不支持 symbolic length、locale、case folding、`strcmp`/
  `strncmp` 的 NUL termination、`strlen`、memory writes、errno、I/O、syscall、
  TLS、callback 或 nondeterministic external state。对 `bcmp` 返回任意非零值的
  代码可正确分支，但依赖具体非零数值的非可移植代码会看到 canonical sign；
- 研究定位：该实现对应 compositional symbolic execution 中的 verified library
  summary，但刻意采用 lowering-to-core IR 而非 opaque model。当前等级 I/T；
  下一阶段应扩展 bounded NUL-aware string summaries 与 memcpy/memset region
  effects，并继续让 validator 从展开后的 core operations 独立复检。

## 77. F82：Bounded External Region-Write Effect Summaries（2026-07-28）

- 研究问题：真实 parser 和结构化输入处理常通过 `memcpy`、`memmove`、`memset`
  更新短 buffer。将调用整体拒绝会切断大量后续路径；直接调用宿主 libc 则无法把
  symbolic bytes、logical object bounds 和初始化状态写回 continuation checkpoint。
  F82 将这三类确定性、有限 region effect 编译成可迁移的核心 memory IR；
- 双 frontend：compiler 同时识别标准 C declaration call 与 LLVM
  `llvm.memcpy`/`llvm.memmove`/`llvm.memset` intrinsic。长度必须是 compile-time
  constant 且不超过 64 bytes，volatile intrinsic fail closed。标准 libc 形式返回
  destination pointer identity；void intrinsic 不伪造返回值。生成 artifact 继续以
  `bounded-external-effect-summary` 标明 external virtualization；
- `memmove` 快照语义：所有 source byte loads 在任何 destination store 之前生成，
  因此 source/destination 完全相同、前向重叠和后向重叠都读取调用发生时的源区域，
  不会被先前写入污染。这一顺序直接编码在 continuation instruction list 中，无需
  executor 专用 opcode；
- `memcpy` 定义域证明：compiler 对 destination/source 的所有有限 pointer
  alternatives 和 dynamic-index aliases 枚举地址区间，使用扩宽整数比较证明每一对
  `[base,base+n)` 均不相交；只要一个可行候选无法证明 disjoint 就整体拒绝。该策略
  保守牺牲部分带 guard 关联的合法程序，但不会把 C 中重叠 `memcpy` 的未定义行为
  误建模成 `memmove`；
- `memset` symbolic fill：第二参数作为目标位宽 BV 求值，再按 C 语义取低 8 bit，
  同一 symbolic byte 写入全部目标位置。普通 byte stores 会更新 page-COW memory
  和逐字节 initialization marker，因此随 checkpoint、MPI lease 和恢复保持一致；
- Object semantics：每个展开的 byte load/store 都继续使用 finite alias cases，
  复检 input logical length、heap live/logical size、stack owner/read-only、
  caller-domain certificate、pointer-union guards 和目标初始化。写入 read-only、
  null、越界或 inactive object 不会由 concrete witness 补全，而由原 memory-domain
  约束 fail closed；
- Zero/bound contract：`n==0` 不读取 source、不写 destination；libc 返回值仍是原
  destination numeric pointer，因此 `memset(NULL,x,0)` 可在不解引用 null 的前提
  下正确比较。Symbolic length、`n>64`、错误 signature、volatile intrinsic 或
  无法证明 disjoint 的 `memcpy` 输出结构化 rejection report；
- 自动化证据：LLVM tests 覆盖重叠 `memmove` 得到 `AABC`、input-buffer `memset`、
  symbolic fill byte、跨全局对象 `memcpy`、zero/null 和 overlap rejection；
  真实 C tests 额外经过 Clang 的 LLVM memory intrinsic 路径。阶段 compiler
  `-Werror` build 与专门 lowering/checker tests 通过；全量回归结果记录在阶段报告；
- 正确性边界：当前不接受 symbolic/over-64 length，不优化 guard-correlated
  conditional disjointness，也未建模 fortified variants、alignment promise、
  sanitizer interceptor、DMA/I/O memory、atomic/volatile region effect 或外部
  resource。NUL-aware string termination 仍需 guarded access semantics；
- 研究定位：F81/F82 形成 read-only 与 bounded write-effect 两个 verified library
  summary 基线。其关键属性不是 libc API 数量，而是 external call 被降到同一
  proof-carrying object-memory/CAS state，避免 opaque host execution。当前等级
  I/T；下一依赖项是 pointer-returning indirect dispatch，然后引入 guarded load
  以实现 bounded `strlen`/`strcmp`/`strncmp`。

## 78. F83：Pointer-Returning Bounded Indirect Dispatch（2026-07-28）

- 研究问题：F80 已能对有限 internal function-pointer target fork，但为避免丢失
  object identity，主动拒绝 pointer return。这使 factory/selector/visitor 风格的
  C callback 即使只在两个纯内部 helper 间选择，也无法继续解引用返回对象；
- Target-wise return summary：compiler 对每个 finite indirect target 独立遍历所有
  pointer return，递归复用 direct-call 的 bounded object provenance expansion，
  再对全 target 集合去重。每个对象仍由 call-result numeric equality 和
  `result__domain==1` 约束；callee return-side certificate 只在实际进入的 target
  frame 生成，因此未选 target 不能凭静态 union 伪造合法 provenance；
- Indirect pointer argument：callee formal 的 interprocedural summary 现在同时扫描
  direct call 与 finite indirect call sites。若 owner function 是 target set 成员，
  对应 actual pointer alternatives 会进入 callee object summary；hidden
  `__ptr_domain_<index>` ABI、signed GEP bounds 和 pointer-union guards继续按调用点
  传输；
- Runtime/validator reuse：没有新增 executor opcode。既有 `indirect_call` target
  feasibility fork 进入普通 `ContinuationFrame`；普通 return path绑定
  `dst`/`pointer_domain_dst`。Validator 已要求所有 target 的完整 typed signature
  tuple相同，其中包含 pointer parameter/domain mapping、pointer return width 与
  `pointer_return_domain`，因此缺失 destination、异构 target 或错误 certificate
  在创建 checkpoint 前 fail closed；
- 自动化证据：LLVM IR 与真实 C 均在两个 pointer-returning function targets 间
  symbolic dispatch，一个 target 返回固定 global，另一个返回 pointer actual，
  随后 caller load 得到 65/66。手写 artifact test观察 target fork，并篡改
  return-domain destination 与 target domain contract 验证拒绝；
- 正确性边界：target provenance 仍只来自 local constant/cast/select/acyclic PHI，
  最多 64 targets；data pointer summary 最多 256 finite alternatives。仍不从
  function-pointer argument/global/heap load做 general points-to，不支持 varargs、
  recursive SCC、external callback、callee-stack escape 或 pointer-valued memory；
- 研究定位：F83 完成了 F79 proof-carrying data pointer 与 F80 proof-carrying
  control pointer 的组合闭环。它是 context-sensitive bounded target/product
  summary，不是完整 Andersen/SeaDsa。当前等级 I/T；下一项以 guarded access
  表达 NUL-aware external string semantics。

## 79. F84：Guarded Access 与 Bounded NUL-Aware String Summaries（2026-07-28）

- 研究问题：把 `strlen`/`strcmp` 简单展开成固定次数普通 load 会错误要求 NUL
  之后的字节仍可读，尤其会把短 object、logical input 尾部和 heap logical-size
  之外的 backing memory 纳入定义域。F84 先引入通用 conditional memory semantics，
  再在其上构造 string library summary；
- Guarded load core：普通 `load` 可选携带 1-bit `guard`。Executor 对 access domain
  `D` 只提交 `NOT guard OR D`，结果为 `ITE(guard, loaded_value, 0)`；guard concrete
  false 时甚至不求值 address。`D` 仍包含 physical/logical bounds、finite alias
  address equality、pointer-union/caller-domain guards、heap live/size、stack owner
  和逐字节 initialization。Validator 限制 guard 只能出现在 load，runtime 独立复检
  expression width；
- `strlen`：从 active=1 开始逐字节 guarded load，first NUL 用 nested select记录
  offset，active 更新为 `active AND byte!=0`。对象候选最大 extent 与 64-byte 全局
  bound决定展开次数；末尾 `assume NOT active`，所以没有在定义域内终止的模型被
  剪枝，而不是读取 object 外的宿主零字节；
- `strcmp`：两侧只在此前 bytes 全部相等且非 NUL 时读取。首个差异产生 canonical
  `-1/+1`，同时停止后续 access；共同 NUL 返回 0。不同大小 pointer-union 的短 arm
  在后续 offset没有 alias case，但其 active 已为 false，因此仍满足 guarded
  domain；
- `strncmp`：长度必须为 compile-time constant `0..64`。`n==0` 不解析 pointer
  并返回 0；达到 n 时无需 NUL。若某个 candidate object 在 n 前结束，则 active
  必须已经因 NUL/差异变 false，否则定义域不可满足；
- Artifact/capability：summary 不增加 opaque string opcode，生成
  `load/binary/unary/select/assume`，并标记 `guarded-memory-access`、
  `bounded-nul-string-summary` 和 `bounded-external-effect-summary`。因此 CAS
  expression sharing、incremental QF_BV context、MPI checkpoint 和 branch fork
  全部沿用；
- 自动化证据：LLVM/C tests 覆盖 empty/`AB` pointer union 的 strlen、`AB`/`AC`
  strcmp、无 NUL 前两字节相等的 strncmp、n=0/null 和 symbolic n rejection。
  Input-buffer test证明 `strlen==logical_size` 不可行，不能借用 capacity backing
  zero；手写 artifact test证明 guard=false 可跳过物理 OOB，guard=true infeasible，
  8-bit malformed guard在 runtime 拒绝；
- 正确性边界：搜索与 `strncmp` 上界为 64，不支持 symbolic n、wide/locale/
  case-insensitive/multibyte string、`strcoll`、external locale state 或 fortified
  variants。超过 64 才出现 NUL/差异的路径被有界 `assume` 排除，属于显式
  under-approximation，不应表述为完整 libc semantics；
- 研究定位：该实现把 compositional library summary 的短路访问条件提升为
  proof-carrying memory domain，而不是用 host wrapper concretization。当前等级
  I/T；后续可在同一 guard primitive 上增加 `memchr`/`strchr` 与 bounded
  string-copy effect，并将动态 length交给可信 loop/array-theory lowering。

## 80. F85：Bounded Pointer-Returning Search Summaries（2026-07-28）

- `memchr(ptr,c,n)` 要求 constant `n<=64`，普通 byte loads 保持完整 n-byte region
  定义域，reverse nested select选择 first match；`n==0` 不解析 pointer并返回 null；
- `strchr(ptr,c)` 复用 F84 guarded load，active 在命中或 NUL 后关闭，最多搜索有限
  object extent/64 bytes，末尾要求已经命中或终止；needle 按 C 语义取低 8 bit；
- 返回指针不是无 provenance 的整数。Compiler 为 null 和每个有限 source
  object/offset candidate生成 `result==address` guard；dynamic base先按有限 alias
  contract 展开。因此返回值可继续 load、作为 pointer actual跨 frame传输或与 null
  比较，仍受 256 项 union 上界；
- 两种 summary 均只生成 core load/BV/select/assume，并标记
  `bounded-pointer-search-summary`。LLVM/C tests覆盖 first match、symbolic needle、
  pointer load、strchr short union、zero/null 和长度超过所有 object extent拒绝；
- 当前不支持 symbolic/over-64 `memchr` length、wide-character/locale search、
  general array-theory pointer result或超过 finite alias bound 的对象。等级 I/T；
  下一阶段用 conditional store 实现 bounded string-copy write effects。

## 81. F86：Guarded Memory Write 与 Bounded String-Copy Summaries（2026-07-28）

- 研究问题：`strcpy` 的写集合由 first NUL 决定，不能用固定次数无条件 store
  近似；否则既会覆盖 terminator 后的目标字节，也会要求短 source/destination 的
  不活跃 suffix 有效。`strncpy` 还要求在 source NUL 后持续写零。F86 将 F84 的
  conditional definedness 对偶扩展到 write effect；
- Guarded store core：`store` 可选 1-bit `guard`。Executor 只要求
  `NOT guard OR access_defined`，并将每个目标 byte 更新为
  `ITE(guard,new,old)`；guard 为 concrete false 时不求值 address/value，也不改变
  memory root。Symbolic address仍枚举 finite alias cases，每个候选写条件是
  `guard AND alias_domain`。Stack/heap 初始化 marker 同样以该条件更新，checkpoint
  恢复后不会把未执行写误报为已初始化；
- Snapshot 与 short circuit：compiler 在发出任何 destination store 之前，先以
  guarded loads 快照全部可能 source bytes。Active 初值为真，每读取非 NUL 字节后
  保持，读到 NUL 后关闭。`strcpy` 对第 `i` 个 byte 使用读取前的 active 作为写
  guard，因此精确写入 first NUL 而不触碰其后 suffix；在 bounded extent 内未终止
  的路径由 `assume !active` 剪枝；
- `strncpy`：长度必须是 compile-time `0..64`。它无条件写恰好 n bytes，source
  NUL 后的 guarded load结果为零，从而实现 padding；source object 在 n 前结束且
  尚未终止时不可行。`n==0` 不解析 source/destination，仍返回原 destination；
- Object/provenance contract：source 与 destination 的所有 finite pointer
  alternatives 必须能证明属于不同对象，避免把未定义 overlap 悄悄建模成
  `memmove`。普通 object/logical-size/liveness/read-only/caller-domain checks继续
  作用于每个 byte。返回值复用 destination 的有限 provenance，因此可继续
  dereference、跨 direct/indirect frame传递或与 null 比较；
- Artifact：摘要只生成 `load/store/binary/unary/assume` 等 core IR，并标记
  `guarded-memory-write`、`bounded-string-copy-summary`、
  `bounded-nul-string-summary` 与 `bounded-external-effect-summary`。Validator
  只允许 load/store 携带 guard，executor再次复检 condition width；
- 自动化证据：LLVM tests覆盖 `AB\0` 复制且保留 destination suffix、返回 pointer
  identity、empty/AB source union 的 66/88 条件写、`strncpy` NUL padding、
  n=0/null、same-object overlap rejection 与 symbolic n rejection；真实 C
  frontend以禁用 builtin folding的声明调用对拍。手写 artifact test覆盖 guard
  false 跳过物理 OOB、guard true infeasible 和 malformed 8-bit guard；
- 正确性边界：不支持 symbolic/over-64 n、可能重叠对象、fortified/wide/locale
  variants、unbounded strings、volatile/atomic destination、external resource 或
  general points-to。对象上限内无 NUL 的 `strcpy` 路径是显式 under-approximation。
  当前等级 I/T；下一优先级是补全 poison/undef/UB-sensitive LLVM integer semantics
  与 pointer-valued memory/general points-to。

## 82. F87：LLVM Integer Defined-Value Guards（2026-07-28）

- 研究问题：直接拒绝除法、符号 shift 和 optimizer 生成的 `nuw/nsw/exact` 会使
  大量普通 LLVM IR 无法导出；忽略这些条件则会把 UB/poison execution 当作正常
  路径。F87 采用 defined-value continuation 语义，将定义域证明显式降成 core
  `assume`，并沿 CAS solver root迁移；
- Division/remainder：`udiv/urem/sdiv/srem` 在求值前断言 divisor 非零；`sdiv`
  额外排除 `INT_MIN/-1`。Guard排在 binary instruction 之前，因此 concrete
  executor不会先触发宿主除零，symbolic divisor则由同一 incremental QF_BV context
  剪枝；
- Shift：`shl/lshr/ashr` 的 amount不再要求常量，而是断言 unsigned
  `amount < bitwidth`。这同时覆盖 symbolic 和 constant out-of-range poison，
  没有按当前 input witness截断 amount；
- Overflow flags：`add/sub` 通过 unsigned order或 sign transition验证，
  `mul nuw` 使用 safe-divisor `UINT_MAX/rhs` 上界，`mul nsw` 使用 signed
  reversible quotient并显式排除 inverse division overflow；`shl nuw/nsw` 分别用
  logical/arithmetic reverse shift证明未丢失有效位。两个 flag同时出现时两个
  condition都必须成立；
- Exact：exact `udiv/sdiv` 断言相应 remainder为零；exact `lshr/ashr` 将结果左移
  恢复原 operand并断言相等。Opcode先分类后读取 LLVM optional-data bits，避免
  `nuw` 与 `exact` 共用 bit位时被跨 subclass误判；
- Freeze 边界：对已定义 bounded integer，freeze仍是 identity。直接
  `freeze undef/poison` 需要一个可迁移、每次动态实例稳定的 nondeterministic
  choice primitive；当前没有该 primitive，因此结构化拒绝而非错误固定成零。
  对 flagged operation产生 poison后再 freeze 的执行，本阶段通过 defined-value
  assume排除，属于明确的 sound under-approximation；
- Runtime contract：artifact标记 `llvm-defined-value-guards`。Validator新增
  binary/unary operator白名单、1..64 bit结果和 operand形状检查；executor要求
  assume condition为 1 bit。SMT lowering与 concrete evaluator已有完整 BV div/rem/
  shift语义，因此不增加 opaque opcode；
- 自动化证据：LLVM tests覆盖 symbolic udiv/shift的多路径、有效 flag组合、constant
  divide-by-zero、signed division overflow、nuw/nsw overflow、non-exact division/
  shift的 infeasible结果，以及 freeze poison rejection；真实 C `/`、`%` frontend
  与手写 artifact operator/assume-width篡改也有定向测试。阶段 compiler
  `-Werror` build、LLVM/CMake lit 93/93 和定向 executor tests通过；
- 正确性边界：这不是完整 LLVM poison lattice。尚未支持 freeze undef/poison 的
  nondeterministic choice、select对 poison的精确非严格传播、vector/FP poison、
  inrange/GEP新 flags、poison-valued memory或 deferred poison consumption。
  当前等级 I/T；下一项建立 pointer-valued memory的有限 points-to sidecar。

## 83. F88：Bounded Pointer-Valued Memory Sidecar（2026-07-28）

- 研究问题：F74--F83 能在 SSA、参数和返回值中保留 pointer provenance，但
  `store ptr`/`load ptr` 会跨越 memory SSA边界。只把地址作为 64-bit整数写回会让
  后续 load无法证明其对象、lifetime和 bounds；直接把所有同类型对象合并又会制造
  不可行 alias；
- 双轨表示：pointer value按 DataLayout pointer width写入普通 page-COW memory，
  checkpoint仍保存真实 numeric bytes；compiler同时为受支持 load构造 finite
  provenance sidecar。每个 candidate继承 stored pointer的 select/PHI/object guards，
  再加入 `loaded_numeric == candidate_address`。后续 GEP/load/store/call沿用已有
  `alias_cases` 和 caller-domain certificate，无 executor专用 pointer opcode；
- Global cell：scalar address-space-0 global pointer initializer可以直接指向另一个
  bounded data global或 null。Layout先为每个对象分配稳定非重叠地址，再按 target
  endianness把地址序列化进初始 memory；load从 initializer恢复唯一 provenance。
  Constant-expression GEP表和 function-pointer initializer仍拒绝；
- Dynamic cell：同一函数内的 pointer slot必须由 exact same stripped storage SSA
  标识，并存在唯一、非 atomic/volatile、支配 load的 pointer store。若另一写在
  load后则忽略；若有 path-conditional writer、不可比较 reaching definitions或
  non-pointer覆盖则拒绝。该规则支持 stack array cell与 heap pool cell，也支持
  stored select/PHI union，但不猜测 MemorySSA phi；
- Initialization/lifetime：pointer load/store使用普通 pointer-width memory
  instruction，所以 stack/heap dominating initialization、heap live/logical size、
  read-only、pool slot guard、COW bytes和checkpoint marker全部复用。Heap cell中
  取出的 data pointer可继续 GEP，cell释放不改变所指对象的独立身份；
- Artifact：新增 `bounded-pointer-memory` capability。现有 validator仍复检
  load/store width、object/alias cases和初始化，pointer result的 guard在后续访问时
  成为 proof-carrying alias condition；
- 自动化证据：LLVM tests覆盖 scalar global pointer initializer、stack cell中
  select后store/load得到65/66、heap cell load后GEP得到66、stored null equality；
  真实 C global pointer也执行得到77。负例覆盖分支两侧 stores在merge load、
  pointer slot被i64覆盖和function pointer store，全部结构化拒绝；
- 正确性边界：这不是 general Andersen/Steensgaard/SeaDsa，也不是完整 MemorySSA。
  尚不支持 pointer arrays/tables、constant GEP initializer、conditional reaching
  store merge、跨函数/escaped cell、部分/unaligned覆盖、memcpy携带pointer
  provenance、function-pointer memory、recursive heap graph或moving GC。当前等级
  I/T；下一阶段先扩展 function-pointer table与 bounded reaching-definition merge，
  再考虑 field-sensitive heap graph summary。

## 84. F89：Scalar Function-Pointer Memory（2026-07-28）

- 研究问题：F80/F83 的 function points-to 只在 SSA select/PHI 中存在；真实前端常把
  callback 写入 global、stack 或 heap cell。若按普通 data pointer 解释，函数 ID 会
  被误当成对象地址；若在 load 后丢失 target set，则间接调用无法复检 typed ABI；
- 表示：函数地址仍使用 F80 的稳定 module-local function ID，按 target endianness
  序列化到 pointer-width memory。Compiler另外恢复 finite
  `FunctionAlternative` sidecar，并加入 `loaded_id == candidate_id` 与原 stored
  select/PHI guards；executor继续使用既有 `indirect_call.targets`，不增加不可复检
  的 host callback primitive；
- 支持边界：scalar global initializer可为 internal function/null；dynamic cell要求
  exact storage identity与 F88 的唯一支配 pointer store。Store lowering区分 data
  pointer address和 function ID，load后的每个 target仍必须通过 exact argument/
  return width和 pointer-domain contract；
- Artifact：使用该能力时声明 `bounded-function-pointer-memory`。Validator对
  `targets`、稳定 ID、guards和 typed signature执行原有强校验，单一可行 target也
  保留间接调用语义；
- 自动化证据：LLVM覆盖 global单目标 cell和 stack select双目标 cell，真实 C global
  callback覆盖 frontend lowering；结果分别验证单目标执行和两个 symbolic target
  child，compiler `-Werror` build与定向 lit通过；
- 正确性边界：不支持 external/variadic function、跨模块可重定位 function address、
  pointer authentication、非完整宽度覆盖、escaped cell或未经有限 provenance证明
  的 integer-to-function-pointer。当前等级 I/T。

## 85. F90：Bounded Global Pointer Tables（2026-07-28）

- 研究问题：parser dispatch和对象选择常用只读 pointer table。F88/F89 的 scalar
  initializer不能表达 `table[symbolic_index]`，而仅保留加载的数字又会丢失元素级
  provenance；
- 初始化与恢复：支持 address-space-0、固定长度一维 global pointer array；每个
  element必须为 null、bounded data global/internal function，或以 constant GEP指向
  bounded data global。静态 memory image沿用 DataLayout endian序列化，侧边恢复
  汇总全表候选并去重；
- Symbolic index：现有 bounded GEP/alias enumeration证明 table element address
  属于有限域；pointer load再以 `loaded_numeric == candidate_address/function_id`
  过滤候选。因此 table index条件和元素身份都进入同一 QF_BV solver prefix，而不是
  按 concrete witness选择一项；
- Artifact：使用时声明 `bounded-pointer-table`，并与
  `bounded-pointer-memory`/`bounded-function-pointer-memory`组合。没有新增运行时
  opcode，table bytes、COW pages、alias guards和间接调用 target contract均由既有
  validator/executor复检；
- 自动化证据：LLVM data-pointer table对 symbolic index产生值1/2，function-pointer
  table产生目标返回6/7；真实 C data table得到相同两个结果。定向 lowering/executor
  tests与完整 compiler build通过；
- 正确性边界：仅支持固定一维全局常量形状；nested aggregate、runtime table mutation、
  relocation/ifunc、跨 DSO target、table element部分覆盖与无限 points-to仍拒绝。
  当前等级 I/T。

## 86. F91：Direct-Predecessor Pointer-Memory Merge（2026-07-28）

- 研究问题：常见 `if/else` 会在两个分支分别写 pointer cell，再于 join load。
  F88 的“唯一支配 store”会拒绝这种合法 memory-phi；直接汇总函数内所有 stores又
  会把不可达旧值引入 points-to；
- 有界 reaching proof：先寻找 exact stripped storage上的唯一最新支配 pointer
  store。若不存在，只接受 load所在 block的每个直接 CFG predecessor恰有一个该
  slot的 pointer store，且函数中不存在其他同 slot store或非 pointer覆盖。所有
  writer必须非 atomic/non-volatile，且位于对应 predecessor并支配其 terminator；
- Merge semantics：data/function pointer候选分别对所有 predecessor stores递归恢复，
  去重后增加 loaded numeric equality。路径可达性仍由 predecessor branch与 PHI
  edge执行决定，numeric equality阻止来自另一 writer的伪 candidate；function
  reachability discovery也纳入同一有限 target union；
- Artifact：使用条件合流时声明 `bounded-pointer-memory-merge`。这是一项
  compiler proof capability；core memory、solver和indirect-call schema保持不变；
- 自动化证据：LLVM data-pointer conditional cell产生1/2，function-pointer版本
  产生6/7；缺少一个 predecessor writer的 uninitialized merge结构化拒绝。定向
  LLVM/CMake lit与 compiler `-Werror` build通过；
- 正确性边界：这不是 MemorySSA。循环回边、超过一层 predecessor merge、dominant
  default store加条件 overwrite、switch predecessor复用、跨函数/escaped cell、
  memcpy传播 provenance、atomic/volatile和部分宽度覆盖仍 fail closed。当前等级
  I/T；后续 general heap graph必须引入显式 memory-version或可验证 points-to
  artifact，而不能继续靠全函数 store扫描外推。

## 87. F92：Proven-`nounwind` Invoke Normal-Edge Lowering（2026-07-28）

- 研究问题：C++ frontend和优化后 IR即使调用不会抛出，也可能保留 `invoke` 与
  landingpad。把它整体拒绝会丢失普通路径；无条件忽略 unwind edge则会错误执行真正
  可能抛出的函数；
- 证明条件：call-site带 LLVM `nounwind` attribute，或 direct internal callee带
  `nounwind`；有限间接目标则要求每个 recovered internal target都带
  `nounwind`。目标不完整、为空、external/intrinsic或任一目标可能抛出时 fail
  closed；
- Control transfer：call/indirect-call artifact新增可选 `normal_target`，指向
  `context.edgeTarget(invoke_parent, normal_dest)`。Callee return先绑定普通 result与
  pointer-domain，再跳到该 block的instruction 0；因此 normal destination含 PHI时
  仍经过既有 two-phase edge copy，不会跳过 parallel assignment；
- Semantic reachability：compiler从函数 entry计算 continuation CFG。对已证明
  `nounwind` 的 invoke只遍历 normal successor，未到达的 landingpad/unwind subtree
  不被 lowering、call-graph discovery或recursion proof消费；普通 terminator和未
  证明 invoke仍保留全部 LLVM successors；
- Artifact：使用时声明 `bounded-nounwind-invoke`。Validator要求
  `normal_target`必须是同一 caller function内的现有 block；runtime不信任 raw
  program中的任意 PC。该字段对direct与bounded indirect call使用相同契约；
- 自动化证据：LLVM direct invoke通过normal-edge PHI后返回7；select形成的两个
  nounwind targets分叉返回6/7；带潜在 external throw的internal target结构化拒绝，
  两个 unwind landingpad均不会进入 executable artifact。手写 artifact测试证明
  return跳过call后的线性instruction并到达显式normal block，missing target在create
  阶段拒绝。定向 lit、executor unit、Python syntax、whitespace及compiler
  `-Werror` build通过；
- 正确性边界：这不是 Itanium/Windows EH lowering，不建模 exception object、
  cleanup/destructor、catch/filter/resume、personality、`landingpad`/`catchswitch`
  或跨 frame unwinding。只有证明 unwind edge不可达的 invoke进入 executable
  subset；其余仍拒绝。当前等级 I/T。

## 88. F93：Stable Dynamic Nondeterministic Freeze Choice（2026-07-28）

- 研究问题：LLVM `freeze undef/poison`必须选择任意但对该动态 SSA实例稳定的值。固定
  为零会丢路径；按静态指令只建一个 SMT变量会把循环多次执行错误绑定为同一值；
  不持久化实例编号则 checkpoint恢复后会改变约束身份；
- Core IR：新增 `nondet {dst,bits,site}` instruction。Compiler仅对1..64-bit整数的
  direct `freeze undef`/`freeze poison`生成该指令，并声明
  `stable-nondeterministic-freeze`；已定义 operand的 freeze仍降为 identity；
- Dynamic identity：executor在 symbolic store中维护64-bit
  `@nondet:counter`。执行时表达式token为 `<stable-site>:<counter>`，随后原子递增
  counter。SSA destination保存同一 CAS expression digest，因而所有uses一致；循环
  re-entry获得新token，pause/resume从checkpoint中的counter继续；
- Solver semantics：expression `nondet`始终被视为symbolic，concrete witness取任意
  合法零值以驱动concolic evaluator。QF_BV lowering为每个token声明独立、精确位宽
  的BV常量；input仍保留数字变量名，避免影响现有model-to-byte extraction。
  One-shot和CAS-rooted incremental fragment使用同一带宽声明集合；
- Artifact contract：validator要求site字符集限定为字母数字及`._:-`、长度有界、
  module artifact内唯一，且bits为1..64。Runtime生成的token再次校验，重复site或
  非法token不会静默造成变量碰撞；
- 自动化证据：LLVM `freeze poison`与`freeze undef`后比较常量均由SMT产生true/false
  两个结果；executor循环在同一静态site执行两次，跨中间checkpoint得到
  `site:0`/`site:1`并保留counter=2；重复site artifact拒绝。定向 lit/unit、
  compiler `-Werror` build、Python syntax和whitespace检查通过；
- 正确性边界：F93解决direct undef/poison freeze的动态choice，不是完整LLVM poison
  lattice。F87当前仍在产生poison的算术操作前加入defined-value assume，因此
  `freeze(add nsw overflow)`仍被保守剪枝；select非严格传播、poison memory、
  vector/FP freeze、deferred poison consumption与跨线程choice ordering尚未实现。
  当前等级 I/T。

## 89. F94：Bounded Acyclic Pointer-Memory SSA Closure（2026-07-28）

- 研究问题：F91要求join的每个直接前驱都有writer，不能表达常见的“entry默认值、
  某个嵌套分支覆盖、其他路径沿用默认值”。全函数收集stores则会把旁路旧写和已经
  覆盖的写错误并入provenance；
- Backward dataflow：从pointer load所在block、load之前的位置逆向扫描exact stripped
  storage identity，只取每个block最后一个同slot writer；若没有writer则递归所有CFG
  predecessors。每条entry路径必须到达一个定义，结果按StoreInst identity去重；
- Last-write semantics：只有实际reaching的最后写需要是full-width pointer且
  non-atomic/non-volatile；被后续完整pointer store覆盖的早期writer不影响结果，
  load之后或无法逆向到达的writer不会制造歧义。Data/function pointer provenance
  使用同一closure，后者也用于call-graph target discovery；
- Bounds：最多访问4096个backward节点、恢复256个stores。active block集合检测循环
  memory-phi并fail closed；缺失entry definition、non-pointer last writer、
  atomic/volatile last writer分别给出结构化诊断；
- Artifact：跨前驱闭包使用时声明`acyclic-pointer-memory-ssa`；存在多个reaching
  writers时同时声明`bounded-pointer-memory-merge`。Core memory仍以实际loaded
  numeric equality过滤候选，不增加host-side path猜测；
- 自动化证据：LLVM nested two-level branch包含entry default、inner overwrite、
  forwarding和bypass paths，得到1/1/2并保留两项provenance；原direct merge、
  function-pointer merge继续通过。无写入口、non-pointer overwrite和循环回边均
  拒绝。定向 compiler build、artifact execution与whitespace检查通过；
- 正确性边界：storage identity仍要求同一stripped SSA value；不等价合并不同GEP
  expressions。循环fixed point、partial/unaligned writes、memcpy pointer
  provenance、escaped/interprocedural cells和atomic memory model尚未实现。这是
  bounded acyclic MemorySSA-like proof，不是LLVM MemorySSA或general points-to。
  当前等级 I/T。

## 90. F95：Deterministic Scalar External/Intrinsic Summaries（2026-07-28）

- 研究问题：continuation frontend已覆盖memory/string externals，但普通parser常见的
  `abs`与network byte-order调用仍导致整个函数拒绝。直接执行host libc会把symbolic
  argument concretize，并可能把host ABI/endian误用于目标；
- Absolute-value summary：支持严格ABI的`abs(i32)`、`labs(target-long)`和
  `llabs(i64)`，参数/返回必须同宽。Core DAG计算signed-negative、`0-x`与select；
  C标准中不可表示的`abs(INT_MIN)`以显式`x != INT_MIN` assume进入CAS solver prefix，
  而不是依赖宿主溢出；
- Byte-order summary：支持`htons/ntohs(i16)`与`htonl/ntohl(i32)`。Big-endian
  DataLayout降为identity，little-endian按byte mask、logical shift和OR构建完整BV
  permutation。相同helper支持LLVM `bswap` intrinsic的16/32/64-bit形态，保证C
  frontend builtin folding前后语义一致；
- Artifact：external scalar使用时声明`bounded-external-effect-summary`与
  `bounded-scalar-external-summary`；纯LLVM bswap声明后者。所有实现只使用已验证
  binary/unary/select/assume core ops，不增加host-call opcode或隐藏状态；
- 自动化证据：LLVM symbolic abs产生match/non-match 1/2，constant ntohl与
  `llvm.bswap.i32(0x01020304)`均得到`0x04030201`；真实C在禁用builtin folding后
  abs与ntohl走external declaration并得到相同结果。x86-64上错误`labs(i32)`精确
  拒绝。定向artifact、executor、compiler `-Werror`与whitespace检查通过；
- 正确性边界：不建模locale-dependent ctype、errno、FP/libm、随机数、时间、
  filesystem/network state或用户自定义同名但非标准语义；也不接受variadic、
  pointer参数或错误ABI。当前等级 I/T。

## 91. F96：Bounded Bit-Count Intrinsic Summaries（2026-07-28）

- 研究问题：LLVM frontend和优化器会把parser中的bitset、bitmap索引与整数归一化
  变换为`llvm.ctpop/ctlz/cttz`。若将这些pure intrinsics当external call拒绝，
  continuation覆盖会随优化级别变化；若调用host builtin，则无法把符号输入保留到
  solver；
- Core lowering：对1..64-bit scalar integer，以`lshr/and/add`展开population
  count；leading/trailing zero从MSB/LSB依次建立zero-prefix predicate并求和。
  结果保持原LLVM位宽，零输入且`is_zero_poison=false`时严格返回位宽；
- Definedness：`ctlz/cttz`的第二个`i1 immarg`必须是常量。为true时生成
  `input != 0` assume并进入既有CAS-rooted QF_BV prefix，因此零输入状态被明确标记
  infeasible；为false时不引入额外定义域。错误arity、非标量或超过64-bit均
  fail closed；
- Artifact：使用时声明`bounded-bitcount-intrinsic`。没有新增executor opcode，
  validator/runtime继续复检已有binary/unary/assume primitive；展开规模为O(w)，
  当前w上限64，避免不受控artifact增长；
- 自动化证据：LLVM symbolic `ctpop.i8`与`cttz.i8`产生match/non-match双分支，
  `ctlz.i8(0,false)`返回8，`ctlz.i8(0,true)`不可行；真实C
  `__builtin_popcount`经`-O1` frontend同样进入capability。定向artifact执行、
  compiler `-Werror` build、Python syntax和whitespace检查通过；
- 正确性边界：仅支持scalar integer overload，不支持vector、scalable vector、
  arbitrary precision、VP intrinsics或target-specific bit operations。当前线性
  DAG优先保证可移植和可审计；超宽值应由未来native/SMT primitive cost model选择
  专用表示。当前等级 I/T。

## 92. F97：Direct Deferred-Poison Freeze（2026-07-28）

- 研究问题：F87在产生`nuw/nsw` poison的算术点立即加入definedness assume，
  对普通consumer是安全的under-approximation，但错误处理了LLVM
  `freeze(poison)`：freeze本应把poison变为任意且对该动态实例稳定的值；
- 有界数据流：仅对1..64-bit `add/sub/mul`、至少一个`nuw/nsw` flag、唯一use且该
  use为direct `freeze`的形态延迟definedness。Compiler仍生成wrapped BV result，
  将所有unsigned/signed no-wrap条件合取后保存在function-local side table，不在
  产生点assume；
- Freeze semantics：freeze生成F93的stable dynamic nondet choice，并绑定
  `ite(defined, wrapped_result, choice)`。定义路径严格保留原值；溢出路径不受
  wrapped witness约束；site×persistent counter继续保证循环实例与checkpoint恢复
  identity；
- Artifact：使用时同时声明`bounded-deferred-poison-freeze`与
  `stable-nondeterministic-freeze`。Core只增加已有nondet/select表达式，执行器和
  solver不信任compiler特有side table；
- 自动化证据：constant `add nsw i8 127,1`后direct freeze产生两个可解分支，
  `add nuw nsw i8 1,1`后freeze严格返回2；同一poison-producing result存在额外
  consumer时不进入延迟子集，并按F87得到infeasible。Compiler `-Werror` build、
  artifact execution、Python syntax通过；
- 正确性边界：这不是完整deferred poison lattice。Shift/div/exact、cast/compare/
  select/PHI传播、memory/call/return边界、多use但所有use安全的证明、vector/FP均
  仍采用defined-only策略。该限制防止在没有per-SSA poison bit和consumer规则时
  做不可靠的广义传播。当前等级 I/T。

## 93. F98：Bounded Cyclic Pointer-Memory SSA Fixed Point（2026-07-28）

- 研究问题：F94的backward DFS遇到writer-free回边即拒绝，即使cell已在entry完整
  初始化；这会排除parser loop中常见的loop-invariant callback/data pointer，也
  无法表达上一迭代的完整pointer store；
- Dataflow：对入口可达CFG建立每个block的`IN/OUT = {last stores, may-uninitialized}`
  lattice。Block内最后一个exact-slot store执行kill；否则OUT继承IN。所有live
  predecessors做union，entry边引入uninitialized，迭代至fixed point；
- Load point：若block内load之前有writer仍走fast path；否则读取IN集合。任何路径
  保留uninitialized即拒绝；最多4096次block transfer与256个reaching stores。
  Atomic/volatile/non-pointer last writer在最终reaching集合中结构化拒绝；
- Cyclic proof：反向收集能到达load的live ancestors并做颜色DFS，区分acyclic与
  cyclic certificate。后者声明`bounded-cyclic-pointer-memory-ssa`；多个loop-
  carried writer仍同时声明merge capability。Candidate按函数指令顺序输出，保持
  artifact确定性；
- Runtime semantics：numeric pointer/function ID仍由真实page-COW load得到，有限
  provenance candidate以loaded equality过滤。因此entry writer与回边writer同时
  出现在静态集合时，当前迭代只可能采用实际内存版本，而不是host侧猜测迭代次数；
- 自动化证据：固定三次循环的loop-invariant cell返回65；入口写left、每次loop尾
  写right的loop-carried case返回66并恢复两个候选；无入口初始化的同构循环拒绝。
  Compiler `-Werror`、artifact executor、Python syntax与whitespace检查通过；
- 正确性边界：仍要求exact stripped storage identity与full-width store；不支持
  partial/unaligned/memcpy pointer writes、escaped/interprocedural cell、atomic
  memory model或不同GEP表达式的alias证明。这是有限may-reaching-definition
  fixed point，不是LLVM MemorySSA+AA的通用替代。当前等级 I/T。

## 94. F99：Canonical Constant-GEP Pointer-Cell Identity（2026-07-28）

- 研究问题：同一数组/结构字段常由不同LLVM GEP instructions重新计算。F98按raw
  stripped SSA pointer匹配时，会把语义相同的非零offset storage误判为没有writer；
- Canonical key：对address-space pointer width不超过64的constant GEP chain，
  使用target DataLayout逐层`accumulateConstantOffset`，以
  `(stripped base, modular pointer-width offset)`作为cell identity。Bitcast与zero
  GEP继续归一化，symbolic/nonconstant GEP退回原SSA identity；
- Integration：last-writer kill、fixed-point store集合、data pointer provenance和
  function target discovery使用同一key。只有load/store raw SSA不同但canonical key
  相等时声明`canonical-pointer-cell-identity`，便于artifact审计实际使用；
- Soundness guard：offset按target pointer width模运算，与LLVM pointer arithmetic
  表示一致；base或offset不同绝不合并。不引入宿主地址或基于名称的alias猜测；
- 自动化证据：两个独立`gep [2 x ptr], ..., 1`之间store/load返回65并声明
  capability；在offset 0写入另一pointer后从重复offset 1读取仍返回65，证明
  interference没有被错误合并。Compiler `-Werror`、artifact execution和whitespace
  检查通过；
- 正确性边界：symbolic GEP、不同base但AA可证明must-alias、ptrtoint/inttoptr、
  address-space cast、partial/overlapping cell与escaped pointer仍不规范化。当前
  等级 I/T。

## 95. F100：Bounded Bit-Permutation/Funnel-Shift Intrinsics（2026-07-28）

- 研究依据：[LLVM Language Reference, Bit Manipulation Intrinsics]
  (https://llvm.org/docs/LangRef.html#bit-manipulation-intrinsics)定义
  `bitreverse`的bit M到N-M-1映射，以及`fshl/fshr`对`{a:b}`拼接值执行
  `shift % bitwidth`；
- Core lowering：对1..64-bit scalar integer，bitreverse逐位
  `lshr/and/shl/or`；funnel shift先以`urem`规范化shift，再计算互补距离并合并两侧
  片段。所有参数/返回必须精确同型；
- Boundary handling：normalized shift为0时，互补距离等于bitwidth。Continuation
  core采用SMT-LIB BV shift语义，shift-by-width片段为0，因此结果精确等于对应未移位
  half，不引入LLVM poison；大shift则先取模；
- Artifact：使用时声明`bounded-bit-permutation-intrinsic`。实现为O(w) reverse
  DAG或O(1) funnel DAG，不增加target-specific opcode或host builtin；
- 自动化证据：symbolic `bitreverse.i8`以concrete 0xb6命中0x6d并求得双分支；
  LangRef风格`fshl.i8(0xff,0,15)`返回0x80，
  `fshr.i8(0,0xff,15)`返回1。Compiler `-Werror`、artifact execution与Python
  syntax通过；
- 正确性边界：不支持vector/VP、`clmul/pext/pdep`、target-specific intrinsics或
  超过64位。当前等级 I/T。

## 96. F101：Bounded Saturating Add/Sub Intrinsics（2026-07-28）

- 研究依据：LLVM LangRef的saturation arithmetic把数学结果限制到目标signed或
  unsigned可表示区间，而不是产生wrap/poison；
- Core lowering：支持1..64-bit scalar `sadd/uadd/ssub/usub.sat`。Unsigned add以
  `wrapped < lhs`检测上溢并clamp全1，unsigned sub以`lhs < rhs`检测下溢并clamp 0；
  signed add/sub以operand sign关系和result sign翻转检测溢出，再按lhs sign选择
  signed min/max；
- Artifact：声明`bounded-saturating-arithmetic-intrinsic`，只使用
  add/sub/compare/select core ops；不借用host integer promotion或UB；
- 自动化证据：一个packed oracle同时验证`250+20 -> 255`、`3-5 -> 0`、
  `120+20 -> 127`、`-120-20 -> -128`；symbolic unsigned add到255的predicate求得
  saturation/non-saturation双分支；
- 正确性边界：本节不含shift/fixed-point/vector saturation；shift由F102扩展。
  当前等级 I/T。

## 97. F102：Bounded Saturating Shift Intrinsics（2026-07-28）

- 研究依据：LLVM `sshl.sat/ushl.sat`在shift amount小于bitwidth时执行clamping，
  amount大于等于bitwidth则产生poison；
- Core lowering：先生成`amount < width` assume，再计算wrapped shl。Unsigned以
  `lshr(result,amount)==input`判断可逆性，否则clamp unsigned max；signed使用ashr
  可逆性并按input sign clamp signed min/max；
- 自动化证据：i8 signed `-100 << 2`clamp到-128，unsigned `100 << 2`clamp到255，
  packed i16返回65408；amount=8的i8 case进入明确infeasible状态并声明LLVM
  defined-value guard；
- 正确性边界：该poison边界仍遵循F87 defined-only策略，尚未进入F97 direct
  deferred-freeze子集；vector/VP/fixed-point shifts不支持。当前等级 I/T。

## 98. F103：Bounded Scalar Abs/Min/Max Intrinsics（2026-07-28）

- 研究依据：LLVM LangRef定义scalar `abs/smax/smin/umax/umin`为任意integer-width
  overload；`llvm.abs`的第二个constant flag单独决定INT_MIN是保留INT_MIN还是
  poison；
- Core lowering：支持1..64位。Abs使用signed-negative、0-x与select；flag为true时
  额外加入`x != INT_MIN` definedness assume。Min/max使用signed/unsigned compare
  直接选择operand，不经过host promotion；
- Artifact：声明`bounded-scalar-selection-intrinsic`，与F95 C `abs`区分：C函数
  固定排除INT_MIN，而LLVM intrinsic的false flag允许返回wrapped INT_MIN；
- 自动化证据：packed i64同时复检abs(-5)、abs(INT_MIN,false)、signed min/max和
  unsigned min/max六个结果；symbolic smax产生双分支；
  abs(INT_MIN,true)明确infeasible；
- 正确性边界：不支持vector/VP和后续LangRef `scmp/ucmp`三向比较。当前等级 I/T。

## 99. F104：LLVM Branch-Hint Identity Intrinsics（2026-07-28）

- 研究问题：`llvm.expect`和`expect.with.probability`只影响optimizer分支权重，运行
  语义严格为第一个参数；未知intrinsic拒绝会使带likely/unlikely hint的frontend
  产物无法执行；
- Lowering：对1..64-bit integer overload验证参数/返回同型；probability variant
  额外要求constant double位于[0,1]。Expected value和probability不进入path
  constraint，结果以identity绑定第一个参数；
- Artifact：使用时声明`llvm-optimization-hint-identity`。该capability表明只移除
  非语义hint，不会把期望值错误地当assumption；
- 自动化证据：symbolic i8依次通过expect(expected=10)和
  expect.with.probability(10,0.9)，随后比较10仍产生true/false两条可行路径；
- 正确性边界：不处理profile metadata的调度利用、`ssa.copy`、deopt/guard、
  sideeffect或pointer hints。当前等级 I/T。

## 100. F105：Bounded Static `llvm.objectsize`（2026-07-28）

- 研究问题：fortify/bounds-check frontend常保留`llvm.objectsize`。已有provenance已
  知对象与offset，继续拒绝会丢失可精确执行的安全长度分支；
- Static semantics：对1..64-bit result、四参数标准ABI及三个constant i1 flags做
  验证。Global、fixed stack和fixed-capacity heap返回
  `object_size - current_offset`；null按`null_is_unknown`和min/max flag返回0或
  unknown sentinel；
- Pointer union：为每个finite alternative复用pointer provenance guards并构建
  size ITE；fallback遵循min unknown=0、max unknown=all-ones。不同对象size不被
  host concretize；
- Artifact：声明`bounded-objectsize-intrinsic`。Guard comparisons与select均为
  普通core IR，runtime无需信任compiler-side object metadata；
- 自动化证据：8-byte stack offset2返回6；4-byte offset1与6-byte offset2的
  symbolic object union返回3/4并驱动双分支；known-null返回0。Pointer-size input
  且dynamic=true在F105中明确拒绝，防止把capacity误报为logical length；
- 正确性边界：F105只提供fixed-size exact provenance。Runtime input、nullable
  heap、realloc size、symbolic offset和unknown external object留给dynamic
  objectsize扩展；vector/nonstandard ABI拒绝。当前等级 I/T。

## 101. F106：Bounded Dynamic `llvm.objectsize`（2026-07-28）

- 研究问题：F105只可回答编译期固定capacity。Pointer-size input ABI和F78
  nullable heap同时具有“静态池容量”和“运行时逻辑长度”；把前者当作
  `llvm.objectsize(...,dynamic=true)`会允许越过实际输入或分配请求；
- Runtime semantics：当dynamic flag为true时，输入对象使用continuation
  `input_size`，`malloc`使用请求size，`calloc`使用count × element-size。当前
  pointer与对象base在目标pointer bitwidth下相减，结果无符号转换到intrinsic
  result width，并以`logical_size >= offset`保护`logical_size - offset`；
- Conservative fallback：runtime-sized对象或symbolic offset在dynamic=false时
  不返回预分配capacity，而严格按minimum flag返回0或all-ones unknown。
  Null alternative仍单独服从`null_is_unknown`，finite pointer union继续以
  provenance guard构造ITE；
- Artifact：除F105 `bounded-objectsize-intrinsic`外，实际使用runtime logical
  size时声明`bounded-dynamic-objectsize-intrinsic`。导出物因此可区分静态
  provenance query与动态长度语义；
- 自动化证据：3-byte pointer-size input的offset1精确返回2；symbolic-entry
  `malloc(3)`返回3而不是4096-byte池容量；同一runtime allocation在
  dynamic=false且maximum模式返回`2^64-1` unknown。Compiler `-Werror` build、
  directed execution与whitespace checks通过；
- 正确性边界：支持input、malloc、calloc和已有bounded symbolic GEP。尚未追踪
  `realloc`成功后的当前逻辑长度，也不猜测unknown external/custom allocator
  object；pointer/result widths限于1..64，vector/nonstandard ABI拒绝。当前等级
  I/T。

## 102. F107：Bounded `*.with.overflow` Aggregates（2026-07-28）

- 研究依据：LLVM的六个signed/unsigned add/sub/mul overflow intrinsics返回
  `{wrapped-value, i1 overflow}`，而非poison；普通SymCC instrumentation已有支持，
  但portable continuation此前不支持aggregate/extractvalue；
- Lowering：为1..64位scalar生成独立wrapped BV与overflow predicate，并只接受
  结构索引0/1的`extractvalue`。Unsigned add/sub使用wrap ordering；signed add/sub
  使用operand/result sign关系；mul以安全非零divisor做可逆性检查，signed
  `INT_MIN * -1`单独置overflow；
- Artifact：声明`bounded-overflow-arithmetic-intrinsic`。Aggregate不进入通用
  struct ABI，而以call-stable value/flag binding桥接，因此不会暗示任意
  `insertvalue/extractvalue`已支持；
- 自动化证据：packed oracle覆盖六个overflow variant的wrapped value与flag，
  另覆盖unsigned乘零、symbolic uadd双路径和signed minimum negation边界；
- 正确性边界：vector、超过64位和非overflow aggregate仍拒绝。当前等级 I/T。

## 103. F108：Bounded Integer/Pointer `llvm.ssa.copy`（2026-07-28）

- 研究依据：LangRef规定`ssa.copy`只建立新SSA名称，runtime value严格等于operand，
  常由PredicateInfo/extended SSA中间形态产生；
- Lowering：1..64位integer降为identity；address-space-0 data pointer保留完整
  finite provenance；function pointer同时扩展target discovery与typed indirect
  dispatch，使copy不会切断reachable target graph；
- Artifact：声明`bounded-ssa-copy-intrinsic`。Identity不产生assumption或branch
  bias；pointer guard仍引用原select/PHI/loaded target certificate；
- 自动化证据：symbolic integer copy保留双分支，data pointer select经copy/load
  保留两对象路径，function pointer select经copy后仍分派increment/increment-two。
  测试隔离在opt-only lit模块：LLVM 18 IPSCCP会在人工持久化的ssa.copy模块上先于
  SymCC pass触发`PredicateBase`崩溃，该上游工具链缺陷不计作lowering失败；
- 正确性边界：floating/vector/token/metadata overload与nonzero address space拒绝。
  当前等级 I/T。

## 104. F109：Dynamic `objectsize` after `realloc`（2026-07-28）

- 正确性修复：F106若仅沿原malloc provenance，直接查询realloc result可能误报旧
  logical length或pool capacity；这比“未支持”更危险；
- Lowering：每个`StaticPointer` alternative携带自己的`reallocationObject`，
  provenance经GEP/cast/select/PHI保持；非null alternative使用对应realloc请求
  size，actual pointer减原对象base得到offset。该设计避免整个objectsize调用
  共享一个语法realloc root；guard不匹配表示realloc failure/null，并服从
  `null_is_unknown`；
- 自动化证据：bounded in-place `realloc(4,2)`成功后返回2；请求5超出对象容量时
  runtime realloc失败，`null_is_unknown=false`返回0；`select(realloc,
  fixed-global)`的symbolic双分支分别返回2与4，并由校验器验证guarded objectsize
  union ITE而非仅检查capability字符串；
- 正确性边界：只覆盖现有F78 bounded in-place realloc，不声称moving realloc、
  custom allocator或任意heap graph支持。当前等级 I/T。

## 105. F110：Transitive Deferred-Poison Freeze（2026-07-28）

- 研究问题：LLVM poison可穿过整数cast、算术和comparison，F97只支持flagged
  arithmetic直接进入freeze，导致本可精确恢复的linear chain被defined-only
  剪枝；
- Analysis：从poison-producing value沿single-use链前向检查，允许bounded integer
  cast、core binary、`icmp`和integer `ssa.copy`，终点必须是唯一freeze。任一
  multi-use、PHI、select、memory、call或control consumer立即回退原definedness
  assumption；
- Lowering：每个透明节点传递“value is defined”条件；多个operand条件取合取。
  Freeze生成stable nondet并选择`ite(defined, wrapped, nondet)`，声明
  `bounded-transitive-deferred-poison`；
- 自动化证据：overflow经sext/trunc、xor/icmp和ssa.copy的三类poison chain均
  恢复双路径；defined add经cast/add chain精确返回3；既有multi-consumer case
  仍infeasible；
- 正确性边界：未覆盖select条件依赖、PHI合流、memory poison、跨函数传播或
  aggregate/vector poison。当前等级 I/T。

## 106. F111：Totalized Deferred Division/Exact Freeze（2026-07-28）

- 执行层问题：continuation concrete evaluator会急切求值select children。即使
  defined=false，未选择的`udiv 7,0` wrapped child仍可能抛除零错误；
- Totalization：复用`divisor != 0` definedness predicate构造
  `safe_divisor = ite(nonzero, divisor, 1)`。主division/remainder和exact remainder
  check均使用safe divisor；原predicate仍进入freeze defined condition，因此只在
  LLVM-defined域暴露wrapped结果；
- 自动化证据：udiv zero、sdiv INT_MIN/-1和non-exact `udiv exact 7,2`均由freeze
  恢复稳定non-deterministic双路径；`udiv exact 8,2`精确返回4且无隐藏除零；
- 正确性边界：该totalization只服务single-use deferred-freeze链，不改变普通
  defined-only division的UB pruning。当前等级 I/T。

## 107. F112：Path-Sensitive Select Poison（2026-07-28）

- 研究问题：LLVM select不会传播未选中value arm的poison，但condition poison总使
  result poison；简单合取三个defined bit会错误丢弃合法路径；
- Semantics：对single-consumer integer select计算
  `cond_defined AND ite(cond, true_defined, false_defined)`，value本身继续使用
  wrapped select。结果definedness沿后续透明链进入freeze；
- Artifact：实际使用时声明`bounded-select-deferred-poison`，并保留F110
  `bounded-transitive-deferred-poison`；
- 自动化证据：constant false选择defined arm时即使另一臂overflow poison仍精确
  返回7；constant true选择poison arm及poison comparison作为condition时均由
  freeze恢复双路径；
- 正确性边界：只支持bounded integer select、single-consumer freeze sink；
  pointer/vector select poison不在本子集。当前等级 I/T。

## 108. F113：PHI Edge-Definedness Merge（2026-07-28）

- 研究问题：PHI definedness取决于实际CFG predecessor，不能在merge block用无
  control certificate的ITE近似；
- Lowering：对唯一流向freeze-chain的integer PHI预分配`phi__defined`。每条已有
  edge block同时stage incoming value与incoming defined bit，再在所有stage后
  two-phase commit，保持parallel PHI语义和循环边复制协议；
- Artifact：当incoming确有deferred condition时声明
  `bounded-phi-deferred-poison`；
- 自动化证据：constant false edge选择defined 7时精确返回7；constant true edge
  选择overflow poison时freeze恢复双路径；
- 正确性边界：cyclic/self-dependent poison PHI、多consumer和跨函数PHI仍回退
  defined-only。当前等级 I/T。

## 109. F114：Straight-Line Memory Poison Sidecar（2026-07-28）

- 研究问题：scalar mem2reg通常消除stack store/load，但mutable global或未提升
  memory仍会承载poison；直接假设store value defined会丢失freeze语义；
- Conservative analysis：只接受同一basic block、相同pointer SSA、相同scalar
  type、非atomic/volatile、恰好一个store与一个load，期间及其余block suffix无
  第二个memory access/call。Store保存definedness sidecar，唯一load恢复后继续
  transparent chain；
- Artifact：声明`bounded-memory-deferred-poison`。分析不调用alias oracle，任何
  可能别名或额外访问直接不建立sidecar；
- 自动化证据：mutable global上的overflow store/load/freeze恢复双路径，defined
  store/load返回2；同一store后的two-load case回退defined-only并infeasible。
  初始alloca oracle因前置mem2reg消失而未被误计为memory证据；
- 正确性边界：不支持跨block MemorySSA、alias union、循环、heap escape、partial
  width、多load或跨函数memory poison。当前等级 I/T。

## 110. F115：Canonical-Address Memory Poison（2026-07-28）

- 研究问题：F114要求store/load使用同一pointer SSA，导致两个语义等价的constant
  GEP被误判为未知alias；直接放宽为“同一object”又会错误合并不同offset；
- Analysis：使用LLVM DataLayout的`GetPointerBaseWithConstantOffset`分别归一化
  store/load pointer，仅当strip-cast base相同且signed byte offset严格相等时，
  才接受为exact address。Dynamic index、不同base或不同offset继续fail closed；
- Artifact：在既有`bounded-memory-deferred-poison`之外声明
  `canonical-address-memory-deferred-poison`，便于实验区分pointer-SSA直连与
  canonical-address证明；
- 自动化证据：`gep [4 x i8], @array, 0, 2`与
  `gep i8, gep(@array,0,0), 2`之间的poison store/load/freeze恢复双路径；
  offset 1 store与offset 2 load不传播sidecar并保持infeasible；
- 正确性边界：这是exact constant-address等价证明，不是general AA/MemorySSA；
  跨block、symbolic alias、partial overlap、循环与多access仍保守拒绝。当前等级
  I/T。

## 111. F116：Linear-CFG Memory Poison（2026-07-28）

- 研究问题：F114--F115只能在单一basic block识别store-to-load definedness，
  即使两者之间只是unconditional CFG edge也会丢失合法freeze语义；
- Analysis：从store向后及从load向前最多遍历64条edge。每一步要求当前block只有
  一个successor、下个block只有一个predecessor，沿途除目标exact store/load外
  不得有任何memory effect；visited set显式拒绝cycle。两向证明必须在lowering
  context中汇合到同一store sidecar；
- Artifact：声明`bounded-cross-block-memory-deferred-poison`。该capability只表示
  unique-edge linear corridor，不暗示一般dominance、MemorySSA或join处理；
- 自动化证据：store与load跨两个unconditional blocks时poison/freeze恢复双路径；
  本阶段的merge反例后来由F124--F149的common-store、path-dependent、initial-state
  与byte-lane
  edge contracts转为正例；
- 正确性边界：F116本身只证明linear corridor；branch/join、loop与跨路径clobber
  的当前支持边界见F124--F149；general symbolic alias与control-flow lane PHI仍
  拒绝。当前等级 I/T。

## 112. F117：Direct-Call Return Poison ABI（2026-07-28）

- 研究问题：LLVM poison可经函数返回值到达caller中的freeze；在callee return前
  强制defined会错误丢失合法nondeterministic语义，只传wrapped value又会把poison
  当普通值；
- Analysis：只接受bounded integer callee、唯一direct callsite、callee poison
  source到return的single-use透明链，以及call result到freeze的single-use透明链。
  Producer仍复用F110 definedness计算；多callsite和indirect target不建立通道；
- ABI：function声明`return_defined`，每个return携带1-bit `defined` operand，
  call声明`defined_dst`。Program validator要求function/return/call三端一致并限制
  non-pointer、non-void typed return；executor在pop callee frame前求值该bit并写回
  caller-local binding；
- 自动化证据：poison callee return在caller freeze恢复双路径；defined nsw add
  精确返回2；同一callee两个callsite回退defined-only并infeasible；独立executor
  test证明false bit跨frame选择fallback 42，缺失return或call endpoint均被拒绝；
- 正确性边界：尚不传递poison argument，不支持多callsite summary、indirect
  dispatch、pointer/aggregate/vector return、recursion或跨函数memory poison。
  当前等级 I/T。

## 113. F118：Direct-Call Argument Poison ABI（2026-07-28）

- 研究问题：caller中的poison producer可作为callee参数，并只在callee内被freeze；
  caller-side defined assumption会错误剪枝，单传wrapped argument则丢失poison；
- Analysis：要求callee只有一个direct callsite、bounded integer argument、producer
  到call operand及对应formal parameter到freeze均为single-use透明链。Backward
  source walk以64层和active set为界，支持core BV/cast/icmp/select/PHI/ssa.copy；
- ABI：callee `defined_params`把原参数索引映射到追加的1-bit typed parameter，
  call `defined_args`映射到对应追加argument。Validator复检两侧mapping完全相等、
  parameter width为1且不与pointer-domain auxiliary slot重叠；executor沿既有
  call-frame参数绑定自然传输，不维护旁路状态；
- 自动化证据：constant overflow实参在callee freeze恢复双路径，defined nsw add
  精确返回2；callee具有两个callsite时回退infeasible；独立artifact test证明false
  defined argument跨frame选择42，缺失call/callee metadata均被拒绝；
- 正确性修复：F117 `return_defined` detection改为从实际return operand做backward
  slice，避免“函数内存在传参poison”被误报为poison return；
- 正确性边界：multi-callsite、indirect call、多个consumer、memory/aggregate/
  pointer/vector poison argument与recursion仍保守拒绝。当前等级 I/T。

## 114. F119：Bounded Multi-Callsite Poison ABI（2026-07-28）

- 研究问题：F117--F118的defined-bit ABI并不依赖callee只有一个调用点；真正约束是
  所有function uses必须可枚举为exact direct calls，并满足各方向的sink contract；
- Analysis：最多枚举64个direct callsites。Return poison要求每个call result均沿
  single-use chain进入freeze，任何普通consumer使整个return通道fail closed；
  argument poison只要求formal parameter进入freeze且至少一个actual argument含
  deferred source，其他callsite显式传`defined=true`；
- Artifact：实际多调用点使用时声明`bounded-multicallsite-deferred-poison`；
  function signature中的defined auxiliary slot对所有calls一致，validator继续逐
  call复检mapping；
- 自动化证据：同一argument sink被poison/defined两个callsite调用时，第一条路径
  恢复双分支且第二条精确传true；同一poison-return callee的两个call results均
  freeze时恢复双分支；已有“一个call普通消费、一个call freeze”反例仍infeasible；
- 正确性边界：function bitcast/alias、indirect call、超过64 callsites、任一return
  ordinary consumer、递归和跨函数memory仍拒绝。当前等级 I/T。

## 115. F120：Symbolic Pointer-Memory and Initial Definition Merge（2026-07-28）

- 审查发现：pointer load若slot具有global initializer，旧实现完全跳过runtime
  reaching stores；symbolic GEP pointer又以静态base address作为loaded-value
  guard。两者组合会把合法非零index错误折叠到initializer/base；
- Provenance：`sameStaticPointer`补齐dynamic index/scale/width/object base/range、
  interprocedural与realloc identity。写入pointer cell的finite symbolic GEP先通过
  object bounds枚举为concrete address alternatives，再以实际loaded pointer等于
  address作为provenance guard，声明`bounded-symbolic-pointer-memory`；
- Reaching definitions：fixed point新增`hasInitialDefinition` entry state。Data与
  function pointer load统一合并global initializer和runtime stores；function target
  discovery使用同一集合，避免新target因未分配stable function ID而漏失。实际同时
  存在initial/runtime defs时声明`pointer-initial-definition-merge`；
- 自动化证据：symbolic index 0/1的`"AB"` pointer经mutable global cell roundtrip
  后产生A/B双路径；function-pointer global必写overwrite在increment/increment-two
  间双分派；conditional write路径与no-write initializer fallback同样双分派；
- 证据校准：初始alloca版本被mem2reg消除，只证明普通symbolic GEP，未计入F120；
  最终oracle使用真实mutable global pointer cell；
- 正确性边界：dynamic domain仍受alias limit约束；general pointer-int casts、
  partially initialized raw bytes、moving heap graph与concurrent atomic pointer
  cells未覆盖。当前等级 I/T。

## 116. F121：Transitive Argument-to-Return Poison（2026-07-28）

- 审查发现：F117 return detection只识别callee-local poison producer。若F118已标记
  的formal parameter经callee透明BV链返回，argument defined bit会进入frame但
  `return_defined`不建立，caller freeze断链；
- Analysis：return backward slice除local source外识别已证明的deferred-poison
  Argument，沿bounded BV/cast/icmp/select/PHI/ssa.copy透明节点传播；active set
  防止cyclic SSA traversal。该condition在callee内由F110传播，再由F117 return
  channel输出；
- Artifact：argument与return ABI同时使用时声明
  `bounded-transitive-call-deferred-poison`；既有`defined_params/defined_args`和
  `return_defined/defined/defined_dst`仍由统一validator逐端复检；
- 自动化证据：同一passthrough callee具有poison/defined两个direct callsites，
  parameter先经`xor 0`再return，两项call result均freeze；poison path恢复1/2，
  defined call传true，并同时触发multi-callsite与双向ABI capabilities；
- 审查修复：capability条件从“函数同时有argument channel和任意return poison”
  收紧为“return backward slice实际到达deferred-poison Argument”；callee内部
  freeze参数、但返回独立local poison的负例同时保留双向ABI且禁止transitive-call
  capability，防止artifact夸大语义覆盖；
- 正确性边界：仅透明bounded integer slice；indirect/mixed function uses、
  recursion、memory/aggregate/vector passthrough仍拒绝。当前等级 I/T。

## 117. F122：Bounded Multi-Consumer Deferred Poison（2026-07-28）

- 研究问题：F97--F121要求每个deferred-poison value只有一个use，即使同一poison
  SSA的所有消费者都最终进入`freeze`，也会被defined-only under-approximation
  裁掉；这会系统性损失编译优化后出现的fan-out/fan-in路径；
- Analysis：把single-use existential walk替换为最多256次value访问的all-use
  proof。每个use必须是freeze，或属于已支持的integer cast/BV/icmp/select/PHI、
  `ssa.copy`、exact memory sidecar、direct argument/return ABI之一，并且递归证明
  其全部后继到达freeze。active set拒绝循环，空use、未知consumer和预算溢出均
  fail closed；
- Definedness：共享producer condition沿每条透明链传播；每个freeze仍创建各自
  stable dynamic nondeterministic value，符合“freeze结果稳定、不同freeze不要求
  相同”的LLVM语义。Artifact声明
  `bounded-multiconsumer-deferred-poison`；
- 自动化证据：同一`add nsw` poison由两个独立freeze消费，联合branch可恢复1/2；
  一个freeze加一个最终未freeze普通consumer的混合图保持不可行，并用通用
  `--reject-capability` oracle禁止能力误标；
- 正确性边界：只证明有限、无环、全部sink可见的integer use graph；general
  memory alias、循环poison SSA、indirect call、aggregate/vector/FP与超过256访问
  的图继续使用defined-only under-approximation。当前等级 I/T。

## 118. F123：Bounded Multi-Load Memory Poison（2026-07-28）

- 研究问题：F114--F116只允许一个exact load。F122虽能证明SSA fan-out，却无法表示
  同一memory version的多个load；更重要的是，旧forward scan在发现首个load后立即
  停止，可能看不到后续consumer，证明边界不够严格；
- Memory-version analysis：从store沿最多64个unique-successor/unique-predecessor
  block扫描，收集最多64个同类型、非atomic/volatile、exact canonical-address
  loads；精确同址全宽store作为version clobber，函数出口作为终点。未知memory
  effect、不同地址访问、CFG分叉/合流、循环和预算溢出均拒绝；
- Reverse proof：load向前驱逆序搜索最近store时允许跳过同址earlier loads，
  但遇到任意不同/未知memory effect即fail closed。这样每个load都绑定同一个
  可复检definedness sidecar，不以源代码顺序猜测别名；
- Composition：store对应的全部load还必须分别通过F122 all-use freeze-sink proof；
  两项证明同时成功才声明`bounded-multiaccess-memory-deferred-poison`。原有
  same-block、canonical-address与cross-block capabilities保持；
- 自动化证据：同一poison store的两个load各自freeze并产生1/2；同址defined
  overwrite终止version。已有“第二个load freeze、首个load普通消费”负例继续不可行
  且禁止capability；旧memory正例增加显式同址clobber，防止首个load后证明截断；
- 正确性边界：尚不是alias-aware MemorySSA；只覆盖有限线性CFG和exact full-width
  scalar cell，不覆盖symbolic alias、partial overlap、分支memory phi、循环、
  atomic/volatile及并发内存。当前等级 I/T。

## 119. F124：Acyclic Branch Memory Poison（2026-07-28）

- 研究问题：F123只沿unique predecessor/successor线性CFG传播，无法处理一个store
  支配diamond两臂、再在merge block读取的常见优化形态；
- Forward proof：以active/completed block集合对store后的CFG执行最多64 block的
  bounded DFS，收集同址loads，并在exact clobber、exit处结束各路径；active back
  edge、未知memory effect、非同址访问与预算溢出均拒绝；
- Reverse proof：每个load从所在block逆序跨越同址earlier loads，对全部predecessor
  递归寻找最近store；只有每条路径返回同一个`StoreInst`才成立。不同路径分别返回
  original store与clobber store时整体失败，因此没有伪造path-dependent memory
  definedness PHI；
- Composition：forward收集的每个load必须反向解析回原store，并继续满足F122
  all-use sink proof。使用branch/join证明时artifact声明
  `bounded-branch-memory-deferred-poison`；
- 自动化证据：symbolic selector diamond两臂无memory effect，merge load/freeze
  对两项selector状态分别产生1/2，即`1,1,2,2`；path-dependent store与initial
  definition的组合由后续F125/F127/F129处理；
- 正确性边界：只覆盖acyclic exact-address、all-path common-store情况；不同
  reaching stores的真正definedness PHI、循环MemorySSA、symbolic alias、partial
  overlap、atomic/volatile与并发内存仍未实现。当前等级 I/T。

## 120. F125：Path-Dependent Memory Definedness PHI（2026-07-28）

- 研究问题：F124只接受all-path common store；diamond两臂分别full-width写入同一
  cell时，数值memory本身可由runtime正确执行，但poison definedness仍需随实际CFG
  edge选择，不能静态合并为任一store condition；
- Static proof：仅接受2--64个direct predecessors；merge block在目标load前不得有
  memory effect，每个predecessor必须具有唯一successor，且逆序遇到的首个memory
  effect必须是同类型、非atomic/volatile、exact-address full store。至少一个
  incoming value包含bounded local poison source，load的全部use还需通过F122；
- Edge lowering：为每条predecessor->merge edge预先建立edge block，并写入统一
  `<load>__defined` destination。Poison store写其condition，defined store写constant
  true；merge load沿实际执行路径读取该sidecar，再由freeze决定wrapped value或
  independent stable choice；
- Artifact contract：function metadata声明`memory_defined_phis`，包含load、
  defined destination、merge block与全部incoming edge blocks；独立validator要求
  每条edge恰有一个i1 identity assignment，且末尾jump指向声明merge。Artifact声明
  `bounded-memory-definedness-phi`；
- 自动化证据：symbolic selector一臂`add nsw` poison store、一臂constant 7 store，
  执行结果为poison侧1/2加defined侧2，即`1,2,2`；checker删除一个edge assignment
  后，production validator以memory-PHI endpoint错误拒绝artifact；
- 正确性边界：只覆盖direct-predecessor full-store merge；argument/call/memory
  来源的incoming poison、嵌套/循环MemorySSA、symbolic alias、partial overlap、
  atomic/volatile与并发内存仍fail closed。当前等级 I/T。

## 121. F126：Interprocedural Memory Definedness PHI（2026-07-28）

- 研究问题：F125的potential-source analysis只识别callee-local producer，因此
  F117 return defined-bit或F118 argument defined-bit虽已进入function context，
  仍不能作为incoming store condition写入memory-PHI edge；
- Composition：bounded source walk新增两类已证明端点：`Argument`必须通过
  `argumentHasDeferredPoison`的all-callsite argument contract；direct internal call
  result必须通过callee return backward slice。透明cast/BV/icmp/select/PHI/
  `ssa.copy`仍可继续组合，未知parameter/call不推测；
- Lowering：incoming store直接复用`context.poisonConditions`中的ABI bit，F125
  edge assignment与production validator无需新执行语义。Artifact额外声明
  `bounded-interprocedural-memory-definedness-phi`，contract metadata标记
  `interprocedural: true`；
- 自动化证据：argument方向由caller `add nsw`经F118进入callee，callee在poison/
  defined两臂store后merge/freeze，结果`1,2,2`；return方向由F117 poison-return
  call result在caller两臂store后执行相同oracle，也得到`1,2,2`。两者分别要求
  cross-function argument/return capability与interprocedural memory-PHI；
- 正确性边界：只组合已验证的bounded direct-call integer ABI；indirect/mixed
  callsites、aggregate/vector/FP、memory-mediated跨函数来源、递归与general
  interprocedural alias graph仍fail closed。当前等级 I/T。

## 122. F127：Multilevel Memory Definedness PHI（2026-07-28）

- 研究问题：F125要求store位于merge的direct predecessor，编译优化插入一到多层
  memory-free forwarding blocks后，等价reaching definition会被不必要拒绝；
- Analysis：对每个immediate incoming edge独立执行最多64 block的reverse walk。
  逆序可跨同址earlier loads和无memory blocks；遇到分支时，其全部predecessors必须
  递归解析到同一StoreInst。不同store、未知memory effect、循环或预算溢出拒绝；
- Edge lowering：defined assignment仍放在最终immediate predecessor的edge block，
  但operand可引用祖先store已经计算的condition。所有incoming若解析到同一个store，
  则退回F124 common-store proof，不创建冗余PHI；
- 自动化证据：poison/defined stores分别经过2层/1层forwarding后在merge load，
  symbolic selector结果`1,2,2`；一臂exact defined clobber、另一臂沿祖先poison
  store forwarding的非对称图同样得到`1,2,2`；artifact声明
  `bounded-multilevel-memory-definedness-phi`并标记contract `multilevel`；
- 正确性边界：仅bounded acyclic predecessor subgraph；未知alias、partial memory
  effects和超过64 block的图fail closed。当前等级 I/T。

## 123. F128：Cyclic Memory Definedness PHI（2026-07-28）

- 研究问题：loop header load的entry/backedge stores形成真正动态memory condition
  state；F124 forward DFS和F127 reverse walk都必须拒绝cycle，不能直接复用静态
  common-store结果；
- Semantics：当loop header每个direct incoming edge都能在本edge子图内解析到exact
  full store时，F125 edge assignment自然成为每次迭代的state update。header支配
  某incoming predecessor时标记cyclic；producer sink proof以显式incoming-store
  membership替代会遇到backedge的forward DFS；
- Artifact：声明`bounded-cyclic-memory-definedness-phi`并在
  `memory_defined_phis` contract标记`cyclic`；既有validator继续逐edge验证assignment
  和loop-header jump；
- 自动化证据：entry poison store进入loop，第一次load/freeze的choice经SSA PHI
  保存；latch defined store更新condition，第二次load为7并退出，最终按第一次choice
  产生1/2。负例backedge不store，reverse proof遇到cycle，保持不可行并禁止capability；
- 正确性边界：本阶段要求每个iteration edge具有可证明的exact full store；纯
  no-write backedge由F131扩展。单一最终edge内部混合store/carry、多个alias cell、
  partial/atomic/volatile store、nested irreducible loops与并发内存仍fail closed。
  当前等级 I/T。

## 124. F129：Initial Memory Definedness Merge（2026-07-28）

- 研究问题：F127能沿no-store incoming edge回溯祖先store，但若该路径一直到function
  entry仍无runtime writer，global scalar initializer是合法且defined的reaching
  definition；直接拒绝会裁掉可恢复的poison-store路径；
- Analysis：仅在continuation entry function中对direct scalar integer global
  cell接受initial state；global必须有同类型`ConstantInt` initializer、非TLS且
  address space 0。Internal callee entry不能假定global重新初始化。Reverse result显式
  区分invalid与valid-initial，避免以null同时表示失败；
- Edge semantics：initial incoming endpoint没有StoreInst，F125 edge assignment
  写constant true；其他incoming poison store写其condition。Canonical numeric
  memory继续从已有initial image读取实际值；
- Artifact：声明`bounded-initial-memory-definedness-merge`，memory-PHI contract
  标记`initial`；production validator仍复检所有edge assignment/jump；
- 自动化证据：symbolic selector一臂local poison store，另一臂无store并读取global
  initializer 0；poison侧freeze产生1/2，initial侧确定返回2，合计`1,2,2`；
- 正确性边界：只支持entry function中的direct scalar ConstantInt global
  initializer；aggregate/GEP subcell由F130扩展。Stack uninitialized state、
  heap lifetime initial state、undef/poison
  initializer、symbolic alias与并发内存仍fail closed。当前等级 I/T。

## 125. F130：Initial Subobject Definedness Merge（2026-07-28）

- 研究问题：F129只把全局对象根地址上的scalar initializer视为defined entry
  state，实际LLVM IR常以独立重算的constant GEP访问数组元素或结构体字段；若
  no-store路径保留聚合初始化器、另一条路径写入poison，仍需要同一个edge-defined
  PHI表达动态definedness；
- Analysis：使用LLVM `GetPointerBaseWithConstantOffset`按target `DataLayout`
  取得global base与signed byte offset，再调用官方
  `ConstantFoldLoadFromConst`从aggregate initializer折叠目标typed scalar load。
  仅当结果为`ConstantInt`、global非TLS/address-space 0、且位于continuation entry
  function时，no-store incoming才是valid defined state；越界、undef/poison、
  非整数或不可折叠子对象保持fail closed；
- Composition：store/load可使用独立构造但base+offset等价的GEP，F115 canonical
  address proof确认同一cell；F125/F127 edge block在initial edge写true，在poison
  store edge写producer condition。Numeric memory仍读取序列化的aggregate initial
  image，不用另造常量值；
- Artifact：声明`bounded-initial-subobject-definedness-merge`，并在既有
  `memory_defined_phis` contract同时标记`initial`和`initial_subobject`。Production
  validator强制`initial_subobject => initial`，防止孤立子对象声明绕过initial
  state证明；
- 自动化证据：`[4 x i8] zeroinitializer`的element 2在一臂接收`add nsw` poison，
  另一臂保留初值；merge/freeze对symbolic selector产生`1,2,2`。Checker同时要求
  canonical-address与initial/subobject capabilities，并删除父级`initial`执行
  tamper oracle，生产执行器必须拒绝；
- 正确性边界：仅支持constant-offset、typed scalar integer、完整load/store且可由
  LLVM常量折叠证明的global subobject。Symbolic indices、partial overlap、
  aggregate/vector load、internal-callee初始假设、stack/heap lifetime初值、
  atomic/volatile与并发内存仍fail closed。当前等级 I/T。

## 126. F131：Cyclic No-Write Definedness Carry（2026-07-28）

- 研究问题：F128要求每个loop entry/backedge都存在exact full store；若某个
  backedge整条路径不写目标cell，LLVM memory state应保持上一迭代的definedness，
  既不能把它误当global initializer写true，也不应丢弃可恢复的poison路径；
- Analysis IR：reaching-definition结果由nullable store重构为显式`Store`、
  `Initial`、`Carry`三态。Reverse walk在回到目标loop-header load时，只扫描本次
  load之后的memory effects；若未遇到writer则返回`Carry`。非header cycle、未知
  memory effect、超过64 block预算仍拒绝；
- Edge semantics：entry store edge照常写producer condition；carry backedge执行
  `<load>__defined = <load>__defined`，从持久executor local state保留上一迭代
  sidecar。若header之后存在exact full store，reverse walk优先返回该store，不会
  错误carry旧状态；
- Artifact：声明`bounded-cyclic-memory-definedness-carry`，top-level
  memory-PHI contract同时标记`cyclic`与`carry`，具体endpoint标记`carry`。
  Production validator要求carry endpoint的唯一i1 identity assignment必须从同一
  defined destination自引用，并强制`carry => cyclic`及top-level/endpoint一致；
- 自动化证据：entry poison store后运行两次header load，backedge无store；第二次
  freeze仍按carried poison condition产生1/2。Checker删除父级`cyclic`后artifact
  被生产执行器拒绝。负例在同一最终backedge子图内一臂defined store、一臂no-write，
  因无法在单一edge assignment表达路径选择而保持不可行并禁止capability；
- 正确性边界：支持一个immediate incoming edge的全部bounded predecessor路径都
  解析为同一carry状态；canonical direct-arm store/carry由F132扩展。更深层
  forwarding、多个条件或不同store在进入同一最终edge前发生合流时，仍需递归
  memory-SSA transfer；symbolic alias、partial overlap、
  atomic/volatile、跨cell state与并发内存也不推测。当前等级 I/T。

## 127. F132：Conditional Store/Carry Transfer（2026-07-28）

- 研究问题：F131只能在整条backedge无writer时自carry；常见loop latch以一个
  canonical diamond条件更新cell，store臂应使用新definedness，no-write臂应保留
  prior state。把整个最终edge静态归为任一来源都会不健全；
- Static proof：reverse analysis在同一block发现恰好两个不同来源时，只接受一个
  direct parent以exact full store结束、另一个parent解析为Carry，且两parent各自
  只有同一个conditional `BranchInst` predecessor；branch的两个successor必须恰为
  这两臂。其他store/carry合流继续拒绝；
- Transfer semantics：最终backedge edge block生成一位
  `select(branch_condition, store_defined, prior_defined)`；若store位于false arm
  则交换两operand。Defined store使用true，poison/interprocedural store复用其已
  验证condition，carry arm自引用header load的defined destination；
- Artifact：声明`bounded-conditional-memory-definedness-carry`，contract和具体
  endpoint标记`conditional_carry`并继续标记`carry/cyclic`。Validator要求endpoint
  恰有一个一位select写声明destination、condition/两臂均为合法operand且恰一臂
  自引用prior defined state，同时复检top-level/endpoint标记一致；
- 自动化证据：entry poison store后，symbolic selector在latch选择defined store或
  no-write carry；第二次load/freeze分别产生确定2与1/2，完整终态为`1,2,2`。
  Checker删除top-level conditional标记后生产执行器拒绝。Store臂增加额外forward
  block的负例仍不可行且不声明capability；
- 正确性边界：当前证明只识别direct-arm canonical two-way branch，不跨arm内
  forwarding由F133扩展；仍不递归组合多个condition，也不合并两个不同conditional stores。
  Symbolic alias、partial overlap、atomic/volatile、异常边和并发memory effects
  继续fail closed。当前等级 I/T。

## 128. F133：Forwarded Conditional Carry（2026-07-28）

- 研究问题：F132的store/carry语义只依赖共同branch condition，但实现要求branch
  successor就是最终join parent；优化器插入一个或多个memory-free forwarding block
  后，语义不变却被拒绝；
- Static proof：对最终join的store与carry candidate分别沿unique-predecessor链反向
  枚举conditional-arm祖先，总reverse/ancestor walk共享64-block预算。Store链必须
  实际经过已证明的exact StoreInst所在block；两臂只有找到同一个conditional
  `BranchInst`且对应不同successors时才组合。Cycle、非唯一predecessor和预算溢出
  保持拒绝；
- Transfer：复用F132的最终edge i1 select，不增加运行时状态；branch condition在
  LLVM SSA中支配forwarding join。Store arm仍选择store condition，carry arm仍选择
  prior header definedness；
- Artifact：新增`bounded-forwarded-conditional-memory-definedness-carry`，
  contract/endpoint标记`forwarded_conditional_carry`，并同时保留
  conditional/carry/cyclic和multilevel capabilities。Validator强制forwarded标记
  依附conditional标记且top-level与endpoint一致；
- 自动化证据：defined store后增加独立forward block再与carry arm合流，symbolic
  selector仍产生`1,2,2`；checker移除父conditional标记后生产validator拒绝。
  三路最终join（两个forwarded store paths加carry）保持不可行并禁止新capability；
- 正确性边界：支持每个候选沿unique-predecessor链寻找同一二路split。多路join、
  中的同源endpoint由F134扩展。Nested conditional transfer、两个不同store
  condition、symbolic alias、partial
  overlap、atomic/volatile与并发memory effects仍fail closed。当前等级 I/T。

## 129. F134：Multi-Arm Equivalent-Source Carry（2026-07-28）

- 研究问题：一个外层store/carry branch的store臂可在写入后因无关控制流再次分裂，
  导致最终join有三个或更多predecessors；若多个endpoint都解析到同一个StoreInst，
  它们在definedness transfer上是同一source，不需要多值memory PHI；
- Source grouping：reverse analysis按store identity、source kind、已有condition、
  arm orientation、forwarded/multiarm flags的完整tuple将全部candidate分组。只有
  恰好两个groups且分别为exact Store和Carry时进入conditional composition；
- All-endpoint proof：先从representative endpoints提出共同conditional split，再
  对store group和carry group的每一个endpoint重新执行bounded ancestor-arm查找。
  每项必须映射到同一个BranchInst及对应同一个arm successor；不能以一个代表样本
  替整组背书。全部walk共享64-block预算；
- Transfer/Artifact：执行语义继续复用单一i1 edge select。新增
  `bounded-multiarm-conditional-memory-definedness-carry`，contract/endpoint标记
  `multiarm_conditional_carry`并依附conditional contract；validator复检层级与
  top-level/endpoint一致；
- 自动化证据：outer selector选择store/carry；store之后的第二selector分到两个
  forwarding endpoints，三路join执行产生carry侧1/2和两个defined侧2，即
  `1,2,2,2`。Checker移除父conditional标记后生产validator拒绝。两个不同StoreInst
  加carry形成三个source groups，保持不可行且禁止新capability；
- 正确性边界：只合并完全相同的reaching source identity。不同StoreInst即使当前
  都是defined constants的情况由F135扩展；递归condition tree、symbolic alias、
  partial overlap、atomic/volatile、异常边与并发memory effects继续fail closed。
  当前等级 I/T。

## 130. F135：Equivalent Defined-Store Carry（2026-07-28）

- 研究问题：F134按StoreInst identity分组，两个不同writers即使都写入LLVM语义上
  必定defined的值也形成不同groups；对numeric memory它们当然不同，但对一位
  definedness sidecar都严格等于true；
- Equivalence proof：两个`Kind::Store` sources仅在各自value都通过bounded
  `valueMayCreateDeferredPoison`分析、确认没有local flagged producer、已验证
  interprocedural poison或透明传播来源时视为definedness-equivalent。Exact address、
  full-width type与non-atomic/volatile条件仍由reaching-store proof保证；
- All-writer validation：source group保留每个endpoint自己的StoreInst。
  Ancestor-arm复检对每个candidate使用其自身writer作为required store，不能让
  representative writer替其他路径背书。最终edge select的store arm写true；
- Separation of concerns：该等价只改变definedness grouping；原有store lowering
  继续向numeric memory写各自实际值。因此constant 7/8不会被错误合并成同一个
  数据值；
- Artifact/证据：声明`bounded-equivalent-defined-store-memory-carry`，
  contract/endpoint标记`equivalent_defined_stores`并依附multiarm conditional
  contract；validator复检层级。两个distinct constant stores加carry产生
  `1,2,2,2`，删除父multiarm标记后生产执行器拒绝；一条defined writer和一条
  `add nsw` poison writer不等价，负例保持不可行；
- 正确性边界：只合并经当前bounded source analysis证明必定defined的writers。
  写入同一poison-capable SSA value的writers由F136扩展；
  symbolic alias、partial overlap、atomic/volatile、异常边和并发memory effects
  继续fail closed。当前等级 I/T。

## 131. F136：Shared-Poison Writer Carry（2026-07-28）

- 研究问题：不同StoreInst若写入同一个poison-capable LLVM SSA value，它们的
  numeric write sites不同，但definedness condition严格相同；F135只合并必定
  defined writers，仍会不必要拒绝该结构；
- Equivalence proof：两个`Kind::Store` candidates仅在
  `getValueOperand()`指针完全相同的情况下获得shared-poison equivalence。相同
  opcode、常量或语法形状但不同SSA producers不视为等价，不进行表达式猜测；
- Writer membership：首次实现时incoming只保留representative StoreInst，导致
  第二个writer的`poisonMergeLoadsForStore`查询失败、edge错误退化成true。修复后
  reaching source携带去重的全部`equivalentStores`集合；forward sink proof、
  reverse membership和每个store的condition传播都按该集合判断。代表store仅用于
  读取已证明共享的condition；
- Transfer/Artifact：numeric memory仍执行各自store，definedness store arm选择
  shared condition。声明`bounded-shared-poison-store-memory-carry`，
  contract/endpoint标记`shared_poison_stores`并依附multiarm conditional合同；
  validator复检层级与top-level/endpoint一致；
- 自动化证据：entry先写defined 7；两个route arms各自以不同StoreInst写同一个
  `add nsw` poison value，carry arm保留defined state。两个poison paths各产生1/2，
  carry产生2，总计`1,1,2,2,2`。删除父multiarm标记后生产validator拒绝。两个独立
  poison producers不合并，只有carry路径可行并返回2；
- 正确性边界：仅接受相同SSA value identity，不以结构哈希或SMT等价证明条件。
  两个不同producer的严格两层condition tree由F137扩展；symbolic alias、partial overlap、
  atomic/volatile、异常边和并发memory effects继续fail closed。当前等级 I/T。

## 132. F137：Nested Conditional Writer Carry（2026-07-28）

- 研究问题：两个不同poison writers和一个carry形成三个definedness sources。它们
  不能由F135/F136合并，但若CFG是严格的外层store-vs-carry、内层store-A-vs-store-B
  二叉树，其动态condition可精确组合成两层select；
- Static shape proof：只接受恰好三个source groups且每组一个endpoint，其中两个为
  exact Store、一个为Carry。Ancestor analysis必须找到一个内层conditional branch
  将两个store映射到不同successors，以及一个外层branch将两个store映射到同一
  successor、carry映射到另一successor；内层branch block必须等于外层store
  successor。全部reverse/ancestor工作共享64-block预算；
- Eager dominance：continuation core IR的select在执行前解析全部operands，并非
  lazy SSA control dependence。因此内层predicate与两个stored values必须由LLVM
  DominatorTree证明支配最终incoming terminator；否则即使源LLVM路径合法也拒绝，
  防止carry路径读取未定义local；
- Edge semantics：backedge先生成
  `inner = select(inner_condition, storeA_defined, storeB_defined)`，再生成
  `load_defined = select(outer_condition, inner, prior_defined)`；两层arm方向均从
  BranchInst successor顺序推导。Numeric memory继续由实际store路径决定；
- Artifact：声明`bounded-nested-conditional-memory-definedness-carry`，
  contract/endpoint标记`nested_conditional_carry`并依附multiarm conditional。
  Validator从外层非self arm解析inner destination，要求endpoint中恰有一个一位
  inner select，condition/两臂合法且内层两臂均不直接引用prior state；
- 自动化证据：两个支配latch的独立`add/sub nsw` producers分别由内层selector
  写入，外层selector还可选择defined carry；两个poison路径各产生1/2，carry产生2，
  总计`1,1,2,2,2`。删除父multiarm标记后production validator拒绝。三个独立
  poison stores加carry形成四source groups，保持不声明capability且仅carry返回2；
- 正确性边界：仅支持固定深度2、三个source groups、单cell exact full stores且
  所有eager operands支配最终edge。更深递归condition tree、路径局部condition的
  edge staging、symbolic alias、partial overlap、atomic/volatile、异常与并发
  memory effects仍fail closed。当前等级 I/T。

## 133. F138：Bounded Recursive Writer/Carry Condition Tree（2026-07-28）

- 研究问题：F137用固定字段描述两个writer，无法组合三个以上独立definedness
  sources。继续增加`secondary/tertiary`字段不能形成可递归证明或可验证的语义；
- Proof IR：新增扁平`MemoryDefinednessTreeNode`，叶节点为exact Store或唯一Carry，
  内部节点保存LLVM i1 branch condition及true/false子节点索引。当前预算为4--8个
  singleton source groups、深度3--6、一个carry、64次共享CFG访问；已有F137深度2
  合同保持不变；
- CFG reconstruction：对每个leaf从最终join endpoint沿唯一前驱链收集包含其writer
  的conditional ancestors。递归构造器只选择出现在当前全部leaf路径中且把集合
  严格分为两个非空successor partitions的branch，并从最外层split向内分解。无法
  完整分割、非singleton source group、多个carry或预算耗尽均拒绝；
- Eager soundness：所有内部predicate及所有store value必须为constant/argument，
  或由LLVM DominatorTree证明支配最终incoming terminator。该条件防止portable
  continuation的eager select读取只在某个LLVM arm上定义的local；
- Edge semantics：树以后序生成`recursive_memory_defined_*`一位select，根直接
  写入load的`__defined` sidecar；Store叶读取对应writer condition，Carry叶唯一
  自引用prior sidecar。Numeric page-COW memory仍由实际LLVM store路径更新；
- Artifact/validator：声明
  `bounded-recursive-memory-definedness-condition-tree`，并在contract/endpoint
  写入`recursive_conditional_carry`、`condition_tree_depth`和
  `condition_tree_leaves`。生产validator从根assignment反向重建select DAG，复检
  i1节点、无环、全部临时节点可达、精确深度/叶数、full-binary node count和唯一
  carry；endpoint与top-level元数据必须一致；
- 自动化证据：三个独立`add/sub/mul nsw` writer与一个defined carry形成深度3树，
  三个poison paths各产生freeze结果1/2，carry产生2，总计
  `1,1,1,2,2,2,2`。将endpoint深度篡改加一后生产validator拒绝。相同四叶CFG若
  predicate/value只在各自path局部定义，则不声明F138且仅carry返回2；
- 正确性边界：这是有界singleton-leaf control tree，不是通用MemorySSA。等价源
  分组与递归树的组合、路径局部lazy staging、symbolic alias、partial overlap、
  multi-cell transfer、atomic/volatile、异常和并发memory effects仍fail closed。
  当前等级 I/T。

## 134. F139：Grouped Recursive Source Tree（2026-07-28）

- 研究问题：F138要求每个tree leaf只有一个final CFG endpoint，因此同一writer在
  store之后发生无memory effect的控制分流时会被拒绝；此类endpoints具有完全相同
  的definedness source，不应人为复制tree leaf；
- Group representation：每个递归leaf现在保存一个由完整`sameSource` tuple证明的
  endpoint group。组内可以是同一StoreInst、必定defined writers或写入同一
  poison-capable SSA value的writers；leaf仍只表示一个definedness condition，
  numeric stores保持原路径语义；
- All-endpoint arm proof：候选branch必须出现在当前每个source group的每个endpoint
  ancestor list中；同一group的全部endpoints必须映射到同一个successor。只有随后
  才把整个group作为一个leaf加入true/false partition。任何组内跨臂分歧都使该
  branch不可用，不能由代表endpoint替其他成员作证；
- Artifact：复用F138的深度、叶数和根select合同，另声明
  `bounded-grouped-recursive-memory-definedness-condition-tree`并在top/endpoint
  标记`grouped_recursive_conditional_carry`。Validator要求该标记蕴含
  `recursive_conditional_carry`且两级元数据一致；删除父标记的篡改被拒绝；
- 自动化证据：深度3树的writer A在store后由额外symbolic branch分成两个latch
  endpoints，writer B、writer C和carry各一个endpoint。树仍为4 source leaves，
  但执行产生四个poison CFG paths的1/2与一个carry结果2，总计
  `1,1,1,1,2,2,2,2,2`；
- 正确性边界：只合并已由source identity证明且对每个选定ancestor branch同臂的
  endpoints。若同一source出现在tree的不同结构位置，需要带共享叶/DAG或条件
  归约；该位置重复随后由F140的受控leaf复制扩展。F138的4--8 leaves、深度3--6、
  唯一carry、64-block和eager-dominance预算均保持。当前等级 I/T。

## 135. F140：Repeated-Source Recursive Tree（2026-07-28）

- 研究问题：一个definedness source可以由不同StoreInst写入同一SSA value，并位于
  condition tree的不同结构位置。F139要求source group同臂，无法把这种非连续
  source class压成一个leaf；
- Position conflict proof：analysis为source group的全部endpoint建立
  `BranchInst -> successor`映射。如果同一个ancestor branch在组内映射到不同
  successors，则判定为structural position conflict；只检查代表endpoint不充分；
- Controlled duplication：发生位置冲突的Store source group按原CFG endpoints展开
  为多个tree leaves。每个leaf仍引用已经过identity proof的同一个definedness
  condition，但递归partition分别证明其结构位置。没有冲突的group继续使用F139
  单leaf表示；Carry group若发生位置冲突仍拒绝，保证树中只有一个prior-state
  self-reference；
- Budget/semantics：展开后的树仍必须有4--8 leaves、深度3--6、唯一carry，并满足
  64-block和eager-dominance预算。Lowering无需新增执行op，重复Store leaves读取
  同一condition，numeric stores及路径计数保持实际CFG语义；
- Artifact：声明
  `bounded-repeated-source-recursive-memory-definedness-condition-tree`，
  top/endpoint标记`repeated_source_recursive_conditional_carry`。Validator要求
  该标记蕴含F138 recursive contract；删除父标记后拒绝；
- 自动化证据：writer A/B在相邻嵌套branch的不同位置写同一个`add nsw` SSA value，
  writer C/D各有独立poison condition，再加一个carry。source classes为4，位置展开
  后为深度4、5叶树；四个poison CFG paths各产生1/2，carry产生2，总计
  `1,1,1,1,2,2,2,2,2`。F139 post-store fanout正例继续不声明F140 capability；
- 正确性边界：只复制Store leaves，不复制多个Carry位置；不做Boolean condition
  minimization，也不声称canonical BDD/DAG。多个Carry位置随后由F141扩展。超过
  leaf/depth/CFG预算、路径局部eager operand、symbolic alias、partial overlap和
  multi-cell transfer继续fail closed。当前等级 I/T。

## 136. F141：Multi-Carry Recursive Tree（2026-07-28）

- 研究问题：prior iteration definedness可能在递归树的多个互斥控制位置保持不变。
  F138--F140的“唯一Carry leaf”和根select恰有一个直接self arm是假定的形状约束，
  不是语义必要条件；
- Analysis：当Carry source group对同一个ancestor branch映射到不同successors时，
  复用F140 position-conflict证明并按endpoint展开。每个Carry leaf引用同一个prior
  `merge.defined`，但由完整递归branch partition保证运行时只选择一条路径；
- Edge/validator semantics：根select允许0或1个直接self arm；production validator
  从根递归遍历全部select operands，实际统计self-reference leaf occurrences。
  Artifact新增`condition_tree_carry_leaves`，top与endpoint必须和实际计数完全一致；
  非multi-carry树必须恰为1，multi-carry树必须为2--8。F137固定深度2 validator保持
  原有恰一直接self arm规则；
- Artifact：声明
  `bounded-multicarry-recursive-memory-definedness-condition-tree`，
  top/endpoint标记`multicarry_recursive_conditional_carry`，并要求其蕴含
  repeated-source与recursive父合同。篡改endpoint carry-leaf count后被树validator
  拒绝；
- 自动化证据：三个独立poison Store leaves与两个结构位置不同的Carry leaves形成
  深度4、5叶树。三个poison路径各产生freeze结果1/2，两个carry路径各返回2，总计
  `1,1,1,2,2,2,2,2`。F140 repeated-Store正例重新生成后仍为单carry且显式拒绝
  F141 capability；
- 正确性边界：仍要求Carry endpoints属于同一exact no-write source class，不允许
  hidden clobber。树预算仍为4--8 leaves、深度3--6和64-block；不做无界DAG/BDD
  化、symbolic alias、partial overlap或multi-cell MemorySSA。当前等级 I/T。

## 137. F142：Disjoint Multi-Cell Memory Definedness PHI（2026-07-28）

- 研究问题：`predecessorPoisonStores`此前把目标load之前或writer回溯途中出现的任意
  其他memory operation视为clobber。因此同一loop header只能为第一个cell建立
  definedness PHI，即使后续访问有静态不相交证明；
- Region proof：新增固定宽度scalar region disjointness。地址必须由target
  DataLayout规范化为constant base+offset；同一base仅在两个`[offset, offset+size)`
  区间严格不重叠时接受，不同base仅接受两个不同`GlobalVariable`对象。宽度未知、
  算术溢出、symbolic offset和其他base组合返回unknown；
- Reverse scan：header pre-scan允许越过非atomic/nonvolatile的同cell exact load或
  proven-disjoint scalar load/store；backward writer scan同样越过disjoint accesses，
  但overlap/unknown effect仍使当前cell证明整体失败。每个cell继续独立执行原有
  Store/Carry/conditional/tree reaching-source analysis；
- Composition：`prepareMemoryPoisonMerges`在全部merge建立后，对同一header中的load
  pairs再次运行disjoint proof并标记两侧contract。Edge block可顺序更新多个独立
  `__defined` sidecars；numeric memory及每个load的freeze semantics不合并；
- Artifact：声明`bounded-multicell-memory-definedness-phi`，每个参与contract标记
  `multicell`。Production validator要求同一merge block至少两个marked contracts；
  删除一个marker导致孤立multi-cell contract并被拒绝；
- 自动化证据：同一`[4 x i8]` global的offset 0/1 subcells在entry写7，回边可同时写
  两个独立poison values或carry。两个freeze的xor在poison路径可为0/非0，carry路径
  为0，产生`1,1,2`；两个load指向同一cell的对照组同样执行，但明确不声明multi-cell
  capability；
- 正确性边界：这里只组合独立full-width scalar cells，不传播跨cell value relation，
  不处理partial overlap、symbolic alias、memcpy-style region write、atomic/volatile、
  call effects或general points-to MemorySSA。当前等级 I/T。

## 138. F143：Identified-Object Multi-Cell Definedness PHI（2026-07-28）

- 研究问题：F142只在同一base的常量区间或两个不同global之间证明不相交，尚未覆盖
  continuation frame中天然独立的固定栈对象。这会使两个互不别名的static allocas
  因header中的交错访问而相互阻断definedness PHI；
- Object proof：将不同base证明扩展为lowerer能够完整建模的identified objects：
  `GlobalVariable`或entry block中的static `AllocaInst`。两个base必须是不同LLVM
  values，且两侧都属于上述集合；同一base仍回到F142的DataLayout常量区间证明。
  动态alloca、普通pointer argument、无`noalias`证明的call result与symbolic base
  一律保持unknown；
- Arithmetic hardening：固定scalar store size在转换为signed offset前先限制为
  `INT64_MAX`以内，随后才执行半开区间末端加法检查。该顺序避免超大类型宽度转换
  破坏overflow guard；
- Composition：当同一merge block中的load pair通过“不同identified object”路径
  得到不相交结论时，两侧merge除`multicell`外再标记
  `identifiedObjectMultiCell`。Reverse/header scans使用同一证明跨越另一个对象的
  load/store，每个对象的Store/Carry/condition-tree sidecar仍完全独立；
- Artifact：新增
  `bounded-identified-object-multicell-memory-definedness-phi`，并在参与合同写入
  `identified_objects`。Production validator要求该标记蕴含`multicell`，且同一
  block至少存在两个identified-object marked contracts；删除其中一个marker会
  形成孤立合同并被拒绝；
- 自动化证据：函数建立两个独立`[1 x i8]` static allocas，loop header分别load/
  freeze，回边同时写两个独立poison values或carry，执行得到`1,1,2`。F142同一
  aggregate offset0/1正例明确拒绝F143 capability，同cell对照组同时拒绝F142/F143，
  从而覆盖能力层级与反向误报；
- 正确性边界：这是LLVM identified static storage的有界扩展，不是general
  points-to analysis。不同heap allocations、`noalias`参数/返回值、跨函数逃逸对象、
  partial overlap、symbolic offset、region writes、atomic/volatile与unknown call
  effects继续fail closed。当前等级 I/T。

## 139. F144：Fixed-Heap-Object Multi-Cell Definedness PHI（2026-07-28）

- 研究问题：F143覆盖固定栈/全局对象，但continuation已经用F72/F75的site-local
  bounded slot pool执行heap allocations；两个同时存活的固定`malloc` sites也具有
  独立对象身份，仍因交错访问而阻断彼此的definedness PHI是不必要的；
- Allocation proof：只接受direct external `malloc`、单个constant integer size、
  大小非零且不超过`SYMCC_CONTINUATION_MAX_HEAP_OBJECT`、address space 0，并要求
  两个LLVM base values不同。该条件与`layoutHeap`的infallible fixed-capacity路径
  对齐；至少一侧必须是此类heap object，另一侧可以是同类heap或F143 static object；
- Lifetime argument：在LLVM定义良好的执行中，两个同时可访问的malloc返回对象
  生命周期不重叠存储；若对象已经free，继续通过旧pointer访问本身即为UB。Lowerer
  又为不同allocation sites分配不相交的synthetic slot pools，因此numeric memory
  和一位sidecar的对象身份一致。分析不跨越`free`/unknown calls，它们仍是clobber；
- Composition：F142的header/reverse scans使用扩展后的disjoint predicate越过另一
  fixed heap object的scalar load/store。Pair composition在两侧记录
  `fixedHeapObjectMultiCell`，各自的Store/Carry/recursive transfer保持独立；
- Artifact：声明
  `bounded-fixed-heap-object-multicell-memory-definedness-phi`，合同写入
  `fixed_heap_objects`并同时保留`multicell`。Production validator复检父合同及
  同一block至少两个heap-object markers；删除一侧marker被拒绝；
- 自动化证据：entry由两个不同`malloc(1)` sites分配对象并写7，loop header分别
  load/freeze，回边可同时写两个poison values或carry，执行得到`1,1,2`。F143双
  static-alloca正例和F142同aggregate正例均明确拒绝F144 capability，同cell负例
  拒绝全部三层multi-cell capability；同构的dynamic-size双malloc用例保持
  guarded/infeasible且不声明multi-cell capability；
- 正确性边界：不接受zero/dynamic-size malloc、calloc nullable semantics、realloc、
  同一callsite的多个动态实例、custom allocator、noalias参数/返回、跨函数逃逸
  points-to、symbolic base/offset、partial overlap、region/atomic/volatile access。
  因而这是bounded heap pool的专用证明，不是general heap MemorySSA。当前等级 I/T。

## 140. F145：Finite Pointer-Domain Multi-Cell Definedness PHI（2026-07-28）

- 研究问题：F142--F144只处理单一constant base+offset。Lowerer的F73/F74已经能执行
  finite pointer select/PHI union，但definedness reverse scan仍把整个pointer SSA
  当unknown，无法证明两个符号地址域始终不相交；
- Side-effect-free domain extraction：新增只读region collector，递归展开pointer
  `select`与无环pointer `phi`，沿constant `GEPOperator`累积signed 64-bit offset，
  叶子仅接受F143 static base或F144 fixed malloc base。它不调用会写diagnostic的
  lowering provenance builder，因此证明失败不会把原本可保守lower的函数变成错误；
- Budget：每个pointer域去重后最多`SYMCC_LIVE_ALIAS_LIMIT`个regions，默认16；
  两个域最多执行256次pair checks。循环PHI、symbolic GEP、offset算术溢出、null/
  ordinary argument/unknown base、超预算均返回unknown；
- Product proof：至少一侧实际包含select/PHI时才进入F145。对两个region sets做完整
  笛卡尔积；不同已识别base利用F143/F144对象身份，同base利用F142半开常量区间。
  只有每一对都不重叠才允许header/reverse scan跨越另一个访问，不能以concrete
  witness或代表alternative代替全域证明；
- Composition/Artifact：每个symbolic cell继续以原LLVM pointer SSA执行exact
  writer/load匹配和独立edge sidecar。声明
  `bounded-finite-pointer-domain-multicell-memory-definedness-phi`，合同标记
  `finite_pointer_domains`并蕴含`multicell`；production validator要求同一block
  至少两项，删除单个marker被拒绝；
- 自动化证据：pointer A在aggregate offsets `{0,1}`中选择，pointer B在`{2,3}`
  中选择，2x2 product全部不重叠；pointer select和前置diamond生成的两个pointer
  PHIs两种形态均执行得到`1,1,2`。负例把B域改为`{1,2}`，product存在A/B offset1
  重叠，保持guarded/infeasible且不声明任何multi-cell capability；
- 正确性边界：当前product proof故意忽略alternative guards之间的互斥关系。因此
  即使两个重叠regions由同一condition的互斥arms保护也会保守拒绝；该select-guard
  relational case由F146扩展。cyclic pointer PHI fixed point、symbolic index区间、
  partial overlap byte composition、general BasicAA/MemorySSA仍未覆盖。当前等级 I/T。

## 141. F146：Guard-Correlated Pointer-Domain Multi-Cell PHI（2026-07-28）

- 研究问题：F145的笛卡尔积证明忽略control guards。例如A为
  `select(c, offset0, offset1)`、B为`select(c, offset1, offset2)`时，cross product
  包含A.false/B.true的offset1重叠，但该组合要求`c=false ∧ c=true`，不可执行；
- Relational domain：finite region leaf新增一组`(i1 condition, expected polarity)`
  guards。展开select true/false arms时分别追加true/false guard；嵌套时若同一
  condition要求相反极性，该leaf本身不可行并被丢弃。Pointer PHI当前仍不生成关系
  guard，保持F145的保守全对全检查；
- Compatibility proof：对每个空间重叠region pair，比较两侧guard conjunction。
  只有发现同一LLVM condition identity且期望极性相反时才证明该pair不可行并跳过；
  无重叠pair直接通过。任一空间重叠且guards兼容的pair立即使整体证明失败。条件仅
  按SSA identity关联，不用语法相似、concrete witness或未经证明的SMT等价；
- Budget/Composition：沿用F145每域最多16个regions和最多256个pair checks。
  Header/reverse scans仅在完整关系proof通过后跨越另一个access，每个symbolic cell
  仍使用exact pointer SSA writer/load identity和独立edge sidecar；
- Artifact：声明
  `bounded-guard-correlated-pointer-domain-multicell-memory-definedness-phi`，
  合同标记`guard_correlated_pointer_domains`并蕴含`multicell`。它与F145的
  “all spatial pairs disjoint”capability分开，production validator要求同一block
  至少两个markers并拒绝孤立marker；
- 自动化证据：上述A `{0,1}`、B `{1,2}`共享同一condition，唯一cross-product
  overlap由互斥极性排除，loop执行得到`1,1,2`且只声明F146。负例把B true arm改为
  offset0，使A.true/B.true在兼容guards下实际重叠；执行保持guarded/infeasible，
  不声明任何multi-cell capability。F145 select/PHI正例重新生成后仍只声明F145；
- 正确性边界：仅处理相同i1 SSA condition的literal polarity冲突。逻辑互补但SSA
  不同的conditions、PHI edge correlation、path constraints、SMT guard
  unsatisfiability、cyclic pointer domains、symbolic offset interval和partial-byte
  composition继续fail closed。当前等级 I/T。

## 142. F147：PHI-Correlated Pointer-Domain Multi-Cell PHI（2026-07-28）

- 研究问题：两个不同pointer PHIs位于同一merge block时，它们的incoming choices由
  同一条CFG predecessor edge共同决定。F145把所有incoming做cross product，F146
  只认识select condition identity，都会保守包含不可能的跨edge组合；
- Edge relation：region guard新增`(phi merge block, incoming predecessor)`。
  展开每个PHI incoming时记录其实际predecessor；同一merge block要求两个不同
  predecessors的guard conjunction不可同时成立，同一predecessor则兼容。循环PHI
  仍由active-set拒绝；
- Compatibility：空间重叠pair若含同merge/different-predecessor关系可跳过；若含
  same-predecessor重叠则整体失败。Select polarity与PHI edge关系可共存在同一leaf，
  但分别记录F146/F147 capability；
- Artifact：声明
  `bounded-phi-correlated-pointer-domain-multicell-memory-definedness-phi`，合同标记
  `phi_correlated_pointer_domains`并蕴含`multicell`；validator要求同block至少两个
  markers并拒绝孤立marker；
- 自动化证据：diamond中A PHI为left/right offsets `{0,1}`，B为`{1,2}`；唯一空间
  overlap来自A.right/B.left的不同edges，loop执行得到`1,1,2`。将B.left改为offset0
  后A.left/B.left在同edge实际重叠，保持infeasible且不声明multi-cell。F145的
  all-pairs PHI与F146 select正例仍保持原capability；
- 正确性边界：仅关联同一LLVM merge block的直接incoming predecessor identity，
  不推导跨block等价edge、edge predicate SMT关系、循环PHI fixed point、symbolic
  offset区间或partial-byte composition。当前等级 I/T。

## 143. F148：Symbolic-Index Interval Multi-Cell PHI（2026-07-28）

- 研究问题：F145--F147只展开常量offset alternatives，仍拒绝F73已经可执行的
  one-term symbolic GEP；两个索引范围位于不相交子区域时不应互相阻断sidecar；
- Range proof：对`collectOffset`得到的唯一symbolic term调用LLVM 18
  `computeConstantRange(..., ForSigned=true)`，把constant与signed scale应用到
  range端点，并用`__int128`检查乘加溢出。Region由单点扩展为保守
  `[minimumOffset, maximumOffset]`；
- Product：同base regions仅在`maxA + widthA <= minB`或反向条件成立时不相交；
  不要求range精确，过近似仍分离即可。Select/PHI guards可与区间组合，未知/full
  range通常自然导致拒绝；
- Artifact：声明
  `bounded-symbolic-index-interval-multicell-memory-definedness-phi`，合同标记
  `symbolic_index_intervals`并蕴含`multicell`；validator要求同block至少两个marker；
- 证据：`[2 x [2 x i8]]` global中两个GEP分别以常量subarray 0/1为基准，共享
  `and i8 selector,1`索引范围；区间`[0,1]`与`[2,3]`分离，执行得到`1,1,2`。
  两者均指向subarray 0时保持infeasible且不声明capability；
- 边界：只接受一个collectOffset symbolic term、<=64-bit integer range与constant
  scale；多项仿射、跨变量关系、wrap-sensitive精确域、partial-byte composition和
  cyclic MemorySSA继续fail closed。当前等级 I/T。

## 144. F149：Byte-Lane Memory Definedness Composition（2026-07-28）

- 研究问题：F114--F148把一个scalar memory cell的poison definedness表示为一位
  sidecar，并要求reaching writer与load同宽。真实LLVM IR经SROA、memcpy lowering
  或窄字段更新后，经常由多个部分写共同定义一个宽load；继续要求exact full-width
  writer会把可证明的freeze语义退化为eager UB guard；
- Lane domain：对2--8 byte、byte-aligned integer load建立按地址递增的lane vector。
  从load向后沿最多64个basic blocks反向扫描，每个lane绑定第一个覆盖它的非
  atomic/non-volatile scalar store，即该byte的最近写者。相同base使用DataLayout
  constant offset和半开区间交集；已证明不相交的访问可跨越，未知clobber、覆盖缺口、
  多前驱或循环立即返回unknown；
- Initial completion：如果扫描到entry仍有未覆盖lane，只有
  `ConstantFoldLoadFromConst`证明整个typed global load为`ConstantInt`时，才能把
  剩余lane标记为`initial`。Stack/heap未初始化字节、undef/poison initializer和
  internal-callee entry均不推断为defined；
- Definedness transfer：每个可能产生deferred poison且真正成为lane来源的store
  获得独立一位sidecar。宽load的definedness是所有唯一poison-capable source
  sidecars的AND；普通defined writer和initial lane贡献true。后来窄写覆盖旧byte时，
  reverse scan自然选择新writer，未被覆盖的lane仍保留旧writer，因此支持重叠写的
  byte-precise last-writer语义；
- Sink proof：旧store-to-load前向证明现在可以越过已由同一verified lane contract
  解释的部分clobber，并且仍遍历全部successors。任何未被exact或lane contract覆盖
  的潜在consumer都会使deferred propagation失败，不能仅凭一个正例load放宽整条
  store use chain；
- Artifact：函数写入`byte_lane_memory_definedness`，逐lane记录`source`、
  `store`、`store_byte`、`store_bytes`和可选`defined`，store指令带唯一
  `byte_lane_store`及sidecar destination。Capability为
  `bounded-byte-lane-memory-definedness`。Production validator重建完整lane覆盖、
  store宽度/唯一性、紧邻sidecar、load后的AND数据流、`initial`/`cross_block`
  一致性，并要求capability与合同双向对应；
- 自动化证据：两个i8 writers跨block组成i16 load并在poison路径产生`1,2`；一个
  i8 poison writer与global initial byte组成同一load；两个实际defined的nsw writers
  组合后精确返回little-endian `1026`；i16 poison writer被后续i8高字节写部分覆盖，
  lane0/lane1分别绑定旧/新writer并产生`1,2`。F149阶段的分支两臂对照组需要
  lane PHI，因此未声明本capability；该边界随后由F150的独立edge contract覆盖。
  删除一个lane或删除capability均被validator拒绝；
- 验证：LLVM 18 compiler target构建通过，定向五组lower/execute/tamper oracle通过，
  Python unittest `284/284`，LLVM/CMake lit `94/94`；最终完整build、syntax和
  whitespace检查见阶段验证记录；
- 正确性边界：F149是bounded acyclic/single-predecessor byte MemorySSA，不是通用
  MemorySSA。分支来源的per-lane PHI、循环lane carry、symbolic address overlap、
  memcpy/memmove region-source sidecar、atomic/volatile、unknown calls、非byte-aligned
  scalar和超过8 byte的load仍fail closed。控制流lane-PHI由F150继续扩展，F149
  本身仍只声明直线/唯一前驱合同。当前等级 I/T。

## 145. F150：Direct-Predecessor Byte-Lane Definedness PHI（2026-07-28）

- 研究问题：F149能在单一路径组合多个byte writers，但一个wide load位于CFG join时，
  不同predecessor可拥有不同的lane最近写者集合。把所有路径writers无条件AND会让
  未执行路径污染definedness；只选一个代表path又不完备；
- Per-edge reconstruction：load所在block要求2--64个直接predecessors，每个
  predecessor只有merge这一successor。对每条edge分别从endpoint反向扫描，使用
  F149相同的byte-aligned width、constant base+offset、nearest-writer、disjoint
  access和typed global initial规则；每条path必须完整覆盖全部lanes。若未覆盖lane
  需要穿越另一个multi-predecessor join、cycle或unknown clobber，则整个PHI proof
  返回unknown；
- Edge transfer：为每条incoming edge收集该path上唯一的poison-capable writer
  sidecars，在lowerer已有的edge block内做AND并写同一个
  `load__byte_phi_defined` destination。没有poison-capable source的edge写true。
  Numeric memory仍由正常load读取；freeze只读取沿实际edge建立的defined bit，因此
  左右路径writers不会交叉合取；
- Sink closure：raw per-edge candidates在deferred-poison sink分析前先注册
  store-to-load关系；筛选完成后再按F149+F150 accepted contracts重建函数内索引。
  这既允许writer sidecar生成，又防止rejected candidate残留为伪sink；
- Artifact：新增`byte_lane_memory_definedness_phis`，合同包含load、defined、
  merge block、width和2--64个incoming endpoints；每个endpoint记录实际edge block、
  可选initial和完整lane vector。Capability为
  `bounded-byte-lane-memory-definedness-phi`，与F149直线capability分离；
- Validator：逐edge复检lane编号全集、store ID/width/local-byte、initial marker、
  紧邻store sidecar、edge block末端到merge的jump，以及仅由该endpoint source
  sidecars/true构造的AND数据流。所有lane stores必须被F149或F150合同引用；
  capability与至少一个PHI合同双向一致；
- 自动化证据：一条path以poison/defined两个i8 writers组成i16，另一条path以两个
  defined writers组成i16，symbolic edge+freeze执行得到`1,2,2`；两路分别只写低/
  高byte并由global initial补足另一lane时得到`1,1,2,2`。删除incoming lane或删除
  capability均被拒绝。负例使未覆盖lane必须穿越上游multi-predecessor join，保持
  guarded/infeasible且不声明F150；
- 验证：LLVM 18完整build、Python unittest `284/284`、LLVM/CMake lit `94/94`、
  `llvm-as`、Python syntax、正向/initial/negative/tamper与whitespace检查通过；
- 正确性边界：只处理direct-predecessor acyclic lane state，不包含backedge carry、
  conditional store/carry tree、跨join递归source grouping、symbolic overlap、
  region-copy source、atomic/volatile和unknown calls。下一依赖项是bounded cyclic
  byte-lane carry fixed point。当前等级 I/T。

## 146. F151：Cyclic Byte-Lane Definedness PHI（2026-07-28）

- 研究问题：F150在每条直接前驱edge上计算一个aggregate defined bit，但部分写循环
  需要保留“哪些byte仍沿回边继承旧状态”。若只回传aggregate bit，一条低byte poison
  写会错误地把未写高byte也标为poison；若整值carry，又会漏掉低byte的新writer。
  因此循环状态必须提升为每个address-order byte一位的fixed-point vector；
- Candidate proof：仅处理2--8 byte、byte-aligned、non-atomic/non-volatile integer
  load，header有2--64个直接前驱且每个endpoint只有header这一successor。每条edge
  从末端反向执行最多64-block nearest-writer scan：遇到同一header load时，尚未覆盖
  lane标记为`carry`；到函数entry时只允许typed constant global initial补足；已证明
  disjoint的访问可越过，未知clobber、多前驱corridor和活动扫描环立即返回unknown；
- Alias-closed sink：为避免旧store-to-load前向扫描在回边上因cycle失败而被任意绕过，
  F151在整个函数内检查目标region的memory-effect closure。除合同中的writers与目标
  load外，任何可能alias的load/store、volatile/atomic访问或未知memory effect都会
  拒绝candidate。只有通过该证明的store才可用合同记录的目标load作为deferred-poison
  sink；筛选结束后会清空raw索引并从F149--F151 accepted contracts重建；
- Per-lane transfer：每个合同为load建立`bytes`个稳定lane state variables。
  Entry edge把defined/full-width writer lane写为true或对应store sidecar；backedge
  对新写lane写sidecar，对未写lane执行self-identity carry。Header完成numeric load
  后按lane state做完整AND并生成aggregate defined bit，`freeze`仅在该bit为false时
  使用稳定nondeterministic value；
- Artifact：新增`cyclic_byte_lane_memory_definedness_phis`，记录`load`、
  aggregate `defined`、header `block`、`bytes`、有序`lane_defined`变量和2--64个
  incoming edge vectors。每个lane source严格为`store`、`initial`或`carry`；
  capability为`bounded-cyclic-byte-lane-memory-definedness-phi`，与F149直线和
  F150无环merge能力分离；
- Production validator：要求lane state名字非空、唯一且与width一一对应；逐edge
  检查完整lane编号、末端jump、store ID/width/local byte、紧邻sidecar，并要求每个
  lane destination恰好有一次合法identity transfer。`carry`必须读取自身，initial/
  defined store必须写true，poison-capable store必须读取声明sidecar。Header采用
  dependency-set重建AND链，最终aggregate必须依赖全部lane而非任意子集。所有带
  `byte_lane_store`的指令必须被F149--F151某一合同引用，capability与合同双向一致；
- 自动化证据：entry以defined i16初始化两个lanes，第二轮前仅以poison i8覆盖低
  lane，高lane沿回边carry；第一次freeze保留42，第二次freeze生成两种稳定选择，
  最终返回集合`1,2`。负例在回边加入额外aliasing i8 load，alias closure失败，
  producer恢复eager defined-value guard，状态不可行且不声明F151。Checker还把
  `carry`伪造成`initial`以及删除capability，production validator均拒绝；
- 验证：LLVM 18完整build、`llvm-as`、Python unittest `284/284`、LLVM/CMake lit
  `94/94`、Python syntax、正例/负例/metadata tamper/capability tamper、
  完整`live_continuation_lowering.ll`与whitespace检查通过；
- 正确性边界：这是bounded、single-header、alias-closed的per-lane fixed point，
  不是通用MemorySSA。Conditional partial stores、多个循环headers、irreducible
  CFG、symbolic overlap、跨调用memory effects、memcpy/memmove region sources、
  atomic/volatile和超过8 byte的state仍fail closed。下一依赖是conditional
  per-lane carry/source tree，再后续是region-copy definedness summary。当前等级
  I/T。

## 147. F152：Conditional Cyclic Byte-Lane Carry（2026-07-28）

- 研究问题：F151要求回边路径上的partial writer无条件执行。常见loop body却是
  `if (condition) store narrow;`，write/carry两臂先合流到latch，再由唯一backedge
  返回header。若在latch才读取write-arm sidecar，skip路径会引用从未定义的SSA
  local；若直接把latch视为carry，又会漏掉实际执行的writer；
- Strict-diamond proof：只接受一个conditional branch的两个直接successor arms，
  两臂各自只有同一join这一successor且只有该branch这一predecessor。一臂必须含
  唯一、constant base+offset、non-atomic/non-volatile、与2--8 byte load部分重叠的
  scalar writer；另一臂不得含aliasing writer。额外aliasing load、未知memory
  effect、两个writers、full-width writer或非严格CFG均返回unknown。函数级
  alias-closure继续由F151复检整个region；
- Edge-state semantics：lowerer为`store-arm -> join`和`carry-arm -> join`都建立
  synthetic edge block。Store edge对被覆盖lanes写writer sidecar/true，对未覆盖
  lanes写self-carry；carry edge对全部lanes写self-carry。`join -> ... -> header`
  backedge随后只携带已经更新的vector，因此任何路径都不会求值未执行arm的sidecar；
- Artifact：新增
  `conditional_cyclic_byte_lane_memory_definedness_phis`和
  `bounded-conditional-cyclic-byte-lane-memory-definedness-phi`。除F151的header、
  `lane_defined`和incoming vectors外，合同含一个`conditional_transfers`项，记录
  branch、store/carry arms、join、两个synthetic edges、`store_when_true`以及完整
  store/carry lane vector；
- Production validator：先执行F151的seed edge、header complete-AND和incoming
  carry校验，再重建branch true/false拓扑、arm-to-edge jumps、edge-to-join jumps，
  并要求join实际跳向该header的一个all-carry incoming edge。每条lane在两个arm
  edges上都必须恰好赋值一次：store edge读取匹配sidecar/true或自身，carry edge
  必须读取自身。循环至少有一条不含carry的完整seed endpoint，避免首次header
  load引用未初始化lane state；
- 自动化证据：symbolic i1控制write/skip，entry以i16常量初始化，write arm以poison
  i8覆盖低lane，得到状态集合`1,1,2`；skip路径保持42，write路径的第二次freeze
  产生两种稳定结果。负例在carry arm加入额外aliasing load，candidate失败，常量
  true的write路径恢复eager guard并不可行，不声明F152。Checker翻转
  `store_when_true`或删除capability时，production validator均拒绝；
- 验证：LLVM 18完整build、`llvm-as`、Python syntax、Python unittest `284/284`、
  LLVM/CMake lit `94/94`、正例/负例/topology tamper/capability tamper、
  完整`live_continuation_lowering.ll`与whitespace检查通过；
- 正确性边界：当前仅一个direct strict diamond和一个partial writer，不处理
  forwarded arm corridors、多arm switch、两个条件writers、nested/recursive
  condition tree、多个conditional updates、symbolic overlap、region copy或跨调用
  memory effect。下一依赖是forwarded/multiarm conditional lane transfer，再后续
  才是recursive per-lane source tree。当前等级 I/T。

## 148. F153：Forwarded Conditional Cyclic Byte-Lane Carry（2026-07-28）

- 研究问题：编译器CFG清理、coverage instrumentation或手写状态机常在conditional
  branch与实际writer/join之间插入若干无条件blocks。F152要求branch successors
  就是join predecessors，会保守拒绝这些语义等价的forwarded diamonds；
- Corridor proof：从join的两个直接predecessors分别反向沿unique-predecessor、
  unconditional single-successor chain搜索，最多16个blocks；两条path必须遇到同一
  conditional branch的不同successors，block集合互不相交。只接受最内层共同branch，
  遇到nested conditional、多前驱、cycle、非branch terminator或超预算即返回unknown；
- Memory proof：writer scan从单block扩展到整条corridor。两条path中恰有一条含唯一
  constant-offset partial scalar writer，另一条无alias writer；disjoint accesses可
  跨越，alias load/unknown effect继续由arm scan与函数级alias closure拒绝。状态仍在
  两个join-predecessor synthetic edges上更新，因此forwarding blocks不引入未定义
  sidecar读取；
- Artifact：新增
  `forwarded_conditional_cyclic_byte_lane_memory_definedness_phis`与
  `bounded-forwarded-conditional-cyclic-byte-lane-memory-definedness-phi`。
  Transfer在F152字段外增加`store_successor`、`carry_successor`与`forwarded=true`，
  区分branch直接targets和实际arm endpoints；
- Validator：从完整continuation blocks图重建predecessor sets。Header的实际
  predecessors必须与incoming edge合同完全相同；每条corridor从声明successor沿
  最多16个真实jump到endpoint，且每个block只有期望的唯一predecessor；join的实际
  predecessors必须恰为两个synthetic edges。随后复用F152逐lane transfer、seed、
  all-carry backedge和complete header AND证明；
- 自动化证据：write/carry各经过两个forwarding blocks，symbolic write/skip仍得到
  `1,1,2`。负例在write corridor加入第二个conditional与multi-predecessor tail，
  proof拒绝，常量write路径恢复eager guard并不可行。Checker把
  `store_successor`伪造成endpoint或删除capability时，production validator拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、F151--F153定向执行、
  nested negative与corridor/capability tamper通过；Python unittest `284/284`、
  LLVM/CMake lit `94/94`和完整build通过；
- 正确性边界：只允许两条线性corridors、一个conditional update和一个writer。
  Switch/multiarm、两臂都写、多个顺序conditional updates、nested/recursive source
  tree、irreducible CFG、symbolic overlap与region-copy sources仍fail closed。
  下一依赖是multiarm conditional lane transfer。当前等级 I/T。

## 149. F154：Multi-Arm Conditional Cyclic Byte-Lane Carry（2026-07-28）

- 研究问题：一个循环更新常由优先级条件树选择多个窄写入，例如第一臂更新低字节、
  第二臂更新高字节、末臂保持旧值。F152/F153只能表示一个Store/Carry二选一，
  把两个writer折叠成aggregate condition会丢失逐lane来源，也可能在未执行路径读取
  writer sidecar；
- Bounded tree proof：接受恰好三个direct endpoints汇入同一join的严格两级二叉树。
  Root branch的一条边直达root arm，另一条边唯一进入inner branch；inner的true/false
  分别直达另两臂。每个arm只有声明branch这一predecessor和join这一successor，
  inner branch也只能由root进入。三臂中必须恰有两个各含一个constant-offset partial
  scalar writer，第三臂无alias writer；函数级alias closure继续排除额外load/store、
  unknown effect、atomic和volatile访问；
- Lane fixed-point semantics：两个writer arms分别在自己的synthetic edge上把覆盖
  lane写为对应defined sidecar/true，并self-carry其余lanes；纯carry arm对全部lanes
  self-carry。Join之后只转发已完成更新的lane vector，header仍以dependency-checked
  complete AND生成aggregate defined bit。因而三个互斥路径都不求值其他路径的局部
  sidecar；
- Artifact：新增
  `multiarm_conditional_cyclic_byte_lane_memory_definedness_phis`与
  `bounded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi`。一个
  `multiarm_transfers`项记录`root_branch`、`inner_branch`、`join`及三个带
  `root`/`inner_true`/`inner_false` route的arm；每个arm显式记录block、synthetic
  edge与完整store/carry lane vector；
- Production validator：从完整continuation图重建root/inner branch targets、
  inner唯一predecessor、三个arm和edge的唯一predecessor、arm-to-edge与edge-to-join
  jumps以及join的精确三edge predecessor set。它还要求两个不同store IDs、一个
  all-carry arm、每个writer arm同时含store与carry lane、store确实位于对应arm且
  紧邻匹配sidecar，并逐lane核验edge assignment。Join必须流向header的all-carry
  incoming path，且循环必须有完整non-carry seed；
- 自动化证据：两个symbolic i1依次选择低字节poison writer、高字节poison writer
  或carry，稳定freeze的返回多重集为`1,1,1,2,2`。四臂深树超过证明范围时不声明
  capability，常量poison路径恢复eager guard并不可行。Checker伪造重复route或删除
  capability时，production validator拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正例、四臂负例、
  route/capability tamper与whitespace检查通过；Python unittest `284/284`、
  LLVM/CMake lit `94/94`和完整build通过；
- 正确性边界：当前只接受direct、两级、三叶树和两个partial writers。Forwarded
  multiarm corridors、四叶以上recursive tree、顺序多次conditional update、
  switch-chain归一化、symbolic overlap、region-copy来源、跨调用memory effects及
  general cyclic MemorySSA仍fail closed。下一依赖是recursive per-lane source tree，
  然后才是region-copy definedness summary。当前等级 I/T。

## 150. F155：Forwarded Multi-Arm Cyclic Byte-Lane Carry（2026-07-28）

- 研究问题：F154的两级三叶树要求三个branch successors就是join predecessors；
  instrumentation、loop rotation和CFG cleanup通常会在各arm插入若干无条件blocks，
  writer也可能位于corridor中间。只比较endpoint metadata既会漏掉等价程序，也无法
  排除隐藏的二次分支或旁路入口；
- Corridor proof：对join的三个predecessors分别反向沿unique-predecessor、
  unconditional single-successor chain搜索，单臂最多16 blocks，直到最近conditional
  branch。三个corridors必须互不相交；其中两条命中同一inner branch的true/false
  successors，第三条命中root branch的另一个successor。Root的剩余successor必须
  直接且唯一进入inner branch，避免把未经证明的decision corridor混入树结构；
- Memory/state semantics：writer scan覆盖每条corridor的全部blocks，仍要求两个
  corridors各含一个partial writer、第三条无alias writer。Lane state只在三个实际
  endpoint-to-join synthetic edges更新，因此中间块不会提前读取sidecar；函数级
  alias closure继续排除合同之外的潜在alias access和unknown effects；
- Artifact：新增
  `forwarded_multiarm_conditional_cyclic_byte_lane_memory_definedness_phis`与
  `bounded-forwarded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi`。
  F154每个arm增加`successor`，transfer设置`forwarded=true`，从而区分branch target
  与实际join endpoint；
- Production validator：从声明successor沿最多16个真实jump重建每条corridor，
  要求首块predecessor是对应root/inner branch、后续块只有前一块这一predecessor、
  三条path互斥且endpoint跳向自己的synthetic edge。Writer必须真实位于对应corridor；
  随后继续复检三个edge、join精确predecessor set、two-writer/one-carry partition、
  all-carry backedge、seed和header complete AND；
- 自动化证据：low/high/carry三条arms各经两个blocks，writer位于tail，两个symbolic
  条件仍产生`1,1,1,2,2`。负例在low corridor插入nested conditional并让tail拥有
  两个predecessors，proof拒绝，常量poison路径恢复eager guard且不可行。Checker
  把一个`successor`伪造成endpoint或删除capability时，production validator拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正例、nested negative、
  successor/capability tamper与whitespace检查通过；Python unittest `284/284`、
  LLVM/CMake lit `94/94`和完整build通过；
- 正确性边界：decision root-to-inner仍必须direct；只接受三叶、两个writers和线性
  arm corridors。Forwarded decision corridor、四叶以上recursive tree、顺序多次
  updates、switch、symbolic overlap、region copy、跨调用effects与irreducible CFG
  仍fail closed。下一依赖是bounded recursive per-lane source tree。当前等级 I/T。

## 151. F156：Recursive Conditional Cyclic Byte-Lane Carry（2026-07-28）

- 研究问题：优先级更新链和lowered switch常产生四个以上的writer/carry leaves；
  为每一种固定形状增加特殊规则不可扩展。所需抽象不是在join构造会求值全部writer
  sidecars的巨大ITE，而是把控制树证明与leaf-local lane state transfer分离；
- Recursive tree proof：对拥有4--8个direct predecessors的join，枚举conditional
  root并递归跟随true/false successors。每个内部节点必须是二路conditional branch，
  每个child只能由该parent到达；所有路径必须恰好覆盖join的全部leaf predecessors，
  无共享leaf、cycle或额外出口。树深上限6、总branch+leaf上限64，且只能有一个满足
  完整partition的root；
- Source proof：每个leaf只能包含至多一个constant-offset partial scalar writer；
  至少两个leaves写入、至少一个leaf纯carry，且writer StoreInst两两不同。每个writer
  leaf的未覆盖lanes carry，纯carry leaf的全部lanes carry。函数级alias closure继续
  排除树外的潜在alias access和unknown memory effect；
- Artifact：新增
  `recursive_conditional_cyclic_byte_lane_memory_definedness_phis`与
  `bounded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`。
  `recursive_transfers`记录root、join、depth、完整branch节点`block/true/false`和
  leaf节点`block/edge/lanes`，避免仅凭扁平source列表猜测层次；
- Production validator：核验4--8 leaves、`branches = leaves - 1`、depth 2--6，
  并从真实branch terminators递归重建reachability、唯一parent、无共享/无环和完整
  node partition；实际最大深度必须等于声明。每个leaf-to-edge-to-join关系、join
  精确predecessor set、writer所在leaf与sidecar、逐lane assignment、至少two-store/
  one-carry partition、all-carry backedge、seed及header complete AND均独立复检；
- 自动化证据：深度3的四叶优先级树含low/high/low三个独立poison writers和一个
  carry leaf，三个symbolic条件产生`1,1,1,1,2,2,2`。负例让一个leaf多出不可达
  predecessor，tree proof拒绝，常量poison路径恢复eager guard并不可行。Checker
  篡改depth或删除capability时，production validator拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、四叶正例、shared-leaf
  negative、depth/capability tamper与whitespace检查通过；Python unittest
  `284/284`、LLVM/CMake lit `94/94`和完整build通过；
- 正确性边界：leaves必须direct，writers不可重复或分组，carry leaf可以有多个但
  当前测试覆盖一个；不支持forwarded recursive arms、leaf group/repeated source、
  sequential updates、symbolic overlap、region copy、跨调用effects、irreducible
  或超过预算的树。下一依赖是forwarded recursive lane tree，再处理grouped/repeated
  lane sources。当前等级 I/T。

## 152. F157：Forwarded Recursive Cyclic Byte-Lane Carry（2026-07-28）

- 研究问题：F156仍要求branch successors直接命中下一个branch或leaf，而真实CFG会在
  decision-to-decision和decision-to-leaf edges上插入无条件blocks。只扩展leaf arms
  不足以覆盖loop rotation产生的decision corridor；
- Recursive corridor proof：每个conditional edge从实际successor开始，沿
  unique-predecessor、unconditional single-successor chain前进，最多16 blocks，
  直到下一个声明branch或leaf。所有intermediate blocks全树唯一，不能被另一条edge
  复用；terminal仍参与F156的unique-parent、acyclic、complete leaf partition和
  depth<=6证明。总branch、leaf与intermediate blocks不超过64；
- Writer/state proof：leaf writer scan覆盖到该leaf的完整terminal corridor；
  decision corridors若含潜在alias access会因函数级alias closure被拒绝。Lane state
  仍只在实际leaf endpoint-to-join edge更新，不在decision corridor构造会触及局部
  sidecar的select；
- Artifact：新增
  `forwarded_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`与
  `bounded-forwarded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`。
  Transfer设置`forwarded=true`，每个leaf增加branch-edge `successor`；branch节点的
  true/false保留实际corridor入口；
- Production validator：从每个真实branch target沿最多16个jump重建terminal，
  逐块要求精确唯一predecessor并维护全树corridor-membership set；重新计算branch
  depth和完整node partition，再要求leaf声明successor等于实际corridor首块。
  Writer可位于leaf corridor任意块，但不能出现在其他corridor；随后复检所有leaf
  edges、join、lane assignments、seed与header AND；
- 自动化证据：四叶树的root-to-inner decision edge及四个leaf arms都加入forwarding
  blocks，三个symbolic条件仍得到`1,1,1,1,2,2,2`。负例在writer corridor插入nested
  branch并形成multi-predecessor tail，proof拒绝且常量poison路径不可行。Checker
  把leaf `successor`伪造成endpoint或删除capability时，production validator拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、forwarded正例、nested
  negative、successor/capability tamper与whitespace检查通过；阶段全量回归为
  Python unittest `284/284`、LLVM/CMake lit `94/94`；
- 正确性边界：仍要求严格二叉树、4--8 leaves、depth<=6、distinct leaf writers，
  不支持shared/grouped/repeated writer sources、顺序updates、switch-specific
  semantic grouping、symbolic overlap、region copy或跨调用effects。下一依赖是
  grouped/repeated recursive lane sources。当前等级 I/T。

## 153. F158：Grouped Recursive Cyclic Byte-Lane Source（2026-07-28）

- 研究问题：一个partial writer可能在内部conditional node的terminator之前执行，
  随后多个控制leaves共享该定义。F156要求writer位于leaf，因而拒绝这种
  store-then-fanout形状；简单复制store ID又无法证明每个引用leaf都真实执行过writer；
- Group proof：在F156 direct tree上允许恰好一个internal branch block含唯一partial
  writer。沿严格unique-parent tree反向检查，该block必须是至少两个leaves的结构祖先；
  这些descendant leaves不得再含自己的alias writer。树外至少还有一个独立leaf
  writer和一个carry leaf，使合同包含至少两个真实source stores并保留loop carry；
- State semantics：group writer执行后立即产生一次defined sidecar，所有且仅有其
  descendant leaves在各自edge transfer中读取该sidecar；未覆盖lanes继续self-carry。
  因sidecar定义block结构支配整组leaves，不会出现未执行路径读取局部值；
- Artifact：新增
  `grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`与
  `bounded-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`，
  transfer设置`grouped=true`。同一store ID在组内多个leaf lane vectors中重复出现；
- Production validator：先从真实CFG重建完整树与`branch -> terminal children`
  关系，再为每个store ID收集引用leaf集合。必须恰有一个重复source，其实际store
  block是声明branch node，且该branch递归可达的完整descendant-leaf集合必须与引用
  集合完全相等；其他sources只能leaf-local且各出现一次。随后复检sidecar、lane
  transfers、carry leaf、join、seed和header AND；
- 自动化证据：root选择一个含low-byte poison writer的group branch，该branch的
  两个leaves共享writer；另一子树含high-byte独立writer和carry，三个symbolic条件
  得到`1,1,1,1,2,2,2`。负例在两个internal branches各放一个group writer，
  compiler proof拒绝，常量第一组poison路径不可行。Checker清除`grouped` marker或
  删除capability时，production validator拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、grouped正例、
  two-group negative、marker/capability tamper与whitespace检查通过；阶段全量
  回归为Python unittest `284/284`、LLVM/CMake lit `94/94`；
- 正确性边界：只允许一个group writer，group tree必须direct且组内leaves不得有
  override writer。不支持多个disjoint groups、nested overwrite、forwarded group、
  repeated leaf source位置、多carry group、symbolic overlap、region copy或跨调用
  effects。下一依赖是repeated-source recursive lane transfer。当前等级 I/T。

## 154. F159：Repeated-Source Recursive Cyclic Byte-Lane Transfer（2026-07-28）

- 研究问题：F158要求group writer覆盖其全部descendant leaves；实际树中部分leaves
  会用更近writer覆盖同一lane region，使group source只出现在其余多个位置。若把
  override leaf简单视为全新source但不检查region，会错误丢失group writer覆盖而
  override未覆盖的lanes；
- Nearest-source proof：保留一个internal group writer，允许至少一个descendant leaf
  含自己的writer。Override必须与group writer具有相同canonical base、constant
  offset和byte width，确保它完全替换group定义的lane集合；至少两个其他descendant
  leaves仍引用group source。树外保留carry leaf，全部writers仍受alias closure；
- State semantics：未override leaves在edge上读取internal sidecar，override leaves
  读取自己的更近sidecar，其他lanes self-carry。因此同一个group store ID可以在树中
  多次出现，而override positions使用不同ID，不需要在join求值路径条件ITE；
- Artifact：新增
  `repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  与
  `bounded-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`，
  transfer设置`repeated_source=true`；
- Production validator：识别唯一重复store ID及其internal branch block，计算完整
  descendant set并取`descendants - repeated-reference-leaves`作为override positions。
  该集合必须非空且每个位置引用不同leaf-local writer；validator还比较group与所有
  override leaves的store-lane index集合，保证覆盖范围一致。其他sources只能出现
  一次，随后复检sidecar、edges、join、seed与header AND；
- 自动化证据：五叶树中low-byte internal source出现在两个leaves，第三个descendant
  以同范围low-byte writer覆盖，另一子树含high-byte writer与carry；四个symbolic
  条件得到`1,1,1,1,1,2,2,2,2`。负例用high-byte writer覆盖low-byte group，
  proof拒绝且常量poison路径不可行。Checker清除`repeated_source`或删除capability
  时，production validator拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、repeated-source正例、
  mismatched-override negative、marker/capability tamper与whitespace检查通过；
  阶段全量回归为Python unittest `284/284`、LLVM/CMake lit `94/94`；
- 正确性边界：只允许一个internal source group及same-range leaf overrides；
  不支持不同lane的partial composition、多个groups、forwarded repeated tree、
  同一source的非支配位置、多carry group、region copy、symbolic overlap或跨调用
  effects。下一依赖是per-lane override composition，再处理multi-carry groups。
  当前等级 I/T。

## 155. F160：Composed Repeated-Source Recursive Byte-Lane Transfer（2026-07-28）

- 研究问题：F159把一个override leaf整体归给internal source或same-range local
  writer，不能表达更近writer只覆盖group region的一部分。直接用leaf级source会
  丢失未覆盖lane的上游definedness；错误地把全部lanes绑定到`leaf.store`还会在
  edge上读取错误sidecar；
- Per-lane nearest-source proof：仍只接受一个direct strict recursive group。
  Leaf-local writer必须与internal writer的constant byte interval真实相交，但不得
  完全遮蔽它；逐lane优先选择leaf-local writer，其次选择结构祖先internal writer，
  最后self-carry。至少一个composed leaf必须同时保留两个store IDs，且至少两个
  pure group leaves保持完整internal lane set；same-range overrides继续归F159，
  exact与partial override混用暂时拒绝；
- Compiler implementation：`RecursiveCyclicByteLaneTransfer`新增
  `composedRepeatedSource`。Recursive alias closure、candidate poison detection、
  store-ID/defined-sidecar注册、multi-access统计与edge lowering全部从
  leaf-level `store`迁移到实际`lane.store`，从而让同一leaf安全读取两个不同的
  path-dominating sidecars；
- Artifact：新增
  `composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  与
  `bounded-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`，
  transfer设置`composed_repeated_source=true`；
- Production validator：除重建完整branch tree、edge、join和sidecar外，为每个
  `(store ID, leaf)`独立重建lane set。唯一internal repeated source必须位于branch；
  至少两个pure leaves具有相同完整group lane set。每个composed leaf恰有一个
  leaf-local source，该source与group set相交，group在该leaf中的lane set必须精确
  等于`group_set - local_set`且非空；全部descendants仍引用internal source，
  其他writers只能在单一leaf corridor出现；
- 自动化证据：i32循环中internal i16 poison writer覆盖lanes 0--1，override leaf
  的i8 writer只覆盖lane 1，artifact确认该leaf为`internal, override, carry, carry`。
  两个pure group leaves、一个独立lane-2 writer与一个carry leaf得到
  `1,1,1,1,1,2,2,2,2`。负例让i16 local writer完全遮蔽lane-1 group source，
  proof拒绝、不声明F160且常量poison路径不可行；marker/capability tamper均由
  production validator拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、F158/F159兼容回归、
  F160正负定向执行与`git diff --check`通过；全量回归将在本阶段收尾同步；
- 正确性边界：只支持一个direct group、一个leaf-local writer/override leaf和
  constant byte intervals；不支持完全遮蔽、exact/partial混合、forwarded group、
  多internal groups、symbolic overlap、region copy、unknown calls、atomic或
  volatile effects。下一依赖是recursive byte-lane multi-carry source groups。
  当前等级 I/T。

## 156. F161：Multi-Carry Composed Recursive Byte-Lane Tree（2026-07-28）

- 研究问题：F156--F160只要求“至少一个”pure carry leaf，artifact不能区分多个互斥
  no-write控制位置，也无法检测producer伪造carry位置数量。多个carry leaves不是
  多个lane carry variables，而是同一完整lane state在树中多个不同控制叶上的
  identity transfer；
- Scope：F161建立在F160 composed repeated-source proof上，允许2--8个pure
  all-carry leaves。树仍为4--8 leaves、depth<=6、64 nodes的direct strict binary
  tree；每个carry leaf必须有唯一branch parent和独立leaf-to-join synthetic edge；
- Compiler/artifact：`RecursiveCyclicByteLaneTransfer`记录`multiCarry`和
  `carryLeaves`，候选从无store的完整lane vectors精确计数。新增
  `multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  与
  `bounded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`；
  transfer同时设置`composed_repeated_source=true`、`multicarry=true`与
  `carry_leaves=N`；
- Production validator：从每个leaf lane vector独立重算all-carry leaf数量，
  要求声明值为2--8且精确相等；非multi F160合同规范化为恰好一个pure carry leaf，
  防止把同一producer artifact在两个能力类别之间任意移动。完整tree reachability、
  unique predecessor、join edge集合、composed store partition、seed和header AND
  继续全部复检；
- 自动化证据：六叶树包含两个pure group leaves、一个composed override leaf、
  一个独立writer leaf和两个pure carry leaves，五个symbolic条件得到
  `1,1,1,1,1,1,2,2,2,2`。负例让carry branch的true/false共享同一leaf，
  unique-parent proof拒绝且常量poison路径不可行；checker修改`carry_leaves`或删除
  capability时validator拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正负定向执行、
  count/capability tamper与`git diff --check`通过；全量回归将在阶段收尾同步；
- 正确性边界：F161当前只扩展F160 direct composed tree，不声明一般recursive、
  grouped或same-range repeated树的multi-carry能力；不支持shared carry node、
  forwarded tree、多个internal groups、symbolic overlap、region copy、calls、
  atomics或volatile effects。下一依赖是multiple internal source groups。
  当前等级 I/T。

## 157. F162：Multiple Internal Recursive Byte-Lane Groups（2026-07-28）

- 研究问题：F158--F161只允许一个internal writer group。真实控制树可在两个互不
  相交的子树中分别执行不同partial writer；把它们扁平化为leaf-local copies会丢失
  source identity，也无法证明sidecar在所有引用位置均已执行；
- Scope/proof：允许恰好两个internal branch writers。每个writer必须是至少两个
  leaves的结构祖先，且其完整descendant-leaf集合必须全部且仅引用该source。两个
  descendant sets必须不相交，组内不允许leaf-local override；树外至少有一个pure
  carry leaf。Forwarded、nested groups和第三个internal writer继续拒绝；
- Compiler implementation：internal writer discovery从单值扩展为最多两个
  `GroupWriter`记录。逐leaf沿unique-parent chain查找ancestor group；同时命中两个
  group即表示嵌套并拒绝。每组独立计数pure leaves，要求均至少2；multi-group合同
  只允许两个实际store IDs，不能混入leaf-local writer；
- Artifact：新增
  `multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`与
  `bounded-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`，
  transfer设置`multiple_groups=true`、`group_count=2`；
- Production validator：识别恰好两个重复store IDs，验证两者实际store blocks均为
  声明branch，分别递归计算完整descendant sets并与引用leaf sets精确相等；两个集合
  必须disjoint，每个leaf只能引用所属组source，各组所有leaves的lane set一致，
  且不存在第三个store ID。Tree、edge、sidecar、carry、seed与header AND仍独立复检；
- 自动化证据：i16循环中第一个internal group写low byte并有两个leaves，第二个
  disjoint group写high byte并有两个leaves，另有carry leaf；四个symbolic条件得到
  `1,1,1,1,1,2,2,2,2`。负例把第二组嵌套到第一组，使其leaves受两个writers支配，
  proof拒绝且常量poison路径不可行；修改`group_count`或删除capability均被拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正负定向执行、
  count/capability tamper与`git diff --check`通过；全量回归将在阶段收尾同步；
- 正确性边界：恰好两个direct disjoint groups，组内无override；不支持grouped+
  composed混合、nested groups、forwarding、三个以上groups、symbolic overlap、
  region copy、calls、atomics或volatile effects。下一依赖是bounded mixed
  multi-group composition，然后才是general cyclic MemorySSA。当前等级 I/T。

## 158. F163：Mixed Multi-Group Recursive Byte-Lane Composition（2026-07-28）

- 研究问题：F162的两个groups都必须为pure complete-descendant sources，不能同时
  表达F160式partial override。直接放宽到任意override会使每组source partition、
  local source identity和per-lane nearest definition相互混淆；
- Scope：保留两个direct disjoint internal groups，允许恰好一个group中的恰好一个
  descendant leaf含partial overlapping local writer。该组仍需至少两个pure leaves；
  另一个group全部leaves保持pure。Local writer必须保留至少一个internal lane，
  不允许same-range或full-shadow override；
- Compiler implementation：双组候选允许最多一个`candidateComposition`，并要求
  source集合恰为两个internal writers加一个local writer。Artifact新增
  `mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  与
  `bounded-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`，
  transfer设置`multiple_groups=true`、`group_count=2`、`mixed_groups=true`和
  `composed_group_count=1`；
- Production validator：先识别两个重复internal IDs及disjoint descendant sets。
  对每组从pure leaves恢复完整group lane set；恰好一个group允许一个双source leaf，
  其local source必须leaf-local且唯一、与group set相交，保留的internal lanes必须
  精确为`group_set - local_set`且非空。全合同store IDs必须等于两个internal加该
  唯一local ID；
- 自动化证据：i32树中第一组i16 writer覆盖lanes 0--1并在一个leaf由lane-1 i8
  writer部分覆盖，第二组i8 writer覆盖lane 2；两组各保留两个pure leaves，另有
  carry leaf。五个symbolic条件得到`1,1,1,1,1,1,2,2,2,2,2`。负例让local i16
  writer完全遮蔽第一组i8 source，proof拒绝且常量poison路径不可行；
  `composed_group_count`与capability tamper均拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、F162兼容回归、F163
  正负定向执行与`git diff --check`通过；全量回归将在阶段收尾同步；
- 正确性边界：两个direct groups、仅一个partial override，不支持两组同时override、
  exact/partial混合、forwarded corridors、nested groups、第三组、symbolic overlap、
  region copy、calls、atomics或volatile effects。下一依赖是forwarded group trees。
  当前等级 I/T。

## 159. F164：Forwarded Grouped Recursive Byte-Lane Source（2026-07-28）

- 研究问题：F158 grouped source要求direct branch-to-child edges；loop rotation与
  block splitting会在group root、decision和leaf edges插入无条件blocks。只接受
  direct tree会丢失已由F157证明安全的CFG形状；
- Proof composition：只对“单internal group、无override”组合F157与F158。
  每条branch edge仍最多16个unique-predecessor unconditional blocks，全树最多64
  nodes；`treeAncestor`沿实际唯一前驱链证明group writer支配所有descendant leaves，
  writer/alias扫描覆盖leaf terminal corridors；
- Artifact：新增
  `forwarded_grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  与
  `bounded-forwarded-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`，
  transfer同时设置`forwarded=true`、`grouped=true`；
- Validator：以F157逐jump恢复全部decision/leaf corridors、唯一predecessors、
  disjoint block membership与真实depth，再执行F158的唯一重复store ID、internal
  branch位置及完整descendant-set相等证明；两个marker必须与artifact类别一致；
- 自动化证据：root-to-group/root-to-other decision edges和四条leaf edges均含
  forwarding blocks，low-byte group writer、high-byte leaf writer和carry执行得到
  `1,1,1,1,2,2,2`。负例在group leaf corridor加入nested branch并形成
  multi-predecessor endpoint，proof拒绝且常量poison路径不可行；forwarded marker与
  capability tamper均拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正负定向执行与
  `git diff --check`通过；全量回归将在阶段收尾同步；
- 边界：不支持forwarded repeated/composed/multigroup sources、nested branch、
  shared corridor、超过16-block edge、symbolic overlap、region copy、calls、
  atomics或volatile effects。下一依赖是forwarded same-range repeated source。
  当前等级 I/T。

## 160. F165：Forwarded Repeated-Source Recursive Byte-Lane Transfer（2026-07-28）

- 研究问题：F159只接受direct recursive tree，因此LLVM block splitting产生的
  unconditional forwarding blocks会使已证明的same-range leaf override退化为
  unsupported；简单把`forwarded` marker附到F159合同又无法证明corridor未分支或
  汇合；
- Proof composition：只组合F157与F159，不放宽source语义。每条tree edge最多跨越
  16个unique-predecessor jump blocks，全树/走廊预算64 nodes；唯一internal writer
  仍至少被两个pure descendant leaves引用，exact override leaves必须使用相同
  base、offset、width与lane set，且树外保留独立writer和carry；
- Artifact与compiler：新增
  `forwarded_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  与
  `bounded-forwarded-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`。
  Compiler只在`tree.forwarded && repeatedSource`时选择该类别，transfer同时声明
  `forwarded=true`、`repeated_source=true`，不会误发布旧F159或F164 capability；
- Production validator：先逐jump恢复全部decision/leaf corridors、实际parent、
  depth和disjoint membership，再重算唯一重复store ID、internal branch位置、
  pure group lane set、完整descendant set与exact override集合；两个marker都必须
  与artifact类别精确一致；
- 自动化证据：五叶i16循环的root、group-inner、override、other-writer与carry
  edges均经过forwarding blocks；两个pure group leaves、一个same-range override、
  一个high-byte writer和carry得到`1,1,1,1,1,2,2,2,2`。负例把group corridor
  改为nested branch并让terminal具有多个predecessors，lowering fail-closed且常量
  poison路径infeasible；分别篡改`forwarded`、`repeated_source`或删除capability
  均被拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正负定向执行、双marker
  与capability tamper、`git diff --check`通过；全量回归将在阶段收尾同步；
- 边界：只支持same-range exact override；forwarded partial composition、
  multi-carry、multiple groups、shared/branching corridor、symbolic overlap、region
  copy、calls、atomics与volatile effects仍拒绝。下一依赖是F166 forwarded
  per-lane composed source。当前等级 I/T。

## 161. F166：Forwarded Composed Repeated-Source Byte-Lane Transfer（2026-07-28）

- 研究问题：F165只证明same-range leaf replacement，无法表达F160中一个leaf同时从
  internal writer、部分相交的local writer与carry取不同lanes的情形；forwarding
  又使leaf-local writer membership不能靠直接前驱判断；
- Scope与proof：组合F157 corridor proof和F160 per-lane composition，且本阶段要求
  恰好一个pure carry leaf。Compiler沿unique-parent corridor寻找结构祖先writer，
  leaf-local writer必须位于恢复出的terminal corridor；每lane仍选择最近的local
  source、未覆盖的internal source或carry。至少两个pure leaves恢复完整group set，
  composed leaf必须保留非空`group_set - local_set`；
- Artifact：新增
  `forwarded_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  和
  `bounded-forwarded-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`。
  Transfer同时声明`forwarded=true`与`composed_repeated_source=true`；多carry树不会
  被误归入本类别；
- Production validator：先验证每条最多16-block、全树最多64-node的真实jump
  corridors、unique predecessors、tree depth与leaf edge，再从sidecar逐store/leaf
  重建lane sets，证明internal writer的完整descendant集合、pure set一致性、local
  source leaf-locality及精确集合差；
- 自动化证据：i32五叶树中i16 internal source覆盖lanes 0--1，composed leaf的
  lane-1由更近i8 writer覆盖，另有lane-2 writer与pure carry；forwarded执行得到
  `1,1,1,1,1,2,2,2,2`。Branching corridor与multi-predecessor endpoint负例
  infeasible；`forwarded`、`composed_repeated_source`及capability tamper均拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正负定向执行、双marker
  与capability tamper、`git diff --check`通过；全量回归将在阶段收尾同步；
- 边界：单个composed group、恰好一个carry leaf；forwarded multi-carry、
  multiple groups、多个composed leaves、shared/branching corridors、symbolic
  overlap、region copy、calls、atomics和volatile effects仍拒绝。下一依赖是
  forwarded multigroup tree。当前等级 I/T。

## 162. F167：Forwarded Multi-Group Recursive Byte-Lane Tree（2026-07-28）

- 研究问题：F162的双group proof依赖direct tree adjacency，而常见CFG规范化会在
  group入口、decision与leaf edges插入jump blocks；若只验证flattened leaf列表，
  两个internal writers可能被错误关联到重叠或不完整的descendant partitions；
- Scope：组合F157与F162，仍只接受恰好两个pure、direct-by-reconstructed-tree、
  互不嵌套的groups；每组至少两个leaves且lane set一致，树外至少一个carry。
  本阶段不允许任何local override；
- Artifact与compiler：新增
  `forwarded_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  与
  `bounded-forwarded-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`。
  Transfer同时声明`forwarded=true`、`multiple_groups=true`、`group_count=2`；
- Validator：先按每edge最多16 jumps、全树64 nodes预算恢复完整唯一前驱树，再识别
  两个重复store IDs；分别要求store block为声明branch、完整descendant set等于
  source references、两个member sets disjoint、组内lane sets相同且合同无第三
  source。`forwarded`、`multiple_groups`与count均类别绑定；
- 自动化证据：i16五叶树中low/high byte两个internal writers各支配两个leaves，
  root、group与leaf edges均含forwarding blocks，另有carry，执行得到
  `1,1,1,1,1,2,2,2,2`。负例在第一组leaf corridor加入branching detour形成
  multi-predecessor terminal，lowering拒绝且poison路径infeasible；marker、
  `group_count`和capability tamper均拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正负定向执行、
  marker/count/capability tamper、`git diff --check`通过；全量回归待阶段收尾；
- 边界：两个pure groups；forwarded mixed overrides、nested/third groups、
  shared/branching corridors、symbolic overlap、region copy、calls、atomics和
  volatile effects仍拒绝。下一依赖是F168 forwarded mixed multigroup。当前等级
  I/T。

## 163. F168：Forwarded Mixed Multi-Group Byte-Lane Composition（2026-07-28）

- 研究问题：F167只允许pure groups，不能表达F163中“一个group含唯一partial
  override、另一个group保持pure”的多源leaf；需要同时维护corridor reachability、
  两组descendant partitions和composed leaf的per-lane差集；
- Scope：组合F157、F160与F163。恰好两个disjoint internal groups，其中恰好一个
  group含一个双source leaf；该组仍至少两个pure members，另一组全部pure。合同
  source集合恰为两个internal writers与一个leaf-local writer；
- Artifact与compiler：新增
  `forwarded_mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  及
  `bounded-forwarded-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`。
  Transfer绑定`forwarded=true`、`multiple_groups=true`、`group_count=2`、
  `mixed_groups=true`与`composed_group_count=1`；
- Validator：先恢复bounded unique-predecessor tree，再分别重建两组完整lane sets。
  唯一composed leaf的local source必须只在该leaf出现、与group set相交，保留的
  internal lanes必须精确等于非空`group_set - local_set`；两个member sets仍
  disjoint且分别等于对应branch的完整descendants；
- 自动化证据：i32六叶树中第一组i16 source覆盖lanes 0--1并由lane-1 local writer
  部分覆盖，第二组i8 source覆盖lane 2，另有carry；group-entry corridor存在时
  执行得到`1,1,1,1,1,1,2,2,2,2,2`。负例把该corridor改成branching merge，
  proof拒绝且poison路径infeasible；`forwarded`、`mixed_groups`、count与capability
  tamper均拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正负定向执行、
  marker/count/capability tamper和`git diff --check`通过；全量回归待阶段收尾；
- 边界：两个groups且仅一个composed group/leaf；forwarded multi-carry、两组同时
  override、三个以上groups、shared/branching corridors、symbolic overlap、region
  copy、calls、atomics与volatile effects仍拒绝。下一依赖是F169 forwarded
  multi-carry composition。当前等级 I/T。

## 164. F169：Forwarded Multi-Carry Composed Byte-Lane Tree（2026-07-28）

- 研究问题：F166将forwarded composed tree规范为一个carry leaf，无法覆盖F161的
  多个独立carry alternatives；若只沿用`has_carry`布尔量，shared leaf或伪造计数
  会破坏condition-tree partition的可审计性；
- Scope：组合F157、F160与F161。单个composed group保留至少两个pure leaves和
  per-lane partial override，另有2--8个distinct all-carry leaves；每个carry
  terminal仍满足unique-parent与独立edge条件；
- Artifact与compiler：新增
  `forwarded_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  与
  `bounded-forwarded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`。
  Transfer绑定`forwarded=true`、`composed_repeated_source=true`、
  `multicarry=true`和精确`carry_leaves=N`；
- Validator：在恢复每条bounded jump corridor及完整tree后，从每个完整lane vector
  重算all-carry leaves，要求2--8范围内且与声明精确相等；同时复用F160的pure group
  set、leaf-local source及非空difference证明。类别marker、count与capability均
  双向绑定；
- 自动化证据：i32六叶树含两个pure group leaves、一个composed leaf、一个独立
  writer和两个carry leaves；group-entry corridor存在时得到
  `1,1,1,1,1,1,2,2,2,2`。负例把corridor改为branching merge并形成多前驱
  terminal，proof拒绝且poison路径infeasible；`forwarded`、`multicarry`、count与
  capability tamper均拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正负定向执行、
  marker/count/capability tamper与`git diff --check`通过；全量回归待阶段收尾；
- 边界：单个composed group与2--8 carry leaves；多个groups同时composition、
  三组以上、shared/branching corridors、symbolic overlap、region copy、calls、
  atomics及volatile effects仍拒绝。下一步审计bounded multi-group generalization
  与general cyclic MemorySSA的证明成本。当前等级 I/T。

## 165. F170：Three-Group Recursive Byte-Lane Tree（2026-07-28）

- 研究问题：F162/F167把internal source partition固定为两组；在现有8-leaf预算内，
  三个互不嵌套groups仍可形成有限、可完全枚举的结构，但需要避免只检查前两组而
  漏掉第三组重叠；
- Scope：direct strict binary tree中恰好三个internal writers，每组至少两个pure
  descendant leaves，三个member sets两两disjoint，树外至少一个carry。由此最小
  形状是7 leaves/6 branches，仍满足4--8 leaves与depth<=6预算；不允许local
  overrides或forwarding；
- Compiler与artifact：internal group discovery上限由2扩为3，但三组类别要求
  `writers.size()==3`、每组pure count>=2、无exact/partial overrides。新增
  `trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`及
  `bounded-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`，
  transfer声明`multiple_groups=true`、`triple_groups=true`、`group_count=3`；
- Validator：从三个重复store IDs恢复各自branch与完整descendant sets，执行所有
  pairwise disjoint检查，要求合同source IDs精确等于三组writers、每组lane set
  一致、每个leaf只引用所属组；marker与count必须与类别精确一致；
- 自动化证据：i32七叶树的三个i8 internal writers分别覆盖lanes 0、1、2，各有
  两个leaves，另有carry；六个symbolic conditions得到七个`1`和六个`2`共十三
  状态。负例把第二组嵌入第一组，使其leaves同时受两个writers支配，compiler
  fail-closed且poison路径infeasible；`triple_groups`、`group_count`与capability
  tamper均拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正负定向执行、
  marker/count/capability tamper、`git diff --check`通过；全量回归待阶段收尾；
- 边界：恰好三个direct pure groups；forwarding、group-local override、四组以上、
  nested groups、symbolic overlap、region copy、calls、atomics及volatile effects
  拒绝。下一依赖是双composed groups。当前等级 I/T。

## 166. F171：Double-Composed Multi-Group Byte-Lane Tree（2026-07-28）

- 研究问题：F163/F168只允许两个groups中的一个含partial override。仅把全局
  `composedOverrideCount`上限改成2并不充分，因为两个overrides可能都落在同一组，
  另一组没有对应的composed proof；
- Scope：direct two-group tree中，每组恰好一个composed leaf且至少两个pure leaves，
  两个local writers各自leaf-local并与所属group相交，均保留非空internal lanes；
  树外至少一个carry；
- Compiler与artifact：新增per-group composed counters，要求向量精确为`[1,1]`、
  source集合为两个internal加两个local writers。新增
  `double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  与
  `bounded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`；
  transfer声明`multiple_groups=true`、`group_count=2`、`mixed_groups=true`、
  `double_composed_groups=true`和`composed_group_count=2`；
- Validator：分别从两组pure leaves恢复完整lane set，要求每组恰好一个composed
  member；两个local IDs必须distinct且各自只在对应leaf出现，每个internal set
  精确等于`group_set - local_set`并非空。完整source IDs必须等于两组internal与
  两个local的并集；
- 自动化证据：i32七叶树的两个i16 groups分别覆盖lanes 0--1与2--3，每组由一个
  i8 local writer覆盖后一lane并保留两个pure leaves，另有carry；六个条件得到
  十三状态。负例把两个local leaves都置于第一组，尽管全局count仍为2，per-group
  `[2,0]` proof拒绝且poison路径infeasible；double marker、count与capability
  tamper均拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正负定向执行、
  marker/count/capability tamper、`git diff --check`通过；全量回归待阶段收尾；
- 边界：两个direct groups、每组一个composed leaf；forwarding、额外carry计数
  分类、三个groups中的composition、多个override/group、symbolic overlap、region
  copy、calls、atomics及volatile effects拒绝。下一步设计统一bounded multigroup
  contract以控制组合爆炸。当前等级 I/T。

## 167. F172：Forwarded Double-Composed Multi-Group Tree（2026-07-28）

- 研究问题：F171的两个per-group composition proofs仍要求direct edges；需要证明
  corridor reconstruction不会改变local writer membership或把两个groups的
  composed leaves混淆；
- Scope与实现：组合F157与F171，保留两个groups各自`1`个composed leaf、至少两个
  pure leaves及树外carry。Compiler允许`tree.forwarded && [1,1]`，并新增独立
  `forwardedDoubleComposedGroupsRecursiveConditional`分类，避免误发布F171；
- Artifact：新增
  `forwarded_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  与
  `bounded-forwarded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`。
  Transfer同时声明`forwarded=true`、`double_composed_groups=true`及原有group/count
  markers；
- Validator：先恢复每条最多16 jumps、全树64 nodes内的actual tree，再分别执行
  两组complete partition、pure lane set、distinct leaf-local source与非空difference
  证明。`forwarded`与double marker均类别绑定；
- 自动化证据：F171七叶i32树在第一组entry edge加入forwarding block后仍得到十三
  状态。负例把该block改成branching merge形成多前驱group terminal，lowering
  fail-closed且poison路径infeasible；两个marker与capability tamper均拒绝；
- Correctness fix：初次tamper执行暴露validator把artifact dispatch误插入transfer
  normalization、提前引用`normalized_contract`的问题；已把marker normalization与
  contract dispatch恢复为两个阶段，并由capability-tamper路径回归覆盖；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正负定向执行、
  marker/capability tamper与`git diff --check`通过；全量回归待阶段收尾；
- 边界：forwarded two groups、每组一个composed leaf；three-group composition、
  多override/group、symbolic overlap、region copy、calls、atomics及volatile effects
  拒绝。下一依赖是forwarded three-group proof。当前等级 I/T。

## 168. F173：Forwarded Three-Group Recursive Byte-Lane Tree（2026-07-28）

- 研究问题：F170的三组partition要求每条tree edge直接相邻，无法覆盖branch
  normalization插入的unconditional jump blocks；仅检查flattened leaves会漏掉
  shared corridor、branching detour或第三组被错误接入另一组的情况；
- Scope：组合F157与F170。恰好三个pure internal writers，每组至少两个leaves，
  三个完整member sets两两disjoint，树外至少一个carry；每条逻辑edge允许最多
  16个unique-predecessor forwarding blocks，全树恢复预算64 nodes；
- Compiler与artifact：新增
  `forwarded_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  与
  `bounded-forwarded-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`。
  Transfer绑定`forwarded=true`、`multiple_groups=true`、`triple_groups=true`和
  `group_count=3`，避免被direct F170或two-group F167错误接收；
- Validator：先逐edge恢复唯一forwarding corridor与实际branch tree，再从三个
  repeated store IDs分别重建完整descendant/reference sets。要求所有pairwise
  intersections为空、每组lane set一致、source IDs精确等于三组writers，且marker、
  count、artifact与capability双向绑定；
- 自动化证据：F170七叶i32树在第一组入口加入forwarding block后仍由六个条件得到
  七个`1`和六个`2`。负例把入口改成branching merge，产生多前驱terminal并被
  fail-closed lowering拒绝，poison路径保持infeasible；forwarded/triple markers、
  `group_count`与capability tamper均拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、正向十三状态执行、
  branching corridor负例、marker/count/capability tamper与`git diff --check`
  通过；全量回归待本阶段收尾；
- 边界：恰好三个forwarded pure groups；group-local composition、四组以上、
  nested groups、shared/branching corridors、symbolic overlap、region copy、
  calls、atomics及volatile effects拒绝。下一依赖是三组中唯一composed group的
  direct/forwarded证明。当前等级 I/T。

## 169. F174：Composed Three-Group Recursive Byte-Lane Tree（2026-07-28）

- 研究问题：F170/F173的三个groups均要求pure leaves，F163的partial composition
  则固定为两个groups；在8-leaf上限内，三组恰好还能容纳一个composed leaf，但
  validator必须同时证明三组完整partition和唯一local writer的lane差集；
- Scope：direct strict binary tree中恰好三个internal writers；其中一组含至少
  两个pure leaves与一个partial-override leaf，另两组各至少两个pure leaves，
  树外一个carry。总计恰好八个leaves/七个branches；不允许exact override、
  第二个composed group或forwarding；
- Compiler与artifact：三组分类允许`composedOverrideCount<=1`，要求source集合
  大小精确为`3+count`并保留每组pure count。新增
  `composed_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  及
  `bounded-composed-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`。
  Transfer声明`multiple_groups=true`、`triple_groups=true`、`mixed_groups=true`、
  `composed_triple_groups=true`、`group_count=3`和`composed_group_count=1`；
- Validator：mixed partition由固定两组泛化为声明的2/3组，对全部member sets执行
  pairwise disjoint；逐组要求branch descendants精确等于source references和至少
  两个一致pure members。唯一composed leaf必须含一个distinct leaf-local writer，
  与所属group相交，并使保留internal lanes精确等于非空
  `group_set - local_set`；
- 自动化证据：i32八叶树中第一组i16 source覆盖lanes 0--1并在lane 1受i8 local
  writer覆盖，另外两个i8 groups覆盖lanes 2、3，另有carry；七个symbolic条件得到
  八个`1`和七个`2`。负例用同范围i16 local writer完全遮蔽第一组，compiler识别
  为exact override并fail closed，poison路径infeasible；category marker、
  composed count和capability tamper均拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、十五状态正向执行、
  full-shadow负例及marker/count/capability tamper通过；全量回归待阶段收尾；
- 边界：direct three groups且仅一组一个partial override；forwarding、第二个
  composed group、四组以上、nested groups、symbolic overlap、region copy、calls、
  atomics及volatile effects拒绝。下一依赖是F175 forwarded composed-trigroup。
  当前等级 I/T。

## 170. F175：Forwarded Composed Three-Group Byte-Lane Tree（2026-07-28）

- 研究问题：F174的八叶上界已经达到当前tree预算，但真实LLVM CFG仍可能在group
  入口与leaf edge间插入jump blocks；需要确认corridor recovery不会把唯一local
  override归到错误group，或在三组partition中遗漏shared predecessor；
- Scope：组合F157与F174。三个groups中恰好一组含一个partial-override leaf，
  每组至少两个pure leaves，树外一个carry；每条逻辑edge允许最多16个
  unique-predecessor unconditional blocks，全树最多64个恢复节点；
- Compiler与artifact：移除F174对`tree.forwarded`的临时拒绝，复用per-group
  counters与per-lane nearest-source transfer，新增
  `forwarded_composed_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis`
  及
  `bounded-forwarded-composed-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi`。
  Transfer同时绑定`forwarded=true`、三组、mixed、composed-triple及两个精确count；
- Validator：artifact类别独立于F173/F174，先逐edge恢复actual branch tree，再
  重建三个完整descendant/reference sets、全部pairwise disjoint关系与唯一
  leaf-local source的非空lane difference。`forwarded`和
  `composed_triple_groups`均为类别必需marker；
- 自动化证据：F174八叶树在第一组入口加入forwarding block后仍得到八个`1`与七个
  `2`。负例把入口改为branching merge，使group terminal具有两个predecessors，
  proof fail closed且poison路径infeasible；两个类别marker、composed count与
  capability tamper均拒绝；
- 验证：LLVM 18 compiler build、`llvm-as`、Python syntax、十五状态正向执行、
  branching corridor负例、marker/count/capability tamper及`git diff --check`
  通过；全量回归待阶段收尾；
- 边界：三组、唯一composed group/leaf与8-leaf预算；第二个三组composition需要
  至少9 leaves，已超当前证明上限。四组以上、shared/branching corridors、
  symbolic overlap、region copy、calls、atomics与volatile effects拒绝。当前
  bounded recursive byte-lane组合矩阵在8-leaf预算内闭合；下一步转向general
  multi-cell cyclic MemorySSA和跨prefix求解复用。当前等级 I/T。

## 171. F176：Validated Cross-Prefix Polyhedral Reuse（2026-07-28）

- 研究问题：原Pangolin-style cache key绑定exact branch PC、open-branch hash、
  prefix fingerprint、target与dependencies，因此只能复用相同上下文；AFL mutator
  虽可消费全矩阵，却无法判断另一个prefix的抽象是否相关，也未在保存每个sample前
  复检当前完整路径；
- 规范化与索引：QSYM runtime对cached/current linear constraints分别执行term排序、
  同offset coefficient合并及同normal bounds求交；再按双向implication分类为
  `equivalent`、`cached-subset`、`current-subset`或`compatible`。无共享变量、
  同normal区间冲突或规范化失败均不进入候选集；
- Soundness边界：relation只用于排序，不作为SAT证明。Exact-key lookup仍优先，
  exact UNSAT只对原key生效且绝不跨prefix传播。外来SAT model按当前seed应用后，
  必须由QSYM expression evaluator逐项验证当前完整prefix与desired target；由其
  box/full matrix生成的每个John/Dikin/integer sample也在写盘前执行相同验证；
- 有界执行：`SYMCC_POLY_CROSS_PREFIX=1`启用，按relation强度与稳定cache key排序，
  `SYMCC_POLY_CROSS_PREFIX_PROBES`默认32、上限256。Sampling strategy与adaptive
  MPI helper默认开启，自配置参数域可学习开关和probe budget；
- Telemetry：新增`poly_cross_prefix_probes/relations/hits/
  validation_failures`与`poly_sample_validations/validation_failures`，把抽象召回、
  精确有效率和采样拒绝成本分开；
- 自动化证据：新lit先在`value<=2`prefix缓存`value==1`的SAT polytope，再在
  exact key不同的`value<=3`prefix运行；第二次产生`\x01`并报告relation、probe和
  cross-prefix hit。原`poly_prefix_cache`继续证明窄prefix UNSAT不会污染宽prefix，
  `poly_cache`继续覆盖full-matrix John walk；Python mutator/self-config/adaptive
  tests全部通过；
- 验证：QSYM runtime完整重编译，三个poly lit tests、14个相关Python tests、
  syntax与diff checks通过；全量回归待阶段收尾；
- 边界：当前inclusion是同input-offset坐标系上的bounded linear-template关系，
  尚不做跨格式字段重命名、Fourier--Motzkin projection或一般非线性抽象。
  Concrete replay/AFL novelty仍是最终接纳边界，公开benchmark效果待验证。当前
  等级 I/T/E。

## 172. F177：Verifier-Gated Optimistic Generator Simplification（2026-07-28）

- 研究问题：F29 的 reusable generator 只在完整 path condition 上计算 byte
  range，因此生成域不会超过单次精确查询；GenSlv 的 optimistic simplification
  要求在较弱约束上构造 generator，但若直接把弱化模型写入语料会破坏 path
  soundness；
- 弱化构造：persistent `symcc-query-solver` 在载入 target SMT 前记录 prefix
  assertion 边界。默认 `SYMCC_GENERATOR_OPTIMISTIC=1` 时，proposal solver 只保留
  target assertions，并删除全部 prefix assertions；原 solver 始终保留完整
  prefix 与 target。由于弱化式是原合取式的断言子集，证书方向固定为
  `original-implies-weakened`。one-shot、无 prefix 或关闭开关时保留全部断言；
- 可审计证书：`symcc-solution-generator-v1` 新增
  `symcc-optimistic-simplification-v1`，记录原 query content hash、原断言数、
  target 起始索引、完整且互斥的 kept/dropped partition、策略、蕴含方向、
  `proposal_only` 与 `full_validation_required`。Python normalizer 检查 query ID、
  有序唯一索引、完整 partition、target 不可删除及验证计数守恒，畸形 artifact
  fail closed；
- 生成与验证：per-byte lower/upper 二分和 field sampling 在弱化 solver 上执行，
  因而可得到严格大于完整 PC 的范围；每个 boundary/midpoint/random candidate
  随后把全部 byte assignment 加回原 solver。仅 `sat` 进入
  `verified_models`，`unsat`、`unknown` 和异常均计入
  `full_validation_failures` 且不物化。Python 离线 sampler 继续要求调用方 verifier，
  concrete replay/AFL novelty 仍是最终语料边界；
- Telemetry：generator metrics 新增 `full_validation_checks`、
  `full_validation_failures` 与 `full_validation_accepted`，并强制
  `checks=failures+accepted`、`accepted=len(verified_models)`。这使 benchmark 能同时
  报告域扩张、solver 复核成本和有效模型率；
- 自动化证据：新 `query_generator_optimistic.py` 在完整约束
  `8 <= byte <= 10` 上证明弱化 target 域为 `[8,255]`，至少两个越界候选被完整
  solver 拒绝，所有保存 model 仍位于 `[8,10]`；关闭开关后范围恢复 `[8,10]` 且
  零验证失败。`test_solution_generator.py` 新增 partition、query identity 与计数
  tamper 拒绝；
- 验证：query helper 增量构建、6 个 generator unittest、13 个 QueryStore
  unittest、原 generator integration 与新 optimistic integration 均通过；
- 边界：当前是可验证的 target-assertion slice，而不是 Z3 tactic pipeline 内部
  model converter/simplifier 的完整导出；弱化 range 只能作为 proposal domain，
  不能单独证明完整 path feasibility。下一研究步骤是公开 benchmark 的
  accepted/proposed、models/query、unique edges/query 与 coverage/CPU-hour 消融，
  以及有独立证明检查器的 tactic model-converter 提取。当前等级 I/T/E。

## 173. F178：Native Z3 Tactic Model-Converter Generation（2026-07-28）

- 研究问题：此前 `converter_chain` 是 Query IR 上手写且可证明可逆的语法子集，
  没有执行 GenSlv 所依赖的 Z3 tactic model converter；仅在文档中记录 tactic
  名称或重新实现 substitution 都不能证明真实 converter 路径已运行；
- 原生 pipeline：generator helper 把 F177 的 proposal assertions 构造成
  model-enabled Z3 goal，按 `simplify & solve-eqs` 执行有界 tactic。仅接受单一
  subgoal；多 subgoal、超时和异常均 fail soft，继续使用 range sampler。当前系统
  Z3 4.8.12 通过 `Z3_goal_convert_model` 将 subgoal model 转回原 goal model，
  直接复用 Z3 维护的 elimination/model-conversion 语义；
- 多模型枚举：在转换后的 subgoal 中识别仍存活的8-bit input variables，对其当前
  assignment加入disjunctive blocking clause，最多尝试
  `4 * SYMCC_GENERATOR_CONVERTER_SAMPLES` 次。被 `solve-eqs` 消去的输入不在
  blocking clause中，而由 model converter 随每个 surviving-variable model恢复；
  空 blocking set时安全停止；
- 双重验证：converted model先覆盖完整SAT base中的原输入变量，再以全部byte
  equality回到原始full-prefix solver检查。只有`full solver == sat`才进入共享
  `verified_models`；converter失败不会改变主SAT结果，也不会绕过 F177 与 concrete
  replay边界。Converter models与range models共同受
  `SYMCC_GENERATOR_SAMPLES`总预算约束；
- Artifact：新增 `symcc-z3-tactic-model-converter-v1`，记录pipeline、原query
  hash、proposal assertion数、subgoal数、converted models、验证
  checks/failures/accepted、errors及`full_validation_required`。Normalizer验证
  pipeline白名单、query identity、flag状态和计数守恒，accepted数不得超过
  `verified_models`；
- 配置：`SYMCC_GENERATOR_TACTIC_CONVERTER=1`默认开启；
  `SYMCC_GENERATOR_CONVERTER_SAMPLES`默认2、上限64，且被generator总sample预算
  截断；
- 自动化证据：新`query_generator_tactic_converter.py`构造
  `x = y + 1, 8 <= y <= 10`。`solve-eqs`消去一个输入后，helper枚举多个subgoal
  models，通过原生converter恢复`(x,y)`，至少一个非base model通过完整solver；
  所有保存结果保持`x=y+1`和`y`范围。Generator unittest增加converter provenance、
  pipeline与计数tamper拒绝；
- 验证：query helper增量编译、7个generator unittest、13个QueryStore unittest、
  原range generator、F177 optimistic generator与新tactic-converter真实集成测试
  均通过；
- 边界：Z3 model converter是context-local对象，当前持久化其pipeline/provenance
  和已转换模型，不序列化内部converter闭包；pipeline固定为
  `simplify/solve-eqs`，多subgoal、非BV8 surviving variables与跨进程converter
  replay尚未实现。下一步应以公开benchmark比较syntax converter、native tactic
  converter、range-only和组合策略。当前等级 I/T/E。

## 174. F179：Validated Cross-Size Polyhedral Projection（2026-07-28）

- 研究问题：F176虽能在不同prefix key间按linear relation复用，但cache loader以
  当前input size规范化旧矩阵，旧artifact中超出当前长度的变量会在relation之前被
  丢弃；同时候选扫描硬性要求相同input size，无法复用共享字段上的抽象；
- Artifact正确性修复：新增按显式artifact `input_size`规范化约束的路径。Cache
  loader先完整保留旧坐标系矩阵，当前查询约束仍按当前input size校验，避免把
  “无法在当前维度直接解释”误写成“旧artifact非法”；
- 有界投影：对cached/current约束取当前有效的shared offsets。对每行
  `L <= kept + eliminated <= U`，依据每个被消去byte的`[0,255]`域计算
  `emin/emax`，生成
  `L-emax <= kept <= U-emin`。中间运算使用`__int128`并饱和到linear infinity；
  纯被消去且区间可满足的行被删除，不可满足行拒绝。多行共享被消去变量时，该
  per-row interval elimination是存在量projection的保守外包络，不声称精确
  Fourier--Motzkin整数投影；
- 召回与排序：`SYMCC_POLY_PROJECTED_REUSE=1`在cross-prefix模式默认开启。同长度
  direct equivalent/subset关系优先；direct `compatible/none`可尝试shared-variable
  projection。输入长度不同时强制经过projection路径，即使共享子空间已经
  equivalent，防止绕过维度证明；
- Soundness：仍只扫描SAT entries，跨prefix UNSAT禁止不变。外来model只应用当前
  范围内offset，必须通过当前完整prefix/target expression evaluator。Projected
  sample只使用过滤后的box与projected constraints，每个sample继续完整复检；
- Telemetry与调度：新增`poly_projection_attempts/relations/hits`，sampling
  profile、adaptive MPI默认开启，self-config可学习0/1开关；
- 自动化证据：新`poly_projected_reuse.c`先用两字节输入缓存
  `x<=2, y<=2, x==1`，再以一字节输入和`x<=3, x==1`运行。第二次通过shared-`x`
  projection产生`\x01`并报告attempt/relation/hit；关闭投影时projection计数均为
  零。原exact/cross-prefix/UNSAT-isolation三个poly tests继续通过；
- 验证：QSYM runtime完整重编译，四个poly lit tests、5个self-config与5个adaptive
  component tests、Python syntax和diff checks通过；
- 边界：当前只在相同offset语义下消去缺失变量，不做字段重命名、endianness/width
  转换、等式耦合的精确整数消元或非线性抽象。Projection只决定候选召回，完整
  expression validation与concrete replay仍是接纳边界。当前等级 I/T/E。

## 175. F180：Bounded Structure-Preserving Polyhedral Field Renaming（2026-07-28）

- 研究问题：协议版本、不同解析入口和结构化seed经常把同一组语义字段移动到不同
  byte offsets。F176/F179只比较相同offset，因而即使两个线性系统在变量置换后完全
  相同，也无法复用已有SAT polytope；
- 有界同构搜索：从cached/current矩阵提取非零变量及其系数/约束出现签名，签名只
  用于稳定候选排序，不作为可能产生false negative的硬过滤。对变量建立有界双射并
  回溯验证重命名后的完整规范化矩阵关系；默认最多6个变量，硬上限8个，默认检查
  128个完整双射、硬上限4096，防止最坏情况下的阶乘爆炸；
- 结构保持变换：命中双射后统一重写cached full matrix、model offset和polyhedral
  byte box，再进入现有relation-ranked候选序列。Direct relation优先于projection，
  projection优先于renaming；同一relation等级内保持确定性顺序；
- Soundness：字段重命名只用于同input-size SAT artifact召回，不传播UNSAT，也不与
  cross-size projection叠加。每个重命名model和每个衍生sample仍由当前完整
  prefix/target expression evaluator复检；错误或歧义映射最多降低召回率，不会
  成为路径可满足性证据；
- 配置与遥测：`SYMCC_POLY_FIELD_RENAMING=1`在cross-prefix模式默认开启；
  `SYMCC_POLY_RENAME_VARS`默认6、最大8，
  `SYMCC_POLY_RENAME_ATTEMPTS`默认128、最大4096。新增
  `poly_renaming_attempts/relations/hits`，sampling profile、MPI worker与
  self-config参数域均已接入；
- 自动化证据：新`poly_field_renaming.c`先缓存四字节输入左侧字段
  `(x0,x1)`的约束和model，再对结构等价但移动到`(x2,x3)`的查询执行。复用路径产生
  `00 00 01 00`，并报告attempt/relation/hit；关闭开关后三项计数均为零；
- 验证：QSYM runtime完整重编译，exact/prefix/cross-prefix/cross-size/field-rename
  五个poly lit tests、5个self-config和5个adaptive component tests、Python syntax
  与diff checks通过；
- 边界：当前只证明byte变量的线性矩阵置换同构，不推断语义字段名，不做
  width/endianness转换、多个字段的packing/unpacking或非线性映射；bounded搜索也
  可能遗漏高维或高度对称实例。所有候选仍必须通过exact expression validation与
  concrete replay。当前等级 I/T/E。

## 176. F181：Persistent Query-IR Converter Replay（2026-07-28）

- 研究问题：F29已把可逆Query IR equality写入`converter_chain`，但离线
  `sample_generator()`从未消费它，QueryStore也只物化solver侧
  `verified_models`。因此converter在跨进程持久化后仍只是元数据，不能称为可重放
  generator；
- Artifact合约：新增`QUERY_IR_CONVERTER_SCHEMA =
  symcc-query-ir-converter-v1`。Normalizer对root、equal relation、最多64步的
  invertible op白名单、最多16个byte assignments、数值位宽及constant side逐项
  检查；recipe必须声明`proposal_only`和`full_validation_required`。旧版无schema
  recipe按相同严格字段迁移到新canonical schema，未知op和关闭验证义务均fail
  closed；
- 层次重放：sampler在solver-verified models之后，依次生成单recipe proposal和
  无冲突的累积recipe proposal。后者可同时满足由不同prefix/target roots提取的多个
  equality；fixed冲突、重复byte不同赋值和越界offset均显式计数或拒绝。所有候选
  继续经过调用方verifier，新增`converter_attempts/conflicts`漏斗指标；
- QueryStore执行路径：完成SAT事务后，store加载content-addressed Query IR，以完整
  `[prefix..., target]` roots构造bounded evaluator verifier，再调用持久化generator。
  只有全部roots求值为true的offline model才进入candidate目录和外部async输出；
  `SYMCC_GENERATOR_REPLAY_SAMPLES`默认8、最大64、0禁用；
- 证据标记：离线候选manifest使用
  `candidate_source=generator-query-ir-replay`、
  `solver_verified=false`、`generator_replay_verified=true`和
  `query_ir_verified=true`，并保存generator hash、replay index及proposal漏斗。
  它不会伪装成solver model，后续concrete replay的`verified=false`边界不变；
- 自动化证据：generator unittest证明两个独立direct equality recipe只有累积为
  `AB`时通过verifier，并验证未知step/关闭full-validation被拒绝；QueryStore测试以
  `[64,67]`完整Query IR和更窄`[64,66]`generator domain物化`@/A/B`，检查离线候选
  的证据标记；
- 验证：9个solution-generator、14个QueryStore unittest、Python syntax与diff
  checks通过；
- 边界：持久化的是可审计Query IR inverse recipe，不是Z3 context-local model
  converter闭包；bounded evaluator不支持的表达式会保守拒绝离线候选。当前重放
  按recipe顺序累积，不求解冲突recipe的最大可满足子集。当前等级 I/T/E。

## 177. F182：SMT-Validated Exact Integer Projection Relations（2026-07-28）

- 研究问题：F179的per-row interval elimination是安全外包络，但多个约束共享被消去
  byte时会丢失耦合关系；整数投影还可能产生奇偶/同余等非凸集合，不能诚实地塞回
  单一`A x <= b`矩阵。外包络会把实际不相交的cache/current projection误排为
  compatible，浪费完整candidate验证；
- 双层表示：原区间投影继续作为可高效采样的proposal matrix；新增精确关系层把
  cached/current byte变量解释为`[0,255]`有界Int，分别构造对非shared变量的
  existential Presburger projection。系统不声称外包络本身已经精确；
- 精确分类：以三个有界Z3检查决定
  `cached => current`、`current => cached`和二者intersection，从而返回
  equivalent/cached-subset/current-subset/compatible/none。证明为none时不会进入
  cross-prefix probe；证明为非none时仍只用于SAT候选排序，采样继续完整表达式复检；
- 成本边界：`SYMCC_POLY_EXACT_PROJECTION=1`在cross-prefix模式默认开启；默认最多
  2个被消去变量（最大6）、32行（最大128）、每次10ms（最大1000ms），每个branch
  最多8个exact projection probes（最大64）。任一预算超限、Z3 `unknown`或异常均
  fail soft到F179区间关系；
- 排序与遥测：同relation等级下direct优先、exact projection其次、interval
  projection再次、field renaming最后。新增
  `poly_exact_projection_attempts/relations/unknown/hits`，sampling profile、MPI
  worker和self-config参数域已接入；
- 自动化证据：新`poly_exact_integer_projection.c`加载合法cache artifact
  `x - 2y = 0`，当前一字节查询要求`x = 1`。精确整数投影识别偶数域与`{1}`不相交，
  relation和validation failure均为0；关闭精确层后区间外包络产生relation，并由
  完整表达式验证拒绝其`x=2`model；
- 验证：QSYM runtime完整重编译；exact/prefix/cross-prefix/cross-size/
  field-renaming/exact-integer六个poly lit tests、self-config/adaptive tests、
  Python syntax与diff checks通过；
- 边界：当前使用Z3有界Presburger量词判定关系，没有导出无量词整数投影矩阵；
  超预算/unknown回退会恢复F179的保守外包络与exact candidate validator，而不是
  阻塞主求解。非线性、width/endian转换仍不进入此关系证明。当前等级 I/T/E。

## 178. F183：Cross-Size Endian-Aware Field Alignment（2026-07-28）

- 研究问题：F180的结构保持重命名被`cached.input_size == current.input_size`硬限制。
  实际格式迁移常同时改变总record长度、字段offset和byte order；即使两边使用相同
  数量的字段bytes，也无法进入重命名；
- 表示洞察：QSYM linear IR把multi-byte concat展开为byte coefficient，例如big-endian
  16-bit为`256*x0 + x1`，little-endian为`x2 + 256*x3`。因此相同width的端序变化
  不需要猜测host ABI，可由保持coefficient/constraint结构的byte bijection证明；
- 实现：字段重命名现在允许不同artifact/current input sizes。关系搜索仍要求双方
  非零变量数相同并受6/8变量与bijection attempt预算限制；成功映射统一重写cache
  matrix、model和box。跨长度候选必须显式经过projection或renaming之一，不能再以
  input-size mismatch无条件拒绝；
- Soundness：这不是按字段名猜测schema。只有重命名后的完整矩阵通过现有relation
  classifier才进入SAT candidate列表；外来model随后必须通过当前prefix/target
  expression evaluator。UNSAT仍不跨prefix，错误端序映射最多导致验证失败；
- 自动化证据：新`poly_cross_size_endian_renaming.c`加载两字节big-endian
  `256*x0+x1=1` artifact，将其映射到四字节record的little-endian
  `x2+256*x3=1`字段。Cache model `(0,1)`被转换为`00 00 01 00`并产生
  rename attempt/relation/hit；关闭开关计数均为0；
- 验证：QSYM runtime完整重编译，新cross-size endian lit及前六个poly tests通过，
  diff checks通过；
- 边界：当前覆盖相同byte数量的纯置换/搬移，不覆盖16到32位的zero/sign extension、
  bitfield packing、scale或非线性编码；这些转换若要加入，必须携带独立可检查的
  widening/narrowing证明。当前等级 I/T/E。

## 179. F184：Exact-Proof Widening Field Alignment（2026-07-28）

- 研究问题：F183要求cached/current非零变量数相同，不能复用16-bit字段到32-bit
  zero-extended表示。直接忽略新增bytes会把任意宽化误当作等价，尤其无法区别
  zero extension、sign extension和普通高位数据；
- 注入搜索：当cached变量数小于current且current变量数不超过rename上限时，搜索
  cached byte到current byte的有界injective mapping。Coefficient/row signatures
  继续只负责确定性候选排序，同一映射重写cached matrix/model/box；
- 强证明门：变量数不等的mapping必须同时经过F182 exact integer projection，并且
  返回`projected=true && exact_projected=true`；仅有interval relation的mapping
  一律拒绝。Z3存在量化current未映射bytes后检查双向蕴含与交集，因此高bytes只有在
  约束系统允许一致extension语义时才被接受；
- 组合成本：新增`SYMCC_POLY_RENAME_EXACT_PROBES`默认8、最大32，限制一次renaming
  search中可触发的exact projections；原variable、bijection、projection variable/
  row/timeout预算同时生效。预算耗尽只降低召回，不回退到不精确widening；
- 组合数据流：candidate可同时标记renamed/projected/exact_projected。采样使用精确
  关系对应的cached interval envelope，box offset先重命名再按projected variables
  过滤；model只写mapped bytes，未映射current bytes保持当前concrete base并由完整
  validator检查；
- 自动化证据：新`poly_exact_widening_renaming.c`把两字节big-endian
  `256*x0+x1=1` cache artifact注入四字节little-endian `uint32_t==1`查询。
  Exact projection证明额外高两字节可取0，model输出`01 00 00 00`，同时报告
  renaming relation/hit和exact projection relation/hit；关闭renaming计数为0；
- 验证：QSYM runtime完整重编译；widening、新cross-size endian与原field-renaming
  lit tests、self-config/adaptive tests、diff checks通过；
- 边界：当前只实现cached narrower到current wider的unsigned byte-aligned线性
  extension；不实现反向narrowing、sign-extension识别、bitfield packing或scale。
  这些场景继续走普通Z3，不会被宽化reuse近似。当前等级 I/T/E。

## 180. F185：Exact-Proof Narrowing Field Alignment（2026-07-28）

- 研究问题：F184只覆盖cached变量少于current变量的宽化，无法把旧格式中更宽、
  更靠后的字段投影到新格式的窄字段。简单截断model或忽略高位会把非零高位、
  溢出和任意变量排列误当作合法narrowing；
- 子字段搜索：当cached变量数大于current时，为每个current byte搜索一个不同的
  cached source，未选cached bytes改写到当前input范围之外的synthetic offsets，
  再作为存在量变量消去。映射、约束矩阵和model使用同一source-to-destination
  注入关系；
- 结构证据门：每个映射边必须具有完整约束出现签名一致，或至少一个规范化线性
  系数重叠。零结构重叠的全排列不进入SMT探测，避免把数学上偶然可投影的任意byte
  当作同一字段；签名与系数重叠只筛选/排序候选，不承担语义证明；
- 精确证明门：narrowing没有interval-only fallback。转换后的cached系统与current
  系统必须由F182 bounded Presburger检查证明为非空relation；变量、行、timeout、
  mapping attempts与`SYMCC_POLY_RENAME_EXACT_PROBES`预算共同限制成本。预算耗尽或
  `unknown`只降低召回；
- Soundness：外来SAT model只复制被选cached bytes，未选bytes不写入current
  input；relation命中后仍由当前完整prefix/target expression evaluator验证每个
  model/sample。UNSAT不跨prefix，synthetic变量不会进入输出；
- 自动化证据：新`poly_exact_narrowing_renaming.c`加载四字节线性字段，其中低16位
  位于offset 2/3，将其精确投影并重命名到两字节little-endian查询，生成
  `01 00`且同时报告renaming/exact-projection relation与hit；关闭renaming后计数
  为0。F182奇偶反例同时验证零结构重叠的单字节猜测不会绕过精确投影；
- 验证：QSYM runtime完整重编译；narrowing、widening、cross-size endian、
  field-renaming、projected-reuse与exact-integer六个poly lit tests、
  self-config/adaptive tests及diff checks通过；
- 边界：当前覆盖byte-aligned unsigned linear subfield selection，不推断C类型、
  signedness或schema；sign-extension、bitfield packing/unpacking、scale与非线性
  编码继续保守回退普通Z3。当前等级 I/T/E。

## 181. F186：Conditional Contextual ParaSuit Parameter Graph（2026-07-28）

- 研究问题：原`SelfConfiguringPolicy`虽能posterior sample与扩展数值域，但参数仍
  来自Python内嵌表，`select(context=...)`立即丢弃context，依赖关系只靠选择后的
  hard-coded guard；更严重的是reward会更新全部注册参数的当前/unset值，而非本次
  实际采样项，产生系统性归因污染；
- 机器schema：新增`util/self_config_schema.json`，使用
  `symcc-self-config-parameter-schema-v1`声明参数值、numeric bounds和
  `active_when`依赖。默认自动加载，可由`SYMCC_SELF_CONFIG_SCHEMA`替换、
  `SYMCC_SELF_CONFIG_SPACE`覆盖候选值；未知parent与循环依赖节点fail closed。
  `python3 util/self_config.py --print-schema`输出规范化工具registry；
- 条件参数DAG：选择前只排序当前profile中active specs，选择后迭代删除因parent
  改变而失活的descendants。Exact executor的fast/multi/optimistic禁用作为最终
  hard guard显式下发，避免继承环境重新开启；sampling profile自动建立poly/cache
  prerequisite；
- 上下文后验：token绑定稳定context key：
  `cold/warm/mature × targeted × structural-task × input-size bucket`。MPI派发输入
  size与target/task信息；worker rank故意不入键，使并行度变化不碎片化学习。
  全局value、context-value与context-local pair interaction后验按
  55/25/20层次组合，context最多128、interaction最多4096；
- 正确归因：pending token持久化`profile/context/choices`，完成时只更新本次真正
  选择的value与value-pairs；完整configuration posterior单独更新。Numeric expansion
  也只由被选择值触发。State schema升级到v2并兼容读取v1 pending/profile；
- 迁移隔离：`SYMCC_SELF_CONFIG_PRIOR`只读导入其他campaign的global value
  posteriors，以随本地pull数衰减的弱prior参与选择；不复制observations、contexts、
  interactions或pending，也不写回prior文件，避免跨target污染；
- 可复现性：state保存规范化parameter schema SHA-256、global/context/interaction
  posteriors和配置统计，写入仍采用temp+atomic replace；seed开关控制抽样；
- 自动化证据：`test_self_config.py`从5项扩展到8项，覆盖schema依赖、循环拒绝、
  inactive descendant裁剪、hard guard、稳定context key、interaction持久化、
  v2恢复与read-only transfer prior隔离；CLI schema JSON、Python syntax、
  adaptive component和diff checks均通过；
- 边界：这是对ParaSuit自动识别/选择/采样思想的SymCC参数注册表实现，不声称复刻
  KLEE命令行爬取、论文silhouette clustering或其12-program覆盖结果。当前尚缺
  等CPU公开目标上fixed-registry、global-only、conditional-contextual与transfer
  prior的分项消融。当前等级 I/T/E。

## 182. F187：Feedback-Validated Online Token Grammar（2026-07-28）

- 研究问题：原`solve_complete`只把AFL extras/历史fragment做固定宽度overwrite；
  `literal/delimited/key_value/segment`没有持久规则identity、没有variable-length
  completion，也不消费proposal验证结果，因而不能称为在线grammar acquisition；
- Span推断：从`comparison_taints`的`(branch,lo,hi)`core向两侧扩展到空白、分隔符
  或bounded lexical boundary；完整quote/bracket pair优先形成closed span，防止把
  相邻payload误吞入token。`SYMCC_GRAMMAR_MAX_SPAN`默认128、范围8--1024；
- 规则系统：新增stable SHA-256 `LearnedGrammarRule`，在线累积literal、delimited、
  key-value、segment、choice、相邻span sequence、optional删除和repetition复制。
  `SYMCC_GRAMMAR_RULES`默认2048、最少32、硬上限8192；满额时优先淘汰未retained、
  低score、低support规则；
- 可变长度生成：grammar路径使用`content[:lo] + production + content[hi:]` splice，
  可生成更短/更长输入，继续受全局proposal byte cap约束。Rule按未尝试探索、
  support、target-validation ratio、AFL-retention ratio和coverage features排序；
- 验证闭环：proposal manifest与`VerifiedProposal`持久化`grammar_rule_id`和
  generator label。真实目标执行更新`validations/verified`；只有进入全局AFL
  novelty triage后才更新`retained/coverage_features`。Rule feedback只影响排序，
  从不把grammar接受当作parser proof或SAT证据；
- Artifact：`.semantic_proposal_generator.json`升级schema v2，兼容读取v1，
  保存rules、support/attempt/validation/retention漏斗、grammar coverage和预算。
  `grammar_snapshot()`报告attempted/validated/verified/retained rule coverage；
- 自动化证据：`test_semantic_proposals.py`扩展为7项，新增词法span、长token splice、
  provenance传入proposal manager、验证/coverage反馈与v2恢复；quote-delimited旧
  回归、inverse/surrogate/Hydra/UCSan tests继续通过。2个proposal-manager、
  8个self-config tests、Python syntax和diff checks通过；
- 边界：当前是comparison-taint/token evidence驱动的bounded regular tree grammar
  approximation，不是完整CFG推断；尚未从parser accept/reject trace做冲突规则拆分，
  也未实现Query IR partial model与nonterminal hole的统一求解。当前等级 I/T/E。

## 183. F188：Fail-Soft Per-Query Selective Concolic Partitioning（2026-07-28）

- 研究问题：原`SYMCC_FOCUS_BYTES`只能在执行前全局决定symbolization范围，无法按
  query约束结构选择变量；直接把非目标byte concrete化后返回UNSAT则会错误剪枝其他
  concrete completion；
- 数据协议：`WorkLease`携带QueryStore中content-validated witness `input_hex`，
  persistent helper协议从5字段扩展为向后兼容的6字段；8字段live continuation
  check协议不变。空/超长/奇数长度/非hex witness保守禁用选择性路径；
- 依赖分区：helper为每条prefix/target assertion收集8-bit input offsets，建立隐式
  assertion-to-byte超图，从全部target bytes做传递闭包。只允许闭包外disconnected
  variables使用witness equality；与target通过任意prefix row耦合的byte始终保持
  symbolic；
- 非对称可信边界：短超时selective solver仍断言完整prefix+target，只额外加入
  witness equalities，因此SAT model是完整query的真实模型，可标为`z3-selective`。
  Selective UNSAT/unknown/exception只pop局部frame并恢复完整timeout执行普通Z3，不写
  UNSAT proof、不进入subsumption；generator model继续在去除fixes后的full solver
  中逐个验证；
- 成本与自配置：`SYMCC_SELECTIVE_QUERY`默认开；默认至少fixed 2 bytes、最多64，
  剩余symbolic最多16，probe最多`min(50ms, query timeout)`，各有硬上限并进入F186
  schema。超预算直接full solve；
- Telemetry：result增加`selective_query_attempted/hit/symbolic/fixed/elapsed_us`，
  QueryStore严格bounded normalize并统计attempts/hits；portfolio仍可逐backend比较；
- 自动化证据：新`query_solver_selective.py`构造两个disconnected prefix bytes和
  一个target byte。合法witness产生`z3-selective`模型`42 05 07`（fixed=2、
  symbolic=1）；故意错误witness使局部查询UNSAT，但fallback full Z3仍返回相同SAT
  模型且hit=0。104项lit中该测试及5个query generator/PSCache/prefix tests通过；
  14个QueryStore、9个solution-generator tests、runtime完整重编译与diff checks通过；
- 边界：当前只分区QF_BV 8-bit input constants，不处理FP、String/Array或随机变量，
  也未实现FM 2026论文的学习型timeout predictor/MDP；这是可审计、fail-soft的机制
  baseline，科研收益仍需公开benchmark。学习型byte-domain子集已由F189补充；本段
  保留F188阶段边界。当前等级 I/T/E。

## 184. F189：Persistent Cost-Aware Mixed Completion Policy（2026-07-28）

- 研究问题：F188只用静态变量阈值决定每个eligible query都执行一次witness probe，
  无法区分“局部fix经常命中并节省长求解”的上下文与“局部fix稳定失败、只增加额外
  solver调用”的上下文，也没有让fuzz-side concrete completion参与同一个精确查询；
- 上下文与在线估计：persistent helper把断言数、8-bit输入变量数、target依赖闭包
  比例、fixed变量数、共享AST节点数和mul/div/rem/shift高成本节点数做对数/比例分桶。
  每个context维护decision/trial/hit计数以及full/selective elapsed EWMA；Beta平滑
  命中率加置信上界形成optimistic expected saving。冷启动探索后，只有预测节省达到
  `SYMCC_SELECTIVE_QUERY_POLICY_MIN_SAVINGS_US`才尝试，否则记录`learned-skip`并
  直接完整求解；
- Timeout predictor：选择性预算以上一轮配置值为上界，以两倍selective EWMA与
  full-solve EWMA的四分之一为候选收紧，并受最小timeout、原query timeout双重限制。
  Predictor只控制额外probe成本，不缩短完整solver的权威timeout；
- Mixed completion：同一个disconnected component可在总预算内依次尝试witness、
  全零、全`0xff`和由query ID/witness确定性播种的随机assignment，最多8个并去重。
  每次都在独立push/pop frame中额外断言fixed equalities，完整prefix+target保持不变；
  因此任意completion SAT都是精确模型，所有UNSAT/unknown/exception仍只触发下一个
  completion或full solver，绝不进入UNSAT cache/subsumption；
- 持久化与并行隔离：策略artifact为
  `symcc-selective-query-policy-v1`，有限context表使用temp+rename替换，非法、
  非有限或计数不一致的行忽略。Query service默认按
  `worker slot × portfolio backend`分配独立state路径，跨重启恢复且避免并发writer
  覆盖；用户显式共享路径不提供多writer一致性保证；
- Telemetry与配置：result新增`eligible`、policy context/decision/observations、
  predicted savings、full EWMA、learned timeout、completion attempts/hit index。
  QueryStore对所有整数做硬边界校验，并统计policy skips和non-witness mixed hits；
  completion、learning、explore、minimum saving、minimum timeout已进入F186条件参数
  图，路径、context容量和保存周期保留为运维配置；
- 自动化证据：扩展`query_solver_selective.py`。原合法witness仍返回精确
  `42 05 07`；错误witness仍fallback得到相同完整模型；`ff ff` disconnected
  prefix由第三个all-`0xff` completion命中；两个稳定失败样本后第三个同context
  query产生`learned-skip`，关闭helper并重启后第四个query继续skip且observation从
  state恢复。该lit测试、8个self-config、14个QueryStore测试、runtime重编译、
  Python/JSON/diff checks均通过；
- 边界：这是bounded contextual bandit/cost predictor，不声称复刻论文完整MDP。
  当前mixed concrete domain仍限QF_BV byte变量；FP、nonlinear real、SMT String/
  Array的domain-specific partition与公开benchmark saved-time/coverage消融仍待完成。
  当前等级 I/T/E。

## 185. F190：Query-IR-Verified Grammar Hole Completion（2026-07-28）

- 研究问题：F187的grammar completion只依据comparison token span，F181的Query IR
  model/converter replay只生成满足约束的byte assignment；两者没有共享“哪些字节已被
  当前path语义约束、哪些结构字段仍是hole”的机器可验证表示，因而无法实现
  Solve-Complete式“SMT先约束语义、grammar再补结构”；
- Query candidate contract：QueryStore在每个solver/generator candidate manifest中
  写入由真实expression DAG收集的`query_input_offsets`、实际
  `model_assignment_offsets`与evaluator计算的`query_ir_verified`，并为MPI
  `.query_candidates`输出生成隐藏`.NAME.query.json` sidecar。Master显式跳过隐藏
  sidecar，防止metadata被当成测试输入；
- 正确性加固：过去primary solver manifest隐含假设solver返回值必然有效，本阶段改为
  对materialized witness+assignment执行Query IR evaluator后才设置
  `query_ir_verified=true`。新增矛盾fake SAT回归确认伪模型仍可作为非可信诊断artifact，
  但不得获得Query-IR verified身份；
- Hole生成：SemanticProposalGenerator只在manifest candidate SHA匹配、query ID合法、
  sidecar读集与QueryStore重新计算的读集完全一致、原candidate重新通过Query IR时
  启用联合补全。Comparison-derived lexical span与任何query input offset相交则拒绝；
  对其余span应用online grammar，允许长度变化，但每个结果再次执行完整Query IR
  evaluator，绝对offset变化破坏语义时自然拒绝；
- 三层证书：通过第一层的candidate携带
  `symcc-query-grammar-hole-v1`，绑定query ID、source candidate/manifest SHA、
  精确read set、hole范围与stable grammar rule ID，并明确
  `target_replay_required/coverage_retention_required`。VerifiedProposalManager严格
  校验证书和rule provenance、持久化query/hole字段；之后仍由真实target telemetry
  验证目标，再由AFL bitmap决定retention；
- 反馈与预算：`query_hole_completion`复用F187 rule validity/coverage feedback，
  grammar state schema升级到v3并兼容v1/v2，记录hole attempts/verified/rejected。
  `SYMCC_QUERY_GRAMMAR_HOLES`提供campaign消融，
  `SYMCC_QUERY_GRAMMAR_HOLE_MAX`默认16、硬上限64；
- 自动化证据：`test_semantic_proposals.py`新增真实QueryStore端到端测试：只约束
  JSON首字节`{`，把未约束`"x"`hole变长补为`"LONGVALUE"`，所有输出重新满足Query
  IR；篡改sidecar隐藏约束读集后零hole proposal。`test_verified_proposals.py`新增
  证书严格校验、tamper拒绝与state恢复；QueryStore测试从14增至15，新增真实manifest
  read/assignment set、隐藏sidecar与contradictory fake-SAT gate。8个semantic、
  3个proposal-manager、15个QueryStore测试及对应3项lit、Python syntax和diff checks
  均通过；
- 边界：当前“hole”来自comparison-taint lexical span，不是CFG nonterminal推断或
  parser-state proof；Query IR evaluator仍只覆盖其bounded BV操作集。History-guided
  seed acquisition、parser accept/reject冲突拆分和string/array partial model仍待
  后续阶段。当前等级 I/T/E。

## 186. F191：Plateau-Gated Retained-History Acquisition（2026-07-28）

- 研究问题：F187会从所有观察输入学习fragment，无法区分普通路径噪声与真正被AFL
  全局novelty保留的结构；若持续做跨seed completion，还会在已有策略仍产出覆盖时
  增加大量低价值replay。G11需要一个由真实coverage history驱动、只在plateau触发
  的结构化seed acquisition baseline；
- Retained bank：proposal通过真实target验证并进入AFL全局bitmap后，读取其
  `candidate_path`，以content SHA建立`HistorySeed`。每项有coverage features、
  retained、attempts、target validations/verified漏斗；内容默认最多4096 bytes，
  bank默认128项，满额按未retained、低yield/score优先淘汰；
- Plateau gate：MPI把每次完成任务的authoritative `coverage_delta`传给semantic
  generator。连续零增益计数达到`SYMCC_HISTORY_ACQUISITION_PLATEAU`（默认64）前
  不运行history route；任意正增益立即归零。计数有界到阈值四倍并进入state；
- Acquisition：平台期按coverage yield、验证率和underexplored bonus排列retained
  seeds，从其中提取bounded lexical/closed-delimiter tokens，splice到当前
  comparison-derived spans。Proposal标记`history_acquisition`与stable
  `history_seed_id`，manager严格校验并持久化provenance。目标验证结果回写seed
  validity，AFL retention回写二次coverage yield，同时任何新retained semantic
  candidate都可进入bank；
- 状态与预算：semantic generator state升级schema v4并兼容v1--v3，持久化seed
  内容/漏斗、plateau计数与全局history attempts/verified/retained。Seed数、单seed
  bytes和plateau阈值各有硬上限；proposal仍受全局per-observation与input byte预算；
- 自动化证据：`test_semantic_proposals.py`从8增至9项。保留
  `kind=LONGVALUE;`后，`kind=x;`第一次零覆盖不产生history proposal，第二次达到
  threshold后生成含`LONGVALUE`的candidate；proposal manager保存seed ID，target/
  retention反馈及generator/manager重启均恢复；随后`coverage_delta=1`立即清零并
  禁止history route。3个proposal-manager、15个QueryStore回归与Python/diff checks
  同时通过；
- 边界：当前history结构是bounded lexical token bank，不是Cottontail完整path/
  context-sensitive history或LLM acquisition；plateau按master观测次数而非时间/
  survival model定义。公开benchmark需消融always-on、plateau-gated、random-history
  与no-history并报告proposal validity、retention和coverage/CPU。当前等级 I/T/E。

## 187. F192：Independent Parser Oracle and Contextual Grammar Conflict Splitting（2026-07-28）

- 研究问题：命中目标branch只能证明candidate可被当前执行接受，不能证明其满足独立
  语法。F187的rule rejection又是全局的，同一个fragment在一种结构位置失败后可能
  错误压制其他有效位置。F192把parser evidence作为目标回放之后、coverage triage
  之前的独立可信门，并把grammar冲突拆分到结构上下文；
- Parser oracle：`SYMCC_PROPOSAL_PARSER`接受shell-split命令，`{input}`替换为
  content-addressed candidate路径；若没有占位符则追加路径。只有退出码0表示
  parser-valid，非零、启动错误和有界timeout全部fail closed，stdout/stderr丢弃。
  Parser从不替代真实target/branch replay，也不替代AFL全局novelty；
- Context identity：grammar candidate携带由target branch、输入长度桶、hole左/右
  bounded lexical neighborhood计算的stable context ID。相同rule在同一context连续
  两次parser rejection后只屏蔽该context；其他context仍可生成。后续parser
  acceptance会清除对应局部冲突；
- 反馈正确性：MPI仅按本次validation的`last_reason`回传parser verdict，不能把历史
  累计“曾经接受”误当成当前结果。目标回放未通过时parser没有被执行，反馈为unknown，
  不会错误解除context suppression；
- 状态与审计：grammar state升级为schema v5并兼容v1--v4，持久化rule-level parser
  validation/acceptance以及每条rule最多256个rejected contexts；proposal manager
  保存parser漏斗、grammar context和最后结论。非法context/provenance在恢复时拒绝；
- 自动化证据：`test_verified_proposals.py`用独立进程oracle在相同target gate之后
  接受`GOOD`、拒绝`BAD`；`test_semantic_proposals.py`证明同一rule/context两次拒绝
  后跨重启保持局部屏蔽，随后parser acceptance重新开放。10个semantic、4个
  proposal-manager、15个QueryStore测试、3个query lit测试及Python/diff checks通过；
- 边界：parser由campaign显式提供，框架不能从任意binary自动恢复权威CFG或
  nonterminal。当前context是稳定的bounded结构指纹，不是parser-state proof；
  每个候选增加一次独立进程开销。完整Cottontail ECT/nonterminal representation、
  自动parser instrumentation与公开benchmark validity/coverage消融仍是后续研究。
  当前等级 I/T/E。

## 188. F193：Exact String/BV Dual-View Query Contract（2026-07-28）

- 研究问题：F33的string query只声明SMT `String`，再把model文本回填成input byte，
  没有同时保留bit-vector path view，也不能表达String predicate与byte predicate的
  联合约束。审查还发现`nul_terminated`变量缺少NUL时仍可通过concrete evaluator，
  非NUL变量允许与固定capacity不一致的动态长度；
- 双表示schema：`symcc-string-query-v2`为每个non-overlap variable保留String view，
  并为其每个input offset声明独立8-bit BV view。V1 artifact继续可读并规范化为v2。
  `byte_constraints`支持eq/ne与unsigned order，且只能引用已链接的string span；
- 精确链接：非NUL region强制`len == capacity`并逐位置约束
  `str.at == str.from_code(bv2nat(byte))`。NUL region强制`len < capacity`，所有
  `i < len`字符与非零byte双向一致，`i == len`的byte必须为零；suffix不被错误固定，
  可保留原witness；
- Binary与模型提取：literal lowering对每个0--255 byte使用`str.from_code`，不再把
  非打印字符退回patch-only路径。C++ helper把显式BV model设为权威来源，最后覆盖
  String C API反序列化结果，避免UTF-8或内嵌NUL改变offset/value；
- 正确性修复：SMT-LIB `str.contains`的参数是`(haystack, needle)`；旧lowering与
  prefix/suffix共用相反顺序，可能得到错误UNSAT。现在单独lower且有文本与真实solver
  回归。Concrete evaluator要求NUL实际存在，并同时复核所有String和BV predicates；
- Telemetry：materializer与MPI worker metrics新增`dual_view_queries`和
  `dual_view_verified`，候选仍进入正常target/AFL coverage triage；
- 自动化证据：8个string单元测试覆盖双view文本、联合contains/BV、NUL缺失拒绝、
  legacy升级和固定长度拒绝。真实`z3-string` helper集成覆盖比较翻转、
  `contains + byte[0]=='A'`及`41 01 42` binary literal；37项跨semantic/proposal/
  QueryStore/string回归、runtime重编译、Python/diff checks均通过；
- 边界：这是单一Z3 context中的精确String/BV view，不是smt-switch多backend IR。
  F193阶段runtime仍主要从memcmp/strcmp/strncmp导出artifact；strlen/strchr/strstr
  已由F194补齐，conversion summaries、cvc5/Princess/Z3str3 portfolio和公开
  SymCC-str benchmark对拍仍留给后续阶段。当前等级 I/T/E。

## 189. F194：Guarded Runtime String-Operation Query Artifacts（2026-07-28）

- 研究问题：F193具备contains/indexof/length的solver IR，但runtime只从
  memcmp/strcmp/strncmp生成token equality，真实`strlen/strchr/strstr`调用无法把
  symbolic length/search语义送入双view backend；
- Operation contract：新增`symcc-string-operation-v1`。记录operation/site、
  symbolic role、constant operand、concrete observed length/index以及从符号对象首字节
  到first-NUL的连续`input_bytes`。只接受全部byte为direct input offset、offset连续、
  没有提前NUL、总长不超过4096的记录；
- Query translation：`strlen`生成`length != observed_length`，`strchr/strstr`生成
  `indexof != observed_index`。Observed hit可探索其他位置或missing，observed miss
  可探索hit。符号needle与符号haystack分别映射到同一backend-neutral term位置；
  operation record不携带direct patches，防止观察值被误当作candidate；
- Runtime语义：compiler新增`strlen/strstr` interception，`strchr`沿用既有wrapper。
  `strlen`为当前bounded path构造first-zero ITE返回表达式，并约束前缀非零/terminator
  为零；`strchr`补上过去遗漏的found-byte equality或not-found terminator条件；
  `strstr`在最多16384个byte comparisons内建立BV contains OR-of-matches。超预算不做
  近似，保留concrete libc结果并允许严格operation artifact走String solver；
- 正确性边界：只在单符号角色可证明时导出operation；`strchr('\\0')`不映射到排除NUL
  的SMT String；empty/过长/不连续/多符号情况保守跳过。所有String solver model仍由
  F193 dual-view evaluator复核，再进入target/AFL triage；
- 自动化证据：新增真实instrumented `string_operations.c`，输入三个`ABC\\0` span，
  分别导出strlen length 3、strchr miss -1、strstr hit 1；独立checker对每条artifact
  调用真实Z3 helper，确认原witness不满足alternative且model满足双view。10个string
  unit、39项跨层回归、原strcmp/strchr集成、compiler/runtime重编译和diff checks通过；
- 边界：当前没有strtol/atoi/base64/URL/Unicode等conversion model，strstr的BV
  fallback是有界quadratic summary；smt-switch多backend与真实parser benchmark仍待
  实现。当前等级 I/T/E。

## 190. F195：Validation-First Parallel String Backend Portfolio（2026-07-28）

- 研究问题：单Z3 backend无法测量string theory solver差异，也不能在某个backend
  timeout/unknown时利用其他实现；但直接相信任一solver的UNSAT或厂商特定String
  model会把portfolio disagreement变成错误剪枝；
- Backend-neutral adapter：保留`symcc-json`协议，并新增`SmtLibCliStringBackend`。
  后者把同一v2 dual-view SMT-LIB交给外部命令，只请求已链接的numeric 8-bit symbols，
  解析`#xNN`、`#bNNNNNNNN`和`(_ bvN 8)`；不依赖backend特有String model打印格式；
- 并发与预算：`StringSolverPortfolio`最多8个backend，使用bounded thread pool并发，
  每个process backend仍有独立timeout。结果按配置顺序规范化，最多消费8条；
  materializer可在全局candidate budget内接纳多个不同verified SAT model；
- 非对称可信边界：每个SAT先应用到原witness并执行完整F193 concrete evaluator。
  Invalid/incomplete model只记rejected；相同合法model记duplicate agreement而不是
  invalid。SAT与UNSAT同时出现只增加`solver_disagreements`；UNSAT不剪枝、不缓存、
  不阻止任何合法SAT candidate；
- 配置与观测：MPI和离线CLI支持`SYMCC_STRING_SOLVER_PORTFOLIO`/`--portfolio` JSON
  或文件，entry分`symcc-json`与`smtlib`，命令不经shell。Metrics增加backend runs、
  SAT/UNSAT/unknown/error、disagreement、rejected与duplicate models；
- 自动化证据：unit portfolio并发一个UNSAT、一个伪SAT、一个合法SAT和一个重复SAT，
  仅合法model被接受，漏斗分类为4 runs/3 SAT/1 UNSAT/1 rejected/1 duplicate/
  1 disagreement。CLI stub覆盖三种BV model格式与显式`get-value`；配置边界覆盖两类
  backend。真实双Z3离线smoke产生2 SAT、1 verified、1 duplicate、0 rejected；
  13个string unit、42项跨层回归、真实string solver集成与Python/diff checks通过；
- 边界：F195阶段尚未安装cvc5、Princess或Z3str3，因此当时只验证了adapter contract
  与双Z3并发；F198已补上cvc5 1.1.2真实conformance，Princess/Z3str3仍未验证。
  Portfolio当前不学习backend选择或共享incremental context；真实多solver效果与
  资源隔离仍需benchmark。当前等级 I/T/E。

## 191. F196：Exact-Subdomain Decimal Conversion Semantics（2026-07-28）

- 研究问题：G04已有string search/length，却没有字符串到整数的跨理论转换；直接把
  完整C `atoi`映射到SMT `str.to_int`会在空白、正负号、trailing bytes、locale和
  overflow上产生语义不一致；
- 精确子域：只接受first-NUL前1--10个ASCII digits、无sign/space/prefix/trailing
  character，且concrete fold不超过`INT_MAX`并与真实libc结果相等。其他输入不导出
  artifact、不设置symbolic return expression；
- Query IR：string predicate新增`decimal`和`to_int`。前者lower为non-empty
  `[0-9]+` regex，后者lower为`str.to_int` integer relation；两者必须同时成立，
  防止无效string的SMT `-1`被错误当作C `atoi` model。Concrete evaluator用同一
  ASCII digit contract复核；
- Runtime语义：compiler拦截`atoi`；wrapper在32-bit BV中逐digit执行
  `acc = acc * 10 + digit`，每byte加入`'0' <= byte <= '9'`，并约束observed NUL。
  `symcc-string-operation-v1`新增`observed_value`，转换为
  `decimal(value) ∧ str.to_int(value) != observed`；
- 验证链：direct-input连续span和first-NUL仍由F194 provenance gate证明，String/BV
  link与candidate evaluator沿用F193，portfolio沿用F195。候选可改变数字或缩短长度，
  但必须留在精确decimal子域；
- 自动化证据：unit构造offset 2的`123\\0`，确认原值不满足alternative、`124\\0`
  满足、`ABC\\0`被regex/evaluator拒绝。真实instrumented operation test新增offset
  12--15的atoi artifact，检查observed value 123并由真实Z3产生有效纯数字candidate。
  14个string unit、43项跨层回归、compiler/runtime重编译、Python/diff checks通过；
- 边界：尚不支持`strtol/strtoul`的base/endptr、signed decimal、leading whitespace、
  overflow/errno或locale，也不处理数字后suffix。该保守子域是可证明baseline，
  不是完整libc conversion模型。当前等级 I/T/E。

## 192. F197：Width-Bound Signed strtol Contract（2026-07-28）

- 研究问题：F196只覆盖unsigned `atoi`；`strtol`还包含sign、base、endptr消费位置、
  target-long位宽与errno/overflow。若忽略任一项，String model可能与C observable
  state不一致；
- 严格启用门：只在`endptr == NULL`、base参数concrete且等于10、输入为可选单个
  `-`加1--19位digits（32-bit long最多10位）、first-NUL结束、真实调用未ERANGE且
  手工overflow-safe fold与libc结果相等时启用。其他情况保留原libc返回与errno；
- Width contract：operation artifact新增`integer_bits=32|64`。F196 atoi同样明确
  `integer_bits=32`；query除different-value外加入目标类型min/max。由此String
  proposal不能生成超出C返回类型的decimal，即使原observed value本身合法；
- Signed String IR：`decimal(signed=true)`lower为`[0-9]+ | -[0-9]+`，
  `to_int(signed=true)`以prefix test、substring和整数negation组合
  `str.to_int`。Concrete evaluator使用完全相同的可选minus/digits/range规则；
- Runtime语义：compiler拦截`strtol`。Wrapper保存并按success/error恢复errno，
  以unsigned accumulator和`limit-digit`预检避免host arithmetic overflow，
  在target `long`位宽构造multiply-by-10 fold；negative path显式约束minus，
  digits与NUL继续有BV guards；
- 自动化证据：unit以offset 1的`-123\\0`验证signed regex、prefix/substr lowering、
  negative/positive alternative及`+123`拒绝。真实operation test在offset 16--20
  导出observed -123、integer_bits 64，并由真实Z3求得范围内candidate；同一span的
  non-null endptr/base16调用不产生第二条artifact。15个string unit、44项跨层回归、
  compiler/runtime重编译、Python/diff checks通过；
- 边界：完整strtol仍需endptr symbolic memory effect、base 0/2--36、leading
  whitespace/`+`、prefix、suffix消费、locale和ERANGE/errno状态。当前子域因
  `endptr == NULL`而避免伪造副作用，并不声称operation-complete。当前等级 I/T/E。

## 193. F198：Executable Cross-Solver String Conformance Gate（2026-07-28）

- 研究问题：F195的backend adapter测试只能证明本地协议和model parser，不能证明另一
  String solver接受同一SMT-LIB语法、返回完整BV model且与F193 concrete contract一致；
  单纯获得`SAT`也无法排除不同String/Integer语义或错误model解释；
- 可执行门禁：新增`util/string_backend_conformance.py`，固定运行binary byte、
  contains+BV、negative indexof、atoi和signed strtol10五类query。每个backend的每个
  结果都必须为SAT，assignment应用到witness后必须通过独立dual-view concrete
  evaluator；空结果、unknown、UNSAT、不完整model和语义不一致均令门禁失败；
- 可移植性修复：实测cvc5暴露四处Z3方言依赖，lowering统一改用SMT-LIB可接受的
  `bv2nat`、`str.to_int`、`str.to_re`，负整数常量使用`(- N)`而非词法`-N`。
  CLI adapter在非零退出时合并stdout/stderr，保留solver parser诊断；
- 证据协议：输出版本化`symcc-string-backend-conformance-v1`，逐case记录solver、
  status、verified、assignment数量、耗时和有界reason；`--backend`可直接检查F195
  portfolio配置，`--solver`检查单个内置helper，lit加入持续回归入口；
- 自动化证据：仓库内置Z3和Ubuntu打包cvc5 1.1.2都在真实进程中完成5/5 SAT且5/5
  concrete-verified；binary、联合String/BV、负index和两种跨String/Integer转换均
  不是adapter stub。相关string/semantic/QueryStore回归、Python compile和diff
  checks通过；
- 边界：本轮未安装Princess和Z3str3，也未声称五个探针覆盖完整SMT-LIB String
  标准、增量context、性能稳定性或UNSAT proof。新增backend仍须固定版本运行该门禁，
  并在公开目标上另做等CPU效果评测。当前等级 I/T/E。

## 194. F199：Validation-Aware Contextual String Backend Selection（2026-07-28）

- 研究问题：F195对每条query固定运行全部backend，能发现disagreement但在某个solver
  对特定String fragment持续慢或产生无效model时浪费预算。通用SMTgazer调度器又不
  掌握F193的独立concrete verification结果，不能直接作为String可信反馈；
- 上下文表示：策略以operation集合、总capacity桶、单/多变量和是否含BV predicate
  形成稳定context。它维护backend全局arm与最多1024个context-local arm，统计pull、
  concrete-verified SAT和elapsed microseconds，不把solver自报SAT直接当成功；
- 选择策略：显式`selection.max_backends`限制每query实际backend数；cold-start保证
  每个backend至少运行`warmup`次，周期性`explore_every`选择低使用arm，其余按平滑
  verified yield、timeout归一化成本和不确定性项排序。默认无`selection`时行为保持
  全portfolio并发；
- 可信边界：materializer先应用assignment并执行完整dual-view evaluator，再批量回灌
  policy。duplicate合法model仍是solver语义成功；invalid/incomplete SAT、UNSAT、
  unknown和error均为零成功，但UNSAT仍不剪枝、不缓存。target coverage acceptance
  与backend语义有效性继续分层；
- 持久化与并发：新增`symcc-string-backend-policy-v1`，按backend name保存global/
  context arm、round和updates；写入采用临时文件、fsync、原子rename，文件锁保护共享
  文件系统上的MPI多进程read-modify-write。context按last-round有界淘汰，损坏或缺失
  state fail-soft回到先验；
- 观测与门禁：worker metrics增加selected、skipped、updates、explorations和contexts。
  `solve_all_conformance`强制绕过学习选择，确保F198始终检查所有配置backend，而不是
  只验证当前winner；
- 自动化证据：unit覆盖冷启动逐arm探索、有效快速backend胜出、每轮严格max-backends、
  state重启恢复、conformance全backend旁路，以及只有concrete-valid SAT得到成功反馈。
  17个string unit、46项跨层回归通过；真实Z3+cvc5配置在`max_backends=1`时仍由门禁
  运行10个结果并全部concrete-verified；
- 边界：该策略是低维在线bandit，不是完整SMTgazer的X-means、censored PAR-2和
  sequence BO；共享状态只保证本机/共享POSIX文件系统一致性，尚未接入分布式CAS。
  Coverage reward目前不进入backend arm，因为未通过target的candidate不能反证solver
  语义能力；公开benchmark需分别报告调用节省、wall time和coverage/CPU。当前等级
  I/T/E。

## 195. F200：Width-Bound Unsigned strtoul and Overflow-Proof Decimal Folds（2026-07-28）

- 研究问题：F197只覆盖signed `strtol`，且F196/F197 runtime fold仅证明当前concrete
  witness不溢出；同长度symbolic alternative仍可能越界，使BV回绕与libc的undefined
  `atoi`域或`strto*` ERANGE饱和语义不一致；
- 严格`strtoul`子域：compiler新增`strtoul` interception。Runtime只在endptr为NULL、
  base concrete 10、参数本身concrete、1--20位纯ASCII digits、无ERANGE、手工
  overflow-safe fold与libc结果一致时建立symbolic return和artifact。`-`/`+`、base
  0/2--36、non-null endptr、whitespace、prefix/suffix全部保守回退；
- 无符号artifact：`symcc-string-operation-v1`新增`strtoul10`，允许32/64-bit
  `integer_bits`和`0..2^bits-1` observed value；writer用单一共享去重/配额状态和
  显式unsigned输出，避免64-bit `ULONG_MAX`经signed窄化；
- Query contract：unsigned decimal regex与`str.to_int`同时约束`0..2^bits-1`和
  different observed value。通用to-int常量边界扩为有界64-bit unsigned范围，
  concrete evaluator同样拒绝sign、非digits和width overflow；
- 逐位溢出证明：atoi、strtol和strtoul的每个digit fold都新增
  `acc < limit/10 || (acc == limit/10 && digit <= limit%10)` path guard；signed
  strtol依据当前固定sign选择`LONG_MAX`或`abs(LONG_MIN)` limit。由此runtime BV
  expression只在与libc一致的defined/non-ERANGE子域内使用；
- 自动化证据：unit以`ULONG_MAX`证明原值被alternative排除、`ULONG_MAX-1`接受、
  20位全9 overflow拒绝。真实instrumented测试导出唯一`strtoul10(456)`，negative
  input、base16/non-null-endptr不误导出；测试先清理自己的append artifact并连续
  两次通过，证明幂等。18个string unit、47项跨层回归、Z3/cvc5各6/6 conformance、
  compiler/runtime重编译和diff checks通过；
- 边界：尚未建模sign的C unsigned negation、endptr消费memory effect、base
  autodetection/2--36、locale whitespace、prefix/suffix和errno分支。逐位guard只
  保证当前严格domain，不把域外调用转成符号返回。当前等级 I/T/E。

## 196. F201：Parser-State Structural Trace and Nonterminal-Aware Grammar Context（2026-07-28）

- 研究问题：F192的独立parser只有布尔exit status，冲突context仍由branch、长度桶和
  lexical邻域启发式构造；相同字节位置可能位于不同nonterminal，同一词法context中的
  不同rule也可能触发不同parser state，全局一对一映射会产生错误屏蔽；
- 版本化trace：`SYMCC_PROPOSAL_PARSER`新增可选`{trace}`占位符。Parser写出
  `symcc-parser-structural-trace-v1`，绑定parser实现标识、accepted verdict及最多
  4096个`symbol/state/[start,end)/parent`节点；文件限制1 MiB，label必须bounded
  printable ASCII；
- 严格验证：accepted必须与process exit code一致，节点span必须落在candidate内，
  parent必须先出现并包含child。Grammar proposal显式携带candidate replacement span；
  manager选择覆盖该span的最小节点。成功退出但trace损坏、verdict不一致或没有enclosing
  node都以`parser-trace-invalid` fail closed；
- Structural identity：context hash包含parser implementation与selected node的完整
  bounded ancestor `(nonterminal,state)`链，不包含易过拟合的绝对offset。Proposal
  state保存context ID、symbol/state、trace SHA-256和node count，但验证后删除临时
  trace；
- 冲突拆分：semantic state升级schema v6并兼容v1--v5，持久化
  `(grammar_rule_id, lexical_context_id) -> parser_context_id`别名，最多4096条。
  下一次同rule/source context直接按真实结构context查询rejection；不同rule不能相互
  覆盖别名。零宽deletion在offset 0也用显式span-present位区分旧版缺失字段；
- 三层反馈：target/branch未通过时仍不运行parser；parser rejection可以携带合法错误
  state用于局部学习，但candidate不能进入coverage triage；parser acceptance也不替代
  AFL novelty。MPI反馈优先使用本次record的parser context，并保留source lexical
  context建立alias；
- 自动化证据：proposal-manager测试覆盖两层node中最小`value/string`选择、trace
  digest/state持久化，以及exit 0/accepted false mismatch拒绝。Semantic集成测试从
  真实grammar proposal span运行拒绝trace，两次反馈后按`field_value/after-equals`
  context局部屏蔽，重启仍保持rule-scoped alias。17项semantic/proposal、32项含
  QueryStore回归、54项扩展相关回归和Python/diff checks通过；
- 边界：框架不从任意binary自动恢复parser/CFG，trace由campaign配置的可信parser
  instrumentation提供；当前使用最小enclosing node和ancestor path，不含完整ECT
  structural path、recursive production induction或parser增量复用。公开benchmark仍需
  评估额外parser/trace成本、validity、rule coverage和coverage/CPU。当前等级 I/T/E。

## 197. F202：Structural Rule-Coverage-Aware Grammar Scheduling（2026-07-28）

- 研究问题：F187只把coverage features累计到rule全局；F201得到parser structural
  context后若仍按全局rule score排序，会反复生成已覆盖nonterminal中的高收益规则，
  无法区分“规则有效”与“该规则/结构组合仍有探索价值”；
- Coverage state：每条`LearnedGrammarRule`新增最多256个
  `parser_or_lexical_context -> coverage feature count`。MPI只在proposal通过target、
  parser gate并经AFL novelty保留后，用record的parser context（无trace时退回lexical
  context）回灌delta；
- 调度：solve-complete与Query-IR hole在每个span内重新按
  `global rule score + 0.5/sqrt(1+context_features)`排序。未覆盖组合获得有限探索奖励，
  随feature证据单调衰减；validity/retention/parser validity的原排序信号仍保留；
- 状态边界：context map达到上限时淘汰coverage最低且ID稳定排序最小项；计数饱和到
  signed 31-bit。Retention API新增`retained < verified`前置条件，拒绝无验证证据或
  重复消费同一次verified observation，保持重启校验不变量；
- 自动化证据：结构context集成测试记录7个features，证明已覆盖context的score低于
  未覆盖context并在schema v6重启后恢复；先建立verified证据、再执行parser局部拒绝，
  证明coverage统计不解除冲突屏蔽。32项semantic/proposal/QueryStore回归、54项扩展
  相关回归及Python/diff checks通过；
- 边界：该计数使用AFL全局feature delta，不是语法产生式自身的独立coverage bitmap；
  未做edge/data/string feature的多目标Pareto调度，也未证明inverse-square-root系数最优。
  需在公开parser目标消融global-only、structural novelty与随机rule选择。当前等级
  I/T/E。

## 198. F203：Recursive Parser Production Induction and Independent Grammar Bitmap（2026-07-28）

- 研究问题：F201只把parser树压缩为最小nonterminal的context，F202又以AFL新特征
  近似“语法覆盖”。前者丢失`A -> prefix A suffix`递归产生式，后者把程序coverage
  与grammar production exercise混为一谈，不能回答规则是否探索了新的结构路径；
- Production IR：在已经通过大小、label、parent/span和exit/verdict检查的
  `symcc-parser-structural-trace-v1`上，从replacement最小enclosing node向祖先搜索
  最近的、恰有一个同symbol direct child的节点。以parser实现、LHS/state、按byte span
  排序的RHS `(symbol,state,recursive-slot)`和terminal-gap SHA-256构造稳定
  `symcc-parser-production-v1` ID；超过32层祖先、direct sibling重叠均fail closed；
- 有界递归归纳：只有一个明确self-recursive slot、slot外至少一个byte且两侧总计不
  超过128 bytes时，保存concrete prefix/suffix和观察到的递归深度。Parser接受后，
  semantic generator学习稳定`recursive` rule；应用语义是对当前token执行一次
  `prefix + token + suffix` splice。每proposal只展开一层并继续受input/rule预算、
  target replay、parser gate和AFL triage约束；
- ECT式结构统一：proposal-state schema升级到v3，保存selected structural path ID、
  production ID/LHS/state/arity、recursive depth和wrapper。Semantic-state升级到v7，
  兼容v1--v6，并分别持久化`(rule, lexical context) -> structural path`与
  `-> production`的rule-scoped alias；
- 独立grammar coverage：新增逻辑64K count bitmap，以
  `(rule ID, structural path ID, production ID)`哈希索引。Parser trace完成一次合法
  production exercise即更新，计数8-bit饱和；稀疏state同时保存首个owner digest、
  event和collision计数。它不读取AFL edge/data delta，因此与F202 retained coverage
  是两个独立目标；
- 联合调度：每span排序在F202全局质量与AFL结构收益之外，再加
  `0.35/sqrt(1+grammar_count)`。已解析过的rule/path/production组合奖励递减，未知
  production仍保留探索奖励；parser rejection仍进入冲突屏蔽且绝不会诱导recursive
  rule，只有parser acceptance能扩充生成语法；
- 状态防护：production/context/owner均需64位hex digest；bitmap最多65536个槽，
  counters、events和collisions有界；规则kind使用显式allowlist。加载时复检production
  字段一致性、wrapper hex/128-byte上限、`recursive_depth <=> nonempty wrapper`和
  原有verified/retained漏斗不变量；
- 自动化证据：proposal-manager测试以`expr -> '(' expr ')'`三层树证明最小`value`
  context与ancestor recursive production同时提取，prefix/suffix为`28/29`、depth为2，
  v3重启保持ID。Semantic测试证明parser接受归纳`recursive` rule、生成`(x)`、
  独立bitmap计数和v7 aliases/rule跨重启恢复。19项semantic/proposal、34项含
  QueryStore、56项扩展相关回归、Python compile和diff checks通过；
- 边界：当前只归纳单一direct self-recursive slot，不处理mutual recursion、多recursive
  slots、epsilon recursion、跨parser symbol校准或完整probabilistic CFG。64K bitmap
  允许哈希碰撞并显式计数，但不提供无碰撞proof；edge/data/string/grammar的Pareto
  权重仍需公开benchmark校准。当前等级 I/T/E。

## 199. F204：Bounded Multi-Slot/Mutual CFG Fragments and Derivation Search（2026-07-28）

- 研究问题：F203只接受一个direct self-recursive child，并把一个wrapper直接挂到
  token rule；它无法表达`Expr -> Expr ',' Expr`的两个递归槽，也无法从
  `A -> B -> A`识别互递归。规则去重后若只保存一个cycle provenance，还会覆盖不同
  production导出的同形wrapper；
- 技术依据：Lase以syntax-rule coverage驱动online grammar search，Cottontail以ECT
  表示比branch-only路径更完整的结构进度，Arvada通过parse-tree bubbling处理高度递归
  grammar。F204采用可信parser tree上的bounded CFG fragment，不引入未经parser验证的
  oracle泛化。参考：[Lase/OOPSLA 2026](https://zbchen.github.io/files/oopsla2026.pdf)、
  [Cottontail/S&P 2026](https://mboehme.github.io/paper/SP26-cottontail.pdf)、
  [Arvada](https://arxiv.org/abs/2108.13340)；
- Canonical CFG fragment：proposal manager沿replacement ancestry提取最多32个
  去重production；每个production包含parser、LHS/state、ordered RHS
  `(symbol,state,direct-recursive)`和`arity+1`个terminal-gap SHA-256。Canonical
  JSON `symcc-parser-cfg-fragment-v1`自身再绑定SHA-256，proposal state升级v4；
- Cycle extraction：每个direct same-symbol child独立形成slot，因此多槽production
  不再拒绝；ancestry中任意重复nonterminal形成bounded cycle path，故
  `A/state0 -> B/state1 -> A/state2`被识别为mutual cycle。Cycle ID绑定parser、
  outer production、slot和完整`(symbol,state)` path；prefix/suffix是outer span相对
  descendant span的concrete context，总计不超过128 bytes且不得同时为空；
- 双层复检：semantic generator不直接相信proposal record，而是重新解析fragment、
  复算每个production/cycle ID、检查path首尾LHS、slot/arity、digest、label、大小和
  32/16预算。只有parser acceptance且整个fragment通过二次验证才合入最多4096个
  production/cycle的持久图；tampered JSON/digest不学习任何cycle；
- Derivation search：recursive rule在一个observation内生成depth `1..N`的独立
  candidate，`SYMCC_GRAMMAR_DERIVATION_DEPTH`默认3、范围1--8。每层执行真实
  prefix/suffix composition并立即检查全局input size；epsilon cycle不进入rule。
  这允许`<x>`继续生成`<<x>>`和`<<<x>>>`，但不会执行无界递归；
- Provenance与覆盖：semantic state升级v8并兼容v1--v7，保存canonical productions、
  direct/mutual cycles、multi-slot count及`recursive rule -> {cycle IDs}`一对多关系。
  同形wrapper可去重成一个rule，但所有cycle来源都保留；F203的独立64K
  rule/path/production bitmap继续作为derivation排序信号；
- 自动化证据：proposal测试以`Expr -> Expr ',' Expr`证明两个slot均进入fragment、
  每个cycle声明`recursive_slots=2`；semantic集成测试从真实parser subprocess的
  `A -> B -> A`树提取mutual path，生成depth 1/2/3候选，验证production/cycle/
  one-to-many provenance跨v8重启恢复，并证明追加一个byte的tampered fragment无法
  学习递归rule。22项semantic/proposal、37项含QueryStore回归、全量319项Python与
  4项subtest通过；
- 正确性边界：这是沿一个replacement ancestry观察到的bounded CFG fragment，不是
  全parser grammar；mutual cycle必须在同一trace中闭合。当前template固定未替换
  sibling的concrete bytes，尚不做多nonterminal同时展开、epsilon production、
  ambiguity/SPPF、subtree correspondence或PCFG概率估计。下一阶段是incremental ECT
  subtree matching和多目标Pareto调度。当前等级 I/T/E。

## 200. F205：Incremental ECT Subtree Correspondence and Verified Substitution（2026-07-28）

- 研究问题：F204只用terminal-gap digest认证production，并以cycle template执行递归；
  digest无法恢复一个已接受subtree的concrete yield，也不能判断两个candidate中的
  production是否只有终结符实例不同。因此它不能执行Cottontail式增量结构复用或
  Arvada式同形subtree替换；
- 表示分层：`symcc-parser-cfg-fragment-v2`为每个production增加
  `symcc-parser-production-shape-v1` ID。Shape绑定parser、LHS/state和ordered RHS，
  但有意排除terminal-gap内容；原production ID仍绑定全部gap digest。最多32个
  `symcc-parser-subtree-instance-v1`再绑定production/shape、完整yield SHA-256、
  root-to-node ancestry path、concrete yield和逐gap bytes。这样“结构对应”与“具体
  语法实例”不再混为一个哈希；
- 可信边界：只有target replay和独立parser均接受后，semantic层才解析v2 fragment，
  并重新计算shape、production、instance、yield和每个gap hash；path末端必须等于
  production LHS/state。单yield与gap总量各不超过256 bytes、fragment仍不超过64 KiB。
  错误outer digest、内部yield修改、gap不一致或持久化引用悬空均fail closed；
- 增量correspondence：semantic state v9维护最多4096个shape、8192个instance和
  shape-to-instances equivalence class。相同shape下具有不同yield的instance才计为
  correspondence class。每个instance学习显式`subtree` rule，并保留
  `rule -> {shape IDs}/{instance IDs}`多对多provenance；
- Context-safe substitution：只有一个lexical source/candidate context已由accepted
  selected instance映射到对应shape时，该shape的subtree rule才进入候选集。错误branch/
  邻域context在生成前被过滤；产生的proposal携带shape/instance ID，并仍需经过
  target、parser和AFL novelty三层验证。这不是跨nonterminal的无条件字节字典；
- Incremental chain：manager按candidate的branch、input-size bucket和replacement
  两侧16-byte邻域计算candidate-local context。Source context与accepted-output
  context都映射到selected shape，因此被保留的candidate可作为下一轮source继续复用，
  而无需重新泛化整个parser grammar；
- 长期运行与降级：proposal state升级v5，semantic state升级v9并兼容旧状态。CFG
  production、cycle、shape、instance和rule provenance采用引用一致的级联驱逐；达到
  64 KiB时优先确定性移除非selected instance，必要时放弃instance增强证据而尽量保留
  F204 CFG/cycle，不因F205可选信息拒绝原本合法的结构反馈。加载proposal state时还
  重新哈希content-addressed candidate和candidate context；
- 自动化证据：真实parser subprocess分别接受同一`value/atom` shape的`A`与`B`，
  得到不同instance并形成两个可执行subtree alternatives；错误branch context不生成，
  v9重启保持shape/instance/rule correspondence。测试重算outer digest后篡改内部
  `yield_hex`，semantic层仍拒绝学习；32层长label树证明64 KiB超限时裁剪instance且
  保留selected evidence。24项semantic/proposal、39项含QueryStore定向回归、全量
  321项Python与4项subtest通过；
- 科研定位与边界：这是observed ancestry上的incremental ECT correspondence，不是
  完整Cottontail expression tree、SPPF或parser增量算法。Shape严格包含parser ID和
  state，故不会擅自跨parser对齐；当前不替换多个nonterminal槽、不支持epsilon yield、
  ambiguity forest、PCFG概率或全局Pareto corpus replacement。下一阶段是把edge/data/
  string/grammar/ECT novelty纳入显式多目标Pareto frontier，并做公开parser目标等CPU
  消融。参考：[Cottontail/S&P 2026](https://mboehme.github.io/paper/SP26-cottontail.pdf)、
  [Lase/OOPSLA 2026](https://zbchen.github.io/files/oopsla2026.pdf)、
  [Arvada](https://arxiv.org/abs/2108.13340)。当前等级 I/T/E。

## 201. F206：Seven-Objective Pareto Grammar/ECT Scheduling（2026-07-28）

- 研究问题：F202--F205依次把AFL structural yield、grammar bitmap和ECT instance接入
  排序，但最终仍是固定系数加权和。加权和会把互有优势的规则压成一个标量，不能保留
  non-convex trade-off；新规则、未触发String的规则还可能因“缺数据等于零收益”被错误
  支配；
- 七维目标：每个rule/context分别最大化target validation posterior、AFL retained
  edge-feature yield、dynamic data-comparison progress、独立concrete-verified String
  model yield、`rule/context/production` grammar novelty、matched-shape ECT novelty和
  scheduling age。Data由`SolverTelemetry.data_features`的matched/width质量反馈；
  worker把本次`.string_solver_metrics.json`中的query/verified/dual-view计数并入
  telemetry，绝不把SAT但未通过concrete evaluator的model记为String成功；
- 缺失观测语义：data与String采用0.5的bounded Bayesian neutral prior，未出现相关
  operation不被当作成功或失败。Edge objective结合retention posterior与每attempt的
  feature rate；grammar objective使用独立64K bitmap而非AFL delta；ECT objective只对
  已认证matched shape的subtree rule计算；
- Bounded Pareto archive：每个source context先对全部bounded grammar rules计算目标，
  按七个维度的极值round-robin保留最多256个arm，避免在8192-rule硬上限执行无界
  O(n²)。在archive内执行精确non-dominated sorting；同一front按每维归一化crowding
  distance排序，优先保留相互远离的trade-off而非重新求加权和；
- 公平性修复：初版测试发现parser局部冲突解除后的低收益rule可能长期位于后续front。
  F206增加持久化`last_attempt_observation`和age objective；刚执行rule的age归零，
  等待rule随observation增长成为不可被近期rule支配的极端。该机制提供bounded
  starvation resistance，而不虚构coverage收益；
- 状态与消融：`SYMCC_GRAMMAR_PARETO=1`默认启用，设0恢复F202/F203 scalar排序作为
  实验baseline。Semantic state升级v10并兼容v1--v9，保存per-rule data quality、
  String query/verified、last-attempt与global frontier/ranking计数；加载复检finite
  quality、`quality_sum <= observations`、`verified <= queries`和时间上界；
- 自动化证据：构造一个edge-yield极端、一个data-progress极端和一个被二者支配的
  rule，证明前两者同处front 0且低质量rule进入后续front；真实telemetry mapping证明
  data quality与2/3 verified String yield跨v10重启恢复。原parser rejection测试进一步
  证明age objective使解除冲突的rule重新获得proposal预算。26项semantic/proposal、
  41项含QueryStore、81项含hybrid-feedback定向回归、全量323项Python与4项subtest
  通过；C++ build、2项String lit及真实Z3/cvc5各6/6 conformance通过；
- 科研边界：这是contextual Pareto arm scheduling，不是整个corpus的多目标
  replacement proof。当前edge反馈仍是AFL feature delta而不是按edge family拆分；
  String objective仅在配置了artifact/cross-solver验证时有观测；frontier的公开目标
  coverage/CPU、hypervolume、invalid ratio和各目标消融尚需F67 confirmatory protocol。
  下一阶段是Pareto corpus ownership/replacement与公开parser campaign，而不是继续
  人工调权。当前等级 I/T/E。

## 202. F207：Epsilon-Pareto Corpus Metadata Ownership and Replacement（2026-07-28）

- 研究问题：F206只在grammar rule层保留多目标trade-off；主seed调度仍由LinUCB标量
  reward和`DataCoverageTracker`逐feature winner组成。后者能回答“谁是某个data
  feature最优”，却不能决定edge、data、String、结构、path和成本互有优势时哪些seed
  应继续占用bounded replay metadata；
- 六维archive：`ParetoCorpusArchive`对每个已执行path累计AFL edge-feature delta、
  data coverage refinement bits、concrete-verified String query yield、branch-trace
  structural sites、stable path hash和reward/cost efficiency。严格全目标不差且至少
  一维更优才构成dominance；缺String observation继续使用neutral posterior；
- Admission/replacement：若任一entry支配新seed，拒绝其metadata admission；若新seed
  支配已有entry，则级联移除这些dominated metadata。容量默认为4096、硬范围8--65536，
  不在全量entry上反复执行O(n²) front排序；
- Epsilon density：容量溢出时把六维目标量化到2--32 bins。每个目标至少保护一个全局
  extreme，然后从最拥挤的未保护cell选择总效用最低的victim；这在bounded cost下近似
  保持frontier diversity。Grid只参与capacity tie-break，不改变strict dominance；
- 系统接入：`AdaptiveHybridScheduler.observe`在DataCoverage delta和统一reward已确定
  后更新archive；`score`只给archive member一个有界priority bonus。驱逐仅删除SymCC
  replay/scheduling metadata，不删除、改名或覆盖AFL queue/crash文件，也不改变
  `CoverageBitmap`的权威ownership；
- 状态与开关：`SYMCC_PARETO_CORPUS`默认1，
  `SYMCC_PARETO_CORPUS_ENTRIES`默认4096，`SYMCC_PARETO_CORPUS_BINS`默认8。
  Adaptive scheduler state升级v12并兼容v1--v11，持久化entry evidence、admission/
  rejection、dominance/density eviction计数；加载复检path长度、finite reward/cost、
  String漏斗和logical observation time；
- 自动化证据：edge、data和String三个互不支配extreme同时保留，零收益/无path evidence
  seed被拒绝；一个在六维均更优的`super` seed替换三个旧entry。Round-trip测试证明
  archive counters/evidence恢复；scheduler集成测试证明edge=3、String=1/2和state v12
  跨重启保持。83项hybrid/semantic/proposal/QueryStore定向回归、全量325项Python与
  4项subtest、C++ build和2项String lit通过；
- 科研边界：这是metadata archive而不是物理AFL corpus minimizer，故不会减少AFL自身
  queue磁盘规模；当前结构目标使用site diversity而非完整ECT hypervolume，edge目标
  使用新feature数量而非稀有度直方图。公开实验仍需比较no archive、code/data winner、
  scalar utility与epsilon-Pareto，并报告archive churn、front hypervolume、coverage/
  CPU和内存。下一阶段可把archive admission接到distributed coverage ownership
  consensus，但在证明跨master线性化前不能删除共享queue文件。当前等级 I/T/E。

## 203. F208：Packed Parser Forest Families and Verified Epsilon Derivation（2026-07-28）

- 研究问题：F204/F205只能消费一棵非空span tree，因此无法表示同一nonterminal的多个
  parser-supported RHS，也会把合法的零宽归约排除在ECT之外。直接允许空字节规则又会
  产生无上下文删除器；直接遍历GLR/SPPF全部节点则可能把未被当前parse选择的备选节点
  错认成replacement context，并导致产生式和instance证据无界膨胀；
- Trace v2协议：新增`symcc-parser-structural-trace-v2`。`epsilon`必须与
  `start == end`严格等价，epsilon节点不得有children。每个节点可声明最多8个
  `alternatives`，每个alternative最多64个有序、互不重叠的direct-child index；
  alternative列表必须唯一，其并集必须恰好覆盖全部direct children。第0项是parser
  选中的concrete tree，其余项只是packed evidence；
- 主树定位修复：replacement最小enclosing node只在从roots沿alternative 0可达的
  primary nodes中选择。非主分支不能因相同span或更晚node index劫持context；它们只在
  所属primary ancestor处形成额外RHS family。这一约束把“当前执行树”与“可能语法”
  分离，也是后续context-safe生成的必要条件；
- Canonical family：v2 trace使用`symcc-parser-production-v2`和
  `production-shape-v2`，二者都把epsilon语义纳入hash；packed ordinal不进入
  production ID，只作为证据元数据，故语义相同的备选会规范化合并。
  `symcc-parser-cfg-fragment-v3`先保留最多32层primary ancestry production，再按
  parser/node/alternative确定性顺序填充非主family到总计32条；cycle只引用primary
  production，instance按ID去重并继续受32项、单yield/gap 256 bytes与总64 KiB限制；
- Verified epsilon：只有empty RHS、唯一empty terminal gap、empty concrete yield、
  production/shape/instance全部hash一致的epsilon evidence才可进入semantic图。
  Semantic层在target replay和独立parser均接受后学习body为空的`epsilon` rule；
  rendering是对当前source token执行零字节splice，即删除，而不是插入不受约束的
  空节点；
- ECT上下文门：epsilon rule与普通subtree rule共享`rule -> shapes/instances`和
  lexical-context-to-selected-shape映射。错误branch/邻域context在生成前被过滤；
  proposal仍携带零宽candidate span并重新经过target、parser和AFL novelty三层门禁。
  Pareto ECT objective也把epsilon视为结构rule，而不是给予固定neutral分；
- 状态与兼容：proposal state升级v6，新增packed alternative/epsilon production
  计数并在加载时与v3 fragment复核；semantic state升级v11，持久化v2 production、
  empty instance、epsilon rule和全部引用关系。Proposal v1--v5、semantic v1--v10、
  parser trace v1与CFG fragment v1/v2继续兼容；
- 自动化证据：真实parser subprocess输出`choice -> empty | token` packed trace，
  验证4条production、1条非主family、1条epsilon production和selected empty
  instance；错误child union fail closed。构造16层、每层8个备选的forest证明primary
  优先和32-production确定性上限。跨层测试证明epsilon rule只在认证shape context
  生成空candidate，错误branch不生成，v11重启恢复；修改selected empty yield并重算
  outer digest仍被二次验证拒绝。30项semantic/proposal定向测试、全量329项Python与
  4项subtest、C++ build、2项String lit及Z3/cvc5各6/6 conformance通过；
- 科研定位与边界：F208实现的是沿一个已选择parse ancestry的bounded packed
  production families，不是共享节点/边的完整SPPF、generalized parsing算法或
  probabilistic CFG。跨alternative的深层subtree correspondence、多个nonterminal
  同步替换、nullable-cycle fixed point、production probability/inside-outside
  估计、跨parser symbol校准和公开parser campaign仍未完成。所有性能提升结论在F67
  equal-CPU confirmatory实验前保持E级。参考：
  [Lase/OOPSLA 2026](https://zbchen.github.io/files/oopsla2026.pdf)、
  [Cottontail/S&P 2026](https://mboehme.github.io/paper/SP26-cottontail.pdf)、
  [Arvada](https://arxiv.org/abs/2108.13340)。当前等级 I/T/E。

## 204. F209：Shared Packed DAG and Deep Alternative ECT Correspondence（2026-07-28）

- 研究问题：F208的v2 alternatives仍以单parent nodes表示，本质是把多棵树压在一个
  列表中，不能证明两个derivation真正共享同一个forest node；production也只沿选中
  ancestry抽取，非主分支下一层的subtree不会进入ECT。简单递归全部备选又会出现
  exponential tree unfolding、重复instance和无界provenance；
- V3 shared-DAG trace：新增`symcc-parser-structural-trace-v3`。Node不再声明
  `parent`，而由`roots`和alternative child indexes形成DAG；child必须严格位于更大的
  node index，故无须相信parser的cycle声明即可证明有限无环。每个alternative仍要求
  span包含、有序、内部不重叠；最多4096 nodes、16384 edge occurrences和1 MiB；
- Concrete-tree约束：`roots[0]`和每个reachable node的alternative 0构成当前parser
  选中的concrete tree。该子图要求单父；replacement context只在此子图定位。共享
  node可被不同非主alternative、不同非主parent或主/非主边共同引用，但不能制造两个
  current-tree parents。这保留SPPF sharing而不把ambiguity误当作当前execution；
- Bounded closure：提取器先无条件保留最多32层selected ancestry，再从这些节点按
  确定性BFS展开所有alternatives，闭包最多32 nodes。Production总预算仍是32：
  ancestry primary优先、再保留deep node primary、最后填充alternative families。
  Graph edge先保留每个node的一条完整root path，再填充到128 edges，因此每个保留
  instance都有可复检的derivation path；
- Canonical graph evidence：`symcc-parser-cfg-fragment-v4`增加
  `symcc-parser-packed-node-v1`和`packed-edge-v1`。Node ID绑定parser、原trace order、
  symbol/state、span、epsilon和yield digest；edge ID绑定parent/child、alternative和
  slot。`roots`、`selected_path`和`truncated`区分完整forest与有界projection；
  总fragment继续受64 KiB限制；
- Deep ECT instance：`symcc-parser-subtree-instance-v2`在F205的production/shape/
  yield/path上再绑定`node_id`与完整`node_path`。这使相同production/yield位于不同
  shared-node provenance时不会错误折叠instance，同时shape equivalence仍能跨node和
  candidate聚合；
- 二次验证与执行边界：semantic层复算全部node/edge/production/shape/instance ID，
  检查topological order、span containment、root无incoming edge、alternative slot、
  selected path全为alternative 0、每个instance path edge存在且symbol/state/yield与
  node一致。Non-primary deep instance可学习subtree rule，但只有source context已经
  被selected instance认证为同一shape时才可生成，故packed evidence不能自行授权；
- 长期运行：proposal state升级v7，记录packed node/edge计数并与v4 fragment复核。
  Semantic state升级v12，保存最多8192 nodes、32768 edges和v2 instances；node驱逐
  删除所有incident edges及含该node的instances，edge驱逐在不存在平行parent-child
  edge时级联删除失去完整path的instance，并沿F205引用图撤销rule shape/instance；
- 自动化证据：真实parser subprocess输出5-node/5-edge DAG，delimiter node在两个
  root alternatives间共享；非主wrapper下的deep `value("b")`与主树
  `value("a")`同shape。验证器保留完整selected/deep paths，semantic层在正确context
  生成`b`、错误branch不生成；v12重启恢复shared graph和生成能力。删除deep path edge
  会级联撤销instance，修改edge alternative并重算outer digest仍使整个fragment
  fail closed；primary graph非法sharing被拒绝。33项semantic/proposal定向测试、
  全量332项Python与4项subtest、C++ build、2项String lit及Z3/cvc5各6/6通过；
- 科研定位与边界：这是从独立parser消费的bounded SPPF projection，不是SymCC内部
  GLL/GLR parser，也不对被截断forest声称完整性。尚未实现nullable-cycle fixed point、
  packed-edge probability/inside-outside、跨多个nonterminal的同步substitution、
  增量parse cache或跨parser symbol calibration。公开benchmark必须报告forest
  truncation、shared-node ratio、deep-instance yield、invalid ratio、内存和
  coverage/CPU。参考：
  [SPPF transformation model](https://doi.org/10.14279/tuj.eceasst.73.1032)、
  [Lase/OOPSLA 2026](https://zbchen.github.io/files/oopsla2026.pdf)、
  [Cottontail/S&P 2026](https://haoxintu.github.io/files/sp2026-cottontail.pdf)、
  [Arvada/ASE 2021](https://arxiv.org/abs/2108.13340)。当前等级 I/T/E。

## 205. F210：Nullable SCC Least Fixed Point and Proof-Carrying Deletion（2026-07-28）

- 研究问题：F208只能从一个实际出现的零宽parse node学习epsilon，F209虽能表达共享
  forest，却有意保持node graph无环。现实CFG的可空性可通过`A -> B -> ... -> epsilon`
  传播，并可能穿过互递归SCC；把“位于循环中”直接等同于nullable会错误接受没有基例的
  `A -> B, B -> A`，而展开所有推导既不终止也无法形成可审计证据；
- V4协议：`symcc-parser-structural-trace-v4`保留v3的forward packed DAG，再附加最多
  32条`lhs -> rhs` nullable dependency rules；LHS/RHS元素都绑定bounded printable
  `(symbol,state)`，每个RHS最多8项。规则以parser、LHS和ordered RHS形成
  `symcc-parser-nullable-rule-v1` ID，因此不同parser/state不能无意合并；
- SCC与最小不动点：验证器在至多288个symbol/state节点上计算dependency reachability
  和强连通分量。空RHS给出depth 0基例；仅当一个rule的全部children已有证明时才给
  LHS生成`1 + max(child depth)`证明，并按`(depth, rule ID)`选择规范化最小证明。
  循环SCC有稳定`symcc-parser-nullable-scc-v1` identity，但SCC本身不提供可空性；
  纯循环证明集为空，有epsilon基例的互递归才收敛；
- Proof-carrying fragment：`symcc-parser-cfg-fragment-v5`同时携带排序后的rule、proof
  与cyclic SCC。Proposal manager保存每项计数并升级state v8。Semantic层不信任这些
  派生字段，而是重算完整certificate并要求逐项相等；修改proof depth后即使重算外层
  SHA-256也会整段fail closed；
- 增量全局语义：semantic state v13按parser合并至多4096条已验证规则，每次合入、
  production驱逐、instance驱逐或重启后重算全局least fixed point。持久文件中的proof/
  SCC只作审计输出，恢复权限完全由source rules重新推导，避免陈旧派生缓存继续授权；
- Context-safe执行：一个已存production只有在同parser的`(lhs,state)`进入nullable
  fixed point时，其shape才加入epsilon rule。生成仍要求source lexical context已由
  selected instance认证到同shape，随后还必须通过target replay、独立parser与AFL
  novelty。Packed graph仍是forward DAG；grammar-level SCC不会引入node cycle，也
  不能让非selected ambiguity evidence自行授权删除；
- 引用一致性修复：没有concrete epsilon instance的proof-backed shape可以在普通ECT
  instance淘汰后继续存在；其context alias和epsilon shape link由nullable certificate
  维持。状态加载延迟过滤shape alias，待productions、instances、rules全部恢复并重算
  fixed point后才移除真正悬空引用；
- 自动化证据：v4 subprocess给出`A -> B, B -> A, B -> epsilon`，验证v5 fragment含
  3 rules、2 proofs、1 SCC，深度分别为A=1/B=0；纯`A <-> B`保持0 proof/0 nullable
  shape/0 epsilon rule。Semantic集成证明正确context可删除、错误branch不可删除，
  proof depth篡改被拒绝，v8/v13重启及selected instance淘汰后能力仍一致。16项
  proposal、19项semantic聚焦测试、全量334项Python与4项subtest、原生build、2项
  String lit及Z3/cvc5各6/6通过；
- 科研定位与边界：这是由独立parser提供规则的bounded nullable certificate，不是
  SymCC内部的完整grammar recovery或增量GLR/GLL parser；当前证明只回答布尔可空性
  与最小rule depth，不计算推导数量、最小terminal cost或概率质量。下一阶段应实现
  多nonterminal同步substitution，再研究incremental parse cache和packed-edge
  inside/outside概率；公开实验需报告nullable proof命中、无效删除率、额外内存与
  coverage/CPU。参考：
  [SPPF transformation model](https://doi.org/10.14279/tuj.eceasst.73.1032)、
  [Lase/OOPSLA 2026](https://zbchen.github.io/files/oopsla2026.pdf)、
  [Cottontail/S&P 2026](https://haoxintu.github.io/files/sp2026-cottontail.pdf)。
  当前等级 I/T/E。

## 206. F211：Atomic Multi-Nonterminal ECT Transactions（2026-07-28）

- 研究问题：现有subtree rule只能替换一个已观察的完整yield，逐个替换parent的多个
  child slot则会把parser-invalid中间态送入queue；另一方面，对同shape child做无界
  Cartesian product会产生组合爆炸，也会把“结构相同”错误扩大成跨parent任意适配。
  F211目标是在一个candidate内原子改变至少两个nonterminal slot，并为组合来源提供
  可撤销的结构证书；
- 可信输入：不新增未经验证的parser hint，而是消费F209/F210已复检的parent
  production、alternative-0 packed edges、parent/child v2 instances和terminal gaps。
  Parent arity限定2--8；slot必须恰好为`0..arity-1`，每个child必须有同parser的primary
  production instance。先用`gap0 + child0 + ... + childN + gapN`重建source parent，
  与accepted parent yield逐byte相等后才进入组合搜索；
- Bounded synchronized search：建立一次性的`shape -> concrete yield -> canonical
  instance`索引。每槽最多保留2个不同accepted yield；一次transaction改变2--4槽，
  每个parent最多保留4项，全局最多4096项。结果超过256 bytes或已经是该parent shape
  的accepted yield会跳过，因此生成的是未观察组合而不是重复whole-subtree replay；
- Proof-carrying transaction：`symcc-parser-sync-transaction-v1`绑定parser、parent
  shape/production/instance、完整ordered edge sequence、每槽shape/source/target
  instance、changed bitmap、不变terminal gaps和result SHA-256。Transaction ID与
  `synchronized` rule一对多关联，同一结果可保留多个独立结构来源；
- 精确执行门：shape context仍是必要条件但不再充分。生成前还要求当前source token
  与certificate的source parent yield逐byte相等，因此一个为`a,c,e -> b,d,e`学习的
  两槽transaction不能套到`b,c,e`上退化为单槽变化。候选通过一次完整parent splice
  构造，没有可见中间态；随后仍经过target telemetry、独立parser和AFL全局novelty；
- 生命周期与状态：transaction是derived state。Semantic v14虽输出transaction用于
  审计，但加载时忽略该对象，从已复检graph重新计算；篡改持久化yield无效。Instance、
  edge、production或synchronized rule驱逐会级联重算/撤销引用。按shape/yield预索引
  避免每parent反复全图扫描；grammar容量淘汰同步删除transaction，不留悬空rule ID。
  Proposal state v9新增`grammar_sync_transaction_id`，只作为provenance而非parser
  acceptance替代物；
- 自动化证据：真实v3 parser subprocess分别接受`a,c,e`和`b,d,f`。三槽图导出未观察
  的`b,d,e`、`b,c,f`、`a,d,f`，每个candidate保持两个comma gap且相对其source精确
  改变两槽；错误branch/context不生成。测试复检slot edge/source/target identity、
  manager v9 provenance、parser replay、semantic v14重启、持久transaction篡改后
  重算，以及target child instance驱逐后对应transaction消失。16项proposal、20项
  semantic聚焦测试、全量335项Python与4项subtest、原生build、2项String lit及
  Z3/cvc5各6/6通过；
- 科研定位与边界：这是bounded CFG/ECT组合器，不是attribute grammar或一般
  context-sensitive compatibility solver。当前只保持concrete terminal gaps并依赖
  replay过滤跨slot语义相关性；未学习matching-tag/equality等relation，也不缓存
  candidate的incremental parse result。下一阶段是content-addressed incremental
  parse cache与局部forest invalidation，随后才适合做packed-edge inside/outside
  probability或relation-aware slot compatibility。参考：
  [Cottontail/S&P 2026](https://haoxintu.github.io/files/sp2026-cottontail.pdf)、
  [Lase/OOPSLA 2026](https://zbchen.github.io/files/oopsla2026.pdf)、
  [Arvada/ASE 2021](https://arxiv.org/abs/2108.13340)。当前等级 I/T/E。

## 207. F212：Content-Addressed Incremental Parser Cache Protocol（2026-07-28）

- 研究问题：F201--F211对每个grammar candidate都启动独立parser并重新解析完整输入；
  大部分transaction只改局部span，完整parse会重复处理稳定前后缀。Manager又不能把
  cached acceptance直接当作新candidate的acceptance，否则一次错误invalidation即可
  绕过语法门。F212因此把cache定义为parser性能输入，而非acceptance oracle；
- 协议接入：`SYMCC_PROPOSAL_PARSER`同时包含`{trace}`和`{cache}`时，manager为每次
  invocation写入`symcc-parser-incremental-cache-v1` manifest。Cold invocation仍
  传递明确mode；一个完整trace通过原有exit/verdict/tree/DAG/hash检查后，raw trace和
  candidate input才按SHA-256进入最多256项cache。Parser command完整tuple也进入cache
  identity，换parser实现或参数不会复用旧trace；
- Edit与失效：base由proposal `source_path`内容digest精确命中，不做相似hash猜测。
  Manager计算最长公共prefix/suffix，绑定offset/delete/insert bytes；delete+insert
  超过proposal patch budget即cold fallback。与edit或其两侧16-byte guard相交的node
  全部失效；完全位于公共prefix/suffix的node才映射新span，再逐byte验证base/candidate
  yield一致。最多提供512个reusable nodes和4096个invalidated indexes；
- 可复检receipt：parser仍必须输出完整v1--v4 trace，可选
  `symcc-parser-incremental-reuse-v1`只报告实际采用的`base_index -> candidate_index`。
  Manager复检manifest digest、索引唯一性、offered membership、symbol/state、
  mapped span、epsilon和candidate yield SHA-256。Receipt缺失表示full parse fallback，
  合法；错误digest或虚假node mapping使完整trace fail closed；
- 内容寻址与故障恢复：cache trace以`input_sha.trace.json`原子写入独立目录，state只
  保存path/digest/parser/schema/node count/last-used。每次reuse前重新读取并验证base
  input和trace digest；corrupt/missing entry被删除，本次自动转cold而非拒绝合法
  candidate。LRU-like `(last_used,digest)`确定性驱逐不触碰AFL corpus文件；
- 状态与遥测：proposal state升级v10并兼容v1--v9。每record记录manifest/base digest、
  offered invalidation数、verified reused node数和hit；snapshot汇总cache entries、
  requests、incremental offers、hits、reused与invalidated nodes。Retry前清零本次字段，
  防止旧receipt计数污染新的parser invocation；
- 自动化证据：64-byte v3 parser fixture将middle edit之外的left/right两node复用，
  验证cold首轮、2 reused/2 invalidated hit、state v10重启后再次hit。错误receipt
  digest在exit 0下仍被拒绝为`parser-trace-invalid`；手工破坏base trace后下一轮cold
  fallback仍通过完整parser并撤销坏entry。17项proposal、20项semantic聚焦测试、全量
  336项Python与4项subtest、原生build、2项String lit及Z3/cvc5各6/6通过；
- 科研定位与边界：F212实现的是parser-neutral incremental cache contract与verified
  reuse accounting，不是内置tree-sitter/GLR增量算法；fixture能证明协议真实触发，
  不能证明wall time下降。外部parser必须实际消费manifest才能获得性能收益。下一阶段
  可在该协议上加入packed-edge empirical probability与inside/outside调度，同时以
  parser CPU、reuse ratio、invalidated frontier、trace size和coverage/CPU做消融。
  参考：[Cottontail/S&P 2026](https://haoxintu.github.io/files/sp2026-cottontail.pdf)、
  [Tree-sitter incremental parsing](https://tree-sitter.github.io/tree-sitter/using-parsers/3-advanced-parsing.html)、
  [SPPF transformation model](https://doi.org/10.14279/tuj.eceasst.73.1032)。
  当前等级 I/T/E。

## 208. F213：Accepted-Forest PCFG Posterior and Inside/Outside Scheduling（2026-07-28）

- 研究问题：F209已经保存bounded shared packed DAG，但F206仍把所有已认证production
  视为等价的结构候选。直接按出现次数排序会永久压制稀有新规则；把低频规则剪掉又会
  破坏coverage exploration。F213需要从独立parser已经接受的concrete tree学习稳定
  概率，同时在完整packed alternatives上计算reachability mass，并且只改变预算顺序，
  不能把概率变成语法许可或拒绝条件；
- 可信观测：每个v4/v5 CFG fragment只沿`roots[0]`和alternative 0的完整edge groups
  计数。只有slot恰好为`0..|RHS|-1`、production/instance均已在semantic层重算通过的
  节点才能贡献一次观测。Fragment SHA-256是去重键；最多8192个键，达到上限后停止
  学习新计数而不驱逐旧键，因此已计数fragment不会因窗口轮换被重复计数。Non-primary
  ambiguity只参与后续动态规划，不伪装成实际接受样本；
- Bayesian PCFG：production family由`(parser,LHS,state)`规范化为
  `symcc-parser-production-family-v1`。对同family的shape使用Jeffreys型对称
  Dirichlet先验`alpha=0.5`：
  `P(r|D)=(count(r)+0.5)/(sum count+0.5*|R|)`。未观察但已由可信CFG发现的shape
  因此得到非零概率，而不是错误的0.5默认值；原始计数与fragment选择证据持久化，
  posterior始终重算；
- Canonical hyperedge：parser的alternative ordinal只在一棵forest内部有意义。
  F213把ordinal绑定到concrete ECT instance用于观测，却按
  `(node,production,ordered child IDs)`形成v2 probability hyperedge用于全局推断。
  这避免同一production在不同样本中从主分支变为备选分支时被后一次覆盖，也会合并
  仅ordinal不同但child derivation相同的重复边；
- Inside/outside：对forward DAG逆拓扑计算
  `inside(v)=sum_a P(rule_a)*product_c inside(c)`，再从显式root membership正拓扑传播
  outside contribution。Root身份单独持久化，因为内容寻址节点可能在一棵forest是根、
  在另一棵是内部节点；仅从全局union的incoming edges推断会漏失root mass。
  `symcc-parser-packed-root-evidence-v1`把accepted fragment digest、parser和root node
  绑定为规范ID；v15只从复检证据恢复，v14迁移才使用union frontier并标记
  `legacy-frontier`。所有derived mass限制在`[0,1]`并在edge/node/production驱逐或
  重启后重算，不信任状态文件中的派生表；
- 九维调度：F206的七维Pareto增加PCFG posterior likelihood与information/reachability
  两维。后者把`1/sqrt(1+count)`与outside influence组合，既保留高概率结构的有效预算，
  又给低样本但可从root到达的shape探索机会。概率只进入non-dominated ranking；任何
  候选仍必须经过target replay、独立parser acceptance和AFL novelty，低概率永不被
  soundness pruning；
- 生命周期与状态：semantic state升级v15并兼容v1--v14，保存bounded family/count/
  observation/root evidence。加载时重算family ID，只接受仍有已验证production的
  shape、正整数饱和计数和不超过计数的observation tally；负数/伪造shape被忽略。
  CFG驱逐会裁剪无活动family/shape但保留空fragment去重标记，长期内存仍受4096
  production与8192 observation预算约束；
- 自动化证据：真实v3 parser subprocess对`L0/L1/L2/R0`产生同一root的左右两条
  alternatives，并让接受分支在每个trace中成为alternative 0。3:1观测在
  `alpha=0.5`下精确得到0.7/0.3；4棵forest的root inside均为1，左右hyperedge
  outside分别为0.7/0.3。测试还覆盖未观察shape的0.125平滑、九维Pareto、重复fragment
  幂等、v15恢复、负计数/root-evidence篡改拒绝、v14迁移、root恢复和edge驱逐后的
  父/叶mass同步撤销。
  本轮静态审查还修复F210 nullable刷新中一个未定义`instance`变量；新增
  proof-backed nullable shape与concrete epsilon instance同时存在的组合回归，避免
  两条独立通过的路径在合并时才抛出`NameError`。21项semantic聚焦测试、全量337项
  Python与4项subtest通过；
- 科研定位与边界：这是accepted concrete-tree的监督计数，不是对未选择ambiguous
  derivation做inside-outside EM；当前family只条件化parser/LHS/state，也不能表达
  sibling relation、matching tag、跨字段相等或长程上下文。2026年的ExplainFuzz已
  展示概率电路相对普通PCFG捕获上下文依赖的潜力，因此下一阶段应先实现
  relation-aware slot compatibility，再研究conditional/hierarchical probability
  model和native incremental parser wall-time消融。参考：
  [Lari--Young inside-outside](https://www.cs.jhu.edu/~jason/600.665/lari-young.pdf)、
  [Feature Forest Models](https://aclanthology.org/anthology-files/pdf/J/J08/J08-1002.pdf)、
  [EvoGFuzz](https://arxiv.org/abs/2008.01150)、
  [ExplainFuzz 2026](https://arxiv.org/abs/2604.06559)。当前等级 I/T/E。

## 209. F214：Relation-Aware Synchronized Slots with Exploration Reserve（2026-07-28）

- 研究问题：F211能原子替换多个nonterminal，却把同shape的child yield做笛卡尔组合。
  对重复字段、长度头和成对标识符，这会把预算大量花在明显违背accepted-parent
  关系的组合上；反过来，若把少量观测学到的关系当成硬grammar constraint，又会永久
  排除能带来新coverage的反例。F214因此把slot relation定义为可撤销的调度证据，
  只重排和限额，不参与语法许可、soundness pruning或最终接受判断；
- 观测与关系族：系统只使用F211已经逐byte重建成功的alternative-0 parent。
  同一parent production shape至少有两个独立accepted concrete instance，且当前全部
  观测都满足时，才为slot pair保留一个最强关系。固定强度顺序是byte equality、
  canonical decimal integer equality、左整数等于右byte length、右整数等于左byte
  length、byte-length equality。十进制只接受1--20个ASCII digit并限制到uint64；
  因此前导零相等可区别于byte equality，域外数字不会静默截断；
- 可撤销证据：relation ID规范绑定parser、parent shape、ordered slot pair和kind，
  保存support、零violation以及最多64个parent-instance witness。所有relation和
  `parent_shape -> relation`索引在每次accepted graph变化、驱逐或重启时从已复检
  instance重建；一条新accepted反例会使旧relation降级到下一个仍被全部观测支持的
  kind，或完全撤销。状态文件中的relation/support/transaction仅用于审计，不能作为
  恢复权限；
- Relation-aware组合：每个parent仍只改变2--4个slot，每slot最多两个canonical
  accepted alternative；最多枚举256个candidate。排序首先最大化满足关系的比例和
  数量，再考虑较少变化槽、result bytes与instance identity。最多输出四个transaction：
  前三个取高分组合；若存在未满足至少一条关系的剩余组合，显式保留一个最低分
  exploration reserve。这样关系能集中预算，却不能把反例空间剪空；
- Proof-carrying transaction：transaction升级为
  `symcc-parser-sync-transaction-v2`，canonical identity额外绑定每条relation ID及
  satisfied bit，并记录`relation_score/matches/total/exploration`。候选执行仍要求
  parent shape、source parent bytes和完整slot assignment匹配，随后经过target replay、
  独立parser acceptance与AFL novelty；relation本身不能绕过任何验证门；
- 精确edge provenance：审查发现全局内容寻址node可能汇集不同forest的edge。
  Semantic schema v16因此让每个packed ECT instance保存其本地production alternative的
  ordered `child_edge_ids`。加载时要求该列表与RHS形成slot `0..arity-1`完整双射，
  edge的parent/alternative/child symbol/state全部匹配；缺边、乱序、跨parent引用均
  fail closed。Edge驱逐会先撤销显式引用它的instance，即使全局图中仍存在同
  parent-child的parallel edge。V15迁移可从唯一的旧alternative edge set保守推导，
  推导不完整或歧义则丢弃instance；
- 有界性与遥测：relation总量限制4096，transaction总量限制4096，单parent组合预算
  256。Grammar snapshot增加relation数、累计support、kind数、relation-conforming和
  exploration transaction数；保存文件输出relation与parent索引以便科研审计，但
  restore只采用重算结果；
- 自动化证据：真实parser subprocess依次接受`A|A|tail`、`BB|BB|z`和
  `CCC|CCC|qq`，学习support=3的slot0/1 byte equality。源parent精确保留三个
  ordered child edge；四个transaction中三个保持相等，一个exploration reserve故意
  不相等。测试覆盖v16重启、伪造relation kind/score不被信任、v15无
  `child_edge_ids`迁移、edge binding乱序/缺失拒绝，以及`D|EEEE|www`反例到达后
  relation撤销。22项semantic聚焦测试、全量338项Python与4项subtest、原生build、
  2项String lit及Z3/cvc5各6/6通过；
- 科研定位与边界：这是零反例的有限关系假设器，不是attribute grammar、SMT-backed
  semantic grammar或概率电路。当前不表达checksum、tag/value枚举映射、算术仿射关系、
  多slot联合relation和长程祖先依赖；support也尚未做time decay或Bayesian置信校准。
  下一阶段应先把同一可信边界推广到conditional/hierarchical model，并以
  relation precision、valid-candidate/CPU、coverage/CPU和exploration-reserve
  contribution做消融，再决定是否引入ExplainFuzz式probabilistic circuit。参考：
  [Lase/OOPSLA 2026](https://zbchen.github.io/files/oopsla2026.pdf)、
  [Cottontail/S&P 2026](https://haoxintu.github.io/files/sp2026-cottontail.pdf)、
  [ExplainFuzz 2026](https://arxiv.org/abs/2604.06559)。当前等级 I/T/E。

## 210. F215：Hierarchical Context-Conditioned PCFG and Context-State Forest Mass（2026-07-28）

- 研究问题：F213的`(parser,LHS,state)` family posterior无法区分同一nonterminal在
  不同parent production下的选择偏好；F214能识别少量pairwise byte/length relation，
  但不能给production choice分配条件概率。直接为每个上下文训练独立分布会在online
  小样本下过拟合，完整ExplainFuzz概率电路又需要给定CFG、离线训练、可分解结构和
  单独的条件查询接口。F215先落地一个可解释、bounded、可迁移的层次化中间层；
- 条件上下文：每个accepted concrete-tree node产生
  `symcc-parser-production-context-v1`，规范绑定parser、child family、
  parent production shape和RHS slot；root使用空parent与slot -1。上下文不依赖
  forest-local alternative ordinal，因此同一parent shape跨样本可聚合，而不同parent
  grammar structure不会混淆。最多保留8192个有观测context；
- 原子可信观测：F213计数路径已重构为root-0/alternative-0 BFS。每个node必须存在
  primary concrete instance，其local `child_edge_ids`必须与完整slot sequence一致；
  selected tree中出现重复primary parent、缺instance、缺slot或edge mismatch时，整个
  fragment不更新任何全局或条件count，只留下空SHA-256去重记录。全局与context
  selections由同一次闭合遍历原子产生，避免“半棵accepted tree”污染posterior；
- Hierarchical posterior：令F213全局posterior为`P0(r|family)`。Root context继续直接
  使用`P0`；非root context使用强度`beta=2.0`的经验Bayes收缩：
  `P(r|c,D)=(n(c,r)+beta*P0(r))/(N(c)+beta)`。无context观测时严格退化到`P0`；
  少样本偏好不会立刻变成0/1，known-but-unseen shape始终保留非零质量。该概率只用于
  调度，既不改变CFG membership，也不批准mutation；
- Context-state inside/outside：共享packed node在不同parent下可能需要不同posterior，
  因此F215不能复用单一`inside(node)`。系统建立最多32768个
  `(packed node, production context)`状态，逆拓扑计算
  `inside(v,c)=sum_r P(r|c)*product_i inside(child_i,(r,i))`，再从每个显式root的
  root-context正拓扑传播outside。每条
  `symcc-parser-context-alternative-v1`绑定context state与F213 canonical
  hyperedge，记录posterior/inside/outside；所有表在重启和graph驱逐后重算；
- 调度集成：F206/F213仍保持九维Pareto。Production likelihood与
  information/reachability目标同时查看无条件和context-state artifact，取可验证图中
  最强的条件信号；这对不同parent下的稀有/常见shape更敏感，但不增加hard-pruning
  维度。无条件PCFG、relation-disabled transaction和普通grammar proposal仍是回退；
- 状态与篡改边界：semantic state升级v17并兼容v1--v16。持久化context identity、
  正整数count和每fragment selections，不持久化派生mass。加载时重算family/context
  identity，检查parent shape/slot确实能产生child family，并要求context observation
  digest属于同一已接受PCFG observation集合。Count与fragment selections通过原子
  固定点对账：任一坏selection使整个fragment退出，受影响count再级联撤销，首次加载
  即达到稳定闭包；保存后再次加载不继续变化。V16迁移没有context evidence时使用
  `P0`，不从全局graph伪造历史条件count；
- 遥测：snapshot增加context数、context observation fragment数、selected production
  总数、context inside/outside state数、context alternative数和训练证据上的mean
  NLL bits。后者是在线诊断量，不是held-out perplexity或研究结论；
- 自动化证据：真实parser subprocess让两个record parent shapes共享
  `value -> x|y` family。八个accepted fragments使全局x/y为4:4，同时parent A为3:1、
  parent B为1:3；`beta=2`精确得到0.5全局与2/3、1/3条件posterior，contextual
  hyperedge同步采用该概率。测试覆盖duplicate幂等、v17恢复、伪造count的原子固定点
  拒绝及二次重启稳定、伪造context slot/ID拒绝，以及v16无条件0.5 fallback。
  23项semantic与5项packed/parser交叉测试、全量339项Python与4项subtest、
  原生build、2项String lit及Z3/cvc5各6/6通过；
- 科研定位与边界：F215是一级parent-conditioned hierarchical PCFG，不是ExplainFuzz
  的sum/product probabilistic circuit，也不是Goblin式带任意semantic constraint的
  input-generation language。它不建模grandparent/long-range dependency、多个slot
  联合条件、checksum、symbol table/type environment或用户查询条件；当前NLL使用训练
  证据，尚未做prequential/held-out校准。下一阶段应实现time-ordered prequential
  evaluation和bounded multi-context feature circuit，保留`P0`/pairwise relation/
  hierarchical PCFG三组消融，再决定是否接入完整概率电路。参考：
  [ExplainFuzz 2026](https://arxiv.org/abs/2604.06559)、
  [Lase/OOPSLA 2026](https://zbchen.github.io/files/oopsla2026.pdf)、
  [Goblin/ICSE 2026](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/228/Parse-this-Summoning-Context-Sensitive-Inputs-with-Goblin)。
  当前等级 I/T/E。

## 211. F216：Prequential Context Calibration and Drift-Safe Global Fallback（2026-07-28）

- 研究问题：F215的`pcfg_context_mean_nll_bits`用最终count评价产生这些count的训练
  样本，必然存在look-ahead optimism；它能诊断拟合，却不能证明parent-conditioned
  posterior比F213全局PCFG预测得更好。若无条件启用context posterior，偶然的3:1小
  样本或分布漂移会长期误导packed-forest调度。F216把每次accepted selection改成
  test-then-train的prequential event，并以相对全局基线的历史proper log loss门控；
- Update-before receipt：在同一fragment的任何全局/context count增加之前，每个
  selected node生成`symcc-parser-pcfg-prequential-v1`。Canonical receipt绑定fragment、
  node、context/family、parent shape/slot、selected shape，以及预测所需的充分统计：
  global selected/total before、当时known-shape数、context selected/total before。
  Probability不持久化，而由这些整数和F213 `alpha=0.5`、F215 `beta=2`重算；同一
  fragment内的多个selection采用batch-before-update语义；
- 有界证据：最多保存32768个receipt，达到上限后饱和停止，不驱逐旧event或把同一
  fragment重复计入。Context/tree count仍按原8192 fragment预算继续学习，因此校准
  窗口饱和只冻结gate evidence，不阻断grammar learning；
- Prequential聚合：按receipt ID确定性累加每个context的global/context NLL bits、
  observation数、context probability高于global的事件数和
  `gain=global NLL-context NLL`。固定顺序消除重启时浮点加法次序差异。Root
  context的两个预测相同，不会凭空产生gain；
- 校准门：至少两个event且累计gain为正时，
  `w=min(1,n/8)*min(1,max(0,gain)/2)`；context-state forest实际使用
  `P_eff=P0+w*(P_context-P0)`，artifact同时保留raw posterior与weight。
  因此少量正证据只渐进偏离全局，零证据、负gain或后续drift自动令`w=0`并恢复
  `P0`。这只改变九维Pareto的概率/可达性输入，不改变CFG、relation或验证门；
- 状态与失败闭合：semantic state升级v18并兼容v1--v17。加载receipt时要求canonical
  ID、唯一`(fragment,node,context,shape)`event、有效context selection、匹配的
  family/parent/slot、有限整数范围，以及所有before-count严格早于最终已复检count。
  无效receipt被单独丢弃，PCFG/context counts不受影响；derived calibration统计和
  context-state mass全量重算。V17拥有context counts但没有update-before evidence，
  故raw posterior仍可审计，实际调度严格回退全局；
- 遥测：snapshot增加receipt数/容量/饱和、prequential observation数、global与
  context累计NLL、context gain、逐事件wins、非零校准context数和mean context
  weight。这些量可直接支持F213/F215/F216等CPU消融，且不混同训练内NLL；
- 自动化证据：F215八样本真实parser fixture产生40个node-selection receipts；
  receipt/count在v18重启后逐项相同，确定性校准统计完全相同。单receipt字段篡改只
  撤销该校准证据而保留context counts；v17迁移保留2/3与1/3 raw posterior但所有
  effective value选择回到0.5全局。独立drift测试先用四个context优胜event得到非零
  weight和`P_eff>P0`，再加入两个global正确/context错误event，使累计gain为负、
  weight精确归零且`P_eff=P0`。24项semantic与5项packed/parser交叉测试、静态检查
  通过；全量340项Python与4项subtest、原生build、2项String lit及Z3/cvc5各6/6
  通过；
- 科研定位与边界：这是prequential model-selection gate，不是完整online Bayesian
  change-point detector。当前receipt预算饱和后不滚动，gain对所有历史等权，没有
  exponential forgetting、ADWIN/CUSUM、per-target calibration或置信区间；canonical
  digest能发现普通损坏，但不是对恶意重写状态的签名。状态最多影响调度，永远不能
  授权candidate。下一步应实现bounded recency epochs和time-decayed regret，并在
  held-out/confirmatory corpus报告NLL、calibration、fallback rate、
  valid-candidate/CPU和coverage-AUC。参考：
  [Dawid prequential analysis](https://doi.org/10.1111/j.2517-6161.1984.tb01270.x)、
  [ExplainFuzz 2026](https://arxiv.org/abs/2604.06559)。
  当前等级 I/T/E。

## 212. F217：Recency-Window Calibration, Stale Detection and Recovery（2026-07-28）

- 研究问题：F216使用全历史累计regret。一个早期长期有效的context即使最近失准，
  也可能需要很多反例才把累计gain拉回零；反之，早期不稳定但新阶段已可靠的context
  可能长期被旧loss压制。Receipt容量饱和或近期没有该context时，继续沿用旧weight也
  不符合out-of-distribution保守回退。F217增加显式时间证据与fixed recency gate；
- 有序receipt：新`symcc-parser-pcfg-prequential-v2`在F216充分统计之外绑定
  `observation_index`，等于该fragment进入accepted PCFG observation stream前的长度。
  同一fragment全部node receipts共享index，不同fragment不能共享index。Index只给
  calibration排序/窗口定位，不参与grammar count或candidate authorization；
- 最近窗口：全局以最新accepted fragment位置推进16-fragment窗口，而非只看最后一张
  receipt；因此tree evidence为空、context未出现或receipt预算已经饱和时，时间仍会
  前进。每个context分别累计窗口内global/context NLL、gain、wins与selection数。
  一旦存在v2顺序证据，weight完全使用recent statistics；少于两个recent selection、
  recent gain非正或窗口内零event均令weight为0。后者额外标记`stale=1`；
- 漂移恢复：旧的positive cumulative gain不会覆盖recent negative gain；同样，旧的
  cumulative loss也不会阻止足够的新positive events重新启用context。F216的凸组合
  公式不变，所以disable/recovery连续落在`P0..P_context`之间，raw posterior与count
  均不因gate切换而丢失；
- 混合迁移：semantic state升级v19并兼容v1--v18。V18 v1 receipt没有时间序号，继续
  采用F216累计模式；加载v18后产生的新v2 receipt可与旧v1共存，该context一旦出现
  v2 evidence就进入recent mode。V19加载检查index范围、每fragment单index和
  index-to-fragment单射，再用v1/v2各自schema重算receipt ID；删除或修改index不能把
  v2静默降级成v1；
- 遥测：snapshot增加固定窗口长度、recency-mode context数、stale context数、
  recent selection数、recent global/context NLL、recent gain和recent wins。
  累计F216指标继续保留，便于量化“长期模型质量”与“当前调度gate”的差异；
- 自动化证据：F215 integration把40个v2 receipts在v19重启后逐项恢复；把它们按v1
  schema重新签名并降级为v18后，全部receipt保留但recency mode为0。独立阶段测试先
  用index 0--7建立positive recent gain，再在24--25注入recent adverse evidence使
  weight归零；其他context推进到40--41后原context成为stale；42--49的新positive
  evidence又恢复非零weight和`P_eff>P0`。25项semantic与5项packed/parser交叉测试、
  静态检查通过；全量341项Python与4项subtest、原生build、2项String lit及
  Z3/cvc5各6/6通过；
- 科研定位与边界：这是确定性的fixed sliding-window policy，不是ADWIN、CUSUM或
  Bayesian online change-point detection。16是工程预算，不具有自适应最优性或
  false-positive/negative bound；selection密集的context在同一fragment可贡献多个
  event。固定窗口便于可复现消融，却可能在gradual drift和稀疏context间产生
  bias。下一步应以receipt log-loss difference stream实现bounded adaptive-window
  certificate，并对fixed-16、cumulative、adaptive三者报告detection delay、false
  reset、fallback rate和coverage/CPU。参考：
  [ADWIN/SDM 2007](https://doi.org/10.1137/1.9781611972771.42)、
  [The Window Dilemma 2026](https://arxiv.org/abs/2602.06456)。
  当前等级 I/T/E。

## 213. F218：Bounded Adaptive Log-Loss Window and Verifiable Cut Certificate（2026-07-28）

- 研究问题：F217固定16-fragment窗口能快速回退，却把稀疏context与密集context置于
  同一时间尺度，也无法说明为什么在某一点丢弃历史。F218把v2 receipt流解释为
  `d=log2(P_context/P_global)`的prequential相对收益序列，在有界内存内在线重放
  change detection，并为每次截断输出可独立复算的阈值证书；
- 独立样本单位：同一accepted fragment可能包含多个同context node，直接把每张
  receipt当独立样本会伪造样本量。实现先按`observation_index`聚合，并以该fragment
  内receipt的平均收益作为一个检测样本。检测值裁剪到`[-4,4]` bits，最多重放最近
  64个context-bearing fragments，左右段各至少8个fragment；
- 统计门：对当前窗口的全部合法切点同时比较左右裁剪均值。若本轮有`m`个切点，
  使用`delta'=0.05/m`和有界两样本Hoeffding阈值
  `epsilon=4*sqrt(2*ln(2/delta')*(1/n_left+1/n_right))`。只接受
  `abs(mean_left-mean_right)>epsilon`，选择正margin最大的切点并丢弃左段；每个新
  fragment到达后重复，所以早期已确认的漂移不会被后续相反阶段在最终窗口中抵消；
- 调度统计：切点检测用裁剪收益，实际gate则在最后保留后缀上分别计算每fragment
  平均的未裁剪global/context NLL，再令
  `adaptive_gain=adaptive_global_nll-adaptive_context_nll`。F216的样本/gain凸混合
  公式保持不变，但样本量现在是独立fragment数而不是node receipt数。F217最近16个
  fragment的统计继续作为可消融基线，并且近期完全无该context时仍强制`stale`
  回退全局posterior；
- Proof-carrying cut：`symcc-parser-pcfg-adaptive-cut-v1`绑定context、窗口起止、
  retained suffix首index、两侧fragment/receipt数、均值、difference、epsilon、
  margin、clip bound、delta/多重检验数及有序receipt-ID摘要。Certificate ID对全部
  字段canonical hash。`verify_pcfg_adaptive_certificate`从当前canonical receipts
  重新计算概率、聚合、阈值、摘要与ID，任何字段篡改均失败；
- 状态与可信边界：semantic state升级v20并兼容v1--v19。Receipt仍是唯一校准证据；
  adaptive statistics和certificate均为derived audit artifact。状态文件写出证书便于
  实验归档，但恢复时完全忽略持久化副本并从已复检receipt重算，证书永远不能授权
  grammar candidate。最大64-fragment窗口、最小8+8切分和最多32768 receipts共同
  给出确定的CPU/内存上界；
- 遥测：snapshot增加adaptive最大/最小窗口、clip/delta、context/certificate数、
  retained fragment/receipt数、截断fragment数、后缀global/context NLL、gain和
  wins；累计F216与fixed-16 F217指标同时保留，支持三策略等预算消融；
- 自动化证据：平稳16-fragment强positive流保持零切点和非零weight；随后16个强
  adverse fragment生成有效证书、截断旧段并把weight归零；再加入16个positive
  fragment生成第二张证书并恢复weight。测试要求两张证书均可复算、阈值小于观测
  difference，篡改epsilon立即失败，并检查adaptive遥测。26项semantic测试与静态
  检查通过；全量342项Python与4项subtest、原生build、2项String lit及Z3/cvc5
  各6/6通过；
- 科研定位与边界：这是ADWIN-inspired bounded detector，不是完整ADWIN。Hoeffding
  界控制单次扫描内所有候选切点，在线反复扫描尚未使用anytime-valid alpha spending，
  因而不能声称无限时间horizon的全局5% false-alarm保证；clipping也只让检测统计
  有界，不证明原始log-loss分布无偏。后续实验必须报告fixed-16/cumulative/adaptive
  的false reset、detection delay、retained-window长度、fallback率、
  valid-candidate/CPU和coverage-AUC，并把adaptive gate作为multi-context
  probabilistic circuit的校准前置条件。参考：
  [ADWIN/SDM 2007](https://doi.org/10.1137/1.9781611972771.42)、
  [The Window Dilemma 2026](https://arxiv.org/abs/2602.06456)、
  [Dawid prequential analysis](https://doi.org/10.1111/j.2517-6161.1984.tb01270.x)。
  当前等级 I/T/E。

## 214. F219：Verifier-Gated Grandparent Multi-Context Probabilistic Circuit（2026-07-28）

- 研究问题：F215只按immediate parent shape/slot条件化。若相同parent production
  出现在两种外层record/document结构中，一级context会把两种相反的value分布合并，
  即使accepted tree已经提供无歧义的grandparent path。F219增加一个深度2 expert，
  并把global、parent、grandparent组合为有界、可校准的sum/product circuit；
- Context identity：`symcc-parser-production-circuit-context-v1`绑定parser、child
  family、parent shape/slot与grandparent shape/slot。它不含绝对offset或concrete
  bytes；只有完整alternative-0 primary tree的本地edge/instance闭包才能产生
  observation。相同shared packed node经不同grandparent到达时使用不同context-state
  v2 identity，避免在DAG上错误合并；
- Hierarchical posterior：parent raw posterior仍按F215以`beta=2`收缩到global。
  Grandparent raw posterior再以同一`beta=2`收缩到parent raw：
  `P2=(n2+2*P1)/(N2+2)`。因此无二级证据时严格等于P1，所有shape上的P2仍归一化；
- 双基线prequential gate：每个二级selection在任何count更新前生成
  `symcc-parser-pcfg-circuit-prequential-v1`，绑定global、parent与circuit三层
  selected/total充分统计、known-shape数、完整context path和observation index。
  Circuit event的robust gain定义为
  `min(NLL_global-NLL_circuit,NLL_parent-NLL_circuit)`；只有同时胜过global和parent
  才能获得正weight，防止更细模型仅利用parent已经解释的收益；
- Adaptive calibration：同fragment receipt先聚合为一个检测样本，robust gain裁剪
  到`[-4,4]` bits，并复用F218的64-fragment、最小8+8 Hoeffding detector。切点生成
  独立`symcc-parser-pcfg-circuit-cut-v1`，验证函数从circuit receipts重算三种概率、
  robust gain、阈值、证据摘要和certificate ID。Fixed-16无近期evidence仍触发
  stale fallback；
- Sum/product推断：context-state现在携带parent与可选circuit context。每条packed
  alternative的mass仍为`posterior * product(child inside)`，同state的alternative
  继续求和。若二级expert通过校准，
  `P_eff2=(1-w2)*P_eff1+w2*P2`；否则精确退化为F218的parent effective posterior。
  这是归一化凸组合，不做逐shape独立截断。Artifact同时公开parent raw、circuit raw、
  两层weight与最终posterior；
- 状态与失败闭合：semantic state升级v21并兼容v1--v20。分别持久化circuit context、
  count、fragment witness与update-before receipt；加载时重算context ID、验证两级
  production/RHS路径、对count和fragment selection做固定点对账，并要求receipt匹配
  parent context、最终三层count及fragment-index bijection。Derived circuit mass、
  calibration table和certificate不作为恢复权威。V20没有二级证据，严格保留parent
  模型；
- 有界性：最多8192个二级context、32768张circuit receipt，并共享32768个packed
  context-state上限。Receipt预算饱和只关闭新增二级校准证据；grammar candidate仍需
  concrete target replay、独立parser acceptance与AFL novelty；
- 遥测：snapshot增加circuit context/fragment/selection/receipt数、训练诊断NLL、
  校准context/平均weight/stale数、adaptive certificate/retained/truncated数，以及
  adaptive global/parent/circuit NLL和robust gain；
- 自动化证据：32个真实parser accepted fragments让唯一parent value context保持
  `x:y=16:16`，两个grandparent path分别得到`14:2`和`2:14`，P2为`5/6`与`1/6`，
  且两边weight均非零、packed artifact相对0.5向相反方向移动。V21重启逐项恢复
  counts/receipts/calibration；伪增count被固定点撤销，单receipt篡改只撤销该校准
  event，V20迁移不伪造circuit。独立48-event测试验证positive/adverse/recovery、
  双基线负gain、两张cut certificate与epsilon篡改拒绝。28项semantic与静态检查
  通过；
- 科研定位与边界：这是可解释的深度2 hierarchical mixture circuit，不是完整
  ExplainFuzz。尚未表达ordered sibling joint factor、任意ancestor depth、symbol
  table、checksum、query predicate或自动结构学习；per-scan Hoeffding界仍不是
  anytime-valid。下一步应实现有界sibling autoregressive factor，并用held-out
  prequential NLL、circuit sparsity、valid-candidate/CPU与coverage-AUC消融
  global/parent/grandparent/sibling各层。参考：
  [ExplainFuzz 2026](https://arxiv.org/abs/2604.06559)、
  [Lari--Young Inside-Outside](https://www.cs.jhu.edu/~jason/600.665/lari-young.pdf)、
  [ADWIN/SDM 2007](https://doi.org/10.1137/1.9781611972771.42)。
  28项semantic与静态检查通过；全量344项Python与4项subtest、原生build、2项
  String lit及Z3/cvc5各6/6通过。当前等级 I/T/E。

## 215. F220：Bounded Ordered-Sibling Autoregressive PCFG Factor（2026-07-28）

- 研究问题：F219的global、parent与grandparent expert都只描述一个node的祖先路径。
  对同一parent production内的有序字段，后一个child常由前一个child的产生式决定，
  例如tag选择不同value grammar，而parent/ancestor shape保持不变。把child inside
  独立相乘会把这种局部联合分布错误分解。F220增加一阶ordered-sibling factor，并
  保持无校准证据时严格退回F219；
- Context identity：`symcc-parser-production-sibling-context-v1`绑定parser、当前
  child family、parent production shape、当前slot和紧邻左兄弟的selected production
  shape。只从完整accepted alternative-0 primary tree提取；slot 0没有兄弟expert。
  Context不保存concrete bytes、offset或parser局部alternative ordinal，最多8192个
  sibling contexts；
- 层次posterior：兄弟raw posterior以`beta=2`收缩到当前可用的最细祖先raw先验。
  有grandparent path时
  `Ps=(ns+2*P2)/(Ns+2)`，否则使用
  `Ps=(ns+2*P1)/(Ns+2)`。最终调度分布为
  `P_eff_s=(1-ws)*P_eff_ancestor+ws*Ps`。由于两个分量分别归一化且`ws in [0,1]`，
  mixture在shape维保持归一化；无样本、负收益、stale或legacy状态令`ws=0`；
- 三基线prequential gate：每次selection在global、parent、circuit和sibling count
  全部更新前产生`symcc-parser-pcfg-sibling-prequential-v1`。Receipt绑定完整
  sibling/parent/optional-circuit path、四层selected/total统计、known-shape数与
  accepted-fragment index。Robust gain定义为
  `min(NLL_global-NLL_sibling, NLL_parent-NLL_sibling,
  NLL_circuit-NLL_sibling)`；没有circuit时第三个baseline精确等于parent。因此
  sibling expert只有在提供所有已有层之外的新信息时才获得正weight；
- 漂移与证书：同fragment的多个receipt先求平均，robust gain裁剪到`[-4,4]` bits，
  复用64-fragment、最小8+8的bounded Hoeffding detector。切点产生独立
  `symcc-parser-pcfg-sibling-cut-v1`，`verify_pcfg_sibling_certificate`从canonical
  receipts重算四种prequential概率、切点、阈值、receipt摘要和certificate ID。
  Persisted certificate仍仅是审计输出，不是恢复权威；
- 精确inside消息：packed context-state升级v3并携带可选sibling context。对一个有
  `k`个children的parent alternative，前向消息不是
  `product_i inside(child_i)`，而是按slot递推
  `F_{i+1}(s_i)=sum_{s_{i-1}} F_i(s_{i-1})
  sum_{a_i:shape(a_i)=s_i} inside(a_i|s_{i-1})`。
  Parent artifact mass为`posterior(parent)*sum_s F_k(s)`。每个factor step保存
  previous shape、selected shape、child context-state、child artifact与inside
  mass，形成可审计的规范化一阶链；
- 精确outside消息：对每个parent artifact从右向左计算suffix message。Child
  artifact的外部系数为
  `outside(parent artifact)*posterior(parent)*F_i(previous)*B_i(selected)`，
  明确排除该child自身inside mass。这样右侧后缀依赖不会被一个共享child-state标量
  错误折叠；artifact级outside是权威影响量，state outside只作为有界聚合遥测。
  计算复杂度对实际factor transitions线性，且共享32768个context-state硬上限；
- 状态与失败闭合：semantic state升级v22并兼容v1--v21。分别持久化sibling context、
  count、fragment witness和最多32768张update-before receipt。加载重算context ID，
  验证parent RHS当前slot及左slot production路径，对count/witness执行固定点对账，
  并要求receipt匹配parent、optional circuit、最终四层count和所有共享
  fragment-index双射。Derived weight、certificate、inside/outside与factor message
  全部从已复检证据重算。V21即使文件含未知sibling字段也不会猜测历史兄弟证据；
- 遥测：snapshot增加sibling context/fragment/selection/receipt数与容量、平均
  prequential sibling NLL、校准context/平均weight/stale数、adaptive certificate、
  retained/truncated fragments、四层adaptive NLL、robust gain，以及实际factor
  step/transition数；
- 自动化证据：32个真实parser accepted fragments保持value的global、parent和唯一
  grandparent circuit均为`x:y=16:16`；左tag node的两个内部production shape分别让
  sibling context得到`14:2`与`2:14`，raw posterior为`5/6`与`1/6`且两边weight均
  非零。测试逐步重算两slot前向质量，检查artifact posterior归一化与outside边界。
  V22重启逐项恢复counts/receipts/calibration；伪增count由固定点撤销，单receipt
  篡改只撤销该event，V21严格忽略sibling层。独立48-event测试验证相对三个baseline
  的positive/adverse/recovery、两张cut certificate和epsilon篡改拒绝；
- 验证结果：30项semantic测试通过；全量346项Python与4项subtest通过；
  `cmake --build build -j2`通过；2项String lit通过；真实Z3与cvc5 1.1.2均为6/6
  SAT且concrete-verified；Ruff、`py_compile`与差异空白检查通过；
- 科研定位与边界：这是有界一阶、左到右的autoregressive grammar factor，不是完整
  ExplainFuzz结构学习，也不表达非相邻/双向兄弟、任意ancestor depth、symbol table、
  checksum、semantic predicate或跨parent long-range dependency。Outside消息对当前
  有界packed forest精确，但容量饱和仍可能关闭更细context；Hoeffding界仍仅覆盖
  单次扫描。下一步应做global/parent/grandparent/sibling等预算消融，报告held-out
  prequential NLL、factor sparsity、detection delay、valid-candidate/CPU与
  coverage-AUC，再决定是否引入learned factor graph或symbol-table/checksum expert。
  参考：[ExplainFuzz 2026](https://arxiv.org/abs/2604.06559)、
  [Lari--Young Inside-Outside](https://www.cs.jhu.edu/~jason/600.665/lari-young.pdf)、
  [Dawid prequential analysis](https://doi.org/10.1111/j.2517-6161.1984.tb01270.x)、
  [ADWIN/SDM 2007](https://doi.org/10.1137/1.9781611972771.42)。
  当前等级 I/T/E。

## 216. F221：Portable SMT-LIB Signed-Integer Schedule Evidence（2026-07-28）

- 触发条件：安装cvc5 1.1.2后，F58 research-evidence的SC/TSO/RA公式在Z3中通过，
  但cvc5解析阶段拒绝reads-from初始写sentinel `-1`。SMT-LIB 2负整数不是原子
  numeral，规范形式是`(- 1)`；原实现依赖了Z3的宽松词法扩展，导致“后端可执行”
  被计入capacity后又以parse error使整个evidence manifest失效；
- 实现：`schedule_exploration.py`新增唯一整数常量序列化函数。SC/TSO read-from
  domain/implication、RA read-from/coherence、RMW初始源、seq_cst初始源和
  Query-IR byte-value bridge全部通过该函数输出负整数；非负event index保持原形式。
  `research_evidence.py`的store-buffering附加outcome也改用规范`(- 1)`，模型读取仍
  解析为Python整数`-1`，不改变RF语义或artifact schema；
- 失败闭合：这不是把cvc5 error从available backend集合中隐藏。Validator仍要求每个
  已安装backend返回预期SAT/UNSAT；修复目标是让同一QF_LIA obligation在不同solver
  family中可解析并保持oracle一致。若以后出现真正的语义分歧，evidence仍失败；
- 自动化证据：SC/TSO/RA base SMT逐项断言包含`(- 1)`且不含裸` -1`。真实cvc5对
  store-buffering SC返回UNSAT，对TSO/RA返回SAT；research-evidence的三项backend
  case同时报告Z3与cvc5两个family且expected oracle通过。Schedule与evidence定向
  63项通过；全量346项Python与4项subtest通过；
- 科研边界：该修复只建立当前schedule QF_LIA整数词法的跨solver可移植性，不代表
  完整SMT-LIB conformance，也不证明Z3/cvc5在更大公式上的独立实现一致性。后续若
  扩展Real、FP、quantifier或solver-specific option，应为各sort建立统一printer并
  将parse/status/stderr纳入版本化evidence。当前等级 I/T/E。

## 217. F222：Fail-Closed MPI Grammar-Hole QueryStore Wiring（2026-07-28）

- 缺陷：MPI master构造`SemanticProposalGenerator`时引用不存在的
  `query_store_dir`和`async_query_enabled`。只要启用semantic proposals就可能在
  初始化阶段触发`NameError`；该路径之前没有被全目录Ruff门禁捕获；
- 修复：master在异步服务分支前统一解析`SYMCC_QUERY_STORE`及默认路径。Grammar-hole
  generator只在`query_service_process`已成功启动且
  `SYMCC_QUERY_GRAMMAR_HOLES=1`时接收该store；workers为0、命令启动失败或功能关闭时
  显式传入`None`。这把“配置了路径”与“存在可消费该路径的query service”分开，
  避免半初始化store被误认为可用；
- 失败闭合：query service异常仍走现有stderr告警并降级，普通semantic grammar、
  parser replay、target validation与AFL novelty不受影响。该修复不让grammar-hole
  candidate绕过Query-IR evaluator，也不改变QueryStore schema；
- 静态加固：同轮删除确认无引用的局部变量/import，并把测试lambda改为具名函数，
  使`ruff check util test benchmark`首次在当前完整工作树上通过，而不是只对本轮
  文件做局部检查。所有受影响模块完成`py_compile`；
- 自动化证据：query/schedule/solution-generator/structural/path-cover/SMT-policy
  等受影响定向套件127项通过；全量346项Python与4项subtest通过。当前等级 I/T/E。

## 218. F223：Context-Wise Anytime-Valid PCFG Drift Certificates（2026-07-28）

- 研究问题：F218--F220的bounded Hoeffding cut只在一次扫描的候选切点之间分配
  `delta/m`。同一context在执行期间会被反复扫描，因此该界不能推出可选停止下的
  无限时域误报概率。简单再按扫描轮次做Bonferroni会使后期阈值迅速超过`[-4,4]`
  bits可观测差值，实际失去检测能力。F223将响应性调度cut和科研证书分成两层：
  原cut继续决定retained suffix；新证书只声明其严格成立的anytime审计保证；
- 构造：每个fragment级clipped gain经`x=(gain+4)/8`归一化到`[0,1]`。在context
  内第`j`个fragment启动一个forward CS，分配
  `alpha_j=delta*6/(pi^2*j^2)`；该CS第`k`次查看再分配
  `alpha_jk=alpha_j*6/(pi^2*k^2)`，用双侧Hoeffding半径
  `sqrt(log(2/alpha_jk)/(2k))`，并对所有历史查看区间取running intersection。
  当前端点若两个启动位置至少相隔8个fragment、两者各有至少8个样本且running
  intervals不相交，则产生证书。实现只保留最近64个launch，删除检验机会不会增加
  第一类错误；
- 保证与假设：在单个context内，若归一化gain有界且满足常条件均值
  `E[x_t|F_(t-1)]=mu`，Hoeffding--Azuma和两次union bound给出
  `sum_j sum_k alpha_jk <= delta=0.05`，因此任意扫描次数和data-dependent stopping
  下`P(false certified cut ever)<=0.05`。该声明不是跨所有context的全局FWER，
  也不覆盖条件均值本身随scheduler/admission策略变化的流。证书显式保存
  `guarantee`与`null_assumption`，避免把统计诊断误写成无条件正确性证明；
- 三层接入：parent、grandparent circuit和ordered sibling robust-gain流分别使用
  `symcc-parser-pcfg-{anytime,circuit-anytime,sibling-anytime}-cut-v1`。Sibling仍
  取相对global/parent/circuit三个baseline的最小收益，circuit仍取两个baseline的
  最小收益；因此anytime层没有放宽F219/F220的比较对象；
- Proof-carrying artifact：证书绑定context、窗口、全局launch ordinal、cut/detection
  index、双层预算、clipping区间、两条CS的样本均值/running interval/最终半径/
  tightening age、正separation gap和ordered receipt digest，再对canonical core
  哈希。三个公开verifier都从update-before receipts重算概率、robust gain、预算、
  区间和ID，字段篡改失败；
- 状态与迁移：semantic schema升级v23并接受v1--v22。Anytime表和证书均为derived
  audit state，持久化副本从不作为恢复权威；v22文件缺少新字段时，只能由通过既有
  count/witness/receipt固定点验证的证据重建。它不授权candidate，也不改变v20
  adaptive suffix、fixed-16 stale guard或PCFG mixture weight；
- 遥测：snapshot公开构造/保证名称、spending scale，以及parent/circuit/sibling的
  certified context和certificate数；per-context calibration保留cut index与
  normalized separation gap。Schema v23序列化三类derived certificate供实验归档；
- 自动化证据：三条64-fragment强漂移流均产生正gap证书并通过各自canonical verifier；
  证书预算、gap或区间篡改均被拒绝，双重逆平方预算的有限前缀严格小于1；v22迁移
  在删除所有新字段后仍恢复validated sibling receipts/counts。稳定和原有
  positive/adverse/recovery行为继续由同一30项semantic suite覆盖。全量346项
  Python与4项subtest、Ruff、`py_compile`、原生增量build、2项String lit、
  Z3/cvc5各6/6 concrete-verified conformance及双family research-evidence均通过；
- 科研依据与边界：构造采用confidence sequence的可选停止语义，并借鉴
  repeated-forward-CS change detector的逐时启动方式；与e-detector文献一致地把
  ARL/PFA和检测延迟分开。逆平方PFA版本对很晚的change point会有对数级延迟惩罚，
  因而当前不让它替代响应性gate。参考：
  [Howard et al., Annals of Statistics 2021](https://arxiv.org/abs/1810.08240)、
  [Shekhar--Ramdas repeated-FCS 2023](https://arxiv.org/abs/2309.09111)、
  [Shin--Ramdas--Rinaldo e-detectors 2024](https://arxiv.org/abs/2203.03532)、
  [Waudby-Smith--Ramdas bounded-mean betting 2024](https://arxiv.org/abs/2010.09686)。
  当前等级 I/T/E。

## 219. F224：Bounded Second-Order Sibling-History PCFG Factor（2026-07-28）

- 研究问题：F220只以紧邻左兄弟为条件，无法区分“相同bridge shape、不同更早
  anchor shape”导致的后续production选择。ExplainFuzz指出普通PCFG不能表达
  ancestor、sibling与long-range依赖；直接引入无界概率电路会使在线context、
  packed-state和证据空间失控。F224选择可审计的激进有界近似：对RHS slot `i>=2`
  学习`(shape[i-2],shape[i-1]) -> shape[i]`，显式检验在控制immediate sibling后
  非相邻兄弟是否仍有预测增益；
- 层次模型：history context由`parser/child family/parent shape/current slot/
  older shape/left shape`规范化哈希。Raw posterior以F220 immediate-sibling raw
  posterior为`beta=2`先验：
  `Ph=(nh+2*Ps)/(Nh+2)`。最终分布
  `P_eff_h=(1-wh)*P_eff_s+wh*Ph`是两个归一化分布的凸组合；因此无证据、stale、
  adverse或legacy状态令`wh=0`时精确恢复F220，而不是以零频shape做硬剪枝；
- 五基线update-before证据：每次完整accepted primary tree在任何count更新前生成
  `symcc-parser-pcfg-history-prequential-v1`，绑定fragment/node、完整两兄弟context、
  parent/optional circuit/sibling ID、shape、accepted-fragment index，以及global、
  parent、circuit、sibling、history五层selected/total充分统计。History robust gain
  是相对前四层NLL收益的最小值；只有history在同一证据上胜过所有已有expert时才能
  获得正weight；
- 漂移与无限时域审计：同fragment receipts先平均，gain裁剪到`[-4,4]` bits。
  响应性gate复用64-fragment、最小8+8的bounded Hoeffding detector并输出
  `symcc-parser-pcfg-history-cut-v1`；独立审计层复用F223双重逆平方alpha spending，
  输出`...history-anytime-cut-v1`。两个verifier均从canonical receipts重算五层
  概率、切点或running CS、预算、ordered evidence digest和certificate ID。F223的
  context-wise constant-conditional-mean/PFA边界原样适用，不扩张为跨context FWER；
- 精确order-2推断：context-state升级v4并可携带history context。Parent alternative
  的forward frontier由单shape变为最近最多两个shape的tuple；每个transition保存
  `previous_sibling_shape_ids`、selected shape、`next_sibling_shape_ids`、child
  state/artifact与inside mass。Inside对所有兼容tuple history derivation求和。
  Outside使用同一tuple state的forward prefix与backward suffix，将
  `outside(parent)*posterior(parent)*prefix*suffix`传给child artifact，明确排除
  child自身inside mass。因此结果对当前有界二阶链精确，而不是把history posterior
  近似乘进独立children；
- 有界资源与失败闭合：最多8192个history contexts、32768张history receipts，并与
  全部context states共享32768上限。Semantic schema升级v24且接受v1--v23。恢复时
  重算context ID，验证parent当前/前一/前二RHS spec与production shape，固定点对账
  count/witness，检查五层最终count、canonical receipt ID及global/parent/circuit/
  sibling/history fragment-index双射；derived weight、certificate与inside/outside
  全部重建。V23即使携带未知history字段也不会推断不存在的update-before证据；
- 确定性修复：在线dict最初按观测插入receipt，而序列化按receipt ID排序，导致重启
  后浮点NLL累加顺序变化。F224统一按receipt ID重放history events，使在线刷新与
  restore逐字段确定一致；这不是用近似浮点比较掩盖持久化差异；
- 遥测：snapshot增加history context/fragment/selection/receipt与容量、校准context、
  mean weight、stale、adaptive/anytime certificate及order-2 factor-state数；
  mapping持久化原始context/count/witness/receipt并导出derived certificate供实验审计；
- 自动化证据：真实parser fixture使用三child record和64个accepted fragments。
  Global、parent、grandparent均为`x:y=32:32`，每个immediate bridge context均为
  `16:16`；四个`(anchor,bridge)` history context分别为`14:2/2:14/2:14/14:2`，
  得到`5/6`或`1/6`且weight为正。测试重算tuple forward inside、posterior归一化与
  outside边界，逐项检查v24 restart、receipt/count篡改和v23 fail-closed fallback。
  独立64-event序列验证history相对四个baseline的responsive cut、anytime certificate
  与数值篡改拒绝。32项semantic、全量348项Python与4项subtest通过；Ruff、全目录
  `py_compile`、`git diff --check`、原生增量build与106项LLVM lit通过；真实Z3和
  cvc5 1.1.2 String/BV conformance均为6/6 SAT且concrete-verified。Research
  evidence v2的8项obligation通过，SC/TSO/RA在两个solver family上分别为
  UNSAT/SAT/SAT且无分歧；
- 科研定位与边界：这是ExplainFuzz动机下的bounded order-2 autoregressive factor，
  不是论文完整grammar-aware probabilistic circuit。它不表达任意距离、右向/双向、
  跨parent、symbol-table、checksum、semantic predicate或query-conditioned依赖；
  context cap可能使稀有组合回退。下一优先级是等CPU消融
  `global/parent/circuit/sibling/history`并报告held-out prequential NLL、fallback、
  states/transitions、parser CPU、valid-candidate/CPU和coverage-AUC；再依据残差研究
  bounded arbitrary-distance relation selector或native PC，而不是无证据扩大阶数。
  参考：[ExplainFuzz 2026](https://arxiv.org/abs/2604.06559)、
  [Lari--Young Inside-Outside](https://www.cs.jhu.edu/~jason/600.665/lari-young.pdf)、
  [Dawid prequential analysis](https://doi.org/10.1111/j.2517-6161.1984.tb01270.x)、
  [Howard et al. confidence sequences](https://arxiv.org/abs/1810.08240)。
  当前等级 I/T/E。

## 220. F225：Cross-Context Infinite-Horizon FWER Audit（2026-07-28）

- 研究问题：F223明确只控制单个context的首次certificate误报概率。系统同时学习
  parent、grandparent circuit、immediate sibling与history数千个context；若每个都
  以0.05解释为“系统显著”，至少一次误报概率可远高于0.05。F225把内部可选停止与
  外部online multiple testing组合，新增严格独立于调度gate的系统级审计层；
- 方法选择：Tian--Ramdas的online FWER工作说明alpha-spending在任意依赖p-value下
  控制更强的PFER并蕴含FWER；Online Fallback也允许任意依赖，但回收预算需要定义
  rejection序列和转移权重。ADDIS-Spending功效更高但依赖独立或局部依赖/保守null。
  Grammar contexts共享accepted fragments、生产式count和scheduler反馈，不满足这些
  额外条件，因此当前选择保守的不可回收alpha-spending，不以不可信独立性换功效；
- 三层预算：第`c`个首次注册context分配
  `delta_c=0.05*6/(pi^2*c^2)`；其第`j`个forward CS分配
  `delta_cj=delta_c*6/(pi^2*j^2)`；该CS第`k`次查看分配
  `delta_cjk=delta_cj*6/(pi^2*k^2)`。三次逆平方和各不超过1，所以所有context、
  launch和look的总预算不超过0.05。对每个真实null，若normalized gain在`[0,1]`
  且满足constant conditional mean，则union bound给出任意context间依赖、任意扫描
  次数和data-dependent stopping下的strong FWER上界；
- Append-only ledger：context注册顺序覆盖parent/circuit/sibling/history四类，
  同一accepted fragment内按固定kind和context ID排序；每项绑定kind、context ID、
  全局ordinal、first observation、global/context alpha、spending rule和
  `append-only-no-alpha-recycling` contract。最多32768项；context失活不释放ordinal，
  容量饱和后新context不再产生global certificate。Certificate绑定截至自身ordinal
  的allocation-ID prefix digest，所以未来append不会使旧artifact失效。为使外层
  alpha在被检验结果之前确定，注册context的首条observation只用于ledger，global
  CS从后续receipt开始；F223 local证书和运行gate仍可使用首条证据；
- 统一四模型重放：`_pcfg_global_anytime_units`分别从parent二层、circuit三层、
  sibling四层和history五层update-before receipts重算原有robust gain，再交给同一
  tuple-independent repeated-forward CS构造。全局证书schema为
  `symcc-parser-pcfg-global-anytime-cut-v1`，显式声明
  `cross-context-infinite-horizon-fwer`、outer allocation及
  `cross_context_dependence_assumption=none`。Verifier重算ledger prefix、context
  alpha、model-specific units、inner launch/look预算、running intervals与ID；
- 状态与失败闭合：semantic schema升级v25并接受v1--v24。V25加载要求allocation
  ordinal从1连续、ID/alpha/first-index规范、key唯一，并要求每个保留ordered receipt
  对应allocation且first index精确一致；任一缺失/重排/篡改令
  `pcfg_anytime_allocation_ledger_valid=0`并关闭全部global certificate，但不撤销
  grammar counts、不影响响应性cut或F223单context证书。V24及更早状态可从已经通过
  count/witness/index固定点的ordered receipts确定性建立初始ledger。Persisted global
  certificate仍是derived audit output；
- 自动化证据：parent、circuit、sibling、history四条64-fragment强漂移流分别以
  ordinal 1产生global certificate并通过统一verifier；prefix digest篡改拒绝。
  额外注册256个无证据context，验证ordinal严格为`1..257`且已分配alpha和小于0.05，
  无证据context不产生certificate。真实F224 parser状态逐项重启保持ledger；
  allocation ordinal篡改关闭global层，v24删除ledger字段后从validated history
  receipts恢复；32项semantic、全量348项Python与4项subtest、Ruff、全目录
  `py_compile`、`git diff --check`、原生build和106项LLVM lit通过；真实Z3与cvc5
  1.1.2 String/BV conformance均为6/6 SAT且concrete-verified，research evidence
  v2复检通过且SC/TSO/RA在两个solver family上无分歧；
- 保证边界：FWER family只包含获得ledger allocation且可能输出global certificate的
  contexts；容量之后不会检验新global hypothesis。证明假设不要求跨context独立，
  但要求各true-null context内部的bounded constant-conditional-mean条件，并要求
  append-only ledger完整。当前JSON文件不是外部透明日志；恶意重写整个ledger并重签
  所有派生artifact不在本地损坏模型内，科研归档应同时封存state/evidence digest。
  Global certificate只审计change claim，不授权grammar proposal、不调整PCFG weight；
- 后续实验：比较context-wise、global alpha-spending和不做统计证书三种报告层，
  记录certification delay、allocated/unused alpha、ledger saturation与false alarm。
  若真实context序列表现出可证明的局部依赖或conservative null，再研究
  ADDIS-Spending；在此之前不启用alpha recycling。参考：
  [Tian--Ramdas, Online control of the FWER](https://arxiv.org/abs/1910.04900)、
  [Howard et al., Confidence sequences](https://arxiv.org/abs/1810.08240)、
  [Shekhar--Ramdas, repeated FCS](https://arxiv.org/abs/2309.09111)。
  当前等级 I/T/E。

## 221. F226：Five-Level PCFG Confirmatory Ablation and Sealed Artifact（2026-07-28）

- 研究问题：F224/F225已经实现五层层次模型和全局统计审计，但此前没有一个能保证
  “只运行到指定层”的实验控制。仅把最终weight置零仍会支付高阶context、receipt、
  certificate和tuple-state成本，不能回答覆盖收益是否值得CPU/内存开销；只打印
  snapshot也无法把证据可靠绑定到随机区组、源代码和CPU预算。F226把机制消融和
  tamper-evident科研产物接入F67 sealed protocol；
- Cost-faithful阶数控制：`SYMCC_PCFG_CONTEXT_ORDER=0..4`或
  `global/parent/circuit/sibling/history`。`_observe_pcfg_fragment`只建立不高于所选
  层的context、update-before receipt和allocation；刷新只计算启用层的校准与证书。
  Level 0完全跳过contextual inside/outside，level 1只构造parent state，level 2加入
  circuit，level 3的factor frontier只记一个左shape，level 4才记两个shape并构造
  history。默认4保持F225行为；该运行配置写入mapping/snapshot但不改变v25 evidence
  schema。跨order恢复会清空全部PCFG evidence/ledger/derived mass并以
  `pcfg_state_order_mismatch_reset=1`记录，保留validated CFG/ECT，防止共享目录污染
  消融；同order resume与旧版本默认history迁移保持原语义；
- 确认性工件：MPI master在初始化、周期save和finally阶段原子写
  `symcc-pcfg-research-artifact-v1`。Canonical SHA-256覆盖semantic schema、所选
  层、protocol metadata和完整scalar grammar snapshot。新增snapshot字段包括五层
  自适应NLL/robust gain、每类anytime/global证书从cut到detection的平均fragment
  delay、已分配alpha，以及既有context/receipt/state/transition/capacity指标。
  Artifact不授权proposal，只是科研证据；
- Protocol闭环：`pcfg_context_ablation_configurations`从一条base command生成
  `pcfg-global`至`pcfg-history`五个同command/同核数/同grace配置，只改变阶数并强制
  工件。CLI spec可直接使用`pcfg_context_ablation`。Executor注入run directory、
  target、seed和CPU budget；成功却缺少、损坏或跨run/config/order的artifact会转为
  failed并保留stdout/stderr。Verifier同时检查artifact内部digest、schedule/protocol
  binding和完整artifact-tree digest；
- 统计接入：`analyze_ablation.py --pcfg-artifact PATH...`递归读取并验证工件，按
  protocol run ID把所有scalar `pcfg_*`指标连接到benchmark rows，因此同一paired
  framework可分析coverage AUC、prequential NLL、robust gain、certificate delay、
  alpha/state/receipt开销和失败。NLL/delay/memory/bytes/overhead自动按lower-is-better
  处理；确认性结论仍需至少20个paired repeats及Holm校正；
- 自动化证据：最小packed forest `root -> record -> (a,b,c)`在五档上验证非空状态
  精确为`{}`、parent、+circuit、+sibling、+history；context artifacts中的高阶ID和
  factor-state随档位严格出现。Artifact round-trip通过，指标篡改失败，非法order拒绝。
  Protocol测试验证100-cell五层确认性矩阵、CPU一致性、schedule/config/order绑定、
  digest篡改拒绝和missing-artifact失败保留；analysis测试验证artifact指标只按
  protocol run连接。定向semantic/protocol/analysis分别33/7/6项通过；全量351项
  Python与4项subtest、106项LLVM lit、原生build、Ruff、全目录`py_compile`和
  `git diff --check`通过。真实Z3与cvc5 1.1.2的String/BV/conversion conformance
  均为6/6 SAT且concrete-verified；research evidence v2重新生成并验签，8项
  obligation全部通过，SC/TSO/RA在两个solver family中一致为UNSAT/SAT/SAT；
- 科研边界：F226完成的是实验机制、证据schema和分析入口，不等于已经执行公开目标
  的20-repeat campaign，也不证明history优于低阶模型。各档必须使用隔离的新run
  directory；跨档复用同一semantic state会改变prequential问题。下一步应运行tuning
  后封存confirmatory manifest，同时报告coverage/CPU与结构指标；只有held-out residual
  在history档仍显著且可重复时，才进入任意距离/cross-parent factor研发。
  当前等级 I/T/E。

## 222. F227：Persistent Native Tree-sitter Incremental Parser（2026-07-28）

- 研究问题：F212已经定义content-addressed cache manifest和可复检reuse receipt，
  但测试parser只消费JSON协议，未调用真实增量解析API。每次proposal重新启动普通
  parser仍会丢失内存中的旧parse tree，因此不能声称native incremental parsing，
  也无法区分“span未变”与“解析器实际复用了该subtree”；
- 原生增量路径：新增`util/tree_sitter_incremental_parser.py`。`serve`进程加载官方
  `tree-sitter>=0.25`和目标language provider，在mode-0600 Unix socket后串行服务
  proposal master，并以input SHA-256保留最多256棵native `TSTree`。Manifest中的
  base digest和`offset/delete/insert_hex`必须能逐byte重建candidate；命中后复制
  base tree、以byte-accurate row/column调用`Tree.edit`，再执行
  `Parser.parse(candidate, old_tree)`。Base不存在、daemon重启或manifest不匹配时
  明确cold parse，不把协议命中伪装为性能复用；
- Node-ID proof：Tree-sitter官方API规定，基于old tree产生的新tree若复用旧node，
  两者node ID相同。服务在完整preorder v2 trace中保存base/candidate native ID映射，
  只为F212 manifest实际offered且在新tree中ID相同的节点输出
  `symcc-parser-incremental-reuse-v1`。现有`VerifiedProposalManager`不信任ID声明，
  继续独立检查manifest digest、base/candidate index唯一性、symbol、grammar/
  parse state、mapped span、epsilon和candidate yield SHA-256；receipt缺失是合法
  cold fallback，伪造或错配仍fail closed；
- 服务与资源边界：长度前缀RPC限制64 KiB，manifest/trace各限制1 MiB，input和node
  数有硬上限；trace使用临时文件、`fsync`和rename原子发布。Parser被单服务线程
  串行访问，避免共享state竞态；LRU-like确定性驱逐只删除内存TSTree，不触碰AFL
  corpus或manager的权威artifact。`ping`输出cold/incremental/reused/cache telemetry，
  `shutdown`清理socket；parser拒绝的error tree可输出审计trace但不会进入cache；
- 配置与依赖：`requirements.txt`加入`tree-sitter>=0.25,<0.27`和可执行conformance
  grammar `tree-sitter-json>=0.24,<0.26`。用户以
  `SYMCC_PROPOSAL_PARSER="python3 ... parse --input {input} --trace {trace} --cache {cache}"`
  接入现有MPI链，无需新增旁路acceptance gate。其他目标只需安装相应官方/兼容
  language provider；
- 自动化证据：纯Python fake native tree精确检查edit byte/point参数、左右node ID
  复用、坏edit cold fallback、error tree不缓存、node cap fail-closed、cache驱逐和
  RPC/manifest digest。真实Tree-sitter 0.25.2与tree-sitter-json 0.24.8进一步验证
  JSON局部数字替换保留多个native node ID；独立Unix daemon/client和完整
  `VerifiedProposalManager -> manifest -> native parse -> receipt -> manager
  revalidation`链路均命中。当前专项10项、受影响面60项、全量361项Python与4项
  subtest、107项LLVM lit、原生build、Ruff、全目录`py_compile`和
  `git diff --check`通过；Z3/cvc5各6/6 String/BV conformance通过，research
  evidence v2验签通过；
- 科研边界：Tree-sitter提供高性能concrete syntax tree和真实incremental reuse，
  但不导出全部歧义derivation的GLR/GLL SPPF。因此F227关闭“真实Tree-sitter增量API”
  差距，不把v2 concrete tree称为complete forest；v3/v4 packed alternatives、
  nullable proof和inside/outside仍依赖能导出SPPF的外部parser。下一步应把同一
  manifest/receipt接入至少一个native GLR/GLL complete-forest adapter，并在F226
  sealed protocol中报告parser wall time、native ID reuse、invalidated frontier、
  trace bytes、valid-candidate/CPU与coverage/CPU。参考：
  [Tree-sitter Advanced Parsing](https://tree-sitter.github.io/tree-sitter/using-parsers/3-advanced-parsing.html)、
  [py-tree-sitter Parser](https://tree-sitter.github.io/py-tree-sitter/classes/tree_sitter.Parser.html)、
  [py-tree-sitter Node identity](https://tree-sitter.github.io/py-tree-sitter/classes/tree_sitter.Node.html)。
  当前等级 I/T/E。

## 223. F228：Proof-Carrying Parser Telemetry and Sealed Cost Ablation（2026-07-28）

- 研究问题：F227证明native增量路径真实发生，但原proposal snapshot只按record最后
  状态统计cache hit/reused nodes，没有累计parser wall time、零复用receipt或proof
  类型。即使运行F226 sealed campaign，也无法回答节省的native parse time是否超过
  client process/RPC/trace开销，或cache offer有多少因daemon restart退化为cold；
- State schema 11：`VerifiedProposal`累计manager实测subprocess wall microseconds、
  服务报告的native parse microseconds、trace bytes、cache request/incremental
  offer、receipt、zero-reuse、Tree-sitter node-ID proof、reused/invalidated nodes。
  最新一次字段仍用于proposal诊断，累计字段用于科研聚合。V11读取v1--v10，旧state
  以最后观测作为保守初始total；恢复要求
  `proof <= receipt <= offer <= request <= validation`、zero-reuse不超过receipt、
  totals不小于latest且64-bit有界。所有字段只观测，不改变parser/target/AFL gate；
- Proof-carrying telemetry：F227 receipt新增
  `proof=tree-sitter-node-id-v1`。Manager只接受空proof或该规范值；若存在F227
  telemetry，必须满足schema、cold/incremental与receipt一致、reported node/reuse
  精确等于独立trace复检结果、cache entry有界。伪造incremental mode、node count、
  reuse count、proof或非有限/超时elapsed会使trace fail closed；
- Cost-faithful control：`SYMCC_PROPOSAL_PARSER_CACHE=0`使manager完全不创建manifest、
  不保留trace cache、不计request，但仍执行同一parser command和完整trace validation。
  `parser_incremental_ablation_configurations`从一条base生成`parser-cold`和
  `parser-incremental`，保持command、core和grace完全一致，只改变cache mode并要求
  artifact；
- Sealed artifact：MPI在初始化、周期save和finally原子写
  `symcc-parser-research-artifact-v1`。Canonical digest绑定proposal schema 11、
  cache mode、run/pair/config/seed/CPU metadata和全部scalar parser funnel/cost指标。
  Protocol verifier检查digest、counter lattice、schedule和configured mode；成功但
  artifact缺失/损坏/跨run会变failed，失败运行的原始artifact仍由artifact-tree
  digest保留。`analyze_ablation.py --parser-artifact`只连接验签后的
  `proposal_parser_*`指标；
- 指标解释：manager mean wall time是用户实际支付成本；reported parse time用于拆分
  native parser和RPC/process/JSON开销。Receipt rate、zero reuse、mean reused nodes、
  invalidated nodes和trace bytes用于解释机制；coverage AUC、valid-candidate/CPU和
  retained/CPU仍是最终效用，不能用高reuse rate替代；
- 自动化证据：cache-off配置仍接受完整trace且request/cache entry均为零；错误native
  telemetry被拒绝。真实Tree-sitter manager链检查wall/reported time、trace bytes、
  offer/receipt/proof并完成artifact round-trip/tamper。Protocol验证40-cell
  confirmatory cold/incremental矩阵、schedule/config/cache-mode binding与
  missing-artifact失败保留；analysis按protocol run ID连接parser标量。受影响面77项、
  全量365项Python与4项subtest、107项LLVM lit、原生build、Ruff、全目录
  `py_compile`和`git diff --check`通过；真实Z3/cvc5各6/6 String/BV conformance
  与research evidence v2验签通过；
- 科研边界：F228提供机制可辨识的实验面，不等于已经测得增量加速，也不证明JSON
  grammar可代表大型语言或binary formats。Tree-sitter daemon当前串行，适合作为
  proposal master的共享服务；跨node/跨主机部署需要独立排队与网络成本实验。
  Confirmatory campaign必须分别报告cold/incremental的parser输入大小、edit span、
  invalid/error比例和daemon restart。当前等级 I/T/E。

## 224. F229：Persistent Generalized Earley Complete-SPPF Adapter（2026-07-28）

- 研究问题：F209--F226已经能验证、学习和调度shared packed DAG，但真实parser路径
  只有F227 Tree-sitter selected CST，全部歧义来自手写fixture。把Tree-sitter内部
  GLR recovery路径或多个CST child误写成packed alternatives会制造不存在的grammar
  evidence。F229选择官方明确暴露SPPF的Lark Earley后端，为v3/v4提供第一个真实
  complete-forest producer；
- Parser构造：新增`util/lark_sppf_parser.py`与`lark>=1.3,<1.4`依赖。服务固定
  `parser=earley`、`ambiguity=forest`、`lexer=dynamic_complete`、
  `use_bytes=True`、`keep_all_tokens=True`、`ordered_sets=True`。前两项直接返回
  SPPF；dynamic-complete保留terminal内部备选；byte mode使Lark position与candidate
  byte offset一致，不以Unicode codepoint近似。Grammar限制1 MiB并绑定SHA-256和
  Lark版本；mode-0600 Unix daemon编译一次immutable grammar、串行处理请求；
- 无损有界编码：SPPF completed Symbol与LR(0) intermediate item各保留一个trace
  node，Token保留terminal node。每个PackedNode转为独立`@packed` wrapper，其state
  绑定origin、完整expansion、alias和SHA-256；父Symbol的每个alternative只指向对应
  wrapper，因此两个不同rule即使具有相同child indexes也不会被v3唯一性规则塌缩。
  Wrapper再指向原SPPF children，保留shared incoming edges与source span。拓扑排序
  确保所有边forward；Lark排序的首个Packed derivation作为alternative zero；
- Selected-tree不变量：完整SPPF是DAG，但manager要求root zero沿alternative zero
  形成一棵无多父树。编码器先检测循环，再仅对该primary projection中第二次出现的
  node复制其primary closure；其他alternative仍指向原node，所以歧义共享证据不被
  全树展开。复制仍受4096 node总上限，不能以隐藏指数展开换取acceptance；
- Nullable桥接：Lark SPPF合法保留`A -> B -> epsilon`零宽子图，而现有trace-v3
  明确要求epsilon node没有child。编码器从折叠前的全部零宽alternative导出不同
  `(symbol,state) -> RHS`依赖，把零宽节点规范化为叶并输出trace-v4。Manager随后
  独立重算nullable rule ID、依赖SCC和least fixed-point proof；超过32条rule或出现
  非nullable child时拒绝。这既满足现有schema，也没有把nullable链静默丢成一个
  无来源空节点；
- 完整性与资源边界：循环SPPF对应无限歧义，forward trace-v3/v4无法无损表达，故
  明确失败。节点4096、edge occurrence 16384、symbol alternatives 8、production
  children 64、nullable rules 32、input默认16 MiB、trace 1 MiB、RPC 64 KiB均是
  hard bound；超过任一上限不写部分trace。Syntax rejection写一个结构合法的negative
  trace并返回1；grammar/internal/bound/cycle失败返回2。`forest_telemetry.complete`
  仅在完整有界编码成功时为true；
- 主路径接入：CLI沿用`SYMCC_PROPOSAL_PARSER`的`{input}/{trace}`contract，不需要
  manager旁路。独立服务的`ping`暴露grammar digest、Lark版本、accepted/rejected/
  failure和Earley elapsed；`shutdown`清理socket。`{cache}`参数仅为命令兼容而被
  忽略，F229不声称incremental parse；
- 自动化证据：`test/test_lark_sppf_parser.py`用真实Lark验证`aaa`二叉歧义至少一个
  双alternative Symbol、非主路径shared incoming edge、manager完整v3复检；双
  epsilon文法验证v4 nullable certificate与manager least-fixed-point复检；原始
  `c3 a9`验证byte span为`[0,2)`；另覆盖syntax negative trace、非法grammar、
  无限歧义cycle、node cap、持久Unix RPC及完整
  `VerifiedProposalManager -> parser -> trace-v3 -> structural validation`链。
  当前8项专项、Ruff、`py_compile`和`git diff --check`通过；
- 科研定位与下一步：Lark官方文档明确Earley `ambiguity=forest`返回SPPF，
  Symbol的children是Packed derivations；因此F229是实际generalized Earley/SPPF
  integration，而不是显式树枚举。Lark是pure Python，且每个candidate重新parse；
  当前不能称native GLR/GLL或incremental SPPF。下一步先把complete/raw/encoded/
  clone/alternative/edge/nullable/time telemetry纳入sealed artifact并执行
  cross-parser acceptance/precision/cost calibration，再根据CPU结果决定接入原生
  或增量forest backend。参考：
  [Lark SPPF API](https://lark-parser.readthedocs.io/en/latest/forest.html)、
  [Lark parser algorithms](https://lark-parser.readthedocs.io/en/latest/parsers.html)。
  当前等级 I/T/E。

## 225. F230：Proof-Carrying Complete-Forest Telemetry and Calibration（2026-07-28）

- 研究问题：F229的结构trace由manager独立验证，但`forest_telemetry.complete`、
  raw node和parse time仍是provider自报字段。如果不核对这些字段，sealed artifact
  可以在结构有效时夸大complete rate、缩小forest cost或把Tree-sitter trace计为SPPF。
  F230把“语法授权”和“科研计量”继续分离：telemetry不授权candidate，但任何错误
  telemetry会使该trace不能进入研究状态；
- Proof contract：F229 trace新增`proof=lark-earley-complete-sppf-v1`、grammar
  SHA-256和Lark version。`VerifiedProposalManager._load_parser_trace`要求proof/
  provider identity规范，禁止与Tree-sitter incremental telemetry共存，并从规范化
  nodes重新计算encoded node、alternative和edge occurrence，从原v4 rules计算
  nullable count。`complete`必须等于parser accepted；raw、clone、encoded关系、
  elapsed、v3/v4和全部hard bound必须闭合。Syntax-negative trace保留proof但
  complete=false、raw=0；
- State v12：每个record累计forest trace、complete trace、proof、raw/encoded node、
  primary clone、packed alternative、edge、nullable rule和forest parse time。
  Snapshot提供全局和；research metrics另外给出complete rate、mean raw/encoded
  nodes、mean edges/alternatives/time。V12读取v1--v11；restore要求
  `complete <= proof <= trace <= validation`、encoded至少覆盖trace、raw至少覆盖
  complete、clone不超过encoded、nullable不超过`32*complete`、forest time不超过
  总reported parser time，所有累计量均限制signed 64-bit。同一record不得切换
  grammar digest；state顶层的sorted grammar identity集合必须覆盖每个forest record；
- Artifact闭环：`symcc-parser-research-artifact-v1`保持schema但绑定proposal state
  12及全部新scalar、规范化parser command SHA-256和实际grammar SHA-256集合。
  Manager与protocol verifier都复检forest counter/time lattice；artifact digest继续
  绑定run/pair/config/seed/CPU。MPI metadata新增
  `parser_forest_mode`；
- Cost-faithful calibration：新增
  `parser_forest_ablation_configurations`和CLI `parser_forest_ablation`。一条base
  展开`off/selected/complete`三档：off清空parser command，selected使用指定CST
  command，complete使用指定SPPF command。三档同command/cores/grace，统一
  `SYMCC_PROPOSAL_PARSER_CACHE=0`排除增量reuse，均要求parser artifact。Verifier
  要求metadata mode、parser command与configuration一致，complete实际grammar必须
  等于protocol预声明SHA-256，off/selected不得报告forest trace或grammar；
- 自动化证据：F229真实daemon/manager测试现检查state v12的trace/complete/proof/
  raw/encoded/alternative/edge/time，artifact round-trip和proof篡改，save/reload及
  state中`proof > trace`记录丢弃；独立trace edge篡改拒绝。Protocol测试验证
  20-repeat三档共60 cells、同CPU/cache-off、mode binding与错误mode拒绝，并通过
  CLI spec实际生成三档manifest。F228 Tree-sitter、proposal migration、analysis
  fixture同步升级state 12并保持通过。最终全量376项Python与4项subtest、108项
  LLVM lit、原生build、Ruff、全目录`py_compile`、`git diff --check`和权威文档
  相对链接检查通过；真实Z3与cvc5 1.1.2各6/6 String/BV case均SAT且
  concrete-verified，research evidence v2的8项obligation重新验签通过；
- 解释边界：off/selected/complete不是纯算法消融，因为Tree-sitter与Lark可能接受
  不同语言或选择不同concrete tree。实验必须报告acceptance confusion matrix、
  invalid ratio、trace规模、manager/parser CPU、valid-candidate/CPU和coverage AUC；
  不能把complete档单独更高的coverage直接归因于SPPF。F230也不实现incremental
  SPPF；它先提供决定该高成本研发是否值得的证据面。当前等级 I/T/E。

## 226. F231：Candidate-Paired Cross-Parser Calibration（2026-07-28）

- 研究问题：F230的`off/selected/complete`是独立fuzzing campaign。不同cell产生的
  candidate集合不同，所以三个aggregate acceptance rate不能恢复同一candidate上的
  `both/primary-only/secondary-only/neither`，文档要求的acceptance confusion
  matrix此前没有可执行证据。F231新增`util/cross_parser_oracle.py`，让complete-SPPF
  primary与selected-CST secondary并发解析同一不可变candidate；
- 无shell双执行：两个child command由`shlex.split`变成有界argv，必须把`{input}`和
  `{trace}`作为完整argument，禁止`{cache}`与相同命令对。两个subprocess用独立临时
  trace并发运行，stdin/stdout/stderr隔离，单个timeout限制为60秒；只有return code
  0/1分别表示accept/reject，timeout、exec失败、rc 2或无trace全部fail closed。
  Wrapper不把secondary变成授权oracle，最终退出码始终跟随primary；
- Proof envelope：primary保持原v1--v4顶层结构，secondary完整JSON嵌入
  `cross_parser_trace`。`symcc-cross-parser-telemetry-v1`绑定proof
  `paired-independent-trace-v1`、candidate SHA-256、两个规范化argv SHA-256、
  两个canonical trace SHA-256、parser name、return code、accept bit和各自elapsed。
  Child trace禁止递归携带cross receipt，合并后仍受1 MiB manager trace上限；
- 独立复检：Proposal manager不相信wrapper的secondary accept bit。它把嵌入trace
  canonical编码到mode-0600临时文件，并以`allow_cross=false`递归调用原有v1--v4
  validator；因此span/parent/packed DAG/nullable fixed point、Tree-sitter telemetry
  和Lark complete proof全部沿用同一验证面。随后复算两个trace digest、candidate
  digest、parser identity、rc/accept等价和elapsed bounds。任一字段篡改使整个
  primary trace无效；
- State v13与artifact：每个record累计paired trace、agreement、both-accept、
  primary-only、secondary-only、both-reject和两侧时间，并锁定一个有序command pair。
  Restore要求四格之和等于trace、agreement等于对角线、trace不超过parser validation、
  command pair完整且存在于顶层集合，所有时间在signed 64-bit内。Research artifact
  密封sorted pair集合和新scalar/rate；manager与protocol verifier重复检查恒等式；
- Sealed protocol：新增`parser_cross_calibration_configurations`与CLI
  `parser_cross_calibration`，从一条base生成同CPU的`selected/complete/paired`
  三档，统一关闭parser cache并要求artifact。Paired档仍标记forest complete，绑定
  wrapper command digest、预声明grammar digest及两个child digest，并要求
  `forest_traces == cross_traces`。独立档`SYMCC_PARSER_CROSS_MODE=off`不得报告pair；
- 自动化证据：真实Lark Unix daemon作为complete primary、独立v2 fixture作为selected
  secondary，分别覆盖both-accept与secondary-only；manager仍拒绝primary-negative
  candidate但保留校准象限。另覆盖secondary node篡改、相同child command拒绝、
  state save/reload、artifact round-trip、四格总和篡改、60-cell确认性矩阵与CLI
  expansion。新增3项parser测试和2项protocol测试后全量381项Python通过；109项
  LLVM lit、原生build、Ruff、全目录`py_compile`、`git diff --check`与9份权威文档
  相对链接检查通过。Z3和cvc5 1.1.2各6/6 String/BV case均SAT且
  concrete-verified，既有research evidence v2验签通过；
- 调研结论与边界：GNU Bison GLR官方实现会分裂/合并stack并延迟semantic action，
  但没有通用SPPF导出；Tree-sitter公开API提供incremental selected tree而非完整
  ambiguity forest；Iguana提供GLL+binarized SPPF，但当前项目环境无JVM，且未找到
  incremental forest update的官方承诺。因此F231不虚构“native incremental SPPF”
  已完成。它提供决定下一后端投资的同候选precision/cost证据；公开20-repeat campaign
  仍未执行。参考：
  [GNU Bison GLR](https://www.gnu.org/software/bison/manual/html_node/GLR-Parsers.html)、
  [Tree-sitter grammar conflicts](https://tree-sitter.github.io/tree-sitter/creating-parsers/2-the-grammar-dsl.html)、
  [Iguana GLL/SPPF](https://iguana-parser.github.io/getting_started.html)。
  当前等级 I/T/E。

## 227. F232：Parser-Neutral Structural Correspondence Certificate（2026-07-28）

- 研究问题：F231回答两个parser是否接受同一candidate，但`both-accept`仍可能掩盖
  完全不同的token boundary和constituent segmentation。直接比较symbol name也不
  科学：Lark grammar的Symbol/LR(0)/Packed label与Tree-sitter grammar node type
  不在同一ontology。F232先实现不依赖symbol命名的byte-yield结构校准；
- Selected projection：manager新增确定性selected-node提取。Trace v3/v4直接使用
  已验证的`primary_nodes`，即root alternative-zero闭包；v1/v2从所有root沿每个node
  的alternative zero遍历，没有显式alternative时沿parent tree全部direct child。
  因为计算发生在双trace独立验证之后，坏parent、坏alternative、循环或越界无法进入
  correspondence计数；
- 两层span证据：对primary分别收集alternative-zero与complete encoded forest中所有
  非epsilon `(start,end)`，对secondary收集selected-tree span。相同yield的Symbol、
  LR item和Packed wrapper只计一次，避免parser内部representation multiplicity放大
  precision；若存在非root span则去除`[0,input_size)`这个平凡共同root。另收集所有
  `0 < boundary < input_size`的内部start/end集合；
- State v14：仅`both-accept`生成structural pair，累计primary-selected、
  primary-forest、secondary、selected-shared、forest-shared、两种union，以及四类
  boundary计数。Restore与artifact verifier要求selected subset forest、
  intersection不超过任一集合、`union=left+right-intersection`、selected intersection
  不超过forest intersection，并要求structural pair恰等于both-accept。所有累计量
  继续受signed 64-bit界；v1--v13可读，v13旧pair保留象限但span计数为零，不伪造历史
  correspondence；
- 科研指标：artifact新增micro-averaged selected-span precision/recall/Jaccard、
  forest-span precision/recall/Jaccard和boundary Jaccard。Selected指标衡量两个
  concrete projection是否产生相同分段；forest precision衡量secondary span是否被
  complete forest支持；forest recall低可能只是完整歧义包含更多结构，不能单独解释
  为错误。Boundary Jaccard弱化层级差异，适合先定位lexer/terminal segmentation问题；
- 自动化证据：真实Lark `a+`与逐byte selected fixture在同一candidate上得到非零
  selected/forest shared span，验证selected subset、两类intersection和union恒等式；
  primary reject/secondary accept不产生structural pair。Protocol fixture封存一组
  非平凡span/boundary计数并拒绝union篡改；state save/reload与全部旧parser fixture
  已升级schema 14。最终381项Python、109项LLVM lit、原生build、Ruff、全目录
  `py_compile`、`git diff --check`和9份权威文档链接检查通过；Z3/cvc5各6/6
  String/BV conformance concrete-verified，research evidence v2验签通过；
- 边界与下一步：byte span correspondence不是symbol/production语义对齐，也不能说明
  哪个grammar更正确。下一步应在paired tuning中先检查acceptance四象限和boundary
  Jaccard，再对稳定的一对grammar建立显式symbol mapping/terminal taxonomy；只有
  span证据接近且Earley成本仍显著时，native GLL或增量SPPF性能工程才具有可解释性。
  当前等级 I/T/E。

## 228. F233：Independent GLR/SPPF Differential Oracle（2026-07-28）

- 研究问题：F229的complete forest来自单一Lark Earley实现。即使manager能验证DAG
  自洽，也无法发现provider对grammar conflict或packed sharing的系统性遗漏。F233
  选择Parglare 0.21.1：其论文与公开API明确实现Farshi/Rekers GLR，并把SPPF作为
  `Forest.result`的Parent/possibility共享图直接暴露；
- 真实forest而非树枚举：新增`util/parglare_sppf_parser.py`与
  `parglare>=0.21,<0.22`。`GLRParser`固定`build_tree=true`、关闭shift、
  shift-over-empty与lexical preference。编码只读取`Forest.result`，不访问
  `solutions`、`get_tree`或lazy tree iterator。Parent成为symbol node，每个
  NodeNonTerm production成为独立`@packed` wrapper，NodeTerm成为`@token` wrapper，
  child Parent以object identity intern，保留GSS/SPPF汇合后的sharing；
- 共用验证不变量：Parglare encoder复用F229已经测试的acyclic/reachable检查、
  zero-width nullable折叠、primary alternative-zero ownership clone、topological
  order及4096 node/16384 edge/8 alternative/64 child/32 nullable/1 MiB hard bounds。
  不同production即使child相同仍有不同wrapper。Cyclic SPPF表示无限歧义，不能无损
  进入forward trace v3/v4，故明确失败而非枚举或截断；
- Byte contract：candidate以Latin-1一对一解码，Parglare character position因此
  等于原始byte offset；grammar必须按byte alphabet描述非ASCII terminal。Grammar
  UTF-8文件限制1 MiB并绑定SHA-256。持久mode-0600 Unix daemon只构造一次grammar和
  LR table，仍对每个candidate完整GLR parse；
- Provider proof：新增`parglare-glr-complete-sppf-v1`与
  `symcc-parglare-glr-sppf-telemetry-v1`。Manager只在schema/proof/version/grammar
  配对正确时接受，并继续从规范化trace复算encoded/raw/clone、alternative、edge、
  nullable rule、accepted/complete和elapsed关系。Syntax error输出negative proof
  trace并返回1；grammar/internal/cycle/bound失败返回2；
- Differential composition：F231 wrapper可把Lark Earley作为primary、Parglare GLR
  作为secondary，或交换顺序。嵌套Parglare forest经过同一manager v1--v4 validator，
  F231四象限与F232 span/boundary指标因而能比较两个真正generalized parser，而不是
  只比较complete forest与selected CST；
- 自动化证据：7项新测试覆盖真实`S -> S S | a`二叉歧义、双alternative与shared
  incoming edge、nullable v4 fixed-point certificate、syntax-negative、无限循环、
  node bound、invalid grammar、proof篡改、持久RPC/manager/artifact，以及同时启动
  Lark和Parglare daemon的candidate-paired both-accept/structural-overlap链。初步
  专项全部通过。最终388项Python、110项LLVM lit、原生build、Ruff、全目录
  `py_compile`、`git diff --check`、依赖闭包与9份权威文档链接检查通过；
  Z3/cvc5各6/6 String/BV conformance concrete-verified，research evidence v2
  验签通过；
- 科研边界：Parglare论文明确其实现为pure Python，且当前adapter不做incremental
  parse。因此F233提供algorithmic differential oracle与第二个真实complete-SPPF
  producer，不关闭native/incremental forest性能缺口。参考：
  [Parglare parse forests](https://igordejanovic.net/parglare/stable/parse_forest_trees/)、
  [Parglare software paper](https://doi.org/10.1016/j.scico.2021.102734)。
  当前等级 I/T/E。

## 229. F234：Proof-Carrying Cross-Parser Grammar Correspondence（2026-07-28）

- 研究问题：F232只能说明两个parser在byte span和boundary上相似，不能回答
  `Lark.start`与`Parglare.S`、或两侧production在观测数据上是否承担相同结构角色。
  直接按名称匹配会把命名习惯误当语义；仅按span匹配又会在递归grammar的同区间
  unary chain上产生多解。F234实现保守、可复检的sample-conditioned correspondence；
- 透明结构摘要：manager只处理两份trace都独立验证且共同接受的alternative-zero
  projection。`@packed`、`@item`、`@token`等provider合成wrapper被透明展开，真实
  grammar symbol保留。每个节点以`(start,end,epsilon)`、相对child span和child递归
  摘要形成parser-neutral Merkle shape；实现使用显式栈，可处理1500层回归和最多
  4096 node边界，不受Python recursion limit影响；
- 唯一性门控：符号对应必须在双方同一个`span+shape`桶中各恰有一个节点。同span
  但同shape仍多解时记录`ambiguous_symbol_spans`并不猜测。产生式对应还要求双方
  去wrapper后的有序direct-child partition完全一致且非空；shape SHA-256、
  两侧symbol/state与observation count共同构成证据。该规则允许同span不同递归层级
  由shape区分，同时拒绝仅凭名称或遍历顺序配对；
- State v15：每个record保存canonical
  `symcc-parser-correspondence-v1` JSON及SHA-256，累计两侧semantic node、
  symbol correspondence、ambiguous span和production correspondence。重复验证以
  parser pair相同为前提合并规范计数；restore复算JSON canonical form、摘要、label/
  shape bounds、排序唯一性、observation sum及
  `production <= symbol <= min(primary,secondary)`。V1--v14继续可读，但旧state
  对应字段置零，不回填不存在的历史证据；
- Sealed artifact：输出按parser pair聚合、排序且无重复的完整symbol/production
  correspondence表，并绑定distinct mapping数与observation总数。Manager verifier
  和`benchmark/research_protocol.py`独立复检表结构、总和、排序、digest和集合上界；
  研究指标新增两侧symbol alignment rate及production/symbol alignment rate。
  映射count或artifact observation被篡改，即使攻击者重算顶层artifact摘要也会失败；
- 自动化证据：真实Lark与逐byte parser得到非零symbol mapping；真实Lark Earley和
  Parglare GLR在二叉歧义grammar上同时得到symbol与production mapping。另覆盖同
  span同shape多解拒绝、1500层显式栈、canonical state摘要篡改、artifact mapping
  count篡改、v14迁移、保存恢复和两套verifier。当前全量390项Python通过；LLVM lit、
  原生build、双solver与research evidence在最终门禁中复核；
- 学术定位与边界：该方法与grammar convergence中“先规范化、再建立结构关系”的
  思路一致，但这里只从已观测共同样本提取保守对应，不生成grammar transformation，
  不证明两个CFG语言等价，也不把低support mapping用于授权候选。正式研究必须报告
  support、歧义率、跨seed稳定性及holdout复现。Libmarpa官方bocage API虽为原生C
  紧凑forest，但公开accessor只有ambiguity/null指标，不能直接遍历packed graph，
  因而本轮不依赖其内部ABI。参考：
  [Recovering Grammar Relationships](https://ir.cwi.nl/pub/18063)、
  [Guided Grammar Convergence](https://arxiv.org/abs/1503.08476)、
  [Libmarpa bocage API](https://jeffreykegler.github.io/Marpa-web-site/libmarpa_api/latest/index.html)。
  当前等级 I/T/E。

## 230. F235：Bounded Exhaustive Parser-Language Differential Certificate（2026-07-28）

- 研究问题：F231--F234都由fuzzing实际产生的candidate采样，零disagreement仍不能
  排除未采样短串上的grammar差异。一般CFG等价不可判定；因此F235不声称解决无界
  等价，而是对研究者显式给出的有限byte alphabet和最大长度建立完整域证书；
- 实现：新增`util/parser_equivalence_audit.py`。Alphabet限制1--16个不同byte并
  canonical排序，枚举采用shortlex：先长度0，再按byte字典序。精确case数为
  `sum(|alphabet|^length, length=0..max_length)`，硬限制65536，避免把无法完成的
  天文域标为exhaustive。最大长度可到64，因此单byte alphabet仍可检查长链；
- 复用可信链：每个候选调用F231的无shell并发wrapper，随后把paired trace交给
  `VerifiedProposalManager._load_parser_trace`。两侧return code、accepted bit、
  candidate/command/trace digest、v1--v4结构、complete-SPPF provider proof和
  nullable certificate全部沿用既有复检。任一timeout、rc2、坏trace或manager拒绝
  使整个audit失败，不把未完成域写成complete；
- Artifact：`symcc-parser-bounded-equivalence-v1`携带proof
  `exhaustive-shortlex-byte-domain-v1`、规范alphabet、max length、case count、
  四象限、mismatch、命令摘要、全case transcript SHA-256、elapsed和最多256个
  shortlex反例。`minimal_counterexample`必须等于反例表首项；表截断显式记录。
  Artifact自带canonical SHA-256，独立`--verify`复检域基数、四格和、排序、反例
  域成员关系及`equivalent iff mismatches==0`；
- CLI语义：默认即使发现差异也成功写出研究工件；`--require-equivalent`在存在反例
  时返回1，内部/未完成错误返回2。这样CI可以把有限域一致作为gate，而探索性实验
  不会因发现有价值的反例丢失artifact；
- 自动化证据：fake parser在alphabet `{a,b}`、长度0--2的7个case上分别覆盖完全
  一致和5个secondary-only差异，验证空串为shortlex最小反例、截断、CLI 0/1/2、
  digest重算后的counter tamper、重复alphabet与超65536域拒绝。真实Lark Earley与
  Parglare GLR在`{a}^{0..3}`上完成4/4穷举，空串共同拒绝、其余3串共同接受；
- 顺带修复：Parglare 0.21.1在“不接受空输入”时，其错误消息构造会对空line list
  抛`IndexError`。Adapter仅对`candidate==b""`且精确异常文本执行已知兼容路径，
  输出普通negative proof trace；其他`IndexError`仍fail closed。新增empty-negative
  manager回归，避免掩盖一般内部错误；
- 学术定位：bounded equivalence、ambiguity与inclusion已有SAT-based研究；近期CFG
  (in)equivalence框架也明确一般问题不可判定，并组合canonization、理论子类和反例
  解释。F235当前是parser implementation级穷举，而不是grammar公式SAT编码；它适合
  CI小域和最短反例发现，正式语言等价仍需受支持grammar子类的证明器。参考：
  [Analyzing CFGs Using an Incremental SAT Solver](https://doi.org/10.1007/978-3-540-70583-3_34)、
  [Detecting and explaining CFG (in)equivalence](https://arxiv.org/abs/2407.18220)、
  [Comparison of CFGs from generated test data](https://ir.cwi.nl/pub/18555)。
  最终393项Python、111项LLVM lit、原生build、Ruff、全目录`py_compile`、
  `git diff --check`、依赖闭包和9份权威文档链接检查通过；Z3/cvc5各6/6
  String/BV conformance concrete-verified，research evidence v2验签通过。
  当前等级 I/T/E。

## 231. F236：Proof-Carrying Assumption-Conflict PSCache（2026-07-28）

- 研究问题：F32只持久化最终SAT model，F35只在helper内试探recent SAT assignment，
  尚未利用求解搜索中失败的输入赋值。PSCache原论文在CDCL冲突发生时把当前SAT
  assignment投影到已跟踪的输入bit，重建成bit-vector值，并将它附着到实际满足的
  path prefix或off-path branch。系统当前链接Ubuntu系统Z3 4.8.12，而不是仓库中的
  vendored Z3；直接修改私有SAT trail既不会进入当前binary，也会把实现绑定到不稳定
  ABI。因此F236实现backend-stable的assumption-conflict实例，并明确不把它称作原始
  CDCL trail hook；
- 冲突采集：persistent `symcc-query-solver`从当前concrete witness和未命中的cached
  assignment重建`input_offset == byte`假设，以`solver.check(assumptions)`检查完整
  prefix+target。只有结果为UNSAT且`solver.unsat_core()`包含至少一个输入假设时才
  产生`ConflictSolution`。公式本身UNSAT时，删除检查会把core降为空并丢弃，避免把
  assignment-independent矛盾伪装成输入冲突；
- Core证书：对Z3报告的assumption core执行受预算约束的deletion minimization。
  每次删除后仍UNSAT则永久删去该byte；SAT证明该byte在当前core中必要；unknown或预算
  耗尽保留byte但设置`core_minimal=false`。最终core必须再次返回UNSAT。Helper输出
  `symcc-solver-conflict-solution-v1`，包含完整输入投影、非空core投影、来源、
  `z3-assumption-unsat-core-v1`、proof check数和最小化状态；每请求最多64条；
- 输入重建与进程内复用：符号名继续沿用Query IR数字byte declaration，因而无需从
  私有bit-blast variable反查SMT AST。SAT model与冲突assignment统一进入有界、
  去重、move-to-front cache；后续命中仍必须在当前完整公式下由Z3返回SAT，任何
  conflict miss都不能推出新query UNSAT；
- Store二次验证：`QueryStore._validate_result()`严格检查schema、类型、offset
  canonical uniqueness、core非空、core为assignment同值子集、proof kind及
  `proof_verified=true`，并生成canonical certificate SHA-256。入库时再用源query
  的独立Query IR evaluator检查完整assignment确实使至少一个root为假；helper声称
  与Query IR求值不一致时拒绝写入。`proof_json`、provenance和source-verified bit
  随SQLite row持久化；
- Prefix/off-path索引：新增`query_literals`和`partial_solution_literals`。
  Query root被规范化为`(base expression hash, polarity)`；连续`lnot`通过翻转polarity
  消除，因此源query中不满足的正root可与后续query显式出现的反root共享identity。
  每个冲突assignment只连接它实际满足的signed literal；lookup先按literal overlap、
  再按旧clause overlap和recency排序。候选仍须满足目标query全部Query IR roots才写
  `async-partial-*`，manifest记录conflict provenance、source proof digest和两类
  overlap，不改变SAT/UNSAT result cache；
- 预算与遥测：新增`SYMCC_SOLVER_PSCACHE_CONFLICTS`、
  `CONFLICT_ASSIGNMENTS`、`CONFLICT_SOLUTIONS`、`CONFLICT_CORE_CHECKS`和
  `CONFLICT_TIMEOUT`。结果与QueryStore stats记录conflict checks、retained solutions、
  conflict partial rows和signed-literal links，可用于同CPU的Kp/core-budget消融；
- 自动化证据：真实系统Z3 helper测试证明`input[0]==0x42`在witness 0下得到单byte、
  deletion-minimal、两次复核的core，随后`0x40<=input[0]<=0x50`仍由cached 0x42
  命中；固有矛盾`input[0]==0x41 && input[0]==0x42`不会产生冲突记录。QueryStore
  测试证明冲突0可在另一个`input[0]==0`查询上经Query IR验证后物化，并拒绝core
  非子集、空core、未验证proof及canonical offset alias；
- 学术定位与边界：F236覆盖论文算法中的“冲突来源、输入值投影、prefix/off-path
  相关性、Kp式预算和快速验证”语义，并额外提供可复核core；但它观察的是显式输入
  assumptions导致的语义冲突，不是Z3 bit-blast CDCL每次BCP冲突时的内部
  `curModel`。因此可能收集更少、分布不同，仍需用公开benchmark比较命中率和
  coverage/CPU-hour。若未来构建自有Z3，应以版本化公开扩展接口接入SAT trail并保留
  F236为backend-neutral对照组。参考：
  [PSCache FSE 2024](https://doi.org/10.1145/3660817)、
  [公开论文PDF](https://zbchen.github.io/files/fse2024.pdf)、
  [Z3 solver assumptions/unsat-core API](https://z3prover.github.io/api/html/classz3_1_1solver.html)。
  当前等级 I/T/E。

## 232. F237：Proof-Carrying Backend-Neutral QF_BV Portfolio（2026-07-28）

- 研究问题：G06此前的`PortfolioSolver`能够并行运行多个helper并隔离SAT/UNSAT
  disagreement，但所有Query IR helper实际上仍是同一Z3实现或由使用者手写协议适配。
  这不足以证明cvc5/Bitwuzla等异构solver收到与Z3相同语义的公式，也没有在solver启动
  前声明operator/width/model/UNSAT能力。F237在QueryStore CAS边界实现独立QF_BV
  lowering和显式capability matrix，不复用`Z3_solver_to_string`产生的文本；
- Capability contract：新增`util/qf_bv_backend.py`和
  `symcc-qfbv-capability-v1`。合同规范化logic、38类Query IR operator、
  `max_bits/max_nodes/max_input_bytes`、`model_values`和`accept_unsat`，并绑定
  canonical SHA-256。未知operator、越界或非规范合同在启动外部进程前fail closed。
  `accept_unsat`默认false；未授权后端的UNSAT只作为`backend_status=unsat`遥测，
  顶层结果降为unknown，因此不能进入UNSAT subset cache；
- Sort/shape proof：content-addressed expression DAG由roots递归加载，lowerer拒绝cycle、
  missing node、错误arity、Bool/BV混用、宽度不匹配、越界extract、窄化extension、
  非8-bit input read和非Bool root。Concat/extract/extension、算术、signed/unsigned
  division/remainder/comparison、bitwise/logical、shift、ITE与variable rotate均直接
  降到标准QF_BV；rotate显式用`bvurem`规范化shift，不依赖solver私有扩展；
- Lowering certificate：`symcc-qfbv-lowering-v1`绑定query ID、root/reachable-node数、
  每operator精确计数、最大位宽、input-offset集合摘要、capability摘要、最终SMT-LIB
  摘要、sort验证bit和`get-value` model protocol。QueryStore重新计算两个证书摘要，
  检查operator计数和node总数、operator属于advertised set、所有计数/宽度落在合同
  上限内，并拒绝重算hash后的内部字段篡改；
- 模型可信链：`SmtLibQfbvSolver`对每个8-bit input symbol发出标准
  `(get-value (...))`，有界S-expression parser接受`#x/#b/(_ bvN 8)`三种标准值。
  缺失、重复域外值、响应过大、非法status或进程错误均不产生SAT。得到完整assignment
  后，adapter先patch当前witness并用Query IR evaluator检查全部prefix+target roots；
  `QueryStore.complete()`再从CAS独立加载表达式，对最多4096个source witness重复验证。
  只有两次都通过才允许`backend_model_verified=true`的SAT进入result/candidate cache；
- Portfolio接入：`SYMCC_QUERY_SOLVER_PORTFOLIO`新增
  `kind=smtlib-qfbv`，command支持`{query}`/`{timeout_ms}`内嵌placeholder。
  该kind固定one-shot；原有`kind=symcc-json`继续支持persistent prefix server。
  Attempt telemetry新增backend kind、capability status、model/UNSAT authorization、
  capability/lowering digest和bounded reason；原有disagreement=>unknown边界不变；
- 自动化证据：`test/test_qf_bv_backend.py`的7项测试覆盖canonical capability与
  S-expression model、缺失byte拒绝、pre-launch capability rejection、portfolio
  config、真实cvc5 1.1.2 SAT模型双验证、UNSAT未授权/授权差分、assignment篡改与
  重算certificate hash后的operator-count篡改。完整operator matrix在同一真实cvc5
  query中覆盖全部38类IR operator并得到`input[0]=0x42,input[1]=0x03`，随后由独立
  evaluator复检；
- 学术定位与边界：这完成了G06的backend-neutral QF_BV语义和capability基础层，
  不是完整SMTgazer。当前环境有cvc5 1.1.2但没有Bitwuzla executable，因此Bitwuzla
  仅有标准命令配置和未执行适配路径，不宣称实机conformance。外部UNSAT授权仍是
  experiment trust choice，不是proof object；未来应增加proof/checkable core或
  至少Z3/cvc5/Bitwuzla consensus实验。本阶段Backend为one-shot；F238--F242后来补
  persistent context、X-means/BIC、ensemble/BO及SAT-gated cancellation，但尚无
  native solver API interrupt或跨worker clause transport。参考：
  [SMTgazer ASE 2025](https://conf.researchr.org/details/ase-2025/ase-2025-papers/47/SMTgazer-Learning-to-Schedule-SMT-Algorithms-via-Bayesian-Optimization)、
  [SMT-LIB 2.7](https://smt-lib.org/papers/smt-lib-reference-v2.7-r2025-02-05.pdf)、
  [cvc5 CLI/SMT-LIB quickstart](https://cvc5.github.io/docs/latest/binary/quickstart.html)、
  [Bitwuzla CLI](https://bitwuzla.github.io/docs/binary.html)。
  最终402项Python、112项LLVM lit、原生build、Ruff、全目录`py_compile`、
  `git diff --check`和依赖闭包均通过；Z3/cvc5各6/6 String/BV conformance及
  research evidence验签保持通过。
  当前等级 I/T/E。

## 233. F238：Prefix-Keyed Persistent SMT-LIB QF_BV Contexts（2026-07-28）

- 研究问题：F237的一次性异构backend消除了Z3 printer依赖，但每个query仍重复启动
  solver、解析相同prefix。G01已有prefix key和Z3 helper push/pop复用；若cvc5路径
  不能共享相同语义单位，异构portfolio的进程开销会掩盖solver互补性。F238新增
  `PersistentSmtLibQfbvSolver`，在不依赖cvc5/Bitwuzla语言绑定的前提下使用标准
  interactive SMT-LIB增量命令；
- 精确context ownership：每个content-addressed `prefix_key`独占一个solver process，
  context记录规范prefix-term digest与已声明input offsets。首次请求声明当前完整
  input集合并永久assert prefix roots；后续同prefix target仅执行
  `push 1 -> assert target -> check-sat/get-value -> pop 1`。新target使用新offset时，
  declaration在push外增加，不改变prefix可满足语义。Prefix key与重新lower的term
  digest不一致时销毁context并返回error，不依赖hash名字猜测等价；
- 有界进程池：`prefix_cache`默认4、范围1--64，以LRU回收完整process而不是在一个
  solver中用`reset`伪造多context。这样每个prefix的assertion stack互不污染；
  telemetry沿用`prefix_cache_hit/entries`并新增
  `smtlib-prefix-process-push-pop-v1` context protocol。每个worker/backend持有独立
  pool，尚不做跨worker共享；
- Framing与恢复：每次请求附加唯一SMT-LIB `echo` marker，后台reader在8 MiB边界内
  读取到marker，故多行`get-value`和UNSAT后的error响应不会与下一请求串包。外部
  wall-clock deadline独立于solver方言；timeout会terminate/kill并移除单个prefix
  process。EOF、broken pipe、非法初始化响应和超大输出也销毁context；下一请求冷建，
  不复用可能已失步的solver；
- Capability与配置：`symcc-qfbv-capability-v1`新增canonical
  `incremental` Boolean，默认false。`persistent=true`必须同时声明
  `incremental=true`，且interactive command禁止`{query}/{timeout_ms}` placeholder。
  Query service用`ExitStack`保证worker退出时关闭全部process；one-shot F237路径保持
  不变。旧F237 v1 artifact缺少该可选字段时按false及其原始摘要验证，避免协议扩展
  破坏已有result store；
- 可信边界：F238从F237同一次strict lowering取得prefix/target terms，完整formula
  lowering certificate仍绑定所有roots和最终标准SMT-LIB。每个增量SAT继续执行adapter
  evaluator与QueryStore CAS evaluator两级模型复核；UNSAT仍受显式
  `accept_unsat`控制，portfolio disagreement仍降为unknown。Context cache只优化
  解析与prefix assertion，不提供新的证明权限；
- 自动化证据：真实cvc5 1.1.2在容量1的pool上依次运行`prefix=true,target=rol`
  的两个query，第二次精确命中并恢复`0x43`；切换到不同UNSAT prefix触发LRU驱逐，
  再切回原prefix的新target为cold miss并恢复`0x44`。独立fake interactive solver
  在首个check写marker后挂起，外部1 ms query budget触发kill和cache清空；同一lease
  第二次调用冷启成功并恢复`0x42`。定向文件现有9项测试，且ResourceWarning提升为
  error时仍无process/pipe泄漏；
- 学术定位与边界：该设计提供backend-neutral exact-prefix incremental baseline，
  但每个prefix一个OS process的内存成本高于原生API多context或可持久化clause数据库。
  它不迁移learned clauses，也不支持跨prefix solver state subsumption。F242后来
  补充process-group cancellation与cold context recovery，但不是native API
  interrupt。下一阶段应测量cache hit/RSS/CPU后决定
  是否实现单process replay stack、native cvc5/Bitwuzla API adapter或跨worker
  context transport。参考：
  [SMT-LIB 2.7](https://smt-lib.org/papers/smt-lib-reference-v2.7-r2025-02-05.pdf)、
  [cvc5 incremental quickstart](https://cvc5.github.io/docs/latest/binary/quickstart.html)。
  最终404项Python、112项LLVM lit、原生build、Ruff、全目录`py_compile`、
  `git diff --check`、依赖闭包和9份权威文档链接检查通过；Z3/cvc5各6/6
  String/BV conformance及research evidence验签保持通过。
  当前等级 I/T/E。

## 234. F239：Proof-Carrying X-means/BIC Censored Sequence Prior（2026-07-28）

- 研究问题：原`SMTAlgorithmScheduler`把“样本数至少16且最大轴方差超过阈值”称为
  online X-means-style，但它不保存实例、没有k-means refinement或BIC model
  selection，也只能按cluster/arm在线Thompson采样。`offline_policy.py`能够对全局
  constant action做SNIPS/DR门控，却不能学习context-specific solver sequence。
  F239把两条链分开：在线调度继续保持低开销，离线工具
  `util/smt_sequence_training.py`产生可复现、可独立复算的上下文先验；
- 统一特征合同：`symcc-smt-context-features-v1`与在线调度器使用相同9维有界向量：
  bias、`log1p(input_bytes)`、difficulty、timeout ratio、solver-unknown ratio、
  dependency ratio、data uncertainty、branch pressure和targeted bit。轨迹记录新增
  `solver_unknown_ratio/branch_pressure/timed_out`；旧JSONL缺字段时按保守默认值
  加载，schema 1保持兼容；
- 有界X-means/BIC：从单簇开始，对每个可分簇沿最大方差轴选择确定性极端种子，
  执行最多256轮Lloyd二均值；child均须达到`min_cluster_size`。Parent与two-child
  spherical-Gaussian mixture分别计算含mixing/variance/centroid参数罚项的BIC，
  仅当gain严格超过margin时接受全局最佳split，最多32簇。相同输入、参数和轨迹产生
  byte-identical artifact；退化同点簇不会被虚假拆分；
- 删失目标与保守门控：每条明确`timed_out`或`killed`记录按`2*assigned timeout`
  收取PAR-2（F240前的旧轨迹无stage budget时回退全局timeout），
  其他cost截断到同一上界；utility为interference-adjusted reward减去归一化log-cost。
  每个cluster/action使用有界inverse-propensity weights，计算ESS、局部utility、
  PAR-2均值与标准误，再以全局action estimate做经验Bayes shrinkage。只有raw
  samples、ESS和lower confidence bound同时达到门槛才输出sequence；无支持簇输出
  空建议，绝不强制一个未证实arm；
- Proof-carrying artifact：`symcc-smt-sequence-policy-v1`绑定完整trajectory的
  canonical摘要、action set、特征/目标/训练schema、训练参数、centroid/SSE/member
  digest、全部split trial与最终BIC。每个estimate封存weight sum/squared-weight、
  raw utility sum、加权utility一/二阶矩和加权PAR-2和。验证器独立重算BIC、ESS、
  局部/收缩utility、标准误、LCB、support bit、recommendation、cluster/global
  behavior与样本/删失总数；只改外层摘要或修改内部数值后重算摘要均不能绕过这些
  一致性检查。这是自洽计算证书和内容完整性，不是对训练数据来源的数字签名；
- 在线接入：`SYMCC_SMT_ALGORITHM_PRIOR=<artifact>`在scheduler启动时fail closed
  加载；action必须属于当前受限sequence space。若当前没有
  `OfflinePolicyController`显式建议，nearest verified centroid的supported sequence
  才成为原有90%利用/10%探索的偏好；活动中的多stage sequence不被中途改写。
  assignment记录artifact SHA-256、cluster和建议，state/benchmark记录建议数、
  实际命中数与match rate。更换artifact时历史prior计数不会错误继承；
- 自动化证据：7项新测试覆盖双峰轨迹稳定产生两个互补建议、同点BIC拒绝、
  killed记录精确收取`2T`、小样本不建议、摘要篡改和重哈希BIC篡改拒绝、verified
  prior仅作为在线偏好、损坏artifact忽略以及train/verify CLI。另以500条确定性
  随机轨迹构造8簇/28次split trial，制品通过全量独立复算。最终411项Python、
  113项LLVM lit、原生build、Ruff和定向`py_compile`通过；
- 学术定位与边界：F239实现了SMTgazer公开描述中的X-means实例分组、删失cost-aware
  sequence学习接口和保守online prior，但估计器是IPS+empirical-Bayes LCB，不是论文
  dataset-level Bayesian optimization，也没有boosting/bagging surrogate。Logging
  policy的残余confounding、cluster drift和跨benchmark迁移仍须以holdout和同CPU
  confirmatory campaign测量。F240已继续实现可消融的bagged/boosted surrogate与
  budgeted sequence optimizer；仍需fixed-version Bitwuzla conformance、
  Z3/cvc5/Bitwuzla equal-budget corpus。F241已继续实现schedule-level Bayesian
  acquisition，F242已实现SAT-gated running-attempt cancellation，但真实holdout
  效果仍待测量。参考：
  [SMTgazer ASE 2025](https://conf.researchr.org/details/ase-2025/ase-2025-papers/47/SMTgazer-Learning-to-Schedule-SMT-Algorithms-via-Bayesian-Optimization)、
  [X-means](https://www.cs.cmu.edu/~dpelleg/download/xmeans.pdf)、
  [PAR-2 in SAT competition evaluation](https://satcompetition.github.io/2024/rules.html)。
  当前等级 I/T/E；尚无覆盖率提升的R级声明。

## 235. F240：Bagged/Boosted Censored-Cost Budgeted Sequence Optimizer（2026-07-28）

- 研究问题：F239能为每个X-means context选择一个已有sequence，但SMTgazer的核心是
  把互补算法组织成共享预算内的sequential portfolio，并用bagging/boosting增强
  surrogate泛化。现有轨迹是带propensity的单action观察，不包含每个instance对所有
  action的完整反事实矩阵；直接把未观察结果当失败会产生选择偏差。F240新增
  `util/smt_sequence_optimizer.py`，明确采用受限监督surrogate，而不声称恢复了无偏
  virtual-best matrix；
- 三目标删失模型：每个action分别学习`completion`、`par2_ratio`和
  interference-adjusted `reward`。明确timeout/kill为completion 0；新轨迹还记录
  `solved`，旧轨迹缺失时仅以非删失作为兼容近似。F239的stage-aware objective现读取
  `budget_sec`：删失action按`2*stage_budget`计费，再相对全局campaign timeout
  归一化，避免15秒stage错误收取60秒PAR-2；
- 确定性ensemble：不新增NumPy/scikit-learn依赖。每个target训练3--32棵由稳定
  SHA-256 seed驱动的bootstrap shallow CART；每棵树在9维context上以weighted SSE
  选择至多32个确定性threshold。另以1--32轮residual tree和0.01--1.0 shrinkage做
  gradient boosting。两路预测等权融合；bag variance与boost residual RMSE构成
  uncertainty。树深最多5、每leaf最少2、action最多沿用F239的32类边界；
- 保守schedule评价：completion使用LCB，cost使用UCB，reward使用LCB。候选stage
  预算来自离散fraction并四舍五入为真实整数秒；同action不重复，总预算不超过全局
  `T`。期望PAR-2显式保留未解概率的`2T`尾罚，再与reward LCB形成可配置目标。
  `budgeted-sequence-beam-search-v1`最多8 stage、beam 4--1024；只有multi-action
  objective至少比最佳single改善门槛时才升级，否则保守退化为single；
- 全局与局部优化：同一ensemble同时在全部X-means centroid（按cluster样本加权）
  上生成dataset-level schedule，并为每个cluster单独生成localized schedule。模型
  样本不足的action不进入搜索。互补synthetic corpus中，两个action分别只解决一个
  context；优化器选择两个15秒stage，objective严格优于任一single。一个action不足
  门槛时只产生支持充分的single schedule；
- Sealed artifact与复算：`symcc-smt-sequence-ensemble-policy-v1`嵌入完整verified
  F239 base artifact，绑定trajectory/base摘要、配置、modeled action、所有CART/
  residual tree、cluster centroid和global/local schedule。验证器检查tree深度、
  node/sample守恒、boost root weight、target bounds、模型样本与F239 IPS weight，
  然后从sealed model和context重新运行全部beam search，逐字段复算candidate count、
  best-single、objective、PAR-2、reward、unsolved probability与最终schedule。
  它证明“所封模型推出所封schedule”，不证明模型等于未观察反事实；
- 可执行在线协议：同一`SYMCC_SMT_ALGORITHM_PRIOR`可加载F239或F240 schema。F240
  schedule绑定到seed replay；90/10探索若偏离建议则不推进，命中后推进到下一action。
  每个action内部原有multi-stage progression先完成，再进入schedule下一action。
  `SYMCC_ALGORITHM_BUDGET_SEC`由worker在1..global timeout内规范化后传给真实
  `ExecutorPortfolio.resolve()`，不是只写遥测。State/benchmark新增prior kind、
  budgeted assignment和schedule completion计数；
- 轨迹可辨识性：`OfflinePolicyController.observe()`新增bounded `instance_id`、
  `solved`和`budget_sec`。MPI使用输入content SHA-256作为instance ID，并修复此前
  semantic fallback过早`pop`该hash导致后续策略轨迹拿不到identity的问题。这为未来
  paired action matrix与doubly-robust sequence estimator提供稳定连接键；
- 自动化证据：`test/test_smt_sequence_optimizer.py`的5项测试覆盖确定性互补schedule、
  under-supported action排除、摘要/重哈希模型篡改、跨replay预算推进和CLI
  train/verify；`test_offline_policy.py`新增censor/instance/budget协议测试，F239
  PAR-2测试改为5秒stage在10秒全局预算下精确收取10秒。完整门禁为417项Python、
  114项LLVM lit、Z3/cvc5各6/6 concrete conformance、research evidence、Ruff、
  源码边界`py_compile`、pip和文档链接检查；
- 学术定位与边界：ASE 2025公开摘要明确SMTgazer使用dataset-level hyperparameter
  optimization、X-means及boosting/bagging surrogate；F240覆盖ensemble和可执行
  budgeted sequence search，但搜索器是确定性bounded beam search，不是论文的
  Bayesian acquisition loop。单臂logging仍可能有unmeasured confounding；executor
  route的额外timeout scale也需在实验manifest中固定。F241已继续实现
  expected-improvement式SMBO及exhaustive诊断，F242已补SAT-gated cancellation；
  下一步做cost-matched beam/SMBO holdout对拍、Bitwuzla和equal-CPU公开campaign。
  参考：
  [SMTgazer ASE 2025](https://conf.researchr.org/details/ase-2025/ase-2025-papers/47/SMTgazer-Learning-to-Schedule-SMT-Algorithms-via-Bayesian-Optimization)、
  [XGBoost](https://doi.org/10.1145/2939672.2939785)、
  [Random Forests](https://doi.org/10.1023/A:1010933404324)。
  当前等级 I/T/E；无R级效果声明。

## 236. F241：Bounded Expected-Improvement Schedule SMBO（2026-07-28）

- 研究问题：F240使用beam按当前surrogate objective直接排序，不能表达“在严格优化
  预算下优先评估不确定但可能改进的schedule”。F241新增
  `util/smt_schedule_smbo.py`，在封存的F240 global/per-cluster action模型上构造
  schedule-level sequential model-based optimization。该层研究优化器的样本效率，
  不把F240的观察性模型当作真实反事实oracle；
- 有界搜索空间：action顺序、整数秒预算和stage位置共同编码为
  `1 + L*(|A|+1)`维固定向量。候选池始终包含全部single-action/budget基线，枚举
  有序双action组合，并以稳定SHA-256 seed采样更长且不重复action的schedule；
  `max_candidates<=4096`、`max_schedule_length<=8`、总stage预算不超过campaign
  timeout。候选上限或评估预算无法容纳全部single基线时fail closed，防止基线被
  排序截断；
- Sequential acquisition：固定初始设计包含所有single基线及确定性hash选出的补充
  点。此后仅用已观测schedule训练3--16棵bootstrap shallow CART和1--16轮residual
  boosting；bag variance与residual RMSE合成预测标准差。最小化目标使用解析
  `EI=(f_best-mu-xi)Phi(z)+sigma*phi(z)`，每轮确定性选择最大EI并惰性调用一次
  sealed objective，最多512次selection evaluation。未选候选的目标在acquisition
  完成前不会计算或读取；
- 诊断隔离：选择结束后才对完整候选池计算post-hoc oracle，用于报告
  `simple_regret`。Artifact分别记录`evaluations`和
  `posthoc_oracle_evaluations`，因此oracle成本不会被伪装进优化预算，也不能影响
  acquisition trace。正式cost-matched实验应关闭或在两组都等价收取该诊断成本；
- Proof-carrying replay：`symcc-smt-schedule-smbo-policy-v1`嵌入完整verified F240
  artifact，绑定trajectory/ensemble摘要、候选池摘要、初始设计、逐轮预测均值、
  uncertainty、EI、incumbent转移、观测目标、global和每个X-means cluster的最终
  schedule。验证器先复核F240树和beam证书，再从封存模型、配置和稳定seed重放完整
  SMBO与post-hoc oracle；只改摘要或修改trace后重算外层摘要均被拒绝；
- 在线执行：scheduler在F239、F240 loader之后识别F241 schema，转换为同一
  `EnsembleSequencePrior`执行合同，但保留
  `prior_kind=smbo-expected-improvement-sequence`。nearest cluster schedule仍只占
  90% preference，跨seed replay推进，并通过F240已有
  `SYMCC_ALGORITHM_BUDGET_SEC`控制真实worker timeout；F241没有扩大UNSAT或模型信任
  权限；
- 自动化证据：5项专项测试覆盖二action且`max_schedule_length=3`的候选边界、严格
  6次评估/4点初始设计与确定性trace、EI的确定/不确定极限、摘要及重哈希trace篡改、
  online loader/budget/kind和train/verify CLI。F239/F240与scheduler定向回归保持
  通过；完整门禁为422项Python、115项LLVM lit、Z3/cvc5各6/6 concrete
  conformance、research evidence、Ruff、源码边界`py_compile`、依赖闭包、
  `git diff --check`和9份权威文档本地链接；
- 学术定位与边界：该实现落实SMTgazer公开描述中的dataset-level Bayesian
  optimization方向，但surrogate是确定性bagged/boosted CART近似，目标来自F240
  action surrogate，并非对真实solver schedule逐点昂贵采样；也不声称复现论文的
  私有搜索空间或训练代码。下一步必须在holdout query corpus上做beam与SMBO
  cost-matched simple-regret/PAR-2/CPU消融。F242已继续实现running-attempt
  cancellation；剩余fixed-version Bitwuzla对拍、feature schema v2和
  native/cross-worker context transport。参考：
  [SMTgazer ASE 2025](https://conf.researchr.org/details/ase-2025/ase-2025-papers/47/SMTgazer-Learning-to-Schedule-SMT-Algorithms-via-Bayesian-Optimization)、
  [Efficient Global Optimization](https://doi.org/10.1023/A:1008306431147)。
  当前等级 I/T/E；尚无R级solver或coverage效果声明。

## 237. F242：SAT-Gated Bounded-Consensus Portfolio Cancellation（2026-07-28）

- 研究问题：F34把同一query的solver attempts并行化，但`ThreadPoolExecutor`上下文
  会等待全部Future，所谓race没有winner早停；one-shot helper由`subprocess.run`
  隐藏进程句柄，persistent helper也没有query-scoped interrupt。尾部慢solver因此
  继续消耗CPU。直接在首个结果后全取消又会破坏现有SAT/UNSAT disagreement边界；
- 保守触发规则：F242新增显式
  `SYMCC_QUERY_SOLVER_PORTFOLIO_CANCEL_GRACE_MS=-1..60000`。默认`-1`完全保持旧的
  all-attempt collection；非负值仅在首个SAT后启动bounded consensus grace。
  UNSAT永不触发早停，仍等待全部attempt。Grace内出现SAT/UNSAT冲突仍返回unknown；
  grace结束后才取消未完成attempt。被早停SAT仍必须通过QueryStore的完整Query IR
  candidate复核，复核失败不会缓存模型，但可能损失一个较慢的有效fallback，这是
  明确的完整性/CPU权衡而非证明权限扩大；
- Future与进程两级取消：`PortfolioSolver`先取消尚未启动的Future，再对running
  backend的`cancel(lease)`协议发query-scoped interrupt。JSON和QF_BV one-shot
  backend均改为持有active `Popen`，使用独立process session并对整个process group
  先发SIGTERM、200 ms后升级SIGKILL，避免只杀wrapper而遗留solver child；
- Persistent恢复：persistent JSON server以I/O lock防止多service job交错line
  protocol，以独立state lock允许cancel线程在阻塞`readline`时终止process group；
  下一query检测dead server并cold respawn。Prefix-persistent QF_BV在每个framed
  `_request`登记active context，cancel使reader解阻、将该prefix context从LRU删除，
  下一次严格cold rebuild；不会把被中断的assertion stack继续复用；
- 可审计结果：portfolio attempt新增`cancelled/cancel_reason`，portfolio新增
  `cancellation_enabled/cancel_grace_ms/cancel_requested/cancelled_attempts/
  consensus_complete`。QueryStore stats新增`portfolio_cancel_requests`、
  `portfolio_cancelled_attempts`和`portfolio_incomplete_consensus`。`cancel_requested`
  与实际cancelled分开，因不支持cooperative cancel的自定义callable仍会被等待；
- 自动化证据：QueryStore新增3项测试覆盖真实5秒one-shot helper在1秒内被进程组
  中断、100 ms grace捕获快速SAT/UNSAT冲突、persistent JSON helper被取消后cold
  respawn；QF_BV新增2项测试覆盖one-shot interrupt及incremental prefix context
  drop/rebuild。所有测试以`ResourceWarning`为error，验证pipe/process回收；高并发
  lit曾触发process登记前cancel竞态，修复为query-level sticky cancellation并将两组
  进程测试各重复10次。最终427项Python、115项LLVM lit、Z3/cvc5各6/6、Ruff、
  `py_compile`、依赖闭包、research evidence、diff和9份文档链接门禁通过；
- 学术定位与边界：这是SAT-gated speculative portfolio early stopping和bounded
  cross-check window，不是完整solver consensus，也不是learned clause sharing。
  正式实验应对`-1/0/10/50/200 ms`报告wall time、solver CPU、cancel success、
  incomplete consensus、invalid SAT、disagreement、PAR-2和coverage/CPU-hour。
  下一步是fixed-version Bitwuzla实机对拍和context feature schema v2；只有同CPU
  结果支持时再投入native/cross-worker clause/context transport。当前等级 I/T/E。

## 238. F243：Query-IR Structural Context Feature Schema v2（2026-07-28）

- 研究问题：F239--F241此前只按输入长度、历史难度、timeout、unknown、dependency、
  data quality、branch pressure和target bit学习sequence。同为64-byte输入时，
  “线性byte equality”和“大型宽位非线性/shift/concat DAG”可能落入同一context，
  无法让SMTgazer式模型识别solver互补性。F243把已经存在于Query IR的结构信号接入
  实际runtime telemetry、MPI轨迹、在线cluster和全部离线artifact，而不是只在训练
  脚本中构造不可观测特征；
- 零额外遍历的数据生产：QSYM `Solver::exportQuery()`原本已经为序列化执行一次
  后序DAG遍历。F243在同一node loop内计算每个成功query的node count、不同
  `ReadExpr` input offset数、最大bit width，以及comparison、nonlinear arithmetic、
  bitwise/shift/rotate、structural/control四组operator count。只有Query IR临时文件
  原子rename成功后才提交执行级累积量；失败/过大/不可lower的export不会污染训练
  样本。`solver_telemetry.json`新增七个`query_ir_*`计数，Python
  `SolverTelemetry`自动做非负规范化并声明`query_ir_structure` capability；
- Schema v2：`symcc-smt-context-features-v2`在原9维prefix合同后追加7维，共16维：
  per-export mean node和input规模的`log1p/16`、最大width的`log2(bits+1)/16`，
  以及四个operator-group count除以总node count的比例，全部截断到`[0,1]`。
  用per-query均值避免一个执行导出更多branch query时仅因数量变大而改变结构规模；
  operator比例保留shape而不重复编码DAG大小。没有Query IR或未启用spool时追加维度
  全为0，旧engine仍能参与调度；
- 端到端传播：`OfflinePolicyController`把query数、node/input/width和四组直方图写入
  schema-1 trajectory context；`SMTAlgorithmScheduler.context()`使用同一
  `context_feature_vector()`实现，避免在线/离线公式漂移。完成一次seed replay后，
  16维结构context写入`seed_contexts`，用于该seed后续action选择；cold seed使用
  原9维默认值加7个0，不虚构尚未观察的query shape；
- 向后兼容迁移：artifact顶层policy schema保持不变，feature schema精确区分v1的
  9维和v2的16维。验证器只接受这两个完整的name/dimension/ordered-feature合同，
  从artifact自身维数重算centroid、SSE、每轮BIC、CART feature index和F240/F241
  schedule replay。旧v1 artifact摘要无需重写即可继续验证；新scheduler向旧prior
  传递context时确定性截取前9维。旧online state中的9维centroid/m2在加载时追加7个
  0；v2 prior遇到旧9维调用则补0。其他维数、乱序feature名、NaN或越界向量均
  fail closed；
- F240/F241合同：F240的base policy、ensemble feature schema和tree verifier必须
  三者维数一致；loader把受信dimension带入`EnsembleSequencePrior`。F241嵌入并
  复验F240 artifact，loader同样传播dimension/schema，因此既可执行历史9维
  SMBO制品，也可执行新16维制品，不把未知schema猜成当前版本；
- 自动化证据：新增/扩展测试覆盖16维数值公式与边界、真实9维F239 artifact的摘要
  验证及16→9在线投影、真实9维F240 ensemble的树复检/加载/调度、旧scheduler state
  的9→16迁移、runtime telemetry capability、离线轨迹字段和在线context。
  `test/query_ir.c`通过编译后的QSYM程序确认一个真实32-bit comparison query导出
  node/input/width/comparison计数；C++ runtime重新构建通过。专项Python共75项。
  最终434项Python、115项LLVM lit、Z3/cvc5各6/6 String/BV conformance、
  research evidence双family复检、Ruff、源码边界`py_compile`、依赖闭包、
  `git diff --check`和文档链接门禁通过；
- 学术定位与边界：四组histogram是有界、可解释的结构摘要，不是完整SMT AST
  embedding；它无法区分组内operator、DAG depth、shared-subterm topology或
  bit-blast clause规模。结构特征来自成功Query IR export，因此禁用query spool的
  campaign只有零值。下一步应在holdout query corpus做v1/v2 paired消融，报告
  cluster stability、action entropy、Brier/calibration、PAR-2、solver CPU和
  coverage/CPU-hour；在这些证据之前不声明调度性能提升。当前等级 I/T/E。

## 239. F244：Fixed-Version Bitwuzla QF_BV Conformance Evidence（2026-07-28）

- 研究问题：F237已经从CAS Query IR独立lower全部38类QF_BV operator，并由cvc5
  1.1.2完成模型双验证和UNSAT trust差分，但此前Bitwuzla只有未执行的配置示例。
  “命令格式看起来兼容”不能作为异构solver证据；版本漂移、不同二进制、模型parser
  差异或UNSAT默认授权都可能使portfolio的可信边界与文档不一致；
- 固定依赖身份：新增`benchmark/install_bitwuzla_0_9_1.sh`，固定官方Bitwuzla
  0.9.1 tag对应commit
  `8d1eb01093ae54d9b4586456b69c3bf31000a4c2`。脚本检查Meson/Ninja/pkg-config、
  GMP >= 6.3与MPFR >= 4.2.1，执行release/no-Python/no-testing构建，并在安装后
  精确检查`bitwuzla --version == 0.9.1`。本次实机构建使用CaDiCaL 2.1.2及
  upstream固定SymFPU commit `40bdec00...`；安装二进制SHA-256为
  `7a46a4be317a2df6d9a76b2c63971e02e25a868606a40b87e41454cccb5a48bc`；
- 单一规范矩阵：新增`util/qf_bv_conformance.py`，把原测试内的operator matrix
  提升为可复用生产构造器。公式同时包含`read/bool/constant`、结构、算术、signed/
  unsigned div/rem与比较、逻辑、shift/rotate、`ite`等全部38类当前operator，并有
  唯一期望input模型`{0:0x42,1:0x03}`。F237真实cvc5测试改用同一构造器，避免测试
  matrix和正式证据日后漂移；
- 三重可信探针：每个backend执行同一SAT matrix，并经过adapter的完整Query IR
  candidate validation与`QueryStore.complete()`从CAS进行第二次验证；随后对
  `x==0x42 && x==0x43`执行两次UNSAT。`accept_unsat=false`必须保留
  `backend_status=unsat`但对外返回`unknown`，`accept_unsat=true`才可返回
  authorized `unsat`。三项case都绑定完整capability和lowering certificate，
  verifier从规范envelope重建content-addressed query ID并重新lower，逐字段比较
  operator count、sort、input set、width、SMT-LIB与capability摘要；
- 版本化可重放证据：`symcc-qfbv-backend-conformance-v1`记录精确command、
  version command/output、期望/实际版本、解析后的绝对executable、二进制SHA-256、
  三项result、证书和store completion。`artifact_sha256`封存完整运行，
  `semantic_sha256`排除时间戳、elapsed和本机binary path/hash，用于不同运行环境的
  语义对拍。离线`--verify`不启动solver；`--replay`先验签原artifact，再按封存
  command重跑并要求语义摘要一致。未知版本、缺失binary、乱序/修改证书、模型或
  trust结果均fail closed；
- 实机结果：checked artifact
  `benchmark/evidence/qfbv_conformance_f244.json`最初绑定cvc5 1.1.2与Bitwuzla
  0.9.1；F245建立三solver campaign时又把系统Z3 CLI 4.8.12加入同一前置证书。
  三者均为38/38 operator、SAT模型双验证通过、未授权UNSAT=`unknown`、授权
  UNSAT=`unsat`。当前artifact摘要为
  `dfe1fff43ac2fc7b077a8e51bb7cdfb667e33bf25dce91e747d096093a0a830f`，
  跨运行语义摘要为
  `78e66d0aceb2038d54fe3b88348070c28bb3eea7f04a193d390d3179f34cb856`；
  5项新测试覆盖精确operator surface、配置约束、真实Bitwuzla、内层篡改后重哈希
  拒绝、语义重放和CLI离线验签；QF_BV两文件共16项专项测试通过。安装脚本另在
  `/tmp` clean prefix完成222步编译、安装、精确版本与相同binary SHA复核。最终
  439项Python、116项LLVM lit、Z3/cvc5各6/6 String/BV concrete conformance、
  cvc5/Bitwuzla QF_BV artifact及semantic replay、双family research evidence、
  原生build、Ruff、源码`py_compile`、依赖闭包、三层diff和文档链接门禁通过；
- 学术定位与边界：该证据证明固定版本Bitwuzla能执行当前Query IR lowering合同，
  不是Bitwuzla比cvc5/Z3更快，也不证明UNSAT跨solver consensus。正式性能结论仍需
  equal-CPU、disjoint holdout query corpus，比较单solver、顺序/并行portfolio、
  F240/F241 schedule与F242 cancellation，并报告PAR-2、solver CPU、invalid model、
  disagreement及coverage/CPU-hour。当前one-shot conformance也不替代native
  incremental API、proof-producing UNSAT或跨worker clause/context transport。
  参考官方
  [0.9.1 release/tag](https://github.com/bitwuzla/bitwuzla/releases/tag/0.9.1)、
  [安装要求](https://bitwuzla.github.io/docs/install.html)和
  [CLI合同](https://bitwuzla.github.io/docs/binary.html)。当前等级 I/T/E。

## 240. F245：Sealed Paired QF_BV Holdout Campaign（2026-07-28）

- 研究问题：F239--F243已经有sequence learner、SMBO和结构特征，F244也证明了
  backend adapter语义，但项目仍没有一个能把真实Query IR固定分成不相交train/
  holdout、逐query等资源执行多个solver并独立复核配对统计的工具。直接从日志挑
  “有趣query”或比较不等query数/timeout会造成selection bias；只记wall time又会
  隐藏多进程/二次确认的CPU成本；
- Sealed corpus：新增`util/qf_bv_campaign.py`及
  `symcc-qfbv-corpus-split-v1`。输入支持单JSON、JSON list/`queries` bundle、
  JSONL或目录中的`*.query.json`，最多4096 query/256 MiB。每个完整envelope被
  嵌入artifact并绑定SHA-256；QueryStore重新ingest得到content-addressed query ID，
  再用`accept_unsat=true`预lower并绑定certificate摘要。不允许重复query ID。
  `SHA256(split_seed:query_id) mod N`决定train/holdout，行按query ID排序；verifier
  从零重建CAS、lowering与split，修改envelope、membership或摘要后重哈希仍被拒绝；
- Solver identity gate：campaign必须嵌入一个通过F244 verifier的conformance
  artifact。运行前对选中backend重新解析version、resolve绝对binary并计算文件
  SHA-256，必须和conformance完全相同；因此同名但升级/替换的solver不能静默进入
  实验。离线`--verify`只验证封存身份，不要求本机安装solver；`--replay`才重新执行
  当前binary并再次检查身份；
- 公平执行与测量：每个holdout query、backend和repetition恰有一次任务，统一
  one-shot、统一wall timeout；顺序按
  `SHA256(order_seed:repetition:query:backend)`稳定随机化，缓解固定热身/时间趋势
  偏差。runner串行执行任务，使用`getrusage(RUSAGE_CHILDREN)`差分测量solver及
  必要子进程CPU；这让Z3的status-only rerun也进入成本，而不是被隐藏。成功的
  PAR-2为`min(elapsed, timeout)`，unknown/error/timeout收取`2T`。Artifact明确把
  资源合同命名为equal query count/equal wall timeout/measured child CPU，不把它
  错写成事先强制相等CPU；
- 配对结果：每backend聚合SAT/UNSAT/unknown/error、模型验证、authorized UNSAT、
  timeout、wall、child CPU、PAR-2；每pair按完全相同的
  `(repetition,query_id)`计算both/left-only/right-only/neither solved、
  SAT/UNSAT disagreement及paired PAR-2差值。全体再报告any-backend oracle solved，
  用于量化solver互补性上限；不从这个oracle反馈选择策略；
- 独立验证与重放：`symcc-qfbv-holdout-campaign-v1`封存corpus、conformance、
  protocol、binary identity、逐任务result与aggregate。Verifier重建每个Query IR和
  `accept_unsat=true` lowering certificate，检查任务笛卡尔积/随机顺序/资源字段/
  PAR-2，再将每个SAT assignment patch回封存witness并执行QueryStore evaluator；
  UNSAT必须带authorization。最后从逐行结果重算全部backend/pair aggregate。
  `campaign_sha256`绑定完整时间观测；`semantic_sha256`排除model选择和timing，只绑定
  query/backend/repetition/status/trust/certificate，允许合法不同模型的跨运行重放；
- Z3 interoperability修复：三solver前置验证发现Z3 4.8.12在UNSAT后的标准
  `get-value`输出`model is not available`并以code 1退出，旧adapter因此错误返回
  error。`SmtLibQfbvSolver`现在只在首轮非零退出且已经解析为UNSAT时，用原timeout
  剩余预算重跑移除`get-value`的status-only脚本；第二次正常退出且仍为UNSAT才交给
  原有capability gate，并记录`status-only-rerun-v1`。SAT、unknown、首轮parse错误
  或二次不一致仍fail closed；QueryStore result schema也只接受这个精确协议名；
- 功能证据：checked
  `benchmark/evidence/qfbv_holdout_f245_smoke.json`使用8个synthetic公式和
  `f245-smoke` hash split得到4 train/4 holdout；三solver共12次任务，holdout各为
  3 SAT/1 authorized UNSAT，全部模型复核通过、无disagreement/unknown/error，
  Z3记录一次status-only确认。Corpus/campaign/semantic摘要分别为
  `20e09efe13ee02b7e211219c02395611cb218eb4542d90d1466a02178f8d38f0`、
  `090e350e6e542f8dfe358e2595b2c254e3701a41bd282a8fa4897febe47c6559`、
  `f79661bf70a540dcf895d3dad148393fefbbf78e7a5e773f3358db8e5ba0cfb9`，
  semantic replay一致。5项测试覆盖split确定性/篡改、confirmatory阈值、真实三
  solver、无效模型内层篡改后重哈希拒绝及semantic replay。最终444项Python、
  117项LLVM lit、Z3/cvc5各6/6 String/BV concrete conformance、三solver F244
  conformance与F245 campaign真实重放、research evidence v2的8项obligation离线
  验签、QSYM原生build、Ruff、源码边界`py_compile`、依赖闭包、三层diff和权威
  文档链接门禁通过；
- 科研边界：synthetic smoke只有一次、4个holdout，时间数字只证明计量管线，不能
  排名solver。`--confirmatory`拒绝synthetic source、少于20 repetitions或没有
  disjoint train split；但source label仍由实验者提供，正式研究必须同时封存实际
  QSYM spool/campaign provenance，并报告不同随机seed/target。当前runner比较
  individual solver attempts；F246已补上F240/F241 sequence及F242 parallel
  cancellation arm，但仍没有真实公开corpus或coverage反馈。当前等级I/T/E，尚无
  R级性能或coverage声明。

## 241. F246：Leakage-Checked Solver-Strategy Holdout Campaign（2026-07-28）

- 研究问题：F245只比较单个solver，无法回答F240 beam、F241 EI/SMBO和F242
  cancellation在同一query/资源合同下是否优于单solver。更严重的是，如果policy
  artifact来自holdout或只按文件名宣称“train”，即使执行完全配对也会产生训练泄漏；
  并行portfolio若只记录winner，还会遗漏被取消或在grace内完成的内部结果；
- Train-only policy proof：新增`util/qf_bv_strategy_campaign.py`和
  `symcc-qfbv-policy-training-bundle-v1`。Bundle嵌入全部规范化
  `TrajectoryEvent`，每条必须携带属于F245 train split的content-addressed
  `query_id`；任一holdout ID立即拒绝。Verifier从事件重新运行F240
  `build_ensemble_sequence_policy`并要求artifact逐字段相等，再从该F240 artifact
  重新运行F241 `build_smbo_schedule_policy`。因此只修改trajectory、模型、cluster、
  schedule、EI trace或配置并重算外层SHA仍无法通过。F240/F241 modeled action必须
  完全等于F244已认证且本次选中的solver集合；
- Query-structural decision：每个holdout Query IR独立重算F243 context：witness
  长度、read offset、node/max-width及comparison/nonlinear/bitwise/structural
  operator组；再按policy封存的v1/v2 feature schema生成向量、选择最近cluster和
  stage schedule。Artifact记录feature vector、cluster和完整计划，离线verifier
  重算三者，不能用运行后状态改写选择；
- 六臂执行：同一稳定随机task manifest包含Z3/cvc5/Bitwuzla三个individual baseline、
  F240 beam、F241 SMBO及F242 parallel-cancellation。Sequence把artifact的秒级
  budget精确转换为毫秒，并要求stage budget之和不超过共同`T`；逐stage执行到首个
  SAT/UNSAT。Parallel臂同时启动全部backend，只有SAT开启grace/cancel，UNSAT等待
  完整收集。`RecordingSolver`在复用生产`PortfolioSolver`取消逻辑的同时保存每个
  已执行attempt的完整model/capability/lowering结果，未启动而取消的attempt也保留
  显式占位；
- 资源与统计合同：每个strategy获得相同logical query数和声明的shared wall budget，
  但并行臂可能消耗多倍CPU，故protocol明确写成
  `parallel-cpu-not-pre-equalized`。每行测量全策略wall和
  `RUSAGE_CHILDREN` user+system CPU，unknown/error按PAR-2 `2T`收费。Aggregate按
  strategy报告attempt、cancel request、实际取消、incomplete consensus、SAT/UNSAT、
  model/trust、wall/CPU/PAR-2，并对所有策略对重算solved四象限、结果分歧和paired
  PAR-2差；
- 独立验证与重放：`symcc-qfbv-strategy-campaign-v1`重新绑定F244 binary identity、
  F245 corpus、policy bundle、策略定义、task顺序、attempt和aggregate。Verifier对
  每个非取消attempt重建同一QF_BV lowering certificate；每个内部及winner SAT model
  都patch回封存witness执行QueryStore evaluator，UNSAT必须授权。它还复核
  winner/outer assignment、sequence prefix终止、parallel disagreement/cancel/
  consensus关系、timeout标志与PAR-2。完整摘要绑定观测时间；semantic摘要保留外层
  status/trust、确定性schedule与已观察终态集合，但排除合法model选择、winner竞态和
  timing，使并发重放可比较语义而非线程时序；
- 功能证据：checked
  `benchmark/evidence/qfbv_strategy_f246_smoke.json`复用F245的8-query 4/4 split，
  从train-only synthetic轨迹构建三backend F240/F241 policy，在4个holdout上执行
  6臂共24任务。各臂均3 SAT/1 authorized UNSAT、4/4 solved；F240/F241均实际选择
  两阶段计划但首阶段即求解。`grace=0`并行臂在3个SAT query发出取消请求并共取消
  6个attempt，UNSAT query收齐三后端。Bundle/beam/SMBO/campaign/semantic摘要为
  `3e62cd759adb4c023c91d8b23666cfd5728bb6b1446fc788dd9c0cdfc3d90180`、
  `5c166880747d56a320399050149981fa1611336e08e5763854497c27d13b5a2c`、
  `ade0fb8ea5e69c803c0597f7be38dac67b5c190898322965dc2ff9b4bd79e784`、
  `14aeecf6d18c4e1bbd6431f38ca1c0090fa07809edd5479ee6a453dbe76f4dcf`、
  `52cc64748dac7e9f381517539940fee7b09cfc69e9750aa803f622c72dd3c254`，
  真实重放semantic一致。5项新测试覆盖train泄漏、trajectory-policy精确重建、
  六臂资源/计划、重哈希schedule/winner/model篡改和并发语义重放；负CPU、证书、
  cancel count与policy timeout的额外变异均被拒绝。最终449项Python、118项LLVM
  lit、Z3/cvc5各6/6 String/BV concrete conformance、F244/F245/F246三份checked
  artifact离线验签、F246真实semantic replay、research evidence v2的8项
  obligation验签、QSYM原生build、Ruff、受控源码`py_compile`、依赖闭包、三层
  diff和权威文档链接门禁通过；
- 科研边界：该artifact的训练成功/失败标签是人为构造，holdout也只有4个微型公式，
  因而任何wall、CPU或PAR-2排序都无统计含义。`--confirmatory`拒绝synthetic
  corpus/policy和少于20 repetitions，但`source_kind`仍不是外部透明日志；正式实验
  必须从冻结QSYM spool及训练期solver trajectory生成bundle，并封存target、seed、
  binary、CPU affinity和source commit。F246保证共同声明wall budget并测量实际CPU，
  不保证并行臂预先等CPU；论文结论必须按CPU-hour归一化或用外层CPU配额重复实验。
  下一步是把query/result与seed replay的AFL edge/data coverage连接，完成F243
  v1/v2 calibration和coverage/CPU-hour分析；之后才根据真实prefix hit/RSS/cancel
  结果决定native context/clause transport。当前等级I/T/E。

## 242. F247：Sealed AFL Edge/Data Coverage Join 与 Feature Calibration（2026-07-28）

- 研究问题：F246已经在同一holdout上执行六种solver strategy，但其`coverage_delta`
  仍来自训练trajectory中的标签，不能证明solver生成的具体input实际增加AFL
  coverage，也不能校准F243结构特征。直接合并不同`afl-showmap`进程的bitmap还存在
  隐蔽错误：AFL++ PCGUARD按`__afl_final_loc`动态协商有效map，而旧data preload把
  内部`__AFL_MAP_SIZE`分配上限误当成有效范围；data byte可能写入共享内存却不被
  streaming/fuzzer扫描。PIE下用绝对return address作site ID也会随ASLR漂移；
- 原生map正确性修复：`util/afl_data_coverage_rt.c`现在用
  `dladdr(module basename,module-relative offset)`产生跨ASLR稳定site identity，
  并以thread-local 64-entry cache和递归guard约束hook开销。Preload constructor在
  PCGUARD目标自身constructor/forkserver启动前把`__afl_final_loc`低64 KiB保留为
  data namespace，后续edge guard ID从其后分配；data hash严格限制在该namespace，
  不再使用内部`__AFL_MAP_SIZE`。`SYMCC_AFL_DATA_MAP_SIZE`只可把hash范围收窄到
  1--65536，不能越过已预留区。边基线和combined oracle都加载同一preload以保持
  guard布局相同，仅通过`AFL_DATA_COVERAGE=0/1`关闭或开启写入；因此
  `combined-edge`才是可比较的data feature集合；
- Sealed target与真实采集：新增`util/qf_bv_coverage_join.py`及
  `symcc-afl-coverage-target-v1`。Target artifact要求命令恰有一个`@@`，绑定目标、
  `afl-showmap`和preload的绝对路径、文件SHA-256、showmap版本、timeout与paired
  preload协议。采集器实现AFL++ `-S -e` length-value streaming协议，复用持久
  forkserver；`map_repetitions`范围为1--100，confirmatory至少为2，多次观测时要求
  status和sparse bitmap完全稳定。Verifier还要求两种oracle status相同、edge集合
  为combined子集，且新增data ID只位于低64 KiB namespace；
- Query/seed/coverage join：工具从已验签F246 artifact重新materialize每个SAT
  assignment，并把candidate及每个原始witness按content SHA-256去重。每条六臂结果
  绑定query、strategy、repetition、solver status、witness/candidate hash、child
  CPU和PAR-2，再计算candidate相对其原witness的edge、combined和纯data新增ID。
  Strategy aggregate报告baseline/candidate union、gain、唯一candidate数、
  coverage/solver-child-CPU-second；所有strategy pair报告edge/data/combined
  shared、left/right-only与Jaccard，避免只比较最终总数；
- F243 v1/v2校准：从F246嵌入的train-only trajectory按backend训练确定性L2
  logistic模型，标签为训练期`coverage_delta>0`；只在individual-backend holdout
  行上评估，真实标签为replay得到的`edge_new_features>0`。Artifact同时封存模型、
  每行概率/标签以及Brier、log loss和5-bin ECE。Verifier从训练事件、holdout
  Query IR和coverage row完整重训/重算两个schema，策略arm不会被混入校准；
- 独立验证与重放：`symcc-qfbv-coverage-join-v1`封存完整F246 source artifact、
  target identity、协议、五个唯一input的双map观测、24条join、六臂aggregate及
  calibration。完整摘要绑定实测时间；semantic摘要排除采集成本和时间戳，但绑定
  sealed strategy语义、target、输入、status、bitmap、join和校准。离线`--verify`
  不启动目标，却会重建candidate、novelty、aggregate和模型；`--replay`先检查当前
  三个binary hash，再重跑目标并要求semantic摘要相等。Confirmatory coverage join
  必须继承F246 confirmatory source且至少双重map replay；
- 功能证据：`benchmark/qfbv_coverage_smoke.c`是匹配F245单字节Query IR的
  AFL-instrumented微型target，其中`0xbd`同时触发一个额外matched-prefix data
  feature。Checked
  `benchmark/evidence/qfbv_coverage_f247_smoke.json`从F246的24条结果得到5个唯一
  input和24条join；原witness union为6 edge/1 data。Bitwuzla、F240、F241和F242
  各新增4 edge，cvc5新增5 edge，Z3新增6 edge和1 data；相应solver child CPU均来自
  F246，不把showmap采集CPU混入solver分母。v1/v2 holdout各12行、6个正例，
  Brier分别为`0.280713505408219`和`0.281005485219151`。Campaign/semantic/target
  摘要分别为
  `03403e01f9d735506b921733c339ad43dcf9bc00d481450caeda6f1b9de68be6`、
  `703c2fa844978e67be65c7e93a757f7c79a2b49996fbb9024b8c04680911e166`、
  `c767fd1715f92c67b00a0f5de2f85198e466d83283685c80b11077f833995acd`，
  真实replay semantic一致。4项F247测试覆盖真实paired join、内层重哈希coverage/
  namespace篡改、binary漂移、confirmatory拒绝和semantic replay；data runtime的
  4项测试另覆盖exported map、SysV fallback、8进程PIE稳定性和真实streaming map。
  全量回归为455项Python、119项LLVM lit，全部通过；
- 科研边界：smoke只有4个holdout、一个原witness和人为训练标签，Brier/ECE及六臂
  coverage/CPU数值只证明管线，不支持策略优劣。正式R级结论仍须在多个公开target上
  冻结真实QSYM spool、train trajectory、target binary和seed provenance，至少20次
  paired randomized repetitions，并使用外层CPU quota或CPU-hour归一化；还要报告
  map collision、目标崩溃/timeout、coverage AUC、grace敏感性及置信区间。F247没有
  实现跨worker native solver context/clause transport，也不提供proof-producing
  UNSAT。当前等级I/T/E。

## 243. F248：Profile-Guided Hydra Control-Flow Melding 与 Original Replay（2026-07-29）

- 研究问题：F187 的 `targeted_transform` 仍只是输入字节变换，不能移除实际的昂贵
  symbolic branch。直接把 diamond 改为 `select` 还有两个跨层风险：一是 SymCC
  原本会把每个普通 `select` 再记录为路径选择，等价于重新引入被移除的 fork；二是
  Hydra 的激进 memory linearization 按论文定义只保证 failure-preserving，不保证
  semantics-preserving，变换程序独有的越界 load 等 failure 绝不能进入最终结果。
  本实现依据 OOPSLA 2026
  [Taming the Hydra](https://doi.org/10.1145/3798202) 的 complete alignment、
  extra instruction、read-back store 和 original replay 边界，在编译器与实验层建立
  可归因、可否决的完整闭环；
- Profile-guided single-site build：新增 `compiler/HydraTransformation.{h,cpp}`，
  在 live-continuation export 与常规 symbolization 之前运行独立 module pass。
  `util/hydra_transform.py profile`从 runtime `branch_trace`聚合稳定 branch site，
  把每次 execution 的 solver CPU 按完整 trace 等分归因，使用
  `observations + 4*interesting + 2*log1p(solver_us/1000)`排序。Compiler 接受该
  文本 profile、显式 site 或 denylist，每个 module 只选择最高分 eligible site；
  replay 进一步要求整个 manifest 恰有一条 record，避免多 TU/多站点构建造成
  spurious failure 归因歧义；
- CFG 与 alignment：只接受 single-entry、two-arm、single-exit 的严格 diamond，
  两臂必须唯一前驱、共同 merge 且 merge 恰有两个前驱；block address、escaping
  SSA、call/invoke、atomic/volatile 和未知 instruction fail closed。Pass 对两臂
  instruction sequence 求兼容 opcode/type LCS，把匹配 ALU 的相异 operand 变为
  条件 `select`；未匹配 ALU 使用 operation-aware extra operand 完成 alignment，
  integer div/rem 的额外 divisor 固定为 1，其他 first-class operand使用零值或
  原 pointer。原 LLVM opcode flags、poison/undef/freeze 语义不被静默清除；
- Failure-preserving memory mode：`SYMCC_HYDRA_MODE=aggressive`允许 simple
  load/store。相同地址 load 合并一次，不同地址 load 线性执行后选择结果；未匹配
  store 先从同一地址读取旧值，再执行
  `store(select(condition, original, old), address)`，因此未选路径只写回刚读取的
  值。相同地址的成对 store 选择原值后只写一次。该模式可能无条件执行原先受 guard
  保护的 load 并引入 transformed-only failure，manifest 因而强制
  `requires_original_replay=true`；safe 模式只接受 ALU/compare/cast/select/GEP/
  freeze，不接受 memory；
- SymCC fork-elision：所有由 melding 新增的 operand/output select 带
  `!symcc.hydra_select`。`Symbolizer::visitSelectInst`对这些节点只构建 ITE data
  expression，不调用 `_sym_push_path_constraint`；普通源程序 select 仍保持原行为。
  这使目标 CFG branch 及其生成的 operand selects 都不会重新形成 solver fork，
  同时后续 target 可以继续读取完整 ITE，复用现有 Backsolver/Query IR；
- Sealed original replay：`util/hydra_transform.py campaign`绑定 original/
  transformed executable SHA-256、单站点 compiler manifest、timeout 和嵌入式
  content-addressed input。每个候选始终先运行 transformed、再运行 original，
  分类为 `real-failure`、`spurious-transformed-failure`、
  `failure-preservation-violation`、`original-validated` 或 timeout。
  `accepted_failure_inputs`只能来自前两程序都失败的交集；transformed-only failure
  永不保留并把 site 写入下一轮 `SYMCC_HYDRA_DENYLIST`；若原程序失败而变换程序
  不失败，artifact 显式令 `failure_preservation_holds=false`，CLI 以 code 2
  fail closed。Offline verifier重算嵌入 input、分类、accepted set、denylist、
  semantic/full digest；replay重新执行两二进制并比较 semantic摘要；
- 功能与原生证据：`test/hydra_transform.ll`覆盖 safe LCS/extra ALU、profile选点、
  aggressive load/store readback、LLVM verifier、manifest 与 denylist不变换路径；
  `test/test_hydra_transform.py`覆盖 profile成本归因/篡改、真实/伪阳性/违例三种
  replay关系、accepted set、denylist、artifact篡改及semantic replay。
  `benchmark/hydra_replay_smoke.ll`构造一个受 branch 保护的无效 load：checked
  `benchmark/evidence/hydra_f248_smoke.json`中输入`A`只在 transformed程序失败，
  输入`B`在两程序都失败，得到1个real、1个spurious、denylist site `424248`，
  `failure_preservation_holds=true`。Campaign/semantic摘要分别为
  `650c350c6abc32fe07301741eaafe39586ddb3c04f16c0e12b8d5d1c9757206f`和
  `b9ed9cd1c835a8bdf5a4dc450e272359fbbed1e6b340a3a64486f2b9e18326b7`，
  native semantic replay一致；
- 科研边界：该 smoke 有意制造一个 invalid pointer，只证明 false-positive
  detector 和 failure-preservation oracle，不能证明 coverage/CPU 改善。当前 pass
  只实现论文的严格单块 diamond 子集，不支持 multi-block isomorphic region、
  multi-exit、loop、call/exception edge、atomic/volatile、alias-sensitive store
  reordering或动态 symbolic-address profitability proof；store readback 的中性结论
  限于无 data-race 的顺序语义。正式结论必须从真实 target profile 选择站点，报告
  fork/solver CPU、select/ITE规模、coverage AUC、spurious迭代次数和 denylist
  收敛，并对 safe/aggressive/original 做相同总 CPU 的至少20次配对实验。当前等级
  I/T/E，不声称完整复刻 Hydra 或通用 IFSS。

## 244. F249：Bounded Multi-Arm IFSS Region-State Worklist（2026-07-29）

- 研究问题：此前 compiler Backsolver 只能把二臂 PHI 和递归嵌套的二臂 PHI
  恢复为 ITE；一个由多层条件分支汇入同一 PHI 的三臂以上区域会丢失 implicit
  control dependence，因而目标比较只能看见当前 concrete PHI 值。F249在
  `compiler/Symbolizer.{h,cpp}`中加入独立的多臂 IFSS 子集，不改变原程序控制流，
  而是在 merge 点重建完整的符号 region state；
- Region discovery 与预算：候选 PHI 必须有3--8个唯一 incoming block，类型为
  integer/floating/pointer。实现从全部 incoming predecessor 求最近共同 dominator，
  要求 merge postdominate controller，且 root 是具有符号条件的二路 branch。
  两个入口 successor 都须通过现有 easy-region 校验：单出口、无环、最多32个
  basic block。显式 DFS worklist 最多枚举64条路径；只接受 branch-only corridor，
  拒绝 switch、loop、未知 terminator，以及 conditional edge 直接进入 PHI block
  的 edge-sensitive 歧义；
- Path relevance 与状态合并：worklist保存每一步
  `(BranchInst, takeTrue)`，并按最终 PHI predecessor 分组；发现的 predecessor
  集合必须与 PHI incoming 集合精确相等。每条路径把条件或其否定做 conjunction，
  同一 predecessor 的多条路径做 disjunction。incoming value复用有界
  `RegionValue` symbol substitution，可接受支配值、常量、受支持的无副作用 SSA、
  nested ITE，以及由 MemorySSA/AA 证明没有干扰写的 read-only load snapshot。
  最后从后向前构造 ITE worklist：
  `state_i = ite(relevance_i, value_i, state_{i+1})`，并用最终表达式替换原
  symbolic PHI；
- 完备默认臂与原子失败：路径枚举已证明 incoming 集合完备，所以最后一臂作为
  default state，不生成未被最终 ITE 使用的 predicate。早期版本为该默认臂留下
  `_sym_build_equal(expr, null)`孤立调用，原生 QSYM 在表达式表查询时抛出
  `std::out_of_range`；F249据 GDB 栈和生成 IR 定位后消除了该调用。更一般地，
  late synthesis failure现在从 merge 的原始 insertion point回滚全部临时 IR，
  确保不支持的 call/memory/shape只触发保守 fallback，不污染随后 instrumentation；
- 验证与结果：`test/backsolver_multiarm_ifss.ll`同时检查 NOT/AND/ITE IR，并运行
  原生QSYM Backsolver。输入`AX`使当前路径选择值10，而目标值20要求第一字节不再
  等于`A`且第二字节仍为`X`；实测
  `targets/attempts/sat=1/1/1`、direct `attempts/sat=4/1`，生成候选
  `00 58`满足该跨臂约束。`test/backsolver_multiarm_ifss_reject.ll`令最后一臂
  依赖side-effecting opaque call，证明在已合成前两臂之后的 late rejection仍经
  LLVM verifier，且输出中没有`ifss.*`、`_sym_build_ite`或
  `_sym_build_bool_xor`残留。两项定向lit均通过；
- 科研边界：这是显式 region-state/worklist、relevance predicate 和 symbol
  substitution 的 I/T 子集，不是任意 IFSS。它仍要求有界、无环、branch-only、
  single-exit region；不处理 switch edge、loop-carried state、multi-exit
  exit selector、side-effecting call、异常边、volatile/atomic、ambiguous alias
  或 general memory state。当前测试证明机制和一个 SAT witness，不证明公开
  target上的 coverage/CPU收益；正式R级证据仍需冻结真实站点与输入，在等总CPU下
  对 two-arm baseline、multi-arm IFSS 和关闭 IFSS 做至少20次配对实验，并报告
  ITE规模、候选有效率、coverage AUC、solver CPU和拒绝原因分布。当前等级I/T。

## 245. F250：MemorySSA/AA-Proven IFSS Memory-State Merge（2026-07-29）

- 研究问题：常规shadow memory只保留实际执行的store expression。即使两个分支
  都向同一地址写值，merge后的load也只能看见taken arm，F249的数据PHI worklist
  无法恢复这种经memory dependence传播的implicit flow。F250在
  `compiler/Symbolizer.{h,cpp}`中记录候选simple load，并在数据PHI完成后、统一
  short-circuit lowering之前恢复严格的双臂memory state；
- MemorySSA与AA前提：load必须是non-atomic/non-volatile supported scalar，且其
  直接defining access是load所在merge block的二输入`MemoryPhi`。每个incoming
  access必须是对应predecessor内最后一个simple `StoreInst`，store block以
  unconditional edge直接进入merge，stored type与load type完全相同。对
  `MemoryLocation::get(load/store)`的两个AA查询都必须得到`MustAlias`；NoAlias、
  PartialAlias和MayAlias一律拒绝；
- Region effect proof：两个store predecessor的最近共同dominator必须是symbolic
  conditional branch，merge必须postdominate controller，两个successor形成最多
  32 blocks的有界无环region。实现收集region内全部原始instruction；除两个候选
  store外，任何可能写内存的instruction都必须有MemorySSA access，且
  `AAResults::getModRefInfo(instruction, loadLocation)`不得包含Mod。这样不确定call、
  escaping alias、额外同址写、volatile/atomic均fail closed；
- State reconstruction：dominator关系唯一映射true/false store。两个stored value
  使用现有`RegionValue` substitution在load前合成，再建立
  `_sym_build_ite(controller, trueValue, falseValue)`；原`_sym_read_memory`
  expression仍可无副作用执行，但其所有下游use被新的memory-state ITE替换。
  生成call命名为`ifss.memory.state`，并附
  `!symcc.ifss_memory = !{!"must-alias-memoryssa-v1", load-site,
  controller-site, true-store-site, false-store-site}`，绑定稳定site形成可审计的
  编译期结构证书。该metadata不是独立solver proof，可信边界仍是LLVM
  MemorySSA/AA及pass verifier；
- 原子回滚与definedness：候选load在原有shadow-read之后记录rollback anchor；
  stored value的late synthesis若失败，删除anchor与load之间的全部新IR。共享
  `trySynthesizeRegionValue`入口同时改为递归拒绝`undef`和`poison`，避免把未定义
  concrete value包装成看似有效的QSYM constant；`freeze undef/poison`目前也保守
  fallback，不声称完整LLVM definedness modeling；
- 验证与结果：`test/backsolver_memory_state.ll`从输入`A`执行store 10，而target
  要求merge load为20；生成IR含绑定四个site的proof metadata，原生QSYM得到
  Backsolver `targets/attempts/sat=1/1/1`、direct `attempts/sat=2/1`，候选`00`
  成功选择未执行arm。`test/backsolver_memory_state_reject.ll`覆盖不同alloca的
  NoAlias store、已合成一臂后遇到readnone opaque return的late rollback，以及
  poison store；三者均经LLVM verifier且没有IFSS memory metadata/call残留。连同
  F249 multi-arm、nested Veritesting和read-only snapshot共6项定向lit全部通过；
- 科研边界：F250只处理双臂、single-exit、merge-local load和每臂最后一个直接
  MustAlias store；不沿MemorySSA def chain越过NoAlias defs，不合并3--8臂memory
  phi，不支持部分重叠/不同宽度、多个location、load/store排序、multi-exit、
  loop、异常、atomic或并发memory model。正向smoke只证明alternative memory state
  可到达，不证明coverage/CPU收益。R级评估需在公开target报告MemoryPhi候选/
  MustAlias接受/ModRef拒绝数量、ITE规模、Backsolver有效率、solver CPU和coverage
  AUC，并与shadow-memory baseline做至少20次等CPU配对。当前等级I/T。

## 246. F251：Bounded NoMod MemorySSA Def-Chain Recovery（2026-07-29）

- 研究问题：F250只查看MemoryPhi的直接incoming definition；若目标MustAlias store
  之后还有一个完全无关的store，MemorySSA仍会把后者作为incoming，导致明明可证明
  的memory state被拒绝。F251把每个incoming扩展为有界backward def-chain，同时
  不把MayAlias猜测成NoAlias；
- 追链算法：从每个MemoryPhi incoming开始，最多跳过8个`MemoryDef`。遇到simple、
  等宽且对load location为`MustAlias`的store即停止；候选store仍必须位于对应
  incoming predecessor，且该block直接无条件进入merge。其他definition只有在
  `AAResults::getModRefInfo(instruction, loadLocation)`不含Mod时才能跳过，顺序按
  nearest-to-farthest记录。遇到MemoryPhi/liveOnEntry、MayAlias/PartialAlias写、
  target上的不兼容store、第9个可跳过definition或未知Mod立即拒绝；
- 可观察内存边界：即使definition对目标location为NoMod，volatile store、
  atomic RMW、atomic cmpxchg和fence也不进入本子集，避免把并发/可观察顺序语义
  降格为普通NoAlias。Region effect scan仍会对全部其他原始写执行独立ModRef检查，
  所以chain proof与region proof是两层保守门禁；
- Chain certificate：没有跳过项时继续产生F250
  `must-alias-memoryssa-v1`。存在跳过项时改为
  `must-alias-memoryssa-chain-v1`，前四个site字段仍是
  load/controller/true-store/false-store，随后编码
  `true-skip-count, true-skipped-sites..., false-skip-count,
  false-skipped-sites...`。计数为i32，site为稳定target-width整数；它使接受路径和
  bounded proof chain可由生成IR审计，但仍不是独立重跑AA的proof object；
- 验证与结果：`test/backsolver_memory_def_chain.ll`在两个目标store之后各放一个
  独立alloca store，IR metadata准确给出`1 + 1`个跳过site；原生QSYM从`A`生成
  `00`，Backsolver `targets/attempts/sat=1/1/1`、direct
  `attempts/sat=2/1`。扩展的`test/backsolver_memory_state_reject.ll`证明两个
  pointer argument之间的MayAlias intervening store、第9个NoAlias definition和
  volatile NoAlias store均无chain metadata/IFSS memory call并通过LLVM verifier。
  F250完成时全量门禁为459/459 Python、125/125 lit；
- 科研边界：F251仍不跨nested MemoryPhi，不把store移出incoming predecessor，
  不处理multi-arm、partial overlap、不同width/endian、多个memory location、
  call summary proof、loop或multi-exit。固定8不是性能最优结论；公开target实验应
  报告chain length分布、0--8预算敏感性、AA rejection taxonomy、额外compile time、
  ITE规模、solver CPU和coverage收益，再决定是否引入MemorySSA walker缓存或
  proof-carrying ModRef artifact。当前等级I/T。

## 247. F252：Shared Multi-Arm IFSS Data/Memory Partition（2026-07-29）

- 研究问题：F249的数据PHI和F250/F251的MemoryPhi已经有相同的controller、
  postdom、path和endpoint声音性前提，但此前分别实现；继续直接复制会使block/path
  预算、conditional-edge拒绝和终点完备性逐渐分叉。F252在
  `compiler/Symbolizer.cpp`抽出`buildIFSSRegionPartition`，让data state与memory
  state消费同一份控制区域证明；
- 共享partition：输入为2--8个endpoint和一个merge。算法从全部endpoint求最近共同
  dominator，要求merge postdominate controller且controller是conditional branch；
  两个root successor的唯一basic block并集总计不超过32，而不是此前分别32；
  DFS全局最多64 paths，拒绝cycle、非branch terminator和conditional direct-to-merge
  edge。输出按predecessor分组的`(branch,takeTrue)`路径；endpoint必须唯一，枚举
  key集合必须与输入集合精确相等。F249 multi-arm data PHI改为直接使用该partition，
  保持既有行为；
- Multi-arm memory state：F250方法从固定二输入推广为2--8输入MemoryPhi。每个arm
  仍须由F251有界NoMod chain找到位于对应direct merge predecessor的simple、
  等宽MustAlias store。除这些store外的region写继续逐项通过MemorySSA/ModRef。
  对前`arms-1`个endpoint，路径literal做AND、同endpoint多路径做OR；最后arm是
  完备partition证明下的default，不生成孤立predicate。各stored value经
  `RegionValue`替换后从后向前形成`arms-1`层memory ITE；
- Metadata兼容与扩展：可直接映射root true/false的二臂case继续产生F250/F251
  `must-alias-memoryssa[-chain]-v1`，已有工件和测试不变。其余case产生
  `must-alias-memoryssa-multi-v1`：
  `load-site, controller-site, arm-count`之后，每臂编码
  `incoming-block-site, store-site, path-count, skip-count,
  skipped-definition-sites...`。最终ITE统一命名`ifss.memory.state`并携带该proof；
  任意arm晚失败仍通过load anchor原子回滚全部临时predicate/value/ITE；
- 验证与结果：`test/backsolver_multiarm_memory_state.ll`构造三臂nested branch，
  每臂先向同一slot写10/20/30，再写一个NoAlias噪声slot，使机制必须同时完成3-arm
  path partition、3条one-hop def chain和2层memory ITE。IR metadata报告3 arms，
  各`path-count=1, skip-count=1`；输入`AX`实际取10，而target要求20，原生QSYM
  得到Backsolver `targets/attempts/sat=1/1/1`、direct
  `attempts/sat=4/1`和候选`00 58`。Reject suite新增三臂中一个pointer-argument
  MayAlias store，证明整个merge拒绝且无multi metadata。F249--F252共6项定向lit
  全部通过；
- 科研边界：这是共享single-exit partition与multi-arm scalar memory state，不是
  general memory SSA interpreter。Store仍须位于direct merge predecessor；不处理
  nested MemoryPhi、同arm多个target store、partial overlap、不同width、多个location
  的联合state、switch、loop、exception、multi-exit或concurrency。Path predicate
  目前可能重复合成公共subexpression，虽无孤立调用但会放大ITE DAG；下一阶段应加
  per-partition condition cache/ownership，再以此作为exit-selector的control state。
  正式评估需报告arm/path/chain联合分布、expression DAG增量、compile/solver CPU、
  candidate validity和coverage AUC。当前等级I/T。

## 248. F253：Partition-Scoped Condition DAG Cache 与 Ownership（2026-07-29）

- 研究问题：F252共享了path partition，但每个non-default endpoint仍独立调用
  `trySynthesizeRegionValue`。四臂binary region中，前两个leaf共享同一内部branch
  condition，旧实现会复制concrete clone和QSYM expression，放大DAG；直接复用
  `RegionValue.computation`又会让同一instruction range被多次merge，违反
  `SymbolicComputation::merge`的时间顺序前提；
- 去重与确定性：data PHI和MemoryPhi在构造predicate前，按arm顺序、path顺序和
  branch-step顺序预扫描前`N-1`个endpoint，以原始LLVM condition `Value*`为key去重。
  每个唯一condition只合成一次，缓存保留`concreteValue/expressionValue`，清空
  `RegionValue.computation` ownership；后续literal、AND/OR和ITE只消费该缓存，
  不再次clone condition。默认arm仍不生成predicate；
- 两阶段ownership：首版把多个缓存condition合并为一个prefix computation，再与
  state computation合并。真实四臂IR证明这仍不正确：short-circuit只为整个range的
  最后结果建立出口PHI，较早condition和其concrete clone被困在slow path，产生
  “instruction does not dominate all uses”，原生QSYM最终在
  `_sym_build_bool_and`触发`map::at`。最终实现让**每个唯一condition各自成为一个
  独立`SymbolicComputation`**，成功构造整个partition后按定义顺序登记，再登记
  predicate/state computation。Short-circuit因而为每个缓存expression分别建立
  fast-null/slow-expression出口PHI，后续use均被其支配；
- 事务与输入边界：condition computations直到整个region成功前都不进入
  `expressionUses`，所以late rejection仍可安全删除全部IR而不留下dangling记录。
  若一个新condition computation没有任何可作为short-circuit判据的input，当前
  保守回滚；不把无输入的memory-derived expression错误地塞入需要非空input的
  lowering。每个真正合成的缓存call附
  `!symcc.ifss_condition = !{!"partition-condition-cache-v1", condition-site}`，
  便于静态计数和site归因；
- 验证与结果：`test/backsolver_ifss_condition_cache.ll`包含一个四臂data PHI和
  一个四臂MemoryPhi。每个partition有root、left、right三个condition；root已在
  merge可用，left被两个前置leaf共享，right被第三leaf使用，因此总计恰有4个
  cache call/metadata，而不是按leaf重复产生6个。测试强制LLVM verifier并运行
  原生QSYM：输入`AXY`取memory值10，target要求20，得到Backsolver
  `targets/attempts/sat=1/1/1`、direct `attempts/sat=5/1`和候选`41 00 59`。
  F253完成后全量compiler/lit为128/128；
- 科研边界：cache作用域是一次data或memory merge，不跨同函数的多个消费者共享，
  也不对两个不同condition内部的公共SSA子图做value numbering；缓存只减少构造
  重复，不改变path partition或solver语义。进一步跨partition DAG hash-consing
  必须具有显式dominance、lifetime和fast-path ownership证明，并测量表达式节点、
  compile time、solver serialization CPU与coverage收益；不能仅以IR变短宣称性能
  改善。当前等级I/T。

## 249. F254：Bounded Multi-Return Exit-State Lowering（2026-07-29）

- 研究问题：F249--F253只在一个既有merge中恢复data/memory state，源程序的多个
  `ret`没有共同consumer，因而callee只会把实际出口的return expression传给caller。
  F254新增预符号化module pass `IFSSExitLowering`，把可证明封闭的多返回区域归并成
  一个`ifss.exit.state` PHI和synthetic dispatch return，使既有IFSS region-state
  能在callee构造返回ITE，并通过`_sym_set/_get_return_expression`跨调用传播；
- 结构证明：只接受2--8个integer/floating/pointer同类型返回出口。DominatorTree
  求全部return block的最近共同dominator，并要求其为两个不同successor的conditional
  branch；从controller显式枚举全部路径，region必须single-entry、branch-only、
  acyclic、无address-taken/EH block，最多32个非controller block和64条路径，且
  实际到达的return集合与函数全部return精确相等。Switch、indirect/invoke、
  loop/backedge、unreachable额外return、九出口和无法封闭的路径均不修改IR；
- 值与ABI边界：拒绝void/aggregate/vector/token返回、直接undef/poison和函数内任意
  `musttail` call，避免破坏musttail紧邻return的IR约束。全部return operand为同一
  `Value*`时视为无收益归并而跳过。这个pass只重定向正常return edge，不线性执行
  arm内副作用；return value能否形成symbolic ITE仍由F249/F253的RegionValue、
  ownership和late rollback证明决定；
- 分析时序：pass位于Hydra、live-continuation export和常规symbolization之前，因此
  AA/MemorySSA在CFG归并后重新计算。候选收集阶段不写IR；只有至少一个region接受时
  才先冻结全模块原始`!symcc.site_id`，再执行任何CFG rewrite，避免后续函数site因
  前序变换漂移，也保证无候选时准确返回`PreservedAnalyses::all()`；
- Proof metadata：每条原return edge成为带
  `!symcc.ifss_exit_arm = !{!"bounded-return-exit-state-v1", ordinal,
  original-return-site}`的无条件branch。Return-state PHI绑定controller site、
  arm/block/path count以及按ordinal排列的全部return site；统一return携带相同核心
  counts。该metadata是结构分析证书和实验分层依据，不是独立可重放的CFG proof；
- 验证与结果：`test/backsolver_multi_exit_return.ll`包含三返回callee。输入`AX`
  实际从第一出口返回10，caller要求20；归并后IR有3-arm PHI与2层ITE，LLVM verifier
  通过，原生QSYM产生保持第二字节`X`且翻转根出口的候选，Backsolver
  targets/attempts/SAT及direct attempt/SAT均非零。测试同时验证flag关闭时保留三个
  `ret`。`backsolver_multi_exit_return_reject.ll`覆盖switch、可达loop、aggregate、
  poison、九出口和musttail，均无exit metadata且verifier-clean；F254完成后全量
  compiler/lit为130/130；
- 科研边界：这是normal-return这一类真正multi-exit的语义保持归并，不是任意
  continuation selector。尚未覆盖break/continue到不同continuation、多个live-out
  tuple、跨出口memory location、exception/coroutine/indirect exit、loop-carried
  state或general control effect。公开评估需分别报告结构候选/接受、symbolic-state
  接受、arms/blocks/paths、ITE/DAG节点、compile/solver CPU、跨调用validated yield
  和coverage AUC，并以`SYMCC_IFSS_EXIT_STATE=0/1`等CPU消融。当前等级I/T。

## 250. F255：Bounded Switch-to-IFSS Chain Lowering（2026-07-29）

- 研究问题：F252共享partition只接受`BranchInst`，而LLVM `switch`的case/default
  edge不能直接表示为`IFSSPathStep(branch,takeTrue)`；另写switch专用data/memory
  state会复制路径、condition ownership和MemorySSA声音性逻辑。F255新增
  `IFSSSwitchLowering` module pass，在symbolization前把严格有界switch转换成
  确定性equality branch chain，使F252/F253以及F250--F252 memory proof原样复用；
- 接受边界：switch须有2--8个**唯一**successor，每个case/default目标只由controller
  进入、非address-taken且非EH pad。所有arm要么都以unconditional branch进入同一
  exact-predecessor merge，要么都直接normal return。共享case destination、外部
  predecessor、conditional arm、混合exit、九臂和不能封闭的merge均保持原switch；
- Lowering语义：按LLVM case迭代顺序生成`icmp eq`测试；case为true进入原目标，
  false进入下一test，最后false进入default。Arm内指令和到merge/return的edge不移动，
  destination PHI的controller incoming block精确改写为对应test block。全部候选在
  写IR前验证，所以不存在中途部分lowering；至少一个候选时才冻结全模块原始site，
  并在F254之前执行，因而direct-return switch可进一步形成return exit state；
- 证书：每个生成comparison和conditional branch携
  `!symcc.ifss_switch = !{!"bounded-switch-chain-v1", original-switch-site,
  case-ordinal, arm-count, !"case", case-value}`；最后branch另携
  `symcc.ifss_switch_default`及default ordinal。F252 multi-arm memory metadata继续
  记录最终arm/path/store chain，F253只为不支配merge的内部case condition建立独立
  `partition-condition-cache-v1` computation；
- 验证与结果：`test/backsolver_switch_ifss.ll`同时包含三臂data PHI、三臂
  MemoryPhi和direct-return switch。纯lowering及switch→F254组合均经LLVM verifier；
  data/memory partition各恰有一个内部condition cache。原生memory test输入`A`
  实际写10、target要求case `B`的20，QSYM产生首字节`B`的validated Backsolver
  candidate，Backsolver/direct attempt与SAT均非零。Flag-off保留原switch。
  `backsolver_switch_ifss_reject.ll`覆盖shared destination、external predecessor、
  conditional arm、九臂和mixed exit，无schema残留；
- 科研边界：这是strict fanout的语义保持lowering，不是general switch lowering或
  jump-table优化。当前不保留原`branch_weights`的概率分解，也不处理多个case共享
  successor、critical-edge PHI multiplicity、range compression、bit-test tree、
  indirectbr/callbr或loop switch。公开评估应报告switch候选/接受、arms/cases、
  chain depth、condition cache与ITE节点、compile/solver CPU、candidate validity和
  coverage AUC，并对linear/order、balanced/profile-guided chain做等CPU消融。当前
  等级I/T。

## 251. F256：Unsigned Range-Balanced Switch Tree（2026-07-29）

- 研究问题：F255 linear chain对`C`个case只生成`C`次equality，总IR小，但最后case
  与default路径深度为`C`；在IFSS中这会同步放大path literal、branch constraint和
  Backsolver controller距离。F256增加`SYMCC_IFSS_SWITCH_MODE=balanced`，保留
  `linear`默认兼容模式，并用无符号range tree降低最坏路径深度；
- Tree构造：case按同位宽APInt unsigned值排序。递归节点以左半最后一个case作为
  upper bound，生成`icmp ule selector, bound`；叶节点再做精确`icmp eq`，false
  进入default。对`C=7`，root bound为2，共6个range和7个equality node，任一case/
  default路径最多3次range加1次equality，而linear最坏为7次equality。总comparison
  从`C`增至`2C-1`，因此这是depth/IR-size trade-off而非无条件性能提升；
- Default多路径SSA：每个leaf的非case值都可进入default，所以default从一个edge
  扩展为`C`个gap predecessor。Lowering先保存default目标每个PHI的原controller
  incoming value，删除原incoming，再为每个leaf复制同一value；case目标仍只有一个
  leaf predecessor。该重写保持edge-specific PHI语义，并使F252把default的多条
  path做OR而不是伪造一个谓词；
- 证书与声音性：balanced comparison/branch使用
  `bounded-switch-tree-v1`，节点kind为`unsigned-upper`或`case`；每个leaf branch
  还带default metadata。F255的unique successor、exact merge、2--8 arms和
  fail-closed边界不变。最大7 cases产生14条case/default逻辑路径和20个
  non-controller/arm block，仍在F252的64-path/32-block预算内；
- 验证与结果：`test/backsolver_switch_balanced.ll`同一8-arm IR分别执行linear和
  balanced。静态检查确认linear为7 eq，balanced为6 ule+7 eq、root bound=2，
  default edge-PHI incoming从1精确扩展为7；两者均经LLVM verifier。Symbolized
  balanced tree产生12个非支配condition-cache computation和7层state ITE。原生
  输入`00`实际选case0/value10，target为case6/value70，QSYM得到首字节`06`的
  validated Backsolver candidate，direct与fallback telemetry均通过；
- 科研边界：当前balanced按case数而非profile概率或value-domain gap质量切分，
  不保存原switch branch weights，也未实现optimal alphabetic tree、Huffman/
  Hu-Tucker、range compression或online mode selection。正式实验应同时报告
  comparisons、max/mean weighted depth、default path count、IR/condition DAG、
  compile/solver CPU和coverage，并在uniform/skewed profile上比较linear、
  balanced与未来profile-aware策略。当前等级I/T。

## 252. F257：Profile-Weighted Optimal Alphabetic Switch Tree（2026-07-29）

- 研究问题：F256只优化最坏深度，真实程序中的switch case频率通常高度偏斜。
  F257增加`SYMCC_IFSS_SWITCH_MODE=profile`及按stable switch site索引的外部观测
  profile，在不改变case的unsigned有序关系、leaf精确匹配和default语义的前提下，
  最小化case观测分布上的期望range判定成本。该设计是有序决策树而非Huffman树：
  每个内部节点仍是可直接lower到LLVM的`selector <= upper-bound`；
- 输入与选择：`SYMCC_IFSS_SWITCH_PROFILE=<path>`的每个非注释行严格为
  `<site> <unsigned-decimal-case|default> <positive-weight>`。每个被选择站点必须
  恰好提供全部case和一个default，case文本规范化前导零；重复键只使对应site
  invalid，无法解析的全局行使文件malformed。所有region在任何CFG变换前一次性
  读取和选择profile，保证多switch模块中前一个lowering不能改变后一个site映射；
- 最优树算法：将unsigned排序后的case权重记为`w_i`。区间动态规划使用
  `D[l,r] = W[l,r] + min_k(D[l,k] + D[k,r])`，`D[i,i+1]=0`，并以最小split作为
  相同成本的确定性tie-break。最终证书objective为
  `D[0,C] + sum_i(w_i)`，后一项表示每个case固定执行一次leaf equality。
  `C<=7`时构建时间`O(C^3)`、空间`O(C^2)`；所有加法饱和到`uint64_t`，不会因
  profile计数溢出改变为低成本树；
- 证明与回退：profile树节点使用`bounded-switch-profile-tree-v1`；
  root comparison和branch另携`profile-weighted-switch-v1`，记录site、完整64位
  内容指纹、default权重、objective、case数及有序`(case,weight)`。缺少路径、
  文件不可读、语法损坏、site缺失、重复键、case集合不符或不完整时不做部分
  profile变换，而是生成F256 balanced tree，并以
  `profile-switch-fallback-v1`记录精确原因。原switch site在删除前固化到
  lowering result，证明附着阶段不读取悬空Instruction；
- 验证与结果：`test/backsolver_switch_profile.ll`固定site 257257并给case0权重
  1000、其余case权重1。七case optimal tree的root由balanced的upper bound 2变为
  0，仍保持6个range、7个leaf equality、LLVM verifier和default PHI语义；
  incomplete、unreadable及duplicate profile分别产生可检查的balanced fallback
  reason。Symbolized树仍生成7层state ITE，原生输入`00`可得到case6的`06`
  validated Backsolver candidate，证明profile优化未破坏跨arm可达性；
- 科研边界：单一default计数无法说明其值落在哪个case gap，因此default weight
  当前只写入证明，**不参与split objective**；否则会凭空假设gap分布。当前也未
  导入LLVM `branch_weights`、在线衰减/置信区间、value-gap压缩、bit-test或
  profile/tree独立manifest。正式实验必须比较linear/balanced/profile三臂，
  报告profile采集与评估划分、weighted/max depth、objective重算、IR/DAG、
  compile/solver CPU、candidate validity和coverage AUC；当前等级I/T。

## 253. F258：Shared-Destination Switch Edge/PHI Multiplicity Proof（2026-07-29）

- 研究问题：LLVM允许switch的多个case/default edge指向同一destination，但PHI
  必须为每条重复edge保存一个incoming，且同一predecessor的这些incoming value
  必须相同。F255曾直接拒绝shared destination。F258将“逻辑switch edge”与
  “唯一IFSS endpoint”分离：仍限制2--8条case/default edge，但只要求至少两个
  unique destination，使共享case归入同一data/memory/return state；
- 接受与PHI重写：collector用`getUniquePredecessor`而非
  `getSinglePredecessor`，并验证每个destination的CFG predecessor edge数等于
  原switch multiplicity；其每个PHI必须有相同数量、相同Value identity的controller
  incoming。Lowering删除全部重复incoming，再为每个新test/range leaf edge复制
  原值。所有edge同一destination没有state收益，仍fail closed；外部predecessor、
  不等incoming、EH/address-taken及原有结构拒绝边界不变；
- 两种multiplicity语义：linear chain每个原case/default仍对应一条新edge，所以
  示例共享目标为`3→3`。Balanced/profile tree的default由每个case leaf miss进入，
  因此三case示例中共享目标的两个case edge加三个default miss形成`3→5`；这不是
  重复插入bug，而是F256精确default gap展开。root携
  `shared-switch-edge-split-v1`，记录site、original/lowered总edge数、unique
  destination数及逐destination `(ordinal, original, lowered)`；受影响PHI携
  `shared-switch-phi-multiplicity-v1`；
- Data正确性修复：共享目标可能使merge仍只有两个unique incoming。旧两臂IFSS
  shortcut仅按root successor dominance选值，会把“非首case”误当成另一arm。
  F258检测root的shared-edge证书后强制两臂PHI走F252完整partition，逐路径AND并对
  同destination做OR，再构造ITE。MemorySSA原本已走该通用partition，但proof
  classifier也改为`must-alias-memoryssa-multi-v1`，不再错误声明direct-root proof；
- Exit组合：switch chain最后可能出现true/false同destination。F254现允许这种
  内部conditional edge按两条逻辑路径枚举，根controller仍要求不同successor；
  因而共享case的两个unique return blocks可继续归并为certified exit-state PHI；
- 验证与结果：`test/backsolver_switch_shared.ll`用default/case A/case C共享
  destination、case B独立的四edge switch，显式包含三个相同controller incoming
  的PHI。Linear verifier确认`3→3`，balanced/profile确认`3→5`，weighted与shared
  root proof可共存；symbolized data和MustAlias memory各生成完整路径ITE，
  switch→F254形成两返回state。输入`A`经复杂OR谓词的Z3 fallback生成并验证`B`
  候选。负向测试把全边同目标保留为未转换switch；
- 科研边界：同predecessor重复PHI value在LLVM verifier层必须相同，因此本实现
  不声称支持edge-specific不同值；也未处理external predecessor、critical edge
  跨region、indirectbr/callbr或循环switch。公开评估应报告original/lowered edge
  multiplicity、unique endpoints、default gap放大、路径/condition DAG、direct与
  Z3 fallback率、compile/solver CPU、validated yield及coverage；当前等级I/T。

## 254. F259：LLVM Branch-Weight Import 与可重放 Switch Manifest（2026-07-29）

- 研究问题：F257只把profile proof留在变换后IR，无法独立枚举最终tree/destination
  mapping，也未利用Clang/LLVM已有的标准`!prof branch_weights`。F259把外部
  stable-site profile、LLVM metadata、tree shape、fallback和shared multiplicity
  归一化为同一proof-carrying JSONL artifact；
- 来源优先级：`profile` mode若指定外部文件，则该文件中的对应site优先。文件
  unreadable/malformed或site entry invalid/incomplete时fail closed到balanced，
  即使原switch另有`!prof`也不静默掩盖配置错误；仅当未指定文件或文件中没有该
  site时，才调用LLVM `extractBranchWeights`。Successor 0严格映射default，
  successor 1..C映射原case order，再按unsigned case value重排。权重数须等于
  `C+1`且全部非零；zero或invalid metadata产生分类fallback；
- Proof扩展：`profile-weighted-switch-v1`新增来源
  `external-stable-site`或`llvm-branch-weights`，profile指纹同时绑定source、
  stable site、有序case/value/weight及default weight。相同计数但不同来源不会
  被误认为同一证据；
- Manifest：`SYMCC_IFSS_SWITCH_MANIFEST_OUT=<path>`为每个接受switch追加一行
  `symcc-ifss-switch-tree-v1` JSON。记录module/source/function/site、
  requested/effective mode、node schema、logical/lowered edges、unique/shared
  destinations、逐destination original/lowered multiplicity、source-order case
  inventory、normalized weights、fallback、profile fingerprint/objective，以及
  完整preorder comparison tree。Site、case、weight与fingerprint均用规范十进制
  string保存，避免JSON consumer的64位精度损失；
- Tree身份：`tree_fingerprint`绑定site、effective mode、profile fingerprint、
  logical/lowered edge totals、default destination、逐destination multiplicity、
  每个case ordinal/value/destination和所有range split；因此相同comparison shape
  但不同state mapping也不能共享身份。所有manifest在CFG改写前构建、变换全部完成
  后一次追加，避免读取已删除switch；
- 独立验证：`util/verify_ifss_switch_manifest.py`不依赖LLVM，逐JSONL record重算
  FNV-style profile/tree fingerprint、饱和`uint64_t` interval DP及objective、
  linear顺序或balanced/profile split、preorder node坐标、default expansion和
  shared multiplicity。任何schema、weight、split、destination、objective或
  fingerprint不一致均非零退出；
- 验证与结果：`backsolver_switch_profile.ll`验证external与LLVM metadata均把
  七case root置于0、objective 2028，explicit incomplete external仍回退root 2；
  manifest经独立verifier，篡改root value被拒绝。
  `backsolver_switch_branch_weight_reject.ll`验证zero branch weight分类回退；
  `backsolver_switch_shared.ll`验证linear/balanced的多record manifest及`3→3`/
  `3→5` multiplicity可重算；
- 科研边界：聚合default weight没有value-gap分布，故range split仍只优化case
  weights，也**不**把推测概率写回生成branch的标准`!prof`；这样牺牲下游概率提示
  但避免伪造观测。当前append artifact没有跨进程锁/原子rename，也未绑定编译器
  binary hash、在线衰减或置信区间。公开实验应封存输入profile、IR、manifest和
  verifier版本，报告source覆盖/回退率、objective、weighted depth、compile/
  solver CPU与coverage AUC；当前等级I/T。

## 255. F260：Bounded Multi-Continuation Exit-ID/Live-Out Tuple（2026-07-29）

- 研究问题：F254只能把多个normal return归并成跨调用返回状态，不能表达
  `break`/早退分支继续到不同CFG continuation的控制身份。F260新增
  `IFSSContinuationLowering` module pass，把一个严格有界区域的多个逻辑出口编码为
  `ExitState { exit_id, scalar_live_outs[] }`。`exit_id`不是仅供审计的标签，而由
  synthetic dispatch branch chain实际消费；既有F252/F253随后为exit-id和每个
  live-out PHI恢复完整path partition与ITE；
- 结构证明：controller必须是两个不同successor的conditional branch。从controller
  做确定性DFS，只允许`BranchInst`、single-entry dominated、acyclic、非EH、
  非address-taken区域；遇到不再被controller支配或以PHI开头的block即视为边界。
  每个候选限制2--8条唯一CFG exit edge、至少两个continuation destination、
  最多32个区域block和64条枚举路径。每个函数只提交指令序中第一个完整候选，
  所有函数候选在任何CFG修改前收集；
- Tuple完备性：destination中的每个PHI都是一个live-out slot，整个region限制1--8
  个slot，只接受integer/floating/pointer。对每个region source predecessor，
  incoming数量必须与CFG edge multiplicity精确相等；LLVM重复edge要求相同
  `Value*`。直接`undef`/`poison`、vector/aggregate/token、缺失edge或超过slot预算
  会拒绝整个候选。Destination的外部incoming保持不变；本pass只替换来自已证明
  region的incoming；
- Lowering语义：每条原exit edge先进入独立capture block，再汇入dispatch。
  Dispatch含一个`i8 ifss.cont.exit_id` PHI和每slot一个live-out PHI。该slot所属
  destination的exit携真实incoming，其他exit携类型正确的零/null中性值；中性值
  只存在于不会消费该slot的continuation路径。Dispatch按exit ordinal比较
  `exit_id`，进入独立resume trampoline，再跳回原destination。Arm内计算、store和
  call均留在capture之前的原路径，不被线性执行或投机复制；
- SSA/edge正确性：原destination PHI删除所有region source incoming，再按逻辑exit
  逐一添加`(dispatch-liveout,resume)`；同predecessor双edge因两个capture/resume
  保持精确multiplicity。全部slot、edge和预算验证成功后才改IR。新dispatch比较及
  branch携`symcc.ifss_force_partition`，确保共享/非一一映射的两臂consumer不误用
  direct-root shortcut；`Symbolizer`的data和MemorySSA proof分类均识别该强制标记；
- Proof metadata：controller与dispatch状态携
  `bounded-continuation-tuple-v1`及controller site、exit/destination/slot/block/
  path count。Capture和resume edge记录exit ordinal、原terminator site、
  successor index和destination ordinal；每个dispatch/destination PHI绑定slot
  ordinal、destination ordinal与原PHI site。这是可静态审计的结构证书，不是独立
  CFG verifier或alias proof；
- 验证与结果：`test/backsolver_continuation_tuple.ll`覆盖3 exits、2 destinations、
  2 live-outs和真实两级exit-id consumer。纯lowering与symbolized IR均经LLVM
  verifier；F252/F253为exit-id及两slot生成6个ITE。输入`C`走第三出口，原生QSYM
  生成跨destination输入`B`，本次记录为Backsolver targets/attempts/SAT
  `3/3/2`、direct attempts/SAT `9/2`及一次Z3 fallback。
  `backsolver_continuation_shared.ll`证明同source双edge的`2→2` PHI multiplicity；
  `backsolver_continuation_tuple_reject.ll`覆盖单destination、vector、poison和cycle。
  Flag关闭保持原CFG。加入F260后全量compiler/lit为139/139，LLVM18 `-Werror`
  build通过；
- 科研边界：当前tuple只承载destination PHI可表示的scalar SSA live-out，不包含
  MemorySSA location、aggregate decomposition、exception/coroutine/indirect exit、
  loop-carried state或跨函数continuation。Neutral slot依赖“未选destination不消费”
  的控制证明，不能被当作全路径有效值。公开评估应报告候选/接受/拒绝taxonomy、
  exits/destinations/slots/paths、ITE/DAG和constraint增量、compile/solver CPU、
  validated cross-destination yield及coverage AUC，并做
  `SYMCC_IFSS_CONTINUATION_STATE=0/1`等CPU消融；当前等级I/T。

## 256. F261：Proof-Carrying Bounded Affine Natural-Loop Summary（2026-07-29）

- 研究问题：逐迭代concolic execution会为小符号trip loop重复构造相同branch与
  recurrence DAG；简单unroll又按bound复制IR。F261新增`IFSSLoopSummary` module
  pass，对一个可完整证明的自然循环执行闭式加速，把
  `x_{t+1}=x_t+c`归约为`x_exit=x_0+T*c (mod 2^w)`，使后续SymCC直接求解
  `mul/add`而不重放0--8次loop condition；
- 自然循环形状：使用LLVM `DominatorTree/LoopInfo`，只接受恰好两个block的
  canonical loop：唯一preheader无条件进入header，header是唯一exiting block，
  true edge进入唯一latch，false edge进入唯一exit，latch无条件回header，且只有
  一条backedge。四个边界block不得address-taken/EH；每个函数只提交preorder中
  第一个完整候选；
- Trip证明：header condition必须精确为unsigned
  `icmp ult %iv, %trip`，induction从0开始并由无`nuw/nsw`的普通`add %iv,1`
  更新。Trip必须loop-invariant，静态上界不超过8。当前上界解释器有界递归8层，
  支持integer width<=3、常量、`freeze`、到<=i3的`trunc`、`zext`、两臂
  `select`、constant-mask `and`及positive-constant `urem`；直接undef/poison、
  无法证明或上界>8均保持原loop。由`0<=T<=8`和unit step可知退出前induction不
  wrap，执行次数精确为T；
- State证明：除induction外要求1--8个integer header PHI。每个state必须有
  preheader initial和latch update `add phi, ConstantInt`，类型完全一致，且update
  无`nuw/nsw`。Header除PHI/condition/branch、latch除这些updates/branch外只允许
  debug intrinsic；因此load/store/call/fence/atomic、非仿射或交叉state recurrence
  全部拒绝。Initial direct undef/poison拒绝，且至少一个非induction state在loop外
  被观察；
- 模算术声音性：LLVM plain integer add是位向量模`2^w`运算，重复T次常量加法与
  `initial + trunc_or_zext(T)*step`在同一ring中等价；state宽度小于trip时先trunc
  仍保持该ring中的T同余类，宽度较大时zero-extend。原update若有`nuw/nsw`，重复
  执行可能产生poison而闭式flags条件不同，故不复制或弱化flags，而是整体拒绝；
- CFG与SSA事务：所有候选先收集并冻结原始site。每个state在preheader生成真实
  `mul/add`（即使operand为常量也禁止IRBuilder折叠），exit PHI先新增合法preheader
  incoming，外部state use改为summary，preheader再重定向到exit，最后由LLVM
  `DeleteDeadBlocks`删除header/latch并清理旧incoming。这一顺序避免瞬时PHI/
  predecessor不一致；loop内effects不存在，所以无操作被投机移动；
- Proof metadata：summary `mul/add`和重定向branch携
  `bounded-affine-loop-summary-v1`，绑定原header/condition-branch site、
  maximum trip、state count、state ordinal、原PHI/update site。Branch使用
  `stateOrdinal=stateCount`并绑定induction PHI/update。这是结构与闭式来源证书，
  尚不是独立重放的LoopInfo/SSA verifier；
- 验证与结果：`test/backsolver_loop_summary.ll`覆盖一状态i8和双状态
  i8-trip→i16-state、exit PHI清理及flag-off。原始/summary IR用`lli`对输入
  `0x00..0x1f`逐一差分，覆盖每个trip 0--7四次，return status全部相等；
  symbolized IR产生直接`_sym_build_mul/add`。输入`00`、target state 25时常规
  solver只用1 query/1 SAT生成精确候选`05`，Backsolver target为0符合闭式无ITE
  的预期。`backsolver_loop_summary_reject.ll`覆盖bound 15、store、乘法recurrence、
  signed condition、`add nuw`和poison trip，均无schema且verifier-clean。F261后
  全量compiler/lit为141/141，LLVM18 `-Werror` build通过；
- 科研边界：这不是通用loop invariant推断、polyhedral acceleration或loop
  invariant code motion。当前不支持多block body、多个exit、break/continue、
  affine matrix/variable step、signed/descending induction、memory recurrence、
  calls、exception、nested interaction或large/symbolically unbounded trip。
  删除loop也改变instrumented edge集合，因此公开评估必须以原程序replay coverage
  为权威，并报告候选/接受/拒绝、trip/state分布、IR/DAG缩减、compile/solver CPU、
  candidate validity和coverage AUC；做`SYMCC_IFSS_LOOP_SUMMARY=0/1`等CPU消融后
  才能声称收益。当前等级I/T。

## 257. F262：MemorySSA/AA-Proven Continuation Memory Tuple（2026-07-29）

- 研究问题：F260只把destination PHI表示的scalar SSA值放入continuation tuple。
  一个exit若通过store传递状态、而对应continuation再load该location，普通动态
  shadow memory只能观察已执行路径的store，无法为未执行exit构造隐式控制值。直接
  把load复制到capture同样只读取当前路径状态，不能形成完整候选空间。F262因此在
  F260之后、Symbolizer之前新增`IFSSContinuationMemory` Function pass，以LLVM
  MemorySSA定义状态、AliasAnalysis的MustAlias/ModRef结论和F260 edge证书共同构造
  显式per-exit memory live-out PHI；
- 结构绑定：候选必须是F260 destination中的有use simple scalar load，类型限
  integer/floating/pointer。其MemoryUse defining access必须是F260 dispatch中的
  MemoryPhi，MemoryPhi incoming数、dispatch capture predecessor和
  `bounded-continuation-tuple-v1` exit count必须一致。Destination全部predecessor
  必须是同一controller、同一destination ordinal的F260 resume edge，且集合与
  capture证书中的相关exit精确相等；external predecessor、缺失/重复ordinal和
  metadata不一致均拒绝；
- Memory proof：对每个会到达该destination的exit，从dispatch MemoryPhi对应
  capture incoming向后遍历最多8个MemoryDef。第一个对load location为MustAlias的
  writer必须是non-atomic/non-volatile、等宽simple store，stored value不能直接为
  undef/poison，且store/value都支配capture terminator。中间definition只有在
  MemorySSA存在且AA证明对该location为NoMod时才能跳过；MayAlias/Mod、MemoryPhi、
  atomic/fence、预算耗尽或没有覆盖store均使该location拒绝；
- Lowering语义：每个controller最多4个memory slots。全部候选先收集；超过预算时
  该controller不做部分提交。成功候选在dispatch增加
  `ifss.cont.memory` PHI：相关exit携其reaching store value，其他destination的
  exit携同类型zero/null。中性值只位于控制证明不会消费该slot的路径。Destination
  原load保留并继续执行，因此原有trap/访存时序不被删除；其数据uses改为MemorySSA
  已证明等价的dispatch value。Load之前若destination已有任何may-write操作则拒绝，
  防止把后续状态错误地前移；
- Symbolic composition：新PHI是普通scalar tuple state，F249/F252/F253按原
  controller路径构造完整ITE并复用condition ownership。原load标记
  `symcc.ifss_continuation_memory_source`，避免F250再次尝试同一dead symbolic merge。
  新PM自动顺序为switch、loop summary、return exit、F260 continuation、F262
  memory tuple、Hydra、live continuation和Symbolizer；legacy PM保持相同顺序；
- Proof metadata：`must-alias-continuation-memory-tuple-v1`绑定controller site、
  memory slot ordinal、destination ordinal、原load site、总exit/相关exit数，
  并为每个相关exit记录exit ordinal、MustAlias store site、按nearest-first顺序的
  skipped NoMod definition数量和site。该IR证书足以静态定位证明输入，但尚不是能在
  独立进程中重算AA/MemorySSA的sealed artifact；
- 验证与结果：`test/backsolver_continuation_memory.ll`覆盖3 exits、2
  destinations、两个不同location及每exit一项NoMod def-chain。Lowered与flag-off
  IR对全部256个一字节输入用`lli`差分一致；symbolized IR经LLVM verifier且两个
  memory slots都形成ITE。原生输入`C`生成跨destination的`B` backsolve witness；
  记录为Backsolver targets/attempts/SAT `3/3/2`、direct attempts/SAT `9/2`和一次
  Z3 fallback。`backsolver_continuation_memory_reject.ll`覆盖MayAlias干扰、相关
  exit缺store、volatile load、external predecessor、destination前缀写和第5个
  slot的controller级原子回退；
- 科研边界：F262处理完整等宽scalar MustAlias store，不做partial-overlap byte
  composition、initial-memory fallback、nested/cyclic MemoryPhi、symbolic alias、
  aggregate/vector、atomic/concurrent memory、destination内写后load或跨函数memory
  state。它是F250--F252 proof在multi-continuation上的有界组合，不是general
  MemorySSA/points-to或完整IFSS memory model。公开评估应报告候选location、相关
  exits、def-chain长度、拒绝taxonomy、tuple/ITE规模、compile/solver CPU、
  validated cross-destination yield和original-replay coverage AUC；当前等级I/T。

## 258. F263：Replay-Verifiable Continuation CFG/Tuple Manifest（2026-07-29）

- 研究问题：F260/F262的metadata能在IR中定位controller、exit、slot和MemoryDef
  chain，但缺少独立artifact时，实验脚本无法证明一个运行使用了哪组CFG/tuple，
  也不能检测序号、destination或chain在归档过程中的篡改。F263新增
  `SYMCC_IFSS_CONTINUATION_MANIFEST_OUT` JSONL和独立
  `util/verify_ifss_continuation_manifest.py`；
- 输出前重放：manifest不是把metadata直接转成JSON。F263在F262 Function pass的同一
  AA/MemorySSA分析时点重新收集dispatch capture，要求exit ordinal 0..N-1完备且
  `(original terminator site, successor index)`唯一；再扫描全部resume，要求每个
  exit恰好一个resume、source/successor/destination与capture proof一致，且每个
  destination至少一条edge。Scalar slots按ordinal重放原PHI site和destination；
- Memory proof二次验证：对每个`ifss.cont.memory` PHI，先由metadata取得slot、
  destination和原load site，再唯一定位仍保留的source load，重新调用F262
  `collectCandidate(requireUse=false)`。这会再次检查load MemoryUse→dispatch
  MemoryPhi、精确capture/resume集合、AA MustAlias、最多8项NoMod chain、
  store/value dominance和destination前缀无写。重算的每个exit/store/skipped site
  必须与metadata逐项相同，之后record才能标记
  `llvm-memoryssa-aa-revalidated-v1`；没有memory slot的F260结构record标记
  `structural-only-v1`；
- Artifact schema：`symcc-ifss-continuation-manifest-v1`保存module/function、
  controller site、exit/destination/scalar/memory/block/path count，规范exit表、
  destination→exit partition、scalar slot→original PHI以及memory
  slot→load→per-exit store/NoMod chain。64位site和fingerprint用规范十进制string
  保存，避免JSON number精度损失；
- 内容身份：`proof_fingerprint`使用与stable site相同的64位FNV-style mix，顺序
  绑定schema、module、function、所有预算、exit edge、scalar slot和memory
  load/store/skipped chain。字段顺序由ordinal规范化；相同CFG但不同memory writer
  或NoMod chain不会共享身份；
- 独立验证：Python verifier不加载LLVM。它重算canonical uint64、全部count/bound、
  edge唯一性、destination partition、slot ordinal、相关exit集合、NoMod长度<=8、
  site局部唯一性、analysis分类和完整fingerprint。它能验证artifact内部结构与
  内容身份；MemorySSA/AA语义重算已在compiler输出门完成，离线端不声称自己实现
  LLVM BasicAA；
- 验证与结果：`backsolver_continuation_memory.ll`现在生成memory-on和memory-off
  两个manifest，独立verifier分别接受`llvm-memoryssa-aa-revalidated-v1`和
  `structural-only-v1`。测试把exit destination、首个NoMod site和fingerprint各
  篡改一次，三者均非零拒绝；原有256输入差分、native `C→B` witness和LLVM
  verifier继续通过。Python脚本通过`py_compile`与Ruff，C++通过LLVM18 `-Werror`；
- 科研边界：输出是append-only，尚未做跨compiler process文件锁、atomic rename、
  compiler binary/LLVM build identity、输入IR cryptographic digest或artifact
  sealing。离线verifier重算结构和FNV内容身份，不从bitcode重跑MemorySSA/AA；因此
  公开实验应同时封存输入/输出IR、compiler identity和manifest，并在单build新文件
  上运行verifier。当前等级I/T/E。

## 259. F264：Atomic SHA-256 Continuation Artifact Seal（2026-07-29）

- 研究问题：F263 proof fingerprint能检测record内部变化，但64位FNV不是
  cryptographic identity，append manifest也没有绑定输入IR、最终lowered IR、
  compiler pass binary或LLVM tool。把这些hash放进Function pass仍不正确，因为
  pass运行时还看不到最终输出文件。F264新增build后独立工具
  `util/seal_ifss_continuation_artifact.py`，把结构证明与真实构建制品分层；
- Seal流程：`seal`子命令先调用F263独立verifier，读取全部非空record的
  `proof_fingerprint`并锁定顺序/数量。随后流式计算manifest、input IR、lowered
  IR、SymCC compiler module和实际LLVM tool五个文件的SHA-256与byte size；所有路径
  先`resolve(strict=True)`且目标必须为regular file。LLVM identity通过受5秒deadline
  和16 KiB上限约束的`<tool> --version`取得；
- Envelope：`symcc-ifss-continuation-seal-v1`保存record count、有序proof
  fingerprints、五个artifact的resolved basename/size/SHA-256以及完整LLVM identity。
  对不含`seal_sha256`的对象使用UTF-8/ASCII、sorted-key、无额外空白的canonical
  JSON，再计算外层SHA-256。时间戳、绝对路径和随机nonce不进入身份，因此相同输入
  可复算；
- 原子封存：输出旁路`.lock`使用`flock(LOCK_EX)`串行化竞争者；锁内要求seal path
  尚不存在，拒绝覆盖已有证据。同目录`mkstemp`写入后flush+file `fsync`，
  `os.replace`提交，再对目录`fsync`，从而避免部分JSON、跨文件系统rename和
  crash后目录项未落盘。失败路径清理临时文件；
- Verify流程：`verify`先检查schema与canonical lowercase 64-hex outer hash，再对
  去除hash的envelope重算SHA-256；随后重新验签manifest、重新hash所有文件、重取
  LLVM identity并要求完整对象相等。验证必须显式提供全部artifact路径，不允许
  仅凭seal自证；
- 验证与结果：`backsolver_continuation_memory.ll`在F263 record上完成seal/verify；
  同一output二次seal因fresh-output gate拒绝。把lowered IR追加一行注释后验证失败，
  修改sealed `record_count`但不更新outer hash也失败。脚本通过`py_compile`和Ruff，
  原有manifest tamper、256输入差分与native witness保持通过；
- 科研边界：F264绑定的是调用者提供的input/lowered IR和binary，不能证明这些文件
  一定来自同一个命令行，也没有签名密钥、远端透明日志、TPM attestation或
  content-addressed object store。`flock`只保证同一支持POSIX锁的文件系统；建议
  每个compile job使用独立seal path，再将verified envelope纳入F67实验manifest。
  当前等级I/T/E。

## 260. F265：Independent Bitcode MemorySSA/AA Proof Replay（2026-07-29）

- 研究问题：F264证明manifest和制品在seal之后没有改变，但完整性不等于语义有效性。
  攻击性较低但科研上同样危险的例子是：修改lowered IR中的alias关系，再用原manifest
  生成一个新的、内部完全一致的seal。单独SHA-256无法判断旧的MustAlias/NoMod chain
  是否仍适用于新IR。F265新增`util/replay_ifss_continuation_artifact.py`，把F263的
  compiler-time重放提升为sealed artifact上的独立LLVM进程重放；
- 身份门：replay首先完整调用F264 verify，重新验证outer hash、manifest、
  input/lowered IR、SymCC pass binary、LLVM tool及version identity。随后再次运行
  F263 verifier并读取规范JSON records；因此独立证明使用的IR、pass实现和`opt`
  executable均由同一envelope绑定，而不是从`PATH`隐式选择；
- 独立分析进程：工具在fresh temporary directory启动新的`opt` subprocess，只运行
  `-passes=ifss-continuation-memory`。环境强制
  `SYMCC_IFSS_CONTINUATION_MEMORY=0`和`SYMCC_IFSS_CONTINUATION_STATE=0`，禁止再次
  lowering，只通过`SYMCC_IFSS_CONTINUATION_MANIFEST_OUT`请求证明重建。进程有60秒
  deadline，错误输出截断为最后4 KiB，避免挂起或无界诊断污染实验控制面；
- 语义重建：sealed lowered IR中保留的F260/F262 proof metadata只用于定位
  controller/load/store sites；pass仍需从bitcode重新构造当前MemorySSA和AA结果，
  验证capture/resume partition、source load的MemoryUse、dispatch MemoryPhi、
  MustAlias writer、最多8项NoMod chain、dominance及destination前缀无写。新manifest
  必须先通过独立Python verifier，再与sealed原record做数量和JSON对象逐项完全相等
  比较；
- 验证与结果：正例对F262的3-exit/2-destination/2-location lowered IR完成seal、
  verify和新进程replay。反例把一条原先写`%noise`且被AA判为NoMod的store改为写
  `%slot_a`；修改后的IR仍通过LLVM verifier，也能使用原manifest生成一个新的有效
  SHA-256 seal，但F265独立replay非零拒绝。这明确区分了artifact integrity与
  proof semantic validity；两个continuation定向lit均通过；
- 科研边界：这是process-independent、artifact-bound的同实现重放，不是第二套AA
  算法或形式化证明检查器；compiler plugin bug可能在产生和重放阶段一致复现。
  subprocess尚未使用容器、seccomp或hermetic loader，也没有签名/透明日志。公开
  实验应保存seal、重放stdout/stderr摘要和返回码，并将“seal verified”与
  “MemorySSA/AA replayed”作为两个独立证据字段。当前等级I/T/E。

## 261. F266：Upper-Triangular Affine Loop Closed Form and Manifest（2026-07-29）

- 研究问题：F261只处理彼此独立的`state += constant`，不能摘要常见的归纳链，
  例如`x'=x+2y+3; y'=y+z; z'=z+1`。直接按`trip=0..7`生成每个候选状态和select
  虽然concrete等价，却会造成单块内数百条symbolic operation；早期原型实际触发
  Symbolizer反复split后的非法CFG。F266没有掩盖此失败，而改用unipotent
  upper-triangular matrix的紧凑闭式；
- 识别边界：沿用F261经LoopInfo证明的单preheader、header、latch、backedge、
  exiting header、单exit、恰好两loop blocks及`iv=0; iv<trip; iv++`、`trip<=8`。
  新路径接受2--4个统一整数位宽（最多4096 bit）的state PHI；update expression是
  最多32个latch指令组成的plain `add/sub`与常数`mul` affine DAG。矩阵对角必须全1，
  row `i`只能依赖column `i..N-1`且至少有一项cross-state coefficient。`nuw/nsw`、
  非常数乘法、逆三角依赖、非unit diagonal、跨位宽、额外指令和原有副作用边界均
  fail closed；F261独立更新仍走原schema和代码路径；
- 模位宽代数：编译器把update规范化为`x'=Ax+b`的APInt矩阵，在状态的
  \(\mathbb Z/2^w\mathbb Z\)环中计算。令`N=A-I`，upper-triangular与unit diagonal
  保证`N^state_count=0`，因此
  `A^T=sum C(T,k)N^k`，累计偏移为
  `sum C(T,k+1)N^k b`。`T<=8`时二项式系数在i16中用
  `C(T,k)=C(T,k-1)*(T-k+1)/k`精确递推，无符号减法在`T<k`时只与已经为0的前项相乘；
  随后系数截断到state width，保持LLVM plain integer的模位向量语义；
- IR事务：所有`N^k`与`N^k b`在compile time计算，preheader只生成共享binomial DAG
  和每state的仿射组合，不枚举trip select。生成值携
  `bounded-upper-triangular-loop-summary-v1`，绑定header/branch/state/update sites、
  bounds、width和recurrence fingerprint。Exit PHI、外部uses、preheader redirect
  与dead-block删除复用F261事务；任何收集失败都不改IR；
- 可验证证据：`SYMCC_IFSS_LOOP_MANIFEST_OUT`输出
  `symcc-ifss-loop-recurrence-manifest-v1` JSONL，保存module/function、sites、初值
  身份、规范`A,b`以及trip 0..maximum的全部`A^t`与累计offset。独立
  `util/verify_ifss_loop_recurrence_manifest.py`重算canonical uint、upper-triangular/
  diagonal/cross-state约束、每一项模矩阵乘法与offset recurrence，并重算绑定
  schema/module/function/sites/initial/A/b的64位内容指纹；
- 验证与结果：三状态链式回归经LLVM verifier，摘要前后对32个一字节输入用`lli`
  完全一致；symbolized闭式IR也通过verifier。从输入`00`求解`result==184`得到低
  三位为5的候选，telemetry为1 query/1 SAT、一次Z3 solve、约6.4 ms solver CPU。
  Manifest基矩阵、派生power和fingerprint三类独立篡改均拒绝。逆三角依赖、
  non-unit diagonal和cross-width coupling保持原循环；三项loop定向lit通过，
  Python通过`py_compile`和Ruff，C++通过LLVM18 `-Werror`；
- 科研边界：F266仍不处理multi-block/multi-exit、非unit diagonal一般矩阵、
  symbolic coefficient、非线性、memory recurrence、异常或unbounded loop。
  i16 binomial正确性依赖公开的`trip<=8`证明；manifest验证代数artifact，但尚未
  cryptographically seal input/output IR，也未独立解析bitcode重建update DAG。
  删除loop edge会改变instrumented coverage，公开实验必须original-authoritative
  replay并报告IR DAG规模、solver CPU和接受/拒绝taxonomy。当前等级I/T/E。

## 262. F267：Bounded Post-Update Break/Continue Exit Tuple（2026-07-29）

- 研究问题：F261/F266要求唯一exiting header，只能表达trip耗尽后的normal exit。
  真实循环常在完成本轮state update后根据归纳变量break；简单删除循环会混淆
  normal header state与break latch state。F267在不扩大loop body或break predicate
  语言的条件下，为这一canonical multi-exit形态建立显式exit identity和live-out
  证明；
- CFG/语义边界：仍要求两块natural loop、单preheader/header/latch/backedge、
  `iv=0; iv<trip; iv++`和`trip<=8`。Header false edge是normal exit；latch在完成
  已接受的F261/F266 recurrence后，只允许`icmp eq iv, break_at`，true edge到独立
  break exit，false edge继续header。`break_at`必须同位宽且loop invariant。
  `ne`、比较`iv_next`、反向successor polarity、相同exit、第三exit、loop-variant
  break value或任何既有副作用均fail closed；
- Closed form：对当前迭代索引进行后更新break时，
  `break_taken = ult(break_at, trip)`，
  `executions = select(break_taken, break_at + 1, trip)`。`trip=0`自然得到normal
  exit和0次迭代；`break_at<trip`保证被选择的加一值在0..8内。F261 independent
  closed form和F266 nilpotent/binomial closed form都改用`executions`，无需枚举
  每轮break条件；
- Live-out事务：normal exit的loop-owned PHI输入只能是header `iv/state`，break
  exit输入只能是latch `iv_next/state_next`；direct external use、错误phase value
  或缺失/重复source edge拒绝。Lowering在preheader生成共享state摘要和
  `break_taken`分支，为两组exit PHI分别增加同一preheader的正确summary输入，再
  删除header/latch。LLVM随后可折叠单入边PHI，但共同final PHI仍保留normal/break
  logical-edge multiplicity；生成值携`bounded-break-loop-exit-v1`并绑定
  header/condition/two-exit sites、bound和state count；
- 独立证据：`SYMCC_IFSS_LOOP_EXIT_MANIFEST_OUT`输出
  `symcc-ifss-loop-exit-manifest-v1` JSONL，绑定module/function、header/branch/
  break/normal/break sites、trip/break identities、induction/state PHI与update
  sites、state widths/steps或F266 recurrence fingerprint。Artifact还显式保存
  `trip=0..maximum × break_at=0..maximum`的完整
  `(break_taken, executions)` truth table。独立
  `util/verify_ifss_loop_exit_manifest.py`重算bounds、phase-specific live-out
  identity、truth table和完整64位fingerprint；
- 验证与结果：两字节正例分别控制trip和break_at，normal消费更新前state、break
  消费更新后state并加100 exit bias。摘要前后对8×8共64组输入的`lli`状态完全一致，
  lowered与symbolized IR均通过LLVM verifier。从`00 07`出发求解result 119，生成
  `break_at=2, trip>=3`候选；telemetry为5 queries、3 SAT、2 UNSAT，Backsolver
  1/1 SAT。Manifest execution row、break live-out site和fingerprint篡改均拒绝；
  四类定向fail-closed测试在已证明`trip<=7`后才触发各自边界；
- 科研边界：F267是单一post-update equality break与implicit continue，不处理
  pre-update break、`iv_next`语义、多个break/continue sites、break predicate DAG、
  multi-block body、side-effecting/memory recurrence、嵌套循环或异常edge。Exit
  manifest验证公式和身份但尚未seal IR/binary，也没有独立bitcode CFG replay。
  公开评估应区分normal/break候选、truth-table bound、removed loop branches、
  solver CPU和original replay coverage。当前等级I/T/E。

## 263. F268：Bounded Multi-Block Linear-Arm Hydra Alignment（2026-07-29）

- 研究问题：F248只接受每个分支各含一个basic block的strict diamond，因而不能处理
  编译器前端或轻量优化产生的线性多块arm。F268把同一profile-guided single-site
  Hydra事务扩展到有界线性region，同时保留F248的原程序权威重放、spurious failure
  denylist和failure-preservation边界；
- CFG边界：每侧arm必须由1--4个unique-predecessor basic block组成，内部块只有
  unconditional branch，两个arm块数相同并汇入同一恰有两个predecessor的merge。
  Arm内禁止PHI；既有call/invoke、atomic/volatile、block address、unsupported
  instruction和escaping SSA限制继续生效。跨块SSA use只允许留在同一arm，离开arm
  只能成为merge PHI从该arm tail输入。不同长度arm、内部条件分支、循环、异常edge
  或额外外部predecessor均fail closed；
- 扁平化对齐：编译器按CFG顺序收集每侧block和instruction，把每条arm压平为一个
  保持def-before-use的序列，再复用F248 compatible-operation LCS。Matched operation
  逐operand生成condition select并构造merged instruction；unmatched safe ALU仍按
  F248规则补齐。由于arm内SSA use可跨块，对齐与value map覆盖整个flattened region，
  而不是逐块局部匹配。Merge PHI从两个tail block读取，成功后一次性删除全部arm
  blocks；任何晚期失败不提交部分CFG；
- Proof metadata与artifact：变换后的branch携
  `bounded-multiblock-linear-hydra-v1`，绑定stable site、arm block count及两侧
  flattened instruction count。`symcc-hydra-transform-v1` manifest新增
  `region_schema`、`arm_blocks`、`multi_block`、左右有序block sites、逐slot
  instruction sites/opcodes、output PHI sites、
  `requires_original_coverage_replay=true`和64位`structure_fingerprint`。指纹按
  schema、controller、block序列、完整alignment与输出身份确定性混合，防止只篡改
  alignment描述而保留旧摘要；
- 独立验证与campaign gate：新增
  `util/verify_hydra_transform_manifest.py`，独立检查1--4块边界、左右site唯一且
  不相交、alignment ordinal、site/opcode同时出现、每条instruction只出现一次、
  matched opcode相同、aligned/output计数、mode/replay分类和完整结构指纹。
  `util/hydra_transform.py`在读取带`region_schema`的新record时强制调用该verifier，
  因而campaign不能消费结构篡改后的manifest；历史F248 checked artifact没有新字段，
  保持原验证路径兼容；
- 验证与结果：`test/hydra_multiblock.ll`构造2×2 block linear arms并验证lowered
  与symbolized IR。原始与变换IR对全部256个一字节输入用`lli`差分一致，SymCC输出
  通过LLVM verifier；block-site重叠、alignment opcode和fingerprint三类篡改均被
  独立verifier拒绝。2-block/1-block unequal-arm反例保持原CFG；F248原profile/safe/
  aggressive/denylist测试继续通过。Hydra三项定向lit、Python `py_compile`/Ruff和
  LLVM18 `-Werror`构建均通过；
- 科研边界：F268不是任意SESE region melding。它不对齐不同长度block chain，不
  处理arm内条件树、loop、exception、call或general memory dependence；aggressive
  store readback仍只有failure-preserving语义。Safe模式的concrete差分不等价于
  source-edge coverage等价，因此所有模式都显式要求original-authoritative
  coverage replay。公开实验需报告接受/拒绝region形状、LCS/edit规模、fork与solver
  CPU、transformed/original coverage AUC、spurious failure和denylist收敛，并在相同
  总CPU下至少20次配对重复。当前等级I/T/E。

## 264. F269：Partial Store/Live-on-Entry Continuation Memory Tuple（2026-07-29）

- 研究问题：F262要求一个destination的每条相关exit都沿MemorySSA链到达
  equal-width MustAlias store；只要一条路径保留进入函数时的内存值，整个location
  就会被拒绝。把缺store直接解释为零值不正确，而把load无条件前移到controller又会
  在其他destination不访问该地址的路径上引入额外fault。F269只接纳store与
  `MemoryLiveOnEntry`的proof-carrying部分重叠；
- 分层MemorySSA证明：每条相关dispatch incoming仍最多遍历8个MemoryDef，并只跳过
  AA证明对load location为NoMod的definition。链可终止于原F262的simple MustAlias
  store，或精确终止于当前Function MemorySSA的LiveOnEntry。LiveOnEntry路径要求原
  load pointer支配对应capture terminator；同一candidate至少有一条store路径，若
  所有相关exit都只是LiveOnEntry则无合并收益并保持原load。遇到nested/cyclic
  MemoryPhi、MayAlias/Mod、atomic/fence、超预算或不可前移pointer仍fail closed；
- 路径局部snapshot：对LiveOnEntry exit，pass在该exit独占的capture block、
  dispatch branch之前插入同类型、同alignment的simple load。它只在原本会resume到
  该destination并执行原load的路径上运行，且MemorySSA/NoMod证明保证capture处值与
  function-entry值相同；因此不会在无关destination上增加地址访问。Dispatch tuple
  对store exit携stored operand，对initial exit携snapshot，对其他destination仍携
  不可消费的typed zero/null；
- Proof schema与正确性加固：混合tuple使用
  `must-alias-or-live-on-entry-continuation-memory-tuple-v2`。每条相关exit显式记录
  `Store=0`或`LiveOnEntry=1`、store site或canonical zero以及完整NoMod chain；
  snapshot携同一proof node。F269同时修复F263/F265重放只核对metadata而未核对
  dispatch PHI实际incoming的缺口：现在重放要求每个predecessor恰有一个incoming，
  store路径必须等于原stored operand，initial路径必须是同capture、同pointer/type/
  alignment、到terminator之间无write且绑定同proof的load，无关路径必须为typed
  neutral。把tuple值改成另一合法LLVM value会被语义重放拒绝；
- Artifact与独立验证：outer manifest继续使用兼容的
  `symcc-ifss-continuation-manifest-v1`，但混合slot增加`state_schema`，每个state
  增加`state_kind`，analysis分类为
  `llvm-memoryssa-aa-live-on-entry-revalidated-v2`。FNV内容指纹在新slot中额外混合
  schema、kind与canonical source identity；Python verifier仍接受历史F262全store
  records，但要求v2 schema恰好包含至少一个LiveOnEntry state、initial state没有
  `store_site`，并重算新fingerprint；
- 验证与结果：三exit/两destination正例中，destination A的一条exit store 10，
  另一条经一项NoMod noise store到LiveOnEntry；caller用输入派生初值。Lowered与
  原始IR对256个输入完全一致，symbolized IR通过LLVM verifier。从输入`C`求解
  result 26得到跨partial-overlap路径的`B`候选，Backsolver target/attempt/SAT均
  非零。Manifest kind篡改拒绝；正例seal后独立bitcode replay成功。随后把合法IR中
  snapshot PHI incoming改成常量99并重新生成有效seal，F265 replay仍拒绝。部分
  initial路径上的MayAlias write、原有all-missing-store/volatile/external-prefix/
  budget反例均不生成memory tuple；三项continuation定向lit、LLVM18 `-Werror`、
  Python `py_compile`/Ruff和diff check通过；
- 科研边界：LiveOnEntry是函数级初始memory version，不是任意region-entry snapshot。
  F269尚不穿越nested MemoryPhi，不合并部分字节/不同宽度，不处理symbolic alias
  split、aggregate/vector、atomic/concurrent memory、跨函数MemorySSA或destination
  prefix write。Snapshot load保留原路径的顺序failure语义，但不构成异常/信号环境
  下的形式化等价。下一层应为有界acyclic nested MemoryPhi建立逐incoming provenance
  DAG与独立replay，再研究byte-lane partial overlap。当前等级I/T/E。

## 265. F270：Bounded Acyclic Nested-MemoryPhi Provenance Tree（2026-07-29）

- 研究问题：F269只能让每条dispatch incoming沿线性MemoryDef chain到达store或
  Function LiveOnEntry；若continuation之前已经发生控制流汇合，MemorySSA会以
  非dispatch `MemoryPhi`表示到达状态，线性证明会拒绝本可精确恢复的memory
  live-out。F270把每个相关exit的MemorySSA provenance递归展开成有界无环树，同时
  继续以F260 dispatch、capture/resume partition和destination load为证明根；
- 证明边界：递归节点只允许`Store=0`、`LiveOnEntry=1`和`MemoryPhi=2`。每个
  MemoryPhi必须支配下游capture或父phi incoming block，具有2--4个不同incoming
  block，且不能是F260 dispatch phi；每个exit最多4个nested phi、16个provenance
  nodes，每条边最多跳过8个经AA证明NoMod的MemoryDef。Active-set拒绝循环；simple
  equal-width MustAlias store仍需stored operand与store支配对应edge，LiveOnEntry
  pointer仍需支配路径局部snapshot。整个slot仍至少包含一条真实store路径；
  MayAlias/Mod、atomic/fence、5-arm phi、重复incoming、超预算、poison/undef或
  dominance失败均使整个candidate fail closed；
- IR物化：编译器按MemorySSA树自底向上，在每个原MemoryPhi block建立同类型scalar
  PHI，其incoming分别是原store operand、capture/edge-local initial snapshot或更深
  scalar PHI；所有节点携同一
  `symcc.ifss_continuation_memory_nested` proof。根值再进入F260 dispatch memory
  PHI，无关destination仍使用typed neutral。该方法不复制memory operation，也不
  把load提升到不访问原destination的路径；
- v3证据：proof schema为
  `bounded-acyclic-memoryphi-continuation-memory-tuple-v3`，每个相关exit按
  canonical preorder记录root、node ordinal/kind/source site、逐节点NoMod sites，
  以及每条incoming的block site和child ordinal。外层manifest保持v1兼容，增加
  `state_schema`、`root_node`、`provenance_nodes`，analysis分类为
  `llvm-memoryssa-aa-nested-phi-revalidated-v3`。内容指纹按相同树结构混合；
  Python verifier独立检查1--16节点、最多4 phi、2--4臂、唯一incoming block、
  child严格后置、除root外恰好一次引用、store/initial身份和完整指纹。独立LLVM
  replay重新构造MemorySSA/AA树，并递归核对实际nested/dispatch scalar PHI values；
- 通用正确性修复：两层正例发现region-value递归合成会先生成子runtime computation、
  后生成父concrete clone。Short-circuit分块可能把父clone前移到slow path之前，却
  越过仍位于其后的子clone，产生SSA dominance错误。`trySynthesizeRegionValue`
  现在对concrete operand closure做同block拓扑前移：先递归移动依赖，再移动父节点
  到最早symbolic computation之前。这个修复覆盖PHI select、binary/unary、
  comparison、cast、select和freeze，而不是为F270测试绕开符号化；
- 验证与结果：正例包含dispatch之外两层MemoryPhi，canonical provenance依次为
  `phi,phi,store,store,store`。Lowered与原始IR对256个selector乘4种内层控制值共
  1024组`lli`执行完全一致，lowered和symbolized IR均通过LLVM verifier；从
  `C 03`出发可生成`A`且低两位为0的Backsolver候选，target/attempt/SAT均非零。
  Manifest edge篡改被独立verifier拒绝；正向seal/replay通过，把最深scalar PHI的
  incoming从10改为99后，IR仍合法且可重新seal，但独立semantic replay拒绝。
  MayAlias inner arm和5-incoming nested phi均保持原load；v1/v2/v3四项continuation
  memory定向lit、LLVM18 `-Werror`、Python `py_compile`/Ruff和diff check通过；
- 科研边界：实现表示“将有界acyclic MemoryPhi DAG按使用路径展开成canonical
  provenance tree”，尚未共享同一DAG子节点，也不处理loop-carried/cyclic phi、
  byte-lane partial overlap、不同load/store width、symbolic alias split、
  aggregate/vector、atomic/concurrent memory、跨函数heap graph或异常语义。I/T/E
  证明机制正确和可重放，不代表公开target上的coverage/CPU收益；公开实验必须报告
  candidate/accept/reject taxonomy、树深/节点数、编译与solver CPU、候选有效率及
  original-authoritative coverage。当前等级I/T/E。

## 266. F271：Bounded Multi-Break Exit-Priority Circuit（2026-07-29）

- 研究问题：F267只能摘要latch上的单个post-update equality break。多个break site
  既不能简单OR，因为不同`break_at`命中不同迭代，也不能只取数值最小而忽略同一
  迭代的CFG先后顺序。F271为canonical post-update check chain构造显式优先级电路，
  将动态循环中的“最早迭代、同迭代最先site”语义变成有界closed form；
- CFG与拒绝边界：仍要求单preheader/header/backedge、`iv=0; iv<trip; iv++`、
  `trip<=8`和F261/F266 recurrence。Header true edge进入唯一update block；该block
  完成本轮所有state/induction update，随后可有2--3个线性check blocks。每个check
  仅允许`icmp eq iv, break_at[i]`和conditional branch，true进入唯一外部break
  exit，false进入下一check，最后false回header。`break_at`必须同位宽、无
  undef/poison且loop invariant。第四个break、`ne`、比较`iv_next`、true/false
  polarity反转、重复exit、额外block instruction、非唯一check predecessor、错误
  phase live-out、side effect或既有recurrence边界均fail closed；
- 优先级代数：令初始`winner_at=trip`。按CFG ordinal从0到B-1依次计算
  `better_i = break_at[i] < winner_at`，仅在strictly smaller时更新
  `winner_at`与`winner_ordinal`。因此较早ordinal在相等值上保持winner；值不小于
  trip的site从不eligible。最终
  `break_taken = winner_at < trip`，
  `executions = break_taken ? winner_at + 1 : trip`。F261 independent-add与F266
  nilpotent/binomial recurrence都消费同一个execution count；
- CFG事务与live-out：lowering在preheader计算winner circuit，再创建每break一个
  dispatch block。第i个block以
  `break_taken && winner_ordinal==i`进入对应break exit，否则继续下一dispatch；
  最后fallback到normal exit。Normal exit PHI只从最终dispatch消费header-phase
  summary；每个break exit PHI只从自己的dispatch消费post-update summary。
  原header/update/check blocks整体删除，late collection失败不修改IR。V1单break
  路径保持原branch形态与metadata，避免兼容性回归；
- Proof artifact：新IR使用`bounded-multi-break-loop-exit-v2` metadata，绑定
  header/branch/normal exit、bound/state/break count，以及有序condition/exit
  sites。Outer JSONL继续兼容
  `symcc-ifss-loop-exit-manifest-v1`，以
  `post-update-priority-equality-break-v2`区分语义；记录2--3个有序break的
  condition/exit/value identity、每state normal与全部break live-out sites、
  recurrence kind/fingerprint，并枚举
  `(trip, break_at[0..B-1])`完整有界truth table。最大B=3、bound=8时最多6561行；
  大于bound的值统一落入“不早于trip”的等价类；
- 独立验证：Python verifier保留F267 v1路径，并为v2独立检查break/site唯一性、
  state phase、2--3/1--8边界、lexicographic table顺序和每行winner/executions。
  Winner由最小`(break_at, ordinal)`重算，完整artifact以schema、CFG/state identities、
  recurrence和每行vector/taken/winner/executions计算64位内容指纹。Winner、condition
  site、break live-out和fingerprint四类独立篡改均拒绝；
- 验证与结果：2-break independent-add正例对8³=512组trip/break组合摘要前后
  `lli`完全一致，同值break由ordinal 0获胜；lowered/symbolized IR均通过LLVM
  verifier。从`00 07 07`求解break1-only结果219，生成`trip>=3, break0>2,
  break1=2`候选且solver query/SAT非零。3-break上限正例与F266两状态
  upper-triangular recurrence组合，recurrence/exit两个manifest均独立验签，
  对4⁴=256组输入差分一致并通过symbolized verifier。第四break、第二条件`ne`和
  第一check successor反向均保持原循环；8项loop定向lit、LLVM18 `-Werror`、
  Python `py_compile`/Ruff和diff check通过；
- 科研边界：F271只处理每轮update后的有序equality check chain，不处理pre-update
  break、任意predicate DAG、continue到非header位置、多个update phase、不同break
  对应不同recurrence、side-effecting/memory loop、nested loop、exception edge或
  unbounded trip。Truth table验证闭式和身份，尚未seal input/lowered IR，也未在
  独立bitcode进程重建原CFG。删除loop/check edges会改变instrumented bitmap，公开
  实验必须用原程序重放并报告break-count/priority taxonomy、removed dynamic
  checks、generated circuit/DAG、solver CPU、候选有效率和coverage/CPU。当前等级
  I/T/E。

## 267. F272：Proof-Carrying Unequal Linear-Arm Hydra Alignment（2026-07-29）

- 研究问题：F268已把single-block diamond推广到每侧1--4块的线性arm，但人为要求
  左右block count相等。Block boundary在该模型中只携无条件branch，现有变换又已经
  按CFG顺序flatten全部instruction并以全region value map维护跨块def-use，因此
  “块数相等”不是正确性条件，只会拒绝常见的front-end/optimization布局差异。
  F272移除这个伪边界，并把不同长度的edit alignment升级为可独立检查的v2 artifact；
- 接纳与预算：左右arm分别允许1--4个block，数量可以不同；每个非merge block仍需
  unique predecessor、无PHI、只有supported ALU或在aggressive mode允许的simple
  load/store，terminator必须unconditional。两侧必须到达同一恰有两个predecessor
  的merge，跨arm SSA禁止，escaping value只能成为merge PHI incoming。每侧新增
  64条flattened instruction硬预算。第5块、第65条instruction、内部条件分支、
  loop、exception/address-taken block、call、unsupported memory或错误external use
  仍fail closed；
- Edit alignment与lowering：确定性LCS继续用`compatible`（same operation/type/
  flags/address-space，store还要求同pointer）匹配，两种最优走法相等时固定消费左侧，
  形成`compatible-lcs-left-tie-v2`。Matched pair逐operand select后只执行一次；
  unmatched safe ALU用condition选择原operand或opcode-safe neutral operand，
  unmatched aggressive load/store沿F248 failure-preserving路径线性化。整个flattened
  序列共享左右value map，因此1×4跨四block依赖仍按def-before-use生成。Merge output
  PHI转为select，原branch变unconditional，左右所有block一次性删除；
- v1/v2兼容证据：等长arm仍输出字节兼容的
  `bounded-multiblock-linear-hydra-v1`。只有不同块数使用
  `bounded-unequal-linear-hydra-v2`，metadata分别记录left/right block count和
  instruction count。Manifest增加`left_arm_blocks`、`right_arm_blocks`、两侧
  instruction count、`edit_distance`、algorithm identity与
  `unequal_arm_blocks=true`；每个alignment slot增加左右block ordinal，缺失侧为
  -1。结构指纹混合v2 schema、独立块/指令数、edit distance、有序block sites、
  完整site/opcode/block-ordinal edit script和output PHI sites；
- 独立验证与campaign gate：verifier按schema分派，继续原样验证F268 v1。V2要求
  1--4且左右不等、最多64条instruction、site/opcode/ordinal presence一致、两侧
  instruction site各出现一次、block ordinal单调、matched opcode相同、one-sided
  slot数等于edit distance，并重算完整指纹。`hydra_transform.py` campaign的
  `_load_manifest`继续对任何带region schema的record强制调用该verifier，因此v2
  无旁路；ordinal篡改在执行候选前即被直接verifier和campaign gate共同拒绝；
- 验证与结果：2×1正例的flattened序列为左`add,mul`、右`add`，得到1 matched+
  1 deletion，edit distance 1；摘要前后对256个输入完全一致，lowered和symbolized
  IR通过LLVM verifier。Manifest ordinal与edit-distance篡改均拒绝，campaign可
  读取正例并拒绝tampered record。反向1×4正例把四个matched operation分布在右侧
  四块、左侧一块，block ordinal为`0,1,2,3`，再对256输入差分一致并通过符号化
  验证。1×5和internal conditional tree保持原CFG；F248/F268 safe/aggressive/
  denylist/v1 manifest回归、4项Hydra lit、LLVM18 `-Werror`、Python
  `py_compile`/Ruff和diff check通过；
- 科研边界：F272仍是两侧线性SESE arm，不处理internal branch tree、multiple
  merge、critical external predecessor、loop或exception。Verifier证明manifest
  edit script的有界规范与完整消费，但尚未独立解析bitcode重算LLVM
  `isSameOperationAs`兼容性或LCS最优性；这应由后续sealed independent replay补齐。
  Safe模式保持concrete语义不代表source-edge coverage不变，aggressive memory仍
  仅failure-preserving；所有公开评估必须original-authoritative replay并报告
  left/right block/instruction分布、edit distance、matched/extra ratio、compile/
  solver CPU、spurious failure和coverage/CPU。当前等级I/T/E。

## 268. F273：Proof-Carrying Bounded Internal-Tree Hydra Melding（2026-07-29）

- 研究问题：F272仍把每侧arm限制为无条件线性链，无法消除arm内部的昂贵symbolic
  branch。直接按basic-block顺序flatten内部树是不正确的：只在某条内部路径执行的
  instruction会被无条件求值，merge PHI也不再只有左右两个incoming。F273为无环
  branch tree显式构造路径谓词，并将operation alignment、路径ownership和多叶
  output恢复绑定到同一proof artifact；
- CFG接纳与预算：每侧最多7个tree block、3个internal conditional branch、4条到
  共同merge的leaf edge和64条非terminator instruction。根块必须由outer branch
  唯一进入；其余tree block必须有唯一parent，DFS preorder中每条child edge严格
  指向后续ordinal；树内不允许reconvergence、PHI、loop、call/invoke、EH/address-
  taken block或external predecessor。两侧所有leaf edge必须精确覆盖共同merge的
  predecessor集合。纯linear region仍保持F272的每侧4块上限；只有至少一侧含内部
  branch时启用7块tree预算。第4条内部branch、第5条leaf、tree内汇合、空alignment、
  proof identity碰撞和既有unsupported instruction均在修改IR前fail closed；
- Path-predicate lowering：outer true/false分别形成左右root guard。对child block
  递归生成`parent_guard && edge_condition`，对直接merge edge再合取terminator
  outcome，得到互斥且完备的leaf guard。Flattened compatible-LCS仍保持每侧DFS
  def-before-use顺序；matched ALU的每个operand由left guard、right guard和opcode-
  safe neutral三级select构造，extra ALU由自身block guard与neutral选择，从而不会
  在非活动路径消费潜在poison/div-zero/invalid shift operand。所有生成的select
  携`!symcc.hydra_select`，不重新产生独立path constraint；
- Memory与output语义：aggressive load仍可speculatively linearize，因此只具
  failure-preserving地位；store无论matched或extra都先读old value，再以精确block
  guard决定写入新值或old value。Matched同址tree store现显式计入
  `readback_stores`。每个merge PHI从typed zero开始，按左右canonical leaf顺序用
  leaf guard重建多路值；由于叶谓词互斥完备，恰有一个原incoming生效。最后outer
  branch改为unconditional，全部tree block一次性删除；
- v3证据：`bounded-internal-tree-hydra-v3`记录左右block/instruction/internal-
  branch/leaf数、`compatible-lcs-tree-preorder-left-tie-v3`、ordered block sites、
  alignment site/opcode/block ordinal，以及逐block的terminator site、parent、
  incoming edge、conditional/unconditional kind和block/merge successors；leaf数组
  绑定具体`(block ordinal, successor index)`。结构指纹混合上述拓扑、edit script
  与output PHI identity。独立verifier重建rooted-tree indegree/parent关系、preorder、
  branch/leaf预算和merge-edge集合，检查跨类别identity唯一、完整instruction消费、
  block单调性与fingerprint；campaign `_load_manifest`在执行候选前强制同一验签；
- 验证与结果：最小3×1树含1个内部condition与3路merge，lowered/original在256个
  输入上一致并通过symbolized verifier，parent和leaf篡改由direct verifier及
  campaign gate拒绝。最大7×7正例两侧各3个internal branch、4个leaf，8路PHI和4个
  aligned operation在256输入上一致；successor篡改拒绝。Aggressive双树在左右
  条件leaf执行同址matched store，guarded readback后对256输入一致，manifest正确
  标记original failure replay。第4 branch、empty alignment和tree内reconvergence
  保持原CFG。6项Hydra定向lit、LLVM18 `-Werror`、Python `py_compile`/Ruff与diff
  check通过；
- 科研边界：这是single-entry、共同single-merge的无汇合branch tree，不是任意
  acyclic SESE DAG；不支持tree-local PHI、critical external predecessor、multiple
  merge、loop、exception或call。Verifier独立验证manifest规范，但尚未从sealed
  input bitcode重建LLVM compatibility、DFS tree和实际rewrite；F274统一seal/replay
  将补齐该信任边界。Safe ALU只保证defined execution的concrete值，edge coverage
  仍必须由原程序测量；aggressive memory仍以原程序failure replay为唯一权威。
  当前等级I/T/E。

## 269. F274：Unified Sealed Independent Transformation Replay（2026-07-29）

- 研究问题：F264/F265只封存continuation，并从已经lowered的IR重做MemorySSA/AA
  语义证明；loop recurrence/exit与Hydra topology虽有独立manifest verifier，
  却没有统一绑定输入IR、实际lowered IR、pass binary和LLVM工具，更没有从原输入
  独立重跑完整变换。仅验证manifest会留下“证明记录正确但实际rewrite不同”的信任
  缺口。F274把continuation、loop和Hydra纳入同一write-once envelope，并把exact
  transformation replay作为共同验收条件；
- 统一schema与配置恢复：`symcc-transformation-seal-v1`按`continuation`、`loop`、
  `hydra`区分pipeline，`--manifest KIND=PATH`显式声明
  `continuation`、`loop-recurrence`、`loop-exit`或`hydra`。每份清单先调用其独立
  verifier，再绑定有序record数、`proof_fingerprint`或
  `structure_fingerprint`、文件size/SHA-256。Hydra从record恢复唯一site及
  safe/aggressive mode；loop恢复实际存在的recurrence/exit输出集合；
  continuation从analysis集合推导memory开关，全`structural-only-v1`精确重放
  memory-off，任一memory proof则重放memory-on；
- 原子封存：envelope同时绑定input IR、textual lowered IR、compiler plugin和
  `opt`的size/SHA-256，以及有界`opt --version`身份和canonical JSON SHA-256。
  Manifest在独立验签前后各取artifact快照，文件变化即拒绝。输出使用POSIX
  `flock`、fresh-output gate、同目录临时文件、file fsync、atomic rename和
  directory fsync；重复封存不会覆盖既有证据；
- 独立重放：`replay_transform_artifact.py`先完整重验seal，清除父进程遗留的全部
  `SYMCC_HYDRA*`、`SYMCC_IFSS_LOOP*`和`SYMCC_IFSS_CONTINUATION*`变量，再用seal绑定
  的`opt`与plugin从原input IR独立执行精确pass pipeline。新进程输出临时manifest
  与lowered textual IR；所有manifest重新独立验签并要求JSON record逐项完全相等，
  lowered IR要求size/SHA-256完全相等，且必须通过独立LLVM `verify`。结束前再次
  重验外部seal与制品，缩小重放期间TOCTOU窗口。子进程有120秒上限，失败输出限制为
  最后8 KiB；
- 验证与结果：continuation覆盖nested-MemoryPhi memory-on以及
  `structural-only-v1` memory-off；loop同时封存upper-triangular recurrence与
  three-break exit truth table；Hydra封存最大7×7 internal tree。三类正例均完成
  seal→verify→input-IR independent replay。每类又构造LLVM-verifier-clean的lowered
  IR语义篡改，给篡改产物重新生成合法seal后，仍因独立重跑结果不一致而拒绝；
  另覆盖seal字段篡改和重复output拒绝。4项定向lit、Python `py_compile`/Ruff与
  diff check通过；最终`ninja -C build check`为155/155通过（97.79秒）；
- 科研边界与暂停状态：这是确定性、同compiler/LLVM版本、local POSIX filesystem
  上的exact replay，不是数字签名、远端透明日志、可信执行环境或跨LLVM版本语义
  等价证明。当前输出固定为`opt -S` textual IR，因此编译命令、pass顺序和IR打印
  必须可重现；公开coverage仍以original binary为权威，公开等CPU多轮campaign尚未
  执行。按F274完成时的开发指令，该阶段曾在验证与文档后暂停；byte-lane
  constant-offset部分随后由F275恢复开发，symbolic alias partition、
  general-memory loop和general SESE DAG仍未实现。当前等级I/T/E。

## 270. F275：Proof-Carrying Continuation Byte-Lane Partial Overlap（2026-07-30）

- 研究问题：F262/F269/F270把一个continuation memory live-out视为单个等宽标量，
  因而遇到编译器常见的宽store后局部byte覆盖，或只写部分byte而其余byte继承函数
  初始内存时会整体回退。仅依赖AA的`PartialAlias`结论不足以恢复值：必须证明每个
  load byte的最近写者、覆盖顺序和端序，并把实际重建IR纳入独立replay；
- 接纳算法与边界：仅处理2--8 byte、byte-width整数load。旧v1--v3证明优先；任一
  相关exit失败后，所有相关exit统一重跑v4。每条路径逆向扫描最多16个线性MemoryDef，
  用`GetPointerBaseWithConstantOffset(..., AllowNonInbounds=false)`把load/store约化为
  同一base上的常量区间；最新store先占据尚未赋值的address-order lane。Store必须为
  1--8 byte simple integer且value/store支配capture；非重叠definition只有AA证明
  NoMod时才能跳过，仍受8项预算。到达LiveOnEntry后用初始内存补齐剩余lane。每条
  相关路径必须完整覆盖且至少含一个store。动态/未知offset、MemoryPhi、MayAlias/Mod、
  volatile/atomic/fence、undef/poison、non-inbounds和超预算均在修改IR前fail closed；
- Lowering与端序：每个LiveOnEntry路径只建立一个capture-local snapshot。每条lane从
  store operand或snapshot按source-byte提取i8，zero-extend后按destination lane定位，
  再以确定顺序OR成原load宽度。Bit shift由DataLayout决定：little endian使用
  `8*byte`，big endian使用`8*(width-1-byte)`。Dispatch PHI仍对无关destination使用
  typed zero，原load保留但data uses改接重建值；
- 证明、清单与独立复检：v4 metadata schema为
  `byte-lane-continuation-memory-tuple-v4`，逐exit绑定byte width、endianness、NoMod
  site和规范lane向量`(ordinal, source-kind/site, source-byte, source-width)`。
  Manifest analysis为`llvm-memoryssa-aa-byte-lane-revalidated-v4`，完整字段进入FNV
  proof fingerprint。Python verifier检查lane覆盖、source byte唯一性、LiveOnEntry
  规范映射、端序和指纹。编译器replay重新执行MemorySSA/AA，并反向解析实际OR、
  shl、zext、lshr、trunc和snapshot链；因此正确metadata不能掩盖错误重建值。F264/
  F265 continuation seal及F274统一input-IR exact replay无需新旁路即可消费v4；
- 验证与结果：little-endian正例同时覆盖`i16 store + high i8 overwrite`和
  `low i8 store + LiveOnEntry high byte`，变换前后256个selector完全一致，symbolized
  IR通过LLVM verifier，原生`C`种子生成`B` Backsolver模型。V4清单验签、lane篡改
  拒绝、continuation与unified seal/replay均通过；把lowered IR中source store从51
  改为52后，IR仍verifier-clean且可重新seal，但独立replay拒绝。Big-endian专项验证
  相反bit位置。动态offset、volatile partial store和nested MemoryPhi partial store
  保持原load。V1--v3兼容回归与新测试合计6/6通过，LLVM18 `-Werror`编译通过；
  最终全量lit为157/157、Python为459/459通过；
- 科研边界：F275是bounded constant-offset linear MemorySSA byte composition，
  不是symbolic alias partition、cyclic/loop-carried MemoryPhi、memcpy/aggregate
  region summary或跨函数memory graph。当前证据为I/T/E；公开目标上的接纳率、
  编译开销、solver CPU与coverage/CPU提升尚须F67等CPU多轮confirmatory campaign。

## 271. F276：Guarded Two-Arm Symbolic Alias Partition（2026-07-30）

- 研究问题：F275要求store pointer直接约化到load base常量区间，因而编译器把
  `condition ? overlapping_ptr : disjoint_ptr`保留为pointer select时，AA只能给出
  MayAlias并整体回退。直接忽略MayAlias不正确；必须证明selector两臂互斥完备、逐臂
  地址效果，以及未命中臂应回退到哪一个older lane source；
- 接纳与证明：一个相关路径最多允许一个simple integer store以pointer-valued
  `select i1`寻址。Guard必须是有stable site的instruction并支配capture。两个pointer
  arm分别经inbounds constant base+offset分类：same-base overlap精确得到目标lane和
  source byte，不同base必须由AA独立证明NoAlias。同一lane若两臂都overlap，只在
  source byte相同时折叠为unconditional store；恰一臂overlap则记录guarded overlay，
  并继续逆向MemorySSA寻找older store/LiveOnEntry fallback。未知臂、第二个guarded
  store、argument-only guard、PHI pointer、MemoryPhi、volatile/atomic及预算超限均
  fail closed；
- Lowering与replay：先按F275恢复base byte，再提取guarded store byte，以pointer
  select的相同condition和polarity建立i8 select，最后zext/position/OR。V5 schema
  `guarded-byte-lane-continuation-memory-tuple-v5`在v4 lane后增加guard presence、
  guard site、polarity、store site、source byte/width；manifest analysis为
  `llvm-memoryssa-aa-guarded-byte-lane-revalidated-v5`。Python verifier约束同一
  path只使用一个guard/store identity、source-byte唯一性与fingerprint。独立LLVM
  replay重新做逐臂AA/interval分类，并核对实际select condition、arm顺序和extract链；
- 验证与结果：正例用`select guard, slot+1, local-disjoint`条件覆盖i16高byte，
  两个continuation exit统一进入v5；原/变换IR对256×4=1024个输入一致，symbolized
  verifier通过，`C 01`生成`A 01` Backsolver模型。Continuation及F274 unified
  seal/replay均通过；guard polarity manifest tamper拒绝，把lowered select中的51
  改为52后IR仍verifier-clean且可fresh seal，但独立replay拒绝。未知MayAlias arm、
  同路径双guarded store和无stable-site argument guard保持原load。两项定向lit通过，
  LLVM18 `-Werror`与Python `py_compile`通过；最终全量lit为159/159、Python为
  459/459通过；
- 科研边界：这是一个store、两个完备select arms、最多一个guard layer的finite alias
  partition，不支持多层select/PHI points-to、symbolic offset interval、多个guarded
  writes的优先组合、cyclic MemoryPhi或general heap graph。当前等级I/T/E，仍需公开
  target统计alias形态接纳率及等CPU coverage收益。

## 272. F277：Proof-Carrying Cyclic Byte-Lane Continuation MemoryPhi（2026-07-30）

- 研究问题：F275/F276只能沿无环MemoryDef链恢复continuation load。若共同循环前缀
  在preheader写入宽值、在latch只覆盖部分byte，则capture处的MemorySSA状态是一个
  loop-carried MemoryPhi；把它当无环provenance会递归回自身，把它当最后一次store又
  会丢失零次迭代和未写lane。F277把实际值提升为address-order byte vector fixed
  point。它与F151的definedness sidecar循环状态不同：这里重建的是F260 dispatch
  所消费的具体整数值，并必须从lowered LLVM PHI表达式独立重放；
- Canonical cycle proof：仅接受2--8 byte simple integer load和一个两输入MemoryPhi。
  Phi block必须支配capture，且恰有一个不被header支配的entry predecessor和一个被
  header支配的backedge predecessor；两者都必须以唯一无条件边进入header。Entry
  使用F275的same-base inbounds constant-offset/LiveOnEntry byte proof。Backedge
  从incoming MemoryAccess逆向扫描最多16个definition直到同一MemoryPhi：simple
  integer store按最近写者占据lane，AA-proven NoMod最多跳过8项，到phi时所有未写
  lane标记为self-carry。至少一个lane来自store且至少一个来自carry。外层capture到
  cycle之间也只允许8项NoMod；
- Lowering与执行次序：先在entry predecessor末尾生成entry byte composition，再在
  原循环header建立`ifss.cont.memory.cycle`整数PHI。Latch末尾从该PHI提取carry
  byte、从本轮store operand提取更新byte，按DataLayout端序执行zext/shift/OR，并把
  结果接回PHI backedge。循环退出后，F260 capture不再读取内存来恢复数据，而是把
  已随实际迭代演化的cycle PHI送入dispatch memory tuple；原load仍执行，其data uses
  改接dispatch PHI。零次迭代选择entry值，任意正迭代逐轮应用同一lane transfer；
- Proof、manifest与独立重放：v6 metadata schema为
  `cyclic-byte-lane-continuation-memory-tuple-v6`，每个exit先区分linear/cyclic
  byte state。Cyclic record绑定byte width、endianness、capture-prefix NoMod、
  header/entry/backedge terminator site、完整entry lane state、backedge NoMod和
  逐lane`store/carry` source。Manifest analysis为
  `llvm-memoryssa-aa-cyclic-byte-lane-revalidated-v6`；全部拓扑与source字段进入FNV
  fingerprint。Python verifier要求v6至少含一个真实cycle、三类拓扑site互异、
  entry规范覆盖，以及backedge同时含store和canonical self-carry。编译器replay
  重新构造MemorySSA/AA候选，并解析实际header PHI、incoming block、entry/backedge
  extract/zext/shift/OR链、carry对同一PHI的引用和proof metadata identity；
- 验证与结果：little-endian正例以i16 entry store和latch high-byte store形成
  `low=carry, high=store`，同一destination另一exit为linear byte state；原始/改写
  IR对256个selector乘4个iteration值共1024组一致，symbolized IR通过LLVM verifier，
  `C 01`可生成`A`且iteration非零的Backsolver模型。V6 manifest、continuation seal
  和F274 unified seal/replay通过；把carry伪造成store，以及把lowered backedge常量
  51改为52后fresh seal，均被独立验证拒绝。Big-endian专项证明carry lane先右移8位
  再放回高位。Conditional backedge MemoryPhi、动态offset、volatile store、
  full-width无carry transfer和三个incoming/multiple-backedge保持原load。三项F277
  lit通过，v1--v6 continuation-memory兼容回归11/11；LLVM18 `-Werror`构建、
  Python `py_compile`和diff check通过；最终全量lit为162/162、Python为459/459；
- 科研边界：这是single-header、single-entry、single-latch、无条件partial update的
  scalar integer fixed point，不是一般MemorySSA循环求解。Conditional/多latch写、
  nested cycle、pointer-select回边、symbolic offset、full-width loop selection、
  memcpy/aggregate、atomic/volatile、跨调用heap graph和一般loop nest仍fail closed。
  当前等级I/T/E；公开target上的cycle形态接纳率、编译开销、solver CPU和等CPU
  coverage收益尚未形成R级结论。

## 273. F278：Bounded Multi-Level Finite Pointer-Union Partition（2026-07-30）

- 研究问题：F276只能解释`select guard, overlap, noalias`一个二分pointer domain。
  优化器常把多个地址候选保留为嵌套select；若只看root，两臂本身仍是symbolic pointer，
  AA返回MayAlias。F278把一个store的pointer operand展开成有界完备二叉决策树，并对
  每个leaf独立证明其对每个load lane的作用；
- Candidate proof：仅接受深度不超过2、2--3个内部select、3--4个leaf的unique-node
  tree。每个condition必须是有stable site且支配capture的i1 instruction；共享select
  DAG拒绝，保证artifact是canonical tree。每个leaf通过F276同一分类器成为same-base
  inbounds constant interval或AA NoAlias，并记录每个lane的source byte或fallback。
  整棵树至少有一个overlap，且每个load lane至少有一个fallback leaf，保证older
  F275 source实际出现在表达式中并可独立验证。一个path仍只允许一个partition store；
- Lowering与proof：先恢复每个lane的older store/LiveOnEntry byte，再按原pointer
  tree自底向上建立i8 select；overlap leaf提取当前store byte，NoAlias leaf复用base
  byte，最后才执行端序定位。V7 schema
  `finite-pointer-union-continuation-memory-tuple-v7`区分linear/partitioned exit，
  绑定store site/width、preorder node的select/guard/child identity和每个leaf的完整
  lane-source vector。Analysis为
  `llvm-memoryssa-aa-finite-pointer-union-revalidated-v7`。编译器replay递归解析实际
  select tree和每个leaf expression；Python verifier重建single-parent tree、
  preorder/depth/leaf覆盖及fingerprint；
- 验证与结果：3-node/4-leaf正例实现两个guard的XNOR地址选择，高byte在两个leaf
  overlap、两个leaf fallback；原/变换IR对256×2×2=1024组一致，symbolized verifier、
  cross-continuation Backsolver模型、continuation与unified seal/replay通过。Child
  topology tamper和fresh-sealed leaf 51→52篡改拒绝。Depth-3、未知MayAlias leaf、
  shared select DAG和同路径两个partition store保持原load。两项定向lit通过；
- 全量门禁：F278/F279合入后的统一门禁为LLVM/lit 165/165（113.43 s）及Python
  459/459（105.892 s）；LLVM 18 Werror构建、Python `py_compile`、技术索引
  `--check`与`git diff --check`同时通过；
- 科研边界：这是一个store、最多四个pointer leaves的finite union，不是points-to
  fixed point、symbolic index interval、pointer PHI、循环pointer state或多个union
  writer组合。每lane fallback要求会保守拒绝“所有leaf都写该lane”的可简化情形，
  目的是让base provenance仍能从actual IR重放。当前等级I/T/E。

## 274. F279：Two-Write Guarded Priority Composition（2026-07-30）

- 研究问题：多个conditional writes按MemorySSA程序顺序覆盖同一byte时，不能把guard
  独立OR，也不能任意排序select。正确关系是
  `new_guard ? new_byte : (old_guard ? old_byte : base_byte)`；尤其两个guard同时成立时
  必须选择后写值。F279把F276单overlay扩展为最多两条guarded store；
- 分析与表示：反向MemorySSA扫描按newest-to-oldest遇到store，并给store分配priority
  0/1。每条lane只记录实际影响它的overlay，所以允许稀疏priority；每条overlay仍需
  两pointer arms完备分类、instruction guard支配和source byte精确。第三条guarded
  store、与F277 cycle或F278 pointer tree混用、未知arm及预算超限fail closed；
- Lowering、proof与replay：lane从base开始按oldest-to-newest构造select，因此最终
  IR外层一定是priority 0最新writer。V8
  `guarded-write-priority-continuation-memory-tuple-v8`逐lane记录0--2个有序
  `guarded_sources`，每项含priority、guard/polarity、store和source byte/width；
  analysis为`llvm-memoryssa-aa-guarded-write-priority-revalidated-v8`，全部字段进入
  fingerprint。Replay从外到内按newest-to-oldest剥离实际select，逐层核对condition、
  polarity和store operand，最后才验证F275 base；
- 验证与结果：old store写34、new store写51到同一high byte；四个guard组合分别产生
  base/old/new/new，原/变换IR对256×2×2=1024组一致，symbolized verifier、两套
  seal/replay和`C 01 01`到`A`且new guard成立的Backsolver模型通过。Priority tamper
  和fresh-sealed outer 51→52篡改拒绝。原F276单overlay继续输出v5；旧双store拒绝
  用例提升为第三store拒绝。F276/F279三项兼容lit通过；F278/F279合入后的统一门禁
  为LLVM/lit 165/165（113.43 s）及Python 459/459（105.892 s）；
- 科研边界：当前只处理同一路径最多两条single-level two-arm guarded stores。三写、
  nested pointer-union writer、跨MemoryPhi priority、循环conditional writes及
  symbolic offset不支持。Priority证明的是MemorySSA顺序，不是源语言并发happens-
  before。当前等级I/T/E，公开程序形态分布和coverage/CPU收益仍待R级campaign。

## 275. F280：Conditional Cyclic Byte-Lane Transfer（2026-07-30）

- 研究问题：F277只能把无条件latch上的partial store建模为循环状态转移，真实LLVM
  常以`branch -> {write, carry} -> latch`的MemoryPhi表达条件更新。直接把write臂
  当作必写会在guard为假时制造错误值；把两臂简单合并又会丢失后写与self-carry关系；
- 有界候选：F280只接纳single-header、single-entry、single-backedge且回边为严格
  两臂diamond的规范子集。Latch MemoryPhi必须恰有两个incoming：write臂沿有界
  MemoryDef链回到header MemoryPhi，carry臂直接引用同一header MemoryPhi。两臂都
  只有一个共同的条件分支前驱，并各自以唯一无条件边进入latch；guard必须是具有
  stable site的i1 instruction并支配latch。Write链继续使用same-base inbounds
  constant-offset、AA、16-definition和8项NoMod边界；必须同时存在store lane与carry
  lane；
- Lowering与独立重放：原header建立递归integer PHI。未覆盖lane直接提取PHI byte；
  覆盖lane在latch生成
  `guard ? stored_byte : carried_byte`，false polarity时交换两臂，然后按DataLayout
  端序执行zext/shift/OR。V9
  `conditional-cyclic-byte-lane-continuation-memory-tuple-v9`在v6基础上绑定
  branch、write-arm、carry-arm、guard site与store polarity；analysis为
  `llvm-memoryssa-aa-conditional-cyclic-byte-lane-revalidated-v9`。这些字段进入
  metadata、manifest和fingerprint。编译器从input IR重新推导严格MemorySSA拓扑，
  并解析actual PHI与guarded byte select；Python verifier检查七个站点互异、极性、
  canonical carry、完整lane覆盖和指纹；
- 验证与结果：little-endian正例覆盖256个selector、两种iteration和两种write值，
  共1024组原/变换IR语义差分；symbolized LLVM verifier、Backsolver跨continuation
  模型、continuation与统一双seal/replay均通过。Manifest polarity tamper与把actual
  selected byte 51改成52后fresh seal均被拒绝。Big-endian false-polarity专项验证
  反向lane定位。参数型无site guard、carry臂第二writer、write臂未知ModRef、非严格
  join、动态offset、volatile、full-width无carry及multiple backedge全部保持原load；
  同一slot混合v6无条件cycle与v9条件cycle也因proof layout不唯一而整体拒绝。
  v1--v9 continuation-memory兼容回归16/16通过；最终统一门禁为LLVM/lit
  167/167（114.18 s）和Python 459/459（107.558 s），LLVM 18 Werror构建、
  `py_compile`、技术索引与diff检查通过；
- 科研边界：这不是多latch、嵌套循环或一般MemorySSA fixed point。多回边、两臂都写、
  多级conditional transfer、pointer union、symbolic offset、aggregate、atomic、
  exception edge和跨调用heap state仍fail closed。当前等级I/T/E；公开target接纳率、
  solver CPU与coverage收益仍需等CPU多轮实验。

## 276. F281：Two-Latch Cyclic Byte-Lane MemoryPhi（2026-07-30）

- 研究问题：多回边自然循环的MemorySSA header PHI会直接包含多个latch incoming。
  把它们人为排序为select既无必要又可能改变路径语义；正确的scalar fixed point应
  保留LLVM PHI按实际predecessor选择incoming的规则；
- 有界候选：F281接纳恰好三个incoming的header MemoryPhi：一个唯一域外entry和
  两个唯一、受header支配的域内latch；三者都必须以无条件边进入header。Entry继续
  使用F275完整byte proof。每个latch独立在最多16个MemoryDef内回溯到同一header
  MemoryPhi，只允许same-base inbounds constant-offset simple stores和最多8项NoMod；
  每个transfer都必须同时含至少一个store lane与一个canonical self-carry lane。
  任一latch失败即拒绝整个slot；
- Lowering、proof与replay：在原header建立entry加两个latch incoming的三输入
  `ifss.cont.memory.multi.cycle` integer PHI；每个latch在自己的terminator前按端序
  生成独立extract/zext/shift/OR transfer，不引入虚构priority。V10 schema为
  `multi-latch-cyclic-byte-lane-continuation-memory-tuple-v10`，analysis为
  `llvm-memoryssa-aa-multi-latch-cyclic-byte-lane-revalidated-v10`。Proof、
  manifest和fingerprint绑定header/entry、ordered两个backedge site、每条NoMod链
  与完整lane vector。Compiler replay核对actual三输入PHI、incoming block和两个
  self-reference表达式；Python verifier要求两个canonical backedge record、四个
  topology site互异且每条transfer为partial store/carry；
- 验证与结果：little-endian正例按iteration parity在两个latch分别写低字节51和
  高字节68，原/变换IR对256×4=1024组一致；symbolized verifier、Backsolver模型、
  continuation与unified seal/replay通过。Manifest transfer-kind tamper及把第二latch
  actual byte 68改69后fresh seal均被拒绝。三个latch、任一full-width无carry latch、
  latch内nested conditional MemoryPhi保持原load；v1--v10兼容18/18通过；
- 最终门禁：F282合入后包含F281的统一门禁为LLVM/lit 171/171（113.52 s）及
  Python 459/459（106.658 s）通过；
- 科研边界：这是恰好two-latch、unconditional edge、independent partial transfer
  的规范子集，不支持三个以上latch、conditional latch与multi-latch组合、嵌套循环、
  irreducible CFG、symbolic pointer/offset、aggregate、atomic、异常边或一般heap
  fixed point。当前等级I/T/E，尚无公开target的接纳率和等CPU收益结论。

## 277. F282：Pointer-Union Writer Priority Composition（2026-07-30）

- 研究问题：F278能表达一个最新pointer-union writer，F279能表达两条single-level
  guarded writer，但两者此前互斥。真实MemorySSA链可能先有一条条件写，随后由
  nested pointer select选择地址并再次写同一byte。正确的lane关系是
  `partition_leaf_overlap ? union_byte : (old_guard ? old_byte : base_byte)`；
  若把旧guard与pointer tree并列或改变次序，两个条件同时命中时会破坏最近写者语义；
- 有界接纳与fail-closed边界：F282只接纳同一路径恰好一个F278 depth-two finite
  pointer-union store作为最新相关writer，以及至多一个更旧的F276 single-level
  guarded store。逆向MemorySSA必须先遇到partition，再遇到旧guard；partition之后
  出现更新的guard、两个旧guard、第二个partition、未知MayAlias leaf、共享select
  DAG、动态offset、MemoryPhi、volatile/atomic或预算超限均拒绝整个slot。所有guard
  仍须是有stable site、支配capture的i1 instruction；每个leaf仍须独立证明same-base
  constant interval或AA NoAlias；
- Lowering与实际表达式：先按F275恢复base byte，再应用旧guard得到
  `ifss.cont.byte.alias`，最后按F278的canonical pointer tree自底向上建立
  `ifss.cont.byte.partition`。因此overlap leaf取最新union store byte，fallback leaf
  精确复用已包含旧writer优先关系的alias byte。每个lane完成后才按DataLayout端序
  zext/shift/OR，不把pointer决策或writer次序降格为无序集合；
- Proof、manifest与独立重放：v11 schema为
  `pointer-union-priority-continuation-memory-tuple-v11`，analysis为
  `llvm-memoryssa-aa-pointer-union-priority-revalidated-v11`。记录同时包含v5式
  base-lane guarded source和v7式partition store、preorder node/child identity及
  leaf lane vector；全部字段进入fingerprint。Python verifier要求至少一个
  partitioned exit确实带guarded fallback，重建单父树、depth/leaf覆盖、polarity与
  指纹。Compiler replay重新执行MemorySSA/AA，先从actual fallback剥离guarded i8
  select，再递归核对pointer tree的condition、arm顺序、selected byte和base引用；
- 验证与结果：正例构造3-node/4-leaf XNOR pointer tree覆盖i16高byte，并在它之前
  放置一条旧guarded writer。原始/变换IR对128个selector、两个pointer guards、一个
  old guard共1024组输入语义一致；symbolized LLVM verifier、Backsolver跨
  continuation模型、continuation与unified两套seal/replay均通过。Manifest中旧guard
  polarity篡改被fingerprint拒绝；把actual最新selected byte从51改为52后即使重新
  seal，独立replay仍拒绝。反向写入次序、两个旧guarded writer，以及同一slot其他
  exit使用F279双guard priority而造成v8/v11 schema冲突时均保留原load。
  `backsolver_continuation_pointer_union_priority.ll`及
  `backsolver_continuation_pointer_union_priority_reject.ll`两项定向lit通过；
  v1--v11 continuation-memory兼容回归20/20通过；
- 全量门禁：LLVM/lit 171/171（113.52 s）与Python 459/459（106.658 s）通过；
  LLVM 18构建、Python `py_compile`、技术索引生成/`--check`与diff检查通过；
- 科研边界：这是“一条最新depth-two union writer + 一条旧single-level guarded
  writer”的固定上界，不支持union作为旧writer、两个union、多个旧guard、pointer
  PHI、symbolic index interval、循环pointer state、跨MemoryPhi priority、aggregate、
  atomic或一般heap graph。当前等级I/T/E；公开target上的形态接纳率、编译成本、
  solver CPU和coverage/CPU收益仍须等CPU多轮campaign。

## 278. F283：Conditional Multi-Latch Cyclic Byte-Lane MemoryPhi（2026-07-30）

- 研究问题：F280能证明一个conditional latch，F281能证明两个unconditional
  latch，但二者此前不能组合。真实natural loop可由多个latch回到同一header，且每个
  latch可能独立选择partial write或self-carry。人为给latch排序会改变LLVM PHI的
  predecessor语义；把conditional transfer降为必写又会在guard为假时制造错误值；
- 有界候选与语义：F283保留F281“一个域外entry、两个域内backedge、实际三输入
  MemoryPhi”的外层契约。每个backedge transfer可以是F281无条件partial
  store/carry，也可以是F280严格write/carry diamond：latch MemoryPhi恰有两个
  incoming，一个沿有界MemoryDef链回到header，另一个直接引用header；共同
  conditional branch的两臂各有唯一前驱并以无条件边汇入latch。两个transfer中至少
  一个必须conditional，且每个transfer仍必须同时含store lane和canonical carry
  lane。Guard必须是有stable site、支配latch的i1 instruction；同一支配比较可以合法
  复用于两个latch。Argument-only guard、两臂都写、未知ModRef、三latch、动态offset、
  full-width无carry、volatile/atomic和非严格join全部fail closed；
- Lowering与proof：header建立entry加两个latch incoming的
  `ifss.cont.memory.multi.cycle` integer PHI。每个latch只生成自己的transfer：
  unconditional lane直接选择store/carry，conditional store lane生成
  `guard ? stored : carried`或反极性版本；随后按DataLayout端序执行
  extract/zext/shift/OR，不引入跨latch priority。V12 schema为
  `conditional-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v12`，
  analysis为
  `llvm-memoryssa-aa-conditional-multi-latch-cyclic-byte-lane-revalidated-v12`。
  每条backedge记录`transfer_kind`；conditional记录额外绑定branch、store arm、
  carry arm、guard site和polarity。Metadata、manifest和fingerprint保持v10全
  unconditional layout的字节兼容，仅v12加入逐transfer presence tag；
- 独立验证与一致性修复：compiler replay重新执行MemorySSA/AA，核对actual三输入
  recursive PHI，并对每个incoming分别解析普通composition或guarded i8 select。
  Python verifier要求两个ordered transfer、唯一CFG block topology、完整lane覆盖、
  至少一个conditional transfer和完整fingerprint。验证器只要求CFG block site互异，
  不再错误要求两个latch的data guard site互异，从而与LLVM中共享dominant guard的
  合法表达一致；
- 自动化证据：little-endian混合conditional/unconditional正例对256个selector、
  两种iteration和两种write值共1024组原/变换IR语义一致，并通过symbolized LLVM
  verifier、Backsolver跨continuation模型、continuation/unified双seal/replay、
  polarity篡改和fresh-sealed selected-byte 51→52篡改拒绝。PowerPC64 big-endian
  专项让两个conditional latch共享一个guard、使用相反store polarity并分别写数值
  low/high byte，manifest与独立seal/replay通过。专用拒绝测试确认前序continuation
  lowering已经发生，而argument guard、双writer arm和write-arm未知ModRef仍保留
  original load。三项F283定向lit通过；
- 完整门禁：continuation-memory v1--v12兼容组23/23通过；LLVM 18 Werror构建与
  LLVM/lit 174/174（113.11 s）、Python unittest 459/459（106.166 s）通过；
  `py_compile`、技术索引`--check`与`git diff --check`同时通过。环境未安装
  `clang-format`可执行文件，故未把格式工具写成已执行证据；
- 兼容与科研边界：v12允许同一slot的其他exit使用v10形态，因为v12为每条transfer
  显式编码conditional presence；旧v1--v11 proof保持原布局。这仍是恰好two-latch、
  strict single-diamond、constant-offset integer byte-lane fixed point，不是任意
  predicate、任意latch数、nested/irreducible loop、pointer-PHI、symbolic-address
  array theory、aggregate、exception edge或一般heap fixed point。当前等级I/T/E；
  公开target接纳率、编译成本、solver CPU与等CPU coverage收益尚无R级结论。

## 279. F284：Bounded 2--4-Latch Cyclic Byte-Lane MemoryPhi（2026-07-30）

- 研究问题：F281/F283把multi-latch header固定为两个backedge。优化后的switch loop、
  状态机和多continue循环可自然形成三个或四个latch；将这些incoming压成一串select
  会伪造writer顺序，而逐个拒绝会丢失LLVM PHI已经明确给出的互斥predecessor语义；
- 有界分析：F284把cycle header上限提升为一个域外entry加2--4个域内latch，即实际
  MemoryPhi共3--5个incoming。每个backedge仍必须无条件进入header，并独立通过F281
  partial-store/self-carry或F283 strict conditional write/carry证明；所有地址仍为
  same-base inbounds constant interval，MemoryDef/NoMod、byte width和AA边界不变。
  任一transfer失败即拒绝整个slot，5个latch、动态offset、full-width无carry、
  unknown ModRef、volatile/atomic、nested/irreducible loop继续fail closed；
- 兼容schema：恰好两个全无条件latch继续输出v10，恰好两个且至少一条conditional
  继续输出v12。只有某相关exit出现3--4个latch时，slot才升级为v13
  `bounded-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v13`，analysis为
  `llvm-memoryssa-aa-bounded-multi-latch-cyclic-byte-lane-revalidated-v13`。
  v13逐transfer无条件写出presence tag和`transfer_kind`，因此3-latch全无条件状态
  也有唯一proof layout。同一slot其他相关exit可有2--4条transfer，但verifier要求
  至少一个状态实际超过2条，防止纯two-latch制品绕过v10/v12 canonical schema；
- Lowering、proof与重放：header integer PHI的incoming数严格等于
  `1 + transfer_count`；entry使用F275 byte composition，每个latch只物化自己的
  store/carry或guarded-store/carry expression。Metadata、manifest和fingerprint绑定
  实际MemorySSA incoming顺序、所有backedge和conditional topology。Compiler replay
  核对actual N-input recursive PHI和每条表达式；Python verifier检查2--4 canonical
  ordinals、互异CFG block topology、可共享的dominant data guard、lane completeness、
  v13 presence tags及完整fingerprint；
- 自动化证据：4-latch little-endian正例含conditional/unconditional/conditional/
  unconditional四条transfer、共享guard和相反polarity，对256个selector、5种
  iteration和2种write值共2560组原/变换IR语义一致；symbolized LLVM verifier、
  Backsolver跨continuation模型、continuation/unified双seal/replay、ordinal tamper
  及fresh-sealed selected byte 81→82篡改拒绝通过。PowerPC64 big-endian 3-latch
  全无条件正例证明v13仍为三条transfer逐条编码presence/tag并通过独立replay；5-latch负例
  确认前序continuation lowering已发生但original load保留。三项F284定向测试通过；
- 完整门禁：continuation-memory v1--v13兼容组26/26通过，其中4-latch穷举正例
  使兼容组耗时67.75秒；LLVM 18构建、LLVM/lit 177/177和Python unittest
  459/459（107.757秒）通过。Python `py_compile`、技术索引`--check`及
  `git diff --check`同时通过。当前环境没有`clang-format`可执行文件，故未把格式化
  工具列为已执行证据；
- 科研边界：2--4是显式proof budget，不是任意latch fixed point；transfer仍是单条
  linear partial-store链或一个strict diamond，不支持nested predicate tree、
  pointer PHI、symbolic-index array、跨迭代alias集合、aggregate、exception edge、
  loop nest或一般heap graph。当前等级I/T/E，公开程序形态接纳率和等CPU收益尚未
  达到R级证据。

## 280. F285：Nested-Predicate Cyclic Byte-Lane Transfer Tree（2026-07-30）

- 研究问题：F280/F283/F284的conditional transfer只允许一层strict diamond，
  即一个store叶和一个carry叶。真实状态机、解析循环和多级过滤常在同一latch前
  形成嵌套条件；把叶子MemorySSA状态伪装成有顺序的writer会改变互斥路径语义，
  把所有guard提前合取又可能改变LLVM poison/undef与按路径求值语义；
- 有界模型：F285接纳每个backedge最多3个内部`br i1`节点、3--4个叶子的
  unique-parent full binary tree，并允许一个header有1--4个latch。每个叶子必须
  无条件汇入对应latch；叶子MemorySSA状态只能是纯self-carry，或经最多16个定义、
  最多8个NoMod节点回溯到header MemoryPhi的same-base inbounds constant-offset
  partial-store/carry组合。至少一个叶子必须实际写入；5叶、共享DAG、switch节点、
  full-width无carry、动态地址、unknown ModRef、volatile/atomic均fail closed；
- 保语义lowering：编译器不把树条件改写为eager布尔式，而是在每个原叶子块中按
  DataLayout恢复完整整数值，在latch建立与实际MemoryPhi incoming顺序一致的
  3/4-input `ifss.cont.memory.predicate.cycle` PHI，再把它作为header递归PHI的
  对应incoming。因此叶子互斥性由LLVM predecessor语义决定，并可让不同叶子更新
  不同byte lane。单回边和2--4回边可共用同一表示，其他回边仍可为ordinary或
  F283 conditional transfer；
- proof-carrying artifact：新增schema
  `nested-predicate-cyclic-byte-lane-continuation-memory-tuple-v14`与analysis
  `llvm-memoryssa-aa-nested-predicate-cyclic-byte-lane-revalidated-v14`。
  每条transfer编码`unconditional`、`conditional`或`predicate-tree` kind；
  tree记录preorder node ordinal、branch/guard site、true/false child kind/index，
  以及canonical leaf ordinal、leaf site、NoMod链和逐lane source。Metadata replay
  核对实际内层PHI、每个incoming block和leaf expression；独立Python verifier
  证明node 0为唯一根、其余node/leaf入度恰为1、child只前向引用、站点互异、
  lane完备且tree至少有store，并与FNV proof fingerprint逐字段一致；
- 自动化证据：little-endian单latch最大3-node/4-leaf正例覆盖三个不同partial
  writer和一个pure-carry叶，对256个continuation selector、5种iteration和4种
  leaf choice共5120组原/变换IR差分；通过symbolized LLVM verifier、Backsolver
  模型、continuation/unified双seal/replay、child-index manifest tamper及
  freshly sealed selected-byte 65→66篡改拒绝。PowerPC64 big-endian正例组合
  一个3-leaf tree transfer与一个ordinary latch，验证全slot v14 tagged
  fingerprint、地址序lane映射和独立replay；5-leaf负例证明continuation lowering
  已发生但原load保留；
- 完整门禁：continuation-memory v1--v14兼容组29/29通过（131.86秒）；LLVM 18
  构建、LLVM/lit 180/180和Python unittest 459/459（108.233秒）通过。Python
  `py_compile`、技术索引`--check`与`git diff --check`同时通过；当前环境没有
  `clang-format`可执行文件，故未把格式化工具列为已执行证据；
- 科研边界：该能力是显式3-node/4-leaf proof budget，不是任意predicate CFG。
  它要求reducible、unique-parent二叉树和leaf-local partial state，不支持共享
  reconvergence DAG、switch、多级MemoryPhi leaf、symbolic-index array、
  pointer PHI、跨迭代alias集合、exception edge、loop nest或一般heap fixed point。
  当前等级I/T/E；公开target接纳率、编译成本、solver CPU和等CPU coverage收益仍需
  confirmatory campaign。

## 281. F286：Bounded Ordered Pointer/Guard Writer Graph（2026-07-30）

- 研究问题：F278只能恢复一个depth-two pointer-union writer，F279只能恢复两条
  single-level guarded writer，F282又把二者固定成“最新partition、一个更旧guard”
  的唯一顺序。真实优化后IR可以交错出现多个有限pointer union和guarded store；
  只分别保存`pointerPartition`与per-lane guard列表会丢失两类writer之间的全局
  MemorySSA次序，从而无法区分`guard(partition(base))`与
  `partition(guard(base))`；
- 有界统一模型：F286沿MemorySSA从最新定义向最旧定义扫描，保存最多4个canonical
  writer layer，其中最多2个depth-two/3-node/4-leaf pointer partition和2个
  single-level guarded writer。每层ordinal 0表示最新写；guard layer保存store、
  instruction guard、store width以及逐lane source byte和true/false polarity，
  partition layer保存完整select-tree与逐leaf lane映射。已被更晚无条件写覆盖的
  lane会在较老writer中显式变为fallback，避免恢复不可见旧值。所有指针叶仍必须是
  same-base inbounds constant interval或由AA证明NoAlias，未知alias、动态offset、
  poison/undef value、volatile/atomic和超预算均fail closed；
- 保序lowering：先从最近的确定store或path-local LiveOnEntry snapshot重建base
  byte，再按writer序列从最旧到最新应用更新，使最新writer成为actual nested
  `select`的最外层。Partition的每个NoAlias leaf复用当时的完整fallback expression；
  guard对无影响lane不产生select。该构造保留last-writer-wins和原LLVM guard极性，
  不需要把不同writer guard合取，也不把有限union误解释为一般points-to集合；
- 兼容schema与proof：单partition继续输出v7、两guard继续输出v8、
  `newest partition + older guard`继续输出v11。只有第二个partition、partition外
  的更新guard、一个partition加两个guard或其他非旧式有序组合才升级为
  `ordered-writer-graph-continuation-memory-tuple-v15`，analysis为
  `llvm-memoryssa-aa-ordered-writer-graph-revalidated-v15`。Metadata、
  manifest和FNV fingerprint逐层绑定ordinal/kind/store/guard/polarity/lane或完整
  partition tree；compiler replay重做MemorySSA/AA并从actual outermost select
  向内逐层解析，独立Python verifier检查kind预算、canonical order、fallback/
  polarity一致性、tree ownership、唯一writer store和完整fingerprint；
- 自动化证据：最大little-endian正例按程序顺序包含old partition、old guard、
  new partition、new guard，逆向proof得到`guard/partition/guard/partition`四层；
  192组覆盖全部64种writer条件组合及三个continuation selector的原/变换IR差分
  一致；正例还在old partition之后对`high`执行无条件写，并断言该partition所有
  high-lane source均被规范化为`-1` fallback，而仍可见的low lane来源保持为0。
  测试同时通过symbolized LLVM verifier、Backsolver、continuation/unified双
  seal/replay、manifest ordinal/polarity tamper及freshly sealed selected-byte
  102→103篡改拒绝。PowerPC64 big-endian双partition正例验证地址序lane 0与两层
  fallback；第三个partition负例保留original load。原F278/F282的第二partition、
  newer-guard-after-partition和two-older-guards旧负例已升级为v15正例断言，而
  depth-three/shared DAG/unknown leaf、跨exit legacy-schema冲突仍拒绝；
- 完整门禁：continuation-memory v1--v15兼容组32/32通过（132.21秒）。LLVM 18
  构建通过；最终全量LLVM/lit为183/183（135.31秒），Python unittest为459/459
  （77.937秒）。Python `py_compile`与`git diff --check`通过；当前环境没有
  `clang-format`可执行文件，因而未把格式化工具列为已执行证据；
- 科研边界：这是constant-offset integer byte-lane上的有界writer expression
  graph，不是一般symbolic-address memory model。它不支持第三个partition/guard、
  depth-three或共享select DAG、pointer PHI、symbolic-index array、跨路径
  MemoryPhi priority、aggregate、exception edge、跨迭代alias集合或一般heap
  fixed point。当前等级I/T/E；公开target上的接纳率、表达式增长、solver CPU和
  等CPU coverage收益仍需confirmatory campaign。

## 282. F287：Bounded Acyclic SESE DAG and Local-PHI Predication（2026-07-30）

- 研究问题：F273只能识别每个非root block恰有一个父节点的internal tree，因此
  `if/else -> local join -> if/else -> local join`这类常见reducible CFG会因
  tree-local reconvergence和arm PHI而拒绝。Hydra/cfm-se的核心目标是把昂贵
  symbolic branch转换为ITE dataflow并由原程序复核aggressive-memory失败
  （[Saumya et al., OOPSLA 2026](https://arxiv.org/abs/2308.01554)）；要把该思想
  从树推进到实际优化后IR，必须先证明无环SESE拓扑，再按真实入边恢复局部PHI，
  不能把共享join复制成多个假叶子；
- 有界DAG发现：compiler用`PostDominatorTree`求左右入口的最近公共post-dominator
  作为唯一外部merge；每个arm从入口到该merge发现最多10个block、4个内部条件、
  8条merge leaf edge、3个多前驱local merge和64条flattened instruction。随后按
  stable block site进行确定性Kahn拓扑排序，要求root只有outer entry前驱、其他
  block的全部前驱均在同一arm且ordinal严格前向、左右arm不重叠、至少存在一个真实
  local merge。循环、外部前驱、多出口、exception、address-taken block、重复边、
  第4个local merge或预算溢出均fail closed；纯超长linear arm和无重汇合的4-branch
  chain仍沿用旧拒绝边界；
- 路径与PHI lowering：root guard分别为outer condition及其反值；每条内部边的
  guard等于`block_guard AND edge_condition`，多前驱block guard是所有incoming
  edge guard的OR。拓扑序保证前驱定义先完成，每个局部PHI再按canonical incoming
  edge逐项物化为guarded `select`，无活动路径时使用类型正确的零fallback。之后
  复用F273的predicated LCS alignment处理ALU/load/store，最终merge PHI按真实leaf
  edge guard恢复。Safe模式仍只接受无副作用运算；aggressive模式的load/store仍
  使用failure-preserving speculative/readback lowering并要求原binary replay；
- v4 proof：新增`bounded-acyclic-sese-dag-hydra-v4`，保留v1/v2/v3制品格式。
  Manifest绑定左右block/instruction/branch/leaf/local-merge/local-PHI数量、
  topological block/terminator sites、每个block的canonical predecessor
  `(block_ordinal, successor_index)`集合、successor/merge edge、local PHI sites、
  LCS slot、output PHI和FNV structure fingerprint。独立Python verifier从successor
  反推全部predecessor与local merge，检查严格前向拓扑、leaf集合、LLVM PHI opcode
  与声明site逐项相等、proof identity唯一性，并独立重算同一指纹；
- 自动化证据：最大正例含左侧两个、右侧一个local merge及3个局部PHI、2个最终
  output PHI；对全部256个byte输入比较原/变换IR，验证symbolized IR、manifest、
  campaign loader、unified seal/replay，并拒绝predecessor、local-PHI site及
  freshly sealed lowered-IR篡改。Aggressive-memory正例在左右local PHI之后写同一
  byte，256输入差分通过并证明一个guarded readback store。负例保留含跨arm外部
  前驱、循环和4-local-merge/13-block超预算CFG；F273旧
  `internal_reconvergence`负例升级为v4正例，而4-branch tree和1x5 linear arm仍
  拒绝。3项F287定向测试与9项Hydra v1--v4兼容组通过；
- 完整门禁：LLVM 18 Werror构建、Python `py_compile`和定向/兼容回归通过；
  Hydra v1--v4兼容组9/9通过，全量LLVM/lit为186/186（135.16秒），Python
  unittest为459/459（76.879秒）；技术索引`--check`、本地Markdown链接检查和
  `git diff --check`也通过；
- 科研边界：F287是reducible、single-entry/single-exit、branch-only CFG的显式
  proof budget，不是任意DARM/Hydra region。它不接受critical external predecessor、
  irreducible loop、switch/invoke/EH、call、atomic/volatile、超过3个local merge、
  超过8个局部PHI、跨region escaping SSA、symbolic-address safety proof或一般
  heap effect。当前等级I/T/E；是否减少solver query/CPU并提高公开target等CPU
  coverage仍需profile-guided confirmatory campaign。

## 283. F288：Profile-Bound and Concurrent-Safe Transformation Evidence（2026-07-30）

- 研究问题：原Hydra profile v1只封存遥测文件及打分，compiler manifest也只记录
  被选site的统计量，因此无法证明“生成该profile的程序”就是campaign中作为语义
  权威的原始程序。与此同时，Hydra、switch、loop和continuation四类compiler
  manifest都直接用`raw_fd_ostream(..., OF_Append)`写JSONL；多个编译进程共享
  manifest时没有进程间临界区，长JSON记录可能交错，seal也可能读到半条记录；
- profile v2身份链：`hydra_transform.py profile`新增必需参数
  `--profiled-command-json`。`symcc-hydra-profile-v2`封存解析后的绝对argv、
  input mode、profiled executable SHA-256和command SHA-256；文本profile同时
  携带profile artifact、executable和command三个摘要。Compiler严格解析v2头和
  五列entry，重复site、非规范摘要、缺头、非法数值、`interesting >
  observations`、不可读文件均使整个Hydra pass fail closed。一个合法但无entry的
  profile也保持“没有候选”，不再退化为无profile的隐式任意site选择。显式
  `SYMCC_HYDRA_SITE`仍按文档覆盖profile，v1文本继续兼容；
- manifest与campaign闭环：compiler record新增`selection_source`，v2选择时还
  绑定`profile_schema`、`profile_sha256`、`profiled_executable_sha256`和
  `profiled_command_sha256`。独立manifest verifier检查selection kind及三个
  lowercase canonical SHA-256。`symcc-hydra-replay-v2` campaign必须接收
  `--profile-artifact`，重新验证artifact内部摘要，要求其command与campaign
  original command逐字段相同、executable摘要相同、manifest摘要相同，并核对
  被选site的score/observations/interesting/solver time；不匹配在执行输入前拒绝。
  历史v1 profile、manifest和`benchmark/evidence/hydra_f248_smoke.json`仍由兼容
  路径验证，但不能声称具有v2 binary identity；
- 并发安全发布：新增统一`compiler/ManifestWriter.h`。四个compiler pipeline均
  先在进程内构造完整JSONL payload，再以`O_APPEND|O_CREAT`打开目标，对同一文件
  获取advisory exclusive `flock`，循环处理`EINTR`和partial write，完成全部记录后
  `fsync`，最后解锁并关闭。这样同一主机上所有新compiler进程共享manifest时，
  一个pipeline的一批记录成为不可交错临界区；旧的四处`OF_Append`已全部移除；
- 自动化证据：Python新增v2 artifact/header、内部command摘要篡改、campaign
  identity匹配、重新封存后的original executable篡改和替换original command拒绝，
  Hydra专项6/6通过。LLVM测试覆盖v1兼容、v2 manifest字段、独立verifier和缺少v2
  头时不变换；端到端路径由CLI生成真实v2 profile、LLVM pass写manifest、campaign
  验证原始命令，并在换成另一个可执行文件时非零退出。并发压力同时启动24个
  `opt`进程写同一Hydra JSONL，最终恰有24条可解析且独立验证通过的完整记录。
  F288定向lit组3/3及LLVM 18 Werror构建通过；
- 完整门禁：新增并发测试先后连续重跑两次均通过；最终全量LLVM/lit为187/187
  （135.52秒），Python unittest为461/461（77.260秒）。Python `py_compile`、
  技术索引`--check`、本地Markdown链接检查和`git diff --check`同时通过；
- 科研与信任边界：`flock`是同一主机、同一协作实现之间的advisory lock；它不保证
  所有NFS实现的锁/持久化语义，也不能约束绕过helper的外部writer。SHA-256绑定可
  检出无密钥篡改，但没有签名者身份、密钥轮换、append-only transparency log、
  inclusion proof或跨主机CAS transport。Profile v2绑定原始权威程序与选中统计，
  transformed binary仍通过campaign command digest及F274 seal分别记录，不等于
  reproducible build或远程证明。当前等级I/T/E；签名透明传输留给下一阶段。

## 284. F289：Signed Merkle-Logged Cross-Host Transformation Bundle（2026-07-30）

- 研究问题：F274/F288可证明本机文件在seal、replay和并发写入期间未发生无意变化，
  但任何人都能重新计算SHA-256，复制到另一主机后也没有可信签名者、日志包含性或
  自包含blob集合。F289参考
  [RFC 8032](https://www.rfc-editor.org/rfc/rfc8032.html)的Ed25519和
  [RFC 9162](https://www.rfc-editor.org/rfc/rfc9162.html)的Merkle leaf/node域
  分离，构造面向科研制品的有界签名transport；它借用密码学构件，不声称实现
  Certificate Transparency协议；
- 签名descriptor：新增`util/transform_artifact_bundle.py`。Publisher加载并验证
  F274 `symcc-transformation-seal-v1`，要求调用者提供的input IR、lowered IR、
  compiler、LLVM tool和全部kind manifest角色与seal恰好相等。Canonical
  `symcc-transform-transport-descriptor-v1`记录pipeline、seal digest以及每个文件的
  role、原名、transport名、mode、size、SHA-256和content-addressed blob路径。
  OpenSSL Ed25519在`"symcc-transform-bundle-v1\\0"`域内签署完整descriptor；
  verifier只信任命令行给出的public key，并以SPKI DER SHA-256固定trust root，
  不会信任bundle自带的key；
- append-only Merkle log：每个leaf绑定signed payload、signature和signer-key摘要，
  leaf hash为`SHA256(0x00 || canonical_leaf)`，内部节点为
  `SHA256(0x01 || left || right)`。Exclusive `flock`下先审计已有日志的连续index、
  每个历史root、previous-root chain、entry digest及Ed25519 tree-head signature，
  再原子追加新leaf并`fsync`。Bundle携带发布时tree head、独立log-key fingerprint、
  tree-head signature和从leaf到root的inclusion path；offline verifier不读取日志也
  能复算root，指定`--log`时还会核对相应历史record。Artifact signer和log signer
  使用分离的显式key参数；
- 跨主机制品：bundle是ZIP64容器，只允许`bundle.json`与descriptor声明的
  `blobs/<sha256>`成员；重复成员、额外成员、角色/文件名冲突、非法mode、错误
  uncompressed size、digest、嵌入seal或seal角色映射均fail closed。默认上限为32个
  role、单文件512 MiB、总计2 GiB、metadata 2 MiB、日志4096项/64 MiB。验证全部
  blob后，`--extract-dir`在同父目录建立临时目录，逐文件`fsync`、恢复受限mode、
  写入receipt，再以rename和parent-directory `fsync`发布；已存在输出拒绝。
  Publisher在实际写ZIP时重新hash源字节，importer也要求重新打开的metadata与已
  验签对象相等并在提取流上重新hash，关闭“验证A、复制B”的TOCTOU窗口。因此
  bundle可经任意外部复制方式传到无共享文件系统的主机后离线验签并导入；
- 自动化证据：6项Python测试覆盖keygen权限、API与CLI publish/verify/audit/extract、
  两个历史tree head及offline proof、8个并发publisher对同一日志得到连续0--7
  index并逐bundle核验、重新封存metadata后的signature/proof篡改、blob增补、错误
  artifact trust root、previous-root日志改写、seal角色缺失、artifact/log key-pair
  不匹配、验签后bundle替换和重复extract拒绝。定向Python 6/6及lit 1/1通过；
- 完整门禁：OpenSSL 3.0.13实机签名/验证、Python `py_compile`通过；最终全量
  LLVM/lit为188/188（134.79秒），Python unittest为467/467（79.299秒）。
  技术索引`--check`、本地Markdown链接检查和`git diff --check`同时通过；
- 信任与科研边界：私钥保护、passphrase/HSM、证书身份、key rotation/revocation和
  timestamp authority不在此实现内。单个本地日志文件可由拥有文件系统权限的对手
  回滚到一个历史上有效的prefix；发现split view/rollback需要外部checkpoint witness、
  gossip或独立公共日志。本实现没有HTTP提交/查询服务，所谓cross-host transport是
  自包含可复制bundle与原子import，不是网络传输守护进程。发布在日志append后崩溃
  可能留下无bundle的orphan leaf；这不破坏append-only验证，但需运维回收。它也不
  证明compiler为reproducible build或跨LLVM语义等价。当前等级I/T/E。

## 285. F290：LLVM Semantic Refinement and Cross-Major Exact Replay（2026-07-30）

- 研究问题：F274只在封存它的同一LLVM工具上做exact replay，F287的Hydra DAG
  lowering也没有显式记录`poison`、`undef`、`freeze`、潜在UB操作和异常控制流的
  处理契约。仅通过LLVM verifier不能证明两个版本的pass产生相同结构；另一方面，
  原生QF_BV tactic可能返回只覆盖部分输入offset的model，如果把它直接覆盖到当前
  witness而不回放完整Query IR，会把不满足目标的输入误写入async目录；
- LLVM语义契约：实现依据LLVM
  [Undefined Behavior](https://llvm.org/docs/UndefinedBehavior.html)、
  [freeze](https://llvm.org/docs/LangRef.html#freeze-instruction)、
  [select](https://llvm.org/docs/LangRef.html#select-instruction)和
  [br](https://llvm.org/docs/LangRef.html#br-instruction)规则。Hydra每个alignment
  slot只合并该次动态路径上的一对`freeze`，不同slot仍是不同动态实例；单边
  `freeze`先用outer path predicate屏蔽非活动值，再执行一次。可能在非活动arm
  产生即时UB的除数被替换为1，其余非活动operand替换为零；因此变换不会仅因
  speculative evaluation引入除零。含非`br` terminator或EH pad的region继续原样
  保留，当前没有把`invoke`、landingpad或异常边近似为普通边；
- 可复检manifest：Hydra记录新增`llvm_ir_semantics`、真实`llvm_version`/
  `llvm_major`、`inactive_operand_policy`、`freeze_policy`、`exception_policy`
  以及左右freeze数、成对数和单边extra数。独立verifier从alignment opcode重新
  推导四个计数并检查策略常量和版本格式；旧manifest仍兼容，但跨LLVM证书只接受
  带完整新契约的record。Compiler改用LLVM 17/18均支持的
  `IRBuilder::Insert(Instruction *, Twine)`，修复了LLVM 18 iterator overload在
  LLVM 17无法编译的API漂移，同时保留稳定指令名；
- 跨major exact replay：新增`cross_llvm_transform_replay.py`。它先执行F274 sealed
  baseline的独立exact replay，再为每个ABI匹配的candidate `opt`/plugin运行相同
  pass、LLVM verifier和candidate版本的`llvm-diff`。只有
  `llvm_version`/`llvm_major`可从manifest规范化掉；ordered proof identity、
  其余manifest、lowered textual IR size/SHA-256必须与baseline完全相同。候选
  `opt`与`llvm-diff`的major必须一致，pipeline与manifest kind必须一致，且证书
  至少包含两个不同LLVM major。当前持久证据
  `benchmark/evidence/hydra_f290_cross_llvm_certificate.json`在LLVM 18.1.3
  baseline与LLVM 17.0.6 candidate上通过，lowered IR与规范化manifest分别得到
  SHA-256 `ecf1990d...6ebc`和`08dfcb05...8644`；
  Audit还要求manifest声明的major等于实际tool identity，在candidate执行前后复核
  `opt`/`llvm-diff`/plugin制品相同，并在全部candidate完成后再次验证baseline seal，
  从而拒绝运行期间的工具、compiler或sealed artifact替换；
- Query候选完整性修复：`sample_generator`现在把应用`fixed`后的base作为第一个
  verifier-gated proposal，即使converter与fixed完全重合、`input_size=0`也不会
  丢失完整模型。`QueryStore.materialize_candidates`在存在Query IR时只发布通过
  prefix+target完整回放的primary/verified model；不满足Query IR的partial model
  不再写内部candidate或外部async文件。清空历史输出后的真实32-bit等式用例从错误
  `78 00 00 00`修复为唯一候选`78 56 34 12`；
- 自动化证据：`hydra_llvm_semantics.ll`覆盖paired/one-sided freeze、inactive
  `udiv`安全除数、EH拒绝、manifest计数篡改和LLVM verifier；
  `cross_llvm_transform_replay.ll`覆盖LLVM 18→17 exact replay、同major拒绝和重哈希
  claim篡改；Python测试覆盖证书内部digest、额外字段、major重标、manifest-kind
  替换、候选配置重复、fixed-base补全和矛盾partial model fail closed。最终
  LLVM 18为191/191（147.07秒），LLVM 17为190 passed/1 unsupported
  （147.83秒；cross-major驱动只在LLVM 18主套件启用），Python为471/471
  （124.550秒），两个compiler/runtime构建均通过；
- 科研边界：项目CMake声明的支持范围仍为LLVM 8--18；LLVM官方已经发布
  [21.1.0](https://releases.llvm.org/21.1.0/docs/ReleaseNotes.html)，但本项只实际
  构建并证明17/18，不能外推到8--16、19--21。Exact textual IR相等是所测输入与
  pass配置的强结构证据，不是对任意LLVM IR、优化pipeline或程序行为的形式化
  refinement证明；`llvm-diff`也不是定理证明器。Exception语义是拒绝策略而非
  建模。证书的canonical SHA-256提供自完整性但不提供外部身份，敌手可重算摘要；
  需要认证时应把证书作为artifact封入F289签名bundle。尚未完成公开target等CPU
  confirmatory campaign。当前等级I/T/E。

## 286. F291：Exact Symbolic-Index Heap-Region Writer Graph（2026-07-30）

- 研究问题：F275--F286只能把常量offset、guarded pointer select和有限pointer
  partition恢复为continuation memory tuple。形如
  `store V, gep i8, malloc(N), symbolic_index`的普通数组写即使allocation大小固定、
  load窗口固定且MemorySSA次序明确，也会因地址不是有限pointer leaf而整体保留原
  load。这不仅漏掉parser/table类程序的常见内存状态，也让F286“第三个partition”
  这类只是超过旧预算、并未改变语义类别的writer序列无处承载；
- 精确区域模型：新增v16
  `symbolic-region-writer-graph-continuation-memory-tuple-v16`。Compiler只接受地址
  空间0中外部声明形式的标准`malloc`或`calloc`，其常量extent必须在
  `[1, 2^32]`；load是区域内固定2--8 byte整数窗口。动态store必须是simple整数
  store，pointer为`inbounds getelementptr i8`，恰有一个非constant instruction
  index，其位宽等于DataLayout pointer-index位宽且不超过64，并支配capture。
  Store pointer的constant base、load base和allocation instruction必须逐值相同；
  未知extent、argument/external base、allocator定义、非inbounds/非byte GEP、
  位宽不符、volatile/atomic及未知ModRef均fail closed；
- byte等式编译：设load窗口起点为`L`，动态GEP的常量base offset为`B`，store第
  `s`个地址序byte覆盖load第`l`个lane当且仅当
  `index = L + l - s - B`。Compiler对每个lane只枚举store自身最多8个source byte，
  而不是枚举整个index域；每个可表示的有符号target变成
  `icmp eq index, constant`和`select(stored_byte, older_byte)`。因此即使index为
  64-bit，IR大小仍受`load_width × store_width`约束。MemorySSA仍按newest-first
  收集writer，lowering按oldest-to-newest应用；较新的无条件lane会清空较老动态
  layer的case，最后写者仍是actual expression外层。双端序只影响source byte的
  bit extraction，地址等式保持不变；
- 扩展writer预算：v15原有最多4层、2个partition和2个guard的canonical制品保持
  不变。V16允许最多8个总layer；出现symbolic-region layer，或layer/partition/
  guard超过v15预算时才升级。因此原先拒绝的三个depth-two partition现在由v16
  精确表达，但没有借新schema包装旧v15形状。它仍不是无界writer graph；
- proof与独立复验：LLVM metadata和FNV proof fingerprint逐layer绑定store site/
  width、region-base site、extent、index site/bits、signed base offset、逐lane
  case count以及每个signed index/source-byte。Manifest以规范有符号十进制输出
  `base_offset`和`index_value`。Compiler replay重新执行MemorySSA/AA并从actual
  `select/icmp`表达式最外层逐case剥离；Python verifier独立检查allocator region
  字段、signed index值域、lane/case ordinal、case唯一性和
  `(index-1, source+1)`规范次序，以二补码uint64重算指纹。Region extent或case
  的单独改写会导致proof fingerprint不匹配；F274 sealed replay还会从输入IR重建
  compiler proof，拒绝攻击者自行重算整个JSON的替换；
- 自动化证据：`backsolver_continuation_symbolic_heap_region.ll`覆盖固定
  `malloc(8)`、16-bit动态写到32-bit load、负index equality、768组原/变换
  `lli`差分、manifest extent/case篡改、F274与统一seal/replay、fresh-sealed
  actual `icmp`篡改、SymCC symbolization和真实Backsolver；从种子`A 00`求得保持
  `A`且`index mod 7 = 2`的目标模型。`symbolic_calloc_region.ll`覆盖
  `calloc(4,2)`乘法extent与非零`base_offset=2`；
  `symbolic_heap_region_big_endian.ll`验证PowerPC64地址序与高/低byte extraction；
  reject测试覆盖动态extent和external heap identity。原F286第三partition拒绝
  测试升级为v16正例。LLVM 17和18定向组均通过。最终全量门禁为LLVM 18
  195/195、LLVM 17 194 passed/1 unsupported（cross-major主驱动只在LLVM 18
  套件启用）和Python 471/471；
- 跨major证据：
  `benchmark/evidence/ifss_f291_cross_llvm_certificate.json`封存LLVM 18.1.3
  baseline与LLVM 17.0.6 candidate。两者continuation proof identity均为
  `17025496568858965401`，lowered textual IR SHA-256均为
  `ff1c9213...7192`，并同时通过独立manifest verifier、LLVM verifier、
  candidate `llvm-diff`和byte-identical gate；
- 科研边界：F291是单一固定大小heap object上的线性MemorySSA byte-address
  writer summary，不是一般heap graph、symbolic base、points-to、pointer PHI、
  strided/多维GEP、realloc、对象生命周期证明、动态extent、循环MemoryPhi fixed
  point或任意array theory。`inbounds`之外的未定义执行不形成额外语义保证。
  8-layer及8-byte预算是显式编译成本边界；当前证据证明正确性机制和Backsolver
  可达性，不证明公开target上的coverage/CPU收益。跨LLVM证书仍是fixture-specific
  structural evidence而非普遍refinement theorem。当前等级I/T/E。

## 287. F292：Symbolic-Region Cyclic Byte Fixed Point（2026-07-30）

- 研究问题：F277--F285能把常量offset的循环partial store恢复为有限byte-state
  PHI，F291能在线性MemorySSA链中恢复固定heap region上的symbolic-index writer，
  但二者此前互斥。Parser table、逐字节填充和bounded buffer loop中常见的
  `A[index_t] = value_t`因此在跨continuation readback时仍退回原load。直接展开
  各次迭代会让表达式随trip count增长，直接使用一般SMT array又无法复用当前
  continuation proof/replay链；
- 有限窗口不动点：F292只追踪最终固定2--8 byte load window，而不追踪整个对象。
  对每个地址序lane `l`，循环PHI保存上一迭代的byte `X[t,l]`；一个1--8 byte
  动态store的第`s`个byte给出递推

  ```text
  X[t+1,l] =
    ite(index[t] = load_offset + l - s - writer_base_offset,
        store_byte(value[t], s),
        X[t,l])
  ```

  同一lane有多个source byte时按规范case链组合。Lowering在回边生成有限
  `icmp eq/select`，再把全部lane组合为原load宽度的整数PHI。状态和IR规模受
  `load_width × store_width`约束，与运行时迭代数无关；循环每执行一次就对当前
  `index/value`应用一次transfer，因此没有丢弃写历史；
- 精确准入：只接受单entry、单unconditional backedge、单symbolic writer；entry
  必须能由既有线性byte proof精确恢复。Region仍须是地址空间0中外部声明、非变参、
  标准原型的`malloc/calloc`，size参数位宽等于DataLayout pointer-index位宽，
  常量extent在`[1,2^32]`。Load与store必须逐值共享allocator base；store须为simple
  integer store，经单一pointer-width `inbounds gep i8`索引，index、stored value和
  store均支配回边terminator。Argument base、动态extent、伪造同名allocator原型、
  两个动态writer、常量/动态writer混合、conditional transfer、multi-latch、
  pointer PHI、unknown ModRef和volatile/atomic均保留原load；
- proof与复验：新增
  `symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v17`和analysis
  `llvm-memoryssa-aa-symbolic-region-cyclic-byte-lane-revalidated-v17`。Metadata、
  manifest和FNV fingerprint绑定cycle header/entry/backedge、entry lane state、
  all-carry回边、store/allocator/index identity、extent、signed base offset及逐lane
  signed case。Compiler重新收集MemorySSA/AA，并从actual cycle PHI的每个lane剥离
  `select/icmp`链；Python verifier独立要求all-carry base、signed位宽、规范ordinal/
  case、互斥writer字段和二补码fingerprint编码。F274与统一seal再从原始IR重编译，
  因而能拒绝攻击者连JSON fingerprint一起重算的lowered-IR替换；
- 自动化证据：主测试覆盖`malloc(8)`、32-bit entry state、循环i8动态写、四lane
  equality、270组原/变换`lli`差分、extent与非法source-byte篡改、双seal/replay、
  fresh-sealed equality篡改、SymCC symbolization及真实Backsolver；从
  `C 01 aa`产生至少一个首字节为`A`的目标模型。PowerPC64大端测试以i16动态写
  验证`[(0,0),(-1,1)]`等case和高字节提取。Reject测试覆盖argument region、动态
  extent、两个动态writer、常量/动态混合；F291 reject另覆盖错误的
  `calloc(i32,i32)`原型。LLVM 17/18聚焦组均为6/6；
  最终全量门禁为LLVM 18 198/198、LLVM 17 197 passed/1 unsupported
  （cross-major主驱动只在LLVM 18套件启用）和Python 471/471；
- 跨major证据：
  `benchmark/evidence/ifss_f292_cross_llvm_certificate.json`封存LLVM 18.1.3
  baseline与LLVM 17.0.6 candidate。两者proof identity均为
  `11600885392756402730`，lowered textual IR SHA-256均为
  `6e5e4c5c...91189`，规范manifest SHA-256均为`2eb7bf05...1901`，并通过sealed
  baseline replay、独立manifest verifier、LLVM verifier、candidate `llvm-diff`
  和byte-identical gate；
- 科研边界：这是固定单对象、固定read window上的精确scalarized recurrence，
  不是完整array theory、loop invariant synthesis、symbolic base/points-to、
  多对象heap graph、动态extent、realloc/lifetime、多个/条件/多回边动态writer
  或任意退出时刻的对象快照。`inbounds`为原程序定义域的一部分；越界迭代本身是
  原IR未定义行为，不由摘要赋予新语义。当前证据是机制正确性与跨版本结构证据，
  尚未给出公开target的触发率、IR增长、solver CPU或coverage AUC。当前等级I/T/E。

## 288. F293：Bounded Shared-Predicate DAG Guard Hash-Consing（2026-07-30）

- 研究问题：F287已能处理每arm至多10个block、3个local merge的无环SESE CFG，
  但更深的`branch -> local PHI -> branch`链仍因证明预算拒绝。与此同时，原v4
  lowering在恢复block guard、local PHI和最终leaf时会多次物化同一CFG edge guard；
  多个branch复用同一SSA predicate时也会重复构造其反值。直接提高预算会让新增
  `and/or/not/select`与图规模一起增长，反而放大符号表达式和solver负担；
- v4兼容与v5准入：candidate仍先走F273 tree和F287 v4，完全处于旧证明域的region
  保持原schema及原lowered IR。只有v4失败后，v5才把每arm预算扩为14 blocks、
  5个内部branch、10条external-merge leaf edge、4个local merge、12个local PHI和
  96条flattened instruction。它继续要求最近公共post-dominator作为唯一外部
  merge、stable-site Kahn拓扑、单入口、全部前驱位于同一arm、严格前向edge、
  左右arm不重叠及至少一个真实local merge；15 block/第5个local merge、cycle、
  external predecessor、switch/invoke/EH、escaping SSA和unsupported effect仍
  fail closed；
- region-local hash-consing：v5为每个arm建立以
  `(predecessor_ordinal, successor_index)`为键的edge-guard cache。一条edge第一次
  物化为`block_guard AND edge_condition`，之后local PHI、后继block或final leaf
  直接复用同一SSA值；两个arm再共享一个以lowered predicate `Value*`为键的negation
  cache，因此相同predicate的false edge只生成一个`not`。Block guard仍是全部
  incoming edge guard的OR，PHI仍按真实incoming edge做guarded select；缓存只做
  公共子表达式消除，不合并edge身份，也不改变路径优先级或fallback语义；
- v5 proof与独立复检：新增
  `bounded-shared-predicate-sese-dag-hydra-v5`和alignment
  `compatible-lcs-shared-dag-topological-left-tie-v5`。Manifest除继承v4完整
  predecessor/successor/leaf/PHI/alignment/output证明外，还绑定canonical CFG
  guard edge数、按拓扑顺序的左右predicate site序列、unique/reused predicate数、
  edge/predicate缓存复用计数及
  `per-arm-edge-and-global-negation-hash-cons-v1`策略。Compiler metadata和FNV
  fingerprint同时封存这些字段。Python verifier从successor row重算edge数，从
  predicate序列重算等价类和复用次数，要求v5至少有一个维度真实超出v4预算，
  并把实测缓存复用计数纳入指纹，防止把普通v4 record重标为v5；
- 自动化证据：最大正例左arm含13 blocks、4 branch、4 local merge/PHI，两个branch
  共享同一predicate；右arm为单block。Compiler报告18条canonical edge、3个unique
  predicate/1次结构复用、8次edge-cache hit和至少1次negation reuse；lowered IR中
  5次branch predicate occurrence只保留4条i1 negation。测试对全部256个byte输入
  比较原/变换IR，并覆盖LLVM verifier、SymCC symbolization、campaign manifest
  loader、unified seal/replay、edge/predicate manifest tamper及freshly sealed
  lowered-IR opcode tamper；另一个13-block/4-merge但无local PHI的正例证明合法
  `reused_guard_edges=0`仍可被compiler/verifier一致接纳。更新后的负例用
  16-block/5-local-merge图验证v5预算拒绝；
  Hydra v1--v5兼容组在LLVM 17和18上均为9/9；
- 跨major证据：
  `benchmark/evidence/hydra_f293_cross_llvm_certificate.json`封存LLVM 18.1.3
  baseline和LLVM 17.0.6 candidate。两者proof identity均为
  `3502535688576112958`，lowered textual IR SHA-256均为
  `cd12b34975f22a06c2c8de323509511dc576b94693ed38f1f767df7dc0e6d700`，
  normalized manifest SHA-256均为
  `409fe0b27aaf5699d4f86f3e30f14322fad721e730f6dcbd5e13754ed17f3e9f`；
  sealed baseline replay、独立manifest verifier、candidate LLVM verifier、
  `llvm-diff`及byte-identical gate全部通过；
- 完整门禁：LLVM 18和17 Werror构建、Python `py_compile`、技术索引与
  `git diff --check`通过；最终按套件顺序、固定`-j16`执行时，LLVM 18 lit为
  199/199（134.63秒），LLVM 17为198 passed/1 unsupported（133.79秒），
  Python unittest为471/471（79.270秒）。诊断轮曾同时启动两套默认192-worker
  lit和Python，分别令`poly_exact_widening_renaming.c`的精确投影资源不足、
  `test_qf_bv_backend.py`的持久helper被取消；两项隔离复测分别在0.48秒和
  3.85秒通过，随后上述受控全量门禁全部通过。因此它们被归类为同机过度并发
  争用，不作为功能失败，也不删除对应压力测试；
- 科研边界：F293只在一个被选Hydra region内部复用edge guard，并允许其左右arm
  共享predicate negation；它尚未跨多个region或编译单元做全局hash-consing，也
  不支持critical external predecessor、irreducible/cyclic CFG、switch/invoke/EH、
  arbitrary call/memory effect或动态profitability proof。14/5/10/4/12/96均为显式
  proof budget，不是任意DARM/Hydra区域。当前证据是机制、差分与跨版本结构证据，
  尚无公开target等CPU下的触发率、IR减少、solver query/CPU和coverage AUC。
  当前等级I/T/E。

## 289. F294：Conditional Multi-Latch Symbolic-Region Transfer（2026-07-30）

- 研究问题：F292首次把固定heap region上的symbolic-index store压缩为不随trip
  count增长的byte-window循环递推，但v17只允许一个无条件回边。真实parser table、
  bucket update和循环内scatter常由多个continue/回边退出到同一header，且某个
  latch上的写还可能受branch guard控制。把各latch的writer串成一个顺序链会错误地
  假设同一迭代执行了多个互斥回边；把条件写无条件应用则会覆盖未走store arm时的
  carry。F294因此扩展的是MemoryPhi的互斥回边transfer，而不是一般array theory；
- 互斥transfer语义：设header中load window的上一状态为`X`，第`j`个latch的动态
  store index/value为`i_j/v_j`，store source byte `s`覆盖lane `l`所需的有符号常量
  为`k_{j,l,s}`。先定义writer region更新

  ```text
  R_j(X,l) =
    ite(i_j = k_{j,l,s_0}, byte(v_j,s_0),
      ite(i_j = k_{j,l,s_1}, byte(v_j,s_1), ..., X_l))
  ```

  无条件latch使用`T_j(X,l)=R_j(X,l)`；条件store arm使用
  `T_j(X,l)=ite(g_j,R_j(X,l),X_l)`，其中`g_j`的polarity按实际store successor
  绑定。Header的下一状态由LLVM MemoryPhi在entry和各个`T_j`之间选择；不同
  latch是同一次迭代的替代来源，绝不按ordinal顺序复合。Lowering相应生成内层
  `index == k` equality/select和外层guard/select，整数PHI仍承载全部address-order
  byte lane；
- 精确准入与兼容：新增
  `symbolic-region-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v18`
  及
  `llvm-memoryssa-aa-symbolic-region-multi-latch-cyclic-byte-lane-revalidated-v18`。
  只接受2--4个latch；每个latch至多一个固定region symbolic writer，writer可为
  unconditional或单层条件store arm。Region、allocator原型、固定extent、2--8
  byte load window、1--8 byte simple integer store、pointer-index位宽、逐值相同
  allocation base、`inbounds gep i8`和dominance条件继承F291/F292。一个latch存在
  两个dynamic writer、动态extent、第5个latch、nested predicate、unknown ModRef、
  pointer PHI和volatile/atomic均fail closed。旧v17单回边及v10/v12/v13常量offset
  multi-latch shape继续产生原schema，不借v18重标；
- proof、actual-IR replay与独立复验：每个transfer在既有lane state之后新增一个
  presence bit和完整`symbolic_region_writer` record；presence、allocator/extent、
  store/index identity、signed base/case、conditional guard site/polarity和ordinal
  全部进入metadata与FNV fingerprint。Compiler重跑MemorySSA/AA后，从actual
  cycle PHI检查每个incoming value：先剥离外层guard select，再逐lane剥离内层
  equality chain，最终fallback必须回到同一个cycle PHI byte。Python verifier
  独立要求symbolic transfer的普通lane state全为canonical carry，校验2--4 latch、
  至少一个symbolic writer、互斥legacy字段、signed规范序和同一presence-aware
  fingerprint。统一seal从原始IR重编译，因而单改JSON并重算外层digest不能替代
  compiler proof；
- 自动化证据：x86主测试含两个回边，一个conditional `slot[index]=value`，另一个
  unconditional `slot[index+1]=90`；3个continuation selector、7个iteration
  count、两种write选择和3组value共126组原/变换`lli`差分通过，并覆盖manifest
  extent/source-byte篡改、两类seal/replay、fresh-sealed actual equality篡改、
  LLVM verifier、SymCC symbolization和真实Backsolver。PowerPC64四回边上界测试
  用i16 writer验证大端`(index_value,source_byte)=(0,0),(-1,1)`及一个条件transfer。
  Reject测试分别证明同latch双dynamic writer、动态malloc extent和五回边保留原
  load。LLVM 17/18三项聚焦测试均为3/3；
- 跨major证据：
  `benchmark/evidence/ifss_f294_cross_llvm_certificate.json`封存LLVM 18.1.3
  baseline与LLVM 17.0.6 candidate。两者continuation proof identity均为
  `2389278380394154034`，lowered textual IR SHA-256均为
  `c2802fe2c1015fc0117954e0f74c3edcf19d6cac2d67cabd400825ebe87f3769`，
  normalized manifest SHA-256均为
  `296583a8c561d62aa2abaecca7e844a7c5edb1aa4c5dcecca7ef51638e0ca409`，
  且通过独立manifest verifier、sealed baseline replay、candidate LLVM verifier、
  `llvm-diff`和byte-identical gate；
- 完整门禁：LLVM 18/17构建、Python `py_compile`、技术索引生成/检查、cross-major
  certificate verifier和`git diff --check`通过。三个重型套件按顺序固定`-j16`
  执行：LLVM 18 lit为202/202（134.31秒），LLVM 17为201 passed/1 unsupported
  （134.07秒；cross-major主驱动只在LLVM 18套件启用），Python unittest为
  471/471（79.584秒）；
- 科研边界：F294不是同一latch内多个ordered dynamic writer、nested-predicate
  symbolic transfer、symbolic base、dynamic extent、strided/typed多维GEP、
  pointer-PHI、多对象heap graph、realloc/lifetime或SMT array summary。2--4 latch、
  单writer/latch、固定2--8 byte观察窗口和单层guard是显式proof/cost budget。
  126组差分、双端序和跨major证书证明机制与制品一致性，不证明公开target上的
  触发率、IR增长、solver CPU、coverage AUC或优于baseline。当前等级I/T/E。

## 290. F295：Dominance-Proven Cross-Region Guard Hash-Consing（2026-07-30）

- 研究问题：F293虽然在一个selected Hydra region内复用edge guard和predicate
  negation，但`transformHydra`每次module pass只选择一个候选，cache也随
  `applyDiamond`返回而销毁。因此同一函数中串联的多个昂贵DAG即使复用同一个
  外部SSA predicate，仍会重复构造`not`及其后续符号表达式。把cache简单提升为
  module-global会让前一函数或非支配分支上的Instruction泄漏到当前插入点，产生
  非法SSA或错误相关性；
- 有界多site准入：新增显式
  `SYMCC_HYDRA_SITES=site0,site1[,site2,site3]`，仅接受2--4个非零、互异site。
  默认单site/profile选择路径完全不变。多site请求必须全部位于同一函数，每个候选
  必须先独立满足F293 v5 proof；所有arm-owned block两两不相交，任何候选entry/
  merge都不能被另一region拥有；给定顺序中的前一entry block必须支配后一entry
  block。Compiler在任何IR编辑前完成全体收集、disjointness/dominance检查，并求
  各region internal conditional predicate的`Value*`交集；交集为空、stable-site
  identity折叠、逆序、重复/缺失site均整批fail closed；
- dominance-safe复用：function-scoped cache以原始predicate `Value*`为键，value为
  `{negation instruction, producer site}`。同一region内沿用F293复用；跨region
  命中还必须由针对当前已变换函数重新构建的LLVM `DominatorTree`证明producer
  negation支配当前outer branch插入点，否则在当前region重新物化并更新cache。
  因而复用的是完全相同SSA predicate的一元`not`，不是只因opcode/文本相同就合并
  任意ALU、load、select或不同路径上的undef选择。Per-arm edge cache仍保持region
  local，edge ownership没有跨region混合；
- v6 transaction proof：新增
  `bounded-cross-region-shared-predicate-sese-dag-hydra-v6`、
  `compatible-lcs-cross-region-shared-dag-topological-left-tie-v6`和
  `per-arm-edge-and-function-dominating-negation-hash-cons-v2`。Transaction
  fingerprint绑定有序site序列及各region共同predicate site集合；每个record再绑定
  相同fingerprint、ordinal/size、完整site/predicate数组、本region跨区reuse次数与
  唯一producer site列表。Structure fingerprint在完整v5 topology/alignment/cache
  proof外继续混入全部transaction字段。Merged branch metadata也携带schema、
  transaction identity/ordinal/size和实际cross-reuse/source计数；
- 独立复验与重放：Python verifier逐record重算v5 topology/predicate/cache proof、
  transaction和structure两层FNV，要求首record的cross count/source均为零，后续
  record至少一次cross reuse且source只能来自更早ordinal；JSONL层再要求2--4条
  record按ordinal连续、函数/site/predicate/transaction identity完全一致且总复用
  非零。Legacy v1--v5不得夹带v6字段。F274 seal从“Hydra恰好一条record”扩为
  “一条legacy或一个完整v6 transaction”，replay清空外部变换环境后用封存的有序
  site数组重编译，仍要求manifest和lowered IR逐字节一致；
- 自动化证据：主fixture串联两个13-block、4-local-merge的v5 DAG，并让它们共享
  `shared/middle/last`三个函数参数predicate。若各自独立物化需要8条`xor i1`，
  function-scoped dominance cache只生成5条；第二record报告至少3次cross reuse，
  producer严格为site 7001。测试对全部256个byte输入比较原/变换返回码，覆盖LLVM
  verifier、SymCC symbolization、两record seal/replay、重新封存actual `mul->add`
  篡改拒绝、ordinal/source/JSONL顺序篡改；逆dominance顺序、重复site、缺失site和
  同函数但无共同predicate的site 7001/7003均保留完整原CFG。Hydra v1--v6兼容组在
  LLVM 17/18上均为11/11；
- 跨major证据：
  `benchmark/evidence/hydra_f295_cross_llvm_certificate.json`封存LLVM 18.1.3
  baseline与LLVM 17.0.6 candidate。两条structure proof identity分别为
  `8175852705754774924`和`1889164356607597381`；lowered textual IR SHA-256均为
  `a0df2ebd9b22a950a6301330cdbdc421fbc6bf35cc3b4eab0a458c2cf957a937`，
  normalized manifest SHA-256均为
  `31a953eeb4e74106e7bbadb2e66af29fa725db8e8745cc4c43827cbf5942d4c8`，
  且双record sealed baseline replay、candidate LLVM verifier、`llvm-diff`与
  byte-identical gate全部通过；
- 最终门禁：顺序执行LLVM 18/17全量lit与Python全量unittest。LLVM 18为
  203/203（134.10秒），LLVM 17为202 passed加1项预期unsupported（133.26秒），
  Python为471/471（79.599秒；shell总墙钟80.120秒）。计数包含F295新测试，未以
  定向测试替代全量门禁；
- 科研边界：F295不是跨函数/module CSE、任意结构hash、跨region edge-guard复用、
  多site profile profitability selection、非支配兄弟region、overlapping/nested
  region、critical edge、switch/invoke/EH或cyclic/irreducible CFG。当前
  `util/hydra_transform.py`的failure/coverage campaign仍严格消费单site manifest；
  v6已有编译、proof、seal和cross-major replay，但公开多site等CPU campaign尚未
  接入。2--4 site、v5-only、同函数strict-dominance chain和共同predicate交集是
  显式proof budget；当前等级I/T/E。

## 291. F296：Same-Latch Ordered Symbolic-Region Multiwriter（2026-07-30）

- 研究问题：F292的单回边fixed-region recurrence只接受一个dynamic writer，
  F294则把多个writer限制在互斥latch上。真实循环体经常在同一次迭代内连续写同一
  动态区域；这些写不能按多latch的“替代incoming state”解释，而必须保留MemorySSA
  的程序顺序和last-writer-wins语义。F296实现该不同子域，不改变v17单writer和v18
  多latch proof；
- 有界准入：仅处理单入口、单无条件回边、2--8 byte固定读取窗口，以及由标准
  `malloc`/`calloc`常量extent创建的同一allocation。回边MemorySSA定义链允许2--4
  个simple、非volatile、非atomic的symbolic-index integer store；每个store的
  inbounds `i8` GEP、pointer-index位宽、stored value和store instruction必须支配
  latch终结点，AA不得证明它与读取window NoAlias。所有writer必须绑定同一
  allocation identity和extent。第5个writer、动态extent、static/dynamic相关写
  混合、条件latch、未知ModRef、MemoryPhi/atomic/fence或域外GEP整项fail closed；
- 顺序语义：MemorySSA从load向前遍历时首先遇到最新定义，因此内部数组以
  `ordinal 0 = newest`记录。对byte lane `l`，转移为
  `W0(l) if hit0 else W1(l) if hit1 ... else carry(l)`。Lowering从数组尾部
  oldest writer开始构造select，再向前包裹newer writer；最终IR最外层因此是
  newest writer。Actual-IR replay按相反方向从最外层逐个剥离`icmp eq`/`select`，
  最后必须恢复到同一cycle PHI的对应byte，避免只相信manifest声称的顺序；
- v19 proof与制品：新增
  `ordered-symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v19`和
  `llvm-memoryssa-aa-ordered-symbolic-region-cyclic-byte-lane-revalidated-v19`。
  Metadata先记录writer count，再按newest-first次序写入每条store/base/extent/
  index/base-offset/signed lane-case proof。Manifest的
  `symbolic_region_writers`数组显式携带连续ordinal。Compiler replay重新执行
  MemorySSA/AA、验证writer ordinal、共享region和actual expression；独立Python
  verifier要求2--4 writer、store site互异、base/extent一致、all-carry基础状态、
  v17/v19字段互斥，并按相同顺序重算完整continuation fingerprint；
- 自动化证据：小端主fixture在一个latch内按old/middle/new顺序执行3个dynamic
  store，其中前两条命中相同地址，第三条写`index+1`。105组原/变换输入差分覆盖
  0--6次迭代、3个continuation selector和5组writer value，验证middle覆盖old及
  跨迭代newest优先；LLVM verifier、SymCC symbolization、真实Backsolver、
  legacy/unified seal/replay、writer-order/extent manifest tamper和重新封存的
  equality-constant IR tamper均通过。PowerPC64正例用两个i16 writer验证大端
  `(index,source-byte)`映射；负例拒绝5 writer和两writer之间的unknown ModRef。
  v16--v19兼容组在LLVM 17/18上均为12/12；
- 跨major证据：
  `benchmark/evidence/ifss_f296_cross_llvm_certificate.json`封存LLVM 18.1.3
  baseline与LLVM 17.0.6 candidate，proof identity为
  `3118488025292340061`，lowered textual IR SHA-256为
  `cd776afe464d40533caae83d942d1eded06b5174652585128feae7ad313e03c5`，
  normalized manifest SHA-256为
  `d83e318f2ec82a93aab8894eab29c278b5a91a9254a1470cb35221c4f7fabe55`；
  baseline sealed replay、candidate LLVM verifier、`llvm-diff`和byte-identical
  gate全部通过；
- 最终门禁：双LLVM Werror构建通过；顺序执行全量套件，LLVM 18为206/206
  （lit 135.31秒，shell 135.361秒），LLVM 17为205 passed加1项预期unsupported
  （lit 139.22秒，shell 139.276秒），Python为471/471（unittest 80.736秒，
  shell 81.265秒）。计数包含F296三项新lit和被重新分类的v17拒绝fixture；
- 科研边界：F296不是nested-predicate/conditional same-latch dynamic writer、
  多latch内多writer、symbolic base、dynamic allocation、pointer-PHI、跨对象heap
  graph、realloc/lifetime、strided/typed多维GEP或一般SMT array summary。2--4
  writer、固定allocation/window和线性MemorySSA链是显式proof/cost budget；
  机制与fixture达到I/T/E，不代表公开target上的触发率、IR增长、solver CPU、
  coverage AUC或优于baseline。

## 292. F297：Scalar LLVM Integer Min/Max Symbolization（2026-07-30）

- 研究问题：真实前端和LLVM优化会把长度裁剪、范围选择规范化为
  `llvm.smin/smax/umin/umax`。continuation IR已有自己的有界lowering，但普通
  Symbolizer此前把这些结果具体化；libarchive 7zip对象的`-O3`编译真实触发10条
  warning，后续分支无法反推min/max的输入；
- 精确语义：公共runtime新增四个二元builder，分别构造
  `ite(a <s b,a,b)`、`ite(a >s b,a,b)`、`ite(a <u b,a,b)`和
  `ite(a >u b,a,b)`。比较与ITE直接使用后端bit-vector primitive，不经过host
  promotion。Compiler Runtime表导入四个ABI，Symbolizer沿用已有短路机制注册
  scalar symbolic computation；
- 覆盖联动：numeric data-origin追踪从“只查第一个参数”改为遍历全部intrinsic
  参数，并将四种min/max纳入，因此静态数据位于第二个operand时仍能生成扩展data
  comparison通知；vector overload显式保留在具体化边界；
- 自动化证据：`test/integer_min_max.ll`检查instrumented IR中的四个builder与
  `_sym_notify_data_cmp_ext`，编译日志无对应unhandled warning，并由QSYM从
  `00 00`实际生成同时满足四种比较的`2e fb`。LLVM 18与LLVM 17定向lit均为1/1，
  双plugin/QSYM runtime构建通过；顺序全量门禁为LLVM 18 210/210
  （138.45秒）、LLVM 17 209 passed加1项预期unsupported（139.56秒）及Python
  475/475（82.117秒）；
- 真实程序证据：相同libarchive
  `archive_write_set_format_7zip.c`、相同include与`-O3`命令在180秒保护下exit 0；
  min/max concretize warning从10降为0，仍只有原源码的一条bit-field warning。
  对象SHA-256为
  `46e25209ccdb312aedb2d002921dac0f6e191cf0f0c61548b1c6b134d0d0a06b`；
  完整续档与日志见
  [`SOTA_Implementation_Continuation_2026-07-30.md`](SOTA_Implementation_Continuation_2026-07-30.md)
  及
  [`libarchive_7zip_object_after_f297.log`](evidence/current-eval-2026-07-30/libarchive_7zip_object_after_f297.log)；
- 科研边界：F297不支持vector/VP overload，不新增完整poison/undef求解，也不证明
  coverage或wall-time提升。表达式保真可能增加solver成本，需等CPU、多轮消融。
  当前等级I/T/B-mechanism。

## 293. F298：AFL++ Native SymCC Peer Feedback（2026-07-31）

- 研究问题：旧helper把SymCC判新的输入直接追加到`fuzzer01/queue`，但AFL不会重新
  扫描自己的磁盘queue，导致“离线能看见文件”被误认为“在线corpus已导入”。初版
  修复采用`-F`独立目录；对AFL++ 4.40c源码和真实进程复核后发现`-F`按whole-second
  `st_mtime`判新，高频producer存在同秒遗漏窗口；
- 原生peer协议：SymCC只向已有的`afl_out/symcc01/queue`写严格连续
  `id:NNNNNN,src:NNNNNN`。AFL `-M fuzzer01`把该目录作为同campaign的兼容peer，
  用`fuzzer01/.synced/symcc01`四字节native-endian next-ID游标扫描，并仅把由自己
  当前virgin map判新的输入保存为`...,sync:symcc01,...`。真正的honggfuzz、
  GRIMOIRE或用户外部目录仍可使用`-F`；SymCC不再重复发布两份；
- 生命周期：hybrid配置删除继承的`AFL_NO_SYNC`，规范化最短受支持
  `AFL_SYNC_TIME=1`并设置`AFL_FINAL_SYNC=1`。终止顺序改为producer、AFL
  secondary、AFL master，MPI日志也在producer退出刷新后读取。AFL++ 4.40c在
  stop signal后的final sync仍可由`stop_soon`在单case边界中断，系统因此不伪造
  complete保证；
- 连续性修复：普通queue、hang与crash改用三个独立、可恢复的ID counter；否则任一
  hang/crash都会消耗共享ID并令peer queue出现缺号，AFL scanner会永久停在缺失的
  next-ID。回归在同一batch依次制造hang、crash和interesting case，证明三个目录
  都从`id:000000`独立推进且peer queue无洞；
- 回环去重：已接纳SymCC内容的SHA-256登记从可选`-F`写入分支提升到公共接纳路径，
  同时持久化hash ledger；AFL通过native peer导回相同内容后，helper不会把自己的
  concolic产物再次派发。无`afl_sync_dir`回归直接检查内存集合与持久callback；
- 可观测性：结果与CSV/JSON/text report区分`peer_published`、native cursor
  `peer_scanned`、按queue名称精确归因的`peer_imported`、
  `peer_not_retained=scanned-imported`及`peer_sync_complete`。AFL自身
  `corpus_imported`保留为所有来源聚合，不能用于多peer下的SymCC独占归因；
- 兼容CLI：`--afl-sync-dir`保留给手工独立`-F` producer，但文件名修为AFL标准
  `id:NNNNNN`，parser以`realpath`身份同时拒绝AFL own queue、SymCC peer queue及
  符号链接别名，避免自同步和覆盖；
- 自动化证据：四文件`py_compile`、13项AFL orchestration测试、3项report测试和
  scoped `git diff --check`通过。测试实际执行`_batch_triage`、验证原子内容、严格
  名称、二进制cursor坏长度拒绝、来源隔离和CSV持久化；顺序全量门禁为Python
  480/480（83.682秒，回环去重后复跑；realpath guard由定向测试覆盖）、LLVM 18
  210/210（133.54秒）、LLVM 17 209 passed加1项
  unsupported（133.41秒）；
- 真实机制证据：75秒、`np=4`、一主AFL加两SymCC worker，并故意继承
  `AFL_NO_SYNC=1`。Hybrid成功移除禁用项；SymCC发布23、AFL游标扫描22、精确保留
  4、已扫描未保留18、硬停止尾部1。AFL为170/715 edges、1,205,634 exec、
  15,862.35 exec/s；四个`sync:symcc01`文件与其source SHA-256为4/4一致。证据见
  [`f298_native_peer_sync_2026-07-31.md`](evidence/current-eval-2026-07-30/f298_native_peer_sync_2026-07-31.md)；
- 科研边界：F298证明native online transfer与来源归因，不证明23.78%覆盖是其净
  收益。短预算hard-stop可留下未扫描尾部；`not_retained`包含内容已知或无新coverage，
  不是solver错误。真正的收益仍需等CPU、多轮sync on/off、coverage AUC与尾部率
  消融。当前等级I/T/B-mechanism。

## 294. F299：Cross-Layer Correctness Hardening（2026-07-31）

- 研究问题：F298之后的深度审查发现，单个机制各自“能运行”仍不足以保证端到端
  正确性。AFL可观察文件名、distributed coverage claim、showmap生命周期、严格
  query与cache budget、LLVM `freeze`、DPOR调用来源和benchmark计量单位之间存在
  跨层不变量；任何一层破坏都可能把半写文件、oracle故障、截断公式、库内部锁或
  重复queue文件误解释为有效科研结果；
- Durable peer transaction：新增非变异`count_delta`和同目录隐藏staging；完整写入、
  file fsync、atomic rename、directory fsync后才允许claim。disk failure不claim也不
  消耗ID。distributed race loser保留完整但全局冗余的ID槽并推进counter，避免删除
  后破坏AFL native cursor的连续性；同时把candidate bits立即合入本地bitmap，避免
  等待周期gossip期间重复发布。foreign queue复用同一publish primitive；
- Oracle recovery：`StreamingShowmap`支持spawn/restart/单输入重试，第二次失败再用
  isolated one-shot showmap。所有oracle失败才记`showmap_none`，同时撤销content
  digest，使同一输入以后可重试；故障不再伪装成byte duplicate；
- Solver/compiler soundness：严格Z3 query收集完整dependency forest及target，1024
  仅是UNSAT core artifact cache上限；超大core不缓存但不截断公式。普通symbolic
  operand经过LLVM `freeze`保持同一expression，concrete freeze仍由原IR执行。
  static input dependence所有offset/upper/overlap用128-bit中间算术，只有可表示的
  非负int64 input interval才写入；常量size先检查active bits再读取uint64，i128超宽
  size直接fail closed而不会触发LLVM assertion；
- DPOR module provenance：constructor用`dl_iterate_phdr`封存主程序及显式allowlist
  DSO的PT_LOAD区间；pthread wrapper按return address过滤libc/runtime内部调用。
  `SYMCC_SCHEDULE_MODULES`纳入启动时DSO，`SYMCC_SCHEDULE_ALL_MODULES=1`保留诊断
  兼容。paired原子程序从1502行降至101行，467 lock + 467 acquire + 467 unlock全部
  消除，6个原子源事件与1个result仍精确保留；
- Evidence contract：AFL-only按每个np启动等数量master/secondary实例。AFL work来自
  `execs_done`，retained file单列，unique对所有queue做SHA-256并集；CSV增加
  `afl_executions`、`generated_kind`、`throughput_kind`。隐藏staging和`.tmp`既不计数
  也不复制进最终corpus，live queue原子变化按瞬时缺失处理。report不同单位不再计算
  speedup，总体排序使用edge coverage及跨模式一致的retained unique。默认统计分析
  移除异构`generated`；
- 测试基础设施：32-bit `suffixes.add`经隔离实验确认正确，推翻原审查误报；真正的
  hermeticity问题是LLVM tools未进入lit PATH，site config现前置CMake发现的工具目录。
  `TARGET_32BIT=ON`配置可递归发现231项测试；
- 自动化证据：LLVM 18全量214/214（203.26秒），Python unittest 489/489
  （81.672秒），LLVM 17重新构建与本轮7项定向lit为7/7。新增或加强
  `unsat_core_long_prefix.c`、`freeze_symbolic.ll`、`schedule_atomic_trace.c`、
  `concurrency_guidance.c`、`schedule_module_filter.c`、
  `static_dependencies_wide_size.ll`以及AFL/benchmark Python tests；
- 科研边界：F299证明修复后的机制不变量和计量契约，不提供新的LAVA-M漏洞数、覆盖率
  AUC或CPU-hour提升。filesystem与multi-shard claim不是跨介质ACID transaction；
  DSO allowlist不自动捕获constructor之后的`dlopen`；freeze不建立完整poison lattice；
  正式性能结论必须用新schema重新运行等CPU多轮campaign。完整设计、实现和证据见
  [`Correctness_Hardening_2026-07-31.md`](Correctness_Hardening_2026-07-31.md)。
  当前等级I/T/E-mechanism。

## 295. F300：Executable-Bound Empirical Value Profiling and Sparse Input Correctness（2026-08-04）

- 正确性修复：simple backend 的输入表达式缓存从按最大offset扩张的`vector`改为按
  实际offset索引的`unordered_map`。旧实现首次访问高offset会在较小offset留下空
  `SymExpr`并把变量误命名为`stdin0`；现在变量名、重复访问和乱序访问均按真实offset
  稳定，空间由`O(max_offset)`降为`O(distinct_offsets)`；
- SOTA依据：FormaliSE 2026 profile-guided constraint simplification用Empirical Value
  Profiling识别有限动态值域。F300实现其中的观测与证据阶段，不复制论文的
  12.4%--85.2% KLEE求解时间结果；画像假设的本项目Z3消费由后续F301实现；
- 运行时画像：编译器已有立即数comparison和switch通知进入QSYM bounded profile。
  逻辑身份为`(executable_sha256, stable_site, bits)`；单进程最多512个profile、每个
  保存8个不同值，第9个值把profile永久标为saturated。switch只记录原site一次，
  Data Coverage派生探针不重复画像；
- 跨版本隔离：MPI worker流式计算实际目标可执行文件SHA-256并通过
  `SYMCC_VALUE_PROFILE_CONTEXT`传入。runtime要求telemetry、显式开关和合法64字符
  小写摘要同时存在，否则不收集。离线聚合器按context分区，缺失context的旧/外部
  记录不参与，防止不同二进制中位置相同而语义变化的site被合并；
- 可执行证据：`util/empirical_value_profile.py`限制输入文件数/大小和每记录profile
  数，跳过文件轮转、乱码和畸形JSON；只准入观察数达阈值、未饱和、不同值有界且频次
  完整的domain。输出对完整频次计算entropy、对饱和缺失频次显式写`null`，并包括
  排序profile、context集合、明确的
  `empirical-sat-accept-unsat-full-formula-fallback-v1`策略和canonical SHA-256；原子
  写入与独立`--verify`重建统计、准入和摘要；
- 可信边界：F300检查点只实现collection/parser/aggregation/verification；后续F301
  保持画像SAT仅作proposal并接受完整相关prefix验证，画像UNSAT或unknown去掉假设后
  重求原公式；
- 机制实测：真实QSYM目标产生3个profile，其中1个满足limited-domain准入；9次循环
  induction观测达到saturated并被拒绝，8次单值输入比较被准入，1次read结果比较因
  样本不足被拒绝；artifact经`--verify`通过，SHA-256为
  `848f621e1fdfe4b0c7772e1e55295c598baa2d25e806fd4d1752ed802bf4dfc3`。该数据是机制
  证据，不是性能提升；
- 测试与文档：新增simple稀疏1 TiB逻辑offset、真实QSYM画像/畸形context fail-closed、
  两个executable context不合并、饱和/篡改/CLI/atomic writer和telemetry解析测试；
  最终门禁为Python 495/495、LLVM 18 216 passed加1项预期unsupported、LLVM 17定向
  4 passed加1项预期unsupported及独立simple backend 1/1；
  完整实现、图示、研究边界和后续实验顺序见
  [`Continuous_Optimization_2026-08-04.md`](Continuous_Optimization_2026-08-04.md)。
  当前等级I/T/E-mechanism。

## 296. F301：Verified Empirical-Domain Solver Consumption（2026-08-04）

- 跨层缺陷修复：真实端到端测试发现，普通`icmp`画像使用comparison stable ID，
  `addJcc`使用branch stable ID，初版消费端虽`profiles_loaded=1`却`attempts=0`。
  Compiler现为普通conditional branch和非Hydra select额外发送path-constraint site画像；
  原comparison site继续服务Data Coverage，bitmap身份不变；switch原本已经对齐；
  通知携带symbolic expression，纯concrete比较不再挤占512-site画像上限；
- 严格制品：`empirical_value_profile.py --runtime-output`只从通过重建验证的JSON物化
  `symcc-empirical-value-runtime-v1`。C++ parser限制4 MiB、4096 profile、每行8值，
  严格检查schema/policy/lowercase SHA/site/width/canonical整数/互异值/精确行数和尾随
  token；聚合器和验证器另对跨文件不同key实施全局4096上限；任一错误整体fail closed。
  加载只接受与当前目标executable context精确匹配的
  `(site,bits)`；
- SAT-only求解：匹配目标必须是一个symbolic operand与一个constant operand的二元关系。
  runtime先用LLVM APInt按`eq/ne/ult/ule/ugt/uge/slt/sle/sgt/sge`精确过滤画像值；空域
  不调用Z3而直接回退。非空域查询同步完整相关prefix并求解`F ∧ D`，SAT模型还要经过
  expression evaluator复核目标与prefix，成功才写`evp-domain`候选；
- Sound fallback：经验域UNSAT/UNKNOWN、解析失败、context错配或validation失败均进入
  原optimistic/strict/backsolver链。经验UNSAT不进入poly/UNSAT-core cache，也不设置
  branch最终状态，因此不完整画像不会造成漏解；错误画像最多增加查询开销或失去加速
  机会；
- 遥测与协调器：新增loaded/context skips/parse failures、attempts/solver queries、
  SAT/validated/validation failures、UNSAT/UNKNOWN fallbacks，并由
  `hybrid_feedback.py`暴露`empirical_domain_solver` capability；
- 机制证据：真实QSYM equality fixture产生`000000-evp-domain`并验证后续三次经验UNSAT
  均完整回退；不含目标值的四次尝试由APInt预过滤为0次domain Z3，而原链仍生成4个
  candidate。context错配加载0行，畸形尾随token记录1次parse failure。有符号fixture
  从`{-1,1}`画像为输入5翻转promoted `(int8_t)value < 0`，结果attempt/query/SAT/
  validated/generated均为1；switch fixture只画像一次共享site，并在逐case求解中得到
  1次validated SAT和2次安全回退；
- 验证：双LLVM compiler/QSYM runtime及独立simple backend构建通过；最终Python为
  497/497（81.953秒），LLVM 18全量为218 passed加1 unsupported（134.51秒），LLVM 17
  为217 passed加2 unsupported（134.22秒），simple稀疏offset为1/1且新ABI分支程序
  compile/execute通过。完整门禁结果见
  [`Profile_Guided_Solver_Consumption_2026-08-04.md`](Profile_Guided_Solver_Consumption_2026-08-04.md)；
- 科研边界：当前是I/T/E-mechanism，不继承FormaliSE 2026的KLEE性能数字，也不声称
  LAVA-M、公开target coverage AUC或solver wall-time已提升。滚动profile发布由F302
  完成；仍需等CPU多轮消融、按site收益控制和完整campaign context。

## 297. F302：Versioned Online Empirical-Domain Feedback（2026-08-04）

- 集成缺口：F301已实现严格sidecar和SAT-only消费，但MPI helper只自动采集
  `empirical_value_profiles`，master不聚合/发布，worker也不设置
  `SYMCC_VALUE_PROFILE_IN`。F302把手工机制接成真实campaign的跨worker在线闭环；
- 在线协调器：新增`util/online_value_profile.py`，复用离线analyzer的严格规范化，维护
  默认256条、最大10000条滚动记录；按发布间隔聚合，artifact按canonical SHA-256命名，
  sidecar完整字节摘要作为代际版本。恢复时artifact必须重新验证且重新物化结果与
  `current.runtime`逐字节相等；窗口或准入策略变化会在首次分派前强制重新物化；最多
  保留8代并保护current；
- MPI协议：master随work lease发送版本，只有worker未确认该版本时才附带sidecar bytes；
  worker检查4 MiB上限和SHA-256后原子安装到本地目录；缓存命中也重新有界读取并校验
  本地摘要，随后在下一次`TAG_READY`回报版本。
  因内容随消息传输，跨主机不要求共享master文件路径；摘要错误、缺失内容或本地写失败
  不启用旧代际；
- 撤销语义：当已准入域饱和或超过distinct policy时，master发布合法
  `profile_count 0` tombstone而不是静默停止更新，使所有worker显式覆盖旧sidecar。
  counts变化但域语义未变只增加semantic no-op，不重复传输；
- 正确性与资源：publication I/O失败记录并保留dirty重试，不中止普通concolic；恢复
  checkpoint最多16 MiB，超限只裁最旧持久化记录而不改变内存窗口。portfolio context
  改按实际`execution_route.command`绑定，并以device/inode/size/mtime签名触发重新哈希，
  修复不同executor binary误用启动目标摘要；
- 自动化证据：新增6项online coordinator测试，覆盖跨worker准入、语义去重、换代/
  tombstone、context隔离、严格恢复、payload篡改、窗口边界与I/O fail-open；Python全量
  `503/503`（82.039秒），LLVM18全量219 passed加1 unsupported（138.48秒），LLVM17
  全量218 passed加2 unsupported（135.24秒），两版F300--F302过滤组均为`4/4`；
- 真实MPI证据：1 master + 1 QSYM worker的8秒机制运行收到17条记录，发布3个语义代际、
  14次no-op，实际加载4次、尝试/domain query 4/4、SAT/validated 1/1、经验UNSAT完整回退
  3次，最终发布0-profile tombstone；publication/validation/parse/context错误均为0，
  SymCC peer queue保留3个输入。原始state、三代artifact、sidecar、日志和哈希见
  [`F302 evidence`](evidence/f302-online-evp-2026-08-04/)；
- 科研边界：当前等级I/T/E-mechanism。单worker短窗口证明协议实际执行，不证明公开
  benchmark的solver wall-time、coverage AUC、CPU-hour或横向扩展提升；R级结论仍需冻结
  配置后对online=0/1做等CPU多轮配对消融。完整设计见
  [`Online_Empirical_Domain_Feedback_2026-08-04.md`](Online_Empirical_Domain_Feedback_2026-08-04.md)。

## 298. F303：Outcome-Adaptive Exact-Domain Admission（2026-08-04）

- 研究问题：F302只依据画像稳定性准入，一个小值域即使反复导致经验域Z3 UNSAT也会
  在每次worker执行中先支付无收益查询。F303新增按
  `(executable context,site,bits,exact values)`归因的在线结果反馈；画像从`[1]`演化到
  `[1,2]`时不继承旧域失败，防止跨域错误抑制；
- 运行时协议：QSYM为最多512个实际尝试域记录attempts、APInt prefilter rejects、
  solver queries、SAT、validated、validation failures、solver UNSAT和UNKNOWN；每行
  强制满足`attempts=prefilter+queries`、`queries=SAT+UNSAT+UNKNOWN`、
  `SAT=validated+validation_failures`。计数饱和，超界或损坏行不能成为策略证据；
- 准入策略：默认至少8次真实solver query且validated/query严格低于125000 ppm时
  抑制精确域；便宜prefilter reject不进入分母，阈值0关闭抑制，聚合溢出fail-open。
  证据驻留F302滚动窗口，旧失败离窗后域自动重新准入，实现有界重新探索而非永久
  denylist；
- 证明工件：新增`empirical-value-profile-v2`，封存阈值、精确key、八项计数、固定原因
  和实际runtime domain count。验证器重查摘要、v1画像可重建性、成员关系、无重复、
  守恒式和阈值证明；QSYM仍消费过滤后的runtime-v1文本，策略与求解器解耦。旧v1恢复
  或策略变化会使协调器dirty并在下一次分派前重新物化；
- 恢复事务加固：checkpoint新增`records_complete`和`current_artifact_sha256`。容量裁剪、
  旧状态、state/runtime代际错配、缺失state或缺失runtime均在下一次lease前重新物化；
  空恢复窗口发布tombstone，避免新sidecar与旧记录在崩溃窗口后被误当作一致快照；
- 编排控制：新增两个反馈阈值配置；关闭component adaptation时可用
  `SYMCC_SOLVER_COMPONENT=exact/learned/diverse`固定实验solver画像，开启适配时仍由
  portfolio选择，避免实验控制意外覆盖在线策略；
- 验证与真实机制证据：Python全量508/508（82.829秒），LLVM18为220 passed加1项
  unsupported（134.27秒），LLVM17为219 passed加2项unsupported（133.18秒），
  两版EVP过滤组均5/5。端到端脚本先准入2域，精确`[1,2]`
  产生2次真实query、2次solver UNSAT、0 validated后只抑制该域；抑制执行中该key为
  0反馈行；两条新记录使失败离窗并重准入2域，随后真实reprobe再次产生1次query。
  三代v2 artifact、原始telemetry、sidecar与哈希见
  [`F303 evidence`](evidence/f303-adaptive-evp-2026-08-04/)；
- 正确性边界：抑制只删除F301可选经验域探针，完整prefix、SAT模型复验、UNSAT/UNKNOWN
  完整公式回退和原cache语义均不改变。当前等级I/T/E-mechanism；机制实测不等于
  solver-time、coverage AUC或漏洞发现提升。完整设计和实验边界见
  [`Adaptive_Empirical_Domain_Admission_2026-08-04.md`](Adaptive_Empirical_Domain_Admission_2026-08-04.md)。

## 299. F304：Cost-Aware Exact-Domain Admission（2026-08-04）

- 研究问题：F303只按真实query数和validated比例抑制，把廉价失败与昂贵长尾视为
  等价。F304把每个精确域实际进入Z3 `check()`的累计微秒数加入充分证据，默认要求
  `queries>=8`、validated/query严格低于12.5%且累计成本至少1000 us；成本门0精确保留
  F303行为，ratio门0仍关闭抑制；
- 运行时协议：QSYM在经验域`check()`前后对已有`solving_time_`取非负差值，以饱和
  `uint64_t`更新全局`empirical_domain_solver_time_us`和最多512个精确域的
  `solver_time_us`。反馈行由11项扩为12项；原三个计数守恒式不变，时间只覆盖Z3
  check，不含公式构造、验证、完整回退、传输或重放；
- 证明与兼容：当前工件升级到`empirical-value-profile-v3`和
  `empirical-domain-admission-v2`，绑定成本门、每个抑制域成本和固定reason；验证器重查
  摘要、成员关系、守恒、比例、查询数、成本及runtime count。v1画像、F303
  v2/admission-v1和11项反馈仍可读；旧反馈补零，因此默认成本门下fail open；
- 在线事务：协调器checkpoint累计全局成本；恢复时旧schema或任一策略阈值变化都会
  dirty并在下次lease前重建。滚动证据老化、tombstone、SAT模型完整prefix复验和
  UNSAT/UNKNOWN完整公式回退保持不变。策略只变化而runtime域相同时也强制发布新证明
  代际；没有sidecar但有checkpoint记录时，重启主动重评，避免陈旧证明和遗漏准入；
- 自动化证据：成本单测覆盖800/1200 us跨门、999 us保留、proof成本篡改拒绝、v2兼容
  及恢复；双LLVM真实fixture要求12项反馈与正成本。相关Python为86/86、双LLVM过滤组
  均5/5；顺序全量为Python 511/511（82.076秒）、LLVM18 220+1 unsupported
  （137.88秒）、LLVM17 219+2 unsupported（139.25秒）；
- 真实机制对照：同一`[1,2]`域的两份真实telemetry产生2 query/2 UNSAT/0 validated和
  142 us。成本门0抑制后runtime由2域变1域；对同一反馈设置143 us门则保留2域。
  随后失败离窗、重准入和reprobe仍实际发生。原始JSON、v3工件、sidecar、checkpoint
  与SHA-256见[`F304 evidence`](evidence/f304-cost-aware-evp-2026-08-04/)；
- 科研边界：当前等级I/T/E-mechanism。142 us是机制夹具的单次本机测量，不是默认
  1000 us门的优化收益，也不证明LAVA-M/公开target的solver time、coverage或缺陷发现
  改善。完整说明见
  [`Cost_Aware_Empirical_Domain_Admission_2026-08-04.md`](Cost_Aware_Empirical_Domain_Admission_2026-08-04.md)。

## 300. F305：Replay-Verified Online EVP Recovery（2026-08-04）

- 审查问题：F303/F304恢复只验证state声明的artifact标签与runtime artifact一致，不能
  证明checkpoint中的滚动记录在当前策略下仍导出相同域；所有`_checkpoint()`返回值
  又被忽略，state写失败后可能清除dirty并停止主动重试；
- 恢复重放：正常publish与restart共享唯一`_build_candidate()`，依次执行bounded记录
  聚合、v3成本感知准入、验证和runtime物化。state/runtime都存在时，重建sidecar与
  磁盘sidecar比较除artifact摘要行外的完整求解语义；记录畸形、不完整、重放异常或
  域不一致均fail-open并在下次lease前重建；
- 持久化重试：`_checkpoint_or_retry()`覆盖零域、semantic no-op、发布错误和成功发布
  后的所有state写。失败增加`checkpoint_failures`并保持dirty；已发布runtime仍可用，
  下一次semantic no-op可只补写state。snapshot新增dirty、recovery replay/mismatch及
  checkpoint failure计数，旧state缺失字段以0迁移；
- 自动化证据：online suite增至14项。新测试保留匹配artifact标签但把合法记录`[1]`
  替换为`[2]`，要求restart产生1 replay/1 mismatch并发布`[2]`；另只对state原子写
  注入`OSError`，要求runtime发布成功、dirty/failure=1，随后无新telemetry重试落盘。
  相关Python 88/88、全量Python 513/513（84.787秒）；LLVM18 220+1 unsupported
  （138.10秒）、LLVM17 219+2 unsupported（138.78秒），两版完整lit均执行online测试；
- 可执行机制证据：独立驱动封存`[1] -> drifted [2] -> [2,3]`三代v3 artifact、重放前后
  sidecar、故障state、最终checkpoint、summary和逐文件SHA-256。最终状态为3 generations、
  1 replay、1 mismatch、1 checkpoint failure、1 semantic no-op和0 publication failure；
  见[`F305 evidence`](evidence/f305-replay-verified-evp-recovery-2026-08-04/)；
- 正确性与科研边界：F305保证恢复经验域同时由磁盘工件和存活记录重建语义支持；不把
  三文件变成跨文件原子事务，也不改变完整公式回退。当前等级I/T/E-mechanism，未测
  最大窗口启动成本或公开target性能。完整说明见
  [`Replay_Verified_Online_Profile_Recovery_2026-08-04.md`](Replay_Verified_Online_Profile_Recovery_2026-08-04.md)。

## 301. F306：Interleaved Fail-Closed Coverage Attribution Oracle（2026-08-05）

- 审查问题：历史 QA3 脚本会把 showmap 非零错误、畸形/空 map 或陈旧固定临时文件解释为
  无新覆盖；landing 与 exclusive-edge 又来自两个独立 campaign。MPI 流式 showmap
  客户端丢弃状态字、信任远端长度且没有独立读取 deadline，无法支撑严谨归因；
- 受界协议：抽出 `util/afl_streaming_showmap.py`，保留 status/detail/raw status、完整稀疏
  edge、stdout/stderr和兼容 `get_edges()`。输入、edge 数和辅助输出均有硬上界，单次读取
  有 target timeout 加5秒 grace，未知状态/保留位/短读 fail closed并且每次调用最多重启
  一次；真实fd错误不退化为阻塞读。文本map拒绝重复edge和u8范围外count，telemetry读取
  上限1 MiB；
- oracle 兼容性：每个 campaign 首个输入同时走流式 `-S` 和隔离 one-shot，状态与完整
  edge-ID集合必须都相同，否则关闭流式并让整个 campaign 统一回退 one-shot。真实XML
  fixture探针得到`timeout/933`对`ok/1019`，记录
  `status-mismatch+edge-set-mismatch`和1次fallback；
- 交错复测：默认3轮、轮间隔1秒，每轮对corpus做循环位移；每个输入保留全部副本集合，
  显式支持`strict`、`intersection`和`union`。默认strict遇到任一边漂移返回2；交集是保守
  欠近似，并集只表示可能覆盖；
- 同源证据：`run_qa3_coverage_campaign.py`从一次campaign同时导出landing、novel union和
  exclusive edges，保存轮次顺序、逐输入逐轮边集、输入/目标/showmap SHA-256、summary与
  内容清单。证据20输入×3轮共60次正式观测，另有2次兼容探针；11/20输入不稳定、46个
  不稳定边事件，baseline交集/并集1018/1020，显式交集下nominal landing 19/20、novel
  union 1378。单标签corpus不证明策略互补性或性能提升；
- 自动化：新增16项QA3测试，MPI编排测试增至26项，覆盖状态解码、超界响应、重启、严格
  map/telemetry解析、时序漂移、自动fallback、交错顺序、单源指标和最小CLI E2E；顺序
  全量为Python 532/532（83.282秒）、LLVM18 221 passed加1项预期unsupported
  （133.39秒）、LLVM17 220 passed加2项预期unsupported（133.89秒），Ruff全目录通过。
  当前等级I/T/E-mechanism；完整原始边集和科研边界见
  [`Interleaved_Fail_Closed_Coverage_Oracle_2026-08-05.md`](Interleaved_Fail_Closed_Coverage_Oracle_2026-08-05.md)
  与[`F306 evidence`](evidence/f306-interleaved-coverage-oracle-2026-08-05/)。

## 302. F307：Input-ABI and Terminal-Stratified Coverage Oracle（2026-08-06）

- 审查问题：F306证明了流式showmap失败不能被当作零覆盖，但真实复验继续暴露出输入
  ABI不一致：项目XML harness的persistent/stdin入口与`argv[1]`文件入口会产生不同边集。
  AFL++ 4.40c的`afl-showmap -S`和`-I`也不会安全地替换`@@` target占位符，`-S -- target @@`
  在该目标上只返回2条边。旧compatibility probe因此把file route和persistent route混在一起；
- ABI显式化：QA3新增`--input-mode auto|stdin|file`。auto只按项目AFL SHM/persistent
  marker推断stdin，否则选择file；通用第三方target需要显式指定。stdin模式的streaming
  probe必须与同ABI one-shot在status和完整edge-ID集合上完全一致，否则整个campaign统一
  one-shot fallback。file模式不创建streaming客户端，而是用绝对输入路径调用目标并关闭
  stdin；
- 终端分层：新增`--terminal-status-policy normal-only|stratified`。normal-only下任何
  candidate/baseline的crash、timeout或error都fail closed；stratified下terminal输出被计入
  terminal stratum，但不参与landing、novel union、exclusive edges或worker bitmap合并；
- AFL sparse map语义修复：`parse_sparse_edge_rows`区分“最多记录数”和“AFL map id上限”，
  edge id必须小于`1<<23`，hit count必须为1..255，重复edge、空map和畸形行都拒绝。MPI
  batch/streaming triage只在`ok`且边集非空时更新bitmap；batch fast path遇到placeholder不再
  猜测`@@`，one-shot才通过临时staged file替换placeholder；
- 同源证据：F307 evidence使用schema v2，`input_mode=stdin`、`terminal_status_policy=normal-only`。
  真实XML探针得到streaming`timeout/933`对one-shot stdin`ok/935`，因此oracle模式为one-shot、
  1次fallback。20个唯一测量输入中baseline不计入candidate，正式观测60次、probe 2次；
  baseline交集/并集934/935，20/20输入存在时序漂移、57个不稳定边事件。19个真实nominal
  candidate全部有seed-novel edge，landing率1,000,000 ppm，novel union 1374；
- 自动化与文档：QA3定向测试增至19项，MPI/AFL profile编排测试增至31项，Ruff定向通过。
  本轮顺序全量门禁为Python 540/540（83.144秒）、LLVM18 221 passed加1项预期unsupported
  （136.93秒）、LLVM17 220 passed加2项预期unsupported（138.14秒）。新增专题报告、
  机制示意图和`verify_delivery.py`复算器。当前等级I/T/E-mechanism；该证据
  只证明测量oracle正确性，不证明solver策略提速。完整说明见
  [`Input_ABI_Terminal_Stratified_Coverage_Oracle_2026-08-06.md`](Input_ABI_Terminal_Stratified_Coverage_Oracle_2026-08-06.md)
  与[`F307 evidence`](evidence/f307-input-abi-terminal-stratification-2026-08-06/)。

## 303. F308：Directed Edge-Utility Replay Scheduler（2026-08-06）

- 研究问题：现有`EdgeDependenceCoverage`只能把高价值种子重新放回队列，返回
  `ReplayJob(path, 0)`，worker无法知道应优先反转哪一个分支。对于最近directed hybrid
  fuzzing、target-centric seed scheduling和selective concolic testing关注的“边级冗余”
  问题，seed级重播仍会把多个已知边、已失败目标和同一输入路径重复求解；
- 边级目标提取：F308把`branch_trace`中的`site/taken`哈希成edge-dependence row，
  同时保存trace给出的opposite branch ID作为`target_branches`队列。每个row持久化
  `site_id`、目标游标、局部corpus、co-occurrence cell、`reward_ema`、`cost_ema`、
  `terminal_failures`、`successful_targets`和`target_distance`；SAT/UNSAT目标在再次观测
  后从所有row的目标队列中退休；
- directed utility模型：调度分数由under-explored row、co-occurrence结构量、
  `SYMCC_EDGE_DEP_DISTANCE`/`SYMCC_DIRECTED_DISTANCE`中的静态距离、收益/成本EMA、
  显式目标存在性、成功目标数、冷却年龄和terminal失败惩罚组成。没有距离图时策略退化为
  普通edge-dependence replay；有距离图时更靠近目标的site优先，但仍受失败和成本反馈抑制；
- MPI/worker接入：`AdaptiveHybridScheduler.observe()`把coverage delta、interesting case、
  elapsed和killed传给edge-dependence oracle；`replay_candidates()`现在会从F308返回
  `ReplayJob(path, target_branch)`，worker随后沿既有`select_strategy(target_branch)`和
  targeted concolic路径执行。普通corpus接纳仍由AFL edge/data coverage oracle决定，F308
  只改变“下一次求解哪个前沿”的排序，不绕过真实重放或coverage准入；
- 状态与兼容：scheduler snapshot升级为schema 13，旧schema 1--12继续可读；旧
  edge-dependence state缺失的新字段按空目标、0收益、0成本和0失败迁移。`to_mapping()`和
  `restore()`覆盖目标队列、游标、距离与反馈字段，支持长时间MPI campaign重启后继续边级
  调度；
- 自动化：`test_hybrid_feedback.py`新增三项F308测试，覆盖distinct context seed返回
  非零目标、静态距离优先选择near target、收益/成本/失败反馈在状态恢复后仍能优先fast
  target。scheduler级回归确认没有新AFL edge时也会生成`ReplayJob(seed, 3)`，并在snapshot
  记录`edge_dependence_targets`。本轮定向门禁为`ruff check util/hybrid_feedback.py
  test/test_hybrid_feedback.py`通过、`pytest test/test_hybrid_feedback.py -q`为45/45；
  完整Python回归`python3 -m unittest discover -s test -p 'test_*.py' -v`为542/542，
  86.687秒；
- 科研边界：当前等级I/T。它是调度机制改进，不是公开benchmark上的性能结论；仍需用
  F306/F307 oracle和等CPU多轮消融报告coverage AUC、redundant target solves、
  time-to-target、solver cost和terminal failure率。机制示意图见
  [`edge-utility-replay-2026-08-06.svg`](diagrams/edge-utility-replay-2026-08-06.svg)。

## 304. F309：In-Flight Target Leasing and Scheduler Integrity（2026-08-07）

- 审查问题：F308具备branch-targeted replay后，Prefix DAG、CSTG、hierarchical
  concurrency、edge-dependence和fallback replay仍可在结果返回前分别选择同一target；
  edge row裁剪还会遗留虚高`row_cells`，重复target observation会移动round-robin队列，
  空/截断`branch_trace`则分别造成terminal target不退休和CSTG误记divergence；
- 未完成工作占位：借鉴WU-UCT“unobserved samples必须进入下一轮选择状态”的原则，
  coordinator新增有界`target_leases`和`target_lease_groups`。`ReplayJob`的primary target
  与全部非`skip` S2F action作为一个原子group准入；任一成员已在途则抑制整组。默认
  120秒租约由`SYMCC_EDGE_DEP_TARGET_LEASE`配置，worker回报释放整组，失联由deadline
  自动恢复。该机制不复现WU-UCT公式或理论保证；
- 跨lane接入：Prefix DAG、CSTG和concurrency在本地打分前读取active target集合，统一
  出口再做group冲突检查；edge-dependence和普通replay使用同一registry。snapshot升级
  schema 14并导出inflight/reservation/suppression计数，schema 1--13继续读取。active
  lease不跨coordinator重启恢复，因为旧进程worker已经失效；
- 完整性修复：已有target重复观测改为no-op，退休时按删除位置修正cursor；branch row
  prune后从保留cell重建`row_cells`；restore先清空并拒绝孤儿cell，保证幂等；terminal
  退休要求`target_reached && status in {sat,unsat}`且不再依赖trace非空；CSTG在target
  outcome存在但trace截断时记录arrival/status而非divergence；
- 自动化：新增round-robin稳定、无trace terminal退休、跨row租约去重、expiry/release、
  S2F group释放、128-row裁剪计数、幂等restore/孤儿cell拒绝、CSTG截断trace和
  Prefix/CSTG跨lane去重测试。定向`test_hybrid_feedback.py`为53/53；调度/编排组合为
  89 passed加8 subtests，Ruff与compileall通过；
- 科研边界：当前等级I/T。单coordinator内的重复target分派被机制性抑制，但不同seed
  对同一target可能提供有价值的上下文多样性；多coordinator target-only租约、租约时长
  消融及coverage AUC/solver CPU收益仍待等CPU多轮实验。完整状态机、图和实验计划见
  [`F309研究报告`](research-progress/In_Flight_Target_Leasing_F309_2026-08-07.md)。

## 305. F310：Transactional Target Admission and Work-Conserving Backfill（2026-08-07）

- 审查问题：F309统一target group租约后，Prefix DAG、CSTG和concurrency的`select()`仍在
  全局租约准入前写入`attempts`、`last_scheduled`、`queue_epoch`或target cursor；冲突
  proposal虽未交给worker，却被错误计为已调度并进入cooldown。target lane又在收集到
  `limit`个proposal后提前停止，租约去重可能把2-worker批次缩成1个而不扫描已有备选；
- 无副作用proposal：三个选择器增加兼容的`commit=False`模式，保留评分、配额、seed去重和
  S2F action聚合，但不修改调度状态；新增各自`commit_jobs()`，只对成功获得group lease的
  `ReplayJob`提交attempt、时间戳、队列epoch和游标。默认`commit=True`保持直接调用兼容；
- 原子准入与回填：scheduler的`_reserve_target_jobs()`通过`on_accept`回调提交实际接纳集合。
  Prefix/CSTG target lane按原顺序交错proposal，但以lease成功数而非proposal数计算配额；冲突
  后继续扫描后备target，直至worker配额满足或候选真正耗尽。edge lane也改为lease成功后才
  更新row状态；
- 自动化：加强跨row同target回归，要求`scheduled`总和严格为1；新增Prefix/CSTG冲突零副作用、
  冲突后`[77,88]`配额回填、concurrency冲突记录不进入cooldown，以及失败action group不
  遮蔽同seed/primary的可行fallback。定向`test_hybrid_feedback.py`为56/56，调度/编排组合
  92 passed加8 subtests，完整Python 553/553（83.193秒），Ruff与compileall通过；
- 科研边界：当前等级I/T。F310借鉴WU-UCT未完成工作计数和Sparrow late binding的系统原则，
  但不复现其算法或理论保证；自动化结果证明状态一致性与有界工作保持，不构成coverage或
  solver吞吐提升。完整协议、图和实验指标见
  [`F310研究报告`](research-progress/Transactional_Target_Admission_F310_2026-08-07.md)。

## 306. F311：Cross-Coordinator Target-Group Fencing（2026-08-07）

- 审查问题：现有multi-master租约按完整work payload建立ID，不同seed求解同一branch target
  仍可由多个coordinator并发派发；scheduler在本地准入后应用agent hint，还可能把已租约的
  `target_branch/s2f_actions`改成另一组目标；
- target-only group表：新增`FencedTargetLeaseTable`。primary与全部非`skip` S2F action
  组成最多64成员的group，每个target映射到确定性SHA-256 shard。claim按digest全序取得全部
  短期mkdir锁，先检查全组再以相同opaque token发布记录；任一未过期成员使整组拒绝。普通
  写失败在持锁期尽力回滚，进程中途崩溃只可能留下由TTL回收的保守部分记录；
- current-token fencing：heartbeat/release必须匹配当前record token。过期后新owner覆盖记录，
  stale release不会删除新租约。token不是全局单调fencing number，target lease只抑制有效期内
  的合作式重复派发；极端暂停/时钟偏差仍可能产生重复计算，结果状态正确性继续由exact-work
  `begin_commit()`仲裁；
- 两级事务准入：scheduler新增`external_admission`，顺序为local reserve、shared claim、
  selector commit。共享拒绝或callback异常先释放本地group，Prefix/CSTG/concurrency和fallback
  的attempt、cooldown、epoch及cursor不前移。`proposal_limit`与worker `limit`分离，保留
  Prefix高低队列配额，同时在跨主冲突后扫描有界状态全集回填；
- MPI生命周期：候选token以pending状态跨轮携带并heartbeat；派发前重新验证，hint只可调整
  strategy/focus/parameter，不得替换已有target/actions契约；exact-work claim失败回滚target；
  成功后token转active，结果通过work fence和triage后释放。carried截断、stale result和正常关闭
  均清理对应token；周期日志报告磁盘lease及本地pending/active计数；
- 自动化：新增8项故障导向回归，覆盖重叠group跨实例互斥、无冲突部分发布、heartbeat/expiry、
  stale release、第二成员写故障回滚、外部拒绝零副作用回填、fallback游标保持、skip正规化和
  hint契约恢复，以及按两类TTL较小值调度heartbeat。组合为167 passed加12 subtests；完整
  Python为561/561（83.576秒测试计时，84.111秒shell墙钟），Ruff与`py_compile`通过；
- 科研边界：当前等级I/T。共享目录依赖合作coordinator、原子mkdir/同目录replace和可接受的
  时钟偏差，不是Chubby/共识服务或跨文件线性化事务。当前无真实双master共享NFS campaign，
  单元测试不构成coverage/solver吞吐提升。完整协议、失败模型、配置和实验设计见
  [`F311研究报告`](research-progress/Cross_Coordinator_Target_Fencing_F311_2026-08-07.md)。

## 307. F312：Failure-Atomic Lease Heartbeat and Fault Isolation（2026-08-07）

- 审查问题：target group heartbeat逐成员写`updated`，后续成员失败会形成不同expiry point；
  work/target heartbeat的共享存储`OSError`会穿透MPI主循环并终止coordinator；原子replace
  失败还会遗留随机名临时record；
- 失败原子续期：`heartbeat_group()`在全组token验证后保存独立record快照，只修改副本；
  任一成员写失败时仍持有全部有序锁并恢复已发布成员，使正常I/O故障后的group继续共享一个
  到期点。回滚再次失败只留下current-token保护的保守租约，由TTL恢复；该语义不是跨文件
  事务或共识；
- 故障隔离：MPI新增`_heartbeat_fenced_leases()`，逐条处理active exact-work、active target
  与pending target lease。每条`OSError`或stale token只进入`work/target ok/failed`守恒计数，
  不阻止其余独立租约续期；主循环输出聚合告警，pending派发前仍重新验证，active结果仍经过
  exact-work `begin_commit()`；
- 持久化清理：`_write_record()`用`published`状态和`finally`清除未完成fsync/replace的`.tmp`，
  保留原异常给上层协议处理；
- 自动化：新增3项故障注入测试，覆盖第二成员续期失败后的时间回滚与整组接管、work/target
  异常隔离和四类计数守恒、replace失败后无record/临时文件残留。定向组合为170 passed加
  12 subtests（5.81秒）；完整Python为564/564（83.089秒测试计时，83.632秒shell墙钟），
  Ruff与`py_compile`通过；
- 科研边界：当前等级I/T。测试证明状态机，不证明共享NFS吞吐、覆盖率或故障恢复时间；真实
  双master延迟/EIO/kill campaign仍待执行。完整协议、失败模型和实验计划见
  [`F312研究报告`](research-progress/Failure_Atomic_Lease_Heartbeat_F312_2026-08-07.md)。

## 308. F313：Transactional MPI Dispatch Handoff（2026-08-07）

- 审查问题：从任务选择到 `comm.send(TAG_WORK)` 返回之间，MPI master 会依次占用 target、
  exact-work、state-shard、参数/算法策略、seed-worker pairing 和 verified proposal 等异构
  资源。旧路径没有统一提交边界，send失败、共享work冲突或stale fenced result可能留下虚假
  pending/active状态；worker对象ID还可能在内容真正送达前被误记为已缓存；
- 三阶段交接：新增 `_DispatchReservationTransaction`，把生命周期显式划分为PREPARING、
  ACTIVE、COMMITTING和COMMITTED。每项reservation登记send前撤销与send后丢弃回调，逆序
  补偿且隔离单项异常；worker在三张事务表中至多出现一次，`begin_commit()`成功后才从
  active迁移到committing，triage及资源完成全部走完后才`commit()`；
- 资源专用语义：新增current-token shared-work `abandon`、本地journal持久化`reclaim`、
  state task `abandon/discard`、self-config token `abandon`、algorithm stage `abandon/discard`、
  seed-worker `release/discard`和proposal `abandon_dispatch`。未发送路径回退虚假计数及阶段，
  已执行但结果失信路径保留真实attempt/assignment并拒绝学习反馈；
- 失败原子细节：local journal先追加事件再修改内存；CAS worker cache只在send返回后更新；
  派发调用点对非send准备异常也立即扫描三张事务表并补偿。algorithm精确回滚恢复stage、
  prior统计、RNG和`sequence_number`，连续LIFO撤销可生成相同重放token；
- 自动化：新增10项状态机/故障注入回归，覆盖journal append失败、current-token/precommit门、
  state未发送与已执行语义、parameter/algorithm/seed-worker/proposal守恒、阶段化逆序补偿、
  补偿异常隔离、跨registry保守清理和LIFO token重放。定向组合为213 passed加12 subtests
  （5.98秒），完整Python为574/574（82.846秒测试计时，83.376秒shell墙钟），Ruff与
  `py_compile`通过；
- 科研边界：当前等级I/T。MPI send异常具有投递歧义，F313选择撤销fence并拒绝可能晚到结果，
  可能浪费一次计算但不提交失信反馈。本实现不是跨文件/覆盖状态的两阶段提交，也尚无真实
  多rank失联、EIO或kill campaign。完整协议、失败模型和实验计划见
  [`F313研究报告`](research-progress/Transactional_MPI_Dispatch_Handoff_F313_2026-08-07.md)。

## 309. F314：Reboot-Safe Local Lease Fencing（2026-08-07）

- 审查问题：默认本地`WorkLeaseJournal`和`StateShardCoordinator`允许第二个worker覆盖仍活动的
  同一ID，ownerless completion/rollback又可删除后来owner的状态；两者还把monotonic timestamp
  持久化，跨系统重启后旧uptime可能处于新clock的未来，连zero-TTL resume也无法恢复；
- 单所有者准入：work/state lease已有活动ID时无副作用拒绝。work `complete/abandon`与state
  `complete/abandon/discard`按master记录的worker校验；MPI结果批次携带source rank，错误或晚到
  owner不能完成、撤销或覆盖当前lease。state准入冲突进入F313统一rollback栈；
- 重启安全clock：新work事件和state snapshot标记`clock=unix`并使用`time.time()`；compaction
  保留legacy clock标记。zero-TTL恢复无条件回收，无标记旧记录视为跨epoch stale。coordinator
  restart保留state reward/completion/failure统计，但使旧MPI rank lease立即过期；
- 权威身份：master活动`state_task_id`优先于worker回传字段，不一致只记录告警；只有旧协议中
  master无活动ID时才兼容采用report。state提交因此同时绑定master task ID和MPI source rank；
- 自动化：新增6项回归，覆盖work/state重复准入、错误owner完成/撤销、legacy future clock、
  zero-TTL future record、Unix默认时钟、跨coordinator epoch失效和worker state-ID错配。定向
  组合为123 passed加12 subtests（5.69秒），扩展的F313/F314相关组合为219 passed加
  12 subtests（6.23秒）；完整Python为580/580（81.846秒测试计时，82.385秒shell墙钟），
  Ruff与`py_compile`通过；
- 科研边界：当前等级I/T。机制预计减少重复solver work并提高恢复可信度，但尚无真实reboot、
  delayed-result或等CPU coverage实验。当前rank owner依赖“一rank一活动事务”协议；若未来允许
  pipeline，应升级为`(rank, dispatch_generation)`。完整说明和协议图见
  [`F314研究报告`](research-progress/Reboot_Safe_Local_Lease_Fencing_F314_2026-08-07.md)。

## 310. F315：Dispatch-Generation Result Fencing（2026-08-07）

- 审查问题：F314仍以长期MPI rank作为result owner。master收到`TAG_RESULT`后先按rank弹出
  active work/state/target/策略上下文，shared exact-work fence又是可选配置；idle rank的
  未受托结果或rank复用后的迟到结果因此可能被错误归因到当前任务并进入triage/学习反馈；
- 代次身份：master启动时生成256-bit随机epoch并维护单调dispatch sequence，按
  `SHA256(v1 || epoch || worker || sequence)`派生64位hex token。每个
  `_DispatchReservationTransaction`强制持有合法token，`TAG_WORK`携带同一值；token不进入
  语义work payload/hash，重试仍保持稳定work ID；
- 全出口回显：worker严格校验WORK token，continuation、输入物化失败、普通执行成功及弹性异常
  的三个发送出口统一经过`_send_dispatch_result()`。sender复制结果并用master token覆盖任何
  上层字段，避免遗漏或误写；
- 前置提交门：master在任何`active_*` pop、lease `begin_commit`、generated统计、proposal验证、
  agent/solver观察及triage之前，将结果分类为`current/unowned/missing/malformed/stale`。只有
  current进入原提交链；其他结果保持当前代次状态并按原因隔离；
- 可观测性：`Stats`新增quarantine总数和原因计数，周期stats文件及最终摘要稳定输出。正常运行
  期望为0，非零值作为协议错配、迟到或编排故障信号，不作为coverage指标；
- 自动化：新增4项token唯一性/代次错配、畸形fail-closed、统一sender权威覆盖及分类统计回归。
  MPI/AFL profile编排为42 passed加8 subtests（0.52秒）；分布式状态、策略、反馈、proposal与
  MPI组合为223 passed加12 subtests（6.03秒）；完整Python为584/584（81.618秒），Ruff与
  `py_compile`通过；
- 科研边界：当前等级I/T。token是同一MPI作业内的关联键，不是认证、共识或全局单调fence；
  protocol-corrupt current result当前会保守隔离但没有独立per-dispatch timeout/requeue。真实
  多rank延迟、重复、kill、restart与混合版本campaign尚未执行，不宣称coverage或吞吐提升。
  完整协议、图和实验计划见
  [`F315研究报告`](research-progress/Dispatch_Generation_Result_Fencing_F315_2026-08-07.md)。

## 311. F316：Generation-Aware READY Join and Persistent Dispatch Recovery（2026-08-07）

- 审查问题：F315拒绝missing/malformed current RESULT后会保留ACTIVE事务，但READY只携带
  rank和coverage/profile版本，master不能确认worker完成的是哪个派发代次。RESULT与READY又使用
  不同MPI tag，tag-specific probe可先观察READY；直接按接收顺序idle或回滚会覆盖当前状态或在
  合法RESULT仍排队时产生假恢复；
- 双消息代次协议：worker的统一result sender返回权威token，下一条READY回显
  `completed_dispatch_token`并在发送后清空。`_DispatchGenerationGate`分别保存同代READY和
  missing/malformed RESULT事实；master先排空READY、再排空RESULT、最后sweep，因而两种接收
  顺序等价。合法current RESULT清除invalid；stale result绝不污染当前代次；
- fail-closed READY：按idle/current/missing/malformed/stale/unowned分类。活动代次的current
  READY只登记join、不能立即idle；其他异常READY不修改bitmap/profile version或当前事务。
  `bitmap_version`非法类型、布尔值、负向越界和未来代次安全回退为-1，不再穿透master循环或
  阻止后续完整bitmap同步；
- 精确恢复：仅`ready[rank]==T && invalid[rank]==T`触发。`_rollback_owned_dispatch()`同时匹配
  rank和exact token，调用F313 post-send补偿并清除work/strategy/hash/lease fence/target/state/
  schedule/agentic/component等side table；state协调器随后立即落盘。语义恢复payload包含
  path/focus/target/action seed/schedule/continuation，但排除transport token以保持work ID稳定；
  普通replay要求seed仍存在，自包含continuation则允许在AFL删除旧路径后从CAS状态恢复；
- 有界与持久活性：`SYMCC_DISPATCH_PROTOCOL_RETRIES`默认1、范围0--16；预算内插回当前work
  frontier，耗尽后写独立`.dispatch_protocol_deferred.jsonl`，`SYMCC_RESUME=1`时zero-TTL恢复。
  journal写失败回退内存重排队而非静默丢任务。stats新增READY quarantine原因、protocol
  requeued和deferred守恒计数；
- 自动化：新增6项乱序join、READY分类、错代际原子回滚、稳定语义payload、journal重启恢复和
  统计回归。MPI/AFL profile编排48 passed加8 subtests（0.55秒）；MPI、distributed state、
  agentic、hybrid feedback与proposal六模块253 passed加12 subtests（13.00秒）；Ruff和
  `py_compile`通过；完整Python为590/590（83.320秒）；
- 科研边界：当前等级I/T。worker在RESULT/READY之前失联仍缺少per-dispatch timeout或ULFM
  failure detector；重试预算为coordinator进程内边界；尚无真实多rank重排、kill、EIO、restart
  campaign，不宣称coverage、吞吐或恢复延迟提升。完整状态机、图和实验边界见
  [`F316研究报告`](research-progress/Generation_Aware_READY_Recovery_F316_2026-08-07.md)。

## 312. F317：Generation-Fenced Dispatch Watchdog and Worker Rehabilitation（2026-08-07）

- 审查问题：F316只能在worker已经发送READY和畸形RESULT时恢复；worker在任何完成消息前静默
  hang会永久占用ACTIVE事务及work/state/target/策略资源。超时后直接把同一rank重新idle又会
  让仍在运行的旧代与新派发交错，timeout suspicion不能当作进程死亡证明；
- 事务deadline：`mark_dispatched()`仅在TAG_WORK send返回后记录monotonic时间，PREPARING、
  COMMITTING和closed事务不参与watchdog。`SYMCC_DISPATCH_WATCHDOG_SEC`默认
  `max(120,4*SYMCC_TIMEOUT)`、范围0--86400，0关闭；NaN/Infinity/非法值回默认。RESULT和F316
  recovery先于sweep，已有同代READY的事务不因跨tag可见性差异误超时；
- exact recovery：超时快照携带rank与token，继续使用F313 post-send补偿和F316全side-table
  清理/state快照。抽出`_enqueue_dispatch_recovery()`统一invalid-result与watchdog的稳定语义
  ID、立即重试、deferred JSONL和持久化失败内存fallback；运行期shared-lease stealing也改为
  允许自包含continuation在原seed删除后恢复；
- rank隔离：`_RetiredDispatchGate`在timeout后保存`parked[rank]=T`，派发入口拒绝parked rank。
  迟到RESULT仍被generation fence隔离，missing/malformed/other-token READY继续parked；只有
  retired exact-token READY证明旧worker循环结束，随后才接纳有界版本元数据并恢复idle；
- 可观测性：stats新增watchdog timeout与worker recovery计数；requeue/defer总数覆盖两类恢复，
  master结构日志用`source=invalid-result/watchdog-timeout`区分原因；
- 自动化：新增3项deadline/config边界、parked exact READY恢复和retry/defer/EIO不丢任务测试，
  并扩展stats回归。MPI/AFL profile编排51 passed加8 subtests（0.56秒）；六个相关模块
  256 passed加12 subtests（13.28秒）；完整Python为593/593（82.938秒）；Ruff与
  `py_compile`通过；
- 科研边界：当前等级I/T。固定timeout误判只由fencing转化为额外计算，未实现MPI membership、
  revoke/shrink或rank replacement；真正dead rank仍parked，默认MPI runtime也可能直接终止job。
  尚无真实hang/kill/网络长尾campaign，不宣称coverage、吞吐或恢复时间提升。完整设计、文献
  定位、协议图和实验计划见
  [`F317研究报告`](research-progress/Generation_Fenced_Dispatch_Watchdog_F317_2026-08-07.md)。

## 313. F318：Acknowledged Bounded MPI Shutdown and Finalization（2026-08-07）

- 审查问题：F317能隔离运行期静默worker，但三条master退出路径仍对所有rank阻塞
  `comm.send(TAG_STOP)`，随后所有进程无条件进入阻塞`Barrier()`。parked、busy或失联rank可能
  没有posted receive；捕获已返回的MPI异常不能给阻塞调用设置deadline，运行期活性修复会在
  关闭阶段重新失效；
- 三阶段停止协议：新增`_ShutdownGenerationGate`，按随机epoch和rank派生exact shutdown token。
  master只有观察到READY或已有权威idle记录才以`isend`发送一次tokenized STOP；worker严格校验
  schema/目标rank/token，在profile、showmap、live-state及临时目录全部清理后回显TAG_STOP_ACK。ACK必须
  同时匹配MPI source、载荷rank和exact token，missing/malformed/stale/unowned确认均不完成rank；
- 有界反压与退出：关闭循环每rank、每tag按有界批次排空RESULT/READY/ACK，使可能卡在结果发送
  的worker可到达receive boundary，同时不被异常消息洪泛垄断。`SYMCC_SHUTDOWN_GRACE_SEC`默认
  `max(120,4*SYMCC_TIMEOUT)`；deadline后pending rank触发rank 0 `Abort(70)`。全ACK后使用
  `Ibarrier()+Request.Test()`，`SYMCC_FINALIZE_GRACE_SEC`默认30秒，超时`Abort(71)`；
- 一致性修复：dispatch recovery只有在exact owned-state rollback成功后才retire generation gate；
  side-table不一致时保留READY/invalid-result证据，不把未回滚状态伪装成已完成；
- 自动化与真实协议证据：新增3项状态机、仿真silent-rank和bounded barrier回归。MPI/AFL profile
  编排54 passed加8 subtests（0.55秒），六个相关模块259 passed加12 subtests（13.29秒），完整
  Python 596/596（83.630秒）。Open MPI 4.1.6真实1 master + 2 worker测试得到2/2 exact ACK、
  pending为空、有界barrier成功；完整helper早期失败入口退出码0；静默rank注入在0.150064秒
  报告`acked=(1), pending=(2)`并由框架Abort(70)，未触发5秒外层timeout；Ruff与`py_compile`通过；
- 科研边界：当前等级I/T/E-mechanism。真实测试只证明健康MPI栈上的协议可执行，不证明rank kill、
  网络分区或故障恢复性能；Abort是有界失败终止而非communicator repair，ACK token也不是认证或
  共识fence。完整状态机、图和故障注入计划见
  [`F318研究报告`](research-progress/Acknowledged_Bounded_MPI_Shutdown_F318_2026-08-07.md)。

## 314. F319：Shared MPI Lifecycle and Ordered Quiescence（2026-08-07）

- 审查问题：F318只完整收敛AFL协同入口，standalone `mpi_concolic_execution.py`
  仍使用阻塞STOP、结果/统计收敛和`Barrier`；两个前端各自复制生命周期语义。
  更隐蔽的是master先按TAG_READY派发、后按TAG_RESULT收集，不同tag选择性接收可先
  观察下一轮READY，使新active ownership被旧RESULT错误清除；`--max-idle`还以5秒
  轮次取整，1秒配置会立即退出却报告5秒；
- 共享协议：新增`util/mpi_lifecycle.py`，集中tag、shutdown token/ACK分类、有界
  worker shutdown和Ibarrier；两前端导入同一函数对象。`clean`现在要求pending、
  communication error和result callback error同时为空，不会在曾发生MPI异常后因偶然
  收到ACK而伪装成功；
- 顺序无关join：`_WorkerAvailabilityGate`停泊提前READY，仅从`ready - active`认领
  worker。主循环与shutdown drain共用严格结果消费函数：64位小写十六进制hash、
  非布尔非负生成数和source-owned active任一不成立即fail closed；关闭期晚到RESULT
  因此仍进入生成数、interesting hash和跨master同步记账。畸形/未拥有结果的rank被
  禁止再claim，但READY仍交给shutdown gate清理，不在最终Abort前继续派发；
- master收敛：新增有界`_bounded_master_stats_exchange()`。submaster用随机token非阻塞
  发布strict nonnegative stats；root按已知source每peer只接纳一次，在晚到hash send全部
  完成后回exact-token ACK。任何阶段失败都在`group_comm.Free()`前`Abort(70)`；
  finalization仍由共享有界Ibarrier约束；
- 时间语义：`_idle_time_remaining()`改用monotonic deadline，新工作重置idle window，
  最后一次sleep只等剩余时间，日志报告实测elapsed和limit；
- 自动化与真实协议证据：MPI lifecycle/AFL编排63 passed加13 subtests（0.55秒）；
  七个相关模块268 passed加17 subtests（13.22秒）；完整Python为605 passed加
  37 subtests（83.07秒）。Open MPI 4.1.6真实单master
  2/2 ACK和双master各3/3 ACK均exit 0，`--max-idle 1`均准确报告1.000秒；静默
  stats peer使root在0.150484秒报告`received=(1), pending=(2)`并Abort(70)，未触发
  5秒外层timeout；
- 科研边界：当前等级I/T/E-mechanism。真实双master对单seed报告2次analysis
  observation，暴露现有hash broadcast仍是复制探索而非全局work ownership；该现象
  作为下一轮去重路由基线，不写成F319性能收益。未实现ULFM revoke/shrink、
  分布式终止检测或rank replacement。完整协议、图、实测与后续计划见
  [`F319研究报告`](research-progress/Shared_MPI_Lifecycle_and_Ordered_Quiescence_F319_2026-08-07.md)。

## 315. F320：Lease-Fenced Global Work Ownership and Two-Phase Quiescence（2026-08-07）

- 审查问题：F319真实双master对一个content-addressed seed产生2次analysis observation，
  证明旧hash broadcast把同一frontier复制给所有worker group；本地hash set无法解决跨进程
  并发claim、owner暂停后的接管、旧结果晚到和全局静止竞态；
- 全局放置与围栏：standalone多master以SHA-256内容摘要为work identity，使用HRW/
  rendezvous hashing选择首选master，再由job-epoch-scoped `FencedWorkLeaseTable`原子claim。
  pending/active/committing token统一heartbeat；RESULT必须通过current-token
  `begin_commit`后才可发布子任务，`complete`永久关闭work。过期记录可由任意master接管，
  stale token结果不能完成新owner；缺失corpus文件会abandon新取得的pre-commit token；
- 共享发现与内容闭环：移除复制hash broadcast，各master扫描共享内容语料库但只claim自己的
  rendezvous分片。worker复制输入后重新计算SHA-256，并在所有RESULT路径回显；master把摘要
  与active assignment绑定校验，复制失败、不可读或内容/文件名不一致均fail closed；
- 两阶段静止：master发布严格递增的idle/busy status。root在全体fresh stable-idle达到
  `--max-idle`后发随机token PREPARE；submaster重扫外部输入、共享corpus和过期lease后投票，
  YES即冻结discovery，BUSY触发exact ABORT。全YES后COMMIT，root必须收齐每个submaster的
  exact COMMIT ACK才可进入worker shutdown；commit后busy或拒绝ACK是控制面冲突；
- 有界锁与可观测性：`FencedWorkLeaseTable`新增monotonic lock-acquisition deadline，避免
  陈旧或不可删除lock目录造成永久等待。新增5个standalone配置，最终stats给出per-master
  analysis分布，成功final barrier后rank0清理`.standalone-work-<epoch>`；
- 自动化与真实协议证据：lifecycle+distributed-state为100 passed加9 subtests（5.62秒），
  完整Python为611 passed加37 subtests（84.28秒），Ruff、`py_compile`与diff check通过。
  Open MPI 4.1.6双master下，单seed从F319的2次降为1次；24个不同seed严格24次且
  master分布12/12；12个seed均生成同一child时总分析13、generated 13、interesting 1，
  分布6/7；全部3/3 worker ACK、exact quiescence/stats ACK、exit 0，隐藏lease目录为0；
- 科研边界：当前等级I/T/E-mechanism。上述数据证明冗余消除、无漏任务和协议可执行，不是
  coverage/throughput R级结论。系统依赖共享POSIX文件语义，目录扫描仍为O(N)，PREPARE后
  新到外部输入留给下一campaign，未实现ULFM revoke/shrink或rank replacement。完整方案、
  状态图、配置和实验边界见
  [`F320研究报告`](research-progress/Lease_Fenced_Global_Work_Ownership_F320_2026-08-07.md)。

## 316. F321：Failure-Atomic Frontier Admission and Symmetric Quiescence PREPARE（2026-08-07）

- 审查问题：F320的root在PREPARE前没有执行submaster已有的三源重扫，边界时刻归root的输入
  可能留到下一campaign；畸形worker RESULT会先移除active assignment，却留下持续heartbeat的
  token，形成pending/active之外的悬空任务；corpus文件名、恢复payload和按年龄删除的短锁也
  尚未形成端到端身份闭环；
- 可验证准入：`_atomic_write()`使用PID、monotonic序号和随机量构造独占临时文件，write/flush/
  file-fsync/atomic-replace任一失败向上传播并清理临时文件。外部seed、共享扫描、过期恢复由
  master执行SHA-256回验，worker私有复制后再次回验；暂时不可读的外部文件不提前加入已导入集；
- 子结果隔离：worker把child写入job-private、global-rank/per-result隔离的隐藏stage并只回传
  `staging_id`与hash。master要求stage普通文件集合与声明集合严格相等，逐文件回验摘要；无owner、
  stale或畸形RESULT先清理stage，只有父任务exact-token fence成功后才提升到公开corpus；
- 恢复身份：`FencedWorkLeaseTable`严格验证schema、authoritative record id、状态、payload、
  非空token和有限非负Unix时间；已存在的损坏record不可被当作空记录覆盖。新
  `recover_expired_records()`返回`(record_id,payload)`，standalone仅在
  `payload.hash == record_id`时reclaim，NaN/Infinity和payload重定向均fail closed；
- 失败原子执行：无效RESULT先回验corpus。对象完整时隔离故障worker，并只在仍持有同一lease
  token时把exact assignment插回队首；token已替换则按stale处理，健康worker容量耗尽则立即
  control failure。语义是at-most-once commit与可能重复的fenced execution，不冒充
  exactly-once execution；
- 不可逆commit fence：复核发现`begin_commit`后token已离开active续租集合，长triage或多child
  发布可能跨过TTL并被另一master偷取，导致旧holder恢复后双写。当前只有`leased`可过期恢复；
  `committing`不再计入expired或允许新claim。该选择消除TTL双写窗，但commit后崩溃会保守阻塞，
  自动恢复仍需持久result manifest与逐副作用重放日志；
- 对称静止：root达到stable idle后与submaster一样顺序刷新外部输入、共享corpus和过期lease。
  刷新发现工作则不生成probe token、重置idle window并强制发布BUSY；刷新后仍空才进入
  PREPARE/YES冻结。由此root和peer共享同一本地线性化条件；
- 锁正确性：取消依据wall-clock年龄`rmdir`短update lock。暂停holder不等于死亡，时间阈值无法
  fencing其后续写；当前用monotonic acquisition deadline有界fail closed，`lock_ttl`只保留作
  兼容/诊断年龄提示；状态表构造器本身拒绝NaN/Infinity lease、年龄提示与acquisition timeout，
  避免绕过上层配置过滤后重新引入无界等待；
- 自动化与真实证据：lifecycle+distributed-state为107 passed加10 subtests（5.51秒），完整
  Python为618 passed加38 subtests（82.66秒），Ruff与`py_compile`通过。Open MPI 4.1.6健康
  双master对12 seeds严格12次分析、6/6分布、双方3/3 ACK、exit 0；预置合法摘要文件名但错误
  内容时在派发前报告expected/observed摘要，2/2 ACK后Abort(70)，未产生analysis observation；
  另一次真实双master暂存实验中12 seeds生成同一child，最终corpus 13、analysis 13、unique child
  1、master 0/1为7/6，两组3/3 ACK且隐藏job-state目录清零；单master 2 seeds暂存场景为corpus/
  analysis=3、unique child=1、2/2 ACK、精确idle 1.000秒且job-state目录清零；
- 科研边界：当前等级I/T/E-mechanism。原始日志和SHA-256清单证明机制行为，不证明真实目标
  coverage或throughput提升；尚未注入畸形RESULT、rank pause、NFS/Lustre故障或掉电。mkdir
  mutex和commit后崩溃残留当前选择安全失败，自动释放/事务重放后端需在目标共享文件系统上验证。完整方案、
  机制图和证据见
  [`F321研究报告`](research-progress/Failure_Atomic_Frontier_Prepare_F321_2026-08-07.md)。

## 317. F322：Durable Result Manifest and Idempotent Corpus Redo（2026-08-07）

- 审查问题：F321正确地让`committing`不可按TTL偷取，却没有保存commit后崩溃所需的child集合、
  stage identity和generated计数；进程在部分corpus promotion后退出会永久阻塞。显式重启还会
  等待旧pre-commit lease的120秒TTL，未来时间戳可进一步延迟恢复；
- WAL-inspired decision：standalone RESULT经严格stage验证后构造canonical
  `symcc-standalone-result-commit-v1` manifest，记录worker global rank、32位stage ID、排序去重
  child hashes和非负generated数。`begin_commit`把manifest与`leased→committing`同一次原子record
  替换持久化；同token重试只接受完全相同manifest；
- 幂等redo：恢复扫描严格绑定record ID、payload hash、token和manifest，验证每个声明对象位于
  `stage ∪ public`且摘要正确。promotion接受已存在的同摘要对象；并发helper观察source消失时只有
  destination摘要精确匹配才继续。`complete_once`区分`completed/already/stale`，仅唯一
  `completed`胜者恢复generated/analysis记账并claim children；
- 显式epoch：新增`SYMCC_STANDALONE_WORK_EPOCH`，rank 0校验并广播64位小写hex；不可变
  `state.json`绑定schema、epoch和shard数。恢复先redo committing，再对剩余leased执行TTL 0扫描
  和TTL 0 claim，最后导入新输入；即使旧Unix时间戳在未来也可立即重新fencing。单master同样使用
  durable coordinator；
- fail-closed：现存但损坏的record或缺失durable manifest的committing record触发完整性错误，
  同轮后续frontier修改短路，worker有界ACK后Abort(70)，epoch目录保留。周期扫描与PREPARE边界
  均commit-first，未处理decision不能被静止投票越过；
- 自动化与真实证据：Ruff、`py_compile`通过；定向111 passed加10 subtests（5.45秒），完整Python
  622 passed加38 subtests（84.16秒）。Open MPI 4.1.6健康单master为3 observations/2-of-2 ACK，
  健康双master为13 observations、8/5分布、6-of-6 ACK；future-clock lease在120秒TTL下立即得到
  `recovered=1`；双master部分publish恢复得到`replayed_commits=1`、2 generated、3 observations、
  2/1分布；缺manifest场景0次analysis、2/2 ACK后exit 70且状态目录保留；
- 科研边界：当前等级I/T/E-mechanism。它是ARIES/持久数据流启发的redo子协议，不包含undo/CLR、
  原子多文件可见性、机器掉电目录屏障、全局exactly-once execution、历史统计重建、AFL triage
  recovery或同epoch并发membership。原始日志只证明恢复正确性，不证明coverage/throughput提升。
  完整方案、机制图和SHA-256 evidence见
  [`F322研究报告`](research-progress/Durable_Result_Manifest_Replay_F322_2026-08-07.md)。

## 318. F323：Directory-Entry Durability Barriers（2026-08-07）

- 审查问题：F322的file fsync加atomic rename只证明内容完整和进程可见性，没有同步包含final
  name的目录；主机掉电后manifest/lease/corpus目录项仍可能消失。跨目录stage promotion又同时
  修改public和staging两个目录，只同步一侧不能证明完整移动；
- 持久化原语：新增严格`fsync_directory`、逐级`durable_makedirs`、同/跨目录
  `durable_replace`、`durable_link`和`durable_unlink`。新目录逐级同步自身与父目录；hard link和
  删除在返回前同步目标父目录。目录open/fsync错误不降级，沿协议路径传播；
- 有序跨目录提交：`stage/H -> public/H`固定执行rename、public-dir fsync、staging-dir fsync。
  public屏障先成功可保证第二屏障失败时公开对象仍耐久；源名称即使重现，也能由F322的SHA-256
  联合验证和幂等redo收敛。两个屏障完成后才允许父任务继续完成；
- 接入范围：content-addressed input/live-state对象、coverage shard/heartbeat、append lease
  journal、fenced work/target record、epoch hard-link metadata、worker stage目录、standalone
  corpus原子写与promotion均接入。短锁和per-run临时目录刻意保持易失；dedup ledger仍是
  best-effort提示，不能承担授权语义；
- 自动化与系统证据：Ruff、`py_compile`、`git diff --check`通过；新增故障路径7/7；定向
  116 passed加10 subtests（13.63秒）；完整Python 627 passed加38 subtests（92.05秒）。Linux
  `strace`确认file fsync、rename、public目录fsync、staging目录fsync的实际次序；Open MPI
  4.1.6单master为3 observations/2-of-2 ACK，双master为13 observations、5/8分布、6-of-6 ACK，
  两者exit 0且清理epoch状态；
- 成本与边界：overlayfs 4 KiB、5轮共500样本的post-file-fsync微基准中，仅rename中位数
  17.236 us，耐久replace中位数2897.566 us、P95 2991.771 us，中位新增2880.331 us。这是可靠性
  成本，不是吞吐收益。当前等级I/T/E-mechanism；尚未真实物理断电，不证明控制器flush诚实、
  网络分区恢复、原子多文件可见、全局exactly-once或coverage提升。完整说明、示意图和原始证据见
  [`F323研究报告`](research-progress/Directory_Entry_Durability_Barriers_F323_2026-08-07.md)。

## 319. F324：Shard-Level Durable Heartbeat Group Commit（2026-08-07）

- 审查问题：F323在本机量化出每次parent-directory屏障约2.88 ms固定成本，但standalone和
  hybrid heartbeat仍按活动record逐条同步；同一master的多条lease即使位于相同shard，也会在每个
  周期重复flush同一目录。target-group续租还有相同写放大；
- 两层group commit：新增same-directory-only `_DirectorySyncBatch`和结构化
  `LeaseHeartbeatBatch`。每条record仍单独lock、current-token验证、temp write、file fsync和atomic
  rename；`heartbeat_many()`按`(shard,id)`排序并去重parent path，在自然heartbeat快照末尾每个实际
  命中shard只执行一次directory fsync，不引入人为commit delay；
- 失败边界：第N条写失败时`finally`先flush此前已rename前缀再抛错；任一directory fsync失败时
  整批不返回成功。stale token只进入`lost`且不触发写。batch在rename前拒绝跨目录source/destination，
  F323的`stage -> public`仍严格保持destination-first/source-second屏障；
- 多入口接入：standalone `_SharedWorkCoordinator`改为批量续租并输出batch/renewal/directory-sync/
  avoided/failure指标；hybrid MPI/AFL active work lease合并调用并保留scalar兼容；target-group续租在
  全组锁内按shard合并屏障。相关审查还修复target lease接受NaN/Infinity时间以及覆盖损坏record的
  fail-open路径；
- 自动化与系统证据：Ruff、`py_compile`、`git diff --check`通过；F324定向9/9（0.67秒）；
  distributed-state 102/102、AFL profile 55/55、MPI lifecycle 22/22；完整`pytest -q test`为
  636 passed加38 subtests（91.15秒）。真实Open MPI 4.1.6双master强制TTL=1秒产生17批、67次
  renewal、41次目录sync，避免26次且failure=0；13 observations、7/6分布、6/6 ACK、epoch状态清零；
- 微基准与边界：64 records集中1个shard时目录sync 64→1（减少98.438%），中位339.717→
  174.842 ms（1.943x）；分布8 shards时64→8（减少87.5%），中位338.188→173.314 ms（1.951x）。
  这是本机overlayfs租约持久化热路径，不是DSE/solver/coverage benchmark；未做物理断电、NFS/Lustre
  或真实应用等CPU campaign。当前等级I/T/E-mechanism。完整说明、示意图和SHA-256证据见
  [`F324研究报告`](research-progress/Shard_Level_Heartbeat_Group_Commit_F324_2026-08-07.md)。

## 320. F325：Prevalidated Target-Group Mutation Batching（2026-08-07）

- 审查问题：旧`release_group()`逐成员交错执行token检查和删除；若digest顺序后部成员已被新token
  接管，会返回`False`但此前匹配成员已经删除，破坏group-level rejection。`claim_group()`又在
  首次publication后才可能发现后续payload序列化错误。claim/release成功热路径仍逐record执行
  parent-directory `fsync`，没有复用F324的分片屏障；
- 全组预处理与预验证：claim在获取锁和首次写入前固定owner/worker/payload快照并完成JSON预检，
  按target digest全序获取全部锁，读取、校验全组并一次性构造新record。release在首个unlink前
  要求每个成员的schema、authoritative ID、`leased`状态和exact token全部匹配；任一冲突以零record
  mutation返回；
- 分片mutation batch：`_DirectorySyncBatch`新增`unlink()`。claim每条仍执行temp JSON write、
  file fsync和same-directory atomic rename，release逐条unlink；两者只登记唯一parent shard，并在
  全组锁内每个命中shard执行一次directory fsync。全部屏障成功后才返回token/`True`；
- 检测故障补偿：保存exact previous snapshots和已修改索引。write/rename/unlink或directory barrier
  抛错时，在仍持有全组锁的条件下恢复已修改成员，并把rollback产生的replace/unlink再次按shard
  flush；错误不被误报为成功。claim-side crash residue是TTL有界的保守lease；release在持续存储故障
  下仍可能部分缺失，因此明确不宣称跨文件crash atomicity或数据库事务；
- 自动化与并发证据：新增7项测试，target-table定向13/13（0.44秒），distributed-state为109 passed
  加4 subtests（14.02秒），MPI lifecycle+AFL profile为77 passed加14 subtests（0.78秒），完整
  `pytest -q test`为643 passed加38 subtests（92.26秒），Ruff、`py_compile`和diff check通过。
  两个独立进程以32-target group和16-target overlap竞争50轮，50/50严格单赢家、0部分发布、0残留、
  0 hang；
- 微基准与边界：64-target claim+release共128次mutation，1-shard目录sync由128降至2，
  中位509.349→180.959 ms（2.815x）；8-shard由128降至16，中位508.899→180.124 ms
  （2.825x）。本机7轮overlayfs数据只证明target lease持久化路径，未执行真实S2F target campaign、
  coverage/solver/LAVA-M对比、物理掉电或分布式文件系统实验。当前等级I/T/E-mechanism。完整协议、
  机制图、可执行脚本、原始样本和SHA-256 evidence见
  [`F325研究报告`](research-progress/Prevalidated_Target_Group_Mutation_Batching_F325_2026-08-07.md)。

## 321. F326：Crash-Released Kernel Locks and Coverage-State Integrity（2026-08-07）

- 审查问题：work/target短锁使用`mkdir/rmdir`，持锁进程SIGKILL后目录永久残留；coverage-owner
  又依据wall-clock mtime删除“过期”锁，暂停但仍存活的holder恢复后会与新writer并发。coverage
  shard读取还把不存在、损坏JSON和一般I/O错误统一当作空状态，可能覆盖已经提交的权威coverage；
- crash-released短锁：新增共享`_bounded_advisory_lock()`。以稳定普通文件和
  `O_RDWR|O_CREAT|O_CLOEXEC|O_NOFOLLOW`取得descriptor，`fstat`拒绝非普通文件；循环
  `flock(LOCK_EX|LOCK_NB)`只对竞争errno重试，以monotonic deadline有界失败。锁由close、异常或
  进程退出释放，稳定pathname永不unlink，避免活holder与新opener锁住不同inode；
- 语义分层：kernel lock只保护一次read/validate/write临界区，长期work/target ownership仍由
  TTL和fencing token表达；`lock_ttl`仅作诊断age hint，不再授权时间抢锁。不支持flock的文件系统
  直接失败关闭；mixed-version旧lock directory不自动删除，迁移要求停止全部旧writer；
- coverage完整性：只有`FileNotFoundError`构造空shard；损坏JSON、schema、shard ID、epoch、
  非递增/跨shard index、非法bucket bits、contributors或非有限时间均拒绝且不覆盖原字节，其他
  `OSError`传播。heartbeat/owner/claim时间统一为有限非负值，`Infinity` peer不能成为永生owner；
- 自动化与系统证据：定向12 passed加3 subtests，相关共享状态/MPI/AFL回归197 passed加21
  subtests，完整warnings-as-errors Python为654 passed加41 subtests；Ruff、`py_compile`和diff
  check通过。20轮共40个已确认持锁进程SIGKILL后，work/coverage各20/20重新取得，最大6.220/
  10.678 ms且0 timeout；10轮8进程向256个相同index提交不同bits，均精确2048/2048 novel bits、
  最终`0xff`、0 lost update/hang；
- syscall、成本与边界：strace观察两次`openat`、exclusive nonblocking flock与unlock，warm路径
  0 mkdir/rmdir。10000 cycles×9轮无竞争微基准中旧目录锁中位9.932 us，新kernel lock为5.349 us，
  1.857x且namespace mutation 2→0。实验仅覆盖本机Linux overlayfs和进程SIGKILL，不证明NFS/
  CIFS/Lustre、网络分区、节点掉电、锁公平性或DSE/solver/coverage吞吐。完整说明、示意图、脚本和
  SHA-256 evidence见
  [`F326研究报告`](research-progress/Crash_Released_Kernel_Locks_and_Coverage_Integrity_F326_2026-08-07.md)。

## 322. F327：Shared-Filesystem Capability Contract（2026-08-07）

- 审查问题：F323-F326的目录屏障、跨目录publication与kernel lock正确性依赖部署文件系统的
  `fsync/replace/link/flock`语义，但旧流程直到第一次真实lease/corpus更新才发现不兼容。hybrid
  frontend还可能先启动async query service、object store和本地状态，再探测共享work/target/
  coverage roots；探针异常会穿出master，形成晚失败和半初始化风险；
- executable contract：新增`probe_shared_state_filesystem()`和不可变v1 capability snapshot。
  在真实root/private random tree上依次执行file/dir fsync、same/cross-directory durable replace、
  配置的state→publication边界replace、hard-link inode回验、durable unlink，再用两个全新
  `python -I` child分别证明parent持锁时必须contention和descriptor close后必须取得。直接library
  timeout也限制在0.001--60秒，任何unsupported/timeout/content mismatch/cleanup错误均失败关闭；
- 诊断与证明边界：解析`/proc/self/mountinfo`转义并选择deepest mount，记录root/publication
  device、fsid、filesystem type/source和distributed classification；这些字段不参与提升证明等级。
  snapshot固定`probe_scope=same-host-subprocess-v1`与`cluster_lock_verified=false`，不把本机child
  外推为NFS/CIFS/Lustre远端锁域、server failover或power-cut证明；
- 启动集成：standalone在immutable epoch metadata前探测hidden state root与actual corpus root；
  hybrid新增共享根枚举，按realpath去重并前移到async service/object store之前，成功snapshot在后续
  constructor调用复用。失败时先执行bounded STOP/exact ACK，再由main走非零Abort(70)。work/
  target lease和coverage gossip另保留`verify_filesystem=True` library gate；
- 自动化与实验：新增14项测试；定向13/13，相关distributed-state/MPI/AFL回归211 passed加21
  subtests，完整warnings-as-errors Python为668 passed加41 subtests（93.21秒），Ruff、
  `py_compile`、diff check通过。20次串行加5轮×8进程共60/60探针成功，0 child failure、invalid
  snapshot或残留；overlayfs串行中位/P95为54.315/57.678 ms，并发单进程97.736/112.716 ms。
  Open MPI单master 3 ranks和双master 8 ranks均exit 0、完整ACK、无epoch/probe残留；
- 科研边界：探针延迟是一次性startup cost，不是DSE加速；MPI目标为`/bin/true`，没有测solver、
  coverage或LAVA-M。当前等级I/T/E-mechanism；尚缺coordinated cross-host lock probe、mount-option
  matrix、server failover/network partition和CrashMonkey式断电恢复。完整协议、机制图、脚本、
  原始日志与SHA-256 evidence见
  [`F327研究报告`](research-progress/Shared_Filesystem_Capability_Contract_F327_2026-08-07.md)。

## 323. F328：Path-Specific Filesystem Requirement Profiles（2026-08-07）

- 审查问题：F327对standalone、lease和coverage根统一要求九项能力，导致不使用hard link、跨目录
  publication甚至durable unlink的协议仍可能被无关能力拒绝；若仅按路径缓存第一个较弱快照，
  多协议共用路径时又可能漏检更强要求；
- 精确profile：新增不可变`SharedFilesystemRequirementProfile`和full/lease/coverage三种具名
  契约，分别要求9/6/5项操作。profile严格验证稳定名称、精确bool、file/dir fsync强制存在以及
  lock exclusion/release成对；通用merge helper从唯一operation表计算并集，防止字段演进漂移；
- 三态探针：`probe_shared_state_filesystem()`只执行required阶段。完整九项保持F327 v1快照；部分
  profile输出v2，required为`true`、未执行项为JSON `null`并列入`unverified_operations`。required
  失败仍抛出能力错误，不产生带false的成功快照，也不降级一致性协议；
- 路径组合与接入：hybrid在service前枚举真实路径并按realpath合并同路径profile，cache只在
  cached verified set覆盖新required set时复用。work/target lease默认lease profile，coverage
  gossip默认更小的coverage profile；standalone继续在epoch前执行包含actual corpus boundary的
  完整九项；
- 自动化与实验：新增5项测试；F328定向17/17，相关distributed-state/MPI/AFL回归216 passed加
  21 subtests，完整warnings-as-errors Python为673 passed加41 subtests（94.04秒），Ruff、
  `py_compile`和diff check通过。full/lease/coverage各40次平衡交错真实探针中中位为51.763/
  42.038/36.918 ms，replace调用3/1/1、hard link 1/0/0、durable unlink 1/1/0，全部snapshot和
  residue验证通过；
- 科研边界：18.79%/28.68%是单机overlayfs一次性startup-cost observation，不是DSE/solver/
  coverage提升；未验证跨host锁域、server failover、网络分区或power-cut恢复。当前等级
  I/T/E-mechanism。完整设计、矩阵图、逐样本JSON和SHA-256 evidence见
  [`F328研究报告`](research-progress/Path_Specific_Filesystem_Requirement_Profiles_F328_2026-08-07.md)。

## 324. F329：MPI Cross-Host Shared-Lock Qualification（2026-08-07）

- 审查问题：F327/F328的lock exclusion/release由本机隔离child证明；NFS `local_lock=flock`、
  SMB protocol/mount/server差异或client配置漂移仍可能让不同节点各自取得同一path的“本地锁”。
  standalone多master若在该前提下使用共享lease record，单赢家和fencing token更新都会失去基础；
- 独立控制面：新增master-only communicator，生产路径只接受`MPI.Get_processor_name()`返回的实际
  processor identity。严格schema/token/phase/source envelope通过rank 0执行有界对象all-gather，
  不用待验证文件系统marker协调。全协议共享单一monotonic deadline；正常轮询从0.5 ms起步并在
  无进展时指数退避到10 ms；缺失、malformed、stale、冲突或send未完成均失败关闭；
- 跨client双向litmus：rank 0准备`O_RDWR|O_NOFOLLOW`稳定regular lock record，内容绑定epoch派生
  token且运行期不unlink。每个processor代表依次持锁，全部其他master必须观察`EACCES/EAGAIN`；
  descriptor关闭和MPI release barrier之后，下一个不同processor代表必须成功接管。H个processor、
  M个master的完整证明精确要求H轮、H*(M-1)次contention和H次remote release；
- 证明升级与接入：capability snapshot新增member/representative/round/contention/release字段；只有
  至少两个实际processor且完整基数成立时才置`cross-host-mpi-lock-v1`和true。同机多master记录
  membership但保持false和零轮次。资格发生在`state.json`前，通过后同一个capability注入
  `_SharedWorkCoordinator`，避免重复九项探测；失败`Abort(65)`。hybrid跨作业仍不冒充已验证；
- 自动化与机制实验：F329定向/故障8 passed、160 deselected；相关168 passed加13 subtests；完整
  warnings-as-errors Python为681 passed加41 subtests（93.83秒），Ruff、`py_compile`和diff check
  通过。真实Open MPI 7-rank形成2 master/5 worker，5/5 ACK、exit 0；两个master同为`cuda-ke`，
  因此正确输出false、零轮次并清理epoch状态。合成2/3/4 processor状态机各20次零失败，检查基数
  为2/6/12 contention及2/3/4 release；自适应轮询中位13.205/18.661/24.267 ms，较固定10 ms基线
  下降89.29%/89.33%/89.26%；
- 科研边界：当前等级I/T/E-mechanism。合成processor名称只证明状态机和before/after开销，JSON明确
  `deployment_evidence=false`；当前环境没有第二台真实client，因此尚无true deployment snapshot。
  协议不证明cache coherence、rename visibility、server/lock-manager failover、network partition、
  kernel panic、掉电、同epoch并发membership或DSE/coverage提升。完整协议、机制图、原始日志和
  SHA-256 evidence见
  [`F329研究报告`](research-progress/MPI_Cross_Host_Shared_Lock_Qualification_F329_2026-08-07.md)。

## 325. F330：Runtime Cross-Host Lock Qualification Renewal（2026-08-07）

- 审查问题：F329只在startup验证当前master client的`flock`排他与close-release语义；长时作业若发生
  remount、server/lock-manager restart或client语义漂移，内存中的
  `cluster_lock_verified=true`会成为陈旧资格，fenced work lease仍可能依赖已经失效的单赢家前提；
- 代次化可续期资格：新增`ClusterLockRenewalController`。rank 0是唯一发起者，请求精确包含schema、
  epoch、下一generation和domain-separated SHA-256 token；peer只接受本地`generation+1`。稳定lock
  record只绑定epoch，而每次MPI phase exchange token同时绑定generation，拒绝迟到、重放、跳代和
  token冲突；
- 主循环组合安全：只有startup真实跨processor证明为true且interval大于0才保留master-only
  communicator。root在quiescence未prepare且wall余量充分时发起；所有master先批量heartbeat当前
  work lease，再完整执行H/H*(M-1)/H lock litmus。运行期timeout取cluster probe timeout与work lease
  TTL/3的较小值，避免health probe耗尽租约窗口；
- 失败关闭与可观测性：续期成功后推进generation并替换内存能力；unverified、semantic drift、
  malformed、缺失master或transport错误均不回退上一代快照，而进入global control error、worker
  STOP/exact-ACK和`MPI_Abort(70)`。真实MPI故障实验还发现并修复“root abort早于peer尾部指标输出”
  的可观测性缺陷，失败判定完成后每rank立即且只输出一次metrics；
- 自动化与系统证据：F330定向13/13；相关MPI/distributed/hybrid回归292 passed加21 subtests；完整
  warnings-as-errors Python为688 passed加41 subtests（95.24秒），Ruff、AST解析和diff check通过。
  真实Open MPI 7-rank合成processor拓扑中，2 master/5 worker正常作业双方各续期3代并exit 0；第2代
  注入排他语义漂移后，双方均记录1 success/1 failure，作业exit 70；
- 成本与边界：2/3/4-master、20次交错同机机制基准中，续期中位13.218/18.811/24.317 ms，相对同一
  资格状态机直接调用增量为+0.062%/+0.254%/-0.008%（最后一项为噪声），检查基数保持2/2/2、
  3/6/3、4/12/4。实验明确`synthetic_topology=true`、`deployment_evidence=false`；未验证真实第二
  client、server failover、partition、rank repair、DSE/coverage或LAVA-M提升。当前等级
  I/T/E-mechanism。完整说明、图示和SHA-256证据见
  [`F330研究报告`](research-progress/Runtime_Cross_Host_Lock_Qualification_Renewal_F330_2026-08-07.md)。

## 326. F331：Deterministic Bounded Renewal Jitter（2026-08-07）

- 审查问题：F330在单一MPI job内只有rank 0按固定interval发起，不会产生communicator内timer
  分裂；但同时启动的独立jobs会从相近completion time重复进入同一固定周期，在共享存储、锁管理器和
  MPI控制面形成thundering herd及Moiré负载峰值；
- 确定性有界调度：新增domain-separated SHA-256调度函数，以work epoch和严格下一generation派生
  `[0,1)`分数，计算`D_g=I*(1+j*u_g)`。`j`严格限制在0..0.5，因此基础interval仍是最小间隔且
  `D_g<I*(1+j)`；高53位映射避免分数舍入为1，最终乘加若命中上端则用`nextafter`回退一个ULP；
  默认`j=0.1`，`0`精确恢复F330固定节拍。peer不读取本地timer，只跟随F330认证
  request，代次、防重放、TTL/quiescence/wall gate和失败关闭语义不变；
- 热路径与可观测性：controller在初始化和每代complete后各计算一次下一计划并缓存；complete把旧
  `next`直接提升为实际`last`，循环`due()`和`snapshot()`不重复hash。新增
  `SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_JITTER`，metrics升级为v2并
  记录jitter fraction、maximum interval、last/next scheduled interval；保守陈旧窗口显式变为
  `I*(1+j)+T_eff+scheduling delay`；
- 自动化与系统证据：新增4项测试加5个非法值subtests；定向17 passed加5 subtests，相关MPI/
  distributed/hybrid回归296 passed加26 subtests，完整warnings-as-errors Python为692 passed加46
  subtests（96.73秒），Ruff、AST和diff check通过。真实Open MPI 7-rank合成processor拓扑中，双方
  完成5代且last/next计划值完全一致，5/5 worker ACK并exit 0；真实同机高频50% jitter作业仍为
  cluster false、0 rounds且完全不启用runtime renewal；
- 群体反事实与边界：1024个独立epoch、12代、100 ms桶中，fixed/jitter都严格保存12288个事件；
  固定峰值1024、非空桶12，10% jitter峰值26、非空桶2232，计划峰值下降97.4609375%。调度函数20次
  交错中位为1.102851/1.766609 us per generation decision，生产每代只计算一次。该结果只证明合成
  计划到达分散，不是storage IOPS、DSE、solver、coverage或LAVA-M提升；当前无真实第二client、
  server failover、partition或rank repair证据，等级I/T/E-mechanism。完整说明、图示和SHA-256证据见
  [`F331研究报告`](research-progress/Deterministic_Bounded_Renewal_Jitter_F331_2026-08-07.md)。

## 327. F332：Epoch-Bound Renewal Configuration Consensus（2026-08-07）

- 审查问题：F330/F331的代次协议默认各master读取相同的interval、有效probe timeout与jitter；但
  启动脚本、容器环境或逐rank wrapper可能造成配置漂移。旧请求只绑定epoch/generation，配置不同的
  peer仍可能接受请求，使双方对续期窗口和陈旧资格上界产生静默分歧；
- 规范配置身份：controller将有效interval、timeout和规范化jitter以大端IEEE-754 binary64编码，
  把epoch与三项数值输入域分离SHA-256。`-0.0`规范为`+0.0`；NaN、Inf、非正timeout、负interval与
  越界jitter在生成指纹前拒绝。完整记录还携带派生maximum interval，接收端重算字段与摘要；
- 有界generation-0共识：所有qualified master在任何续期尝试前复用有界root-gather/
  result-broadcast point-to-point exchange比较完整记录。缺rank、畸形记录或任一字段不一致均失败
  关闭；没有使用可能无限等待全部成员的裸collective。请求升级为v2并在每代token中再次绑定配置
  指纹；`begin_request()`重算本地配置，`complete()`在修改代次、计数和下一计划缓存前再次复核，
  因此发送前或in-flight结果提交前的字段突变都原子拒绝。controller还新增默认启用的共识typestate：
  未由有界exchange封印精确本地指纹时，`due/begin/accept/complete`均不得推进；兼容开关为keyword-only，
  只在隔离pre-F332测试与机制基准中显式关闭，生产入口保持强制；
- 关闭次序修复：真实漂移故障注入发现root可能在peer完成本地worker ACK前调用`MPI_Abort`。当前每个
  master先完成自己的worker shutdown，再进入有界master-only rendezvous，之后才统一Abort(70)，
  从而保留两侧关闭证据；这仍不是失效rank后的communicator repair；
- 自动化与系统证据：定向24 passed加8 subtests，相关MPI/distributed/hybrid回归303 passed加29
  subtests，完整warnings-as-errors Python为699 passed加49 subtests（101.91秒）。真实Open MPI
  7-rank同机合成拓扑以固定epoch独立复跑两次，双方每次形成同一64字符（256位）指纹并各完成3代、
  3/3与2/2 worker ACK、exit 0，跨复跑指纹2/2一致；只把peer jitter从0.1改为0.25后，两次均保持
  generation/attempts为0并完成两组本地ACK，在0.912403/0.875331秒内exit 70；
- 成本与边界：2/4/8-master同机线程合成控制面各20次交错样本中，完整记录验证相对裸有界exchange的
  中位增量为0.050506/0.104471/0.339265 ms，比率1.041691/1.077403/1.220914x。该结果包含Python
  线程调度，只是机制成本；processor identity和漂移由测试wrapper注入，不证明真实多主机共享存储、
  partition、rank repair、DSE吞吐、coverage或LAVA-M提升。当前等级I/T/E-mechanism。完整说明、图示
  和SHA-256证据见
  [`F332研究报告`](research-progress/Epoch_Bound_Renewal_Configuration_Consensus_F332_2026-08-07.md)。

## 328. F333：Durable Renewal Configuration Manifest（2026-08-07）

- 审查问题：F332只证明同一次MPI启动内的live masters对interval、有效timeout和jitter一致；显式
  work epoch恢复时，另一组masters仍可对一份不同配置达成新的live共识，使同一WAL-like状态跨重启
  对应多个续期策略，破坏恢复语义稳定性与epoch级实验归因；
- 两级承诺：F333只接受已经由F332有界exchange封印且本地字段仍未漂移的controller，把
  `config-v1`完整记录包装成canonical ASCII JSON。固定文件
  `.standalone-work-<epoch>/renewal-configuration.json`重复携带epoch、配置指纹和完整配置；live共识
  约束当前communicator，durable manifest约束同一epoch的历史，任一层都不能替代另一层；
- 失败原子create-once：缺文件时先以随机`xb`临时名写入并file-fsync，再用hard link建立唯一目标名并
  directory-fsync；并发相同配置loser读取winner后幂等成功，不同配置loser只能exact-compare失败，
  不允许replace或last-writer-wins。final component用`O_NOFOLLOW`打开，只接受regular file和4096字节
  上界；截断、追加、非普通文件、symlink和超限均失败关闭；
- 恢复快路径与诊断修复：已有manifest先走bounded exact-read，不再产生临时文件或fsync；只有ENOENT
  才进入发布路径。20轮不同配置并发竞争均恰好一个publisher/一个rejection。重复试验还发现按本地
  期望长度读文件会把部分合法长度漂移误报为unreadable，最终改为固定上界读取后exact compare，使
  drift诊断不依赖JSON数值文本长度；
- 生产次序：F327/F329 storage资格→F332 generation-0 live共识→F333 durable compare/publish→F330
  generation 1。manifest I/O或比较失败设置global control error，双方先完成3/3和2/2 worker ACK，再经
  master-only有界rendezvous以70退出；失败恢复只读拒绝且不覆盖旧承诺，正确配置仍可重开状态；
- 自动化与真实MPI证据：lifecycle/filesystem定向53 passed加14 subtests，六模块关联回归306 passed加
  29 subtests，完整warnings-as-errors Python为702 passed加49 subtests（99.71秒）。7-rank/2-master
  真实Open MPI同机实验在manifest输出后SIGKILL（-9）；所有rank统一将jitter 0.1改为0.25后，live共识
  可成立但durable compare使双方generation/attempts保持0/0、原文件SHA-256不变并exit 70；恢复0.1后
  双方各3 attempts/3 successes/0 failures、exit 0并清理epoch状态；
- 成本与边界：本机overlayfs上10次warm-up、100次交错复用样本中，exact-read快路径中位10.481 us，
  删除前temp-file fsync/link/unlink反事实2698.4085 us，中位节省2687.9275 us、比率257.457x；30次首次
  durable publication中位7372.645 us。该数据只量化每master每次恢复的一次性本机控制I/O，不是DSE、
  solver、coverage或LAVA-M提升。processor identity与进程组崩溃由测试驱动注入，未证明真实多主机
  storage、partition、rank repair或ULFM，等级I/T/E-mechanism。完整说明、图示和SHA-256证据见
  [`F333研究报告`](research-progress/Durable_Renewal_Configuration_Manifest_F333_2026-08-07.md)。

## 329. F334：Durable Completed-Epoch Cleanup and Finalize Gate（2026-08-07）

- 审查问题：clean lifecycle末尾使用`shutil.rmtree(..., ignore_errors=True)`后直接`MPI_Finalize`；删除
  失败会被静默吞掉，成功删除也没有shared-root目录fsync。固定work epoch的残留状态可能在下一次
  启动时被误当作中断WAL恢复，F333 manifest也会继续约束一项实际上已经完成的任务；
- 持久清理原语：`durable_rmtree`拒绝filesystem root，递归删除active work-state后对其父目录
  fsync，删除与目录屏障异常均向上传播。`_cleanup_completed_work_state`要求epoch是shared root的直接
  子目录且名称精确匹配`.standalone-work-<64位小写十六进制>`；普通目录、短epoch和大小写异常在
  删除前失败关闭。显式output只删除epoch，程序拥有临时root时按epoch→root顺序删除；
- 两阶段退出门控：worker exact ACK、master stats和cleanup rendezvous完成后，所有rank先通过bounded
  pre-cleanup Ibarrier；rank 0执行持久清理，其他rank等待post-cleanup Ibarrier。只有root清理成功并
  加入第二屏障后才能共同Finalize。控制不完整、pre-barrier、持久删除、post-barrier分别以
  Abort(70/71/72/73)失败关闭；删除后fsync失败不伪装回滚或成功；
- 自动化与真实MPI证据：distributed/lifecycle/filesystem定向191 passed加24 subtests，六模块关联
  308 passed加32 subtests，完整warnings-as-errors Python为704 passed加52 subtests（94.73秒）。真实
  Open MPI 7-rank/2-master同机且无合成processor拓扑；rank 0删除前I/O故障发生前两组worker已完成
  3/3与2/2 ACK，作业exit 72并保留`state.json`与fenced record；相同epoch取消注入后exit 0并删除状态；
- 成本与边界：本机overlayfs两文件合成epoch各100次交错原始样本中，旧式未确认rmtree与durable
  rmtree中位为70.0455/2756.892 us，目录屏障中位增加2686.8465 us、比率39.3586x。它只发生在作业
  完成边界，不是DSE热路径或性能提升；未测真实多机断电、server failover、ULFM、MPI barrier规模、
  DSE吞吐、solver、coverage或LAVA-M。等级I/T/E-mechanism。完整说明、机制图和SHA-256证据见
  [`F334研究报告`](research-progress/Durable_Completed_Epoch_Cleanup_F334_2026-08-07.md)。

## 330. F335：Atomic Completed-Epoch Retirement and Deferred Reclamation（2026-08-07）

- 优化问题：F334的durable rmtree保证完成语义，但仍在所有rank进入Finalize前遍历并删除整个状态树，
  manifest、shard record、lock和staging数量会线性放大collective critical path。F335将“恢复器不再看见
  active epoch”的逻辑提交与inode/block物理回收分离；
- 原子退役：persistent output只接受shared root直属的真实目录和精确64位小写hex active名称，生成
  128-bit retirement id后同目录rename为`.retired-standalone-work-<epoch>-<id>`并fsync shared root。
  rename/fsync任何异常均Abort(72)，包括rename已可见但持久性未知；owned temporary root仍完整删除；
- 延迟GC：rank 0在后续启动、service开始前读取严格的`SYMCC_RETIRED_WORK_STATE_GC_LIMIT`（默认1），
  使用stable regular-file、`O_NOFOLLOW`、bounded flock串行化跨进程回收；先预验证完整reserved namespace，
  再按名称排序durable rmtree至多limit个根。畸形名称、symlink、非目录、锁超时或删除/fsync异常均
  Abort(66)，`limit=0`显式禁用；
- 自动化与真实MPI证据：定向195 passed加30 subtests，六模块关联312 passed加38 subtests，完整
  warnings-as-errors Python为708 passed加58 subtests（96.23秒）。真实Open MPI 7-rank/2-master同机在
  3/3+2/2 ACK后注入post-rename/pre-fsync故障，active已消失且一个retired根保留，exit 72；相同epoch
  重启先回收1个旧根，再exit 0并产生不同id的新retired根，corpus摘要和bytes保持完整；
- 分层成本与边界：本机overlayfs每层5次warm-up、30+30交错样本。3文件树退役/递归删除比率0.990x，
  没有收益；129文件为1.243x、节省641.739 us；1025文件为3.133x、节省6132.870 us。树构造和延迟GC
  不在计时区内，因而只证明当前作业完成边界随树规模改善，不表示总I/O消失、end-to-end DSE、solver、
  coverage或LAVA-M提升；未测真实多机、跨作业锁、断电和NFS server failover，等级I/T/E-mechanism。
  完整说明、机制图、原始样本和SHA-256证据见
  [`F335研究报告`](research-progress/Atomic_Completed_Epoch_Retirement_F335_2026-08-07.md)。

## 331. F336：Kernel-Enforced No-Clobber Epoch Retirement（2026-08-07）

- 审查问题：F335的128-bit retirement id降低偶然冲突概率，但`lexists + os.replace`仍是两个操作；
  检查后出现同名目标时普通rename会覆盖兼容对象，故“目标不存在”尚不是原子不变量；
- 内核no-clobber：`distributed_state.durable_rename_noreplace()`通过ctypes调用Linux
  `renameat2(AT_FDCWD, source, AT_FDCWD, destination, RENAME_NOREPLACE)`，复制errno并在成功后fsync
  destination parent。libc无符号或文件系统不支持时失败关闭，绝不回退check-then-rename；EEXIST保留
  active与retired双方。其他需要replace语义的manifest/heartbeat/publication路径保持原helper；
- 真实挂载点资格：persistent output在service前由rank 0创建process-owned随机probe root，要求第一次
  no-replace rename成功、重建source后的第二次同目标rename严格EEXIST，再耐久删除整个probe。缺ABI、
  flag错误、静默clobber、create/fsync/cleanup不确定均bcast并Abort(67)；temporary output不需要该原语；
- 自动化与系统证据：定向199 passed加30 subtests，六模块关联316 passed加38 subtests，完整
  warnings-as-errors Python为712 passed加58 subtests（98.48秒）。双进程竞争严格1 publisher/1 rejection；
  strace观察`RENAME_NOREPLACE=0`、parent fsync和第二次`EEXIST`。真实Open MPI 7-rank/2-master同机在
  3/3+2/2 ACK后遇到已fsync的固定retired目标，exit72且active/retired marker均保留；相同epoch重启
  GC1后正常退役并exit0，corpus摘要与bytes保持；
- 成本与边界：overlayfs 5 warm-up、60+60交错样本中durable replace/no-replace中位
  2598.252/2603.320 us，增量5.068 us、比率1.002x；60次完整启动probe中位11.944 ms、P95 13.024 ms。
  仅证明本机Linux/glibc/overlayfs机制成本和同机MPI恢复，不证明NFS/Lustre/GPFS跨client、断电、rank
  repair、DSE吞吐、solver、coverage或LAVA-M提升，等级I/T/E-mechanism。完整说明、机制图、raw样本、
  syscall和SHA-256证据见
  [`F336研究报告`](research-progress/Kernel_Enforced_No_Clobber_Epoch_Retirement_F336_2026-08-07.md)。

## 332. F337：Resumable Budgeted Retired-Tree GC（2026-08-07）

- 审查问题：F335/F336把完成边界从递归删除缩短为原子名称退役，但启动GC的limit只约束选中根数；
  对一棵大树仍调用完整`shutil.rmtree`，启动延迟和stable GC lock持有时间随树内条目数无界增长；
- 三预算增量回收：保留`SYMCC_RETIRED_WORK_STATE_GC_LIMIT`根预算，新增严格
  `SYMCC_RETIRED_WORK_STATE_GC_ENTRY_BUDGET`（默认4096）和
  `SYMCC_RETIRED_WORK_STATE_GC_TIME_BUDGET_SECONDS`（默认0.05秒）。entry budget严格统计成功
  `unlink/rmdir`，root本身也计1；time budget在syscall之间协作检查且至少允许一次变更，明确不承诺抢占
  一个阻塞storage syscall。锁等待仍由独立bounded timeout约束；
- 可恢复descriptor-relative DFS：`durable_rmtree_step`用显式frame stack保存directory fd、
  `scandir(fd)` iterator、basename和dirty位；所有目录以`O_DIRECTORY|O_NOFOLLOW`相对父fd打开，symlink
  只unlink不跟随。预算停止时从最深层同步仍存活的dirty目录；子目录/root被删除时分别同步其父目录；
  同步/关闭异常仍尽力处理全部descriptor后抛出。部分树继续留在retired namespace，下一启动重新扫描
  即可续删，不需要另一份可能漂移的GC journal；
- 枚举语义修复：Python明确目录变更期间`scandir`的未返回条目行为未指定。EOF后的`rmdir`若得到
  `ENOTEMPTY`，实现关闭并重开同一directory fd上的iterator继续，不把枚举短读误报为存储损坏；其他
  扫描、类型、删除、fsync或lock异常仍在service前bcast并Abort(66)。启动日志新增有效三预算、partial
  root、removed entries和stop reason；
- 自动化与系统证据：定向204 passed加43 subtests；六模块相关321 passed加51 subtests；完整
  warnings-as-errors Python 717 passed加71 subtests（99.48秒），Ruff、compile和diff check通过。
  真实Open MPI 7-rank/2-master同机连续三次启动以2-entry预算把旧根从5→3→1 files→absent，每轮均
  3/3+2/2 ACK并exit0；syscall显示`O_NOFOLLOW` descriptor open、恰好2次`unlinkat`和目录fsync；
- 机制成本与边界：local overlayfs上129/1025/4097文件、每规模2 warm-up加20+20交错样本，完整删除
  中位3.372/7.365/28.521 ms，固定64-entry单步3.089/3.579/3.648 ms；完整路径随规模增长8.459x，
  固定单步增长1.181x。4097文件按生产默认预算两步总计30.032 ms，相对完整删除中位1.053x，说明收益
  是单次启动封顶和跨启动摊销，不是总I/O减少。未验证硬wall-clock、顶层namespace发现预算、真实多机
  存储、DSE/solver/coverage或LAVA-M提升，等级I/T/E-mechanism。完整说明、示意图、raw样本、真实MPI、
  syscall与SHA-256证据见
  [`F337研究报告`](research-progress/Resumable_Budgeted_Retired_Tree_GC_F337_2026-08-07.md)。

## 333. F338：Streaming Bounded-Memory Retired-Root Discovery（2026-08-07）

- 审查问题：F337虽以root/entry/time三预算约束物理删除，候选发现仍先
  `tuple(os.scandir(shared))`保留全部`DirEntry`，再保存全部retired名称并全排序。shared root含N个条目、
  R个候选时，Python保留状态为O(N+R)，选择为O(R log R)，删除预算无法约束发现阶段峰值；
- 完整预验证的流式Top-K：`_select_retired_work_states`使用`with os.scandir`单遍消费条目，仍对每个reserved
  对象执行精确名称解析与`is_dir(follow_symlinks=False)`；反序比较器使普通min-heap的堆顶成为当前最大
  名称，容量最多为root limit L。堆满后仅当新名称更小时`heapreplace`，最终排序至多L项，严格保持旧版
  词法最小L项语义且与scandir任意顺序无关；
- 正确性与复杂度：循环不变量为`H_k=smallest_L(P_k)`、`|H_k|=min(k,L)`；完整iterator结束后才调用F337
  删除，因此后置畸形reserved名、symlink、不可读类型仍在首次mutation前失败关闭。候选保留状态降为
  O(min(R,L))，选择工作为O(R log min(R,L))；完整getdents/type validation仍为O(N)，不宣称硬启动时限；
- 遥测与测试：GC结果新增`scanned_entries/candidate_roots`，启动日志分别报告scan/candidate/selected，避免
  把发现工作混入成功unlink/rmdir计数。33个反向候选、L=4测试证明精确Top-K与heap峰值4，并扩展原测试
  检查disabled/partial/remainder统计。定向205 passed加43 subtests，相关322 passed加51 subtests，完整
  warnings-as-errors Python 718 passed加71 subtests；
- 真实MPI证据：真实Open MPI 7-rank/2-master/5-worker同机运行扫描19个shared-root条目、识别17个retired
  候选并选择词法最小4个，4次rmdir后13个fixture根保留，本次epoch正常退役新增1根，最终严格
  `17-4+1=14`；worker ACK为3/3+2/2、exit0、3.709秒，corpus seed摘要与字节保持；
- 成本与边界：local overlayfs、limit8、128/1024/4096候选，每规模3 warm-up、30+30交错timing和10+10
  交错tracemalloc样本。旧/新traced peak中位为66130/3586、502762/3652、1995370/3652 B，降低
  18.44x/137.67x/546.38x；旧/新时间中位为299.475/318.804、2195.427/2284.486、
  9245.0845/9356.0305 us，新路径慢6.45%/4.06%/1.20%，不宣称speedup。tracemalloc不是RSS或内核/远端
  内存；未测真实多机、到达时间公平、DSE/solver/coverage/bug/LAVA-M收益，等级I/T/E-mechanism。
  完整说明、机制图、raw样本、真实MPI和SHA-256证据见
  [`F338研究报告`](research-progress/Streaming_Bounded_Memory_Retired_Discovery_F338_2026-08-07.md)。

## 334. F339：Streaming Expected-First No-Follow Staging Integrity（2026-08-07）

- 审查问题：worker-private stage与durable result manifest已保证父任务commit前child不可见，但live验证仍先
  `tuple(os.scandir(stage))`保留全部`DirEntry`，且未知但格式正确的64-hex对象会在最终set比较拒绝前被完整
  哈希；公共`_file_sha256`使用path-following `open()`，使指向正确内容的staged/public symlink可能冒充
  content-addressed regular object，并影响replay与promotion幂等判断；
- 流式expected-first准入：`_verified_staged_output_hashes`用context-managed `os.scandir`一次消费一个条目，
  名称规范化后必须先属于expected manifest，才执行no-follow类型检查和内容摘要；未知名称在metadata/hash
  I/O前失败，提前返回仍关闭iterator。live保持`staged==expected`，crash replay继续允许expected分布于
  verified staging与verified public corpus，缺失stage会把全部对象转到public回验；
- descriptor-bound摘要：`_file_sha256`以`O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`打开对象，
  `fstat(fd)`必须为`S_ISREG`，再从同一descriptor分块读取SHA-256；缺少`O_NOFOLLOW`、symlink、非普通
  对象或I/O不确定性统一失败关闭，异常路径在`finally`关闭fd。initial/shared/recovery/active/worker/replay/
  promotion所有内容地址回验共享该原语；
- 测试与真实MPI：新增regular/symlink/directory、缺flag、fd关闭、staged/public symlink、未知名0-hash及
  iterator首项拒绝/关闭测试；定向209 passed加43 subtests，六模块相关326 passed加51 subtests，完整
  warnings-as-errors Python 722 passed加71 subtests。真实Open MPI 4-rank/1-master/3-worker同机运行由确定性
  output-contract target产生1个child，最终2/2 public regular对象名称/内容摘要一致，无staged对象残留，
  3/3 ACK、exit0、1.773秒；
- 成本与边界：合法集合128/1024/4096对象、每规模3 warm-up、30+30交错timing和10+10交错tracemalloc，
  统一stub内容hash以隔离控制结构；旧/新peak为53216/22775、383328/141433、1532256/562297 B，下降
  2.34x/2.71x/2.72x，但新时间慢3.82%/3.26%/3.17%。16 MiB非manifest对象30次从30 hashes/480 MiB
  requested降至0/0；1406.04x热缓存比率不外推campaign。expected/observed仍为O(M)，无真实多机、掉电、
  Byzantine worker、DSE/solver/coverage/bug/LAVA-M收益，等级I/T/E-mechanism。完整说明、图、raw样本、
  真实MPI和SHA-256证据见
  [`F339研究报告`](research-progress/Streaming_Expected_First_NoFollow_Staging_F339_2026-08-07.md)。

## 335. F340：Bounded Streaming Result Admission（2026-08-10）

- 审查问题：F339已约束master的stage验证，但worker仍以`os.listdir`物化全部输出路径、逐文件无界
  `read()`整体载入内存并发送无上限hash vector；master没有每parent对象数、原始逻辑总字节或worker声明
  字节的独立准入，单一父任务可在commit fence前制造无界Python内存、MPI metadata和staging占用；
- 权威预算与双阶段worker门：rank 0严格解析并广播
  `SYMCC_STANDALONE_RESULT_MAX_OBJECTS`（默认4096）和
  `SYMCC_STANDALONE_RESULT_MAX_BYTES`（默认256 MiB），非法配置在共享状态前Abort(68)。单遍
  context-managed `scandir`在第limit+1项停止，只接收no-follow regular entry并累计metadata size；随后以
  `O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`打开same-fd regular inode，每次最多1 MiB同步SHA-256和partial-safe
  `os.write`，file fsync、原子content-address rename及digest/size回验，异常关闭fd并清除未提交stage；
- 重复计费与master独立验证：ordered `new_hashes`保留重复项，`staged_bytes`统计每个原始文件，即使物理stage
  只保留一个相同hash inode。master先限制vector/generated/declared bytes，再对每个unique inode做F339
  no-follow摘要/size，按ordered vector重新展开logical bytes；exact set、声明和双预算全部成立才允许
  `begin_commit`；
- 失败与恢复时间语义：显式`result-budget-exceeded(resource, observed, limit)`删除未提交stage，不截断、不
  发布、不把确定性超大parent转为无限worker重试；本地清理不确定时仍保留规范
  `staging_id`，使master能重试删除，然后保留pre-commit恢复边界并经bounded shutdown Abort(70)。
  当前预算不重放到历史durable v1 committing manifest；它过去已被准入，仍必须严格重验hash/regular inode并
  完成irreversible promotion redo；
- 自动化与真实MPI：生命周期定向45 passed加35 subtests，六模块关联334 passed加62 subtests，完整
  warnings-as-errors Python 727 passed加82 subtests（94.94秒），Ruff、compile、diff check通过。真实Open MPI
  三次2-rank同机运行中，success公开seed+child并exit0；3对象对2上限及17字节对16上限均只保留seed、无stage
  residue、active/retired epoch为1/0并exit70；
- 机制成本与边界：fresh-process sparse-file基准每尺度2 warm-up加10+10样本。8/32 MiB的旧/新Python traced
  peak为10487262/2099636 B（4.99x）与35653086/2099636 B（16.98x）；RSS中位为45980/37270 KiB（1.23x）
  与70600/37252 KiB（1.90x）。新路径时间慢27.99%/10.16%，体现固定峰值边界换取Python syscall循环成本。
  该数据不是campaign、DSE、solver、coverage、bug或LAVA-M收益；无真实多机、NFS/Lustre、storage failover或
  Byzantine worker证据，等级I/T/E-mechanism。完整说明、图、raw samples、真实MPI与SHA-256证据见
  [`F340研究报告`](research-progress/Bounded_Streaming_Result_Admission_F340_2026-08-10.md)。

## 336. F341：Stable Streaming Input Admission（2026-08-10）

- 审查问题：external seed导入仍以`f.read()`物化完整内容，`is_file(no-follow)`与后续follow-open
  分离；worker使用`copy2`后再读一遍SHA-256；import/shared `scandir`未显式关闭；最终
  `listdir/isfile`会物化名称、跟随链接，且只减去启动seed，将运行中晚到external input误报为
  symbolic-generated interesting case；
- 稳定snapshot与配置不变量：新增`SYMCC_STANDALONE_INPUT_MAX_BYTES`（默256 MiB），rank 0
  严格广播且要求`RESULT_MAX_BYTES <= INPUT_MAX_BYTES`，非法组合Abort(68)。`O_NOFOLLOW|O_NONBLOCK`
  打开regular inode，用固定1 MiB窗口读取，对比读前/读后fd及最终path的
  `(dev,ino,size,mtime_ns,ctime_ns)`，同时约束内容和路径身份；
- 两阶段seed发布：阶段A流式hash后按内容HRW选owner，`imported_files`由filename set升级为
  filename-to-identity map，同名原子替换可再准入；owner在阶段B按exact identity重开源，同一字节流
  完成copy/hash/partial-safe write/file fsync/content-addressed atomic publication和目标回验。变更不记忆名称并可
  下轮重试；确定性超限不发布/不派发并Abort(70)；
- worker与统计修正：worker以一次fixed-window copy同步验证expected SHA-256和稳定身份，替代
  `copy2 + second digest read`；公共扫描和关闭计数使用context-managed `scandir`，只统计64-hex
  no-follow regular对象；当时以shutdown external set修复late queue误报，阶段A已观察但阶段B未发布的
  phantom集合域问题由F342继续纠正；
- 验证：定向49 passed加40 subtests，六模块关联338 passed加67 subtests，完整warnings-as-errors
  Python 731 passed加87 subtests（96.11秒）。真实2-rank Open MPI中，同名seed replacement的两个内容各
  执行一次，external=2/new=0、exit0、active/retired=0/1；17 B对16 B上限在target前exit70、
  0 public/stage、1/1 ACK、active/retired=1/0；
- 机制成本：fresh-process 8/32 MiB各2 warm-up加10+10样本。tracemalloc peak从
  10487568/35653392 B降至2099834/2099834 B（4.99x/16.98x），RSS中位从42360/67538降至
  35586/35264 KiB（1.19x/1.92x），时间慢9.59%/11.04%。owner第二次稳定读取是identity closure
  的显式代价；仍保留O(N) identity/provenance metadata和M倍external hash read，无多机、DSE/solver/
  coverage/campaign/bug/LAVA-M收益结论，等级I/T/E-mechanism。详见
  [`F341研究报告`](research-progress/Stable_Streaming_Input_Admission_F341_2026-08-10.md)。

## 337. F342：Public Corpus Provenance Intersection（2026-08-10）

- 审查问题：F341在阶段A稳定观察 external content 时记录`external_hashes`，但只有HRW owner能在阶段B按
  exact identity发布。源在两阶段之间替换、删除或失稳时，旧摘要合法留在observed set却从未进入public
  corpus；旧式`max(0, public_count-len(external_hashes))`把不同集合域的基数直接相减，会由phantom
  external静默抵消真实generated object；
- 同域provenance投影：新增不可变`_CorpusProvenanceCounts(public, external)`；final root在一个
  context-managed `scandir`中只接受canonical 64-hex no-follow regular对象，同时计算public总数和
  `public ∩ observed-external`成员数，`generated=public-external`。external只在public分支内增加，结构上保持
  `0 <= external <= public`与`public=external+generated`，不再用`max(0)`掩盖域错配；启动日志改称
  `Observed`，准确区分阶段A观察与owner阶段B发布；
- 自动化与真实MPI：新增present external、generated、phantom external、metadata、目录和symlink同场反例；
  定向49 passed加40 subtests，六模块相关338 passed加67 subtests，完整warnings-as-errors Python
  731 passed加87 subtests。真实Open MPI 7-rank/2-master/5-worker同机运行用显式6秒rank-1 owner延迟，
  rank 0观察后删除external源；phantom未发布，唯一generated对象执行一次，最终external0/new1、
  3/3+2/2 ACK、active/retired=0/1、无stage residue、exit0，10/10结构检查通过；
- 机制成本与边界：4096 public对象包含2048 present external和2048 generated，observed set另含2048
  phantom；旧反事实错误报告generated0，F342精确报告2048。5 warm-up后各30个逐轮交错样本，旧count-only/
  intersection中位为10383.5935/10566.924 us，F342成本比1.01766（约1.77%）。空文件metadata-hot本机基准、
  显式rank delay和synthetic target只证明统计正确性与机制成本；无真实多机、冷缓存、DSE/solver/coverage/
  campaign/bug/LAVA-M收益，等级I/T/E-mechanism。完整说明、机制图、全部raw样本、真实MPI与SHA-256证据见
  [`F342研究报告`](research-progress/Public_Provenance_Intersection_F342_2026-08-10.md)。

## 338. F343：Bounded Streaming Simulation Mutations（2026-08-10）

- 审查问题：standalone `--simulate` 虽只用于普通编译目标的框架机制测试，却重新以`read()`加载完整
  worker input；每个输出再保留完整`bytearray(data)`，写前`bytes(mutated)`又产生一份全量副本，单worker
  Python payload峰值接近`3B`。默认5个输出的`5B`结果预算只在全部写完后的directory discovery检查，已知
  越界仍产生无效I/O；顺序直接写`sim_NNNN`还会在中途失败时留下可见前缀并覆盖同名文件；
- 语义保持的双重有界流：新增共享partial-write helper和32-output descriptor batch。先证明
  `N<=object_limit`与`N*B<=byte_limit`，再以`O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`打开regular source并记录
  dev/ino/size/mtime/ctime。每个输出仍按旧顺序抽取1至3个position/value pair，重复position保持后写覆盖；
  源按1 MiB chunk读取，无变异的输出直接写immutable chunk，有变异时一次只创建当前输出的一个
  `bytearray(chunk)`。默认`N=5<32`只读源一遍；一般峰值为`O(C+N)`、fd上界为33；
- 稳定快照与失败原子发布：全部`O_EXCL` 0600 temporary写完后复验同一fd和最终path identity；同目录
  `link(tmp,sim_NNNN,follow_symlinks=False)`提供原子no-clobber发布，再unlink临时名。预占目标和竞态目标
  均EEXIST且不被覆盖；任一write/source/link失败回滚本次已发布名称并清理全部temporary。worker-private
  文件只承诺调用边界原子可见性，后续由F340执行hash/fsync/stage，不把临时输出描述为掉电持久；
- 自动化与真实MPI：固定seed的7输出在31-byte chunk、2-output batch、强制最多7-byte partial write下与旧
  算法逐字节一致；另覆盖对象/字节预检、empty/symlink、预占与racer、写失败、源变化、第二次link失败及
  8 MiB固定heap。定向53 passed加43 subtests，六模块相关342 passed加70 subtests，完整
  warnings-as-errors Python为735 passed加90 subtests（95.02秒）。真实Open MPI 2-rank/1-worker生产
  `--simulate`运行公开1 external加5 generated one-byte对象，6/6各执行一次、1/1 ACK、stage residue0、
  active/retired=0/1、exit0；
- 机制结果与边界：overlayfs、8/32 MiB、每机制每尺度2 warm-up加10 retained fresh-process样本，全部ordered
  output digest一致。旧/新traced peak为25,175,994/3,152,950 B（7.985x）与
  100,673,466/3,152,990 B（31.930x）；RSS中位降低1.670x/3.358x；耗时中位22.789→10.617 ms与
  138.964→28.822 ms，本机机制加速2.146x/4.821x。输入4倍时新heap仅增长1.000013x，但tracemalloc不含
  native/kernel内存；synthetic observer不调用solver，无多机、coverage、campaign、bug或LAVA-M收益结论，
  等级I/T/E-mechanism。完整方案、图、40个raw样本、真实MPI与SHA-256证据见
  [`F343研究报告`](research-progress/Bounded_Streaming_Simulation_Mutations_F343_2026-08-10.md)。

## 339. F344：Bounded Hybrid Worker Result Admission（2026-08-10）

- 审查问题：standalone结果路径已由F340有界化，但hybrid `run_symcc_worker`仍先
  `list(scandir)`物化全部目录项，再对所有输出无上限`read()`并把全部唯一`bytes`同时保存在
  `uniq`；对象数、总逻辑字节、单对象和hint记录均无准入契约，异常parent可放大worker堆、
  mpi4py RESULT及showmap后处理；
- 完整预算预检：新增`SYMCC_HYBRID_RESULT_MAX_OBJECTS`（默认4096）、
  `SYMCC_HYBRID_RESULT_MAX_BYTES`（默认256 MiB）和
  `SYMCC_HYBRID_RESULT_MAX_HINTS`（默认65536）。单遍context-managed `scandir`在读取payload前
  对flat no-follow regular results/hints证明对象、聚合逻辑字节及单对象上限；单对象同时受
  `SYMCC_MAX_TRANSPORT_INPUT`约束，目录总entry另受`2*N+16`上限，防止点文件隐藏无界扫描；
- 稳定两遍收集：预检记录dev/ino/size/mtime/ctime；每次读取用
  `O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`打开regular fd，并闭合fd-before/fd-after/path-after identity。
  第一遍仅保留16-byte BLAKE2内容键用于跨item去重；第二遍重开稳定候选并只让coverage-interesting
  bytes进入`new_tests`。showmap前payload空间从全部结果总字节改为一个对象加有界metadata/hints；
- 失败与遥测：任一对象/字节/hint/entry预算超限整批拒绝，不按readdir顺序截断前缀；结构化
  `{resource,observed,limit,objects}`保留真实target return code、exec/post时间和killed状态，worker经普通
  RESULT通知master并继续campaign。symlink、FIFO、设备和预检后路径替换不能进入内容处理；第二遍
  showmap发生I/O失败时撤销第一遍暂存的`worker_seen`摘要，使相同内容在后续item仍可重试；
- 自动化：新增normal/hint兼容、对象/聚合字节/hint-file/hint-record预算、payload-before-read、symlink、
  path replacement与32x1 MiB固定heap测试。定向57 passed加47 subtests，六模块相关346 passed加74
  subtests，完整warnings-as-errors Python 739 passed加94 subtests（96.63秒）；真实本地child process经
  生产`SymCCEngine + run_symcc_worker`验证success及3类精确拒绝，6/6结构检查通过；
- 机制成本与边界：32/128个1 MiB unique sparse outputs，每尺度/机制2 warm-up加10 retained fresh
  processes，共40 raw样本且对象/字节/digest vector完全一致。旧/F344 traced peak为
  33,573,032/1,090,489 B（30.787x）和134,282,660/1,164,505 B（115.313x），RSS降低1.594x/3.476x；
  两遍稳定读取使本地时间增加1.583x/1.649x。该证据不含真实MPI、afl-showmap、solver或campaign，
  无coverage、throughput、bug或LAVA-M收益结论，等级I/T/E-mechanism。完整方案、图、raw data、
  本地生产wrapper集成及SHA-256证据见
  [`F344研究报告`](research-progress/Bounded_Hybrid_Worker_Result_Admission_F344_2026-08-10.md)。


## 340. F345：Stable Hybrid Input Admission and Acknowledged CAS Residency（2026-08-10）

- 审查问题：hybrid输入数据面仍以`f.read()`计算AFL queue摘要并永久按路径缓存；单次扫描可突破
  cache cap且保留全部候选后才取Top-K；frontier showmap、master派发和worker `copy2`可能分别读取
  同名路径的不同版本；CAS只看`isfile`便复用，master又在send后、worker确认物化前提前记录远端驻留，
  使损坏对象、路径换代或失败派发可能导致错误输入归因或后续错误省略payload；
- 稳定输入与有界选择：新增共享`stable_regular_file_snapshot`，以固定1 MiB窗口、
  `O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC` regular fd计算SHA-256，并闭合fd-before/fd-after/path-after的
  dev/ino/size/mtime/ctime。AFL缓存同时记录identity与digest，同名换代重算；finite K使用capacity-K
  min-heap、确定性tuple tie-break并逐插入执行cache cap，最终选中项在`K<=limit`时固定到cache尾部，
  使早枚举高分候选经历FIFO逐出后仍保留score-to-dispatch摘要栅栏；
- 前副作用准入与双模式执行：master在strategy、target/work/state lease、parameter和agentic副作用前
  稳定导入CAS，并要求resident score digest与准入object id一致；frontier showmap改在稳定CAS路径运行。
  object mode要求`object_id==sha256`，path compatibility mode也必须稳定导入worker本地CAS并匹配master
  fence，删除无界`copy2`回退。`SYMCC_MAX_TRANSPORT_INPUT`统一限制两种byte-backed模式；
- 自校验CAS与确认式驻留：已有对象只有在no-follow regular且稳定重哈希等于名称时才能复用；损坏或
  symlink对象在无content时拒绝，在收到精确content时以partial-safe write、file fsync和durable replace
  原子修复。identity cache与per-worker residency均在300000项clear-on-overflow。master只有在current
  generation RESULT回显exact object id后才记录resident；missing/mismatch会驱逐并使未来重传；
- continuation语义修复：初始concrete input改用稳定snapshot并受executor 4 MiB能力上限；自包含
  continuation以canonical checkpoint id作为dispatch identity，不再为已持久状态重开可能消失的AFL
  locator，也不进入input digest去重或worker object ACK集合；
- 验证与成本：定向205 passed加58 subtests；六模块warnings-as-errors相关349 passed加74 subtests；
  完整Python 742 passed加94 subtests（100.85秒）。本地真实文件生产原语集成11/11检查通过，覆盖两版
  digest、Top-K逐出、CAS损坏/修复、object/path模式、ACK与缺失locator continuation。32/128 MiB
  fresh-process机制实验共40 retained样本，旧/F345 traced peak为33,559,002/2,098,278 B（15.994x）和
  134,222,298/2,098,278 B（63.968x），RSS降低2.161x/5.694x；本机机制时间为旧式的0.692x/0.695x。
  sparse overlayfs结果不外推真实MPI、solver、coverage、campaign、bug或LAVA-M收益，等级
  I/T/E-mechanism。完整方案、机制图、raw samples、集成JSON与SHA-256证据见
  [`F345研究报告`](research-progress/Stable_Hybrid_Input_Admission_F345_2026-08-10.md)。

## 341. F346：Descriptor-Bound CAS Publication Closure（2026-08-10）

- 审查问题：F345的CAS修复路径在`durable_replace(tmp,path)`后关闭写fd，仅对最终path做一次
  no-follow `stat`并把该identity直接登记为已验证。若竞争writer在rename与stat之间修改同一inode或
  替换公开名称，旧路径会把未经摘要验证的竞争者identity写入正向cache；
- descriptor-bound提交：写fd保持到发布闭环结束。记录rename前`W`和rename后`D`，要求
  `(dev,ino,size,mtime)`不变，再要求rename后fd完整identity与no-follow公开路径`P`相等。`ctime`只在
  `D==P`中比较，因为成功rename可合法更新ctime；该区分避免正常路径永久退化为二次哈希；
- 乐观竞争仲裁：无竞争路径直接缓存发布inode，fallback hash严格为0。若fd内容字段变化、path非regular
  或`D!=P`，先删除旧正向事实，再用稳定no-follow SHA-256验证当前路径。同摘要不同inode被视为并发正确
  writer并幂等接受；错误摘要、symlink、消失或失稳对象拒绝且不进入identity cache，temporary始终清理；
- 验证：新增单元故障注入，固定无竞争0次回退哈希、同inode错误修改一次复核后拒绝、同摘要竞争者一次
  复核后收敛。生产原语集成覆盖baseline、same-inode corruption、wrong/exact inode competitor、symlink、
  negative cache和temporary cleanup共8/8检查；定向206 passed加58 subtests，六模块相关350 passed加74
  subtests，完整warnings-as-errors Python为743 passed加94 subtests；
- 复杂度与边界：common path只增加一次`fstat`与常数identity记录，不重读payload；仅检测到竞争时支付
  `O(object_bytes)`稳定摘要。该本地确定性故障注入不运行真实MPI、target、afl-showmap、solver或campaign，
  无throughput、coverage、bug或LAVA-M收益结论，等级I/T/E-mechanism。完整方案、图与证据见
  [`F346研究报告`](research-progress/Descriptor_Bound_CAS_Publication_Closure_F346_2026-08-10.md)。

## 342. F347：Unified Live-State CAS and Stable Snapshot Reads（2026-08-10）

- 审查问题：executable continuation虽以SHA-256对象图保存，`LiveStateStore._put_mapping`仍用
  会跟随symlink的`os.path.isfile`把“名称存在”当成“内容已验证”，损坏对象或指向根外精确内容的
  symlink会被直接复用；独立temporary/replace写路径没有F346发布闭环，`_get_mapping`普通`open`
  读取也不闭合fd/path identity，live state与hybrid input两套CAS语义持续分叉；
- 后缀感知统一CAS：`ContentAddressedInputStore`新增严格leaf-only `object_suffix`，拒绝路径分隔符和
  NUL；默认空串保持input布局，live state使用`.json`保持既有
  `objects/xx/<digest-rest>.json`及checkpoint ID完全兼容。expression、program、solver frame、
  symbolic store、COW page/root和continuation descriptor全部通过F346 descriptor-bound writer发布；
- 稳定读取与cache状态机：每个live对象以`O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC` regular fd做有界
  retain-content snapshot，同时验证读取长度、fd-before/fd-after/path-after完整identity及SHA-256，
  同一保留字节随后进入ASCII JSON/schema解析。成功读记录verified identity，供后续put走O(1)
  metadata fast path；打开、类型、上限、身份或摘要失败会撤销旧positive fact；
- 故障注入与回归：生产原语集成建立expression/frame/store/page/root/checkpoint六对象图，覆盖兼容
  `.json`布局、发布前精确symlink替换、缓存后损坏修复、同摘要竞争writer收敛、读取中path replace、
  精确symlink读取拒绝、negative cache和temporary cleanup，共8/8检查；修复与竞争各一次fallback hash。
  定向9 passed加3 subtests，六模块相关352 passed加77 subtests，完整warnings-as-errors Python为
  745 passed加97 subtests；
- 复杂度与边界：已验证重复put为O(1) metadata；首次/变化对象put及每次live read为
  O(object_bytes)，read保留空间受`max_object_bytes`约束且不会比JSON解析多读一遍。证据为本地
  regular/symlink确定性故障注入，不运行真实MPI、target、afl-showmap、solver或campaign，无throughput、
  coverage、bug或LAVA-M收益结论，等级I/T/E-mechanism。完整方案、对象图、原始JSON和SHA-256见
  [`F347研究报告`](research-progress/Unified_Live_State_CAS_F347_2026-08-10.md)。

## 343. F348：Budgeted Transitive Live-State Restore（2026-08-10）

- 审查问题：F347闭合了单个live-state对象的CAS读写，但一次checkpoint restore没有统一的唯一对象/
  累计字节预算；parent只验descriptor外壳；memory root只用`has_object`证明page名称存在，错误schema和
  symbolic leaf可能延迟到实际读页才失败；共享expression/page/root还会沿多条引用边重复稳定读取；
- Merkle DAG预算与memo：新增restore-local `_LiveStateTraversalBudget`，以digest索引已解析mapping，
  `max_graph_objects`与`max_graph_bytes`分别限制唯一节点数和canonical JSON总长度。first-seen对象在摘要
  通过后、JSON parse前做前瞻`+1/+size`准入；重复引用重新检查调用点schema后O(1)复用，不重复I/O或计费；
- 传递闭包：恢复当前checkpoint及完整parent ancestry，对每个descriptor验证canonical identity，并遍历
  solver frame/assertions、symbolic store/expressions、memory root/pages/symbolic cells及program。新增frame
  assertion上限、symbolic name唯一性、page index和cell offset唯一性/范围检查；JSON或schema失败撤销
  positive inode identity；
- 接入与观测：`LiveContinuationBundle`和`symcc_live_state.py inspect`输出唯一对象及canonical-byte
  实测值；MPI master/worker通过`SYMCC_LIVE_GRAPH_MAX_OBJECTS`（默认262144）与
  `SYMCC_LIVE_GRAPH_MAX_BYTES`（默认256 MiB）消费同一有界配置；磁盘schema、路径、摘要和checkpoint ID
  不变；
- 验证：两层parent DAG为8个唯一对象、2062 canonical bytes且恰好8次稳定snapshot；精确8/2062预算
  通过，7对象或2061字节拒绝；parent深层wrong-schema page、重复symbolic name和invalid JSON均失败关闭，
  生产原语集成9/9。定向4 passed加4 subtests，六模块相关356 passed加81 subtests，完整Python
  749 passed加101 subtests；
- 边界：canonical bytes不是CPython heap精确值；未引入独立edge quota或父目录openat2能力。证据不运行
  真实MPI、target、afl-showmap、solver或campaign，无throughput、coverage、bug或LAVA-M收益结论，等级
  I/T/E-mechanism。完整方案、图、原始JSON和SHA-256见
  [`F348研究报告`](research-progress/Budgeted_Transitive_Live_State_Restore_F348_2026-08-10.md)。

## 344. F349：Descriptor-Anchored CAS Namespace（2026-08-10）

- 审查问题：F346只闭合写fd与最终leaf，F347/F348也仍通过普通父pathname定位CAS root与digest shard；
  共享存储竞争者若在发布或读取中rename root/shard再放入同名目录或symlink，旧路径可能重定向后续I/O，
  或把只存在于detached旧目录的对象误登记为公开namespace中的成功；
- root/shard capability：CAS root以`O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`打开并闭合fd/path identity；
  shard通过root fd相对`mkdir/open`，同样记录device/inode。内部对象操作只接收`{shard fd,fixed leaf}`，
  temporary create、replace、stat、snapshot与unlink均不重新解析父pathname；
- 使用后namespace closure：`_object_directory`在内部I/O正常完成后复核公开root与root fd、root-fd下的
  shard name与shard fd。目录被detach时I/O仍留在原inode而不会穿过新alias，但最终返回失败；positive
  identity只有上下文成功退出后才登记，任何对象/目录失败都撤销旧事实；
- 发布与读取统一：`durable_replace`支持同一directory fd的`renameat`语义和directory fsync；稳定snapshot
  支持descriptor-relative leaf。LiveStateStore经同一anchored snapshot消费JSON，摘要、解析字节、leaf inode
  与目录归属形成一次事务；同摘要leaf competitor仍一次fallback hash收敛，无竞争cache hit保持零payload hash；
- 验证：root/shard symlink、发布中root/shard替换、live read中shard替换且alias含精确对象、exact competitor、
  cache与temporary均有确定性故障注入。机制集成11/11，4个redirected external目录均0 entry、4个失败store
  均0 cached identity、detached bytes精确且temporary residue 0；定向5 passed，六模块相关360 passed加81
  subtests，完整warnings-as-errors Python为753 passed加101 subtests，Ruff通过；
- 边界：本实现是portable Python `dir_fd`/openat-family闭环，不是从受信祖先开始的
  `openat2(RESOLVE_BENEATH)`，也不证明mount或Byzantine存储语义。本地overlayfs证据不运行真实MPI、target、
  afl-showmap、solver或campaign，无throughput、coverage、bug或LAVA-M收益结论，等级I/T/E-mechanism。
  完整方案、机制图、11-check JSON、测试日志和SHA-256证据见
  [`F349研究报告`](research-progress/Descriptor_Anchored_CAS_Namespace_F349_2026-08-10.md)。

## 345. F350：Component-Wise Anchored CAS Root（2026-08-10）

- 审查问题：F349只对完整绝对pathname的最终CAS root分量设置`O_NOFOLLOW`，构造前的普通
  `durable_makedirs`仍会沿祖先symlink创建后代；已有真实root位于symlink祖先下时构造还会直接成功。
  每次操作重开root也会解析这些未绑定祖先，使“root/shard已锚定”证明建立在尚未证明的prefix上；
- 逐组件能力链：构造时把root固定为绝对内部/公开路径，从`/`目录fd开始，对每个组件先执行相对父fd的
  `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`打开。仅`ENOENT`进入`mkdirat(parent_fd,leaf,0777&umask)`，并发
  `EEXIST`后重新no-follow打开；新child和parent均fsync，前一fd随遍历关闭，所有异常闭合当前fd；
- 使用后ancestry closure：每次对象事务都以同一strict walk取得root fd；内部shard/leaf I/O结束后再次
  从`/`重走，并要求最终device/inode等于本次root fd，随后再闭合shard identity。祖先detach后I/O留在
  已打开的原目录，但API失败且不登记positive cache；alias即使包含同摘要对象也不能替代namespace proof；
- 路径稳定性与兼容：`self.root`与`object_path`统一为构造时绝对路径，跨`chdir`仍指向真实CAS。磁盘布局、
  摘要、suffix和checkpoint ID不变；新建root组件保持旧`0777&umask`，shard保持`0700&umask`。任一祖先
  symlink现在明确拒绝，部署须提供真实无symlink绝对路径；
- 验证：新增嵌套创建、相对根跨chdir、缺失/已存在root的祖先symlink、发布中祖先替换、live read中祖先
  替换且alias含exact JSON共6个测试。定向8 passed，六模块相关366 passed加81 subtests，完整
  warnings-as-errors Python为759 passed加101 subtests；生产原语集成11/11，4个新建目录调用均为leaf-only
  dirfd、external副作用0、失败cache 0、detached bytes精确、competitor fallback hash 1、temporary 0；
  F346-F349历史driver仍为8/8、8/8、9/9、11/11，Ruff通过；
- 边界：当前是Python `dir_fd`/openat-family循环，不是一条原子`openat2`解析；不禁止mount crossing、
  不检测replace-and-restore ABA，也不回滚更早创建的真实组件。本地overlayfs结果不运行真实多机、MPI、
  target、afl-showmap、solver或campaign，无throughput、coverage、bug或LAVA-M收益结论，等级
  I/T/E-mechanism。完整方案、机制图、11-check JSON、测试日志和SHA-256证据见
  [`F350研究报告`](research-progress/Component_Wise_Anchored_CAS_Root_F350_2026-08-10.md)。

## 346. F351：Identity-Closed Cluster-Lock Qualification（2026-08-10）

- 审查问题：F329-F333在stable lock inode上验证跨processor排斥与close-release，但每轮以pathname
  重开并在结束后关闭全部描述符；一个字节/token完全相同的新inode可在行为轮次后替换公开名称，使旧
  inode上的通过结果被错误归给新锁域。运行期续期还沿用初始capability的state/publication dev/fsid，
  没有确认当前路径仍绑定同一文件系统；
- 双层绑定：本地membership前重新打开canonical state/publication root并核对`st_dev/f_fsid`。每个master
  保持`O_DIRECTORY|O_NOFOLLOW` root fd和相对该root打开的`O_NOFOLLOW` regular lock fd跨越全部轮次；rank 0
  准备、每轮open与release verifier均使用`openat(root_fd,fixed_leaf)`，避免在事务内重走父pathname；
- namespace closure：锁轮转完成后，每个master重新打开公开root，比较root fd `(dev,ino)`，再相对新root
  打开leaf，比较anchor `(dev,ino)`并复核exact epoch token。独立`namespace-identity-closed` MPI phase要求
  全部`M`个检查成功；升级gate同时验证round=`H`、contention=`H*(M-1)`、release=`H`、identity=`M`；
- 证据代际与消费门：成功能力改为v3 snapshot、`probe_scope=cross-host-mpi-lock-v2`并记录
  `cluster_lock_identity_checks`。renewal controller要求v2、至少2个identity及result/member/capability基数
  一致；standalone runtime setup还要求identity count等于live master数。旧v1布尔verified不能绕过重资格；
- 验证：同内容leaf替换保持SHA-256相同但inode不同，两方完成2轮后均identity=0并失败；伪造state device
  或publication fsid在round 0全体拒绝；同processor name保持clean-but-unverified；legacy v1 renewal记1次
  failure。机制driver 8/8，定向4 passed（186 deselected），相关369 passed加81 subtests，完整
  warnings-as-errors Python 762 passed加101 subtests；Ruff、py_compile与diff check通过；
- 边界：集成使用本地overlayfs、线程控制面和合成processor name，不是实际多机MPI或共享NFS/Lustre
  部署认证。最终check之后仍可能变化，不检测replace-and-restore ABA，也未把F350逐组件walk抽到共享root；
  无throughput、coverage、bug或LAVA-M收益结论，等级I/T/E-mechanism。完整机制图、8-check JSON、测试日志
  和SHA-256证据见
  [`F351研究报告`](research-progress/Identity_Closed_Cluster_Lock_Qualification_F351_2026-08-10.md)。

## 347. F352：Generation-Bound Lock-Proof Transcript（2026-08-10）

- 审查问题：renewal completion只检查clean/verified、v2 scope和identity基数，没有把返回对象绑定当前
  in-flight generation，也没有逐字段核对result、capability和live master拓扑；旧代成功结果或
  “旧capability + 零轮次/异根result”拼接对象可被错误计为本代成功；
- 规范化转录：新增domain-separated SHA-256，覆盖epoch、generation、有序rank/processor成员、processor
  代表及round/contention/release/identity四类计数。整数用big-endian u64，UTF-8 processor写入u32字节
  长度，避免JSON/`repr`顺序与字段边界歧义；result显式携带generation和proof transcript；
- 全master提交：F351结构与namespace identity闭合后，新增`qualification-transcript-commit`有界MPI
  phase；所有master提交同一`(generation, transcript)`，任一畸形、代次或摘要分歧使全体失败，不允许
  局部成功；
- 严格完成门：controller重跑F351 evidence-shape gate并重算transcript；生产路径还绑定有序
  `master_ranks`和client-local startup capability精确值。旧代重放、result字段拼接、capability计数拼接
  和foreign-root capability均计failure；启动与成功续期由root输出transcript供审计；
- 验证：定向4 passed加13 subtests（27 deselected），六模块相关373 passed加94 subtests，完整
  warnings-as-errors Python 766 passed加114 subtests（96.95 s）。生产机制driver 10/10：M=3/H=2/g=7
  三方摘要相同且可重算，单方摘要变异使三方在2轮/3 identity后统一失败，旧代重放、零轮次与异根
  capability全部拒绝，八类字段变异产生八个不同摘要；Ruff与py_compile通过；
- 边界：SHA-256无密钥，仅用于受信框架内部的重放、拼接与分歧检测，不是签名/MAC或Byzantine认证；
  集成使用本地overlayfs、线程控制面和合成processor name，不是实际多机MPI或共享存储部署证据，未运行
  solver、coverage或LAVA-M campaign，无性能/覆盖收益结论，等级I/T/E-mechanism。完整方案、机制图、
  10-check JSON与SHA-256 evidence见
  [`F352研究报告`](research-progress/Generation_Bound_Lock_Proof_Transcript_F352_2026-08-10.md)。

## 348. F353：Failure-Atomic Renewal Result Admission（2026-08-10）

- 审查问题：F352强化了资格证明，但`ClusterLockRenewalController.complete()`仍在结果结构验证前推进
  generation、清除in-flight并增加attempt。`members=object()`使验证抛`TypeError`后，快照变为
  `generation=1,in_flight=0,attempts=1,successes=0,failures=0`，既消费代次又违反attempt守恒；
- 总化准入：result gate要求members/representatives为精确tuple，并在完整异常边界内重放F351结构与
  F352 transcript/capability检查；畸形字段稳定映射为False。generation增加精确int门，拒绝Python
  `True == 1`别名；elapsed只接受有限非负int/float，NaN/非数值拒绝且不会污染累计遥测；
- 单提交点：proof验证、下一代jitter计算和累计耗时/溢出检查全部前移。可识别坏proof越过提交点后形成
  完整failure并保持`attempts=successes+failures`；validator/scheduler等内部异常不越过提交点，generation、
  in-flight、schedule和全部metrics逐字段保持原快照；成功路径没有新增MPI轮次、I/O或payload复制；
- 验证：F353定向2 passed加7 subtests（31 deselected），资格模块加MPI生命周期92 passed加75 subtests，
  六模块相关375 passed加101 subtests；生产controller/capability机制集成10/10，覆盖1个成功、5类畸形
  结果、2个内部异常与1个布尔代次，并检查所有已提交attempt守恒和异常快照完全不变；完整
  warnings-as-errors Python为768 passed加121 subtests（94.70秒）；
- 边界：这是本地in-memory controller的强异常安全，不是数据库事务、分布式atomic commit或持久WAL。
  机制证据未运行真实MPI、多机共享存储、target、solver、coverage、campaign或LAVA-M，不作性能/覆盖/
  漏洞提升结论，等级I/T/E-mechanism。完整方案、状态机图、10-check JSON、测试日志与SHA-256证据见
  [`F353研究报告`](research-progress/Failure_Atomic_Renewal_Result_Admission_F353_2026-08-10.md)。

## 349. F354：Rank-Contained Renewal Completion（2026-08-10）

- 审查问题：F353让controller内部异常保持完整pre-state，但生产调用方只捕获
  `RuntimeError/TypeError/ValueError`。`total_elapsed + elapsed`真实溢出会抛`OverflowError`，因此单个
  master仍可能越过统一fatal-control路径展开Python栈，而其他rank处于资格协议或有序关闭中；
- 显式异常隔离：新增`complete_cluster_lock_renewal()`作为唯一completion边界。有效proof返回
  `(True, "")`并提交success；proof rejection返回`(False, "")`并提交failure；普通`Exception`返回
  `(False, bounded_error)`且由F353保证逐字段pre-state不变。诊断删除控制字符并限制为512字符，异常格式化
  自身失败时退化为类型名；
- 进程控制与生产接入：边界只捕获`Exception`，不吞掉`KeyboardInterrupt/SystemExit`等`BaseException`。
  standalone runtime不再枚举异常类型，所有完成操作经统一API；非空accounting error进入既有
  fatal-control shutdown，空错误串的False仍走proof失败路径，避免错误记账和错误诊断；
- 验证：定向1 passed（33 deselected），资格模块加MPI生命周期93 passed加75 subtests，六模块相关
  376 passed加101 subtests，完整warnings-as-errors Python 769 passed加121 subtests（97.05秒）。生产
  controller/capability/transcript边界机制集成9/9，覆盖有效/拒绝、真实binary64累计溢出、注入
  `LookupError`、512字符诊断上限、错误controller及`KeyboardInterrupt`透传；异常场景逐字段快照不变；
- 边界：这是本地rank的普通异常值化与失败关闭，不是ULFM revoke/shrink、rank replacement、跨rank异常
  广播或失败后继续执行。证据未运行真实MPI、多机共享存储、target、solver、coverage、campaign或
  LAVA-M，不作性能、覆盖或漏洞提升结论，等级I/T/E-mechanism。完整方案、机制图、9-check JSON、测试日志
  与SHA-256证据见
  [`F354研究报告`](research-progress/Rank_Contained_Renewal_Completion_F354_2026-08-10.md)。

## 350. F355：Total Cluster-Lock Qualification Boundary（2026-08-10）

- 审查问题：F354闭合了renewal result消费异常，但公共qualification生产函数仍依赖内部各层人工维护的
  exception type tuple。真实communicator adapter从`Get_rank()`抛`LookupError`时没有生成result，直接
  绕过startup `Abort(65)`和runtime F353/F354 fatal-control链；
- 公共总化边界：原协议实现私有化为`_qualify_mpi_cluster_advisory_lock()`，公共同签名API只调用一次。
  正常结果原样返回；普通`Exception`统一映射为`clean=false/verified=false`、空成员/代表/transcript、
  零proof计数和`elapsed=0.0`哨兵的结构合法失败结果；
- 有界类型化诊断：有效精确int generation与有效capability保留，布尔/越界generation归零，错误capability
  归`None`；固定前缀加错误详情的总长度不超过512字符，换行/NUL清洗，异常`__str__`再次失败时使用类型名；
  类型名自身也经过同一清洗和截断，超长动态类名不能绕过上限；仅捕获`Exception`，
  `KeyboardInterrupt`等`BaseException`继续传播；
- 生产消费组合：startup失败结果进入现有root诊断与`Abort(65)`；runtime失败结果由F353提交一次proof
  failure，再经F354空accounting-error分支进入fatal-control shutdown。producer异常形成失败尝试，consumer
  异常仍保留pre-state，两种事务语义不混淆；成功路径不增加MPI phase、文件I/O、hash或payload复制；
- 验证：F355定向1 passed（34 deselected），资格模块加MPI生命周期94 passed加75 subtests，六模块相关
  377 passed加101 subtests；生产公共API机制集成10/10，覆盖clean-but-unverified语义、真实communicator/
  monotonic `LookupError`、generation/capability保留与错误输入归一化、512字符清洗、异常文本失败及
  `KeyboardInterrupt`透传；完整warnings-as-errors Python为770 passed加121 subtests（95.48秒）；
- 边界：本地真实filesystem probe加合成communicator不等于实际MPI或多机共享存储。实现不提供ULFM、
  rank replacement、跨rank异常广播或同步错误共识；peer仍可能等待既有deadline。未运行solver、coverage、
  campaign或LAVA-M，不作性能、覆盖或漏洞提升结论，等级I/T/E-mechanism。完整方案、机制图、10-check JSON、
  回归日志与SHA-256证据见
  [`F355研究报告`](research-progress/Total_Cluster_Lock_Qualification_Boundary_F355_2026-08-10.md)。

## 351. F356：Collective-Safe Qualification Input Observation（2026-08-10）

- 审查问题：F355公共qualification已总化，但startup本地filesystem probe与startup/runtime processor
  provider仍分别枚举少数异常类型；两个真实`LookupError`反例均在F355前逃逸，使本rank跳过membership，
  peer只能等待deadline且统一Abort/fatal-control入口被绕过；
- 统一观测结果：新增冻结`ClusterLockQualificationObservation`和
  `observe_cluster_lock_qualification_inputs()`。已有错误、可选capability probe、既有capability与processor
  provider按固定次序观测；每个provider最多调用一次，filesystem失败后仍继续processor观测。普通异常、
  非法返回、空/超长/NUL processor归一化为有界失败输入，`BaseException`传播；
- 失败可表达的严格schema：membership只在`local_probe_ok=true`时要求processor非空；显式失败record可为空，
  但字段集合、rank、布尔类型、字符串类型/长度/NUL仍严格验证。已有local error优先于泛化processor错误，
  失败在representative选择和lock round前收敛，不放宽成功证明；
- 生产接入与复用：startup将真实filesystem probe闭包和MPI processor provider交给同一边界；runtime复用既有
  capability并合并前置heartbeat error。F355异常文本与动态类型名回退抽成共享原语；成功路径不增加probe、
  MPI phase、I/O、hash、tag或schema；
- 验证：F356定向1 passed（35 deselected），资格模块加MPI生命周期95 passed加75 subtests，六模块相关
  378 passed加101 subtests；生产API机制集成12/12，覆盖clean/双provider异常/继续观测/非法返回/上游错误/
  512字符/文本失败/双BaseException及双rank零滞留、同错误、零proof收敛；完整warnings-as-errors Python为
  771 passed加121 subtests（96.48秒）；
- 边界：真实本地probe加线程message bus不是实际MPI或多机共享存储；不提供ULFM、rank replacement、
  provider永久阻塞恢复或跨rank异常共识。runtime heartbeat与前后clock尚未总化；未运行solver、coverage、
  campaign或LAVA-M，不作性能、覆盖或漏洞提升结论，等级I/T/E-mechanism。完整方案、机制图、12-check JSON、
  回归日志与SHA-256证据见
  [`F356研究报告`](research-progress/Collective_Safe_Qualification_Observation_F356_2026-08-10.md)。

## 352. F357：Renewal Heartbeat and Clock Containment（2026-08-10）

- 审查问题：运行期续期接受generation后，`heartbeat_all()`仍只按`OSError`人工捕获；资格前和qualification
  result后分别还有一次位于F355/F354外的`time.monotonic()`。Heartbeat或完成时钟抛`LookupError`会使单rank
  跳过membership或completion统一失败路径；coordinator也在验证batch结构前更新本地metrics；
- 一次性heartbeat观测：新增冻结`WorkLeaseHeartbeatObservation`和`observe_work_lease_heartbeat()`。provider
  最多调用一次，只接受无重复规范work-hash tuple；普通异常、非法list/摘要/重复项/不可哈希嵌套值归一化为
  512字符内错误，异常文本失败退化为类型名，`BaseException`传播；
- 准入后提交：`_SharedWorkCoordinator.heartbeat_all()`先验证`LeaseHeartbeatBatch`类型、精确sync计数、摘要、
  去重、不相交与renewed/lost完整partition，再提交metrics/token；所有普通失败只增加一次failure计数。外部lease
  写入可能已经发生，因此错误路径失败关闭且不自动重试；
- 时钟闭包：production续期复用已通过`due()`的event-loop `now`，删除资格前额外clock；F354公共completion
  API在自己的`Exception`边界内调用可注入monotonic provider，时钟错误保留F353完整pre-state并进入accounting
  fatal-control，proof rejection仍提交恰好一次failure；
- 验证：定向3 passed加4 subtests（94 deselected），资格模块加MPI生命周期97 passed加79 subtests，六模块相关
  380 passed加105 subtests；生产API机制集成14/14，覆盖一次调用、畸形返回、有界诊断、双`BaseException`、
  completion clock pre-state及双rank同错误/零proof/单次failure；完整warnings-as-errors Python为773 passed加
  125 subtests（97.16秒）；
- 边界：真实本地probe加线程message bus不是实际MPI或多机共享存储；不提供已部分完成heartbeat的rollback/
  retry、provider hang恢复、ULFM或rank replacement。未运行solver、coverage、campaign或LAVA-M，不作提升结论，
  等级I/T/E-mechanism。完整方案、机制图、14-check JSON、回归日志与SHA-256证据见
  [`F357研究报告`](research-progress/Renewal_Heartbeat_and_Clock_Containment_F357_2026-08-10.md)。

## 353. F358：Renewal Control Transport Containment（2026-08-10）

- 审查问题：F357闭合heartbeat/clock后，请求`begin/poll/Test`仍枚举人工异常类型。root在
  `begin_request()`已把controller置为generation 1后，`isend()`抛`LookupError`会直接展开栈；调用方既拿不到
  generation，也丢失此前成功创建的send handle，已经收到请求的peer可能独自进入qualification；
- 状态感知begin边界：communicator或begin前普通异常返回`generation=0`并保留pre-state；generation建立后
  的任一发送异常返回非零generation、所有已创建句柄和512字符内错误。其余peer发送仍继续尝试，生产root即使
  partial send也进入有界qualification，再由fatal-control关闭，保留部分进度而不伪装原子回滚；
- peer分类：`Get_rank/Get_size/iprobe/recv/accept_request`普通异常全部值化。无消息继续，安全接收但格式/代次
  错误的消息quarantine，合法请求进入精确generation，local admission异常作为不可安全消费请求进入
  fatal-control；`BaseException`仍传播；
- 三值完成观测：新增冻结`ClusterLockRenewalDeliveryObservation`与
  `observe_cluster_lock_renewal_delivery()`，每个唯一句柄最多调用一次`Test()`，分别累计completed、incomplete、
  uncertain并保持`T=C+I+U`；duplicate在任何调用前拒绝，异常文本再次失败时退化为类型名，不自动重试不确定
  send；
- 验证：F358定向5 passed（36 deselected），资格模块加MPI生命周期100 passed加79 subtests，六模块相关383
  passed加105 subtests；确定性4-rank点到点driver 15/15，partial send保留generation 1与2个句柄，rank 1/3
  接受而rank 2未观察，并验证1/1/1三值分类、一次调用、有界诊断及三个`KeyboardInterrupt`；完整
  warnings-as-errors Python `test/`为776 passed加125 subtests（97.41秒）；
- 边界：合成bus不是实际MPI、collective或多机证据；部分发送不回滚、不重试且未实现ACK、ULFM、cancel/free、
  communicator修复或rank replacement。未运行solver、coverage、campaign或LAVA-M，不作性能、覆盖和漏洞提升
  结论，等级I/T/E-mechanism。完整方案、机制图、15-check JSON、回归日志与SHA-256证据见
  [`F358研究报告`](research-progress/Renewal_Control_Transport_Containment_F358_2026-08-10.md)。

## 354. F359：Hermetic Pytest Discovery Boundary（2026-08-10）

- 审查问题：仓库根没有pytest配置，无位置参数的`python3 -m pytest`在发现776个父项目测试后继续递归进入
  vendored QSYM的6个原生模块；当前环境没有独立构建的`qsym`/PIN runtime，因而在任何测试执行前以6个
  `ModuleNotFoundError`和exit 2终止。该结果既不是父项目回归，也不是QSYM功能失败，而是测试生命周期边界错误；
- 默认路由而非隐藏：根`pytest.ini`仅设置`testpaths = test`，使无位置参数入口精确发现SymCC-Parallel测试。
  不设置`norecursedirs`、`--ignore`、动态skip或伪模块；显式传入QSYM目录时pytest仍进入其测试并透明报告构建
  前置条件，保留独立backend job的可达性；
- 防回退契约：新增两个pytestconfig测试，验证实际rootpath、单一testpaths、QSYM路径位于项目test根之外、
  `norecursedirs`不含QSYM且`addopts`没有`--ignore`。测试不要求嵌套子模块已初始化，使浅检出仍可验证父项目配置；
- 验证：修复前真实基线为776 collected加6 errors、exit 2；加入两个契约测试后，默认根collect-only为778
  collected、exit 0、零QSYM路径，命令行清空testpaths的反事实为同778 collected加6 errors、exit 2；显式QSYM
  入口仍到达`test_utils.py`并报告`prerequisite-missing`，显式父项目模块收集9项。四路生产命令机制集成12/12，
  完整无路径根命令warnings-as-errors为778 passed加125 subtests（97.27秒）；
- 边界：本轮没有构建或运行QSYM/PIN原生测试，也没有执行LLVM lit、solver、MPI、coverage、campaign、公开
  benchmark或LAVA-M；单次collect时间不构成性能benchmark。F359是hermetic testing与证据可重复性改进，不是
  符号执行算法SOTA或覆盖/漏洞提升，等级I/T/E-mechanism。完整方案、分层图、12-check JSON、回归日志与
  SHA-256证据见
  [`F359研究报告`](research-progress/Hermetic_Pytest_Discovery_Boundary_F359_2026-08-10.md)。

## 355. F360：Capability-Closed Python CI Gate（2026-08-10）

- 审查问题：F359建立了可靠的根pytest入口，但GitHub Actions没有任何job运行这套Python测试，且依赖清单未声明
  pytest；solver、AFL++、LLVM、parser、MPI等集成能力缺失时，普通pytest还可能以skip退化后保持绿色；
- 能力闭包：新增独立`python_quality` job，递归checkout、安装Python 3.12与系统/Python依赖、缓存固定
  Bitwuzla 0.9.1，并在pytest前检查10个命令、5个Python模块和1个动态库。缺失能力返回2，不进入测试；
- 零降级门：`util/python_test_gate.py`通过pytest公共hooks记录普通测试、subtest、xfail/xpass、反选和收集错误；
  当前CI契约要求collection不少于历史下界778，skip（含subtest）、xfail、xpass和deselection全为0。结果经临时文件
  加`os.replace`原子写入JSON，CI在成功或失败时都上传artifact；
- 深度修复：首次完整门禁终端为125 subtests，JSON仅121。根因是三个环境变量使用相同malformed参数，显示标签
  重复而被集合折叠。recorder改为按hook事件计数，并加入三个同label subtest必须计3/3的回归；同时修复subtest
  skip未进入`max_skips`的遗漏；
- 验证：门禁契约6 passed，Ruff、py_compile、actionlint 1.7.7和diff检查通过；空Python 3.12 venv仅用两份
  requirements成功安装并完成2项gate smoke；最终本地等价CI门禁为784 passed加125 subtests（98.30秒），
  0 failed/skip/xfail/xpass/deselected/collection error，16项能力0 missing，终端与JSON计数一致；
- 边界：本轮未触发GitHub托管runner，actionlint与本地等价命令不能替代远端job结果；没有运行QSYM/PIN原生测试、
  LLVM lit、真实MPI、solver/coverage campaign、公开benchmark或LAVA-M，不作性能、覆盖或SOTA提升结论，等级
  I/T/E-mechanism。完整方案、流程图、机器可读JSON、空环境记录和SHA-256证据见
  [`F360研究报告`](research-progress/Capability_Closed_Python_CI_Gate_F360_2026-08-10.md)。

## 356. F361：Canonical Pytest Identity Inventory（2026-08-10）

- 审查问题：F360 的 `min_collected=778` 只能阻止测试数量明显下降，无法识别“删除测试 A、增加无关测试 B”
  这种总数不变的覆盖退化；零 skip、pytest exit 0 和 collection floor 在该反事实下都会继续通过；
- 规范身份清单：新增 `util/python_test_inventory.py` 和版本化 `test/pytest-nodeids.json`，将 `test/` 下 787 个
  pytest nodeid 规范化为有界、排序、唯一、根约束的 UTF-8 列表，并以逐项 LF 编码的 SHA-256 摘要绑定内容。
  reader 严格拒绝未知字段、越界输入、乱序、重复、root 逃逸、count 或摘要不一致，writer 通过临时文件加
  `os.replace` 原子发布；
- 精确准入：`util/python_test_gate.py` 升级为 v2，保留 F360 能力闭包和结果退化门，同时比较完整 expected /
  observed 集合；CI 要求 missing、unexpected、duplicate 全为零，并把差异计数、摘要、有界详情和截断状态写入
  JSON。生成器、gate 和 workflow 默认关闭第三方 pytest 插件自动加载，所需插件必须显式声明；
- 反事实证明：以真实清单删除一个 nodeid、增加一个同根 replacement 并重算合法摘要，expected 与 observed 都为
  787 且 floor 满足，但 gate 准确报告 `missing=1`、`unexpected=1` 并 exit 1；原清单 collect-only 为
  787/787、摘要一致并 exit 0；
- 验证：定向契约扩展为 9 passed；连续重建 JSON 字节一致；空 Python 3.12 venv 复核 787/787；Ruff、
  `py_compile`、actionlint 1.7.7 与 diff 检查通过；完整本地等价门禁为 787 passed 加 125 subtests
  （96.38 秒），16 项能力零缺失且结果/身份两类退化均为零；
- 边界：nodeid 证明测试身份集合而不证明测试体或 oracle 语义，重命名会产生有意差异，SHA-256 是内容校验而非
  签名。本轮未触发 GitHub 托管 runner，也未运行 LLVM lit、QSYM/PIN、真实 MPI、solver/coverage campaign、
  公开 benchmark 或 LAVA-M，不作符号执行性能、覆盖或漏洞提升结论，等级 I/T/E-mechanism。完整方案、示意图、
  真实/反事实 JSON、空环境记录和 SHA-256 证据见
  [`F361研究报告`](research-progress/Canonical_Pytest_Identity_Inventory_F361_2026-08-10.md)。

## 357. F362：Single-Snapshot Pytest Manifest Admission（2026-08-11）

- 审查问题：F361 reader 先 `stat` 再按路径 `read_text`，16 MiB 上限与解析字节未绑定同一对象；标准
  `json.loads` 静默接受重复 member；生成器/gate 的 `setdefault` 在隔离变量已存在但为空时不会建立所声明的
  plugin boundary；
- 单快照准入：reader 从一个 `rb` 描述符最多读取 `MAX+1` 字节，在 UTF-8/JSON 解析前判定上限，随后 schema、
  排序、count 与摘要验证全部消费同一有界快照；删除预读 path stat，路径在 open 后替换不再使大小证明和解析
  指向不同对象；
- 无歧义 JSON：`object_pairs_hook` 在 dict 构造前拒绝任意重复 member，即使两个值相同也不采用 first/last-wins；
  错误在 pytest 启动前形成 gate preflight exit 2、`pytest.exit_code=null` 与 `collected=0`；
- 强制环境协议：两个生产入口无条件把 `PYTEST_DISABLE_PLUGIN_AUTOLOAD` 设为字符串 `1`，JSON 仅在值精确为 `1`
  时报告隔离启用；显式 `-p` 插件路径保持可用；
- 反事实与验证：226 B 合法清单在 32 B 测试上限、伪造 `stat=1 B` 下以零 stat 调用拒绝；重复 `count` 在执行前
  拒绝；空环境变量被规范化后 1/1 fixture 通过。现有 9 个 nodeid 内扩展断言，清单仍为 787 项且字节一致；完整
  capability + identity 门禁为 787 passed 加 125 subtests（97.69 秒），16 项能力、结果与身份退化均为零；
- 边界：单描述符快照关闭 path reopen 竞态，但不是 hostile filesystem/in-place writer 的形式化证明；摘要不是
  签名，nodeid 仍不证明测试体语义。本轮未触发 GitHub 托管 runner，也未运行 LLVM lit、QSYM/PIN、真实 MPI、
  solver/coverage campaign、公开 benchmark 或 LAVA-M，不作性能、覆盖或漏洞提升结论，等级 I/T/E-mechanism。
  完整方案、示意图、三类反事实 JSON、回归日志和 SHA-256 证据见
  [`F362研究报告`](research-progress/Single_Snapshot_Pytest_Manifest_Admission_F362_2026-08-11.md)。

## 358. F363：Bounded Single-Snapshot Query IR Admission（2026-08-11）

- 审查问题：异步 QueryStore 文件入口先用 `Path.stat()` 检查 256 MiB，再重新打开路径交给 `json.load()`；大小
  证明和解析字节可属于不同对象，第二次读取也没有解析前硬上限。默认 JSON decoder 还会静默采用
  last-member-wins 并接受 `NaN`/正负无穷；
- 有界单快照准入：新增 `_load_query_envelope()`，从一个 `rb` 描述符读取至多 `MAX+1` 字节，超限在 UTF-8/JSON
  构造前失败；正好 MAX 字节继续解析。`ingest_file()` 仅在 loader 返回顶层 object 后调用既有语义与持久入库；
- 严格 JSON 与失败关闭：`object_pairs_hook` 拒绝任意层级重复 member，`parse_constant` 拒绝非有限 token，顶层非
  object、UTF-8、语法和递归错误均明确拒绝。spool 继续把失败项及 `.error` 移入 `rejected/`，坏输入不进入
  content-addressed objects、prefix trie 或 SQLite work queue；
- 生产反例：845 B 合法 envelope 在 845 B 精确上限下接受；同一 envelope 在 64 B 上限且伪造 `stat=1 B` 时以
  0 次 stat 调用拒绝。good/duplicate-schema/NaN 三项同批 spool 得到 imported=1、failed=2，最终 SQLite 严格
  queries=1、witnesses=1；
- 验证：既有 QueryStore 20 passed（2.14秒），QueryStore/QF_BV/schedule/semantic proposal 关联 124 passed
  （14.86秒）；787-node inventory 字节一致；完整 capability + identity gate 为 787 passed 加 125 subtests
  （98.41秒），16项能力、结果与身份退化均为零；
- 边界：256 MiB 约束 raw bytes 而非完整 RSS；尚未要求 no-follow regular descriptor、原地写事务隔离或多消费者
  spool claim。本轮未触发 GitHub 托管 runner，也未运行 LLVM lit、QSYM/PIN、真实 MPI、solver/coverage
  campaign、公开 benchmark 或 LAVA-M，不作性能、覆盖或漏洞提升结论，等级 I/T/E-mechanism。完整方案、机制图、
  五类生产反例、回归日志和 SHA-256 证据见
  [`F363研究报告`](research-progress/Bounded_Single_Snapshot_Query_IR_Admission_F363_2026-08-11.md)。

## 359. F364：Stable Query IR and Crash-Released Spool Admission（2026-08-11）

- 审查问题：F363虽已闭合Query IR的有界严格JSON入口，但服务没有多消费者claim；两个实例可以枚举并入库同一
  `incoming/query.json`，随后一个实例在文件移动时抛未处理`FileNotFoundError`并留下误导性`.error`。最终symlink
  会被跟随，FIFO还可能阻塞reader，读取后也没有descriptor/path身份复核；
- 稳定文件准入：`_load_query_envelope()`以`O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`打开最终leaf，只接受regular
  file；按最多1 MiB块读取至`MAX+1`，并在解析前复核descriptor-after与no-follow path-after的六字段身份。symlink、
  FIFO、读后path替换和可观测原地修改均在QueryStore持久化前失败关闭；
- crash-released单消费者协议：每个spool使用固定`.ingest.lock`，要求锁descriptor/path为同一regular inode，
  再以`flock(LOCK_EX | LOCK_NB)`尝试准入。忙锁直接返回`(0,0)`且不扫描、不调用store；赢家持锁完成整批
  scan/ingest/move。`finally`、普通进程退出或`SIGKILL`均由内核关闭fd并释放锁，commit后move前的输入可按内容
  身份幂等重放；
- 反事实与故障证据：旧双消费者算法确定得到2次ingest、1次正常返回、1个未处理ENOENT和1个误导错误文件；真实
  持锁子进程被`SIGKILL(-9)`后，新服务接管为`(1,0)`且query/witness严格1/1。good+symlink+FIFO为1 accepted / 2
  rejected且不阻塞；读后inode替换和symlink/FIFO锁对象均形成0次错误入库；
- 验证：QueryStore定向20 passed加2 subtests（2.25秒），四模块关联124 passed加2 subtests（15.22秒）；787-node
  inventory字节一致；完整capability + identity gate为787 passed加127 subtests（98.85秒），16项能力、结果与身份
  退化均为零；
- 边界：`flock`只约束协作消费者，本轮未做跨主机filesystem资格验证、hostile namespace/metadata ABA证明、父目录
  逐组件锚定、backlog吞吐/公平性测量或全局exactly-once。未触发GitHub托管runner，也未运行LLVM lit、QSYM/PIN、
  真实MPI、solver/coverage campaign、公开benchmark或LAVA-M，不作性能、覆盖或漏洞提升结论，等级
  I/T/E-mechanism。完整方案、机制图、真实SIGKILL接管、反例JSON、测试日志和SHA-256证据见
  [`F364研究报告`](research-progress/Stable_Query_IR_and_Crash_Released_Spool_Admission_F364_2026-08-11.md)。

## 360. F365：Outcome-Separated Query Spool Commit（2026-08-11）

- 审查问题：F364的`ingest_spool()`仍把QueryStore入库和accepted move放在同一个`except Exception`内。有效查询
  已持久化后若accepted publication抛`OSError`，旧生产控制流返回`(0,1)`、把输入移入rejected并写误导`.error`；
  reader I/O、CAS和SQLite故障也被同样误报为坏Query IR；
- 显式准入类型：新增`QueryAdmissionError(ValueError)`，只表示稳定文件、strict JSON和完整Query IR验证在任何
  QueryStore持久副作用前拒绝输入。`ingest_file()`一次完成load/validate，再调用`_ingest_validated()`；direct
  `ingest()`保持原`ValueError`兼容。最终symlink的ELOOP/EMLINK仍属输入拒绝，其他文件I/O异常保留原类型；
- 三阶段事务边界：spool只捕获`QueryAdmissionError`并进入rejected；reader、CAS/SQLite和accepted publication
  异常全部传播、保留incoming且不生成error sidecar。accepted move位于拒绝except之外；锁仍在finally或进程退出时
  释放。sidecar保持`ValueError:`前缀以兼容F363/F364现有消费者；
- 故障恢复：publication失败后QueryStore已有query/witness 1/1，重试得到`(1,0)`且仍为1/1；reader和SQLite故障后
  为0/0，重试同样收敛到1/1。真正invalid JSON仍为`(0,1)`、rejected/error存在且数据库0/0。旧宽泛异常反事实则
  稳定复现store=1但错误rejected；
- 验证：五结果生产driver PASS；QueryStore定向20 passed加2 subtests（2.44秒），四模块关联124 passed加2
  subtests（15.49秒）；787-node inventory字节一致；完整capability + identity gate为787 passed加127 subtests
  （99.55秒），16项能力、结果与身份退化均为零；F363/F364历史driver重新执行且JSON逐字节一致；
- 边界：SQLite主事务前可能留下不可变CAS orphan，本轮未实现GC；error sidecar/rejected move尚非持久原子事务，
  spool目录未逐组件锚定；服务传播基础设施异常后当前退出，未实现typed retry/backoff/health telemetry。未触发GitHub
  托管runner，也未运行LLVM lit、QSYM/PIN、真实MPI、solver/coverage campaign、公开benchmark或LAVA-M，不作
  性能、覆盖或漏洞提升结论，等级I/T/E-mechanism。完整报告、机制图、五结果JSON、日志和SHA-256证据见
  [`F365研究报告`](research-progress/Outcome_Separated_Query_Spool_Commit_F365_2026-08-11.md)。

## 361. F366：Verified Query Artifact CAS and Pre-Lease Admission（2026-08-11）

- 审查问题：QueryStore旧`_store_artifact()`只以`Path.exists()`判断摘要路径是否可复用，不验证regular类型、
  stable identity或实际SHA-256；预放错误SMT2后，数据库登记期望摘要而worker读取另一摘要的确定性反例成立。
  `claim()`又先提交`leased/attempts+1`再解析artifact path，读取失败会留下虚假在途租约；
- 统一CAS：复用F346-F350的`ContentAddressedInputStore`，新增默认关闭的`full_digest_leaf`兼容模式，使QueryStore
  保持`kind/H[0:2]/H.smt2`布局，同时获得逐组件no-follow目录能力、descriptor-relative临时对象、完整写入、
  fsync/replace、发布后身份复核、摘要验证和有界identity cache；
- repair/verify边界：ingest先`put(content, expected_digest)`，可安全替换损坏regular leaf、symlink与FIFO且不触碰
  symlink目标，随后才UPSERT规范`kind/relative_path/size`。公开artifact读取拒绝非规范digest、未知kind、数据库
  路径重定向以及内容/类型/身份不匹配；进程重开在cache为空时重新散列；
- 提交前lease准入：`BEGIN IMMEDIATE`选中查询后，使用同一SQLite snapshot依次验证full/prefix/target SMT2，
  三者全部成功才条件更新lease token、owner、TTL和attempts；任一验证失败回滚并保持`pending, attempts=0`；
- 证据：旧规则稳定得到期望`614fe277…`、实际`69b0c4d9…`且静默接纳；生产损坏拒绝、symlink/FIFO修复、
  path tamper拒绝、重开验证及8 writer/2 store收敛均PASS，最终1摘要、1数据库行、0临时残留；定向183 passed加
  20 subtests，相关253 passed加67 subtests，787-node清单字节一致，完整16能力门禁787 passed加127 subtests
  （115.18秒）且零退化；
- 边界：WorkLease仍传路径而非fd，尚未闭合solver open-time TOCTOU；CAS成功/SQLite失败可留下正确orphan，
  未实现GC；未做真实跨主机文件系统、solver/coverage campaign、公开benchmark或LAVA-M，不作性能或覆盖提升
  结论，等级I/T/E-mechanism。完整方案、机制图、反例/生产JSON、日志和SHA-256证据见
  [`F366研究报告`](research-progress/Verified_Query_Artifact_CAS_F366_2026-08-11.md)。

## 362. F367：Sealed Query Artifact FD Handoff（2026-08-11）

- 审查问题：F366虽在lease提交前验证full/prefix/target的内容身份，WorkLease仍只传pathname；claim返回后替换
  digest path，旧worker确定读取期望`614fe277…`之外的`69b0c4d9…`，solver可对错误公式返回结构合法结果；
- 不可变lease快照：claim在同一SQLite写事务中对三个CAS对象执行stable no-follow snapshot与SHA复核，再写入
  `memfd_create(MFD_ALLOW_SEALING)`对象；全写、fsync和回读后强制WRITE/GROW/SHRINK/SEAL四位，三者完成前不
  修改token/attempt。第二次分配故障关闭首个fd且保持`pending, attempts=0`；
- 所有权模型：WorkLease持线程安全bundle，portfolio只获取`dup`；complete/fail幂等关闭，析构兜底。一次性
  helper用`pass_fds`和`/proc/self/fd/N`继承full对象，不再打开CAS路径；
- 持久化协议：父子建立Unix `SOCK_SEQPACKET` channel，以`sendmsg(SCM_RIGHTS)`发送prefix/target并用query-id
  payload绑定文本请求。C++ helper要求恰好2个regular、128 MiB内、四seal完整的fd，使用bounded `pread`和
  前后身份复核，再以`solver.from_string`解析；cache hit仍消费并关闭本请求fd；
- 证据：三条CAS path替换后，三个快照摘要仍全匹配且写入返回EPERM；Python一次性/SCM receiver、真实C++
  one-shot/persistent SAT与两轮prefix-cache miss/hit均正确。定向20 passed加2 subtests，相关227 passed加20
  subtests，query/string lit 10 passed，787-node清单字节一致；完整16能力门禁787 passed加127 subtests
  （114.93秒）且零退化；
- 边界：机制依赖Linux memfd seals、`/proc/self/fd`和Unix descriptor passing；手工无bundle lease仍走兼容路径。
  未做CAS orphan GC、大对象/高并发资源测量、跨主机、公开benchmark、LAVA-M或覆盖campaign，不作性能/覆盖
  提升结论，等级I/T/E-mechanism。完整方案、架构图、反例/生产JSON、真实helper/lit日志和SHA-256证据见
  [`F367研究报告`](research-progress/Sealed_Query_Artifact_FD_Handoff_F367_2026-08-11.md)。

## 363. F368：Failure-Atomic Persistent Solver Generation Reset（2026-08-11）

- 审查问题：F367持久化solver在Unix socket先发送`SCM_RIGHTS` descriptor datagram，再向stdin
  写文本请求；若fd发送成功后write/flush失败，旧代码保留同一helper。下一请求会读取上一请求滞留的fd消息，
  可形成B文本绑定A prefix/target的跨请求错配；
- generation事务边界：把process group、stdin/stdout和descriptor socketpair作为不可拆分generation，新增
  `_process_valid`和`_invalidate_process()`。helper选定后的send/write/read/parse/ID-check任一不确定结果都会先
  单调标记invalid、分离并关闭socket，再TERM/KILL进程组并幂等关闭streams；下一请求只能冷启动新generation；
- 前置准入与有界reader：空字段及TAB/CR/LF在任何transport副作用前拒绝。stdout改用
  `selectors.DefaultSelector`和monotonic deadline，以最大64 KiB块读取，强制16 MiB总量、单newline frame、
  strict UTF-8、JSON object和response-ID一致；避免无界`readline`及`select.select`的FD_SETSIZE限制；
- 可执行证据：反事实稳定复现同一helper仅启动1次且A-fd/B-text ID错配、B实际消费A摘要；production在
  post-send故障后终止旧generation，第二次启动严格绑定B ID和B摘要。协议组还验证字段预检不损伤健康
  generation，以及ID错配、超长响应、部分响应超时各自终止并冷恢复；driver双跑JSON字节一致；
- 验证：QueryStore定向20 passed加2 subtests（3.33秒），相关227 passed加20 subtests（29.94秒），
  query/string lit 10 passed（3.98秒），787-node清单字节一致；完整16能力门禁787 passed加127 subtests
  （115.80秒），skip/xfail/xpass/deselection和身份漂移均为零；
- 边界：失败恢复会丢弃该generation的Z3 prefix cache，尚未测量高错误率冷启动成本；未做GitHub托管job、
  完整LLVM lit、vendored QSYM/PIN、真实MPI、跨主机、公开benchmark、LAVA-M或coverage campaign，不作
  性能/覆盖提升结论，等级I/T/E-mechanism。完整方案、事务图、反例JSON、测试日志和SHA-256证据见
  [`F368研究报告`](research-progress/Failure_Atomic_Persistent_Solver_Generation_Reset_F368_2026-08-11.md)。

## 364. F369：Protocol-Complete Persistent Solver Request Preflight（2026-08-11）

- 审查问题：F368只在transport前验证query ID、timeout、prefix key和witness，无sealed bundle时实际发送的
  prefix/target pathname未进入验证集合。newline pathname把一次调用拆为attacker与future-ID victim两行；通过
  延迟第二响应绕过同read多frame门后，真实victim稳定接受旧请求注入的同ID模型；
- commit-image预检：先固定pathname/sealed transport mode，构造最终六字段`request_field_preview`；新增纯
  `_validate_request_fields()`，对全部字段拒绝empty/NUL/TAB/CR/LF。sealed模式request ID额外要求strict ASCII
  且最多256 B，与native `recvmsg` payload envelope一致；
- 提交一致性：helper selection、SCM_RIGHTS和pipe write只发生在预检成功后；artifact准备的两个实际字段必须
  等于preview，文本提交直接复用同一tuple。纯拒绝不登记active query、不污染socket/pipe且保留健康generation
  和Z3 prefix cache；预检后模式不确定仍由F368整代回收；
- 可执行证据：仅mock新增纯预检时，旧路径复现attacker截断行、同一helper与victim接受injected key/assignment
  66；production拒绝newline/TAB/CR/NUL四类path和non-ASCII/257 B两类descriptor ID，合法victim观察自身
  `legitimate`字段/assignment 99，随后sealed请求仍绑定同ID并接收2个fd；双跑JSON字节一致；
- 验证：QueryStore定向20 passed加8 subtests（3.50秒），相关227 passed加26 subtests（30.38秒），query/string
  lit 10 passed（4.03秒），787-node inventory字节一致；完整16能力门禁787 passed加133 subtests（115.29秒），
  skip/xfail/xpass/deselection和身份漂移均为零；
- 边界：兼容pathname mode只获得frame grammar隔离，内容身份仍弱于sealed快照；256 B是当前native协议上限而非
  版本协商；未做GitHub托管job、完整LLVM lit、vendored QSYM/PIN、真实MPI、跨主机、公开benchmark、
  LAVA-M或coverage campaign，不作性能/覆盖提升结论，等级I/T/E-mechanism。完整报告、机制图、反事实JSON、
  日志和SHA-256证据见
  [`F369研究报告`](research-progress/Protocol_Complete_Persistent_Solver_Request_Preflight_F369_2026-08-11.md)。

## 365. F370：Deadline-Bounded Dual-Channel Request Commit（2026-08-11）

- 审查问题：F368只给stdout response reader建立monotonic deadline；sealed模式的阻塞
  `sendmsg(SCM_RIGHTS)`以及所有模式的`TextIOWrapper.write/flush`都发生在deadline创建之前。
  helper若停止消费descriptor socket或stdin，请求可在Z3读取timeout之前无限等待。Query IR允许
  128 MiB witness，`input_hex`文本可接近256 MiB，显著放大pipe背压窗口；
- 反事实：真实pipe读端由休眠helper持有，旧式1 MiB `write+flush`在100 ms观察窗内仍阻塞；真实Unix
  `SOCK_SEQPACKET`先写至内核返回`EAGAIN`，旧式blocking `sendmsg`同样保持阻塞。两者都要在关闭peer
  后才释放，证明response/Z3 timeout尚未获得执行机会；
- 有界commit image：最终六字段tuple在任何transport副作用前完成精确integer timeout准入、F369字段
  grammar、strict UTF-8编码和总frame上限检查。上限为`2 * 128 MiB + 64 KiB = 268500992 B`，覆盖既有
  最大Query IR witness同时限制手工WorkLease；纯拒绝保持健康generation和prefix cache；
- 单deadline双通道：请求建立唯一absolute `monotonic + timeout + 5 s`时刻。descriptor以
  `MSG_DONTWAIT`发送，stdin以nonblocking `os.write`和显式offset提交，`EAGAIN`统一经selector等待剩余
  时间；每次syscall前与完整提交后均复核绝对时刻，持续小进展也不能绕过边界。response reader消费相同
  deadline，`EINTR/EAGAIN`不刷新预算，三个阶段不再各自获得完整timeout；
- 故障原子恢复：descriptor成功后文本失败、文本部分写、response timeout/framing/ID错误都属于不确定
  generation，沿用F368关闭socket、TERM/KILL进程组和stdio，下一请求只能冷启动，残留FD或半行不能跨请求；
- 证据：driver在真实pipe/socket/SCM_RIGHTS、sealed lease和休眠helper上验证两条legacy阻塞、两条
  production deadline、两次generation retirement/cold recovery及四类纯预检，连续两次JSON字节一致，
  SHA-256为`616c65bd72f1b08c7fd9b49ddced0a7b7fdfd43aa92d1c7c701e7497c2bfd2fd`；定向
  20 passed加12 subtests，相关227 passed加30 subtests，query/string lit 10/10，规范787-node字节不变，
  完整16能力门禁787 passed加137 subtests（115.36秒），零skip/xfail/xpass/deselection与身份漂移；
- 边界：本轮未测默认5秒grace的tail latency、大witness吞吐/RSS或prefix-cache冷启动成本；witness仍走
  文本pipe，并未扩展为第三个sealed fd。未执行GitHub托管runner、完整LLVM lit、vendored QSYM/PIN、
  真实MPI、跨主机、公开benchmark、LAVA-M或coverage campaign，不作性能、覆盖或漏洞提升结论，等级
  I/T/E-mechanism。完整方案、示意图、反事实/production JSON、测试日志与SHA-256 evidence见
  [`F370研究报告`](research-progress/Deadline_Bounded_Dual_Channel_Request_Commit_F370_2026-08-11.md)。

## 366. F371：Generation-Unique Logical Request Admission（2026-08-11）

- 审查问题：F368-F370只要求response JSON中的`request_id`等于当前逻辑query ID。QueryStore在lease重试时
  合法复用内容寻址ID；helper若在第一次成功响应后迟到输出同ID重复JSON，第二次调用仍会通过framing、JSON和
  ID检查。真实反事实中第一次`repeat`返回assignment 11，旧generation随后flush同ID assignment 22，第二次
  同ID调用稳定接收22；
- 兼容性约束：native字段0同时用于`writeResult`回显、`ArtifactDescriptors.receive`的SCM_RIGHTS payload绑定，
  并传入`checkSolver`影响稳定随机种子、generator metadata和选择性求解。F371不把它替换成随机correlation token，
  也不扩展未版本化第七字段；
- generation内唯一准入：F369/F370纯预检与frame编码完成后、任何descriptor/pipe副作用前，对逻辑ID计算
  SHA-256指纹；同一指纹在一个helper generation只准入一次。重复时先沿F368关闭socket/stdio、TERM/KILL旧
  process group，再冷启动、清空历史并在新代提交；准入前后各检查portfolio cancel，覆盖换代窗口；
- 有界状态：历史只保存固定32 B digest，最多65,536项；达到容量时下一不同ID也保守换代。digest净载荷上限
  2 MiB，容器开销实现相关。碰撞只造成额外冷启动，不会接受旧response；纯预检拒绝不登记，不同ID默认继续
  复用同代Z3 prefix/partial-solution cache；
- 可执行证据：只mock新增准入方法的counterfactual保持真实process、pipe及F368-F370 reader，得到单代
  `11 -> 22`；production得到两代`11 -> 11`。另验证newline预检后同ID合法调用不换代、不同ID复用原PID以及
  容量1换代。driver连续两遍JSON字节一致，SHA-256为
  `2bdd7cb0b3975b2e1bc51358255c9797bce3c156d826bb505d0d49ec934d4cb8`；
- 验证：QueryStore定向20 passed加12 subtests（4.96秒），相关227 passed加30 subtests（31.69秒），
  query/string lit 10/10（5.28秒），F368/F369/F370历史JSON逐字节一致，规范787-node digest不变；完整16能力
  门禁787 passed加137 subtests（115.91秒），skip/xfail/xpass/deselection、能力与身份退化均为零；
- 边界：该性质是per-helper-generation at-most-once admission，不是跨worker/global exactly-once。重复与容量
  换代会丢失旧代prefix cache，尚未测量重试频率、冷启动成本、cache hit、吞吐、RSS或coverage。未执行
  GitHub托管job、完整LLVM lit、vendored QSYM/PIN、真实MPI、跨主机、公开benchmark、LAVA-M或fuzzing
  campaign，不作性能、覆盖或漏洞提升结论，等级I/T/E-mechanism。完整报告、示意图、反事实/生产JSON、
  测试日志和SHA-256 evidence见
  [`F371研究报告`](research-progress/Generation_Unique_Logical_Request_Admission_F371_2026-08-11.md)。

## 367. F372：Bounded Content-Equivalent CAS Publication Revalidation（2026-08-11）

- 审查问题：F371全局交付复核重放F366真实8-writer driver时，`ContentAddressedInputStore.put()`偶发抛出
  `ESTALE input object changed during publication`。单独复跑确认：own descriptor与public inode因同摘要writer
  竞争而分裂后，旧fallback只做一次stable snapshot；第三个精确content replace落入snapshot窗口会把合法收敛
  误判为失败；
- 三值重验证：新增`_verified_object_after_publication()`，每次从descriptor-anchored shard no-follow stat
  canonical leaf并做`max_object_bytes`有界stable snapshot。精确digest返回当前identity；稳定wrong digest立即失败；
  OSError/ValueError代表未形成稳定观察，预算内重试；
- 有界与隔离：`_INPUT_STORE_PUBLICATION_VERIFY_ATTEMPTS = 32`封顶持续漂移，耗尽后仍由`put()`抛ESTALE；
  non-regular立即拒绝。原root/shard身份退出复核、atomic replace、fsync、temporary finally cleanup和verified-cache
  eviction不变，不以重试掩盖目录替换或稳定损坏；
- 证据：只替换新方法的一次性反事实在首次模拟ESTALE后错误拒绝精确competitor；production前三次漂移后第4次
  接纳。稳定wrong只hash一次即拒绝，持续漂移严格32次拒绝，真实8-writer全部返回相同identity、最终内容精确且
  temporary 0。driver双跑JSON字节一致，SHA-256为
  `c249b6ac505516c1634a07204dba0b182981e535d9b20ecefb349d340bc8859e`；既有F366完整driver另跑20轮，
  20/20与历史JSON字节一致；
- 验证：现有CAS race identity 1 passed（0.21秒），distributed state + QueryStore关联183 passed加30 subtests
  （24.92秒），规范787-node digest不变；完整16能力门禁787 passed加137 subtests（121.54秒），
  skip/xfail/xpass/deselection、能力与身份退化均为零；
- 边界：最多32次可能放大竞争路径读I/O，但无竞争fast path不进入循环；尚未测竞争频率、延迟、吞吐或RSS。
  未做断电、跨主机/NFS资格验证、真实MPI、公开benchmark、LAVA-M或coverage campaign，不作性能/覆盖提升结论，
  等级I/T/E-mechanism。完整报告、示意图、反事实/production/stress证据和SHA-256清单见
  [`F372研究报告`](research-progress/Bounded_Content_Equivalent_CAS_Publication_Revalidation_F372_2026-08-11.md)。

## 368. F373：Report-Only Query Artifact Reachability Audit（2026-08-11）

- 审查问题：Query artifact CAS发布发生在主Query事务之前；进程若在CAS成功后、artifact row或query commit前失败，
  可能留下数据库可见但无query root的indexed orphan，或只有物理对象、没有row的unindexed orphan。旧SQL-only
  视图只能看到前者，无法证明物理namespace闭包，也不能为后续保守回收提供可信mark；
- 协作式snapshot边界：新增本机有界exclusive `.artifact-audit.lock`，公共ingest从三个CAS publication一直覆盖到
  artifact row、query transaction和post-commit materialization；audit复用同一锁并在SQLite显式read transaction中
  提取full/prefix/target digest root。锁对象以既有no-follow regular-file契约打开，symlink/FIFO在任何Query副作用前
  失败关闭；该协议只约束协作调用者，不等于跨主机或敌对进程隔离；
- descriptor-anchored物理inventory：`ContentAddressedInputStore.scan_objects()`从已锚定root/shard descriptor枚举，
  只接受规范两位小写hex shard与精确digest leaf，leaf必须no-follow regular；root entry与leaf共享一个全局条目预算，
  每层使用后复核身份。输出记录完整性、已扫描条目、非规范entry及有界对象metadata，不散列对象内容；
- typed relational join：audit独立关联query roots、artifact row和三个物理namespace，区分reachable primary/duplicate、
  unreferenced artifact row、unindexed/orphan physical、dangling reference、invalid row、missing primary与noncanonical entry。
  完整扫描才允许物理缺失判断；预算耗尽时`scan.complete=false`、missing类字段为`null`、CLI exit 2，避免把未知编码成空集；
- 固定report-only边界：报告schema固定`mode=report-only`、`safe_to_sweep=false`、
  `content_digests_verified=false`、`verdict.sweep_authorized=false`，实现没有unlink、row delete、candidate登记或GC入口。
  下一阶段仍须实现两次完整mark、grace period、identity-bound candidate、重新验证和有预算的失败原子durable sweep；
- 可执行证据与验证：反事实数据库视图仅发现1个indexed orphan且看不到1个unindexed physical orphan；production
  同时分类二者及1个noncanonical entry并保留全部对象。`max_entries=1`给出partial/null，真实CLI完整/部分分别exit 0/2；
  publication-fence测试证明audit扫描期间publisher保持blocked，释放后再完成。driver双跑JSON字节一致，SHA-256为
  `43b0efaf58398fdea5e2a12544fb589c8349ac46a148e1caf41e837705955f09`；定向1 passed加4 subtests
  （0.86秒），QueryStore 20 passed加16 subtests（5.35秒），关联183 passed加34 subtests（20.14秒），规范
  787-node摘要不变；完整16能力门禁787 passed加141 subtests（116.43秒），零skip/xfail/xpass/deselection与身份漂移；
- 边界：未散列物理对象内容，内容正确性继续由F366/F367消费门负责；条目预算不是wall-time预算，锁会串行化本机
  协作ingest且尚未测量等待/吞吐/RSS。未做GitHub托管job、完整LLVM lit、vendored QSYM/PIN、真实MPI、跨主机、
  公开benchmark、LAVA-M或coverage campaign，不作性能、覆盖、漏洞发现或磁盘回收结论，等级I/T/E-mechanism。
  完整方案、关系图、反事实/生产/CLI/并发JSON、测试日志和SHA-256证据见
  [`F373研究报告`](research-progress/Report_Only_Query_Artifact_Reachability_Audit_F373_2026-08-11.md)。

## 369. F374：Agolic Run-Level Bounded-Execution Planning（2026-08-12）

- 技术问题：单 seed/trace 的 agentic hook 不能规划一次有界符号执行饱和后的下一次独立运行，也不能对
  多 worker 结果做无重复覆盖归因；
- 实现：`util/agolic_planning.py` 将目标、模式、profile、witness、符号输入面与时间/内存预算组成规范
  `RunPlan`，经 deterministic admission 后允许 worker 并行执行，但按完成顺序串行原生 replay，并以
  `new-reach`、`increased-target-coverage`、`reached-no-gain`、`not-reached`/`unverified` 记录反馈；
- 持久化与审查：实验/程序身份绑定，stable no-follow state snapshot、原子 durable replace、精确
  fingerprint 去重、history 容量失败关闭和 replay identity 内容承诺；两轮审查修复历史截断重派发、
  并行覆盖重复归因、未验证 reach、恢复字段不完整和非整数预算等问题；
- 证据与边界：定向 10 passed，提交时完整 Python 816 passed + 178 subtests。该实现是运行级协议和
  verifier 闭环，不等价于 KLEE 的逐指令 witness release，也没有本项目公开 benchmark 提升结论。
  详见 [`F374研究报告`](research-progress/Agolic_Run_Level_Planning_F374_2026-08-12.md)。

## 370. F375：Checkpoint-Resumable Cleanup-Only Exception Unwind（2026-08-12）

- 技术问题：旧 continuation 只能把可证明 `nounwind` 的 invoke 退化为普通调用，无法保存并迁移正在
  cleanup/unwind 的异常状态；
- 实现：LLVM lowering 为受控整数异常源生成 `throw_if/throw`，为内部 may-unwind call 保存 normal/unwind
  continuation；executor 将 active、payload、handler depth 与 frame/solver/memory/program root 一同写入
  CAS，逐帧清除局部状态并找到最近 handler，根部未处理异常显式返回；
- 可信边界：`bounded-cleanup-exception-unwind` capability 双向闭包，summary 声明和调用类型精确匹配，
  外部 may-unwind、catch/filter、Windows funclet、嵌套活动异常及 ABI exception object 继续失败关闭；
- 证据：Python、LLVM lit、手工 artifact/checker 与 checkpoint 恢复均通过；机制不提供覆盖或吞吐提升
  结论。详见 [`F375研究报告`](research-progress/Cleanup_Only_Exception_Unwind_F375_2026-08-12.md)。

## 371. F376：Collision-Checked Exact Typed Exception Matching（2026-08-12）

- 技术问题：cleanup-only 异常没有 type selector，特定 catch 无法判定接管权，错配异常可能执行错误
  handler；
- 实现：规范 typeinfo global 经稳定 module identity 映射为正 i32 selector，module 内反向表检测碰撞；
  typed throw、landingpad `exception_match` 与 `llvm.eh.typeid.for` 共享该映射。匹配 gate 位于 handler
  指令之前，错配直接继续展开；type 与 active/payload/depth 一同进入 CAS 并执行组合不变量验证；
- 可信边界：只支持精确 typeinfo identity、catch-all 与 cleanup，不推测 C++ 继承、adjusted pointer、
  exception object 生命周期、filter 或 personality ABI；
- 证据：定向 executor/store、LLVM lowering/signature lit 和伪造 checkpoint 反例均通过；不报告性能
  提升。详见 [`F376研究报告`](research-progress/Exact_Typed_Exception_Matching_F376_2026-08-12.md)。

## 372. F377：Capability-Closed Declarative Pure External Models（2026-08-12）

- 技术问题：硬编码函数名 summary 难扩展，也不能证明宿主外部调用可迁移、确定和无副作用；
- 实现：外部 declaration 用 `symcc-continuation-model="pure-v1:..."` 声明 constant、identity、neg、
  bitnot、有界二元运算/比较和 select。compiler 要求固定 1–64 bit 整数 ABI、定义实参、唯一稳定 site、
  `memory(none) nounwind willreturn speculatable nofree nosync`，并发出保留 provenance 的
  `external_pure`；executor 独立重解析、验证 capability/schema/ABI/site，再构造 QF_BV DAG而不调用宿主函数；
- 多轮审查：统一两端 ASCII 名称域、严格拒绝 bool/float 数字别名、检查重复 site 和 terminal normal edge，
  将 checkpoint 测试改为独立 Python 进程从 CAS 恢复，闭合空字段 parser 分歧与多义 operand JSON，
  并排除 div/rem/shift 等非 total 操作；
- 证据与边界：七个模型族、符号分支、nounwind invoke、fresh-process-style CAS 恢复和失败关闭反例均有
  Python/LLVM 测试。LLVM 属性只约束 effect envelope，人工公式仍是受信模型，最终语义证据来自原程序
  native replay；不报告 benchmark、coverage 或吞吐提升。详见
  [`F377研究报告`](research-progress/Declarative_Pure_External_Models_F377_2026-08-12.md)。

## 373. F378：Scalar Exception Token and Catch Lifecycle（2026-08-12）

- **问题**：F375/F376 能传播 cleanup/typed exception，但没有 exception structure token，且将
  unwinding 与 caught 混成同一 active 状态；真实 Clang catch 的 begin/end/rethrow 次序无法恢复或审计。
- **方案**：为精确签名、C calling convention 的 `__cxa_begin_catch`、`__cxa_end_catch`、
  `__cxa_rethrow` 建立失败关闭 lowering；landingpad index 0 只允许作为当前 begin 的单消费者 token，
  begin 返回对象 pointer 必须无使用。continuation IR 新增 `exception_token`、
  `exception_begin_catch`、`exception_end_catch` 与 `exception_rethrow`。
- **状态机**：checkpoint 显式记录 phase 1 unwinding、phase 2 caught、phase 3 rethrowing，正常 end 删除
  payload/type/token，重抛 cleanup 的 end 保留同一异常并回到 phase 1；phase、token、catch depth 与 program
  capability 双向校验，未配对 begin/end、提前 return/halt 和伪造 token 全部拒绝。
- **集成**：`LiveProgramGraph` 增加 throw normal/unwind、rethrow unwind、end normal 控制边；LLVM 集成
  覆盖正常 catch、内层重抛到外层、Clang 风格 end invoke，以及对象 pointer 物化的拒绝。
- **证据**：LLVM 17/18 构建通过；完整 lowering lit 1/1（236 excluded，179.62 s）；定向 26 passed +
  22 subtests；完整能力门 862 passed + 224 subtests（127.31 s）；实现、图、报告和 artifact 见
  [`F378研究报告`](research-progress/Scalar_Exception_Token_and_Catch_Lifecycle_F378_2026-08-12.md) 与
  [`F378证据`](evidence/f378-exception-catch-lifecycle-2026-08-12/)。
- **边界**：这是单活动、无对象物化/析构的 Itanium-style 标量子集；不支持 catch 参数 load、继承调整、
  handler-count stack、nested exception、Windows funclet 或完整 personality ABI；无 benchmark 提升结论。

## 374. F379：Bounded Trivial Scalar Catch Object（2026-08-12）

- **问题**：F378 要求 `__cxa_begin_catch` 返回 pointer 无使用，因而仍拒绝最小的平凡整数 catch 参数读取；
- **方案**：只允许 begin 返回值上的直接、非 atomic、非 volatile、1--64 bit 整数 load，且 begin 的非空
  use set 必须全部满足该条件；lowering 发出 `exception_value(dst,bits)`，不物化对象地址或平台 header；
- **运行时**：能力闭包要求 cleanup、typed matching、catch lifecycle 和 scalar object 四项同时成立；读取时
  要求 phase=2、当前 frame 等于 catch-depth、payload expression 位宽与 load 完全一致，然后复用 symbolic
  digest 绑定局部 SSA。begin 前、end 后、错误 frame 或位宽转换均拒绝；
- **审查**：修复测试 fixture 的 typed capability 遗漏，并把该依赖固化到生产 artifact validator；新增
  GEP、store、atomic、ptrtoint 负例以及 fresh-executor checkpoint 恢复；
- **证据**：LLVM 17/18 重建通过；完整 lit 237 passed、1 unsupported、0 failed；定向 Python 30 passed +
  27 subtests；完整能力门 868 passed + 229 subtests。报告、图和 artifact 见
  [`F379研究报告`](research-progress/Trivial_Scalar_Catch_Object_F379_2026-08-12.md) 与
  [`F379证据`](evidence/f379-trivial-scalar-catch-object-2026-08-12/)；
- **边界**：不是通用异常对象/arena，不支持字段布局、写入、析构、继承 adjusted pointer 或完整 C++ ABI；
  没有 benchmark、coverage 或吞吐提升结论。

## 375. F380：Generation- and Ownership-Certified Exception Object Arena（2026-08-12）

- **问题**：F379 仅投影 scalar payload，没有 `__cxa_allocate_exception` 产生的对象身份、symbolic memory、
  跨 frame 所有权或销毁语义；地址槽复用还会造成旧 checkpoint 与新对象的 ABA 混淆；
- **方案**：精确准入固定非零大小的 `__cxa_allocate_exception`、对象直接 stores 和
  `__cxa_throw(object,typeinfo,null)`；每个 site 预布局有界 `bounded-exception-arena` 槽位，并发出
  `exception_alloc`、`exception_throw`、`exception_object_load`；
- **运行时**：每槽维护 monotonic generation、frame owner、live、logical size 和逐 byte initialization；
  throw 验证 base/site/type/owner/generation/lifetime 后把 owner 转移给 active exception，catch load 重验
  generation/bounds/init，normal end 清 live/size/init 但保留 generation。未抛出对象在 owner frame
  return/unwind 时自动释放；
- **审查**：收紧 throw 的 noreturn/unreachable CFG；隔离普通 heap alloc/free/realloc；把证书从 concrete-value
  比较加强为规定宽度常量与 digest 组合验证；加入本 frame、跨 frame、fresh executor、同槽代次递增、
  owner/generation 篡改、未初始化、越界、动态 size、非空析构、interior pointer 和 scalar/object producer
  混用反例；混用在 lowering 前失败关闭，避免 catch payload 被全局错误解释；
- **证据**：LLVM 17/18 重建通过；完整 lit 238 passed、1 unsupported；定向异常组合 32 passed + 23
  subtests，其中 arena 8 passed；完整能力门 877 passed + 229 subtests；normal/catch 分别得到 7/42，跨 frame 得到
  7/84。报告、图和 artifact 见
  [`F380研究报告`](research-progress/Bounded_Exception_Object_Arena_F380_2026-08-12.md) 与
  [`F380证据`](evidence/f380-bounded-exception-object-arena-2026-08-12/)；
- **边界**：只支持固定大小、空析构器、direct offset-0 标量读取和单 active exception；不模拟 runtime
  header、非平凡析构、nested caught stack、RTTI 继承 adjusted pointer 或完整 native ABI；无性能或覆盖
  提升结论。

## 376. F381：Pre-I/O Persistent Portfolio Cancellation Registration（2026-08-12）

- **问题**：persistent solver 只有取得 `_io_lock` 后才发布 `_active_query`；future 已运行但尚未发布 active
  时，`Future.cancel()` 与 solver `cancel()` 可同时失败，SAT winner 的取消请求因此丢失；高并发完整门禁复现
  `cancelled_attempts=0` 和未关闭 pipe 告警；
- **方案**：每次 callable 在竞争 I/O 锁前，以 `_state_lock` 将唯一 cancellation event 登记到
  `running[query_id]`；`cancel()` 设置同 query 的所有 event，且仅在 active event 属于该集合时中断当前
  process group；outer `finally` 总注销，`close()` 覆盖 queued/active registrations；
- **测试**：受控持有 `_io_lock`，使 persistent future 确定地阻塞在 I/O admission 前；fast SAT 后要求取消计数
  为 1、attempt 标记 cancelled，随后 cold helper 恢复。反事实 20/20 重复通过；QF_BV + QueryStore 为
  38 passed + 22 subtests，warnings-as-errors、Ruff、py_compile 与 whitespace gate 通过；
- **完整门禁**：LLVM lit 238 passed、1 unsupported、0 failed；Python capability gate 877 passed + 229
  subtests，零 skip/xfail/xpass/deselection/node-ID 漂移；
- **证据**：方案、时序图、原始门禁与哈希见
  [`F381研究报告`](research-progress/Pre_IO_Persistent_Portfolio_Cancellation_F381_2026-08-12.md) 和
  [`F381证据`](evidence/f381-pre-io-persistent-cancellation-2026-08-12/)；
- **边界**：证明本地 thread portfolio 的取消可达性与资源生命周期，不提供实时上界、分布式取消、solver
  内部抢占或吞吐/coverage/campaign 提升结论；保守 cold reset 可能增加一次 helper 重启。

## 377. F382：DataLayout-Precise Constant-GEP Exception Object Fields（2026-08-12）

- **问题**：F380 仅支持异常对象基址上的直接整数 store/load；真实 Clang 对结构体与数组异常使用
  constant GEP，且旧源码驱动把 exporter 注册在 `-O1` pipeline 起点，名义优化级别与 exporter 真正看到的
  IR 不一致；
- **编译语义**：throw 侧遍历由 `__cxa_allocate_exception` 基址派生的有限 pointer-use DAG，只接受
  DataLayout 可折叠的非负常量偏移、对象内 1--64 bit 整数 store；`__cxa_throw` 仍只允许分配基址。
  catch 侧接受 `__cxa_begin_catch` 后任意层 constant GEP 与多次整数 load，并将其降为带 exact offset 的
  `exception_object_load`，不把宿主 pointer 写入 checkpoint；
- **源码管线**：`llvm_to_continuation.py` 改为 Clang `-O0 -disable-O0-optnone -emit-llvm` 生成 bitcode，
  再由 `opt` 显式执行 `sroa,mem2reg,instcombine` 与 exporter。该 profile 规范化 frontend temporary，且不
  运行会把待探索 branch if-convert 为 select 的完整 `-O1`；返回值分别记录 compile/export command；
- **运行时与合同**：新增 `bounded-exception-object-fields`，要求 arena capability 与至少一个非零 offset
  load；非零 offset 没有 capability 时拒绝。执行时复核 active catch、generation、object size、单槽边界
  和逐字节 initialized condition，再按目标端序合成值；
- **审查与证据**：手写四槽 LLVM aggregate 形成 `base+0/+4/+8` guard-correlated stores 和三个 field
  loads，正常/异常返回 7/47；动态 catch GEP、动态 store、OOB store、interior throw 失败关闭。真实 C++
  `throw Payload{5,{19,23}}` 在 LLVM 17/18 分别 compile/canonicalize/lower/execute 为 7/47，旧 C source
  仍保持两条结果 0/66；异常组合回归 47 passed + 51 subtests；完整 LLVM lit 为 239 passed、1
  unsupported，完整 Python capability gate 为 878 passed + 229 subtests，node-ID 与 canonical inventory
  精确一致且零退化；LLVM 17/18 构建和静态门禁通过；
- **边界**：仅为固定对象、平凡析构、整数 aggregate field 子集，不支持 float/pointer payload、symbolic
  index、union active member、非平凡析构、nested caught stack、RTTI adjusted pointer 或完整 C++ ABI；
  不作 benchmark、coverage 或吞吐提升结论。详见
  [`F382研究报告`](research-progress/Constant_GEP_Exception_Object_Fields_F382_2026-08-12.md) 与
  [`F382证据`](evidence/f382-constant-gep-exception-fields-2026-08-12/)。

## 378. F383：Proof-Carrying Multi-Cell Alias Graph（2026-08-12）

- **问题**：F142--F148 的 LLVM 端能证明 merge block 中多个 MemorySSA definedness cell 独立，但旧
  artifact 只保留 `multicell=true` 与证明类别；恢复端无法从真实 load 地址域独立复核，篡改 alias 元数据后
  标签仍可能表面自洽；
- **方案**：编译器为可独立复核的 pair 输出对称 `alias_graph_neighbors`，并声明
  `bounded-multicell-alias-graph`；symbolic GEP 同时输出与地址逐项对应的 `alias_index_values`，避免把相同
  地址集合误判为同一 index 下的可行重叠；
- **运行时准入**：按 load 宽度、memory-object bounds、constant/alias/alias-case 地址、canonical BV guard
  与精确 index 值重建最多 256 个 cell；逐边枚举笛卡尔积，只有区间不重叠或联合 guard 存在同 operand
  不同等值约束时才接纳。capability 无合同、非对称/跨 block/自环边、漂移的 index 摘要和 guard 相容重叠
  全部在执行前失败关闭；
- **审查修复**：第一次整文件 LLVM 回归暴露地址集合 oracle 的保守误报：两个 load 都枚举
  `[64,65,66,67]`，但对应 index 分别为 `[0,1,2,3]` 与 `[-2,-1,0,1]`。逐地址见证修复后正例和篡改反例
  均通过；PHI-correlated domain 因缺少 edge-block 联合 tag 证书而明确不生成 F383 图边；
- **证据**：fixed heap、select-guard 与 symbolic-index artifact 均有一条可复核无向边，LLVM 17/18 的
  symbolic artifact 一致；PHI 边界 artifact 不声明新 capability。定向 Python 5 passed；完整 LLVM lit
  240 passed、1 unsupported；完整 capability-closed Python gate 883 passed + 229 subtests，node-ID 无漂移；
  LLVM 17/18、Ruff、py_compile 与 whitespace gate 通过。详见
  [`F383研究报告`](research-progress/Proof_Carrying_Multicell_Alias_Graph_F383_2026-08-12.md) 与
  [`F383证据`](evidence/f383-multicell-alias-graph-2026-08-12/)；
- **边界**：这是有界有限域上的 pairwise proof-carrying alias 证书，不是一般 heap graph、完整 points-to
  analysis 或通用 guard SMT；不验证未声明边，也不提供 coverage、吞吐或公开 benchmark 提升结论。

## 379. F384：Shared PHI Edge Discriminator（2026-08-13）

- **问题**：F383 无法仅凭两个 load-local pointer tag 复核 PHI correlation；重叠地址可能分别受
  `v1_tag=1` 与 `v2_tag=0` 约束，看似相容，但 LLVM CFG 实际只允许两个 PHI 同时选择同一 predecessor；
- **方案**：同一 PHI block 的数据/函数 pointer PHI 共用一个 32-bit edge discriminator，每条 synthetic
  incoming edge 只写一次 predecessor `blockId`。alternative guard 按 predecessor identity 编码，不依赖不同
  PHI 可能不一致的 incoming 文本顺序；function-level `phi_edge_discriminators` 保存 block/tag/edge/value 合同；
- **准入闭包**：runtime 要求 capability 与合同双向存在，edge/value/predecessor 唯一，每条 edge 恰有一个匹配
  const 并 jump 到目标 PHI block，tag 无合同外定义，且全部 incoming jump edge 与合同集合精确相等；新式
  PHI-correlated load 的每个有限 cell 必须由已注册共享 tag guard 约束，再复用 F383 图 oracle 拒绝可行重叠；
- **多轮审查**：大型回归发现函数指针 PHI 依赖 `bounded-indirect-call-dispatch` 而非必然依赖
  `bounded-pointer-union`，已改为二者择一；新增 reversed incoming-order LLVM fixture，证明两个 PHI 即使列表
  顺序相反，地址 65 仍由 `tag=right` / `tag=left` 正确判为不可同时到达；遗漏 edge、赋值漂移、越权定义和
  未注册 guard 均失败关闭；
- **证据**：ordered/reordered LLVM 18、reordered LLVM 17 和 function-pointer 四份 artifact 通过独立
  checker；定向 Python 5 passed；完整 LLVM lit 241 passed、1 unsupported；完整 capability-closed Python
  gate 888 passed + 229 subtests，node-ID 无漂移；LLVM 17/18、Ruff、py_compile、whitespace 与文档门通过。
  详见 [`F384研究报告`](research-progress/Shared_PHI_Edge_Discriminator_F384_2026-08-13.md) 与
  [`F384证据`](evidence/f384-shared-phi-edge-discriminator-2026-08-13/)；
- **边界**：只证明同函数、同 PHI block 的有界 predecessor 联合关系，不是一般 path-relation、heap graph、
  points-to 或通用 SMT guard 推理；无 coverage、吞吐、漏洞数或公开 benchmark 提升结论。

## 380. F385：Proof-Carrying Byte-Lane Writer Graph（2026-08-13）

- **问题**：原 byte-lane contract 校验 lane 数、store ID/宽度与 definedness 合成，却不从实际地址和 CFG 顺序
  重建 last writer；在界内修改 `store_byte`，或让被新写覆盖的旧 store 冒充最终 source，仍可能结构自洽；
- **方案**：普通 2--8 byte composition 声明 `bounded-byte-lane-writer-graph` 与 `writer_graph=true`。恢复端从
  load 位置反向扫描最多 64 blocks，当前 block 逆序遇到的第一个覆盖 store 必须与每条 lane edge 的函数内 ID、
  byte offset 和 width 完全一致；未完成时只允许唯一 predecessor，到入口的剩余 lane 必须为 initial；
- **Poison 绑定**：graph 引用的 store 输出 `byte_lane_poison_source`；紧邻的 1-bit identity sidecar 必须从该变量
  取值，且 lane/store 的 defined 名称一致。它闭合 transfer 边，但不宣称 runtime 已从 LLVM IR 独立重建所有
  overflow/shift/div poison predicate；
- **审查修复**：初版因 capability 为程序级而误把 poison-source 要求施加到另一函数的 PHI-only store；最终按
  graph contract 引用的函数内 store ID 校验。新增三函数 reachable fixture，使普通 writer graph 与 byte-lane
  PHI 在同一 artifact 中并存；`store_byte`/地址漂移、shadowed writer、poison 改绑、capability/contract 漂移均
  失败关闭；
- **证据**：LLVM 18/17 overlap artifact 都得到 `lane0→wide byte0`、`lane1→narrow byte0`；initial artifact
  得到 store+initial 两边；cross-function artifact 含 3 functions、1 graph、1 PHI contract。定向 Python 5
  passed；完整 Python 893 passed + 229 subtests；受控 32-worker lit 243 discovered、242 passed、1
  unsupported；LLVM 17/18 和静态门通过。首次 192-worker 执行中一个既有 poly exact-projection 测试发生一次
  solver unknown，随后独立 10/10 与完整 32-worker 均通过，断言未放宽；
- **边界**：仅为非 atomic/volatile、静态可定址、单前驱有界路径上的普通 composition，不是一般 MemorySSA、
  分支/循环 writer DAG、任意 symbolic pointer 或线程间内存模型；无 coverage、吞吐、漏洞数或 benchmark
  提升结论。详见 [`F385研究报告`](research-progress/Proof_Carrying_Byte_Lane_Writer_Graph_F385_2026-08-13.md)
  与 [`F385证据`](evidence/f385-byte-lane-writer-graph-2026-08-13/)。

## 381. F386：Proof-Carrying Byte-Lane PHI Writer Graph（2026-08-13）

- **问题**：普通 byte-lane PHI 合同只校验 store ID 存在、宽度及 endpoint→merge 跳转，不能证明
  每个 endpoint 上的 store 是对应 lane 的最后写者；跨分支 store 替换、shadowed old writer 或 poison
  source 改绑仍可能形成结构合法制品；
- **方案**：新增 `bounded-byte-lane-phi-writer-graph` 与 `writer_graph=true` 双向合同。runtime 从每个
  endpoint terminal jump 前反向扫描最多 64 blocks，只沿唯一 predecessor；第一个覆盖未解析 lane 的 store
  必须精确匹配函数局部 ID、地址导出的 byte offset 和 width，无前驱时只准 remaining lane 为 initial；
- **Poison 闭包**：仅为 F385/F386 图实际引用的 store 输出 `byte_lane_poison_source`，identity sidecar 必须
  精确读取该变量。第二轮审查进一步拒绝“借 program-level capability 给仅由 legacy/cyclic 合同引用的 store
  携带未经图复核的 poison source”；
- **反例与兼容**：checker 新增 capability、marker、writer substitution/address drift 和 poison drift 四类
  tamper；initial endpoint 无同址替代 store 时使用确定性地址漂移后备反例；三函数 artifact 同时保留 F385
  non-PHI graph 与 F386 PHI graph，证明 function-local store namespace 不串扰；
- **证据**：LLVM18/17 普通 PHI 均为 1 graph、2 endpoints、4 poison-bound stores；initial artifact 为
  2 store lanes + 2 initial lanes；定向 5 passed，全部 live continuation 为 69 passed + 51 subtests；完整
  Python gate 898 passed + 229 subtests，完整受控 lit 244 discovered、243 passed、1 unsupported；LLVM17/18、
  Ruff、py_compile、whitespace 与交付门通过；
- **边界**：只证明普通、非循环、endpoint 内唯一前驱的有限 writer path，不是通用 MemorySSA、任意 join
  writer DAG、loop-carried writer graph 或 LLVM poison predicate 的独立重建；无 coverage、吞吐、漏洞数或
  benchmark 提升结论。详见
  [`F386研究报告`](research-progress/Proof_Carrying_Byte_Lane_PHI_Writer_Graph_F386_2026-08-13.md) 与
  [`F386证据`](evidence/f386-byte-lane-phi-writer-graph-2026-08-13/)。

## 382. F387：Proof-Carrying Cyclic Byte-Lane Writer Graph（2026-08-13）

- **问题**：基础 cyclic byte-lane PHI 已表达 store/carry/initial lane 及边上布尔状态传递，但不能证明
  合同声明的 store 是从 endpoint 逆向到本轮 merge load 期间第一个覆盖该 lane 的写者；shadowed store、
  地址/offset 漂移和把 carry 当无条件兜底仍可能产生结构自洽但语义错误的 artifact；
- **方案**：新增 `bounded-cyclic-byte-lane-writer-graph` 和 base contract 的 `writer_graph=true`。runtime
  从每个 endpoint terminal 前逆序扫描，逐 lane 验证函数局部 store ID、常量地址导出的 byte offset、width
  及 last-writer 顺序；未覆盖 lane 只有在到达同一 merge load 时才能成为 carry，无前驱时才能成为 initial；
  路径限定最多 64 blocks、无重复且每段唯一前驱；
- **Poison/能力闭包**：图引用且可产 poison 的 store 输出 `byte_lane_poison_source`，紧邻 identity sidecar
  必须精确读取该变量；最终图引用集合阻止 legacy/conditional/其他函数 store 借程序级 capability 越权。
  capability 与 base marker 双向闭合，而 conditional、multi-arm、recursive 合同明确不获得 F387 标记；
- **验证**：独立最小模型 5 passed，F385--F387 联合 15 passed，全部 live-state Python 74 passed + 51
  subtests；LLVM18/17 基础循环实物均为 1 graph、2 endpoints、1 carry lane、2 store IDs，条件循环对照不含
  F387；完整 Python gate 903 passed + 229 subtests，受控 lit 245 discovered、244 passed、1 unsupported，
  LLVM17/18、Ruff、py_compile、whitespace 与交付门通过；
- **边界**：只证明基础循环、常量地址和唯一前驱分段，不是 conditional leaf writer graph、通用多 latch
  MemorySSA fixed point、symbolic pointer、并发内存模型或 LLVM poison predicate 的独立推导；无 coverage、
  吞吐、漏洞数或 benchmark 提升结论。conditional leaf 缺口已由后续 F388 独立合同闭合。详见
  [`F387研究报告`](research-progress/Proof_Carrying_Cyclic_Byte_Lane_Writer_Graph_F387_2026-08-13.md) 与
  [`F387证据`](evidence/f387-cyclic-byte-lane-writer-graph-2026-08-13/)。

## 383. F388：Proof-Carrying Conditional Cyclic Byte-Lane Writer Graph（2026-08-13）

- **问题**：条件循环的 backedge 前有 store/carry 两个 leaf 汇入多前驱 join；从 backedge 直接回溯需要猜测
  前驱，而仅检查 `conditional_transfers` 形状又不能证明 leaf 上声明的 store 是实际 last-writer；
- **方案**：新增 `bounded-conditional-cyclic-byte-lane-writer-graph` 与 direct/forwarded 合同
  `writer_graph=true`。将证明分为 seed→root、merge-edge→join、store/carry-leaf→branch 三段，每段逆向扫描
  最多 64 个唯一 block，只允许唯一前驱；逐 lane 验证常量地址导出的 store ID、byte offset、width 和首个
  覆盖顺序，carry 只允许在显式 join/branch 边界解析；
- **Forwarded 与 poison 闭包**：branch 到 arm 的 forwarded corridor 被纳入 leaf 重放，不依赖 block 相邻；
  只有 transfer/seed 图实际引用的函数局部 store ID 可携带 `byte_lane_poison_source`，紧邻 identity sidecar
  必须精确同源。multi-arm/recursive 合同不借用该 marker；
- **反例与实物**：checker 主动删除 capability/marker、漂移 store 地址和 poison source；独立模型还插入
  store-leaf 后置覆盖写和 carry-leaf 覆盖写。direct LLVM18/17 与 forwarded LLVM18 三份 producer artifact
  均为 1 graph、2 endpoints、1 transfer、1 store lane + 1 carry lane，全部篡改失败关闭；
- **验证**：F388 定向 5 passed，F385--F388 联合 20 passed，全部 live-state 79 passed + 51 subtests；完整
  capability-closed Python gate 908 passed + 229 subtests，node-ID 908/908 精确一致且零退化。完整 lit 与静态门
  保存在独立证据目录；
- **边界**：只覆盖 direct/forwarded 二叶条件循环和固定地址，不是 multi-arm、recursive condition tree、
  通用 MemorySSA、符号别名或并发内存证明；multi-arm 缺口已由后续 F389 闭合。无 coverage、吞吐、漏洞数或 benchmark 提升结论。详见
  [`F388研究报告`](research-progress/Proof_Carrying_Conditional_Cyclic_Byte_Lane_Writer_Graph_F388_2026-08-13.md)
  与 [`F388证据`](evidence/f388-conditional-cyclic-byte-lane-writer-graph-2026-08-13/)。

## 384. F389：Proof-Carrying Multi-Arm Cyclic Byte-Lane Writer Graph（2026-08-13）

- **问题**：三叶 multi-arm 树包含 root 与 inner 两级 branch；统一回溯会错误混合 leaf 作用域或在 join
  猜测前驱，现有 route/lane 元数据不能独立证明实际 last-writer；
- **方案**：新增 `bounded-multiarm-cyclic-byte-lane-writer-graph` 与 direct/forwarded marker。seed→root、
  merge-edge→join 独立验证；root arm 截止 root branch，inner-true/false arm 截止 inner branch。每段最多
  64 blocks、唯一前驱，首个覆盖 store 精确匹配函数局部 ID、地址 offset 与 width；
- **Poison/forwarded**：三条 forwarded corridor 各自纳入重放且限制 16 blocks；仅两个 store arm 实际引用
  的 store 获得 poison source，identity sidecar 必须同源；纯 carry arm 的任何覆盖写失败关闭；
- **验证**：LLVM18/17 direct 与 LLVM18 forwarded 均为 1 graph、2 endpoints、1 transfer、3 arms、2 store
  arms + 1 carry arm；定向5、F385--F389联合25、live相关84加51 subtests；完整Python 913加229 subtests，
  身份913/913精确一致；checker拒绝capability/marker/address/poison及shadow/carry-arm写反例；
- **边界**：recursive tree、一般 SCC/多 latch、通用 MemorySSA、符号指针和并发内存仍未覆盖；无性能或
  coverage结论。详见 [`F389研究报告`](research-progress/Proof_Carrying_MultiArm_Cyclic_Byte_Lane_Writer_Graph_F389_2026-08-13.md)
  与 [`F389证据`](evidence/f389-multiarm-cyclic-byte-lane-writer-graph-2026-08-13/)。

## 385. F390：Proof-Carrying Recursive Cyclic Byte-Lane Writer Graph（2026-08-13）

- **问题**：基础 recursive tree 有三层 branch、四个 leaf 和多前驱 join；仅验证树形元数据不能证明叶合同
  声明的 store 是从该叶回到其直接父分支之间的实际最后写者，也不能阻止兄弟子树作用域混合；
- **方案**：新增 `bounded-recursive-cyclic-byte-lane-writer-graph` 与 direct/forwarded marker。runtime 在已验证
  树遍历中重建每个 leaf 的 immediate parent，把证明拆为 seed→merge、merge-edge→join 和每个
  leaf-edge→parent branch；逐 lane 验证第一个覆盖 store 的函数局部 ID、地址 offset 与 width；
- **闭包**：只有基础 recursive/forwarded recursive 获得能力；grouped/repeated/composed/multicarry/
  multigroup/trigroup 全部拒绝借用。三个 store leaf 的 poison source 必须与紧邻 identity sidecar 同源，纯
  carry leaf 出现覆盖写即拒绝；
- **验证**：独立6、F385--F390联合31、live相关90加51 subtests；LLVM18 direct/forwarded及LLVM17 direct
  均为1 graph/2 endpoints/1 transfer/3 branches/4 leaves/3 store leaves+1 carry leaf，grouped对照0 graph；
  完整Python 919加229 subtests且身份919/919精确一致，完整lit 247 passed加1 unsupported；capability、
  marker、address、poison、shadow writer、pure-carry写及specialized borrowing反例全部失败关闭；
- **边界**：不是共享branch writer的复杂递归图、通用MemorySSA、一般SCC/多latch、符号指针、并发内存
  或LLVM poison predicate独立推导；无coverage、吞吐、求解速度、漏洞数或benchmark提升结论。详见
  [`F390研究报告`](research-progress/Proof_Carrying_Recursive_Cyclic_Byte_Lane_Writer_Graph_F390_2026-08-13.md)
  与 [`F390证据`](evidence/f390-recursive-cyclic-byte-lane-writer-graph-2026-08-13/)。

## 386. F391：Lease-Fenced Persistent Live-State Frontier（2026-08-13）

- **问题**：CAS continuation 已可恢复单个状态，真实 executor 也使用 KLEE 风格搜索策略，但 frontier、
  随机流和 coverage/subpath 统计仍是进程内对象；崩溃会丢失队列，多执行器可重复领取同一 checkpoint，
  超时接管后的旧 worker 还可能迟到提交；
- **方案**：新增 `symcc-persistent-live-state-frontier-v1`，用单调 generation 和互斥
  `ready/leased/done` partition 持久化 checkpoint 所有权；claim 同时证明搜索器恰好前进一次，随机 token
  栅栏结果，heartbeat 续租，TTL 回收只重新开放 ready，不授权旧 token；
- **搜索与提交闭包**：Python 随机源换为可版本化 `splitmix64-v1`，snapshot 保存策略配置、随机 state/draw、
  轮次、coverage/location/subpath 计数。completion 完整恢复所有 child 并校验 program root，在最新
  generation 上重放本次 observations；并发漂移返回 conflict 并 rebase，token 漂移返回 stale 并拒绝；
- **持久性与接入**：payload canonical SHA-256、`O_NOFOLLOW`有界稳定读取、file fsync + atomic rename +
  directory fsync；`LiveContinuationExecutor.resume_persistent()`真实接入候选选择、后台 heartbeat、执行、
  child closure 和 CAS completion；CLI 新增 `resume-persistent`/`frontier-inspect`；
- **验证**：定向19 passed加6 subtests，相关live-state 109 passed加57 subtests，双执行器10轮重复、80步
  随机重启状态机、原子replace故障、digest/noncanonical/future-generation/随机流/配置漂移反例通过；
  第一轮完整门禁发现并修复solver artifact临时目录延迟清理，GC回归证明无`ResourceWarning`；最终Python
  938 passed加235 subtests且node-ID 938/938精确一致，完整lit 249 passed加1 unsupported；
- **机制开销与边界**：本机overlay文件系统上5001累计状态的snapshot中位数5.984 ms，durable claim
  19.082 ms。结果显示单文件实现适合有界本地/小规模共享前沿，不代表大集群吞吐、coverage、LAVA-M、
  求解速度或漏洞数提升；MPI分片lease协议没有被替代。详见
  [`F391研究报告`](research-progress/Lease_Fenced_Persistent_Live_State_Frontier_F391_2026-08-13.md) 与
  [`F391证据`](evidence/f391-persistent-live-state-frontier-2026-08-13/)。

## 387. F392：Bounded Interpreter-Level ConDPOR（2026-08-13）

- **问题**：F57 的 `symcc-bounded-condpor-graph-v1` 只能从 observed native trace 恢复
  `R/W/C/A` 图与 revisit 候选；修改旧 read 的 `rf` 后，它可以 withholding 可能路径相关的后缀，
  但没有程序 transition oracle 重新生成控制流，因此不能声称 interpreter-level ConDPOR；
- **方案**：新增 closed finite `symcc-condpor-program-v1` 和
  `bounded-interpreter-sc-qfbv-condpor-v1`。replay 从线程入口按 graph add order 恢复 PC、local symbolic
  term、write equality 和 constraint outcome；稳定事件 ID 同时绑定 thread index 与 PC。`R` 枚举 init/
  current write，`C` 枚举可满足 outcome，`W` 枚举全部 coherence insertion 并对旧非因果同址 read
  生成 restriction/revisit，deleted 后继由解释器重新生成而非复用 trace；
- **一致性与证书**：每个 graph 要求 total same-location `rf`、per-location total `co`，并检查
  `acyclic(po∪rf∪co∪fr)`；路径由系统 libz3 求 QF_BV SAT。terminal symbol model 以逐符号 unsigned
  二分固定为词典序最小值；canonical graph SHA 去重，证书记录所有 bounds、prune/revisit/关系和精确
  claim scope，verifier 在摘要之后完整重跑。CLI 提供 durable atomic `explore`/`verify`；
- **多轮审查与验证**：修复 assert 后残留事件、n 元 `xor/concat` 非标准 SMT、undefined local 被普通
  prune 后错误 complete 三个问题；hash tamper 与 resealed semantic tamper 均拒绝。定向 12 passed、
  F392+旧 schedule 72 passed、warnings-as-errors 和静态门通过；直线 `2W+2R` 的 18 个候选由独立
  `po/rf/co/fr` oracle 得 4 个合法图，双对象双分支程序由独立 concrete SC interleaving oracle 也得
  4 类，均与 F392 精确相等；完整 capability-closed Python gate 为 950 passed 加 235 subtests，
  canonical identity 950/950 且零退化；完整 lit 251 discovered、250 passed 加 1 unsupported；
- **机制结果与边界**：11 样本中 1--5 writer 分别穷尽 1/2/6/24/120 个 coherence 终态，5 writer
  访问 154 graph、154 solver checks，中位 934.887 ms；控制重生成得到 1 normal、1 assertion error、
  1 accepted revisit、3 UNSAT，中位 61.237 ms。这是本机机制成本，不是 speedup/coverage/漏洞结论。
  当前不等价于 native pthread/LLVM、同步原语、dynamic threads、TSO/RA/C11、论文 unique maximal
  extension 或 unbounded optimality。详见
  [`F392研究报告`](research-progress/Bounded_Interpreter_Level_ConDPOR_F392_2026-08-13.md) 与
  [`F392证据`](evidence/f392-interpreter-condpor-2026-08-13/)。

## 388. F393：Recoverable UCSan Object Context（2026-08-13）

- **问题**：F11/F26 已实现编译式 harness、JITI pointer translation、对象动态扩容和结构 seed，但 loader
  边解析边修改全局状态，损坏尾部可留下部分 graph；dump 直接截断 target，unordered object ID/顺序不稳定，
  也没有对象数、shadow 数、总物化字节预算和跨进程恢复闭环；
- **严格准入**：v1 seed 从 stable regular descriptor 读取；识别 magic 后对 exact kind、root identity、
  point-by path、单 payload、signed bound、entry closure 和 graph budget 全部失败关闭。root/object/path 先进入
  临时容器，只有全图合法才一次提交；Python serializer/parser 使用同一合同并增加 canonical `verify`；
- **运行时正确性**：JITI 增加单对象、对象数、shadow 数、总物化字节和 path 深度预算；修复 pseudo-base
  signed subtraction、zero-size effective access、跨 int64 全域 object span 以及同 path 新 pseudo base 误复用
  stale shadow。旧 shadow 保持存活，path 表仅指向最新绑定；
- **规范快照**：root 按 ID、shadow 按 point-by path 排序，对象 ID 按首个规范 path 重新编号，每对象恰好一个
  payload；`_sym_ucsan_snapshot()` 和退出钩子均执行同目录 exclusive temp、file fsync、renameat、directory
  fsync，写失败不发布部分文件；
- **验证**：Python 10 passed/warnings OK，LLVM 17/18 UCSan 各5/5；native alias-cycle 对象从42改为43，
  snapshot由fresh process重放后第二份184-byte文件与第一份SHA-256完全一致；trailing-byte和对象预算负例由
  runtime拒绝。完整Python为954 passed加235 subtests且954/954身份精确，完整lit为251 passed加1既有
  unsupported；
- **机制成本与边界**：31个whole-process样本、每样本两次durable publication，中位16.850 ms、P95
  17.863 ms。只证明当前JITI对象上下文的规范恢复；不是任意native continuation、完整C++ ABI、完整
  UCSan OOB/UAF/UBI checker，也不声称coverage、漏洞数或跨系统性能提升。详见
  [`F393研究报告`](research-progress/Recoverable_UCSan_Object_Context_F393_2026-08-13.md) 与
  [`F393证据`](evidence/f393-ucsan-recoverable-object-context-2026-08-13/)。

## 389. F394：UCSan Explicit-Object OOB/UAF Closure（2026-08-13）

- **问题**：F11/F393 对 under-constrained JITI 对象按需扩容是正确的，但 stack/heap 显式对象若沿用同一
  growth 路径，会把真实越界扩成合法对象；原实现也没有 free/function-exit tombstone，alias 在释放后仍会
  进入原生访存；
- **对象分型**：runtime `Object` 增加 native ownership、`STACK/HEAP`、exact size、dynamic frame 与
  freed 状态；`knownShadows` 约束 shadow 身份。JITI 继续有界 provision，explicit load/store/atomic/
  memintrinsic 在原操作前严格检查 lower/upper/crossing bounds；零长度操作不伪造访问；
- **编译接入**：scoped function 动态 `push_frame`，每个 alloca 注册 count×element-size；标准 C
  malloc/calloc/aligned_alloc/realloc/reallocarray/free 与常见 Itanium new/delete、array、sized/aligned 家族
  注册同一 heap metadata。return、Itanium resume 和直接 noreturn throw/rethrow 结束 frame；musttail 与
  Windows funclet 编译期失败关闭；
- **释放与重分配事务**：free/delete 在 libc 前验证 heap/base/live，保留 tombstone 供外部 alias 检出
  UAF；realloc 使用 prepare/commit，失败保留 old live，原址更新 bounds 并清空尺寸变化区间，搬迁时转移
  保留区 pointer-slot shadows 后失效旧 object；reallocarray overflow failure 不误报；
- **验证**：LLVM17/18 UCSan 定向各9/9；native C 覆盖合法 stack/heap/calloc/realloc、双边 OOB、跨界
  memcpy、heap/stack UAF、double/stack free、realloc failure/slot-shadow 与 reallocarray overflow；C++
  覆盖 new[]/delete[] OOB/UAF 和两类 unwind，并验证原地扩缩不泄漏旧slot shadow。14 mode×11机制复跑为154/154预期结果；完整 Python
  954 passed加235 subtests且954/954身份精确；完整lit为255 passed加1个既有unsupported（共256项）；
- **边界**：实现论文 §3.4 allocation-level OOB/UAF 核心闭环，不实现逐字节 UNINIT/branch sink UBI、
  custom allocator effect mapping、Windows/non-local exit、kernel wrapper或论文等CPU bug-yield；机制结果不
  声称coverage、漏洞数或speedup。详见
  [`F394研究报告`](research-progress/UCSan_Explicit_Object_Checkers_F394_2026-08-13.md) 与
  [`F394证据`](evidence/f394-ucsan-explicit-object-checkers-2026-08-13/)。

## 390. F395：UCSan Byte Initialization and UBI Sink Closure（2026-08-13）

- **问题**：F394 已建立显式 stack/heap 对象的 exact bounds 与共享 tombstone，但没有记录对象内哪些字节
  已由程序定义；allocation-level 单 bit 无法正确表达局部 store、短 copy、realloc retained prefix，普通
  未初始化 load 与真正控制解引用/分支的危险使用也不能区分；
- **字节状态**：`Object.initializedBytes` 为每个显式字节保存 INIT/UNINIT；alloca、malloc 与常见 new 初始
  UNINIT，calloc 初始 INIT，JITI 对象保持 under-constrained 而不进入 UBI。store/memset 精确覆盖范围，
  memcpy/memmove 用 overlap-safe 临时副本同步复制 byte tag 与 pointer-slot shadow；64 MiB 默认累计预算由
  `SYMCC_UCSAN_MAX_EXPLICIT_SHADOW_BYTES` 约束，free/pop-frame 回收记账；
- **传播与 sink**：load 把访问范围的 UNINIT OR 提升为动态 SSA tag；cast/freeze/GEP/operator/PHI/select
  继续传播，256 槽 TLS argument/return channel 跨 scoped call。普通 load 不立即报告，只有非零 pointer
  dereference、branch/switch、indirect target、allocator/memory extent 或未建模 external pointer boundary
  命中时，在原生操作前失败关闭；零长度 memintrinsic 保持 no-op；
- **事务与原子**：realloc failure 保留旧 tag，缩小保留前缀，增长尾部 UNINIT，搬迁复制交集范围；atomic RMW
  恢复旧值 tag 与整数化 pointer provenance，cmpxchg 分离 `{old,success}` 字段并只在成功时提交 pointer-slot
  shadow。当前 hook 与 native atomic 分离，不宣称跨线程 metadata 线性化；
- **审查修复**：生成的 `__symcc_ucsan_stub.*` 曾被误认作 runtime helper，导致 arbitrary external 绕过
  argument shadow/TLS；helper 判定现仅接受 `_sym_ucsan_`，native 反例验证 invalidation 后 alias UAF。
  同轮还修复 zero-size pointer 误报、cmpxchg aggregate 过度合并与 atomic pointer integer-lowering provenance；
- **验证**：LLVM17/18 UCSan 定向各10/10；28个native mode×11次为308/308预期结果，其中12类合法、16类
  sink/传播拒绝；完整Python 954 passed加235 subtests、954/954身份精确且零退化；完整LLVM18 lit为256
  passed加1个既有unsupported（257项）。Ruff、Python format、py_compile与whitespace门通过，环境无
  clang-format，双LLVM构建提供C++编译门；
- **边界**：尚未实现global Super Object、一般libc/custom effect、full DFSan、复杂non-local stack lifecycle
  与concurrent shadow linearizability；unknown native source copy按initialized处理。所有数字均为机制/回归证据，
  不声称coverage、漏洞产出、单指令开销或跨系统speedup。详见
  [`F395研究报告`](research-progress/UCSan_Byte_Initialization_and_UBI_F395_2026-08-13.md) 与
  [`F395证据`](evidence/f395-ucsan-byte-initialization-ubi-2026-08-13/)。

## 391. F396：Native Parameter Provider and Lifecycle-Safe ParaSuit Registry（2026-08-13）

- **问题**：F186 的条件参数图仍以随源码发布的机器 schema 为主要事实来源；当前
  executable 与声明可能漂移。更严重的是 query-solver/campaign 启动参数被逐 seed
  policy 选择后并不会改变已经启动的服务或 master，却会获得错误 reward attribution；
- **原生协议**：新增 `symcc-parameter-provider-v1`。`symcc-query-solver
  --print-parameters` 原生声明其真实读取的 23 个 prefix-cache、selective-query 和
  PSCache 参数；coordinator 生成 40 项 native contract。provider 命令不经过 shell，
  stdout 流式限制 1 MiB，timeout 限制 50 ms--30 s；name/value/finite bounds/
  `active_when`/scope 全部严格解析；
- **原子合并与生命周期**：同名同合同去重，任一 values/bounds/condition/scope 冲突
  拒绝整个 provider；最终 53 项 registry 分成 28 task、23 query-service、2
  coordinator-campaign。只有 task 项进入逐 work-item Thompson/context/pair 策略，
  从而修复 selective-query/TACE no-op attribution；
- **可恢复性**：state schema 升级为 v3，持久化 provider source/digest/count/error/
  conflict、provenance hash 和 task schema hash。当前 schema hash 不一致时旧 posterior、
  pending assignment 和动态 value 不导入；静态 schema 保留为与 native contract 对拍的
  兼容快照和显式消融入口；
- **验证与结果**：定向 Python 15 passed，LLVM17/18 真实 provider lit 各1/1；超时、
  oversize、NaN、条件环、整 provider 冲突和 stale state 反例失败关闭。100 次独立发现
  均成功且 provider/registry hash 各唯一，中位 1887.812 us、P95 2331.673 us、最大
  2518.865 us；完整Python为960 passed加235 subtests且身份960/960，完整lit双复跑均为
  257 passed加1既有unsupported；这是 campaign 初始化机制成本，不是 coverage 或求解 speedup；
- **边界**：未复现 ParaSuit 的通用 binary/help extraction、完整 per-program value-space、
  silhouette/clustering policy、query-service 配置进程池和 12-program/公开目标等 CPU
  效果实验。详见
  [`F396研究报告`](research-progress/Native_Parameter_Provider_and_Lifecycle_Routing_F396_2026-08-13.md) 与
  [`F396证据`](evidence/f396-native-parameter-provider-2026-08-13/)。

## 392. F397：Program-Bound ParaSuit Value Space and Silhouette Policy（2026-08-13）

- **研究问题**：F396 已按生命周期发现和路由参数，但 task 参数值仍主要来自静态
  registry/局部扩展，没有判断某个目标程序的历史取值是否形成稳定高收益区域；
- **程序绑定与观测**：MPI master 用完整 argv 和目标可执行内容 SHA-256 构造 program
  identity；assignment token 只把一次完成执行的 bounded reward、elapsed、killed 与实际
  选择值归因一次。context 达到样本门槛时使用局部窗口，否则回退到本程序参数窗口；
- **Algorithm 2 适配**：新增无第三方依赖的确定性 MeanShift 与 silhouette gate。数值轴
  归一化，reward 按相对 worker 成本校正；稳定分区按簇平均 utility 抽簇并以簇内加权
  均值构造值，退化/不足/低 silhouette 时探索或回退 contextual Thompson。负数、零值、
  整数及 provider min/max 显式封闭；
- **有界性与恢复**：分析窗口最多 256，持久历史每参数最多 512，sidecar 最多 4 MiB。
  sidecar 绑定基础 state 字节 SHA-256、sequence、task schema、provider provenance、程序
  identity 与完整策略；读取用单个 no-follow regular-file snapshot，并把已验证的基础映射
  直接导入，关闭校验后按路径重开竞态；
- **验证与机制结果**：9 项 F397 定向测试及相关回归 210 passed + 84 subtests；完整
  Python为969 passed + 235 subtests且身份969/969，完整LLVM18为258 passed加1个既有
  unsupported；1000/1000
  随机有界性质通过。固定 8 样本双簇的 label 为 `00001111`、silhouette 为
  `0.914156472`；1000 次聚类中位 75.033 us，adaptive selection 中位 85.779 us，state
  pair 冷载入中位 668.729 us；
- **边界**：未实现 ParaSuit 完整 standalone baseline/branch-rarity/synergy parameter
  selection，也未做官方 sklearn cross-oracle、query-service 配置进程池或 12-program
  等 CPU campaign。所有数据为本机小样本机制结果，不声称 coverage、solver throughput、
  bug yield 或 speedup。详见
  [`F397研究报告`](research-progress/Program_Bound_ParaSuit_Value_Space_F397_2026-08-13.md) 与
  [`F397证据`](evidence/f397-parasuit-value-space-2026-08-13/)。

## 393. F398：ParaSuit Branch-Rarity Parameter Selection and Synergy-Aware Hybrid Policy（2026-08-13）

- **研究问题**：F397 已按程序聚类并选择单个参数值，但下一次选择哪些参数仍由 F186
  hierarchy 决定，尚缺 ParaSuit 的 standalone baseline、inverse-frequency branch score
  和组合惩罚；直接使用 AFL 全局新增位又会把其他 worker 的先到先得竞争错误归因给本配置；
- **精确反馈面**：master 从同一次 worker telemetry 提取 `(stable uint64 site,taken)`，
  去重后用确定性 BLAKE2b MinHash 限制到每次128项，历史限制16--256次。它只驱动参数
  学习；QSYM branch-interest bitmap 仍负责重复求解判断，AFL edge bitmap 仍独占 corpus
  novelty。缺 telemetry 不制造覆盖观测；
- **两阶段算法**：初始阶段依次执行28个task参数的88个声明非unset值，只注入目标值、
  传递激活父项和hard guards；以 `Σ1/frequency(branch outcome)` 建立参数独立baseline。
  组合阶段若运行得分低于某成员baseline，就将该成员本次credit置零，否则保留运行得分，
  再平均并归一化。可选pure ParaSuit独立概率、F186 hierarchical消融，默认用0.6 rarity加
  0.4 hierarchy rank融合；证据不足时失败关闭回hierarchy；
- **审计差异**：官方提交先计算inverse frequency，但后续求和使用branch count；F398按
  论文公式实现，并用“频率4与1、分支个数相同”的差分反例固定0.25与1.0排序，避免复制
  参考源码的变量替换行为；
- **一致恢复**：新增parameter-selection sidecar，绑定base/value文件SHA-256、sequence、
  task schema、provider provenance、program identity和精确策略。F397暴露实际认证并导入的
  两个摘要，F398不重开路径；missing/torn/symlink/策略漂移清空三层学习。受限恢复
  `selection_phase`，修复in-flight standalone在重启后被误记为组合样本的问题；
- **验证与结果**：F398定向11 passed，自配置回归35 passed，相关回归272 passed加116
  subtests；完整capability-closed Python为980/980 passed加235 subtests且身份精确，完整
  LLVM18为260 discovered、259 passed加1既有unsupported，LLVM17/18定向各1/1；两类
  1000/1000随机性质通过。固定oracle中A被惩罚为0、B归一化为1；1000次分析中位3.716 us、
  ParaSuit选择22.554 us、128条trace MinHash 79.099 us、三文件恢复700.904 us；
- **边界**：当前是机制与回归证据，不包含ParaSuit 12程序、公开/LAVA-M等CPU多种子
  campaign，不声称coverage、solver throughput、bug yield或speedup；尚需sklearn
  cross-oracle、query-service配置进程池与跨文件generation commit。详见
  [`F398研究报告`](research-progress/ParaSuit_Branch_Rarity_Parameter_Selection_F398_2026-08-13.md) 与
  [`F398证据`](evidence/f398-parasuit-parameter-selection-2026-08-13/)。

## 394. F399：Cottontail-Inspired Alpha-Normalized Constraint Selection（2026-08-13）

- **问题与审计纠正**：Cottontail 的 ECT 保存 branch/call-context/taken/visit 等结构，
  constraint expression 是独立通道；旧审计把“ECT 不存 solver AST”直接等同于缺少完整
  Cottontail 表示并不准确。官方 artifact 对同分支 SMT 字符串以 `k!n -> k!X` 规约，
  但该替换不理解 bit width、变量 alias、根次序或 Query IR 上下文；
- **结构等价**：新增 `AlphaConstraintShapeIndex`，一次失败关闭地验证最多 250,000 个
  稠密拓扑 Query IR 节点，再按首次结构出现把绝对 `read.index` 重命名为 alpha slot。
  op、位宽、常量、attributes、ordered children/roots 和 read alias 保持语义；节点与根
  descriptor 用 canonical JSON + SHA-256 内容寻址；
- **上下文正确性**：分别生成 target shape，并对“最近最多8个prefix roots + target”
  联合投影；联合域保留 target 是否复用 prefix read 的关系，关闭两次独立归一化造成的
  alias误合并。site/branch fallback、desired、joint shape 与 target shape 形成context类；
- **事务与调度**：SQLite `query_shapes` 在 `BEGIN IMMEDIATE` 中分配rank和representative。
  默认 `structural` 按shape novelty、explicit priority、depth、age排序；rank-r初始score为
  `1/r`，300秒线性恢复为1，因此shape惩罚不会永久饿死旧duplicate。旧dfs/bfs/priority及
  `SYMCC_QUERY_SHAPE_SELECTION=0`保留消融；SAT/UNSAT不由shape推断，model仍经Query IR
  validation和concrete replay；
- **生命周期**：`SYMCC_QUERY_TRAVERSAL`和`SYMCC_QUERY_SHAPE_SELECTION`进入可执行
  parameter provider并标记query-service，逐seed policy仍只有28个task参数；生产合并
  registry由62增至64项，其中28 task、25 query-service、11 campaign；
- **验证与结果**：定向9 passed，相关119 passed加22 subtests；完整capability-closed
  Python为989/989 passed加235 subtests且零身份漂移；LLVM17完整lit为261 discovered、
  259 passed加2 unsupported。1000轮性质检查的alpha重命名、常量扰动、alias扰动各
  1000/1000通过；64 reads/192 nodes/1000次的实际target+joint ingest pipeline中位
  514.565 us，复用validated DAG投影中位73.100 us；
- **边界**：这不是完整Cottontail LLM/solve-complete复现，也没有公开目标、LAVA-M或
  等CPU多种子coverage实验。机制延迟不能写成solver throughput或覆盖率提升；历史
  QueryStore shape重建、跨节点类同步及ECT/query联合学习仍待研究。详见
  [`F399研究报告`](research-progress/Cottontail_Alpha_Normalized_Constraint_Selection_F399_2026-08-13.md) 与
  [`F399证据`](evidence/f399-cottontail-alpha-constraint-selection-2026-08-13/)。

## 395. F400：Persistent Multi-Objective Live-State Feedback（2026-08-14）

- **研究缺口**：F391 已有真实 continuation frontier、generation CAS、lease fencing 和
  可恢复搜索快照，但完成效果没有形成跨 claim 的持久反馈；独立进程内多目标 API 不能
  直接替代 durable policy，否则会丢失重启顺序并让迟到 worker 污染策略；
- **生产接线**：`LiveStateSearchPolicy` 新增 `multi-objective`，直接消费当前 checkpoint
  的 location novelty、未覆盖/目标距离、路径深度、query cost、loop exit，以及按
  `SHA256(location,target)` 聚合的历史 location gain、成本、不确定度和失败率。选择为
  确定性 argmax，identity 负责平分，不消耗随机流；
- **事务反馈**：claim 的 selection transition 强制恰好一个 attempt；完成在最新
  generation 上重放 location observations，只把仍然新颖的位置计入 gain，再由
  observation transition 强制至多一个 completion。generation conflict 重读重算，
  stale token 零写入；
- **失败闭环**：深度 review 发现初版 failure penalty 没有生产写入者。F400 扩展
  `abandon()` 为可选 generation-bound observation transaction；执行或 heartbeat 失败
  原子写入零增益失败 outcome 并归还 checkpoint，避免高不确定度反复偏爱故障状态；
- **恢复与上界**：新写入 snapshot v2 保存有界整数 attempt/completion/location-gain/
  steps/queries/failures，保持 `failures <= completions <= attempts`；v1 以空历史升级。
  outcome table 预留 `@overflow`，修复首次溢出写出 `limit+1` 项而不可恢复的问题；
- **验证与结果**：定向27 passed加6 subtests，全部 live/persistent/path-cover/ConDPOR
  相关回归131 passed加57 subtests；完整 capability-closed Python 为995/995 passed加
  235 subtests且身份精确；LLVM17完整lit为261 discovered、259 passed加2既有
  unsupported。31样本机制测试中，64/1024/4096候选的多目标选择中位为
  0.128764/2.164946/8.027188 ms；这只证明本机Python选择成本与线性规模，不包含
  frontier I/O、solver或目标执行；
- **Review 与边界**：五轮审查分别修复overflow容量、候选×context复杂度、保留key、
  double-completion oracle、失败事务和coverage归因。`coverage_gain`明确表示live
  interpreter CFG location增量，不是AFL bitmap或native replay coverage。F400是W1的
  生产反馈基础，不等价于完整Empc/CBC/CGS/TopSeed，也没有公开目标coverage、bug-yield
  或端到端speedup结论。详见
  [`F400研究报告`](research-progress/Persistent_Multi_Objective_Live_State_Feedback_F400_2026-08-14.md) 与
  [`F400证据`](evidence/f400-persistent-multi-objective-live-state-2026-08-14/)。

## 396. F401：Persistent Empc-Style Live-State Multiple Minimum Path Covers（2026-08-14）

- **研究缺口**：既有 path-cover 只消费完成后的 concrete trace 并更新 seed/PrefixDAG，
  不能调度 F391 的可恢复 pending symbolic states；不完整的 call graph 又缺少静态 return
  edge，直接求 interprocedural cover 会产生错误路径事实；
- **图与 MPC**：`LiveProgramGraph` 分离全局 distance graph 和函数内 cover graph，使用迭代
  Kosaraju 折叠 SCC，在 DAG split-node 图上执行迭代 BFS 增广的 maximum matching，并用
  固定 `max(32,16*K)` exclusion 预算寻找至多 K 个不同 minimum covers。公共枚举原语由
  seed planner 与 live planner 复用，但两者不共享易变反馈；
- **checkpoint compatibility**：复用已持久化的
  `uint64_be(SHA256(site NUL choice)[0:8])` branch token，只有当前函数唯一映射才能收缩
  active cover。未知、跨函数、同 site 重用、token 碰撞、SCC 内边和未保留 edge 均保守
  不缩小集合；
- **调度与复杂度**：每个 frontier generation 一次投影 covered location，并为每个 cover
  反向预计算 component suffix 的未覆盖比例；候选以0.40 support + 0.45 remaining +
  0.15 current novelty排序。无 plan 回 BFS，平分用 checkpoint identity，零随机消耗；
- **生产与恢复**：`resume()` 和 `resume_persistent()` 均传入 generation-local context；graph
  cache 绑定program root、MPC bounds和enable flag。snapshot v3持久化cover/node两个边界，
  v1/v2可升级；配置漂移由现有transition verifier拒绝，claim/complete/failure仍遵循F400
  generation CAS与lease fencing；两类执行结果均公开函数准入、cover/component、token
  歧义等派生graph telemetry，使超限或无plan的BFS降级可审计而不污染snapshot；
- **审查修复**：递归 maximum matching 和 SCC 改为迭代；唯一 cover 的 exclusion 工作量
  不再随V线性重匹配；coverage从每candidate扫描L个位置改为一次/generation预计算；
  call graph和token collision均增加反例边界；公共枚举器以迭代拓扑检查拒绝环图；
- **验证与机制结果**：F401定向37 passed加6 subtests，相关199 passed加57 subtests，完整
  capability-closed Python为1003/1003 passed加235 subtests，LLVM17为259 passed加2个
  既有unsupported。1--5节点DAG matching与穷举oracle一致，SCC与双向reachability
  oracle一致；31样本中769节点/512候选的MPC构建、coverage context、整批guidance和
  policy select中位分别14.992490/0.454554/1.221638/0.075273 ms；
- **边界**：这是Empc式live-state MPC的机制闭环，不是Empc artifact逐行复现，也未实现
  CBC、CGS或TopSeed。实验没有target、solver、MPI、AFL map或公开campaign，不声称
  coverage、solver throughput、bug yield或端到端speedup。详见
  [`F401研究报告`](research-progress/Persistent_Empc_Live_State_Path_Cover_F401_2026-08-14.md) 与
  [`F401证据`](evidence/f401-persistent-empc-live-path-cover-2026-08-14/)。

## 397. F402：Compatible Branch Coverage Live-State Pruning（2026-08-14）

- **来源核验**：以PLDI 2024论文（DOI `10.1145/3656443`）和作者CBC-SE V2.1工件
  （DOI `10.5281/zenodo.10960926`）为主源，确认CBC核心是fork-time branch-compatible
  set一致性剪枝，而不是普通未覆盖距离排序；
- **静态证明**：`LiveProgramGraph`以迭代worklist恢复SSA-like input/nondet provenance，
  为`assume`建立跨输入correlation atom，以整数bitset迭代计算post-dominator和控制依赖，
  再传递controller完整closure。只有同函数、均可分析、closure不相交的分支才两两兼容；
- **保守边界**：load/heap/参数/异常对象、cyclic或无exit分支、未知assume、重复site/token
  碰撞、超限图均fail-open；深度review发现“只禁止跨函数分组”仍不能排除caller约束影响
  callee，最终对普通/间接调用的caller和callee整体关闭CBC；
- **生产剪枝**：普通`branch`和`throw_if`先做feasibility，再在CAS commit前构造prospective
  child并检查CBC。状态压力门默认5；持久claim把全局ready/lease压力带入本地slice。全拒绝
  时防御性保留全部child，被实际拒绝的child不发布checkpoint；
- **持久恢复**：search snapshot v4绑定threshold/function-node/branch三个CBC边界，v1--v3
  可升级，配置进入transition verifier和program-graph cache key。环境不同的重启worker仍
  使用frontier原配置；执行结果公开static admission与checks/pruned/fail-open/pressure；
- **验证与机制结果**：定向42 passed加9 subtests，全部live/persistent相关132 passed加60
  subtests；完整Python为1013/1013 passed加238 subtests且node-ID摘要精确，LLVM17为
  260 passed加2个既有unsupported。2--8分支的508个布尔模式穷举oracle保留每个分支
  true/false；6独立分支11轮机制对照中，终态64降到2、checkpoint 126降到12、
  feasibility checks 126降到22，wall-clock中位2.776778秒与0.487879秒；
- **声明边界**：synthetic模型刻意匹配独立笛卡尔积，不是公开目标或作者实验复现；不把
  本机wall-clock比例外推为通用speedup，也不声称path completeness、bug witness保留、
  LAVA-M coverage或bug-yield提升。详见
  [`F402研究报告`](research-progress/Compatible_Branch_Coverage_Live_State_Pruning_F402_2026-08-14.md) 与
  [`F402证据`](evidence/f402-compatible-branch-coverage-2026-08-14/)。

## 398. F403：Concrete Constraint Guided Scheduling（2026-08-15）

- **来源与算法身份**：以ICSE 2024论文（DOI `10.1145/3597503.3639078`）、作者提交
  `7cf3890d80df12660255d81a8b50945cf1cc85a3`和Zenodo 10516325为主源，确认CGS核心是
  具体store value驱动的target FIFO优先，而不是query-complexity score或路径剪枝；
- **保守静态准入**：分析函数内固定地址、无guard、同址同宽的`store -> load -> icmp`，
  支持迭代identity链、一层`and/or`及十类1--64 bit有/无符号比较。稳定store token与
  branch site均检测碰撞；symbolic/guarded/overlapping write、memory-affecting call、
  ambiguous SSA和预算全部fail-open；
- **复杂度修复**：初版branch逐store扫描被review为最坏二次复杂度，最终以每store最多8个
  byte slot和`(function,address,width)`精确索引实现线性构建、每目标常数宽度重叠检查；
- **生产调度**：解释器记录instruction clock、具体branch mask和latest concrete store
  marker；continuation停在store时只对已具体且满足未覆盖侧的RHS前瞻。满足target的state
  进入FIFO优先队列，其余state保留普通FIFO；CGS不决定SAT/UNSAT且不删除checkpoint；
- **持久恢复**：snapshot v5绑定四项CGS配置与instruction/outcome/partial-order/
  observation/dropped状态，兼容v1--v4。claim完成在最新generation按序重放压缩事件；
  conflict重读重放、stale lease零写入，transition verifier校验outcome bit所需最小观测；
- **验证与机制结果**：定向47 passed加11 subtests，相关216 passed加65 subtests，完整
  capability-closed Python为1025/1025 passed加243 subtests；LLVM17为263 discovered、
  261 passed加2既有unsupported。独立oracle的80 case/10200 predicate评估全部通过。
  500指令distractor机制中首次target预算519降至18（96.5318%），但完整运行均519步，
  wall中位3.430163与3.441958秒，不形成总体speedup；
- **边界**：这是content-addressed continuation的函数内保守CGS子集，不是作者IDA/KLEE
  工件等价复现；没有公共目标、coverage、solver throughput、bug yield或完备性结论。
  详见[`F403研究报告`](research-progress/Concrete_Constraint_Guided_Scheduling_F403_2026-08-15.md)
  与[`F403证据`](evidence/f403-concrete-constraint-guided-scheduling-2026-08-15/)。

## 399. F404：TopSeed-Style Persistent Campaign Seed Selection（2026-08-15）

- **来源与身份纠正**：以ICSE 2025论文（DOI `10.1109/ICSE55347.2025.00095`）、
  Zenodo 14602979和作者GitHub HEAD `2193d090a6650edc08ee30b927e65ba9e6f9e045`
  为主源，确认TopSeed是跨短symbolic-execution runs选seed，不是topological
  pending-state search。论文`eta_Learn=20`与工件`eta_lp=10`的差异被显式记录；
- **画像与分组**：候选先进入content-addressed object store，每轮有界冷showmap；AFL
  map的每个bucket bit编码为`(index<<3)|bit`，exact feature set经域分隔SHA-256分组。
  画像失败/空结果不改变普通调度，QSYM interest bitmap和AFL corpus novelty保持独立；
- **Explore/Exploit/Learn**：Explore保留coverage、inverse frequency、uniqueness、bug
  proxy、group size五特征和`unique/long/short/random`四策略；Exploit按生成覆盖的inverse
  rarity打分。独立oracle暴露普通Lloyd二聚类局部划分问题，最终用排序+prefix SSE枚举
  实现全局最优一维k=2。权重用均匀/截断正态学习，四策略都有非空证据后才更新概率；
- **MPI事务**：`propose`不修改历史，现有target/state/parameter/lease准入和`comm.send`
  成功后才`commit`；worker-rank绑定run token，triage只聚合worker实际保留输出的AFL
  feature union与branch-trace proxy。send失败、dispatch拒绝discard，timeout/stale/watchdog/
  shutdown orphan形成failed outcome，晚到或重复结果不能二次学习；
- **持久恢复**：canonical snapshot绑定target program context、SplitMix64 state/draw、五维
  分布、四策略概率、候选/运行/组和守恒计数；`O_NOFOLLOW`有界读取、严格schema和原子
  fsync发布。review修复只含PID临时名遇到崩溃残留后永久保存失败，改为同目录安全唯一
  临时文件；候选/运行容量只驱逐无引用或已完成历史，全pending容量在派发前失败开放，
  防御性commit失败显式discard而不泄漏proposal；
- **验证与机制结果**：TopSeed/MPI相关202 passed加91 subtests，完整capability-closed
  Python 1041 passed加250 subtests且node-ID精确；LLVM17为264 discovered、262 passed加
  2既有unsupported。独立group/policy/rarity/k=2/bitmap/restart oracle共11417次全部通过。
  10000候选、2500组、11轮机制测量验证proposal/snapshot/restore成本；刻意有利固定权重
  模型中高价值输入由FIFO第501次变为第1次派发；
- **边界**：AFL bucket-bit group、branch-trace proxy和retained output是本框架适配，不是
  作者KLEE/gcov数据的等价复现。未进行论文17程序或等CPU多种子campaign，不声称35.5%
  branch gain、10个bug、solver throughput或总体speedup。详见
  [`F404研究报告`](research-progress/TopSeed_Persistent_Campaign_Seed_Selection_F404_2026-08-15.md)
  与[`F404证据`](evidence/f404-topseed-campaign-selection-2026-08-15/)。

## 400. F405：Certified Finite Heap-Lifetime Pointer Union（2026-08-15）

- **来源与定位**：以arXiv `2407.16827v2`和SANER 2026 POSE论文为主源，并以KLEE
  OSDI 2008对象内存模型为传统对照。POSE把alias/distinctness关系编码为ITE、只在实际
  控制流决策分叉；F405借鉴该原则，但严格限定在bounded C heap中已布局的有限对象基址，
  不声称初始符号堆、fresh object、Java字段/继承语义或完整POSE复现；
- **编译器证明**：`free`参数经既有`pointerAlternatives`恢复select/PHI有限域；每个非空
  候选必须是普通heap object base，禁止dynamic index、interior offset与exception arena，
  null必须具有干净provenance。任一坏候选导致整体lowering失败；合法基址排序去重后输出
  精确`op/address/addresses/bits`合同和`bounded-heap-lifetime-pointer-union` capability；
- **制品与运行语义**：validator闭合精确字段、capability依赖、1--64 bit、最多256候选、
  升序唯一和ordinary-heap归属。执行期再次绑定表达式实际位宽；`valid=NULL OR any(release)`
  进入路径约束，`live:=live AND NOT release`、`size:=ITE(release,0,size)`和逐字节init marker
  使用同一条件更新，且不产生alias-choice continuation fork；
- **深度review修复**：新增certificate/expression width运行时一致性检查和篡改反例；独立
  oracle初版对共享expression DAG重复递归，改为每input digest memoization，在保持34816次
  语义判定不变的同时消除测试自身的重复计算；明确consumer结构校验不能独立重证producer
  completeness，遗漏候选会保守约束行为而不是错误释放对象；
- **验证与机制结果**：LLVM17/18整份continuation lowering fixture均通过，select/PHI/null
  证书为`[64,80]`、`[64,80]`、`[64]`；heap/pointer相关17 passed，独立oracle覆盖domain
  1--16、4096个input assignments、34816个marker判定、restart equality和4类篡改。完整
  Python为1041 passed加250 subtests；LLVM17为264 discovered、262 passed加2既有unsupported。
  16候选11轮机制测量为49 steps、0 forks，resume中位1.203925470秒；16→1仅为分析态基数；
- **边界**：symbolic free后的alias-aware survivor load仍受现有MemorySSA initialization
  writer graph支持域限制；没有一般pointer arithmetic、跨过程allocator wrapper、native
  adapter或公共heap-heavy campaign，不声称coverage、solver throughput、bug yield或总体
  speedup。详见
  [`F405研究报告`](research-progress/Certified_Finite_Heap_Lifetime_Union_F405_2026-08-15.md)
  与[`F405证据`](evidence/f405-certified-heap-lifetime-union-2026-08-15/)。

## 401. F406：Collective Dominating Heap-Union Initialization（2026-08-15）

- **研究缺口**：F405已能在一个continuation中条件释放有限heap-base pointer union，但
  后继survivor load仍被旧初始化门误拒。旧门要求某一条store单独覆盖全部load alternatives；
  对“left/right分别由两条无条件支配store初始化”量词次序过强；
- **集合支配证明**：保留旧single-store快速路径后，只对至少两个普通本地heap alternatives
  启动collective cover。候选禁止dynamic index、interprocedural/calloc/stack混合；store必须
  非volatile/atomic、支配load、宽度足够、pointer domain恰好一个alternative。按heap-object
  identity、base与静态字节区间累计cover bitset，要求至少两条不同store贡献、全部alternative
  covered且至少两个不同object base；缺失覆盖和branch-local非支配store继续失败关闭；
- **proof-carrying合同**：成功load输出升序唯一`initialization_bases`并声明
  `bounded-collective-heap-union-initialization`。consumer要求该capability依赖heap lifetime与
  pointer union，证书长度2--256且全为普通heap base，并按load宽度重建每个alias address的
  owner，要求owner-base集合与证书精确相等；capability必须至少有一个合同使用点；
- **运行语义**：证书只控制lowering准入，不设置runtime initialized状态。F405仍以条件
  `release=(victim==base) AND live`更新`live/size/init`；后继load仍消费真实alias guard与
  live/init marker。因而伪造结构证书不能绕过已释放/未初始化访问检查；
- **验证结果**：LLVM17/18新增正例均返回11/22，不完整覆盖和非支配写入负例均拒绝；
  heap/pointer相关17 passed，测试身份门11 passed。独立oracle覆盖domain 2--32、1--8 byte，
  完成4216次区间检查、10912个不同victim/survivor赋值，旧规则248组预期拒绝、partial 248组、
  nondominating 248组和4类证书篡改全部符合预期。32对象、11轮、每轮10000次Python参考证明
  中位3986 ns/proof；992→1仅为分析态基数；
- **边界**：不是完整POSE、一般MemorySSA、任意符号堆或path-sensitive definite assignment；
  F406单独不处理branch-correlated predecessor stores，2026-08-16由F407在有界guard-tree
  子域继续扩展。尚无公开coverage、solver、bug-yield或端到端speedup结论。详见
  [`F406研究报告`](research-progress/Collective_Heap_Union_Initialization_F406_2026-08-15.md)
  与[`F406证据`](evidence/f406-collective-heap-union-initialization-2026-08-15/)。

## 402. F407：Guard-Correlated Heap-Union Initialization（2026-08-16）

- **缺口与定位**：F406 只能累计支配 load 的 store，无法证明“每个分支只初始化该分支
  实际选择对象”的安全程序。F407 是 MemoryPhi-inspired 有界 guard-tree 证明，不构造完整
  LLVM MemorySSA，不外推为一般 clobber analysis 或完整 POSE；
- **生产者证明**：F406 失败后，以 merge predecessors 的最近公共支配点为 root，按 true/
  false DFS 枚举完整无环路径。每条路径用 select condition 或 merge pointer-PHI edge tag
  选出唯一 ordinary local heap object，再逆向找 singleton pointer-domain、非atomic/volatile、
  静态区间完整覆盖的同路径 store；限制 64 paths、64 blocks/path、depth 8 和 4096 referenced
  blocks，至少两个不同 stores/bases；
- **proof-carrying 合同**：load 同时输出 F406 `initialization_bases` 与
  `symcc-guarded-heap-union-initialization-v1` transcript，后者包含 root/merge/depth、规范路径
  blocks/decisions/predecessor、base/load address 和 store block/ordinal/address/bytes；声明
  `bounded-guard-correlated-heap-union-initialization` 并依赖 heap lifetime、pointer union 与 F406；
- **consumer 与 runtime**：consumer 独立重枚举 artifact CFG，透明穿越 PHI edge-copy block，
  重算 select/PHI guards、heap owner、allocation identity、store ordinal/interval，并要求 path
  base 集合与 F406 证书精确相等。runtime 不消费证书来设置初始化位，真实 store、live/size/
  byte-init marker 仍是最终权威，program root 内容寻址保持 fresh-process resume 一致；
- **review 修复**：nested alias guard 从立即 unknown 改为“先收集 unresolved，若外层 guard
  后续排除 candidate 则忽略”；64 global-block 限制改为 64×64 引用上界并加入真实64叶 fixture；
  producer/consumer 对重复 condition 冲突共同失败关闭；load/store allocation identity guard
  从“存在则校验”收紧为“必须存在且精确”；F406 tamper 在剥离 F407 合同后独立验证；
- **验证与边界**：F407封存时LLVM17/18 select、PHI、四叶nested、64路径和四类source
  negative通过；F408复核后重复condition用例因所有真实MemoryPhi edge均有store而由F408
  接纳，当前保留三类F407 source negative，历史证据不改写；
  独立 oracle 的 48 trees、1008 assignments/interval checks、missing/mismatch/partial 各1008组
  及10类 artifact mutations 全部符合预期；完整 Python 1041 passed 加250 subtests，LLVM17
  264 passed 加2既有unsupported。64路径 Python 参考证明中位15214 ns/proof，64→1只表示
  分析态基数；没有公共coverage、solver throughput、bug-yield或端到端speedup结论。详见
  [`F407研究报告`](research-progress/Guard_Correlated_Heap_Union_Initialization_F407_2026-08-16.md)
  与[`F407证据`](evidence/f407-guard-correlated-heap-union-initialization-2026-08-16/)。

## 403. F408：LLVM MemorySSA/AA Bounded Heap Initialization（2026-08-16）

- **缺口与定位**：F407从CFG重建二叉guard tree，不能一般表达switch、多层MemoryPhi或
  terminal store之前的无关MemoryDef。F408接入真实LLVM `MemorySSAAnalysis`与
  `AAManager`，但严格限定为函数内、有界、静态heap interval切片，不宣称一般clobber
  serialization或完整POSE；
- **分析生命周期**：新PM先统一执行scalar mem2reg；IR改变后逐函数invalidate FAM，再懒
  获取AA/MemorySSA并注入`FunctionContext`。未请求export时保持分析，legacy PM无可靠注入
  时传null并失败关闭，避免stale analysis；
- **生产者证明**：从load `MemoryUse`的defining access递归展开`MemoryPhi/MemoryDef`；限制
  128 nodes、64 incoming、depth 8、每terminal 64 skips。terminal simple store须singleton
  静态pointer、同allocation identity、完整区间覆盖并支配incoming点。其他store仅静态对象/
  区间不交或AA `NoAlias`时跳过；仅可一一映射的内部直接call可在ModRef不含`Mod`时输出
  `aa-no-modref`；其余unknown、liveOnEntry、cycle、partial、MayAlias全部拒绝；
- **pointer/memory相关性**：多对象domain要求root MemoryPhi位于load merge，且使用同一
  pointer-PHI edge discriminator把每个incoming绑定到唯一base；全部alias alternatives和
  witness bases必须精确覆盖，单对象多definition也受支持；
- **proof-carrying合同**：load增加`initialization_memoryssa`，schema为
  `symcc-memoryssa-aa-heap-initialization-v1`，记录merge/load width/root、forward-ID nodes、
  phi incoming、terminal store与skipped definitions；capability为
  `bounded-memoryssa-aa-heap-initialization`；
- **consumer与runtime**：consumer重建branch/jump和PHI edge-copy logical predecessors，
  验证连续ID、forward edges、全root可达、owner、store ordinal/interval/allocation guard、
  skipped store以及按memory-definition ordinal定位的真实lowered call。LLVM负责AA数学事实，
  consumer负责artifact身份闭合；runtime真实live/size/byte-init marker仍是最终权威；
- **review修复**：修正mem2reg后分析失效、NoModRef自报kind可冒名、独立oracle mutation只
  计标签、F407重复condition旧负例误判四项问题；生产者限制call支持域并增加ordinal/kind
  篡改，oracle现真正构造并变异DAG；producer还显式要求merge-root MemoryPhi，与consumer
  的三节点/root-phi合同对齐；consumer对同对象skipped store由owner-only修正为真实width/
  半开区间重算，并新增offset 0/1双LLVM正例；
- **验证与边界**：LLVM17/18 switch、single-object、nested、NoModRef call、missing/partial
  负例及64-way生产边界通过。独立oracle接纳504 graphs，执行16632个incoming/interval/
  NoAlias checks，missing/partial/mismatch各16632拒绝，8类graph mutation拒绝；完整Python
  1041 passed加250 subtests，LLVM17 265 passed加2既有unsupported。64路机制基准仅为
  Python参考成本和64→1分析态基数，无公共coverage、solver throughput、bug-yield或
  end-to-end speedup结论。详见
  [`F408研究报告`](research-progress/MemorySSA_AA_Heap_Initialization_F408_2026-08-16.md)、
  [`F408示意图`](diagrams/memoryssa-aa-heap-initialization-f408.svg)与
  [`F408证据`](evidence/f408-memoryssa-aa-heap-initialization-2026-08-16/)。

## 404. F409：Callsite-Instantiated Interprocedural Heap Effect Summary（2026-08-16）

- **研究缺口与定位**：LLVM MemorySSA是函数内分析，F408无法把allocator wrapper或参数
  initializer中的callee写入证明连接到caller load。F409借鉴SMART与demand-driven
  compositional execution的callsite实例化原则，但只实现可审计的有限heap interval切片，
  不宣称完整函数摘要、递归fixed point或完整POSE；
- **生产者证明**：caller仍从真实load MemoryUse沿线性MemoryDef链逆行，静态区间/对象不交
  或LLVM NoAlias才跳过store，NoModRef才跳过直接内部call。terminal call仅接受两类单块、
  唯一return摘要：返回唯一malloc/calloc allocation，或用唯一simple store写
  `formal_parameter + constant_offset`。后者在当前actual heap object上实例化并要求store半开
  区间完整覆盖load；条件、多writer、递归、间接调用、dynamic interval和EH失败关闭；
- **证明携带合同**：成功load声明`bounded-interprocedural-heap-effect-summary`并输出
  `symcc-interprocedural-heap-effect-summary-v1`。transcript绑定kind、callee、caller call、
  actual/formal、heap allocation、store/return identity、load/store interval和全部skipped defs；
  returned finite heap pool按对象输出证书集合，并与load/allocator/store alias域精确闭合；
  64个callsite产生64个各自绑定actual的证书，不把callee全局pointer union与caller object做
  4096项笛卡尔匹配；
- **consumer与runtime**：consumer重建caller/callee操作身份和顺序、call argument/parameter、
  allocation owner、store宽度/区间、唯一writer及skip witness；LLVM仍负责AA/ModRef数学事实。
  transcript不设置initialized状态，runtime真实live/size/byte-init bitmap仍是最终权威；
- **验证与边界**：LLVM17/18覆盖wrapper 42、initializer 77、同对象不相交skip、双callsite
  154、subobject 99、2-slot returned pool、64-callsite边界及三类source negative；独立oracle穷举182196个interval
  实例，接受49896个合法cover并拒绝132300个non-cover、49896个wrong-callsite及8类artifact
  mutation。64-callsite机制基准的4096→64只表示关系检查基数；816 ns/certificate和
  52226581 ns batch只量化Python参考validator，不是LLVM成本、coverage或端到端speedup。
  完整门禁为Python 1046 passed加250 subtests、LLVM17 267 passed加2既有unsupported。详见
  [`F409研究报告`](research-progress/Interprocedural_Heap_Effect_Summary_F409_2026-08-16.md)、
  [`F409示意图`](diagrams/interprocedural-heap-effect-summary-f409.svg)与
  [`F409证据`](evidence/f409-interprocedural-heap-effect-summary-2026-08-16/)。

## 405. F410：Bounded Symbolic-Length Dynamic Byte-Lane Cover（2026-08-16）

- **研究缺口与定位**：F408/F409只证明静态load/store interval，不能处理
  `memset/memcpy/memmove(..., symbolic_length)`之后的dynamic-index宽load。F410借鉴
  bounded symbolic-size与MInt的memory-aware effect思想，实现有界、证明携带式适配；不宣称
  完整lazy tuple memory model或一般符号内存；
- **区域效应生产**：从pointer provenance计算最多64-byte容量，必要时加入`ule(length,C)`
  assume；对每个offset生成`ult(offset,length)`，将区域操作lower为零状态分叉的guarded byte
  reads/writes。copy/move先读完全部source bytes，保持memmove snapshot语义；
- **动态lane证明**：从动态load真实MemoryUse沿线性MemoryDef链逆行至唯一region writer；
  load alias域最多256地址，`address x lane`最多2048 witness。每个lane必须映射到writer半开区间，
  中间scalar store仅静态不交或LLVM NoAlias时跳过；MemoryPhi、未知call、partial cover、多region
  effect、动态writer base均失败关闭；
- **proof-carrying合同**：区域bound/guard/read/write使用四种v1 schema，load输出
  `symcc-symbolic-length-byte-lane-cover-v1`，绑定writer site/kind/length/capacity、stack/heap owner、
  index exact domain、load addresses、完整lane Cartesian product和skip witnesses；
- **consumer与runtime**：consumer按whole-function site重放offset顺序、guard identity、
  read-before-write、连续地址、CFG dominance、object owner与lane映射。LLVM producer仍负责
  MemorySSA/AA数学事实；runtime真实执行`I'=I OR guard`并在动态load合取每个lane初始化标记，
  证书从不直接把字节设置为initialized；
- **验证与边界**：LLVM17/18的22条RUN覆盖外部/intrinsic memset、memcpy、guarded heap、
  NoAlias skip、64-byte/64-alias/456-lane边界和三类source negative；独立oracle穷举48294 cases、
  364317 lane checks、102004 guard equivalences并拒绝10类mutation。完整Python 1051 passed加250
  subtests，LLVM17 269 passed加2既有unsupported。64-byte边界为65个bounded length values、
  64 conditional writes和0 continuation state forks；10309 ns/certificate只量化Python参考
  validator，不是LLVM成本或65倍加速，无公共coverage/solver/bug-yield/end-to-end结论。详见
  [`F410研究报告`](research-progress/Symbolic_Length_Byte_Lane_Cover_F410_2026-08-16.md)、
  [`F410示意图`](diagrams/symbolic-length-byte-lane-cover-f410.svg)与
  [`F410证据`](evidence/f410-symbolic-length-byte-lane-cover-2026-08-16/)。

## 406. F411：Loop-Carried MemoryPhi Byte-Lane Induction（2026-08-16）

- **研究缺口与定位**：F410 的 dynamic lane cover 只沿线性 MemoryDef 链回溯，遇到循环头
  MemoryPhi 即失败。F411 面向逐字节初始化循环，严格区分 MemorySSA 的 may-reach writer 与
  当前路径实际初始化；不把 backedge store 冒充为支配所有退出路径的必然写入；
- **LLVM producer**：load 的真实 MemoryUse 必须到达 header MemoryPhi；LoopInfo 证明单
  preheader、三块 loop、单 latch/exit且exit仅由header进入。loop内唯一scalar PHI必须是seed 0、
  step 1，额外PHI/call/alloca失败关闭；guard 必须为
  `ult(iv,input-derived bound)`，且 bound 源位宽可表示 writer exclusive upper bound。
  MemoryPhi 两条 incoming 精确绑定 liveOnEntry 与唯一一字节 writer MemoryDef；loop 内其他
  MemoryUse/Def 全部拒绝；
- **有限 lane 证明**：枚举同对象 writer/load alias 与 index 域，对每个 load address 和每个
  byte lane 输出唯一 `(writer_address, induction_value)` witness；上限为 256 aliases、2048 lanes，
  64-byte/8-byte 边界为 64 writer aliases、57 load aliases、456 witnesses；
- **严格 consumer/runtime**：consumer 重建 direct CFG predecessors、两条 scalar PHI edge-copy、
  `ult` guard、`add(iv,1)`、唯一 store、input/parameter bound provenance、owner、index 域和完整
  witness 笛卡尔积；整数合同排除bool，identity保持位宽，且最多一次zext必须严格扩宽。
  runtime byte-init 仍是当前路径权威；同时修复 alias 指令地址 digest 化为
  concrete 且落在声明域外时绕过域约束的问题，越界迭代现以 infeasible 终止，而域内 concrete
  地址仍保留对象生命周期与 logical-size 诊断；
- **验证与边界**：LLVM17/18 的 15-RUN fixture 覆盖动态 `i16` 正例、五类 source negative、
  六类 artifact mutation 与 64-byte producer boundary；独立 oracle 穷举 8019 cases、91773 lane
  checks、22154 runtime bitmap equivalences，并拒绝 12 类 mutation。完整门禁为 Python 1056
  passed 加 250 subtests、LLVM17 271 passed 加 2 个既有 unsupported；F407--F411 关联 lit 在
  LLVM17/18 各 5/5。64-byte Python reference validator 中位 51116 ns/certificate，仅描述证书
  检查成本；不形成 LLVM、solver、coverage、bug-yield 或 end-to-end speedup 结论。
  详见[`F411研究报告`](research-progress/Loop_MemoryPhi_Byte_Lane_Induction_F411_2026-08-16.md)、
  [`F411示意图`](diagrams/loop-memoryphi-byte-lane-induction-f411.svg)与
  [`F411证据`](evidence/f411-loop-memoryphi-byte-lane-induction-2026-08-16/)。

## 407. F412：Strided Loop MemoryPhi Residue-Class Byte-Lane Cover（2026-08-16）

- **研究缺口与定位**：F411仅支持seed 0、unit step、scale 1和一字节writer；直接放宽step会把
  store完整alias域中的不可达索引误当成循环writer。F412显式分离完整alias域与按
  `index mod step == 0`过滤的recurrence-reachable子域，并为每个load byte输出
  `(writer start, writer lane, induction value)`唯一见证；
- **算术与内存证明**：支持1--64的正向常量step、正向affine pointer scale和1--8 byte scalar
  writer，要求`writer_bytes <= step*scale`以排除重叠owner；producer与consumer都验证
  `bound_max <= 2^induction_bits-step`，窄位宽潜在回绕失败关闭。partial residue仅在load alias域
  完全落入覆盖类时接纳；
- **版本化合同与runtime**：广义情形输出
  `symcc-loop-memoryphi-byte-lane-induction-v2`和
  `bounded-strided-loop-memoryphi-byte-lane-induction`；consumer重建CFG/PHI、实际
  `pointer_offset`、完整/可达域、stride、residue与lane笛卡尔积，整数拒绝bool。静态证书不设置
  init，zero-trip/early-exit仍由路径局部byte bitmap裁决；
- **验证与边界**：LLVM17/18的15-RUN fixture覆盖step/scale/partial-residue正例，gap/overlap/
  wrap负例、8类扩展artifact mutation和64-byte边界；独立oracle枚举24744 cases，接纳2906、拒绝
  21838，复核199836 lanes、80404次bitmap等价及15类独立mutation。完整Python为1061 passed加
  250 subtests，node-ID digest为
  `79e009242ed5cdea20e39e914abd7267e512cd073b095d65521573004bda1d31`。64-byte边界为57个
  store aliases、8个可达writers、64个covered bytes和456个witnesses；95151 ns/certificate仅为
  Python参考validator成本，不形成LLVM、coverage、solver、bug-yield或端到端性能结论。
  详见[`F412研究报告`](research-progress/Strided_Loop_MemoryPhi_Residue_Cover_F412_2026-08-16.md)、
  [`F412示意图`](diagrams/strided-loop-memoryphi-residue-cover-f412.svg)与
  [`F412证据`](evidence/f412-strided-loop-memoryphi-residue-cover-2026-08-16/)。

## 408. F413：Conditional Loop MemoryPhi Guard-Carry Byte-Lane Certificate（2026-08-16）

- **研究缺口与定位**：F411/F412只接受每次进入body都执行的writer；条件store的header
  backedge实际来自latch MemoryPhi，它在writer MemoryDef与header MemoryPhi pass-through之间
  合并。MemoryPhi只表达may-reach，不能据此把guard-false、zero-trip或尚未到达writer iteration
  的字节标为initialized。F413闭合single-latch、single-layer conditional writer，不宣称一般
  loop invariant、数组摘要或多latch fixed point；
- **producer精确准入**：接受
  `header -> decision -> writer|skip -> latch -> header`五块loop。decision condition必须是本块
  integer `icmp`，两个operand同位宽且可lower；writer可位于true或false edge，polarity从真实
  successor恢复。header MemoryPhi必须合并liveOnEntry与latch MemoryPhi；latch MemoryPhi必须
  精确合并唯一writer MemoryDef和同一个header MemoryPhi。loop内其余MemoryUse/Def/Phi、call、
  alloca、多writer、raw-i1 guard与多latch失败关闭；
- **v3 guarded witness**：新增
  `bounded-conditional-loop-memoryphi-byte-lane-induction`与
  `symcc-loop-memoryphi-byte-lane-induction-v3`。artifact绑定decision/writer/skip/latch、两层
  MemoryPhi kind、guard variable/predicate/left/right/writer_when，以及每条
  `(load address,lane)->(writer start,writer lane,iv,guard==polarity)`。v3复用F412完整store alias、
  recurrence-reachable域、stride/residue、bound容量和whole-domain no-wrap；广义shape继续要求
  strided capability；
- **consumer与runtime闭合**：consumer独立重建direct CFG predecessor/successor和迭代block
  dominators，要求guard operand在decision前可用，实际comparison、branch edge、store ordinal、
  pointer_offset、owner和lane笛卡尔积与证书精确一致。14类actual artifact mutation覆盖缺
  capability、polarity/predicate/target/witness/MemoryPhi/operand篡改、skip store、late operand及
  异宽/越界常量operand、transcript删除，均在executor create前拒绝。证书从不设置init；runtime仅在真实writer edge
  store时更新path-local byte bitmap；
- **验证结果**：LLVM17/18的15-RUN focused各1/1，F411--F413关联各3/3；true-edge正例输入
  `000201`返回7个75并产生infeasible states，false-edge与conditional strided i16正例通过，
  two-writer/raw-i1/skip-store源负例失败关闭。完整LLVM17为275 passed加2既有unsupported，
  Python exact gate为1066 passed加250 subtests且身份无漂移。独立oracle枚举126624 cases，接纳
  20640、拒绝105984，复核768672 lanes、551712次runtime bitmap等价并拒绝18类独立mutation；
- **机制边界**：64-byte/stride8/writer8/load8证书含57完整aliases、8 reachable writers、64
  potential writer bytes、57 load aliases、456 guarded witnesses、2个MemoryPhi和4条incoming。
  Python参考validator中位149259 ns/certificate只描述证书校验成本；不形成LLVM latency、
  solver throughput、coverage、bug-yield或端到端speedup结论。该multi-latch MemoryPhi
  SCC fixed-point缺口已由F414关闭。详见
  [`F413研究报告`](research-progress/Conditional_Loop_MemoryPhi_Guard_Carry_F413_2026-08-16.md)、
  [`F413示意图`](diagrams/conditional-loop-memoryphi-guard-carry-f413.svg)与
  [`F413证据`](evidence/f413-conditional-loop-memoryphi-guard-carry-2026-08-16/)。

## 409. F414：Multi-Latch MemoryPhi SCC Finite Fixed-Point Certificate（2026-08-16）

- **研究缺口与语义**：LLVM自然循环允许多条latch/backedge；header MemoryPhi的incoming是
  may-reach alternatives，不能顺序复合成“一轮执行所有writer”。F414接受2--4条互斥backedge，
  建立`H=phi(liveOnEntry,T0(H),...,Tn(H))`，其中writer transfer单调加入潜在byte lanes，carry
  transfer保持`H`。有限recurrence域上逐轮union并显式输出`+0 lane`稳定轮；静态closure只证明
  potential provenance，runtime真实path-local byte-init bitmap仍决定当前路径是否可读；
- **producer严格准入**：从exit load的真实MemoryUse绑定header MemoryPhi，用LoopInfo验证单
  preheader/header/exit与2--4 direct latches；header true edge须是一棵无join/side-entry、最多3个
  integer-icmp节点的二叉树。所有latch共享seed 0与同一`add(iv,step)`，step为1--64；MemoryPhi
  incoming逐block绑定liveOnEntry、直接writer MemoryDef或header carry，至少一个writer，且loop内
  不允许未转录的MemorySSA effect。guard operand递归限制为bounded参数、非负常量、cast、binary/
  icmp、select和defined freeze，拒绝load/call、pointer provenance与nondeterministic freeze；
- **affine lane不动点**：每个writer是同一stack/heap object上的唯一simple store，宽1--8 byte、
  正scale且`writer_width <= step*scale`。producer保留完整aliases与recurrence-reachable aliases，
  验证bound容量、whole-domain no-wrap、地址/索引有序性；按induction domain计算round new/total、
  final lanes和stability，并为每个load alias×lane列出全部
  `(transfer,writer_address,writer_lane,induction_value)` alternatives。load lane上限2048，全部
  alternatives上限8192；不同writer可重叠，因为它们互斥且证书不总结值；
- **proof-carrying消费合同**：新增
  `bounded-multilatch-loop-memoryphi-byte-lane-fixed-point`与
  `symcc-loop-memoryphi-byte-lane-induction-v4`。consumer从lowered blocks独立重建CFG、true-first
  decision tree、dominator、PHI edge-copy、comparison定义与operand位宽/来源，绑定MemoryPhi
  incoming ordinal、唯一store、pointer_offset、owner、alias/domain/no-wrap，并重新计算全部fixed
  point rounds、final lanes与witness alternatives。capability/use-point/dangling capability四向闭合；
  bool-as-int、同步篡改和缺失transcript均在executor创建前失败关闭；
- **测试与独立验证**：LLVM17/18的17-RUN fixture覆盖writer/carry、双writer、3/4-latch tree、
  step2/i16、64-byte producer边界及different-step/carry-def/nondeterministic-guard source negatives；
  专属checker拒绝17类actual artifact mutation，共享base/strided checker另覆盖6+8类。独立oracle
  枚举42208 cases，接纳6880个fixed points、拒绝35328个unsupported/partial，复核256224 lanes、
  633552次transfer-equation/runtime-bitmap等价（345284 writer-path接受、288268 carry/prefix拒绝）
  并拒绝18/18 reference mutations。完整Python为1071 passed加250 subtests且规范node-ID无漂移；
  LLVM17全量为277 passed加2个既有unsupported、0 failed；
- **机制边界**：64-byte、stride/writer/load均8 byte、四transfer（2 writer+2 carry）证书含每writer
  57 aliases/8 reachable writers、128 potential writer bytes、57 load aliases、456 witnesses、912
  alternatives、3 decisions、4 backedges、1个MemoryPhi/5 incoming和8+1 rounds。11×1000 Python
  reference validation批次最小/中位/最大为103406795/104156971/111543685 ns，中位104156
  ns/certificate；只描述Python参考验证成本，不形成LLVM latency、solver、coverage、bug-yield或
  端到端speedup结论。每latch有序multiwriter已由F415关闭；后续仍需nested-loop composition、value/last-write、
  pointer-union、多对象、非树状CFG与跨过程循环摘要。详见
  [`F414研究报告`](research-progress/Multi_Latch_MemoryPhi_Fixed_Point_F414_2026-08-16.md)、
  [`F414示意图`](diagrams/multilatch-memoryphi-fixed-point-f414.svg)与
  [`F414证据`](evidence/f414-multilatch-memoryphi-fixed-point-2026-08-16/)。

## 410. F415：Ordered Multi-Writer Backedge Transfer Certificate（2026-08-16）

- **研究缺口与语义**：F414每条writer backedge仅接受一个直接MemoryDef，不能表示同一latch内
  `H -> W0 -> W1 -> ... -> H`的真实定义链。F415保留不同backedge互斥、同一backedge内writer
  顺序执行的两层语义；initializedness仍取byte union，但v5逐alternative携带writer ordinal，
  为后续last-write/value和nested-loop composition保留序列身份；
- **producer准入与兼容**：沿header MemoryPhi incoming的MemoryDef链逆行至同一header，逐节点
  要求同latch simple store，再反转并用`comesBefore`复核程序顺序。每transfer 1--4 writer、全loop
  最多16；每writer独立验证width/affine scale/aliases/reachable recurrence/owner，并拒绝链外任何
  MemorySSA effect。至少一条链长度大于1时输出
  `symcc-loop-memoryphi-byte-lane-induction-v5`和
  `bounded-multilatch-loop-memoryphi-ordered-writer-transfer`；全singleton保持v4字节兼容；
- **严格consumer**：从lowered latch真实store列表按ordinal绑定每个writer的width、pointer_offset、
  alias/index/reachable域，要求v5 MemoryPhi kind为`ordered-writer-memory-def-chain`，重新计算全部
  fixed-point rounds与含`transfer+writer`的完整witness alternatives。每transfer/全局计数、至少一条
  multiwriter链、capability依赖/use-point/dangling capability均失败关闭；runtime byte-init仍仅由
  实际选择的transfer及其真实store更新；
- **测试与独立验证**：LLVM17/18的9-RUN fixture覆盖可区分1/2-byte顺序、multiwriter+singleton
  混合、5-writer source negative及4 latch×4 writer=16边界；专属checker对可区分正例拒绝13类
  actual artifact mutation。独立oracle枚举13632 cases，接纳3312、拒绝10320，复核72576 lanes、
  231282次runtime bitmap等价、718968个last-writer provenance单元，并拒绝22类reference mutation；
  完整Python为1076 passed加250 subtests且身份无漂移，LLVM17全量为279 passed加2 unsupported；
- **机制边界**：64-byte、两writer/two-carry transfer、每writer transfer宽度序列`[8,7,6,5]`的
  证书含8个writer records、64 reachable instances、416 potential bytes、456 witnesses与2964
  alternatives。11×1000 Python reference validation最小/中位/最大为
  210741732/211515028/222742650 ns，中位211515 ns/certificate；只描述参考validator与artifact
  基数，不形成LLVM、solver、coverage、bug-yield或端到端性能结论。详见
  [`F415研究报告`](research-progress/Ordered_MultiWriter_MemoryPhi_Transfer_F415_2026-08-16.md)、
  [`F415示意图`](diagrams/ordered-multilatch-writer-transfer-f415.svg)与
  [`F415证据`](evidence/f415-ordered-multilatch-writer-transfer-2026-08-16/)。

## 411. F416：Nested-Loop MemoryPhi Summary Composition（2026-08-16）

- **研究缺口与语义**：F411--F415只能在一个loop层级内解释目标load。F416绑定真实两层
  LoopInfo与MemorySSA，建立`H_outer=phi(entry,S_inner(H_outer))`；先按inner induction和有序
  MemoryDef链构造byte-lane effect，再作为outer backedge摘要。initializedness union利用幂等性
  避免outer×inner静态展开，但static summary不设置init，zero-trip/短prefix仍由runtime bitmap裁决；
- **producer与v6合同**：严格接纳两层、各single-header/single-latch的可归约shape，inner bound对
  outer invariant；验证两组seed/step/ult/bound/no-wrap、outer Phi指向inner Phi、inner Phi指向同
  body的1--4个有序simple stores，并排除未转录MemorySSA effect。输出
  `symcc-loop-memoryphi-byte-lane-induction-v6`和独立
  `bounded-nested-loop-memoryphi-summary-composition`，不错误依赖multi-latch capability；
- **严格consumer**：独立重建两层CFG/predecessor、四个PHI edge-copy、两组recurrence/guard和bound
  dominance，绑定两个MemoryPhi方程、真实store ordinal、pointer/owner/alias/reachable域和目标load
  地址公式，重新计算fixed point与全部witness。整数拒绝bool，capability dependency/use-point/
  dangling、preheader/load前memory effect及同步IR/transcript篡改均失败关闭；
- **测试与独立验证**：LLVM17/18的14-RUN focused各1/1，F411--F416关联各6/6，关联Python30/30；
  完整Python为1081 passed加250 subtests，LLVM17为281 passed加2 unsupported。独立oracle枚举
  18240 cases，接纳670、拒绝17570，复核21480次runtime/composed bitmap等价、52662个last-writer
  provenance单元并拒绝26类reference mutation；两次oracle SHA-256均为
  `228f5cb0a98e95f1ef523396ea71455e249368dfce230a6879bf9421138586a9`；
- **机制边界**：64-byte、inner step8、writer widths `[1,2,4,8]`、load8的真实v6产物含2个Phi、
  4 writers、32 reachable instances、456 witnesses、855 alternatives和9 rounds。11×1000 Python
  reference validation最小/中位/最大为96371593/97062772/103855714 ns，中位97062 ns/certificate；
  不形成LLVM、solver、coverage、bug-yield或端到端性能结论。F416是受LoopSCC从内到外方向启发的
  项目局部initializedness适配，不是一般LoopSCC复现。详见
  [`F416研究报告`](research-progress/Nested_Loop_MemoryPhi_Summary_Composition_F416_2026-08-16.md)、
  [`F416示意图`](diagrams/nested-loop-memoryphi-summary-f416.svg)与
  [`F416证据`](evidence/f416-nested-loop-memoryphi-summary-2026-08-16/)。

## 412. F417：Nested-Loop MemoryPhi Last-Write Value Summary（2026-08-16）

- **研究缺口与精确语义**：F416只证明initialized byte union，不能选择重叠store的最终值。F417
  对每个load lane将潜在来源按`inner induction降序、同轮writer ordinal降序`排序，每个case携带
  `minimum_inner_bound=k+1`与端序相关`value_byte`；只有outer bound大于0时选择第一个满足inner
  bound的case，无命中保持uninitialized而不是数值0；
- **producer与渐进回退**：在F416真实LoopInfo/双MemoryPhi/ordered MemoryDef/affine alias证明上，
  仅当1--4个writer的真实value operand全部为完整字节宽度`ConstantInt`时，按LLVM DataLayout展开
  1--8 byte memory vector，输出`v7`和
  `bounded-nested-loop-memoryphi-last-write-value-summary`。任一动态writer保留v6 initializedness
  证书且不声明F417，避免半可信值推断破坏已有支持域；`i1`等非整字节表示不臆测padding bits，
  producer拒绝摘要并由strict lowering失败关闭；
- **严格consumer**：从lowered inner-body store按ordinal重新读取`{const,bits}`，独立按program
  endianness重算bytes，再从真实writer address/width/reachable domain重建、排序全部last-write
  cases并重算minimum/value；schema/key/resource、bool-as-int、capability dependency/use-point/
  dangling、实际store与transcript同步篡改全部在执行前失败关闭；
- **测试与独立验证**：LLVM17/18 15-RUN focused各2/2，覆盖动态值v6回退、非整字节失败关闭和
  真实大端模块；F411--F417关联各8/8，关联Python35/35，完整Python为1086 passed加250 subtests；
  独立oracle枚举3456组合，接纳496、拒绝2960，复核16416组runtime load-vector和62112个已定义
  value bytes，统计43212完整/57572未初始化或partial loads，并拒绝18类mutation；两次oracle
  SHA-256均为`2c77fa549fae8e37202c97250c305e793f2b6881bd35d08f3a10b5b8409687a9`；
- **机制边界**：64-byte证书有4 value records、15 stored bytes、456 witnesses、855 last-write
  cases、每lane最多4 cases。11×1000参考基准中validation中位360090 ns/certificate，first-match
  selection中位97304 ns/query；仅描述Python参考机制，不形成LLVM、solver、coverage、bug-yield或
  端到端speedup结论。当前runtime仍执行真实循环且byte-init bitmap保持具体路径权威；符号writer
  value、outer-dependent二维地址、nested conditional/multi-latch、pointer union、多对象和一般
  LoopSCC仍在后续域。详见
  [`F417研究报告`](research-progress/Nested_Loop_MemoryPhi_Last_Write_Value_Summary_F417_2026-08-16.md)、
  [`F417示意图`](diagrams/nested-loop-memoryphi-value-summary-f417.svg)与
  [`F417证据`](evidence/f417-nested-loop-memoryphi-value-summary-2026-08-16/)。

## 413. F418：Nested-Loop MemoryPhi Two-Dimensional Affine Summary（2026-08-16）

- **研究缺口与精确语义**：F417假定writer address只依赖inner induction，因此outer只决定摘要是否
  激活。F418支持`address=base+p*(c+alpha*outer+beta*inner)`，在窄整数bound的完整最大域枚举
  `(outer,inner,index,address)`实例；每个load lane按outer、inner、writer ordinal依次倒序，case同时
  携带`minimum_outer_bound=o+1`、`minimum_inner_bound=i+1`和target-endian value byte，无命中保持
  uninitialized；
- **producer与v8合同**：从真实两层LoopInfo/双MemoryPhi/ordered MemoryDef/alias证明出发，只接受
  constant/add/multiply-by-direct-positive-constant、深度不超过4的二维affine树；两维最大实例各不超过
  64、乘积不超过256，所有index无回绕并必须命中实际alias map。1--4个writer均须为1--8 byte完整
  `ConstantInt`，width不超过有效inner地址stride；输出v8、二维pair fixed point与独立capability。
  dynamic value、`outer*inner`、混合direct/2D、越界或超预算失败关闭；
- **严格consumer与review修复**：consumer从lowered ABI恢复窄bound域，从actual binary definition
  tree独立归一化`c/alpha/beta`，绑定pointer base/scale、store aliases与每个instance，重算端序bytes、
  closure rounds、双threshold和全部witness。专属checker破坏capability/schema/系数/instance/pair/
  threshold/actual expression/pointer均须拒绝。review修复v8在`inner_step=1,width=1`但`beta>1`或
  pointer scale>1时漏发strided capability的问题，并加入隔离边界；
- **测试与独立验证**：LLVM17/18 focused各3/3，共17条RUN，含真实big-endian返回`0xAA34`、24-byte
  12-pair生成边界、affine-stride隔离及dynamic/non-affine source negatives；F416--F418关联LLVM
  两版本各6/6、关联Python 15/15；完整Python为1091 passed加250 subtests，LLVM17受控完整门禁为
  288 passed加2既有unsupported。首次192-worker完整LLVM运行的既有QF_BV campaign资源敏感失败、
  隔离1/1通过及8-worker完整零失败均保留原始日志。Python F418为5/5。
  独立oracle枚举4608组合，接纳28、拒绝4580，复核608组runtime load-vector和3384个defined bytes，
  统计1692完整/5252未初始化或partial loads，拒绝18类mutation；两次输出SHA-256均为
  `7be3e10a9aead4949038be68c0bd5a4b82639156fa5c64f55df9494d2d1ddd4e`；
- **机制边界**：参考证书含2个writer value records、24个instance records、12个fixed-point pairs、
  46个witnesses和69个last-write cases；reference validation/selection中位分别53528 ns/certificate与
  13745 ns/query，仅描述Python机制，不形成LLVM、solver、coverage、bug-yield或端到端性能结论。
  runtime仍执行真实循环且path-local bitmap权威；一般Polly/ISL、LoopSCC、多分支、多对象、符号value
  和loop skipping未宣称。详见
  [`F418研究报告`](research-progress/Nested_Loop_MemoryPhi_Two_Dimensional_Affine_Summary_F418_2026-08-16.md)、
  [`F418示意图`](diagrams/nested-loop-memoryphi-two-dimensional-affine-f418.svg)与
  [`F418证据`](evidence/f418-nested-loop-memoryphi-two-dimensional-affine-2026-08-16/)。

## 414. F419：Nested-Loop MemoryPhi Affine Symbolic Value Summary（2026-08-17）

- **研究缺口与精确语义**：F418可以枚举`base+p*(c+a_o*outer+a_i*inner)`二维writer地址，
  但要求全部store value为常量。F419接纳
  `V=c_v+s_o*outer+s_i*inner+s_x*input mod 2^bits`，并把每个last-write case中的IV项特化为
  固定位宽常量，保留至多一个入口输入项，再按DataLayout端序提取目标byte。case继续按outer、
  inner、writer ordinal倒序，用双minimum-bound threshold激活；无命中保持uninitialized；
- **producer与v9合同**：在F418完整二维地址、双MemoryPhi、ordered store和alias证明上，从真实store
  SSA递归归一化同位宽ConstantInt、outer IV、inner IV、一个entry integer Argument、add及一侧为
  直接正常量的mul。深度上限4，定义必须在inner body中且位于store之前，所有记录系数不超过
  `INT64_MAX`；`nuw/nsw`、sub、多个输入、跨块/过深DAG失败关闭。producer记录argument SSA及
  input offset/bytes，输出v9和独立capability；
- **严格consumer与review**：consumer从lowered store definition tree独立恢复四个系数，从entry
  block重建`input -> identity/zext -> shl -> or -> identity`小端参数拼接，要求offset连续且唯一；
  再重算F418 instances、case顺序、模`2^bits`特化常量、input scale和端序low-bit。checker破坏
  capability/schema/系数/semantics/input variable/offset/bytes/witness/actual SSA/actual input ABI均须
  拒绝。review还把F418“直接动态参数必须拒绝”的过时负例改为真正超出新语法的`xor(input,1)`；
- **测试与独立验证**：LLVM17/18 focused各3/3，主fixture含16条RUN并覆盖i16/i32/i64、纯IV、
  输入、溢出、constant overlay和真实big-endian；F416--F419关联各9/9。Python F419 6/6、关联
  21/21；完整Python为1097 passed加250 subtests，node-ID摘要
  `0879d6e20ef492b553b2929a587f7bf3d9af9c684a45196686a65d86f51fe07d`；LLVM17受控八worker完整
  为292 passed加2既有unsupported。独立oracle枚举1152组合，接纳6、拒绝1146，复核384组
  runtime load-vector、1392个defined bytes，统计696完整/2504 partial loads并拒绝18类mutation；
  两次输出SHA-256均为`fae5d9ccaaa58ed3735bfce1ca83885cb6f4d3b67087e0e3b636e196097dfa04`；
- **机制边界**：参考证书含2个writer value records、24个instances、46个witnesses、69个cases和
  46个symbolic byte expressions。9x500 Python reference validation/selection中位43406 ns/
  certificate和10863 ns/query，仅描述参考机制，不形成LLVM、solver、coverage、bug-yield或端到端
  性能结论。runtime仍执行真实循环/store且path-local bitmap权威；一般SCEV/Polly/ISL、piecewise
  affine、多输入、多对象、跨过程value summary和loop skipping均未宣称。详见
  [`F419研究报告`](research-progress/Nested_Loop_MemoryPhi_Affine_Symbolic_Value_Summary_F419_2026-08-17.md)、
  [`F419示意图`](diagrams/nested-loop-memoryphi-affine-symbolic-value-f419.svg)与
  [`F419证据`](evidence/f419-nested-loop-memoryphi-affine-symbolic-value-2026-08-17/)。

## 415. F420：Nested-Loop MemoryPhi Guard-Specialized Piecewise-Affine Value Summary（2026-08-17）

- **研究缺口与语义**：F419只能给每个二维writer instance绑定一个affine bit-vector公式；F420
  接纳`select(icmp P IV,C),V_true,V_false`。`P`限于`eq/ne/ugt/uge/ult/ule`，IV精确绑定outer或
  inner induction，常量可位于任一侧。两臂各满足F419的8--64 bit affine grammar，合计至多共享一个
  入口输入。producer对F418有限`(outer,inner)`实例求guard、选择唯一臂、按`2^bits`特化IV项并生成
  target-endian byte expression；无命中仍为uninitialized；
- **v10与严格重建**：schema升级为`...-v10`并加入
  `bounded-nested-loop-memoryphi-piecewise-affine-value-summary`。consumer不信任metadata，而从
  lowered store的实际select/icmp、predicate、operand方向、IV、常量、双臂SSA和input ABI重建公式，
  再逐instance复算`guard_result/selected_arm/value`、双threshold、case顺序和witness。纯v9证书即使
  篡改schema/capability也不能伪升级；两臂按位宽模归一化后等价时失败关闭，修复了i8
  `256*x`与`0`被误判不同的review缺陷；
- **安全与运行时边界**：仅接纳inner body中`icmp -> select -> store`的直接有序定义；signed、
  input-dependent、nested/branch guard，不同入口输入，`nuw/nsw`、unsupported opcode、跨块/过深DAG
  均拒绝。不进行loop skipping、memory prefill、select/store替换；真实循环与path-local byte-init
  bitmap继续裁决zero-trip、短prefix和partial load；
- **验证结果**：LLVM17/18 focused各3/3，F416--F420关联各12/12；Python F420 6/6、关联27/27，
  完整门禁1103 passed加250 subtests；LLVM17完整296 passed加2既有unsupported。有限oracle覆盖96
  配置，接纳48、拒绝48，复核6144组runtime load-vector、58752个defined bytes、29376完整和111936
  partial/uninitialized loads，并拒绝22类mutation；两次输出SHA-256均为
  `c6d13d7149e38ad67e09726746f466583735d9870c2d6205010d4c7fe64a631b`；
- **机制成本与结论边界**：参考证书含24 instances、46 witnesses、69 cases和46 guard-specialized
  expressions；Python reference validation/selection中位为50978/10814 ns。该数字只描述证书重建与
  有限guard选择，不形成LLVM分析、executor吞吐、SMT、coverage、defect yield或端到端speedup结论。
  完整说明、图与封存原始结果见
  [`F420研究报告`](research-progress/Nested_Loop_MemoryPhi_Piecewise_Affine_Value_Summary_F420_2026-08-17.md)、
  [`F420示意图`](diagrams/nested-loop-memoryphi-piecewise-affine-value-f420.svg)与
  [`F420证据`](evidence/f420-nested-loop-memoryphi-piecewise-affine-value-2026-08-17/)。

## 416. F421：Nested-Loop MemoryPhi Affine Decision DAG Value Summary（2026-08-17）

- **研究缺口与接纳域**：F420只能表示一个直接induction guard。F421接纳至少2个guard组成的
  共享Decision DAG：guard限于`eq/ne/ugt/uge/ult/ule`及outer/inner IV与非负同位宽常量，leaf复用
  F419的`c+s_o*o+s_i*i+s_x*x mod 2^bits`。DAG最多31节点、guard深度最多4，相同LLVM SSA operand
  仅编码一次；input-dependent/signed/cross-block guard、超预算拓扑和unsupported leaf失败关闭；
- **producer与v11合同**：producer递归下降store operand，按child-before-parent后序编号并缓存
  `(node_id,subtree_depth)`，阻止cycle和共享非叶子深度低估。对每个F418有限writer instance实际求
  predicate、记录root-to-leaf path并生成target-endian specialized byte expression。v11可混合constant、
  single-affine、direct-piecewise和DAG writer，但至少一个writer必须是真实DAG；
- **strict consumer与review**：consumer从actual select/icmp、operand方向、instruction order、SSA leaf
  和entry input ABI重建DAG，复核连续ID、拓扑、唯一root、全可达、无重复operand、独立深度、leaf语义、
  instance path、byte extraction、last-write witness及capability closure。mutation battery破坏root/depth/
  child/predicate/leaf/witness/actual select/capability均在`create()`阶段拒绝。第二轮review修复共享非叶子
  深度缓存和DAG/direct-piecewise混合writer兼容性；
- **测试与独立验证**：LLVM17/18 focused各2/2、F416--F421关联各14/14；Python F421为5/5、
  F420--F421为11/11。独立oracle枚举1152配置，复核55296组load-vector、608256次scalar load、
  165888个defined byte、114048完整及494208 partial/uninitialized load，并拒绝6/6 unsupported shape；
  预期可复现SHA-256为`235b7fed1b5110e125d56fe04752edd0b92ad3003821cef31b195398a50fcf13`；
- **机制成本与可信边界**：9x500 Python reference的validation、guard selection、last-write中位为
  357/347/9511 ns每次，只度量有限DAG重建、专门化和查询，不形成LLVM、solver、coverage、缺陷发现或
  端到端speedup结论。F421仍执行真实outer/inner loop、select和store；可执行loop skipping由F422在
  effect/live-out/refinement与事务状态契约下单独实现。详见
  [`F421研究报告`](research-progress/Nested_Loop_MemoryPhi_Affine_Decision_DAG_Value_Summary_F421_2026-08-17.md)、
  [`F421 refinement契约`](research-progress/Executable_Loop_Summary_Refinement_Contract_F421_2026-08-17.md)、
  [`F421示意图`](diagrams/nested-loop-memoryphi-decision-dag-f421.svg)与
  [`F421证据`](evidence/f421-nested-loop-memoryphi-decision-dag-value-2026-08-17/)。

## 417. F422：Executable Nested-Loop Memory Summary Transfer（2026-08-17）

- **研究缺口与受限可执行域**：F421的v11 transcript能够证明两层循环的有限writer instance、
  Decision DAG value和last-write byte，但executor仍执行真实循环。F422只对固定stack object、完整
  store集合、无load/call/异常/外部effect、无scalar live-out的v11 loop安装preheader
  `loop_summary_transfer`；scalar live-out或任何未建模effect保留v11 proof但不生成executable
  capability，默认继续真实循环；
- **producer/consumer双端闭包**：producer从真实outer LoopInfo扫描actual store、effect和所有SSA user，
  绑定preheader/header/exit/source load并输出`closed-memory-only` proof。consumer不信任proof，重新执行
  v11 strict reconstruction，再从lowered CFG、实际store、operand use、stack object和DataLayout复算
  region、live-out、对象范围、store count与block顺序；对`(function,source_load)`强制唯一、全程序最多64
  个transfer，篡改已有`_compiled` cache同样失败关闭；
- **ITE transformer与事务语义**：consumer把每个静态instance编译为按
  `outer -> inner -> writer ordinal`排序的write program，activation为
  `(o<outer_bound)&&(i<inner_bound)`。runtime逐byte生成
  `ite(active,new,old)`，initializedness生成`old OR active`，在私有memory root/value map完成所有检查后
  一次提交并跳到exit。故障注入破坏最后一个write时，frames、solver root、values、memory root和搜索
  元数据均保持不变；
- **状态集合与coverage边界**：真实loop的多个符号bound exit state被合并为一个ITE state，正确关系是
  concretization集合等价而非solver-root结构相同。zero-trip/partial load保留initializedness定义域，
  不把backing zero当确定值。搜索图同时保留fallback和target可达性，但summary configuration gate不是
  程序branch，不产生CBC/CGS/path-cover token，也不写AFL edge/data bitmap；
- **验证结果**：独立源级oracle枚举11个合法load offset、4个outer bound和4个inner bound，共176配置；
  176次transfer全部命中、0 fork、最大27步，2,112/2,112 memory bytes、2,112/2,112 initializedness
  markers及33/33完全初始化load value等价；143个partial/uninitialized load只验证定义域。LLVM17/18
  正向与scalar-live-out fallback各2/2，Python定向3/3，strict mutation/transaction battery、ruff、
  py_compile及双版本构建通过；
- **结论边界**：该结果证明受限i2/i2、i16 Decision-DAG stack-memory transformer的机制正确性，不是
  一般LoopSCC、heap/multi-object、scalar recurrence、外部effect、solver时间、coverage或campaign
  speedup结论。详见
  [`F422研究报告`](research-progress/Executable_Nested_Loop_Memory_Summary_Transfer_F422_2026-08-17.md)、
  [`refinement契约`](research-progress/Executable_Loop_Summary_Refinement_Contract_F421_2026-08-17.md)、
  [`F422示意图`](diagrams/executable-nested-loop-memory-summary-transfer-f422.svg)与
  [`F422差分oracle`](../../benchmark/check_executable_loop_summary_transfer_oracles.py)。

## 418. F423：Executable Agolic Witness-Guided BSE Runner（2026-08-17）

- **缺口与执行语义**：F374已经给出跨运行plan、并行worker与串行replay合同，但
  `witness-guided`只是schema中的模式名。F423把它接入真实`LiveContinuationExecutor`：
  release前依据witness concrete value以单状态执行symbolic branch/throw/indirect call，追加route
  constraint但禁止fork和feasibility query；到达唯一release function或decision site后，保留
  memory、frame和solver-prefix并恢复普通solver-backed exploration；
- **精确求模与恢复合同**：terminal checkpoint从完整solver-frame链构造QF_BV模型，只接纳
  连续input byte domain内的numeric assignment。checkpoint持久绑定mode、release-config digest与
  prefix/released marker，不同释放点或执行模式恢复会失败关闭；step/state/wall预算与CLI
  `RLIMIT_AS`建立有界执行；
- **候选与coverage信任边界**：worker仅以SHA-256名称把候选持久到plan-private staging，
  不能写corpus或提交coverage。coordinator严格复核plan/result/program/release/resource身份后，
  从harness入口执行concrete-only replay；只有0 fork、0 solver、真实terminal的输入才以
  content-addressed no-replace协议发布到corpus。累计coverage由真实执行的function和
  `(branch site, T/F)`重建，再生成F374 planner兼容outcome；
- **验证与反例**：定向Python测试15/15，Agolic/live/persistent/exception/CBC/CGS/F422关联
  回归108 passed加36 subtests，`ruff`与`py_compile`通过。新增harness-entry入口探索、target function/op归属、witness二次身份、
  terminal checkpoint、result status、prior coverage和state-frontier bounded反例；完整Python门禁为
  1,126 passed加250 subtests，零skip/xfail/xpass/deselection/node-ID漂移。独立源级oracle枚举8个两字节配置：
  4个有效route全部release并产生8个replay-equivalent candidate，4个无效route全部零候选，
  target true/false集各为4，8/8 planner outcome一致，6类身份或证据篡改全部拒绝；
- **准确边界**：这是当前有界continuation IR支持域的可执行Agolic BSE mechanism，不是任意
  C/C++或native KLEE等价性，也没有复现论文的公开目标branch提升。详见
  [`F423研究报告`](research-progress/Executable_Agolic_Witness_Guided_BSE_Runner_F423_2026-08-17.md)、
  [`F423三泳道图`](diagrams/agolic/f423_executable_bse_runner.svg)与
  [`F423独立oracle`](../../benchmark/check_agolic_bse_runner_oracles.py)与
  [`F423证据`](evidence/f423-executable-agolic-bse-runner-2026-08-17/)。F423未改C/C++，本增量未重跑LLVM；
  紧邻F422封存的LLVM17/18全量结果分别为302+2和303+1 unsupported，不计作F423新实验。

## 419. F424：Selective Concolic Relation Graph 与概率 MDP（2026-08-17）

- **研究缺口**：F188/F189只按target dependency closure剥离完全不相连的input byte，无法表达
  `PC_r/PC_c`共享边界；原Prefix DAG的`mdp_value`也只是一次启发式backup。F424依据FM 2026
  Selective Concolic Testing的数据流，在当前QF_BV域实现共享边界的两阶段候选生成，并把调度更新
  改为Laplace转移概率与同步Bellman迭代；
- **加权图和受限切分**：每条assertion收集input byte和高成本BV operator，变量边权是原子共现次数；
  高成本原子进入`PC_r`，其余进入`PC_c`。切分必须两侧非空、random-only byte满足既有预算、witness
  完整、cut weight不超过总边权75%；assertion、全图变量、单原子变量、唯一边和pair observation
  分别限制为65536、4096、256、262144和1048576，任何超限均失败关闭到full Z3；
- **精确裁决**：独立solver先在总probe预算三分之一内解`PC_c`，再把partial model与witness/0/ff/
  确定性随机completion加入原完整solver。只有完整prefix+target返回SAT才发布candidate；partial
  UNSAT/unknown/timeout/miss/异常全部回退full Z3，从不写UNSAT cache或剪枝；QueryStore进一步验证
  telemetry、Query IR和跨字段合同；
- **概率MDP**：同一`(parent,site)`结果按`visits+0.5*attempts+1`做Laplace平滑；coverage/data/
  backsolver/path-cover收益、feasibility和solver cost进入同步value iteration。循环默认最多32轮、
  残差`1e-6`，snapshot记录并在恢复时重算概率、novelty、迭代次数和残差；
- **验证与边界**：四位独立oracle枚举65536个赋值和16个witness配置，得到10个完整模型、16/16
  candidate hit、`false_sat=false_unsat=0`；4边/1 cut/1 shared关系图与解析两状态固定点一致，17轮
  残差`5.54e-13`。真实helper还覆盖错误completion必须miss后full-SAT的反例；定向Python 2/2、
  关联135 passed加25 subtests、完整Python 1,130 passed加253 subtests，LLVM17/18全量分别为
  304+2和305+1 expected unsupported。详见
  [`F424研究报告`](research-progress/Selective_Concolic_Relation_Graph_and_MDP_F424_2026-08-17.md)、
  [`F424示意图`](diagrams/selective-concolic/f424_relation_graph_mdp.svg)与
  [`F424独立oracle`](../../benchmark/check_selective_concolic_mdp_oracles.py)。这是受限QF_BV mechanism，
  未复现论文的KLEE/JFS/METIS/SVM、BVFP公开目标或coverage/time结果。

## 420. F425：Native ConDPOR、C11 原子提交与有界再执行（2026-08-17）

- **研究缺口**：F62/F392已有bounded execution graph与closed-IR interpreter，但native trace缺少
  原子值，pre-hook在真实指令完成前推进prefix，关系图没有确定性`rf/mo/sc`证书，successor也没有
  fresh-process path/event regeneration；
- **编译与ABI**：LLVM17/18 pass为atomic load/store、RMW、cmpxchg和fence生成group化pre/value/
  result/commit hook；仅无损记录1--64 bit integer/pointer。`SYMCC_DPOR_ATOMIC_ONLY`隔离普通memory
  trace，`SYMCC_DPOR_SCHEDULE_ONLY`跳过symbolic shadow、libc重定向和LowerAtomicPass，IR测试确认真实
  `load atomic/store atomic`被保留；
- **两阶段提交**：prefix命中时TLS保存`(group,index)`但不推进，真实atomic和value hook完成后commit
  以seq_cst fence及gate lock核对并推进。fallback、commit mismatch和nested pending conflict均形成
  campaign负证据，不允许静默成功；parser只按唯一`group+tid+object`绑定value/commit；
- **关系图与campaign**：有界枚举rf来源、逐对象mo/co与RA seq_cst方向，用权威SC/TSO/RA SMT模型
  逐候选准入，证书身份不依赖任意Z3 position。fresh-process campaign封存runtime/executable摘要，
  重建memory/observed graph和schedule query；verifier复算协议、digest、analysis、topology和fixed point；
- **验证与边界**：独立SC线性扩展得到3个store-buffering结果，生产SC为3 SAT/1 UNSAT，TSO/RA均
  4 SAT/0 UNSAT；RA stale message data被拒绝，三写者恰有6个coherence次序。LLVM17/18各40次真实
  原子回放均40/40匹配，零fallback/mismatch/conflict；每版campaign均3 runs/3 prefixes/0 invalid并
  达到bounded fixed point。详见[`F425研究报告`](research-progress/Native_ConDPOR_C11_Atomic_Reexecution_F425_2026-08-17.md)、
  [`闭环图`](diagrams/native-condpor/f425_native_condpor_c11_pipeline.svg)和
  [`独立oracle`](../../benchmark/check_native_condpor_c11_oracles.py)。这是I/T/E-mechanism，不是完整
  ISO C11、herd7 corpus、论文级sound/complete/optimal证明或公开目标性能结果。

## 421. F426：Content-Addressed Cross-Worker Incremental QF_BV Context（2026-08-17）

- **研究缺口**：原persistent QF_BV backend只能在单进程LRU中保留prefix solver，另一worker或
  重启进程无法识别和复用同一前缀，也没有跨进程物化配额、崩溃接管和QueryStore独立身份复算；
- **内容身份**：新增`cross_worker_context.py`，把每条规范SMT-LIB prefix assertion编码为不可变
  delta manifest。SHA-256同时绑定父context、Query IR root、term、capability、累计/新增offset、
  lowering protocol、depth与累计formula digest；完整链限制为4096层/64 MiB，单term限制1 MiB。
  hard-link create-once、file/dir fsync、`O_NOFOLLOW`稳定inode读取和SQLite元数据承诺使并发发布、
  配置漂移与对象替换失败关闭；
- **物化与执行**：带单调token和TTL的fenced lease同时提供per-context互斥和store-wide quota；过期
  可接管，旧owner同名也不能用旧token取消新lease。backend以terminal context digest作为本地LRU
  key：exact local hit直接复用，父context命中只追加一个delta并re-key，fresh worker从验证后的完整
  plan重建solver；目标仍以`push/assert/check/get-value/pop`临时求解。quota等待纳入query deadline
  且可取消，超时返回`unknown/quota-timeout`并保证不启动solver；
- **双重准入**：backend发布后立即解析并复算全链，SAT model先通过Query IR evaluator；QueryStore
  commit前稳定重读查询、重新lowering、独立复算certificate/terminal/parent/depth，再次验证candidate。
  shared exact hit只表示prefix formula plan身份相同，不表示solver result或learned-clause命中；
- **验证**：独立参考编码器覆盖32条深度1--4链且`false_identity=0`；真实cvc5三次SAT分别得到
  input byte 66/67/68，同worker depth-2请求命中parent extension，另一backend达到
  `shared_exact_hit=true/local_hit=false`，三次均经QueryStore提交。tamper、symlink、并发发布、同名
  stale token、TTL接管、quota timeout/cancel和无solver启动反例均通过；定向Python 21/21，关联
  49 passed加25 subtests，完整Python为1,153 passed加253 subtests；LLVM17/18分别为
  308 passed加2 unsupported与309 passed加1 unsupported，两版F426定向lit均2/2；
- **准确边界**：共享制品是可移植prefix公式计划，不是序列化solver heap、preprocessing/search state
  或learned clauses。F426没有新增可检查SMT UNSAT proof，不提高既有`accept_unsat`权限，也没有
  公共target speedup结论。20次八层exact publish+verified resolve的封存run-1机制成本中位数
  7.777 ms，紧邻run-2为4.123 ms，只能量化协议及主机抖动。详见
  [`F426研究报告`](research-progress/Cross_Worker_Incremental_QFBV_Context_F426_2026-08-17.md)、
  [`架构图`](diagrams/solver-context/f426_cross_worker_qfbv_context.svg)、
  [`独立oracle`](../../benchmark/check_cross_worker_context_oracles.py)与
  [`证据目录`](evidence/f426-cross-worker-incremental-qfbv-context-2026-08-17/)。当前等级I/T/E-mechanism，
  W4的proof-carrying UNSAT、lemma传输和公开多节点R级评测仍未完成。

## 422. F427：Proof-Carrying QF_BV UNSAT Receipts（2026-08-17）

- **研究缺口与信任边界**：F426共享的是可重建的prefix公式计划，worker返回的`UNSAT`仍不能被
  另一节点独立授权。F427把结果提升为证明回执：联合绑定主solver SMT2、CPC proof query、Ethos
  reference、Query IR lowering certificate、F426 context、backend capability、generator/checker
  executable和51个CPC signature文件的内容身份；proof store仅是不可信传输层；
- **生成、检查与复用**：主solver的UNSAT只触发固定cvc5 1.3.4 safe-mode CPC生成。portable proof
  移除节点本地路径，每次检查都从本地Query IR重新导出reference，并要求Ethos return code为0、
  stdout逐字节等于`correct\n`、stderr为空。跨worker命中只跳过主solver和generator，backend仍检查
  一次，QueryStore在提交前重新lowering并第二次检查；没有proof配置时one-shot/persistent backend
  保持原单次lowering和显式`accept_unsat`合同；
- **语义闭合与系统协议**：普通与binary BV literal lowering经SMT AST逐节点证明只存在同值同宽
  literal表示差异；proof顶层只允许declaration/definition/assumption/step族，最后一步必须显式证明
  `false`；trust/hole/incomplete、reference控制命令、signature symlink、工具链漂移和CAS替换均失败
  关闭。generator、checker、复用重检和跨进程publication lock共享单一绝对deadline，portfolio
  cancellation终止外部进程组；
- **CAS、并发与资源**：proof/receipt采用SHA-256分离CAS、稳定`O_NOFOLLOW`读取、hard-link
  create-once、文件和目录`fsync`。store-wide稳定regular-file `flock`在写文件前串行检查winner与
  SQLite对象/字节配额；两独立进程在只能容纳一个结果的配额下恰有一个winner，不产生第二组已发布
  CAS文件。进程在文件发布后、索引提交前仍可能留下最多一组未索引orphan，跨作业GC留待后续；
- **验证与准确边界**：定向Python为14 passed加7 subtests，关联为63 passed加32 subtests；完整
  Python为1,167 passed加260 subtests且零skip/xfail/xpass/deselection/身份漂移；LLVM17/18分别为
  309 passed加2 unsupported与310 passed加1 unsupported。两次真实cvc5/Ethos oracle均得到1次
  checked generation、5/5跨worker reuse、reference/CAS tamper全拒绝和0 false authorization。
  这是QF_BV完整结果的I/T/E-mechanism证据，不是learned-clause依赖、solver heap迁移、多节点
  speedup或公开目标覆盖提升。详见
  [`F427研究报告`](research-progress/Proof_Carrying_QFBV_UNSAT_Receipts_F427_2026-08-17.md)、
  [`证明回执图`](diagrams/solver-context/f427_qfbv_proof_receipt.svg)、
  [`独立oracle`](../../benchmark/check_qfbv_proof_receipt_oracles.py)与
  [`证据目录`](evidence/f427-qfbv-proof-receipts-2026-08-17/)。

## 423. F428：Verified QF_BV Learned Lemma Exchange（2026-08-17）

- **研究缺口**：F426只共享可重建prefix公式，F427只证明完整UNSAT结果；直接共享solver内部clause
  无法证明其预处理变量、assumption scope和目标prefix仍有效。F428将cvc5 learned literal转为
  `source context + C_s∧¬L CPC receipt`，并只允许精确ancestor到descendant的蕴含迁移；
- **生产闭环**：persistent backend在原公式SAT/model replay后，以有界one-shot完整公式请求
  `get-learned-literals`。严格parser只接受`sat + one list`，term限制为已知QF_BV/Bool atom和source
  input offsets。每个候选经cvc5 safe-mode CPC生成与Ethos检查后发布record；fresh worker遍历F426
  parent chain、加载祖先record、重建entailment并本地复证后才永久`assert(L)`；
- **双重裁决与资源**：QueryStore对所有活动lemma再次验证target ancestry和receipt，并对新发布record
  从当前Query IR重建完整source、加载CAS和再次运行Ethos，worker不能伪造published计数。单lemma
  64 KiB/4096 node/128 depth、每查询1--64候选、process最多64 active、输出8 MiB/64 KiB、store record/
  总字节quota、stable `flock`、create-once和共享deadline全部进入合同；extractor超时后进程组被回收；
- **完整门禁发现的既有修复**：首次全量运行在F426并发初始化测试暴露SQLite WAL建立race和barrier
  永久等待；生产context store增加稳定inode跨进程initialization lock，测试增加5秒timeout和failure
  abort。修复前压力第9轮复现，修复后200/200通过；
- **验证与边界**：定向7 passed加13 subtests，关联70 passed加45 subtests，完整Python1,174 passed加
  273 subtests且零退化；LLVM17/18为310+2和311+1/312。两次真实oracle各穷举65,536赋值、5/5
  fresh-worker注入、sibling/错误lemma/record/proof篡改全拒绝、0误授权。consumer协议中位604,605/
  578,072 us包含提取、proof与双checker，不是speedup。当前是QF_BV ancestor learned literal的
  I/T/E-mechanism，不是arbitrary clause、solver heap、跨theory或公开多节点R级结果。详见
  [`F428研究报告`](research-progress/Verified_QFBV_Lemma_Exchange_F428_2026-08-17.md)、
  [`机制图`](diagrams/solver-context/f428_qfbv_verified_lemma_exchange.svg)、
  [`独立oracle`](../../benchmark/check_qfbv_lemma_exchange_oracles.py)与
  [`证据目录`](evidence/f428-qfbv-verified-lemma-exchange-2026-08-17/)。

## 424. F429：Lease-Fenced QF_BV Artifact Lifecycle 与依赖感知 GC（2026-08-17）

- **研究缺口**：F426--F428的context、proof/receipt和lemma分别使用内容寻址文件与独立SQLite
  index，长期并行作业缺少跨store的活动引用、依赖闭包和崩溃orphan回收。按年龄或单store引用计数
  会误删活动lemma所需proof/context，永久引用又会在worker崩溃后泄漏；
- **统一图与根**：新增`qfbv_artifact_lifecycle.py`，以`(kind,digest)`统一context/proof/receipt/lemma，
  精确边为context到parent、receipt到proof、lemma到source context与receipt。带owner/generation/deadline
  的job lease及`last_seen` grace共同形成roots，SQLite recursive CTE计算依赖闭包；旧generation不能
  touch/heartbeat/release，新ID达到持久`max_jobs`配额后失败，已有ID仍可单调增代；
- **并发与GC**：稳定regular-file no-follow `flock`让publish/read/touch持共享锁、初始化/GC持独占锁。
  三store在CAS公开前登记lifecycle，旧索引在GC前有界全量同步；source/target/job/reference任一端点
  缺失均在删除前失败。sweep只选没有不可达dependent的节点，故dependent-first；对象、字节与
  selection/lock time均有硬准入预算，dependency cycle不删；
- **崩溃恢复**：store callback按index-first/file-second删除并fsync目录，lifecycle行只在callback成功
  后删除；publish-before-index与index-before-unlink两个故障窗口均可幂等恢复。受管store持久绑定同一
  lifecycle root，已经打开的非受管实例也在每次操作前拒绝绕过；query-service提供后台heartbeat、
  on-start/gc-only、完整inventory和JSON统计；
- **review修复**：proof inventory增加result mapping双向闭合；GC图检查扩展为四端点；修复错误owner
  release虽然返回false却删除活动refs的问题；所有wall-time注入拒绝NaN/Inf/bool；job tombstone因
  generation安全不能随意删除，改用1--10000000的持久唯一ID配额；
- **验证与边界**：定向22 passed，关联52 passed加20 subtests；完整Python1197 passed加273 subtests，
  零skip/身份漂移；LLVM17/18为311+2和312+1/313，定向各1/1。两轮独立oracle均在64个DAG/1024节点
  上零误删、零漏删、零顺序错误，真实五制品链活动保护5/5、释放删除5/5、旧代拒绝1/1、orphan回收
  1/1。机制耗时464704/436942 us包含proof/文件同步/GC，不构成solver、coverage或端到端speedup。
  详见[`F429研究报告`](research-progress/Lease_Fenced_QFBV_Artifact_Lifecycle_F429_2026-08-17.md)、
  [`机制图`](diagrams/solver-context/f429_qfbv_artifact_lifecycle.svg)、
  [`独立oracle`](../../benchmark/check_qfbv_artifact_lifecycle_oracles.py)与
  [`证据目录`](evidence/f429-qfbv-artifact-lifecycle-2026-08-17/)。当前等级I/T/E-mechanism；
  solver-native snapshot/search-state、arbitrary clause scope、跨地域CAS和公开多节点R级实验未完成。

## 425. F430：Native QF_BV Solver-State Fork Reuse（2026-08-17）

- **研究缺口**：F426跨worker运输的是规范formula-plan，原persistent backend也只保留未预热的交互
  进程；target timeout/error会销毁整个context，无法证明solver原生预处理状态被隔离复用；
- **父快照与子事务**：新增Linux/Z3 helper，父进程对prefix执行隐藏warm `check()`并记录generation；
  每个target以COW `fork()`继承父heap，只在child执行`push/assert/check/model`，有界pipe返回后`_exit`。
  deadline只kill/wait child并返回unknown，父代不变；prefix delta重新warm并单调增代；
- **严格合同**：Python要求唯一`native-state-fork-v1`元数据，复核generation/query/warm序列、status、
  timing、PID/fault/RSS与模型；缺失、重复、回退或模型错误在保留快照前失败关闭。SAT仍经backend与
  QueryStore双重复验，UNSAT权限不变，部署开关不进入既有capability hash；同backend禁止cvc5
  learned-literal发布；
- **验证与边界**：Z3 5.0.0下专项8 passed加5 subtests，相关43 passed加25 subtests，LLVM17/18
  定向各1/1；完整Python1205 passed加278 subtests，LLVM17/18完整分别312+2和313+1/314；
  ASan/UBSan及32目标oracle通过。两轮各128目标与同版本cold Z3对拍均0 status mismatch、
  0 invalid model、128 unique child；timeout后同generation恢复。含一次预热的cold/native总成本比为
  2.252x/2.249x，只是固定prefix机制成本，不是coverage或公开目标speedup。详见
  [`F430研究报告`](research-progress/Native_QFBV_Solver_State_Fork_Reuse_F430_2026-08-17.md)、
  [`原生状态图`](diagrams/solver-context/f430_qfbv_native_state_fork.svg)、
  [`独立oracle`](../../benchmark/check_qfbv_native_state_fork_oracles.py)与
  [`证据目录`](evidence/f430-qfbv-native-state-fork-2026-08-17/)。当前为Linux同机易失快照，
  不是跨节点/跨版本heap transport，也未完成公开campaign R级实验。

## 426. F431：Verified Variable-Substitution QF_BV UNSAT-Core Reuse（2026-08-17）

- **研究缺口**：F427只能复用同一目标完整UNSAT receipt，F428只允许精确ancestor-prefix lemma；
  输入字段、循环迭代或同构状态造成的结构相同但offset不同公式仍会重复求解。F431迁移Cache-a-lot的
  变量置换思想，但不信任结构hash、Bloom或core extractor；
- **精确语义**：结构fingerprint只忽略8-bit input read index，保留operator/width/child顺序/常量；
  Bloom仅预过滤。逐子句AST统一生成offset关系行，变量域交集和选择性自然连接求一致映射，允许
  非单射置换与子句塌缩；最终按映射重建内容寻址roots并要求`sigma(core) subseteq target`；
- **证明与二次裁决**：完整公式先经现有UNSAT proof verifier；命名core由cvc5提取后单独生成CPC并由
  Ethos检查。fresh consumer首次按验证域重放receipt；worker与QueryStore的LRU域分离，QueryStore
  从落盘目标Query IR重建lowering、证明和映射。热cache只复用源core定理，每目标仍精确匹配；
- **并发与资源**：SQLite WAL/FULL加immutable CAS、stable regular-file no-follow读取、expected shard、
  稳定publication lock、record/总store配额；scan、candidate、unification pair、join state、AST
  node/depth和绝对deadline均有界。双pipe extractor限制8 MiB/64 KiB，timeout/cancel杀并回收process
  group；F429增加`core -> receipt -> proof`依赖和精确旧metadata迁移；
- **review修复**：删除对非单射不成立的clause-count过滤；加入变量域交集；修复selector SIGKILL不可达
  和TimeoutError误包装；deadline覆盖预处理；修复SQL LIMIT/Bloom候选饥饿；拒绝duplicate JSON key、
  symlink/lock替换；拆分worker/query-store proof cache domain；
- **验证与边界**：定向38 passed，关联101 passed加45 subtests；完整Python1221 passed加278
  subtests且零退化；LLVM17/18分别315+2与316+1/317。两轮512-case独立穷举oracle均294正、218负、
  97非单射正例、0 mismatch。64轮热机制总成本比13.861x（2 clauses）和5.973x（130 clauses），但
  fresh consumer冷复核均慢于baseline中位数。该数据不构成coverage、defect-yield或论文74%复用率
  复现。详见[`F431研究报告`](research-progress/Variable_Substitution_UNSAT_Core_Reuse_F431_2026-08-17.md)、
  [`机制图`](diagrams/solver-context/f431_qfbv_substitution_core_reuse.svg)、
  [`独立oracle`](../../test/qfbv_substitution_core_oracle.py)与
  [`证据目录`](evidence/f431-qfbv-substitution-core-2026-08-17/)。当前等级I/T/E-mechanism；下一步是
  assumption/increment scoped实时证明clause交换和bit-blasted可验证后端。

## 427. F432：Verified Incremental QF_BV SAT 与持久 Proof DAG（2026-08-17）

- **研究缺口**：F426--F431已能运输formula-plan、证明完整UNSAT、交换ancestor lemma、管理依赖
  生命周期、复用Z3原生父快照和变量置换core，但仍缺少SAT层activation assumption、bit-blasted
  clause、原生增量context和可递归验证的持久clause proof；
- **确定性CNF**：新增完整38-operator QF_BV bit-blaster，LSB-first表示，ripple add、restoring
  div/rem、barrel shift、任意位宽rotate和SMT除零/有符号溢出语义。每root生成activation literal与
  `(-a_i OR root_i)`，increment chain绑定parent/clause/变量域，certificate绑定formula、assumption、
  CNF、input map和operator counts；
- **可验证clause交换**：真实CaDiCaL ASCII LRAT在`formula + assumption units`上产生；producer把每个
  子句加入failed-assumption否定guard，删除临时unit hints，并在永久公式上重放ordered LRUP。result
  receipt绑定formula、完整assumption identity、failed set与final clause record。imports逐片段递归
  复核，cycle、descendant-to-ancestor、变量域/CNF prefix漂移均失败关闭；
- **持久DAG与生命周期**：proof fragment使用规范JSON、SHA-256 immutable CAS、SQLite WAL/FULL及
  descriptor-anchored no-follow读写删除；import digest形成proof DAG。F429新增`sat-proof` kind和
  dependency edges，旧metadata精确迁移，GC按dependent-first删除；
- **后端与双重裁决**：一次性CaDiCaL 3.0.1路径生成LRAT；原生C/IPASIR路径缓存1--64个exact formula
  contexts、永久CNF只加一次、assumption逐solve设置、verified import逐context注入。SAT由Query IR
  replay，原生UNSAT仍须独立CLI proof。QueryStore从落盘IR重做bit-blast、重载所有imports/final record
  并二次重放receipt；
- **review修复**：补全increment变量域；模型只查询input variables；分离state/solve locks并阻止
  `close()`并发释放；unsupported返回普通unknown；CAS父分片替换失败关闭；receipt强制protocol；
  拒绝descendant proof import；stdout/stderr转有界文件，状态文本与10/20退出码必须一致；
- **验证与边界**：定向38 passed加13 subtests，关联116 passed加58 subtests；完整Python
  1236 passed加291 subtests且身份清单1236/1236。LLVM17/18均发现320项且无failure/unresolved。
  两轮512-case CaDiCaL差分oracle均0 mismatch，38-operator matrix与cvc5模型一致，真实
  LRAT均24 steps/22 propagations。64轮cold/native总机制成本比分别1.755x和1.765x。详见
  [`F432研究报告`](research-progress/Verified_Incremental_QFBV_SAT_and_Proof_DAG_F432_2026-08-17.md)、
  [`机制图`](diagrams/solver-context/f432_qfbv_incremental_sat.svg)、
  [`独立oracle`](../../test/qfbv_incremental_sat_oracle.py)与
  [`证据目录`](evidence/f432-qfbv-incremental-sat-2026-08-17/)。当前为solve-boundary同步检查和本机
  exact-context机制，不是mid-solve ImpCheck streaming、Mallob动态资源重调度、多节点R级实验或
  coverage/defect-yield提升结论。

## 428. F433：Realtime Checked QF_BV Proof Stream（2026-08-18）

- **研究缺口**：F432只在solve边界消费已验证proof records；活动长求解无法看到其他worker刚发布的
  高价值子句，也缺少“solver实际消费”而非“worker已入队”的可审计证据；
- **持久事件流**：proof CAS增加formula-scoped单调`AUTOINCREMENT`事件序号，record与event同事务
  发布，旧F432 records确定性迁移，GC删除不复用序号。session从recent high-water window轮询，按
  digest和clause去重，并严格执行events/imports/literals/checker预算；
- **求解中checked import**：新增CaDiCaL 3.0.1 C++ bridge，连接`ExternalPropagator`、`Learner`和
  `Terminator`。worker独立重放LRUP/RUP后才能入有界queue；CaDiCaL逐literal callback消费并在终止
  `0`后生成token、solve generation和连续delivery ordinal ACK；未ACK只算pending；
- **learned与二次裁决**：Learner短子句重新从基础CNF推导RUP hints并复核后发布。QueryStore重做
  bit-blast、反查event、重算ACK摘要、复核计数并再次重放proof；SAT telemetry必须覆盖全部实际
  external imports，native UNSAT仍走独立LRAT授权；
- **并发/失败关闭**：deadline与cancel共享原子termination fence；termination clear前移到active登记
  前，关闭pre-solve竞态。session任何异常先停止线程/清队列，再淘汰可能含未登记子句的context；
  imports/learned容量0在native层真正禁用；默认F432路径不变；
- **验证与边界**：专项组合49 passed加38 subtests；真实C++以`-Wall -Wextra -Werror`编译。两轮
  512-case CaDiCaL oracle均0 mismatch、512/512 delivered和ACK replay、篡改拒绝；两轮64-round
  均64/64 delivered，checked-import cold总成本为native cold的1.499x/1.488x，idle median为
  2367/2339 us。详见[`F433研究报告`](research-progress/Realtime_Checked_QFBV_Proof_Stream_F433_2026-08-18.md)、
  [`机制图`](diagrams/solver-context/f433_realtime_proof_stream.svg)、
  [`真实oracle`](../../test/qfbv_realtime_stream_oracle.py)、
  [`机制基准`](../../test/qfbv_realtime_stream_benchmark.py)和
  [`证据目录`](evidence/f433-realtime-proof-stream-2026-08-18/)。当前等级I/T/E-mechanism；不是
  ImpCheck原生wire格式、公开多节点coverage/defect-yield结论或形式化checker。

## 429. F434：Qualified Multi-Rank Realtime Proof Evaluation（2026-08-18）

- **研究缺口**：F433 的真实 oracle 仍是单进程机制实验，不能证明多 publisher 的 event 归属、
  publication 时 consumer 确实位于活动 solve、共享状态跨主机可用，或所有 rank 的 ACK 集完整；
- **资格与身份**：新增 coordinator/publisher/consumer 互斥角色、规范配置摘要、逐轮唯一随机 3-SAT
  Query IR/CNF 身份和 native library 摘要共识。MPI shared-memory communicator 证明物理共址；
  多主机必须通过跨 host lock/namespace qualification，未经资格不生成成功工件；
- **求解中因果门禁**：native shim 暴露 `solving` lifetime；active 模式要求每个 consumer 同时满足
  `solve_generation=1` 和 `solving=1` 后才 release publishers。不同 publisher 并发发布不同 RUP
  record，consumer 检查后由 ExternalPropagator 消费并生成 generation/ordinal ACK；
- **持久与裁决修复**：proof store 由不适合网络文件系统的 SQLite WAL 改为 rollback journal 加已有
  stable flock；新增 `event_for_record(digest)` 精确归属并发事件。rank 0 重建每轮 plan，独立重放每个
  ACK，并拒绝漏收、重复、backpressure、timeout、stream/solve error 和 active miss；
- **时钟与统计边界**：只报告 rank 本地 publish/solve/notification-to-finish 以及 rank 0 epoch
  makespan，禁止直接相减不同主机 monotonic clock。5-rank、2 publisher、2 consumer、2-round 的
  一次 preloaded 和两次 active 真实 job 共 24/24 delivery 与 24/24 root replay；两次 active rate
  均 1.0。过短 workload 以非零状态拒绝伪 active。详见
  [`F434研究报告`](research-progress/Qualified_Multirank_Realtime_Proof_Evaluation_F434_2026-08-18.md)、
  [`评测闭环图`](diagrams/solver-context/f434_multirank_proof_evaluation.svg)、
  [`MPI runner`](../../benchmark/run_qfbv_realtime_multirank.py)、
  [`重复oracle`](../../benchmark/check_qfbv_realtime_multirank_oracles.py)和
  [`证据目录`](evidence/f434-multirank-proof-evaluation-2026-08-18/)。当前为 I/T/E-local；未完成
  8/32/128 worker、公开目标、等 CPU、20 轮统计的 R 级多节点效果实验。

## 430. F435：Adaptive Checked-Proof Admission 与背压恢复（2026-08-18）

- **研究缺口**：F433/F434 对检查通过的实时 clause 只尝试一次 native enqueue；solver 尚未活动或
  queue 满时直接丢失，source质量、proof成本、solve阶段和实际ACK没有形成反馈；
- **确定性准入**：新增整数固定点 controller，封印clause长度、proof steps/propagation、checker
  elapsed、source历史、native solving/generation、solve age、remaining time及queue/ACK/deferred深度。
  proof checker仍是唯一可信准入；controller只输出`admit/defer/reject`，不能授权未经检查的clause；
- **有界恢复**：deferred按retry/event/record确定性排序，以`2^min(retry,6)` polls延迟；queue、deferred、
  retries、decisions、source和clause长度均有硬上限。决策预算耗尽停止扫描；inactive/high-watermark/
  solve-tail/低分延期，硬边界拒绝；
- **闭环与裁决**：native ACK、backpressure或solve-end expired精确结清每个admit decision，重复/未知
  feedback拒绝，stream切换要求零pending。QueryStore重放decision/snapshot，并从当前Query IR、CAS和
  durable event再次核对record/formula/source/proof，即使重算SHA的错误event也被拒绝；
- **review修复**：增加settled候选门禁消除实验竞态；oracle只发布规范化互异clause；solve age改从首次
  `solving=true`计时；abort清空adaptive pending以保证幂等与跨stream复用；trace必须是snapshot末尾；
- **验证与边界**：专项31 passed；完整Python1267 passed加291 subtests且身份1267/1267；双LLVM各
  325 discovered且零失败。测试含256组随机固定点回放、exactly-once反馈、预算/故障/篡改。六轮
  真实CaDiCaL同步突发均由静态2/8交付与6背压提升为adaptive 8/8交付与0背压（+6、+75 percentage
  points、4.0x delivery count）。这是本地构造压力机制结论，不是solver speedup、coverage、漏洞数、
  网络或多节点扩展。详见[`F435研究报告`](research-progress/Adaptive_Checked_Proof_Admission_F435_2026-08-18.md)、
  [`机制图`](diagrams/solver-context/f435_adaptive_proof_admission.svg)、
  [`真实oracle`](../../benchmark/check_qfbv_adaptive_exchange_oracle.py)与
  [`证据目录`](evidence/f435-adaptive-proof-admission-2026-08-18/)。当前等级I/T/E-local；native
  utilization/activity、ImpCheck/PalRUP互操作、proof-aware worker pairing、Mallob式malleability和
  公开多节点R级实验未完成；依据SAT 2025结果，LBD只保留为可选诊断而非默认准入信号。

## 431. F436：Native Clause Activity 与可验证效用遥测（2026-08-18）

- **研究缺口**：F433--F435只能证明共享子句通过checker、被准入并由CaDiCaL完整消费，不能区分
  “已交付”和“consumer搜索曾把它推到unit/conflict”。仅用delivery反馈会系统性高估已满足或长期
  dormant的publisher；
- **原生状态机**：CaDiCaL `ExternalPropagator` callback维护assignment map、decision trail、
  per-import satisfied/unassigned计数及variable-to-occurrence倒排表。仅跟踪已有native ACK的checked
  clause；第一次进入`satisfied=0, open=1`记录unit，`satisfied=0, open=0`记录conflict，backtrack
  精确撤销尚未激活子句，已激活子句不重复计数；
- **协议与可信边界**：新增完整协商的`native-clause-activity-v1` ABI、可复用有界dequeue buffer和
  密封receipt。receipt绑定proof/event/ACK、token/generation/ordinal、native identity、decision
  level、unit literal与完整falsifying witness。QueryStore从当前Query IR重建CNF、重放proof/event/
  ACK并复算witness；这是semantic activation opportunity，不是唯一因果贡献或speedup证明；
- **生命周期与多rank闭环**：`api_mutex`将solve状态转换与add/assume/observe/reset/enable等pre/post
  API原子串行化，同时保留求解中queue并发。session强制连续ordinal、queue清空、native quiescence及
  `unit+conflict+unactivated=delivered`。MPI配置SHA绑定activity开关，rank 0独立重放全部receipt并
  聚合delivery/activation分离指标；
- **review加固**：拒绝bool/float/数字字符串和非精确摘要、部分ABI、重封印错误witness、未知ACK、
  非连续ordinal及计数漂移；修复portfolio future到backend登记间cancel丢失竞态并增加确定性hand-off
  测试；修复干净CMake `check`漏依赖`symcc_schedule_rt`导致调度测试伪失败；
- **验证与边界**：F436关联27 passed；完整Python1274 passed加291 subtests且身份1274/1274；
  LLVM17为324+2/326、LLVM18为325+1/326，均零失败。两轮128-case真实CaDiCaL oracle均128/128
  delivery/unit/replay/tamper rejection，ASan/UBSan 32 cases无诊断。两轮本机5-rank active实验累计
  8/8 delivery与4/8 semantic activation，证明两者可测且不同；不构成solver、coverage、defect-yield、
  网络或多节点R级收益。详见[`F436研究报告`](research-progress/Native_Clause_Activity_and_Utility_Feedback_F436_2026-08-18.md)、
  [`机制图`](diagrams/solver-context/f436_native_clause_activity.svg)、
  [`真实oracle`](../../benchmark/check_qfbv_clause_activity_oracles.py)与
  [`证据目录`](evidence/f436-native-clause-activity-2026-08-18/)。当前等级I/T/E-local；下一步F437
  是带不确定性与探索保底的proof-aware publisher/consumer pairing。

## 432. F437：Utility-Aware Proof-Worker Pairing（2026-08-18）

- **研究问题**：F433--F436已经能证明、投递并观测checked clause，但固定的publisher/consumer全连接会把
  checker、队列和native导入预算消耗在长期低效或过期的pair上。系统需要在不绕过proof gate、不把一次
  activation误当因果收益的前提下，自适应选择值得继续投递的worker pair。
- **方案**：以`publisher x consumer x formula-family`为学习键；family是由QF_BV/CNF协议、规模字段和
  operator-count构成的值无关结构统计签名，并非完整表达式图同构。整数评分结合历史reward、带Laplace
  平滑的delivery/activity yield、不确定性奖励，以及历史加当前checker成本和event lag惩罚。低样本和
  周期refresh强制explore，其余按阈值exploit；`unactivated`只给弱正反馈，不视为因果失败。
- **生产实现**：`RealtimeClauseExchangeSession`只在LRUP/RUP与durable event验证后构造candidate，决定
  suppress或进入native queue；每个admit必须以unit、conflict、unactivated、backpressure或expired恰好
  结算一次。decision/outcome/snapshot使用严格schema、SHA-256 seal和完整replay。QueryStore重新构造
  Query IR、bit-blast、proof/event/ACK/activity/pairing，随后在完成事务内单调提交worker checkpoint；
  persistent backend在启动时恢复已验证且无pending outcome的snapshot。
- **并行评测合同**：F434多rank runner新增opportunity/admit/delivery/activity分层守恒，允许
  `admit = delivered + expired + backpressure`；pairing基线和处理组使用相同seed、相同formula/CNF/library
  身份，并要求至少三轮以越过探索阶段。
- **结果**：F437关联`68 passed + 25 subtests`；完整Python为`1290 passed + 291 subtests`且node-id
  `1290/1290`。两个本机5-rank paired trial共24次机会，抑制4次native导入（16.7%），baseline和
  treatment都保留12次activation，unactivated由12降至8，fixture activation rate由50%升至60%。两个
  seed的solve-time方向相反，聚合约-0.41%，因此不声称speedup。
- **review修复**：补齐严格shape/reward复算、event-slot预留、历史成本累计、expired守恒、
  active-at-publication采样、随机定基数极性、重启恢复、非法outcome异常安全、worker identity一致边界，
  并加固重载环境中的取消测试前置条件。
- **边界**：当前是I/T/E-local；activation是语义机会而非单clause唯一因果贡献。本功能未完成Mallob式
  grow/shrink、PalRUP/ImpCheck wire互操作、8/32/128 worker多节点R级实验或proof-prefix partitioning。
  详见[`F437研究报告`](research-progress/Utility_Aware_Proof_Worker_Pairing_F437_2026-08-18.md)、
  [`F437后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F437_2026-08-18.md)、
  [`机制图`](diagrams/solver-context/f437_utility_aware_pairing.svg)和
  [`证据目录`](evidence/f437-utility-pairing-2026-08-18/)。

## 433. F438：Generation-Fenced Malleable Worker Pool（2026-08-18）

- **背景**：F437能够学习publisher/consumer/formula-family的checked-proof效用，但物理worker仍按固定
  job配额运行。静态配额不能响应backlog、activation yield和checker成本变化；直接复用worker又可能让
  旧lease、迟到结果或未持久proof跨越新assignment。
- **方案**：新增固定物理slot、动态逻辑assignment模型。确定性整数分配器综合backlog、历史reward、
  delivery/activation yield、checker和event-lag成本；以最小份额、递减边际效用和滞回控制grow/shrink，
  并优先保留持有lease的同job worker。
- **正确性协议**：所有assignment绑定`pool/worker/job/generation/token`。重分配必须经过prepare、禁止新
  claim、精确lease retire、proof-set/cursor drain receipt和commit；旧token失败关闭。snapshot verifier
  重算精确worker mapping和事件/租约/证明守恒，QueryStore拒绝同ordinal分叉与generation回滚。
- **生产接线**：`symcc_query_service.py --malleable-workers`在预启动`--jobs`线程上切换active/draining/
  standby；active worker动态重编号shard。生命周期flock保证同pool单协调者；`work_counts()`避免调度轮询
  解析完整历史。在线process-live lease优先于数据库超时，重启后再以QueryStore token恢复或回收。
- **评测**：专项17 passed；完整Python为1307 passed加291 subtests且node-id 1307/1307精确匹配。
  LLVM17为328 discovered、326 passed加2 expected unsupported，LLVM18为327 passed加1 expected
  unsupported，均零失败。测试含80轮随机状态机、重密封篡改、checkpoint fork、在线过期lease、重启与
  锁竞争。两组本机5-rank、8-epoch、3-job机制实验累计38 attach/38 retire、38 durable proofs、31次
  旧fence拒绝和145个物理rank操作归属摘要；每个operation从空controller重放。
- **边界**：当前是预启动物理槽上的逻辑malleability，不是动态MPI spawn、节点故障后communicator
  repair，也没有多节点speedup、coverage或defect-yield结论。等级I/T/E-local；下一项为F439 proof wire
  互操作和公开R级实验。
- **文档与证据**：[`研究报告`](research-progress/Generation_Fenced_Malleable_Worker_Pool_F438_2026-08-18.md)、
  [`F438后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F438_2026-08-18.md)、
  [`机制图`](diagrams/solver-context/f438_malleable_worker_pool.svg)和
  [`证据目录`](evidence/f438-malleable-workers-2026-08-18/)。

## 434. F439：LIDRUP / PalRUP Proof-Wire 互操作（2026-08-18）

- **背景与纠错**：历史`PROOF_FRAGMENT_SCHEMA`字符串含`palrup`，实际对象是项目自定义JSON LRUP
  import DAG，不是外部PalRUP wire。F439保留旧值以维持record SHA兼容，新增
  `PROJECT_LRUP_DAG_SCHEMA`显式边界。
- **LIDRUP协议**：递归验证根DAG后按child-first展平；base ID保持、import ID映射到child最终ID、step
  分配`N+1...`单调全局ID，并对翻译后每步再次执行LRUP。输出匹配的`interaction.icnf`和
  `proof.lidrup`，固定用`lidrup-check --strict`复验；canonical反向导入会重建无import内部record并再次
  经过项目checker。
- **外部工具边界**：checker 0.0.7固定源码commit、binary SHA和版本。实际执行已哈希bytes的私有快照，
  非阻塞pipe实时限制stdout/stderr，超时回收进程组；只接受exit 0、空stderr和独立`s VERIFIED`行。
  receipt不是签名，因此默认复跑checker。
- **生产与持久化**：`LidrupWireStore`以原子staging/hard-link、`O_NOFOLLOW`、`fsync`、`flock`、SQLite
  配额保存ICNF/proof/metadata/receipt。CaDiCaL先完成内部LRUP授权和外部strict检查；QueryStore从CAS
  重载并再次运行外部checker后才提交UNSAT。服务提供checker path/SHA、wire store、配额和timeout配置。
- **PalRUP字节合同**：实现官方`a/i/d`directive、32-bit literal/64-bit ID signed base-128编解码、canonical
  varint拒绝和bounded parser；用固定SAT 2026 commit的`proof_fragment_to_txt`逐字节对拍。receipt明确标为
  fragment syntax interoperability，不冒充多worker全局UNSAT confirmation。
- **验证**：F439专项13 passed加12 subtests；关联56 passed加50 subtests。官方oracle对1条递归import展平
  2条lemma，ICNF 64 bytes、LIDRUP 110 bytes，artifact SHA为
  `29dee2f10f19b16f414d0776d38a72a74c084f28bce0aff829b81897ab81d2fc`；PalRUP 3 directives/16 bytes经
  官方converter一致，fragment SHA为`1c10c832560aeeff6a61ea6226edd10ff95a8b5329a3542e9b732b02a90f7299`。
- **边界与材料**：等级I/T/E-local；尚无PalRUP`local_check/redistribute/confirm`全局授权、多节点
  8/32/128 worker或speedup/coverage/defect-yield结论。详见
  [`研究报告`](research-progress/LIDRUP_PalRUP_Proof_Wire_Interoperability_F439_2026-08-18.md)、
  [`机制图`](diagrams/solver-context/f439_proof_wire_interoperability.svg)和
  [`证据目录`](evidence/f439-proof-wire-2026-08-18/)。

## 435. F440：PalRUP 多 Worker 全局确认流水线（2026-08-18）

- **背景**：F439只完成PalRUP二进制fragment语法互操作，尚未驱动官方`local_check → redistribute →
  confirm`，因此不能由多个fragment授权全局UNSAT。F440把固定SAT 2026 checker实现为显式、失败闭锁的
  库与CLI。
- **拓扑与流程**：对`N`个immutable fragment先执行`N`个local checks；令
  `w=ceil(sqrt(N))`，按官方`test_full_run.c`和`pal.sh`执行`w²`个redistribute tasks；最后执行`N`个
  confirmations。README对redistribute数量的简写与实际代码不一致，项目以固定commit的可运行集成合同和
  12-fragment oracle为准。
- **可信边界**：固定源码commit及三工具SHA，实际执行私有executable snapshot；公式/proof采用
  `O_NOFOLLOW`和单descriptor快照，按文件、输入总量和stage总量限额。子进程具备deadline、进程组回收、
  stdout/stderr实时上限和首错队列取消；源proof不交给官方cleanup路径。
- **全局授权**：要求连续rank的hash/proxy/import产物、精确`N`个`.check_ok`目录、非空规范UNSAT witness、
  `N/w²/N`任务守恒及字节守恒。receipt分离policy、bundle、workspace和运行相关phase digest；结构验证严格
  拒绝额外字段，默认用原输入重跑完整pipeline。
- **验证**：专项`6 passed + 7 subtests`覆盖完整3-worker/4-cell路径以及缺失/额外marker、空witness、空
  formula、15-byte错误hash、symlink、tool replacement、非零退出、stderr、timeout、output flood、预算和
  receipt篡改。
  固定官方`r3unsat_200`实测12 fragments/8,452,282 input bytes，执行12 local + 16 redistribute + 12
  confirm，确认12/12、witness `[3]`、40个stage artifacts/8,644 bytes，默认独立重跑通过。
- **边界与材料**：等级I/T/E-local；当前SymCC solver尚不原生产生PalRUP proof fragments，未完成跨节点
  transport/故障恢复，也没有solver speedup、coverage、defect-yield或多节点scalability结论。详见
  [`研究报告`](research-progress/PalRUP_Global_Confirmation_Pipeline_F440_2026-08-18.md)、
  [`机制图`](diagrams/solver-context/f440_palrup_global_confirmation.svg)和
  [`证据目录`](evidence/f440-palrup-global-2026-08-18/)。

## 436. F441：Generation-Fenced MPI / ULFM 故障恢复（2026-08-19）

- **问题**：F438的逻辑malleability不能修复物理MPI进程退出后的communicator；rank又会在shrink
  后变化，不能直接作为持久worker、shard或lease身份。
- **状态模型**：以稳定`endpoint_id + incarnation + host`区分逻辑端和进程实例；generation token
  绑定run与完整成员，shard token绑定owner，lease token绑定work和ordinal。checkpoint cursor单调，
  completion形成摘要链，旧代消息统一分类为stale。
- **恢复协议**：`prepare → Revoke → Shrink → 两阶段有界Iallgather身份attestation → Agree →
  commit(g+1)`。允许repair期间额外成员失效；严格拒绝重复/reappearing endpoint、非稠密rank、
  过期plan和receipt/state拼接。rendezvous hashing重分配失效owner的shard。
- **保守语义**：revoke后无法可靠区分旧路由上的完成状态，因此所有在途lease进入recovery queue，
  包括survivor-owned任务；恢复队列优先领取并原子产生新lease。该策略允许幂等重复计算但避免漏任务。
- **真实运行时**：固定Open MPI 5.0.10、源码SHA和`--with-ft=ulfm --enable-mpi-ext=ftmpi`；
  installer检查公共MPIX符号。系统Open MPI 4.1.6的多rank `Revoke()`为`NotImplementedError`，未被
  表面API探测错误接纳。Open MPI 5.0.10的4-rank capability全部通过。
- **验证**：专项11 passed。确定性oracle在12 endpoints/96 shards/2 failures下重分配16 shards，
  故障前32 leases中8项已完成、24项全量重排并重放，24个旧token全部拒绝，最终队列为0。本机
  TCP transport真实rank退出完成world 4→3，一次repair，3名survivor产生相同receipt
  `451a6b2a...211e8`。
- **边界**：等级I/T/E-local。本次交付是独立可测试恢复substrate，尚未接入
  `mpi_concolic_execution.py`默认热路径或durable QueryStore提交；没有补员、跨主机MTTR、solver
  speedup、coverage或defect-yield结论。按收口要求完成后暂停后续功能。
- **文档与证据**：[`研究报告`](research-progress/Generation_Fenced_ULFM_Recovery_F441_2026-08-19.md)、
  [`机制图`](diagrams/solver-context/f441_ulfm_generation_recovery.svg)和
  [`证据目录`](evidence/f441-ulfm-recovery-2026-08-19/)。

## 437. F443：生产热路径 Generation-Fenced ULFM（2026-08-25）

- **问题**：F441是独立恢复substrate，尚未进入真实master/worker派发、WAL提交与有界shutdown。
- **实现**：生产loop在每次dispatch前持久化generation/shard/lease fence；worker回显完整envelope；
  master以commit manifest为跨QueryStore/WAL的可重放决定点。物理worker或原root退出后，survivor修复
  communicator、推进generation、重做`committing`并零TTL回收不确定lease，总wall deadline不重置。
- **证据**：固定Open MPI 5.0.10 ULFM的worker/master真实退出门禁均通过；独立oracle复算receipt、
  snapshot、WAL、corpus与epoch retirement。5秒simulation只证明机制，不构成性能数据。
- **边界**：opt-in、失败关闭；F443当时只证明单master同机TCP缩容恢复。multi-master、补员与跨节点由
  F446/F447继续闭合。
- **材料**：[`研究报告`](research-progress/Production_ULFM_Hot_Path_Recovery_F443_2026-08-25.md)。

## 438. F446：弹性 Multi-Master ULFM 与 Warm Spare（2026-08-25）

- **问题**：单master shrink会持续损失并行度，且旧group communicator、瞬时transport rank和最后一代
  内存统计不能支撑连续故障。
- **实现**：增加全局恢复communicator与每代group/master communicator；初始rank作为stable endpoint；
  每代封存layout并确定性重组。预启动高rank warm spares按稳定身份晋升，无需在故障后依赖spawn。
  最终统计从共享WAL done manifests重新聚合，避免前代工作丢账。
- **证据**：同机四场景覆盖multi-worker、original-root、warm-spare和连续双故障。10-rank连续场景两次
  communicator replacement后依次晋升rank 8/9并保持8 active。该门禁为物理失效机制证据。
- **边界**：不保证exactly-once computation，只保证fenced、effectively-once publication；性能、MTTR
  和跨节点结论不从短simulation推导。
- **材料**：[`研究报告`](research-progress/Elastic_Multi_Master_ULFM_F446_2026-08-25.md)。

## 439. F447：跨节点连续恢复、原子 Replay 与规模证据门禁（2026-08-25）

- **跨节点一致失败集合**：生产路径不再把局部`Get_failed`当成成员真相。survivor先`Revoke/Shrink`，
  在共同communicator上固定宽度`Iallgather` stable ranks，以旧成员集合差得到missing set并`Agree`；
  fresh-context第二次shrink/attestation吸收竞态故障。
- **持久状态隔离与事务**：每个master/generation使用独立QueryStore root。`replay_commit_once`在单条WAL
  record锁内完成manifest校验、corpus发布和done；失败保留committing供下一master重试，消除并发master
  消费同一staging的竞态。
- **跨用户合同**：snapshot与public corpus分别由严格八进制、非执行文件模式控制；发布前
  `fchmod/fsync`。默认仍为`0600`，跨用户部署必须显式选择并遵守目录访问策略。
- **科研封存修复**：`research_protocol.py`在每个新cell前重算Git worktree、submodule、工具环境和显式
  input identity，拒绝封存后漂移，避免同一结果矩阵混入两个实现或corpus。
- **跨节点证据**：双主机10 ranks、8 active + 2 spare，连续真实退出rank 2/3后两次恢复并晋升8/9；
  generation 2仍有8 active、2 masters/6 workers和2 surviving hosts。WAL为164 analyzed/820 generated，
  corpus 820对象，2,491个证据文件摘要通过。launcher 86由独立oracle接纳。
- **提前停止规模实验**：预注册20轮/档，用户要求累计超过30个cell后停止。35个外层结果落盘，11个
  三档完整区组中128-worker有2次内部`exit-70`；驱动器此前错误吞掉内部失败，现已在报告落盘后传播
  非零退出。性能只使用9个三档均成功的配对区组：相对8 worker，32/128 worker的unique/s配对变化为
  +17.63%/+29.29%，但endpoint edges为-5.99%/-1.82%，保留率下降8.27%/13.98%。单调覆盖饱和模型
  被拒绝，未伪造coverage ceiling。
- **边界与等级**：I/T/E-crosshost + engineering-scale。SSH/NAT shim、SMB和test flock broker属于受控
  门禁；规模矩阵提前停止且128-worker为2/11失败，不是R级confirmatory结果，不证明通用coverage提升、
  生产上限、原生生产网络/文件系统或MTTR分布。
- **材料**：[`研究报告`](research-progress/Cross_Node_Continuous_ULFM_and_Scale_Evidence_F447_2026-08-25.md)、
  [`架构图`](diagrams/f447-cross-node-ulfm-architecture.svg)。

## 440. F442：并行规模模型与有界 Prefix-DAG MDP（2026-08-20，补录）

- **问题**：此前缺少把实测吞吐、串行协调成本、worker 竞争和覆盖饱和分开表达的规模决策框架，
  Prefix-DAG/ColorGo 刷新又会在高并行度下放大 master 开销。
- **实现**：增加 USL 参数化决策模型、hybrid master profile 和结果 triage；把 Prefix-DAG MDP
  刷新限制在有界受影响前缀，并保留输入数据、拟合拒绝原因和证据等级。模型只在数据满足单调性与
  规模点要求时给出 ceiling；F447 非单调覆盖数据因此被明确拒绝拟合。
- **证据与边界**：现有短时/提前停止数据仅支持工程诊断，不足以形成稳定 R 级上限。模型来源、
  公式、变量、敏感性和最低数据要求见
  [`研究报告`](research-progress/Parallel_Scaling_Model_and_Bounded_Prefix_MDP_F442_2026-08-20.md)。

## 441. F448：Proof-Prefix 引导的可认证 QF_BV 分区（2026-08-25）

- **问题**：F438 能在逻辑 job 间动态分配 slot，但单个困难 QF_BV 查询没有可证明不重不漏的
  子任务边界；直接信任 activity heuristic 可能造成错误分区或不可复现实验。
- **实现**：重放 F436 proof/ACK/activity 后计算变量优先级；从根 cube 做 `K-1` 次完整二叉切分，
  支持任意 `K<=4096`；证书绑定 Query IR bit-blast formula、CNF、base assumptions、policy、
  排名、split transcript、cube assumptions和精确有理质量。verifier独立重算全部字段，排序只影响
  效率，不进入覆盖正确性根。
- **工程闭环**：descriptor-anchored canonical JSON CAS；独立 `partition` lifecycle kind；自动
  `partition -> sat-proof` 依赖；registry身份持久绑定；旧schema迁移；QueryStore生成/复验CLI；
  query-service stats、inventory和GC路由；malleable job catalog。
- **验证**：F448/生命周期聚焦门禁51项通过；完整capability-closed Python为1396 passed加310
  subtests且1396/1396身份精确、16项能力零缺失。10轮oracle覆盖1/3/5/8/32/128/512 cubes，枚举验证
  完整/互斥；32路发布收敛为1个新对象；19对象/84边dependent-first GC删除19/19。512-cube构建
  并内置重放中位139.881 ms，独立重放68.033 ms。
- **边界**：I/T/E-mechanism。主query-service尚未自动执行cube assumptions；没有等CPU公开目标
  的static/guided/unpartitioned对照，因此不声称speedup、coverage或defect-yield提升。
- **材料**：[`研究报告`](research-progress/Proof_Prefix_Guided_Certified_Partitioning_F448_2026-08-25.md)、
  [`机制图`](diagrams/f448-proof-prefix-certified-partitioning.svg)和
  [`证据目录`](evidence/f448-proof-prefix-certified-partition-2026-08-25/)。

## 442. F449：证明感知的可认证 QF_BV 分区执行（2026-08-25）

- **问题**：F448 只生成经过认证的 cube 目录，主 query-service 未执行 assumptions；单条困难
  QF_BV 查询仍不能使用多个求解槽，叶级 UNSAT 也不能合成为原查询证明。
- **方案**：用 query/plan/partition/policy 封印 execution identity；SQLite 为每个 cube 保存
  owner/token/expiry fenced lease、heartbeat、retry 和 crash reclaim；每槽独立 CaDiCaL context
  只安装 solve-local assumptions。SAT 经 cube polarity 与 Query IR replay 后原子抢先并取消 peer；
  全叶 UNSAT 经 signed receipt 重放和 F448 split tree 反向 resolution，生成 base-query LRUP receipt。
- **实现**：新增 `qfbv_partition_execution.py`、query-service 配置/预算/GC 路由、QueryStore 父租约
  renewal 与结果二次裁决、one-shot/persistent assumption API、signed proof/wire 协议和
  `partition-execution` 生命周期。持久 JSON 必须 canonical；并发聚合身份只由 execution 决定。
- **测试**：专项覆盖正负 cube、SAT/UNSAT 服务 E2E、并发领取/聚合/取消、崩溃恢复、严格 JSON、
  proof/inventory 篡改、persistent native context 和 dependent-first GC；正式 oracle 对
  2/4/8/16/32/64 cubes 各 5 轮，64 叶经 63 步聚合，SAT 首胜完成 1 个并取消 63 个。专项
  22 passed；耦合回归 143 passed 加 50 subtests；完整能力门禁 1419 passed 加 310
  subtests，1419/1419 nodeid 精确且 16 项能力零缺失。
- **审查修复**：signed negative assumption、persistent UNSAT exact-scope confirmation、父租约续期、
  双协调者确定性聚合、NaN/Infinity 时间、canonical ledger 重放、完整 cube-result 二次复核和
  execution lifecycle 悬挂引用。
- **边界**：I/T/E-mechanism。当前是同进程多独立 backend slot；没有公开目标同 CPU 长时多轮
  speedup/coverage/defect-yield 结论，也未将 cube lease 映射到跨节点 F438 worker catalog。
- **材料**：[`研究报告`](research-progress/Proof_Aware_Certified_Partition_Execution_F449_2026-08-25.md)、
  [`机制图`](diagrams/f449-proof-aware-partition-execution.svg)和
  [`证据目录`](evidence/f449-proof-aware-partition-execution-2026-08-25/)。

## 443. F450：闭包绑定的有界证明重放授权缓存（2026-08-25）

- **问题**：F449 的 64-cube 证明聚合会反复授权同一 project-LRUP import DAG；重复 JSON 解析、
  formula-position 查找、LRUP propagation 和逐对象 SQLite/flock 操作构成明显串行成本。直接缓存父授权
  又会掩盖传递依赖被替换的事实。
- **方案**：缓存键绑定同一 `BitBlastPlan` 对象与 proof record digest，entry 以 weakref、formula/
  assumption/tuple scope、immutable authorization 和完整 `(CAS digest, encoded SHA-256)` import closure
  组成。每次调用仍先规范化根记录；hot hit 在一个 shared store lock 和一个 SQLite connection 中对全部
  依赖做 canonical path、indexed size、stable-read identity 和 encoded digest 见证，全部成功后才返回。
- **资源与兼容**：entry-count/accounted-byte 双预算 LRU，默认 4096/16 MiB，双零关闭；非规范 mutable
  plan 只重放、不缓存。既有 checker `policy_sha256` 保持稳定，缓存使用独立 protocol/policy identity。
  attestation 失败不计 hit，精确驱逐 entry，并输出 hits/misses/evictions/oversized/bypassed/failure 及
  attested objects/bytes。
- **审查演进**：R1 修复 imported CAS tamper 被旧父授权掩盖；R2 分离 persistent checker 与 local cache
  policy；R3 以无收益反例否决逐对象 JSON/SQLite 设计并改为 batch attestation；R4 收紧 plan
  mutability/weakref/concurrency；R5 将 hit 线性化点移到闭包见证成功后。
- **验证**：专项 9 passed；耦合回归 88 passed 加 50 subtests；完整 capability gate 为 1428 passed
  加 310 subtests，1428/1428 nodeid 精确、16 项能力零缺失且零 skip/xfail/deselect。10 轮 8/32/64 层
  oracle 的 disabled/hot 中位数为 35.655/9.140、148.042/10.410、279.206/10.997 ms，对应
  3.900985x、14.221134x、25.389288x；hot path 仍见证 70/310/630 个依赖对象。
- **边界**：I/T/E-mechanism。倍率只属于 proof-DAG authorization，不是 SAT solver、coverage 或
  defect-yield 提升；尚未实现 TACAS 2026 native checker clause compression、跨节点缓存或 F449 cube
  到 F438/F447 worker catalog 的映射。
- **材料**：[`研究报告`](research-progress/Closure_Bound_Proof_Replay_Cache_F450_2026-08-25.md)、
  [`F450后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F450_2026-08-25.md)、
  [`机制图`](diagrams/f450-closure-bound-proof-replay-cache.svg)和
  [`证据目录`](evidence/f450-closure-bound-proof-replay-cache-2026-08-25/)。

## 444. F451：代次栅栏下的跨节点认证 Cube 执行（2026-08-26）

- **问题**：F449 的 cube owner/token/expiry 只能防止本地重复提交，不知道 communicator
  generation、stable endpoint 或 shard；F447 的 ULFM work fence 又不知道 cube certificate、
  assumptions 和 SAT/UNSAT 证据。节点恢复后，两层任一独立使用都无法阻止越代结果。
- **方案**：为每个 cube 规范绑定 F449 inner lease 与 F447 generation/shard/work lease；
  `run_id` 和 `work_id` 精确编码 execution 与 ordinal。heartbeat/result 必须先证明外层
  current，再续租或重放内层证据。SAT winner 取消全部 current peers；UNSAT 继续使用
  F448 checked split tree 聚合。
- **恢复与崩溃闭包**：ULFM receipt 提交后，全部 ambiguous in-flight work（包括
  survivor-owned）进入 recovery queue。`recover_lease` 原子提升 inner token 但不增加 semantic
  attempt。`resume_recovery_queue` 不重复推进 generation；`reconcile` 修复 outer-dispatch/
  binding-insert 与 inner-result/outer-finish 两个跨 ledger 窗口。
- **持久化与并发**：binding store 使用 stable no-follow 初始化锁、SQLite FULL 同步、
  inner/outer lease 唯一约束和严格 canonical JSON。多 Master 共用 QueryStore 连续 state ordinal，
  stale fork 失败关闭并补偿已领取的 inner cube。无状态 CLI 每次从五类持久工件重建。
- **验证**：专项 15 passed；耦合 127 passed 加 38 subtests；完整门禁 1443 passed 加
  310 subtests，1443/1443 nodeid 精确且 16/16 能力存在。5 轮逻辑 oracle 的 10 个
  recovery case 恢复 40/40 cubes，丢失 0；3/3 轮物理双主机 adapter 在远端 exit 86 后
  均完成 generation 0→1、3/3 在途重排、survivor SAT 和 2/2 peer 取消。
- **审查演进**：六轮依次收紧组合信任边界、持久化/策略身份、跨 ledger 崩溃原子性、
  恢复可重入、stale multi-master/SAT 语义验证和物理 transport 拼接阻断。
- **边界**：I/T/E-crosshost-adapter。SSH 只是应用层适配器；F447 独立证明物理 ULFM
  communicator 恢复。不声明 exactly-once computation、solver speedup、coverage 或 defect yield。
- **材料**：[`F451研究报告`](research-progress/Generation_Fenced_Distributed_Cube_Execution_F451_2026-08-26.md)、
  [`F451后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F451_2026-08-26.md)、
  [`机制图`](diagrams/f451-generation-fenced-distributed-cube-execution.svg)和
  [`证据目录`](evidence/f451-generation-fenced-distributed-cubes-2026-08-26/)。

## 445. F452：原生 Checker 子句压缩（2026-08-26）

- **问题**：F450 缓存减少重复 proof replay，但实时 native import queue 和 activity tracker 仍以
  `std::vector<int>` 保存每个 32-bit literal；大量中长子句造成 payload、堆分配和 cache footprint。
- **方案**：固定 TACAS 2026 ImpCheck 机制合同，把 signed literal 映射为 `-1,+1,-2,+2→0,1,2,3`，
  unsigned 排序后差分，使用 canonical 7-bit varint，并以包含自身的总长度开头；`<=7 B` 显式数组
  inline，其余 owned heap。输入只读、对象 move-only，解码 cursor 有 size/canonical/overflow/count 门。
- **生产接线**：proof authorization 后才压缩；CaDiCaL callback 逐 literal 解码，完整消费后才 ACK；
  activity tracker 持续保存压缩对象。独立 C ABI 采用两次调用容量协商且不部分写。portfolio 可用
  `require_clause_compression` 强制协议；安装脚本检查全部动态符号。
- **证据闭环**：native 新增原始/压缩字节、inline/heap、queued bytes、decoded literals 和 failure
  telemetry；session 做基线差分，QueryStore 要求字段成组、计数守恒且 failure 为零。旧 shim 默认兼容，
  显式 require 时缺能力失败关闭。
- **审查修复**：分离误入 numeric options 的布尔开关；空输出避免 null iterator；补持久遥测；恢复被
  测试插入切断的 cleanup；保留单 literal 负收益。ASan/UBSan 100k 随机 round-trip，GCC/Clang Werror。
- **验证**：专项 23 passed；耦合 37 passed；完整门禁 1467 passed 加 310 subtests，1467/1467
  nodeid、16/16 能力且零退化。20k oracle 中 3/8/32/256 literals payload 分别减少
  24.3%/38.6%/48.4%/52.1%，1 literal 增加 11.4%。真实 CaDiCaL 两条子句 152 B→40 B、2/2 ACK、
  38 literals 解码、failure 0。
- **边界**：I/T/E-mechanism。字节只计算 literal payload，ABI 时间含 ctypes；不声明进程 RSS、SAT
  speedup、coverage、defect yield 或复现论文 1216-core/<33% 结果。
- **材料**：[`F452研究报告`](research-progress/Native_Checker_Clause_Compression_F452_2026-08-26.md)、
  [`F452后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F452_2026-08-26.md)、
  [`机制图`](diagrams/f452-native-clause-compression.svg)和
  [`证据目录`](evidence/f452-native-clause-compression-2026-08-26/)。

## 446. F453：在线 Activity/Cost 引导的认证 QF_BV 分块（2026-08-26）

- **问题**：F448 的 activity 排序和 F449--F451 的执行结果彼此分离；固定 cube 数无法根据公式族的
  历史成本调整，额外 prerun 与更大 `K` 还可能在实验中获得不公平的计算预算。
- **方案**：以 formula-family 为作用域，在 `static/activity/cost` 三个 arm 间进行持久、有界、可回放
  的 warm-up、epsilon/UCB 选择；cost arm再对 `2/4/8/16` 等候选做独立探索。decision记录policy身份、
  arm/cube/联合propensity、选择原因和证据来源。无activity能力确定性回退static。
- **可信闭环**：guided prerun只消费ACK配对的checked activity，且由F448独立重放；预运行SAT必须
  回放candidate，UNSAT必须有授权proof。F448分区完整性/互斥性和F449结果复核不信任在线策略。
  QueryStore重新规范化decision/outcome并核对公式族、partition与预算。
- **资源与持久化**：统一CPU-ms上限为`base K × base timeout × attempts`；prerun从上限扣除，剩余按
  chosen K和attempts归一化。SQLite FULL/WAL、no-follow路径、canonical JSON、digest、pending上限和
  verified history replay封闭重启、并发与篡改边界。
- **审查修复**：补齐三层propensity；把pending计入并发探索；拒绝SQL冗余列污染；将prerun和大K纳入
  同一预算；持久复用backend；分离arm/cost两层warm-up；verified terminal才能提前结束。
- **验证**：专项与partition组合37 passed；耦合136 passed加25 subtests；完整门禁1482 passed加310
  subtests，nodeid 1482/1482且16项能力零缺失。15次机制oracle中三arm各5次，
  同为8000 CPU-ms配置上限；static/activity/cost中位总时间为348.016/1813.961/1742.517 ms，cost依次
  探索4/8/16/2后再次选择2。负结果说明该小型公式不适合guided cubing。
- **边界**：I/T/E-mechanism。没有公开目标、长时多seed或统计功效，不声明solver speedup、coverage或
  defect-yield提升。
- **材料**：[`F453研究报告`](research-progress/Online_Activity_Cost_Guided_Cubing_F453_2026-08-26.md)、
  [`F453后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F453_2026-08-26.md)、
  [`机制图`](diagrams/f453-online-activity-cost-cubing.svg)和
  [`证据目录`](evidence/f453-online-activity-cost-cubing-2026-08-26/)。

## 447. F454：Solver-native 子句共享与 PalRUP 生产闭环（2026-08-26）

- **问题**：F439定义外部proof wire，F440可运行官方全局checker，但SymCC侧不能由真实并行solver原生
  生成和交换PalRUP proof；测试夹具不能代表生产链路。
- **方案**：固定Mallob CaDiCaL fork，在隔离helper内运行N个rank thread。每个rank有独立proof tracer
  与fragment；SharingTracer将规范化短学习子句写入有界ClauseBus，LearnSource从per-rank队列导入。
  stable rank ID、start gate、seed/phase差异与五类telemetry形成可审计并行协议。
- **可信闭环**：parent快照输入与工具，严格核对DIMACS header、公式内容、rank namespace、worker
  stats和exact artifact layout；内置parser只早检。官方local-check/redistribute/confirm是唯一UNSAT
  裁决边界。成功后fsync，完整staging tree再次官方recheck，再no-replace原子发布；验收oracle还从最终
  path执行第三次recheck。
- **审查修复**：通信子句数值排序以满足官方checker；拒绝零/INT_MIN/重复/重言式；修复
  `max_parallel`、witness race、bool计数、重封元数据、crash staging、wrapper原子安装和三方公式绑定。
- **验证**：专项13 passed加7 subtests；耦合34 passed加26 subtests；完整门禁1496 passed加317
  subtests、node ID 1496/1496且16项能力零缺失；strict lint、shell syntax、GCC Werror及wrapper边界
  ASan/UBSan通过。1/2/4 rank各3轮官方复核，2/4 rank真实import，fan-out守恒；
  SAT negative不发布。pool中位0.264/0.224/0.273秒，端到端中位3.218/6.974/9.760秒；终态
  pending中位为0/1,002/10,192、最大10,441。
- **边界**：I/T/E-mechanism。结果证明native生产与官方确认，不证明solver speedup、coverage、defect
  yield或多节点扩展；增长的proof/check成本是后续优化对象。
- **材料**：[`F454研究报告`](research-progress/Solver_Native_Clause_Sharing_PalRUP_Production_F454_2026-08-26.md)、
  [`F454后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F454_2026-08-26.md)、
  [`机制图`](diagrams/f454-solver-native-palrup-production.svg)和
  [`证据目录`](evidence/f454-native-palrup-production-2026-08-26/)。

## 448. F455：结构化 Agentic Concolic 闭环（2026-08-26）

- **问题**：旧agent hook缺少不可变实验身份、跨重启总预算、严格response binding、真实执行回馈和可证明
  同任务的三臂消融；每轮调用还会浪费模型预算并干预正常求解。
- **方案**：新增`StructuredAgenticController`和canonical hash-chain ledger。worker结果先由master合并
  AFL edge bitmap，再以UNKNOWN/timeout/Backsolver失败、符号零产出或覆盖plateau作反应式触发。任务
  包含有界遥测与同episode已验证history；模型只可返回schedule或具体candidate。
- **身份与资源**：policy绑定experiment/program、触发策略、provider/model/prompt/transport、schema和
  全部预算。portfolio共享每请求reservation；请求、token、time、cost、candidate count/bytes均跨重启
  持久。command/HTTP输出先流式限为1 MiB，失败由circuit breaker和确定性fallback处理。
- **可信闭环**：严格schema、echo摘要、duplicate/nonfinite/unknown字段拒绝；schedule只能匹配源输入且
  一次性选择；candidate进入VerifiedProposal、真实目标/分支、可选parser和全局AFL novelty通道。候选
  摘要与decision登记后，真实执行结果回到原episode；模型不裁决SAT、reachability或coverage。
- **实验隔离**：online应用合格动作，shadow验证并记录但只执行fallback，fallback零backend调用；离线
  比较要求相同base policy和精确`episode/iteration/task_sha256`多重集合。
- **验证**：专项28 passed加6 subtests，含MPI triage耦合88 passed加43 subtests。三臂oracle各4次门控
  均trigger 3/suppress 1；online/shadow各3有效response，fallback零调用；online准入3候选，shadow提出
  3但准入0；重复edge的worker级权威delta为1/0。完整门禁1517 passed加323 subtests、身份1517/1517、
  16项能力零缺失且零skip/xfail/deselect。
- **边界**：I/T/E-mechanism。固定command backend只证明协议、门控、计量和消融完整性，不证明真实LLM
  coverage、speedup或论文复现；公共目标效果归R-track。
- **材料**：[`F455研究报告`](research-progress/Structured_Agentic_Concolic_Closed_Loop_F455_2026-08-26.md)、
  [`F455后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F455_2026-08-26.md)、
  [`闭环图`](diagrams/f455-structured-agentic-closed-loop.svg)和
  [`证据目录`](evidence/f455-structured-agentic-concolic-2026-08-26/)。

## 449. F456：POSE-C Initial Symbolic Heap（2026-08-26）

- **问题**：F405--F422从编译器给定的有限heap候选出发，没有入口未知对象图的null/alias/fresh按需
  materialization；经典lazy initialization会为非CFG heap选择制造额外状态。
- **方案**：以稳定reference/proxy object和canonical term DAG表示initial heap；首次byte/reference-field
  读取构造同类型alias ITE。store/free条件更新所有可能proxy；alias quotient保存已知相等/不等关系，只有
  `branch_alias`代表真实CFG decision并建立child。
- **可信边界**：固定layout/bounds/live/initialized和六类资源预算；snapshot严格验证identity/DAG/width/
  relation/metric。QF_BV由Z3验收；model须通过reference/scalar domain、quotient和全部guard，才物化
  concrete graph。快照内容寻址嵌入continuation symbolic store并保留parent lineage。
- **验证**：定向25 passed加256 subtests；耦合232 passed加282 subtests；完整门禁1542 passed加579
  subtests、1542/1542 nodeid且16项能力零缺失；独立oracle 9/9，read/store各
  27/27 alias assignment，swap/sum/list-max10为2/1/12 CFG paths且heap paths为0。11轮16引用机制中位
  2662.702 us、289 terms、85,960 B，解析alias/null partition为82,864,869,804种。
- **边界**：I/T/E-mechanism。paper lazy trace仅作来源标注；机制计数/时间不证明coverage、defect yield、
  solver或端到端speedup。自动LLVM layout恢复、任意pointer arithmetic、并发heap和公开长时实验归后续。
- **材料**：[`F456研究报告`](research-progress/POSE_C_Initial_Symbolic_Heap_F456_2026-08-26.md)、
  [`F456后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F456_2026-08-26.md)、
  [`机制图`](diagrams/f456-pose-initial-symbolic-heap.svg)和
  [`证据目录`](evidence/f456-pose-initial-symbolic-heap-2026-08-26/)。

## 450. F457：全局覆盖收敛与并行有效产出优化（2026-08-26）

- **问题**：master首次派发前未建立AFL覆盖基线；历史queue前缀可耗尽扫描预算；busy worker在旧bitmap上过滤；
  多主coverage owner逐候选持久写；相邻窗口并行比较受覆盖衰减和旧在途任务污染；benchmark将cleanup混入campaign。
- **方案**：完整启动回放加增量retry bridge，稀疏/原生map零扩展，未见输入工作预算，版本化原子共享快照，
  queue-first的ordered `claim_many`，A-B-A switchback加cohort fencing，以及freeze/drain/final-sync分层收尾。
- **正确性**：AFL/SymCC使用同一单调union novelty；queue先于claim持久化；批处理保持candidate顺序归因；全局竞争
  失败的已发布child仍登记摘要；旧快照和旧cohort不能污染当前状态。跨shard不是分布式原子事务，崩溃后由queue replay收敛。
- **验证**：专项四模块316 passed加104 subtests；完整门禁1556 passed加579 subtests。机制测试证明协议、
  失败路径与每touched shard/batch至多一次写语义，不推导公开目标coverage或端到端speedup。
- **材料**：[`F457研究报告`](research-progress/Global_Coverage_Convergence_and_Parallel_Conversion_F457_2026-08-26.md)和
  [`机制图`](diagrams/f457-global-coverage-parallel-conversion.svg)、
  [`证据目录`](evidence/f457-global-coverage-convergence-2026-08-26/)。

## 451. F458：并行执行正确性与扩展性深度审查（2026-08-27）

- **问题**：AFL replay失败输入可越过基线门；query和continuation租约未覆盖完整提交；阻塞ULFM shrink
  无法兑现期限；多分片coverage可永久部分提交；删失样本、角色计数和无效拟合会扭曲实验结论；多类showmap
  位于master串行热路径；concolic终止输入与SymSan目标参数可能静默丢失。
- **方案**：稳定文件身份准入和SQLite replay索引；query/continuation提交期心跳加token fencing；仅接受可轮询
  `Ishrink/Iagree`；每批唯一intent WAL、相交shard闭包锁和幂等前滚；右删失concordance、四级规模门禁、真实
  worker轴及`R²`失败关闭；有界异步profile；终态样本持久化；完整SymSan argv协议。
- **正确性**：后台线程不修改权威coverage或调度状态；互不相交shard事务可并行，相交事务恢复闭包保持原子前滚；
  WAL/shard/heartbeat均使用有界稳定读取和独占原子发布；续租核对完整fence且不替代最终token裁决；删失、非单调、
  欠定或低质量模型不产生confirmatory/模型型结论。
- **验证**：GCC/simple lit 249 passed加95 unsupported；最终QSYM lit在192 workers下344 passed加1 unsupported；
  exact widening单项100/100、widening/narrowing成对100/100；最终完整严格Python门禁1576 passed加581 subtests，
  零failure/skip/xfail/xpass/deselect/missing/unexpected node ID，稳定node ID清单1576/1576。
- **边界**：机制和回归已验证；没有新增公共目标长时性能数据。不声明coverage或speedup因果提升。热点shard锁、
  多节点ULFM故障注入、真实共享文件系统kill-point、SQLite索引GC、正式生存分析和样本外规模预测仍是后续工作。
- **材料**：[`F458研究报告`](research-progress/Deep_Correctness_and_Scaling_Review_F458_2026-08-27.md)和
  [`F458可测试版本清单`](research-progress/F458_Testable_Version_Manifest_2026-08-27.md)。

## 452. F459：代码、架构与算法反例审查（2026-08-28）

- **范围**：沿 MPI generation/lease、worker result、coverage authority、QueryStore、
  live-state frontier、规模分析和交付链逐项构造故障反例，而不是只重复 happy-path 测试。
- **发现**：确认 QueryStore 提交后制品窗口、coverage 非法 delta、frontier fence 强度不一、
  规模幸存者偏差、coverage shard 锁放大、单 Master 服务台以及未跟踪源码交付风险。
- **结果边界**：该阶段只形成审查和修复优先级；四模块回归 233 passed 加 51 subtests
  只能证明旧行为可运行，不能否定新构造的跨层反例。
- **材料**：[`F459审查报告`](research-progress/Deep_Code_Architecture_Algorithm_Review_F459_2026-08-28.md)。

## 453. F460：持久事务、规模证据与交付门禁修复（2026-08-28）

- **持久协议**：QueryStore 增加 SQLite outbox 与启动重放；coverage authority 对类型、
  index、bits、重复项和总预算失败关闭；frontier heartbeat/complete/abandon 统一完整 lease
  fence；coverage shard 预取 liveness 并缩短持锁元数据扫描。
- **控制面与实验**：Master 增加有界纯验证 admission；终止样本 digest 改为启动一次扫描、
  运行中增量维护；规模模型记录全部 outcome、验证角色守恒，以配对 bootstrap 和可靠性
  门禁约束 ceiling。
- **交付**：新增 source-delivery manifest/gate，验证普通文件摘要、Git tracked/clean 状态和
  runtime gitlink。门禁能精确报告当前脏工作树，但不等于已经替维护者完成提交。
- **验证**：阶段权威门禁为 1597 passed 加 607 subtests；LLVM 18/17 分别 345/344 passed，
  另有 1/2 项能力标注 unsupported，均无 failure。
- **材料**：[`F460修复报告`](research-progress/Deep_Review_Remediation_F460_2026-08-28.md)。

## 454. F461：并行控制面与热路径收敛（2026-08-28）

- **算法与持久化**：dense coverage delta 改为流式校验、bitset 去重和 `array("Q")` 分片；
  frontier 改为 generation transition log、周期压缩、精确增量尺寸核算与有界 expiry heap；
  QueryStore 启动重放具有条数/时间预算和坏记录隔离。
- **并行路径**：Master admission 改为有界异步、有序提交；多 Master queue winner 压缩为
  无空洞 ID，loser 不再持续堆积；QF_BV timeout 统一子进程与三条 pipe 生命周期；普通
  runtime 对未实现的原生多线程 path solving 明确失败关闭。
- **机制结果**：256 KiB dense delta 峰值约 24.21 MiB 降至 2.08 MiB；25k-state frontier
  hot claim 约 4.0x、hot snapshot 约 956x。它们是本机机制微基准，不是端到端 coverage
  或 solver speedup。
- **验证**：阶段权威门禁 1611 passed 加 607 subtests；LLVM 18/17 分别 346/345 passed，
  另有 1/2 项能力标注 unsupported，均无 failure。
- **修订**：F461 曾要求 `complete()` 的即时文件发布失败向 solver 上抛；F462 的 outbox
  故障注入证明该语义错误，现已改为逻辑提交成功、派生发布异步重试。
- **材料**：[`F461修复报告`](research-progress/Deep_Review_Remediation_F461_2026-08-28.md)和
  [`证据目录`](evidence/f461-control-plane-remediation-2026-08-28/README.md)。

## 455. F462：恢复路径、长跑编号与 outbox 语义闭合（2026-08-30）

- **覆盖恢复**：resume 启动基线由 AFL queue 扩展为 AFL 与已提交 SymCC queue 的具体执行
  并集；versioned 原子 snapshot 在 worker 后处理前刷新，异常版本、载荷和尾随数据稳定拒绝。
- **worker 有界性**：跨任务内容摘要在 concrete showmap 后才提交；hint、候选读取、batch、
  streaming 和 one-shot showmap 共享可选绝对 deadline；`save-all` 使用内容寻址原子发布。
- **corpus 事务**：新增 prepared/decided/commit/recover 覆盖队列事务；AFL ID 解析扩展到
  1--10 位 `uint32`，queue/crash/hang 在耗尽时失败关闭；重复 sparse row 只按 union 计数。
- **QueryStore 与生命周期**：startup reconcile 遵守 `next_retry`；常驻 janitor 支持退避、
  dead letter、requeue 与多轮 drain；`complete()` 以 SQLite commit 为权威，只做 64 条/100 ms
  尽力发布。查询服务推迟到所有 Master 控制对象完成构造后、在统一 `try/finally` 中启动。
- **实验口径**：showmap parser 区分 existing-edge denominator 与 bitmap capacity；规模分析 v5
  强制 coverage 采样 provenance，抽样 endpoint 不具备决策资格。
- **验证**：最终 Python 为 1638 passed 加 609 subtests（332.78 秒），关键耦合回归为
  466 passed 加 172 subtests（107.50 秒）；LLVM 18 为 347 passed 加 1 unsupported，
  LLVM 17 为 346 passed 加 2 unsupported，均发现 348 项且无 failure。Ruff、compileall
  与 `git diff --check` 通过；没有新增公开目标长时 coverage 结论。
- **材料**：[`F462研究报告`](research-progress/Second_Deep_Review_and_Recovery_Closure_F462_2026-08-30.md)。

## 456. F463：第三轮深度审查与失败原子性闭合（2026-08-31）

- **持久事务**：修复 coverage manifest 已 rename、目录 fsync 失败后的悬挂可见状态；
  publication janitor 在 SQLite 写锁内重读权威 attempts，多个并发失败不再 lost update；
  查询达到 solve 上限时，在同一事务提交 `done`、error result 和 publication outbox。
- **覆盖与生命周期**：AFL 失败基线在每个 queue poll 优先重试；一次性 coverage bridge
  确定关闭 executor/index。AFL command line 使用 `shlex`，支持引号 argv 和嵌入 `@@`，
  空命令或无 target 失败关闭。异步查询服务运行中退出会禁用依赖，并以独立 session 的
  TERM/KILL 进程组清理覆盖 solver descendants。
- **规模证据**：USL/coverage ceiling 只比较真实 `n -> 2n`，有限 horizon 不再制造伪饱和；
  零方差 R² 明确拒绝；retention 使用总计数比并拒绝 `unique > generated`；CSV 整数通过
  `Decimal` 精确解析为 `uint64`，保留大于 `2^53` 的执行计数；超大整数和乘积溢出在拟合前拒绝。
- **验证**：受影响组合 157 passed 加 78 subtests；最终 Python 为 1650 passed 加
  615 subtests（350.77 秒）；Ruff、`py_compile` 与 `git diff --check` 通过。本轮未修改 LLVM
  代码，也没有新增公开目标长时 coverage、solver speedup 或缺陷发现率结论。
- **材料**：[`F463研究报告`](research-progress/Third_Deep_Review_and_Failure_Atomic_Closure_F463_2026-08-31.md)。

## 457. F464：语义、传输与缩放闭合（2026-09-03）

- **符号语义**：GEP 按 address-space index width 对源索引做符号扩展/截断，在低位宽内回绕并保留
  指针高位；结构成员偏移保留为 64 位，避免超过 4 GiB 时截断；元素步长改用 allocation size，
  scalable vector 纳入运行时 `vscale`。
- **并行控制面**：hybrid RESULT 正常路径改为内容寻址对象引用，Master admission 和 triage 两次稳定
  复验；候选与 proposal 共享 coverage 行预算，worker coverage 与内容去重整批提交；AFL coverage
  失败项写入持久 ledger，单槽交替、多槽预留并回填；data coverage 使用 `pthread_once`、重入安全
  raw compare 和 never-zero 原子 map 更新。
- **实验可信度**：hybrid CSV 显式记录五类后台池的 `auxiliary_compute_slots`；资源上限扣除协调者和辅助
  计算；重复轮次以充分统计保留组内 SSE；paired bootstrap 缓存相同 seed-count 拟合。
- **生命周期与交付**：启动配置解析总化；无 worker、输出目录/队列锁冲突及 Master 异常均进入有界
  STOP/ACK 且保持失败状态；source-delivery manifest/gate 校验普通文件摘要、Git 跟踪状态和 runtime
  gitlink。
- **验证边界**：定向冻结前 coverage 9 passed、规模/评估 32 passed、RESULT/worker coverage
  17 passed加26 subtests，LLVM 17/18 GEP 各 1 passed；完整Python 1668 passed加616 subtests；
  LLVM18为348 passed加1 unsupported，LLVM17为347 passed加2 unsupported。没有新增公共目标长时
  coverage、solver speedup 或缺陷发现率结论。
- **材料**：[`F464研究报告`](research-progress/Fourth_Deep_Review_Semantic_Transport_Scaling_F464_2026-09-03.md)。

## 458. F465：求解并行度与位向量语义闭合（2026-09-04）

- **查询调度**：保留`prefix_id % jobs`本地优先亲和性；本地没有可领取任务时，在同一
  `BEGIN IMMEDIATE`事务内按原遍历顺序跨分片窃取。共同根前缀不再把全部查询固定到单个
  persistent solver，更新仍由query ID、owner和递增lease token栅栏线性化。
- **租约协议**：自动续租在取得SQLite写锁后重新取时钟，避免锁等待吞掉短租约的大部分有效期；
  显式`now`仍保留确定性故障测试语义，完成时继续执行最终owner/token/deadline裁决。
- **QF_BV语义**：本地候选复验按SMT-LIB定义处理零除数：`bvudiv`返回全1，`bvurem`和
  `bvsrem`返回被除数，`bvsdiv`按被除数符号返回全1或1。该改动只修复独立model复验，
  不放宽SAT候选必须满足完整Query IR的可信边界。
- **运行时与构建**：饱和算术min/max常量不再执行`1ULL << 64`；1--64位使用有界常量，
  更宽且运行时ABI可表示的位宽使用位向量not/shift构造。benchmark构建data coverage
  interposer显式加入`-pthread`，与其`pthread_once`实现和单元构建口径一致。
- **测试身份**：规范pytest清单从1597项重建为当前真实1671项；净增74项包含此前F464提交中
  未同步的测试和本轮2项新增测试，不能把全部差额归因于F465。
- **验证与边界**：聚焦3 passed加22 subtests；QueryStore 41 passed加50 subtests；AFL编排
  77 passed加42 subtests；完整Python 1671 passed加638 subtests；LLVM18为349 passed加1
  unsupported，LLVM17为348 passed加2 unsupported。上述结果证明语义、调度容量和生命周期
  反例闭合，不证明公共目标coverage、solver speedup或defect yield提升。
- **材料**：[`F465研究报告`](research-progress/Fifth_Deep_Review_Query_Parallelism_and_Bitvector_Semantics_F465_2026-09-04.md)和
  [`证据目录`](evidence/f465-query-parallelism-bitvector-semantics-2026-09-04/README.md)。
