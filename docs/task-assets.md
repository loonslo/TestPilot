# F1：统一任务与资产基础（2026-10-30）

F1 的数据、状态边界与本机资产存储已经完成机制验证。F2–F8 的发现、生成、批准、执行和报告不属于本包完成范围。

## 由用户选择任务目标

创建任务不要求当场选择项目。项目、提交和资料读取范围保存在每个任务中，不预设衡仓，不读取全局被测目录。

```powershell
uv run testpilot task create my-task
uv run testpilot task configure my-task --project my-project --commit <完整提交> --scope "GET /items" --document-paths docs/api.json
uv run testpilot task validate-target my-task
uv run testpilot task show my-task
```

`configure` 只登记输入，不访问源码或文档；`validate-target` 只检查登记完整性，不证明仓库版本、资料存在或环境身份。F2 开始发现时才核对真实输入。源码模式需同时配置 `--repository` 与 `--source-paths`；文档模式配置 `--document-paths`。路径相对该任务后续选定的材料根目录，不能使用绝对路径、上级目录或隐藏路径。禁止的凭据和真实数据不因登记路径而获得读取许可。

目标尚未确定可以保留 created 草稿；不同任务各自选择。用例导入要求已选完整提交一致，发现或已有用例后不允许原位换目标，以免复用旧修订和审核；另建任务选择新版本。F8 将实现跨任务资产复用。

默认存储为被忽略的 runtime/tasks；自定义位置用 `task --root <目录>`，选用不提交的本机目录。本包没有自动启动服务、模型调用或 HTTP 请求。

## 可演示的合成用例

以下仅演示资产管理，提交和规则均为合成输入，没有生成或执行真实测试：

```powershell
uv run testpilot task create synthetic --project synthetic --commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --scope "GET /items"
uv run testpilot task import-case synthetic examples/tasks/human-case.json
uv run testpilot task import-case synthetic examples/tasks/ai-case.json
uv run testpilot task show synthetic
```

人工/AI 仅区分 origin，共用同一目录和校验。用例按 case_id/00000001.json 追加；修改必须顺序增加 revision、填写 change_summary 并用 supersedes 绑定上一版完整哈希。不覆盖旧定义；用例与来源、参数理由、准备/主体/核对/清理、断言都参与哈希。哈希并非语义等价判断。

JSON 保留 null、空字符串、数字和字符串的区别；mode=omitted 表示缺失，reference/generated 的运行解析属于 F3/F5。来源为相对位置、版本、摘要和内容哈希；规则区分 requirement、implementation、hypothesis，不能据此直接确认产品缺陷。接口对象保留方法、完整路径、挂载前缀、原操作 ID、来源、冲突/待确认/不支持/范围外状态与原因，F2 才负责实际提取、归一化和对账。

## 状态模型和拒绝路径

任务、修订审核和执行结果独立。`state` 只接受 blocked、aborted、created（从 blocked 恢复），每次写入前后状态、UTC 时间和原因，并原子替换完整任务文件；空原因、非法跳转、终止后恢复均拒绝。

```powershell
uv run testpilot task state my-task blocked --reason "等待资料"
uv run testpilot task state my-task created --reason "继续准备"
```

模型保留完整业务状态；discovered 由 F2 的接口清单接入，designed/awaiting_review 由 F3 材料接入。当前不能手动跳到 ready、running、completed 或 incomplete。F4–F6 必须接入批准清单与实际执行事实后才能推进这些状态；不是让用户自行填写“完成”。

每个新修订的审核状态为 draft。宿主可提交 pending_review，再记 rejected/deferred；每条记录绑定实际存在的最新修订完整哈希、操作者、时间和原因。新修订重新变为 draft，旧审核历史保留，不复用旧版决定。

```powershell
uv run testpilot task review-state synthetic --hash <import-case输出的哈希> --decision pending_review --actor local-user --reason "提交审核"
uv run testpilot task review-state synthetic --hash <同一哈希> --decision deferred --actor local-user --reason "预期需补充依据"
```

F1 不开放 approved：F4 尚需冻结任务/版本/环境/来源/执行器和批准集合。ReviewDecision 的 approved 仅预留数据契约，不能通过存储 API 记录。没有 approve/execute 或模型批准工具，review-state 不等同于方案中的完整审核材料导出。

执行结果契约为 passed/failed/error/blocked/skipped/not_run；前三者要求 evidence_ids。这个结构校验不验证证据真实性，F5/F6 将负责宿主证据和统计。没有写执行结果的 CLI；show 不制造通过计数。

## 旧资产适配契约

| 旧资产 | F1 保留与未来适配要求 |
| --- | --- |
| cases.py 探索用例 | 旧协议、public 可见范围和重放路径不变。不能因已有 rule_id 自动捏造参数理由、来源、账号映射或新审核；迁移需补齐统一元数据并作为新修订重新审核。 |
| regression 登记 pytest | 旧目录和命令继续可用。统一模型暂拒绝 execution_kind=pytest；F5 适配须冻结脚本、fixture、配置、参数和依赖快照，并读取结构化执行明细，不能只凭 node ID/退出码批准或判通过。 |
| 历史结果 | 保留原产物语义，不回填为新流程批准或执行证据。F8 通过新任务绑定复用修订，历史结果始终指向旧快照。 |

因此 F1 的“适配边界”已明确；自动转换、pytest 执行与资产跨任务复用仍分别属于后续工作包，未宣称完成。新旧命令均不改变原预算和审核要求。

存储面向单进程，不提供多进程事务、断电恢复或对有本机写权限者的防篡改保证。目标与历史的原子替换减少半写风险，修订和审核记录以独占新建方式防止正常路径覆盖。模型 frozen 不代表嵌套 JSON 在内存中不可变，哈希每次根据完整内容重算。

## 验证与下一步

可重复测试覆盖空任务、按任务选目标、不同任务不共享选择、配置锁定、来源路径拒绝、接口不确定性、非法状态跳转、历史保留、修订级审核失效与不能伪造批准/执行。实际命令演示和完整测试结果见 acceptance.md。

下一包 F2 实现通用的指定版本/资料读取、接口发现和输入对账；开发使用合成仓库和文档，不要求用户现在选真实目标。用户仅在实际任务使用时选择目标；真实模型和真实项目效果验收属于 F7。

## F2 首批离线发现（2026-10-30，进行中）

新增 `task discover <id> --materials-root <文档根目录>`。源码从任务 repository 的 target_commit 用 Git 只读命令取 blob，不 checkout、不导入目标 Python；只选择 source_paths 内文件，默认过滤隐藏文件、凭据命名、运行/数据/备份/已有测试等路径，超限和未匹配项记录为限制。文档仅接受 document_paths 且解析后必须位于显式 materials-root 内。来源内容和 SHA256 保存在被忽略的任务目录 inventory.json。

OpenAPI JSON/YAML 首批支持方法、paths、basePath/首个 server 路径、原 operationId；重复声明留为 conflict；multipart/流式为 unsupported，引用为 unresolved，范围以 `METHOD /path` glob 匹配，范围外条目保留。只读文档模式明确标记版本及运行系统未经核验。

FastAPI 首批只做 AST 静态局部声明：识别本文件 FastAPI/APIRouter、常量 prefix 和 HTTP 装饰器；不执行模块，局部结果为 unresolved，include_router/mount/add_api_route、动态路径和特殊装饰器保留逐位置限制。尚未实现跨模块挂载前缀解析，因此局部路径不能宣称是完整有效路由，也不能将本次 discovered 状态当作接口全集已确认。

`task materials <id> --max-chars 40000` 导出模型材料 JSON。预算按最终文本（含 JSON 转义与来源）计算，整文件剔除而不截断 JSON；每个未发送文件保留 source_id。连来源和遗漏清单都容不下时拒绝。此命令只导出材料，没有模型调用；F3 将复用实际输入构造函数。源码/文档内容原样保留空白，避免哈希与保存快照不一致。

回归采集也修复两个已知缺口：context_files 进入实际请求；最终请求按 max_total_prompt_chars 确实删除超限内容并记录遗漏，模板和元数据自身超限则拒绝。相关上下文现在从 head 提交读取，不读取脏工作区内容；最终裁剪会同步更新上下文与请求内遗漏说明。

F2 待补：跨文件 FastAPI 路由挂载图、文档/源码字段与认证差异的细化对账、失败时部分采集落盘与可重试/重入路径、原回归来源定位及最终送出记录的进一步核对。F3–F8 未完成；真实模型和真实项目验收仍未执行。所有本次验证使用本项目合成输入，不读取衡仓或其他真实项目源码。

## F2 路由图与失败恢复补齐（2026-10-30）

FastAPI 现通过 fastapi_static.py 构建静态挂载图：支持跨文件绝对/相对导入及别名、src 布局下可唯一定位的模块、常量 APIRouter 前缀、include_router、多次挂载、嵌套子应用 mount、api_route 多方法及 add_api_route。保留路由声明与各级挂载来源、参数注解/默认值、依赖和装饰器选项。路径可静态解析表示实现声明已确认，不证明业务规则或认证正确；依赖语义仍待人工确认。孤立路由、动态路径/前缀、循环引用、函数工厂和条件注册保留限制，不运行目标模块。WebSocket 使用独立方法标识并标 unsupported。

OpenAPI 增加本地 JSON Pointer 引用和 path-item 参数继承；外部引用不联网取回，循环/过深引用保留 unresolved。重复 YAML/JSON 映射键明确拒绝，避免悄悄覆盖。每个接口保存全部 declarations，记录参数类型/必填、响应状态和认证未确认等差异，以及只见于文档或实现的遗漏项；不把实现观察当成正式需求。

每次发现有独立 discovery/<attempt>/ 记录，采集到的原始资料在解析前保存；失败保存类型与时间，重试产生新记录并保留旧快照。若完整 inventory 已写入但任务状态尚未推进，重入校验任务/材料根绑定后复用冻结快照；不重新读取可能已变化的资料。Inventory 读取时校验来源集合、内容哈希及接口来源。

## F3 设计与审核材料（2026-10-30，机制进行中）

- `task design <id>`：只构造并保存 request.json、request.sha256，不调用模型。
- `task design <id> --response <响应.json>`：导入离线生成器响应并验证，明确不代表真实模型效果。
- `task design <id> --model-config examples/tasks/model.example.toml`：用户显式运行时才尝试真实生成；本轮开发未执行该命令。只允许当前获批的 OpenRouter 免费路由及环境变量 API_TEST_AGENT_MODEL_API_KEY；付费配置在读取 key 前拒绝，不改变回归付费调用批准要求。
- `--operations <接口ID...>`：选择本批，默认先取前十个范围内接口；完整清单仍由宿主生成七类场景矩阵，未选接口保留 pending，不丢弃。最终序列化请求预算包括指令、Schema、接口和实际材料，来源未送出时不得伪称已读。
- `task review <id>`：导出 Markdown 参数理由表、前置条件、规则/来源、步骤/副作用、预期与断言、场景矩阵、完整修订及 manifest；草稿进入 pending_review，绝不自动 approved。

生成器没有业务 HTTP 或批准工具。整批先校验再保存定义：提交/接口/来源必须匹配快照、每个参数必须有理由和依据、JSON 值与 omitted 模式区分、引用必须指向前序步骤、安全表达式不得执行代码、不得携带认证头、场景矩阵必须与本批实际用例一致。未确认的拒绝响应可作为 hypothesis 待审，不计正式缺陷。保存规则目录和场景矩阵；不合格输出及供应商失败保存失败记录，不能生成通过结果。后续修订重回 draft，已导出的旧材料保持。

实时适配最多一次调用且关闭自由工具；调用前检查请求数、时长及输入/输出 token 上界，使用原共享 runtime/model-usage.json 计数，调用前落账；按配置等待最小间隔，报告任何非零费用就停止接收用例并保存调用元数据。记录每次实际回答模型、用量和延迟，不保存 key。模型失败不隐式重试或触发 HTTP 测试。

F3 模型生成与审核材料导出已打通机制验证。已接入调用器不代表真实模型可用性或生成质量验收；真实效果留在 F7。

## F4 人工审核与执行前冻结（2026-10-30，机制验收通过）

- `task approve <task_id> <decisions.json> --execution-config <config.json>`：记录人工审核决定并冻结本轮执行集合。
- `task verify-approval <task_id> <approval_id> --execution-config <config.json>`：复核冻结批准快照完整性。

人工提交绑定审核文档与 manifest 哈希，支持逐条 approved/rejected/deferred。批准包冻结任务、全修订集合、来源清单、执行配置、执行器指纹、操作者和时间戳。若用例参数、断言、步骤、版本、配置或执行器实现发生任何变更，或者新增用例打破当前集合，旧批准立即失效并拒绝执行。外部部署明确标示 version_verified=false，模型无批准入口。

## F5 统一执行引擎与证据对账（2026-10-30，机制验收通过）

- `task execute <task_id> [--approval <id>] [--execution-config <path>] [--max-cases <n>] [--stop-on-failure]`

执行特性与边界守卫：
- 严格执行前置校验：仅在任务处于 `ready` 且批准快照完整性复核通过时才可启动；任何未批准或篡改均直接拒绝，HTTP 请求数为零。
- 回环地址限制：只能访问 `http://127.0.0.1:<port>`，禁止外连。
- 宿主凭据注入与安全脱敏：模型与用例只知账号别名（如 synthetic、alice），宿主在请求时按配置从环境变量读取令牌，所有证据（EvidenceRecord）、事件流（events.jsonl）和报告中令牌均替换为 `[REDACTED]`，绝不落盘或泄漏。
- 确定性参数与步骤执行：支持 literal、reference（`{{step.path}}` 解析）及 omitted（字段删除）。步骤分为 prepare、test、verify、cleanup 四阶段；prepare 失败记录为 error（若启动后出错）或 blocked（若依赖缺失）；断言失败记录为 failed；清理步骤无论成功失败均会运行，清理失败记录告警 issues，不掩盖测试主体通过状态。
- 守恒中断与恢复：支持 `--max-cases` 与 `--stop-on-failure` 中断，未执行用例显式标记为 `not_run`，任务状态置为 `incomplete`，未知副作用（side-effect）记录为未确定，绝不盲目自动重试。
- 既有 pytest 回归适配增强：`classify` 增加对全部跳过（skipped）和未收集到（no tests ran）的结构化明细解析，退出码为 0 但无通过用例时不伪装为 passed。

## F6 报告生成与守恒覆盖统计（2026-10-30，机制验收通过）

- `task report <task_id> [--execution-id <id>] [--output-dir <path>]`

报告与对账能力：
- 确定性对账公式成立：`计划执行数 (planned) = passed + failed + error + blocked + skipped + not_run`，并在报告中显式核算守恒状态。
- 落盘凭据真实性核验：`passed`、`failed`、`error` 必须有对应落盘证据（evidence_id 文件存在），否则标记凭据缺失，禁止模型捏造虚假通过。
- 接口与场景覆盖率独立展示：接口覆盖率展示发现接口数、confirmed 范围内接口数、支持自动执行数及实际调用接口数；场景覆盖率展示 7 类场景矩阵已设计与已执行槽位比例。
- 清晰的缺陷与未执行分类：报告区分疑似产品缺陷（suspected_defect）、未经确认的假设（unconfirmed_hypothesis）、用例问题（case_issue）、环境问题（environment_issue）及清理失败（cleanup_failure）。未批准、暂缓与拒绝用例单列统计。
- 全流程随需出具报告：无论任务完成（completed）、中断（incomplete）、未执行（ready）或阻塞（blocked），均能输出格式统一的 Markdown 与 JSON 报告。


## F5/F6 五项问题补验后的执行约定（2026-10-30）

`task execute` 恢复 incomplete 时仅继续 not_run，已尝试但结果不确定的中断请求记 error，不能自动重发。请求预算在全部阶段调用前检查，并跨同批准的恢复累计；旧执行汇总缺少 requests_attempted 时拒绝自动恢复。中断后先核对 error 的副作用和未完成清理，再决定下一次审核与执行。running 状态拒绝再次执行；本地存储没有多进程锁或强杀恢复保证。

步骤证据分别保存 request_attempted 与 response_received。接口覆盖只统计收到响应的步骤，HTTP 失败响应仍计调用，连接失败不计；场景覆盖必须来自该用例本身的响应证据。缺失、损坏或错配的证据使报告对账 FAIL，不计通过。旧证据未提供标记时不推断调用；本轮执行器指纹变化使旧批准失效，必须重新审核。

宿主凭据仅保留在内存供步骤引用和断言；落盘时按敏感值递归处理键和值，包括短令牌回显、请求参数、断言实际值、Issue 与报告。最新验证结果与未验证范围见 acceptance.md 的“用户确认五项修复后的边界补验”。


## F5/F6 三项 P2 边界修复（2026-10-30）

清理步骤响应现在与其他阶段一致写入内存 results，后续清理可用 `{{clean1.body.id}}` 引用；落盘证据仍沿用脱敏处理，清理失败仍作为独立 Issue，不改变主体断言结果。

POST/PUT/PATCH 请求明确区分整个 JSON 值为 null 和未提供请求体：`body` / `$` 的 literal null 或 reference 解析为 null 时发送 `null` 字节及默认 application/json；omitted 或没有 body 参数时不发送 JSON。具名字段 null 仍编码为对象中的 null。步骤证据 request_parameters 增加 body_provided，保留输入是否提供的区别。

Markdown 第 7 节在限制说明之后逐项输出与 JSON uncovered_operations 相同的接口缺口；当前清单没有缺口时明确注明。此修改不改变接口/场景覆盖率口径；真实项目与模型效果仍属 F7。详细验证见 acceptance.md 最新三项 P2 修复记录。
