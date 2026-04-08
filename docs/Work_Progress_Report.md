# SymCC MPI 并行符号执行 — 工作进展报告

## 一、已完成的工作

### 1.1 并行框架开发

实现了两种 MPI 并行模式，共约 1,850 行 Python 代码：

**MPI 纯并行模式**（`mpi_concolic_execution.py`，771 行）

Master-Worker 架构。Master 将种子输入分发给 Workers，每个 Worker 运行 SymCC 符号执行引擎对输入做约束求解，产出新的测试用例反馈给 Master，形成迭代深化循环。进程数超过 46 时自动分裂为多个 Master，通过非阻塞 MPI 通信同步。

**Hybrid AFL+SymCC 模式**（`mpi_fuzzing_helper.py`，1,075 行）

在 MPI 并行 SymCC 的基础上增加 AFL 模糊测试器。AFL 做快速随机变异，SymCC 做精确约束求解，两者通过共享文件系统双向交换测试用例。SymCC 发现的有趣输入喂给 AFL 作为新种子，AFL 发现的新覆盖输入喂给 SymCC 做更深的约束求解。

### 1.2 性能优化（6 轮）

通过 profiling 工具精确定位瓶颈，逐轮优化：

| 轮次 | 瓶颈 | 优化方法 | 效果 |
|------|------|----------|------|
| 1 | Master 逐个 fork afl-showmap 做 triage | 将 triage 移到 Worker 端，返回稀疏边列表 | triage 103s → 0ms |
| 2 | MPI 消息传输文件内容 (~8MB/msg) | 改为路径协议，Worker 从文件系统直接读 | 消息 8MB → 64B |
| 3 | Worker 端 afl-showmap fork/exec 开销 | 使用 `afl-showmap -S` 流式模式持久 fork server | 12ms → 0.6ms/call (19x) |
| 4 | AFL queue 目录扫描 (88% CPU 时间) | `os.scandir` 替代 `os.listdir`+`stat`，有反馈时跳过扫描 | 39s → 3.7s (10x) |
| 5 | MPI recv 反序列化 (38% CPU 时间) | Worker 端 coverage dedup，只传 3% 的 interesting 测试用例 | 消息 34MB → 3.6MB |
| 6 | dispatch 和 collect 串行 | 交替处理 TAG_READY 和 TAG_RESULT | 减少 Worker 空闲 |

优化后 Master 在 np=64 时负载仅 9%（recv 5% + triage 4%），idle 57%，不再是瓶颈。

### 1.3 Bug 修复（20+ 个）

修复了影响正确性的关键 bug：

- **Hybrid 模式完全失效**：afl-showmap 路径解析产生 `./afl-showmap`（不存在），导致所有 triage 静默失败
- **LAVA-M 零产出**：coreutils 的 `fread_unlocked` 绕过 SymCC 的符号化包装器，修复方法是 patch `unlocked-io.h` 将 `*_unlocked` 映射回标准函数
- **AFL 管道死锁**：`afl_proc.stderr=PIPE` 未读取，64KB 缓冲区满后 AFL 静默停滞
- **MPI Barrier 死锁**：Master 异常退出未发送 TAG_STOP，Workers 永久阻塞
- **覆盖率测量虚高**：`total_generated` 重复计算了 SymCC 的 interesting 和 all 输出
- **base64 参数缺失**：benchmark 用 `base64 @@`（编码模式），应为 `base64 -d @@`（解码模式），覆盖率 7.44% → 23.9%
- **AFL 目标发现遗漏**：`discover_public_afl_targets()` 硬编码只扫描 `google-fts-afl/`，LAVA-M 的 AFL 二进制全部跳过

### 1.4 测试工具与基础设施

- **`run_benchmark.py`**（2,259 行）：自动化 benchmark 框架，支持 Serial/MPI/Hybrid/AFL-only 四模式对比，AFL 边覆盖率测量
- **`profile_bottleneck.py`**（726 行）：Master 各环节精确计时（scan/dispatch/recv/triage/idle），输出 scaling 分析报告
- **`run_multi_instance.py`**（407 行）：多实例并行调度器，种子分组独立运行
- **`compile_public_benchmarks.sh`**（1,108 行）：7 个测试目标的编译脚本（Google FTS、LAVA-M、libarchive、SQLite、合成 benchmark）
- **目标特定参数**（`.args` 文件机制）：每个二进制可配置额外命令行参数
- **base64 harness**：编码+解码+crash recovery（SIGSEGV handler + siglongjmp），crash 率 47% → 0%

### 1.5 字典引导约束求解

修改 SymCC 的 QSYM 后端（`solver.cpp`，+80 行 C++），在 Z3 约束求解后用 AFL 字典 token 替换被修改的字节位置，生成额外变体。通过 `SYMCC_DICT` 环境变量启用。

---

## 二、遇到的问题

### 问题 1：覆盖率不随并行度增长

MPI 吞吐量随 np 线性增长（np=128 达 15,022 tc/s，117x 加速），但覆盖率在 np=8 后就基本饱和。SQLite 200K 行代码，np=2 到 np=190 只多了 0.4% 覆盖率。

**原因分析**：SymCC 做单步约束翻转（flip one branch per execution）。一次执行产出 ~90 个变体，覆盖了该路径上所有相邻分支。更多 Worker 只是更快地处理更多输入，但所有输入来自同一棵约束树。实测发现，SQLite 的 SymCC 输出是原始 SQL 的逐字节替换（`SELECT → ?ELECT, S?LECT, SE?ECT...`），无法生成语义不同的 SQL（如 `INSERT`）。

### 问题 2：Hybrid 模式吞吐量远低于纯 MPI

Hybrid np=128 只有 497 tc/s，MPI 有 15,022 tc/s（30x 差距）。

**原因分析**：Hybrid 模式的输入来自 AFL queue（AFL 每秒只产出 ~50 个 interesting 用例），而 MPI 模式的输入来自自身产出的反馈循环。AFL 的产出速率是瓶颈。

### 问题 3：190 线程反而比 32 线程慢

libarchive 的覆盖率时间曲线显示 np=190 在 80s 才达到 np=32 在 20s 就达到的覆盖率。

**原因分析**：190 进程 = 5 个 Master 互相同步 hash，协调开销大。搜索空间（~2100 条可达边）在 20s 内被 32 个 Worker 穷尽，之后 158 个 Worker 做无用功。

### 问题 4：分种子多实例覆盖率反降

将 25 个 SQL 种子按类型分成 6 组，各跑一个 np=32 实例，合并覆盖率反而比单实例低 0.14%。

**原因分析**：分种子打断了跨类型正反馈循环。单实例中 CREATE 种子的变体可以作为 INSERT 的输入，形成组合探索。分实例后每组只能在自己的种子类型内探索。

### 问题 5：字典引导在全种子集下无效

字典引导在单种子时提升覆盖率 50%（1802→2710 edges），但在 25 个多样化种子下提升为 0%。

**原因分析**：字典 token 生成的变体（如 `abs()`, `count()`）已经被现有种子（如 `functions.sql` 中的 `SELECT abs(-1)`）覆盖。字典引导是种子多样性的替代品，不是额外提升。

---

## 三、解决方案与效果

### 3.1 并行框架：吞吐量优化成功

| 指标 | np=2 | np=32 | np=128 | 加速比 |
|------|-----:|------:|-------:|------:|
| xml tc/s | 128 | 7,683 | **15,022** | **117x** |
| SQLite tc/s | 236 | 6,579 | **19,741** | **84x** |
| harness tc/s | 172 | 4,117 | **12,249** | **71x** |

### 3.2 Hybrid 模式：覆盖率最优

xml_read_fuzzer 120s 覆盖率对比：

| 模式 | 边数 | 覆盖率 | 说明 |
|------|-----:|------:|------|
| AFL-only | 3,843 | 7.55% | 随机变异 |
| MPI-only np=128 | 3,183 | 6.26% | 约束求解 |
| **Hybrid np=128** | **4,496** | **8.84%** | **两者结合，最高** |

### 3.3 SymCC 的真实价值

base64 目标上 SymCC 成功求解了 LAVA-M 注入的 magic value 条件。AFL 执行 37 万次找不到的 bug，SymCC 一次约束求解就触发了（47% 的输出导致 crash）。这是 concolic execution 相对于 fuzzing 的核心优势。

### 3.4 覆盖率天花板的根因

通过 SQLite 的逐字节翻转分析，确认了覆盖率天花板的根因：SymCC 的约束翻转在**字节级别**操作（`buf[0]='S' → buf[0]=0x00`），无法做**语义级别**的替换（`SELECT → INSERT`）。这是 concolic execution 技术的固有限制，不是并行框架的问题。

---

## 四、结论

### 4.1 并行的价值是时间压缩

并行 SymCC 的核心价值不是"更多核 = 更高覆盖率"，而是"相同覆盖率所需时间更短"。实测：np=32 在 20s 达到 np=2 在 120s 的覆盖率（6x 时间加速）。对于有时间预算的安全审计，这是实际价值。

### 4.2 最优配置是 32 核/实例

| 搜索空间 | 最优 np | 原因 |
|---------|------:|------|
| 小（base64） | 8 | np=8 后覆盖率饱和 |
| 中（libarchive） | 32 | np=32 效果最好，np=190 反而慢 |
| 大（SQLite/xml） | 64-128 | 吞吐量峰值，但覆盖率增量 <3% |

192 核最优方案：6 个 np=32 实例分别测试不同目标，而非 1 个 np=190 实例。但分种子多实例在同一目标上无效（打断正反馈循环）。

### 4.3 Hybrid > AFL > MPI（在覆盖率维度）

Hybrid 模式在所有目标上实现了最高覆盖率，因为 AFL 的随机变异和 SymCC 的约束求解互补：AFL 做 SymCC 做不到的语义级替换（通过字典变异），SymCC 做 AFL 做不到的 magic value 精确求解。

### 4.4 覆盖率的根本限制是探索深度

无论多少核、多少实例、是否用字典，concolic execution 的单步约束翻转只能到达路径树的一步邻居。深层路径需要多步组合条件（如先 CREATE TABLE 再 INSERT 再 SELECT），单步翻转无法跨越。这不是工程问题，是理论限制。

---

## 五、待完成的工作

### 5.1 可立即推进

- **多目标并行调度器**：192 核同时测试 6 个不同目标（当前 `run_multi_instance.py` 已实现分种子，需扩展为分目标）
- **时间序列覆盖率**：`--timeseries` 功能已定义但未接入主循环，可输出覆盖率随时间变化的曲线
- **更多测试目标**：fuzzer-test-suite 中的 openssl、pcre2、harfbuzz 等尚未编译测试

### 5.2 需要更多研究

- **多步约束求解**：修改 QSYM 后端支持连续翻转 2-3 个分支条件的组合，而非单步。这可能显著提升覆盖率但工程复杂度高
- **Grammar-aware mutation**：将 AFL 的语法感知变异（如 SQL grammar）与 SymCC 的约束求解结合，生成语法正确的变体
- **V8/SpiderMonkey 等大型目标**：百万行级代码可能有更大的搜索空间，但编译和测试基础设施需要额外投入

### 5.3 已验证为无效的方向

- **分种子多实例**：实验证明降低覆盖率（打断正反馈循环）
- **字典引导**：在种子充分时无额外收益
- **np>128**：吞吐量和覆盖率均下降
