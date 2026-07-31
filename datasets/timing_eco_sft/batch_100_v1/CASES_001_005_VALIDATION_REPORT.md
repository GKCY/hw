# B1_CASE_001–005 构造与验证报告

## 1. 结论

`B1_CASE_001`～`B1_CASE_005` 均已有真实 Cadence Innovus 验证证据，五题均达到各自目标 WNS 窗口，并在两个独立 replay 槽位上完成修复闭合和物理门禁检查。

需要区分两组证据来源：

- `B1_CASE_001` 来自独立的原始/manual-v1 run：`work/manual_v1/B1_CASE_001`；
- `B1_CASE_002`～`B1_CASE_005` 来自 relaxed-v3 run：`work/batch100-relaxed-v3-r1`。

因此，本报告证明的是五个 case 分别已经 `VALIDATED`，不表示它们已在同一个 100-case run 中完成 finalization。`B1_CASE_002`～`005` 的技术分类为 `mock_training_relaxed_v3_non_signoff`；`B1_CASE_001` 保留其原始独立 run 的 `mock_training_non_signoff` 分类。

## 2. 验证环境与接受条件

- 设计：`NV_NVDLA_CMAC_CORE_mac`
- 工具：Cadence Innovus `21.10-p004_1`
- Setup view：`functional_setup_ss`
- Hold view：`functional_hold_ff`
- 时钟周期：8 ns
- 基线 setup WNS/TNS：`+0.051 ns / 0 ns`
- 基线 hold WNS：`+0.050 ns`
- 允许的 ECO：功能等价组合逻辑 RVT `ecoChangeCell`，最后仅执行 `refinePlace -eco true`
- 违反态必须只有题卡指定的精确负 endpoint 集合
- 修复后必须满足 setup WNS 非负、setup TNS 为 0、无新增负 endpoint
- 必须保持功能等价、拓扑不变、无 routing mutation、放置合法，且 DRV/DRC/连接不得回归
- 每题必须从哈希绑定的违反态 checkpoint 在两个不同 VM 槽位独立 replay

`B1_CASE_002`～`004` 的原始题卡分别使用 IA 构造，其中 `B1_CASE_003` 原计划为两个有效修改。relaxed-v3 只放宽构造复杂度：统一为一个 endpoint-local I0 resize，并允许 exact inverse repair；原始 severity、tolerance、层级与物理/replay 门禁保持不变。

## 3. 汇总结果

| Case | 层级 | 目标 WNS | 实测 WNS | 与中心值偏差 | 注入 resize | Replay | 状态 |
|---|---|---:|---:|---:|---|---|---|
| `B1_CASE_001` | `exp` | -60 +/-10 ps | -60.7419 ps | -0.7419 ps | `u_exp/U434`: `XOR2_X2M` -> `XOR2_X1M` | `slot1`, `slot2` | `VALIDATED` |
| `B1_CASE_002` | `pipeline` | -80 +/-12 ps | -84.9323 ps | -4.9323 ps | `u_tree_l0n13/U209`: `OAI21_X3M` -> `OAI21_X0P5M` | `slot1`, `slot2` | `VALIDATED` |
| `B1_CASE_003` | `top_tree` | -100 +/-15 ps | -96.7464 ps | +3.2536 ps | `u_tree_l0n11/U123`: `XNOR2_X3M` -> `XNOR2_X0P7M` | `slot0`, `slot3` | `VALIDATED` |
| `B1_CASE_004` | `exp` | -120 +/-18 ps | -124.0890 ps | -4.0890 ps | `u_exp/FE_OFC7405_n4112`: `BUF_X2M` -> `BUF_X0P7M` | `slot1`, `slot3` | `VALIDATED` |
| `B1_CASE_005` | `pipeline` | -140 +/-21 ps | -135.8390 ps | +4.1610 ps | `u_tree_l0n11/U705`: `XNOR2_X3M` -> `XNOR2_X0P5M` | `slot0`, `slot2` | `VALIDATED` |

五题修复后的两个 replay 结果一致：setup WNS 均恢复为 `+0.051 ns`，setup TNS 均为 `0 ns`，负 endpoint 集合为空，hold WNS 保持 `+0.050 ns`。

## 4. 分题结果

### 4.1 B1_CASE_001

- 证据来源：`work/manual_v1/B1_CASE_001`
- 场景：`exp` 层级、单关键锥、capture 近端组合逻辑 resize，I0 直接逆操作允许
- 目标 endpoint：`u_exp/exp_sft_09_reg_1_/D`
- 注入：`u_exp/U434`，`XOR2_X2M_A9TR40 -> XOR2_X1M_A9TR40`
- 实测违反态 WNS：`-0.0607419 ns`
- 违反态负 endpoint：仅 `u_exp/exp_sft_09_reg_1_/D`
- 路径构成：cell delay `96.3822%`，net delay `3.6178%`
- 修复：`u_exp/U434` 恢复为 `XOR2_X2M_A9TR40`
- Replay：`slot1`、`slot2`，均 `PASS`
- 修复后：setup WNS/TNS=`+0.051/0 ns`，hold WNS=`+0.050 ns`
- 物理结果：max transition/capacitance/fanout=`0/0/431`；DRC `6954 -> 6952`；connectivity `7 -> 0`；routing mutation=`0`
- 功能等价与 placement legal：两个 replay 均通过

该题证明单点 XOR2 drive 恢复可以稳定闭合约 60 ps 的目标违例，且收益由 cell delay 主导，符合 G0/P0。

### 4.2 B1_CASE_002

- 证据来源：`work/batch100-relaxed-v3-r1`
- 场景：`pipeline` 层级、单关键锥；relaxed-v3 单点 I0 构造
- 目标 endpoint：`pp_out_l0n13_1_d1_reg_3_/D`
- 注入：`u_tree_l0n13/U209`，`OAI21_X3M_A9TR40 -> OAI21_X0P5M_A9TR40`
- 实测违反态 WNS：`-0.0849323 ns`
- 违反态负 endpoint：仅 `pp_out_l0n13_1_d1_reg_3_/D`
- 路径构成：cell delay `99.6826%`，net delay `0.3174%`
- 修复：`u_tree_l0n13/U209` 恢复为 `OAI21_X3M_A9TR40`
- Replay：`slot1`、`slot2`，均 `PASS`
- 修复后：setup WNS/TNS=`+0.051/0 ns`，hold WNS=`+0.050 ns`
- 物理结果：max transition `2 -> 0`，max capacitance=`0`，max fanout=`431`；DRC `6954 -> 6952`；connectivity `6 -> 0`；routing mutation=`0`
- 功能等价与 placement legal：两个 replay 均通过

该题实测值位于 `[-92, -68] ps` 窗口内，且不存在附带负 endpoint。

### 4.3 B1_CASE_003

- 证据来源：`work/batch100-relaxed-v3-r1`
- 场景：`top_tree` 层级、单关键锥；relaxed-v3 将原计划两点 IA 构造简化为单点 I0
- 目标 endpoint：`pp_out_l0n11_0_d1_reg_25_/D`
- 注入：`u_tree_l0n11/U123`，`XNOR2_X3M_A9TR40 -> XNOR2_X0P7M_A9TR40`
- 实测违反态 WNS：`-0.0967464 ns`
- 违反态负 endpoint：仅 `pp_out_l0n11_0_d1_reg_25_/D`
- 路径构成：cell delay `99.8524%`，net delay `0.1476%`
- 修复：`u_tree_l0n11/U123` 恢复为 `XNOR2_X3M_A9TR40`
- Replay：`slot0`、`slot3`，均 `PASS`
- 修复后：setup WNS/TNS=`+0.051/0 ns`，hold WNS=`+0.050 ns`
- 物理结果：max transition `2 -> 0`，max capacitance=`0`，max fanout=`431`；DRC `6956 -> 6952`；connectivity `9 -> 0`；routing mutation=`0`
- 功能等价与 placement legal：两个 replay 均通过

该题实测值位于 `[-115, -85] ps` 窗口内，说明保留 100 ps severity 的同时可以把构造复杂度降为一个 endpoint-local resize。

### 4.4 B1_CASE_004

- 证据来源：`work/batch100-relaxed-v3-r1`
- 场景：`exp` 层级、单关键锥；relaxed-v3 单点 I0 构造
- 目标 endpoint：`u_exp/exp_sft_13_reg_1_/D`
- 注入：`u_exp/FE_OFC7405_n4112`，`BUF_X2M_A9TR40 -> BUF_X0P7M_A9TR40`
- 实测违反态 WNS：`-0.1240890 ns`
- 违反态负 endpoint：仅 `u_exp/exp_sft_13_reg_1_/D`
- 路径构成：cell delay `97.1154%`，net delay `2.8846%`
- 修复：`u_exp/FE_OFC7405_n4112` 恢复为 `BUF_X2M_A9TR40`
- Replay：`slot1`、`slot3`，均 `PASS`
- 修复后：setup WNS/TNS=`+0.051/0 ns`，hold WNS=`+0.050 ns`
- 物理结果：max transition/capacitance/fanout=`0/0/431`；DRC `6953 -> 6952`；connectivity `3 -> 0`；routing mutation=`0`
- 功能等价与 placement legal：两个 replay 均通过

该题实测值位于 `[-138, -102] ps` 窗口内。候选选择经过 raw 筛选和 fresh-refine，再由正式 calibration 与双 replay 独立确认。

### 4.5 B1_CASE_005

- 证据来源：`work/batch100-relaxed-v3-r1`
- 场景：`pipeline` 层级、单关键锥、capture 近端组合逻辑 resize，I0 直接逆操作允许
- 目标 endpoint：`pp_out_l0n11_0_d1_reg_3_/D`
- 注入：`u_tree_l0n11/U705`，`XNOR2_X3M_A9TR40 -> XNOR2_X0P5M_A9TR40`
- 实测违反态 WNS：`-0.1358390 ns`
- 违反态负 endpoint：仅 `pp_out_l0n11_0_d1_reg_3_/D`
- 路径构成：cell delay `99.8022%`，net delay `0.1978%`
- 修复：`u_tree_l0n11/U705` 恢复为 `XNOR2_X3M_A9TR40`
- Replay：`slot0`、`slot2`，均 `PASS`
- 修复后：setup WNS/TNS=`+0.051/0 ns`，hold WNS=`+0.050 ns`
- 物理结果：max transition `2 -> 0`，max capacitance=`0`，max fanout=`431`；DRC `6958 -> 6952`；connectivity `9 -> 0`；routing mutation=`0`
- 功能等价与 placement legal：两个 replay 均通过

该题实测值位于 `[-161, -119] ps` 窗口内，且两个 replay 的 timing 与物理计数一致。

## 5. 共性观察

1. 五题的 cell delay fraction 为 `96.38%`～`99.85%`，net delay fraction 为 `0.15%`～`3.62%`，明显满足 `cell >= 70%`、`net <= 30%` 的 G0 准入条件。
2. 每题违反态均只有一个指定负 endpoint；checkpoint portability 检查和两个 replay 均复现相同负 endpoint 集合。
3. 所有 repair 都是功能等价 RVT drive 恢复，没有新增实例、连接修改、约束修改或 routing mutation。
4. 所有 replay 在 legalization 后恢复到基线 setup WNS `+0.051 ns`、TNS `0 ns`；hold WNS 保持 `+0.050 ns`。
5. DRC 最终计数统一回到基线 `6952`，connectivity 最终为 `0`，placement 均合法；这里的门禁是相对基线无回归，不应把设计原有 DRC 总数误写为零。
6. 002～005 说明 relaxed-v3 的核心方向合理：保留原始 severity curriculum，同时用一个 endpoint-local 等价 resize 降低构造离散性。

## 6. 证据索引

- `B1_CASE_001`：
  - `work/manual_v1/B1_CASE_001/cases/B1_CASE_001/validation.json`
  - `work/manual_v1/B1_CASE_001/cases/B1_CASE_001/metrics.json`
  - `work/manual_v1/B1_CASE_001/state/B1_CASE_001.json`
- `B1_CASE_002`～`B1_CASE_005`：
  - `work/batch100-relaxed-v3-r1/cases/<case-id>/validation.json`
  - `work/batch100-relaxed-v3-r1/cases/<case-id>/metrics.json`
  - `work/batch100-relaxed-v3-r1/state/<case-id>.json`
- relaxed-v3 分类迁移审计：
  - `work/batch100-relaxed-v3-r1/relaxed_v3_reclassification_audit.json`
- relaxed-v3 题卡：
  - `RELAXED_V3.md`
  - `case_specs_relaxed_v3.json`

## 7. 适用范围

本报告仅覆盖 `B1_CASE_001`～`B1_CASE_005`。结果属于 mock training、non-signoff 数据；当前未形成已 finalize 的 100-case 数据集，也不能外推为其余 95 题已经可达或通过。
