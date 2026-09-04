# F368：Failure-Atomic Persistent Solver Generation Reset

> 日期：2026-08-11  
> 状态：已实现并完成本地机制验证  
> 证据等级：I/T/E-mechanism  
> 上一阶段：F367 Sealed Query Artifact FD Handoff

## 1. 研究背景

F367解决了“solver最终读取的字节是否仍是claim时验证的字节”：full、prefix和target
SMT2被复制到四seal memfd，一次性helper继承full fd，持久化helper通过Unix
`SCM_RIGHTS`接收prefix/target fd。该设计关闭了pathname替换窗口，但持久化helper的一个
请求横跨两个独立有序通道：

1. Unix `SOCK_SEQPACKET`上传递带query ID的descriptor datagram；
2. stdin pipe上传递query ID、timeout、prefix key和两个descriptor marker；
3. stdout pipe返回带request ID的JSON。

两个通道各自有序，不等于三步构成一个原子事务。旧Python控制流先发送fd，再验证并写
文本行；如果`sendmsg`成功后发生字段错误、pipe写失败或flush异常，helper看不到文本请求，
但fd datagram仍留在socket接收队列中。后续请求发送自己的fd和文本后，helper会先取到旧
datagram。若helper只信任文本request ID，错误artifact会被静默绑定；F367的C++ helper虽会
检测ID不一致并返回error，当前generation仍已失去可证明的通道对齐关系。

因此本轮目标不是“重试当前请求”，而是建立更强不变量：

> 任一请求只要无法证明descriptor、文本和响应三段全部完成，同一helper generation就不得
> 服务下一请求；下一次调用必须使用新的process、pipe和socketpair。

## 2. 可执行反例

证据driver使用真实`SOCK_SEQPACKET + SCM_RIGHTS`和production sealed leases：

1. 请求A成功发送prefix/target fd；
2. 在文本提交前注入`OSError`；
3. 模拟旧实现，不执行generation失效；
4. 请求B发送自己的fd和文本；
5. helper收到B的文本，却从socket取出A的query ID和A的artifact摘要。

保存结果为：

| 观察 | 旧行为 |
| --- | ---: |
| 注入故障被观察 | true |
| helper generation被复用 | true |
| descriptor request ID与文本ID不同 | true |
| 实际消费A而非B的artifacts | true |
| helper启动次数 | 1 |

这不是求解器逻辑错误，而是跨通道事务边界错误。单独验证每个memfd的seal、SHA或regular
类型无法解决消息配对问题。

## 3. 正确性模型

### 3.1 Generation状态

持久化solver把一个子进程及其三个端点视为不可拆分generation：

```text
G = (process group, stdin pipe, stdout pipe, descriptor socketpair)
```

`G`只有`valid`和`invalid`两类可复用状态。下列任一事件把`G`单调转为`invalid`：

- fd复制或`sendmsg`失败；
- 文本write/flush失败；
- 响应等待超时或EOF；
- 响应缺少换行、超过16 MiB、包含多响应或不是严格UTF-8；
- JSON无效、顶层不是object或response ID不等于request ID；
- 调用线程遭遇普通异常或`BaseException`；
- portfolio取消当前helper。

invalid generation不尝试“排空一个消息”或猜测管道位置，因为发生故障时无法证明究竟有
多少字节、多少fd、多少响应已由内核或对端接收。正确恢复动作是关闭父端descriptor channel、
终止整个子进程组、关闭所有stdio stream，并把`_process_valid`保持为false。下一请求只能经
`_ensure_process()`创建新的socketpair和helper。

### 3.2 事务不变量

实现维护四条不变量：

1. **Pre-transport validation**：空字段及tab/CR/LF在选择helper、复制fd和发送消息前拒绝；
2. **Generation isolation**：发生不确定I/O后，旧generation永不接收下一请求；
3. **Bounded response**：响应必须在`query timeout + 5 s`内、单行、严格UTF-8且不超过
   16 MiB；
4. **Request-response binding**：JSON中的request ID必须与已发送文本ID严格一致，否则整代
   失效。

## 4. 实现设计

### 4.1 前置字段准入

`PersistentSubprocessSolver.__call__()`首先规范化四个普通字段：query ID、timeout、prefix
key和witness。任何字段为空或包含协议分隔字符时直接抛`ValueError`。此时没有调用
`_ensure_process()`或`_artifact_fields()`，所以健康helper及descriptor队列保持不变。

### 4.2 单调generation失效

新增`_process_valid`和`_invalidate_process(process)`：

1. 在state lock内确认传入process仍是current generation；
2. 先置`_process_valid = False`并从对象中分离descriptor socket；
3. 关闭父端socket，使阻塞`recvmsg`立即失去通道；
4. 以`SIGTERM`终止process group，短暂等待后必要时升级`SIGKILL`；
5. 幂等关闭stdin/stdout/stderr；
6. 保留旧Popen仅作已终止对象，下一次`_ensure_process()`原子替换它。

`__call__()`在helper选定后的整个send/write/read/parse范围使用`except BaseException`执行上述
回收，再原样传播异常。这样普通错误和进程级中断都不能留下可复用的半事务generation。

### 4.3 有界响应reader

旧代码直接调用`TextIOWrapper.readline()`，没有总deadline和内存上限。新reader：

- 使用`selectors.DefaultSelector`等待stdout可读；Linux默认实现为epoll，避免
  `select.select`的`FD_SETSIZE`限制；
- 使用`os.read`按最多64 KiB分块读取；
- 总deadline由monotonic clock计算，EINTR只重试剩余预算；
- 在构造超过16 MiB的Python字符串前拒绝超限；
- 只接受恰好一条newline-terminated响应，拒绝同一read中的尾随第二响应；
- 最后才执行strict UTF-8 decode和JSON解析。

该机制把“solver内部Z3 timeout”与“helper控制面无响应”分开：即使solver没有遵守内部超时，
master线程也会在外层预算结束后回收整个generation。

### 4.4 正常复用路径

正常成功请求仍保留F367的prefix context reuse：

```text
validate fields
  -> ensure valid generation
  -> dup sealed prefix/target
  -> sendmsg(query_id, SCM_RIGHTS)
  -> write + flush text request
  -> bounded read one JSON line
  -> verify response request_id
  -> return result; keep generation/cache
```

只有失败路径发生冷启动；成功路径不额外创建helper，也不改变C++ prefix cache语义。

## 5. 测试设计

### 5.1 现有测试身份内扩展

为保持规范pytest身份清单可比较，本轮没有新增test function，而是在
`test_cancelled_persistent_helper_restarts_cold`中增加三组断言：

- response ID错配必须终止旧PID，下一请求来自新PID；
- 将响应上限故障注入为16 B后，64 B响应必须拒绝并冷启动恢复；
- fd发送完成后注入异常，旧process必须已经退出，下一sealed lease收到与自身query ID和
  prefix/target摘要一致的结果。

### 5.2 独立反事实driver

`run_generation_reset_checks.py`执行三组独立场景：

| 场景 | 验证内容 |
| --- | --- |
| legacy | 禁用generation回收后稳定复现A-fd/B-text错配 |
| production | A失败后旧进程终止，B在第二generation读取B artifacts |
| protocol | 字段预验证、错误ID、超长行、部分行超时及每次冷恢复 |

driver连续两次输出字节完全相同，JSON SHA-256为
`69232b35fc7e276f869b1c1aa8816c3009d75908a508218ba83cb0db499e5915`。

## 6. 验证结果

| 验证范围 | 结果 | 时间 |
| --- | ---: | ---: |
| QueryStore定向 | 20 passed + 2 subtests | 3.33 s |
| QueryStore/QF_BV/semantic/distributed关联 | 227 passed + 20 subtests | 29.94 s |
| query/string过滤lit | 10 passed，216 excluded | 3.98 s |
| pytest规范身份重建 | 787/787，字节一致 | - |
| 16项能力关闭完整门禁 | 787 passed + 127 subtests | 115.80 s |

完整门禁中skip、xfail、xpass、deselected、collection error、missing/unexpected/duplicate nodeid
均为0，16项命令、Python模块和动态库能力无缺失。

production证据显示：

- 注入的post-send故障被传播；
- 失败generation已终止；
- recovery请求由第二helper generation执行；
- fd datagram ID等于recovery文本ID；
- helper读取的两个SHA-256等于recovery prefix/target而非前一请求。

protocol证据显示4次helper启动分别覆盖初始、ID错配后、超长响应后和部分响应超时后的
generation。字段预验证不会回收健康generation，其余三类不确定状态全部回收。

## 7. 先进性、创新性与挑战

### 7.1 从artifact完整性扩展到事务完整性

F366证明磁盘对象，F367证明solver字节，F368进一步证明跨pipe/socket消息不会跨请求错配。
这是三种不同层次的完整性：content identity、object lifetime和protocol transaction。把三者
分开建模，避免用“fd已密封”错误推导“请求一定正确配对”。

### 7.2 以generation fencing替代启发式排空

双通道故障后尝试读取一个fd消息、跳过一行或继续复用prefix cache都依赖无法证明的偏移。
整代失效采用fail-stop恢复语义，牺牲一次冷启动以恢复确定性边界。该思路与并行框架已有的
lease token、dispatch generation和renewal generation fencing一致，使控制面与求解数据面
使用统一的“未知代次不可复用”原则。

### 7.3 控制面资源有界化

selector deadline、64 KiB分块、16 MiB硬上限、严格单响应framing和进程组回收共同约束了
时间、内存、消息数和子进程生命周期。挑战在于不能破坏正常prefix cache复用、portfolio
取消语义或F367 sealed descriptor所有权；关联回归和真实C++ lit均保持通过。

## 8. 代价与适用边界

正常请求增加selector注册与分块读取，未增加helper restart。失败请求会丢弃整个Z3 prefix
cache，这是恢复正确性所需的明确代价。尚未测量高错误率下的冷启动成本，也没有实现跨进程
持久化prefix cache。

本轮证据是本地Linux机制证据，不包括：

- GitHub托管runner；
- 完整LLVM lit或vendored QSYM/PIN tracer；
- 真实MPI transport或跨主机文件系统；
- 公开solver benchmark、LAVA-M、覆盖campaign；
- 性能、覆盖率或漏洞发现提升。

因此不能把“完整门禁零退化”解释为吞吐或coverage uplift。当前等级仍为I/T/E-mechanism。

## 9. 实现与证据索引

- Python实现：`util/query_store.py`；
- 回归测试：`test/test_query_store.py`；
- 配置契约：`docs/Configuration.txt`；
- 示意图：[`persistent-solver-generation-reset-2026-08-11.svg`](../diagrams/persistent-solver-generation-reset-2026-08-11.svg)；
- 可执行证据：[`f368-persistent-solver-generation-reset-2026-08-11/`](../evidence/f368-persistent-solver-generation-reset-2026-08-11/)；
- 完整门禁：证据目录中的`full-gate.json`和`full-gate.log`；
- 规范身份：`test/pytest-nodeids.json`及`inventory-rebuild.log`。

## 10. 后续研究

1. 对大量并发persistent workers测量selector、restart和prefix-cache warmup成本；
2. 为helper generation增加显式单调编号和结构化transport telemetry；
3. 评估把descriptor与文本控制合并到单一`SOCK_SEQPACKET`消息的可行性，同时处理大witness、
   backpressure和跨语言framing；
4. 为CAS orphan建立引用可达性扫描与有预算GC；
5. 在固定CPU预算的公开benchmark上比较冷启动、持久cache、故障注入与coverage AUC。

## 11. 结论

F368关闭了F367之后仍存在的双通道半提交窗口。系统不再尝试在未知pipe/socket偏移上继续
使用Z3上下文，而是把process、stdio和descriptor channel作为一个generation统一fence。
反事实证明旧行为确实会跨请求消费错误artifact；production证据证明失败后新请求只在新
generation上接收自己的sealed descriptors，同时保持787个规范测试身份和16项能力零退化。
