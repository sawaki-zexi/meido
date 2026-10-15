# 角色联网工具

状态：当前实现；最后核验：2026-10-15。
事实来源：`backend/app/network_tools.py`、`backend/app/network_extract.py`、
`tests/python/test_network_tools.py`、`tests/python/test_network_api.py`；Refs #140。

安装根目录 `requirements.txt` 后启动后端。角色默认没有联网工具；通过现有角色 API
的 `agentConfig.enabledTools` 显式加入 `web_search`、`web_fetch`，保留该配置中其他已授权工具。
模型连接必须支持结构化工具调用。角色设置中的工具目录由对应能力配置功能提供。

调用示例：

```json
{"query":"今日产品发布","num_results":8,"livecrawl":"preferred","type":"deep"}
```

```json
{"url":"https://example.com","format":"markdown","timeout":30}
```

`web_search` 数量为 1–20、默认 8；`livecrawl` 为 fallback/preferred，默认 fallback；
`type` 为 auto/fast/deep，默认 auto。结果保留 `query`、`result`、`sources`、HTTP `status`
与 `truncated`。默认搜索端点是上游使用的 `https://mcp.exa.ai/mcp`，支持 JSON 和 SSE，
完成对应 RPC 后主动关闭流。该公开服务的可用性、配额和未来收费由运营方决定，不能承诺永久免费或稳定。

宿主可在后端启动前配置以下环境变量；修改后重启后端。模型不能提交这些字段：

| 环境变量 | 默认值 / 含义 |
| --- | --- |
| `MEIDO_NETWORK_ENABLED` | true；设为 false/0/off 后，角色 enabledTools 也不能绕过 |
| `MEIDO_WEB_SEARCH_PROVIDER` | exa_mcp；可改为 exa_api 使用 Exa REST |
| `MEIDO_WEB_SEARCH_ENDPOINT` | 留空使用该 provider 默认端点；exa_api 为 https://api.exa.ai/search |
| `MEIDO_WEB_SEARCH_HEADERS` | JSON 对象，例如 `{"x-api-key":"宿主密钥"}`；仅用于搜索 |
| `MEIDO_WEB_SEARCH_PARAMETERS` | JSON 对象，加入 provider 的额外查询参数，例如 category；模型业务参数仍覆盖同名默认参数 |

宿主搜索密钥不进入工具 schema、配置 repr 或请求审计参数；错误响应不回显服务端正文，
成功响应若包含已配置凭据也会替换为 `[REDACTED]`。搜索 POST 不跟随重定向，避免转发凭据；
端点变更应由宿主更新配置。缺凭据、HTTP 错误、错误协议和空结果均返回错误工具结果。

`web_fetch` 的格式为 markdown（默认）、text、html；timeout 为 1–120 秒，默认 30。
返回原 URL、最终 URL、HTTP 状态、内容类型、格式、长度和正文。HTML text 会去除脚本、
样式等噪声；Markdown 保留链接。按响应声明编码或 BOM 解码，无法解码时明确失败。
没有分页协议；超过 50000 字符时标记截断并提示缩小来源范围。

两个工具同时约束线上下载和解压后内容，最大 5 MiB；支持 identity、gzip、deflate，
其他压缩格式、额外 gzip 成员、二进制和未知非文本类型明确失败。HTML 和搜索文档解析
使用短命进程，整体时限和取消会终止工作进程；默认搜索整体 25 秒，网页整体按 timeout。
`html2text>=2025.4.0`、`lxml>=6.0.0` 与固定上游一致，用于保留 Markdown 和 HTML 提取语义。

网络仅允许 HTTP/HTTPS，禁止 URL 凭据、本地域名、私有/回环/保留/非公共目标。
每次 DNS 解析的所有地址都必须通过检查，每次跳转重新检查，最多 5 次；连接固定到已校验 IP，
Host 和 TLS SNI/证书校验保留原始主机名，禁用环境代理、netrc 和自动携带 cookie。
外部回合允许这两个只读网络工具，但继续要求角色 enabledTools 和宿主许可。

验证命令：`python -m pytest tests/python/test_network_tools.py tests/python/test_network_api.py`。
2026-10-15 真实无凭据 smoke：HTTPS example.com 返回 200 和正文；匿名 Exa MCP 搜索
`example.com documentation domain`、num_results=1、type=fast 返回 200 和 IANA 来源。
该结果仅证明当次真实端点可用，测试不依赖在线服务。
