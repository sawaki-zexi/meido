# Shiori 回复中状态、工具发现与预算降级核验

状态：当前（固定源码研究；末节记录 #277 本地实施验收）  
最后核验：2026-10-30  
关联：[#277](https://github.com/sawaki-zexi/meido/issues/277)  
事实来源：Meido `2b1f2fd`；Shiori-Agent `d149de7f052f77c5bb88d4d1393d48dce1870875`，本机只读 checkout `D:/shiori-agent-audit-20261026`；上游提交日期为 2026-10-24，不声称是最新版本。已在线核对固定提交源码。下文“基线”指实施前，末节另记本地实施与验证；没有调用真实模型、正式账号或读取正式配置数据。

## 结论与证据边界

应分别修复“等待可见”“公开思考及时到达”“预算失效后真实请求没有工具”。最后一项不能仅靠增加工具提示词解决。

主代理只读核查提供的两次正式运行证据为：运行约 201/146 秒，输入本地估计 97,236/98,587，硬输入上限 87,712；压缩失败类型分别涉及 TimeoutError/ValueError，进入 `fallback=minimal`、`toolsDisabled=true`、`toolCount=0`；角色自然分条关闭。此处是主代理提供的脱敏结果，本研究代理没有独立读取正式数据。摘要请求约 29.9k 实际输入、最小正文约 1,413 实际输入，也由主代理提供。摘要和普通请求的系统提示、历史范围、工具声明不同，不能直接以 29.9k 替换普通请求的 98k 估计。

固定 Shiori 的最小请求也会移除全部业务工具，只保留自然回复输出协议；不存在“上游 minimal 始终保留 tool_search”的证据。因此，若 Meido 为当前用户结果保留可装入预算的工具，应明确记录为必要行为差异，并重新计量实际请求，不能称为原样照搬。

## 已提取：首 token 前的 UI 与流式过程

Shiori 在发送调用前设置 session sending，并插入乐观用户消息；聊天头部直接使用 `typing={sending}`，显示输入动画和“正在输入…”。该状态不等待模型首 token 或工具事件。[发送状态](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/app/desktopChatSend.ts#L65)、[头部绑定](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatSurface.tsx#L425)、[输入状态组件](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatHeader.tsx#L64)

公开思考的源头是模型供应商实际返回的 `reasoning_content`。provider 生成 `thinking_delta`，运行时通过 `StreamDeltaReady` 发布，桌面桥将其写入 `chat.delta`，附 session/turn 身份。前端仅更新对应临时 assistant，思考文本进入可折叠块。这里是桌面 bridge 事件，不是 Shiori 的 HTTP SSE；Meido 可以映射到自己既有 SSE，不需要引入桌面桥。[供应商读取](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/provider.py#L769)、[桥投影](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/desktop_bridge/chat_service.py#L419)、[前端合并](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/chatStreamingState.ts#L5)

自然分条回复只缓冲尚未通过协议和 hooks 的正文。`reasoner.py` 在 `buffer_reply` 时仍把 `thinking_delta` 送到原 stream sink；完成分条正文后再持久提交。工具 started/completed 事件也独立于正文。因此不能为了保护 `finish_reply/messages` 原始协议而吞掉公开思考事件。[自然模式缓冲](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/reasoner.py#L303)

Meido 基线已经有完整普通模式链路：`MeidoProvider` 的 `ModelThinkingDelta → ThinkingDeltaEvent`、loop 的 `MessageUpdateEvent.thinking_delta`、main 的 `assistant_thinking_delta`、前端 `streamThinking`。初始 `assistant_generation_started` 在 SSE generator 开头、记忆准备之前就发送；不能错误地认为只有 loop MessageStart 才有占位消息。[Meido provider](../../backend/app/agent_runtime/meido_provider.py)、[SSE 编排](../../backend/app/main.py)、[消息合并](../../web/src/features/chat/useChat.ts)

明确缺口有两个：

- `NaturalReplyProvider._response` 只消费正文、工具、错误与完成事件，没有向外传递 ThinkingDeltaEvent，故自然分条的思考只在最终 metadata 出现。这不是上述两次自然模式关闭运行的原因，但属于相同公开流式契约的缺口。[自然回复包装器](../../backend/app/agent_runtime/natural_reply.py)
- `ChatView` 将“正在回复…”放进 `.reply-content`；`styles.css` 的 `data-stream-replies=false` 同时隐藏 `.reply-content` 和无 `.message-process` 的整个气泡。首段正文/过程到来前，等待气泡因此消失；当前头部没有 Shiori 那样独立的 sending 提示。首个 HTTP 响应到来前也只有发送状态，尚无可靠的独立等待行。[聊天视图](../../web/src/features/chat/ChatView.tsx)、[显示偏好样式](../../web/src/styles.css)

## 已提取：工具初始可见、发现与下一轮加载

Shiori 普通发现模式的初始工具顺序为：当前许可内的 always-on 工具，再接当前未压缩原文窗口中使用或解锁过的 deferred 工具。always-on 元工具不只有 tool_search，还包括文件读写、目录、搜索/抓取、shell 与后台 shell 查询/停止、消息回溯/搜索、推送及已注册记忆工具。注册和来源过滤共同决定实际集合，不能把静态目录当作所有来源的授权。[元工具注册](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/tools/meta/register.py#L50)、[初始顺序](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/tool_visibility.py#L7)

`generate_image` 在该固定 Shiori 基线明确以 `always_on=True` 注册；它不是需要先搜索的 deferred 工具。Meido `_deferred_discovery_tools()` 也没有 generate_image；本次不应为了“对齐按需加载”把生图改成 deferred。插件、凭据和角色许可仍决定它是否实际就绪。[NovelAI 工具注册](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/plugin.py#L149)

对尚不可见工具，Shiori 注入一个名字级目录，按 builtin/MCP server 分组，解释 schema 尚未暴露，并提供 `select:名称` 和关键词两种加载方式。目录排除已可见、always-on、meta 和不允许当前外部来源使用的工具；不把全部 deferred schemas 塞进系统提示。[目录提示](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/helpers.py#L180)

tool_search 搜索最多返回 10 个匹配，默认 5；精确路径区分 newly unlocked、already loaded、missing、risk blocked、external restricted。调用成功后运行时读取结果中的 unlocked，加入当前有序可见集合，下一模型请求才提供其 schema；未经解锁的模型猜测调用会得到先搜索的引导。[搜索工具](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/tools/tool_search.py#L95)、[请求 schema](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/reasoning_loop.py#L217)、[结果扩展](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/reasoning_loop.py#L714)

Meido 已有冻结候选、来源授权、搜索、持久成功后发布和下一请求加载，不能重复建立新工具注册器。`enabledTools=[]` 也不等于实际没有工具，因为宿主默认内置和就绪许可还有自己的装配逻辑。现有 `tool_use_prompt` 仅根据实际 wire schemas 生成“当前可用”，是正确边界；预算已移除 schemas 后只修改提示反而会造成虚假能力。[能力装配](../../backend/app/agent_runtime/capabilities.py)、[发现实现](../../backend/app/agent_runtime/discovery.py)、[实际请求提示](../../backend/app/agent_runtime/conversation_prompt.py)

## 已提取：完整预算、工具恢复与降级

Shiori 对 messages、tools、response_format 的实际规范化输入一起计量，预留输出和安全余量。没有在本次核查路径发现单独的 schema 字节限额；搜索结果数量控制不等于总 token 预算。常驻核心 schema 也计入输入，不存在“核心声明免费”的规则。[本地估计](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/prompting/token_estimate.py)、[输入预算](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/prompting/input_budget.py)

输入估计有请求身份明确的真实 usage 锚点：完全相同请求采用 actual prompt_tokens；可验证前缀追加且工具/格式等不变时采用真实数加本地新旧差值；工具、格式或非追加历史变化即失效并回到 local。辅助摘要与普通回复不能混用锚点。Meido 基线只做本地 ASCII/3、非 ASCII×2 估计，保存过 usage 不等于预算实际使用了它。[Shiori 锚点](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/prompting/usage_anchor.py#L145)、[Meido 估计](../../backend/app/agent_runtime/meido_provider.py)、[Meido 预算](../../backend/app/agent_runtime/context_provider.py)

压缩候选不仅重建历史，也从保留的原文窗口重建历史预加载工具；已经移出窗口的旧 deferred 工具不会无限保持可见。本轮新解锁工具和准确的当前工具结果仍随本轮请求处理。[窗口工具恢复](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/reasoner.py#L640)、[候选 schemas](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/compaction.py#L147)、[窗口原文到工具](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/compaction_render.py#L62)

软阈值触发但原请求在硬上限内时，摘要失败可保持原请求；硬超限才允许 minimal。minimal 清空业务 schemas、去掉检索记忆和技能/工具目录提示，保留当前输入、已有合法摘要、身份约束和已完成工具结果；自然回复 finish_reply 只是输出合同。降级后模型仍请求业务工具会失败，不能继续执行。[软失败与最小请求](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/compaction.py#L133)、[最小 prompt](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/compaction_render.py#L133)、[请求后校验](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/budgeted_request.py#L112)

Meido 另有新增声明 32 KiB、累计声明 128 KiB 的安全容量限制。baseline 不会被静默裁剪；这限制声明字节而非模型 token，不能证明完整输入适合模型窗口。已选择工具从持久 capability selection 恢复，与 Shiori 原文窗口恢复不同。定位本次问题应拆分完整输入中的系统/记忆/历史/schema 成本，避免把错误归因于单一工具目录。[声明预算](../../backend/app/agent_runtime/search_budget.py)、[恢复逻辑](../../backend/app/agent_runtime/capabilities.py)

## 已提取：摘要输出协议的关键差异

两边均使用六个字符串状态字段和非空合法来源 ID 数组；上游确实要求 source_message_ids 来自输入资料，并未改成无来源纯文本摘要。但上游只拒绝缺少必需字段，允许额外字段，再规范化投影为所需字段并去重来源 ID；Meido 基线要求键集合完全相等，额外字段会导致整次摘要失败。[Shiori 校验](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/compaction_summary_validation.py#L94)

Shiori 真实摘要请求传递 `response_format={type: json_object}`，提示给出完整 JSON 模板，禁止 Markdown 包装；预估也使用相同格式参数。Meido 基线只有提示词，没有向 adapter 传结构化输出参数。这是可定位的协议差异，但尚不能证明本次 ValueError 必然是 JSON 错误；目前正式证据只有异常类型。[摘要请求协议](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/compaction_summary_request.py#L87)

Shiori 摘要长度优先使用供应商正文输出 token 上界，缺失时才用规范化本地估计；当投影掉额外字段/重复 ID 且原 usage 超限，也回到规范化文本估计。只有结构完整但太长的合法摘要才缩短一次；格式错误、非法 ID 和截断不作缩短重试。Meido 保存 usage，但摘要是否超长仍一律本地估计，中文内容可能因此被判得更长。[长度计量](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/compaction_summary_validation.py#L120)、[单次缩短](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/compaction_summary_request.py#L104)

上游前缀过大时按可装入请求的连续批次递进重写摘要；单条来源过大时收缩其中长文本。Meido 当前前缀无法装入摘要请求即报错。该机制可解释长期窗口鲁棒性的差别，但批处理会新增付费请求和取消/总预算交互，不能仅为此次两条失败盲目照抄，应先由实际失败阶段和最小复现决定。[摘要批处理](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/compaction_summary.py#L55)、[来源投影](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/compaction_summary_sources.py)

## 最小移植数据流与验证建议（计划）

1. 从本地 sending 起就有可见等待状态；正文实时显示偏好只控制正文，不能隐藏等待、公开思考或工具事实。错误、停止、完成结束状态；不要伪造百分比或阶段。
2. 普通链路沿用现有 thinking SSE；自然回复包装器仅缓冲正文/协议，将真实公开思考透传。对迟到事件保留 run/message 归属校验，不补写模型未提供的内部思维链。
3. 通过现有能力发现 seam 验证“授权就绪 → 初始 schemas/可发现目录 → search 成功持久化 → 下一请求 schema → 实际执行器”。生图核对 always-on 就绪链，deferred 用浏览器/账号/MCP 等真实例子，不重建注册器。
4. 先修摘要输出契约、规范化校验和计量缺口，再评估是否仍频繁落入 minimal。真实 usage 校准只能用于身份匹配的原请求；不可直接借摘要 usage 放宽普通请求，更不能取消硬预算。
5. 如仍需“临时减历史但保留预算内工具”的降级，应写成 Meido 必要差异：重新渲染完整系统和当前输入，完整计量保留 schemas，保留调用/结果配对与来源许可，明确丢失的是临时旧历史而非真实能力；必需块仍装不下就明确失败。不能把业务操作历史当新指令重放。
6. 采用可控 HTTP/adapter 和 React 公共边界验证：首输出前等待、关闭实时正文、自然 thinking 早于 final、额外摘要字段、JSON 请求格式、中文摘要长度、软失败保留工具、硬预算可行/不可行情形、搜索后下一请求、来源/撤权不扩权。未测试真实模型时，不宣称延迟或供应商成功率已经实测改善。

本报告是固定源码行为对照。正式失败链路、修复选择及最终验收由 #277 的实施与测试记录承接；未把上游的所有机制列为本次必须实现的范围。

## #277 本地实施与验收

头部 sending 动画、首段正文前光标、正文显示偏好隔离，以及自然模式公开思考透传已落地。取消时关闭底层生成器，不发布格式纠正草稿；较新输入令计划失效时抑制新公开事件，仍消费完当前模型步骤后释放回合。

摘要 JSON 请求、额外字段投影与来源 ID 去重、按供应商输出用量校验长度已落地。回归专门验证供应商输出超额且本地估计较小时仍缩短，不能无条件取二者较小值。硬预算 minimal 保留能装入预算的已授权 schemas、超额明确失败；此为前述上游差异。

验证使用本机合成数据与 HTTP 模型桩，没有真实供应商或账号：

- 后端 `test_context_windows`、`test_chat_runtime_http`、`test_natural_reply_protocol`、`test_natural_reply_api`、`test_chat_presentation`、`test_model_layer`、`test_pi_openai_adapter`、`test_model_protocol_adapters` 共 160 项通过；`test_chat_stop` 3 项通过。
- HTTP 闭环从硬超限长历史出发，确认 `tool_search` 搜索结果只解锁授权 deferred 工具，下一请求包含该 schema，随后实际执行 deferred 工具及 always-on 生图模拟器并将回执带入最终回复；禁用候选不出现，原历史未删除，失败摘要未提交。
- 前端 `ChatPresentation`、`ToolProgress` 8 项，`useChat` 5 项通过；`npm run build` 包含 TypeScript、网页及桌宠构建，通过。
- 独立静态网页及合成 HTTP/SSE 在 `127.0.0.1:19817` 验证：服务端响应前头部已有输入状态，空正文仍有等待光标；关闭流式正文时思考和工具进度可见，最终正文出现后清理状态；390px 宽度无水平溢出。截图保留于工作区忽略目录 `.data/runtime-feedback-waiting.png` 与 `.data/runtime-feedback-tools-390.png`。

这些结果不证明真实服务延迟下降，也没有实现摘要分批、普通请求 actual/anchor_delta 校准或减少始终可见工具集合。ValueError 的真实细分原因仍待新阶段诊断提供证据。
