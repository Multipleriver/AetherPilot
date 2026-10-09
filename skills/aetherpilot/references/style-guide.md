# 原理图可读性规范（analog_default）与 lint 检查

本规范是 AetherPilot 模拟原理图的可读性约定，规则与 `aetherpilot/style.py` 的 lint 检查器一一对应（括号内为规则 id）。认证 fixture（cmos_inverter / ota5 / 差分对模板与 ota5_dut）即本规范的参考实现：它们必须零违例通过 lint。

## 规则集

| 规则 id | 级别 | 约定 | 依据 |
| --- | --- | --- | --- |
| `grid_alignment` | 违例 | 实例原点、引脚位置、线点必须落在工艺网格（默认 0.0625）上 | 网格对齐是命中端子与后续编辑的前提 |
| `device_layering` | 违例 | NMOS 整体在低行、PMOS 整体在高行（max NMOS y < min PMOS y） | 模拟原理图垂直分层惯例（电流源→信号路径→负载自下而上） |
| `differential_pair_form` | 警告 | 差分对（同 master、同行、栅接不同网）应左 R0 右 MY 镜像 | 差分对称性一眼可辨；同栅对（电流镜）不适用 |
| `pin_periphery` | 违例 | 引脚必须落在器件体外围，不得插入器件体内部 | 引脚内嵌导致连线穿过器件，图面混乱 |
| `wire_orthogonality` | 违例 | 线段只能水平/垂直 | 斜线在原理图惯例中不可接受 |
| `wire_crossing` | 违例 | 不同网络的线段不得相交或相触 | 相交即短路 |
| `net_labeling` | 警告 | 有连线的内部网络应有 wire_name 标签 | 无标签内网阅读时需追线 |
| `power_naming` | 警告 | 电源/地网络用规范名（VDD/VSS/gnd!） | 大小写混乱造成误判 |
| `instance_overlap` | 违例 | 器件体不得重叠 | 重叠即不可读 |
| `signal_flow` | 警告 | 仅当所有信号输入都在所有输出右侧（整体反向）才报；差分对双边输入不误报 | 左到右信号流惯例 |
| `label_on_wire` | 违例 | wire_name 标签原点必须落在其网络的线段上（容差 0.1，覆盖原生文本原点偏移） | 标签离线即失去语义锚点 |
| `label_collision` | 警告 | wire_name 标签的估算包围盒不得互相重叠 | 重叠标签不可读 |
| `bend_budget` | 警告 | 单网络超过 16 个弯折 | 过多弯折难以追线 |
| `pin_spacing` | 警告 | 同侧相邻引脚间距不小于 1.0 | 过密引脚难插线 |
| `wire_jog` | 警告 | 亚网格级「出-回」小凸起（绕行异网导线/端子的合法闪避不报） | 小凸起在放大时像缺陷 |
| `semantic_net_names` | 违例 | 网络名必须携带语义；net0/n12/N_h_*/I_h_* 等自动名一律拒绝 | 自动网名在阅读与评审时丢失设计意图 |
| `semantic_instance_names` | 警告 | 实例名应体现器件类别语义（如 MN1/MP3/RTAIL）；纯数字或单字母名报警 | 无语义实例名无法快速定位器件 |

## 使用方式

```bash
# 离线：对计划文件 lint
python -m aetherpilot lint --plan plan.json --config <cfg>
# 原生：对库中已保存原理图 lint（只读，不改设计）
python -m aetherpilot lint --library <lib> --cell <cell> --config <cfg>
```

输出为 JSON：`ok`（无违例即通过）、`findings`（逐条规则/级别/对象/说明/证据坐标）、`summary`（规则数、违例/警告数、对象统计）。违例使 operation 以 `style_violations` 失败（退出码非零），警告只报告。结果写入证据目录 `lint.json` 并进入 manifest；`report.render` 会把 lint 结果收编为报告的「可读性检查」节。

## 边界

lint 是可读性与一致性检查，不是 ERC/DRC，不构成电路正确性或工艺签核结论；`differential_pair_form` 等警告级规则依赖启发式（同 master 同行+栅接不同网），可能漏报非典型结构。

## 生成侧：style 布局与重画（recreate）与结构化组合（compose）

- 配方库（`layout_recipes.RECIPES`）：`ota5` / `inverter` / `mirror2` / `tgate` 四份受验证配方。每份配方 = 显式角色映射（默认对应参考拓扑，可用 `--roles` 覆盖到真实样例的实例名）+ 连接前提（same_net/distinct_net + pin-net 绑定，失败即 `recipe_mismatch`）+ 少量规划函数。网名一律从真实连接派生，不硬编码；重命名实例/网络几何不变。
- 通用几何层（`layout_geometry.py`）：网格捕捉、orientation 变换、gate 朝向验证选取、带端子接近区的体碰撞检查、角色映射校验、计划组装与 compile 内检、H 归一化指标、组接口（占用区域+端口接入侧）。
- 组合接口（`layout_groups.py`，`compose --structure ota5 --via groups`）：各组本地坐标系规划，组合器按 link 接口几何（端子列对齐 + 声明 H 间隙）反解组间平移，再生成 link 竖线与 tap 引脚；平移矛盾/未对齐/间隙不符/占用重叠均以 `interface_conflict` 失败闭环。OTA5 分组组合与单体路径几何集合一致（原生验收在案）。
- 真实样例适配：`--from-library ChargePump --from-cell cp_Tgate` 可把教程库真实传输门读回后按 tgate 配方重排（角色映射到真实实例名 PM0/NM0），拓扑与源样例严格一致。
- `python -m aetherpilot compose-ota5 --library <lib> --cell <new>`：把认证五管 OTA 拓扑组合成紧凑可读布局（兼容入口，等价 `compose --structure ota5`）。显式角色映射（`structured_layout.OTA5_STRUCTURE`，可用 `--roles` 覆盖并失败封闭校验）；朝向由真实 symbol 端子变换验证（输入对 gate 朝外、镜像负载 gate 朝内）；器件原点按 drain 端子同列、尾管居中对齐计算；布线用局部模板（VDD rail/drain 列/镜像 gate 线/二极管就近tie/TAIL 公共条中点下接/左右输入/右侧输出/就近偏置）。验证链：计划阶段拓扑 digest + 保存重开后「严格连接 + 声明参数包含」比对 + masters 感知 lint。
- lint 几何口径：`lint_schematic(..., masters=...)` 时 `instance_overlap`/`pin_periphery` 用真实变换后 body bbox；无 masters 保持原点启发式。报告 `geometry` 字段标明口径（`native_masters` / `origin_heuristic`）。配方可声明豁免规则（tgate 豁免 device_layering），豁免项以 `waived` 级别可见报告，绝不隐藏。

- `python -m aetherpilot natural-apply "<描述>" --style analog_default ...`：通用器件文法按本规范派生 soft 布局约束（NMOS 下/PMOS 上/引脚入列），显式约束永远优先，被覆盖的 style 约束记入决策。
- 引脚区域约定（style_constraints 派生，soft 可覆盖）：输入引脚在左下区域成列、输出引脚在右下区域成列、input-output 引脚在底部成排；VDD 类电源引脚居中置顶、VSS/GND 类居中置底。
- 引脚引线与标签（pin_lead_wires，生成路径自动执行）：每个顶层引脚向外拉出一段短引线并在线中点打 wire_name 标签——输入向左、输出与 input-output 向右、VDD 向上、VSS/GND 向下。引线属于引脚网络本身，不改变连接关系。
- 电源端子短桩（`--wire-policy labeled_power`，可选）：接顶层电源引脚的 VDD/VSS/GND 网络不再拉通长轨，而是每个器件端子引出一小节短桩并逐端子打 wire_name 标签（按名绑定，仿真网表一致性不变）；短桩方向优先上（VDD）/下（VSS），但与异网导线或端子（如相隔 0.1875 的 B/S 端子）冲突时自动选举无冲突方向与长度，全部方向被占时以 `stub_blocked` 失败闭环。计划自动获得 `routing_policy.allow_label_islands`，标签岛约定要求每个岛各有标签。
- 内部互连默认直接走线、不另打标签；无引脚的内部网络由 label_internal_nets 自动在导线上锚定一个 wire_name 标签（防止原生检查器把其漂移到 net0/net1 自动名）。
- 语义命名（semantic_net_renames，层次网表物化路径自动执行）：SPICE/CDL 网表中的 net0/n12 类自动网名按「端点器件_端子」派生为语义名（如 net0 → MN1_S_MN2_D），实例连接、引脚、导线、标签同步改写；recreate 路径因拓扑 digest 等价契约不做改名。lint 侧对应 `semantic_net_names`（违例）/`semantic_instance_names`（警告）。
- `python -m aetherpilot recreate --library <lib> --cell <cell> --new-cell <new>`：把已保存的扁平原理图重画到统一网格（行/列聚类、差分对归一化 R0/MY、确定性重布线、内部网自动补 wire_name 标签），新 cell 发布前做拓扑 digest 等价校验与 lint。
- 已知布线边界：贪心单平面布线器在致密多行核（如 2×2 OTA 核）上可能因自身线网互锁而 fail-closed（`routing_failed`）；此时布局/映射仍然成功，可调整间距或显式 routing 预算重试。recreate 默认放宽为 max_expansions=1000000、timeout=60s。

## 依据文件

`aetherpilot/style.py`、`aetherpilot/structured_layout.py`、`tests/test_style.py`、`tests/test_structured_layout.py`、`tests/test_lint_operation.py`、`tests/native/schematic_lint_acceptance.py`、`tests/native/schematic_readability_acceptance.py`、`tests/native/schematic_compose_acceptance.py`、认证 fixture `examples/inverter.plan.json`、`examples/ota5_dut.plan.json`。
