# Inbox Zero 邮件后续改进建议

状态：当前；发送副本关联、线程跟进、按线程行动视图和默认关闭的到期催复草稿已实现，真实模型质量回归和 QQ 长期验收待继续。

最后核验：2026-10-24

事实来源：Inbox Zero `main` 固定提交 `aea0ec2db43a1147b90776d9bbf3f467afc0af26` 的源码；Meido 当前分支源码、合成 IMAP 复现和邮件测试。

这份记录只回答下一步做什么，不改变已确认的 IMAP-only、主人审核发送、草稿追加版本和不确定写入保护边界。

## 1. 先修相关历史的候选排序

**事实。** 早期实现曾在 IMAP 返回顺序中提前截断候选，导致较新的相关回复被丢弃；当前 `related_sent` 已在有界窗口内完整解析、精确过滤并按日期排序，重复结果由入库去重。[adapter.py](../../backend/app/mail/adapter.py:356) [adapter.py](../../backend/app/mail/adapter.py:315)

**已落地。** 仍只保证有界候选窗口内的最近样本，不承诺扫描全邮箱；模型继续最多使用 3 条样本。

**可借鉴的一手行为。** Inbox Zero 固定取最多 8 个相关线程，排除当前邮件，只保留主人实际发给该对象的已发送邮件，按 provider 日期降序取最近 3 封，每封正文限 600 字符：[sender-reply-examples.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/reply-tracker/sender-reply-examples.ts)。Meido 已有方向过滤、来源记录和 24→3 的边界，并已修复先截断 IMAP 返回顺序导致漏掉较新相关回复的问题。

## 2. 给 `no_reply` 提供纠错和状态分组

**事实。** 当前页面已为 `no_reply` 提供主人显式的“重新判断邮件”入口，并保留原 preparation/草稿版本。[MailSettings.tsx](../../web/src/features/settings/MailSettings.tsx:252)

**已实现。** 列表现在可以按 `sent_pending`、`sent_matched`、`sent_ambiguous` 和 `sent_unknown` 筛选发送副本关联状态，并分别显示“发送待确认”“已找到发送副本”“发送待核实”和“发送状态未知”。这些状态仍与来信处理状态分开，避免把发送关联的不确定性压缩成笼统的 `uncertain`。

**边界。** 不照搬 Inbox Zero 的自动低置信度放行：其草稿提示词允许模型给出“最可能答案”，返回契约只有 `reply` 和 `confidence`：[draft-reply.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-reply.ts)。Meido 的 `needs_input` 仍必须等待主人决定。

## 3. 补齐邮件搜索与定位

**可借鉴的一手行为。** Inbox Zero 的邮件引擎支持按正文搜索并把结果合并回线程列表，搜索结果与分页、线程状态和后续操作保持同一套身份边界：[search-body.test.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/integration/mail-engine/search-body.test.ts)。

**已实现。** Meido 邮件页现在支持在当前账号的本地已同步来信中搜索主题、发件人、收件人、正文和 Message-ID，查询限制为 200 字符，可与状态筛选和分页组合；后端返回过滤后的总数，搜索只读本地数据，不产生 QQ 端副作用。具体契约见 [mail-integration-spec.md](../specs/mail-integration-spec.md)。

角色入口同时提供只读 `stats` 操作，返回当前账号的状态分布、草稿数、未核实写入数和跟进线程数；统计不包含邮件正文，和网页搜索使用同一账号隔离。

## 4. 建立可重复的回复质量验收

**已实现。** Meido 现在有八个固定合成场景，覆盖主人决定、无需回复、对方给出期限但主人未承诺、主人已有明确承诺、多问题、附件事实不足、邮件提示注入以及写作偏好/相关历史；测试复用真实 `MailService` 和 SQLite，模型输出直接回放夹具中的 `model` 字段。因此它验证流水线契约与上下文传递，不能据此判断真实模型是否会编造承诺或误判无需回复。[质量夹具](../../tests/fixtures/mail_quality_cases.json) [回放测试](../../tests/python/test_mail_quality_regression.py) [验收边界](mail-quality-regression.md)。Inbox Zero 的草稿评估集按场景让语义评审器检查“不编造时间/价格/附件”“使用给定知识”“遵循写作风格”等行为，并记录每个用例的 pass/fail：[draft-reply.test.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/draft-reply.test.ts)。

**后续建议。** 扩充现有回归集，明确区分“对方给出了截止日期”和“主人已经承诺接受该期限”，增加未完成承诺、结束性致谢、引用旧问题及未经读取的附件等场景。经授权比较真实模型时，固定线程、相关历史、主人资料、写作偏好和期望约束，记录模型、prompt hash、耗时、输入字符量和人工抽检结果；用同一输入比较 prompt/检索改动，不把 confidence、来源元数据或夹具通过结果称为事实核验或真实模型质量评测。

## 5. 发送关联稳定后再做双向跟进

**已实现的能力。** Meido 通过 Sent 文件夹 UID checkpoint 增量读取发送副本，并对仍未匹配的草稿周期性重扫最近发送窗口；关联记录独立保存 `pending`、`matched`、`ambiguous` 和 `unknown` 状态。草稿缺失但已找到唯一发送副本时显示为已发送；候选不唯一时仍待核实。具体契约见 [mail-sent-association-spec.md](../specs/mail-sent-association-spec.md)。

**已实现。** Meido 保存线程级 `to_reply`、`awaiting_reply`、`closed` 和 `unknown` 状态、到期时间及幂等提醒账本；Sent 暂不可读时线程进入 `unknown`，提醒设置变更会取消旧调度并创建新的调度代次。线程刷新保存 `signals-v3` 的问题、承诺、下一步动作、截止时间、兑现事件和依据事件；主人发送纯信息不会被误判为等待回复，明确问题/请求才进入 `awaiting_reply`。主人可以明确请求为仍等待对方的线程生成一封可审核催复草稿，生成任务按线程事件指纹幂等，并复用 APPEND 回读和未知结果保护。网页提供按线程行动视图，未持久化状态的本地线程也能只读即时分类。新增默认关闭的“到期自动生成催复草稿”开关；只有 QQ 草稿能力完成验证时才会自动起草，最终发送仍由主人审核。详细行为见 [mail-followup-spec.md](../specs/mail-followup-spec.md)。Inbox Zero 的状态判断和到期处理源码仍作为比较依据：[determine-thread-status.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/determine-thread-status.ts)、[follow-up/process.ts](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/follow-up/process.ts)。

**后续建议。** 将当前规则分类器与真实模型评估集对比，重点校准多事项的兑现关联、部分问题回复和相对日期/时区，之后再决定是否增加默认关闭的自动催复草稿。继续验证真实 QQ 长期运行中的迟到同步、角色切换和提醒失败恢复；当前不引入 Gmail/Outlook 标签、发送、删除、归档或移动能力。剩余工作的具体优先级见 [邮件改进建议](mail-improvement-recommendations-2026-10-24.md#当前剩余工作的优先级)。

## 6. 同步身份和后台可观测性

**已实现。** QQ IMAP checkpoint 现在同时保存文件夹的 `UIDVALIDITY`。命名空间变化时 checkpoint 从 0 开始，旧邮件的本地来源键保留为历史身份，新 UID 不会复用旧记录；可用时解析出的邮件 Message-ID 兜底值也包含 UID 命名空间。只读 `SELECT`、`LIST`、`SEARCH` 和 `FETCH` 的连接级瞬时错误会有限重连并指数退避，每次重连重新选择文件夹；`APPEND` 不自动重试。状态和统计接口按当前账号公开文件夹 checkpoint、持久任务/作业状态、未核实 APPEND 的总数、到期数和对账尝试次数，网页显示这些运行态信息。

**验收边界。** 这覆盖了可由合成 IMAP 验证的恢复逻辑；QQ 服务器实际返回 UIDVALIDITY、文件夹重建和长期断线重连仍需真实账号验收。它不改变未知 APPEND 只能按原 marker 对账、不能盲目再次追加的边界。

## 不值得复刻

- Inbox Zero 的多步历史代理不是第一阶段必要条件；它带搜索工具、步数和结果上限，成本与恢复面都更大。Meido 先修有界确定性检索。
- 自动从草稿编辑学习长期风格仍需更长时间的实际发送样本和主人确认；当前继续使用主人显式写作偏好。
- 旧草稿读取/删除失败后自动创建替代草稿不适用于 ADR-0008；远端状态不明时继续待核实。
