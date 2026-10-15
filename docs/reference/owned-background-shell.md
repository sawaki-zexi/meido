# 角色后台 Shell 与 TaskRuntime

状态：当前。最后核验：2026-10-16。

当前分支已复用 #148 的共用来源授权，原 9 个公开 AgentTool 反例由红转绿；另验证平台暂停、同 ID 账号重建、同源后续回合与重新配对后的日志隔离。代码等待总 PR #162 集成与独立审查，不代表完整 Shiori 目标完成。

事实来源：[ADR-0012](../adr/0012-owned-task-runtime.md)、[TaskRuntime](../../backend/app/task_runtime/runtime.py)、[真实进程执行器](../../backend/app/task_runtime/process.py)、[AgentTool](../../backend/app/agent_runtime/shell_tool.py)、[进程验收](../../tests/python/test_background_shell.py)、[HTTP 验收](../../tests/python/test_background_shell_api.py)。对应 #149 / 完整复刻规格 C03、D02、T07。

## 工具协议

角色 `agentConfig.shell.enabled` 为真且白名单非空时，提供 `shell`、`task_output`、`task_stop`。关闭开关或清空白名单后，把这些名字放入 `enabledTools` 也不能重新启用。配置改变会停止角色现有 Shell 任务；旧能力快照不能重新启动已撤销的命令许可。任务日志与角色工作目录分开存储。

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `shell` | 必填 `command`；可选 `description`（最多 500 字符）、`timeout`（秒）、`run_in_background`、`auto_promote` | 保留 argv 解析、白名单、固定 workspace、最小环境与关闭 stdin。仅 command 的旧调用兼容；前台完成直接返回输出。显式后台立即返回稳定 task ID；默认运行 15 秒后自动提升后台，`auto_promote=false` 保持前台 |
| `task_output` | 必填 `task_id`；可选 `block=false`、`timeout_ms=30000`（0–30000） | 返回当前快照或有界等待后的快照；等待结束不停止、不重跑任务；取消观察也不停止后台执行 |
| `task_stop` | 必填 `task_id` | 停止整个受管进程树，等待资源释放后返回终态；重复停止保留第一次终态 |

`timeout` 默认取角色 Shell 超时，不能超过角色上限或宿主 600 秒上限；后台与自动提升均保留原始截止，不获得无限运行许可。自动提升阈值由宿主设置，模型不能修改。普通前台调用随父回合取消，已返回后台的任务继续由应用持有，完成默认通过 `task_output` 拉取。

渠道任务从真实入站消息冻结 sender、角色/线程/账号及创建代际、配对 identity/pairedAt 和主人私聊边界；不读取正文自报身份。提交、查询和后台授权观察复用 `FrozenChannelSource`，平台/账号暂停、响应范围关闭、屏蔽、解绑或重绑均会拒绝日志访问并停止原执行。来源 scope 不包含单条消息时间，因而同一绑定/发送者的后续回合可查询；重新配对即使还是同一平台 ID，也不能继承旧任务。前台工具 child token 结束不撤销已返回的后台任务。

Shell 同时遵守冻结实例白名单与当前最终命令白名单；后台观察也检查原命令是否仍许可。Windows 环境变量名按大小写不敏感匹配既有许可集合，保留 `SYSTEMROOT`，不扩大可继承环境范围。

结果 `details` 包含 `taskId`、`status`、`exitCode`、`background`、`durationSeconds`、`description`、`kind`、`timeoutSeconds`、`outputChars`、`truncated`、`logPath`、`logBytes`、`logComplete`、`logTruncated`、`logError`、`progress` 和 `origin`。`origin` 固定角色、会话、来源线程/账号、父 run、宿主唯一 tool call、该调用实际能力版本及 `sourceScopeId/sourceAuthority`；后续同来源回合读取时仍返回原 origin。不存在与不属于当前来源使用相同错误，不泄漏记录是否存在。

状态为 `queued`、`running` 或 `succeeded` 时工具结果不是错误；非零退出为 `failed`，硬截止为 `timed_out`，主动停止/撤销为 `cancelled`，关闭和重启后缺失句柄为 `interrupted`。失败终态不会被重复 stop 改成成功。

## 输出、存储与回收

stdout/stderr 持续消费，UTF-8 分块使用增量解码。运行中尾部和保留日志按泵到达次序记录，stderr 片段带 `stderr:` 前缀；正常排空后的终态展示则先组合 stdout，再附加 `\nstderr: ` 和 stderr，共同受角色展示预算截断。`shell` 与后续 `task_output` 返回相同终态展示，不将 stderr 插入 stdout JSON 中；超出预算时仍可能截去 JSON 前缀，并明确报告截断，不保证截断内容可解析。执行器只保留每条流有界尾部，不无限缓存输出。

超过展示预算的输出仍进入独立日志。`truncated=true` 表示展示尾部被截断；只有 `logComplete=true` 才表示日志保存了完整输出。达到日志存储上限后继续消费管道，明确返回 `logTruncated=true`，保留文件不能称为完整输出。

展示预算兼容角色设置的完整有效范围（最多 200000 字符）。日志路径不可写等消费故障会停止真实进程并返回 `failed`、`logComplete=false` 和仅含异常类型的 `logError`；失败终态写入独立 SQLite，不再次依赖已经损坏的日志路径，也不把写入故障解释为预算耗尽。

宿主默认最多同时运行 8 个任务、每角色 3 个；每任务日志最多 4 MiB、每角色日志最多 32 MiB。提交时预留运行任务的完整日志额度，必要时删除较旧的已结束记录和日志。每角色最多保留 100 条记录；终态默认保留 24 小时，在启动、提交、查询和完成时清理过期记录。运行任务不会因为日志保留期或观察超时被盲目重启。日志路径使用角色身份摘要和宿主生成的任务 ID，模型不能指定路径、cwd、env 或执行类型。

SQLite 保存不可变 origin、执行类型、预算、描述、进度、输出计数、日志引用和终态，不保存原始 argv、环境或模型凭据。该模块由应用生命周期持有，与 SSE 连接和 portable AgentLoop 分离。

Windows 使用以挂起方式创建的进程和 `KILL_ON_JOB_CLOSE` Job，启动前完成树归属；成功父进程遗留的子孙也会清理。POSIX 正常停止使用独立进程组。应用关闭停止活动任务并记录 `interrupted`；重启将遗留 queued/running 记录标为 interrupted，不重新执行。角色删除同时清理任务记录及日志；启动时清理删除中断造成的失效角色记录。Shell 配置或来源失效时，运行中的授权观察会停止相应进程。

## 可复用的应用边界

`TaskRuntime.submit(origin, executor, budget, kind, description, authorized)` 提交真实执行；`query` 只观察，`stop` 取消，`cancel_role_tasks`、`set_role_enabled`、`delete_role`、`reconcile_roles`、`close/start` 管理归属和应用生命周期。`TaskExecutor.run(TaskExecution)` 必须返回 `TaskOutcome`，`cancel()` 必须释放其资源。`TaskExecution` 提供冻结 budget/deadline 和有界日志/进度写入；`TaskOutcome.output_tail` 可提供终态展示，Runtime 再次按同一预算截断，保留日志与实际消费计数不被它替换。

Shell 是此边界的首个完整执行器。后续子 agent、调度和 Coding 执行器可复用任务身份、SQLite、预算、日志与取消；本票没有注册尚未实现的工具或伪装为主人回合。spawn 的完成回灌和调度交付仍属于后续 Ticket。

## 与 Shiori 的明确差异

[固定上游 Shell 实现](https://github.com/YinFengWindy/Shiori-Agent/tree/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell) 使用 pwsh/系统 shell，显式后台未传 timeout 时可无硬截止，终态日志采用短暂内存注册表。本实现保留 Meido 已批准的 argv 白名单、有限宿主截止、最小环境和固定目录，以 SQLite 保留真实终态，停止任务后保留受预算约束的日志供原来源查询。参数使用上游名称；结果沿用 Meido `AgentToolResult` 的 camelCase 元数据，没有伪造上游 JSON 字段兼容性。

这仍是以宿主操作系统身份运行的白名单进程，不能将白名单或角色日志归属称为操作系统沙箱。
