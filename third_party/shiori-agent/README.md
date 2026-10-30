# Shiori-Agent attribution

状态：当前。最后核验：2026-10-30。

## 单次输入用量校准（2026-10-30）

`backend/app/agent_runtime/{usage_anchor,meido_provider,context_provider}.py` 与
`ai/input_projection.py` 适配固定提交 `71c8b0e460530683cd28aad6bb62e873c7167c89` 的
`apps/backend/agent/prompting/{usage_anchor,input_budget,token_estimate,assembler}.py`、
`apps/backend/agent/provider.py` 和 `apps/backend/core/roles/model_runtime.py`：
actual / anchor_delta / local、有界 64 上下文 LRU、最新请求响应归属、显式宿主帧与辅助请求隔离。
`ComposerTools.tsx` 沿已有上下文详情增加同版 `chatContextState.ts` 的锚点估算来源。
Meido 使用原生协议 preparation、显式 Provider 所有权和既有冻结授权视图，不复制上游宿主或持久累计账单。
合同与差异见[输入用量校准](../../docs/reference/input-usage-calibration.md)。
Copyright (c) 2026 YinFengWindy，MIT 许可见 [LICENSE](LICENSE)。

## 工具可见性与调用指引（2026-10-30，后续核验）

`backend/app/main.py` 与 `agent_runtime/{capabilities,discovery,search_metadata,conversation_prompt,meido_provider}.py`
适配固定提交 `21e68d99d88f421e798fabc4c5d9f8c1b4ad454f` 的
`apps/backend/agent/tools/{registry,tool_search}.py`、`tools/meta/register.py`、
`core/passive_turn/{helpers,reasoning_loop}.py`、`apps/backend/prompts/agent.py`、
`bootstrap/toolsets/` 及各插件 `backend/plugin.py`：常驻/按需、风险声明、未加载名称目录、
关闭按需时保留核心搜索、按功能发现与委托/记忆/历史调用决策。

文件、shell、记忆、spawn、schedule、group、network、pet/screen/computer/coding 工具定义
按该提交对应工具源码适配 `function.description` 与已有参数说明；浏览器28项和电脑8项
JSON 声明逐项核对。Copyright (c) 2026 YinFengWindy，MIT 许可见 [LICENSE](LICENSE)。

Meido 保留 4096字节目录、原来源/owner/file-root/服务许可；此前 ADR-0026 的执行冻结与恢复缓存已由下述 ADR-0032 替代，
通用进度 `description` 参数协议按下述工具调用意图条目适配，不注册未实现工具。
完整名称、源码链接、风险和执行差异见[本轮报告](../../docs/research/shiori-tool-exposure-and-guidance-2026-10-30.md)。
此前条目中的固定 SHA 继续表示各自历史来源，不被本次核验覆盖。

## 回复中反馈与摘要协议（2026-10-30）

`web/src/features/chat/ChatTypingSparkles.tsx`、`ChatView.tsx` 及相关 CSS 适配固定提交
`d149de7f052f77c5bb88d4d1393d48dce1870875` 的
`apps/desktop/renderer/src/chat/ChatTypingSparkles.tsx`、`ChatHeader.tsx`、`ChatMessageRow.tsx` 及星形路径 `packages/sdk/src/icons/brand.tsx`：
发送状态驱动头部星光、输入提示与首段正文前的光标。`backend/app/agent_runtime/natural_reply.py`
按同版 `apps/backend/agent/core/passive_turn/reasoner.py` 保留正文缓冲同时透传公开思考；
`context_provider.py` 与 OpenAI adapter 按 `apps/backend/core/compaction_summary_request.py`、
`compaction_summary_validation.py` 适配摘要 JSON 格式、规范化投影和输出长度口径。
Copyright (c) 2026 YinFengWindy，MIT 许可见 [LICENSE](LICENSE)。

Meido 沿用 SSE、取消与授权边界；硬预算最小请求保留能够装入预算的已授权工具，
这是区别于上游清除业务工具的行为。没有复制上游摘要批处理、token 校准或插件宿主。
精确源码链接与证据限制见[本轮研究](../../docs/research/shiori-live-reply-tool-loading-2026-10-30.md)。

## 工具调用意图（2026-10-30）

`backend/app/agent_runtime/tools.py` 适配固定提交
`71c8b0e460530683cd28aad6bb62e873c7167c89` 的
`apps/backend/agent/tools/registry.py`：模型 schema 仅在工具没有业务 `description`
参数时注入必填短意图，实际执行业务参数前移除宿主字段。
保留原有同名业务参数和上游 5–12 字提示，不额外强制拒绝缺少意图的调用。
Copyright (c) 2026 YinFengWindy，MIT 许可见 [LICENSE](LICENSE)。
调用链核验与 Meido 适配见[研究记录](../../docs/research/shiori-tool-call-intent-2026-10-30.md)。

## 聊天思考与工具展示（2026-10-29）

`web/src/features/chat/ChatThinkingBlock.tsx`、`ChatToolCalls.tsx`、`ChatReplyMetrics.tsx`
及 `chat-presentation.css` 移植、适配固定提交
`d149de7f052f77c5bb88d4d1393d48dce1870875` 的
`apps/desktop/renderer/src/chat/ChatThinkingBlock.tsx`、`ChatToolCalls.tsx`、
`ChatReplyMetrics.tsx`、`ChatMessageRow.tsx` 和 `apps/desktop/renderer/src/styles.css`。
保留布局、折叠、动画和 Phosphor 图标；Tailwind 样式转换为普通 CSS，数据接入 Meido
原生 runtime/SSE 与公开历史投影，另加凭据过滤与键盘可访问性。
Copyright (c) 2026 YinFengWindy，MIT 许可见 [LICENSE](LICENSE)。
Phosphor 图标由 `@phosphor-icons/react` 包提供，遵循其随包 MIT 许可。

本项目的 `backend/app/network_tools.py`、`backend/app/network_extract.py` 改写了
[Shiori-Agent](https://github.com/YinFengWindy/Shiori-Agent) 提交
`4f266aeaf42fcd611362e2456417b91f659341cf` 中的
`apps/backend/agent/tools/web_fetch.py` 和 `web_search.py`：业务参数、默认值、
Exa MCP 请求结构和 HTML 转换选项。上游版权和 MIT 许可见 [LICENSE](LICENSE)。

Meido 自行适配 AgentTool、角色授权、宿主搜索配置、DNS 固定连接、逐跳校验、
流式容量控制、可终止解析进程和取消协议；不引入上游工具宿主、数据库或身份系统。
`backend/app/agent_runtime/discovery.py` 另遵循同版 `tool_search.py` 的查询、精确选择、
风险筛选行为；当前执行与原文恢复按下述较新基线对齐，真实来源及持久化协议由 Meido 实现。
更新以人工核对固定上游版本和公开行为测试为准，不自动同步。

## Skill 指令适配

`backend/app/builtin_skills/` 的 weather、summarize、meme-manage、skill-creater 改写自同一固定上游的 `apps/backend/skills/`，保留 MIT 授权。Meido 的 frontmatter、角色 workspace、CLI 版本和权限行为差异见 `docs/reference/shiori-builtin-skills.md`。

## 桌宠格式与播放器

`backend/app/desktop_pet/packages.py` 适配同一固定提交的 `plugins/desktop_pet/backend/package_archive.py`、`package_images.py`、`pet_packages.py` 中的包格式、图片和动作规则；`desktop-pet/renderer/spriteContract.ts`、`main.tsx` 适配 `plugins/desktop_pet/surface/spriteContract.ts`、`CodexSpritePetRenderer.tsx` 的 atlas、节拍及播放逻辑。Copyright (c) 2026 YinFengWindy，MIT 许可见本目录 LICENSE。

Meido 自行实现角色授权、受管 stdio/Windows Job、Electron 主进程与 preload、动作回执和配置界面；不包含上游语音、气泡或通用 surface 宿主。Windows 本地发行目录保留本声明与 Electron 自带的 LICENSE、LICENSES.chromium.html。

## 角色表情回复

`backend/app/channels/role_reactions.py` 适配固定提交
`d3acaed186b172a78a5892e0307e9affd89759f0` 的
`plugins/meme/backend/runtime.py` 中 `RoleReactionCatalog` / `RoleReactionDecorator`：
角色分类发送许可、普通聊天的末尾 `<meme:分类ID>` 协议与每次回复最多一张插画。
Meido 沿用自身的冻结候选、附件引用、原来源撤权与真实渠道投递回执，不复制插件框架。
MIT 版权与许可见本目录 [LICENSE](LICENSE)。

## Grok Bot 回程与交流协议

`backend/app/channels/grok/client.ps1` 和 `protocol.py` 改写自 Shiori-Agent 固定提交
`d3acaed186b172a78a5892e0307e9affd89759f0` 的 `plugins/grok_bot/client.ps1` 和
`plugins/grok_bot/backend/protocol.py`。同目录 `adapter.py`、`exchange.py`、`config.py`
适配该提交的 `plugins/grok_bot/backend/connection.py`、`exchange.py`、`config.py`、
`transport.py` 中固定 Bot、回程、交流预算与接收回执行为。
Copyright (c) 2026 YinFengWindy，MIT 许可见本目录 [LICENSE](LICENSE)。

Meido 使用自身账号生命周期、SQLite 收据和渠道投递账本；未知父消息拒绝、访客权限及
`transport_acceptance` 的宿主边界见 [ADR-0030](../../docs/adr/0030-fixed-grok-bot-exchanges.md)，
不引入上游插件宿主或默认授予完整工具权限。

## Story 剧情模式

`backend/app/story/` 与 `web/src/features/story/` 的剧情状态、时间、导演协议和播放辅助逻辑适配同一固定提交
`d3acaed186b172a78a5892e0307e9affd89759f0` 的 `plugins/story/{backend,ui}`。
来源清单见 [UPSTREAM.md](../../backend/app/story/UPSTREAM.md)，MIT 许可另保留于
[SHIORI-LICENSE.txt](../../backend/app/story/SHIORI-LICENSE.txt)。模型、NovelAI、角色快照、媒体地址与路由沿用 Meido 原生运行时。

## 普通自然聊天与多消息提交

`backend/app/agent_runtime/natural_reply.py`、`backend/app/channels/reply_sequence.py`
适配固定提交 `d3acaed186b172a78a5892e0307e9affd89759f0` 的
`apps/backend/agent/core/passive_turn/natural_reply.py`、
`apps/backend/agent/turns/reply_sequence.py`、`reply_sequence_commit.py` 以及
`apps/backend/prompts/natural_chat.py`：`finish_reply(messages)` 输出协议、一次有限格式纠正、
独立消息提交与发送间隔。角色开关对应 `apps/backend/core/roles/natural_chat.py`。
Copyright (c) 2026 YinFengWindy，MIT 许可见本目录 [LICENSE](LICENSE)。

Meido 保留自身普通工具循环、角色／来源授权、桌面 SSE、SQLite 消息及真实渠道回执；
旧有无需 @ 的群聊主动接话为独立保留功能，不作为上述普通自然聊天的实现。

## 当前工具注册表与原文窗口恢复

`backend/app/agent_runtime/{capabilities,discovery,loop,tool_window,context_provider}.py`
适配固定提交 `71c8b0e460530683cd28aad6bb62e873c7167c89` 的
`apps/backend/agent/tools/{registry,execution}.py`、
`apps/backend/agent/core/passive_turn/{helpers,tool_visibility,compaction_render,reasoning_loop}.py`：
按当前名称查实现、最终参数检查、原文窗口 used/unlocked 名称恢复及同回复顺序解锁。
Meido 保留自身工具协议、真实角色/来源、原消息回执与任务归属；不整体移植上游 SDK。
决定与验证见 [ADR-0032](../../docs/adr/0032-current-tool-registry-and-raw-window.md)。
Copyright (c) 2026 YinFengWindy，MIT 许可见本目录 [LICENSE](LICENSE)。
