# 在另一台机器安装 TestPilot

代码仓库为私有仓库 https://github.com/loonslo/TestPilot 。本次上传是源代码备份和跨环境安装入口；F7 真实项目/模型验收、F8 维护接入尚未完成。

## 从 GitHub 安装（Windows / PowerShell）

准备 Git、uv 和有该私有仓库读取权限的 GitHub 登录；不把访问令牌写进 URL 或配置文件。Python 由 uv 按项目要求准备。

```powershell
gh repo clone loonslo/TestPilot
Set-Location TestPilot
uv sync --locked --python 3.14
uv run testpilot --version
uv run testpilot --help
uv run testpilot task --help
```

GitHub CLI 仅用于私有仓库认证/克隆；已配置 Git 凭据管理器时也可使用 `git clone https://github.com/loonslo/TestPilot.git`。要固定复现某次版本，先 `git switch --detach <完整提交号>`，再执行 `uv sync --locked --python 3.14`。

安装后先运行 [README 内置合成示例](../README.md#1-下载后先跑通内置示例)，不需要模型 Key 或目标仓库。服务文档可通过 [一步导入命令](openapi-import.md) 接入，支持大小配置、认证头、重定向和聚合目录。

## 目标项目与本机配置

任务的项目路径、固定提交、允许读取的源码/文档范围、执行环境及账号映射需要在使用机器重新配置，流程见 [统一任务说明](task-assets.md)。仓库不带目标项目源码、运行证据或本机配置，不会自动迁移原机器的任务。需要迁移历史证据时应另行确定脱敏范围，不上传整个 runtime。

旧探索模块的本机配置由 `config.example.toml` 复制为被忽略的 `config.local.toml`；回归配置由 `examples/regression/finunity/testagent.example.toml` 复制为同目录被忽略的 `testagent.toml`。示例中的目标克隆需另行按授权获取。模型和业务账号凭据只从环境变量/本机安全存储提供，不写进共享文件；环境变量名见各模板。

现有探索沙箱仅支持 Windows；统一 HTTP 执行器仅接受 `http://127.0.0.1:<端口>` 且 synthetic_only=true，环境版本仍为 version_verified=false。这次上传没有解除这些约束，也没有验收 Linux 部署或直接连接真实生产数据/账号的能力。生产主机上的安装与允许执行哪些测试需要分别确定；当前测试仍只使用合成数据。

先生成小批用例、核对审核材料，再批准执行；更换机器/解释器/执行器可能使旧批准指纹失效，应在实际执行环境重新审核。模型调用仍沿用预算约束；付费 DeepSeek 调用需逐次批准。上述安装与帮助命令不会调用模型或发送业务 HTTP。

## 在另一台电脑实测自定义项目与模型（F7 演练）

### 1. 配置模型与凭据
从 `examples/tasks/model.example.toml` 复制为 `model.local.toml`（该文件已在 `.gitignore` 或不提交）：
```powershell
# 设置环境变量（以 DeepSeek 为例）
$env:DEEPSEEK_API_KEY = "sk-xxxxxxxx"

# 或设置 OpenRouter
$env:API_TEST_AGENT_MODEL_API_KEY = "sk-or-v1-xxxx"
```
修改 `model.local.toml` 中的 `base_url`、`model_id` 与 `api_key_env`（若使用本地 Ollama，填 `base_url = "http://127.0.0.1:11434/v1"` 且无需真实 key）。

### 2. 配置执行环境
从 `examples/tasks/execution.example.toml` 复制为 `execution.local.toml`：
- 填写被测服务在本地监听的环回地址（如 `http://127.0.0.1:8000`）；
- 填写被测项目的 40 位 Git Commit SHA（测试期间可填具体 commit，需满足 40 位十六进制字符）；
- 填写允许执行的 `allowed_operations` 列表。

### 3. 运行完整闭环命令
```powershell
# 1. 登记任务（指定被测项目文档或源码）
uv run testpilot task create real-test `
  --project "TargetProject" `
  --commit "0000000000000000000000000000000000000000" `
  --scope "GET /api/*" "POST /api/*"
uv run testpilot task configure real-test --document-paths "path/to/openapi.json"

# 2. 离线发现接口
uv run testpilot task discover real-test --materials-root "path/to/materials"

# 3. 驱动模型生成用例
uv run testpilot task design real-test --model-config model.local.toml

# 4. 导出审核材料
uv run testpilot task review real-test

# 5. 人工审核并在 decisions.json 记录批准决定后冻结
uv run testpilot task approve real-test decisions.json --execution-config execution.local.toml

# 6. 启动被测服务并执行
uv run testpilot task execute real-test

# 7. 导出报告与对账
uv run testpilot task report real-test
```

### 4. 发现问题并修改代码的回传
在另一台电脑上修改代码后：
```powershell
uv run python -m pytest -q -p no:cacheprovider
git commit -am "fix: ..."
git push origin master
```

## 验证范围

本机源码测试 416 项及 Ruff 已通过，记录见 [acceptance.md](acceptance.md)，不是用户独立复核结论。本次额外构建 wheel/sdist，并将 wheel 安装到独立 Python 3.14 环境，依赖从 uv.lock 导出锁定；在源码根目录以外验证 --version、--help、task --help。新机器仍需按实际环境验收；GitHub 上传不代表 F7/F8 或生产环境验证已完成。
