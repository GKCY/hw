# B1_CASE_013 Timing ECO SFT 构造尝试报告（未成题）

## 1. Case 定位

| 字段 | 内容 |
|---|---|
| 类型 / 难度 | Setup / E |
| 形态 / 策略 | single_cone / A |
| relaxed-v3 候选层级 | exp |
| 注入 / 修复策略 | endpoint-local RVT downsize / exact inverse restore |
| ECO 修改预算 | relaxed-v3 严格 1 个组合逻辑 RVT cell |
| 目标窗口 | `[-0.115, -0.085] ns`，即 `-100 +/-15 ps` |
| 当前状态 | `PLANNED`；未找到满足全部门禁的候选 |
| 证据来源 | `work/batch100-relaxed-v3-r2` |

本文件是失败搜索记录，不是可训练 SFT case。当前没有 violating checkpoint、instruction、Gold fix、双 replay 或 `VALIDATED` 结论。

## 2. Design 与构造合同

- Top：`NV_NVDLA_CMAC_CORE_mac`；工具：Innovus 21.10-p004_1。
- Setup/Hold view：`functional_setup_ss` / `functional_hold_ff`。
- 合格基线 setup WNS/TNS=`+0.051/+0.000 ns`；hold WNS/TNS=`+0.050/+0.000 ns`。
- 基线 entry SHA256：`d554d549384bd48b65f270524d82a375f0bee0039db3c0be80a32ab46df19458`。
- 必须只有一个负 endpoint，且路径满足 exp hierarchy group。
- 必须保持实例集合、pin-net 拓扑、约束、时钟和路由不变；修复后还需通过双 VM replay、物理 no-regression 和严格 validator。

### 2.1 本报告采用的术语

| 术语 | 含义 |
|---|---|
| 合法候选全集 | 满足层级、endpoint-local、逻辑功能等价、RVT 类型、驱动强度和未占用约束的全部单元替换方案 |
| 快速时序估算 | 不进行完整布局合法化和全局时序枚举的候选排序结果，只能用于安排测量顺序 |
| 批量物理筛选 | 在 Innovus 中对一组候选逐一执行 `ecoChangeCell`、`refinePlace` 和 post-route timing，用于初步筛选 |
| 单候选独立全局验证 | 在新启动、重新载入基线的 Innovus 进程中只测试一个候选，并枚举全局负裕量 endpoint；这是进入正式校准前的必要证据 |
| 同基线去重键 | 由 baseline、endpoint、instance、baseline ref 和 new ref 共同定义，用于判断某个替换方案是否已经测量 |

## 3. 自动规划候选及拒绝结果

规划 binding 为：

| endpoint | 实例 | 基线 cell | 规划注入 |
|---|---|---|---|
| `u_exp/exp_sft_14_reg_3_/D` | `u_exp/U4624` | `AOI22_X1M_A9TR40` | `AOI22_X0P7M_A9TR40` |

已有同基线证据显示 `X0P7M` 的快速时序估算值为 `+0.0544696 ns`；更弱的 `X0P5M` 在布局合法化后的 post-route 时序结果中仍为 `+0.0131693 ns`，没有形成 setup violation。因此该规划候选未进入状态机的正式 `BOUND`，状态文件中的 binding/injection/repair 哈希仍为空。

## 4. 尝试时间线

时间均为 Asia/Shanghai；不同候选集合之间允许重叠，不能直接相加为独立单元替换方案数。

| 时间 | 阶段 | 尝试与实测证据 | 判定 / 下一步 |
|---|---|---|---|
| 2026-08-03 15:05 | 初始化 | state 创建为 `PLANNED`；规划 `u_exp/U4624 AOI22_X1M -> X0P7M` | 仅规划，不是有效 binding |
| 2026-08-03 16:36 | 复用既有物理证据 | 同实例更弱 `X0P5M=+0.0131693 ns`；`X0P7M` 的快速时序估算值为 `+0.0544696 ns` | 无违例，放弃初始候选 |
| 2026-08-04 10:41–10:47 | 建立 exp 合法候选全集 | 枚举 2144 个合法单元替换方案；形成 250 个快速筛选候选、648 个物理结果预测排序候选和另一组 250 个优先候选 | 只用于覆盖与运行顺序安排 |
| 2026-08-04 13:52–13:53 | 插值档位物理筛选 | `FE_OFC7405 X0P8B/M=-0.0815992/-0.0796928 ns`；`u_exp/U380 X0P7M=-0.157706 ns` | 一个方向偏浅、一个方向偏深，无合法中间档 |
| 2026-08-04 15:45–17:43 | 相似单元校正 / 中位残差 / 未测实例 / K 近邻预测 | 依次生成 40、32、24、24、247 个排序候选；完成 12 个相似单元候选和 96 个宽范围 exp 候选的物理筛选 | 最近的批量物理结果为 `u_exp/U3095=-0.0823126 ns`，比上界少负 `2.6874 ps`，严格越界 |
| 2026-08-04 17:42 | 单候选独立全局验证 | `u_exp/U3765 MXIT2_X1P4M -> X0P5M=+0.0805006 ns`，负裕量端点数量为 0 | 独立验证证明无违例 |
| 2026-08-04 18:48–20:01 | 未测 XOR/XNOR 候选穷举 | 32 个候选、8 个分片全部完成；代表负值 `u_exp/U2548=-0.1815 ns`、`u_exp/U367=-0.789675 ns` | 32/32 无窗口命中 |
| 2026-08-04 19:14 | 中间驱动强度档位测试 | 6 个单元替换方案，结果从 `-0.330928` 到 `+0.165729 ns`；`u_exp/U434 X0P7M=-0.164642 ns` | 标准驱动强度档位仍跨过目标窗口 |
| 2026-08-04 19:13–19:35 | 未测 BUF 候选筛选 | 6 个候选全部完成，结果 `+0.0208311`～`+0.496897 ns` | 6/6 均为正时序裕量 |
| 2026-08-04 19:41–20:30 | 未测 MXIT2 候选筛选 | 生成 23 个候选；首分片 6 行、第二分片 2 行完成，8 个结果均为正时序裕量 | 8/23 已测；剩余 15 个未测 |
| 2026-08-04 20:30 后 | 用户停止 | 精确终止在跑任务，确认目标进程消失；同步中断表格与日志 | 保留断点，不再派发 |

## 5. 逐轮详细复盘

### 5.1 第 0 轮：检查自动规划候选是否值得正式校准

**目的**

先验证 deterministic binding 给出的 `u_exp/U4624` 是否能形成目标违例。如果该候选有效，就可以直接进入正式 `BOUND -> CALIBRATING`，避免重新搜索整个 exp hierarchy。

**动作**

1. 从 binding 读取目标 endpoint、实例、基线 ref 和合法 downsize ladder。
2. 在同一合格基线的历史筛选结果索引中查找完全相同的单元替换方案。
3. 对比规划档 `AOI22_X0P7M_A9TR40` 的快速时序估算值，以及更弱 `AOI22_X0P5M_A9TR40` 的布局合法化后时序结果。

**结果**

- `X0P7M` 快速时序估算值=`+0.0544696 ns`；
- 更弱的 `X0P5M` 经 `refinePlace -eco true` 和 post-route timing 后仍为 `+0.0131693 ns`；
- 负裕量端点数量为 0，没有产生 setup violation。

**决策**

最弱合法档仍不违例，继续校准该实例没有意义。规划候选绑定不提升为正式绑定，case 状态保持 `PLANNED`，搜索转向整个 exp 合法候选全集。

### 5.2 第 1 轮：建立 exp 合法候选全集并排除重复测量

**目的**

把问题从“猜一个热点”改成“测量尚未覆盖的合法单元替换方案”。同时建立同基线候选去重索引，排除已经获得物理结果的方案，避免重复运行昂贵的 Innovus 任务。

**动作**

1. 生成 `case011015_universe_exp_v300`，枚举 exp 路径上的合法同功能 RVT 驱动强度替换，共 2144 个方案。
2. 用 endpoint、instance、baseline ref、new ref 组成同基线去重键。
3. 生成 `case011015_triage_exp_v301`：250 个优先进行物理筛选的候选。
4. 根据快速时序估算值与已有物理结果之间的偏差，建立 `case011015_modeled_exp_v302`：648 个带物理裕量预测值的排序候选。
5. 从预测排序表中生成另一组 250 个优先候选 `case011015_triage_modeled_exp_v303`。

**结果**

- 获得了可恢复、可分片的 exp 搜索空间；
- 各候选集合之间存在重叠，不能把 2144、250、648、250 直接相加；
- 模型误差与 30 ps 目标窗口同量级，因此模型只能调整运行顺序，不能排除候选。

**决策**

先测试最有机会落入目标区间的中间驱动强度档位和相似单元候选；任何批量筛选得到的近窗结果，仍必须再做单候选独立全局验证。

### 5.3 第 2 轮：测试中间驱动强度和同功能库单元变体

**目的**

已有结果显示部分弱驱动档造成的违例过深，而较强驱动档又不产生足够违例。本轮专门测试标准驱动强度序列中尚未测量的中间档，以及 B/M 同功能库单元变体，希望得到位于 `-0.115` 与 `-0.085 ns` 之间的结果。

**动作**

在 `c1115pt_v315_exp_interp` 中以批量物理筛选模式依次执行 4 个单元替换方案。每个方案都从合格基线恢复设计，执行 `ecoChangeCell`、`refinePlace -eco true`、`timeDesign -postRoute`，记录目标时序裕量后恢复原单元。

| 单元替换方案 | 布局合法化后的 WNS |
|---|---:|
| `FE_OFC7405 BUF_X2M -> X0P8B` | `-0.0815992 ns` |
| `FE_OFC7405 BUF_X2M -> X0P8M` | `-0.0796928 ns` |
| `u_exp/U418 XOR2_X3M -> X1P4M` | `+0.0622563 ns` |
| `u_exp/U380 XOR2_X2M -> X0P7M` | `-0.157706 ns` |

其中 `X0P8B` 距上界只有 `3.4008 ps`，因此又在 `c1115fg_v316_buf08b_00000` 中启动单候选独立全局时序验证，检查全局负裕量 endpoint 集合。

**结果**

- 单候选独立验证稳定复现 `FE_OFC7405 X0P8B=-0.0815992 ns`，且只有一个负裕量 endpoint；
- 但该值仍比严格上界少负 `3.4008 ps`；
- `X0P8M` 更浅，`U380 X0P7M` 又过深 `42.706 ps`。

**决策**

这是一个物理实现有效、但违例幅度不合格的近界候选。不得放宽容差；下一轮转向相似单元偏差校正，希望在结构相近的其他实例上得到稍深的违例。

### 5.4 第 3 轮：相似单元偏差校正

**目的**

快速时序估算值在 `refinePlace` 后经常改变符号或出现较大偏差。本轮不直接采用快速估算值，而是利用同单元类型、相近驱动强度比和相同层级中已测候选的“物理结果减快速估算值”偏差，对尚未测量的实例进行校正排序。

**动作**

1. 生成 `case013_analog_exp_v329`，共 40 个候选，分为 10 个分片。
2. 每个候选记录相似样本数、估算偏差、参考 endpoint/instance 和预测的物理时序裕量。
3. 选择前三个分片，在 `c1215pt_v333_expa0`、`c1215pt_v334_expa1`、`c1215pt_v345_expa2` 中完成 12 个候选的批量物理筛选。

**结果**

| 分片 | 行数 | 负 slack 数 | 最小 / 最大 WNS | 关键结果 |
|---|---:|---:|---:|---|
| `v333_expa0` | 4 | 0 | `+0.0732479 / +0.200241 ns` | 预测最靠前的 `u_exp/U264` 实测变为 `+0.200241 ns` |
| `v334_expa1` | 4 | 2 | `-0.709878 / +0.152500 ns` | `u_exp/U3095=-0.0823126 ns`，距上界 `2.6874 ps`；`u_exp/U418=-0.709878 ns` 过深 |
| `v345_expa2` | 4 | 0 | `+0.0334339 / +0.171870 ns` | 全部仍为正 slack |

**决策**

`U3095` 是批量物理结果中最接近目标区间的未占用替换方案，但它严格越界，而且仅来自同一进程内的多候选筛选，不能作为最终依据。相似单元偏差校正对个别实例仍有较大误差，不能据此缩小合法候选全集；下一轮改用中位偏差、未测实例约束和跨 run 的 K 近邻预测共同排序。

### 5.5 第 4 轮：降低相似样本离群值影响并扩大未测实例覆盖

**目的**

上一轮表明，单个相似样本可能使预测产生明显偏差。本轮分别采用中位偏差、零偏差快速估算、排除已有物理结果的实例，以及跨 run 的 K 近邻预测。目标是提高实例覆盖度，而不是继续集中在同一热点。

**动作**

1. `case013_analog_median_v354`：32 个候选，使用中位估算偏差降低离群值影响。
2. `case013_analog_freshinst_v357`：24 个候选，排除已有物理结果的实例。
3. `case013_rawmeasured_freshinst_v360`：24 个候选，采用零估算偏差，只按快速时序估算值排序。
4. `case013_knn_crossrun_v363`：247 个候选，合并相同合格基线历史 run 的物理近邻，进行 K 近邻排序。
5. 从宽范围 exp 候选队列执行 `v332_exp3`、`v344_exp4`、`v346_exp5`、`v368_exp6` 四个批量物理筛选分片，共 96 个结果。

**结果**

| 分片 | 行数 | 负 slack 数 | 最小 / 最大 WNS | 结论 |
|---|---:|---:|---:|---|
| `v332_exp3` | 25 | 2 | `-0.709878 / +0.431795 ns` | 两个负值都明显过深 |
| `v344_exp4` | 25 | 1 | `-0.0823126 / +0.466653 ns` | 唯一负值仍是严格窗外的 `U3095` |
| `v346_exp5` | 21 | 0 | `+0.117940 / +0.399394 ns` | 全部为正 slack |
| `v368_exp6` | 25 | 0 | `+0.040462 / +0.381240 ns` | 全部为正 slack |

K 近邻模型将 `u_exp/U3765 MXIT2_X1P4M -> X0P5M` 预测为 `-0.102740454 ns`，正好位于目标区间。为避免把模型预测误当成实测结果，随后启动 `c1215fg_v364_e013_u3765` 单候选独立全局时序验证。

独立验证实测为 `+0.0805006 ns`，负裕量 endpoint 数量为 0，与模型预测相差约 `183.241 ps`。

**决策**

该模型只能用于安排候选测量顺序，不能作为接受或排除依据。继续调整模型的收益有限，因此下一步改为按单元类型穷举尚未测量的替换方案，首先选择历史上最容易产生负时序裕量的 XOR/XNOR。

### 5.6 第 5 轮：未测 XOR/XNOR 候选定向穷举

**目的**

已知 exp 层级中的显著负时序裕量多来自 XOR/XNOR 降低驱动强度。本轮不再依赖模型挑选少数候选，而是把尚无物理结果的 XOR/XNOR 替换方案全部测完。

**动作**

1. 合并当前 run 和相同基线历史 run 的筛选结果，按同基线去重键剔除已测方案。
2. 生成 `case013_xor_fresh_v383`，共 32 个替换方案，分为 8 个分片，每个分片 4 个候选。
3. 以批量物理筛选模式运行 `v385_expxor0r`、`v390_expxor1`、`v396_expxor2`、`v399_expxor3`、`v402_expxor4`、`v403_expxor5`、`v407_expxor6`、`v408_expxor7`。首分片的原任务未形成完整结果表，因此使用新的任务标识重新运行，并保存为 `v385_expxor0r`。

**结果**

- 32/32 个替换方案完成；
- 30 个方案仍为正时序裕量；
- `u_exp/U2548 XOR2_X1P4M -> X0P5M=-0.1815 ns`，比下界多负 `66.5 ps`；
- `u_exp/U367 XOR2_X3M -> X0P5M=-0.789675 ns`，严重过深；
- 严格窗口命中数为 0。

**决策**

未测 XOR/XNOR 候选已全部完成。对两个违例过深的实例不直接放弃，而是追加更温和的中间驱动强度档位，判断能否回到目标区间。

### 5.7 第 6 轮：对已知敏感实例测试中间驱动强度档位

**目的**

X0P5 档造成的违例过深，不代表同一实例的其他合法驱动强度都不可用。本轮选择已有极端结果的实例，改用 X0P7、X1M 等中间档，尝试把 WNS 调整到目标区间。

**动作**

在 `c1215pt_v386_expinterp` 中运行 6 个中间驱动强度替换方案：

| 单元替换方案 | 布局合法化后的 WNS |
|---|---:|
| `u_exp/U367 XOR2_X3M -> X0P7M` | `-0.330928 ns` |
| `u_exp/U434 XOR2_X2M -> X0P7M` | `-0.164642 ns` |
| `u_exp/U4678 XOR2_X1P4M -> X1M` | `-0.0121717 ns` |
| `u_exp/U617 XOR2_X1P4M -> X1M` | `-0.00797224 ns` |
| `u_exp/U380 XOR2_X2M -> X1M` | `+0.165729 ns` |
| `u_exp/U3103 XOR2_X3M -> X0P7M` | `+0.0362043 ns` |

**结果**

6 个结果均未落入目标区间。最近的负值 `U434=-0.164642 ns` 仍比下界多负 `49.642 ps`；`U4678/U617` 又过浅，说明这些实例的标准驱动强度档位跨过了目标区间。

**决策**

停止继续细分 XOR/XNOR 驱动强度，转向 BUF 单元类型。BUF 曾产生最接近目标区间、且经过独立验证的近界候选 `FE_OFC7405 X0P8B`，因此其他 BUF 实例可能产生相近的违例幅度。

### 5.8 第 7 轮：未测 BUF / BUFH 候选

**目的**

利用 `FE_OFC7405` 的近界结果，在尚无物理结果的 buffer 实例及同功能库单元变体上寻找相近的延迟增量，同时避免重复已经测过的替换方案。

**动作**

1. 生成 `case013_buf_fresh_v392`，根据历史筛选结果和同基线去重键剔除已测方案，并保留实例多样性。
2. 选出 6 个尚未测量的 BUF/BUFH 替换方案。
3. 在 `c1215pt_v393_expbuf` 中一次完成 6 个候选的批量物理筛选。

**结果**

- 6/6 为正时序裕量；
- 最小值为 `FE_OFC7113 BUF_X1B -> X0P7B=+0.0208311 ns`；
- 其余值位于 `+0.0747023`～`+0.496897 ns`；
- 负 slack 数和窗口命中数均为 0。

**决策**

本轮未测 buffer 候选全部排除。最后转向 MXIT2，因为历史快速时序估算结果中该单元类型曾出现 `-0.111301 ns` 的窗口内预测，并且仍有一批完全相同替换键尚未获得物理结果的候选。

### 5.9 第 8 轮：未测 MXIT2 候选

**目的**

验证 MXIT2 的快速时序估算近界结果，能否在其他未测实例上形成稳定的物理窗口命中，并补齐该单元类型尚未覆盖的候选。

**动作**

1. 生成 `case013_mxit_fresh_v404`，共 23 个未测替换方案，分为 4 个分片。
2. 在 `c1215pt_v410_expmxit0` 完成首分片 6 个候选。
3. 在 `c1215pt_v413_expmxit1` 运行第二分片；用户停止前保存 2 个结果。

**结果**

| 已完成分片 | 结果数 | 负裕量结果数 | WNS 范围 |
|---|---:|---:|---:|
| `v410_expmxit0` | 6 | 0 | `+0.165099`～`+0.578037 ns` |
| `v413_expmxit1` | 2 | 0 | `+0.263529`～`+0.422799 ns` |

已测 8 个替换方案全部为正时序裕量；另有 15 个替换方案未测。

**决策**

由于用户要求停止，本轮并未完成全部候选。报告只能写 8/23 已排除，不能把剩余 15 个候选视为失败。

### 5.10 第 9 轮：安全停止与证据保全

**目的**

立即停止计算，同时不丢失 live batch 已经产生的部分结果，也不误杀其他 VM 或历史任务。

**动作**

1. 中断四个本地筛选 driver。
2. 发现本地 SSH 中断后虚拟机内 Innovus 仍在运行，于是按精确筛选任务标识查找对应 bash、timeout 和 Innovus 进程树。
3. 只向四个目标任务的精确 PID 发送 TERM，不停止 VM，也不触碰无关任务。
4. 对每个筛选任务标识重新执行 `pgrep`，确认目标进程消失。
5. 将四个中断目录的 `screen.tsv`、日志和配置同步回本地。

**结果**

- case013 的 `c1215pt_v413_expmxit1` 最终保留 2 个可用的批量物理筛选结果；
- 远端没有遗留对应 Innovus/timeout 进程；
- state 未被错误提升，仍为 `PLANNED`；
- 没有生成违反态 checkpoint、instruction、Gold fix 或 replay 产物。

**决策**

搜索在可恢复断点上结束。以后恢复时先纳入这 2 个部分结果，并从剩余 15 个 MXIT2 替换方案继续，而不是重跑已完成分片。

### 5.11 时间消耗归因

从首个 case013 专用合法候选全集产物（10:41）到用户停止（20:30 后），经过时间约 9 小时 49 分钟。该时间与 011、012、014、015 的并行任务共享远端槽位，因此不能解释为 case013 独占 CPU 时间，但阶段归属很明确：

- 10:41–13:52：建立合法候选全集、跨 run 去重、物理结果预测排序和首批任务准备；
- 13:52–20:30：多轮 `ecoChangeCell + refinePlace + post-route timing` 物理构造筛选，以及少量单候选独立全局验证；
- 正式 calibration、违反态 checkpoint 冻结、Gold fix、双 replay、strict validation：均未开始。

因此，case013 的主要时间确实消耗在“构造并证明一个符合完整合同的违例”上，而不是修复违例。负时序裕量本身并不稀缺；稀缺的是同时满足 exp 层级、唯一负裕量 endpoint、严格 30 ps 目标区间和未占用指纹的单元替换方案。

## 6. 关键近窗证据

| 单元替换方案 | 证据级别 | 实测 WNS | 相对窗口 | 不能采用的原因 |
|---|---|---:|---:|---|
| `u_exp/U3103 XOR2_X3M -> X0P5M` | 已通过正式校准和验证 | `-0.111876 ns` | 窗内 | 已由 `B1_CASE_010` 占用，替换方案和指纹不可复用 |
| `u_exp/U3095 XOR2_X2M -> X0P5M` | 同一进程内的多候选物理筛选 | `-0.0823126 ns` | 上界外 `2.6874 ps` | 严格越界，且未做单候选独立验证 |
| `u_exp/FE_OFC7405_n4112 BUF_X2M -> X0P8B` | 单候选独立全局验证 | `-0.0815992 ns` | 上界外 `3.4008 ps` | 严格越界 |
| `u_exp/FE_OFC7405_n4112 BUF_X2M -> X0P8M` | 同一进程内的多候选物理筛选 | `-0.0796928 ns` | 上界外 `5.3072 ps` | 严格越界 |
| `u_exp/FE_OFC7405_n4112 BUF_X2M -> X0P7M` | 同一进程内的多候选全局时序检查 | `-0.124089 ns` | 下界外 `9.089 ps` | 严格越界，且未做单候选独立验证 |
| `u_exp/FE_OFC7405_n4112 BUF_X2M -> X0P7B` | 同一进程内的多候选全局时序检查 | `-0.128893 ns` | 下界外 `13.893 ps` | 严格越界，且未做单候选独立验证 |
| `u_exp/U3765 MXIT2_X1P4M -> X0P5M` | 单候选独立全局验证 | `+0.0805006 ns` | 无违例 | 负裕量 endpoint 数量为 0 |

快速时序估算表中曾有 5 个 exp 替换方案落入 case013 目标区间，但布局合法化后的时序结果出现了足以跨越整个 30 ps 区间的偏移：

- `FE_OFC7405 X0P7B`：快速估算 `-0.109701`，物理结果 `-0.128893 ns`；
- `FE_OFC7405 X0P7M`：快速估算 `-0.104901`，物理结果 `-0.124089 ns`；
- `u_exp/U3395 MXIT2_X0P7M`：快速估算 `-0.111301`，物理结果 `-0.146129 ns`；
- `u_exp/U418 XOR2_X1M`：快速估算 `-0.0896006`，单候选验证结果 `-0.203806 ns`；
- `u_exp/U3103 XOR2_X0P5M`：快速估算 `-0.102901`，单候选验证结果 `-0.111876 ns`，但已被 case010 占用。

## 7. 搜索足迹与失败原因

对明确归入本轮 case013/exp 尝试的物理表重新汇总：

- 共 165 个批量物理筛选或全局时序检查结果；
- 按同基线去重键去重后为 162 个单元替换方案；
- 12 个不同替换方案产生负时序裕量；
- 未占用且严格落入目标区间的替换方案为 0；
- 另有 1 个窗内正式验证方案已由 `B1_CASE_010` 占用。

失败原因不是没有负时序裕量，而是标准驱动强度档位离散、快速估算到布局合法化后时序结果的偏差、严格边界和指纹占用无法同时满足；近界值不允许通过扩展容差来接受。

## 8. 停止点与恢复方式

- state 仍为 `PLANNED`，没有正式 binding 或 case 目录；
- 未测 XOR/XNOR 候选 32/32、BUF 候选 6/6 已完成并排除；
- MXIT2 只完成 8/23，剩余 15 个替换方案未测；
- 中断目录 `c1215pt_v413_expmxit1` 已保存 2 行结果；
- 恢复时先重建跨 run 的同基线候选去重索引，减去这 8 个已测 MXIT2 替换方案，再动态重新分片；不能盲目重跑旧分片。

## 9. 证据入口

### 9.1 Case 合同与初始候选

- [state.json](../../work/batch100-relaxed-v3-r2/state/B1_CASE_013.json)
- [binding.json](../../work/batch100-relaxed-v3-r2/bindings/B1_CASE_013.json)
- [初始候选物理证据](../../work/batch100-relaxed-v3-r2/manual_screen/case007_fresh_v4/screen.tsv)

### 9.2 合法候选全集与物理结果预测排序

- [exp universe v300](../../work/batch100-relaxed-v3-r2/manual_candidates/case011015_universe_exp_v300)
- [快速估算优先候选集 v301](../../work/batch100-relaxed-v3-r2/manual_candidates/case011015_triage_exp_v301)
- [物理结果预测排序集 v302](../../work/batch100-relaxed-v3-r2/manual_candidates/case011015_modeled_exp_v302)
- [预测结果优先候选集 v303](../../work/batch100-relaxed-v3-r2/manual_candidates/case011015_triage_modeled_exp_v303)

### 9.3 中间驱动强度与相似单元偏差校正

- [exp 插值筛选](../../work/batch100-relaxed-v3-r2/manual_screen/c1115pt_v315_exp_interp/screen.tsv)
- [X0P8B 单候选独立验证](../../work/batch100-relaxed-v3-r2/manual_screen/c1115fg_v316_buf08b_00000/screen.tsv)
- [相似单元校正候选集 v329](../../work/batch100-relaxed-v3-r2/manual_candidates/case013_analog_exp_v329)
- [相似单元校正分片 0](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v333_expa0/screen.tsv)
- [相似单元校正分片 1](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v334_expa1/screen.tsv)
- [相似单元校正分片 2](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v345_expa2/screen.tsv)

### 9.4 中位偏差、未测实例与 K 近邻预测

- [中位偏差候选集 v354](../../work/batch100-relaxed-v3-r2/manual_candidates/case013_analog_median_v354)
- [未测实例相似单元候选集 v357](../../work/batch100-relaxed-v3-r2/manual_candidates/case013_analog_freshinst_v357)
- [零偏差快速估算候选集 v360](../../work/batch100-relaxed-v3-r2/manual_candidates/case013_rawmeasured_freshinst_v360)
- [跨 run K 近邻候选集 v363](../../work/batch100-relaxed-v3-r2/manual_candidates/case013_knn_crossrun_v363)
- [宽范围 exp 分片 3](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v332_exp3/screen.tsv)
- [宽范围 exp 分片 4](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v344_exp4/screen.tsv)
- [宽范围 exp 分片 5](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v346_exp5/screen.tsv)
- [宽范围 exp 分片 6](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v368_exp6/screen.tsv)
- [U3765 单候选独立全局验证](../../work/batch100-relaxed-v3-r2/manual_screen/c1215fg_v364_e013_u3765/screen.tsv)

### 9.5 XOR/XNOR 与中间驱动强度

- [未测 XOR/XNOR 候选集](../../work/batch100-relaxed-v3-r2/manual_candidates/case013_xor_fresh_v383)
- [XOR/XNOR 分片 0 重测](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v385_expxor0r/screen.tsv)
- [XOR/XNOR 分片 1](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v390_expxor1/screen.tsv)
- [XOR/XNOR 分片 2](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v396_expxor2/screen.tsv)
- [XOR/XNOR 分片 3](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v399_expxor3/screen.tsv)
- [XOR/XNOR 分片 4](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v402_expxor4/screen.tsv)
- [XOR/XNOR 分片 5](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v403_expxor5/screen.tsv)
- [XOR/XNOR 分片 6](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v407_expxor6/screen.tsv)
- [XOR/XNOR 分片 7](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v408_expxor7/screen.tsv)
- [中间驱动强度筛选](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v386_expinterp/screen.tsv)

### 9.6 BUF 与 MXIT2

- [未测 BUF 候选集](../../work/batch100-relaxed-v3-r2/manual_candidates/case013_buf_fresh_v392)
- [BUF 批量物理筛选结果](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v393_expbuf/screen.tsv)
- [未测 MXIT2 候选集](../../work/batch100-relaxed-v3-r2/manual_candidates/case013_mxit_fresh_v404)
- [MXIT2 完成分片](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v410_expmxit0/screen.tsv)
- [MXIT2 中断分片](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v413_expmxit1/screen.tsv)

## 10. SFT / Benchmark 使用边界

不得把本记录包装成成功 case，也不得从快速时序估算的近界值反推 Gold fix。只有新的未占用候选完成单候选独立全局验证、正式校准、checkpoint 冻结、两个独立 replay 和严格验证后，才可替换本记录中的未成题状态。
