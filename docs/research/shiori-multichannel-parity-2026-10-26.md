# Shiori 多端会话差距复核（2026-10-26）

状态：当前研究记录；2026-10-28 补充之后的原始矩阵仍描述 2026-10-26 固定基线，不代表当前主线或真实平台验收完成。

事实来源：下文链接到两边固定提交的实现、现有行为测试和 UI 调用链；旧调研仅用于定位，不作为功能完成的证据。

最后核验：2026-10-28；本次仅复核开放 PR 涉及的功能，不重新扩展整个产品的功能清单。

本文保留差距核验基线；用户随后已选择按 Shiori 对齐，共享边界及交付目标见[对齐 Spec](../specs/shiori-multichannel-alignment-spec.md)。[最后一轮深核](shiori-multichannel-spec-deep-dive-2026-10-26.md)进一步明确：24 条限制针对自动附加片段而非已有历史查询工具；Shiori 的成员“渠道键”实际可能包含账号标识，不能一概当作全平台身份键。

## 2026-10-28：开放 PR 的功能复核

**已提取：** GitHub 的 PR 元数据、各 PR 固定头提交及上游源码共同作为证据；PR 正文的“通过”或测试数量不作为功能已实现的替代。此时八个 PR 仍开放，#248/#251/#252 为 Draft。下表是本轮修改前的快照，后续修复与合并状态以[对齐 Spec](../specs/shiori-multichannel-alignment-spec.md)及对应 PR 为准。

| 对象 | 固定版本与核验范围 |
| --- | --- |
| Meido main | `45003831ac108c4c25e37cf388e85ccbfd9ee82d`；#258 已于 2026-10-27 合入，不能再列为缺失或待合入 |
| Shiori 原研究 → 本次主线 | `d149de7f052f77c5bb88d4d1393d48dce1870875` → [`d3acaed186b172a78a5892e0307e9affd89759f0`](https://github.com/YinFengWindy/Shiori-Agent/commit/d3acaed186b172a78a5892e0307e9affd89759f0)，前进 13 个提交 |
| Pi 原参考 → 本次主线 | `d5b3de22d0e24a3e9c38a9410d3329b099717283` → [`97553d10f2cb8be11a4d0fda331fe06967139643`](https://github.com/earendil-works/pi/commit/97553d10f2cb8be11a4d0fda331fe06967139643)，前进 96 个提交 |

Shiori 两个固定版本的 215 个相关文件逐 blob 比较后，QQ 文件接收、授权历史视图、成员档案/动态、NovelAI 工作台/service 和 Grok 收发核心内容未变；变化集中在插件注册、账号装配和 memory runtime。因此仍可复用下文的核心行为证据，但不能把整个上游仓库说成未变化。Pi 的 [`buildSessionProjection` / `buildSessionContext`](https://github.com/earendil-works/pi/blob/97553d10f2cb8be11a4d0fda331fe06967139643/packages/coding-agent/src/core/session-manager.ts#L542-L583)保留来源并处理压缩/上下文编辑；本批具体用户流程直接参照 Shiori，不因 Pi 前进而引入其无关 CLI、provider 或 Chord 功能。

| PR / 已读取头提交 | 上游用户流程与 Meido 实际代码 | 复核判断 |
| --- | --- | --- |
| [#243](https://github.com/sawaki-zexi/meido/pull/243) / `90f2b6ec9464f9b2375e5486d3af4f99bedc130e` | F01–F13 规格与研究；没有运行代码 | 纠正 #258 状态，保留原研究基线；不得把规格本身算作功能交付 |
| [#245](https://github.com/sawaki-zexi/meido/pull/245) / `0c2c1dc9c6b8c168e1dae7c92d7b3b267393abc0` | QQ 直接/引用文件 ID → NapCat 地址查询 → 有界 TXT/MD 下载 → 角色附件读取；`napcat.py`、`qq_files.py`、`main.py`、`multichannel.py` 和聊天页面已有接线 | 主链路已有。使用受管 NapCat v4.18.28 参数，不复制旧的 group/busid 参数；拒绝本机路径、私网/任意域和重定向是有意收紧 |
| [#247](https://github.com/sawaki-zexi/meido/pull/247) / `5bfd5d2388d195ff46e33c45d4aadf79552fb28c` | `external_members.py` 共享身份档案，`external_memory.py` 笔记/3 天最多 5 条动态，HTTP CRUD、`ExternalMemoryPanel.tsx` 及普通/主动上下文都有实际调用 | 管理与共享主链路已有；`group_tools.py::prompt_context` 仍按 ID 排序取最多 10 个成员，需核对当前发言者、结构化 @ 和被引用者的优先注入，不能让更相关成员被字典序挤掉 |
| [#248](https://github.com/sawaki-zexi/meido/pull/248) / `b6adde5a570b79167de352d4d960a312067066a2` | `availability.py`、`application.py::tool_enabled`、`tools.py` 已接通就绪/工具发现；`NovelaiStudio.tsx`、`useGenerationDrawer.ts` 提供提示词页签、底图、大图/缩略图、历史与错误修正 | 不再缺基础生成/历史/下载；完整参数重生成和普通作品的角色素材写回仍有真实差异，见下文 |
| [#251](https://github.com/sawaki-zexi/meido/pull/251) / `963dc764468efd187f91c7540cbef778703eda12` | `prompt_tags.py::_append` 对 `::`、花括号、方括号保留整段，普通标签继续去重；预览、聊天和 CG 共用 | 修复两边共同的闭合符丢失问题，不应为复刻而恢复上游错误 |
| [#252](https://github.com/sawaki-zexi/meido/pull/252) / `e62f3839c61201a651689584b977bf33530ef268` | `PromptTagManager.tsx` 卡片/独立编辑/删除确认；`usePromptTags.ts` 完整有界分页、NFKC 搜索及组合筛选 | 卡片流程已有；搜索是 Meido 增强，Shiori 无对应搜索。两边标签参考图均仅供识别，不是供应商条件参考输入 |
| [#253](https://github.com/sawaki-zexi/meido/pull/253) / `d0956bfbf6e7b77e223c86069e9c0d87619e4f3c` | `ConversationHistoryView` 从原 transcript 投影授权历史，重建角色/来源/引用/媒体；历史工具、普通/主动/Drift 共用 | 已不是固定 24 条文本补充。保留独立时间线、冻结来源与预算裁剪是实现边界，不是跨端连续性缺失 |
| [#257](https://github.com/sawaki-zexi/meido/pull/257) / `6b717b1035173446a94f25d2b3633db9be0c5a28` | `grok/adapter.py`、`callback.py`、`client.ps1`、`exchange.py` 与真实 Runtime、宿主恢复和投递账本接通 | 固定双向文字流程已有。未知父消息拒绝为已确认 A5；本机回程、外部访客身份、HTTP 接受不等于 Bot 回复均应保留 |

### 已提取：需要补齐与不应照搬的行为

- **成员选择：** Shiori [`member_profiles.py` L269–352](https://github.com/YinFengWindy/Shiori-Agent/blob/d3acaed186b172a78a5892e0307e9affd89759f0/apps/backend/core/memory/member_profiles.py#L269-L352)先取触发者、结构化 @ 和被引用者的完整档案，再按可见窗口的近远放其他成员速记。Meido 应保持真实身份/会话来源和总模型预算，不照搬“完整档案不截断”的无界部分。
- **自动档案合并：** Shiori [`merge_member_profile`](https://github.com/YinFengWindy/Shiori-Agent/blob/d3acaed186b172a78a5892e0307e9affd89759f0/apps/backend/core/memory/member_profiles.py#L132-L146)只用非空自动结果替换旧档案/速记；#247 固定头提交的 `consolidation.py` 将缺失或空字段转换为空串后写回，会丢失已有认识。补齐时应只改变自动整理的合并，保留主人手动清空的能力及已有版本冲突保护。
- **完整重生成：** Shiori [`service.py` L164–211](https://github.com/YinFengWindy/Shiori-Agent/blob/d3acaed186b172a78a5892e0307e9affd89759f0/plugins/novelai/backend/service.py#L164-L211)深拷贝可信旧 request，仅换 seed。#248 的 `loadParameters` 不带回 `qualityToggle`/`ucPreset`，`parameters.py` 使用当前服务偏好；改变偏好后重新生成不等于原条件只换 seed。若补齐，应由宿主恢复可信历史条件，并重新校验当前模型、模式、附件、来源和次数许可，不接收模型任意旧 payload。
- **角色素材：** Shiori [`service.py` L254–270](https://github.com/YinFengWindy/Shiori-Agent/blob/d3acaed186b172a78a5892e0307e9affd89759f0/plugins/novelai/backend/service.py#L254-L270)在开关允许时把普通生成加入 illustration 并设聊天背景。Meido 有 CG 的受限角色卡片写回，但该固定 #248 工作台没有普通历史作品的对应动作。此差异不是图片保存失败，应复用既有角色素材和并发覆盖保护。
- **保留的适配：** 成员的平台/应用身份作用域、旧身份隔离、迁移冲突与持久编辑版本；QQ 有界附件与 URL 限制；主人历史授权/已送达筛选；Grok 未知父消息拒绝；不确定付费请求不自动重试。它们提供同类用户结果而不复制上游较弱的边界。
- **区分原票与新增范围：** Story 独立叙事/视觉资源域不是这些原有开放 PR 的缺失按钮。用户随后于 2026-10-28 明确追加 Story 与角色插画/聊天背景能力，已另建 [#261](https://github.com/sawaki-zexi/meido/issues/261)，在 #248 基础上独立交付，不回填为 F01–F13 的原范围。动态插件加载、公共回程服务器、真正的参考条件生成仍不能据此自动扩入。

**集成建议（不是已合并事实）：** NovelAI 可按 #251 → #248 → #252 集成；渠道 PR 没有未合并分支硬依赖，但 `main.py`、`multichannel.py` 及 provider/主动上下文重叠，应逐个整合并保留双方行为。#243 在真实合并后最后刷新交付表。#247 与 #248 同时使用 ADR-0027，编号需协调，不能依赖 Git 自动发现该语义冲突。

**许可事实：** Shiori、Pi、Meido 根许可证均为 MIT。Meido 已带 `third_party/shiori-agent/LICENSE` 和 `backend/app/novelai_generation/SHIORI-LICENSE.txt`；新增实质代码改写时保留相应版权/许可并记录固定来源路径。功能参考不自动证明复制了 Pi 代码，不补虚构来源。该复核未运行真实平台账号或付费模型，也未更新正式实例。

### 2026-10-28 后续补齐：代码结果，不代替合并状态

上述修改前快照发现的成员相关性及自动空值覆盖问题，在 `e0e9d2aeef79d9ee1adbeed2cf6c0fbc8f33e5ad` 补齐：当前发送者、结构化 @、可信引用成员优先，其他成员按近期可见记录提供有界速记；自动整理不再用空结果清掉旧认识。完整历史参数重生成在 `4b457774c58b77c345e47fea63011fc4d253907b` 补齐，同提交将 NovelAI ADR 改为 `0031-novelai-plugin-readiness.md`，消除与成员资料 ADR-0027 的重号。两项仍保留当前来源、授权和人工编辑边界。

另行批准的 [#261](https://github.com/sawaki-zexi/meido/issues/261) 按相同 Shiori 固定版本复核；以下是 `8e247cf0c55fd4f05179c8ab66127ac8edb4c751` 加 `69a00707a543b2e9c09d815d1b1043e85a7ff976` 的源码结果，不混入原 F01–F13：

| Shiori 实际用户流程 | Meido 实现与结果 |
| --- | --- |
| `RoleAssetsPage.tsx`、`RoleAssetCategoryGroups.tsx`、`useRoleManagement.ts`：多图导入、分类增改移动/整组删除、原图查看、头像/背景选择与清除 | `RoleIllustrationLibrary.tsx` → `roleAssets.ts` → `role_assets.py` / `role_store.py` 已接通；最后两项差距（批量上传、分类连同图片移除）由 `69a00707` 补齐。保留“仅移除分类、保留图片”选项；移图进入本地恢复区，未声称有应用内恢复入口 |
| 角色 mood 绑定与默认/当前立绘；`plugins/meme/backend/runtime.py` 的允许分类自动图片反应 | `RoleMoodBindingEditor.tsx`、`roleMood.ts`、`role_mood.py` 连接绑定、展示和 `set_current_mood`；`role_reactions.py` 接入普通桌面及支持媒体的渠道回合，依分类许可选择图片并复用真实投递回执，不增加新目标或工具权限 |
| `plugins/story`：创建角色故事、生成/播放/玩家输入、保存载入、记录、CG 查看与失败重试/重生成 | `features/story/` → `api/story.ts` → `backend/app/story/` 已接通角色模型、持久故事与独立视觉资源；角色/图片快照冻结，默认 mood 立绘参与展示，故事不混入普通聊天记忆。另提供明确的继续、归档/恢复与纯文字选择 |
| NovelAI 普通作品成为角色素材、跨模块保留工作台状态 | 普通生成及历史作品可进入角色插画/背景；`App.tsx` 保持已访问的 Studio 挂载，离开模块不丢草稿或在途生成 |

本次终核未发现该明确范围内的其他缺失用户操作。Shiori 的音频、手动存档槽、分支选择没有相应完整链路，主动/Drift 也未接普通回复的图片装饰器，故不制造占位按钮或额外能力来充数。Story 改写保留 `backend/app/story/SHIORI-LICENSE.txt`，角色图片反应来源记录在 `third_party/shiori-agent/README.md`。以上是调用链核验，不是像素一致、真实平台收发或付费生图的验收声明；最终交付状态仍以实际合并记录为准。

### 2026-10-28 已确认合并记录

以下为实际主线合并，不改写上文修改前的固定快照。最终已确认代码基线为 `b7f5ee420ecf872570a70855b73ab1e3ab1c8389`：原七个功能 PR 及用户另行批准的 #261 扩展均已合入。已确认的本轮功能范围完成对齐；不声称两个项目的所有无关插件、供应商或 CLI 均已复刻。

| PR | 实际合并提交 |
| --- | --- |
| [#245 QQ 文件](https://github.com/sawaki-zexi/meido/pull/245) | `65aedfbcea8191f6fcaff1eb9c913591c4cdcf44` |
| [#248 NovelAI 工作台](https://github.com/sawaki-zexi/meido/pull/248) | `31bf8c37ad6cd2574b9a0e6b72bf403ba3ea72fc` |
| [#252 标签库](https://github.com/sawaki-zexi/meido/pull/252) | `f32c8a6ee480875d2e74589bbdcfbf5d69da8095` |
| [#251 加权片段](https://github.com/sawaki-zexi/meido/pull/251) | `1bd3c21f58a06d1329b9a52fea8bb717ad6d1597` |
| [#247 成员资料与笔记](https://github.com/sawaki-zexi/meido/pull/247) | `aacb49ced8c6acf1ccae9c02c36a65d91b2be42a` |
| [#253 主人连续历史](https://github.com/sawaki-zexi/meido/pull/253) | `6c7745bda8b49bbe3d7e7c0017b72a27ecaf748d` |
| [#257 Grok Bot](https://github.com/sawaki-zexi/meido/pull/257) | `fc164abdf5cafbadc4df23f585cc8945af9e53e1` |
| [#262 Story、角色素材/心情/图片反应及工作台状态（Refs #261）](https://github.com/sawaki-zexi/meido/pull/262) | `b7f5ee420ecf872570a70855b73ab1e3ab1c8389` |

#262 的合并前头提交为 `093d09445f1ac0e2f6c27c9752dcab175f68c2ca`，[CI 40762228465](https://github.com/sawaki-zexi/meido/actions/runs/40762228465) 全部成功；其主应用/桌宠构建与三项定向集成检查记录见 [Story 规格](../specs/shiori-story-parity.md)。文档最终整合只检查差异、链接和来源事实，没有重复运行已通过且未变化的功能检查；真实账号、供应商消费及正式实例更新仍未执行。

合并前另补 Grok 来源声明：`client.ps1`、`protocol.py` 的实质改写和连接/预算行为适配均在 `third_party/shiori-agent/README.md` 映射到上述 Shiori 固定版本；版权及 MIT 全文保留。Story 与角色素材的来源记录也已核对，不把独立功能参考误记为 Pi 代码复制。

## 原始结论与计数口径（2026-10-26）

**本轮按 27 个不重复的用户能力核对：Meido main 有 15 项主链路已具备、8 项部分对齐、4 项对应行为缺失，共 12 项未完全对齐。** 这不是完成率，也不是两个项目全部功能的穷尽清单。没有把一个页面的按钮、接口和存储分成三个功能计数；平台表、差距明细和待合入改动也不重复计数。

最影响当前 QQ 体验的差距是：引用中的主人身份没有明确传给模型；成员档案、会话笔记和最近动态没有用户入口；同一群友在不同群的档案彼此独立；部分 QQ 文件只有平台文件 ID 时不能接入已有附件阅读链路。Telegram、飞书还存在格式呈现、限流恢复、失效引用恢复等细节差距。新增 Grok Bot 渠道属于低优先级扩展。

“未完全对齐”不全部等于应该照抄的缺陷。**跨群共享档案、跨会话摘要注入、跨端完整历史组装涉及现有隔离和上下文策略，需先确定共享边界。** 当前实现保留隔离的行为有明确测试，不能当作漏写了几个函数。[Meido 隔离测试][m-context-test]、[ADR-0009][m-adr9]

用户本次提出的两个问题应另行准确描述：

- **私聊配对后群里不认主人：** main 已按真实平台发送者 ID 识别主人，QQ/Telegram 的平台绑定跨角色账号适用，不需要逐群配对。问题在身份如何进入具体模型回合；PR #230 加强当前发送者的可信身份提示，并补自然聊天路径，但引用中的主人身份仍未补齐。[Meido 全局身份][m-identity]、[身份行为测试][m-identity-test]、[PR #230](https://github.com/sawaki-zexi/meido/pull/230)
- **每群独立关闭全部回复：** Shiori 最新基线也只有账号级群响应开关，不具备用户要求的每群彻底关闭回复。Meido main 同样没有；PR #230 是待合入的新增能力，关闭后抑制 @、引用、自然接话等回复，并保留独立旁听。不能把它算作 main 已有，也不能算成 Shiori 已有而我们漏做的能力。[Shiori 群准入][s-hub]、[Meido 当前群设置][m-group-settings]、[PR #230](https://github.com/sawaki-zexi/meido/pull/230)

## 基线与范围

| 对象 | 本轮事实基线 | 用途 |
| --- | --- | --- |
| Shiori-Agent | [`d149de7f052f77c5bb88d4d1393d48dce1870875`](https://github.com/YinFengWindy/Shiori-Agent/commit/d149de7f052f77c5bb88d4d1393d48dce1870875) | 本轮 GitHub API 确认的最新 main；只读源码工作区 |
| Meido 已合入代码 | [`8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750`](https://github.com/sawaki-zexi/meido/commit/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750) | 所有“已具备/部分/缺失”计数的唯一 Meido 基线 |
| Meido 待合入改动 | PR #230，`25de6e2e94d9c375587138bcc2aa163152bbbbf0` | 只描述当前身份提示和每群回复开关，不并入 main 计数 |

源码基线不等于 `D:\meido` 常驻实例实际运行版本。本轮没有更新或重启正式服务，没有使用真实渠道账号外发，也没有确认当前生产进程已运行上述 main 提交。旧 Issue 的开放状态或旧研究中的“完成”结论不能替代现有代码核验。范围参照开放的 [多端规格 #116](https://github.com/sawaki-zexi/meido/issues/116)、[会话浏览 #202](https://github.com/sawaki-zexi/meido/issues/202)；PR #230 已重新确认仍为未合入的 Draft。

分类定义：

- **已具备：** 本行所定义的用户主链路在 main 有实际实现，且能沿调用链核验；不表示所有边界、平台限制或 UX 细节都相同。
- **部分：** 已有基础能力，但缺一个可感知环节，或当前实现有明确的行为差异；选型差异额外标明。
- **缺失：** 本行限定的行为没有对应实现，不能用相邻能力代替，例如正文流式不能代替工具执行进度。
- **待合入、两边均无、Meido 扩展、纯架构实现差异：** 单独列出，不混入 Shiori 功能差距计数。有用户可感知结果差异的选型计入对应“部分”项，并标明需先确定共享边界。
- **已提取**是源码或现有测试直接证明的事实；影响判断和优先级属于**推断/建议**。建议未立项，也不构成已批准的实现计划。

## 27 项能力矩阵（已提取）

| # | 核对的用户能力 | Meido main | 核验结论及双方证据 |
| --- | --- | --- | --- |
| 01 | QQ、Telegram、官方 QQBot、飞书既有接入范围 | 已具备 | QQ/Telegram 有私聊与群聊；QQBot/飞书只做私聊。不是缺后两者的群聊支持。[Shiori QQBot][s-qqbot-send]、[飞书][s-feishu-private]；[Meido QQBot][m-qqbot-in]、[飞书][m-feishu-in]、[QQ][m-qq-in]、[Telegram][m-tg-in] |
| 02 | 角色拥有独立渠道账号、同一平台账号不能归两角色 | 已具备 | 两边均限制一个角色每渠道一个账号，防止账号重复归属。[Shiori 注册约束][s-ownership]；[Meido 注册][m-account-register]、[测试][m-account-test] |
| 03 | 平台总开关与账号连接意愿分开保存 | 已具备 | 暂停平台不会把所有账号原有开关重写；恢复只启动有连接意愿的账号。[Shiori 运行状态][s-account-runtime]；[Meido 总开关][m-platform-switch]、[测试][m-platform-test] |
| 04 | 账号配置、托管 QQ 扫码、连接状态与重新连接 | 已具备 | 两边有实际账号管理和受管 NapCat 链路；不代表安装/扫码已经在本轮真机验收。[Shiori QQ 管理][s-managed]；[Meido QQ 管理][m-managed]、[状态/重试测试][m-managed-test] |
| 05 | 账号级私聊/群聊响应开关、发送者黑名单 | 已具备 | 入站实际检查规则，而非只有表单。范围仍是账号级。[Shiori 准入][s-hub]；[Meido QQ 准入][m-qq-in]、[Telegram 准入][m-tg-in] |
| 06 | 一次性配对、平台/应用作用域、解绑撤销 | 已具备 | QQ/Telegram 平台身份共享；QQBot/飞书应用范围绑定；共享身份不共享不同角色的记忆。[Shiori 身份匹配][s-identity]；[Meido 身份][m-identity]、[测试][m-identity-test] |
| 07 | 模型明确知道当前发送者及被引用者是不是主人 | 部分，D01 | 当前普通回合已有主人前缀；自然聊天当前身份提示在 PR #230；被引用主人仍只是昵称/ID。[Shiori 可信引用标记][s-hub]、[引用渲染][s-reply-label]；[Meido 当前发送者][m-sender-label]、[引用拼接][m-reply-label] |
| 08 | 每个外部聊天独立显示、群聊/访客原始历史隔离 | 已具备 | 两边的群回合都不直接混入其他群或主人私聊原文。存储方式不同不等于缺会话隔离。[Shiori 视图][s-context]；[Meido 当前线程历史][m-turn-context]、[测试][m-context-test] |
| 09 | 主人从桌面换到私聊后按共享历史继续聊 | 部分，D02；含选型差异 | 已能双向带近期文字，但不是等价的共享消息历史组装。[Shiori 历史组装][s-history]；[Meido 24 条文字查询][m-owner-context]、[桌面注入][m-desktop-context] |
| 10 | 群 @ / 引用机器人触发、停止及查询 ID 命令 | 已具备 | 群消息实际按触发条件运行，控制命令不依赖让模型猜含义。[Shiori 群准入][s-hub]、[命令][s-commands]；[Meido 群触发][m-qq-trigger]、[控制命令][m-commands] |
| 11 | 每群旁听、每日上限、近期窗口、独立整理游标 | 已具备 | QQ 旁听独立于普通回合，默认每日 200 条；超额仍有有界近期窗口。[Shiori 旁听存储][s-listening]、[开关][s-listening-switch]；[Meido 旁听][m-listening]、[测试][m-listening-test] |
| 12 | 自动整理群笔记/摘要与成员资料，区分主人记忆 | 已具备 | 两边有实际整理与提交链路；共享范围和人工管理另列 13–16，不重复算这里。[Shiori 整理提交][s-consolidate]；[Meido 整理][m-consolidate]、[测试][m-consolidate-test] |
| 13 | 同一渠道同一群友跨群沿用一份档案 | 部分，D03；含选型差异 | Meido 可查到多个来源，却分别按线程存储/注入档案；不是一份共享档案。[Shiori 成员键/存储][s-member-store]；[Meido 档案表及更新][m-consolidate]、[聚合查询/注入][m-member-context] |
| 14 | 成员档案查看、编辑、删除及昵称历史查看 | 部分，D04 | 自动维护/模型查询已有，手机中没有对应人工管理入口；Shiori 的昵称历史也是只读。[Shiori 成员页][s-member-ui]、[客户端链路][s-phone-client]；[Meido 当前聊天页][m-phone]、[头像/详情][m-phone-message]、[后台查询][m-member-context] |
| 15 | 用户查看/编辑群及陌生人私聊的会话笔记 | 部分，D05 | 群笔记后台已有但无 UI；陌生私聊不具备 Shiori 同等会话笔记链路。[Shiori 外部会话判定][s-phone-info]、[笔记编辑][s-note-ui]；[Meido 群限定存储][m-group-context]、[当前聊天页][m-phone] |
| 16 | 用户查看最近动态摘要，角色感知其他外部会话概况 | 部分，D06 | 有当前群摘要后台；没有动态展示，注入也限当前群。跨群摘要共享需先决定边界。[Shiori 动态 UI][s-info-ui]、[摘要注入][s-activity]；[Meido 当前群注入][m-member-context]、[当前聊天页][m-phone] |
| 17 | 目录发现及按可达目标发送：群、成员、已知私聊、话题/@ | 已具备 | 已有真实目录查询与发送前校验，不能根据任意自报 ID 取得目标权限。[Shiori 目标工具][s-account-tools]、[投递服务][s-delivery-service]；[Meido 目录][m-directory]、[目标校验][m-send-target] |
| 18 | 主动消息发送到授权主人目标并写回聊天记录 | 已具备 | 主人目标来自绑定/显式许可；投递不是只往平台丢一条消息，结果会回写所属会话。[Shiori 推送同步][s-push]、[投递服务][s-delivery-service]；[Meido 主动投递][m-proactive-delivery]、[提醒推送][m-push] |
| 19 | 文字、图片、文本文件及引用附件进入阅读链路 | 部分，D07 | 大部分已具备；QQ 文件只有 file_id、没有直链时少了解析下载路径。[Shiori QQ 文件解析][s-qq-files]、[ID 换下载地址][s-qq-file-url]；[Meido QQ 解析][m-qq-media-parse]、[下载][m-qq-media-download]、[现有测试][m-attachment-test] |
| 20 | 平台富文本、长消息呈现与拆分 | 部分，D08 | QQ 长消息合并转发已具备；缺口主要在 Telegram 格式实体、QQBot 普通发送 Markdown、飞书非流式卡片/分段。[Shiori Telegram][s-tg-markdown]、[QQBot][s-qqbot-markdown]、[飞书][s-feishu-send]；[Meido Telegram][m-tg-send]、[QQBot][m-qqbot-transport]、[飞书][m-feishu-send] |
| 21 | Telegram/QQBot/飞书正文流式回复 | 已具备 | Telegram 编辑同条预览、QQBot C2C、飞书 CardKit 都有；QQ/NapCat 不流式，两边一致。[Shiori Telegram 流式][s-tg-stream]、[QQBot][s-qqbot-send]、[飞书][s-feishu-stream]；[Meido 流式调用][m-stream-dispatch]、[Telegram][m-tg-stream]、[QQBot][m-qqbot-stream]、[飞书][m-feishu-stream] |
| 22 | 外部 Telegram 中的正在输入及工具执行进度反馈 | 缺失，D09 | Meido 只转发模型正文更新，不转工具开始/完成事件，也没有 Telegram typing 请求。[Shiori 工具进度][s-tg-stream]、[typing][s-tg-typing]；[Meido 外部事件循环][m-stream-dispatch]、[Telegram API][m-tg-api] |
| 23 | Telegram 账号/聊天发送限流与 Telegram/飞书显式限流恢复 | 缺失，D10 | Meido 有预览更新节流，缺这里限定的跨发送队列与平台明确限流后的安全退避。不能因此说所有发送都无节流。[Shiori Telegram limiter][s-tg-limiter]、[飞书重试][s-feishu-retry]；[Meido Telegram API][m-tg-api]、[飞书传输][m-feishu-transport] |
| 24 | 引用目标被撤回后自动退为普通发送 | 缺失，D11 | Telegram/飞书引用请求被拒时，Meido 没有 Shiori 对应的无引用发送后备路径。[Shiori Telegram reply 参数][s-tg-reply]、[飞书后备][s-feishu-send]；[Meido Telegram reply 参数][m-tg-api]、[飞书请求][m-feishu-transport] |
| 25 | 真实回执、发送失败/不确定状态、重启后不盲目重发 | 已具备 | 两边都在发送前后记账；Shiori 工具账本以 pending 保留不确定，Meido 显式 uncertain，状态名不同不算缺失。[Shiori 账本][s-ledger]；[Meido 账本/恢复][m-ledger]、[真实回执测试][m-receipt-test] |
| 26 | 手机时间线、头像/来源、引用折叠、图片、分页、实时更新 | 已具备 | 基础查看链路已有；缺资料管理页面见 14–16。两边手机均为只读观察界面。[Shiori 手机][s-phone]；[Meido 手机][m-phone]、[消息组件][m-phone-message] |
| 27 | Grok Bot 固定连接、Webhook/回调双向文字会话 | 缺失，D12 | Shiori 新基线有真实插件与入站/发送链路；Meido 可选渠道集合仍是四种。[Shiori Grok 收发][s-grok-in]、[目标/发送][s-grok-out]；[Meido 渠道枚举][m-platform-switch] |

## 12 项差距明细与优先级（影响为推断，优先级仅建议）

| 差距 | 用户可感知的例子 | 当前缺到哪里 | 建议 |
| --- | --- | --- | --- |
| D01 身份提示不完整 | 群友 @ 女仆并引用主人的话，模型知道引用昵称，却不知道那是主人 | 普通当前发言已标主人；PR #230 加强当前发言和自然回合；被引用者主人/女仆身份仍未等价标注。引用身份只影响理解，不应把引用者的权限转移给当前群友。[Shiori 引用身份][s-reply-label]、[Meido 拼接][m-reply-label] | P1；跟随当前身份问题补齐，但与 PR #230 已做范围区分 |
| D02 跨端接续只是近期文字补充 | 私聊发过图片或较早的多轮细节，回桌面让她接着分析 | Meido 默认另取 24 条 completed 文本并拼提示块；Shiori 的授权主人消息进入同一历史视图，可重建仍存在的图片附件，再受正常上下文预算裁剪。不能说 Shiori 无限保留所有原文，也不能说 Meido 完全不共享历史。Meido 桌面拼接还未保留该片段的 user/assistant 标签。[Shiori 历史窗口][s-history]、[历史图片][s-history-media]、[Meido 查询][m-owner-context]、[桌面组装][m-desktop-context] | P2；先明确图片、角色标识、历史长度需要达到的用户结果，再评估 ADR-0009 |
| D03 群友档案按群分裂 | 同一个 QQ 群友从 A 群来到 B 群，已学到的爱好不会自动成为 B 群的档案 | Shiori 一角色一渠道一成员一份档案；Meido 主键含 thread_id，自动注入也是当前线程。Meido 已有昵称来源聚合查询，不能称为完全不认同一平台 ID。[Shiori][s-member-store]、[Meido][m-member-context]、[明确隔离测试][m-context-test] | P2；先决定跨群可共享哪些事实，不直接移除隔离 |
| D04 成员档案没有人工管理入口 | 想纠正女仆对某人的错误印象、删除档案、查看旧昵称 | Shiori 有查看、保存、删除的完整 UI/bridge；Meido 有后台成员资料与查询工具，聊天头像却没有打开档案的行为。[Shiori][s-member-ui]、[Meido][m-phone-message] | P1 |
| D05 会话笔记没有可用的用户流程 | 想查看女仆记住的群规并改正，或维护陌生人私聊备注 | Meido 群笔记有后台存储与模型工具，无人工 UI；陌生人私聊还受 get_group 限定，不能仅加页面就达到 Shiori 的外部会话范围。[Shiori][s-note-ui]、[外部会话范围][s-phone-info]、[Meido][m-group-context] | P1；分别验收群与陌生私聊 |
| D06 最近动态摘要不可见 | 想快速看她最近在哪个群聊了什么 | Meido 存了当前群摘要却没有展示；Shiori 可展示，并将有限的其他外部会话摘要注入角色提示。后者是一项共享策略差异，不因补 UI 就自动授权跨群注入。[Shiori UI][s-info-ui]、[提示块][s-activity-prompt]、[Meido 当前群提示][m-member-context] | P1 补查看入口；跨群摘要注入先确定共享边界 |
| D07 QQ 无直链文件不能进入已有阅读链路 | 群里上传 txt/md，事件只带 file_id，没有 url | Shiori 保留文件描述并通过 OneBot 解析地址；Meido 只保留 url/文件名，下载时没有 url 就跳过。现有测试给的是已有直链，不能据此宣布全部 QQ 文件已支持。[Shiori][s-qq-files]、[解析接口][s-qq-file-url]、[Meido][m-qq-media-download]、[测试][m-attachment-test] | P1；不扩大成任意文件或语音支持 |
| D08 富文本/长文渠道呈现不一致 | Telegram 收到原样 Markdown；飞书主动长消息没有同等卡片拆分 | Telegram 普通发送缺格式实体；QQBot 非流式发送为 msg_type=0；飞书普通发送是整段 text。QQBot/飞书已有的流式 Markdown 不覆盖这些路径。QQ 长文合并转发两边已有，不能重复列缺。[Shiori TG][s-tg-markdown]、[QQBot][s-qqbot-markdown]、[飞书][s-feishu-send]；[Meido TG][m-tg-send]、[QQBot][m-qqbot-transport]、[飞书][m-feishu-send] | P2 |
| D09 外部过程反馈缺失 | Telegram 里任务执行较久，只能等正文，无法看出工具正在做什么 | Shiori 有 typing 和工具开始/结束状态；Meido 外部循环只消费正文更新。此项不等于正文流式缺失。[Shiori][s-tg-stream]、[typing][s-tg-typing]、[Meido][m-stream-dispatch] | P2；思考内容展示策略可独立选择，不应为了对齐直接暴露全部内容 |
| D10 平台明确限流后的恢复缺失 | 多条消息或更新触发 Telegram/飞书频率限制，回复直接失败或留下不确定结果 | Shiori 有 Telegram 全局/聊天限流及 RetryAfter、飞书明确限流退避；Meido 的单条流式节流不能替代它们。不建议对超时/连接断开一律重发，投递结果不确定时仍需避免重复。[Shiori TG][s-tg-limiter]、[飞书][s-feishu-retry]、[Meido TG][m-tg-api]、[飞书][m-feishu-transport] | P1 |
| D11 引用失效后的降级缺失 | 用户撤回了触发消息，生成完的回复因为无法引用而发送失败 | Shiori Telegram 使用 allow_sending_without_reply，飞书在明确引用拒绝后改普通发送；Meido 请求仍带原引用，未实现同等后备行为。[Shiori TG][s-tg-reply]、[飞书][s-feishu-send]、[Meido TG][m-tg-api]、[飞书][m-feishu-transport] | P1；只对明确拒绝做安全后备 |
| D12 Grok Bot 渠道缺失 | 希望角色与一个固定外部 Grok Bot 双向聊天 | Shiori 有角色所有的固定 Webhook、认证回调、消息去重与交流链上限；Meido 没有第五类渠道。它不是 QQ、Telegram 等现有接入失效。[Shiori][s-grok-in]、[发送][s-grok-out]、[Meido][m-platform-switch] | P3；有使用需求再引入 |

## 平台与消息类型范围（不重复计数）

| 平台 | 双方已核到的主要范围 | 不应夸大的地方 |
| --- | --- | --- |
| QQ / NapCat | 私聊、群聊、群临时目标；@/引用触发；QQ 旁听；文字/图片、受支持文本文件阅读；长文合并转发 | 非流式；Meido 文件 ID 解析有 D07 差距；已有图片/文件段解析不等于支持语音识别或视频理解。[Shiori 文件][s-qq-files]、[长文][s-qq-forward]；[Meido 文件下载][m-qq-media-download]、[长文][m-qq-forward] |
| Telegram | 私聊/群聊、已观察话题、图片/文档和引用、私聊正文流式 | 群聊不按私聊流式处理；限流和格式能力仍有 D08–D11 差距。[Shiori 媒体][s-tg-media]；[Meido 入站][m-tg-in]、[流式][m-tg-stream] |
| 官方 QQBot | C2C 私聊、文字/图片、流式替换 | 两边均不提供相同链路的群聊；不能把任何附件都当已支持文件发送。[Shiori][s-qqbot-send]；[Meido][m-qqbot-stream] |
| 飞书 | 私聊、文字/富文本接收、图片/文件接收、CardKit 流式 | 双方均拒绝群聊；普通出站卡片/长文/频控存在差距。Meido 能发本地文件，不宜反向假设 Shiori 所有出站媒体也都支持任意文件。[Shiori 入站][s-feishu-private]、[发送][s-feishu-send]；[Meido 入站][m-feishu-in]、[发送][m-feishu-send] |
| Grok Bot | 仅 Shiori 有固定 Bot 私聊文字接入 | 不是通用群聊平台；上游明确拒绝媒体和任意其他目标。[Shiori][s-grok-out] |

## 不计为本轮 Shiori 功能缺口的事项

1. **每群彻底关闭回复：两边 main 均无，Meido PR #230 待合入。** Shiori 的每群旁听开关不是每群回复开关；账号级 group_enabled 也不是每群开关。[Shiori][s-hub]、[Meido][m-group-settings]
2. **QQ 群自然接话与表情包学习/使用：Meido main 已新增的扩展。** Shiori 本轮群路由仍是 @/引用触发；不能把 Meido 的自然聊天漏传身份问题当成 Shiori 同类自然聊天更完善的证据。[Shiori][s-hub]、[Meido 自然回合][m-natural]
3. **动态渠道插件：架构选型差异。** Shiori 通过渠道插件声明注册；Meido ADR-0010 明确首期采用内置渠道，不引入动态插件加载。Grok 用户能力已计 D12，不再把“没有它的插件框架”计第二个缺口。[Shiori Grok 注册][s-grok-plugin]、[Meido ADR-0010][m-adr10]
4. **给外部 Bot 显式授予完整工具：可选授权模型差异。** Shiori 有 allow_full_tools 且须插件能力允许；Meido 沿用主人/外部来宾边界。不是普通群友都应取得主人权限，也不是本轮应自动打开的能力。[Shiori 检查][s-full-tools]、[Meido 来源权限][m-send-target]
5. **手机代发输入框：两边均无相同用户流程。** Shiori 手机页明确为只读观察；Meido 手机只读不能据此算少了一套聊天发送端。[Shiori 手机][s-phone]、[Meido 手机][m-phone]
6. **官方 QQBot/飞书群聊、通用语音/视频理解：不凭平台本身能做就算 Shiori 已实现。** 本轮核到的适配器链路不能证明这些上游功能，因而不记“我们缺”。[Shiori QQBot][s-qqbot-send]、[飞书][s-feishu-private]、[Telegram 媒体注册][s-tg-media-reg]

## 核验方式、证据限制与后续建议

本轮完成的是**固定源码和现有测试内容核验**：顺着入站解析 → 宿主准入/身份 → 历史及记忆注入 → 工具/回复发送 → 手机展示的调用链核对；没有用 README、接口名称或一个能力标志证明完整实现。对于“无入口/无实现”，同时检查相邻调用路径和相关目录的全局检索结果。

已阅读的行为证据包括：Meido 平台身份/解绑、跨线程隔离、旁听上限与独立游标、托管 QQ 状态/重试、附件阅读（测试事件带直链）、缺少真实回执保持不确定；Shiori 手机成员与笔记交互、Telegram Markdown 分段及限流重试、成员旧昵称查询。**这些是已存在测试的内容核验，本轮未重新运行测试，也未做真机收发验收。** [Meido 身份测试][m-identity-test]、[上下文测试][m-context-test]、[旁听测试][m-listening-test]、[附件测试][m-attachment-test]、[回执测试][m-receipt-test]；[Shiori 手机测试][s-phone-test]、[Telegram 测试][s-tg-test]、[成员测试][s-member-test]

建议顺序（尚未立项）：先完成当前 PR #230 的合入与真实 QQ 验收，再补 D01 引用身份、D04–D07 的 QQ 可见体验；需要稳定使用 Telegram/飞书时优先 D10/D11。D02/D03 和 D06 中跨群摘要部分先确定共享边界与验收场景；D08/D09 做体验增强；D12 按实际外部 Bot 使用需求决定。

真实平台验收应分别覆盖：配对主人在多个群的普通/自然发言与被引用发言；每群关闭后 @/引用/正在生成回复均不再发送；QQ 无直链文件；Telegram/飞书的实际格式、限流与源消息撤回。未完成这些验收前，不宜从本报告推出“与 Shiori 完全一致”或“生产问题已经全部解决”。

[s-hub]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/channels/hub.py#L126-L205
[s-identity]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/identity/models.py#L81-L176
[s-ownership]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/accounts/registry.py#L245-L289
[s-account-runtime]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/accounts/runtime_state.py
[s-managed]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/qq/backend/managed_napcat.py
[s-reply-label]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/packages/sdk/python/shiori_sdk/channels/reply_context.py#L53-L64
[s-context]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/conversation/context_scope.py#L250-L264
[s-history]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/helpers.py#L30-L44
[s-history-media]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/session/manager/helpers.py#L132-L161
[s-commands]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/channel/commands.py
[s-listening]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/conversation/listening_store.py
[s-listening-switch]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/conversation/listening_switches.py#L16-L130
[s-consolidate]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/memory/markdown/listening.py#L213-L323
[s-member-store]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/memory/member_profiles.py#L39-L194
[s-member-ui]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneMemberProfilePage.tsx#L35-L76
[s-phone-client]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/phoneMemoryClient.ts#L39-L79
[s-phone-info]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/phoneChatInfo.ts#L29-L40
[s-note-ui]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneGroupNoteSection.tsx#L19-L40
[s-info-ui]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneChatInfoSections.tsx#L44-L82
[s-activity]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/memory/group_environment.py#L136-L197
[s-activity-prompt]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/prompt_block.py#L436-L456
[s-account-tools]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/tools/account_delivery.py#L30-L145
[s-delivery-service]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/account_delivery/service.py
[s-push]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/conversation/push_sync.py#L57-L155
[s-qq-files]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/qq/backend/channel/files.py#L89-L140
[s-qq-file-url]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/qq/backend/accounts_actions.py#L242-L260
[s-qq-forward]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/qq/backend/accounts_actions.py#L267-L329
[s-tg-markdown]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/utils/sending.py#L72-L139
[s-tg-stream]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/channel/streaming.py#L73-L181
[s-tg-typing]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/channel/outbound.py#L247-L268
[s-tg-limiter]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/utils/limiter.py#L15-L90
[s-tg-reply]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/utils/sending.py#L23-L27
[s-tg-media]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/channel/media.py#L26-L156
[s-tg-media-reg]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/channel/lifecycle.py#L130-L153
[s-feishu-private]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/feishu/backend/channel.py#L289-L300
[s-feishu-send]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/feishu/backend/channel.py#L697-L764
[s-feishu-stream]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/feishu/backend/streaming.py
[s-feishu-retry]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/feishu/backend/api.py#L292-L316
[s-qqbot-send]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/qqbot/backend/outbound.py#L25-L149
[s-qqbot-markdown]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/qqbot/backend/outbound.py#L193-L198
[s-ledger]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/accounts/delivery_ledger.py#L69-L126
[s-phone]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneChatPage.tsx#L15-L56
[s-grok-in]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/backend/connection.py#L204-L258
[s-grok-out]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/backend/delivery.py#L15-L59
[s-grok-plugin]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/backend/plugin.py#L13-L33
[s-full-tools]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/accounts/tool_access.py#L19-L50
[s-phone-test]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneConversationScreens.test.tsx#L36-L132
[s-tg-test]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/tests/test_utils.py#L47-L171
[s-member-test]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/tests/backend/agent/tools/test_member_lookup.py#L18-L69
[m-identity]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/multichannel.py#L989-L1073
[m-identity-test]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/tests/python/test_channel_identity_parity.py#L36-L81
[m-account-register]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/multichannel.py#L704-L751
[m-account-test]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/tests/python/test_channel_identity_parity.py#L91-L107
[m-platform-switch]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/main.py#L3234-L3266
[m-platform-test]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/tests/python/test_channel_settings_api.py#L357-L378
[m-managed]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/napcat_managed.py
[m-managed-test]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/tests/python/test_channel_settings_api.py#L149-L270
[m-qq-in]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/napcat.py#L106-L168
[m-tg-in]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/multichannel.py#L1654-L1780
[m-qqbot-in]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/qqbot.py#L61-L99
[m-feishu-in]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/feishu.py#L142-L199
[m-sender-label]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/session_store.py#L45-L60
[m-reply-label]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/main.py#L6137-L6143
[m-turn-context]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/main.py#L6121-L6148
[m-context-test]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/tests/python/channels/test_owner_memory_context.py#L79-L90
[m-owner-context]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/multichannel.py#L1108-L1133
[m-desktop-context]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/main.py#L6601-L6613
[m-qq-trigger]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/napcat.py#L146-L177
[m-commands]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/main.py#L2751-L2801
[m-listening]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/listening.py#L20-L199
[m-listening-test]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/tests/python/channels/test_listening_parity.py#L31-L85
[m-consolidate]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/consolidation.py#L49-L207
[m-consolidate-test]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/tests/python/channels/test_consolidation_parity.py
[m-group-context]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/group_tools.py#L78-L113
[m-member-context]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/group_tools.py#L174-L238
[m-group-settings]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/main.py#L3961-L3990
[m-phone]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/web/src/features/channels/ChannelView.tsx#L649-L733
[m-phone-message]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/web/src/features/channels/PhoneMessage.tsx#L20-L48
[m-directory]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/host.py#L291-L354
[m-send-target]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/host.py#L475-L537
[m-proactive-delivery]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/proactive_runtime/channel_delivery.py#L101-L193
[m-push]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/push.py#L59-L116
[m-qq-media-parse]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/napcat.py#L129-L140
[m-qq-media-download]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/napcat.py#L399-L412
[m-attachment-test]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/tests/python/channels/test_attachment_turn.py#L20-L140
[m-qq-forward]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/napcat.py#L260-L312
[m-tg-send]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/telegram.py#L56-L128
[m-qqbot-transport]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/transports.py#L413-L429
[m-feishu-send]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/feishu.py#L213-L291
[m-stream-dispatch]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/main.py#L6259-L6286
[m-tg-stream]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/telegram.py#L168-L225
[m-qqbot-stream]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/qqbot.py#L176-L273
[m-feishu-stream]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/feishu.py#L323-L465
[m-tg-api]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/multichannel.py#L211-L264
[m-feishu-transport]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/transports.py#L829-L855
[m-ledger]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/multichannel.py#L1365-L1425
[m-receipt-test]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/tests/python/channels/test_delivery_receipts.py#L20-L50
[m-natural]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/main.py#L2885-L2955
[m-adr9]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/docs/adr/0009-multichannel-conversation-threads.md
[m-adr10]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/docs/adr/0010-channel-lifecycle-and-managed-qq.md
