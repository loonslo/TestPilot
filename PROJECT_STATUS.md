---
project_id: api-test-agent
updated: 2026-10-25
status: waiting
overview: "测试 Agent，两个模块：探索模块用工具调用探索 REST API，由独立规则判对错；回归模块按两次发布之间的差异给出回归建议，并执行已登记的接口用例。被测对象是衡仓后端 FinUnityServer，源码克隆在被忽略的 subjects/ 里，探索基准固定在提交 55cf875。探索模块还没有在真实模型上运行过。"
progress: "2026-10-25 R0：testagent 并入为回归模块 api_test_agent.regression，293 项测试与 Ruff 两项通过；doctor 9 项、sandbox verify 64/0/0/0 与 sandbox check 13 项通过；witnesses 的 5×6 矩阵符合预期，scripted_run 的 dev-02 完成并提交 1 个 detects 用例、dev-09 完成且无用例；testagent check 与两组 plan --stub 通过，FinUnity 冒烟和购物车演示各执行 3/3 全通过；没有调用任何模型。check 显示 DEEPSEEK_API_KEY 未设置，用户当日明确接受差异并批准继续离线合并。历史记录：2026-10-15 探索模块 A0–A2 与 A3 离线部分通过项目侧验收；testagent 2026-10-22 的自测与真实链路试点为历史记录搬迁，未复测，详见 docs/acceptance.md。"
next: "探索模块：等用户设置 API_TEST_AGENT_MODEL_API_KEY 并说明每周小时数，之后跑 model status、model ping 和十个开发任务（A3）。回归模块：R1 需要用户提供 5–10 个历史版本对的人工参考结论，并逐次批准真实 DeepSeek 调用（每次 ≤ ¥1，R 系列合计 ≤ ¥30）；真实调用前须在运行进程中配置 DEEPSEEK_API_KEY；R2 等单仓 server/ 有了导入后的真实提交再做。"
evidence:
  - README.md
  - TASKS.md
  - docs/startup-record.md
  - docs/acceptance.md
  - docs/rules-evidence.md
  - benchmarks/dev/README.md
  - docs/regression/README.md
  - docs/regression/architecture.md
---

# api-test-agent · 项目概览与进度

本页是本项目的概览与进度摘要。更新进度时先核对证据文件及实际验收；任务细节以 TASKS.md 和 docs/acceptance.md 为准。只根据有证据的变化修改状态与日期；未验证事项保留“待核实”，不把文件存在或 Git 改动当作完成。

2026-10-15 新建项目并完成 A0、A1、A2，以及 A3 中不需要 API key 的部分（历史记录）。范围、批准、预算和 OpenRouter 免费模型的约束在 docs/startup-record.md；验收命令、环境、结果和未验证项在 docs/acceptance.md；规则的出处和参考构建观察在 docs/rules-evidence.md；开发基准的缺陷、任务和命令在 benchmarks/dev/README.md。

2026-10-25 完成 R0：原 testagent 作为回归模块并入，程序、提示词、测试、示例与说明分别位于 `src/api_test_agent/regression/`、`tests/regression/`、`examples/regression/` 与 `docs/regression/`。两个模块共用被忽略的 `subjects/FinUnityServer` 克隆，各自保持配置、运行时与用例边界；探索模块的原有代码、测试与工具未改动。本次 293 项测试与两个模块的离线链路通过，未调用模型；密钥可见性差异已获用户明确接受，详细对照与历史记录搬迁见 docs/acceptance.md。

项目跟进状态为 waiting：等待探索模块的真实模型条件、回归 R1 的人工参考结论与逐次模型调用批准，以及 R2 的单仓历史条件；不表示真实模型质量评测或后续工作已验收。此次仅做本地基线与合并提交，不推送。PROJECT_STATUS 已在本项目手工核对 frontmatter、状态、日期和 evidence 路径，未调用日常同步工具或写入 vault。
