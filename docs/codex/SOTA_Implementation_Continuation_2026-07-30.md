# SymCC-Parallel SOTA 实施续档

- 续档日期：2026-07-30
- 基线：F00-F296 汇报快照之后的持续开发
- 文档边界：只记录 Codex 后续实现；不修改 `docs/` 顶层的并发维护文档
- 当前条目：F298

## 1. 为什么需要续档

`Project_Report_2026-07.md`、PPTX 和 F00-F296 技术档案是汇报时点快照。后续开发若
直接改写其中的历史测试数和实验结论，会混淆“当时已经验证的事实”和“快照之后新增的
能力”。本续档采用追加式记录：

1. 新功能继续使用连续 F 编号；
2. 每项必须同时记录研究问题、语义、代码、测试、真实程序证据和未覆盖边界；
3. 过去的实验数字不回填为新功能收益；
4. 新增功能进入全量门禁后，再更新当前总览，历史报告保持快照语义。

## 2. F297：Scalar LLVM Integer Min/Max Symbolization

### 2.1 问题与影响

LLVM 优化器和真实 C/C++ 前端会把边界收紧、长度裁剪和范围选择规范化为：

```text
llvm.smin.*  llvm.smax.*  llvm.umin.*  llvm.umax.*
```

continuation lowering 已在自己的受限 IR 中实现这些操作，但主 `Symbolizer` 没有处理
它们。结果是普通 concolic 执行在调用点发出 `result will be concretized`，之后使用
min/max 结果的分支只能看到具体值，无法反推输入。libarchive 的 7zip 编译对象真实
触发了 10 次该问题。

### 2.2 精确语义

对相同位宽的 scalar bit-vector `a`、`b`，实现保持 LLVM 的有符号或无符号比较域：

```text
smin(a,b) = ite(a <s b, a, b)
smax(a,b) = ite(a >s b, a, b)
umin(a,b) = ite(a <u b, a, b)
umax(a,b) = ite(a >u b, a, b)
```

这里没有 host integer promotion，也没有先扩展到宿主整数再比较。比较和 ITE 都复用
runtime 后端已经存在的 bit-vector primitive，因此 simple 与 QSYM 后端共享同一组合
语义。相等时选择任意一侧不改变 bit-vector 结果。

### 2.3 实现

| 层 | 文件 | 实现 |
| --- | --- | --- |
| 公共 ABI | `runtime/include/RuntimeCommon.h` | 声明四个二元 expression builder |
| runtime 语义 | `runtime/src/RuntimeCommon.cpp` | 用 signed/unsigned comparison + ITE 组合 |
| compiler runtime 表 | `compiler/Runtime.h/.cpp` | 导入四个 `_sym_build_*_{min,max}` 符号 |
| Symbolizer | `compiler/Symbolizer.cpp` | 对 LLVM > 11 的四种 scalar intrinsic 注册 symbolic computation |
| Data Coverage | `compiler/Symbolizer.cpp` | numeric intrinsic 来源追踪遍历全部参数，并纳入 min/max |
| 回归 | `test/integer_min_max.ll` | IR、求解结果、静态 data-origin 与无 concretize 警告 |

vector overload 被显式保留在未支持边界并继续具体化；实现不会用 scalar runtime ABI
错误解释 vector 值。没有新增环境变量或默认行为开关。

### 2.4 自动化证据

定向 lit 同时验证四件事：

1. instrumented IR 分别调用 signed/unsigned min/max builder；
2. 静态常量位于 intrinsic 第二个参数时，比较仍生成
   `_sym_notify_data_cmp_ext`；
3. 编译日志不再出现四种 intrinsic 的 unhandled 警告；
4. 从初始输入 `00 00` 出发，QSYM 实际生成满足四个约束的输入 `2e fb`
   （16-bit little-endian `-1234`）。

执行结果：

| 门禁 | 结果 |
| --- | --- |
| LLVM 18 build（plugin + QSYM runtime） | 通过 |
| LLVM 17 build（plugin + QSYM runtime） | 通过 |
| LLVM 18 `integer_min_max.ll` | 1/1 passed |
| LLVM 17 `integer_min_max.ll` | 1/1 passed |
| LLVM 18 full lit | 210/210 passed，138.45 s |
| LLVM 17 full lit | 209 passed + 1 unsupported，139.56 s |
| Python full unittest | 475/475 passed，82.117 s |
| `git diff --check`（F297 文件） | 通过 |

### 2.5 真实程序复验

复用当前评估中完全相同的 libarchive 源文件、头文件、`-O3` 和 SymCC wrapper 编译
`archive_write_set_format_7zip.c`：

| 指标 | F297 前 | F297 后 |
| --- | ---: | ---: |
| min/max concretize 警告 | 10 | 0 |
| 对象编译 | 成功 | 成功 |
| 其他源码警告 | 1 | 1 |

F297 后对象 SHA-256 为
`46e25209ccdb312aedb2d002921dac0f6e191cf0f0c61548b1c6b134d0d0a06b`。原始命令、
退出状态和日志见
[`libarchive_7zip_object_after_f297.log`](evidence/current-eval-2026-07-30/libarchive_7zip_object_after_f297.log)。

这证明了真实目标的符号语义覆盖缺口被消除，不等于已经证明 coverage 或 wall-time
提升。性能效果仍需等 CPU、多轮 campaign。

### 2.6 正确性边界

- 当前只支持 scalar integer overload；vector/VP intrinsic 未实现；
- 沿用项目的 defined-execution 符号语义，不新增完整 poison/undef 求解模型；
- Data Coverage 只改进来源传播，不改变 AFL data map 的哈希 namespace；
- F297 消除表达式具体化，但可能增加 solver 表达式规模，收益必须通过消融测量；
- 真实程序证据是一次编译语义复验，证据等级为 I/T/B-mechanism，不是 R 级性能结论。

## 3. F298：AFL++ Native SymCC Peer Feedback

### 3.1 发现的真实编排错误

旧 helper 把通过自身全局 edge bitmap 的 SymCC 用例直接追加到
`fuzzer01/queue`。AFL++ 不会把外部进程后来塞入自己 queue 的文件重新加入内存
corpus，因此这些文件主要污染结束后离线并集计数，并不构成可靠在线反馈。

最初修订尝试使用 `afl-fuzz -F DIR`。对 AFL++ 4.40c 源码和真实探针复核后又发现，
`-F` scanner 以 whole-second `st_mtime` 判断新文件；高频 producer 在一次扫描之后同秒
发布的文件可能永远不满足 `mtime > previous_max`。项目本来就有连续编号的
`afl_out/symcc01/queue`，而 AFL 主实例在 `-M` 模式下会把同一 output root 的它识别为
非 AFL 但协议兼容的 campaign peer。因此最终实现使用原生 peer ID cursor，而不是让
SymCC 依赖 `-F` 的粗粒度时间戳。

### 3.2 执行协议

```text
SymCC worker candidate
  -> helper 的全局 edge bitmap merge_delta
  -> 仅 coverage_delta > 0 时原子写 symcc01/queue/id:NNNNNN,...
  -> AFL master sync_fuzzers 扫描严格连续 ID
  -> 在 fuzzer01/.synced/symcc01 保存 4-byte next-ID cursor
  -> AFL 用自己的当前 virgin map 重放
  -> 仅新覆盖输入进入 fuzzer01/queue/...sync:symcc01...
```

`run_hybrid` 删除了 SymCC 的重复 `-F` 目录和 `--afl-sync-dir` 参数；手工 CLI 仍保留
该参数用于真正独立的外部 producer，但发布名已修成严格
`id:NNNNNN,src:NNNNNN`，且 parser 拒绝把 AFL 自身 queue 或 SymCC peer queue 当作
foreign 目录；比较使用`realpath`身份，符号链接别名也不能绕过保护。

普通queue、hang和crash现在使用三个独立ID计数器。旧共享计数器会在出现hang/crash
时让`SymCC/queue`跳号；AFL peer scanner只寻找当前next-ID，遇到缺号会停止推进。
恢复时三类计数器分别从各自目录最大ID继续，保证peer queue保持严格连续。
每个已接纳SymCC内容的SHA-256也在公共路径立即写入analyzed ledger，而不再依赖可选
`-F`分支；AFL之后把它导回master queue时，helper会识别为已处理，避免重复执行同一
concolic输入。

Hybrid 配置会移除继承的 `AFL_NO_SYNC`，设定最短受支持的
`AFL_SYNC_TIME=1`，并启用 `AFL_FINAL_SYNC=1`。停止时先冻结 MPI、honggfuzz、
GRIMOIRE 等 producer，再停 secondary，最后停 master，避免 producer 在 consumer
退出后继续发布。这里保留一个 AFL++ 4.40c 边界：收到终止信号后，final sync 可在单个
testcase 边界看到 `stop_soon` 并中断，因此不能声称硬停止时游标一定完全追平。

### 3.3 可审计指标

benchmark CSV/JSON/text report 新增：

| 字段 | 定义 |
| --- | --- |
| `symcc_peer_published` | `symcc01/queue` 的已接纳文件数 |
| `symcc_peer_scanned` | `.synced/symcc01` 的 native-endian next-ID cursor |
| `symcc_peer_imported` | AFL queue 中来源为 `sync:symcc01` 的保留数 |
| `symcc_peer_not_retained` | `scanned - imported`，已重放但重复或无新覆盖 |
| `symcc_peer_sync_complete` | 游标存在且 `scanned >= published` 时为 1 |

`afl_master_corpus_imported` 仍保留为 AFL 的所有来源聚合值，不能在多 AFL/多 foreign
配置中拿它冒充 SymCC 独占贡献。新来源字段通过 AFL queue 文件名精确归因。

### 3.4 自动化与真实证据

定向自动化覆盖环境配置、foreign directory 去重、严格文件名、内部 queue 拒绝、
4-byte cursor 解码、来源计数、实际 `_batch_triage` 双目录原子发布和 CSV 字段持久化：

| 门禁 | 结果 |
| --- | --- |
| `py_compile`（4 个修改文件） | passed |
| AFL profile/orchestration tests | 13/13 passed |
| current-evaluation report tests | 3/3 passed |
| Python full unittest | 480/480 passed，83.682 s（回环去重后复跑；realpath guard由13项定向覆盖） |
| LLVM 18 full lit | 210/210 passed，133.54 s |
| LLVM 17 full lit | 209 passed + 1 unsupported，133.41 s |
| F298 文件 `git diff --check` | passed |

75 秒真实 hybrid run 使用 AFL++ 4.40c、1 个 AFL master、2 个 SymCC worker，并从
父环境故意注入 `AFL_NO_SYNC=1`。结果为：

| 指标 | 值 |
| --- | ---: |
| SymCC published / AFL scanned | 23 / 22 |
| SymCC-attributed imports | 4 |
| scanned but not retained | 18 |
| hard-stop unscanned tail | 1 |
| AFL edges | 170 / 715（23.78%） |
| AFL executions / throughput | 1,205,634 / 15,862.35 exec/s |

四个导入项都按 `src` ID 找到 SymCC 文件，SHA-256 为 4/4 完全相等。完整 binary
identity、四组 digest 和边界解释见
[`f298_native_peer_sync_2026-07-31.md`](evidence/current-eval-2026-07-30/f298_native_peer_sync_2026-07-31.md)。
这是 I/T/B-mechanism 证据：证明在线转移、原生游标和精确归因，不证明单次 run 的
23.78% 覆盖来自 F298，也不替代等 CPU、多轮对照。

### 3.5 一手契约依据

- [AFL++ 4.40c peer sync 实现](https://github.com/AFLplusplus/AFLplusplus/blob/v4.40c/src/afl-fuzz-init.c)
  说明 `id:%06u`、`.synced/<peer>` 与 next-ID cursor；
- [AFL++ 4.40c foreign scanner](https://github.com/AFLplusplus/AFLplusplus/blob/v4.40c/src/afl-fuzz-run.c)
  说明 `-F` 的 `st_mtime` 判新；
- [AFL++ 4.40c 环境变量文档](https://github.com/AFLplusplus/AFLplusplus/blob/v4.40c/docs/env_variables.md)
  说明 `AFL_SYNC_TIME`、`AFL_FINAL_SYNC` 与 `AFL_NO_SYNC` 优先级。

## 4. 暂停点与后续实验

F298 已完成代码、定向测试、真实机制实验与文档。按当前暂停点，不继续实现新的 SOTA
功能。下一次进入性能研究时，首要工作是等 CPU、多轮比较 native peer sync 开/关，
并把尾部未扫描率、在线 import yield 和 coverage AUC 同时作为响应变量；不能用本次
单 run 代替该结论。
