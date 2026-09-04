# SOTA 技术差距审计（2026-07-24）

- 最近更新：2026-07-30（F56--F276 bounded Source/Optimal-DPOR、ConDPOR
  regeneration/enabledness、extended RA atomics、executable continuation IR、
  conservative LLVM/static-memory lowering、guarded pointer provenance union、
  bounded multi-instance/runtime-sized nullable heap、bounded cross-function object
  references/caller-domain certificate、pointer-returning bounded indirect-call
  dispatch 与
  deterministic memory-compare/region-write/NUL-string/search/copy/scalar summaries、
  LLVM integer defined-value guards、
  bounded data/function pointer-valued memory provenance、global pointer table
  与 direct-predecessor merge、proven-nounwind invoke normal edge、
  stable dynamic nondeterministic freeze、
  bounded cyclic pointer-memory SSA fixed point、
  CAS-rooted incremental feasibility context、validated cross-prefix poly reuse、
  verifier-gated optimistic generator simplification、native Z3 tactic model
  conversion、validated cross-size polyhedral projection、bounded
  structure-preserving field renaming、persistent Query IR converter replay、
  exact integer projection relations、cross-size endian-aware field alignment、
  exact-proof unsigned widening/narrowing、cost-aware selective mixed
  completion、Query-IR-verified grammar hole completion、
  plateau-gated retained-history acquisition、independent parser oracle、
  contextual grammar conflict splitting、bounded packed parser families、
  shared packed DAG/deep ECT、nullable SCC least fixed point、
  atomic multi-nonterminal ECT transactions、
  content-addressed incremental parser cache protocol、
  persistent native Tree-sitter incremental parser与node-ID reuse proof、
  proof-carrying parser telemetry与sealed cold/incremental cost ablation、
  persistent generalized Earley complete-SPPF adapter与nullable certificate、
  proof-carrying complete-forest telemetry与sealed cross-parser calibration、
  same-candidate dual-parser acceptance confusion proof与sealed command pair、
  parser-neutral selected/forest span与boundary correspondence certificate、
  independent Parglare GLR complete-SPPF differential oracle、
  accepted-forest PCFG posterior与packed-DAG inside/outside调度、
  relation-aware synchronized slots与exploration reserve、
  hierarchical parent-conditioned PCFG与context-state forest mass、
  prequential calibration与drift-safe global fallback、
  fixed-recency stale/drift/recovery gate、
  bounded adaptive log-loss window与verifiable cut certificate、
  verifier-gated grandparent multi-context probabilistic circuit、
  bounded ordered-sibling autoregressive factor与精确factor消息、
  five-level cost-faithful PCFG ablation与sealed confirmatory artifact、
  context-wise anytime-valid repeated-forward-CS drift certificate、
  portable signed-integer schedule SMT evidence、
  fail-closed MPI grammar-hole QueryStore lifecycle、
  proof-carrying epsilon derivation、exact String/BV dual view、
  guarded runtime string operations、validation-first parallel String
  portfolio、exact-subdomain decimal conversion、proof-carrying
  backend-neutral QF_BV lowering/cvc5 capability matrix、
  prefix-keyed persistent SMT-LIB contexts 与 sealed research protocol）

## 1. 审计目标

本审计回答一个严格问题：当前 SymCC-Parallel 相对 2023--2026 年并行符号执行、
concolic execution 和 hybrid fuzzing 的代表性工作，还有哪些技术没有实现，或只
实现了不能等同于论文系统的子集。

项目的目标仍是以并行符号执行提高 fuzzing 的覆盖效率。本文不把漏洞利用、攻击链
生成或武器化能力纳入范围。安全会议论文仅作为程序测试、路径探索和约束求解的技术
来源。

审计范围：

- LLVM/C/C++ 编译型符号执行和 concolic execution；
- 多 worker 状态/种子/查询并行；
- SMT、polyhedral abstraction、缓存和多解生成；
- 面向结构化输入的 data/string/grammar coverage；
- 并发程序的输入与调度联合探索；
- 可验证的 LLM/agentic proposal；
- 以 coverage per CPU-hour 为主指标的 hybrid fuzzing。

不作为主线差距：仅适用于 EVM、浏览器 Wasm、MCU 或特定硬件平台的系统。它们只在
其机制可迁移到本项目时列为 P2 研究方向。

## 2. 判断口径

本文使用四种状态：

| 状态 | 含义 |
| --- | --- |
| `I` | 论文的核心语义已进入实现，并有针对性测试 |
| `P` | 只实现论文思想的有界子集、调度近似或接口占位 |
| `M` | 代码路径不存在，或现有能力不能完成论文的核心操作 |
| `E` | 有实现但只有单元/集成证据，尚无等 CPU、多轮公开 benchmark 证据 |

特别说明：

- 名称中的 `-style` 只表示借鉴设计思想，不表示复现论文。
- 一个环境开关、调度 route 或 JSON 字段不是对应算法的实现证据。
- candidate 通过原程序 replay 能保证最终保留结果可信，但不能把 candidate
  生成算法自动升级为论文等价实现。
- 下文的 “SOTA” 表示近期同行评审工作或明确标注的最新预印本，不表示本项目已在
  benchmark 上达到最优。

## 3. 结论摘要

当前系统已经有较完整的工程骨架：LLVM 插桩、QSYM/SymSan 双后端、MPI seed-worker
调度、PrefixDAG/ECT 摘要、Pangolin 风格线性 polyhedral 采样、AFL 原生反馈、
bounded Backsolver/Veritesting、候选 replay 验证和 benchmark 基础设施。

严格审计以及本轮 G01、G03、G02/G18 落地后，Query IR/异步求解、static Data
Coverage、reusable solution generator 和 AFL 原生 full-matrix polytope sampling
已有核心实现与针对性测试，但仍缺公开 benchmark 证据。其余条目的优先级和状态
继续按下文逐项维护；当前最影响研究定位的基础缺口是：

1. executable continuation IR 已能在 MPI 中 fork/resume，LLVM/C bounded integer
   SSA、static-global memory 与 exact `(pointer,size)` symbolic input buffer 也可
   自动 lower，fixed frame-local stack 与 bounded call-site malloc/free lifetime
   也已有保守实现；单变量 symbolic GEP 以及 acyclic pointer PHI/select 的 guarded
   cross-object finite alias/ITE 子集已实现；同一 allocation site 的固定容量多
   live instances、GEP-after-union，以及无环 direct-call graph 上的 pointer
   argument/return object identity 传递也已实现；QF_BV feasibility check 现以
   CAS solver root 复用 exact/parent context，并保留 cold rebuild/one-shot
   fallback；Pangolin-style full-matrix cache可按linear
   equivalent/subset/compatible关系扫描跨prefix SAT abstraction，并对每个model/
   sample复检当前完整prefix与target；caller symbolic-GEP 的 finite domain certificate 可随 pointer
   argument/return 跨 frame 传输；local finite function-pointer select/PHI 已能
   guarded dispatch，包含 pointer-returning target 与 pointer actual forwarding；
   constant-length memcmp/bcmp、memcpy/memmove/memset 与 guarded
   strlen/strcmp/strncmp 已可
   展开到 core IR；动态/通用
   heap、多项 alias、general points-to、一般 external-state/exception 与任意
   native continuation 尚未实现；
2. 通用解生成器已覆盖 byte/range/field、有界可逆 BV chain、带断言分区证书
   和完整 solver 复核的 target-slice optimistic simplification，并实际执行 Z3
   `simplify/solve-eqs` model converter 枚举子目标模型；尚未序列化 context-local
   converter 闭包，也未覆盖多 subgoal/更多 tactic pipelines；
3. string theory 和 grammar synthesis 的语义覆盖不完整；
4. 真实多solver portfolio、proof-carrying backend-neutral QF_BV lowering/cvc5
   model validation、prefix-keyed persistent SMT-LIB context、QueryStore partial solution cache、solver-helper
   recent-SAT probe及proof-carrying assumption-conflict PSCache已实现；
   逐次CDCL/bit-blast trail采样仍是可选低层后端缺口。

因此，当前系统可以准确称为“并行 hybrid concolic framework”，并具有可序列化
symbolic-state 数据面和可执行 continuation-IR control plane。它已经能在 MPI lease
中恢复/fork portable CPS-like states，并从 LLVM/C 的 bounded integer-SSA、
static-global/input-buffer/fixed-stack/bounded-multi-instance-heap/finite-alias
memory 与 bounded cross-function proof-carrying object-reference 子集生成这些状态，但还不能从任意 LLVM/native
instruction暂停/恢复
完整 GenSym continuation；也不能把同一 Z3 后端的配置序列称为完整
SMTgazer。

## 4. 代码级证据

| 能力 | 当前代码证据 | 严格判断 |
| --- | --- | --- |
| 分布式 state | `util/distributed_state.py` 的 state task 可含 input/focus/target/action/schedule prefix；`LiveStateStore` 持久化 expression、solver frame、symbolic store、page-COW memory 和 continuation roots | 已有可迁移 state data contract；仍不暂停/恢复 native PC、register、call stack 或外部资源 |
| LLVM continuation frontend | `compiler/ContinuationLowering.*` 导出 integer SSA、PHI edge copies、switch/direct call、static global、exact pointer-size input、fixed frame-local stack、bounded call-site malloc/calloc/realloc/free pool、constant/one-term symbolic GEP、GEP-after-union、guarded pointer PHI/select union、direct/finite-indirect pointer argument/return summary与 caller-domain BV certificate、local finite function-pointer dispatch，以及 constant-length memcmp/bcmp/memcpy/memmove/memset 与 guarded strlen/strcmp/strncmp core-IR summary；`util/llvm_to_continuation.py` 与 `run-llvm` 接入 executor/MPI | bounded integer-SSA/object-memory/lifetime/cross-object finite-ITE、nullable runtime-sized heap、in-place realloc、无环 direct-call proof-carrying object-reference、guarded select/PHI indirect-call及 pointer return、只读 memory-compare、region-write 与 NUL-aware string 子集已可执行；multi-term symbolic address、general points-to、moving/custom allocator、heap graph、dynamic stack、callee-stack escape、其他 pointer ABI、exception、一般 external call、recursion 和完整 poison/UB 保守拒绝 |
| ECT / constraint channel | `util/expressive_coverage.py` 保存 branch/call-context/taken/visit/reward 等结构；`util/query_store.py` 独立保存完整 Query IR。F399按官方artifact的双通道事实纠正旧判断，并实现带跨prefix-target read alias的alpha-normalized constraint class | ECT本就不应承载solver AST；结构约束选择核心已实现，完整LLM seed acquisition/solve-complete协议和公开实验仍缺 |
| SMT 调度 | `util/smt_algorithm_scheduler.py:125-173` 的 exact/fallback 均为 Z3 配置 | 是 runtime profile bandit，不是多 solver/algorithm portfolio |
| 参数自配置 | `self_config.py` 与 `symcc-query-solver --print-parameters` 提供provider v1、原子registry、task/query-service/campaign scope和schema-bound state；F397实现program-bound value-space/MeanShift/silhouette；F398实现assignment-local branch-rarity standalone/synergy parameter selection与三文件恢复 | 本项目原生枚举、生命周期、论文value sampling和parameter selection核心已闭合；通用help/binary extraction、sklearn逐决策cross-oracle、服务级配置进程池与公开等CPU benchmark仍缺 |
| Data Coverage | `compiler/Symbolizer.cpp` 的 load provenance/compare/switch 插桩，`compiler/Pass.cpp` 的 constant-global registry，QSYM `solver.cpp` 的 ELF segment/access abstraction | 核心 static-data 语义已实现；真实 parser 收益仍待实验 |
| Data novelty | `util/hybrid_feedback.py:2235-2334` 以 `(object,offset,width)` 保存 winner 和 code-summary refinement；AFL preload 保持独立兼容通道 | 无碰撞持久 novel set 已实现；不物理删除 AFL queue |
| Pangolin reuse/mutator | QSYM runtime 对 full linear matrix 做 bounded cross-prefix relation ranking、shared-offset interval sampling envelope、exact existential integer projection relation、cross-size coefficient-preserving endian/field renaming与exact-proof unsigned widening/narrowing、完整prefix/target复检；`util/afl_symcc_hint_mutator.py` 读取 model、byte box 和 full matrix并在输出前检查约束 | bounded cross-prefix/cross-size/exact-projected/endian-permuted/width-aligned SAT abstraction与原生全矩阵mutator已实现；无量词projection artifact、sign-extension/packing和公开benchmark仍缺 |
| Backsolver | `compiler/Symbolizer.cpp:40-41` 将 region 限制为深度 12、32 blocks | 是 bounded acyclic SSA/ITE 子集，不是通用 IFSS 状态探索与合并 |
| DPOR | `util/schedule_exploration.py` 支持依赖图/Mazurkiewicz certificate、causal source/sleep sets、weak-initial wakeup tree、cooperative ready evidence、bounded po/rf/co execution graph/backward revisit/maximal extension、SC/TSO/RA、single-context Query IR × schedule × RF、lifecycle partial order/lazy refinement 和 checked replay | 已有 bounded Source/Optimal-DPOR 与 ConDPOR data structures/operations；仍不具备 interpreter-level path-dependent event generation、unbounded sound/complete/optimal proof 或完整 C11 性质 |
| Agentic proposal | `util/semantic_proposals.py:280-329` 产出确定性 byte candidates | 没有生成、编译和验证 ghost code，也没有 LLM constraint-core refinement |
| Solver domain | QSYM inline `solver.h` 仍依赖`z3++.h`；异步QueryStore已有Query-IR-derived标准QF_BV lowering、capability certificate、cvc5模型双验证及prefix-keyed persistent push/pop process pool；`runtime/src/backends/qsym/Runtime.cpp:435` 不支持 FP | 异步QF_BV已有backend-neutral incremental portfolio基础层；inline路径、FP/array/string、跨worker clause/context transport仍未统一 |

## 5. P0：应首先落地的核心缺口

### G01. 可持久化 Query IR、trace/solve 解耦和全局 query trie

**状态：`I/E`（核心机制已实现并测试，科研效果待验证）。**

Triereme 把 tracing 与 solving 放入不同进程，以 query trie 组织公共前缀，从而同时
获得 pruning、incremental push/pop 和跨执行缓存。Marco 同样把约束采集、全局路径
价值评估和异步求解分开。当前实现已经提供：

- `symcc-query-ir-v1` versioned expression DAG、path/query record、concrete witness、
  dependency set 和稳定 metadata；
- SHA-256 expression/artifact/query store、SQLite prefix trie、DFS/BFS/priority
  调度、fenced lease、timeout retry 和损坏 spool quarantine；
- tracer 原子发布 immutable query，独立 worker 在 tracer 退出后继续消费；
- 持久 Z3 helper 按 prefix hash 缓存上下文，并对目标执行
  `push -> add -> check/model -> pop`；
- exact result cache，以及基于规范化 clause-set subset 证明的 UNSAT-superset
  pruning；
- SAT assignment 按 query ID 回传，候选重新进入 MPI/AFL concrete replay。

`query_prefix_reuse.py` 已证明第二个公共前缀查询出现真实 cache hit；
`query_ir.c` 已证明 `SYMCC_QUERY_DEFER=1` 时 tracer 的 inline solver query 为 0，
进程退出后仍得到正确候选；`test_query_store.py` 覆盖 lease fencing、内容寻址和
UNSAT 超集剪枝。实现细节见技术档案 F27。

剩余研究验证：

- 与 inline Z3 对拍更大规模的 SAT/UNSAT/model corpus；
- 公开 benchmark 上报告 query redundancy、prefix-cache hit、solver CPU、RSS、
  coverage/CPU-hour 和 candidate validity；
- 当前 helper 只有 Z3 QF_BV，跨 solver portfolio 属于 G06；
- 当前 UNSAT reuse 使用完整已证集合；G07已实现QueryStore partial assignment reuse
  与可验证/可最小化的assumption-conflict core，尚未实现每个内部CDCL trail冲突的
  低层投影。

这是 GenSlv、完整 SMTgazer、PSCache 和真正跨 worker solver reuse 的共同前置。

### G02. GenSlv 式通用 generator solving

**状态：`I/T/E`（核心 generator 语义、持久化重放与验证边界已实现并测试，
科研效果待验证）。**

ICSE 2026 的 GenSlv 不为一次查询只返回一个模型，而是提取可重复调用的输入生成器：
复用 Z3 invertible model converters、构建层次化 range sampler，并用 optimistic
simplification 扩大可生成解集合。论文直接在 SymCC 上评测。

当前实现已经提供：

- `symcc-solution-generator-v1`：fixed assignments、byte ranges、field hints、
  converter_chain、verified_models、deterministic seed、metrics 和 content hash；
- Query IR 上的有界可逆 equality converter：`concat` split、extension、bitwise/arithmetic
  inverse、odd modular multiplication inverse 和 rotate inverse；
- persistent `symcc-query-solver` SAT 后在完整 solver context 中为 input bytes 计算
  exact lower/upper；F177 也可在仅含 target assertions 的弱化 solver 上计算更宽
  lower/upper，并用 boundary/midpoint/random field sampler 产生 proposal；
- 每个 solver-side model 都重新以完整 byte assignment 检查 SAT；query store 再把
  generator 与 result 在同一事务中持久化，并把所有 verified model 物化为 candidate；
- `symcc-optimistic-simplification-v1` 记录原 query hash、prefix/target assertion
  边界、完整 kept/dropped partition、`original-implies-weakened` 和强制完整验证
  义务；normalizer 对 partition 与 validation metrics 做 fail-closed 检查；
- F178 对 proposal goal 实际运行 Z3 `simplify & solve-eqs`，枚举 surviving-byte
  subgoal models，并用 `Z3_goal_convert_model` 恢复被消去输入；恢复后的全部 byte
  assignments仍由原full-prefix solver验证；
- `symcc-z3-tactic-model-converter-v1`持久化pipeline/provenance/subgoal/model与
  validation漏斗，tactic失败只回退range sampler，不改变主SAT结果；
- F181把Query IR inverse提升为严格版本化recipe，离线sampler执行单条和无冲突累积
  replay；未知step、越界assignment和关闭full-validation义务均fail closed；
- QueryStore以持久化完整prefix/target roots的bounded evaluator验证离线proposal，
  manifest区分`generator_replay_verified/query_ir_verified`与`solver_verified`；
- generator artifact 进入 `generators/<hash>.json`，concrete replay仍是最终保留边界。

剩余研究验证与语义差距：

- 当前同时保留可跨进程执行的Query IR inverse recipe和实际Z3 tactic converter；
  后者的context-local闭包不能由Z3 API直接序列化，artifact保存pipeline/provenance
  与已完整验证的转换模型；
- optimistic simplification 是可审计的 target-assertion subset；当前尚未支持
  multi-subgoal converter composition、非BV8 surviving variables或自适应 tactic
  pipeline；
- helper 只覆盖 QF_BV byte variables，string/array/domain-specific generator 属于
  G04/G08；
- 公开 benchmark 仍需报告 generator coverage、models/query、valid-model ratio、
  unique edges/query、coverage/CPU-hour，以及与 single-model、poly-only sampler 的消融。

### G03. 完整 Data Coverage，而非 comparison-prefix coverage

**状态：`I/E`（核心机制已实现并测试，科研效果待验证）。**

USENIX Security 2024 的 Data Coverage 覆盖 immediate 和 static constant data，
把六类数据访问经静态分析和运行时抽象为 `(address,length)`，并维护 novel data
set。其关键价值包括 lookup table、lexer/parser automata、图和 DSL 数据结构；
这些场景并不一定产生常量比较。

当前实现已经提供：

- LLVM load/static-storage provenance 回溯；simple load、load-based predicate、整数
  comparison、switch 和 region comparison 的分类插桩，trivial branch 仍交给 edge
  coverage；
- constant-global ctor registry，以及 `dl_iterate_phdr` 构造的已加载 ELF/module
  readable/non-writable segment 后备表；
- 相等谓词按 equal-bit count、有序谓词按 leading-equal bits、region 按相等前缀加
  首个差异字节计算有效长度；
- switch 只探测相邻 case 或 endpoint，并输出 switch/probe 数供测试和消融；
- ASLR 稳定的 `(object_id, byte_offset, effective_width_bits)`，且细粒度 object
  优先于包含它的 segment；
- telemetry 中的结构化 kind 0/1/2 feature，以及 coordinator 独立持久的无碰撞
  novelty/dominance 状态；winner 仅在 matched bits 严格提升时替换，不删除 AFL
  自有 queue 文件；
- `data_coverage_static.c` 覆盖 lookup table、static graph、predicate、region 和
  switch，`data_coverage_dso.c` 覆盖未插桩 DSO，跨 ASLR ID/path 稳定。
- F247修复AFL兼容平面的两个正确性缺口：comparison hook的site ID改为module
  basename加relative offset，跨PIE/ASLR稳定；preload在PCGUARD/forkserver启动前
  预留低64 KiB data namespace，不再把内部`__AFL_MAP_SIZE`分配上限误当有效map。
  Edge与combined replay加载同一preload并只切换data写入，保证guard ID相同。
- `qf_bv_coverage_join.py`把F246 SAT candidate与原witness送入真实
  `afl-showmap -S -e`，双重复稳定后封存edge/data sparse map、coverage/solver
  child CPU、pair complementarity及F243 v1/v2 holdout calibration；离线verifier
  重建全部join，真实semantic replay通过。

剩余研究验证：

- 在真实 flex/bison automata、static graph、DSL/parser target 上进行等 CPU 多轮
  ablation，报告 data-feature AUC、edge coverage、refinement 和插桩开销；
- 当前 code-equivalence 使用完整 path fingerprint，保守但可能漏掉等 edge-set
  seed replacement；需要以真实 coverage feature set 做更宽松且可证明的 dominance；
- runtime 初始化后才 `dlopen` 的未插桩 DSO 尚不会自动重扫；
- AFL preload 的 comparison-prefix bitmap 是兼容平面，不应表述为无碰撞 static
  novelty；64 KiB hash仍可能碰撞，权威无碰撞数据集合位于scheduler state。
  F247 checked evidence只是单字节synthetic机制验证，公开parser/static-data target
  的20次等CPU效果实验仍未执行。实现细节见技术档案F28与F247。

### G04. SymCC-str 式 bit-vector/string 双表示

**状态：`P/I/T/E`（offset-aware bridge、单context精确String/BV dual view、
backend-neutral CLI/并行portfolio、overflow-proof受限conversion、Z3/cvc5可执行conformance与
validation-aware上下文backend选择已实现；完整incremental IR与operation语义待完成）。**

TACAS 2026 的 SymCC-str 直接建立在 SymCC 上，通过 smt-switch 同时维护 bit-vector
和 string 表示，在真实 C 字符串程序上取得覆盖增益。当前仓库携带的 Z3 源码包含
string theory。本轮实现先补上一个保守子集：

- `_sym_expr_input_offset` 只对 QSYM direct `ReadExpr` 暴露 input offset；
- `memcmp/strcmp/strncmp` wrapper 可写出 `symcc-string-constraint-v1` JSONL，记录
  concrete token、NUL flag 和 exact input-offset patches；
- `util/string_constraints.py` 能严格校验并 materialize string candidates；
- AFL hint mutator 能读取 JSONL 并作为 `symcc_string` stage 直接 patch seed；
- `string_constraints.c` 证明真实 instrumented `strcmp` 可把 `xxxxx\0zz` 的符号输入
  还原为 `MAGIC\0zz` 候选。
- `symcc-string-query-v2` 已支持 bounded String term/predicate 子集，`symcc-query-solver
  --generic` 可通过 Z3 String model 回填输入 offset，MPI helper 和
  `util/symcc_string_solver.py` 可把 verified `string-solver-*` 候选送入原有 triage；
- v2在同一context声明String与逐offset 8-bit BV，精确链接字符、长度、first-NUL，
  并允许BV byte predicates与contains/indexof/substr联合求解；v1兼容升级；
- concrete evaluator复核两个view，显式BV model优先于可能损失binary信息的String
  C API序列化；NUL缺失和非NUL动态长度现在fail closed；
- `str.contains`参数顺序错误已通过真实Z3联合query回归修复，binary literal不再
  限于printable ASCII。
- `symcc-string-operation-v1`从bounded direct-input `strlen/strchr/strstr`导出
  first-NUL span与observed length/index，并转换为alternative length/indexof；
- runtime同时保留first-zero ITE、strchr match/terminator和product-bounded strstr
  BV contains语义；无法证明连续provenance或超过预算时不做近似。
- `symcc-json`/SMT-LIB CLI adapter可并行1--8个backend，SMT-LIB只交换显式BV model；
  每个SAT concrete复检，duplicate/invalid/disagreement分开，UNSAT不产生剪枝。
- guarded `atoi`把1--10位、无符号、first-NUL、`int`不溢出的decimal子域同时编码为
  regex、`str.to_int`和32-bit BV fold；域外C语义不近似。
- guarded `strtol`增加optional minus与target-long范围，但只允许null endptr、
  concrete base10和非ERANGE path；base/endptr副作用不做不完整近似。
- 五case可执行conformance gate要求每个SAT通过独立concrete evaluator；内置Z3与
  cvc5 1.1.2真实进程均通过binary、String+BV、negative indexof、atoi和strtol10；
- 对拍把方言相关的`bv2int`、`str.to.int`、`str.to.re`和词法负数统一为可移植的
  `bv2nat`、`str.to_int`、`str.to_re`和`(- N)`。
- opt-in contextual selector按operation/capacity/variable/BV context学习独立复核
  SAT yield与成本，带cold-start、周期探索、原子锁定持久化和严格per-query调用预算；
  conformance旁路选择并测试全部backend，UNSAT仍无proof权。
- guarded `strtoul10`覆盖target unsigned-long纯decimal范围；atoi/strtol/strtoul的
  runtime BV fold加入逐digit quotient/remainder overflow guard，符号候选不能逃逸到
  libc不一致的回绕/ERANGE域；Z3/cvc5门禁扩为六case。

这解决了“字符串语义候选无法按 offset 复用”和“已相等字符串路径缺少 alternative
candidate”的工程缺口，并形成单Z3精确双view baseline，但还不是完整 SymCC-str：

应继续实现：

- 将当前backend-neutral SMT-LIB artifact进一步下沉为smt-switch incremental IR；
- 在固定版本环境继续完成Princess/Z3str3语法、option、model conformance，并为
  Z3/cvc5结果保留版本化证据；五case门禁不替代完整标准或性能验证；
- `strtol/strtoul`的non-null endptr、base0/2--36、whitespace/sign、suffix消费与
  errno/overflow state，及编码、大小写、Unicode等转换model；
- substring/concat来自程序中间string object时的lifetime/alias-aware guarded query；
- 无法提升时保守回退 bit-vector，不改变现有 concrete semantics；
- 与 SymCC-str 论文 benchmark 和解析器目标做等 CPU 对拍。

字符串理论与 G03/G08 可组成统一的 semantic-coverage 层：code edge、constant data、
token grammar 和 string constraint 分别描述不同的程序状态。

## 6. P1：完成论文语义并建立真正并行 SE 内核

### G05. GenSym 式 live symbolic state/continuation 并行

**状态：`P/I/T`（live continuation descriptor、content-addressed solver/store、
page-COW memory、可执行 continuation IR、MPI frontier lease、conservative LLVM
integer-SSA/static-global/input-buffer/fixed-stack/bounded-heap/finite-alias
memory lowering、guarded pointer provenance union 与 CAS-rooted incremental
QF_BV feasibility pruning
已实现；
完整 LLVM/native continuation semantics 未实现）。**

GenSym 的核心是把 symbolic branching 编译成 continuation-passing style 的协作式
并发；被调度的是包含程序位置、symbolic store、memory 和 path condition 的可恢复
执行状态。当前 `StateShardCoordinator` 对 seed replay 元组做 ownership、lease 和
work stealing，这些控制面能力已复用；F38 提供 canonical live continuation
descriptor，F59 实现可移植 solver/store/memory data plane，F66 又让 bounded
CPS-like IR 能从 checkpoint 恢复、symbolic fork 并通过 MPI frontier 继续执行。
F68 又将 bounded integer-SSA LLVM/C 模块自动导出为该 IR，并在提交 branch child
前用完整 CAS path frames 调用现有 solver helper。当前编译器仍只覆盖保守子集，
不把 unsupported LLVM 结构静默近似。F69 进一步为 referenced initialized global
建立 object-aware synthetic memory，支持 constant GEP 和最多 8-byte integer
load/store，同时让 executor 独立复检 bounds/read-only 并拒绝 symbolic address。
F70 又把 exact `(address-space-0 pointer, integer size)` entry 映射为同时具有
capacity 和 actual-length 边界的 symbolic input object。
F71 为 fixed alloca 增加 owner-function object、dominating-store initialization
proof、per-depth CAS markers 与 return reclamation。
F72 为 direct constant-size malloc call site 增加 fixed synthetic heap slot、
explicit alloc/free ops、live/init CAS markers 与释放后重分配语义。
F73 将 one-term symbolic GEP 分解为 finite in-object aliases，并以 signed
no-wrap index domain、ITE load、guarded byte store 和 conditional
initialization marker 保存可复检有限语义。
F74 递归保留 acyclic pointer select/PHI 的 guarded object alternatives；memory op
以 case-local object/index contract 实现 cross-object finite load/store，PHI edge
discriminator 避免用 synthetic numeric address 猜测 provenance。
F75 将每个 malloc site 扩展为 fixed-capacity stable slot pool；canonical first-free
选择给可交换空闲实例一个确定性命名，malloc pointer alternatives 继续通过 F74
case guards 保留每个 instance 的 object identity。GEP provenance 同时改为递归
变换 base alternatives，因此支持 pool pointer 和 select/PHI 后的 GEP。
F76 在无环、可达的 direct-call graph 上构造有限 pointer formal/return summary：
调用点以 `formal==candidate-address` 重写实参 provenance，返回点以
`call-result==candidate-address` 重写被调函数 provenance。artifact 显式记录
`pointer_params`、`pointer_return_bits`、call-side `pointer_args` /
`pointer_result_bits` 和 return-side `pointer_bits`，executor 在载入 artifact 和
运行时再次检查位宽、参数位置及签名一致性。caller stack object 通过 active
owner-frame depth 访问对应初始化 marker；callee-local alloca escape、递归导致的
owner ambiguity、caller-side symbolic GEP domain 丢失以及 indirect call 均
fail closed。
F77 以 CAS `path_condition_root` 作为 state-local solver context identity。
exact root 使用同一 Z3 solver 的 push/pop 探测候选；child root 可从缓存 parent
assertions 加单个 frame delta 派生；cold worker 从完整 CAS chain 重建。MPI worker
跨 lease 保留 executor/helper，server failure 则熔断到 one-shot generic query。
该 cache 不进入 checkpoint identity，迁移或丢失只影响性能。
F78 为 dynamic malloc/calloc 引入 `bounded-pool-nullable`，把物理 slot capacity
与 `@heap:size:<base>` 逻辑长度分离。first-free、live、size、init 和 free 都可为
条件表达式；zero/overflow/超界/容量耗尽返回 null，memory domain 同时证明
liveness 与逻辑 bounds。Calloc 做目标位宽乘法 wrap 检查和条件零初始化；
realloc 实现 C 允许的 bounded in-place strategy，覆盖 preserve/grow/shrink/
zero/failure，pointer `eq/ne` 使 OOM 分支可由真实 LLVM/C state fork。
F79 为每个 direct-call pointer actual 构造 caller-domain 1-bit BV certificate：
provenance arm guards 与 signed dynamic-index bounds 在 caller 合取/析取，随后通过
hidden parameter 进入 callee；pointer return 用独立 domain destination 反向传输。
Callee memory case 同时要求 address equality 与 certificate true。Artifact validator
精确匹配两侧 mapping，runtime 复检 1-bit 宽度，false certificate 由 feasibility
solver 剪枝。
F80 对 local function constant/cast/select/acyclic-PHI 做最多 64 targets 的 finite
points-to，分配 stable function ID，并以 selector equality 与 edge guards 驱动
symbolic dispatch fork。Reachability/recursion graph 纳入全部 target edges；
`param_bits`/`return_bits`/`result_bits` 让 validator 与 runtime 可独立复检间接
目标 typed ABI，而不是只相信参数个数。
F81 将 constant-length `memcmp`/`bcmp` 展开为 object-aware byte loads 和
lexicographic BV/select DAG，返回 canonical `-1/0/+1`。因此不是 host concretization，
并自动继承 input/heap logical bounds、lifetime/init、pointer-union domain 与 CAS
solver semantics；zero length不解引用，symbolic/over-64 length fail closed。
F82 将 constant-length `memcpy`/`memmove`/`memset` declaration 与 LLVM intrinsic
展开为同一 core byte load/store IR。`memmove` 先完成 source snapshot，`memcpy`
只在所有有限候选区间可证明不相交时接受，`memset` 保留 symbolic low byte；zero
length 不触碰 memory，普通 store 路径继续维护 page-COW/init/lifetime contract。
F83 对每个 bounded indirect target 汇总 pointer return object，并把 indirect call
site 纳入 pointer formal actual summary；实际 target 的 return-domain certificate
通过普通 frame return绑定到 caller，validator 要求全 target pointer ABI完全一致。
F84 新增 guarded load relation `!guard OR access_defined`，并据此把 bounded
`strlen`/`strcmp`/`strncmp` 展开到 core IR。NUL/差异后的 offset 不再要求定义，
input logical tail、heap logical size与 short pointer-union arm仍由同一 domain证明。
F85 将 `memchr`/`strchr` 降为 first-match BV/select summary，并给 null 或每个有限
source object/offset结果恢复 proof-carrying pointer provenance。
F86 新增 guarded store relation `!guard OR access_defined` 和条件 initialization
marker；`strcpy` 以 source snapshot/first-NUL guards写入，`strncpy` 对 constant
bound执行 NUL padding，且所有 source/destination候选必须能证明属于不同对象。
F87 把 div/rem、symbolic shift、nuw/nsw/exact 的 LLVM definedness降为显式 core
assume；除零、signed division overflow、shift range、wrap与exact条件随 CAS solver
prefix迁移；F93随后为direct integer freeze undef/poison补充动态稳定choice。
F88 允许scalar global pointer initializer与exact-slot unique-dominating pointer
store/load；numeric bytes进入page-COW memory，loaded equality恢复finite data-object
provenance。F89 对 scalar function-pointer cell保存稳定 function ID并恢复typed
targets；F90 支持固定一维 global data/function pointer table与symbolic element
selection；F91 支持direct-predecessor merge，F94进一步以bounded backward
last-writer closure覆盖default+nested overwrite/forwarding CFG；F98再以bounded
IN/OUT fixed point覆盖loop-invariant与loop-carried full pointer writes。可能未
初始化、non-pointer/partial overwrite与escaped cell仍拒绝。F92 对call-site或所有有限
internal targets均可证明nounwind的invoke保留
normal PC并裁剪dead unwind subtree；真正exception object/unwinding仍拒绝。F93
以stable site和checkpoint-persisted动态counter为direct integer freeze undef/poison
建立独立BV choice；deferred poison lattice仍未实现。F95以core BV DAG支持
strict-ABI abs/hton/ntoh/LLVM bswap，INT_MIN与target endianness显式建模。
F96进一步以O(w) core DAG实现scalar ctpop/ctlz/cttz，并把zero-poison contract
接入solver definedness。
F97对single-use direct freeze的nuw/nsw add/sub/mul延迟definedness，并以
defined-result/stable-choice ITE恢复LLVM freeze语义；F110--F111将其扩展到
single-consumer cast/BV/icmp/ssa.copy与division/shift/exact，并用safe divisor
totalize未定义division；F112--F119继续覆盖path-sensitive select、PHI edge
definedness、exact straight-line store/load sidecar和constant-address GEP
等价证明、bounded unique-edge CFG corridor，并以defined-bit ABI覆盖唯一direct
direct callsites的integer return及argument双向传输。F121进一步组合双向ABI，
使formal argument经bounded透明integer slice返回时definedness不在callee断链。
F122以<=256访问的all-use proof覆盖全部消费者均到freeze的fan-out poison DAG，
同时拒绝mixed ordinary consumer和循环。
F123再把exact memory sidecar扩为<=64个线性同址loads，以最近store和exact
clobber界定memory version，并修复首个load后过早停止证明的问题。
F124以bounded forward DFS与all-predecessor reverse proof覆盖acyclic diamond
的共同store，并拒绝缺少共同runtime definition的路径。
F125为direct-predecessor full-store merge生成validator-checked edge definedness
PHI，使poison/defined reaching stores可随真实CFG路径组合。
F126将F117/F118的direct-call return/argument defined-bit双向接入该edge PHI。
F127允许incoming edge跨bounded forwarding subgraph；F128把exact entry/backedge
stores作为loop-header的动态definedness state update，并拒绝missing-store cycle。
F129将direct scalar global ConstantInt initializer作为defined entry state接入
path-dependent edge PHI。
F130进一步以target `DataLayout`和LLVM常量折叠证明constant-GEP aggregate
subobject的typed integer初值，并以`initial_subobject => initial` artifact不变量
约束该扩展。
F131将loop incoming source拆为`Store/Initial/Carry`，支持整条backedge无writer时
保持上一迭代definedness，并以validator-checked自引用edge assignment固化语义。
F132进一步识别direct-arm canonical store/carry diamond，在最终backedge生成
branch-conditioned i1 transfer select；forwarded形态随后由F133扩展。
F133沿bounded unique-predecessor arm ancestry寻找共同conditional split，覆盖
memory-free forwarded store/carry；同源multi-way随后由F134扩展。
F134按完整reaching-source identity分组并复检组内全部endpoints，覆盖同一writer
经无关控制流分裂形成的multi-arm join。
F135在一位definedness域合并多个经bounded source proof确认必定defined的writers，
同时保留numeric memory值与per-endpoint writer membership。
F136合并写入同一poison-capable SSA value的不同writers，并以完整writer-membership
集合保证每个store都完成sink proof与condition传播；不同producer仍拒绝。
F137以严格inner store split/outer carry split和eager-dominance proof覆盖两个
不同producer的固定深度2 condition tree。F138进一步以扁平proof IR、递归
common-branch partition和后序edge selects覆盖4--8个singleton sources、深度
3--6且唯一carry的condition tree，并要求全部predicate/value支配最终edge。F139
把完全同源且对每个选定branch均同臂的多个endpoints合并为一个递归leaf，同时复检
每个成员而非只检查代表endpoint。F140检测同一Store source在组内映射到同一
branch不同successors的位置冲突，并在总leaf预算内展开为引用同一condition的多个
结构leaf。F141对同一no-write Carry source执行相同的位置展开，并把实际
self-reference leaf count加入validator-checked artifact合同。
F142进一步允许同一header内多个静态不相交constant base+offset scalar cells各自
建立edge sidecar；reverse scan只越过DataLayout region proof确认不重叠的访问，
并以同block至少两个`multicell` contracts固化artifact范围。
F143把不同base证明扩展到lowerer可完整建模的identified static storage：
不同`GlobalVariable`或entry-block static `AllocaInst`。这类合同另带
`identified_objects`层级标记；validator要求其蕴含`multicell`且同block至少两项。
两个独立栈对象执行得到`1,1,2`，同aggregate与同cell对照组约束capability边界。
F144进一步覆盖不同constant-nonzero `malloc` sites。证明与现有bounded heap slot
pool的专用语义对齐，合同标记`fixed_heap_objects`并保持同block双marker不变量；
双heap正例得到`1,1,2`，static/aggregate对照组不被错误升级。
F145展开pointer select与acyclic PHI形成最多16个constant regions，并对两个域做
完整product disjointness proof；`{0,1}`对`{2,3}`正例得到`1,1,2`，
存在offset1交集的对照组保持infeasible且不声明capability。
F146保留select alternative的condition identity/polarity；空间重叠pair只有在
同一condition要求相反极性时才被证明不可行。互斥overlap正例得到`1,1,2`，
guards兼容的实际重叠负例保持infeasible。
F147为同一merge block的pointer PHIs记录incoming predecessor identity，不同edge
alternatives互斥、same-edge兼容；edge-exclusive overlap得到`1,1,2`，same-edge
实际重叠负例保持infeasible。
F148用LLVM ConstantRange把one-term symbolic GEP转为offset interval，并执行完整
region-product分离证明；两个subarray区间正例得到`1,1,2`，同区间负例拒绝。
F149把partial-byte memory definedness提升为显式address-order lane vector：
bounded reverse MemorySSA为每个lane选择最近writer，未写lane可由typed constant
global initial state补足，重叠窄写只替换实际覆盖的lane；所有可能poison的唯一
writer sidecars在宽load后AND。Production artifact逐lane复检store ID/宽度、
sidecar和组合数据流，split/initial/overlap正例与branch-merge负例均有执行证据。
F150进一步为direct predecessor merge逐edge重建lane vector，在synthetic edge
block中只合取该path的writer sidecars；path-dependent writers得到`1,2,2`，
store+initial edge组合得到`1,1,2,2`。Production validator复检每个endpoint的
完整lane覆盖、local reduction和merge jump；未覆盖lane需要穿越上游join时拒绝。
F151把循环header的aggregate bit提升为真正的per-lane fixed point。Entry/backedge
分别为每条lane写store sidecar、true或self-carry，header的dependency-checked AND
必须覆盖全部lane；函数级alias-closed memory-effect proof保证回边sink只绕过已证明
闭合的访问集。Partial poison overwrite正例得到`1,2`，额外aliasing load负例保持
infeasible，carry/capability tamper均被production validator拒绝。
F152覆盖一个strict conditional backedge diamond：write/carry arms在各自到join的
synthetic edge上更新或保留每条lane，避免skip路径读取未执行writer的sidecar。
Validator重建branch polarity、arm/edge/join拓扑、join-to-header all-carry path和
完整seed endpoint；symbolic write/skip得到`1,1,2`，alias-arm负例与polarity tamper
均拒绝。
F153把两臂扩展为最多16个unconditional unique-predecessor forwarding blocks，
writer proof覆盖完整corridor；validator从完整blocks图重建header/corridor/join
predecessor sets。Forwarded正例得到`1,1,2`，nested branch与伪造successor拒绝。
F154进一步接受严格两级三叶条件树：root/inner branches选择两个不同partial
writers或一个all-carry arm，三个edge分别执行完整lane transfer。Validator重建
两级branch tree、精确predecessor sets、两个不同store sidecars和一个carry
partition；low/high/carry正例得到`1,1,1,2,2`，四臂树与伪造route拒绝。
F155允许每条arm带最多16个unique-predecessor forwarding blocks，writer proof
覆盖完整corridor；validator逐jump重建三条互斥path并复检writer membership。
Forwarded正例保持`1,1,1,2,2`，nested/multi-predecessor corridor与伪造successor
拒绝。
F156将direct leaves推广为4--8叶、深度不超过6的严格二叉lane-source tree；
完整branch/leaf partition、唯一parent、实际depth、distinct writers与leaf edges
均由validator递归复检。四叶正例得到`1,1,1,1,2,2,2`，shared leaf与伪造depth
拒绝。
F157进一步允许所有branch-to-child edges经过最多16个unique-predecessor
forwarding blocks，包含decision corridors；validator逐jump恢复下一个声明node并
复检全树corridor disjointness。Forwarded四叶正例保持相同状态集，nested tail和
伪造leaf successor拒绝。
F158允许一个internal partial writer由其完整descendant leaf group共享；结构祖先
证明和validator重建的descendant集合必须与重复store ID引用集合精确相等。Grouped
四叶正例保持相同状态集，两个internal groups与伪造marker拒绝。
F159允许组内部分leaves以same-base/offset/width writer完全覆盖internal source，
其余至少两个位置重复使用上游source；validator复检override集合和lane positions。
五叶正例得到九状态，different-lane override与伪造marker拒绝。
F160进一步在单个leaf中逐lane组合结构祖先source、部分相交的更近override和carry；
compiler与validator均以`lane.store`及per-store/per-leaf lane sets为准。i32正例的
目标leaf为`internal, override, carry, carry`并得到九状态，完全遮蔽group与伪造
marker拒绝。
F161在F160 tree中显式支持2--8个pure all-carry leaves，artifact记录精确计数，
validator从lane vectors与unique-parent CFG独立重算。六叶正例得到十状态，共享
carry leaf和计数tamper拒绝。
F162允许两个direct disjoint internal writer groups，并分别证明完整descendant
partition与重复source引用集合相等。Low/high双组正例得到九状态，nested groups与
group-count tamper拒绝。
F163允许其中恰好一个group含一个per-lane partial override，同时另一组保持pure；
validator分别恢复两组完整lane sets并证明唯一local source差集。六叶正例得到十一
状态，full-shadow与composed-group-count tamper拒绝。
F164把F157的bounded corridor reconstruction与F158的single-group dominance
proof组合起来；decision与leaf edges均可含最多16个unique-predecessor jump
blocks，validator先恢复完整树和实际depth，再证明internal writer的完整descendant
集合等于重复store-ID引用集合。Forwarded四叶正例得到七状态，nested corridor、
forwarded marker与capability tamper拒绝。
F165进一步组合F157与F159，只对same-range exact override放开forwarding；
validator在完整corridor reconstruction之后重算pure group lane set与override
partition。五叶正例得到九状态，branching corridor、两个marker及capability
tamper均拒绝。
F166组合F157与F160，在corridor reconstruction后按store/leaf重建lane sets并
验证partial local source和非空internal差集；本阶段规范为一个pure carry leaf。
Forwarded composed正例得到九状态，branching corridor、双marker与capability
tamper拒绝。
F167组合F157与F162，在恢复tree后分别证明两个disjoint internal groups的完整
descendant/reference partitions。双组forwarded正例得到九状态，branching
corridor、marker/count和capability tamper拒绝。
F168进一步允许其中恰好一组含唯一partial override；validator分别恢复两组lane
sets并验证composed leaf的leaf-local source与非空差集。Forwarded mixed正例得到
十一状态，branching corridor、marker/count和capability tamper拒绝。
F169把F161的2--8 distinct carry leaves与forwarded composed tree组合；validator
从完整lane vectors重算精确count并复检unique-parent corridors。六叶正例得到十
状态，branching corridor、marker/count和capability tamper拒绝。
F170在8-leaf预算内接受三个direct pure groups，validator恢复三个完整partitions
并检查全部pairwise disjoint关系。七叶正例得到十三状态，nested group、
marker/count与capability tamper拒绝。
F171允许两个direct groups各有一个partial override，并以per-group `[1,1]`
计数避免两个local writers集中于同一组。七叶正例得到十三状态，`[2,0]`负例及
marker/count/capability tamper拒绝。
F172组合F157与F171，在恢复corridors后重做两个per-group source differences。
Forwarded七叶正例得到十三状态，branching corridor与marker/capability tamper
拒绝；tamper路径同时发现并修复了validator过早dispatch的未初始化变量错误。
F173组合F157与F170，在bounded unique-predecessor corridors后重建三个完整
partitions并执行全部pairwise disjoint检查。Forwarded七叶正例得到十三状态，
branching corridor与marker/count/capability tamper拒绝。
F174在8-leaf预算内允许三个direct groups中的一组含唯一partial override；
validator泛化为三组全对disjoint并证明leaf-local source与非空internal差集。
八叶正例得到十五状态，full-shadow负例及marker/count/capability tamper拒绝。
F175在F174上恢复bounded unique-predecessor corridors，再重做三组partition与
source difference。Forwarded八叶正例得到十五状态，branching corridor与
marker/count/capability tamper拒绝；当前8-leaf组合矩阵由此闭合。
Indirect/mixed transfer、
unbounded/Boolean-minimized condition DAG、PHI/path/SMT-correlated alias、
symbolic-offset alias、
超过8 leaves的多composition与general multi-cell
cyclic MemorySSA仍未完成。
F98以last-writer set和uninitialized bit的有限fixed point支持循环pointer-memory
versions，loaded numeric equality继续复检实际迭代版本。
F99以target DataLayout的base+modular-offset规范化constant GEP cell identity，
等价field地址共享memory version而不同offset保持隔离。
F120进一步把global pointer initializer作为reaching-definition entry state，
与runtime writers统一合流；symbolic GEP pointer通过finite concrete-address
alternatives跨pointer cell传递，data/function target discovery共享同一domain。
F100按LLVM concat/mod-width语义实现scalar bitreverse/fshl/fshr core DAG。
F101--F102以显式overflow/reversibility predicates和defined shift domain实现
scalar signed/unsigned saturating add/sub/shl。
F103实现LLVM abs flag与signed/unsigned min/max；F104保留expect hints的identity
runtime semantics且不污染path constraints。
F105以finite provenance实现fixed-object/null/union的LLVM objectsize查询。
F106进一步以runtime logical-size provenance实现input、malloc/calloc与symbolic
offset的dynamic objectsize，同时保持static runtime query为unknown。
F107支持六类scalar overflow aggregate与bounded extractvalue；F108保持integer/
data/function pointer `ssa.copy` identity/provenance；F109补齐bounded in-place
realloc success/failure后的dynamic objectsize，并按pointer alternative保留
realloc provenance，避免mixed select/PHI union共享错误请求长度。

已实现保守子集：

- `ContinuationFrame` 与 `LiveContinuationDescriptor` 定义
  `symcc-live-continuation-v1` canonical schema；
- descriptor 包含 PC frames、path-condition/store/memory content roots、parent
  checkpoint 和 target branch，digest 字段经 SHA-256 hex 校验；
- `checkpoint_id()` 对 canonical descriptor 求 SHA-256；
- `StateShardCoordinator.payload_from_item()` 可选接收 continuation descriptor，并把
  `continuation`/`continuation_id` 纳入 state-task id、shard owner 与 lease/recovery；
- `LiveStateStore` 以 canonical JSON SHA-256 CAS 保存 expression、parent-linked
  solver frames、symbolic store、concrete/symbolic memory pages 和 memory roots；
- `fork_memory()` 只复制发生写入的 fixed-size pages，未修改 page digest 在父子
  checkpoint 间共享；restore 全程校验 digest、schema、引用、深度和 cycle；
- `symcc_live_state.py inspect|memory-diff` 提供 portable checkpoint 审计，并明确
  标记 `native_resume_supported=false`；
- `LiveContinuationExecutor` 执行 const/input/input_size/BV expression、load/store、
  heap_alloc/heap_realloc/heap_free、assume/branch、jump/call/return/halt；symbolic branch提交
  两个 path-frame child；
- legacy/new-PM `live-continuation-export` 对入口可达 integer SSA、select、switch、
  direct call 和 PHI edge semantics 做 CPS-like lowering；PHI 采用 two-phase edge
  copy 保持 parallel assignment；
- exporter 对未支持的 pointer 形式、vector/FP、non-promotable/dynamic memory、
  exception、indirect/external/variadic call、recursion、poison-generating flag、
  division 和 symbolic shift 输出结构化 rejection report，不发布部分 executable
  artifact；
- executor 将 expression DAG 与 parent-linked path frames 降为 QF_BV，UNSAT
  branch/assume 被剪枝，solver unknown/unavailable 保守保留并计入 telemetry；
- F69 按 DataLayout 序列化 static-global initializer，artifact 保存
  endianness/memory image/object bounds；multi-byte symbolic store 拆成 byte roots，
  load 重新组合为 BV，继续复用 page-COW；
- compiler 与 executor 双重拒绝 null/OOB/read-only access；dynamic GEP 和
  symbolic runtime address 不使用 concrete witness 猜测；
- F70 新增 `symcc-live-input-buffer-v1` contract 和 `input_size` instruction；
  checkpoint creation 将 seed expressions 写入 input object 的 page-COW bytes，
  并以 CAS `@input:length` root 保存实际长度；
- input capacity 受 input/memory limit 和 size 参数位宽共同限制；runtime 按
  `min(capacity,input-length)` 复检访问，因此短 seed 不能读取 backing zero suffix；
- F71 lowering fixed nonzero alloca/constant GEP，并要求每个 stack load 有覆盖完整
  字节区间的 dominating store；runtime 只允许 top owner frame 访问并逐字节检查
  `@stack:init:<depth>:<address>`；
- stack markers 和 SSA locals 随 checkpoint 持久化，在 return 时按 depth 清理；
- F72/F75 lowering direct `malloc(constant nonzero size)` 与 base/null `free`，
  并以 `kind=heap,site,slot,capacity,lifetime=runtime-alloc-free,
  allocation=bounded-pool-infallible` 记录固定容量对象 pool contract；
- heap allocation/store 分别持久化 `@heap:live:<base>` 和逐字节
  `@heap:init:<address>`；load 复检 liveness/initialization，free 清理 markers，
  因此同 site 释放后重分配不会读取旧 backing bytes；
- validator 要求每个 heap site 恰有完整的 `[0,capacity)` slot 集合、统一大小、
  slot-ordered allocation address list和唯一 allocation instruction，并对含
  stack/heap object 的 recursive call graph fail closed；
- F73 新增 `pointer_offset` 与 per-memory-op alias contract；compiler 按 signed
  index/scale/object/access width 枚举最多 256 个 in-object addresses，并输出
  signed alias-index interval；超限或 pointer/index-width mismatch 拒绝；
- executor 将 symbolic load 编码为 finite ITE，将 symbolic store 编码为 guarded
  per-byte read-over-write，并把 address disjunction 与 no-wrap index domain
  持久化到 solver frame，阻止 BV wrap 重新接纳 `inbounds` poison；
- stack/heap init markers 可保存 conditional 1-bit expression，候选区间重叠的
  multi-byte store 也按所有 address guards 组合；
- F74 新增 `bounded-pointer-union` 与 `alias_cases` contract；select 条件或 PHI
  discriminator、case-local no-wrap index 和 address equality 共同进入 solver
  frame，不同 case 可属于不同 object，整个 union 仍受同一 alias limit；
- executor 对每个 case 独立复检 input length、top stack frame、heap liveness 和
  read-only 属性；null/read-only undefined arm 不被 concrete witness 补全；
- F75 的 `heap_alloc` 选择第一个非 live stable slot；每个 malloc SSA 的 slot
  alternative 以 `result==slot_base` guard 进入 `alias_cases`。pool exhaustion
  显式报告模型边界，free 后仍可 deterministic reuse 且不会恢复旧 init markers；
- GEP runtime address 从真实 base SSA 计算，而 provenance 对每个 alternative
  独立应用 constant/one-term offset，支持 GEP-after-union；
- F76 为 pointer formal 与 pointer result 输出有限 object/address/offset alternatives，
  并保留原有 numeric SSA call/return relation；global/input/heap/caller-stack
  identity 可跨 direct call checkpoint/replay；
- validator 对 pointer signature、call arity、argument index、return width 和
  destination 做 exact matching；runtime 按 object owner function 定位唯一 active
  stack depth，使 callee 可以读写 caller stack，同时拒绝 callee stack pointer
  return；
- F79 为 pointer formal 追加 `__ptr_domain_<index>`，call/function artifact 以
  `pointer_domains` 映射其位置；动态 actual 的 signed index domain 作为 expression
  传输。Pointer return 以 `pointer_return_domain`、`pointer_domain` 和
  `pointer_domain_dst` 保持证明链，callee alias guards 必须重新证明 domain 为真；
- F80 为 address-taken internal target 输出 stable `function_id`，将 function
  pointer SSA 降为 BV selector；`indirect_call` 对每个 target 保存 ID 与
  select/PHI guards，executor 按可行 target fork。Typed parameter/return widths、
  target ID、间接 call graph 和 recursion 均由 artifact validator 复检；
- F81 以普通 core continuation instructions 实现 bounded memcmp/bcmp read-only
  effect summary；first-difference ordering、zero-length no-dereference 与
  64-byte expression bound 明确进入 compiler/test contract，不增加 opaque runtime
  primitive；
- F82 以普通 core load/store 实现 bounded memcpy/memmove/memset write-effect
  summary；source snapshot、finite candidate disjointness proof、symbolic low-byte
  fill、LLVM intrinsic/C declaration 双入口与 zero-length contract 均有定向测试；
- F83 将 finite target points-to 与 pointer return provenance 做有界乘积汇总；
  indirect pointer actual、selected-target domain return、typed target equality 与
  tamper rejection沿用普通 call/return可信边界；
- F84 以 guarded load定义域实现 bounded strlen/strcmp/strncmp short circuit；
  first-NUL/first-difference、64-byte bound、n=0 no-access、logical-input tail和
  malformed guard均有定向证据；
- F85 以 bounded core IR实现 memchr/strchr first-match search，并为每个有限
  source offset恢复 pointer result provenance；
- F86 以 guarded store、条件初始化 marker和 source snapshot实现
  strcpy/strncpy；terminator、padding、destination return provenance、zero/null、
  overlap/symbolic length拒绝均有定向证据；
- F87 以 QF_BV assumptions实现 div/rem/shift definedness与 nuw/nsw/exact
  proof；constant UB/poison路径不可行，symbolic defined paths保留，operator/
  assume-width artifact contract由 validator/runtime复检；
- F88 以 numeric memory加 finite provenance sidecar实现 bounded pointer-valued
  global/stack/heap cell；loaded pointer可继续GEP/access；
- F89--F91 将sidecar扩展到scalar function-pointer cell、固定一维global pointer
  table和direct-predecessor-complete data/function memory merge；所有候选均以loaded
  address/function-ID equality复检，loop/multi-level reaching definitions fail closed；
- F92 对proven-nounwind direct/indirect invoke导出显式normal target并保留PHI edge
  copy；validator复检target存在性，may-unwind invoke fail closed；
- F93 的typed nondet core为direct integer freeze undef/poison建立动态稳定choice；
  loop实例、checkpoint counter和one-shot/incremental SMT declaration均可复检；
- F94 对exact pointer slot实现4096-node/256-store有界无环reaching-definition
  closure；nested/default merge与function targets共享证明，cycle/missing fail closed；
- F95 将abs family、network byte order与LLVM bswap降为host-independent core BV
  summary；wrong ABI、locale/errno/IO等外部状态继续拒绝；
- F96 将scalar ctpop/ctlz/cttz降为bounded core BV summary；zero semantics与
  `is_zero_poison`定义域由自动化测试复检；
- F97、F110--F175 对bounded integer poison图实现deferred
  freeze；cast/BV/icmp/ssa.copy/division/shift/exact/select/acyclic PHI与
  straight-line exact/canonical-address memory、unique-edge cross-block corridor
  与bounded multi-direct-call return/argument、argument-to-return组合及
  all-use multi-consumer freeze graph、exact multi-load与acyclic common-store
  branch memory version、direct-predecessor path-dependent definedness PHI
  及其direct-call argument/return、multilevel forwarding和exact-store loop组合
  与global scalar/constant-offset aggregate subobject initial state及纯no-write
  loop carry与direct/forwarded/equivalent-source multi-arm conditional
  store/carry transfer，以及bounded acyclic byte-lane nearest-writer
  composition/direct-edge lane PHI、alias-closed cyclic fixed point与strict
  direct/forwarded/two-level-multiarm/forwarded-multiarm/recursive/
  forwarded-recursive/grouped/repeated-source-recursive conditional lane
  carry已覆盖，
  general alias-aware memory join、
  indirect/mixed transfer、symbolic-alias/conditional lane memory继续使用
  defined-only under-approximation；
- F98 对pointer cell实现bounded cyclic reaching-definition fixed point；
  loop-invariant/loop-carried writer可执行，未初始化路径继续拒绝；
- F99 对constant GEP pointer cell实现target-aware canonical identity；
  equivalent/different offset均有正反自动化证据；
- F120 对pointer cell合并global initial definition与runtime reaching stores，
  并保留symbolic GEP pointer的完整dynamic identity/concrete address domain；
- F100 将scalar bitreverse/funnel shift降为bounded core BV permutation；
  modulo-width和zero-shift边界有自动化证据；
- F101--F102 将scalar signed/unsigned saturation arithmetic降为core BV；
  extrema、symbolic clamp与shift poison均有证据；
- F103--F104 覆盖scalar abs/min/max与expect identity；INT_MIN poison和hint
  non-assumption均有自动化证据；
- F105--F106、F109 支持fixed与bounded runtime-sized pointer objectsize；
  dynamic logical-size与capacity严格区分，bounded in-place realloc已覆盖，
  moving/custom external allocator仍未覆盖；
- F77 的 helper server 对 exact prefix 做 push/pop，对 parent hit 只解析 frame
  delta，对 cold miss 解析完整 prefix；prefix/delta/target SMT fragment、CAS frame
  metadata 和 context 数量均有界，execution result 分别报告 exact/parent/rebuild/
  fallback counts；
- F78 的 nullable pool 以 slot-ordered ITE 合成 pointer 或 null；所有 heap alias
  case 都加入 runtime live/logical-size condition。Calloc 的因子、zero contract 和
  realloc 的 base/strategy/width 由 artifact validator 独立复检；
- `llvm_to_continuation.py`、`symcc_live_state.py run-llvm` 和
  `SYMCC_LIVE_LLVM` 将 `.ll/.bc` 或 C/C++ source 接到单机/MPI state path；
- `SYMCC_LIVE_PROGRAM` 让 MPI master 建 initial checkpoint，worker 按 step/state
  lease budget resume，master 复检并重新排队 frontier；
- `test_distributed_state.py` 覆盖 checkpoint identity、page-COW sharing、完整
  restore、pause/fork/call/CLI、tamper 和 missing-object rejection。

下一阶段需扩展独立 `live-state` engine mode：

- continuation frame：PC、SSA/register bindings、call stack、path condition root；
- moving/custom allocator、heap pointer escape、heap graph quotient 与
  path-optimal state merging；
- dynamic/recursive stack、callee-stack escape 与更一般的 path-sensitive
  initialization；
- multi-term alias、超出有限global table/direct-predecessor merge的上下文敏感
  general points-to，或超出 finite bound 时的可信 array-theory memory；
- external resource、TLS、signal 和 syscall state virtualization；
- 更广的 guarded string/search/copy summaries、exception/general indirect call、完整
  poison/undef/freeze/UB 与可信 native adapter；
- backend-neutral incremental protocol、跨 worker context/clause transport 与
  unsat-core-guided state subsumption；
- locality-aware owner、bounded stealing、failure recovery；
- 与现有 concolic seed-replay mode 并存，不能混用命名。

这是一项 XL 级架构工作，但也是项目从“并行 concolic 编排”升级为“并行符号执行
框架”的必要条件。

### G06. 真实 solver portfolio 和论文级 SMTgazer

**状态：`P/I/T/E`（异步query-service portfolio、disagreement telemetry、
bounded parallel racing、proof-carrying QF_BV lowering/capability matrix、cvc5
实机模型双验证、prefix-keyed persistent context、真实X-means/BIC与删失PAR-2
proof-carrying offline prior、bagged/boosted CART cost surrogate与可执行budgeted
sequence beam optimizer、schedule-level EI/SMBO acquisition已实现；
  SAT-gated running-attempt cancellation、persistent cold recovery及Query IR
  structural feature schema v2与v1 artifact/state迁移已实现；
  固定Bitwuzla 0.9.1的38-operator/model/UNSAT trust实机证据与可重放artifact已实现；
  sealed Query IR train/holdout、三solver identity gate、paired PAR-2/child-CPU/
  complementarity artifact与离线verifier已实现；
  跨worker clause transport仍未实现）。**

SMTgazer 调度的是互补 SMT solver/algorithm sequence，使用 X-means 对实例聚类，
再以 dataset-level Bayesian optimization 和 boosting/bagging surrogate 优化序列。
此前模块只有 Z3 exact、fast、optimistic、poly 等运行配置，并用在线 centroid split
和 Thompson-style arm sampling 选择配置。本轮在 G01 query service 边界新增：

- `PortfolioSolver` 对同一 `WorkLease` 运行多个 backend/helper；
- result schema `symcc-solver-portfolio-v1` 记录所有 attempt、winner 和 disagreement；
- SAT/UNSAT 冲突保守返回 `unknown`，不进入 proof/model cache；
- `SYMCC_QUERY_SOLVER_PORTFOLIO`/`--portfolio` 支持 persistent 或 one-shot helper；
- `SYMCC_QUERY_SOLVER_PORTFOLIO_PARALLELISM`/`--portfolio-parallelism` 控制同一 query
  内的并发 attempt，portfolio result 记录 mode、parallelism 和 wall-clock elapsed；
- query-store stats 记录 portfolio results 与 disagreements。
- F237新增`kind=smtlib-qfbv`：从CAS Query IR独立lower全部38类当前QF_BV operator，
  严格验证sort/width/arity/root与资源上限，不复用Z3 printer；
- `symcc-qfbv-capability-v1`显式声明operator、width/node/input、model和UNSAT能力；
  `symcc-qfbv-lowering-v1`绑定operator计数、input集合、SMT-LIB及capability摘要；
- SAT的全部input-byte `get-value`先由adapter Query IR复核，再由QueryStore从CAS二次
  复核；未授权UNSAT默认降为unknown，证书/模型篡改不能进入缓存；
- F244固定官方Bitwuzla 0.9.1 commit并将38-operator matrix提升为统一生产工具；
  F245接入的Z3 4.8.12以及cvc5 1.1.2、Bitwuzla均通过SAT模型双验证、未授权UNSAT
  降级和授权UNSAT接受。
  Versioned artifact绑定command/version/binary/capability/lowering/result，离线
  verifier重建Query IR与证书，replay以排除elapsed/path的semantic digest对拍。
- F238为`incremental=true` backend建立每prefix独立interactive process，永久assert
  prefix并对target执行push/check/get-value/pop；1--64 LRU、echo framing、timeout
  kill/cold recovery、prefix digest复检与context telemetry均有真实/伪backend测试。
- F239新增确定性局部two-means refinement和spherical-Gaussian BIC split selection，
  对显式timeout/kill收取PAR-2 `2T`，按context/action计算bounded IPS、ESS、
  empirical-Bayes shrinkage与LCB；versioned artifact封存split/estimate充分统计，
  独立复算通过后才作为保留10%探索的online preference。轨迹、scheduler state和
  benchmark均记录artifact/cluster/recommendation provenance。
- F240为completion、stage-budget-aware censored PAR-2 ratio与adjusted reward训练
  inverse-propensity-weighted shallow CART bagging和residual boosting；以uncertainty
  LCB/UCB执行共享timeout内的global/per-cluster bounded beam search。Verified
  artifact在worker中真正控制整数秒stage timeout，跨replay推进action schedule，
  并记录input identity、solved和stage budget供后续paired/DR估计。
- F241在相同sealed F240 objective上实现schedule-level有界SMBO：固定设计覆盖全部
  single-action/budget基线，bagged/boosted shallow-tree代理以解析Expected
  Improvement逐点选择未观测schedule，严格限制selection evaluation。完整候选池
  oracle只在选择结束后隔离计算并单列调用数；artifact封存并重放global/per-cluster
  acquisition trace，在线执行保留90/10探索和真实stage timeout。
- F242新增opt-in bounded consensus grace：只有SAT触发queued/running attempt
  cancellation，UNSAT继续全收集；JSON/QF_BV one-shot和persistent helper均支持
  process-group interrupt，persistent状态被丢弃后cold rebuild。Telemetry区分
  cancel request、实际取消和incomplete consensus，默认`-1`保持原语义。
- F243把成功导出的Query IR node/input/width及四组operator histogram从QSYM
  runtime送入telemetry、trajectory和online scheduler；16维features-v2按query
  平均规模并使用operator/node比例。F239 BIC、F240 tree和F241 SMBO验证器从sealed
  artifact自身推导9/16维；旧v1 artifact原摘要可验证，旧moment state零扩展迁移。
- F245新增`qf_bv_campaign.py`：完整Query IR corpus按content ID hash切分train/
  holdout，campaign由F244 exact binary identity授权，按随机化paired task执行
  Z3/cvc5/Bitwuzla并测量child CPU/PAR-2。Verifier重建CAS/lowering/SAT witness和
  所有pair aggregate；confirmatory模式要求外部corpus、disjoint split和至少20次。
  当前checked evidence是single-repeat synthetic smoke，不是性能结论。
- F246新增`qf_bv_strategy_campaign.py`：train event必须绑定F245 train query ID，
  verifier从事件完整重训F240/F241并复算每个holdout的F243 vector/cluster/schedule。
  同一manifest执行三单solver、beam、SMBO和F242 cancellation六臂，保留所有内部
  attempt并复核SAT/UNSAT、winner、grace、child CPU、PAR-2和配对aggregate。
  Checked synthetic证据为24/24 solved及6个真实取消，但仍不是性能结论。
- F247新增`qf_bv_coverage_join.py`：从F246 verified SAT model重新materialize
  candidate，与原witness在sealed AFL target上做paired edge/data replay，报告六臂
  union gain、pair complementarity、coverage/solver child CPU以及F243 v1/v2
  Brier/log-loss/ECE。Verifier独立重算input、bitmap关系、novelty、aggregate和
  calibrator；checked 5-input/24-row synthetic artifact真实semantic replay一致。

仍需实现：

- 更强的solver consensus或checkable UNSAT证据；F244 conformance验证trust gate，
  但没有生成proof-producing UNSAT；
- native multi-context/clause sharing、跨worker context transport、per-stage solver
  timeout；F242已有helper interrupt，但尚无native solver API级cancel；
- 在真实公开holdout上对9维v1和16维v2做paired calibration/PAR-2/coverage消融。
  F247已有完整join/calibration机制，但checked input仍是synthetic smoke；需据公开
  结果决定是否增加operator细粒度、DAG depth/topology或bit-blast proxy；
- F246/F247已经提供F240 beam、F241 EI/SMBO与F242 parallel的同manifest执行和
  coverage/实测solver CPU归一化；仍需外层CPU quota、optimizer CPU、simple regret
  及公开target的20次coverage AUC/CPU-hour对拍；
- equal-CPU Z3/cvc5/Bitwuzla corpus、holdout transfer和online confirmatory实验。
  F245--F247已提供equal-task/shared-timeout、实测CPU、组合arm及authoritative AFL
  replay，但尚未运行公开真实corpus的20次confirmatory campaign；并行CPU也尚未由
  外层配额等量化；

### G07. PSCache 式 solver 内部部分解复用

**状态：`P/I/T/E`（QueryStore partial assignment reuse、persistent recent-SAT
probe、proof-carrying Z3 assumption-conflict extraction、bounded core minimization
与signed prefix/off-path relevance index已实现并测试；逐次CDCL/bit-blast trail
conflict hook未实现）。**

FSE 2024 的 PSCache 从 bit-blast/CDCL 求解过程中采集导致 conflict 的中间赋值，
重建为 bit-vector partial solutions，并把它们挂到可能在后续 path condition 中
出现的 prefix/off-path 子约束。它不是“只按分支表达式缓存”，也不等同于当前完整
model cache、poly cache 或 UNSAT core cache。

旧文档以“路径前缀相关、分支缓存不安全”为由拒绝 PSCache，这个理由只反驳了错误
的 branch-only cache，没有反驳论文算法。正确实现仍按完整 clause set 做
subset/superset 检查，并验证 cached assignment 是否满足新 query。

已实现：

- `util/query_store.py` 在 SAT completion 中持久化 primary model 和
  solver-verified generator model 的 byte assignment；
- 新增 `symcc-partial-solution-v1`、`partial_solutions`、
  `partial_solution_clauses` 与 `partial_solution_candidates`；
- 新 query ingest 和 SAT completion 后的 pending-query rescan 会查找 partial
  assignment；
- lookup 按当前 query 与 source clause 的 overlap 对 partial solutions 排序；
- bounded Query IR evaluator 验证所有 prefix roots 和 target root 后才写
  `async-partial-*` candidate；
- partial hit 不会把 query 标记为 SAT，也不会写 UNSAT proof；candidate 仍需 concrete
  replay 和 AFL novelty triage；
- `test_query_store.py` 覆盖正向复用和不满足 target 时拒绝复用。
- `symcc-query-solver --server` 维护进程内 recent SAT assignment cache，在正式 Z3
  check 前用短超时把 cached input-byte assignments 断言进当前 prefix+target context；
  只有当前 query 在该 assignment 下经 Z3 返回 SAT，才以 `z3-pscache` 返回；
- helper result 暴露 `solver_pscache_hit/probes`，QueryStore stats 记录
  `solver_pscache_hits`，`query_solver_pscache.py` 覆盖真实 persistent helper 命中。
- F236从当前concrete witness和失败cache probe重建numeric input-byte equality，
  使用公开Z3 assumption API检查完整prefix+target；只有UNSAT且返回非空assumption
  core时才生成版本化conflict solution；
- 对core做有界deletion minimization和最终UNSAT复核。固有UNSAT公式缩到空core后
  拒绝记录；unknown或预算耗尽只降低`core_minimal`，不会伪造最小性；
- QueryStore严格检查core subset、canonical offset、proof类型并生成证书摘要，再用
  独立Query IR evaluator证明assignment确实与源query冲突；
- 新增signed `query_literals/partial_solution_literals`倒排索引；连续`lnot`
  规范为base expression与polarity，使实际满足的prefix/off-path literal进入lookup
  relevance排序；目标query仍必须完整Query IR通过才生成candidate；
- 冲突数量、checks、core/minimality、literal links和provenance已进入result、SQLite、
  candidate manifest和stats；真实系统Z3回归覆盖最小core、SAT hit和固有UNSAT拒绝。

仍需实现：

- 若实验证明assumption-conflict采样不足，在版本锁定的自建Z3 CDCL/bit-blast边界
  暴露受预算约束的每冲突`curModel`投影，并与F236做同查询对拍；
- 对该可选低层后端实现SAT variable到input bit的映射证书；当前F236直接从公开SMT
  input-byte AST重建，不声称完成私有bit-blast反向映射；
- 将采集数量、lookup 开销、partial-hit candidate validity 纳入 ParaSuit 自配置。

### G08. Lase 式在线 token grammar synthesis

**状态：`P/I/T/E`（comparison-taint span、在线production、variable-length
completion、parser-state structural path、bounded direct/multi-slot/mutual CFG、
packed alternatives、shared-DAG projection、deep ECT、nullable SCC fixed point、
atomic multi-nonterminal substitution、proof-carrying epsilon derivation、独立
grammar bitmap、parser-neutral incremental cache protocol、accepted-forest PCFG
posterior/inside-outside、pairwise relation-aware slots与一级parent-conditioned
hierarchical PCFG、update-before prequential calibration与drift fallback已实现；
fixed-16 recency/stale/recovery gate、bounded adaptive-window cut certificate、
grandparent multi-context circuit、ordered-sibling与bounded second-order
sibling-history autoregressive factor、persistent native Tree-sitter incremental
parser与node-ID reuse proof、sealed parser cost/reuse telemetry已实现；
native GLR/GLL complete-forest adapter、
arbitrary-distance/cross-parent long-range probabilistic factors与confirmatory
calibration待完成）。**

OOPSLA 2026 的 Lase 使用 input-grammar-oriented search 和 token-level online grammar
synthesis。当前 `grimoire_gen.py` 提供 grammar-free recombination，ECT 也只保存
branch/taint summary；二者都没有在线推导产生式、token 边界和语法规则覆盖。

已实现保守子集：

- `semantic_proposals.py` 从 AFL extras/string tokens 与历史 comparison-core fragment
  合成 `literal/delimited/key_value/segment` token grammar rules；
- `SemanticProposalGenerator` 在 inverse/surrogate 后生成 kind=`solve_complete` 的
  grammar completion candidate，并经 `VerifiedProposalManager`、target replay 和 AFL
  novelty triage 过滤；
- `.semantic_proposal_generator.json` 保存 bounded grammar fragments；
- `test_semantic_proposals.py` 覆盖 rule synthesis 和 quote-delimited completion。
- F187从comparison taint推断bounded lexical spans，持久学习stable
  literal/choice/sequence/optional/repetition productions；
- grammar proposal可splice变长/变短输入，stable rule ID贯穿proposal manager；
  target replay与AFL retention分别更新validity和rule-coverage yield并参与排序；
- v2 artifact记录attempted/validated/verified/retained规则漏斗与显式成本预算。
- F201接受versioned parser nonterminal/state span tree，验证replacement span后以
  parser implementation和ancestor structural path形成rule-scoped context alias；
  malformed trace和exit/verdict mismatch fail closed，重启保持局部冲突。
- F202把AFL-retained feature delta按rule/structural-context持久化，并以递减novelty
  bonus参与每span排序；无verified证据的retention回灌被拒绝。
- F203把可信parser树归一化为stable LHS/RHS/terminal-gap production ID，从唯一
  direct self-recursive child归纳一次有界`prefix + Nonterminal + suffix`展开；另以
  64K稀疏饱和bitmap独立记录rule/path/production exercise和hash collision，不再把
  AFL feature delta称为grammar coverage。
- F204沿replacement ancestry形成canonical CFG fragment：保存全部direct recursive
  slots，并从重复nonterminal提取mutual cycle path；semantic层复算ID后按depth
  1--N做有界derivation，持久化一对多rule/cycle provenance。
- F205把production shape与concrete subtree instance分离，保存bounded derivation
  path/yield/gaps；parser接受的同形instance形成增量ECT correspondence，并只在已认证
  shape context内执行cross-candidate substitution。状态重启、内部tamper与64 KiB
  证据降级均有自动化门禁。
- F206以七维non-dominated fronts/crowding替代grammar/ECT rule固定加权和，显式联合
  target、AFL edge、data、concrete-verified String、grammar、ECT和age目标；256-arm
  极值archive、neutral prior、持久age与scalar消融已落地。
- F207增加六维epsilon-Pareto seed metadata archive，按edge/data/String/structure/
  path/cost执行strict dominance与extreme-protected density replacement；仅影响SymCC
  replay priority，不删除AFL权威queue。
- F208增加parser trace v2的显式零宽节点和最多8路packed child alternatives。主树只
  沿alternative 0定位；v3 fragment在32-production/32-instance/64 KiB预算内保存
  canonical family。只有empty RHS/gap/yield全部复检且context shape匹配时才学习
  可执行epsilon删除规则，proposal/semantic v6/v11重启保持引用一致。
- F209增加无single-parent限制的v3 forward DAG trace与v4 graph certificate。
  Alternative 0仍形成单父selected tree；32-node/128-edge bounded closure为非主
  深层节点生成node-path-bound instance。共享edge、selected path与全部identity在
  semantic层二次复检，v7/v12状态和级联驱逐保持引用一致。
- F210增加v4 grammar-level nullable dependency rules和v5 proof certificate。SCC
  identity与least fixed point分离：空RHS提供depth-0基例，只有全部children已证明
  才传播，故纯递归SCC保持non-nullable。Semantic v13按parser重算全局certificate，
  只有selected context认证的nullable production shape才能授权epsilon删除；proof
  tamper、wrong context、restart和instance eviction均有自动化门禁。
- F211从已复检parent/child graph派生原子multi-nonterminal transaction。Parent
  source yield必须由terminal gaps和全部primary child instances精确重建；2--4槽在
  一个splice内改变，source bytes与parent shape/context共同门控。Transaction在v14
  状态中仅作审计输出，重启/驱逐从可信graph重算；proposal v9记录provenance digest。
- F212增加`{cache}` parser-neutral manifest、content-addressed accepted base trace、
  LCP/LCS edit与16-byte guard invalidation。Parser仍输出完整trace；可选reuse receipt
  逐node复检symbol/state/span/yield，缺receipt退化full parse，假receipt拒绝，坏entry
  自动cold fallback。Proposal v10持久化cache漏斗。
- F213只从已接受fragment的root-0/alternative-0 concrete tree更新
  `(parser,LHS,state)` production-shape count，以`alpha=0.5` Dirichlet平滑未观察
  shape；fragment digest饱和去重。全局packed hyperedge按node/production/children
  而非parser局部ordinal规范化，显式root membership支持内容寻址forest union，并在
  forward DAG上重算inside/outside。Posterior likelihood和information/reachability
  把F206扩展为九维Pareto，但只排序、不剪枝；v15状态、篡改、迁移和驱逐有回归。
- F214只从可逐byte重建的accepted alternative-0 parent学习slot-pair关系；至少两个
  观测且零当前反例，按bytes equality、decimal equality、value/length和length
  equality顺序保留最强关系。每个parent前三个transaction按relation一致性优先，并
  显式保留一个不一致exploration reserve；关系只调度、不剪枝。Transaction v2绑定
  relation witness，semantic v16绑定instance本地完整child-edge sequence；关系、
  transaction和迁移edge provenance均从已复检graph重算，篡改与新反例有回归。
- F215把F213全局posterior作为`beta=2`先验，为
  `(parser,child family,parent shape,slot)`学习条件count。共享node的推断状态扩展为
  `(node,context)`并执行bounded inside/outside；条件mass进入原九维Pareto但仍只排序。
  Accepted tree计数改为完整edge/instance闭包的原子事务；v17状态对count与fragment
  selections做固定点对账，v16迁移无证据时严格回退全局posterior。4:4全局与两个
  3:1/1:3 parent fixture得到0.5、2/3、1/3，并覆盖篡改和幂等恢复。
- F216在count更新前为每个selected node保存global/context sufficient-stat receipt，
  以prequential NLL相对全局的累计gain和样本量形成`[0,1]`权重；context forest使用
  global/raw posterior凸组合。负gain或drift使weight归零，v17无receipt迁移也严格
  使用global。V18只让校准receipt影响调度；篡改不会撤销grammar counts。40-event
  integration、确定性重启和正gain后反向drift回退均有回归。
- F217为receipt v2增加accepted-fragment index，实际weight改看最近16个全局fragment
  内的prequential regret；无近期event标记stale并回退global，recent adverse关闭，
  后续positive evidence可恢复。窗口由PCFG observation stream推进，receipt饱和也不
  冻结时间。V19验证fragment-index双射并兼容v18无序累计模式；四阶段状态有回归。
- F218将同context同fragment的receipts聚合为独立检测样本，在最近最多64个fragment
  上对裁剪到`[-4,4]` bits的log-loss gain执行最小8+8的Hoeffding切分。每轮对全部
  切点做`delta/m` union allocation，显著cut撤销旧prefix；实际gate使用保留后缀上
  未裁剪的每fragment平均NLL。V20输出绑定证据摘要、切点、均值、epsilon和置信参数
  的canonical certificate，恢复时从receipt重算。平稳、drift、二次recovery及证书
  篡改均有回归；当前只声称per-scan bound，不声称anytime-valid ADWIN保证。
- F219增加`(grandparent shape/slot,parent shape/slot,child family)`二级context，
  posterior以beta-2继续向parent raw收缩。Update-before receipt同时绑定global、
  parent和circuit充分统计，robust gain取相对两个baseline收益的较小值；只有提供
  parent之外的新信息才启用。Context-state v2在shared DAG上保留两级path，推断使用
  归一化凸mixture与sum/product mass。V21固定点复检count/fragment/receipt，circuit
  adaptive cut使用独立certificate。真实fixture在parent 16:16下学到grandparent
  14:2与2:14，得到5/6、1/6及双向有效posterior。
- F220增加`(parent shape,current slot,left sibling selected shape,child family)`
  一阶有序兄弟context，raw posterior以beta-2收缩到最细parent/circuit先验。
  Update-before receipt同时绑定global、parent、optional circuit与sibling四层统计，
  robust gain取相对前三个baseline收益的最小值；独立sibling cut可从canonical
  receipt复算。Context-state v3以shape-indexed forward/backward message替代父内
  child independence product，并保存artifact级外部系数。V22严格复检左右RHS路径、
  count/witness固定点与共享index双射。真实fixture保持global/parent/circuit 16:16，
  仅由左tag内部shape得到sibling 14:2与2:14、posterior 5/6与1/6。
- F223在不改变F218响应性cut的前提下增加独立anytime审计层。每个context-local
  fragment启动forward Hoeffding confidence sequence，launch ordinal与序列内look
  分别按`6/(pi^2 n^2)`分配0.05预算并取running interval intersection。Bounded
  constant-conditional-mean零假设下，对所有launch和look的预算和不超过0.05，
  因而单个context在无限扫描与可选停止下的首次certificate误报概率受控。Parent、
  circuit和sibling分别从canonical robust-gain receipts重建证书；v23不信任持久化
  派生物，v22缺少新字段时只从已验证receipt迁移。该保证不跨context联合，也不把
  自适应scheduler下的非平稳流假设成null。参考
  [confidence sequences](https://arxiv.org/abs/1810.08240)、
  [repeated forward-CS detector](https://arxiv.org/abs/2309.09111)和
  [e-detectors](https://arxiv.org/abs/2203.03532)。
- F224增加有界二阶兄弟history context：
  `(parent shape,slot,shape[i-2],shape[i-1],child family)`。Raw posterior以F220
  immediate-sibling raw posterior为beta-2先验；update-before receipt保存global、
  parent、circuit、sibling、history五层统计，robust gain必须胜过全部四个baseline。
  Context-state v4用最近两个shape的tuple执行精确forward/backward factor message，
  responsive cut与F223 anytime audit均有独立history schema和canonical verifier。
  V24固定点复检前二/前一/current RHS、count/witness、五层before/final count及共享
  fragment-index；v23不推断history证据。真实64-fragment fixture在所有粗层保持平衡
  时学到四个14:2/2:14 history contexts，并覆盖重启与篡改拒绝。这是ExplainFuzz动机
  下的bounded order-2近似，不是完整任意long-range概率电路。
- F225用append-only online alpha ledger修复F223跨context multiplicity边界。
  Parent/circuit/sibling/history第`c`个context获得
  `0.05*6/(pi^2*c^2)`，context内部再对launch/look执行F223两层逆平方spending。
  三重预算和不超过0.05，因此在各true-null context满足bounded
  constant-conditional-mean时，无需context间独立即可控制无限context、无限扫描与
  可选停止下的global FWER。V25验证不可回收ordinal、first index、allocation ID/
  prefix和全部retained receipt membership；ledger损坏只关闭global audit。V24从
  validated ordered receipts迁移。注册observation从global CS排除以保证alpha先于
  被检验结果确定。该证书不改变weight或proposal验证。
- F226把五层PCFG变成可执行的等CPU机制消融。`global/parent/circuit/sibling/history`
  各档停止更高层context、receipt、certificate和factor-state构造，而不是只把weight
  归零；level 0跳过contextual forest，level 3/4分别使用一阶/二阶shape frontier。
  MPI按sealed run输出内容寻址artifact，绑定protocol identity、seed、CPU budget、
  context order与完整prequential/NLL/gain/delay/alpha/state telemetry。F67可从一条
  base command扩展五档随机配对矩阵；成功但缺失、篡改或跨run绑定的artifact记为
  failed。分析器按run ID连接artifact与coverage rows。机制和验证已落地，但公开
  20-repeat campaign尚未执行，不能把I/T/E写成R。
- F227把F212协议接入真实Tree-sitter增量API。持久Unix服务保留content-addressed
  native TSTree，逐byte复检manifest edit后调用`Tree.edit`和
  `Parser.parse(candidate, old_tree)`；reuse receipt只包含跨old/new tree保持相同
  native node ID的offered节点。Manager继续复检symbol/state/span/epsilon/yield
  digest，daemon丢失base时cold fallback。真实Tree-sitter 0.25.2、JSON grammar、
  Unix RPC与manager端到端链路通过；该v2 CST不等同完整歧义forest。
- F228为该路径加入可复检成本实验面。Proposal state v11累计manager/native parse
  time、trace bytes、request/offer/receipt/zero-reuse、node-ID proof和reuse/
  invalidation；counter lattice损坏拒绝恢复。Cache-off仍执行同一parser/trace gate但
  不创建manifest，形成cost-faithful cold control。Sealed protocol生成等CPU
  cold/incremental矩阵，MPI artifact绑定run和cache mode，分析器只连接验签指标。
  机制已实现，但公开20-repeat parser speedup/coverage campaign尚未执行。
- F229接入真实Lark generalized Earley `ambiguity=forest`路径，不枚举parse trees。
  Byte-mode dynamic-complete解析保留Symbol、LR(0) item、Packed production、Token和
  非主路径sharing；selected projection只在多primary-parent处有界复制。零宽子森林
  折叠为trace-v4 leaf并携带manager独立重算的nullable SCC/fixed-point rules。
  循环无限歧义及node/edge/alternative/rule/byte超限全部fail closed。该实现关闭
  “没有真实complete-SPPF provider”缺口，但Lark为pure Python且尚无incremental
  forest reuse，不能写成native GLR/GLL。
- F230把F229自报规模升级为manager复检证据。Proposal state v12交叉验证provider
  proof、grammar digest/version、accepted/complete、raw/clone/encoded、实际
  alternatives/edges/nullable rules和elapsed，并将完整漏斗/规模/成本写入sealed
  parser artifact。三档`off/selected/complete`等CPU、cache-off protocol绑定mode，
  非forest cell不能伪报forest trace。该矩阵能测cross-parser tradeoff，但grammar
  acceptance差异仍需单独校准。
- F231关闭了上述“单独校准”中candidate不配对的实现缺口。新的dual-oracle wrapper
  对同一candidate并发运行complete-SPPF primary与selected-CST secondary；manager
  复用完整v1--v4 validator校验嵌套secondary trace，再累计both-accept、primary-only、
  secondary-only和both-reject。Proposal state v13与artifact密封两个child argv摘要、
  candidate/trace摘要、grammar、四格恒等式和每侧耗时；新的
  `selected/complete/paired`等CPU协议能实际报告acceptance confusion matrix。
  这仍不是symbol-level grammar alignment，也没有实现incremental forest。
- F232在both-accept pair上由manager提取primary alternative-zero、primary完整forest
  与secondary selected tree的去重byte-yield span，并单独比较内部boundary。State
  v14与sealed artifact复检subset/intersection/union恒等式，导出selected/forest
  precision、recall、Jaccard和boundary Jaccard。该层不依赖symbol name，已关闭
  structural span precision/recall的采集缺口；F234在其上补充保守、样本条件化的
  symbol/production correspondence，但完整grammar equivalence仍未实现。
- F233接入Parglare 0.21.1 GLR的直接Parent/possibility SPPF，不读取solution count
  或枚举tree。它与Lark共用cycle/nullable/primary/topological/resource proof，
  携带独立provider schema/version/grammar identity，并已通过同candidate
  Earley-vs-GLR paired both-accept与span overlap。现在有两个真实complete-forest
  algorithm可做differential validation；二者仍都是pure Python且非incremental。
- F234在共同接受的alternative-zero projection上透明展开provider wrapper，并以
  byte span、递归有序child extent shape和双侧唯一性建立sample-conditioned symbol
  correspondence；production还必须具有相同非空child partition。State v15与sealed
  artifact保存parser-identified mapping、support、ambiguity和canonical digest，
  manager/protocol双verifier复检总和与上界。该实现关闭“没有可执行symbol/
  production对应证据”的缺口，但不证明grammar语言等价。
- F235对显式byte alphabet和最大长度执行最多65536个shortlex完整枚举；每个case
  仍经过F231 paired wrapper与manager结构复检。Sealed artifact记录精确域基数、
  四象限、完整transcript摘要和最短反例，`--require-equivalent`可作为CI gate。
  这关闭“小有限域仍只做随机采样”的缺口，不解决一般CFG无界等价。

仍需实现：

- 融合string/data access与path divergence，超越当前comparison-taint spans；
- 在F229/F233双algorithm真实complete-SPPF语义基线上接入native或incremental
  GLR/GLL forest provider，并把F227 exact-edit/reuse proof推广到complete-forest
  reuse；
- 在F234 sample-conditioned correspondence上执行holdout稳定性、support与ambiguity
  校准，并用显式grammar transformation/lexer taxonomy推进到可解释grammar convergence；
  同候选acceptance四象限由F231、byte-span/boundary由F232实现；
- distributed physical corpus ownership/replacement的epoch/fence proof与公开
  hypervolume/coverage校准；
- native/incremental GLR/GLL complete-forest parser、完整constraint-expression ECT，以及能表达多slot联合、
  任意距离/双向sibling、任意ancestor或cross-parent long-range依赖的概率factor，
  以及使用F226工件实际执行的公开confirmatory calibration、对结果残差驱动的
  arbitrary-distance/cross-parent设计，以及更高功效但有额外依赖假设的online
  multiple-testing消融。

依赖关系：G03/G04 提供比 branch-only taint 更稳定的 token/semantic evidence。

### G09. 通用 IFSS Backsolver 与 Hydra 式 targeted transformation

**状态：`P/I/T/E`（显式有界多臂 IFSS region-state/worklist、IFSS-like
relevance slice、compiler 级 single-site bounded linear/internal-tree Hydra
transformation、
fork-eliding ITE、MemorySSA/AA证明的双臂write-state merge、aggressive memory
readback、partition-scoped condition DAG ownership、有界normal-return exit
state、有界switch chain、bounded multi-continuation exit-id/scalar/memory-live-out
tuple、bounded affine natural-loop summary与 original replay/denylist 已实现；
bounded acyclic nested-MemoryPhi provenance、bounded multi-break priority loop
exit、path-predicate-owned Hydra tree和bounded 2--4-latch conditional byte-lane
fixed point已实现；general loop IFSS、general memory state与arbitrary-SESE
Hydra engine未实现）。**

当前实现支持 `select`、two-arm PHI，以及3--8 incoming、最多32 blocks/64 paths的
branch-only无环single-exit region。多臂实现具有显式IFSS路径worklist、相关谓词、
region state merge和目标符号替换；F260另支持2--8 exit、至少2 destinations和
1--8 scalar PHI slot的有界multi-continuation dispatch；F262进一步把最多4个由
MemorySSA/AA证明的MustAlias/NoMod scalar location加入dispatch；F261可闭式加速
trip<=8的两block constant-add affine loop；F269接纳store/LiveOnEntry部分重叠并
生成路径局部snapshot；F270把非dispatch MemoryPhi按每exit最多4 phi/16 node的
canonical provenance tree展开并递归重放。完整Backsolver仍需要cyclic/general
memory、byte-lane partial overlap、general loop和exception state。

OOPSLA 2026 的 Hydra 进一步用 failure-preserving、非完全 semantics-preserving 的
targeted control-flow transformation 移除昂贵 symbolic branches，并检测变换引入的
spurious failure。它比固定的 bounded Veritesting 更激进，也更适合本项目的
proposal + original replay 可信边界。

已实现子集：

- `semantic_proposals.py` 从 comparison-taint core 构造 IFSS-like relevance slice，
  按 target branch、interesting 标记、依赖密度和 span 长度排序；
- `SemanticProposalGenerator` 新增 kind=`targeted_transform` 的
  `hydra-copy-core`、`hydra-swap-cores`、`hydra-boundary-*` 和
  `hydra-truncate-after-core` 候选；
- `VerifiedProposalManager` 接受 `targeted_transform`，但仍只 materialize data-only
  candidate，不执行代码或变换后程序；
- 所有候选必须经原程序 target telemetry 和 AFL novelty triage 验证；
- `test_semantic_proposals.py` 覆盖 relevance slice、core copy candidate 和 ingest；
- F248新增独立LLVM module pass，在symbolization前从稳定site profile选择一个严格
  two-arm/single-exit diamond，以compatible-operation LCS构造complete alignment；
- 匹配ALU通过operand select合并，未匹配ALU插入safe extra operand；激进模式支持
  simple load linearization及load-old/select/store的readback store；
- 变换生成的select带`!symcc.hydra_select`，只建立ITE而不重新产生
  `_sym_push_path_constraint`，真正消除目标fork；
- `symcc-hydra-transform-v1` manifest约束single-site build；sealed replay driver
  对每个input执行transformed和original，只有original确认的failure才可保留，
  transformed-only failure生成denylist，preservation violation fail closed；
- checked native smoke得到1 real/1 spurious并完成semantic replay。F248严格边界和
  artifact详见技术档案243。
- F249从多臂PHI incoming predecessor求共同controller，显式枚举有界路径并按
  predecessor构造AND/OR relevance predicate，再以反向ITE链完成region state merge
  和symbolic PHI substitution；
- F249要求枚举终点与PHI incoming精确相等，最后一臂作为完备default state；任何
  late synthesis failure原子回滚临时IR。原生三臂test得到跨root branch的
  Backsolver SAT witness，opaque-call拒绝test证明无孤立runtime call。详见技术
  档案244。
- F250识别merge-local load的二输入MemorySSA phi，只在两个incoming都是等宽
  simple MustAlias store、区域内其他写对目标location无Mod时构造memory-state ITE；
  metadata绑定load/controller/store稳定site。NoAlias、opaque late rejection和
  poison均fail closed，原生test得到未执行store arm的SAT witness。详见技术档案245。
- F251允许每个MemoryPhi incoming沿MemoryDef chain跳过最多8个经AA证明NoMod的
  definition，chain metadata记录arm-local count及稳定site；MayAlias、第9项、
  volatile/atomic/fence仍拒绝。两臂各跳过一个definition的原生test保持SAT witness，
  三类负向边界通过。详见技术档案246。
- F252抽出data/MemoryPhi共用的2--8 endpoint region partition，统一32-block/
  64-path、postdom、branch-only与精确终点证明；MemoryPhi推广到3--8臂并结合F251
  chain生成多层memory ITE和逐arm metadata。三臂one-hop native test得到跨arm
  witness，任一MayAlias arm令整体拒绝。详见技术档案247。
- F253在构造predicate前按确定性顺序去重每个partition中的原始condition；每个需要
  合成的condition独占一个short-circuit computation和exit PHI，cache仅保存无所有权
  RegionValue，从而保证多路径复用时定义支配所有使用。metadata使用
  `partition-condition-cache-v1`；任何empty-input或late failure仍整区回滚。四臂
  data与四臂MemorySSA native test各只生成两个internal condition computation，
  经LLVM verifier并得到跨controller memory witness。详见技术档案248。
- F254在symbolization前证明2--8个normal return属于共同conditional controller下的
  single-entry、branch-only、acyclic、32-block/64-path有界区域，再归并为带稳定
  arm/controller/site证书的return-state PHI。既有F249--F253由此构造跨callee/caller
  的返回ITE。三返回native test得到非执行出口的validated witness；switch、loop、
  aggregate、poison、九出口和musttail均保持未变。详见技术档案249。
- F255把2--8个唯一case/default的strict switch fanout按稳定case顺序lower为
  equality branch chain，使F252 data/memory partition和F253 condition ownership
  原样复用；direct-return switch还能继续进入F254。三臂MemorySSA native test从
  `A`生成case `B`候选，data/memory各只有一个internal condition cache；shared
  destination、external predecessor、conditional arm、九臂和mixed exit拒绝。详见
  技术档案250。
- F256增加unsigned range-balanced tree：内部`ule`二分、leaf `eq`、default多路径
  PHI incoming复制并由F252做OR。7-case最坏深度从7降到4，代价是comparison从7增至
  13；8-arm native test验证12个condition cache、7层ITE和case6 witness。详见技术
  档案251。
- F257增加按stable switch site读取的profile-weighted optimal alphabetic tree：
  对有序case权重执行确定性区间DP，root proof绑定完整profile指纹、权重和objective；
  缺失、不可读、不完整、重复或case集合不符均proof-carrying回退balanced。偏斜
  七case测试把root bound从2移到0并保持native case6 witness。聚合default权重因
  缺少gap分布只记录、不参与split。详见技术档案252。
- F258允许多个case/default edge共享至少两个unique destination，并证明原始与
  lowered edge/PHI multiplicity。Linear保持`3→3`，range树按default leaf miss
  形成`3→5`；两unique-arm data不再误走direct shortcut，MemorySSA输出multi-path
  proof，shared return可组合F254。原生测试从`A`得到`B` witness。详见技术档案253。
- F259安全导入LLVM标准`branch_weights`作为external profile缺席时的有序case
  权重，显式坏external与zero metadata仍分类回退；JSONL manifest绑定来源、DP
  objective、完整tree、case/destination和shared multiplicity，独立verifier可重算
  双fingerprint并拒绝篡改。详见技术档案254。
- F260把branch-only、acyclic multi-exit region降为真实消费`i8 exit_id`的dispatch
  和per-destination scalar live-out tuple；每条逻辑exit经capture/resume保持edge
  multiplicity，destination PHI一次性改写，强制F252完整partition。三exit/
  两destination/两slot测试从第三出口输入`C`生成跨continuation输入`B`，共享双edge、
  cycle/vector/poison/单destination边界均经verifier。详见技术档案255。
- F261对canonical两block natural loop证明`iv=0; iv<T; iv++`、`T<=8`及1--8个
  side-effect-free `state+=constant` recurrence，再生成模位宽闭式
  `initial+T*step`并删除循环。单/双state、跨位宽和exit PHI经32输入差分；
  直接solver 1 query得到`05`，bound/memory/nonlinear/signed/poison/flags均拒绝。
  详见技术档案256。
- F262在F260 dispatch上要求destination load的MemoryUse由同一MemoryPhi定义，
  对每个相关exit沿最多8个MemoryDef跨越AA证明NoMod的definition并找到等宽simple
  MustAlias store；最多4个location形成带非消费neutral值的memory PHI。两个location
  和one-hop chain经256输入差分，原生`C`生成`B`；MayAlias、缺store、volatile、
  external predecessor、destination写及第5 slot均fail closed。详见技术档案257。
- F263在manifest输出前重放capture/resume/destination/scalar tuple，并对每个memory
  slot重新运行MemorySSA/AA proof后才记录store/NoMod chain；规范JSONL内容指纹由
  独立Python verifier重算。Memory-on/structural-only均通过，destination、chain和
  hash篡改均拒绝。详见技术档案258。
- F264在build后验签F263 manifest，并用SHA-256/size绑定input IR、lowered IR、
  SymCC binary和LLVM tool/version；write-once envelope经flock、双fsync和atomic
  rename提交，verify重哈希全部artifact。重复seal、IR漂移和envelope篡改均拒绝。
  详见技术档案259。
- F265先验签F264 envelope，再在独立`opt`进程对sealed lowered IR重新构造
  MemorySSA/AA continuation manifest，并要求新旧records完全一致。Alias修改后的
  合法IR可以重新seal，但其旧NoMod chain被语义replay拒绝。详见技术档案260。
- F266把canonical loop扩展到2--4个同位宽unit-diagonal upper-triangular affine
  states，用nilpotent/binomial闭式代替trip枚举；独立manifest verifier重算`A,b`、
  全部bounded powers/offset和内容指纹。三状态链经32输入差分及1-query SAT，
  matrix/power/hash篡改与逆三角/non-unit/cross-width均覆盖。详见技术档案261。
- F267证明单一post-update `iv==break_at` exit，把实际迭代数闭式为
  `break_at<trip ? break_at+1 : trip`，并分别恢复normal header state与break latch
  update。64组差分、跨exit SAT、完整truth-table/phase manifest及独立verifier已
  落地；`ne`、`iv_next`、反向edge和direct use拒绝。详见技术档案262。
- F268把Hydra从single-block diamond扩展到两侧各1--4个、块数相等的linear arms，
  在flattened跨块SSA序列上执行LCS并原子删除region。结构manifest绑定block sites、
  alignment/output identities、coverage replay要求与fingerprint；独立verifier及
  campaign gate拒绝三类篡改。2×2正例经256输入差分与symbolized verifier，
  unequal-arm fail closed。详见技术档案263。
- F269允许同一continuation destination的相关exits混合MustAlias store与经NoMod
  chain到达MemoryLiveOnEntry的状态，并在initial exit专属capture插入snapshot。
  v2 schema/manifest绑定state kind和实际tuple incoming；256输入差分、跨initial
  Backsolver候选、kind tamper和重新seal后的incoming-value tamper replay均覆盖。
  Partial MayAlias路径保持拒绝。详见技术档案264。
- F270允许相关exit递归穿过有界acyclic非dispatch MemoryPhi：每phi 2--4臂、
  每exit最多4 phi/16 provenance nodes，每条edge继续执行F262/F269
  MustAlias/LiveOnEntry/NoMod证明。V3 canonical preorder manifest绑定node、source、
  incoming block与child ordinal，独立replay递归核对实际scalar/dispatch PHI。
  两层正例经1024组差分、symbolized verifier与Backsolver候选；edge/value tamper、
  MayAlias和5-arm phi拒绝。实现同时修复递归region concrete clone的拓扑前移
  dominance错误。详见技术档案265。
- F271把F267扩展为2--3个ordered post-update equality break，以strict minimum
  `(break_at, ordinal)`构造winner/execution closed form和phase-correct dispatch。
  V2 manifest保存有序site/value/state identity及完整trip×break-vector table；
  verifier独立重算winner、phase和fingerprint。2-break independent与3-break
  triangular正例分别经512/256组差分，winner/site/live-out/hash篡改及第四break、
  `ne`、reversed edge拒绝。详见技术档案266。
- F272解除F268的等块数限制：每侧独立接受1--4个linear blocks与最多64条flattened
  instructions，以deterministic compatible-LCS处理one-sided edit和跨块def-use。
  V2 manifest绑定左右块/指令数、算法身份、每个edit slot的site/opcode/block
  ordinal、edit distance、output PHI与完整fingerprint；独立verifier及campaign
  gate核对单调顺序、完整site消费和内容身份。2x1与最大1x4正例各经256组差分和
  symbolized verifier，ordinal/edit篡改拒绝；第五块和internal branch tree保持
  fail closed。详见技术档案267。
- F273接受每侧最多7块、3个internal condition、4条merge leaf edge的unique-parent
  acyclic branch tree。Lowering按outer/internal edge outcome构造block/leaf guard，
  matched/extra ALU选择真实或opcode-safe operand，multi-incoming output PHI按完整
  leaf guard重建；aggressive tree store以old-value readback保持inactive路径。
  V3 manifest绑定terminator、parent/incoming edge、successor/merge topology、
  leaf集合、alignment ordinal与fingerprint，独立verifier重建indegree/preorder和
  merge-edge覆盖。3x1、最大7x7/8-leaf与aggressive matched-store正例各经256输入
  差分和symbolized verifier；parent/leaf/successor tamper拒绝，第4 branch、empty
  alignment和内部reconvergence fail closed。详见技术档案268。
- F274以`symcc-transformation-seal-v1`统一封存continuation、loop和Hydra：
  各manifest先独立验签，再与有序proof identity、input/lowered IR、compiler、
  LLVM tool/version及精确replay配置共同SHA-256封存。独立进程清除ambient变换变量，
  从原input IR重跑完整pass，要求新manifest逐record相等、新textual IR逐字节hash
  相等且通过LLVM verifier，最后再次重验seal。Memory-on/off continuation、
  recurrence+three-break loop和最大internal-tree Hydra均正向重放；三类
  verifier-clean、重新seal的lowered-IR篡改仍拒绝。详见技术档案269。
- F275在continuation scalar memory proof失败时，为2--8 byte整数load执行bounded
  linear MemorySSA byte-lane nearest-writer恢复：same-base inbounds常量区间store
  按新到旧占据lane，LiveOnEntry补齐未写byte，DataLayout决定双端序bit位置。V4
  metadata/manifest绑定每条lane source并进入fingerprint；独立LLVM replay重新执行
  AA/MemorySSA且解析实际extract/shift/OR链。Little/big-endian、256输入差分、
  Backsolver witness、双seal/replay与fresh-sealed value tamper均有自动化证据。
  Dynamic alias、MemoryPhi partial write和volatile/atomic仍fail closed。详见技术档案270。
- F276接纳单路径一个pointer-select store：instruction guard的两臂必须分别证明为
  same-base constant overlap或AA NoAlias，conditional lane以guarded overlay回退到
  older F275 source。V5 proof绑定guard/polarity/store/source byte，独立replay核对
  逐臂alias分类与实际i8 select。1024输入差分、双seal/replay、cross-continuation
  model与tamper rejection通过；未知arm和第二guarded store fail closed。详见技术档案271。
- F277接纳single-header/single-latch的两输入loop-carried MemoryPhi。Entry复用F275
  lane proof，backedge逐lane选择constant-offset store或同一整数PHI self-carry；
  v6绑定header/entry/backedge topology、双NoMod链和完整transfer。1024输入差分、
  双端序、双seal/replay、Backsolver model和carry/value tamper rejection通过；
  conditional/multi-latch/dynamic/volatile/full-width-no-carry拒绝。详见技术档案272。
- F278把一个pointer-select store扩展到depth-2、3-node/4-leaf finite union，
  逐leaf执行interval/AA分类并绑定canonical tree；1024输入、双replay、model和
  topology/value tamper通过，depth3/unknown/shared-DAG/双partition拒绝。详见档案273。
- F279按MemorySSA newest-to-oldest给两条guarded writes分配priority，并生成
  `new ? new : (old ? old : base)`；v8重放实际嵌套select。1024输入、双replay、
  model与priority/value tamper通过，第三writer拒绝。详见档案274。
- F280把F277扩展为strict-diamond conditional cycle：latch MemoryPhi的一臂沿
  partial-store链回到header，另一臂直接self-carry；v9绑定branch/arm/guard/polarity
  并重放actual guarded byte select。1024输入、双端序、双replay、Backsolver model
  和tamper rejection通过；双writer、未知ModRef、非严格join拒绝。详见档案275。
- F281接纳一个entry加两个unconditional latch的header MemoryPhi；每个latch独立
  证明partial-store/self-carry并成为actual三输入integer PHI。V10绑定两条transfer；
  1024输入、双replay、Backsolver model与tamper rejection通过，third/full-width/
  nested-conditional latch拒绝。详见档案276。
- F282按MemorySSA次序组合一个最新depth-two pointer-union writer与一个旧
  single-level guarded writer。Lowering先生成旧guarded fallback，再把它嵌入最新
  pointer tree，得到`union-hit ? union : (old-guard ? old : base)`。V11同时绑定
  v5式guard/polarity/source和v7式tree/leaf proof；1024输入、symbolized verifier、
  双replay、Backsolver model与两类tamper rejection通过，反向次序与两个旧guard
  fail closed。详见档案277。
- F283把F280 strict conditional transfer与F281实际two-latch header组合。两个
  backedge可各自为conditional/unconditional partial-store/carry，至少一个
  conditional；v12逐transfer绑定kind、branch/arm/guard/polarity并重放actual
  three-input recursive PHI。Little-endian 1024输入、big-endian双conditional共享
  guard、双replay、Backsolver model与tamper rejection通过；argument guard、双writer
  arm和unknown ModRef fail closed。详见档案278。
- F284把header扩为一个entry加2--4个latch，逐backedge保留ordinary/conditional
  partial-store/carry和actual predecessor-selected 3--5输入integer PHI。V13仅在
  至少一个状态超过two-latch时启用，并对每条transfer显式编码presence tag。4-latch
  mixed 2560输入、3-latch全无条件大端、双replay、Backsolver model和tamper rejection
  通过；第5个latch fail closed。详见档案279。
- F285把单个backedge的strict diamond扩为最多3个内部条件、4个叶子的
  unique-parent二叉树。每个叶子独立证明pure carry或partial-store/carry，lowering
  在原叶子物化值并由actual latch PHI按predecessor选择，不提前合取guard。V14绑定
  node/child/leaf拓扑及逐lane provenance；最大树5120输入、big-endian tree+ordinary
  双latch、双replay、Backsolver和tamper rejection通过，5-leaf fail closed。详见
  档案280。
- F286把F278/F279/F282的分离状态统一为最多4层的newest-first writer sequence，
  有界支持最多2个depth-two pointer partition和2个single-level guard。Lowering
  oldest-to-newest应用layer，较新无条件写会mask较老lane；v15绑定全序、逐lane
  polarity/fallback和完整partition tree。最大四层192组全条件差分、大端双partition、
  双seal/replay、Backsolver与三类tamper通过，第三partition fail closed。F278/F282
  中“第二partition、反向guard/partition、两个旧guard”的旧拒绝边界由本项取代；
  depth-three/shared DAG/unknown leaf与跨exit不一致仍拒绝。详见档案281。
- F287把F273的unique-parent internal tree扩为有界acyclic SESE DAG。Compiler以
  最近公共post-dominator确定唯一外部merge，按stable site确定性拓扑排序每个arm，
  用incoming-edge guard OR恢复多前驱block并把local PHI折叠为ITE；v4绑定完整
  predecessor/successor、local merge/PHI、alignment/output与指纹。最大2+1 local
  merge及aggressive store正例各完成256输入差分，双replay/tamper通过；external
  predecessor、cycle和第4个local merge拒绝。详见档案282。
- F288把Hydra profile升级为原始binary/argv绑定的v2 artifact，并在compiler
  manifest与replay-v2 campaign中闭合profile digest、command digest、selected-site
  统计和original authority identity。Malformed/empty profile不再退化为隐式选择。
  四类compiler manifest统一使用`flock`、`O_APPEND`、partial-write循环和`fsync`；
  24并发compiler记录完整，替换original即使重封campaign也拒绝。详见档案283。
- F289在F274 seal上增加externally trusted Ed25519 signer、分离log signer、
  RFC9162-style Merkle tree/signed previous-root head、offline inclusion proof及
  content-addressed ZIP64 atomic import。8并发publisher连续append，signature/
  proof/blob/key/log/role tamper fail closed；无共享文件系统的接收端可离线验证。
  详见档案284。
- F290显式封存Hydra的LLVM poison/undef/freeze refinement、inactive operand
  safe-constant和exception fail-closed策略，并从alignment重算freeze实例计数。
  新cross-major gate先独立重放F274 baseline，再要求candidate LLVM verifier、
  manifest/proof identity、`llvm-diff`及byte-identical textual IR同时通过；LLVM
  18.1.3与17.0.6的checked certificate已落地。同期修复partial native model在未
  通过完整Query IR时写入async目录的问题。详见档案285。
- F291把linear continuation MemorySSA上的dynamic byte GEP编译为精确overlap等式：
  固定`malloc/calloc`对象、固定load窗口和pointer-width index下，每个lane只生成
  `index = load_offset + lane - source_byte - base_offset`的有限case。V16绑定region/
  index/case及最多8层writer，第三partition也由该扩展接管；malloc/calloc、
  双端序、真实Backsolver、fail-closed边界和LLVM17/18 exact replay均有证据。
  详见档案286。
- F292把F291 equality作为单entry/单unconditional backedge的有限窗口transfer：
  每个lane以循环PHI byte为carry，当前迭代index命中时选择stored byte。状态大小与
  `load_width × store_width`相关而不随trip count增长；v17绑定cycle topology、
  all-carry state和region record。270组差分、真实Backsolver、大端、拒绝边界及
  LLVM17/18 exact replay均通过。Allocator准入同时收紧到标准非变参原型和
  pointer-index size位宽。详见档案287。
- F293在保留F287 v4优先匹配的前提下，把单region Hydra DAG扩为每arm
  14 blocks/5 branch/10 leaf/4 local merge/12 local PHI/96 instructions。V5按真实
  edge identity hash-cons block/PHI/leaf guard，并让左右arm共享lowered predicate
  negation；manifest/verifier从topology和predicate序列重算canonical edge、unique/
  reused predicate及FNV proof。13-block共享predicate正例完成256输入差分、双
  LLVM seal/replay/tamper与exact cross-major证书，16-block/5-merge拒绝。
  详见档案288。
- F294把F292的symbolic-region递推扩为2--4个互斥MemoryPhi latch transfer，并
  允许每个latch上的单writer受一层branch guard控制。V18按transfer绑定writer
  presence、region/index/signed case、guard polarity和actual PHI/select/icmp
  结构；不同latch作为alternative incoming state，绝不被错误地顺序复合。双回边
  126组差分与真实Backsolver、PowerPC64四回边大端、同latch双writer/dynamic
  extent/五回边拒绝及LLVM17/18 exact certificate均已落地。详见档案289。
- F295新增显式2--4 site的同函数v5 dominance-chain batch。Compiler先整批验证
  region ownership不相交、entry支配顺序和共同原始predicate identity，再以
  function-scoped cache复用确实支配后序插入点的negation；v6绑定transaction/
  ordinal/source和完整v5 proof。双13-block region将8条反值降为5条，256输入差分、
  无共同predicate/逆序/缺失拒绝、双record seal及LLVM17/18 exact certificate均
  通过。详见档案290。
- F296把F292单回边单writer扩为同一unconditional latch内2--4个有序dynamic
  writer。V19按MemorySSA newest-first绑定writer数组，共享固定allocation/extent，
  lowering按oldest-to-newest构造last-writer-wins select链，compiler从actual IR
  反向剥离到cycle PHI；Python独立检查ordinal/field exclusivity并重算fingerprint。
  小端3 writer 105组差分、真实Backsolver、PowerPC64大端、5 writer/unknown
  ModRef拒绝及LLVM17/18 exact certificate均已落地。详见档案291。

仍需实现：

- 超出F285 bounded predicate tree的一般predicate DAG，以及超出F291/F292/F294/
  F296单一固定heap object、固定read window、每latch 2--4个线性dynamic writer、
  单层guard和
  2--4 latch预算的symbolic base、pointer-PHI、strided GEP、dynamic extent、
  nested-predicate/conditional same-latch或multi-latch multiwriter
  symbolic-region fixed point；
- 超出F293/F295 14-block/4-local-merge/12-local-PHI、同函数v5-only strict-
  dominance chain和共同predicate-negation预算的任意共享CFG DAG、跨函数/非支配/
  overlapping selected-region structural reuse、switch/invoke/EH region与动态
  multi-site profitability proof；
- 带external predecessor的critical-edge region扩展；
- 公共透明日志服务、checkpoint witness/gossip、key rotation/revocation/HSM/
  timestamp，以及continuation跨partition结构hash-consing；
- LLVM 19--21适配、一般exception语义和任意IR refinement证明；当前F290只对
  项目支持矩阵内的17/18及sealed Hydra fixture做exact structural replay；
- 动态symbolic-address profitability证明；
- 公开target上的profile-guided safe/aggressive/original等CPU消融、spurious收敛和
  coverage/CPU统计。当前profile/transform/replay机制为I/T/E级，不是完整Hydra复刻。

### G10. ConDPOR 式输入/调度联合、最优并发探索

**状态：`P/I/T`（SC vector-clock、lockset memory-conflict pruning、runtime
read/write trace、schedule-constraint artifact、Query IR joint replay validation、
memory provenance filtering/tagging、source-style replay prefix 与 bounded
schedule-SMT structural encoding、mutex/rwlock complete-section enabledness、
condition-variable wait/wake、thread create/join operational subset 及 lifecycle
partial-order、scaled-anchor links、lazy exclusion refinement、direct model/certificate
solve、detach/cancel retirement、bounded Source-DPOR certificate/source/sleep set、
single-context path/schedule/RF、extended SC/TSO/RA subset、bounded wakeup tree、
operational ready/enabledness certificate、bounded ConDPOR graph/backward revisit/
maximal extension、path-dependent event regeneration 和 native atomic trace 已实现；
完整 ConDPOR/Optimal-DPOR 与完整 C11 memory model 未实现）。**

CONCUR 2025 的 ConDPOR 联合 concolic data nondeterminism 与 optimal DPOR，并给出
sound、complete、optimal 和 memory-model-parametric 性质。当前实现记录 mutex/
condition/join 等同步点，在相同对象上生成有界 thread-ID prefix；本轮进一步加入
compiler-emitted `read/write` memory trace、byte-granular preload logging、Python
HB/lockset 冲突分类、schedule-constraint artifact、Query IR joint replay validation、
stack/owner memory provenance filtering/provenance tag propagation，以及可由 Z3
解析求解的 bounded SC schedule-SMT event-position encoding、complete mutex/rwlock
lifecycle-state nonoverlap constraints，以及 condition wait mutex split 和 optional
signal/broadcast wake witness、stable logical thread identity 与 create/join ordering。
F49 又把内部 lifecycle 全排列约束替换为可线性扩展的严格部分序，同时保留
replay-visible event 的精确 permutation。F50--F52 继续把 controlled pairwise
links 降为 scaled anchors，并提供 order IR、构造式证书检查和 runtime prefix 投影。
F53--F55 又加入真正按 model violation 构造 libz3 AST 的 section refinement、直接
model extraction/solve CLI，以及 detach/cancel/join-cancelled identity retirement。
F56--F58 再加入 dependency-equivalence/source certificate、persistent sleep set、
Query IR QF_BV 与 schedule QF_LIA/read-from 的单 context 求解，以及有界
SC/TSO/RA memory consistency。F63--F65 再把 branch/action existence、operation-level
enabledness offers 和 pre-LowerAtomic memory-order evidence 纳入同一 artifact plane。

已实现保守子集：

- `annotate_schedule()` 为 trace event 附加 SC vector-clock 和当前 lockset；
- `happens_before()` 判断 vector-clock order；
- `classify_schedule_conflicts()` 识别未 HB ordered 的同步冲突，以及 same-object、
  跨线程、至少一侧 write、无共同 lock 的内存冲突；
- `DporScheduleExplorer.propose_prefixes()` 基于 conflict classification 和 F56
  causal source records 生成 bounded Source-DPOR replay prefix；
- `SYMCC_DPOR_MEMORY=1` 让编译器对非 atomic/volatile load/store 插入
  `_sym_notify_schedule_read/write`；
- `SYMCC_SCHEDULE_MEMORY=1` 让 preload runtime 写出 byte-granular `read/write` rows；
- `SYMCC_SCHEDULE_CONSTRAINT_OUT=PATH` 让 MPI master 追加
  `symcc-schedule-constraint-v1` JSONL，记录 trace digest、target branch、当前 prefix、
  bounded event/conflict summary 与实际生成的 replay prefixes；
- `SYMCC_SCHEDULE_QUERY_VALIDATION_OUT=PATH` 与 `symcc_query_service.py` 的
  schedule-validation 入口写出 `symcc-joint-schedule-query-v1` JSONL，绑定 schedule
  trace/replay prefix、Query IR prefix/target roots、solver result 和 bounded evaluator
  replay status；
- `SYMCC_SCHEDULE_SMT_OUT=PATH` 写出 `symcc-schedule-smt-v7` JSONL；其 F45
  structural base 为 bounded
  conflict-derived prefix 生成可独立物化的 QF_LIA SMT-LIB2 delta，硬约束包含 event
  permutation、per-thread PO、thread-prefix slot 和 source-conflict reversal。观测 HB
  仅作为可选 assumption literal；general runtime enabledness、read-from、alias、path roots
  和 weak-memory axioms 明确标为 not encoded；公共 event/PO/HB context 由
  `base_smt2` 共享，候选只保存 delta，并提供 `push/check-sat/pop` incremental script
  复用 solver context；
- F46 `symcc-schedule-smt-v2` 进一步把 lock attempt 与 `acquire/*_fail`、
  `unlock/rwunlock` 配对，在独立 `sync_ord_*` permutation 中恢复完整临界区，并把
  controlled-attempt order 与 `pos_*` order 连接；不兼容的完整 mutex/rwlock 区间
  必须不重叠，两个 rdlock reader 可重叠，trylock busy 仅作为 optional assumption；
  open/unmatched/truncated lifecycle 不生成 hard exclusion；
- F47 v3 从真实 preload trace 恢复
  `wait -> wait_mutex_release -> wait_mutex_acquire -> wake/timeout`，用 release/reacquire
  拆开 waiter mutex section。成功 wake 可在 `cond_wake_signal_*` optional assumption
  下选择区间内同 condition 的 signal/broadcast；plain signal witness 一对一，
  broadcast witness 可复用，timeout 无 wake witness，默认不断言以保留伪唤醒；
- F48 v4 用 logical child id 统一 create/start/exit/join identity，cleanup handler
  覆盖普通 return、`pthread_exit` 与 cancellation exit marker；硬约束 create 在
  thread_start 前、thread_exit 在 mapped join_success 前。join attempt 可先发生并
  阻塞，create/join failure、unmapped 或截断证据不生成 completion constraint；
  vector clock 同步传播 create-to-start 与 exit-to-join-success；
- F49 v5 审计 lifecycle formula 只依赖严格顺序原子、方向析取和 witness 选择，
  因而默认只保留 `sync_ord_*` 的部分序 rank，并以拓扑线性扩展解释为 SC 总序；
  replay `pos_*` 仍为精确排列。`SYMCC_SCHEDULE_SMT_ORDER_ENCODING=permutation`
  可恢复 bounds + all-different 作为差分/消融。受控双位置连接改成严格二方向
  析取，禁止 partial 模式下相等 rank 逃逸；
- F50 v6 用 `sync_ord_i=(L+1)*pos_j` scaled anchors 把 C 个受控 lifecycle
  events 的连接从 `C(C-1)/2` 降到 C；L 个预留整数槽足以嵌入 bounded window 内
  所有非受控 events。legacy permutation 保留 pairwise path 用于对拍；
- F51 导出 `symcc-lifecycle-order-ir-v1`，构造器按 solver ranks 选择 exclusion
  direction 并做确定性拓扑排序；独立 checker 重验 source ranks、anchors、固定/
  选择边、query replay slots/conflict reversal、总序保持、projection 和 digest；
- F52 的 CLI/API 只把通过 checker 的 controlled anchors 投影为 logical-thread-id
  prefix，并已通过真实 pthread preload 二次 replay。证书 scope 不包含 optional
  assumptions、path feasibility 或 concrete enabledness；含 read/write slot 或
  endpoint 的 query 标为不可由 pthread prefix 直接 replay 并拒绝物化；
- F53 v7 保留精确 `base_smt2`，另输出 hashed relaxed base；内置 solver 从 model
  检查全部 nonoverlap choices，仅把当前违例通过 `Z3_mk_lt/Z3_mk_or` AST 加入同一
  solver。只有零剩余违例才能返回 exact SAT/certificate；round limit 明确非精确；
- F54 通过 system libz3 C API 的 model completion 读取 `pos_*`/`sync_ord_*`，
  校验 base/delta digest 后自动构造并复检 certificate；`symcc_schedule_solve.py`
  可直接写 result/certificate/runtime prefix，保留 eager exact-base 消融；
- F55 在真实 create 前预留 logical identity，成功后绑定 handle；detach 与 exit
  两条件齐备或成功 join 后发出 `thread_retire`。取消阻塞中的 joiner 会记录
  `join_cancelled` 而不消费 target mapping，另一线程仍可 mapped join；
- F56 的 `symcc-bounded-source-dpor-v1` 重算 thread PO/dependency graph、
  canonical Mazurkiewicz class、causal wakeup sequence 和 source sets；F61 的
  `symcc-bounded-wakeup-tree-v1` 按 weak-initial recursion 检查 ordered leaves、
  sleep set 与 cooperative ready roots；
- F57 的 `symcc-joint-path-schedule-rf-v1` 在同一 libz3 context 中组合 Query IR
  bytes、schedule positions、RF selectors 和显式 `sym-byte/init/value` bridge，
  并由 QueryStore evaluator 与独立 verifier 双重重检；
- F58 为 schedule authoritative base 加入 memory-model-parametric constraints：
  SC last-global-write、TSO store-buffer forwarding/FIFO，以及 bounded RA
  rf/mo/hb/coherence/release-acquire subset；
- F221统一SC/TSO/RA schedule SMT的signed-integer printer，把reads-from初始源
  sentinel规范化为SMT-LIB `(- 1)`，并覆盖RF domain、coherence、RMW、seq_cst与
  Query-IR value bridge。真实system-libz3/cvc5 1.1.2对store-buffering
  UNSAT/SAT/SAT oracle一致；已安装但parse error的backend仍会使evidence失败；
- F62 的 `symcc-bounded-condpor-graph-v1` 保存稳定 action/read/write/constraint/init
  nodes 与 po/rf/co/add-order，执行 causal-successor deletion、write-to-read
  backward revisit、deterministic bounded maximal extension 和跨 trace identity
  dedup；explorer schema v4 持久化 wakeup leaves 与 revisit hashes；
- F63 将 compiler branch/switch/action trace 与最近 read byte interval 关联；
  backward revisit 对 read-dependent constraint 及其控制流 suffix 标记 regeneration，
  不再恢复可能不存在的 observed events；
- F64 的 `symcc-operational-enabledness-v1` 从 runtime operation offers 与 pthread
  lifecycle 恢复 enabled/blocked/unknown，显式 stop+empty registry 才形成 bounded
  terminal witness；
- F65 在 LowerAtomic 前记录 atomic load/store/RMW/cmpxchg/fence；bounded RA 新增
  RMW immediate predecessor、release sequence、fence SW、seq_cst rank、显式
  non-atomic race rejection与 mixed-size interval source containment；
- `SYMCC_SCHEDULE_MEMORY_FILTER=stack|owner|all` 可在 preload runtime 侧过滤当前
  thread stack rows，并用 bounded owner table 压缩首次跨线程共享 transition；
  `SYMCC_SCHEDULE_MEMORY_PROVENANCE=1` 为 memory rows 追加 `prov=*` tag，Python
  artifact 统计 `provenance_counts`，joint validation 继续传播该统计；
- `test_schedule_exploration.py` 覆盖 HB、unprotected read/write conflict、共同 lock
  pruning、artifact/export、LD_PRELOAD replay、stack/owner memory filter、provenance
  tag parser/runtime、condition signal/broadcast SAT/UNSAT、真实 pthread wait 与
  create/join/`pthread_exit` lifecycle，以及 partial/permutation differential
  SAT/UNSAT、scaled-anchor count/SAT、certificate tamper/query checking、CLI 与真实
  topology-prefix replay；
  `schedule_memory_trace.c` 覆盖真实编译器/runtime memory trace；`test_query_store.py`
  覆盖 schedule artifact 与 Query IR 的 joint validation。

仍缺：

- arbitrary runtime operation 的完整 enabled set、unbounded maximal execution
  detection/race reversal 与每个 Mazurkiewicz class 恰好一次的 Optimal-DPOR proof；
- interpreter-level path-dependent event existence、变化控制流的 symbolic event
  generation、完整 consistency oracle，以及 ConDPOR sound/complete/optimal proof；
- 补齐 condition error path、mutex type/fairness、double join 与 detach/join
  undefined race、fork 和 process-shared object operational enabledness；
- 完整 C11/C++ executable semantics：consume/dependency ordering、完整 SC axioms、
  undefined-race consequences、lifetime/unaligned tearing、完整 x86 TSO propagation，
  并与 herd7/diy corpus 系统对拍；
- 把当前 executable certificate checker 升级为 proof-producing solver
  integration 或外部定理证明，并覆盖更多 operational choices；
- 在公开并发 benchmark 上做等 CPU、多轮 schedule coverage、equivalence reduction、
  solver cost 和 SC/TSO/RA differential evaluation。

保留当前 bounded explorer 作为 fuzzing fast mode；完整 ConDPOR 应是独立 verified
mode，不能通过修改文档宣称现有实现完备。

### G11. 完整 Cottontail Solve-Complete 和结构化 seed acquisition

**状态：`P/I/T/E`（F187在线grammar、F190 Query-IR hole、F191 plateau-gated
retained-history、F192 independent parser gate与F201 parser-state/nonterminal
structural context已接入；F399完成Query IR alpha-normalized contextual constraint
selection；完整LLM seed-acquisition/solve-complete同协议复现与公开实验待完成）。**

当前 ECT 有 context、dependency、visit 和 reward summary，agentic hooks 能传递
focus/target/action JSON，proposal manager 能 concrete replay。需要纠正的是：官方
Cottontail ECT自身也不保存Z3表达式，constraint expression是独立文件/通道，因此旧版
“summary ECT不能表示Cottontail约束”的推理不成立。F399在独立Query IR通道实现
alpha-normalized target shape和最近8个prefix roots+target的联合context shape，按
site/direction分配持久duplicate class，并以有界年龄恢复做novelty-first调度。缺失的是
Cottontail完整LLM seed acquisition/迭代Solve-Complete协议及其公开目标效果复现，而
不是“把solver AST塞进ECT”。

已实现保守子集：

- comparison core + token grammar 生成 data-only `solve_complete` candidate；
- F399在已验证Query IR DAG上保留op/width/constant/attribute/ordered-child和read-alias，
  只alpha重命名绝对input offset；联合prefix-target投影关闭跨根alias误合并，默认
  `structural`按shape novelty、priority和depth选query，300秒消除duplicate penalty，
  但不删除query或据shape推断SAT/UNSAT；
- online grammar可生成variable-length candidate并以rule ID记录target/AFL反馈；
- QueryStore SAT model artifact导出精确Query IR read set与真实evaluator gate，
  grammar只补全不相交lexical hole，并对每个变长candidate重跑Query IR；
- `symcc-query-grammar-hole-v1`绑定query/source/manifest/read-set/hole/rule provenance，
  tampered sidecar不能授权completion；
- F222修复MPI master的QueryStore生命周期绑定：只有异步query service进程成功启动
  且grammar-hole开关启用时才向semantic generator提供store；workers为0或启动失败
  均fail closed到普通grammar模式，不再引用未定义初始化变量；
- F223为parent/grandparent/sibling prequential gain增加context-wise infinite-
  horizon PFA certificate；它是审计输出，不授权grammar hole或绕过parser/target/
  AFL三重验证；
- F224为slot `i>=2`加入受五基线校准的second-order sibling-history factor，并用
  tuple forward/backward message在bounded packed forest上精确传播；它仍不授权
  grammar candidate或替代parser/target/AFL验证；
- F225为四类context增加append-only outer alpha-spending与cross-context
  infinite-horizon FWER certificate；global证书仍只是审计输出，不授权candidate；
- 只有经过AFL全局novelty保留的bounded seeds进入history bank；连续零coverage
  observation达到阈值后才按yield/validity/exploration选择历史token，正增益立即复位；
- 可选独立parser只在真实target gate后执行，非零、启动错误和timeout均拒绝；
  parser acceptance不替代target replay或AFL novelty；
- grammar rule绑定branch、input-size bucket与hole邻域组成的stable context；
  重复parser rejection只屏蔽当前context，后续acceptance可重新开放；
- 可选v1/v2 structural trace把grammar replacement span映射到最小enclosing parser
  nonterminal/state和ancestor chain；rule-scoped alias取代同rule的lexical context，
  trace provenance与选择结果进入proposal state；
- AFL保留feature按rule/structural-context计数，未覆盖组合获得递减探索奖励且状态
  有界持久化；
- parser tree归一化stable production ID，multi-slot/direct与mutual cycle可在
  acceptance后执行depth-bounded derivation；rule/path/production由独立64K grammar
  bitmap计数，不依赖AFL feature delta；
- production shape与concrete instance组成bounded incremental ECT correspondence；
  同形subtree只在已认证shape context内替换，内部yield/gap/path均二次复检；
- v2 trace的alternative 0限定当前parse ancestry，最多8路packed family在总计32条
  production内规范化；empty RHS/gap/yield一致的epsilon instance可在shape context内
  学习删除规则；
- v3 trace以roots/forward edges表达共享packed DAG；v4 fragment保留bounded deep
  closure，非主深层instance只在selected shape context内执行；
- v4 trace提供bounded nullable dependency rules；v5 fragment绑定SCC与least
  fixed-point proof，纯循环不授权epsilon，proof-backed shape仍受selected context门；
- accepted alternative-0 tree为production shape提供Dirichlet-smoothed posterior；
  canonical packed hyperedge的inside/outside质量作为九维Pareto调度信号，低概率shape
  不被剪枝且全部候选仍经parser replay；
- proposal 通过 concrete target replay、target telemetry 和 AFL novelty 验证；
- 不依赖 LLM，不执行外部生成代码；未配置独立parser时不把grammar completion当作
  parser proof。

仍推荐在 G01/G04/G08 之上继续：

- 将当前query/read-set/hole certificate继续扩展到string token与constraint-expression
  ECT，并连接native parser incremental parse cache；
- 把F227的exact-edit/node-ID方法推广到native GLR/GLL parser，在bounded shared-DAG、
  nullable certificate和multi-slot transaction上实现complete-forest reuse与跨parser校准；
- 使用F226对当前已实现的pairwise relation、parent/grandparent/sibling/history
  PCFG circuit、prequential、fixed-recency gate与adaptive-window certificate执行
  至少20次等预算确认性消融，再
  研究多slot联合、任意距离sibling或更深ExplainFuzz式概率电路，并独立报告
  relation precision、
  held-out/prequential perplexity、detection delay、fallback rate、
  valid-candidate/CPU与coverage/CPU；
- 把当前master-observation plateau升级为time/target-conditioned survival model，
  并在公开benchmark消融always-on/random/no-history；
- 单独记录 token cost、invalid ratio 和增量覆盖，保留非 LLM baseline。

## 7. P2：高风险前沿与领域扩展

### G12. Selective Concolic Testing

**状态：`P/I/T/E`（QF_BV query dependency partition、fail-soft mixed completion、
持久化cost-aware policy与timeout predictor已实现；跨理论domain策略待完成）。**

FM 2026 将“哪些输入/约束符号化”建模为 cost-aware MDP，并以 constraint-dependency
partition 和 solver-timeout predictor，把一部分变量交给 SMT，另一部分交给随机/
fuzzing 求解。F188已在每个persistent Query IR中构造assertion-byte超图：target闭包
保持symbolic，disconnected components可固定到witness做短SAT probe。F189进一步
按assertion/variable/closure/AST/costly-op结构分桶，学习selective hit与
full/selective cost EWMA，使用optimistic expected saving决定attempt/skip并预测短
timeout；策略state按worker/backend隔离且跨helper重启恢复。Concrete side在总预算内
尝试witness、zero、`0xff`和deterministic random completion。所有SAT仍满足完整
query；局部UNSAT/unknown强制full-solver fallback，因此不产生错误剪枝。预算与完整
policy/completion telemetry已接入ParaSuit schema和QueryStore。

剩余差距：

- 当前为contextual cost bandit，不是带长期coverage reward与transition model的完整
  MDP；需要在benchmark中比较static、global和contextual policy并校准prediction；
- FP/nonlinear-real/String/Array的domain-specific mixed symbolic/random partition；
- completion目前是byte assignment portfolio，尚未接grammar nonterminal、string
  token或浮点special-value sampler；
- 公开benchmark上selective hit、saved solver time、coverage/CPU与false-local-UNSAT
  fallback率消融。

### G13. Live-state search：Empc/CBC/CGS/Vital/TopSeed

**状态：`P/I/T`（G05 portable executable continuation-IR data/control plane 与
G10 bounded concurrent constraint plane 已实现；native-program live-state search
policy 未实现）。**

当前 path-cover、MCTS 和 PrefixDAG 排序大多作用于已完成 concrete trace 的 replay
任务，不是 live symbolic states。F38--F59 已提供 checkpoint identity、
schedule-conflict analysis、runtime memory-trace evidence、schedule artifact 与 Query IR
joint replay validation、memory provenance filtering/tagging、schedule-SMT structural
queries、mutex/rwlock/condition/thread lifecycle state、可线性扩展部分序、bounded
Source-DPOR/RF/weak-memory constraints、CAS solver/store/page-COW memory，以及可由
MPI 实际 resume/fork 的 bounded continuation IR。F68--F88 已将真实 LLVM/C 的
   bounded integer-SSA、static-global、exact pointer-size input buffer 与 fixed
   frame-local stack、bounded call-site heap lifetime、one-term finite symbolic alias
   以及 guarded pointer PHI/select union、fixed-capacity multi-instance heap 绑定到
   该路径；runtime-sized nullable malloc/calloc、in-place realloc 及无环 direct-call
   pointer argument/return 的 object identity 与 caller-domain certificate 也可跨
   frame 与 checkpoint 保留；local select/PHI function pointers 可做 guarded
   dispatch；constant-length memory compare 可展开为 core IR。后续仍需 heap
   graph、general points-to 与一般 external semantics，
才能让一般 target 进入 CPS/native live states。
以下论文机制尚未实现：

- Empc 的 multiple minimum path covers over live states；
- Compatible Branch Coverage 的兼容分支集合；
- Concrete Constraint Guided state selection；
- Vital 的 unsafe-pointer MCTS；
- TopSeed 从零学习 symbolic-execution seed strategy。

在没有 heap-graph/general-symbolic-pointer/external lowering 或 native adapter 前继续增加
同名 post-trace score，科研收益有限且会扩大名实不符。

### G14. UCSan 完整化与 heap path optimality

**状态：`I/T/E-mechanism + P`。**

F393-F395 已把早期 UCSan 子集推进到三个可执行闭环：严格、规范、可耐久重放的 JITI
object context；显式 stack/heap allocation-level OOB 与 alias-preserving UAF；以及逐字节
INIT/UNINIT、SSA/调用传播与 pointer/branch/extent/external sink-only UBI。realloc 的
failure/in-place/move、常见 Itanium C++ allocation/unwind、单线程 atomic RMW/cmpxchg tag
迁移和 LLVM 17/18 native 回归已经覆盖。F395 机制矩阵为 28 modes × 11 = 308/308
预期结果，完整门禁为 Python 954 passed 加 235 subtests、LLVM lit 256 passed 加 1 个
既有 unsupported。

该进展关闭的是 UCSan §3.4 的 scoped explicit-object 核心，不是完整系统复现。global
Super Object、full DFSan、一般 libc/custom allocator effect、callbacks/variadic entry、
复杂 non-local lifetime、跨线程 shadow linearizability、系统环境与更一般 object grammar
仍不完整；公开 nbench/UBITect 或等价的等 CPU 重复实验也尚未形成 R 级结论。

F72 的 alloc/free marker 提供了可迁移的有界 lifetime 基础，F73 又支持有限
symbolic offset；F75 为一个 site 增加 fixed-capacity 多 live instance identity，
并以 canonical first-free 减少仅由空闲 slot 命名造成的差异。但系统尚未对 heap
graph 做 isomorphism quotient，也不执行 path-optimal state merging，因此没有
实现 POSE path optimality。

可进一步研究 SANER 2026 POSE 的 path-optimal heap idea，但其原始对象是 Java；
迁移到 C 必须先解决 pointer arithmetic、alias、lifetime 和 object identity，不能直接
声称复现。

### G15. Gordian、ConcoLLMic、NeuroSCA 与 Symbolon

**状态：`P`。**

现有 `semantic_proposals.py` 实现 byte inversion、exemplar transfer 和对象 class
split/merge；agentic route 只选择策略。尚未实现：

- Gordian 的 inverse/surrogate/heap-partition ghost code 生成、编译和迭代修正；
- ConcoLLMic 的语言/理论无关 symbolization、environment modeling 和自然语言约束推理；
- NeuroSCA 的语义 constraint-core 抽取与 verifier 逐步补回遗漏约束；
- 2026-06 预印本 Symbolon 的 offline transformation search、skill distillation 和
  repo-level context-sensitive transform/run/replay。

这些技术适合作为 untrusted proposal plane。优先级低于 query IR、string/grammar 和
generator solving，因为后者同时提供更强的验证输入和非 LLM baseline。

### G16. ParaSuit 完整自配置

**状态：`I/T/E`（机器schema、条件参数图、context/interaction posterior、隔离
transfer prior、F396 executable provider/原子registry/生命周期路由、F397
program-bound value-space/MeanShift/silhouette，以及 F398 inverse-frequency
standalone/synergy parameter-selection stage 已实现；公开科研效果待验证）。**

F186新增`self_config_schema.json`版本化registry，自动发现参数值、numeric bounds与
`active_when`依赖；未知parent/循环节点拒绝，normalized registry可由CLI导出。
选择与反馈现在使用program phase、targeted、structural task和input-size context，
并维护有界parameter-pair posterior。Assignment token只归因本次实际选择项，修复
旧版更新全部unset参数的偏差。跨campaign state只以衰减read-only prior参与选择，
不导入目标内统计。

F396新增统一`symcc-parameter-provider-v1`：coordinator生成40项native contract，
`symcc-query-solver --print-parameters`声明23个实际读取的prefix/selective/PSCache
参数。stdout在读取过程中限制1 MiB、启动有界，values/finite bounds/condition/scope
严格解析；同合同去重，任一重叠冲突整provider回滚。合并后的53项按28 task、23
query-service、2 coordinator-campaign路由，只有task进入逐工作项策略，从而修复
已启动service和campaign参数的no-op reward attribution。state v3绑定task schema和
provider provenance，schema漂移时不导入旧posterior和值。双LLVM真实provider与100轮
稳定性/成本基准已通过；机制中位启动成本约1.888 ms，不代表coverage提升。

F397复现Algorithm 2的value sampling核心，并作适合并行SymCC的显式有界适配：MPI
master以完整argv和目标可执行内容SHA-256绑定程序身份；每个task参数维护至多512条
assignment-exact观测，聚类窗口至多256条。数值轴归一化，reward按相对worker耗时校正；
确定性MeanShift与silhouette门决定继续探索还是按簇平均utility抽样并在簇内构造加权
值。基础state与sidecar按字节哈希、sequence、task schema、provider provenance、程序
身份和精确策略配置联合验证；验证后的基础映射不再按路径二次打开。已知双簇oracle的
silhouette为0.914156472，1000/1000随机有界性质成立；这些仍是机制证据，不是覆盖提升。

F398从同一次assignment的runtime `branch_trace`提取稳定`(site,taken)`，去重并以
BLAKE2b MinHash限制为每次128项；它不使用并发先到先得的AFL全局新增位，也不改变AFL
corpus接纳。初始阶段对28个task参数的88个声明非unset值逐一建立baseline，只补传递
激活父项和hard guard；组合阶段使用论文`Σ1/frequency`评分，低于成员baseline的credit
置零，再归一化进行pure ParaSuit概率选择或与F186 hierarchy融合。官方审计提交中
inverse score计算后实际累加branch count的差异由频率4:1反例固定。第三sidecar绑定
base/value摘要、程序、registry、策略与sequence，并保留in-flight extraction phase。
定向11项、完整Python 980/980加235 subtests和LLVM18 259 pass加1 unsupported通过；
机制中位选择22.554 us，仍不是coverage提升证据。

剩余研究工作：

- 将统一provider推广到其余独立SymCC组件，并研究对任意第三方help/binary的安全
  parameter extraction，而不是要求工具作者提供合同；
- 用官方sklearn实现做离线cross-oracle，量化F397确定性有界MeanShift适配的决策差异；
- 为query-service/campaign scope实现按配置隔离的进程池、受控重启和跨运行reward
  attribution；在此之前它们只发现和审计，不逐seed采样；
- 在ParaSuit 12程序和本项目公开/LAVA-M targets做fixed registry、hierarchical、pure
  ParaSuit、hybrid、transfer prior与strategy portfolio interaction的等CPU、多轮消融。

### G17. Directed mode 的 Locus/TACO 完整语义

**状态：`P`。**

当前 target distance、path difficulty、actionseed 和 coloration 是可用的 directed
调度基础，但还没有：

- TACO-Fuzz 的完整 target-centric 两阶段 seed selection 和 extended path condition；
- Locus 的 agent-generated semantic milestone predicates；
- 用 symbolic execution 证明 predicate 是 target state 的安全 relaxation；
- predicate instrumentation、迭代 refinement 和 false-rejection audit。

此方向只在用户提供 targets 的 campaign 中启用，不应影响通用 coverage 默认路径。

### G18. Pangolin 跨前缀抽象复用与 AFL 原生全矩阵采样

**状态：`I/T/E`（原生全矩阵mutator、约束保持采样与有界跨prefix/cross-size
  exact-projection-ranked/field-renamed/width-aligned复用已实现，科研效果待验证）。**

QSYM runtime 已能缓存完整线性 constraints、执行 dense John/Dikin walk，并在保存
前检查 `A x <= b`；AFL Python mutator 现在也能读取 poly cache 的 full-matrix 字段，
恢复 feasible base，并按完整线性矩阵计算 joint direction 的 alpha interval。输出前
再次验证矩阵；若只能得到可行定点，也不会回退到破坏约束的普通 havoc。

当前已完成：

- poly cache 第六列支持 `lower:upper:offset=coefficient,...;...` 约束矩阵；
- mutator 将 `(model, byte box, full matrix)` 与独立 byte box 分离处理；
- 对不相关 AFL seed 先 bounded recovery 一个满足矩阵的 feasible point；
- 对 direction 同时施加 byte box bounds 与所有线性 inequalities；
- tests 覆盖 `x + y == 10` 这类相关约束，以及可行定点不回退 havoc；
- exact key miss后可对cache执行规范化linear relation分类：
  equivalent、cached-subset、current-subset与compatible；
- cache loader按artifact自身input size保留旧矩阵；不同变量集/输入长度可在当前有效
  shared offsets上进行per-row `[0,255]` interval elimination projection；
- projected relation只是保守召回；跨长度entry必须显式经过projection，model与过滤后
  sampling matrix仍逐个复检当前完整表达式；
- 有界Presburger关系层存在量化双方非shared byte，以双向蕴含和交集检查精确区分
  equivalent/subset/compatible/none；区间矩阵只保留为采样外包络；
- 同长度但字段offset变化时，可在有界变量/双射预算内搜索线性约束超图同构；命中
  映射统一重写matrix、model与byte box，签名只用于确定性排序；
- 该双射允许跨总input size，并以multi-byte coefficient保持证明相同width字段的
  endian byte permutation；不依赖host ABI或字段名；
- cached字段较窄时可搜索到current字段的注入映射，但必须由exact existential
  projection证明未映射current bytes的一致extension语义，interval-only拒绝；
- cached字段较宽时可选择具有约束签名或规范化系数证据的子字段，把未选cached
  bytes作为fresh existential variables消去；只有exact projection证明非空关系才
  接受，零结构重叠排列与interval-only narrowing均拒绝；
- relation只排序SAT artifact，跨prefix UNSAT严格禁止；外来model和每个衍生sample
  均由QSYM evaluator复检当前完整prefix与target；
- probe budget、relation/hit/validation failure与sample validation均有独立
  telemetry，并进入sampling profile、MPI helper和self-config；
- lit覆盖不同prefix key、两字节到一字节projection、奇偶整数投影不相交、左字段到
  右字段renaming命中、各开关关闭路径，以及窄prefix UNSAT不污染宽prefix。

剩余差距是：

- 当前width转换覆盖exact-proof unsigned byte-aligned widening与narrowing，
  未做sign extension、bitfield packing/unpacking或非线性抽象；精确
  整数关系尚未导出无量词projection matrix；
- 尚未在真实 benchmark 上报告 cache hit、sample yield、validity 和 coverage/CPU。

下一步应以真实benchmark判断是否值得加入无量词整数projection artifact、sign/packing转换与
跨格式 abstraction reuse；所有candidate仍需exact expression validation和concrete replay。

## 8. 建议实施顺序

依赖优先于论文年份，建议按以下顺序实施：

| 顺序 | 工作包 | 依赖 | 规模 | 主要产物 |
| --- | --- | --- | --- | --- |
| 1 | G01 Query IR + async trie solver | 无 | XL | trace corpus、solver daemon、query replay |
| 2 | G03 完整 Data Coverage | 可独立 | L | static-load instrumentation、无碰撞data novel set、ASLR稳定且PCGUARD namespace-correct的AFL兼容map、sealed edge/data replay join已落地；下一步公开parser/static-data等CPU消融 |
| 3 | G02 GenSlv + G18 native constrained generator | G01 | L | reusable IR、optimistic slice、native Z3 converter、persistent Query IR recipe replay与bounded cross-prefix/cross-size exact projection relation、endian-aware field renaming、exact-proof unsigned widening/narrowing已落地；下一步做benchmark消融与sign/packing研究 |
| 4 | G04 BV/string 双表示 | G01 | XL | 精确双view、overflow-proof decimal conversion、并行portfolio、Z3/cvc5 conformance与上下文选择已落地；下一步Princess/Z3str3、incremental smt-switch IR与完整endptr/base operation model |
| 5 | G06 solver portfolio + G07 PSCache | G01 | XL | G06 parallel portfolio、proof-carrying QF_BV、Z3/cvc5/固定Bitwuzla实机conformance、prefix context、X-means/BIC prior、ensemble beam、EI/SMBO、SAT-gated cancellation、Query IR features-v2、sealed三solver paired holdout/PAR-2/child-CPU verifier及AFL edge/data/feature-calibration join，G07 QueryStore cache、solver-helper probe与assumption-conflict core已落地；下一步公开corpus 20次confirmatory与外层CPU quota、native/cross-worker transport、checkable UNSAT及按证据决定CDCL trail hook |
| 6 | G08 Lase + G11 Solve-Complete | G03/G04 | XL | bounded grammar、Query-IR hole、history、parser path、multi-slot/mutual CFG、packed alternatives、shared-DAG/deep ECT、nullable SCC fixed point、atomic multi-nonterminal transaction、parser-neutral incremental cache、persistent native Tree-sitter CST增量复用与sealed cost telemetry、accepted-forest PCFG inside/outside、pairwise relation-aware slots/exploration reserve、parent/grandparent/sibling/history hierarchical PCFG circuit、精确order-1/order-2 factor消息、prequential calibration、fixed-recency stale/drift/recovery、bounded adaptive-window cut、context-wise anytime与cross-context FWER certificate、proof-carrying epsilon、独立grammar bitmap、九维rule Pareto与六维seed metadata archive已落地；下一步native GLR/GLL complete-forest adapter、公开parser/PCFG confirmatory campaign、任意距离/cross-parent long-range circuit factor、distributed physical corpus proof与更高功效online testing研究 |
| 7 | G09 IFSS/Hydra | G01 | XL | F248 Hydra、F249 data state、F250/F251 memory proof、F252共享多臂partition、F253 condition DAG ownership、F254 normal-return exit state、F255 switch chain、F256 balanced、F257 profile-optimal、F258 shared destination、F259 switch manifest、F260 bounded continuation tuple、F261 affine loop、F262 memory tuple、F263 continuation manifest、F264 atomic SHA-256 seal、F265独立bitcode ModRef replay、F266 upper-triangular loop、F267 bounded break exit、F268 multi-block linear-arm Hydra、F269 store/LiveOnEntry partial tuple、F270 bounded acyclic nested-MemoryPhi provenance、F271 multi-break priority circuit、F272 unequal linear-arm、F273 internal-tree path-predicate alignment、F274统一sealed input-IR exact replay、F275双端序byte-lane partial-overlap、F276 guarded two-arm alias partition、F277 canonical cyclic byte-lane MemoryPhi、F278 depth-2 finite pointer union、F279 two-write priority、F280 strict conditional、F281 two-latch cyclic transfer、F282 pointer-union/guarded-writer priority组合、F283 conditional two-latch、F284 bounded 2--4-latch、F285 bounded nested-predicate fixed point、F286 bounded ordered writer graph、F287 bounded acyclic SESE DAG/local-PHI predication、F288 profile-bound/concurrent-safe evidence、F289 signed Merkle-logged cross-host bundle、F290 LLVM semantics/cross-major exact replay、F291 fixed-heap symbolic-index writer graph、F292单回边symbolic-region byte fixed point、F293 bounded shared-predicate DAG guard hash-consing、F294 conditional 2--4-latch symbolic-region transfer、F295 dominance-proven cross-region negation hash-consing及F296 same-latch ordered symbolic-region multiwriter已落地；下一步为nested-predicate/conditional general region memory、critical-edge/switch region、跨函数/非支配结构复用、LLVM19--21与一般exception、公开透明服务及confirmatory campaign |
| 8 | G05 live continuation engine | G01 | XL | descriptor、CAS/page-COW、executable IR、MPI frontier、integer-SSA/static-global/input-buffer/fixed-stack、runtime-sized nullable heap/in-place realloc、finite-alias LLVM lowering、guarded pointer union、direct/indirect pointer arg/return、caller-domain certificate、bounded indirect dispatch、memory-compare/region-write/NUL-string/search/copy/scalar/bit-count/permutation/saturation/selection/objectsize/overflow summaries、optimization-hint/ssa.copy identity、integer defined-value guards、all-use bounded transitive deferred-poison freeze、byte-lane nearest-writer composition/direct-edge PHI、bounded data/function pointer-memory、global pointer tables、cyclic pointer-memory SSA fixed point、canonical constant-GEP cell identity、proven-nounwind invoke、stable nondeterministic freeze 与 CAS-rooted incremental QF_BV pruning 已落地；下一步 cyclic per-lane carry、general heap graph/alias-aware MemorySSA、完整exception/general external virtualization 与 native adapter |
| 9 | G10 ConDPOR + G13 live-state search | G05 | XL | bounded Source/Optimal-DPOR、operational offers、po/rf/co revisit/regeneration、single-context solve 与 extended RA 已落地；下一步 unbounded proof、完整 C11/herd 对拍与 native live-state search |
| 10 | G14--G17 experimental planes | 前述基础 | L/XL | UCSan、agentic transform、directed mode |

G02/G03/G04 可以由独立实验分支并行推进，但合入主线时都应统一使用 G01 的 query、
feature 和 result schema，避免再形成只能由 MPI Python 层理解的旁路格式。

## 9. 可形成的科研创新

仅逐篇复刻并不足以形成有辨识度的系统。基于当前架构，三个更有价值的组合研究问题是：

### RQ1. Distributed Query-to-Generator Service

把 Triereme 的 query trie、Marco 的异步全局调度、GenSlv 的 generator 和 Pangolin
的 poly sampler统一为 content-addressed service。核心假设是：并行 hybrid fuzzing
的共享单位应从 seed 文件升级为 query prefix 和 solution generator。

### RQ2. Hierarchical Semantic Coverage

把 edge、constant-data access、SMT string operation、token grammar rule 和 ECT
structural path 放入分层 novelty lattice。研究不同层级的 dominance、预算和 corpus
replacement，而不是把所有特征哈希进同一 bitmap。

### RQ3. Dual-Mode Parallel Symbolic Execution

保留高吞吐 seed-replay concolic mode，同时增加 GenSym-style live-state mode。两者
共享 query store、solver portfolio 和 coverage ownership，由在线 policy 决定一个
frontier 应继续 concrete replay，还是升级为 continuation fork。该设计比单纯增加
worker 数量更可能形成新的并行 SE 贡献。

## 10. 实验门槛

任何条目从 `P/M` 升级为 `I` 前至少需要：

1. 论文机制级单元测试，而不只是开关解析测试；
2. 与旧实现的结果对拍和 conservative fallback；
3. microbenchmark 证明目标机制确实被触发；
4. 公开目标上的 equal-CPU、至少 20 次独立重复；
5. median、A12/Cliff's delta、置信区间和 timeout/censored cost；
6. feature ablation 与 interaction ablation；
7. coverage 由 authoritative AFL/showmap 或原程序 replay 计算；
8. 记录 trace/query/generator/cache 命中率和额外内存，避免只报告最终覆盖。

当前已有 I/T/B 证据证明大量组件可以运行，但最新全系统仍缺少满足以上条件的 R 级
结果。因此，在完成此实验门槛前，应表述为“实现并通过测试”，而不是“达到 SOTA”。

F67 已把上述实验门槛实现为 `symcc-research-protocol-v1`：confirmatory 少于 20
repeats 会拒绝，target×repeat 采用 paired randomized blocks，所有配置使用相同
CPU-second budget，并封存 source/input/environment provenance、raw outcomes 与
failure/timeout。配套分析执行 paired bootstrap/sign-flip test、A12/Cliff's delta
和 Holm correction。它提供形成 R 级证据的工具，不代表 20-repeat 公开 campaign
已经执行。F67 的 coverage-over-time 现对 serial/MPI/hybrid/AFL-only 使用同一
showmap 定义；多引擎 hybrid 在每个时间点 hard-link live corpus union，campaign
结束后再内容去重和 replay，避免测量 CPU 污染被比较作业，并以 manifest 声明的
核数而不是 mode/np 推导 CPU/wall budget。

F60 的 `symcc-research-evidence-v2` 已把 F56--F59 以及 F61/F62 的 deterministic
obligations、formula/input hashes、bounds、backend capability 和 excluded claims
变为可执行 gate；verifier 继续接受历史 v1。
它能防止单元结果被扩大解释，并支持跨环境复检；但其
`runs_per_logical_obligation=1`、`performance_claims=false`，因此不满足上述 R 级
重复实验门槛。当前环境只有 system-libz3 时也只记录单一 family，不宣称独立
cross-solver consensus。

## 11. 原始资料

- [GenSlv, ICSE 2026](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/44/Generator-Solving-for-Symbolic-Execution)
- [ParaSuit, ICSE 2026](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/222/Enhancing-Symbolic-Execution-with-Self-Configuring-Parameters)
- [SymCC-str, TACAS 2026](http://theory.stanford.edu/~barrett/pubs/CB26-abstract.html)
- [Lase, OOPSLA 2026](https://2026.splashcon.org/details/oopsla-2026/43/Online-Input-Grammar-Synthesis-Aided-Symbolic-Execution)
- [Taming the Hydra, OOPSLA 2026](https://2026.splashcon.org/details/oopsla-2026/3/Taming-the-Hydra-Targeted-Control-Flow-Transformations-for-Dynamic-Symbolic-Executio)
- [Cottontail, IEEE S&P 2026](https://haoxintu.github.io/files/sp2026-cottontail.pdf)
- [UCSan, OSDI 2026](https://www.usenix.org/conference/osdi26/presentation/yin)
- [Gordian, ISSTA 2026 preprint](https://arxiv.org/abs/2603.19239)
- [Symbolon, 2026-06 preprint](https://arxiv.org/abs/2606.29108)
- [ConcoLLMic, IEEE S&P 2026](https://srg.doc.ic.ac.uk/files/papers/concollmic-ieee-sp-26.pdf)
- [NeuroSCA, 2026-03 preprint](https://arxiv.org/abs/2603.01272)
- [Selective Concolic Testing, FM 2026](https://conf.researchr.org/details/fm-2026/fm-2026-tap/2/Selective-Concolic-Testing)
- [POSE, SANER 2026](https://conf.researchr.org/details/saner-2026/saner-2026-papers/7/Path-Optimal-Symbolic-Execution-of-Heap-Manipulating-Programs)
- [Locus, ICSE 2026](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/61/Agentic-Predicates-Reasoning-for-Directed-Fuzzing)
- [TACO-Fuzz, OOPSLA 2026](https://2026.splashcon.org/details/oopsla-2026/35/Efficient-Directed-Hybrid-Fuzzing-via-Target-Centric-Seed-Selection-and-Generation)
- [SimiFuzz, ISSTA 2026](https://conf.researchr.org/details/issta-2026/issta-2026-research-papers/16/SimiFuzz-Seed-Worker-Scheduling-for-Parallel-Fuzzing-via-Contextual-Bandits)
- [SMTgazer, ASE 2025](https://conf.researchr.org/details/ase-2025/ase-2025-papers/47/SMTgazer-Learning-to-Schedule-SMT-Algorithms-via-Bayesian-Optimization)
- [ConDPOR, CONCUR 2025](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.CONCUR.2025.26)
- [Empc, 2025 preprint](https://arxiv.org/abs/2505.03555)
- [TopSeed, ICSE 2025](https://doi.org/10.1109/ICSE55347.2025.00095)
- [Backsolver, ACM TOSEM 2025](https://doi.org/10.1145/3712194)
- [PSCache, FSE 2024](https://doi.org/10.1145/3660817)
- [Data Coverage, USENIX Security 2024](https://www.usenix.org/conference/usenixsecurity24/presentation/wang-mingzhe)
- [Compatible Branch Coverage, OOPSLA 2024](https://doi.org/10.1145/3656443)
- [Concrete Constraint Guided Symbolic Execution, ICSE 2024](https://doi.org/10.1145/3597503.3639078)
- [Marco, ICSE 2024](https://conf.researchr.org/details/icse-2024/icse-2024-research-track/10/Marco-A-Stochastic-Asynchronous-Concolic-Explorer)
- [GenSym, ICSE 2023](https://conf.researchr.org/details/icse-2023/icse-2023-artifact-evaluation/20/Compiling-Parallel-Symbolic-Execution-with-Continuations)
- [Triereme primary paper](https://gleissen.github.io/papers/triereme.pdf)
- [Pangolin primary paper](https://home.cse.ust.hk/~charlesz/papers/pangolin.pdf)
- [Lari--Young Inside-Outside SCFG estimation](https://www.cs.jhu.edu/~jason/600.665/lari-young.pdf)
- [EvoGFuzz probabilistic grammar fuzzing](https://arxiv.org/abs/2008.01150)
- [ExplainFuzz probabilistic circuits, 2026](https://arxiv.org/abs/2604.06559)
