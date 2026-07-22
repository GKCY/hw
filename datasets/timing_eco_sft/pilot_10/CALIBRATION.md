# Pilot-10 real-tool calibration contract

本文定义 `catalog.json` 与 `templates/pilot_runtime.tcl` 的校准和 Gold replay
边界。当前十条 case 的注入参数均已由一次 fresh real-tool candidate 校准并标为
`FROZEN`。这只表示单次注入命中了目标窗口，不代表 Gold 已通过；两个 fresh Innovus
replay、完整物理证据和 PrimeTime crosscheck 仍须全部通过。任何参数改动都必须重新走
本文的 NOT_GOLD 冻结流程；静态检查不能替代 Innovus/PrimeTime 证据。

## 1. 基线前提

只接受由 `validate_benchmark_baseline.py` 生成的 qualified bundle。bundle 必须包含：

- `qualification.json`，schema 为 `smic40_baseline_qualification.v1`，status 为
  `QUALIFIED_CANDIDATE`；
- Gold prepare 时，`qualification.json.source_artifacts` 必须分别包含
  `setup_liberty_ss` 与 `hold_liberty_ff`，每项记录真实 qualified Liberty 的
  SHA256、正整数 byte count 与绝对 `source_path`；旧 bundle 若缺失该字段只能用于
  显式 NOT_GOLD calibration；
- `QUALIFIED_CANDIDATE` typed marker；
- setup/hold WNS 均至少 `+0.020 ns`，TNS 均为 0；
- `gold_status=false`、`signoff_eligible=false`、
  `technology_classification=derived_non_signoff`。这表示真实 Innovus qualification
  已完成，但该 OA 派生技术适配器不是 foundry signoff deck；
- `base.enc`、非空 `base.enc.dat/`、`manifest.tcl`、`restore.tcl`、`setup.tcl`、
  `cds.lib`。

host 在 prepare 时记录 baseline tree、catalog、qualification 和 GNU checksum
manifest 的 SHA256。同步到 guest 后，必须先用 `sha256sum --check --strict` 验证每个
baseline 文件，再启动 Innovus。runtime 将这些真实 hash/status 写入
`reports/baseline_guard.json` 和 `reports/injection_provenance.json`。任一值缺失、格式
错误或状态不匹配都会 fail closed。每个 replay 的 `run_status.json` 还必须绑定相同的
四个 hash、正确 replay index、已 fetch 状态、`GOLD_REPLAY` mode、Gold eligibility
与 typed success marker；finalizer 不把 process exit 0 或 marker 单独视为通过。

每个 trial/replay 都恢复同一 immutable checkpoint；不得接续上一个 ECO DB，也不得
回写 baseline。guest run/replay 目录必须是新目录。

## 2. 两种严格分离的运行模式

### 2.1 NOT_GOLD calibration

未冻结 case 只能显式准备和运行一次：

```bash
python3 datasets/timing_eco_sft/tools/run_pilot.py prepare \
  --baseline-bundle /path/to/baseline_qualified \
  --run-id calibrate-SETUP-001-try-01 \
  --case SETUP_001 \
  --allow-unfrozen-calibration

python3 datasets/timing_eco_sft/tools/run_pilot.py run \
  --run-dir /path/to/calibrate-SETUP-001-try-01 \
  --allow-unfrozen-calibration
```

该模式强制一个 fresh replay，只执行：restore、baseline guard、target resolution、
一次 candidate injection、ECO legalization/route、测量和 calibration typed evidence。
`apply_injection` 完成后立即写 NOT_GOLD marker 并退出；不会生成只供 Gold replay 使用的
before timing/DRV/DRC/connectivity reports、两 view SDC snapshot 或 violating
checkpoint/netlist/SPEF。随后已存在的最小冻结证据包括：

- `reports/injection_provenance.json`，status=`calibration_candidate`，含 setup/hold
  实测 WNS/TNS；
- `reports/violation_locality.json`，即使 candidate miss 也保存 selected/violating
  endpoint、每个 selected endpoint 的 post-injection exact slack、
  cardinality/locality/coverage/headroom/slack-consistency 布尔值与失败原因；
- scope 合格时写 `reports/diagnostic_context.json`；
- `reports/calibration_status.json` 和根目录 `NOT_GOLD_CALIBRATION`。

它不执行 repair，不写 `SFT_CASE_PASSED`，也不生成或伪装 Gold physical evidence，
不能进入 finalizer。冻结工具只消费上述 typed evidence、baseline/catalog 绑定和
`resolved_targets.tcl`；完整 before/after physical evidence 仍由冻结后的两次 fresh Gold
replay 生成。普通 prepare/run 会在启动远端命令前拒绝 `PROBE_REQUIRED` case。

若需要换 step/count/rank，每个新 candidate 必须用新 run-id 从 fresh baseline 再跑
一次；禁止在一个 DB 中累计尝试。catalog 已删除 `max_attempts` 和顶层共享
`drive_steps`/`delay_cells`。

NOT_GOLD calibration 在执行注入前也会只读解析完整 repair action：精确 resize step、
Liberty 等价 family 和目标方向都必须可行，之后才允许 calibration-only return。这样
不会冻结一个能制造违例、却没有合法 Gold repair 的 candidate；该检查不修改设计。

### 2.1.1 用实测证据冻结 candidate

不要根据 `NOT_GOLD_CALIBRATION` marker 手工改状态：candidate 即使 miss 窗口也会写
这个 marker。host 侧冻结工具会读取 fetched `replay_1` 的 typed evidence，并在任一
证据缺失或不一致时 fail closed；它自身不调用 Innovus、PrimeTime 或其他 EDA：

```bash
python3 datasets/timing_eco_sft/tools/freeze_calibration.py \
  --case SETUP_001 \
  --calibration-case-dir \
    datasets/timing_eco_sft/pilot_10/work/calibrate-SETUP-001-try-01/cases/SETUP_001
```

默认不修改 `catalog.json`，而是写同目录的
`catalog.frozen-SETUP_001.json`；若要覆盖输入 catalog，必须显式加 `--in-place`，也可用
`--output /new/path/catalog.json` 指定新文件。已有默认输出不会被覆盖。

冻结前会交叉检查 prepared catalog 与 baseline hash、非 dry-run 的 fetched
`run_status.json`、`baseline_guard.json`、`calibration_status.json`、一次且仅一次的
`injection_provenance.json` action/parameter、两方向实测 WNS/TNS、target window、
opposite margin、`violation_locality.json` 的 exact cardinality/locality/coverage、
selected endpoint slack 的结构/完整集合/负端点/WNS/TNS 一致性、
`diagnostic_context.json` 的逐 endpoint slack 与安全解析的 `resolved_targets.tcl`。
除其他 case 先前由同一
工具冻结的 status/clock-reference/data-delay-reference 外，输入 catalog 必须与
calibration 时的 catalog 一致。旧 prepared catalog 可以没有
`setup_parameters.delay_cell_reference`；freezer 会把字段缺省、probe placeholder 和
先前 stage 已冻结的值统一归一化后再比较，其余 candidate 内容仍须完全一致。

`HOLD_003`/`MIXED_002` 的 `clock_cell_reference` 只从实测
`capture_clock_injection` action 的 `cell` 字段取得，不接受命令行猜值。action 数量必须
与正数 `clock_cell_count` 完全一致；多级 action 必须使用同一个安全 reference、同一个
capture clock pin，并位于唯一 early endpoint 的 capture hierarchy 中，才能冻结该
reference。freezer 不按 case ID 推断该规则；任何使用
`local_capture_clock_delay` 或 `mixed_data_delay_and_capture_skew` 的 case 都执行相同的
clock-reference 归一化、证据校验与冻结输出，其他 strategy 出现 clock action 则失败。
对于 `insert_data_delay`/`mixed_data_delay_and_capture_skew`，freezer 还要求全部
`data_delay_injection` action 使用同一个安全 cell reference，并把该实测值写入本 case
的 `setup_parameters.delay_cell_reference`；它不从全局 candidate 顺序猜测冻结值。工具输出
`gold_replays_completed=false`；冻结 catalog 以后仍必须重新 prepare，并从 immutable
baseline 完成两个 fresh Gold replay。

### 2.2 FROZEN Gold replay

当且仅当一次 real-tool candidate 满足全部 injection gate 后，才可把该 case 的
`calibration_status` 改为 `FROZEN`。capture-clock case 还必须把
`clock_cell_reference` 从 `PROBE_REQUIRED` 改为实测的准确 cell 名；setup data-delay
injection case 必须同样保存实测的 `delay_cell_reference`。Gold runtime 严格使用 case-local
reference，后续为其他 case 调整全局 `delay_candidates` 的顺序不会改变已经冻结的注入。
凡 repair strategy 为 `insert_data_delay`、`insert_delay_and_downsize`、
`setup_then_hold` 或 `coordinated_setup_hold`，还必须在 `repair` 中保存独立的
`delay_cell_reference` 与 `delay_cells_per_endpoint`。前者必须是安全 exact Liberty cell，
后者必须为正整数；它们不是 setup injection reference 的隐式复用。

冻结后，用普通 prepare/run 启动 catalog 规定的两个 fresh replay；不得带
`--allow-unfrozen-calibration`。两次 replay 都必须独立通过，且 finalizer 要比较：

- resolved target tuple；
- injection provenance 的唯一 one-shot parameter/action；
- concrete repair Tcl 与 changed-cell/native diff；
- setup/hold 两份 active SDC；
- normalized fixed netlist；
- WNS/TNS（1 ps 容差）和 DRV/DRC/connectivity。

只冻结第一次就命中窗口的完整 action。不存在“第 N 次累计命中后把该 DB 当 Gold”的
路径。

## 3. target、locality 与 opposite timing

target resolution 采用显式排序键：

```text
(slack numeric, endpoint, beginpoint, driver_inst, net)
```

同时对 endpoint、driver 和 data net 去重；mixed 的 setup/hold target 也必须互不
重叠。hierarchy 只匹配实际要修改的 endpoint/driver，不用 beginpoint 偶然命中。
同一 timing/hierarchy class 的 selector 使用半开 rank 区间
`[stable_rank, stable_rank+count)`；任意两个区间重叠都会在 prepare 前失败，相邻区间
可以共存。

当前 selector seed 已按 qualified baseline 的 r3 只读 probe 与 runtime 的
`max_paths=1000` 对齐。虽然 probe 为诊断覆盖收集了 2000 条 timing path，selector
可用性只按 `source_path_index < 1000` 计算：late 的
`exp/pipeline/top_tree/multiplier` 去重候选数分别为 `256/578/744/0`，early 分别为
`208/400/792/0`。因此十条 candidate 不再引用 multiplier；这只证明 selector 在
runtime 搜索窗内可解析，不代表注入已命中 WNS 窗口，也不改变
`PROBE_REQUIRED`/NOT_GOLD 状态。需要 resize 的 seed 还用 `drive_families.tsv` 核对了
精确步进：`SETUP_002` 的 late pipeline rank 0/1 可 downsize 2 step，
`MIXED_001` 的 early pipeline rank 16/17 可 upsize 2 step。最终对象仍以每次 fresh
restore 后写出的 `resolved_targets.tcl` 为准。

注入后重新收集所有负 slack endpoint，不能复用 restore 后的旧 slack：

- setup/hold 先在各自的 late/setup、early/hold view 收集完整负 slack path，并按 endpoint
  保留最差的实测 slack；10k path 截断仍直接 fail closed；
- `selected_endpoints` 若已出现在该注入后负 slack 字典中，直接复用同一次全量查询得到的
  endpoint worst slack。仅对字典中缺失的 selected endpoint 重新执行
  `report_timing -collection -to <exact-pin> -max_paths 1`；因此 opposite direction 的非负
  slack 仍有 exact pin/view 实测值，负 path 查询意外漏项也会被后续一致性检查拒绝。结果按
  endpoint 排序写入 `selected_endpoint_slacks:[{endpoint,slack_ns}, ...]`，任何 selected
  endpoint 都不得省略；
- 每个 selected slack 必须为有限数值、endpoint 必须唯一且完整；其负值 endpoint 集合必须与
  `violating_endpoints` 相等，required direction 的最小值/总和必须分别复现 WNS/TNS；
- required timing direction 的 `violating_endpoints` 必须与 `selected_endpoints`
  集合完全相等；
- endpoint 数必须等于 selector 的冻结 count，cluster 不得退化；
- setup-only case 的 hold WNS 必须至少 `opposite_wns_min_ns`（当前 10 ps）、TNS=0、
  无负 endpoint；hold-only 的 setup 同理；
- mixed case 的 setup/hold 两方向分别满足自己的 target interval 和 exact locality。

Gold 的 `reports/diagnostic_context.json` 使用
`timing_eco_diagnostic_context.v1`，仅含 case/design/max budget 与 runtime-resolved
target 的 role、timing、endpoint、beginpoint、fix 前实测负 slack、net、driver pin、
driver instance/ref。`driver_*` 是注入并 ECO route 后重新查询的 immediate data
driver，不是 restore 时的旧 driver。每个 target 还含：

- `original_driver:{inst,ref}`：baseline resolve 得到的原始 data driver；
- `local_cells:[{inst,ref}, ...]`：从当前 endpoint immediate driver 沿单输入 data
  chain 向上游遍历，最多 8 个 cell，必须终止于 original driver。

`original_driver` 只保留在 evidence artifact 中用于链完整性审计，不得写入 training
instruction。两级 delay repair 所引用的两个当前 DLY 与上游 cell 仍都能由
instruction 的 `local_cells` current-DB 事实推出，但不会被标注为注入前对象。local
cell 名生成时使用中性 `PATH`/`CLOCKPATH` tag，禁止实例名包含
INJECT/action/oracle/strategy 等 hidden 术语。context 不含 hidden action、parameter
或 expected replacement ref；任何 concrete fix 引用的既存 instance 若不在
`local_cells` 中，runtime 会拒绝生成答案。exporter 还会 fail-close 拒绝训练消息中的
`original_driver_inst=`/`original_driver_ref=` 字段。

## 4. mixed 与 capture-clock case

mixed case 的参数分开冻结：

- `setup_parameters` 只描述 late 方向 action；
- `hold_parameters` 只描述 early 方向 action；
- runtime 只执行一次，两组参数没有共享 step，也没有累计 attempt。

`HOLD_003` 和 `MIXED_002` 的 capture-clock 部分各只选择一个 early endpoint，并只修复
该 endpoint。`clock_cell_count` 可以是任意正整数；runtime 在同一个已显式分类的 capture
clock pin 前串联该数量的同 reference clock cell，逐项记录 action，并审计实际 serial
chain。不能“只 skew 一个 sink，却声称注入并修复 8 个 endpoint”。如果将来改为 shared
clock branch，必须新增真实 fanout/branch 证明并重新校准，不能只增加 selector count。

clock library candidates 会跨 glob 收集、按字典序去重。`PROBE_REQUIRED` 模式只把
排序后的首项作为可测 candidate 并明确标 NOT_GOLD；Gold 必须校验 catalog 中冻结的
准确 reference 仍存在于排序集合。clock cell 还必须由 Liberty 证明为单输入、单输出、
非反相。

setup data-delay injection 的 probe catalog 可缺省
`setup_parameters.delay_cell_reference`，也可写 `PROBE_REQUIRED`；此时 runtime 从全局
`delay_candidates` 选择第一个实际存在的候选并明确标 NOT_GOLD。冻结后字段必须是安全的
实测 exact reference，runtime 不再查询全局顺序。这个 case-local 约束只作用于 violation
injection。hold repair 使用另一组 case-local `repair.delay_cell_reference` 与
`repair.delay_cells_per_endpoint`；`PROBE_REQUIRED` case 可暂时缺省这两个字段，但若提供
必须成对且合法，`FROZEN` case 则强制两者存在。Gold runtime 对每个 selected early
endpoint 串联准确数量，且绝不回落到全局 `delay_candidates` 首项。

## 5. repair 与物理证据

repair Tcl 只允许白名单中的物理 ECO/closeout 命令，禁止 timing exception、clock/
IO delay、analysis view 或 constraint mode 修改。

host catalog 校验会按确定性 operation 数计算 `max_eco_cells` 静态下界：delay insertion
按 `early endpoint 数 × delay_cells_per_endpoint`，并叠加 strategy 固有的 resize、setup
delay replacement 或 buffer operation。预算低于下界时在 prepare 前即失败；runtime 仍
逐项执行动态预算检查。

每个 Gold replay 必须保存：

- `constraint_setup_before.sdc` 与 `constraint_setup_after.sdc`：每次
  `write_sdc` 后仅把唯一精确的 `#  Generated on:` 整行替换为固定 marker，再做
  raw byte-identical 比较；该行缺失或重复即 fail closed；
- `constraint_hold_before.sdc` 与 `constraint_hold_after.sdc`：采用同一严格规范化，
  除上述唯一整行外不忽略任何字符或字节；
- `violating.enc` + 非空 `violating.enc.dat/`；
- `before.v`、`setup_before.spef`、`hold_before.spef`；
- `fixed.v`、`setup_after.spef`、`hold_after.spef`；
- before/after setup、hold、DRV、DRC、connectivity reports；
- `physical_no_regression.json`、`functional_audit.json`、checkDesign evidence。

finalizer 对 `.enc.dat/` 和 checkDesign 目录做无 symlink 的确定性 tree inventory/hash，
并在 canonical case 中保存 replay 1 与 replay 2 的完整目录证据。checkDesign 的 primary
ASCII report 必须证明 command/header 完整，并对五类 critical issue 给出显式 0；缺文件、
空目录或无法解析都不能推断为 clean。独立 PrimeTime setup/hold Liberty 还必须按 hash
与 byte count 精确绑定上面的 `setup_liberty_ss`/`hold_liberty_ff`，不能用同名替代库。

before 和 after connectivity 都必须显式为 0。DRV parser 必须分别得到
max_transition/max_capacitance/max_fanout 的唯一 typed count，after 不得增加。DRC
每一 category 和 total 都不得增加；report record 数必须与 typed total 对账，不能靠
手写 summary。所有 baseline、probe 和 Gold replay 的 DRC 采集固定使用
`verify_drc -limit 1000000`；parser 从唯一 `# Command:` header 复核该值，并拒绝
任何截断/提前停止标记或 `total >= 1000000` 的报告。

冻结注入在 legalization/route 后得到的 setup/hold WNS/TNS 与紧随其后的 before stage
属于同一设计状态和显式 MMMC view；runtime 可将这两个数值对缓存给 before stage，避免
重复的 summary collection 查询。但 `setup_before.rpt`/`hold_before.rpt` 仍必须由真实完整
`report_timing` 命令生成并进入 Gold evidence，finalizer 仍从报告独立重建数值；after stage
禁止使用缓存，必须从修复后的数据库重新实测 setup/hold。

DLY→BUF 不能只因两个 cell 都“非反相”就替换：输入 pin 名集合、输出 pin 名与每个
输出的 Liberty function 必须完全一致。普通 resize 也必须保持 drive flavor、pin
interface 与 Boolean function，且 exact step 存在，禁止 clamp。

通用 harness 若使用 native `optDesign -selectedTerms`，必须把 resolved endpoint 按确定顺序逐行写入
`reports/native_selected_terms.txt`，回读 byte-compare 后用
`optDesign ... -selectedTerms reports/native_selected_terms.txt -incr` 执行；不能把 GUI
selection 状态当作 `-selectedTerms` 参数，也不能把 native `optDesign` 包在
`setEcoMode -batchMode` 中。执行后统计完整 leaf-cell diff，数量不超过
`max_eco_cells`，并用该版本支持的 fanin API 证明所有 changed cell 都属于优化前 fanin
与优化后 fanin 的并集，从而容纳合法的新增/删除实例；API 无法证明时输出
`SFT_PROBE_REQUIRED` 并失败，不退化为全局优化。

真实 Innovus 21.10 Gold 运行中，SETUP_004 的
`optDesign -postRoute -setup -selectedTerms <file> -incr` 被
`IMPOPT-3337`/`IMPOPT-570` 拒绝。`HOLD_004` 的 selected-terms hold 形式虽可执行，
却在目标 endpoint 的优化前/后 fanin 并集之外改变了 3 个 leaf cell；加入
`-targeted` 后又因与 incremental/mode flow 不兼容而被 `IMPOPT-7277` 拒绝。该结果
不能满足本数据集的局部性 contract，因此不能通过放宽 fanin 或预算来保留 native。
最终 pilot 固定为 10 条 surgical + 0 条 native：`SETUP_004` 使用 surgical
replacement/upsize，`HOLD_004` 使用下表的两级局部 data-delay repair。通用 native
validator/fixture 仍保留，但不代表最终 catalog 含 native case。

## 6. 十条 candidate 的当前结构

| Case | 注入 candidate | repair 范围 |
|---|---|---|
| SETUP_001 | late exp rank 0：1 个 endpoint 插 1 个 `DLY2_X4M_A9TR40` | delay replacement + driver upsize，budget 2 |
| SETUP_002 | late exp rank 15：2 个 endpoint 各插 2 个 `DLY2_X4M_A9TR40` | 4 个 delay replacement + 2 个 driver upsize，budget 6 |
| SETUP_003 | late top-tree rank 2：2 个 endpoint 各插 2 个 `DLY4_X2M_A9TR40` | 4 个 delay replacement + 2 个 driver upsize，budget 6 |
| SETUP_004 | late top-tree rank 10：4 个 endpoint 各插 2 个 `DLY4_X4M_A9TR40` | 8 个 delay replacement + 4 个 driver upsize，budget 12 |
| HOLD_001 | early top-tree rank 51：capture path 插 1 个 `DLYCLK8S8_X1B_A9TR40` | data path 插 1 个 `DLY4_X0P5M_A9TR40`，budget 1 |
| HOLD_002 | early top-tree rank 487：capture path 插 2 个 `DLYCLK8S6_X1B_A9TR40` | data delay + driver downsize，budget 4 |
| HOLD_003 | early top-tree rank 142：capture path 插 2 个 `DLYCLK8S8_X1B_A9TR40` | data path 插 2 个 `DLY4_X0P5M_A9TR40`，budget 2 |
| HOLD_004 | early top-tree rank 233：capture path 插 2 个 `DLYCLK8S6_X1B_A9TR40` | surgical data path 插 2 个 `DLY4_X0P5M_A9TR40`，budget 2 |
| MIXED_001 | late exp rank 17：2 个 endpoint 各插 2 个 `DLY2_X4M_A9TR40`；early top-tree rank 319：capture path 插 2 个 `DLYCLK8S6_X1B_A9TR40` | coordinated setup replacement + 1 个 `DLY4_X0P5M_A9TR40` hold delay，budget 6 |
| MIXED_002 | late top-tree rank 4：3 个 endpoint 各插 3 个 `DLY4_X4M_A9TR40`；early top-tree rank 400：capture path 插 2 个 `DLYCLK8S8_X1B_A9TR40` | 9 个 setup replacement + 2 个 `DLY4_X0P5M_A9TR40` hold delay，budget 11 |

表中参数均已由 fresh NOT_GOLD real-tool candidate 冻结；这不是 Gold replay 通过声明。
任何 rank/count/reference 或 operation 数改动都会使原校准绑定失效，必须用新 fresh trial
重测，不得放宽 Gold gate。

## 7. 仍需 Innovus 21.10 real probe 的 API

runtime 对以下版本相关点均为 fail closed，并打印 `SFT_PROBE_REQUIRED`：

- Innovus 21.10 的布尔属性按有序 alias 组解析：首个可读的显式
  true/false 即为最终值（例如 `is_clock_pin=false` 不再继续查询该版本不支持的
  `clock`，避免 `IMPDBTCL-248` 消息洪泛）；首个可读值若不是布尔值，或整组
  属性均不可读，安全过滤一律 fail closed。power、ground、clock 是三个独立
  net 分类，分别验证，不能合并成一个 alias 组；capture clock pin 也必须有
  显式 true 分类，不使用 pin 名字正则兜底；

- Innovus 21.10 的 net 常量分类按 `get_db $net .constant` 枚举解析：
  只有 `no_constant`/`none`/`false`/`0` 这组显式 false 值可作为普通 data
  net，其他非空枚举均拒绝；属性缺失或
  空值也拒绝。`is_power`/`is_ground`/`is_clock` 仍按实机证据的布尔属性处理；

- `report_timing -collection` 的 endpoint/beginpoint/slack property；
- `get_lib_pins`/Common-UI Liberty object schema；
- `ecoAddRepeater` 连续插入是否形成可审计串联链；
- `refinePlace -eco true`、`ecoRoute`；
- `write_sdc -view` 是否生成只含一个精确 `#  Generated on:` volatile header、
  且该整行固定替换后其余内容可 raw byte-compare 的文件；
- `saveDesign` 的 `.enc/.enc.dat` 自包含输出；
- `rcOut -spef ... -view ...`；
- `optDesign ... -selectedTerms <fileName> -incr`；
- `all_fanin -to <pin> -only_cells` 的 native before/after locality；
- `report_constraint -all_violators` 与 `verify_drc` 的 typed report format。

本次静态/runtime 加固没有启动 EDA，也没有把这些 probe 标为通过。只有真实 Innovus
log/report、两个 fresh replay 与独立 PrimeTime 双 SDC/SPEF crosscheck 都通过后，case
才能进入最终 10 条 SFT JSONL。
