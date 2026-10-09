# 工作流：写操作（原理图 authoring）

依据：`aetherpilot/contract.py`、`aetherpilot/transactions.py`、`aetherpilot/harness.py:512-527`、`aetherpilot/cli.py:148-162`、`aetherpilot/worker.py:522-543`、`examples/inverter.plan.json`、`examples/ota5_dut.plan.json`。

写操作纪律见 `../SKILL.md`：默认只读；任何写必须用户显式批准；优先走 `change-prepare` → `change-publish --approve` 事务管线；写后必须回读核对。

## 计划 schema 要点（schema_version=1，`schematic.apply`）

`validate_plan`（`contract.py:155-328`）严格校验，未知字段直接拒绝（`fields()`，`contract.py:32-38`）：

- 必填：`schema_version`（整数 1）、`operation`（仅 `"schematic.apply"`）、`target`、`instances`（至少一个）、`nets`（非空、≤10000）、`pins`、`wires`。可选：`mode`、`grid`、`simulation`、`labels`、`inherited_connections`、`port_order`、`routing_policy`、`source_contract`（`contract.py:162`）。
- `target`：`library`/`cell`/`technology` 必填、`view` 默认 `schematic`；v1 只写 schematic view，且 `technology` 只接受 `"018um_PDK"`（`contract.py:172-180`）。
- `mode`：`create`（默认）或 `modify`（`contract.py:165-167`）。`grid` 默认 `0.0625`，必须为正且 ≥1e-6（`contract.py:168-171`）。
- 标识符：`^[A-Za-z0-9_]{1,80}$`，禁止 `APstage_`/`APbackup_` 保留前缀（`contract.py:15,41-46`）。
- 参数只能是字面量：有限数值或工程后缀（`meg|T|G|M|K|k|m|u|n|p|f`）、布尔（转为 `"1"`/`"0"`）、或简单 CDF 选择串；表达式、指令、`.include` 一律拒绝（`contract.py:93-103,124-130`）。JSON 重复键也被 `no_duplicate_keys` 拒绝（`contract.py:331-337`）。
- 坐标：必须落在网格上且 |坐标| ≤ 1e6（`contract.py:114-121`）。wire：2–1000 个点、每段非零且正交、net 必须已声明（`contract.py:233-243`）。
- instance：`master{library, cell, view=symbol}`、`orientation ∈ {R0,R90,R180,R270,MX,MY,MXR90,MYR90}`、`origin`、`parameters`、`connections`（非空，且每个连接必须引用已声明 net）（`contract.py:194-215`）。
- pin：`name` 必须等于其 `net`，`direction ∈ {input, output, inputOutput}`（`contract.py:216-226`）。`port_order` 必须恰好命名每个 pin 一次（`contract.py:227-232`）。
- `labels`/`inherited_connections`/`routing_policy`/`source_contract`/`simulation` 的细粒度规则见 `contract.py:244-441`；`simulation` 块仅支持 ALPS 名义 DC 扫描（`contract.py:340-404`）。

## 最小建图示例（反相器）

`examples/inverter.plan.json` 是完整可校验的最小例子：target 指向新 cell，`instances` 两个 MOS（`018um_PDK` 的 `p18`/`n18` symbol），`nets` 四个，`pins` 与 net 同名，`wires` 全部正交。先离线静态验证（不触碰数据库）：

```bash
python -m aetherpilot validate examples/inverter.plan.json --config "$AETHERPILOT_CONFIG"
```

`validate` 输出 `{"ok": true, "scope": "static-only; master geometry and OA readback require run", "segments": N}`（`cli.py:296-299`）——它不做任何原生写入。

直接写路径是 `python -m aetherpilot run <plan.json> --config <cfg>`（operation `schematic.apply`），但注意：`run` 没有 `--approve` 闸门，对已存在的非托管目标会拒绝覆盖（`target_exists`，`native.py:1252`），`mode=modify` 要求目标已存在且受管（`target_missing`，`native.py:1269`）。按本 skill 纪律，agent 应优先使用下面的事务管线，而不是直接 `run`。

## 修改流程（事务管线）

1. **准备候选**：`python -m aetherpilot change-prepare <plan.json> [--base-sha256 <sha>] --config <cfg>`。harness 在原生侧把计划物化到 staging cell（`APstage_<candidate_id>`），回读并与计划比对；返回的 `readback` 是 candidate 记录，含 `candidate_id`、`candidate.content_sha256`、`baseline`、`validation`、`publishable`（`transactions.py:169-210`）。证据目录内有 `candidate.json`、`candidate-readback.json`；有基线时另有 `baseline-readback.json` 与 `candidate-diff.json`。
2. **人工核对**：把 `candidate-diff.json` 与 candidate 记录展示给用户，取得显式批准。base 在 prepare 后被外部改动时，publish 会报 `external_changes`，需重新 prepare（`transactions.py:117-136`）。
3. **发布**：`python -m aetherpilot change-publish <candidate_id> --domain schematic --candidate-sha256 <candidate.content_sha256> [--base-sha256 <baseline sha>] [--approve] --config <cfg>`。缺 `--approve` 报 `approval_required`（`harness.py:522-523`、`transactions.py:222-223`）；sha 与当前 candidate/baseline 不绑定报 `approval_mismatch`（`transactions.py:227-229`）。发布成功后写 `publication.json` 与 `readback.json`，并对已保存的受管基线建 `APbackup_` 备份（`transactions.py:261-267,307-318`）。
4. **写后回读核对**：`python -m aetherpilot read --library <lib> --cell <cell> --config <cfg>`，将新 operation 证据目录的 `readback.json`（`worker.py:504`）与计划逐项比对；再用 `inspect <operation_id>` 校验证据完整性。

## 失败恢复

发布中断或校验失败时，后续写操作报 `recovery_required`（`transactions.py:296,328`、`native.py:1241,1343`）。恢复入口：`python -m aetherpilot change-recover <publication_operation_id> --approve --config <cfg>`——domain 只支持 `schematic`（`cli.py:159-162`），且必须显式批准（`harness.py:524-527`）。恢复完成前不要对该目标发起新的写操作；无法恢复的状态（如未 checkpoint 的原生副本）按设计是 fail-closed，需人工介入，不强行绕过（`capabilities.py` 的 `schematic_postwrite_recovery` 节）。

## 依据文件

`aetherpilot/contract.py:155-445`（计划校验）、`aetherpilot/transactions.py:169-339`（prepare/publish/recover）、`aetherpilot/harness.py:512-527`（approve 闸门）、`aetherpilot/cli.py:87-99,148-162,296-299,358-359,397-404`（相关子命令）、`aetherpilot/worker.py:522-543`（schematic.apply 原生行为）、`aetherpilot/native.py:1252-1269`（create/modify 目标闸门）、`examples/inverter.plan.json`、`examples/ota5_dut.plan.json`（modify 模式、labels、port_order 的实例）。
