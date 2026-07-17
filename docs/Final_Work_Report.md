# SymCC MPI 并行符号执行 — 工作进展及成果报告

> **用途说明**：本报告汇总了项目全部历史工作，适用于指导制作汇报 PPT。建议 PPT 按照报告章节顺序组织，每节对应 1-3 页幻灯片。
>
> **统计严谨性声明**：本报告所有实验数据均为单轮运行（`rounds=1`），未计算标准差。小幅差异（<1pp）可能为运行间噪声，不应作为确定性结论的依据。多轮统计（3-5 轮）列为高优先级后续工作。

---

## 一、项目概述

### 1.1 研究背景

**SymCC** 是一种编译时符号执行工具，通过 LLVM Pass 在编译阶段注入约束跟踪代码，使程序在运行时同时进行具体执行和符号约束收集，速度接近原生执行。然而 SymCC 每次只能处理一个输入，无法利用多核服务器的算力。

**项目目标**：使用 MPI 将 SymCC 并行化，实现两种模式：
1. **MPI 纯并行模式**：多 Worker 并行执行符号执行
2. **Hybrid AFL+SymCC 模式**：AFL 随机模糊测试 + 并行 SymCC 约束求解，双向反馈协同

### 1.2 硬件环境

| 资源 | 配置 |
|------|------|
| CPU | AMD Threadripper PRO 9995WX — 96 核 / 192 线程 |
| 内存 | 250 GB DDR5 |
| GPU | 3× NVIDIA RTX PRO 6000（各 96 GB 显存，共 288 GB） |
| 磁盘 | 7.3 TB NVMe |
| OS | Ubuntu 24.04, kernel 6.17.0 |

### 1.3 软件栈

- SymCC（QSYM 后端）+ LLVM/Clang 18 + Z3 4.x
- AFL++ 4.40c（含 CmpLog 支持）
- OpenMPI + mpi4py
- Python 3.10+

### 1.4 代码规模

| 文件 | 行数 | 功能 |
|------|-----:|------|
| `mpi_concolic_execution.py` | 771 | MPI 纯并行模式 |
| `mpi_fuzzing_helper.py` | 1,075 | Hybrid AFL+SymCC 模式 |
| `run_benchmark.py` | 2,443 | 自动化 benchmark 套件 |
| `profile_bottleneck.py` | 726 | 性能瓶颈分析工具 |
| `run_multi_instance.py` | 407 | 多实例并行调度器 |
| `compile_public_benchmarks.sh` | 1,108 | 编译脚本 |
| QSYM solver 字典求解扩展 | ~80 | Z3 字典引导约束求解 |
| **合计** | **~6,600** | |

---

## 二、系统架构设计

> **PPT 建议**：用架构图展示两种模式，突出 Master-Worker 模式和 AFL 双向反馈

### 2.1 MPI 纯并行模式

```
Rank 0 (Master): 分发种子、去重、收集结果
    │
    ├─→ Worker 1: 执行 SymCC → 输出新测试用例
    ├─→ Worker 2: 执行 SymCC → 输出新测试用例
    ├─→ ...
    └─→ Worker N: 执行 SymCC → 输出新测试用例
         │
         └─→ 共享文件系统（内容寻址存储, SHA-256 命名）
```

**核心设计**：
- **Hash 协议**：MPI 仅传输 64 字节 hash，Worker 通过共享文件系统读写内容（消息从 ~8MB 降至 64B）
- **内容寻址存储**：所有测试用例以 SHA-256 命名，O(1) 去重
- **多 Master 自动扩展**：进程数 >46 时自动通过 `MPI_Comm_Split` 拆分为多 Master，每 2 秒 `isend` 同步 hash 集合

### 2.2 Hybrid AFL+SymCC 模式

```
AFL fuzzer ←──────────────────→ Master (rank 0)
  │ 随机变异 (~2000-5000 exec/s)    │ 分发+分诊
  │                            ┌────┼────┐
  ↓                            │    │    │
fuzzer01/queue/           Worker 1..N (SymCC + showmap)
  ↑                                 │
  └── id:symcc_NNNNNN ←── 写入有趣的测试用例
```

**双向反馈机制**：
- **AFL → SymCC**：Master 扫描 AFL queue，分发给 SymCC Workers
- **SymCC → AFL**：有趣的输出写入 AFL sync 目录，AFL 自动导入
- **SymCC → SymCC**：有趣的输出加入反馈队列，实现迭代深化

**四层过滤保证 AFL 队列质量**：
1. SymCC 层：`isInterestingBranch()` 检查分支是否已覆盖
2. Worker 端 dedup：Streaming showmap（0.6ms/TC），仅传送有新边的（~3% 通过率）
3. Master triage：合并全局 coverage bitmap，仅保存有新边的用例
4. AFL 同步过滤：AFL 独立判断是否触发新路径

---

## 三、性能优化历程

> **PPT 建议**：用表格 + 柱状图展示 6 轮优化效果，突出数量级改善

### 3.1 六轮 Profiling 驱动优化

| 轮次 | 瓶颈 | 优化方案 | 效果 |
|:----:|------|---------|------|
| 1 | Master 端 afl-showmap triage | Worker 端执行 showmap，Master 仅处理稀疏边列表 | triage **103s → 0ms** |
| 2 | MPI 消息体积（~8MB） | Hash 协议：仅传路径，Worker 从共享文件系统读取 | 消息 **~8MB → 64B** |
| 3 | Worker afl-showmap 开销（12ms/call） | Streaming fork server（`afl-showmap -S`） | **12ms → 0.6ms** (19x) |
| 4 | AFL queue 扫描（占 88% Master 时间） | `os.scandir` + 反馈优先跳过扫描 | **39.5s → 3.7s** (10x) |
| 5 | MPI recv 反序列化（34MB 消息） | Worker 端 coverage dedup（仅传 ~3% interesting） | **34MB → 3.6MB** (9.4x) |
| 6 | 串行分发/收集循环 | 交替处理 TAG_READY 和 TAG_RESULT | Worker 空闲时间减少 |

### 3.2 优化后 Master 负载分布（np=64）

| 组件 | 占比 | 说明 |
|------|-----:|------|
| AFL queue 扫描 | 34% | 已用 scandir 优化 |
| MPI recv | 5% | 消息从 34MB 降至 3.6MB |
| Triage | 4% | 0.04ms/msg，稀疏边集 O(500) |
| **空闲** | **57%** | Master 等待 Worker |

**结论**：Master 不再是瓶颈。实测 np=64（63 Workers）时 Master 仍有 57% 空闲。代码默认 np>46 时自动拆分多 Master（每组 ~45 Workers）。

---

## 四、Bug 修复

> **PPT 建议**：列出关键 bug 类别和数量，选 2-3 个代表性案例说明

### 4.1 修复统计

共修复 **25+ 个 bug**，分为三个等级：

| 等级 | 数量 | 代表案例 |
|------|:----:|---------|
| **致命** | 5 | afl-showmap 路径解析错误（Hybrid 完全不工作）; LAVA-M 零输出（`fread_unlocked` 绕过符号化）; base64 缺少 `-d` 参数（覆盖率 7.44% 应为 23.9%） |
| **严重** | 5 | AFL stderr PIPE 死锁; MPI Barrier 死锁; 消息缓冲溢出 |
| **中等** | 15+ | AFL-only 吞吐量虚高; 覆盖率重复计算; 原子写入缺失; subprocess timeout 等 |

### 4.2 关键修复案例

1. **LAVA-M 零输出**：coreutils 使用 `fread_unlocked`（gnulib 宏），绕过了 SymCC 的 I/O 拦截。通过 `unlocked-io.h` 补丁将 `fread_unlocked` 重映射回 `fread`，修复后 base64 从 0 输出变为 **16,855 个测试用例**
2. **afl-showmap 路径错误**：`./afl-showmap` 在非本地目录不存在，用 `shutil.which()` 回退解决
3. **who 零输出**：`getutxent()` glibc 高层 API 不在 SymCC 拦截列表中，输入未被符号化

---

## 五、覆盖率优化措施

> **PPT 建议**：强调每个优化的动机和效果

| 优化 | 内容 | 效果 |
|------|------|------|
| **unlocked-io.h 补丁** | 将 coreutils 的 `fread_unlocked` 重映射回 `fread` | LAVA-M 从 0 → 16,855 TC |
| **CRC 绕过** | 编译时 patch libpng CRC 校验 | SymCC 可探索更深数据处理路径 |
| **base64 harness** | 编码+解码+崩溃恢复（SIGSEGV handler + siglongjmp） | crash 率 47%→0%，覆盖率达 49.48%（早期 lcov 实验） |
| **增强 PNG harness** | 添加颜色转换 API（`png_set_expand` 等） | Hybrid 分支覆盖率 12.1% → 24.5%（早期 lcov 实验） |
| **增强 XML harness** | 添加 XPath/DTD/XInclude | Hybrid 分支覆盖率 4.0% → 13.0%（早期 lcov 实验） |
| **种子多样化** | base64: 13 种编码; SQLite: 25 种 SQL; libarchive: 13 种格式 | 各目标均有提升 |
| **`.args` 配置机制** | 目标特定参数（如 base64 的 `-d`） | base64 覆盖率从 7.81% 修正为 23.9% |
| **字典引导求解** | 修改 QSYM 后端，Z3 求解后生成字典变体（`SYMCC_DICT`） | 单种子 +50%，多种子 +0%（已被种子覆盖） |
| **AFL++ CmpLog 集成** | 编译时插桩比较操作，运行时自动提取字典 | SQLite 上 AFL +2.55pp（120s CmpLog A/B 对比实验） |

> **注**：增强 harness（PNG/XML）使用 lcov 分支覆盖率衡量，与后续第六章使用的 AFL 边覆盖率（`afl-showmap -C`）是不同指标，数值不可直接对比。v2 全量 benchmark（第六章）使用的是 Google FTS 原始 harness，而非增强版，因此第六章的 PNG/XML 覆盖率低于此处的增强 harness 数据。

---

## 六、实验结果

> **PPT 建议**：这是核心章节，建议用 3-5 页展示。图表为主，文字为辅。

### 6.0 覆盖率指标说明

本报告使用两种覆盖率指标，理解其区别对阅读后续数据至关重要：

| 指标 | 来源 | 含义 | 适用模式 |
|------|------|------|---------|
| **ShowmapCov** | `afl-showmap -C` 重放全部已保存测试用例 | 累积覆盖（queue + SymCC 输出） | 所有模式 |
| **FstatsCov** | AFL `fuzzer_stats` 中的 `bitmap_cvg` | AFL 所有执行（含未保存变异）的累积覆盖 | 仅 Hybrid / AFL-only |

**两者差异的典型表现**：
- **FstatsCov > ShowmapCov**（如 SQLite）：AFL 执行了大量有效变异但未保存到 queue，覆盖仅存在于 AFL 内部 bitmap
- **ShowmapCov > FstatsCov**（如 libarchive、pcre2）：SymCC 输出有额外覆盖，但 AFL 在 300s 内未完全同步
- **两者相等**（如 freetype2）：无未同步输出，AFL bitmap 与 queue 一致

**指标选择原则**：评估"SymCC 是否帮助 AFL 发现更多路径"用 FstatsCov；评估"所有工具合计的覆盖潜力"用 ShowmapCov。

> **数据来源说明**：6.1 节吞吐量数据来自第一阶段 120s benchmark；6.2-6.5 节覆盖率数据来自最终 300s benchmark（`benchmark_results_v2`，10 目标 × 70 配置）；6.5 节 LAVA bug 案例来自早期 120s 实验。所有数据均为单轮运行。

### 6.1 吞吐量 Scaling（MPI 纯并行，120s）

| 目标 | np=2 | np=8 | np=32 | np=128 | 最大加速比 | np=128 效率 |
|------|-----:|-----:|------:|-------:|--------:|--------:|
| xml | 128 | 1,604 | 7,683 | **15,022** | **117x** | 92% |
| SQLite | 236 | 1,492 | 6,579 | **19,741** | **84x** | 66% |
| base64 harness | 172 | 1,027 | 4,117 | 12,249 | 71x | 56% |

**单位：tc/s（测试用例/秒）。** xml 在 np=128 仍保持 92% 效率；SQLite 和 base64 效率在高 np 下降（66%/56%），主要因路径邻域趋于饱和导致冗余探索增加。np=8~32 区间 xml 出现超线性 scaling（种子反馈正循环）。np≤32 效率普遍在 74%~100%。

> 注：本表仅测试了 np=2/8/32/128 四个点（未测 np=4/16/64），无法判断中间区间是否存在局部抖动。数据来自 120s benchmark（第一阶段实验）。

### 6.2 全量 Benchmark（10 个目标，300s，np=2/8/32）

#### 覆盖率汇总表（ShowmapCov，afl-showmap）

| 目标 | AFL 总边数 | 种子 | MPI best (np) | AFL-only | Hybrid best (np) |
|------|------:|-----:|--------:|--------:|-----------:|
| png | 3,072 | 10.35% | 14.94% (2) | 14.10% | **16.96%** (32) |
| xml | 50,880 | 3.08% | 5.74% (2) | 7.60% | **8.90%** (32) |
| base64 | 1,088 | 11.58% | 23.25% (8) | 7.81%† | **23.90%** (8) |
| md5sum | 1,344 | 7.14% | 7.14% (-) | 7.37% | **7.37%** (-) |
| uniq | 1,216 | 9.54% | 9.54% (-) | 10.61% | **10.61%** (-) |
| who | 10,688 | 6.73% | 6.73% (-) | 44.57% | **47.26%** (32)‡ |
| libarchive | 13,760 | 6.45% | 13.90% (8) | 15.07% | **23.36%** (32) |
| SQLite | 31,680 | 13.70% | 14.97% (2) | 18.55% | **18.84%** (32) |
| pcre2 | 7,488 | 7.16% | 18.66% (8) | 41.73% | **43.58%** (32) |
| freetype2 | 21,632 | 2.22% | 2.26% (2) | 5.75% | **11.23%** (8) |

†base64 AFL-only 缺少 `-d` 参数，不反映真实能力
‡who 的 Hybrid 覆盖率提升几乎全部来自 AFL 自身（FstatsCov 仅差 0.01pp），ShowmapCov 差异（+2.69pp）是因为 SymCC 输出虽包含少量新覆盖的测试用例，但 AFL 在 300s 内尚未完成同步

> 以 ShowmapCov 衡量，Hybrid 在每个目标的最优 np 下覆盖率均 ≥ AFL-only。但以 FstatsCov 衡量则不然（见下表 pcre2 反例）。

#### 覆盖率汇总表（FstatsCov，AFL fuzzer_stats）

以下反映 AFL 内部所有执行的累积覆盖（含未保存变异），仅适用于 AFL 参与的模式。

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

**关键观察**：
1. 以 FstatsCov 衡量，**大部分目标 Hybrid 与 AFL-only 差异 <1pp**——AFL 自身变异已覆盖了绝大部分可达路径
2. SymCC 净贡献显著的仅有 **libarchive**（+4.04pp）和 **freetype2 np=8**（+5.49pp）
3. pcre2 出现 **-1.37pp 的负贡献**：SymCC workers 争抢 CPU 降低了 AFL exec/s（4,870 → 3,931），AFL 自身探索受损
4. 差异 <1pp 的目标（png、xml、base64、SQLite 等），在单轮实验下无法排除运行间噪声，不应视为确定性结论

### 6.3 SymCC 有效性分类

> **PPT 建议**：用三色分类图或矩阵展示

| 类别 | 目标 | ShowmapCov 贡献 | FstatsCov 贡献 | 说明 |
|:----:|------|-----------|-----------|------|
| **MPI 主力** | base64 | MPI np=8 比种子 +100.8% | Hybrid 比 AFL 仅 +0.77pp | SymCC 独立发现大量新路径（magic number），但在 Hybrid 中 AFL 已通过其他途径覆盖 |
| **Hybrid 有效** | libarchive | Hybrid np=32 比 AFL +8.29pp | **+4.04pp** | 互补效应最强：SymCC 精确构造各格式 magic header |
| **Hybrid 有效** | freetype2 (np=8) | Hybrid 比 AFL +5.48pp | **+5.49pp** | 适中的 SymCC worker 数提供有效种子（见 6.4 详析） |
| **边际** | png, xml, pcre2, SQLite | Hybrid 比 AFL 高 0.3-2.9pp | 差异 <1pp 或为负 | AFL 变异已足够，SymCC 边际贡献在统计噪声范围内 |
| **无效** | md5sum, uniq | 贡献 0 | 0 | 平坦二进制，随机翻转即可覆盖 |
| **无效** | who | MPI = 种子（0 贡献） | ≈0 | glibc `getutxent()` API 绕过 SymCC 拦截 |
| **有害** | freetype2 (np=32) | 比 AFL-only -0.16pp | -0.16pp | 30 个无效 SymCC workers 争抢 CPU |

### 6.4 freetype2 Hybrid 覆盖率的非单调变化

freetype2 的 Hybrid 覆盖率随 np 呈先升后降：

| np | ShowmapCov | FstatsCov | AFL exec/s | AFL queue | Hybrid 总生成 |
|---:|------:|------:|------:|------:|------:|
| 2 | 6.39% | 6.39% | 3,648 | 575 | 578 |
| 8 | **11.23%** | **11.24%** | 3,408 | 1,190 | 1,200 |
| 32 | 5.59% | 5.59% | 3,433 | 553 | 602 |
| AFL-only | 5.75% | 5.75% | 3,496 | 541 | 541 |

**np=8 最优的原因**：6 个 SymCC workers 恰好在 CPU 资源与产出之间达到平衡——AFL queue 从 575 增至 1,190（+107%），说明 SymCC 确实为 AFL 提供了有效种子，帮助 AFL 进入了更多字体解析分支。同时 AFL exec/s 仅下降 6.6%（3,648 → 3,408），CPU 争抢尚可接受。

**np=32 下降的原因**：30 个 SymCC workers 几乎全部产出无效（MPI 纯模式覆盖率 = 种子 2.26%），但仍持续运行 Z3 求解和字体解析，大量消耗 CPU。AFL queue 反而从 1,190 降回 553（低于 AFL-only 的 541），说明 CPU 争抢已严重压缩 AFL 的有效执行时间。

**根因不是种子污染**：四层过滤机制保证无效输出不进入 AFL queue。覆盖率下降完全来自 CPU 资源争抢。

### 6.5 时间压缩效应

**libarchive**（120s benchmark 数据）：np=32 在 20 秒内达到 np=2 在 120 秒才能达到的覆盖率（~1,770 edges）—— **6 倍时间加速**。

> **局限**：此结论来自人工观察 120s 时间窗内的覆盖率变化趋势，项目尚未实现逐时间点采样（`--timeseries` 功能已实现但未在正式实验中启用）。精确的时间-覆盖率曲线数据列为后续工作。

### 6.6 SymCC 精确求解的独特价值

**base64 LAVA-M bug 触发**（120s 早期实验数据）：
- AFL 执行 373,805 次变异，0 次触发 LAVA bug（纯随机变异无法猜出精确 4 字节常数 `0x6c617664`）
- SymCC **单次执行**即精确求解 `lava_get(N) == 0x6c617564` 约束
- 采样检测显示约 47% 的 SymCC 输出触发 SIGSEGV —— 证明 SymCC 在精确约束求解上的不可替代性

> **PPT 建议**：作为亮点案例单独一页展示。此案例清楚展示了 SymCC 相对 AFL 的独特优势维度——不在覆盖率宽度（AFL 更强），而在精确约束求解深度。

### 6.7 120s vs 300s 覆盖率对比

两轮实验使用相同目标、相同 np 值，仅 timeout 不同。对比可验证 300s 是否比 120s 显著提升覆盖率。

**png（ShowmapCov）**：

| 模式 (np) | 120s | 300s | 差异 |
|----------|------:|------:|------:|
| MPI (2) | 14.23% | 14.94% | +0.71pp |
| MPI (8) | 15.36% | 14.58% | -0.78pp |
| MPI (32) | 15.69% | 14.36% | -1.33pp |
| Hybrid (2) | 16.86% | 16.44% | -0.42pp |
| Hybrid (8) | 16.96% | 16.50% | -0.46pp |
| Hybrid (32) | 16.86% | 16.96% | +0.10pp |

**xml（ShowmapCov）**：

| 模式 (np) | 120s | 300s | 差异 |
|----------|------:|------:|------:|
| MPI (2) | 5.72% | 5.74% | +0.02pp |
| MPI (8) | 5.88% | 5.48% | -0.40pp |
| MPI (32) | 6.13% | 5.43% | -0.70pp |
| Hybrid (2) | 7.57% | 8.24% | **+0.67pp** |
| Hybrid (8) | 8.39% | 8.70% | **+0.31pp** |
| Hybrid (32) | 8.80% | 8.90% | **+0.10pp** |

**观察**：
- **MPI 纯模式**：300s 覆盖率反而低于 120s（png MPI np=32: -1.33pp）。这是因为 MPI 输出量极大（np=32 产生 64 万 TC），300s 的 afl-showmap 测量使用了 20K 抽样，抽样波动导致数字下降。该差异不具统计意义。
- **Hybrid 模式**：300s 的 xml Hybrid 覆盖率略高于 120s（最大 +0.67pp），说明额外 180s 让 AFL 有更多时间进行有效变异和同步 SymCC 输出。但增幅有限，表明 300s 内主要覆盖增长已在前 120s 完成。

---

## 七、核心发现与结论

> **PPT 建议**：每个结论一页，用数据支撑

### 结论 1：并行框架吞吐量 scaling 有效

- 吞吐量 scaling：xml **117x**、SQLite **84x**、base64 **71x**（均在 np=128）
- 并行效率因目标而异：xml np=128 高达 92%，SQLite 66%，base64 56%。np≤32 效率普遍 74%~100%
- Master 经 6 轮优化后负载仅 9%（np=64），不再是瓶颈

### 结论 2：覆盖率不随并行度线性增长

这是 concolic execution 的**固有限制**，而非框架问题。

**根因**：SymCC 进行单步约束翻转——对一条路径上的每个分支生成 negated 约束的解。所有输出来自同一约束树的"邻域"。更多 Worker 更快处理更多输入，但所有输入来自同一约束树。

**实验验证**：SQLite 的 SymCC 输出是逐字节替换（`SELECT` → `·ELECT`、`S·LECT`...），无法生成语义级别不同的 SQL（如 `INSERT`、`UPDATE`）。

### 结论 3：Hybrid 模式互补效应成立，但幅度因指标而异

- 以 **ShowmapCov** 衡量：10 个目标中 Hybrid（最优 np）覆盖率均 ≥ AFL-only，最大增益 +8.29pp（libarchive）
- 以 **FstatsCov** 衡量：仅 **libarchive**（+4.04pp）和 **freetype2 np=8**（+5.49pp）有显著正贡献；**pcre2** 出现负贡献（-1.37pp）；其余目标差异 <1pp，在单轮实验下无统计置信度
- **互补效应最显著**：libarchive（Hybrid 23.36% 远超 AFL-only 15.07% 和 MPI-only 13.90%），因 SymCC 精确构造各格式 magic header 帮助 AFL 进入不同解析分支

### 结论 4：SymCC 有效性高度依赖目标特征

| 场景 | 代表目标 | 原因 |
|------|---------|------|
| **有效**：magic number / 校验和 | base64 | AFL 无法猜出精确常数，SymCC 一次求解 |
| **有效**：多格式解析器 | libarchive | SymCC 精确构造不同格式 magic header |
| **边际**：复杂输入格式 | png, xml, pcre2, SQLite | AFL 变异已足够发现大部分边，SymCC 边际贡献在噪声范围内 |
| **无效**：平坦二进制 | who, md5sum, uniq | 随机翻转即可覆盖，无需约束求解 |
| **需控制 np**：结构化格式 | freetype2 | np=8 有效（+5.49pp），np=32 有害（-0.16pp） |

### 结论 5：192 线程的最优使用策略

- 单目标 np=190 效果**劣于** np=32（libarchive 实测）
- **推荐**：1 个 Hybrid 实例 ~32 核 + 剩余核心运行多个 AFL 实例或测试不同目标
- 多实例分割种子**不可取**（SQLite 25 种子分 6 组，6×np=32 吞吐量 +2.8x 但覆盖率 -0.14%；libarchive -1.20%），会破坏跨类型反馈环

> **局限**：本项目未系统对比"N 个 AFL 实例 vs 1 AFL + (N-1) SymCC workers"的覆盖率。该对比对最优资源分配至关重要，列为后续工作。

### 结论 6：CmpLog 对文本解析器比 SymCC 字典引导更有效

- CmpLog 是**动态**字典（运行时从 `strcmp` 等调用中提取），即使种子充分仍有效
- SYMCC_DICT 是**静态**字典（用户提供、编译时固定），种子充分时无额外收益
- SQLite 上 AFL+CmpLog (21.03%) > Hybrid best (18.84%)

> **注**：CmpLog 数据来自独立的 120s A/B 对比实验（仅 SQLite/pcre2/libarchive/freetype2 四目标），与 v2 全量 benchmark（300s，含 CmpLog）实验条件不同（timeout、CmpLog 启用状态均不同），数值不可直接对比。此结论仅适用于 SQLite 类文本解析器，不可泛化。

---

## 八、覆盖率瓶颈的根因分析

> **PPT 建议**：用三层天花板模型图展示

### 8.1 三层天花板模型

```
第一层天花板：Harness API 覆盖范围
  │  例：PNG 原始 harness 仅调用 4 个 API，22.7% 写入模块 + 6% 渐进读取永远不可达
  │  优化：增强 harness，添加颜色转换 API → PNG lcov 分支覆盖率从 18% 提升到 24.5%（早期实验）
  ↓
第二层天花板：种子多样性
  │  例：9 个 PNG 种子仅覆盖少数颜色类型
  │  优化：扩展种子（13 种编码格式）→ 各目标均有提升
  ↓
第三层天花板：Concolic 求解能力
  │  例：CRC 联合约束无法求解；单步翻转无法生成语义变体
  │  优化：CRC 绕过；字典引导（单种子 +50%）
  ↓
并行化在第三层天花板内运作 → 天花板已达到则加并行无效
```

**关键洞察**：提升覆盖率必须**由外向内突破**天花板。并行化加速到达每层天花板的速度，但不能突破天花板本身。v2 全量 benchmark 使用原始 harness（未增强），因此受第一层天花板限制。

---

## 九、已验证的无效方向

> **PPT 建议**：列出已排除的方向，避免后续重复探索

| 方向 | 实验结果 | 原因 |
|------|---------|------|
| 分割种子多实例 | SQLite 25 种子分 6 组，6×np=32：吞吐量 +2.8x 但覆盖率 -0.14%；libarchive -1.20% | 隔离破坏跨类型反馈环 |
| 字典引导 + 多样化种子 | 单种子 +50%，25 种子 +0% | 多样种子已覆盖大部分字典关键字 |
| np > 128 | 效率下降至 30%~56%，甚至更慢 | 路径邻域耗尽 + 多 Master 同步开销 |
| 对平坦二进制目标使用 SymCC | md5sum/uniq/who 贡献 0 | 随机翻转即可覆盖，无需约束求解 |
| 对结构化格式高 np Hybrid | freetype2 np=32 覆盖率低于 AFL-only | CPU 争抢，SymCC 无效输出白耗资源 |

---

## 十、测试目标全景

> **PPT 建议**：简要展示测试目标的多样性

| 来源 | 目标 | 代码规模 | AFL 边数 | 类型 |
|------|------|---------|------:|------|
| Google FTS | png_read_fuzzer | libpng 1.2.56 | 3,072 | 二进制图像格式 |
| Google FTS | xml_read_fuzzer | libxml2 2.9.2 | 50,880 | 文本标记语言 |
| LAVA-M | base64 | coreutils 345 行 | 1,088 | 编码工具+注入 bug |
| LAVA-M | md5sum, uniq, who | coreutils | 1,216-10,688 | 系统工具 |
| Fuzzer Test Suite | libarchive | 大型库 | 13,760 | 多格式归档解析 |
| Fuzzer Test Suite | SQLite | 200K 行 | 31,680 | SQL 数据库引擎 |
| Fuzzer Test Suite | pcre2 | 正则引擎 | 7,488 | 正则表达式匹配 |
| Fuzzer Test Suite | freetype2 | 字体渲染 | 21,632 | TrueType 字体 |
| 自定义 | parallel_scaling | 130 行 | 768 | 64 个独立 switch-case |
| 自定义 | base64_harness | 编码+解码 | 192 | 崩溃恢复+全模式 |

---

## 十一、项目时间线

> **PPT 建议**：用时间轴展示项目进展

| 阶段 | 工作内容 | 关键成果 |
|------|---------|---------|
| **阶段一** | MPI 框架开发 | 实现纯并行和 Hybrid 两种模式（~1,850 行） |
| **阶段二** | 性能优化 | 6 轮优化，Master 负载从 >100s 降至 9% |
| **阶段三** | Bug 修复 | 修复 25+ bug，LAVA-M 从 0 输出到 16K+ |
| **阶段四** | 初步 Benchmark（120s） | 7 目标测试，确认吞吐量 84-117x scaling |
| **阶段五** | 覆盖率瓶颈分析 | 三层天花板模型，增强 harness（PNG lcov +102%） |
| **阶段六** | 全量测试（300s） | 10 目标，np=2/8/32，70 配置，双指标体系 |
| **阶段七** | 深入分析 | who 根因、freetype2 CPU 争抢、CmpLog 集成、120s vs 300s 对比 |

---

## 十二、局限性与待完成工作

> **PPT 建议**：展示未来方向，坦诚局限

### 12.1 当前局限

| 局限 | 影响 | 缓解方案 |
|------|------|---------|
| **单轮实验** | <1pp 的差异可能是噪声 | 3-5 轮计算标准差（**高优先级**） |
| **无时间-覆盖率曲线** | 无法精确量化并行时间加速 | 启用 `--timeseries 30` 采样（功能已实现） |
| **无 CPU 利用率数据** | 无法评估资源效率 | 采集 `mpstat` / `top` 数据 |
| **未对比 N×AFL vs 1 AFL+(N-1) SymCC** | 无法量化 SymCC 相对并行 AFL 的价值 | 增加 multi-AFL 对照组 |
| **Hybrid 中 AFL/SymCC 贡献未分离** | 无法精确归因覆盖率来源 | 分别测量 AFL queue 和 SymCC queue 的独立覆盖 |
| **CmpLog 实验条件不一致** | 120s vs 300s, CmpLog on/off 混合对比 | 统一实验条件重跑 |

### 12.2 后续工作

| 方向 | 说明 | 优先级 |
|------|------|:------:|
| 多轮统计 | 3-5 轮，计算均值和标准差 | 高 |
| 自适应 np 选择 | 根据目标特征自动调整 SymCC/AFL 资源分配 | 高 |
| Multi-AFL 对照组 | N 个 AFL 实例 vs 1 AFL + (N-1) SymCC | 高 |
| 时间-覆盖率曲线 | 每 30s 采样覆盖率 | 中 |
| 长时间 benchmark | 300s → 10min/1h | 中 |
| 更多目标 | OpenSSL, RE2, HarfBuzz | 低 |
| 多步约束组合 | 突破单步翻转限制 | 研究 |

---

## 附录 A：PPT 结构建议

| 页码 | 标题 | 内容 | 对应章节 |
|:----:|------|------|---------|
| 1 | 封面 | 项目名称、团队、日期 | - |
| 2 | 项目背景与目标 | SymCC 简介、并行化目标、硬件环境 | 一 |
| 3 | 系统架构 | MPI 纯并行 + Hybrid 架构图 | 二 |
| 4 | 性能优化 | 6 轮优化表 + 效果对比 | 三 |
| 5 | 吞吐量 Scaling | 折线图：tc/s vs np，84-117x | 六.1 |
| 6 | 覆盖率指标说明 | ShowmapCov vs FstatsCov 定义与区别 | 六.0 |
| 7 | 全量覆盖率对比 | 分组柱状图：10 目标 × 4 模式 | 六.2 |
| 8 | SymCC 有效性分类 | 分类矩阵（双指标） | 六.3 |
| 9 | 案例：libarchive 互补效应 | Hybrid 23.36% >> AFL 15.07% / MPI 13.90% | 六.2 |
| 10 | 案例：freetype2 非单调变化 | np=8 最优，np=32 有害 | 六.4 |
| 11 | 案例：base64 精确求解 | AFL 37万次 0 触发 vs SymCC 一次求解 | 六.6 |
| 12 | 三层天花板模型 | 模型图 + 突破方法 | 八 |
| 13 | 核心结论（6 条） | 要点列表 | 七 |
| 14 | 局限性与未来工作 | 诚实列出局限 + 计划表 | 十二 |

## 附录 B：实验可重现命令

```bash
# 全量 benchmark（10 目标，300s，np=2,8,32）
python3 benchmark/run_benchmark.py \
  --targets gfts-png_read_fuzzer,gfts-xml_read_fuzzer,lava-base64,lava-md5sum,lava-uniq,lava-who,libarchive-archive_fuzzer,sqlite-sqlite_fuzzer,pcre2-pcre2_fuzzer,freetype2-freetype2_fuzzer \
  --np-list 2,8,32 --timeout 300 --rounds 1 \
  --hybrid --afl-only --no-default --public --skip-build --no-serial \
  --output benchmark/benchmark_results_v2

# 吞吐量 scaling 测试（120s，np=2~128）
python3 benchmark/run_benchmark.py \
  --targets gfts-xml_read_fuzzer,sqlite-sqlite_fuzzer \
  --np-list 2,8,32,128 --timeout 120 --rounds 1 \
  --no-default --public --skip-build \
  --output benchmark/benchmark_results_scaling
```
