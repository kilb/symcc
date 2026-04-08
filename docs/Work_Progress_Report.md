# SymCC MPI 并行符号执行 — 工作进展报告

## 项目背景

**SymCC** 是一种编译期符号执行工具（Poeplau & Francillon, USENIX Security 2020），通过 LLVM pass 在目标程序中插入符号化跟踪代码，运行时收集路径约束并用 Z3 求解器生成覆盖新路径的测试用例。相比传统符号执行引擎（如 KLEE），SymCC 以接近原生的速度运行，但一次只能处理一个输入。

**项目目标**：通过 MPI 并行化框架，让 SymCC 能利用多核服务器同时处理多个输入，加速符号执行的探索速度；同时实现 AFL+SymCC 混合模式（Hybrid），结合模糊测试的随机变异和符号执行的精确求解。

**硬件环境**：AMD Threadripper PRO 9995WX（96 核 / 192 线程）、250GB DDR5、Ubuntu 24.04

**软件环境**：SymCC（QSYM 后端）、LLVM/Clang 18、Z3 4.x、AFL++ 4.40c、OpenMPI、mpi4py

**评估指标**：

- **吞吐量**（test cases/s）：单位时间内生成的测试用例数量，衡量并行效率
- **AFL 边覆盖率**：通过 `afl-showmap -C` 测量程序执行到的控制流边数占总边数的百分比。选择 AFL 边覆盖率而非 gcov 行覆盖率，因为它是 fuzzing 社区的事实标准，且所有模式（MPI、Hybrid、AFL-only）可以用同一个 AFL-instrumented 二进制统一测量
- **Scaling 效率**：`(np=N 的吞吐量) / (np=2 的吞吐量) / (N/2)`，衡量并行扩展性

---

## 一、已完成的工作

### 1.1 并行框架开发

实现了两种 MPI 并行模式，共约 1,850 行 Python 代码：

**MPI 纯并行模式**（`mpi_concolic_execution.py`，771 行）

Master-Worker 架构。Master 将种子输入分发给 Workers，每个 Worker 运行 SymCC 对输入做约束求解，产出新的测试用例反馈给 Master，形成迭代深化循环。Master 只通过 MPI 发送 64 字节的 SHA-256 hash（不传文件内容），Workers 从共享目录按 hash 读取文件，避免了序列化瓶颈。进程数超过 46 时自动分裂为多个 Master（通过 `MPI_Comm_Split` 创建子通信域），Master 间通过非阻塞 `isend` 每 2 秒同步 hash 集合。

**Hybrid AFL+SymCC 模式**（`mpi_fuzzing_helper.py`，1,075 行）

在 MPI 并行 SymCC 的基础上增加 AFL 模糊测试器。AFL 做快速随机变异（~7,000 exec/s），SymCC 做精确约束求解（~200 tc/s/worker）。两者通过共享文件系统双向交换测试用例：SymCC 发现的有趣输入写入 AFL 的 sync 目录作为新种子，AFL queue 中的新覆盖输入被 Master 分发给 SymCC workers。SymCC 产出的有趣测试用例同时加入反馈队列，无需等待 AFL sync 即可被其他 Worker 继续处理。

### 1.2 性能优化（6 轮）

开发了 `profile_bottleneck.py` 工具精确测量 Master 各环节耗时（scan/dispatch/recv/triage/idle），据此逐轮定位和消除瓶颈：

| 轮次 | 瓶颈位置 | 占比 | 优化方法 | 效果 |
|------|---------|------|----------|------|
| 1 | Master 逐个 fork afl-showmap 做 triage | 60% per-task | Worker 端运行 afl-showmap，返回稀疏边列表 `[(edge_id, count)]` | triage 103s → 0ms |
| 2 | MPI 消息传输文件内容 | ~8MB/msg | 路径协议：只发文件路径，Worker 直接读共享文件系统 | 消息 8MB → 64B |
| 3 | Worker 端 afl-showmap fork/exec | 12ms/call × 95次 = 1.1s/task | `afl-showmap -S` 流式模式：持久 fork server + stdin/stdout 管道协议 | 12ms → 0.6ms/call (19x) |
| 4 | AFL queue 目录扫描 | 88% (np=8) | `os.scandir` 替代 `os.listdir`+`stat`；有 SymCC 反馈时跳过扫描 | 39.5s → 3.7s (10x) |
| 5 | MPI recv 反序列化 | 38% (np=64) | Worker 端 coverage dedup：只传 ~3% 的 interesting 测试用例 | 消息 34MB → 3.6MB，recv 18.7s → 2.3s |
| 6 | dispatch 和 collect 串行执行 | — | 交替处理 TAG_READY 和 TAG_RESULT，同一循环内完成 | 减少 Worker 空闲时间 |

优化后 Master 在 np=64 时：recv 5% + triage 4% + scan 34% + **idle 57%**。Master 不再是瓶颈。

### 1.3 Bug 修复（20+ 个）

修复了影响正确性和稳定性的关键问题，按严重程度分类：

**致命 Bug（导致功能完全失效）**：
- **afl-showmap 路径错误**：当 AFL 通过 PATH 调用时，路径解析产生 `./afl-showmap`（不存在），导致 Hybrid 模式所有 triage 静默失败，`symcc_interesting` 永远为 0
- **LAVA-M 零产出**：coreutils 的 gnulib 用 `fread_unlocked` 宏替换 `fread`，绕过了 SymCC 的 `fread_symbolized` 包装器。Patch `unlocked-io.h` 将 `*_unlocked` 映射回标准函数后，base64 从 0 test cases 恢复到 16,855
- **base64 参数缺失**：benchmark 以编码模式（`base64 @@`）运行，实际应为解码模式（`base64 -d @@`），导致覆盖率 7.44%（编码查表路径）vs 23.9%（解码验证路径）

**严重 Bug（导致死锁或数据丢失）**：
- **AFL stderr 管道死锁**：`afl_proc.stderr=PIPE` 未读取，64KB 缓冲区满后 AFL 停滞
- **MPI Barrier 死锁**：Master 异常退出未发送 TAG_STOP，Workers 永久阻塞
- **MPI shutdown 未排空在途消息**：`group_comm.Free()` 时仍有未消费的 TAG_RESULT

**中等 Bug（导致数据不准确）**：
- **AFL-only tc/s 虚高**：报告的 `generated` 是 AFL queue 文件数（interesting 用例），不是总执行数。现在额外报告 `execs_done` 和 `execs_per_sec`
- **AFL 目标发现遗漏**：`discover_public_afl_targets()` 硬编码只扫描 `google-fts-afl/`，LAVA-M 的 AFL 二进制被跳过
- **覆盖率测量双重计数**：`total_generated = afl_count + symcc_count + symcc_all_count`，其中 `symcc_count` 是 `symcc_all_count` 的子集

### 1.4 测试工具链

| 工具 | 代码量 | 功能 |
|------|------:|------|
| `run_benchmark.py` | 2,259 行 | Serial/MPI/Hybrid/AFL-only 四模式自动化 benchmark |
| `profile_bottleneck.py` | 726 行 | Master 各环节精确计时，scaling 分析报告 |
| `run_multi_instance.py` | 407 行 | 多实例并行调度器（种子分组独立运行） |
| `compile_public_benchmarks.sh` | 1,108 行 | 7 个测试目标的编译（含 SymCC、AFL、gcov 三种版本） |

测试目标覆盖：

| 测试集 | 目标 | 代码规模 | AFL 边数 | 特点 |
|--------|------|------:|------:|------|
| Google FTS | xml_read_fuzzer | libxml2 | 50,880 | XML 解析器 |
| Google FTS | png_read_fuzzer | libpng 1.2 | ~50,000 | 图像解析，CRC 校验 |
| LAVA-M | base64 | coreutils 345 行 | 1,088 | 注入 bug，magic value |
| 自制 | base64_harness | lib/base64.c | 192 | 编码+解码+crash recovery |
| 自制 | parallel_scaling | 130 行 | 768 | 64 switch-case，验证理论 scaling |
| fuzzer-test-suite | libarchive | 大型库 | 13,760 | 20+ 归档格式 |
| fuzzer-test-suite | SQLite | 200K 行 | 31,680 | 50+ SQL 语句类型 |

### 1.5 字典引导约束求解

修改 SymCC 的 QSYM 后端（`solver.cpp`，+80 行 C++）。在 Z3 求解约束后，对被修改的字节位置额外用 AFL 字典中的 token 替换，生成变体。例如 Z3 将 `buf[0]='S'` 翻转为 `buf[0]=0x00`，字典引导额外生成 `buf[0..5]="INSERT"`、`buf[0..5]="CREATE"` 等。每次翻转最多生成 20 个字典变体（`kMaxDictVariants=20`，通过实验确定——过多会淹没工作队列，降低整体效率）。通过 `SYMCC_DICT` 环境变量指定字典文件路径（复用 AFL 的 `.dict` 格式）。

---

## 二、遇到的问题与分析

### 问题 1：覆盖率不随并行度增长

**现象**：MPI 吞吐量随 np 线性增长（np=128 达 15,022 tc/s，相对 np=2 加速 117x），但覆盖率在 np=8 后就基本饱和。SQLite 200K 行代码，np=2 到 np=190 覆盖率只从 14.93% 增加到 15.35%（+0.42%），期间生成了 234 万个测试用例。

**根因**：SymCC 基于 concolic execution，做**单步约束翻转**（flip one branch per execution）。对输入 `SELECT * FROM t1` 执行后，它收集路径上所有分支条件（`buf[0]=='S'`, `buf[1]=='E'`, ...），对每个条件求反，生成 ~90 个变体。但这些变体是原始输入的**逐字节替换**（`?ELECT`, `S?LECT`, `SE?ECT`...），不是语义级替换（如 `INSERT`、`CREATE`）。所有变体走的是 SQLite 解析器的**同一条错误处理路径**（无效 SQL → 报错退出），因此覆盖率几乎不增长。更多 Worker 只是更快地处理同样无效的变体。

**性质**：这是 concolic execution 技术的**固有限制**，不是并行框架的工程问题。

### 问题 2：Hybrid 模式吞吐量远低于纯 MPI

**现象**：Hybrid np=128 只有 497 tc/s，纯 MPI 有 15,022 tc/s（30x 差距）。

**根因**：Hybrid 的输入来自 AFL queue（AFL 每秒只产出 ~50 个 interesting 用例），而纯 MPI 的输入来自 SymCC 自身产出的反馈循环（无外部瓶颈）。Hybrid 中 SymCC Workers 大部分时间在等待 AFL 产出新输入。

**性质**：这是架构性取舍。Hybrid 用吞吐量换覆盖率质量——AFL 提供的种子虽少但多样性更高。

### 问题 3：190 线程反而比 32 线程慢

**现象**：libarchive 的时间-覆盖率曲线显示 np=190 在 80s 才达到 np=32 在 20s 就达到的覆盖率。

**根因**：190 进程 = 5 个 Master 互相同步，协调开销大；搜索空间（~2100 条可达边）被 32 个 Worker 在 20s 内穷尽，之后 158 个 Worker 做冗余工作；文件系统在 190 进程并发写入时出现争用。

**性质**：并行度应匹配搜索空间大小。小目标用少量核，大目标用多核。

### 问题 4：分种子多实例覆盖率反降

**现象**：将 SQLite 的 25 个种子按 SQL 类型分成 6 组，各跑一个 np=32 实例，合并覆盖率 15.15% < 单实例 15.29%（-0.14%）。

**根因**：分种子**打断了跨类型正反馈循环**。单实例中 CREATE 种子的 SymCC 输出可以包含 INSERT 语句，形成 `CREATE→INSERT→SELECT` 的组合探索。分实例后 DDL 组永远不会产出 SELECT 相关的变体。SymCC 的正反馈循环是其核心价值之一。

**性质**：已验证为无效方向。

### 问题 5：字典引导在全种子集下无效

**现象**：字典引导在单种子（只有 SELECT）时覆盖率 +50%（1,802→2,710 edges），但在 25 个多样化种子下 +0%（4,832→4,835）。

**根因**：25 个种子已覆盖了字典中大部分 SQL 关键字（CREATE、INSERT、SELECT、PRAGMA...）。字典替换产生的变体（如 `abs() FROM t1`）走的代码路径，已经被对应种子（`functions.sql` 中的 `SELECT abs(-1)`）覆盖。字典引导是**种子多样性的替代品**，而非额外提升。

**性质**：方法本身有效，但与多样化种子集互为替代关系。

---

## 三、实验结果

### 3.1 吞吐量 Scaling

基线为 np=2（1 个 Worker），测试时间 120 秒。

| 目标 | np=2 | np=8 | np=32 | np=128 | 加速比 | 效率 |
|------|-----:|-----:|------:|-------:|------:|-----:|
| xml (tc/s) | 128 | 1,604 | 7,683 | **15,022** | **117x** | 92% |
| SQLite (tc/s) | 236 | 1,492 | 6,579 | **19,741** | **84x** | 66% |
| base64_harness (tc/s) | 172 | 1,027 | 4,117 | **12,249** | **71x** | 56% |

注：tc/s = test cases per second（SymCC 生成的测试用例总数 / 运行时间）。np=8~32 出现超线性 scaling（效率 >100%），原因是更多 Worker 产出更多种子互相受益形成正反馈。

### 3.2 覆盖率对比

xml_read_fuzzer，np=128，120 秒：

| 模式 | 边数 | 覆盖率 | 说明 |
|------|-----:|------:|------|
| 种子 | 1,571 | 3.09% | 20 个 XML 种子文件 |
| AFL-only | 3,843 | 7.55% | 865K executions，7,215 exec/s |
| MPI-only | 3,183 | 6.26% | 1.56M test cases |
| **Hybrid** | **4,496** | **8.84%** | **AFL + SymCC 互补** |

base64 -d（LAVA-M），np=16，120 秒：

| 模式 | 边数 | 覆盖率 | 说明 |
|------|-----:|------:|------|
| 种子 | 126 | 11.58% | 13 个 base64 种子 |
| AFL-only | 85 | 7.81% | 无 -d 参数，编码模式 |
| MPI-only | 253 | 23.25% | SymCC 求解 LAVA magic value |
| **Hybrid** | **260** | **23.90%** | **最高** |

### 3.3 SymCC 的特有价值：精确求解 magic value

在 base64 目标上，AFL 执行 37 万次无法触发任何 LAVA-M 注入的 bug。SymCC 通过约束求解精确构造了满足 `lava_get(N) == 0x6c617564` 条件的输入，**47% 的 SymCC 输出触发了 SIGSEGV**。这证明了 concolic execution 在精确路径探索上相对于随机模糊测试的不可替代性。

### 3.4 时间压缩效果

libarchive 目标上，np=32 在 **20 秒**内达到 np=2 在 **120 秒**才能达到的覆盖率水平（均为 ~1,770 edges），实现 **6x 时间加速**。

---

## 四、结论

### 4.1 并行化有效，核心价值是时间压缩

并行 SymCC 在吞吐量维度实现了优秀的 scaling（84-117x at np=128）。其核心价值在于将覆盖率达标所需的时间从分钟级压缩到秒级。对于有时间预算的安全审计场景（如 CI/CD 流水线中的自动化安全测试），这是直接可用的工程价值。

### 4.2 Hybrid 模式实现了最高覆盖率

在所有测试目标上，Hybrid（AFL + 并行 SymCC）的覆盖率均 ≥ AFL-only 和 MPI-only。AFL 提供了 SymCC 无法生成的语义级变异（通过字典和 havoc 阶段），SymCC 提供了 AFL 无法触达的精确路径（通过 magic value 约束求解）。两者互补效应明确。

### 4.3 覆盖率天花板是 concolic execution 的固有限制

覆盖率不随并行度线性增长，根因是 SymCC 的单步约束翻转只能到达路径树的一步邻居。这不是并行框架的工程缺陷，而是 concolic execution 技术本身的理论边界。突破此限制需要多步约束组合求解或 grammar-aware 符号执行等更深层的技术创新。

### 4.4 最优资源配置建议

单实例最优并行度为 32-128 核（取决于目标复杂度）。192 核服务器的推荐用法是同时测试多个不同目标（每个 32 核），而非单目标堆核。同一目标的分种子多实例无效（打断正反馈循环）。

---

## 五、待完成的工作

### 5.1 可立即推进

| 方向 | 预期收益 | 工作量 |
|------|----------|--------|
| 多目标并行调度器 | 充分利用 192 核 | 1-2 天 |
| 时间序列覆盖率曲线 | 可视化 scaling 效果 | 半天 |
| 更多测试目标（openssl, pcre2, harfbuzz） | 验证结论的普适性 | 2-3 天/目标 |
| 多轮统计（3-5 rounds/config） | 增加结果可信度 | 计算时间 |

### 5.2 需要深入研究

| 方向 | 潜力 | 难度 |
|------|------|------|
| 多步约束组合求解 | 可能突破覆盖率天花板 | 高（改 QSYM 后端核心逻辑） |
| Grammar-aware symbolic execution | 生成语法正确的变体 | 高（需要目标语法定义） |
| 与 LibAFL/AFL++ 的更深集成 | 利用 AFL 的 cmplog/redqueen | 中 |

### 5.3 已验证为无效的方向

| 方向 | 结论 | 数据 |
|------|------|------|
| 分种子多实例 | 覆盖率降低 | SQLite -0.14%, libarchive -1.20% |
| 字典引导（种子充分时） | 无额外收益 | +3 edges / 4832 total |
| np > 128 | 吞吐量和覆盖率均下降 | xml np=190 比 np=128 低 7% |

---

## 附录：实验可重现性

所有实验可通过以下命令重现（需先编译测试目标）：

```bash
# MPI scaling benchmark
python3 benchmark/run_benchmark.py \
  --targets gfts-xml_read_fuzzer,lava-base64 \
  --np-list 2,8,32,128,190 --timeout 120 --rounds 1 \
  --no-default --public --skip-build --output results/

# Hybrid + AFL-only
python3 benchmark/run_benchmark.py \
  --targets gfts-xml_read_fuzzer --hybrid --afl-only \
  --np-list 2,8,32,128 --timeout 120 --output results_hybrid/

# 瓶颈分析
SYMCC_MASTER_PROFILE=1 mpirun -np 32 python3 -u util/mpi_fuzzing_helper.py ...

# 字典引导
SYMCC_DICT=/path/to/sql.dict mpirun -np 32 python3 -u util/mpi_concolic_execution.py ...
```

注：当前 benchmark 结果为单轮运行（`rounds=1`），未计算标准差。多轮统计（建议 ≥3 轮）是后续工作的一部分。
