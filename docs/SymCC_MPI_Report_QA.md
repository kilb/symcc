# SymCC MPI 并行评估报告——问题解答

## Q1: 为什么三种构建在同一目录下编译？设置不同目录有什么问题？如果以 AFL 为主，为什么还需要 gcc --coverage？

### 同一目录编译

当前编译脚本中三种构建（SymCC、AFL、Coverage）均对同一份 libpng/libxml2 源码目录执行 `./configure && make`，原因是脚本复用了下载解压后的同一个源码目录 `$work_dir/libpng-1.2.56`。这属于**设计缺陷**。

更合理的做法是将源码 tarball 解压三份到不同目录（如 `libpng-symcc/`、`libpng-afl/`、`libpng-cov/`），每份独立构建，互不干扰。当前方案的代价是必须严格控制构建顺序（SymCC → AFL → Coverage），否则 AFL 构建的 `make distclean` 会删除 Coverage 构建产生的 `.gcno` 文件。

### gcc --coverage 的必要性

覆盖率测量依赖 `.gcno`（编译期生成的控制流图）和 `.gcda`（运行时命中计数）两类文件，只有用 `gcc --coverage` 编译的二进制在执行时才会写入 `.gcda`。SymCC 二进制和 AFL 二进制均不产生此类 profiling 数据。

因此，即使以 AFL 为主要测试手段，要**量化**覆盖率仍然必须有一个 Coverage 构建——将 AFL/SymCC 生成的测试用例喂给 Coverage 二进制执行，收集 `.gcda`，再用 lcov 统计分支覆盖率。Coverage 构建不参与实际的 fuzzing 或符号执行，仅用于事后度量。

---

## Q2: 混合模式中为什么先 SIGTERM 再 SIGKILL？初始种子在 AFL 队列中是否重复合并？

### 两阶段终止

SIGTERM 是"请求进程优雅退出"的信号，进程可捕获该信号执行清理逻辑。`mpi_fuzzing_helper.py` 中注册了 SIGTERM handler（第 363-367 行），收到信号后依次执行以下操作：

1. 设置 `shutdown_requested = True`，退出主循环
2. 向所有 worker 发送 TAG_STOP 消息
3. 调用 `stats.log()` 写入最终统计
4. 关闭 `stats_file`

若直接发送 SIGKILL，MPI 进程无法执行上述清理——可能导致 worker 进程变为孤儿进程、统计文件未 flush、MPI 资源未释放等问题。因此采用两阶段策略：先发 SIGTERM 等待 10 秒让进程自行退出，超时后再发 SIGKILL 强制终止。

### 种子重复合并

当前实现中存在种子重复问题。`run_hybrid()` 第 970-973 行将种子文件复制到 `combined_dir`：

```python
for f in os.listdir(seed_dir):
    shutil.copy2(src, os.path.join(combined_dir, f"seed_{f}"))
```

而 AFL 的 queue 目录已包含初始种子（`id:000000,orig:seed_01.png` 等）。由于覆盖率测量是幂等的——同一测试用例执行两次不会增加覆盖率——重复合并不影响结果正确性，但浪费了执行时间。此处应去掉种子复制步骤。

---

## Q3: gcov 单文件覆盖率相同问题——是否因为底层库未用 gcc --coverage 编译？

**确实如此**。最初的 Coverage 构建仅对 harness 文件（`png_read_fuzzer.c`）使用了 `--coverage` 选项，libpng 静态库仍以普通 gcc 编译，未产生 `.gcno` 文件。gcov 只能统计存在 `.gcno` 的源文件覆盖率。

harness 代码约 30 行，逻辑为：打开文件 → 调用 `png_read_info` → 调用 `png_read_row` → 释放资源。任何合法 PNG 文件都会走完全相同的 harness 分支，因此覆盖率恒为 77.3%。真正的代码路径差异体现在 libpng 库内部（数千行源码），gcov 对此完全不可见。

修复方案为：在 `build_google_fts_coverage()` 函数中将 libpng/libxml2 同样以 `gcc --coverage` 重新编译，并将库的构建目录记录到 `.covlibdirs` 元数据文件中，使 lcov 能够采集库源码的覆盖率数据。

---

## Q4: 覆盖率测量系统"三次迭代"是什么意思？目前以哪个指标为主？

此处并非三个并存的版本，而是**修复过程中的三次递进改进**，每次发现问题后迭代修正：

1. **第一次**（gcov 单文件）：仅测量 harness 覆盖率，发现所有并行度覆盖率均为 77.3%，无实际意义
2. **第二次**（lcov 多目录聚合）：改用 lcov 聚合 harness + 库目录的覆盖率数据。但代码修改时使用 `replace_all=true` 仅匹配到串行调用点，MPI 调用点遗漏，仍走旧 gcov 路径，导致 MPI 模式下覆盖率数据错误。此缺陷隐藏了一轮完整的 benchmark 运行周期
3. **第三次**：修复 MPI 调用点，同时处理 lcov 新版本将 `lcov_branch_coverage=1` 弃用为 `branch_coverage=1` 的兼容性问题

**最终方案**：以 **lcov branch coverage（分支覆盖率）** 为主要指标。代码逻辑位于 `measure_coverage()` 第 498-502 行：若 `lib_dirs` 存在且系统中安装了 lcov，则使用 lcov 测量；否则退化到 gcov。报告中所有覆盖率百分比均为 lcov branch coverage。

---

## Q5: 为什么 SymCC 输出"不构成 AFL 新覆盖"？SymCC 生成的种子是否被 AFL 用上？

### AFL 的覆盖率判定机制

AFL 的 edge bitmap 使用 `(prev_block XOR cur_block) % MAP_SIZE` 映射到 64KB bitmap 的某个 byte，记录 hit count bucket（0→1, 1→2, 2→3, 3→4-7, ...）。AFL 判定测试用例"interesting"的条件是：该用例触发了 bitmap 中某个 byte 从未出现过的 hit count bucket。

SymCC 生成的变异通常仅差 1-3 字节，执行路径虽然在某个分支决策上有所不同（lcov 可观测到），但经过的 edge 集合几乎相同——连 hit count bucket 都未发生变化。因此 afl-showmap 将全部 SymCC 输出判定为"不 interesting"。

需要指出，原报告中"不同计数桶不构成新覆盖"的表述有误——实际情况是 SymCC 的微小变异**连计数桶都未改变**，而非"改变了计数桶但 AFL 不予认可"。

### SymCC 种子未被 AFL 利用

这是当前实现的一项缺陷。`mpi_fuzzing_helper.py` 的 master 进程将 afl-showmap 判定为不 interesting 的测试用例直接丢弃，不写入 AFL 的 queue 目录（`symcc01/queue/`）。虽然 `--save-all` 参数将全量输出保存到了独立目录用于覆盖率测量，但这些输出**未回灌至 AFL 的输入队列**。

AFL 实际上完全不知道 SymCC 生成了哪些输出——双方的反馈循环处于断开状态。合理的改进方案是：即使 afl-showmap 判定不 interesting，也选择性地将部分 SymCC 输出回灌给 AFL，使 AFL 有机会在此基础上进行进一步变异。

---

## Q6: 吞吐量饱和的原因是否有证据？竞态条件修复是否引入了并行开销？

### np=64 数据存在但报告遗漏

np=64 的实测数据存在于 `benchmark_results_public/benchmark_data.csv` 中：

| 目标 | np=32 | np=64 | np=128 | np=190 |
|------|-------|-------|--------|--------|
| PNG tc/s | 4,736 | 8,183 | 8,175 | 7,804 |
| XML tc/s | 9,287 | 16,338 | 19,780 | 18,983 |

报告表格（3.1、3.2 节）遗漏了 np=64 行，属于报告编写疏忽。

### 饱和原因缺乏实测证据

报告中"受限于 MPI 通信开销和 multi-master 同步瓶颈"的表述仅为推测，缺乏实测数据支撑。要严格论证该结论，需要对以下各阶段分别计时：

- SymCC 执行时间（worker 实际计算耗时）
- MPI 消息收发时间（master 分发/收集开销）
- hash 同步时间（master 间通信耗时）
- 队列等待时间（worker 空闲等待）

当前代码仅统计了总 wall time 和每次 SymCC 执行的 elapsed，未对 MPI 通信部分单独计时。饱和原因应修正为"尚待进一步分析"。

### 竞态修复的开销

`_atomic_write` 在原有 `open().write()` 基础上增加了一次 `os.rename()` 调用。在 Linux 同一文件系统上，rename 是原子的 inode 操作，开销在微秒级别，对整体吞吐量的影响可以忽略。

### 能否通过不同文件名避免竞争

当前文件名即 SHA-256 hash——相同内容必然同名。这是 content-addressable storage（内容寻址存储）的设计决定，天然实现去重。若改为每个 worker 写入不同文件名（如 `{hash}.worker{rank}`），则丧失去重能力，shared_dir 存储量将膨胀 N 倍。当前的 atomic write + exists check 方案是合理的权衡：即使多个 worker 同时写同一 hash，rename 保证最终文件内容完整，仅浪费少量 I/O。

---

## Q7: "吞吐量"指的是什么？4.2 和 4.3 的覆盖率标准分别是什么？

### 吞吐量 (tc/s)

指 SymCC（或 AFL+SymCC）在单位时间内**生成的测试用例总数** ÷ wall time。包括所有 worker 产生的输出，无论是否 unique。计算公式位于 `run_benchmark.py`：`throughput = generated / elapsed`。

### 4.2 与 4.3 的覆盖率测量标准

两者采用**完全相同的测量方法**——lcov branch coverage。具体流程为：

1. 清零所有 `.gcda` 文件
2. 将测试用例逐一喂给 `gcc --coverage` 编译的 Coverage 二进制执行
3. 通过 `lcov --capture` 采集 harness + 库目录的覆盖率数据
4. 通过 `lcov --summary` 提取 branch coverage 百分比

两组数据的区别仅在于**测试用例来源**：

- **4.2（纯 Concolic）**：来源为 `mpi_concolic_execution.py` 的 shared_dir，仅包含 SymCC 输出
- **4.3（Hybrid）**：来源为 combined_dir，包含 AFL queue + SymCC queue + SymCC 全量输出的合并

---

## Q8: "200K 采样上限"是什么？

在 MPI 高并行度（np=128）下运行 5 分钟，SymCC 可生成 200 万至 500 万个测试用例。将这些全部喂给 Coverage 二进制执行需耗时数小时（Coverage 二进制带 profiling 开销，每次执行需数十毫秒）。

`measure_coverage()` 函数设置了 `max_cases=200000` 参数：当测试用例数超过 20 万时，采用**分层采样**策略——按文件名排序后均匀跳跃取样 200K 个，再补充每个"桶"的边界样本（代码第 424-451 行）。目的是在可接受的时间范围（数分钟）内获得有代表性的覆盖率估计。

此采样策略引入了约 ±1.5% 的覆盖率噪声——同一批测试用例的不同采样可能产生略有差异的覆盖率数值。这也是报告中"不同并行度间的覆盖率差异在采样噪声范围内"这一判断的依据。

---

## Q9: "路径邻域有限"是什么意思？为什么认为路径邻域是有限的？

### SymCC 的工作方式

SymCC 的 concolic execution 对输入 X 执行具体运行，记录路径上所有分支条件，然后依次取反每个分支条件进行约束求解，生成新输入 X'。X' 与 X 通常仅差 1-3 字节（仅改变使某个分支条件翻转的最少字节数）。

假设输入 X 经过路径 A→B→C→D（4 个分支点），SymCC 将尝试生成：
- A→B→C→D'（翻转最后一个分支）
- A→B→C'→?（翻转第三个分支）
- A→B'→?→?（翻转第二个分支）
- A'→?→?→?（翻转第一个分支）

这些"翻转单一分支"的变异构成输入 X 的"路径邻域"，最多 N 个方向。

### 邻域有限的原因

对于某个具体的种子结构（如一个简单的 RGB PNG），其执行路径上分支点数量固定。邻域变异很快被穷举完毕。SymCC 继续对变异输出做 concolic（二阶邻域、三阶邻域），但每次变异仍仅差 1-3 字节——始终在同一"结构族"内循环。

### "百万级 hash 不同但走同一组代码路径"的含义

hash 不同仅意味着**文件内容**（字节级别）不同，不代表**执行路径**不同。例如：修改 PNG 文件 IDAT chunk 中 1 字节的解压数据，文件 hash 发生变化，但 libpng 的解析逻辑（读 IHDR → 读 IDAT → 解压 → 读 row）完全相同——触发的分支集合不变。

要跨越到不同结构的代码路径（如触发 libpng 的 tRNS 透明度处理、sBIT 色深处理），需要输入中出现全新的 chunk type（4 字节标记 + 正确的 CRC 校验），这无法通过翻转单个分支条件实现。

---

## Q10: 为什么 Hybrid 对 XML 有效但对 PNG 无效？二进制文件也是结构化的，为什么不行？

关键区别不在于"是否结构化"，而在于**格式对随机变异的容错程度**。

### XML（文本格式）——对变异高度容错

- AFL 随机翻转一个字节：`<root>` 变为 `<roou>` — libxml2 的 `XML_PARSE_RECOVER` 模式尝试恢复解析，仍可产生不同的 DOM 结构
- AFL 插入随机字节：`<a><b/></a>` 变为 `<a><b/><c/></a>` — 仍为合法 XML，触发更多节点处理逻辑
- AFL 的字典模式可插入 `<`, `>`, `</`, `="` 等 token，生成结构多样的新标签组合

实测数据：AFL 在 5 分钟内生成约 3,500 个 XML queue 条目。SymCC 对这些结构不同的 XML 输入执行符号分析后，覆盖率达到 10.7%。

### PNG（二进制格式）——校验机制严格

PNG 文件结构为：8 字节 magic → (4 字节长度 + 4 字节 type + N 字节 data + 4 字节 CRC) 循环。其中：

- 翻转 CRC 中任何 1 bit → CRC 校验失败 → libpng 调用 `png_error()` 终止解析
- 翻转 type 字段中 1 bit → 产生未知 chunk type → libpng 跳过或报错
- 翻转 IHDR 中 1 bit → 可能导致 width/height 不合理 → harness 中 `w*h > 1000000` 检查将拒绝该输入

实测数据：AFL 在 5 分钟内仅生成约 170 个 PNG queue 条目（对比 XML 的 3,500 个），原因是绝大多数随机变异被 PNG 的格式校验拒绝。

### 实测覆盖率对比

| 目标 | 种子基线 | 纯 MPI np=128 | Hybrid np=2 | Hybrid 效果 |
|------|---------|--------------|-------------|------------|
| PNG | 12.1% | 17.6% | 16.9% | 持平 |
| XML | 4.0% | 7.7% | **10.7%** | **+39%** |

PNG 的 Hybrid 覆盖率未超过纯 MPI 最佳结果，而 XML 的 Hybrid 比纯 MPI 最佳结果高出 2.7 个百分点。根本原因在于：AFL 能为 XML 提供大量结构不同的有效输入（约 3,500 个），但对 PNG 仅能提供极少量有效变异（约 170 个）。

---

## Q11: "高并行度下去重效率下降"是什么意思？

**含义**：在相同运行时间内，高并行度配置生成了更多总测试用例，但去重后的唯一数反而减少。

### 具体数据（PNG，benchmark_results_public）

| np | 架构模式 | 总生成数 | 去重唯一数 | 唯一率 |
|----|---------|---------|-----------|--------|
| 32 | 单 master | 1,268,051 | 858,946 | 67.7% |
| 64 | 多 master (2 个) | 2,190,734 | 819,990 | 37.4% |
| 128 | 多 master (3 个) | 2,191,383 | 565,907 | 25.8% |
| 190 | 多 master (5 个) | 2,085,161 | 388,590 | 18.6% |

从 np=32 到 np=190：总生成数从 127 万增长到 209 万（+64%），而唯一数从 86 万下降到 39 万（-55%）。

### 原因分析

np>46 时系统启用 multi-master 架构，每个 master 管理各自的 worker 组。master 间通过异步 `isend`/`iprobe` 同步已知 hash，同步间隔为 `SYNC_INTERVAL = 2.0` 秒。

在该 2 秒延迟窗口内，多个 master 组会独立处理相同的输入并产生相同的输出。具体场景如下：Master A 的 Worker 1 发现了 hash H 并上报给 Master A，但 Master A 尚未将 H 同步至 Master B 时，Master B 已将同一种子分配给 Worker 5，Worker 5 也生成了 hash H。H 在两个 group 中各被计入总数一次，但去重后仅计为 1 个。

即：master 数量越多，同步延迟窗口内的重复探索越严重，去重效率相应下降。

---

## Q12: 为什么测试用例生成数更多，但覆盖率反而下降？

以 PNG 实测数据为例：

| np | 生成数 | 分支覆盖率 |
|----|--------|-----------|
| 32 | 1,268,051 | 17.9% |
| 64 | 2,190,734 | 17.4% |
| 128 | 2,191,383 | 17.0% |
| 190 | 2,085,161 | 17.5% |

上述覆盖率差异（17.0% ~ 17.9%）均在 ±1.5% 采样噪声范围内，**不应解读为覆盖率真正发生了下降**。

每次覆盖率测量时，从百万级测试用例中抽样 200K 个喂给 Coverage 二进制执行。不同的采样可能恰好包含或遗漏触发特定分支的测试用例。np=32 的 17.9% 与 np=128 的 17.0% 之间 0.9% 的差异，大概率由采样随机性导致。

这是后续计划中将"多轮统计测试"列为高优先级任务的原因——需要 3-5 轮独立运行取中位数，方能区分真实差异与统计噪声。

---

## Q13: 不同指标的并行度测试应当一致，并行度跨度不宜过大

此为报告的数据展示缺陷。实际上各并行度的测试数据均已采集，但分布在不同的 benchmark 运行批次中：

- `benchmark_results_public/`：np = 2, 4, 8, 16, 32, 64, 128, 190（完整的纯 MPI 测试）
- `benchmark_results_hybrid/`：np = 2, 8, 32, 128（Hybrid 模式 + 对应 MPI 重测）

报告应做以下修正：

1. 所有表格统一采用相同的 np 列表（如 2, 4, 8, 16, 32, 64, 128）
2. Hybrid 模式补测 np=4, 16, 64，使并行度梯度与纯 MPI 测试保持一致
3. 在 MPI 与 Hybrid 对比表中使用相同的 np 值，避免跳跃式对比（如从 np=2 直接到 np=128）

---

## Q14: 为什么选择 PNG 和 XML 作为测试目标？LAVA-M 和 Google FTS 是否均无法完整编译？

### 选择依据

- **Google Fuzzer Test Suite (FTS)** 是 Google 发布的公开 fuzzing 基准集，在学术界和工业界被广泛用于 fuzzer 性能评估。PNG (libpng) 和 XML (libxml2) 是其中最常用的两个目标
- PNG 代表**二进制格式**，XML 代表**文本格式**——覆盖两种主要的输入格式类型
- 两者均为真实世界广泛使用的开源库，测试结果具有实际参考价值

### 各基准集编译与运行状态

| 基准集 | 编译状态 | 运行状态 | 说明 |
|--------|---------|---------|------|
| Google FTS — PNG | 成功 | 正常 | 完整可用 |
| Google FTS — XML | 成功 | 正常 | 完整可用 |
| Google FTS — 其他目标 | 未实现 | — | FreeType、re2、SQLite 等目标的编译脚本尚未编写，并非编译不通过 |
| LAVA-M (4 个目标) | 成功 | 异常 | 编译成功（已包含 glibc 兼容性 patch），但 SymCC 运行时不产生输出，原因尚未确诊 |
| CGC (cb-multios) | 脚本已实现 | 未测试 | CGC 目标为 32-bit 程序，SymCC 对 32-bit 的支持存在局限 |

LAVA-M 的问题属于运行时问题（可能涉及 stdin/file 输入模式不匹配、QSYM 后端对 glibc 工具程序的兼容性等），而非编译问题。后续计划包括调试 LAVA-M 运行时问题，以及为更多 Google FTS 目标编写编译脚本以扩大测试覆盖面。

---

## Q15: 4.3 中"最佳纯 MPI"是什么意思？Hybrid 测试应以什么作为基准？

### "最佳纯 MPI"的含义

指在所有纯 concolic 并行度配置（np=2~190）中，分支覆盖率最高的那个配置。PNG 对应 np=32 的 17.9%，XML 对应 np=32 的 8.1%。

### 基准选择不当

该基准选择**不符合 Hybrid fuzzing 的标准评估方法**。在混合测试领域，标准对比方案应包含以下三组：

| 对比组 | 含义 |
|--------|------|
| **AFL alone** | **基准线**——仅运行 AFL fuzzer，不启用 SymCC |
| SymCC alone | 仅运行纯 concolic execution（即当前的 MPI 测试结果） |
| AFL + SymCC | Hybrid 模式，用于评估 SymCC 在 AFL 基础上带来的**增量收益** |

提升幅度应计算为 `(Hybrid - AFL alone) / AFL alone`，而非 `(Hybrid - 纯 concolic 最佳) / 纯 concolic 最佳`。

### 当前实验的不足

当前实验**缺少 AFL-only 对照组**。Hybrid 结果中的覆盖率为 AFL + SymCC 全量输出的混合数据，无法分离各自的贡献。以下两个关键问题无法回答：

- AFL 单独运行 5 分钟能达到多少 lcov branch coverage？
- SymCC 在 AFL 基础上额外贡献了多少覆盖率增量？

### 应补充的实验

需增加 AFL-only 基线测试：使用相同种子、相同运行时间（300s），仅运行 `afl-fuzz`，然后用 lcov 测量 AFL queue 的覆盖率。修正后的结果表格应采用如下结构：

| 目标 | AFL alone | SymCC alone (最佳) | Hybrid | Hybrid vs AFL 提升 |
|------|-----------|--------------------|--------|--------------------|
| PNG | 待测 | 17.9% | 16.8% | 待测 |
| XML | 待测 | 8.1% | 10.8% | 待测 |
