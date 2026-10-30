# TestPilot · 测试领航员

一个覆盖 REST API 探索与发布回归的测试 Agent。探索模块由模型选择测试点、编写结构化用例，工具层限制操作，独立规则判对错；回归模块分析版本变化、生成回归计划，并执行已登记的用例。两个模块都保留可核对、可重放的测试证据。

项目正式名称为 **TestPilot（测试领航员）**，安装包名称为 `testpilot`。统一使用 **`uv run testpilot <命令>`**：`plan/run/show` 处理发布回归，`agent run` 执行探索测试。已移除旧的 `api-test-agent`、`testagent` 命令入口。现有目录 `api-test-agent/`、Python 包名 `api_test_agent` 与配置文件名保留。

最新整体设计见 [架构与流程图](docs/architecture.md)，包含两个模块的数据来源、模型边界、状态与失败分支。

2026-10-29 新需求的[实施方案（已启动）](docs/implementation-plan-20261029.md)：从零发现接口、生成有测试依据与参数理由的用例，人工审核后执行，并统一报告与测试资产。2026-10-30 F1 基础验收通过：可先建空草稿，再为每个任务选择目标、导入统一用例修订并保存审核状态和任务历史，见 [统一资产说明](docs/task-assets.md)。F2 首版静态发现机制已验证，F3 已接入小批设计请求、响应校验和 Markdown 审核材料；F4–F6 的审核冻结、统一执行和报告已完成合成机制验收，并补验异常恢复、预算累计、脱敏及证据覆盖边界；F7 真实模型/项目效果与 F8 维护仍待验收。详见 [验收记录](docs/acceptance.md)；下面保留原有模块说明。

**当前状态（2026-10-25）：** 探索模块的沙箱、判定套件和开发基准（A0 到 A2）完成，A3 中不需要 API key 的部分用假模型和真实沙箱验证过，尚未调用真实模型。回归模块由 testagent 并入为 `api_test_agent.regression`，提供版本差异分析、回归计划和已登记接口用例执行；合并验收结果见 [docs/acceptance.md](docs/acceptance.md)，后续任务见 [TASKS.md](TASKS.md)。

## 现在有什么

- 沙箱：用 `git archive` 取被测项目（衡仓后端）固定提交 55cf875 的已提交文件，按它自己的锁文件建 Python 3.12 环境，在 127.0.0.1:18080 上用独立 SQLite 数据库运行；每个场景从种子库复制，可重置。两个合成用户 A 和 B，令牌由宿主登录获得。
- 接口片段和规则（Agent 将读取的部分）：[benchmarks/public/](benchmarks/public/)，10 个操作、18 条规则、响应字段说明、10 个任务。
- 判定套件：[src/api_test_agent/conformance.py](src/api_test_agent/conformance.py)，每条规则一个确定性检查，参考构建上 18 条全过。[src/api_test_agent/oracle.py](src/api_test_agent/oracle.py) 独立重算账本汇总。
- 开发基准：[benchmarks/dev/](benchmarks/dev/README.md)，5 个单点缺陷（各自只违反预期的规则）、9 个灵敏度变异体、5 个范围共 10 个任务。它对开发者可见，不是没见过的测试集。
- Agent（A3，离线部分）：[model.py](src/api_test_agent/model.py)（OpenAI 兼容调用，错误分类，关闭 SDK 重试）、[budget.py](src/api_test_agent/budget.py)、[tools.py](src/api_test_agent/tools.py)（`search_spec`、`get_operation`、`http_call`、`submit_case`）、[cases.py](src/api_test_agent/cases.py) 与 [expr.py](src/api_test_agent/expr.py)（用例语言和手写的安全表达式）、[runner.py](src/api_test_agent/runner.py)（循环）、[replay.py](src/api_test_agent/replay.py)（在参考构建和缺陷构建上重放用例）。
- 参考构建探针：`tools/probe_reference.py`；不需要 key 的端到端冒烟：`tools/scripted_run.py`。

验收结果、环境和未验证项在 [docs/acceptance.md](docs/acceptance.md)；范围、批准和预算在 [docs/startup-record.md](docs/startup-record.md)；规则的出处与参考构建观察在 [docs/rules-evidence.md](docs/rules-evidence.md)。

## 在其他机器使用

用户已授权将当前版本上传到私有仓库 [loonslo/TestPilot](https://github.com/loonslo/TestPilot)。克隆、锁定依赖安装和环境边界见 [安装说明](docs/installation.md)。目标源码、本机配置、凭据和 runtime 不随代码上传，实际环境仍需完成 F7 验收。

## 运行

先复制 `config.example.toml` 为 `config.local.toml`，填入被测仓库的本机路径（该文件不进 Git）。探索命令在项目根目录执行；回归可在配置目录执行，或通过 `--config` 指定配置。环境用 [uv](https://docs.astral.sh/uv/) 管理，需要 Python 3.14。

```powershell
# 项目根目录
uv sync --locked
uv run testpilot --help
uv run testpilot doctor

# 回归配置目录（先按下节准备本机配置）
cd examples/regression/finunity
uv run testpilot check
uv run testpilot plan --base 55cf875 --head e69f09f --stub --run-id review-001 --env-version "<已确认的环境版本>"
# 人工核对计划后执行；修改过用例先运行 testpilot reconcile review-001
uv run testpilot run review-001 --env-version "<已确认的环境版本>"
uv run testpilot show review-001
```

## 回归模块

回归模块比较两次发布的 base/head 快照，结合模块映射、业务资料和用例目录给出建议；人工核对计划后，只执行已登记的 pytest 入口，并报告实际结果与覆盖缺口。统一命令为 `uv run testpilot check|plan|reconcile|run|show|runs`。**`plan` 不加 `--stub` 会调用付费模型 DeepSeek；每次真实调用都要先获用户批准。** 离线验证使用 `plan --stub`。

FinUnity 配置模板在 [examples/regression/finunity/testagent.example.toml](examples/regression/finunity/testagent.example.toml)，复制为同目录被忽略的 `testagent.toml`，填写被测克隆的 Python 解释器绝对路径。仓库和资料路径按配置文件所在目录解析。

两个模块共用被忽略的 `subjects/FinUnityServer` 源码克隆。用 FinUnity 2026-10-25 整合时做的 FinUnityServer 完整 bundle（本机备份）恢复：先创建 `subjects/`，执行 `git clone <bundle 本机路径> subjects/FinUnityServer`，再在克隆内 `git remote remove origin`；核对 `main` HEAD 为 `e69f09f18f85cb6516093e739282adba73d1284a`、历史共 24 个提交，再执行 `uv sync --locked --extra dev --python 3.12`。探索模块仍固定使用 `55cf875b9d0350e80cfed14b971b7bec8e22b50b`。克隆不提交、不推送，不改其已跟踪文件。

完整用法见 [回归模块说明](docs/regression/README.md) 和 [架构](docs/regression/architecture.md)。

## 边界

只用合成数据；被测仓库只读；沙箱目前只支持 Windows；仅按用户授权推送至私有仓库，不公开发布（`benchmarks/dev/` 的补丁含被测私有仓库的几行源码，公开前要先处理）；模型密钥只放环境变量。完整约束见 [AGENTS.md](AGENTS.md)。
