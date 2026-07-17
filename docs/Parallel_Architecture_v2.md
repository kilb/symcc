    # SymCC MPI 并行符号执行 — 方案架构文档

## 一、系统概述

### 1.1 目标

将单线程的 SymCC concolic execution 引擎并行化，利用多核服务器加速符号执行，并与 AFL++ 模糊测试器深度集成形成双向反馈闭环。

### 1.2 代码全景

两种运行模式共享底层 SymCC 运行时（`libsymcc-rt.so`），通过 MPI（mpi4py + OpenMPI）实现进程间通信。

| 文件 | 行数 | 角色 |
|------|-----:|------|
| `util/mpi_fuzzing_helper.py` | 1,235 | **Hybrid AFL+SymCC 模式**：AFL 模糊测试 + SymCC 约束求解协同 |
| `util/mpi_concolic_execution.py` | 771 | **MPI 纯并行模式**：纯 SymCC 符号执行，不依赖 AFL |
| `runtime/.../solver.cpp` | 1,137 | QSYM 约束求解器（含多分支联合求解、Fuzzy-Sat） |
| `runtime/.../solver.h` | 129 | 求解器头文件 |
| `runtime/.../Runtime.cpp` | 517 | SymCC ↔ QSYM 桥接层（含选择性符号化） |
| `benchmark/run_benchmark.py` | 2,443 | 自动化测试套件 |
| **合计** | **6,232** | |

---

## 二、MPI 纯并行模式

### 2.1 架构

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
     │ Sub-Master │  │ Sub-Master │  │ Sub-Master │   （np > 46 时自动拆分）
     └──┬──┬──┬──┘  └──┬──┬──┬──┘  └──┬──┬──┬──┘
        │  │  │        │  │  │        │  │  │
       W1 W2 W3      W4 W5 W6      W7 W8 W9      Workers
        └──┴──┴────────┴──┴──┴────────┴──┴──┘
                 共享文件系统（SHA-256 内容寻址）
```

### 2.2 角色分配算法

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

### 2.3 通信协议

#### MPI 消息标签

| 标签 | 值 | 方向 | 内容 |
|------|---:|------|------|
| `TAG_WORK` | 1 | Master → Worker | SHA-256 hash 字符串（64 字节） |
| `TAG_RESULT` | 2 | Worker → Master | `{"new_hashes": [str], "retcode": int, "elapsed": float}` |
| `TAG_STOP` | 3 | Master → Worker | `None`（终止信号） |
| `TAG_READY` | 4 | Worker → Master | `rank`（就绪信号） |
| `TAG_HASH_SYNC` | 10 | Sub-master → Root | `[hash_str, ...]`（发现的新 hash） |
| `TAG_HASH_BCAST` | 11 | Root → Sub-master | `[hash_str, ...]`（广播的新 hash） |

#### Hash 协议

MPI 仅传输 64 字节 SHA-256 hash（而非 ~8MB 文件内容），Worker 从共享文件系统读取实际内容：

```
Master:                           Worker:
  send(hash, TAG_WORK)              recv(TAG_WORK) → hash
                                    read shared_dir/{hash} → content
                                    run SymCC(content) → outputs
                                    for out: write shared_dir/{sha256(out)}
                                    send({"new_hashes": [...]}, TAG_RESULT)
  recv(TAG_RESULT)
  for h in new_hashes:
    if h ∉ analyzed_hashes:
      pending_queue.append(h)       # 迭代深化
```

#### 多 Master 同步

每 2 秒执行 `sync_hashes()`：Root 收集各 Sub-master 的新 hash（`TAG_HASH_SYNC`），去重后广播给其他 Sub-master（`TAG_HASH_BCAST`）。**所有跨 Master 通信使用非阻塞 `isend`**，Sub-master 先 recv 再 send（避免死锁）。

### 2.4 终止条件

1. Wall-clock 超时（`--wall-timeout`）
2. 空闲超时：`pending_queue` 为空 + 无活跃 Worker + 连续 `max_idle_rounds × 5` 秒无新输入
3. 外部信号（SIGTERM/SIGINT）

---

## 三、Hybrid AFL+SymCC 模式

### 3.1 架构

```
 ┌─────────────────────────────────────────────────────────┐
 │                    AFL++ Fuzzer（外部独立进程）            │
 │  afl-fuzz -M fuzzer01 -i seeds -o afl_out -- target @@  │
 └───────────┬──────────────────────────┬──────────────────┘
             │ 读取 AFL queue            │ 写入 AFL extras/
             ▼                          ▲
 ┌───────────────────────────────────────────────────────┐
 │              Master (MPI rank 0)                      │
 │  输入源：1. symcc_feedback_queue（深度优先）             │
 │          2. AFL queue（智能打分调度）                    │
 │  自适应：hints → focus_bytes → 选择性符号化             │
 └───┬──────┬──────┬──────┬──────┬──────┬───────────────┘
     ▼      ▼      ▼      ▼      ▼      ▼
   W1     W2     W3     W4     W5     W6    SymCC Workers
         写入 symcc01/queue/ + fuzzer01/queue/
```

AFL 由用户手动启动或 benchmark 框架自动启动（轮询 `fuzzer_stats` 文件等待就绪）。Master 通过 `AflConfig` 读取 AFL 配置；若 AFL 退出，Master 进入等待直到超时。

### 3.2 通信协议

**Master → Worker**：
```python
{"path": str, "bitmap_version": int, "focus_bytes": "start-end" | ""}
```

**Worker → Master**：
```python
{
    "new_tests": [{"content": bytes, "bitmap": [(eid, cnt)], "hints": [(off, old, new)]}],
    "total_generated": int, "retcode": int, "elapsed": float, "killed": bool,
}
```

### 3.3 Master 主循环

```
while not shutdown:
  # 1. 构建工作队列（反馈优先 → AFL queue 智能调度）
  feedback = sorted(symcc_feedback_queue, key=generation, reverse=True)
  afl_inputs = best_new_testcases(seen, analyzed_hashes) if 反馈不足 else []
  work_queue = feedback + afl_inputs

  # 2. 交替分发（iprobe TAG_READY）和收集（iprobe TAG_RESULT）
  #    批量 triage → 更新 bitmap_version → 更新 focus_bytes

  # 3. 无工作时 sleep(2) 等待 AFL 产出
```

### 3.4 Worker 执行流程

```
循环:
  send(rank, TAG_READY)
  msg = recv(TAG_WORK | TAG_STOP)

  # 1. Bitmap 同步（仅版本更新时重读，原子文件 tmp+replace）
  if msg.bitmap_version > current: copy shared_bitmap → local

  # 2. 设置 SYMCC_FOCUS_BYTES（若 Master 指定）

  # 3. 执行 SymCC（timeout=30s）

  # 4. 收集输出 + Worker 端去重（StreamingShowmap ~0.6ms/TC）
  #    仅传 interesting TC（~3% 通过率，消息 323KB → ~10KB）
  #    同时收集 .hints 文件

  send(result, TAG_RESULT)
```

### 3.5 四层覆盖率过滤

| 层 | 位置 | 过滤对象 | 机制 |
|----|------|---------|------|
| 1 | SymCC 运行时 | 分支 | `isInterestingBranch()` 检查 AFL bitmap，已覆盖分支不求解 |
| 2 | Worker | 输出文件 | StreamingShowmap + 本地 bitmap merge，仅传有新边的 TC |
| 3 | Master | Worker 传来的 TC | 全局 bitmap merge，仅保存有新边的 TC |
| 4 | AFL | sync 目录文件 | AFL 独立判断是否触发新路径 |

各层过滤率因目标而异。典型观察：~95 个输出 → ~3 个传回 Master → 0-2 个进入 AFL queue。

### 3.6 智能种子调度

```python
def best_new_testcases(seen, analyzed_hashes):
    # 多因子打分：+cov 标记 +100, symcc_ 前缀 +20, 新颖度 +0~50, 大文件 -30
    # SHA-256 内容去重：跳过与已分析种子内容相同的文件
    return sorted(candidates, by=-score)
```

### 3.7 迭代代数跟踪

AFL 种子 gen=0 → SymCC 输出 gen=1 → 再次 SymCC gen=2 → ...
调度优先级：generation 越高越优先（深度优先）。`SYMCC_MAX_DEPTH` 控制上限。

---

## 四、性能优化历程（6 轮 Profiling 驱动）

| 轮次 | 瓶颈 | 优化 | 效果 |
|:----:|------|------|------|
| 1 | Master 端 showmap triage 103s | 移到 Worker 端，Master 仅处理稀疏边列表 | **103s → 0ms** |
| 2 | MPI 消息 ~8MB | Hash 协议（详见 2.3） | **~8MB → 64B** |
| 3 | Worker showmap 12ms/call | Streaming fork server（`afl-showmap -S`） | **12ms → 0.6ms** (19x) |
| 4 | AFL queue 扫描占 Master 88% | `os.scandir` + 反馈时跳过扫描 | **39.5s → 3.7s** (10x) |
| 5 | MPI recv 34MB（全部 TC） | Worker 端 coverage dedup，仅传 ~3% | **34MB → 3.6MB** (9.4x) |
| 6 | 串行 dispatch/collect | 交替处理 TAG_READY 和 TAG_RESULT | Worker 空闲减少 |

优化后 Master 负载（np=64）：scan 34%, recv 5%, triage 4%, **idle 57%**。

---

## 五、约束求解层优化

### 5.1 多分支联合求解

**问题**：`negatePath()` 每次翻转一个分支 → 单字节变体（`"ustar"` → `"·star"`, `"u·tar"` ...）。

**方案**：收集连续 interesting 分支，检测"连续字节比较"模式后联合翻转：

```cpp
void Solver::addJcc(ExprRef e, bool taken, ADDRINT pc) {
    if (is_interesting) {
        negatePath(e, taken);              // 标准求解（保留）
        if (multi_solve_enabled) {
            pending_branches.push_back({e, taken, pc});
            if (size >= kMaxGroupSize) { tryNegateGroup(); clear(); }
        }
    } else {
        // non-interesting 中断收集 → 触发联合求解检测
        if (size >= kMinGroupSize) tryNegateGroup();
        clear();
    }
}

void Solver::negateGroup() {
    set_timeout(kSolverTimeout * 3);       // Z3 超时 3 倍
    // 1. 全翻转：所有分支同时 negate → checkAndSave("group")
    // 2. 半翻转（≥4 分支）：前半 negate + 后半保持 → checkAndSave("group-half")
    restore_timeout();
}
```

Z3 不会"智能地"生成有效 magic，而是生成满足约束的任意解。联合求解的价值在于一次改变多个字节，当路径约束中包含对其他 magic 的比较时可能生成有效格式。

**三种检测模式**（`tryNegateGroup` 按优先级调用）：

| 模式 | 条件 | 场景 | 级别 |
|------|------|------|------|
| 连续字节比较 | 字节偏移严格连续 | `strcmp("ustar")` | `SYMCC_MULTI_SOLVE=1` |
| 同变量 switch-case | 相同依赖集 + 不同常量 | `switch(type)` | `=2`（实验性） |
| 邻近结构体字段 | 64B 窗口内 | 文件头多字段 | `=2`（实验性） |

### 5.2 Fuzzy-Sat 近似求解

对 `ReadExpr(i) op Constant` 模式直接计算满足值，跳过 Z3（`SYMCC_FAST_SOLVE=1`）：

```cpp
bool Solver::fastSolve(ExprRef e, bool taken) {
    // 提取: byte_offset + const_value + operator_kind
    // 计算: negate(kind, taken) → target_byte
    //   Equal→C^0x01, Ugt→C+1, Ult→C-1, Uge/Ule→C, 有符号同理
    // 写入输出文件（标记 "-fast"），Z3 仍会被调用
}
```

`fastSolve` 在 `negatePath` 开头执行，成功后**不短路** Z3 调用，因此是纯增量产出。

### 5.3 选择性符号化

在 `_sym_get_input_byte()` 中，`SYMCC_FOCUS_BYTES="start-end"` 范围外的字节直接返回 concrete（不创建符号表达式），从源头减少约束。Master 根据累积的 hint 偏移自动计算范围（interesting 偏移 ±32 字节），通过 dispatch 消息传递给 Worker。

### 5.4 约束 Hint 传递与字典引导

**Hint 传递**（`SYMCC_EMIT_HINTS=1`）：`saveValues()` 写入 `.hints` 文件（`offset:old:new`）→ Worker 收集 → Master 写入 AFL `extras/` → AFL 自动作为字典 token 使用。参考 CONFETTI（ICSE 2022）的 global hinting 机制。

**静态字典引导**（`SYMCC_DICT`）：Z3 求解后用 AFL 字典 token 替换被修改的字节位置，生成最多 20 个变体。适用于单种子场景（+50%），多样种子时无额外收益。

**CmpLog 动态字典**：AFL++ 编译时插桩 `strcmp`/`memcmp`，运行时自动提取比较参数。与 SymCC 配合：`afl-fuzz -c target_cmplog -l 2AT ...`。对文本解析器（SQLite），CmpLog 比 SymCC 字典引导更有效。

### 5.5 solveAll 全解枚举

`solveAll()` 循环求解一个约束的**所有**满足值（排除已找到的值后继续求解），用于符号化内存地址的边界测试（`addAddr` 中调用）。

### 5.6 优化交互关系

```
addJcc() 执行顺序：
  is_interesting?
    No  → tryNegateGroup()（中断收集）→ clear()
    Yes → 1. fastSolve()    （μs 级，纯增量）
          2. negatePath()   （ms~s 级，含 hint 输出）
          3. pending_branches.push_back()（收集联合求解分支）
```

**已知依赖问题**：hints → focus_bytes 存在鸡生蛋问题——hints 需 Master triage 判定 interesting 才收集，而实验中 `symcc_interesting=0` 导致选择性符号化从未激活。

---

## 六、环境变量配置

| 环境变量 | 默认 | 功能 |
|---------|------|------|
| `SYMCC_MULTI_SOLVE` | 未设置 | `=1` 连续字节联合求解；`=2` 含实验性模式 |
| `SYMCC_FAST_SOLVE` | 未设置 | `=1` 简单约束快速求解 |
| `SYMCC_EMIT_HINTS` | 未设置 | `=1` 输出 .hints 文件 |
| `SYMCC_FOCUS_BYTES` | 未设置 | `="start-end"` 选择性符号化范围 |
| `SYMCC_TIMEOUT` | `30` | SymCC 执行超时（秒） |
| `SYMCC_MAX_DEPTH` | `0` | 迭代深化最大代数（0=无限） |
| `SYMCC_AFL_COVERAGE_MAP` | — | AFL bitmap 共享路径 |
| `SYMCC_DICT` | 未设置 | AFL 字典文件路径 |
| `SYMCC_MASTER_PROFILE` | 未设置 | `=1` Master 性能 profiling |

**推荐配置**：
- 多格式解析器（libarchive）：`SYMCC_MULTI_SOLVE=1 SYMCC_EMIT_HINTS=1 SYMCC_TIMEOUT=30`
- 文本解析器（SQLite）：上述 + `SYMCC_FAST_SOLVE=1`

---

## 七、实验结果

### 7.1 吞吐量 Scaling（MPI 纯并行，120s benchmark）

| 目标 | np=2 | np=8 | np=32 | np=128 | 加速比 |
|------|-----:|-----:|------:|-------:|------:|
| xml | 128 | 1,604 | 7,683 | 15,022 | 117x |
| SQLite | 236 | 1,492 | 6,579 | 19,741 | 84x |
| base64 harness | 172 | 1,027 | 4,117 | 12,249 | 71x |

单位：tc/s。np≤32 效率 74%-100%。

### 7.2 全量覆盖率（10 目标，Hybrid，300s v2 benchmark，ShowmapCov）

| 目标 | 种子 | AFL-only | Hybrid best (np) |
|------|-----:|--------:|-----------:|
| libarchive | 6.45% | 15.07% | **23.36%** (32) |
| who | 6.73% | 44.57% | **47.26%** (32) |
| pcre2 | 7.16% | 41.73% | **43.58%** (32) |
| png | 10.35% | 14.10% | **16.96%** (32) |
| base64 | 11.58% | 7.81%† | **23.90%** (8) |
| xml | 3.08% | 7.60% | **8.90%** (32) |
| SQLite | 13.70% | 18.55% | **18.84%** (32) |
| freetype2 | 2.22% | 5.75% | **11.23%** (8) |
| md5sum | 7.14% | 7.37% | 7.37% (-) |
| uniq | 9.54% | 10.61% | 10.61% (-) |

†base64 AFL-only 缺少 `-d` 参数

### 7.3 深度集成优化效果（独立对比实验，Hybrid np=8, 300s, Hybrid-AFL FstatsCov 差值）

| 方案 | libarchive 差值 | SQLite 差值 |
|------|------:|------:|
| 基线（原始代码 + TIMEOUT=10） | +1.38pp | +1.48pp |
| multi-solve v2（连续字节） | **+3.32pp** | -0.17pp |
| 全部增强 | **+4.56pp** | -0.53pp |
| 全部增强 + fast-solve | +1.45pp | **+0.96pp** |

多实例并行调度（分割种子为 6 组 × np=32）：吞吐量 +2.8x 但覆盖率 -0.14%。分割种子破坏跨类型反馈环，不推荐。

---

## 八、使用方法

### 8.1 MPI 纯并行

```bash
mpirun -np 32 python3 util/mpi_concolic_execution.py \
    -i seeds/ -o output/ -t 30 --wall-timeout 300 -- ./target_symcc @@
```

### 8.2 Hybrid AFL+SymCC

```bash
# 步骤 1：启动 AFL
afl-fuzz -M fuzzer01 -i seeds -o /tmp/afl_out -- ./target_afl @@

# 步骤 2：启动 SymCC MPI Workers
SYMCC_MULTI_SOLVE=1 SYMCC_EMIT_HINTS=1 \
mpirun -np 8 python3 util/mpi_fuzzing_helper.py \
    -a fuzzer01 -o /tmp/afl_out -n symcc01 -- ./target_symcc @@
```

### 8.3 Benchmark

```bash
python3 benchmark/run_benchmark.py \
    --targets sqlite-sqlite_fuzzer,libarchive-archive_fuzzer \
    --np-list 2,8,32 --timeout 300 --rounds 3 \
    --hybrid --afl-only --no-default --public --skip-build --output results/
```
