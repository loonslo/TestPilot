---
project_id: api-test-agent
updated: 2026-10-15
status: waiting
overview: 用工具调用探索 REST API 的测试 Agent，用独立规则判对错；首个被测对象是衡仓后端固定提交 55cf875 的本机沙箱。沙箱、判定套件和开发基准（A0 到 A2）完成，A3 的 Agent 代码不需要 key 的部分完成；模型指定为 OpenRouter 的 openrouter/free，真实模型还没有运行过，等用户设置 API key。
progress: "2026-10-15 通过项目侧验收：沙箱 13 项检查通过；判定套件 18 条规则在参考构建上全部通过，五个缺陷各只违反预期的规则，九个灵敏度变异体都被标出（18 条规则里 15 条有失败过的构建，LED-08、LED-09、ISO-04 还没有）；十个任务已生成，Agent 读的文件经扫描无泄露措辞。A3 离线部分：模型适配器、预算与每日额度、四个工具、用例语言与重放、Agent 循环、CLI 已实现，245 项单元测试和 Ruff 通过，五个手写见证用例在六个构建上的重放矩阵符合预期（各自只在目标缺陷构建上失败），脚本化的端到端冒烟在真实沙箱上通过。未验证：没有向任何模型发过请求，不知道免费路由器选到的模型能否稳定产出合格工具调用，十个开发任务没有跑，没有任何检出率；openrouter/free 每次请求随机换模型且每天只有 50 次请求（买过 10 点额度后 1000 次），一轮十个任务最多要 120 次调用；开发集不是未见测试集；没有操作系统级断网。"
next: "等用户把 OpenRouter API key 设为用户环境变量 API_TEST_AGENT_MODEL_API_KEY 并说明每周小时数；之后运行 model status 与 model ping，再用 agent run 跑 dev-02，按真实输出调提示词和工具说明，最后跑十个开发任务（日额度不够就分几天）并在 G1 检查点再决定下一步。"
evidence:
  - README.md
  - TASKS.md
  - docs/startup-record.md
  - docs/acceptance.md
  - docs/rules-evidence.md
  - benchmarks/dev/README.md
---

# api-test-agent · 项目概览与进度

本页是本项目的概览与进度摘要。更新进度时先核对证据文件及实际验收；任务细节以 TASKS.md 和 docs/acceptance.md 为准。只根据有证据的变化修改状态与日期；未验证事项保留“待核实”，不把文件存在或 Git 改动当作完成。

2026-10-15 新建项目并完成 A0、A1、A2，以及 A3 中不需要 API key 的部分；项目现在等用户设置 key（状态 waiting）。范围、批准、预算和 OpenRouter 免费模型的约束在 docs/startup-record.md；验收命令、环境、结果和未验证项在 docs/acceptance.md；规则的出处和参考构建观察在 docs/rules-evidence.md；开发基准的缺陷、任务和命令在 benchmarks/dev/README.md。项目目录是新建的独立 Git 仓库，尚无提交。
