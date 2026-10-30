# 架构图

![模块与数据流](architecture.png)

![两阶段操作](two-phase.png)

两张图由 `make_diagrams.py` 生成（matplotlib + 微软雅黑），改结构后重跑即可：

```powershell
uv run --with matplotlib python docs/regression/make_diagrams.py
```

## 模块与数据流（Mermaid 源码）

程序位于 `src/api_test_agent/regression/`，提示词位于其中的 `prompts/`；测试在 `tests/regression/`，示例资产在 `examples/regression/`。两个模块共用被忽略的 `subjects/FinUnityServer` 源码克隆，探索模块固定在 55cf875，回归模块按 base/head 采集。命令入口保留 `uv run testpilot`；本次合并验收只用 `plan --stub`，不调用模型。

```mermaid
flowchart TD
    subgraph IN["输入：项目资产（Git 管理）"]
        CFG["项目配置 testagent.toml<br/>仓库 / 文档 / 套件 / 预算<br/>模型端点 · 密钥变量名"]
        AST["项目资产 assets/<br/>modules.yaml 模块映射<br/>cases.yaml · smoke.yaml"]
        DOC["业务资料 docs/<br/>核心流程 · 业务规则<br/>接口说明 MD / OpenAPI"]
        GIT["本地 Git 仓库<br/>base_sha ── head_sha<br/>两端固定"]
    end

    P["project<br/>加载配置、模块映射、用例目录、冒烟集<br/>只读；相对路径按配置文件解析"]
    C["collect<br/>git diff + 相关文件 + 业务文档<br/>排除凭据与运行目录；超预算如实记录"]
    A["analyze<br/>1 次模型调用 → 结构化影响<br/>证据强度 · 代码位置 · 待确认项"]
    PL["plan<br/>只认已登记编号 · 合并固定冒烟集<br/>缺口单列 · 计划指纹防篡改"]
    H{{"两阶段边界<br/>人核对 / 修改 plan.json 后显式确认"}}
    R["runner<br/>只跑用例目录登记的 pytest 入口<br/>异常 / 超时 ≠ 通过"]
    RP["report<br/>版本 · 证据 · 实际结果 · 覆盖缺口<br/>未执行不写成通过"]

    subgraph ART["产物 artifacts/&lt;run_id&gt;/"]
        A1["context.json<br/>两个 SHA、文件列表、diff"]
        A2["prompt.txt · analysis.json<br/>发送的输入 + 结构化输出"]
        A3["plan.json<br/>唯一可被执行的文件（含指纹）"]
        A4["run.json<br/>真实计数、断言、日志、耗时"]
        A5["report.md · log.txt<br/>报告 + 步骤日志"]
    end

    IN --> P
    P --> C
    C --> A
    A --> PL
    PL --> H
    H --> R
    R --> RP
    C -.-> A1
    A -.-> A2
    PL -.-> A3
    R -.-> A4
    RP -.-> A5
```

## 两阶段与守卫（Mermaid 源码）

```mermaid
flowchart LR
    C["collect<br/>固定两个 SHA"] --> A["analyze<br/>1 次模型调用"]
    A --> PL["plan<br/>校验编号 · 冒烟集 + 指纹"]
    PL --> H["人 确认<br/>查看 / 增删 / reconcile"]
    H --> R["run<br/>复核后执行"]
    R --> RP["report<br/>证据报告"]

    subgraph GUARD["拒绝条件"]
        G1["head 提交移动 → 重新 plan"]
        G2["计划被手工修改 → 指纹不匹配 → reconcile"]
        G3["用例入口漂移 → 更新目录后重新 plan"]
        G4["环境版本未提供 → 拒绝执行"]
        G5["非交互终端未加 --skip-confirm → 退出码 3"]
        G6["执行异常 → error，不计入通过数"]
    end

    subgraph STATE["任务状态（与测试结果分开）"]
        S1["planned 已生成计划"]
        S2["running 执行中"]
        S3["completed 已完成（≠ 测试全通过）"]
        S4["aborted 执行异常"]
        S5["incomplete 未完成"]
    end
```

## 读图要点

1. **一条竖线就是执行顺序**：`project → collect → analyze → plan →（人）→ runner → report`，没有分支、没有循环，也没有让模型自己决定下一步。
2. **每步右侧的产物是契约**：模块之间只通过结构化对象与文件引用交接，`plan.json` 是唯一可被执行的文件。
3. **粉色虚线是唯一的跨界点**：`plan` 之前不执行任何测试，`runner` 之前必须有人确认。
4. **灰色产物框说明证据链**：`context.json` 记录两个 SHA 与 diff，`run.json` 记录真实执行，`report.md` 只能从这两处取数。
