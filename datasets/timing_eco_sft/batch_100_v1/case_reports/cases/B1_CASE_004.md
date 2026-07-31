# B1_CASE_004 Timing ECO SFT 数据报告

## 1. Case 定位

| 字段 | 内容 |
|---|---|
| 类型 / 难度 | Setup / `E` |
| 形态 / 策略 | `single_cone` / `A` |
| 候选层级 | `exp` |
| 注入策略 | relaxed-v3 `endpoint_local_rvt_downsize` |
| 修复策略 | `exact_inverse_rvt_restore` |
| ECO 修改预算 | 严格 1 个组合逻辑 RVT cell；Gold 实际修改 1 项 |
| 注入校准 | `FROZEN`；目标 [-0.138, -0.102] ns，实测 -0.1240890 ns |
| 证据来源 | `work/batch100-relaxed-v3-r1`，状态 `VALIDATED` |

本报告包含隐藏注入 oracle，仅用于人工审核。训练时只应使用 case 的 `instruction.txt` 和 `answer.txt`，不能把本报告或注入动作送入模型。

## 2. Design 初始情况

### 2.1 未注入基线

- Top：`NV_NVDLA_CMAC_CORE_mac`；工具：Innovus `21.10-p004_1`。
- Setup/Hold view：`functional_setup_ss` / `functional_hold_ff`。
- 基线 setup WNS/TNS=`+0.051/+0.000 ns`；hold WNS/TNS=`+0.050/+0.000 ns`。
- 基线 DRV：transition=0、capacitance=0、fanout=431；connectivity=0；冻结参考 DRC=6952。
- 技术分类 `mock_training_relaxed_v3_non_signoff`；这是 benchmark 数据，不是 signoff 声明。

### 2.2 注入后、修复前的 SFT 输入

| 检查项 | 实测值 |
|---|---|
| Setup | WNS/TNS=-0.124/-0.124 ns；精确校准 WNS=-0.1240890 ns |
| 精确负 endpoint | `u_exp/exp_sft_13_reg_1_/D` |
| Path delay | cell=7.512600 ns，net=0.216200 ns；占比 97.1154% / 2.8846% |
| Hold | WNS/TNS=+0.050/+0.000 ns |
| DRV | transition=0，capacitance=0，fanout=431 |
| DRC / Connectivity | 6953 / 3 |

## 3. SFT Instruction（原文）

```text
案例 B1_CASE_004 的 violating checkpoint 在 exp 层级观察到 setup WNS=-0.124000 ns、TNS=-0.124000 ns。负裕量 endpoint 恰为：u_exp/exp_sft_13_reg_1_/D。目标 data path 的 cell/net delay (ns) 为：u_exp/exp_sft_13_reg_1_/D=7.512600/0.216200。DRV 计数：transition=0，capacitance=0，fanout=431。请给出严格 1 个不同实例的最小 RVT 组合逻辑 resize ECO，并在末尾仅执行 refinePlace -eco true；不得修改连接、约束、时钟或路由。
```

## 4. Probe / 注入 Tcl

### 4.1 冻结注入动作的核心 Tcl

```tcl
# Hidden injection oracle; never include in SFT messages.
setEcoMode -batchMode true
ecoChangeCell -inst {u_exp/FE_OFC7405_n4112} -cell {BUF_X0P7M_A9TR40}
setEcoMode -batchMode false
refinePlace -eco true
```

| # | 实例 | 基线 cell | 注入 cell | 可达负 endpoint |
|---:|---|---|---|---|
| 1 | `u_exp/FE_OFC7405_n4112` | `BUF_X2M_A9TR40` | `BUF_X0P7M_A9TR40` | `u_exp/exp_sft_13_reg_1_/D` |

### 4.2 注入 Tcl 做了什么

- 对 exp 路径上的 endpoint-local buffer 做等价 RVT downsize，产生约 124 ps 单 endpoint setup 违例。
- 候选先经过 raw ranking 和 fresh-refine，再由正式 calibration、checkpoint portability 与双 replay 确认。
- relaxed-v3 将原 IA/共享负载构造简化为 I0 exact inverse，但保留 120+/-18 ps severity 和全部物理门禁。

## 5. Gold Fix Tcl（原文）

```tcl
setEcoMode -batchMode true
ecoChangeCell -inst {u_exp/FE_OFC7405_n4112} -cell {BUF_X2M_A9TR40}
setEcoMode -batchMode false
refinePlace -eco true
```

Gold fix 恢复 buffer 基线驱动档，不改连接、约束、时钟或路由。两个 replay 的功能等价、拓扑和 placement 检查均通过。

## 6. 实测修复结果

| 方向 | Innovus 修复前 WNS/TNS | Innovus 修复后 WNS/TNS | PrimeTime |
|---|---:|---:|---|
| Setup | -0.124 / -0.124 ns | +0.051 / +0.000 ns | 未执行；不属于本 case 接受门禁 |
| Hold | +0.050 / +0.000 ns | +0.050 / +0.000 ns | 未执行；仅记录 Innovus hold |

| 物理/完整性检查 | 修复前 | 修复后 | 结论 |
|---|---:|---:|---|
| max_transition | 0 | 0 | 无回归 |
| max_capacitance | 0 | 0 | 无回归 |
| max_fanout | 431 | 431 | 无回归 |
| DRC total | 6953 | 6952 | 回到冻结参考 |
| Connectivity | 3 | 0 | PASS |
| Routing mutations | 0 | 0 | PASS |

`slot1`、`slot3` 两次独立 replay 均为 `PASS`；修复后的 setup、hold、DRV/DRC、连接、功能、约束、拓扑与放置结果一致。

## 7. 证据入口

- [instruction.txt](../../work/batch100-relaxed-v3-r1/cases/B1_CASE_004/instruction.txt)
- [fix.tcl](../../work/batch100-relaxed-v3-r1/cases/B1_CASE_004/fix.tcl)
- [metrics.json](../../work/batch100-relaxed-v3-r1/cases/B1_CASE_004/metrics.json)
- [validation.json](../../work/batch100-relaxed-v3-r1/cases/B1_CASE_004/validation.json)
- [binding.json](../../work/batch100-relaxed-v3-r1/bindings/B1_CASE_004.json)
- [accepted_measurement.json](../../work/batch100-relaxed-v3-r1/jobs/B1_CASE_004/calibration/accepted_measurement.json)
- [state.json](../../work/batch100-relaxed-v3-r1/state/B1_CASE_004.json)

## 8. SFT / Benchmark 使用边界

该 case 已在 relaxed-v3 partial run 中通过双 replay，但尚未形成 100-case finalized dataset。报告中的 hidden oracle 只能用于审核；候选结果必须使用可信重放证据判定。
