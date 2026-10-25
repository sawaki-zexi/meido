---
title: Shiori 手机消息呈现与 Meido 适配
kind: 源码研究
status: 当前
last_verified: 2026-10-25
shiori_baseline: d149de7f052f77c5bb88d4d1393d48dce1870875
meido_baseline: 729f33c5bdcaf136d797348441fb150ae687dc51
---

# Shiori 手机消息呈现与 Meido 适配

状态：当前（源码研究；Meido 适配为建议，不表示已经实现）。最后核验：2026-10-25。

事实来源：[Shiori 官方固定提交][upstream]与 Meido 固定提交 `729f33c5bdcaf136d797348441fb150ae687dc51`。本次通过 GitHub API 再次核验 Shiori 最新 `main`，仍为 `d149de7f052f77c5bb88d4d1393d48dce1870875`，提交时间 `2026-10-24T10:42:02Z`。本机 `D:\shiori-agent-ref-20261024` 的 remote 指向官方仓库，所有读取使用固定提交的 `git show`，不使用其旧工作树内容。

本文调查消息层，不重复上轮[手机导航研究](shiori-phone-connected-flow.md)。**已提取**表示源码或已有测试证明，**推断**表示由源码组合得出的解释，**计划**表示针对 Meido 的适配。没有访问已连接 QQ 的数据，没有启动服务、修改实现或运行完整测试。

## 1. 气泡、头像、名字各自的位置

**已提取。** 手机聊天从角色的视角显示：角色自己的消息靠右，他人消息靠左；角色头像与平台发送者头像分别在正文列外侧，不嵌在文字气泡里。每行都会显示头像，没有按连续同发送者隐藏头像的逻辑。角色头像来自角色资料，未设置时显示名字首字；他人头像来自缓存的平台头像，未缓存时显示通用用户图标。[消息行第 53–76 行][row]、[角色头像][role-avatar]、[平台头像][avatar]

**已提取。** 外层消息行负责头像和对齐，内层正文列最大宽度为 78%，让窄屏给头像与间距留出空间。名字／身份标记、引用、正文气泡、附件依次排列。正文气泡采用内容宽度、保留换行、长词换行；角色气泡使用强调色背景，对方气泡使用普通面板背景，旁听气泡另用浅色虚线边框。[消息行第 9–18、57–76 行][row]

**已提取。** 角色自己的消息不重复显示名字。他人消息上方显示平台发送者名，未知时回退发送者 ID；已绑定主人加“这是我”标记。群旁听在名字旁加耳朵图标。该规则同样适用于他人私聊，不仅限于群聊。[展示解析第 77–86 行][chat-presentation]、[名字与标记第 62–68 行][row]、[已有行为测试第 29–42 行][chat-tests]

**已提取。** 上游的主人标记基于读取时的当前身份绑定计算，不直接照搬旧消息中保存的主人标志。这是身份语义，不是纯样式；Meido 本次消息展示优化不应顺带改变自己的身份判定方式。[后端手机消息投影第 105–133 行][requests]

## 2. 时间采用分组，正文不常驻诊断信息

**已提取。** 第一条时间有效的消息前显示居中时间分隔。之后只有与上一条时间有效消息间隔**严格大于五分钟**才新增分隔；恰好五分钟不新增，时间无效的消息不产生分隔，也不更新比较基准。实现没有单独的跨日强制分隔判断。[阈值与循环第 5–6、62–89 行][chat-presentation]、[时间行为测试第 52–71 行][chat-tests]

**已提取。** 今天的分隔只显示 `HH:mm`，昨天显示“昨天 HH:mm”，近一周显示“周几 HH:mm”，更早显示月／日或年／月／日加时分。分隔使用小号、低对比度、居中的圆角标签；消息行不再逐条显示完整日期时间。[时间解析第 40–48 行][chat-presentation]、[日期标签第 52–80 行][day-label]、[时间标签渲染第 49–56 行][page]

**已提取。** 上游手机消息的传输模型只包含发送者、文本、媒体、提及、引用、时间与旁听等展示字段，没有 `status`、`error`、`deliveryStatus`、`viaAccount` 或平台回执 ID。消息组件也没有常驻的来源账号、平台消息编号、投递状态或失败原因区域。[手机消息类型第 50–74 行][client]、[后端投影第 123–145 行][requests]、[完整消息行][row]

**推断。** 上游的简洁并不能证明“失败与不确定状态应隐藏”。这里只能确认当前手机消息投影没有支持这些字段；不能据此断言 Shiori 的全部聊天界面或投递系统都没有错误处理。手机聊天页现有 `PhoneLoadError` 对应读取时间线失败，是页面读取错误，不能代替单条生成或投递失败提示。[聊天页第 33–47 行][page]、[消息投影][requests]

## 3. 空文本仍可能有内容：提及、附件与引用

| 情形 | Shiori 手机的实际呈现 | Meido 适配要求 |
| --- | --- | --- |
| 只有结构化 @，正文为空 | 只要有提及标签就渲染正文气泡，显示 `@名字`，没有名字时显示 `@ID` | 详情和会话预览都应识别提及，不显示“无内容” |
| 只有图片／文件，正文为空 | 不生成空文字气泡，附件独立出现在正文列 | 保留已有附件读取、下载和图片展示能力 |
| 引用存在，但当前正文为空 | 引用仍然显示，随后可显示当前消息的附件 | 不把引用或引用附件误判为无内容 |
| 文本、提及、引用、附件均为空 | 消息解析仍保留这一行；组件没有“无内容”兜底，可能只剩头像等行结构 | 按 Meido 的真实消息状态解释，不能照搬为空白行 |
| 生成失败且正文为空 | 当前手机类型没有失败字段，组件没有专门失败呈现 | 明确显示生成失败及可读原因；不是一条空消息 |

上表前四行的上游事实分别由[文字气泡条件][row]、[提及解析][chat-presentation]、[附件组件][media]和[引用组件][quote]证明；失败限制由[手机类型][client]和[消息投影][requests]证明。右列为 Meido **计划**，不是上游已实现行为。

### 3.1 提及放在正文开头

**已提取。** 提及以结构化列表生成 `@名字` 标签，名字缺失时使用 ID；标签使用强调色、按原顺序置于正文最前面，与文字共用一个气泡。渲染条件是正文非空**或**提及数量非零，因此纯 @ 消息可读，不依赖平台把 @ 写进 `content`。[提及映射第 85 行][chat-presentation]、[气泡条件第 70–74 行][row]、[提及行为测试第 45–49 行][chat-tests]

### 3.2 图片与文件独立于正文气泡

**已提取。** 普通图片在最大 `180×220` 的范围内按原比例缩小，不放大、不裁切；图片放在正文气泡外的独立按钮中。点击后在整个窗口中放大查看，支持关闭按钮、点击外部和 Esc 关闭，并由对话框处理焦点。[手机附件第 6–25 行][media]、[图片缩放算法][image-layout]、[大图对话框][lightbox]

**已提取。** 不是按文件 MIME 猜测图片，而是按去掉查询／片段后的文件扩展名判断；此版本识别 `png`、`jpg`、`jpeg`、`gif`、`webp`、`bmp`。其他文件显示文档图标与截断文件名。手机文件 chip 是 `span`，没有下载链接；桌面版同一文件内容组件可以放进链接，不能把两者混为一谈。[图片识别第 11–18 行][image-kind]、[手机文件 chip 第 26–29 行][media]、[文件名内容第 10–23 行][file-chip]

**计划。** Meido 应保留现有按附件 ID 访问的图片／文件链接能力，借鉴尺寸、间距与独立附件区域即可，不必为了像上游而删掉可用的下载交互。也不需要改成上游的本地路径资源协议。[Meido 现有附件呈现][meido-attachments]、[上游手机附件][media]

### 3.3 引用在气泡上方，并与当前附件分开

**已提取。** 引用块位于正文气泡上方，显示被引用者名字（缺失时回退 ID）、原文和引用媒体。超过 40 个字符或超过两行的原文初始折叠为两行，可点击展开／收起；引用图片缩略图最大 `64×64`，同样可放大。引用图片留在引用块内部，当前消息自己的图片放在正文气泡之后。[引用解析第 8–10、51–59 行][chat-presentation]、[引用组件第 7–43 行][quote]、[已有布局测试][row-test]

**已提取。** 后端只有在引用具有文本或媒体快照时才生成引用对象；只有回复 ID 而没有原文／媒体时返回空引用。因此，上游这一路径不会凭空重建一个未加载的原文。[引用投影第 72–93 行][requests]

**计划。** Meido 已有“原文未加载”提示及引用附件；这些信息应保留。可以把引用布局移到正文上方，但不要删除未知回复 ID 的可访问性提示，也不要把引用附件误当作当前消息自己的附件。[Meido 回复与附件第 685–710 行][meido-row]

## 4. 群聊会话列表的预览字段

**已提取。** 列表行第一行显示聊天名与最新时间，第二行只显示一行消息摘要。群头像来自该群的缓存头像，缺失时显示群聊标记；不在列表摘要里重复来源账号与回执。[列表行第 24–45 行][list]

**已提取。** 后端将最新正文截到前 200 个字符，并提供 `role`、时间与 `has_media`；群聊预览用的发送者名来自最新消息的 `source.sender_name`，仅在该消息为 `user` 时提供。它不是群名／会话标题。[摘要投影第 16–28 行][preview-backend]、[手机最新消息第 392–398 行][requests]

**已提取。** 前端先把 Markdown 扁平化为一行：代码块变成 `[代码]`，链接保留标题，去掉部分排版标记并合并空白。空正文且有媒体时使用 `[图片]`；群聊中只有“最新消息是他人且已知发送者名”才加“名字：”，角色自己的消息不加名字，私聊也不加发送者前缀。[预览函数第 54–64 行][list-presentation]、[Markdown 扁平化][preview-text]、[已有预览测试][list-tests]

**已提取。** 上游最新消息的列表模型不含提及、引用、错误或具体附件类型。因此纯 @ 无正文、无媒体时，上游预览可能为空，群聊也可能只剩“名字：”；只有文件的消息也可能统一显示成 `[图片]`。这些是字段与函数能够直接推出的限制，不应作为 Meido 必须复刻的结果。[最新消息类型第 7–16 行][client]、[预览函数][list-presentation]

**计划。** Meido 详情与会话预览应复用同一套“可读内容”规则，覆盖正文、结构化提及、附件、引用与失败状态。预览有真实发送者名时才加名字，不能用群名代替最后发言者。研究时 Meido `LatestMessage` 类型只有 `content`、`role`、`status`、`createdAt`；若当前列表接口没给发送者信息，应使用不带发送者前缀的摘要，而非编造名字。[Meido 预览类型第 30 行][meido-types]、[上游真实发送者来源][requests]

## 5. Meido 必须保留的状态与来源

**已提取。** Meido 同时具有生成状态与渠道投递状态。消息模型有独立 `status`；回合终结可将错误原因写入 `metadata.error`；投递状态另有 `pending`、`sent`、`failed`、`uncertain`。生成失败不等于投递失败，投递不确定也不等于确定失败。[Meido 消息模型][meido-model]、[终结写入第 1336–1344 行][meido-error]、[投递状态枚举][meido-delivery]

**已提取。** Meido 基线消息行已显示来源账号、引用、结构化提及、投递状态、折叠平台消息编号和完整时间，但只要正文与附件为空就额外显示“无内容”，没有把提及与生成失败纳入该判断。列表预览也直接以空正文回退“无内容”。这正是样式优化同时需要修正的语义缺口。[Meido 消息行第 682–714 行][meido-row]、[Meido 列表第 667–670 行][meido-list]

以下均为 **计划**，用于适配现有能力，不声称上游原样提供：

- 详情可采用外置头像、左右气泡、上方引用、独立附件和五分钟时间分组，正文优先于来源与编号。
- 仅提及显示 `@名字／ID`；仅附件显示附件；仅引用显示引用；生成失败显示“生成失败”和原因。失败且仍有已生成正文时，保留正文并附状态。
- `pending`、`failed`、`uncertain` 保持可辨识；明确区分生成失败与投递失败。不得因为 Shiori 正常气泡简洁，就让不确定结果看起来已经发送成功。
- 来源账号快照、平台编号与精确时间可以收进可展开的消息详情；保留历史账号归属、账号已移除说明及记忆来源回跳。折叠的是诊断密度，不是删除来源事实。
- 详情与列表摘要用相同解析规则；接口缺失某种字段时如实回退，不把群名当发送者，也不把所有文件都叫图片。

这些适配依据是 Meido 已有的[消息状态与错误][meido-error]、[投递枚举][meido-delivery]、[来源／引用／回执呈现][meido-row]与上文逐项核验的 Shiori 布局。本文不更改身份、消息归属、投递重试或历史读取规则。

## 6. 核验范围

- 阅读了固定版本的消息行、聊天页、展示解析、会话预览、头像、附件、图片布局、引用、手机传输类型与对应已有测试；没有运行完整测试。
- 对“纯 @”“纯附件”“只有回复 ID”“失败空正文”给出源码分支或 Meido 契约依据；没有用真实已连接账号做实验。
- 本次只新增本文，未修改正在实现的消息视图、共享解析、测试和原有研究文档。最终界面仍需由实现后的用户流程核验。

[upstream]: https://github.com/YinFengWindy/Shiori-Agent/tree/d149de7f052f77c5bb88d4d1393d48dce1870875
[row]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneChatMessageRow.tsx
[role-avatar]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/roles/RoleAvatar.tsx#L4-L18
[avatar]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneAvatarFace.tsx#L4-L17
[chat-presentation]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/phoneChatPresentation.ts#L5-L89
[chat-tests]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/phoneChatPresentation.test.ts#L29-L71
[requests]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/desktop_bridge/phone_requests.py
[day-label]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/roles/roleChatPreview.ts#L52-L80
[page]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneChatPage.tsx#L15-L63
[client]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/phoneClient.ts#L7-L74
[media]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneMessageMedia.tsx
[quote]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneMessageQuote.tsx#L7-L43
[image-layout]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/chatMessageImageLayout.ts
[lightbox]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneImageLightbox.tsx
[image-kind]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/chatImageHistory.ts#L11-L18
[file-chip]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatMessageAttachments.tsx#L10-L75
[row-test]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneChatMessageRow.test.tsx#L6-L25
[list]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneConversationList.tsx#L24-L45
[preview-backend]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/desktop_bridge/session_presenter.py#L16-L28
[list-presentation]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/phonePresentation.ts#L54-L64
[preview-text]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/src/shared/chatPreviewText.ts
[list-tests]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/phonePresentation.test.ts#L55-L69
[meido-attachments]: https://github.com/sawaki-zexi/meido/blob/729f33c5bdcaf136d797348441fb150ae687dc51/web/src/features/channels/ChannelView.tsx#L113-L122
[meido-row]: https://github.com/sawaki-zexi/meido/blob/729f33c5bdcaf136d797348441fb150ae687dc51/web/src/features/channels/ChannelView.tsx#L682-L714
[meido-types]: https://github.com/sawaki-zexi/meido/blob/729f33c5bdcaf136d797348441fb150ae687dc51/web/src/features/channels/ChannelView.tsx#L15-L38
[meido-model]: https://github.com/sawaki-zexi/meido/blob/729f33c5bdcaf136d797348441fb150ae687dc51/backend/app/models.py#L185-L200
[meido-error]: https://github.com/sawaki-zexi/meido/blob/729f33c5bdcaf136d797348441fb150ae687dc51/backend/app/session_store.py#L1336-L1344
[meido-delivery]: https://github.com/sawaki-zexi/meido/blob/729f33c5bdcaf136d797348441fb150ae687dc51/backend/app/channels/models.py#L20-L24
[meido-list]: https://github.com/sawaki-zexi/meido/blob/729f33c5bdcaf136d797348441fb150ae687dc51/web/src/features/channels/ChannelView.tsx#L667-L670
