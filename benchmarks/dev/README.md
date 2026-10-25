# benchmarks/dev

开发基准 dev-v1：调试 Agent、度量早期效果用的开发集。**它对开发者可见，不是没见过的测试集**，不能当泛化证据。冻结测试集（A4）以后放在被 Git 忽略的 `benchmarks/private/`。Agent 看不到本目录，只读 `benchmarks/public/`。

## 文件

| 文件 | 内容 |
| --- | --- |
| `defects.json` | 五个单点缺陷。每个是对参考副本的一处精确文本替换（要求恰好匹配一次），并写明它应当违反哪些规则 |
| `mutants.json` | 九个灵敏度变异体，不是 Agent 的任务：只用来证明判定套件在五个缺陷没碰到的规则上也会失败 |
| `manifest.json` | 五个任务范围（S-A 到 S-E）和十个任务：五个跑在缺陷构建上，五个跑在原样的参考构建上。S-E 的调用者含 `invalid_token`（宿主注入一个错误令牌），因为 ISO-04 要测无效令牌 |
| `witness_cases.json` | 五个手写的见证用例，每个缺陷一个：在参考构建上通过，只在它对应的缺陷构建上失败。证明用例语言能表达这些缺陷、重放能区分两个版本（`agent witnesses`）。不是 Agent 的产出 |

Agent 看到的任务文件在 `benchmarks/public/tasks/`，由 `tools/build_dev_tasks.py` 从 manifest 生成，不含构建信息；同一范围的两个任务除编号外内容完全相同。

## 五个缺陷

| 编号 | 缺陷 | 违反的规则 | 任务 |
| --- | --- | --- | --- |
| def-01 | 不同币种账户之间的转账被接受（去掉了币种比较） | LED-05 | dev-02 |
| def-02 | 汇总时所有币种的持仓都加进基准币种（跨币种混加） | LED-01 | dev-06 |
| def-03 | 已归档的流水仍计入账本汇总 | ARC-01 | dev-03 |
| def-04 | 只作为转入方的账户可以被归档（守卫漏了转入账户） | ARC-03 | dev-05 |
| def-05 | 归档时不检查记录属于谁（别的用户能归档你的记录） | ISO-01、ISO-02 | dev-08 |

## 九个变异体

| 编号 | 变异 | 违反的规则 |
| --- | --- | --- |
| mut-01 | 估值向下取整而不是四舍五入 | LED-03 |
| mut-02 | 月份过滤变成累计：包含之前所有月份 | LED-04 |
| mut-03 | 收入支出不取绝对值，负数金额被当作负值 | LED-04 |
| mut-04 | 归档不校验记录版本 | ARC-02 |
| mut-05 | 持仓可以引用别人的账户（同时也能引用已归档账户） | ARC-05、ISO-03 |
| mut-06 | 转给同一个账户被接受 | LED-06 |
| mut-07 | 汇总接受任意月份数字 | LED-07 |
| mut-08 | 被流水引用的持仓可以被归档 | ARC-04 |
| mut-09 | 收入流水被加进现金余额 | LED-02 |

五个缺陷和九个变异体合起来，让 18 条规则里的 15 条各有至少一个会失败的构建；LED-08、LED-09、ISO-04 还没有。

## 命令（项目根目录）

```powershell
uv run python -m api_test_agent benchmark build                  # 建五个缺陷构建，放在 runtime/builds/
uv run python -m api_test_agent benchmark verify                 # 参考构建全过；每个缺陷只违反自己的规则
uv run python -m api_test_agent benchmark build --set mutants
uv run python -m api_test_agent benchmark verify --set mutants
uv run python -m api_test_agent benchmark conformance --build def-03   # 单个构建的 18 条检查
uv run python tools/build_dev_tasks.py                           # 由 manifest 重新生成 benchmarks/public/tasks/
```

判定套件在 `src/api_test_agent/conformance.py`：每条规则一个确定性检查，只在宿主一侧运行。检查相对于它看到的状态写（差值、自己创建的 id），所以可以共用一个场景，并按 LED、ARC、ISO 的顺序运行。检查中途准备数据失败记为 error（规则没被判定），不记为违反。
