# F422：可执行 Nested-Loop Memory Summary Transfer

日期：2026-08-17  
状态：实现完成；机制级有限域差分、LLVM 17/18 交叉验证与三轮 review 通过  
定位：把 F416--F421 的证明型 MemoryPhi transcript 首次接入可执行状态变换，受限地跳过真实两层循环

## 1. 研究问题

F416--F421 已能从 LLVM LoopInfo、MemorySSA、AA 和 lowered SSA 中恢复两层循环的有限迭代域、
二维地址、ordered writers、符号 affine value 与多 guard Decision DAG，并在 exit load 上记录 byte-lane
last-write 证明。但此前 executor 仍逐次执行 loop guard、body 和 store；transcript 只证明 load 的
initializedness/value 来源，并不减少路径分叉或指令数。

F422 解决的核心问题是：在不信任 producer metadata、不伪造 coverage、不遗漏 LLVM 副作用和
initializedness 的条件下，能否把一个真实循环替换成一次原子的符号内存变换？本实现选择一个小而
可验证的 production domain，而不是把不完整的一般 LoopSCC 分析直接接入运行时。

## 2. 接纳域与失败关闭边界

producer 只有同时满足以下条件才生成 executable transfer：

1. transcript 为 F421 的 `symcc-loop-memoryphi-byte-lane-induction-v11`，并具有真实 Decision DAG value；
2. outer/inner 均为从零开始、正步长、unsigned `< bound` 的有限规范循环；
3. 所有实际 store 与 transcript 的 ordered writer store 集合完全相等；
4. 唯一目标为当前函数的固定 stack object，所有静态 write instance 均落在对象内部；
5. 循环中没有 load、call/invoke、allocation/free、volatile/atomic/fence、异常或未建模 effect；
6. 循环定义的非 void SSA 值没有任何 outer-loop 外部 use，即 `live_outs=0`；
7. writer 数、instance 数和生成 byte 数满足 consumer 的 1,024 writes / 8,192 bytes 硬预算。

当前明确不接纳 heap/multi-object、loop-carried load/store recurrence、scalar live-out、multi-exit、
input-dependent guard、外部调用、异常和一般 Polly/ISL LoopSCC。scalar live-out 负例仍保留 v11
证明 transcript，但不生成 executable capability，默认路径继续执行真实循环。

## 3. Producer：从证明记录到控制流操作

`compiler/ContinuationLowering.cpp` 在 nested Decision-DAG certificate 完成后再次扫描真实 outer loop：

- 收集实际 `StoreInst` 集合并与全部 summarized writers 做集合等价检查；
- 检查 effect whitelist 和 `mayHaveSideEffects()`；
- 对每个 loop-defined 非 void instruction 检查所有 user 都位于 outer loop 内；
- 将通过的 certificate 绑定到 `(preheader, header, source load)`，同一入口只登记一次；
- 在 outer preheader 的 PHI edge block 末尾输出终结指令，而不是普通 jump。

操作采用显式 opt-in：

```json
{
  "op": "loop_summary_transfer",
  "schema": "symcc-loop-summary-transfer-v1",
  "mode": "explicit-opt-in",
  "fallback": "bb1",
  "target": "bb6",
  "source_load": "v30",
  "transcript": {"schema": "...-v11"},
  "proof": {
    "effect": "closed-memory-only",
    "memory_kind": "stack",
    "memory_base": 64,
    "live_outs": 0,
    "store_count": 2,
    "loop_blocks": ["bb1", "bb2", "bb3", "bb4", "bb5"]
  }
}
```

对应 capability 为
`refinement-verified-nested-loop-memory-summary-transfer`。未显式启用时，executor 使用 `fallback`
进入原 header，保持历史行为和调试可观察性。

## 4. Strict consumer：不信任 producer 的第二次证明

`util/live_continuation.py` 在 `create()` 阶段完成独立重建：

1. 要求 capability、v11 Decision DAG capability 和 operation use point 闭合；
2. 从 transcript 与 lowered blocks 复核 preheader edge、header、inner entry/header/body、latch 和 exit；
3. 定位唯一 `source_load`，要求其 initialization transcript 与 transfer transcript 精确一致；
4. 调用既有 v11 strict reconstruction，重新验证 IV、bound、writer、DAG、input ABI、实例和 witness；
5. 扫描实际 region，只允许证明域内 opcode，store 必须全部位于 inner body；
6. 从实际 operand use 重算 scalar live-out，不能仅相信 `proof.live_outs=0`；
7. 重算 stack owner、对象范围、store count、loop block 顺序和完整 proof dictionary；
8. 将 transcript 编译为规范化的不可变 write program；已有 `_compiled` cache 若不完全相等则拒绝；
9. 全程序限制最多 64 个 transfer，并要求 `(function, source_load)` 唯一。

checker mutation battery 覆盖 capability 删除、source load 篡改、fallback/target 互换、effect、memory kind、
live-out、store count、block order、transcript base 和 compiled cache。所有变异均在 `create()` 前失败。

## 5. 规范化 write program

consumer 对每个静态 `(outer, inner, writer ordinal)` instance 选择唯一 value：

- constant writer 直接保存 target-endian bytes；
- affine writer 保存 `constant + input_scale*x mod 2^bits`，outer/inner 项已按 instance 专门化；
- piecewise 和 Decision DAG 的 guard 只依赖静态 IV，因此 consumer 沿 DAG 选择唯一 leaf；
- 所有 write 按 `outer -> inner -> writer ordinal` 升序排列，严格复现源程序 store 顺序。

每个 instance 的运行时 activation 为：

```text
active(o, i) = (o < outer_bound) AND (i < inner_bound)
```

每个 byte 的变换为：

```text
M_next[a] = ite(active(o,i), writer_byte, M_prev[a])
I_next[a] = I_prev[a] OR active(o,i)
```

重复地址按程序顺序串接 ITE，所以后写覆盖前写；重叠 `i16 store` 后接 `i8 store` 时只替换低地址
byte，高地址 byte 保留原 i16 value。大端/小端 byte extraction 由 artifact 的 DataLayout 决定。

## 6. 事务执行与状态合并语义

运行时采用 prepare/commit：

- **prepare**：解析 bound/value operand，构建 activation、byte ITE、initializedness marker 副本和私有
  candidate memory root；所有地址、位宽、对象和表达式检查均在此阶段完成；
- **commit**：只有 prepare 完全成功后才同时替换 `state.memory_root` 与 `state.values`，计数命中并把 PC
  跳到 outer exit；中间不存在对原 state 的写入。

故障注入测试在真实 preheader checkpoint 破坏最后一个 write address，使前面表达式已经构建后再失败，
并比较 `_ExecutionState` 的 frames、solver root、value map、memory root 和搜索元数据，确认完全不变。

真实符号循环会按 bound guard 分成多个 exit state；F422 把这些状态合并成一个带 ITE 的状态。因此正确
关系是出口状态集合的指称并集等价，而不是 checkpoint、表达式 digest 或 solver-root 的结构相等。
零次迭代和 partial load 保留 initializedness=false；底层 zero byte 只是 concrete backing，不是可声明的
程序返回值。后续 load 仍加入 definedness 条件，只有完全初始化的 load 才具有可直接比较的确定值。

## 7. 调度、coverage 与兼容性

`util/live_state_search.py` 为 summary operation 建立两条 CFG 可达边：

- `fallback`：显式关闭优化时进入真实 loop；
- `target`：显式启用优化时进入 exit。

该选择来自 executor configuration，而不是程序 branch，所以不生成 branch decision token，不进入 CBC、
CGS 或 path-cover 的 branch 集合，也不写 AFL coverage/data map。摘要候选产生的新输入仍需 concrete replay；
只有真实执行观察到的 edge/data coverage 才能进入跨 worker bitmap。

默认 `enable_loop_summary_transfer=False`。构造器只接受精确 boolean；每次 resume 重置并报告：

- `loop_summary_transfer_enabled`；
- `loop_summary_transfers_applied`；
- `loop_summary_transfer_fallbacks`。

## 8. 实验方法与结果

### 8.1 独立有限域差分

`benchmark/check_executable_loop_summary_transfer_oracles.py` 直接执行源级两层循环，不读取 producer
transcript 或 normalized writes，避免同源错误。固定 `payload=0x1234`，枚举：

```text
11 个合法 i16 load offset x 4 个 i2 outer bound x 4 个 i2 inner bound
= 176 个配置
```

结果：

| 指标 | 结果 |
|---|---:|
| 配置 | 176 / 176 通过 |
| summary transfer | 176 次命中 |
| fork | 0 |
| memory byte 等价 | 2,112 / 2,112 |
| initializedness marker 等价 | 2,112 / 2,112 |
| 完全初始化 load value 等价 | 33 / 33 |
| partial/uninitialized load | 143，保留定义域，不比较任意值 |
| 最大总步骤 | 27 |

两个代表输入的启用态结果：

| input hex | 默认真实循环 | 启用 F422 | 启用态 fork / steps |
|---|---|---:|---:|
| `0001033412` | 6 个 path state，返回值均为 13994 | 13994 | 0 / 27 |
| `0803033412` | 6 个 path state，4 个 0、2 个 9898 | 9898 | 0 / 27 |

默认结果包含其他符号 bound valuation 的状态；启用结果中的具体 annotation 对应当前输入，完整符号 ITE
仍表示所有 valuation。表格不能解释为一般程序 6x 加速或 campaign coverage 提升。

### 8.2 工程门禁

- LLVM 18：正向 executable fixture 与 scalar-live-out fallback，2/2 通过；
- LLVM 17：相同测试 2/2 通过；
- Python F422：graph/oracle 单测 3/3 通过；
- ruff、`py_compile`、LLVM 17/18 Werror build 通过；
- strict checker 的结构 mutation 与真实 checkpoint 事务回滚通过。

## 9. 三轮 review 结论

1. **合同 review**：补充 capability 依赖、terminal use point、source-load 唯一性、预算和 dangling
   capability；禁止信任 `_compiled` cache。
2. **语义 review**：发现最初 oracle 把 partial load 的 backing zero 错当成确定返回值；修正为 byte、
   initializedness、defined-load value 三层验证。实现的 marker/ITE 在该反例上正确。
3. **系统 review**：图模型保留 fallback/target 可达性但不制造 coverage branch；LLVM 17/18 正向、
   默认回退和 scalar-live-out 失败关闭均通过。

## 10. 结论与后续边界

F422 首次把项目的 polyhedral-like finite instance、MemorySSA/AA、Decision DAG 和 byte-lane definedness
连接成真正可执行的 loop-free transformer。创新点不是“有一个 loop summary”，而是 producer proof、
strict consumer 重建、事务执行、指称状态合并、coverage 权威隔离和独立差分证据组成了完整可信链。

下一阶段 F423 应扩展到生成式随机 predicate/endianness/affine coefficient differential，并报告 baseline
与 transfer 的 state、instruction、solver-query 和 wall-time 分布。一般 heap/multi-object、scalar
recurrence live-out、data-dependent trip count 和外部 effect 仍需新的 summary language，不属于 F422
已经证明的范围。

## 11. 复现

```bash
python3 benchmark/check_executable_loop_summary_transfer_oracles.py \
  /tmp/f422-source.json --payload 0x1234 --output /tmp/f422-oracle.json

lit -sv build/test/live_nested_loop_memoryphi_executable_transfer.ll \
  build/test/live_nested_loop_memoryphi_executable_transfer_fallback.ll

python3 /usr/lib/llvm-17/build/utils/lit/lit.py -sv \
  build-llvm17/test/live_nested_loop_memoryphi_executable_transfer.ll \
  build-llvm17/test/live_nested_loop_memoryphi_executable_transfer_fallback.ll
```

机制图：[`executable-nested-loop-memory-summary-transfer-f422.svg`](../diagrams/executable-nested-loop-memory-summary-transfer-f422.svg)。
