# F421: 可执行循环摘要的精化契约与实现前置审计

日期：2026-08-17  
状态：语义契约冻结；F421 Decision DAG 与 F422 summary-transfer 已按本契约实现  
研究定位：从“load 处的初始化证明记录”升级到“可替换真实循环的、可独立复核的状态变换”

## 1. 审计结论

F416--F420 已经能为一个受限的两层循环证明迭代域、writer 地址、writer 顺序、last-write case
和最终 byte value。然而 v10 transcript 只附着在 outer exit 的 `load` 上。executor 到达该 load 时，
真实 outer/inner loop 已经执行完毕，因此 v10 当前是严格的初始化证明，而不是循环加速器。

把 v10 直接在 load handler 中“执行”会重复写内存；把 load handler 攓成直接返回 witness 值也不能
消除循环路径、store 和 solver 成本。真正的可执行摘要必须在 outer preheader edge 上执行一次原子
状态变换，并直接转移到 outer exit。该改变要求比 F420 更强的 refinement proof。

## 2. 精化目标

符号 bound 会让真实执行在循环 guard 处产生多个路径态，因此不能假设从 preheader 只到达一个
`SL`。设 `Loop(S0)={SL_1,...,SL_n}` 是真实循环的全部正常出口态，摘要变换为单个含 ITE 的合并态
`T(S0)`。接纳条件是其**指称集合**等价：

```text
for every admitted input and path state S0:
    concretizations(observable(T(S0)))
      == union_i concretizations(observable(SL_i))
```

这不是 checkpoint、表达式摘要或 solver-root 的结构相等，也不是要求合并态与某一条分支态一一对应。
真实循环把 bound 条件分散在多个 path-condition frame 中；F422 用 bound activation ITE 把同一状态集
合并到一个 memory/initializedness 表达式中。验证必须按有限 valuation 比较内存与定义域，并对完全
初始化的 load 比较值，不能把某个 concrete annotation 当成未初始化表达式的模型。

本阶段的 observable 至少包含：

- 所有 continuation 可寻址内存字节及其 initialized/poison 状态；
- exit 后可读取的 scalar/pointer SSA live-out 及其 definedness；
- 正常返回、异常、halt 和外部调用等控制效应；
- solver path condition 的逻辑可满足集合与执行错误；
- concrete replay 所需的分支/coverage 事实不由摘要伪造，仍由真实重放产生。

只有内存最终值相同并不足够。例如漏掉一个 loop-defined scalar live-out、volatile store、可能抛出的
call 或 poison-producing operation，都会破坏 refinement。

## 3. Producer 必须给出的证明

### 3.1 CFG 与迭代域

- canonical two-level loop，outer/inner 均有唯一 preheader、header、latch 和正常 exit；
- induction 从零开始、正步长、unsigned `< bound`，边界来源及位宽满足无回绕证明；
- preheader edge 和 exit block 由实际 lowered CFG 名称绑定，不能由 consumer 猜测；
- writer instance 总数、Decision DAG 节点数和生成 byte 数均有硬预算。

### 3.2 完整副作用闭包

- 循环内所有 store 都必须出现在 ordered writer transfer 中；
- 拒绝 volatile/atomic/fence、call/invoke/callbr、异常边、indirect branch、inline asm；
- 拒绝未被摘要建模的 load、memory intrinsic、allocation/free 和其他写内存指令；
- 纯整数/GEP/select/icmp/PHI/branch 仅在其 poison 与溢出前提已被证明时接纳；
- writer 对象、地址域、别名、端序、宽度和真实程序顺序必须完整封闭。

### 3.3 Live-out 闭包

第一版可执行 transfer 采用保守的 **memory-only live-out** 契约：除 loop control、writer address/value
内部值外，循环中定义的 SSA 值不得被 outer loop 外使用。若 exit PHI 或其他指令使用 loop-defined
scalar/pointer，producer 必须拒绝生成 executable capability。后续若支持 scalar recurrence summary，
应显式列出每个 live-out 的闭式表达式和 definedness，而不能默认沿用跳过前的旧值。

### 3.4 Coverage 边界

summary-transfer 是 symbolic continuation 的内部优化；它不声称已执行被跳过的 LLVM branch，也不直接
把摘要推导出的边写入 AFL bitmap。候选输入仍需 concrete replay 才能贡献真实 coverage。调试或要求
逐指令 trace 的模式必须关闭 transfer，保留原循环执行。

## 4. Strict consumer 的独立证明

consumer 不能信任 producer 的 `executable=true`。在 `create()` 阶段它必须：

1. 从 lowered blocks 重建 preheader/header/body/latch/exit 关系和 PHI edge assignments；
2. 从实际 store、GEP、select/icmp 和 arithmetic definitions 重建每个 writer 与 Decision DAG；
3. 复算有限 `(outer, inner)` instance、地址、last-write 顺序、端序 byte expression；
4. 复核 effect whitelist 和 memory-only live-out；
5. 检查 capability 依赖、唯一 use point、预算以及无悬空 transfer；
6. 将规范化后的不可变 transfer 安装到 preheader edge，运行时不再解析原始 JSON。

任一字段、IR 定义或 capability 不一致都在 executor 创建前失败，不能在运行中部分执行后回退。

## 5. Runtime 事务语义

summary-transfer 对单个 `_ExecutionState` 采用 prepare/commit 两阶段：

```text
prepare:
    evaluate concrete/symbolic bounds and all selected byte expressions
    construct a private candidate memory root and initialization delta
    check every address, width and expression

commit:
    publish candidate memory root and initializedness root together
    set PC to the unique outer exit
```

prepare 阶段的任何失败都不得改变原 state。正常零次 outer 或 inner iteration 不写任何字节；如果这会
使 exit load 未初始化，summary 必须保留 initializedness=false 的定义域语义，由后续 load 加入相同的
definedness 约束或判为 infeasible，不能把底层 concrete zero 宣称为程序返回值。多 writer 重叠时按
`outer -> inner -> writer ordinal -> byte lane` 的程序顺序写入，最终状态与 last writer 一致。

## 6. Decision DAG 前置升级

F420 只接纳一个 direct `select(icmp(IV,C), affine_true, affine_false)`。可执行摘要需要表达常见的多 guard
值逻辑，因此先升级为有界 Decision DAG：

- guard 节点精确绑定一个实际 `select` 和一个实际 unsigned/equality `icmp`；
- leaf 是 F419 affine bit-vector expression；
- 节点按拓扑序编号，child id 必须小于 parent id，root 唯一且所有节点从 root 可达；
- 相同 LLVM SSA 子表达式只编码一次，允许共享 leaf/子树；
- 禁止循环、悬空节点、不可达节点、跨 block value 和超过预算的 DAG；
- consumer 从 lowered definitions 重建 DAG，并对每个静态 writer instance 专门化到唯一 leaf。

该 DAG 仍只允许依赖 outer/inner IV 的常量 guard。input-dependent guard 需要保留符号 ITE 或拆分状态，
属于后续更一般的 guarded transformer，不在本次证明边界内。

## 7. 分阶段交付门禁

1. **F421 Decision DAG**：v11 schema、producer/consumer 双端重建、大小端、共享节点、深度/节点预算、
   mutation battery、独立有限 oracle；仍执行真实循环。
2. **F422 executable transfer**：新的 operation/capability、effect/live-out proof、事务内存变换、显式
   enable/disable、零次迭代与错误等价、原循环对照 oracle。
3. **F423 refinement validation**：随机小域 differential execution、LLVM 17/18、完整 Python/LLVM 门禁，
   再报告迭代数、执行指令数、solver query 和 wall time；在此之前不宣称端到端 speedup。

## 8. 已知边界

该契约不是一般 LoopSCC 复现：它先处理有限、规则、可完全枚举的两层 memory transformer。一般
multi-exit、data-dependent trip count、跨迭代 load-store recurrence、scalar live-out、异常与外部效应仍需
后续摘要语言。其价值在于先建立一个小而可证明的 executable core，使后续扩展可以保持同一 refinement
接口，而不是把不完整分析直接接入运行时。
