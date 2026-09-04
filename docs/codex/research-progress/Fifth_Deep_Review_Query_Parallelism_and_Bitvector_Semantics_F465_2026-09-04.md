# F465：第五轮深度审查与求解并行度、位向量语义闭合

- 日期：2026-09-04
- 范围：QueryStore领取/续租、Query IR候选复验、饱和算术运行时、AFL data coverage构建和测试身份
- 证据级别：I/T/E-mechanism

## 1. 审查方法与结论

本轮没有按模块表面接口重复检查，而是为状态机和算术语义构造最小反例：让大量查询共享同一
prefix，观察多solver领取能力；把求解租期压缩到120 ms并在结果验证阶段制造等待；令四种QF_BV
除法/余数的除数取零；令LLVM饱和加法使用64位最大值。审查确认四个生产问题和一个构建遗漏，
另一个最初怀疑的MPI worker恢复问题经当前`HEAD`逐路径核对后被排除：非watchdog恢复已经把
rank重新加入`idle_ranks`，因此没有为未成立的问题制造重复补丁。

## 2. QueryStore：从静态分片到本地优先窃取

### 2.1 原问题

旧领取条件固定为：

```text
eligible(query) AND prefix_id mod jobs = worker_slot
```

这个策略能提高persistent solver的prefix context命中率，但prefix是查询族属性，不是均匀任务
标识。空前缀或共同入口前缀会让大量query具有同一`prefix_id`。四个solver面对这一队列时，只有
一个slot可以领取，另外三个即使空闲也会持续得到`None`。增加`--jobs`因此可能只增加线程，不能
增加同时求解数。

### 2.2 修复后的两阶段领取

```text
BEGIN IMMEDIATE
  1. 按原traversal/shape/priority顺序领取本slot的prefix shard
  2. 若本地为空且jobs > 1，按完全相同顺序领取任意eligible query
  3. 原子更新owner、deadline、递增token和attempts
COMMIT
```

第一阶段保留缓存局部性；第二阶段只在局部空闲时牺牲局部性换取solver利用率。两次SELECT和最终
UPDATE位于同一SQLite写事务，其他worker不能在中间领取同一行。跨分片不是可信依据：最终提交仍
必须同时满足query ID、owner、token和未过期deadline，并通过独立Query IR复验。

测试向一个全新store写入4个具有相同prefix、不同target的query，然后让4个不同slot依次领取。
修复后4个slot得到4个不同lease；旧谓词最多只有`prefix_id mod 4`对应的一个slot能够领取该族。
这是调度容量反例，不是端到端求解加速比。

## 3. 短租约续租的锁等待竞态

原实现进入`renew()`时先计算`lease_until = now + duration`，之后才等待SQLite
`BEGIN IMMEDIATE`。在写锁或同步落盘较慢时，事务成功提交的“新”deadline可能已经只剩很短时间，
下一次心跳会在到达锁前过期。120 ms压力用例实际出现了有效solver仍在结果验证、lease却被另一
worker以新token重新领取，原结果只能返回`stale`。

自动续租现在先取得写锁，再读取时钟并计算deadline；显式传入的`now`不重写，以保留确定性协议
测试。续租没有扩大权限：若owner/token不匹配或旧deadline在锁后时刻已经过期，UPDATE仍失败；
`complete()`仍在提交事务中执行最终fence检查。原压力测试恢复为稳定SAT提交。

## 4. QF_BV除零语义

SMT-LIB位向量除法是全函数，除数为零并不产生“未定义/无法复验”。对宽度`w`的被除数`x`，本地
复验器现在执行：

| 运算 | 零除数结果 |
| --- | --- |
| `bvudiv x 0` | `2^w - 1` |
| `bvurem x 0` | `x` |
| `bvsdiv x 0` | `x`为负时`1`，否则`2^w - 1` |
| `bvsrem x 0` | `x` |

旧实现对四者统一返回`None`，会拒绝solver按照标准语义生成的合法model，造成假阴性。新测试覆盖
8位`00/01/7f/80/ff`五个符号边界，共20个子用例。修复与已有bit-blast backend的总函数语义一致；
候选仍须满足完整prefix和target，不能仅凭solver状态入库。既有多后端operator matrix是冻结证据
的一部分，本轮不重签该历史artifact；新增边界由QueryStore独立复验器的定向回归承担。

## 5. 饱和算术与data coverage构建

`buildMaxSignedInt()`和`buildMaxUnsignedInt()`原来通过`(1ULL << bits) - 1`构造mask；`bits=64`
时左移量等于类型宽度，属于C++未定义行为。修复后1--64位对64单独使用`UINT64_MAX`，更宽且ABI
可表示的位宽通过`not(0)`及逻辑移位构造，不依赖宿主整数移位。LLVM18/QSYM新增i64
`llvm.uadd.sat`真实插桩执行用例并通过。

data coverage interposer已经使用`pthread_once`，其专项测试构建也使用`-pthread`，但benchmark
临时构建路径只链接`-ldl`。该路径现显式传入`-pthread`，避免在旧glibc或不同链接器上依赖隐式
pthread符号合并；单元测试检查实际传给编译器的argv。

## 6. 验证记录

| 层级 | 结果 | 说明 |
| --- | --- | --- |
| 最小反例 | 3 passed + 22 subtests | shape开/关的热prefix、四类除零、pthread argv |
| QueryStore完整模块 | 41 passed + 50 subtests | 含短租约、持久solver、artifact与outbox |
| AFL编排完整模块 | 77 passed + 42 subtests | data-map构建与hybrid编排 |
| LLVM 18/QSYM | 349 passed + 1 unsupported | 新增i64饱和算术后共350项 |
| Python规范身份 | 1671/1671 collect-only | SHA-256 `5065f614a8ac299bda2dc83d9a2c895783d640a1842687a1837c376fca897eb7` |
| 完整Python | 1671 passed + 638 subtests，330.19秒 | 零skip/xfail/xpass/deselect/missing/unexpected |
| LLVM 17/QSYM | 348 passed + 2 unsupported | 347项主套件加1项隔离provenance |

规范pytest清单此前停留在1597项，而当前代码实际收集1671项。本轮净差74项中只有2项是F465新增，
其余来自上一批实现未同步和1项测试重命名；清单按真实collect-only结果重建，不能把74项都记为
本轮工作量。

## 7. 证据边界与后续优化

这些测试证明了标准语义、租约fence和热点prefix下的领取容量，尚未证明公开目标上的coverage、
solver speedup或缺陷发现率提升。后续性能实验应增加`local_claim/steal_claim/prefix_cache_hit`三项
遥测，用相同CPU预算比较严格亲和与本地优先窃取，并按prefix偏斜度分层报告。若窃取率很高且
cache miss抵消并行收益，可进一步采用带滞回的steal threshold或按prefix backlog分裂solver上下文；
在没有实测前，不把机制容量的4/4结果写成4倍加速。

机器可读Python门禁结果保存在
[`full-python-gate.json`](../evidence/f465-query-parallelism-bitvector-semantics-2026-09-04/full-python-gate.json)。
