# SymCC 深度集成研究计划

> 基于 2024-2026 最新研究调研，以下方向需要较大工作量（半天~数周），记录为中长期计划。

---

## 计划 4：Fuzzy-Sat 近似约束求解

**来源**：FUZZOLIC（Computers and Security, 2021）

**核心思想**：对简单约束（如 `byte[i] == const`、`byte[i] > const`）不调用 Z3，而是用 JIT 编译的近似求解器直接生成满足条件的输入。仅对真正复杂的约束（多变量非线性）回退到 Z3。

**预期收益**：简单约束求解速度 +100x（μs 级 vs Z3 的 ms 级），减少 Z3 超时，提升 SymCC 总吞吐量。

**实施方案**：
1. 在 `solver.cpp` 的 `negatePath()` 中，检查 negated 约束是否为简单模式（`ConstSym` 且为 Equal/Distinct/比较运算）
2. 若是简单模式，直接从约束推导目标值（如 `byte[i] != 0x50` → 随机选一个非 0x50 的值），跳过 Z3
3. 若非简单模式，走原有 Z3 路径
4. 可参考 FUZZOLIC 的 Fuzzy-Sat 实现：https://github.com/season-lab/fuzzolic

**工作量估计**：2-3 天（C++ 层修改 solver.cpp + 测试）

**依赖**：无，可独立实施

---

## 计划 5：LLM 驱动的约束求解

**来源**：
- Cottontail（IEEE S&P 2026）：LLM 驱动的 concolic execution，结构化输入覆盖率 +30-41%
- ConcoLLMic（IEEE S&P 2026）：LLM Agent 做 concolic execution，覆盖率比 KLEE +115-233%

**核心思想**：对结构化输入（SQL、JSON、XML、字体文件等），Z3 的位向量理论无法理解语义。LLM 能理解"这是 SQL 关键字"从而直接生成 `INSERT INTO` 而非逐字节翻转出 `·ELECT`。

**预期收益**：对文本解析器（SQLite、pcre2、xml）覆盖率 +30%+；对二进制格式效果有限。

**实施方案**（两种路径）：

### 路径 A：LLM 作为 Z3 的回退求解器
1. 当 Z3 返回 `unsat` 或 `timeout` 时，将约束序列化为自然语言描述
2. 调用 LLM API（Claude/GPT）生成满足约束的输入
3. 验证 LLM 输出是否真的满足约束（用 Z3 的 `model.evaluate()`）
4. 集成点：`solver.cpp` 的 `negatePath()` 中 Z3 失败后的回退路径

### 路径 B：LLM 作为独立的变异引擎（参考 Cottontail）
1. 收集 SymCC 的路径约束树（ECT），序列化为结构化描述
2. LLM 根据约束树和目标分支生成新输入
3. 新输入注入 AFL queue 或直接作为 SymCC 种子
4. 集成点：`mpi_fuzzing_helper.py` 中新增 LLM Worker 类型

**工作量估计**：1-2 周（路径 A）；2-4 周（路径 B）

**依赖**：LLM API 访问（Claude API / 本地部署的开源 LLM）

**参考实现**：
- https://github.com/Cottontail-Proj/cottontail
- https://github.com/ConcoLLMic/ConcoLLMic

---

## 计划 6：反向路径适配（Backsolver）

**来源**：Backsolver（ACM TOSEM 2025）

**核心思想**：当约束不可解时（`unsat`），不放弃，而是回溯修改**前序路径**使约束变为可解。解决隐式信息流（implicit flow）导致的约束不可解问题。

**示例**：
```c
int type = classify(input);  // implicit flow: type 依赖 input 但无直接约束
if (type == SPECIAL) {        // Z3 无法求解：type 与 input 之间无符号关系
    dangerous_code();
}
```
标准 concolic 无法求解 `type == SPECIAL`（因 `classify` 内部逻辑未被符号化）。Backsolver 会回溯到 `classify` 内部，找到一条使 `type == SPECIAL` 的前序路径，然后从那条路径重新 concolic 执行。

**预期收益**：解决 who 目标中 `getutxent()` 类似的间接依赖问题；对含隐式信息流的程序覆盖率提升显著。

**实施方案**：
1. 在 `negatePath()` 中，当 Z3 返回 `unsat` 时，记录失败的分支和依赖
2. 分析依赖树中是否存在"断链"（依赖的变量无符号约束）
3. 若存在断链，标记需要回溯的函数/代码区域
4. 在后续执行中，对该区域启用更深层的符号化
5. 需要修改 QSYM 的 `syncConstraints` 和 dependency forest

**工作量估计**：2-3 周（需深入修改 QSYM 后端）

**依赖**：对 QSYM 表达式系统的深入理解（已具备）

**参考**：https://dl.acm.org/doi/10.1145/3712194
