# 普通聊天输入用量校准

状态：当前。最后核验：2026-10-30。

事实来源：[Ticket #282](https://github.com/sawaki-zexi/meido/issues/282)、
[`usage_anchor.py`](../../backend/app/agent_runtime/usage_anchor.py)、
[`MeidoProvider`](../../backend/app/agent_runtime/meido_provider.py)、
[`协议输入投影`](../../backend/app/ai/input_projection.py)、
[`行为与 HTTP 测试`](../../tests/python/test_input_usage_calibration.py)。

## 来源与请求边界

| `source` | 含义 | 用量 |
| --- | --- | --- |
| `actual` | 规范化请求完全相同，已有对应供应商输入用量 | 对应单次实际输入 |
| `anchor_delta` | 历史保持前缀，仅合法追加或替换宿主上下文帧 | 实际锚点 + 新旧本地估计差值，最小为 0 |
| `local` | 尚无锚点或不能证明请求可比较 | 当前完整输入的本地估计 |

各适配器的 `prepare_input` 复用实际 HTTP 请求的纯准备函数。协议转换、工具名称别名、缺失结果修复、实际工具声明、响应格式和多模态内容均参与计量，预算估算不发网络请求或获取 Vertex ADC 凭据。模型与连接身份的摘要参与比较，但不算作输入 token，也不记录明文凭据。

历史改写、工具 schema、响应格式、模型/连接变化使旧锚点失效。只有由 Meido 构造、位于静态系统提示之后的明确宿主帧可替换；该帧包含本轮时间、记忆和选中的活动/检索上下文。角色设定、明确传入的系统文本、必需身份约束与能力指令仍参与普通前缀比较。用户在原文中写同样的 marker 不会获得替换资格。

文本采用 Shiori 的保守本地公式，图片按实际协议保留的块估计为 4096 tokens；协议明确发送 low detail 时为 85。图片 base64 长度不当成文字长度。供应商真实 usage 可校准同一图片请求；这些本地值不代表供应商 tokenizer 的精确结果。

## 所有权与生命周期

普通回合在新建 Provider 时绑定共享的进程内存储，依据真实角色、原会话/线程、scope 和已有冻结授权 `authority` 识别上下文；不使用本次 run ID 或消息水位。数据库与角色实例改变也不会复用旧身份。每个上下文只有一个最新 pending ticket，迟到、已撤权、失败和取消响应不能替换较新锚点。

最多保留 64 个最近使用上下文；淘汰时同步丢弃其锚点和 pending 请求。重启后从本地估算重新开始，不持久化锚点，也不以累计账单替代单次输入。摘要请求在准备、预算和响应阶段都排除普通锚点；独立子任务的新 Provider 使用临时存储。硬预算降级的最小请求仍属于原会话，但必须按改变后的真实请求重新比较和计量。

## 供应商 usage 合同

`ModelUsage` 的现有计费字段保持不变。上下文输入按适配器合同计算：

| 协议 | `contextUsage.inputTokens` |
| --- | --- |
| OpenAI Completions / Responses（含 Azure、Codex）、Mistral | `input_tokens` 已含缓存，不能再加缓存字段 |
| Anthropic、Bedrock | `input_tokens + cache_read_tokens + cache_write_tokens` |
| Google Generative / Vertex、Pi Messages | 适配器的 `input_tokens` 是未缓存部分，加回缓存字段一次 |

Google 的原始 `promptTokenCount` 已含 `cachedContentTokenCount`，但 Meido 的既有 Google adapter 在归一化时扣除了缓存；上下文层加回缓存，保留原计费字段。协议定义见 [Google UsageMetadata](https://ai.google.dev/api/generate-content#UsageMetadata)。

当前适配器以全零 usage 表示未提供输入量，因此只有正的总输入建立新实际锚点。无有效输入量时保留请求前的校准估计；不会拿输出、总账或上一次其他请求的输入冒充本请求实际值。

## 预算、诊断与界面

可选上下文选择、压缩触发和发送前硬预算检查消费同一估计。可用输入仍扣除本次实际输出上限和安全余量；自然分条的 `finish_reply` 声明算入正常请求，格式纠正只计实际允许的 `finish_reply`。新 schema 不能作为免费追加。

运行 `budgetDiagnostic` 包含 `inputEstimate` 与 `inputSource`；完成消息的 `contextUsage` 包含 `inputTokens`、`source`、`capacity`、`configurationId`、`model`。响应提供有效 usage 时显示本请求实测，否则显示请求前的 `anchor_delta` 或 `local`。已有上下文弹层沿用“供应商实测输入量 / 锚点估算输入量 / 本地估算输入量”，未知容量保持未知，不生成百分比。这里不包含待发送草稿或累计消耗。

机制适配 Shiori 固定提交 `71c8b0e460530683cd28aad6bb62e873c7167c89` 的
`agent/prompting/{usage_anchor,input_budget,token_estimate,assembler}.py`、`agent/provider.py`
及 `core/roles/model_runtime.py`。Meido 通过显式 Provider 所有权与既有冻结历史授权连接，未移植 ContextVar/角色容器或另建授权系统。分批摘要与 Herdr 不属于本票。
