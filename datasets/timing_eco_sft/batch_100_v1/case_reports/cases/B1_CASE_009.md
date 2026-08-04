# B1_CASE_009 Timing ECO SFT 数据报告

## 1. Case 定位

| 字段 | 内容 |
|---|---|
| 类型 / 难度 | Setup / E |
| 形态 / 策略 | single_cone / A |
| 候选层级 | top_tree |
| 注入策略 | relaxed-v3 endpoint_local_rvt_downsize |
| 修复策略 | exact_inverse_rvt_restore |
| ECO 修改预算 | 严格 1 个组合逻辑 RVT cell；Gold 实际修改 1 项 |
| 注入校准 | FROZEN；目标 [-0.1035, -0.0765] ns，实测 -0.0902176 ns |
| 保护 endpoint | cfg_is_fp16_d0_reg_0_/D，基线与修复后均为 +6.462 ns |
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
| Setup | WNS/TNS=-0.090/-0.090 ns；精确校准 WNS=-0.0902176 ns |
| 精确负 endpoint | pp_out_l0n11_0_d1_reg_26_/D |
| Path delay | cell=7.4955 ns，net=0.01510039 ns；占比 99.8443% / 0.1557% |
| Hold | WNS/TNS=+0.050/+0.000 ns |
| DRV | transition=2，capacitance=0，fanout=431 |
| DRC / Connectivity | 6956 / 9 |

## 3. SFT Instruction（原文）

~~~text
请在随样本提供的 NV_NVDLA_CMAC_CORE_mac 已布线 post-route violating checkpoint 上完成 B1_CASE_009 setup timing ECO。
当前 Innovus MMMC setup 视图 functional_setup_ss 的 WNS/TNS=-0.090000/-0.090000 ns；负裕量 endpoint 精确集合为：pp_out_l0n11_0_d1_reg_26_/D。
当前 hold 仅作为观察证据记录，WNS/TNS=+0.050000/+0.000000 ns。
当前完整物理检查计数：max_transition=2、max_capacitance=0、max_fanout=431、DRC=6956、connectivity=9。
以下局部路径证据均直接取自该 violating checkpoint；每个列表按报告中的正 point delay 从大到小截取，这些条目仅为报告排序结果，不构成实例或目标 ref 推荐：
目标 1: beginpoint=u_mul_46/cfg_is_int8_d1_reg/Q; endpoint=pp_out_l0n11_0_d1_reg_26_/D; slack=-0.090218 ns; data_cell_delay=7.495500 ns; data_net_delay=0.015100 ns。
  1. inst=u_tree_l0n11/U237; pin=u_tree_l0n11/U237/Y; current_ref=XOR2_X0P5M_A9TR40; reported_point_delay=1.493000 ns
  2. inst=u_tree_l0n11/U510; pin=u_tree_l0n11/U510/SUM; current_ref=ADDFH_X1M_A9TR40; reported_point_delay=0.417000 ns
  3. inst=u_mul_46/U26; pin=u_mul_46/U26/Y; current_ref=INV_X4B_A9TR40; reported_point_delay=0.339000 ns
  4. inst=u_mul_46/FE_OFC3476_cfg_is_int8_d1; pin=u_mul_46/FE_OFC3476_cfg_is_int8_d1/Y; current_ref=BUF_X3M_A9TR40; reported_point_delay=0.328000 ns
  5. inst=u_mul_46/u_booth_4/U33; pin=u_mul_46/u_booth_4/U33/Y; current_ref=AND2_X1M_A9TR40; reported_point_delay=0.327000 ns
  6. inst=u_mul_46/U552; pin=u_mul_46/U552/Y; current_ref=OAI211_X1M_A9TR40; reported_point_delay=0.303000 ns
  7. inst=U11837; pin=U11837/Y; current_ref=NAND2_X1B_A9TR40; reported_point_delay=0.289000 ns
  8. inst=u_mul_46/U406; pin=u_mul_46/U406/Y; current_ref=MXIT2_X1M_A9TR40; reported_point_delay=0.271000 ns
请从上述可观察路径实例中选择恰好一个不同实例，每个实例只执行一次 ecoChangeCell，并仅替换为逻辑功能、pin signature 与 RVT 家族等价的 drive-strength ref；这里的最小化指修改实例数固定为题面预算，目标 ref 必须根据当前可观察证据与等价性、闭合要求选择。
修复后必须满足：setup WNS>=0、TNS=0、无负裕量 endpoint；max_transition/max_capacitance/max_fanout、DRC 和 connectivity 均不得比当前计数增加；placement 必须合法，约束、时钟、实例集合、pin-net 拓扑和路由不得改变。
输出一个 Tcl 代码块。除注释和空行外，命令顺序必须是：setEcoMode -batchMode true；规定数量的 ecoChangeCell -inst {...} -cell {...}；setEcoMode -batchMode false；最后且仅最后执行一次 refinePlace -eco true。
~~~

## 4. Probe / 注入 Tcl

~~~tcl
# Hidden injection oracle; never include in SFT messages.
setEcoMode -batchMode true
ecoChangeCell -inst {u_tree_l0n11/U237} -cell {XOR2_X0P5M_A9TR40}
setEcoMode -batchMode false
refinePlace -eco true
~~~

| # | 实例 | 基线 cell | 注入 cell | 可达负 endpoint |
|---:|---|---|---|---|
| 1 | u_tree_l0n11/U237 | XOR2_X3M_A9TR40 | XOR2_X0P5M_A9TR40 | pp_out_l0n11_0_d1_reg_26_/D |

该 endpoint-local XOR2 downsize 形成约 90 ps setup 违例。保护 endpoint cfg_is_fp16_d0_reg_0_/D 保持正 slack，符合 fork/launch-near 题卡约束。

## 5. Gold Fix Tcl（原文）

~~~tcl
setEcoMode -batchMode true
ecoChangeCell -inst {u_tree_l0n11/U237} -cell {XOR2_X3M_A9TR40}
setEcoMode -batchMode false
refinePlace -eco true
~~~

Gold fix 将唯一注入实例恢复为基线驱动档，没有新增实例、routing mutation 或拓扑变化。

## 6. 实测修复结果

| 方向 | Innovus 修复前 WNS/TNS | Innovus 修复后 WNS/TNS | PrimeTime |
|---|---:|---:|---|
| Setup | -0.090 / -0.090 ns | +0.051 / +0.000 ns | 未执行；不属于本 case 接受门禁 |
| Hold | +0.050 / +0.000 ns | +0.050 / +0.000 ns | 未执行；仅记录 Innovus hold |

| 物理/完整性检查 | 修复前 | 修复后 | 结论 |
|---|---:|---:|---|
| max_transition | 2 | 0 | PASS |
| max_capacitance | 0 | 0 | 无回归 |
| max_fanout | 431 | 431 | 无回归 |
| DRC total | 6956 | 6952 | 回到冻结参考 |
| Connectivity | 9 | 0 | PASS |
| Routing mutations | 0 | 0 | PASS |

slot1、slot4 两次独立 replay 均为 PASS；保护 endpoint 在两次 replay 中均保持 +6.462 ns，功能等价、约束/拓扑哈希、placement legal 和物理 no-regression 结果一致。

## 7. 证据入口

- [instruction.txt](../../work/batch100-relaxed-v3-r2/cases/B1_CASE_009/instruction.txt)
- [fix.tcl](../../work/batch100-relaxed-v3-r2/cases/B1_CASE_009/fix.tcl)
- [metrics.json](../../work/batch100-relaxed-v3-r2/cases/B1_CASE_009/metrics.json)
- [validation.json](../../work/batch100-relaxed-v3-r2/cases/B1_CASE_009/validation.json)
- [binding.json](../../work/batch100-relaxed-v3-r2/bindings/B1_CASE_009.json)
- [accepted_measurement.json](../../work/batch100-relaxed-v3-r2/jobs/B1_CASE_009/calibration/accepted_measurement.json)
- [state.json](../../work/batch100-relaxed-v3-r2/state/B1_CASE_009.json)

## 8. SFT / Benchmark 使用边界

该 case 已完成真实 Innovus 双 replay，但尚未形成 100-case finalized dataset。报告中的 hidden oracle 不能进入训练消息；候选答案必须由 trusted harness 重新验证全部 hard gate。
