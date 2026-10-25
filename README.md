# api-test-agent

一个用工具调用探索 REST API 的测试 Agent：模型负责选测试点、写结构化用例，工具层限制它能做什么，确定性规则判对错，产物是可重放的用例和普通回归测试。

**当前状态（2026-10-25）：** 探索模块的沙箱、判定套件和开发基准（A0 到 A2）完成，A3 中不需要 API key 的部分用假模型和真实沙箱验证过，尚未调用真实模型。回归模块由 testagent 并入为 `api_test_agent.regression`，提供版本差异分析、回归计划和已登记接口用例执行；合并验收结果见 [docs/acceptance.md](docs/acceptance.md)，后续任务见 [TASKS.md](TASKS.md)。

## 现在有什么

- 沙箱：用 `git archive` 取被测项目（衡仓后端）固定提交 55cf875 的已提交文件，按它自己的锁文件建 Python 3.12 环境，在 127.0.0.1:18080 上用独立 SQLite 数据库运行；每个场景从种子库复制，可重置。两个合成用户 A 和 B，令牌由宿主登录获得。
- 接口片段和规则（Agent 将读取的部分）：[benchmarks/public/](benchmarks/public/)，10 个操作、18 条规则、响应字段说明、10 个任务。
- 判定套件：[src/api_test_agent/conformance.py](src/api_test_agent/conformance.py)，每条规则一个确定性检查，参考构建上 18 条全过。[src/api_test_agent/oracle.py](src/api_test_agent/oracle.py) 独立重算账本汇总。
- 开发基准：[benchmarks/dev/](benchmarks/dev/README.md)，5 个单点缺陷（各自只违反预期的规则）、9 个灵敏度变异体、5 个范围共 10 个任务。它对开发者可见，不是没见过的测试集。
- Agent（A3，离线部分）：[model.py](src/api_test_agent/model.py)（OpenAI 兼容调用，错误分类，关闭 SDK 重试）、[budget.py](src/api_test_agent/budget.py)、[tools.py](src/api_test_agent/tools.py)（`search_spec`、`get_operation`、`http_call`、`submit_case`）、[cases.py](src/api_test_agent/cases.py) 与 [expr.py](src/api_test_agent/expr.py)（用例语言和手写的安全表达式）、[runner.py](src/api_test_agent/runner.py)（循环）、[replay.py](src/api_test_agent/replay.py)（在参考构建和缺陷构建上重放用例）。
- 参考构建探针：`tools/probe_reference.py`；不需要 key 的端到端冒烟：`tools/scripted_run.py`。

验收结果、环境和未验证项在 [docs/acceptance.md](docs/acceptance.md)；范围、批准和预算在 [docs/startup-record.md](docs/startup-record.md)；规则的出处与参考构建观察在 [docs/rules-evidence.md](docs/rules-evidence.md)。

## 运行

先复制 `config.example.toml` 为 `config.local.toml`，填入被测仓库的本机路径（该文件不进 Git）。命令都在项目根目录执行，环境用 [uv](https://docs.astral.sh/uv/) 管理，需要 Python 3.14。

```powershell
uv sync --locked
uv run python -m api_test_agent doctor                  # 检查前置条件，不调用模型
uv run python -m api_test_agent sandbox export          # git archive 固定提交到 runtime/sut/
uv run python -m api_test_agent sandbox venv            # 用被测项目自己的锁文件建环境
uv run python -m api_test_agent sandbox seed            # 建种子库（用户 A、B）
uv run python -m api_test_agent sandbox check           # A1 验收检查，结果写入 artifacts/
uv run python -m api_test_agent benchmark build         # 建五个缺陷构建
uv run python -m api_test_agent benchmark verify        # 参考构建全过，每个缺陷只违反自己的规则
uv run python -m api_test_agent agent witnesses         # 手写见证用例在每个构建上重放
uv run python -m api_test_agent model status            # 需要 API key：账号档位与今天的请求数
uv run python -m api_test_agent model ping              # 需要 API key：两次请求的工具调用检查
uv run python -m api_test_agent agent run --task dev-02 --replay   # 需要 API key：跑一个任务并判定用例
uv run pytest -q -p no:cacheprovider --basetemp=$env:TEMP\api-test-agent-pytest-1
```

## 回归模块

回归模块比较两次发布的 base/head 快照，结合模块映射、业务资料和用例目录给出建议；人工核对计划后，只执行已登记的 pytest 入口，并报告实际结果与覆盖缺口。保留命令入口 `uv run testagent check|plan|reconcile|run|show|runs`。**`plan` 不加 `--stub` 会调用付费模型 DeepSeek；每次真实调用都要先获用户批准。** 离线验证使用 `plan --stub`。

FinUnity 配置模板在 [examples/regression/finunity/testagent.example.toml](examples/regression/finunity/testagent.example.toml)，复制为同目录被忽略的 `testagent.toml`，填写被测克隆的 Python 解释器绝对路径。仓库和资料路径按配置文件所在目录解析。

两个模块共用被忽略的 `subjects/FinUnityServer` 源码克隆。用 FinUnity 2026-10-25 整合时做的 FinUnityServer 完整 bundle（本机备份）恢复：先创建 `subjects/`，执行 `git clone <bundle 本机路径> subjects/FinUnityServer`，再在克隆内 `git remote remove origin`；核对 `main` HEAD 为 `e69f09f18f85cb6516093e739282adba73d1284a`、历史共 24 个提交，再执行 `uv sync --locked --extra dev --python 3.12`。探索模块仍固定使用 `55cf875b9d0350e80cfed14b971b7bec8e22b50b`。克隆不提交、不推送，不改其已跟踪文件。

完整用法见 [回归模块说明](docs/regression/README.md) 和 [架构](docs/regression/architecture.md)。

## 边界

只用合成数据；被测仓库只读；沙箱目前只支持 Windows；不公开、不推送（`benchmarks/dev/` 的补丁含被测私有仓库的几行源码，公开前要先处理）；模型密钥只放环境变量。完整约束见 [AGENTS.md](AGENTS.md)。
