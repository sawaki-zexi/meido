# 角色插画自动表情

状态：当前（#261 实现分支）。最后核验：2026-10-28。

`channels/role_reactions.py` 适配 Shiori 固定提交
`d3acaed186b172a78a5892e0307e9affd89759f0` 的
`plugins/meme/backend/runtime.py` 中 `RoleReactionCatalog` / `RoleReactionDecorator`。

## 普通回复与自然 QQ

- 仅把当前角色 `illustrationCategorySend[分类名] == true` 且包含真实有效原图的分类加入普通回复提示。默认无许可；心情绑定、背景选择和生图入库均不授予发送许可。
- 模型在最终回复末尾选择一个 `<meme:分类ID>`。分类 ID 由宿主生成，名称只是数据；宿主随机选一张该分类的冻结候选，不接受模型给出的文件路径。流式与最终正文隐藏协议，短文字继续正常流式显示。
- 桌面把原图的附件引用保存进同一完成回复；QQ、Telegram、飞书、QQBot 沿用普通 `account_send` 附件投递。渠道启用分类时暂缓平台文字预览，完成后一次提交文字和图片，避免先发文字后另起不可追踪的图片投递。现有平台目标、格式、大小和 QQBot 被动回复限制不变；不支持媒体的渠道不提供协议。
- 图像生成工具已经附图时保留其原始投递，避免自动表情替换或重复生成图片。
- 自然 QQ 复用原有当轮候选 ID、图片冷却、有限预算与一次投递；只把已授权角色插画加入原有候选，不新增工具或收录权限。
- 每次选择冻结文件身份/内容摘要、原角色和分类；引用准备及实际投递前重新检查原来源、账号/角色、分类许可和插画归属。附件字节沿用 `ChatMemeLibrary.reference` 冻结后交给原投递链路，未完成或撤销不选择替代图。
- 渠道只保存实际 `sent/failed/uncertain` 回执及分段结果。重复入站不重发，不把未知结果当失败自动重试；引用恢复只清理本次持有的暂存文件，不补发、不恢复已移出的原图。引用创建的 `reaction` 来源不冒充主人命令。

## 主动消息边界（与固定上游一致）

角色表情装饰器并不应用于 Shiori 的 proactive / Drift 草稿：

1. `plugins/meme/backend/plugin.py:113–117` 仅注册 `PromptRendering` 与 `ReasoningFinished`；后者由 `agent/core/passive_turn/pipeline.py` 的 AfterReasoning 阶段触发。
2. `agent/turns/orchestrator.py:269–398` 的 `handle_proactive_turn` 校验已冻结的 `RoleReply` 并直接投递其 content/media，不发出 `ReasoningFinished`。
3. 同文件 `_handle_natural_reply`（约 404–409 行）明确写明 “Proactive drafts have no content decorators”，直接消费已校验消息列表。
4. Drift 的 `proactive_v2/agent_tick_factory.py:292–346` `_build_drift_send_message_fn` 也调用 `handle_proactive_turn`。

因此此功能不修改 Meido 的 `proactive_runtime` final_draft、Drift 图片权限、去重或投递协议；原有显式附件流程仍有效。不能把普通回复的分类许可转为自主运行授权。

MIT 版权与许可保留于 `third_party/shiori-agent/LICENSE`。
