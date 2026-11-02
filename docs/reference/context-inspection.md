# 当前保存上下文接口

状态：当前；最后核验：2026-11-02。手动压缩扩展见[上下文与记忆契约](context-memory.md)。
事实来源：[后端](../../backend/app/context_inspection.py)、[共享预算](../../backend/app/agent_runtime/context_provider.py)、[前端生命周期](../../web/src/features/chat/useChatContextStatus.ts)、[API 测试](../../tests/python/test_context_inspection.py)。

`GET /api/roles/{role_id}/context-status` 不接受草稿、客户端提示、工具声明或其他会话身份。不创建会话或运行；会话键由服务端固定为该角色桌面会话。角色不存在返回 HTTP 404。

`canCompact` 按实际完整前缀判断。圆环点击与 `/compact` 调用 `POST /api/roles/{role_id}/context-compact`，返回 `compacted`、最新用量与原因，不持久聊天消息或推进情绪。语义消费者和周期优化不使检查自动忙碌；宿主 Markdown 整理与当前回合会使检查忙碌。

| 字段 | 含义 |
| --- | --- |
| `roleId`, `sessionKey` | 本次读取所属角色和保存会话 |
| `status` | `ready`、`busy` 或 `unavailable` |
| `reason` | 忙碌、失败、模型缺失、目录过期或容量未知的说明；正常时为空 |
| `inputTokens` | 保存输入的计量；不可用时为 `null` |
| `source` | `actual` 完全匹配的供应商实测；`anchor_delta` 可复用锚点的增量估算；`local` 本地估算 |
| `configurationId`, `model` | 当前实际角色聊天模型 |
| `capacity` | 配置的模型上下文容量；未知时 `null` |
| `inputLimit` | 容量扣除回复预留和安全余量；未知容量时 `null` |
| `outputReserve`, `safetyMargin` | 当前回复预留与安全余量 |
| `compactTrigger`, `compactTarget` | 同正常请求预算算法的自动压缩阈值与目标；未知容量时 `null` |

忙碌或不可用状态不携带可误用的旧数值。`ready` 仍可能容量未知，此时不计算百分比。圆环分子仅为输入量，分母为容量；输出预留不加入分子。读取跨越角色、模型、设置、历史或工作窗口变化时丢弃结果，允许重试。无效工作摘要在只读投影中回退到完整原文，不提交失效水位。

前端取消过期请求、合并同时读取，并在回复完成后的忙碌交接最多延迟重试一次。若维护更久，复用聊天已有的空闲会话刷新事件（当前每 10 秒）继续检查 busy；ready 状态不随此刷新额外读取。悬停／聚焦圆环打开简短提示或返回窗口时也会更新，不提供额外重查按钮。前端在 busy 时仅保留同角色、会话、模型的最近有效用量，并标明上次用量；身份变化清除，未知容量不填充圆环。历史消息中的 `contextUsage` 继续作为当时请求的事实保存，但不再驱动输入框圆环。响应使用 `Cache-Control: no-store`。
