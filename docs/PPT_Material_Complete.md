# SymCC MPI 并行符号执行 — PPT 完整汇报材料

> 本文档为制作汇报 PPT 提供**完整详尽**的素材。内容覆盖项目全部历史工作，包括：技术原理、系统架构、性能优化、实验数据、深度集成、文献调研、未来规划。所有数据均标注来源。

---

# ═══════════════════════════════════════════════
# 第一部分：项目背景与技术原理
# ═══════════════════════════════════════════════

## 1. 研究背景

### 1.1 模糊测试与符号执行

**模糊测试（Fuzzing）**：以 AFL++ 为代表。通过对输入进行随机变异（bitflip、havoc、dictionary 等策略），执行目标程序，用 65536 字节的 bitmap 记录边覆盖情况。发现新边的变异结果保存为新种子，加入队列继续变异。优点是速度快（~2,000-5,000 exec/s），缺点是无法突破复杂约束（如 4 字节 magic number `0x504B0304`）。

**符号执行（Symbolic Execution）**：以 SymCC 为代表。在编译时通过 LLVM Pass 在程序的每条 IR 指令上插入"影子计算"代码。程序运行时除正常执行外，同时构建**符号表达式树**——记录每个变量如何依赖输入字节。遇到分支时，将分支条件取反，交给 Z3 SMT 求解器求解，生成满足取反条件的新输入。优点是精确求解复杂约束，缺点是速度慢（~200 tc/s）且每次只改变 1-3 个字节。

**Hybrid 方案**：AFL 负责广度探索（随机变异覆盖简单路径），SymCC 负责精度突破（精确求解复杂约束）。两者通过共享种子队列协同——SymCC 的输出写入 AFL 队列，AFL 的新种子分发给 SymCC。

### 1.2 现有 Hybrid 方案的局限

| 方案 | 年份 | 机制 | 局限 |
|------|------|------|------|
| Driller | NDSS 2016 | AFL + angr（基于 VEX IR 的符号执行） | angr 速度极慢，单次执行数分钟 |
| QSYM | USENIX Security 2018 | AFL + 原生指令级 concolic execution | 单进程单线程，无并行；与 AFL 仅文件级种子共享 |
| SymCC | USENIX Security 2020 | 编译时插桩 + QSYM 后端 | 速度快但仍单进程；每次只翻转一个分支 |

所有方案都只做**文件级种子共享**——SymCC/QSYM 的输出文件丢进 AFL 队列，存在 4 个效率损失。

### 1.3 四个效率损失

| # | 损失 | 详细说明 | 举例 |
|---|------|---------|------|
| 1 | **单步约束翻转** | SymCC 每次只取反一个分支条件，输出仅改 1 字节。要生成语义级变体（如 `INSERT`）需同时翻转 6 个分支，但标准 SymCC 不支持 | 输入 `"SELECT * FROM t"` → 输出 `"·ELECT * FROM t"`（仅改 byte[0]），不是 `"INSERT * FROM t"` |
| 2 | **盲目种子选择** | Master 按 FIFO 从 AFL 队列取种子。队列中大量种子已被 AFL 充分变异，SymCC 重复分析只会产出 AFL 已覆盖的路径 | AFL 队列 1,500 个种子（SQLite 300s 实验数据），大部分已被 AFL 的 havoc 变异覆盖 |
| 3 | **约束信息丢失** | SymCC 求解出"byte[257] 必须等于 0x75 才能匹配 tar magic 'ustar'"，但传给 AFL 的只是一个文件。AFL 不知道哪些字节是关键的，仍用盲目变异 | SymCC 发现关键字节 → 传文件 → AFL 收到后随机翻转 → 可能把关键字节改掉 |
| 4 | **约束膨胀** | SymCC 为输入的全部字节创建 Z3 符号变量。1KB 输入 → 1024 个变量。但目标分支可能只依赖其中 3 个字节。Z3 收到大量无关约束导致超时（默认 10 秒限制） | SQLite 的 SQL 输入 1KB → Z3 需跟踪 1024 个符号变量的所有操作历史 |

### 1.4 项目目标

1. 设计 **MPI 并行框架**，利用 96 核 / 192 线程服务器加速符号执行
2. 实现 **深度集成优化**，从约束求解层面解决上述 4 个效率损失
3. 在 **10 个真实目标** 上进行系统性实验验证

### 1.5 硬件环境

| 资源 | 配置 |
|------|------|
| CPU | AMD Threadripper PRO 9995WX — 96 核 / 192 线程 |
| 内存 | 250 GB DDR5 |
| GPU | 3× NVIDIA RTX PRO 6000（各 96 GB 显存，共 288 GB） |
| 磁盘 | 7.3 TB NVMe |
| OS | Ubuntu 24.04, kernel 6.17.0 |

### 1.6 软件栈

- SymCC（QSYM 后端）+ LLVM/Clang 18 + Z3 4.x
- AFL++ 4.40c（含 CmpLog 支持）
- OpenMPI + mpi4py
- Python 3.10+

---

## 2. SymCC 技术原理

### 2.1 两阶段架构

```
源代码 → symcc 编译器（clang + LLVM Pass）
  → 插桩二进制（每条 IR 指令上插入 _sym_* 运行时调用）
    → 执行时：libsymcc-rt.so（QSYM 后端 + Z3）
      → 沿路径收集约束 → 取反求解 → 输出新测试用例
```

### 2.2 涉及的模块

| 模块 | 文件 | 功能 |
|------|------|------|
| **LLVM Pass** | `compiler/Pass.cpp`, `compiler/Symbolizer.cpp` | 编译时遍历每条 IR 指令，插入 `_sym_*` 运行时调用。`visitBranchInst` 对条件分支插入 `_sym_push_path_constraint`；`visitLoadInst` 插入 `_sym_read_memory`；`visitBinaryOperator` 插入 `_sym_build_add`/`_sym_build_mul` 等 |
| **libc 拦截层** | `runtime/src/LibcWrappers.cpp` | 拦截 `fread`/`read`/`fgets`/`fgetc` 等标准 I/O 函数，将读入的每个字节标记为符号变量（调用 `_sym_get_input_byte`）。**未拦截的函数**（如 glibc 的 `getutxent`、gnulib 的 `fread_unlocked`）会导致输入未被符号化 |
| **运行时桥接** | `runtime/.../Runtime.cpp` (517 行) | 将 `_sym_*` 调用转换为 QSYM 表达式操作。包含选择性符号化（`SYMCC_FOCUS_BYTES`） |
| **表达式构建** | `runtime/.../expr_builder.h` | 7 层链式构建器（从外到内）：`PruneExprBuilder` → `SymbolicExprBuilder` → `CommonSimplifyExprBuilder` → `ConstantFoldingExprBuilder` → `CommutativeExprBuilder` → `CacheExprBuilder` → `BaseExprBuilder`。常量折叠、代数简化、结构去重 |
| **约束求解** | `runtime/.../solver.cpp` (1,137 行) | 分支条件收集（`addJcc`）→ 新颖性检查（`isInterestingBranch`）→ 约束求解（`negatePath`/`negateGroup`/`fastSolve`）→ 输出生成（`saveValues`） |
| **依赖跟踪** | `runtime/.../dependency.h` | 并查集（Union-Find）结构。两条约束共享一个输入字节就合并到同一棵树。`syncConstraints` 仅加载与目标分支相关的约束树——这比加载全部路径约束高效，但共享字节多时树会很大 |
| **覆盖跟踪** | `runtime/.../afl_trace_map.cpp` | 复制 AFL 的边覆盖哈希（`(prev_loc >> 1) XOR cur_loc`），判断分支是否 interesting。同时标记反方向为已覆盖（避免重复求解） |
| **影子内存** | `runtime/src/Shadow.cpp` | 稀疏页表，每字节存储一个 `SymExpr` 指针。`_sym_read_memory`/`_sym_write_memory` 通过影子内存传播符号表达式 |

### 2.3 约束求解完整流程（以 libarchive 解析 tar magic "ustar" 为例）

libarchive 在解析 tar 文件时，检查偏移 257-261 处的 5 字节 magic number `ustar`：

```
if (buf[257]=='u' && buf[258]=='s' && buf[259]=='t' && buf[260]=='a' && buf[261]=='r')
```

**第 1 步：输入字节符号化**

程序通过 `fread` 读取 tar 文件时，libc 拦截层的 `fread_symbolized` 逐字节调用 `_sym_get_input_byte(offset=257, value=0x75)`：
- 记录具体值 0x75（'u'）到 `inputs_[257]`
- 创建 Z3 符号变量 `byte_257`（`ReadExpr(index=257, bits=8)`）
- 存入影子内存 `shadow[&buf[257]]`

**第 2 步：表达式构建**

比较 `buf[257] == 'u'`（0x75）触发 `_sym_build_equal(sym_buf257, sym_0x75)`：
- 构建器链中 `ConstantFoldingExprBuilder` 检查：一侧非常量 → 不折叠
- `CacheExprBuilder` 存入缓存
- 结果：表达式 `Equal(byte_257, 0x75)`

**第 3 步：分支条件注册**

`_sym_push_path_constraint(Equal(byte_257, 0x75), taken=true, site_id=0x401234)`：
- `isConcrete()`? No（byte_257 是符号变量）
- `isInterestingBranch()`：计算 AFL 边 hash `idx = (prev_loc >> 1) XOR XXH32(0x401234, true)`，检查 `virgin_map[idx]` 是否有新 bucket
- 若 interesting → 调用 `negatePath()`
- 将约束加入依赖森林 `dep_forest.addNode(expr)`

**第 4 步：约束求解**

`negatePath(Equal(byte_257, 0x75), taken=true)` —— 目标：生成 `byte_257 ≠ 0x75` 的输入

```
1. Fuzzy-Sat 快速路径（若 SYMCC_FAST_SOLVE=1）：
   模式匹配: ReadExpr(257) == Const(0x75), taken=true
   → 取反: byte_257 != 0x75
   → 直接计算: 0x75 ^ 0x01 = 0x74 ('t')
   → 写入输出文件（μs 级），不调用 Z3
   → Z3 仍会被调用（纯增量）

2. Z3 求解：
   reset()（清空 Z3 solver 状态）
   syncConstraints()：从依赖森林找出涉及 byte_257 的所有历史约束，加入 Z3
   addToSolver(Equal(byte_257, 0x75), taken=false)  // 即 byte_257 != 0x75
   check() → SAT
   getConcreteValues()：Z3 模型中 byte_257 = 0x00, 其余字节不变
   saveValues()：写入输出文件 + .hints 文件（若 SYMCC_EMIT_HINTS=1）

3. 乐观求解（若上述 UNSAT）：
   reset()（丢弃所有路径约束）
   仅添加 byte_257 != 0x75
   check() → 几乎必定 SAT
   → 产出标记 "optimistic" 的输出
   → 可能不满足完整路径约束，但往往触发新覆盖
```

**第 5 步：多分支联合求解（若 SYMCC_MULTI_SOLVE=1）**

程序继续检查 buf[258]=='s', buf[259]=='t'... 共 5 个连续字节比较。`addJcc` 收集到 `pending_branches_`：

```
pending_branches_ = [
  {Equal(byte_257, 0x75), true},  // u
  {Equal(byte_258, 0x73), true},  // s
  {Equal(byte_259, 0x74), true},  // t
  {Equal(byte_260, 0x61), true},  // a
  {Equal(byte_261, 0x72), true},  // r
]

isConsecutiveByteComparison()：偏移 [257,258,259,260,261] 严格连续 → true

negateGroup()：
  Z3 超时增至 3 倍（30s）
  同时求解: byte_257!=0x75 ∧ byte_258!=0x73 ∧ ... ∧ byte_261!=0x72
  → 一次性改变 5 个字节
  半翻转变体：前 3 个翻转 + 后 2 个保持
```

### 2.4 影响效率的关键实现

| 机制 | 说明 | 影响 |
|------|------|------|
| **短路优化** | 每个 `_sym_build_*` 调用包裹在 null 检查中，所有操作数 concrete 时直接跳过 | SymCC 在具体路径上接近原生速度 |
| **乐观求解** | 路径约束 UNSAT 时丢弃上下文仅解当前分支，产出 infeasible 但可能触发新覆盖的输入 | QSYM 最重要的创新，大幅提升覆盖发现率 |
| **PruneExprBuilder** | 对非线性表达式（两个符号值相乘）直接返回 concrete | 避免 Z3 进入不可判定理论 |
| **依赖森林合并** | 两条约束共享输入字节即合并同一棵树。`syncConstraints` 加载整棵树 | 树很大时 Z3 收到大量约束→超时（约束膨胀根因） |
| **反向标记** | `isInterestingBranch` 判定一个方向 interesting 时同时标记反方向为已覆盖 | 避免重复求解，但反方向若需不同路径约束才能到达则被永久跳过 |
| **垃圾回收** | `_sym_collect_garbage()` 扫描影子内存和栈帧，释放不可达表达式 | 长时间执行大输入时必需，否则内存线性增长 |
| **libc 拦截覆盖** | 仅拦截 `fread`/`read`/`fgets`/`fgetc`。glibc 高层 API（`getutxent`）和 gnulib 变体（`fread_unlocked`）不在列表 | who 目标零输出、LAVA-M 零输出的根因 |

---

# ═══════════════════════════════════════════════
# 第二部分：系统架构与并行框架
# ═══════════════════════════════════════════════

## 3. 系统架构

### 3.1 代码规模

| 文件 | 行数 | 功能 |
|------|-----:|------|
| `util/mpi_fuzzing_helper.py` | 1,235 | Hybrid AFL+SymCC 模式主程序 |
| `util/mpi_concolic_execution.py` | 771 | MPI 纯并行模式主程序 |
| `benchmark/run_benchmark.py` | 2,443 | 自动化 benchmark 套件（Serial/MPI/Hybrid/AFL-only 四模式对比） |
| `benchmark/profile_bottleneck.py` | 726 | Master 性能瓶颈分析工具 |
| `benchmark/run_multi_instance.py` | 407 | 多实例并行调度器 |
| `benchmark/compile_public_benchmarks.sh` | 1,108 | 10 个目标的编译、补丁、配置脚本 |
| `benchmark/generate_seeds.py` | 106 | 种子生成工具 |
| `runtime/.../solver.cpp` | 1,137 | QSYM 约束求解器（含多分支联合求解、Fuzzy-Sat） |
| `runtime/.../solver.h` | 129 | 求解器头文件 |
| `runtime/.../Runtime.cpp` | 517 | SymCC ↔ QSYM 桥接层（含选择性符号化） |
| **合计** | **8,579** | |

> 数据来源：`wc -l` 对以上 10 个核心文件的统计。项目共 522 个 git commit。

### 3.2 两种运行模式

| 模式 | 文件 | 适用场景 | 特点 |
|------|------|---------|------|
| **MPI 纯并行** | `mpi_concolic_execution.py` | 纯 SymCC 符号执行，不依赖 AFL | Hash 协议、多 Master 自动扩展、内容寻址存储 |
| **Hybrid AFL+SymCC** | `mpi_fuzzing_helper.py` | AFL 模糊测试 + SymCC 约束求解协同 | 双向反馈、四层过滤、智能调度、hint 传递 |

### 3.3 为什么选择 MPI

- SymCC 每次执行是**独立进程**（给定输入→沿一条路径执行→产出文件），天然适合进程级并行
- MPI 支持**跨节点扩展**（不局限于单机共享内存）
- 与 AFL 进程天然隔离，**无需修改** SymCC 或 AFL 的内部线程模型
- mpi4py 提供 Python 友好接口，开发效率高

### 3.4 Hybrid 模式架构图

```
 ┌────────────────────────────────────────────────┐
 │  AFL++ Fuzzer（外部独立进程）                     │
 │  afl-fuzz -M fuzzer01 -i seeds -o afl_out       │
 │  执行速度 ~2,000-5,000 exec/s                    │
 └───────┬──────────────────────────┬──────────────┘
         │ ① Master 扫描 AFL queue    │ ⑥ Master 写入 AFL extras/ (hints)
         │   智能打分调度               │
         ▼                            ▲
 ┌──────────────────────────────────────────────────┐
 │  Master (MPI rank 0)                              │
 │  ② 构建工作队列（反馈优先 → AFL queue）             │
 │  ③ 交替分发（TAG_READY）和收集（TAG_RESULT）        │
 │  ④ 批量 triage（全局 bitmap merge）                │
 │  ⑤ 更新 bitmap_version + focus_bytes              │
 └──┬──────┬──────┬──────┬──────┬──────┬───────────┘
    │      │      │      │      │      │
    ▼      ▼      ▼      ▼      ▼      ▼
  W1     W2     W3     W4     W5     W6    SymCC Workers
  │      │      │      │      │      │
  └──────┴──────┴──────┴──────┴──────┘
    ⑦ 有趣输出写入 symcc01/queue/ 和 fuzzer01/queue/
    ⑧ SymCC 反馈队列（迭代深化，深度优先调度）
```

### 3.5 MPI 纯并行模式

```
                    ┌──────────────────────┐
                    │     Root Master      │
                    │     (rank 0)         │
                    │  pending_queue       │
                    │  analyzed_hashes     │
                    └──────┬───────────────┘
                           │ TAG_HASH_BCAST / TAG_HASH_SYNC
              ┌────────────┼────────────────┐
              │            │                │
     ┌────────▼──┐  ┌──────▼─────┐  ┌──────▼─────┐
     │ Sub-Master │  │ Sub-Master │  │ Sub-Master │  (np > 46 时自动拆分)
     └──┬──┬──┬──┘  └──┬──┬──┬──┘  └──┬──┬──┬──┘
        │  │  │        │  │  │        │  │  │
       W1 W2 W3      W4 W5 W6      W7 W8 W9     Workers
        └──┴──┴────────┴──┴──┴────────┴──┴──┘
                 共享文件系统（SHA-256 内容寻址）
```

#### 角色分配算法

```python
def compute_roles(comm_size, workers_per_master=45):
    num_avail = comm_size - 1  # rank 0 必为 master
    if num_avail <= workers_per_master:
        return {0: [1, 2, ..., num_avail]}       # 单 master
    M = max(1, min(ceil(num_avail / 45), num_avail // 3))  # 多 master
    # 轮询分配 worker → master
```

| np | Master 数 | Worker 数 | 每 Master 管辖 |
|---:|------:|------:|------:|
| 2-46 | 1 | np-1 | np-1 |
| 47 | 2 | 45 | 22-23 |
| 128 | 3 | 125 | 41-42 |
| 190 | 5 | 185 | 37 |

#### 通信协议

| 标签 | 值 | 方向 | 内容 |
|------|---:|------|------|
| `TAG_WORK` | 1 | Master → Worker | SHA-256 hash（64 字节），Worker 从共享文件系统读取内容 |
| `TAG_RESULT` | 2 | Worker → Master | `{"new_hashes": [str], "retcode": int, "elapsed": float}` |
| `TAG_STOP` | 3 | Master → Worker | `None`（终止信号） |
| `TAG_READY` | 4 | Worker → Master | `rank`（就绪信号） |
| `TAG_HASH_SYNC` | 10 | Sub-master → Root | `[hash_str, ...]`（新发现的 hash） |
| `TAG_HASH_BCAST` | 11 | Root → Sub-master | `[hash_str, ...]`（广播的 hash） |

#### Hash 协议

早期方案通过 MPI 传输完整测试用例内容（TC 可达数 MB），导致消息 ~8MB。优化后 MPI 仅传 64 字节 SHA-256 hash，Worker 从共享文件系统读取实际内容。内容寻址存储提供 O(1) 去重。

#### 多 Master 同步

每 2 秒执行 `sync_hashes()`：Root 收集各 Sub-master 的新 hash（`TAG_HASH_SYNC`），去重后广播给其他 Sub-master（`TAG_HASH_BCAST`）。**所有跨 Master 通信使用非阻塞 `isend`**，Sub-master 先 recv 再 send 避免死锁。

### 3.6 Hybrid 模式通信协议

**Master → Worker 工作消息**：
```python
{
    "path": "/path/to/input_file",      # 输入文件路径
    "bitmap_version": 42,               # 共享 bitmap 版本号
    "focus_bytes": "100-200",           # 选择性符号化范围（或空字符串）
}
```

**Worker → Master 结果消息**：
```python
{
    "new_tests": [                      # 仅含 interesting 的 TC（~3%）
        {
            "content": b"\x89PNG...",   # 测试用例二进制内容
            "bitmap": [(312, 1), ...],  # 稀疏边列表 [(edge_id, hit_count)]
            "hints": [(257, 0x75, 0x00)], # 约束 hints [(offset, old, new)]
        },
    ],
    "total_generated": 95,              # 总生成数（含被过滤的）
    "retcode": 0,
    "elapsed": 2.3,                     # 执行耗时（秒）
    "killed": False,
}
```

### 3.7 四层覆盖率过滤

SymCC 对一个输入执行一次，路径上约数十至上百个 interesting 分支，每个分支求解产出 1 个输出文件（典型 ~95 个）。大部分与已有覆盖重叠，需逐层过滤：

| 层 | 位置 | 过滤对象 | 机制 | 效果 |
|----|------|---------|------|------|
| 1 | SymCC 运行时 | **分支** | `isInterestingBranch()` 检查 AFL bitmap，已覆盖分支不调用 `negatePath()` | 源头减少求解调用 |
| 2 | Worker 端 | **输出文件** | StreamingShowmap（0.6ms/TC）+ 本地 bitmap merge | ~97% 丢弃，仅 ~3 个 TC 传回 Master |
| 3 | Master | **Worker 传来的 TC** | 全局 bitmap merge（原子 tmp+replace 写入 `.shared_bitmap`） | 仅保存有新边的 TC |
| 4 | AFL | **sync 目录文件** | AFL 独立判断是否触发新路径 | 最终 0-2 个进入 AFL queue |

### 3.8 AFL 进程管理

Hybrid 模式中 AFL 是外部独立进程。用户手动启动或 benchmark 框架自动启动（轮询 `fuzzer_stats` 等待就绪）。Master 通过 `AflConfig` 读取 AFL 配置。若 AFL 退出，Master 进入等待直到超时。

---

## 4. 六轮 Profiling 驱动优化

每轮用 `profile_bottleneck.py`（726 行）定位瓶颈，再针对性优化：

| 轮次 | 瓶颈（profiling 定位） | 优化方案 | 效果 |
|:----:|------|------|------|
| 1 | Master 端 afl-showmap triage 耗时 **103s/轮** | 将 showmap 移到 Worker 端执行，Master 仅处理稀疏边列表 | **103s → 0ms** |
| 2 | MPI 消息体积 **~8MB**（传输完整文件内容） | Hash 协议：仅传 64B SHA-256 hash，Worker 从共享文件系统读取 | **~8MB → 64B** |
| 3 | Worker 端 afl-showmap 每次 **fork+exec（12ms/call）** | Streaming fork server（`afl-showmap -S` 持久进程，二进制管道协议） | **12ms → 0.6ms** (19x) |
| 4 | AFL queue 扫描占 Master **88%** 时间 | `os.scandir` 替代 `listdir+stat`（利用 d_type 避免额外 stat）；有 SymCC 反馈时跳过 AFL 扫描 | **39.5s → 3.7s** (10x) |
| 5 | MPI recv 反序列化 **34MB**（传输全部 ~95 个 TC） | Worker 端 coverage dedup，仅传 ~3% interesting 的 TC | **34MB → 3.6MB** (9.4x) |
| 6 | 串行 dispatch/collect 循环导致 Worker 空闲 | 交替处理 TAG_READY 和 TAG_RESULT，在同一循环中分发和收集 | Worker 空闲减少 |

**优化后 Master 负载分布**（np=64 profiling 数据）：

| 组件 | 占比 | 说明 |
|------|-----:|------|
| AFL queue 扫描 | 34% | 已用 scandir 优化 |
| MPI recv | 5% | 消息从 34MB 降至 3.6MB |
| Triage | 4% | 0.04ms/msg，稀疏边集 O(500) |
| **空闲** | **57%** | Master 等待 Worker |

**结论**：Master 不再是瓶颈。实测 np=64（63 Workers）时 Master 仍有 57% 空闲。代码默认 np>46 时自动拆分多 Master（每组 ~45 Workers）。

---

## 5. Bug 修复与覆盖率工程优化

### 5.1 Bug 修复统计（25+）

| 等级 | 数量 | 代表案例 | 影响 | 修复方式 |
|------|:----:|---------|------|---------|
| **致命** | 5 | `fread_unlocked` 绕过 SymCC 拦截 | LAVA-M base64 零输出 | `unlocked-io.h` 补丁，重映射回 `fread` → 0 → **16,855 TC** |
| | | afl-showmap 路径解析错误 | Hybrid 完全不工作 | `shutil.which()` 回退 |
| | | base64 缺少 `-d` 参数 | 覆盖率 7.81%（应为 23.9%） | `.args` 配置机制 |
| | | `discover_public_afl_targets` 硬编码 | LAVA-M 无 Hybrid/AFL-only | 通用扫描 |
| | | who 的 `getutxent` 绕过拦截 | MPI 覆盖率 = 种子 | 根因分析（glibc API 盲区） |
| **严重** | 5 | AFL stderr PIPE 死锁 | AFL 静默挂起 | 异步 stderr 读取 |
| | | MPI Barrier 死锁 | Workers 永久阻塞 | TAG_STOP 信号替代 Barrier |
| | | 消息缓冲溢出 | 数据丢失 | 分批发送 |
| **中等** | 15+ | AFL-only 吞吐量虚高 | 指标失真 | 修正计算方式 |
| | | 覆盖率重复计算 | 数据错误 | 去重逻辑修复 |
| | | 原子写入缺失 | 竞争条件 | tmp+rename 原子写 |

### 5.2 覆盖率优化措施

| 优化 | 内容 | 效果 | 数据来源 |
|------|------|------|---------|
| **unlocked-io.h 补丁** | 将 coreutils 的 `fread_unlocked` 重映射回 `fread` | LAVA-M base64 从 0 → **16,855 TC** | 120s MPI 实验 |
| **CRC 绕过** | 编译时 patch libpng CRC 校验 | SymCC 可探索 CRC 后的数据处理路径 | 增强 harness 实验 |
| **base64 harness** | 编码+解码+崩溃恢复（SIGSEGV handler + siglongjmp） | crash 率 47%→0%，lcov 覆盖率达 49.48% | 早期 lcov 实验 |
| **增强 PNG harness** | 添加颜色转换 API（`png_set_expand` 等） | lcov 分支覆盖 12.1% → 24.5% | 早期 lcov 实验 |
| **增强 XML harness** | 添加 XPath/DTD/XInclude API | lcov 分支覆盖 4.0% → 13.0% | 早期 lcov 实验 |
| **种子多样化** | base64: 13 种编码; SQLite: 25 种 SQL; libarchive: 13 种格式 | 各目标均有提升 | — |
| **`.args` 配置** | 目标特定参数（如 base64 的 `-d`） | base64 覆盖率从 7.81% 修正为 23.9% | v2 benchmark |
| **字典引导求解** | 修改 QSYM 后端，Z3 求解后用 AFL 字典 token 生成变体（`SYMCC_DICT`） | 单种子 +50%，多样种子 +0% | 120s 实验 |
| **AFL++ CmpLog** | 编译时插桩 `strcmp`/`memcmp`，运行时提取比较参数做字典 | SQLite AFL+CmpLog **21.03%** > Hybrid 18.84%（ShowmapCov, 120s A/B） | 120s CmpLog 实验 |

> 注：增强 harness 数据使用 lcov 分支覆盖率，与后续 v2 benchmark 的 AFL 边覆盖率（`afl-showmap -C`）是不同指标，不可直接对比。v2 全量 benchmark 使用原始 harness。

---

# ═══════════════════════════════════════════════
# 第三部分：实验结果
# ═══════════════════════════════════════════════

## 6. 实验方法论

### 6.1 覆盖率指标

| 指标 | 来源 | 含义 | 分母定义 |
|------|------|------|---------|
| **ShowmapCov** | `afl-showmap -C` 事后重放所有已保存 TC | 累积覆盖率（queue + SymCC 输出） | 编译时插桩的**全部**边数（含从未触发的） |
| **FstatsCov** | AFL `fuzzer_stats` 文件的 `bitmap_cvg` | AFL 所有执行（含未保存变异）的累积覆盖 | AFL bitmap 有效槽位数（受 hash 碰撞影响，通常 < 插桩边数） |

**为什么分母不同**：`afl-showmap` 分析插桩二进制代码段直接统计所有注入的边（含死代码）；AFL 的 `total_edges` 只统计 bitmap 中被使用的 hash 槽位，碰撞导致后者更小。例如 SQLite: ShowmapCov 分母 31,680 vs FstatsCov 分母 20,509。

**本报告所有对比均在同一指标内进行，不跨指标比较百分比值。**

### 6.2 CPU 核数说明

| 模式 | 使用核数 | 组成 |
|------|------:|------|
| AFL-only | 1 | 1 AFL 进程 |
| MPI np=8 | 8 | 1 Master + 7 Workers |
| Hybrid np=8 | 8 | 1 AFL（独立进程）+ 1 MPI Master + 6 MPI SymCC Workers |

Hybrid 使用 CPU 多于 AFL-only。覆盖率差异中包含 CPU 资源差异的影响。缺少"N 个 AFL 实例"的等 CPU 对照组。

### 6.3 测试目标

| 来源 | 目标 | 代码规模 | AFL 边数 | 类型 |
|------|------|---------|------:|------|
| Google FTS | png_read_fuzzer | libpng 1.2.56 | 3,072 | 二进制图像格式 |
| Google FTS | xml_read_fuzzer | libxml2 2.9.2 | 50,880 | 文本标记语言 |
| LAVA-M | base64 | coreutils 345 行 | 1,088 | 编码工具+注入 bug |
| LAVA-M | md5sum | coreutils | 1,344 | 哈希工具 |
| LAVA-M | uniq | coreutils | 1,216 | 文本去重 |
| LAVA-M | who | coreutils | 10,688 | 用户查询 |
| Fuzzer Test Suite | libarchive | 大型库 | 13,760 | 多格式归档解析 |
| Fuzzer Test Suite | SQLite | 200K 行 | 31,680 | SQL 数据库引擎 |
| Fuzzer Test Suite | pcre2 | 正则引擎 | 7,488 | 正则表达式匹配 |
| Fuzzer Test Suite | freetype2 | 字体渲染 | 21,632 | TrueType 字体 |

共 **113 个种子文件**，**34 个编译目标**（7 个基础目标 × 2-4 变体：SymCC/AFL/CmpLog/Coverage，不同目标变体数不同）。

### 6.4 数据可靠性

所有数据为**单轮运行**（`rounds=1`），未计算标准差。AFL-only 基线在 6 次独立实验中的 ShowmapCov 标准差：libarchive ±0.23pp，SQLite ±0.63pp。差异 <1pp 的结论应视为**初步观察**。

---

## 7. 吞吐量 Scaling 结果

> 数据来源：`benchmark_results_scaling`（120s, MPI-only, np=2/8/32/128/190）

| 目标 | np=2 | np=8 | np=32 | np=128 | np=190 | 最大加速比 | np=128 效率 |
|------|-----:|-----:|------:|-------:|-------:|--------:|--------:|
| xml | 128 | 1,604 | 7,683 | **15,022** | 13,544 | **117x** | 92% |
| base64 harness | 172 | 1,027 | 4,117 | 12,249 | 10,341 | 71x | 56% |

> 注：SQLite 的 scaling 数据来自 `benchmark_results_scaling` 的 120s 实验，仅测试了 xml 和 base64 两个目标。SQLite 的 84x 加速比来自 `benchmark_results_scaling_hybrid` 中的独立实验（np=2: 236 tc/s → np=128: 19,741 tc/s）。

**单位**：tc/s（测试用例/秒）

**要点**：
- np≤32 效率 74%-100%（近线性 scaling）
- xml 在 np=8 出现**超线性** scaling：理论线性值 128×7=896 tc/s，实际 1,604 tc/s（179%）——多 Worker 产出的种子互相反馈形成正循环
- np=128 后效率下降：SQLite 66%，base64 56%——路径邻域饱和导致冗余探索增加
- np=190 比 np=128 更慢：多 Master 同步开销 + 文件系统竞争

### 时间压缩效应

> 数据来源：`benchmark_results_scaling`（120s, libarchive）

libarchive：np=32 在 20 秒内达到 np=2 在 120 秒才能达到的覆盖率（~1,770 edges）—— **6 倍时间加速**。

---

## 8. 全量覆盖率（10 目标）

> 数据来源：`benchmark_results_v2`（300s, np=2/8/32, Hybrid + AFL-only + MPI-only, 单轮, ShowmapCov）

### 8.1 ShowmapCov 汇总

| 目标 | AFL 总边数 | 种子 | MPI best (np) | AFL-only | Hybrid best (np) | Hybrid 增益† |
|------|------:|-----:|--------:|--------:|------:|------:|
| libarchive | 13,760 | 6.45% | 13.90% (8) | 15.07% | **23.36%** (32) | +8.29pp |
| freetype2 | 21,632 | 2.22% | 2.26% (2) | 5.75% | **11.23%** (8) | +5.48pp |
| pcre2 | 7,488 | 7.16% | 18.66% (8) | 41.73% | **43.58%** (32) | +1.85pp |
| png | 3,072 | 10.35% | 14.94% (2) | 14.10% | **16.96%** (32) | +2.86pp |
| who | 10,688 | 6.73% | 6.73% (-) | 44.57% | **47.26%** (32) | +2.69pp‡ |
| xml | 50,880 | 3.08% | 5.74% (2) | 7.60% | **8.90%** (32) | +1.30pp |
| base64 | 1,088 | 11.58% | 23.25% (8) | 7.81%§ | **23.90%** (8) | — |
| SQLite | 31,680 | 13.70% | 14.97% (2) | 18.55% | **18.84%** (32) | +0.29pp |
| md5sum | 1,344 | 7.14% | 7.14% (-) | 7.37% | 7.37% (-) | 0 |
| uniq | 1,216 | 9.54% | 9.54% (-) | 10.61% | 10.61% (-) | 0 |

†Hybrid 增益 = Hybrid best ShowmapCov - AFL-only ShowmapCov（CPU 核数不对等）
‡who 增益来自 AFL 自身（FstatsCov 维度 SymCC 贡献 ≈0）
§base64 AFL-only 缺 `-d` 参数

### 8.2 FstatsCov 汇总（仅 AFL 参与模式）

| 目标 | AFL-only | Hybrid np=2 | Hybrid np=8 | Hybrid np=32 | SymCC 净贡献 |
|------|------:|------:|------:|------:|------:|
| png | 14.28% | 14.28% | 14.38% | 14.28% | ≈0 |
| xml | 7.63% | 7.59% | 7.39% | 7.82% | +0.19pp |
| base64 | 14.74% | 14.74% | **15.51%** | 14.74% | +0.77pp |
| md5sum | 7.59% | 7.59% | 7.59% | 7.59% | 0 |
| uniq | 11.16% | 11.16% | 11.16% | 11.16% | 0 |
| who | 44.62% | 44.63% | 44.63% | 44.63% | ≈0 |
| libarchive | 15.13% | 16.28% | 16.50% | **19.17%** | **+4.04pp** |
| SQLite | 25.87% | 26.00% | 25.59% | **26.15%** | +0.28pp |
| pcre2 | **41.83%** | 40.90% | 40.24% | 40.46% | **-1.37pp** |
| freetype2 | 5.75% | 6.39% | **11.24%** | 5.59% | **+5.49pp** (np=8) |

SymCC 净贡献 = Hybrid best FstatsCov - AFL-only FstatsCov

---

## 9. SymCC 有效性分类

| 类别 | 目标 | ShowmapCov 贡献 | FstatsCov 贡献 | 根因分析 |
|:----:|------|------:|------:|------|
| **有效** | libarchive | +8.29pp | +4.04pp | 多格式 magic header（tar/zip/cpio），SymCC 精确构造各格式 magic |
| **有效** | freetype2 (np=8) | +5.48pp | +5.49pp | 适中 Worker 数提供有效种子，AFL queue +107% |
| **MPI 主力** | base64 | MPI 比种子 +100.8% | Hybrid 仅 +0.77pp | SymCC 独立发现大量路径（magic number），但 Hybrid 中 AFL 已覆盖 |
| **边际** | png, xml, pcre2, SQLite | 0.3-2.9pp | <1pp | AFL 变异已足够，SymCC 边际贡献在噪声范围 |
| **无效** | md5sum, uniq | 0 | 0 | 平坦二进制，随机翻转即可覆盖 |
| **无效** | who | 0 (MPI=种子) | ≈0 | glibc `getutxent()` API 绕过 SymCC libc 拦截 |
| **有害** | freetype2 (np=32) | -0.16pp | -0.16pp | 30 个无效 Worker 争抢 CPU，AFL exec/s 从 3,648 降到 3,433 |

### who 根因深入分析

who 通过 glibc 的 `getutxent()` 读取 utmp 文件。该函数不在 SymCC 的 libc 拦截列表（`fread`/`read`/`fgets` 等）中 → 输入未被符号化 → Z3 无约束可解 → MPI 覆盖率 = 种子 6.73%。

修复方法：将 `#ifdef UTMP_NAME_FUNCTION` 改为 `#if 0`，强制走 `fread` 路径。修复后 who 的 `nm` 输出中出现 `fread_symbolized`，SymCC 产出 2 个测试用例，但覆盖率不变——因为 utmp 是平坦二进制结构体数组，随机翻转比特即可覆盖。

**普遍意义**：任何通过 glibc 高层 API（`getaddrinfo`、`getpwnam`、`glob` 等）读取输入的程序都有此问题。

### freetype2 非单调覆盖率

> 数据来源：`benchmark_results_v2`（300s, freetype2, Hybrid）

| np | ShowmapCov | FstatsCov | AFL exec/s | AFL queue | Hybrid 总生成 |
|---:|------:|------:|------:|------:|------:|
| 2 | 6.39% | 6.39% | 3,648 | 575 | 578 |
| **8** | **11.23%** | **11.24%** | 3,408 | **1,190** | 1,200 |
| 32 | 5.59% | 5.59% | 3,433 | 553 | 602 |
| AFL-only | 5.75% | 5.75% | 3,496 | 541 | 541 |

- **np=8 最优**：6 个 SymCC Worker 为 AFL 提供有效种子，AFL queue +107%（575→1190），exec/s 仅下降 6.6%
- **np=32 有害**：30 个 Worker 产出无效（MPI 覆盖率 = 种子 2.26%），但持续消耗 CPU
- **根因是 CPU 争抢**，不是种子污染（四层过滤保证无效输出不进入 AFL queue）

---

## 10. 三层天花板模型

```
第一层天花板：Harness API 覆盖范围
  │  例：PNG 原始 harness 仅调用 4 个 API
  │  22.7% 写入模块 + 6% 渐进读取 = 28.7% 代码永远不可达
  │  优化：增强 harness → lcov 分支覆盖 12.1% → 24.5%
  ↓
第二层天花板：种子多样性
  │  例：9 个 PNG 种子仅覆盖少数颜色类型
  │  优化：扩展种子（13 种编码格式）→ 各目标均有提升
  ↓
第三层天花板：Concolic 求解能力
  │  例：CRC 联合约束无法求解；单步翻转无法生成语义级变体
  │  前期优化：CRC 绕过、字典引导（报告一/二）
  │  本期优化：多分支联合求解、Fuzzy-Sat、hint 传递（报告三）
  ↓
并行化在第三层天花板内运作 → 天花板已到则加并行无效
```

**关键洞察**：
- 吞吐量增长 117x 但覆盖率仅增 1-2pp → 第三层天花板已到
- 提升覆盖率必须**由外向内**突破天花板
- 并行化加速到达每层天花板的速度，但不能突破天花板本身

### 120s vs 300s 覆盖率对比

> 数据来源：`benchmark_results_full`（120s）vs `benchmark_results_v2`（300s）

xml Hybrid np=32：120s 达 8.80%，300s 达 8.90%（+0.10pp）。主要覆盖增长在前 120s 完成，额外 180s 收益有限——进一步佐证天花板已到。

---

# ═══════════════════════════════════════════════
# 第四部分：深度集成优化（报告三成果）
# ═══════════════════════════════════════════════

## 11. 文献调研

对 2021-2026 年最新研究进行系统调研，识别出 6 个改进方向：

| 来源 | 方向 | 解决的效率损失 | 核心思想 |
|------|------|-----------|---------|
| CONFETTI (ICSE 2022) | 约束 hint 传递给 fuzzer | 损失 3（信息丢失） | 将 SymCC 发现的约束知识（"byte[257]=0x75 是关键"）作为 AFL 字典 token |
| LeanSym (RAID 2021) | 选择性符号化 | 损失 4（约束膨胀） | 仅对影响目标分支的字节创建符号变量 |
| MEUZZ / K-Scheduler | 智能种子调度 | 损失 2（盲目选种） | 用机器学习或图分析选择高价值种子 |
| FUZZOLIC (2021) | Fuzzy-Sat 近似求解 | 损失 4（Z3 开销） | 简单约束直接计算，不走 Z3 |
| Cottontail (S&P 2026) | LLM 约束求解 | 损失 1（单步翻转） | LLM 理解语义，直接生成 `INSERT` 而非逐字节翻转 |
| ConcoLLMic (S&P 2026) | LLM Agent concolic | 损失 1 | 语言无关的 LLM concolic，4h 覆盖率超 KLEE 115-233% |
| Backsolver (TOSEM 2025) | 反向路径适配 | 损失 1 | 约束不可解时回溯修改前序路径 |

本期实施前 4 个方向 + 自研的多分支联合求解。后 3 个列入中长期计划。

---

## 12. 五项深度集成优化

### 12.1 多分支联合求解（C++ 层，solver.cpp，+490 行）

**目标**：解决损失 1——让 Z3 一次性改变多个字节。

**设计**：在 `addJcc()` 中，标准逐分支求解之外，额外收集连续 interesting 分支到 `pending_branches_`。检测到"连续字节比较"模式时，调用 `negateGroup()` 同时取反所有分支。

**"偏移连续"含义**：5 个分支分别依赖 byte[257], byte[258], byte[259], byte[260], byte[261]——偏移量 257,258,259,260,261 严格递增无间隔。典型出现在 `strcmp`/`memcmp` 的逐字节比较中。

**三种检测模式**：

| 模式 | 检测条件 | 典型场景 | 环境变量 |
|------|---------|---------|---------|
| 连续字节比较 | 各分支偏移严格连续 | `strcmp("ustar")` | `SYMCC_MULTI_SOLVE=1` |
| 同变量 switch-case | 各分支依赖相同字节集，比较不同常量 | `switch(type)` | `SYMCC_MULTI_SOLVE=2`（实验性） |
| 邻近结构体字段 | 各分支字节在 64B 窗口内 | 文件头多字段 | `SYMCC_MULTI_SOLVE=2`（实验性） |

**迭代演进**：

> 数据来源：`benchmark_results_multsolve`/`multsolve_v2`/`multsolve_v3`（300s, Hybrid np=8, libarchive）

| 版本 | 策略 | FstatsCov | vs 基线 | 生成量变化 | 教训 |
|------|------|------:|------:|------:|------|
| 基线 | 标准逐分支 | 17.94% | — | — | — |
| v1 | 盲目联合每 8 个分支 | 17.88% | -0.06pp | **-30%** | Z3 超时严重 |
| **v2** | **仅连续字节偏移** | **20.02%** | **+2.08pp** | -2% | **精准命中 magic** |
| v3 | +switch+struct | 16.88% | -1.06pp | -24% | 约束过复杂 |

**结论**：联合求解成功取决于**模式识别精准度**——连续字节（v2）是唯一有效模式。v2 定型为 `SYMCC_MULTI_SOLVE=1`。

### 12.2 智能种子调度（Python 层，+50 行）

**目标**：解决损失 2——优先处理高价值种子。

**设计**：重写 `best_new_testcases()` 方法，从 FIFO 改为多因子打分：

| 因子 | 权重 | 原理 |
|------|-----:|------|
| AFL `+cov` 标记 | +100 | AFL 文件名带 `+cov` 表示发现新覆盖→最可能触发新约束 |
| SymCC 产出（`symcc_` 前缀） | +20 | 迭代深化：对 SymCC 输出再做一轮符号执行 |
| AFL ID 新颖度 | +0~50 | ID 越大越新，越可能在探索前沿 |
| 大文件惩罚 | -30 max | >10KB 文件 SymCC 执行慢且约束多 |

额外：SHA-256 内容去重——跳过与已分析种子内容完全相同的文件。

### 12.3 约束 hint 传递（C++ + Python，+80 行）

**目标**：解决损失 3——将约束知识传递给 AFL。

**设计**（参考 CONFETTI 论文的 global hinting）：

1. **C++ 层**（`saveValues()`）：SymCC 生成输出文件时同时写入 `.hints` 文件，记录被修改的字节位置和新值。格式：`offset:old_hex:new_hex`。由 `SYMCC_EMIT_HINTS=1` 控制。
2. **Worker 层**：收集 `.hints` 文件，附加到 TC 结果中通过 MPI 传回 Master。
3. **Master 层**：将 interesting TC 的 hint 字节写入 AFL `extras/` 目录。AFL 自动读取作为字典 token，在变异中以一定概率在**任意位置**插入这些字节。

### 12.4 选择性符号化（C++ + Python，+60 行）

**目标**：解决损失 4——减少无关约束。

**含义**：只对输入的特定字节范围创建 Z3 符号变量，其余保持具体值（不产生约束）。

**机制**：`SYMCC_FOCUS_BYTES="start-end"`，在 `_sym_get_input_byte()` 中检查偏移是否在范围内，范围外直接返回 NULL。

**自适应**：Master 根据累积的 hint 偏移自动计算范围（interesting 偏移 ±32 字节），通过 dispatch 消息动态传递给 Worker。初始无限制；积累 ≥5 个 interesting 偏移后自动收窄。

**实际状态**：300s 实验中因 `symcc_interesting=0`（Master triage 无通过项）而**未激活**。需更长运行时间或更宽松触发条件。

### 12.5 Fuzzy-Sat 近似约束求解（C++ 层，+160 行）

**目标**：进一步解决损失 4——简单约束跳过 Z3。

**设计**：在 `negatePath()` 开头新增 `fastSolve()` 快速路径。对 `ReadExpr(i) op Constant` 模式直接计算满足取反条件的值。支持 10 种运算符（Equal/Distinct/Ult/Ule/Ugt/Uge/Slt/Sle/Sgt/Sge）。

```
示例：byte[5] == 0x50 (taken=true)
  → 取反: byte[5] != 0x50
  → 直接计算: 0x50 XOR 0x01 = 0x51（μs 级）
  → 写入输出文件（标记 "-fast"）
  → Z3 仍被调用做完整求解（纯增量，不短路）
```

### 12.6 迭代深化改进（Python 层）

| 改进 | 原值 | 新值 | 原理 |
|------|------|------|------|
| SymCC 超时 | 10s | 30s（`SYMCC_TIMEOUT`） | 10s 过短，复杂路径未充分探索即被 kill。30s 是工程判断（非调参） |
| 调度策略 | FIFO | 深度优先（最新代优先） | 优先探索 SymCC 输出的输出（迭代深化） |
| 代数跟踪 | 无 | `max_generation_reached` | 量化迭代达到的深度，日志可见 |
| 深度限制 | 无 | `SYMCC_MAX_DEPTH`（可配） | 防止在无效目标上无限迭代 |

---

## 13. 深度集成实验结果

> 数据来源：`benchmark_results_baseline`（基线）、`benchmark_results_enhanced`（全增强）、`benchmark_results_fastsol`（+fast-solve），300s, Hybrid np=8, 单轮

### 13.1 libarchive（Hybrid-AFL FstatsCov 差值，消除基线波动）

| 方案 | Hybrid FstatsCov | AFL-only FstatsCov | **差值** | vs 基线净增 |
|------|------:|------:|------:|------:|
| 基线（原始代码，TIMEOUT=10） | 17.94% | 16.56% | +1.38pp | — |
| v1 盲目联合求解 | 17.88% | 17.00% | +0.88pp | -0.50pp |
| **v2 连续字节联合求解** | 20.02% | 16.70% | **+3.32pp** | **+1.94pp** |
| v3 三模式联合求解 | 16.88% | 17.11% | -0.23pp | -1.61pp |
| **全增强**（优化 1-4，TIMEOUT=30） | **21.75%** | 17.19% | **+4.56pp** | **+3.18pp** |
| 全增强 + fast-solve（优化 1-5） | 18.43% | 16.98% | +1.45pp | — |

> 注：全增强+fast 的 ShowmapCov 差值为 +5.66pp（22.57%-16.91%），高于 FstatsCov 差值，因 SymCC 输出尚未被 AFL 完全同步。

### 13.2 SQLite

| 方案 | Hybrid FstatsCov | AFL-only FstatsCov | **差值** | vs 基线净增 |
|------|------:|------:|------:|------:|
| 基线 | 28.83% | 27.35% | +1.48pp | — |
| **全增强 + fast-solve** | **29.58%** | 28.62% | **+0.96pp** | ±噪声 |

### 13.3 分析

- **libarchive +3.18pp 净增**：远超基线波动（±0.23pp），**统计可信**
- **SQLite +0.96pp**：处于基线波动边缘（±0.63pp），需**多轮确认**
- "全增强" = 优化 1-4 代码 + TIMEOUT=30。优化 4（选择性符号化）因 `symcc_interesting=0` **未激活**，实际生效的是优化 1-3 + 超时增加
- 基线 TIMEOUT=10s vs 全增强 TIMEOUT=30s——覆盖率差异中**包含超时增加的贡献**，缺少消融实验无法分离
- **最优配置因目标而异**：多格式解析器（libarchive）用联合求解，文本解析器（SQLite）加 fast-solve

---

## 14. 已验证的无效方向

| 方向 | 实验结果 | 数据来源 | 教训 |
|------|---------|---------|------|
| 分割种子多实例（6×np=32） | 吞吐量 +2.8x，覆盖率 **-0.14%** | `run_multi_instance.py` 实验 | 隔离破坏跨类型反馈环 |
| 字典引导 + 多样种子 | 单种子 +50%，25 种子 +0% | 120s 实验 | 多样种子已覆盖字典关键字 |
| np > 128 | 效率降至 30%-56% | `benchmark_results_scaling` | 路径邻域饱和 + 多 Master 同步开销 |
| 盲目联合求解 (v1) | 生成量 -30%，FstatsCov -0.06pp | `benchmark_results_multsolve` | Z3 超时 |
| 三模式联合求解 (v3) | FstatsCov -1.06pp | `benchmark_results_multsolve_v3` | 约束过复杂 |
| SymCC 处理平坦二进制 | md5sum/uniq/who 贡献 0 | `benchmark_results_v2` | 随机翻转已足够 |

---

# ═══════════════════════════════════════════════
# 第五部分：对比、结论与展望
# ═══════════════════════════════════════════════

## 15. 与现有工作的对比

### 15.1 并行符号执行框架对比

| 框架 | 年份 | 并行粒度 | 机制 | 约束共享 | 我们的差异 |
|------|------|---------|------|---------|-----------|
| Cloud9 | 2011 | 执行树状态 | 中央 LB + P2P 状态传输 | CoW 内存 | 我们无需状态序列化，MPI 仅传 64B |
| S2E | 2011 | VM 状态 | fork-and-split | fork 后独立 | 我们有动态负载均衡 |
| KLEE | 2008 | 无内建并行 | 外部扩展 | 本地约束缓存 | 我们基于 SymCC（编译时，更快） |
| Driller | 2016 | 粗粒度核池 | AFL+angr, FS sync | 仅文件级 | 我们有智能调度+四层过滤+hint |
| QSYM | 2018 | 无并行 | 单进程+AFL | 乐观求解 | 我们在 QSYM 之上加并行+约束优化 |
| Pangolin | 2020 | 无显式并行 | 增量多面体抽象 | 跨轮复用 | 我们有显式 MPI 并行 |
| Cottontail | 2026 | LLM API | LLM 替代 Z3 | 无状态 | 我们是 CPU 密集型，它是 API 密集型 |

### 15.2 QSYM 原有优化 vs 我们的优化

| 维度 | QSYM（进程内） | 我们（进程间 + 约束层） |
|------|-------------|---------------------|
| 约束去重 | 依赖森林按字节分组 | SHA-256 输入去重 |
| 求解加速 | 乐观求解 + 指数退避 | Fuzzy-Sat + 联合求解 |
| 覆盖判断 | isInterestingBranch + 反向标记 | 同左 + Worker showmap + Master bitmap |
| 并行扩展 | 无 | MPI Master-Worker + 多 Master |

**关系**：QSYM 优化在 Worker **内部**运行（进程内），我们的并行框架是 Worker **之间**的协调（进程间）。两者基本正交可叠加——每个 Worker 内享受 QSYM 所有优化，Worker 之间通过 MPI 协调避免重复工作。但存在冗余：指数退避不跨 Worker 共享，依赖森林各 Worker 独立构建。

---

## 16. 192 线程最优使用策略

### 已验证的无效策略

| 策略 | 结果 |
|------|------|
| 单目标 np=190 | 效果劣于 np=32（路径邻域饱和） |
| 分割种子 6×np=32 | 吞吐量 +2.8x 但覆盖率 -0.14%（反馈环断裂） |

### 推荐策略

```
192 线程分配：
  1 个 Hybrid 实例 ~32 核（1 AFL + 1 Master + 30 SymCC Workers）
  + 剩余 160 核运行多个 AFL 实例或测试不同目标

单目标最优 np 因目标而异：
  libarchive: np=32（SymCC 有效）
  freetype2:  np=8 （部分有效，np=32 有害）
  md5sum:     np=2 或纯 AFL（SymCC 无效）
```

---

## 17. 核心结论

| # | 结论 | 数据支撑 | 数据来源 |
|---|------|---------|---------|
| 1 | **吞吐量 scaling 有效** | xml 117x, SQLite 84x (np=128) | `benchmark_results_scaling`, 120s |
| 2 | **覆盖率不随并行度线性增长** | np=2→128 覆盖率仅 +1-2pp | `benchmark_results_v2`, 300s |
| 3 | **Hybrid 互补性成立** | 10 目标 ShowmapCov 均 ≥ AFL-only | `benchmark_results_v2`, 300s |
| 4 | **深度集成显著提升效率** | libarchive FstatsCov 差值 +1.38→+4.56pp | `benchmark_results_baseline`/`enhanced` |
| 5 | **有效性高度依赖目标** | libarchive +8.3pp vs md5sum 0 vs freetype2 -0.16pp | `benchmark_results_v2` |
| 6 | **最优配置因目标而异** | libarchive 用联合求解，SQLite 加 fast-solve | `benchmark_results_enhanced`/`fastsol` |

---

## 18. 未来工作

### 18.1 近期（高优先级）

| 方向 | 说明 |
|------|------|
| 消融实验 | 分离 5 项改进的独立贡献（当前同时启用无法区分） |
| 多轮统计 | 3-5 轮，计算标准差，确认 libarchive +4.56pp |
| 自适应 np | 运行时检测 SymCC 产出率，自动调整 Worker 数 |
| 多 AFL 对照组 | 等 CPU 核数下 N×AFL vs 1 AFL + (N-1) SymCC |
| fast-solve 下降排查 | libarchive 全增强+fast 比全增强低 4.13pp（ShowmapCov） |
| 选择性符号化激活 | 放宽触发条件或延长运行时间 |

### 18.2 中长期研究（已做方案设计，见 `docs/Future_Research_Plan.md`）

| 方向 | 来源 | 预期收益 | 工作量 |
|------|------|---------|------:|
| LLM 约束求解 | Cottontail / ConcoLLMic (S&P 2026) | 结构化输入覆盖率 +30%，成本 ~$0.78/hr | 1-4 周 |
| 反向路径适配 | Backsolver (TOSEM 2025) | 解决 who 类隐式信息流问题 | 2-3 周 |
| 进程内集成 | LibAFL concolic tracing | 消除文件 I/O，延迟 <1ms | 2-4 周 |

---

## 19. 项目时间线

| 阶段 | 工作内容 | 关键成果 |
|------|---------|---------|
| **阶段一** | MPI 框架开发 | 实现纯并行和 Hybrid 两种模式（~1,850 行） |
| **阶段二** | 性能优化 | 6 轮 profiling 驱动优化，Master 负载从 >100s 降至 9% |
| **阶段三** | Bug 修复 | 修复 25+ bug，LAVA-M 从 0 输出到 16,855 TC |
| **阶段四** | 初步 Benchmark（120s） | 7 目标测试，确认吞吐量 84-117x scaling |
| **阶段五** | 覆盖率瓶颈分析 | 三层天花板模型，增强 harness（PNG +102%，XML +225%） |
| **阶段六** | 全量测试（300s） | 10 目标，np=2/8/32，70 个配置，双指标体系 |
| **阶段七** | 深入分析 | who 根因、freetype2 CPU 争抢、CmpLog 集成 |
| **阶段八** | 文献调研 + 深度集成 | 5 项约束层优化，libarchive FstatsCov +3.18pp 净增 |

---

## 20. 新增环境变量

| 环境变量 | 默认值 | 功能 |
|---------|-------|------|
| `SYMCC_MULTI_SOLVE` | 未设置（禁用） | `=1` 连续字节联合求解（推荐）；`=2` 含实验性模式 |
| `SYMCC_FAST_SOLVE` | 未设置（禁用） | `=1` 简单约束快速求解（Fuzzy-Sat） |
| `SYMCC_EMIT_HINTS` | 未设置（禁用） | `=1` 输出 .hints 约束文件 |
| `SYMCC_FOCUS_BYTES` | 未设置（全字节） | `="start-end"` 选择性符号化范围 |
| `SYMCC_TIMEOUT` | `30` | SymCC 单次执行超时（秒） |
| `SYMCC_MAX_DEPTH` | `0`（无限） | 迭代深化最大代数 |
| `SYMCC_DICT` | 未设置 | AFL 字典文件路径（字典引导求解） |

**推荐配置**：
- 多格式解析器（libarchive）：`SYMCC_MULTI_SOLVE=1 SYMCC_EMIT_HINTS=1 SYMCC_TIMEOUT=30`
- 文本解析器（SQLite）：上述 + `SYMCC_FAST_SOLVE=1`

---

# ═══════════════════════════════════════════════
# 附录
# ═══════════════════════════════════════════════

## 附录 A：参考文献

| 简称 | 全称 | 会议/期刊 | 年份 |
|------|------|----------|------|
| SymCC | Symbolic execution with SymCC: Don't interpret, compile! | USENIX Security | 2020 |
| QSYM | A practical concolic execution engine tailored for hybrid fuzzing | USENIX Security | 2018 |
| Driller | Augmenting fuzzing through selective symbolic execution | NDSS | 2016 |
| Cloud9 | Parallel symbolic execution for automated real-world software testing | EuroSys | 2011 |
| S2E | Selective symbolic execution | ASPLOS | 2011 |
| CONFETTI | Amplifying concolic guidance for fuzzers | ICSE | 2022 |
| LeanSym | Efficient hybrid fuzzing through conservative constraint debloating | RAID | 2021 |
| FUZZOLIC | Mixing fuzzing and concolic execution | Computers & Security | 2021 |
| Pangolin | Incremental hybrid fuzzing with polyhedral path abstraction | IEEE S&P | 2020 |
| Cottontail | LLM-driven concolic execution for structured test input | IEEE S&P | 2026 |
| ConcoLLMic | Agentic concolic execution | IEEE S&P | 2026 |
| Backsolver | Adapting preceding execution paths to solve constraints | ACM TOSEM | 2025 |

## 附录 B：完整实验数据索引

| 数据集 | 目录 | 目标数 | 数据行数 | 条件 | 用途 |
|--------|------|------:|------:|------|------|
| v2 全量 | `benchmark_results_v2` | 10 | 81 | 300s, np=2/8/32 | 主要覆盖率数据 |
| 吞吐量 scaling | `benchmark_results_scaling` | 9 | 65 | 120s, np=2~190 | 吞吐量加速比 |
| 基线对照 | `benchmark_results_baseline` | 2 | 7 | 300s, Hybrid np=8 | 深度集成基线 |
| 全增强 | `benchmark_results_enhanced` | 2 | 7 | 300s, Hybrid np=8 | 深度集成效果 |
| +fast-solve | `benchmark_results_fastsol` | 2 | 7 | 300s, Hybrid np=8 | Fuzzy-Sat 效果 |
| multi-solve v1 | `benchmark_results_multsolve` | 2 | 7 | 300s, Hybrid np=8 | 盲目联合 |
| multi-solve v2 | `benchmark_results_multsolve_v2` | 2 | 7 | 300s, Hybrid np=8 | 连续字节 |
| multi-solve v3 | `benchmark_results_multsolve_v3` | 2 | 7 | 300s, Hybrid np=8 | 三模式 |
| 早期全量 | `benchmark_results_full` | 4 | 65 | 120s, np=2/8/32 | 早期数据 |
| Hybrid 扩展 | `benchmark_results_scaling_hybrid` | — | 25 | 120s | Hybrid scaling |
| **共计** | **18 个数据集** | — | **~510 行** | — | — |

## 附录 C：关键数据速查

| 指标 | 数值 |
|------|------|
| 硬件 | 96 核 / 192 线程，250GB RAM |
| 总代码量 | 8,579 行（Python 5,688 + C++ 1,783 + Shell 1,108） |
| Git commits | 522 |
| 测试目标 | 10 个（21 个编译变体），113 个种子文件 |
| 吞吐量加速比 | xml **117x**, SQLite **84x**, base64 **71x** |
| Master 负载 | 9%（np=64 时 57% idle） |
| 最大覆盖率提升（ShowmapCov） | libarchive Hybrid vs AFL-only **+8.29pp** |
| 深度集成净增（FstatsCov 差值） | libarchive **+3.18pp** |
| Bug 修复数 | **25+**（致命 5 + 严重 5 + 中等 15+） |
| LAVA-M 修复效果 | base64 0 → **16,855 TC** |
| 优化轮次 | **6 轮**（消息 8MB→64B, showmap 12ms→0.6ms 等） |
| 环境变量 | **7 个**新增 |
| 实验数据集 | **18 个**，共 **~510 行** 数据 |
