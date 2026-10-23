# 独立子任务与完成交付

状态：当前；#157 已进入总 PR #162 集成分支，独立两轴审查及修复复核完成。完整 Shiori 复刻仍在进行，未合入 main。

最后核验：2026-10-16。

事实来源：[ADR-0012](../adr/0012-owned-task-runtime.md)、[子任务工具](../../backend/app/agent_runtime/spawn_tool.py)、[受管任务](../../backend/app/task_runtime/runtime.py)、[完成 worker](../../backend/app/task_runtime/completion.py)、[会话追加](../../backend/app/session_store.py)、[行为测试](../../tests/python/test_spawn.py)、[HTTP 与渠道测试](../../tests/python/test_spawn_api.py)、[网页测试](../../web/src/features/role-management/SpawnSettings.test.tsx)。行为参考固定 Shiori `4f266aeaf42fcd611362e2456417b91f659341cf` 的 `spawn.py`、`subagent_profiles.py`、`subagent_manager.py` 和完成流。

## 授权与调用

普通主人对话默认提供 `spawn` 和 `spawn_manage`，旧 spawn.enabled 不再关闭普通委托。角色网页保留有限预算；研究/脚本/general profile 仍只消费冻结工具子集。受限 Shell 需要独立的冻结与当前命令许可。

`spawn` 接收 `task`、可选 `label`、`profile`（`research` / `scripting` / `general`）、`run_in_background`（默认 false）、`retry_count`（0 或 1）。未知参数、来源身份字段、未知 profile 和超预算重试计数会被拒绝。`retry_count` 记录调用者明确发起的重试，系统不自动重跑可能产生作用的脚本。

返回稳定的宿主 `taskId` / `job_id`、真实状态、原始 `origin`、有界结果与日志信息。同步等待真实终态，只通过工具结果返回；显式后台立即返回任务身份，不依赖已结束的父回合取消令牌。

`spawn_manage` 的 `action` 为 `list`、`get`、`cancel`，后两者需要 `job_id`。仅能观察或停止同角色、同原始上下文的子任务；不存在和越权使用同一拒绝，不暴露日志地址或内容。重复取消保留已有终态。

## 独立执行与有限权限

子任务在应用 TaskRuntime 的执行线程中运行 portable AgentLoop，不获取普通角色对话锁，不创建主人 session、入站 user 或普通角色 run。任务 ID 同时作为子模型 session/run 和子工具调用身份的宿主前缀。仅传递任务文本，不复制父对话、主人记忆或待办上下文。子 transcript 的工具调用和结果进入有界独立任务日志。

调用当刻冻结父实际可见的能力版本、工具定义、工具对象和模型连接配置；之后激活新工具或修改模型连接不会扩展子任务。每次执行和运行期间的 watchdog 同时检查当前授权：

| Profile | 允许的工具，始终与冻结父工具取交集 |
| --- | --- |
| research | read_file、list_dir、web_search、web_fetch |
| scripting | read_file、list_dir、write_file、edit_file、shell |
| general | 以上并集 |

不继承递归委托、工具发现、Memory 写入、Todo、渠道外发、浏览器或任意 MCP 工具。读操作可访问父已授权角色工作区和自身任务产物；写文件工具只接受宿主生成的独立任务目录，沿用已有路径、符号链接和原子写保护。

子 Shell 直接持有真实 ProcessExecutor，使用父冻结白名单、最小环境、固定任务目录及当前白名单交集。它保持前台并属于子任务取消链，不能开启后台或自动转后台，也不创建独立孙 Shell 任务。命令超时不超过父 Shell 的冻结超时。**这不是操作系统文件或网络沙箱**：argv 白名单不能约束程序自身的任意文件或网络访问。

宿主设置：超时默认 120 秒、最大 600 秒；模型请求默认 12、最大 30 次；结果默认 20000、最大 100000 字符；累计模型输出默认 60000、最大 200000 字符。每轮工具调用也计数，累计最多 `maxTurns × 4` 次；流式文本、工具参数和模型 metadata 纳入输出预算。rich 模型请求还设置至多 4096 输出 token。日志、全局并发、每角色并发和保留期沿用 [TaskRuntime 预算](owned-background-shell.md)。

## 原来源与完成交付

渠道 origin 从宿主真实 run、入站消息、角色账号和 FrozenChannelSource 捕获，包含发言者、绑定身份、绑定时间、账号创建代际和原线程。每次管理、子工具调用、watchdog、完成整理及进入发送 lease 后重验；跨角色、群、发言者、重新绑定、账号重建、平台暂停或来源失效均不获得旧任务权限。

后台终态与完成 outbox 持久化在同一任务 SQLite 中。完成 worker 等待原角色正常回合释放锁，再以原角色进行一次最多 15 秒、8000 字符、无工具模型整理；它不触发主人记忆或待办维护。整理使用原模型连接身份；连接定义变化或被删除时标记交付失败，避免把已有结果发到新服务。

内部完成消息明确标注任务 ID 和真实终态，以确定性消息 ID 追加到已存在的原桌面会话或渠道线程。它是 `assistant / internal_completion`，不是主人发言，不创建新会话。SQLite 追加事务保证 outbox 在会话追加后崩溃、重启时也不重复追加。

桌面追加成功后完成状态为 `sent`。渠道通过已有 ChannelHost 目录和 delivery 账本投向原账号、原目标，保留 `pending`、`sent`、`failed`、`uncertain`，不改投其他目标；消息初次追加事务即保存实际渠道与 `pending`，未确认消息不会作为已送达 transcript 回放。外发前先持久化 `sending` 和已提交标记；合法 `pending` 回执保留原 attempt，不能再次进入待发送队列。发送超时、取消或进程中断转为 `uncertain`，不盲目重发。重启按权威 outbox 同步全部已记录消息状态，包括收据已提交而本地消息未更新的窗口；本地消息更新失败也不能把已确认收据改成失败。来源失效则封存为失败，不向新绑定发送。

执行状态保留 `succeeded`、`failed`、`cancelled`、`timed_out`、`interrupted`。关闭释放真实模型/进程资源；重启将缺句柄的 queued/running 标记 interrupted，不自动重跑。连续取消不能跳过资源释放：子执行器只发起一次取消，后续取消等待已有清理；同步 spawn 和子 Shell 等待实际清理完成，之后才允许宿主记录终态。

未交付的 `pending/sending` 完成事件连同任务、日志和目录保留，避免父锁被占用或应用关闭期间被普通保留期/配额清理静默丢弃；它们仍占用每角色记录数和日志总预算。容量被占满时拒绝新任务，直到已有完成交付或失效，而不无限扩大存储。收据与本地消息尚未同步时也先保留权威 outbox，重启修复并确认消息同步后才允许正常保留期清理；已确认、确定失败或 uncertain 的已同步记录沿用正常保留期。角色删除会清理全部归属数据。

完成 worker 在常驻循环中逐条修复未同步收据。短暂 SQLite 锁冲突保留 pending/dirty 状态并重试，不让一条恢复失败终止其他自动交付；首次追加和收据修复使用短事务锁等待。重试只涉及本地追加/状态同步，不再次提交已有外部 attempt。

## 已验证与边界

公开 HTTP tracer 验证父持角色锁时，子独立循环实际读取文件并同步返回，无同锁死锁；真实 localhost SSE 模型验证独立 transcript、配置冻结和工具往返。后台、真实脚本进程取消、来源封存、并发及模型预算、重启 interrupted、会话幂等和渠道账本的成功/失败/uncertain 均由受控外部边界测试验证，执行器、子循环和 SQLite 不使用替身。

平台发送验证使用获准的本地传输 fixture，没有使用真实平台账号、付费模型或真实凭据。当前范围只完成独立 spawn 切片，不表示通用调度、Coding Agent、Drift 或全部 Shiori 工具已经完成。
