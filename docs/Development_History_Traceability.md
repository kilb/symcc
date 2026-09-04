# SymCC-Parallel 开发历史与实现追踪矩阵

- 文档状态：持续维护
- 审计日期：2026-07-30
- 提交范围：`7dc2ee6^..146e01b`，共 139 个项目定制提交
- 未提交范围：2026-07-24--07-30 工作树中的 F00-F16、F26-F296

## 1. 目的与覆盖口径

本文回答“从并行符号执行框架开始开发以来，实现是否都有文档记录”：

- 以提交历史为时间轴，保证早期架构、性能优化、评测修正、交付和 SymSan 工作
  不因后续 SOTA 实现而丢失；
- 以 F00-F296 为能力轴，将提交映射到当前权威技术档案；
- 以代码、自动化测试、benchmark artifact 和专题报告为证据轴；
- 显式区分实现完成度与科研验证强度。

审计结论：上述 139 个提交均落入 P01-P07 的连续区间，当前工作树落入 P08-P10；
全部能力归入 F00-F296。一般修复、审查、构建和文档提交归入其所在阶段；本轮跨层
正确性加固因改变可信边界，单列 F26。不是每个小修复都单独创建功能 ID。
F27 单列持久化 Query IR 和异步求解服务，F28 单列完整 static Data Coverage，
F29 单列 reusable solution generator 与原生 full-matrix polytope mutator，F30 单列
string-constraint artifact 与 offset-aware string candidate，F31 单列 asynchronous
solver portfolio 与 disagreement telemetry，F32 单列 PSCache-inspired partial
solution cache 与 Query IR verified reuse，F33 单列 SymCC-str phase-2 string theory
backend 与 MPI/CLI materialization，F34 单列 bounded parallel solver portfolio
racing，F35 单列 solver-helper internal PSCache assignment probing，F36 单列
Lase/Cottontail-inspired token grammar solve-complete proposals，F37 单列
IFSS/Hydra-style targeted transformation proposals，F38 单列 GenSym-style live
continuation checkpoint descriptors，F39 单列 ConDPOR-style HB/lockset schedule
conflict analysis，F40 单列 ConDPOR-style memory read/write trace instrumentation，
F41 单列 schedule constraint artifact export，F42 单列 Query IR × schedule artifact
joint replay validation，F43 单列 schedule memory provenance filtering，F44 单列
schedule memory provenance tags and artifact propagation，F45 单列 bounded SC
schedule-SMT replay-prefix encoding，F46 单列 mutex/rwlock lifecycle-state
enabledness constraints，F47 单列 condition-variable wait mutex split 与
signal/broadcast wake witness，F48 单列稳定 thread identity、create/start/exit/join
operational enabledness 与 lifecycle HB，F49 单列 lifecycle partial-order /
linear-extension schedule-SMT v5、严格双位置顺序连接与 legacy permutation 消融，
F50 单列 scaled-anchor 线性连接，F51 单列构造式 linear-extension certificate
checker，F52 单列 verified topology-to-runtime replay materialization，F53 单列
violation-driven lazy section refinement，F54 单列 direct system-libz3 model/
certificate solver，F55 单列 detach/cancel/join-cancelled identity retirement，
F56 单列 bounded Source-DPOR certificate/source/sleep sets，F57 单列 Query IR ×
schedule × read-from single-context solve，F58 单列 bounded SC/TSO/RA memory
models，F59 单列 content-addressed solver/store/page-COW live state，F60 单列
executable research-evidence bundle 与 claim gates，F61 单列 bounded
Optimal-DPOR wakeup tree/cooperative ready evidence，F62 单列 bounded ConDPOR
execution graph/backward revisit/maximal extension，F63 单列 path-dependent
event regeneration，F64 单列 operational enabledness certificate，F65 单列
native atomic trace/extended RA，F66 单列 executable continuation IR/MPI
state execution，F67 单列 sealed randomized research protocol/paired statistics，
F68 单列 conservative LLVM-to-continuation lowering/QF_BV feasibility pruning，
F69 单列 object-aware static global memory/multi-byte COW lowering，
F70 单列 explicit-size symbolic input-buffer entry ABI，F71 单列 frame-local fixed
stack objects、initialization tracking 与 frame reclamation，F72 单列 bounded
call-site heap objects、alloc/free lifetime 与 checkpoint marker，F73 单列 bounded
symbolic GEP alias enumeration、finite ITE memory 与 conditional init marker，
F74 单列 guarded pointer PHI/select provenance union 与 cross-object memory cases，
F75 单列 bounded multi-instance heap identity、canonical first-free pool 与
GEP-after-union propagation，F76 单列 bounded cross-function pointer argument/
return summary、owner-frame stack lifetime 与 signature contract，F77 单列
CAS-rooted state-local incremental solver context、parent delta 与 MPI worker
lifetime reuse。
完整方案与边界以
[`New_Implementation_Archive.md`](New_Implementation_Archive.md) 为准。

### 1.1 结果等级

| 等级 | 判定条件 |
| --- | --- |
| I | 主路径接入真实系统 |
| T | 有单元、lit、集成或部署测试 |
| B | 有明确目标、预算和环境下的本地测量 |
| R | 等 CPU、多轮、置信区间、效应量和逐项消融 |

历史报告中的单次或少轮数字统一保留为 B，不追溯升级为 R。

## 2. 提交区间全覆盖

下表区间首尾均包含。P01-P07 连续覆盖命令
`git log --reverse 7dc2ee6^..146e01b` 的全部 139 个提交；P08 是尚未提交的当前
实现，不能用 commit hash 冒充版本边界。

| 阶段 | 日期 | 提交区间/状态 | 主要工作 | 功能映射 |
| --- | --- | --- | --- | --- |
| P01 | 2026-02-26--02-28 | `7dc2ee6` ... `5b86564` | MPI 原型、benchmark、公开目标、coverage/crash 口径、多 master 与协议修复 | F17, F21 |
| P02 | 2026-03-10--03-31 | `1ecbc48` ... `ab8eb85` | LAVA-M/Google FTS、AFL hybrid 闭环、worker bitmap、streaming showmap、扩展评测 | F17, F18, F21 |
| P03 | 2026-04-01--04-20 | `090480c` ... `969b333` | synthetic/public scaling、dictionary、CmpLog、full benchmark、负结果与报告校正 | F18, F20, F21 |
| P04 | 2026-07-02 | `0257685` ... `23ed66b` | hybrid 加固、persistent/shmem、focus partition、work stealing、密度平衡 | F18, F19, F20 |
| P05 | 2026-07-03--07-13 | `4c2b85a` ... `f02b1c0` | 质量审计、one-command setup、无 Git/offline 包、bare-machine 验证 | F25 |
| P06 | 2026-07-15--07-18 | `e04e8a2` ... `ee4d03c` | phase timing、SIGTERM 归因、冗余 funnel、内容预去重与 batch showmap | F22 |
| P07 | 2026-07-19--07-20 | `a358c0a` ... `146e01b` | solver telemetry、双引擎、SymSan 技术迁移、RGD/JIGSAW、真实目标、离线打包 | F23, F24, F25 |
| P08 | 2026-07-24--07-28 | 当前工作树 | F00-F16 新实现、跨层正确性加固、持久化异步求解、static data coverage、reusable solution generator、string constraint artifact、solver portfolio、partial solution cache、string theory backend、parallel solver portfolio racing、solver-helper PSCache、token grammar solve-complete proposals、targeted transformation proposals、live continuation descriptors、HB/lockset schedule conflict analysis、memory read/write schedule trace、schedule constraint artifact export、Query IR × schedule joint replay validation、schedule memory provenance filtering/tagging、bounded schedule-SMT、pthread lifecycle、partial-order/scaled-anchor/certificate/topology replay、lazy/direct solving、detach/cancel retirement、bounded Source-DPOR、single-context path/schedule/RF、extended C11 RA、CAS/page-COW live state、executable evidence gates、bounded wakeup tree、ConDPOR graph/revisit、path-dependent regeneration、operational enabledness、executable continuation IR/MPI state execution、sealed research protocol、conservative LLVM continuation lowering、static-global/input-buffer/fixed-stack/runtime-sized heap、finite symbolic alias ITE、guarded pointer provenance union、bounded multi-instance heap identity、cross-function domain certificate、pointer-returning bounded indirect dispatch、external memory compare/region-write/NUL-string/search/copy/scalar/bit-count/permutation/saturation/selection/objectsize summaries、overflow aggregates与ssa.copy、optimization-hint identity、guarded memory read/write domain、LLVM defined-value guards与cast/BV/icmp/select/PHI/straight-line-memory transitive deferred poison、canonical-address/acyclic/cyclic-CFG finite memory-version poison与interprocedural/initial/subobject/carry/conditional/forwarded/multiarm/equivalent-defined/shared-poison/nested/recursive/grouped/repeated-source/multi-carry/multi-cell/static/heap/finite/select/PHI-domain-transfer edge-definedness PHI、byte-lane nearest-writer composition、direct-predecessor lane PHI、alias-closed cyclic fixed point与direct/forwarded/multiarm/forwarded-multiarm/recursive/forwarded-recursive/grouped-recursive/repeated-source-recursive/composed-repeated-source-recursive/multicarry-composed-recursive/multigroup-recursive/three-group recursive conditional lane carry、bounded multi-direct-call defined-bit ABI、transitive argument-to-return composition与all-use freeze-sink proof、bounded symbolic data/function pointer-memory、global initial/runtime definition merge与pointer tables、cyclic pointer-memory SSA fixed point与canonical cell identity、proven-nounwind invoke、stable nondeterministic freeze、dynamic/realloc objectsize、state-local incremental solver context、validated cross-prefix/exact-projected/cross-size-endian/exact-width field-renamed polyhedral reuse、verifier-gated optimistic generator simplification、native Z3 tactic model conversion、persistent Query IR converter replay、conditional contextual self-configuration与feedback-validated online token grammar | F00-F16, F26-F187 |
| P09 | 2026-07-28--07-29 | 当前工作树（P08续） | fail-soft selective dependency partition、persistent contextual cost/timeout predictor、mixed completion、Query-IR-verified grammar hole、plateau-gated retained-history acquisition、independent parser oracle、parser-state/nonterminal structural path、multi-slot/mutual CFG derivation、incremental ECT subtree correspondence、九维Pareto grammar/ECT调度、六维seed metadata archive、bounded packed parser families、shared packed DAG/deep ECT、nullable SCC least fixed point、atomic multi-nonterminal ECT transaction、content-addressed incremental parser cache、persistent native Tree-sitter incremental parser与node-ID reuse proof、proof-carrying parser telemetry与sealed cold/incremental cost ablation、persistent generalized Earley与GLR complete-SPPF adapters、proof-carrying forest telemetry与cross-parser calibration、candidate-paired dual-parser confusion proof、parser-neutral span/boundary、proof-carrying symbol/production correspondence及bounded exhaustive language differential、proof-carrying assumption-conflict PSCache、proof-carrying backend-neutral QF_BV lowering/capability matrix、cvc5模型双验证与prefix-keyed persistent SMT-LIB context、proof-carrying X-means/BIC删失PAR-2 sequence prior、bagged/boosted stage-budget-aware sequence optimizer、严格预算schedule-level EI/SMBO acquisition、SAT-gated bounded-consensus portfolio cancellation、Query IR structural feature schema v2及v1 artifact/state迁移、固定Bitwuzla 0.9.1与可重放三后端QF_BV可信矩阵、sealed三solver paired holdout/PAR-2/child-CPU artifact与离线verifier、train-leakage-checked F240/F241重建及六臂QF_BV strategy/cancellation campaign、sealed AFL edge/data replay join与v1/v2 holdout calibration、profile-guided single-site Hydra LCS melding、aggressive memory readback、fork-eliding ITE metadata与sealed original replay/denylist、有界多臂IFSS region-state/worklist、相关谓词合成、符号PHI替换与原子IR回滚、MemorySSA/AA MustAlias双臂write-state ITE、稳定site proof metadata与undef/poison fail-closed、bounded NoMod MemoryDef chain与arm-local proof site、data/memory共享多臂partition、逐arm path/chain proof和N-1 memory ITE、partition-scoped condition cache、逐condition short-circuit ownership与dominance proof、bounded normal-return exit-state lowering、跨调用return ITE与arm/site proof、bounded switch equality chain、case/default proof及data/memory/return state复用、unsigned range-balanced switch tree、default multi-gap PHI与depth/size trade-off、profile-weighted optimal alphabetic switch tree与分类fallback proof、shared-destination edge/PHI multiplicity及两臂完整路径partition、LLVM branch-weight import与独立可验证switch tree manifest、accepted-forest PCFG posterior/inside-outside scheduling、relation-aware synchronized slot调度与exploration reserve、hierarchical parent/grandparent PCFG circuit、prequential/recency/adaptive、context-wise anytime与cross-context online-FWER calibration、ordered-sibling与second-order history autoregressive factor及精确factor inside/outside、五档cost-faithful PCFG消融与sealed artifact、portable signed-integer schedule SMT evidence、fail-closed grammar-hole QueryStore lifecycle、proof-carrying epsilon derivation与独立grammar bitmap、exact String/BV dual view、guarded runtime string operations、validation-first parallel String portfolio、overflow-proof width-bound atoi/strtol/strtoul conversion、Z3/cvc5可执行String conformance、validation-aware上下文backend选择与三层验证 | F188-F259 |
| P10 | 2026-07-29--07-30 | 当前工作树（P09续） | bounded multi-continuation exit-id/scalar-live-out tuple、capture/dispatch/resume CFG、重复edge multiplicity、强制完整IFSS partition；proof-carrying independent/upper-triangular affine natural-loop closed form、single/multi post-update break priority exit tuple、PHI事务与recurrence/exit manifests；MemorySSA/AA continuation memory tuple、bounded NoMod chain、store/LiveOnEntry partial state、path-local snapshot、bounded acyclic nested-MemoryPhi provenance tree、constant-offset byte-lane partial-overlap重建、guarded two-arm alias partition、canonical/conditional/two-latch loop-carried byte-lane fixed point、depth-2 finite pointer union与two-write priority；replay-verified CFG/tuple manifest、actual nested/cyclic/guarded-cycle/multi-latch/select-tree/dispatch-PHI/value-expression validation、独立verifier、atomic SHA-256 IR/compiler/LLVM seal、独立bitcode MemorySSA/AA replay；bounded equal/unequal linear-arm及path-predicate internal-tree Hydra alignment、v1/v2/v3结构manifest/campaign gate、原程序coverage replay；continuation/loop/Hydra统一write-once seal、input-IR exact transformation replay、manifest/IR双重相等与TOCTOU重验；差分重放、拒绝矩阵与科研文档 | F260-F281 |

### 2.1 可重复审计命令

```bash
git rev-list --count 7dc2ee6^..146e01b
git log --reverse --format='%h|%ad|%s' --date=short \
  7dc2ee6^..146e01b
git status --short
```

第一条必须输出 `139`。若后续新增提交，应扩展阶段表，而不是修改已经发布的历史
区间含义。

## 3. 阶段到代表提交

这里列出改变架构或实验口径的代表提交。未列出的 bugfix、review、merge 和文档
提交仍由上一节的连续区间覆盖。

| 阶段 | 代表提交 | 可追踪决策 |
| --- | --- | --- |
| P01 | `7dc2ee6`, `7452b7b`, `524d0e6`, `ff86d0d` | MPI master/worker；coverage/crash；master 吞吐；multi-master |
| P02 | `dcb4c5f`, `7c0c7db`, `7e5ec97`, `c979241`, `276cf01` | AFL baseline/hybrid；双向反馈；worker bitmap；streaming showmap；worker dedup |
| P03 | `201517d`, `fa430f0`, `59197d0`, `771e485`, `969b333` | SQLite；dictionary；full benchmark；CmpLog；CPU contention 结论修正 |
| P04 | `a4b4842`, `1b5913c`, `840ab65`, `01f762a`, `23ed66b` | persistent/shmem；细粒度分解；stealing；密度平衡；runtime 更新 |
| P05 | `e882aa4`, `f0d64fe`, `5733c06`, `7a8aeeb`, `f02b1c0` | setup；无 Git 包；air-gap；审查加固；bare-machine 修复 |
| P06 | `e04e8a2`, `1d90074`, `5df64e1`, `bd70017`, `ee4d03c` | phase timing；中断归因；冗余漏斗；根因拆分；batch triage |
| P07 | `c27512a`, `4615505`, `bf7cc99`, `5cd83d1`, `3f44011`, `146e01b` | engine API；full hybrid；RGD；runtime 修复；6-target；SymSan offline |

## 4. 功能到代码、验证与文档

### 4.1 当前跨层实现 F00-F16、F26-F281

| ID | 权威代码 | 自动化/运行证据 | 详细文档 | 状态 |
| --- | --- | --- | --- | --- |
| F00 | `compiler/Symbolizer.cpp`, `runtime/src/RuntimeCommon.cpp` | `pointer_width_casts.ll`, lit 回归 | 技术档案 3.1 | I/T |
| F01 | QSYM `solver.*`, `util/hybrid_feedback.py`, `util/executor_portfolio.py` | `telemetry.c`, 两组 Python 测试 | 技术档案 4 | I/T |
| F02 | `compiler/Symbolizer.*`, `compiler/Pass.cpp`, QSYM `solver.*` | `backsolver_*.ll`, `veritesting_memory_reject.ll` | 技术档案 5 | I/T |
| F03 | QSYM `solver.*` | `poly_cache.c`, `unsat_core_cache.c` | 技术档案 6 | I/T |
| F04 | `compiler/Pass.cpp`, `runtime/src/LibcWrappers.cpp`, `util/afl_data_coverage_rt.c` | `data_coverage_libc.c`, `test_afl_data_coverage.py` | 技术档案 7 | I/T |
| F05 | QSYM `solver.*`, `util/hybrid_feedback.py`, MPI helper | `s2f_actionseed.c`, hybrid feedback tests | 技术档案 8 | I/T |
| F06 | `compiler/Pass.cpp`, `util/merge_directed_distance.py`, `util/structural_tasks.py` | directed/concurrency/structural tests | 技术档案 9 | I/T |
| F07 | `util/expressive_coverage.py`, `util/path_cover.py` | 对应 Python tests | 技术档案 9 | I/T |
| F08 | `util/hybrid_feedback.py`, `util/adaptive_components.py`, `util/self_config.py` | 三组 Python tests | 技术档案 10 | I/T |
| F09 | `util/smt_algorithm_scheduler.py` | `test_smt_algorithm_scheduler.py` | 技术档案 10 | I/T |
| F10 | semantic/agentic/verified proposal utilities | 四组 Python tests | 技术档案 11 | I/T |
| F11 | `compiler/UCSan.*`, runtime UCSan, `util/ucsan_seed.py` | `ucsan_jit.c`, seed tests | 技术档案 12 | I/T |
| F12 | `util/distributed_state.py`, MPI helper | `test_distributed_state.py` | 技术档案 13 | I/T |
| F13 | `util/symcc_schedule_rt.c`, `util/schedule_exploration.py` | `test_schedule_exploration.py` | 技术档案 14 | I/T |
| F14 | `util/afl_symcc_hint_mutator.py`, benchmark runner | hint/profile/tace tests | 技术档案 15 | I/T |
| F15 | `util/offline_policy.py`, benchmark runner | `test_offline_policy.py` | 技术档案 11 | I/T |
| F16 | benchmark runner 与 `analyze_ablation.py` | `test_ablation_analysis.py` | 技术档案 16 | I/T |
| F26 | solver、SiteId、UCSan、hybrid feedback、distributed state | modular/cache/fast/libc/UCSan/site/fencing 回归 | `Correctness_Review_Fixes_2026-07.md`、技术档案 21 | I/T |
| F27 | Query IR exporter、content-addressed store、persistent Z3 service、MPI helper | `test_query_store.py`, `query_prefix_reuse.py`, `query_ir.c` | 技术档案 22、SOTA 差距审计 G01 | I/T |
| F28 | LLVM static provenance、ELF segment registry、data novelty/dominance | `data_coverage_static.c`, `data_coverage_dso.c`, hybrid feedback tests | 技术档案 23、SOTA 差距审计 G03 | I/T |
| F29 | reusable generator IR、query-solver range sampler、query-store generator artifacts、full-matrix AFL mutator | `test_solution_generator.py`, `test_query_store.py`, `test_afl_hint_mutator.py`, `query_generator.py` | 技术档案 24、SOTA 差距审计 G02/G18 | I/T |
| F30 | string-constraint runtime artifact、ReadExpr offset introspection、string candidate materializer、AFL string stage | `test_string_constraints.py`, `test_afl_hint_mutator.py`, `string_constraints.c` | 技术档案 25、SOTA 差距审计 G04 | I/T |
| F31 | portfolio solver runner、query-service portfolio config、attempt/disagreement result schema | `test_query_store.py` | 技术档案 26、SOTA 差距审计 G06 | I/T |
| F32 | partial-solution cache、bounded Query IR evaluator、query-IR verified partial candidates | `test_query_store.py` | 技术档案 27、SOTA 差距审计 G07 | I/T |
| F33 | backend-neutral string-query schema、Z3 generic string backend、MPI/CLI string materialization | `test_string_constraints.py`, `string_solver.py`, CLI smoke | 技术档案 28、SOTA 差距审计 G04 | I/T |
| F34 | bounded parallel portfolio racing、portfolio mode/parallelism telemetry、service parallelism option | `test_query_store.py` | 技术档案 29、SOTA 差距审计 G06 | I/T |
| F35 | persistent solver-helper recent SAT assignment probe、solver_pscache telemetry、QueryStore stats | `query_solver_pscache.py`, `test_query_store.py` | 技术档案 30、SOTA 差距审计 G07 | I/T |
| F36 | token grammar rule synthesis、solve_complete semantic proposals、grammar fragment state | `test_semantic_proposals.py` | 技术档案 31、SOTA 差距审计 G08/G11 | I/T |
| F37 | IFSS-like relevance slices、Hydra-style targeted core transforms、verified proposal kind | `test_semantic_proposals.py` | 技术档案 32、SOTA 差距审计 G09 | I/T |
| F38 | canonical live continuation descriptor、checkpoint id、state-task payload integration | `test_distributed_state.py` | 技术档案 33、SOTA 差距审计 G05 | I/T |
| F39 | SC vector-clock annotation、lockset memory-conflict pruning、source-style schedule replay prefixes | `test_schedule_exploration.py` | 技术档案 34、SOTA 差距审计 G10/G13 | I/T |
| F40 | compiler-emitted schedule read/write notifications、preload byte-granular memory trace rows、no-op runtime ABI | `schedule_memory_trace.c` | 技术档案 35、SOTA 差距审计 G10/G13 | I/T |
| F41 | `symcc-schedule-constraint-v1` artifact、shared replay-prefix proposal path、MPI JSONL export | `test_schedule_exploration.py` | 技术档案 36、SOTA 差距审计 G10/G13 | I/T |
| F42 | `symcc-joint-schedule-query-v1` artifact、Query IR bounded evaluator replay validation、query-service/MPI export | `test_query_store.py` | 技术档案 37、SOTA 差距审计 G10/G13 | I/T |
| F43 | stack/current-owner memory trace filtering、compressed shared-transition evidence、bounded owner table | `test_schedule_exploration.py` | 技术档案 38、SOTA 差距审计 G10/G13 | I/T |
| F44 | memory provenance trace tags、schedule artifact provenance counts、joint validation propagation | `test_schedule_exploration.py`, `test_query_store.py` | 技术档案 39、SOTA 差距审计 G10/G13 | I/T |
| F45 | `symcc-schedule-smt-v1`、SC event-position/PO/prefix/conflict-reversal SMT-LIB2、observed-HB assumptions、incremental context reuse、MPI JSONL export | `test_schedule_exploration.py`（含 libz3 parse/solve） | 技术档案 40、SOTA 差距审计 G10/G13 | I/T |
| F46 | `symcc-schedule-smt-v2`、lock outcome/release pairing、mutex/rwlock complete-section nonoverlap、trylock busy assumptions、rwunlock HB fix | `test_schedule_exploration.py`（SAT/UNSAT + real preload trace） | 技术档案 41、SOTA 差距审计 G10/G13 | I/T |
| F47 | `symcc-schedule-smt-v3`、wait mutex release/reacquire trace、condition lifecycle recovery、signal one-to-one/broadcast reusable wake witness、timeout ownership restoration | `test_schedule_exploration.py`（29 项；condition SAT/UNSAT + real pthread wake/timeout preload trace） | 技术档案 42、SOTA 差距审计 G10/G13 | I/T |
| F48 | `symcc-schedule-smt-v4`、logical child identity、cleanup-based thread exit、create-before-start、exit-before-join-success、create/join HB | `test_schedule_exploration.py`（33 项；thread SAT/UNSAT + real pthread_exit preload trace） | 技术档案 43、SOTA 差距审计 G10/G13 | I/T |
| F49 | `symcc-schedule-smt-v5`、lifecycle partial-order/linear-extension、严格 `pos_*`/`sync_ord_*` 顺序连接、legacy permutation 消融 | `test_schedule_exploration.py`（35 项；partial/permutation differential SAT/UNSAT + strict link）；64-thread B 级压力测量 | 技术档案 44、SOTA 差距审计 G10/G13 | I/T/B |
| F50 | `symcc-schedule-smt-v6`、`sync_ord_i=(L+1)*pos_j` scaled anchors、linear actual link count、legacy pairwise ablation | `test_schedule_exploration.py`（38 项；24-event count/SAT + 64-thread pressure） | 技术档案 45、SOTA 差距审计 G10/G13 | I/T/B |
| F51 | `symcc-lifecycle-order-ir-v1`、constructive topological extension、独立 source-rank/query/choice/digest checker | model/query/tamper certificate tests | 技术档案 46、SOTA 差距审计 G10/G13 | I/T |
| F52 | model/observed certificate 到 controlled logical-thread prefix 投影、CLI/atomic writer | CLI materialization + real pthread preload replay | 技术档案 47、SOTA 差距审计 G10/G13 | I/T |
| F53 | v7 exact/relaxed 双基线、model-violation choice 检查、C API AST 按需 assert、eager 消融 | exact/relaxed SAT/UNSAT、round limit、64-section 2016-choice 压力消融 | 技术档案 48、SOTA 差距审计 G10/G13 | I/T/B |
| F54 | system-libz3 ctypes binding、model completion extraction、solve result/certificate/prefix CLI、digest gate | direct model/certificate、tamper、CLI integration | 技术档案 49、SOTA 差距审计 G10/G13 | I/T |
| F55 | create-before-call registry reservation、detach/cancel interposition、join cancellation cleanup、explicit retirement edges | real detach/cancel preload、4104 detached threads 全 mapped/recycled | 技术档案 50、SOTA 差距审计 G10/G13 | I/T/B |
| F56 | bounded dependency graph、Mazurkiewicz equivalence certificate、causal source set/wakeup sequence、persistent sleep sets | independent/dependent swap、source causality、tamper、persistence tests | 技术档案 51、SOTA 差距审计 G10/G13 | I/T |
| F57 | Query IR QF_BV、schedule QF_LIA 与 read-from/byte bridge 的单一 libz3 context；QueryStore 持久结果 | disjoint-SAT/joint-UNSAT、joint-SAT、Query IR evaluator/certificate recheck | 技术档案 52、SOTA 差距审计 G10/G13 | I/T |
| F58 | authoritative RF/coherence constraints、SC/TSO/RA 参数化 bounded semantics、MPI environment plumbing | SB、TSO forwarding、RA release/acquire litmus | 技术档案 53、SOTA 差距审计 G10/G13 | I/T |
| F59 | canonical JSON SHA CAS、parent-linked solver frames、symbolic store、fixed-page COW memory、continuation bundle CLI | fork/restore/diff、page sharing、tamper/missing reference tests | 技术档案 54、SOTA 差距审计 G05/G13 | I/T |
| F60 | versioned executable evidence、backend oracle/capability detection、semantic claim gates | backend/digest/tamper/CLI evidence tests | 技术档案 55、SOTA 差距审计实验门槛 | I/T |
| F61 | bounded weak-initial wakeup tree、cooperative ready snapshots、state schema v3 | weak-initial/ready/tamper/persistence + real replay tests | 技术档案 56、SOTA 差距审计 G10/G13 | I/T |
| F62 | bounded po/rf/co execution graph、backward revisit、maximal extension、state schema v4 | cycle/protection/extension/tamper/revisit persistence tests | 技术档案 57、SOTA 差距审计 G10/G13 | I/T |
| F63 | compiler/runtime constraint/action trace、read-byte overlap、path-dependent suffix regeneration | lit memory trace + dependency/regeneration tests | 技术档案 58、SOTA 差距审计 G10/G13 | I/T |
| F64 | ready operation offers、abstract pthread state recovery、enabled/blocked/unknown/terminal certificate | synthetic/tamper/wait + real pthread replay | 技术档案 59、SOTA 差距审计 G10/G13 | I/T |
| F65 | pre-LowerAtomic instrumentation、atomic ABI、RMW/release sequence/fence/SC/race/mixed-size bounded RA | atomic lit + weak-memory unit tests | 技术档案 60、SOTA 差距审计 G10/G13 | I/T |
| F66 | executable continuation IR、CAS PC/solver/store/memory resume、symbolic fork、MPI frontier lease | `test_distributed_state.py` pause/fork/call/COW/CLI | 技术档案 61、SOTA 差距审计 G05/G13 | I/T |
| F67 | paired randomized-block manifest、provenance/equal-CPU executor、coverage AUC/censoring、paired statistics | `test_research_protocol.py`, `test_ablation_analysis.py` | 技术档案 62、实验门槛 | I/T |
| F68 | legacy/new-PM LLVM exporter、integer SSA/CPS lowering、PHI edge copies、QF_BV branch feasibility、C/LLVM/CLI/MPI plumbing | `live_continuation_lowering.ll`, `live_continuation_source.c`, `test_distributed_state.py` | 技术档案 63、SOTA 差距审计 G05 | I/T |
| F69 | static global object layout、initializer serialization、constant GEP、multi-byte endian-aware symbolic COW、runtime object recheck | memory lowering lit/C source、big-endian/symbolic-address/readonly unit tests | 技术档案 64、SOTA 差距审计 G05 | I/T |
| F70 | exact `(ptr,size)` entry detection、symbolic input COW mapping、input-length root、logical bounds/capacity contract | LLVM/C pointer-size lowering、CLI、capacity/logical-OOB/tamper tests | 技术档案 65、SOTA 差距审计 G05 | I/T |
| F71 | fixed alloca layout、dominating full-width initialization、per-depth CAS markers、top-frame liveness、return reclamation | LLVM/C stack lowering、sequential reentry、pause/resume、uninitialized/inactive/recursive/dynamic rejection | 技术档案 66、SOTA 差距审计 G05 | I/T |
| F72 | fixed call-site malloc layout、heap alloc/free ops、live/init CAS markers、free reclamation、bounded reallocation | LLVM/C heap lowering、pause/resume、reallocation、UAF/double-free/uninitialized/dynamic/interior rejection | 技术档案 67、SOTA 差距审计 G05/G14 | I/T |
| F73 | one-term dynamic/nested GEP decomposition、object alias enumeration、signed no-wrap index domain、finite ITE load/guarded byte store、conditional init markers | LLVM/C alias lowering、branch fork、wrapped-index pruning、overlapping store、heap checkpoint、limit/width/contract rejection | 技术档案 68、SOTA 差距审计 G05/G13 | I/T |
| F74 | recursive select/PHI pointer alternatives、edge discriminator、cross-object `alias_cases`、case-local guard/index/lifetime checks、union-wide bound | LLVM/C pointer select/PHI/dynamic arms/null pruning、cross-object load/store、cycle/total-limit/artifact rejection | 技术档案 69、SOTA 差距审计 G05/G13 | I/T |
| F75 | per-site fixed-capacity pool、stable slot metadata、canonical first-free allocation、slot-guarded provenance、GEP-after-union、pool validator | LLVM/C same-site multi-live、pool exhaustion、checkpoint markers、missing/reordered slot rejection、union-after-GEP | 技术档案 70、SOTA 差距审计 G05/G14 | I/T |
| F76 | reachable direct-call pointer summary、formal/result address guards、pointer signature schema、owner-frame stack marker、callee-stack escape rejection | LLVM/C arg/return/echo、caller-stack checkpoint、heap return/free、signature tamper、symbolic-actual rejection | 技术档案 71、SOTA 差距审计 G05/G13 | I/T |
| F77 | CAS solver-root context identity、exact push/pop、parent-frame derivation、cold rebuild、persistent worker executor、circuit-breaker fallback | six-query structural counts、warm/cold checkpoint resume、one-shot equivalence、legacy QueryStore server | 技术档案 72、SOTA 差距审计 G01/G05 | I/T |
| F78 | nullable first-free heap、runtime logical size、conditional live/init、calloc overflow/zero、bounded in-place realloc | LLVM/C malloc/calloc/realloc、OOM/exhaustion/logical-OOB/narrow-width/reuse tests | 技术档案 73、SOTA 差距审计 G05/G14 | I/T |
| F79 | caller-domain BV certificate、hidden pointer-domain ABI、finite symbolic actual expansion、pointer-return domain forwarding | LLVM/C cross-function symbolic GEP、false-domain pruning、argument/return mapping tamper tests | 技术档案 74、SOTA 差距审计 G05/G13 | I/T |
| F80 | finite internal function points-to、stable function ID、guarded indirect dispatch、typed parameter/return widths | LLVM select/PHI、C function pointer、symbolic target fork、ID/heterogeneous-signature tamper tests | 技术档案 75、SOTA 差距审计 G05/G13 | I/T |
| F81 | bounded memcmp/bcmp summary、byte-load expansion、canonical lexicographic sign、zero-length no-dereference | LLVM/C equality/order/zero、pointer-union、65-byte rejection tests | 技术档案 76、SOTA 差距审计 G05/G04 | I/T |
| F82 | bounded memcpy/memmove/memset summary、source snapshot、finite disjointness proof、symbolic byte fill | LLVM intrinsic 与 C frontend、overlap/zero/null/symbolic-fill/disjointness rejection tests | 技术档案 77、SOTA 差距审计 G05 | I/T |
| F83 | indirect target-wise pointer-return summary、indirect pointer-actual summary、return-domain transport | LLVM/C pointer callback、target fork、destination/target-domain tamper tests | 技术档案 78、SOTA 差距审计 G05/G13 | I/T |
| F84 | guarded load domain、bounded strlen/strcmp/strncmp core-IR expansion、NUL short circuit | LLVM/C string union、input logical bound、n=0/null、symbolic n、guard domain/tamper tests | 技术档案 79、SOTA 差距审计 G05/G04 | I/T |
| F85 | bounded memchr/strchr first-match pointer result、finite result provenance | LLVM/C search、symbolic needle、zero/null、short union、extent rejection tests | 技术档案 80、SOTA 差距审计 G05/G04 | I/T |
| F86 | guarded store domain、bounded strcpy/strncpy snapshot/terminator/padding、destination return provenance | LLVM/C copy、source union、n=0/null、overlap/symbolic n、guard domain/tamper tests | 技术档案 81、SOTA 差距审计 G05/G04 | I/T |
| F87 | div/rem/shift definedness、nuw/nsw/exact constraints、defined freeze boundary、operator validation | LLVM/C symbolic/constant SAT/UNSAT、freeze poison、operator/assume-width tamper tests | 技术档案 82、SOTA 差距审计 G05 | I/T |
| F88 | scalar global pointer initializer、exact-slot dominating pointer store/load、finite loaded provenance guards | LLVM/C global/stack/heap cells、GEP/null、conditional/overwrite/function-pointer rejection tests | 技术档案 83、SOTA 差距审计 G05/G13 | I/T |
| F89 | stable function ID memory encoding、scalar global/dynamic function-pointer cell recovery、typed dispatch reuse | LLVM/C global/stack cells、single/select targets、typed execution | 技术档案 84、SOTA 差距审计 G05/G13 | I/T |
| F90 | fixed 1-D global pointer initializer table、symbolic element address domain、loaded address/ID filtering | LLVM/C data table、LLVM function table、symbolic index结果 | 技术档案 85、SOTA 差距审计 G05/G13 | I/T |
| F91 | exact-slot latest dominating store或direct-predecessor-complete merge、data/function candidate union | LLVM conditional data/function cells、incomplete predecessor rejection | 技术档案 86、SOTA 差距审计 G05/G13 | I/T |
| F92 | call-site/all-target nounwind proof、explicit normal target、unwind subtree reachability pruning | LLVM direct/indirect invoke、normal PHI、may-unwind与artifact target tamper rejection | 技术档案 87、SOTA 差距审计 G05 | I/T |
| F93 | stable-site × persisted dynamic counter token、typed nondet BV、incremental SMT declaration | LLVM poison/undef branches、loop checkpoint resume、duplicate-site rejection | 技术档案 88、SOTA 差距审计 G05 | I/T |
| F94 | backward last-writer closure、all-entry-path definition proof、cycle/budget detection | LLVM nested default/overwrite/forward merge、direct/function regressions、cycle/missing rejection | 技术档案 89、SOTA 差距审计 G05/G13 | I/T |
| F95 | abs-family defined domain、target-endian hton/ntoh、shared LLVM bswap DAG | LLVM/C symbolic abs、constant network order/bswap、wrong-ABI rejection | 技术档案 90、SOTA 差距审计 G05/G04 | I/T |
| F96 | ctpop bit sum、ctlz/cttz zero-prefix、zero-poison definedness | LLVM/C symbolic popcount、zero leading count、trailing count、poison infeasibility | 技术档案 91、SOTA 差距审计 G05/G04 | I/T |
| F97 | direct flagged arithmetic definedness side table、freeze ITE+nondet | LLVM overflowing/defined add freeze、multi-consumer fallback | 技术档案 92、SOTA 差距审计 G05 | I/T |
| F98 | CFG IN/OUT store-set fixed point、uninitialized bit、cycle certificate | LLVM loop-invariant/loop-carried pointer cells、uninitialized cycle rejection | 技术档案 93、SOTA 差距审计 G05/G13 | I/T |
| F99 | DataLayout constant-GEP `(base,offset)` key、symbolic fallback | LLVM equivalent nonzero GEP、distinct-offset interference | 技术档案 94、SOTA 差距审计 G05/G13 | I/T |
| F100 | bitreverse bit placement、mod-width fshl/fshr concatenation | LLVM symbolic reverse、official-style funnel examples | 技术档案 95、SOTA 差距审计 G05/G04 | I/T |
| F101 | signed/unsigned overflow predicates、min/max/zero clamp | packed four-extrema oracle、symbolic uadd saturation | 技术档案 96、SOTA 差距审计 G05/G04 | I/T |
| F102 | saturating shl reversibility、shift-range poison guard | signed/unsigned shift clamp、out-of-range infeasibility | 技术档案 97、SOTA 差距审计 G05/G04 | I/T |
| F103 | LLVM abs flag semantics、signed/unsigned min/max select | packed extrema、symbolic smax、INT_MIN poison | 技术档案 98、SOTA 差距审计 G05/G04 | I/T |
| F104 | expect/probability structural validation、first-value identity | symbolic chained hints retain both paths | 技术档案 99、SOTA 差距审计 G05 | I/T |
| F105 | static object remaining-size、null/unknown flags、guarded union ITE | stack/global union/null、dynamic input rejection | 技术档案 100、SOTA 差距审计 G05/G04 | I/T |
| F106 | runtime logical size、target-width pointer offset、dynamic/static fallback | input interior、dynamic malloc、static runtime unknown | 技术档案 101、SOTA 差距审计 G05/G04 | I/T |
| F107 | wrapped result/overflow pair、合法extractvalue bridge | six variants、symbolic uadd、signed-min multiply | 技术档案 102、SOTA 差距审计 G05/G04 | I/T |
| F108 | scalar identity、data/function pointer provenance forwarding | integer、pointer union、indirect function execution | 技术档案 103、SOTA 差距审计 G05/G13 | I/T |
| F109 | per-alternative realloc provenance、logical size与null fallback | shrink success、growth failure、realloc/global union | 技术档案 104、SOTA 差距审计 G05/G04 | I/T |
| F110 | single-use poison sink analysis、cast/BV/icmp/ssa.copy condition propagation | poison/defined chains与multi-use fallback | 技术档案 105、SOTA 差距审计 G05 | I/T |
| F111 | safe-divisor totalization、exact remainder复用 | div-zero、sdiv-overflow、exact/defined division | 技术档案 106、SOTA 差距审计 G05 | I/T |
| F112 | `cond_defined ∧ ite(cond,T_defined,F_defined)` | unselected/selected poison arm、poison condition | 技术档案 107、SOTA 差距审计 G05 | I/T |
| F113 | PHI defined-bit edge stage/parallel commit | selected/unselected predecessor poison | 技术档案 108、SOTA 差距审计 G05 | I/T |
| F114 | exact straight-line store/load definedness sidecar | global poison/defined、multi-load fallback | 技术档案 109、SOTA 差距审计 G05 | I/T |
| F115 | DataLayout base+constant-offset exact address proof | equivalent GEP positive、distinct offset negative | 技术档案 110、SOTA 差距审计 G05 | I/T |
| F116 | bounded unique-pred/succ memory corridor | cross-block positive；merge/byte lanes由F124--F149 supersede | 技术档案 111、SOTA 差距审计 G05 | I/T |
| F117 | function/return/call defined-bit ABI、frame transport | poison/defined/multi-call、malformed endpoints | 技术档案 112、SOTA 差距审计 G05 | I/T |
| F118 | defined parameter/argument mapping、typed frame transport | poison/defined/multi-call、malformed metadata | 技术档案 113、SOTA 差距审计 G05 | I/T |
| F119 | <=64 exact direct callsites、all-return-sink proof | return/argument positive、mixed sink rejection | 技术档案 114、SOTA 差距审计 G05 | I/T |
| F120 | full dynamic pointer identity、initial/runtime reaching-def merge | symbolic GEP、function overwrite、initializer fallback | 技术档案 115、SOTA 差距审计 G05 | I/T |
| F121 | argument-to-return poison backward slice、双向defined-bit ABI组合 | poison/defined multi-call passthrough、capability/endpoint复检 | 技术档案 116、SOTA 差距审计 G05 | I/T |
| F122 | <=256 all-use freeze-sink proof、fan-out definedness propagation | two-freeze positive、mixed ordinary consumer/capability negative | 技术档案 117、SOTA 差距审计 G05 | I/T |
| F123 | <=64 exact loads、reverse nearest-store proof、exact clobber | two-load freeze、same/canonical/cross-block回归、mixed-consumer negative | 技术档案 118、SOTA 差距审计 G05 | I/T |
| F124 | <=64 block forward DFS、all-predecessor common-store reverse proof | diamond `1,1,2,2`、mixed-clobber/capability negative | 技术档案 119、SOTA 差距审计 G05 | I/T |
| F125 | direct-predecessor store proof、edge i1 sidecar、artifact endpoint contract | poison/defined `1,2,2`、deleted endpoint rejection | 技术档案 120、SOTA 差距审计 G05 | I/T |
| F126 | F117/F118 ABI source recognition、edge sidecar复用 | argument/return双向`1,2,2`与capability复检 | 技术档案 121、SOTA 差距审计 G05 | I/T |
| F127 | <=64 block per-incoming reverse proof、ancestor condition operand | asymmetric forwarding `1,2,2` | 技术档案 122、SOTA 差距审计 G05 | I/T |
| F128 | loop-edge exact-store state update、header dominance marker | two-iteration 1/2、missing-backedge-store rejection | 技术档案 123、SOTA 差距审计 G05 | I/T |
| F129 | valid-initial reaching state、null-store edge=true | poison-store/no-store `1,2,2`、contract marker | 技术档案 124、SOTA 差距审计 G05 | I/T |
| F130 | DataLayout constant-folded global subobject initial state | constant-GEP poison/initial `1,2,2`、parent-contract tamper rejection | 技术档案 125、SOTA 差距审计 G05 | I/T |
| F131 | explicit Store/Initial/Carry source、backedge sidecar self-carry | two-iteration 1/2、mixed store/carry rejection、contract tamper | 技术档案 126、SOTA 差距审计 G05 | I/T |
| F132 | canonical diamond store/carry transfer、edge i1 select | symbolic selector `1,2,2`、forwarded-arm rejection、contract tamper | 技术档案 127、SOTA 差距审计 G05 | I/T |
| F133 | unique-predecessor arm ancestry、common conditional split | forwarded selector `1,2,2`、three-way rejection、contract tamper | 技术档案 128、SOTA 差距审计 G05 | I/T |
| F134 | complete source tuple grouping、all-endpoint common-arm proof | three-way `1,2,2,2`、distinct-store rejection、contract tamper | 技术档案 129、SOTA 差距审计 G05 | I/T |
| F135 | poison-free writer equivalence、per-endpoint writer membership | distinct constants `1,2,2,2`、mixed poison rejection、contract tamper | 技术档案 130、SOTA 差距审计 G05 | I/T |
| F136 | shared SSA poison equivalence、complete writer-membership set | shared poison `1,1,2,2,2`、distinct producer rejection、contract tamper | 技术档案 131、SOTA 差距审计 G05 | I/T |
| F137 | strict two-level Store/Store/Carry tree、eager dominance proof | distinct poison `1,1,2,2,2`、four-source rejection、contract tamper | 技术档案 132、SOTA 差距审计 G05 | I/T |
| F138 | depth-3--6 singleton Store*/Carry tree、eager dominance proof | three-poison `1,1,1,2,2,2,2`、local-value rejection、depth tamper | 技术档案 133、SOTA 差距审计 G05 | I/T |
| F139 | grouped recursive source leaf、all-endpoint same-arm proof | post-store fanout nine-state execution、parent-contract tamper | 技术档案 134、SOTA 差距审计 G05 | I/T |
| F140 | repeated Store source position expansion、unique-carry retention | depth-4 five-leaf nine-state execution、parent-contract tamper | 技术档案 135、SOTA 差距审计 G05 | I/T |
| F141 | repeated Carry source position expansion、exact carry-leaf contract | depth-4 five-leaf eight-state execution、count tamper | 技术档案 136、SOTA 差距审计 G05 | I/T |
| F142 | static scalar region disjointness、independent same-header sidecars | aggregate offset0/1 `1,1,2`、same-cell negative、marker tamper | 技术档案 137、SOTA 差距审计 G05 | I/T |
| F143 | distinct static global/alloca object proof、independent sidecars | two static allocas `1,1,2`、hierarchy negative、marker tamper | 技术档案 138、SOTA 差距审计 G05 | I/T |
| F144 | distinct fixed malloc-site proof、bounded heap sidecars | two heap sites `1,1,2`、dynamic-size rejection、marker tamper | 技术档案 139、SOTA 差距审计 G05 | I/T |
| F145 | finite select/acyclic-PHI region product proof、bounded symbolic sidecars | select/PHI 2x2 `1,1,2`、overlap infeasible、marker tamper | 技术档案 140、SOTA 差距审计 G05 | I/T |
| F146 | select-guard polarity relation、overlap compatibility proof | mutually-exclusive overlap `1,1,2`、compatible-overlap infeasible、tamper | 技术档案 141、SOTA 差距审计 G05 | I/T |
| F147 | PHI merge/predecessor relation、edge compatibility proof | edge-exclusive overlap `1,1,2`、same-edge infeasible、tamper | 技术档案 142、SOTA 差距审计 G05 | I/T |
| F148 | one-term symbolic GEP ConstantRange、region interval product | separated subarrays `1,1,2`、same-range infeasible、tamper | 技术档案 143、SOTA 差距审计 G05 | I/T |
| F149 | bounded byte-lane nearest-writer composition、store sidecar AND | split/initial/overlap `1,2`、defined `1026`、merge negative、tamper | 技术档案 144、SOTA 差距审计 G05 | I/T |
| F150 | per-edge byte-lane source vectors、edge-local defined reduction | path-dependent `1,2,2`、initial `1,1,2,2`、upstream-join negative、tamper | 技术档案 145、SOTA 差距审计 G05 | I/T |
| F151 | per-lane loop state、entry/backedge transfer、alias closure | cyclic partial write `1,2`、extra-alias infeasible、carry/capability tamper | 技术档案 146、SOTA 差距审计 G05 | I/T |
| F152 | strict conditional diamond、arm-edge per-lane state updates | symbolic write/skip `1,1,2`、alias-arm infeasible、polarity/capability tamper | 技术档案 147、SOTA 差距审计 G05 | I/T |
| F153 | bounded forwarded arm corridors、full CFG predecessor reconstruction | forwarded `1,1,2`、nested-branch infeasible、successor/capability tamper | 技术档案 148、SOTA 差距审计 G05 | I/T |
| F154 | strict two-level three-arm lane tree、two writer/one carry partition | low/high/carry `1,1,1,2,2`、four-arm infeasible、route/capability tamper | 技术档案 149、SOTA 差距审计 G05 | I/T |
| F155 | three bounded arm corridors、nearest root/inner branch reconstruction | forwarded `1,1,1,2,2`、nested-corridor infeasible、successor/capability tamper | 技术档案 150、SOTA 差距审计 G05 | I/T |
| F156 | strict binary lane-source tree、4--8 leaves/depth<=6 | four-leaf `1,1,1,1,2,2,2`、shared-leaf infeasible、depth/capability tamper | 技术档案 151、SOTA 差距审计 G05 | I/T |
| F157 | branch-to-child bounded corridors、recursive terminal reconstruction | forwarded four-leaf execution、nested-tail infeasible、successor/capability tamper | 技术档案 152、SOTA 差距审计 G05 | I/T |
| F158 | internal writer dominance、exact descendant-leaf source group | grouped four-leaf execution、two-group infeasible、marker/capability tamper | 技术档案 153、SOTA 差距审计 G05 | I/T |
| F159 | internal repeated source、same-range descendant overrides | five-leaf `1,1,1,1,1,2,2,2,2`、mismatched override infeasible、marker tamper | 技术档案 154、SOTA 差距审计 G05 | I/T |
| F160 | per-lane nearest internal/override/carry sources、lane.store edge lowering | mixed-source leaf nine-state execution、full-shadow infeasible、marker/capability tamper | 技术档案 155、SOTA 差距审计 G05 | I/T |
| F161 | exact 2--8 all-carry leaf count、independent leaf edges | six-leaf ten-state execution、shared-carry infeasible、count/capability tamper | 技术档案 156、SOTA 差距审计 G05 | I/T |
| F162 | two disjoint internal writer groups、exact descendant partitions | dual-group nine-state execution、nested-group infeasible、count/capability tamper | 技术档案 157、SOTA 差距审计 G05 | I/T |
| F163 | one composed group + one pure group、unique local source | mixed six-leaf eleven-state execution、full-shadow infeasible、count/capability tamper | 技术档案 158、SOTA 差距审计 G05 | I/T |
| F164 | forwarded single-group tree、bounded unique-predecessor corridors | forwarded four-leaf seven-state execution、nested corridor infeasible、marker/capability tamper | 技术档案 159、SOTA 差距审计 G05 | I/T |
| F165 | forwarded exact-override tree、same-range group partition | forwarded five-leaf nine-state execution、branching corridor infeasible、dual-marker/capability tamper | 技术档案 160、SOTA 差距审计 G05 | I/T |
| F166 | forwarded partial-override tree、per-lane source difference | forwarded composed nine-state execution、branching corridor infeasible、dual-marker/capability tamper | 技术档案 161、SOTA 差距审计 G05 | I/T |
| F167 | forwarded two-group tree、disjoint descendant partitions | forwarded dual-group nine-state execution、branching corridor infeasible、marker/count/capability tamper | 技术档案 162、SOTA 差距审计 G05 | I/T |
| F168 | forwarded mixed two-group tree、unique local-source difference | forwarded mixed eleven-state execution、branching corridor infeasible、marker/count/capability tamper | 技术档案 163、SOTA 差距审计 G05 | I/T |
| F169 | forwarded multi-carry tree、exact all-carry count | forwarded six-leaf ten-state execution、branching corridor infeasible、marker/count/capability tamper | 技术档案 164、SOTA 差距审计 G05 | I/T |
| F170 | three pure internal groups、all-pairs disjoint partitions | seven-leaf thirteen-state execution、nested group infeasible、marker/count/capability tamper | 技术档案 165、SOTA 差距审计 G05 | I/T |
| F171 | one composed leaf per each of two groups、per-group counters | seven-leaf thirteen-state execution、same-group override infeasible、marker/count/capability tamper | 技术档案 166、SOTA 差距审计 G05 | I/T |
| F172 | forwarded double-composed tree、two source differences | forwarded seven-leaf thirteen-state execution、branching corridor infeasible、dual-marker/capability tamper | 技术档案 167、SOTA 差距审计 G05 | I/T |
| F173 | forwarded three-group tree、all-pairs partition proof | forwarded seven-leaf thirteen-state execution、branching corridor infeasible、marker/count/capability tamper | 技术档案 168、SOTA 差距审计 G05 | I/T |
| F174 | one composed leaf in three direct groups、source difference | eight-leaf fifteen-state execution、full-shadow infeasible、marker/count/capability tamper | 技术档案 169、SOTA 差距审计 G05 | I/T |
| F175 | forwarded composed three-group tree、reconstructed partitions | forwarded eight-leaf fifteen-state execution、branching corridor infeasible、marker/count/capability tamper | 技术档案 170、SOTA 差距审计 G05 | I/T |
| F176 | normalized cross-prefix polyhedron relation、per-candidate exact validation | different-key SAT reuse、UNSAT isolation、three poly lit tests、adaptive/self-config tests | 技术档案 171、SOTA 差距审计 G18 | I/T/E |
| F177 | target-assertion optimistic generator slice、full-solver validation certificate | `[8,255]` weak domain、invalid-model rejection、exact-mode `[8,10]`、certificate tamper | 技术档案 172、SOTA 差距审计 G02 | I/T/E |
| F178 | native Z3 simplify/solve-eqs model converter、subgoal enumeration、full validation | `x=y+1` eliminated-variable recovery、converter provenance/count tamper、three generator integrations | 技术档案 173、SOTA 差距审计 G02 | I/T/E |
| F179 | interval-elimination projection、artifact-sized normalization、cross-size SAT replay | two-byte to one-byte shared-`x` hit、projection-disabled zero counters、four poly lit tests | 技术档案 174、SOTA 差距审计 G18 | I/T/E |
| F180 | coefficient-hypergraph variable bijection、matrix/model/box remapping、exact validation | shifted `(x0,x1)` to `(x2,x3)` hit、rename-disabled zero counters、five poly lit tests | 技术档案 175、SOTA 差距审计 G18 | I/T/E |
| F181 | versioned Query-IR inverse recipe、individual/cumulative replay、full-root evaluator gate | cumulative `AB` converter、unknown-step rejection、range replay `@/A/B` manifests | 技术档案 176、SOTA 差距审计 G02 | I/T/E |
| F182 | bounded existential Presburger projection、bidirectional implication/intersection classification | parity-disjoint exact relation、interval false relation/validator rejection、six poly lit tests | 技术档案 177、SOTA 差距审计 G18 | I/T/E |
| F183 | cross-size coefficient-preserving byte bijection、endian/offset model remapping | two-byte big-endian to four-byte little-endian hit、disabled-zero counters | 技术档案 178、SOTA 差距审计 G18 | I/T/E |
| F184 | narrower-to-wider injective field mapping、mandatory exact existential projection | 16-bit big-endian to 32-bit little-endian `01 00 00 00`、dual telemetry hits | 技术档案 179、SOTA 差距审计 G18 | I/T/E |
| F185 | wider-to-narrower structural subfield injection、synthetic-variable elimination、mandatory exact projection | relocated 32-bit artifact low16 to 16-bit `01 00`、parity false-renaming regression | 技术档案 180、SOTA 差距审计 G18 | I/T/E |
| F186 | versioned parameter schema、conditional DAG、context/pair posterior、isolated transfer prior | 8 self-config tests、schema CLI、adaptive regression | 技术档案 181、SOTA 差距审计 G16 | I/T/E |
| F187 | taint-span online grammar、stable productions、variable-length splice、validation/retention feedback | 7 semantic proposal tests、2 proposal-manager regressions | 技术档案 182、SOTA 差距审计 G08/G11 | I/T/E |
| F188 | target-closure query partition、disconnected witness fixing、SAT-only proof boundary、full fallback | selective hit/bad-witness fallback lit、14 QueryStore tests | 技术档案 183、SOTA 差距审计 G12 | I/T/E |
| F189 | contextual cost/timeout predictor、persistent policy、mixed completion portfolio | witness/fallback/mixed-hit/learned-skip/restart lit、self-config/QueryStore regressions | 技术档案 184、SOTA 差距审计 G12 | I/T/E |
| F190 | Query IR read/model set manifest、grammar hole evaluator gate、three-stage certificate | JSON variable-length hole、tampered sidecar、fake-SAT、certificate persistence tests | 技术档案 185、SOTA 差距审计 G11 | I/T/E |
| F191 | retained-seed bank、coverage plateau gate、history token acquisition与反馈漏斗 | threshold/no-threshold、history splice、reset、generator/manager persistence tests | 技术档案 186、SOTA 差距审计 G11 | I/T/E |
| F192 | target后独立parser oracle、context-local grammar conflict splitting、v5持久反馈 | parser accept/reject、双拒绝局部屏蔽、restart与acceptance reopen tests | 技术档案 187、SOTA 差距审计 G11 | I/T/E |
| F193 | String/BV逐offset精确链接、first-NUL/length约束、联合byte predicates、binary-safe model | 8 unit、真实Z3 compare/contains+BV/binary、37项跨层回归 | 技术档案 188、SOTA差距审计G04 | I/T/E |
| F194 | direct-input strlen/strchr/strstr operation artifact、length/indexof转换、有界BV语义 | 10 unit、真实instrumented三operation/Z3 checker、39项跨层回归 | 技术档案189、SOTA差距审计G04 | I/T/E |
| F195 | JSON/SMT-LIB backend adapter、8路并发、SAT concrete validation、disagreement漏斗 | 13 unit、stub三BV格式、双Z3 smoke、42项跨层回归 | 技术档案190、SOTA差距审计G04 | I/T/E |
| F196 | decimal regex/str.to_int/32-bit BV fold、guarded atoi artifact | 14 unit、真实atoi operation/Z3 checker、43项跨层回归 | 技术档案191、SOTA差距审计G04 | I/T/E |
| F197 | signed decimal regex、target-long range、null-endptr/base10 strtol BV fold | 15 unit、真实signed strtol/Z3与base/endptr guard、44项跨层回归 | 技术档案192、SOTA差距审计G04 | I/T/E |
| F198 | versioned五case跨solver门禁、portable String/BV/Integer lowering | Z3/cvc5 1.1.2各5/5 SAT与concrete-verified、lit入口 | 技术档案193、SOTA差距审计G04 | I/T/E |
| F199 | validation-aware上下文backend bandit、严格调用预算、锁定持久化、conformance旁路 | 17 string unit、46项跨层回归、预算一路时Z3/cvc5十结果门禁 | 技术档案194、SOTA差距审计G04/G06 | I/T/E |
| F200 | unsigned-long decimal artifact/query/BV fold、三conversion逐位overflow proof | 18 string unit、47项跨层回归、双solver各6/6、真实strtoul与域外/幂等lit | 技术档案195、SOTA差距审计G04 | I/T/E |
| F201 | versioned parser span tree、exit/verdict/tree验证、rule-scoped structural context alias | 17 semantic/proposal、32项含QueryStore、54项扩展回归、mismatch fail-closed | 技术档案196、SOTA差距审计G08/G11 | I/T/E |
| F202 | rule/structural-context coverage state、递减novelty排序、verified-before-retained门 | context score/persistence/invariant tests、32项semantic链与54项扩展回归 | 技术档案197、SOTA差距审计G08/G11 | I/T/E |
| F203 | stable parser production ID、single-slot direct recursion induction、64K独立grammar bitmap与path/production新颖性调度 | recursive expr tree、bounded wrapper、bitmap/alias/rule重启、19项semantic/proposal与56项扩展回归 | 技术档案198、SOTA差距审计G08/G11 | I/T/E |
| F204 | canonical CFG fragment、multi-slot/direct与mutual cycle、depth-bounded derivation、一对多cycle provenance | binary Expr双slot、A-B-A mutual path、depth1--3、tamper/restart、22项semantic/proposal、37项定向回归 | 技术档案199、SOTA差距审计G08/G11 | I/T/E |
| F205 | production shape/concrete instance分层、incremental ECT correspondence、context-safe subtree substitution、引用一致驱逐 | 同形A/B跨candidate、错误context预过滤、yield tamper、v9 restart、32层64 KiB降级、24项semantic/proposal、39项定向回归 | 技术档案200、SOTA差距审计G08/G11 | I/T/E |
| F206 | 七维rule/context Pareto、256-arm极值archive、non-dominated fronts/crowding、data/String telemetry、age公平性 | edge/data互不支配极端、dominated rule、String 2/3与data quality v10 restart、parser conflict reopen、26项semantic/proposal、41项定向、323项全量、Z3/cvc5各6/6 | 技术档案201、SOTA差距审计G08/G11 | I/T/E |
| F207 | 六维epsilon-Pareto seed metadata archive、strict dominance、extreme-protected density replacement、scheduler priority | edge/data/String extremes、dominated rejection、super replacement、capacity density、v12 restart、83项定向、325项全量 | 技术档案202、SOTA差距审计G08/G11 | I/T/E |
| F208 | parser trace v2零宽节点、bounded packed alternatives、primary-tree定位、canonical v3 families、context-safe epsilon删除 | 4条production/1条alternative/1条epsilon、错误partition、32条上限、错误context、tamper、v6/v11 restart、30项定向、329项全量、双solver各6/6 | 技术档案203、SOTA差距审计G08/G11 | I/T/E |
| F209 | parser trace v3 shared forward DAG、v4 graph certificate、32-node/128-edge deep closure、node-path instance、引用级联驱逐 | 5-node/5-edge共享delimiter、deep `a -> b`、错误branch、primary sharing拒绝、edge tamper/drop、v7/v12 restart、33项定向、332项全量、双solver各6/6 | 技术档案204、SOTA差距审计G08/G11 | I/T/E |
| F210 | parser trace v4 nullable dependency rules、SCC least fixed point、v5 proof certificate、proof-backed context-safe epsilon | 有基例互递归A=1/B=0、纯循环0 proof、proof tamper、wrong context、instance eviction、v8/v13 restart、35项聚焦、334项全量、双solver各6/6 | 技术档案205、SOTA差距审计G08/G11 | I/T/E |
| F211 | parent/child yield重建、2--4槽atomic ECT transaction、source-yield精确门、derived-state级联撤销 | `a,c,e`/`b,d,f`导出三种双槽新组合、wrong context、v9 provenance、v14 restart/tamper/eviction、36项聚焦、335项全量、双solver各6/6 | 技术档案206、SOTA差距审计G08/G11 | I/T/E |
| F212 | `{cache}` parser-neutral manifest、content-addressed base trace、edit/guard invalidation、verified reuse receipt、cold fallback | 64-byte fixture cold/2-node hit/restart、receipt tamper fail-closed、cache corruption fallback、v10 persistence、37项聚焦、336项全量、双solver各6/6 | 技术档案207、SOTA差距审计G08/G11 | I/T/E |
| F213 | accepted alternative-0计数、Dirichlet(0.5) posterior、canonical packed hyperedge、显式root membership、inside/outside与九维Pareto | 3:1分支得0.7/0.3、root inside=1、outside质量、未观察shape平滑、幂等/tamper/v14迁移/edge驱逐、21项semantic、337项全量 | 技术档案208、SOTA差距审计G08/G11 | I/T/E |
| F214 | 两观测零反例slot relation、relation-aware transaction v2、三优先加一探索、exact child-edge provenance、v15迁移 | equality support=3、3 conforming/1 exploration、relation/score篡改重算、乱序/缺边拒绝、反例撤销、22项semantic、338项全量、双solver各6/6 | 技术档案209、SOTA差距审计G08/G11 | I/T/E |
| F215 | parent-shape/slot context、beta=2全局收缩、context-state inside/outside、atomic tree count、fixed-point evidence reconciliation、v16 fallback | 全局4:4=0.5、parent 3:1/1:3=2/3与1/3、duplicate/restart/count/context篡改/二次重启/v16迁移、23项semantic、339项全量、双solver各6/6 | 技术档案210、SOTA差距审计G08/G11 | I/T/E |
| F216 | update-before sufficient-stat receipt、prequential global/context NLL、gain/sample gate、effective posterior convex fallback、v17无校准迁移 | 40 receipts、确定性重启、receipt篡改隔离、v17 raw保留/effective全局、正gain启用与drift负gain归零、24项semantic、340项全量、双solver各6/6 | 技术档案211、SOTA差距审计G08/G11 | I/T/E |
| F217 | receipt v2 fragment index、fixed-16 recent regret、stale/OOD fallback、recent drift/recovery、v18累计模式迁移 | 40个v19有序receipt重启、v18重签迁移、positive/adverse/stale/recovery四阶段、25项semantic、341项全量、双solver各6/6 | 技术档案212、SOTA差距审计G08/G11 | I/T/E |
| F218 | per-fragment bounded log-loss stream、64-window/8+8 Hoeffding detector、online prefix truncation、canonical cut certificate、v20 derived-state重算 | stationary零切点、positive/adverse/recovery两次切分、certificate重算/epsilon篡改、26项semantic、342项全量、双solver各6/6 | 技术档案213、SOTA差距审计G08/G11 | I/T/E |
| F219 | grandparent/parent/family context、beta-2二级posterior、global+parent robust prequential gate、context-state v2 sum/product circuit、adaptive circuit cut、v21固定点恢复 | parent 16:16、grandparent 14:2/2:14得5/6与1/6、双基线weight、shared-state分离、count/receipt篡改、v20 fallback、28项semantic、344项全量、双solver各6/6 | 技术档案214、SOTA差距审计G08/G11 | I/T/E |
| F220 | parent/slot/left-production sibling context、beta-2三层收缩、global+parent+circuit robust gate、context-state v3 forward/backward factor message、adaptive sibling cut、v22固定点恢复 | global/parent/circuit均16:16时sibling 14:2/2:14得5/6与1/6、精确factor mass、count/receipt篡改、v21 fallback、30项semantic、346项全量、双solver各6/6 | 技术档案215、SOTA差距审计G08/G11 | I/T/E |
| F221 | SMT-LIB signed integer printer、SC/TSO/RA RF sentinel规范化、Query-IR RF bridge与evidence outcome可移植化 | base SMT无裸`-1`、真实Z3/cvc5对SC/TSO/RA oracle一致、63项schedule/evidence、346项全量 | 技术档案216、SOTA差距审计G10/G13 | I/T/E |
| F222 | MPI master QueryStore统一初始化、service-process成功门、grammar-hole fail-closed fallback、全目录静态清理 | 127项受影响定向、全量Ruff/py_compile、346项Python与4项subtest | 技术档案217、SOTA差距审计G01/G11 | I/T/E |
| F223 | 双层逆平方alpha spending、repeated forward Hoeffding CS、context-wise infinite-horizon PFA证书、parent/circuit/sibling canonical verifier、v22迁移重建 | 三类64-fragment强漂移、稳定流、预算界、三类篡改拒绝、v22缺字段迁移、30项semantic | 技术档案218、SOTA差距审计G08/G11 | I/T/E |
| F224 | `(shape[i-2],shape[i-1])`二阶兄弟history context、beta-2层次收缩、五层robust prequential gate、context-state v4 tuple forward/backward、history adaptive/anytime certificate、v24固定点恢复 | 64个真实parser fragments隔离四个14:2/2:14 history contexts、精确tuple inside/outside、count/receipt篡改、v23 fallback、四基线漂移/anytime证书、32项semantic、348项全量、106项lit、双solver各6/6 | 技术档案219、SOTA差距审计G08/G11 | I/T/E |
| F225 | append-only context allocation ledger、outer inverse-square alpha spending、parent/circuit/sibling/history统一global certificate与verifier、cross-context arbitrary-dependence FWER、v25 fail-closed restore | 四种64-event强漂移global证书、257项预算和ordinal、prefix/ledger篡改、真实parser重启、v24 receipt迁移、32项semantic、348项全量、106项lit、双solver各6/6 | 技术档案220、SOTA差距审计G08/G11 | I/T/E |
| F226 | 五档cost-faithful PCFG context order、sealed per-run artifact、protocol矩阵扩展、artifact-to-ablation metric join | 最小packed forest五档状态边界、artifact round-trip/tamper、100-cell等CPU矩阵、missing-artifact失败保留、33/7/6项定向、351项Python、106项lit、双solver conformance与8项research obligation | 技术档案221、SOTA差距审计G08/G11 | I/T/E |
| F227 | 持久native Tree-sitter service、exact edit、TSTree cache、node-ID reuse proof、v2 trace/RPC与manager复检 | fake边界7项、真实Tree-sitter/JSON、Unix client/server和VerifiedProposalManager端到端共10项、361项Python、107项lit、双solver与research evidence | 技术档案222、SOTA差距审计G08/G11 | I/T/E |
| F228 | proposal state v11累计parser cost/proof、cache-off control、sealed parser artifact、cold/incremental protocol与analysis join | cache-off/伪telemetry、真实cost/proof、artifact tamper、40-cell矩阵、missing artifact与metric join、77项定向、365项Python、107项lit、双solver与research evidence | 技术档案223、SOTA差距审计G08/G11 | I/T/E |
| F229 | 持久Lark generalized Earley、complete SPPF到trace v3、nullable v4 certificate、cycle/resource fail-closed | 真实二叉歧义/shared DAG、epsilon fixed point、byte span、invalid/cyclic/bound、Unix RPC与manager端到端8项 | 技术档案224、SOTA差距审计G08/G11 | I/T/E |
| F230 | forest provider proof/identity与结构计数复检、proposal state v12累计规模/成本、三档sealed cross-parser calibration | edge/proof/grammar/state篡改、60-cell等CPU矩阵、mode/command/grammar binding、CLI、376项Python、108项lit、双solver/evidence | 技术档案225、SOTA差距审计G08/G11 | I/T/E |
| F231 | same-candidate complete/selected双oracle、nested trace独立复检、state v13 acceptance四象限、sealed child command pair与paired protocol | 真实Lark+selected fixture、both/secondary-only、trace篡改、restart/artifact、四格篡改、60-cell与CLI、381项Python、109项lit、双solver/evidence | 技术档案226、SOTA差距审计G08/G11 | I/T/E |
| F232 | manager-derived alternative-zero/full-forest/secondary去重span与boundary correspondence、state v14集合证书、micro precision/recall/Jaccard | 真实Lark与逐byte selected非零shared span、subset/union、非共同接受排除、artifact union篡改、schema迁移、381项Python、109项lit、双solver/evidence | 技术档案227、SOTA差距审计G08/G11 | I/T/E |
| F233 | 持久Parglare GLR直接Parent/possibility SPPF、provider proof、与Earley同candidate differential composition | 二叉歧义/shared DAG、nullable、negative/cycle/bound/invalid/tamper、RPC/manager、Lark-vs-GLR paired chain 7项、388项Python、110项lit、双solver/evidence | 技术档案228、SOTA差距审计G08/G11 | I/T/E |
| F234 | 透明provider wrapper、span+递归shape唯一symbol mapping、child-partition production mapping、state v15 canonical proof与sealed聚合表 | 真实Lark/fixture symbol、Lark/Parglare production、同span歧义拒绝、1500层显式栈、state/artifact篡改、390项Python | 技术档案229、SOTA差距审计G08/G11 | I/T/E |
| F235 | shortlex有限byte域穷举、双trace manager复检、四象限/transcript/minimal-counterexample sealed artifact与CI gate | 7-case fake一致/差异、真实Earley/GLR 4-case、bounds/tamper/CLI、Parglare empty-negative修复、393项Python、111项lit、双solver/evidence | 技术档案230、SOTA差距审计G08/G11 | I/T/E |
| F236 | Z3 assumption-conflict input投影、非空UNSAT core、bounded deletion最小化、Query IR二次冲突证明、signed prefix/off-path literal索引 | 真实helper单byte minimal core/SAT hit/固有UNSAT拒绝、17项QueryStore含reuse与proof篡改、全量门禁 | 技术档案231、SOTA差距审计G07 | I/T/E |
| F237 | Query-IR-derived标准QF_BV lowering、canonical capability/lowering certificate、SMT-LIB model parser、两级Query IR模型验证与显式UNSAT授权 | 7项定向测试；真实cvc5 1.1.2执行全部38类operator matrix、SAT/UNSAT trust差分、模型/重哈希证书篡改拒绝；402项Python/112项lit全量通过 | 技术档案232、SOTA差距审计G06、Configuration | I/T/E |
| F238 | 每prefix独立interactive SMT-LIB process、push/check/model/pop、1--64 LRU、echo framing、deadline kill/cold recovery与canonical incremental capability | 真实cvc5 same-prefix hit/LRU eviction/cold rebuild；fake hung solver timeout/recovery；定向文件9项；404项Python/112项lit全量通过 | 技术档案233、SOTA差距审计G06、Configuration | I/T/E |
| F239 | 确定性X-means/two-means refinement、spherical-Gaussian BIC、删失PAR-2 IPS/ESS/empirical-Bayes LCB、可独立复算制品与保守在线先验 | 7项定向测试、500-event/8-cluster压力构造；411项Python/113项lit全量通过 | 技术档案234、SOTA差距审计G06、Configuration、benchmark README | I/T/E |
| F240 | completion/PAR-2/reward浅树bagging+residual boosting、uncertainty-aware共享预算beam schedule、跨replay真实timeout推进与instance/budget轨迹 | 5项optimizer与1项trajectory协议测试；互补双action两阶段schedule、under-support退化、重哈希篡改、CLI；417项Python/114项lit | 技术档案235、SOTA差距审计G06、Configuration、benchmark README | I/T/E |
| F241 | 固定single-action初始设计、位置化schedule向量、bagged/boosted schedule surrogate、解析EI严格预算acquisition、隔离post-hoc oracle/simple regret、可重放artifact与在线prior kind | 5项SMBO测试；二action三阶段边界、6次预算/确定性trace、EI极限、重哈希篡改、CLI与真实预算调度；422项Python/115项lit、双solver各6/6 | 技术档案236、SOTA差距审计G06、Configuration、benchmark README | I/T/E |
| F242 | SAT-only bounded consensus grace、queued Future与JSON/QF_BV process-group interrupt、persistent cold respawn/prefix drop、sticky cancel及cancel/incomplete-consensus telemetry | 5项新增测试；真实one-shot提前退出、快速disagreement、JSON/QF_BV persistent cold recovery、QueryStore统计、两组10x竞态重复；427项Python/115项lit、双solver各6/6 | 技术档案237、SOTA差距审计G06、Configuration、benchmark README | I/T/E |
| F243 | Query IR node/input/width与四组operator统计、16维features-v2、runtime/trajectory/online传播、F239--F241动态维数验证、v1 artifact投影与9→16 state迁移 | 6组专项脚本共75项、真实Query IR lit、QSYM runtime build、v1 F239/F240 artifact回归；434项Python/115项lit、双solver各6/6与全量静态门禁 | 技术档案238、SOTA差距审计G06、Configuration、benchmark README | I/T/E |
| F244 | 固定Bitwuzla 0.9.1构建身份、统一38-operator matrix、SAT模型双验证、UNSAT拒绝/授权探针、binary/version-bound artifact、离线验签与语义重放 | 5项新测试、QF_BV专项16项；cvc5/Bitwuzla均38/38、clean-prefix 222步复现构建、artifact/重放及篡改拒绝；439项Python/116项lit、双String solver/evidence与静态门禁 | 技术档案239、SOTA差距审计G06、Configuration、benchmark README、checked evidence | I/T/E |
| F245 | sealed full-envelope Query IR corpus、hash train/holdout、conformance-gated exact solver binary、随机化paired tasks、child CPU/PAR-2/complementarity、离线SAT/aggregate verifier与semantic replay；Z3 nonzero-UNSAT status-only二次确认 | 5项新测试；8-query 4/4 smoke、Z3/cvc5/Bitwuzla各3 SAT/1 UNSAT、12/12 solved、无分歧、内层模型篡改拒绝、replay一致；444项Python/117项lit、双String conformance、三solver重放与evidence验签 | 技术档案240、SOTA差距审计G06、Configuration、benchmark README、checked smoke evidence | I/T/E |
| F246 | train-only trajectory/query-ID proof、F240/F241确定性重训、F243 holdout feature/schedule复算、三individual+beam+SMBO+SAT-gated cancellation六臂、完整内部attempt与child CPU/PAR-2配对验证 | 5项新测试；4 holdout×6臂=24任务全解，grace-0取消6 attempt、UNSAT完整共识；训练泄漏及重哈希policy/schedule/winner/model/certificate/CPU/cancel篡改拒绝，semantic replay一致；449项Python/118项lit及全量门禁 | 技术档案241、SOTA差距审计G06、Configuration、benchmark README、checked strategy evidence | I/T/E |
| F247 | ASLR稳定comparison site、PCGUARD低64 KiB data namespace、paired-preload streaming edge/data oracle、F246 candidate/witness coverage join、六臂coverage/solver-CPU与pair complementarity、F243 v1/v2 holdout calibration | 4项F247及1项新增runtime测试；5 input/24 join、稳定双replay、Z3 arm 6 edge+1 data gain、重哈希map/row篡改与binary漂移拒绝、semantic replay一致；455项Python/119项lit全量通过 | 技术档案242、SOTA差距审计G03/G06、Configuration、benchmark README、checked coverage evidence | I/T/E |
| F248 | 稳定site画像与单站点选择、strict-diamond LCS complete alignment、safe extra ALU、aggressive load linearization/store readback、Hydra select fork-elision、original-authoritative replay与spurious denylist | compiler lit覆盖profile/safe/aggressive/denylist；4项Python覆盖画像、三类failure关系、篡改与semantic replay；native smoke为1 real/1 spurious、site 424248 denylist、campaign/semantic验签与重放一致 | 技术档案243、SOTA差距审计G09、Configuration、benchmark README、checked Hydra evidence | I/T/E |
| F249 | 3--8臂branch-only IFSS显式路径worklist、AND/OR relevance predicate、反向ITE region-state merge、symbolic PHI substitution、完备default与late-failure原子IR回滚 | 正向native lit得到跨controller候选`00 58`，Backsolver 1/1/1、direct 4/1；opaque-call late rejection经LLVM verifier且无孤立IFSS runtime call；2项定向lit | 技术档案244、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F250 | merge-local load的二输入MemorySSA phi、等宽simple MustAlias stores、region ModRef proof、memory-state ITE use substitution、稳定site metadata、late rollback与undef/poison拒绝 | native候选`00`、Backsolver 1/1/1、direct 2/1；NoAlias/opaque late/poison拒绝经LLVM verifier；含既有边界共6项定向lit | 技术档案245、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F251 | 每arm最多8项MemoryDef backward chain、逐项NoMod proof、nearest-first chain metadata、MayAlias/预算/volatile/atomic/fence fail closed | 两臂各跳过1个NoAlias def后native候选`00`、Backsolver 1/1/1、direct 2/1；MayAlias、第9项、volatile拒绝经verifier | 技术档案246、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F252 | data/MemoryPhi共享2--8 endpoint partition、全region 32-block/64-path精确证明、3--8臂MustAlias/NoMod memory state、逐arm proof metadata与兼容二臂schema | 三臂各one-hop chain、2层ITE，native候选`00 58`、Backsolver 1/1/1、direct 4/1；任一MayAlias arm整体拒绝；6项定向lit | 技术档案247、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F253 | partition内原始condition确定性去重、逐condition独立short-circuit computation与出口PHI、无所有权RegionValue cache、定义序登记及late rollback、稳定condition-site metadata | 四臂data与四臂MemorySSA各只生成2个internal condition computation，共4个`partition-condition-cache-v1`标记；LLVM verifier通过；native候选`41 00 59`、Backsolver 1/1/1、direct 5/1 | 技术档案248、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F254 | 2--8个normal return的共同controller/branch-only/acyclic/32-block/64-path精确证明、synthetic dispatch return-state PHI、跨调用return ITE、arm/controller/site proof metadata、musttail与无收益门 | 三返回callee经verifier并生成非执行return的validated Backsolver候选且保留独立字节；flag-off保持3 ret；switch/loop/aggregate/poison/九出口/musttail拒绝；2项定向lit | 技术档案249、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F255 | 2--8唯一case/default strict fanout、稳定equality branch chain、destination PHI改写、case/default/site metadata、F252/F253 data/memory复用及switch→F254组合 | 三臂memory输入`A`生成case `B` validated候选；data/memory各1个internal condition cache；direct-return组合经verifier；shared/external/conditional/九臂/mixed拒绝；2项定向lit | 技术档案250、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F256 | unsigned APInt排序、`ule` range-balanced tree、exact-eq leaves、default多gap predecessor PHI复制、独立tree schema与linear fallback | 8-arm验证linear 7 eq对balanced 6 ule+7 eq、root bound 2、default PHI 1→7、12 condition cache、7 ITE；native `00`生成case6 `06`候选；1项定向lit | 技术档案251、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F257 | stable-site完整正权profile、unsigned optimal alphabetic interval DP、饱和cost与确定性tie-break、64位内容指纹/objective/root proof、分类balanced fallback、删除前site固化 | 7-case偏斜profile把root bound 2→0，6 ule+7 eq且verifier-clean；incomplete/unreadable/duplicate分类回退；native `00`生成case6 `06`候选；1项定向lit | 技术档案252、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F258 | 逻辑edge/unique endpoint分离、重复predecessor PHI同值与multiplicity证明、linear `m→m`/range default-miss扩张、shared两臂完整partition、MemorySSA multi proof、F254同successor路径组合 | 四edge/两endpoint验证linear `3→3`、balanced/profile `3→5`及双root proof；data/memory/return组合经verifier；native `A`经Z3 fallback生成`B`候选；全同目标拒绝；1项定向lit | 技术档案253、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F259 | external-authoritative/LLVM branch-weight fallback、source-bound profile proof、完整tree/case/destination/multiplicity JSONL、profile/tree双fingerprint、独立饱和DP/tree verifier | external与LLVM七case均root0/objective2028；explicit incomplete仍balanced；zero metadata分类回退；linear/balanced/profile及shared多record验证，root篡改拒绝；2项定向lit | 技术档案254、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F260 | branch-only/acyclic 2--8 exit、至少2 continuation、1--8 scalar PHI slot的原子证明与lowering；真实exit-id consumer、capture/resume edge身份、destination PHI multiplicity及强制完整partition | 三exit/两destination/两slot产生6个ITE，native `C`生成跨continuation `B`；同source双edge保持multiplicity；单destination/vector/poison/cycle拒绝；3项定向lit及139/139全量 | 技术档案255、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F261 | LoopInfo canonical两block natural loop、递归trip upper-bound<=8、unit induction、1--8独立constant-add integer recurrence、模位宽closed form、exit PHI/predecessor事务与stable proof metadata | 单/双state和i8→i16、32输入`lli`差分、direct solver 1/1 SAT候选`05`；bound15/store/nonlinear/signed/nuw/poison拒绝；2项定向lit及141/141全量 | 技术档案256、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F262 | F260 dispatch MemoryPhi绑定、resume/capture精确集合、每相关exit最多8项NoMod MemoryDef chain、等宽simple MustAlias store、最多4个memory live-out slots及controller级原子预算回退 | 3 exits/2 destinations/2 locations/one-hop NoMod；256输入`lli`差分，native `C`生成`B`且Backsolver 3/3/2；MayAlias/缺store/volatile/external predecessor/destination写/第5 slot拒绝；2项定向lit及143/143全量 | 技术档案257、SOTA差距审计G09、Configuration、benchmark README | I/T |
| F263 | 输出前重放capture/resume/destination/scalar tuple，per-memory-slot重新执行MemorySSA/AA并核对store/NoMod sites；规范JSONL/FNV内容身份与独立结构verifier | memory-on与structural-only record验签；exit destination、NoMod site、fingerprint三类篡改拒绝；Python py_compile/Ruff、LLVM18 Werror及143/143全量 | 技术档案258、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F264 | manifest/input IR/lowered IR/SymCC binary/LLVM tool五制品SHA-256+size、LLVM identity、canonical envelope hash、flock/fresh gate/file+dir fsync/atomic rename与完整重验 | seal/verify成功；重复output、lowered IR漂移、envelope字段篡改拒绝；Python py_compile/Ruff及continuation lit | 技术档案259、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F265 | 完整seal identity gate、独立`opt` subprocess、lowering强制关闭、sealed lowered IR的MemorySSA/AA manifest重建、独立验签及record精确相等 | 正向seal→replay；alias修改IR经LLVM verifier并可重新seal，但旧NoMod proof replay拒绝；2项continuation定向lit | 技术档案260、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F266 | 2--4 state同位宽unit-diagonal upper-triangular affine DAG识别、APInt `A,b`、nilpotent/binomial闭式、共享preheader DAG、recurrence/power manifest及独立模矩阵verifier | 三状态链32输入差分、symbolized verifier、1 query/1 SAT trip-5候选；matrix/power/hash篡改及逆三角/non-unit/cross-width拒绝；3项loop lit | 技术档案261、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F267 | 单post-update equality break/implicit continue证明、`break_at<trip` exit identity、实际iteration闭式、normal/header与break/latch phase live-out事务、完整truth-table exit manifest | 64组trip/break差分、跨exit候选、5 queries/3 SAT；table/live-out/hash篡改及4类break拒绝；2项lit | 技术档案262、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F268 | 两侧各1--4块等长linear-arm CFG证明、flattened跨块SSA LCS、全region value map与原子删除；block/alignment/output身份、coverage replay obligation和结构指纹manifest；独立verifier与campaign gate | 2×2正例256输入差分、symbolized verifier；block/alignment/hash篡改拒绝、2×1 unequal-arm不变、F248回归兼容；3项Hydra lit及静态门禁 | 技术档案263、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F269 | 相关exit的MustAlias store/MemoryLiveOnEntry混合证明、path-local capture snapshot、v2 state-kind metadata/manifest；store/snapshot/neutral dispatch PHI实际incoming重放验证 | 256输入差分、symbolized verifier、`C`→`B`跨initial Backsolver候选；kind篡改、重新seal后的constant-incoming篡改拒绝，partial MayAlias fail closed；3项continuation lit及静态门禁 | 技术档案264、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F270 | 每exit最多4个/16节点的bounded acyclic nested-MemoryPhi tree、逐edge MustAlias/LiveOnEntry/NoMod证明、bottom-up scalar PHI、v3 node/edge manifest与actual nested-PHI replay；递归region concrete clone拓扑前移修复 | 两层tree对1024输入差分、symbolized verifier、`C 03`生成`A`且低两位为0；edge tamper及重新seal后的deep incoming tamper拒绝，MayAlias/5-arm fail closed；4项continuation memory lit及静态门禁 | 技术档案265、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F271 | 2--3个post-update equality check chain、strict `(break_at,ordinal)` priority circuit、shared execution closed form、phase-correct per-break dispatch；v2 vector truth-table manifest与独立verifier | 2-break independent 512组、3-break triangular 256组差分及symbolized verifier；break1 solver候选；winner/site/live-out/hash tamper和4-break/`ne`/polarity拒绝；8项loop lit及静态门禁 | 技术档案266、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F272 | 每侧独立1--4个linear blocks/64 instructions、deterministic compatible-LCS unequal alignment与one-sided edit、跨块SSA remap；v2左右规模/edit slot block ordinal/edit distance/output/fingerprint proof及campaign gate | 2x1 one-sided edit与最大1x4跨块def-use各256输入差分、symbolized verifier；ordinal/edit tamper经direct/campaign拒绝，1x5与internal branch tree fail closed；4项Hydra定向lit | 技术档案267、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F273 | 每侧最多7 blocks/3 internal conditions/4 merge leaves的unique-parent DFS tree、block/leaf path guard、predicated LCS ALU、guarded memory readback与multi-leaf PHI恢复；v3完整topology/fingerprint proof及campaign gate | 3x1、最大7x7/8-leaf和aggressive matched-store各256输入差分及symbolized verifier；parent/leaf/successor tamper拒绝，4th branch/empty/reconvergence fail closed；6项Hydra定向lit | 技术档案268、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F274 | 三pipeline canonical seal、独立manifest verifier与ordered proof identities、input/lowered IR/compiler/LLVM/version绑定、配置恢复、fresh lock/fsync/atomic publish；清洁环境从input IR重跑完整pass，manifest record及lowered IR exact match、LLVM verify与结束后seal重验 | continuation memory-on/off、recurrence+three-break loop、max-tree Hydra正向seal/verify/replay；三类verifier-clean重新seal的IR篡改、seal字段篡改和重复output拒绝；4项定向lit、Python静态门禁及155/155全量回归 | 技术档案269、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F275 | 2--8 byte整数load的linear MemorySSA最近写者lane证明、same-base inbounds constant interval、LiveOnEntry补齐、DataLayout双端序extract/compose、v4 metadata/manifest/fingerprint与actual IR expression replay | little-endian两种partial组合256输入差分、`C`到`B`模型、双seal/replay与fresh-seal store tamper拒绝；big-endian专项；dynamic/volatile/MemoryPhi拒绝；v1--v3兼容回归6/6、LLVM18 Werror、157/157 lit及459/459 Python | 技术档案270、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F276 | 单路径一个pointer-select store、逐臂same-base interval/AA NoAlias完备分类、guarded older-lane overlay、v5 guard/polarity/store/source proof与actual i8 select replay | 1024输入差分、`C 01`到`A 01`模型、双seal/replay；guard/fresh-sealed select-value tamper拒绝；unknown arm/双guarded store/argument guard拒绝；2项定向lit、Werror/py_compile、159/159 lit及459/459 Python | 技术档案271、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F277 | 两输入canonical loop MemoryPhi、entry F275 lane proof、latch最近partial store/self-carry fixed point、DataLayout双端序recursive integer PHI、v6 topology/transfer proof与actual PHI/expression replay | little-endian 1024输入差分、`C 01`到`A`非零iteration模型、双seal/replay及carry/fresh-sealed value tamper拒绝；big-endian专项；conditional/dynamic/volatile/full-width/multi-backedge拒绝；3项定向及v1--v6兼容11/11、Werror/py_compile、162/162 lit及459/459 Python | 技术档案272、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F278 | 单store depth-2/3-node/4-leaf unique select tree、逐leaf interval/AA分类、per-lane fallback、v7 node/child/leaf proof与actual nested-select replay | 1024输入差分、双seal/replay、cross-continuation model、topology/fresh-sealed leaf tamper拒绝；depth3/unknown/shared DAG/第二partition拒绝；2项定向、165/165 lit及459/459 Python | 技术档案273、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F279 | 两条single-level guarded stores的MemorySSA newest-first priority、oldest-to-newest lowering、v8稀疏lane priority chain及actual outer-to-inner select replay | 1024输入差分、双seal/replay、new-writer model、priority/fresh-sealed outer value tamper拒绝；v5兼容、第三writer拒绝；3项定向、165/165 lit及459/459 Python | 技术档案274、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F280 | strict write/carry diamond的latch MemoryPhi、共同instruction guard与极性、partial-store/self-carry select、v9七站点拓扑证明及actual recursive-PHI/guarded-byte replay | little-endian 1024输入差分、big-endian false-polarity、双seal/replay、Backsolver model、polarity/fresh-sealed value tamper拒绝；argument guard/双writer/unknown ModRef/non-strict join拒绝；v1--v9兼容16/16、167/167 lit及459/459 Python | 技术档案275、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F281 | one-entry/two-unconditional-latch header MemoryPhi、per-latch partial-store/self-carry transfer、actual三输入integer PHI、v10 ordered backedge proof/replay | 1024输入差分、双seal/replay、Backsolver model、transfer/fresh-sealed value tamper拒绝；third/full-width/nested-conditional latch拒绝；v1--v10兼容18/18 | 技术档案276、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F282 | 最新depth-two pointer-union writer与一个旧single-level guarded writer的MemorySSA优先组合、guarded fallback先行lowering、v11联合proof/fingerprint及actual nested-select replay | 1024输入差分、双seal/replay、Backsolver model、polarity/fresh-sealed selected-value tamper拒绝；反向次序/两个旧guard/跨exit v8-v11冲突拒绝；v1--v11兼容20/20、171/171 lit及459/459 Python | 技术档案277、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F283 | one-entry/two-latch header MemoryPhi、per-transfer conditional/unconditional partial-store/self-carry、actual三输入PHI、v12 conditional topology/polarity proof与共享dominant guard | little-endian mixed transfer 1024输入差分、big-endian双conditional共享guard、双seal/replay、Backsolver model、polarity/fresh-sealed value tamper拒绝；argument guard/双writer arm/unknown ModRef拒绝；v1--v12兼容23/23、174/174 lit及459/459 Python | 技术档案278、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F284 | one-entry/2--4-latch header MemoryPhi、逐transfer ordinary/strict-conditional partial-store/carry、actual 3--5输入PHI、v13 tagged proof/replay | 4-latch mixed 2560输入差分、3-latch all-unconditional big-endian、双seal/replay、Backsolver model、ordinal/fresh-sealed value tamper拒绝；5-latch拒绝；v1--v13兼容26/26、177/177 lit及459/459 Python | 技术档案279、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F285 | 每backedge最多3-node/4-leaf unique-parent nested predicate tree、逐leaf pure-carry或partial-store/carry、actual latch leaf-PHI与1--4 latch header fixed point、v14 tagged topology/provenance proof | little-endian最大树5120输入差分、big-endian tree+ordinary双latch、symbolized verifier、Backsolver、双seal/replay、child/value tamper拒绝；5-leaf fail closed；v1--v14兼容29/29、180/180 lit及459/459 Python | 技术档案280、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F286 | 最多4层的MemorySSA newest-first writer序列、最多2个depth-two pointer partition与2个逐lane guarded writer、shadowed-lane masking、oldest-to-newest last-writer lowering、v15全序proof/replay | little-endian最大交错图192组全writer条件差分及旧high lane显式fallback断言、big-endian双partition、symbolized verifier、Backsolver、双seal/replay、ordinal/polarity/value tamper拒绝；第三partition fail closed；v1--v15兼容32/32、183/183 lit及459/459 Python | 技术档案281、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F287 | PostDominator唯一外部merge、每arm确定性拓扑排序、最多10 block/4 branch/8 leaf/3 local merge/8 local PHI、incoming-edge guard OR、local-PHI ITE与v4完整DAG proof | 最大2+1 local merge/3 local PHI及双output PHI对256输入差分、aggressive guarded readback store对256输入差分、symbolized verifier、manifest/campaign loader、seal/replay及predecessor/PHI/lowered-IR tamper拒绝；external predecessor/cycle/4th merge fail closed；Hydra v1--v4兼容9/9、186/186 lit及459/459 Python | 技术档案282、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F288 | v2 profile原始binary/argv/artifact/selected-stat身份闭环、malformed/empty profile fail closed；四compiler pipeline共享`flock`/partial-write/fsync JSONL publisher | v1兼容、v2 header/manifest/verifier/campaign端到端及重新封存身份篡改拒绝；24并发`opt`记录24/24完整且连续重跑通过，Hydra专项6/6、F288 lit组3/3、187/187 lit及461/461 Python | 技术档案283、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F289 | Ed25519 domain-separated descriptor、external trust roots、RFC9162-style Merkle leaf/node、signed previous-root tree head与inclusion proof、content-addressed ZIP64及atomic import | 6项测试覆盖API/CLI、双head/offline proof、8并发publisher连续index、signature/proof/blob/错误key/log rewrite/role/key-pair/TOCTOU tamper及重复extract拒绝；定向lit 1/1、188/188 lit及467/467 Python | 技术档案284、SOTA差距审计G09、Configuration、benchmark README | I/T/E |
| F290 | LLVM poison/undef/freeze refinement契约、inactive operand安全化、EH fail-closed、LLVM17/18 exact IR/manifest/proof replay证书；Query IR候选准入完整回放 | paired/extra freeze、udiv/EH/manifest tamper、cross-major/same-major/major与kind重标、fixed-base/partial-model回归；LLVM18 191/191、LLVM17 190+1 unsupported、Python 471/471及持久证书 | 技术档案285、SOTA差距审计G09、Configuration、benchmark README、checked cross-LLVM evidence | I/T/E |
| F291 | 固定malloc/calloc区域的symbolic-index byte overlap等式、8层newest-first writer graph、v16 metadata/manifest/fingerprint与actual select/icmp复验 | malloc 768组差分、真实Backsolver、calloc非零base、PowerPC64大端、dynamic extent/external base拒绝、第三partition升级；LLVM18 195/195、LLVM17 194+1 unsupported、Python 471/471及exact cross-major certificate | 技术档案286、SOTA差距审计G09、Configuration、benchmark README、checked cross-LLVM evidence | I/T/E |
| F292 | 固定heap read window的单回边symbolic-index recurrence、all-carry byte PHI、v17 cycle/region proof与actual PHI/select/icmp复验；标准allocator原型门 | 270组差分、真实Backsolver、大端i16 case、extent/source/IR tamper、argument/dynamic extent/two-writer/mixed writer/错误calloc原型拒绝；LLVM18 198/198、LLVM17 197+1 unsupported、Python 471/471及exact cross-major certificate | 技术档案287、SOTA差距审计G09、Configuration、benchmark README、checked cross-LLVM evidence | I/T/E |
| F293 | v4-first兼容、14-block/5-branch/10-leaf/4-local-merge/12-local-PHI/96-instruction v5 SESE DAG；per-arm edge guard与跨arm predicate-negation hash-consing、完整predicate/guard proof | 13-block/4-merge共享predicate正例256输入差分、18 canonical edge/8 cache hit、symbolized verifier、seal/replay、edge/predicate/lowered-IR tamper拒绝；16-block/5-merge fail closed；双LLVM Hydra v1--v5 9/9、LLVM18 199/199、LLVM17 198+1 unsupported、Python 471/471及exact cross-major certificate | 技术档案288、SOTA差距审计G09、Configuration、benchmark README、checked cross-LLVM evidence | I/T/E |
| F294 | 2--4个MemoryPhi互斥回边的fixed-region symbolic-index transfer、单层conditional外guard、v18 presence-aware writer proof及actual PHI/guard/equality replay | x86双回边conditional/unconditional 126组差分、真实Backsolver、extent/source/equality篡改与双seal；PowerPC64四回边i16大端；同latch双writer/dynamic extent/五回边拒绝；双LLVM聚焦3/3、LLVM18 202/202、LLVM17 201+1 unsupported、Python 471/471及exact cross-major certificate | 技术档案289、SOTA差距审计G09、Configuration、benchmark README、checked cross-LLVM evidence | I/T/E |
| F295 | 显式2--4 site同函数v5 dominance-chain batch、function-scoped original-predicate negation cache、actual DominatorTree reuse gate、v6 transaction/structure双fingerprint | 双13-block/4-merge region由8降至5条i1 negation、第二site至少3次cross reuse、256输入差分、symbolization、双record seal/replay与tamper拒绝；逆序/重复/缺失/无共同predicate整批拒绝；双LLVM Hydra v1--v6 11/11，LLVM18 203/203、LLVM17 202+1 unsupported、Python 471/471及exact cross-major certificate | 技术档案290、SOTA差距审计G09、Configuration、benchmark README、checked cross-LLVM evidence | I/T/E |
| F296 | 单回边固定heap window内2--4个same-latch dynamic writer、MemorySSA newest-first proof、oldest-to-newest lowering与actual select/equality peeling、v19 ordered writer fingerprint | 小端3-writer 105组差分、真实Backsolver、writer order/extent/actual equality tamper与双seal；PowerPC64双writer大端；5 writer/unknown ModRef拒绝；双LLVM v16--v19兼容12/12、LLVM18 206/206、LLVM17 205+1 unsupported、Python 471/471及exact cross-major certificate | 技术档案291、SOTA差距审计G09、Configuration、benchmark README、checked cross-LLVM evidence | I/T/E |

以上“技术档案 N”均指
[`New_Implementation_Archive.md`](New_Implementation_Archive.md) 的相应章节。
P08 提交后，必须在本文件补充实际 commit 区间。

### 4.2 历史基础与引擎实现 F17-F25

| ID | 权威代码/脚本 | 验证证据 | 详细文档 | 状态 |
| --- | --- | --- | --- | --- |
| F17 | `util/mpi_concolic_execution.py`, `util/mpi_fuzzing_helper.py` | serial/MPI/multi-master runs | `MPI_Parallelization.txt`, `Parallel_Architecture_v2.md` | I/T/B |
| F18 | MPI helper, `benchmark/profile_bottleneck.py` | profile CSV/JSON、scaling benchmark | `Parallel_Architecture_v2.md` | I/T/B |
| F19 | MPI helper, persistent target script | synthetic/public scaling、多实例负结果 | `Research_and_Optimization_Report.md` | I/T/B |
| F20 | `util/grimoire_gen.py`, benchmark runner, QSYM runtime | LAF/NGRAM/GRIMOIRE/honggfuzz 固定配置 | `Research_and_Optimization_Report.md` | I/T/B |
| F21 | benchmark runner 与 public setup/build scripts | full benchmark、10 targets、190 ranks | benchmark README 与评估报告 | I/T/B |
| F22 | MPI helper 与 benchmark runner | phase/redundancy artifacts | `SymSan_工作总结_PPT补充材料.md` 及历史结果 | I/T/B |
| F23 | `util/concolic_engine.py`, engine dogfight scripts | parser、coverage、full-hybrid 对拍 | `engine_abstraction.md` | I/T/B |
| F24 | SymSan patches/build scripts | public targets、6-target x 3 rounds | `symsan_ported_techniques.md`, `symsan_hybrid_benchmark_6targets.md` | I/T/B |
| F25 | `setup.sh`, `package.sh`, SymSan build script | smoke、offline、debootstrap | root README 与 package/setup history | I/T/B |

## 5. 文档职责

| 文档 | 权威范围 | 不应承担的职责 |
| --- | --- | --- |
| `New_Implementation_Archive.md` | F00-F296 的方案、实现、证据、边界 | 逐提交日志 |
| `Development_History_Traceability.md` | commit/阶段/功能/证据映射 | 重复全部实现细节 |
| `Configuration.txt` | 用户可见开关、默认值、产物 | 性能结论 |
| `sota_hybrid_execution_2026.md` | 最新文献、技术定位、未决差距 | 宣称本地实现已经 R 级 |
| `Recent_Work_and_Next_Research_Plan.md` | 当前阶段与后续科研计划 | 取代历史事实 |
| `benchmark/README.md` | 运行、schema、复现和分析命令 | 跨版本混合比较 |
| 专题/阶段报告 | 保留当时实验条件与观察 | 作为当前状态唯一来源 |

当专题报告与总账冲突时，以总账的当前实现状态为准；历史数字仍以产生该数字的
原报告为准，不静默改写。

## 6. 尚未形成 R 级证据的工作

文档完整覆盖不等于科研验证已经完成。当前最重要的实验缺口是：

1. 用相同初始 corpus、相同总 CPU、固定版本和固定随机种子重跑 baseline、
   单功能消融与 full system；
2. 用 F67 manifest 分离 tuning/confirmatory，执行最终比较至少 20 次独立运行；
3. 报告 coverage AUC、final coverage、time-to-target、accepted/CPU-hour、
   solver cost 和 worker redundancy；
4. 用 F67 paired bootstrap、randomization test、A12/Cliff's delta 与 Holm correction，
   并保留失败、超时和零收益目标；
5. 对 F23/F24 同时报告兼容性、吞吐、覆盖与失败模式，不用单个 dogfight 推广到
   所有程序。

这些缺口属于“科研证据未完成”，不是“实现没有记录”。

## 7. 后续维护流程

每次合并新功能时执行：

1. 在技术档案分配 Fxx，并记录方案、可信边界、代码、开关、测试和结果等级；
2. 在本文增加 commit 或工作树阶段映射；
3. 更新 `Configuration.txt` 和 benchmark schema；
4. 运行 `git diff --check`、本地 Markdown 链接检查和 `ninja -C build check`；
5. 只有满足 R 级条件后，才把“已实现”更新为“实验支持提升”。
