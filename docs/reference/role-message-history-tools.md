# 角色消息证据工具

状态：当前

最后核验：2026-10-15

事实来源：[工具实现](../../backend/app/agent_runtime/message_history_tools.py)、[公开工具测试](../../tests/python/test_message_history_tools.py)、[HTTP 对话测试](../../tests/python/test_message_history_tools_api.py)，对应 [Issue #141](https://github.com/sawaki-zexi/meido/issues/141)。

## 启用与范围

两工具默认关闭。角色配置 `agentConfig.enabledTools` 包含 `search_messages`、`fetch_messages` 后，后续运行才可调用。更新角色时保留完整的现有 `agentConfig`，避免覆盖其他能力配置。

宿主在运行创建时绑定角色、当前真实会话和主人身份；模型不能传入 `role_id`、`thread_id` 或 `turn_session_key`。桌面只接受本角色的 `role:<id>` 会话。已配对主人私聊可读本角色桌面及当前授权的主人私聊，配对前消息与配对前发起、配对后才完成的回复不进入该视图；每次执行重新检查绑定。访客私聊和所有群聊只读当前线程，即使主人在群中发言也不会并入私人历史。

只返回已完成的普通用户/角色文本。工具调用、工具结果、失败/生成中回复、主动提醒和内部 metadata 不作为消息证据；外部角色回复还需 `deliveryStatus=sent`。来源引用仅用于定位，不能授予读取权限。账号移除后，旧外部工具实例拒绝继续读取。

## search_messages

必填 `query`（最长 500 字符）；可选 `role=user|assistant`、`session_key`、`limit=1..50`（默认 10）、`offset>=0`（默认 0）。空白查询返回空列表。`session_key` 只能缩小宿主范围。

查询按空白拆词，不区分大小写，任一词命中即返回；优先匹配更多词的消息，再按时间从新到旧排序。返回 `count`、`matched_count`、`limit`、`offset`、`has_more`、`next_offset` 和 `messages`。下一页使用 `next_offset`；历史在分页期间变化时应重新搜索。

每条结果包含稳定 `id/source_ref`、`session_key`、原始 `seq`、`role`、ISO 时间 `timestamp`、`matched_terms` 和 `preview`。预览最多前 50 行及 10000 字符，并用 `truncated`、`preview_line_count`、`total_line_count` 标明限制；需要作为原话证据时继续取原文。

## fetch_messages

接受 `ids`、单个 `source_ref`、`source_refs`、记忆工具的 `evidence`（读取其中的 `refs/source_ref`），可混用并去重，最多 50 个不同引用。兼容 JSON 字符串/数组以及引用末尾 `#...` 片段；未知或不允许的 ID 不返回原文。无引用时返回空结果。

`context=0..10` 指每个命中前后各取几条获准文本，默认 0。邻接限原始 transcript，不把时间交错的其他私聊当作邻居；绑定边界外的消息也不会成为邻居。结果包含 `count`、精确命中数 `matched_count` 和 `messages`。每条包含 `id/source_ref`、`session_key`、原始 `seq`、`role`、`timestamp`、未经改写的 `content` 和 `in_source_ref`。不返回内部 metadata、工具参数或本地附件路径。

## 上游来源与兼容差异

行为参考 [Shiori message_lookup.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/message_lookup.py) 和 [搜索实现](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/session/store/search.py)。MIT 许可保留于 [third_party/shiori-agent/LICENSE](../../third_party/shiori-agent/LICENSE)。

Meido 使用既有独立线程和绑定边界，额外严格拒绝未声明参数，并限制引用数量/字符串大小及预览字符数；查询采用当前消息库的文本匹配，未新增 FTS 索引。工具通过现有 AgentTool/HTTP 对话和持久审计接入，不维护第二份历史存储。
