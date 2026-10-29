# Shiori 聊天展示与输入区调研

状态：当前（固定上游源码核验，非运行态验收）  
最后核验：2026-10-29  
事实来源：Shiori-Agent `d149de7f052f77c5bb88d4d1393d48dce1870875`；本机只读 checkout `D:/shiori-agent-audit-20261026` 工作区干净，提交日期为 2026-10-24；已在线核对 [上游仓库](https://github.com/YinFengWindy/Shiori-Agent)。本文不声称该提交是上游最新版本。  
关联：Meido #271，实施基线 `c167cef`。本文的“计划”不代表已实现。

## 结论

截图中的 UI 对应四个已有上游组件：`ChatThinkingBlock`、`ChatToolCalls`、`ChatContextRing`、`ChatEmojiPicker`。工具与思考展示有真实流式事件和持久历史支撑；上下文圆环显示当前输入预算占模型窗口的比例；笑脸是 Unicode emoji 选择器，选择后插入草稿，不会立即发送，也不是角色图片表情库入口。

顶部主体只呈现头像、名称，生成时附短暂“正在输入…”；Shiori 消息布局是角色左、用户右。用户后续已澄清：想将左侧角色列表的介绍替换成 QQ 式最近消息预览，不要求角色气泡右对齐。下文据此补充会话列表的数据流。[顶部源码](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatHeader.tsx#L44)、[消息行源码](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatMessageRow.tsx#L118)

## 已提取：工具与思考的数据流

后端从模型返回的 `reasoning_content` 读取 thinking，并在流式模式中发布 `thinking_delta`。这是供应商实际提供的输出，不是前端根据回复编造的推理，也不是读取模型没有公开的内部状态。[provider.py](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/provider.py#L769)

每个带工具的模型迭代完成后，运行时按顺序追加 `tool_chain` 组，每组含 `text`、`reasoning_content`、`calls`。调用记录包含 `call_id`、名称、原始参数、最终参数、状态、结果预览。最终无工具回复的 thinking 单独放入消息顶层 `reasoning_content`，最终正文放入 `content`；工具链随 assistant 消息保存。[迭代分组](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/reasoning_loop.py#L801)、[消息持久化准备](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/lifecycle/phases/after_reasoning.py#L586)

桌面桥发布 `chat.delta`、`chat.tool.started`、`chat.tool.completed`。事件携带 session 和 turn 身份，工具事件另有 iteration 和 call ID；前端按 turn 找到临时 assistant，按 iteration 分组、按 call ID 更新，避免将后来事件写入别的消息。工具结果桌面预览最多 2,000 字符。这里的截断函数只限制长度，本身不负责凭据脱敏，不应把函数名中的 sanitize 解释成全量秘密过滤。[桥事件](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/desktop_bridge/chat_service.py#L418)、[流式合并](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/chatStreamingState.ts#L96)、[结果限长](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/desktop_bridge/tool_call_preview.py)

历史投影保留组次序、组正文和 reasoning；UI 对有中间内容的链依次显示“该轮思考 → 该轮正文 → 该轮工具”，最后显示最终思考及最终回复。无中间正文/思考时，可合并成单个工具计数。流式 thinking delta 没有 iteration，暂存在顶层累计字段，因此这套上游实现的临时思考布局不等于完整历史的分组交错；最终持久投影恢复完整组结构。顶层历史 thinking 还有 `streamed_reply` 条件：仅流式、标记曾流式或尚无持久 ID 的消息展示；工具组思考不走这一顶层条件。[历史投影](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/desktop_bridge/session_presenter.py#L287)、[展示条件](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/chatMessagePresentation.ts#L10)、[顺序渲染](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatMessageBubbleBody.tsx#L74)

交互上，思考块默认展开，以浅色竖线和较小文字呈现，可折叠；没有内容就不渲染。工具总块在执行中展开，流式结束后收起；每条调用还可独立展开结果，显示名称、参数摘要及成功/失败/执行中状态。最终正文仍单独按 Markdown 渲染。[思考组件](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatThinkingBlock.tsx#L18)、[工具组件](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatToolCalls.tsx#L24)

## 已提取：上下文圆环的准确口径

圆环比例是后端 `tokens / model_context_window`，不是累计 token 消费，也不是生成进度。后端空闲检查会重新装配当前完整持久请求，再测量输入；其中包含模型实际输入所需的消息、提示和工具声明。输出预留、安全余量另有预算字段，不加入圆环分子。前端悬浮/聚焦显示具体 token、窗口和百分比，区分“实际 / 锚点估算 / 本地估算”；未知输入或窗口显示“上下文用量未知”，圆环无填充并显示问号。[服务端检查](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/context_window.py#L57)、[比例口径](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/chatContextState.ts#L90)、[圆环](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatContextRing.tsx#L16)

`actual` 是某个完全匹配当前规范化请求的单次 `prompt_tokens`，不含该次输出，也不是工具多轮之和。若当前请求仅在可验证前缀后追加内容，且工具/格式等配置未变，就使用最近一次真实输入 token 加本地新旧差值，标记 `anchor_delta`；没有可复用锚点则使用 `local`。模型/配置变化不能沿用旧数字冒充实际用量。[锚点算法](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/prompting/usage_anchor.py#L145)、[独立累计计费统计](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/prompting/usage_accounting.py#L40)

上游圆环同时是手动压缩按钮，仅后端允许时可点；压缩状态和可用理由来自服务端。进行中的用量读取只保留同会话最近数字，不串角色；回合终止、模型和上下文变更后刷新。Meido 若本次只要求用量展示，可保留现有压缩入口，不需要为复制圆环另造压缩流程。[刷新生命周期](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/useChatContextStatus.ts#L33)

## 已提取：输入框与表情

输入卡片在聊天底部居中，正文输入区与下方操作行分开；操作行为附件、模型、弹性空白、笑脸、上下文圆环、发送/停止。Enter 发送，Shift/Ctrl+Enter 换行，IME composition 不触发发送。[输入组件](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatComposer.tsx#L163)

emoji 弹层是六列常用 Unicode 表情。选择会替换输入框当前选区，返回新的光标位置，然后关闭弹层；保持原有草稿，随后由发送按钮提交。弹层用 portal 避免被输入卡片裁剪，支持外部点击、Escape 关闭和焦点回到按钮。图片表情库不属于这个按钮的数据来源。[表情组件](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatEmojiPicker.tsx#L85)、[插入算法](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/chatEmojiState.ts#L20)、[弹层行为](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/useChatComposerPopover.ts#L27)

## 已提取：左侧最近消息预览

Shiori `RoleSidebar` 的卡片布局为左侧头像、右侧两行：第一行名称及靠右时间，第二行单行省略的消息正文和可选未读数。不存在 `description` 回退；没有可用消息且没有未读时，第二行不显示。列表直接遍历传入 `roles`，此组件没有按消息时间重新排序。[会话列表](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/roles/RoleSidebar.tsx#L102)

预览规则取最近一条有效用户或角色消息，并非始终取角色最后一句：用户内容前加“你：”，角色正文直接展示。活跃会话倒序扫描，跳过错误消息、仍在流式生成的回复和没有正文/媒体的空过程；纯媒体回退“[图片]”。正文压成单行，Markdown 链接保留可读文字、代码块替换成“[代码]”、标题/列表/强调标记去除。上游仅根据 `has_media` 使用图片回退，并不区分普通文件/音频；Meido 已有附件类型时，应做准确的“[文件]”等回退，不必继承上游这一限制。[预览选取](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/roles/roleChatPreview.ts#L12)、[Markdown 扁平化](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/src/shared/chatPreviewText.ts)

时间按浏览器本地日期显示：今天为 `HH:mm`，昨天为“昨天”，前 2–6 天为星期，本年更早为月/日，往年为年/月/日；空或无效时间不显示。列表每分钟更新用于相对时间标签的 `now`。[时间格式](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/roles/roleChatPreview.ts#L52)、[分钟刷新](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/roles/RoleSidebar.tsx#L88)

刷新有两条路径：

1. 首次加载、重开应用或非活跃角色：`roles.list` 返回每个角色的 `last_message`，字段为 role、正文前 200 字符、timestamp、has_media。后端只从该角色 desktop 可见 threads 读取最新消息，不将未授权渠道历史放进列表。`last_message_preview` 的数据库读取为 limit=1；因此后台预览若遇到无效最新记录，前端只能得到空，不能像活跃会话那样继续向前查找，这属于该基线差异。[角色投影](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/desktop_bridge/role_presenter.py#L56)、[消息预览查询](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/desktop_bridge/session_presenter.py#L174)、[字段限长](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/desktop_bridge/session_presenter.py#L17)
2. 活跃角色：`DesktopAppFrame` 从当前 session messages 计算 `activeRolePreview`，有值时覆盖桥返回的预览。`session.updated` / `chat.message` 直接合并当前会话，完成后预览随状态更新，无须逐 token 请求角色列表。非活跃角色的相同事件安排一次 1 秒后 `roles.list` 刷新，时间窗口内事件合并；这不是每秒持续轮询。组件卸载会清理定时器。未读数只对不可见的主动桌面回复增长，不能把所有渠道更新都算成桌面未读。[活跃预览传递](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/app/DesktopAppFrame.tsx#L443)、[事件驱动刷新](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/app/useDesktopBridgeLifecycle.ts#L198)

Meido 本次计划应覆盖刷新后的持久预览与当前会话即时预览，不只在已经打开过的角色上用内存缓存替换简介。测试应验证首次加载、发送后的更新、切换角色和重新载入后仍显示正确最后消息，以及无消息/图片/文件的回退。

## Meido 落地建议（计划）

- 复用现有角色、聊天回合、模型和工具事件；对齐展示契约，避免引入第二套 runtime 或凭据路径。
- 回复内呈现可折叠的供应商公开思考内容/摘要和工具记录。只渲染实际收到的数据；没有公开内容时不捏造过程，不请求未公开的内部思维链。失败、中断和重新打开会话后仍应准确显示工具状态与归属。
- 用真实上下文预算字段驱动圆环，明确估算来源；若现有接口只提供最后请求值，标签必须说明“上次请求”，不能宣称当前完整上下文。缺少容量时显示未知，不硬编码模型窗口。
- 角色顶部移除简介等长文，保留头像及名称；左侧会话列表改为最近消息预览，正文消息左右排布保持现状。列表预览沿用可见历史范围，使用轻量持久查询，正确跟随活动及后台消息刷新。
- 笑脸入口先实现 emoji 草稿插入与发送；图片表情素材若另有需求，再明确其选择和发送契约。
- 在已有公共测试边界验证工具/思考归属、历史回读、上下文真实/估算/未知、emoji 光标插入与发送、精简顶部；不以静态截图替代数据链路验收。

本报告没有运行 Shiori GUI，也没有使用真实模型凭据或发送外部消息。若复制上游实现代码或实质片段，应保留其 [MIT 许可证](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/LICENSE) 和 Copyright (c) 2026 YinFengWindy；本报告提供行为证据，不要求直接拷贝组件。
