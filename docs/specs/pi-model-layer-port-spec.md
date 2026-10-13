# Pi 模型层语义移植规格

状态：草案（第一阶段已实现，适配器接入待后续 PR）

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

将 Pi 模型层中与 provider 无关的消息、转换和流式调用语义改写为 Python，并接入 Meido 现有的 Model Adapter 与 Agent Runtime。完成后，Meido 可以在一次模型调用中稳定处理文本、图片、thinking、多个工具调用、工具结果、usage、finish reason 和 provider 错误，同时继续兼容现有纯文本 SSE 和 `ModelTextDelta`/`ModelToolCall` 调用方。

## 2. 范围

### 2.1 必须实现

- 有序的 assistant 内容块：text、thinking、tool call；用户和工具结果支持 text/image 内容。
- provider-neutral 的 user、assistant、tool result 和 system message 类型，保留 provider、api、model、timestamp、usage、stop reason 和错误诊断字段。
- 消息转换器：
  - 将空或空值内容规范化为内容块列表；
  - 对不支持图片的模型插入稳定占位文本，并合并连续占位符；
  - 跨模型重放时降级 thinking，移除只对原模型有效的 thinking signature；
  - 按 provider 能力规范化 tool call ID，并同步改写 tool result ID；
  - 清除 error/aborted assistant 的不完整回合；
  - 为孤立 tool call 合成确定性的错误 tool result；
  - 保持 system message 和工具声明的顺序。
- 类 Pi 的 assistant 事件流：start、text start/delta/end、thinking start/delta/end、tool call start/delta/end、done、error，并提供最终 assistant message 结果。
- OpenAI Chat Completions 兼容适配器：
  - 解析文本和 `reasoning`、`reasoning_content`、`reasoning_text`、`reasoning_details`；
  - 聚合新版 `tool_calls` 与旧版 `function_call`，支持多调用、分片 ID/name/arguments 和完整 message 响应；
  - 解析 usage、cache tokens、reasoning tokens、finish reason、response id/model；
  - 对 DeepSeek 等需要 reasoning replay 的 provider 按能力配置发送兼容字段；
  - 统一 HTTP/SSE 错误并脱敏 API Key。
- 兼容层：现有 `stream_messages`、`stream_structured_messages` 和 Runtime provider 继续可用，新的 rich stream 作为更完整的内部边界。
- 外部行为测试覆盖正常文本、thinking、图片降级、跨模型重放、工具历史修复、多工具聚合、usage/finish reason、错误和取消。

### 2.2 明确不包含

- Pi 的 Anthropic、Google、Bedrock、Mistral、Responses、Pi Messages 等原生协议适配器。
- Pi Coding Agent、CLI/TUI、JSONL/tree session、OAuth、动态模型目录和扩展宿主。
- 改变 Meido 的角色、会话、SQLite、记忆、能力授权或工具安全边界。

自定义服务商和当前预设仍只承诺 OpenAI Chat Completions 兼容接口；未来原生协议应使用独立适配器和独立规格。

## 3. 数据流

```text
Meido Runtime messages
  -> canonical provider-neutral messages
  -> transform_messages(model capabilities)
  -> OpenAI-compatible wire messages
  -> SSE parser / event stream
  -> canonical assistant events + final message
  -> MeidoProvider compatibility events
  -> Runtime / SQLite transcript
```

消息转换发生在发送请求前，不能修改调用方持有的历史对象。响应解析在适配器内完成，Runtime 不读取 provider-specific JSON。

## 4. 验收标准

- [ ] 含图片的历史在支持图片的模型中保留；不支持图片的模型收到稳定占位文本，连续图片不重复插入占位符。
- [ ] 跨模型重放不会发送原模型的 redacted thinking 或 thought signature；普通 thinking 文本可降级为 text。
- [ ] 任意被规范化的 tool call ID 都会在对应 tool result 中使用相同 ID；孤立调用会得到一个 `is_error=true` 的合成结果，孤立结果不会发送给 provider。
- [ ] 一个 assistant 回合中的多个 tool call 保持顺序，并能从分片 SSE、legacy function_call 和完整 message 响应恢复。
- [ ] provider usage、reasoning tokens、cache tokens、response id/model 和 finish reason 可在最终 assistant message 中读取；缺失字段使用明确的零值或 `None`，不抛解析异常。
- [ ] HTTP 400/401/429/5xx、SSE 内嵌 error、无 `[DONE]` 终止和取消分别产生可诊断的统一错误；错误文本不包含 API Key。
- [ ] 现有纯文本对话、Runtime 工具循环、连接测试和前端构建保持通过。
- [ ] `python -m pytest`、本次文件 Ruff、compileall、`npm run build` 和 `git diff --check` 全部通过。

## 5. 实施阶段

1. **Canonical types**：新增 Python 内容块、usage、响应元数据和事件类型，保留旧类型的兼容投影。
2. **Message transforms**：实现图片/ thinking / tool ID / orphan tool history 转换，先以纯函数测试锁定行为。
3. **OpenAI stream**：将现有适配器的请求规范化和 SSE 聚合迁移到 rich event stream，保留旧方法包装器。
4. **Runtime integration**：让 MeidoProvider 消费 rich stream；对现有 SQLite 字段做增量 metadata 持久化，不改变旧消息读取格式。
5. **Verification**：按规范和仓库标准分别 review；确认没有把 Pi 原生协议或 Coding Agent 范围带入。

### 当前实现状态

本变更完成第一阶段和第二阶段的可复用基础：

- `backend/app/model_types.py` 提供结构化内容块、响应 usage/metadata 和 FIFO 事件类型；
- `backend/app/message_transform.py` 提供不修改输入历史的图片、thinking、工具 ID 和孤立工具结果转换；
- `backend/app/model_event_stream.py` 提供终止事件和最终 assistant message 的异步结果；
- 现有 `OpenAICompatibleAdapter` 和 Runtime 的接入留在后续变更，等待工具格式兼容 PR #111 合入后从最新 `main` 开始，避免同一适配器产生堆叠分支。

## 6. 风险和决策

- Pi 的 thinking signature 在不同 provider 上可能是不可解释的 opaque replay data；跨模型必须删除，原模型重放才保留。
- JSON Schema 和工具函数名兼容只能做确定性校验与别名，不能猜测性改写 provider 专有字段。
- Meido 现有 Runtime 的 `AssistantMessage.content` 是字符串。迁移期间 rich block 作为 adapter/model-layer 类型，Runtime 通过文本和工具调用兼容投影消费；只有在持久化和 SSE 需要展示 thinking 时再扩展领域消息。
- 取消必须沿用 Meido `CancellationToken`，不能只在读到下一条 SSE 后检查。
