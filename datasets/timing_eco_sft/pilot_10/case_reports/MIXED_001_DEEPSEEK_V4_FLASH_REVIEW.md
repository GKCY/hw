# MIXED_001 Claude Code + deepseek-v4-flash Session 复盘

## 结论

Claude Code + deepseek-v4-flash 找到了一个通过全部官方 outcome gate 的修复：

- 将 4 个 `DLY2_X4M_A9TR40` 替换为 `BUF_X4M_A9TR40`；
- 在 `pp_exp_d2_reg_2_/D` 插入 1 个
  `DLY4_X0P5M_A9TR40`；
- 两次 trusted fresh Innovus replay 完全一致；
- 独立 PrimeTime setup/max 与 hold/min 均通过。

官方结果：

| Gate | Before | After |
|---|---:|---:|
| Innovus setup WNS/TNS | `-0.144 / -0.273 ns` | `+0.028 / 0 ns` |
| Innovus hold WNS/TNS | `-0.069 / -0.069 ns` | `+0.050 / 0 ns` |
| DRV transition/cap/fanout | `0 / 0 / 431` | `0 / 0 / 431` |
| DRC total | `6952` | `6951` |
| Connectivity | `0` | `0` |
| PrimeTime setup WNS | — | `+0.190923 ns` |
| PrimeTime hold WNS | — | `+0.045089 ns` |

最终判定：**PASS**。

质量结论是正面的，效率结论则相反：最终候选在 14:03 已经得到完整的
setup/hold、DRV、DRC 和 connectivity 结果，但模型仍用相同 action set 做了
4 次后续 Innovus replay。按保守口径，至少 `19:30.4`、即模型墙钟
`25.0%` 是可避免的重复验证。

## 运行身份与计时

| 项目 | 数值 |
|---|---:|
| Case | `MIXED_001` |
| Claude session ID | `191d1007-112b-4f63-91fd-78adf5b68967` |
| Runner 实测模型 | `deepseek-v4-flash` |
| Skill SHA256 | `f95e601bd4f2fc8add6e8d0419acca08b9757bee18a706ba25f749a21662f312` |
| Task SHA256 | `76497c3cd3a6dfe4d418b6f684d45f7de6f4e25acf0b224f7b909b9cdec44080` |
| Final fix SHA256 | `e093ace8afe046568d453205991ae203752d8ddd37504c73ed877f703eec96ad` |
| 外层 Claude 启动次数 | 1 |
| 内部 turns | 69 |
| Bash calls | 60 |
| Agent Innovus calls | 17 |
| Agent PrimeTime calls | 4 |
| Runner Claude wall | `4676.352 s`（`1:17:56.352`） |
| Claude Code reported duration | `4720.172 s` |
| Input/output tokens | `117838 / 33830` |
| Cache-read input tokens | `2998016` |
| 记录成本 | `$2.942878` |
| Official wall | `2587.478 s`（`0:43:07.478`） |
| Official Innovus/PT | `2 / 2` |

正式模型耗时以 runner 的 monotonic wall 为准。Claude Code JSONL 事件首尾、
usage duration 与 runner 墙钟来自不同计时边界，不应互相替代。

## 最终修复

```tcl
setEcoMode -batchMode true
ecoAddRepeater -term pp_exp_d2_reg_2_/D -cell DLY4_X0P5M_A9TR40 -name SFT_ECO_MIXED_001_HOLD_1
ecoChangeCell -inst u_exp/SFT_ECO_MIXED_001_PATH_1 -cell BUF_X4M_A9TR40
ecoChangeCell -inst u_exp/SFT_ECO_MIXED_001_PATH_2 -cell BUF_X4M_A9TR40
ecoChangeCell -inst u_exp/SFT_ECO_MIXED_001_PATH_3 -cell BUF_X4M_A9TR40
ecoChangeCell -inst u_exp/SFT_ECO_MIXED_001_PATH_4 -cell BUF_X4M_A9TR40
setEcoMode -batchMode false
refinePlace -eco true
ecoRoute -target
```

共 5 个 ECO action，低于 6-cell 上限。脚本不修改约束、不生成证据、不 source
诊断文件。

## `fix.tcl` 是否变化

最终 action set 从 `candidate_v2` 开始没有再变化。对下列脚本提取
`setEcoMode`、`ecoAddRepeater`、`ecoChangeCell`、`refinePlace` 和
`ecoRoute`，去掉行首空白后计算 SHA256，结果都相同：

| 脚本/阶段 | Action SHA256 |
|---|---|
| `candidate_v2.tcl` | `e093ace8...96ad` |
| `check_hold.tcl` | `e093ace8...96ad` |
| 第一次 `final_validate.tcl` | `e093ace8...96ad` |
| 第二次 `final_validate.tcl` | `e093ace8...96ad` |
| 最终 `fix.tcl` | `e093ace8...96ad` |
| `export_for_pt.tcl` | `e093ace8...96ad` |

`verify_fix.tcl` 是 `source /workspace/fix.tcl`，实际加载的也是同一份
`e093ace8...96ad` 文件。模型写出 `fix.tcl` 后没有再编辑；结尾两次 Read
看到的内容一致，runner 收集到的 final fix SHA 也一致。

因此，过度验证不能解释为“fix 变了，所以必须重验”。候选没变是有哈希证据的。

## 阶段复盘

### 1. 环境与 checkpoint

13:22 开始读取 TASK、列出 `/case` 与 `/workspace`，确认真正的 Innovus
checkpoint 数据目录为 `/case/violating.enc.dat`。首个 `diagnostic.tcl`
在一次 restore 中收集基础 timing、DRV、DRC 和 connectivity，命令形式正确。

### 2. 诊断探索

模型随后连续运行 `diag2` 到 `diag9`：

- 第一次使用 `report_timing -nosplit`，Innovus 21.10 不支持；
- 后续用报告文件和 `grep` 查两条 setup path、hold endpoint、cell/ref；
- 三次尝试 `-transition_time`、`-nets` 等不支持参数；
- 通过 `get_lib_cells`/timing 报告确认可替换 cell。

主要问题不是完全不会诊断，而是每个小问题都新开 fresh Innovus。`diag6`、
`diag7`、`diag8` 连续三次只为修正同一条 path report 选项，每次都支付
restore 和启动成本。

### 3. 候选 v1：方向错误

13:39–13:49 的候选：

- hold：插入 `DLY2_X4M_A9TR40`
- setup：4 个 `DLY2_X4M -> DLY2_X0P5M`

结果：

- setup 最差 `-0.210 ns`
- hold 最差 `-0.009 ns`
- DRC `6952`
- connectivity `0`

这说明更小驱动的 delay cell 没有加速 setup，hold delay 也不够。模型正确否决
候选，没有把失败方案写进最终文件。

### 4. 候选 v2：最终工程解

13:55–14:03 的候选改为：

- hold：`DLY4_X0P5M`
- setup：4 个 `DLY2_X4M -> BUF_X4M`

该次运行已得到：

- setup 最差 `+0.028 ns`
- hold 最差 `+0.050 ns`
- max transition/capacitance 为 0
- max fanout 与 baseline 同为 431
- DRC `6951`
- connectivity `0`

新补入 Skill 的通用 DRC/connectivity 命令一次成功，没有 marker API 探索：

```tcl
verify_drc -limit 1000000 -report <path>
verifyConnectivity -type all -report <path>
```

### 5. 过度验证

候选 v2 之后 action SHA 不变，模型仍执行：

| 调用 | Innovus real | 新增有效信息 |
|---|---:|---|
| `check_hold.tcl` | `5:50` | 单独再次查看相同 hold endpoint 和整体 timing |
| `final_validate.tcl` 第 1 次 | `6:34` | 相同候选，再次生成 timing/DRV/DRC/connectivity |
| `final_validate.tcl` 第 2 次 | `6:51` | 脚本和候选均未变化，结果相同 |
| `verify_fix.tcl` | `6:47` | 仅把相同 action set 改为从 `fix.tcl` source |

四次合计 Innovus real `26:02`。如果保留其中一次作为 Skill 要求的
consolidated final validation，那么另外三次
`check_hold + 第二次 final_validate + verify_fix` 合计：

`350 + 411 + 407 = 1168 s`，约 `19:28`；按 JSONL tool interval 是
`1170.4 s`，约 `19:30.4`，占 `4676.352 s` 模型 wall 的 **25.0%**。

若把 `candidate_v2` 本身视作已经完成最终检查，则四次全部可压缩，JSONL
interval 合计 `1565.4 s`，占 **33.5%**。报告采用前一个保守数字作为
“确定可避免”耗时。

### 6. 为什么模型会重复验证

从 transcript 的可观测文字和工具行为看，动机不是候选变化，而是以下几种
置信度补偿：

1. `candidate_v2` 的 shell 输出用了 `grep`，模型担心未看到完整 hold 细节，
   因而启动 `check_hold`。
2. 第一次 `final_validate` 的摘要 Tcl 使用了错误的 `file_exists` 命令，
   导致自定义 summary 没正常打印。底层报告其实已生成，但模型把“摘要不可见”
   当成“验证可能不完整”，于是原样再跑一次。
3. `report_constraint` 的 431 个 max-fanout 是继承 baseline，不是新回退。
   模型花时间重新计数，试图获得更强信心。
4. 写出最终 `fix.tcl` 后，模型把“source 最终文件再跑一次”视为交付一致性
   证明；但 action SHA 已经说明脚本内容与候选相同。
5. Skill 的停止规则是自然语言建议，没有候选 SHA 状态机或重复执行拦截。
   模型在信息不完整时倾向于增加验证，而不是信任已有证据并终止。

这是一种“通过重复工具调用降低主观不确定性”的行为。它的意图是保守，
但实际没有增加独立性：所有调用都从同一 checkpoint、同一 action set、
同一工具版本运行，不能替代真正不同的 official replay/PrimeTime。

### 7. Agent 侧 PrimeTime

模型在 14:33–14:38 又做了一次 Innovus export 和 4 次 PT 调用。方向上符合
Skill 的 bounded cross-check，但 agent 侧 PT 结果不能作为可信证据：

- 第一次调用参数不兼容；
- 第一次完整 setup 读取 SDC 时 `current_design` 仍报错；
- 修正版 setup/hold SDC 仍保留 `get_designs`，两边日志都有 2 个 error；
- 模型看到 `No paths with slack less than 0.00` 后就声称 PT 通过，但这个结论
  建立在 SDC 部分读取失败的状态上。

官方 evaluator 用独立生成的 PT-compatible SDC、正确 Liberty/SPEF、
propagated clocks 和路径覆盖审计重新运行，setup `+0.190923 ns`、hold
`+0.045089 ns`。最终 PASS 依赖这组 trusted PT，而不是 agent 的无违例文本。

## Restore 占总耗时多少

每个 Innovus log 都包含：

```text
#% Begin load design ... real=...
#% End load design ... real=...
```

17 次 agent Innovus 的 `restoreDesign` real time 依次为：

`30, 20, 19, 20, 19, 22, 19, 19, 18, 19, 19, 19, 18, 21, 21, 24, 18 s`

合计 **345 s（5:45）**。

- 占模型 wall `4676.352 s`：**7.38%**
- 17 次 Innovus 总 real `3963 s` 中占：**8.71%**
- Innovus 总 real 占模型 wall：约 **84.7%**

这回答的是 Innovus 日志明确标注的“load design/restore”区间，不包含：

- Innovus 可执行文件启动到 `Begin load design` 之前的初始化；
- restore 后的 timing update、placement、route、DRC；
- official 两次 replay；
- Claude API 思考和普通 Bash。

所以“5:45 / 7.38%”是纯 restore 的可审计下界，不是每次重新启动 Innovus
带来的全部代价。若把进程启动、库初始化和 restore 后被反复触发的时序初始化
都算作“fresh-process 成本”，实际占比会更高，但日志没有一个无歧义的统一边界，
本报告不做臆测。

## 官方验证

两次 fresh replay 的 candidate fix、timing、DRV、物理指标和功能审计均一致。
Replay 1 的详细物理计数：

| 类别 | Before | After | Delta |
|---|---:|---:|---:|
| SPACING | 4229 | 4228 | -1 |
| SHORT | 2682 | 2682 | 0 |
| NSMETAL | 29 | 29 | 0 |
| VIAENCLOSURE | 10 | 10 | 0 |
| CUTSPACING | 1 | 1 | 0 |
| MAR | 1 | 1 | 0 |
| Total | 6952 | 6951 | -1 |

其他门禁：

- constraints setup/hold before/after SHA 完全相同；
- functional audit 11 项全部通过；
- 5 个关键 `checkDesign` 计数均为 0；
- 5 个手工 ECO action 与 Tcl 一致，未超 6-cell budget；
- 两次 replay 最大 timing 差为 0；
- PrimeTime 使用 `R-2020.09-SP4`，parasitics 已读、clock 已 propagated，
  setup/hold violating paths 均为 0。

## 时间戳跟踪原理

Claude Code 的 JSONL 对每个可观测事件记录 UTC `timestamp`。本报告的步骤账本
按以下方法生成：

1. 读取 `assistant` 消息中的 `tool_use`，记录 tool name、输入和
   `tool_use.id`；
2. 读取后续 `user` 消息中的 `tool_result`，用 `tool_use_id` 配对；
3. `tool_result timestamp - tool_use timestamp` 得到该工具调用的外部墙钟区间；
4. UTC 加 8 小时显示为 Asia/Shanghai；
5. 对 Innovus，再用 `innovus.log*` 内部的 Begin/End/Ending 时间核对
   restore 与进程 real time；
6. runner 自己的 monotonic wall 用作正式 case 总耗时。

这种方法能回答“CC 在每个时间点调用了什么、何时拿到结果”，不能看到模型未
输出的内部思维，也不能把后台子进程的每一秒精确归因给某段推理。

一次 Bash 可能同时显示 launcher 和 worker 两个 PID；它们属于同一个
tool call，不能仅凭 PID 数量推断并发跑了两次 Innovus。调用计数以 JSONL
tool_use 和 runner 的 EDA invocation 计数器为准。

## 完整工具步骤账本

时间为北京时间；耗时为 JSONL tool use 到对应 result。写 Tcl 与执行 Innovus
有时在同一个 Bash 调用内完成。

| # | 完成时间 | 耗时 | 动作与结果 |
|---:|---:|---:|---|
| 1 | 13:22:24 | 0.0s | 读取完整 TASK 与拼接 Skill。 |
| 2 | 13:22:28 | 0.1s | 列出 `/case`。 |
| 3 | 13:22:28 | 0.1s | 列出 `/workspace`。 |
| 4 | 13:22:31 | 0.0s | 检查 PT Liberty。 |
| 5 | 13:22:31 | 0.0s | 检查 checkpoint 数据目录。 |
| 6 | 13:22:33 | 0.0s | 读取 checkpoint 入口文件。 |
| 7 | 13:22:33 | 0.0s | 读取 MMMC view definition。 |
| 8 | 13:22:43 | 0.0s | 写首个综合 `diagnostic.tcl`。 |
| 9 | 13:26:58 | 252.6s | 运行 diagnostic；得到 baseline timing/DRV/DRC/connectivity。 |
| 10 | 13:27:46 | 43.6s | 写并运行首个 `diag2`；`-nosplit` 不兼容。 |
| 11 | 13:30:17 | 147.6s | 修正并重跑 `diag2`。 |
| 12 | 13:30:21 | 0.1s | 从保存的输出提取 setup timing。 |
| 13 | 13:30:27 | 0.0s | 提取第二条 setup path。 |
| 14 | 13:34:34 | 239.4s | `diag3` 扩展 endpoint/cell 查询。 |
| 15 | 13:37:49 | 143.0s | `diag4` 查询路径局部对象。 |
| 16 | 13:39:03 | 44.9s | `diag5` 查询 lib/cell 信息。 |
| 17 | 13:39:22 | 0.1s | 写候选 v1。 |
| 18 | 13:49:05 | 580.7s | 运行 v1；setup `-0.210`、hold `-0.009`，否决。 |
| 19 | 13:49:08 | 0.0s | 解析 v1 setup。 |
| 20 | 13:49:08 | 0.0s | 解析 v1 hold。 |
| 21 | 13:49:28 | 0.0s | 查 setup 最差 slack 上下文。 |
| 22 | 13:49:32 | 0.0s | 查 setup endpoints。 |
| 23 | 13:49:32 | 0.0s | 定位 `-0.210` 路径。 |
| 24 | 13:50:25 | 43.0s | `diag6` 尝试 path report 选项；部分参数不支持。 |
| 25 | 13:51:15 | 40.3s | `diag7` 再试 report 选项。 |
| 26 | 13:52:11 | 47.4s | `diag8` 再试 report 选项。 |
| 27 | 13:52:15 | 0.0s | 读取 hold path report。 |
| 28 | 13:52:15 | 0.0s | 读取 setup path report。 |
| 29 | 13:54:44 | 143.5s | `diag9` 生成最终路径报告。 |
| 30 | 13:54:47 | 0.0s | 读取 hold 详情。 |
| 31 | 13:54:47 | 0.0s | 读取 setup path 31。 |
| 32 | 13:54:47 | 0.0s | 读取 setup path 23。 |
| 33 | 13:55:08 | 0.0s | 写候选 v2。 |
| 34 | 14:03:12 | 479.6s | 运行 v2；setup/hold、DRV、DRC、connectivity 均通过。 |
| 35 | 14:03:16 | 0.0s | 解析 v2 hold。 |
| 36 | 14:03:16 | 0.0s | 解析 v2 setup。 |
| 37 | 14:03:19 | 0.0s | 再查 hold endpoint。 |
| 38 | 14:09:18 | 351.4s | 相同 action set 运行 `check_hold`。 |
| 39 | 14:09:41 | 0.0s | 写 `final_validate.tcl`。 |
| 40 | 14:16:19 | 395.0s | 第一次 final validation。 |
| 41 | 14:23:18 | 412.2s | 原样第二次 final validation。 |
| 42 | 14:23:23 | 0.0s | 解析 final DRC。 |
| 43 | 14:23:23 | 0.0s | 解析 final hold。 |
| 44 | 14:23:26 | 0.0s | 检查 DRC/DRV 报告存在。 |
| 45 | 14:23:31 | 0.0s | 查看 fanout 报告头。 |
| 46 | 14:23:31 | 0.0s | 再解析 DRC。 |
| 47 | 14:23:31 | 0.0s | 查看 transition/cap 报告。 |
| 48 | 14:23:34 | 0.0s | 查看 DRC 报告尾。 |
| 49 | 14:23:34 | 0.0s | 统计 transition/cap 的 No Violations。 |
| 50 | 14:25:42 | 0.0s | grep fanout 文本计数。 |
| 51 | 14:25:42 | 0.0s | 再次 grep fanout。 |
| 52 | 14:25:45 | 0.0s | 用表格行重新统计 fanout。 |
| 53 | 14:25:52 | 0.0s | 写最终 `fix.tcl`；action 与 v2 相同。 |
| 54 | 14:32:52 | 406.8s | fresh restore 后 source 相同 `fix.tcl` 再验。 |
| 55 | 14:32:56 | 0.1s | 查看 fix DRC。 |
| 56 | 14:32:56 | 0.0s | 查看 fix setup。 |
| 57 | 14:32:56 | 0.0s | 查看 fix hold。 |
| 58 | 14:36:56 | 229.5s | 相同 action set 导出 PT netlist/SDC/SPEF。 |
| 59 | 14:36:59 | 0.0s | 检查 PT 导出文件。 |
| 60 | 14:36:59 | 0.0s | 查看 setup SDC 头。 |
| 61 | 14:37:10 | 0.0s | 写第一版 PT setup driver。 |
| 62 | 14:37:14 | 0.9s | `pt_shell -no_gui` 参数不兼容。 |
| 63 | 14:37:35 | 16.9s | PT setup；`current_design` SDC 适配失败。 |
| 64 | 14:38:07 | 21.0s | PT setup2；仍有 `get_designs` SDC error。 |
| 65 | 14:38:40 | 21.1s | PT hold；日志仍有 2 errors。 |
| 66 | 14:38:44 | 0.0s | 读取最终 `fix.tcl`。 |
| 67 | 14:38:51 | 0.1s | 模型清理诊断目录；Claude 临时输出也被删，命令 result 报 ENOENT。 |
| 68 | 14:38:54 | 0.0s | 再读最终 `fix.tcl`，内容未变。 |

## 做得好的部分

- 一次 Claude session 内找到并验证了真实可行修复。
- v1 失败后能根据 setup/hold 两侧证据同时调整，而不是只修一个方向。
- 最终 5-action 候选低于预算，命名、功能和 pin compatibility 正确。
- 新 Skill 的 DRC/connectivity 命令首次使用即成功。
- 最终脚本无约束修改，official 两次 replay 完全确定。
- trusted PrimeTime、功能审计和物理逐类别 gate 全部通过。

## 主要问题与改进

### 1. 用完整日志作为唯一事实源

不要把 `innovus ... | tail` 或 `| grep` 当作唯一证据。应先把 stdout/stderr
完整重定向到一个文件，再从文件提取摘要；这样不会因为看不到早期输出而重跑。

### 2. 候选 SHA 驱动停止

当 action SHA、checkpoint SHA 和验证 profile 都不变时：

- 已有一次成功 consolidated validation：停止 Innovus；
- 只是想确认最终文件与候选一致：比较 SHA，不重跑 EDA；
- 只有候选 action、输入 checkpoint 或 gate 集合变化才允许新 full replay。

### 3. 区分 baseline 非零与回退

max-fanout `431` 是 before/after 都存在的 baseline。正确判据是计数/对象集合不
增加，不是绝对清零。

### 4. Agent PT 必须 fail closed

`read_sdc` 有 error、clock/path coverage 未完整证明时，不能根据
`No paths with slack less than 0.00` 宣称通过。要么修完 SDC dialect 并重新跑，
要么把 PT 标记为 inconclusive，保留 Innovus-tested candidate 后结束。

### 5. Skill 已写停止规则，但需要 harness 约束

本轮 Skill 已明确写出“candidate 和 restored state 不变时不重复
`verify1`/`final_verify`”。模型仍违反，说明自然语言覆盖不足。建议 runner
增加候选验证缓存或重复调用告警，才能把原则变成稳定行为。

## 证据位置

- Run metadata：
  `work/agent_benchmark/deepseek-v4-flash-pilot3-drc-connect-skill-20260727/run.json`
- Claude JSONL：
  `cases/MIXED_001/claude_turn_1.jsonl`
- Agent scripts/logs：
  `cases/MIXED_001/agent/`
- Official acceptance：
  `cases/MIXED_001/official/cycle_0001/acceptance.json`
- Finalized metrics：
  `cases/MIXED_001/official/cycle_0001/finalized/cases/MIXED_001/metrics.json`
- Trusted PrimeTime：
  `cases/MIXED_001/official/cycle_0001/finalized/cases/MIXED_001/primetime_crosscheck.json`
