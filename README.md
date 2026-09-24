# AetherPilot

## 项目定位

AetherPilot 是一个面向 EDA 数据工作的 Python Harness：它把结构化计划、保存态设计事实、网表/结果解析、受限变更和证据记录组织成可验证的接口。实际数据库写入、仿真和工艺规则由可选 backend 负责；公开源码不包含厂商运行时、工艺库、许可证、模型或任何本机工程数据。

它接收结构化计划或已保存事实，执行格式校验、关系整理、差异比较、结果解析和证据摘要；使用者或其 Agent 提供自己的 EDA 环境、工艺根目录、模型、许可证和 workspace 配置。

## 公开目录

| 路径 | 内容与作用 |
| --- | --- |
| `aetherpilot/` | 完整 Python 实现：公共 facade、通用数据层、校验、解析、布局/网络算法、结果处理和可选 native backend。 |
| `aetherpilot/core/` | 不依赖具体数据库或工艺的 contract/readback 原语。 |
| `aetherpilot/schemas/` | 机器可读的计划和结果结构。 |
| `tests/` | 公开离线测试；原生测试和合成运行夹具不进入公开包。 |
| `scripts/` | `run-unit-tests.sh`、`reproduce_review.py` 等可公开入口；许可证和本机启动脚本不公开。 |
| `docs/deployment.md` | 使用者或 Agent 配置自己的 backend、PDK、模型、许可证和 workspace 的部署说明。 |
| `pyproject.toml`、`MANIFEST.in` | Python 包元数据和发行文件边界。 |
| `config.example.json` | 不含工艺、许可证和机器路径的配置模板。 |
| `LICENSE` | MIT 许可证。 |

## 公开模块与功能

| 模块 | 接收 | 处理 | 输出 |
| --- | --- | --- | --- |
| `artifacts.py` | 文件、目录和摘要对象 | 原子写入、哈希、大小边界和安全字段处理 | 可追踪的文件摘要 |
| `capture.py` | 有界任务和输出目录 | 捕获过程输出并执行资源限制 | 结构化执行记录 |
| `changes.py` | 基线、候选和差异描述 | 检查基线一致性、字段范围和发布前条件 | 可审查的变更结果 |
| `hierarchy.py`、`netgraph.py` | 层次对象、实例和网络关系 | 递归遍历、连接投影、位/总线关系整理 | 规范化层次与网络事实 |
| `layout.py`、`layout_append.py`、`layout_edit.py` | 几何对象和编辑计划 | 坐标、图形、追加和差异的通用处理 | 可比较的几何数据 |
| `parameters.py`、`signals.py` | 参数、单位和信号描述 | 类型、单位、方向和表达式的规范化 | 稳定的字段表示 |
| `placement.py`、`routing.py`、`planner.py` | 位置、连接和约束 | 有界规划、冲突检查和正交路径计算 | 可交给 backend 的中间表示 |
| `results.py` | 原始结果摘要或波形记录 | 数值、单位、状态和来源整理 | 可比较的结果对象 |
| `errors.py`、`resources.py` | 异常和资源状态 | 统一错误分类、时间/磁盘/内存预算 | 明确的失败原因和预算状态 |
| `backend.py` | 本地配置和环境入口 | 静态探测 native backend，或返回 offline stub | backend 状态和机器可读失败原因 |
| `harness.py`、`cli.py` | 计划、目标和配置 | 统一编排离线或 native 操作 | 结构化结果、错误和证据 |

## 可选 backend

本机原生数据库、工艺器件、仿真器、许可证、工作区锁、GUI 会话和原始运行证据不由项目提供。backend 从使用者配置读取环境入口、工作区、工艺根目录、模型和输出目录；没有 backend 时，通用解析、校验、差异和结果整理仍可运行，原生操作返回明确的 `backend_unavailable`。

`backend-status` 是无副作用的静态探测，不会 source 环境脚本、启动许可证服务、打开 GUI 或修改 workspace；`preflight` 才会在使用者明确提供 backend 后验证产品 Python、PyAether、workspace 和 simulator 配置。

## 数据流

1. 调用者提供结构化计划、保存态事实或结果文件。
2. 核心层校验字段、标识、来源、资源预算和基线摘要。
3. 关系层把实例、端口、网络、层次和几何转换为稳定的中间表示。
4. 可选 backend 执行数据库或仿真动作，并返回保存后的事实。
5. 结果层整理状态、数值、单位、来源和限制，供程序或人工复核。

## 最小部署入口

```bash
cp config.example.json local-config.json
# 编辑 local-config.json，填写使用者自己的 backend、环境、workspace、PDK、模型和输出目录
python -m aetherpilot backend-status --config local-config.json
python -m aetherpilot preflight --config local-config.json
```

`local-config.json` 只在使用者本地保存，不应提交。完整字段、Agent 检查顺序和 backend 行为见 [部署说明](docs/deployment.md)。

## 设计原则

- 事实来自保存的数据或明确输入，不把计划对象当作数据库回读。
- 不确定的字段保留不确定状态，不用默认值伪造工艺语义。
- 变更先校验基线、范围和资源预算，再交给可选 backend 执行。
- 结果带来源和摘要，便于比较、复现和人工复核。
- 没有配置 backend 时，通用解析和校验仍应可独立运行；需要原生操作的入口必须明确报告 backend 不可用。

## 边界

公开核心不承诺任意工艺、任意厂商语法、物理签核、远程主机控制或自动完成未声明的 EDA 操作。任何工艺规则、器件模型和环境配置都必须由使用者在自己的 backend 中提供。

## 验证与发行

当前公开候选树可以安装、导入完整 Python 包并运行公开离线测试；本机 backend 另有独立 preflight 验证。wheel/sdist 构建只包含公开源码、schema、部署说明、README、配置模板、许可证和公开测试，不包含 `reservation/`、`resources/`、examples、原生运行证据或许可证材料。

公开部分采用 MIT License，版权归属为 `AetherPilot contributors`。外部厂商软件、工艺库、模型、许可证和任何本机运行材料不由本许可证再授权。
