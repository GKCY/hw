# B1_CASE_014 Timing ECO SFT 构造尝试报告（未成题）

## 1. Case 定位

| 字段 | 内容 |
|---|---|
| 类型 / 难度 | Setup / M |
| 形态 / 策略 | single_cone / A |
| relaxed-v3 候选层级 | pipeline |
| 注入 / 修复策略 | endpoint-local RVT downsize / exact inverse restore |
| ECO 修改预算 | relaxed-v3 严格 1 个组合逻辑 RVT cell |
| 目标窗口 | `[-0.207, -0.153] ns`，即 `-180 +/-27 ps` |
| 当前状态 | `PLANNED`；未找到满足全部门禁的候选 |
| 证据来源 | `work/batch100-relaxed-v3-r2` |

本文件是失败搜索记录，不是可训练 SFT case。当前没有 violating checkpoint、instruction、Gold fix、双 replay 或 `VALIDATED` 结论。

## 2. Design 与构造合同

- Top：`NV_NVDLA_CMAC_CORE_mac`；工具：Innovus 21.10-p004_1。
- Setup/Hold view：`functional_setup_ss` / `functional_hold_ff`。
- 合格基线 setup WNS/TNS=`+0.051/+0.000 ns`；hold WNS/TNS=`+0.050/+0.000 ns`。
- 基线 entry SHA256：`d554d549384bd48b65f270524d82a375f0bee0039db3c0be80a32ab46df19458`。
- 必须只有一个负 endpoint，且路径满足 pipeline hierarchy group。
- 必须保持实例集合、pin-net 拓扑、约束、时钟和路由不变；修复后还需通过双 VM replay、物理 no-regression 和严格 validator。

## 3. 自动规划候选及拒绝结果

规划 binding 为：

| endpoint | 实例 | 基线 cell | 规划注入 |
|---|---|---|---|
| `pp_out_l0n00_0_d1_reg_24_/D` | `u_tree_l0n00/U647` | `XNOR2_X2M_A9TR40` | `XNOR2_X1P4M_A9TR40` |

真实物理证据与规划估计方向不一致：

- `X1P4M` physical-target 为 `+0.181852 ns`；
- 更弱 `X0P7M` 为 `+0.458829 ns`；
- 最弱 `X0P5M` 的独立结果仍为 `+0.313723 ns`，`negative_count=0`。

因此该实例不能形成目标窗口内违例，未进入正式 `BOUND`；state 中 binding/injection/repair 哈希仍为空。

## 4. 已尝试的搜索路径

| 搜索路线 | 代表证据目录 | 结果 |
|---|---|---|
| 复用 pipeline 历史 raw / physical registry | `case006_008_raw_v2_retry`、`case006_fresh_v3`、`pipeline_isolated_v167_*` | raw 近窗值在真实物理结果中漂移，或 exact operation 已被其他 case 占用 |
| 高风险门族 fresh 池 | `case014_riskfam_fresh_v400` | 160 行候选中的一个 20 行分片完成；全部为正 slack，最小 `+0.395957 ns` |
| l0n11/l0n13 定向池 | `case014_l11l13_risk_v406` | 60 行候选中完成 8 行整分片，并在停止前保存另一个分片 1 行；9 行均为正 slack，最小 `+0.492201 ns` |
| XOR/XNOR/OAI21 高风险门族 | `c1215pt_v401_piperisk7`、`c1215pt_v411_pipel11l13_1` | 没有产生负 slack，更没有严格窗口命中 |

最后一轮明确归入 014 的 pipeline 定向搜索保存 29 行物理结果；跨池是否重叠仍需在恢复时按 exact operation key 去重。

## 5. 窗口附近的关键证据

| operation | 证据 | 结果 | 判定 |
|---|---|---:|---|
| `u_tree_l0n11/U237 XOR2_X3M -> X0P5M` | raw / singleton | `-0.171047 / -0.0902176 ns` | raw 在 014 窗内，但 singleton 明显过浅，且已由 `B1_CASE_009` 占用 |
| `u_tree_l0n11/U705 XNOR2_X3M -> X0P5M` | authoritative singleton | `-0.135839 ns` | 比 014 上界少负 `17.161 ps`，且已由 `B1_CASE_011` 占用 |
| `u_tree_l0n11/U123 XNOR2_X3M -> X0P7M` | physical | `-0.0967464 ns` | 过浅 |
| `u_tree_l0n11/U657 XOR2_X3M -> X0P5M` | singleton | `-0.300047 ns` | 过深 |
| `u_tree_l0n11/U123 XNOR2_X3M -> X0P5M` | singleton | `-0.772835 ns` | 过深；drive ladder 跨过目标窗口 |

014 的目标窗口宽 54 ps，但已测 operation 仍呈现“正 slack / 过浅”和“明显过深”两簇，没有未占用的严格窗口命中。

## 6. 停止点

- state 仍为 `PLANNED`，没有正式 binding 或 case 目录；
- `case014_riskfam_fresh_v400` 只完成 20/160 行；
- `case014_l11l13_risk_v406` 保存 9/60 行物理结果；
- 中断目录 `c1215pt_v415_pipel11l13_4` 的 1 行结果已同步；
- 恢复前必须把 29 行结果并入跨 run exact-key registry，不能直接重跑旧 shard。

## 7. 证据入口

- [state.json](../../work/batch100-relaxed-v3-r2/state/B1_CASE_014.json)
- [binding.json](../../work/batch100-relaxed-v3-r2/bindings/B1_CASE_014.json)
- [规划实例 physical-target](../../work/batch100-relaxed-v3-r2/manual_screen/c1115pt_v307_p0/screen.tsv)
- [规划实例 X0P5M singleton](../../work/batch100-relaxed-v3-r2/manual_screen/case006_008_single_u647_x05_v1/screen.tsv)
- [fresh risk-family pool](../../work/batch100-relaxed-v3-r2/manual_candidates/case014_riskfam_fresh_v400)
- [l0n11/l0n13 pool](../../work/batch100-relaxed-v3-r2/manual_candidates/case014_l11l13_risk_v406)
- [完成的 high-risk 分片](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v401_piperisk7/screen.tsv)
- [完成的 l0n11/l0n13 分片](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v411_pipel11l13_1/screen.tsv)
- [中断分片](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v415_pipel11l13_4/screen.tsv)

## 8. SFT / Benchmark 使用边界

不得把本记录包装成成功 case，也不得从 raw 近窗值反推 Gold fix。只有新的未占用候选完成 fresh full-global singleton、正式校准、checkpoint 冻结、两个独立 replay 和严格验证后，才可替换本记录中的未成题状态。
