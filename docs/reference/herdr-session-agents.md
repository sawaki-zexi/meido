# Herdr session 代理控制

状态：当前实现，待 #284 交付。最后核验：2026-10-30。
事实来源：`backend/app/herdr_control/`、`main.py`、`PluginParameters.tsx`、
`tests/python/test_herdr_control{,_channel}.py`。
上游：Shiori `71c8b0e460530683cd28aad6bb62e873c7167c89` 的
`plugins/herdr/backend/{agent_tools,agent_control,agent_watch,agent_commands,agents,cli,config,sessions}.py`。

## 配置与生命周期

设置 → 插件 → Herdr 代理管理应用级开关与公共参数。
`/api/plugin-management/herdr` 使用既有 revision / cleanupPending 配置协议；
`/api/plugin-management/herdr/status` 只读检查已保存 session，返回实际错误或按工作区分组的代理列表。

| 参数 | 默认值 | 约束 |
| --- | --- | --- |
| executable | herdr | 非空可执行文件路径或命令名 |
| session | default | 字母、数字、下划线、连字符 |
| cli_timeout_seconds | 45 | 5–300 秒 |
| task_timeout_seconds | 7200 | 60–86400 秒；从最新指令计时 |

首次操作检查 CLI 与运行中 session server 均兼容且至少为 0.9.4；
操作失败后重新检查依赖以解释真实原因。session 不存在、停止、版本错误和缺少程序均不表示空列表。
插件只连接已有 session，不启动或停止 server、工作区或代理。
Windows 使用隐藏的 argv 子进程；`.cmd/.bat` 参数中的 cmd 控制、展开、引号和控制字符按上游拒绝。
超时或取消只终止并回收本次 CLI 子进程；线程 worker 完成后取消才返回。
此边界兼容 Windows Uvicorn 的 SelectorEventLoop。

参数更新保留进程内指令、接手关系和追踪；已有追踪继续使用捕获的 session 配置。
关闭插件停止追踪、清除接手关系，保留本进程指令回执；不终止代理。
应用重启不恢复指令或追踪，不重放输入。没有新增持久任务账本。

## 四个工具

| 名称 | 业务参数 | 风险与结果 |
| --- | --- | --- |
| herdr_list_agents | 无 | 只读，包含手动代理；workspace 分组、pane_id、status、title、cwd、handler |
| herdr_read_agent | pane_id | 只读，末尾 4000 字输出、最近 20 条指令、can_prompt、handler |
| herdr_prompt_agent | pane_id、text | 外部效果，完整 prompt 发送一次 |
| herdr_send_keys | pane_id、keys | 外部效果，按顺序发送逻辑键名 |

四工具 source 为 `builtin:herdr`，默认按需发现，沿用统一 description 调用意图、
当前工具注册表与原文窗口恢复。模型不能指定回报目标，不另加每次输入确认。
prompt 与 keys 共用发送与记录链；每个 session/pane 最多保留 100 条记录。
prompt 使用 `agent prompt <pane> <text> --wait --until working --until blocked --timeout 10000`，
CLI 进程等待至少留出 15 秒。keys 使用 `agent send-keys <pane> <keys...>`。
输入记录区分 sent、unconfirmed、failed；timeout、agent_prompt_stalled、invalid_response 为不确定，
取消已交给 CLI 的输入也为不确定，继续追踪但绝不自动重发或再次输入探测。

## 接手与回报

宿主从真实用户回合捕获 TaskOrigin 与冻结的渠道来源。输入交给 Herdr 时，发起角色接手该代理。
回报按真实角色与聊天目标分组，每组使用最后一条命令 ID，最多尝试一次；失败或投递不确定不重试。
来源解绑、角色删除或插件关闭时不改投其他聊天。

每 2 秒读取状态，起步宽限 30 秒，连续 5 次失败为 unreadable。
见过 working 后 idle/done 为 finished；未见 working 且宽限结束为 idle。
另有 blocked、gone、timeout。keys 前读取 state_change_seq，旧 blocked 直到序号推进、
观察到离开 blocked 或宽限结束才算新阻塞。新指令重置计时并使正在读取的旧结果失效。
读输出与投递期间新到的指令属于下一批。

回报作为宿主 `herdr_agent_report` 事件进入原角色普通工具回合，使用当前注册表、原文窗口和真实来源。
内部触发不在公开聊天里显示为主人发言，也不成为后续普通回合的主人输入。
接手角色可继续操作自己接手的代理，回报仍用该代理的原目标；其他角色只能在真实用户发起的回合输入。
普通工具仍遵守自身来源规则，没有 Herdr 回报专用总工具白名单。

## 与受管编程任务的关系

已有 `herdr_start_task/list_tasks/get_task/continue_task/cancel_task` 仍为 `builtin:coding`，
保留独立 server、worktree、可信仓库/CLI、消费探针与发布批准边界。
四个 session 工具不继承这些专用配置门槛。
实际受管任务绑定的 session/pane 正在执行或仍占有运行资源时，直接输入被拒绝并指向 `herdr_cancel_task`。
`herdr_start_task` 引导仍只承诺本地编辑和固定验证；本次没有新增自动 Draft PR 交付。

验收使用编译的无网络 C CLI 夹具和本机 OneBot 服务，不操作真实代理或账号。
