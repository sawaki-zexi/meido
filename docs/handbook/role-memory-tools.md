# 对话中的显式记忆工具

状态：当前实现分支；最后核验：2026-10-15。Refs #142。
事实来源：`agent_runtime/memory_write_tools.py`、`agent_runtime/memory_tool.py`、
`memory_engine.py` 与 `tests/python/test_memory_write_tools.py`、`test_memory_tools_api.py`。

角色通过 `agentConfig.enabledTools` 授权 `memorize`、`forget_memory`；召回继续由既有
`memoryRecall.enabled` 和能力策略授权。全局记忆开关关闭后，手填任何工具名称都不能恢复权限。
三个工具均绑定当前角色与来源会话，只允许桌面主人或仍获授权的已配对主人私聊；群和访客不开放主人私有记忆。

```json
{"summary":"主人不喝咖啡","memory_kind":"preference","memory_domain":"relationship"}
```

`memorize` 支持 summary、memory_kind、memory_domain，以及 procedure 的 tool_requirement、steps。
类型允许 fact/profile/preference/event/procedure；留空使用当前 Meido 引擎的 fact 默认值，返回实际类型。
domain 可选 role_self/relationship/shared 或空。步骤只是保存的做事方式，不能授予工具权限。
summary 上限 2000 字符、steps 最多 50 条；返回稳定 item_id、实际 memory_kind、status 和 summary。
相同来源中的相同类型/摘要重试幂等，同一用户消息可以保存多条不同记忆。

来源由宿主从 `ToolContext.run_id` 对应的真实完成用户消息定位；校验 run、角色、session 与主人授权。
模型不能提交 role_id、session_key、messageIds、metadata 或 current_user_source_ref。
MemoryStore 保留 `role:{id}` canonical scope，evidence.refs 保存原始消息 ID；source_ref 为
`真实消息ID#memory-摘要hash`，可由消息工具按 # 片段前的真实 ID 定位。渠道来源 ID 保留其 thread 前缀，
重新取原文时仍需消息工具重新核验当前授权。事件时间默认使用来源用户消息时间。

```json
{"query":"最近做过的事","intent":"timeline","memory_kind":"event","time_filter":"recent_7d","limit":30}
```

`recall_memory` 保留原有 context/answer/interest/procedure，补 timeline，以及 memory_kind、memory_domain、
time_filter。数量为 1–200，保留原有默认 5；输出仍受 12000 字符预算约束，截断时明确标记。
支持上海时区的 today、yesterday、recent_3d/7d/30d、YYYY-MM-DD、YYYY-MM-DD~YYYY-MM-DD；
日期区间包含末日，非法或逆序范围明确返回 invalid_time_filter。timeline 必须提供时间过滤。
查询继续遵循现有 MemoryEngine 的相关性、类型配额与只读语义，不修改检索状态。

结果保留 records/text，增加同内容 items、memory_type、source_ref、cited_item_ids 和安全 trace。
procedure 返回已保存的 steps/tool_requirement；不回传内部 metadata、原始存储/admin 字段或其他会话资料。
有来源的记忆摘要仍不等于原始消息证据，精确引用需要继续通过消息工具核验原文。

```json
{"ids":["memory-..."]}
```

`forget_memory` 对当前角色 ID 标记 forgotten，不物理删除。为兼容上游，命中列表字段名保留
superseded_ids；另返 requested_ids/count、missing_ids/count、already_forgotten_ids、状态和 items。
跨角色 ID 与未知 ID 都仅报告 missing；重复失效明确报告 unchanged 和 already_forgotten_ids。
大批结果保留总数并对列表标记截断，保持 JSON 完整。每次修改复用宿主现有角色写锁、结构化记忆与
MEMORY.md/PENDING.md 同步回滚流程；后续正常召回不会返回已忘记的条目。

运行 `python -m pytest tests/python/test_memory_write_tools.py tests/python/test_memory_tools_api.py tests/python/test_memory_tool.py tests/python/test_memory_engine.py`。
公开 HTTP 测试用真实 SQLite/MemoryEngine 完成 memorize→recall→forget→无有效命中，
并核对每次工具结果先持久化再进入下一次模型请求、真实来源 ID 和文档同步。已配对私聊与解绑用真实
MultichannelStore 验证；不需要真实账号、外发或付费模型服务。
