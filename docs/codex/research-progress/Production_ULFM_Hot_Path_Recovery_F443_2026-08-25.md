# F443：生产热路径的 generation-fenced ULFM 恢复

**日期**：2026-08-25  
**状态**：opt-in 生产链路已接通；本机真实 worker/master 失效门禁通过；尚无多节点 R 级结论  
**前置**：F441 恢复协议、共享工作 WAL、QueryStore  
**协议**：`symcc-generation-fenced-ulfm-recovery-v1`

## 1. 研究问题与结论

F441 已证明稳定 endpoint、generation/shard/lease 栅栏和 ULFM
`Revoke/Shrink/Agree` 的机制正确性，但当时没有进入
`mpi_concolic_execution.py` 的真实 master/worker 循环。F443 完成该生产接线：物理 worker
或原 master 退出后，survivor 修复 communicator、持久推进 generation、重放所有不确定工作，
并由修复后的 rank 0 继续同一 campaign。

当前结论是：**单 master、启动时至少两个 worker 的 opt-in campaign 已具备缩容继续执行能力**。
默认路径完全不启用 ULFM；不合格的 MPI 实现会在启动阶段失败关闭。该结果不等价于多节点性能
提升，也不包含 spare process 补员或 multi-master 修复。

## 2. 当前架构

```text
                         stable endpoint inventory
                                   |
                                   v
  input/corpus --> shared work WAL --> master scheduler --> fenced work envelope
                       |                    |                       |
                       |                    v                       v
                       |             durable QueryStore       worker process
                       |          dispatch/finish/recovery          |
                       |                    ^                       v
                       +<-- commit manifest + publication <-- staged result

  process failure
       |
       v
  Ack_failed/Get_failed -> Revoke -> durable prepare(g) -> Shrink
       -> endpoint attestation -> Agree -> durable commit(g+1)
       -> repaired rank 0 -> redo committing + reclaim leased -> resume
```

系统保留两个职责不同的持久层：

| 持久层 | 权威内容 | 故障后的职责 |
| --- | --- | --- |
| shared work WAL | 输入 work identity、fenced token、commit manifest、发布状态 | 重做不可逆发布，零 TTL 回收未提交任务 |
| QueryStore ULFM state | generation、成员、shard、活动 lease、recovery queue、receipt | 拒绝旧代结果，证明成员与任务守恒 |

二者不能互相替代。WAL 决定语料发布是否可重做；ULFM state 决定来自哪个 communicator generation
的结果仍有提交资格。

## 3. 无故障执行次序

1. 启动时每个 rank 在 `COMM_SELF` 上实际调用 `ERRORS_RETURN/Get_failed/Agree/Revoke/Shrink`，
   并 allgather 完整 recovery policy SHA；任一能力不通过或 policy 不一致即 `Abort(74)`，不静默降级。
2. master 创建 generation 0 controller，并将初始快照以 `state_ordinal=0` 写入 QueryStore。
3. shared WAL 为输入生成 token；QueryStore 再为同一 work 生成 generation/shard/lease fence，先持久化
   dispatch，后发送 envelope。
4. worker 严格验证 envelope，只读取其中唯一的 `input_hash`，执行目标后把结果写入私有 staging；
   `TAG_RESULT` 原样回显 fence。
5. master 同时核对 transport worker、活动 assignment、work hash 与完整 fence。旧代结果只计为 stale，
   malformed/unowned 结果隔离 worker 并失败关闭。
6. 结果验证后，master 先把 commit manifest 写入 shared WAL，再持久完成 ULFM lease，然后发布 staged
   objects，最后把 WAL 记录置为 done。

第 6 步的顺序是本次重要修正。commit manifest 一旦持久化，后续发布可确定性重做；因此先完成
ULFM lease 不会丢失结果。反过来，若先完成 WAL、后完成 ULFM lease，master 在两次写入之间退出会留下
“WAL 已完成但 ULFM 仍要求重放”的幽灵任务。

## 4. 失效恢复次序

1. 数据面 MPI 调用返回 `ERR_PROC_FAILED(_PENDING)/ERR_REVOKED`，或 master 的低频
   `Ack_failed/Get_failed` 轮询发现失效。后者是必要的，因为 `Iprobe(ANY_SOURCE)` 不保证主动报告一个
   没有待收消息的退出 worker。
2. 首个观察者 `Revoke` 旧 communicator，唤醒仍阻塞在 `recv/send` 的 survivor。
3. 每个 survivor 从同一 QueryStore 恢复 controller。第一个进程写入 recovery plan；其余进程对相同
   ordinal/摘要作幂等确认。若同一 ordinal 出现不同摘要，QueryStore 拒绝 fork。
4. plan 冻结旧成员和**全部**在途 lease。保守重放包含健康 worker 的不确定工作，以漏执行风险换取
   可由 work identity 去重的有限重复计算。
5. `Shrink` 删除失败成员；survivor 在新 communicator 上交换稳定 endpoint、incarnation、host、
   old/new rank attestation，并执行 `Agree(True)`。
6. receipt 将 generation 推进到 `g+1`，失效 owner 的 shard 通过 rendezvous hashing 重分配；所有旧
   generation token 立即失效。
7. 新 communicator 的 rank 0 成为 master。它重新打开 WAL，先重做 `committing`，再以零 TTL 回收
   `leased` 记录；recovery queue 中相同 `work_id` 的任务优先领取原 shard 并生成新 lease。
8. `--wall-timeout` 使用恢复循环外的 campaign 起点，不因 generation 切换而重新计时。

## 5. 原子性与失败边界

| 失效位置 | 持久状态 | 恢复行为 |
| --- | --- | --- |
| dispatch 持久化前 | 无 ULFM lease | WAL 输入仍可重新领取 |
| dispatch 持久化后、发送前 | 活动 ULFM lease | recovery plan 将其保守重排 |
| worker 执行或回传中 | staging 可能存在 | 旧 fence 失效，WAL 与 recovery queue 重新派发 |
| WAL begin-commit 后 | commit manifest 可重放 | 先完成/恢复 ULFM，再确定性发布 |
| 发布后、WAL done 前 | manifest 仍为 committing | 重放 hard-link/rename 幂等完成 |
| recovery prepare 后 | pending plan 已持久 | survivor 复用同一 plan，不再次 prepare |
| recovery commit 写入结果不明 | 可能已提交 | 读取 ordinal 与 snapshot digest 调和，不盲目回滚 |

QueryStore 为每次 dispatch、finish、cancel、prepare、commit 分配连续 `state_ordinal`。同 ordinal 同摘要
是幂等重试；同 ordinal 不同摘要是状态分叉并被拒绝；generation 只能在精确 receipt 存在时加一。

## 6. 实现与审查中修复的问题

1. **缺少主动 failure detector**：真实测试中 master 的 `Iprobe` 未发现退出 worker。加入有界频率的
   `Ack_failed/Get_failed` 后，master 可及时 revoke。
2. **并发 prepare 竞态**：较慢 survivor 可能读到另一个 survivor 已写入的 pending plan。现在直接复用
   并校验该 plan，而不是重复 prepare。
3. **双持久层幽灵 lease**：调整 ULFM finish 与 WAL commit 的顺序，以可重放 manifest 作为跨层决定点。
4. **错误的 receipt 连接方式**：receipt 的 `post_state_sha256` 绑定恢复瞬间，不能与继续执行后的最终快照
   直接相等。oracle 改为连接 generation、membership、generation token 与 shard assignment。
5. **恢复后 wall timeout 重置**：campaign 起点提升至 generation 循环外，保持总时限语义。
6. **恢复任务被 preferred shard 阻塞**：dispatch 先按 `work_id` 查找 recovery queue，领取其原 shard；
   普通新任务仍遵守 preferred shard 的优先恢复约束。

## 7. 测试与实测结果

固定运行时为 Open MPI 5.0.10 ULFM，启动参数包含 `--with-ft ulfm`、`pml ob1` 和
`btl tcp,self`。端到端脚本对每个公开对象复算文件名 SHA-256，对最终 snapshot/receipt 独立验证，
并检查 completed epoch 已原子退休。

| 场景 | 初始拓扑 | 物理失效 | recovery | 重排 | 最终完成 | 公共对象 | 最终状态 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |
| worker failure | 1 master + 2 workers | `rank-1` | 1 次，generation 1 | 2 | 32 | 162 | queue=0，active=0 |
| master failure | 1 master + 3 workers | `rank-0` | 1 次，generation 1 | 1 | 32 | 161 | queue=0，active=0 |

两场 launcher 均返回预期状态 86，因为被注入失效的进程真实退出；survivor 均打印最终统计并完成
epoch retirement。两份快照的 `state_ordinal` 分别为 68 和 67，均只有一个 recovery receipt。
`SHA256SUMS.txt` 共校验 999 个证据文件，零不一致。修正 wall-time 跨 generation 重置后，两场
均严格共享同一 5 秒 campaign 预算，因此该轮对象数低于修正前的诊断运行。

选择性回归为 **20 passed、58 deselected、7 subtests passed**，覆盖协议、QueryStore、恢复映射、旧
lease 拒绝、one-shot 注入和 standalone 生命周期。最终完整 Python 回归为 **1,353 passed、310
subtests passed**；4 条 warning 均为既有 multiprocessing 在多线程进程中使用 `fork()` 的弃用提示，
没有 skip 或失败。上述 5 秒 simulation 用于机制验证，不应用于声明吞吐、coverage 或 defect-yield
提升。

## 8. 使用与复验

```bash
bash benchmark/install_openmpi_ulfm_5_0_10.sh
F443_OUTPUT_DIR=/tmp/f443 \
  benchmark/run_f443_ulfm_hot_path_tests.sh
```

生产模式必须显式设置 `SYMCC_ULFM_HOT_PATH=1` 并使用固定 ULFM `mpirun --with-ft ulfm`。
`SYMCC_ULFM_TEST_FAIL_INITIAL_RANK` 只属于测试门禁，生产环境不得设置。

## 9. 证据与边界

- 实现：`util/mpi_concolic_execution.py`、`util/mpi_ulfm_recovery.py`、`util/query_store.py`
- 单元/协议测试：`test/test_mpi_lifecycle.py`、`test/test_mpi_ulfm_recovery.py`
- 端到端 oracle：`benchmark/check_f443_ulfm_hot_path_oracles.py`
- 一键物理失效门禁：`benchmark/run_f443_ulfm_hot_path_tests.sh`
- 当前证据：`.symcc-research-evidence/f443-ulfm-hot-path-20260825-current-r3/`

当前支持 shrink 后由 survivor 继续执行，不支持补员；只支持单 master communicator，multi-master
故障协调仍失败关闭；已验证同主机 TCP transport，尚未给出跨节点 MTTR 分布、连续多故障压力、
8/32/128 worker 吞吐或公开 benchmark coverage 结论。这些是后续 R 级实验缺口，不应被当前机制结果
替代。
