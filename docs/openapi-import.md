# 服务文档接入

下载项目后执行 `uv sync --locked --python 3.14`。先按 README 运行内置合成示例，确认安装和发现功能可用；无需目标仓库或模型账号。

## 一步接入

```powershell
uv run testpilot task import-openapi recruit --project Recruit --commit "<完整40位提交ID>" --url "http://127.0.0.1:8080/v2/api-docs"
```

支持 Swagger v2、OpenAPI v3 JSON/YAML，以及 Springfox/Knife4j `/swagger-resources` 返回的 `[{name,url/location}, ...]`、Swagger 配置返回的 `{urls: [{name,url}, ...]}`。

传入文档或服务目录端点，不是 `/doc.html` 页面。没有自动浏览器登录或任意 HTML 爬取。提交是用户声明版本；文档始终标为 `unverified-document`。

## 认证与大型文档

复制 `examples/tasks/openapi-import.example.toml` 为 `openapi.local.toml`，按需编辑，无需改源码：

```toml
max_document_bytes = 50000000
max_total_bytes = 200000000
max_documents = 128
timeout_seconds = 30
total_timeout_seconds = 300
max_redirects = 5
fetch_refs = true
allowed_origins = ["http://127.0.0.1:8081"]

[header_env]
Authorization = "TESTPILOT_DOC_AUTH"
# Cookie = "TESTPILOT_DOC_COOKIE"
# X-API-Key = "TESTPILOT_DOC_API_KEY"
```

只在本机环境变量中提供完整认证头值，例如 `Bearer ` 前缀加令牌。不要把值填到 TOML、URL 或共享文件。配置只保存变量名；缺失变量在网络请求前报错。公开文档无需配置认证头。

```powershell
uv run testpilot task import-openapi recruit-auth --project Recruit --commit "<完整40位提交ID>" --url "http://127.0.0.1:8080/v2/api-docs" --openapi-config openapi.local.toml
```

默认单份20,000,000字节、累计100,000,000字节，最多64份文档。大小针对解压后的内容；不会截断 JSON/YAML。超限指出可调整的配置键，不会假装导入成功。默认单次网络等待15秒、导入时间预算120秒、重定向最多5次。

## 重定向、引用与聚合

入口同源的重定向和 `$ref` 默认下载；跨源需在 `allowed_origins` 显式登记协议、主机、端口。HTTPS 重定向不允许降到 HTTP。

认证头只发给最初入口的来源，即使列入允许清单也不会向另一来源转发；不复用服务端设置的 Cookie。多个服务需要不同凭据时分别创建任务，或从统一认证的聚合网关获取。

引用按最终 URL 解析，原文与来源保留在快照中。解析器仅查已冻结文件，不自行联网。循环引用、未授权来源或下载失败的引用仍显示 unresolved，详情见 inventory 的 limitations。聚合目录任一成员失败会中止，避免误报完整清单。重复引用按 URL 去重，数量、字节和时间预算约束整个导入。

## 失败后继续

首次 import-openapi 已创建任务但下载失败时，补好变量或修改配置后执行：

```powershell
uv run testpilot task discover recruit-auth --openapi-config openapi.local.toml
```

也可指定新的 `--openapi-url`。发现完成后目标锁定；换文档请创建新任务。已保存完整清单但状态推进中断时，相同输入会从快照恢复，不重新下载。

401/403 检查凭据；HTML 响应通常是登录页面；跨源拒绝检查允许清单；超限按提示调配置。

导入仅生成接口清单，不调用模型、不执行业务用例。模型设计仍需配置，用例执行仍需人工批准。UI 暂无这些配置表单，使用 CLI。
