# Claude Code + GLM-5.2：Pilot-10 Timing ECO Benchmark 复盘

日期：2026-07-24

Outcome 口径复核：2026-07-25

执行方式：每个 case 仅启动一个 Claude Code session，session 内允许无限 turn；由 runner 将 TASK 与通用 Timing ECO Skill 拼接后注入。

计时口径：`Claude wall` 是 runner 记录的单个 Claude Code 进程墙钟时间；官方验证时间不包含在该值内。

`SETUP_001` 先作为单题运行；确认 runner 和 Skill 后，再运行剩余 9 题。本文按同一计时与 outcome 口径汇总完整 10 题。

## 总结

- 当前 outcome-based benchmark 通过：7/10（70.0%）。
- 原始 runner 曾记录 4/10；另外 3 个因已删除的注释文本扫描或 Gold 精确动作检查被拒绝。2026-07-25 使用原有证据重新执行 finalizer 后，这 3 个均通过，无需重跑 EDA。
- Claude Code 总墙钟时间：13:43:50.144。
- Claude Code 平均每题：1:22:23.014。
- 官方验证总时间：6:39:58.808，平均每题 39:59.881。
- 合计 722 个模型内部 turn、674 次 Bash、180 次 agent 侧 Innovus、56 次 agent 侧 PrimeTime。
- 全部 case 都遵守“一题一个 Claude Code 外层 session”，失败后没有启动第二个 Claude Code 重试。

## 逐 case 结果与耗时

| Case | Claude wall | 内部 turn | Bash | Agent Innovus/PT | 官方验证 | Outcome 判定 | 关键结果或失败原因 |
|---|---:|---:|---:|---:|---:|---|---|
| SETUP_001 | 1:32:47.872 | 58 | 55 | 19 / 6 | 40:01.217 | PASS | 1 个 `DLY2_X4M` 替换为 `BUF_X4M`；两次 replay、PT 和全部物理 gate 通过 |
| SETUP_002 | 1:08:53.730 | 58 | 56 | 20 / 9 | 47:16.307 | PASS | 4 个 `DLY2_X4M` 精确替换为 `BUF_X4M` |
| SETUP_003 | 0:52:46.033 | 78 | 62 | 10 / 4 | 39:15.048 | PASS | 两次 Innovus replay 均通过；旧 finalizer 的注释扫描误判已移除 |
| SETUP_004 | 1:02:13.735 | 80 | 77 | 18 / 8 | 35:44.867 | FAIL | timing 通过；SHORT 2683→2684，虽总 DRC 不变但单类别回退 |
| HOLD_001 | 2:15:43.976 | 103 | 101 | 23 / 5 | 38:13.561 | PASS | 精确位置插入一个 `BUF_X0P7M_A9TR40`；Innovus/PT、DRC、连接性均通过 |
| HOLD_002 | 0:57:03.904 | 87 | 82 | 13 / 5 | 35:07.800 | FAIL | timing 通过；DRC 总数下降，但 SHORT 2679→2680 |
| HOLD_003 | 2:04:35.552 | 67 | 63 | 22 / 6 | 34:36.807 | FAIL | 两级 hold delay 修复 timing；DRC 6952→6954，SHORT 增加 1 |
| HOLD_004 | 1:25:53.670 | 76 | 67 | 22 / 6 | 47:02.407 | PASS | 一个 `DLY4_X0P5M_A9TR40` 修复 hold；DRC 6931→6929，PT setup/hold 通过 |
| MIXED_001 | 1:20:13.477 | 62 | 60 | 18 / 4 | 40:32.771 | PASS | 两次真实 replay 全通过；`BUF_X4M` 是满足全部公开约束的等价工程解 |
| MIXED_002 | 1:03:38.195 | 53 | 51 | 15 / 3 | 42:08.023 | PASS | 两次真实 replay 全通过；旧 finalizer 的注释扫描误判已移除 |

## 失败分类

### 1. 物理 DRC 单类别回退：3 个

SETUP_004、HOLD_002、HOLD_003 都已修复 timing，但 SHORT 或 DRC 总数恶化。模型对 timing 候选的搜索能力尚可，主要不足是：

- 没有把每个 DRC 类别都作为硬约束；
- 在合法化/增量布线后缺少足够稳健的物理候选比较；
- 对 hold repeater 的 cell、级数、位置选择仍有较大试探性。

### 已删除的非 outcome 检查

以下 3 个原始失败不再计为 benchmark 失败。

#### 最终 Tcl 注释触发静态对象检查：2 个

SETUP_003 和 MIXED_002 的可执行命令使用了精确实例名，真实 EDA replay 也通过，但注释包含前缀、占位符或范围简写。扫描器把注释也视作交付物的一部分：

- `SFT_ECO_SETUP_003_`
- `SFT_ECO_MIXED_002_HOLD_<ordinal>`
- `SFT_ECO_MIXED_002_PATH_1..9`

注释不参与 Tcl 执行。当前检查只验证可执行命令中的对象，不再扫描注释和回答 prose。

#### 结果正确但动作不符合精确 canonical repair：1 个

MIXED_001 使用 4 个 `BUF_X4M` 后，setup/hold、DRV、DRC、连接性和 PT 均通过；隐藏 finalizer 要求的是精确 4 个 `BUF_X2M` restore，再追加 hold repeater。该失败不是工具使用错误，而是 benchmark 同时考核动作序列与结果。

当前 benchmark 不再要求复制唯一 Gold 动作。Gold 指纹只用于数据生成和校准，candidate 接受标准以公开约束和真实 EDA outcome 为准。

## 耗时观察

HOLD_004 是本轮对单 session 做过最细拆解的 case：

- Innovus：22 次，约 1:00:51.5，占 Claude wall 约 71%。
- GLM API：约 24:51.5，占约 29%。
- PrimeTime：6 次，约 1:10.6。
- 其他 Bash：约 3.1 秒。

Innovus 内最大的重复项是 DRV 命令发现和检查：

- DRV 相关 9 次：29:45.9。
- 其中 8 次语法/查询探索：21:47.8。
- 最终 DRV 验证：7:58.1。

这说明总耗时的核心不是文件操作，而是反复恢复 Innovus checkpoint，以及模型不熟悉精确 DRV API 后进行的命令发现。

## Skill 演进及实验边界

SETUP_001 单题运行使用的 Skill SHA256：

`48086dd914f9569cb56a63020787e4d7d1561e87dd5fa20e62577c47ccd7b037`

随后从 SETUP_002 到 HOLD_004 的 7 个 case 使用的 Skill SHA256：

`b912be8c3fd03c10d7645b161f02eacf8cd05834b5c6af56498b0fe7d1f344bd`

MIXED_001、MIXED_002 使用加入 Innovus 21.10 DRV 知识后的 Skill SHA256：

`a594b26359587f5f44e9f99d60028bc549212ce99411e471d91a64bc60507b82`

DRV 更新后，MIXED_001 首次使用以下命令即成功，不再出现 HOLD_004 的 8 轮 DRV 语法探索：

```tcl
foreach drv_type {max_transition max_capacitance max_fanout} {
    report_constraint \
        -drv_violation_type $drv_type \
        -view <setup_view> \
        -all_violators \
        -verbose
}
```

全部 benchmark 结束后，又把 PT 导出经验沉淀进 Skill：

1. post-route ECO 后在 `rcOut` 前执行 `extractRC`，避免 dirty in-memory parasitics 导致 `IMPEXT-7050`。

跑后 Skill SHA256：

`548cd917b8911fca1afad3947aeed96356c3885a48f2efcd90a8d19fab66719d`

该版本没有用于上述 10 个 case，因此不把它的潜在收益计入本次结果。

## 结论

Claude Code + GLM-5.2 已经能独立完成 restore、timing 诊断、候选 ECO、Innovus/PT 验证和最终 Tcl 交付，完整 Pilot-10 的 outcome-based benchmark 成绩为 7/10。当前主要短板不是“完全不会修 timing”，而是：

1. 物理 DRC 单类别的严谨收敛；
2. Innovus 冷启动和重复 restore 带来的长时间成本。

MIXED_001 的逐工具调用、逐步行为与耗时详见
[`MIXED_001_REVIEW.md`](MIXED_001_REVIEW.md)。
