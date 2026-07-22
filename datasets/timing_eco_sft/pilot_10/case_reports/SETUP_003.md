# SETUP_003 Timing ECO SFT 数据报告

## 1. Case 定位

| 字段 | 内容 |
|---|---|
| 类型 / 难度 | Setup / `hard` |
| 修复模式 | `surgical` |
| 注入策略 | `insert_data_delay` |
| 修复策略 | `replace_delay_and_upsize` |
| ECO 修改预算 | 最多 6 个 cell；Gold 实际设计修改 6 项 |
| 注入校准 | `FROZEN`，一次冻结注入命中 setup [-0.250, -0.150] ns |

本报告是人类审核材料，包含隐藏的注入 oracle。训练时应只使用规范 `dataset.jsonl` 中的 `instruction`/`answer`，不能把本报告或 `inject.tcl` 整体送入模型，否则会泄漏违例构造方法。

## 2. Design 初始情况

这里的“初始”分为两层：第一层是注入前的未改动 post-route 基线，用来证明违例不是设计原生遗留；第二层是注入完成后的 `violating.enc`，它才是 SFT instruction 描述、模型需要修复的输入状态。

### 2.1 未注入基线

- Top：`NV_NVDLA_CMAC_CORE_mac`；工具：Innovus `21.10-p004_1`；数据库状态：post-route，`top.statusRouted` 为 complete。
- Setup view：`functional_setup_ss`，SS/max Liberty `sc9mc_logic0040ll_base_rvt_c40_ss_typical_max_0p99v_125c.lib`；Hold view：`functional_hold_ff`，FF/min Liberty `sc9mc_logic0040ll_base_rvt_c40_ff_typical_min_1p21v_m40c.lib`。
- 干净基线 setup WNS/TNS = +0.051 / +0.000 ns；hold WNS/TNS = +0.050 / +0.000 ns，两方向均无 violating endpoint。
- DRV：max_transition=0、max_capacitance=0、max_fanout=431；connectivity=0。
- 冻结参考 DRC=6952（CUTSPACING=1, MAR=1, NSMETAL=29, SHORT=2682, SPACING=4229, VIAENCLOSURE=10）。这个数值是 benchmark 的 no-regression 参考，不是 foundry signoff-clean 声明。
- Baseline 状态为 `QUALIFIED_CANDIDATE`，技术分类 `derived_non_signoff`，`signoff_eligible=false`。因此这些数据可用于 ECO/SFT benchmark，不能表述为 foundry signoff 结果。

### 2.2 注入后、修复前的 SFT 输入

| 检查项 | 实测值 |
|---|---|
| Setup（`functional_setup_ss`） | WNS/TNS = -0.203 / -0.381 ns |
| Hold（`functional_hold_ff`） | WNS/TNS = +0.050 / +0.000 ns |
| DRV | transition=0，capacitance=0，fanout=431 |
| DRC | total=6949；CUTSPACING=1, MAR=1, NSMETAL=29, SHORT=2682, SPACING=4226, VIAENCLOSURE=10 |
| Connectivity | 0 |

## 3. SFT Instruction（原文）

```text
请在 NV_NVDLA_CMAC_CORE_mac 的已布线 post-route 数据库上完成 SETUP_003 timing ECO；当前 setup 方向存在负裕量。真实 Innovus MMMC 报告显示：setup 视图 functional_setup_ss 的 WNS/TNS 为 -0.203 ns/-0.381 ns，最差路径为 cfg_is_fp16_d2_reg_1_/Q -> mac_out_data_reg_32_/D；hold 视图 functional_hold_ff 的 WNS/TNS 为 +0.050 ns/+0.000 ns，最差路径为 res_tag_b1_d1_reg_30_/Q -> res_tag_b1_d1_reg_30_/D。ECO 前 DRV 计数为 max_transition=0、max_capacitance=0、max_fanout=431，DRC=6949，connectivity=0。允许修改的 ECO cell 上限为 6；若新增 ECO 实例，必须采用确定性命名 SFT_ECO_SETUP_003_<SETUP|HOLD>_<ordinal>，其中 role 必须匹配修复方向，ordinal 从 1 连续递增且不得复用；以下是从同一 post-route DB 解析并经双重放一致性校验的全部目标证据：
目标 1: role=setup_primary; timing=late; slack=-0.178 ns; launch_clock_pin=cfg_is_wg_d2_reg_1_/CK; endpoint=mac_out_data_reg_24_/D; net=FE_RN_2; driver_pin=SFT_ECO_SETUP_003_PATH_2/Y; driver_inst=SFT_ECO_SETUP_003_PATH_2; driver_ref=DLY4_X2M_A9TR40; local_cells=[SFT_ECO_SETUP_003_PATH_2(ref=DLY4_X2M_A9TR40),SFT_ECO_SETUP_003_PATH_1(ref=DLY4_X2M_A9TR40),U23988(ref=OAI21_X1M_A9TR40)]
目标 2: role=setup_primary; timing=late; slack=-0.203 ns; launch_clock_pin=cfg_is_wg_d2_reg_1_/CK; endpoint=mac_out_data_reg_32_/D; net=FE_RN_4; driver_pin=SFT_ECO_SETUP_003_PATH_4/Y; driver_inst=SFT_ECO_SETUP_003_PATH_4; driver_ref=DLY4_X2M_A9TR40; local_cells=[SFT_ECO_SETUP_003_PATH_4(ref=DLY4_X2M_A9TR40),SFT_ECO_SETUP_003_PATH_3(ref=DLY4_X2M_A9TR40),U10956(ref=NAND2_X1B_A9TR40)]
请给出针对这些真实对象的最小化 Innovus Tcl，不得修改 SDC、放松时钟/I/O 约束或添加 false path、multicycle path、disable timing；修复 Tcl 只需完成 ECO、增量摆放与 ECO 布线，独立 replay harness 将在执行后复查 setup、hold、DRV、DRC 和 connectivity，并要求两个时序方向 WNS 均至少为 +0.010 ns。
```

## 4. Probe / 注入 Tcl

### 4.1 规范入口 `inject.tcl`（原文）

```tcl
# Injection oracle for SETUP_003; never include this file in SFT messages.
if {![llength [info commands ::sft::apply_injection]]} {
    error "pilot_runtime.tcl was not loaded"
}
::sft::apply_injection
```

这个文件故意只是运行时入口：所有 fail-closed 校验和冻结参数都在同 case 的 `pilot_runtime.tcl` 与 `case_config.tcl` 中。它不是可以脱离 replay harness 单独执行的脚本。

### 4.2 冻结注入动作的核心 Tcl 展开

```tcl
# 核心设计修改展开；fail-closed 对象/命名/Liberty 校验与报告采集仍由 pilot_runtime.tcl 执行。
ecoAddRepeater -term mac_out_data_reg_24_/D -cell DLY4_X2M_A9TR40 -name SFT_ECO_SETUP_003_PATH_1
ecoAddRepeater -term mac_out_data_reg_24_/D -cell DLY4_X2M_A9TR40 -name SFT_ECO_SETUP_003_PATH_2
ecoAddRepeater -term mac_out_data_reg_32_/D -cell DLY4_X2M_A9TR40 -name SFT_ECO_SETUP_003_PATH_3
ecoAddRepeater -term mac_out_data_reg_32_/D -cell DLY4_X2M_A9TR40 -name SFT_ECO_SETUP_003_PATH_4
setEcoMode -batchMode false
refinePlace -eco true
ecoRoute -target
```

上面的展开来自本 case 的真实 Innovus log 与 `injection_provenance.json`；为便于审核只保留设计修改和增量物理收敛命令，不替代规范入口，也省略 runtime 的只读检查与报告命令。

| # | 动作 | 目标 term | 最终注入 cell | 实际实例 | 初始 scaffold |
|---:|---|---|---|---|---|
| 1 | 数据延迟注入 | `mac_out_data_reg_24_/D` | `DLY4_X2M_A9TR40` | `SFT_ECO_SETUP_003_PATH_1` | `—` |
| 2 | 数据延迟注入 | `mac_out_data_reg_24_/D` | `DLY4_X2M_A9TR40` | `SFT_ECO_SETUP_003_PATH_2` | `—` |
| 3 | 数据延迟注入 | `mac_out_data_reg_32_/D` | `DLY4_X2M_A9TR40` | `SFT_ECO_SETUP_003_PATH_3` | `—` |
| 4 | 数据延迟注入 | `mac_out_data_reg_32_/D` | `DLY4_X2M_A9TR40` | `SFT_ECO_SETUP_003_PATH_4` | `—` |

### 4.3 注入 Tcl 做了什么

- 数据侧在 2 个 late endpoint 前串入 4 个冻结 delay cell，推迟 data arrival，从干净基线定量制造 setup 负裕量。
- `::sft::apply_injection` 还会执行精确对象存在性、cell 安全性、串联拓扑、目标覆盖和命名检查，随后增量摆放/目标布线并在指定 MMMC view 中实测 WNS/TNS；只有第一次冻结动作命中窗口才可进入 Gold replay。

## 5. Gold Fix Tcl（原文）

```tcl
# Concrete Gold timing ECO for SETUP_003.
# Deterministically generated from reports/resolved_targets.tcl; no constraints are modified.
setEcoMode -batchMode true
ecoChangeCell -inst SFT_ECO_SETUP_003_PATH_1 -cell BUF_X2M_A9TR40
ecoChangeCell -inst SFT_ECO_SETUP_003_PATH_2 -cell BUF_X2M_A9TR40
ecoChangeCell -inst SFT_ECO_SETUP_003_PATH_3 -cell BUF_X2M_A9TR40
ecoChangeCell -inst SFT_ECO_SETUP_003_PATH_4 -cell BUF_X2M_A9TR40
ecoChangeCell -inst U23988 -cell OAI21_X3M_A9TR40
ecoChangeCell -inst U10956 -cell NAND2_X3B_A9TR40
setEcoMode -batchMode false
refinePlace -eco true
ecoRoute -target
```

### 5.1 修复 Tcl 做了什么

- 四个 `DLY4_X2M_A9TR40` 注入实例改成 `BUF_X2M_A9TR40`，同时恢复两个 endpoint 的数据路径速度。
- 再把 `U23988` 加强到 `OAI21_X3M_A9TR40`、`U10956` 加强到 `NAND2_X3B_A9TR40`，补足 hard case 的 setup 裕量。
- `setEcoMode -batchMode true/false` 将 cell 级修改作为一个 ECO 批次提交；`refinePlace -eco true` 只做增量合法化/摆放，`ecoRoute -target` 只收敛 dirty nets。

修复代码没有 `source` runtime helper，也没有修改 SDC、clock、I/O delay、false path、multicycle path 或 disable timing；它只操作 instruction 已暴露的局部设计对象并完成增量物理收敛。

## 6. 实测修复结果

| 方向 | Innovus 修复前 WNS/TNS (ns) | Innovus 修复后 WNS/TNS (ns) | PrimeTime 修复后 WNS/TNS (ns) | PT |
|---|---:|---:|---:|---|
| Setup | -0.203 / -0.381 | +0.051 / +0.000 | +0.192988 / +0.000000 | `PASS` |
| Hold | +0.050 / +0.000 | +0.050 / +0.000 | +0.045089 / +0.000000 | `PASS` |

PrimeTime `R-2020.09-SP4` 使用 post-ECO gate netlist、对应 corner SPEF 和传播时钟独立复核；setup 用 max、hold 用 min。跨工具 slack 数值不要求逐位相同，但两边都必须满足 `WNS >= +0.010 ns`、`TNS = 0`。

| 物理/完整性检查 | 修复前 | 修复后 | 结论 |
|---|---:|---:|---|
| max_transition | 0 | 0 | 无回归 |
| max_capacitance | 0 | 0 | 无回归 |
| max_fanout | 431 | 431 | 无回归 |
| DRC total | 6949 | 6949 | 按类别无回归 |
| Connectivity | 0 | 0 | PASS |

修复后 DRC 分类为：CUTSPACING=1, MAR=1, NSMETAL=29, SHORT=2682, SPACING=4226, VIAENCLOSURE=10。约束哈希未变、功能审计通过、PrimeTime crosscheck 通过；Innovus replay 为 2 次且 `deterministic=true`。

## 7. 证据入口

- [instruction.txt](../cases/SETUP_003/instruction.txt)
- [inject.tcl](../cases/SETUP_003/inject.tcl)
- [fix.tcl](../cases/SETUP_003/fix.tcl)
- [metrics.json](../cases/SETUP_003/metrics.json)
- [injection_provenance.json](../cases/SETUP_003/reports/injection_provenance.json)
- [resolved_targets.tcl](../cases/SETUP_003/reports/resolved_targets.tcl)
- [primetime_crosscheck.json](../cases/SETUP_003/primetime_crosscheck.json)
- [manifest.json](../cases/SETUP_003/manifest.json)
- [Innovus log](../cases/SETUP_003/logs/innovus.log)

## 8. RL / Benchmark 可验证 PASS

本 case 的候选修复不需要复现 Gold Tcl；允许在 `6` 个 cell 预算内给出其他局部方案。
官方判定必须使用 [共享 PASS 标准](../BENCHMARK_PASS_CRITERIA.md) 和 trusted harness，不能信任候选自行生成的 metrics。

```sh
python3 tools/check_benchmark_pass.py \
  --reference-case pilot_10/cases/SETUP_003 \
  --result-dir /path/to/trusted_candidate_result/SETUP_003 \
  --profile official
```

只有全部 hard gate 通过时才输出 `BENCHMARK_PASS` 和 `binary_reward=1.0`；任一 timing、物理、功能、
约束、replay、PrimeTime 或 evidence-integrity gate 失败均为 0。
