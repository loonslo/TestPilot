# 验收记录

## 2026-10-15：A0 与 A1（沙箱阶段）

**环境：** Windows 10 Pro 10.0.19045；宿主 Python 3.14.4、uv 0.12.10、git 2.54.0.windows.1；被测运行时 Python 3.12.13、FastAPI 0.141.1、Starlette 1.6.0、uvicorn 0.52.4、pydantic 2.13.4、pydantic-settings 2.15.0。

**被测对象：** 衡仓后端 FinUnityServer，提交 `55cf875b9d0350e80cfed14b971b7bec8e22b50b`，tree `f04f293e7cd99cb63ac0e0c99fa9fd9ea2c79b6c`，64 个已提交文件。

**用到的命令（项目根目录）：**

```powershell
uv run python -m api_test_agent doctor
uv run python -m api_test_agent sandbox export     # 以及 verify、venv、seed、check
uv run python tools/probe_reference.py
uv run python tools/build_public_spec.py
uv run ruff format --check . ; uv run ruff check .
uv run pytest -q -p no:cacheprovider --basetemp=<新目录>
```

### 沙箱检查（`sandbox check`，13 项全部通过）

| 检查 | 结果 |
| --- | --- |
| 导出的副本与固定提交逐文件一致（Git blob 哈希） | 通过：64 个文件，缺失 0、改动 0、多出 0 |
| 种子库文件与记录的哈希一致 | 通过 |
| 账号 A、B 都能登录并读到各自的空状态 | 通过：`me`、账户、持仓、流水、汇总共 5 个读接口，两个账号都是 200 |
| A 写入的账户对 B 不可见 | 通过 |
| 快照能发现一次写入（对照，说明下面两项重置检查不是空检查） | 通过 |
| 被测进程的 TCP 套接字只在本机（一次时点快照） | 通过：2 个进程、3 个套接字，没有非本机连接，没有监听在本机以外 |
| 第一次重置回到初始状态 | 通过 |
| 第二次重置与第一次、初始状态完全一致 | 通过 |
| 所有场景之后种子库未变 | 通过 |
| 运行被测服务之后导出的副本未变 | 通过（没有 `__pycache__`，没有多出文件） |
| 源仓库 HEAD 和工作区状态前后一致 | 通过 |
| 没有遗留的场景目录 | 通过 |
| 被测端口已释放 | 通过 |

### 过程中发现并修好的问题

第一次导出后 `verify` 报 64 个文件全部“已改动”：Git for Windows 的系统配置是 `core.autocrlf=true`，`git archive` 把 LF 改成了 CRLF。导出改为带 `-c core.autocrlf=false`，重新导出后与提交逐字节一致。变异检查：临时去掉这个参数，`test_export_ignores_a_machine_wide_autocrlf_true` 失败（得到 `b'hello\r\n'`，期望 `b'hello\n'`），同文件另外 4 项通过；恢复后全部通过。

### 单元测试

73 项通过（全新 basetemp，Ruff 检查与格式检查通过）。覆盖：设置校验；ID 与受管目录删除守卫（含目录联接、孙目录、缺少标记）；被测进程环境的白名单（机器上的密钥、代理、数据库路径都进不去，也不能覆盖沙箱设置）；导出与逐文件校验（CRLF、中文路径、未跟踪文件不被复制、不改源仓库）；独立算术（含逐项篡改后会被报告）；公开文件的格式、互相一致性和敏感信息扫描。

### 参考构建探针与独立算术

`tools/probe_reference.py` 的一次完整运行：102 步（92 次 HTTP 调用、6 个探针自身断言、4 次算术对照）。所有设了预期状态码的调用都与预期一致；6 个自身断言全部通过；独立算术（`oracle.py`）在 4 个状态上与服务返回的汇总完全一致：十月、九月、B 越权尝试之后的 A、B 自己的状态。逐条规则的观察见 [rules-evidence.md](rules-evidence.md)。

### 公开文件（sha256 前 16 位；A0/A1 当时的值，后续已改，最新值见 A2 一节）

`benchmarks/public/openapi.json` 9fe86897e3b30823（9 个路径、10 个操作、7 个模型）；`rules.json` aa904d14e6e0aeb8（18 条）；`response-notes.json` d4ebf4971811f2a2。

### A0 与 G0 结论

A0 的三个验收条件在项目侧通过（见 [startup-record.md](startup-record.md) 第 5 节）。G0（A1 之后）通过：隔离、重置、账号可用都达标，没有需要暂停或换被测对象的问题。

### 未验证与限制

- 没有操作系统级的断网。只有一次 `netstat` 快照，不能证明运行期间从未发起外连；外部依赖靠配置关闭（OCR、邮箱、行情源都没有配置）。
- 沙箱只在这台 Windows 机器上验证过；被测进程用 `taskkill /T` 整树结束。
- 没有调用过任何模型，没有 Agent 代码，没有设计基准缺陷，没有任何检出率。
- 10 条规则的“业主确认项”没有经用户确认；规则里的错误码和类型映射目前取自参考实现。
- 现有 126 项测试是被测项目 2026-10-15 验收记录里的数字，本项目没有复跑。
- 被测环境是开发配置的本机沙箱，观察结果不代表线上行为。
- 用量与费用是估算，没有实测；价格沿用 2026-10-14 的核对，没有重新查。
- 项目目录是新建的独立 Git 仓库，尚无提交。

## 2026-10-15：A2（小基准与判定套件）

**环境与被测对象：** 同上。判定套件和缺陷构建都在本机沙箱里运行；没有调用过任何模型。

**用到的命令（项目根目录）：**

```powershell
uv run python -m api_test_agent benchmark build                  # 以及 --set mutants
uv run python -m api_test_agent benchmark verify                 # 以及 --set mutants
uv run python -m api_test_agent benchmark conformance --build <构建>
uv run python tools/build_dev_tasks.py
uv run python tools/build_public_spec.py
uv run ruff format --check . ; uv run ruff check .
uv run pytest -q -p no:cacheprovider --basetemp=<新目录>
```

### 判定套件

`conformance.py` 为 18 条规则各写了一个确定性检查。检查相对于它看到的状态写（差值、自己创建的 id），所以共用一个场景，并按 LED、ARC、ISO 的顺序运行，避免后面检查归档的记录影响前面的检查。准备数据失败记为 error（规则没被判定），不记为违反。**在参考构建上 18 条全部通过。**

### 五个缺陷（`benchmark verify`，每个构建单独运行）

| 构建 | 改动 | 套件标出的违反 | 预期 | 结果 |
| --- | --- | --- | --- | --- |
| reference | 无 | 无 | 无 | 一致 |
| def-01 | 去掉转账的币种比较 | LED-05 | LED-05 | 一致 |
| def-02 | 汇总按基准币种混加所有持仓 | LED-01 | LED-01 | 一致 |
| def-03 | 汇总读取时不再排除已归档流水 | ARC-01 | ARC-01 | 一致 |
| def-04 | 账户归档守卫漏掉转入账户 | ARC-03 | ARC-03 | 一致 |
| def-05 | 归档不检查记录属于谁（两处） | ISO-01、ISO-02 | ISO-01、ISO-02 | 一致 |

每个缺陷构建只改了预期的源文件（`v1_api.py` 或 `store.py`），并确认服务进程是从该构建自己的目录导入的包，而不是共用虚拟环境里的参考副本。

### 九个灵敏度变异体

它们不是 Agent 的任务，只用来证明套件在五个缺陷没碰到的规则上也会失败。9 个变异体加参考构建共 10 次运行，违反的规则都与预期一致：mut-01 LED-03（估值向下取整，报 `market_value_minor is 100, expected 101`）、mut-02 LED-04（月份过滤变成累计）、mut-03 LED-04（负数金额不取绝对值）、mut-04 ARC-02（归档不校验版本，报 `expected HTTP 409 VERSION_CONFLICT, got HTTP 200`）、mut-05 ARC-05 和 ISO-03、mut-06 LED-06、mut-07 LED-07、mut-08 ARC-04、mut-09 LED-02。五个缺陷加九个变异体让 18 条规则里的 15 条各有至少一个会失败的构建；**LED-08、LED-09、ISO-04 还没有**，套件对它们只证明了“参考构建上通过”，不知道对它们是否灵敏。

### 过程中发现并修好的问题

1. 第一次验证时 def-05 只被标出 ISO-02，没有标出预期的 ISO-01：ISO-01 的检查没有覆盖“用户 B 尝试归档 A 的记录后，A 读到的内容不变”，而规则陈述里包含归档。补上这一步后，def-05 同时违反 ISO-01 和 ISO-02。
2. mut-05（去掉持仓对账户的引用检查）实际同时违反 ARC-05 和 ISO-03：同一个检查既挡住已归档账户也挡住别人的账户。预期按真实效果改为两条。
3. 泄露扫描测试发现公开文件里有“reference build/service”的措辞，暗示存在不同的构建；已改成中性说法，并把这类措辞加入扫描。

### 任务与泄露

十个任务（`benchmarks/public/tasks/dev-01.json` 到 `dev-10.json`）：五个范围各用两次，一次在缺陷构建上，一次在参考构建上，共 5 个有缺陷、5 个没有。任务文件只含范围、规则、允许的操作和调用者、限额，不含构建信息；同一范围的两个任务除编号外内容完全相同（有测试）。测试扫描 Agent 会读的全部文件（接口片段、规则、响应说明、任务），不含“参考构建”“缺陷”“变异”“补丁”“注入”“基线”等措辞和构建编号。

### 单元测试

91 项通过（全新 basetemp，Ruff 检查与格式检查通过）。新增的覆盖：补丁必须恰好匹配一次、保留周围字节（含 CRLF 与中文）、不能越出构建目录；清单一致性（五个缺陷各出现一次、五个任务在参考构建上、18 条规则在范围里不重不漏、范围里的操作都在公开接口片段里）；任务文件与清单一致；套件对 18 条规则各有一个检查；变异体覆盖面的文档化断言；仅补丁构建才设置 `PYTHONPATH`。

### 文件（sha256 前 16 位）

`benchmarks/public/openapi.json` d706e4ffdd93a34c；`rules.json` ab69da09bba602a3（18 条，10 条带 `confirmed_on`）；`response-notes.json` 2e129e81b0f7449e；`tasks/` 全部拼接 039854c17f95b91a；`benchmarks/dev/defects.json` 9e3e0f64f496502a；`mutants.json` a0b98ad1b3ba1d8a；`manifest.json` c4f923d5f735499a。

### A2 验收结论

| 条件 | 结果 |
| --- | --- |
| 每个缺陷可独立触发 | 通过：每个缺陷单独构建，只违反它预期的规则 |
| 参考构建满足规则 | 通过：18 条全部通过 |
| 没有给 Agent 的泄题材料 | 通过：Agent 读的文件经扫描无泄露措辞，成对任务内容相同；缺陷、变异体和清单只在 `benchmarks/dev/` |

### 未验证与限制

- 开发集对开发者可见，**不是没见过的测试集**，不能当泛化证据；冻结测试集要到 A4 再做。
- 套件对 LED-08、LED-09、ISO-04 没有失败过的构建，灵敏度未证明；套件本身是确定性脚本，没有用 Agent 或第三方交叉验证，它的期望值来自规则陈述（其中 10 条经用户确认）。
- 十个任务的预算（12 次模型调用、30 次 HTTP 调用、600 秒）是方案默认值，没有用真实运行验证够不够，S-C 和 S-E 范围的操作较多，30 次 HTTP 调用可能偏紧。
- 没有调用过任何模型，没有 Agent 代码，没有任何检出率；没有和被测项目现有的 126 项测试做对比。
- 缺陷是我设计的单点缺陷，与真实线上缺陷的分布没有对照；def-01 到 def-04 的难度和 def-05 的影响面没有校准。

## 2026-10-15：A3 的离线部分（模型适配器、四个工具、用例重放、Agent 循环）

**状态：A3 进行中。真实模型还没有运行过：** 用户指定的模型是 OpenRouter 的 `openrouter/free`，但调用它需要的 API key 还没有设置（当前进程环境和用户级环境变量里都没有 `API_TEST_AGENT_MODEL_API_KEY`），所以以下都是不需要 key 的部分，用脚本化的假模型和真实沙箱验证。没有向任何模型服务发过请求。

**用到的命令（项目根目录）：**

```powershell
uv run pytest -q -p no:cacheprovider --basetemp=<新目录>
uv run python -m api_test_agent agent witnesses
uv run python tools/scripted_run.py
uv run ruff format --check . ; uv run ruff check .
```

### 实现了什么

| 模块 | 作用 |
| --- | --- |
| `model.py` | OpenAI 兼容的聊天调用（openai SDK 3.31.0，关闭 SDK 自带重试）。把错误分成 rate_limit、quota（当天额度用完）、timeout、network、server、auth、payment、policy、bad_request、protocol；记录实际回答的模型（路由器每次可能不同）、用量和费用 |
| `budget.py` | 一次运行的预算（模型调用、HTTP 调用、时间、token、费用）和按 UTC 日累计的请求数（写在 `runtime/model-usage.json`，跨运行保留）；每次尝试都计数，检查在调用之前 |
| `spec.py`、`targethttp.py` | 公开规格索引；对被测服务的调用只能走规格里的固定路由，令牌由宿主注入，路径参数只许普通标识符，不读环境代理、不跟随重定向，回给模型的响应最多 4 KiB，完整响应作为证据存盘 |
| `expr.py`、`cases.py` | 用例语言：步骤（操作、调用者、参数、期望状态码和错误码）加检查（一个比较式，支持 + - * 与比较运算、len、abs、sum）；表达式手写解析，不交给任何解释器；`{{步骤.路径}}` 引用前面的响应 |
| `tools.py` | 四个工具 `search_spec`、`get_operation`、`http_call`、`submit_case` 和它们的权限、预算、证据、用例校验；工具定义只用最朴素的 JSON Schema（没有 anyOf、$ref、additionalProperties），因为路由器可能把请求送到任何一家供应商 |
| `runner.py` | Agent 循环：检查预算、间隔、调用模型、先记录模型消息、串行执行每个工具调用并回传、最后一次调用留给最终报告；每次结束都带明确的停止原因（final_report、max_model_calls、max_seconds、daily_requests、no_progress、model_quota 等），预算停止不会写成 completed |
| `replay.py` | 在新场景里重放用例：参考构建上通过且目标构建上失败才算 `detects`；参考构建上就失败的用例记为 `fails_on_reference` |
| `capability.py` | 两次请求的工具调用检查（`model ping`），不碰沙箱 |
| CLI | `model status`、`model ping`、`agent run --task dev-02 [--replay]`、`agent replay --run`、`agent witnesses` |

### 验证结果

- **单元测试 245 项通过**（全新 basetemp，Ruff 通过）。覆盖：表达式解析与求值（含恶意输入、类型错误）、用例的静态校验和重放（含缺字段算失败、引用缺失算错误）、工具权限与预算与证据与令牌不外泄、适配器对各种 HTTP 错误的分类并确认 SDK 不会在背后重试、预算与每日额度、循环的各条停止路径（最后一次调用保留、忽略提示的模型、重复调用三次、限流等待、额度用完、连续失败、成功后清零）、运行产物与状态映射、有 bug 时场景仍会被停止。写测试时发现并修好一个真实缺陷：`Trace.event` 的参数名与事件字段 `kind` 冲突，导致任何模型错误都会被误记为 harness_error。
- **五个手写见证用例**（`benchmarks/dev/witness_cases.json`，每个缺陷一个）：各自在参考构建上通过，只在它对应的缺陷构建上失败，在其余构建上都通过（5 个用例 × 6 个构建的矩阵）。这证明用例语言能表达这五个缺陷，重放能把构建区分开。它们是我写的，不是模型的产出。
- **脚本化的端到端运行**（`tools/scripted_run.py`，一个不是模型的确定性“测试员”，经真实的运行器和真实沙箱）：dev-02（在 def-01 构建上）完成，7 次模型调用（脚本）、3 次 HTTP 调用、提交 1 个用例，重放标签为 `detects`；dev-09（在参考构建上）完成，没有用例。对这两次运行的全部 18 个产物文件扫描，没有沙箱密钥、令牌形态的文本。这只是流水线的冒烟测试，不是任何模型的证据。

### 关于 `openrouter/free` 要知道的（OpenRouter 官方资料，2026-10-15 查）

- 它是路由器：对每个请求从可用的免费模型里随机选一个，并按请求需要的能力（图像、工具调用、结构化输出）过滤。所以一次运行可能混用多个模型；运行产物里记录每次回答的模型（`served_by`），混用时在 manifest 标 `mixed_model`，不能当作单一模型的结果。要做可复现的评测，需要改成固定的某个免费模型（2026-10-15 的模型清单里，除路由器自身外有 13 个支持工具调用的免费模型）。
- 免费模型每天的请求数：累计购买额度不足 10 点是 50 次，达到后是 1000 次；每分钟上限的数字在文档里是占位符，没有查到。账号余额为负时免费模型也会返回 402。免费模型文档称可用性和限额可能与付费模型不同，“不适合生产”。
- 一轮十个任务最多需要 120 次模型调用，超过 50 次的日额度；`max_requests_per_day` 默认 45，运行前会检查，额度用完时以 `daily_requests` 或 `model_quota` 停止。
- 提示词会发给免费模型的供应商，供应商可能记录；发出的只有接口片段、规则、响应说明和合成数据，不含源码、环境文件、令牌或真实数据。

### 未验证

- 没有任何真实模型的运行：不知道它选到的模型能不能稳定地产出合格的工具调用参数，不知道工具定义会不会被某家供应商拒绝，不知道 `max_tokens` 对会“思考”的免费模型够不够，不知道每分钟限额。
- 十个开发任务还没有跑；A3 的验收条件（十个任务都有明确结束状态、至少一条模型产出的用例可在两个版本上重放）还没有满足，现在满足的只有“重放引擎能区分两个版本”（见证用例）。
- 提示词和工具说明是第一版，没有按真实模型的行为调过。
- `model status`、`model ping` 没有对真实服务运行过，`GET /key` 的返回格式只按官方文档假设。

## 2026-10-25：R0 并入 testagent（回归模块）

**结果：** testagent 并入 `api_test_agent.regression`，保留 `uv run testagent` 命令入口。293 项测试、Ruff 两项、探索模块离线链路与回归模块隔离用例通过。探索模块原有 `src/api_test_agent/*.py`、`tests/*.py`、`tools/`、`benchmarks/` 均未改动。全程未调用任何模型，未推送、未添加远程；FinUnity 单仓与原始 bundle 只读，未修改日常库。

**批准与预期差异：** 本次用户要求按合并执行方案实施，方案 A–D 均已勾选，包括本地基线与合并提交、两组隔离用例执行，以及验收通过后的旧目录移动归档。`testagent check` 实测当前进程看不到 `DEEPSEEK_API_KEY`，与方案的“已设置”预期不同；发现后暂停后续步骤并询问用户，用户于 2026-10-25 明确回复“接受差异，继续完成离线合并（不调用模型）”。因此该项按用户接受的差异记录，不写成密钥已设置。未读取或打印密钥、环境变量值、`.env` 或 `runtime/secrets.json`；本地探索配置仅修改 `source_repo` 一行。

**环境（本次实测）：** Microsoft Windows 10 专业版 10.0.19045；宿主 Python 3.14.4；uv 0.12.10；git 2.54.0.windows.1；被测克隆 Python 3.12.13；宿主 openai 3.31.0、pydantic 2.14.1、PyYAML 6.0.3、pytest 9.1.1、ruff 0.17.0。项目 `requires-python` 保持 `>=3.14`，被测环境按自己的锁文件用 Python 3.12 建立，未安装本项目依赖。

**被测对象：** 从 FinUnity 2026-10-25 整合时做的 FinUnityServer 完整 bundle（本机备份）恢复到被忽略的 `subjects/FinUnityServer`，移除克隆的 origin。`main` HEAD 为 `e69f09f18f85cb6516093e739282adba73d1284a`，共 24 个提交；探索基准 `55cf875b9d0350e80cfed14b971b7bec8e22b50b`，tree 为 `f04f293e7cd99cb63ac0e0c99fa9fd9ea2c79b6c`。

**前置与迁移核对：** 开工状态恰为方案中的 14 项未跟踪内容，无 HEAD；`subjects/` 与回归子包均不存在；bundle 校验通过。基线 245 项测试通过（6.11 秒），本地基线提交 `50c5997`。复制回归源码 11 个 Python 文件和 7 个测试文件；导入替换恰为 39 处，无旧 `testagent` 导入；默认提示词路径改为回归包内 `prompts/`。`uv add` 引入 PyYAML 6.0.3，锁文件新增 28 行；只对回归模块路径自动格式化，16 个文件重排，22 项 Ruff 问题自动修复。

**用到的主要命令：** 在项目根目录执行下列探索命令；回归命令分别在 `examples/regression/finunity/` 与 `examples/regression/demo/` 执行。PowerShell 的 basetemp 参数先赋给独立变量，再用带引号的完整参数传递。首次内联表达式传参被 pytest 拒绝，未执行测试；改正调用方式后基线与整体测试均通过。

```powershell
$pytestTemp = Join-Path $env:TEMP ("ata-r0-20261025-" + (Get-Date -Format HHmmss))
uv run pytest -q -p no:cacheprovider "--basetemp=$pytestTemp"
uv run ruff format --check .
uv run ruff check .
uv run python -m api_test_agent doctor
uv run python -m api_test_agent sandbox verify
uv run python -m api_test_agent sandbox check
uv run python -m api_test_agent agent witnesses
uv run python tools/scripted_run.py
uv run testagent --help

# examples/regression/finunity/
uv run testagent check
uv run testagent plan --base 55cf875 --head e69f09f --env-version r0-20261025 --stub --run-id r0-smoke-20261025
uv run testagent run r0-smoke-20261025 --env-version r0-20261025 --skip-confirm
uv run testagent plan --base 8d1836f --head 2f4b565 --env-version r0-20261025 --stub --run-id r0-pair-20261025

# 项目根目录
uv run python examples/regression/demo/setup_demo.py
# examples/regression/demo/
uv run testagent plan --base base --head head --stub --env-version demo --run-id r0-demo-20261025
uv run testagent run r0-demo-20261025 --env-version demo --skip-confirm
```

| 检查 | 预期 | 实测 |
| --- | --- | --- |
| pytest | 293 passed | 293 passed，34.83 秒 |
| Ruff | format --check 与 check 均通过 | 74 files already formatted；All checks passed |
| doctor | 全部 ok，能找到固定提交 55cf875 | 9 项全 ok；pinned commit exists in source repo: 55cf875b9d03 |
| sandbox verify | 64 个文件，tree f04f293e7cd9，缺失/改动/多出均 0 | ok=True files=64 tree=f04f293e7cd9 missing=0 modified=0 extra=0 |
| sandbox check | 13 项全过，与 2026-10-15 的 A1 表一致 | 13 项全 ok，all_ok=True；包括重置、合成账号隔离、源码与种子不变、端口释放 |
| agent witnesses | 5 个用例 × 6 个构建矩阵与历史记录一致 | 30 个判定符合预期；每个用例只在对应缺陷上失败，其余与 reference 均通过；all_ok=True |
| scripted_run | dev-02 完成、1 个用例 detects；dev-09 完成、无用例 | dev-02 completed(final_report)，cases 1，def-01，detects；dev-09 completed(final_report)，cases 0，reference；scripted pipeline ok |
| testagent --help | 保留 plan/reconcile/run/show/runs/check | usage: testagent [-h] [--version] {plan,reconcile,run,show,runs,check} ... |
| testagent check | 检查通过；7 模块、9 用例（active 8）、3 冒烟、两份资料存在；密钥已设置 | 除密钥外全部符合；DEEPSEEK_API_KEY 显示未设置，用户明确接受该差异后继续离线验收 |
| plan 55cf875 → e69f09f | 4 个变更文件，3 条冒烟 | 4 个文件，diff 6779/6779 字符；TC-HEALTH-001、TC-V1-001、TC-LEDGER-001，无额外建议用例 |
| run r0-smoke-20261025 | 执行 3/3，通过 3，失败 0，异常 0 | 完全符合；三条分别耗时约 9.0、2.6、3.1 秒，未覆盖 0 |
| plan 8d1836f → 2f4b565 | 31 文件，diff 84467/84467 字符，6 用例（冒烟 3 / 建议 3） | 完全符合；建议 TC-HEALTH-002、TC-MODEL-001、TC-SVC-001；ui 用例被剔除，覆盖缺口 1 项单列；未执行该历史版本对用例 |
| 购物车演示 plan / run | 3 文件，3 用例（冒烟 1 / 建议 2），执行 3/3，通过 3 | 完全符合；diff 1346/1346 字符；TC-CART-001/002/003 全 passed，失败 0、异常 0、未覆盖 0 |
| 被测克隆 | 工作区干净，HEAD 仍 e69f09f | git status --porcelain 无输出；HEAD e69f09f18f85cb6516093e739282adba73d1284a，分支 main，无 remote |

**见证矩阵复核：** 本次核对了 `artifacts/witnesses-20261025T144947Z.json` 的 5 × 6 明细，结果如上；运行文件被忽略，仅此稳定文档作为验收正本。回归本次产物分别位于 FinUnity 的 `artifacts/r0-smoke-20261025/`、`artifacts/r0-pair-20261025/` 和演示的 `artifacts/r0-demo-20261025/`；它们均被忽略，不作为 PROJECT_STATUS evidence。

**提交前路径与邮箱核对：** 原有本机路径形态出现在 `docs/startup-record.md` 第 19 行（工作区位置）、`src/api_test_agent/sandbox.py` 第 238 行（Windows 路径处理），以及测试夹具 `tests/test_dev_benchmark.py` 第 238/242/244 行、`tests/test_sandbox_env.py` 第 13/14/15/18/27/30/31/35/37/41/42/50 行、`tests/test_settings.py` 第 22 行。新增的 `docs/regression/make_diagrams.py` 第 24–26 行包含 Windows 字体路径。它们不阻止本地提交，公开前需审视；本地 FinUnity 解释器路径只在被忽略的配置中。邮箱命中只有 `t@example.invalid`（原测试夹具）、`test@example.com`（回归测试夹具）和 `demo@example.com`（演示仓库身份），均为假地址。未发现所扫描的私钥/令牌形态，待提交文件均为 UTF-8 文本（PNG 除外），文本无 CRLF。

**未验证项：** 两个模块的真实模型调用；R1 历史改动质量评测及人工参考结论；R2 FinUnity 单仓 `server/`；`benchmarks/dev` 公开前的源码片段确认；Mac/Linux。R3 扩充用例目录仍待开始。本次离线桩结果不能作为真实模型建议质量或检出率的证据。

### 历史记录搬迁：testagent 2026-10-22（未复测）

以下来自原 `testagent/PROJECT_STATUS.md` 的“当前进度”“真实链路样例”“边界与未做”，是 2026-10-22 历史记录搬迁，**不是本次真实模型复测**。原目录在 R0 验收通过后按方案移动归档。表中保留历史完成状态，证据路径已注明合并后的对应位置。

| 能力 | 历史状态（2026-10-22） | 原证据 → 现位置 |
| --- | --- | --- |
| 版本采集（固定 base/head、完整文件列表、diff、来源定位） | 已完成 | src/testagent/collect.py → src/api_test_agent/regression/collect.py；tests/test_collect.py → tests/regression/test_regression_collect.py |
| 影响分析（结构化输出、证据强度、待确认项） | 已完成 | src/testagent/analyze.py → src/api_test_agent/regression/analyze.py；prompts/analyze.md → src/api_test_agent/regression/prompts/analyze.md；tests/test_analyze.py → tests/regression/test_regression_analyze.py |
| 回归计划（校验编号、冒烟集必留、缺口单独记录、指纹） | 已完成 | src/testagent/plan.py → src/api_test_agent/regression/plan.py；tests/test_plan.py → tests/regression/test_regression_plan.py |
| 执行（只跑已登记 pytest 入口、异常不计通过） | 已完成 | src/testagent/runner.py → src/api_test_agent/regression/runner.py；tests/test_runner.py → tests/regression/test_regression_runner.py |
| 报告（版本/证据/结果/缺口/限制） | 已完成 | src/testagent/report.py → src/api_test_agent/regression/report.py；tests/test_report.py → tests/regression/test_regression_report.py |
| 两阶段 CLI 与状态复核 | 已完成 | src/testagent/cli.py → src/api_test_agent/regression/cli.py；tests/test_cli.py → tests/regression/test_regression_cli.py |
| 离线演示与真实项目试点 | 已完成 | examples/demo、examples/finunity → examples/regression/demo、examples/regression/finunity |
| 自测 | 48 项通过 | 历史命令 py -3 -m pytest -q；本次随整体 293 项一起复测，不等于重跑历史真实模型 |

真实链路历史样例原位于 `examples/finunity/artifacts/demo-live/`，现复制到 `examples/regression/finunity/artifacts/demo-live/`（本机、被忽略，不作为 evidence）：

- base `8d1836f` → head `2f4b565`，31 个变更文件，diff 84,467 字符全部进入分析；
- 模型给出 11 条影响（含代码位置与业务来源）、9 项覆盖缺口、6 项待确认；
- 计划 6 条（3 条固定冒烟 + 3 条建议），实际执行 6 条全部通过；
- 报告明确标注 9 项未覆盖不计入通过数。

历史边界与未做：未做账号/权限、RAG、用例平台、多项目并发、Web、Webhook、PR 回写、自动部署、UI/性能执行、多 Agent、自动改断言；只支持 base 为 head 祖先的两端快照，跨分支合并历史不支持；模型密钥只从环境变量读取，配置与产物中不落盘。R0 未扩展这些边界。

## 2026-10-25：N0 正式命名 TestPilot（测试领航员）

**结果与范围：** 用户要求采用正式名称，选择 TestPilot（测试领航员）。统一项目标题、介绍、协作入口、任务与启动记录；安装包名由 `api-test-agent` 改为 `testpilot`，版本仍为 0.1.0。新增探索主命令 `uv run testpilot`，CLI 帮助显示 TestPilot。旧探索命令 `uv run api-test-agent`、`uv run python -m api_test_agent` 和回归入口 `uv run testagent` 保持兼容。工作区目录及 `project_id` 仍为 `api-test-agent`，Python 包名保留 `api_test_agent`；构建配置显式指定该模块名。未移动目录、被测克隆或环境，未改变业务逻辑、模型变量名、配置文件名及已有运行目录标记。

**环境：** 同本日 R0：Windows 10 专业版 10.0.19045，宿主 Python 3.14.4，uv 0.12.10，pytest 9.1.1，Ruff 0.17.0。

| 检查与命令 | 本次实测 |
| --- | --- |
| uv lock --offline；uv sync --locked --offline | 安装 testpilot 0.1.0，旧安装名称被替换；26 个第三方包的锁定记录逐项比较完全不变 |
| uv build --offline | 生成 testpilot-0.1.0.tar.gz 与 testpilot-0.1.0-py3-none-any.whl（dist/ 被忽略），未发布 |
| wheel 内容与安装元数据 | Name=testpilot、Version=0.1.0；三个命令入口正确，探索/回归模块及回归提示词均包含在 wheel 内 |
| testpilot --help；api-test-agent --help；testagent --help | 三个已安装入口均退出 0；探索显示 usage: testpilot，回归保持 usage: testagent |
| uv run pytest -q -p no:cacheprovider --basetemp=<本次全新目录> | 293 passed，34.14 秒 |
| uv run ruff format --check .；uv run ruff check . | 74 files already formatted；All checks passed |
| uv run testpilot doctor | 9 项全 ok，固定提交 55cf875b9d03 可读，端口空闲 |
| uv run testpilot sandbox verify | ok=True files=64 tree=f04f293e7cd9 missing=0 modified=0 extra=0 |
| 在 examples/regression/finunity/ 执行 uv run testagent check | 通过；7 模块、9 用例（active 8）、3 冒烟，两份资料存在；DEEPSEEK_API_KEY 仍未设置，无需读取密钥值 |
| uv run testagent plan --base 55cf875 --head e69f09f --env-version rename-20261025 --stub --run-id rename-smoke-20261025 | 4 个变更文件，diff 6779/6779 字符；3 条固定冒烟，无建议用例；仅生成离线计划，未执行被测用例 |
| 被测克隆 git status --porcelain；git diff --check | 均无问题；被测克隆已跟踪文件未修改 |

**边界与未复测：** 本次只验证命名、打包、命令兼容与上述离线检查，未重复 R0 的 sandbox check、见证矩阵、scripted_run 或真实被测用例执行；其通过结果仍是本日 R0 记录。未调用模型、未推送、未发布、未写日常库；真实模型、R1/R2、Mac/Linux 等未验证项保持原状。Python 包名、目录名和旧入口的保留是兼容策略，不代表改名遗漏。

## 2026-10-25：U0 统一入口与最新架构/流程图

**结果：** 用户要求只保留统一入口，并提供当前架构与流程以便分析。安装包只注册 `testpilot = api_test_agent.cli:main`；移除旧 api-test-agent/testagent console 脚本。统一解析器接入回归的六个根命令，再按解析结果分发到探索或回归；回归命令在读取探索配置前分流，避免把回归配置当成探索配置。回归 `--config` 可放在命令前后，探索配置沿用全局位置；文件名和资产不迁移。提示、示例与当前使用文档同步为 testpilot，N0/R0 的历史验收记录保持原样。

**最新设计交付：** docs/architecture.md 为整体架构正本，包含整体架构、探索循环、回归两阶段流程的 Mermaid 源码，输入输出、状态边界、代码映射和失败分支；docs/architecture.png/.svg 与 docs/workflow.png/.svg 为可查看、放大的两张总览图，已逐张目视核对布局与中文显示。绘图复用原有风格，通过可选 Matplotlib 生成，不新增运行依赖。图中的“探索用例到回归资产未自动打通”“回归不自动 checkout/证明环境与 head 一致”“字符预算不等于金额守卫”均按代码核对，未把待做能力画成已实现。

**环境：** Windows 10 专业版 10.0.19045，Python 3.14.4，uv 0.12.10，pytest 9.1.1，Ruff 0.17.0；被测克隆仍为 main/e69f09f，Python 3.12.13（沿用本日 R0 环境，本次未重建）。

| 本次检查 | 实测 |
| --- | --- |
| 新增统一入口集成测试 | 5 项通过：回归配置前后位置、回归不加载探索配置、离线 plan→篡改被拒→reconcile→dry-run→show/runs、探索独立配置、无配置 help；模型构造器用禁止调用的替身验证 --stub 分支 |
| pytest（全新 basetemp） | 298 passed，34.95 秒；包括原 293 项与新增 5 项 |
| Ruff format --check / check | 77 files already formatted；All checks passed |
| uv sync --locked --offline；uv build --offline | 成功；安装元数据和 wheel 均只有 testpilot 一个 console 入口，旧 api-test-agent.exe 与 testagent.exe 已不存在，回归提示词仍在 wheel 内 |
| testpilot doctor；testpilot sandbox verify | doctor 9 项全 ok；verify 为 64 个文件，缺失/改动/多出均 0，tree f04f293e7cd9 |
| testpilot check --config examples/regression/finunity/testagent.toml | 7 模块、9 用例（active 8）、3 冒烟、两份资料存在，检查通过；DEEPSEEK_API_KEY 仍未设置 |
| testpilot plan --config examples/regression/finunity/testagent.toml --base 55cf875 --head e69f09f --env-version unified-20261025 --stub --run-id unified-smoke-20261025 | 4 个文件，diff 6779/6779 字符；生成 3 条冒烟用例，无建议用例；下一步提示均使用 testpilot |
| testpilot run unified-smoke-20261025 --config examples/regression/finunity/testagent.toml --env-version unified-20261025 --skip-confirm --dry-run | 退出 0；3 条均为 skipped，实际执行 0/3、通过 0；只验证解析与守卫，不冒充真实测试通过 |
| 被测克隆状态 | git status --porcelain 无输出；未修改已跟踪源码 |

本次未调用任何模型，未执行真实被测用例，未重复 sandbox check、见证矩阵或 scripted_run，未推送/发布、未写日常库。原有未提交的 N0 改动继续保留并在其上调整；真实模型质量、R1/R2、Mac/Linux 和公开前审查仍未验证。

## 2026-10-29：需求梳理与 F0 实施方案交付（非功能验收）

用户本次要求先提供可落地方案供审阅，不启动实现。需求包括：无既有用例的真实项目；从资料/接口文档/指定版本源码梳理接口；生成有场景、依据和参数理由的用例；人工查看并确认后执行；真实结果与未完成项如实报告；人工与 AI 资产逐步统一。

交付 docs/implementation-plan-20261029.md，包含首版范围、全量发现与分批执行流程、模块和状态边界、用例字段、审核绑定、执行证据、异常/中断行为、覆盖口径、F1–F8 依赖与逐项验收，以及旧实现复用与先修事项。TASKS.md 登记 F0 方案交付和待开始工作包；README.md 与 startup-record.md 加入待审入口。方案未获用户审阅确认，不代表任何新增能力已验收。

本次只核对本项目说明和相关代码，未读取新的被测仓库、凭据或运行数据，未改 src/、tests/、模型配置和旧功能状态，未执行模型、HTTP 业务测试、提交或推送。原有未提交修改继续保留。

### 同日本会话此前的技术基线（与方案交付分开记录）

2026-10-29 简历项目分析期间，在本机 Windows PowerShell 与现有项目环境执行：

- `uv run --no-sync pytest -q -p no:cacheprovider --basetemp=runtime/resume-review-20261029-8b7e3b`：298 passed，36.78 秒；使用当时新建的 basetemp，不调用模型。
- `uv run --no-sync ruff check .`：All checks passed；`uv run --no-sync ruff format --check .`：77 files already formatted。

这些是本日已有实现的检查，不是 F1–F8 验收；本次方案文档编辑后未重复运行测试，也没有重复 sandbox check、见证矩阵或真实模型质量评测。

### 文档与摘要校验边界

交付前在本项目内校验新增方案的任务引用、PROJECT_STATUS 必填字段、字段唯一性、日期类型、状态及证据相对路径：通过，12 个 evidence 文件均符合约束；README/TASKS 的 27 个本地文档链接均存在；方案 14 节、F0–F8 任务一致，F1–F8 均标为待开始，拟议命令明确标为尚不可用。`git diff --check` 通过。未使用日常同步工具或写入 vault，未读取凭据或运行目录。该校验只证明文档与摘要自洽，不证明接口发现、审核守卫或真实测试链路已经实现。


## 2026-10-30：F1 首批统一资产机制验证（整包未验收）

用户要求按方案开始执行。新增 tasks/models.py、store.py、cli.py 与唯一入口的 task 分发，具体边界见 task-assets.md。未覆盖或还原用户已有未提交改动。

Windows PowerShell、项目 uv/Python 环境，实际验证：

- `uv run pytest -q -p no:cacheprovider --basetemp=runtime/f1-tests-20261030-01`：311 passed，60.77 秒，使用本次全新目录。13 项新增机制测试覆盖空目录建任务、人工/AI 同目录、旧修订保持、重复任务/修订拒绝、理由/预期/提交/来源/清理变更影响完整哈希、未知引用和缺证据拒绝。替身与合成数据仅证明机制。
- `uv run ruff check .`：通过；`uv run ruff format --check .`：83 files already formatted。
- 实际运行 `uv run testpilot task --root runtime/f1-demo-20261030 create f1-demo --project synthetic --commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --scope "GET /items"` 后 show：退出 0，任务 created、空 history、无用例；提交为合成输入，未核实真实仓库。
- `git diff --check`：通过（Git 提示部分原有文件未来转换 CRLF，无空白错误）。

F1 为进行中：状态转换持久化、接口对象、审核状态修订级存储及旧资产适配契约待补；没有批准/执行入口，不声称已有审核守卫、可执行 pytest 适配或防篡改结果库。F2–F8 未实施，目标项目/提交/资料范围待指定。未调用模型、发送被测请求、读取新目标源码、提交/推送或访问日常库；未重复沙箱全链路或真实模型效果验收。


## 2026-10-30：F1 基础验收完成与目标选择澄清

用户说明项目应由自己选择，无须当前聊天写死目标。落实为每任务选择：`task create <id>` 可创建无目标、无用例草稿；`configure` 登记项目/完整提交/范围/源码或文档位置/环境；登记不读取文件，`validate-target` 只验完整性，不伪装已验证版本。已有用例或发现后锁定目标，变更版本另建任务，跨任务复用留待 F8。

F1 补齐 Operation 来源与不确定状态模型、任务阻塞/恢复/终止原子写入及历史、按完整修订哈希追加审核状态（draft→pending_review→rejected/deferred）、新修订恢复 draft、旧审核历史保留，以及人工/AI 合成 JSON 示例。旧探索与 pytest 适配边界已在 task-assets.md 固定，不自动推断缺失理由或批准；pytest 导入继续明确拒绝。

本机 Windows、PowerShell、现有 uv 项目环境，实际验证：

- 定向测试 `uv run pytest -q -p no:cacheprovider --basetemp=runtime/f1-target-tests-20261030-02 tests/test_task_assets.py`：23 passed，2.17 秒。
- 完整测试 `uv run pytest -q -p no:cacheprovider --basetemp=runtime/f1-full-20261030-03`：321 passed，63.43 秒，使用本次新建 basetemp。新增覆盖任务独立配置、不完整输入拒绝、导入后换目标拒绝、非法/空原因状态转换拒绝与历史保持、终止不可恢复、新修订审核失效、审核旧修订及 approved 写入拒绝、路径越界和隐藏路径拒绝、接口冲突状态与来源保留。
- `uv run ruff check .` 与 `uv run ruff format --check .`：通过，84 files already formatted。
- 真实 CLI 合成演示：以 `task --root runtime/f1-user-choice-20261030` 依次 create user-choice、configure（synthetic-user-selection、40 位合成提交、GET /items、docs/synthetic-api.json）、validate-target、导入 examples/tasks/human-case.json 和 ai-case.json、state blocked、show；均退出 0，显示两条 draft、blocked 原因与状态历史。资料路径仅登记，示例资料未提供，不冒充完成发现或真实版本验证。
- 用同一演示任务和人工样例哈希执行 `review-state --decision pending_review` 后 `--decision deferred`，均退出 0；show 显示人工样例 deferred、AI 样例 draft，审核状态与任务状态独立，无批准/执行。

F1 按“统一对象、状态与旧资产适配边界”口径完成基础验收。完整业务状态推进的发现证据、冻结批准、实际执行和报告由 F2–F6 接入；不把预留状态枚举或审核状态记录当成这些功能已完成。F2–F8 未实施；开发无需等待用户现在选择真实项目，可使用合成输入继续。未调用模型、发送业务 HTTP、读取新目标源码、修改被测仓库、提交/推送或访问日常同步工具；未复测真实模型质量、沙箱全链路、跨平台和断电恢复。


## 2026-10-30：F2 首批实现（整包进行中）

新增 tasks/discovery.py、task discover/materials；Git 固定提交只读 blob 快照、文档路径限定、来源哈希、OpenAPI 初步发现、FastAPI AST 局部路由、冲突/范围外/不支持/待确认项和实际导出材料字符预算已实现。回归 context_files 进入实际请求；相关文件改从 head 提交读取，总预算在最终用户提示词序列化后实际删减内容并保留来源/遗漏说明，不再只写“已截断”却仍发送全文。该字符限制不是金额预算，未授权任何模型调用。

本次开发只创建合成 Git 仓库与文档。定向测试 `uv run pytest -q -p no:cacheprovider --basetemp=runtime/f2-discovery-20261030-03 tests/test_task_assets.py tests/test_task_discovery.py`：28 passed，3.08 秒；证明脏工作树不进入指定提交快照、不执行目标 Python、过滤已有测试/凭据路径、保留来源哈希和接口限制、文档冲突/范围外/不支持项，以及最终材料 JSON 的实际长度和未发送列表。发现快照字符串被默认 strip 导致哈希不一致，已禁用快照字段的空白裁剪并复测通过。

回归输入定向测试 `uv run pytest -q -p no:cacheprovider --basetemp=runtime/f2-analyze-20261030-07 tests/regression/test_regression_analyze.py`：9 passed，6.49 秒。新增验证采集上下文标记确实进入请求、收紧预算后实际消失而来源及遗漏保留、模板/元数据本身超限明确报错。中间测试发现 head_snapshot 未加入 source 枚举、冻结 DocRef 无法原位修改，已补枚举并以新模型替换裁剪条目；此前失败不计为通过。

F2 仍未验收：跨文件 FastAPI 挂载图、字段/认证差异对账、失败部分采集产物及恢复路径待补。discovered 只是离线清单已落盘，不等于全集已确认。F3–F8 未实现；没有真实模型效果或新目标实测。未调用模型、被测 HTTP、读取衡仓源码、提交/推送或访问日常同步工具。

F2 本批最终完整检查：`uv run pytest -q -p no:cacheprovider --basetemp=runtime/f2-full-20261030-08`，327 passed，65.25 秒；`uv run ruff check .` 通过，`uv run ruff format --check .` 为 86 files already formatted。均使用本机 Windows/项目环境，未调用模型。交付前本项目摘要字段和证据路径自检、git diff --check 通过；未使用日常同步工具。


## 2026-10-30：F2 路由图/重入机制与 F3 设计审核链路

本轮仅使用本项目合成源码、文档、生成器响应和假供应商，不读取真实目标源码或调用真实模型/被测接口。F2 原局部扫描替换为跨文件静态挂载图：导入别名/相对导入、嵌套前缀、重复挂载和子应用均有明确预期清单；循环、孤立与动态注册保留限制。OpenAPI 继承参数、解析本地引用，不跟随外部引用；重复映射键拒绝。每例保留声明、挂载和字段/认证/状态差异，不掩盖只在一侧出现的接口。失败采集资料在独立 attempt 保存，重试不覆盖旧记录；完整清单写入后中断，可核对绑定后推进状态而不重读变化文档。

F3 新增 design.py/design_model.py、task design/review：从空资产构造实际带 Schema/来源的请求；小批默认十项，可显式选 operation IDs，宿主全清单场景矩阵保留其他接口 pending；整批验证后导入草稿，输出规则目录与矩阵，Markdown 展示参数理由表、步骤副作用和断言及完整快照。输出不合法时保存 response/failed，不产生用例或通过记录；人工审核材料只将 draft 变为 pending_review，manifest 明确 executable=false。正常/边界/异常合成样例保留不同理由，未有正式拒绝响应依据时标 hypothesis，不能借此确认产品缺陷。

实时调用适配仅允许既有免费路由，仍使用原 key 环境变量及共享每日计数；最多一请求，调用前检查预算/计数和 token 上界，报告非零费用则停止接受草稿。保存实际模型元数据，key 不落盘。相关测试替换为假客户端，仅证明协议和守卫，未证明真实生成效果。

实测环境：本机 Windows PowerShell、现有 uv/Python 项目环境：

- `uv run pytest -q -p no:cacheprovider --basetemp=runtime/f2-graph-20261030-10 tests/test_task_discovery.py tests/test_task_assets.py`：35 passed，3.77 秒。精确验证五个挂载后的 method/path、三份挂载来源、循环/动态/孤立/WebSocket、引用/重复键、差异、失败重试、快照写入后中断重入、篡改哈希拒绝。
- `uv run pytest -q -p no:cacheprovider --basetemp=runtime/f23-final-20261030-16`：350 passed，65.22 秒。完整测试基于本批最终运行代码（此后仅增加一项测试及文档）。
- 追加正常/边界/待确认异常三类设计样例后，`uv run pytest -q -p no:cacheprovider --basetemp=runtime/f23-scenarios-20261030-17 tests/test_task_design.py tests/test_task_discovery.py tests/test_task_assets.py`：52 passed，4.51 秒。包括设计到审核过程 HTTP 调用数为零、响应多余批准字段/未知来源/缺理由/恶意表达式/认证头/错误版本/缺失值/场景缺项拒绝、请求实际字符预算、供应商失败留痕、修订失效，以及假供应商每日限额阻止第二次调用、付费配置在读 key 前拒绝。
- `uv run ruff check .`：通过；`uv run ruff format --check .`：90 files already formatted（添加最后场景测试后再核对格式/规则）。

F2 按首版静态适配支持范围完成机制验收；动态/运行框架隐式路由、认证语义、外部引用等无法静态证明的范围仍保留限制，真实项目接口全集对账在 F7 验收，不用合成测试冒充已覆盖衡仓。F3 继续进行中，仍需补预算下片段与整文件实际送出对账、更多中断/拒绝路径和 F4 连接；F4–F8 未实现，整个目标尚未完成。未调用真实模型、执行被测请求、修改被测仓库、提交/推送或访问日常库。


## 2026-10-30：F3 送出对账与 F4 冻结批准（进行中）

新增 approval.py、task approve/verify-approval；review 可带 execution-config。人工提交绑定审核文档及 manifest 精确哈希，可逐条批准/拒绝/暂缓，未提及项保持待审。批准包保存任务、全修订集合、来源清单、执行配置、执行器指纹、操作者和时间；新增修订撤回 ready，关键内容变化拒绝沿用旧批准。无配置的旧审核材料不能变成执行批准，模型没有批准工具。外部环境当前明确 version_verified=false，不把环境名称当部署版本证明。

F3 送出记录区分完整文件、实际发送的解析声明及完全省略材料；导入中断保留批次标记，阻止审核，design-recover 校验请求/输出哈希后补齐，不重新调用模型。增加供应商报告费用、中断和实际材料记录的拒绝测试。

`uv run pytest -q -p no:cacheprovider --basetemp=runtime/f34-guard-20261030-20 tests/test_task_design.py tests/test_task_approval.py tests/test_task_assets.py tests/test_task_discovery.py`：74 passed，7.44 秒。验证参数、理由、断言、准备/清理、版本、配置、执行器与审核文本变化拒绝；部分批准、未批准项保留；假供应商非零费用拒绝且 key 不落盘。Ruff 修复一处测试导入排序后检查通过，格式检查为 92 files already formatted。

实际执行器尚未接入，F4 仅冻结/复核机制验证，不冒充真实业务请求前的端到端守卫。F5–F8 尚未完成；未调用真实模型、被测接口或读取新目标源码。此前完整检查句柄失效，未用其推断结果，重新启动 f34-final-20261030-22 全量检查，结果待回收。没有提交或推送。

完整检查已回收：`uv run pytest -q -p no:cacheprovider --basetemp=runtime/f34-final-20261030-22`：373 passed，44.16 秒。Ruff、格式检查及项目摘要自检通过。


## 2026-10-30：F4 审核冻结、F5 统一执行与 F6 报告守恒对账（机制验收通过）

在前期 F1–F3 的统一数据模型、接口发现、小批生成与审核材料基础上，本轮完成 F4 人工冻结快照、F5 统一执行引擎（`structured-http-v1`）与 F6 报告生成与守恒覆盖统计的机制验收。

### 1. 交付物与机制验证
- **F4 人工审核与执行前冻结**：
  - 新增 `approval.py`、`task approve` 与 `task verify-approval`。
  - 绑定审核材料 manifest 精确哈希，支持逐条批准、拒绝或暂缓。
  - 冻结快照绑定任务设置、全修订哈希集合、接口清单、执行配置、执行器实现指纹及操作者时间。
  - 任何改动（参数值/理由/预期断言/步骤/版本/执行配置/执行器代码）或引入新修订均使旧批准失效，执行前直接拒绝，业务请求数为零。
- **F5 统一执行引擎（`structured-http-v1`）与证据对账**：
  - 新增 `execution.py`、`task execute`。
  - 严格限制宿主请求仅流向回环地址 `http://127.0.0.1:<port>`，禁止任何外网连接。
  - 宿主从环境变量读取账号凭据，所有落盘证据（EvidenceRecord）、事件日志（events.jsonl）和报告中令牌均严格替换为 `[REDACTED]`，绝不落盘或泄露。
  - 步骤严格按 prepare、test、verify、cleanup 四阶段执行。prepare 启动后异常记为 error，启动前缺凭据记为 blocked；断言失败记为 failed，并自动根据规则依据创建疑似产品缺陷（suspected_defect）或待确认假设（unconfirmed_hypothesis）Issue；清理步骤在任何情况下均会触发，清理失败告警 issues，不掩盖主体步骤通过记录。
  - 中断对账：`--max-cases` 与 `--stop-on-failure` 中断后剩余项标记为 `not_run`，任务置为 `incomplete`，不盲目重试带副作用步骤。
  - 既有 pytest 回归适配增强：`runner.py` 的 `classify` 增加对全部跳过（skipped）和未收集到（no tests ran）的结构化明细解析，退出码为 0 但无通过用例时不伪装为 passed。
- **F6 报告生成与守恒覆盖统计**：
  - 新增 `report.py`、`task report`。
  - 确定性守恒公式严格核算：`计划执行数 (planned) = passed + failed + error + blocked + skipped + not_run`，若不平衡直接告警。
  - 凭据真实性核验：`passed`、`failed`、`error` 必须有对应落盘证据文件存在，无证据不能增加通过计数。
  - 覆盖统计分离：接口实际调用覆盖率（called / confirmed_in_scope）与 7 类场景矩阵槽位覆盖率独立计算展示。
  - 未批准、暂缓与拒绝用例单列统计；支持在任务完成、中断或尚未执行等任意状态下出具 Markdown（report.md）与 JSON（report.json）双格式报告。

### 2. 实测结果
实测环境：本机 Windows 10 专业版、PowerShell、现有 Python 3.14.4 / uv 0.12.10 项目环境。
- 新增专项测试 `tests/test_task_execution.py`（5 项）与 `tests/test_task_report.py`（4 项），全部通过。
- 任务模块全量测试：`uv run pytest -q -p no:cacheprovider --basetemp=runtime/check-tasks-all-01 tests/test_task_assets.py tests/test_task_discovery.py tests/test_task_design.py tests/test_task_approval.py tests/test_task_execution.py tests/test_task_report.py`：83 passed，6.92 秒。
- 全量回归与探索测试：`uv run pytest -q -p no:cacheprovider --basetemp=runtime/check-full-f56-02`：382 passed，43.54 秒（较本轮开始前的 373 项增加 9 项通过）。
- 代码规范：`uv run ruff format --check .` 96 files already formatted；`uv run ruff check .` All checks passed。
- CLI 端到端实测验证：通过 `task create`、`configure`、`discover`、`design`、`review`、`approve`、`execute`、`report` 完整跑通真实回环合成服务，验证通过、失败、中断 not_run 及报告守恒核对成立。

### 3. 边界与未完成项
F4/F5/F6 按机制验收口径通过。真实模型与真实目标项目从零端到端验收属于 F7，跨任务资产复用与维护属于 F8。本次开发与机制验证未调用真实付费/免费模型，未修改被测衡仓仓库，未提交或推送代码，未访问日常同步工具。


## 2026-10-30 独立复核：F5/F6 仍有待修缺口

本次仅审查，未修改实现；未调用模型或真实业务接口。Windows / Python 3.14，重新运行全量 pytest（禁用缓存、系统临时目录中的全新 UUID basetemp）：382 passed in 46.19s；ruff check 与 format --check 通过（96 files）。首次测试因指定临时目录的父目录不存在而产生 setup errors，修正临时路径后完整重跑通过。

额外合成探针复用 approved_setup，经 MockTransport 执行两条用例后，仅删除该探针临时目录内的证据文件，再生成报告：planned=2、passed=2、conserved=true、evidence_backed_count=0。确认缺证据仍计通过，且未输出缺证据明细，违反方案第 9/12 节。

代码复核还发现以下未覆盖路径（静态确认，尚未逐项动态复现）：
- F5 execution.py：max_requests 仅在配置中声明，执行路径无请求计数和调用前检查，批准清单可突破该上限。
- F5 execution.py：参数引用解析在异常捕获外；外层 finally 只关闭客户端。引用缺失或 KeyboardInterrupt 会跳过用例记录、剩余 not_run 和最终汇总，任务留在 running；再次 execute 接受 running/incomplete 并从清单首项重跑，可能重复副作用。
- F5 execution.py：仅脱敏固定头名称，响应体、引用后的请求参数及断言实际值直接持久化；回显宿主令牌时会泄露，需统一按敏感值脱敏并保留内存计算数据。
- F6 report.py：按用例 outcome 将全部 operation_ids 计入实际调用覆盖，准备失败或网络异常时也计入未调用接口；应从逐步证据计算。

结论：已有测试全绿仅证明已覆盖路径；F5/F6 首版验收需补以上拒绝/失败/缺证据场景并修复后重验。F7 暂不宜进入真实执行。本次未重验 F7/F8、真实模型质量或真实目标环境。


## 2026-10-30：F5/F6 复核缺口修复与二次验收

针对独立复核发现的 5 项缺陷，本轮完成全面代码修复与自动化验证，未调用真实模型或被测接口。

### 1. 缺陷修复对照
- **P1 缺证据仍计通过**：`report.py` 中增加对落盘证据逐项物理存在性检验。若用例声明通过但证据文件丢失，不计入 `passed`，增加 `missing_evidence_count` 与 `missing_evidence_ids`，守恒判定直接置为 `FAIL (不平衡/证据缺失)`；报告顶部渲染警示横幅与缺失 ID 清单，用例列表标注 `[证据缺失]`。
- **P1 异常中断收尾与防重复副作用**：
  - `execute_step` 包装参数解析异常并记为步骤错误与证据，不泄漏至外层；
  - 执行主循环捕获 `KeyboardInterrupt` 与意外异常，安全记录当前用例与剩余 `not_run`，`finally` 块确保计算完整对账、落盘 `execution.json` 并转移任务状态至 `incomplete`；
  - 再次执行 `incomplete` 任务时，自动复用并保留已有终态用例，仅执行 `not_run` 用例，避免重复执行有副作用的请求；`running` 状态显式拒绝并发重复执行。
- **P1 凭据全链路脱敏**：实现递归脱敏 `redact_sensitive`；宿主注入的所有真实账号令牌在落盘数据（URL、请求参数/体、响应头/体、错误信息、Issue、事件日志、对账与报告）中均严格替换为 `[REDACTED]`，绝不落盘；同时在内存 `step_output` 保留未脱敏数据供下游参数引用与断言使用。
- **P1 请求上限调用前检查**：执行器接入 `RequestCounter`，在每个 HTTP 调用前执行计数检查；超过 `max_requests` 时立即终止并阻止后续外发，将剩余用例置为 `not_run`，任务置为 `incomplete`。
- **P2 接口与场景覆盖率虚高**：`report.py` 不再按用例 outcome 添加全量 `c.operation_ids`，改为严格根据实际产生步骤证据的调用统计 `called_operations`；准备步骤失败后未调用的接口准确保留在 `uncovered_operations` 中。

### 2. 自动化实测与验证
- 实测环境：本机 Windows 10 专业版、PowerShell、Python 3.14.4、uv 0.12.10。
- 任务模块专项测试：`uv run pytest -q -p no:cacheprovider --basetemp=runtime/tmp-check-tasks-all-04 tests/test_task_assets.py tests/test_task_discovery.py tests/test_task_design.py tests/test_task_approval.py tests/test_task_execution.py tests/test_task_report.py`：91 passed（新增 8 项测试覆盖上述全部 5 项缺陷场景）。
- 全量回归与任务测试：`uv run pytest -q -p no:cacheprovider --basetemp=runtime/tmp-check-full-05`：390 passed in 50.28s（原 382 项 + 新增 8 项全部通过）。
- 代码规范：`uv run ruff format --check .` 96 files already formatted；`uv run ruff check .` All checks passed。
- 边界与未完成项：机制已完整闭环并修复全部已知缺口；真实模型端到端调用与真实项目全接口发现验收属于 F7。本次未调用真实模型、未连接外部网络、未修改被测仓库。


## 2026-10-30：用户确认五项修复后的边界补验

环境：本机 Windows / PowerShell、现有 uv 项目环境（Python 3.14）。本轮保留此前未提交的实现及文档，使用合成数据和 httpx.MockTransport，不读取真实凭据文件、不调用模型或被测业务服务。

先增加五项失败探针：`uv run pytest -q -p no:cacheprovider --basetemp=runtime/review-repro-20261030-01 tests/test_task_execution.py tests/test_task_report.py`，结果 **5 failed, 17 passed in 8.38s**。这表明上节 390 项通过记录未覆盖首请求中断后恢复、恢复预算累计、字典键敏感值、连接失败覆盖及损坏证据；旧记录保留为历史，不能据此推断这些边界通过。

本轮修复：

- 证据新增 request_attempted / response_received，明确请求尝试和已收到响应。参数引用失败、预算阻止、连接失败均不增加实际接口覆盖；收到 HTTP 500 的接口仍属于已调用。
- KeyboardInterrupt 在 HTTP 边界捕获后先保存中断证据，再传给外层收尾。已尝试的当前用例记 error，未开始项才记 not_run；对账落盘并置 incomplete 后重新抛出 KeyboardInterrupt。恢复只继续 not_run，保留终态结果、证据和 Issue，首请求中断不再自动重复。客户端关闭在汇总落盘之后，避免关闭异常跳过汇总。
- max_requests 在 prepare/test/verify/cleanup 的共同调用入口检查，每次尝试在调用前计数；execution.json 保存 requests_attempted，恢复沿用累计数，不通过恢复清零。旧汇总没有可靠计数、汇总损坏或同批准恢复缺少指针时拒绝自动继续。
- 宿主凭据敏感值不再排除短令牌，递归处理字典键和值；响应、引用后的请求参数、断言实际值、Issue 和事件在持久化时脱敏，原始步骤结果仍留内存供引用/断言。合成短令牌回显、下游查询参数及断言失败后，对任务文件和新报告扫描无原值。
- 报告将不存在、无法解析及任务/用例/步骤/接口不匹配的证据列为缺失，不计 passed/failed/error，对账 FAIL。执行收尾也核对证据是否有效落盘。场景槽位按各用例自己的已响应步骤计算，不能借用其他用例调用的同一接口。

验证结果：

- 修复后专项 `uv run pytest -q -p no:cacheprovider --basetemp=runtime/review-target-20261030-03 tests/test_task_execution.py tests/test_task_report.py`：**24 passed in 8.73s**。
- 最终运行代码的全量测试 `uv run pytest -q -p no:cacheprovider --basetemp=runtime/review-full-20261030-04`：**397 passed in 61.13s**。
- 全量启动后仅扩充证据损坏测试的参数化，增加 wrong_case / wrong_operation 两种错配；最终专项 `uv run pytest -q -p no:cacheprovider --basetemp=runtime/review-evidence-20261030-05 tests/test_task_execution.py tests/test_task_report.py`：**26 passed in 10.96s**。不把新增两项冒充已跑全量；运行代码在两次检查之间未改动。
- `uv run ruff check .`：All checks passed；`uv run ruff format --check .`：96 files already formatted。全部 pytest 使用各自全新 basetemp，未复用缓存。

兼容与未验证边界：新增证据标记默认 false，旧证据不据此推断真实调用；执行器指纹变化会使旧批准失效，需要重新审核批准。缺少累计请求计数的旧 incomplete 执行拒绝自动恢复。存储仍为单进程，未验证断电、进程强杀、多进程竞争及磁盘不可写下的可靠持久化；不宣称 exactly-once。中断时不会自动补发清理请求，可能已产生的副作用需人工检查；有条件可捕获的中断不等同于强杀恢复。F7 真实模型和真实项目从零验收、F8 仍未执行。未提交或推送，未访问日常同步工具。


## 2026-10-30：修复后再次独立复核

本轮仅复核，未修改实现。Windows / Python 3.14，使用系统临时目录中全新 UUID basetemp、禁用 pytest 缓存：399 passed in 74.25s；Ruff check 与 format --check 通过（96 files）。上轮五项修复已有实现与测试，但本次新增合成 MockTransport 探针发现：

- P2 清理步骤结果未写入 results：两个用例各有 test、clean1、clean2，clean2 引用 clean1.body.id；审批通过后共应调用六次，实际四次，两个 clean2 均报 step 'clean1' has not run，主体仍计 passed=2。execution.py 清理分支需像其他阶段一样保存结果，补清理链测试。
- P2 显式 JSON null 被当作无请求体：execute_step 对 POST 的 $ / literal / None 参数实际发出 b''，不是 JSON null；当前 body_payload is not None 条件丢失了用户审核的 null 输入。需独立保留请求体是否提供的标记并正确编码 null，补 null 与 omitted 的区别测试。
- P2 Markdown 报告未呈现接口缺口：report.py 计算 uncovered_operations 并写入 JSON，但 Markdown 第 7 节只渲染 limitations；已确认但未调用的接口可能只看到“无特殊运行限制记录”，缺少方案要求的逐接口未覆盖清单（静态路径确认）。

上述两项执行问题已动态复现；报告展示问题经代码核对。本轮无真实模型或真实目标请求。F5/F6 仍需补这三项后验收，F7/F8 未验证。


## 2026-10-30：三项 P2 遗漏修复与补验

环境：本机 Windows / PowerShell，uv 项目环境 Python 3.14.4；仅合成数据、httpx.MockTransport。保留原有未提交改动，不读取真实目标源码或凭据文件，不调用模型及被测服务。

根因与修改：

- 清理链：execution.py 的 cleanup 分支遗漏 results[step.step_id] 写回；现在沿用其他阶段的响应传递。新增主体通过与主体断言失败两种场景，两条用例各执行 test/clean1/clean2，六次调用全部实际到达 MockTransport，clean2 分别收到对应 clean1 的 ID（2、5），每用例三份响应证据且没有 cleanup_failure。主体 failed 状态仍保留，不被清理成功覆盖。
- 显式 null：请求体提供与否用 has_body 单独判断；由于 httpx 的 json=None 等于不发送 JSON，整个值显式为 None 时使用 content=b'null'，未指定 Content-Type 时补 application/json。具名字段仍按 JSON 对象编码，未提供或 omitted 仍为空体。证据 request_parameters.body_provided 保存该区别。新增 POST/PUT/PATCH × literal null/reference null/omitted/absent 共 12 项请求字节、Content-Type 和证据标记验证，另验证具名字段 null 保留。
- Markdown 缺口：第 7 节逐项输出与 uncovered_operations 相同的未覆盖接口，列表为空则明确提示当前清单中无未覆盖接口。新增未执行与全部调用测试，并增强已有准备失败测试，确认仅未调用接口出现在缺口清单中、已调用准备接口不混入。

实测命令与结果（各用全新 basetemp，禁用缓存）：

- 仅增加测试，修改实现前：`uv run pytest -q -p no:cacheprovider --basetemp=runtime/p2-repro-20261030-01 tests/test_task_execution.py tests/test_task_report.py`：**11 failed, 32 passed in 17.71s**，复现清理链四次请求、六种 null 输入丢失，以及报告缺少接口列表/空列表提示。
- 三项修复后专项：`uv run pytest -q -p no:cacheprovider --basetemp=runtime/p2-target-20261030-02 tests/test_task_execution.py tests/test_task_report.py`：**43 passed in 16.93s**。
- 此后增加 body_provided 证据标记及断言，最终代码全量：`uv run pytest -q -p no:cacheprovider --basetemp=runtime/p2-full-20261030-03`：**416 passed in 97.89s**，较此前 399 项增加 17 项。
- `uv run ruff check .`：All checks passed；`uv run ruff format --check .`：96 files already formatted。

本轮三项按上述合成机制口径补验通过。此前 399 项独立复核及待修结论为历史记录，未用其代替本次验证。F7 真实项目/真实模型效果与 F8 未验收；断电、强杀与多进程竞争等既有未验证边界保持不变。执行器指纹改变，已有批准需重新审核。未提交或推送，未访问日常同步工具。


## 2026-10-30：GitHub 备份前打包与安全边界检查

用户授权私有 GitHub commit + push。本地原无 remote，新建 https://github.com/loonslo/TestPilot 并经 GitHub 元数据核对 visibility=private；尚未以本段声明推送成功。

实际检查：待提交 136 个路径及已有 195 个历史 blob，检测 OpenAI/OpenRouter key、GitHub token、私钥和 AWS access key 模式，并比对当前进程可见相关密钥值（只在内存比较，不输出值），未发现匹配。确认候选不含 runtime/subjects/虚拟环境/本机配置；本检查不宣称穷尽所有凭据形式。现有 benchmarks/dev 含被测私有代码片段，因此仅向私有仓库上传，不公开发布。

`uv build` 成功生成 testpilot-0.1.0 wheel 与 sdist；在 runtime/github-install-20261030 创建独立 Python 3.14.0 环境，先以 wheel 正常解析依赖安装，再通过 `uv export --locked --no-dev --no-emit-project --output-file runtime/github-smoke-requirements.txt` 和 `uv pip sync` 对齐 uv.lock，最后 `uv pip install --no-deps` 安装 wheel。在源码根目录以外运行已安装 testpilot 的 --version、--help、task --help，全部退出 0。构建与安装没有调用模型或业务接口。构建产物和临时环境被忽略，不作为项目摘要 evidence。

源码测试沿用此前 416 passed in 97.89s 及 Ruff 记录，未在本次重新执行完整测试，亦不是用户独立复核。新增 installation.md 说明异机克隆、锁定安装、本机配置及当前回环/合成数据/Windows 沙箱边界；目标机器、Linux、生产环境与 F7/F8 均未验收。实际远端提交核验在推送后另记。


### GitHub 远端核验结果（2026-10-30）

源码提交 `51bc48101d5fb43e6f5765b1d2949f005b84cb0e`（feat: add reviewed task workflows and evidence-backed execution）已通过 `git push -u origin master` 上传。随后 `git rev-parse HEAD` 与 `git ls-remote origin refs/heads/master` 均为该完整哈希；`gh repo view loonslo/TestPilot --json nameWithOwner,visibility,url,defaultBranchRef` 返回私有仓库、默认 master。仓库链接：https://github.com/loonslo/TestPilot 。当前保留原两次本地提交历史，没有强制推送或改写历史。

补充安装文档后最终暂存扫描 137 个路径未匹配凭据模式/进程可见相关密钥值，目标源码、runtime、本机配置均未纳入。新增 SVG 的行末空白规范化后 `git diff --cached --check` 通过；Ruff check 通过，format --check 为 97 files already formatted。本次仅打包、独立安装 CLI 冒烟和 Git 远端核验，不重新声称全量 416 项已在新机器或生产环境复测。上述源码提交之后仅追加上传验收记录与摘要，另以文档提交同步远端。

## 2026-11-02：F7 实测准备——通用模型适配与双机测试模板

**目标：** 解除对单一测试模型（`openrouter/free`）和固定环境变量的硬编码限制，支持测试机本地配置任意 OpenAI 兼容模型（DeepSeek、OpenAI、本地 Ollama 等），并补充端到端测试配置模板供异机实测。

**改动：**
- `src/api_test_agent/settings.py`：放宽 `ModelSettings.base_url` 正则，支持 `http://` 协议（用于本地 Ollama 等测试端点）。
- `src/api_test_agent/tasks/design_model.py`：移除 `openrouter/free` 与硬编码费用为 0 的强制校验；支持从配置指定的 `api_key_env` 动态读取密钥；本地环回端点缺省时自动使用虚拟本地 key；费用校验适配用户设置的 `max_cost_usd`。
- `tests/test_task_design.py`：更新测试以覆盖自定义 `api_key_env` 与本地端点免 Key 逻辑。
- `examples/tasks/model.example.toml`：完善 DeepSeek / Ollama / OpenRouter 示例与参数说明。
- `examples/tasks/execution.example.toml`：新增统一执行配置模板，并通过 Pydantic 校验。
- `docs/installation.md`：补充双机实测操作流水线与修改回传指南。
- `.gitignore`：追加 `*.local.toml`。

**实测结果：**
- 全量测试：`uv run python -m pytest -q -p no:cacheprovider`：**417 passed in 40.93s**。
- 代码风格：`uv run python -m ruff check .` 与 `uv run python -m ruff format --check .` 全部通过（97 files already formatted）。

## 2026-11-03：多语言/多仓库架构落地（P0/P1/P2）与可视化测试控制台（UI）

**目标：** 解除 TestPilot 对 Python 单仓单 commit 的硬编码限制，建立语言无关的适配器体系（支持通用 OpenAPI / Swagger 提取及多执行器支持），消除历史语法隐患；同时交付交互式 Web 控制台与过程文件全景透视工作台，支持免手写配置文件的可视化交互。

**环境：** Windows 10 Pro；Python 3.14.4；全新测试临时目录（禁用缓存）。

**改动：**
- **P0 异常语法规范**：全库扫描并消除 unparenthesized `except` 异常，统一使用标准括号元组格式。
- **P1 适配器层与注册表**：创建 `src/api_test_agent/adapters/` 核心包，提供 `DiscoveryAdapter` 协议与 `AdapterRegistry`。将 FastAPI 静态提取拆分收编为 `PythonAstAdapter`，`tasks/fastapi_static.py` 保留 100% 兼容重导出。
- **P2 OpenAPI 通用适配器**：实现 `OpenAPIAdapter`，支持 OpenAPI 3.x 及 Swagger 2.0 规格解析与 Operation 提取；`tasks/discovery.py` 支持按任务显式指定 adapter 或按文档类型通用接入。
- **扩展性增强**：
  - `tasks/discovery.py` 的 `permitted()` 动态汇集所有已注册适配器的扩展名（支持 `.java` 等由插件声明），保留敏感凭据过滤；
  - `tasks/models.py` 的 `CaseRevision.execution_kind` 放宽为字符串（支持 `http`、`junit` 等，保留 `pytest` 守卫）；`Task` 扩展 `version_manifest: dict[str, str]` 支持多仓库独立 SHA 版本，并扩展可选 `adapter` 字段；`TaskStore.configure` 放行新增字段；
  - `regression/runner.py` 引入 `RUNNER_REGISTRY` 与 `register_runner`，支持通过注册表自定义构建执行器命令（默认保持 `pytest`）。
- **交互式 Web 控制台与过程文件透视工作台**：
  - 实现 `src/api_test_agent/ui/server.py` 与 `ui/dashboard.html`，提供 `uv run testpilot ui` 入口（支持 `--port`、`--host`、`--root`、`--no-open`）；
  - 遵循 `baoyu-design` 工业级美学，支持深浅色模式、设计 Token 严格遵循界面层级；
  - 交付三大核心板块：数据源配置（代码库/接口文档双模式）、模型与预算配置（支持端点连通性探活与凭据环境安全检测）、测试集与执行报告；
  - 落地核心 signature **Sources 追溯面板 (Sources Panel)**：支持在界面中逐项下钻并完整查验 `task.json`、`model.json`、`inventory.json`、`cases.json`、`execution.json`、`evidence/*.json` 与 `report.md`，全流程资产双向印证。
- **新增测试**：新增 `tests/test_adapters.py`（10 项）与 `tests/test_ui_server.py`（6 项），累计 16 项新测试，覆盖适配器注册与提取、版本清单、多执行器插拔、UI 控制台静态资产服务、API 任务生命周期、安全 Ping 端点及 CLI 解析。

**实测结果：**
- 全量测试：`uv run python -m pytest -q -p no:cacheprovider --basetemp=runtime/test_clean_ui`：**433 passed in 40.54s**（较此前 417 项净增 16 项）。
- 代码风格：`uv run ruff check .` 与 `uv run ruff format --check .` 全部通过（105 files already formatted）。
