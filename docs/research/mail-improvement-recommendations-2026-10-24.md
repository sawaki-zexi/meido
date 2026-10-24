# Meido 邮件改进建议：先判断对，再自动起草

状态：当前；本文区分已实现改进与仍需验证的建议，不表示真实邮箱验收已经完成。

最后核验：2026-10-24；Meido `feat/201-mail-draft-assistance` 当前提交 `734a4dba69a990d239a395c7d7141ecfdb1dd6f1`；Inbox Zero 固定提交 `aea0ec2db43a1147b90776d9bbf3f467afc0af26`。

事实来源：下列本地源码、测试，以及固定提交的 GitHub 原始源码。完整外部缓存的 HEAD 与固定提交不同，因此本次外部结论直接读取固定提交，未采用缓存工作树的新版本。

能力逐项状态见：[Inbox Zero 与 Meido 对照矩阵](inbox-zero-meido-parity-matrix-2026-10-24.md)。

**结论（阶段更新）：线程语义判断、逐事项问题/承诺跟踪、承诺截止/兑现关系、按线程行动视图、线程级处理 dossier、明确触发的催复草稿和默认关闭的到期自动催复草稿已经接入；下一步是用真实模型评估校准分类和草稿质量，并完成真实 QQ 长期验收。当前缺口主要是判断准确性和真实互操作，无需重写 IMAP 接入。**

## 已有能力，不再重复列为缺失

- **已提取：** 已保存文件夹 `UIDVALIDITY`，命名空间变化会重置 checkpoint；邮件来源键包含命名空间。[adapter.py:134](../../backend/app/mail/adapter.py#L134)、[store.py:406](../../backend/app/mail/store.py#L406)、[store.py:493](../../backend/app/mail/store.py#L493)
- **已实现：** 收件箱和已发送增量按 UID 从旧到新分页读取，完成每页后才推进 checkpoint；单次页数达到上限时下一次同步继续，避免批量变化跳过中间邮件。[adapter.py](../../backend/app/mail/adapter.py)、[service.py](../../backend/app/mail/service.py)
- **已实现：** 只读 `SELECT`、`LIST`、`SEARCH` 和 `FETCH` 的连接级瞬时错误会有限重连并指数退避；每次重连重新选择文件夹，`APPEND` 不自动重试，未知写入仍按 marker 对账。[adapter.py](../../backend/app/mail/adapter.py)
- **已提取：** 已有 `no_reply / needs_input / draft`、主人写作偏好、同对象已发送历史的方向过滤、最多 3 个样本、主人资料检索，以及来源和上下文元数据；这些已经覆盖 Inbox Zero 的部分上下文收集价值。[preparation.py:7](../../backend/app/mail/preparation.py#L7)、[service.py:1278](../../backend/app/mail/service.py#L1278)、[service.py:1306](../../backend/app/mail/service.py#L1306)、[同对象样本源码][iz-samples]
- **已实现：** 已有 Sent 副本关联、四种线程跟进状态、手动关闭及提醒幂等；当前也支持主人明确触发和默认关闭的到期自动催复草稿，仍需主人在 QQ 审核发送。[service.py:710](../../backend/app/mail/service.py#L710)、[service.py:840](../../backend/app/mail/service.py#L840)、[跟进测试](../../tests/python/test_mail_assistance.py#L701)
- **已提取：** 已有 8 个固定质量场景；模型调用直接返回 `case["model"]`，测试验证管道处理和上下文注入，不能证明真实模型理解了邮件。[回归测试:81](../../tests/python/test_mail_quality_regression.py#L81)、[夹具](../../tests/fixtures/mail_quality_cases.json)

## 1. 先让跟进状态理解“谁还欠什么”

**已提取：** Inbox Zero 的状态提示词要求检查整条线程：早先未回答的问题仍有效；区分主人承诺稍后回复或交付、对方承诺和明确结束；主人发送纯信息也可以结束线程。它最终输出状态与理由，不能把这些提示词视为准确率保证。[线程状态源码][iz-status]

**已提取（历史缺口，现已修复）：** 早期实现把最新 outbound 一律设为 `awaiting_reply`，最新 inbound 且为 `no_reply` 一律设为 `closed`；当前已改为线程信号分析并保留承诺兑现关系。[service.py](../../backend/app/mail/service.py)

**已提取（历史缺口，现已修复）：** Sent 同步确实把读取到的邮件写入本地 outbound；早期跟进刷新只从 `list_thread_ids(..., direction="inbound")` 取线程，完全由主人发起、尚未收到来信的新线程因此可能根本没有跟进状态。当前刷新已覆盖 inbound/outbound；行动接口也会为未持久化状态的本地线程即时分类。[service.py](../../backend/app/mail/service.py)、[store.py:595](../../backend/app/mail/store.py#L595)

**推断：** 按该规则，主人说“谢谢，事情办完”仍可能被提醒等待对方；对方说“我周五发材料，你不必回复”则可能关闭线程，漏掉尚未兑现的承诺。单封邮件不需回复与整件事情已结束需要分开。

**已实现；仍需深化：** 在现有四态上保存有来源的未解决问题、责任人、承诺、下一步动作和依据事件；承诺保存可解析的 `dueAt`，同一责任方后续明确完成时保存 `fulfilledByEventId`，并以 `nextDueAt` 驱动提醒。当前仍是信号规则，正文不足或历史被截断时还需继续加强 `unknown` 保护。

用户可见结果：能看到“等谁做什么、为什么还没结束、何时再跟进”。

验收：

- 主人只发送结束致谢 → `closed`，不继续催复。
- 主人明确承诺稍后交付 → 仍有主人待办；对方承诺周五交付 → 保持等待，不因“无需回复”关闭。
- 后续闲聊不能抹掉旧问题；每个待办都能定位依据邮件；已有人工关闭在事件未变时继续生效。

## 2. 把现有回放升级为真实模型输出评估

**已提取：** Inbox Zero 的评估调用实际起草函数，再按场景评审“不编造时间或价格、使用给定知识、遵循语气”等，并记录模型、判定及输出；部分断言使用语义评审器。它的“允许猜主人答案”等验收取向不适合直接照搬。[质量评估源码][iz-eval]

**已修正：** 原 `draft-uses-known-facts` 夹具把来信“请于 2026-11-05 前提交材料”预设成主人已承诺，现改为 `needs_input`；另以主人历史中明确承诺的场景覆盖可安全复述日期。夹具仍是确定性回放，不能说真实模型已经发生或避免了该错误。[质量夹具](../../tests/fixtures/mail_quality_cases.json)

**已实现第一阶段：** 保留确定性管道测试，并提供默认不触发的 `--live` 真实模型评估入口；固定场景已覆盖主人决定、来信要求与主人承诺的区别、旧历史日期/金额、多问题、附件事实不足和提示注入。评估记录 git HEAD、模型归因、Prompt hash、字符量、耗时和逐例结果；评估器还提供可注入的独立语义 judge 契约，非法或异常结果按失败处理。仍需在明确授权后运行真实账号、接入实际 judge 并完成人工抽检。

用户可见结果：后续修改提示词、模型或检索时，有依据判断草稿是否更准确，降低代替主人作决定和漏答问题的风险。

验收：

- 仅有对方要求时，不自动声称主人已经接受；缺少关键决定时保持 `needs_input`。
- 历史样本不能带入旧日期、金额或附件声明；同一输入可对比不同版本的实际输出。
- 保存模型、流水线版本、Prompt 哈希、耗时及逐例结果；语义评审配合人工抽检，不把 confidence 或来源清单当成事实核验。

## 3. 状态可靠后，再生成可审核的催复草稿

**已提取：** Inbox Zero 对 `AWAITING_REPLY` 可按开关生成催复草稿，对 `TO_REPLY` 只提醒。起草前重新读取线程，要求被跟踪消息仍是最新消息且确为主人发出，并使用原发信的 `To` 作为收件人。[到期处理源码][iz-process]、[催复生成源码][iz-followup]

**已提取：** Meido 现有跟进投递只发送提醒；已有未知 APPEND 按原标记核对、不得盲目重复追加的保护。[service.py:939](../../backend/app/mail/service.py#L939)、[未知写入测试](../../tests/python/test_mail_assistance.py#L278)

**已实现：** 增加“生成催复草稿”动作，并增加默认关闭的到期自动起草开关。两条路径都仅允许已确认等待对方且最新事件仍是主人实际已发送的线程；最新是对方来信的等待承诺场景继续只提醒。收件人必须明确指向对方，复用现有版本、来源记录、稳定任务键、APPEND 回读及对账保护。自动开关只在模型评估和长期 QQ 验收通过后考虑是否建议开启。

用户可见结果：到期后 QQ 草稿箱出现一封待主人审核的催复邮件；最终发送仍由主人在 QQ 完成。

验收：

- 开关关闭不自动创建；同一到期事件反复同步或重启最多追加一个版本。
- 起草过程中收到新回复、切换账号或撤销相关许可，最终写入前能拒绝陈旧任务。
- 写入结果未知只核对原标记；不覆盖主人修改、不删除旧稿、不自动发送。

## 4. 用“按线程的行动视图”整理现有功能

**已提取：** 当前页面按邮件条目展示，使用处理状态下拉筛选；跟进状态和关闭动作在选中邮件详情内，后台队列与对账统计也在同页。[MailSettings.tsx:88](../../web/src/features/settings/MailSettings.tsx#L88)、[列表:245](../../web/src/features/settings/MailSettings.tsx#L245)、[跟进详情:284](../../web/src/features/settings/MailSettings.tsx#L284)

**推断：** 用户需要跨多种底层状态判断下一步；同一线程多封信也不适合作为多个独立待办。这是把现有模块变得可用的机会，不需要扩展成完整邮件客户端。

**已实现第一阶段：** 增加“邮件 / 线程行动”视图，按当前账号每个线程显示一行，支持状态、下一步动作、关键词和分页筛选，直接展示最新邮件、截止时间、问题/承诺数量、草稿数和待核实写入数；未持久化跟进状态的本地线程由只读接口即时分类。UID、checkpoint、任务队列仍在状态摘要中展示，详细诊断入口可继续补强。

用户可见结果：打开邮件模块就知道今天该处理什么，可以直接补答案、看草稿、关闭跟进或核对未知写入。

验收：同一线程只出现一项行动；关闭或收到新回复后视图同步更新；待核实写入入口只执行原标记对账；搜索和详情仍使用同一账号边界。

## 当前剩余工作的优先级

1. **已实现第一阶段，仍需模型校准：** `signals-v3` 现在按问题片段和主题逐项保留 `questionId/topic`，部分回复不会清空其他问题；承诺兑现按责任方和主题关联；主人只发送信息时按已处理关闭，明确请求回复才进入等待；“本周五 / 下周五 / 周五”按明确的日历语义计算，带时区的邮件时间会保留来源时区再转换为 UTC。仍需用真实模型和真实线程样本校准歧义表达和跨语言同义词。[followup.py](../../backend/app/mail/followup.py)
2. **P0：运行并校准真实模型质量评估。** 入口、固定场景、可注入独立 judge 和 provider usage 归因已加入；下一步是在明确授权后调用真实模型，检查擅自接受期限、遗漏问题、旧历史中的日期/金额、未读取附件和邮件提示注入，并完成人工抽检。没有 provider usage 时报告会明确留空，不把字符数写成 token 或费用；现有夹具通过只能证明管道行为。
3. **已实现第一阶段，仍需增强交互：** 线程详情现在聚合待补准备结果、所有草稿版本与待核实写入；行动列表支持“待补充 / 已到期 / 需核实”筛选；问题、承诺和历史准备结果可以直接跳到依据邮件；长线程默认显示最新 6 封，可展开或收起较早邮件。后续应让处理状态、人工跟进覆盖和下一步提示在列表与详情中保持一致。[MailSettings.tsx](../../web/src/features/settings/MailSettings.tsx)、[service.py](../../backend/app/mail/service.py)
4. **P1：真实 QQ 长期互操作验收。** 验证断线恢复、网页改稿、主人手动发送后 Sent 副本延迟、命名空间变化及重启恢复，记录是否重复追加、是否丢失人工修改，以及自动催复开关的到期行为。当前仅完成合成邮箱和本地回归，尚无真实账号长期验收结论。

当前分支已完成线程行动 API、状态/动作筛选、事件变化后的快照失效、列表样式和默认关闭的到期催复草稿。本地邮件 Python 回归和邮件设置网页回归通过；这些结果不覆盖真实模型质量或真实 QQ 互操作。

## 不照搬与尚未验证

- **已提取／取舍：** Inbox Zero 提示词允许推测主人“最可能的答案”，由 confidence 表达不确定；Meido 明确要求缺少主人决定时 `needs_input`。保留 Meido 的边界，不改成“先猜再让主人编辑”。[起草源码][iz-draft]、[service.py:1466](../../backend/app/mail/service.py#L1466)
- **已提取／推断：** Inbox Zero 还有多步历史搜索代理，具备工具、时间窗口和结果上限；Meido 已有有界同对象检索。先用评估证明检索不足，再决定是否引入更多步骤和成本。[上下文代理源码][iz-context]、[service.py:1278](../../backend/app/mail/service.py#L1278)
- **已提取／取舍：** Inbox Zero 催复路径在本地跟踪写入失败时会尝试删除孤立远端草稿；Meido 的 ADR 禁止删除和覆盖原稿，不能照搬该清理方式，也不扩大到 SMTP、Gmail/Outlook 或附件复制。[催复生成源码][iz-followup]、[ADR-0008](../adr/0008-qq-mail-draft-boundary.md)
- **未验证／计划：** 本次只核验源码，未接真实 QQ、未调用真实模型。长期断线恢复、QQ 网页编辑、主人手动发送后副本到达及 UIDVALIDITY 变化仍需受控真实账号验收；本地构建通过不等于这些互操作行为通过。[真实验收范围](../adr/0008-qq-mail-draft-boundary.md#验证)

[iz-status]: https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/determine-thread-status.ts
[iz-process]: https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/follow-up/process.ts
[iz-followup]: https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/follow-up/generate-draft.ts
[iz-draft]: https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-reply.ts
[iz-samples]: https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/reply-tracker/sender-reply-examples.ts
[iz-context]: https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/reply-context-collector.ts
[iz-eval]: https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/draft-reply.test.ts
