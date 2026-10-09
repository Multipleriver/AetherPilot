# CLI 契约：`python -m aetherpilot`

依据：`aetherpilot/cli.py`（`parser()` 第 30-284 行、`main()` 第 287-513 行）、`aetherpilot/artifacts.py`、`aetherpilot/harness.py`、`aetherpilot/backend.py`、`aetherpilot/runtime.py`、`aetherpilot/worker.py`。

## 调用形式与退出码

```bash
python -m aetherpilot <command> [args] [--config <path>] [--json]
```

- `--config`：可信私有运行配置 JSON；也可用环境变量 `AETHERPILOT_CONFIG` 代替（`aetherpilot/config.py:86`）。`--json` 恒为真（输出始终是 JSON），可省略。
- stdout 是单行 JSON（`json.dumps(..., sort_keys=True, ensure_ascii=False)`，`cli.py:512`）。退出码：`result.ok` 为真返回 0，否则返回 1（`cli.py:513`）。
- `schema` 与 `capabilities` 两个命令输出原始文档；其余命令输出经 `redact` 脱敏（路径、IP、许可证端点、用户名等替换为 `<private-path>` 等占位符，`cli.py:511`、`artifacts.py:81-97`）。
- 解析输入 JSON 时使用 `no_duplicate_keys`，重复键直接拒绝（`cli.py:516-517`、`contract.py:331-337`）。

## 输出 JSON 结构

除 `schema`/`capabilities` 外，所有命令返回统一结果信封（`artifacts.py:100-105`）：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 恒为 1 |
| `ok` | 操作是否成功（不代表工程验收） |
| `operation_id` | 32 位小写十六进制证据目录名 |
| `operation` | 内部 operation 名（如 `schematic.apply`） |
| `stage` | 终止阶段（`plan`/`session`/`read`/`publish`/`complete` 等） |
| `changes` | 变更记录列表 |
| `readback` | 操作回读/返回数据 |
| `metrics` | 指标（如仿真指标、review 统计） |
| `artifacts` | 证据目录内文件相对路径清单 |
| `warnings` / `errors` | 警告与错误列表 |

错误对象结构（`errors.py:11-18`）：`category`（异常类名）、`stage`、`code`（机器可读错误码，见 SKILL.md 速查表）、`message`、`details`。

## 证据目录结构

每个经 `Harness._execute` 的操作在 `<artifact_root>/<operation_id>/` 下生成私有证据目录（权限 0o700，`artifacts.py:119-184`）：

- `journal.jsonl` — 逐阶段追加的事件日志（time/operation_id/stage/status）；`stage.json` 是最新一条事件。
- `request.json` — 发给原生 worker 的请求（原生操作）；`session.json` — 会话记录（owner/pid/state/resources）。
- `result.json` — 最终结果信封（与 stdout 一致，含 `artifacts` 清单）。
- `manifest.json` — `{"schema_version": 1, "operation_id", "privacy": "private", "created_at", "files": [{path, size, sha256}]}`；manifest 不覆盖自身与 `job-status.json`（`artifacts.py:108-116`）。
- 依操作另有 `plan.json`、`input.json`、`readback.json`、`candidate.json`、`review.json`/`review.md`、`native.stdout.log`/`native.stderr.log` 等。

核查命令：`inspect <operation_id>` 校验 manifest 与 result 的一致性（篡改报 `integrity_failed`，`artifacts.py:187-210`）；`export-summary <operation_id> --output <path>` 导出只含状态的公开摘要（不含设计数据，目标已存在则报 `target_exists`，`cli.py:499-503`、`artifacts.py:213-224`）。

## 子命令清单

「离线」= 不启动 Aether 运行时、不需要原生 backend；「原生」= 经 `AetherRuntime` 调用 `require_backend`，backend 不可用时报 `backend_unavailable`（`runtime.py:68`、`backend.py:177-186`）。原生操作还会取 workspace 排他锁（冲突报 `workspace_busy`）并以产品 Python 子进程执行（`runtime.py:55-128`、`worker.py`）。

### 常用命令详表

| 命令 | 参数（按 parser 核实） | 离线/原生 | 说明 |
| --- | --- | --- | --- |
| `read` | `--library --cell [--view schematic] [--max-depth 20] [--no-recursive] [--view-configuration F]` | 原生 | 只读回读保存态 schematic；证据含 `readback.json`（`worker.py:486-504`） |
| `review` | `--readback F [--op P] [--intent F] [--netlist P] [--output-dir D]` 或 `--library --cell [--view] [--max-depth] [--no-recursive]` | 文件模式离线；`--library/--cell` 模式先原生 read | 只读结构/工作点审查；`--output-dir` 下写 `review.json`/`review.md`，已存在报 `target_exists`（`cli.py:446-479`） |
| `simulate` | `<setup.json> --library --cell` | 原生 | 运行仿真（setup 经 `validate_analysis_request` 校验） |
| `run` | `<plan.json>` | 原生 | 直接执行 `schematic.apply` 计划（写路径，见 workflow-authoring） |
| `validate` | `<plan.json>` | 离线 | 静态校验 + 编译布线；`scope` 注明 static-only（`cli.py:296-299`） |
| `schematic-plan` | `<plan.json> <constraints.json> [--routing R.json]` | 原生 | 布局布线规划，不写数据库（`database_written: false`，`worker.py:472-485`） |
| `natural-plan` | `[description] \| --description-file F --library --cell [--technology 018um_PDK] [--constraints F]` | 不带 `--constraints` 时离线；带约束走原生 | 受限自然语言编译为计划；description 与 description-file 二选一（`cli.py:420-425`、`harness.py:546-557`） |
| `natural-apply` | 同 `natural-plan` | 原生 | 编译并创建新 schematic；目标已存在报 `target_exists`（`worker.py:444-446`） |
| `maestro` | `<action> --library --cell [--view state] [--request R.json] [--output-dir D]` | 原生 | 单个 Maestro/MDE 动作；request 必须是 JSON 对象（`cli.py:376-385`） |
| `maestro-run` | `--library --cell [--view state] [--test T] [--output-dir D]` | 原生 | 运行 Maestro 测试 |
| `maestro-capabilities` | 无额外参数 | 原生 | 运行时派生的 MDE 类/writer 能力矩阵 |
| `waveform-normalize` | `<input.csv> --analysis A [--source S.json]` | 离线 | CSV 波形归一化为 `aetherpilot.waveform.v1`（`cli.py:313-317`） |
| `waveform-validate` | `<input.json> [--analysis A]` | 离线 | 校验波形结果结构 |
| `snapshots` | 无额外参数 | 离线 | 列出本地快照库 |
| `snapshot` | `--library --cell [--view] [--max-depth] [--no-recursive] [--view-configuration F]` | 原生 | 创建保存态快照 |
| `snapshot-inspect` | `<snapshot.json>` | 离线 | 校验快照完整性 |
| `snapshot-store-inspect` / `snapshot-retain` | `<snapshot_id>` / `<snapshot_id> <reference>` | 离线 | 查看/保留快照 |
| `inspect` | `<operation_id>` | 离线 | 校验证据包完整性并返回 result |
| `export-summary` | `<operation_id> --output <path>` | 离线 | 导出状态摘要 |
| `diff` | `<before.json> <after.json>` | 离线 | 两份 schematic 回读的结构化 diff |
| `validate-change` | `<readback.json> <change_set.json>` | 离线 | 校验候选变更并输出 candidate/diff |
| `change-prepare` | `<plan.json> [--base-sha256 S]` | 原生 | 构建候选视图（staging cell），返回 candidate 记录 |
| `change-publish` | `<candidate_id> --domain {layout,schematic} --candidate-sha256 S [--base-sha256 S] [--destination D.json] [--approve] [--allow-unmanaged]` | 原生 | 发布候选；无 `--approve` 报 `approval_required`（`harness.py:522-523`） |
| `change-recover` | `<publication_operation_id> [--approve]` | 原生 | 恢复中断的 schematic 发布；domain 固定 `schematic`（`cli.py:159-162`） |

### 其余命令（按 parser 顺序归组）

- 静态/元信息（离线）：`capabilities`、`backend-status`（别名 `backend`）、`schema {plan,result}`、`validate-layout <plan>`、`validate-analysis <setup>`、`layout-diff <before> <after>`、`layout-summary <readback>`、`layout-select <readback> [--bbox x1 y1 x2 y2] [--layer L] [--purpose P] [--net N] [--intersects]`、`pdk-catalog`（读本地 `pdk_root`，离线）。
- 库与类别（原生；删除/重命名/增删 cell 带 `--approve` 闸门）：`libraries`、`library-get <name>`、`library-create <name> [--path P] [--technology-library T]`、`library-delete <name> --approve`、`library-rename <name> <new_name> --approve`、`library-set-technology <name> <tech> --approve`、`category-list <lib>`、`category-create <lib> <cat> [--cells F] [--approve]`、`category-delete <lib> <cat> --approve`、`category-cells <lib> <cat>`、`category-add-cell <lib> <cat> <cell> --approve`、`category-remove-cell <lib> <cat> <cell> --approve`、`category-rename <lib> <cat> <new_name> --approve`。
- 原理图/符号/版图写与读（原生）：`layout-run <plan>`、`symbol-run <plan>`、`symbol-append <plan> --base-sha256 S`、`layout-append <plan> --base-sha256 S`、`symbol-generate --library --cell [--sides F] [--symbol-shape transmission] [--pin-space 0.125] [--pin-length 0.25] [--pin-shape round] [--pin-origin topLeft] [--sort-pins {alphanumeric,geometric}] [--overwrite]`、`symbol-read --library --cell [--view symbol]`、`layout-read --library --cell`、`layout-session --library --cell <actions.json>`、`gds-export --library --cell --output O --layer-map M [--overwrite]`、`layout-find-via <name> --library --cell`、`layout-delete-cell <baselines.json> --library --cell [--approve]`、`cell-inspect --library --cell`、`smoke --lib L --cell C [--technology 018um_PDK]`、`preflight`、`pdk-verify`。
- 网表（原生）：`netlist-export --library --cell [--output O]`、`netlist-export-hierarchical --library --cell [--output O]`、`netlist-export-native --library --cell [--view schematic] [--output-dir D] [--temperature 27] [--format {hspice,spectre}]`、`netlist-import <netlist> --library --cell [--require-metadata]`、`netlist-import-hierarchical <netlist> --library --cell [--format {auto,spice,cdl,spectre}] [--materialize]`（`--materialize` 当前被 gate 拒绝：`native_materialization_unavailable`，`worker.py:323`）、`netlist-migrate <netlist> <manifest.json> --library --cell [--approve] [--materialize]`。
- 风格/可读性与重画（新增）：`lint --library --cell [--view schematic] [--style analog_default]`（原生只读核查）或 `lint --plan plan.json`（离线）、`recreate --library --cell --new-cell <name>`（原生，重画到新 cell）；规则集与边界见 `style-guide.md`。
- 证据与报告（新增）：`capture-image --library --cell [--view schematic]`（原生，PNG 入证据）、`report --run-operation <id> --output <新.html>`（离线，自包含 HTML 报告）。
- Agent 接入与文档检索（新增）：`mcp`（只读+审批闸门写工具的 MCP stdio server）、`docs-index --root R --db D`（离线建索引）、`docs-search <query> --db D [--limit N]`（离线检索）。

## 依据文件

`aetherpilot/cli.py:30-284`（parser）、`aetherpilot/cli.py:287-513`（main 与退出码）、`aetherpilot/artifacts.py:100-224`（信封、证据目录、inspect、公开摘要）、`aetherpilot/harness.py:450-729`（`_execute` 离线分支与委派）、`aetherpilot/backend.py:140-186`（状态与 `require_backend`）、`aetherpilot/runtime.py:19-128`（锁与 worker 启动）、`aetherpilot/worker.py:24-587`（各 operation 的原生行为）。
