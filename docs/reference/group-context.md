# 群资料查询与维护

状态：当前（#166 实现，待汇入总 PR #162）。最后核验：2026-10-16。

事实来源：[群工具与本地资料](../../backend/app/channels/group_tools.py)、[共用来源校验](../../backend/app/channels/group_access.py)、[冻结实际入站来源](../../backend/app/channels/source_authority.py)、[旁听事务](../../backend/app/multichannel.py)、[公开工具测试](../../tests/python/channels/test_group_tools.py)、[Runtime 取消测试](../../tests/python/channels/test_group_runtime_cancellation.py)、[HTTP/provider 往返](../../tests/python/test_group_context_api.py)。对应规格 C09，固定 Shiori `4f266aeaf42fcd611362e2456417b91f659341cf` 的三个群工具。

| 工具与参数 | 行为 |
| --- | --- |
| `lookup_group_context`：`name` | 按群名部分匹配，忽略大小写；不读原始聊天。省略名字和 ID 时列出全部群。 |
| `group_thread_id` | 从候选选中的群线程 ID。只能读取当前角色的群；不能与名字或非第一页同时使用。 |
| `page` | 从 1 开始，默认 1。每页最多 20 项，返回准确 `total`、`page`、`nextPage` 和 `candidates`。目录按线程 ID 稳定排序，不受最近会话的 500 条上限影响。 |
| `update_group_context`：`group_thread_id` | 必填的目标群 ID；私聊和其他角色群均拒绝。 |
| `group_note`、`summary` | 至少提供一个字符串；提供字段整篇替换，省略保留，空串清空。仅提供 `summary` 时更新摘要时间。 |
| `set_group_listening`：`channel`、`chat_id`、`enabled` | `enabled` 必须是布尔值。在群中省略目标即当前群；仅支持当前角色已有 NapCat QQ 账号的群。 |

读取和更新均返回 `groupThreadId/channel/name/groupNote/summary/summaryUpdatedAt`，尚无保存内容的字段为空串。三个工具都拒绝 schema 外参数，包括自报角色或主人身份。工具不返回聊天或旁听原文，修改不改变原文。

| 实际运行来源 | 群笔记与摘要 | 旁听设置 |
| --- | --- | --- |
| 主人桌面 | 当前角色保存的全部群；目标账号离线或删除后仍可读写历史资料 | 当前角色可配置的 NapCat 群；桌面提供工具仍遵循既有可用渠道门禁 |
| 仍有效配对的主人私聊 | 当前角色保存的全部群 | 当前角色可配置的 NapCat 群 |
| 已准入主人群回合 | 当前角色保存的全部群；结构化笔记不共享原始聊天 | 仅当前群 |
| 访客私聊或群回合 | 无权使用 | 无权使用 |

有保存的群或可用渠道账号时自动提供本地查询和维护工具；空白新角色不增加无用工具。网页目录对本地资料声明 `ready`，实际模型 schema 与目录一致。账号离线只保留主人桌面的本地资料能力，不赋予离线外部回合新的来源授权，也不增加网络发送或账号启停权限。

四个群/成员工具共用宿主访问检查。渠道来源固定到实际 run 对应的入站消息、线程、发送者、账号创建代际和主人绑定；`is_owner` 构造标记不能单独授权。每次调用复核平台、账号、会话范围、屏蔽、绑定、角色存活和取消。解绑重绑、同 ID 账号重建后，旧回合与首次捕获旧消息均拒绝。成员工具保持[原当前线程范围](member-lookup.md)，不会把其群来源限制误套在主人群笔记上。

笔记更新和旁听开关将阻塞数据库写入放在线程中；运行中派生工具的取消令牌直接读取父回合的取消状态，无需等待监测轮询。SQLite `BEGIN IMMEDIATE` 取得写入位置后重新检查许可和取消；即使取消和解锁都发生在 50ms 监测间隔内，失效调用也不能提交。取消时工具等待写线程实际退出；连续取消也不会提前返回并留下继续写入的线程。令牌只向下读取父状态，取消子令牌不修改父令牌或兄弟令牌，独立后台任务仍使用独立令牌。省略字段在同一写事务读取并保留，避免两个并发 patch 相互覆盖。旁听继续通过既有存储入口，变更时将 `operator=role` 的 toggle 与设置一起提交。

验收使用真实 SQLite、工具执行、实际来源服务、`run_agent_loop` 父回合取消及 HTTP/SSE 对话；模型是传输夹具。覆盖 500 条混合线程外的旧群、20 项分页、字段保留/清空、离线 schema/result 往返、主人与访客范围、每种撤权、锁等待后拒绝、连续取消等待线程退出和并发 patch。未连接真实渠道账号或执行外部发送。
