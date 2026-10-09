# 工作流：只读审查（Reviewer）

依据：`scripts/reproduce_review.py`、`aetherpilot/reviewer.py`、`aetherpilot/harness.py:599-620`、`examples/ota5_reviewer_intent.json`、`AGENTS.md`「Reviewer 稳定试用」节。

## 稳定入口

唯一稳定入口是项目源码内的 `scripts/reproduce_review.py`，不从历史 `reservation/feedback/<日期>/` 目录寻找 runner。它执行两次原生只读 schematic 查询、检查一个已完成的历史 run，并把该 run 的不可变 OP/网表文件交给 `Harness.review_files`（脚本 docstring，第 1-9 行）。全流程只读：不保存或发布设计、不重新仿真、不修改许可证/PDK/模型；已有输出目录不得覆盖。

## 前置条件

1. `source .venv/bin/activate`（项目虚拟环境）。
2. 在 source Aether 环境脚本**之前** `export AETHERPILOT_SKIP_LICENSE_BOOTSTRAP=1`——该变量被 `scripts/aether-2026.03.env.bash:26` 消费，用于跳过许可证 bootstrap；不启停许可证服务。
3. 运行配置：`--config <path>`，或环境变量 `AETHERPILOT_CONFIG`；都缺省时依次尝试 `<project-root>/.aetherpilot/config.json`、`<project-root>/aetherpilot.local.json`，找不到即报错退出（`reproduce_review.py:131-140`）。

## 参数契约

argparse 定义见 `reproduce_review.py:232-243`：

| 参数 | 必填 | 含义 |
| --- | --- | --- |
| `--project-root` | 是 | AetherPilot 项目根（必须含 `aetherpilot/` 包，`:119-121`） |
| `--config` | 否 | 本地 backend 配置 JSON（私有，不提交） |
| `--library` | 是 | DUT 与 testbench 所在库 |
| `--dut-cell` | 是 | DUT cell（view 固定 `schematic`，`:146-149`） |
| `--testbench-cell` | 是 | testbench cell |
| `--run-operation` | 是 | 已完成历史 run 的 operation_id；脚本用 `inspect` 校验其 `stage == "complete"`，否则退出（`:160-162`） |
| `--intent` | 是 | 显式设计意图 JSON（见下文） |
| `--output-dir` | 是 | 新输出目录；若已含任何文件即报错拒绝覆盖（`:124-126`） |
| `--max-depth` | 否 | 层次读取深度，默认 10 |

## 执行流程与出口

脚本内部顺序（`:141-221`）：`preflight` → 原生只读 `read` DUT 与 testbench（recursive、`max_depth`、含 geometry 与 properties）→ 校验根回读必须是 `saved=true` 且 `modified=false` 的快照（`_root_readback`，`:69-81`）→ `inspect` 选定历史 run → 在 run 证据目录取 `result.json` 与首个 `*.netlist` → `Harness.review_files(dut_readback, op=run result, intent, netlist)` → 写产物。

- 成功：stdout 打印 JSON 摘要（`ok`、`dut_read_operation`、`testbench_read_operation`、`selected_run`、`review_operation`、`coverage_complete`、`pairing_status`、`finding_ids`），退出码 0（`:249-250`）。
- 失败：stderr 打印 `reproduce_review: error: ...`，退出码 2（`:246-248`）。

端到端示例：

```bash
cd <项目根目录>
source .venv/bin/activate
export AETHERPILOT_SKIP_LICENSE_BOOTSTRAP=1
python scripts/reproduce_review.py \
  --project-root <项目根目录> \
  --config "$AETHERPILOT_CONFIG" \
  --library <lib> --dut-cell ota5_dut --testbench-cell <tb_cell> \
  --run-operation <32位hex的已完成run operation_id> \
  --intent examples/ota5_reviewer_intent.json \
  --output-dir <新的空目录>
```

## 产物

- `review.json` — 结构化审查结果：`coverage`（design/topology/operating_point/pairing_status/complete）、`pairing`（设计与 OP 来源配对状态）、`topology`（有界的三类结构候选：输入差分对、电流镜负载、尾支路）、`operating_point`、`checks`、`findings`、`summary`（`reviewer.py:807-831`）。`ok` 只表示审查执行完成，不表示电路通过检查（`reviewer.py:783-784`）。
- `review.md` — `render_review_markdown` 渲染的人读报告。
- `execution.json` — 本轮审计：代码包 SHA-256、runner/intent 的 SHA-256、各 operation_id、preflight 摘要、`safety` 标志（`eda_writes/simulation_started/license_or_pdk_modified/historical_fallback` 全 false，`reproduce_review.py:204-220`）。

## intent 文件

示例 `examples/ota5_reviewer_intent.json`：

```json
{
  "schema_version": 1,
  "description": "Explicit review assumptions for the five-MOS OTA; these are engineering assumptions, not expected findings.",
  "input_pair": ["MN1", "MN2"],
  "tail": ["MN5"],
  "matching": [
    {"objects": ["MP3", "MP4"], "fields": ["Length", "Single_Width"], "ratio": 1}
  ]
}
```

字段含义（按 `reviewer.py` 核实）：

- `description`：自由文本；注意 intent 是**工程假设**，不是期望发现（示例自述）。
- `input_pair` / `tail`：角色断言，引用实例名或稳定层次路径；与拓扑候选核对，不被连接事实唯一确认时产生 `WORKSPACE_ROLE_TOPOLOGY_UNCONFIRMED`，不会把角色强加于器件（`reviewer.py:729-742`）。
- `matching`：参数一致性目标列表；每项 `objects`（别名 `devices`）至少两个对象、`fields`（别名 `parameters`）为要比对的参数字段、可选 `ratio` 声明宽度比目标、可选 `role` 用结构角色（`input_pair`/`tail`/`mirror_load`）代替显式对象（`reviewer.py:655-724`）。`mirrors` 是 `matching` 的兜底别名（`reviewer.py:657`）。
- 另支持工作区目标：`expected_regions`（器件名→工作区）或 `workspace.expected_regions` / `workspace.expected_regions_by_role`（角色→工作区），仅在显式声明时才判断符合性（`reviewer.py:613-617`）。

## 追问纪律

追问只能复用同一快照（同一份 readback/OP/网表证据），明确不重新查询数据库；需要新证据时，由用户显式发起新一轮运行。

## 依据文件

`scripts/reproduce_review.py`（argparse `:232-243`、流程 `:118-221`）、`aetherpilot/reviewer.py`（只读保证 `:819`、intent 语义 `:613-742`、产物结构 `:807-831`）、`aetherpilot/harness.py:599-620`（review operation 的本地执行）、`examples/ota5_reviewer_intent.json`、`AGENTS.md`。
