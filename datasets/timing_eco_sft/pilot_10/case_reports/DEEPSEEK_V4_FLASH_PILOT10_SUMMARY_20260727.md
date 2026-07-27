# Claude Code + deepseek-v4-flash：Pilot-10 Timing ECO Benchmark 复盘

日期：2026-07-27

执行方式：每个 case 只启动一个 Claude Code 外层 session；Claude Code 实际模型由
runner 记录为 `deepseek-v4-flash`。模型阶段最多 2 小时，trusted official
Innovus/PrimeTime 验证不计入该预算。

计分口径：候选必须通过两次 fresh Innovus replay、setup/hold、DRV、逐类别 DRC、
connectivity、约束/功能审计、确定性和独立 PrimeTime。任一硬门禁失败即记 FAIL。

## 结论

- 完整 10 case：**7/10，70.0%**。
- 本次最后 5 个执行：**3/5**。`HOLD_004`、`MIXED_001`、`MIXED_002`
  通过；`HOLD_003` 超时无交付；`SETUP_002` 的两次 Innovus replay 均成功，
  但 `fix.tcl` 最后缺少 LF，被 finalizer 的可移植格式门禁拒绝。
- `SETUP_002` 首次运行是 2 小时内未交付；按用户要求重测后已找到真实 EDA
  可行解，但仍因末尾换行格式失败。因此无论按首次运行还是以重测替换首次结果，
  总通过率都是 7/10。
- Claude Code 模型阶段总墙钟：`15:01:14.057`；平均每题
  `1:30:07.406`。
- trusted official 阶段已消耗 `6:02:08.383`。`HOLD_003` 没有候选，
  official 未启动；`SETUP_002` 在两次 Innovus replay 后、PrimeTime 前失败。
- 合计记录 514 个模型内部 turn、505 次 Bash、202 次 agent 侧 Innovus、
  29 次 agent 侧 PrimeTime。
- 对比 Claude Code + GLM-5.2 的 outcome 成绩也是 7/10。通过率相同，
  失败分布不同：GLM-5.2 在 `SETUP_004` 发生 DRC 回退；deepseek-v4-flash
  通过了该题，但在 `SETUP_002` 丢在交付格式门禁。

`HOLD_001` 在模型 2 小时硬上限正式启用前运行，墙钟为 3 小时 7 分；
它保留在 10 case 原始实验结果中。后续 case 均按 2 小时模型预算执行。

## 逐 case 结果

| Case | 模型 wall | 内部 turn | Bash | Agent Innovus/PT | Official wall | 判定 | 关键结果或失败原因 |
|---|---:|---:|---:|---:|---:|---|---|
| `SETUP_001` | 0:53:17.405 | 56 | 52 | 15 / 4 | 0:39:10.831 | PASS | 两次 replay、物理门禁和 PT 均通过 |
| `SETUP_002`（重测） | 1:30:06.838 | 45 | 39 | 23 / 5 | 0:46:23.683 | FAIL | 两次 Innovus replay 均通过；`fix.tcl` EOF 缺 LF，finalizer 拒绝，PT 未启动 |
| `SETUP_003` | 1:08:16.317 | 49 | 45 | 19 / 3 | 0:38:32.062 | PASS | 两次 replay、物理门禁和 PT 均通过 |
| `SETUP_004` | 0:56:59.617 | 35 | 33 | 15 / 5 | 0:35:23.909 | PASS | deepseek 修复通过全部 outcome gate |
| `HOLD_001` | 3:07:32.939 | 54 | 51 | 18 / 4 | 0:35:58.097 | PASS | 在硬上限启用前运行；最终全部通过 |
| `HOLD_002` | 1:26:52.906 | 67 | 61 | 25 / 0 | 0:34:46.560 | FAIL | timing 通过，DRC `6957 -> 6953`，但 `SHORT 2681 -> 2682`，逐类别回退 |
| `HOLD_003` | 1:59:53.272 | 0* | 36 | 28 / 0 | — | FAIL | 到 2 小时预算仍无 `fix.tcl`，official 未启动 |
| `HOLD_004` | 1:09:27.834 | 56 | 51 | 18 / 4 | 0:43:34.899 | PASS | 两次 replay、物理门禁和 PT 均通过 |
| `MIXED_001` | 1:17:56.352 | 69 | 60 | 17 / 4 | 0:43:07.478 | PASS | setup/hold、逐类别 DRC、连接性、双 replay、PT 全通过 |
| `MIXED_002` | 1:30:50.577 | 83 | 77 | 24 / 0 | 0:45:10.864 | PASS | agent 未完成可信 PT，但 trusted PT 与全部 gate 通过 |

\* `HOLD_003` 的中断 transcript 没有形成正常 usage 汇总，runner 把内部 turn
记为 0；36 次 Bash 和 28 次 Innovus 是独立计数器记录的可观测事实。

## 失败分类

### 1. 物理逐类别 DRC 回退：`HOLD_002`

两次 official replay 一致：

- setup WNS：`+0.051 ns`
- hold WNS：`-0.065 ns -> +0.050 ns`
- connectivity：`0`
- DRC total：`6957 -> 6953`
- SPACING：`4235 -> 4230`
- SHORT：`2681 -> 2682`

虽然总 DRC 减少 4，`SHORT` 增加 1，违反“每个类别均不得增加”的公开硬门禁。
这是真实物理结果失败，不是 evaluator 文本或 Gold 动作匹配问题。

### 2. 模型预算耗尽且无交付：`HOLD_003`

模型进行了 28 次 Innovus 调用，接近两小时上限仍未写出 `fix.tcl`。没有候选
SHA、没有 official replay。主要问题是候选和验证探索没有及时收敛，而不是
official 验证太慢。

### 3. 交付文件字节格式：`SETUP_002`

重测候选 SHA256：

`7c73c4152e81ae0cfe928cda7425ede5626cf64af433babc11c48daee6391d64`

两次 fresh Innovus replay 均 exit 0，并产生 `SFT_CASE_PASSED`。Replay 1 的
可观测结果为：

- setup WNS：`-0.098 ns -> +0.027 ns`
- hold WNS：`+0.050 ns -> +0.050 ns`
- DRC：`6950 -> 6950`，六个类别全部相同
- connectivity：`0`

但 381-byte `fix.tcl` 的最后 24 个字节以 `ecoRoute -target` 结束，没有
`0a`。`finalize_pilot.py::_validate_concrete_fix` 要求文本以 LF 结束，因而报：

```text
concrete fix must use LF and one nonblank first line
```

错误文案把三种格式条件合并在一起；本次真实触发条件是
`not text.endswith("\n")`，不是首行空白，也不是脚本只能有一行。由于 finalizer
在 PrimeTime 前失败，官方 PT 次数为 0。按现行硬门禁，此题仍必须记 FAIL。

## Skill 应用效果

最后 5 个 case 使用的 Timing ECO Skill SHA256：

`f95e601bd4f2fc8add6e8d0419acca08b9757bee18a706ba25f749a21662f312`

该版本补入了具有泛化性的 Innovus 21.10 DRC/连接性用法：

```tcl
verify_drc \
    -limit 1000000 \
    -report [file join $report_dir drc_after.rpt]
verifyConnectivity \
    -type all \
    -report [file join $report_dir connectivity_after.rpt]
```

同时明确要求：

- before/after 使用相同命令；
- DRC `-limit` 足够大，确认报告未被截断；
- 比较 before/after 类别并集，而不只比较 total；
- connectivity after 必须为 0；
- 报告必须存在且非空；
- 不使用不稳定的 marker 内部对象接口。

从 `HOLD_004`、`MIXED_001`、`MIXED_002` 和 `SETUP_002` 的 transcript
看，这两条命令都能在首次实际使用时成功，说明“正确命令形式”这一问题已补足。

Skill 也已经写明：候选与 fresh state 未变化时，只做一次 consolidated final
validation，不应再运行 `verify1`、`final_verify` 等重叠验证。但这只是指令，
不是 runner 强制状态机。deepseek-v4-flash 仍多次忽略停止条件：

- `MIXED_001` 在 `candidate_v2` 已通过后，又运行 `check_hold`、
  两次相同 `final_validate` 和 `verify_fix`；
- `MIXED_002` 在完整检查后又运行 `final_verify` 和 `end_to_end`；
- `SETUP_002` 因日志截断和误读 baseline fanout，多次重复同一 DRV/候选验证。

因此，Skill 已“覆盖”避免重复自验的原则，但尚未“保证执行”。更可靠的后续改进
是由 harness 维护候选 SHA 与验证状态：当候选 SHA、checkpoint SHA 和验证脚本
类别不变时，拒绝或提醒重复全量 replay。

## deepseek-v4-flash 的主要行为特征

### 做得好的部分

- 7 个通过 case 都完成了真实 fresh replay 闭环，而不是只凭静态 Tcl 推断。
- 对 mixed case 能同时处理 setup replacement 和 hold repeater。
- 新 DRC/connectivity Skill 的命令形式被正确采用。
- `MIXED_001` 和 `MIXED_002` 的最终候选在两次 official replay 中完全确定。
- `SETUP_004` 相比 GLM-5.2 避免了逐类别 DRC 回退。

### 主要短板

- “通过后停止”执行不稳定，重复 restore 和全量验证占用大量时间。
- 容易因输出管道只保留 `tail` 而失去早期 timing/DRV 证据，随后用新 replay
  补证。
- 对 `report_constraint` 的 stdout 与 Tcl 返回值区别理解不稳；在
  `SETUP_002` 曾把返回值写入小文件并误以为得到完整报告。
- 曾把继承的非零 max-fanout baseline 当成必须清零，而公开标准实际是
  before/after 不回退。
- 交付前缺少最小字节级 preflight：末尾 LF、无 CR、首行非空、命令白名单。

## 结论与建议

Claude Code + deepseek-v4-flash 的 Pilot-10 outcome 成绩是 **70%**，与
GLM-5.2 持平。deepseek 在部分真实修复上表现更好，但效率和停止纪律较弱，
并新增了一个完全可避免的交付格式失败。

建议按优先级补三项通用机制：

1. runner 在提交前做只读格式 preflight，至少检查 LF EOF、首行非空、CR 和
   Markdown fence；
2. 以 `candidate SHA + checkpoint SHA + validation profile` 作为验证缓存键，
   候选未变时不重复 full replay；
3. 将工具完整 stdout 先保存到文件，再从该文件解析摘要，禁止用 `| tail`
   作为唯一证据源。

`MIXED_001` 的逐阶段、逐调用和 restore 耗时分析见
[`MIXED_001_DEEPSEEK_V4_FLASH_REVIEW.md`](MIXED_001_DEEPSEEK_V4_FLASH_REVIEW.md)。
