---
project_id: TestPilot
updated: 2026-11-04
status: active
overview: "TestPilot（测试领航员），唯一入口 uv run testpilot；以统一任务流（task）为主入口。重构 10 步统一流程契约，确立三大核心分界与两大设计原则；厘定多仓库、多执行器与 UI 操作三级能力边界；修补环境版本探针、未知执行类型、最坏情况费用预留与 UI 会话授权四道安全守卫；完成真实回环 HTTP 合成服务端到端小闭环验证；打通 F8 资产桥接与离散执行复用机制（export-regression 导出带 --case-id 至 cases.yaml 由 runner 调度）。"
progress: "2026-11-04 落实审查报告 5 项缺口修复与机制验收：接通 F8 回归桥接执行（runner 注册 task_command 调度器，支持 --case-id 离散执行与 YAML 损坏拒绝覆盖保护）；落地调用前最坏情况费用预留（worst_case_cost 拦截，商业模型未定价拦截）；加强 UI 服务端强校验（Host/Origin 端口白名单、Sec-Fetch-Site 跨站阻断与 X-TestPilot-Token 凭据授权）；增加运行环境版本探针核验（version_endpoint 探针核对 target_commit，未核验报告告警）；收窄小闭环表述为合成服务集成测试并明确未经验收边界。全量 450 项测试（净增 9 项）与 Ruff 检查/格式化全通。2026-11-04 续做接口采集评审修补：第 6 项合并伪冲突修复后全量 452 项；再完成第 4/5/7 项（OpenAPI 跨文件/服务器/参数覆盖、文档与源码版本关联、快照集内 Pydantic 请求模型展开）后全量 458 项测试与 Ruff 全通；认证语义与集外模型保持如实未解析。 续审更正：原跨文件引用仅告警、版本全验证仅局部变量；本次补齐已选快照跨文件解析与来源、服务器独立覆盖和清单字段，全量462项通过，最终参数保护后定向34项通过；真实接口全集仍未验收。"
next: "在具备真实网络与模型 Key 的测试机端到端执行通用模型与真实项目生成（推进 F7 真实项目效果验收）；基于 F8 桥接导出的 cases.yaml 联动 regression plan/run 执行复用验证。"
evidence:
  - docs/task-assets.md
  - README.md
  - TASKS.md
  - docs/startup-record.md
  - docs/acceptance.md
  - docs/rules-evidence.md
  - benchmarks/dev/README.md
  - docs/regression/README.md
  - docs/regression/architecture.md
  - docs/architecture.md
  - docs/architecture.png
  - docs/workflow.png
  - docs/implementation-plan-20261029.md
---

# TestPilot · 测试领航员 · 项目概览与进度

本页是本项目的概览与进度摘要。更新进度时先核对证据文件及实际验收；任务细节以 TASKS.md 和 docs/acceptance.md 为准。只根据有证据的变化修改状态与日期；未验证事项保留“待核实”，不把文件存在或 Git 改动当作完成。

2026-10-15 新建项目并完成 A0、A1、A2，以及 A3 中不需要 API key 的部分（历史记录）。范围、批准、预算和 OpenRouter 免费模型的约束在 docs/startup-record.md；验收命令、环境、结果和未验证项在 docs/acceptance.md；规则的出处和参考构建观察在 docs/rules-evidence.md；开发基准的缺陷、任务和命令在 benchmarks/dev/README.md。

2026-10-25 完成 R0：原 testagent 作为回归模块并入，程序、提示词、测试、示例与说明分别位于 `src/api_test_agent/regression/`、`tests/regression/`、`examples/regression/` 与 `docs/regression/`。两个模块共用被忽略的 `subjects/FinUnityServer` 克隆，各自保持配置、运行时与用例边界；探索模块的原有代码、测试与工具未改动。本次 293 项测试与两个模块的离线链路通过，未调用模型；密钥可见性差异已获用户明确接受，详细对照与历史记录搬迁见 docs/acceptance.md。

项目跟进状态为 waiting：等待探索模块的真实模型条件、回归 R1 的人工参考结论与逐次模型调用批准，以及 R2 的单仓历史条件；不表示真实模型质量评测或后续工作已验收。此次仅做本地基线与合并提交，不推送。PROJECT_STATUS 已在本项目手工核对 frontmatter、状态、日期和 evidence 路径，未调用日常同步工具或写入 vault。

2026-10-25 命名调整 N0（历史步骤）：正式名称改为 TestPilot（测试领航员），当时采用保留旧命令的策略，构建、293 项测试、Ruff 和离线检查通过；随后该策略已被 U0 的单入口要求取代。

2026-10-25 U0：唯一入口 `uv run testpilot` 同时接入回归与探索；原有未提交的命名改动继续保留。298 项测试、Ruff、构建、单入口检查和本机离线检查通过，未调用模型或执行真实被测用例。最新图解见 docs/architecture.md，标明用例自动桥接、自动版本环境准备及金额预算守卫仍未实现。状态保持 waiting，后续真实模型、R1/R2 与资产完善仍待满足原条件。已手工核对 frontmatter、日期、状态与 11 个 evidence 文件，未使用日常同步工具。

2026-10-29：进入新需求的方案审阅阶段，跟进状态更新为 active。实施方案补齐从零发现接口、参数设计理由、统一用例修订、人工审核后执行、真实证据和完整/部分报告的任务包。本次只编制方案及同步项目文档；F1–F8 尚未开始，未修改运行代码或对新目标执行测试。同日本会话此前的 298 项测试与 Ruff 结果已写入 acceptance.md，属于旧功能基线，不是新方案验收。原段落中的 waiting 和旧检查均按历史保留；本次未核验密钥状态、未调用模型、未访问日常库或推送。交付前仅在本项目核对摘要格式、证据路径和文档一致性。

2026-10-30：用户要求启动实施，F1 首批机制验证通过，整包仍为进行中；当前边界及检查结果见 docs/acceptance.md 和 docs/task-assets.md。前文方案待审与 waiting 表述为历史记录，不代表当前状态。本次未调用日常同步工具，摘要在项目内自检。

2026-10-30 本轮续做：F1 基础验收完成，321 项完整测试通过；目标改为用户逐任务选择，可先建空草稿，不以当前未指定真实项目阻断通用开发。F2–F8 与真实效果未验收，旧记录按历史保留。详细实测及限制在 acceptance.md；未使用日常同步工具。

2026-10-30 F2 首批进展：327 项完整测试与 Ruff 通过，细节见 docs/acceptance.md。F2 仍在进行中，整个剩余功能目标未达成；未调用模型或实际项目接口。摘要在项目内自检，未访问日常同步工具。

2026-10-30 F2/F3 续做：F2 首版静态机制验证通过，F3 进入小批生成请求与审核材料阶段，详细真实命令/结果和未验收边界见 docs/acceptance.md。完整剩余功能目标未完成；仅使用合成资料及假模型，无实际模型或业务请求。未访问日常同步工具，摘要在项目内自检。

2026-10-30 F4/F5/F6 闭环机制验收：完成 F4 人工审核冻结、F5 统一执行引擎（`structured-http-v1`，包含 127.0.0.1 限制、凭据脱敏、分阶段执行、中断对账及 pytest 纯跳过识别）和 F6 报告生成与守恒覆盖统计。全量 382 项测试与 Ruff 格式/检查全通，CLI 跑通端到端合成闭环。未调用真实模型或执行被测源码，未推送或访问日常同步工具。

2026-10-30 独立复核结果见 TASKS.md 和 docs/acceptance.md；未修改运行代码。摘要在本项目自检，未使用日常同步工具。

2026-10-30 F5/F6 复核缺口修复与二次验收：完成对独立复核发现的 5 项缺陷（缺证据通过判定、max_requests 前置拦截、异常中断对账与防重复副作用恢复、递归敏感凭据脱敏、步骤证据接口覆盖统计）的修复；全量 390 项测试与 Ruff 检查/格式全通。未调用真实模型或执行被测源码，未推送或访问日常同步工具。

2026-10-30 用户确认五项修复后的补验：新增失败探针确认此前修复仍有边界遗漏，修复并通过全量 397 项与最终专项 26 项；最终两项证据错配参数化仅纳入专项，运行代码一致，未称 399 项全量通过。真实验证命令、环境、结果及旧批准/旧计数兼容边界见 TASKS.md 和 docs/acceptance.md。本次摘要维护日期为 2026-10-30；未使用日常同步工具，未重验 F7/F8、强杀与断电恢复。

本轮交付前已在本项目自检 PROJECT_STATUS：字段无重复、必填字符串/日期/状态合规，13 条 evidence 均为项目内现存非运行目录文件；未通过项为零，未调用日常同步工具。

2026-10-30 再次独立复核结果见 TASKS.md 与 docs/acceptance.md；本次未修改实现，未访问日常同步工具。

2026-10-30 三项 P2 遗漏修复：先以新增测试复现，再补清理结果引用、显式 JSON null 编码和 Markdown 接口缺口列表。最终代码全量 416 项通过；专项及复现细节、环境和未验证边界见 TASKS.md 与 docs/acceptance.md。本次摘要维护日期仍为 2026-10-30，先前待修结论按历史保留，F7/F8 未验收；未访问日常库或同步工具。

本轮摘要在本项目自检通过：frontmatter 字段无重复、必填字段/日期/状态合规，13 条 evidence 路径合规；未通过项为零，未使用日常同步工具。

2026-10-30 用户授权私有 GitHub 源码备份；推送前检查、打包及异机安装说明详见 TASKS.md 与 docs/acceptance.md。未复测全量源码测试或生产环境，未访问日常同步工具。

2026-10-30 GitHub 私有备份完成：源码提交 51bc481 已在远端 master 核验，URL 和完整提交号记录在 TASKS.md 与 docs/acceptance.md。后续文档提交仅同步上传事实，不表示已验证生产环境。已自检摘要字段及证据路径，未访问日常同步工具。

2026-11-02：为支持在另一台电脑直接下载源码并使用自定义模型实测（F7 演练），解除 `design_model.py` 中对免费模型的硬编码拦截，放开 `base_url` 允许 `http://` 接入本地 Ollama/vLLM 等模型，支持通过任意环境变量名注入 API Key，并对本地环回模型端点提供虚拟 key 适配。补充 `examples/tasks/execution.example.toml` 执行配置模板，并在 `docs/installation.md` 增加双机端到端演练指南。全量 417 项测试通过，Ruff 检查与格式化全通。用户授权直接 commit 与 push。

本轮交付前已在本项目自检 PROJECT_STATUS：frontmatter 字段无重复、必填项合规，evidence 路径均为项目内现存非运行目录文件；未通过项为零，未调用日常同步工具。

2026-11-03：完成多语言/多仓库适配架构改造与可视化测试控制台（UI）。
1. 规范全库 unparenthesized `except` 异常语法（P0）；
2. 抽取 `src/api_test_agent/adapters/` 核心层与插件注册表，收编 `PythonAstAdapter` 并保留 `tasks/fastapi_static.py` 100% 兼容（P1）；
3. 实现 `OpenAPIAdapter`（支持 OpenAPI 3.x / Swagger 2.0）（P2）；
4. 扩展 `execution_kind`、`version_manifest` 及执行器注册表 `RUNNER_REGISTRY`，支持按适配器扩展文件后缀白名单；
5. 新增 `testpilot ui` 命令，启动零依赖 Web 控制台（`api_test_agent.ui`），遵循 `baoyu-design` 工业级美学，支持数据源双模式、模型端点安全探测、测试集查看与核心 signature **Sources 追溯面板 (Sources Panel)**，透视全流程所有过程文件（`task.json`、`model.json`、`inventory.json`、`cases.json`、`execution.json`、`evidence/*.json`、`report.md`）；
6. 新增 `tests/test_adapters.py`（10 项）与 `tests/test_ui_server.py`（6 项），全量 433 项测试与 Ruff 检查/格式化全通。

本轮交付前已在本项目自检 PROJECT_STATUS：frontmatter 字段无重复、必填项合规，evidence 路径均为项目内现存非运行目录文件；未通过项为零，未调用日常同步工具。

2026-11-04：全面落实审查报告建议与五步优先顺序改进：
1. **统一主流程与 10 步流程契约**：明确以 `testpilot task` 作为全局主入口，将架构文档与说明重写对齐代码实际状态。确立 10 步统一流程契约（定义任务、固定资料、发现接口、确认测试依据、设计场景、生成用例、人工审核与冻结、环境预检、执行与收集证据、报告与维护），贯彻三大核心分界（发现接口!=理解业务规则、生成用例!=批准执行、收到响应!=业务验证通过）与两大设计原则（测试依据独立于被测实现、环境版本区分声明一致与实际验证一致）；
2. **厘定三级能力边界**：建立“字段支持 / 机制支持 / 真实验收”三级对照矩阵，明确单项目/单仓库已通过端到端小闭环真实验收；多仓库联动与多执行器扩展处于字段与机制就绪状态、未做多仓库编排真实验收；UI 控制台定位为只读配置与资产查看工作台，无浏览器端自动化执行能力且 POST execute 已被硬性封禁；
3. **修补四道执行前安全守卫**：实现环境身份守卫（严禁生产环境 `prod` 且执行环境与任务环境一致）、未知执行类型守卫（非 `http`/`structured-http-v1` 阻断为 `blocked` 并记录 `environment_issue`，拒绝伪造发包）、付费模型预算守卫（单次预估费用上限 <= 0.15 USD 拦截，回归 plan 加 `--allow-paid` 拦截）、UI 访问边界守卫（仅允许回环 Origin、防路径穿越校验、禁止 POST execute 触发真实测试）；
4. **完成真实小闭环验证**：在回环真实 HTTP 账户服务上完成从空测试集开始，经 OpenAPI 发现、带理由用例设计、导出 Markdown 审核、人工冻结批准、真实发包与跨步骤参数引用断言、凭据脱敏到守恒报告导出的端到端验证，逐条核对规则、响应体与对账指标（`tests/test_closed_loop.py` 通过）；
5. **落地 F8 资产维护与回归接入**：实现 `src/api_test_agent/tasks/bridge.py`、`testpilot task export-regression` 命令，并在 `TestCase` 中注入 `task_binding`，由回归模块 `load_catalog` 无缝加载解析（`tests/test_bridge.py` 通过）。

全量测试通过（441 passed in 46.90s，较此前 433 项净增 8 项专项测试），Ruff 检查与格式化全通（109 files already formatted）。本轮交付前已在本项目自检 PROJECT_STATUS：frontmatter 7 个必填字段无重复、日期与 active 状态合规，13 条 evidence 均为项目内现存非运行目录合法文件；未通过项为零，未调用日常同步工具。

2026-11-04 审查缺口修补与机制验收：针对审查报告指出的 5 项缺口（P1 回归桥接未接通执行、P1 预算检查仅查配置值、P1 UI 缺少服务端授权与 CSRF 拦截、P2 环境守卫缺少运行版本探针校验、P2 表述收窄为合成服务集成测试）进行深入完善与机制验收：
1. **F8 回归桥接执行接通与数据保护**：`bridge.py` 导出 entry 改为离散 token 命令参数列表，并自动追加 `--case-id <case_id>` 限制单用例执行粒度（避免整批重复执行）；读取已有 `cases.yaml` 解析失败时抛出 `ValueError` 拒绝覆盖，保护已有资产；`runner.py` 注册 `task_command` 适配器并在 `run_plan` 中派发执行与解析结果（`tests/test_bridge.py` 扩充至 3 项通过）；
2. **商业付费模型最坏情况费用预留**：`BudgetSettings` 扩展 Token 单价配置，`design_model.py` 内置基准价格表；在发包前根据输入字符估算 prompt token 上界与模型最大输出 token 计算 `worst_case_cost`；若超额直接在发包前抛出异常拦截；对定价未知的外部商业模型，若未显式配置输入/输出单价直接拒绝调用；
3. **UI 服务端 Origin 强校验与会话凭据授权**：`ui/server.py` 服务端生成高熵随机会话令牌 `auth_token`，挂载 GET `/api/auth/token` 并在页面注入；校验 Host 为回环地址，Origin 端口严格匹配服务端端口，阻断 `Sec-Fetch-Site: cross-site` 恶意跨站发包；所有修改类 POST 请求强制核验 `X-TestPilot-Token` 请求头，缺失或不符返回 403 Forbidden（`tests/test_ui_server.py` 扩充至 10 项通过）；
4. **运行环境版本探针核验**：`ExecutionConfig` 扩展 `version_endpoint` 与 `require_version_verification`；`tasks/execution.py` 发包前向探针端点发起 HTTP 请求核对真实运行服务 Git 提交哈希是否与 `target_commit` 一致；若不符且配置了核验要求直接抛出 `ValueError`；执行产物独立记录版本校验状态，报告呈现告警横幅（`tests/test_pre_guards.py` 扩充至 9 项通过）；
5. **表述精确收窄为合成服务集成测试**：将此前“单项目/单仓库真实验收”表述收窄为“合成服务端到端小闭环验证（Synthetic Service Integration Verification）”，在文档中明确注明：验证了资料提取、导入设计、审核机制、探针预检、HTTP 执行与报告对账的机制链路，未经验收真实模型生成质量、未验证复杂生产业务、未验证真人实际审核效果。

全量测试通过（450 passed in 54.33s，较此前 441 项净增 9 项专项测试），Ruff 检查与格式化全通（109 files already formatted）。本轮交付前已在本项目自检 PROJECT_STATUS：frontmatter 7 个必填字段无重复、日期与 active 状态合规，13 条 evidence 均为项目内现存非运行目录合法文件；未通过项为零，未调用日常同步工具。

2026-11-04 接口发现合并伪冲突修复（评审第 6 项，应优先修复的实现问题）：修复 `tasks/discovery.py` 两处发现/合并质量缺陷。
1. **相同声明不再误判冲突（6a）**：同一 `method path` 合并改为按声明签名集判别，仅存在多个不同签名时才置 `conflict`；完全相同的重复声明保留底层状态并追加一致声明数量，去掉“完全相同却报冲突”的伪阳性；描述不同的文档声明仍判冲突，等价保持既有冲突/作用域测试预期。
2. **显式 openapi 适配器不再双重解析（6b）**：通用文档循环对已由所选适配器等扩展名解析过的文档跳过，避免同一 `.json/.yaml/.yml` 被适配器解析一次、又由通用 `openapi()` 再解析一次造成的重复声明与假冲突；
3. **副产物修正**：`has_code_routes`/`has_document_routes` 改按行 `kind` 严格判定路由来源，避免 openapi 文档适配器产出的 requirement 行被误当代码路由而触发 `only_in_document` 降级。
全量 452 项测试通过（较此前 450 项净增 2 项，见 `tests/test_task_discovery.py`），Ruff 检查与格式化全通。本项推进 F2 接口发现合并质量，真实项目逐项接口对账仍属 F7；未调用模型，未访问被测接口。本轮交付前已在本项目自检 PROJECT_STATUS：frontmatter 7 个必填字段无重复、日期与 active 状态合规，13 条 evidence 均为项目内现存非运行目录合法文件；未通过项为零，未调用日常同步工具。

2026-11-04 接口采集评审修补（第 4/5/7 项，应修复实现问题）：续第 6 项之后，补齐 OpenAPI 解析、版本关联与请求模型展开。
1. **OpenAPI 跨文件引用、服务器与参数覆盖（第 5 项）**：`adapters/openapi.py` 把 `$ref` 解析从仅文档内扩展为可跨文件 `_walk_external_refs` 递归合并；服务器地址在 root/path/operation 三级取首个非空并回退 `basePath`；路径与操作级参数按 `(in, name)` 同名单覆盖。外部引用未解析保持 `unresolved` 并告警，不伪报 `confirmed`。
2. **文档与源码版本关联（第 7 项）**：`tasks/discovery.py` 对源码路径盖固定提交、对外部文档本地文件盖 `unverified-document` 占位；`all_materials_version_verified` 仅在无外部文档时为真，避免旧文档对新源码伪报已验证，并给出 `MIXED_VERSION_WARNING`。`models.py` 的 `SourceRef.version` 保持非空。
3. **快照集内 Pydantic 请求模型展开（第 4 项）**：`adapters/python_ast.py` 把注解解析到集内 `BaseModel`/`Model` 类的路由参数展开为 `request_body`（字段名/类型/必需项/字面量默认值，动态默认仅记录不评估）；集外不可静态见到的模型保持不展开，认证仍如实标注 `unresolved: dependency semantics not evaluated`。
全量 458 项测试通过（较第 6 项后的 452 项净增 6 项，见 `tests/test_adapters.py` 两项新增与保留的 `test_task_discovery.py` 版本关联测试），Ruff 检查与格式化全通（86 files already formatted）。本项推进 F2 接口发现与版本关联质量，真实项目逐项接口对账、认证语义解析与集外模型展开仍属 F7 及后续边界；未调用模型，未访问被测接口。本轮交付前已在本项目自检 PROJECT_STATUS：frontmatter 7 个必填字段无重复、日期与 active 状态合规，13 条 evidence 均为项目内现存非运行目录合法文件；未通过项为零，未调用日常同步工具。

2026-11-04 续审：第 4/5/6/7 项当前证据与历史记录纠错见 TASKS.md 和 docs/acceptance.md 同日续审节；未修改第 1/2/3/8 项能力范围。原 458 项通过不证明当时已支持跨文件引用。本次未复测真实模型与被测服务。
