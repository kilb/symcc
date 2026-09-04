# SOTA 技术缺口实施路线与验收矩阵

- 基线日期：2026-08-14
- 当前功能集合：F00--F434
- 目标：关闭十类已审计的 SOTA 技术缺口，并以代码、测试、实验和文档共同证明完成
- 当前进度：F400--F404 已完成持久多目标、Empc/CBC/CGS live-state调度与TopSeed跨运行
  种子选择；F405--F409 已推进有限heap points-to、路径相关MemorySSA/AA与调用点实例化effect；
  F410--F421 已把符号长度region、loop MemoryPhi、常量stride/affine scale、residue-class lane
  cover、single-latch conditional writer guard-carry、2--4 latch SCC fixed point和每backedge有序
  multiwriter、两层inner-to-outer MemoryPhi摘要组合、常量值/最后写者v7以及outer/inner二维
  affine地址、双threshold最后写者v8、affine symbolic value v9与guard-specialized
  piecewise-affine value v10、Decision DAG value v11与显式opt-in事务循环摘要传递接入生产
  continuation；F423已把Agolic witness-guided BSE的release前单状态、release后普通探索、
  精确求模和串行replay接入该执行器；F424已把FM 2026 Selective Concolic的数据流落为
  受限QF_BV关系图切分、partial-model completion、完整SAT复核，并将Prefix DAG升级为
  Laplace转移与循环收敛的概率MDP；F425已实现native原子值/两阶段commit、SC/TSO/RA有界关系图
  证书与fresh-process前缀再执行；F426完成W4第一阶段的内容寻址QF_BV prefix delta chain、跨worker
  exact formula identity、本地parent extension、fenced物化配额、取消/崩溃接管和SAT双重replay；
  F427进一步完成cvc5 CPC/Ethos可独立检查的QF_BV UNSAT结果回执、跨worker精确复用和
  QueryStore二次证明裁决；F428完成ancestor-prefix限定的proof-carrying learned literal交换、
  fresh-worker注入和发布/消费双向QueryStore复证；F429进一步完成context/proof/receipt/lemma
  跨作业引用图、fenced job roots、grace roots、依赖感知有界GC和崩溃恢复；F430进一步以
  Z3父级prefix warm check和Linux COW per-target child实现同机原生solver-state隔离复用；F431进一步
  完成Cache-a-lot式input-byte变量置换UNSAT-core复用、源core独立CPC/Ethos证明和QueryStore二次
  精确包含裁决；F432完成确定性QF_BV bit-blast、增量CaDiCaL与proof DAG；F433完成求解中
  checked clause stream、native ACK和双向复证；F434完成MPI身份/共享状态资格、active-solve
  因果门禁、精确事件归属、root独立重放及本机多rank机制评测。
  W1仍缺完整
  external/native adapter、更一般heap/points-to与扩大
  跨过程域；W5仍缺initial symbolic heap、callback/varargs/custom allocator/concurrent shadow。
- 状态符号：`P` 计划，`I` 已实现，`T` 已测试，`E` 已形成机制实验，`R` 已完成公开目标确认性实验

## 1. 完成原则

功能编号连续、单元测试通过或存在同名策略都不能单独证明论文技术已经实现。每个工作包只有同时满足
以下条件才能关闭：

1. **语义闭合**：明确支持域、拒绝域和 fallback；不能用 post-trace 打分冒充 live-state 算法；
2. **生产接线**：功能必须从编译器/runtime/worker 的真实路径可达，而不只是独立 Python API；
3. **持久与并行正确性**：进入共享 frontier、solver service 或 corpus 的状态必须具备身份、代次和失败原子边界；
4. **反例测试**：至少覆盖错误合并、陈旧结果、预算耗尽、语义不支持和损坏 artifact；
5. **差分或独立 oracle**：变换、弱内存、求解和 heap 合并必须由独立实现或 concrete replay 复检；
6. **确认性实验**：性能/覆盖声明必须使用等 CPU、固定初始 corpus、预注册指标和多次独立运行；
7. **文档可追溯**：每个新增功能有功能 ID、实现映射、配置、测试、原始证据和不宣称边界。

## 2. 依赖关系

```text
通用 continuation / heap / alias / external semantics
  |-- native live-state search ---- S2F/TACO/ParaSuit/agentic planning
  |-- Hydra/IFSS general regions
  |-- native ConDPOR + C11 memory model
  `-- UCSan + POSE

Query IR + solver service
  |-- cross-worker incremental context/proof
  |-- SymCC-str cross-theory solving
  |-- Lase/Cottontail Solve-Complete
  `-- Pangolin projection/generator

以上全部 -> 等 CPU 多轮公开 benchmark 与最终完成审计
```

因此实施顺序优先关闭公共语义和证据面，再完成依赖它们的策略；agentic 模块始终位于不可信 proposal
plane，所有结果必须经过原程序 concrete replay 或精确 solver 验证。

## 3. 十项工作包的完成定义

| ID | 工作包 | 当前基线 | 完成所需的权威证据 |
| --- | --- | --- | --- |
| W1 | 通用原生 continuation 与 live-state search | bounded LLVM lowering、持久 frontier、多目标/Empc/CBC/CGS/TopSeed；有限heap points-to、MemorySSA/AA、调用点effect、符号region、single-latch affine/conditional、2--4 latch MemoryPhi fixed point及有序multiwriter transfer已生产接线 | 更一般heap/points-to域；完整external/native adapter；nested/value-summary循环拓扑；扩大跨过程域；重启与多 worker 等价测试 |
| W2 | Hydra/IFSS 一般控制流转换 | bounded SESE DAG、循环和 byte-lane writer graph | nested/overlapping/critical-edge/switch/invoke/EH region；跨函数或明确拒绝证明；变换前后 concrete differential、solver replay 和 spurious-result detector |
| W3 | native ConDPOR 与 C11/C++ 内存模型 | bounded trace/SC interpreter；F425原子值/commit、SC/TSO/RA关系图和fresh-process campaign | 扩展RC11 mixed-size/fence/RMW；弱内存RF可执行控制；herd7/diy/GenMC corpus对拍；unbounded sound/complete/optimal研究证明 |
| W4 | 跨 worker 增量 solver context | F426--F432完成formula/proof/lifecycle/COW/substitution/bit-blasted exact context；F433完成project-native mid-solve checked stream；F434完成MPI角色/身份/共享状态资格、native active门禁、并发event归属、root独立ACK裁决和本机多rank机制实验 | 8/32/128 worker公开多节点R级评测；ImpCheck原生wire adapter；Mallob动态资源重调度；failed-assumption最小化；跨version协商；可移植跨节点search-state；跨地域CAS/lock failover |
| W5 | 完整 UCSan 与 POSE | JITI、显式 OOB/UAF、byte INIT/UBI；有限已分配C heap基址的POSE-inspired条件lifetime | global/DFSan/callback/varargs/custom allocator/concurrent shadow；初始符号堆与C heap alias quotient；完整path-optimal fork oracle；nbench/UBITect或等价实验 |
| W6 | Lase/Cottontail 与 SymCC-str | bounded grammar/SPPF/PCFG、Query hole、BV/String 子域 | GLR/GLL complete forest；迭代 Solve-Complete；incremental smt-switch IR；完整 conversion/endptr/base 支持域；FP/Array mixed strategy；parser 实验 |
| W7 | ParaSuit、S2F、TACO | 参数 provider/value/selection 核心；actionseed 和有界 target score | 隔离配置进程池与重启；第三方参数发现；完整 S2F exact/tailored/sampling 协调；TACO 两阶段选择和 extended path condition；等 CPU 消融 |
| W8 | 验证隔离的 agentic 技术 | proposal/verifier API、Agolic coordinator core；F423有界continuation BSE runner、求模与串行replay | native/public-target invocation adapter与R级Agolic复现；Gordian inverse/surrogate/heap partition；Symbolon transform skill；NeuroSCA add-back；Locus predicate proof；模型不可用时确定性退化 |
| W9 | Pangolin 完整抽象与 generator | full matrix、cross-prefix/size、unsigned width relation | sign extension、bitfield pack/unpack、非线性保守抽象、无量词整数 projection artifact；逐 candidate exact validation；真实 cache-hit/yield 实验 |
| W10 | R 级实验与完成审计 | 机制测试和少量公开目标 smoke/ablation | 相同 CPU/corpus/seed；不少于 20 次独立运行或预先论证的功效设计；AUC/最终覆盖/solver CPU/冗余/有效候选；paired bootstrap/randomization、效应量和多重校正；原始 evidence seal |

## 4. 软件工程门禁

每个功能增量按以下次序开发：

1. 写支持域、状态机不变量和反例；
2. 先增加失败测试，再实现最小语义闭合的生产路径；
3. 运行定向测试、相关回归、warnings-as-errors 和静态检查；
4. 第一轮 review 检查语义与错误合并，第二轮检查持久/并发故障，第三轮检查测试 oracle 和声明边界；
5. 更新 `New_Implementation_Archive.md`、技术总账、配置、研究进展和 evidence；
6. 只有 R 级实验通过后才记录覆盖率、速度或漏洞/错误发现提升。

## 5. 第一实施切片：持久多目标 live-state feedback

独立的 `live_state_scheduler.py` 只有进程内状态，直接接入会破坏 F391 的 deterministic restore 和
generation-CAS 语义。第一切片改为扩展生产 `LiveStateSearchPolicy`：

- 多目标策略直接消费 continuation 的路径深度、solver query、未覆盖距离、目标距离和 loop-exit；
- 一次 frontier claim 只推进一次选择状态；
- 完成反馈以整数计数/成本和覆盖增量表示，可在最新 generation 上交换合并；
- selection transition 与 observation transition 分别验证，旧 worker 仍由 lease token 拒绝；
- v1 search snapshot 保持可读，新写入使用版本化扩展 schema；
- 本地 `resume()` 内部选择与持久 frontier claim 选择明确分层，避免重复计算被误报为全局调度历史。

该切片关闭 W1 的生产接线基础，但不提前宣称 Empc/CBC/CGS/TopSeed 已完整复现。

### 5.1 F400 验收结果

该切片已于 2026-08-14 以 F400 落地：selection/observation 转移、成功与失败事务、v1/v2
恢复、并发 rebase、overflow 上界、完整 Python 995/995 和 LLVM 17 259 passed 加 2
unsupported 均有可执行证据。机制基准只报告 pending-state 选择成本，不形成 coverage
或端到端性能结论。详细记录见
[`Persistent_Multi_Objective_Live_State_Feedback_F400_2026-08-14.md`](research-progress/Persistent_Multi_Objective_Live_State_Feedback_F400_2026-08-14.md)。

### 5.2 F401 验收结果

F401 已把 Empc 式 MPC 从完成后 seed/PrefixDAG feedback 推进到真实 pending
continuation：函数内 CFG 经迭代 SCC 折叠，迭代 maximum matching 和有界 edge
exclusion 生成 multiple minimum covers；checkpoint branch token 保守收缩兼容集合，
每个 frontier generation 一次计算未覆盖 suffix；`path-cover` 从本地 `resume()` 到
持久 claim 均生产可达。snapshot v3 绑定两个 MPC 边界并兼容 v1/v2。

验收为定向37 passed加6 subtests、相关199 passed加57 subtests、完整Python
1003/1003加235 subtests、LLVM17 259 passed加2个既有unsupported。机制基准不形成
coverage或speedup结论。详细记录见
[`Persistent_Empc_Live_State_Path_Cover_F401_2026-08-14.md`](research-progress/Persistent_Empc_Live_State_Path_Cover_F401_2026-08-14.md)。

### 5.3 F402 验收结果

F402 以PLDI 2024论文和CBC-SE V2.1作者工件为主源，将compatible branch set从概念审计
落到生产fork路径：迭代data provenance、assume相关性、bitset post-dominator与控制闭包
证明函数内分支独立；达到local/durable frontier压力后，在checkpoint提交前删除组内结果
不一致的child。memory/call/callee/cycle/unknown/collision/budget全部fail-open，snapshot v4
绑定三个边界并兼容v1--v3。

验收为定向42 passed加9 subtests、live/persistent相关132 passed加60 subtests、完整Python
1013/1013加238 subtests、LLVM17 260 passed加2个既有unsupported。508模式穷举oracle
保留全部branch outcomes；11轮synthetic机制实验不形成公开coverage或通用speedup结论。
详细记录见
[`Compatible_Branch_Coverage_Live_State_Pruning_F402_2026-08-14.md`](research-progress/Compatible_Branch_Coverage_Live_State_Pruning_F402_2026-08-14.md)。

### 5.4 F403 验收结果

F403 以ICSE 2024论文与作者KLEE/IDA工件为主源，将具体constraint guidance接到真实
continuation ready-state：函数内固定地址同址同宽`store -> load -> icmp`依赖，具体
branch outcome与latest store marker、保守RHS前瞻、target/ordinary双FIFO、snapshot v5
及lease-conflict按序事件重放形成闭环。symbolic/guarded/overlap/call/歧义/超限全部
fail-open，CGS不剪枝也不决定SAT/UNSAT。

验收为定向47 passed加11 subtests、相关216 passed加65 subtests、完整Python
1025/1025加243 subtests、LLVM17 261 passed加2个既有unsupported。80 case/10200次
独立bit-vector oracle全部通过；11轮synthetic机制实验只证明target首次命中预算变化，
不形成公共coverage或总体speedup结论。详细记录见
[`Concrete_Constraint_Guided_Scheduling_F403_2026-08-15.md`](research-progress/Concrete_Constraint_Guided_Scheduling_F403_2026-08-15.md)。

### 5.5 F404 验收结果

F404 以ICSE 2025论文和Zenodo作者工件为主源，纠正了早期“TopSeed是topological
live-state search”的误读：它是跨symbolic-execution runs的seed selection。生产实现用
AFL bucket-bit exact group承载五维Explore评分，用retained-output inverse rarity与全局
最优一维k=2承载Exploit，并以unique/long/short/random四策略和周期学习更新选择分布。

proposal只有在MPI send成功后才commit；worker-rank/run-token栅栏、program-bound
canonical snapshot、可恢复SplitMix64、orphan失败退休和有界安全驱逐关闭了并行恢复边界。
验收为相关202 passed加91 subtests、完整Python 1041 passed加250 subtests、LLVM17
262 passed加2个既有unsupported；11417次独立oracle全部通过。10000候选机制基准只
证明选择成本和固定权重派发顺序，不形成公共coverage、solver、bug-yield或总体speedup
结论。详细记录见
[`TopSeed_Persistent_Campaign_Seed_Selection_F404_2026-08-15.md`](research-progress/TopSeed_Persistent_Campaign_Seed_Selection_F404_2026-08-15.md)。

### 5.6 F405 验收结果

F405 以arXiv `2407.16827v2`和SANER 2026 POSE为主源，以KLEE对象解析为传统对照，
把`free(select/PHI)`的有限普通heap-base域闭合为排序唯一points-to证书。consumer严格验证
capability、对象归属、候选上界和pointer width；runtime把地址有效性加入路径约束，并在
单一continuation state内条件更新`live/size/init`，不为对象选择增加控制流fork。

LLVM17/18的select/PHI/null/mixed/interior fixture通过；heap/pointer相关17 passed。
独立oracle覆盖domain 1--16、4096个input assignments、34816次live-marker判定、暂停恢复
等价和4类证书篡改。完整Python为1041 passed加250 subtests；LLVM17为264 discovered、
262 passed加2既有unsupported。16候选11轮机制运行产生49 steps、0 forks，中位1.203925470秒；
16→1只表示分析态基数，不形成wall-time speedup。该切片不等价于POSE初始符号堆，下一步
仍需alias-aware survivor initialization与native adapter。详细记录见
[`Certified_Finite_Heap_Lifetime_Union_F405_2026-08-15.md`](research-progress/Certified_Finite_Heap_Lifetime_Union_F405_2026-08-15.md)。

### 5.7 F406 验收结果

F406完成F405遗留的alias-aware survivor initialization切片。旧准入要求一条store覆盖
pointer union的所有候选；新实现保留该路径，再对至少两个普通本地heap objects累计不同的
dominating scalar stores，以allocation identity和静态字节区间证明全覆盖。成功load携带
`initialization_bases`和独立capability，consumer按alias address归属重建owner-base集合并
要求精确相等；runtime的live/init guard保持不变。

LLVM17/18的symbolic-free/survivor正例和partial/nondominating负例通过；独立oracle覆盖
domain 2--32、1--8 byte，共4216次区间检查、10912个victim/survivor赋值及4类制品篡改。
32对象Python参考证明中位3986 ns/proof；992→1是分析态基数，不是墙钟加速。下一缺口是
guard-correlated branch-local store的MemoryPhi/guard-tree证明、producer dominance transcript
和公开heap-heavy等CPU campaign；不外推为完整POSE或一般MemorySSA。详见
[`Collective_Heap_Union_Initialization_F406_2026-08-15.md`](research-progress/Collective_Heap_Union_Initialization_F406_2026-08-15.md)。

### 5.8 F407 验收结果

F407 完成 F406 的 guard-correlated branch-local initialization 缺口。生产者对 merge 的最近
公共支配根枚举完整无环 guard tree，逐路径以 select condition 或 pointer-PHI edge
discriminator 选择唯一 heap object，并要求同路径 singleton store 静态区间覆盖 load。
成功 artifact 携带 root/merge/depth、全部路径决策、base/load address 和 store ordinal 的
proof transcript；consumer 独立重建 CFG、guards、owner、allocation identity 与 store。

F407封存时LLVM17/18的select、PHI、四叶nested、64-path boundary和四类负例通过；F408
复核后重复condition用例由真实MemoryPhi证明接纳，当前保留三类F407 source negative；独立
oracle 覆盖 48 trees、1008 path assignments/interval checks，missing/mismatch/partial 各
1008 组均拒绝，10类 artifact tamper 均拒绝。完整 Python 为1041 passed加250 subtests，
LLVM17为264 passed加2个既有unsupported；64-path Python参考证明中位15214 ns/proof。
下一缺口是一般 LLVM MemorySSA/AA clobber transcript，而不是继续把 guard-tree 切片外推。
详见[`Guard_Correlated_Heap_Union_Initialization_F407_2026-08-16.md`](research-progress/Guard_Correlated_Heap_Union_Initialization_F407_2026-08-16.md)。

### 5.9 F408 验收结果

F408 将 F407 的 MemoryPhi-inspired guard tree 替换为真实 LLVM MemorySSA/AA 有界证明。
新PM先执行mem2reg并失效函数分析缓存，再按函数取得AAManager和MemorySSA。load的MemoryUse
沿MemoryPhi/MemoryDef逆向遍历；全部incoming必须到达完整区间store，无关store仅在静态
对象/区间不交或AA返回NoAlias时跳过，artifact可精确定位的内部直接call仅在ModRef不含Mod
时跳过。多对象pointer union由同一PHI edge discriminator关联memory incoming与base。

producer输出最多128节点、64 incoming、深度8的forward-ID DAG transcript；consumer重建
logical CFG、heap owner、allocation guard、store ordinal、call memory-definition ordinal和
pointer-PHI edge bases。runtime真实live/size/byte-init marker不被证书改写。

双LLVM的switch、single-object、nested、NoModRef call、missing/partial负例和64-way边界通过；
独立oracle接纳504 graphs，执行16632次incoming/interval/NoAlias检查，三类各16632负例和8类
结构变异均拒绝。完整Python为1041 passed加250 subtests，LLVM17为265 passed加2个既有
unsupported。机制成本与64→1只描述Python参考谓词和分析态基数，不形成公开coverage或
端到端speedup结论。详见
[`MemorySSA_AA_Heap_Initialization_F408_2026-08-16.md`](research-progress/MemorySSA_AA_Heap_Initialization_F408_2026-08-16.md)。

### 5.10 F409 验收结果

F409关闭F408的首个跨过程初始化缺口。caller的真实MemoryUse仍是证明起点，只有当线性
MemoryDef链终止于一个可证明的直接内部call时，才实例化callee effect。当前支持两类单块
摘要：返回唯一malloc/calloc对象的allocator wrapper，以及把唯一scalar store写到
`formal_parameter + constant_offset`的参数initializer。后者在当前callsite actual上实例化，
不会把另一个callsite的初始化事实借给当前load。

producer输出caller call、callee allocation/store/return、parameter/actual、heap identity、
load/store interval及skipped defs的版本化transcript。consumer重建两侧操作身份、执行顺序、
唯一writer与半开区间；LLVM负责NoAlias/NoModRef事实，runtime真实live/size/byte-init保持最终
权威。returned finite heap pool按对象出证，并与load、allocator和callee store alias域精确
相等；递归、间接调用、条件/多writer、dynamic interval和异常后置条件均失败关闭。

LLVM17/18正负fixture和64-callsite生产边界通过；独立oracle穷举182196个interval实例，合法
cover 49896个全接受，132300个non-cover和49896个wrong-callsite全拒绝，并拒绝8类artifact
mutation。机制基准中的4096→64仅为关系检查数量；816 ns/certificate与52226581 ns batch只
描述Python参考validator。完整门禁为Python 1046 passed加250 subtests、LLVM17 267 passed
加2个既有unsupported；没有公共coverage、solver
throughput、bug-yield或端到端speedup结论。详见
[`Interprocedural_Heap_Effect_Summary_F409_2026-08-16.md`](research-progress/Interprocedural_Heap_Effect_Summary_F409_2026-08-16.md)。

### 5.11 F410 验收结果

F410关闭F409之后的首个动态interval缺口。符号长度
`memset/memcpy/memmove`从静态object span取得最多64-byte容量，必要时生成unsigned
`length <= capacity` assume，再为每个offset生成`offset < length` guard。copy/move先读取全部
source bytes，随后执行guarded byte writes，因此不按65个可能长度复制continuation state。

对dynamic-index load，producer从真实MemoryUse沿线性MemoryDef链追溯唯一region writer，
枚举最多256个地址和2048个address×lane witness，要求所有lane位于writer半开区间。consumer
重放whole-function bound/guard/read/write schema、read-before-write、CFG dominance、owner、exact
index domain和lane Cartesian product；LLVM负责MemorySSA/AA数学事实，runtime实际执行
`byte_init' = byte_init OR guard`并拒绝未初始化的length/index组合。

LLVM17/18各自通过含22条RUN的fixture，F406--F410关联lit各5/5；独立oracle覆盖48294 cases、
364317 lane checks、102004 runtime guard equivalences并拒绝10类mutation。完整门禁为Python
1051 passed加250 subtests、LLVM17 269 passed加2个既有unsupported。64-byte边界的65个bounded
length values、64个conditional writes和0 state forks是机制基数；10309 ns/certificate只描述
Python reference validator，不是65倍加速，也没有公共coverage、solver throughput、bug-yield或
端到端speedup结论。详见
[`Symbolic_Length_Byte_Lane_Cover_F410_2026-08-16.md`](research-progress/Symbolic_Length_Byte_Lane_Cover_F410_2026-08-16.md)。

### 5.12 F411 验收结果

F411关闭F410显式保留的loop-carried MemoryPhi缺口。producer从dynamic load真实MemoryUse
取得header MemoryPhi，以LoopInfo闭合single preheader、三块loop、single latch/exit；标量
recurrence限制为seed 0、unit step和`ult(iv,input-derived bound)`，并验证bound源位宽能够表示
writer exclusive upper bound。MemoryPhi incoming必须恰为liveOnEntry和唯一byte writer
MemoryDef，loop内其他memory access失败关闭。

writer/load有限域形成`load address x byte lane -> writer address x induction value`唯一证书。
consumer独立重建direct CFG、两个PHI edge-copy、guard、step、bound provenance、store ordinal、
object owner和完整witness笛卡尔积；runtime byte-init bitmap仍决定当前count/index组合是否实际
可读。实现同时修复alias-bearing memory instruction在地址digest变为concrete且落在声明域外
时绕过alias域的错误，one-past迭代现在成为infeasible；域内concrete地址仍保留heap生命周期
与logical-size诊断。

LLVM17/18各自通过15条RUN，含动态i16正例、五类source negative、六类artifact mutation与
64-byte/8-byte生产边界；独立oracle覆盖8019 cases、91773 lane checks、22154 bitmap
equivalences并拒绝12类mutation。完整门禁为Python 1056 passed加250 subtests、LLVM17
271 passed加2个既有unsupported；关联lit在双LLVM各5/5。64 writer aliases、57 load aliases和
456 witnesses是证书基数，51116 ns/certificate仅描述Python reference validator成本；没有
公共coverage、solver throughput、bug-yield或end-to-end speedup结论。详见
[`Loop_MemoryPhi_Byte_Lane_Induction_F411_2026-08-16.md`](research-progress/Loop_MemoryPhi_Byte_Lane_Induction_F411_2026-08-16.md)。

### 5.13 F412 验收结果

F412关闭F411明确保留的第一个非单位affine recurrence缺口。producer保留真实store完整
alias/index域，再按`index mod step == 0`导出recurrence-reachable writer；正向pointer scale与
1--8 byte writer形成`address_stride=step*scale`，并在`writer_bytes <= address_stride`下把每个
load byte映射到唯一writer start/lane/iteration。partial residue只在load alias域完全落入覆盖类时
接纳。bound来源同时满足writer容量和`Bmax <= 2^bits-step`无回绕证明。

v2 artifact绑定完整/可达域、scale、stride、residue与writer-lane；consumer独立重建CFG、PHI
edge-copy、实际pointer_offset、store/load alias、bound provenance和lane笛卡尔积，并严格闭合
基础/扩展capability。runtime byte-init仍是zero-trip/early-exit权威。

LLVM17/18的15-RUN fixture、F411关联回归和实际continuation执行通过；独立oracle覆盖24744
cases、199836 lane checks、80404 bitmap equivalences并拒绝15类mutation。完整Python为1061
passed加250 subtests。64-byte边界含57个store aliases、8个reachable writers、64个covered
bytes和456个witnesses；95151 ns/certificate仅为Python参考validator成本，不形成公共coverage、
solver throughput、bug-yield或端到端speedup结论。single-latch conditional writer缺口已由
F413关闭，该multi-latch SCC fixed-point缺口已由F414关闭。详见
[`Strided_Loop_MemoryPhi_Residue_Cover_F412_2026-08-16.md`](research-progress/Strided_Loop_MemoryPhi_Residue_Cover_F412_2026-08-16.md)。

### 5.14 F413 验收结果

F413关闭F412明确保留的single-latch conditional writer缺口。producer只接受精确五块
`header -> decision -> writer|skip -> latch -> header`拓扑，从真实integer `icmp`和branch
successor恢复predicate、operands与writer true/false polarity。MemorySSA必须形成
`H=phi(liveOnEntry,L)`和`L=phi(writerDef(H),H)`两层版本，loop内其他memory effect失败关闭。

v3 artifact在F412 affine/no-wrap/lane witness上加入完整writer guard transcript和逐lane
guard result。consumer独立重建direct CFG和block dominance，拒绝late arm-defined operand，
并把comparison、edge、两层MemoryPhi、store、pointer_offset、alias/domain和lane笛卡尔积绑定到
实际artifact。runtime byte-init bitmap仍只由真实store更新，guard false、zero-trip、early-exit
和prefix不足保持infeasible。

LLVM17/18的15-RUN focused各1/1、F411--F413关联各3/3；LLVM17完整为275 passed加2既有
unsupported，Python exact identity为1066 passed加250 subtests。独立oracle覆盖126624 cases、
768672 lane checks、551712 runtime bitmap equivalences并拒绝18类mutation。64-byte边界含456
guarded witnesses、2个MemoryPhi和4条incoming；149259 ns/certificate仅为Python参考validator
成本，不形成公共coverage、solver throughput、bug-yield或端到端speedup结论。该下一切片
multi-latch MemoryPhi SCC fixed point已由F414关闭。详见
[`Conditional_Loop_MemoryPhi_Guard_Carry_F413_2026-08-16.md`](research-progress/Conditional_Loop_MemoryPhi_Guard_Carry_F413_2026-08-16.md)。

### 5.15 F414 验收结果

F414关闭F413明确保留的bounded multi-latch缺口。producer从exit load的真实MemoryUse绑定
header MemoryPhi，以LoopInfo和CFG重建2--4条direct latch及最多3层无join decision tree；所有
latch共享seed 0、同一1--64常量step，MemoryPhi incoming逐block分类为direct writer MemoryDef或
同一header MemoryPhi carry。guard-expression域与consumer闭合为bounded参数、非负常量、cast、
binary/icmp、select和defined freeze，load/call、pointer-derived或nondeterministic来源失败关闭。

v4 artifact把`H=phi(E,T0(H),...,Tn(H))`、完整guard path、writer aliases/reachable domain、
逐轮new/total lanes、final lanes、稳定轮以及每个load lane的全部writer alternatives绑定起来。
consumer从lowered program独立重建CFG、dominance、PHI edge-copy、actual comparison/store/
pointer_offset、owner、bound/no-wrap和完整fixed point。各transfer是互斥alternative，绝不顺序
复合；静态闭包只给potential provenance，runtime byte-init bitmap仍是zero-trip、carry、prefix
不足和actual writer choice的最终权威。

LLVM17/18的17-RUN fixture各1/1，F411--F414 Python定向20 passed、关联LLVM各4 passed。独立
oracle枚举42208 cases，接纳6880、拒绝35328，复核256224 lane checks、633552次runtime bitmap
等价并拒绝18类mutation。完整Python为1071 passed加250 subtests，LLVM17为277 passed加2个
既有unsupported。64-byte四transfer边界含456 witnesses、912 alternatives、1个MemoryPhi/
5 incoming和9 rounds；104156 ns/certificate仅为Python reference validator中位成本，不形成
公共coverage、solver throughput、bug-yield或端到端speedup结论。每latch有序multiwriter已由F415
关闭；W1下一组合切片转向nested-loop summary composition、value/last-write、pointer-union与
跨过程循环摘要。
详见
[`Multi_Latch_MemoryPhi_Fixed_Point_F414_2026-08-16.md`](research-progress/Multi_Latch_MemoryPhi_Fixed_Point_F414_2026-08-16.md)。

### 5.16 F415 验收结果

F415关闭F414的每backedge单writer限制。producer从header MemoryPhi的latch incoming沿真实
MemoryDef链逆行到同一header，链内只允许同latch simple store；反转后用LLVM `comesBefore`和
store ordinal重建程序顺序。每transfer允许1--4 writer、全loop最多16，每writer独立验证width、
affine alias、recurrence reachable域与owner，链外MemorySSA effect失败关闭。

v5将不同backedge建模为互斥alternative，将同backedge writer建模为有序sequence；witness新增
writer ordinal，consumer从lowered store列表独立重建顺序、pointer/index域、MemoryPhi incoming
kind、fixed point和完整alternatives。initializedness只使用lane union，runtime path-local byte-init
仍是must-initialize权威；writer ordinal为后续last-write/value与inner-to-outer组合保留接口。

LLVM17/18的9-RUN fixture各1/1，覆盖可区分1/2-byte顺序、multiwriter+singleton混合、5-writer
拒绝及4×4=16 writer边界；关联fixture各5/5，完整Python为1076 passed加250 subtests，LLVM17为
279 passed加2 unsupported。独立oracle枚举13632 cases，接纳3312、拒绝10320，复核72576 lanes、
231282次bitmap等价、718968个last-writer provenance单元并拒绝22类mutation。64-byte最大生产
边界含456 witnesses和7296 alternatives；8-writer参考基准含2964 alternatives，中位211515
ns/certificate，只描述Python参考validator，不形成公共coverage、solver、bug-yield或端到端
speedup结论。下一切片F416为nested-loop MemoryPhi summary composition。详见
[`Ordered_MultiWriter_MemoryPhi_Transfer_F415_2026-08-16.md`](research-progress/Ordered_MultiWriter_MemoryPhi_Transfer_F415_2026-08-16.md)。

### 5.17 F416 验收结果

F416关闭单层loop MemoryPhi证明不能跨嵌套层级组合的缺口。producer从目标load真实MemoryUse
定位outer MemoryPhi，用LoopInfo验证精确两层、各single-header/single-latch的可归约shape；outer
Phi回边必须是inner Phi，inner Phi回边必须是同inner body内1--4个有序simple-store MemoryDef。
两组seed/step/ult/bound、outer-invariant inner bound、whole-domain no-wrap、writer affine alias/
reachable域及全loop MemorySSA effect共同闭合。

v6以`H_outer=phi(entry,S_inner(H_outer))`显式记录inner-to-outer组合。consumer独立重建两层CFG、
四条edge-copy、双recurrence/guard、bound dominance、两个MemoryPhi、实际load/store pointer、
owner/alias/fixed point和完整witness，并拒绝bool-as-int与同步IR/transcript篡改。静态inner closure
不设置init，zero outer trip和inner短prefix继续由runtime path-local bitmap判定。

LLVM17/18的14-RUN fixture各1/1，F411--F416关联各6/6，关联Python30/30；完整Python为1081
passed加250 subtests，LLVM17为281 passed加2 unsupported。独立oracle枚举18240 cases，接纳670、
拒绝17570，复核68160 lanes、21480次bitmap等价、52662个last-writer provenance单元并拒绝26类
mutation。64-byte真实生产边界含2 Phi、4 writers、456 witnesses、855 alternatives和9 rounds；
参考validator中位97062 ns/certificate，不形成公共coverage、solver、bug-yield或端到端speedup
结论。F416是受LoopSCC从内到外方向启发的局部initializedness组合，不是一般LoopSCC复现。下一
切片优先研究value/last-write summary，再扩展outer-dependent二维地址和nested conditional/
multi-latch effect。详见
[`Nested_Loop_MemoryPhi_Summary_Composition_F416_2026-08-16.md`](research-progress/Nested_Loop_MemoryPhi_Summary_Composition_F416_2026-08-16.md)。

### 5.18 F417 验收结果

F417关闭F416只证明initializedness、不能给出重叠store最终值的缺口。producer仅在F416支持域内
全部writer均为完整字节宽度真实`ConstantInt`时升级v7，按LLVM DataLayout记录signed operand、bits和memory
bytes；每个load lane按inner induction降序、同轮writer ordinal降序生成first-match cases，并以
`minimum_inner_bound=k+1`绑定`ult` loop的执行阈值。outer bound必须大于0，无可行case保持
uninitialized。动态writer完整回退v6，不声明值能力；非整字节表示不臆测padding bits并失败关闭。

consumer从lowered真实store ordinal与program endianness独立重算operand/bytes、case order、
threshold和value byte，并闭合F417依赖F416的capability/use-point/dangling合同。runtime尚不跳过
循环，真实store与path-local byte-init bitmap继续裁决zero-trip、短prefix和partial multi-byte load。

LLVM17/18的15-RUN fixture各2/2，覆盖动态值v6回退、非整字节失败关闭与真实大端模块；
F411--F417关联各8/8，关联Python35/35，完整Python为1086 passed加250 subtests。独立oracle枚举3456
cases，接纳496、拒绝2960，复核16416组runtime load-vector和62112个defined value bytes，统计
43212完整与57572 uninitialized/partial loads，并拒绝18类mutation。64-byte边界有4 value
records、15 stored bytes、456 witnesses和855 cases；Python reference validation与first-match
selection中位分别为360090 ns/certificate和97304 ns/query；完整LLVM17为284 passed加2
unsupported。不形成LLVM、solver、coverage、
bug-yield或端到端speedup结论。下一切片转向outer-dependent二维affine address/value，再处理
nested conditional/multi-latch与pointer-union/multi-object。详见
[`Nested_Loop_MemoryPhi_Last_Write_Value_Summary_F417_2026-08-16.md`](research-progress/Nested_Loop_MemoryPhi_Last_Write_Value_Summary_F417_2026-08-16.md)。

### 5.19 F418 验收结果

F418关闭F417 writer address只能依赖inner induction的缺口。producer从真实inner-body binary
definition tree恢复`c+alpha*outer+beta*inner`，只接纳常量、加法和一侧为直接正常量的乘法；两维
bound均从lowering ABI中的窄integer argument恢复完整最大域，每维最多64个实例、笛卡尔积最多256。
每个枚举点必须命中实际alias index/address map，全部index和地址均须无回绕且落在对象内。

v8显式记录二维writer instance、pair fixed point、target-endian常量bytes以及按outer、inner、writer
ordinal倒序排列的last-write cases。每个case同时携带`minimum_outer_bound=o+1`和
`minimum_inner_bound=i+1`；无命中保持uninitialized。consumer独立重建CFG、双归纳/guard、两个
MemoryPhi、实际pointer/store、affine expression、最大域实例、closure、端序和双threshold，动态值、
`outer*inner`、混合direct/2D shape、越界、超预算和同步篡改均失败关闭。review还修复了
`inner_step=1,width=1`但`beta>1`或pointer scale大于1时strided capability漏发的问题。

LLVM17/18的17-RUN focused各3/3，覆盖真实big-endian、24-byte/12-pair生成边界、affine-stride隔离
以及dynamic/non-affine negatives；关联LLVM两版本各6/6、关联Python 15/15，完整Python为1091
passed加250 subtests，LLVM17受控完整为288 passed加2既有unsupported。Python F418为5/5。
独立oracle枚举4608 cases，接纳28、拒绝
4580，复核608组runtime load-vector、3384个defined bytes、1692个完整与5252个partial/
uninitialized loads，并拒绝18类mutation。参考证书含24 instances、12 pairs、46 witnesses和69
cases，Python reference validation/selection中位53528/13745 ns，仅形成机制成本证据，不形成LLVM、
solver、coverage、bug-yield或端到端speedup结论。runtime仍执行真实循环，path-local byte-init bitmap
保持最终权威；一般Polly/ISL、LoopSCC、多分支、多对象、符号value和loop skipping未宣称。详见
[`Nested_Loop_MemoryPhi_Two_Dimensional_Affine_Summary_F418_2026-08-16.md`](research-progress/Nested_Loop_MemoryPhi_Two_Dimensional_Affine_Summary_F418_2026-08-16.md)。

### 5.20 F419 验收结果

F419关闭F418只能给二维地址实例绑定常量writer value的缺口。producer从真实inner-body store
definition tree恢复`c+s_o*outer+s_i*inner+s_x*input mod 2^bits`，只接受同位宽非负常量、双IV、
至多一个入口整数参数、add和直接正常量mul；表达式深度不超过4且必须在store前同块定义。
`nuw/nsw`、sub、多个输入、跨块/过深表达式失败关闭。入口参数同时绑定真实SSA、连续input byte
offset/bytes，避免证书只按参数名关联。

v9对每个F418 writer instance用APInt按位宽特化IV项，保留input scale并按目标端序生成
`extract-affine-bitvector-byte`；常量overlay用`constant-byte`。consumer从lowered SSA和entry
`input -> identity/zext -> shl -> or -> identity`链独立重建公式与ABI，再重算instances、双bound
threshold、last-write顺序和全部byte expressions。runtime仍执行真实循环/store，path-local bitmap
继续裁决zero-trip、短prefix与partial load。

LLVM17/18 focused各3/3、F416--F419关联各9/9；Python F419 6/6、关联21/21，完整门禁为1097
passed加250 subtests；LLVM17八worker完整为292 passed加2既有unsupported。有限oracle枚举1152
cases，接纳6、拒绝1146，复核384组load-vector、1392个defined bytes、696完整和2504 partial/
uninitialized loads，拒绝18类mutation；两次输出SHA-256均为
`fae5d9ccaaa58ed3735bfce1ca83885cb6f4d3b67087e0e3b636e196097dfa04`。参考证书含24 instances、
46 witnesses、69 cases与46 symbolic expressions，reference validation/selection中位43406/10863
ns，仅为Python机制成本，不形成LLVM、solver、coverage、bug-yield或端到端性能结论。详见
[`Nested_Loop_MemoryPhi_Affine_Symbolic_Value_Summary_F419_2026-08-17.md`](research-progress/Nested_Loop_MemoryPhi_Affine_Symbolic_Value_Summary_F419_2026-08-17.md)。

### 5.21 F420 验收结果

F420关闭F419只能为每个二维writer instance给出单一affine value的缺口。producer识别inner body
中实际有序的`icmp -> select -> store`，仅接纳`eq/ne/ugt/uge/ult/ule`、outer/inner IV与直接
非负常量的比较；两个select arm各自使用F419的byte-complete affine bit-vector grammar，且合计至多
共享一个入口输入。对F418有限实例静态求guard、选择唯一arm、按位宽特化IV项并输出target-endian
`guard-specialized-byte`。双臂按`2^bits`归一化后相同则拒绝，避免把i8 `256*x`与`0`等语义相同
公式误作piecewise value。

v10 capability依赖v9及此前全部父能力。consumer从lowered store、actual select/icmp、predicate、
operand方向、双臂SSA和input ABI独立重建，再重算每个instance的guard result、selected arm、byte
expression、双threshold和descending last-write顺序；纯v9证书不能通过schema/capability篡改伪升级。
signed/input-dependent guard、different inputs、nested/cross-block或poison shape失败关闭。runtime仍
执行真实loop/select/store，path-local byte-init bitmap继续裁决zero-trip、短prefix和partial load。

LLVM17/18 focused各3/3、F416--F420关联各12/12；Python F420 6/6、关联27/27，完整门禁为1103
passed加250 subtests；LLVM17完整为296 passed加2既有unsupported。有限oracle覆盖96配置，接纳48、
拒绝48，复核6144组load-vector、58752个defined bytes、29376完整和111936 partial/uninitialized
loads，拒绝22类mutation；两次输出SHA-256均为
`c6d13d7149e38ad67e09726746f466583735d9870c2d6205010d4c7fe64a631b`。参考证书含24 instances、
46 witnesses、69 cases和46 guard-specialized expressions，reference validation/selection中位
50978/10814 ns，仅为Python机制成本，不形成LLVM、solver、coverage、bug-yield或端到端性能结论。
详见
[`Nested_Loop_MemoryPhi_Piecewise_Affine_Value_Summary_F420_2026-08-17.md`](research-progress/Nested_Loop_MemoryPhi_Piecewise_Affine_Value_Summary_F420_2026-08-17.md)。

### 5.22 F421 验收结果

F421关闭F420只能表示单个直接`icmp -> select`的缺口。producer从真实store value递归构造最多31节点、
最多4层guard的后序Decision DAG；guard仍限于outer/inner induction与常量之间的六类equality/unsigned
比较，leaf复用F419 byte-complete affine bit-vector grammar。相同LLVM SSA只编码一次，允许共享leaf与
非叶子子DAG；每个F418有限writer instance沿真实predicate专门化到唯一leaf并生成target-endian byte
expression。v11允许同一ordered transfer混合constant、single-affine、direct-piecewise与DAG writer，
但必须至少存在一个真实DAG，阻止纯v10证书伪升级。

consumer从actual select/icmp、operand方向、block/instruction order、SSA leaf和input ABI独立重建DAG，
要求连续ID、child先于parent、root最后且唯一、全节点可达、无重复operand，并独立复算深度、guard path、
specialized leaf、byte extraction、last-write case和capability闭包。两轮review修复了共享非叶子节点复用时
深度低估，以及v11误拒绝DAG与direct-piecewise混合writer的问题。signed/input-dependent/cross-block
guard、悬空或循环拓扑、超预算和unsupported affine leaf均失败关闭。

LLVM17/18 focused各2/2，F416--F421关联各14/14；Python F421为5/5，F420--F421为11/11。
独立有限oracle枚举1152配置，复核55296组load-vector、608256次scalar load和165888个defined byte，
统计114048完整及494208 partial/uninitialized load，并拒绝6/6 unsupported shape；两次预期JSON摘要为
`235b7fed1b5110e125d56fe04752edd0b92ad3003821cef31b195398a50fcf13`。Python reference的validation、
guard selection、last-write中位成本分别为357、347、9511 ns/次，只描述机制，不构成LLVM、executor、
solver、coverage或端到端speedup结论。runtime仍执行真实循环；F422才按已冻结refinement契约实现
事务型summary-transfer。详见
[`Nested_Loop_MemoryPhi_Affine_Decision_DAG_Value_Summary_F421_2026-08-17.md`](research-progress/Nested_Loop_MemoryPhi_Affine_Decision_DAG_Value_Summary_F421_2026-08-17.md)。

### 5.23 F422 验收结果

F422把F421的v11证明首次接入真实preheader控制流。producer只有在actual stores与summary writers
集合相同、循环effect封闭、固定stack object且没有scalar live-out时，才输出显式opt-in
`loop_summary_transfer`。consumer重新执行v11重建，并独立复核CFG、source load、region opcode、store、
operand live-out、对象范围和完整proof；随后把有限writer instances编译为按outer、inner和writer ordinal
排序的write program。运行时用bound activation ITE更新每个memory byte，以`old_init OR active`更新
initializedness，只在私有candidate memory/value全部成功后提交并跳到exit。

默认模式通过fallback执行真实循环。LiveProgramGraph把fallback与summary target都作为可达边，但不把
configuration gate加入branch decision、CBC、CGS、path-cover或AFL bitmap。真实循环多exit path states
与单一ITE摘要态按concretization集合比较，而非要求solver-root结构相等；zero-trip与partial load保留
definedness域，不能把concrete backing zero解释为确定返回值。

独立源级oracle枚举176个offset/bound配置，复核2,112个memory bytes、2,112个initializedness markers和
33个fully-defined load values，全部等价；143个partial/uninitialized load保持定义域，176次summary均
命中，0 fork，最大27步。LLVM17/18正向和scalar-live-out fail-closed各2/2，Python定向3/3，事务故障
注入、mutation battery、ruff、py_compile与双版本构建通过。该结论仅覆盖当前有界stack-memory
Decision-DAG domain，不外推到一般LoopSCC、heap、solver/coverage或campaign性能。详见
[`Executable_Nested_Loop_Memory_Summary_Transfer_F422_2026-08-17.md`](research-progress/Executable_Nested_Loop_Memory_Summary_Transfer_F422_2026-08-17.md)。

### 5.24 F423 验收结果

F423把F374的`execute_run`/`replay_run`回调合同接入真实`LiveContinuationExecutor`。
`witness-guided`模式在release前只保留一个状态，按witness concrete value选择branch、
throw和indirect-call路线，追加route assertion但强制零fork、零feasibility query。到达唯一
release function或decision site后，保留memory、frames和solver-frame链，恢复普通符号分叉。
terminal checkpoint使用完整QF_BV path condition求解input-byte model，并只写plan-private staging。

coordinator对plan/program/release/resource/candidate身份再做一次严格准入，随后从入口执行
concrete-only replay；只有零fork、零solver并到达真实terminal的candidate才以SHA-256发布到
corpus，coverage从重放实际进入的function和`(branch site,T/F)`重建。定向测试15/15，
关联回归108 passed加36 subtests；独立源级oracle覆盖8个配置，4个到达release并生成8个
replay-equivalent candidate，4个未到release且零candidate，6类mutation全部拒绝。
完整capability-closed Python门禁为1,126 passed加250 subtests，零skip/xfail/xpass/deselection与
node-ID漂移。F423无C/C++变更，本增量未重跑LLVM；F422紧邻封存结果只作历史基线。

该结果关闭W8的“有界continuation真实BSE runner”子项，但不关闭native/public-target adapter、
任意外部效应、KLEE等价性或论文公开性能复现。详见
[`Executable_Agolic_Witness_Guided_BSE_Runner_F423_2026-08-17.md`](research-progress/Executable_Agolic_Witness_Guided_BSE_Runner_F423_2026-08-17.md)。

### 5.25 F424 验收结果

F424关闭了FM 2026 Selective Concolic在当前QF_BV执行链中的两个机制缺口。persistent query
helper按原子约束中的input-byte共现构建有界加权关系图，把高成本BV operator侧作为`PC_r`、
其余作为`PC_c`；先求`PC_c` partial model，再为random-only byte做witness/zero/ff/确定性随机
completion。候选必须在原完整prefix+target solver上返回SAT，并继续通过Query IR验证和concrete
replay。partial UNSAT、unknown、timeout、图超限、紧耦合cut和completion miss都只触发full-Z3
fallback，不能写UNSAT cache或剪枝。

Prefix DAG按同一`(parent,site)`的branch outcomes做Laplace平滑，结合coverage/data/backsolver/
path-cover收益、feasibility和solver cost进行同步value iteration；循环状态以最大迭代轮数和残差
停止，snapshot恢复后重算派生概率和值。四位独立源级oracle枚举65,536个赋值与16个witness，得到
10个完整模型和16/16 candidate hit，false SAT/UNSAT均为0；4-edge/1-cut/1-shared图与解析两状态
固定点一致，17轮残差为`5.54e-13`。完整Python、双LLVM、静态检查和交付门禁数字由F424 evidence
目录封存：完整Python为1,130 passed加253 subtests；LLVM17为304 passed加2个预期unsupported，
LLVM18为305 passed加1个预期unsupported，两套均发现306项且无其他状态。

该工作包达到I/T/E-mechanism，但不升级为R：当前operator-risk分类器不是论文的LIBSVM timeout
predictor，图切分不是METIS，solver不是JFS/KLEE BVFP组合，也未运行535个GSL/Cephes函数、41个
FDLIBM函数或不少于20轮等CPU公开实验。详见
[`Selective_Concolic_Relation_Graph_and_MDP_F424_2026-08-17.md`](research-progress/Selective_Concolic_Relation_Graph_and_MDP_F424_2026-08-17.md)。

### 5.26 F425 验收结果

F425完成W3的第一段native闭环：LLVM17/18原子探针记录group化具体值与post-instruction commit；
schedule-only产物跳过symbolic shadow和LowerAtomicPass，preload仅在真实原子完成后推进prefix。
分析侧枚举有界`rf/mo/sc`等价类并由SC/TSO/RA权威SMT context准入，campaign为每个successor启动
fresh process并由离线verifier重算协议、digest、analysis、parent/query topology与截断状态。

独立source oracle验证SC store-buffering仅3个结果，生产SC为3 SAT/1 UNSAT，TSO/RA均4 SAT；RA
message-passing stale data被拒绝，3写者恰为6个coherence order。LLVM17/18各40次真实回放均40/40
命中期望读值且零fallback/mismatch/conflict；每版campaign均3 runs/3 prefixes/0 invalid并达到有界
fixed point。W3仍未关闭：完整RC11、herd7/diy/GenMC corpus、弱内存RF执行控制、无界证明和公开目标
R级实验保持待实施。详见
[`Native_ConDPOR_C11_Atomic_Reexecution_F425_2026-08-17.md`](research-progress/Native_ConDPOR_C11_Atomic_Reexecution_F425_2026-08-17.md)。

### 5.27 F426 验收结果

F426完成W4的公式上下文阶段。Query IR prefix按assertion编码为parent-linked不可变manifest，摘要
绑定root、规范SMT-LIB term、backend capability、累计/新增offset、depth和累计formula identity。
CAS以hard-link create-once、fsync和稳定no-follow读取发布；worker解析时正向复算整个父链。persistent
backend以terminal digest作为本地LRU key，支持exact local hit、单delta parent extension和fresh
worker cold reconstruction。带单调token/TTL的materialization lease限制同context和store-wide并发，
等待纳入deadline并可取消，quota timeout保证不启动solver。

独立oracle的32条深度1--4链与参考编码器逐字段相同，`false_identity=0`；真实cvc5三次SAT得到
66/67/68，验证同worker parent reuse与另一backend `shared_exact_hit=true/local_hit=false`，且三次均
经QueryStore重新lowering、身份复算和SAT replay提交。symlink、对象/capability篡改、并发发布、旧
token、TTL接管、quota timeout/cancel反例均通过。共享制品只是prefix formula plan，不包含solver
heap或learned clauses；F426没有新增可检查UNSAT proof。W4因此保持进行中，下一阶段是proof-carrying
result receipt/UNSAT checker，再研究可验证lemma传输和多节点R级实验。详见
[`Cross_Worker_Incremental_QFBV_Context_F426_2026-08-17.md`](research-progress/Cross_Worker_Incremental_QFBV_Context_F426_2026-08-17.md)。

### 5.28 F427 验收结果

F427完成W4的可检查结果阶段。backend将主solver SMT2、CPC generator query、Ethos reference、
Query IR lowering certificate、F426 context、capability、generator/checker与完整CPC signature tree
内容身份共同绑定到result key。主solver的UNSAT只触发固定cvc5 1.3.4 safe-mode CPC生成；portable
proof body不保存本机路径，checker在本地加入内容一致的CPC includes和从Query IR新鲜导出的
reference。普通与binary BV literal lowering经SMT AST逐节点验证仅表示不同。

Ethos必须以return code 0、stdout精确`correct\n`、空stderr结束；trust/hole/incomplete、非false
末步、顶层exit/reset/include/reference、reference命令、工具链漂移和CAS篡改全部失败关闭。
proof/receipt CAS在跨进程稳定regular-file `flock`内先检查winner与quota，再create-once发布；reuse
可以跳过主solver和generator，但backend与QueryStore各自从本地Query IR重建并再执行checker。
generator、checker、winner重检与锁等待共享单一deadline；proof-disabled one-shot/persistent路径保持
单次旧lowering，不承担proof成本。

定向pytest为14 passed加7 subtests，关联集为63 passed加32 subtests；capability-closed全量Python为
1167 passed加260 subtests，零skip/xfail/xpass/deselection和node-ID漂移。LLVM17/18均发现311项，
分别为309 passed加2 expected unsupported、310 passed加1 expected unsupported；F427定向lit两版
各1/1。两次真实cvc5/Ethos oracle均为1次checked generation、5/5跨worker reuse、reference/CAS
tamper各1/1拒绝、false authorization为0、唯一356-byte proof/receipt/result key各1。fresh-worker
reuse中位约0.55秒包含store初始化、工具链哈希和两次checker，不形成solver speedup或公开目标性能
结论。

W4下一阶段为可验证learned-clause/SMT lemma依赖闭包与prefix entailment；solver-native snapshot、
CAS跨作业GC和多节点R级实验仍未完成。详见
[`Proof_Carrying_QFBV_UNSAT_Receipts_F427_2026-08-17.md`](research-progress/Proof_Carrying_QFBV_UNSAT_Receipts_F427_2026-08-17.md)。

### 5.29 F428 验收结果

F428完成W4的可验证ancestor-prefix learned literal阶段。persistent backend在SAT后以有界one-shot
cvc5对完整prefix+target公式调用`get-learned-literals`，严格parser只接受精确`sat + one list`；
每个候选`L`必须通过`C_s ∧ ¬L`的safe-mode CPC生成和本地Ethos检查，record绑定source context、
roots/terms、capability、entailment、proof receipt与exchange policy。消费者只查询目标context的
精确祖先，重新构造同一蕴含问题并运行Ethos后才执行`assert(L)`；SAT model replay和F427完整UNSAT
receipt仍是最终结果权威。

QueryStore不仅复证活动lemma，还从当前Query IR重建完整source context并复证backend声称的新发布
record，防止worker伪造研究统计。lookup/checker/injection/extractor/publication共用backend剩余期限，
commit端活动与发布记录共用另一有界期限；候选、proof、policy、source、CAS或sibling篡改均不注入。
完整门禁同时发现并修复F426首次并发SQLite初始化race：稳定inode初始化锁和有界barrier在200轮压力
中全部通过。

定向pytest为7 passed加13 subtests，关联集为70 passed加45 subtests；capability-closed全量Python为
1174 passed加273 subtests且零退化，node-ID SHA-256为
`951248100f507b674c31676d1ccd5e6e3e6a280285c0f0f627dcfca9394c4aad`。LLVM17/18均发现312项，
分别310 passed加2 expected unsupported、311 passed加1 expected unsupported。两次真实oracle均穷举
65,536赋值、得到1条有效lemma、5/5 fresh-worker注入、四类负例全拒绝和0误授权；consumer全协议
中位604,605/578,072 us包含提取、proof与双重checker，不构成speedup结论。

W4下一阶段为context/proof/lemma CAS跨作业引用追踪和有界GC，再研究solver-specific fork-server或
preprocessing/search-state复用；arbitrary clause scope、混合theory与公开多节点R级实验仍未完成。详见
[`Verified_QFBV_Lemma_Exchange_F428_2026-08-17.md`](research-progress/Verified_QFBV_Lemma_Exchange_F428_2026-08-17.md)。

### 5.30 F429 验收结果

F429完成W4的跨作业存储生命周期阶段。统一registry以`(kind,digest)`登记context、proof、receipt和
lemma，精确边为context到parent、receipt到proof、lemma到source context与receipt。active
owner/generation/deadline job refs与`last_seen` grace组成roots，SQLite recursive CTE标记依赖闭包；
共享operation和独占GC稳定锁排除读删竞态，旧store index必须在GC前完成有界全量同步。

sweep只选择没有不可达dependent的节点，按dependent-first执行index-first/file-second幂等删除；对象、
字节和selection/lock time均有预算，dependency cycle和source/target/job/reference任一端点缺失均不删。
publish-before-index与index-before-unlink故障可重试。review修复错误owner release仍删除活动refs的缺陷，
增加proof result mapping双向闭合、非有限时间拒绝及持久`max_jobs` fencing tombstone配额。

定向22 passed，关联52 passed加20 subtests；完整Python1197 passed加273 subtests且零退化，LLVM17/18
分别311+2和312+1/313。两轮独立oracle均在64个DAG/1024节点上零误删、零漏删、零依赖顺序错误；真实
五制品链活动保护5/5、释放删除5/5、旧代拒绝1/1、orphan回收1/1。464704/436942 us包含proof、文件同步
和GC，只是机制成本，不形成solver、coverage或端到端speedup结论。下一阶段是solver-specific
fork-server或可验证preprocessing/search-state复用。详见
[`Lease_Fenced_QFBV_Artifact_Lifecycle_F429_2026-08-17.md`](research-progress/Lease_Fenced_QFBV_Artifact_Lifecycle_F429_2026-08-17.md)。

### 5.31 F430 验收结果

F430完成W4的Linux同机solver-native state阶段。Z3父进程先对规范prefix执行warm `check()`，再为
每个target建立COW child；child临时`push/assert/check/model`并经有界pipe返回，deadline只终止并回收
child，父snapshot generation保持可复用。Python严格复核generation/query/warm/status/model及资源
元数据，任何缺失、回退或不一致都逐出父context；SAT双重复验、UNSAT授权及F426--F429 identity不变。

官方Z3 5.0.0下专项8 passed加5 subtests、相关43 passed加25 subtests，LLVM17/18定向各1/1，
完整Python1205 passed加278 subtests，LLVM17/18完整分别312+2和313+1/314，ASan/UBSan通过。
两轮各128目标与同版本cold Z3对拍均0 mismatch、0 invalid model、128 unique child，
10 ms超时后在同generation恢复；含一次初始化的cold/native总机制成本比为2.252x/2.249x。该数据只
量化固定prefix机制，不构成coverage或公开目标性能结论。F431已经关闭input-byte变量置换
UNSAT-core复用；剩余W4缺口是assumption/increment scoped实时clause proof交换、跨节点可移植state、
跨地域存储与R级多节点实验。详见
[`Native_QFBV_Solver_State_Fork_Reuse_F430_2026-08-17.md`](research-progress/Native_QFBV_Solver_State_Fork_Reuse_F430_2026-08-17.md)。

### 5.32 F431 验收结果

F431以Cache-a-lot为基线，将QF_BV input-byte变量置换UNSAT-core复用接入F426--F430生产链。结构
footprint和1024-bit Bloom仅作候选过滤；逐子句有序AST统一、变量域交集、选择性自然连接和最终
内容摘要重算共同证明`sigma(core) subseteq target`，并允许非单射映射。命名core由cvc5提取后
单独生成CPC并经Ethos检查；worker和QueryStore使用分离的验证cache domain，fresh process仍复核
receipt，热命中也逐目标重做精确置换。

SQLite/CAS、stable no-follow读取、并发发布锁、全流程绝对deadline、双pipe输出界限、取消回收、
配额和`core -> receipt -> proof`生命周期依赖已经生产接线。三轮review删除了错误clause-count过滤，
修复candidate scan饥饿、selector kill、timeout分类和cache-domain独立性。

验收为定向38 passed、关联101 passed加45 subtests、完整Python1221 passed加278 subtests；LLVM17/18
完整分别315+2和316+1/317。两轮512-case独立穷举oracle均294正、218负、97非单射正例、0 mismatch。
64轮热机制总成本比为13.861x（2 clauses）和5.973x（130 clauses），但fresh consumer冷复核均慢于
baseline中位数。该结论只证明支持域内正确性和跨同构目标摊销，不是coverage、defect yield或论文
74%复用率复现。详见
[`Variable_Substitution_UNSAT_Core_Reuse_F431_2026-08-17.md`](research-progress/Variable_Substitution_UNSAT_Core_Reuse_F431_2026-08-17.md)。

### 5.33 F432 验收结果

F432把W4推进到SAT层：稳定Query IR的38种QF_BV运算被确定性编码为LSB-first CNF；每个root使用
activation literal和守卫子句形成formula increment chain，certificate绑定formula、assumption、CNF、
input map、变量域和operator counts。CaDiCaL 3.0.1一次性路径生成ASCII LRAT，原生C/IPASIR路径
缓存exact formula contexts并逐solve设置assumptions。

LRAT中的临时assumption units通过加入否定guard提升为永久公式上的LRUP clause。worker在注入前递归
重放全部proof fragments，QueryStore从落盘IR重做bit-blast并再次复核imports、final clause和result
receipt。proof CAS的import edge形成持久DAG，并作为F429`sat-proof`生命周期边参与dependent-first GC。
作用域严格拒绝变量域/CNF prefix漂移、循环和descendant-to-ancestor import。

验收为定向38 passed加13 subtests、完整Python1236 passed加291 subtests且身份1236/1236。两轮
512-case CaDiCaL oracle均0 mismatch，38-operator matrix与cvc5模型一致，真实LRAT均24 steps/22
propagations。两轮64次cold/native机制实验总成本比为1.755x/1.765x。该结论只覆盖本机exact-context
和solve-boundary同步交换，不是mid-solve实时检查、多节点扩展、coverage或defect yield结论。详见
[`Verified_Incremental_QFBV_SAT_and_Proof_DAG_F432_2026-08-17.md`](research-progress/Verified_Incremental_QFBV_SAT_and_Proof_DAG_F432_2026-08-17.md)。

### 5.34 F433 验收结果

F433把F432的solve-boundary交换推进为project-native mid-solve checked exchange。proof CAS新增
formula-scoped单调事件流；活动session从recent high-water window读取候选，独立重放LRUP/RUP后
才能入有界native queue。CaDiCaL 3.0.1通过IPASIR-UP external-clause callback消费子句，bridge只在
终止`0`被取走后生成绑定solve generation和连续delivery ordinal的ACK。Learner导出的短子句必须
在基础CNF上重新推导RUP hints后才能发布。

QueryStore重做bit-blast、反查event sequence、重算ACK并再次重放proof。deadline/cancel使用原子
termination fence；任一stream异常都停止线程并淘汰可能含未登记子句的context。普通F432 C/IPASIR
路径保持默认，实时模式必须显式配置。

两轮各512-case真实CaDiCaL oracle均0 mismatch、512/512 checked imports delivered并重放，篡改ACK
被拒绝。两轮64-round机制基准均64/64 delivered；checked-import cold总成本为对应native cold的
1.499x/1.488x，idle median为2367/2339 us。该数据是正确性与本地成本证据，不是coverage或多节点
加速结论。剩余W4重点是公开多节点R级实验、ImpCheck原生wire adapter、自适应proof流控、动态资源
重调度和形式化checker。详见
[`Realtime_Checked_QFBV_Proof_Stream_F433_2026-08-18.md`](research-progress/Realtime_Checked_QFBV_Proof_Stream_F433_2026-08-18.md)。

### 5.35 F434 验收结果

F434没有把本机MPI包装成多节点性能结论，而是先关闭实验协议本身的正确性缺口。rank 0广播封闭
配置与角色；所有rank核对逐轮唯一Query IR/CNF和native library身份。MPI shared-memory communicator
证明物理共址；存在多个host domain时必须通过跨host lock/namespace qualification。proof store由
SQLite WAL改为rollback journal加已有stable flock，并以`event_for_record(digest)`精确归属并发
publisher事件。

active模式中，consumer必须由native状态证明`solve_generation=1`且`solving=1`，全体readiness
collective完成后publisher才可提交RUP record。每个consumer必须对当轮publisher record形成精确ACK
集合；rank 0从CAS重新加载并独立重放所有event、record和proof。统计只在同一rank时钟域计算，禁止
跨主机直接相减monotonic timestamps。

5-rank、2 publisher、2 consumer、2-round的preloaded和两次active真实CaDiCaL job共完成24/24
delivery和24/24 root replay；两次active delivery rate均为1.0。8-variable过短active负例在publication
前因solve已结束而非零失败。当前等级I/T/E-local；公开8/32/128 worker、至少20轮等CPU、coverage AUC、
CAS/网络放大和solve-tail仍是P0 R级缺口。详见
[`Qualified_Multirank_Realtime_Proof_Evaluation_F434_2026-08-18.md`](research-progress/Qualified_Multirank_Realtime_Proof_Evaluation_F434_2026-08-18.md)。

### 5.36 F435 验收结果

F435完成W4中自适应proof流控的第一阶段。每个persistent CaDiCaL worker保存一个跨solve controller，
但以stream identity重置有界decision budget。LRUP/RUP checker先给出绑定record/formula/source/event/
proof-cost的授权；固定点controller再根据短clause质量、propagation density、历史delivery yield、
solve age、checker cost、queue pressure和retry输出可回放的admit/defer/reject。当前bridge没有可信LBD，
实现没有以代理值冒充LBD。

deferred queue按retry/event/digest确定性排序并指数退避，native ACK/backpressure/expired对每个admit
exactly-once结清；跨stream前必须零pending。QueryStore重放完整decision/snapshot后，仍从当前Query IR、
CAS和durable event独立复证proof/source/event，启发式不能越过正确性边界。

专项31 passed，完整Python1267 passed加291 subtests且身份闭合，双LLVM各325 discovered且零失败；
测试含256组随机决策回放、预算、并发、abort和resealed tamper。真实CaDiCaL同步突发
oracle独立重复六次，每次静态queue-capacity=2只交付2/8，adaptive交付8/8且零backpressure，即该构造
机制压力下+6、+75 percentage points和4.0x delivery count。数据不构成solver、coverage、defect-yield
或多节点speedup。SAT 2025显示共享单条clause的LBD没有可测选择价值，因此W4不再把LBD列为默认
准入信号；仍需native utilization/activity、ImpCheck/PalRUP互操作、proof-aware worker pairing、
Mallob式动态资源调整、WAN transport、
形式化checker和8/32/128 worker公开目标R级实验。详见
[`Adaptive_Checked_Proof_Admission_F435_2026-08-18.md`](research-progress/Adaptive_Checked_Proof_Admission_F435_2026-08-18.md)。

### 5.37 F436 验收结果

F436关闭“checked delivery被误当成实际效用”的观测缺口。CaDiCaL
`ExternalPropagator`回调维护真实trail上的assignment、decision level、回溯和每条已ACK导入子句的
`satisfied/unassigned`计数；只在首次进入unit或conflict状态时产生receipt。已满足或始终保留两个以上
open literal的导入计为unactivated。每条receipt精确绑定proof record、durable event、native ACK、
solve generation、连续ordinal、clause、source、decision level、unit literal和falsifying witness。

Session先结算ACK再结算activity，并强制`unit + conflict + unactivated = delivered`、连续ordinal、空队列
和native quiescence。QueryStore从当前Query IR重建CNF，重新加载event/proof并重放checker、ACK及
witness；MPI rank 0对每条activity receipt再次独立重放。新增`api_mutex`消除了solve状态检查与
add/assume/observe/reset/enable之间的TOCTOU，同时修复portfolio cancellation hand-off和干净CMake
`check`对schedule runtime的依赖。

验收为F436关联27 passed，完整Python 1274 passed加291 subtests且身份1274/1274；LLVM17为
324 passed加2 expected unsupported，LLVM18为325 passed加1 expected unsupported，均发现326项且
零失败。双轮128-case真实CaDiCaL oracle各完成128/128 delivery、unit receipt、independent replay和
tamper rejection；32-case ASan/UBSan无诊断。两轮本机5-rank active实验累计8/8 delivery、4/8
semantic activation，证明delivery与activation可被分别测量，但不构成唯一因果贡献、solver speedup、
coverage、defect-yield或多节点扩展结论。下一阶段F437将以activation、checker成本和staleness建立
带不确定性的publisher/consumer pairing。详见
[`Native_Clause_Activity_and_Utility_Feedback_F436_2026-08-18.md`](research-progress/Native_Clause_Activity_and_Utility_Feedback_F436_2026-08-18.md)。

### 5.38 F437 验收结果

F437把F436的原生activity观测变成proof-worker配对闭环。控制器按
`publisher x consumer x formula-family`维护持久反馈，其中family是值无关的协议/规模/operator-count
统计签名。评分组合历史reward、Laplace平滑的delivery和双权activity yield、整数不确定性奖励、checker
成本及event staleness；低样本和周期refresh必须探索，只有proof/event检查完成的candidate才允许被
suppress或进入native queue。所有admit以unit、conflict、unactivated、backpressure或expired恰好结算
一次，decision/outcome/snapshot均可独立重放。

QueryStore在完成事务前重建Query IR、bit-blast、proof、event、ACK、activity和pairing，并将无pending
outcome的snapshot与query result原子提交；同ordinal不同摘要拒绝fork，旧snapshot不能回滚状态。
persistent backend启动时恢复对应worker和policy的checkpoint。多rank评测进一步区分opportunity、admit、
delivery和activity，并以相同formula/CNF/library身份执行baseline/treatment paired oracle。

F437关联门禁为68 passed加25 subtests；完整Python为1290 passed加291 subtests且node-id精确匹配。
两个本机5-rank、三轮、双publisher实验共24次机会，处理组抑制4次native导入（16.7%），两组都观测到
12次activation，unactivated由12降到8，fixture activation rate由50%升到60%。aggregate solve time约
-0.41%，两个seed方向不一致，因此不声称speedup、coverage、defect-yield或因果收益。下一阶段F438为
generation-fenced Mallob式资源伸缩，随后是F439 proof wire互操作和8/32/128 worker公开R级实验。详见
[`Utility_Aware_Proof_Worker_Pairing_F437_2026-08-18.md`](research-progress/Utility_Aware_Proof_Worker_Pairing_F437_2026-08-18.md)与
[`SOTA_Gap_Audit_After_F437_2026-08-18.md`](research-progress/SOTA_Gap_Audit_After_F437_2026-08-18.md)。

### 5.39 F438 验收结果

F438把F437的效用反馈推进到资源控制层。通用controller在固定物理slot上维护standby、active和draining
assignment；按backlog、reward、delivery/activation yield、checker和event lag做有界整数分配，以最小
份额、递减边际效用、滞回和busy-worker retention减少资源抖动与无谓排空。

任何assignment变化必须经过prepare、精确lease retire、proof set/cursor drain receipt和commit；
generation/token共同隔离迟到操作。snapshot verifier重算精确worker mapping和事件、租约、证明守恒。
QueryStore提供单调checkpoint、同ordinal fork拒绝、轻量work count和exact query-token裁决；生产服务增加
动态active shard、生命周期flock以及在线process-live lease/重启持久lease的双重恢复语义。

专项门禁为17 passed；完整Python为1307 passed加291 subtests且node-id 1307/1307精确匹配。LLVM17
为326 passed加2 expected unsupported，LLVM18为327 passed加1 expected unsupported，均发现328项且
零失败。两组本机5-rank、8-epoch、3-job机制实验累计38 attach/38 retire、38 durable proofs、31次
stale fence拒绝和145个物理rank操作归属摘要；完整trace逐操作从空controller重放。该结果不包含dynamic MPI spawn、节点故障修复、
公开目标、多节点speedup、coverage或defect-yield结论。

下一阶段按[`F438后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F438_2026-08-18.md)优先实现F439
PalRUP/ImpCheck/LIDRUP proof wire互操作，再进行动态运行时故障恢复和8/32/128 worker公开R级实验。详见
[`F438研究报告`](research-progress/Generation_Fenced_Malleable_Worker_Pool_F438_2026-08-18.md)。

### 5.40 F439 验收结果

F439关闭了内部proof DAG与外部标准wire之间的语义缺口。历史含`palrup`的schema值保持不变以保护内容
哈希，但通过`PROJECT_LRUP_DAG_SCHEMA`明确其只是项目JSON LRUP DAG。导出器递归验证并child-first
展平imports，以目标CNF后的单调全局ID翻译所有hints；同时生成`interaction.icnf`和`proof.lidrup`，固定
使用`lidrup-check 0.0.7 --strict`验证。反向导入器只接受canonical有界子集，并再次运行项目LRUP core。

checker policy绑定binary SHA、版本、源码commit和strict模式；执行内容快照、实时输出限额、timeout进程组
回收和默认receipt重验封闭外部进程边界。配额sidecar以原子staging/hard-link和SQLite事务持久化证据。
CaDiCaL backend首次验证，QueryStore重建计划、重载CAS并第二次运行checker后才提交UNSAT。

专项门禁为13 passed加12 subtests，关联门禁为56 passed加50 subtests。固定官方LIDRUP工具对1条递归
import展平出的2条lemma strict通过；ICNF为64 bytes、proof为110 bytes。真正的PalRUP`a/i/d` signed
base-128 codec也与SAT 2026固定commit的官方converter逐字节一致，oracle片段为3 directives/16 bytes。
当前PalRUP结论仅是fragment syntax interoperability，不是完整`local_check/redistribute/confirm`全局
UNSAT确认；也没有多节点speedup、coverage或defect-yield结论。详见
[`F439研究报告`](research-progress/LIDRUP_PalRUP_Proof_Wire_Interoperability_F439_2026-08-18.md)。

### 5.41 F440 验收结果

F440将F439的PalRUP fragment语法互操作推进为固定官方checker的完整全局确认机制。输入公式和`N`个
fragment先建立不可变快照；随后执行`N`个local checks、`ceil(sqrt(N))²`个方阵redistribute tasks和
`N`个confirm tasks。该`w²`合同来自固定commit的`test_full_run.c`与`pal.sh`，并由官方12-fragment样例
实测验证。

每阶段具备工具内容固定、进程deadline、stdout/stderr上限和聚合bytes预算。全局授权要求hash/proxy/import
连续守恒、精确`N`个`.check_ok`目录及非空规范UNSAT witness；policy、输入bundle、workspace和phase结果
形成canonical receipt，验证默认重跑完整pipeline。

官方`r3unsat_200`结果为12 fragments、8,452,282 input bytes，执行12 local + 16 redistribute + 12
confirm，12/12确认，witness `[3]`，40个stage artifacts共8,644 bytes。该结果是单机共享文件系统上的
官方机制oracle；当前SymCC尚不原生生成PalRUP fragments，也没有跨节点恢复、solver speedup、coverage或
defect-yield结论。详见
[`F440研究报告`](research-progress/PalRUP_Global_Confirmation_Pipeline_F440_2026-08-18.md)。

### 5.42 F441 验收结果

F441关闭了F438遗留的物理MPI进程失效恢复缺口。新controller将稳定endpoint/incarnation与可变化的
communicator rank分离，以generation、shard、lease三级token栅栏迟到消息；checkpoint cursor单调，
completion chain和strict receipt共同证明成员、分片、在途任务和恢复队列守恒。

live adapter执行`Revoke → Shrink → 两阶段有界Iallgather身份认证 → Agree → commit(g+1)`。
失效owner的shard以rendezvous hashing重分配；由于revoke后无法区分旧路由上的未提交结果，全部在途
lease保守重排并优先重放。固定Open MPI 5.0.10以`--with-ft=ulfm --enable-mpi-ext=ftmpi`构建，
并对公共MPIX符号和真实调用做资格验证。

专项门禁为11 passed。12-endpoint/96-shard oracle在2个endpoint失效后重分配16 shards，将24个在途
lease全部重排、重放并拒绝全部24个旧token。4-rank capability probe六项语义全部通过；本机TCP
transport真实rank退出后world由4降为3，一次repair，3个survivor产生相同sealed receipt。

该验收等级为I/T/E-local：恢复substrate及真实本机故障闭环可独立复验，但尚未接入主执行器热路径和
durable QueryStore，也没有跨节点MTTR、solver speedup、coverage或defect-yield结论。按用户收口要求，
F441完成完整回归与证据封存后暂停后续SOTA功能实现。详见
[`F441研究报告`](research-progress/Generation_Fenced_ULFM_Recovery_F441_2026-08-19.md)。

### 5.43 F442 验收结果

F442建立了基于实测service demand、串行比例、共享资源饱和和恢复成本的并行规模模型，并把模型用于
hybrid master瓶颈定位。实现同时收紧result triage热路径，并将Adaptive内部profile与
Prefix-DAG/ColorGo MDP更新改为有预算刷新。模型给出决策区间而非伪精确的单点上限；结论、参数与
实验边界见
[`F442研究报告`](research-progress/Parallel_Scaling_Model_and_Bounded_Prefix_MDP_F442_2026-08-20.md)。

### 5.44 F443 验收结果

F443关闭F441“独立substrate尚未进入生产执行器”的缺口。standalone master在发送前持久化
generation/shard/lease envelope，worker逐字回显，master将其与transport assignment和共享WAL
token联合裁决。QueryStore以连续state ordinal记录dispatch、finish、cancel、prepare和commit；恢复
期间同ordinal同摘要幂等，不同摘要拒绝fork。

真实失效由数据面MPI异常或低频`Ack_failed/Get_failed`发现，随后执行
`Revoke -> durable prepare -> Shrink -> endpoint attestation -> Agree -> durable commit(g+1)`。
修复后的rank 0接管master，先重做committing manifest，再零TTL回收pre-commit lease；原master退出
同样可由原worker晋升接管。默认非ULFM路径不变，不合格运行时失败关闭，multi-master修复显式拒绝。

专项选择性回归为20 passed加7 subtests，最终完整Python为1353 passed加310 subtests。固定
Open MPI 5.0.10 ULFM的两场物理失效门禁中，3-rank
worker失效场完成32项工作并形成162个公共对象；4-rank原master失效场完成32项工作并形成161个公共
对象。两场均generation=1、单一receipt、最终recovery queue=0且active lease=0；999个证据文件
SHA-256复核零不一致。该结果是本机TCP transport机制结论，不声称多节点MTTR、吞吐、coverage或
defect-yield提升。详见
[`F443研究报告`](research-progress/Production_ULFM_Hot_Path_Recovery_F443_2026-08-25.md)。

### 5.45 F446 验收结果

F446把F443的单master缩容恢复扩展为弹性multi-master与预启动warm spare。运行时分离全局恢复
communicator和每代group/master communicator，以stable endpoint而不是瞬时rank持久标识成员；每次
恢复都封存旧layout、推进generation并确定性重组。高rank spare可在成员退出后晋升，避免依赖故障后
不稳定的dynamic spawn。最终统计从共享WAL done manifests重新聚合，跨代已完成工作不会因原master
退出而丢失。

同机四个真实物理失效场景覆盖multi-worker、original-root、warm-spare和连续双故障；10-rank连续
场景完成两次communicator replacement，rank 8/9依次晋升并保持8个active endpoint。该证据证明
failure fencing、补员和跨代守恒机制，不证明跨主机性能或通用MTTR。详见
[`F446研究报告`](research-progress/Elastic_Multi_Master_ULFM_F446_2026-08-25.md)。

### 5.46 F447 验收结果

F447将连续ULFM恢复推进到双主机。survivor在`Revoke/Shrink`后通过共同communicator上的固定宽度
身份allgather和`Agree`形成一致missing set；fresh-context二次attestation吸收恢复窗口中的竞态故障。
每个master/generation使用隔离QueryStore root，`replay_commit_once`在单条WAL锁内完成manifest验证、
corpus发布和done转换，失败则保留committing供下一master幂等重放。科研协议在每个实验cell前重新
固定代码、submodule、工具环境和输入身份，拒绝跨版本混合封存。

双主机10-rank门禁在rank 2/3连续真实退出后完成两次恢复，晋升8/9，generation 2仍保持8 active、
2 masters/6 workers和2 surviving hosts；820个corpus对象及2491个证据文件通过摘要复核。提前停止的
规模矩阵保留35个外层cell，9个三档均成功配对区组显示32/128 worker相对8 worker的unique/s变化为
+17.63%/+29.29%，但endpoint edges与保留率下降，且128 worker有2/11内部失败。因此单调coverage
ceiling拟合被拒绝，结论保持I/T/E-crosshost + engineering-scale。详见
[`F447研究报告`](research-progress/Cross_Node_Continuous_ULFM_and_Scale_Evidence_F447_2026-08-25.md)。

### 5.47 F448 验收结果

F448为单个困难QF_BV查询建立proof-prefix引导但正确性不依赖启发式的分区证书。从根cube执行
`K-1`次完整二叉切分，支持任意`K<=4096`；证书绑定Query IR bit-blast formula、CNF、base
assumptions、ranking policy、split transcript和全部leaf assumptions。独立verifier重算完整性、
互斥性、排序和内容身份，activity只影响效率，不进入覆盖正确性根。partition artifact同时进入CAS、
生命周期、QueryStore、service inventory和dependent-first GC。

专项/生命周期门禁51项通过；完整Python为1396 passed加310 subtests，1396/1396身份精确且16项能力
零缺失。10轮oracle覆盖1/3/5/8/32/128/512 cubes并枚举验证不重不漏；512-cube构建并内置重放中位
139.881 ms，独立重放68.033 ms。F448只证明分区生成与认证，执行闭环由F449完成。详见
[`F448研究报告`](research-progress/Proof_Prefix_Guided_Certified_Partitioning_F448_2026-08-25.md)。

### 5.48 F449 验收结果

F449把F448证书转化为可恢复的并行执行ledger。每个cube由SQLite保存owner/token/expiry fenced lease、
heartbeat、retry和crash reclaim；每个slot使用独立CaDiCaL context并只安装solve-local assumptions。
SAT winner经过cube polarity与Query IR重放后原子抢先并取消peer；全叶UNSAT则逐份重放signed receipt，
沿F448 split tree反向resolution，形成base-query LRUP receipt。QueryStore父租约续期、执行生命周期、
proof wire和GC均进入同一事务边界。

专项22 passed，耦合回归143 passed加50 subtests，完整门禁1419 passed加310 subtests且nodeid与16项
能力完全闭合。2/4/8/16/32/64 cubes各5轮oracle中，64个UNSAT叶经63步聚合；SAT首胜只完成1个cube
并取消63个peer。该版本仍是同进程多backend slot，跨节点worker映射列为F451。详见
[`F449研究报告`](research-progress/Proof_Aware_Certified_Partition_Execution_F449_2026-08-25.md)。

### 5.49 F450 验收结果

F450消除F449证明聚合中的重复project-LRUP DAG授权。缓存键绑定同一不可变`BitBlastPlan`对象、record
digest和独立cache policy；entry保存weakref、proof-relevant plan scope、immutable authorization及
完整传递import closure的`(CAS digest, encoded SHA-256)`。hot hit仍先规范化根record，再以一次store
flock和一个SQLite connection批量见证全部依赖的canonical path、indexed size、stable identity和字节
摘要；只有闭包全部通过后才计hit并返回。双预算LRU、非规范plan bypass、失败驱逐和独立遥测封闭资源
与可信边界。

五轮审查依次修复传递CAS篡改、persistent policy漂移、逐对象attestation无收益、可变plan/id复用和
hit计数线性化错误。专项9 passed，耦合回归88 passed加50 subtests，完整门禁1428 passed加310
subtests且16/16能力存在。10轮8/32/64层机制oracle的disabled/hot中位数分别为
35.655/9.140、148.042/10.410、279.206/10.997 ms，即3.900985x、14.221134x、25.389288x；hot path
仍见证完整依赖闭包。该倍率只属于proof replay authorization，不是solver或coverage提升。下一项F451
按审计顺序把F449 cube ledger接入F447跨节点stable endpoint与generation recovery。详见
[`F450研究报告`](research-progress/Closure_Bound_Proof_Replay_Cache_F450_2026-08-25.md)及
[`F450后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F450_2026-08-25.md)。

### 5.50 F451 验收结果

F451把F449认证cube ledger与F447的stable endpoint、generation、shard和work fence连成
双层current协议。每个work identity精确编码execution与cube ordinal；heartbeat/result先验外层
ULFM fence，再续租或重放内层SAT/UNSAT证据。故障恢复将全部ambiguous in-flight work
换generation和token重排，包括survivor-owned任务；基础设施恢复不增加semantic solve attempt。

绑定ledger使用stable no-follow初始化锁、SQLite FULL同步和inner/outer lease唯一约束。
`resume_recovery_queue`和`reconcile`分别关闭generation receipt后崩溃、dispatch/binding及
inner-result/outer-finish两个跨ledger窗口；stale multi-master由QueryStore连续state ordinal失败
关闭。无状态CLI每次从持久工件重建协议，SAT还必须通过parent Query IR二次验证。

专项15 passed，耦合回归127 passed加38 subtests，完整门禁1443 passed加310 subtests，
1443/1443 nodeid精确且16/16能力存在。5轮8-cube逻辑oracle的10个recovery case恢复40/40
在途cube、丢失0；3轮物理双主机adapter均在远端exit 86后generation 0→1，重排3/3
在途cube，接受survivor SAT并取消2/2 peers。这证明跨节点应用协议与收敛机制，不是新的
MPI、solver、coverage或defect-yield结论。

下一项F452按审计顺序实现TACAS 2026风格native checker clause compression。详见
[`F451研究报告`](research-progress/Generation_Fenced_Distributed_Cube_Execution_F451_2026-08-26.md)及
[`F451后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F451_2026-08-26.md)。

### 5.51 F452 验收结果

F452把实时 checked-import 的完整 `int32` 子句向量替换为原生规范压缩对象。编码保持 TACAS 2026
ImpCheck 主线：signed-to-unsigned 顺序、排序、差分、canonical 7-bit varint、总长度前缀和 7-byte
inline；本项目进一步采用显式数组/owned heap、只读输入、有界 decoder 和稳定 C ABI。CaDiCaL 3.0.1
callback 流式解码，只有完整消费才生成 ACK；activity tracker 也保存压缩对象。

portfolio 可显式要求 `symcc-qfbv-native-clause-compression-v1`，安装脚本用动态符号门防止部分 ABI。
七项 native telemetry 经 session 差分进入 QueryStore，并要求 inline+heap 守恒、failure 为零。专项
23 passed、耦合37 passed；完整门禁1467 passed加310 subtests，新增24 nodeid且删除0。

20k property oracle 的混合分布中，3/8/32/256 literals payload 分别减少24.3%/38.6%/48.4%/52.1%，
但1 literal增加11.4%；真实CaDiCaL两条子句152 B降至40 B并2/2 ACK。该结果是checker codec
机制证据，不是RSS、solver或coverage提升。下一项F453是online activity/cost-guided cubing。详见
[`F452研究报告`](research-progress/Native_Checker_Clause_Compression_F452_2026-08-26.md)及
[`F452后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F452_2026-08-26.md)。

### 5.52 F453 验收结果

F453把F436 checked activity、F448认证分区和F449 verified outcome/cost闭合为query-family持久在线
策略。三arm为static/activity/cost；cost arm在规范候选cube数间独立探索。每次decision保存固定policy
身份、arm/cube/联合propensity与选择来源；并发pending进入warm-up平衡，重启后继续同一历史。无native
activity能力确定性回退static。

guided prerun只接受ACK配对并可由F448重放的activity；预运行SAT/UNSAT只有通过candidate/proof独立
验证才可提前结束。策略不进入分区完整性、互斥性或SAT/UNSAT正确性根。配置CPU-ms上限固定为base
cube数、base timeout和最大attempts的乘积；prerun从中扣除，其余按chosen cube数和attempts归一化。

专项与partition组合37 passed；耦合136 passed加25 subtests；完整门禁1482 passed加310 subtests，
1482/1482 nodeid精确且16项能力零缺失。最终15次机制oracle中三arm各5次、同为
8000 CPU-ms配置上限；static/activity/cost总时间中位为348.016/1813.961/1742.517 ms。cost探索
4/8/16/2后下一次继续选2，但该小型矛盾fixture上guided明显慢于static。此负结果支持门控与fallback，
不支持solver speedup、coverage或defect-yield外推。

下一项F454按审计顺序实现solver-native PalRUP producer。详见
[`F453研究报告`](research-progress/Online_Activity_Cost_Guided_Cubing_F453_2026-08-26.md)及
[`F453后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F453_2026-08-26.md)。

### 5.53 F454 验收结果

F454关闭F440“官方PalRUP consumer存在但无原生producer”的缺口。固定Mallob CaDiCaL fork在隔离
helper内创建1/2/4个solver rank，每个rank通过原生Tracer写独立PalRUP fragment；有界ClauseBus把
规范化短学习子句fan-out到其他rank的LearnSource，并记录export/deliver/import/drop/pending守恒。
parent三方核对公式、DIMACS header与worker统计，随后执行官方local-check、redistribute、confirm，
只有成功才以fsync和RENAME_NOREPLACE发布，并从发布目录再次官方recheck。

首轮真实confirm暴露communication clause排序前提；对照Mallob路径后，bus改为数值排序并拒绝零、
INT_MIN、重复和重言式。后续审查又修复实际并行上限、witness race的非确定性边界、bool计数、重封元
数据、native crash staging、跨文件系统wrapper替换和公式统计绑定。

专项13 passed加7 subtests，F439/F440/F454耦合34 passed加26 subtests，完整门禁1496 passed加
317 subtests、1496/1496 node ID且16项能力零缺失；strict lint、shell syntax、GCC Werror及wrapper
边界ASan/UBSan通过。pigeonhole(9,8)的1/2/4 rank各3轮均官方复核，2/4 rank
import中位为53,695/135,504条且主实验fan-out守恒、drop为零；join后pending中位为0/1,002/10,192，
最大10,441并被显式记录。pool中位约0.264/0.224/0.273秒，端到端中位3.218/6.974/9.760秒，
明确表明
proof/check成本随rank增长。结论等级
I/T/E-mechanism，不外推solver speedup、coverage、defect yield或多节点扩展。

下一项按审计顺序为F455结构化LLM concolic闭环，其后F456 POSE heap；公开长时等CPU实验独立归
R-track。详见
[`F454研究报告`](research-progress/Solver_Native_Clause_Sharing_PalRUP_Production_F454_2026-08-26.md)及
[`F454后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F454_2026-08-26.md)。

### 5.54 F455 验收结果

F455把原有松散agent hook收束为结构化、反应式、可回放的concolic控制闭环。worker遥测必须先经过
master triage，得到合并后全局AFL coverage delta；默认只在solver UNKNOWN/Z3 timeout、Backsolver验证
失败或回退、符号分支零候选、持久零覆盖plateau时触发。productive run重置plateau并抑制调用。

请求绑定固定provider/model/prompt/transport和完整policy，严格echo request/task摘要；unknown/duplicate/
nonfinite字段、非规范hex和越界action整份失败关闭。portfolio共享请求级token/time reservation和跨重启
campaign预算；command/HTTP解析前有1 MiB流式上限。schedule按源摘要一次性使用；candidate进入
VerifiedProposal、真实目标/分支、可选parser及AFL novelty通道。候选后继只有登记摘要才能把真实结果
写回原episode。canonical hash-chain ledger恢复预算、plateau、iteration、decision和history。

online/shadow/fallback共享除mode外完全相同的base policy，并要求精确任务多重集合相等。专项为
28 passed加6 subtests，含MPI triage的耦合回归为88 passed加43 subtests。固定backend oracle中每臂
4次门控均为trigger 3/suppress 1；online/shadow各3个有效响应，fallback零调用；online准入3候选，
shadow记录3但准入0。完整门禁1517 passed加323 subtests、身份1517/1517且16项能力零缺失。该结果
等级为I/T/E-mechanism，不代表真实LLM coverage或speedup。

下一项为F456完整POSE风格initial symbolic heap；公开结构化目标上的真实模型长时等CPU实验独立进入
R-track。详见
[`F455研究报告`](research-progress/Structured_Agentic_Concolic_Closed_Loop_F455_2026-08-26.md)及
[`F455后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F455_2026-08-26.md)。

### 5.55 F456 验收结果

F456新增有界POSE-C initial symbolic heap域。入口root和64-bit reference field具有稳定引用身份，首次
解引用创建proxy object并加入非空约束；同类型对象的field/byte选择编码为嵌套ITE。load/store/free分别
做条件读取、alias-wide条件更新和live条件清除，不创建continuation child；只有真实CFG reference
comparison通过`branch_alias`分叉。alias quotient、disequality、bounds、initialized和live保持一致。

完整状态采用canonical JSON/SHA-256 term DAG，固定type/reference/object/term/cell/condition预算；严格
loader拒绝identity、DAG、width、ordinal、relation、metric和budget异常。确定性QF_BV SMT-LIB2已由真实
Z3验收；solver model还要经过引用/标量域、quotient和全部guard验证，才合并proxy并物化concrete graph。
快照作为live expression嵌入`LiveStateStore`，保留parent lineage且相同输入产生相同checkpoint。

定向为25 passed加256 subtests；distributed-state/continuation/frontier/cross-worker耦合为232 passed加
282 subtests；完整门禁1542 passed加579 subtests、身份1542/1542且16项能力零缺失。独立oracle 9/9：三引用read/store各27/27 assignment等价；本地swap/sum/list-max10为
2/1/12 CFG paths且heap paths均0。11轮1/2/4/8/16引用机制中位为55.854/121.483/273.663/776.911/
2662.702 us；16引用snapshot 85,960 B、289 terms，同时解析表示82,864,869,804种alias/null partition。
计数是关系空间而非lazy trace，时间是Python机制成本，不外推coverage、solver或端到端speedup。

F456关闭当前已登记的最后一个运行时SOTA功能缺口。下一阶段按审计进入F455/F456公共目标长时等CPU
R-track和自动LLVM/C heap前端，而不是继续添加未经证据支持的heuristic名称。详见
[`F456研究报告`](research-progress/POSE_C_Initial_Symbolic_Heap_F456_2026-08-26.md)及
[`F456后SOTA审计`](research-progress/SOTA_Gap_Audit_After_F456_2026-08-26.md)。
