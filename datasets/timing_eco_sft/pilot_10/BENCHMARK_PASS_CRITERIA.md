# Pilot-10 RL / Benchmark 可验证通过标准

## 1. 唯一的官方 PASS 定义

候选修复不需要与 Gold `fix.tcl` 相同。只要候选 Tcl 在可信 harness 恢复出的同一份冻结
`violating.enc` 上执行，并且下面所有 hard gate 同时通过，就判为 `BENCHMARK_PASS`：

```text
BENCHMARK_PASS =
    start_state_match
  ∧ candidate_policy_pass
  ∧ innovus_setup_pass
  ∧ innovus_hold_pass
  ∧ drv_no_regression
  ∧ drc_per_category_no_regression
  ∧ connectivity_clean
  ∧ constraints_unchanged
  ∧ functional_audit_pass
  ∧ check_design_pass
  ∧ two_replays_deterministic
  ∧ primetime_setup_pass
  ∧ primetime_hold_pass
  ∧ evidence_integrity_pass
```

任一项失败即为 FAIL；不采用“总分足够高即可掩盖某个硬失败”的方式。

机器可读定义位于
[`benchmark_pass_criteria.json`](benchmark_pass_criteria.json)，可执行判定器位于
[`tools/check_benchmark_pass.py`](../tools/check_benchmark_pass.py)。

## 2. Hard gate 明细

| Gate | 可执行判定 |
|---|---|
| 起始状态 | result 的 case ID、类型、top、预算、before timing、DRV、DRC、connectivity、diagnostic context 和 before-SDC 必须与 reference case 匹配；timing 容差为 1 ps，其余计数/哈希精确相等。 |
| Candidate Tcl policy | 只允许白名单 ECO/增量物理命令；禁止 Tcl substitution、多命令行、`source`、文件/进程操作和任何约束/analysis-view 修改。 |
| ECO 预算 | `ecoChangeCell + ecoAddRepeater <= case.max_eco_cells`；不能靠扩大 cell 数获得通过。 |
| 对象可见性 | `ecoChangeCell -inst` 必须来自 instruction 对应 diagnostic `local_cells`；`ecoAddRepeater -term` 必须是 diagnostic endpoint。 |
| 命名 | 新实例必须为 `SFT_ECO_<CASE_ID>_<SETUP|HOLD>_<ordinal>`，ordinal 从 1 连续递增。 |
| Innovus setup | `functional_setup_ss`：WNS ≥ +0.010 ns，TNS = 0。 |
| Innovus hold | `functional_hold_ff`：WNS ≥ +0.010 ns，TNS = 0。 |
| DRV | max transition、max capacitance、max fanout 三类 after count 均不得大于 before。 |
| DRC | 不仅 total 不得增加；before/after 类别并集中的每个 category 均必须 `after <= before`，缺失类别按 0 计。 |
| Connectivity | after violations 必须为 0。 |
| Constraints | setup 与 hold 各自的 before/after SDC 在只归一化唯一时间戳头后必须逐字节一致。 |
| 功能完整性 | 所有 cell replacement/repeater 必须有 Liberty Boolean/non-inverting/成对反相证明；功能审计的 11 个 typed check 全部通过。 |
| checkDesign | missing LEF、missing timing、multiple-driver、Verilog multiple-driver、unplaced I/O 五个关键计数均为 0。 |
| Replay | 官方 profile 从同一冻结输入独立执行 2 次；timing 差值 ≤ 1 ps，semantic evidence、functional audit、injection provenance、success marker 一致。 |
| PrimeTime | `R-2020.09-SP4` 独立读取 post-ECO netlist、对应 SPEF、传播时钟；setup/max 与 hold/min 均 WNS ≥ +0.010 ns、TNS = 0、violating paths = 0。 |
| Evidence | 所有被消费的报告必须由 trusted harness 生成，路径受 case root 限制，并由 manifest 的 byte count 与 SHA256 绑定。 |

数值判定使用有限数检查，拒绝 NaN/Inf。TNS 的机器容差为 `1e-12 ns`；报告显示时仍写作
`0.000 ns`。

## 3. RL fast profile 与官方 Benchmark profile

### `rl_fast`

用于训练内环：执行一次 fresh Innovus replay，并执行候选 policy、起始状态、双向 timing、DRV、
逐类别 DRC、connectivity、constraint、functional audit 与 checkDesign gate。通过时输出
`RL_FEASIBLE`，binary reward 为 1；失败为 0。

它不要求第二次 replay 和 PrimeTime，因此不能计为官方 benchmark PASS。可以额外使用 WNS/TNS、
ECO action 数、DRV/DRC delta 组成 dense reward，但这些 shaping 分数不得覆盖 hard gate。

### `official`

用于最终评测：包含 `rl_fast` 的全部 gate，再强制两次确定性 Innovus replay和独立 PrimeTime
setup/hold crosscheck。只有该 profile 可以输出 `BENCHMARK_PASS`。

## 4. Trust boundary 与防作弊

候选 agent 只能提交 `fix.tcl`。恢复 checkpoint、source candidate、生成报告、解析指标、写 manifest、
执行第二次 replay 和 PrimeTime 都必须由 evaluator 控制。候选 Tcl 无权写 `metrics.json`、修改报告或
制造 PASS marker。

因此“手工编辑一个 WNS=+0.1 的 JSON”不构成证据；本地修改 metrics 后即使字段看起来通过，artifact
hash gate 也应失败。生产 benchmark 还应在隔离 VM 中运行候选脚本，并设置 wall-time、CPU、内存和
license timeout；进程异常、超时或缺少报告均 fail closed。

## 5. 执行方式

验证当前 Gold case 自身：

```sh
python3 tools/check_benchmark_pass.py \
  --reference-case pilot_10/cases/SETUP_001 \
  --result-dir pilot_10/cases/SETUP_001 \
  --profile official
```

验证任意候选 result bundle：

```sh
python3 tools/check_benchmark_pass.py \
  --reference-case pilot_10/cases/SETUP_001 \
  --result-dir /path/to/trusted_candidate_result/SETUP_001 \
  --profile official \
  --output /path/to/acceptance.json
```

判定器输出结构化 gate 列表、失败原因、观测 timing/physical 指标和 binary reward。退出码为：PASS=0、
hard-gate FAIL=1、bundle 缺失/格式错误/不可信=2。

## 6. 使用边界

这批数据的技术分类是 `derived_non_signoff`。这里的 `BENCHMARK_PASS` 表示在冻结工具、库、RC、MMMC
和策略合同下通过 timing ECO benchmark，不等同于 foundry signoff，也不能据此宣称流片签核完成。
