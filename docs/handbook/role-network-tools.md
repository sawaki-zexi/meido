# 角色联网工具

状态：当前实现；最后核验：2026-10-29。
事实来源：`backend/app/network_tools.py`、`backend/app/network_extract.py`、
`tests/python/test_network_tools.py`、`tests/python/test_network_api.py`；Refs #140、#265、#269、#275。
上游对照：[Shiori web_search](https://github.com/YinFengWindy/Shiori-Agent/blob/d3acaed186b172a78a5892e0307e9affd89759f0/apps/backend/agent/tools/web_search.py)、
[web_fetch](https://github.com/YinFengWindy/Shiori-Agent/blob/d3acaed186b172a78a5892e0307e9affd89759f0/apps/backend/agent/tools/web_fetch.py)。

网络搜索和抓取默认随角色提供，不需要逐项配置 enabledTools。宿主联网开关、请求预算和真实来源限制仍生效。安装 requirements.txt 后启动后端，模型连接须支持结构化工具调用；在角色工具目录查看真实依赖状态。

调用示例：

```json
{"query":"今日产品发布","num_results":8,"livecrawl":"preferred","type":"deep"}
```

```json
{"url":"https://example.com","format":"markdown","timeout":30}
```

`web_search` 数量为 1–20、默认 8；`livecrawl` 为 fallback/preferred，默认 fallback；
`type` 为 auto/fast/deep，默认 auto。结果保留 `query`、`result`、`sources`、HTTP `status`
与 `truncated`；正常空结果返回 `results=[]`、`sources=[]`、`count=0`，不是错误。默认搜索端点是上游使用的 `https://mcp.exa.ai/mcp`，支持 JSON 和 SSE，
支持 LF、CRLF、CR 行分隔，完成对应 RPC 后主动关闭流。该公开服务的可用性、配额和未来收费由运营方决定，不能承诺永久免费或稳定。

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
端点变更应由宿主更新配置。HTTP 错误、错误协议和内容解析失败返回错误工具结果；
正常空结果不失败，含文本却没有可核验来源仍明确拒绝。

搜索访问的是宿主配置的固定服务，保留域名交给 HTTP 客户端和系统网络处理，遵循
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` 等 HTTPX 网络环境配置。
HTTPS 仍校验证书，服务地址仍禁止 URL 凭据、本地域名与非公共 IP 字面量。
服务域名的解析由宿主 DNS/代理负责，不进行网页抓取的预解析和 IP 固定；这允许
TUN/Fake-IP 环境正常访问公共 Exa 服务。该边界只适用于宿主选择的搜索服务，不适用于
模型传入的 URL、搜索结果链接或重定向；不能把不可信地址填入宿主搜索端点配置。

高级设置 `searchEnabled` 只控制按需工具发现（tool_search），不是网页搜索开关；
联网工具的宿主开关是 `MEIDO_NETWORK_ENABLED`。Shiori 同样默认调用无密钥 Exa 公共 MCP；
`exa_api` 和宿主自定义端点/凭据是 Meido 保留的兼容扩展，不是上游的额外供应商自动回退。

## 失败排查

两个工具均返回稳定 `error_code`、中文 `error`、操作提示 `hint` 和 `retryable`；搜索还返回
`provider`，HTTP 拒绝额外保留数字 `http_status`。`retryable` 仅表示可重试建议，不会自动重发。
错误结果不回显查询、输入 URL、上游响应正文、异常字符串或配置密钥；原工具调用参数仍按既有
会话审计规则保存在本地，错误诊断没有新增原始请求日志。

| error_code | 含义与下一步 |
| --- | --- |
| `http_error` | 401/403 检查凭据或访问权限，429 等待并检查配额，5xx 稍后重试；以 `http_status` 为准 |
| `dns_error` | 解析实际失败或没有地址；检查本机 DNS/网络 |
| `connect_error` / `transport_error` | 建连或后续传输失败；检查网络、证书与目标可达性，不据此断言某供应商宕机 |
| `timeout` / `cancelled` | 整体时限到达或主动停止；保留旧 `status=timed_out/cancelled` |
| `provider_error` | 有效 JSON-RPC/MCP 明确返回执行错误；检查服务、配额与配置 |
| `invalid_response` / `missing_sources` | JSON/协议/正文编码无效，或结果缺少可核验 URL；不能当作空结果 |
| `invalid_arguments` / `invalid_url` | 修正模型业务参数或宿主端点格式 |
| `network_disabled` / `unsafe_url` | 宿主未许可或目标触及地址边界；不能通过重试绕过 |
| `redirect_error` | 提供正确最终地址，搜索端点由宿主更新，不转发搜索凭据 |
| `response_too_large` / `unsupported_content` | 缩小来源范围或使用支持该文件类型的专用工具 |
| `parse_error` / `empty_content` | 正文解析失败、解析进程不可用或网页为空；换来源，持续发生则检查本机解析依赖 |

## 网页抓取与资源边界

`web_fetch` 的格式为 markdown（默认）、text、html；timeout 为 1–120 秒，默认 30。
返回原 URL、最终 URL、HTTP 状态、内容类型、格式、长度和正文。HTML text 会去除脚本、
样式等噪声；Markdown 保留链接。按响应声明编码或 BOM 解码，无法解码时明确失败。
没有分页协议；超过 50000 字符时标记截断并提示缩小来源范围。

两个工具同时约束线上下载和解压后内容，最大 5 MiB；支持 identity、gzip、deflate，
其他压缩格式、额外 gzip 成员、二进制和未知非文本类型明确失败。HTML 和搜索文档解析
使用短命进程，整体时限和取消会终止工作进程；默认搜索整体 25 秒，网页整体按 timeout。
`html2text>=2025.4.0`、`lxml>=6.0.0` 与固定上游一致，用于保留 Markdown 和 HTML 提取语义。

网页抓取仅允许 HTTP/HTTPS，禁止 URL 凭据、本地域名、私有/回环/保留/非公共目标。
每次 DNS 解析的所有地址都必须通过检查，每次跳转重新检查，最多 5 次；连接固定到已校验 IP，
Host 和 TLS SNI/证书校验保留原始主机名，禁用环境代理、netrc 和自动携带 cookie。
外部回合允许这两个只读网络工具，但继续要求角色 enabledTools 和宿主许可。

因此浏览器或搜索成功不代表 `web_fetch` 一定成功。网页域名若被本机 DNS 映射到
`198.18.0.0/15`，仍按非公共地址拒绝；需要在网络软件中为相应来源使用真实 DNS，
或在已有授权范围使用浏览器能力。不要关闭公共地址校验，也不要把错误当成网页正文。

验证命令（选与改动相关的用例，不要求重复全套）：
`python -m pytest tests/python/test_network_tools.py tests/python/test_network_api.py`。
2026-10-29 在隔离分支上以无凭据公开查询 `Shiori-Agent GitHub` 实测：原版在解析到
`198.18.1.3` 后返回 `unsafe_url`；修复版返回 HTTP 200 和真实来源。另以本地 HTTP 代理
夹具验证配置服务访问与重定向拒绝。没有修改正式实例、调用真实聊天模型或付费凭据。
2026-10-28 本地真实 HTTP 夹具验证分块 SSE/持续连接、成功空结果、429 与无效 JSON；
会话 API 夹具验证空结果和安全错误经工具回传后仍可继续正文，并保存在原会话。
已复现并修复：CR-only SSE 的完整 RPC 原先无法提前结束，持续连接最终超时。
这不证明用户当天两次旧搜索失败由同一缺陷引起；旧记录只有泛化错误，无法定位 DNS、HTTP
或协议阶段。10 月 28 日这轮没有使用用户密钥或真实搜索查询，也没有声称验证当时线上服务可用性。

历史记录：
2026-10-15 真实无凭据 smoke：HTTPS example.com 返回 200 和正文；匿名 Exa MCP 搜索
`example.com documentation domain`、num_results=1、type=fast 返回 200 和 IANA 来源。
该结果仅证明当次真实端点可用，测试不依赖在线服务。
