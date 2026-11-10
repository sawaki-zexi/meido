# 分批工作摘要

状态：当前。最后核验：2026-11-10。
事实来源：[分批选择](../../backend/app/agent_runtime/summary_batches.py)、[宿主来源投影](../../backend/app/agent_runtime/summary_sources.py)、[压缩事务入口](../../backend/app/agent_runtime/context_provider.py)、[行为验收](../../tests/python/test_batched_summary.py)。需求：[Ticket #283](https://github.com/sawaki-zexi/meido/issues/283)、[Spec #279](https://github.com/sawaki-zexi/meido/issues/279)、[来源预算修复 #351](https://github.com/sawaki-zexi/meido/issues/351)。

## 输入与批次

现有完整轮选择先确定本次应移出的原文。固定 Shiori `71c8b0e460530683cd28aad6bb62e873c7167c89` 的 `core/compaction_summary{,_sources,_request}.py` 提供批次语义：先尝试全部剩余原文，装不下时二分选择当前辅助输入预算内最长的连续前缀。每次把上一批已验证摘要作为工作状态重新计量，再处理下一批，直到覆盖全部选定原文。

输入只含文本、附件名称与媒体类型、工具调用和结果及必要来源关系，不序列化图片 Base64 或完整模型元数据。工具结果先按上游 10000 字符规则保留首尾；单条仍放不下时，对其中的长字符串从 10000 字符预算逐次减半，保留首尾、截断数量和行数说明。原来源由宿主完整保留，模型侧按下节投影；工具调用 ID 保持原样。循环下限是 200，实际依次尝试 10000、5000、2500、1250、625、312，不尝试 156；固定字段和最低可用文本仍放不下时明确报告压缩失败，不跳过该条原文。

每批复用六字符串字段及 `source_message_ids`、合法来源校验、供应商结构化输出选项和输出长度规则。当前绑定仅含此前合法摘要与本批实际展示原文的来源，不准入未展示的后续批次。只有结构合法但超长的结果可以缩短一次；缩短也是真实请求。摘要提示要求保留仍有效的较早任务与结果，不能只摘要最新一批。

### #351：有界来源集合

宿主将本批来源绑定为一个固定长度的 `sources:` 集合引用；原文与此前摘要均使用该引用，输出 `source_message_ids` 只能包含这一个引用。`message_refs` 区分具体发言，并投影已识别的来源/引用关联字段；说话人和平台 ID、工具调用号、正文中的真实业务标识不替换。未知、跨范围、旧调用、旧批次或未展示来源不能通过输出来源校验。范围外引用关系使用 `unavailable:` 短标记保留区别，不增加授权。

校验和计量在模型可见的有界状态上进行，通过后由宿主恢复完整原始来源供原有事务保存。持久化仍为旧的六字段和完整来源数组，无迁移；来源数量不再无界占用摘要输出。后续请求、只读检查、最小窗口和重启共用稳定的有界回放投影，再计量最终实际输入，不展开回原 ID 列表。设计取舍见 [ADR-0038](../adr/0038-bounded-working-summary-provenance.md)，HTTP 验收见[来源回归](../../tests/python/test_summary_sources.py)。

## 提交与失败

批次状态只保存在当前调用内存中。先等待完整切点的既有记忆覆盖，再请求摘要；所有批次成功后按最终窗口和当前工具声明重新计量，由原有事务核对来源权限、原文指纹、窗口版本和覆盖证明，一次提交摘要及水位。没有中间摘要表或持久化批次流水，原始历史不删除。

后批异常、非法来源、停止、请求步数用尽、覆盖不足或最终预算失败都不提交半成品。软阈值失败且原请求仍可容纳时继续使用原文；硬阶段的生成、校验和覆盖错误直接传播。只有合法候选预算不足或供应商溢出等允许路径进入最小请求，保留当前输入、合法摘要和已执行结果，移除业务 schemas，保留回复协议并重新计量；必需内容仍放不下则失败。摘要不会恢复已被移出原文窗口的工具，压缩候选与正文使用相同窗口选择规则，当前规则见 [ADR-0036](../adr/0036-shiori-context-memory-ownership.md)。

## 计量与验证

预检和发送共用实际适配器的纯 `prepare_input`、输出预留及 JSON 选项。每次摘要计量只用 local，不写普通对话的 actual/anchor_delta 锚点；压缩后普通请求按新历史与声明重新计量，再由其自身响应更新锚点。没有锚点也可完成分批。

每次批次生成及缩短调用均消耗现有 `maxIterations` 请求步数，并记录独立实际 usage、结束原因与耗时；继续保留正文请求所需的一个请求名额。每次辅助请求沿用 90 秒期限和取消检查，正文沿用自己的期限。多批可能增加调用数与总等待时间，不保证更快，不增加无限重试。

### #292：可见正文与缩短反馈

[协议计量](../../backend/app/ai/output_usage.py) 在丢失原始字段的有效性之前保留严格计数，普通聊天的既有 `output_tokens` 显示不变。有效整数总输出扣除有效且小于总量的 reasoning，作为正文上界；总量为零、缺失或非法，以及观察到 thinking 但没有明确细分时，使用规范化正文的本地估算。OpenAI Completions、Responses（含 Azure／Codex）与 Pi 采用总输出／reasoning 的合同。Anthropic 使用最终 `message_delta` 的 `output_tokens_details.thinking_tokens`；没有细分时不猜测。Google 的 `candidatesTokenCount` 独立于 thoughts，因此直接作为可见正文上界，不重复扣除。Bedrock 与没有有效细分的 Mistral 在存在 thinking 时回退本地估算。协议出处见[调研](../research/chat-responsiveness-shiori.md)。

[摘要校验](../../backend/app/agent_runtime/summary_validation.py) 先检查六个字符串字段和当前合法非空来源数组，再去重来源、投影必需字段。供应商上界超限且投影去掉了额外字段或重复来源时，以规范化正文重新计量。首次和缩短请求都提供完整 JSON 模板；仅合法但超长时重试一次。缩短载荷只含完整规范化 `summary` 和 `budget_feedback`（`counted_tokens`、`count_source`、`hard_limit`、缩短说明），不重新发送原文批次。第二次仍超长则失败，按上述软/硬阶段规则处理，不继续重试。

安全诊断保留 `limit`、`batch`、`shortening`、`providerOutputTokens`、`localTokens`、`countedTokens`、`sourceCount`，以及白名单 `countSource`、`fallbackReason` 和 `outcome`。`sourceCount` 为当前完整原来源数量，不是集合引用数。结果可区分 accepted／oversized、非法 JSON、缺字段、字段类型、来源、截断、超时、未完成及输入容量失败；诊断不包含摘要正文、来源 ID、集合映射或上游错误原文。持久化后的高级设置诊断同样保留这些安全字段。手动长度失败通过现有 `reason` 显示实际计量值、上限、供应商可见输出上界或规范化本地估算，以及包含 JSON 和有界引用的口径。

[#292 行为回归](../../tests/python/test_summary_accounting.py) 验证合法 2500 总量／1500 reasoning 不发生额外请求、无依据用量回退、规范化投影、一次反馈缩短及失败保留原文；[协议回归](../../tests/python/test_output_usage.py) 验证各适配器实际流式终态的计量语义。

受控 HTTP 验证覆盖单批/多批、最长前缀及来源顺序、旧摘要续写、文本/附件/工具结果收缩、最低输入失败、后批错误/非法来源/取消/步数耗尽、每批一次缩短、软硬失败、提交前工具变化、授权/原文/版本/覆盖门槛、工具窗口回收及普通锚点隔离。已有共享主人历史与真实 loopback HTTP 工具续接回归继续适用；这些测试不调用付费模型或真实渠道账号。
