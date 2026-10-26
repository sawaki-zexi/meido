# 角色后台 Shell 与 TaskRuntime

状态：当前。最后核验：2026-10-16。

#149 已随 PR #162 合入 main；以下为现行协议，普通默认 Shell 的授权决定见 [ADR-0021](../adr/0021-default-builtin-tools.md)。不代表真实服务全部验收完成。

事实来源：[ADR-0012](../adr/0012-owned-task-runtime.md)、[TaskRuntime](../../backend/app/task_runtime/runtime.py)、[真实进程执行器](../../backend/app/task_runtime/process.py)、[AgentTool](../../backend/app/agent_runtime/shell_tool.py)、[进程验收](../../tests/python/test_background_shell.py)、[HTTP 验收](../../tests/python/test_background_shell_api.py)。对应 #149 / 完整复刻规格 C03、D02、T07。

## 工具协议

普通主人对话默认提供 `shell`、`task_output`、`task_stop`，不要求角色 enabled 或命令白名单；旧空配置同样生效。角色、真实来源、取消和删除仍在调用与后台观察时校验。`agentConfig.shell` 保留预算及受限任务许可，旧开关不再控制普通 Shell。任务日志与角色工作目录分开存储。

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `shell` | 必填 `command`；可选 `description`、`timeout`、`run_in_background`、`auto_promote` | Windows 使用 PowerShell 7，POSIX 使用 /bin/sh；固定 workspace、最小环境与关闭 stdin。后台/自动提升仍使用原 TaskRuntime 预算与进程树生命周期。 |
| `task_output` | 必填 `task_id`；可选 `block=false`、`timeout_ms=30000`（0–30000） | 返回当前快照或有界等待后的快照；等待结束不停止、不重跑任务；取消观察也不停止后台执行 |
| `task_stop` | 必填 `task_id` | 停止整个受管进程树，等待资源释放后返回终态；重复停止保留第一次终态 |

`timeout` 默认取角色 Shell 超时，不能超过角色上限或宿主 600 秒上限；后台与自动提升均保留原始截止，不获得无限运行许可。自动提升阈值由宿主设置，模型不能修改。普通前台调用随父回合取消，已返回后台的任务继续由应用持有，完成默认通过 `task_output` 拉取。

渠道任务从真实入站消息冻结 sender、角色/线程/账号及创建代际、配对 identity/pairedAt 和主人私聊边界；不读取正文自报身份。提交、查询和后台授权观察复用 `FrozenChannelSource`，平台/账号暂停、响应范围关闭、屏蔽、解绑或重绑均会拒绝日志访问并停止原执行。来源 scope 不包含单条消息时间，因而同一绑定/发送者的后续回合可查询；重新配对即使还是同一平台 ID，也不能继承旧任务。前台工具 child token 结束不撤销已返回的后台任务。

普通 Shell 不做 argv 白名单过滤。受限子任务与 soft schedule 独立校验创建时及当前开关/命令白名单；它们不继承普通默认 Shell。Windows 环境名按大小写不敏感匹配既有最小集合，保留 SYSTEMROOT，不继承服务密钥。

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

Shell 是此边界的首个完整执行器。#157 的[独立子任务](owned-subagents.md)复用任务身份、SQLite、预算、日志与取消，并增加实际消费的任务目录、原来源列表和持久完成 outbox。未交付 pending/sending 任务占用有限保留额度，不能被普通清理静默移除；额度无法释放时新任务提交被拒绝。#158 的[通用调度](owned-schedules.md)已复用同一 Runtime，并补齐幂等交接及结果保留/ACK，两者均已复核并进入集成分支；Coding 执行器已随总 PR #162 合入，真实认证和消费另验。

## 与 Shiori 的明确差异

[固定上游 Shell 实现](https://github.com/YinFengWindy/Shiori-Agent/tree/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell) 使用 pwsh/系统 shell，显式后台未传 timeout 时可无硬截止，终态日志采用短暂内存注册表。本实现按 ADR-0021 默认使用平台 Shell，保留有限宿主截止、最小环境和固定目录，以 SQLite 保留真实终态，停止任务后保留受预算约束的日志供原来源查询。参数使用上游名称；结果沿用 Meido `AgentToolResult` 的 camelCase 元数据，没有伪造上游 JSON 字段兼容性。

这仍以宿主操作系统身份运行，普通 Shell 可执行本机命令；固定目录和日志归属不是操作系统沙箱。

## TaskOrigin 历史兼容（2026-10-26）

统一 `TaskOrigin.from_dict` 接受明确的旧 snake_case 存储与 camelCase 公共格式，拒绝未知/混合字段及格式错误；保留旧位置参数入口。capability_version/capabilityVersion 是非授权历史 request/catalog 元信息，新调用不再通过目录链取权限。真正的自主/来源验证版本使用 grant_domain/grant_revision（公开 grantDomain/grantRevision）；只从已知旧来源自身字段解释旧重载，不读取最新配置补权。

旧不完整渠道来源可以按原 scope 查询、停止与清理，但新增执行/投递须校验原 FrozenChannelSource 与 live 许可；缺失来源不会被修复成主人。NovelAI 旧 canonical origin 不添加空字段，保持原请求/receipt 身份。Coding、schedule、source verification 共同使用该 codec。旧行无需全量重写，任务/回执不因工具目录迁移重放。见 [ADR-0026](../adr/0026-request-bound-lightweight-tool-loading.md)。
