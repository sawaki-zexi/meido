# 多端会话来源与应答关系：实施后对照

- 状态：当前分支实施核验；关联 [Issue #338](https://github.com/sawaki-zexi/meido/issues/338)，交付状态以关联 PR 的 CI 和审查为准。
- 最后核验：2026-11-09。
- 规格：[多端会话身份与应答关系改进方案](../specs/multichannel-identity-and-reply-context-spec.md)。
- 事实来源：本分支源码、隔离数据及受控模型的实际请求/投递回归，以及下列固定上游源码。未运行真实渠道或付费模型效果采样。

## 核验版本

Meido 从 main 的 98d6a31 开始实施。发布 [PR #339](https://github.com/sawaki-zexi/meido/pull/339) 时，main 合入 [857333d / #337](https://github.com/sawaki-zexi/meido/commit/857333dbb72e7b8ea12ede8f043015c694c37b0c) 的按需技能能力，本分支已整合该主线；模型提供层保留技能窗口与来源观察两条路径，受影响链路另做定向验证。Tulpa 的复核版本仍为 6753a28。Shiori 复核时 main 已从方案基线 249043d 前进至 467a97d；[两者差异](https://github.com/YinFengWindy/Shiori-Agent/compare/249043d653cd707d6556b737db445ec80495094a...467a97d1749b6f5aa7929f80c4304bc3732e989b)只有 Fish Audio 相关文件，下表引用的来源、输入保护及回复代码未变。

## 核验结论

本期将实际输入来源、模型选择的回应关系和实际投递分别保存。主人 @ 角色请她回答群友时，模型可以选择群友的消息、成员或话题；宿主核验可见范围与执行一致性，不要求回应对象等于触发者，也不按“回答了别人的问题”直接判错。

自动回归验证的是模型收到哪些事实、可提交哪些合法选择、平台实际收到什么，以及后续如何重建这些关系。真实模型是否理解请托、指代和参与时机，仍需按完整语境进行效果采样；目前没有误回率改善的测量结果。

实现入口：[共同来源及回应选择](../../backend/app/channels/conversation_context.py)、[自然流程](../../backend/app/channels/natural_turn.py)、[回复协议](../../backend/app/agent_runtime/natural_reply.py)、[历史回放](../../backend/app/session_store.py)、[群资料整理](../../backend/app/channels/consolidation.py)。

## 与参考实现逐项对照

| 参考机制 | Meido 本次实现与证据位置 | 核验边界 |
| --- | --- | --- |
| Shiori 的[统一来源快照与模型标签](https://github.com/YinFengWindy/Shiori-Agent/blob/467a97d1749b6f5aa7929f80c4304bc3732e989b/packages/sdk/python/shiori_sdk/channels/message_source.py) | conversation_context.message_source/project_record 读取已记录的发言人、规范身份、接收角色平台身份、原账号/聊天、真实 @、引用及应答关系；普通输入、旁听、历史和资料整理共用。 | 不用当前昵称或配对反推旧身份；缺失字段保留未知。 |
| Shiori 的[独立背景与当前输入](https://github.com/YinFengWindy/Shiori-Agent/blob/467a97d1749b6f5aa7929f80c4304bc3732e989b/apps/backend/agent/context.py)、[输入所有权恢复](https://github.com/YinFengWindy/Shiori-Agent/blob/467a97d1749b6f5aa7929f80c4304bc3732e989b/apps/backend/agent/core/passive_turn/minimal_request.py) | 普通群旁听进入已有动态背景帧；当前 UserMessage 单独保留。复用已有 ContextWindowProvider 的工具续接、重绘和最小请求保护，新增宿主 source_context 供摘要使用。 | 用户正文或尾部 user 资料块不决定来源；已签名的思考/工具块保持原样。 |
| Tulpa 的[对角色/对其他人线索](https://github.com/fumingyang2004/Tulpa/blob/6753a28a9757c93c0ad34ed54064965df2cb081c/chatlocal/mcp_chat_prompts.py) | 有界时间线同时包含显式提问、旁听和已确认角色发言，保留角色真实平台 ID、@、引用作者和已发回应关系；按实际时刻排序。 | 指向线索供模型理解，普通入口沿用原触发规则；自然参与仍可回应、等待或沉默。 |
| Tulpa 的[目标、话题和意图规划](https://github.com/fumingyang2004/Tulpa/blob/6753a28a9757c93c0ad34ed54064965df2cb081c/chatlocal/mcp_chat_turns.py) | 显式群回复可用 finish_reply.response 选择消息、成员或话题，关闭分条聊天时也可用；自然回复先规划再表达，表达阶段改选后重新核验并生成。 | 候选来自实际发送给模型的宿主投影；正文仅提到旧编号不授予可见性。已核验引用依附真实父消息，不补写虚构原始记录。 |
| Tulpa 的[按计划发送与新鲜度检查](https://github.com/fumingyang2004/Tulpa/blob/6753a28a9757c93c0ad34ed54064965df2cb081c/chatlocal/mcp_chat_turns.py) | RelatedTopic 跟踪所选逻辑目标、引用链、参与者转向及少量话题词；检查规划期间和表达期间的新输入。原批次额度、期限、角色调用间隔继续生效。 | 无关流量不延长期限；两次实际请求分别计数，等待/沉默不生成正文，失效稿件不派发。 |
| Tulpa 的[发送请求与自身回显关联](https://github.com/fumingyang2004/Tulpa/blob/6753a28a9757c93c0ad34ed54064965df2cb081c/chatlocal/mcp_chat.py) | responseRelation 贯通分段、平台请求、回执和后续历史；逻辑对象、请求引用/@ 与实际显示分别记录。只投影已确认的完整或部分内容；引用降级按回执重建。 | 结果不确定不自动重发；无显示回执的旧数据不补成“已显示引用”。选择成员不改变原发送账号/聊天或工具权限。 |
| Shiori 的来源感知历史与身份边界 | 工作摘要、群笔记/成员资料、主人事实提取采用共同来源；引用作者与当前发言者分开。相关来源指纹增量带入投影版本，复用现有授权与缓存失效。 | 群友、引用和角色推测不因上下文出现而成为主人事实。授权原文缺失时继续明确标记，不清空用户历史或重建记忆。 |

Meido 继续使用现有线程、角色模型、调度和投递运行时。Tulpa 的常驻 MCP Agent、语言学习及新增群互动属于其他产品能力，本次范围已在规格中排除。Shiori 当前普通群入口要求 @ 或回复角色；Meido 依既有配置保留自然参与。

## 验收证据

下表只标记可自动验证的来源与执行行为。A01–A08、A18–A19 的真实模型理解效果单独列入后文，不能由受控模型的正确决定代替。

| 编号 | 自动验证的行为与主要证据 |
| --- | --- |
| A01 | [实际入站/请求回归](../../tests/python/channels/test_conversation_relations.py)分开当前请求与旁听，真实请求明确 @ 对象、自身平台身份及 other 指向。 |
| A02 | [群主人身份](../../tests/python/channels/test_group_owner_identity.py)验证不同发送者的当前身份进入模型，不把成员认作主人。 |
| A03 | 同组引用身份/工具权限回归验证引用作者不替代触发者，不授予主人工具。 |
| A04 | [自然两阶段](../../tests/python/channels/test_natural_conversation.py)的等待/沉默仅规划，不产生正文、空消息或业务工具。 |
| A05 | 自然时间线同时呈现旧显式提问、角色已发回复及新发言人，允许选择较早目标。 |
| A06 | 同例验证所选目标对应成员资料进入表达请求，另验证改选后重新生成。 |
| A07 | [自然调度](../../tests/python/channels/test_natural_scheduler.py)验证明确转向后无标记补充脱离原话题，引用后重新加入。 |
| A08 | [共同投影](../../tests/python/channels/test_conversation_projection.py)和引用回归保留旧字段、失效引用、未知作者及不可读附件状态。 |
| A09 | 关系回归重新打开真实存储，历史引用身份与应答关系保持；[自然生命周期](../../tests/python/channels/test_natural_turn.py)验证无旧发送重放。 |
| A10 | 共同投影参数化 QQ、飞书、QQBot 的同数字/同名及账号作用域，检查实际模型消息。 |
| A11 | 外群本地 ID 拒选；平台 ID 仅在原账号/线程解析，不跨来源选择。 |
| A12 | 自然表达期间纠正使旧稿失效；原调度和生命周期回归覆盖无关消息、显式抢占与撤权。 |
| A13 | 生命周期回归覆盖开关关闭、清空、连接换代及延迟入站，不恢复旧输入。 |
| A14 | 部分确认正文可选且可复验，未知尾部不入模型；原回显与[分段投递](../../tests/python/channels/test_natural_reply_delivery.py)回归保持不补发。 |
| A15 | 实际外部整理、主人事实提取、私聊整理窗口和工作摘要请求保留来源、引用和回应关系。 |
| A16 | 最低来源包无法完整容纳时零请求、零投递；[摘要裁剪](../../tests/python/test_context_windows.py)保留宿主来源，旧身份保持未知。 |
| A17 | [授权历史 API](../../tests/python/test_owner_history_view_api.py)、[引用附件](../../tests/python/channels/test_owner_quoted_attachment.py)、Telegram 实际 @/引用降级及群上下文检查保持原范围。 |
| A18 | 关闭分条聊天仍能回应另一成员；未单独入库的文本、附件和空正文引用可合法派发；明确 null / [] 不被默认覆盖。 |
| A19 | 运行和回应记录分别保存真实触发者、所选对象与执行来源；原普通分段/补充及自然与显式交错回归保持原归属。 |
| A20 | 最小请求不因正文旧编号放行未展示候选；工具续接、签名回放及引用附件仍保留真实归属。 |
| A21 | 两阶段按真实请求计数，保留默认间隔；原三次额度内改选/重判，取消准备不扣调用，不发送失效草稿。 |

### 本地检查与 CI

本地使用隔离数据和受控模型。日志位于开发工作区的 .data/development/identity-338-checks/；下列次数按各次检查记录，存在重叠，不累加为唯一用例数。未变化的成功检查按仓库规则复用。

| 日志 / 检查 | 结果与处置 |
| --- | --- |
| channels.log | 101 passed；来源、自然调度/媒体、群身份及整理回归。 |
| context-and-compatibility.log | 108 passed；另两项旧用例的定位/隔离修正后定向通过，见下文。 |
| final-targeted.log、final-fixes.log | 51 passed；群 inspection 冻结来源修复与相关断言另 3 passed。 |
| timeline-offsets.log | 1 passed；混合 UTC / +08 时间线的最终请求。 |
| quote-followup-green.log | 45 passed；授权回放、附件/空正文引用、显示与真实平台目标。 |
| private-memory-quote.log | 14 passed；私聊整理窗口、群/主人整理及摘要来源。 |
| natural-quote-followup.log | 31 passed；自然规划/表达、媒体与引用回归。 |
| main-integration-provider-skills.log | 110 passed；整合 #337 后的技能发现/生命周期、上下文准备/检查及签名回放。 |
| main-integration-group-natural.log | 37 passed；整合后的显式发送、自然两阶段、主人身份与回复协议。 |
| git diff --check | 通过。 |

最后三组的准确命令：

```powershell
python -m pytest -q tests/python/test_owner_history_view.py tests/python/test_owner_history_view_api.py tests/python/channels/test_conversation_relations.py tests/python/channels/test_conversation_projection.py tests/python/channels/test_owner_quoted_attachment.py --tb=short

python -m pytest -q tests/python/channels/test_conversation_projection.py::test_private_memory_request_resolves_quotes_only_inside_its_admitted_window tests/python/channels/test_conversation_projection.py::test_actual_external_maintenance_request_keeps_sources_quotes_and_response_relations tests/python/channels/test_conversation_projection.py::test_owner_statement_extractor_receives_only_owner_inputs_with_quote_attribution tests/python/channels/test_consolidation_parity.py tests/python/channels/test_owner_memory_progress.py tests/python/test_context_windows.py::test_summary_shrinking_preserves_host_source_without_trusting_body_labels --tb=short

python -m pytest -q tests/python/channels/test_natural_conversation.py tests/python/channels/test_natural_media_turn.py tests/python/channels/test_natural_turn.py::test_natural_quote_names_owner_separately_from_guest --tb=short
```

两项兼容用例的修正保留原验收要求：固定基线 98d6a31 的 rich 消息本来就可能以 user 背景帧结束，签名测试改为选择实际 assistant，完整块类型、签名和 namespace 断言不变；36 条历史的投影用例隔离既有 Markdown 后台维护，仍断言一次对话请求、全部历史与原记录不变，未修改 memory_service.py。

新增来源、自然两阶段、资料投影及受影响的自然调度/媒体回归已加入 .github/workflows/ci.yml 的显式测试列表。前端构建、类型检查、Web 测试、Windows 聊天 E2E 和各后端分组由关联 PR 的 CI 记录当前提交结果；规范与规格两轴独立审查在 CI 通过后进行。

## 仍需效果验收的内容

1. **真实模型理解效果。** 尚未用真实模型重复采样，无法据此断言误回率已降低到某个水平。评审应包含“主人 @ 另一人”“@ 角色请她回应他人”“引用与转述”“多个话题交错”和“解释角色之前的话”，结合完整语境判断选择是否合理；允许有依据的澄清、沉默和回应他人。
2. **隐式话题切换。** 宿主能跟踪明确引用、@、参与者转向和有限话题词。没有结构标记的私梗、反讽或隐式转述仍依赖模型；两项目的上述检查也不构成通用语义正确性证明。
3. **历史证据缺失。** 旧记录缺少的平台身份、引用作者、正文或送达回执无法可靠补全。新链路保留未知，不能让旧记录具有未经证明的确定关系。
4. **自然回复的成本。** 一次完整自然回复通常需要两次请求；真实模型的延迟未测量。相关纠正或重新选题会消耗原剩余额度，额度或期限不足时结束本批次，可能表现为这次不接话。当前没有提高原限额。

### 后续模型效果采样记录

实际采样时记录模型版本、角色设定、采样参数、改进前后最终请求、模型选择、生成结果和平台回执；关键场景重复采样。分别记录人物误认、交流意图理解、选择与执行偏离、合理加入或沉默，避免为所有场景规定唯一接收人或唯一答案。模型和环境额度落实后，可沿用新增回归中的合成会话作为固定样例。
