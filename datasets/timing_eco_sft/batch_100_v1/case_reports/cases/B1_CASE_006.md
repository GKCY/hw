# B1_CASE_006 Timing ECO SFT 构造尝试报告（未成题）

## 1. Case 定位

| 字段 | 内容 |
|---|---|
| 类型 / 难度 | Setup / E |
| 形态 / 策略 | single_cone / A |
| relaxed-v3 候选层级 | top_tree |
| 注入 / 修复策略 | endpoint-local RVT downsize / exact inverse restore |
| ECO 修改预算 | relaxed-v3 严格 1 个组合逻辑 RVT cell |
| 目标窗口 | [-0.184, -0.136] ns，即 -160 +/-24 ps |
| 当前状态 | PROBE_ELIGIBLE；未找到满足全部门禁的候选 |
| 证据来源 | work/batch100-relaxed-v3-r2 |

本文件是失败搜索记录，不是可训练 SFT case。当前没有 violating checkpoint、instruction、Gold fix、双 replay 或 VALIDATED 结论。

## 2. Design 与构造合同

- Top：NV_NVDLA_CMAC_CORE_mac；工具：Innovus 21.10-p004_1。
- Setup/Hold view：functional_setup_ss / functional_hold_ff。
- 合格基线 setup WNS/TNS=+0.051/+0.000 ns；hold WNS/TNS=+0.050/+0.000 ns。
- 基线 entry SHA256：d554d549384bd48b65f270524d82a375f0bee0039db3c0be80a32ab46df19458。
- 必须只有一个负 endpoint，且该 endpoint 的路径携带 top_tree hierarchy group。
- 必须保持实例集合、pin-net 拓扑、约束、时钟和路由不变；修复后还需通过双 VM replay、物理 no-regression 和严格 validator。

## 3. 首轮确定性绑定及拒绝结果

首轮 relaxed-v3 自动绑定为：

| endpoint | 实例 | 基线 cell | 注入尝试 |
|---|---|---|---|
| pp_out_l0n00_1_d1_reg_25_/D | U10567 | NAND2_X1B_A9TR40 | NAND2_X0P7B_A9TR40，随后尝试 X0P5B |

目标 endpoint 的合格基线 slack 为 +0.126980 ns。两级实际注入测得该 endpoint 分别仍为 +0.0726104 ns 和 +0.284804 ns，没有形成目标 setup 违例；calibration/rejections.json 将该候选记为 exact-set/severity miss，状态由 CALIBRATING 退回 PROBE_ELIGIBLE。

## 4. 已尝试的搜索路径

| 搜索路线 | 代表证据目录 | 结果 |
|---|---|---|
| top_tree 宽表 raw 枚举 | manual_candidates/case006_top_tree_full_v82、case006_top_tree_full20k_v84 | 扩展到 2k/20k raw 候选，没有形成可接受物理命中 |
| 低基线、deep、near-window 穷举 | lowbase_top_tree_exhaustive_raw_v109a、top_tree_deep_raw_v107、top_tree_near_exhaustive_raw_v116、top_tree_near2_exhaustive_raw_v118 | 作为电气排序证据；raw 命中不能替代物理结果 |
| isolated / endpoint-local 搜索 | top_tree_isolated_pool_v168、top_tree_exclusive_isolated_pool_v170、l0n11_isolated_pool_v173 | 对单 endpoint、单实例和共享层级约束做筛选并执行物理/单例复核，未命中严格窗口 |
| cell flavor 与模拟档位 | analog_case006_v128a、analog_union_case006_v140a、physical_neighbors_case006_v135a | 覆盖 A/B/M flavor、相邻 drive 和物理邻居，未得到可接受 singleton |
| 空间、驱动比和敏感度排序 | tree_high_ratio_pool_v196、tree_high_sensitivity_pool_v199、sensitive_top_pool_v215a、modeled_top_v265 | 仅用于排序；测得模型误差与目标窗口同量级，不能据此排除候选 |
| capture-near 新池 | capture_top_pool_v280、capture_top_fresh_v281 | 最后生成 60 个 operation / 36 个独立实例的新池；停止时尚无物理 screen 结果 |

这些池之间大量重叠，表中数量不得相加。按当前同基线 manual_screen 和 frozen probe 的 top_tree group 重新汇总，共见到 23,058 行、21,465 个不同的 endpoint/instance/ref operation；其中 822 个 operation 有某种物理结果。该统计是搜索足迹，不表示所有 operation 都同时满足未占用 fingerprint、endpoint-local 和权威 singleton 门禁。

## 5. 窗口附近的关键证据

- 在 [-0.184, -0.136] ns 内找到的 19 行均为 raw-only 记录，negative_count=-1；没有权威 singleton 命中。
- u_tree_l0n11/U237 的 XOR2_X3M -> XOR2_X0P7M raw 值为 -0.171047 ns，看似在窗内，但同实例更弱档的真实校准结果与 raw 预测发生大幅偏移；该实例最终被 B1_CASE_009 使用，不能复用。
- 最近的精确 singleton 是 u_tree_l0n11/U705：XNOR2_X3M -> XNOR2_X0P5M，实测 -0.135839 ns。它比窗口上界少负 0.000161 ns，严格越界，且同基线 operation 已被 VALIDATED 的 B1_CASE_005 占用。
- 另一精确 singleton 为 u_tree_l0n11/U657：XOR2_X3M -> XOR2_X0P5M，实测 -0.300047 ns，明显过深。

因此，006 的困难不是没有产生负 slack，而是没有找到同时满足严格窗口、唯一负 endpoint、top_tree、未占用 fingerprint 和权威 singleton 条件的 operation。

## 6. 停止点

停止时没有可物化的 binding：

- injection_sha256、repair_sha256 和 replay slot 均为空；
- 没有 cases/B1_CASE_006 目录；
- capture_top_fresh_v281 的 36 个独立实例仍是后续可恢复的未物理覆盖方向；
- 恢复搜索前必须先按同基线 exact operation 重建 evidence registry，避免重复已经测过的物理候选。

## 7. 证据入口

- [state.json](../../work/batch100-relaxed-v3-r2/state/B1_CASE_006.json)
- [binding.json](../../work/batch100-relaxed-v3-r2/bindings/B1_CASE_006.json)
- [calibration rejection](../../work/batch100-relaxed-v3-r2/jobs/B1_CASE_006/calibration/rejections.json)
- [第一次注入 slacks](../../work/batch100-relaxed-v3-r2/jobs/B1_CASE_006/calibration/candidate_00/001_injection/fetch/001_injection/injection_slacks.tsv)
- [第二次注入 slacks](../../work/batch100-relaxed-v3-r2/jobs/B1_CASE_006/calibration/candidate_00/002_injection/fetch/002_injection/injection_slacks.tsv)
- [frozen probe paths](../../work/batch100-relaxed-v3-r2/probe/paths.tsv)
- [manual candidate evidence](../../work/batch100-relaxed-v3-r2/manual_candidates)
- [manual screen evidence](../../work/batch100-relaxed-v3-r2/manual_screen)

## 8. SFT / Benchmark 使用边界

不得把本记录包装成成功 case，也不得从 raw 近窗值反推 Gold fix。只有新候选完成精确 singleton、校准、checkpoint 冻结、两个独立 replay 和严格验证后，才可替换本记录中的未成题状态。
