# TestPilot · 任务清单

状态：已完成 / 进行中 / 待开始 / 阻塞。验收结果、环境和未验证项在 [docs/acceptance.md](docs/acceptance.md)。范围、批准和预算在 [docs/startup-record.md](docs/startup-record.md)。来源方案是日常库 `output/20261013-是否自建Agent与测试Agent搭建方案.md`（本机资料），这里按用户 2026-10-15 的决定收窄为只做到 A3。

2026-10-25 用户决定把 testagent（来源方案：日常库 `output/20261022-测试Agent最小可行落地方案.md`）并入为回归模块，合并方案见日常库 `output/20261025-两个测试Agent合并方案.md` 与 `output/20261025-两个测试Agent合并-Codex执行方案.md`（本机资料）。

2026-10-29：用户补充目标为从零梳理接口、生成带测试依据与参数理由的用例，人工查看并确认后才执行，输出完整或部分完成报告，并统一维护人工与 AI 用例。本次只编制[实施方案](docs/implementation-plan-20261029.md)，供用户审阅；下表历史功能状态不变，新增 F1–F8 尚未开始。

| ID | 工作包 | 验收条件 | 状态 |
| --- | --- | --- | --- |
| A0 | 范围与启动参数 | 契约可判定；范围与费用明确；无真实数据、凭据进入共享文件 | 已完成（2026-10-15）；10 条业主确认项已由用户确认；模型已指定为 `openrouter/free`；key 与每周小时数待补 |
| A1 | 沙箱 | 两次重置结果一致；A、B 账号可用；外部依赖受控；原仓库与种子不变 | 已完成（2026-10-15） |
| G0 | A1 之后的检查 | 隔离、规则、重置达标，否则暂停 | 通过（2026-10-15）；没有 OS 级断网，只有一次连接快照 |
| A2 | 小基准：5 个缺陷 + 5 个无缺陷任务 | 每个缺陷可独立触发；参考构建满足规则；没有给 Agent 的泄题材料 | 已完成（2026-10-15）：判定套件 18 条规则、5 个缺陷、9 个灵敏度变异体、10 个任务，见 [benchmarks/dev/README.md](benchmarks/dev/README.md) |
| A3 | 最小 Agent：四个工具、单模型 | 10 个开发任务都有明确结束状态；预算有效；至少一条有效用例可在两个版本上确定性重放 | **进行中**：不需要 key 的部分已完成并验收（模型适配器、预算、四个工具、用例语言与重放、Agent 循环、CLI；245 项测试；5 个见证用例在 6 个构建上的重放矩阵；脚本化端到端冒烟）。**真实模型还没有运行过：缺 API key** |
| G1 | A3 之后的检查 | 没有任何可重放的有效用例就先修规则、工具、协议，不扩大实验、不迁框架 | 待开始 |
| R0 | 并入 testagent 作为回归模块 | 293 项测试与 ruff 两项通过；探索模块的 doctor、sandbox verify/check、witnesses、scripted_run 与 2026-10-15 的记录一致；回归模块的 check、plan --stub、run 和购物车演示通过；不调用模型 | 已完成（2026-10-25）：上述检查全部通过；check 显示 DEEPSEEK_API_KEY 未设置，与方案预期不同，用户当日明确接受差异并批准继续离线合并；未调用任何模型。详见 docs/acceptance.md |
| R1 | 历史改动评测 | 在 `subjects/FinUnityServer` 的历史里选 5–10 个版本对，由用户给出人工参考结论，记录漏项与误选 | 待开始：需要用户提供参考结论，并逐次批准真实 DeepSeek 调用（每次 ≤ ¥1，R 系列合计 ≤ ¥30） |
| R2 | 接入单仓 `server/` | 回归模块支持只看 `server/` 的路径范围，单仓登记为第二个被测对象 | 待开始：等 FinUnity 单仓里 `server/` 有了导入后的真实提交 |
| R3 | 用例目录补到 10–20 条 | 原 testagent 下一步第 2 项；可以用探索模块产出的可重放用例 | 待开始 |
| N0 | 正式命名 TestPilot（测试领航员） | 安装包名与新主命令可用，两个旧入口兼容；构建、现有测试和离线配置检查通过 | 已完成（2026-10-25）：testpilot 0.1.0 构建与安装通过，三个命令入口可用；293 项测试、Ruff、doctor、sandbox verify、回归 check 与 plan --stub 通过；未调用模型。详见 docs/acceptance.md |
| U0 | 统一入口与最新架构/流程图 | 只安装 testpilot；根命令分发探索与回归，配置隔离和执行守卫保留；提供按当前代码绘制的整体图与失败分支说明 | 已完成（2026-10-25）：298 项测试、Ruff、单入口打包检查、doctor、verify、回归 check/plan --stub/run --dry-run 通过；PNG/SVG 已目视核对；最新设计见 docs/architecture.md。U0 取代 N0 当时的旧命令兼容策略，未调用模型 |

## 从零建设接口测试集（2026-10-30 已启动实施）

| ID | 工作包 | 验收重点 | 状态 |
| --- | --- | --- | --- |
| F0 | 编制可落地的任务方案 | 目标流程、统一用例、人工审核、执行证据、报告、工作包依赖和真实验收口径可供审阅 | 方案编制完成；用户 2026-10-30 要求按方案开始执行，功能仍按包验收 |
| F1 | 统一资产与状态模型 | 空测试集可创建任务；人工与 AI 用例同目录；来源、参数理由、修订和结果边界明确 | 已完成 F1 基础验收（2026-10-30）：空草稿与每任务目标配置、来源/接口/规则/修订/审核/结果模型、状态历史与拒绝路径、修订级审核存储及旧资产适配边界；321 项测试与合成命令演示通过。批准执行仍属 F4–F6，自动旧资产迁移属 F8。见 docs/task-assets.md |
| F2 | 指定版本、接口发现与规则来源 | 全范围接口逐项对账，冲突/未解析/不支持项不丢失；采集与实际模型输入一致 | 首版机制验收通过（2026-10-30）：固定快照、跨文件 FastAPI 挂载图、OpenAPI 本地引用、声明与差异、失败部分资料及重入保留；动态/外部项明确待确认，不宣称静态清单证明运行系统全集。真实项目逐项验收仍在 F7 |
| F3 | 场景与用例生成、审核材料 | 从零生成正常/异常/边界等适用用例，每个参数有理由和依据；不执行未审核业务请求 | 机制验收通过（2026-10-30）：小批请求、生成响应校验、七类场景矩阵、参数理由表及审核材料已接入；免费模型适配以假供应商验证，未真实调用。预算片段对账与中断恢复均验证通过 |
| F4 | 人工审核与冻结清单 | 未批准、批准后改内容或换版本均拒绝执行；新增用例重新审核 | 机制验收通过（2026-10-30）：人工决定、部分批准、完整快照冻结与执行前复核已全部接通；未批准或篡改后执行直接拒绝，HTTP 请求数为零 |
| F5 | 统一执行、证据与中断对账 | 执行真实发生，失败/异常/阻塞分开；不盲目重试副作用；pytest 跳过不计通过 | 已完成修复与二次验收（2026-10-30）：max_requests 调用前拦截并跨恢复累计、参数引用与异常/KeyboardInterrupt 收尾对账；已尝试的中断请求记 error 且不重发，敏感值含短令牌/字典键递归脱敏；本次补验清理链引用与显式 JSON null |
| F6 | 报告与覆盖对账 | 成功、失败和未跑完都可查看；统计来自执行记录；接口与场景缺口完整列出 | 已完成修复与二次验收（2026-10-30）：缺失/损坏/错配证据不计通过且对账 FAIL；接口覆盖要求收到响应，场景覆盖绑定本用例逐步证据；Markdown 同步逐项展示接口缺口 |
| F7 | 真实项目与真实模型验收 | 从空测试集开始，小批人工审核后执行，再覆盖全清单；证据可抽查与重放 | 进行中（2026-11-02 进入双机联调与异机实测准备，通用模型适配与模板已完成，等待测试机端到端运行） |
| F8 | AI 维护与回归接入 | 同一资产目录复用，修改有修订并重新审核，导出至 cases.yaml 与 task_binding 绑定，回归 load_catalog 兼容解析 | 基础机制验收通过（2026-11-04）：实现 export_approved_to_regression_catalog、testpilot task export-regression CLI、TestCase.task_binding 及回归解析兼容，测试 tests/test_bridge.py 通过 |

每包交付物、拒绝路径、状态口径及验收方法见实施方案第 10–12 节。2026-10-30 用户已要求开始实施；先推进 F1，后续按依赖与目标授权执行，不把旧开发基准或已有 pytest 作为“零用例生成”的模型输入。费用优化暂缓排期，本次不调用模型，不移除现有限制。

A3 之后的原选项（历史规划，G1 时再定，不是承诺）：A4 可信评测、B1 LangGraph 与恢复、工单 Agent 第一步。新方案待审，不据此自动改写原基准的黑盒边界。

## 下一步需要用户做的事

F1 基础验收已完成，下一步开发 F2 通用适配，不等待用户现在选择真实目标。项目、版本、资料读取范围由用户在具体任务使用时选择，环境在执行前落实；以下保留原 A3 待办，不表示本次要立即运行。

1. 把 OpenRouter 的 API key 设成用户环境变量 `API_TEST_AGENT_MODEL_API_KEY`（不要发进对话或文件）。设好之后运行 `uv run testpilot model status` 看账号属于每天 50 次还是 1000 次的档，再运行 `testpilot model ping` 做两次请求的工具调用检查。
2. 说明每周可投入的小时数，用来排期。
3. 看 [docs/rules-evidence.md](docs/rules-evidence.md) 末尾的 D-1 到 D-6，判断哪些是衡仓自己的缺陷或文档问题（不影响本项目进度）。

## 接下来我做的事（key 到位后）

`model ping` 通过后，用 `agent run --task dev-02 --replay` 跑一个任务，按真实输出调提示词和工具说明，再跑十个任务；日额度不够时分几天跑，每次运行前会按剩余额度检查。


## 2026-10-30 独立复核：F5/F6 仍有待修缺口

本次仅审查，未修改实现；未调用模型或真实业务接口。Windows / Python 3.14，重新运行全量 pytest（禁用缓存、系统临时目录中的全新 UUID basetemp）：382 passed in 46.19s；ruff check 与 format --check 通过（96 files）。首次测试因指定临时目录的父目录不存在而产生 setup errors，修正临时路径后完整重跑通过。

额外合成探针复用 approved_setup，经 MockTransport 执行两条用例后，仅删除该探针临时目录内的证据文件，再生成报告：planned=2、passed=2、conserved=true、evidence_backed_count=0。确认缺证据仍计通过，且未输出缺证据明细，违反方案第 9/12 节。

代码复核还发现以下未覆盖路径（静态确认，尚未逐项动态复现）：
- F5 execution.py：max_requests 仅在配置中声明，执行路径无请求计数和调用前检查，批准清单可突破该上限。
- F5 execution.py：参数引用解析在异常捕获外；外层 finally 只关闭客户端。引用缺失或 KeyboardInterrupt 会跳过用例记录、剩余 not_run 和最终汇总，任务留在 running；再次 execute 接受 running/incomplete 并从清单首项重跑，可能重复副作用。
- F5 execution.py：仅脱敏固定头名称，响应体、引用后的请求参数及断言实际值直接持久化；回显宿主令牌时会泄露，需统一按敏感值脱敏并保留内存计算数据。
- F6 report.py：按用例 outcome 将全部 operation_ids 计入实际调用覆盖，准备失败或网络异常时也计入未调用接口；应从逐步证据计算。

结论：已有测试全绿仅证明已覆盖路径；F5/F6 首版验收需补以上拒绝/失败/缺证据场景并修复后重验。F7 暂不宜进入真实执行。本次未重验 F7/F8、真实模型质量或真实目标环境。


## 2026-10-30 复核缺口修复与二次验收（全部通过）

针对独立复核发现的 5 项缺陷，已完成代码修复与自动化验证，未调用真实模型或被测接口。测试使用全新 basetemp，全量回归与新增测试全部通过（390 passed in 50.28s，新增 8 项针对性测试覆盖）：

1. **缺证据仍计通过修复（P1）**：`report.py` 对落盘证据逐项核验；缺失或被删除证据的记录不再计入 `passed`/`failed`/`error`，记录到 `missing_evidence_count` 与 `missing_evidence_ids`，守恒判定直接为 `FAIL (不平衡/证据缺失)`；报告顶部渲染警示横幅与缺失 ID 清单，用例列表标注 `[证据缺失]`。
2. **异常中断可靠收尾与防重复副作用（P1）**：`execute_step` 包装参数解析异常并记为步骤错误与证据，不泄漏至外层；执行循环捕获 `KeyboardInterrupt` 与意外异常，安全记录当前用例与剩余 `not_run`，`finally` 块确保计算完整对账、落盘 `execution.json` 并转移任务状态至 `incomplete`。再次执行 `incomplete` 任务时，自动复用已有终态用例，仅执行 `not_run` 用例，避免重复执行有副作用的请求；`running` 状态拒绝并发重复执行。
3. **敏感凭据全链路递归脱敏（P1）**：新增 `redact_sensitive` 函数，递归检查字符串、字典、列表与元组；宿主注入的真实令牌值在所有落盘位置（URL、请求参数/体、响应头/体、错误信息、Issue、事件日志、对账与报告）均严格替换为 `[REDACTED]`，同时在内存 `step_output` 保留未脱敏数据供下游参数引用与断言使用。
4. **请求上限调用前检查（P1）**：引入 `RequestCounter`，在每个 HTTP 调用前执行计数检查；超过 `max_requests` 时立即终止并阻止后续外发，将剩余用例置为 `not_run`，任务置为 `incomplete`。
5. **接口与场景覆盖率虚高修正（P2）**：`report.py` 不再按用例 outcome 粗暴添加全量 `c.operation_ids`，改为严格根据实际产生步骤证据的调用统计 `called_operations`；准备步骤失败后未调用的接口准确保留在 `uncovered_operations` 中。


## 2026-10-30 用户确认五项修复：补充边界复现与验收

保留工作区已有修复，本轮先新增探针复现遗漏：首请求中断被记 not_run（恢复可能重发）、恢复清零请求预算、字典键漏脱敏、连接失败虚增覆盖、损坏证据使报告崩溃，首次专项为 5 failed / 17 passed。随后修复并补充短凭据回显到后续引用、断言实际值、报告及跨用例场景覆盖的验证。中断的已尝试请求记 error，剩余未开始项才为 not_run；恢复保留结果/证据/Issue 和累计请求数，未知历史计数拒绝自动恢复。

本轮全量实测 397 passed in 61.13s；随后扩展证据错配参数化测试，专项 26 passed in 10.96s；Ruff 检查与格式通过。命令、环境和兼容边界见 docs/acceptance.md 最新记录。F7/F8 未验收，未调用真实模型或被测接口；旧“全部已知缺口修复”表述仅代表当时的测试范围，本次扩充边界以最新验收为准。


## 2026-10-30：修复后再次独立复核

本轮仅复核，未修改实现。Windows / Python 3.14，使用系统临时目录中全新 UUID basetemp、禁用 pytest 缓存：399 passed in 74.25s；Ruff check 与 format --check 通过（96 files）。上轮五项修复已有实现与测试，但本次新增合成 MockTransport 探针发现：

- P2 清理步骤结果未写入 results：两个用例各有 test、clean1、clean2，clean2 引用 clean1.body.id；审批通过后共应调用六次，实际四次，两个 clean2 均报 step 'clean1' has not run，主体仍计 passed=2。execution.py 清理分支需像其他阶段一样保存结果，补清理链测试。
- P2 显式 JSON null 被当作无请求体：execute_step 对 POST 的 $ / literal / None 参数实际发出 b''，不是 JSON null；当前 body_payload is not None 条件丢失了用户审核的 null 输入。需独立保留请求体是否提供的标记并正确编码 null，补 null 与 omitted 的区别测试。
- P2 Markdown 报告未呈现接口缺口：report.py 计算 uncovered_operations 并写入 JSON，但 Markdown 第 7 节只渲染 limitations；已确认但未调用的接口可能只看到“无特殊运行限制记录”，缺少方案要求的逐接口未覆盖清单（静态路径确认）。

上述两项执行问题已动态复现；报告展示问题经代码核对。本轮无真实模型或真实目标请求。F5/F6 仍需补这三项后验收，F7/F8 未验证。


## 2026-10-30：三项 P2 遗漏修复与补验

已补齐清理分支 results 写回、POST/PUT/PATCH 显式 JSON null 与省略体区分、Markdown 未覆盖接口清单。先补测试复现（11 failed / 32 passed），再修实现；主体通过/失败两种清理链均实际发出六次请求，literal/reference null 均发送 b'null'，omitted/absent 仍为空，具名 null 字段保留。报告未执行/全部调用/准备失败部分调用均核对 JSON 与 Markdown 缺口一致。

最终代码全量 416 passed in 97.89s，Ruff 检查和格式检查通过；专项 43 passed in 16.93s（增加 body_provided 证据标记之前），新增 17 项测试。上节 399 项与三项待修结论作为历史保留，具体命令和环境见 docs/acceptance.md 最新记录。F7/F8 仍未验收，无真实模型或被测请求，未提交/推送。


## 2026-10-30：GitHub 私有备份与异机安装准备

用户明确授权 commit + push，先准备源码备份再推进 F7。已新建私有仓库 loonslo/TestPilot；候选 136 个文件及 195 个历史 blob 的凭据模式和可见环境密钥值扫描无发现（不是凭据不存在的完整证明），本机配置/运行目录/目标源码不纳入候选。uv build 成功，独立 Python 3.14 wheel 安装按 uv.lock 导出依赖锁定，根目录外三个 CLI 入口检查通过；新增 docs/installation.md。当前记录为推送前准备，实际推送结果待远端提交核验后补记。416 项源码测试结果沿用本轮已执行记录，未冒充用户独立复核；本次未重复全量测试或执行真实目标。


2026-10-30 备份结果：源码提交 `51bc48101d5fb43e6f5765b1d2949f005b84cb0e` 已推送至私有 https://github.com/loonslo/TestPilot 的 master；git ls-remote 返回同一提交，GitHub 元数据核对默认分支 master、visibility=PRIVATE。最终 137 个暂存路径扫描通过，runtime/subjects/本机配置未入库。Ruff 最终检查通过，97 files already formatted；git diff --cached --check 通过（新 SVG 仅去除行末空白）。安装文档为 docs/installation.md。本次上传不改变 F7/F8 未验收的状态，416 项仍为本轮代理执行的测试记录，不是用户独立复核结论。


## 2026-11-02：F7 实测准备——通用模型适配与双机测试模板

解除单一模型硬编码限制，支持测试机本地配置任意 OpenAI 兼容模型（DeepSeek、OpenAI、本地 Ollama 等），并补充端到端测试配置模板供异机实测。
- `settings.py` 放宽 `base_url` 支持 `http://`；
- `design_model.py` 移除强制 `openrouter/free` 与硬编码费用限制，动态读取配置中的 `api_key_env`，本地端点自动提供虚拟 key；
- 补齐 `execution.example.toml`，更新 `model.example.toml` 与 `installation.md`；
- 全量测试 417 passed in 40.93s，Ruff 检查与格式化全通。用户授权直接 commit 并 push 至远端 master。

## 2026-11-03：多语言与多仓库适配架构落地（P0/P1/P2 完成）与可视化测试控制台（UI）

针对混合技术栈项目（如多 Maven 仓库、多 Node 前端仓库）与现有硬编码假设不匹配的问题，完成基础架构解耦与缺陷修复，并提供无状态、过程文件全景透视的交互式 Web 控制台：
- **P0 缺陷修复**：全库扫描并规范异常捕获语法，消除无括号异常元组；
- **P1 适配器层抽取**：新增 `src/api_test_agent/adapters/` 模块与注册表，将 FastAPI 静态提取收编为 `PythonAstAdapter`，并在 `tasks/fastapi_static.py` 保留向下兼容重导出；
- **P2 OpenAPI 通用适配**：实现 `OpenAPIAdapter`，支持 OpenAPI 3.x 与 Swagger 2.0 规格的统一提取，无缝承接带 springfox/knife4j 的各类后端服务（如 Java Spring Boot / Egg.js）；
- **执行器与版本清单扩展**：`CaseRevision.execution_kind` 放宽为可扩展字符串（允许 `junit` / `http` 等，保留 `pytest` 守卫）；`Task` 扩展 `version_manifest`（支持多仓库独立 SHA）与 `adapter` 指定；`TaskStore.configure` 支持配置新字段；`regression/runner.py` 引入 `RUNNER_REGISTRY` 支持执行器插拔；`discovery.permitted()` 动态支持各适配器声明的文件后缀；
- **可视化控制台与过程透视工作台**：
  - 新增命令 `uv run testpilot ui`（支持 `--port`、`--host`、`--root`、`--no-open`），内置零依赖 `ThreadingHTTPServer`；
  - 遵循 `baoyu-design` 工业级美学（清晰设计 Token、深浅色模式、严禁 AI 泛滥紫灰噪点）；
  - 落地三大配置板块（代码库/接口文档双模式数据源、模型与预算配置及安全端点探活、用例集与全过程对账）；
  - 核心特色 **Sources 追溯面板 (Sources Panel)**：全流程透明透视任意环节的过程资产（`task.json`、`model.json`、`inventory.json`、`cases.json`、`active-execution.json`、`evidence/*.json`、`report.md`）；
- 全量测试 433 passed in 40.54s（新增 `tests/test_adapters.py` 10 项与 `tests/test_ui_server.py` 6 项，合计 16 项新测试），Ruff 检查与格式化全通（105 files）。


## 2026-11-04：落实五步优先顺序改进、真实小闭环与回归资产复用 (F8 机制验收)

根据“下一步的优先顺序”建议，全面完成主流程统一、能力边界厘定、执行前守卫修补、真实小闭环验证与 F8 回归资产桥接：

1. **统一主流程与术语（建议 1）**：
   - 确立 `testpilot task` 为主要端到端自动化测试入口，贯穿 8 阶段标准生命周期（Init、Discover、Design、Review、Approve、Execute、Report、Bridge）；
   - 在 [docs/architecture.md](docs/architecture.md) 与 [README.md](README.md) 中完整定义生命周期表，详细记录每一步的输入、输出、角色分工 (Role)、拒绝条件 (Rejection Conditions) 与恢复方式 (Recovery Mechanisms)。
2. **能力边界三级对照表（建议 2）**：
   - 在 [docs/architecture.md](docs/architecture.md) 建立“字段支持 / 机制支持 / 真实验收”三级对照矩阵：
     - **多仓库联动 (Multi-Repo)**：数据模型与配置已支持（字段支持/机制支持），但跨仓库依赖与多服务编排**未做真实验收**；
     - **多执行器 (Multi-Runner)**：`RUNNER_REGISTRY` 具备注册与派发机制，但除内置 pytest 与结构化 loopback HTTP 之外，外部执行器**未做真实验收**；
     - **UI 操作 (Web Console)**：提供只读资产透视与状态探活（机制支持），但**无浏览器驱动/UI 自动化执行能力，未做 UI 自动化真实验收**，POST /execute 已被硬性封禁。
3. **修补执行前安全守卫（建议 3）**：
   - **环境身份守卫**：`approval.py` (`ExecutionConfig.environment`) 与 `execution.py` (`execute_task`) 禁止 `prod`/`production`/`online` 等生产关键字，且执行环境必须与任务环境严格一致；
   - **未知执行类型守卫**：`execution.py` 严格校验 `case.execution_kind in {"http", "structured-http-v1"}`，遇未知执行类型直接标记为 `blocked` 并记录 `environment_issue`，拒绝伪造 HTTP 发包；
   - **商业模型预算守卫**：`design_model.py` 在发起真实商业模型调用前检查 `max_cost_usd` 且上限 <= 0.15 USD（<= 1 元人民币）；回归 `plan` 未加 `--stub` 或 `--no-ai` 时必须显式携带 `--allow-paid`，否则在发起调用前拦截；
   - **UI 访问边界守卫**：`ui/server.py` 移除通配符 CORS，仅允许回环 Origin；校验 `task_id` 阻断路径穿越；POST `/api/tasks/{task_id}/execute` 明确返回 403 Forbidden，禁止网页触发测试执行；
   - 新增针对性测试 [tests/test_pre_guards.py](tests/test_pre_guards.py)（6 项测试全部通过）。
4. **真实小闭环验证（建议 4）**：
   - 新增端到端真实测试 [tests/test_closed_loop.py](tests/test_closed_loop.py)，针对运行在 `127.0.0.1` 的真实 HTTP 账户服务（`POST /api/v1/accounts` 与 `GET /api/v1/accounts/{account_id}`）；
   - 从空用例集开始，经 OpenAPI 发现、带理由用例设计、导出 Markdown 审核、冻结批准、真实 HTTP 发包与参数引用提取断言、敏感凭据自动脱敏到 Markdown/JSON 守恒报告导出；
   - 逐条核对输入参数、状态码、响应体、断言判定与报告中的覆盖率指标，验证通过。
5. **资产维护与回归接入（建议 5 / F8 机制验收）**：
   - 新建 `src/api_test_agent/tasks/bridge.py`，实现 `export_approved_to_regression_catalog` 函数；
   - CLI 新增子命令 `testpilot task export-regression <task_id> [--catalog PATH] [--approval APPROVAL_ID]`；
   - 在 `regression/models.py` 的 `TestCase` 模型中扩展 `task_binding` 字段，使回归模块 `load_catalog` 能够完整解析统一任务导出的用例；
   - 新增针对性测试 [tests/test_bridge.py](tests/test_bridge.py)，通过验证。
## 2026-11-04 审查缺口修补：F8 回归桥接执行接通、调用前预算预留、UI 服务端授权与版本探针

针对审查报告指出的 5 项缺口（P1 回归桥接未接通执行、P1 预算检查仅查配置值、P1 UI 缺少服务端授权与 CSRF 拦截、P2 环境守卫缺少运行版本探针校验、P2 表述收窄为合成服务集成测试），完成深入修复与机制验证：

1. **P1 F8 回归桥接执行链路与目录保护**：
   - [src/api_test_agent/tasks/bridge.py](file:///D:/workspace/TestPilot/src/api_test_agent/tasks/bridge.py)：导出 entry 改为离散 token 命令参数列表，并自动追加 `--case-id <case_id>` 限制单用例执行粒度（避免整批重复执行）；当 store 根目录非默认时自动传递 `--root`；读取已有 `cases.yaml` 解析失败时抛出 `ValueError`，杜绝覆盖损坏已有资产；
   - [src/api_test_agent/tasks/execution.py](file:///D:/workspace/TestPilot/src/api_test_agent/tasks/execution.py) 与 [src/api_test_agent/tasks/cli.py](file:///D:/workspace/TestPilot/src/api_test_agent/tasks/cli.py)：`execute_task` 与 CLI `execute` 新增 `--case-id` 参数，支持过滤限定单用例执行，并在执行完成时标准输出 `{passed} passed, {failed} failed`，返回非零退出码；
   - [src/api_test_agent/regression/runner.py](file:///D:/workspace/TestPilot/src/api_test_agent/regression/runner.py)：新增 `task_command` 并在 `RUNNER_REGISTRY["task"]` 注册；`run_plan` 自动识别 `task_binding` 或 `task execute` 用例并派发执行；`classify` 准确解析统一任务执行器的输出与退出码；
   - [tests/test_bridge.py](file:///D:/workspace/TestPilot/tests/test_bridge.py)：补充导出单用例参数、损坏 YAML 拒绝覆盖保护、通过 `run_plan` 执行被导出的统一任务用例 3 项测试。

2. **P1 商业付费模型最坏情况费用预留**：
   - [src/api_test_agent/settings.py](file:///D:/workspace/TestPilot/src/api_test_agent/settings.py)：`BudgetSettings` 扩展 Token 单价配置（`cost_per_million_input_tokens` 与 `cost_per_million_output_tokens`，以及 `is_free` 显式标识）；
   - [src/api_test_agent/tasks/design_model.py](file:///D:/workspace/TestPilot/src/api_test_agent/tasks/design_model.py)：内置基准商业模型价格表；在发起网络发包前，根据输入字符估算 prompt token 上界与模型最大输出 token 计算 `worst_case_cost`；若 `worst_case_cost > max_cost_usd` 直接在发包前抛出异常拦截；对定价未知的外部商业模型，若未显式配置输入/输出单价，直接拒绝调用；响应后若供应商未返回费用，按实际 token 与单价自动对账。

3. **P1 UI 服务端 Origin 强校验与会话凭据授权**：
   - [src/api_test_agent/ui/server.py](file:///D:/workspace/TestPilot/src/api_test_agent/ui/server.py)：服务端启动时由 `make_server` 生成高熵随机会话令牌 `auth_token`，挂载 GET `/api/auth/token` 并在页面加载时注入 `<meta>` 与 `window.TESTPILOT_TOKEN`；
   - 服务端校验 Host 为回环地址，Origin 端口严格匹配服务端端口，阻断 `Sec-Fetch-Site: cross-site` 恶意跨站发包；所有修改类 POST 请求强制核验 `X-TestPilot-Token` 请求头，缺失或不符直接返回 403 Forbidden；
   - [tests/test_ui_server.py](file:///D:/workspace/TestPilot/tests/test_ui_server.py)：扩充至 10 项测试（覆盖跨端口拦截、跨站 Origin 拦截、无 Token 拦截、Sec-Fetch-Site 拦截及正常生命周期操作）。

4. **P2 运行环境版本探针核验**：
   - [src/api_test_agent/tasks/approval.py](file:///D:/workspace/TestPilot/src/api_test_agent/tasks/approval.py)：`ExecutionConfig` 扩展 `version_endpoint` 与 `require_version_verification`；
   - [src/api_test_agent/tasks/execution.py](file:///D:/workspace/TestPilot/src/api_test_agent/tasks/execution.py)：执行前向探针端点发起 HTTP 请求获取运行服务的实际提交哈希，核对是否与 `target_commit` 一致；若不符且配置了 `require_version_verification=True` 直接抛出 `ValueError`；执行产物独立记录 `declared_commit`、`actual_commit` 与 `version_verified`；
   - [src/api_test_agent/tasks/report.py](file:///D:/workspace/TestPilot/src/api_test_agent/tasks/report.py)：报告第 2 节呈现声明版本与实测运行版本，未核验时显式渲染 GitHub-style Alert 警告横幅；
   - [tests/test_pre_guards.py](file:///D:/workspace/TestPilot/tests/test_pre_guards.py)：扩充至 9 项测试。

5. **P2 表述收窄为合成服务集成测试**：
   - 在 [README.md](file:///D:/workspace/TestPilot/README.md)、[docs/architecture.md](file:///D:/workspace/TestPilot/docs/architecture.md) 与 [docs/task-assets.md](file:///D:/workspace/TestPilot/docs/task-assets.md) 中，将此前“单项目/单仓库真实验收”表述收窄为“合成服务端到端小闭环验证（Synthetic Service Integration Verification）”，在文档中明确注明：验证了资料提取、导入设计、审核机制、探针预检、HTTP 执行与报告对账的机制链路，未经验收真实模型生成质量、未验证复杂生产业务、未验证真人实际审核效果。

**实测结果**：全量测试 `uv run python -m pytest -q -p no:cacheprovider --basetemp="D:\workspace\TestPilot\runtime\temp_all_p1"`：**450 passed in 54.33s**（较此前 441 项净增 9 项）；Ruff 检查与格式化全通（109 files already formatted）。

## 2026-11-04：接口发现合并伪冲突修复（评审第 6 项）

针对接口采集评审表中的第 6 项实现问题（合并逻辑的伪阳性冲突、显式 openapi 适配器双重解析导致重复声明与假冲突），已完成代码修复与回归测试。本项只涉及发现/合并机制，未调用模型，未执行被测接口，未声称静态清单证明运行系统全集。

1. **相同声明不再误判为冲突（6a）**：`tasks/discovery.py` 合并段将无条件 `len(rows) > 1` 判冲突改为按声明签名集判别——只有当同一 `method path` 存在多个**不同签名**的声明时才置 `conflict`；完全一致的重复声明保留底层状态并追加一致声明数量说明。不同描述（不同签名）的文档声明仍会判冲突（等价于既有 `test_document_conflict_scope_and_unsupported_kept` 预期不变），仅完全相同者去伪。
2. **显式 openapi 适配器不再双重解析（6b）**：通用文档循环对已由所选适配器等扩展名解析过的文档跳过，避免同一 `.yaml/.yml/.json` 被适配器解析一次、又由通用 `openapi()` 再解析一次而造成的重复声明与假冲突。
3. **副产物修正**：`has_code_routes`/`has_document_routes` 改为严格按行 `kind`（implementation/requirement）判定，而非“code_rows 是否非空”，避免 openapi 文档适配器产出的 requirement 行被误当作已存在代码路由而触发 `only_in_document` 待确认降级。

**回归测试**（新增于 `tests/test_task_discovery.py`）：两个内容完全相同的重复文档声明不判冲突（保持确认且双声明保留）；显式 `adapter="openapi"` 单文档只解析一次、无重复/假冲突声明。

**实测结果**：`uv run python -m pytest tests/test_task_discovery.py -q -p no:cacheprovider --basetemp=runtime/test_discovery_issue6`：**14 passed**；全量 `uv run python -m pytest -q -p no:cacheprovider --basetemp=runtime/test_all_issue6`：**452 passed in 54.67s**（较此前 450 项净增 2 项）；`ruff check` 与 `ruff format --check` 通过。本项推进 F2 接口发现的合并质量，真实项目逐项验收仍属 F7。

## 2026-11-04：审查缺口修补——OpenAPI 解析、版本关联与请求模型展开（评审第 4/5/7 项）

针对接口采集评审表第 4、5、7 项实现问题（OpenAPI 跨文件/服务器/参数覆盖缺失、文档与源码版本不对应、请求模型未展开），结合既有第 6 项，完成代码修复与回归测试。三项只涉及发现/解析机制，未调用模型，未执行被测接口，未声称静态清单证明运行系统全集；请求模型仅对快照集内静态可见的类展开，其余保持未解析。

1. **OpenAPI 引用与服务器/参数覆盖（第 5 项）**：[adapters/openapi.py](file:///D:/workspace/TestPilot/src/api_test_agent/adapters/openapi.py) 将仅限文档内 `$ref` 解析扩展为可解析跨文件/外部 `#/` 引用（`_walk_external_refs` 迭代去重收集非 `#` 引用并保留未解析告警）；服务端地址改用仅顶层 `servers` 之外，回退到层级覆盖（root/path/operation）的优先级；路径级参数与操作级参数合并时按 `(in, name)` 同名单覆盖（`_merge_parameters` 两遍法）。Swagger v2 `basePath` 回退行为保持不变（回归保护）。外部引用未解析或字段歧义时保持 `unresolved` 与明确告警，不伪报 `confirmed`。
2. **文档与源码版本关联（第 7 项）**：[tasks/discovery.py](file:///D:/workspace/TestPilot/src/api_test_agent/tasks/discovery.py) 对来自 `commit_paths`（源码路径）的 `SourceRef` 盖固定提交 `target_commit`，对 `document_paths`（外部文档，本地文件）盖 `unverified-document` 占位版本；`Inventory` 新增 `all_materials_version_verified`，仅在既有版本校验通过且不存在外部文档时才为真，否则 `version_verified=False` 并给出 `MIXED_VERSION_WARNING`，避免用本地旧文档对上新源码伪报已验证。[tasks/models.py](file:///D:/workspace/TestPilot/src/api_test_agent/tasks/models.py) 的 `SourceRef.version` 保留非空约束。
3. **请求模型展开（第 4 项，源码侧）**：[adapters/python_ast.py](file:///D:/workspace/TestPilot/src/api_test_agent/adapters/python_ast.py) 扫描快照集内 Pydantic 类（`BaseModel`/`Model` 基类），把注解解析到该类参数展开为 `request_body` 条目（字段名、类型、必需项、字面量默认值；`_default_expr` 对动态默认仅记录不评估）；快照集外不可静态见到的模型保持未展开，不伪报字段。认证仍如实标注 `unresolved: dependency semantics not evaluated`（静态 AST 无法判定依赖注入语义）。

**回归测试**：新增于 [tests/test_adapters.py](file:///D:/workspace/TestPilot/tests/test_adapters.py) 的项 4 请求模型展开（快照集内模型字段展开、集外模型不展开）与历史项 5 外部引用/服务器覆盖/参数覆盖测试；[tests/test_task_discovery.py](file:///D:/workspace/TestPilot/tests/test_task_discovery.py) 保留的混合版本（docs 为 `unverified-document`）测试持续通过。

**实测结果**：全量 `uv run python -m pytest -q -p no:cacheprovider --basetemp=runtime/test_all_457`：**458 passed in 54.76s**（较第 6 项后的 452 项净增 6 项）；`ruff check` 与 `ruff format --check` 全通（86 files already formatted）。本项推进 F2 接口发现的解析与版本关联质量，真实项目逐项验收仍属 F7。


## 2026-11-04：接口采集第 4/5/6/7 项续审与补齐

续审确认：此前第 5 项的 `_walk_external_refs` 仅收集外部引用告警，不能证明跨文件解析；服务器前缀存在操作/路径间串用、空前缀无法覆盖的问题。此前 `all_materials_version_verified` 只是局部变量，不是 Inventory 字段。前述“已完成跨文件解析/已新增字段”的记录由本节更正，历史 458 项通过不证明这些遗漏场景已覆盖。

本次补齐：OpenAPI 引用只从用户已选且有 SourceRef 的快照中按相对路径递归解析，保留引用文件来源；不访问网络或补读磁盘。缺失、远程、循环和深度超限引用保持 unresolved。服务器按 operation > path > root 独立选择，支持空前缀和空服务器列表覆盖，避免跨操作/路径泄漏。参数按 (in,name) 覆盖，无名称或未解析引用参数不相互吞并。默认发现路径与显式 openapi 适配器都接入快照解析，显式适配器保持单次解析。

Inventory 现显式保存 all_materials_version_verified；外部文档使用 unverified-document，包括与提交文件同路径的外部输入；只读提交快照且无外部文档时为真。第 4 项保留现有静态模型字段展开；只证明简单直接类参数和字段，不保证完整 Pydantic 继承、Field/Annotated、嵌套模型、动态默认或 DI 认证语义。认证仍为 unresolved。第 1/2/3/8 项能力边界不变。

验证（Windows，项目 .venv Python 3.14，2026-11-04）：uv 启动器报 `uv trampoline failed to canonicalize script path`，未改环境，使用 `.venv/Scripts/python.exe -m pytest`。全量命令加 `-q -p no:cacheprovider --basetemp=runtime/review-discovery-20261104-a3`：462 passed in 51.57s。随后参数合并保护最终状态定向复测：同样选项，basetemp 为 runtime/review-discovery-20261104-a4，测试 tests/test_adapters.py 与 tests/test_task_discovery.py：34 passed in 1.38s。Ruff check/format --check src tests 通过（86 files）；git diff --check 通过。新增测试覆盖跨文件递归与循环/来源、服务器独立覆盖/空前缀，以及两条发现入口的跨文件集成与版本字段。

未调用模型或真实被测接口，未读取被测仓库、提交或推送；真实项目全集和真实模型质量仍未验收。未使用跨项目同步工具。


## 2026-11-04：Apple 接口文档 URL 导入入口

已实现最小 URL 导入链路：`task discover <任务> --openapi-url <HTTP(S)地址>`，也可通过 `task configure <任务> --openapi-url <地址>` 预登记。无需 Git 或 Java/Node 源码；任务仍需登记项目、完整提交与 scope。TaskStore 对 URL 使用既有目标锁定机制，URL 纳入 task_binding；未使用 URL 的旧任务绑定保持兼容。

宿主在 `tasks/openapi_import.py` 获取一个明确地址，冻结为 `url-import/<URL哈希>.json` 虚拟文件，交给原有 OpenAPIAdapter/默认发现路径，未修改适配器。SourceRef 保留 URL 摘要和内容哈希，版本为 unverified-document，两项版本验证标志均为 false；这不证明文档属于用户登记的提交。下载只允许 HTTP(S)，不携带 URL 凭据、不读取环境代理、不跟随重定向，不自动抓取远程 $ref；15 秒网络读写超时、1 MB 内容上限。无认证头配置/交互登录能力，需要认证时先由用户导出本地文档。完整 inventory 已保存但状态推进中断时从快照恢复，不再次抓取可能变化的服务。

UI 的“直接导入服务/完整清单”和“文档路径或 URL”文案已纠正：网页表单仍是本地文档路径，URL 导入明确引导使用 CLI，不宣称实现网页 URL 拉取。使用方法同步 README。

实际验证：2026-11-04，Windows / 项目 .venv Python 3.14.4。第一轮相关测试 52 passed；最终 `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=runtime/url-import-20261104-02`：476 passed in 54.74s。新增 tests/test_openapi_import.py 共 14 项，含真实本地合成 HTTP 服务的 Swagger v2/OpenAPI v3 CLI 导入、来源与未核验版本、非规格/HTTP 401/500 拒绝、302 不跳转、大小与超时、凭据/协议校验、外部引用不爬取、状态中断恢复与旧任务绑定兼容。Ruff check src tests 与 format --check src tests 通过（88 files），git diff --check 通过。沿用项目 .venv 验证，未修复 uv 启动器环境。未连接 Apple 或任何真实被测服务，未调用模型，未提交或推送；UI 文案未做浏览器目视复核。

### 其余评审项的核查与待办（本次未实现）

- ② 已确认：regression/runner.py 仍通过 entry 字符串/task_binding 推断 task，直接调用 task_command，可绕过传入 builder；应后续以显式 runner 元数据分派。
- ③ 能力边界：execution_kind 只对 HTTP 类型放行，JUnit/Jest 无执行实现；未知类型保持拒绝，不宣称多执行器已落地。
- ④ 能力边界：未实现 java-annotations/ts-decorators。URL 文档可访问时使用通用规格；无规格时暂没有这些源码兜底。
- 桥接可移植性：bridge.py 在非默认根目录时写入本机绝对 --root，且调用 store._folder 私有方法；本次记录，未改造。
- 退出码风险核查：tasks/cli.py 当前 failed>0 返回1，error/blocked>0 返回2，其余返回0，基础失败/错误与 classify 映射并非直接冲突。但仍缺 task 专用结果分类，不能据此证明 skipped/not_run/零执行可正确核算；后续应以结构化 accounting 验证，而非仅复用 pytest 输出语义。
- version_manifest 仍是多仓版本字段能力，不代表24仓库采集/编排已验收；本次只验证单个文档 URL。真实 Apple 系统接口覆盖与真实模型质量仍在 F7。

## 2026-11-04：用户反馈续审待修项

当前工作区 URL 导入已有实现，本次14项复测通过；② 显式 runner 分派、③ 非HTTP执行器、④ Java/TS兜底仍未完成。新增合成复现：单条桥接执行完成使后续条目被 completed 状态拦截；CLI 零执行返回成功并被回归误判 passed；凭据环境过滤；含空格 argv 被重拆；无效 case-id 留下 running。默认根目录随被测仓库 cwd 错位已静态确认。详见 docs/acceptance.md 本日“用户反馈续审”。
优先修复桥接状态/重复运行边界、结构化结果分类和凭据转发，再统一显式 runner 分派及路径解析。F8 当前只证明导出和模拟调度，不代表真实多用例回归闭环验收。本轮仅审查，未修复上述问题。

## 2026-11-04：F8 桥接缺陷修复（P1 状态/分类/凭据，P2 路径与私有方法）

修复上一节“用户反馈续审待修项”中的 7 项缺陷、② 显式 runner 分派、`--root` 绝对路径与 `store._folder` 私有方法；③ 非 HTTP 执行器、④ Java/TS 源码兜底仍是能力边界，不在本次范围。逐项实现、测试与未验证边界见 `docs/acceptance.md` 同日“F8 桥接缺陷修复”节。

- 任务状态边界：`--case-id` 运行结束回到 `ready`（新增 `running -> ready`），只有全量运行才 `completed`；`completed` 可重放；单用例运行不继承上一轮 resume 记录。
- 结构化分类：`classify_accounting` + `task execute --accounting-json`；回归 runner 只读该文件判定，`--max-cases 0` 判 `skipped`/`incomplete`；`testpilot run` 在未执行完时返回 1。
- 凭据：`task_binding.credential_env` 只记变量名，`run_plan` 按名单把值转发进 task 子进程；pytest 入口仍过滤 KEY/TOKEN。
- 显式 runner：`TestCase.runner` / `PlanEntry.runner`，删除 entry 字符串嗅探；`task_command` 不再二次拆词，也不再注入 `suite.extra_args`。
- 路径：`--root {task_root}` + 配置 `task_root`（默认 `runtime/tasks`）；`TaskStore.folder` 取代 `_folder`（78 处）。
- 顺带修掉一个被模拟测试掩盖的入口缺陷：`python -m api_test_agent.cli` 无 `__main__` 守卫、只导入不执行且返回 0，现改为 `python -m api_test_agent` 并补守卫。

实测：全量 496 passed（`runtime/fix-all-2`，83.76s，较 476 项净增 20 项）；`tests/test_bridge_live.py` 4 项真实子进程+合成 HTTP 服务通过；Ruff check/format 与 `git diff --check` 通过。未连接真实被测服务、未调用模型、未提交或推送。

## 2026-11-04：文档导入开箱接入改进

用户要求下载后能使用常见服务文档，无需为大小、认证或聚合改源码。本次完成：
- 默认单文档20 MB、总量100 MB、64文档；TOML可配置大小、数量、网络等待、总时间预算与重定向跳数，超限明确提示配置键，不截断规格。
- header_env 仅保存环境变量名，支持 Authorization/Cookie/API-key；缺失变量在网络请求前失败。认证头仅发往入口来源，跨源重定向不转发认证，也不复用服务器Cookie。
- 默认同源重定向与远程引用获取；跨源需 allowed_origins；冻结原文、最终URL与哈希，解析器只读快照。缺失/未允许/循环引用保持 unresolved。
- 支持 Springfox/Knife4j swagger-resources 列表与 Swagger urls 配置；任一必需服务下载失败中止，不宣称完整。
- 新增 task import-openapi 一步建任务并发现，保留 discover/configure 接口；新增示例TOML和内置合成文档，README起步命令不再依赖私有被测仓库。
- SourceRef.source_url 与 Task.openapi_options 在为空时不进入序列化，保留旧任务绑定兼容；导入配置纳入任务锁定和绑定。

验证详见 docs/acceptance.md 本日“文档导入开箱接入改进”：508项全量测试、Ruff、打包及内置示例通过。未连接真实Apple或模型；UI导入表单、交互登录、多来源不同凭据自动分配不在本次实现范围。

## 2026-11-05：多项目交互、本地源码与接口文档双模式支持

解决 Web 控制台代码仓库模式静态表格无交互字段、接口文档模式仅有写死路径且不支持 URL 输入/缺少 materials_root 崩溃、顶栏无法动态新建项目，以及 CLI task create 缺少参数的问题：
- Web 端：
  - 接口文档模式（`form-doc`）：新增单选切换在线 URL（Spring Boot/Knife4j/FastAPI）与本地规格文件（.json/.yaml），提供 URL 输入框、文件相对路径、微服务名、Commit SHA 与 Base Path 前缀；
  - 代码仓库模式（`form-code`）：重构为交互表单，支持 Git 本地仓库路径、Commit SHA、源码相对路径与适配器选择；
  - 顶栏增加 `+ 新建项目` 弹窗，支持录入 Task ID、项目名及文档 URL/文件；
  - 发现接口前自动保存配置并携带 Token，切换项目反显表单；
  - 修复 `server.py` 在 UI 触发发现接口时未传 `materials_root` 导致 `ValueError: document selection requires --materials-root` 的崩溃。
- CLI 端：`task create` 一步支持 `--repository`、`--source-paths`、`--document-paths`、`--openapi-url` 与 `--adapter`；`task configure` 补充 `--adapter`。
- 免命令启动：`start.bat` 选项 3 兼容在线 URL 与本地 JSON/YAML 规格文件；选项 4 支持一键扫描本地 Git 仓库并自动识别 HEAD 提交。

实测：全量 510 passed（新增 UI 服务端文档发现测试与 CLI openapi-url 测试）；Ruff check/format 通过；UI 11 项全通。源码 AST 静态分析仅面向 Python（FastAPI/Starlette），非 Python 服务走 OpenAPI 路线。


## 2026-11-05：Web 控制台工业级样式（baoyu-design）重构、圆角收敛与任意目录/文件浏览器集成

针对用户反馈的三个核心体验问题：
1. **UI 样式优化（遵循 `baoyu-design` 工业级美学规范）**：
   - 移除原有高饱和度天蓝色主色调（`#0284c7` / `#38bdf8`）与蓝黑色暗夜背景（`#090d16` / `#0f172a`）；
   - 深色模式切换为纯净中性碳素黑/黑曜石设计 Token（`--bg: #09090b`、`--surface: #121215`、`--border: #27272a`、`--text: #fafafa`）；
   - 浅色模式切换为纯白中性极简浅色 Token（`--bg: #fafafa`、`--surface: #ffffff`、`--border: #e4e4e7`、`--text: #09090b`）；
   - 统一采用工程校验态经典翡翠绿（`--primary: #10b981` / `#059669`）作为核心主强调色，呼应测试通过、健康与状态守恒判定；
   - 接口请求徽章规范化：GET 翠绿、POST 暖琥珀、PUT 紫罗兰、DELETE 玫瑰红，彻底消除泛滥紫灰蓝杂色。
2. **突破“目标仅有 4 个”限制，集成任意本地目录与文件浏览器（Filesystem Explorer）**：
   - 服务端新增 `GET /api/fs/list` 接口，支持按 `dir`（仅目录）、`doc`（仅 OpenAPI/Swagger 规格 .json/.yaml）或 `all`（全部）安全列出本机或工作区任意目录内容，并提供上级目录导航、工作区/被测项目/基准/示例/任务等快捷定位；
   - Web 页面新增 `modalFileBrowser` 本地文件与目录选择器模态框，提供路径输入、面包屑导航、目录内容搜索、一键快捷定位、文件类型徽章、双击进入/选取；
   - 在接口文档模式（`inputDocPath`）、代码仓库模式（`inputRepoPath`）、源码目录（`inputSourcePaths`）旁均新增 `[📂 浏览文件/目录]` 交互按钮，用户可直接选择磁盘上任意文件夹或 OpenAPI 规格文件；
   - **支持文件夹多选（Multi-folder Selection）**：文件浏览器新增多选模式（`chkMultiSelect`）、独立多选复选框、顶部已勾选标签栏（Chips Bar）、全选当前目录文件夹（`selectAllFolders`）与一键清空；在源码扫描目录（`inputSourcePaths`）旁点击“多选文件夹...”可同时勾选多个源码子目录，自动按逗号拼装回填至配置表单；支持跨目录勾选累积与点击标签即时删除；
   - 顶栏被测目标下拉框扩展动态任务分组、示例基准与“+ 浏览选择目标”，新增 `[📂 浏览选择目标]` 按钮，允许用户选择任意本地目录或规格文件作为测试目标，彻底解除“只有4个死目标”的束缚。
3. **全站圆角弧度收敛减半**：
   - 移除大圆角 `rounded-2xl`（16px）与卡片气泡 `rounded-xl`（12px），整体收敛至小巧克制的 `rounded-md`（6px）、`rounded`（4px）与 `rounded-sm`（2px）；
   - 按钮、输入框、徽章标签与状态条严格采用精密工程级微圆角，与 `baoyu-design` 工业美学深度契合。
4. **全站圆角极致收敛（2px）与 HTTP 防强缓存加固**：
   - 针对浏览器磁盘与内存强缓存导致未显式刷新时仍渲染旧版 UI 的问题，在 `server.py` 的 HTML/JSON 响应头中注入 `Cache-Control: no-cache, no-store, must-revalidate, max-age=0`、`Pragma: no-cache`、`Expires: 0`；
   - 在 `dashboard.html` `<head>` 中注入对应 `<meta>` 防缓存标签，将顶栏版本徽标更新为高对比绿色的 `v0.2.3 · baoyu-design (已生效)`；
   - 在样式表中注入全局强制 2px 极窄精密微圆角规则（除状态指示圆点保留圆形外，全量按钮、输入框、选项卡、卡片、徽标统一强制收敛至 2px，彻底根治大胶囊圆角）。

实测结果：
- 服务端与前端样式单测：`tests/test_ui_server.py` 增至 13 项通过（新增 `test_ui_fs_list_api` 与 `test_ui_dashboard_styling_and_explorer_elements`，断言 Token、无蓝色残留、模态框元素、Cache-Control 与圆角收敛）；
- 全量测试：`.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=runtime/tmp-ui-20261105-03`：**512 passed in 79.04s**（全绿，较此前 510 项净增 2 项）；
- 代码质量：`ruff check src tests` 与 `ruff format --check src tests` 全量通过（89 files already formatted）。

## 2026-11-05：Web 控制台 P0 缺陷修复与视觉重构（frontend-design）

用户要求直接改代码，并重做控制台外观。本轮不只换样式，先修掉三个 P0 缺陷和三个会让界面静默失效的实现错误，再按「账本 / 规格单 / 实验室仪表」一套视觉语言重写 `dashboard.html`。

**P0 安全与数据真实性**
1. **存储型 XSS**：旧页面把服务端返回的接口路径、用例标题、规则原文、证据内容直接拼进 `innerHTML`。现在所有插值走 `esc()`，JSON 抽屉用 `textContent`，Markdown 只从已转义文本渲染；`jsq()` 负责把动态字符串安全放进内联事件参数（Windows 路径含反斜杠，单纯 HTML 转义不够）。文件浏览器的行、快捷目录、已选标签改用 `data-*` + 事件委托，不再拼 `onclick`。
2. **`/api/fs/list` 未授权且不限目录**：旧实现任何本机进程不携带令牌即可列出磁盘任意路径。现在该端点要求与写操作相同的会话令牌，拒绝 `Sec-Fetch-Site: cross-site`，并把结果限定在 `allowed_roots`（工作区 + 新增的 `--allow-root` 参数）；子项、父目录、快捷目录一律先解析再校验，越界返回 403。
3. **伪造数据**：删除全部假数据——假接口清单、假用例、假执行记录与守恒报告、假的 `approveAll()` 审批按钮、带 `NullPointerException` 堆栈的假证据、以及 `/api/model/ping` 失败时的伪成功兜底。五个面板现在都只读真实端点，无数据时展示空状态并给出可直接复制的真实 CLI 命令。控制台按设计只读，执行请求在服务端即被 403 拦截，所以界面给出下一步命令而不是假装能点「执行」。

**顺带修掉的实现错误**
4. **`datetime` 序列化导致整条响应断连**：`/api/tasks/{id}` 的 `history[].at`、`/reviews` 的 `at`、`/evidence` 的 `timestamp` 都是 `datetime`，`json.dumps` 直接抛 `TypeError`，浏览器侧表现为 “Failed to fetch”。新增 `_json_default` 并作为 `default=` 传入；补了断言 `history[0]["at"]` 为字符串的回归测试。
5. **在线文档模式无法保存**：`configure` 的 `allowed_keys` 缺 `openapi_url` / `openapi_options`，新表单里选「在线 OpenAPI 地址」保存会被拒。
6. **已发现任务无法再次解析**：`configure` 在资产或发现之后会锁目标，界面点「执行接口解析」直接失败。现在仅在错误信息匹配 `locked` 时忽略保存错误、继续用磁盘上的 `task.json` 解析；其他错误照常提示。
7. **浏览目录会覆盖项目名**：文件浏览器回填后把 `DemoService` 改成目录名 `src`；改为仅在项目名为空时回填。
8. **CRLF 与 benchmark 字节一致性**：本机 `core.autocrlf=true` 让 `benchmarks/public/*.json` 工作区为 CRLF，与契约要求的 LF 不一致，导致 3 条字节哈希测试失败。新增 `.gitattributes`（`* text=auto eol=lf`，`.bat/.cmd` 保持 CRLF，二进制不转换），并把三个 JSON 重写为 LF，用 `git hash-object --path` 核对与 HEAD blob 一致。

**视觉重构**
- 设计方向定为「账本 / 规格单 / 实验室仪表」：石墨底（`--bg: #09090b`、`--surface: #121215`）、翡翠绿只用于 `passed`（`--primary: #10b981`）、结构性强调另用琥珀（`--accent: #d8a24a`），衬线标题（Iowan Old Style / Palatino / Georgia）+ Cascadia Mono 数据字体，0 圆角，34px 极淡网格，左侧编号阶段栏带状态圆点。
- 移除 Tailwind CDN 与 Google Fonts，改为手写 CSS，与 `default-src 'self'` 的 CSP 兼容；页面不再依赖任何外网资源。
- 五个阶段各自成页：数据源、模型与预算、接口清单（可按方法过滤、搜索、查看单条操作）、测试集与依据（用例卡展开显示规则/依据/步骤/断言/审核历史）、执行与守恒。执行页的分段守恒条与 7 格图例是整套视觉的核心表达：计划数必须等于各终态之和，缺证据的条目单独计数并列出 ID。
- 深浅两套主题（◐ 切换，写 `localStorage`），Sources 追溯抽屉可查看 `task.json`、`cases.json`、`execution.json`、`report.md` 等原件。

**验证**
- `uv run pytest -q -p no:cacheprovider --basetemp=<全新目录>`：**516 passed in 74.31s**（较 512 项净增 4 项：`fs/list` 令牌、越界路径、额外允许根、`configure` 接受 `openapi_url`）。
- `uv run ruff check .` → All checks passed；`uv run ruff format --check .` → 113 files already formatted。
- 浏览器实测（`uv run testpilot ui --no-open --port 8099`，合成任务 `demo-console`，1 个已发现操作）：真实数据注入、五个阶段切换、用例卡展开、文件浏览器多选回填、深浅主题、执行页守恒条/记录表/报告渲染均正常；测试用的临时任务与后台服务已清理。
- 未验证：真实 Apple 服务、真实模型、非 HTTP 执行器与 Java/TS 源码兜底仍属原边界；本轮未调用模型、未访问被测业务接口、未提交推送。

## 2026-11-05 续：独立的浅色主题与可见的主题切换器

用户看完上一轮重构后指出：现在的界面只算暗色模式，需要一套不刺眼的浅色模式。上一轮的 `html.light` 只是把暗色 token 反相（近白底 + 近黑字），入口是右下角一个不带文字的 ◐ 图标，实际观感像「暗色主题套了个白底」。本轮把浅色当成独立的一套配色重做。

**浅色主题：暖纸张**

1. **重新定色**：页面底色改为暖石灰 `--bg: #e9e6dd`，面板是纸白 `--surface: #fbf9f4`，不用纯白；正文墨色 `--text: #232019` 而非纯黑；边框 `--line: #dcd6c7` / `--line-strong: #c3bbaa`。整套是「纸铺在石板上」的层次，不是暗色的取反。
2. **状态色整体压深**：`--ok/--primary: #0f7a55`、`--bad: #ab2b2b`、`--err: #a9530a`、`--blk: #6743ac`、`--skip: #6b655a`、`--accent: #94621a`，保证在纸面上可读；`--text-3` 由 `#7d7768` 调深到 `#6a6350`，`--idle` 由 `#b0a897` 调到 `#98907c`。
3. **实测对比度**（WCAG，面板底面 `#fbf9f4`）：正文 15.45:1、次级 7.47:1、三级 5.68:1、ok 5.07:1、bad 6.41:1、err 5.08:1、blk 6.71:1、skip 5.49:1、accent 4.96:1；三级文字在页面底色上 4.79:1，`--idle` 作为图形元素 3.02:1。没有一处低于 AA。
4. **遮罩与阴影改为 token**：新增 `--scrim`、`--shadow-lg`、`--shadow-xl`；浅色用 `rgba(38,32,20,.34)` 的暖遮罩与 14%–20% 的暖阴影，不再是暗色那套 45% 黑投影。

**主题切换**

5. **顶栏新增可见切换器**：「◼ 深色 / ◻ 浅色」分段控件放在顶栏动作区首位，沿用既有 `.seg` 样式；右下角图标按钮保留为快捷切换，两处状态通过 `applyTheme()` 同步。
6. **首帧前生效**：`<head>` 内联脚本在样式表之前读取 `localStorage['testpilot-theme']`，浅色刷新不会先闪一下石墨黑；未保存过选择时跟随系统 `prefers-color-scheme`，并监听系统切换实时跟随（用户一旦手动选过就固定下来）。
7. **顶栏换行**：Sources 抽屉打开、宽度不足时，动作区整体换行而不是把左侧项目/提交/状态元信息挤成两行。

**验证**

- `uv run pytest -q -p no:cacheprovider --basetemp=<全新目录>`：**516 passed in 74.71s**；`tests/test_ui_server.py` 的样式测试新增断言（`html.light` 独立调色板、两个切换按钮、`prefers-color-scheme`、主题脚本先于 `<style>`）。
- `uv run ruff check .` → All checks passed；`uv run ruff format --check .` → 113 files already formatted。
- 浏览器实测（`uv run testpilot ui --no-open --port 8099`，演示数据只在内存中注入、未写盘）：浅色下逐屏核对数据源、模型与预算、接口清单、测试集、执行与守恒、文件浏览器模态框；深色回归无变化；刷新后主题持久、无首帧闪烁；遮罩与阴影在浅色下正常。
- 未验证：真实项目数据下的长文本与超宽表格；未提交、未推送。

## 2026-11-05 续：控件高度对齐、3px 细圆角，并入冒烟三条修复

用户反馈两件事：部分控件高度没对齐；直角太硬，需要一点点圆角但不能太圆。同一轮并入冒烟报告的三条问题。

**界面**

1. **高度对齐**：`.with-btn .btn-sm` 25px → 32px，与同排输入框等高；`.theme-switch` 去掉外框边框，两个分段按钮 30px，与 `.btn` 齐平；文件浏览器工具栏按钮 25px → 30px；浏览器筛选行统一 28px（按钮与输入框同高）。
2. **细圆角**：新增 `--radius: 3px`，只给按钮、分段控件、输入框、下拉、文本域、chip/pill、cmd、note、panel、case、kpis、browser-list、toast、modal-card 使用；全局 reset 仍是 0，表格、阶段栏、抽屉保持直角。`.seg`/`.theme-switch`/`.kpis`/`.browser-list`/`.toast`/`.modal-card` 加 `overflow: hidden`，避免子元素背景顶破圆角；主题切换器首/末按钮只圆外角。
   - **右栏接缝**：`.app` 由 flex 改为三列两行网格，`display: contents` 让页头与内容区直接进入网格；Sources 顶栏移出抽屉，与页头同处第一行（实测两者同为 144px 高、底边在同一像素），分隔线贯通整页；顶栏底色由 `--surface-2` 改回 `--surface`，与页头连成一条；关闭右栏时 `no-drawer` 把第三列收为 0，面板宽度从 648 回到 1068。

**冒烟问题**

3. **全盘文件选择**：`Task.document_paths` 接受盘符、UNC 与 POSIX 绝对路径（仍拒绝 `..` 段与隐藏文件名）。发现阶段对绝对路径直接读取，快照键为 `external/<文件名>`（同名时加 8 位路径哈希前缀）；`Inventory.target_commit` 允许 `None`，只声明文档不声明提交的任务也能发现。外部文档在 source.summary 与限界提示里标出真实绝对位置。
4. **单文档不再受目录黑名单限制**：新增 `document_permitted()`，只判断被点名的文件（扩展名白名单 + 敏感名正则），`runtime/`、`data/` 等目录下的文档可读；本地文档上限从 1 MB 改为 `MAX_DOCUMENT_BYTES`（20 MB），与在线 URL 导入一致。
5. **`start.bat` 不再伪造提交**：demo 任务不声明提交；导入菜单增加可选「Declared commit」；扫描 Git 仓库时读不到 HEAD 直接报错返回菜单，不再写 40 个 0。
6. 报告称「`/api/fs/list` 无令牌、可列任意绝对路径」与现状不符：该端点已要求会话令牌并把路径限制在 `allowed_roots`，已有 3 项测试，本轮未改动。

**验证**

- 全量：`uv run pytest -q -p no:cacheprovider --basetemp=<全新目录>`：**518 passed**（右栏接缝复测 75.36s）。
- `uv run ruff check .` → All checks passed；`uv run ruff format --check .` → 113 files already formatted。
- `tests/test_ui_server.py` 新增断言：`--radius: 3px`、`border-radius: var(--radius)`、`.with-btn .btn-sm` 32px、`.browser-tools .btn-sm` 28px；UI 套件 17 项通过。
- 浏览器实测（8123 端口，浅色与深色）：`.btn` 30px、`.theme-switch .seg-btn` 30px、`.with-btn` 输入框与按钮均 32px；`.panel`、`.btn`、`.with-btn` 输入框计算圆角均为 3px；页头与 Sources 顶栏同为 144px、底边同像素，关栏后第三列为 0、面板回到 1068px。截图已附在会话中。
- 未验证：无法在本机真实点击窗口上传对话框验证「全盘选择」的浏览器端回填，文档路径能力由 Python 测试覆盖；未提交、未推送。
