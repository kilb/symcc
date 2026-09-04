# 2026-08-04 持续优化：稀疏输入正确性与可执行文件绑定 EVP

- 功能编号：F300
- 状态：I/T/E-mechanism
- 代码基线：`146e01b` 上的当前工作树
- 目标：修复 simple backend 的乱序输入偏移错误，并引入可审计的 Empirical Value
  Profiling（EVP）观测、聚合和可信回退边界
- 结论边界：本文证明机制和不变量已实现；不把论文数据当作本项目数据，也不声称本轮
  已取得覆盖率或 solver wall-time 提升

> 状态更新：本文主体保留 F300 采集/聚合检查点。F301 已完成 sidecar、路径 site
> 对齐、SAT-only 求解消费、独立模型验证和完整公式回退；当前实现以
> [`Profile_Guided_Solver_Consumption_2026-08-04.md`](Profile_Guided_Solver_Consumption_2026-08-04.md)
> 为准。

## 1. 本轮审查结论

深度审查先复跑原有 Python 门禁，再沿“输入符号创建 → comparison telemetry →
离线聚合 → 后续求解消费”检查数据不变量，发现并修复两类问题。

1. simple backend 把输入表达式放在按最大偏移扩张的 `vector` 中。首次请求偏移 `N`
   时，旧实现先创建名为 `stdin0` 的变量，再把容器补到 `N`，导致 `[0,N)` 都是空
   表达式；随后请求较小偏移会直接返回空值。大偏移还产生 `O(N)` 空槽。
2. 初版 EVP 虽使用编译器 stable site ID，但离线文件没有绑定目标二进制。不同构建若
   在同一模块/函数/位置放置了语义不同的指令，直接聚合存在陈旧画像污染风险。

同时，F300 当时的 Ruff 审查清除了活跃代码中的未定义类型注解、无效导入和无效局部
状态；当时 `benchmark/qa3_repro` 仍作为历史快照排除。后续 F306 已完成该目录的严格
showmap/telemetry 解析、交错复测、流式兼容探针和全目录 Ruff 修复；当前状态以
[`Interleaved_Fail_Closed_Coverage_Oracle_2026-08-05.md`](Interleaved_Fail_Closed_Coverage_Oracle_2026-08-05.md)
为准。

## 2. simple backend 修复

`runtime/src/backends/simple/Runtime.cpp` 现在用
`unordered_map<size_t, SymExpr>` 按实际访问偏移保存输入变量：

- 变量名直接由真实偏移构造，例如偏移 1 恒为 `stdin1`；
- 同一偏移重复访问返回同一 Z3 AST；
- 先访问高偏移再访问低偏移，不再返回空表达式；
- 空间复杂度由 `O(max_offset)` 改为 `O(distinct_offsets)`。

`test/simple_out_of_order_input.c` 在独立 simple build 中先请求一个逻辑上的巨大偏移
（64 位环境为 1 TiB），再请求偏移 1 并复查高偏移，覆盖 non-null、distinct、
idempotent 和变量命名四个不变量。这个测试验证稀疏索引，不会实际分配 1 TiB 内存。

## 3. 最新技术调研与选择

本轮重点复核了三个近期方向。

| 工作 | 核心思想 | 与当前仓库的关系 |
| --- | --- | --- |
| [FormaliSE 2026: Profile-Guided Constraint Simplification for Symbolic Execution](https://2026.formalise.org/details/Formalise-2026-papers/18/Profile-Guided-Constraint-Simplification-for-Symbolic-Execution) | 用 empirical value profiling 识别小动态值域，再简化约束；论文报告 KLEE 中求解时间降低 12.4%--85.2% | 本轮选择的新增方向；该百分比仅属于论文，不是 SymCC 实测 |
| [TACAS 2026: SymCC-str](https://theory.stanford.edu/~barrett/pubs/CB26-abstract.html) | String/BV 双表示并按约束选择理论 | 当前仓库已有 string artifact、双表示和验证式候选链路，本轮不重复实现 |
| [Cache-a-lot](https://arxiv.org/abs/2504.07642) | 通过变量替换扩大 UNSAT 查询复用 | 当前仓库已有结构统一、PSCache、UNSAT core 和跨前缀验证式复用；仍需后续正式消融 |

选择 EVP 的原因不是它“更新”，而是它与现有稳定 site ID、data comparison 通知、
原子 telemetry 和验证式候选边界可以低耦合集成。F300 只实现可靠观测和证据制品；
后续 F301 已完成第 9 节的求解阶段。

## 4. 实现流程

![F300-F304 EVP 证据、在线发布与成本感知闭环](diagrams/continuous-optimization-2026-08-04.svg)

### 4.1 运行时采集

编译器对“一个动态整数与立即数比较”和 `switch` 已发出 stable site ID、当前具体值
和位宽。QSYM runtime 以 `(executable_sha256, site, bits)` 为逻辑身份：

F301 将普通比较的消费身份进一步收紧为 branch/select 路径约束 site，并要求通知携带
非空 symbolic expression；纯 concrete induction compare 不再占用画像容量。

1. MPI worker 对实际执行目标分块计算 SHA-256，并设置
   `SYMCC_VALUE_PROFILE_CONTEXT`；
2. runtime 只在 telemetry、`SYMCC_VALUE_PROFILE=1` 和合法 64 字节小写摘要同时存在
   时启用；缺失/畸形摘要时画像为空，不能形成未绑定证据；
3. 每次比较把 concrete value 截断到声明位宽，在 site profile 中累计观察数和值频次；
4. 单进程最多保存 512 个 profile、每个最多 8 个不同值；第 9 个值只增加观察数并把
   `saturated` 永久置位；
5. `switch` 只按原始 site 记录一次画像，Data Coverage 的相邻 case 探针继续使用派生
   site，但不会制造重复 EVP profile；
6. 进程退出时画像进入原有原子 telemetry JSON。

C 的整数提升会影响位宽。例如 `uint8_t value == 7` 在普通 C 编译中先提升为 `int`，
因此实测 comparison profile 是 32 位而非 8 位。这是 LLVM IR 语义，不应由分析器
擅自改写。

### 4.2 解析、聚合与证据

`util/hybrid_feedback.py` 对 profile 数、位宽、观察数、不同值数、重复值和频次总和做
有界解析。`util/empirical_value_profile.py` 进一步执行：

- 最多读取 10,000 个、每个不超过 16 MiB 的 JSON 文件；文件轮转、消失、乱码或
  JSON 损坏只跳过该输入，不中止整批分析；
- 无合法 executable context 的记录不进入聚合；不同 context 永远分别建 profile；
- 只有 `observations >= 8`、未饱和、不同值不超过 4 且频次精确覆盖全部观察时，才把
  profile 标为 `limited_domain=true`；
- 输出按 context/site/width/value 排序；只有频次覆盖全部观察时才计算 Shannon
  entropy，饱和/丢计数画像显式写 `null`。canonical JSON 生成 SHA-256；原子临时
  文件、`fsync` 和 `replace` 避免读到半写 artifact；
- `--verify` 从值频次重建语义对象，复核 context、准入、排序、统计和摘要，不能只
  “摘要对上”就接受人工篡改后的派生字段。

### 4.3 可信求解策略

artifact 固定声明策略
`empirical-sat-accept-unsat-full-formula-fallback-v1`。F301 接入求解器时保持：

- 在 empirical domain 下得到 SAT，只能把模型当作 proposal；它通过完整当前路径公式
  求值并经目标程序 replay 后才可接纳；
- 在 empirical domain 下得到 UNSAT 或 unknown，不能据此丢弃分支，必须去掉画像假设
  对完整公式重求；
- 不同 executable context 的画像不能参与同一查询。

因此 EVP 是可撤销的优化提示，而不是程序语义假设。F301 runtime 已按上述边界注入
临时域；任何非 validated SAT 都会删除经验域并回到原有求解链。

## 5. 真实机制观测

对 `test/empirical_value_profile.c` 的 QSYM 实际运行使用一个精确目标二进制摘要
`e3f8cd...4ce5`，产生的 artifact 通过独立 `--verify`：

| 指标 | 实测值 |
| --- | ---: |
| 输入 telemetry | 1 |
| executable contexts | 1 |
| profiles | 3 |
| limited-domain profiles | 1 |
| artifact SHA-256 | `848f621e...dfc3` |

三个 profile 分别表现为：循环 induction site 观察 9 次、出现 8 个已保存值并
`saturated=true`，因此拒绝准入；输入比较 site 观察 8 次且只有一个值，准入；`read`
结果比较只观察 1 次，低于阈值而不准入。两个频次完整的单值画像熵为 0；循环画像
存在第9个未保存值，完整熵不可恢复，因此artifact写`null`而不报告伪精确数字。该运行
只证明采集、饱和、准入和验证器机制，不是性能实验。

## 6. 自动化验证

本轮新增或加强：

- `test/simple_out_of_order_input.c`：simple backend 稀疏/乱序偏移；
- `test/empirical_value_profile.c`：真实 QSYM 画像、C integer promotion、饱和和畸形
  context fail-closed；
- `test/test_empirical_value_profile.py`：跨运行合并、跨 executable 隔离、饱和拒绝、
  摘要篡改、CLI 原子生成与复检；
- `test/test_hybrid_feedback.py`：engine-neutral telemetry context/profile 解析。

最终门禁：

| 门禁 | 结果 |
| --- | ---: |
| Python `unittest` 全量 | 495/495，81.669 s |
| LLVM 18 / QSYM lit 全量 | 216 passed + 1 expected unsupported，209.04 s |
| LLVM 17 / QSYM F300 与 data coverage 定向 | 4 passed + 1 expected unsupported，0.33 s |
| 独立 simple backend 稀疏偏移 | 1/1，0.07 s |
| Ruff（F300 当时排除第三方 public 与历史 `qa3_repro`） | passed |
| 受管 Python `py_compile` | passed |

两项 `unsupported` 都是 `simple_out_of_order_input.c` 在 QSYM-only build 中由
`REQUIRES: simple` 正确过滤；它已在独立 simple build 中通过，不能把 unsupported
写成 pass。LLVM 17/18 runtime和compiler目标均成功增量构建；构建日志仍含上游QSYM
既有 unused/override warning，本轮没有新增编译错误。

## 7. 开销与适用性

运行时状态有硬上界：512 个 site，每个 8 个 `uint64 -> uint32` 计数。开启时每个合格
comparison/switch 做一次有序 map 查找；关闭时只经过布尔门禁。MPI adaptive 模式默认
启用并自动绑定目标摘要，standalone runtime 默认关闭。

EVP 更适合状态枚举、协议阶段、长度/类型码、小计数器等低熵值域。输入字节、哈希、
随机数和高变化循环 induction 往往快速饱和，应被准入器排除。不同输入集、运行时环境
和 corpus 阶段会改变画像，因此它必须作为 campaign-local evidence 管理。

## 8. 已知边界

- F302后executable hash按实际executor route计算，并在device/inode/size/mtime变化时
  重算；正式campaign仍应避免无manifest的目标热替换；
- 内容摘要隔离目标本体，但不自动表达外部 DSO、配置文件或环境变量的语义版本；真正
  消费画像时还应把完整 campaign manifest 纳入上下文；
- stable site ID 对同一已编译目标可重复，但源路径、CFG 或构建变化可改变 ID；这正是
  需要 executable context 的原因；
- 目前只有立即数整数比较和 switch 进入 EVP；普通 branch/select 已由 F301 使用路径
  constraint site 对齐，`memcmp`/字符串、浮点和间接比较仍需要各自的值域定义；
- 当前没有可报告的 solver-time、coverage AUC 或 LAVA-M 漏洞数增益。

## 9. F301-F303 完成情况与后续顺序

1. 已完成 profile artifact 的 context/width/site 精确连接、严格 runtime sidecar 和
   branch/select path-site 对齐；完整 campaign manifest 仍是后续工作；
2. 已完成 empirical assumption 临时求解、SAT 模型验证及 UNSAT/UNKNOWN 完整重求；
3. 已增加命中、query、validation failure、UNSAT/UNKNOWN fallback telemetry；
4. F302已完成MPI滚动聚合、内容寻址代际、版本感知传输、worker原子安装和0-profile撤销；
5. F303已完成按精确域归因的结果反馈、守恒校验、v2抑制证明、滚动证据老化和重新
   探索；真实机制实验记录2次query/2次UNSAT后的抑制、抑制期0行及重准入后reprobe；
6. 后续在固定二进制、固定 corpus、相同 CPU 预算下做 off/on 配对消融，再扩展到公开目标和
   LAVA-M；报告 overhead、solve-time 分布、coverage AUC、time-to-target 和失败目标；
7. 只有完成多轮置信区间和效应量分析后，才把 F300-F304 从 I/T/E-mechanism 提升为 B 或 R。

F303的完整协议、阈值公式、证明工件、真实状态转换和学术边界见
[`Adaptive_Empirical_Domain_Admission_2026-08-04.md`](Adaptive_Empirical_Domain_Admission_2026-08-04.md)。
