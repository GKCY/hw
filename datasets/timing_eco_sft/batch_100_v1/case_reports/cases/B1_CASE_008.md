# B1_CASE_008 Timing ECO SFT 构造尝试报告（未成题）

## 1. Case 定位

| 字段 | 内容 |
|---|---|
| 类型 / 难度 | Setup / E |
| 形态 / 策略 | single_cone / A |
| relaxed-v3 候选层级 | pipeline |
| 注入 / 修复策略 | endpoint-local RVT downsize / exact inverse restore |
| ECO 修改预算 | relaxed-v3 严格 1 个组合逻辑 RVT cell |
| 目标窗口 | [-0.0805, -0.0595] ns，即 -70 +/-10.5 ps |
| 当前状态 | PROBE_ELIGIBLE；未找到满足全部门禁的候选 |
| 证据来源 | work/batch100-relaxed-v3-r2 |

本文件是失败搜索记录，不是可训练 SFT case。当前没有 violating checkpoint、instruction、Gold fix、双 replay 或 VALIDATED 结论。

## 2. Design 与构造合同

- Top：NV_NVDLA_CMAC_CORE_mac；工具：Innovus 21.10-p004_1。
- Setup/Hold view：functional_setup_ss / functional_hold_ff。
- 合格基线 setup WNS/TNS=+0.051/+0.000 ns；hold WNS/TNS=+0.050/+0.000 ns。
- 基线 entry SHA256：d554d549384bd48b65f270524d82a375f0bee0039db3c0be80a32ab46df19458。
- 必须只有一个负 endpoint，且该 endpoint 的路径携带 pipeline hierarchy group。
- 必须保持实例集合、pin-net 拓扑、约束、时钟和路由不变；修复后还需通过双 VM replay、物理 no-regression 和严格 validator。

## 3. 首轮确定性绑定及拒绝结果

首轮 relaxed-v3 自动绑定为：

| endpoint | 实例 | 基线 cell | 注入尝试 |
|---|---|---|---|
| pp_out_l0n00_0_d1_reg_20_/D | U24777 | AO1B2_X1M_A9TR40 | AO1B2_X0P7M_A9TR40，随后尝试 X0P5M |

目标 endpoint 的合格基线 slack 为 +0.231134 ns。两级实际注入测得该 endpoint 分别仍为 +0.210615 ns 和 +0.197776 ns，没有形成目标 setup 违例；calibration/rejections.json 将该候选记为 exact-set/severity miss，状态由 CALIBRATING 退回 PROBE_ELIGIBLE。

## 4. 已尝试的搜索路径

| 搜索路线 | 代表证据目录 | 结果 |
|---|---|---|
| pipeline 宽表 raw 枚举 | manual_candidates/case008_pipeline_full_v82、case008_pipeline_full20k_v84 | 扩展到 2k/20k raw 候选，raw 近窗值在物理阶段没有复现为可接受结果 |
| 低 slack、alternate path 与 reachability | case008_pipeline_low_slack_v28、alternate_raw_case008_v147、alternate_local_case008_v150、manual_reachability_probe | 覆盖原 frozen paths 之外的候选和 endpoint-local 可达性；只有 raw 近窗记录 |
| isolated / endpoint-local 搜索 | pipeline_isolated_pool_v166、pipeline_isolated_search_v169、p008_iso_v172 系列 | 执行 isolated physical-target 与 singleton 检查，未命中严格窗口 |
| flavor、模拟档位、物理邻居与空间 | analog_case008_v128b、analog_union_case008_v140b、physical_neighbors_case008_v135b、spatial_pipe_u209_v213b | 覆盖 A/B/M flavor、相邻 drive、物理邻居和空间敏感候选，未形成权威命中 |
| 敏感度与模型排序 | sensitive_pipe_pool_v215b、modeled_pipe_v264、rawtarget_pipe_v255 | 模型仅用于排序；物理结果多次相对 raw 预测换号或大幅漂移 |
| capture-near 覆盖 | capture_pipe_pool_v270、capture_pipe_fresh_v271、capture_pipe_fresh_both_v284、capture_pipe_unseen_v283 | 主池 608 个 operation / 286 个实例；截至停止时 284 个 operation 有物理证据，324 个仍无物理证据 |
| 最后一轮未物理池 | capture_pipe_unphysical_v292、manual_screen/unphys_v293_s0...s7 | 配置了 8 个 25-row shard，共 200 个 operation；停止时只有 screen_config.tcl，没有生成 screen.tsv |

上述池存在重叠，数量不得相加。按当前同基线 manual_screen 和 frozen probe 的 pipeline group 重新汇总，共见到 19,502 行、18,157 个不同 operation；其中 626 个 operation 有某种物理结果。该统计是搜索足迹，不表示所有 operation 都同时满足未占用 fingerprint、endpoint-local 和权威 singleton 门禁。

## 5. 窗口附近的关键证据

- 在 [-0.0805, -0.0595] ns 内找到的 17 行全部来自 raw alternate-path 筛选，negative_count=-1；没有物理 authoritative 命中。
- 最近的已校准精确负结果之一是 u_tree_l0n11/U237：XOR2_X3M -> XOR2_X0P5M，实测 -0.0902176 ns，比窗口下界多负 0.0097176 ns，且该 operation 已被 VALIDATED 的 B1_CASE_009 占用。
- physical-target 多行模式曾给出 u_tree_l0n11/U705 的 -0.0393472 ns，但同 operation 的独立 singleton/full-global 结果为 -0.135839 ns。这个差异证明多行或 target-only 值只能用于排序，不能用于接受。
- 最后一轮 200-operation 队列尚未产出结果，因此不能声称 pipeline 候选宇宙已穷尽。

因此，008 的当前结论是未找到，而不是证明不存在可行候选。

## 6. 停止点

停止时没有可物化的 binding：

- injection_sha256、repair_sha256 和 replay slot 均为空；
- 没有 cases/B1_CASE_008 目录；
- unphys_v293_s0...s7 已写入候选配置但没有 screen.tsv，属于可恢复的中断队列；
- 恢复前应把 r1/r2 同基线 evidence 按 endpoint、instance、baseline_ref、new_ref 精确去重，再只调度真正未物理覆盖的 operation。

## 7. 证据入口

- [state.json](../../work/batch100-relaxed-v3-r2/state/B1_CASE_008.json)
- [binding.json](../../work/batch100-relaxed-v3-r2/bindings/B1_CASE_008.json)
- [calibration rejection](../../work/batch100-relaxed-v3-r2/jobs/B1_CASE_008/calibration/rejections.json)
- [第一次注入 slacks](../../work/batch100-relaxed-v3-r2/jobs/B1_CASE_008/calibration/candidate_00/001_injection/fetch/001_injection/injection_slacks.tsv)
- [第二次注入 slacks](../../work/batch100-relaxed-v3-r2/jobs/B1_CASE_008/calibration/candidate_00/002_injection/fetch/002_injection/injection_slacks.tsv)
- [608-operation capture pool](../../work/batch100-relaxed-v3-r2/manual_candidates/capture_pipe_pool_v270)
- [最后一轮 200-operation pool](../../work/batch100-relaxed-v3-r2/manual_candidates/capture_pipe_unphysical_v292)
- [中断 shard 示例](../../work/batch100-relaxed-v3-r2/manual_screen/unphys_v293_s0/screen_config.tcl)
- [manual screen evidence](../../work/batch100-relaxed-v3-r2/manual_screen)

## 8. SFT / Benchmark 使用边界

不得把本记录包装成成功 case，也不得把 raw window hit 或 multi-row physical 值当作注入 oracle。只有新候选完成精确 singleton、校准、checkpoint 冻结、两个独立 replay 和严格验证后，才可替换本记录中的未成题状态。
