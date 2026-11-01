# 桌面聊天运行与恢复协议

状态：当前实现；最后核验：2026-11-01；Refs #293。
事实来源：[服务所有者](../../backend/app/chat_runs.py)、[HTTP 宿主](../../backend/app/main.py)、[Runtime](../../backend/app/agent_runtime/runtime.py)、[客户端](../../web/src/features/chat/useChat.ts)、[HTTP 回归](../../tests/python/test_chat_lifecycle.py)。决定见 [ADR-0035](../adr/0035-service-owned-desktop-chat-runs.md)。

## 接受与任务归属

`POST /api/roles/{roleId}/runs` 接受 `SendMessageInput`，返回 `{session, messages, run}`。`content`、`attachments`、`toolMemoryIds`、`skillIds` 沿原语义；新增可选 `requestId`，为 1–80 个字母、数字、下划线或连字符。网页发送 UUID，运行标识固定为 `run-${requestId}`；未提供时服务生成唯一标识。

数据库原子保存 run、用户消息和 assistant 占位行即为接受。任务从此由服务持有；是否读取 JSON 响应或 SSE 不影响它。规范化输入摘要绑定角色、附件与显式技能等字段；相同 ID 和相同请求返回原运行，ID 被另一角色或另一输入使用返回 409。恢复不需要再次提交原文。

接受后的完整视图暂时无法稳定读取时，POST 返回 `202 {snapshotPending: true, run}` 和 `X-Meido-Run-Id`，仍表示接受成功。客户端保留已接受身份，从原游标订阅或读取确切运行，不能恢复成待发送草稿。完整视图就绪时，通常返回 `200 {session, messages, run}`。

普通回复同角色已有运行或尚在收尾时返回 409。自然聊天补充输入先以 `queued` 持久接受，失效旧计划的未开始尾段，再等待前驱退出并取得原角色锁。排队也计入 owner 活动登记，Stop 或权限失效会收尾这条输入；它不取得旧工具的取消权。

`POST /api/roles/{roleId}/messages` 仍返回 SSE 和 `X-Meido-Run-Id`，内部使用同一接受边界与订阅。关闭该流仅解除订阅，包括尚未消费第一帧的情况。

兼容入口的慢订阅超出事件窗口时，服务把 SQLite 当前状态转换成旧客户端认识的接受、过程和最终消息事件，不要求旧客户端处理 `run_snapshot`。输出使用本连接序号；内部同时记录完整快照覆盖的运行游标，跳过 worker 读取期间已经包含的增量，避免重复追加。它不影响新 GET 订阅的运行级游标；最终回复超过环容量时也能送达，历史自然分段按持久 ID 恢复。

## 查询、投影与序号

- `GET /api/roles/{roleId}/session` 返回原有 `{session, messages}`，增加 `activeRun`（无活动运行时为 `null`）。拟人补充存在时返回该桌面来源最新的未收尾执行。
- `GET /api/roles/{roleId}/runs/{runId}` 返回 `{session, messages, run}`，始终按确切 ID 恢复。不存在、别的角色或渠道运行返回 404。
- `GET /api/roles/{roleId}/runs/{runId}/events?after=N` 订阅严格大于 N 的事件。省略 after、游标超前或已落后于保留窗口时，发送 `run_snapshot`，客户端重新读取上述确切运行接口，以数据库消息和新游标替换本地投影。这个操作不会调用模型或工具。

`run` / `activeRun` 的字段：

| 字段 | 含义 |
| --- | --- |
| `runId`, `roleId`, `sessionKey` | 固定归属 |
| `sequence` | 服务事件序号；与旧 `runtimeSequence` 不同，阶段也占序号 |
| `status` | queued / created / running / completed / failed / cancelled / max_turns |
| `phase` | 下面列出的真实阶段 |
| `stopping` | 此所有者是否已收到停止请求 |
| `tools` | 最近至多 32 个调用的安全状态快照，包含 call ID、名称、短意图、状态、有限 result 和 droppedUpdates |
| `process` | 从 SQLite 按需投影的既有过程；用于工具执行中尚无可见文本行的恢复，不在内存重复积累历史 |
| `error` | 公开失败说明或 null；持久失败回退不返回原始配置/异常详情 |

当前内容和有界公开思考来自现有消息行；历史工具过程来自持久记录。工具进行中没有可见文本行时，网页使用临时展示行承载过程，后续 durable message 替换它。`messages` 是正常会话历史，不是新 token 日志。

会话、确切运行及订阅准入的 SQLite 读取在独立 I/O worker 完成；事件环、运行身份和序号只在事件循环访问。完整投影在 worker 前后检查最新接受身份、活动/指定运行引用及其序号，发生变化就丢弃读取结果重读，避免旧内容配上新游标而跳过增量。连续三次变化返回 `503` 和 `Retry-After: 1`，网页延后重试，不暂停生产者。确切运行的这类 503 还包含已验证的 `X-Meido-Run-Id`，供丢失 POST 响应时确认原接受。空闲期间有新运行接受并结束也会使旧读取失效。缺口 SSE 仅发内存快照信号，不在订阅生成器内同步读数据库。

当前流思考按 `${message.id}:thinking` 与过程条目合并，后续 delta 继续扩展同一条。不同模型/工具轮的 group ID 保持独立，即使思考文字相同也分别显示。

Runtime 的数据库状态可能先结束，桌面任务仍在发送拟人尾段或清理资源；此时活动投影继续返回 `running`。只有全部收尾后才发布 `assistant_completed` / `assistant_failed` 或终态快照。客户端完成后可立即发送下一轮。

原 SSE 消息和工具事件名保留，增加 `assistant_phase {phase, runId, sequence}` 与 `run_snapshot`。客户端同时校验角色、会话、run 和序号；忽略重复与迟到事件。`assistant_message_committed` 只 upsert 已提交消息，不结束自然回复序列。

## 有界订阅

每运行事件环最多 128 个 envelope、UTF-8 合计 256 KiB；超大事件不进入环，订阅者通过缺口重新读取数据库。生产者不等待 SSE 写入，没有按订阅者增长的事件积压队列。

当前工具投影至多 32 个调用，每个 result 至多 16 KiB；工具本身的 progress callback 仍受[原进度预算](../handbook/tool-progress.md)约束。服务至多持有 128 个未收尾运行和 16 个已结束运行的短期投影；满额或正在退出时，新接受返回 503。旧投影淘汰后仍能按 runId 查询数据库终态。

中间快照可丢弃，最终消息与真实工具回执不可丢弃。订阅和恢复不会重复写消息、执行工具或将旧事件投给下一运行。

## Stop、退出和重启

`POST /api/roles/{roleId}/runs/{runId}/stop` 只停止指定运行，等待任务和清理后返回最新 `SessionResponse`。已结束运行的重复 Stop 只确认原终态；即使同角色已有下一条，也不取消下一条。用户点击 Stop 时接受响应尚未到达，网页仍用提前生成的确切 ID；404 只等待这次接受，不查询“当前运行”代替它。

Stop 前已经完成的工具结果保持真实回执。重复 Stop 和断开 Stop 请求的连接不会再次打断 cleanup。拟人排队取消会等待旧任务，停止未发送尾段；已发送前段保持持久记录。关闭自然聊天后，未开始的排队回复不再执行。

服务关闭先拒绝新接受，取消并等待全部桌面所有者，再关闭共享工具、模型与记忆资源。启动时现有 SessionStore 恢复逻辑把 `queued/created/running` 和未完成占位收敛为失败；保存原输入、已有内容和工具诊断，不重新调用模型或工具。

上述退出顺序由 `python -m backend.app.server` 在 Uvicorn 等待 HTTP/SSE 排空**之前**执行；`npm run dev:api`、新版 tracked `Start-Meido.ps1` 与 `Start-Dev.ps1`/`start-dev.mjs` 使用这个入口，包含 reload。自有开发 launcher 在 Windows 通过私有 stdin 请求退出，只停止自己的 API/Vite。原生 `python -m uvicorn backend.app.main:app` 仍可启动 ASGI，但不具备 pre-drain 回调，不能保证 SSE 保持连接时及时取消。旧本机 `.runtime` helper 不随源码自动更新，需按[运行手册](../handbook/local-runtime.md)选用新版入口。

## 真实阶段

| phase | 过程区文案 | 触发边界 |
| --- | --- | --- |
| queued | 等待上一条回复收尾 | 已接受，尚未进入角色锁 |
| preparing | 准备上下文 | 记忆/宿主准备及每次 ContextWindowProvider 准备 |
| summarizing | 整理历史 | 实际进入压缩、摘要和提交等待 |
| model | 模型回复中 | 最终预算已就绪，开始普通模型请求 |
| tool | 工具执行中 | Runtime 开始实际工具调用 |
| delivering | 发送回复中 | 回复内容/拟人分段提交 |
| finishing | 正在收尾 | Runtime 或宿主正在释放资源 |
| finished | 无活动提示 | 所有者已结束 |

阶段不是时间推测或百分比；摘要期间不显示工具仍在执行。模型与工具的真实速度不在此协议保证范围。
