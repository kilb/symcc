# SymCC 技术深度解析

## 一、SymCC 的实现原理

### 1.1 整体架构

SymCC 分为**编译时**和**运行时**两个阶段：

```
源代码 → symcc 编译器（clang + LLVM Pass）→ 插桩二进制
                                                │
                                                ▼  执行
                                         SymCC 运行时（libsymcc-rt.so）
                                                │
                                         QSYM 约束求解后端
                                                │
                                         Z3 SMT 求解器
                                                │
                                         输出新测试用例
```

涉及的模块：

| 模块 | 文件 | 功能 |
|------|------|------|
| LLVM Pass | `compiler/Pass.cpp`, `compiler/Symbolizer.cpp` | 编译时在每条 IR 指令上插入 `_sym_*` 运行时调用 |
| libc 拦截层 | `runtime/src/LibcWrappers.cpp` | 拦截 `fread`/`read`/`fgets` 等 I/O 函数，将读入的字节标记为符号化输入（调用 `_sym_get_input_byte`）。`fread_unlocked` 等 gnulib 变体不在拦截列表中——这是上期 LAVA-M 零输出 bug 的根因 |
| 运行时桥接 | `runtime/.../Runtime.cpp` | 将 `_sym_*` 调用转换为 QSYM 表达式操作 |
| 表达式构建 | `runtime/.../expr_builder.h` | 链式构建器（从外到内调用）：`PruneExprBuilder` → `SymbolicExprBuilder` → `CommonSimplifyExprBuilder` → `ConstantFoldingExprBuilder` → `CommutativeExprBuilder` → `CacheExprBuilder` → `BaseExprBuilder` |
| 约束求解 | `runtime/.../solver.cpp` | 分支条件收集 → 新颖性检查 → Z3 求解 → 输出生成 |
| 依赖跟踪 | `runtime/.../dependency.h` | 并查集结构，按共享输入字节分组约束 |
| 覆盖跟踪 | `runtime/.../afl_trace_map.cpp` | 复制 AFL 的边覆盖哈希，判断分支是否 interesting |
| 影子内存 | `runtime/src/Shadow.cpp` | 稀疏页表，每字节存储一个 `SymExpr` 指针 |

### 1.2 具体示例：libarchive 解析 tar 文件 magic number 的完整流程

libarchive 在解析 tar 文件时，会检查偏移 257 处的 magic：`if (buf[257] == 'u' && buf[258] == 's' && buf[259] == 't' && buf[260] == 'a' && buf[261] == 'r')`。以第一个字节比较 `buf[257] == 'u'` 为例，以下是 SymCC 的完整处理流程：

#### 第 1 步：编译时插桩（LLVM Pass）

`Pass.cpp` 的 `instrumentFunction()` 遍历每条 LLVM IR 指令。`Symbolizer` 类是 `llvm::InstVisitor`，对不同指令类型有对应的 visitor 方法：

- `visitLoadInst`：插入 `_sym_read_memory(addr, 1, LE)` → 从影子内存读取 `buf[257]` 的符号表达式
- `visitCmpInst`（`buf[257] == 'u'`）：插入 `_sym_build_equal(sym_buf257, sym_u)`
- `visitBranchInst`：插入 `_sym_push_path_constraint(cmp_result, taken=true, site_id=0x401234)`

**短路优化**：每个 `_sym_build_*` 调用被包裹在 null 检查中——如果所有操作数都是 concrete（`SymExpr == NULL`），直接跳过构建。这使 SymCC 在具体执行路径上接近原生速度。

#### 第 2 步：输入字节符号化

程序通过 `fread` 读取 tar 文件时，libc 拦截层的 `fread_symbolized` 逐字节调用 `_sym_get_input_byte(offset=257, value=0x75)`：

```cpp
SymExpr _sym_get_input_byte(size_t offset, uint8_t value) {
    // 选择性符号化（SYMCC_FOCUS_BYTES="start-end"）
    if (focus_enabled && offset 不在范围内)
        return NULL;  // concrete，不创建符号表达式

    solver->pushInputByte(257, 0x75);  // 记录具体值
    return createRead(257);            // 创建 Z3 变量 byte_257
}
```

`createRead(257)` 经过构建器链（从外到内）：最内层 `BaseExprBuilder` 创建 `ReadExpr(index=257, bits=8)`，`CacheExprBuilder` 存入缓存，其余层对单个 ReadExpr 无操作。

结果：`byte_257` 存入影子内存 `shadow[&buf[257]]`。

#### 第 3 步：表达式构建

比较 `buf[257] == 'u'`（0x75）触发 `_sym_build_equal(sym_buf257, _sym_build_integer(0x75, 8))`。构建器链中 `ConstantFoldingExprBuilder` 检查两个操作数——`sym_buf257` 非常量 → 不折叠；`CacheExprBuilder` 存入缓存。结果：表达式 `Equal(byte_257, 0x75)`。

#### 第 4 步：分支条件注册与新颖性检查

`_sym_push_path_constraint(Equal(byte_257, 0x75), taken=true, site_id=0x401234)` 调用 `solver->addJcc()`：

```
addJcc(expr=Equal(byte_257, 0x75), taken=true, pc=0x401234)
  │
  ├─ isConcrete()? No
  ├─ isInterestingJcc()?
  │     → h = XXH32(0x401234, true) = 0x7A3B
  │     → idx = (prev_loc >> 1) XOR h    // 复制 AFL 边编码
  │     → virgin_map[idx] |= 新 bucket
  │     → (virgin_map[idx] | trace_map[idx]) != trace_map[idx]?
  │     → Yes → 标记反方向(taken=false)为已覆盖（避免重复求解）
  │     → 返回 interesting = true
  │
  ├─ interesting → 调用 negatePath()
  │
  └─ addConstraint() → dep_forest.addNode(): 将约束加入字节 257 的依赖树
```

#### 第 5 步：约束求解

`negatePath(Equal(byte_257, 0x75), taken=true)` —— 目标：生成使 `byte_257 != 0x75` 的输入。

```
1. fastSolve()（若 SYMCC_FAST_SOLVE=1）：
   模式匹配: ReadExpr(257) == Const(0x75), taken=true
   → negate: byte_257 != 0x75
   → target = 0x75 ^ 0x01 = 0x74 ('t')
   → 直接写入输出，不调用 Z3（μs 级）
   → 不短路后续 Z3 调用

2. Z3 求解：
   reset()
   syncConstraints(Equal(byte_257, 0x75))
     → dep_forest.find(257) → 获取依赖树
     → 将该树中所有约束加入 Z3（如果之前的分支也涉及 byte_257）
   addToSolver(Equal(byte_257, 0x75), taken=false)  // byte_257 != 0x75
   check() → SAT
   getConcreteValues() → Z3 模型中 byte_257 = 0x00, 其余不变
   saveValues() → 写入输出文件
     → 若 SYMCC_EMIT_HINTS=1: 写入 .hints 文件 "257:75:00"

3. 乐观求解（若上述 UNSAT）：
   reset()  // 丢弃所有路径约束
   仅添加 byte_257 != 0x75
   check() → 几乎必定 SAT（无上下文约束限制）
   → 输出标记 "optimistic"
   → 可能产出不满足完整路径约束的 infeasible 输入，
     但实践中这些输入往往走不同路径，触发新覆盖
```

乐观求解是 QSYM 最重要的设计决策之一：放弃路径正确性，换取更高的覆盖发现率。由 fuzzer 负责验证输出是否真正有价值。

#### 第 6 步：多分支联合求解（若 SYMCC_MULTI_SOLVE=1）

程序继续检查 `buf[258] == 's'`、`buf[259] == 't'`... 共 5 个连续字节比较。`addJcc` 收集到 `pending_branches_`，当遇到 non-interesting 分支时触发检测：

```
pending_branches_ = [
  {Equal(byte_257, 0x75), true},  // u
  {Equal(byte_258, 0x73), true},  // s
  {Equal(byte_259, 0x74), true},  // t
  {Equal(byte_260, 0x61), true},  // a
  {Equal(byte_261, 0x72), true},  // r
]

isConsecutiveByteComparison():
  偏移 [257,258,259,260,261] 连续 → true

negateGroup():
  Z3 求解: byte_257!=0x75 ∧ byte_258!=0x73 ∧ ... ∧ byte_261!=0x72
  → Z3 生成满足约束的任意解（如 5 字节全变 0x00）
  → 若路径约束中存在对 "PK\x03\x04"(zip) 的后续比较，
    Z3 可能在满足路径约束的同时生成 zip magic
```

### 1.3 影响效率的关键实现

**乐观求解（optimistic solving）**：路径约束不可解时丢弃上下文仅解当前分支。产出 infeasible 输入但覆盖发现率更高。代价是生成的部分输入在实际执行中走不通。

**表达式膨胀**：每个符号操作创建表达式节点。`PruneExprBuilder` 对非线性表达式（两个符号值相乘）直接返回 concrete，避免 Z3 进入不可判定理论。

**依赖森林的合并效应**：两条约束共享一个输入字节就被合并到同一棵树。`syncConstraints` 加载整棵树——树很大时 Z3 收到大量约束导致超时。

**垃圾回收**：`_sym_collect_garbage()` 扫描影子内存和栈帧，释放不可达的表达式。长时间执行大输入时必需。

**`isInterestingBranch` 的反向标记**：判定一个方向 interesting 时**同时标记反方向为已覆盖**。避免重复求解，但如果反方向需要不同路径约束才能到达，该机会被永久跳过。

**libc 拦截覆盖范围**：仅拦截 `fread`/`read`/`fgets`/`fgetc` 等标准函数。glibc 高层 API（`getutxent`）和 gnulib 变体（`fread_unlocked`）不在列表中，导致输入未被符号化（who 和 LAVA-M 的零输出根因）。

---

## 二、SymCC 和 AFL 的结合方式

### 2.1 当前方法：文件系统种子共享

```
AFL                                    SymCC
 │                                      │
 ├─ fuzzer01/queue/                     │
 │   id:000001,orig:seed ──────────────→│ Master 扫描 queue，分发给 Workers
 │   ...                                │
 │                                      ├─ Workers 执行 SymCC(input)
 │                                      ├─ Worker 端 showmap dedup（~3% 通过）
 │                                      ├─ Master triage（全局 bitmap merge）
 │                                      │
 │   id:symcc_000001 ←──────────────────┤ 有新覆盖的 TC 写回 AFL queue
 │                                      │
 └─ AFL sync 周期导入 symcc_* 文件       └─ 反馈 queue（迭代深化）
```

**核心机制**：
- **AFL → SymCC**：Master 扫描 AFL queue，智能打分调度选取种子
- **SymCC → AFL**：interesting 输出直接写入 `fuzzer01/queue/`。注意：这是直接写入 AFL master 的 queue 目录，而非通过 secondary 实例 sync。在大多数 AFL++ 版本中可正常工作，但可能与 AFL 的并发写入存在竞争——实践中未观察到问题，因为文件名格式（`id:symcc_NNNN`）与 AFL 自身的命名不冲突
- **SymCC → SymCC**：interesting 输出加入 `symcc_feedback_queue`，深度优先调度

### 2.2 四层过滤保证质量

| 层 | 机制 | 作用 |
|----|------|------|
| SymCC 运行时 | `isInterestingBranch()` 对已覆盖分支不调用 `negatePath()` | 源头减少无效求解 |
| Worker | StreamingShowmap（0.6ms/TC）+ 本地 bitmap merge | 仅传 ~3% TC 给 Master |
| Master | 全局 bitmap merge，仅写入有新边的 TC | 保证 AFL queue 不被无效文件淹没 |
| AFL | 独立 sync 过滤 | AFL 自身判断是否有新路径 |

### 2.3 影响效率的关键问题

**问题 1：约束信息丢失**

SymCC 求解出 `byte[257] 必须等于 0x75 才能匹配 "ustar" magic`，但传给 AFL 的只是一个文件——AFL 不知道哪些字节是关键的，仍用盲目的 bitflip/havoc 变异。

**对策（Hint 传递）**：`saveValues()` 写入 `.hints` 文件（`offset:old:new`），Master 写入 AFL `extras/` 目录。AFL 将 extras 作为字典 token 使用。

**问题 2：盲目种子选择**

原始实现按 FIFO 从 AFL queue 取种子。大量种子已被 AFL 充分变异，SymCC 重复分析只产出已覆盖路径。

**对策（智能调度）**：多因子打分（+cov +100、symcc 前缀 +20、新颖度 +0~50、大文件 -30）+ SHA-256 内容去重。

**问题 3：单步约束翻转**

每次只翻转一个分支 → 单字节变体。`ustar` → `·star` 而非 `PK\x03\x04`。

**对策（多分支联合求解）**：连续字节比较模式检测后同时翻转 N 个分支。libarchive FstatsCov +3.32pp。

**问题 4：约束膨胀**

全部输入字节被符号化。1KB 输入 → 1024 个符号变量 → Z3 超时。

**对策（选择性符号化 + Fuzzy-Sat）**：`SYMCC_FOCUS_BYTES` 限制符号化范围；`fastSolve()` 对简单约束直接计算跳过 Z3。

---

## 三、并行符号执行

### 3.1 公开的并行符号执行框架

| 框架 | 并行粒度 | 机制 | 约束共享 |
|------|---------|------|---------|
| **Cloud9** (2011) | 执行树状态 | 中央负载均衡器 + P2P 状态传输 | CoW 内存共享，无跨 worker 约束缓存 |
| **S2E** (2011) | VM 状态 | fork-and-split | fork 后独立，无再平衡 |
| **KLEE** (2008, 本体无并行) | — | 外部扩展：Ranged SE (2012)、Test-Depth (2021) | 本地约束缓存（反例缓存、独立性优化） |
| **Driller** (2016) | 粗粒度 | AFL 核池 + angr 核池，文件系统 sync | 仅测试用例级共享 |
| **QSYM** (2018) | 无并行 | 单进程，与 AFL 配对 | 乐观求解 + 指数退避 |
| **Pangolin** (2020) | 无显式并行 | 增量式多面体路径抽象 | 跨轮复用约束空间 |
| **Cottontail** (2026) | LLM API 调用 | 隐式并行 | 无状态 |
| **ConcoLLMic** (2026) | `--parallel_num` | 并发求解 | 无共享缓存 |

**核心对比**：Cloud9 分割**执行树**（状态级），我们分发**输入文件**（输入级）。Cloud9 需要复杂的状态序列化；我们只传 64 字节 hash。代价是无法共享约束——不同 Worker 可能对相似输入独立求解相似约束。

### 3.2 自研并行框架的实现

#### 测试状态的传递方式

| 信息类型 | 传递机制 | 作用 |
|---------|---------|------|
| 输入文件 | MPI 纯并行：SHA-256 hash + 共享文件系统；Hybrid：路径字符串 | Worker 读取执行 |
| 覆盖 bitmap | `.shared_bitmap` 文件 + `bitmap_version` 版本号 | Worker 端 dedup + SymCC `isInterestingBranch` |
| 新测试用例 | Worker showmap dedup 后 MPI TAG_RESULT 传回 | Master triage → AFL queue |
| 约束 hints | `.hints` 文件 → TAG_RESULT → AFL `extras/` | AFL 字典变异 |
| 符号化范围 | TAG_WORK 消息中的 `focus_bytes` 字段 | Worker 设置 `SYMCC_FOCUS_BYTES` |
| 迭代代数 | `file_generation` + `symcc_feedback_queue` | 深度优先调度 |
| 多 Master 同步 | `TAG_HASH_SYNC`/`TAG_HASH_BCAST`，2s 间隔 | 跨 Master 去重 |

> MPI 纯并行模式的完整协议（hash 协议、多 Master 同步、空闲超时）详见 `docs/Parallel_Architecture_v2.md` 第二章。

#### 关键设计决策

**为什么选择输入级并行而非状态级**：SymCC 的执行模型是"给定一个具体输入，沿一条路径执行"。每次执行产生确定性的一条路径和一组约束，不存在 KLEE 那样的"多条活路径"需要调度。并行化的自然粒度是输入文件。

**为什么用共享文件系统而非 MPI 传输内容**：测试用例可达数 MB（如 TrueType 字体），MPI 传输会成为瓶颈。内容寻址存储（SHA-256 命名）天然去重。

### 3.3 QSYM 原有的约束优化 vs 我们的并行优化

| 维度 | QSYM 原有方案 | 我们的并行方案 |
|------|-------------|-------------|
| **约束去重** | 依赖森林按共享字节合并约束，`syncConstraints` 仅加载相关树 | SHA-256 输入去重，跳过相同内容的文件 |
| **求解加速** | 乐观求解（丢弃路径约束）+ 指数退避（热分支 2^n 求解） | Fuzzy-Sat（简单约束跳过 Z3）+ 联合求解（多字节翻转） |
| **覆盖判断** | `isInterestingBranch()` 对照 AFL bitmap + 反向标记 | 同左 + Worker StreamingShowmap + Master 全局 bitmap |
| **并行扩展** | 无（单进程） | MPI Master-Worker + 多 Master 自动扩展 |

**关系**：QSYM 的优化在每个 Worker **内部**独立运行（进程内优化），我们的并行框架是 Worker **之间**的协调（进程间优化）。两者基本正交可叠加，但存在以下冗余：

- **指数退避不跨 Worker 共享**：同一热分支在 Worker A 已退避到 2^8 触发间隔，Worker B 仍从 2^0 开始。不同 Worker 可能在同一分支上重复进行早期的求解尝试。
- **依赖森林各 Worker 独立构建**：处理相似输入时，不同 Worker 的依赖森林会有大量重叠——相同约束在多个 Worker 中被独立加入 Z3。

这些冗余在实践中影响有限（Worker 处理不同输入，路径差异导致约束差异），但在理论上不如 Cloud9 的全局状态共享高效。

### 3.4 LLM 对约束求解的优化及并行化

#### 现有研究

**Cottontail**（IEEE S&P 2026）：基于 SymCC，用 LLM 替代 Z3 求解结构化输入。Solve-Complete 范式将解析器通过率从 Z3 的 0.1% 提升到 10%+。覆盖率比 SymCC 高 14-16%，成本 ~$0.78/小时。

**ConcoLLMic**（IEEE S&P 2026）：语言无关的 LLM Agent concolic executor。4 小时覆盖率超 KLEE 115-233%，支持 `--parallel_num` 并发求解。

#### LLM 并行化的特点

| 维度 | Z3 并行化 | LLM 并行化 |
|------|----------|-----------|
| 状态共享 | 需要（约束上下文） | 不需要（每次查询独立） |
| 扩展瓶颈 | CPU 核数 | API 并发 / 成本 |
| 约束类型 | 位向量（精确） | 自然语言（灵活但不精确） |
| 适用目标 | 所有程序 | 结构化输入（SQL、JSON） |

#### 与我们框架的结合

```
当前：Worker → SymCC → QSYM → Z3 → 输出

LLM 增强：Worker → SymCC → QSYM → Z3 → 输出
                                    ↓ (unsat/timeout)
                                 LLM API → 输出（回退路径）
```

我们的 MPI 框架并行化**输入执行**（N 个 Worker 处理 N 个输入），LLM 并行化**约束求解**（Worker 内部对失败约束调用 LLM）。两者正交，可同时启用。

**实施挑战**：Z3 的约束是 QF_BV（量词自由位向量）公式，序列化为 LLM 可理解的形式需要解决：(1) 位运算（AND/OR/XOR/shift）的自然语言表示，(2) 多变量约束的依赖关系，(3) 输出字节长度必须与原始输入一致。Cottontail 的做法是不直接序列化 Z3 公式，而是将路径信息表示为层次结构树（ESCT），让 LLM 在更高抽象层面推理。

**实施路径**（已写入 `docs/Future_Research_Plan.md`）：
- **路径 A（Z3 回退）**：`negatePath()` 中 Z3 失败时调用 LLM。1-2 周。
- **路径 B（独立变异引擎）**：LLM 根据路径约束树直接生成输入。2-4 周。

---

## 附录：实验数据摘要

### 吞吐量 Scaling（MPI 纯并行，120s）

| 目标 | np=2 | np=128 | 加速比 |
|------|-----:|-------:|------:|
| xml | 128 tc/s | 15,022 tc/s | 117x |
| SQLite | 236 tc/s | 19,741 tc/s | 84x |

### 深度集成优化效果（Hybrid np=8, 300s, Hybrid-AFL FstatsCov 差值）

| 方案 | libarchive | SQLite |
|------|------:|------:|
| 基线 | +1.38pp | +1.48pp |
| 全部增强 | **+4.56pp** | — |
| 全部增强 + fast-solve | — | **+0.96pp** |
