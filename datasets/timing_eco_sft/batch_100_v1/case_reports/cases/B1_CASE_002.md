# B1_CASE_002 Timing ECO SFT 数据报告

## 1. Case 定位

| 字段 | 内容 |
|---|---|
| 类型 / 难度 | Setup / `E` |
| 形态 / 策略 | `single_cone` / `A` |
| 候选层级 | `pipeline` |
| 注入策略 | relaxed-v3 `endpoint_local_rvt_downsize` |
| 修复策略 | `exact_inverse_rvt_restore` |
| ECO 修改预算 | 严格 1 个组合逻辑 RVT cell；Gold 实际修改 1 项 |
| 注入校准 | `FROZEN`；目标 [-0.092, -0.068] ns，实测 -0.0849323 ns |
| 证据来源 | `work/batch100-relaxed-v3-r1`，状态 `VALIDATED` |

本报告包含隐藏注入 oracle，仅用于人工审核。训练时只应使用 case 的 `instruction.txt` 和 `answer.txt`，不能把本报告或注入动作送入模型。

## 2. Design 初始情况

### 2.1 未注入基线

- Top：`NV_NVDLA_CMAC_CORE_mac`；工具：Innovus `21.10-p004_1`。
- Setup/Hold view：`functional_setup_ss` / `functional_hold_ff`。
- 基线 setup WNS/TNS=`+0.051/+0.000 ns`；hold WNS/TNS=`+0.050/+0.000 ns`。
- 基线 DRV：transition=0、capacitance=0、fanout=431；connectivity=0；冻结参考 DRC=6952。
- 技术分类 `mock_training_relaxed_v3_non_signoff`；所有门禁均为 benchmark no-regression，不是 signoff 声明。

### 2.2 注入后、修复前的 SFT 输入

| 检查项 | 实测值 |
|---|---|
| Setup | WNS/TNS=-0.085/-0.085 ns；精确校准 WNS=-0.0849323 ns |
| 精确负 endpoint | `pp_out_l0n13_1_d1_reg_3_/D` |
| Path delay | cell=7.5038999 ns，net=0.02040009 ns；占比 99.6826% / 0.3174% |
| Hold | WNS/TNS=+0.050/+0.000 ns |
| DRV | transition=2，capacitance=0，fanout=431 |
| DRC / Connectivity | 6954 / 6 |

## 3. SFT Instruction（原文）

```text
案例 B1_CASE_002 的 violating checkpoint 在 pipeline 层级观察到 setup WNS=-0.085000 ns、TNS=-0.085000 ns。负裕量 endpoint 恰为：pp_out_l0n13_1_d1_reg_3_/D。目标 data path 的 cell/net delay (ns) 为：pp_out_l0n13_1_d1_reg_3_/D=7.503900/0.020400。DRV 计数：transition=2，capacitance=0，fanout=431。请给出严格 1 个不同实例的最小 RVT 组合逻辑 resize ECO，并在末尾仅执行 refinePlace -eco true；不得修改连接、约束、时钟或路由。
```

## 4. Probe / 注入 Tcl

### 4.1 冻结注入动作的核心 Tcl

```tcl
# Hidden injection oracle; never include in SFT messages.
setEcoMode -batchMode true
ecoChangeCell -inst {u_tree_l0n13/U209} -cell {OAI21_X0P5M_A9TR40}
setEcoMode -batchMode false
refinePlace -eco true
```

| # | 实例 | 基线 cell | 注入 cell | 可达负 endpoint |
|---:|---|---|---|---|
| 1 | `u_tree_l0n13/U209` | `OAI21_X3M_A9TR40` | `OAI21_X0P5M_A9TR40` | `pp_out_l0n13_1_d1_reg_3_/D` |

### 4.2 注入 Tcl 做了什么

- 将 endpoint-local OAI21 从 X3M downsize 到 X0P5M，产生约 85 ps setup 违例和 2 个 max-transition 计数。
- frozen reachability 证明该实例只覆盖目标 endpoint；违反态精确负集合只有该 endpoint。
- relaxed-v3 将原 IA 构造简化为一个 I0 resize，但保留原 80+/-12 ps severity 和全部物理/replay 门禁。

## 5. Gold Fix Tcl（原文）

```tcl
setEcoMode -batchMode true
ecoChangeCell -inst {u_tree_l0n13/U209} -cell {OAI21_X3M_A9TR40}
setEcoMode -batchMode false
refinePlace -eco true
```

Gold fix 恢复 OAI21 基线驱动档。两个 replay 均证明功能等价、placement legal、拓扑不变且无 routing mutation。

## 6. 实测修复结果

| 方向 | Innovus 修复前 WNS/TNS | Innovus 修复后 WNS/TNS | PrimeTime |
|---|---:|---:|---|
| Setup | -0.085 / -0.085 ns | +0.051 / +0.000 ns | 未执行；不属于本 case 接受门禁 |
| Hold | +0.050 / +0.000 ns | +0.050 / +0.000 ns | 未执行；仅记录 Innovus hold |

| 物理/完整性检查 | 修复前 | 修复后 | 结论 |
|---|---:|---:|---|
| max_transition | 2 | 0 | PASS |
| max_capacitance | 0 | 0 | 无回归 |
| max_fanout | 431 | 431 | 无回归 |
| DRC total | 6954 | 6952 | 回到冻结参考 |
| Connectivity | 6 | 0 | PASS |
| Routing mutations | 0 | 0 | PASS |

`slot1`、`slot2` 两次独立 replay 均为 `PASS`；修复后的 setup、hold、DRV/DRC、连接、功能、约束、拓扑与放置结果一致。

## 7. 证据入口

- [instruction.txt](../../work/batch100-relaxed-v3-r1/cases/B1_CASE_002/instruction.txt)
- [fix.tcl](../../work/batch100-relaxed-v3-r1/cases/B1_CASE_002/fix.tcl)
- [metrics.json](../../work/batch100-relaxed-v3-r1/cases/B1_CASE_002/metrics.json)
- [validation.json](../../work/batch100-relaxed-v3-r1/cases/B1_CASE_002/validation.json)
- [binding.json](../../work/batch100-relaxed-v3-r1/bindings/B1_CASE_002.json)
- [accepted_measurement.json](../../work/batch100-relaxed-v3-r1/jobs/B1_CASE_002/calibration/accepted_measurement.json)
- [state.json](../../work/batch100-relaxed-v3-r1/state/B1_CASE_002.json)

## 8. SFT / Benchmark 使用边界

该 case 已在 relaxed-v3 partial run 中通过双 replay，但尚未形成 100-case finalized dataset。候选必须由 trusted harness 从冻结 checkpoint 重放，并满足精确负端点、setup/TNS、功能等价、约束/拓扑、placement 和物理 no-regression 全部门禁。
