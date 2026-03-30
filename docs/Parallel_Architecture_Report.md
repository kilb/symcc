# SymCC MPI 并行符号执行架构报告

## 目录

1. [并行架构](#1-并行架构)
2. [AFL 与 SymCC 的结合](#2-afl-与-symcc-的结合)
3. [Benchmark 方法与效果](#3-benchmark-方法与效果)

---

## 1. 并行架构

### 1.1 整体设计

系统包含两种并行模式：

| 模式 | 脚本 | 适用场景 | 核心特点 |
|------|------|----------|----------|
| **MPI 纯并行** | `mpi_concolic_execution.py` | 纯 SymCC 符号执行 | 多 master 自动扩展，hash 协议 |
| **Hybrid AFL+SymCC** | `mpi_fuzzing_helper.py` | AFL 模糊测试 + SymCC 符号执行协同 | 单 master，双向反馈环 |

两种模式都基于 MPI（mpi4py）实现 master-worker 架构，通过 `mpirun -np N` 启动 N 个进程。

### 1.2 MPI 纯并行模式

```
                    ┌─────────────┐
                    │   Master 0  │
                    │  (调度+去重) │
                    └──┬──┬──┬──┬─┘
            ┌──────────┤  │  │  ├──────────┐
            ▼          ▼  ▼  ▼  ▼          ▼
        ┌────────┐ ┌────────┐  ┌────────┐ ┌────────┐
        │Worker 1│ │Worker 2│  │Worker 3│ │Worker N│
        │ SymCC  │ │ SymCC  │  │ SymCC  │ │ SymCC  │
        └────────┘ └────────┘  └────────┘ └────────┘
              │          │          │          │
              └──────────┴──────────┴──────────┘
                         ▼
                   共享文件系统
                 (内容寻址存储)
```

**通信协议**：Master 只通过 MPI 发送 hash 字符串（64 字节），Worker 从共享目录按 hash 读取文件内容。这种 hash 协议将 MPI 消息大小从 ~10KB 降到 64B，完全消除了序列化瓶颈。

**多 Master 自动扩展**：当进程数超过 ~46 时，自动分裂为多个 master：
- 每个 master 管理 ~45 个 worker（通过 `MPI_Comm_Split` 创建子通信域）
- master 间通过 `isend`（非阻塞）每 2 秒同步一次 hash 集合
- root master（rank 0）负责聚合和广播

**去重机制**：所有测试用例以 SHA-256 hash 命名，hash 集合（Python `set`）作为全局已分析记录，O(1) 查重。

### 1.3 Hybrid AFL+SymCC 并行模式

```
     ┌──────────┐          ┌──────────────┐
     │   AFL    │ ←─────── │   Master 0   │
     │ (模糊器) │ ───────→ │ (调度+triage)│
     └──────────┘          └──┬──┬──┬──┬──┘
   AFL queue (fs)     ┌──────┤  │  │  ├──────┐
                      ▼      ▼  ▼  ▼  ▼      ▼
                  ┌──────┐┌──────┐  ┌──────┐┌──────┐
                  │ W1   ││ W2   │  │ W3   ││ WN   │
                  │SymCC ││SymCC │  │SymCC ││SymCC │
                  │+smap ││+smap │  │+smap ││+smap │
                  └──────┘└──────┘  └──────┘└──────┘
```

**Master 主循环**（每迭代 < 1ms）：
1. 扫描 AFL queue + SymCC 反馈队列获取新输入（`os.scandir`）
2. 交替处理 `TAG_READY` 和 `TAG_RESULT`：有空闲 worker 就派发，有结果就收集
3. 批量 triage：对 worker 返回的 interesting 测试用例做 coverage merge
4. 将新覆盖的用例写入 AFL sync 目录 + SymCC 反馈队列

**Worker 流程**（每任务 ~0.5s）：
1. 接收文件路径（非文件内容，~64B MPI 消息）
2. 从共享文件系统读取输入
3. 运行 SymCC 符号执行，产生 ~95 个新测试用例
4. 通过 Streaming Showmap 收集每个输出的 AFL 边覆盖（~0.6ms/个）
5. 本地 coverage dedup：只保留触发新边的 ~3 个 interesting 用例
6. 发送精简结果给 master（~10KB vs 原来 ~323KB）

### 1.4 并行瓶颈分析与优化

通过 `profile_bottleneck.py` 工具精确测量了 master 各环节耗时。以下是 np=64（62 workers）下的演进：

#### 瓶颈 #1：AFL Queue 目录扫描（占 88% 到 20%）

| 优化前 | 优化后 | 方法 |
|--------|--------|------|
| `os.listdir()` + `os.stat()` 每文件 | `os.scandir()` 一次系统调用 | 利用 `d_type` 避免额外 stat |
| 每循环迭代都扫描 | 有反馈时跳过扫描 | 优先分发 SymCC 反馈 |
| 39.47s (88%) | 3.68s (20%) | **10x 加速** |

#### 瓶颈 #2：Worker 端 afl-showmap fork/exec（占 60% per-task）

| 优化前 | 优化后 | 方法 |
|--------|--------|------|
| 每测试用例 fork+exec afl-showmap | Streaming 模式持久 fork server | `afl-showmap -S` 管道协议 |
| 12ms/call, 95 次 = 1.14s/task | 0.6ms/call, 95 次 = 57ms/task | **19x per-call 加速** |

#### 瓶颈 #3：MPI recv 反序列化（占 38% 到 5%）

| 优化前 | 优化后 | 方法 |
|--------|--------|------|
| 传 95 个 TC 全部内容 (323KB/msg) | 只传 ~3 个 interesting TC (10KB) | Worker 端 coverage dedup |
| 反序列化 7.8ms/msg | 反序列化 0.9ms/msg | 消息减小 31x |
| 18.70s (38%) | 2.28s (5%) | **8.2x 加速** |

> **关于序列化库**：实测 CPython 的 pickle 和 msgpack 的 C 扩展速度几乎相同（2.23 vs 2.22 ms），问题不在库的选择，而在消息大小。正确的优化方向是减少传输的数据量。

#### 优化后 Master 负载分布（np=64）

```
scan:     34%   AFL queue 扫描（已用 scandir 优化）
recv:      5%   MPI 消息接收（消息从 34MB 降到 3.6MB）
triage:    4%   边覆盖合并（0.04ms/msg，稀疏 edge set O(500)）
idle:     57%   Master 空闲等待 workers
```

**结论**：Master 不再是瓶颈。单 master 可配 60+ workers，此时 master 负载仅 9%（recv+triage）。np=64 的 57% idle 表明瓶颈已转移到**输入源**（AFL 变异速率 + SymCC 反馈环的 interesting 产出率）。

---

## 2. AFL 与 SymCC 的结合

### 2.1 结合架构

AFL 和 SymCC 通过共享文件系统实现双向反馈循环：

```
AFL 模糊测试                         SymCC 符号执行
┌─────────────┐                     ┌─────────────────┐
│ 随机/启发变异 │                     │ 约束求解生成输入  │
│ 快速 (~1000/s)│                     │ 精确 (~50/s/worker)│
└──────┬──────┘                     └────────┬────────┘
       │ 写入                                 │ 写入
       ▼                                      ▼
  fuzzer01/queue/                        symcc01/queue/
  (AFL 的测试用例)                     (SymCC 的有趣用例)
       │                                      │
       │ Master 读取                           │ 写入 AFL sync
       ├──────────→ MPI Master ←───────────────┤
       │            (triage)                   │
       │                                       ▼
       │                              fuzzer01/queue/
       │                         (SymCC 反馈到 AFL, id:symcc_*)
       └──── AFL 拾取 SymCC 用例 ──────────────┘
```

### 2.2 用例共享机制

**AFL 到 SymCC**（Master 轮询）：
- Master 用 `os.scandir` 扫描 `fuzzer01/queue/` 目录
- 新文件作为工作分发给 SymCC workers
- SymCC 对 AFL 发现的有趣输入做约束求解，生成覆盖新路径的变异

**SymCC 到 AFL**（文件同步）：
- Worker 端 streaming showmap 检测新边覆盖
- Interesting 用例写入 `fuzzer01/queue/id:symcc_NNNNNN`
- AFL 通过其 `-M/-S` sync 机制自动拾取这些用例

**SymCC 到 SymCC**（反馈队列）：
- Interesting 用例同时加入 `symcc_feedback_queue`
- 下次 Master 分发时优先使用（不等 AFL sync）
- 实现了类似纯 MPI 模式的迭代深化

### 2.3 覆盖去重：Bitmap 版本化

Worker 维护 master coverage bitmap 的本地副本：

1. Master 发现新覆盖 → `bitmap_version++` → 原子写 `.shared_bitmap` 到共享文件系统
2. Master 在 work message 中附带 `bitmap_version`
3. Worker 比较本地版本号 → 仅在版本更新时重读 bitmap
4. Worker 的 `CoverageBitmap` 用稀疏 edge set 做 O(500) 的 merge 判断

### 2.4 结合效果

以 `xml_read_fuzzer`（libxml2）为目标，60 秒测试：

| 模式 | np=8 tc/s | np=32 tc/s | 边覆盖率 |
|------|--------:|----------:|------:|
| AFL-only | 12.6 | -- | 12.8% |
| MPI SymCC | 1,753 | 8,554 | 10.3% |
| **Hybrid** | **933** | **2,801** | **12.8%** |

**关键发现**：
- Hybrid 的边覆盖率（12.8%）**高于纯 MPI**（10.3%），与 AFL-only 持平
- AFL 的随机变异发现了 SymCC 约束求解遗漏的路径
- SymCC 反过来为 AFL 提供了更好的种子（SymCC/AFL 输出比 = 25.4x at np=32）
- 两者互补效果显著：Hybrid 12.8% > MPI 10.3%，而吞吐量远超 AFL-only

---

## 3. Benchmark 方法与效果

### 3.1 测试框架

```bash
python3 benchmark/run_benchmark.py \
    --targets gfts-xml_read_fuzzer,lava-base64 \
    --np-list 2,4,8,16,32,64 \
    --timeout 300 --rounds 3 \
    --hybrid --afl-only
```

支持四种运行模式的自动化对比：

| 模式 | 描述 | 控制变量 |
|------|------|----------|
| Serial | 单进程 SymCC 循环 | 基线 |
| MPI | 多进程并行 SymCC | 只改 np |
| Hybrid | AFL + MPI SymCC | AFL 1 核 + SymCC np-2 核 |
| AFL-only | 纯 AFL 模糊测试 | 1 核参考 |

### 3.2 测试集

| 测试集 | 目标 | 来源 | 特点 |
|--------|------|------|------|
| **Google FTS** | png_read_fuzzer | libpng 1.2.56 | 图像解析，CRC 校验障碍 |
| | xml_read_fuzzer | libxml2 2.9.2 | XML 解析，XPath/DTD/XInclude |
| **LAVA-M** | base64 | coreutils 8.24 | 注入 bug，需解码模式 |
| | md5sum, uniq, who | coreutils 8.24 | 不同 I/O 模式 |

**选择理由**：
- **Google FTS**：SymCC 论文的标准评估目标，复杂库函数，有丰富的符号约束
- **LAVA-M**：学术界标准的 bug 注入基准集，可验证 bug 发现能力
- 两者覆盖了**文件解析**和**文本处理**两类典型应用

**编译适配**：
- 对 LAVA-M 的 `unlocked-io.h` 做了 patch（将 `fread_unlocked` 等映射回标准函数），否则 SymCC 无法符号化输入读取
- 对 libpng 做了 CRC bypass patch，让 SymCC 能探索 CRC 校验后的代码

### 3.3 评估指标

| 指标 | 定义 | 选择理由 |
|------|------|----------|
| **AFL 边覆盖率** | `afl-showmap -C` 检测到的非零边数 / 总边数 | 统一的覆盖率标准，不依赖 gcov 插桩，AFL/SymCC/Hybrid 可直接对比 |
| **吞吐量** (tc/s) | 测试用例生成速度 | 衡量并行效率，反映系统处理能力 |
| **Scaling 效率** | (tc/s @ np=N) / (tc/s @ np=2) / (N/2) | 衡量并行扩展性，检测 master 瓶颈 |
| **Interesting 数** | 触发新 AFL 边覆盖的测试用例数 | 衡量有效输出质量（非重复探索） |

**为什么用 AFL 边覆盖率而非 gcov 行/分支覆盖率**：
- AFL 边覆盖是 fuzzing 社区的事实标准，直接反映 AFL 能利用的覆盖信息
- gcov 需要特殊编译的 coverage 二进制，增加构建复杂度
- `afl-showmap -C` 批量处理极快（~30ms 处理 20 个文件），不成为测量瓶颈

### 3.4 当前效果

#### MPI 纯并行 Scaling（xml_read_fuzzer, 60s）

```
np=  2 |                                        |    200 tc/s
np=  4 |====                                    |    759 tc/s
np=  8 |========                                |  1,753 tc/s
np= 16 |===================                     |  4,080 tc/s
np= 32 |========================================|  8,554 tc/s
```

**超线性 scaling**（+38% at np=32）：更多 worker 产生更多种子互相受益。

#### Hybrid Scaling（xml_read_fuzzer, 60s, streaming showmap）

```
np=  2 |==                                      |    147 tc/s
np=  4 |====                                    |    267 tc/s
np=  8 |=============                           |    933 tc/s
np= 16 |==================                      |  1,299 tc/s
np= 32 |========================================|  2,801 tc/s
```

#### 模式对比（np=32, 60s）

| | AFL-only | MPI SymCC | Hybrid |
|--|------:|------:|------:|
| tc/s | 12.6 | 8,554 | 2,801 |
| 边覆盖率 | 12.8% | 10.3% | 12.8% |
| 工具互补 | 只有随机变异 | 只有约束求解 | **两者结合** |

#### LAVA-M 修复效果

| 目标 | 修复前 | 修复后 | 原因 |
|------|-----:|------:|------|
| base64 | 0 tc | **16,855 tc** | unlocked-io.h patch |
| uniq | 0 tc | **8 tc** | getc_symbolized 启用 |

---

## 附录：优化时间线

| 提交 | 优化 | 效果 |
|------|------|------|
| `7c0c7db` | 修复 afl-showmap 路径 + bitmap init | Hybrid 从 0 interesting 到可工作 |
| `7e5ec97` | Worker 端 bitmap + 稀疏 edge triage | Master triage 103s 降到 0ms |
| `bb1d72d` | 路径协议 + bitmap 版本号 | MPI 消息 ~8MB 降到 ~64B |
| `c979241` | Streaming showmap | Worker showmap 12ms 降到 0.6ms/call |
| `5855b7c` | scandir + 扫描跳过 | AFL scan 39.47s 降到 3.68s |
| `276cf01` | Worker 端 coverage dedup | recv 18.7s 降到 2.3s，消息 34MB 降到 3.6MB |
