# F301：可验证经验值域求解消费

- 状态：I/T/E-mechanism
- 基线：F300 executable-bound EVP 采集与聚合
- 目标：把经验小值域安全接入 QSYM，同时保证画像错误最多造成额外开销，不能造成漏解
- 结论边界：已证明机制、回退和端到端命中；尚无公开 benchmark 多轮性能或覆盖率结论

![F300-F304 EVP 证据、在线发布与求解闭环（F301报告使用当前总图）](diagrams/continuous-optimization-2026-08-04.svg)

## 1. 深度审查发现的问题

F300 只完成采集、聚合和验证，没有让画像进入求解器。实施消费端时，真实测试又发现
一个跨层身份错误：普通 `icmp` 的 data-comparison 画像使用比较指令 stable ID，而
`addJcc` 使用终结分支 stable ID。两者位宽和表达式虽然一致，site key 并不相等；初版
sidecar 能成功加载，却出现 `profiles_loaded=1, attempts=0`。因此，不能仅凭“都使用
stable ID”推断生产者和消费者已经对齐。

修复后，编译器在普通 conditional branch 和非 Hydra `select` 推送路径约束前，以该
**路径约束 site** 额外记录立即数比较的动态操作数。原 comparison site 继续服务 Data
Coverage，不修改其 bitmap 身份；`switch` 原本就以同一 switch site 采集和求解，无需
重复记录。通知携带符号表达式并在空表达式时立即返回，纯 concrete loop induction 不再
占用 512-site 画像容量。QSYM、simple backend 和公共 runtime ABI 同步增加
`_sym_notify_value_profile`，simple backend 明确实现为 no-op。

## 2. 制品与加载协议

`util/empirical_value_profile.py --runtime-output PATH` 只从通过
`verify_value_profile` 的 canonical JSON 生成依赖无关的 ASCII sidecar：

```text
symcc-empirical-value-runtime-v1
artifact_sha256 <64-char-lowercase-sha256>
policy empirical-sat-accept-unsat-full-formula-fallback-v1
profile_count <N>
profile <context_sha256> <site> <bits> <value_count> <value...>
```

运行时解析是 all-or-nothing：文件上限 4 MiB、profile 上限 4,096、每行最多 8 个互异
值，site 必须非零，位宽必须在 1--64，数值必须在位宽范围内，所有十进制整数必须是
canonical 表示，context/artifact 摘要必须是 64 字符小写十六进制。重复 key、尾随
token、截断、超界或文件错误都会清空整个 sidecar 并增加
`empirical_domain_parse_failures`。
聚合器也在跨 telemetry 层面用稳定 SHA-256 优先级保留最多 4,096 个不同
`(context,site,bits)`，而不是只限制
单个输入文件；否则 10,000 个各含 4,096 行的合法文件会在运行时拒绝前先放大内存和
artifact。选择与输入文件顺序无关，验证器独立拒绝超过该上限的制品。

`SYMCC_VALUE_PROFILE_IN` 启用消费；只有 profile context 与当前
`SYMCC_VALUE_PROFILE_CONTEXT` 精确相等的行才加载。其他 context 是合法但不适用的
证据，计入 `empirical_domain_context_skips`。MPI worker 会从实际目标可执行文件流式
计算 context；手工运行必须自行提供正确摘要。运行时不重新计算 `/proc/self/exe`，但
即使 context 或 sidecar 被误配，后述 SAT 验证和完整公式回退仍保证求解语义不被缩窄。

## 3. 求解次序

对一个 interesting 分支，当前 `negatePath` 次序为：

1. 导出 Query IR；显式 defer 成功时移交异步求解；
2. 尝试 Fuzzy-Sat fast solve；
3. 执行 UNSAT-core cache、线性矛盾和 poly cache replay；
4. 若 `(site,bits)` 有经验域，检查目标必须是“恰好一个常量 operand”的二元关系；
5. 用 LLVM `APInt` 按真实 `eq/ne/ult/ule/ugt/uge/slt/sle/sgt/sge` 语义预过滤经验值；
6. 预过滤为空时不调用 Z3，立即进入完整公式；否则同步相关完整前缀，加入反向目标和
   `operand in {v1,...,vn}`，执行一次经验域查询；
7. 经验域 SAT 模型由表达式求值器重新检查反向目标和全部相关前缀，验证成功才写出
   `*-evp-domain`；
8. 经验域 UNSAT、UNKNOWN、验证失败或任何不适用情况均继续原有
   optimistic-first、strict full formula 和 backsolver 链。

经验域 UNSAT 从不进入 poly/UNSAT-core cache，也从不设置最终 branch status。换言之，
经验域查询只能提前提供一个经完整验证的 SAT witness，不能证明原问题 UNSAT。

## 4. 安全性不变量

设完整相关前缀与反向目标为 `F`，画像域为 `D`。经验查询求解 `F ∧ D`。

- 若返回 SAT，模型本身满足 Z3 中的 `F ∧ D`，随后还通过独立表达式求值器检查 `F`；
- 若返回 UNSAT，只能推出 `F ∧ D` 不可满足，不能推出 `F` 不可满足，因此删除 `D`
  后重走原查询；
- 若返回 UNKNOWN、超时、解析失败或模型验证失败，同样删除 `D`；
- profile 的 target SHA、site 和 bits 只决定是否尝试，不承担最终正确性信任。

因此 sidecar 陈旧、被截断或值域不完整的最坏语义后果是多一次探针或少一次加速机会，
而不是跳过一个可满足分支。宏观资源退化仍可能发生，所以该功能默认不因 F300 采集而
自动消费，必须显式设置 `SYMCC_VALUE_PROFILE_IN`。

## 5. 真实机制结果

`test/empirical_value_profile.c` 先用输入 `1` 和 `2` 生成画像，再分别执行命中、经验域
不含目标、context 错配和畸形 sidecar 四条路径。下表是一轮真实 QSYM 遥测；微秒值受
进程启动和系统负载影响，不用于性能结论。

| 场景 | loaded | attempts | domain Z3 | domain SAT/validated | domain UNSAT fallback | total Z3 | generated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 输入 7，画像 `{1,2}` | 1 | 4 | 4 | 1 / 1 | 3 | 20 | 4 |
| 输入 1，反向目标不在画像域 | 1 | 4 | 0 | 0 / 0 | 4 | 17 | 4 |
| context 错配 | 0 | 0 | 0 | 0 / 0 | 0 | 17 | 4 |
| sidecar 尾随 token | 0 | 0 | 0 | 0 / 0 | 0 | 17 | 4 |

第二行证明 APInt 预过滤在经验域不可能满足目标时不会增加 Z3 查询，同时完整公式仍生成
4 个候选。总 Z3 还包含专门构造的 9 次符号高熵饱和循环，不能用来比较 sidecar
性能。第一行真实写出 `000000-evp-domain`；同一稳定值循环后续更强前缀使经验域冲突，
3 次均回退而没有被误记为全局 UNSAT。错配行有 `context_skips=1`，畸形行有
`parse_failures=1`。

`test/empirical_value_profile_signed.c` 用 `-1` 和 `1` 建画像，再从输入 `5` 翻转
`(int8_t)value < 0`。结果为 loaded=1、attempts=1、domain Z3=1、SAT=1、validated=1、
generated=1，覆盖了 32 位 C integer promotion 下的有符号 APInt 过滤和模型生成。
`test/empirical_value_profile_switch.c` 则证明 switch 通知每次执行只画像一次，并在三个
case 共用的稳定 site 上得到 attempts=3、domain Z3=2、SAT/validated=1/1；其中两次
经验域冲突继续完整回退。

## 6. 测试与可观测性

新增或加强的证据包括：

- Python 画像工具：sidecar 物化、8 值运行时上限、摘要篡改拒绝、CLI 原子写；
- QSYM lit：真实 SAT 命中、零查询语义预过滤、完整回退、context 隔离、畸形文件拒绝；
- signed lit：有符号 promoted comparison 的端到端命中；
- switch lit：单次画像、共享 site 和逐 case SAT/回退；
- engine-neutral parser：全部 `empirical_domain_*` 计数和
  `empirical_domain_solver` capability；
- LLVM 18、LLVM 17 与 simple backend 编译，确认 ABI 一致。

关键遥测为 loaded/skips/parse failures、attempts/solver queries、SAT/validated、
validation failures 以及 UNSAT/UNKNOWN fallbacks。`attempts - solver_queries` 是由精确
谓词预过滤直接拒绝的域数；`validated / solver_queries` 才是经验域的实际有效率。

最终顺序门禁如下：

| 门禁 | 结果 |
| --- | ---: |
| Python unittest | 497/497，81.953 s |
| LLVM 18 / QSYM lit | 218 passed + 1 expected unsupported，134.51 s |
| LLVM 17 / QSYM lit | 217 passed + 2 expected unsupported，134.22 s |
| 独立 simple backend 稀疏 offset lit | 1/1，0.07 s |
| simple backend 新 ABI 分支程序 | compile + execute passed |

LLVM 18 的一项 unsupported 是 simple-only 稀疏 offset 测试；LLVM 17 另有一项既有版本
能力过滤。unsupported 未计作 pass。双版本 compiler/runtime 均在最终源代码上重建；
构建仍显示上游 QSYM 的既有 unused/override warning，没有新增编译错误。
全量 lit 后增加的 parser I/O error 门禁和跨文件全局 4,096-profile 上限，由双 LLVM
F301 三项定向 lit `3/3 + 3/3`、最终双版本重建和上述 497 项 Python 全量覆盖。

## 7. 科研边界与下一步

F301 是 profile-guided solver consumption 的可靠最小闭环，不等同于 FormaliSE 2026
论文在 KLEE 上的性能结果。后续F302已经实现自动滚动聚合、版本化MPI发布、worker
本地安装和显式域撤销。当前仍未实现或未证明的部分包括：

- 按 site/路径上下文学习是否值得尝试，避免循环中的低收益探针；
- 真实 LAVA-M、libarchive、xml 等目标的等 CPU、多轮 solver-time、coverage AUC 和
  effect size；
- 把 DSO、配置和环境语义纳入比 executable SHA 更完整的 campaign context；
- 与 poly cache、Query IR service 和 SMT algorithm scheduler 的联合消融。

正式启用策略应先观察 `validated / solver_queries` 和 fallback 比率；若探针收益不足，
协调器应按 site 降权或停用，而不是扩大经验域并牺牲选择性。
