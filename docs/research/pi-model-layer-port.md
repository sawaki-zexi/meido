# Pi 模型调用层研究与移植边界

状态：当前研究记录  
事实来源：Pi 官方源码固定提交 [`7d75a0e3eb273dff753d49c066ea29fbe7b3b6f6`](https://github.com/earendil-works/pi/tree/7d75a0e3eb273dff753d49c066ea29fbe7b3b6f6)  
最后核验：2026-10-13

> `badlogic/pi-mono` 当前已重定向到 `earendil-works/pi`。以下链接均固定到该提交，避免跟随后续主分支变化。

## 结论

Pi 的模型层把“运行时消息”和“供应商线协议”明确分开：`Context` 先由 `normalizeContext()` 折叠成带首个 system message 的 `TranscriptContext`，各 API adapter 再把同一份 transcript 转成自己的请求结构。所有 adapter 都返回同一个 `AssistantMessageEventStream`，以 `start`、内容块增量、`toolcall_*`、`done`/`error` 结束；最终的 `AssistantMessage` 保留结构化内容、工具调用、思考签名、usage、stop reason 和错误详情。

对 Meido 最有价值的不是照搬某个服务商 payload，而是复制这三个边界：

1. 先把历史消息修复成可重放的 transcript，再做 provider-specific 转换。
2. 让工具调用、思考、图片和错误都经过统一事件协议，不在 HTTP/SSE 解析层直接暴露给 Agent Runtime。
3. 将兼容能力作为模型元数据（是否支持图片、严格 schema、mid-conversation system/tool changes、reasoning、usage 等），由 adapter 根据能力裁剪请求，而不是对所有 OpenAI-compatible 服务发送同一组字段。

## 统一类型与请求生命周期

`packages/ai/src/types.ts` 定义了 10 个已知 chat API：OpenAI Completions/Responses、Azure Responses、Codex Responses、Anthropic Messages、Bedrock Converse、Google Generative AI/Vertex、Mistral Conversations 和 Pi Messages；API/provider 类型可扩展为任意字符串。[`types.ts:61-130`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/types.ts#L61-L130)

通用请求选项集中处理取消、api key、可注入 fetch、provider 环境、payload/response 回调、headers、timeout、retry 和 retry-delay cap。`onProviderStreamEvent` 是可选的、只读的原始事件观察点；sampling 参数只对 OpenAI-compatible adapters 生效。[`types.ts:151-232`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/types.ts#L151-L232)

核心消息模型如下：

- `system` 可以携带文本、命名 sections、增量 `toolsAdded` 和 `toolsRemoved`；这允许 transcript 在对话中改变提示词和工具集合。[`types.ts:531-557`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/types.ts#L531-L557)
- `user` 支持文本和 base64 图片；`assistant` 的 content 是 text、thinking、toolCall 的有序块。[`types.ts:559-596`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/types.ts#L559-L596)
- `toolResult` 绑定 `toolCallId`/`toolName`，支持文字、图片、错误标志、工具自身 usage 和有界的 nested calls；nested calls 只保存在会话记录，不送回模型。[`types.ts:598-639`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/types.ts#L598-L639)
- `Usage` 明确区分 input/output/cacheRead/cacheWrite/reasoning，并携带按模型价目计算的 cost；reasoning 是 output 的子集，不应再次相加。[`types.ts:446-469`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/types.ts#L446-L469)

公共入口有 `stream()`/`complete()` 和简化的 `streamSimple()`/`completeSimple()`。`ModelsImpl` 在真正发请求前解析 auth、合并 headers、应用 `transformHeaders`，并把认证得到的 base URL 回写到 request model；`complete*()` 只是等待事件流的 `result()`。[`models.ts:850-922`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/models.ts#L850-L922)

`createProvider()` 支持一个 provider 对应一个 adapter，也支持按 `model.api` 分发；缺失对应 adapter 时返回异步 stream error，而不是在构造 provider 时静默失败。[`models.ts:1002-1132`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/models.ts#L1002-L1132)

## Transcript 与消息转换

`normalizeContext()` 是唯一生成 `TranscriptContext` 的入口：将 `Context.systemPrompt`/`tools` 变成首个 system message，防止 raw context 直接进入 provider。[`transcript.ts:6-34`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/utils/transcript.ts#L6-L34)

transcript helper 会：读取首个 system message、重放所有 system 更新得到当前 prompt/tool set、折叠不支持中途 system message 的 provider、比较工具声明并把变更表示为 remove+add。[`transcript.ts:42-119`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/utils/transcript.ts#L42-L119) [`transcript.ts:122-164`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/utils/transcript.ts#L122-L164)

`transformMessages()` 处理跨模型重放的危险状态：不支持图片的模型把 image block 替换为去重的占位文本；跨模型时丢弃 redacted thinking 和 Google thought signature，把普通 thinking 转为 text；根据 provider/model/api 对工具调用 ID 做规范化，并将映射同步到后续 tool result；缺失 tool result 时插入 `No result provided` 错误结果；error/aborted 的不完整 assistant turn 不重放；system message 会在 tool call 与结果之间暂存，避免破坏 provider 要求的相邻顺序。[`transform-messages.ts:12-57`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/transform-messages.ts#L12-L57) [`transform-messages.ts:64-155`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/transform-messages.ts#L64-L155) [`transform-messages.ts:158-234`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/transform-messages.ts#L158-L234)

这里的 ID 规则是关键兼容点：OpenAI Responses 可能生成 450 字符以上、带 `|` 的 ID，而 Anthropic 要求 `^[a-zA-Z0-9_-]+$` 且最长 64；因此规范化只在跨模型转换时应用，同模型重放保留原始 opaque signature。[`transform-messages.ts:59-67`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/transform-messages.ts#L59-L67)

## 事件流协议

事件协议要求 `start` 先于所有 partial update，最终只能是 `done` 或 `error`；`partial` 是共享的 response-so-far 对象。text/thinking/tool call 都有 start/delta/end 生命周期；toolcall start 的初始 arguments 由 provider 决定，后续 JSON 通过 delta 累积。[`types.ts:785-817`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/types.ts#L785-L817)

`EventStream` 用 FIFO 队列同时支持已产生事件和等待中的异步消费者；`push()` 遇到 complete 事件立即 resolve `result()`，`end()` 唤醒所有等待者。`AssistantMessageEventStream` 在 done/error/end 时以 monotonic clock 写入 `durationMs`，不会覆盖已有 duration 或 deferred/旧消息时间戳。[`event-stream.ts:3-89`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/utils/event-stream.ts#L3-L89) [`event-stream.ts:91-135`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/utils/event-stream.ts#L91-L135)

## Native API adapters

### OpenAI Chat Completions

`openai-completions.ts` 根据 compat metadata 构造 `messages`、`tools`、`stream_options.include_usage`、`max_tokens`/`max_completion_tokens`、tool choice、cache fields 和 provider-specific thinking 字段；当历史包含工具回合时，即使当前没有工具声明，也可发送 `tools: []` 以满足 Anthropic-over-proxy 等兼容层。[`openai-completions.ts:802-875`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/openai-completions.ts#L802-L875)

流处理按 index/id 聚合 tool-call fragments，支持标准 `function.arguments` 和 grammar/custom tool input；reasoning 兼容 `reasoning_content`、`reasoning`、`reasoning_text`，并从 `reasoning_details` 保存可重放签名；结束时解析 partial JSON、清理 scratch fields、检查 finish_reason，缺失结束原因会转 error。[`openai-completions.ts:386-555`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/openai-completions.ts#L386-L555) [`openai-completions.ts:558-730`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/openai-completions.ts#L558-L730)

### OpenAI Responses 与 Azure Responses

Responses adapter 用 `convertResponsesMessages()`/`convertResponsesTools()` 生成 `input` 和工具定义，按 compat 决定 additional tools、tool search、strict mode、grammar tool、mid-conversation system messages；reasoning 会映射 effort/summary 并请求 encrypted content；ChatGPT 登录态会去掉不接受的 cache、temperature 等字段，`max_output_tokens` 至少为 16。[`openai-responses.ts:127-235`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/openai-responses.ts#L127-L235) [`openai-responses.ts:302-388`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/openai-responses.ts#L302-L388)

Azure adapter 复用相同 Responses message/event converter，但把模型 ID 换成 deployment name、解析 Azure base URL/api-version，并对 API key、采样、工具和错误做自己的边界处理。[`azure-openai-responses.ts:35-153`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/azure-openai-responses.ts#L35-L153) [`azure-openai-responses.ts:174-233`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/azure-openai-responses.ts#L174-L233)

### OpenAI Codex Responses

Codex adapter是 Responses 协议的独立 transport：优先 WebSocket，连接限制、缺失 previous response 等特定错误有限重试，连接在尚未发出 start 前失败才降级 SSE；SSE 请求支持 zstd body 和 retry-after/429/transient network retry。SSE/WebSocket 都经过同一 `processResponsesStream`，最终统一为 assistant event stream。[`openai-codex-responses.ts:238-498`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/openai-codex-responses.ts#L238-L498) [`openai-codex-responses.ts:661-789`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/openai-codex-responses.ts#L661-L789)

Codex 把 provider `error`/`response.failed` 转成结构化异常，保留 code/payload；usage-limit 会产生用户可读的额度错误，而不是泛化成 400。[`openai-codex-responses.ts:744-794`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/openai-codex-responses.ts#L744-L794) [`openai-codex-responses.ts:1597-1621`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/openai-codex-responses.ts#L1597-L1621)

### Anthropic Messages

Anthropic adapter先调用 `transformMessages()`，再决定是否折叠中途 system message。tool-call ID 会变成 64 字符内的安全字符；tool result 必须紧邻对应 tool use。工具 schema 会根据 strict 支持情况过滤不支持的 JSON Schema 关键词，并以 `input_schema` 发出；OAuth token 还会插入 Claude Code identity system block。[`anthropic-messages.ts:571-649`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/anthropic-messages.ts#L571-L649) [`anthropic-messages.ts:1157-1322`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/anthropic-messages.ts#L1157-L1322) [`anthropic-messages.ts:1325-1390`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/anthropic-messages.ts#L1325-L1390)

其 SSE parser 支持 CR/LF、注释、多个 data 行、尾部未带空行的事件，要求已收到 `message_start` 时最终必须收到 `message_stop`；解析失败包含 event/data/raw 诊断。[`anthropic-messages.ts:379-569`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/anthropic-messages.ts#L379-L569)

### Google Generative AI 与 Vertex

两者都把中途 system message 折叠到首个 system instruction，使用 Google `contents`/`parts`、function declarations 和 `toolConfig.functionCallingConfig`；支持 strict sampling 时才发送严格 schema。thinking 使用 includeThoughts/thinkingLevel 或 token budget；functionCall 在收到时立即形成完整 toolCall，并为缺失或重复 ID 生成唯一 ID，同时保留 thought signature。[`google-generative-ai.ts:59-109`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/google-generative-ai.ts#L59-L109) [`google-generative-ai.ts:175-251`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/google-generative-ai.ts#L175-L251) [`google-generative-ai.ts:369-428`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/google-generative-ai.ts#L369-L428)

Vertex 复用同一 Google response shape，但认证可走 Vertex API key 或 ADC/project/location；自定义 fetch 不被支持。两者均要求 finish reason，usage 将 cached prompt tokens 从 input 中扣出并把 thoughts 计入 output/reasoning。[`google-vertex.ts:68-120`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/google-vertex.ts#L68-L120) [`google-vertex.ts:462-525`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/google-vertex.ts#L462-L525)

### Mistral Conversations

Mistral adapter拥有独立 wire 转换：内部 camelCase 字段改为 `tool_calls`、`tool_call_id`、`image_url` 等 snake_case；assistant tool call 的 arguments 始终 JSON.stringify，tool result 总是带可读文本，图片不支持时追加明确 placeholder/error 前缀。它自己的 SSE parser 按事件边界读取流，并把 stop reason 映射成统一值。[`mistral-conversations.ts:386-455`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/mistral-conversations.ts#L386-L455) [`mistral-conversations.ts:576-640`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/mistral-conversations.ts#L576-L640) [`mistral-conversations.ts:808-945`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/mistral-conversations.ts#L808-L945)

Mistral tool-call ID 会根据原始 ID 和 attempt 派生兼容 ID；图片能力通过 model metadata 决定，不能把所有 OpenAI image_url payload 原样透传。[`mistral-conversations.ts:218-270`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/mistral-conversations.ts#L218-L270)

### Amazon Bedrock Converse

Bedrock adapter 折叠中途 system message，构造 `ConverseStreamCommand` 的 `messages`、`system`、`inferenceConfig`、`toolConfig`、cache points 和 additional model request fields。它处理 AWS profile/region/ARN、SigV4 或 bearer token、VPC/proxy/custom endpoint；自定义 headers 通过 Smithy middleware 注入以确保签名覆盖。[`bedrock-converse-stream.ts:127-289`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/bedrock-converse-stream.ts#L127-L289)

Converse 事件按 messageStart/contentBlockStart/contentBlockDelta/contentBlockStop/messageStop/metadata 处理，错误事件转异常；tool result、图片、redacted thinking 和 stop reason 都有独立转换。[`bedrock-converse-stream.ts:298-352`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/bedrock-converse-stream.ts#L298-L352) [`bedrock-converse-stream.ts:582-757`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/bedrock-converse-stream.ts#L582-L757) [`bedrock-converse-stream.ts:976-1200`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/bedrock-converse-stream.ts#L976-L1200)

### Pi Messages

Pi Messages 是 Pi 自己的 HTTP/SSE wire contract：POST `{baseUrl}/messages`，body 包含 model、完整 `context` 和 options；SSE 每个空行分隔块取 `data:` JSON，必须收到 done/error，否则生成 terminal-event error。converter 将 provider 事件直接映射为统一 text/thinking/tool call 生命周期，并累积 tool JSON。[`pi-messages.ts:180-320`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/pi-messages.ts#L180-L320) [`pi-messages.ts:323-444`](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6/packages/ai/src/api/pi-messages.ts#L323-L444)

## 对 Meido 的兼容风险与移植顺序

1. **先补 transcript 层。** Meido 当前把 provider history 近似成 role/content；若不保留 assistant 多 content blocks、tool call/result 配对、thinking signature 和 system/tool state，DeepSeek 或其他 reasoning/tool provider 的下一回合会再次出现 400。
2. **统一事件而非在 adapter 返回字符串。** 最低限度应有 text delta、thinking delta、tool call start/delta/end、done/error；tool arguments 必须在 adapter 内增量解析并在结束时校验 JSON object。
3. **工具 schema 做 provider capability transform。** OpenAI 的 `parameters`、Anthropic 的 `input_schema`、Google 的 function declaration、Bedrock 的 toolSpec 和 Mistral 的 wire key 不可直接互换；strict/unsupported keywords、工具名/ID 限长和 image support 都应由模型能力控制。
4. **错误要保留服务端详情但脱敏。** Pi 将 HTTP/provider protocol errors 变成带 stopReason/errorMessage 的最终事件；可重试的 transport 错误和确定性的 schema/400 错误必须区分，API key 不进入日志或 UI。
5. **Simple API 只能是映射层。** `streamSimple()` 应将 reasoning level/toolChoice/maxTokens 映射到完整 provider options，再调用同一 `stream()`；不要复制另一套消息转换或流解析。
6. **分阶段移植建议：** (a) 统一 transcript/事件类型；(b) OpenAI-compatible Completions + DeepSeek；(c) OpenAI Responses/Azure；(d) Anthropic；(e) Google/Mistral/Bedrock；(f) provider-specific transport、cache、deferred 和 classifier/image APIs。

## 研究限制

本记录核对了请求边界、消息转换、工具/思考/图片/usage、SSE/WebSocket 生命周期和错误语义；未把每个 provider 的完整模型 catalog、全部 compat schema 字段或所有 SDK 类型逐字段复制。后续实现应以固定源码链接和相应 provider 官方 API 文档再次验证具体 payload。
