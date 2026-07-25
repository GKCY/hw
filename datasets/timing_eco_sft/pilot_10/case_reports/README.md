# Pilot-10 Timing ECO SFT 报告索引

[`cases/`](cases/) 子目录包含 10 份逐 case 审核报告：4 条 setup、4 条 hold、2 条 mixed；本目录另收录 Claude Code + GLM-5.2 benchmark 汇总与 MIXED_001 session 复盘。逐 case 报告均嵌入完整 instruction、规范 Probe/注入入口 Tcl、冻结注入动作展开、完整 Gold fix Tcl、设计初始状态、命令说明以及 Innovus/PrimeTime 实测结果。

> 注意：报告包含隐藏注入 oracle，仅用于数据设计与质量审核。SFT 训练应使用 [dataset.jsonl](../dataset.jsonl)，不要把这些报告直接作为训练消息。

## 可执行 PASS 标准

官方通过标准不是“候选 Tcl 与 Gold Tcl 相同”，而是候选修复在同一冻结输入上通过全部结果 gate。
权威定义见 [BENCHMARK_PASS_CRITERIA.md](../BENCHMARK_PASS_CRITERIA.md)，机器合同见
[benchmark_pass_criteria.json](../benchmark_pass_criteria.json)，判定程序见
[check_benchmark_pass.py](../../tools/check_benchmark_pass.py)。

官方 profile 要求双向 Innovus WNS ≥ +0.010 ns、TNS=0，DRV/逐类别 DRC 无回归、connectivity=0、
约束与功能审计通过、关键 checkDesign 计数为 0、两次 replay 确定一致，并通过独立 PrimeTime
setup/max 与 hold/min 复核。任一 hard gate 失败即 FAIL。

## 公共基线

- Top：`NV_NVDLA_CMAC_CORE_mac`；Innovus `21.10-p004_1`；post-route MMMC。
- Setup：`functional_setup_ss`，初始 WNS/TNS +0.051 / +0.000 ns。
- Hold：`functional_hold_ff`，初始 WNS/TNS +0.050 / +0.000 ns。
- Baseline：`QUALIFIED_CANDIDATE` / `derived_non_signoff` / `signoff_eligible=false`。
- 十个 case 均通过两次确定性 Innovus replay、约束不变审计、功能审计、物理 no-regression gate 和 PrimeTime crosscheck。

## Case 汇总

| Case | 类型 / 难度 | 注入后 setup WNS | 注入后 hold WNS | 修复后 setup WNS | 修复后 hold WNS | 设计修改 | 报告 |
|---|---|---:|---:|---:|---:|---:|---|
| `SETUP_001` | setup / easy | -0.042 ns | +0.050 ns | +0.015 ns | +0.050 ns | 2 | [SETUP_001](cases/SETUP_001.md) |
| `SETUP_002` | setup / medium | -0.098 ns | +0.050 ns | +0.027 ns | +0.050 ns | 4 | [SETUP_002](cases/SETUP_002.md) |
| `SETUP_003` | setup / hard | -0.203 ns | +0.050 ns | +0.051 ns | +0.050 ns | 6 | [SETUP_003](cases/SETUP_003.md) |
| `SETUP_004` | setup / medium | -0.096 ns | +0.050 ns | +0.051 ns | +0.050 ns | 8 | [SETUP_004](cases/SETUP_004.md) |
| `HOLD_001` | hold / easy | +0.051 ns | -0.032 ns | +0.051 ns | +0.050 ns | 1 | [HOLD_001](cases/HOLD_001.md) |
| `HOLD_002` | hold / medium | +0.051 ns | -0.065 ns | +0.051 ns | +0.050 ns | 2 | [HOLD_002](cases/HOLD_002.md) |
| `HOLD_003` | hold / hard | +0.051 ns | -0.119 ns | +0.051 ns | +0.050 ns | 2 | [HOLD_003](cases/HOLD_003.md) |
| `HOLD_004` | hold / medium | +0.052 ns | -0.072 ns | +0.052 ns | +0.050 ns | 2 | [HOLD_004](cases/HOLD_004.md) |
| `MIXED_001` | mixed / medium | -0.144 ns | -0.069 ns | +0.022 ns | +0.050 ns | 5 | [MIXED_001](cases/MIXED_001.md) |
| `MIXED_002` | mixed / hard | -0.385 ns | -0.103 ns | +0.051 ns | +0.050 ns | 11 | [MIXED_002](cases/MIXED_002.md) |

## Agent Benchmark 复盘

| 报告 | 内容 |
|---|---|
| [GLM52_PILOT10_SUMMARY_20260724](GLM52_PILOT10_SUMMARY_20260724.md) | Claude Code + GLM-5.2 完整 10 个 case 的结果、逐 case 耗时、失败分类和 Skill 演进 |
| [MIXED_001_REVIEW](MIXED_001_REVIEW.md) | MIXED_001 单 session 的逐阶段分析、完整工具步骤账本和耗时复盘 |

这两份报告记录历史 agent 行为和 evaluator 演进，其中包含 Gold/canonical
动作讨论，仅用于 benchmark 审核与复盘，不应拼接到被测 agent 的任务输入。

## 字段使用建议

- `instruction.txt` 是可进入 SFT user message 的任务描述，包含 post-injection 可观察证据和约束。
- `fix.tcl` 是 Gold 设计修改；`answer.txt` 中唯一的 Tcl fence 与它逐字规范化一致。
- `inject.tcl`、注入动作和 provenance 是隐藏 oracle，只用于 benchmark 构造、replay 与人工审核。
- `derived_non_signoff` 表示这批数据适合工具行为与 ECO 能力训练/评测，但不能对外宣称 foundry signoff。
