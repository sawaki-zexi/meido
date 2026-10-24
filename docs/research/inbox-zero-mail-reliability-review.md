# Inbox Zero 邮件可靠性复核（#201）

状态：当前；研究结论及本轮实现验证记录。功能范围以 #201 规格为准。

最后核验：2026-10-24（Asia/Shanghai）。

研究基线：Inbox Zero `main@aea0ec2db43a1147b90776d9bbf3f467afc0af26`，通过 GitHub `gh api` 固定读取；Meido 初次比较快照为 `feat/201-mail-draft-assistance` 的 `50494bd2b92db6fb123f542812703c686bf2407b`。后续源码复核覆盖提交 `45fea9b` 和当前提交 `734a4dba69a990d239a395c7d7141ecfdb1dd6f1`；前者是研究记录的固定基线，不代表当前 HEAD。未连接真实邮箱，未调用真实模型，未发送或删除邮件。

事实来源：

- [Inbox Zero sender-reply-examples.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/reply-tracker/sender-reply-examples.ts)
- [Inbox Zero sender-reply-examples.test.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/reply-tracker/sender-reply-examples.test.ts)
- [Inbox Zero generate-draft.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/reply-tracker/generate-draft.ts)
- [Inbox Zero draft-reply.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-reply.ts)
- [Inbox Zero draft-context-metadata.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-context-metadata.ts)
- [Inbox Zero draft-attribution.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-attribution.ts)
- [Inbox Zero reply-context-collector.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/reply-context-collector.ts)
- [Inbox Zero draft-management.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/choose-rule/draft-management.ts)
- [Inbox Zero execute.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/choose-rule/execute.ts)
- Meido [mail-draft-assistance-spec.md](../specs/mail-draft-assistance-spec.md)、[ADR-0008](../adr/0008-qq-mail-draft-boundary.md)、`backend/app/mail/{adapter,service,store,preparation}.py`。

## 结论

Inbox Zero 提供了可直接验证的检索和可追溯输入做法，但其默认草稿策略与 #201 的主人决定边界不同。#201 应借鉴“按对象检索已发送回复、验证收件方向、记录输入元数据、按身份和版本保护异步草稿”的具体约束；应保留 Meido 的 IMAP-only、主人审核发送、追加草稿版本和不确定远端写入状态。不能照搬 Inbox Zero 的“低置信度仍可为所有邮件起草”或“读取旧稿失败后继续替换”的行为。

本期最有价值且可局部落地的可靠性顺序是：

1. 先固定当前线程与同对象历史的身份、方向、时间排序和来源指纹。
2. 将模型结果严格限制为 `no_reply`、`needs_input`、`draft`，并把问题绑定到账号、邮件、线程指纹、问题 ID 和请求 ID。
3. 保留生成时实际注入的来源清单、读取失败和模型归因；来源是可追溯输入，不是逐句事实核验。
4. 在写入前后继续使用现有 marker、回读和人工修改保护；取消或任务停止只能进入待核实，不能自动补建。

## 一手证据与比较

### 1. 草稿上下文、澄清和缺失事实

Inbox Zero 的 `draft-reply.ts` 规定当前线程事实优先，显式写作风格优先于学习风格，并要求模型在自己的事实真正未定时说明不确定性。但同一提示词还要求：对于模型认为用户自己可以回答的决定、状态或计划，“说明最可能答案并把不确定性放进 confidence”；返回契约只有 `reply` 和 `confidence`。`DEFAULT_DRAFT_REPLY_CONFIDENCE` 为 `ALL_EMAILS`，也就是默认即使不确定仍可生成草稿。

这和 #201 的 `needs_input` 是有意不同的产品边界：例如“是否接受周五面试”缺少主人决定时，Meido 必须保存具体问题，不能由模型替主人选择。置信度只是模型自评，不能成为绕过主人确认的理由。#201 的 `preparation.py` 已把旧 `needsReply/body` 兼容为二态；新路径应继续拒绝显式矛盾结果，并要求 `needs_input` 有非空问题、没有正文。

Inbox Zero 的 `reply-context-collector.ts` 是可选的多步历史代理：只搜索最近六个月，搜索结果按线程展开、去重，最多每查询扩展 5 个线程、40 封邮件，每线程最多 12 封，完成工具调用或步骤超过 25 停止；失败返回空结果并记录错误。这是较重的能力，不是 #201 必需的第一阶段。Meido 当前一次模型调用已经能返回三态，继续保持一次调用更符合本票的成本和恢复边界。

**对 #201 的建议：** 保留一次结构化调用；把“缺少主人决定”作为硬业务状态，不用 confidence 替代；`reason/questions` 与判断一起持久化，重启后仍能查看和续办。若未来需要独立分类开关，再单独评估是否拆模型阶段。

### 2. 身份、幂等和取消

Inbox Zero 的发送草稿执行在 `execute.ts` 中按每个 action 持久化结果和错误，并在动作成功后记录 provider draft ID。其 compose 引擎的 `StoredReplyDraft` 身份包含 `emailAccountId`、`threadId`、`messageId`；内容有 `requestId`、revision、provider draft ID 和 provider message IDs。写入前检查 revision，账号 epoch 变化会拒绝旧写入；同一 compose session 的邮箱操作串行排队。邮箱创建结果不确定时会写 `providerDraftCreationUnconfirmed`，后续不会盲目重复创建。

一手定位：[compose 身份、请求与 revision 检查](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/mail-engine/reply-drafts.ts#L202)、[每 session 操作串行化](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/app/%28app%29/%5BemailAccountId%5D/compose/draft-operation-queue.ts#L11)、[未知创建与已丢弃 compose 不复活的测试](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/mail-engine/reply-drafts.test.ts#L276)。这是 compose 路径的证据，不能据此声称所有 Inbox Zero 后台任务都使用同一取消协议。

Inbox Zero 的自动替换路径（`draft-management.ts`）会在读取旧稿或删除旧稿出错时记录错误并返回 `shouldCreateDraft: true`。这在其 Gmail/Outlook 执行模型中是可接受的取舍，但违反 Meido ADR-0008 的 QQ IMAP 边界：远端结果不明时不得自动追加可能重复的草稿。

Meido 当前 `service.py` 已有更严格的基础：`task_key`、`request_key`、账号范围、`task_claim`、marker 回读、`uncertain` 状态、人工修改检测和重新生成追加版本；取消会 shield 远端写入并把任务置为待核实。`enqueue_answer` 还校验问题 ID、邮件 ID、当前线程指纹和同请求答案一致性。

**对 #201 的建议：** 不要引入 Inbox Zero 的旧稿删除替换策略。确保每次准备结果和补充答案都使用稳定的 `(账号, mail_id, question_id, request_id)` 身份；相同请求相同内容返回原结果，不同内容拒绝；开始补充前重新同步并重算线程指纹；取消、超时或回读失败统一保持 `uncertain`，只允许按原 marker 核对。

本次本地复核已确认两个需要优先补齐的具体缺口，均在 #201 的现有职责内：

1. **已保存补充准备结果的恢复也必须校验问题指纹。** 初始检查版本仅在 `question_id and preparation is None` 时重新同步、校验 parent question；模型完成并保存 preparation 后若进程停止，恢复可绕过该检查。本轮已让尚未写入远端的补充恢复走同一账号/问题/最新线程指纹验证；已有完成请求返回原任务。合成恢复测试已证明：保存补充结果后写入中断，新来信到达后恢复会拒绝旧答案，不追加草稿。
2. **角色补充任务必须携带授权来源到 worker。** `role_action(answer)` 曾只把问题 ID、答案、请求 ID 入队，没有固定 `sourceRoleId/runId`；worker 因缺少来源角色而可能跳过写前 `roleAccess` 撤销检查。本轮已持久化可信角色来源，并在 worker 执行和草稿写入前重新核验；网页入口不能自由指定角色来源。合成队列测试证明：入队后撤销许可，任务失败且不追加草稿，原运行身份保留在任务中。

这两项是 Meido 现有源码与 #201 撤销/恢复验收的差距，不是声称 Inbox Zero 也有相同 Bug。可借鉴的固定来源证据是其 [账号 epoch 和 revision 拒绝陈旧写入](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/mail-engine/reply-drafts.ts#L306) 和 [动作固定原执行上下文并记录结果](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/choose-rule/execute.ts#L39)。Meido 的权限撤销规则本身以 #201 Spec 为准。

2026-10-24 的继续复核另外发现并修正了三个边界：

1. **远端 APPEND 已开始但结果未知时不能自动再次写入。** 旧路径把 `uncertain` 也放入普通批处理；若远端暂时查不到 marker，重试仍会 APPEND。当前任务表持久记录 `write_started`，恢复时仍会查询原 marker；找不到时保留 `uncertain`，不重复写入。旧版运行中/不确定任务按可能已写入迁移，主人可明确创建新版本。
2. **任务固定账号连接代次。** 邮箱用户名哈希只能区分当前账号，不能识别 A→B→A。当前连接保存递增的 `account_epoch`，队列任务携带该代次，worker 执行时校验；问题准备结果也绑定同一代次。
3. **当前来信只进入模型上下文一次。** 当前邮件作为独立 `body` 注入；线程历史排除目标邮件，并仅选目标邮件之前最多八封，避免重复加权或把后续邮件提供给较早来信的回复。

4. **不确定 APPEND 必须显式分流。** 详情会标记仍可能存在未核实的远端写入；普通重试只读取原 marker，找不到时不调用模型、不重新追加。主人确认后才允许使用新请求键和新 marker 创建独立版本；如果原 marker 后续被找到，先恢复原版本再创建新版本。该交互保留 Inbox Zero 对 provider 草稿身份和 revision 的借鉴，同时继续遵守 ADR-0008 的 IMAP-only 与不盲目重试边界。

5. **后台对账不能形成轮询风暴。** 未确认 APPEND 任务现在记录最近核对时间、下次核对时间和指数退避次数；自动同步跳过退避窗口，主人主动核对仍立即执行。任务 worker 领取前按账号范围和连接代次过滤，并将旧代次排队任务标记为失效。

6. **生成输入需要可复现归因。** 准备结果增加模型配置 ID、prompt 输入哈希、消息数量和输入字符量；网页显示短哈希用于定位同一输入版本，不把正文写入日志。

7. **提醒投递需要和草稿写入一样具备领取边界。** 线程提醒和批次提醒现在都先以 SQLite 事务原子认领为 `sending`，并在服务重启时把中断的 `sending` 恢复为可重试的 `failed`；并发调度入口不会同时投递同一提醒。角色不可用时认领后立即退回 `pending`，不会把提醒永久卡在发送中。

本轮又修正了两个互操作边界：首次从 QQ 读取到 `UIDVALIDITY` 时，如果此前只有 UID checkpoint，也会把旧命名空间保留并从 UID 0 开始；Sent 邮件没有 `Reply-To` 或该字段只有主人地址时，催复草稿改用已解析的 To/Cc/Bcc 外部收件人，避免把主人误当成收件人。

普通回复草稿也沿用同一收件人安全规则：Reply-To 只有在包含外部地址时才优先，否则回退到 From；若两者都没有主人以外的地址，在 APPEND 前失败，不把草稿写给主人自己。

本轮对照 Inbox Zero 的线程状态规则又将分类器升级为 `signals-v3`：主人只发送报告、信息或建议而没有明确请求回复时按已处理关闭；明确“请回复/请查看/请反馈”等请求才进入 `awaiting_reply`；对方尚未兑现的承诺在主人发送致谢后仍保留等待状态，避免误触发或漏掉催复。

### 3. 已发送历史样本和排序

`collectSenderReplyExamples` 是 Inbox Zero 最明确的可复用做法：

- 以规范化发件人地址调用 `getThreadsWithParticipant`，最多搜索 8 个线程。
- 排除当前消息 ID。
- 只保留 provider 认定为已发送的消息。
- 解析 To/Cc/Bcc，只有实际收件人包含该发件人才保留；给其他人发送的邮件不会混入。
- 按 provider 的内部日期降序排序，取最近 3 封，每封正文最多 600 字符。
- 同发件人为空或本身就是主人账号时直接返回空；读取失败只记录 warning 并让草稿继续。

对应测试验证了：当前线程已发送副本被排除、收件方向正确、最近三封顺序正确、长正文截断、来信和其他收件对象不会混入。

Inbox Zero 的当前线程在 `fetchThreadAndConversationMessages` 中显式按 `internalDate` 升序排序，并注释说明 provider 顺序不能盲信；历史代理也过滤当前线程 ID 和 thread ID。Meido 的 `related_sent` 本轮已改为按 `To/Cc/Bcc` 有界读取最多 24 封，并精确校验这些头中的地址；service 层再次核验实际收件人和主人发件人。`upsert_item(..., direction="outbound")` 会标记 `sent`，`pending_items` 和 `list_items` 只选 inbound。这满足了 #201 的方向安全基础。

本轮已处理两个边界：

1. QQ 的 `related_sent` 结果仍由 IMAP UID 限制有界读取；取回后按解析的 UTC 日期排序。`thread_items` 也按解析后的 RFC/ISO 日期排序，不能解析的日期排在最前，同一时间按 Message-ID 和本地 ID 稳定排序。
2. prompt 截断正文时，来源同时保存完整正文与实际提供片段的指纹、字符数和截断标记；网页显示提供字符数与警告，避免暗示模型看到了全文。

**对 #201 的建议：** 保持 24 封有界读取和最多 3 封样本，但以解析后的 UTC 时间排序并用 message ID/UID 做稳定并列键；当前线程按同一规则从旧到新组装。来源记录方向、Message-ID、References、In-Reply-To、时间、账号和正文指纹；已发送记录永远不进入自动待回复队列。不要把旧样本日期、金额或承诺当作当前事实。

### 4. 归因和实际输入来源

Inbox Zero 的 `draft-attribution.ts` 把 provider、modelName 和 `DRAFT_PIPELINE_VERSION`（当前 30）写入生成结果；注释要求每次改变 prompt、检索、路由或后处理时递增版本。`draft-context-metadata.ts` 记录可复用的输入摘要：记忆 ID/类型、知识库是否注入、发送者历史数量、同对象样本数量、写作风格是否自定义、外部工具/会议/附件是否注入等。缓存草稿同时保存 attribution 和 context metadata，旧记录字段可选兼容。

这提供的是“生成时应用实际选取了哪些输入”的审计，不是逐句来源证明。尤其是同对象样本只说明样本被注入，不说明模型使用了其中每个事实；confidence 也只是同一次模型的自评。

Meido 当前 `_draft_content` 已收集主人资料的 document ID、标题、位置、安全 URL、正文指纹；线程和 sent sample 也记录邮件 ID、Message-ID、收发人、时间、方向和指纹；读取异常会进入 warnings。当前还保存 provider、模型、流水线版本和上下文注入统计，并对历史回复去除明确引用尾部后再注入模型。该记录方向正确。每个 preparation/draft version 独立保存，后续生成不能覆盖旧来源；`safe_source_url` 继续拒绝带凭据的链接；普通日志不复制邮件全文。

**对 #201 的建议：** 展示名称使用“提供给模型的资料”或“生成时读取的上下文”，列出邮件/资料身份、标题/位置、时间、截断和失败项；增加稳定模型身份/流水线版本。不要允许模型自行提供来源链接，也不要把该面板称为“事实已核验”。

## 面向 #201 的验收建议

以下是与现有规格一致、可由合成邮箱和模型测试验证的具体行为：

1. 老师的相关主人回复之后有超过 24 封无关已发送邮件时，按对象检索仍能找到它（在最多 24 封同对象匹配范围内）；最近无关邮件不会挤掉它；发送副本不会进入 inbound pending 队列。该设计不保证取回第 25 封更早的同对象匹配。
2. 同一线程以解析日期从旧到新提供给模型；日期相同有稳定 message ID/UID 排序；样本正文截断和失败状态在来源详情可见。
3. 结构化结果分别可保存 `no_reply`、`needs_input`、`draft`；无效或互相矛盾结果失败，不默认为 no reply；`needs_input` 没有正文且问题具体非空。
4. 同一问题 ID 和 request ID 重放返回原准备/草稿结果；相同请求提交不同答案拒绝；新来信、线程指纹变化、账号切换使旧问题失效。
5. 取消、超时、进程重启或 QQ 回读异常不会重复追加草稿；任务进入 `uncertain` 并允许按原 marker 核对。人工修改草稿不被普通重试覆盖，重新生成只追加新版本。
6. 生成详情可定位当前线程、同对象样本、主人资料、模型身份、生成时间、正文指纹和读取失败；不包含凭据或未经约束的模型 URL。
7. 补充答案在模型完成、准备结果保存后停止；新来信到达再恢复时，不写旧答案草稿。角色补充先入队再撤销许可，worker 不调用模型或追加草稿，且审计保留原角色/运行身份。

## 不应照搬的 Inbox Zero 行为

- `ALL_EMAILS` 默认允许低置信度草稿。#201 的主人决定必须走 `needs_input`，不能用 confidence 放行。
- 旧稿读取/删除异常后继续创建替代草稿。Meido ADR-0008 要求远端不确定时待核实。
- 多步历史代理作为第一阶段前提。它有 6 个月搜索、工具调用和 25 步上限，成本和恢复面明显大于本票要求。
- 自动从草稿编辑学习长期风格。没有可靠的实际发送关联时，编辑内容可能包含本次临时事实；#201 只做显式主人写作偏好。
- 将 context metadata 或 attribution 描述成事实校验。它们只能说明输入和模型身份。

## 研究限制

本报告只读取公开 GitHub 源码和 Meido 本地源码/测试；没有运行 Inbox Zero 的完整应用、没有连接 Gmail/Outlook/QQ、没有调用真实模型。Inbox Zero 的行为结论固定到 `aea0ec2...`，Meido 结论覆盖 `45fea9b` 和当前提交 `734a4db`。

## 本轮验证

本轮工作区验证：邮件相关 Python 回归 `164 passed, 40 deselected`；`npm run test:web -- --run web/src/features/settings/MailSettings.test.tsx` 通过 16 项；`npm run build`（含 TypeScript 和桌宠构建）、`python -m compileall -q backend scripts tests` 及 `git diff --check` 通过。新增测试覆盖流水线归因、prompt 哈希、上下文元数据、输入预算、低信心提示、引用清理、不确定 APPEND 对账退避、账号连接代次、线程跟进 API/提醒代次、提醒原子认领与重启恢复、提醒投递中状态展示、旧数据库迁移、角色查询、网页账号切换清理、发送关联状态筛选、UIDVALIDITY 空到已知命名空间切换、Sent 无 Reply-To 收件人回退和信息性发送/明确回复请求的跟进分类。验证使用合成 IMAP/模型，实际 SQLite、HTTP、任务和角色能力装配参与。真实 QQ 网页端编辑与手动发送仍需单独验收，不能以合成测试代替。
