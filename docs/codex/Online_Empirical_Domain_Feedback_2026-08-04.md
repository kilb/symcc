# F302：并行经验值域在线反馈闭环

- 状态：已实现，I/T/E-mechanism
- 日期：2026-08-04
- 前置能力：F300 bounded EVP、F301 verified SAT-only consumption

![F300-F304 在线经验值域闭环（F302报告使用当前总图）](diagrams/continuous-optimization-2026-08-04.svg)

## 1. 为什么 F301 还不是完整并行系统

F301 已经证明 QSYM 能安全消费严格 sidecar，但深度审查真实 MPI 路径后发现：

1. worker 自动设置 `SYMCC_VALUE_PROFILE=1` 并产生画像遥测；
2. master 能收到 `SolverTelemetry`；
3. 但 master 没有聚合画像、发布 sidecar，也没有让后续 worker 设置
   `SYMCC_VALUE_PROFILE_IN`。

因此手工 lit 测试可以得到 `evp-domain`，真实并行 campaign 默认只能采集，不能形成
“执行 -> 学习 -> 再执行”的闭环。F302 修复的是系统集成缺口，不是增加另一个孤立的
求解启发式。

审查还发现两个次生问题：portfolio 切换可执行文件后，context 仍可能沿用启动命令的
摘要；已经发布的小域后来失效时，如果不发布撤销事件，worker 会无限期保留旧 sidecar。
后者不会突破 F301 的 SAT 验证边界，但会持续制造无效查询。

## 2. 完整执行次序

一次代际的严格顺序如下：

1. worker 执行当前输入，QSYM 按 `(executable SHA-256, path site, bits)` 收集最多
   512 个 site、每 site 最多 8 个值；
2. runtime 原子写 telemetry，worker 经 `SolverTelemetry` 规范化后随 `TAG_RESULT`
   返回；
3. master 的 `OnlineValueProfileCoordinator` 使用和离线 analyzer 相同的严格行校验，
   将有效记录加入有界滚动窗口；
4. 到发布间隔后，master 重新聚合窗口，只准入样本数足够、未饱和、频次完整的小域；
5. canonical JSON 以自身 SHA-256 命名，先原子写入 `generations/`，再物化严格 sidecar；
6. sidecar 的完整字节 SHA-256 是传输版本。master 在 work lease 中总是发送版本，只在
   worker 未确认该版本时发送内容；
7. worker 校验内容摘要并原子安装到自己的临时目录，成功后才设置
   `SYMCC_VALUE_PROFILE_IN`；下一次 `TAG_READY` 回报已安装版本；
8. QSYM 执行 F301 的 APInt 过滤、相关前缀经验域查询、模型验证和完整公式回退；
9. attempts、SAT、validated 和 fallback 计数进入下一条 telemetry，由 coordinator
   累积为闭环证据。

MPI 消息携带 sidecar 内容，worker 不读取 master 路径，所以协议不要求多个主机共享
文件系统。同一版本只传一次；观察次数变化但域集合不变时，滚动state仍保存新记录，
solver 语义版本保持不变，不重复广播。

## 3. 域撤销与故障语义

### 3.1 显式 tombstone

若原域 `{1, 7}` 后来观察到第三个值，而策略为 `max_distinct=2`，新聚合结果不再准入
该 site。F302 发布合法的空 sidecar：

```text
symcc-empirical-value-runtime-v1
artifact_sha256 <new-artifact>
policy empirical-sat-accept-unsat-full-formula-fallback-v1
profile_count 0
```

空代际也有版本并发送给所有 worker。它覆盖旧文件，使后续 runtime 加载 0 个域。不能
简单地“不再发布”，因为消息缺失无法区分“没有变化”和“撤销已有状态”。

### 3.2 fail-open 不变量

- payload 缺失、类型错误、超过 4 MiB 或摘要不符：本工作项不启用旧代际；即使 worker
  声明版本已缓存，也会在有界读取后重新校验本地文件摘要，截断或外部改写不能绕过校验；
- artifact/sidecar/checkpoint 写失败：记录 publication failure，普通 concolic campaign
  继续，协调器保留 dirty 状态供以后重试；
- checkpoint 超过 16 MiB：只裁掉最旧的持久化记录，内存窗口不变；当前实现会封存
  `records_complete=false`并在重启时从存活记录重新物化，不能沿用较大窗口的旧决策；
- generation 清理最多保留 8 个 artifact，并始终保护 current artifact；
- runtime sidecar 或 context 错误：F301 parser 整体拒绝；
- 经验域 UNSAT、UNKNOWN 或模型验证失败：完整公式重求，绝不传播经验 UNSAT。

因此在线层最多影响性能机会，不授权 branch UNSAT，也不改变 AFL coverage novelty。

## 4. executable context 修复

旧 worker 在启动时对 `args.target[0]` 求一次 SHA-256。Executor portfolio 若把当前
工作项路由到另一条 command，遥测可能被错误命名到启动目标。F302 改为：

1. 在 route 决定后解析实际 `execution_route.command[0]`；
2. 以 `(device, inode, size, mtime_ns)` 为缓存签名；
3. 签名变化或首次出现时重新流式计算可执行文件 SHA-256；
4. 只在本次 `execution_env` 设置对应 context。

这既避免每个输入重复哈希大型 binary，也能识别 campaign 中被原子替换的目标文件。
用户显式提供的合法 context 仍保持显式配置语义。

## 5. 有界资源与持久化

| 资源 | 默认值 | 硬边界/行为 |
| --- | ---: | --- |
| 滚动 telemetry | 256 条 | 1--10,000 |
| 单 telemetry profile | runtime 最多 512 | analyzer 全局最多 4,096 key |
| 单 site values | 8 | 第 9 个值使 runtime profile 饱和 |
| runtime sidecar | 按需传输 | 最大 4 MiB |
| recovery checkpoint | `state.json` | 最大 16 MiB，旧记录优先逐出 |
| immutable generations | 8 | current 永不被 GC |
| publish interval | 1 秒 | 0--3,600 秒，0 仅建议测试 |

`state.json` 同时保留 records observed、generation/no-op/failure 数以及十二项 F301/F304 消费
计数。恢复时，`current.runtime` 必须和其 artifact 重新验证、重新物化后逐字节相等，
否则不广播该代际。若新的 rolling-window 大小、`min_observations` 或
`max_distinct_values` 与持久化代际不一致，coordinator 将恢复记录标为 dirty，在第一次
分派前按新策略重新物化；新策略不再准入旧域时发布空 tombstone。

后续恢复加固还把`current_artifact_sha256`和`records_complete`写入state。新runtime配
旧state、缺失state、裁剪state、旧版无代际state或state声明的runtime缺失均先标记
dirty；因此跨三个独立原子文件的崩溃窗口不会把不匹配的记录和sidecar当作一致快照。

## 6. 测试与真实 MPI 结果

### 6.1 自动化测试

`test/test_online_value_profile.py` 覆盖：

- 两个 worker 观察合并后达到准入阈值；
- counts 改变但域不变时不产生冗余代际；
- 新值改变域时换代，超过阈值时发布空 tombstone；
- 不同 executable context 不合并；
- state/runtime/artifact 三者一致才允许恢复，配置漂移会触发重新聚合；
- payload SHA 错误、版本缺内容、缓存文件被截断和 publication I/O failure 均 fail open；
- rolling window 与畸形 65-value 行保持有界，窗口缩小时只重新物化保留样本；
- 发布间隔到期后无需新的观测即可重试 dirty 代际。

最终 Python unittest 为 `503/503`（82.039 秒，shell real 82.554 秒）。LLVM 18 全量
为 `219 passed + 1 unsupported`（138.48 秒），LLVM 17 为
`218 passed + 2 unsupported`（135.24 秒）；F300-F302 过滤组均为 `4/4`。

### 6.2 真实进程拓扑

证据目录：
[`evidence/f302-online-evp-2026-08-04/`](evidence/f302-online-evp-2026-08-04/)。

实验使用 1 master + 1 QSYM worker、独立 AFL-instrumented showmap oracle、当前
`empirical_value_profile.c` 目标和一个 `0x01` seed。为了在 8 秒内覆盖换代与撤销，
设置 `min_observations=1`、`max_distinct=2`、发布间隔 0；外部 timeout 返回 124 是预期
停止条件。

| 指标 | 实测 |
| --- | ---: |
| 有效 telemetry records | 17 |
| semantic generations | 3 |
| semantic no-op publish cycles | 14 |
| publication/checkpoint failures | 0 / 0 |
| profiles loaded | 4 |
| empirical attempts / domain Z3 | 4 / 4 |
| empirical SAT / validated | 1 / 1 |
| empirical UNSAT fallback | 3 |
| validation/unknown failures | 0 / 0 |
| 最终 sidecar | `profile_count 0` tombstone |
| SymCC peer queue | 3 retained inputs |

三代 sidecar 版本前缀依次为 `dd2c7d417e1d`、`34edaea327a2`、`7b8528f07145`；最后
一代撤销旧域。这个结果证明真实 MPI 中的采集、聚合、传输、安装、消费、反馈和撤销
全部发生，不是 mock 协议推断。

## 7. 科研边界与下一步实验

F302 解决“实现存在但 campaign 不使用”的系统完整性问题，并为在线 profile-guided
solving 建立可审计代际。它没有证明 EVP 在 LAVA-M、Magma、FuzzBench 或真实 parser
上降低总求解时间或提高覆盖率。当前 8 秒单目标实验只能归为机制证据。

下一阶段应冻结同一 binary/corpus/CPU 配置，对 `online=0/1` 做至少 20 轮配对实验，
报告 coverage AUC、accepted/CPU-hour、solver time、domain query 净节省、validation
failure、tombstone 频率和 sidecar 传输字节。只有这些指标的置信区间和多重比较校正完成
后，才能把 F302 从 I/T/E-mechanism 提升为 R 级性能结论。

F303已经在此交付协议之上增加精确域结果反馈、证明式低收益抑制和滚动重新探索；
它不改写本报告的F302历史实测。后续机制和证据见
[`Adaptive_Empirical_Domain_Admission_2026-08-04.md`](Adaptive_Empirical_Domain_Admission_2026-08-04.md)。
