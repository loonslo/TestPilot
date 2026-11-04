# TestPilot · 测试领航员

一个覆盖 REST API 自动化探索、统一用例生命周期管理与发布回归的测试 Agent。以 **`testpilot task`** 作为端到端测试主入口，结合模型生成数据化用例、人工审核冻结、执行前严格守卫、独立规则判定与回归资产双向复用。

项目正式名称为 **TestPilot（测试领航员）**，安装包名称为 `testpilot`。唯一命令入口为 **`uv run testpilot <命令>`**：
- **`task`**（主入口）：端到端任务流（初始化、接口发现、数据化用例设计、人工审核、安全执行、守恒报告与回归桥接）。
- **`plan/run/show/runs`**：发布差异分析与回归执行。
- **`agent run/replay`**：历史沙箱探索 Agent 工具调用循环。

已移除旧的 `api-test-agent`、`testagent` 命令入口。Python 包名 `api_test_agent` 与既有资产文件保留。

最新整体设计见 [架构与流程图](docs/architecture.md)，包含统一任务流、三级能力边界对照表与执行前守卫规范。

---

## 统一任务流与主流程（Task Workflow）

`testpilot task` 是测试领航员的主入口，贯穿以下完整生命周期：

1. **Init（初始化）**：`task create` 创建空草稿任务；`task configure` 登记目标仓库、提交与资料范围；`task validate-target` 校验配置。
2. **Discover（接口发现）**：`task discover` 解析目标 Git 快照、本地文档，或通过显式 `--openapi-url` 获取服务文档后静态解析，导出 `inventory.json`。
3. **Design（用例设计）**：`task materials` 导出字符预算材料；`task design` 构造数据化测试请求，导入或生成含参数理由的用例。
4. **Review（材料导出）**：`task review` 导出 Markdown 格式审核文档与参数理由表，草稿置为 `awaiting_review`。
5. **Approve（人工冻结）**：`task approve` 记录人工审核决定并冻结本轮执行快照（哈希锁定），严禁模型自我批准；`task verify-approval` 复核完整性。
6. **Execute（安全执行）**：`task execute` 在四道执行前守卫防护下对回环目标（127.0.0.1）执行测试，自动注入并脱敏凭据，记录落盘证据。
7. **Report（报告核算）**：`task report` 导出 Markdown/JSON 守恒报告，核算对账公式 `planned = passed + failed + error + blocked + skipped + not_run`。
8. **Bridge（回归复用）**：`task export-regression` 将已批准用例导出至回归用例集 `cases.yaml`，注入 `task_binding` 溯源元数据。

详细输入、输出、角色分工、拒绝条件与恢复方式详见 [架构文档第 3 节](docs/architecture.md#3-统一主流程与术语unified-task-workflow)。

---

## 执行前守卫与能力边界

系统在执行前强制启用四道安全守卫（详见 [docs/architecture.md#5-执行前安全守卫pre-execution-guards](docs/architecture.md#5-执行前安全守卫pre-execution-guards)）：
- **环境身份守卫**：严禁在 `prod`/`production`/`online` 等生产环境下执行；执行配置必须与任务环境严格一致；支持运行服务版本探针校验（`version_endpoint`），未核验时显式警告。
- **未知执行类型守卫**：仅允许已受支持的 `http`/`structured-http-v1`；未知执行类型拒绝伪造 HTTP 发包，直接拦截标记为 `blocked`。
- **商业模型预算守卫**：基于 Token 单价进行发包前最坏情况费用预留（`worst_case_cost`），定价未知或超额直接拦截；回归 `plan` 未加 `--stub` 时必须显式携带 `--allow-paid`。
- **UI 访问边界守卫**：Web Console 仅允许回环同端口 Origin，阻断跨站请求；强制校验 `X-TestPilot-Token` 服务端会话凭据；POST execute 接口返回 403 Forbidden，禁止通过网页直接触发真实测试执行。

**能力边界三级对照表**（字段支持 / 机制支持 / 真实验收）：
- **单项目/单仓库**：已完成机制与合成服务端到端小闭环验证（未经验收真实模型设计质量、未验证复杂生产业务、未验证真人实际审核效果）。
- **多仓库联动 (Multi-Repo)**：已具备字段支持与配置机制，**跨仓库编排与多仓库依赖未真实验收**。
- **多执行器 (Multi-Runner)**：内置单进程 pytest 与统一任务 loopback HTTP 发包已验收；F8 回归桥接已打通回归执行器按 `--case-id` 离散执行统一任务用例；**外部 Runner 未真实验收**。
- **UI 操作 (Web Console)**：提供只读透视、配置与探活，具备严格服务端鉴权；**无浏览器驱动/UI 自动化执行能力，未做 UI 自动化真实验收**。

---

## 快速上手与运行

环境使用 [uv](https://docs.astral.sh/uv/) 管理，需要 Python 3.14。

### 0. 一键启动（开箱即用 · 免输命令行）

在 Windows 资源管理器中，**直接双击根目录下的 `start.bat`** 即可：
- **全自动环境检测**：自动定位 Python 3.14 虚拟环境并完成依赖加载；
- **全自动网页弹窗**：默认 3 秒后自动启动 TestPilot Web 控制台并弹出默认浏览器（`http://127.0.0.1:8080/`）；
- **免输命令行**：在网页端直接透视接口清单、配置模型与查看对账报告；
- 控制台窗口内亦提供一键运行合成示例与输入 URL 抓取在线服务的快捷选项。

---

### 1. 命令行运行方式 (CLI)

下面的示例不需要目标仓库、账号或模型 Key，只导入仓库自带的合成接口文档：

```powershell
uv sync --locked --python 3.14
uv run testpilot task create quickstart --project SyntheticDemo --commit 0000000000000000000000000000000000000000 --scope "*"
uv run testpilot task configure quickstart --document-paths openapi-demo.json
uv run testpilot task discover quickstart --materials-root examples/tasks
uv run testpilot task materials quickstart
```

输出清单位于 `runtime/tasks/quickstart/inventory.json`，应包含 `GET /items`。
全零提交仅用于这个合成示例。重复演练请换任务名；发现完成后的任务不能换输入。
之后的用例设计、人工审核和执行见 [任务流程](docs/task-assets.md)；
导入文档不会调用模型或执行业务用例。

桥接导出的每条用例带显式 `runner: task`，入口写成
`testpilot task --root {task_root} execute <任务> --approval <批准> --case-id <用例>`。
`{task_root}` 由回归项目配置的 `task_root`（默认 `runtime/tasks`，相对配置文件所在目录）在
执行时替换，用例目录因此不含本机绝对路径，也不受子进程 cwd 影响。凭据不写进用例目录：
`task_binding.credential_env` 只记录变量名，执行时按名单从父进程环境转发给 task 子进程，
pytest 入口不会获得这些变量。每条 `--case-id` 执行都会单独落一个执行编号，跑完回到
`ready`，同一批准集合可以继续执行下一条或作为下一轮回归重放。

服务只在运行时提供 Swagger/OpenAPI 文档时，一条命令创建任务并导入：

```powershell
uv run testpilot task import-openapi apple-recruit --project AppleRecruit --commit "<完整40位提交ID>" --url "http://127.0.0.1:8080/v2/api-docs"
```

也接受 Springfox/Knife4j 的 `/swagger-resources` 列表及带 `urls` 的 Swagger 配置。
默认同源重定向与远程 `$ref` 自动下载，单文档20 MB、总量100 MB、最多64份文档。
更大的文档、认证头和跨源聚合通过配置调整，无需改源码：

```powershell
Copy-Item examples/tasks/openapi-import.example.toml openapi.local.toml
# 编辑 openapi.local.toml：按需启用 header_env、allowed_origins 或增大限制。
# 认证值由本机环境变量提供；TOML 只写变量名。
uv run testpilot task import-openapi apple-recruit-auth --project AppleRecruit --commit "<完整40位提交ID>" --url "http://127.0.0.1:8080/v2/api-docs" --openapi-config openapi.local.toml
```

已创建的任务仍可用 `task discover <任务> --openapi-url <地址> --openapi-config <配置>`。
下载失败保留草稿，修正环境/配置后使用 discover 重试。
配置、认证方式、故障处理及边界见 [服务文档接入](docs/openapi-import.md)。

内容原文、最终来源 URL 与内容哈希保留在快照中，版本标为 `unverified-document`；
填写提交不代表已核验服务版本。跨源不转发认证头；不自动交互登录，也不接受 URL 内凭据。
引用失败保留 unresolved，聚合目录中任一服务失败则中止，避免误报完整清单。
UI 当前仍使用 CLI 导入这些服务文档。

### 2. 发布回归模块

```powershell
cd examples/regression/finunity
uv run testpilot check
uv run testpilot plan --base 55cf875 --head e69f09f --stub --run-id review-001 --env-version "<已确认的环境版本>"
# 人工核对计划后执行；若修改过用例先运行 testpilot reconcile review-001
uv run testpilot run review-001 --env-version "<已确认的环境版本>"
uv run testpilot show review-001
```

### 3. 历史探索沙箱
复制 `config.example.toml` 为 `config.local.toml`：
```powershell
uv run testpilot doctor
uv run testpilot agent run --task 1 --replay
```

---

## 边界与安全约定

- 只用合成数据，严禁读取真实凭据或备份；
- 被测仓库克隆保持只读，不向被测环境安装本项目依赖；
- 沙箱当前支持 Windows 环境；
- 模型 API 密钥仅由环境变量读取，严禁写入文件或日志；
- 付费商业模型调用前必须严格检查预算并获得用户批准。
- 完整开发约束与协作契约见 [AGENTS.md](AGENTS.md)。
