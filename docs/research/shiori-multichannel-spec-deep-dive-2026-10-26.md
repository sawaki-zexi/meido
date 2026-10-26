# Shiori 多端会话 Spec 深核补充（2026-10-26）

状态：当前。下文“计划”是供 Spec 采用的推荐方案，不表示实现完成或架构迁移已获批准。
事实来源：两仓固定提交源码、现有行为测试、平台官方 API；本轮不运行真实账号、不发送渠道消息、不运行全量测试。
最后核验：2026-10-26。Shiori main 为 d149de7f052f77c5bb88d4d1393d48dce1870875；Meido main 为 8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750。
GitHub API 本轮确认 HEAD 未变；本轮 git fetch 失败，不能作为已同步的证据。PR #230 仍未合入，待合入版本为 25de6e2e94d9c375587138bcc2aa163152bbbbf0。
关系：补充[上一轮差距报告](shiori-multichannel-parity-2026-10-26.md)，支撑[对齐 Spec](../specs/shiori-multichannel-alignment-spec.md)。用户已选择按 Shiori 行为改进；本文件细化行为和风险，不重新盘点缺口或创建 Ticket。

## 1. 深核后需要修正的概括

| 已提取的事实 | 对 Spec 的影响 |
| --- | --- |
| Shiori 的成员键是角色目录下的 transport channel + sender_id，channel 不总等于平台名：QQ 为 qq，Telegram 带 bot ref，飞书带 app ref。[成员键][s-member-key]、[QQ][s-qq-channel]、[Telegram][s-tg-channel]、[飞书][s-feishu-channel] | 不可机械翻译成“同平台所有账号的同 ID 共用一份档案”；应显式定义平台身份和应用身份作用域。 |
| 主人共享历史是当前绑定确定的消息视图，并有每私聊授权起点；群聊即使当前发送者是主人，也仍是外部历史视图。[历史视图][s-context] | 身份、工具权限、原始历史范围是三件事；“群里认识主人”不自动导入主人私聊原文。 |
| Meido 已有跨端 search_messages / fetch_messages；24 条纯文本限制属于自动上下文附加片段，不能概括成所有跨端查询只支持 24 条。[查询][m-search]、[自动片段][m-snippets] | 改进重点是自动历史组装、角色/媒体结构和共同授权接口，不重复实现已有搜索。 |
| Shiori 最近动态为三天内更新的最多五个外部会话，外部回合排除当前会话；1500 字是成员速记预算，非动态摘要总预算。[动态][s-activity]、[成员注入][s-member-render] | 明确窗口、条数、覆盖回合及独立预算，不能声称有上游不存在的硬上限。 |
| Shiori 笔记有持久编辑 revision，成员档案只有内容快照比较；删除成员文件不是“永久忘记此人”。[提交检查][s-external-writes]、[成员删除][s-member-store] | 人工修订保护与永久禁止再学习不是一项功能。Meido 应为编辑、清空、删除、重建统一使用持久版本。 |
| Grok README 与代码、测试冲突：文档称淘汰父消息应拒绝，未知或已淘汰 reply_to 实际开新链。[文档][s-grok-readme]、[实现][s-grok-exchange]、[测试][s-grok-tests] | 若 Spec 拒绝此类关联，是有意收紧上游行为，必须标明，不能写成原样复刻。 |

## 2. 已提取：身份、历史与作用域的真实边界

Shiori 从宿主确认的账号和绑定认出当前发送者；群回复引用的发送者另行判定。插件自报 sender_is_user、reply_to_sender_is_user 等字段被投影层剥离，再由宿主补充；引用正文和昵称不能授予主人身份。[入站][s-hub]、[受信字段][s-projection]、[引用称呼][s-reply]

身份绑定有 platform / account 两种适用范围；认人还要找到该角色实际承载这个 transport channel 的账号。重复配对保留已知聊天的最早 context_since；解绑后再绑定产生新的授权边界。消息原有 sender_is_user 是历史事实，解绑不会回写所有旧消息。[身份][s-identities]、[绑定写入][s-bind]、[来源元数据][s-source]

Shiori 用消息的回合开始时间（缺少时才用消息时间）比较授权起点，防止配对前启动、配对后才完成的回复进入共享历史；只包含可作为已投递历史的消息，随后走正常上下文组装与预算裁剪，不等于无上限加载一切。[边界][s-boundary]、[上下文][s-context]、[组装][s-history]

Meido 已保存独立 transcript、真实账号/线程归属、主人私聊授权起点和冻结来源。工具执行前会检查当前账号、绑定 identityId/pairedAt、线程及授权起点；解绑会删除共享聊天和主动目标，既有查询不是无授权的任意 session 读取。[绑定][m-bind]、[冻结来源][m-authority]、[工具授权][m-tool-authority]

Meido 的完整消息读取器已处理附件、完整工具调用组、当前输入围栏及晚到回复顺序；跨端文本查询也保留 sourceSessionKey/sourceSequence。因此“必须把所有消息物理搬到一个 session 才能达到 Shiori 行为”没有源码依据。[当前读取器][m-runtime-history]、[跨端查询投影][m-owner-messages]

### 计划：Spec 应固定的 scope 矩阵

| 发起位置/身份 | 自动原始历史及消息查询 | 成员资料/会话笔记 | 各会话最近动态 |
| --- | --- | --- | --- |
| 桌面主人 | 当前角色桌面 + 当前获授权主人私聊，逐聊天遵守授权起点 | 当前角色资料可管理；当前绑定主人不作为群友资料展示 | 当前角色其他外部会话，三天、最多五条 |
| 已绑定主人私聊 | 与桌面同一个授权历史视图；手机时间线仍显示当前私聊 | 不显示外部会话信息页；旧访客资料不自动转为主人记忆 | 同上，当前主人私聊不作为外部摘要来源 |
| 主人在群中发言 | 仅当前群原文；保留既有主人工具权限策略 | 当前群笔记；平台/应用身份相同的群友共用资料 | 其他外部会话摘要，不导入其他群原文 |
| 群友或陌生私聊 | 仅当前外部线程原文、外部工具范围 | 当前会话笔记；当前涉及成员的共享资料，主人排除 | 同角色其他外部会话摘要，排除当前线程 |
| 主动、定时或 Drift 回合 | 沿对应来源授权；不能因无用户输入获得任意历史 | 按该回合授权读取 | 与 Shiori 相应回合对齐；不因此增加投递或工具权限 |
| 账号暂停/离线；账号已移除 | 本地主人仍可查看保留历史；运行时不得以失效账号取回或发送 | 暂停/离线可本地管理；移除账号历史沿 Meido 现有只读约定 | 失效绑定不再构成主人共享授权；保留资料不等于仍能联网 |

矩阵为目标行为：用户已选择对齐跨群资料和摘要，不再将所有共享边界留作待决定问题；外部原文隔离及跨角色隔离仍保留。现有 ADR-0009 的“近期片段”和逐线程成员整理需要后续架构记录解释调整。[现有 ADR][m-adr]

### 计划：统一历史投影，保留独立 transcript

1. 用同一个宿主授权视图服务自动上下文、search/fetch 和历史媒体访问；输入来自真实回合、账号、线程、发送者和当前绑定，模型不可提交任意 role/session/path 扩大范围。
2. 保留原消息、运行、工具记录及稳定 ID；视图按回合组装、稳定排序、去重，然后使用共同上下文预算，不再额外截成 24 条纯文本。保留 role、来源、引用、附件和完整工具组，不能插入孤立工具结果。
3. 保留当前输入围栏：后排队用户消息不提前出现；较早回合晚完成的内容按既有回合顺序处理。被拒绝、失败或投递不确定的助手文本不冒充已经送达的跨端历史。
4. 跨端媒体由“当前可见消息 ID → 该消息附件记录 → 角色存储中规范化文件”解析；查询结果不能直接接受任意本机路径或平台远端路径。现有 QQ 附件工具只白名单当前回合，不能直接放宽成全角色目录访问。[附件工具][m-attachment]
5. 授权读取前检查，异步读取/下载返回后再次检查，交给模型前再核对当前 binding/account generation；发送、重试和流式下一步也检查。撤权之前已返回的内容无法收回，但之后不得继续扩大读取或投递。
6. 重复配对保留现有历史起点并刷新运行授权代次；解绑后重配从新起点开始。旧工具句柄、媒体引用和待发送工作不能因身份相同而跨越撤权/重配继续有效。
7. 此方案是拟议的读取架构；不需要先物理合并 transcript。若改选重写 session/运行归属的存储迁移，必须单独替代 ADR-0009 并获人工批准，本研究不视为该批准。

## 3. 已提取：成员共享、主人排除与自动整理

Shiori 成员档案按角色存储，并记录昵称历史和曾出现的 thread_ids；触发者、被 @ 者、被引用者注入完整档案，当前窗口其他成员注入速记。完整档案不再截断，因此 1500 字只是裁剪速记的目标，人工写入长档案可能超过它。[存储][s-member-store]、[注入][s-member-render]

成员提取的主人排除同时看历史可信 sender_is_user 和当前绑定。已绑定主人不会出现在成员资料页；当前绑定前产生的资料可以仍在存储中，但不因此自动转成主人资料或抹掉配对前历史边界。[排除][s-member-key]、[管理接口][s-memory-ui]

Meido 当前成员主键含 thread_id，查询可聚合多个来源却仍逐群保存；整理仅排除消息内已有 senderIsOwner，缺少 Shiori 的“此刻已绑定”补判。笔记和摘要存储工具仅接受 group，陌生私聊需要一起补齐。[整理][m-consolidation]、[群上下文][m-group]

### 计划：身份和资料键

| 对象 | 推荐归属键 | 明确排除的合并依据 |
| --- | --- | --- |
| 主人身份 | 沿现有全局平台/账号绑定；每次匹配再验承载账号的角色归属 | 昵称、引用正文、模型声称、适配器自报 owner |
| QQ / Telegram 成员 | role_id + platform + 平台身份作用域 + sender_id | 跨角色、跨平台；相同昵称 |
| 飞书 / 官方 QQBot 成员 | role_id + platform + 应用身份作用域 + 可信不可变应用身份 + sender_id | 不同应用的相同 open_id；新账号占用旧显示名 |
| 会话笔记和最近动态 | role_id + 原外部 thread_id | 合并群笔记；把主人私聊当作外部会话 |
| 成员来源和昵称历史 | 共享成员键 + 原 thread/account + 来源消息/时间 | 用最后写入覆盖所有群的来源证据 |

这是一种显式作用域设计，不能声称 Shiori 对所有插件已经使用相同规则：其 Telegram/飞书实际 transport 命名带账号 ref，而 QQ/QQBot 命名不同。Meido 应沿平台/应用 ID 的真实稳定性定义键；对无法验证的历史应用身份保守隔离，不按当前账号猜测。[适配器命名][s-tg-channel]、[飞书命名][s-feishu-channel]、[QQBot 命名][s-qqbot-channel]、[Meido 账号快照][m-account-snapshot]

计划要求共享档案保留结构化 seen-in 来源与昵称最后出现时间；不同群的相同成员更新一份档案，原群原文依旧各自隔离。模型只能更新本批确实出现且非主人的成员。开始整理和提交前均核验主人绑定，绑定变化时作废旧提取结果；历史上是主人的发言不因解绑被重新当成访客自述。

## 4. 已提取与计划：笔记、最近动态和手机管理

Shiori 三天窗口按摘要实际更新时间计算，最多五个会话，新在前；外部回合先排除自己再取五条。当前已绑定主人私聊不进入列表，原记录不迁移、不删除。源码未对汇总段落设置固定字数硬上限。[选择/渲染][s-activity]

普通主人与外部回合都会注入动态；主动回合及 Drift 也有注入路径。群/陌生私聊的当前笔记单独注入；摘要是系统提供的外部背景，不是主人新指令，也不代表原文检索权。[普通回合][s-activity-passive]、[主动][s-activity-proactive]、[Drift][s-activity-drift]、[提示组装][s-assembler]

已提取：Shiori 用当前外部线程窗口的合规 user/assistant 正文整理会话环境，包含角色回复和可信主人的群发言；只有成员候选排除角色和主人，并过滤 context-frame / 维护消息。Meido 当前先把 batch 缩成“非主人 user 消息”再传给摘要器，因而仅增加 UI 或跨群注入仍无法对齐。目标应分开“完整会话环境输入”和“允许更新的外部成员清单”，不能让主人排除规则同时删掉群事件的必要上下文。[分组与渲染][s-external-input]、[摘要与成员规则][s-external-prompt]、[Meido 输入筛选][m-consolidation]

手机信息页覆盖群及陌生私聊，提供成员列表、昵称历史、档案/速记保存删除、会话笔记编辑和只读最近动态；主人私聊排除。后端验证角色、线程、成员曾在该线程出现及当前主人排除，不应只由前端藏按钮。[后端][s-memory-ui]、[信息页][s-phone-info]、[导航状态][s-phone-screens]

计划：沿这些回合和范围接入动态；使用正常上下文预算内的独立限额，过长时按完整条目从最旧开始裁剪，空列表不注入。展示摘要更新时间；读取不刷新时间。将摘要文字作为数据，不能执行其中的命令，也不能由摘要中的角色/会话名扩大原文检索权限。

计划：本地管理入口不依赖平台在线；切换成员页和返回聊天保留滚动与笔记草稿。保存使用 expected_revision，冲突时保留用户草稿并展示最新内容，不默默覆盖。删除确认写清“此渠道身份在当前角色共用的档案会被清空，后续整理可能重新建立”；主人绑定变化后页面立即失效，已打开的旧页也不能继续保存。

## 5. 已提取与计划：人工编辑、删除和并发整理

Shiori 整理先读快照、模型生成后再持锁比较全部待写对象；任一个已变则整批不写。笔记手动编辑即使修改后恢复原文也增加持久 revision，测试覆盖重启后的 ABA；成员比较的是完整内容快照，未提供同等持久 revision 或删除墓碑。[写回][s-external-writes]、[ABA 测试][s-edit-tests]

Shiori UI 保存/删除也使用同一个锁；删除是删除当前档案文件。旧快照不匹配会阻止本次陈旧结果，但重新读取后的整理仍能再次生成档案，甚至可以来自尚未消费的同一积压批次；这不是永久禁止记住此人。[UI 写入][s-memory-ui]、[准备与游标][s-consolidation]

Meido 已有整理事务、游标和快照检查，但成员检查仅在当前行存在时比较 updated_at；行被删除时不会据此拒绝旧结果。新 UI 若直接删除现有行，就会暴露旧整理结果重新写入的竞态。[现有检查][m-consolidation]

计划写回契约：

- 每份成员档案和会话环境保存持久 revision，删除保留 tombstone/version；编辑、清空、删除、重建均单调递增，时间戳不能替代版本。无变化的保存可不增加版本。
- 整理准备记录各对象版本、来源游标、成员作用域、相关绑定版本；模型等待不持数据库写锁。提交时同一事务全部比较，全部成功才更新资料和游标，任一冲突则整批不提交。
- 人工编辑优先于已经准备的旧结果；重新读取人工内容后，后续整理仍可更新它。删除清空当前档案，不承诺永久禁学；如需永久忽略某成员，应是另一个显式功能，本 Spec 不暗含它。
- 并发群 A/B 整理同一成员时只允许一个旧版本提交，另一个重读新档案后重试；不得仅按线程锁让最后返回的模型结果覆盖前者。
- 清空后恢复相同文字仍使旧整理无效；角色删除开始后不得再写入或恢复档案。取消/失败/冲突不推进消费游标，也不把未完成摘要显示成成功。

## 6. 计划：迁移与回退，不丢旧资料来源

1. 迁移范围为共享成员键、来源关系、会话环境对陌生私聊的支持、持久编辑版本；保留已有 transcript、message/run ID、引用、附件和原整理游标，不重新投递旧消息。
2. 先按旧 role/thread/account 找可信平台/应用身份，再生成新键；历史来源快照只能使用其中真实记录的身份，不能把 account_id fallback 当已验证 app_id。账号已删且身份不足的资料进入独立 legacy scope，保留可查看来源，不合并到当前新账号。
3. 同键同内容去重并合并来源；昵称按可信观察时间合并。不同内容不按数据库返回顺序或最后迁移行静默覆盖：完整保留每份旧文、来源群、原时间，标为待归并。
4. 备选是暂用来源速记并后台模型归并；[最终 Spec 草案](../specs/shiori-multichannel-alignment-spec.md)推荐采用更明确的迁移边界：不同内容保留为“需整理”候选，由主人选择或编辑发布规范档案；未确认的冲突内容不跨群自动注入，旧文仍按来源可查看，迁移不调用模型。自动归并仅保留为未采用备选，不作为实施指令。
5. 历史资料中现已绑定主人者保留来源记录但从成员页面、提取和注入排除；既不把旧访客资料自动提升为主人记忆，也不改写旧消息身份。解绑后的展示依当前状态，但历史主人发言仍不变成新成员证据。
6. 采用可重复运行、版本化的事务迁移；唯一约束防重复，失败不推进迁移标记。自动迁移期间暂停受影响整理，读取可用旧模式；切换后先验证计数/来源归属，再停用旧写入口。
7. 回退保留旧表/归档和映射；新增共享档案不能任意拆回并覆盖每群旧文。若已产生新人工编辑，回退只关闭新能力并保留新数据待恢复，不声称能无损降级为旧写模型。
8. 上述迁移是推荐架构，需在实现前计划及替代/补充 ADR 中批准；研究与 Spec 不执行生产数据迁移。

## 7. 已提取与计划：接收、格式、进度和投递恢复

QQ 无 URL 文件可用 file_id 补取直链。NapCat 文档旧示例与当前接口字段不完全一致：当前源码群接口为 group_id + file_id，私聊为 file_id，URL 可缺失且有 Packet 能力条件；实现应匹配 Meido 受管固定版本的 fixture。[官方说明][napcat-file]、[群接口][napcat-group]、[私聊接口][napcat-private]

Shiori 文本文件限定 txt/md、2 MiB、UTF-8 和下载超时，失败保留文件名和未读取原因。计划保持这些可解释限制；跨端历史文件还要受第 2 节当前绑定/所有权验证，平台远端绝对路径不可当成本机文件打开。[文件处理][s-qq-files]

Telegram 文本实体 offset/length 按 UTF-16；retry_after 单位为秒；引用回退必须保留原 chat/topic，跨 chat/topic 时 allow_sending_without_reply 不能视为通用保证。[实体][tg-entities]、[限流参数][tg-retry]、[引用][tg-reply]

Shiori Telegram 每段成功后先调用 on_receipt 再发下一段；Meido 当前循环仅返回最后消息 ID，后段失败时不能完整表达前面已送达的段。[Shiori 分段][s-tg-sending]、[Meido 分段][m-tg-sending]

计划投递契约：逐段持久保存已确认回执、平台 ID 和序号；明确拒绝且可证明该段未送达时，仅重试该段。限流等待后、每段前、引用/格式回退前重验冻结来源、账号与群回复代次。超时、断线、缺回执 ID 或已有部分成功时不得重放整个文本；停止尾部并分别显示已送达、不确定、未发送。

已提取：Meido 普通 Runtime 从准入到运行清理维持同角色串行；现有 provider/tool timeout 不是整轮总期限。[运行清理][m-runtime-release]、[单步超时配置][m-runtime-timeouts]。计划：限流器不破坏运行内工具/流式的角色锁契约，完成后的投递等待不重新占用该锁；首次进入逻辑消息调度时单独冻结有限投递期限，若来源已有更早期限则取最早。Spec 建议初值 120 秒，这是新的产品建议，不是上游已具备的统一期限。

保留现有 pending/sent/failed/uncertain 总状态，增加分段详情，不把 HTTP 接受等同最终可见。Shiori 飞书 fallback 捕获较宽，不能原样当安全模板；只对明确引用失效或格式拒绝改用同一目标的无引用/简单格式，权限错误不切目标、不改成主动发送。[飞书回退][s-feishu-send]

官方 QQ 支持 Markdown msg_type=2，被动发送使用 msg_id/msg_seq 并受时效与次数限制；格式降级仍保留原消息关联，不能变成主动消息。[QQ 官方消息][qq-message]。飞书官网本轮未返回可读正文，错误码 230020/99991400/11232 仅由 Shiori 实现确认，不宣称已完成平台实证。[飞书限流实现][s-feishu-api]

计划 typing/工具进度为短暂状态：仅当前获准聊天、有限频率、结束/取消/撤权立即停止；只使用宿主批准的简短公开文案，不发工具参数、私有路径、推理或凭据，不写成最终聊天记忆。PR #230 已有出站 dispatch guard，但 stream_start 尚未携带同一回调；未来所有异步流式步骤要逐次遵守此边界。[Shiori 进度][s-tg-progress]、[待合入 dispatch][m-pr230-dispatch]

## 8. 已提取与计划：Grok 接入的实际含义

Shiori Grok Bot 是本机回调桥加外发 Webhook，不是 xAI 模型 API。启用只校验配置、监听 127.0.0.1 并生成本机客户端连接文件，不发生远端握手；外部 Routine 需要能在同一电脑执行随包客户端。UI 必须区分“本机入口已启用”和“真实双向收发已验证”。[连接][s-grok-connection]、[运行前提][s-grok-readme]

协议只接受 message_id/text/reply_to，角色与 peer 来自连接；ID 最长 128、正文 16000，服务端另限制 65536 字节请求体和 16 连接。默认单链交流预算 12（可配 2–100），输入输出都计数；收据 512、单链 pending 16，重启不清除已存额度。[协议][s-grok-protocol]、[配置][s-grok-config]、[服务][s-grok-server]、[账本][s-grok-exchange]

HTTP 200 只表示接受，不意味着完成，没有平台消息 ID；超时/断线进入 uncertain 且不自动重试。更换 Webhook 形成新 peer，旧历史保留；回调密钥不能写入可复制 Routine 指令文案。[传输][s-grok-transport]、[更换连接][s-grok-accounts]

计划保留四种投递状态，另存 receiptKind=platform_message / transport_acceptance；Grok 只有真实 HTTP 接受证据时可标为后者，界面显示“已提交 Bot”，不能伪造平台消息 ID 或显示“Bot 已回复”。其他平台缺少应有消息 ID 仍为 uncertain。本机回程不要求公网端口；生成不含密钥的 Routine 配置与验证真实双向收发是两个独立步骤。

计划采用严格关联：未知、属于其他连接、已淘汰的 reply_to 均拒绝且不刷新额度；这是比 Shiori 当前实现/测试更严格的目标。有限保留账本只能保证保留期内去重，不承诺永久去重。入站先持久准入再进入角色队列；崩溃恢复不重放不确定出站，保留原消息 ID 与关联。

Grok 默认仍为外部来宾权限；Shiori 的可选 allowFullTools 授权模型和动态插件加载继续在本 Spec 范围外。新增连接纳入既有平台/账号启停、目标目录和撤权检查，不让 payload 自选角色、主人身份或接收目标。

## 9. 计划：必须覆盖的场景验收

| 场景 | 可观测结果 |
| --- | --- |
| 私聊配对后到多个群普通发言、自然聊天、被他人引用 | 当前主人和被引用主人分别正确标记；伪造昵称/@/元数据不授予身份；群历史不出现私人原文。 |
| 在私聊上传图片，再从桌面接续并搜索 | 同一授权历史语义，角色和来源不丢；媒体按授权可用；超预算正常裁剪，不能仅得到 24 条纯文本。 |
| 配对前启动、配对后完成的旧回合；解绑重配 | 旧回合不跨起点共享；重配后新的起点生效，旧句柄不复活；重复配对保留既有起点。 |
| 查询、下载、限流等待或流式发送时解绑/换账号/关群回复 | 已确认投递保留真实结果；等待后不得继续读取、回退、发下一段或发送进度，不能自动恢复旧授权。 |
| 同一群友跨两群；不同应用恰有相同 open_id | 前者共用档案并保留两群来源，后者不串资料；跨角色始终隔离。 |
| 访客产生档案后配对；随后解绑 | 当前主人从成员 UI/提取/注入排除；旧资料未被提升为主人记忆；历史主人发言仍保留其原身份。 |
| 六个新摘要、三天边界、主人私聊绑定变化 | 只取符合范围的最新五条；外部回合排除自身；摘要只读不刷新时间；普通/主动/Drift 按各自路径一致。 |
| 用户保存、清空、改回原文、删除时整理在途 | 陈旧结果不覆盖，冲突不推进游标；用户草稿保留；重新读取后的新整理可以按已说明语义重建。 |
| 两群同时整理同一成员；整理时删除角色 | 冲突方重读重试而非覆盖；角色删除后无资料复活。 |
| 旧应用账号已删、资料冲突、迁移中断重跑 | 未知作用域隔离；旧文和来源可核对；不产生重复记录、不丢编辑、不重发历史。 |
| QQ 仅 file_id、文件超限、下载后撤权 | 适配受管版本取得直链；拒绝或失败理由明确；无任意本机路径读取，撤权后内容不交给模型。 |
| Telegram emoji/格式/分段、源引用已删、限流 | 实体偏移正确；仅当前段确定拒绝可恢复；前段不重复；同目标回退，不确定尾部可见。 |
| Grok 未握手、重复/冲突 ID、未知父消息、重启额度 | 状态不虚报在线验收；去重不重复执行；错误关联拒绝；预算保留；不确定出站不重放。 |

这些是后续实现的验收要求，本轮仅静态源码与现有测试核验；未通过真实 QQ/Telegram/飞书/Grok 联调，不能据此宣布平台验收完成。

[s-member-key]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/memory/member_profiles.py#L39-L81
[s-qq-channel]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/qq/backend/accounts_inbound.py#L68-L84
[s-tg-channel]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/bots.py#L97-L112
[s-feishu-channel]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/feishu/backend/accounts.py#L90-L105
[s-qqbot-channel]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/qqbot/backend/account_channel.py#L22-L69
[s-context]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/conversation/context_scope.py#L226-L264
[s-boundary]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/conversation/context_scope.py#L45-L85
[s-history]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/context.py#L61-L89
[s-hub]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/channels/hub.py#L126-L205
[s-projection]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/packages/sdk/python/shiori_sdk/channels/projection.py#L16-L71
[s-reply]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/packages/sdk/python/shiori_sdk/channels/reply_context.py#L53-L64
[s-identities]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/identity/models.py#L81-L199
[s-bind]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/identity/store.py#L73-L240
[s-source]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/packages/sdk/python/shiori_sdk/channels/message_source.py#L129-L208
[s-member-store]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/memory/member_profiles.py#L99-L194
[s-member-render]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/memory/member_profiles.py#L269-L352
[s-activity]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/memory/group_environment.py#L25-L197
[s-activity-passive]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/prompt_block.py#L436-L514
[s-activity-proactive]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/proactive_turn/prompt_context.py#L258-L265
[s-activity-drift]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/drift_turn.py#L368-L374
[s-assembler]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/prompting/assembler.py#L48-L84
[s-memory-ui]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/desktop_bridge/phone_memory_requests.py#L100-L216
[s-phone-info]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/phoneChatInfo.ts#L29-L40
[s-phone-screens]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/phone/PhoneConversationScreens.tsx#L25-L63
[s-external-writes]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/memory/external_writes.py#L46-L142
[s-edit-tests]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/tests/backend/core/memory/test_external_writes.py#L138-L253
[s-consolidation]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/memory/markdown/consolidation.py#L214-L303
[s-external-input]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/memory/markdown/external_segment.py#L85-L225
[s-external-prompt]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/memory/markdown/external_segment.py#L260-L312
[m-bind]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/multichannel.py#L989-L1106
[m-authority]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/source_authority.py#L42-L154
[m-tool-authority]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/main.py#L4977-L5027
[m-search]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/agent_runtime/message_history_tools.py#L19-L149
[m-snippets]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/multichannel.py#L1108-L1133
[m-owner-messages]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/multichannel.py#L1135-L1175
[m-runtime-history]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/session_store.py#L789-L943
[m-attachment]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/attachment_tool.py#L22-L61
[m-account-snapshot]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/multichannel.py#L55-L95
[m-consolidation]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/consolidation.py#L49-L187
[m-group]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/group_tools.py#L78-L238
[m-adr]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/docs/adr/0009-multichannel-conversation-threads.md
[napcat-file]: https://napneko.github.io/develop/file
[napcat-group]: https://github.com/NapNeko/NapCatQQ/blob/3420f0b76d7dc0ac92cde1663f21ea27f61e13bb/packages/napcat-onebot/action/file/GetGroupFileUrl.ts#L8-L36
[napcat-private]: https://github.com/NapNeko/NapCatQQ/blob/3420f0b76d7dc0ac92cde1663f21ea27f61e13bb/packages/napcat-onebot/action/file/GetPrivateFileUrl.ts#L8-L29
[s-qq-files]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/qq/backend/channel/files.py#L19-L142
[tg-entities]: https://core.telegram.org/bots/api#messageentity
[tg-retry]: https://core.telegram.org/bots/api#responseparameters
[tg-reply]: https://core.telegram.org/bots/api#replyparameters
[s-tg-sending]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/utils/sending.py#L72-L138
[m-tg-sending]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/channels/telegram.py#L108-L142
[s-feishu-send]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/feishu/backend/channel.py#L718-L764
[s-feishu-api]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/feishu/backend/api.py#L21-L28
[qq-message]: https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html
[s-tg-progress]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/channel/outbound.py#L247-L268
[m-pr230-dispatch]: https://github.com/sawaki-zexi/meido/blob/25de6e2e94d9c375587138bcc2aa163152bbbbf0/backend/app/channels/host.py#L635-L646
[m-runtime-release]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/agent_runtime/runtime.py#L412-L429
[m-runtime-timeouts]: https://github.com/sawaki-zexi/meido/blob/8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750/backend/app/main.py#L6252-L6256
[s-grok-connection]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/backend/connection.py#L90-L137
[s-grok-readme]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/README.md#L7-L65
[s-grok-protocol]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/backend/protocol.py#L17-L46
[s-grok-config]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/backend/config.py#L8-L62
[s-grok-server]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/backend/callback_server.py#L14-L40
[s-grok-exchange]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/backend/exchange.py#L11-L138
[s-grok-tests]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/tests/test_exchange.py#L43-L79
[s-grok-transport]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/backend/transport.py#L54-L77
[s-grok-accounts]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/grok_bot/backend/accounts.py#L149-L225
