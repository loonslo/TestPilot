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
