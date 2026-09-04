# F303：结果自适应的精确经验域准入

## 1. 摘要

F300--F302已经完成经验值画像（Empirical Value Profiling, EVP）、SAT-only经验域
求解、完整公式回退，以及MPI滚动发布。F303解决下一个实际问题：**一个统计上稳定的
小值域不一定是一个有收益的求解域**。如果某个经验域反复让Z3返回UNSAT，原系统会
安全地回退完整公式，但每次仍先支付一次无收益查询。

F303为每个精确域建立结果反馈，按
`(executable_sha256, site, bits, sorted_domain_values)`聚合实际查询结果。达到最小查询数
且验证成功率低于阈值时，只撤销该精确域；失败样本离开滚动窗口后，域自动重新准入，
从而保留受控探索。抑制决定及其计数证据写入SHA-256封存的v2 artifact，验证器重查
计数守恒、阈值关系和画像成员关系。经验域仍只是可选加速探针；任何撤销、损坏或
策略判断都不改变F301完整公式求解链。

![F300-F304经验域闭环（本报告的后继F304已纳入当前总图）](diagrams/continuous-optimization-2026-08-04.svg)

## 2. 问题与设计目标

F302的准入依据是值域是否完整、未饱和、观察数充分且distinct数有界。这能回答
“运行时是否常见少量值”，却不能回答“把这些值作为Z3析取域是否经常产生可验证
模型”。两种典型低收益情况是：

1. 值域与当前反向分支冲突，APInt预过滤后为空；此时没有Z3开销；
2. 值域对局部谓词可行，但与完整相关prefix冲突，`F_prefix ∧ F_target ∧ D`反复UNSAT。

F303只针对第二类实际查询建立低收益门。设计约束如下：

- **不把便宜预过滤当作昂贵失败**：只有真实solver query进入收益分母；
- **不跨域外推**：`[1]`的失败不能抑制后来形成的`[1,2]`；
- **不永久封禁**：证据按F302滚动窗口老化，旧失败离窗后允许重新探索；
- **可审计**：抑制工件携带原始计数和阈值证明，不能只有一个不透明布尔位；
- **保持求解完备边界**：撤销仅去掉经验域探针，不去掉原prefix或完整公式回退；
- **资源有界**：每次执行最多保留512个实际尝试过的精确域反馈。

## 3. 执行流程

### 3.1 Worker运行时

当QSYM在路径site找到匹配的经验域时，执行顺序固定为：

1. 以APInt语义对`eq/ne/ult/ule/ugt/uge/slt/sle/sgt/sge`计算满足反向分支的域值；
2. 空域记一次`prefilter_rejects`，直接进入F301完整求解链，不调用Z3；
3. 非空域同步完整相关prefix，求解`F_prefix ∧ F_target ∧ D_admissible`；
4. SAT模型用expression evaluator复验完整prefix和目标；只有复验成功才保存输入；
5. solver UNSAT、UNKNOWN或验证失败均进入原完整公式求解链；
6. 进程退出时，按当前加载的精确域发出有界反馈行。

反馈行格式为：

```text
[site, bits, [domain_values...], attempts, prefilter_rejects,
 solver_queries, sat, validated, validation_failures, solver_unsat, unknown,
 solver_time_us]
```

每行必须满足三个守恒式：

```text
attempts       = prefilter_rejects + solver_queries
solver_queries = sat + solver_unsat + unknown
sat            = validated + validation_failures
```

运行时使用饱和`uint64_t`计数；Python规范化器要求字段为有界整数、值域互异、宽度
合法且守恒式成立。损坏行被丢弃，不能制造抑制证据。

> F304兼容说明：本报告下述v2/query-ratio门是F303发布时的协议。当前运行时在行尾
> 追加经验域Z3 `check()`累计微秒数，当前master使用v3/admission-v2成本门；旧11项行
> 仍可读并被规范化为零成本。新协议、默认值和实测见
> [`Cost_Aware_Empirical_Domain_Admission_2026-08-04.md`](Cost_Aware_Empirical_Domain_Admission_2026-08-04.md)。

### 3.2 Master聚合与决策

协调器先按F302规则从滚动记录重建v1画像，再将同一窗口内的精确域反馈与当前画像做
等值连接。默认门限为：

```text
q_min = 8
tau   = 125000 ppm = 12.5%

suppress(D) iff queries(D) >= q_min
                and validated(D) * 1,000,000 < queries(D) * tau_ppm
```

严格小于号意味着恰好达到阈值时保留域。`tau_ppm=0`时不可能满足不等式，因此等价于
关闭反馈抑制。聚合计数若超过`uint64_t`，该精确key采用fail-open，不作抑制。

### 3.3 v2证明工件与运行时侧车

`symcc-empirical-value-profile-v2`在v1画像上增加：

- `runtime_domain_count`：扣除抑制项后实际可物化域数；
- `online_admission.schema`：`symcc-empirical-domain-admission-v1`；
- `min_solver_queries`和`min_validated_ratio_ppm`；
- `suppressed[]`：精确key、八个计数和固定原因
  `low-validated-query-ratio-v1`。

验证器重新检查：工件摘要、v1画像可重建性、suppressed key确实属于当前可物化画像、
无重复key、三个守恒式、最小样本数、严格比例关系和runtime count。任一字段伪造都会
使整个artifact验证失败。

QSYM消费的文本侧车仍是`runtime-v1`。这是有意的职责分离：v2是master侧研究证据，
运行时只接收已经过滤的域集合，不需要实现在线策略。旧v1 artifact仍可验证；F302
协调器恢复旧代际或发现反馈阈值变化时，会标记dirty并在下一次分派前重新物化v2代际。

### 3.4 滚动重新探索

抑制证据不另建永久黑名单，而与画像共享滚动记录窗口。设窗口为`W`：

1. 低收益反馈达到门限，域在下一代侧车中消失；
2. worker不再对该精确域发出尝试，因此新记录不会继续累积该失败；
3. 当旧失败记录被`W`条后续记录替换，查询数低于`q_min`；
4. 域重新物化，获得一轮新的受控探测机会；
5. 新结果可以再次抑制，也可以因环境变化和验证成功而保留。

这是一种由证据自然衰减形成的探索机制，不需要不稳定的随机解禁计时器。窗口越小，
重新探索越频繁；窗口越大，抑制越稳定，需在正式消融中调参。

### 3.5 跨文件恢复事务

`current.runtime`、不可变artifact和`state.json`分别原子写入，但普通文件系统不能把
三个rename合成一次事务。F303恢复加固因此在checkpoint中保存：

- `records_complete`：容量裁剪是否丢失了构造当前代际所用的旧记录；
- `current_artifact_sha256`：该记录状态对应的精确artifact代际。

恢复时，记录被裁剪、旧状态缺少代际字段、runtime与state摘要不匹配、state缺失或
state声明的runtime缺失，都会设置`dirty`。协调器在下一次工作分派前从实际存活记录
重新聚合；记录为空时发布合法0-profile撤销代际。这样，崩溃最多丢失优化机会，不能
让无法由恢复证据证明的经验域继续静默生效。

## 4. 实现映射

| 层 | 文件 | F303职责 |
| --- | --- | --- |
| QSYM运行时 | `runtime/.../pintool/solver.{h,cpp}` | 有界精确域计数、守恒遥测、饱和计数 |
| artifact | `util/empirical_value_profile.py` | 严格规范化、v2策略、证明验证、过滤物化 |
| 在线协调器 | `util/online_value_profile.py` | 滚动聚合、恢复升级、抑制代际、累计统计 |
| 统一遥测 | `util/hybrid_feedback.py` | 解析精确反馈并暴露`empirical_domain_solver`能力 |
| MPI编排 | `util/mpi_fuzzing_helper.py` | 阈值配置和实验solver component控制 |
| 真实夹具 | `test/empirical_value_profile_feedback.c` | 稳定`[1,2]`域与真实UNSAT反馈 |
| 实验驱动 | `benchmark/run_evp_admission_smoke.py` | 准入、抑制、无查询、重准入、重查询全链复现 |

## 5. 配置与可重复性

| 配置 | 默认值 | 语义 |
| --- | ---: | --- |
| `SYMCC_VALUE_PROFILE_FEEDBACK_MIN_QUERIES` | 8 | 抑制前所需实际查询数，最小1 |
| `SYMCC_VALUE_PROFILE_FEEDBACK_MIN_VALIDATED_PPM` | 125000 | 验证成功率阈值，范围0--1000000；0关闭抑制 |
| `SYMCC_SOLVER_COMPONENT` | `learned` | 关闭component adaptation时选`exact/learned/diverse`；开启自适应时由portfolio接管；显式S2F `sample`任务仍可选择sampler |

真实机制实验命令：

```bash
python3 benchmark/run_evp_admission_smoke.py \
  --compiler build/symcc \
  --output /tmp/symcc-f303-smoke
```

脚本要求输出目录为空，保存编译器版本、每次原始telemetry、三个runtime侧车、v2
artifact、协调器checkpoint、摘要和SHA-256清单。正式campaign不应把smoke使用的
`q_min=2`作为默认参数；它只用于在两个真实查询内验证状态转换。

## 6. 测试与实测结果

### 6.1 自动化测试

- `test_empirical_value_profile.py`：v2精确域抑制、摘要重算后伪造原因拒绝、不可哈希
  domain拒绝、非映射公共校验入口fail closed；
- `test_online_value_profile.py`：两次失败后抑制、窗口淘汰后重新准入、代际和累计统计；
- `test_hybrid_feedback.py`：反馈行解析、去重、守恒和prefilter字段；
- `test_afl_profile_orchestration.py`：solver component固定控制及非法值回退；
- LLVM 17和LLVM 18的`empirical_value_profile`过滤组均为5/5，真实QSYM夹具确认至少
  两次solver query、至少两次solver UNSAT、零validated且三个守恒式成立。
- 顺序全量门禁为Python `508/508`（82.829秒，shell real 83.351秒）、LLVM 18
  `220 passed + 1 unsupported`（134.27秒）和LLVM 17
  `219 passed + 2 unsupported`（133.18秒）。
- 在线协调器9项测试还覆盖截断checkpoint、旧v1状态、state/runtime代际错配和无state
  runtime撤销；断言最终profile数和artifact SHA，而不只检查内存标志。

### 6.2 真实状态转换证据

2026-08-04 smoke结果见
[`evidence/f303-adaptive-evp-2026-08-04/`](evidence/f303-adaptive-evp-2026-08-04/)：

| 阶段 | runtime域 | 精确`[1,2]`反馈 | 观察 |
| --- | ---: | ---: | --- |
| 初始准入 | 2 | 尚无 | 两个画像域均物化 |
| 真实探测 | 2 | 2 query / 2 UNSAT / 0 validated | 满足低收益证明 |
| 抑制代际 | 1 | suppressed=1 | 只撤销精确`[1,2]` |
| 抑制执行 | 1 | 0行 | 被撤销域未再查询 |
| 窗口老化 | 2 | 旧证据离窗 | suppressed恢复为0 |
| 重新探测 | 2 | 1 query | 重新探索实际发生 |

协调器共观察6条用于状态更新的记录，发布3个语义代际；累计加载6个runtime profile、
2次初始经验域查询、2次完整回退，parse/context/validation/checkpoint/publication错误
均为0。最后一次reprobe用于证明重新查询，未回写协调器累计计数。

## 7. 学术定位与创新点

[KLEE](https://www.usenix.org/legacy/event/osdi08/tech/full_papers/cadar/cadar_html/paper.html)
强调查询优化和避免无效solver调用；
[SymCC](https://www.usenix.org/conference/usenixsecurity20/presentation/poeplau)把符号执行
编译进目标程序以降低解释开销；
[SymFit](https://www.usenix.org/conference/usenixsecurity24/presentation/qi)进一步优化常见
concrete执行路径；
[Cottontail](https://arxiv.org/abs/2504.17542)展示了使用运行历史和上下文响应式选择
符号执行工作的方向。F303与这些工作的共同目标是把昂贵求解预算集中到更可能产生
进展的查询。

本项目没有把F303表述为上述论文的直接复现，也没有宣称其性能达到SOTA。当前具有
研究价值的系统组合是：

1. 以**完整精确域身份**而非仅site累计结果，避免画像演化后的错误迁移；
2. 将**决策证据和策略阈值封入可验证artifact**，使分布式worker看到的域集合可追溯；
3. 用**滚动证据衰减**统一实现抑制和重新探索，而不是永久denylist；
4. 把**性能策略与正确性链解耦**：策略最多关闭可选探针，不能关闭完整公式回退。

这些是针对SymCC-Parallel现有架构的工程与研究设计。是否带来统计显著的solver-time、
coverage AUC或漏洞发现提升，仍必须通过正式benchmark回答。

## 8. 已知边界与下一步实验

- 当前门使用validated/query单指标；不同site的solver耗时和下游coverage收益尚未进入
  多目标决策；
- 512行反馈上限之外只有全局计数，未记录的域采用fail-open；
- 相同精确域在不同路径上下文中的收益可能不同，当前身份未包含prefix fingerprint；
- 小窗口会产生抑制/重准入振荡，大窗口会延迟适应，需要按target消融；
- smoke证明机制执行，不证明吞吐或覆盖提升；F302短MPI证据也不能替代多轮实验。

建议正式实验固定二进制、种子、CPU-hour和随机种子，对比F302（反馈门关闭）与F303
默认门，至少报告：经验域query数及时间、validated/query、完整回退数、生成输入数、
accepted/CPU-hour、edge/data coverage AUC、time-to-target和worker重复率。至少20轮独立
运行后使用paired bootstrap、randomization test、效应量与多重比较校正，才能把当前
I/T/E-mechanism等级提升为benchmark或research证据。

## 9. 后继实现：F304

F304已经完成本报告第8节“solver耗时进入决策”的第一步：抑制除查询数和低验证率外，
还必须达到精确域累计求解成本下限。默认`1000 us`，设置为0时保留本报告的F303语义。
F304不把机制smoke的微秒数解释为性能收益，正式公开target消融仍未完成。
