# F464：第四轮深度审查与语义、传输、缩放闭合

- 日期：2026-09-03
- 范围：LLVM GEP 符号语义、hybrid RESULT 传输、AFL 覆盖同步、data coverage
  并发运行时、MPI 生命周期、并行规模模型与源码交付门禁
- 证据级别：I/T/E-mechanism

## 1. 审查目的

F463 已闭合若干失败原子性问题，但完整复审仍发现三类不能靠增加并行度解决的基础风险：
符号表达式与 LLVM 具体语义可能不一致；控制面会因大 RESULT、串行 showmap 或隐含线程池
放大内存和 CPU 压力；规模模型可能把重复轮次压成均值后高估拟合质量。本轮按“语义正确性
优先、传输有界性其次、性能结论可信度最后”的顺序修复，并为每个反例增加回归。

## 2. 发现与修复

| 子系统 | 反例或风险 | 修复后的约束 |
| --- | --- | --- |
| LLVM GEP | 指针宽度、index width、源索引宽度不同；结构字段偏移可能超过 32 位；scalable vector 含运行时 `vscale`；non-integral pointer 不支持整数化 | 索引按 address-space index width 符号扩展/截断并在该宽度回绕；结构偏移保留为 64 位；高指针位保留；元素跨度使用 allocation size 和实际 `vscale`；不支持的地址空间失败关闭 |
| hybrid RESULT | worker 将候选字节整体 pickle，Master admission 队列可保留多份大对象；候选与 proposal 的 coverage 预算可被分别耗尽 | 正常路径只传 SHA-256 对象标识和长度；Master admission 与 triage 两次稳定复验，逐对象物化；整条 RESULT 共用一个 coverage 行预算 |
| AFL baseline | showmap 失败对象只有周期性重扫机会；新 queue 可持续饿死 retry；多槽可闲置 | SQLite retry ledger 不丢弃失败项；单槽在新任务/重试间轮换；多槽预留后再双向回填 |
| data coverage | 多线程首次进入 interposer 可能递归初始化；非原子 map 自增丢计数或写回零 | `pthread_once`、线程局部重入保护、独立 raw compare；CAS 自增且 255 后回到 1 |
| MPI 生命周期 | 非法 import-time 配置、无 worker、输出目录/队列锁冲突或 Master 异常可能在清理成功后被误报为运行成功 | 配置解析总化并有界；失败路径进入同一 STOP/ACK 清理，但运行状态始终为失败并最终非零退出 |
| 规模模型 | 辅助线程池未计入资源；重复轮次均值会隐藏组内噪声；bootstrap 重复拟合同一重采样 | 显式辅助槽账本；精确保留组内 SSE；按 seed-count vector 缓存确定性拟合 |
| 源码交付 | 大量实现依赖未跟踪模块，工作树测试不能代表 clean clone | manifest 固定路径、类型、摘要和 runtime gitlink；CI 要求 manifest 中普通文件均被 Git 跟踪 |

## 3. LLVM GEP 语义

LLVM GEP 的整数偏移不是无条件按宿主指针宽度计算。对每个非结构索引，当前实现执行：

```text
source index --sign-extend/truncate--> address-space index width
             --multiply by TypeAllocSize (fixed or minimum * vscale)-->
low(pointer, index width) + offset  [mod 2^index_width]
             --combine with unchanged high pointer bits--> result pointer
```

结构字段继续使用 `StructLayout` 的 64 位 allocation offset，避免大于 4 GiB 的合法布局在宿主
`unsigned` 中截断。固定类型由 `getTypeAllocSize()` 给出
含 padding 的步长；scalable vector 用 `llvm.vscale` 乘已知最小 allocation size。若目标的
pointer/index 布局超出运行时表达式 ABI，编译器给出警告并把结果 concretize，而不是构造
位宽错误的表达式。non-integral pointer address space 同样失败关闭，因为当前运行时以
整数位向量表示地址，不能为该类指针建立可靠的 `ptrtoint` 语义。

`test/gep_index_semantics.ll` 同时覆盖 `p0:64:64:64:32` 下的负窄索引、128 位宽索引、
64 位 index address space 中超过 4 GiB 的结构字段偏移和 scalable-vector 元素跨度。
LLVM 17 与 LLVM 18 均通过该测试。

## 4. RESULT 内容寻址传输

worker 先把每个候选写入 `<symcc-output>/.result_objects/<digest>`，再发送
`object_id/object_size`。Master 的异步 admission 不加载候选内容，只检查 schema、数量、总逻辑
字节预算以及对象稳定身份；真正 triage 某一候选时再次读取并核对 SHA-256 和长度。因此正常
内存上界由“小型 RESULT 元数据 + 当前物化对象”决定，不再近似为
`admission_capacity * max_result_bytes`。worker 还把每个候选 bitmap 缩为相对本地单调并集的
新增 bits；同一 RESULT 内早先候选已携带的旧边不再重复发送。所有候选与可选 proposal bitmap
共同受单消息聚合上限约束，超限整批拒绝并报告 `coverage_rows`，不是抽样或静默截断。worker
只在整批成功后同时提交本地 coverage 与跨任务内容摘要；预算失败不会留下已覆盖或已处理的
假象，后续任务仍可重试同一输出。

干净的全 worker STOP/ACK 后会删除 transport-only store。若 worker 发布 CAS 失败，仍可使用原有
有界 inline bytes 兼容路径；`SYMCC_RESULT_OBJECT_TRANSPORT=0` 可显式关闭新传输。边界必须明确：
mpi4py 在 schema 校验前的初始 pickle 接收分配，以及兼容 inline fallback，不受 CAS 路径本身约束。

## 5. 覆盖反馈并发协议

`AflCoverageBridge` 只有 Master 线程能够提交权威 bitmap；线程池只运行 showmap 并返回观察值。
成功结果需满足“执行成功、bitmap 合法、文件身份未变化”，随后才按以下顺序提交：

```text
showmap observation -> global coverage claim -> baseline merge
                    -> exact file identity -> SQLite commit
```

失败或暂时不存在的路径进入无损 SQLite retry ledger。`SYMCC_AFL_COVERAGE_RETRIES` 只限制一批
选择量，不是保留上限。单 showmap 槽在新 queue 与到期 retry 之间轮换；多槽先为 retry 保留至少
四分之一，再把任何空余槽依次回填给另一侧。候选查询会跳过已在飞行中的 ledger 头部，避免它们
遮挡后续到期项。

data coverage interposer 的初始化由 `pthread_once` 建立跨线程 happens-before。初始化期间若
`dlsym` 再次触发比较函数，线程局部标记令调用落到不依赖 libc 的 raw compare，避免递归和死锁。
共享 AFL map 使用 relaxed CAS 做 never-zero 自增；这里只需要原子计数和 AFL bucket 可见性，不建立
目标程序的同步关系。

## 6. 并行规模模型

hybrid Master 输出 query、result admission、coverage、density 和 auxiliary showmap 五类后台
计算池的配置并发总数。benchmark CSV 将其记录为 `auxiliary_compute_slots`。资源上限为：

```text
pure MPI workers = compute_roles(physical_cores - auxiliary_slots,
                                 workers_per_master)
hybrid compute   = physical_cores - observed_masters - auxiliary_slots
```

若 hybrid 旧数据缺少该字段、任一成功/失败/超时尝试使用不同辅助配置，或资源预算连一个
coordinator 和一个 compute worker 都容纳不了，模型仍可输出探索性拟合，但
`decision_eligible=false`，不会给出正式推荐。

相同并行层级的重复轮次以 `(weighted mean, total weight, within-level SSE)` 作为精确充分统计量。
USL 和 coverage saturation 用聚合均值拟合参数，但 R²/RMSE 把组内 SSE（包括 coverage 的基线
锚点波动）加回，因此有噪声的重复轮次不再产生虚假的完美拟合。配对 bootstrap 仍生成请求数量
的重复分布；相同 seed 计数向量只拟合一次，
降低小样本多次重复网格搜索的成本，不改变百分位样本的频数。

该模型以 USL、覆盖饱和曲线和物理资源三者的最小值形成探索上限。它是决策辅助模型，不是对任意
目标都成立的扩展定律；正式结论仍要求完整 seed block、相同时间预算、非抽样 endpoint coverage、
足够重复和合格拟合质量。

## 7. 验证

定向验证覆盖：

- GEP 窄/宽索引、低位回绕、高位保留、超过 4 GiB 的结构偏移、allocation padding 和
  scalable `vscale`；
- RESULT 对象引用、pickle 体积下降、二次摘要校验与篡改拒绝；
- coverage 缺失路径持久重试、单槽公平性、多槽满载回填和重启恢复；
- 32 线程并发首次 data coverage 初始化、CAS 累加及 `-Wall -Wextra -Werror` 构建；
- 重复轮次噪声进入 R²/RMSE、辅助槽缺失/变化/资源不足拒绝、bootstrap 缓存等价性；
- 非法启动参数、Master 异常和协作停止边界。

本报告冻结前的定向结果为 coverage **9 passed**，规模模型与当前评估 **32 passed**，RESULT/
worker coverage 相关 **17 passed + 26 subtests**，GEP 在 LLVM 17/18 各 **1 passed**。

最终冻结结果如下：

- 完整 Python：**1668 passed + 616 subtests**，342.17 秒，零失败；
- LLVM 18：349 项中 **348 passed + 1 unsupported**，306.90 秒；
- LLVM 17：隔离组合共 349 项，**347 passed + 2 unsupported**；32 路主套件的
  346 个非 provenance 项用时 498.60 秒，`test_research_protocol.py` 独立通过用时 122.33 秒；
- Ruff、项目 Python `py_compile`、data coverage 严格 C11
  `-Wall -Wextra -Werror` 构建和 `git diff --check` 全部通过；
- source-delivery manifest 覆盖 **713 个普通文件 + 1 个 runtime gitlink**，树摘要为
  `30aa2e43d0678b3eda0e6346755016f63227a7fa45af17780ade1668e0b3db4d`，当前内容精确校验通过。

LLVM 17 的 provenance 用例会对整个工作树状态取摘要，必须与可能创建或删除未跟踪产物的其他
lit 用例隔离运行；192 路默认并发还曾使 PALRUP 用例受资源争用影响，但该用例独立运行在
0.92 秒内通过。因此冻结口径采用 32 路非 provenance 主套件加单独 provenance，而不是把
测试基础设施竞态计为产品失败。这些结果仍是机制/回归证据，不能替代公开目标的长时性能实验。

## 8. 结论与边界

F464 修正的是会系统性扭曲路径语义、Master 资源占用和扩展性结论的基础问题。它证明了所列协议
在回归和故障注入范围内成立，不证明公共目标上的覆盖率、求解速度或缺陷发现率提升。仍需保留：

1. MPI 初始反序列化没有独立的字节流 framing，损坏 peer 的预校验分配仍是边界；
2. 单 Master 的权威 coverage/corpus 提交仍可能成为更高规模下的串行瓶颈；
3. `.result_objects` 在运行期间按内容增长，只在全 worker 干净 ACK 后整体删除；长时任务仍需后续加入
   引用计数或分代 GC；
4. 辅助槽采用配置并发的保守上界，不能替代 CPU affinity 和实际利用率遥测；
5. 当前统计模型没有跨机器、跨目标外推保证，正式天花板需由长时、多轮、样本外实验验证；
6. source-delivery gate 只有在 manifest 文件被版本控制提交并在 clean clone 复跑后，才能证明仓库可交付。
