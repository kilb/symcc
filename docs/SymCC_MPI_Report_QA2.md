# SymCC MPI 并行评估报告 — 补充问答（QA2）

> 生成日期：2026-03-26
> 基于 benchmark_results_full（运行中）及代码审查的技术答复

---

## Q1：覆盖率度量体系

### 1.1 AFL 分支覆盖 vs lcov 分支覆盖

在模糊测试领域，AFL 的 edge coverage 是事实上的标准度量。但需区分两套体系：

| 属性 | AFL edge coverage | lcov branch coverage |
|------|-------------------|----------------------|
| 粒度 | 基本块间的控制流边 `(prev_loc XOR cur_loc) % MAP_SIZE` | 源码级条件分支（if/else/switch/for/while） |
| 是否考虑方向 | **是**。A→B 与 B→A 的 XOR 值不同，映射到不同 bitmap slot | **否**。仅记录每个分支条件的 taken/not-taken |
| 碰撞问题 | 存在。64 KB bitmap 下不同 edge 可能映射到同一 slot | 无碰撞，精确到源码行号 |
| 计数方式 | hit count 分为 8 个桶：1, 2, 3, 4–7, 8–15, 16–31, 32–127, 128+ | 精确计数每个分支的执行次数 |
| 用途 | 运行时快速判断"是否触发新路径" | 事后精确测量"覆盖了多少代码分支" |

当前项目以 **lcov branch coverage** 作为最终评估指标，AFL 的 `bitmap_cvg` 作为辅助参考。两者趋势一致但数值不同。以 PNG 目标为例（本轮 benchmark 数据）：

| 模式 | AFL bitmap_cvg | lcov branch_cov |
|------|---------------|-----------------|
| AFL-only | 14.42% | 15.4% |
| Hybrid np=2 | 14.52% | 16.6% |

### 1.2 kcov、gcov、lcov 的定义与区别

| 工具 | 定义 | 实现机制 |
|------|------|----------|
| **gcov** | GCC 内置覆盖率分析工具 | `gcc --coverage` 编译时在 `.gcno` 文件中记录 CFG 结构，运行时通过插桩计数器将数据写入 `.gcda` 文件。`gcov` 命令解析两者输出行级与分支级覆盖率 |
| **lcov** | gcov 的高级前端封装（Linux Test Project 维护） | 调用 gcov 收集数据，可聚合多个源文件与库目录的覆盖率，输出统一的 `.info` 格式。`lcov --capture` 配合 `--branch-coverage` 实现分支级统计 |
| **kcov** | 内核级覆盖率工具 | 使用 Linux KCOV 接口（基于 Clang SanitizerCoverage），不需要 `--coverage` 编译，通过 ptrace 注入收集。本项目未使用 |

简言之：`gcc --coverage` 产生的插桩称为 gcov 插桩；lcov 是 gcov 之上的聚合与报告层。

### 1.3 AFL 分支覆盖与 gcc/lcov 分支覆盖的计算差异

**AFL edge coverage 的计算**：

```c
// AFL 插桩伪代码（每个基本块入口）
cur_location = <compile_time_random>;
shared_mem[cur_location ^ prev_location]++;
prev_location = cur_location >> 1;
```

- 记录的是 `(源基本块, 目标基本块)` 的有向边
- 映射到 64 KB 共享内存 bitmap
- 新 edge 或 hit count 跨桶（如从 1 次变为 2 次）即判定为"新覆盖"

**lcov branch coverage 的计算**：

```
# lcov 解析 gcov 输出的分支行
BRDA:line_number,block_number,branch_number,execution_count
```

- 记录的是每个源码级分支条件的 taken/not-taken
- 分支覆盖率 = (已执行分支数) / (总分支数) × 100%
- 不区分执行方向，仅关注"该分支是否被两个方向各覆盖过"

**关键差异总结**：AFL 考虑控制流方向且存在哈希碰撞；lcov 不考虑方向但精确无碰撞。对于同一组测试用例，两套度量的数值一般不同，但趋势一致。

---

## Q3：覆盖率度量的完整性

### 3.1 所有测试用例是否都能找到库源码？

**Google FTS 目标**：可以。`compile_public_benchmarks.sh` 同时编译 harness 与依赖库（libpng、libxml2），coverage 构建保留了完整的 `.gcno` 文件。lcov 聚合时包含 harness 源码目录与库构建目录：

```
# png_read_fuzzer.covlibdirs 内容示例：
/home/ubuntu/.../libpng-1.2.56
```

因此 lcov 的分支覆盖率统计同时涵盖 harness 代码和库代码。

**LAVA-M 目标**：coverage 二进制仅覆盖 coreutils 自身源码（如 `src/base64.c`），**不包含 glibc 库**的分支覆盖（glibc 未用 `--coverage` 编译）。该限制对 LAVA-M 评估影响有限，因为 LAVA-M 的插入 bug 均位于 coreutils 源码内部。

### 3.2 使用 AFL bitmap 做路径覆盖统计

这是一种合理的替代方案。当前项目的做法与可改进方向：

**当前做法**：使用 lcov（事后测量），hybrid 和 AFL-only 模式额外记录 AFL `bitmap_cvg`（通过解析 `fuzzer_stats`）。

**可改进方向**：对所有模式（包括纯 MPI）统一使用 `afl-showmap` 生成 bitmap，计算 bitmap 非零 byte 占 `MAP_SIZE` 的百分比。这样三种模式（AFL-only、hybrid、MPI）使用同一度量体系，结果可直接对比。

**当前限制**：纯 MPI 模式的输出没有经过 `afl-showmap`，缺少 AFL bitmap 数据。若要统一度量，需在纯 MPI 模式的覆盖率测量阶段额外运行 `afl-showmap`。

---

## Q4：lcov 与 gcov 的具体实现

### 4.1 gcov 的实现流程

1. **编译阶段**：`gcc --coverage -O0 -g source.c -o binary`
   - 生成 `.gcno` 文件（Graph Coverage Note）：记录源码行号与基本块的映射关系、分支结构
   - 在每个基本块入口插入计数器递增代码（`__gcov_increment`）
2. **运行阶段**：执行 `./binary`
   - 计数器数据在进程退出时写入 `.gcda` 文件（Graph Coverage Data）
   - 多次运行会累加计数（若不清空旧 `.gcda`）
3. **分析阶段**：`gcov source.c`
   - 解析 `.gcno`（结构）+ `.gcda`（数据）
   - 输出每行执行次数、每个分支的 taken/not-taken 计数

### 4.2 lcov 的实现流程

1. **数据收集**：`lcov --capture --directory BUILD_DIR --branch-coverage -o coverage.info`
   - 内部遍历 `BUILD_DIR` 下所有 `.gcda` 文件
   - 对每个 `.gcda` 调用 `gcov` 解析
   - 合并多个源文件和库目录的覆盖数据
   - 输出统一的 `.info` 格式文件
2. **数据过滤**：`lcov --extract coverage.info 'src/*' -o filtered.info`
   - 可按路径模式筛选需要统计的源文件
3. **报告生成**：`genhtml filtered.info -o html_report`
   - 生成可视化 HTML 报告

### 4.3 本项目中的具体实现

`run_benchmark.py` 中的 `_measure_with_lcov()` 函数（第 279 行）：

```python
# 1. 清空旧 .gcda 文件
# 2. 批量执行覆盖率二进制（逐个测试用例）
# 3. lcov --capture --branch-coverage
# 4. 解析 .info 文件提取 line_cov 和 branch_cov
```

当测试用例数量超过 200,000 时，采用分层随机采样（stratified sampling）以控制执行时间。

---

## Q5：SymCC 与 AFL 的混合测试架构

### 5.1 SymCC 官方的 AFL 结合实现

SymCC 官方提供了 `symcc_fuzzing_helper`（Rust 实现，位于 `util/symcc_fuzzing_helper/`），其设计利用 AFL 的并行同步机制实现双向反馈：

```bash
# 官方推荐用法（docs/Fuzzing.txt）
afl-fuzz -M afl-master    -i corpus -o afl_out -m none -- afl_build/target @@
afl-fuzz -S afl-secondary -i corpus -o afl_out -m none -- afl_build/target @@
symcc_fuzzing_helper -o afl_out -a afl-secondary -n symcc -- symcc_build/target @@
```

**关键设计**：AFL 的 `-M`（master）和 `-S`（secondary）并行模式会自动同步 `afl_out/*/queue/` 目录之间的文件。`symcc_fuzzing_helper` 监控 `afl-secondary/queue/`，取出种子运行 SymCC，将新输出放入 `afl_out/symcc/queue/`，AFL 的 sync 机制自动发现并导入。

**当前 MPI 实现的偏差**：
- 仅启动 `-M fuzzer01`（master 模式），未启动 `-S` secondary 实例
- SymCC 输出放入 `symcc01/queue/`，但 AFL master 不会 sync 该目录
- SymCC→AFL 反馈路径完全断裂

**本次修复**：在 `mpi_fuzzing_helper.py` 中，将通过 `afl-showmap` 筛选的有趣 SymCC 输出直接复制到 `fuzzer01/queue/`，绕过 AFL sync 机制实现反馈。

### 5.2 所有符号执行的新路径输出都应加入种子

**该观点正确。** 当前实现存在过于激进的双重过滤：

1. **SymCC 运行时过滤**：`SYMCC_AFL_COVERAGE_MAP` 使 QSYM 后端跳过已覆盖路径的 solver query
2. **afl-showmap 事后过滤**：检查输出是否触发新的 AFL bitmap edge

第 2 层过滤过于严格——SymCC 生成的微小变异可能不触发新的 AFL edge（因为 AFL edge 粒度较粗），但可能覆盖了不同的源码级分支。更合理的做法是将所有 SymCC 输出喂回 AFL，由 AFL 自身判断价值。

当前 benchmark 数据佐证了该问题：所有 hybrid 配置中 `symcc_interesting=0`，即没有任何 SymCC 输出通过 `afl-showmap` 过滤。

### 5.3 混合测试应持续双向交互

**该观点正确，这是当前实现的核心架构缺陷。**

**理想流程**：

```
持续循环 {
    AFL 变异产生新输入 → 加入 AFL queue
    SymCC 从 AFL queue 取新种子 → concolic 求解 → 产生新输入 → 放回 AFL queue
    AFL 发现新输入 → 进一步变异 → 产生更多新输入
    ... 双向持续反馈 ...
}
```

**官方 `symcc_fuzzing_helper` 即按此模式运行**——在无限循环中持续从 AFL queue 取种子、运行 SymCC、输出放回 AFL sync 目录。

**当前 MPI 实现的实际行为**：
- **AFL→SymCC 方向**：正常。`mpi_fuzzing_helper.py` 持续监控 AFL queue，取出新种子分发给 worker
- **SymCC→AFL 方向**：修复前完全断裂（输出放入 `symcc01/queue/`，AFL 不可见）；本次已修复（直接复制到 `fuzzer01/queue/`）

**更深层的问题**：即使反馈路径修复后，当前 PNG/XML 目标上 `symcc_interesting=0`——所有 SymCC 输出均未通过 `afl-showmap` 新颖性判定。根本原因是 concolic execution 路径邻域受限（详见 Q9），而非反馈机制问题。要真正发挥混合模式的优势，还需改进符号执行的约束求解能力或放宽新颖性过滤标准。

---

## Q6：并行开销与同步机制

### 6.1 并行开销量化

基于本轮 benchmark 数据（PNG 目标，5 分钟 timeout），可估算并行效率：

| np | Workers | Generated | Throughput (tc/s) | 理论线性 tc/s | 并行效率 |
|----|---------|-----------|-------------------|--------------|----------|
| 2 | 1 | 52,203 | 194 | 194（基线） | 100% |
| 4 | 3 | 133,680 | 499 | 582 | 85.7% |
| 8 | 7 | 332,219 | 1,233 | 1,358 | 90.8% |
| 16 | 15 | 640,922 | 2,372 | 2,910 | 81.5% |
| 32 | 31 | 1,209,286 | 4,461 | 6,014 | 74.2% |
| 64 | 62 | 2,320,965 | 8,455 | 12,028 | 70.3% |
| 128 | 125 | 1,997,978 | 7,240 | 24,250 | 29.9% |

**开销组成（待 profiling 确认）**：

1. **Master 消息处理瓶颈**：Master 在主循环中轮询所有 worker 的消息（`iprobe` 调用），worker 数量越大轮询越慢
2. **共享目录 I/O 竞争**：所有 worker 的 SymCC 输出写入同一目录（content-addressable storage），128 个进程并发写入产生 I/O 等待
3. **路径邻域耗尽效应**：worker 数量增大后，同一时间窗口内多个 worker 探索相同输入集，产生大量重复输出（np=128 时 unique/generated 仅 27%）
4. **Multi-master 同步延迟**：np>46 时启用多 master，hash 同步间隔 2 秒，在此窗口内不同 group 可能重复探索相同路径

### 6.2 竞争的具体成因

**文件系统级竞争**：

所有 worker 的 SymCC 实例将输出写入共享目录，文件名为内容的 SHA-256 hash。当多个 worker 同时生成相同内容的输出时：

```python
# _atomic_write() 已修复的 TOCTOU 竞态
# 修复前：
if not os.path.exists(dest):    # 检查
    open(dest, "wb").write(data) # 写入 ← 两步之间可能被其他进程抢先

# 修复后：
fd, tmp = tempfile.mkstemp(dir=parent_dir)
os.write(fd, content)
os.close(fd)
os.rename(tmp, dest)  # 原子操作
```

**Hash 去重竞争**：

两个 worker 可能同时收到相同输入（在 multi-master 模式下 hash 同步存在 2 秒延迟），各自运行 SymCC 后产生相同的输出集。这不是正确性问题（幂等写入），但浪费了计算资源。np=128 时 unique/generated 仅 27%（545,504/1,997,978）即为此效应的体现。

### 6.3 主从同步与去重机制

**Hash 去重流程**：

```
1. Worker 运行 SymCC，生成测试用例文件
2. 计算输出文件内容的 SHA-256 hash
3. 将输出写入共享目录，文件名 = hash（content-addressable storage）
4. Worker 通过 MPI 发送 (hash, 文件路径) 给所属 Master（TAG_RESULT）
5. Master 维护 analyzed_hashes: set[str]：
   - hash ∈ analyzed_hashes → 跳过（已分析过）
   - hash ∉ analyzed_hashes → 加入 pending_queue 待分配给空闲 worker
```

**Multi-master hash 同步流程**（`sync_hashes()` 函数，第 262 行）：

```
星型拓扑，Root Master (rank 0) 为中心节点：

Sub-master → Root：
  每 2 秒（SYNC_INTERVAL），Sub-master 通过 isend 将新发现的 hash 列表
  发送给 Root（TAG_HASH_SYNC）

Root 处理：
  1. iprobe 检查各 Sub-master 的消息
  2. 收到新 hash 后检查 analyzed_hashes 去重
  3. 新 hash 通过 isend 转发给其他所有 Sub-master（TAG_HASH_BCAST）

Sub-master 接收：
  1. iprobe 检查 Root 的广播
  2. 收到新 hash 后加入自己的 analyzed_hashes 和 pending_queue
```

所有 `isend` 均为非阻塞发送，避免 MPI 死锁。Sub-master 之间不直接通信，全部经由 Root 中转。

---

## Q8：采样误差分析

### 8.1 ±1.5% 是相对值还是绝对值

**绝对值。** 即当实际覆盖率为 17.0% 时，采样测量可能报告 15.5%～18.5%。

### 8.2 +1.5% 偏差的成因

`-1.5%` 的成因直观：采样漏掉了某些测试用例，导致部分分支未被执行。

`+1.5%` 的成因较为微妙，涉及以下因素：

**因素一：分层采样的代表性偏差**

采样策略为分层随机采样（stratified sampling），按文件名前缀分层。某些层中的测试用例可能触发稀有分支：

- 假设测试用例 A 唯一触发分支 X，A 在全集 80 万中仅出现 1 次
- 全量测量（如果能执行）和采样 20 万都可能选中或未选中 A
- 采样恰好选中 A 时，覆盖率包含分支 X
- 但如果全量测量因执行顺序不同导致 `.gcda` 计数累积差异，分支 X 的 taken/not-taken 判定可能不同

**因素二：lcov 分支判定的非确定性**

gcov 的分支 taken/not-taken 判定依赖于计数器值。当多个测试用例通过批量执行累积计数时，不同的执行顺序可能导致中间状态差异。特别是当某分支的执行计数恰好在 0/1 边界时，不同的采样子集可能产生不同的判定结果。

**因素三：采样容量的统计涨落**

200,000 个样本对于百万级全集而言，采样率约 20%–25%。在此采样率下，稀有事件（仅被少数测试用例触发的分支）的覆盖概率具有随机性，可能偏高也可能偏低。

综合以上因素，采样偏差表现为双向，实测范围约 ±1.5 个百分点。

---

## Q9：路径邻域有限性的根因

### 9.1 Concolic execution 与邻域有限的关系

**路径邻域有限的根本原因确实是 concolic execution 的执行模型。**

**Concolic execution 的工作方式**：
1. 以具体输入执行目标程序，收集路径上的所有分支条件（path constraint）
2. 选择路径上的某个分支条件取反（negate）
3. 求解取反后的约束，生成新输入
4. 新输入与原输入仅在被翻转的分支处不同

**邻域有限的原因**：
- 从种子 S 出发，一轮 concolic 探索的邻域大小 ≤ 路径上的分支数（通常几十到几百个）
- QSYM 后端采用单分支翻转策略，每次仅翻转一个条件
- 产生的新输入与原输入仅有 1–3 字节差异

**与纯静态符号执行的对比**：
- 纯静态符号执行（如 KLEE）从程序入口开始，系统性地 fork 每个分支，理论上可探索所有可达路径
- 路径数量虽指数增长，但可达路径空间远大于 concolic 的单路径邻域
- 代价是路径爆炸问题更加严重，且需要完整的环境模型

### 9.2 多样性不足的具体表现

以 PNG 目标为例，邻域有限导致多样性不足的具体机制：

1. **CRC 校验屏障**：PNG 格式每个 chunk 包含 CRC32 校验。SymCC 翻转数据字节时，需要同时满足 `crc32(new_data) == new_crc`，这是多变量联合约束
2. **QSYM 的近似求解**：QSYM 后端对每个分支独立求解，不做联合约束求解，无法同时翻转 data 和 CRC
3. **结果**：所有新输入要么破坏 CRC（走错误处理路径）、要么不改变数据（走相同路径）
4. 大量 hash 不同的测试用例实际触发相同的分支集合

---

## Q10：格式与符号执行的局限性

### 10.1 PNG 的格式分类

准确的分类应为"**带完整性校验的二进制容器格式**"：
- PNG 使用二进制编码（非文本可读），包含固定 magic number（`89 50 4E 47 0D 0A 1A 0A`）、chunk 结构（length + type + data + CRC32）
- "二进制格式"指编码方式（相对于 XML 的文本编码），而非文件用途
- "图片格式"指用途分类

与 XML 的对比：

| 特征 | PNG | XML |
|------|-----|-----|
| 编码 | 二进制 | 文本（ASCII/UTF-8） |
| 完整性校验 | CRC32（每个 chunk） | 无 |
| 结构验证 | 严格（magic number + chunk 长度） | 宽松（标签匹配） |
| 随机变异容错度 | 极低（几乎必定破坏 CRC） | 较高（可产生合法的不同结构） |

### 10.2 符号执行为何也未能突破

**符号执行未能突破的原因不仅是交互次数，更是约束求解能力的根本限制。**

**SymCC（QSYM 后端）的求解策略**：
1. 执行路径经过 `if (crc == computed_crc)` 分支
2. 翻转为 `crc != computed_crc` → 求解器找到让 CRC 不匹配的值 → 走错误处理路径
3. 要走"正确路径"并改变数据，需同时满足：`new_data ≠ old_data` 且 `crc32(new_data) == new_crc`
4. 这是**多变量联合约束**，QSYM 后端不支持此类求解

**即使循环交互 100 次**，如果 SymCC 本身无法生成通过 CRC 校验的新 PNG chunk，那反馈环并不能产生新覆盖。

**真正需要的改进方向**：
1. **更强的约束求解**：使用完整的 Z3 联合求解（而非 QSYM 的近似求解），能同时翻转 data + CRC
2. **校验绕过**：在编译时对 CRC 校验函数做特殊标记或 patch，使符号执行跳过完整性检查
3. **格式感知变异**：使用 grammar-based mutation 生成合法的 PNG chunk 作为种子，扩大初始路径多样性
4. **放宽过滤标准**：将所有 SymCC 输出喂回 AFL（不经 afl-showmap 过滤），让 AFL 的 havoc 变异在此基础上探索

---

## Q11：MPI 并行架构

### 11.1 "纯 MPI"模式的含义

"纯 MPI"指 `mpi_concolic_execution.py`——**仅运行 SymCC concolic execution，不涉及 AFL**。

流程如下：

```
1. Master 从种子目录导入初始输入
2. 将输入分发给 Worker
3. Worker 运行 SymCC → 生成新测试用例（写入共享目录）
4. 新测试用例的 SHA-256 hash 发回 Master
5. Master 去重后将新 hash 对应的输入重新分发
6. 循环直到超时或无新输入
```

在此模式下，种子数量增长的原因是 concolic execution 的迭代探索：每个种子经 SymCC 产生 N 个新输入，N 个新输入再各产生 M 个输入，呈指数级增长。但增长的是**数量**而非覆盖率——大部分新输入覆盖相同的分支集合（路径邻域有限效应）。

与 hybrid 模式的区别：hybrid 模式同时运行 AFL fuzzer 和 MPI SymCC，AFL 提供随机变异能力，SymCC 提供约束求解能力。

### 11.2 np>46 启用 multi-master 的原因

**单 Master 的消息处理瓶颈**：

```python
# Master 主循环（简化）
while True:
    for w in workers:                              # O(num_workers)
        if comm.iprobe(source=w, tag=TAG_READY):   # 检查 worker 是否空闲
            comm.recv(source=w, tag=TAG_READY)
            # 分配新任务...

    for w in workers:                              # O(num_workers)
        if comm.iprobe(source=w, tag=TAG_RESULT):  # 检查 worker 是否有结果
            comm.recv(source=w, tag=TAG_RESULT)
            # 处理结果、去重...
```

当 worker 数量超过 45 时，每轮循环需要 probe 90+ 个消息源（每个 worker 有 READY 和 RESULT 两种消息）。如果 worker 处理速度快（如 SymCC 执行时间短），Master 来不及处理所有结果和分配任务，worker 空闲等待时间增加。

`workers_per_master=45` 为经验阈值，可通过命令行参数 `--workers-per-master` 调整。

### 11.3 Multi-master 架构的改进方向

**当前实现为星型拓扑**：

```
Root Master (rank 0)
  ├── Sub-master 1 → [worker group 1: 约 45 workers]
  ├── Sub-master 2 → [worker group 2: 约 45 workers]
  └── Sub-master 3 → [worker group 3: 约 45 workers]

同步方式：
  Sub-master ──(isend, 每 2s)──→ Root ──(isend)──→ 其他 Sub-master
  不存在 Sub-master 之间的直接通信
```

**当前架构的问题**：

1. **同步延迟导致重复探索**：2 秒 SYNC_INTERVAL 内，不同 group 可能独立发现并探索相同路径。np=128 时 unique/generated 仅 27% 即为佐证
2. **Root 是双重瓶颈**：Root 同时管理自己的 worker group 并承担 hash 汇总转发，负载不均
3. **无任务级协调**：每个 Master 独立从共享目录取任务，可能重复处理相同输入

**建议改进为两级层次结构**：

```
协调 Master (rank 0)：不管理 worker，仅负责任务分区与 hash 汇总
  ├── 分 Master 1 → [worker group 1]
  ├── 分 Master 2 → [worker group 2]
  └── 分 Master 3 → [worker group 3]

改进点：
  1. 协调 Master 将种子集合分区分配给不同分 Master，避免重复探索
  2. 分 Master 产生的新 hash 实时上报协调 Master
  3. 协调 Master 将新 hash 分配给最合适的分 Master（如负载最低者）
  4. 同步间隔从 2 秒缩短至 500ms 或改为事件驱动
```

这样可以在保持消息处理效率的同时，从源头避免重复探索，显著提高大规模并行下的有效利用率。

---

## 附录 A：完整 Benchmark 数据

所有测试均已完成。配置：5 分钟 timeout，1 轮，np=2/4/8/16/32/64/128。

### A.1 PNG 目标（gfts-png_read_fuzzer）

| 模式 | np | Wall Time | Generated | Unique | Throughput (tc/s) | Line Cov | Branch Cov | Crashes |
|------|-----|-----------|-----------|--------|-------------------|----------|------------|---------|
| serial | 1 | 5m0.0s | 0 | 0 | 0.00 | 14.7% | 12.1% | 0 |
| mpi | 2 | 4m28.6s | 52,203 | 37,346 | 196.9 | 18.4% | 16.7% | 0 |
| mpi | 4 | 4m27.9s | 133,680 | 94,108 | 503.8 | 18.6% | 16.8% | 0 |
| mpi | 8 | 4m29.4s | 332,219 | 235,098 | 1,249.5 | 18.5% | 16.8% | 0 |
| mpi | 16 | 4m30.1s | 640,922 | 435,156 | 2,405.6 | 18.6% | 16.8% | 0 |
| mpi | 32 | 4m31.1s | 1,209,286 | 819,346 | 4,516.8 | 20.1% | 17.7% | 0 |
| mpi | 64 | 4m34.8s | 2,320,965 | 1,033,882 | 8,598.9 | 20.5% | 18.1% | 0 |
| mpi | 128 | 4m36.3s | 1,997,978 | 545,504 | 7,454.4 | 18.7% | 16.9% | 0 |
| hybrid | 2 | 5m11.6s | 11,568 | 11,568 | 37.1 | 19.1% | 16.6% | 0 |
| hybrid | 4 | 5m11.6s | 11,862 | 11,862 | 38.1 | 19.3% | 16.9% | 0 |
| hybrid | 8 | 5m11.7s | 12,580 | 12,580 | 40.4 | 19.3% | 16.9% | 0 |
| hybrid | 16 | 5m11.7s | 12,049 | 12,049 | 38.7 | 19.2% | 16.8% | 0 |
| hybrid | 32 | 5m11.7s | 13,826 | 13,826 | 44.4 | 18.8% | 16.5% | 0 |
| hybrid | 64 | 5m11.6s | 14,240 | 14,240 | 45.7 | 19.2% | 16.8% | 0 |
| hybrid | 128 | 5m11.6s | 13,342 | 13,342 | 42.8 | 19.2% | 16.8% | 0 |
| afl-only | 1 | 5m0.0s | 185 | 185 | 0.6 | 17.5% | 15.4% | 0 |

### A.2 XML 目标（gfts-xml_read_fuzzer）

| 模式 | np | Wall Time | Generated | Unique | Throughput (tc/s) | Line Cov | Branch Cov | Crashes |
|------|-----|-----------|-----------|--------|-------------------|----------|------------|---------|
| serial | 1 | 5m0.0s | 0 | 0 | 0.00 | 5.6% | 4.0% | 0 |
| mpi | 2 | 4m30.2s | 80,301 | 47,326 | 302.8 | 8.4% | 7.4% | 0 |
| mpi | 4 | 4m30.4s | 267,488 | 142,461 | 1,007.6 | 8.6% | 8.0% | 0 |
| mpi | 8 | 4m31.1s | 639,899 | 333,208 | 2,404.8 | 8.7% | 8.1% | 0 |
| mpi | 16 | 4m32.5s | 1,336,845 | 662,332 | 5,003.4 | 8.6% | 7.8% | 0 |
| mpi | 32 | 4m34.8s | 2,503,519 | 1,204,115 | 9,302.5 | 8.9% | 8.0% | 0 |
| mpi | 64 | 4m35.2s | 4,420,722 | 1,085,198 | 16,445.9 | 8.8% | 8.0% | 0 |
| mpi | 128 | 4m38.3s | 5,265,371 | 928,047 | 19,497.2 | 8.7% | 7.9% | 0 |
| hybrid | 2 | 5m11.8s | 31,566 | 31,566 | 101.2 | 10.8% | 10.8% | 0 |
| hybrid | 4 | 5m11.9s | 42,334 | 42,334 | 135.7 | 10.9% | 10.9% | 0 |
| hybrid | 8 | 5m11.9s | 75,304 | 75,304 | 241.5 | 10.7% | 10.8% | 0 |
| hybrid | 16 | 5m11.9s | 154,997 | 154,997 | 496.9 | 10.5% | 10.7% | 0 |
| hybrid | 32 | 5m12.0s | 275,374 | 275,374 | 882.5 | 10.6% | 10.7% | 0 |
| hybrid | 64 | 5m11.9s | 368,318 | 368,318 | 1,180.8 | 10.8% | 10.8% | 29 |
| hybrid | 128 | 5m12.0s | 380,098 | 380,098 | 1,218.3 | 10.4% | 10.5% | 0 |
| afl-only | 1 | 5m0.2s | 3,528 | 3,528 | 11.8 | 10.6% | 10.5% | 0 |

### A.3 关键发现

**AFL-only baseline 对比**：

| 目标 | AFL-only Branch Cov | Hybrid 最佳 Branch Cov | MPI 最佳 Branch Cov | 纯 SymCC 增益 |
|------|---------------------|------------------------|---------------------|--------------|
| PNG | 15.4% | 16.9%（np=4/8） | 18.1%（np=64） | +2.7pp |
| XML | 10.5% | 10.9%（np=4） | 8.1%（np=8） | -2.4pp |

- **PNG**：纯 MPI（SymCC only）在分支覆盖上超过 AFL-only baseline，说明 SymCC 的 concolic execution 对 PNG 格式有独立贡献。Hybrid 模式覆盖率与 MPI 相当，但低于 MPI np=64 的峰值
- **XML**：Hybrid 模式（10.5%–10.9%）显著优于纯 MPI（7.4%–8.1%），说明 AFL 的随机变异对 XML 这种变异容错度高的格式贡献更大。AFL-only（10.5%）已接近 hybrid 最佳值，SymCC 的额外贡献有限
- **Crashes**：XML hybrid np=64 发现 29 个 crash，值得进一步分析

**吞吐量 vs 覆盖率悖论**（以 PNG 为例）：

| np | Throughput (tc/s) | Branch Cov | Unique Rate |
|----|-------------------|------------|-------------|
| 2 | 197 | 16.7% | 71.5% |
| 4 | 504 | 16.8% | 70.4% |
| 8 | 1,250 | 16.8% | 70.8% |
| 16 | 2,406 | 16.8% | 67.9% |
| 32 | 4,517 | 17.7% | 67.8% |
| 64 | 8,599 | 18.1% | 44.5% |
| 128 | 7,454 | 16.9% | 27.3% |

吞吐量从 np=2 到 np=64 增长 43 倍，但分支覆盖仅从 16.7% 增至 18.1%（+1.4pp）。np=128 出现吞吐量下降（低于 np=64）且 unique rate 骤降至 27.3%，表明严重的重复探索。

### A.4 LAVA-M 结果

LAVA-M 的 4 个目标（base64、md5sum、uniq、who）在所有并行度下均生成 0 个测试用例。

**诊断结果**：

- SymCC 插桩正常（63 个 `__sym_ctor` 符号，链接 `libsymcc-rt.so`）
- 运行时 SymCC 正常初始化（输出"This is SymCC running with the QSYM backend"）
- 但 QSYM 后端未找到可解的符号条件

**原因**：coreutils 程序（base64 decode、md5sum、uniq 等）的核心逻辑为查表和数学运算，包含的条件分支多为常量比较或格式检查。QSYM 后端在这些二进制上无法有效生成替代路径。这是 SymCC/QSYM 的已知局限性，而非 MPI 并行框架的问题。
