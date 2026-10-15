# 预授权 stdio MCP 服务

状态：当前

最后核验：2026-10-16

事实来源：[配置](../../backend/app/mcp_config.py)、[主人 HTTP API](../../backend/app/mcp_api.py)、[运行宿主](../../backend/app/mcp_runtime.py)、[stdio 协议](../../backend/app/mcp_stdio.py)、[真实 HTTP 测试](../../tests/python/test_mcp_http.py)、[分片凭据 HTTP 测试](../../tests/python/test_mcp_credentials.py)、[来源测试](../../tests/python/test_mcp_origin.py)、[恶意 schema 测试](../../tests/python/test_mcp_schema.py)。对应 [Issue #159](https://github.com/sawaki-zexi/meido/issues/159)、[ADR-0011](../adr/0011-versioned-tool-activation.md)。

## 主人配置 API

`GET /api/mcp/servers` 返回 `{servers: [...]}`。`POST /api/mcp/servers` 创建配置并返回 201，`PUT /api/mcp/servers/{serverId}` 更新并返回 200，二者响应为 `{server: ...}`；`DELETE /api/mcp/servers/{serverId}` 幂等删除并返回 204。目录 GET、角色工具预览和保存配置均不启动 MCP 进程。

写入字段：

| 字段 | 约束与含义 |
| --- | --- |
| `serverId` | `[a-z][a-z0-9_]{0,23}`；更新不能更名 |
| `name` | 1–100 字符的显示名称 |
| `command` | 1–32 个固定 argv；首项为存在的绝对可执行文件，不经过 shell；单项最多 8192 字符、合计 32768 字符 |
| `cwd` | 存在的绝对目录 |
| `env` | 最多 64 个变量，值写系统 CredentialStore；缺省/null 保留已有变量；已有值留空保留；`{}` 清除全部；新变量必须有值 |
| `roleIds` | 最多 100 个存在的角色 ID；仅这些角色获得服务 grant |
| `enabled` | 默认 true；关闭撤销当前配置权限 |
| `risk` | `read_only` / `mutating` / `external`，默认 external；远端 annotations 不改变它 |
| `timeoutSeconds` | 1–120，默认 30；协议请求预算，同时受运行器总预算约束 |
| `maxOutputChars` | 1024–200000，默认 20000；超限文本截断并记录 `truncated` |
| `enableDiscovery` | 默认 true；这次主人保存操作为所选角色启用 tool_search 和三个管理工具，保留其他角色配置 |

公开配置保留 command/cwd，凭据应填写在 env。响应只公开 `envKeys`，不返回 env 值；另有不可变 `revision`、持久连接意愿数 `connectionCount`、当前运行进程数 `activeProcesses` 和安全错误集合 `errors`。配置请求最多 128 KiB；验证错误不会回显输入。新建重复 ID 返回 409，更新不存在/ID 不符返回 404，配置无效返回 422，凭据库或存储不可写返回 503。

成功保存生成新配置代际、清理该服务所有连接意愿并终止旧进程。env 凭据以配置代际寻址；SQLite 不保存 env 值。写入前先持久记录新凭据 key 的清理归属，发布配置时把旧 revision 的 key 放入待清理队列；失败写入及旧凭据清理失败均可跨重启重试。清理失败返回 503，已变更的配置停用、连接意愿清空且旧进程关闭，目录 errors 说明仍需重试。后续相同 ID 的保存/删除实际消费队列，不会把旧凭据默认为已删除。

DELETE 在访问凭据库前先停用配置、清空意愿并关闭进程。凭据未清理完时保留停用配置及队列，返回安全的 503，重复 DELETE 会继续清理；全部成功后才移除配置并返回 204。只有显式保存或删除会重试队列，目录 GET 不读写凭据。子进程只继承 PATH、Windows 系统路径、临时目录、语言及用户目录这些基础变量，再叠加主人指定 env，不继承模型/API 密钥等其他环境变量。

MCP 对系统 CredentialStore 显式启用 `strict_cleanup=True`。长凭据写入先保存分片 manifest，再写分片；中途失败时 manifest 与 SQLite 的主 key 清理归属仍能定位所有已写入或结果不明的分片。严格删除逐项确认分片不存在后才删 manifest，不能把 PasswordDeleteError 或静默未删除当作成功；部分删除后重新创建适配器仍可重试。其他服务的适配器默认行为保持不变。

## 模型工具与授权

`mcp_add({server_id})`、`mcp_remove({server_id})`、`mcp_list({})` 均为 deferred；先经 `tool_search` 加载。模型不能提供 argv、cwd、env、角色或来源。MCP 服务只向主人桌面或平台实际已绑定主人来源开放；访客及非主人群发言没有服务 grant。

运行开始冻结当前角色的服务配置、代际与 ServiceGrant，以及角色/session/真实来源身份。`mcp_add` 只连接这份集合中的服务：initialize → initialized → 全部分页 tools/list → 验证目录 → 暂存带父版本的远端候选。管理结果持久化后提交不可变目录修订；下一次 provider 请求可用 tool_search 激活 `mcp_<serverId>__<remoteName>` 并在同 run 真实 tools/call。同一 provider 回复不能抢先使用新目录。风险、角色与外部范围全部继承原 grant。

`mcp_list` 仅观察，返回 serverId/name/risk、connectionDesired、status、error 及真实发现工具的名称/loaded。status 为 connected、disconnected、failed 或 revoked。退出、坏流或 stdout 关闭会自动回收进程并报告失败；再次 add 重新握手。同 run 重连要求完整远端声明版本一致，变化必须等新 run。

成功 add 保存按角色、线程及原始绑定代际隔离的连接意愿。每个 run 创建自己的 stdio 进程，结束时释放；相同仍获准来源的后续 run、宿主和 SQLite 重建可以自动握手恢复候选。它只重做握手与发现，从不重放 tools/call。其他角色/线程及解绑重绑后的来源不能继承意愿。

remove 先清当前来源的意愿并关闭整棵进程树，再暂存目录移除修订。已存在的历史版本保持不变，但旧版本调用立即被执行授权拒绝。重复 remove 幂等；同一冻结 grant 可以再次 add 并产生新的版本链。配置删除/改变、角色删除、发现权限关闭和原来源失效会取消当前工作。watchdog 一旦实际观察到来源失效，本 run 锁存撤权；平台恢复不会复活旧 run。新入站 run 会重新验证平台与原绑定。没有全局平台启停 epoch，也不保证捕获两个检查之间完成的暂停/恢复。

## 协议与资源边界

仅协商 MCP 2025-06-18 stdio 工具协议，UTF-8 每行 JSON-RPC。最多 16 页、128 工具、512 KiB 目录；消息/未换行缓冲最多 4 MiB；未知请求/通知/迟到响应合计最多 64。工具远端名为 1–48 个字母/数字/下划线/横线，最终 namespace 最多 64 字符，不截断或产生碰撞。重复名/游标、坏 schema、未知协议版本等导致明确失败并回收整树。stderr 只排空，不进入日志或模型。

inputSchema 和 outputSchema 必须描述对象，仅支持 2020-12（缺省）或 draft-07。最多 3000 节点、32 层；拒绝 `$id`、动态/递归引用及非 `#/$defs/` 引用。空离线 Registry 禁止远端引用加载。完整实例验证在固定 `python -I`、无凭据的独立进程内运行，2 秒超时后强制回收；灾难性正则和指数引用图不能阻塞主事件循环、Stop 或撤权检查。实例不符合 inputSchema 不产生 tools/call；outputSchema 不符为错误结果。

远端结果最多 64 个 content 块。structuredContent 必须是对象；文本及结构化内容有界保留，已知配置凭据在字符串节点和 JSON 转义形式中清理；带凭据的工具声明不能入目录。图像接受 PNG/JPEG/WebP/GIF 的有效 Base64，最多 4 张、每张解码最多 1 MiB；解码后包含已配置凭据字节时拒绝，其他图像按既有运行器规则短暂提供给模型。resource/resource_link/audio 只作为数据返回，不跟随 URL。远端 JSON-RPC error、工具 isError、格式错误及宿主超时分别处理，错误不回显远端异常或启动参数。

每个请求的 timeoutSeconds 从写入开始计时，覆盖 write/drain 与响应等待，远端不读 stdin 也不能绕过预算。普通请求取消可尝试发送 notifications/cancelled，最多等待 50ms；失败、超时或再次取消都继续关闭，initialize 不发送该通知。Windows 使用 Job 所有权，POSIX 使用独立进程组；取消、重复 Stop、移除及坏流都等待进程树清理。取消/断连后的业务事务可能结果未知，宿主不会自动重试。浏览器固定客户端与 28 个工具维持独立实现。
