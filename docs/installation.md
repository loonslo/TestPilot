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

## 目标项目与本机配置

任务的项目路径、固定提交、允许读取的源码/文档范围、执行环境及账号映射需要在使用机器重新配置，流程见 [统一任务说明](task-assets.md)。仓库不带目标项目源码、运行证据或本机配置，不会自动迁移原机器的任务。需要迁移历史证据时应另行确定脱敏范围，不上传整个 runtime。

旧探索模块的本机配置由 `config.example.toml` 复制为被忽略的 `config.local.toml`；回归配置由 `examples/regression/finunity/testagent.example.toml` 复制为同目录被忽略的 `testagent.toml`。示例中的目标克隆需另行按授权获取。模型和业务账号凭据只从环境变量/本机安全存储提供，不写进共享文件；环境变量名见各模板。

现有探索沙箱仅支持 Windows；统一 HTTP 执行器仅接受 `http://127.0.0.1:<端口>` 且 synthetic_only=true，环境版本仍为 version_verified=false。这次上传没有解除这些约束，也没有验收 Linux 部署或直接连接真实生产数据/账号的能力。生产主机上的安装与允许执行哪些测试需要分别确定；当前测试仍只使用合成数据。

先生成小批用例、核对审核材料，再批准执行；更换机器/解释器/执行器可能使旧批准指纹失效，应在实际执行环境重新审核。模型调用仍沿用预算约束；付费 DeepSeek 调用需逐次批准。上述安装与帮助命令不会调用模型或发送业务 HTTP。

## 验证范围

本机源码测试 416 项及 Ruff 已通过，记录见 [acceptance.md](acceptance.md)，不是用户独立复核结论。本次额外构建 wheel/sdist，并将 wheel 安装到独立 Python 3.14 环境，依赖从 uv.lock 导出锁定；在源码根目录以外验证 --version、--help、task --help。新机器仍需按实际环境验收；GitHub 上传不代表 F7/F8 或生产环境验证已完成。
