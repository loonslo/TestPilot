# 项目协作入口

项目正式名称为 **TestPilot（测试领航员）**，安装包名称为 `testpilot`，唯一命令入口为 `uv run testpilot`：根命令 plan/run/show 等处理回归，agent/sandbox/benchmark/model 等处理探索。已移除旧的 api-test-agent/testagent 命令入口。工作区目录及项目编号为 `TestPilot`，Python 包名保留 `api_test_agent`。最新整体架构与流程见 docs/architecture.md。

先阅读 README.md、TASKS.md、docs/startup-record.md 和 docs/acceptance.md，再修改项目。有实质进展后，先更新 TASKS.md 与 docs/acceptance.md，再按下面的同步契约更新根目录 PROJECT_STATUS.md；不能用文件存在、格式通过或 Git 改动推断完成。

## 本项目约束

- 项目编号 `TestPilot`，包含两个模块。探索模块：用工具调用的 Agent 探索 REST API，用独立规则判对错。首个被测对象是衡仓后端（FinUnityServer）固定提交 55cf875 的本机沙箱副本。范围、批准和预算见 docs/startup-record.md，探索模块当前只做到任务 A3。回归模块：2026-10-25 由 testagent 并入为 `api_test_agent.regression`，按两次发布之间的差异给出回归建议，并执行已登记的接口用例。两个模块的被测源码都来自 `subjects/FinUnityServer`：从 FinUnity 2026-10-25 整合时做的 FinUnityServer 完整 bundle（本机备份）恢复的本机克隆，被忽略，不提交、不推送；探索基准仍固定在 55cf875。
- 只用合成数据。不读取、复制或复用被测项目的 `.env`、`data/`、生产凭据、真实账号或备份；沙箱密钥由本项目生成并只放在被忽略的 `runtime/`。
- 被测仓库只读：探索模块只通过 `git archive`、`git ls-tree`、`git rev-parse` 读取固定提交，副本放在被忽略的 `runtime/sut/`，用它自己的锁文件建环境。`subjects/FinUnityServer` 允许 Git 读命令、回归模块执行用例前 `git checkout <提交>`（用完切回 `main`），以及 `uv sync --locked --extra dev --python 3.12` 按被测项目自己的锁文件建 `.venv`。不向被测环境安装本项目依赖，不改被测仓库已跟踪文件，不提交；`subjects/` 不进入探索 Agent 的工具、提示词或 trace。
- 探索模块的模型 API key 只从环境变量 `API_TEST_AGENT_MODEL_API_KEY` 读取（进程环境优先，缺失时回落到 Windows 用户级环境变量）；回归模块只从 `DEEPSEEK_API_KEY` 读取，配置里只写变量名。key 不写进任何文件、日志、trace 或对话；没有 key 不能调用模型。供应商地址和模型 ID 不是秘密，探索模块写在被忽略的 `config.local.toml`。
- 当前模型是 OpenRouter 的免费路由器 `openrouter/free`：运行器在供应商报告任何费用时停止，并按每天请求数上限（默认 45）和调用间隔限流。换成付费模型前必须先回到每次 ≤ ¥1、A3 总 ≤ ¥30 的金额上限并更新启动记录。预算在每次调用前检查，不靠模型自觉。不配置付费 CI，不公开发布，不推送到远程，除非用户明确要求。
  回归模块用付费模型 DeepSeek `deepseek-chat`，每次 plan 做 1 次分析调用、最多 2 次尝试；金额上限每次 ≤ ¥1、R 系列合计 ≤ ¥30（用户 2026-10-25 确定）。目前只有字符预算，没有按金额的调用前检查，所以每次真实调用前都要用户批准。R0 合并全程不调用模型，plan 必须带 `--stub`。
- 路由器每次请求可能换一个模型：运行产物记录每次回答的模型，混用时在结果里单列（manifest 的 `mixed_model`）；要可复现的评测必须固定到某一个具体模型。
- Agent 只看 `benchmarks/public/`（接口片段、响应字段说明、规则、任务）。缺陷补丁、构建与任务的对应关系、判定套件、现有 126 项测试内容和评分细节不能进入 Agent 的运行环境、提示词或 trace。开发集（五个缺陷、变异体、清单）放在可提交的 `benchmarks/dev/`，对开发者可见，不称为未见测试集；将来的冻结测试集放在被忽略的 `benchmarks/private/`，不提交。
- `benchmarks/dev/` 的补丁文本含被测项目（私有仓库）的几行源码；公开本项目之前必须先确认这些片段可以公开，或改写成不含源码的描述。
- Agent 的 HTTP 工具只能访问配置好的 127.0.0.1 端口和白名单操作，令牌由宿主注入，模型只见账号别名。宿主只清理自己创建且带标记文件的目录。
- 规则里标了 `owner_confirm` 的条目，必须带 `confirmed_on`（用户确认的日期）才能据此设计缺陷、评分或写入简历；改动这些规则要重新确认。简历、README 里的指标只用已验收的实测值，估算要标明。
- 沙箱目前只支持 Windows。运行测试用全新 basetemp：`uv run pytest -q -p no:cacheprovider --basetemp=<新目录>`，缓存和临时目录不作为进度证据。回归模块的测试在 `tests/regression/`，随整体一起跑。

## PROJECT_STATUS 同步契约（v1）

有实质进展时先更新项目内任务和验收记录，再更新根目录 `PROJECT_STATUS.md`；交付前校验以下格式。只维护本项目，不自动写入日常 vault，不把文件存在、格式通过或 Git 改动当作完成。

- 文件以 YAML frontmatter 开始，字段名不得重复；必填 `project_id`、`updated`、`status`、`overview`、`progress`、`next`、`evidence`。
- `project_id` 与登记的工作区相对路径一致，路径中的 `/` 替换为 `-`；本项目为 `TestPilot`。
- `updated` 使用未加引号的真实 `YYYY-MM-DD` 日期，表示摘要维护日期；纯格式修订也可更新，但须在正文记录修订日期、原进展日期及未重新验收的边界。
- `status` 只允许 `active`（进行中）、`waiting`（等待输入）、`paused`（已延期）、`unknown`（待核实）；正在推进统一用 `active`，禁止 `in_progress`。它是项目跟进状态，不代表所有功能已经验收，也不使用 `done`。
- `overview`、`progress`、`next` 为非空字符串；复杂文字用 YAML 引号或块字符串。`progress` 区分实际通过、历史记录及尚未验收的事项。
- `evidence` 为非空字符串列表，只填项目内现存文件的相对路径，使用 `/`；禁止网址、描述文字、绝对路径、`.`/`..` 路径段、隐藏目录/文件、越界链接和 `PROJECT_STATUS.md` 自引用。不得读取或引用凭据、认证、运行目录；`runtime/`、`tmp/`、`temp/`、`logs/`、`storage/logs/` 下的文件不能作为 evidence 正本，核对过的结果应写入稳定任务/验收文档。
- URL、测试命令、结果、部署编号和说明写入项目内任务或验收文件，再由 `evidence` 引用该文件；注明实际验证日期、环境、结果及未验证项。迁移旧摘要文字时标为历史记录搬迁，不能冒充本次复测。
- 同步契约说明和模板位于日常库 `output/项目进展同步/README.md`、`PROJECT_STATUS.template.md`；本节保留完整字段规则，项目离开共同工作区后仍适用。本项目尚未登记到日常 `projects.json`，登记仅在用户要求时核对后进行。跨项目访问须遵守既有项目边界；仅在用户本次明确授权访问日常同步工具且工具可用时，从共同工作区执行 `py -3.14 -B notebook_obsidian/日常/output/项目进展同步/sync_project_status.py --check --project TestPilot`，只检查格式和证据路径，不读取或写入待办。
- 未使用同步工具时，仍须在本项目内逐项自检字段、状态与证据路径，并报告未通过项；不能跳过摘要维护。未知业务进度用 unknown，不因目录存在而推断完成。
