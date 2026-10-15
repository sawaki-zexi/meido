# 通用 stdio MCP 协议核验

状态：当前调研；稳定接口已同步 [Reference](../reference/mcp-services.md) 与 [Handbook](../handbook/mcp-services.md)。

最后核验：2026-10-16。范围：#159 / T12 / C10。

## 一手协议与本地事实

- stdio 是宿主启动子进程、双向 UTF-8 JSON-RPC、每行一个消息；stdout 只能放协议，stderr 可作日志。通用客户端单独实现有界解码和错误，不改浏览器固定客户端。[官方 Transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- 首个请求为 initialize，协商版本/能力后发送 notifications/initialized；关闭 stdio 先关闭输入并等待，必要时终止进程；请求有总超时。[官方 Lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)
- tools/list 的 nextCursor 必须继续分页；工具 inputSchema 与可选 outputSchema/structuredContent 保留验证。annotations 来自 server，不构成宿主风险授权。tools/call 的协议 error 与 isError 工具结果分开处理。[官方 Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- 普通在途请求可发 notifications/cancelled，initialize 不能发送取消通知；接收方可以无法取消，宿主仍需清理自己启动的进程并忽略迟到响应。[官方 Cancellation](https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/cancellation)
- 官方 Python SDK 当前主线文档为 v2 Client/MCPServer；本环境未安装 mcp distribution，requirements 也未声明。此切片按 2025-06-18 协议实现独立有界客户端，不把未使用的 SDK 版本当作依赖或运行证据。[官方 SDK](https://github.com/modelcontextprotocol/python-sdk)、[客户端文档](https://py.sdk.modelcontextprotocol.io/clients/)
- 浏览器客户端固定 agent-browser 和 2024-11-05 握手，不能直接扩为通用宿主。其进程模块已有 Windows Job 所有权设施；本票复用该设施，POSIX 使用独立进程组，保留专用浏览器路径。[现有客户端](../../backend/app/browser_use/mcp.py)、[进程所有权](../../backend/app/browser_use/process.py)

## 沿用与适配

固定 Shiori 的添加会保存服务配置，后续启动重连；移除删除持久记录，动态工具为 mcp_<server>__<tool>。Meido 只让主人配置固定启动方式，模型使用 server ID；这是已批准 ADR0011 的权限适配。[固定上游 registry.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/registry.py)、[上游管理工具](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/manage_tools.py)、[ADR0011](../adr/0011-versioned-tool-activation.md)

已实现持久化明确连接意愿，按角色、线程和原发言者绑定代际隔离。配置保存不连接；add 成功后后续同一仍获准来源可以自动重新握手和发现，remove 清意愿。进程归当前运行，结束时释放；重启恢复连接意愿不代表重放任何 tools/call。目录 GET 只观察配置和已有状态，不启动程序。[真实 HTTP 用例](../../tests/python/test_mcp_http.py) 已证明同 run 搜索/执行、Host 与 SQLite 重建、旧版本移除拒绝、移除后重新添加的版本链；[来源用例](../../tests/python/test_mcp_origin.py) 已证明角色/线程/原绑定代际隔离、在途及初始握手撤权、移除/重复取消的真实子孙清理。

## 安全补验

- 声明、structuredContent 和文本不能在 JSON 转义之后才匹配原始密钥。含引号、反斜线、换行和 Unicode 的真实 env fixture 复现了旧检查漏洞；现在递归检查/清理原始节点，并覆盖文本内 JSON 转义形式，公开 HTTP 回归证明凭据不进入目录/模型结果/持久详情。
- 完整 namespaced provider function name 最多 64 字符，不能只检查远端 48 字符；超限拒绝整个目录，避免截断冲突。目录另有工具数/页数/总字节及 schema 深度预算。
- 远端退出或关闭 stdout 必须主动回收整树，不能只设置 pending 错误；健康目录随即报告失败，再 add 实际重新启动及握手。相同 run 重连通过完整声明版本 hash 比较，冻结 schema 的 tuple/list 规范化不会误判为远端变更。
- 离线引用限制不能消除 JSON Schema 的计算攻击：`^(a+)+$` 对 32 个 a 加 `!` 能同步卡住事件循环，本地 allOf 引用图也能指数膨胀。[隔离 HTTP 探针](../../tests/python/test_mcp_schema.py) 已实际复现同步实现超出外层 15 秒预算。实例验证现放入固定 `python -I` 子进程，传入经过检查的 schema/实例，使用空 Registry 和最小环境，2 秒超时后按整树所有权清理；输入正则、输出正则和引用图三个用例证明失败明确、宿主事件循环仍响应。
- watcher 一旦观察到原来源失效，就在当前 MCP run 内锁存撤权。公开平台开关 HTTP 用例先暂停、等待真实 peer 退出，再恢复平台并尝试旧 run 的 add，保证不复活。没有引入平台全局 epoch；两次复核之间的瞬时暂停/恢复不声称被捕获，新入站 run 仍须独立验证当前原绑定与连接意愿。
