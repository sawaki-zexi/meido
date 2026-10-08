# Meido Agent Runtime：Tau Loop 语义接入规格

状态：当前（基础 Loop 已实现，角色 Shell 工具作为第二阶段能力接入）

最后核验：2026-10-07

事实来源：

- Meido 对话入口：[`backend/app/main.py`](../../backend/app/main.py)
- Meido 模型边界：[`backend/app/model_adapter.py`](../../backend/app/model_adapter.py)
- Meido 会话持久化：[`backend/app/session_store.py`](../../backend/app/session_store.py)
- Meido 记忆规格：[`memory-system-spec.md`](memory-system-spec.md)
- Tau 源码调研：[`tau-meido-agent-runtime.md`](../research/tau-meido-agent-runtime.md)
- Tau 官方源码：[huggingface/tau](https://github.com/huggingface/tau/tree/0c233ff080ba261ec1178e9232301cbb6479e648)

维护者已确认以下架构边界，本规格作为 #75 基础实现基线：Meido 自有 Runtime seam、SQLite 为运行与消息权威状态、启动时将未完成运行收束为失败。角色范围 Shell 工具作为独立的第二阶段增量，受本文 5.5 和 ADR-0004 约束；其余实现参数由实现计划确定。

本文定义把 Tau 启发的 Agent Loop 语义接入 Meido 的目标行为和边界。Tau 上游核验提交为 `0c233ff080ba261ec1178e9232301cbb6479e648`；本文只采纳 portable loop 的行为语义，不引入完整 `tau-ai` 依赖。本文不是 Tau 完整项目的接入说明，也不授权直接引入 Coding Agent、外部事务工具或长期任务调度。

## 1. 问题与目标

Meido 当前能够完成一次角色文本回复，但模型无法在一次运行中请求一个受控工具、接收工具结果并继续生成。对话编排、角色锁、消息落库、记忆召回和 SSE 传输也集中在 HTTP 路由中，后续增加工具会让路由继续膨胀。

本规格把一次角色对话运行抽象为独立的 Agent Runtime，并复用 Tau 已验证的 Loop 形态：

```text
用户消息
  -> provider 请求
  -> assistant 文本或 tool call
  -> 顺序执行允许的工具
  -> 保存 tool result
  -> provider 下一轮请求
  -> 最终 assistant 回复
```

目标是：

- 保持现有无工具对话的行为和 SSE 兼容性；
- 支持结构化的 assistant tool call、tool result 和多轮 provider 调用；
- 让运行过程可以取消、限轮、诊断和恢复；
- 让 SQLite 继续是会话和运行状态的权威来源；
- 让角色、记忆和模型连接保持 Meido 自己的领域边界；
- 为未来受控的 Meido 工具提供稳定接口。

## 2. 明确不属于本规格

以下内容不在本次 Runtime 接入范围内：

- Tau 的 `tau_coding`、CLI、TUI 和项目目录发现；
- 任意文件写入、浏览器、邮件、聊天平台和其他外部事务工具；角色范围 Shell 工具由本规格的第二阶段增量定义；
- Tau 的 JSONL/tree session，或以 Tau Harness 的内存 transcript 替代 Meido SQLite；
- 多角色协作、子 Agent、并行工具执行和跨会话共享上下文；
- 定时任务、远程任务、审批工作流和 Codex/Claude 执行器；
- 把记忆维护逻辑塞进 Agent Loop。

远程派发和长期任务以后由独立的 `TaskRuntime` 规格定义。Agent Loop 只负责一次运行内的模型轮次和工具轮次。

## 3. 用户可观察行为

### 3.1 普通文本对话

用户向角色发送消息后，系统仍然：

1. 接受用户消息并写入唯一会话；
2. 使用请求开始时的角色和模型配置快照；
3. 通过 SSE 发送增量回复；
4. 成功时保存完整 assistant 消息；
5. 成功后提交记忆维护任务；
6. 失败、取消或客户端断开时保留可诊断的失败状态，用户消息不丢失。

### 3.2 工具对话

当模型请求一个已注册且被当前角色允许的工具时，系统应：

1. 保存包含工具名、参数和 tool call ID 的 assistant 消息；
2. 在工具策略允许的范围内执行工具；
3. 保存工具结果、错误标记和执行元数据；
4. 将工具结果作为下一轮 provider 上下文；
5. 继续生成，直到模型返回最终回复、运行被取消、达到轮数限制或发生不可恢复错误。

工具执行错误（包括单个工具超时）应转换为结构化的错误 tool result，让模型决定是否继续；provider 请求超时、显式取消和不可恢复运行时错误终止当前运行并释放角色锁。客户端断开必须能取消正在等待的 provider/tool await。

### 3.3 前端兼容

已有客户端至少继续收到：

```text
user_message_accepted
assistant_generation_started
assistant_delta
assistant_completed
assistant_failed
```

新增的 `runId`、turn 序号和工具事件使用可选字段或新增 SSE 事件，旧客户端忽略后仍能完成普通文本对话。

## 4. 目标架构

```text
FastAPI / SSE
    |
    v
RuntimeManager
    |-- 每角色运行锁
    |-- run 生命周期、取消和超时
    |-- 角色/模型快照
    |-- SQLite persistence callbacks
    |
    v
AgentLoop
    |-- provider stream -> provider-neutral events
    |-- assistant message
    |-- sequential tool calls
    |-- tool results -> next provider turn
    |-- max_turns / cancellation
    |
    +--> MeidoProviderAdapter --> OpenAI-compatible model API
    +--> MeidoToolRegistry --> 受控的 Meido tools

Before run: MemoryService recall + prompt/context compilation
After completed final: MemoryWorker / memory maintenance
```

建议的模块边界为：

```text
backend/app/agent_runtime/
  types.py       # provider-neutral message 与运行类型
  events.py      # agent、turn、message、tool 事件
  loop.py        # Tau Loop 语义的 Meido 自有实现
  provider.py    # ModelProvider、provider event、CancellationToken
  tools.py       # ToolDefinition、ToolContext、ToolResult、Registry
  runtime.py     # Meido 角色运行编排
```

`main.py` 只负责 HTTP 输入验证、运行创建、事件转 SSE 和错误响应；角色、会话、记忆和模型配置仍由现有 Meido 服务负责。

## 5. 运行契约

### 5.1 AgentRun

每次用户消息创建一个独立运行：

```text
AgentRun
  run_id: string
  role_id: string
  session_key: string
  status: created | running | completed | failed | cancelled | max_turns
  model_configuration_id: string | null
  model_snapshot: object
  turn_count: integer
  started_at: timestamp
  ended_at: timestamp | null
  cancel_reason: string | null
```

状态转换要求：

```text
created -> running -> completed
                  -> failed
                  -> cancelled
                  -> max_turns
```

尚未开始的 `created` run 可以在请求取消或服务启动恢复时直接进入 `cancelled` 或 `failed`。终态不可再次变为 `running`；同一个 `role_id` 同时最多存在一个 `created` 或 `running` 运行。

### 5.2 Provider

Runtime 不直接依赖 OpenAI SDK 或具体 HTTP 响应。Provider 至少需要表达：

```text
stream_response(
  model,
  system,
  messages,
  tools,
  signal,
  session_id,
) -> AsyncIterator[ProviderEvent]
```

Provider event 至少包括 assistant stream start、text delta、tool call start/delta/end、assistant done 和 provider error。

现有 OpenAI-compatible adapter 作为第一个实现。无法使用工具调用的模型仍可在空工具列表下完成普通文本对话；工具能力不可用时必须返回可诊断错误或明确降级，不能静默丢弃 tool call。

### 5.3 AgentMessage

Loop 内部使用 provider-neutral message；持久化层负责转换：

```text
UserMessage
  content

AssistantMessage
  content
  tool_calls[]
  stop_reason: stop | tool_use | error | aborted

ToolResultMessage
  tool_call_id
  tool_name
  content
  is_error
```

Loop 的内存消息列表只是本次运行的工作集。每个可恢复的 message end 事件都必须先交给持久化层，再继续下一轮 provider 调用。

### 5.4 Tool

工具注册契约至少包括：

```text
ToolDefinition
  name
  description
  input_schema
  risk: read_only | mutating | external

Tool.execute(
  arguments,
  context,
) -> ToolResult
```

Loop 在工具调用边界统一补齐结果诊断字段。`content` 是提供给模型的有限文本；`details` 至少包含状态、耗时、原始输出字符数和截断标志，并保留 Tool 自己提供的领域字段。字段和状态转换见 [`agent-runtime-tool-result.md`](../reference/agent-runtime-tool-result.md)。

`ToolContext` 必须包含 `role_id`、`session_key`、`run_id` 和取消信号。工具不能自行读取全局角色或越过 Registry 访问未授权资源。

首批内建工具保持 `read_only`。角色范围 Shell 工具是唯一的第二阶段 `external` 工具：默认关闭、使用显式 executable allowlist、固定角色 workspace、最小环境、超时/取消和有界输出。它不提供操作系统 sandbox；`mutating`、其他 `external` 工具以及强隔离执行需要另行定义审批、权限、审计和幂等规则。

### 5.5 角色 Shell 工具

角色配置通过 `agentConfig.shell` 控制 Shell 是否可见。`allowedCommands` 为空时 Runtime 不注册 Shell 工具；模型不能指定 cwd、环境变量或 shell。工具结果必须携带状态、退出码、截断标志和输出摘要，拒绝、非零退出、超时和取消均作为错误 tool result 交给下一轮模型。详细决策见 [ADR-0004](../adr/0004-role-scoped-shell-tool-boundary.md)。

### 5.6 AgentEvent

Runtime 内部事件采用以下层次：

```text
agent_start / agent_end
turn_start / turn_end
message_start / message_update / message_end
tool_execution_start / tool_execution_update / tool_execution_end
```

每个事件至少带 `run_id`、`sequence`、`timestamp` 和 `type`。`sequence` 在单次运行内单调递增。

## 6. 持久化要求

### 6.1 messages 扩展

现有 user/assistant 文本记录继续可读。消息表需要增加等价于以下字段的能力：

```text
message_type: text | tool_call | tool_result
tool_call_id: string | null
tool_name: string | null
tool_arguments_json: JSON | null
tool_result_json: JSON | null
is_error: boolean
run_id: string | null
metadata_json: JSON | null
```

具体 SQLite migration 方式由实现计划确定，但旧数据不能因为升级丢失或改变显示顺序。

### 6.2 agent_runs

新增运行表保存本规格 5.1 的字段。运行终态和消息状态更新必须具备幂等性；本规格固定服务重启时将遗留 `created` 或 `running` 运行标记为 `failed`，并收束关联的 `streaming assistant`，不恢复旧进程中的 provider/tool 执行。

### 6.3 provider context 重建

服务重启或继续运行时，必须从 SQLite 重建 provider transcript：

- 排除未完成且无可用内容的失败/中止 assistant，但保留诊断行；
- 将连续属于同一个 assistant 回合的多个 tool call 还原成一个 assistant message；
- 按 tool call ID 把结果紧邻配对到对应调用；缺失结果合成确定性的错误结果；
- 丢弃孤立或重复的 tool result，不把不完整的工具历史交给 provider；
- 上下文裁剪按完整对话回合和完整 tool call/result 配对执行。

## 7. 角色、模型和记忆边界

运行开始时读取并冻结：

- 角色设定快照；
- 角色绑定的模型配置快照；
- 当前会话历史窗口；
- 运行前的记忆召回结果；
- 当前允许的工具集合。

运行期间修改角色、模型连接或工具注册，不影响已经开始的运行。

记忆流程保持在 Loop 外：

```text
BeforeRun:
  MemoryService.recall -> context builder -> AgentLoop

AfterCompleted:
  completed final assistant -> MemoryWorker.submit

Failed/Cancelled:
  不提交正常回合记忆维护
```

工具产生的中间结果默认不直接进入长期记忆；只有最终成功回合或显式记忆工具产生的受保护结果才进入现有记忆契约。

## 8. 上下文与资源限制

Runtime 必须支持：

- `max_turns`；
- provider request timeout（超时后终止 run）；
- per-tool timeout（超时后产生 `is_error=true` 的 tool result，由模型决定是否继续）；
- cancellation token；
- 每角色并发限制；
- 输入上下文预算。

上下文截断按完整回合执行，不能截断 assistant tool call 与 tool result 的配对。第一阶段可以保持现有历史行为并设置保守上限；工具循环上线前必须实现明确的预算策略。

工具默认顺序执行，以保持唯一会话中的确定性和便于审计。并行工具执行属于未来扩展。

## 9. 角色锁、取消和删除

RuntimeManager 接管现有 `role_locks` 的职责：

- 同角色已有运行时，新请求返回 409；
- HTTP 客户端断开时向当前运行发出取消信号；
- provider、tool 和 loop 都必须观察取消信号；
- 取消后释放角色锁并写入终态；
- 角色活动运行期间，删除请求继续返回 409；运行终态后才能删除角色。删除完成后不能留下可继续写入的运行；
- 任何异常路径都必须释放锁。

## 10. 实施阶段与验收

### Phase 0：契约和 ADR

确认 Tau 上游 commit、MIT attribution、消息/event schema、SQLite migration 和工具安全边界，并记录到 `docs/adr/`。运行恢复策略按本规格执行；此阶段不改变用户运行行为。

### Phase 1：文本行为平移

验收：

- 现有普通流式对话产生相同 SSE 事件；
- 模型配置在运行开始时冻结；
- 同角色并发仍返回 409；
- 模型失败、流中断、客户端断开后可继续发送；
- 成功回复仍触发记忆维护，失败回复不触发正常记忆任务；
- 现有 API 和前端测试保持通过。

### Phase 2：工具循环

使用 fake provider 和 fake read-only tool 验收：

- `LLM -> tool call -> tool result -> LLM -> final` 完整运行；
- tool call 和 tool result 重启后可以从 SQLite 重建；
- 工具参数校验失败生成错误 tool result；
- 单个工具超时形成可诊断的错误 tool result；provider 超时、取消、provider error 和 max turns 都有明确 run 终态；
- 运行终态幂等，锁不会泄漏。

### Phase 3：Meido 只读工具

首批实现记忆召回、角色上下文读取和主人资料搜索。验收重点是角色隔离、输入 schema、超时、审计和错误降级。

### Phase 4：运行时加固

完成事件序号、启动失败收敛、原子化写入、工具审计、feature flag/legacy fallback 和上下文预算测试。

## 11. 测试决定

测试外部行为，不测试 Tau 源码的内部实现细节。至少需要：

- fake provider 的文本流、tool call、多轮、错误和取消场景；
- SessionStore 的旧消息兼容、tool transcript 持久化和重建；
- RuntimeManager 的角色锁、删除、超时和终态幂等；
- SSE adapter 的旧事件兼容和新增 run/tool 字段；
- MemoryService/MemoryWorker 与 completed/failed/cancelled 运行的边界；
- provider capability 不支持 tool calling 时的降级行为。

## 12. 已收敛的决定与实现前置审查

以下边界作为 #75 基线，不再作为实现期间的开放选项：

1. 参考 Tau `0c233ff080ba261ec1178e9232301cbb6479e648` 的 portable loop 行为；Meido 自行实现 provider-neutral seam，不依赖完整 `tau-ai`，也不逐文件移植上游 `loop.py`。
2. SQLite 是运行和消息的权威来源；以现有 `messages` 表兼容扩展的结构化列表示 tool call/result，并新增 `agent_runs`。任何可恢复的 message end 在下一轮 provider 请求前持久化。
3. 进程启动时将所有遗留 `created` 或 `running` run 标记为 `failed`，原因写明服务重启；同时将关联的 `streaming` assistant 收束为 `failed`。不尝试恢复旧进程中的 provider/tool 执行。
4. 每个 `run_id` 的事件序号单调递增；既有 SSE 事件名保持兼容，工具生命周期事件可作为新增事件暴露。
5. 本票的生产 Registry 为空。Fake tools 只用于 Runtime 测试；角色范围记忆工具由 #78 在 #56 完成后实现。
6. 客户端断开必须取消当前 Runtime，即使 provider/tool 正阻塞等待下一次输出；取消传播到可取消的 provider/tool 请求，并以终态和失败/中止 assistant 收尾。
7. 角色运行期间，删除请求按当前 API 行为返回 409；运行结束后再删除。不得静默删除或遗留仍能写消息的运行。

本实现固定 `max_turns=8`、provider timeout 为 120 秒、单个 tool timeout 为 30 秒；超时配置留在 Runtime 调用边界，后续再评估产品级配置入口。provider timeout 终止 run，单个工具超时产生错误 tool result。上下文预算保留完整回合与工具配对。现阶段无工具 OpenAI-compatible 路径直接使用现有文本 adapter；非空工具列表明确返回 unsupported，不做静默降级。

#78 继续作为依赖 #56 和 #75 的后续票。
