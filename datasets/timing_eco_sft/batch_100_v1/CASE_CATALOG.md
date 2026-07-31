# Mock LEF Timing ECO Batch 100 v1：Case Catalog

## 1. 文档定位

本文只规划首批 100 条正向 setup ECO case，编号为 `B1_CASE_001`～
`B1_CASE_100`。所有条目的状态均为 `planned_unvalidated`：尚未生成 Tcl、
checkpoint 或 timing 报告，尚未在 Innovus/PrimeTime 中验证，也不代表 PASS 或
signoff 结论。

本批固定为 setup-only、RVT-only、resize-rich。允许动作只有功能等价的组合逻辑
RVT cell resize；drive-strength 或 A/B/M flavor 的变化在当前仅有 RVT Liberty/LEF
的 collateral 中不解释为 Vt swap。Hold 只作为修复后的观察项，不是本批高置信标签
或准入 gate。`finish` 只表示已经完成有效修复和全局检查后的正常结束，不构造保持
违例后停止的任务。

本批不设计 hold-only、Vt swap、rollback、no-op、停止类、buffer insertion、
clock ECO 或 routing 优化任务。禁止修改 sequential、clock-tree、clock-gating、
macro 和约束对象。后续若接入 propagated clock 或 multi-Vt collateral，应另建
批次，不回写本批动作定义。

## 2. 字段、策略与统一契约

每张卡片中的字段含义如下：

- `形态`：`单关键锥`、`多 endpoint` 或 `Setup/DRV guardrail`。
- `候选层级`：只允许 `exp`、`pipeline`、`top_tree`；当前 `multiplier`
  候选池为空，不安排任何本批 case。
- `endpoint`：单关键锥和多 endpoint 卡片中的数量只统计计划构造为负 slack、且
  必须被修复闭合的目标 setup endpoint；正裕量保护对象必须在场景中另称
  `非目标 endpoint`，不得混入该数量。Guardrail 卡片的 `目标 endpoint` 同理，
  总相关数等于目标数加场景中明确列出的非目标数。
- `策略`：A = 局部关键弧 resize；B = 沿关键路径进行 slew-chain resize；
  C = 考虑共享负载、输入电容和分支关系的 resize；D = 分析后一次性选择安全的
  多约束组合 resize，不包含试错或 rollback。
- `难度/修改`：E 为 1～2 个预计有效修改，M 为 2～4 个，H 为 4～6 个。
- `目标`：相对 8 ns 时钟周期的 setup severity；为消除边界歧义，本批定义
  E 为 0.5%～2%（含 2%，40～160 ps），M 为大于 2% 且不大于 5%
  （`>160 ps` 且 `<=400 ps`），H 为大于 5% 且不大于 8%
  （`>400 ps` 且 `<=640 ps`）。原始范围在 2%/5% 处重叠；本批卡片采用上述
  tie-break，分别将 2.00% 归 E、5.00% 归 M。
- `场景`：由卡片给出的拓扑/关键位置、电气坏态和诊断重点，与下述隐藏构造档共同
  组成该 case 的隐藏违例构造思路；这些注入细节只用于后续制题，不进入模型题面。
- `直接逆操作`：`允许` 表示后续构造可让一个被 downsize 的 cell 直接恢复原尺寸；
  `不允许` 表示注入动作的完整直接逆操作虽然会回到 timing-clean baseline，但不得
  成为唯一或唯一被验证的解；必须另行验证卡片所述安全解。其 repair set 不得等于
  injection set，至少一个 injected cell 不直接恢复，且至少一个 repair cell 不属于
  injection set。即使标记为允许，也仍需满足完整诊断和全局门禁。

隐藏构造档由每张卡片元数据显式给出：

- `I0`：仅用于 10 张允许直接逆操作的卡；按预计有效修改数对一处或多处所述热点
  cell 做 RVT downsize，允许其中至少一个 cell 直接恢复原尺寸，也允许完整 inverse
  作为安全解。预计 2 个修改的 `B1_CASE_014`、`B1_CASE_036` 因而使用两处注入。
- `IA`：A 类错位局部构造；在所述局部关键段的相邻或同锥非 repair cell 上做
  一处或多处分散 RVT 尺寸扰动，产生卡片所述热点；替代 repair set 聚焦高敏感关键弧。
- `IB`：B 类链式构造；在链首上游或链内非完整 repair set 上分散做 RVT downsize，
  形成首个 slew 拐点和后续传播；替代解重新平衡卡片所述链级。
- `IC`：C 类共享构造；在共享段与分支段使用不对称、且不等同于 repair set 的
  RVT 尺寸扰动，形成负载/输入电容/分支关系；替代解按卡片所述共享关系选点。
- `ID`：D 类约束耦合构造；在目标锥内分散做与一次性 repair set 不同的 RVT
  尺寸扰动，同时保留卡片所述 setup/DRV 或分支权衡；替代解必须一次提交并通过
  所有 guardrail，不设计试错或 rollback。

`IA`～`ID` 的完整 injection inverse 不是被禁止的物理操作，而是不得作为唯一答案；
后续工具校准必须证明所列替代 repair set 独立闭合目标 setup。若做不到，该候选直接
拒绝，不得把构造档降级为 `I0`。

所有卡片显式引用以下统一契约：

- `G0`（后续准入）：实测 `cell_delay_fraction >= 0.70`、
  `net_delay_fraction <= 0.30`；所有修改可合法化；主要收益不依赖重新布线；
  每个目标 setup endpoint 必须闭合到非负 slack，且全局 setup 与 DRV 均无回归。
  Max fanout 统一记录，但不作为主要造题轴，因为 resize 不改变连接拓扑。
- `P0`（修改边界）：只允许功能等价的组合逻辑 RVT resize；禁止修改 sequential、
  clock-tree、clock-gating、macro 和约束对象，也禁止新增 buffer、clock ECO 与
  routing 优化。
- `H0`（观察规则）：修复前后记录 hold 变化，但 hold 不作为本批标签或准入 gate；
  若出现明显异常，仅保留证据并标注供后续人工分析，不以 hold 单项决定本批准入，
  也不据此生成高置信结论。

## 3. 规划分布

| 任务形态 × 动作策略 | 简单 E | 中等 M | 困难 H | 合计 |
|---|---:|---:|---:|---:|
| 单关键锥 × A | 13 | 7 | 2 | 22 |
| 单关键锥 × B | 10 | 9 | 1 | 20 |
| 单关键锥 × C | 2 | 6 | 2 | 10 |
| 单关键锥 × D | 0 | 2 | 1 | 3 |
| 多 endpoint × A | 3 | 4 | 1 | 8 |
| 多 endpoint × B | 2 | 4 | 2 | 8 |
| 多 endpoint × C | 1 | 3 | 2 | 6 |
| 多 endpoint × D | 0 | 2 | 1 | 3 |
| Setup/DRV guardrail × A | 2 | 3 | 0 | 5 |
| Setup/DRV guardrail × B | 1 | 4 | 2 | 7 |
| Setup/DRV guardrail × C | 1 | 2 | 1 | 4 |
| Setup/DRV guardrail × D | 0 | 4 | 0 | 4 |
| **合计** | **35** | **50** | **15** | **100** |

汇总为 55 条单 endpoint/单关键锥、25 条多 endpoint/共享逻辑锥和 20 条
setup 修复带全局 setup/DRV guardrail；难度为 35 E、50 M、15 H；策略为
35 A、35 B、20 C、10 D。

## 4. Case 卡片

### 4.1 单关键锥 × A（22）

#### B1_CASE_001

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 1 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：I0；直接逆操作：允许。
- 场景：关键弧位于 capture 前一级反相/缓冲型组合 cell；诊断聚焦该弧的 cell delay 与输出 slew。通过单点 downsize 形成约 0.75% / 60 ps setup 违例，同时保持 net delay 次要。
- 安全解与检查：恢复该 RVT cell 的合适驱动档位；检查相邻输入电容、目标 setup、全局 setup/DRV、max fanout 与 H0。后续必须满足 G0、P0。

#### B1_CASE_002

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 1 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：关键弧位于 launch 后第一段逻辑；诊断聚焦弱驱动造成的首级 slew 损失。按 IA 在同锥相邻的非 repair cell 上做尺寸扰动，并与既有局部负载组合出约 1.00% / 80 ps 违例；完整逆操作可回到基线，但所列首级 upsize 必须作为不同 repair set 独立闭合目标。
- 安全解与检查：upsize 首个高敏感度 RVT 组合 cell，并确认下一级输入 slew 改善；检查全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_003

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 2 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：关键弧位于路径中段的二输入逻辑，前后各有轻微 slew 损失；诊断聚焦单弧延迟和输入 pin 不平衡。构造约 1.25% / 100 ps 违例。
- 安全解与检查：适度 upsize 该门及其直接前驱中的敏感者，避免无收益的远端放大；检查新增输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_004

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 1 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：关键弧是 capture 近端 AOI/OAI 类复杂门的最慢输入弧；诊断聚焦 pin-to-pin cell delay，而非该网线长。按 IA 扰动目标慢输入的同路径上游，以及与目标支路共享前驱输出 net 的旁支首级 cell，通过旁支输入电容改变共享 net 负载，形成约 1.50% / 120 ps 违例。
- 安全解与检查：只提升复杂门的功能等价 RVT 驱动档位；复核非关键输入负载、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_005

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 1 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：I0；直接逆操作：允许。
- 场景：关键弧位于窄 fanout 的末级 mux；诊断聚焦弱化后上升沿 cell delay。直接 downsize 该 cell，目标约 1.75% / 140 ps。
- 安全解与检查：恢复原驱动档或选择等价且足够的相邻档；检查另一极性、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_006

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 2 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：路径中段的关键局部段由小驱动 NAND 与紧邻反相器组成；诊断需区分两条 cell arc 的门延迟与被放大的输入 slew。通过两个非相邻尺寸扰动形成约 2.00% / 160 ps 违例。
- 安全解与检查：upsize NAND 与紧邻反相器中的高收益组合；检查输入电容回推、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_007

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 1 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：关键弧位于路径中后段 XOR/XNOR 类 cell；诊断聚焦某一 data pin 的慢弧及输出 slew。构造约 0.50% / 40 ps 的边界型轻违例。
- 安全解与检查：仅放大该复杂门一个档位，证明收益来自 cell delay；检查其他输入锥、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_008

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 2 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：capture 前两级中只有倒数第二级具有高 cell-arc delay sensitivity，不存在连续 slew 恶化；诊断重点是避免误改末级低敏感 cell。以分散弱化形成约 0.875% / 70 ps 违例。
- 安全解与检查：upsize 高敏感级并小幅调整其直接前驱；检查两处新增输入电容向上游的回推及末级输入 slew、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_009

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 1 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：launch 近端数据路径上的组合使能门（非 clock-gating cell）的单一条件弧成为瓶颈；诊断聚焦数据逻辑弧而非 endpoint 近端。构造约 1.125% / 90 ps 违例。
- 安全解与检查：放大该使能组合门的 RVT 驱动档；检查其非目标分支的 setup、全局 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_010

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 2 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：路径中段的关键局部段由相邻反相极性对组成，只有两条局部 cell arc/负载匹配主导、没有持续 slew 传播；诊断聚焦两级电气匹配。构造约 1.375% / 110 ps 违例。
- 安全解与检查：选择两级协调的 RVT 尺寸而非最大化单级；检查级间电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_011

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 1 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：关键弧位于中段 mux 的选择输入；诊断需依据实际 sensitized arc 选择目标。按 IA 在 select-driver 的同路径上游及 mux 共享前驱上做错位尺寸扰动，配合既有弱门形成约 1.625% / 130 ps 违例。
- 安全解与检查：upsize 实际慢弧所在的 mux；检查选择/数据两类输入负载、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_012

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 2 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：关键弧是 reconvergence 后的首个门，但本 case 只有一个违规 endpoint；诊断聚焦汇合点 cell delay。构造约 1.875% / 150 ps 违例。
- 安全解与检查：upsize 汇合门和主导输入支路的末级；检查另一支路负载、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_013

- 元数据：`planned_unvalidated`；单关键锥；策略 A；E；预计 1 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：capture 近端末级 NOR 的上升沿孤立为最差弧；诊断聚焦常见串联 pMOS 路径造成的 rise/fall 不对称。构造约 1.25% / 100 ps 违例，避免把整条路径误判为链式问题。
- 安全解与检查：提升该 NOR 的功能等价驱动档；检查相反转换、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_014

- 元数据：`planned_unvalidated`；单关键锥；策略 A；M；预计 2 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：I0；直接逆操作：允许。
- 场景：中段复杂门及其紧邻驱动共同形成局部热点；诊断聚焦两条连续 cell arc。对热点门和其前驱各 downsize 一档，目标约 2.25% / 180 ps。
- 安全解与检查：恢复热点门与前驱的合适驱动档，共 2 处，禁止扩大到整条链；检查输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_015

- 元数据：`planned_unvalidated`；单关键锥；策略 A；M；预计 3 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：capture 近端三门小簇中一条 AOI 弧占主导，其余两级限制可得收益；诊断需做局部 sensitivity 排序。构造约 2.50% / 200 ps 违例。
- 安全解与检查：按收益选择 AOI 与相邻两级的中等档组合；检查电容回推、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_016

- 元数据：`planned_unvalidated`；单关键锥；策略 A；M；预计 2 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：launch 近端门和路径中段单弧各贡献明显 cell delay，但中间网络不主导；诊断聚焦两个离散热点。构造约 3.00% / 240 ps 违例。
- 安全解与检查：分别 upsize 两个热点，避免无差别链式放大；检查其扇出分支、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_017

- 元数据：`planned_unvalidated`；单关键锥；策略 A；M；预计 4 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：末端四级中有两个复杂门慢弧和两个配套驱动限制，关键性高度局部；诊断重点是识别有效四点。构造约 3.50% / 280 ps 违例。
- 安全解与检查：以两个主门为中心选择四个 RVT 尺寸，拒绝放大低敏感旁路；检查输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_018

- 元数据：`planned_unvalidated`；单关键锥；策略 A；M；预计 3 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：reconvergence 后的 mux 与前置两支路末级构成局部瓶颈；只有一个 endpoint 违规。诊断聚焦三个局部高敏感 cell arc，不以共享负载或输入电容权衡为主轴，目标约 4.00% / 320 ps。
- 安全解与检查：放大 mux，并在两支路各调整一个真实限制其输入 slew 的末级，共 3 处；检查非主导弧、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_019

- 元数据：`planned_unvalidated`；单关键锥；策略 A；M；预计 2 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：路径中段一对 logical effort、驱动需求和负载能力不匹配的相邻门出现 drive mismatch；诊断聚焦第一级输入电容和第二级输出 slew 的平衡。构造约 4.50% / 360 ps 违例。
- 安全解与检查：选取两级协调尺寸而非只最大化后级；检查前驱 slack、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_020

- 元数据：`planned_unvalidated`；单关键锥；策略 A；M；预计 4 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：launch 近端、中段和 capture 近端共四个离散 cell arc 累积成约 5.00% / 400 ps 违例，net delay 保持低占比；诊断需证明它们而非拓扑是主因。
- 安全解与检查：按 arc sensitivity 选择四个功能等价 RVT upsize；检查每处输入电容、所有 setup endpoint、DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_021

- 元数据：`planned_unvalidated`；单关键锥；策略 A；H；预计 5 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：长逻辑路径中存在五个彼此分离的高 cell-delay arc，中间网络均非主导；诊断需完成全路径热点排序。构造约 5.75% / 460 ps 违例。
- 安全解与检查：仅放大五个高敏感 cell，控制每个新增输入电容；检查全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_022

- 元数据：`planned_unvalidated`；单关键锥；策略 A；H；预计 6 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IA；直接逆操作：不允许。
- 场景：launch、中段、capture 近端各有两个局部慢弧，合计形成约 7.25% / 580 ps 违例；诊断需避免把它误归类为连续 slew-chain。
- 安全解与检查：对六个离散热点采用克制的分级 upsize；逐点复核收益和电容代价，并检查全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

### 4.2 单关键锥 × B（20）

#### B1_CASE_023

- 元数据：`planned_unvalidated`；单关键锥；策略 B；E；预计 2 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：launch 后两级形成短 slew-chain，第二级 cell delay 被首级差 slew 放大；目标约 0.75% / 60 ps。诊断聚焦 slew 传播而非单一最差弧。
- 安全解与检查：从链首到链尾匹配两级 RVT 尺寸；检查首级输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_024

- 元数据：`planned_unvalidated`；单关键锥；策略 B；E；预计 2 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：capture 前两级的慢 slew 连锁使末级延迟上升；构造约 1.00% / 80 ps 违例。诊断需选择链首而非只修末级。
- 安全解与检查：适度放大倒数第二级并匹配末级，检查向上游回推的负载、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_025

- 元数据：`planned_unvalidated`；单关键锥；策略 B；E；预计 1 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：I0；直接逆操作：允许。
- 场景：一个 downsize 的链首驱动使后续两级 slew 同时退化，目标约 1.25% / 100 ps；诊断需要从报告中追溯首个 slew 拐点。
- 安全解与检查：恢复链首 cell 的合适 RVT 尺寸；检查后级 slew、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_026

- 元数据：`planned_unvalidated`；单关键锥；策略 B；E；预计 2 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：路径中段三段链中首、末两级弱而中级充足，形成约 1.50% / 120 ps 违例；诊断重点是跨过低敏感中级识别有效点。
- 安全解与检查：upsize 首级与末级，保留中级尺寸；检查链首输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_027

- 元数据：`planned_unvalidated`；单关键锥；策略 B；E；预计 2 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：目标转换经 capture 近端两级反相链传播时，第 1 级输出 fall 与第 2 级输出 rise 的 slew 依次恶化，反向转换正常；构造约 1.75% / 140 ps 违例。诊断聚焦极性相关 slew-chain。
- 安全解与检查：为两级选择兼顾第 1 级 fall 与第 2 级 rise 的 RVT 驱动组合；检查相反转换、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_028

- 元数据：`planned_unvalidated`；单关键锥；策略 B；E；预计 2 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：复杂门输出差 slew 传至末级简单门，目标约 2.00% / 160 ps；诊断需区分复杂门固有 delay 与对后级的链式影响。
- 安全解与检查：upsize 复杂门并用适中末级尺寸接力；检查复杂门各输入负载、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_029

- 元数据：`planned_unvalidated`；单关键锥；策略 B；E；预计 1 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：路径中段一个链首 cell 的输出 slew 是唯一拐点，但注入位于其上游，直接撤销不构成唯一解；目标约 0.625% / 50 ps；诊断聚焦首个 slew 拐点。
- 安全解与检查：放大该链首 RVT cell，验证后续两级 delay 同步回落；检查全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_030

- 元数据：`planned_unvalidated`；单关键锥；策略 B；E；预计 2 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：capture 近端三门链仅前两级具备 resize 敏感度，目标约 0.875% / 70 ps；诊断需避免无效修改末级。
- 安全解与检查：匹配前两级尺寸以改善传入末级的 slew；检查前驱输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_031

- 元数据：`planned_unvalidated`；单关键锥；策略 B；E；预计 2 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：路径中段两级 mux/反相链的 data arc 出现轻度 slew 堆积，目标约 1.125% / 90 ps；诊断重点是实际 data arc 而非 select arc。
- 安全解与检查：协调 mux 与后级反相器尺寸；检查 select pin 电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_032

- 元数据：`planned_unvalidated`；单关键锥；策略 B；E；预计 2 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：launch 近端小门到中段复杂门的两级链产生约 1.375% / 110 ps 违例；诊断需发现前级 slew 对复杂门慢弧的放大。
- 安全解与检查：先 upsize 小门一档，再对复杂门做一次匹配的 RVT resize，共 2 处；检查负载回推、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_033

- 元数据：`planned_unvalidated`；单关键锥；策略 B；M；预计 3 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：capture 近端三门连续链的 slew 逐级恶化并形成约 2.25% / 180 ps 违例；诊断聚焦首个超出局部基线的 transition。
- 安全解与检查：从链首开始选择三级渐进尺寸，避免末级过度放大；检查输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_034

- 元数据：`planned_unvalidated`；单关键锥；策略 B；M；预计 2 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：路径中段四级链中第 1 与第 3 级为 slew 拐点，其余为传播受害者；构造约 2.50% / 200 ps 违例。诊断需基于增量收益选点。
- 安全解与检查：upsize 第 1、第 3 级组合 cell，观察整链 cell delay 回收；检查两处输入负载、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_035

- 元数据：`planned_unvalidated`；单关键锥；策略 B；M；预计 4 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：capture 近端四级链承担几乎全部新增 cell delay，目标约 3.00% / 240 ps；诊断需确认 net delay 不主导。
- 安全解与检查：对四级做平衡 resize，首级克制以限制回推电容；检查全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_036

- 元数据：`planned_unvalidated`；单关键锥；策略 B；M；预计 2 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：I0；直接逆操作：允许。
- 场景：链首复杂门与末级 cell 各被 downsize 一档；链首拖慢后面三段，末级形成额外尺寸失配，目标约 3.25% / 260 ps。诊断需同时定位源头和末端限制。
- 安全解与检查：恢复链首与末级 RVT cell 的合适尺寸，共 2 处；检查中间级无需修改、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_037

- 元数据：`planned_unvalidated`；单关键锥；策略 B；M；预计 3 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：路径中后段反相极性交替的五级链中，目标转换对应的第 1/3/5 级敏感输出与中间相反极性传播共同形成 slew 连锁，产生约 3.50% / 280 ps 违例；诊断聚焦逐级转换方向。
- 安全解与检查：只放大三个对目标极性敏感的级；检查另一极性、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_038

- 元数据：`planned_unvalidated`；单关键锥；策略 B；M；预计 4 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：launch 近端到中段的四级组合链具有均匀但累积的 slew 损失，目标约 4.00% / 320 ps；诊断需识别整体尺寸梯度不合理。
- 安全解与检查：采用由前到后的安全尺寸梯度，避免每级直接取最大档；检查回推电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_039

- 元数据：`planned_unvalidated`；单关键锥；策略 B；M；预计 3 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：路径中段到 capture 近端的 mux—AOI—INV 链中三类弧共同放大差 slew，构造约 4.50% / 360 ps 违例；诊断需跨不同逻辑类型比较 sensitivity。
- 安全解与检查：为三门选择匹配的 RVT 档位；检查 mux 非目标输入、AOI 各输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_040

- 元数据：`planned_unvalidated`；单关键锥；策略 B；M；预计 4 个有效修改；候选层级 `exp`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：中后段五级链中四个 cell 对 slew 敏感，目标约 4.75% / 380 ps；诊断需要排除一个低敏感中间级。
- 安全解与检查：放大四个有效级并保留低敏感级；检查级间电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_041

- 元数据：`planned_unvalidated`；单关键锥；策略 B；M；预计 4 个有效修改；候选层级 `pipeline`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：capture 端深链在两个转换方向上有不同 slew 拐点，形成约 5.00% / 400 ps 违例；诊断需选择兼顾目标弧的四点。
- 安全解与检查：一次性采用四级协调 RVT resize；检查非目标极性、全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_042

- 元数据：`planned_unvalidated`；单关键锥；策略 B；H；预计 6 个有效修改；候选层级 `top_tree`；1 个 endpoint；无共享关系；隐藏构造：IB；直接逆操作：不允许。
- 场景：六级关键链从 launch 后即持续累积差 slew，目标约 6.50% / 520 ps；诊断需建立全链 transition 与 cell-delay 因果，而非依赖线网优化。
- 安全解与检查：为六级选择渐进、可放置的 RVT 尺寸组合；检查每级输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

### 4.3 单关键锥 × C（10）

#### B1_CASE_043

- 元数据：`planned_unvalidated`；单关键锥；策略 C；E；预计 2 个有效修改；候选层级 `exp`；1 个 endpoint；存在通向正裕量非目标 endpoint 的旁支；隐藏构造：IC；直接逆操作：不允许。
- 场景：关键驱动同时带一个通向 1 个正裕量非目标 endpoint 的轻载旁支，目标路径约 1.00% / 80 ps 违例；诊断聚焦共享输出负载及候选 upsize 对前驱输入电容的影响。
- 安全解与检查：对共享驱动和关键支路首级各执行一次 RVT resize，共 2 处；确认旁支 setup 不退化，并检查全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_044

- 元数据：`planned_unvalidated`；单关键锥；策略 C；E；预计 2 个有效修改；候选层级 `pipeline`；1 个 endpoint；关键支路与 1 个正裕量非目标支路在中段分叉；隐藏构造：IC；直接逆操作：不允许。
- 场景：分叉前驱动的负载分配使目标关键支路 slew 较差，另一路通向 1 个正裕量非目标 endpoint；构造约 1.75% / 140 ps 违例，诊断需检查共享负载而非只看末级。
- 安全解与检查：适度放大分叉前驱动，并调整关键支路第一级；确认非关键支路及前驱 setup、全局 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_045

- 元数据：`planned_unvalidated`；单关键锥；策略 C；M；预计 3 个有效修改；候选层级 `top_tree`；1 个 endpoint；共享驱动带两个正裕量非目标 endpoint 旁支；隐藏构造：IC；直接逆操作：不允许。
- 场景：共享高负载驱动带两个安全裕量不同的非目标 endpoint 旁支，并与目标关键支路两级共同形成约 2.25% / 180 ps 违例；诊断需评估总负载、各分支 slew 和输入电容回推。
- 安全解与检查：upsize 共享驱动，并在关键支路连续两级各做一处 RVT resize，共 3 处；验证两旁支 setup、全局 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_046

- 元数据：`planned_unvalidated`；单关键锥；策略 C；M；预计 2 个有效修改；候选层级 `exp`；1 个 endpoint；上游与 1 个正裕量非目标 endpoint 共享、后段独占；隐藏构造：IC；直接逆操作：不允许。
- 场景：共享门还服务 1 个正裕量非目标 endpoint，若过度放大会给其前驱增加显著输入电容；目标约 2.75% / 220 ps。诊断聚焦“放大共享门”与“放大独占后级”的收益代价比。
- 安全解与检查：选择较小的共享门增档和较大的独占后级增档；检查前驱及旁支 setup、全局 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_047

- 元数据：`planned_unvalidated`；单关键锥；策略 C；M；预计 4 个有效修改；候选层级 `pipeline`；1 个 endpoint；两次分叉后另有两个正裕量非目标 endpoint；隐藏构造：IC；直接逆操作：不允许。
- 场景：两个共享节点、两个通向正裕量非目标 endpoint 的旁路与目标独占段共同产生约 3.25% / 260 ps 违例；诊断需追踪分支关系并避免无效放大旁路。
- 安全解与检查：对两个共享驱动采取克制增档，并放大独占段两个敏感 cell；检查所有旁支 setup、全局 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_048

- 元数据：`planned_unvalidated`；单关键锥；策略 C；M；预计 3 个有效修改；候选层级 `top_tree`；1 个 endpoint；同一目标输入锥的两支路在中段 reconverge；隐藏构造：IC；直接逆操作：不允许。
- 场景：两个内部支路最终汇合到同一目标 endpoint，不形成额外 endpoint；其末级尺寸会共同改变汇合门输入 slew 与上游电容，目标约 3.75% / 300 ps；诊断聚焦支路不对称。
- 安全解与检查：只放大主导支路末级、汇合门和其后关键 cell；检查次要支路 setup、全局 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_049

- 元数据：`planned_unvalidated`；单关键锥；策略 C；M；预计 4 个有效修改；候选层级 `exp`；1 个 endpoint；关键段含高负载共享门和多个非 endpoint sink；隐藏构造：IC；直接逆操作：不允许。
- 场景：多个非关键 sink 不形成独立 timing endpoint；共享门输出 slew 与后段三门 delay 形成约 4.25% / 340 ps 违例，诊断需确认 net delay 仍低于造题上限。
- 安全解与检查：放大共享门一个安全档并协调后段三个敏感级；检查所有 sink、全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_050

- 元数据：`planned_unvalidated`；单关键锥；策略 C；M；预计 4 个有效修改；候选层级 `pipeline`；1 个 endpoint；前段共享后分成目标窄支和正裕量非目标宽支；隐藏构造：IC；直接逆操作：不允许。
- 场景：通向 1 个正裕量非目标 endpoint 的宽支负载拖累共享驱动，但窄支是实际目标违规路径，约 4.75% / 380 ps；诊断需避免只在宽支局部优化。
- 安全解与检查：放大共享驱动并调整窄支三处有效 cell；检查宽支 setup、输入电容、全局 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_051

- 元数据：`planned_unvalidated`；单关键锥；策略 C；H；预计 5 个有效修改；候选层级 `top_tree`；1 个 endpoint；两级与两个正裕量非目标 endpoint 共享扇出后进入独占深链；隐藏构造：IC；直接逆操作：不允许。
- 场景：共享段还扇至两个正裕量非目标 endpoint；其负载、电容回推和目标独占链 slew 联合形成约 5.50% / 440 ps 违例，诊断需给出五个候选的边际收益。
- 安全解与检查：对两个共享级各做一处必要增档，并在独占链选择三个高收益级，共 5 处；检查全部旁支、全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_052

- 元数据：`planned_unvalidated`；单关键锥；策略 C；H；预计 6 个有效修改；候选层级 `exp`；1 个 endpoint；多分支共享驱动后发生 reconvergence；隐藏构造：IC；直接逆操作：不允许。
- 场景：共享输出负载和两支路输入电容互相制约，构造约 7.00% / 560 ps 违例；诊断需证明一次性尺寸组合不会把压力推回上游。
- 安全解与检查：选择共享驱动 1 处、两支路合计 4 处、汇合后 1 处，共 6 个 RVT resize 的平衡组合；检查全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

### 4.4 单关键锥 × D（3）

#### B1_CASE_053

- 元数据：`planned_unvalidated`；单关键锥；策略 D；M；预计 3 个有效修改；候选层级 `pipeline`；1 个 endpoint；局部支路另有两个正裕量非目标 endpoint；隐藏构造：ID；直接逆操作：不允许。
- 场景：目标支路与两个正裕量非目标 endpoint 共享前驱；链中 slew 与 capture 近端输入电容三项互相制约，目标约 3.00% / 240 ps，诊断需在候选评估后直接选定安全组合。
- 安全解与检查：共享前驱小幅增档、链中主导级增档、末级小幅增档，组成明确的三点组合；一次执行后检查全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_054

- 元数据：`planned_unvalidated`；单关键锥；策略 D；M；预计 4 个有效修改；候选层级 `top_tree`；1 个 endpoint；两支路 reconverge 后进入末端链；隐藏构造：ID；直接逆操作：不允许。
- 场景：支路平衡、汇合门 slew 和末端负载共同形成约 4.50% / 360 ps 违例；任意单点最大化都会增加其他门输入电容。诊断需形成一次性四点方案。
- 安全解与检查：主导支路末级、汇合门及末端两级采用协调 RVT 尺寸；不设计试错或回退，检查全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_055

- 元数据：`planned_unvalidated`；单关键锥；策略 D；H；预计 6 个有效修改；候选层级 `exp`；1 个 endpoint；前段共享负载、中段深链、末段汇合；隐藏构造：ID；直接逆操作：不允许。
- 场景：三类约束耦合形成约 7.50% / 600 ps 违例；诊断需同时量化 cell-delay 收益、输入电容代价和非目标分支 slack。
- 安全解与检查：分析后直接实施六点分级 RVT resize，控制共享节点尺寸并把主要增益放在独占链；检查全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

### 4.5 多 endpoint × A（8）

#### B1_CASE_056

- 元数据：`planned_unvalidated`；多 endpoint；策略 A；E；预计 1 个有效修改；候选层级 `top_tree`；2 个 endpoint；共享类型：公共瓶颈影响 2～4 个 endpoint；隐藏构造：I0；直接逆操作：允许。
- 场景：两个 endpoint 共用 capture 分叉前的末级驱动，该 cell 被 downsize 后两条路径分别出现约 0.75% / 60 ps 和 0.50% / 40 ps 违例；诊断聚焦公共关键弧。
- 安全解与检查：恢复公共 RVT cell 的合适尺寸，一次改善两个 endpoint；检查两条路径、其他扇出、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_057

- 元数据：`planned_unvalidated`；多 endpoint；策略 A；E；预计 2 个有效修改；候选层级 `exp`；2 个 endpoint；共享类型：部分共享后分叉；隐藏构造：IA；直接逆操作：不允许。
- 场景：两个目标 endpoint 与一个正裕量非目标 endpoint 共享前段逻辑，分叉后两个目标支路各有一个局部慢弧，最差约 1.00% / 80 ps；诊断需区分公共段和分支局部贡献。
- 安全解与检查：分别 upsize 两个目标支路的局部高敏感 cell，保留共享段尺寸；检查非目标路径、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_058

- 元数据：`planned_unvalidated`；多 endpoint；策略 A；E；预计 2 个有效修改；候选层级 `pipeline`；2 个 endpoint；共享类型：共享高负载驱动；隐藏构造：IA；直接逆操作：不允许。
- 场景：一个高负载驱动供给两个均为负 slack 的 capture 支路，其公共输出弧与较慢支路末级构成两个局部热点，最差约 1.50% / 120 ps；诊断以局部 arc sensitivity 为主，总负载只作背景检查。
- 安全解与检查：共享驱动小幅增档并 upsize 较慢支路末级，共 2 处；确认另一目标支路也闭合，并检查输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_059

- 元数据：`planned_unvalidated`；多 endpoint；策略 A；M；预计 2 个有效修改；候选层级 `top_tree`；4 个 endpoint；共享类型：公共瓶颈影响 2～4 个 endpoint；隐藏构造：IA；直接逆操作：不允许。
- 场景：四个负 slack endpoint 共享两个相邻的局部慢弧，最差目标约 2.25% / 180 ps，其他三个 severity 略低但仍违规；诊断需确认公共弧覆盖完整目标 endpoint 集合。
- 安全解与检查：协调放大两个公共 RVT cell；逐一检查四条路径、上游输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_060

- 元数据：`planned_unvalidated`；多 endpoint；策略 A；M；预计 3 个有效修改；候选层级 `exp`；3 个 endpoint；共享类型：部分共享后分叉；隐藏构造：IA；直接逆操作：不允许。
- 场景：三条路径共享到中段后分叉，各分支的末级敏感度不同，最差目标约 2.75% / 220 ps；诊断需按分支排序局部关键弧。
- 安全解与检查：对三个分支各选择一个必要的 RVT resize，不改共享段；检查所有 endpoint、全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_061

- 元数据：`planned_unvalidated`；多 endpoint；策略 A；M；预计 3 个有效修改；候选层级 `pipeline`；3 个 endpoint；共享类型：共享高负载驱动；隐藏构造：IA；直接逆操作：不允许。
- 场景：共享驱动的公共输出 arc 及两个分支末级的不同极性 arc 构成三个局部热点并限制三个 endpoint，最差目标约 3.50% / 280 ps；诊断以三处 arc sensitivity 为主，共享负载只用于确认公共弧。
- 安全解与检查：共享驱动安全增档，再调整两个极性对应的分支敏感 cell；检查三条路径、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_062

- 元数据：`planned_unvalidated`；多 endpoint；策略 A；M；预计 4 个有效修改；候选层级 `top_tree`；4 个 endpoint；共享类型：相对独立 endpoint 共享修改预算；隐藏构造：IA；直接逆操作：不允许。
- 场景：四个 endpoint 的关键锥基本独立，各自在 capture 近端有一个局部 cell-delay 热点，统一预算为四处；最差目标约 4.25% / 340 ps。诊断需跨路径做收益排序。
- 安全解与检查：每条路径各 upsize 一个最高敏感 RVT cell；检查预算、全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_063

- 元数据：`planned_unvalidated`；多 endpoint；策略 A；H；预计 6 个有效修改；候选层级 `exp`；4 个 endpoint；共享类型：部分共享后分叉；隐藏构造：IA；直接逆操作：不允许。
- 场景：四条路径共享前段后形成三个分支簇，共享段和五个局部弧共同产生最差约 6.25% / 500 ps 违例；诊断需避免重复计算共享收益。
- 安全解与检查：共享热点只修改一次，并为三个分支选择五个互补局部 resize；检查全部 endpoint、全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

### 4.6 多 endpoint × B（8）

#### B1_CASE_064

- 元数据：`planned_unvalidated`；多 endpoint；策略 B；E；预计 1 个有效修改；候选层级 `pipeline`；2 个 endpoint；共享类型：公共瓶颈影响 2～4 个 endpoint；隐藏构造：I0；直接逆操作：允许。
- 场景：共享链首被 downsize，差 slew 沿公共两级传播到两个 endpoint，最差约 1.00% / 80 ps；诊断需定位首个公共 slew 拐点。
- 安全解与检查：恢复公共链首的 RVT 尺寸；验证两条链的 slew 与 setup 同时恢复，并检查全局 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_065

- 元数据：`planned_unvalidated`；多 endpoint；策略 B；E；预计 2 个有效修改；候选层级 `top_tree`；2 个 endpoint；共享类型：部分共享后分叉；隐藏构造：IB；直接逆操作：不允许。
- 场景：共享链首之后分成三支，其中两个目标分支各出现短 slew-chain，最差目标约 1.50% / 120 ps；第三支是正裕量非目标 endpoint；诊断聚焦两个目标分支的链首。
- 安全解与检查：分别 upsize 两个目标分支的链首 RVT cell，共 2 处；检查共享前段和第三个非目标 endpoint、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_066

- 元数据：`planned_unvalidated`；多 endpoint；策略 B；M；预计 3 个有效修改；候选层级 `exp`；3 个 endpoint；共享类型：公共瓶颈影响 2～4 个 endpoint；隐藏构造：IB；直接逆操作：不允许。
- 场景：三个 endpoint 共用连续三门链，slew 从第一门开始逐级恶化，最差目标约 2.25% / 180 ps；诊断聚焦公共链的尺寸梯度。
- 安全解与检查：为公共三级链选择渐进 RVT 尺寸；检查每个 endpoint、链首输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_067

- 元数据：`planned_unvalidated`；多 endpoint；策略 B；M；预计 3 个有效修改；候选层级 `pipeline`；4 个 endpoint；共享类型：共享高负载驱动；隐藏构造：IB；直接逆操作：不允许。
- 场景：高负载公共驱动后接两段共享链再分至四个 endpoint，最差约 3.00% / 240 ps；诊断需确认差 slew 的公共传播。
- 安全解与检查：平衡 resize 公共驱动及后两级，不在各 endpoint 重复修改；检查四条路径、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_068

- 元数据：`planned_unvalidated`；多 endpoint；策略 B；M；预计 4 个有效修改；候选层级 `top_tree`；3 个 endpoint；共享类型：部分共享后分叉；隐藏构造：IB；直接逆操作：不允许。
- 场景：共享两级链后分成两条不同深度的 slew-chain，覆盖三个 endpoint，最差目标约 3.50% / 280 ps；诊断需比较分支链边际收益。
- 安全解与检查：共享链首小幅增档，并在两个分支选择三个有效级；检查所有 endpoint、输入电容、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_069

- 元数据：`planned_unvalidated`；多 endpoint；策略 B；M；预计 4 个有效修改；候选层级 `exp`；2 个 endpoint；共享类型：相对独立 endpoint 共享修改预算；隐藏构造：IB；直接逆操作：不允许。
- 场景：两个 endpoint 各自在 capture 近端有一条两级 slew-chain，统一限制四个修改，最差目标约 4.25% / 340 ps；诊断需公平分配预算而非只优化最差一路。
- 安全解与检查：两条链各做两级匹配 resize；检查两路均修复、全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_070

- 元数据：`planned_unvalidated`；多 endpoint；策略 B；H；预计 5 个有效修改；候选层级 `pipeline`；4 个 endpoint；共享类型：部分共享后分叉；隐藏构造：IB；直接逆操作：不允许。
- 场景：公共链首后分成三个深度不同的分支链，其中一个分支扇至两个 endpoint，四个目标 endpoint 均为负 slack，最差约 5.75% / 460 ps；诊断需追踪公共 slew 的不同放大。
- 安全解与检查：修改公共链首，并在三个分支分配四个高收益 resize；检查全部路径、全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_071

- 元数据：`planned_unvalidated`；多 endpoint；策略 B；H；预计 6 个有效修改；候选层级 `top_tree`；3 个 endpoint；共享类型：相对独立 endpoint 共享修改预算；隐藏构造：IB；直接逆操作：不允许。
- 场景：三个 endpoint 各有独立的两级 slew-chain，目标最差约 7.00% / 560 ps，修改总预算正好六处；诊断需同时覆盖三条违规路径。
- 安全解与检查：每条链选择两级协调 RVT resize；检查全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

### 4.7 多 endpoint × C（6）

#### B1_CASE_072

- 元数据：`planned_unvalidated`；多 endpoint；策略 C；E；预计 2 个有效修改；候选层级 `exp`；2 个 endpoint；共享类型：公共瓶颈影响 2～4 个 endpoint；隐藏构造：IC；直接逆操作：不允许。
- 场景：两个 endpoint 共享一个负载敏感驱动与其前驱，最差目标约 1.25% / 100 ps；诊断需平衡公共驱动收益和前驱输入电容。
- 安全解与检查：公共驱动增档、前驱小幅匹配，确认两个 endpoint 同时改善；检查全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_073

- 元数据：`planned_unvalidated`；多 endpoint；策略 C；M；预计 3 个有效修改；候选层级 `pipeline`；3 个 endpoint；共享类型：部分共享后分叉；隐藏构造：IC；直接逆操作：不允许。
- 场景：共享驱动后分成高、低负载支路，高负载支路覆盖两个 endpoint、低负载支路覆盖一个，三个目标均为负 slack，最差约 2.50% / 200 ps；诊断需识别负载不对称和分支输入电容。
- 安全解与检查：共享驱动安全增档，并分别调整两支路首级；确认三个目标 endpoint 均闭合，并按 G0 执行全局 setup/DRV 扫描，同时检查 max fanout 与 H0，满足 P0。

#### B1_CASE_074

- 元数据：`planned_unvalidated`；多 endpoint；策略 C；M；预计 4 个有效修改；候选层级 `top_tree`；3 个 endpoint；共享类型：共享高负载驱动；隐藏构造：IC；直接逆操作：不允许。
- 场景：公共高负载门驱动三条负 slack 的 capture 目标支路和一条正裕量非目标支路，最差约 3.25% / 260 ps；诊断需覆盖共享负载与各目标支路敏感度。
- 安全解与检查：公共驱动克制增档，再对三条目标支路各做一个高收益 RVT resize；确认三条目标闭合并检查非目标 endpoint、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_075

- 元数据：`planned_unvalidated`；多 endpoint；策略 C；M；预计 4 个有效修改；候选层级 `exp`；2 个 endpoint；共享类型：相对独立 endpoint 共享修改预算；隐藏构造：IC；直接逆操作：不允许。
- 场景：两个相对独立的目标锥各自在锥内含一个同时服务 1 个正裕量非目标 endpoint 的驱动，以及一个目标独占局部 cell；总计 2 个目标和 2 个非目标 endpoint，锥间不共享逻辑，仅共享四点修改预算。最差约 4.00% / 320 ps；诊断需兼顾各锥内部旁支负载。
- 安全解与检查：每个锥各选择共享驱动和独占级的一组平衡 resize；检查所有旁支、全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_076

- 元数据：`planned_unvalidated`；多 endpoint；策略 C；H；预计 5 个有效修改；候选层级 `pipeline`；4 个 endpoint；共享类型：部分共享后分叉；隐藏构造：IC；直接逆操作：不允许。
- 场景：两级共享段后分成三个负载差异显著的分支，其中一个分支覆盖两个 endpoint，四个目标均为负 slack，最差约 5.50% / 440 ps；诊断需量化共享 resize 的输入电容代价及跨 endpoint 收益。
- 安全解与检查：共享段两个必要级各做一处 RVT resize，三分支各选一个敏感 cell，共 5 处；检查全部目标 endpoint 闭合、全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_077

- 元数据：`planned_unvalidated`；多 endpoint；策略 C；H；预计 6 个有效修改；候选层级 `top_tree`；3 个 endpoint；共享类型：共享逻辑存在明显 setup/DRV 权衡但仍有一次性安全解；隐藏构造：IC；直接逆操作：不允许。
- 场景：公共高负载驱动接三条深浅不一的支路，过小会恶化 setup；过大会增加其各输入 pin 对相应前驱输出 net 的 capacitance，并可能恶化这些 net 的 transition。最差目标约 6.75% / 540 ps；诊断聚焦负载与 DRV 权衡。
- 安全解与检查：公共门取中间安全档，并在三个分支与前驱分配五个 resize；一次完成后检查三条 setup、全局 DRV、max fanout、可合法化与 H0，满足 G0、P0。

### 4.8 多 endpoint × D（3）

#### B1_CASE_078

- 元数据：`planned_unvalidated`；多 endpoint；策略 D；M；预计 4 个有效修改；候选层级 `exp`；4 个 endpoint；共享类型：公共瓶颈影响 2～4 个 endpoint；隐藏构造：ID；直接逆操作：不允许。
- 场景：四个 endpoint 共享两级瓶颈，但末端两类负载要求不同尺寸取舍，最差目标约 3.50% / 280 ps；诊断需形成跨路径的一次性组合。
- 安全解与检查：公共两级使用保守梯度，并为两类末端各选择一个敏感 cell；不试错或回退，检查全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_079

- 元数据：`planned_unvalidated`；多 endpoint；策略 D；M；预计 4 个有效修改；候选层级 `pipeline`；3 个 endpoint；共享类型：共享逻辑存在明显 setup/DRV 权衡但仍有一次性安全解；隐藏构造：ID；直接逆操作：不允许。
- 场景：共享门后的高负载分支覆盖两个 endpoint、另一分支覆盖一个；upsize 共享门可改善三个目标 setup，却会增加其输入 pin 所在前驱输出 net 的 capacitance 与 transition 风险。最差约 4.50% / 360 ps；诊断需先确定安全档位和补偿点。
- 安全解与检查：共享门取非最大档，配合前驱及两个分支的三处 resize；一次执行后检查所有 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

#### B1_CASE_080

- 元数据：`planned_unvalidated`；多 endpoint；策略 D；H；预计 6 个有效修改；候选层级 `top_tree`；4 个 endpoint；共享类型：公共瓶颈影响 2～4 个 endpoint；隐藏构造：ID；直接逆操作：不允许。
- 场景：四个 endpoint 共用深公共锥，之后形成两种负载和极性，最差目标约 7.25% / 580 ps；诊断需联合处理 cell delay、slew、输入电容和分支覆盖。
- 安全解与检查：分析后直接选定公共三点与分支三点的 RVT 尺寸组合；不设计试错或 rollback，检查全局 setup/DRV、max fanout、可合法化与 H0，满足 G0、P0。

### 4.9 Setup/DRV guardrail × A（5）

#### B1_CASE_081

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 A；E；预计 1 个有效修改；候选层级 `exp`；1 个目标 endpoint；guardrail 轴：防止恶化非目标 setup endpoint；隐藏构造：I0；直接逆操作：允许。
- 场景：1 个目标和 1 个正裕量非目标 endpoint 在目标末级输入处共享前驱输出 net；目标末级被 downsize 后形成约 0.75% / 60 ps 违例。恢复该 sink 会把共享 net 的负载恢复到 baseline 水平，诊断需确认非目标路径仍保有预留安全裕量。
- 安全解与检查：恢复目标末级的 RVT 尺寸且不触碰共享前驱；确认目标闭合、非目标 setup 不低于 baseline，并检查全局 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_082

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 A；E；预计 2 个有效修改；候选层级 `pipeline`；1 个目标 endpoint；guardrail 轴：防止恶化非目标 setup endpoint；隐藏构造：IA；直接逆操作：不允许。
- 场景：共享前驱输出 net 分别驱动目标关键门和 1 个正裕量非目标 sink；过度放大关键门会增加该 net 的负载并伤害非目标 endpoint，目标约 1.25% / 100 ps；诊断聚焦 fork 拓扑和非目标 slack。
- 安全解与检查：关键门适度增档并匹配其直接前驱，以补偿共享 net 的新增负载；验证目标修复、非目标 setup 无回归、全局 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_083

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 A；M；预计 3 个有效修改；候选层级 `top_tree`；2 个目标 endpoint；guardrail 轴：防止恶化非目标 setup endpoint；隐藏构造：IA；直接逆操作：不允许。
- 场景：两个目标路径各有一个独占局部热点；它们还经过一个多输入共享门，而第三条小裕量非目标路径连接到该门的另一输入 pin。若放大共享门，其新增 pin cap 会加载非目标输入的前驱 output net；因避开该风险点后仍缺少部分收益，诊断还需定位目标 1 capture 近端的第二高 sensitivity cell。最差约 2.50% / 200 ps。
- 安全解与检查：在两个独占热点及目标 1 的第二高 sensitivity 局部级做三处 RVT resize；检查第三个非目标 endpoint 和全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_084

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 A；M；预计 3 个有效修改；候选层级 `exp`；1 个目标 endpoint；guardrail 轴：max transition 风险；隐藏构造：IA；直接逆操作：不允许。
- 场景：目标路径三个离散 cell-delay 热点形成约 3.50% / 280 ps 违例，其中一个候选的输入 net 已接近 transition 限值；诊断需确认该风险 sink 的直接后级仍有足够 delay sensitivity，可承接原计划收益。
- 安全解与检查：避开风险输入网，在其后级和另外两个热点完成三处 RVT resize；检查全局 max transition、setup、其他 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_085

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 A；M；预计 4 个有效修改；候选层级 `pipeline`；1 个目标 endpoint；guardrail 轴：max capacitance 风险；隐藏构造：IA；直接逆操作：不允许。
- 场景：路径中段局部慢弧可通过 upsize 改善，但其前驱输出 net 已接近 max capacitance；目标约 4.50% / 360 ps。诊断需量化中等档新增 pin cap 的剩余裕量并排除会越限的最大档。
- 安全解与检查：关键门 resize 到经计算仍低于 cap 限值的中等档，并在后续三个局部热点各做一处 RVT resize；检查全局 max capacitance、setup、其他 DRV、max fanout 与 H0，满足 G0、P0。

### 4.10 Setup/DRV guardrail × B（7）

#### B1_CASE_086

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 B；E；预计 1 个有效修改；候选层级 `top_tree`；1 个目标 endpoint；guardrail 轴：防止恶化非目标 setup endpoint；隐藏构造：I0；直接逆操作：允许。
- 场景：1 个目标和 1 个正裕量非目标 endpoint 在目标链首的输入前分叉，该链首输入 net 与非目标 sink 共享同一上游输出；目标链首被 downsize 后仅目标分支出现约 1.00% / 80 ps 违例。恢复链首会增加其输入 pin 对共享 net 的负载，诊断需确认该回推不会伤害非目标路径。
- 安全解与检查：恢复目标链首的合适 RVT 档位；检查目标闭合、非目标 setup 不低于 baseline、全局 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_087

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 B；M；预计 3 个有效修改；候选层级 `exp`；1 个目标 endpoint；guardrail 轴：防止恶化非目标 setup endpoint；隐藏构造：IB；直接逆操作：不允许。
- 场景：公共上游输出 net 同时驱动目标三门 slew-chain 的链首和 1 个正裕量非目标 sink；放大目标链首会回推共享 net 负载，放大末级又会加重前一级输出负载。目标约 2.50% / 200 ps；诊断聚焦两级负载传播与非目标 slack。
- 安全解与检查：目标链首小幅增档、其后两个独占级协调增档，共 3 处；检查共享 net 上的非目标 endpoint 及全局 setup、DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_088

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 B；M；预计 4 个有效修改；候选层级 `pipeline`；2 个目标 endpoint；guardrail 轴：防止恶化非目标 setup endpoint；隐藏构造：IB；直接逆操作：不允许。
- 场景：两条目标链和一条小裕量非目标链（总计 3 个相关 endpoint）共享 launch 近端逻辑，最差约 3.25% / 260 ps；诊断需将 resize 限制在两个独占链。
- 安全解与检查：两个目标链各做两级匹配 resize；检查共享段、非目标 endpoint、全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_089

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 B；M；预计 3 个有效修改；候选层级 `top_tree`；1 个目标 endpoint；guardrail 轴：max transition 风险；隐藏构造：IB；直接逆操作：不允许。
- 场景：capture 近端三门链中，第 1 级输出 net 接近 transition 限值；upsize 第 1 级会改善该 net 的 slew，而 upsize 第 2 级会增加该 net 的负载，后两级差 slew 合成约 3.75% / 300 ps 违例。诊断需量化这组交叉影响。
- 安全解与检查：链首安全增档并以较小档匹配后两级，限制第 1、2 级输出 net 的负载；检查全局 max transition、setup、其他 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_090

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 B；M；预计 4 个有效修改；候选层级 `exp`；1 个目标 endpoint；guardrail 轴：max transition 风险；隐藏构造：IB；直接逆操作：不允许。
- 场景：路径中段到 capture 近端的四级链中，第 1、3 级输出 net 分别是后续待 resize sink 的输入 net，二者均逼近 transition 限值，目标约 4.75% / 380 ps；诊断需平衡上游驱动改善与下游 pin cap 增量，不能仅依赖后级强驱动。
- 安全解与检查：从链首开始采用四级渐进尺寸并控制每级负载；检查全局 max transition、setup、其他 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_091

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 B；H；预计 5 个有效修改；候选层级 `pipeline`；2 个目标 endpoint；guardrail 轴：max transition 风险；隐藏构造：IB；直接逆操作：不允许。
- 场景：共享链首的输入上游 net 与分叉输出 net 均存在 transition 裕量紧张；链首 upsize 会加载前者，两分支首级 upsize 会加载后者，最差 setup 约 5.75% / 460 ps。诊断需找到改善 slew 又不越过两处限值的五点。
- 安全解与检查：共享链首中档增档，并在两支路分配四个渐进 resize；检查全局 max transition、setup、其他 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_092

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 B；H；预计 6 个有效修改；候选层级 `top_tree`；1 个目标 endpoint；guardrail 轴：max capacitance 风险；隐藏构造：IB；直接逆操作：不允许。
- 场景：深链含两个带旁路 tap、后续 reconverge 的 branch-point driver output net；每条 net 都直接扇出多个待增档的链首 sink，相关 pin cap 会在各自 net 上求和并逼近 max capacitance。目标约 7.00% / 560 ps；诊断需先排除任一 net 越限的组合。
- 安全解与检查：在两个分支组各分配 3 个中等、渐进的 RVT resize，共 6 处，并限制每组直接挂在 branch-point net 上的 sink 增容；检查两条 net 及全局 max capacitance、setup、其他 DRV、max fanout 与 H0，满足 G0、P0。

### 4.11 Setup/DRV guardrail × C（4）

#### B1_CASE_093

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 C；E；预计 1 个有效修改；候选层级 `exp`；1 个目标 endpoint；guardrail 轴：防止恶化非目标 setup endpoint；隐藏构造：I0；直接逆操作：允许。
- 场景：共享驱动被 downsize 后，1 个目标路径出现约 1.25% / 100 ps 违例，1 个非目标输出支路仍为正 slack；恢复驱动会改善两条输出支路，但也增加其输入 pin 对上游 net 的负载，该上游 net 同时参与非目标路径。诊断需确认净效应。
- 安全解与检查：恢复共享 RVT 驱动的原尺寸；确认目标闭合、非目标不低于 baseline，并检查上游 net 与全局 DRV、max fanout、H0，满足 G0、P0。

#### B1_CASE_094

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 C；M；预计 3 个有效修改；候选层级 `pipeline`；2 个目标 endpoint；guardrail 轴：防止恶化非目标 setup endpoint；隐藏构造：IC；直接逆操作：不允许。
- 场景：共享高负载驱动服务两个目标 endpoint 和一个小裕量非目标 endpoint；驱动 upsize 改善下游三支，却会通过输入 pin 增加上游共享 net 的负载，该 net 也位于非目标路径。最差约 3.00% / 240 ps；诊断需控制净输入电容。
- 安全解与检查：共享门仅增一个安全档，并在两个目标独占支路各做一处 RVT resize；确认两个目标闭合、非目标无回归，并检查全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_095

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 C；M；预计 4 个有效修改；候选层级 `top_tree`；1 个目标 endpoint；guardrail 轴：max transition 风险；隐藏构造：IC；直接逆操作：不允许。
- 场景：目标分支首级 upsize 会增加共享门输出 net 的 capacitance，使其 transition 逼近限值；两个固定旁支共同构成背景负载。目标 setup 约 4.25% / 340 ps；诊断需量化分支首级增容与共享驱动补偿的净效应。
- 安全解与检查：共享门安全增档以补偿输出 slew，并在目标独占段分配三个受控 RVT resize；检查全部分支、全局 max transition、setup、其他 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_096

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 C；H；预计 6 个有效修改；候选层级 `exp`；2 个目标 endpoint；guardrail 轴：max capacitance 风险；隐藏构造：IC；直接逆操作：不允许。
- 场景：共享两级逻辑与两条目标支路耦合，多个候选尺寸会把前驱 capacitance 推近限值；最差 setup 约 6.50% / 520 ps；诊断聚焦电容回推与分支收益。
- 安全解与检查：共享两级各做一处克制 RVT resize，并在两条独占支路合计分配四个高收益 resize，共 6 处；检查对应前驱输出 net 及全局 max capacitance、setup、其他 DRV、max fanout、可合法化与 H0，满足 G0、P0。

### 4.12 Setup/DRV guardrail × D（4）

#### B1_CASE_097

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 D；M；预计 3 个有效修改；候选层级 `pipeline`；1 个目标 endpoint；guardrail 轴：防止恶化非目标 setup endpoint；隐藏构造：ID；直接逆操作：不允许。
- 场景：1 个目标与 1 个非目标路径在多输入共享门前的不同输入 pin 处关联；放大共享门会增加各输入 pin 对相应前驱 net 的负载并伤害非目标上游路径。目标链已有两个热点，避开共享门后还需目标 capture 近端的第三高 sensitivity cell 补足约 2.50% / 200 ps 收益；诊断聚焦安全组合边界。
- 安全解与检查：保持共享门尺寸，在目标独占链的两个热点和第三高 sensitivity cell 上一次性选择三个 RVT resize；检查非目标及全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_098

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 D；M；预计 4 个有效修改；候选层级 `top_tree`；2 个目标 endpoint；guardrail 轴：防止恶化非目标 setup endpoint；隐藏构造：ID；直接逆操作：不允许。
- 场景：两个目标 endpoint 与一个小裕量非目标 endpoint 经过多输入共享门的不同输入 pin 后再分叉；放大共享门会通过非目标输入 pin 的新增电容加载其前驱 output net，可能拖慢非目标 setup 路径。分叉后负载差异明显，最差约 3.50% / 280 ps；诊断需一次性协调共享和独占尺寸。
- 安全解与检查：共享门取安全中档，两个目标分支各做一处 RVT resize，并补强目标侧共享前驱一处；确认非目标 input net 的负载/DRV 合法且其 setup slack 不低于 baseline，不试错或回退，并检查全局 setup/DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_099

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 D；M；预计 4 个有效修改；候选层级 `exp`；1 个目标 endpoint；guardrail 轴：max transition 风险；隐藏构造：ID；直接逆操作：不允许。
- 场景：四级目标链的 setup 约 4.00% / 320 ps；链首输入上游 net 与中段某待 resize sink 的输入 net 均接近 transition 限值，候选增档会加载其前驱、同时改善自身输出 slew；诊断聚焦两处交叉 DRV 裕量。
- 安全解与检查：分析后直接选择四级渐进 RVT 尺寸，不采用最大档或试错；检查全局 max transition、setup、其他 DRV、max fanout 与 H0，满足 G0、P0。

#### B1_CASE_100

- 元数据：`planned_unvalidated`；Setup/DRV guardrail；策略 D；M；预计 4 个有效修改；候选层级 `top_tree`；2 个目标 endpoint；guardrail 轴：max capacitance 风险；隐藏构造：ID；直接逆操作：不允许。
- 场景：共享驱动与两条目标支路形成约 5.00% / 400 ps 违例，放大任一分支首级都会增加共享驱动的输出 capacitance；诊断需求得一次性安全组合。
- 安全解与检查：共享驱动小幅增档、两分支各选一处高收益 cell，并补强不增加该共享输出负载的前驱；检查全局 max capacitance、setup、其他 DRV、max fanout 与 H0，满足 G0、P0。

## 5. 后续物化与准入流程

本目录当前不应出现具体实例名、具体候选 cell 型号、实测 WNS/TNS、面积变化、
canonical Tcl、checkpoint 或 PASS 标记。后续物化每条 case 时，应先从该卡片指定的
层级和关系选取真实候选，再在 fresh baseline 上构造隐藏违例并收集 setup、hold、
DRV、max fanout、合法化和物理证据。只有满足 G0/P0、完成 H0 观察记录并通过卡片
专属 guardrail 的候选才可进入下一阶段；H0 不附加数值 gate。未命中 severity、
cell/net delay 占比、目标 setup closure 或替代安全解条件的候选应被拒绝，而不是
改写本规划为已验证。

本批最多 10 条允许直接恢复被 downsize cell 的原尺寸，具体为
`B1_CASE_001`、`B1_CASE_005`、`B1_CASE_014`、`B1_CASE_025`、
`B1_CASE_036`、`B1_CASE_056`、`B1_CASE_064`、`B1_CASE_081`、
`B1_CASE_086`、`B1_CASE_093`。其余 90 条不得把注入动作的直接逆操作作为唯一解。

## 6. 文档级自检目标

- ID 必须严格覆盖 `B1_CASE_001`～`B1_CASE_100`，无缺失、无重复。
- 状态必须全部为 `planned_unvalidated`，不得出现工具实测 PASS 结论。
- 形态 × 策略 × 难度必须严格匹配第 3 节矩阵。
- 多 endpoint 子型必须为：公共瓶颈 7 条、部分共享后分叉 8 条、共享高负载驱动
  4 条、相对独立 endpoint 共享修改预算 4 条、明显 setup/DRV 权衡 2 条。
- Guardrail 轴必须为：非目标 setup 10 条、max transition 6 条、
  max capacitance 4 条。
- 隐藏构造档必须为：`I0/IA/IB/IC/ID = 10/30/31/19/10`；`I0` 必须与允许
  直接逆操作的 10 张卡完全同集，其余构造档必须与策略 A/B/C/D 对应。
- 卡片中的 endpoint 数只统计目标违例；任何卡片专属正裕量保护对象必须明确称为
  非目标 endpoint，并给出数量或说明它不是 timing endpoint。
- 每条卡片必须包含状态、形态、策略、难度、修改数、层级、endpoint/共享关系、
  关键弧位置和诊断重点、severity、隐藏构造档、安全解、专属检查、G0、P0 与 H0。
- 候选层级只允许 `exp`、`pipeline`、`top_tree`；不得从空候选池安排 case。
- 只执行上述文本一致性检查；本轮不得启动 Innovus、PrimeTime 或其他 EDA 验证。
