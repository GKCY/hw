# B1_CASE_007 Timing ECO SFT 数据报告

## 1. Case 定位

| 字段 | 内容 |
|---|---|
| 类型 / 难度 | Setup / E |
| 形态 / 策略 | single_cone / A |
| 候选层级 | exp |
| 注入策略 | relaxed-v3 endpoint_local_rvt_downsize |
| 修复策略 | exact_inverse_rvt_restore |
| ECO 修改预算 | 严格 1 个组合逻辑 RVT cell；Gold 实际修改 1 项 |
| 注入校准 | FROZEN；目标 [-0.050, -0.030] ns，实测 -0.031662 ns |
| 证据来源 | work/batch100-relaxed-v3-r2，状态 VALIDATED |

本报告包含隐藏注入 oracle，仅用于人工审核。训练时只应使用 case 的 instruction.txt 和 answer.txt，不能把本报告或注入动作送入模型。

## 2. Design 初始情况

### 2.1 未注入基线

- Top：NV_NVDLA_CMAC_CORE_mac；工具：Innovus 21.10-p004_1。
- Setup/Hold view：functional_setup_ss / functional_hold_ff。
- 基线 setup WNS/TNS=+0.051/+0.000 ns；hold WNS/TNS=+0.050/+0.000 ns。
- 基线 DRV：transition=0、capacitance=0、fanout=431；connectivity=0；冻结参考 DRC=6952。
- 技术分类 mock_training_non_signoff；这是 benchmark 数据，不是 signoff 声明。

### 2.2 注入后、修复前的 SFT 输入

| 检查项 | 实测值 |
|---|---|
| Setup | WNS/TNS=-0.032/-0.032 ns；精确校准 WNS=-0.031662 ns |
| 精确负 endpoint | u_exp/exp_sft_01_reg_2_/D |
| Path delay | cell=7.3141004 ns，net=0.27190012 ns；占比 96.3212% / 3.6788% |
| Hold | WNS/TNS=+0.050/+0.000 ns |
| DRV | transition=2，capacitance=0，fanout=431 |
| DRC / Connectivity | 6960 / 9 |

## 3. SFT Instruction（原文）

~~~text
请在随样本提供的 NV_NVDLA_CMAC_CORE_mac 已布线 post-route violating checkpoint 上完成 B1_CASE_007 setup timing ECO。
当前 Innovus MMMC setup 视图 functional_setup_ss 的 WNS/TNS=-0.032000/-0.032000 ns；负裕量 endpoint 精确集合为：u_exp/exp_sft_01_reg_2_/D。
当前 hold 仅作为观察证据记录，WNS/TNS=+0.050000/+0.000000 ns。
当前完整物理检查计数：max_transition=2、max_capacitance=0、max_fanout=431、DRC=6960、connectivity=9。
以下局部路径证据均直接取自该 violating checkpoint；每个列表按报告中的正 point delay 从大到小截取，这些条目仅为报告排序结果，不构成实例或目标 ref 推荐：
目标 1: beginpoint=u_exp/cfg_is_fp16_d1_reg_0_/Q; endpoint=u_exp/exp_sft_01_reg_2_/D; slack=-0.031662 ns; data_cell_delay=7.314100 ns; data_net_delay=0.271900 ns。
  1. inst=u_exp/U2548; pin=u_exp/U2548/Y; current_ref=XOR2_X0P7M_A9TR40; reported_point_delay=0.351000 ns
  2. inst=u_exp/u_expmax_l1n0/FE_OFC7116_n66; pin=u_exp/u_expmax_l1n0/FE_OFC7116_n66/Y; current_ref=BUF_X2M_A9TR40; reported_point_delay=0.322000 ns
  3. inst=u_exp/U14; pin=u_exp/U14/Y; current_ref=MXIT2_X0P7M_A9TR40; reported_point_delay=0.279000 ns
  4. inst=u_exp/u_expmax_l0n01/U67; pin=u_exp/u_expmax_l0n01/U67/Y; current_ref=MXIT2_X0P7M_A9TR40; reported_point_delay=0.279000 ns
  5. inst=u_exp/U838; pin=u_exp/U838/Y; current_ref=MXIT2_X0P7M_A9TR40; reported_point_delay=0.276000 ns
  6. inst=u_exp/u_expmax_l1n0/FE_OFC6931_n72; pin=u_exp/u_expmax_l1n0/FE_OFC6931_n72/Y; current_ref=BUF_X3M_A9TR40; reported_point_delay=0.248000 ns
  7. inst=u_exp/u_expmax_l1n0/U559; pin=u_exp/u_expmax_l1n0/U559/Y; current_ref=MXIT2_X0P7M_A9TR40; reported_point_delay=0.229000 ns
  8. inst=u_exp/u_expmax_l1n0/U215; pin=u_exp/u_expmax_l1n0/U215/Y; current_ref=OAI21_X1M_A9TR40; reported_point_delay=0.228000 ns
请从上述可观察路径实例中选择恰好一个不同实例，每个实例只执行一次 ecoChangeCell，并仅替换为逻辑功能、pin signature 与 RVT 家族等价的 drive-strength ref；这里的最小化指修改实例数固定为题面预算，目标 ref 必须根据当前可观察证据与等价性、闭合要求选择。
修复后必须满足：setup WNS>=0、TNS=0、无负裕量 endpoint；max_transition/max_capacitance/max_fanout、DRC 和 connectivity 均不得比当前计数增加；placement 必须合法，约束、时钟、实例集合、pin-net 拓扑和路由不得改变。
输出一个 Tcl 代码块。除注释和空行外，命令顺序必须是：setEcoMode -batchMode true；规定数量的 ecoChangeCell -inst {...} -cell {...}；setEcoMode -batchMode false；最后且仅最后执行一次 refinePlace -eco true。
~~~

## 4. Probe / 注入 Tcl

~~~tcl
# Hidden injection oracle; never include in SFT messages.
setEcoMode -batchMode true
ecoChangeCell -inst {u_exp/U2548} -cell {XOR2_X0P7M_A9TR40}
setEcoMode -batchMode false
refinePlace -eco true
~~~

| # | 实例 | 基线 cell | 注入 cell | 可达负 endpoint |
|---:|---|---|---|---|
| 1 | u_exp/U2548 | XOR2_X1P4M_A9TR40 | XOR2_X0P7M_A9TR40 | u_exp/exp_sft_01_reg_2_/D |

该 endpoint-local XOR2 downsize 形成约 32 ps setup 违例。校准确认只有目标 endpoint 为负，并保持实例、连接、约束和路由拓扑不变。

## 5. Gold Fix Tcl（原文）

~~~tcl
setEcoMode -batchMode true
ecoChangeCell -inst {u_exp/U2548} -cell {XOR2_X1P4M_A9TR40}
setEcoMode -batchMode false
refinePlace -eco true
~~~

Gold fix 将唯一注入实例恢复为基线驱动档；两个 replay 的功能等价、pin signature、RVT equivalence、拓扑和 placement 检查均通过。

## 6. 实测修复结果

| 方向 | Innovus 修复前 WNS/TNS | Innovus 修复后 WNS/TNS | PrimeTime |
|---|---:|---:|---|
| Setup | -0.032 / -0.032 ns | +0.051 / +0.000 ns | 未执行；不属于本 case 接受门禁 |
| Hold | +0.050 / +0.000 ns | +0.050 / +0.000 ns | 未执行；仅记录 Innovus hold |

| 物理/完整性检查 | 修复前 | 修复后 | 结论 |
|---|---:|---:|---|
| max_transition | 2 | 0 | PASS |
| max_capacitance | 0 | 0 | 无回归 |
| max_fanout | 431 | 431 | 无回归 |
| DRC total | 6960 | 6952 | 回到冻结参考 |
| Connectivity | 9 | 0 | PASS |
| Routing mutations | 0 | 0 | PASS |

slot4、slot5 两次独立 replay 均为 PASS；修复后负 endpoint 为空，功能等价、约束/拓扑哈希、placement legal 和物理 no-regression 结果一致。

## 7. 证据入口

- [instruction.txt](../../work/batch100-relaxed-v3-r2/cases/B1_CASE_007/instruction.txt)
- [fix.tcl](../../work/batch100-relaxed-v3-r2/cases/B1_CASE_007/fix.tcl)
- [metrics.json](../../work/batch100-relaxed-v3-r2/cases/B1_CASE_007/metrics.json)
- [validation.json](../../work/batch100-relaxed-v3-r2/cases/B1_CASE_007/validation.json)
- [binding.json](../../work/batch100-relaxed-v3-r2/bindings/B1_CASE_007.json)
- [accepted_measurement.json](../../work/batch100-relaxed-v3-r2/jobs/B1_CASE_007/calibration/accepted_measurement.json)
- [state.json](../../work/batch100-relaxed-v3-r2/state/B1_CASE_007.json)

## 8. SFT / Benchmark 使用边界

该 case 已完成真实 Innovus 双 replay，但尚未形成 100-case finalized dataset。报告中的 hidden oracle 不能进入训练消息；候选答案必须由 trusted harness 重新验证全部 hard gate。
