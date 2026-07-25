# MIXED_001 Claude Code + GLM5.2 Session 复盘

## 结论

Claude Code + GLM5.2 找到了一个在真实 EDA 结果上成立的修复：

- 将 4 个 `DLY2_X4M_A9TR40` 替换为 `BUF_X4M_A9TR40`；
- 在 `pp_exp_d2_reg_2_/D` 插入 1 个 `DLY4_X0P5M_A9TR40`；
- 两次 trusted Innovus replay 均通过：
  - setup WNS：`-0.144 ns -> +0.028 ns`
  - hold WNS：`-0.069 ns -> +0.050 ns`
  - DRC：`6952 -> 6951`
  - connectivity：`0 -> 0`

原始 runner 在 2026-07-24 将状态记为 `official_failed`。失败不来自 timing、
DRV、DRC、connectivity 或重放不稳定，而是当时 finalizer 要求四个 setup cell
必须精确恢复为 `BUF_X2M_A9TR40`；模型选择的是同功能、同驱动强度的
`BUF_X4M_A9TR40`。

2026-07-25 benchmark 改为真实工程 outcome 判分，删除了隐藏
canonical-action 门槛。使用原有两次 replay 证据重新执行 finalizer 已通过，
因此当前判定为 **PASS**，没有重跑 Innovus。

这说明原始失败测到的是 Gold 动作匹配，而不是 agent 的真实工具闭环能力。

## 运行身份与计时

| 项目 | 数值 |
|---|---:|
| Case | `MIXED_001` |
| Claude session ID | `23cd0b7b-46fb-4347-a835-d127519cd395` |
| Model | `glm-5.2[1m]` |
| Skill SHA256 | `a594b26359587f5f44e9f99d60028bc549212ce99411e471d91a64bc60507b82` |
| 外层 Claude 启动次数 | 1 |
| 内部 turns | 62 |
| Bash calls | 60 |
| Agent Innovus calls | 18 |
| Agent PrimeTime calls | 4 |
| Runner 记录的 Claude wall time | `4813.477 s`（`1:20:13.477`） |
| Claude transcript duration | `4923.348 s`（`1:22:03.348`） |
| GLM API duration | `1625.377 s`（`27:05.377`） |
| Transcript 非 API 时间 | `3297.971 s`（`54:57.971`） |
| Input/output tokens | `181887 / 67721` |
| Cache-read input tokens | `3722048` |
| 记录成本 | `$4.468489` |
| Official wall time | `2432.771 s`（`40:32.771`） |
| Official Innovus/PT | `2 / 0` |

VM JSONL 的事件时间戳用于还原顺序；runner 的 monotonic wall time 是正式耗时。
两者相差约 1 分 50 秒，不能把 VM 事件首尾差替代 runner wall time。

## 阶段复盘

### 1. 环境确认与 checkpoint 恢复

模型先读取任务，检查 `/workspace`、`/case`、Liberty、checkpoint 和工具版本。
第一次把 `/case/violating.enc` 当成 restore 入口，随后检查文件内容，改用
`/case/violating.enc.dat` 和 top name 完成恢复。这部分方向正确，但用了三次
`probe1` 才稳定下来。

### 2. 时序路径和工具语法探索

模型通过 `probe2`、`probe3` 获取 setup/hold 路径及对象信息，同时并行运行
两个 help probe。help probe 没有显式 `exit`，分别等到 120 秒和 240 秒超时。
这违反了 Skill 中“每个诊断进程必须退出”的指导，是约 4 分钟可直接避免的
等待。

路径解析时一次 `sed` 表达式失败，随后立即改用 `awk`，影响很小。

### 3. Liberty 与可替换 cell 分析

模型先通过 Innovus 查 lib cell，但得到 `NOT FOUND` 后转为直接读取 setup/hold
Liberty。它检查了：

- `DLY2_X4M_A9TR40`
- `BUF_X4M_A9TR40`
- `BUF_X2M_A9TR40`
- `BUF_X1M_A9TR40`
- 多个 `DLY4`/buffer 变体

模型正确证明了 `DLY2` 与 `BUF` 都是 `A -> Y` 的非反相功能，并比较了 delay
table。它最终以“同功能、同 drive strength 更保守”为理由选择 `BUF_X4M`。
这在公开任务和 EDA 结果下是合理选择，但没有命中 evaluator 隐藏的 `BUF_X2M`
canonical repair。

### 4. 候选 A

`cand_a.tcl` 首次实现了最终候选：

- 四个 `DLY2_X4M -> BUF_X4M`
- 一个 endpoint hold repeater
- `refinePlace -eco true`
- `ecoRoute -target`

该次 Innovus 用时 478.4 秒，已经得到通过的 setup/hold、DRV、DRC 和
connectivity 方向性证据。

更新后的 DRV Skill 在这里产生了明确效果：模型第一次就使用了成功的
Innovus 21.10 形式：

```tcl
report_constraint \
    -drv_violation_type <max_transition|max_capacitance|max_fanout> \
    -view functional_setup_ss \
    -all_violators \
    -verbose
```

没有再出现 HOLD_004 中的 `drv_probe` 到 `drv_probe8`，也没有
`report_constraint` 参数错误。

### 5. 重复验证与 baseline DRV

候选没有发生变化，但模型又运行了：

- `validate_a`：约 7 分 25 秒的后台执行/轮询；
- 单独的 `baseline_drv`：约 4 分 40 秒；
- `confirm_state`：103 秒；
- `final_check`：约 9 分 40 秒。

`baseline_drv` 用于确认 431 个 max-fanout baseline pin 与候选完全一致，这个
比较本身有价值；更高效的方式是在第一次 fresh restore 的同一进程中先记录
baseline，再 source 候选并记录 after。

`validate_a` 和 `final_check` 使用的是未改变的候选，属于可压缩重复。新 Skill
要求候选收敛后只做一次 consolidated final validation，但模型只部分遵循：
它正确把 setup、hold、DRV、DRC、connectivity 合并进了 `final_check`，仍保留
了此前内容高度重叠的 `validate_a`。

### 6. PrimeTime 导出与交叉检查

第一次 `pt_export` 使用了不兼容的 `rcOut -spef ... -view ...` 形式，约等待
4 分 40 秒后发现错误。第二次 `pt_export2` 修正后成功导出 netlist、两套 SDC
和 SPEF。

模型随后：

- 把 Innovus SDC 的 `current_design`/design-scoped constraint 改成 PT 可接受
  的形式；
- 修正第一次 PT driver script；
- 分别运行 setup 与 hold；
- 得到 PT setup `+0.19 ns`、hold `+0.05 ns`。

PrimeTime 本身只用了很少时间，主要浪费发生在第一次 Innovus 导出失败。

### 7. 最终交付与 official evaluator

模型写出的 `fix.tcl` 只包含允许的 ECO、增量摆放和 ECO routing，没有修改 SDC
或伪造报告。两次 trusted replay 完全一致并分别通过真实 Innovus gate：

| Gate | Before | After | Replay 1/2 |
|---|---:|---:|---:|
| Setup WNS | `-0.144` | `+0.028` | 相同 |
| Hold WNS | `-0.069` | `+0.050` | 相同 |
| DRC total | `6952` | `6951` | 相同 |
| Connectivity | `0` | `0` | 相同 |
| Innovus marker | 无 | `SFT_CASE_PASSED` | 两次均存在 |

原始 finalizer 随后检查 `concrete_fix.tcl`，报错：

> MIXED_001 repair must use its exact four BUF_X2M restores followed by its exact hold repeater

因此原始状态为 `official_failed`，official PT 次数为 0。2026-07-25 删除该
检查后，现有完整证据通过 validate-only finalization，当前 outcome 判定为
`PASS`。

## 完整工具步骤账本

下表覆盖 transcript 中全部 61 个 tool result。时间为 VM 事件时钟；耗时是从
tool use 到对应 tool result。后台 Innovus 的实际执行跨越后续 sleep/poll 行，
并行 help calls 也会重叠，因此各行耗时不能简单相加。

| # | 时间 | 耗时 | 动作与结果 |
|---:|---:|---:|---|
| 1 | 16:26:52 | 0.0s | 读取完整 `TASK.md` 与拼接 Skill。 |
| 2 | 16:27:07 | 0.1s | 列出 `/workspace`、`/case`，确认可见范围。 |
| 3 | 16:27:07 | 1.9s | 确认 Innovus 21.10 与 PrimeTime R-2020.09-SP4。 |
| 4 | 16:27:17 | 0.0s | 检查 PT Liberty 与 checkpoint 数据目录。 |
| 5 | 16:27:17 | 0.0s | 检查 case/workspace 大小和临时目录。 |
| 6 | 16:27:25 | 0.0s | 创建 scratch/report 目录和首个 `probe1.tcl`。 |
| 7 | 16:27:29 | 13.7s | 首次 Innovus probe；restore 入口不正确，进程正常退出。 |
| 8 | 16:27:47 | 0.1s | 检查 `violating.enc` 文件内容与 `.dat` 目录。 |
| 9 | 16:28:03 | 8.8s | 改用 `.dat` checkpoint 再次 probe。 |
| 10 | 16:28:25 | 48.2s | 第三次 `probe1`，恢复设计并开始 timing 查询。 |
| 11 | 16:30:26 | 43.4s | `probe2` 查询 setup/hold 路径与目标对象。 |
| 12 | 16:31:49 | 120.1s | `help_probe` 查 `report_timing`；未显式 exit，等到超时。 |
| 13 | 16:31:50 | 240.2s | 并行 `help_probe2` 查 ECO 命令；未显式 exit，等到超时。 |
| 14 | 16:36:34 | 41.2s | `probe3` 调整 timing 报告形式。 |
| 15 | 16:37:34 | 240.4s | 扩展 `probe3`，生成完整 setup/hold 诊断报告。 |
| 16 | 16:41:43 | 0.0s | 从报告提取两条 setup path 的关键点。 |
| 17 | 16:43:25 | 0.0s | 用 `sed` 提取 hold path，表达式错误。 |
| 18 | 16:43:30 | 0.0s | 改用 `awk`，成功提取 hold path 和 setup margin。 |
| 19 | 16:44:40 | 96.0s | `probe4` 查询候选 lib cells；短名查询返回 NOT FOUND。 |
| 20 | 16:46:57 | 41.2s | `probe5` 改进 lib-cell 查询并找到 buffer variants。 |
| 21 | 16:47:50 | 0.4s | 从 setup Liberty 定位 `DLY2_X4M` 定义。 |
| 22 | 16:47:57 | 0.1s | grep DLY2/BUF/DLY4 cell 名称。 |
| 23 | 16:48:11 | 0.1s | 查看 `BUF_X4M` 和 DLY cell 定义位置。 |
| 24 | 16:48:23 | 0.2s | 提取 `DLY2_X4M` pin/function/timing 片段。 |
| 25 | 16:48:36 | 0.2s | 比较 `BUF_X4M` 与 `DLY2_X4M` setup delay。 |
| 26 | 16:49:10 | 0.2s | 在 hold Liberty 中核对同类 cells。 |
| 27 | 16:49:20 | 0.9s | 比较多种 `DLY4`/buffer delay。 |
| 28 | 16:51:23 | 0.9s | 比较 `BUF_X4M/X2M/X1M`；模型仍选择 X4M。 |
| 29 | 16:52:56 | 478.4s | 运行 `cand_a`：4 个 setup replacement + 1 个 hold repeater，并做综合检查。 |
| 30 | 17:01:04 | 0.1s | 解析候选前后 setup/hold timing。 |
| 31 | 17:01:43 | 0.1s | 解析 DRV、DRC 和 connectivity 结果。 |
| 32 | 17:01:56 | 0.1s | 详细检查 max-fanout 报告和物理报告。 |
| 33 | 17:03:12 | 0.7s | 额外比较 OAI211 和小驱动 buffer variants。 |
| 34 | 17:05:10 | 5.2s | 后台启动 `validate_a`。 |
| 35 | 17:05:20 | 200.1s | 第一次轮询 `validate_a`，仍在运行。 |
| 36 | 17:08:46 | 240.1s | 第二次轮询，收集完整 timing/DRV/DRC/connectivity。 |
| 37 | 17:12:56 | 0.1s | 检查 setup/hold 最差路径。 |
| 38 | 17:13:10 | 0.0s | 检查 transition/capacitance/DRC/connectivity。 |
| 39 | 17:13:21 | 0.0s | 检查 max-fanout 431 条 baseline 风格违例。 |
| 40 | 17:13:51 | 0.1s | 确认 ECO touched cells 未引入新 fanout violations。 |
| 41 | 17:14:15 | 0.0s | 后台启动独立 `baseline_drv`。 |
| 42 | 17:14:19 | 280.1s | 等待 baseline DRV 完成。 |
| 43 | 17:19:10 | 0.1s | 比较 baseline/after fanout pin 集合，确认完全一致。 |
| 44 | 17:20:50 | 103.0s | `confirm_state` 核对替换 cell 与新增实例状态。 |
| 45 | 17:23:04 | 0.0s | 写最终 `fix.tcl`，并检查无 report/save/evidence 命令。 |
| 46 | 17:23:20 | 0.0s | 后台启动 consolidated `final_check`。 |
| 47 | 17:23:24 | 300.1s | 第一次轮询超出 Bash 客户端窗口，后台任务继续。 |
| 48 | 17:28:30 | 280.1s | 第二次轮询，final timing/DRV/DRC/connectivity 完成。 |
| 49 | 17:33:20 | 0.1s | 解析最终 Innovus 报告。 |
| 50 | 17:34:00 | 0.0s | 后台启动第一次 PT netlist/SDC/SPEF export。 |
| 51 | 17:34:04 | 280.2s | 等待第一次 export。 |
| 52 | 17:38:50 | 0.0s | 发现 `rcOut -spef ... -view ...` 语法错误。 |
| 53 | 17:39:33 | 0.0s | 后台启动修正后的 `pt_export2`。 |
| 54 | 17:39:37 | 300.1s | 等待并确认第二次 export 成功。 |
| 55 | 17:44:44 | 0.0s | 检查 Innovus SDC 中的 design-scoped constraints。 |
| 56 | 17:45:01 | 0.0s | 生成 PT-compatible setup/hold SDC。 |
| 57 | 17:45:28 | 1.0s | 写首个 PT driver，并查看命令帮助。 |
| 58 | 17:45:35 | 21.5s | 首次 PT setup 调用；调整 driver。 |
| 59 | 17:46:41 | 35.2s | 使用修正 driver 完成 setup 与 hold PT 检查。 |
| 60 | 17:47:21 | 0.1s | 解析 PT setup `+0.19`、hold `+0.05`。 |
| 61 | 17:48:29 | 0.1s | 删除大型诊断 PT 输入，保留最终 `fix.tcl` 并输出摘要。 |

## 做得好的部分

- 只启动一次 Claude session，并在 session 内完成完整工具闭环。
- 正确定位 4 个 setup delay cells 和 1 个 hold endpoint。
- 证明 replacement 与 repeater 的 Boolean function/pin compatibility。
- 新 DRV Skill 使 `report_constraint` 第一次即成功，没有重复语法探测。
- 对 max-fanout 非零 baseline 做了 pin-set 级比较，没有误把 431 当作候选失败。
- Innovus 与 agent-side PrimeTime 均得到通过的 timing。
- 最终修复可重放、物理结果稳定，两次 trusted replay 完全一致。
- `fix.tcl` 保持最小化，没有把诊断逻辑写进交付物。

## 主要问题与改进建议

### 1. 已解决：目标函数与 evaluator 不一致

模型以“真实 EDA outcome + 保守同驱动替换”为目标，选择 `BUF_X4M`；evaluator
以隐藏 canonical action 为目标，只接受 `BUF_X2M`。需要决定 benchmark 到底
评估哪一种：

当前 evaluator 已采用第一种口径：接受满足公开约束和 outcome gates 的修复。
Gold 的 exact restore 只保留在数据生成与校准阶段，不参与 candidate 判分。

### 2. Help probe 未退出

两个无设计 help 脚本遗漏 `exit 0`，浪费约 4 分钟。Skill 已有明确模板，模型未
完全遵循。可以把 help 示例本身写成包含 `exit 0` 的完整最小脚本，降低遗漏率。

### 3. 候选未变化却重复完整验证

`cand_a`、`validate_a`、`final_check` 使用同一修复。应把：

- baseline capture
- candidate timing
- DRV
- DRC
- connectivity

合并为一次 fresh-restore candidate run；只有候选变化时才重新运行。

### 4. PT export 语法错误

第一次 export 因 `rcOut` 参数形式错误浪费约 5 分钟。可以把当前版本已成功的
netlist/SDC/SPEF 导出命令补入 Skill，或提供只读、版本固定的 export helper。

### 5. 过度依赖长 sleep

后台 Innovus 本身需要时间，但固定 `sleep 200/240/280/300` 降低了可观测性，
也让已经完成的任务可能等待到下一轮 poll。应使用较短的递增轮询，并同时检查
PID、exit-status 文件和日志进度。

## 新 DRV Skill 的效果

和更新前的 HOLD_004 对比：

- HOLD_004：为 DRV 语法做了 8 次 probe，加 final DRV 共约 29 分 46 秒；
- MIXED_001：没有 DRV 命令发现 probe，第一次候选即正确输出三个 DRV 类别；
- 仍有一次独立 baseline DRV restore（约 4 分 40 秒），说明“正确命令知识”
  已解决，但“把 baseline/after 合并进同一验证进程”仍需进一步强化。

因此这次 Skill 更新有效消除了主要的 DRV 语法探索问题，但没有完全消除候选
不变时的重复全量验证。

## 可核对的原始证据

原始证据位于未纳入 Git 的运行目录
`pilot_10/work/agent_benchmark/glm52-claude-code-remaining2-drv-skill-20260724/`。
下列路径均相对于该目录：

- `run.json`
- `cases/MIXED_001/claude_turn_1.jsonl`
- `cases/MIXED_001/agent/fix.tcl`
- `cases/MIXED_001/agent/reports/`
- `cases/MIXED_001/official/cycle_0001/innovus_runner.log`
- `cases/MIXED_001/official/cycle_0001/prepared/` 下的 `runs/replay_1/`
- `cases/MIXED_001/official/cycle_0001/prepared/` 下的 `runs/replay_2/`
