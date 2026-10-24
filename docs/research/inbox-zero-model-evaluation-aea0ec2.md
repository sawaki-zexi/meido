# Inbox Zero 真实模型评估核验

- 状态：当前研究记录
- 最后核验：2026-10-24
- 固定外部基线：Inbox Zero `aea0ec2db43a1147b90776d9bbf3f467afc0af26`（官方仓库提交）
- 方法：通过官方 GitHub 提交树和 blob 阅读评估测试、评审器、报告器、CI 及起草调用链；没有运行 Inbox Zero、没有调用真实模型、没有连接邮箱或使用真实用户邮件。

## 结论

Inbox Zero 的 `draft-reply` 评估是一个可选的实时配置模型回归流程（provider 也可以是本地 Ollama/OpenAI-compatible），不是固定答案回放。评估测试把合成邮件和上下文送入生产起草函数；随后把同一组输入转换为模型证据，交给另一个模型做二元通过/失败评审。结果同时包含每个模型和测试的 pass/fail、紧凑的实际输出、评审理由，以及可选的成本、令牌和运行历史。

这套机制适合借鉴为 Meido 的质量闸门：固定场景输入、调用真实起草入口、使用独立评审模型、保存模型/提交/成本/实际输出和评审理由，并用确定性断言保护关键事实。它不能证明邮件事实已经核验，也不能把模型的 `confidence` 当成校准概率。Inbox Zero 的提示词允许模型给出“用户最可能的答案”，而 Meido 的主人决定缺失时必须保持 `needs_input`，不能照搬这个取舍。

## 真实模型评估怎样触发

评估套件由 `describe.runIf(shouldRunEval)` 包住。`RUN_AI_TESTS` 不是 `"true"` 时，测试会跳过；即使打开，也必须有评估矩阵中所需 provider 的凭据。未设置 `EVAL_MODELS` 时，矩阵使用目录中的默认 `gpt-6-luna`；设为 `all` 时使用目录中 `includeInAll` 不为 false 的模型；也可以传逗号分隔的目录简称或自定义 JSON 数组。每个模型通过 `getEmailAccountForModel` 注入 provider、model 和对应 API key，之后仍调用同一个生产函数。[触发与矩阵](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/models.ts#L11-L63)；[模型目录与选择](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/model-catalog.ts#L10-L179)

官方 CI 只在 `workflow_dispatch` 手动触发，设置 `RUN_AI_TESTS=true`、可选的 `EVAL_MODELS`、`EVAL_REPORT_PATH` 和 OpenRouter secret；普通 CI 不会因为提交自动调用模型。默认 workflow provider/model 与评审器默认值还可能不同，因此运行记录必须保留实际 provider/model。[手动 CI](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/.github/workflows/ai-evals.yml#L6-L18)；[CI 环境与报告上传](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/.github/workflows/ai-evals.yml#L53-L81)

## 评估输入

### 起草函数输入

每个用例调用 `draftForEval`，将 `currentDate` 缺省固定为 `2026-05-12T09:30:00Z`，然后直接调用 `aiDraftReplyWithConfidence`。输入契约包含：

- `messages`：带 `to` 的 `EmailForLLM` 消息数组；测试用 `getEmail` 构造发件人、收件人、主题、正文和日期，线程按从旧到新提供。
- `emailAccount`：账号身份、时区、关于主人、模型路由和可选 booking link。
- 事实/历史上下文：`knowledgeBaseContent`、`replyMemoryContent`、`emailHistorySummary`、`emailHistoryContext`、`senderReplyExamples`。
- 外部上下文：`calendarAvailability`、`meetingContext`、`recordedMeetingContext`、`mcpContext`、`attachmentContext`。
- 写作与输出上下文：`writingStyle`、`learnedWritingStyle`、`hasConfiguredSignature`、`currentDate`。

这些字段直接来自 `DraftReplyInput`。[输入类型](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-reply.ts#L88-L105)

生产起草器将输入拆成三段证据：线程（`getEmailListPrompt`，配置 `messageMaxLength: 3000`）、非线程上下文和“今天日期 + 以哪个邮箱身份写信”。模型 user prompt 还明确要求按当前线程事实优先、直接回答 sender，并在用户自己知道但上下文没有时写出最可能答案。上下文组装保留知识库、历史摘要、相似线程、同发件人回复样本、写作风格、会议/录音、日历、外部工具和选中的附件等区块。[证据构造与 user prompt](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-reply.ts#L127-L177)；[上下文优先级与区块](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-reply.ts#L185-L337)

评估场景覆盖调度链接与时段、非调度问题、知识库事实、同发件人样本、当前线程覆盖历史冲突、多问题完整性、缺少价格/业务/会议/附件事实、图片标记、会议上下文、写作风格和标点等。测试输入是合成数据和固定日期，不是真实收件箱样本；测试名称和场景定义见固定提交的 `draft-reply.test.ts`。[场景测试入口](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/draft-reply.test.ts#L22-L75)；[同发件人与冲突上下文场景](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/draft-reply.test.ts#L788-L904)；[缺失事实与附件/会议场景](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/draft-reply.test.ts#L1681-L2072)

### 评审器输入

起草结果中的 `modelEvidence` 由同一个 `DraftReplyInput` 再次生成，内容是 `evidence.thread`、`evidence.context` 和 `evidence.temporalAndIdentityContext` 的拼接；这避免评审器看到的上下文与生产提示词漂移。评审调用把这份证据作为 `input`，把起草正文作为 `output`，另传一段自然语言 `expected` 和一个 `criterion`。[评估包装函数](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/draft-reply.test.ts#L2241-L2260)

不是所有用例都依赖 LLM judge：一些用例用 URL、日期、时段或 `confidence !== HIGH_CONFIDENCE` 等确定性条件加上 judge；产品反馈场景用三个并行标准（knowledge base、voice、restraint）调用 `judgeMultiple`。因此“测试通过”并不等于每个输出都经过同一种评审。[确定性与多标准记录示例](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/draft-reply.test.ts#L678-L702)

## 模型输出

起草模型的结构化 schema 只声明两个顶层字段：`reply`（完整纯文本邮件正文）和 `confidence`（`LOW`、`MEDIUM`、`HIGH`）。系统提示还禁止主题、要求纯文本/换行，并在评估时要求输出唯一正文而不是多个候选。模型响应经过 `normalizeDraftReplyFormatting`，再映射为产品枚举 `ALL_EMAILS`、`STANDARD` 或 `HIGH_CONFIDENCE`；同一次调用通过回调记录实际 provider/model。[输出 schema、结果与调用](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-reply.ts#L339-L424)

如果检测到重复文本，起草器会再调用一次同一生成器；重试后仍重复则抛错。因此“每个测试一个模型请求”不是严格保证；还可能有历史上下文收集、日历或其他外部 AI 调用，评估报告的 usage 才能反映实际调用量。[重复输出重试](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-reply.ts#L396-L418)

运行时草稿结果另有两类可追溯字段，但它们不等于评估报告字段：

- `DraftAttribution`：实际 `provider`、`modelName`、`pipelineVersion`（该提交为 30）。[归因](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-attribution.ts#L1-L34)
- `DraftContextMetadata`：选中的 reply memory 数量/ID/类型/范围、知识库可用数量与是否注入、历史摘要/相似线程/同发件人样本数量、日历、写作风格、外部工具、会议、录音会议、附件及 confidence。部分新字段可选，以兼容旧缓存。[上下文元数据 schema](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/ai/reply/draft-context-metadata.ts#L8-L61)

生产路径将 `reply`、`confidence`、可选 `attribution`、`draftContextMetadata` 和附件写入 Redis 缓存；规则执行还把 `draftId`、草稿状态、模型 provider/name、流水线版本和上下文元数据写入 `ExecutedAction`。这些记录说明生成时使用了哪些类别的上下文，不能证明某一句事实来自某封邮件，也不是逐句事实核验。[Redis reply 记录](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/redis/reply.ts#L13-L84)；[ExecutedAction 字段](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/prisma/schema.prisma#L851-L903)

## Judge 和评估记录

`judgeBinary` 使用 `generateObject`，schema 为 `{ pass: boolean, reasoning: string }`；prompt 包含 criterion、完整 evidence、AI output、可选 expected，并要求二元 verdict。没有配置 `EVAL_JUDGE_*` 时，评审 provider/model 默认是 `openrouter` / `deepseek/deepseek-v4.1-flash`，但 `getModel` 仍会受实际环境配置影响。judge 异常会记录 debug artifact 并返回 `pass: false`；多个 criteria 并行评审，全部通过才算 `allPassed`。[Judge schema、prompt 与错误处理](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/judge.ts#L18-L127)；[默认 judge provider/model](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/judge-provider.ts#L1-L33)

每条 `EvalRecord` 的字段是：

| 字段 | 含义 |
| --- | --- |
| `testName`、`model` | 场景名和被评估模型标签 |
| `pass` | 最终通过/失败；测试会同时 `expect(pass).toBe(true)` |
| `expected`、`actual` | 期望说明和紧凑实际文本；常见 `actual` 包含正文 JSON 与 judge PASS/FAIL 及理由 |
| `criteria` | 可选的每条 judge 结果（criterion、pass、reasoning） |
| `durationMs` | 可选运行耗时；该 draft-reply 文件手动记录时通常未填 |
| `cached`、`cacheKey` | 可选结果缓存标记和键；该 draft-reply 文件使用 `record`，没有把缓存当成真实新调用 |

字段定义及重复 `(testName, model)` 保护见 `reporter.ts`。[记录契约](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/reporter.ts#L8-L75)；[draft-reply 实际记录](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/draft-reply.test.ts#L675-L702)

`printReport` 输出控制台比较；设置 `EVAL_REPORT_PATH` 时写 Markdown 和同名 JSON。启用 AI eval 时默认把每次运行写到 `.context/eval-results/<eval-name>/`，历史 JSON 另含 `schemaVersion`、`evalName`、`createdAt`、`cacheMode`、`gitHead`、`records` 和 `usage`。报告器订阅每次 AI 调用的 usage，汇总 provider/model、调用数、总/输入/输出/缓存/推理 token、估算费用以及 provider reported/upstream inference cost。[报告与历史字段](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/reporter.ts#L131-L194)；[usage 汇总](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/reporter.ts#L453-L546)；[usage 事件字段](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/utils/usage.ts#L18-L33)

可选 `EVAL_DEBUG_ARTIFACTS=true` 或 `EVAL_DEBUG_DIR` 时，judge 会把 criterion、input、output、expected、judge model、provider options、instructions、prompt 和原始结果/异常写 JSON。该调试文件适合失败复盘，同时也意味着输入和输出可能包含邮件正文，应按敏感数据处理。[debug artifact](https://github.com/elie222/inbox-zero/blob/aea0ec2db43a1147b90776d9bbf3f467afc0af26/apps/web/__tests__/eval/debug-artifacts.ts#L5-L72)

## 对 Meido 的可借鉴边界

### 建议借鉴

1. **真实入口 + 固定输入。** 质量评估应调用生产起草入口，固定线程顺序、上下文组合和当前日期；不要只测试 prompt 字符串或预填模型结果。评审证据应从同一组已解析输入构造，避免“生产 prompt”和“judge 看到的输入”漂移。
2. **矩阵化比较。** 用环境开关选择单模型、多模型或自定义模型；记录实际 provider/model 标签。评估默认关闭、手动或受控触发，避免普通 CI 隐式消耗 API 预算。
3. **二元语义评审 + 确定性护栏。** 对邮件正文使用独立 judge 的 pass/fail 和简短 reasoning；对 URL、日期、金额、附件、状态等可检测事实使用代码断言；多条标准全部通过才通过用例。
4. **保存可复盘记录。** 至少保存场景、模型、输入版本或 hash、期望约束、实际输出、judge 理由、代码提交、耗时和真实调用/令牌/成本。失败调试资料应可选开启并隔离敏感正文。
5. **区分输入可追溯与事实证明。** 可以记录资料类别、命中数量/ID、模型身份、流水线版本和 confidence，用于重现和定位；展示文案应说明“生成时注入的上下文”，不要宣称逐句来源核验。

### 不应照搬

1. **不要把低 confidence 当作主人决定的替代。** Inbox Zero 的系统提示允许模型对用户自己的决定、状态或计划写出最可能答案，默认 `ALL_EMAILS` 也允许不确定草稿。Meido 对“是否接受、是否承诺、是否确认”这类缺失决定必须返回 `needs_input`，等待主人回答。
2. **不要把单一 LLM judge 当真实准确率。** Judge 也会出错、与被测模型共享偏差，并且 `pass` 是二元主观判定；应保留人工抽样、多个模型/标准和固定回归集，不能由一轮全绿推出邮件事实正确。
3. **不要把合成 eval 当邮箱互操作验收。** 固定提交测试没有连接 Gmail/Outlook/QQ、没有真实线程乱序、断线、发送副本延迟或主人改稿；这些必须另做受控账号验收。
4. **不要无条件引入多步历史代理。** Inbox Zero 的历史上下文收集会再调用带搜索工具的模型代理，有步骤/结果上限和额外成本；Meido 应先评估有界确定性历史是否不足，再决定是否增加调用。
5. **不要默认记录或回放完整邮件正文。** judge debug artifact 会写 input/output，报告实际字段也可能包含正文摘要；Meido 应默认保留来源身份、指纹和必要片段，凭据及完整敏感正文不进入普通日志。
6. **不要误读 reporter 的可选字段。** `durationMs`、`cached`、`cacheKey` 是通用 reporter 能力，不代表当前 `draft-reply` 每例都有耗时或缓存；缓存命中也不应计为新的真实模型质量样本。

## 面向 Meido 的改进建议

- 建立独立的真实模型评估命令，默认跳过，显式环境变量和凭据才运行；先用单模型小集，再按相同输入比较模型或 prompt 版本。
- 输入场景优先覆盖：对方给出期限但主人未承诺、主人已明确承诺、旧历史含过期金额/日期、多问题只回答一部分、附件事实未读取、线程有注入内容、写作偏好与当前线程冲突。
- 让模型输出保持严格三态（`no_reply`、`needs_input`、`draft`），并对 `needs_input` 的具体问题做结构化校验；不要用自评 confidence 放行主人决定。
- 记录 `account/thread/message` 身份、实际注入来源 ID、截断/失败警告、模型 provider/model、pipeline version、prompt/context hash、耗时、token/cost 和 judge reasoning；报告和产品运行时记录分开保存。
- 语义 judge 只作为辅助证据，确定性断言和人工抽样共同构成验收；任何“真实回复质量提升”的结论都应引用真实模型运行记录，而不是固定夹具通过。
