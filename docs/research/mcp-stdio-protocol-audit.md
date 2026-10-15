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
- 独立规范复核发现凭据库清理异常被吞后，DELETE 错误返回 204，且旧 key 失去重试归属。公开 HTTP 故障用例已复现并修复：删除先撤权与整树断连，再清理凭据；失败返回 503 并保留停用配置及持久 key 队列。替换旧 revision、先写后抛的新 key 均留账；重建 Host/SQLite 后的实际保存/删除重试可消费队列并清理，测试只使用内存 Vault。
- 独立规格复核发现响应等待超时没有包含 `_send().drain()`，取消通知也会被同一背压阻塞。真实 fixture 握手/目录后停止读取 stdin 并派生子进程，60000 个中文字符仍在参数字符预算内；旧实现请求超时及单次取消两秒后均 pending。请求现从写入起共享总超时，取消通知严格限 50ms 后进入整树清理；stdio 超时/单次和重复取消、公开 HTTP Stop/断连验证不再卡住，也不重放业务调用。
- 后续两轴复核继续穿透实际 SystemCredentialStore，发现其默认分片 best-effort 清理会先删 manifest 再吞异常。2501 字符真实适配器/内存 keyring 的公开 DELETE 再次复现 204 及 3 个敏感分片遗留。MCP 现显式消费 strict_cleanup 模式：写分片前先保存 manifest，删除时逐项确认不存在、最后才删 manifest；RuntimeError、仍存在 key 的 PasswordDeleteError、静默未删均返回失败。6 个纯内存 HTTP 用例证明部分删除、替换旧 revision、新分片写后抛的清理归属均可在重建适配器/Host/SQLite 后实际消费，最终记录删净；未访问真实 OS 凭据，其他服务默认模式未变。
- #168 的真实 FastMCP 依赖验收进一步复现 Windows 环境名大小写问题：允许表含 `SystemRoot`，实际枚举为 `SYSTEMROOT`，子进程导入 asyncio 的 Winsock 路径失败。最小环境现按大写名字匹配同一固定允许表；真实 stdio fixture 在握手前用 asyncio 打开并关闭 localhost 套接字，先因协议流关闭失败，修复后正常握手。仅保留固定系统变量，不继承其他宿主凭据；服务业务仍须独立角色 grant。
- [Linux CI 38794483224](https://github.com/sawaki-zexi/meido/actions/runs/38794483224) 发现 Windows 验收未覆盖的两个 POSIX 故障。WSL Python 3.12.7 隔离 venv 加载固定 5dc16d6 的 stdio 代码，原样重现父进程退出但子 PID 仍存活，以及 oversize 清理在 process.wait 5 秒超时、随后 transport 析构报 event loop closed。诊断捕获子进程 R 态、属于原进程组；失败后随即消失，没有把僵尸改判为停止。根因分别是 killpg 后只等待父进程、以及协议 reader 因超限退出后 stdout StreamReader 暂停读取而无法完成 EOF。清理现先把 reader 交接为固定块丢弃读取，等待 stdout/stderr EOF，并在既有 5 秒预算内确认整个进程组已不存在。新增子进程不继承任何 stdio 的真实 fixture 分支，保留父/子 PID 断言；Windows 与 Linux 分别运行，transport unraisable warning 升为错误，未调大预算或跳过用例。
- 此轮初次 Linux 全组与 Windows 验收共用 NTFS 工作树默认数据，10 项因主人知识 SQLite 的 disk I/O / unable to open database 错误失败；该次未计为通过，也未删除或修补默认数据库。按 Handbook 分别设置 `/tmp/meido-mcp159-linux-data`、`/tmp/meido-mcp159-linux-roles`、`/tmp/meido-mcp159-linux-knowledge` 后，使用 `/tmp/meido-mcp159-posix-20261016/bin/python`（仅该临时 venv 安装 requirements）运行全部 5 个 MCP 测试文件：67 passed；三种实际 POSIX 清理场景重复 10 轮共 30 次通过。Windows 相关 stdio/HTTP Stop 与撤权用例 27 passed。两端均把 PytestUnraisableExceptionWarning 作为错误；另有一条 FastAPI/Starlette 废弃提示，不影响进程断言。
