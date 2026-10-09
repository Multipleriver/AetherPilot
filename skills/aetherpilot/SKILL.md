---
name: aetherpilot
description: 当需要驱动 AetherPilot harness（华大九天 Aether 平台的 Python 二次开发项目）执行只读审查、原理图/版图/符号/库变更、仿真或证据核查时使用本 skill；它给出三条纪律、快速开始、错误码对策与能力边界。
---

# AetherPilot 操作手册（agent 入口）

## 定位

AetherPilot 是把结构化计划、保存态设计事实、网表/结果解析、受限变更和证据记录组织成可验证接口的 Python harness（版本 0.1.0，运行时零第三方依赖）。统一入口是机器可读 JSON CLI：`python -m aetherpilot <command> [--config <cfg>]`，stdout 恒为单行 JSON，失败退出码非零。实际数据库写入、仿真和工艺规则由可选原生 backend（Aether 2026.03 + PyAether + 官方 018um_PDK）负责；没有配置 backend 时，通用解析与校验仍可离线运行，需要原生操作的入口明确返回 `backend_unavailable`。

## 三条纪律

1. **默认只读。** 优先使用 `read`、`review`、`inspect`、`snapshots`、`diff`、`capabilities`、`backend-status` 等只读入口；除非用户明确要求，不发起任何写操作、仿真或库变更。
2. **写操作必须显式批准并走事务管线。** schematic/layout 变更走 `change-prepare` →（向用户展示 candidate 与 diff，取得批准）→ `change-publish --approve`；library/category 的删除、重命名等破坏性操作同样必须经用户批准后才加 `--approve`；发布中断后的 `change-recover` 也需 `--approve`。不得在用户未批准时自行附加 `--approve`。
3. **每个 operation 后检查证据。** 每次调用后读取 `<artifact_root>/<operation_id>/result.json`（`ok`/`stage`/`errors`）与 `manifest.json`（逐文件 sha256 清单）；失败时读同目录 `journal.jsonl` 定位阶段。可用 `python -m aetherpilot inspect <operation_id>` 校验证据完整性。注意：`ok=true` 只表示操作执行成功，不等于电路工程验收。

## 快速开始

```bash
cd <项目根目录>
source .venv/bin/activate
export AETHERPILOT_CONFIG=/path/to/local-config.json   # 或每次调用传 --config <path>
python -m aetherpilot backend-status --config "$AETHERPILOT_CONFIG"
python -m aetherpilot capabilities --config "$AETHERPILOT_CONFIG"
```

`backend-status` 是无副作用静态探测：不 source 环境脚本、不启动许可证服务、不开 GUI、不改 workspace，只报告 `available`/`reason`（见 `aetherpilot/backend.py:140`）。`capabilities` 返回静态能力声明加上当前 backend 状态（`aetherpilot/cli.py:20`）。配置文件从 `config.example.json` 复制，字段含 `backend`、`env_file`、`workspace`、`artifact_root`、`pdk_root`、超时与快照配额等；相对路径以配置文件位置为基准；配置文件属私有材料，绝不提交。

## 工作流索引

- `references/cli-contract.md` — JSON CLI 调用约定：子命令清单与参数、退出码、输出结构、证据目录结构。
- `references/workflow-review.md` — 只读审查全流程（稳定入口 `scripts/reproduce_review.py`）。
- `references/workflow-authoring.md` — 写操作流程：计划 schema、事务管线、失败恢复、写后回读。

## 错误码速查与对策

所有错误以 `errors[].code` 给出（错误对象结构见 `aetherpilot/errors.py:11`）。下表每个 code 均已在源码中核实出处。

| code | 含义 | agent 对策 | 出处 |
| --- | --- | --- | --- |
| `backend_unavailable` | 原生 backend 不可用：`env_file` 缺失、`AETHER_ROOT` 缺失、产品 Python 缺失、PyAether 缺失，或配置显式选择 `offline`/`stub`；`details.reason` 给出具体原因 | 改用离线子命令继续能做的事；如实报告 reason；绝不把 stub/离线结果冒充原生运行结果 | `aetherpilot/errors.py:34`、`aetherpilot/backend.py:160-174` |
| `workspace_busy` | workspace 锁被其他进程持有，本次未做任何变更 | 停止并重试交由用户决定；告知存在并发会话；不删锁、不接管 | `aetherpilot/runtime.py:29` |
| `recovery_required` | 目标存在未决的发布恢复（上次 publish 中断或校验失败） | 先报告用户，经批准后 `change-recover <publication_operation_id> --approve`，再重做变更；不强行覆盖 | `aetherpilot/transactions.py:296,328`、`aetherpilot/native.py:1241,1343` |
| `approval_required` | 发布/破坏性操作缺少显式批准（publish、recover、library/category 删除与重命名、cell 删除等） | 向用户请求批准并展示将发生的变化；批准后才带 `--approve` 重试 | `aetherpilot/transactions.py:223`、`aetherpilot/harness.py:522-527`、`aetherpilot/library.py:69,107`、`aetherpilot/cells.py:54` |
| `invalid_plan` | 计划违反声明式契约（字段、标识符、网格、网络声明、字面量参数等） | 按 `errors[].details`（missing/unknown 字段等）修计划；先用 `validate` 离线复查再提交 | `aetherpilot/contract.py:29`、`aetherpilot/harness.py:463` |
| `result_provenance` | 波形/运行结果缺少来源、轴非单调、非新鲜结果，或 run manifest 指纹/内容校验失败 | 补齐来源信息（source/manifest）；只用新鲜仿真结果；不导出无来源数据 | `aetherpilot/results.py:37,49,108`、`aetherpilot/maestro_results.py:163-174`、`aetherpilot/harness.py:598` |
| `snapshot_quota_exceeded` | 快照超出本地预算、配额或磁盘预留；受保护快照不会被挤占 | 报告用户调整 `snapshot_quota_bytes` 或清理；不擅自删除快照 | `aetherpilot/snapshots.py:34,89,119` |
| `target_exists` / `target_missing` | create 目标已存在（或非托管目标拒绝覆盖）/ modify、读取、仿真目标不存在 | 核对 library/cell 名；create 换新 cell 名；modify 前先 `read` 确认目标存在且已保存 | `aetherpilot/native.py:1252-1269`、`aetherpilot/worker.py:157,227,400,491` |
| `external_changes` | 基线在 prepare 之后被外部改变，或目标在发布前变动 | 重新 `read` 并重新 `change-prepare`；不强行 publish | `aetherpilot/transactions.py:128-135`、`aetherpilot/native.py:1258,1311` |
| `user_view_open` / `native_view_locked` | 目标被 GUI 编辑器打开或持原生锁 | 请用户关闭视图/解除锁定；不删锁、不关闭用户会话 | `aetherpilot/transactions.py:115`、`aetherpilot/symbol_native.py`（grep 核实） |
| `license_unavailable` / `license_missing` | 原生运行时无法取得许可证 / worker 环境未配置许可证变量 | 报告用户；不启停许可证服务、不改许可证配置 | `aetherpilot/runtime.py:107`、`aetherpilot/worker.py:601` |
| `integrity_failed` | 证据包缺失、不完整或被篡改（manifest 校验失败） | 不采用该证据做任何判断；如实报告 | `aetherpilot/artifacts.py:210` |
| `operation_failed` / `native_exception` | 兜底错误（未分类异常） | 读该 operation 目录的 `journal.jsonl` 与 `native.stderr.log`；原样报告，不猜测根因 | `aetherpilot/harness.py:719`、`aetherpilot/worker.py:625` |

## 能力边界（不得向用户承诺）

以下边界来自 `aetherpilot/capabilities.py` 的静态声明，agent 不得承诺其中任何一项已完成或可签核：

- **deferred（明确不做）**：`drc`、`lvs`、`pex`、`remote`、`full_optimizer`、`reviewer_engine`（`capabilities.py:108`）。
- **`release.complete=False`**，剩余 gate：`g1_native_hierarchical_multicell_atomic_materialization`（g1 层次物化）、`g2_native_vector_composition_and_annotation_matrix`（g2 向量组合）、`g3_remaining_native_analysis_and_general_hspice_writer`（g3 部分分析与通用 HSPICE writer）、`official_pdk_statistical_model_availability`、`official_pdk_npn_model_parameters`（`capabilities.py:11-15`）。
- symbol、maestro、simulation、pdk、layout 等域均标 `native_certified: false`，原生证据为 `backend-dependent`；`capabilities` 的静态字段不构成安装认证，`backend-status` 的 `available` 也不等于原生能力已验收。
- 层次网表导入的 `--materialize` 当前被 gate 拒绝（`native_materialization_unavailable`，`aetherpilot/worker.py:323`）。
- 任何 `ok=true`、计划验证通过、退出码 0 或 Reviewer 结论都不是电路工程验收；工艺合法性、物理签核不在范围内。

## 依据文件

`aetherpilot/cli.py`、`aetherpilot/errors.py`、`aetherpilot/backend.py`、`aetherpilot/capabilities.py`、`aetherpilot/artifacts.py`、`aetherpilot/runtime.py`、`aetherpilot/transactions.py`、`aetherpilot/harness.py`、`aetherpilot/worker.py`、`config.example.json`、`AGENTS.md`。当前状态与范围另见 `docs/PROJECT_STATUS_AND_GAPS.md`（历史文档中的"已通过"不等于今天的总体验收）。
