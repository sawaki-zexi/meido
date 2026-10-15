# 在网页中授权角色基础工具

状态：#163 实现，待总 PR #162 合入；最后核验：2026-10-16。
事实来源：[角色表单](../../web/src/features/role-management/RoleEditor.tsx)、[有限工具清单](../../web/src/features/role-management/BasicToolSettings.tsx)、[用户操作测试](../../web/src/features/role-management/BasicToolSettings.test.tsx)及各工具 HTTP 测试。

1. 在聊天页打开“角色资料 → 资料”，或从角色卡的编辑入口进入。新建角色也使用同一表单。
2. 在“基础工具授权”中逐项选择所需工具。读取与修改工作区分开授权，同组未选项不会自动开启。
3. 保存后打开或刷新“实际工具”目录，查看已保存配置的启用、策略、模型可见性及依赖状态。表单尚未保存时会显示提示，目录仍使用旧配置。

| 分组 | 可分别选择的工具 | 范围 |
| --- | --- | --- |
| 工作区读取 | read_file、list_dir | 当前角色获准的文件和目录 |
| 工作区修改 | write_file、edit_file | 当前角色获准的写入/修改路径 |
| 联网查询 | web_search、web_fetch | 宿主已经配置并许可的网络服务 |
| 消息原文 | search_messages、fetch_messages | 当前角色和发言者仍获准访问的原文 |
| 显式记忆维护 | memorize、forget_memory | 当前角色记忆；全局记忆和主人来源门禁继续生效 |
| 按需发现 | tool_search | 只发现并激活已经授权的候选 |
| 桌面对话 | message_push | 将文本、角色图片或文件推送到当前角色自己的桌面对话 |
| 时间任务 | schedule、list_schedules、cancel_schedule | 当前角色及真实原来源的计划，关闭创建授权撤销执行 |

默认不授权这些工具；关闭某项仅移除该项选择。保存后重开、恢复未提交的新建草稿或编辑角色资料，均保留独立选择和表单未管理的其他能力配置。

桌面推送无需渠道账号。开启后使用 `channel=desktop` 和 `chat_id=owner` 或 `user`；图片和文件使用当前角色已有的附件 ID。桌面推送不向手机或其他账号发送，对外发送仍使用 `account_send`。详见[桌面推送契约](../reference/desktop-push.md)。

勾选表示角色许可，不表示依赖、凭据或服务已经可用。宿主关闭联网或全局记忆时，选择仍保留，目录说明具体原因；实际执行继续拒绝。网络服务的单独配置见[联网工具](role-network-tools.md)，记忆业务行为见[显式记忆工具](role-memory-tools.md)。

Shell、浏览器、主动记忆检索与待办仍用原有专用入口；基础清单不授予 MCP、屏幕、桌面控制、外部账号或付费服务权限。角色关闭专用能力时，不能靠工具名绕过其开关、白名单或 grant。

每个聊天运行开始时冻结允许的候选；按需发现只会追加这个范围内的模型可见性版本。后续撤销许可仍能阻止执行；保存的新授权由新运行使用。
