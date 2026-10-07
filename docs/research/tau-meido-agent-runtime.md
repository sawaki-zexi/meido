# Tau Agent Loop 与 Meido Runtime 结合调研

状态：当前

最后核验：2026-10-07，Tau `main` `0c233ff080ba261ec1178e9232301cbb6479e648`；Meido `main` `987e6c9c462a30903811d9fed48808151388f56d`

事实来源：Tau 一手源码和测试、Meido 当前 `main` 的源码和测试、开放 Issue #75/#78。结论、计划和源码事实分开标记。

## 评估结论

Meido 当前只有“单次文本回复编排”，还没有独立的 Agent Runtime：HTTP 路由负责角色锁、消息落库、记忆召回、模型流式调用和完成后的记忆任务提交。Tau 的纯 Agent Loop 可以补上“模型回复 -> 工具调用 -> 工具结果 -> 下一轮模型回复”的执行内核，但不应整体替换 Meido 的角色、会话、SQLite 和记忆边界。

推荐参考 Tau portable loop 的行为语义，由 Meido 自行定义 Runtime 契约和适配层：

```text
Meido role/session/memory/persistence
        |
        v
Meido Runtime Manager -> Meido Agent Loop
        |                         |
        v                         v
Meido Provider Adapter       Meido Tool Registry
        |
        v
OpenAI-compatible model API
```

不建议直接依赖 Tau 完整 Python 包，也不建议移植 `tau_coding`、TUI/CLI、项目文件发现、JSONL session 或 coding tools。

## 一手源码事实

本轮复核 Tau `main` 的提交为 `0c233ff080ba261ec1178e9232301cbb6479e648`。该提交仍保持 `tau_ai → tau_agent → tau_coding` 分层；`tau_agent` 提供 provider-neutral messages、events、tools、loop、harness 和 session primitives，`tau_coding` 提供 CLI/TUI、coding tools、项目指令和磁盘会话。Tau README 明确把它定位为终端 coding agent 和 Pi-style harness 的 Python 实现；因此适合参考其 portable loop，不等于 Meido 应引入完整 Tau coding 产品。

### Tau Loop

Tau 官方 README 将项目定位为受 Pi 启发的 Python coding-agent harness，并明确说明它是独立的 Python 重写，不是官方逐行 Python port：

- [Tau README](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/README.md)
- [Tau architecture notes](https://github.com/huggingface/tau/tree/0c233ff080ba261ec1178e9232301cbb6479e648/dev-notes/architecture)

[`src/tau_agent/loop.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/loop.py) 的 `run_agent_loop()` 接收 provider、model、system、消息列表、工具列表、最大轮数、取消 token，以及 steering/follow-up 和工具前后钩子。它执行以下流程：

1. 发出 `agent_start` 和 `turn_start`。
2. 调用 provider 的流式接口，把 provider 事件转换成统一的消息事件。
3. 追加完整的 assistant message。
4. 按顺序执行 assistant 中的 tool calls。
5. 追加 `ToolResultMessage`，存在工具调用时重新请求 provider。
6. 没有工具调用或出现错误/中止时发出 `turn_end` 和 `agent_end`。

Loop 还会处理最大轮数、取消、失败消息过滤和悬空 tool history 修复。当前工具调用是顺序执行；这适合 Meido 的单角色串行会话，后续若需要并行工具应单独设计。

当前 `loop.py` 使用 `_provider_context()` 调用 `repair_tool_history()`：没有结果的 call 会得到确定的中断错误结果，孤立/重复结果会被丢弃，结果会移动到匹配 call 后；空内容且 `error/aborted` 的 assistant 会被排除出下一次 provider context，但完整历史仍保留诊断信息。`AgentHarness` 在消费 loop 前后还负责把修复通过事件送到持久化订阅者。Tau loop 本身没有 Meido 需要的 run id、SQLite 事务或 HTTP 锁，这些必须由 Meido 外层承接。

Tau 的官方 `tests/test_agent_loop.py` 进一步确认：完整的 `AssistantMessage` 由 `message_end` 作为持久化边界；工具结果也经过独立的 message lifecycle 事件后才进入下一次 provider 调用。工具执行函数可以接收 `tool_call_id`、取消信号和 `on_update`，因此 Meido 的 Tool 协议应保留这些语义，而不是只返回一个字符串。

对固定提交的 loop、message 与 history 实现再核对后，有几项实现约束值得明确：

- `AssistantMessage.content` 是有序 block 列表，可在同一条消息中交错保存文本、thinking 和多个 `ToolCall`；`tool_calls` 只是从 block 中提取的视图。持久化层应保留一个 assistant turn 的整体顺序与 call ID，不能将每个 call 当成独立 assistant turn。`ToolResultMessage` 独立引用 `tool_call_id`，并包含结构化内容、`details`、`is_error` 和时间戳。见 [`messages.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/messages.py)。
- `message_start/update/end` 的语义不同：update 是流式部分状态，不能作为最终 transcript 记录；assistant 的完整消息在 `AssistantDoneEvent` 或 `AssistantErrorEvent` 转成 `message_end` 时确定。每次工具执行先发 `tool_execution_start/update/end`，再单独发 tool result 的 `message_start/end`。Meido 可以把 update 映射到 SSE，但只应把完整 message lifecycle 作为 transcript 的最终写入依据。见 [`events.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/events.py) 和 [`loop.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/loop.py)。
- 一个 assistant message 内多个 calls 在 Tau 中逐个顺序执行；每个 call 都产生独立结果，工具错误会转为 `is_error=true` 的 `ToolResultMessage`，之后仍可继续下一次 provider 请求。调用前阻止钩子与调用后结果钩子可用于审批/审计；工具收到 call ID、取消 token 与 update 回调。loop 不会把 `asyncio.CancelledError` 吞成普通工具错误。见 [`loop.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/loop.py) 和 [`tools.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/tools.py)。这只说明 Tau 的协作契约，不代表 Meido 已获得可靠的请求中断；provider/tool 的取消仍取决于它们是否合作响应 token。
- `repair_tool_history()` 的目标是构造 provider 可重放上下文：每个 call 后必须有且只有一个按 ID 匹配的结果；优先保留已相邻的有效配对，再将其他结果移到 call 后；缺结果时合成固定错误文本 `Tool call interrupted by user`；孤立结果和重复结果丢弃。它也处理重复 call ID，因此 Meido 最好使用其数据库主键/运行 ID 保证 call ID 在 transcript 中唯一，避免重复 ID 历史让配对含糊。修复结果还给出合成、丢弃、重排计数供诊断。见 [`tool_history.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/tool_history.py) 与 [`test_tool_history.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/tests/test_tool_history.py)。
- `run_agent_loop()` 对既有 `messages` 列表做原地追加；它返回到 `AgentEndEvent.messages` 的是本次新增消息，不是完整历史。`prelude_messages` 会发 message lifecycle 事件但不追加到 `messages`，适合临时前置展示，不应被 Meido 当作已经写入的会话内容。steering 会在下一次模型请求前并入 transcript，follow-up 则在当前工具链收束后启动新链。这些队列语义可留待 Meido 网页需要中途插话时再引入。见 [`loop.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/loop.py)。

Tau 当前 `pyproject.toml` 要求 Python `>=3.12`，完整 `tau-ai` 依赖 `anyio`、`httpx[socks]`、`packaging`、`pillow`、`pydantic`、`pygments`、`rich`、`textual` 和 `typer`。Meido 只需要其中 provider-neutral 的 Agent Loop 类型和事件；复制完整依赖会把 Coding/TUI 运行时边界带入后端。

### Tau 事件、工具和 Harness

[`src/tau_agent/events.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/events.py) 提供 agent、turn、message 和 tool execution 四层事件：

- `agent_start/end`
- `turn_start/end`
- `message_start/update/end`
- `tool_execution_start/update/end`

[`src/tau_agent/harness.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/harness.py) 是可复用的状态壳，持有内存 transcript、订阅者、取消 token、steering 队列和 follow-up 队列。它适合做交互式 Agent，但其 `_messages` 不是持久化真相；Meido 必须让 SQLite 继续作为会话权威状态。

Tau 的 coding 层和 portable agent 层是分开的。Loop 不知道 CLI、TUI、项目路径或 session 文件位置；这些边界说明见其 [phase-3 loop note](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/dev-notes/architecture/phase-3-agent-loop.md) 和 [phase-4 harness note](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/dev-notes/architecture/phase-4-agent-harness.md)。Tau 完整包要求 Python `>=3.12`，并安装 `anyio`、`httpx[socks]`、`packaging`、`pillow`、`pydantic`、`pygments`、`rich`、`textual`、`typer`；Meido 当前以自己的 FastAPI/Pydantic/OpenAI-compatible adapter 和 SQLite 为基础，完整包会带入现阶段不需要的编码界面和依赖。

## Meido 当前实现映射

当前请求入口是 [`backend/app/main.py`](../../backend/app/main.py)：

```text
POST /api/roles/{role_id}/messages
  -> 读取角色和唯一会话
  -> role_locks[role_id] 串行化
  -> 写入 user 与 streaming assistant
  -> MemoryService 召回
  -> ModelAdapter.stream_reply() 文本流
  -> SSE assistant_delta
  -> 完成/失败
  -> 成功后提交 MemoryWorker
```

对应边界如下：

| 现有 Meido 部件 | 现状 | 接入 Tau Loop 后的职责 |
| --- | --- | --- |
| `main.py` | 同时承担 HTTP、锁、消息、记忆和模型编排 | 只负责请求适配、运行创建和 SSE 映射 |
| `model_adapter.py` | `AsyncIterator[str]`，只解析文本 delta | 提供 provider-neutral stream，增加 tool call 事件解析 |
| `session_store.py` | SQLite 文本消息，只有 `user/assistant` | 持久化 assistant tool call、tool result 和 run 关联信息 |
| `memory_service.py`/`memory_worker` | 回复前召回，成功后异步维护 | 保持在 Loop 外；Loop 只消费已编译上下文 |
| `role_locks` | 路由级字典锁 | 迁入 Runtime Manager，保留每角色单运行约束 |
| SSE | `assistant_delta` 等既有事件 | 保持兼容，新增 run/tool 字段或内部事件 |

当前 [`ModelAdapter`](../../backend/app/model_adapter.py) 只暴露 `AsyncIterator[str]` 文本增量，没有工具定义、tool call ID、finish reason 或结构化 assistant message；[`SessionStore`](../../backend/app/session_store.py) 只持久化 user/assistant 文本行。因此不能只把 `run_agent_loop()` 文件复制进来就完成重构。模型连接配置通过 [`model_config.py`](../../backend/app/model_config.py) 由后端读取，不能将密钥放进 run 快照或事件。

当前 [`MemoryMaintenance`](../../backend/app/memory_maintenance.py) 从已完成的 user/assistant 消息对构造近期上下文和 consolidation 窗口。接入内部 tool message 后，维护层必须显式过滤 tool call/result，并以“用户消息 + 最终 completed assistant”作为可记忆回合；中间工具输出默认只用于当前运行和诊断，不直接触发普通长期记忆提取。

## 实现评估中必须守住的边界

以下不是 Tau 的现成能力，而是把 portable loop 放入 Meido 后必须由宿主补齐的行为：

1. **取消必须穿透阻塞点。** 只在 provider 产出下一条事件后轮询 HTTP 连接状态，无法打断正在等待的网络流。Runtime 需要把取消信号传到 provider/tool 的可取消 await；客户端断开、超时和显式取消都要最终写入 run 终态并收束 assistant。
2. **删除与活动运行要有明确策略。** 角色仍在运行时，删除请求应沿用现有 409 保护；运行终态后再删除。静默删除会留下无法解释的 run、tool transcript 或后续写入竞争。
3. **持久化不能按单个 tool call 行直接重放。** 一个 assistant message 可以包含多个 tool call；SQLite 行格式可以逐 call 存储，但 transcript converter 必须按回合合并 assistant calls，按 call ID 配对结果，丢弃重复/孤立结果，并为悬空调用生成确定性错误结果。
4. **fake loop 与生产工具是两件事。** #75 用 fake provider/fake read-only tool 验证 loop，不等于 HTTP 已开放生产工具。第一版生产 Registry 保持为空；#78 在 #56 完成后再接入角色范围只读记忆工具。

## 推荐的实现边界

建议新增 `backend/app/agent_runtime/`，实现与 Meido 领域隔离的最小协议；参考 Tau 的 loop/event/message 行为，但不把上游模块当作运行时依赖：

```text
agent_runtime/
  types.py       # AgentMessage、AssistantMessage、ToolCall、ToolResult
  events.py      # 生命周期和流式事件
  loop.py        # Tau loop 语义的 Meido 自有实现
  provider.py    # ModelProvider 与取消协议
  tools.py       # ToolDefinition、ToolContext、ToolResult、Registry
  runtime.py     # 角色运行、锁、run id、取消和持久化回调
```

Meido 自己保留：

- 角色 prompt 编译、角色模型快照和 provider 配置；
- SQLite session/message/run 存储；
- 记忆召回、记忆任务账本和 Markdown/向量资料；
- FastAPI/SSE、权限和未来的任务调度；
- 工具 allowlist、审批和审计策略。

**评估结论：**自有 provider-neutral loop seam 按 Tau commit `0c233ff080ba261ec1178e9232301cbb6479e648` 的可验证语义实现；不将完整 Tau 包作为生产依赖，也不逐文件复制当前 `loop.py`。这把 Meido 的锁、事件关联、持久化和取消语义集中在本地接口里。若实现复制或改写可识别的上游代码，必须随实现保留 Tau MIT license/attribution，并在 ADR 说明同步策略。

## 本轮评估后的落地切片

现有 #75 是覆盖范围合适的父级开发 Issue，应实施前复用和细化，不再新开重复 Runtime 核心票。其垂直用户结果是文本对话切换到 Runtime 后仍可用，同时以 fake provider/fake read-only tool 证明一次完整循环。#75 不注册真实工具，也不要求新的前端工具 UI；保留当前 SSE 文本事件，并可附加兼容字段。后续 #78 已存在且依赖 #56，承接角色范围只读记忆工具，不能并入 #75。

基础 Runtime 的生产工具应另开 Ticket。当前已单独落地角色范围 Shell 工具，遵循 [ADR-0004](../adr/0004-role-scoped-shell-tool-boundary.md) 的 allowlist、workspace、最小环境和进程生命周期边界；这不等于引入 Tau/Pi coding 层。只读工具仍必须复用 Meido 的 `MemoryEngine`/资料查询边界，不能借此开放任意文件写入或外部事务。

## 必须先解决的数据模型

### 消息

现有 `messages` 表可向后兼容地增加：

- `message_type`：`text`、`tool_call`、`tool_result`；
- `tool_call_id`、`tool_name`；
- `tool_arguments_json`、`tool_result_json`；
- `is_error`；
- `run_id` 和可扩展的 `metadata_json`。

旧的 user/assistant 文本记录继续可读。provider transcript 与数据库行之间需要显式转换器，不能把 Tau 的 Pydantic 对象直接当 SQLite 模型。

### 运行

在需要取消、恢复和诊断前，增加 `agent_runs`：`run_id`、`role_id`、`session_key`、状态、开始/结束时间、turn_count、取消原因和模型配置快照。事件至少带单调递增的 `run_id + sequence`，避免客户端把不同运行的 SSE 拼在一起。

## 分阶段实施

### Phase 0：契约和 ADR

记录实现参考的 Tau commit、是否复用源码、事件 schema、SQLite 权威性、SSE 兼容策略和“暂不引入 coding 层”的决定。此阶段不改运行行为。

### Phase 1：文本行为平移

先实现 provider/message/event 抽象，但工具列表为空。把现有 OpenAI-compatible adapter 包成 `ModelProvider`，让原有对话仍能产生相同的 `assistant_delta`、完成、失败和记忆行为。通过 fake provider 验证配置快照、角色锁、删除锁、流式失败和重试。

### Phase 2：工具循环

扩展消息表和 provider 解析，使用 fake provider 验证 `LLM -> tool call -> tool result -> LLM -> final`。加入 `max_turns`、取消、请求断开处理；工具异常变成带 `is_error` 的 tool result，由 Loop 决定是否继续。

### Phase 3：Meido 首批工具

角色范围 Shell 工具已作为独立第二阶段能力落地：默认关闭，显式命令 allowlist，固定 workspace、最小环境、超时/取消和有界输出。后续只读工具优先实现角色范围记忆召回、角色上下文读取和主人资料搜索；任意文件写入、浏览器、邮件或其他外部事务仍不在范围内。

### Phase 4：运行时加固

加入 run 状态机、事件序号、启动失败收敛、原子化 transcript 写入、工具审计、超时和 legacy fallback feature flag。再评估是否需要 steering/follow-up；第一版网页对话不必暴露它们。

### Phase 5：长期任务

远程派发、定时执行、审批和外部 Codex/Claude 执行器另建 `TaskRuntime`，不要塞进 Agent Loop。Loop 负责一次运行的推理和工具轮次，TaskRuntime 负责 `queued/running/waiting_for_approval/completed/failed/cancelled`。

## 验收标准

- 现有无工具对话的 SSE 和历史展示保持兼容。
- 角色配置在运行开始时冻结，运行期间切换配置不影响本次请求。
- 同一角色仍不可并发生成，删除角色不会留下活动运行。
- provider 返回工具调用时，工具结果以结构化消息持久化，并能继续下一轮模型调用。
- 工具失败、模型失败、客户端断开和取消都留下可诊断的 run/message 状态；失败 assistant 不进入正常记忆维护。
- 服务重启后 SQLite transcript 可重建 provider 上下文，不依赖 Tau Harness 的内存列表。
- 记忆召回仍发生在运行前，正常完成后才提交 MemoryWorker。
- 首批工具不能越过 Meido 的角色范围和本地安全策略访问任意外部事务。

## 主要风险

1. **消息 schema 不完整**：只改 loop、不改持久化会造成工具轮次重启后不可重放。
2. **provider 能力差异**：OpenAI-compatible 服务对 tool calling、流式参数和错误格式并不完全一致，需要能力探测或明确降级。
3. **上下文预算**：当前实现没有统一 token window；引入工具后必须按完整 turn 截断，避免截断 tool call/result 对。
4. **取消和锁**：HTTP 客户端断开、模型超时和角色删除必须统一触发取消并释放锁。
5. **范围膨胀**：移植 Tau coding 层会把 shell、文件、项目上下文和审批问题一起带入，偏离 Meido 当前产品边界。

## 参考源码

- [Tau repository](https://github.com/huggingface/tau)
- [`tau_agent/loop.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/loop.py)
- [`tau_agent/harness.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/harness.py)
- [`tau_agent/events.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/events.py)
- [`tau_agent/provider.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/provider.py)
- [`tau_agent/tools.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/tools.py)
- [`phase-3-agent-loop.md`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/dev-notes/architecture/phase-3-agent-loop.md)
- [`phase-4-agent-harness.md`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/dev-notes/architecture/phase-4-agent-harness.md)

## Shell/Command Tool 对照（补充核验）

这部分只比较命令工具的执行契约，不能把 coding 层工具误认为 portable Agent Loop 的能力。Tau 的 shell 源码位于 `tau_coding`；Pi 的 shell 位于 coding-agent 包；Codex 的 `exec_command` 则与 sandbox、审批和远程 environment 一起实现。

### Tau `bash`

固定提交 [`5137ad5b705bea654b134ce224328032912ae49e`](https://github.com/huggingface/tau/tree/5137ad5b705bea654b134ce224328032912ae49e) 的 [`src/tau_coding/tools.py`](https://github.com/huggingface/tau/blob/5137ad5b705bea654b134ce224328032912ae49e/src/tau_coding/tools.py) 定义了 `bash` 的输入和本地进程行为：

- `command` 是必需字符串，`timeout` 为可选正数；ToolDefinition 的 schema 还要求一个用于展示的 `description` 字段，但执行器对缺失 description 仍保持兼容。
- 工厂注入固定 `cwd`，相对命令路径由子进程工作目录解释；stdin 连接到 `DEVNULL`。POSIX 下新建 session，超时或取消时杀整个进程组；非 POSIX 平台退化为杀直接子进程。
- stdout/stderr 合并并按 UTF-8 replacement 解码；返回内容取尾部最多 2,000 行或 50 KiB。发生截断时把完整输出写入临时文件，并在 `details` 返回退出码、超时/取消、时长、截断元数据和完整输出路径。
- 超时、取消和非零退出码作为结果文本中的状态信息返回，不是统一抛成 Loop 异常。工具可以收到取消 token，但底层进程终止是否及时取决于该实现。

这套工具没有审批或 sandbox；`tau_agent` 只接收抽象 `AgentTool`，因此上述 cwd、进程组和输出策略不会自动出现在 Meido 的 Runtime 中。Tau 的 [architecture index](https://github.com/huggingface/tau/blob/5137ad5b705bea654b134ce224328032912ae49e/dev-notes/architecture/index.md) 也把本地 shell 明确留在 `tau_coding` 边界。

### Pi `bash`

Pi 固定提交 [`46c9de402bddf46b03c3b9f46487b777aaa41861`](https://github.com/earendil-works/pi-mono/tree/46c9de402bddf46b03c3b9f46487b777aaa41861) 的 [`bash.ts`](https://github.com/earendil-works/pi-mono/blob/46c9de402bddf46b03c3b9f46487b777aaa41861/packages/coding-agent/src/core/tools/bash.ts) 与 [`utils/shell.ts`](https://github.com/earendil-works/pi-mono/blob/46c9de402bddf46b03c3b9f46487b777aaa41861/packages/coding-agent/src/utils/shell.ts) 提供更可替换的 shell 适配器：

- schema 为 `{ command: string, timeout?: number }`；timeout 必须是有限正数，最大约 2,147,483 秒。`BashOperations.exec(command, cwd, { onData, signal, timeout, env })` 可替换为 SSH 或其它远端执行器。
- 默认 shell 跨平台解析：Unix 优先 `/bin/bash`，再查 PATH，最后退回 `sh`；Windows 查 Git Bash/PATH 中的 bash。命令使用固定 cwd，stdin 不与 Agent 交互；Unix 进程组和 Windows process tree 都会在 abort/timeout 时被终止。
- `onData` 流式回调会节流；输出保留尾部最多 2,000 行或 50 KiB，完整输出写临时文件。abort、超时和非零退出会抛出带状态和已有输出的错误。
- shell 环境从宿主环境复制；默认去掉 `PI_SESSION_*`/`PI_MODEL` 等内部变量，再按配置显式注入。`spawnHook` 可修改 command、cwd 和 env。

Pi 的 [security 文档](https://github.com/earendil-works/pi-mono/blob/46c9de402bddf46b03c3b9f46487b777aaa41861/packages/coding-agent/docs/security.md) 明确写明 project trust 不是 sandbox，Pi 默认以启动用户权限运行，也没有内置权限弹窗；隔离要交给容器、VM 或扩展。Meido 不应把 Pi 的 `bash` 直接暴露给角色。

### Codex `exec_command`

Codex 当前官方提交 [`0b863c69f50335acd92164aab971cb58d298c2fe`](https://github.com/openai/codex/tree/0b863c69f50335acd92164aab971cb58d298c2fe) 的 [`shell_spec.rs`](https://github.com/openai/codex/blob/0b863c69f50335acd92164aab971cb58d298c2fe/codex-rs/core/src/tools/handlers/shell_spec.rs) 和 [`unified_exec/exec_command.rs`](https://github.com/openai/codex/blob/0b863c69f50335acd92164aab971cb58d298c2fe/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs) 将命令执行视为完整的受控请求：

- `cmd` 必填；`workdir` 默认当前 turn cwd，且相对路径相对选定 environment 的 cwd；还支持 `shell`、`login`、`tty`、`yield_time_ms`、`max_output_tokens`、`timeout_ms`、`environment_id`。
- interactive 模式可返回 process/session id，再用 `write_stdin` 继续交互；one-shot 模式禁用 TTY，默认 10 秒超时，超时或取消后进程不可恢复。
- `exec.rs` 对 shell-like 输出默认保留最多 1 MiB 字节，仍持续消费管道避免子进程阻塞；实时 output delta 最多 10,000 个。模型看到的内容还会带 exit code、wall time、总行数，并按模型的 truncation policy 截断。
- 超时/取消使用统一 expiration；取消先给进程组约 50ms 清理时间再升级 kill，超时使用确定性超时状态/退出码。空 argv、无效 cwd 等输入在执行前拒绝。
- `sandbox_permissions`、`additional_permissions`、`justification` 和 `prefix_rule` 与 `exec_policy`、`AskForApproval` 一起决定 Allow/Prompt/Forbidden；执行请求还经过 Linux/Windows sandbox、workspace roots 和 network policy。命令输入、审批、sandbox 和输出状态属于同一运行时契约。

因此，Meido 第一版即使只做角色任务，也应复用其中的边界原则：固定且经过授权的工作目录、显式超时和取消、输出字节/事件上限、结构化执行结果、allowlist/审批/审计，以及独立的远程执行器接口。Tau/Pi 的本地 shell 代码本身不能提供这些安全保证；若未来委托 Codex 或其它远端执行器，应放在 `TaskRuntime`/Executor 边界，不塞进 portable Agent Loop。
