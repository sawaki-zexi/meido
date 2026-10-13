# Pi 模型层语义移植规格

状态：当前（模型层基础与 provider-specific adapter 已实现）

最后核验：2026-10-13

事实来源：

- Pi `badlogic/pi-mono` `main`：`7d75a0e3eb273dff753d49c066ea29fbe7b3b6f6`
- Pi `packages/ai/src/types.ts`
- Pi `packages/ai/src/api/transform-messages.ts`
- Pi `packages/ai/src/api/openai-completions.ts`
- Pi `packages/ai/src/utils/transcript.ts`
- Pi `packages/ai/src/utils/event-stream.ts`
- Meido [`model-integration-spec.md`](model-integration-spec.md)
- Meido [`agent-runtime-tau-integration-spec.md`](agent-runtime-tau-integration-spec.md)

## 1. 目标

将 Pi 模型层中与 provider 无关的消息、转换和流式调用语义改写为 Python，并接入 Meido 现有的 Model Adapter 与 Agent Runtime。完成后，Meido 可以按模型配置选择对应 wire protocol，在一次模型调用中稳定处理文本、图片、thinking、多个工具调用、工具结果、usage、finish reason 和 provider 错误，同时继续兼容现有纯文本 SSE 和 `ModelTextDelta`/`ModelToolCall` 调用方。

## 2. 范围

### 2.1 必须实现

- 有序的 assistant 内容块：text、thinking、tool call；用户和工具结果支持 text/image 内容。
- provider-neutral 的 user、assistant、tool result 和 system message 类型，保留 provider、api、model、timestamp、usage、stop reason 和错误诊断字段。
- Usage 保留 input/output/cache/reasoning token 以及 provider 返回的可选成本明细；tool result 保留 details、usage、时间戳和有界的 nested-call 诊断，nested calls 只存 transcript、不发送给模型。
- 消息转换器：
  - 将空或空值内容规范化为内容块列表；
  - 对不支持图片的模型插入稳定占位文本，并合并连续占位符；
  - provider、api 或 model 任一变化时按跨模型重放处理，降级 thinking 并移除只对原模型有效的 thinking signature；
  - 按 provider 能力规范化 tool call ID，并同步改写 tool result ID；
  - 清除 error/aborted assistant 的不完整回合；
  - 为孤立 tool call 合成确定性的错误 tool result；
  - 保持 system message 和工具声明的顺序。
- 类 Pi 的 assistant 事件流：start、text start/delta/end、thinking start/delta/end、tool call start/delta/end、done、error，并提供最终 assistant message 结果。
- Provider registry 与协议适配器：
  - 根据配置的 `api` 选择 Pi 源码中定义的十种协议：OpenAI Completions/Responses、Azure OpenAI Responses、OpenAI Codex Responses、Anthropic Messages、Google Generative AI/Vertex、Mistral Conversations、Amazon Bedrock Converse 或 Pi Messages；未设置时兼容旧配置并使用 OpenAI Completions。
  - 每个协议独立转换消息、工具 schema、图片和工具结果，不能把其他协议的请求体直接复用到当前 endpoint；无法表达的 provider schema 约束在请求前报出具体字段。
- OpenAI Chat Completions 兼容适配器：
  - 解析文本和 `reasoning`、`reasoning_content`、`reasoning_text`、`reasoning_details`；
  - 聚合新版 `tool_calls` 与旧版 `function_call`，支持多调用、分片 ID/name/arguments 和完整 message 响应；
  - 对流式工具参数使用 partial JSON 解析；截断对象可继续重放，真正非法 JSON 或非 JSON 参数片段在完成前返回可诊断错误；
  - 解析 usage、cache tokens、reasoning tokens、finish reason、response id/model；
  - 对 DeepSeek 等需要 reasoning replay 的 provider 按能力配置发送兼容字段；
  - 有工具历史但当前工具集合为空时发送 `tools: []`；支持结束原因的端点缺少 `finish_reason` 时失败，不把未完成回合伪装成成功；
  - 统一 HTTP/SSE 错误并脱敏 API Key，包括非流式响应体中的 provider error。
- 兼容层：现有 `stream_messages`、`stream_structured_messages` 和 Runtime provider 继续可用，新的 rich stream 作为更完整的内部边界。
- 外部行为测试覆盖正常文本、thinking、图片降级、跨模型重放、工具历史修复、多工具聚合、usage/finish reason、错误和取消。

### 2.2 明确不包含

- Pi Coding Agent、CLI/TUI、JSONL/tree session、OAuth、动态模型目录和扩展宿主。
- AWS SigV4 和 OAuth 登录/刷新/撤销等凭据生命周期；Codex 接收手动提供的 OAuth bearer token，Vertex 支持 API Key 或 ADC，Bedrock 使用配置提供的 bearer API key。
- 改变 Meido 的角色、会话、SQLite、记忆、能力授权或工具安全边界。

自定义服务商默认使用 OpenAI Chat Completions；原生协议必须通过明确的 `api` 配置选择对应适配器。

## 3. 数据流

```text
Meido Runtime messages
  -> canonical provider-neutral messages
  -> transform_messages(source/target provider, api, model capabilities)
  -> 按 api 选择 provider-specific wire messages
  -> 对应协议的 HTTP/SSE 或 Event Stream parser
  -> canonical assistant events + final message
  -> MeidoProvider compatibility events
  -> Runtime / SQLite transcript
```

消息转换发生在发送请求前，不能修改调用方持有的历史对象。响应解析在适配器内完成，Runtime 不读取 provider-specific JSON。

## 4. 验收标准

- [x] 含图片的历史在支持图片的模型中保留；不支持图片的模型收到稳定占位文本，连续图片不重复插入占位符。
- [x] provider、api 或 model 变化时不会发送原连接的 redacted thinking 或 thought signature；普通 thinking 文本可降级为 text。
- [x] 任意被规范化的 tool call ID 都会在对应 tool result 中使用相同 ID；孤立调用会得到一个 `is_error=true` 的合成结果，孤立结果不会发送给 provider。
- [x] 多个 ID 规范化后发生碰撞时仍生成唯一 ID，且工具结果继续指向对应调用。
- [x] 对文本型工具结果协议，图片会变成可读占位文本而不是被静默丢弃。
- [x] Runtime 经 SQLite 往返后恢复 assistant 有序内容块、text/thinking/tool-call signatures、namespace、provider、api、model、usage 和 stop reason；公开消息不泄露内部 thinking 或 canonical blocks。
- [x] Runtime 与 Pi transcript 保留 tool result details、usage/cost 和时间戳；nested-call 诊断能经 SQLite 重放但不会进入 provider 请求。
- [x] 自定义模型可以选择已注册协议；预设仍使用各自固定协议。
- [x] 错误工具类型和确定无效的嵌套 schema 在请求发送前被拒绝。
- [x] 一个 assistant 回合中的多个 tool call 保持顺序，并能从分片 SSE、legacy function_call 和完整 message 响应恢复。
- [x] 截断的 OpenAI-compatible 工具参数按 partial JSON 恢复；真正非法参数、未知终止原因和缺失必需 `finish_reason` 的响应在完成前转为可诊断错误。
- [x] provider usage、reasoning tokens、cache tokens、response id/model 和 finish reason 可在最终 assistant message 中读取；缺失字段使用明确的零值或 `None`，不抛解析异常。
- [x] HTTP 400/401/429/5xx、SSE 内嵌 error、无终止事件和取消分别产生可诊断的统一错误；错误文本不包含 API Key。
- [x] 现有纯文本对话、Runtime 工具循环、连接测试和前端构建保持通过。
- [x] `python -m pytest`、本次文件 Ruff、compileall、`npm run build` 和 `git diff --check` 全部通过。

## 5. 实施阶段

1. **Canonical types**：新增 Python 内容块、usage、响应元数据和事件类型，保留旧类型的兼容投影。
2. **Message transforms**：实现图片/ thinking / tool ID / orphan tool history 转换，先以纯函数测试锁定行为。
3. **OpenAI stream**：将现有适配器的请求规范化和 SSE 聚合迁移到 rich event stream，保留旧方法包装器。
4. **Runtime integration**：让 MeidoProvider 消费 rich stream；对现有 SQLite 字段做增量 metadata 持久化，不改变旧消息读取格式。
5. **Verification**：按规范和仓库标准分别 review；确认没有把 Pi Coding Agent 范围带入。

### 当前实现状态

本变更完成模型层基础、Runtime rich stream 接入及 provider-specific adapters：

- `backend/app/model_types.py` 提供结构化内容块、响应 usage/metadata 和 FIFO 事件类型；
- `backend/app/message_transform.py` 提供不修改输入历史的图片、thinking、工具 ID 和孤立工具结果转换；
- `backend/app/model_event_stream.py` 提供终止事件和最终 assistant message 的异步结果；
- `backend/app/pi_openai_adapter.py` 实现 OpenAI Chat Completions 的 rich stream 和 complete 调用，解析 reasoning、usage、finish reason、legacy/new tool calls、SSE/HTTP 错误和取消；
- `ModelAdapterRegistry` 按配置协议路由 OpenAI Completions、OpenAI Responses、Anthropic、Google、Mistral、Bedrock 和 Pi Messages；
- registry 也支持 Azure OpenAI Responses、Codex Responses 和 Google Vertex；Vertex 可从 Google ADC 获取凭据，Codex 使用手动提供的 OAuth bearer token；
- `MeidoProvider` 把 rich stream 投影给 Runtime，并从 Runtime transcript 还原模型响应元数据和 thinking；
- Runtime transcript 在 SQLite metadata 中保存有序 canonical assistant blocks，使签名、thought signature、namespace 和 redacted thinking 可在后续模型调用中重放；公开消息过滤这些内部块；
- 自定义连接可显式选择协议，历史配置缺省为 OpenAI Completions。

所有十种协议均通过 HTTP/SSE 或 JSON 转换投影到统一 rich stream。Codex 目前采用 HTTP SSE，不包含 Pi 的 WebSocket、自动重试和 OAuth 生命周期；Bedrock 不包含 AWS SigV4。

## 6. 风险和决策

- Pi 的 thinking signature 在不同 provider 或 api 上可能是不可解释的 opaque replay data；provider、api 或 model 标识变化时必须删除，完全相同的模型连接才可保留。
- JSON Schema 和工具函数名兼容只能做确定性校验与别名，不能猜测性改写 provider 专有字段。
- Meido 现有 Runtime 的 `AssistantMessage.content` 是字符串。迁移期间 rich block 作为 adapter/model-layer 类型，Runtime 通过文本和工具调用兼容投影消费；只有在持久化和 SSE 需要展示 thinking 时再扩展领域消息。
- 取消必须沿用 Meido `CancellationToken`，不能只在读到下一条 SSE 后检查。
