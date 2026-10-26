# 工具响应的完成证据

工具仍顺序执行。任何工具执行事件、hook 或外部动作之前，Runtime 对整个 assistant
batch 做一次完成性门禁；`length`、`aborted`、`error`、未知/缺失终态或任一不完整参数
均拒绝整批，不自动重试。此前已经完成的工具结果不受影响。

拒绝批次记录为失败/取消的 assistant 文本，metadata 保留 provider 结束原因、
`tool_batch_rejected` 和调用 ID/名称/参数完整性摘要，不伪造 tool result 或执行审计。
partial JSON 的修补仅供临时展示，`arguments_complete=False` 不取得执行资格。
合法空对象 `{}` 仍是完整参数。宿主取消与 provider 中止分别记录来源。

## Adapter 合同

| 协议 | 完成证据 |
| --- | --- |
| OpenAI Completions | `[DONE]` **且**正常 `finish_reason`，参数严格 JSON 成功 |
| OpenAI/Azure/Codex Responses | `response.completed`（显式异常 status 仍拒绝），完整函数参数 |
| Anthropic Messages | `message_stop` **且**正常 `stop_reason`，完整 input JSON |
| Google Generative / Vertex | 正常 `finishReason`，functionCall.args 对象 |
| Mistral Conversations | `[DONE]` **且**正常 `finish_reason`，完整参数 JSON |
| Bedrock Converse | `messageStop` **且**正常 `stopReason`，完整 input JSON |
| Pi Messages | 显式 `done` 和正常 reason，toolcall_end 的完整对象或严格 JSON |

`supports_finish_reason=False` 保留无工具文本兼容，但不把 `[DONE]` 推定成工具
语义完成。确实使用完整 `choice.message` 的兼容服务可显式选择
`OpenAICompletionsOptions(tool_completion_contract="complete_message")`；仍须 `[DONE]`、
每个调用的完整 message 快照及完整 JSON。任何已知失败原因都优先拒绝。

旧 `stream_structured_messages` 自定义 adapter 需要在调用后发送公开
`ModelResponseDone("tool_use")`（或正常 `"stop"`）作为语义完成承诺；仅 iterator EOF
默认不准入工具。内置 legacy adapter 已发送该证据。无工具的普通文本 EOF 仍兼容。

对照 Pi `d5b3de22` 的整批 length 拒绝与 error/aborted 终止，但不复制其合成工具失败
结果、默认并行或可变 hook。本实现保持 SQLite message_end 先持久化再请求下一轮。
