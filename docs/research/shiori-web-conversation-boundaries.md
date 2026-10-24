---
title: Shiori 网页多端会话读取边界核验
kind: 研究记录
status: 当前研究
last_verified: 2026-10-23
source_commit: fb57ec1acbc16d6fcc3577915ac6216751d930ec
sources:
  - https://github.com/YinFengWindy/Shiori-Agent/tree/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge
  - https://github.com/YinFengWindy/Shiori-Agent/tree/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/desktop/renderer/src/phone
related:
  - https://github.com/sawaki-zexi/meido/issues/202
  - ../adr/0009-multichannel-conversation-threads.md
---

# 结论

Shiori 的 phone 是角色渠道会话的只读投影，不是新的 Agent 会话。列表只读当前角色、未归档的 network thread；desktop、scheduler、无已存消息的 thread 不出现在列表。消息页通过角色和 thread 双重校验读取，服务端根据当前身份绑定和当前头像索引投影发送者信息。上游的正常会话消息与群旁听记录分别存储、分别分页，但在群消息页中按时间合并为一条只读时间线；“独立旁听记录”描述的是存储、权限和清理边界，不是前端必须拆成两个可见面板。

# 上游事实

## 角色和线程授权

- `phone.conversations.list` 接收 `role_id`，调用 `ConversationService.list_network_threads(role_id)`；该服务只返回 `thread_kind == "network"`、属于该角色且未归档的 thread。desktop thread 不在列表中，归档 thread 也不再属于当前角色视图。[ConversationService](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/conversation/service.py#L113-L126) · [phone_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_requests.py#L149-L170)
- `phone.conversation.messages` 和 `phone.listening.messages` 都先通过 `role_thread()` 调用 `role_channel_thread(role_id, thread_id)`；thread 不存在、角色不匹配、不是 network 或已归档时拒绝。不能用另一个角色的 thread ID 绕过授权。[phone_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_requests.py#L201-L209) · [phone_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_requests.py#L245-L254)
- 列表行只保留存在普通会话消息的 thread；空 thread 不显示。行包含 channel、account_id（账号已移除时可为 null）、chat type、联系人显示名、头像、是否主人私聊、是否支持旁听以及最近消息预览。[phone_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_requests.py#L152-L170) · [phone_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_requests.py#L347-L415)

## 消息投影与身份

- 消息分页按单个 thread 的存储序号读取，返回 oldest-first 页面、`has_more` 和 `next_before_seq`。游标是该存储流的序号，不能把正常会话和旁听流的序号混用。[phone_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_requests.py#L166-L170) · [phone_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_requests.py#L328-L345)
- `phone_message()` 把角色自己的消息投影为 `sender: role`，其他人投影为 `sender: other`，并在读取时根据当前绑定计算 `sender_is_user`。该字段不是入站时写入的永久权限事实；账号删除或解绑后，历史消息仍保留，但新的读取投影不再把发送者认作主人。[phone_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_requests.py#L96-L145)
- 提及、引用、媒体和发送者/账号头像都在 bridge 投影阶段补齐；平台原始字段不直接决定读取权限。[phone_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_requests.py#L72-L145)

## 旁听与统一时间线

- 旁听记录有独立存储和独立序号。`phone.listening.messages` 在停止旁听后仍可读；源码包含清理存储和游标的路径，具体事件名称及 bridge 推送语义以对应版本实现为准，不能据此断言所有部署都发出同名 `phone.listening.cleared` 事件。[phone_listening_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_listening_requests.py#L31-L55) · [phone_listening_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_listening_requests.py#L64-L132)
- 群聊天页同时加载普通会话流和旁听流。`phoneChatTimeline()` 按消息时间交错合并，两流同一时间时普通会话先显示；旁听行带 `listened` 标记。[usePhoneChatTimeline.ts](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/desktop/renderer/src/phone/usePhoneChatTimeline.ts#L7-L25) · [phoneChatTimeline.ts](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/desktop/renderer/src/phone/phoneChatTimeline.ts#L20-L46)
- 两流各自分页时，只显示双方都已覆盖的时间区间。`since = max(仍有旧页的每条流的最早已载时间)`；`olderFrom` 指向当前限制时间范围的那条流，先补该流才能显示更早的另一流记录。这避免只补一条流造成时间线中间缺口。[phoneChatTimeline.ts](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/desktop/renderer/src/phone/phoneChatTimeline.ts#L14-L46)
- 旁听更新是实时事件，但旁听不会创建模型回合；页面只把新记录合并到只读时间线并标记为旁听。记录达到每日持久上限后，上游仍可能保留有界内存窗口供回合上下文使用，但该部分不应伪装成持久历史。[phone_listening_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_listening_requests.py#L107-L132) · [listening_store.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/conversation/listening_store.py#L128-L151)

## 实时更新、首屏竞态和滚动

- `phone.conversation.updated` 事件携带本次提交的消息和刷新后的完整列表行；列表用新行替换旧行并按最近消息重新排序。事件按 `roleId` 过滤，消息只合并到同一 `threadId`。[phone_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/backend/desktop_bridge/phone_requests.py#L212-L243) · [usePhoneConversations.ts](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/desktop/renderer/src/phone/usePhoneConversations.ts#L10-L25)
- 消息首屏尚未返回时到达的实时消息先放入 `early`，首屏返回后按消息 ID 去重合并；旧 stream 的响应由 generation/cleanup 丢弃。翻页请求同时受 single-flight 和 generation 保护。[usePhoneMessagePages.ts](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/desktop/renderer/src/phone/usePhoneMessagePages.ts#L19-L90)
- 事件订阅只按当前 role 过滤；切换 thread/role 时旧回调不会写入新 stream。页面打开时定位到最新消息；读者在底部时新消息和图片尺寸变化保持贴底，读取旧页时按新增高度补偿 scrollTop，避免位置跳动。[usePhoneLiveUpdates.ts](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/desktop/renderer/src/phone/usePhoneLiveUpdates.ts#L6-L33) · [usePhoneChatScroll.ts](https://github.com/YinFengWindy/Shiori-Agent/blob/fb57ec1acbc16d6fcc3577915ac6216751d930ec/apps/desktop/renderer/src/phone/usePhoneChatScroll.ts#L8-L70)

# 与 Meido 当前实现的对照

当前对照基线是 `main@8786b3f`，工作树分支为 `feat/202-web-multichannel-session`。该分支已经具备大部分同等边界：`networkOnly=true` 列表查询与客户端 desktop 防守过滤、角色和 thread API 校验、独立旁听分页、按时间合并两流、SSE 事件、账号移除快照、附件/引用/提及投影，以及 older-page 的滚动锚点。对应实现见 [ChannelView.tsx](../../web/src/features/channels/ChannelView.tsx#L136) 和 [multichannel.py](../../backend/app/multichannel.py#L1210)。以下项目是本次 #202 验收必须保留的行为，而不是新的架构要求：

1. **列表边界**：服务端必须真正排除 desktop、归档和无普通消息 thread；客户端过滤只能作为兼容保护，不能代替服务端角色授权。
2. **完整实时行**：SSE 的 message/listening 事件必须带当前角色投影后的 thread 行、账号快照、头像、最近消息和发送者身份；不能只发送一条未投影消息后让浏览器猜测权限。
3. **首屏 live 合并**：首屏普通消息或旁听页面尚未返回时收到的事件必须暂存，返回后按消息 ID 合并；刷新或切换角色/thread 后旧响应必须丢弃。
4. **两流分页**：正常消息和旁听记录使用各自游标；时间线只展示双方已覆盖的共同时间范围，并且先读取当前 `since` 边界对应的流。不能把两个 `sequence` 直接比较，也不能只把旁听折叠在消息列表之后。
5. **清理竞态**：Meido 自身的旁听清理响应或清理事件到达后必须清空当前 thread 的旁听记录、游标和内存实时缓存；清理响应晚于新 thread/role 时不得清掉新视图的数据。该项是 Meido 的产品验收要求，不应回写成 Shiori 已提供的 `phone.listening.cleared` 协议。
6. **滚动**：在底部时新消息/媒体尺寸增长应贴底；读取更早页应保持原可见消息位置；内容不足一屏且仍有旧页时应继续读取，直到填满或无更多页。
7. **状态显示**：账号已移除仍可读历史并显示来源快照；投递状态必须保留 `pending`、确定失败和 `uncertain` 的区别，不能将未确认结果显示为 sent。

Meido 当前 `/api/roles/{role_id}/channels/events` 是可重连的 SSE，但 HTTP 事件队列本身没有事件游标或快照补洞协议；断线期间丢失的事件只能通过刷新列表/当前页恢复。因此 #202 的“实时更新”验收应包含断线后手动/自动刷新恢复，而不应声称 SSE 本身保证无 gap。当前 API 的正常消息与旁听分页分别使用 `nextBefore`，前端应保持两个游标域独立。[main.py](../../backend/app/main.py#L3750-L3788) · [ChannelView.tsx](../../web/src/features/channels/ChannelView.tsx#L281-L367)

# 对 #202 验收的审查

现有验收方向与 Shiori 不冲突，但需把“旁听以独立观察记录呈现”解释为独立来源标记和独立清理/分页权限，而不是要求旁听永远脱离统一时间线。必须明确以下可复现测试：

- 首屏请求延迟期间注入普通消息和旁听事件，最终每条消息只出现一次，且位于正确时间位置。
- 两条流的最早时间不同，连续翻页后不出现缺口、重复或跨流序号误排序；同一时间戳时普通消息在旁听之前。
- 切换角色、thread、账号删除和旁听清理时，让旧请求/旧 SSE 事件晚到，确认不会污染当前视图或清空新视图。
- 在非底部读取历史时收到新消息，滚动位置不跳；在底部收到新消息或图片加载完成，视图保持贴底；内容不足一屏时自动继续请求旧页。
- 角色 A 请求角色 B 的 thread、desktop thread、归档 thread、空 thread 和已清理旁听记录，服务端均给出拒绝或明确空结果，不泄露内容。

未核实：本研究没有连接真实桌面 bridge、真实渠道账号或浏览器网络重连环境；未验证 Shiori 事件断线期间是否另有宿主层重放机制，也未验证 Meido 生产部署的 SSE 代理是否保留 `Last-Event-ID`。因此不能把“自动断线无遗漏”列为已实现事实。
