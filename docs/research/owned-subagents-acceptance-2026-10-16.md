# 独立子任务本地验收

状态：当前，本地实现与验收，待总 PR #162 集成和独立两轴审查。最后核验：2026-10-16。

事实来源：[Issue #157](https://github.com/sawaki-zexi/meido/issues/157)、[子任务契约](../reference/owned-subagents.md)、[AgentTool 行为测试](../../tests/python/test_spawn.py)、[HTTP 与渠道测试](../../tests/python/test_spawn_api.py)、[网页授权测试](../../web/src/features/role-management/SpawnSettings.test.tsx)。

实现基点为已验证总集成 `acfd2a10d735f270b35a84391e00fd54d5950119`；本地实现提交 `ac16232`，随后依照维护者已批准的总 PR 流程合入 #165 的固定集成头 `af61ad33c50599ccbd101d05f76689abfd1eab96`（本分支合入提交 `d32fd0e`）。没有 push、合并 PR 或关闭 Issue。

## 已提取的行为证据

- 同步 tracer 先因未注册 spawn 失败，再通过：FastAPI HTTP 父回合持普通角色锁时，TaskRuntime 内独立 portable loop 实际读测试文件并返回已知 marker；父会话只有一条真实主人输入，未建立第二主人会话或子角色普通 run。
- localhost ThreadingHTTPServer 的真实 SSE 模型请求，经生产 OpenAICompatibleAdapter 完成父模型 → spawn → 子模型 read_file → 实际文件内容 → 子结果 → 父结果。请求中可见独立 transcript、当前父工具交集、有限输出 token；中途修改模型配置后，父子仍使用调用时被冻结的配置。
- 显式后台父回合先完成，子任务随后读实际文件。原会话收到一次有真实 task ID / 终态的无工具模型整理；重放 pending 事件后仍只有一个内部 assistant 消息，没有新增主人发言。
- 后续 HTTP 回合可对同一任务 list / get / cancel。跨角色、群、发言者、解绑/重绑、平台暂停及同 ID 账号重建不能读日志或取消别人的任务；来源撤权触发 watchdog 停止。首次捕获之前就已发生同 ID 重建时，直接拒绝，无子模型调用或日志生成。
- research / scripting / general 只获得各自 profile 与冻结父工具的交集；真实写入仅进宿主任务目录。真实 Shell 读取产物，越界写和脚本后台参数被拒绝；脚本取消回收实际进程树，不创建孙 Shell task。
- 有界请求/轮次、工具调用计数、模型累计输出、执行超时、并发和来源撤权产生真实终态。前台父取消等待子任务资源释放，显式后台不依赖已完成父回合的令牌。
- 完成 worker 等待原角色锁释放，然后整理并追加内部消息；真实 ChannelHost 与 SQLite delivery 账本验证 sent / failed / uncertain，来源失效不改投。模拟外发收据尚未回写的崩溃窗口后，重开变 uncertain、修正本地消息状态且没有第二次发送。
- 应用关闭将正在执行的子任务标为 interrupted，重开只回传中断事实，子模型调用计数不增加，不盲重跑。
- 连续取消回归使用真实 Shell 输出和外部模型 cleanup 屏障：旧单次 shield 会提前结束前台工具；修复后连续三次取消仍等资源释放，释放前 ended_at 为空，之后才记录 cancelled。
- 保留额度回归先证实旧清理会用新任务挤掉 pending 事件。修复后 pending/sending 持续占用有限额度，容量满拒绝新任务，普通过期查询也不丢未投结果。
- 两路独立审查发现跨库完成状态窗口、子 provider 连续取消和合法 pending 回执重发。新增真实 SQLite/TaskRuntime/ChannelHost/TCP 回归首先得到 13 failed / 5 passed；修复后同组 18 passed。初次消息事务即写渠道/pending，outbox 另持久化已提交标记，重启按所有已记录收据恢复消息，pending 不重发。旧版本 schema 迁移也保留已提交意愿。
- 另以本地消息确认写入一次 OSError 先复现已发送被改成 failed；修复后持久 sent/attempt 保留，消息可恢复且发送计数始终为一。连续四个管理取消在真实 localhost TCP 的 provider finally 屏障前保持未终态，释放连接后才共同返回 cancelled。
- 网页独立启用、有限预算保存、重新打开和关闭保留配置，以及原有 11 项基础工具、Shell 和未知配置保留均通过公开 React 操作验证。后端专用关闭不能被 enabledTools 中同名项绕过。

## 检查记录

以下组含重叠用例，不相加为唯一总数：

| 检查 | 结果 |
| --- | --- |
| 最终独立子任务行为与公开 API 全组 | 35 passed |
| 审查修复后 spawn / API / 完成恢复 / 后台 Shell / 工具发现 | 90 passed |
| 完成恢复 / 角色能力 / 能力层交叉组（与上组重叠） | 32 passed |
| spawn、spawn API、后台 Shell、Shell API、来源授权、ChannelHost、角色能力、工具发现 API、成员 HTTP 交叉组 | 107 passed；其后新增的 recovery 参数场景所在完整渠道完成组另为 5 passed |
| 全部角色管理 React 用例 | 5 files / 16 tests passed |
| npm run build | tsc --noEmit 与 Vite 生产构建通过 |
| Ruff：本票触及 Python 源码与测试 | 通过 |
| mypy：spawn_tool、task_runtime/completion、task_runtime/runtime、models；follow-imports=silent | 4 files，无错误 |
| compileall：backend、tests；git diff --check | 通过 |
| 五份新增/修改契约、手册和 ADR 的相对链接 | 全部存在 |

新增持久恢复证据见[完成恢复行为测试](../../tests/python/test_spawn_completion_recovery.py)。以上 35 / 107 项为首次提交阶段记录；90 / 32 项为独立审查修复阶段记录，不相加为唯一总数。修复仍待独立两轴复核。

扩大到现有 MeidoProvider / main / SessionStore 的全模块 mypy 检查仍有类型错误，未宣称全仓 mypy 通过；仓库 CI 的 Python gate 为 compileall 与 pytest。本票没有调用真实付费模型、真实平台账号或真实凭据；渠道回执是本地受控平台边界，不能解释为真实平台收件证据。完整 76 工具、调度、Coding Agent、桌面/屏幕和其余 Skills 仍由其他范围推进。
