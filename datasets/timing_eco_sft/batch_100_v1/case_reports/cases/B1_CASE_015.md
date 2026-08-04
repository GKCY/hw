# B1_CASE_015 Timing ECO SFT 构造尝试报告（未成题）

## 1. Case 定位

| 字段 | 内容 |
|---|---|
| 类型 / 难度 | Setup / M |
| 形态 / 策略 | single_cone / A |
| relaxed-v3 候选层级 | top_tree |
| 注入 / 修复策略 | endpoint-local RVT downsize / exact inverse restore |
| ECO 修改预算 | relaxed-v3 严格 1 个组合逻辑 RVT cell |
| 目标窗口 | `[-0.230, -0.170] ns`，即 `-200 +/-30 ps` |
| 当前状态 | `PLANNED`；未找到满足全部门禁的候选 |
| 证据来源 | `work/batch100-relaxed-v3-r2` |

本文件是失败搜索记录，不是可训练 SFT case。当前没有 violating checkpoint、instruction、Gold fix、双 replay 或 `VALIDATED` 结论。

## 2. Design 与构造合同

- Top：`NV_NVDLA_CMAC_CORE_mac`；工具：Innovus 21.10-p004_1。
- Setup/Hold view：`functional_setup_ss` / `functional_hold_ff`。
- 合格基线 setup WNS/TNS=`+0.051/+0.000 ns`；hold WNS/TNS=`+0.050/+0.000 ns`。
- 基线 entry SHA256：`d554d549384bd48b65f270524d82a375f0bee0039db3c0be80a32ab46df19458`。
- 必须只有一个负 endpoint，且路径满足 top_tree hierarchy group。
- 必须保持实例集合、pin-net 拓扑、约束、时钟和路由不变；修复后还需通过双 VM replay、物理 no-regression 和严格 validator。

## 3. 自动规划候选及拒绝结果

规划 binding 为：

| endpoint | 实例 | 基线 cell | 规划注入 |
|---|---|---|---|
| `pp_out_l0n08_0_d1_reg_23_/D` | `u_tree_l0n08/U159` | `XNOR2_X1P4M_A9TR40` | `XNOR2_X1M_A9TR40` |

已有真实物理证据显示：

- `X1M` 为 `+0.136072 ns`；
- `X0P7M` 为 `+0.57561 ns`；
- 最弱 `X0P5M` 的 physical-target 结果仍为 `+0.0998516 ns`。

三档均未形成 setup violation，因此该候选未进入正式 `BOUND`；state 中 binding/injection/repair 哈希仍为空。

## 4. 已尝试的搜索路径

| 搜索路线 | 代表证据目录 | 结果 |
|---|---|---|
| 复用 top_tree 历史 raw / physical registry | `case006_008_raw_v2_retry`、`case006008_actualraw_u657_x05m_v103a`、`physical_extract_check_u123_v96b` | 多个 raw 窗内值在真实物理结果中跳到窗口两侧 |
| 高风险门族 fresh 池 | `case012015_riskfam_fresh_v397` | 160 行候选中的首个 20 行分片完成；全部为正 slack，最小 `+0.293857 ns` |
| l0n11/l0n13 定向池 | `case012015_l11l13_risk_v405` | 60 行候选中保存 8+4+2 行物理结果；14 行全部为正 slack，最小 `+0.491064 ns` |
| 同层级联合搜索 | 与 `B1_CASE_012` 共用 top_tree 候选覆盖 | 没有产生可分配给 015 的未占用窗口命中 |

最后一轮明确归入 012/015 的 top_tree 定向搜索共保存 34 行物理结果；跨池可能重叠，恢复时仍需按 exact operation key 重新去重。

## 5. 窗口附近的关键证据

| operation | 证据 | 结果 | 判定 |
|---|---|---:|---|
| `u_tree_l0n11/U123 XNOR2_X3M -> X0P7M` | raw / physical | `-0.220999 / -0.0967464 ns` | raw 在 015 窗内，真实物理结果过浅 |
| `u_tree_l0n11/U657 XOR2_X3M -> X0P5M` | raw / singleton | `-0.220 / -0.300047 ns` | raw 在窗内，singleton 比下界多负 `70.047 ps` |
| `u_tree_l0n11/U237 XOR2_X3M -> X0P5M` | raw / singleton | `-0.171047 / -0.0902176 ns` | raw 贴近上界，真实 singleton 过浅，且已由 `B1_CASE_009` 占用 |
| `u_tree_l0n11/U705 XNOR2_X3M -> X0P5M` | authoritative singleton | `-0.135839 ns` | 比 015 上界少负 `34.161 ps`，且已由 `B1_CASE_011` 占用 |
| `u_tree_l0n11/U123 XNOR2_X3M -> X0P5M` | singleton | `-0.772835 ns` | 过深；与 `X0P7M` 之间没有命中窗口的合法档位 |

015 的主要困难是 raw 排序证据在 refinePlace/post-route 后发生大幅漂移，同时合法 drive ladder 在多个热点上直接跨过 60 ps 目标窗口。

## 6. 停止点

- state 仍为 `PLANNED`，没有正式 binding 或 case 目录；
- `case012015_riskfam_fresh_v397` 只完成 20/160 行；
- `case012015_l11l13_risk_v405` 保存 14/60 行物理结果；
- 中断目录 `c1215pt_v412_topl11l13_2` 和 `c1215pt_v414_topl11l13_3` 的部分结果已同步；
- 恢复前必须把 34 行结果并入跨 run exact-key registry，不能直接重跑旧 shard。

## 7. 证据入口

- [state.json](../../work/batch100-relaxed-v3-r2/state/B1_CASE_015.json)
- [binding.json](../../work/batch100-relaxed-v3-r2/bindings/B1_CASE_015.json)
- [规划实例 X1M 物理结果](../../work/batch100-relaxed-v3-r2/manual_screen/case006008_deep_u159_x1m_v108f/screen.tsv)
- [规划实例 X0P5M 物理结果](../../work/batch100-relaxed-v3-r2/manual_screen/u159_x05_iso_v204_s2/screen.tsv)
- [fresh risk-family pool](../../work/batch100-relaxed-v3-r2/manual_candidates/case012015_riskfam_fresh_v397)
- [l0n11/l0n13 pool](../../work/batch100-relaxed-v3-r2/manual_candidates/case012015_l11l13_risk_v405)
- [完成的 high-risk 分片](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v398_toprisk0/screen.tsv)
- [完成的 l0n11/l0n13 分片](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v409_topl11l13_0/screen.tsv)
- [中断分片 2](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v412_topl11l13_2/screen.tsv)
- [中断分片 3](../../work/batch100-relaxed-v3-r2/manual_screen/c1215pt_v414_topl11l13_3/screen.tsv)

## 8. SFT / Benchmark 使用边界

不得把本记录包装成成功 case，也不得从 raw 近窗值反推 Gold fix。只有新的未占用候选完成 fresh full-global singleton、正式校准、checkpoint 冻结、两个独立 replay 和严格验证后，才可替换本记录中的未成题状态。
